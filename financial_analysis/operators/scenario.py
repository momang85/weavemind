# -*- coding: utf-8 -*-
"""条件情景 / 敏感性：在明确假设下利润或现金如何变化、哪种假设最敏感。

纪律（架构 §5.5，阶段W W0 收紧）：

- 假设**由使用者设定**，不是事实：`assumptions` 里逐条写出来源（本次为"用户设定"），
  未披露参数不得自动充作事实；
- **基准情景必须复现基期**（假设回到基期 → 与基期读数一致，差额为 0）——这是"情景不是编数"的
  可核对条件；
- 无校准分布时**不显示概率、不显示预测置信区间**，措辞是"假设成立时"；
- 极值与单因素方向通过独立检查（`scenario_direction`）。

阶段W W0 的三处经济修正（2026-10-01 架构复核：detail 只是展示、假设被残差吸收、两个阈值目标不同）：

1. **项目按披露符号影响利润**：毛利线以下逐项用 `operating_drivers.LINE_ITEMS` 的
   `+1 增利 / −1 减利` 符号计入；洋河明细齐备时基期残差为 0（旧实现按绝对值相加，留下 3.02 亿元假残差）。
2. **基期残差只算一次、随后冻结**：`残差 = 基期归母净利 − [基期毛利 + Σ(符号×已披露项)]`。
   旧实现每次用"总块 − 改变后的明细"重算残差，于是用户改税率/费用/少数股东比例时，
   残差跟着变、把假设吸收掉（反例：税率 20%→25%、少数股东→10%，利润仍 300、只有残差 100→68）。
3. **正算与两个阈值共用同一求值器**：`evaluate()` 是唯一入口（fixed/detail 两种模式）；
   收入阈值与毛利率阈值用**同一目标、同一模式、同一组假设**（旧实现一个用基期利润、
   一个用使用者目标），detail 模式在允许范围内用同一个求值器求根，超范围如实标不可达。
"""
from __future__ import annotations

from decimal import Decimal

from ..contracts import (
    InputRequirement, ModelSpec, NotApplicable, NotComputable, OutputSpec,
    full_identity_ok,
)

IMPL_VERSION = "scenario/2.0.0"

# 单因素反推的**允许参数范围**（与 `allowed_params` 同源）：detail 模式在此范围内求根，
# 超范围不硬报一个数，而是如实标"不可达"。
GROWTH_BOUNDS = (-0.5, 0.5)
MARGIN_BOUNDS = (-0.3, 0.3)

LIMITS = (
    "情景是“**假设成立时**”的条件推算：不显示发生概率，也不显示预测置信区间",
    "假设由使用者设定并逐条标注来源；未披露的参数不得当成事实",
    "基准情景必须复现基期（假设回到基期时与基期一致）——不一致说明模型或输入有问题",
    "只做单因素与已声明组合的敏感性，不做分布假设下的随机模拟",
    "反向阈值是**单因素反推**：收入侧固定毛利率=基期、毛利率侧固定收入=基期，"
    "两把杠杆共用**同一目标归母净利**（默认取上一期归母净利，回到上年水平；没有上一期就用基期，"
    "此时阈值为 0，可当自检）；给出“需要什么”，不表示该水平可达，也不含实现路径与时间",
    "回款天数只作**单项敏感性**（假设收入/365×Δ天）：不等同经营现金流预测，也不替代现金调节表",
    # W0：明细模式的三条规则与残差纪律
    "明细模式（`below_gross_mode=detail`）对**已披露**的毛利线以下项目只用三种规则："
    "固定金额（可随 `expense_change_ratio` 调整）／随收入变化（`revenue_linked`："
    "税金及附加、销售费用）／单独假设（`tax_rate` 作用于**情景税前利润**、"
    "`minority_share` 作用于**情景合并净利**）；项目按披露符号计入（正号增利、负号减利）",
    "**没有明细的部分保留为基期残差**：只算一次并冻结，不随假设变化补平目标，"
    "也不硬算完整预测或生成「正常化利润」",
    "`fixed` 模式把毛利线以下净额整体当作一块：`tax_rate`/`minority_share` 在该模式下不参与，"
    "单独假设请用 `detail` 模式（给了会被如实拒绝，不静默忽略）",
)

