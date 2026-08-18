# -*- coding: utf-8 -*-
"""数据源适配器（插件式）：每个源实现统一 fetch 接口，路由器调度合并。

统一输出：
    {
      "financials": [{year, revenue, net_profit, ...}],   # 结构化财务数据（亿元）
      "metadata":   {source, retrieved_at, currency, unit},
      "raw":        {url, text}                            # 供验收溯源
    }
"""

from adapters.eastmoney import fetch as fetch_eastmoney


def fetch_structured_financials(company, stock_code, market="HK", year_range=None):
    """统一入口：按市场路由到可用结构化源（当前：东方财富 → 空）。"""
    if market in ("HK", "CN", "US"):
        try:
            return fetch_eastmoney(company, stock_code, year_range=year_range)
        except Exception:
            pass
    return None
