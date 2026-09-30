# -*- coding: utf-8 -*-
"""现金调节桥（U2 主线，2026-10-01）：**合并净利润 → 经营活动现金流**的披露调节表。

回答首批三个问题里的第二个：**利润有没有变成现金、主要支撑/拖累来自哪里。**

恒等式（照抄披露，符号不改）：

    `合并净利润 + Σ(已披露带符号调节项目) = 经营活动现金流 + 对账差额`

纪律：

- 起点是**合并净利润**（调节表用的就是它），不拿归母净利冒充；
- 调节项**只用披露的补充资料**（折旧摊销/减值/递延税/公允价值、存货与经营性应收应付变化、
  其他）；**不拿资产负债表两期期末差去硬凑**现金桥（并购、汇率、重分类会让它不等于调节表）；
- 分组是**确定性声明**（非现金项／营运资本项／其他），逐项贡献与分组合计都要能加回起点；
- 未取得的项目留在"未解释差额"里（既不当零、也不摊派）；差额非零时不写"完整调节"；
- 分组与口径取自披露；算子不解释因果，因果与反证由报告层结合披露写。
"""
from __future__ import annotations

from decimal import Decimal

from ..contracts import (
    InputRequirement, ModelSpec, NotApplicable, OutputSpec, full_identity_ok,
)

IMPL_VERSION = "cash_reconciliation/1.0.0"

# 披露项目 → (标签, 分组)。分组是**声明的**会计含义，不随披露有无而变。
ADJUSTMENT_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("asset_impairment_provision", "资产减值准备", "non_cash"),
    ("depreciation", "固定资产折旧、油气资产折耗、生产性生物资产折旧", "non_cash"),
    ("right_of_use_depreciation", "使用权资产折旧", "non_cash"),
    ("intangible_amortization", "无形资产摊销", "non_cash"),
    ("long_term_prepaid_amortization", "长期待摊费用摊销", "non_cash"),
    ("disposal_long_asset_loss", "处置长期资产的损失", "non_cash"),
    ("fixed_asset_scrap_loss", "固定资产报废损失", "non_cash"),
    ("fair_value_change_loss", "公允价值变动损失", "non_cash"),
    ("finance_expense_adjust", "财务费用（调节项）", "non_cash"),
    ("investment_loss", "投资损失（调节项）", "non_cash"),
    ("deferred_tax_asset_decrease", "递延所得税资产减少", "non_cash"),
    ("deferred_tax_liability_increase", "递延所得税负债增加", "non_cash"),
    ("inventory_decrease", "存货的减少（增加以“－”号填列）", "working_capital"),
    ("operating_receivable_decrease", "经营性应收项目的减少（增加以“－”号填列）",
     "working_capital"),
    ("operating_payable_increase", "经营性应付项目的增加（减少以“－”号填列）",
     "working_capital"),
    ("other_cashflow_adjustments", "其他（调节项）", "other"),
)
GROUP_LABELS = {
    "non_cash": "非现金项（折旧摊销/减值/递延税/公允价值等）",
    "working_capital": "营运资本项（存货/经营性应收/经营性应付）",
    "other": "其他调节项",
}
GROUP_ORDER = ("non_cash", "working_capital", "other")

LIMITS = (
    "这是**披露调节表的复算**：恒等式成立不等于“利润变现金”的原因已经解释",
    "起点是合并净利润（调节表口径），不是归母净利；归属层差异另见其它模型",
    "调节项只取现金流量表补充资料的披露值；**不用资产负债表期末差硬补**现金桥",
    "未取得的项目留在“未解释差额”里：既不当零，也不摊到别的分组",
    "单项调节额是会计口径的加回/扣减，不表示该项目的经济原因（如应付增加不等于融资改善）",
)

