# -*- coding: utf-8 -*-
"""条件情景 / 敏感性：在明确假设下利润或现金如何变化、哪种假设最敏感。

纪律（架构 §5.5）：

- 假设**由使用者设定**，不是事实：`assumptions` 里逐条写出来源（本次为"用户设定"），
  未披露参数不得自动充作事实；
- **基准情景必须复现基期**（参数全 0 → 与基期读数一致，差额为 0）——这是"情景不是编数"的
  可核对条件；
- 无校准分布时**不显示概率、不显示预测置信区间**，措辞是"假设成立时"；
- 极值与单因素方向通过独立检查（`scenario_direction`）。
"""
from __future__ import annotations

from decimal import Decimal

from ..contracts import (
    InputRequirement, ModelSpec, NotApplicable, NotComputable, OutputSpec,
    full_identity_ok,
)

IMPL_VERSION = "scenario/1.0.0"

LIMITS = (
    "情景是“**假设成立时**”的条件推算：不显示发生概率，也不显示预测置信区间",
    "假设由使用者设定并逐条标注来源；未披露的参数不得当成事实",
    "基准情景必须复现基期（参数全 0 时与基期一致）——不一致说明模型或输入有问题",
    "只做单因素与已声明组合的敏感性，不做分布假设下的随机模拟",
    "反向阈值（维持基期利润所需毛利率）是**单因素反推**：给出“需要什么”，"
    "不表示该水平可达，也不含实现路径与时间",
    "回款天数只作**单项敏感性**（收入/365×Δ天）：不等同经营现金流预测，也不替代现金调节表",
)

SPEC = ModelSpec(
    model_id="scenario_sensitivity",
    version="1.0.0",
    family="条件情景/敏感性",
    question="在哪些假设下利润或现金承压、哪种假设最敏感",
    operator="scenario_v1",
    inputs=(
        InputRequirement("revenue_base", "revenue", period_offset=0),
        InputRequirement("gross_profit_base", "gross_profit", period_offset=0),
        InputRequirement("net_profit_base", "net_profit", period_offset=0),
    ),
    outputs=(
        OutputSpec("scenario_net_profit", "情景归母净利润（假设成立时）", kind="amount",
                   structure="scenarios"),
        OutputSpec("scenario_sensitivity", "单因素敏感度（每 +1 个百分点的影响）",
                   kind="amount", structure="sensitivity"),
        # U2 反向情景（2026-10-01）：从"调参数"变成"**改变结论需要什么**"。
        OutputSpec("margin_threshold_to_hold_base_profit",
                   "维持基期归母净利所需毛利率（给定假设收入）", kind="pct"),
        OutputSpec("margin_gap_to_threshold_pp",
                   "所需毛利率与基期毛利率之差（百分点）", kind="pct"),
        OutputSpec("collection_days_capital_per_day",
                   "回款天数敏感性：假设收入下每 1 天的资金占用", kind="amount"),
        OutputSpec("collection_days_sensitivity_10d",
                   "回款天数敏感性：±10 天的资金占用（单项，非现金流预测）", kind="amount"),
    ),
    # 参数界限写进契约：超出范围直接判失败，不允许"随手放大假设"
    allowed_params={"revenue_growth": (-0.5, 0.5), "gross_margin_delta": (-0.3, 0.3),
                    "expense_change_ratio": (-0.5, 0.5), "rounding": ("yi_2", "yuan_2")},
    # 参数不给时**实际用的值**（与 `_assumptions` 的默认分支同源）：显式声明，
    # 页面直接显示默认值，不让读者猜"不改会用什么"（L0-b-5）。
    default_params={"revenue_growth": 0.05, "gross_margin_delta": 0.01,
                    "expense_change_ratio": 0.0},
    tolerance=0.005,
    # 注意：**不含** `identity`——本模型的首个输出的"分解项"是三种**并列情景**，
    # 不是金额分解，把它们加总没有意义；它的同一性由 `base_reproduction` 与
    # `scenario_direction` 两条守住。
    validations=("gold", "unit", "base_reproduction", "scenario_direction"),
    budget={"steps": 1, "seconds": 5},
    limits=LIMITS,
    question_types=("scenario",),
    component_ids={"scenario_net_profit": ("base", "user", "counter"),
                   "scenario_sensitivity": ("revenue_p1pp", "gross_margin_p1pp",
                                            "expense_m1pp")},
)


