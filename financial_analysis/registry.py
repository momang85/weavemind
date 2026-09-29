# -*- coding: utf-8 -*-
"""注册模型：**白名单**。没有注册就没有算子，也不允许任意生成 Python/eval/AutoML。

注册表是唯一真相：`ModelSpec` 说清适用问题、输入输出类型/量纲/口径、参数界限、
容差、验证方案与预算；`runner` 只认这里列出的算子名。
"""
from __future__ import annotations

from .contracts import ModelSpec, NotApplicable
from .models import profit_bridge

# 算子白名单：名字 → (实现模块, 计算函数, 独立金样函数)
OPERATORS = {
    "profit_bridge_v1": (profit_bridge, profit_bridge.compute, profit_bridge.gold),
}

# 同年比率（复用既有口径的四个比率，不重新发明）
RATIO_MODELS: tuple[tuple[str, str, str, str], ...] = (
    ("net_margin", "net_profit", "revenue", "归母净利率"),
    ("cashflow_coverage", "operating_cashflow", "net_profit", "经营现金流对归母净利覆盖"),
    ("debt_ratio", "total_liabilities", "total_assets", "资产负债率"),
    ("rd_intensity", "rd_expense", "revenue", "研发投入强度"),
)

_REGISTRY: dict[str, ModelSpec] = {profit_bridge.SPEC.model_id: profit_bridge.SPEC}


def specs() -> tuple[ModelSpec, ...]:
    return tuple(_REGISTRY.values())


def spec(model_id: str) -> ModelSpec:
    key = str(model_id or "")
    if key not in _REGISTRY:
        raise NotApplicable(f"未注册模型：{key}（不得改写目标/猜最后一列来代替模型）")
    return _REGISTRY[key]


def operators() -> tuple[str, ...]:
    return tuple(OPERATORS)


def ratio_specs() -> tuple[tuple[str, str, str, str], ...]:
    return RATIO_MODELS


def available_for(dataset) -> tuple[str, ...]:
    """这份数据集**够得着**哪些模型（只做输入齐备性判断，不预判结果对错）。

    期间取用一律走 `dataset.period_at(offset)`（0 = 最新一期，-1 = 上一期）——
    不按字符串猜"哪个最新"，也不允许调用方自己拼期间。
    """
    out: list[str] = []
    for m in specs():
        if all(dataset.get(i.metric, dataset.period_at(i.period_offset)) is not None
               for i in m.inputs):
            out.append(m.model_id)
    return tuple(out)
