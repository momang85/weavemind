# -*- coding: utf-8 -*-
"""3×3「利润目标＋天数冲击占用」网格（纯组合模块，B3；规划 §18.10）。

组合两个**既有**算子——`scenario_sensitivity`（利润）与 `cash_pressure`（占用）——
在同一冻结数据集上逐格调用两次 `run`；不新建情景引擎、不复制另一套求值规则、
不新增管线/服务/worker。组合本身零策略含量：只把两组既有读数放进同一张表。

- **同一冻结 ds**：九格共享同一 `dataset.freeze` 结果，基期与读数完全一致；
- **P**：情景算子 `scenario_net_profit` 输出的 `user` 分量（同一 g/m 传入两个算子）；
- **T**：从冻结事实取**上一期归母净利**（经 `target_net_profit` 参数传入情景算子，
  算子记「使用者给定」）——研究者设定目标，**不是管理层指引、不构成业绩承诺**；
- **C**：占用算子合计（回款＋库存天数冲击的**增量**），不含收入增长自身的基础占款，
  不能叫完整资金缺口，也不与利润相加；
- **B 可选**：预算存在时另列 B−C 与两个条件是否同时满足；缺省时联合状态记
  「未设预算」，利润状态照算、**不自动给可行绿灯**；
- **只呈现所算格**：不外推、不插值、不称优化边界；无时间戳/预算消耗进内容（可冻结重放）。
"""
from __future__ import annotations

from decimal import Decimal

SCHEMA = "weavemind.joint_grid/1"
METHOD_VERSION = "joint_grid/1.0.0"
BELOW_GROSS_MODE = "fixed"  # 本批显式假设（毛利线以下净额按基期结构冻结），不是可调旋钮

# 预算 B 的**来源性质**写在载荷里（不是现金余额/授信额度/董事会预算）
BUDGET_LABEL = "假设的天数冲击额外占用容忍预算（研究者工作假设；不是现金余额、授信额度或董事会预算）"
_C_NOTE = ("C 为回款/库存天数冲击的额外资金占用（合计）：只代表同一收入/毛利情景下天数变化的"
           "增量，不含收入增长自身的基础占款，不能叫完整资金缺口，也不与利润相加")


def _num(v) -> float:
    return float(v)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _yi(v) -> str:
    """显示格式（亿、4 位小数）：只在渲染时取整，冻结值保持原始精度。"""
    if v is None:
        return "—"
    return f"{float(v) / 1e8:,.4f}"


