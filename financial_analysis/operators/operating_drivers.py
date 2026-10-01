# -*- coding: utf-8 -*-
"""经营驱动利润桥（U1 主线，2026-10-01）：规模 / 毛利率 / 逐项费用与税项分解。

回答首批三个研究问题里的第一个：**利润变化到底来自收入规模、毛利率，还是费用、税项、
非经营因素；哪些解释得到披露支持。**

公式（全部确定性、可手算复核）：

1. 毛利桥（对称分解，交互项均分，与代入先后顺序无关）：
   `Δ毛利 = ΔR×(m1+m0)/2 + Δm×(R1+R0)/2`，其中 `m = 毛利/收入`。
2. 净利桥：`Δ归母净利 = Δ毛利 + Δ(毛利线以下净额 B)`，`B = 归母净利 − 毛利`。
3. 毛利线以下逐项：`ΔB = Σ signᵢ×ΔXᵢ + 未解释差额`（费用/税金/所得税/少数股东 = −1，
   收益类 = +1，符号按会计含义统一，不按出现顺序）。
4. 分段：对**两期都有收入与成本观察**的口径（分产品/分地区，用现有 `caliber` 表达）
   各算一次毛利桥，并保留"未分类差额"（其他业务或口径差）；两条切法**不可相加**。
5. 量价（仅当同一口径两期都有销量与收入）：`ΔR = ΔQ×p̄ + Δp×Q̄`，
   均价 = 该口径收入/销量，**含产品结构混合，不得命名"提价效果"**。

纪律（与利润桥同一套）：

- 未取到的明细项**留在未解释差额里**，既不当作零，也不摊到别的项目上；
- 毛利率的**百分点**变化不替代金额归因（`no_pp_substitution` 恒定执行）；
- 算子只给金额、口径与公式；**原因与反证由报告层结合披露写**（数值贡献 ≠ 因果）；
- 末位处理：分项各自按分四舍五入后，把 ≤0.01 元的差留给配对分项，
  保证"分项合计 = 父项"（`identity` 恒定执行），差额量级在 `diagnostics.symmetry_gap` 里如实标出。
"""
from __future__ import annotations

from decimal import Decimal

from ..contracts import (
    InputRequirement, ModelSpec, NotApplicable, OutputSpec, full_identity_ok,
)

IMPL_VERSION = "operating_drivers/1.0.0"

# 毛利线以下的披露项目：稳定 id、中文标签、对利润的符号（+1 增利 / −1 减利）
LINE_ITEMS: tuple[tuple[str, str, int], ...] = (
    ("taxes_and_surcharges", "税金及附加", -1),
    ("selling_expense", "销售费用", -1),
    ("admin_expense", "管理费用", -1),
    ("rd_expense", "研发费用", -1),
    ("finance_expense", "财务费用", -1),
    ("other_income", "其他收益", +1),
    ("investment_income", "投资收益", +1),
    ("fair_value_change", "公允价值变动收益", +1),
    ("credit_impairment", "信用减值损失", +1),
    ("asset_impairment", "资产减值损失", +1),
    ("asset_disposal_income", "资产处置收益", +1),
    ("non_operating_income", "营业外收入", +1),
    ("non_operating_expense", "营业外支出", -1),
    ("income_tax_expense", "所得税费用", -1),
    ("minority_interest", "少数股东损益", -1),
)

LIMITS = (
    "Δ归母净利 = Δ毛利 + Δ(毛利线以下净额)：会计恒等式分解，**不等于**因果解释",
    "逐项贡献只覆盖已取得的披露项目；未取到的留在“未解释差额”里，不当作零、不摊派",
    "“收入规模效应/毛利率效应”是同一分解的两个部分（交互项均分），不可读成两笔独立事实",
    "毛利率的百分点变化不能替代金额归因（口径不同，不可互换）",
    "分产品/分地区是同一酒类口径的不同切法，**不可相加**；未分类差额单独列出",
    "量价分解的“均价”由该口径收入/销量算出，含产品结构混合，不得命名“提价效果”",
)

