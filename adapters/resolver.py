# -*- coding: utf-8 -*-
"""公司名 → 股票代码解析（东方财富证券搜索 JSON 接口）。

无需 API Key：GET https://searchapi.eastmoney.com/api/suggest/get?input=腾讯&type=14
返回 [{Code, Name, JYS(HK/US/SH/SZ), SecurityTypeName, QuoteID, MktNum}]。
"""

import json
import os
import re
import urllib.parse
import urllib.request

_SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"

_EXCHANGE_TO_MARKET = {
    "HK": "HK",
    "NASDAQ": "US", "NYSE": "US", "AMEX": "US", "OTC": "US", "US": "US",
    "SH": "CN", "SZ": "CN", "BJ": "CN",
}

# 衍生品/ETF 产品名特征：窝轮、期权、收益策略 ETF 等不是正股
_DERIVATIVE_RE = re.compile(
    r"(沽|购|权证|法兴|摩通|瑞银|麦格理|高盛|德银|花旗|汇丰|中银|大和|野村|"
    r"法巴|渣打|ETF|收益|周收益|期权|溢价|杠杆|做多|做空|债券|优先)",
)

_NON_EQUITY_TYPES = ("债券", "期货", "板块", "基金", "指数", "权证", "港股通", "Reits")

# P2-5 双市场公司市场偏好：按 WEAVEMIND_MARKET_PREFERENCE 改变市场加分。
# hk：港优先（默认，与历史行为一致）；cn：A股优先；us：美股优先；
# auto：等同 hk，保持现状加分规则。
_MARKET_PREFERENCE_BONUS = {
    "hk": {"HK": 10, "US": 6, "CN": 4},
    "cn": {"CN": 10, "HK": 4, "US": 6},
    "us": {"US": 10, "HK": 4, "CN": 6},
}
_VALID_PREFERENCES = ("hk", "us", "cn", "auto")


def market_preference() -> str:
    """读取市场偏好环境变量：WEAVEMIND_MARKET_PREFERENCE（hk/us/cn/auto，默认 hk）。"""
    pref = str(os.environ.get("WEAVEMIND_MARKET_PREFERENCE", "hk") or "hk").lower()
    if pref not in _VALID_PREFERENCES:
        return "hk"
    return pref


def _market_of(m: dict) -> str:
    """按 SecurityTypeName 分类市场（JYS 对 A股不统一：沪A=2/深A=6或80）。"""
    tn = str(m.get("type_name") or "")
    if tn in ("港股",):
        return "HK"
    if tn in ("美股", "粉单", "OTC"):
        return "US"
    if tn in ("沪A", "深A", "创业板", "科创板", "北交所"):
        return "CN"
    if tn == "日股":
        return "JP"
    jys = str(m.get("jys") or "").upper()
    return _EXCHANGE_TO_MARKET.get(jys, jys)


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
    过滤衍生品/ETF；优先正股：港股的"股份/控股/集团"母体 > 其他正股。"""
    if not company:
        return None
    matches = suggest(company)
    if not matches:
        return None
    exact = [
        m for m in matches
        if str(m.get("type_name") or "") not in _NON_EQUITY_TYPES
        and (company in m["name"] or m["name"] in company)
        and not _DERIVATIVE_RE.search(m["name"])
    ]
    pool = exact or matches
    # P2-5 市场偏好显式化：按 WEAVEMIND_MARKET_PREFERENCE 选择市场加分；
    # auto 与默认 hk 保持既有加分规则
    bonus = _MARKET_PREFERENCE_BONUS.get(
        market_preference(), _MARKET_PREFERENCE_BONUS["hk"],
    )

    def rank(m):
        r = 0
        mk = _market_of(m)
        r += bonus.get(mk, 0)
        if any(k in m["name"] for k in ("股份", "控股", "集团")):
            r += 3
        if m["name"] == company:
            r += 2
        if re.search(r"-(?:T|W?R)$", m["name"]):
            # "微软-T" 信托、"美团-WR/小米集团-WR" 人民币柜台等变体，非正股主代码；
            # 不罚 "-W/-SW"（同股不同权/二次上市正股主代码，如 美团-W、阿里巴巴-SW）
            r -= 8
        return -r  # 分数高者优先

    ordered = sorted(pool, key=rank)
    best = ordered[0]
    return {
        "market": _market_of(best),
        "stock_code": best["code"],
        "name": best["name"],
        "quote_id": best["quote_id"],
        # P2-5 候选正股列表（按得分排序，取前 5），供上层标注选择依据
        "resolved_alternatives": [
            {"market": _market_of(m), "name": m["name"], "code": m["code"]}
            for m in ordered[:5]
        ],
    }
