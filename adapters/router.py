# -*- coding: utf-8 -*-
"""数据源路由器：financial → 东方财富/SEC；crypto → CoinGecko；
macro → FRED；news → Google News RSS。统一输出 {source, data, metadata}。
"""

import re

from task_classifier import classify_task
from adapters.resolver import resolve_company
from adapters.eastmoney import fetch as fetch_eastmoney, fetch_ashare
from adapters.sec_edgar import fetch as fetch_sec
from adapters.coingecko import fetch_market, coin_id
from adapters.macro import fetch_macro
from adapters.news import fetch_news


_CRYPTO_KEYWORDS = (
    "加密货币", "比特币", "以太坊", "币价", "虚拟货币", "数字货币",
    "btc", "eth", "bitcoin", "ethereum", "crypto", "coin",
)
_MACRO_KEYWORDS = (
    "gdp", "cpi", "通胀", "通货膨胀", "失业率", "宏观", "宏观经济",
    "unrate", "pmi", "消费者物价",
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
    return "GDP"


def _wrap(source: str, data: dict, metadata: dict) -> dict:
    """统一输出结构 {source, data, metadata}。"""
    return {"source": source, "data": data, "metadata": metadata}


def route_structured(goal: str) -> dict | None:
    """按目标关键词路由到结构化数据源；成功返回 {source, data, metadata}，
    失败返回 None（调用方回退搜索链路）。顺序：crypto → macro → news → financial。"""
    if _keyword_hit(goal, _CRYPTO_KEYWORDS):
        coin = _extract_coin(goal)
        out = fetch_market(coin, "usd")
        if out:
            meta = dict(out.get("metadata") or {})
            data = {
                k: v for k, v in out.items() if k != "metadata"
            }
            return _wrap("coingecko", data, meta)
        return None
    if _keyword_hit(goal, _MACRO_KEYWORDS):
        indicator = _extract_indicator(goal)
        out = fetch_macro(indicator)
        if out:
            meta = dict(out.get("metadata") or {})
            data = {
                k: v for k, v in out.items() if k != "metadata"
            }
            return _wrap("macro", data, meta)
        return None
    if _keyword_hit(goal, _NEWS_KEYWORDS):
        out = fetch_news(str(goal or "")[:80])
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