SPEC = ModelSpec(
    model_id="operating_drivers",
    version="1.0.0",
    family="经营驱动分解",
    question="利润变化有多少来自收入规模、毛利率，多少来自费用/税项/非经营项目；"
             "分产品与分地区各自贡献多少；还有多少未解释",
    operator="operating_drivers_v1",
    inputs=(
        InputRequirement("revenue_cur", "revenue", period_offset=0),
        InputRequirement("revenue_prev", "revenue", period_offset=-1),
        InputRequirement("cost_cur", "operating_cost", period_offset=0),
        InputRequirement("cost_prev", "operating_cost", period_offset=-1),
        InputRequirement("net_profit_cur", "net_profit", period_offset=0),
        InputRequirement("net_profit_prev", "net_profit", period_offset=-1),
    ),
    outputs=(
        OutputSpec("net_profit_change", "归母净利润变化", kind="amount",
                   structure="bridge"),
        OutputSpec("gross_profit_change", "毛利变化", kind="amount",
                   structure="bridge"),
        OutputSpec("revenue_scale_effect", "收入规模效应", kind="amount"),
        OutputSpec("gross_margin_effect", "毛利率效应", kind="amount"),
        OutputSpec("below_gross_line_change", "毛利线以下净额变化", kind="amount"),
        OutputSpec("net_profit_change_detail", "毛利线以下逐项分解", kind="amount",
                   structure="bridge"),
        OutputSpec("gross_profit_change_by_segment", "分产品/分地区毛利变化",
                   kind="amount", structure="bridge"),
        OutputSpec("volume_price_decomposition", "量价分解", kind="amount",
                   structure="bridge"),
    ),
    allowed_params={"rounding": ("yuan_2", "yi_2")},
    tolerance=0.005,
    validations=("gold", "closure", "identity", "unit", "no_pp_substitution"),
    budget={"steps": 1, "seconds": 5},
    limits=LIMITS,
    question_types=("profit_attribution", "volume_price"),
    # 两个**固定铰链**做逐项独立核对（R1-b）：分项集合静态可枚举，缺项/多项/换 id 都失败。
    # 逐项明细（依赖披露了哪些表）与分段明细是动态集合，只受 identity/output_shape 约束，
    # 不声明成静态分项——否则"披露少一张表"会被误判成验证失败。
    component_ids={
        "net_profit_change": ("gross_profit_change", "below_gross_line_change"),
        "gross_profit_change": ("revenue_scale_effect", "gross_margin_effect"),
    },
)


def _d(value) -> Decimal:
    return Decimal(str(value))


def _q(value: Decimal) -> float:
    """金额按分四舍五入（报告口径）。"""
    return float(value.quantize(Decimal("0.01")))


def _split(total: Decimal, first: Decimal) -> tuple[float, float]:
    """把 `total` 拆成 `(first, total - first)`，两项都按分四舍五入且**合计等于 total**。

    `first` 用公式算出来（对称分解的其中一项），配对项用差额，避免"除不尽"导致的假不闭合；
    公式值与差额值的差（≤0.01 元）在 diagnostics 的 `symmetry_gap` 里如实标出。
    """
    first_q = _q(first)
    total_q = _q(total)
    return first_q, float(Decimal(str(total_q)) - Decimal(str(first_q)))


def _periods(dataset) -> tuple[str, str]:
    periods = [p for p in (dataset.manifest.periods or ()) if p]
    if len(periods) < 2:
        raise NotApplicable("数据集只有一个期间：经营驱动桥需要两期（不给单期归因）")
    return periods[-2], periods[-1]


