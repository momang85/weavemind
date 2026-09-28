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
    pages, _kind = pages_text_ex(data)
    return pages


def pages_text_ex(data: bytes) -> tuple[list[str], str]:
    """同 `pages_text`，但**额外报告失败类别**（P1-e：拒收要分因）。

    返回 `(pages, kind)`：成功时 `kind=""`；失败时是
    `"encrypted"`（加密/受保护）/ `"corrupt"`（解析器报错，文件损坏）之一。

    为什么要分清：`EOF marker not found`／`invalid pdf header` 这类是**文件坏了或
    下载被截断**，与"真扫描件"是两回事——前者可以重下，后者重下也没用。
    此前两者共用一句"扫描件或受保护"，读者据此会去换来源，而真正该做的是重试。
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if bool(getattr(reader, "is_encrypted", False)):
            # 加密且空口令解不开：这是**受保护文档**，不是扫描件
            try:
                if reader.decrypt("") == 0:      # type: ignore[arg-type]
                    return [], "encrypted"
            except Exception:                    # noqa: BLE001 - 解不开即按受保护处理
                return [], "encrypted"
        out: list[str] = []
        for i, page in enumerate(reader.pages):
            if i >= MAX_PAGES:
                break
            try:
                out.append(str(page.extract_text() or ""))
            except Exception:
                out.append("")
        return out, ""
    except Exception as exc:                     # noqa: BLE001 - 解析失败按缺口处理
        logger.warning("PDF 解析失败：%s", str(exc)[:140])
        return [], "corrupt"


def classify_pdf_bytes(data: bytes | None, *, meta: dict | None = None) -> tuple[str, str]:
    """取回的字节属于哪一类 → `(kind, detail)`（P1-e：985 字节取件要能分因）。

    先看**响应层**（`meta`：HTTP 状态 / Content-Type / 字节上限），再看**字节层**
    （PDF 头 / `%%EOF` 尾），最后才轮到解析层——顺序就是排查顺序。类别表（互斥，
    判不出归 `unknown`，不假装成别的）：

    | kind | 含义 | 该做什么 |
    |---|---|---|
    | `http_error` | HTTP ≥ 400（403/404/429…） | 换来源；**别当扫描件** |
    | `http_redirect` | 3xx（不跟随重定向） | 取真实地址 |
    | `not_pdf` | 字节头不是 `%PDF-`（HTML 错误页/WAF 挑战/JSON） | **不是 PDF**：别当扫描件 |
    | `wrong_content_type` | Content-Type 明说不是 PDF | 换来源 |
    | `truncated` | 有 `%PDF-` 头但没有 `%%EOF` 尾 | 下载被截断 → **重下** |
    | `too_large` | 超过字节上限 | 换分批/换源 |
    | `pdf` | 有头有尾（内容是否可解析另判） | 交给 `pages_text_ex` |

    `detail` 写清判据（状态码/Content-Type/前若干字节/缺失的尾部标记），便于页面与日志
    直接引用。**只有**走到 `scanned_no_text`（解析成功但无文本层）才该建议 OCR。
    """
    m = dict(meta or {})
    _st = m.get("status")
    if isinstance(_st, int) and _st >= 400:
        return "http_error", (f"HTTP {_st}，响应体 {m.get('body_bytes') or 0} 字节"
                              f"（Content-Type={ (m.get('content_type') or '—')[:60] }）")
    if isinstance(_st, int) and 300 <= _st < 400:
        return "http_redirect", f"HTTP {_st}（不跟随重定向）"
    if m.get("over_limit") or m.get("error_kind") == "too_large":
        return "too_large", str(m.get("error") or "超过字节上限")
    if not data:
        return "empty", "0 字节"
    head = bytes(data[:8])
    _ctype = str(m.get("content_type") or "").lower()
    _url_says_pdf = True                          # 调用方已按 URL/魔数判定过"这该是 PDF"
    if head[:5] != b"%PDF-":
        _peek = bytes(data[:40])
        try:
            _txt = _peek.decode("utf-8", "replace").strip()
        except Exception:                        # noqa: BLE001
            _txt = repr(_peek)
        _low = _txt.lower()
        _ct_note = f"；Content-Type={_ctype[:40]}" if _ctype else ""
        if _low.startswith(("<!doctype", "<html", "<?xml", "<script")):
            return "not_pdf", (f"取回的是 HTML 页面（{len(data)} 字节{_ct_note}）："
                               f"{_txt[:40]!r}")
        if _low.startswith(("{", "[")):
            return "not_pdf", (f"取回的是 JSON（{len(data)} 字节{_ct_note}）：{_txt[:40]!r}")
        return "not_pdf", (f"字节头不是 %PDF-（{len(data)} 字节{_ct_note}）：{_peek!r}")
    tail = bytes(data[-2048:])
    if b"%%EOF" not in tail:
        return "truncated", f"有 %PDF- 头但尾部 2KB 内无 %%EOF（共 {len(data)} 字节）"
    if _url_says_pdf and _ctype and not any(
            k in _ctype for k in ("pdf", "octet-stream", "binary", "download")):
        # 字节头确实是 PDF，但服务端声明的类型不是 PDF/二进制——**如实标注**，
        # 不据此拒收（字节头比声明更可信），只把矛盾记进判据供人工核对。
        return "pdf", (f"{len(data)} 字节；注意 Content-Type={_ctype[:40]} 与字节头不符")
    return "pdf", f"{len(data)} 字节"


def doc_from_url(url: str, title: str = "", *, data: bytes | None = None,
                 on_reject=None) -> dict | None:
    """URL → 文档对象（下载 + 解析 + 页码偏移）；拿不到正文返回 None。

    `on_reject(kind, detail)`：可选回调，**在拒收时**收到类别与判据。调用方据此把
    "为什么没拿到"写进任务日志/页面——此前四种完全不同的原因（网络失败 / 取回的不是
    PDF / 文件损坏或被截断 / 真扫描件无文本层）共用一句"PDF 无可提取文本（扫描件或
    受保护）"，读者据此会去换来源，而真正该做的可能是重下或重试。
    不传回调时行为与以前一致（只记日志、返回 None）。
    """
    def _reject(kind: str, detail: str) -> None:
        logger.info("PDF 未取到正文（类别=%s）：%s —— %s", kind, detail, str(url)[:120])
        if callable(on_reject):
            try:
                on_reject(kind, detail)
            except Exception as exc:             # noqa: BLE001 - 回调异常不改失败处置
                logger.warning("PDF 拒收回调异常：%s", str(exc)[:120])

    _meta: dict = {}
    raw = data if data is not None else fetch_bytes(url, meta=_meta)
    if not raw:
        # 下载层失败：类别与响应元信息见上一条日志（HTTP 状态/Content-Type/字节上限）
        _kind = str(_meta.get("error_kind") or "fetch_failed")
        _detail = (f"下载未取到字节：{_kind}"
                   f"（HTTP {_meta.get('status')}，"
                   f"Content-Type={(str(_meta.get('content_type') or '—'))[:40]}，"
                   f"{_meta.get('body_bytes') or 0} 字节）")
        _reject(_kind if _kind != "fetch_failed" else "fetch_failed", _detail)
        return None
    kind, detail = classify_pdf_bytes(raw, meta=_meta)
    if kind != "pdf":
        _reject(kind, detail)
        return None
    pages, perr = pages_text_ex(raw)
    if perr:
        # **文件损坏 / 受保护**：与"扫描件"分开报（前者可重下，后者不行）
        _reject(perr, detail)
        return None
    if not pages or sum(len(p or "") for p in pages) < MIN_TEXT_CHARS:
        _reject("scanned_no_text",
                f"解析成功（{len(pages)} 页）但可提取文本 "
                f"{sum(len(p or '') for p in pages)} 字符 < {MIN_TEXT_CHARS}"
                f"（真扫描件/无文本层，重下无用）")
        return None
    return doc_from_pages(title or Path(str(url).split("?")[0]).name, url, pages)


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


def fetch_bytes(url: str, *, timeout: int = 30, max_bytes: int = MAX_BYTES,
                meta: dict | None = None) -> bytes | None:
    """下载 PDF 字节（复用**既有**公网校验抓取通道；超限或受限返回 None，不抛）。

    走 `adapters.transport.get_bytes_via_urllib`（协议/主机/IP 边界校验紧邻请求点，
    与文本通道**同一条**通道、同一套前置校验），取**二进制**并带回响应元信息——
    PDF 是二进制，按文本往返会丢信息；而"为什么没取到"必须能分清 HTTP 状态、
    Content-Type、字节上限与连接层失败（P1-e）。

    `meta`：可选 dict，回填 `status`/`content_type`/`transfer_encoding`/`body_bytes`/
    `error_kind`/`error`/`over_limit`——调用方据此把原因写进日志与步骤结果。
    """
    try:
        from adapters.transport import get_bytes_via_urllib
        res = get_bytes_via_urllib(url, timeout=timeout, max_bytes=max_bytes)
    except Exception as exc:                     # noqa: BLE001 - 通道不可用按缺口处理
        try:
            from net_policy import classify_network_error
            kind = classify_network_error(exc)
        except Exception:
            kind = type(exc).__name__
        if meta is not None:
            meta.update({"error_kind": str(kind), "error": str(exc)[:160],
                         "status": None, "content_type": "", "body_bytes": 0})
        logger.warning("PDF 下载失败（类别=%s）：%s：%s", kind, str(url)[:100],
                       str(exc)[:120])
        return None
    if meta is not None:
        meta.update(res)
    if not res.get("ok"):
        logger.warning("PDF 下载失败（类别=%s，HTTP %s，Content-Type=%s，%d 字节）：%s：%s",
                       res.get("error_kind") or "unknown", res.get("status"),
                       (res.get("content_type") or "—")[:40], res.get("body_bytes") or 0,
                       str(url)[:100], str(res.get("error") or "")[:120])
        return None
    return bytes(res.get("data") or b"")


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
