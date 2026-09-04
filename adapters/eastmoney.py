# -*- coding: utf-8 -*-
"""东方财富数据中心适配器（HK 股票主要财务指标）。

数据形态（已勘探验证）：
    GET https://datacenter-web.eastmoney.com/api/data/v1/get
        ?reportName=RPT_HKF10_FN_MAININDICATOR
        &filter=(SECURITY_CODE="00700")
    返回 JSON，含 2014-2025 年报 + 最新季报，89 个字段：
    OPERATE_INCOME 营收 / HOLDER_PROFIT 归母净利 / GROSS_PROFIT 毛利 /
    GROSS_PROFIT_RATIO 毛利率 / OPERATE_PROFIT 经营利润 / TOTAL_ASSETS 总资产 /
    TOTAL_LIABILITIES 总负债 / NETCASH_OPERATE 经营现金流 / BASIC_EPS / ROE_AVG

无需 API Key、无需 HTML/PDF 解析。单位：原始值为元，输出统一为亿元。
"""

import json
import time
import urllib.parse

_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_REPORT = "RPT_HKF10_FN_MAININDICATOR"
_REPORT_A = "RPT_F10_FINANCE_MAINFINADATA"


def _get(url: str, timeout: int = 25) -> str:
    """GET 文本（统一走 transport.get_via_urllib）。"""
    from adapters.transport import get_via_urllib
    return get_via_urllib(
        url, timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )


def _api_url(stock_code: str, page_size: int = 200) -> str:
    qs = urllib.parse.urlencode({
        "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        "pageSize": page_size, "pageNumber": 1,
        "reportName": _REPORT, "columns": "ALL",
        "filter": f'(SECURITY_CODE="{stock_code}")',
    })
    return f"{_BASE}?{qs}"


def _api_url_ashare(stock_code: str, page_size: int = 200) -> str:
    qs = urllib.parse.urlencode({
        "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        "pageSize": page_size, "pageNumber": 1,
        "reportName": _REPORT_A, "columns": "ALL",
        "filter": f'(SECURITY_CODE="{stock_code}")',
    })
    return f"{_BASE}?{qs}"