SPEC = ModelSpec(
    model_id="scenario_sensitivity",
    version="2.0.0",
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
        # W0：与收入侧阈值共用同一目标（`target_net_profit`），单因素（收入固定基期）。
        OutputSpec("margin_threshold_to_hold_base_profit",
                   "维持目标归母净利所需毛利率（单因素；收入固定基期）", kind="pct"),
        OutputSpec("margin_gap_to_threshold_pp",
                   "所需毛利率与基期毛利率之差（百分点）", kind="pct"),
        OutputSpec("collection_days_capital_per_day",
                   "回款天数敏感性：假设收入下每 1 天的资金占用", kind="amount"),
        OutputSpec("collection_days_sensitivity_10d",
                   "回款天数敏感性：±10 天的资金占用（单项，非现金流预测）", kind="amount"),
        # V2（阶段V）：**明细模式**——毛利线以下净额按"固定金额／随收入变化／单独假设"
        # 三种规则逐项给出，没有明细的部分保留残差；不是完整预测。
        # W0：逐项按披露符号计入，总量 = 对归母净利的净影响。
        OutputSpec("scenario_below_gross_detail",
                   "毛利线以下净额（对归母净利的影响；明细模式）", kind="amount",
                   structure="bridge"),
        # V2（阶段V）W0：**收入侧反推**——与毛利率阈值同一目标、同一模式、同一组假设。
        OutputSpec("revenue_growth_to_hold_target",
                   "维持目标归母净利所需收入变化（单因素；毛利率固定基期）", kind="pct"),
    ),
    # 参数界限写进契约：超出范围直接判失败，不允许"随手放大假设"
    allowed_params={"revenue_growth": GROWTH_BOUNDS, "gross_margin_delta": MARGIN_BOUNDS,
                    "expense_change_ratio": (-0.5, 0.5), "rounding": ("yi_2", "yuan_2"),
                    # V2（阶段V）明细模式：模式开关 + 两个**单独假设**
                    # （`tax_rate` 作用于情景税前利润、`minority_share` 作用于情景合并净利）
                    "below_gross_mode": ("fixed", "detail"),
                    "tax_rate": (0.0, 0.5), "minority_share": (0.0, 0.3),
                    # 目标归母净利（"要回到哪一年的利润水平"由使用者给定；不给=上一期，见下）
                    "target_net_profit": (0.0, 1e15)},
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
    for k, bounds in (("revenue_growth", GROWTH_BOUNDS),
                      ("gross_margin_delta", MARGIN_BOUNDS),
                      ("expense_change_ratio", (-0.5, 0.5)),
                      # V2（阶段V）：明细模式的两个**单独假设**与利润目标
                      ("tax_rate", (0.0, 0.5)),
                      ("minority_share", (0.0, 0.3)),
                      ("target_net_profit", (0.0, 1e15))):
        if k not in params or params.get(k) in (None, ""):
            continue
        try:
            v = float(params[k])
        except (TypeError, ValueError):
            return f"参数 {k} 不是数：{params[k]!r}"
        lo, hi = bounds
        if not (lo <= v <= hi):
            return f"参数 {k}={v} 超出允许范围 [{lo}, {hi}]"
    mode = str(params.get("below_gross_mode") or "fixed")
    if mode not in ("fixed", "detail"):
        return f"参数 below_gross_mode={mode!r} 非法（可选 fixed/detail）"
    # W0：fixed 模式把毛利线以下净额整体当作一块，税率/少数股东无处着力——
    # 如实拒绝，而不是收下参数却算出一个不受它影响的数（旧反例正是"改了没变化"）。
    if mode == "fixed":
        for k in ("tax_rate", "minority_share"):
            if params.get(k) not in (None, ""):
                return (f"参数 {k} 只在 below_gross_mode=detail 下有效："
                        "fixed 模式的毛利线以下净额整体固定，"
                        "要单独假设税率/少数股东请改用 detail 模式")
    return ""


# W0：明细模式下**随收入变化**的已披露项目（其它已披露项按固定金额处理）。
REVENUE_LINKED_ITEMS: tuple = ("taxes_and_surcharges", "selling_expense")
# 毛利线以下的两个**单独假设槽位**（所得税作用于情景税前利润；少数股东占比作用于情景合并净利）
EXPLICIT_SLUGS: tuple = ("income_tax_expense", "minority_interest")


def base_context(dataset, period: str) -> dict:
    """基期上下文（W0 统一求值器的**唯一输入**）：基期读数、逐项披露值与**符号**、
    **冻结的基期残差**、基期税前利润/有效税率/少数股东占比。

    残差定义（只算这一次）：

        残差 = 基期归母净利 − [基期毛利 + Σ(符号 × 已披露的毛利线以下项目)]

    它代表"未取得明细的毛利线以下项目"的基期净额；此后**任何假设都不再改它**。
    明细齐备时它应为 0（旧实现留下 3.02 亿元，是因为把减利项按绝对值当加项）。
    """
    from .operating_drivers import LINE_ITEMS as _LINE_ITEMS
    rev_o = dataset.require("revenue", period)
    gp_o = dataset.require("gross_profit", period)
    np_o = dataset.require("net_profit", period)
    unit = str(np_o.unit or "")
    rev, gp, np_ = _d(rev_o.value), _d(gp_o.value), _d(np_o.value)
    items: list[dict] = []
    used: list[str] = []
    missing: list[str] = []
    for slug, label, sign in _LINE_ITEMS:
        obs = dataset.get(slug, period)
        if obs is None or obs.value is None:
            missing.append(slug)
            continue
        used.append(slug)
        items.append({"slug": slug, "label": label, "sign": int(sign),
                      "value": _d(obs.value)})
    by = {it["slug"]: it for it in items}
    tax0 = by["income_tax_expense"]["value"] if "income_tax_expense" in by else None
    min0 = by["minority_interest"]["value"] if "minority_interest" in by else None
    other_signed = sum((_d(it["sign"]) * it["value"] for it in items
                        if it["slug"] not in EXPLICIT_SLUGS), Decimal("0"))
    # 残差：把税/少数股东也一起减掉（缺披露时按 0 计入残差，不假装它们不存在）
    residual = np_ - gp - other_signed + (tax0 or Decimal("0")) + (min0 or Decimal("0"))
    pretax0 = gp + other_signed + residual          # = 归母净利 + 所得税 + 少数股东
    cons0 = pretax0 - (tax0 or Decimal("0"))        # = 归母净利 + 少数股东
    rate0 = ((tax0 / pretax0) if (tax0 is not None and pretax0 != 0) else None)
    share0 = ((min0 / cons0) if (min0 is not None and cons0 != 0) else None)
    cons_obs = dataset.get("net_profit_consolidated", period)
    cons_disc = _d(cons_obs.value) if (cons_obs is not None
                                       and cons_obs.value is not None) else None
    return {
        "period": period, "unit": unit,
        "revenue": rev, "gross_profit": gp, "net_profit": np_,
        "margin": (gp / rev) if rev else Decimal("0"),
        "items": items, "items_used": used, "items_missing": missing,
        "tax0": tax0, "min0": min0, "tax_rate0": rate0, "minority_share0": share0,
        "other_signed": other_signed, "residual": residual,
        "pretax0": pretax0, "consolidated0": cons0, "consolidated_disclosed": cons_disc,
        "block_signed": np_ - gp,                    # fixed 模式：毛利线以下净额（带符号）
    }


