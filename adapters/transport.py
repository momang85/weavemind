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
import json as _json
import logging
import socket
import ssl
import threading as _threading
import time as _time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

# ── D4：数据合规降险 ─────────────────────────────────────────────
# 1) 抓取限频：同一 host 两次请求的最小间隔（秒）。
_MIN_INTERVAL = float(__import__("os").environ.get("SOURCE_MIN_INTERVAL", "3.0") or 3.0)
_throttle_lock = _threading.Lock()
_last_hit: dict[str, float] = {}


def _is_private_addr(addr: str) -> bool:
    """IP 字面量是否为环回/私网/链路本地/未指定/运营商级 NAT。"""
    import ipaddress as _ip
    try:
        ip = _ip.ip_address(addr)
    except ValueError:
        return False
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_unspecified
        or (isinstance(ip, _ip.IPv6Address) and ip.is_site_local)
    )


def _validate_public_url(url: str) -> bool:
    """SSRF 防护（内容派生 URL）：仅放行 http/https 公网目标。

    实现收敛到 `net_policy.validate_public_url`（共享网络策略），修掉旧实现的三处不足：
    1. **DNS 解析失败不再放行**（旧实现"解析失败也放行"，等于把失败当安全）；
    2. 补上共享地址段 `100.64.0.0/10` 等非全局可路由地址；
    3. IPv4/IPv6 映射地址（如 `::ffff:127.0.0.1`）按内部地址判定，并拒绝 URL 用户凭据。

    注意：校验与真正发起连接之间存在 DNS 重绑定竞态；需要强保证时走
    `net_policy.fetch_document`（用已验 IP 连接）。
    """
    try:
        from net_policy import validate_public_url
        return bool(validate_public_url(url).ok)
    except Exception:
        return False


def _load_official_map() -> dict:
    """官方数据源切换位：环境变量 WM_OFFICIAL_SOURCES 为 JSON 映射
    {"原URL前缀": "官方URL前缀"}，命中即重写；重写目标必须通过 SSRF 校验。"""
    try:
        raw = _json.loads(
            __import__("os").environ.get("WM_OFFICIAL_SOURCES", "") or "{}"
        )
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in raw.items()
            if _validate_public_url(str(v))
        }
    except Exception:
        return {}


def _throttle_and_rewrite(url: str) -> str:
    """限频等待 + 官方源前缀重写。"""
    global _last_hit
    rewritten = url
    for prefix, official in _load_official_map().items():
        if url.startswith(prefix):
            candidate = official + url[len(prefix):]
            if _validate_public_url(candidate):
                rewritten = candidate
            break
    host = urllib.parse.urlsplit(rewritten).hostname or ""
    if _MIN_INTERVAL > 0 and host:
        with _throttle_lock:
            prev = _last_hit.get(host)
            now = _time.time()
            if prev is not None and now - prev < _MIN_INTERVAL:
                _time.sleep(_MIN_INTERVAL - (now - prev))
            _last_hit[host] = _time.time()
    return rewritten

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
    url = _throttle_and_rewrite(url)
    # SSRF 防护：校验紧邻请求点（协议/主机/IP 边界）
    if not _validate_public_url(url):
        raise RuntimeError(f"blocked URL by SSRF guard: {url[:120]}")
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
    url = _throttle_and_rewrite(url)
    # SSRF 防护：校验紧邻请求点（协议/主机/IP 边界）
    if not _validate_public_url(url):
        raise RuntimeError(f"blocked URL by SSRF guard: {url[:120]}")
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