def _core(dataset):
    """取六个必需输入并过一次完整身份判据（主体/币种/报表范围/量纲/期间）。"""
    prev_p, cur_p = _periods(dataset)
    rev_c = dataset.require("revenue", cur_p)
    rev_p = dataset.require("revenue", prev_p)
    cost_c = dataset.require("operating_cost", cur_p)
    cost_p = dataset.require("operating_cost", prev_p)
    np_c = dataset.require("net_profit", cur_p)
    np_p = dataset.require("net_profit", prev_p)
    ident_ok, why = full_identity_ok(rev_c, rev_p, cost_c, cost_p, np_c, np_p,
                                     same_scale=True)
    if not ident_ok:
        raise NotApplicable(why)
    if rev_c.period == rev_p.period:
        raise NotApplicable("两期收入期间相同：无法做两期桥接")
    return prev_p, cur_p, rev_c, rev_p, cost_c, cost_p, np_c, np_p


def _amounts(rev_c, rev_p, cost_c, cost_p, np_c, np_p) -> dict:
    """金额与分解（Decimal 精确算，最后按分四舍五入）。"""
    d_rev = _d(rev_c.value) - _d(rev_p.value)
    gp_c = _d(rev_c.value) - _d(cost_c.value)
    gp_p = _d(rev_p.value) - _d(cost_p.value)
    d_gp = gp_c - gp_p
    m_c = gp_c / _d(rev_c.value) if _d(rev_c.value) != 0 else None
    m_p = gp_p / _d(rev_p.value) if _d(rev_p.value) != 0 else None
    if m_c is None or m_p is None:
        raise NotApplicable("收入为 0：毛利率与规模分解不可算（不编造）")
    d_m = m_c - m_p
    scale_exact = d_rev * (m_c + m_p) / 2
    margin_exact = d_m * (_d(rev_c.value) + _d(rev_p.value)) / 2
    scale_q, margin_q = _split(d_gp, scale_exact)
    d_np = _d(np_c.value) - _d(np_p.value)
    below = d_np - d_gp
    gp_q, below_q = _split(d_np, d_gp)
    return {
        "d_rev": d_rev, "d_gp": d_gp, "d_np": d_np, "below": below,
        "scale": scale_q, "margin": margin_q, "gp_q": gp_q, "below_q": below_q,
        "m_c": m_c, "m_p": m_p, "d_m": d_m,
        "symmetry_gap_scale": float(scale_exact - Decimal(str(scale_q))),
        "symmetry_gap_margin": float(margin_exact - Decimal(str(margin_q))),
    }


def _line_details(dataset, prev_p, cur_p, np_c, np_p, below: Decimal) -> tuple[list, list, list]:
    """逐项取披露项目 → `(used, rejected, missing)`，符号按会计含义统一。"""
    used: list[dict] = []
    rejected: list[str] = []
    missing: list[str] = []
    for metric, label, sign in LINE_ITEMS:
        cur = dataset.get(metric, cur_p)
        prev = dataset.get(metric, prev_p)
        if cur is None or prev is None:
            missing.append(metric)
            continue
        ident_ok, why = full_identity_ok(np_c, np_p, cur, prev, same_scale=True)
        if not ident_ok:
            rejected.append(f"{metric}:{why[:60]}")
            continue
        delta = _d(cur.value) - _d(prev.value)
        used.append({
            "component_id": metric, "label": label, "sign": sign,
            "prev_yuan": float(_d(prev.value)), "cur_yuan": float(_d(cur.value)),
            "contribution": _d(sign) * delta,
            "unit": str(cur.unit or ""),
        })
    return used, rejected, missing


# 报表**范围**（不是分段维度）：`合并` 是母项本身，`母公司` 是另一种报表范围。
# U1 实测反例：真实年报抽取同时给出合并与母公司两套利润表，把「母公司」当分段切法后
# `未分类差额` 从 −0.05 亿变成 −34.67 亿（把另一种报表范围混进了分段）。
_REPORT_SCOPES = ("合并", "母公司", "合并报表", "母公司报表")


