"""织光 (ZhiGuang) - Web Fetch Worker：抓取 URL 并提取正文文本（纯标准库）。"""

import asyncio
import json
import logging
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit, urlunsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_paths
from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

logger = logging.getLogger(__name__)


def _encode_iri(url: str) -> str:
    """把含中文等非 ASCII 字符的 IRI 编码为合法 URL（百分号编码）。

    搜索类指令中的 URL 常带未编码中文（query/path），urllib 直接构造
    Request 会抛 'ascii' codec can't encode。只编码非 ASCII 字节，
    ASCII 保留字符（:/?#[]@!$&'()*+,;=）不动。
    """
    try:
        url.encode("ascii")
        return url  # 纯 ASCII 无需处理
    except UnicodeEncodeError:
        pass
    try:
        parts = urlsplit(url)
        safe = "/%:@&=+$,;~*'()!-._"
        path = quote(parts.path, safe=safe)
        query = quote(parts.query, safe=safe + "?")
        return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
    except Exception:
        return url


def _clean_url(url: str) -> str:
    """抓取用的 URL 清洗：**在第一个中日韩字符处截断**，再去掉尾部标点。

    实机（`ui-17947f055b`）：指令里是「…1223370519.PDF；本次…」，中文尾句紧跟 URL、
    中间**没有空格**，`https?://\\S+` 会把整段中文都吃进去；urllib 编码后变成一条不存在的
    路径 → 404。这里按 CJK 边界切开，只保留真正的 URL 部分。
    """
    s = str(url or "").strip()
    if not s:
        return ""
    m = re.search(r"[\u2e80-\u9fff\uff00-\uffef\u3000-\u303f]", s)
    if m:
        s = s[:m.start()]
    return re.sub(r"[)\]>,.;:!?'\"】）》」]+$", "", s).strip()


def _explicit_url(task: dict | None, instruction: str) -> str:
    """**显式选定**的 URL：优先 payload 字段，其次指令里的 `[URL: …]` 标记（第一个）。

    为什么要有它：派发步骤已经按角色选好了目标页（`[URL: …]`），worker 再"从整条指令里
    找第一个 URL"就会把目标文本里的参考直链/中文尾句当成抓取目标——显式字段/标记才是
    选定值，其余 URL 一律不参与选择。
    """
    t = task if isinstance(task, dict) else {}
    for key in ("selected_url", "url", "fetch_url", "source_url"):
        v = t.get(key)
        if isinstance(v, str) and v.strip().startswith("http"):
            return _clean_url(v)
    for m in re.finditer(r"\[URL:\s*([^\]]+)\]", str(instruction or ""), re.I):
        cand = _clean_url(m.group(1))
        if cand.startswith("http"):
            return cand
    return ""


def _store_bytes(task: dict | None, raw: bytes, digest: str) -> dict:
    """把抓到的原始字节存成**可复用工件**，返回工件引用（含 hash 与大小）。

    存到任务工作区的 `project/fetched/<sha16>.<ext>`：解析通道按这个引用读同一份
    字节，不再按 URL 重抓（重抓的字节可能与已取证的不同，且多一次对外请求）。
    拿不到工作区路径（旧派发/单测）时返回空 dict——调用方按"无工件"处理，
    不静默丢字节，也不冒充"已保存"。
    """
    try:
        import hashlib
        from pathlib import Path
        ws = str((task or {}).get("workspace") or "")
        if not ws:
            return {}
        base = Path(ws) / "project" / "fetched"
        base.mkdir(parents=True, exist_ok=True)
        name = f"{digest[:16]}.pdf"
        path = base / name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            path.write_bytes(raw)
        rel = f"project/fetched/{name}"
        return {"path": rel, "abs_path": str(path), "sha256": digest,
                "bytes": len(raw)}
    except Exception as exc:                     # noqa: BLE001 - 存字节失败只记日志
        logger.warning("PDF 原始字节落工件失败：%s", str(exc)[:120])
        return {}


class _TextExtractor(HTMLParser):
    """提取网页正文文本（剔除 script/style/nav 等噪音）。"""

    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "iframe", "svg", "nav", "footer"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "iframe", "svg", "nav", "footer") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


