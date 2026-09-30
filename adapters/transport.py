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
import socket as _socket
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
# 正文/二进制默认字节上限（容量策略另见 transfer_limits）：
DEFAULT_TEXT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_BINARY_MAX_BYTES = 64 * 1024 * 1024

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


class _HeaderDeadline(RuntimeError):
    """响应头还没读完就用尽了总截止（看门狗已关闭连接）。"""


def _open_bounded(url: str, *, budget: float, deadline: float,
                  headers: dict | None = None):
    """用 `http.client` 取响应头，并让**总截止真正可终止**（R2-c，09-30 下午复核）。

    为什么不能继续用 `urlopen(timeout=budget)`：那只是**每次** socket 操作的超时。对端每
    8ms 发 4 个字节时，每次 recv 都"很快返回"，于是"逐字节慢响应头"能把一次 50ms 预算的
    取件拖到 **964ms**，只在"读完头再查钟"处被记为 `read_timeout`——那是**事后判定**，
    不是有界终止（复核要求：50ms 慢头必须在规定容差内实际返回/关闭连接）。

    做法：连接后把 `deadline` 剩余时间交给一个**看门狗线程**，到点直接关闭连接；阻塞中的
    `getresponse()` 立刻抛错返回（≈预算），我们按 `read_timeout` 如实上报。返回
    `(conn, resp)`；`conn` 由调用方负责关闭。
    """
    parts = urllib.parse.urlsplit(url)
    host = str(parts.hostname or "")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    cls = (http.client.HTTPSConnection if parts.scheme == "https"
           else http.client.HTTPConnection)
    conn = cls(host, port, timeout=max(0.001, float(budget)))
    fired = {"v": False}

    def _abort() -> None:
        fired["v"] = True
        # **先 shutdown 再 close**：Windows 上只 close 不会打断另一个线程里阻塞中的 recv
        # （实测：close 之后逐字节慢头照样读到对端发完，仍是 ≈0.96s）。shutdown(SHUT_RDWR)
        # 能让阻塞的读立刻返回（EOF/错误），这才是有界终止。
        try:
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(_socket.SHUT_RDWR)
                except Exception:                    # noqa: BLE001
                    pass
                try:
                    sock.close()
                except Exception:                    # noqa: BLE001
                    pass
        finally:
            try:
                conn.close()
            except Exception:                        # noqa: BLE001
                pass

    try:
        conn.request("GET", target, headers=dict(headers or BROWSER_HEADERS))
    except (_socket.timeout, TimeoutError) as exc:
        _abort()
        raise _HeaderDeadline(f"连接/请求阶段超时：{type(exc).__name__}") from exc
    _left = float(deadline) - _time.monotonic()
    if _left <= 0:
        _abort()
        raise _HeaderDeadline("总截止在发起请求后已用尽")
    killer = _threading.Timer(max(0.001, _left), _abort)
    killer.daemon = True
    killer.start()
    try:
        resp = conn.getresponse()
    except Exception as exc:                         # noqa: BLE001
        if fired["v"] or isinstance(exc, (_socket.timeout, TimeoutError)):
            _abort()
            raise _HeaderDeadline(
                f"响应头读取超时（总截止 {float(budget):g}s 已用尽）："
                f"{type(exc).__name__}") from exc
        _abort()
        raise
    finally:
        killer.cancel()
    if fired["v"]:
        try:
            resp.close()
        except Exception:                            # noqa: BLE001
            pass
        _abort()
        raise _HeaderDeadline(f"响应头读取超时（总截止 {float(budget):g}s 已用尽）")
    # 连接交给响应关闭：`with resp:` 与 `_close_quietly(resp)` 都会走到这里，
    # 调用方不必各自记得再关一次连接（漏关会把 socket 留到 GC）。
    _orig_close = resp.close

    def _close_all() -> None:
        try:
            _orig_close()
        finally:
            try:
                conn.close()
            except Exception:                        # noqa: BLE001
                pass

    resp.close = _close_all                          # type: ignore[method-assign]
    return conn, resp


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
    # 总截止：`urlopen(timeout)` 只管**单次** socket 操作，慢速分块响应可以整体远超它。
    # L0-d（2026-09-30 复核 S1）：截止从**进入取件前**起算，并在响应头读完后再查一次钟——
    # 逐字节慢头（每次都短于 socket 超时）能把"读头"拖到远超总预算，此后若重新起算，
    # 一次 0.05s 的取件会在 0.5s 后照样返回成功。
    from adapters.search_runner import read_with_deadline
    _budget = max(0.5, float(timeout))
    deadline = _time.monotonic() + _budget
    # R2-c：头阶段也走**同一个总截止**（看门狗到点关连接），不再"读完头再查钟"
    try:
        conn, resp = _open_bounded(url, budget=_budget, deadline=deadline, headers=headers)
    except _HeaderDeadline as exc:
        raise TimeoutError("read deadline exhausted while reading response headers"
                           f"（响应头读取超时：{exc}）：不读响应体") from exc
    try:
        return read_with_deadline(resp, deadline,
                                  max_bytes=DEFAULT_TEXT_MAX_BYTES).decode(
                                      encoding, errors="replace")
    finally:
        try:
            resp.close()
        except Exception:                            # noqa: BLE001
            pass
        try:
            conn.close()
        except Exception:                            # noqa: BLE001
            pass