def _segments(dataset, prev_p, cur_p, parent_caliber: str) -> tuple[list, list]:
    """两期都有收入与成本的口径各算一次毛利桥；缺一项就跳过并记原因。

    **只认分段维度**：与母公司报表同口径的那一份（如「合并」）是母项本身，
    其它报表范围（如「母公司」）是**另一种范围**而不是切法——两者都必须排除，
    否则分段合计 = 母项 + 其它范围，`未分类差额` 会被算成一个巨大的假差额。
    """
    out: list[dict] = []
    skipped: list[str] = []
    calibers: list[str] = []
    for period in (cur_p, prev_p):
        for c in dataset.calibers_of("revenue", period):
            if c and c not in calibers:
                calibers.append(c)
    for cal in calibers:
        if str(cal) == str(parent_caliber or ""):
            skipped.append(f"{cal}:与母公司报表同口径（不是分段切法）")
            continue
        if str(cal) in _REPORT_SCOPES:
            skipped.append(f"{cal}:报表范围而不是分段维度（并表范围另有其表）")
            continue
        vals = {}
        for metric in ("revenue", "operating_cost"):
            for period in (cur_p, prev_p):
                vals[(metric, period)] = dataset.get(metric, period, caliber=cal)
        if any(v is None for v in vals.values()):
            skipped.append(f"{cal}:两期收入/成本不全")
            continue
        four = [vals[("revenue", cur_p)], vals[("revenue", prev_p)],
                vals[("operating_cost", cur_p)], vals[("operating_cost", prev_p)]]
        # 分段只在这四项**彼此之间**核身份：分段口径（如「分产品:白酒」）本来就与
        # 合并口径不同，把合并口径的归母净利拉进来比口径会把所有分段都判掉。
        ident_ok, why = full_identity_ok(*four, same_scale=True)
        if not ident_ok:
            skipped.append(f"{cal}:{why[:60]}")
            continue
        r_c, r_p = vals[("revenue", cur_p)], vals[("revenue", prev_p)]
        c_c, c_p = vals[("operating_cost", cur_p)], vals[("operating_cost", prev_p)]
        gp_c = _d(r_c.value) - _d(c_c.value)
        gp_p = _d(r_p.value) - _d(c_p.value)
        if _d(r_c.value) == 0 or _d(r_p.value) == 0:
            skipped.append(f"{cal}:收入为 0")
            continue
        m_c, m_p = gp_c / _d(r_c.value), gp_p / _d(r_p.value)
        d_r, d_m = _d(r_c.value) - _d(r_p.value), m_c - m_p
        d_gp = gp_c - gp_p
        scale_q, margin_q = _split(d_gp, d_r * (m_c + m_p) / 2)
        out.append({
            "caliber": cal, "label": f"{cal}", "cut": str(cal).split(":", 1)[0]
            if ":" in str(cal) else "分段",
            "d_gp": d_gp, "d_gp_q": _q(d_gp), "scale_q": scale_q, "margin_q": margin_q,
            "m_c": m_c, "m_p": m_p, "d_m": d_m,
            "r_c": _d(r_c.value), "r_p": _d(r_p.value),
            "derived_inputs": [x.fact_id for x in four
                               if getattr(x, "derived_from", ())],
            "unit": str(r_c.unit or ""),
        })
    return out, skipped