SPEC = ModelSpec(
    model_id="cash_reconciliation",
    version="1.0.0",
    family="现金形成机制",
    question="合并净利润到经营活动现金流之间，由哪些披露调节项支撑或拖累",
    operator="cash_reconciliation_v1",
    inputs=(
        InputRequirement("net_profit_consolidated_cur", "net_profit_consolidated",
                         period_offset=0),
        InputRequirement("net_profit_consolidated_prev", "net_profit_consolidated",
                         period_offset=-1),
        InputRequirement("operating_cashflow_cur", "operating_cashflow", period_offset=0),
        InputRequirement("operating_cashflow_prev", "operating_cashflow", period_offset=-1),
    ),
    outputs=(
        OutputSpec("operating_cashflow_reconciliation_cur",
                   "本期净利润到经营现金流的调节", kind="amount", structure="bridge"),
        OutputSpec("operating_cashflow_reconciliation_prev",
                   "上期净利润到经营现金流的调节", kind="amount", structure="bridge"),
        OutputSpec("cash_gap_change", "现金缺口变化（经营现金流−合并净利润）",
                   kind="amount", structure="bridge"),
        OutputSpec("largest_support", "最大支撑项（调节额）", kind="amount"),
        OutputSpec("largest_drag", "最大拖累项（调节额）", kind="amount"),
    ),
    allowed_params={"rounding": ("yuan_2", "yi_2")},
    tolerance=0.005,
    validations=("gold", "closure", "identity", "unit", "no_pp_substitution"),
    budget={"steps": 1, "seconds": 5},
    limits=LIMITS,
    question_types=("cash_conversion",),
    # 每期调节桥的分项集合是**静态**的（起点净利润 + 三组 + 未解释差额）→
    # 可做逐项独立核对（R1-b）；两期用**两个不同 metric**，否则验证无法区分是哪一期。
    component_ids={
        "operating_cashflow_reconciliation_cur": (
            "consolidated_net_profit", "non_cash_adjustments",
            "working_capital_adjustments", "other_adjustments", "unexplained_residual",
        ),
        "operating_cashflow_reconciliation_prev": (
            "consolidated_net_profit", "non_cash_adjustments",
            "working_capital_adjustments", "other_adjustments", "unexplained_residual",
        ),
        "cash_gap_change": (
            "change_in_net_profit", "change_in_adjustments",
        ),
    },
)


def _d(value) -> Decimal:
    return Decimal(str(value))


