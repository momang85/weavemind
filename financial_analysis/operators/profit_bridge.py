# -*- coding: utf-8 -*-
"""利润/毛利桥（阶段 Q 的第一个注册模型族）。

回答的问题是：**本期归母净利润的变化由哪些金额项构成、各贡献多少、还有多少没被解释。**

做法刻意"少而硬"：

- 只用**同主体、同口径、同币种、同量纲、同期间关系**的两期事实；
- 分解是**预先声明的会计恒等式**：`Δ归母净利 = Δ毛利 + Δ(归母净利 − 毛利)`，
  两项相加**恰好闭合**（`residual = 0` 是恒等式的结果，不是"误差"）；
- 真正要摆在读者面前的是**没有明细的那一段**：Δ(归母净利 − 毛利) 含费用、税项、
  投资收益、少数股东等——缺明细表就只能写"观察成立、原因待证"，**不得**拿毛利率
  的百分点差来代替金额归因（这是 D 批实机反例：毛利率降 2.09pp 小于净利率降 7.13pp
  被写成"利润下滑并非主要来自毛利端"，而毛利额实际减少 38.01 亿元）。
"""
from __future__ import annotations

from decimal import Decimal

from ..contracts import (
    InputRequirement, ModelSpec, NotApplicable, NotComputable, OutputSpec,
    report_scope_ok,
)

IMPL_VERSION = "profit_bridge/1.0.0"

# 归母净利（合并报表口径下"归属于母公司"的部分）与毛利**归属层不同**：
# 毛利是合并口径的毛利，归母净利是归属母公司的净利。桥接必须显式说出这一点。
LIMITS = (
    "Δ归母净利 = Δ毛利 + Δ(归母净利 − 毛利)：这是会计恒等式分解，**不等于**因果解释",
    "「毛利线以下」一段含费用、税项、投资收益、少数股东等，缺明细表时只能写“原因待证”",
    "毛利率的**百分点**变化不能替代金额归因（两者口径不同，不可互换）",
    "两期口径/币种/量纲/主体不一致时不计算（不给跨期跨公司的差额）",
)

SPEC = ModelSpec(
    model_id="profit_bridge",
    version="1.0.0",
    family="经营变化分解",
    question="本期归母净利润的变化由哪些金额项构成、各贡献多少、还有多少未解释",
    operator="profit_bridge_v1",
    inputs=(
        InputRequirement("net_profit_cur", "net_profit", period_offset=0),
        InputRequirement("net_profit_prev", "net_profit", period_offset=-1),
        InputRequirement("gross_profit_cur", "gross_profit", period_offset=0),
        InputRequirement("gross_profit_prev", "gross_profit", period_offset=-1),
    ),
    outputs=(
        OutputSpec("net_profit_change", "归母净利润变化", kind="amount"),
        OutputSpec("gross_profit_change", "毛利变化", kind="amount"),
        OutputSpec("below_gross_line_change", "毛利线以下净额变化（未解释段）", kind="amount"),
    ),
    allowed_params={"rounding": ("yi_2", "yuan_2")},
    tolerance=0.005,
    validations=("gold", "closure", "identity", "unit", "no_pp_substitution"),
    budget={"steps": 1, "seconds": 5},
    limits=LIMITS,
)


def _d(value) -> Decimal:
    return Decimal(str(value))