def _volume_price(dataset, prev_p, cur_p) -> tuple[dict | None, list]:
    """同一口径两期都有销量与收入时做量价分解；均价含结构，标注不清口径差异。"""
    skipped: list[str] = []
    best: dict | None = None
    calibers: list[str] = []
    for period in (cur_p, prev_p):
        for c in dataset.calibers_of("sales_volume", period):
            if c and c not in calibers:
                calibers.append(c)
    for cal in calibers:
        v_c = dataset.get("sales_volume", cur_p, caliber=cal)
        v_p = dataset.get("sales_volume", prev_p, caliber=cal)
        r_c = dataset.get("revenue", cur_p, caliber=cal)
        r_p = dataset.get("revenue", prev_p, caliber=cal)
        if any(x is None for x in (v_c, v_p, r_c, r_p)):
            skipped.append(f"{cal}:销量/收入不全")
            continue
        q_c, q_p = _d(v_c.value), _d(v_p.value)
        if q_c == 0 or q_p == 0:
            skipped.append(f"{cal}:销量为 0")
            continue
        ok, why = full_identity_ok(r_c, r_p, same_scale=True)
        if not ok:
            skipped.append(f"{cal}:{why[:60]}")
            continue
        p_c = _d(r_c.value) / q_c
        p_p = _d(r_p.value) / q_p
        d_r = _d(r_c.value) - _d(r_p.value)
        vol_exact = (q_c - q_p) * (p_c + p_p) / 2
        vol_q, price_q = _split(d_r, vol_exact)
        cand = {
            "caliber": cal, "label": f"{cal}量价分解",
            "volume_cur": float(q_c), "volume_prev": float(q_p),
            "price_cur": float(p_c), "price_prev": float(p_p),
            "volume_effect": vol_q, "price_effect": price_q,
            "d_rev": _q(d_r), "unit": str(r_c.unit or ""),
            "volume_unit": str(v_c.unit or ""),
            "price_formula": f"{r_c.value}/{v_c.value}（收入/销量，含结构混合）",
            "gap": float((q_c - q_p) * (p_c + p_p) / 2 - Decimal(str(vol_q))
                         + ((p_c - p_p) * (q_c + q_p) / 2 - Decimal(str(price_q)))),
            "_rev": _d(r_c.value),
        }
        if best is None or cand["_rev"] > best["_rev"]:
            best = cand
    if best is not None:
        best.pop("_rev", None)
    return best, skipped


