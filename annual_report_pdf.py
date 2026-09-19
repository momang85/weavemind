# -*- coding: utf-8 -*-
"""年报 PDF 的文本提取与**页码定位**（F2′-2）。

为什么单独一层：A 股发行人的年报通常以 PDF 发布，`WebFetchWorker` 只会把 PDF 字节
按 UTF-8 解码成乱码（实机读数：候选里的年报/研报都是 `.pdf`，抓到的页面因此没有正文）。
这里把**单份公开年报**的下载与解析做成小工具，产出带页码的正文，交给
`narrative_evidence` 做小节切分与定位（`locator` 因此能写"第 N 页 · 小节：…"）。

边界（不做全市场文档平台）：
- 只处理 http/https，且**复用既有的公网校验抓取通道**（不新增服务端请求点）；
- 有大小上限与页数上限；扫描件/无文本层（提取不出文字）→ 明确返回缺口，不硬编；
- 不绕过付费、登录或反爬；访问受限就停在这个来源并说明。
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BYTES = 30_000_000
MAX_PAGES = 400
MIN_TEXT_CHARS = 40          # 全文少于这些字符基本是扫描件/受保护文档，按缺口处理


def looks_like_pdf(url: str, head: bytes = b"") -> bool:
    """URL 后缀或字节头判 PDF（不看标题文字）。"""
    if str(url or "").lower().split("?")[0].endswith(".pdf"):
        return True
    return bytes(head[:5]) == b"%PDF-"


def pages_text(data: bytes) -> list[str]:
    """PDF 字节 → 每页文本（pypdf；解析失败返回空列表，不抛）。"""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        out: list[str] = []
        for i, page in enumerate(reader.pages):
            if i >= MAX_PAGES:
                break
            try:
                out.append(str(page.extract_text() or ""))
            except Exception:
                out.append("")
        return out
    except Exception as exc:                     # noqa: BLE001 - 解析失败按缺口处理
        logger.warning("PDF 解析失败：%s", str(exc)[:140])
        return []


def doc_from_pages(title: str, url: str, pages: list[str]) -> dict:
    """每页文本 → 文档对象（正文 + `page_offsets`，供切分时定位页码）。"""
    text_parts: list[str] = []
    offsets: list[tuple[int, int]] = []
    pos = 0
    for i, page in enumerate(pages, 1):
        body = "\n".join(line for line in str(page or "").splitlines() if line.strip())
        offsets.append((pos, i))
        text_parts.append(body)
        pos += len(body) + 1                     # +1：拼接用的换行
    return {"title": str(title or ""), "url": str(url or ""),
            "text": "\n".join(text_parts), "page_offsets": offsets,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def fetch_bytes(url: str, *, timeout: int = 30, max_bytes: int = MAX_BYTES) -> bytes | None:
    """下载 PDF 字节（复用**既有**公网校验抓取通道；超限或受限返回 None，不抛）。

    走 `adapters.transport.get_via_urllib`（协议/主机/IP 边界校验紧邻请求点），
    用 latin-1 取回再编码回字节——PDF 是二进制，latin-1 是 1:1 字节映射，可原样还原；
    这样不必新增一处服务端请求点。
    """
    try:
        from adapters.transport import get_via_urllib
        text = get_via_urllib(url, timeout=timeout, encoding="latin-1")
        data = str(text).encode("latin-1", errors="replace")
        if len(data) > max_bytes:
            logger.warning("PDF 超过大小上限（%d 字节）：%s", len(data), str(url)[:120])
            return None
        return data
    except Exception as exc:                     # noqa: BLE001 - 受限/失败按缺口处理
        logger.warning("PDF 下载失败（%s）：%s", str(url)[:100], str(exc)[:120])
        return None


def doc_from_url(url: str, title: str = "", *, data: bytes | None = None) -> dict | None:
    """URL → 文档对象（下载 + 解析 + 页码偏移）；拿不到正文返回 None。"""
    raw = data if data is not None else fetch_bytes(url)
    if not raw:
        return None
    pages = pages_text(raw)
    if not pages or sum(len(p or "") for p in pages) < MIN_TEXT_CHARS:
        logger.info("PDF 无可提取文本（扫描件或受保护）：%s", str(url)[:120])
        return None
    return doc_from_pages(title or Path(str(url).split("?")[0]).name, url, pages)


def append_snapshot(task_id: str, doc: dict, *, project=None) -> bool:
    """把 PDF 文档并入 `fetch_snapshot.json`（去重，幂等），供证据提取读取。"""
    if not doc or not str(doc.get("text") or "").strip():
        return False
    try:
        import workspace
        proj = workspace.task_project_dir(task_id, project) if project \
            else workspace.task_project_dir(task_id)
        snap = Path(proj) / "fetch_snapshot.json"
        items: list[dict] = []
        if snap.exists():
            try:
                items = json.loads(snap.read_text(encoding="utf-8")) or []
            except Exception:
                items = []
        url = str(doc.get("url") or "")
        if url and any(str(i.get("url") or "") == url for i in items):
            return False
        items.append(doc)
        snap.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        return True
    except Exception as exc:                     # noqa: BLE001 - 落盘失败只记日志
        logger.warning("PDF 快照写入失败（task=%s）：%s", task_id, str(exc)[:120])
        return False


def url_from_instruction(instruction: str) -> str:
    """从步骤指令里取第一个 `[URL: ...]`（抓取步骤的目标就是它）。"""
    m = re.search(r"\[URL:\s*(https?://[^\s\]]+)\]", str(instruction or ""))
    if m:
        return m.group(1)
    m = re.search(r"https?://\S+", str(instruction or ""))
    return m.group(0) if m else ""
