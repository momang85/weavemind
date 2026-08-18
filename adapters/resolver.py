# -*- coding: utf-8 -*-
"""公司名 → 股票代码解析（东方财富证券搜索 JSON 接口）。

无需 API Key：GET https://searchapi.eastmoney.com/api/suggest/get?input=腾讯&type=14
返回 [{Code, Name, JYS(HK/US/SH/SZ), SecurityTypeName, QuoteID, MktNum}]。
"""

import json
import urllib.parse
import urllib.request

_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"

_EXCHANGE_TO_MARKET = {
    "HK": "HK",
    "NASDAQ": "US", "NYSE": "US", "AMEX": "US", "OTC": "US", "US": "US",
    "SH": "CN", "SZ": "CN", "BJ": "CN",
}


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def suggest(company: str, count: int = 10) -> list[dict]:
    """搜索公司名，返回候选证券列表。"""
    qs = urllib.parse.urlencode({"input": company, "type": 14, "count": count})
    text = _get(f"{_SUGGEST_URL}?{qs}")
    data = json.loads(text)
    items = (data.get("QuotationCodeTable") or {}).get("Data") or []
    out = []
    for it in items:
        out.append({
            "code": str(it.get("Code") or ""),
            "name": str(it.get("Name") or ""),
            "jys": str(it.get("JYS") or ""),
            "type_name": str(it.get("SecurityTypeName") or ""),
            "quote_id": str(it.get("QuoteID") or ""),
            "mkt_num": str(it.get("MktNum") or ""),
        })
    return out


def resolve_company(company: str) -> dict | None:
    """公司名 → {market, stock_code, name, quote_id}；失败返回 None。
    优先名字精确匹配且为港股的条目（Phase 2 只支持港股结构化源）。"""
    if not company:
        return None
    matches = suggest(company)
    if not matches:
        return None
    exact = [m for m in matches if company in m["name"] or m["name"] in company]
    pool = exact or matches
    best = next((m for m in pool if m["jys"] == "HK"), pool[0])
    return {
        "market": _EXCHANGE_TO_MARKET.get(
            str(best["jys"] or "").upper(), str(best["jys"] or "").upper(),
        ),
        "stock_code": best["code"],
        "name": best["name"],
        "quote_id": best["quote_id"],
    }