def _tax_of(ctx: dict, pretax: Decimal, given) -> Decimal:
    """情景所得税：给了假设就 `税前利润 × 假设税率`；否则 `税前利润 × 基期有效税率`。

    基期有效税率用**比例式**实现（`税前×基期所得税/基期税前`），这样基期处恰好等于
    基期所得税（Decimal 精确），基准复现不会被除法余数破坏。
    """
    if given is not None:
        return pretax * _d(given)
    tax0, pretax0 = ctx.get("tax0"), ctx.get("pretax0")
    if tax0 is None or not pretax0:
        return Decimal("0")                          # 缺披露：已并入残差
    return (pretax * tax0) / pretax0


def _minority_of(ctx: dict, consolidated: Decimal, given) -> Decimal:
    """情景少数股东损益：给了占比就 `情景合并净利 × 占比`；否则用基期占比。"""
    if given is not None:
        return consolidated * _d(given)
    min0, cons0 = ctx.get("min0"), ctx.get("consolidated0")
    if min0 is None or not cons0:
        return Decimal("0")
    return (consolidated * min0) / cons0


def evaluate(ctx: dict, *, growth=0.0, margin_delta=0.0, expense_ratio=0.0,
             mode: str = "fixed", tax_rate=None, minority_share=None) -> dict:
    """**唯一的情景求值器**（W0）：一组假设 → 情景归母净利（以及明细项）。

    - `fixed`：`归母净利 = 收入×(1+g)×(基期毛利率+m) + 基期(毛利−归母)×(1+e)`
      （毛利线以下净额整体一块；税率/少数股东不单独参与）。
    - `detail`：已披露项按"随收入变化／固定金额"逐项算（**按披露符号**计入），
      加上**冻结的基期残差**，再按税率算所得税、按占比算少数股东。
    """
    g, m, e = _d(growth), _d(margin_delta), _d(expense_ratio)
    rev1 = ctx["revenue"] * (1 + g)
    gross1 = rev1 * (ctx["margin"] + m)
    if mode != "detail":
        block1 = ctx["block_signed"] * (1 + e)
        return {"mode": "fixed", "revenue": rev1, "gross_profit": gross1,
                "below_gross": block1, "pretax": None, "tax": None,
                "consolidated": None, "minority": None,
                "net_profit": gross1 + block1, "items": []}
    scale = 1 + g
    other = Decimal("0")
    items: list[dict] = []
    for it in ctx["items"]:
        if it["slug"] in EXPLICIT_SLUGS:
            continue
        linked = it["slug"] in REVENUE_LINKED_ITEMS
        value = it["value"] * (scale if linked else (1 + e))
        signed = _d(it["sign"]) * value
        other += signed
        items.append({"component_id": f"item:{it['slug']}", "label": it["label"],
                      "value": signed, "disclosed_value": it["value"],
                      "sign": it["sign"],
                      "rule": ("revenue_linked" if linked else "fixed"),
                      "formula": ("基期披露值×收入变化" if linked
                                  else "基期披露值×(1+费用变化)"),
                      "unit": ctx["unit"]})
    pretax = gross1 + other + ctx["residual"]
    tax = _tax_of(ctx, pretax, tax_rate)
    consolidated = pretax - tax
    minority = _minority_of(ctx, consolidated, minority_share)
    items.append({"component_id": "income_tax_expense", "label": "所得税费用",
                  "value": -tax, "disclosed_value": ctx.get("tax0"),
                  "sign": -1,
                  "rule": ("explicit" if tax_rate is not None
                           else ("base_rate" if ctx.get("tax_rate0") is not None
                                 else "missing")),
                  "formula": ("情景税前利润×假设税率" if tax_rate is not None
                              else "情景税前利润×基期有效税率"),
                  "unit": ctx["unit"]})
    items.append({"component_id": "minority_interest", "label": "少数股东损益",
                  "value": -minority, "disclosed_value": ctx.get("min0"),
                  "sign": -1,
                  "rule": ("explicit" if minority_share is not None
                           else ("base_share" if ctx.get("minority_share0") is not None
                                 else "missing")),
                  "formula": ("情景合并净利×假设占比" if minority_share is not None
                              else "情景合并净利×基期占比"),
                  "unit": ctx["unit"]})
    items.append({"component_id": "residual_unlisted",
                  "label": "残差（未取得明细的毛利线以下项目）",
                  "value": ctx["residual"], "disclosed_value": ctx["residual"],
                  "sign": None, "rule": "residual",
                  "formula": "基期一次算定后**冻结**（不随假设变化）",
                  "unit": ctx["unit"]})
    return {"mode": "detail", "revenue": rev1, "gross_profit": gross1,
            "below_gross": other + ctx["residual"] - tax - minority,
            "pretax": pretax, "tax": tax, "consolidated": consolidated,
            "minority": minority, "net_profit": consolidated - minority,
            "items": items, "other_signed": other}


def _monotone(fn, lo: Decimal, hi: Decimal, points: int = 21) -> bool:
    """在 [lo, hi] 上抽点检查单调不减（detail 模式求根的前提；非单调就不硬求）。

    容差：用比例式还原基期税率/占比时，Decimal 除法会留下 ~1e-25 量级的舍入抖动，
    严格 `<` 会把这种抖动误判成"非单调"。这里只把**超过相对 1e-9** 的回落当作非单调。
    """
    prev = None
    span = hi - lo
    for i in range(points):
        x = lo + span * Decimal(i) / Decimal(points - 1)
        v = fn(x)
        if prev is not None:
            tol = abs(prev) * Decimal("1e-9") + Decimal("1e-6")
            if v < prev - tol:
                return False
        prev = v
    return True


