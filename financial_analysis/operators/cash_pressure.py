# -*- coding: utf-8 -*-
"""现金压力（经营韧性）：同一收入/毛利情景下，回款与库存天数变化带来的**额外资金占用**。

10-08 §14.2（唯一新增算子，2026-10-08）为什么是这个形状：

- **复用同一情景求值**：`R1 = R0×(1+g)`、`G1 = R1×(基期毛利率+m)`、`C1 = R1−G1` 取自
  `scenario.scenario_revenue_gross`——与 `scenario_sensitivity` 是同一处实现，
  不新建情景引擎、不复制另一套求值规则；
- **只有两行、一个恒等式**：`额外占用 = R1/365×Δ回款代理天数 + C1/365×Δ库存天数`，
  两项相加＝合计（`identity` 独立核对），天数全 0 则合计 0（基准复现）；
- **代理与约定写在载荷里**：营业收入代赊销额、营业成本代现金采购支出、全年 365 天是
  **计算约定**；天数是**使用者假设**，不是披露的实际账期；
- **不是预测**：结果是"同一收入/毛利情景下、相对周转不变基准"的条件占用增量，
  不含收入增长自身新增的基础占款，不加总成完整 CFO、融资缺口、现金余额或概率；
- 首例只做回款与库存两项，付款不变；半年数据继续同期观察，不乘二凑全年。
"""
from __future__ import annotations

from decimal import Decimal

from ..contracts import (
    InputRequirement, ModelSpec, NotApplicable, NotComputable, OutputSpec,
    full_identity_ok,
)

IMPL_VERSION = "cash_pressure/1.0.0"

# 与 `scenario_sensitivity` 同源的情景参数界限（同一把尺，不各自发明）
from .scenario import GROWTH_BOUNDS, MARGIN_BOUNDS  # noqa: E402

# 天数变化是**使用者假设**：允许负向（收回/释放），但不允许到荒谬量级
DAYS_BOUNDS = (-365.0, 365.0)
# 全年天数：首批只允许 365（约定）与 360（某些口径的商用年）——不是可随手调的旋钮
DAYS_IN_YEAR = (360, 365)

LIMITS = (
    "营业收入代**赊销额**、营业成本代**现金采购支出**：都是代理变量，不是实际收款/付款",
    "天数是**使用者设定**的假设，不是披露的实际账期；首批只做回款与库存两项，付款不变",
    "全年 365 天是**计算约定**；半年数据继续同期观察，**不乘二**凑成年度",
    "结果是同一收入/毛利情景下、相对「周转天数不变」基准的**冲击增量**；"
    "不含收入增长自身新增的基础占款",
    "两项贡献严格相加＝合计；天数为 0 时合计为 0（基准复现），不得出现「零冲击仍有占用」",
    "不是经营现金流预测、不是融资缺口、不是现金余额，也不与利润相加；无概率与置信区间",
    "缺应收/存货余额**不阻断**增量计算；缺真实基准天数时，负向天数只能标「条件释放」，"
    "不能宣称实际可缩短到该水平",
    "两家公司的原始金额**不作韧性排名**（非同行业、口径与规模都不同）",
)

SPEC = ModelSpec(
    model_id="cash_pressure",
    version="1.0.0",
    family="经营韧性/现金压力",
    question="同一收入/毛利情景下，回款与库存天数变化会额外占用多少资金",
    operator="cash_pressure_v1",
    inputs=(
        InputRequirement("revenue_base", "revenue", period_offset=0),
        InputRequirement("gross_profit_base", "gross_profit", period_offset=0),
    ),
    outputs=(
        OutputSpec("incremental_capital_occupation",
                   "天数冲击的额外资金占用（回款＋库存，合计）", kind="amount",
                   structure="bridge"),
        OutputSpec("collection_days_capital_per_day",
                   "回款代理：同一情景下每 1 天的资金量", kind="amount"),
        OutputSpec("inventory_days_capital_per_day",
                   "库存代理：同一情景下每 1 天的资金量", kind="amount"),
    ),
    allowed_params={"revenue_growth": GROWTH_BOUNDS,
                    "gross_margin_delta": MARGIN_BOUNDS,
                    "collection_days_delta": DAYS_BOUNDS,
                    "inventory_days_delta": DAYS_BOUNDS,
                    "days_in_year": DAYS_IN_YEAR,
                    "rounding": ("yi_2", "yuan_2")},
    # 不给参数时**实际用的值**（页面直接显示，不让读者猜）：零收入/毛利变化、各 +10 天
    default_params={"revenue_growth": 0.0, "gross_margin_delta": 0.0,
                    "collection_days_delta": 10, "inventory_days_delta": 10},
    tolerance=0.005,
    validations=("gold", "unit", "identity"),
    budget={"steps": 1, "seconds": 5},
    limits=LIMITS,
    question_types=("scenario",),
    component_ids={"incremental_capital_occupation": ("collection_days",
                                                      "inventory_days")},
)


