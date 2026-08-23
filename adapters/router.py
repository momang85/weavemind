# -*- coding: utf-8 -*-
"""数据源路由器：A股行情排行 → 东方财富 clist / 新浪分页全市场；
financial → 东方财富/SEC；crypto → CoinGecko；macro → FRED；
news → Google News RSS。统一输出 {source, data, metadata}。

P0-2 规模感知：统计类（前 N% / 占比 / 分布 / 全市场）与 top_n > 50 的排行
走 sina_ranking 分页全市场；前十 / 排行前 N（N≤50）走 eastmoney→tencent
快路径。缓存键含 top_n，避免不同规模串缓存。
P1 能力注册表：DATA_CAPABILITIES 按 market + metric + scale 匹配数据源，
关键词保留为快速兜底。
"""

import logging
import math
import re

from task_classifier import classify_task
from adapters.resolver import resolve_company
from adapters.eastmoney import fetch as fetch_eastmoney, fetch_ashare
from adapters.sec_edgar import fetch as fetch_sec
from adapters.coingecko import fetch_market, coin_id
from adapters.macro import fetch_macro
from adapters.news import fetch_news
from adapters.ashare_ranking import fetch_ranking
from adapters.sina_ranking import fetch_ranking as fetch_sina_ranking
from adapters.tencent_quotes import (
    fetch_ranking as fetch_tencent_ranking,
    fetch_us_ranking as fetch_tencent_us_ranking,
)
from adapters.quote_cache import (
    get_ranking as cache_get_ranking,
    set_ranking as cache_set_ranking,
)


logger = logging.getLogger(__name__)

# 快路径规模上限：≤50 走现有 eastmoney→tencent 链，>50 必须分页全市场
_FAST_PATH_MAX_TOP_N = 50
# 统计类/分布类目标关键词：命中即要求全市场数据（含分母）
_STATISTICAL_KEYWORDS = (
    "占比", "比例", "百分位", "分布", "合计", "汇总",
    "份额", "集中度", "全市场", "整个市场",
)

# P1：数据能力注册表（{source: {markets, metrics, scale, paginated, max_top_n}}）
DATA_CAPABILITIES = {
    "eastmoney_ranking": {
        "markets": {"a"},
        "metrics": {"amount", "volume"},
        "scale": "topN",
        "paginated": False,
        "max_top_n": 50,
        "label": "东方财富行情中心",
    },
    "tencent_ranking": {
        "markets": {"a"},
        "metrics": {"amount", "volume"},
        "scale": "topN",
        "paginated": False,
        "max_top_n": 50,
        "label": "腾讯行情（A股候选池）",
    },
    "tencent_us_ranking": {
        "markets": {"us"},
        "metrics": {"amount", "volume"},
        "scale": "topN",
        "paginated": False,
        "max_top_n": 50,
        "label": "腾讯行情（美股候选池）",
    },
    "sina_ranking": {
        "markets": {"a", "us"},
        "metrics": {"amount", "volume"},
        "scale": "full_market",
        "paginated": True,
        "max_top_n": 0,  # 0 = 不限制，支持任意规模/全市场
        "label": "新浪行情中心",
    },
}


_RANKING_KEYWORDS = (
    "成交量排行", "成交额排行", "成交量前十", "成交额前十",
    "涨停", "跌幅榜", "a股今日", "今日a股", "前十股", "排名榜",
    "股票排行", "a股排行", "股票排名", "成交量榜", "成交额榜",
)
# 美股目标关键词：命中后排行优先走腾讯美股候选池（eastmoney 无美股排行）。
_US_RANKING_KEYWORDS = (
    "美股", "纳斯达克", "nasdaq", "nyse", "纽交所", "美国股市",
    "美国股票", "us股", "美股排行",
)
_CRYPTO_KEYWORDS = (
    "加密货币", "比特币", "以太坊", "币价", "虚拟货币", "数字货币",
    "btc", "eth", "bitcoin", "ethereum", "crypto", "coin",
)
_MACRO_KEYWORDS = (
    "gdp", "cpi", "通胀", "通货膨胀", "失业率", "宏观", "宏观经济",
    "unrate", "pmi", "消费者物价",
    # P1-1：利率/降息/美联储类关键词必须路由到 macro（FRED）
    "利率", "降息", "加息", "美联储", "联邦基金",
)
_NEWS_KEYWORDS = (
    "最新新闻", "头条", "要闻", "今日新闻", "实时新闻", "新闻资讯",
    "news", "headline",
)
_COIN_STOPWORDS = {"usd", "cny", "eur", "price", "行情", "价格"}