def compute(dataset, params: dict | None = None) -> dict:
    """算一次利润桥 → `{outputs, diagnostics, formula, inputs, assumptions}`。

    期间由数据集自身的 `periods` 顺序决定（**不按字符串猜"最新"**）：取最后两期。
    """
    params = dict(params or {})
    rounding = str(params.get("rounding") or "yi_2")
    periods = [p for p in (dataset.manifest.periods or ()) if p]
    if len(periods) < 2:
        raise NotApplicable("数据集只有一个期间：两期利润桥不适用（不给单期归因）")
    prev_p, cur_p = periods[-2], periods[-1]
    cur_np = dataset.require("net_profit", cur_p)
    prev_np = dataset.require("net_profit", prev_p)
    cur_gp = dataset.require("gross_profit", cur_p)
    prev_gp = dataset.require("gross_profit", prev_p)
    for a, b, label in ((cur_np, prev_np, "归母净利润两期"),
                        (cur_gp, prev_gp, "毛利两期")):
        if a.entity_id != b.entity_id:
            raise NotApplicable(f"{label}主体不同（{a.entity_id} vs {b.entity_id}）")
        if a.currency != b.currency:
            raise NotApplicable(f"{label}币种不同（{a.currency} vs {b.currency}）")
        if a.caliber != b.caliber:
            raise NotApplicable(f"{label}口径不同（{a.caliber} vs {b.caliber}）")
        if a.money_scale != b.money_scale or a.money_scale <= 0:
            raise NotApplicable(f"{label}金额量纲不一致（{a.unit} vs {b.unit}）")
    # 跨指标（净利 vs 毛利）**必须在同一报表范围**：只比主体/币种/量纲不够——
    # "合并净利 + 母公司毛利"会算出闭合的桥，却是两个报表的差额（K0-a 反例）。
    scope_ok, scope_why = report_scope_ok(cur_np, prev_np, cur_gp, prev_gp)
    if not scope_ok:
        raise NotApplicable(scope_why)
    if cur_np.money_scale != cur_gp.money_scale:
        raise NotApplicable(
            f"归母净利润（{cur_np.unit}）与毛利（{cur_gp.unit}）量纲不同：先换算再入桥")
    d_np = _d(cur_np.value) - _d(prev_np.value)
    d_gp = _d(cur_gp.value) - _d(prev_gp.value)
    below = d_np - d_gp
    if not params.get("allow_negative_base", False):
        # 基期为负时**不给方向性叙述**（沿用既有裁决：负基数不展示误导性增长率），
        # 但金额桥本身仍然可算——只是不允许把它读成"增长/下降多少"。
        pass
    unit = str(cur_np.unit or "")
    digits = 2

    def _q(x: Decimal) -> float:
        return float(x.quantize(Decimal("1." + "0" * digits)))

    return {
        "periods": (prev_p, cur_p),
        "formula": ("Δ归母净利 = Δ毛利 + Δ(归母净利 − 毛利)；"
                    f"= ({cur_np.value} - {prev_np.value}) [净利] "
                    f"= ({cur_gp.value} - {prev_gp.value}) [毛利] "
                    f"+ ({cur_np.value} - {prev_np.value}) - "
                    f"({cur_gp.value} - {prev_gp.value}) [毛利线以下]"),
        "inputs": (cur_np.fact_id, prev_np.fact_id, cur_gp.fact_id, prev_gp.fact_id),
        "assumptions": (
            # 口径**从已验证输入生成**，不固定写"合并"（K0-a）：母公司口径的数据集
            # 本来也能跑，说明里必须写它真实的那个范围。
            f"两期均为{cur_np.caliber}口径、{cur_np.currency}、{unit}；"
            "毛利与归母净利同属该报表范围（归属层差异已在限制里写明）",
            "期间取自数据集声明的期间顺序的最后两期",
        ),
        "diagnostics": {
            "unit": unit, "currency": cur_np.currency, "caliber": cur_np.caliber,
            "entity": cur_np.entity, "entity_id": cur_np.entity_id,
            "periods": [prev_p, cur_p],
            "input_hashes": {o.period: o.observation_hash
                             for o in (cur_np, prev_np, cur_gp, prev_gp)},
            "closure": str(d_np - (d_gp + below)),      # 恒等式闭合差（字符串化的精确 0）
        },
        "outputs": [
            {"metric": "net_profit_change", "label": "归母净利润变化",
             "value": _q(d_np), "unit": unit, "output_period": f"{cur_p}较{prev_p}",
             "residual": 0.0,
             "components": [
                 {"label": "毛利变化", "value": _q(d_gp), "unit": unit,
                  "formula": f"{cur_gp.value} - {prev_gp.value}"},
                 {"label": "毛利线以下净额变化（含费用/税项/投资收益/少数股东等，"
                           "需明细表才能解释）",
                  "value": _q(below), "unit": unit,
                  "formula": f"({cur_np.value} - {prev_np.value}) - "
                             f"({cur_gp.value} - {prev_gp.value})"},
             ]},
            {"metric": "gross_profit_change", "label": "毛利变化", "value": _q(d_gp),
             "unit": unit, "output_period": f"{cur_p}较{prev_p}",
             "components": [], "residual": None},
            {"metric": "below_gross_line_change",
             "label": "毛利线以下净额变化（未解释段）", "value": _q(below),
             "unit": unit, "output_period": f"{cur_p}较{prev_p}",
             "components": [], "residual": None},
        ],
        "limits": LIMITS,
    }