class WebFetchWorker(AsyncWorkerBase):
    _class_capabilities = ["web_fetch"]
    _needs_task = True          # 需要任务载荷（PDF 原始字节要落到任务工作区工件目录）

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        # X0（10-02 实测）：**优先消费显式选定的 URL**。派发步骤会写 `[URL: …]` 标记，
        # 而目标文本里往往还带着"参考 PDF 直链 + 中文尾句"——旧实现取整条指令里**第一个**
        # URL，于是把中文「；本次…」一起编码进路径，抓到 404（还重复抓了两次）。
        url = _explicit_url(task, instruction)
        if not url:
            urls = [_clean_url(u) for u in
                    re.findall(r'https?://[^\s<>"\']+', instruction)]
            urls = [u for u in urls if u]
            url = urls[0] if urls else ""
        if not url:
            return json.dumps({"status": "failed", "error": "No URL found in instruction"}, ensure_ascii=False)
        # 批次3-2：抓取走**现有文档接入契约**（`net_policy.fetch_document`）——
        # 协议/主机/解析后 IP 边界校验、**连接使用已验 IP**（防 DNS rebinding）、
        # 不跟随重定向、字节上限与审计都在那一层；本 worker 不再自己发裸请求。
        # SSRF 防护（环回/私网/链路本地拒绝）因此与搜索抓取同源，不留两套。
        try:
            import net_policy
            import transfer_limits
            from annual_report_pdf import looks_like_pdf
            resp = net_policy.fetch_document(_encode_iri(url),
                                             timeout=transfer_limits.timeout_of(
                                                 "download" if looks_like_pdf(url) else "text"),
                                             max_bytes=net_policy.MAX_BODY_BYTES)
        except Exception as exc:                 # noqa: BLE001 - 策略拒绝/抓取失败都如实报
            return json.dumps({"status": "failed", "error": str(exc)[:300]},
                              ensure_ascii=False)
        raw = bytes(resp.get("raw") or b"")
        status = int(resp.get("status") or 0)
        # X0（10-02 实测）：**非 2xx 是缺口，不是成功**。旧实现不看 HTTP 状态，404 的
        # 正文（"页面不存在"）照样包装成 `status: success`，于是"抓到了"进快照、
        # 还触发第二次无效重抓。这里如实判失败并把状态码带出来（下游按缺口处理）。
        if status and not (200 <= status < 300):
            return json.dumps({
                "status": "failed",
                "url": url,
                "http_status": status,
                "error": f"HTTP {status}：该地址不可用（按缺口处理，不当作抓取成功）",
                "note": "非 2xx 响应不作为材料；如需该期间材料请换用已准入原件或别的候选",
            }, ensure_ascii=False)
        ctype = str((resp.get("headers") or {}).get("content-type") or "")
        # **PDF 不当文本**：此前把字节按 UTF-8 解码成乱码再截 30000 字符，证据层拿到的是
        # "有正文"的假象却提不出任何小节（实机 ui-750185076a）。这里按 MIME/魔数识别，
        # 保留字节 hash 与状态，正文交给现有解析通道（`annual_report_pdf` 走同一策略层
        # 取字节 + 页码定位），不以截断字节冒充正文。
        if (b"%PDF-" in raw[:1024]) or ("application/pdf" in ctype.lower()):
            import hashlib
            digest = hashlib.sha256(raw).hexdigest()
            artifact = _store_bytes(task, raw, digest)
            return json.dumps({
                "status": "success",
                "url": url,
                "title": "",
                "text": "",
                "pdf": True,
                "content_type": ctype,
                "content_bytes": len(raw),
                "content_hash": digest,
                # 工件引用：解析通道按它读**同一份字节**（不再按 URL 重抓一次）
                "artifact": artifact,
                "note": "PDF 材料：正文需经解析通道提取（不以截断字节冒充正文）",
            }, ensure_ascii=False)
        html = raw.decode("utf-8", errors="replace")
        parser = _TextExtractor()
        parser.feed(html)
        text = "\n".join(line for line in parser.text().splitlines() if line.strip())[:30000]
        title = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        return json.dumps({
            "status": "success",
            "url": url,
            "title": title.group(1).strip() if title else "",
            "text": text,
        }, ensure_ascii=False)


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-web-fetch")
    registry = AsyncRegistry(db_paths.resolve_db_path())
    messaging = AsyncMessaging(
        os.environ.get("REDIS_HOST", "localhost"),
        int(os.environ.get("REDIS_PORT", "6379")),
    )
    worker = WebFetchWorker(
        agent_id="webfetchworker",
        capabilities=WebFetchWorker._class_capabilities,
        registry=registry,
        messaging=messaging,
        max_concurrency=5,
    )
    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.shutdown()


def main():
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