def compute(dataset, params: dict | None = None) -> dict:
    """算一次经营驱动桥 → `{outputs, diagnostics, formula, inputs, assumptions}`。"""
    params = dict(params or {})
    prev_p, cur_p, rev_c, rev_p, cost_c, cost_p, np_c, np_p = _core(dataset)
    a = _amounts(rev_c, rev_p, cost_c, cost_p, np_c, np_p)
    used, rejected, missing = _line_details(dataset, prev_p, cur_p, np_c, np_p, a["below"])
    segs, seg_skipped = _segments(dataset, prev_p, cur_p, str(np_c.caliber or ""))
    vp, vp_skipped = _volume_price(dataset, prev_p, cur_p)

    period_label = f"{cur_p}较{prev_p}"
    unit = str(np_c.unit or "")

    # —— 毛利线以下逐项（动态集合）：合计 = ΔB（差额留给“未解释差额”一项）
    detail_components: list[dict] = []
    explained = Decimal("0")
    for item in used:
        contrib_q = _q(item["contribution"])
        explained += Decimal(str(contrib_q))
        detail_components.append({
            "component_id": item["component_id"], "label": item["label"],
            "value": contrib_q, "unit": unit,
            "formula": (f"{'+' if item['sign'] > 0 else '−'}"
                        f"({item['cur_yuan']} - {item['prev_yuan']})"),
        })
    detail_components.sort(key=lambda c: -abs(c["value"]))
    residual_q = float(Decimal(str(_q(a["below"]))) - explained)
    detail_components.append({
        "component_id": "unexplained_residual", "label": "未解释差额（未取得的披露项目）",
        "value": residual_q, "unit": unit,
        "formula": "Δ(归母净利−毛利) − 已列项目贡献合计",
    })

    outputs: list[dict] = [
        {"metric": "net_profit_change", "label": "归母净利润变化",
         "value": _q(a["d_np"]), "unit": unit, "output_period": period_label,
         "residual": 0.0,
         "components": [
             {"component_id": "gross_profit_change", "label": "毛利变化",
              "value": a["gp_q"], "unit": unit,
              "formula": f"({rev_c.value} - {cost_c.value}) - ({rev_p.value} - {cost_p.value})"},
             {"component_id": "below_gross_line_change", "label": "毛利线以下净额变化",
              "value": a["below_q"], "unit": unit,
              "formula": "Δ归母净利 − Δ毛利"},
         ]},
        {"metric": "gross_profit_change", "label": "毛利变化",
         "value": a["gp_q"], "unit": unit, "output_period": period_label,
         "residual": 0.0,
         "components": [
             {"component_id": "revenue_scale_effect", "label": "收入规模效应",
              "value": a["scale"], "unit": unit,
              "formula": "ΔR×(m1+m0)/2（交互项均分）"},
             {"component_id": "gross_margin_effect", "label": "毛利率效应",
              "value": a["margin"], "unit": unit,
              "formula": "Δm×(R1+R0)/2；末位并入，使分项合计等于毛利变化"},
         ]},
        {"metric": "revenue_scale_effect", "label": "收入规模效应",
         "value": a["scale"], "unit": unit, "output_period": period_label,
         "components": [], "residual": None},
        {"metric": "gross_margin_effect", "label": "毛利率效应",
         "value": a["margin"], "unit": unit, "output_period": period_label,
         "components": [], "residual": None},
        {"metric": "below_gross_line_change", "label": "毛利线以下净额变化",
         "value": a["below_q"], "unit": unit, "output_period": period_label,
         "components": [], "residual": None},
        {"metric": "net_profit_change_detail", "label": "毛利线以下逐项分解",
         "value": a["below_q"], "unit": unit, "output_period": period_label,
         "residual": residual_q, "components": detail_components},
    ]

    if segs:
        # **按切法分开出**：`分产品`/`分行业`/`分地区`/`分销售模式` 是同一口径的不同切法，
        # 汇到一张桥里会重复计数（三套切法各自都覆盖全公司）。每种切法一张桥，
        # 父项仍是公司毛利变化，差额记 `unclassified_gross_profit_change`（其他业务/口径差）。
        by_cut: dict[str, list] = {}
        for s in segs:
            by_cut.setdefault(str(s["cut"]), []).append(s)
        for cut, entries in by_cut.items():
            seg_components = [{
                "component_id": f"segment:{s['caliber']}",
                "label": f"{s['caliber']}毛利变化"
                         + ("（含推算输入）" if s["derived_inputs"] else ""),
                "value": s["d_gp_q"], "unit": unit,
                "formula": "ΔR×(m1+m0)/2 + Δm×(R1+R0)/2",
            } for s in entries]
            seg_sum = sum(Decimal(str(c["value"])) for c in seg_components)
            unclassified = float(Decimal(str(a["gp_q"])) - seg_sum)
            seg_components.append({
                "component_id": "unclassified_gross_profit_change",
                "label": "未分类差额（其他业务/口径差；同一口径的其它切法不可与此相加）",
                "value": unclassified, "unit": unit,
                "formula": "公司毛利变化 − 本切法已列口径毛利变化合计",
            })
            outputs.append({
                "metric": "gross_profit_change_by_segment",
                "label": f"{cut}毛利变化（切法：{cut}）",
                "value": a["gp_q"], "unit": unit, "output_period": period_label,
                "residual": unclassified, "components": seg_components,
            })

    if vp is not None:
        outputs.append({
            "metric": "volume_price_decomposition", "label": vp["label"],
            "value": vp["d_rev"], "unit": unit, "output_period": period_label,
            "residual": vp["gap"],
            "components": [
                {"component_id": "volume_effect", "label": "销量效应",
                 "value": vp["volume_effect"], "unit": unit,
                 "formula": "ΔQ×(p1+p0)/2（均价=收入/销量）"},
                {"component_id": "price_effect", "label": "单位价格效应",
                 "value": vp["price_effect"], "unit": unit,
                 "formula": "Δp×(Q1+Q0)/2；含产品结构混合，不得称“提价效果”"},
            ]})

    # 替代解释（U1）：数值贡献是会计分解，**业务原因需要证据**。这里把两条**确定性**的
    # 反证/口径提醒一并给出，供报告层与读者判断"这个贡献是不是经营改善"：
    # ① 实际税率反事实：所得税减少多来自利润下滑本身，税率变化反而可能吃掉一部分；
    # ② 非经营因素：公允价值变动/投资收益不进经营判断。
    alt: dict = {}
    _line = {i["component_id"]: i for i in used}
    try:
        if "income_tax_expense" in _line:
            tax_c = dataset.get("income_tax_expense", cur_p)
            tax_p = dataset.get("income_tax_expense", prev_p)
            npc_c = dataset.get("net_profit_consolidated", cur_p)
            npc_p = dataset.get("net_profit_consolidated", prev_p)
            if all(x is not None for x in (tax_c, tax_p, npc_c, npc_p)):
                pbt_c = _d(npc_c.value) + _d(tax_c.value)
                pbt_p = _d(npc_p.value) + _d(tax_p.value)
                if pbt_c != 0 and pbt_p != 0:
                    r_c, r_p = _d(tax_c.value) / pbt_c, _d(tax_p.value) / pbt_p
                    at_prev = pbt_c * r_p
                    alt["effective_tax_rate"] = {
                        "prev": float(r_p), "cur": float(r_c),
                        "delta_pp": float((r_c - r_p) * 100),
                        "note": "实际税率＝所得税费用／（合并净利润＋所得税费用）",
                    }
                    alt["tax_at_prior_rate"] = {
                        "actual_yuan": float(_d(tax_c.value)),
                        "at_prior_rate_yuan": float(at_prev),
                        "rate_effect_yuan": float(_d(tax_c.value) - at_prev),
                        "note": ("按上年实际税率折算本年的反事实：所得税的“贡献”里有多少"
                                 "只是利润下滑的被动结果"),
                    }
    except Exception:                                  # noqa: BLE001 - 反事实算不出就不给
        pass
    for _m, _label in (("fair_value_change", "公允价值变动收益"),
                       ("investment_income", "投资收益")):
        if _m in _line:
            alt.setdefault("non_operating_items", {})[_m] = {
                "label": _label, "contribution_yuan": float(_line[_m]["contribution"]),
                # V1（阶段V）：**不按指标名一律判非经常**——分类取决于事项的经济性质、
                # 行业与业务模式并考虑持续性；要看公司披露的非经常性损益/扣非归母净利。
                "note": ("需按公司披露的**非经常性损益/扣非归母净利**与业务实质判断是否"
                         "经常性：不按指标名统一剔除，也不据此制造“正常化利润”"),
            }
    if segs or vp is not None:
        alt["structure_vs_price"] = (
            "分段与均价都是**同一口径的分解**：均价由「该口径收入/销量」算出，含产品结构混合，"
            "不能直接命名“提价效果”；分产品/分行业/分地区/分销售模式是不同切法，不可相加")

    closure = 0
    return {
        "periods": (prev_p, cur_p),
        "formula": (
            "Δ毛利 = ΔR×(m1+m0)/2 + Δm×(R1+R0)/2；"
            "Δ归母净利 = Δ毛利 + Δ(归母净利−毛利)；"
            "Δ(归母净利−毛利) = Σ sign×Δ项目 + 未解释差额"
        ),
        "inputs": (rev_c.fact_id, rev_p.fact_id, cost_c.fact_id, cost_p.fact_id,
                   np_c.fact_id, np_p.fact_id),
        "assumptions": (
            f"两期均为{np_c.caliber}口径、{np_c.currency}、{unit}；"
            "收入/成本/归母净利同属该报表范围",
            "期间取自数据集声明的期间顺序的最后两期",
            "交互项在两个效应间均分（对称分解），与代入顺序无关",
        ),
        "diagnostics": {
            "unit": unit, "currency": np_c.currency, "caliber": np_c.caliber,
            "entity": np_c.entity, "entity_id": np_c.entity_id,
            "periods": [prev_p, cur_p],
            "gross_margin": {"prev": float(a["m_p"]), "cur": float(a["m_c"]),
                             "delta_pp": float(a["d_m"] * 100)},
            "input_hashes": {o.period: o.observation_hash
                             for o in (rev_c, rev_p, cost_c, cost_p, np_c, np_p)},
            "closure": str(closure),
            "symmetric_formula": {
                "scale_yuan": float(a["scale"]), "margin_yuan": float(a["margin"]),
                "symmetry_gap_scale_yuan": a["symmetry_gap_scale"],
                "symmetry_gap_margin_yuan": a["symmetry_gap_margin"],
            },
            "line_items_used": [i["component_id"] for i in used],
            "line_items_missing": missing,
            "line_items_rejected": rejected,
            "unexplained_residual_yuan": residual_q,
            "segments_used": [s["caliber"] for s in segs],
            "segments_skipped": seg_skipped,
            "volume_price_skipped": vp_skipped,
            "volume_price_caliber": (vp or {}).get("caliber", ""),
            "alternative_explanations": alt,
            "caveats": (
                "未解释差额 = 未取得的披露项目；不得当作零或摊到已列项目",
                "分段是同一口径的不同切法，不可相加；未分类差额单独列出",
                "均价含产品结构混合，量价分解不等于“提价/降价”",
            ),
        },
        "outputs": outputs,
        "limits": LIMITS,
    }