def _d(v) -> Decimal:
    return Decimal(str(v))


def _bounds_ok(params: dict) -> str:
    for k, bounds in (("revenue_growth", (-0.5, 0.5)),
                      ("gross_margin_delta", (-0.3, 0.3)),
                      ("expense_change_ratio", (-0.5, 0.5))):
        if k not in params:
            continue
        try:
            v = float(params[k])
        except (TypeError, ValueError):
            return f"参数 {k} 不是数：{params[k]!r}"
        lo, hi = bounds
        if not (lo <= v <= hi):
            return f"参数 {k}={v} 超出允许范围 [{lo}, {hi}]"
    return ""


def _scenario(base: dict, *, growth: float, margin_delta: float,
              expense_ratio: float) -> Decimal:
    """一个情景：收入按 growth 变、毛利率按 margin_delta 变、费用按 expense_ratio 变。

    只做**已声明**的三项假设，不引入分布、不引入未声明项：
    归母净利 ≈ 收入' × 毛利率' − 费用'（费用=基期"收入−毛利−净利"的隐含块）。
    """
    rev = _d(base["revenue"]) * (1 + _d(growth))
    gp_base = _d(base["gross_profit"])
    margin_base = (gp_base / _d(base["revenue"])) if _d(base["revenue"]) else Decimal(0)
    margin = margin_base + _d(margin_delta)
    gp = rev * margin
    # 毛利线以下的隐含块 = 毛利 − 归母净利（由基期读数直接定义，不是"收入−毛利−净利"）。
    # 只有这样定义，参数全 0 时情景才**恰好**复现基期（基准复现差额 0）。
    implied_below_gross = gp_base - _d(base["net_profit"])
    expense = implied_below_gross * (1 + _d(expense_ratio))
    return gp - expense


def threshold_plan(base: dict, *, growth: float, expense_ratio: float = 0.0) -> dict:
    """反向情景的**确定性**反推（U2）：给定假设收入，维持基期归母净利需要多少毛利率。

    `NP1 = R1 × m1 − B0'`，其中 `B0' = 基期(毛利 − 归母净利) × (1+e)` 是**毛利线以下净额**
    （含费用、税项、投资收益、少数股东等，**不能叫纯费用**）。令 `NP1 = 基期归母净利` 得
    `m* = (基期归母净利 + B0') / R1`；与基期毛利率之差即"毛利率余量/缺口"（百分点）。
    另给有边界的回款天数单项敏感性：`收入'/365` 为每一天的资金占用。
    """
    rev_base = _d(base["revenue"])
    rev1 = rev_base * (1 + _d(growth))
    block = (_d(base["gross_profit"]) - _d(base["net_profit"])) * (1 + _d(expense_ratio))
    np_target = _d(base["net_profit"])
    margin_base = (_d(base["gross_profit"]) / rev_base) if rev_base else Decimal(0)
    m_star = ((np_target + block) / rev1) if rev1 else None
    per_day = rev1 / Decimal(365)
    return {"revenue_assumed": rev1, "block": block, "np_target": np_target,
            "margin_base": margin_base, "margin_threshold": m_star,
            "margin_gap_pp": ((m_star - margin_base) * 100) if m_star is not None else None,
            "per_day_capital": per_day,
            "formula": ("m* = (基期归母净利 + 毛利线以下净额×(1+e)) / (基期收入×(1+g))；"
                        "回款敏感性 = 收入'/365 × Δ天数（单项）")}


