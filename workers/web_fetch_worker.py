"""织光 (ZhiGuang) - Web Fetch Worker：抓取 URL 并提取正文文本（纯标准库）。"""

import asyncio
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit, urlunsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_paths
from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging


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

    async def execute(self, instruction: str) -> str:
        urls = re.findall(r'https?://[^\s<>"\']+', instruction)
        urls = [re.sub(r"[),.;\]}>]+$", "", u) for u in urls]
        if not urls:
            return json.dumps({"status": "failed", "error": "No URL found in instruction"}, ensure_ascii=False)
        url = urls[0]
        # 批次3-2：抓取走**现有文档接入契约**（`net_policy.fetch_document`）——
        # 协议/主机/解析后 IP 边界校验、**连接使用已验 IP**（防 DNS rebinding）、
        # 不跟随重定向、字节上限与审计都在那一层；本 worker 不再自己发裸请求。
        # SSRF 防护（环回/私网/链路本地拒绝）因此与搜索抓取同源，不留两套。
        try:
            import net_policy
            resp = net_policy.fetch_document(_encode_iri(url), timeout=30,
                                             max_bytes=net_policy.MAX_BODY_BYTES)
        except Exception as exc:                 # noqa: BLE001 - 策略拒绝/抓取失败都如实报
            return json.dumps({"status": "failed", "error": str(exc)[:300]},
                              ensure_ascii=False)
        raw = bytes(resp.get("raw") or b"")
        ctype = str((resp.get("headers") or {}).get("content-type") or "")
        # **PDF 不当文本**：此前把字节按 UTF-8 解码成乱码再截 30000 字符，证据层拿到的是
        # "有正文"的假象却提不出任何小节（实机 ui-750185076a）。这里按 MIME/魔数识别，
        # 保留字节 hash 与状态，正文交给现有解析通道（`annual_report_pdf` 走同一策略层
        # 取字节 + 页码定位），不以截断字节冒充正文。
        if (b"%PDF-" in raw[:1024]) or ("application/pdf" in ctype.lower()):
            import hashlib
            return json.dumps({
                "status": "success",
                "url": url,
                "title": "",
                "text": "",
                "pdf": True,
                "content_type": ctype,
                "content_bytes": len(raw),
                "content_hash": hashlib.sha256(raw).hexdigest(),
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