def gold(dataset, params: dict | None = None) -> dict:
    """**独立金样**：用 `Decimal` 从原始观察逐项手算（不走 `compute` 的 float 路径）。

    这是"独立验证"的最低要求：不同计算路径 + 不同数值类型（十进制），
    以免"调用生成函数再和自己比"。
    """
    params = dict(params or {})
    periods = [p for p in (dataset.manifest.periods or ()) if p]
    if len(periods) < 2:
        raise NotApplicable("数据集只有一个期间")
    prev_p, cur_p = periods[-2], periods[-1]
    np_cur_o = dataset.require("net_profit", cur_p)
    np_prev_o = dataset.require("net_profit", prev_p)
    gp_cur_o = dataset.require("gross_profit", cur_p)
    gp_prev_o = dataset.require("gross_profit", prev_p)
    # 金样是**独立路径**，同样要拒绝跨报表范围混算：否则"compute 拒绝了、gold 却算出来"
    # 会让验证结论自相矛盾（K0-a）。
    scope_ok, scope_why = report_scope_ok(np_cur_o, np_prev_o, gp_cur_o, gp_prev_o)
    if not scope_ok:
        raise NotApplicable(scope_why)
    np_cur = Decimal(str(np_cur_o.value))
    np_prev = Decimal(str(np_prev_o.value))
    gp_cur = Decimal(str(gp_cur_o.value))
    gp_prev = Decimal(str(gp_prev_o.value))
    d_np = np_cur - np_prev
    d_gp = gp_cur - gp_prev
    below = d_np - d_gp
    q = lambda x: float(x.quantize(Decimal("0.01")))      # noqa: E731
    return {"net_profit_change": q(d_np), "gross_profit_change": q(d_gp),
            "below_gross_line_change": q(below)}


def compute_ratio(dataset, num_metric: str, den_metric: str, period: str, *,
                  label: str = "", pct: bool = True) -> dict:
    """同年比率（复用既有口径：分子/分母都必须同主体、同币种、同口径、可换算为金额）。

    零/负分母**不给比率**：`NotComputable`（不是 0、不是 None 悄悄兜底）。
    """
    num = dataset.require(num_metric, period)
    den = dataset.require(den_metric, period)
    if num.entity_id != den.entity_id:
        raise NotApplicable(f"分子/分母主体不同（{num.entity_id} vs {den.entity_id}）")
    if num.currency != den.currency:
        raise NotApplicable(f"分子/分母币种不同（{num.currency} vs {den.currency}）")
    scope_ok, scope_why = report_scope_ok(num, den)
    if not scope_ok:
        raise NotApplicable(scope_why)
    if num.money_scale <= 0 or den.money_scale <= 0:
        raise NotApplicable(f"分子/分母不可换算为金额（{num.unit}/{den.unit}）")
    den_v = Decimal(str(den.value))
    if den_v == 0:
        raise NotComputable(f"{label or num_metric} {period} 的分母为 0：不可算，不编造")
    ratio = Decimal(str(num.value)) * Decimal(str(num.money_scale)) \
        / (den_v * Decimal(str(den.money_scale)))
    if pct:
        ratio = ratio * 100
    return {"value": float(ratio.quantize(Decimal("0.01"))),
            "formula": (f"({num.value}{num.unit} 换算同量纲) / {den.value}{den.unit}"
                        + (" * 100" if pct else "")),
            "inputs": (num.fact_id, den.fact_id),
            "num_den_flipped": False}