def _q(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def _split(total: Decimal, first: Decimal) -> tuple[float, float]:
    first_q = _q(first)
    total_q = _q(total)
    return first_q, float(Decimal(str(total_q)) - Decimal(str(first_q)))


def _periods(dataset) -> tuple[str, str]:
    periods = [p for p in (dataset.manifest.periods or ()) if p]
    if len(periods) < 2:
        raise NotApplicable("现金调节桥需要两期（本期与上期调节表）")
    return periods[-2], periods[-1]


def _core(dataset):
    prev_p, cur_p = _periods(dataset)
    np_c = dataset.require("net_profit_consolidated", cur_p)
    np_p = dataset.require("net_profit_consolidated", prev_p)
    cf_c = dataset.require("operating_cashflow", cur_p)
    cf_p = dataset.require("operating_cashflow", prev_p)
    ok, why = full_identity_ok(np_c, np_p, cf_c, cf_p, same_scale=True)
    if not ok:
        raise NotApplicable(why)
    return prev_p, cur_p, np_c, np_p, cf_c, cf_p


def _items(dataset, period: str, np_obs) -> tuple[list[dict], list[str], list[str]]:
    used: list[dict] = []
    missing: list[str] = []
    rejected: list[str] = []
    for metric, label, group in ADJUSTMENT_ITEMS:
        obs = dataset.get(metric, period)
        if obs is None:
            missing.append(metric)
            continue
        ok, why = full_identity_ok(np_obs, obs, same_scale=True)
        if not ok:
            rejected.append(f"{metric}:{why[:60]}")
            continue
        used.append({"metric": metric, "label": label, "group": group,
                     "value": _d(obs.value), "fact_id": obs.fact_id,
                     "unit": str(obs.unit or "")})
    return used, missing, rejected


def _bridge(dataset, period: str, np_obs, cf_obs) -> dict:
    used, missing, rejected = _items(dataset, period, np_obs)
    groups: dict[str, Decimal] = {g: Decimal("0") for g in GROUP_ORDER}
    for it in used:
        groups[it["group"]] += it["value"]
    np_v = _d(np_obs.value)
    cf_v = _d(cf_obs.value)
    adjustments = sum(groups.values(), Decimal("0"))
    residual = (cf_v - np_v) - adjustments          # 应≈0（披露表闭合）
    return {"period": period, "net_profit": np_v, "cashflow": cf_v,
            "groups": groups, "adjustments": adjustments, "residual": residual,
            "items": used, "missing": missing, "rejected": rejected}


def compute(dataset, params: dict | None = None) -> dict:
    params = dict(params or {})
    prev_p, cur_p, np_c, np_p, cf_c, cf_p = _core(dataset)
    b_cur = _bridge(dataset, cur_p, np_c, cf_c)
    b_prev = _bridge(dataset, prev_p, np_p, cf_p)
    unit = str(np_c.unit or "")

    def _group_components(b: dict) -> list[dict]:
        out = [{"component_id": "consolidated_net_profit", "label": "合并净利润",
                "value": _q(b["net_profit"]), "unit": unit, "formula": "披露值"}]
        for g in GROUP_ORDER:
            out.append({"component_id": f"{g}_adjustments", "label": GROUP_LABELS[g],
                        "value": _q(b["groups"][g]), "unit": unit,
                        "formula": "＋".join(i["metric"] for i in b["items"]
                                             if i["group"] == g) or "（本期未取到该项目）"})
        out.append({"component_id": "unexplained_residual",
                    "label": "未解释差额（未取得的披露调节项）",
                    "value": _q(b["residual"]), "unit": unit,
                    "formula": "(经营现金流 − 合并净利润) − 已列调节项合计"})
        return out

    outputs: list[dict] = []
    for b, suffix in ((b_cur, "cur"), (b_prev, "prev")):
        comps = _group_components(b)
        outputs.append({
            "metric": f"operating_cashflow_reconciliation_{suffix}",
            "label": f"净利润到经营现金流的调节（{b['period']}）",
            "value": _q(b["cashflow"]), "unit": unit,
            "output_period": str(b["period"]),
            "residual": _q(b["residual"]), "components": comps,
        })
    # 缺口变化：Δ(经营现金流 − 合并净利润)，按分组拆
    gap_c = _d(cf_c.value) - _d(np_c.value)
    gap_p = _d(cf_p.value) - _d(np_p.value)
    d_gap = gap_c - gap_p
    d_np = _d(np_c.value) - _d(np_p.value)
    d_adj = b_cur["adjustments"] - b_prev["adjustments"]
    first_q, second_q = _split(d_gap, -d_np)
    gap_components = [
        {"component_id": "change_in_net_profit", "label": "合并净利润变化（取负：利润少→缺口小）",
         "value": first_q, "unit": unit, "formula": "−(本期合并净利润 − 上期合并净利润)"},
        {"component_id": "change_in_adjustments", "label": "调节项合计变化",
         "value": second_q, "unit": unit, "formula": "本期调节项合计 − 上期调节项合计"},
    ]
    outputs.append({
        "metric": "cash_gap_change",
        "label": f"现金缺口变化（经营现金流−合并净利润，{cur_p}较{prev_p}）",
        "value": _q(d_gap), "unit": unit, "output_period": f"{cur_p}较{prev_p}",
        "residual": float(Decimal(str(_q(d_gap))) - (Decimal(str(first_q))
                                                     + Decimal(str(second_q)))),
        "components": gap_components,
    })
    # 最大支撑/拖累：按**单项调节额**（不含起点净利润），两期各给一组
    def _extreme(b: dict, want_positive: bool):
        cands = [i for i in b["items"]
                 if (i["value"] > 0) == want_positive and i["value"] != 0]
        if not cands:
            return None
        it = max(cands, key=lambda x: abs(x["value"]))
        return {"period": b["period"], "metric": it["metric"], "label": it["label"],
                "value": _q(it["value"]), "unit": unit, "group": it["group"]}
    sup = _extreme(b_cur, True)
    drag = _extreme(b_cur, False)
    for metric, label, item in (("largest_support", "最大支撑项", sup),
                                ("largest_drag", "最大拖累项", drag)):
        outputs.append({
            "metric": metric, "label": f"{label}（{cur_p}调节额）",
            "value": (item or {}).get("value", 0.0), "unit": unit,
            "output_period": str(cur_p), "components": [], "residual": None,
        })

    return {
        "periods": (prev_p, cur_p),
        "formula": ("合并净利润 + Σ(已披露带符号调节项) = 经营活动现金流 + 对账差额；"
                    "分组：非现金项 / 营运资本项 / 其他"),
        "inputs": (np_c.fact_id, np_p.fact_id, cf_c.fact_id, cf_p.fact_id),
        "assumptions": (
            f"两期均为{np_c.caliber}口径、{np_c.currency}、{unit}；"
            "起点是**合并净利润**（调节表口径）",
            "调节项全部来自现金流量表补充资料的披露值，符号照抄披露",
            "资产负债表两期期末差未参与计算（只作旁证，另行判断）",
        ),
        "diagnostics": {
            "unit": unit, "currency": np_c.currency, "caliber": np_c.caliber,
            "entity": np_c.entity, "entity_id": np_c.entity_id,
            "periods": [prev_p, cur_p],
            "closure": str(b_cur["residual"] - b_cur["residual"]),   # 恒等式按构造闭合
            "reconciliation": {
                b["period"]: {
                    "net_profit_yuan": float(b["net_profit"]),
                    "cashflow_yuan": float(b["cashflow"]),
                    "adjustments_yuan": float(b["adjustments"]),
                    "residual_yuan": float(b["residual"]),
                    "groups": {g: float(b["groups"][g]) for g in GROUP_ORDER},
                    "items": [{"metric": i["metric"], "label": i["label"],
                               "group": i["group"], "value_yuan": float(i["value"])}
                              for i in b["items"]],
                } for b in (b_cur, b_prev)
            },
            "largest_support": sup, "largest_drag": drag,
            "items_missing": {"cur": b_cur["missing"], "prev": b_prev["missing"]},
            "items_rejected": {"cur": b_cur["rejected"], "prev": b_prev["rejected"]},
            "closure_note": ("对账差额≈0 表示披露调节项与经营现金流自洽；"
                             "差额非零说明有调节项没取到，报告不得写“完整调节”"),
            "caveats": (
                "经营现金流 ≠ 归母净利：起点是合并净利润，归属层差异另见其它模型",
                "单项调节额是会计加回/扣减，不表示经济原因（应付增加不等于融资改善）",
                "资产负债表期末差只作旁证，不用于补现金桥",
            ),
        },
        "outputs": outputs,
        "limits": LIMITS,
    }


# ---------------------------------------------------------------- 独立金样

def _gold_bridge(dataset):
    prev_p, cur_p = _periods(dataset)
    out = {}
    for period in (cur_p, prev_p):
        np_obs = dataset.require("net_profit_consolidated", period)
        cf_obs = dataset.require("operating_cashflow", period)
        ok, why = full_identity_ok(np_obs, cf_obs, same_scale=True)
        if not ok:
            raise NotApplicable(why)
        groups = {g: Decimal("0") for g in GROUP_ORDER}
        for metric, _label, group in ADJUSTMENT_ITEMS:
            obs = dataset.get(metric, period)
            if obs is None:
                continue
            ok2, _why2 = full_identity_ok(np_obs, obs, same_scale=True)
            if not ok2:
                continue
            groups[group] += Decimal(str(obs.value))
        out[period] = {"np": Decimal(str(np_obs.value)),
                       "cf": Decimal(str(cf_obs.value)), "groups": groups}
    return prev_p, cur_p, out


def gold(dataset, params: dict | None = None) -> dict:
    """独立金样：逐项 Decimal 手算（不复用 compute 的中间对象），键与载荷 metric 同名。"""
    prev_p, cur_p, g = _gold_bridge(dataset)
    q = lambda x: Decimal(str(x)).quantize(Decimal("0.01"))      # noqa: E731
    res = {
        "operating_cashflow_reconciliation_cur": q(g[cur_p]["cf"]),
        "operating_cashflow_reconciliation_prev": q(g[prev_p]["cf"]),
        "cash_gap_change": q((g[cur_p]["cf"] - g[cur_p]["np"])
                             - (g[prev_p]["cf"] - g[prev_p]["np"])),
    }
    # 最大支撑/拖累：按单项调节额（不含起点净利润）取本期绝对值最大者
    items = []
    for metric, _label, _group in ADJUSTMENT_ITEMS:
        obs = dataset.get(metric, cur_p)
        if obs is None:
            continue
        ok, _why = full_identity_ok(dataset.require("net_profit_consolidated", cur_p),
                                    obs, same_scale=True)
        if ok:
            items.append((metric, Decimal(str(obs.value))))
    pos = [x for x in items if x[1] > 0]
    neg = [x for x in items if x[1] < 0]
    res["largest_support"] = q(max(pos, key=lambda x: abs(x[1]))[1]) if pos else q(0)
    res["largest_drag"] = q(max(neg, key=lambda x: abs(x[1]))[1]) if neg else q(0)
    return res


def components_gold(dataset, params: dict | None = None) -> dict:
    """固定铰链的逐项独立计算：`{输出: {component_id: (值, 单位)}}`。"""
    prev_p, cur_p, g = _gold_bridge(dataset)
    unit = str(dataset.require("net_profit_consolidated", cur_p).unit or "")
    out: dict[str, dict] = {}
    for period, suffix in ((cur_p, "cur"), (prev_p, "prev")):
        b = g[period]
        residual = (b["cf"] - b["np"]) - sum(b["groups"].values(), Decimal("0"))
        out[f"operating_cashflow_reconciliation_{suffix}"] = {
            "consolidated_net_profit": (_q(b["np"]), unit),
            "non_cash_adjustments": (_q(b["groups"]["non_cash"]), unit),
            "working_capital_adjustments": (_q(b["groups"]["working_capital"]), unit),
            "other_adjustments": (_q(b["groups"]["other"]), unit),
            "unexplained_residual": (_q(residual), unit),
        }
    d_gap = ((g[cur_p]["cf"] - g[cur_p]["np"]) - (g[prev_p]["cf"] - g[prev_p]["np"]))
    d_np = g[cur_p]["np"] - g[prev_p]["np"]
    first, second = _split(d_gap, -d_np)
    out["cash_gap_change"] = {
        "change_in_net_profit": (Decimal(str(first)), unit),
        "change_in_adjustments": (Decimal(str(second)), unit),
    }
    return out