def compute(dataset, params: dict | None = None) -> dict:
    params = dict(params or {})
    bad = _bounds_ok(params)
    if bad:
        raise NotApplicable(bad)
    period = dataset.period_at(0)
    rev = dataset.require("revenue", period)
    gp = dataset.require("gross_profit", period)
    np_ = dataset.require("net_profit", period)
    # 完整身份（L0-a）：主体/币种/报表范围/金额量纲一次核完；本模型内部要做
    # `gp - np` 与 `gp / rev`，所以三个输入还必须**同一量纲**（same_scale）。
    ident_ok, ident_why = full_identity_ok(rev, gp, np_, same_scale=True)
    if not ident_ok:
        raise NotApplicable(ident_why)
    if _d(rev.value) <= 0:
        raise NotComputable(f"基期收入非正（{rev.value}{rev.unit}）：比率型情景不适用")
    base = {"revenue": float(rev.value), "gross_profit": float(gp.value),
            "net_profit": float(np_.value), "margin": float(_d(gp.value) / _d(rev.value))}
    if base["margin"] <= 0:
        # 基期毛利率非正 ⇒ "收入增长/毛利率改善 ⇒ 利润改善"这个**方向假设**不成立
        # （毛利率为负时，收入越大毛利越负）。实机反例：京蓝 2020（毛利率 −0.78%、
        # 归母净利 −23.55 亿），情景模型算出"上行情景更差"，被 `scenario_direction`
        # 独立验证判失败——验证没错，是**这个模型对亏损公司不适用**。
        # 所以在这里就如实判 `not_applicable`，而不是让它以一个"验证失败"的样子出门：
        # 读者要能一眼分清"模型跑错了"与"模型不适用"。
        raise NotApplicable(
            f"基期毛利率为负（{base['margin']:.2%}）：增长/毛利率改善⇒利润改善的方向假设"
            "不成立，情景模型对亏损期不适用（需要亏损专用的方向判据，属研究侧决定）")
    unit = str(np_.unit or "")
    q = lambda x: float(Decimal(str(x)).quantize(Decimal("0.01")))          # noqa: E731

    sc_base = _scenario(base, growth=0.0, margin_delta=0.0, expense_ratio=0.0)
    base_gap = sc_base - _d(np_.value)
    # 假设参数：**契约里声明的名字优先**（`revenue_growth`/`gross_margin_delta`/
    # `expense_change_ratio`），旧名 `up_*`/`down_*` 继续兼容。此前 `allowed_params`
    # 声明的是前者、`compute` 只读后者——页面按声明改假设时**改了不生效**（K3 实机）。
    _g_up, _m_up, _e_up, _g_down, _m_down = _assumptions(params)
    # 三种情景：基准（参数 0）/ 使用者情景（未给就用声明过的默认值）/ 反向对照
    up = _scenario(base, growth=_g_up, margin_delta=_m_up, expense_ratio=_e_up)
    down = _scenario(base, growth=_g_down, margin_delta=_m_down, expense_ratio=0.0)
    # **数值与解释同一份参数**（L0-c，2026-09-30 复核 M1）：标签/公式不再写死
    # "+5%收入/+1pp毛利率"，而是从上面解析出的同一组数生成；百分比（%）与百分点（pp）
    # 分开写；使用者给的是负增速时不固定称"上行"；"毛利线以下"不冒称纯费用。
    up_label, up_formula = _scenario_label("使用者情景", _g_up, _m_up, _e_up)
    down_label, down_formula = _scenario_label("反向对照", _g_down, _m_down, 0.0)
    # 单因素敏感度：每 +1 个百分点的影响（收入 / 毛利率 / 费用）
    sens = {
        "收入 +1pp": _scenario(base, growth=0.01, margin_delta=0, expense_ratio=0) - sc_base,
        "毛利率 +1pp": _scenario(base, growth=0, margin_delta=0.01, expense_ratio=0) - sc_base,
        "费用 -1pp": _scenario(base, growth=0, margin_delta=0, expense_ratio=-0.01) - sc_base,
    }
    ranked = sorted(sens.items(), key=lambda kv: abs(kv[1]), reverse=True)
    direction_ok = bool(
        _scenario(base, growth=0.05, margin_delta=0, expense_ratio=0)
        > _scenario(base, growth=0.0, margin_delta=0, expense_ratio=0)
        and _scenario(base, growth=0.0, margin_delta=-0.01, expense_ratio=0)
        < _scenario(base, growth=0.0, margin_delta=0, expense_ratio=0))
    _assumed = [k for k in ("revenue_growth", "gross_margin_delta", "expense_change_ratio")
                if k in params]
    _defaults = [f"使用者情景：{up_label}（{'使用者提供' if _assumed else '未提供，用声明默认值'}）",
                 f"反向对照：{down_label}"]
    # 反向阈值按**使用者情景的收入假设**求解（e 与使用者情景一致）：回答"在这个收入下，
    # 毛利率至少要多少才能维持基期利润"。基准（g=0）时阈值恰好等于基期毛利率。
    th = threshold_plan(base, growth=_g_up, expense_ratio=_e_up)
    return {
        "periods": (period,),
        "formula": ("情景归母净利 = 收入×(1+g) × (基期毛利率+m) − 毛利线以下隐含块×(1+e)；"
                    "毛利线以下隐含块 = 基期(毛利 − 归母净利)，含费用/税项/投资收益/"
                    "少数股东等，不是纯费用项"),
        "inputs": (rev.fact_id, gp.fact_id, np_.fact_id),
        "assumptions": tuple(
            [f"基期读数取自 {period}（{rev.value}/{gp.value}/{np_.value}{unit}）",
             "收入增速 g（%）、毛利率变化 m（**百分点 pp**）、隐含块变化 e 均为"
             "**使用者设定**的假设（非披露事实）"
             + ("" if _assumed else "：本次未提供，使用模型中已声明的默认值")]
            + [f"{k}={v}（使用者提供）" for k, v in sorted(params.items()) if k in
               ("revenue_growth", "gross_margin_delta", "expense_change_ratio")]
            + _defaults),
        "diagnostics": {
            "unit": unit, "currency": np_.currency, "caliber": np_.caliber,
            "entity": np_.entity, "entity_id": np_.entity_id, "periods": [period],
            "input_hashes": {o.metric: o.observation_hash for o in (rev, gp, np_)},
            "base": base,
            "base_reproduction_gap": ("0" if base_gap == 0 else str(base_gap)),
            "direction_ok": direction_ok,
            "closure": "0",
            "no_probability": "无校准分布：不显示发生概率，也不显示预测置信区间",
            "thresholds": {
                "assumed_revenue": float(th["revenue_assumed"]),
                "below_gross_block": float(th["block"]),
                "net_profit_target": float(th["np_target"]),
                "margin_base": float(th["margin_base"]),
                "margin_threshold": (float(th["margin_threshold"])
                                     if th["margin_threshold"] is not None else None),
                "margin_gap_pp": (float(th["margin_gap_pp"])
                                  if th["margin_gap_pp"] is not None else None),
                "capital_per_day": float(th["per_day_capital"]),
                "formula": th["formula"],
                "caveats": ("阈值是单因素反推：给出“需要什么”，不表示可达；"
                            "毛利线以下净额含费用/税项/投资收益/少数股东，不是纯费用；"
                            "回款天数只作单项敏感性，不等同经营现金流预测"),
            },
        },
        "outputs": [
            {"metric": "scenario_net_profit", "label": "情景归母净利润（假设成立时）",
             "value": q(up), "unit": unit, "output_period": f"{period}（{up_label}）",
             "residual": 0.0,
             "components": [
                 {"component_id": "base",
                  "label": f"基准（参数 0，复现基期 {np_.value}{unit}）",
                  "value": q(sc_base), "unit": unit, "formula": "g=0, m=0, e=0"},
                 {"component_id": "user", "label": up_label, "value": q(up),
                  "unit": unit, "formula": up_formula},
                 {"component_id": "counter", "label": down_label, "value": q(down),
                  "unit": unit, "formula": down_formula},
             ]},
            {"metric": "scenario_sensitivity", "label": "单因素敏感度（每 +1 个百分点）",
             "value": q(ranked[0][1]), "unit": unit,
             "output_period": f"{period}（最敏感：{ranked[0][0]}）",
             "residual": None,
             "components": [{"component_id": _SENS_ID[k], "label": k, "value": q(v),
                             "unit": unit, "formula": "单因素 +1pp"}
                            for k, v in ranked]},
            # —— 反向情景：改变结论需要什么（U2）——
            {"metric": "margin_threshold_to_hold_base_profit",
             "label": "维持基期归母净利所需毛利率（给定假设收入）",
             "value": (q(th["margin_threshold"] * 100)
                       if th["margin_threshold"] is not None else None),
             "unit": "%", "output_period": f"{period}（{up_label}）",
             "components": [], "residual": None},
            {"metric": "margin_gap_to_threshold_pp",
             "label": "所需毛利率与基期毛利率之差（百分点）",
             "value": (q(th["margin_gap_pp"]) if th["margin_gap_pp"] is not None else None),
             "unit": "%（pp）", "output_period": f"{period}（{up_label}）",
             "components": [], "residual": None},
            {"metric": "collection_days_capital_per_day",
             "label": "回款天数敏感性：每 1 天的资金占用",
             "value": q(th["per_day_capital"]), "unit": unit,
             "output_period": f"{period}（{up_label}）", "components": [], "residual": None},
            {"metric": "collection_days_sensitivity_10d",
             "label": "回款天数敏感性：±10 天的资金占用（单项，非现金流预测）",
             "value": q(th["per_day_capital"] * 10), "unit": unit,
             "output_period": f"{period}（{up_label}）", "components": [], "residual": None},
        ],
        "limits": LIMITS,
    }


