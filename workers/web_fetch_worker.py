"""织光 (ZhiGuang) - Web Fetch Worker：抓取 URL 并提取正文文本（纯标准库）。"""

import asyncio
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging


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
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; WeaveMind/1.0)"},
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
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
        except Exception as exc:
            return json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False)


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-web-fetch")
    registry = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
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