def _root(fn, lo: Decimal, hi: Decimal) -> tuple:
    """单调不减函数在 [lo, hi] 上求 `fn(x)=0` 的根 → `(根 或 None, 是否可达, 说明)`。"""
    f_lo, f_hi = fn(lo), fn(hi)
    if f_lo > 0:
        return (None, False, f"下界 {lo} 处情景利润已高于目标：目标不可达")
    if f_hi < 0:
        return (None, False, f"上界 {hi} 处情景利润仍低于目标：目标不可达")
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = fn(mid)
        if f_mid == 0:
            return (mid, True, "二分求根（精确命中）")
        if f_mid > 0:
            hi = mid
        else:
            lo = mid
        if (hi - lo) < Decimal("1e-12"):
            break
    return ((lo + hi) / 2, True, "二分求根")


def _solve_margin(ctx: dict, *, target: Decimal, mode: str, expense_ratio: Decimal,
                  tax_rate, minority_share, revenue_growth: Decimal) -> tuple:
    """求"在该收入假设下、维持目标归母净利所需毛利率"。

    `revenue_growth=0` 是**单因素**（收入固定基期）；给非 0 值时是**两因素条件计算**，
    结果只作诊断，不与单因素阈值并列当同一把杠杆。
    """
    if mode != "detail":
        block = ctx["block_signed"] * (1 + expense_ratio)
        rev = ctx["revenue"] * (1 + revenue_growth)
        if not rev:
            return (None, False, "假设收入为 0：无法反推毛利率")
        return ((target - block) / rev, True, "代数反推")
    lo, hi = (_d(MARGIN_BOUNDS[0]), _d(MARGIN_BOUNDS[1]))
    fn = (lambda m: evaluate(ctx, growth=revenue_growth, margin_delta=m,
                             expense_ratio=expense_ratio, mode="detail",
                             tax_rate=tax_rate,
                             minority_share=minority_share)["net_profit"] - target)
    if not _monotone(fn, lo, hi):
        return (None, False, "情景利润在该范围内对毛利率非单调：不硬求根")
    root, ok, why = _root(fn, lo, hi)
    if not ok:
        return (None, False, why)
    return (ctx["margin"] + root, True, why + "（detail 模式：同一求值器）")


def _solve_growth(ctx: dict, *, target: Decimal, mode: str, expense_ratio: Decimal,
                  tax_rate, minority_share) -> tuple:
    """求"毛利率固定基期时、维持目标归母净利所需收入变化"（单因素）。"""
    if mode != "detail":
        block = ctx["block_signed"] * (1 + expense_ratio)
        gp0 = ctx["gross_profit"]
        if not gp0:
            return (None, False, "基期毛利为 0：无法反推收入")
        return ((target - block - gp0) / gp0, True, "代数反推")
    lo, hi = (_d(GROWTH_BOUNDS[0]), _d(GROWTH_BOUNDS[1]))
    fn = (lambda g: evaluate(ctx, growth=g, margin_delta=0,
                             expense_ratio=expense_ratio, mode="detail",
                             tax_rate=tax_rate,
                             minority_share=minority_share)["net_profit"] - target)
    if not _monotone(fn, lo, hi):
        return (None, False, "情景利润在该范围内对收入非单调：不硬求根")
    root, ok, why = _root(fn, lo, hi)
    if not ok:
        return (None, False, why)
    return (root, True, why + "（detail 模式：同一求值器）")


