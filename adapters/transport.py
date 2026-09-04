# -*- coding: utf-8 -*-
"""数据源适配器共享传输层（F 重构：消除各源重复的双通道/重试/数值样板）。

统一提供：
- dual_channel_get：urllib（浏览器头）→ raw socket HTTP/1.0 双通道降级，
  带来源名与错误类型参数化（各源只传自己的错误类与编码/头）；
- to_num：原始值 → 数值归一化（'-' 等缺失标记返回 None）。

原 ashare_ranking/sina_ranking/tencent_quotes 各自手写的 _get/_get_via_*/
_num 全部收敛到此；新增数据源只需 import 本模块，不再复制传输样板。
"""

import http.client
import logging
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# 完整浏览器头：动态反爬对 urllib 默认握手不友好，先伪装浏览器请求一次。
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Referer": "https://quote.eastmoney.com/",
    "Connection": "close",
}


def get_via_urllib(
    url: str,
    timeout: int = 25,
    encoding: str = "utf-8",
    headers: dict | None = None,
) -> str:
    """通道 1：urllib + 完整浏览器头 + Connection: close。

    GBK 响应（新浪/腾讯）传 encoding="gbk"。
    """
    req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode(encoding, errors="replace")


def get_via_socket(
    url: str,
    timeout: int = 25,
    encoding: str = "utf-8",
    headers: dict | None = None,
) -> str:
    """通道 2：raw socket + TLS，HTTP/1.0 请求，解析响应头与 body 直到连接关闭。

    与 get_via_urllib 语义一致；encoding 决定响应解码。
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    request_lines = [f"GET {path} HTTP/1.0", f"Host: {host}"]
    request_lines.extend(f"{k}: {v}" for k, v in (headers or BROWSER_HEADERS).items())
    request = "\r\n".join(request_lines) + "\r\n\r\n"
    with socket.create_connection((host, port), timeout=timeout) as raw:
        context = ssl.create_default_context()
        with context.wrap_socket(raw, server_hostname=host) as sock:
            sock.settimeout(timeout)
            sock.sendall(request.encode("utf-8"))
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    if not head:
        raise RuntimeError("socket HTTP/1.0 响应缺少响应头")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    try:
        status_code = int(status_line.split()[1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(f"socket HTTP/1.0 响应状态行异常：{status_line!r}") from exc
    if status_code != 200:
        raise RuntimeError(f"socket HTTP/1.0 返回 HTTP {status_code}")
    return body.decode(encoding, errors="replace")


def dual_channel_get(
    url: str,
    timeout: int = 25,
    encoding: str = "utf-8",
    headers: dict | None = None,
    attempt: int = 1,
    error_cls: type | None = None,
    source: str = "fetch",
) -> str:
    """GET 文本：双通道防反爬（各源共享的统一实现）。

    先走 urllib（浏览器头 + Connection: close）；RemoteDisconnected/URLError
    等连接层失败时降级 raw socket HTTP/1.0；两通道都失败抛异常并带原因。
    error_cls 存在时以其包装（构造签名 (channel, reason)），否则抛 RuntimeError。
    """
    try:
        return get_via_urllib(url, timeout=timeout, encoding=encoding, headers=headers)
    except (urllib.error.URLError, ConnectionError, http.client.HTTPException) as exc:
        urllib_reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "%s fetch attempt %d failed: urllib: %s",
            source, attempt, urllib_reason,
        )
    try:
        return get_via_socket(url, timeout=timeout, encoding=encoding, headers=headers)
    except Exception as exc:
        socket_reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "%s fetch attempt %d failed: socket: %s",
            source, attempt, socket_reason,
        )
        reason = f"urllib: {urllib_reason}; socket: {socket_reason}"
        if error_cls is not None:
            raise error_cls("urllib+socket", reason) from exc
        raise RuntimeError(f"{source} fetch failed: {reason}") from exc


def to_num(raw, scale: float = 1.0) -> float | None:
    """原始值 → 数值；'-' / 空串等缺失标记返回 None。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v * scale, 2) if scale != 1.0 else v