# 单因素敏感度的**稳定 component_id**（R1-b）：标签是显示文本，id 是契约身份。
# 中文键 → 稳定 id，避免"改文案就换身份"或"换文案却撞上同一 id"。
_SENS_ID = {"收入 +1pp": "revenue_p1pp", "毛利率 +1pp": "gross_margin_p1pp",
            "费用 -1pp": "expense_m1pp"}


def _scenario_label(name: str, growth: float, margin_delta: float,
                    expense_ratio: float) -> tuple[str, str]:
    """情景的**显示标签与公式**，从同一组已解析参数生成（L0-c）。

    - 百分比（收入增速、隐含块变化）写 `%`，毛利率变化是**百分点**写 `pp`，两者不混；
    - 使用者给的是负增速时不叫"上行"——`name` 由调用方给（使用者情景 / 反向对照），
      方向词只作为括号里的附注，且按实际正负写"较基期高/低"；
    - 公式串写**实际使用的数值**，不再固定 `g=+0.05, m=+0.01`（复核 M1：数值变了标签没变）。
    """
    tone = "较基期高" if (growth > 0 or margin_delta > 0 or expense_ratio > 0) else (
        "较基期低" if (growth < 0 or margin_delta < 0 or expense_ratio < 0) else "等于基期")
    # `gross_margin_delta` 是**分数**（0.02 = 2 个百分点），显示时换算成 pp，别写成 0.02pp
    label = (f"{name}（收入 {growth:+.2%}／毛利率 {margin_delta * 100:+.2f}pp／"
             f"隐含块 {expense_ratio:+.2%}，{tone}）")
    formula = (f"g={growth:+.4f}（{growth:+.2%}）, m={margin_delta:+.4f}"
               f"（{margin_delta * 100:+.2f}pp）, e={expense_ratio:+.4f}"
               f"（{expense_ratio:+.2%}）")
    return label, formula


