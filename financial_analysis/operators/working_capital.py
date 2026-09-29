# -*- coding: utf-8 -*-
"""营运资本：占款增加在哪里、周转如何变化。

口径纪律（架构 §5.4）：

- 两期**平均余额**周转对比一般需要**三个期末余额**：只有两期期末时，用期末余额并在
  结果里写明"这是期末口径、不是平均余额"，**不悄悄用年末余额冒充平均余额**；
- 应付周转的采购分母与"用营业成本近似"必须分开写；
- 指标词表里目前**没有**应收/存货/应付这些 slug（现役事实层只有收入/利润/现金流/资产/负债等），
  所以本模型在真实样本上会如实返回 `missing_input` 并给出**要补哪些指标**——
  这正是"缺输入就拒绝相应模型、不凑数"。
"""
from __future__ import annotations

from decimal import Decimal

from ..contracts import (
    InputRequirement, ModelSpec, NotApplicable, NotComputable, OutputSpec,
)

IMPL_VERSION = "working_capital/1.0.0"

# 本族需要的指标 slug（事实层若没有，就按缺输入处理并写进补料清单）
REQUIRED_SLUGS = ("accounts_receivable", "inventory", "accounts_payable",
                  "revenue", "operating_cost")

LIMITS = (
    "只有两期期末余额时用**期末口径**：不是平均余额（平均需要三个期末余额）",
    "应付周转的采购分母用营业成本近似——这是近似，不是采购额本身",
    "天数按 365 天/年折算；分母（收入/成本）为 0 或非正时不给天数",
)

SPEC = ModelSpec(
    model_id="working_capital",
    version="1.0.0",
    family="营运资本",
    question="占款增加在哪里、周转如何变化",
    operator="working_capital_v1",
    inputs=(
        InputRequirement("accounts_receivable_cur", "accounts_receivable", period_offset=0),
        InputRequirement("accounts_receivable_prev", "accounts_receivable", period_offset=-1),
        InputRequirement("inventory_cur", "inventory", period_offset=0),
        InputRequirement("inventory_prev", "inventory", period_offset=-1),
        InputRequirement("accounts_payable_cur", "accounts_payable", period_offset=0),
        InputRequirement("accounts_payable_prev", "accounts_payable", period_offset=-1),
        InputRequirement("revenue_cur", "revenue", period_offset=0),
        InputRequirement("operating_cost_cur", "operating_cost", period_offset=0),
    ),
    outputs=(
        OutputSpec("working_capital_occupation_change", "占款变化（应收+存货−应付）",
                   kind="amount"),
        OutputSpec("receivable_days", "应收周转天数（期末口径）", unit="天", kind="count"),
        OutputSpec("inventory_days", "存货周转天数（期末口径）", unit="天", kind="count"),
        OutputSpec("payable_days", "应付周转天数（期末口径）", unit="天", kind="count"),
    ),
    allowed_params={"days_in_year": (365, 360), "rounding": ("yi_2", "yuan_2")},
    tolerance=0.005,
    validations=("gold", "identity", "unit", "posture_disclosed"),
    budget={"steps": 1, "seconds": 5},
    limits=LIMITS,
)


def _d(v) -> Decimal:
    return Decimal(str(v))


def _to_unit(value: Decimal, src_scale: float, dst_scale: float) -> Decimal:
    """把按 `src` 单位写的数换到 `dst` 单位：值 × (源量纲 / 目标量纲)。

    与 `working_paper._RATIO_SPECS` 的既有换算口径一致（`factor = 分子量纲 / 分母量纲`）；
    反过来乘会把 万元→亿元 放大 1e8 倍而不报错（夹具实测过这个反例）。
    """
    if not src_scale or not dst_scale:
        raise NotApplicable("金额量纲不可换算")
    return value * Decimal(str(src_scale)) / Decimal(str(dst_scale))