# ---------------------------------------------------------------- 独立金样

def _gold_amounts(dataset):
    """独立路径（Decimal 手算，不复用 compute 的中间对象）——只依赖原始观察。"""
    prev_p, cur_p = _periods(dataset)
    rc = dataset.require("revenue", cur_p)
    rp = dataset.require("revenue", prev_p)
    cc = dataset.require("operating_cost", cur_p)
    cp = dataset.require("operating_cost", prev_p)
    nc = dataset.require("net_profit", cur_p)
    npo = dataset.require("net_profit", prev_p)
    ok, why = full_identity_ok(rc, rp, cc, cp, nc, npo, same_scale=True)
    if not ok:
        raise NotApplicable(why)
    gp_c = Decimal(str(rc.value)) - Decimal(str(cc.value))
    gp_p = Decimal(str(rp.value)) - Decimal(str(cp.value))
    m_c = gp_c / Decimal(str(rc.value))
    m_p = gp_p / Decimal(str(rp.value))
    d_gp = gp_c - gp_p
    scale = (Decimal(str(rc.value)) - Decimal(str(rp.value))) * (m_c + m_p) / 2
    scale_q, margin_q = _split(d_gp, scale)
    d_np = Decimal(str(nc.value)) - Decimal(str(npo.value))
    gp_q, below_q = _split(d_np, d_gp)
    return {"d_np": d_np, "d_gp": d_gp, "scale": scale_q, "margin": margin_q,
            "gp_q": gp_q, "below_q": below_q}


