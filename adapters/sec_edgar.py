# -*- coding: utf-8 -*-
"""SEC EDGAR 适配器（美股 XBRL 结构化财务数据，权威、无需 API Key）。

- company_tickers.json        : ticker → CIK
- data.sec.gov/.../companyfacts/CIK{}.json : XBRL 财务事实
  （Revenues / NetIncomeLoss / GrossProfit / OperatingIncomeLoss /
   Assets / Liabilities / NetCashProvidedByUsedInOperatingActivities /
   EarningsPerShareBasic，取 10-K 年报）

统一输出 {financials, metadata, raw}；金额统一为 亿美元。
"""

import json
import re
import time
import urllib.request
from datetime import date

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_UA = "WeaveMind Research contact@weavemind.local"

_CIK_CACHE: dict[str, str] = {}

# 字段标签 → companyfacts 中的 XBRL tag（按优先级尝试）
_TAGS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues", "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
    ),
    "net_profit": ("NetIncomeLoss",),
    "gross_profit": ("GrossProfit",),
    "operating_profit": ("OperatingIncomeLoss", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),
    "total_assets": ("Assets",),
    "total_liabilities": ("Liabilities",),
    "operating_cashflow": ("NetCashProvidedByUsedInOperatingActivities",),
    "basic_eps": ("EarningsPerShareBasic",),
}


def _get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="replace")


def resolve_cik(ticker: str) -> str:
    """ticker → CIK（10 位补零），带进程内缓存。"""
    t = str(ticker or "").upper()
    if t in _CIK_CACHE:
        return _CIK_CACHE[t]
    text = _get(_TICKERS_URL)
    data = json.loads(text)
    cik = ""
    for v in data.values():
        if isinstance(v, dict) and str(v.get("ticker") or "").upper() == t:
            cik = str(v.get("cik_str") or "").zfill(10)
            break
    if not cik:
        raise RuntimeError(f"SEC 无此 ticker: {ticker}")
    _CIK_CACHE[t] = cik
    return cik


def _annual_entries(facts: dict, tag: str):
    """取该 tag 的 10-K 年度条目，按财年结束日取最新。
    流量项（营收等）要求 start/end 时长约 350-390 天；
    时点项（资产负债表）start 为 None，按 end 财年直接采纳。"""
    unit = (facts.get(tag) or {}).get("units", {}).get("USD", [])
    by_fy: dict[int, dict] = {}
    for u in unit:
        form = str(u.get("form") or "")
        if not form.startswith("10-K"):
            continue
        start, end = str(u.get("start") or ""), str(u.get("end") or "")
        if len(end) < 8:
            continue
        try:
            d1 = date.fromisoformat(end[:10])
        except ValueError:
            continue
        if start:
            try:
                d0 = date.fromisoformat(start[:10])
            except ValueError:
                continue
            days = (d1 - d0).days
            if not (330 <= days <= 390):
                continue
        fy = d1.year
        by_fy[fy] = {"year": fy, "end": end[:10], "val": u.get("val")}  # 后者覆盖 → 最新
    out = sorted(by_fy.values(), key=lambda x: x["year"])
    return out


def _pick(facts: dict, key: str):
    """按 _TAGS 优先级取第一个有年度数据的 tag。"""
    for tag in _TAGS.get(key, ()):
        ann = _annual_entries(facts, tag)
        if ann:
            return ann
    return []


def fetch(company: str, ticker: str, year_range=None, max_years: int = 12) -> dict:
    """抓取美股 10-K 年报财务数据（美元 → 亿美元）。

    Args:
        company: 公司名（metadata 用）。
        ticker: 美股代码，如 "AAPL"。
    """
    cik = resolve_cik(ticker)
    url = _FACTS_URL.format(cik=cik)
    text = _get(url)
    facts = json.loads(text).get("facts", {}).get("us-gaap", {})
    series = {
        "revenue": _pick(facts, "revenue"),
        "net_profit": _pick(facts, "net_profit"),
        "gross_profit": _pick(facts, "gross_profit"),
        "operating_profit": _pick(facts, "operating_profit"),
        "total_assets": _pick(facts, "total_assets"),
        "total_liabilities": _pick(facts, "total_liabilities"),
        "operating_cashflow": _pick(facts, "operating_cashflow"),
        "basic_eps": _pick(facts, "basic_eps"),
    }
    years = sorted({u["year"] for s in series.values() for u in s})
    if year_range:
        start, end = year_range
        years = [y for y in years if start <= y <= end]
    years = years[-max_years:]

    revenue_by = {u["year"]: u["val"] for u in series["revenue"]}
    net_by = {u["year"]: u["val"] for u in series["net_profit"]}
    gross_by = {u["year"]: u["val"] for u in series["gross_profit"]}
    op_by = {u["year"]: u["val"] for u in series["operating_profit"]}
    assets_by = {u["year"]: u["val"] for u in series["total_assets"]}
    liab_by = {u["year"]: u["val"] for u in series["total_liabilities"]}
    cfo_by = {u["year"]: u["val"] for u in series["operating_cashflow"]}
    eps_by = {u["year"]: u["val"] for u in series["basic_eps"]}

    financials = []
    for y in years:
        revenue = revenue_by.get(y)
        gross = gross_by.get(y)
        financials.append({
            "year": y,
            "report_type": "10-K",
            "revenue": round(revenue / 1e8, 2) if revenue is not None else None,
            "net_profit": round(net_by.get(y) / 1e8, 2) if net_by.get(y) is not None else None,
            "gross_profit": round(gross / 1e8, 2) if gross is not None else None,
            "gross_margin": round(gross / revenue * 100, 2)
                            if gross is not None and revenue else None,
            "operating_profit": round(op_by.get(y) / 1e8, 2) if op_by.get(y) is not None else None,
            "total_assets": round(assets_by.get(y) / 1e8, 2) if assets_by.get(y) is not None else None,
            "total_liabilities": round(liab_by.get(y) / 1e8, 2) if liab_by.get(y) is not None else None,
            "operating_cashflow": round(cfo_by.get(y) / 1e8, 2) if cfo_by.get(y) is not None else None,
            "basic_eps": round(eps_by.get(y), 3) if eps_by.get(y) is not None else None,
        })
    metadata = {
        "source": "sec_edgar",
        "company": str(company),
        "ticker": str(ticker),
        "cik": cik,
        "currency": "USD",
        "unit": "亿美元",
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "annual_count": len(financials),
    }
    # raw 保留原始 XBRL 摘要（截断，避免 Apple 级 companyfacts 数十 MB）
    raw_text = text[:300000]
    return {
        "financials": financials,
        "metadata": metadata,
        "raw": {"url": url, "text": raw_text},
    }


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    company = sys.argv[2] if len(sys.argv) > 2 else ""
    result = fetch(company, ticker)
    print(json.dumps(result["metadata"], ensure_ascii=False, indent=1))
    print("\n年份 | 营收(亿美元) | 净利(亿美元) | 毛利率% | 总资产(亿美元) | 总负债(亿美元)")
    for f in result["financials"][-8:]:
        print(f"{f['year']} | {f['revenue']} | {f['net_profit']} | {f['gross_margin']} | "
              f"{f['total_assets']} | {f['total_liabilities']}")