def compute(dataset, params: dict | None = None) -> dict:
    params = dict(params or {})
    days = int(params.get("days_in_year") or 365)
    cur_p, prev_p = dataset.period_at(0), dataset.period_at(-1)
    ar_c = dataset.require("accounts_receivable", cur_p)
    ar_p = dataset.require("accounts_receivable", prev_p)
    inv_c = dataset.require("inventory", cur_p)
    inv_p = dataset.require("inventory", prev_p)
    ap_c = dataset.require("accounts_payable", cur_p)
    ap_p = dataset.require("accounts_payable", prev_p)
    rev = dataset.require("revenue", cur_p)
    cost = dataset.require("operating_cost", cur_p)
    for a, b in ((ar_c, ar_p), (inv_c, inv_p), (ap_c, ap_p)):
        if a.entity_id != b.entity_id or a.currency != b.currency or a.caliber != b.caliber:
            raise NotApplicable(f"{a.metric} 两期主体/币种/口径不一致")
        if a.money_scale != b.money_scale:
            raise NotApplicable(f"{a.metric} 两期量纲不一致（{a.unit} vs {b.unit}）")
    if rev.entity_id != ar_c.entity_id or cost.entity_id != ar_c.entity_id:
        raise NotApplicable("分母（收入/成本）与占款项主体不一致")
    unit = str(ar_c.unit or "")
    scale = ar_c.money_scale
    d_ar = _d(ar_c.value) - _d(ar_p.value)
    d_inv = _d(inv_c.value) - _d(inv_p.value)
    d_ap = _d(ap_c.value) - _d(ap_p.value)
    occupation = d_ar + d_inv - d_ap
    q = lambda x: float(x.quantize(Decimal("0.01")))          # noqa: E731

    def _days(balance, base, label: str):
        b = _to_unit(_d(base.value), base.money_scale, scale)
        if b <= 0:
            raise NotComputable(f"{label} 的分母非正（{base.value}{base.unit}）：不给天数")
        return (_d(balance.value) / b * Decimal(days)).quantize(Decimal("0.01"))

    ar_days = _days(ar_c, rev, "收入")
    inv_days = _days(inv_c, cost, "营业成本")
    ap_days = _days(ap_c, cost, "营业成本")
    return {
        "periods": (prev_p, cur_p),
        "formula": (f"占款变化 = Δ应收({ar_c.value}-{ar_p.value}) + Δ存货"
                    f"({inv_c.value}-{inv_p.value}) − Δ应付({ap_c.value}-{ap_p.value})；"
                    f"天数 = 期末余额 / 分母 * {days}"),
        "inputs": (ar_c.fact_id, ar_p.fact_id, inv_c.fact_id, inv_p.fact_id,
                   ap_c.fact_id, ap_p.fact_id, rev.fact_id, cost.fact_id),
        "assumptions": (
            "只有两期期末余额：天数为**期末口径**，不是平均余额（平均需三个期末余额）",
            f"应付周转的分母用营业成本近似（{cost.value}{cost.unit}），不是采购额",
            f"天数按 {days} 天/年折算",
        ),
        "diagnostics": {
            "unit": unit, "currency": ar_c.currency, "caliber": ar_c.caliber,
            "entity": ar_c.entity, "entity_id": ar_c.entity_id,
            "periods": [prev_p, cur_p],
            "input_hashes": {f"{o.metric}@{o.period}": o.observation_hash
                             for o in (ar_c, ar_p, inv_c, inv_p, ap_c, ap_p, rev, cost)},
            "posture": "closing_balance",      # 期末口径（不是平均余额）
            "days_in_year": days,
            "closure": str(occupation - (d_ar + d_inv - d_ap)),
        },
        "outputs": [
            {"metric": "working_capital_occupation_change",
             "label": "占款变化（应收+存货−应付）", "value": q(occupation), "unit": unit,
             "output_period": f"{cur_p}较{prev_p}", "residual": 0.0,
             "components": [
                 {"label": "应收账款变化", "value": q(d_ar), "unit": unit,
                  "formula": f"{ar_c.value} - {ar_p.value}"},
                 {"label": "存货变化", "value": q(d_inv), "unit": unit,
                  "formula": f"{inv_c.value} - {inv_p.value}"},
                 {"label": "应付账款变化（负号=占用减少）", "value": q(-d_ap), "unit": unit,
                  "formula": f"-({ap_c.value} - {ap_p.value})"},
             ]},
            {"metric": "receivable_days", "label": "应收周转天数（期末口径）",
             "value": float(ar_days), "unit": "天", "output_period": cur_p,
             "components": [], "residual": None},
            {"metric": "inventory_days", "label": "存货周转天数（期末口径）",
             "value": float(inv_days), "unit": "天", "output_period": cur_p,
             "components": [], "residual": None},
            {"metric": "payable_days", "label": "应付周转天数（期末口径，分母=营业成本近似）",
             "value": float(ap_days), "unit": "天", "output_period": cur_p,
             "components": [], "residual": None},
        ],
        "limits": LIMITS,
    }


def gold(dataset, params: dict | None = None) -> dict:
    period_prev, period_cur = dataset.period_at(-1), dataset.period_at(0)
    ar_c = dataset.require("accounts_receivable", period_cur)
    ar_p = dataset.require("accounts_receivable", period_prev)
    inv_c = dataset.require("inventory", period_cur)
    inv_p = dataset.require("inventory", period_prev)
    ap_c = dataset.require("accounts_payable", period_cur)
    ap_p = dataset.require("accounts_payable", period_prev)
    rev = dataset.require("revenue", period_cur)
    cost = dataset.require("operating_cost", period_cur)
    occ = (_d(ar_c.value) - _d(ar_p.value)) + (_d(inv_c.value) - _d(inv_p.value)) \
        - (_d(ap_c.value) - _d(ap_p.value))
    days = int((params or {}).get("days_in_year") or 365)
    f_rev = _to_unit(_d(rev.value), rev.money_scale, ar_c.money_scale)
    f_cost = _to_unit(_d(cost.value), cost.money_scale, ar_c.money_scale)
    return {
        "working_capital_occupation_change": float(occ.quantize(Decimal("0.01"))),
        "receivable_days": float((_d(ar_c.value) / f_rev * days).quantize(Decimal("0.01"))),
        "inventory_days": float((_d(inv_c.value) / f_cost * days).quantize(Decimal("0.01"))),
        "payable_days": float((_d(ap_c.value) / f_cost * days).quantize(Decimal("0.01"))),
    }