def _keyword_hit(goal: str, keywords: tuple[str, ...]) -> bool:
    g = str(goal or "").lower()
    return any(k in g for k in keywords)


def _extract_coin(goal: str) -> str:
    """从目标里提取币种：中文别名优先，其次英文 token。"""
    g = str(goal or "").lower()
    for alias in ("比特币", "以太坊", "大饼", "二饼", "狗狗币", "币安币"):
        if alias in g:
            return coin_id(alias)
    tokens = re.findall(r"[a-z]{2,12}", g)
    for tok in tokens:
        if tok in _COIN_STOPWORDS:
            continue
        cid = coin_id(tok)
        if cid != "bitcoin" or tok in ("btc", "bitcoin"):
            return cid
    return "bitcoin"


def _extract_indicator(goal: str) -> str:
    g = str(goal or "").lower()
    if "失业" in g:
        return "UNRATE"
    if "通胀" in g or "cpi" in g or "消费者物价" in g:
        return "CPIAUCSL"
    if "gdp" in g or "国内生产总值" in g:
        return "GDP"
    # P1-1：利率/降息/美联储类目标 → 联邦基金有效利率（DFF）
    if any(k in g for k in ("利率", "降息", "加息", "美联储", "联邦基金")):
        return "DFF"
    return "GDP"


def _wrap(source: str, data: dict, metadata: dict) -> dict:
    """统一输出结构 {source, data, metadata}。"""
    return {"source": source, "data": data, "metadata": metadata}


def _ranking_metric(goal: str) -> str:
    """按目标措辞选择排行口径：明确"成交量"时按成交量，否则按成交额。"""
    g = str(goal or "").lower()
    if "成交量" in g and "成交额" not in g:
        return "volume"
    return "amount"


def _ranking_market(goal: str) -> str:
    """按目标措辞选排行市场：美股目标 → "us"，否则 A股 → "a"。"""
    return "us" if _keyword_hit(goal, _US_RANKING_KEYWORDS) else "a"


def _parse_market(goal: str) -> str:
    """P1：从目标解析市场能力（a / us），复用关键词判定。"""
    return _ranking_market(goal)


def _parse_metric(goal: str) -> str:
    """P1：从目标解析指标能力（amount / volume），复用关键词判定。"""
    return _ranking_metric(goal)


def _parse_scale(goal: str) -> str:
    """P1：从目标解析规模需求（topN / full_market）。

    占比/比例/百分位/分布/合计/汇总/份额/全市场，以及"前 N%"、
    显式前 N（N>50）→ full_market（需分页全市场）；其余 → topN。
    """
    g = str(goal or "").lower()
    if any(k in g for k in _STATISTICAL_KEYWORDS):
        return "full_market"
    if re.search(r"前\s*\d+(?:\.\d+)?\s*%", g):
        return "full_market"
    m = re.search(r"(?:前|top)\s*(\d{1,6})", g)
    if m and int(m.group(1)) > _FAST_PATH_MAX_TOP_N:
        return "full_market"
    return "topN"


