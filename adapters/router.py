# -*- coding: utf-8 -*-
"""数据源路由器：financial 任务 → 结构化源（东方财富）+ 搜索补充。

Phase 2 范围：financial + 港股 → 东方财富（主）；其余走搜索链路。
"""

from task_classifier import classify_task
from adapters.resolver import resolve_company
from adapters.eastmoney import fetch as fetch_eastmoney


def route_structured(goal: str) -> dict | None:
    """尝试为 financial 任务获取结构化财务数据；成功返回适配器输出，否则 None。"""
    cls = classify_task(goal)
    if cls["domain"] != "financial" or not cls["company"]:
        return None
    res = resolve_company(cls["company"])
    if not res or res["market"] not in ("HK",):
        return None
    try:
        data = fetch_eastmoney(
            res["name"], res["stock_code"], year_range=cls["year_range"],
        )
        data["classification"] = cls
        data["resolution"] = res
        return data
    except Exception:
        return None