def gold(dataset, params: dict | None = None) -> dict:
    """独立金样：手算 `{指标: 值}`（Decimal 路径，与 compute 不同代码路径）。

    返回 `Decimal`（量化到分）：验证侧用 `_close` 比对，量化后的十进制字符串与载荷一致。
    """
    g = _gold_amounts(dataset)
    q = lambda x: x.quantize(Decimal("0.01"))          # noqa: E731
    return {
        "net_profit_change": q(g["d_np"]),
        "gross_profit_change": q(g["d_gp"]),
        "revenue_scale_effect": Decimal(str(g["scale"])),
        "gross_margin_effect": Decimal(str(g["margin"])),
        "below_gross_line_change": Decimal(str(g["below_q"])),
    }


def components_gold(dataset, params: dict | None = None) -> dict:
    """两个固定铰链的逐项独立计算（R1-b）：`{输出: {component_id: (值, 单位)}}`。"""
    g = _gold_amounts(dataset)
    prev_p, cur_p = _periods(dataset)
    unit = str(dataset.require("net_profit", cur_p).unit or "")
    return {
        "net_profit_change": {
            "gross_profit_change": (g["gp_q"], unit),
            "below_gross_line_change": (g["below_q"], unit),
        },
        "gross_profit_change": {
            "revenue_scale_effect": (g["scale"], unit),
            "gross_margin_effect": (g["margin"], unit),
        },
    }