def _d(v) -> Decimal:
    return Decimal(str(v))


def _bounds_ok(params: dict) -> str:
    for k, bounds in (("revenue_growth", GROWTH_BOUNDS),
                      ("gross_margin_delta", MARGIN_BOUNDS),
                      ("collection_days_delta", DAYS_BOUNDS),
                      ("inventory_days_delta", DAYS_BOUNDS)):
        if k not in params:
            continue
        try:
            v = float(params[k])
        except (TypeError, ValueError):
            return f"参数 {k} 不是数：{params[k]!r}"
        if v < float(bounds[0]) or v > float(bounds[1]):
            return f"参数 {k}={v} 超出允许范围 {list(bounds)}（不随手放大假设）"
    if "days_in_year" in params:
        try:
            d = int(params["days_in_year"])
        except (TypeError, ValueError):
            return f"参数 days_in_year 不是整数：{params['days_in_year']!r}"
        if d not in DAYS_IN_YEAR:
            return (f"参数 days_in_year={d} 不在允许取值 {list(DAYS_IN_YEAR)}"
                    "（全年天数在本算子里是**计算约定**，不是自由旋钮）")
    return ""


def plan_flows(dataset, params: dict | None = None) -> dict:
    """**唯一**的情景流量解析（compute 与 gold 共用）：基期两条流量 → R1/G1/C1。

    不做任何"取值从宽"：收入非正、情景成本非正一律 `not_computable`（不是编数）。
    """
    params = dict(params or {})
    bad = _bounds_ok(params)
    if bad:
        raise NotApplicable(bad)
    period = dataset.period_at(0)
    rev = dataset.require("revenue", period)
    gp = dataset.require("gross_profit", period)
    ident_ok, ident_why = full_identity_ok(rev, gp, same_scale=True)
    if not ident_ok:
        raise NotApplicable(ident_why)
    r0, g0 = _d(rev.value), _d(gp.value)
    if r0 <= 0:
        raise NotComputable(f"基期营业收入非正（{rev.value}{rev.unit}）：天数占款强度不适用")
    margin = g0 / r0
    g = _d(params.get("revenue_growth", 0.0) or 0.0)
    m = _d(params.get("gross_margin_delta", 0.0) or 0.0)
    from .scenario import scenario_revenue_gross
    r1, g1 = scenario_revenue_gross(r0, margin, growth=g, margin_delta=m)
    c1 = r1 - g1
    if c1 <= 0:
        raise NotComputable(
            f"情景营业成本非正（R1 {r1} − G1 {g1}）：毛利率假设下成本代理项不成立")
    days = int(params.get("days_in_year") or 365)
    d_coll = _d(params.get("collection_days_delta", 10) or 0.0)
    d_inv = _d(params.get("inventory_days_delta", 10) or 0.0)
    per_day_rev, per_day_cost = r1 / Decimal(days), c1 / Decimal(days)
    coll, inv = per_day_rev * d_coll, per_day_cost * d_inv
    return {
        "period": period, "unit": str(rev.unit or ""),
        "r0": r0, "g0": g0, "margin": margin, "r1": r1, "g1": g1, "c1": c1,
        "growth": g, "margin_delta": m, "days_in_year": days,
        "collection_days_delta": d_coll, "inventory_days_delta": d_inv,
        "per_day_revenue": per_day_rev, "per_day_cost": per_day_cost,
        "collection_occupation": coll, "inventory_occupation": inv,
        "total": coll + inv,
        "rev_fact": rev, "gp_fact": gp, "params": params,
    }


def _q(x) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.01")))