def build_grid(ds, *, entity_id: str, growth_values, margin_deltas,
               collection_days_delta: float = 10, inventory_days_delta: float = 10,
               budget=None, goal_note: str = "") -> dict:
    """逐格调用两个既有算子；返回可冻结重放的纯数据网格（无时间戳）。"""
    from financial_analysis import runner
    from financial_analysis.contracts import NotApplicable  # noqa: F401 - 语义引用
    from financial_analysis.operators.scenario import GROWTH_BOUNDS, MARGIN_BOUNDS
    from financial_analysis.operators.cash_pressure import DAYS_BOUNDS

    if not isinstance(growth_values, (list, tuple)) or not growth_values:
        raise ValueError("growth_values 须为非空数值列表")
    if not isinstance(margin_deltas, (list, tuple)) or not margin_deltas:
        raise ValueError("margin_deltas 须为非空数值列表")
    for name, values, bounds in (("growth_values", growth_values, GROWTH_BOUNDS),
                                 ("margin_deltas", margin_deltas, MARGIN_BOUNDS)):
        if any(not _is_num(v) for v in values):
            raise ValueError(f"{name} 须为数值（g/m 是研究者假设，不是概率区间）")
        if any(float(v) < float(bounds[0]) or float(v) > float(bounds[1]) for v in values):
            raise ValueError(f"{name} 取值超出算子允许范围 {list(bounds)}")
    for name, v in (("collection_days_delta", collection_days_delta),
                    ("inventory_days_delta", inventory_days_delta)):
        if not _is_num(v) or float(v) < float(DAYS_BOUNDS[0]) or float(v) > float(DAYS_BOUNDS[1]):
            raise ValueError(f"{name} 须为数值且在 {list(DAYS_BOUNDS)} 内（天数是使用者假设）")
    if budget is not None and (not _is_num(budget) or float(budget) < 0):
        raise ValueError("budget 须为非负数（元）；缺省表示未设预算")

    # T 必须引用**冻结事实**：取上一期（目标期前一年）归母净利，全精度、不经显示截断。
    prior = ds.period_at(-1)
    if not prior:
        raise ValueError("冻结数据集没有上一期可比：无法解析研究者目标 T")
    t_obs = ds.get("net_profit", prior)
    if t_obs is None or getattr(t_obs, "value", None) is None:
        raise ValueError(f"{prior} 年归母净利在冻结读数中缺失：T 必须引用冻结事实，不用显示精度常量")
    t_value = Decimal(str(t_obs.value))

    base_period = ds.period_at(0)
    grid = {
        "schema": SCHEMA,
        "method_version": METHOD_VERSION,
        "entity_id": str(entity_id),
        "base_period": str(base_period),
        "target_year_note": f"基期 t={base_period}；目标期：基期后一年；T 取上一期（{prior}）归母净利",
        "collection_days_delta": _num(collection_days_delta),
        "inventory_days_delta": _num(inventory_days_delta),
        "below_gross_mode": BELOW_GROSS_MODE,
        "target": {
            "metric": "net_profit",
            "period": str(prior),
            "value": float(t_value),
            "unit": str(t_obs.unit or "元"),
            "source": "冻结读数（dataset 冻结事实，未按显示精度截断）",
            "goal_note": str(goal_note) or "研究者设定：恢复/保持上一期归母净利水平",
            "nature": "研究者设定目标：不是管理层指引，也不构成业绩承诺",
            "passed_as": "target_net_profit 参数传入情景算子（算子记「使用者给定」）",
        },
        "budget": ({"value": _num(budget), "label": BUDGET_LABEL}
                   if budget is not None else None),
        "growth_values": [_num(v) for v in growth_values],
        "margin_deltas": [_num(v) for v in margin_deltas],
        "cells": [],
        "notes": [
            "P 为情景算子 scenario_net_profit 的 user 分量；g/m 两个算子共用同一组值。",
            _C_NOTE,
            "天数为 0 时 C 应为 0（基准复现）；本模块遇到 C≠0 直接报错。",
            "P−T 用冻结精度原值相减；表中显示才四舍五入（亿、4 位小数）。",
            "below_gross_mode=fixed：毛利线以下净额按基期结构冻结——本批显式假设。",
            "网格只呈现所算格点；空白区域不插值、不称优化边界。",
            "g/m 与天数是研究者假设，不是概率区间、预测或披露的实际账期。",
            "两家公司的数值不作排名（非同行业、口径与规模都不同）。",
        ],
    }
    zero_days = (float(collection_days_delta) == 0.0 and float(inventory_days_delta) == 0.0)
    for g in growth_values:
        for m in margin_deltas:
            shared = {"revenue_growth": _num(g), "gross_margin_delta": _num(m)}
            cell = {"g": _num(g), "m": _num(m), "status": "ok",
                    "P": None, "P_label": "", "P_minus_T": None, "C": None,
                    "profit_state": "不可算（P 缺失）", "joint_state": None,
                    "conditions": None, "scenario_run_id": None, "cash_run_id": None,
                    "reason": ""}
            sc = runner.run("scenario_sensitivity", ds, params=dict(
                shared, below_gross_mode=BELOW_GROSS_MODE,
                target_net_profit=float(t_value))).as_dict()
            cp = runner.run("cash_pressure", ds, params=dict(
                shared, collection_days_delta=_num(collection_days_delta),
                inventory_days_delta=_num(inventory_days_delta))).as_dict()
            cell["scenario_run_id"], cell["cash_run_id"] = sc.get("run_id"), cp.get("run_id")
            if sc.get("status") != "validated":
                cell["status"] = "unavailable"
                cell["reason"] = f"情景算子未验证：{sc.get('reason') or sc.get('status')}"
                grid["cells"].append(cell)
                continue
            for o in sc.get("outputs", []):
                if o.get("metric") != "scenario_net_profit":
                    continue
                for c in o.get("components", []):
                    if c.get("component_id") == "user":
                        cell["P"] = float(c.get("value"))
                        cell["P_label"] = str(c.get("label") or "")
            if cell["P"] is None:
                cell["status"] = "unavailable"
                cell["reason"] = "情景算子未给 user 分量"
                grid["cells"].append(cell)
                continue
            # P−T 用原始精度相减（Decimal），显示时才取整
            p_dec = Decimal(str(cell["P"]))
            cell["P_minus_T"] = float(p_dec - t_value)
            cell["profit_state"] = ("达到研究者目标" if p_dec >= t_value
                                    else "未达到研究者目标")
            if cp.get("status") != "validated":
                cell["status"] = "partial"
                cell["reason"] = f"占用算子未验证：{cp.get('reason') or cp.get('status')}"
            else:
                c_value = next((float(o.get("value")) for o in cp.get("outputs", [])
                                if o.get("metric") == "incremental_capital_occupation"), None)
                if c_value is None:
                    cell["status"] = "partial"
                    cell["reason"] = "占用算子未给 incremental_capital_occupation"
                else:
                    if zero_days and c_value != 0.0:
                        raise ValueError("天数为 0 时 C 应为 0：基准复现被破坏（%r）" % c_value)
                    cell["C"] = c_value
            if cell["C"] is not None and budget is not None:
                b_dec = Decimal(str(float(budget)))
                c_dec = Decimal(str(cell["C"]))
                within = c_dec <= b_dec
                both = bool(p_dec >= t_value and within)
                cell["conditions"] = {
                    "profit_target_met": bool(p_dec >= t_value),
                    "within_budget": bool(within),
                    "both_met": both,
                }
                cell["budget_headroom"] = float(b_dec - c_dec)   # B−C（原始精度）
                cell["joint_state"] = "双条件满足" if both else "未同时满足（利润/预算条件明细见 conditions）"
            elif cell["C"] is not None and budget is None:
                cell["joint_state"] = "未设预算"
            grid["cells"].append(cell)
    return grid