def _read_bounded_bytes(resp, *, deadline: float, limit: int,
                        chunk: int = 65536) -> tuple[bytes, bool, str]:
    """字节通道的**有界读取** → `(字节, 是否超限, 错误类别)`。

    与文本通道（`search_runner.read_with_deadline`）同一套边界：单次读用 `read1`、
    每次读之前把剩余时间设到响应自己的 socket 上、读之后再查钟。区别只在这里**保留
    字节通道既有的契约**——读满 `limit + 1` 就停并标 `over_limit`（供 PDF 拒收分因用），
    而不是抛 `ResponseTooLarge`。

    K0-c（2026-09-29 夜验收）：此前这里直接 `resp.read(n)`，时间边界只有 socket 超时——
    "慢滴"响应可以让一次 `read` 连续 recv 到对端结束，总截止形同虚设。
    """
    import socket as _socket

    from adapters.search_runner import _bound_single_read, _close_quietly, _has_read1

    buf: list[bytes] = []
    total = 0
    use_read1 = _has_read1(resp)
    bounded_sock = False
    while True:
        remain = float(deadline) - _time.monotonic()
        if remain <= 0:
            _close_quietly(resp)
            return b"".join(buf), False, "read_timeout"
        if _bound_single_read(resp, remain):
            bounded_sock = True
        if not (use_read1 or bounded_sock):
            if limit:
                # 两条边界都给不了、但**调用方给了字节上限**：退回"一次受限读取"——
                # 这是 urllib 的 `HTTPError` 包装对象的形状（错误页正文要有诊断价值，
                # "985 字节的 403 HTML" 是常见读数）。它仍是**体积有界**的；只是这类
                # 对象没有可设超时的 socket，时间边界只能靠上层。
                try:
                    raw = resp.read(limit + 1)
                except Exception as exc:             # noqa: BLE001
                    return b"", False, f"{type(exc).__name__}"
                raw = bytes(raw or b"")
                return raw[:limit], len(raw) > limit, ""
            _close_quietly(resp)
            return b"".join(buf), False, "unbounded_read_refused"
        want = int(chunk)
        if limit:
            want = max(1, min(want, limit + 1 - total))
        try:
            block = resp.read1(want) if use_read1 else resp.read(want)
        except (_socket.timeout, TimeoutError):
            _close_quietly(resp)
            return b"".join(buf), False, "read_timeout"
        except Exception as exc:                     # noqa: BLE001 - 读一半断开
            _close_quietly(resp)
            return b"".join(buf), False, f"{type(exc).__name__}"
        if not block:
            return b"".join(buf), False, ""
        buf.append(block)
        total += len(block)
        if limit and total > limit:
            _close_quietly(resp)
            return b"".join(buf), True, ""