def threshold_plan(ctx: dict, *, target: Decimal, mode: str = "fixed",
                   expense_ratio: Decimal = Decimal("0"),
                   tax_rate=None, minority_share=None,
                   revenue_growth_assumed: Decimal = Decimal("0")) -> dict:
    """反向阈值（W0）：**同一目标、同一模式、同一组假设**下的三处反推。

    - `margin_threshold`：单因素（收入固定基期）——"毛利率要多高"；
    - `revenue_growth`：单因素（毛利率固定基期）——"收入要变化多少"；
    - `conditional_margin_threshold`：在 `revenue_growth_assumed` 这个**收入假设**下所需毛利率
      （两因素条件计算，只作诊断；收入假设为 0 时与单因素一致）。
    """
    margin, m_ok, m_why = _solve_margin(
        ctx, target=target, mode=mode, expense_ratio=expense_ratio, tax_rate=tax_rate,
        minority_share=minority_share, revenue_growth=Decimal("0"))
    growth, g_ok, g_why = _solve_growth(
        ctx, target=target, mode=mode, expense_ratio=expense_ratio,
        tax_rate=tax_rate, minority_share=minority_share)
    cond = None
    cond_ok, cond_why = False, "未计算"
    if revenue_growth_assumed:
        cond, cond_ok, cond_why = _solve_margin(
            ctx, target=target, mode=mode, expense_ratio=expense_ratio,
            tax_rate=tax_rate, minority_share=minority_share,
            revenue_growth=_d(revenue_growth_assumed))
    else:
        cond, cond_ok, cond_why = margin, m_ok, "收入假设为 0：与单因素相同"

    def _in(value, bounds):
        return (value is not None and _d(bounds[0]) <= value <= _d(bounds[1]))

    def _pct(x):
        return (x * 100) if x is not None else None

    margin_base = ctx["margin"]
    return {
        "target_net_profit": target,
        "margin_base": margin_base,
        "margin_threshold": margin,
        "margin_gap_pp": (_pct(margin - margin_base) if margin is not None else None),
        "revenue_growth": growth,
        "conditional_margin_threshold": cond,
        "conditional_margin_gap_pp": (_pct(cond - margin_base) if cond is not None
                                      else None),
        "conditional_revenue_growth": _d(revenue_growth_assumed),
        "reachable": {"margin": bool(m_ok), "revenue": bool(g_ok),
                      "conditional": bool(cond_ok)},
        "within_allowed_range": {
            # 与允许参数同量纲：毛利率侧比的是**变化量 Δm**（百分点/分数），不是毛利率水平
            "margin": (_in(margin - margin_base, MARGIN_BOUNDS)
                       if (mode == "detail" and margin is not None) else None),
            "revenue": (_in(growth, GROWTH_BOUNDS)
                        if (mode == "detail" and growth is not None) else None)},
        "method": {"mode": mode,
                   "margin": ("代数反推" if mode != "detail" else m_why),
                   "revenue": ("代数反推" if mode != "detail" else g_why),
                   "conditional": cond_why},
        "notes": {"margin": m_why, "revenue": g_why, "conditional": cond_why},
        "assumed_revenue": ctx["revenue"] * (1 + _d(revenue_growth_assumed)),
        "below_gross_block": ctx["block_signed"] * (1 + expense_ratio),
        "per_day_capital": (ctx["revenue"] * (1 + _d(revenue_growth_assumed))
                            / Decimal(365)),
        "formula": ("单因素：收入侧固定毛利率=基期 g*=(T − B0' − 毛利0)/毛利0；"
                    "毛利率侧固定收入=基期 m*=(T − B0')/收入0；"
                    "detail 模式用同一求值器在允许范围内二分求根，超范围=不可达。"
                    "B0' 为毛利线以下净额（fixed：基期(毛利−归母)×(1+e)；"
                    "detail：逐项明细＋冻结残差）"),
    }


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
    ctx = base_context(dataset, period)
    base = {"revenue": float(rev.value), "gross_profit": float(gp.value),
            "net_profit": float(np_.value), "margin": float(ctx["margin"])}
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
    # 阈值是比率：4 位小数（+15.8226%），否则复核者按两位小数代回求值器对不上目标
    q4 = lambda x: float(Decimal(str(x)).quantize(Decimal("0.0001")))       # noqa: E731

    mode = str(params.get("below_gross_mode") or "fixed")
    _g_up, _m_up, _e_up, _g_down, _m_down = _assumptions(params)
    _tax = params.get("tax_rate")
    _mino = params.get("minority_share")
    tax_arg = _d(_tax) if _tax not in (None, "") else None
    mino_arg = _d(_mino) if _mino not in (None, "") else None
    # **同一求值器**给出基准／使用者情景／反向对照：基准一律回到"假设=基期"
    # （不采用使用者的税率/占比），这样基准复现是模型的稳定性质，而不是随假设漂移。
    base_r = evaluate(ctx, growth=0, margin_delta=0, expense_ratio=0, mode=mode)
    up_r = evaluate(ctx, growth=_g_up, margin_delta=_m_up, expense_ratio=_e_up,
                    mode=mode, tax_rate=tax_arg, minority_share=mino_arg)
    down_r = evaluate(ctx, growth=_g_down, margin_delta=_m_down, expense_ratio=0,
                      mode=mode, tax_rate=tax_arg, minority_share=mino_arg)
    sc_base = base_r["net_profit"]
    up, down = up_r["net_profit"], down_r["net_profit"]
    # 基准复现差额按**报告粒度**（元/分）比较：模型内部用比例式还原基期税率/占比，
    # 除法余数不进这个判定，但我们只对"报送出去的那两个数"负责。
    base_gap = _d(q(sc_base)) - _d(q(np_.value))

    up_label, up_formula = _scenario_label("使用者情景", _g_up, _m_up, _e_up)
    down_label, down_formula = _scenario_label("反向对照", _g_down, _m_down, 0.0)
    # 单因素敏感度：每 +1 个百分点的影响（收入 / 毛利率 / 费用）。
    # 基线用**使用者情景的费用/税项/少数股东假设**（但不含收入与毛利率变化），
    # 这样"最敏感的是哪一项"与同一组假设一致。
    sens_base = evaluate(ctx, growth=0, margin_delta=0, expense_ratio=_e_up, mode=mode,
                         tax_rate=tax_arg, minority_share=mino_arg)["net_profit"]
    sens = {
        "收入 +1pp": evaluate(ctx, growth=0.01, margin_delta=0, expense_ratio=_e_up,
                              mode=mode, tax_rate=tax_arg,
                              minority_share=mino_arg)["net_profit"] - sens_base,
        "毛利率 +1pp": evaluate(ctx, growth=0, margin_delta=0.01, expense_ratio=_e_up,
                                mode=mode, tax_rate=tax_arg,
                                minority_share=mino_arg)["net_profit"] - sens_base,
        "费用 -1pp": evaluate(ctx, growth=0, margin_delta=0,
                              expense_ratio=_d(_e_up) - _d("0.01"), mode=mode,
                              tax_rate=tax_arg,
                              minority_share=mino_arg)["net_profit"] - sens_base,
    }
    ranked = sorted(sens.items(), key=lambda kv: abs(kv[1]), reverse=True)
    direction_ok = bool(
        evaluate(ctx, growth=0.05, margin_delta=0, expense_ratio=_e_up, mode=mode,
                 tax_rate=tax_arg, minority_share=mino_arg)["net_profit"] > sens_base
        and evaluate(ctx, growth=0, margin_delta=-0.01, expense_ratio=_e_up, mode=mode,
                     tax_rate=tax_arg,
                     minority_share=mino_arg)["net_profit"] < sens_base)
    _assumed = [k for k in ("revenue_growth", "gross_margin_delta", "expense_change_ratio")
                if k in params]
    _defaults = [f"使用者情景：{up_label}（{'使用者提供' if _assumed else '未提供，用声明默认值'}）",
                 f"反向对照：{down_label}"]

    # 目标归母净利：使用者给定 > 上一期（"回到上年水平"）> 基期（此时阈值为 0，自检）
    prev_period = dataset.period_at(-1)
    prev_np = None
    if prev_period:
        prev_obs = dataset.get("net_profit", prev_period)
        if prev_obs is not None and prev_obs.value is not None:
            prev_np = _d(prev_obs.value)
    _target = params.get("target_net_profit")
    if _target not in (None, ""):
        target, target_source = _d(_target), "使用者给定"
    elif prev_np is not None:
        target = prev_np
        target_source = f"上一期（{prev_period}）归母净利：回到上年水平"
    else:
        target, target_source = _d(np_.value), "基期归母净利（没有上一期可比）"
    th = threshold_plan(ctx, target=target, mode=mode, expense_ratio=_d(_e_up),
                        tax_rate=tax_arg, minority_share=mino_arg,
                        revenue_growth_assumed=_d(_g_up))

    # W0：明细模式的逐项读数（假设**真正**进入最终利润：残差不补平）
    _detail_items: list = []
    _detail_diag: dict = {"mode": mode}
    if mode == "detail":
        items = [dict(c) for c in up_r["items"]]
        for c in items:
            c["value"] = q(c["value"])
            if c.get("disclosed_value") is not None:
                c["disclosed_value"] = q(c["disclosed_value"])
        _detail_items = items
        _detail_diag = {
            "mode": "detail",
            "rules": {"revenue_linked": list(REVENUE_LINKED_ITEMS),
                      "fixed": "其它已披露项（×(1+费用变化)）",
                      "explicit": ["tax_rate（情景税前利润）",
                                   "minority_share（情景合并净利）"],
                      "residual": "基期一次算定后冻结"},
            "items_used": list(ctx["items_used"]),
            "items_missing": list(ctx["items_missing"]),
            "signs": "各项按披露符号计入：正号为增利、负号为减利",
            "residual_yuan": q(ctx["residual"]),
            "residual_frozen": True,
            "residual_formula": ("残差 = 基期归母净利 − [基期毛利 + Σ(符号×已披露项)]，"
                                 "只算一次、不随假设变化"),
            "pretax_profit_base_yuan": q(ctx["pretax0"]),
            "pretax_profit_yuan": q(up_r["pretax"]),
            "income_tax_base_yuan": (q(ctx["tax0"]) if ctx["tax0"] is not None else None),
            "income_tax_yuan": q(up_r["tax"]),
            "tax_rate": float(tax_arg if tax_arg is not None
                              else (ctx["tax_rate0"] or Decimal("0"))),
            "tax_rate_source": ("使用者单独假设" if tax_arg is not None
                                else ("基期有效税率（基期所得税/基期税前利润）"
                                      if ctx["tax_rate0"] is not None
                                      else "缺所得税披露：已并入残差")),
            "consolidated_base_yuan": q(ctx["consolidated0"]),
            "consolidated_net_yuan": q(up_r["consolidated"]),
            "minority_base_yuan": (q(ctx["min0"]) if ctx["min0"] is not None else None),
            "minority_interest_yuan": q(up_r["minority"]),
            "minority_share": float(mino_arg if mino_arg is not None
                                    else (ctx["minority_share0"] or Decimal("0"))),
            "minority_source": ("使用者单独假设（情景合并净利×占比）" if mino_arg is not None
                                else ("基期占比（基期少数股东/基期合并净利）"
                                      if ctx["minority_share0"] is not None
                                      else "缺少数股东披露：已并入残差")),
            "below_gross_net_yuan": q(up_r["below_gross"]),
            "scenario_net_profit_yuan": q(up_r["net_profit"]),
        }
        if ctx["consolidated_disclosed"] is not None:
            _detail_diag["consolidated_disclosed_gap_yuan"] = q(
                ctx["consolidated0"] - ctx["consolidated_disclosed"])

    return {
        "periods": (period,),
        "formula": (
            "fixed：归母净利 = 收入×(1+g) × (基期毛利率+m) + 基期(毛利−归母净利)×(1+e)"
            "（毛利线以下净额整体一块，含费用/税项/投资收益/少数股东，**不是纯费用**）；"
            "detail：逐项按披露符号计入（随收入/固定），＋冻结的基期残差，"
            "再减 情景税前利润×税率 与 情景合并净利×少数股东占比；"
            "两个阈值共用同一目标归母净利（默认上一期）"),
        "inputs": (rev.fact_id, gp.fact_id, np_.fact_id),
        "assumptions": tuple(
            [f"基期读数取自 {period}（{rev.value}/{gp.value}/{np_.value}{unit}）",
             "收入增速 g（%）、毛利率变化 m（**百分点 pp**）、隐含块变化 e 均为"
             "**使用者设定**的假设（非披露事实）"
             + ("" if _assumed else "：本次未提供，使用模型中已声明的默认值")]
            + [f"{k}={v}（使用者提供）" for k, v in sorted(params.items()) if k in
               ("revenue_growth", "gross_margin_delta", "expense_change_ratio",
                "tax_rate", "minority_share", "below_gross_mode",
                "target_net_profit")]
            + _defaults
            + [f"目标归母净利 {target}（{target_source}）；"
               "收入阈值与毛利率阈值共用这一目标"]),
        "diagnostics": {
            "unit": unit, "currency": np_.currency, "caliber": np_.caliber,
            "entity": np_.entity, "entity_id": np_.entity_id, "periods": [period],
            "input_hashes": {o.metric: o.observation_hash for o in (rev, gp, np_)},
            "base": base,
            "base_reproduction_gap": ("0" if base_gap == 0 else str(base_gap)),
            "direction_ok": direction_ok,
            "below_gross": _detail_diag,
            "closure": "0",
            "no_probability": "无校准分布：不显示发生概率，也不显示预测置信区间",
            "thresholds": {
                "target_net_profit": float(target),
                "target_source": target_source,
                "assumed_revenue": float(th["assumed_revenue"]),
                "below_gross_block": float(th["below_gross_block"]),
                # 单位与对应输出一致：阈值用 %，差额用 pp；另给小数形式供内部计算核对。
                "margin_base": float(th["margin_base"]),
                "margin_base_pct": float(th["margin_base"]) * 100,
                "margin_threshold": (float(th["margin_threshold"]) * 100
                                     if th["margin_threshold"] is not None else None),
                "margin_threshold_fraction": (float(th["margin_threshold"])
                                              if th["margin_threshold"] is not None
                                              else None),
                "margin_gap_pp": (float(th["margin_gap_pp"])
                                  if th["margin_gap_pp"] is not None else None),
                "revenue_growth_threshold": (float(th["revenue_growth"]) * 100
                                             if th["revenue_growth"] is not None else None),
                "revenue_growth_threshold_fraction": (float(th["revenue_growth"])
                                                      if th["revenue_growth"] is not None
                                                      else None),
                "revenue_threshold_formula": th["formula"],
                "conditional_margin_threshold": (
                    float(th["conditional_margin_threshold"]) * 100
                    if th["conditional_margin_threshold"] is not None else None),
                "conditional_margin_gap_pp": (
                    float(th["conditional_margin_gap_pp"])
                    if th["conditional_margin_gap_pp"] is not None else None),
                "conditional_revenue_growth": float(th["conditional_revenue_growth"]),
                "method": th["method"],
                "reachable": th["reachable"],
                "within_allowed_range": th["within_allowed_range"],
                "threshold_notes": th["notes"],
                "capital_per_day": float(th["per_day_capital"]),
                "formula": th["formula"],
                "caveats": ("阈值是单因素反推：给出“需要什么”，不表示可达；"
                            "两把杠杆共用同一目标归母净利（默认上一期）；"
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
                  "label": f"基准（假设回到基期，复现 {np_.value}{unit}）",
                  "value": q(sc_base), "unit": unit,
                  "formula": "g=0, m=0, e=0"},
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
            {"metric": "collection_days_capital_per_day",
             "label": "回款天数敏感性：每 1 天的资金占用",
             "value": q(th["per_day_capital"]), "unit": unit,
             "output_period": f"{period}（{up_label}）", "components": [], "residual": None},
            {"metric": "collection_days_sensitivity_10d",
             "label": "回款天数敏感性：±10 天的资金占用（单项，非现金流预测）",
             "value": q(th["per_day_capital"] * 10), "unit": unit,
             "output_period": f"{period}（{up_label}）", "components": [], "residual": None},
        ] + _threshold_outputs(th, period=period, target_source=target_source,
                               q4=q4) + ([{
            "metric": "scenario_below_gross_detail",
            "label": f"毛利线以下净额（对归母净利的影响；{up_label}）",
            "value": q(up_r["below_gross"]),
            "unit": unit, "output_period": f"{period}（{up_label}）",
            "residual": _detail_diag.get("residual_yuan"),
            "components": _detail_items,
        }] if _detail_items else []),
        "limits": LIMITS,
    }


def _threshold_outputs(th: dict, *, period: str, target_source: str, q4) -> list:
    """阈值输出（W0）：**可达才给数**；不可达就不输出这个数，理由留在诊断里。

    精度用 4 位小数（不是金额的 2 位）：阈值是比率，两位小数会把 +15.8226% 印成 +15.82%，
    复核者按两位小数代回求值器就对不上目标（W0 验收要求"代回能恢复同一目标"）。

    旧实现把不可达写成 `value=None`，独立验证的 `output_shape`（值必须是数）与
    `gold`（`Decimal(str(None))`）都会报错——把"目标不可达"变成"模型跑错了"，
    读者分不清两者。这里按可达性决定是否输出，超范围如实标不可达。
    """
    outs: list = []
    if th.get("margin_threshold") is not None:
        outs.append({"metric": "margin_threshold_to_hold_base_profit",
                     "label": "维持目标归母净利所需毛利率（单因素；收入固定基期）",
                     "value": q4(th["margin_threshold"] * 100),
                     "unit": "%", "output_period": f"{period}（目标：{target_source}）",
                     "components": [], "residual": None})
    if th.get("margin_gap_pp") is not None:
        outs.append({"metric": "margin_gap_to_threshold_pp",
                     "label": "所需毛利率与基期毛利率之差（百分点）",
                     "value": q4(th["margin_gap_pp"]),
                     "unit": "%（pp）", "output_period": f"{period}（目标：{target_source}）",
                     "components": [], "residual": None})
    if th.get("revenue_growth") is not None:
        outs.append({"metric": "revenue_growth_to_hold_target",
                     "label": "维持目标归母净利所需收入变化（单因素；毛利率固定基期）",
                     "value": q4(th["revenue_growth"] * 100),
                     "unit": "%", "output_period": f"{period}（目标：{target_source}）",
                     "components": [], "residual": None})
    return outs


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


def _scenario_args(params: dict) -> dict:
    """compute / gold / components_gold **共用**的一份情景参数解析（W0）。"""
    mode = str(params.get("below_gross_mode") or "fixed")
    g_up, m_up, e_up, g_down, m_down = _assumptions(params)
    tax = params.get("tax_rate")
    mino = params.get("minority_share")
    return {"mode": mode, "g_up": _d(g_up), "m_up": _d(m_up), "e_up": _d(e_up),
            "g_down": _d(g_down), "m_down": _d(m_down),
            "tax": (_d(tax) if tax not in (None, "") else None),
            "minority": (_d(mino) if mino not in (None, "") else None)}


def _resolved_target(dataset, period: str, params: dict, ctx: dict) -> tuple:
    """目标归母净利与来源（compute 与 gold 共用同一处解析，避免两边各取一套）。"""
    _t = params.get("target_net_profit")
    if _t not in (None, ""):
        return _d(_t), "使用者给定"
    prev_period = dataset.period_at(-1)
    if prev_period:
        obs = dataset.get("net_profit", prev_period)
        if obs is not None and obs.value is not None:
            return _d(obs.value), f"上一期（{prev_period}）归母净利：回到上年水平"
    return ctx["net_profit"], "基期归母净利（没有上一期可比）"


def components_gold(dataset, params: dict | None = None) -> dict:
    """**分项**的独立计算（R1-b）：基准/使用者情景/反向对照与三个单因素敏感度逐个重算。

    与 `gold` 同一纪律：独立路径、Decimal、同一条完整身份判据。**基准分项直接对数据集的
    真实基期读数**（假设回到基期），不信载荷自报的 `base_reproduction_gap=0`——
    那条反例正是"基准 +10 / 使用者情景 −10，合计不变、旧验证全过"。
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
    ctx = base_context(dataset, period)
    if ctx["margin"] <= 0:
        raise NotApplicable("基期毛利率为负：增长/毛利率改善⇒利润改善的方向假设不成立")
    a = _scenario_args(params)
    q = lambda x: float(Decimal(str(x)).quantize(Decimal("0.01")))          # noqa: E731
    unit = str(np_.unit or "")
    sc_base = evaluate(ctx, growth=0, margin_delta=0, expense_ratio=0,
                       mode=a["mode"])["net_profit"]
    up = evaluate(ctx, growth=a["g_up"], margin_delta=a["m_up"], expense_ratio=a["e_up"],
                  mode=a["mode"], tax_rate=a["tax"], minority_share=a["minority"]
                  )["net_profit"]
    down = evaluate(ctx, growth=a["g_down"], margin_delta=a["m_down"], expense_ratio=0,
                    mode=a["mode"], tax_rate=a["tax"], minority_share=a["minority"]
                    )["net_profit"]
    sens_base = evaluate(ctx, growth=0, margin_delta=0, expense_ratio=a["e_up"],
                         mode=a["mode"], tax_rate=a["tax"],
                         minority_share=a["minority"])["net_profit"]
    sens = {
        "revenue_p1pp": evaluate(ctx, growth=_d("0.01"), margin_delta=0,
                                 expense_ratio=a["e_up"], mode=a["mode"],
                                 tax_rate=a["tax"], minority_share=a["minority"]
                                 )["net_profit"] - sens_base,
        "gross_margin_p1pp": evaluate(ctx, growth=0, margin_delta=_d("0.01"),
                                      expense_ratio=a["e_up"], mode=a["mode"],
                                      tax_rate=a["tax"], minority_share=a["minority"]
                                      )["net_profit"] - sens_base,
        "expense_m1pp": evaluate(ctx, growth=0, margin_delta=0,
                                 expense_ratio=a["e_up"] - _d("0.01"), mode=a["mode"],
                                 tax_rate=a["tax"], minority_share=a["minority"]
                                 )["net_profit"] - sens_base,
    }
    return {
        "scenario_net_profit": {"base": (q(sc_base), unit), "user": (q(up), unit),
                                "counter": (q(down), unit)},
        "scenario_sensitivity": {k: (q(v), unit) for k, v in sens.items()},
    }


def gold(dataset, params: dict | None = None) -> dict:
    """独立金样：基准复现必须等于基期净利（差 0）；情景、两个阈值与明细总量独立重算。

    与 `compute` 共用 `base_context`/`evaluate`/`threshold_plan` 这三处**定义**，
    但值一律现场从数据集重算，不读载荷自报的诊断字段。
    """
    params = dict(params or {})
    period = dataset.period_at(0)
    rev = dataset.require("revenue", period)
    gp = dataset.require("gross_profit", period)
    np_ = dataset.require("net_profit", period)
    # 独立路径同样核完整身份（L0-a）：与算子入口同一判据，不让"compute 拒绝、gold 照算"。
    ident_ok, ident_why = full_identity_ok(rev, gp, np_, same_scale=True)
    if not ident_ok:
        raise NotApplicable(ident_why)
    ctx = base_context(dataset, period)
    a = _scenario_args(params)
    up_r = evaluate(ctx, growth=a["g_up"], margin_delta=a["m_up"], expense_ratio=a["e_up"],
                    mode=a["mode"], tax_rate=a["tax"], minority_share=a["minority"])
    target, _src = _resolved_target(dataset, period, params, ctx)
    th = threshold_plan(ctx, target=target, mode=a["mode"], expense_ratio=a["e_up"],
                        tax_rate=a["tax"], minority_share=a["minority"],
                        revenue_growth_assumed=a["g_up"])
    out = {"scenario_net_profit": float(up_r["net_profit"].quantize(Decimal("0.01")))}
    # 阈值同样 4 位小数（与 compute 的输出精度一致）：否则金样比载荷"更粗"，
    # 复核者按载荷代回求值器就无法恢复目标。
    if th["margin_threshold"] is not None:
        out["margin_threshold_to_hold_base_profit"] = float(
            (th["margin_threshold"] * 100).quantize(Decimal("0.0001")))
    if th["margin_gap_pp"] is not None:
        out["margin_gap_to_threshold_pp"] = float(
            th["margin_gap_pp"].quantize(Decimal("0.0001")))
    if th["revenue_growth"] is not None:
        out["revenue_growth_to_hold_target"] = float(
            (th["revenue_growth"] * 100).quantize(Decimal("0.0001")))
    out["collection_days_capital_per_day"] = float(
        th["per_day_capital"].quantize(Decimal("0.01")))
    out["collection_days_sensitivity_10d"] = float(
        (th["per_day_capital"] * 10).quantize(Decimal("0.01")))
    # 明细模式的**总量**独立重算（= 对归母净利的净影响）；分项由 `components_gold` 给。
    if a["mode"] == "detail":
        out["scenario_below_gross_detail"] = float(
            up_r["below_gross"].quantize(Decimal("0.01")))
    return out