def _period_label(period) -> str:
    """显示用周期标签：源值可能已带「年」（如 2023年），去掉尾字避免「2023年 年」重复。"""
    s = str(period or "")
    return s[:-1] if s.endswith("年") else s


def markdown(grid: dict) -> str:
    """冻结产物用表（3×3）：逐格 P、P−T、C；预算存在时另列 B−C 与双条件。"""
    t = grid.get("target") or {}
    lines = ["#### 利润目标＋天数冲击占用网格（3×3）", ""]
    lines.append(f"假设：收入增速 g ∈ "
                 f"{{{', '.join(f'{v * 100:+.2f}%' for v in grid.get('growth_values', []))}}}；"
                 f"毛利率变化 m ∈ "
                 f"{{{', '.join(f'{v * 100:+.2f}pp' for v in grid.get('margin_deltas', []))}}}"
                 f"（研究者假设，非概率区间）。")
    lines.append(f"基期 t={grid.get('base_period')}；目标 T：{_period_label(t.get('period'))} 年归母净利"
                 f"（冻结读数）= {_yi(t.get('value'))} 亿 —— {t.get('goal_note')}；"
                 f"研究者设定目标，不是管理层指引或承诺。")
    lines.append(f"天数冲击：回款 {grid.get('collection_days_delta'):+.0f} 天、"
                 f"库存 {grid.get('inventory_days_delta'):+.0f} 天（使用者假设，可切换 0）；"
                 f"below_gross_mode={grid.get('below_gross_mode')}（显式假设）。")
    b = grid.get("budget")
    if b is not None:
        lines.append(f"预算 B：{_yi(b.get('value'))} 亿 —— {b.get('label')}。")
    else:
        lines.append("预算 B：未设——联合状态记「未设预算」，利润状态照算、不自动给可行绿灯。")
    lines.append("")
    mcols = grid.get("margin_deltas", [])
    header = "| g＼m | " + " | ".join(f"m={v * 100:+.2f}pp" for v in mcols) + " |"
    lines.extend([header, "|" + "---|" * (len(mcols) + 1)])
    for g in grid.get("growth_values", []):
        cells = [f"g={g * 100:+.2f}%"]
        for m in mcols:
            cell = next((c for c in grid.get("cells", [])
                         if c.get("g") == g and c.get("m") == m), None)
            if cell is None:
                cells.append("未计算")
                continue
            if cell.get("status") == "unavailable":
                cells.append(f"不可算（{(cell.get('reason') or '')[:40]}）")
                continue
            text = (f"P {_yi(cell.get('P'))}；P−T {_yi(cell.get('P_minus_T'))}；"
                    f"C {_yi(cell.get('C'))}")
            if cell.get("budget_headroom") is not None:
                text += f"；B−C {_yi(cell.get('budget_headroom'))}；{cell.get('joint_state')}"
            elif cell.get("joint_state"):
                text += f"；{cell.get('joint_state')}"
            cells.append(text)
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("单位：亿（P−T、B−C 同单位）；天数为 0 时 C 应为 0（基准复现）。")
    for note in grid.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)