def _to_yi(raw) -> float | None:
    """原始值（元）→ 亿元。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v / 1e8, 2)


def fetch(company: str, stock_code: str, year_range=None, max_years: int = 12) -> dict:
    """抓取港股主要财务指标（年报序列 + 最新季报）。

    Args:
        company: 公司名（仅用于 metadata 标注）。
        stock_code: 港交所代码，如 "00700"。
        year_range: (start, end) 可选，默认全部。
        max_years: 最多返回的年度条数。

    Returns:
        统一结构 {financials, metadata, raw}；失败抛异常。
    """
    url = _api_url(stock_code)
    text = _get(url)
    data = json.loads(text)
    rows = (data.get("result") or {}).get("data") or []
    if not rows:
        raise RuntimeError(f"EastMoney 无数据: {stock_code}")

    annuals = [r for r in rows if "年报" in str(r.get("REPORT_TYPE") or "")]
    annuals.sort(key=lambda r: str(r.get("REPORT_DATE") or ""), reverse=True)
    if year_range:
        start, end = year_range
        annuals = [
            r for r in annuals
            if start <= int(str(r.get("REPORT_DATE") or "")[:4]) <= end
        ]
    annuals = annuals[:max_years]

    financials = []
    for r in annuals:
        year = int(str(r.get("REPORT_DATE") or "")[:4])
        financials.append({
            "year": year,
            "report_type": str(r.get("REPORT_TYPE") or ""),
            "revenue": _to_yi(r.get("OPERATE_INCOME")),
            "net_profit": _to_yi(r.get("HOLDER_PROFIT")),
            "gross_profit": _to_yi(r.get("GROSS_PROFIT")),
            "gross_margin": round(float(r["GROSS_PROFIT_RATIO"]), 2)
                            if r.get("GROSS_PROFIT_RATIO") is not None else None,
            "operating_profit": _to_yi(r.get("OPERATE_PROFIT")),
            "total_assets": _to_yi(r.get("TOTAL_ASSETS")),
            "total_liabilities": _to_yi(r.get("TOTAL_LIABILITIES")),
            "operating_cashflow": _to_yi(r.get("NETCASH_OPERATE")),
            "basic_eps": round(float(r["BASIC_EPS"]), 3)
                         if r.get("BASIC_EPS") is not None else None,
            "roe": round(float(r["ROE_AVG"]), 2)
                   if r.get("ROE_AVG") is not None else None,
        })

    latest = rows[0] if rows else {}
    metadata = {
        "source": "eastmoney_datacenter",
        "company": str(company or latest.get("SECURITY_NAME_ABBR") or ""),
        "stock_code": str(stock_code),
        "currency": str(latest.get("CURRENCY") or "HKD"),
        "unit": "亿元",
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "annual_count": len(annuals),
        "latest_report": f"{str(latest.get('REPORT_DATE') or '')[:10]} {latest.get('REPORT_TYPE')}",
    }
    return {
        "financials": financials,
        "metadata": metadata,
        "raw": {"url": url, "text": text},
    }


def fetch_ashare(company: str, stock_code: str, year_range=None, max_years: int = 12) -> dict:
    """抓取 A 股主要财务指标（RPT_F10_FINANCE_MAINFINADATA，年报序列 + 最新季报）。"""
    url = _api_url_ashare(stock_code)
    text = _get(url)
    data = json.loads(text)
    rows = (data.get("result") or {}).get("data") or []
    if not rows:
        raise RuntimeError(f"EastMoney A股 无数据: {stock_code}")

    annuals = [r for r in rows if "年报" in str(r.get("REPORT_TYPE") or "")]
    annuals.sort(key=lambda r: str(r.get("REPORT_DATE") or ""), reverse=True)
    if year_range:
        start, end = year_range
        annuals = [
            r for r in annuals
            if start <= int(str(r.get("REPORT_DATE") or "")[:4]) <= end
        ]
    annuals = annuals[:max_years]

    financials = []
    for r in annuals:
        year = int(str(r.get("REPORT_DATE") or "")[:4])
        revenue = _to_yi(r.get("TOTALOPERATEREVE"))
        gross = _to_yi(r.get("MLR"))
        financials.append({
            "year": year,
            "report_type": str(r.get("REPORT_TYPE") or ""),
            "revenue": revenue,
            "net_profit": _to_yi(r.get("PARENTNETPROFIT")),
            "gross_profit": gross,
            "gross_margin": round(float(r["XSMLL"]), 2)
                            if r.get("XSMLL") is not None else None,
            "operating_profit": _to_yi(r.get("OPERATE_PROFIT_PK")),
            "total_assets": _to_yi(r.get("TOTAL_ASSETS_PK")),
            "total_liabilities": _to_yi(r.get("LIABILITY")),
            "operating_cashflow": _to_yi(r.get("NETCASH_OPERATE_PK")),
            "basic_eps": round(float(r["EPSJB"]), 3)
                         if r.get("EPSJB") is not None else None,
            "roe": round(float(r["ROEJQ"]), 2)
                   if r.get("ROEJQ") is not None else None,
        })
    latest = rows[0] if rows else {}
    metadata = {
        "source": "eastmoney_ashare",
        "company": str(company or latest.get("SECURITY_NAME_ABBR") or ""),
        "stock_code": str(stock_code),
        "currency": str(latest.get("CURRENCY") or "CNY"),
        "unit": "亿元",
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "annual_count": len(annuals),
        "latest_report": f"{str(latest.get('REPORT_DATE') or '')[:10]} {latest.get('REPORT_TYPE')}",
    }
    return {
        "financials": financials,
        "metadata": metadata,
        "raw": {"url": url, "text": text},
    }


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else "00700"
    name = sys.argv[2] if len(sys.argv) > 2 else ""
    result = fetch(name, code)
    print(json.dumps(result["metadata"], ensure_ascii=False, indent=1))
    print("\n年份 | 营收(亿) | 归母净利(亿) | 毛利率% | 经营利润(亿) | 总负债(亿)")
    for f in result["financials"]:
        print(f"{f['year']} | {f['revenue']} | {f['net_profit']} | {f['gross_margin']} | "
              f"{f['operating_profit']} | {f['total_liabilities']}")