def get_bytes_via_urllib(
    url: str,
    *,
    timeout: int = 25,
    max_bytes: int | None = None,
    headers: dict | None = None,
) -> dict:
    """通道 1 的**二进制 + 元信息**变体（P1-e）。

    与 `get_via_urllib` 走**同一条**通道、同一套前置校验（`_throttle_and_rewrite` +
    `_require_egress_ok` + `_validate_public_url`），差别只有两点：
    1. **不解码成文本**——PDF 是二进制，按 latin-1↔utf-8 往返会丢信息；
    2. **把响应元信息带出来**（状态码 / Content-Type / Transfer-Encoding / 实际字节数），
       因为"没取到内容"必须能分清是 **HTTP 状态**（403/404/302）还是**类型**
       （text/html）还是**字节上限**，而不是一律归成"解析失败"。

    返回（**不抛**，由调用方按 kind 处置）::

        {"ok", "status", "content_type", "transfer_encoding", "body_bytes",
         "data", "over_limit", "error_kind", "error"}

    - `max_bytes` 给定时**按上限截断读取**（多读 1 字节以判超限），不把超大响应整个读进内存；
    - 非 2xx **不抛异常**：错误页正文照样取回（"985 字节取回"往往就是一张 403/302 的
      HTML 页），状态码单独报出。3xx 不跟随——与 raw socket 通道语义一致（防重定向到别处）；
    - chunked 由 `http.client` 透明解块；是否 chunked 记在 `transfer_encoding` 里备查。
    """
    out: dict = {"ok": False, "status": None, "content_type": "",
                 "transfer_encoding": "", "body_bytes": 0, "data": b"",
                 "over_limit": False, "error_kind": "", "error": ""}
    _limited = int(max_bytes) if max_bytes else 0
    try:
        url = _throttle_and_rewrite(url)
        _require_egress_ok()
        if not _validate_public_url(url):
            out.update({"error_kind": "ssrf_blocked",
                        "error": f"blocked URL by SSRF guard: {url[:120]}"})
            return out
        req = urllib.request.Request(url, headers=headers or BROWSER_HEADERS)
        # **总截止从进入取件开始**（L0-d，2026-09-30 复核 S1）：此前截止在 `urlopen` **之后**
        # 才起算——"逐字节慢响应头"（每次 recv 都短于 socket 超时，整体却远超预算）能让
        # `timeout=0.05` 的取件在 0.511s 后照样 `ok=True`。
        # R2-c（09-30 下午复核）：头阶段改用 `_open_bounded`——**看门狗到点关连接**，
        # 于是"50ms 预算 + 逐字节慢头"在预算量级内就终止（修前实耗 ≈964ms，是事后判定）。
        _budget = float(timeout or 0 or 25)
        _deadline = _time.monotonic() + _budget
        try:
            conn, resp = _open_bounded(url, budget=_budget, deadline=_deadline,
                                       headers=req.headers)
        except _HeaderDeadline as exc:
            out.update({"error_kind": "read_timeout", "body_bytes": 0, "data": b"",
                        "error": f"读取超时（{exc}）：不再读取响应体"})
            return out
        with resp:
            status = int(getattr(resp, "status", None) or getattr(resp, "code", 0) or 0)
            hdrs = getattr(resp, "headers", None)
            ctype = str(hdrs.get("Content-Type") or "") if hdrs else ""
            tenc = str(hdrs.get("Transfer-Encoding") or "") if hdrs else ""
            # 响应头阶段就可能已经把预算用光：如实报超时，**不读体**、更不返回成功
            _left = _deadline - _time.monotonic()
            if _left <= 0:
                from adapters.search_runner import _close_quietly
                _close_quietly(resp)
                out.update({"status": status, "content_type": ctype,
                            "transfer_encoding": tenc, "error_kind": "read_timeout",
                            "body_bytes": 0, "data": b"", "over_limit": False,
                            "error": (f"读取超时（总截止 {_budget:g}s 在响应头阶段已用尽）："
                                      "不再读取响应体")})
                return out
            # 字节通道保留既有契约（**截断到上限 + over_limit 标记**，供 PDF 拒收分因用），
            # **时间**边界改用与文本通道同源的"总截止 + 单次读"（K0-c）：此前只有 socket
            # 超时，慢滴响应能把一次 read 拖到对端结束。
            # 传 `_limited`（函数内部按"读满 limit + 1 即停"处理），保持截断语义
            read_n = _limited if _limited else 0
            raw, _over, _rerr = _read_bounded_bytes(resp, deadline=_deadline, limit=read_n)
            if _rerr:
                out.update({"status": status, "content_type": ctype,
                            "transfer_encoding": tenc, "error_kind": _rerr,
                            "body_bytes": len(raw or b""), "data": bytes(raw or b""),
                            "error": (f"读取超时（总截止 {_budget:g}s）"
                                      if _rerr == "read_timeout"
                                      else f"{_rerr}: 读取中断"),
                            "over_limit": bool(_over)})
                return out
        raw = bytes(raw or b"")
        over = bool(_limited and len(raw) > _limited)
        if over:
            raw = raw[:_limited]
        out.update({"status": status, "content_type": ctype, "transfer_encoding": tenc,
                    "body_bytes": len(raw), "data": raw, "over_limit": over})
        if status and status >= 400:
            out.update({"error_kind": "http_error", "error": f"HTTP {status}"})
            return out
        if status and 300 <= status < 400:
            out.update({"error_kind": "http_redirect",
                        "error": f"HTTP {status}（不跟随重定向）"})
            return out
        if over:
            out.update({"error_kind": "too_large",
                        "error": f"响应超过字节上限 {_limited}"})
            return out
        out["ok"] = True
        return out
    except Exception as exc:                     # noqa: BLE001 - 连接层失败按类别报出
        try:
            from net_policy import classify_network_error
            _kind = classify_network_error(exc)
        except Exception:
            _kind = type(exc).__name__
        out.update({"error_kind": _kind, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
        return out


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