def _parse_top_n(goal: str, total: int | float | None = None) -> int | None:
    """从目标解析排行规模：前5% → ceil(总数*5/100)；前250 → 250；前十 → 10；默认 10。

    百分比目标在没有 total 时返回 None（表示需先拉全市场再定前 N）。
    """
    g = str(goal or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", g)
    if m:
        pct = float(m.group(1))
        if total:
            try:
                return max(1, int(math.ceil(float(total) * pct / 100.0)))
            except (TypeError, ValueError):
                pass
        return None
    m = re.search(r"(?:前|top)\s*(\d{1,6})", g)
    if m:
        return int(m.group(1))
    if any(k in g for k in ("前十", "top10", "top 10")):
        return 10
    return 10


def _match_data_source(
    market: str, metric: str, scale: str, top_n: int = 10,
) -> str | None:
    """P1：按 market + metric + scale（+规模）匹配能力注册表。

    full_market 或 top_n>50 → sina_ranking（分页全市场）；
    topN（≤50）→ 对应市场的现有快路径源（A股 eastmoney，美股 tencent_us）。
    匹配失败返回 None，由调用方回退关键词链路。
    """
    market = str(market or "a").lower()
    metric = str(metric or "amount").lower()
    scale = str(scale or "topN").lower()
    top_n = int(top_n or 0)
    if scale == "full_market" or top_n > _FAST_PATH_MAX_TOP_N:
        cap = DATA_CAPABILITIES.get("sina_ranking") or {}
        if market in cap.get("markets", set()) and metric in cap.get("metrics", set()):
            return "sina_ranking"
        return None
    for name in (
        "eastmoney_ranking", "tencent_ranking",
        "tencent_us_ranking", "sina_ranking",
    ):
        cap = DATA_CAPABILITIES.get(name) or {}
        if (
            market in cap.get("markets", set())
            and metric in cap.get("metrics", set())
            and cap.get("scale") == "topN"
        ):
            return name
    return None


def _is_ranking_goal(goal: str) -> bool:
    """是否排行类目标：命中排行关键词，或规模需求为全市场（统计/前 N>50）。

    仅关键词命中不够——"前5%占比"不含"排行"字样，必须靠 scale 兜住；
    同时避免把普通 A股目标（如个股行情）误判为排行。"""
    if _keyword_hit(goal, _RANKING_KEYWORDS):
        return True
    return _parse_scale(goal) == "full_market"


def _ranking_meta(payload: dict, metric: str, market: str, cache_hit: bool = False) -> dict:
    """构造排行 metadata；tencent payload 自带 source，eastmoney 由路由补充。"""
    source = str(payload.get("source") or "")
    if not source:
        if payload.get("market_node") or payload.get("total") is not None:
            source = "sina_ranking"
        elif market == "us":
            source = "tencent_us_ranking"
        else:
            source = "eastmoney_ranking"
    meta = {
        "source": source,
        "market": payload.get("market") or ("美股" if market == "us" else "A股"),
        "metric": payload.get("metric") or metric,
        "top_n": payload.get("top_n") or 0,
        "unit": "亿元",
        "retrieved_at": payload.get("retrieved_at") or "",
        "cache_hit": cache_hit,
    }
    # 全市场规模信息透传（统计任务计算前 N% 需要）
    for key in ("total", "fetched_count", "target_top_n"):
        if payload.get(key) is not None:
            meta[key] = payload.get(key)
    return meta


def _fetch_sina_ranking_payload(
    goal: str, market: str, metric: str,
) -> dict:
    """统计/大规模排行：新浪分页全市场。

    scale=full_market 时拉取全市场（top_n=0），并按目标百分比补 target_top_n，
    供编排器生成"计算前 X% 占比"的 code_execution 指令；显式前 N（>50）
    同样拉全市场后由 target_top_n 标注精确前 N。
    """
    node = "us" if market == "us" else "hs_a"
    payload = fetch_sina_ranking(metric, top_n=0, market=node)
    total = payload.get("total") or len(payload.get("rows") or [])
    target = _parse_top_n(goal, total)
    if target:
        payload["target_top_n"] = target
    return payload


def _route_ranking(goal: str, market: str, metric: str) -> dict:
    """排行路由：按规模选择 sina 分页全市场或 eastmoney→tencent 快路径。

    缓存读取由调用方（route_structured）负责，这里只负责取数。"""
    top_n = _parse_top_n(goal, None)
    scale = _parse_scale(goal)
    if scale == "full_market":
        payload = _fetch_sina_ranking_payload(goal, market, metric)
        # 全市场规模按 target_top_n（或 0）隔离缓存，避免百分比/规模串键
        key_top_n = int(payload.get("target_top_n") or 0)
        cache_set_ranking(market, metric, payload, top_n=key_top_n)
        return payload
    # 快路径：排行前 N（N≤50）→ eastmoney → tencent 降级链
    top_n = int(top_n or 10)
    return _fetch_ranking_with_fallback(market, metric, top_n)


def _route_ranking_by_source(
    source: str, goal: str, market: str, metric: str,
) -> dict:
    """能力注册表命中后的排行取数（source 由 _match_data_source 给出）。

    内部仍走 _route_ranking（规模感知 + 缓存），保证 sina 全市场与
    eastmoney→tencent 快路径行为一致。"""
    return _route_ranking(goal, market, metric)


def _fetch_ranking_with_fallback(
    market: str, metric: str, top_n: int = 10,
) -> dict:
    """排行降级链（快路径，N≤50）：
    A股：eastmoney_ranking → 失败 tencent_ranking（腾讯候选池）；
    美股：优先 tencent_us_ranking。
    腾讯结果回填缓存（任务间复用，键含 top_n）；eastmoney 结果不回填，
    保持原有语义。"""
    top_n = int(top_n or 10)
    if market == "us":
        payload = fetch_tencent_us_ranking(metric, top_n=top_n)
        cache_set_ranking(market, metric, payload, top_n=top_n)
        return payload
    try:
        return fetch_ranking(metric, top_n=top_n)
    except Exception as exc:
        logger.warning("eastmoney ranking fetch failed, try tencent: %s", exc)
        payload = fetch_tencent_ranking(metric, top_n=top_n)
        cache_set_ranking(market, metric, payload, top_n=top_n)
        return payload


def route_structured(goal: str) -> dict | None:
    """按目标关键词路由到结构化数据源；成功返回 {source, data, metadata}，
    失败返回 None（调用方回退搜索链路）。
    顺序：A股行情排行 → crypto → macro → news → financial。"""
    # P0-2/P1：规模感知 + 能力注册表路由。
    # 统计/分布/前 N>50 → sina_ranking 分页全市场；前十/前 N≤50 →
    # eastmoney→tencent 快路径。关键词保留为快速兜底。
    if _is_ranking_goal(goal):
        metric = _parse_metric(goal)
        market = _parse_market(goal)
        top_n = _parse_top_n(goal, None)
        scale = _parse_scale(goal)
        matched = _match_data_source(market, metric, scale, top_n or 10)
        cache_hit = False
        try:
            if scale == "full_market":
                payload = _route_ranking(goal, market, metric)
            else:
                cached = cache_get_ranking(market, metric, top_n or 10)
                if cached:
                    payload = cached
                    cache_hit = True
                    logger.info(
                        "ranking cache hit: market=%s metric=%s top_n=%d",
                        market, metric, top_n or 10,
                    )
                elif matched:
                    payload = _route_ranking_by_source(
                        matched, goal, market, metric,
                    )
                else:
                    payload = _route_ranking(goal, market, metric)
        except Exception as exc:
            # P2-6 预载失败可见化：瞬时接口异常不再静默吞掉，
            # 记录 warning 后由调用方（orchestrator）重试或回退搜索链路
            logger.warning("ranking fetch failed: %s", exc)
            return None
        meta = _ranking_meta(payload, metric, market, cache_hit=cache_hit)
        source = str(payload.get("source") or meta["source"])
        return _wrap(source, payload, meta)
    if _keyword_hit(goal, _CRYPTO_KEYWORDS):
        coin = _extract_coin(goal)
        try:
            out = fetch_market(coin, "usd")
        except Exception as exc:
            logger.warning("crypto fetch failed: %s", exc)
            return None
        if out:
            meta = dict(out.get("metadata") or {})
            data = {
                k: v for k, v in out.items() if k != "metadata"
            }
            return _wrap("coingecko", data, meta)
        return None
    if _keyword_hit(goal, _MACRO_KEYWORDS):
        indicator = _extract_indicator(goal)
        try:
            out = fetch_macro(indicator)
        except Exception as exc:
            logger.warning("macro fetch failed: %s", exc)
            return None
        if out:
            meta = dict(out.get("metadata") or {})
            data = {
                k: v for k, v in out.items() if k != "metadata"
            }
            return _wrap("macro", data, meta)
        return None
    if _keyword_hit(goal, _NEWS_KEYWORDS):
        try:
            out = fetch_news(str(goal or "")[:80])
        except Exception as exc:
            logger.warning("news fetch failed: %s", exc)
            return None
        if out:
            meta = dict(out.get("metadata") or {})
            data = {
                k: v for k, v in out.items() if k != "metadata"
            }
            return _wrap("news", data, meta)
        return None
    # 原有 financial 链路
    cls = classify_task(goal)
    if cls["domain"] != "financial" or not cls["company"]:
        return None
    res = resolve_company(cls["company"])
    if not res or res["market"] not in ("HK", "US", "CN"):
        return None
    try:
        if res["market"] == "HK":
            data = fetch_eastmoney(
                res["name"], res["stock_code"], year_range=cls["year_range"],
            )
        elif res["market"] == "US":
            data = fetch_sec(
                res["name"], res["stock_code"], year_range=cls["year_range"],
            )
        else:
            data = fetch_ashare(
                res["name"], res["stock_code"], year_range=cls["year_range"],
            )
        data["classification"] = cls
        data["resolution"] = res
        return data
    except Exception:
        return None
