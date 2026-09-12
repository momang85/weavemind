# -*- coding: utf-8 -*-
"""巨潮资讯网（cninfo）A 股年报适配器。

官方来源定位：www.cninfo.com.cn 是深交所指定的法定信息披露平台，
A 股年报/公告的权威原始出处（"数据源官方化"合规叙事的落点）。

接口勘探结论（2026-09-07 探针，见 docs/巨潮数据源探针_20260907.md）：
    - 公告搜索接口 POST /new/hisAnnouncement/query 可达（HTTP 200 JSON），
      但对无 cookie/无 token 的服务端请求恒返回 announcements=[]，
      各类参数组合（stock 格式/category/seDate/Referer 伪装）均无数据；
    - webapi.cninfo.com.cn 财务数据族对非浏览器请求返回 405/超时，
      疑似 mcode token 门禁；
    - szse_stock.json（orgId 映射表）请求超时。

因此本模块当前是"骨架 + 环境开关"形态：
    - WEAVEMIND_CNINFO_ENABLED=1 时启用（默认关闭，路由自动回退东财）；
    - 接口打通后只需实现 _fetch_annual_rows()（返回与东财 RDEXPEND 同构的
      年报行序列），fetch_annual_report() 的解析/组装逻辑即可直接复用；
    - 输出契约与 adapters/eastmoney.fetch_ashare 完全一致
      （{financials, metadata, raw}，金额单位亿元），含 rd_expense。
"""

import json
import time
import urllib.parse

_SOURCE = "cninfo_annual"
_QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"


class CninfoError(Exception):
    """巨潮接口调用失败（含无数据/参数不兼容）。"""

    def __init__(self, channel: str, reason: str):
        super().__init__(f"cninfo[{channel}]: {reason}")
        self.channel = channel
        self.reason = reason


def enabled() -> bool:
    """巨潮链路是否启用（默认关闭：接口探针未通过，见模块 docstring）。"""
    import os
    return os.environ.get("WEAVEMIND_CNINFO_ENABLED", "0") == "1"


def _to_yi(raw) -> float | None:
    """原始值（元）→ 亿元。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v / 1e8, 2)


def _fetch_annual_rows(stock_code: str, year_range=None) -> list[dict]:
    """拉取年报财务行（探针未通过，当前恒抛错触发回退）。

    接口打通后的实现要点：
        1. POST _QUERY_URL，form: stock="{code},{orgId}"、
           category=category_ndbg_szsh、seDate 覆盖 year_range；
        2. orgId 需先查 szse_stock.json/sse_stock.json（该端点当前超时）；
        3. 年报为 PDF（adjunctUrl）——需要 PDF 科目解析
           （营收/净利/研发投入取自主要会计数据表），或改走
           webapi.cninfo.com.cn 财务数据族（需 mcode token）。
    """
    raise CninfoError("probe", "巨深接口探针未通过（恒空/405），详见模块 docstring")


def fetch_annual_report(
    company: str, stock_code: str, year_range=None, max_years: int = 12,
) -> dict:
    """抓取 A 股年报财务指标（营收/净利/研发投入等），输出契约同 eastmoney.fetch_ashare。"""
    from adapters.source_health import ensure_available, mark_failure, mark_success

    ensure_available(_SOURCE)
    try:
        rows = _fetch_annual_rows(stock_code, year_range)
        if not rows:
            raise CninfoError("data", f"巨潮 A股 无数据: {stock_code}")
        annuals = [r for r in rows if "年报" in str(r.get("REPORT_TYPE") or "")]
        annuals.sort(key=lambda r: str(r.get("REPORT_DATE") or ""), reverse=True)
        if year_range:
            start, end = year_range
            annuals = [
                r for r in annuals
                if start <= int(str(r.get("REPORT_DATE") or "")[:4]) <= end
            ]
        annuals = annuals[:max_years]
        mark_success(_SOURCE)
    except Exception as exc:
        mark_failure(_SOURCE, str(exc))
        raise

    financials = []
    for r in annuals:
        financials.append({
            "year": int(str(r.get("REPORT_DATE") or "")[:4]),
            "report_type": str(r.get("REPORT_TYPE") or ""),
            "revenue": _to_yi(r.get("TOTALOPERATEREVE")),
            "net_profit": _to_yi(r.get("PARENTNETPROFIT")),
            "gross_profit": _to_yi(r.get("MLR")),
            "gross_margin": round(float(r["XSMLL"]), 2)
                            if r.get("XSMLL") is not None else None,
            "operating_profit": _to_yi(r.get("OPERATE_PROFIT_PK")),
            "total_assets": _to_yi(r.get("TOTAL_ASSETS_PK")),
            "total_liabilities": _to_yi(r.get("LIABILITY")),
            "operating_cashflow": _to_yi(r.get("NETCASH_OPERATE_PK")),
            "rd_expense": _to_yi(r.get("RDEXPEND")),
        })
    metadata = {
        "source": _SOURCE,
        "company": str(company or ""),
        "stock_code": str(stock_code),
        "currency": "CNY",
        "unit": "亿元",
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "annual_count": len(annuals),
        "latest_report": f"{str(annuals[0].get('REPORT_DATE') or '')[:10]} 年报" if annuals else "",
    }
    return {
        "financials": financials,
        "metadata": metadata,
        "raw": {
            "url": _QUERY_URL,
            "text": json.dumps({"stock_code": stock_code, "rows": len(annuals)}, ensure_ascii=False),
        },
    }


def fetch_cn_or_fallback(company: str, stock_code: str, year_range=None,
                         max_years: int = 12, period: str = "annual") -> dict:
    """A 股财务抓取统一入口：巨潮优先（启用且可用时），东财兜底。

    路由层调用本函数即可获得"官方源优先 + 降级"语义；
    巨潮关闭（默认）时直接走东财，行为与现状完全一致。
    period 透传给东财（annual/quarter/all）；巨潮通道目前只做年报，
    period != annual 时直接走东财（它才带季报/中报行）。"""
    if enabled() and str(period or "annual").lower() == "annual":
        try:
            return fetch_annual_report(company, stock_code, year_range, max_years)
        except Exception as exc:
            import logging
            logging.getLogger("adapters.cninfo").info(
                "cninfo unavailable, fallback to eastmoney_ashare: %s", str(exc)[:120])
    from adapters.eastmoney import fetch_ashare
    return fetch_ashare(company, stock_code, year_range, max_years, period=period)