def _assumptions(params: dict) -> tuple[float, float, float, float, float]:
    """参数 → `(上行收入增速, 上行毛利率变化, 上行费用变化, 下行收入, 下行毛利率)`。

    **compute 与 gold 必须共用这一处解析**（K3 实机：两边各读一套名字，页面按声明改假设后
    compute 用新值、gold 用旧默认值 → 独立验证判 `validation_failed`，模型"改了就报错"）。
    声明名优先（`revenue_growth`/`gross_margin_delta`/`expense_change_ratio`），
    旧名 `up_*`/`down_*` 兼容。
    """
    p = dict(params or {})
    g_up = float(p.get("revenue_growth", p.get("up_growth", 0.05)))
    m_up = float(p.get("gross_margin_delta", p.get("up_margin", 0.01)))
    e_up = float(p.get("expense_change_ratio", 0.0))
    g_down = float(p.get("down_growth", -abs(g_up) if g_up else -0.05))
    m_down = float(p.get("down_margin", -abs(m_up) if m_up else -0.01))
    return g_up, m_up, e_up, g_down, m_down


def components_gold(dataset, params: dict | None = None) -> dict:
    """**分项**的独立计算（R1-b）：基准/使用者情景/反向对照与三个单因素敏感度逐个重算。

    与 `gold` 同一纪律：独立路径、Decimal、同一条完整身份判据。**基准分项直接对数据集的
    真实基期读数**（`_scenario(..., 0,0,0)`），不信载荷自报的 `base_reproduction_gap=0`
    或 `direction_ok`——那条反例正是"基准 +10 / 使用者情景 −10，合计不变、旧验证全过"。
    """
    params = dict(params or {})
    period = dataset.period_at(0)
    rev = dataset.require("revenue", period)
    gp = dataset.require("gross_profit", period)
    np_ = dataset.require("net_profit", period)
    ident_ok, ident_why = full_identity_ok(rev, gp, np_, same_scale=True)
    if not ident_ok:
        raise NotApplicable(ident_why)
    if float(rev.value) <= 0:
        raise NotComputable(f"基期收入非正（{rev.value}{rev.unit}）：比率型情景不适用")
    base = {"revenue": float(rev.value), "gross_profit": float(gp.value),
            "net_profit": float(np_.value),
            "margin": float(_d(gp.value) / _d(rev.value))}
    if base["margin"] <= 0:
        raise NotApplicable("基期毛利率为负：增长/毛利率改善⇒利润改善的方向假设不成立")
    q = lambda x: float(Decimal(str(x)).quantize(Decimal("0.01")))          # noqa: E731
    unit = str(np_.unit or "")
    sc_base = _scenario(base, growth=0.0, margin_delta=0.0, expense_ratio=0.0)
    g_up, m_up, e_up, g_down, m_down = _assumptions(params)
    up = _scenario(base, growth=g_up, margin_delta=m_up, expense_ratio=e_up)
    down = _scenario(base, growth=g_down, margin_delta=m_down, expense_ratio=0.0)
    sens = {
        "revenue_p1pp": _scenario(base, growth=0.01, margin_delta=0,
                                  expense_ratio=0) - sc_base,
        "gross_margin_p1pp": _scenario(base, growth=0, margin_delta=0.01,
                                       expense_ratio=0) - sc_base,
        "expense_m1pp": _scenario(base, growth=0, margin_delta=0,
                                  expense_ratio=-0.01) - sc_base,
    }
    return {
        "scenario_net_profit": {"base": (q(sc_base), unit), "user": (q(up), unit),
                                "counter": (q(down), unit)},
        "scenario_sensitivity": {k: (q(v), unit) for k, v in sens.items()},
    }


