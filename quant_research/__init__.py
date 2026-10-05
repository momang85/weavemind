# -*- coding: utf-8 -*-
"""量化域（Y2）：**确定性、可复算、口径显式**的市场读数。

与 `financial_analysis` 的分工：那边吃的是财报 `Observation`（带币种/量纲/归属层/期间身份），
这边吃的是 `market_history/0` 行情载荷（逐日 OHLC + 复权因子 + `available_at`）。**两套输入
契约不互相伪装**——行情没有"归属层""报表范围"，硬塞进财报口径只会把两类证据搅在一起。

本包只用标准库；`adapters.market_history` 在读路径上**延迟导入**，避免 import 期拉起重依赖。
"""
from __future__ import annotations

__all__ = ["event_returns"]
