# -*- coding: utf-8 -*-
"""A股行情排行适配器（东方财富行情中心 clist 接口）。

接口（已勘探验证）：
    GET https://push2.eastmoney.com/api/qt/clist/get
        ?pn=1&pz=10&po=1&np=1&fltt=2&invt=2
        &fid=f6&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23
        &fields=f2,f3,f5,f6,f8,f12,f14,f20
    - fid=f6 按成交额降序；fid=f5 按成交量降序；
    - fs 覆盖沪深京 A 股；
    - f2 最新价 / f3 涨跌幅% / f5 成交量(手) / f6 成交额(元) /
      f8 换手率% / f12 代码 / f14 名称 / f20 总市值(元)。
无需 API Key，失败时由调用方回退通用搜索链路。
"""

import http.client
import json
import logging
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

_logger = logging.getLogger(__name__)

_BASE = "https://push2.eastmoney.com/api/qt/clist/get"
_FS_A_SHARE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
_FIELDS = "f2,f3,f5,f6,f8,f12,f14,f20"

# 完整浏览器头：动态反爬对 urllib 默认握手不友好，先伪装浏览器请求一次。
_BROWSER_HEADERS = {
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

# 初始 1 次 + 重试 3 次，共 4 次尝试；退避 2/4/8 秒（基数可用环境变量覆盖）。
_MAX_ATTEMPTS = 4
_RETRY_BASE_DEFAULT = 2.0


class EastMoneyFetchError(RuntimeError):
    """双通道均失败：携带通道与原因，便于上层按 <通道>: <原因> 记录。"""

    def __init__(self, channel: str, reason: str):
        super().__init__(f"{channel}: {reason}")
        self.channel = channel
        self.reason = reason


def _retry_base() -> float:
    """指数退避基数：默认 2，可用 EASTMONEY_RETRY_BASE 覆盖（非法值回退默认）。"""
    try:
        return max(0.1, float(os.environ.get("EASTMONEY_RETRY_BASE", "2")))
    except (TypeError, ValueError):
        return _RETRY_BASE_DEFAULT


def _get_via_urllib(url: str, timeout: int = 25) -> str:
    """通道 1：urllib + 完整浏览器头 + Connection: close。"""
    req = urllib.request.Request(url, headers=_BROWSER_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _get_via_socket(url: str, timeout: int = 25) -> str:
    """通道 2：raw socket + TLS，HTTP/1.0 请求，解析响应头与 body 直到连接关闭。"""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    request_lines = [f"GET {path} HTTP/1.0", f"Host: {host}"]
    request_lines.extend(f"{k}: {v}" for k, v in _BROWSER_HEADERS.items())
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
    return body.decode("utf-8", errors="replace")


def _get(url: str, timeout: int = 25, attempt: int = 1) -> str:
    """GET 文本：双通道防反爬。

    先走 urllib（浏览器头 + Connection: close）；RemoteDisconnected/URLError
    等连接层失败时降级 raw socket HTTP/1.0；两通道都失败抛异常并带原因。
    """
    try:
        return _get_via_urllib(url, timeout)
    except (urllib.error.URLError, ConnectionError, http.client.HTTPException) as exc:
        urllib_reason = f"{type(exc).__name__}: {exc}"
        _logger.warning(
            "eastmoney fetch attempt %d failed: urllib: %s",
            attempt, urllib_reason,
        )
    try:
        return _get_via_socket(url, timeout)
    except Exception as exc:
        socket_reason = f"{type(exc).__name__}: {exc}"
        _logger.warning(
            "eastmoney fetch attempt %d failed: socket: %s",
            attempt, socket_reason,
        )
        raise EastMoneyFetchError(
            "urllib+socket",
            f"urllib: {urllib_reason}; socket: {socket_reason}",
        ) from exc


def _api_url(metric: str = "amount", top_n: int = 10) -> str:
    """构造排行接口 URL：metric=volume 按成交量，其余按成交额。"""
    fid = "f5" if metric == "volume" else "f6"
    qs = urllib.parse.urlencode({
        "pn": 1,
        "pz": max(1, min(int(top_n), 50)),
        "po": 1,          # 降序
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": fid,
        "fs": _FS_A_SHARE,
        "fields": _FIELDS,
    })
    return f"{_BASE}?{qs}"


def _num(raw, scale: float = 1.0) -> float | None:
    """原始值 → 数值；'-' 等缺失标记返回 None。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v * scale, 2) if scale != 1.0 else v


def fetch_ranking(metric: str = "amount", top_n: int = 10) -> dict:
    """获取 A股 成交额/成交量排行前 N。

    Args:
        metric: "volume"（成交量）或 "amount"（成交额，默认）。
        top_n: 返回条数，默认 10。

    Returns:
        {rows, metric, top_n, source_url, retrieved_at}
        rows 元素：rank/code/name/price/change_pct/volume_hand/
        volume_wan_hand/amount_yuan/amount_yi/turnover_pct/market_cap_yi。
        失败抛异常（调用方回退搜索链路）。
    """
    url = _api_url(metric, top_n)
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            text = _get(url, timeout=25, attempt=attempt)
            break
        except EastMoneyFetchError as exc:
            # 两通道失败的具体原因已由 _get 按通道记录，这里只保留最终异常。
            last_error = exc
        except Exception as exc:
            _logger.warning(
                "eastmoney fetch attempt %d failed: urllib: %s",
                attempt, f"{type(exc).__name__}: {exc}",
            )
            last_error = exc
        if attempt < _MAX_ATTEMPTS:
            # 指数退避：2/4/8 秒（基数可被 EASTMONEY_RETRY_BASE 覆盖）。
            time.sleep(_retry_base() * (2 ** (attempt - 1)))
    else:
        raise last_error
    data = json.loads(text)
    rows_raw = ((data.get("data") or {}).get("diff")) or []
    if not rows_raw:
        raise RuntimeError("EastMoney 行情排行无数据")
    rows: list[dict] = []
    for i, r in enumerate(rows_raw[:top_n], 1):
        amount_yuan = _num(r.get("f6"))
        volume_hand = _num(r.get("f5"))
        rows.append({
            "rank": i,
            "code": str(r.get("f12") or ""),
            "name": str(r.get("f14") or ""),
            "price": _num(r.get("f2")),
            "change_pct": _num(r.get("f3")),
            "volume_hand": volume_hand,
            "volume_wan_hand": (
                round(volume_hand / 1e4, 2) if volume_hand is not None else None
            ),
            "amount_yuan": amount_yuan,
            "amount_yi": (
                round(amount_yuan / 1e8, 2) if amount_yuan is not None else None
            ),
            "turnover_pct": _num(r.get("f8")),
            "market_cap_yi": _num(r.get("f20"), 1e-8),
        })
    return {
        "rows": rows,
        "metric": metric,
        "top_n": len(rows),
        "source_url": url,
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    import sys
    metric = sys.argv[1] if len(sys.argv) > 1 else "amount"
    result = fetch_ranking(metric, 10)
    print(json.dumps(result, ensure_ascii=False, indent=1))
