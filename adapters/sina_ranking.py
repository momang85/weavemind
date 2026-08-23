# -*- coding: utf-8 -*-
"""新浪行情中心排行适配器（Market_Center.getHQNodeData）。

接口（已实测验证，免费无 key，GBK 编码）：
    GET https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/
        Market_Center.getHQNodeData?page=N&num=50&sort=amount&asc=0&node=hs_a&symbol=
    - sort=amount 按成交额降序；sort=volume 按成交量降序；asc=0 降序；
    - node=hs_a 覆盖沪深京 A 股；node=us 覆盖美股；
    - 返回体为"未加引号的 JSON"（键名裸写），需先补齐引号再 json.loads；
    - 响应字段：symbol/code/name/trade/changepercent/volume/amount/
      turnoverratio/mktcap 等（volume 单位为股，amount 单位为元）。

本适配器支持任意 top_n（10 / 250 / 全市场）：top_n=0 时翻页拉全市场
（每页 50 只，约 100 页），并按 amount/volume 排序后返回前 N。
失败重试 2 次（退避 1/3 秒），页间节流 0.8 秒；urllib → socket HTTP/1.0
双通道复用 ashare_ranking 的实现，避免各自重复实现。
"""

import http.client
import json
import logging
import math
import re
import time
import urllib.error

from adapters.ashare_ranking import _get_via_socket, _get_via_urllib
from adapters.source_health import (
    ensure_available,
    mark_failure,
    mark_success,
)

_logger = logging.getLogger(__name__)

_BASE = "https://vip.stock.finance.sina.com.cn"
_PAGE_SIZE = 50
# 全市场安全上限：5000+ 只 ≈ 100 页 × 50；留足余量防止异常死循环
_MAX_PAGES = 200
# 全市场约 100 页 × 0.8s ≈ 80s（可接受）；降低高频翻页触发 456 的概率
_PAGE_INTERVAL = 0.8
# 初始 1 次 + 重试 2 次，共 3 次尝试；退避 1/3 秒
_MAX_ATTEMPTS = 3
_RETRY_BACKOFFS = (1.0, 3.0)

# 完整浏览器头：Referer 必须为新浪财经站内页，否则可能被限流。
_SINA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://finance.sina.com.cn/",
    "Connection": "close",
}


class SinaRankingError(RuntimeError):
    """双通道均失败：携带通道与原因，便于上层按 <通道>: <原因> 记录。"""

    def __init__(self, channel: str, reason: str):
        super().__init__(f"{channel}: {reason}")
        self.channel = channel
        self.reason = reason


def _node_for_market(market: str) -> str:
    """市场名 → 新浪 node：a/hs_a/A股 → hs_a；us/美股 → us。"""
    m = str(market or "").lower()
    if m in ("us", "美股", "us_stock", "nasdaq", "nyse"):
        return "us"
    return "hs_a"


def _market_label(market: str) -> str:
    """node/市场名 → 展示标签。"""
    return "美股" if _node_for_market(market) == "us" else "A股"