def compute(dataset, params: dict | None = None) -> dict:
    f = plan_flows(dataset, params)
    unit = f["unit"]
    days = f["days_in_year"]
    dc, di = f["collection_days_delta"], f["inventory_days_delta"]
    formula = ("额外占用 = R1/365×Δ回款代理天数 + C1/365×Δ库存天数；"
               "R1 = R0×(1+g)，C1 = R1 − R1×(基期毛利率+m)；付款不变")
    return {
        "periods": (f["period"],),
        "formula": formula,
        "inputs": (f["rev_fact"].fact_id, f["gp_fact"].fact_id),
        "assumptions": tuple(
            [f"基期读数取自 {f['period']}（收入 {f['rev_fact'].value}、毛利 "
             f"{f['gp_fact'].value}{unit}）",
             f"收入增速 g={float(f['growth']):+.4f}、毛利率变化 m="
             f"{float(f['margin_delta']):+.4f}（与 `scenario_sensitivity` 同一处求值器）",
             f"Δ回款代理天数={float(dc):+.2f} 天、Δ库存天数={float(di):+.2f} 天"
             "（**使用者设定**的假设，不是披露的实际账期）",
             f"全年 {days} 天为计算约定；营业收入代赊销额、营业成本代现金采购支出"
             "（代理变量）；首批付款不变",
             "结果是情景内、相对「天数不变」基准的**增量**，不是 CFO 预测/融资缺口，"
             "也不含收入增长自身新增的基础占款"]),
        "diagnostics": {
            "unit": unit, "currency": f["rev_fact"].currency,
            "caliber": f["rev_fact"].caliber, "entity": f["rev_fact"].entity,
            "entity_id": f["rev_fact"].entity_id, "periods": [f["period"]],
            "input_hashes": {o.metric: o.observation_hash
                             for o in (f["rev_fact"], f["gp_fact"])},
            "base": {"revenue": float(f["r0"]), "gross_profit": float(f["g0"]),
                     "margin": float(f["margin"])},
            "scenario": {"revenue": float(f["r1"]), "gross_profit": float(f["g1"]),
                         "operating_cost": float(f["c1"]),
                         "revenue_growth": float(f["growth"]),
                         "gross_margin_delta": float(f["margin_delta"])},
            "days": {"days_in_year": days,
                     "collection_days_delta": float(dc),
                     "inventory_days_delta": float(di),
                     "per_day_revenue": _q(f["per_day_revenue"]),
                     "per_day_cost": _q(f["per_day_cost"])},
            # `identity` 独立验证读它：两项相加＝合计（恒等式闭合差 0）
            "closure": "0",
            "not_a_forecast": ("条件占用增量：无概率、无置信区间；不含增长自身新增占款；"
                               "付款不变、不与利润相加"),
            "proxies": {"revenue": "营业收入代赊销额", "cost": "营业成本代现金采购支出",
                        "days": "使用者假设，非实际账期",
                        "basis": "情景内相对天数不变基准的增量"},
            "caliber_note": ("基期读数与 `scenario_sensitivity` 同一冻结数据集与期间；"
                             "单位先显式换算，收入/成本非正即拒绝相应计算"),
        },
        "outputs": [
            {"metric": "incremental_capital_occupation",
             "label": (f"天数冲击的额外资金占用（回款 {float(dc):+.2f} 天／"
                       f"库存 {float(di):+.2f} 天）"),
             "value": _q(f["total"]), "unit": unit,
             "output_period": f"{f['period']}（情景：g={float(f['growth']):+.2%}, "
                               f"m={float(f['margin_delta']):+.2f}pp）",
             "residual": 0.0,
             "components": [
                 {"component_id": "collection_days",
                  "label": "回款代理天数变化 × R1/365",
                  "value": _q(f["collection_occupation"]), "unit": unit,
                  "formula": (f"R1/365×Δ天数 = {_q(f['per_day_revenue'])}×"
                              f"{float(dc):+.2f}")},
                 {"component_id": "inventory_days",
                  "label": "库存天数变化 × C1/365",
                  "value": _q(f["inventory_occupation"]), "unit": unit,
                  "formula": (f"C1/365×Δ天数 = {_q(f['per_day_cost'])}×"
                              f"{float(di):+.2f}")},
             ]},
            {"metric": "collection_days_capital_per_day",
             "label": "回款代理：每 1 天的资金量（同一情景 R1/365）",
             "value": _q(f["per_day_revenue"]), "unit": unit,
             "output_period": f"{f['period']}（情景：g={float(f['growth']):+.2%}）",
             "components": [], "residual": None},
            {"metric": "inventory_days_capital_per_day",
             "label": "库存代理：每 1 天的资金量（同一情景 C1/365）",
             "value": _q(f["per_day_cost"]), "unit": unit,
             "output_period": f"{f['period']}（情景：g={float(f['growth']):+.2%}, "
                               f"m={float(f['margin_delta']):+.2f}pp）",
             "components": [], "residual": None},
        ],
        "limits": LIMITS,
    }


def components_gold(dataset, params: dict | None = None) -> dict:
    """分项的**独立计算**：两项占用各自手算（不信载荷自报的合计）。"""
    f = plan_flows(dataset, params)
    unit = f["unit"]
    return {"incremental_capital_occupation": {
        "collection_days": (_q(f["collection_occupation"]), unit),
        "inventory_days": (_q(f["inventory_occupation"]), unit)}}


def gold(dataset, params: dict | None = None) -> dict:
    """独立金样：三项输出用 Decimal 从数据集现算（不读载荷的诊断字段）。"""
    f = plan_flows(dataset, params)
    return {"incremental_capital_occupation": _q(f["total"]),
            "collection_days_capital_per_day": _q(f["per_day_revenue"]),
            "inventory_days_capital_per_day": _q(f["per_day_cost"])}
