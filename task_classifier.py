# -*- coding: utf-8 -*-
"""任务分类器（task → domain + metadata）。

规则先行：识别 financial 任务，提取 公司名/年份范围/市场提示；
非 financial 任务返回 domain="general"，走原链路（搜索→抓取→清洗→报告）。
"""

import re

FINANCIAL_KEYWORDS = (
    "财报", "年报", "季报", "营收", "净利润", "财务", "业绩", "负债",
    "报表", "financial", "revenue", "earnings", "annual report", "income statement",
)

_MARKET_HINTS = {
    "HK": ("港股", "香港", "港交所", "hkex"),
    "US": ("美股", "纳斯达克", "nyse", "sec", "纽交所", "nasdaq"),
    "CN": ("a股", "沪深", "上交所", "深交所", "巨潮", "科创板"),
}

_GENERIC_WORDS = (
    r"搜索|总结|分析|调研|梳理|介绍|了解|盘点|回顾|关于|针对|研究|预测|"
    r"评估|生成|撰写|输出|做|写|并|与|和|及|的|之|最新|历年|年度|最近|当前|"
    r"分析一下|介绍一下|盘点一下|梳理一下"
)


def _extract_company(g: str) -> str:
    """提取公司名：优先 'X集团/控股'，其次 'X公司'，再次财报前词。"""
    for pat in (
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}})(?:集团|控股)",
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}})公司",
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}})(?:的)?"
        r"(?:历年|年度|最新)?(?:财报|年报|季报|财务)",
    ):
        m = re.search(pat, g)
        if not m:
            continue
        c = m.group(1)
        if len(c) >= 2:
            return c
    return ""


def classify_task(goal: str) -> dict:
    """分类任务：返回 {domain, company, year_range, market}。"""
    g = str(goal or "")
    gl = g.lower()
    domain = "financial" if any(k in gl for k in FINANCIAL_KEYWORDS) else "general"
    company = _extract_company(g)
    years = [int(y) for y in re.findall(r"(20\d{2})", g)]
    year_range = (min(years), max(years)) if years else None
    market = next(
        (mk for mk, kws in _MARKET_HINTS.items() if any(k in gl for k in kws)),
        None,
    )
    return {
        "domain": domain,
        "company": company,
        "year_range": year_range,
        "market": market,
    }