def _num(raw, scale: float = 1.0) -> float | None:
    """原始值 → 数值；'-' 等缺失标记返回 None。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v * scale, 2) if scale != 1.0 else v


def parse_hq_nodes(text: str) -> list[dict]:
    """解析新浪排行响应（GBK 解码后的文本）。

    新浪返回的是未加引号键名的类 JSON 数组：[{symbol:"sh600519",...}]，
    这里先给键名补引号，再走标准 json.loads；解析失败返回空列表。
    """
    if not text:
        return []
    # 键名补引号：只匹配行内 {, 或 [, 之后的裸键（值为数字时同样成立）
    quoted = re.sub(
        r"([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:",
        r'\1"\2":',
        text,
    )
    try:
        data = json.loads(quoted)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _logger.warning("sina hq nodes parse failed: %s", str(exc)[:120])
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _get(url: str, timeout: int = 25, attempt: int = 1) -> str:
    """GET 文本：复用 ashare_ranking 的 urllib → socket HTTP/1.0 双通道。

    新浪响应为 GBK，双通道均按 gbk 解码；两通道都失败抛异常并带原因。
    """
    try:
        return _get_via_urllib(
            url, timeout=timeout, encoding="gbk", headers=_SINA_HEADERS,
        )
    except (urllib.error.URLError, ConnectionError, http.client.HTTPException) as exc:
        urllib_reason = f"{type(exc).__name__}: {exc}"
        _logger.warning(
            "sina fetch attempt %d failed: urllib: %s",
            attempt, urllib_reason,
        )
    try:
        return _get_via_socket(
            url, timeout=timeout, encoding="gbk", headers=_SINA_HEADERS,
        )
    except Exception as exc:
        socket_reason = f"{type(exc).__name__}: {exc}"
        _logger.warning(
            "sina fetch attempt %d failed: socket: %s",
            attempt, socket_reason,
        )
        raise SinaRankingError(
            "urllib+socket",
            f"urllib: {urllib_reason}; socket: {socket_reason}",
        ) from exc


def _hq_url(node: str, metric: str, page: int, num: int = _PAGE_SIZE) -> str:
    """构造 getHQNodeData 分页 URL。"""
    sort = "volume" if metric == "volume" else "amount"
    return (
        f"{_BASE}/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        f"?page={int(page)}&num={int(num)}&sort={sort}&asc=0"
        f"&node={node}&symbol=&_s_r_a=page"
    )


def _count_url(node: str) -> str:
    """构造全市场数量统计 URL（用于 total 字段，失败不影响主流程）。"""
    return (
        f"{_BASE}/quotes_service/api/json_v2.php/"
        f"Market_Center.getHQNodeStockCount?node={node}"
    )


def _fetch_page(url: str, page: int) -> str:
    """单页拉取：失败重试 2 次，退避 1/3 秒。"""
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return _get(url, timeout=25, attempt=attempt)
        except Exception as exc:
            last_error = exc
        if attempt < _MAX_ATTEMPTS:
            time.sleep(_RETRY_BACKOFFS[attempt - 1])
    raise last_error


def _fetch_total(node: str) -> int | None:
    """全市场数量（best-effort）；失败返回 None，不阻断排行主流程。"""
    try:
        text = _get(_count_url(node), timeout=15, attempt=1)
        m = re.search(r"\d+", str(text or ""))
        return int(m.group(0)) if m else None
    except Exception as exc:
        _logger.warning("sina total count fetch failed: %s", str(exc)[:120])
        return None


def _to_row(raw: dict, rank: int) -> dict:
    """新浪原始行 → eastmoney_ranking 同构行（含 mktcap_yi 别名）。"""
    amount_yuan = _num(raw.get("amount"))
    volume = _num(raw.get("volume"))  # 新浪口径为股
    mktcap_yi = _num(raw.get("mktcap"), 1e-8)
    return {
        "rank": rank,
        "code": str(raw.get("code") or ""),
        "name": str(raw.get("name") or ""),
        "price": _num(raw.get("trade")),
        "change_pct": _num(raw.get("changepercent")),
        "volume_hand": (
            round(volume / 100.0, 2) if volume is not None else None
        ),
        "volume_wan_hand": (
            round(volume / 1e6, 2) if volume is not None else None
        ),
        "amount_yuan": amount_yuan,
        "amount_yi": (
            round(amount_yuan / 1e8, 2) if amount_yuan is not None else None
        ),
        "turnover_pct": _num(raw.get("turnoverratio")),
        "market_cap_yi": mktcap_yi,
        "mktcap_yi": mktcap_yi,
    }


def fetch_ranking(
    metric: str = "amount",
    top_n: int = 0,
    market: str = "hs_a",
) -> dict:
    """获取 A股/美股 成交额/成交量排行前 N（新浪分页全市场）。

    Args:
        metric: "volume"（成交量）或 "amount"（成交额，默认）。
        top_n: 返回条数；0 表示拉取全市场（默认），支持任意正整数。
        market: 新浪 node（hs_a / us）或市场名（a / us / A股 / 美股）。

    Returns:
        {rows, metric, top_n, total, fetched_count, source, market,
         source_url, retrieved_at, partial}
        rows 与 eastmoney_ranking 同构（rank/code/name/price/change_pct/
        volume_hand/volume_wan_hand/amount_yuan/amount_yi/turnover_pct/
        market_cap_yi，另含 mktcap_yi 别名）；partial=True 表示分页中途
        失败但已获取 ≥1 页（total=None，统计类任务可用部分数据并标注覆盖率）。
        首页即失败抛异常（调用方回退搜索链路 / 预载重试）；源处于冷却期时
        快失败，不浪费请求。成功/失败同步维护源健康注册表。
    """
    ensure_available("sina_ranking")
    metric = "volume" if metric == "volume" else "amount"
    node = _node_for_market(market)
    top_n = int(top_n or 0)
    max_pages = (
        max(1, math.ceil(top_n / _PAGE_SIZE)) if top_n > 0 else _MAX_PAGES
    )
    raw_rows: list[dict] = []
    first_url = ""
    fetched_pages = 0
    partial = False
    interrupted: Exception | None = None
    for page in range(1, max_pages + 1):
        url = _hq_url(node, metric, page)
        if not first_url:
            first_url = url
        try:
            text = _fetch_page(url, page)
        except Exception as exc:
            interrupted = exc
            mark_failure("sina_ranking", exc)
            break
        batch = parse_hq_nodes(text)
        raw_rows.extend(batch)
        fetched_pages = page
        if len(batch) < _PAGE_SIZE:
            break
        if top_n > 0 and len(raw_rows) >= top_n:
            break
        if page < max_pages:
            # 页间节流：避免高频翻页触发限流
            time.sleep(_PAGE_INTERVAL)
    if not raw_rows:
        if interrupted is not None:
            raise interrupted
        exc = RuntimeError(
            f"新浪行情排行无数据（node={node}, pages={fetched_pages}）"
        )
        mark_failure("sina_ranking", exc)
        raise exc
    if interrupted is not None:
        # 部分数据降级：已获取 ≥1 页不抛错，统计类任务仍可用并标注覆盖率
        partial = True
        _logger.warning(
            "sina ranking partial data after %d pages "
            "(fetched %d rows): %s",
            fetched_pages, len(raw_rows), str(interrupted)[:160],
        )
    sort_key = "volume" if metric == "volume" else "amount"
    valid = [
        r for r in raw_rows
        if _num(r.get(sort_key)) is not None
    ]
    valid.sort(key=lambda r: float(_num(r.get(sort_key))), reverse=True)
    selected = valid[:top_n] if top_n > 0 else valid
    rows = [_to_row(r, i) for i, r in enumerate(selected, 1)]
    # 部分数据时总数未知（未翻完全市场），由调用方按 fetched_count 标注覆盖率
    total = None if partial else _fetch_total(node)
    if not partial:
        mark_success("sina_ranking")
    return {
        "rows": rows,
        "metric": metric,
        "top_n": len(rows),
        "total": total if total is not None else None,
        "fetched_count": len(raw_rows),
        "partial": partial,
        "source": "sina_ranking",
        "market": _market_label(node),
        "market_node": node,
        "source_url": first_url,
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    import sys
    metric = sys.argv[1] if len(sys.argv) > 1 else "amount"
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    market = sys.argv[3] if len(sys.argv) > 3 else "hs_a"
    result = fetch_ranking(metric, top_n, market)
    print(json.dumps(result, ensure_ascii=False, indent=1))
