# -*- coding: utf-8 -*-
"""数据源适配器共享传输层（F 重构：消除各源重复的双通道/重试/数值样板）。

统一提供：
- dual_channel_get：urllib（浏览器头）→ raw socket HTTP/1.0 双通道降级，
  带来源名与错误类型参数化（各源只传自己的错误类与编码/头）；
- to_num：原始值 → 数值归一化（'-' 等缺失标记返回 None）。

出口纪律（专项 §6，S2）：urllib 通道吃部署环境的代理设置，raw socket 通道用**已验 IP**
直连；**代理层失败不得降级为直连**（那等于用户以为走代理、实际从本机直出），统一以
`proxy_error` 归类交回调用方。显式直连（`WM_CONTENT_FETCH_MODE=direct`）时 urllib
通道清空代理表，不再有"代理失败"可言；通道选择只看配置，不看哪条路先失败。

原 ashare_ranking/sina_ranking/tencent_quotes 各自手写的 _get/_get_via_*/
_num 全部收敛到此；新增数据源只需 import 本模块，不再复制传输样板。
"""

import http.client
import json as _json
import logging
import threading as _threading
import time as _time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


class ProxyEgressError(RuntimeError):
    """代理层失败：**不得**降级为直连（专项 §6"不能由异常分支擅自从代理切直连"）。"""

    category = "proxy_error"


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
    _require_egress_ok()
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

    连接走 `net_policy.connect_validated`：**连到校验时解析出的那个 IP**（不再用域名
    二次解析，消除"校验后域名被改指内网"的窗口），TLS 用系统 CA 与正确 SNI，3xx 不跟随。
    """
    url = _throttle_and_rewrite(url)
    from net_policy import connect_validated, validate_public_url
    _require_egress_ok()
    decision = validate_public_url(url)
    if not decision.ok:
        raise RuntimeError(f"blocked URL by SSRF guard: {decision.reason}")
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    request_lines = [f"GET {path} HTTP/1.0", f"Host: {host}"]
    request_lines.extend(f"{k}: {v}" for k, v in (headers or BROWSER_HEADERS).items())
    request = "\r\n".join(request_lines) + "\r\n\r\n"
    chunks: list = []
    last_error = ""
    for ip in decision.resolved:
        sock = None
        chunks = []
        try:
            sock = connect_validated(ip, port, host, parsed.scheme, timeout)
            sock.settimeout(timeout)
            sock.sendall(request.encode("utf-8"))
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        break
    if not chunks:
        raise RuntimeError(f"socket HTTP/1.0 连接失败：{last_error or '无可用地址'}")
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

    先走 urllib（浏览器头 + Connection: close）；连接层失败时降级 raw socket HTTP/1.0
    （**连到已验 IP**）；两通道都失败抛异常并带原因。
    error_cls 存在时以其包装（构造签名 (channel, reason)），否则抛 RuntimeError。

    **代理失败是例外（专项 §6）**：代理层失败不再降级——降级等于"用户以为走代理、实际从
    本机直出"，改抛 `ProxyEgressError`（类别 `proxy_error`）交回调用方，由用户/管理员在
    已有配置入口处理（换代理、改 `WM_CONTENT_FETCH_MODE`），不在异常分支里替用户改出口。
    """
    _require_egress_ok()
    proxy_in_effect = _proxy_in_effect()
    try:
        return get_via_urllib(url, timeout=timeout, encoding=encoding, headers=headers)
    except ProxyEgressError:
        raise
    except (urllib.error.URLError, ConnectionError, http.client.HTTPException) as exc:
        urllib_reason = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "%s fetch attempt %d failed: urllib: %s",
            source, attempt, urllib_reason,
        )
        # 配置了代理（inherit）时，**任何** urllib 失败都不降级直连——包括"已建连后断开"
        # （`RemoteDisconnected`）：那时代码本应经代理出去，改走直连等于换出口（指令 §4-C0.3）。
        # 只有代理不参与（direct / 未配代理）时才允许用第二条通道。
        if proxy_in_effect or _is_proxy_failure(exc):
            logger.warning("%s 代理在生效或代理层失败：不降级直连（指令 §4-C0.3）", source)
            raise ProxyEgressError(
                f"代理在生效，未降级直连：{urllib_reason}") from exc
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
            err = error_cls("urllib+socket", reason)
            _attach_category(err, exc)
            raise err from exc
        raise RuntimeError(f"{source} fetch failed: {reason}") from exc


def _require_egress_ok() -> None:
    """内容入口的出口守卫：`proxy_required` 且不支持经代理 → 抛错（发请求之前）。"""
    try:
        from net_policy import require_egress_ok
        require_egress_ok()
    except ImportError:
        return
    except Exception:
        raise


def _proxy_in_effect() -> bool:
    """当前内容请求是否经由代理（inherit + 环境里配了代理）。"""
    try:
        from net_policy import connection_mode, proxy_settings
        return connection_mode() != "direct" and bool(proxy_settings()["configured"])
    except Exception:
        return False


def _is_proxy_failure(exc: BaseException) -> bool:
    """该失败是否发生在代理层（判定规则见 `net_policy.is_proxy_failure`）。"""
    try:
        from net_policy import is_proxy_failure
        return bool(is_proxy_failure(exc))
    except Exception:
        return False


def _attach_category(err: BaseException, cause: BaseException) -> None:
    """给源适配器自己的错误对象附上统一诊断类别（专项 §6：各源输出同一类别表）。"""
    try:
        from net_policy import classify_network_error
        err.category = classify_network_error(cause)
    except Exception:
        pass


def classify_error(exc: BaseException) -> str:
    """传输层失败 → 统一诊断类别（供适配器把类别带进日志/健康快照）。"""
    try:
        from net_policy import classify_network_error
        return classify_network_error(exc)
    except Exception:
        return getattr(exc, "category", "") or type(exc).__name__


def to_num(raw, scale: float = 1.0) -> float | None:
    """原始值 → 数值；'-' / 空串等缺失标记返回 None（非数值一律判缺失，不猜 0）。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v * scale, 2) if scale != 1.0 else v