def gold(dataset, params: dict | None = None) -> dict:
    """独立金样：基准复现必须等于基期净利（差 0）；上行情景与反向阈值用 Decimal 重算。"""
    params = dict(params or {})
    period = dataset.period_at(0)
    rev = dataset.require("revenue", period)
    gp = dataset.require("gross_profit", period)
    np_ = dataset.require("net_profit", period)
    # 独立路径同样核完整身份（L0-a）：与算子入口同一判据，不让"compute 拒绝、gold 照算"。
    ident_ok, ident_why = full_identity_ok(rev, gp, np_, same_scale=True)
    if not ident_ok:
        raise NotApplicable(ident_why)
    base = {"revenue": float(rev.value), "gross_profit": float(gp.value),
            "net_profit": float(np_.value)}
    g_up, m_up, e_up, _g_down, _m_down = _assumptions(params)
    up = _scenario(base, growth=g_up, margin_delta=m_up, expense_ratio=e_up)
    th = threshold_plan(base, growth=g_up, expense_ratio=e_up)
    out = {"scenario_net_profit": float(up.quantize(Decimal("0.01")))}
    if th["margin_threshold"] is not None:
        out["margin_threshold_to_hold_base_profit"] = float(
            (th["margin_threshold"] * 100).quantize(Decimal("0.01")))
        out["margin_gap_to_threshold_pp"] = float(
            th["margin_gap_pp"].quantize(Decimal("0.01")))
    out["collection_days_capital_per_day"] = float(
        th["per_day_capital"].quantize(Decimal("0.01")))
    out["collection_days_sensitivity_10d"] = float(
        (th["per_day_capital"] * 10).quantize(Decimal("0.01")))
    return out
