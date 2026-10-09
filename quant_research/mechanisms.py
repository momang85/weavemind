# -*- coding: utf-8 -*-
"""Q1-v1：自定义经营余额的**确定性读数**与最小研究卡（§17.2 冻结公式）。

本模块是**纯函数**：不读文件、不联网、不调用模型、不建任务、不写库。输入是已冻结的
财务观察（`financial.json` 里的 `dataset.observations`，或同一字段名的导入预览记录），
输出组成表、方向读数、两条基线与一页研究卡。

冻结定义（q1-v1；**不随数据改**，改公式必须换版本号）：

    E_t   = 毛利_t − 销售费用_t − 管理费用_t − 研发费用_t − 税金及附加_t
    O_t   = (E_t − E_{t−1}) / 收入_{t−1}
    Y_op  = (E_{t+1} − E_t) / 收入_t

纪律（每条都对应一个不能再放宽的反例）：

- **只认合并口径 + CNY + 元 + 年度期间**；母公司/半年度/单季/累计中报一律不进主试验，
  避免把"母公司报表"或"上半年累计"混进同一张组成表；
- **五项输入缺一项 ⇒ E 不可算**（`missing_inputs`），**绝不填 0**：未披露研发费用按 0
  处理会把"缺项"伪装成"经营改善"；
- 分母收入 **≤ 0 ⇒ 不可算**（`base_revenue_not_positive`），不给负分母的方向；
- 同一 (指标, 期间) 出现两个不同值 ⇒ 该格**冲突**，不择一、不择末，读数留
  `conflict`；派生项（毛利）在父项冲突时同样拒绝；
- 方向按**固定精度**：比率先用 Decimal 精确算，再量化到 `1e-10` 后取符号；
  `|值| < 1e-10` 记「零」。零预测 = **弃权**（不计入正/负胜负分母），零实际另行统计；
  不为提高命中率调阈值。
- 归母净利润方向基线（`net_profit`，与 E 的合并口径**不同归属**）只作对照读数，
  不与 E 拼成一个闭合比率；两者分母不同这件事写在读数里。

历史/前瞻：本模块只算"从这些观察能推出什么"，**不判断**这些观察是事后才可见的。
`provenance_kind` 由调用方按其已知时点声明（本批的真实材料都是**历史重建**）。
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

SCHEMA = "weavemind.q1_readings/1"
METHOD_VERSION = "q1-v1"
CARD_SCHEMA = "weavemind.mechanism_card/1"
CARD_VERSION = "q1_card/1.0.0"
PRECISION = Decimal("1E-10")

# 五项构成项与**符号**：E = Σ term×value。符号显式写出来，改公式必须动这里。
TERMS: tuple[tuple[str, int], ...] = (
    ("gross_profit", 1),
    ("selling_expense", -1),
    ("admin_expense", -1),
    ("rd_expense", -1),
    ("taxes_and_surcharges", -1),
)
METRICS: tuple[str, ...] = tuple(metric for metric, _ in TERMS)
BASELINE_METRIC = "net_profit"                 # 归母净利润（方向基线，不是 E 的同一归属）
CONSOLIDATED_METRIC = "net_profit_consolidated"
CASH_METRIC = "operating_cashflow"
TURNOVER_METRICS = ("accounts_receivable", "inventory", "accounts_payable",
                    "total_assets", "total_liabilities")
# 明确**排除**在 E 之外的项目：列出来是为了让读者知道"这不是完整经营利润"。
EXCLUDED_ITEMS = ("finance_expense", "investment_income", "fair_value_change",
                  "credit_impairment", "asset_impairment", "other_income",
                  "asset_disposal_income", "non_operating_income",
                  "non_operating_expense", "income_tax_expense", "minority_interest")

ANNUAL_TYPES = ("", "annual", "年报", "10-K")
_YEAR_RE = re.compile(r"(19|20)\d{2}")


def _d(value) -> Decimal | None:
    """数值 → Decimal（非法/缺失 → None，不猜、不充零）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _f(value: Decimal | None):
    return None if value is None else float(value)


def sign(value) -> str:
    """固定精度符号：正 / 负 / 零 / 未知（None → 未知，不当零）。"""
    d = _d(value)
    if d is None:
        return "未知"
    d = d.quantize(PRECISION)
    if d > 0:
        return "正"
    if d < 0:
        return "负"
    return "零"


def year_of(record) -> str:
    """观察 → 4 位年度（期末日期优先，其次期间标签；取不到返回空串）。"""
    end = str(record.get("period_end") or "")
    m = _YEAR_RE.search(end)
    if m and len(str(record.get("period_end") or "")) >= 4:
        return str(record.get("period_end"))[:4]
    m = _YEAR_RE.search(str(record.get("period") or ""))
    return m.group(0) if m else ""


def _admissible(record, *, caliber: str) -> tuple[bool, str]:
    """一条观察能否进主试验：口径/币种/单位/期间性质四项都必须是**明确同一口径**。"""
    if str(record.get("caliber") or "") != caliber:
        return False, "caliber_not_consolidated"
    currency = str(record.get("currency") or "").upper()
    if currency not in ("CNY", ""):
        return False, "currency_not_cny"
    unit = str(record.get("unit") or "")
    if unit != "元":
        return False, "unit_not_yuan"
    ptype = str(record.get("period_type") or "")
    if ptype not in ANNUAL_TYPES:
        return False, "period_not_annual"
    end = str(record.get("period_end") or "")
    if end and not end.endswith("12-31"):
        return False, "period_end_not_year_end"
    if not year_of(record):
        return False, "period_unparsed"
    return True, ""


def index_observations(observations, *, caliber: str = "合并") -> dict:
    """观察序列 → `{(metric, year): {value, fact_id, ...}}`，冲突格如实留冲突。

    返回 `{"cells": {...}, "conflicts": [...], "rejected": [...]}`。
    """
    cells: dict[tuple[str, str], dict] = {}
    conflicts: list[dict] = []
    rejected: list[dict] = []
    for obs in observations or ():
        if str(obs.get("state") or "ok") not in ("ok", "zero"):
            continue
        value = _d(obs.get("value"))
        if value is None:
            continue
        ok, why = _admissible(obs, caliber=caliber)
        if not ok:
            rejected.append({"metric": obs.get("metric"), "period": obs.get("period"),
                             "reason": why})
            continue
        key = (str(obs.get("metric") or ""), year_of(obs))
        row = {"value": value, "fact_id": str(obs.get("fact_id") or ""),
               "period": str(obs.get("period") or ""), "period_end": str(obs.get("period_end") or ""),
               "entity_id": str(obs.get("entity_id") or ""),
               "source_hash": str(obs.get("source_hash") or ""),
               "derived_from": list(obs.get("derived_from") or ()),
               "formula_version": str(obs.get("formula_version") or ""),
               "period_kind": str(obs.get("period_kind") or ""),
               "restatement": str(obs.get("restatement") or "")}
        prev = cells.get(key)
        if prev is None:
            cells[key] = row
            continue
        same = (prev["value"] == row["value"] and prev["fact_id"] == row["fact_id"])
        if not same:
            # 同一 (指标, 年度) 两个不相容的值：不择一，整格作冲突。
            conflicts.append({"metric": key[0], "year": key[1],
                              "values": [str(prev["value"]), str(row["value"])],
                              "fact_ids": [prev["fact_id"], row["fact_id"]]})
            cells.pop(key, None)
            cells[("__conflict__", key[0] + ":" + key[1])] = {"conflict": True}
    return {"cells": cells, "conflicts": conflicts, "rejected": rejected}


def readings(observations, *, entity_id: str = "", entity: str = "",
             caliber: str = "合并") -> dict:
    """一份冻结观察 → Q1 读数（组成项、E、O、Y_op、归母方向基线）。"""
    indexed = index_observations(observations, caliber=caliber)
    cells = indexed["cells"]
    years = sorted({year for (metric, year) in cells
                    if metric in METRICS + (BASELINE_METRIC, CONSOLIDATED_METRIC,
                                            CASH_METRIC, "revenue")
                    and not metric.startswith("__")})

    def _cell(metric: str, year: str) -> dict:
        return cells.get((metric, year)) or {}

    def _conflict(metric: str, year: str) -> bool:
        return bool((cells.get(("__conflict__", f"{metric}:{year}")) or {}).get("conflict"))

    rows: dict[str, dict] = {}
    for year in years:
        components: dict[str, dict] = {}
        missing: list[str] = []
        conflicted: list[str] = []
        total = Decimal(0)
        complete = True
        for metric, weight in TERMS:
            cell = _cell(metric, year)
            if _conflict(metric, year):
                conflicted.append(metric)
                complete = False
                continue
            if not cell:
                missing.append(metric)
                complete = False
                continue
            components[metric] = {
                "value": _f(cell["value"]), "weight": weight,
                "contribution": _f(cell["value"] * weight),
                "unit": "元", "fact_id": cell["fact_id"], "period": cell["period"],
                "source_hash": cell["source_hash"],
                "derived_from": cell["derived_from"],
                "formula_version": cell["formula_version"],
                "restatement": cell["restatement"],
            }
            total += cell["value"] * weight
        revenue = _cell("revenue", year)
        e_state = ("computed" if complete else
                   ("conflict" if conflicted else "missing_inputs"))
        rows[year] = {
            "year": year,
            "components": components,
            "missing_inputs": missing,
            "conflicting_inputs": conflicted,
            "e": _f(total) if complete else None,
            "e_state": e_state,
            "revenue": _f(revenue["value"]) if revenue else None,
            "revenue_fact_id": revenue.get("fact_id", ""),
            "net_profit": _f(_cell(BASELINE_METRIC, year).get("value")),
            "net_profit_fact_id": _cell(BASELINE_METRIC, year).get("fact_id", ""),
            "net_profit_consolidated": _f(_cell(CONSOLIDATED_METRIC, year).get("value")),
            "operating_cashflow": _f(_cell(CASH_METRIC, year).get("value")),
            "cash_fact_id": _cell(CASH_METRIC, year).get("fact_id", ""),
        }
        for metric in TURNOVER_METRICS:
            rows[year][metric] = _f(_cell(metric, year).get("value"))

    chains: list[dict] = []
    ordered = [y for y in years if rows[y]["e_state"] == "computed"]
    positions = {y: i for i, y in enumerate(years)}
    for year in years:
        row = rows[year]
        if row["e_state"] != "computed":
            continue
        prev = next((y for y in reversed(years) if y < year), "")
        nxt = next((y for y in years if y > year), "")
        chain = {"t": year, "prev_year": prev, "next_year": nxt,
                 "e_t": row["e"], "revenue_t": row["revenue"],
                 "state": "ok", "reason": ""}
        # O_t
        if not prev or rows[prev]["e_state"] != "computed":
            chain.update({"o_t": None, "sign_o": "未知", "state": "o_unavailable",
                          "reason": "没有可算的上一期 E（缺项或未披露）"})
        else:
            base_rev = rows[prev]["revenue"]
            if base_rev is None or _d(base_rev) <= 0:
                chain.update({"o_t": None, "sign_o": "未知", "state": "o_unavailable",
                              "reason": "上期收入不可用或非正"})
            else:
                o_t = (_d(row["e"]) - _d(rows[prev]["e"])) / _d(base_rev)
                chain.update({"o_t": _f(o_t), "sign_o": sign(o_t),
                              "o_base_revenue": base_rev,
                              "o_base_revenue_fact_id": rows[prev]["revenue_fact_id"],
                              "e_prev": rows[prev]["e"]})
        # Y_op
        if not nxt or rows[nxt]["e_state"] != "computed":
            chain.update({"y_op": None, "sign_y": "未知", "y_state": "pending",
                          "y_reason": "下一同口径年度尚未取得（待数据），不填零"})
        elif row["revenue"] is None or _d(row["revenue"]) <= 0:
            chain.update({"y_op": None, "sign_y": "未知", "y_state": "unavailable",
                          "y_reason": "本期收入不可用或非正"})
        else:
            y_op = (_d(rows[nxt]["e"]) - _d(row["e"])) / _d(row["revenue"])
            chain.update({"y_op": _f(y_op), "sign_y": sign(y_op), "y_state": "mature",
                          "y_revenue": row["revenue"],
                          "e_next": rows[nxt]["e"], "next_year": nxt})
        # 归母净利润方向基线（同一 t：ΔNP_t 的方向；与 E 不同归属，只作对照）
        np_t, np_prev = rows[year]["net_profit"], rows[prev]["net_profit"] if prev else None
        if np_t is None or np_prev is None:
            chain["baseline_profit"] = {"delta": None, "sign": "未知",
                                        "state": "unavailable",
                                        "reason": "缺本期或上期归母净利润"}
        else:
            delta = _d(np_t) - _d(np_prev)
            chain["baseline_profit"] = {
                "delta": _f(delta), "sign": sign(delta), "state": "ok",
                "base_year": prev, "note": "归母净利润变化方向（与 E 的合并口径不同归属）"}
        chain["baseline_exploration_majority"] = {"sign": "未知", "state": "not_assessed",
                                                  "reason": "探索期在评估层固定后才可算"}
        chains.append(chain)

    return {
        "schema": SCHEMA, "method_version": METHOD_VERSION,
        "entity_id": entity_id, "entity": entity,
        "caliber": {"scope": caliber, "currency": "CNY", "unit": "元",
                    "period": "年度（累计；不混入 H1/YTD/单季）"},
        "formula": {
            "E": "毛利 − 销售费用 − 管理费用 − 研发费用 − 税金及附加",
            "O": "(E_t − E_{t−1}) / 收入_{t−1}",
            "Y_op": "(E_{t+1} − E_t) / 收入_t",
            "terms": [{"metric": m, "weight": w} for m, w in TERMS],
            "excluded_items": list(EXCLUDED_ITEMS),
            "not_claimed": ["不是法定营业利润或 EBIT", "不与归母净利润拼成同一闭合关系",
                            "不是对既有文献因子的复制"],
        },
        "years": years, "rows": rows, "chains": chains,
        "conflicts": indexed["conflicts"], "rejected": indexed["rejected"],
        "precision": str(PRECISION),
    }


def composition(reading: dict, t: str) -> dict:
    """`t` 相对上一期的组成表：逐项 Δ 贡献 + **恒等式核对**（贡献和 = ΔE）。"""
    rows = reading["rows"]
    row = rows.get(t) or {}
    prev_year = ""
    for chain in reading["chains"]:
        if chain["t"] == t:
            prev_year = chain.get("prev_year") or ""
            break
    prev = rows.get(prev_year) or {}
    if row.get("e_state") != "computed" or prev.get("e_state") != "computed":
        return {"t": t, "prev_year": prev_year, "state": "unavailable",
                "reason": "本期或上期 E 不可算", "items": [],
                "identity_ok": None, "delta_e": None, "residual": None}
    items, total = [], Decimal(0)
    for metric, weight in TERMS:
        a = (prev.get("components") or {}).get(metric)
        b = (row.get("components") or {}).get(metric)
        if not a or not b:
            return {"t": t, "prev_year": prev_year, "state": "unavailable",
                    "reason": f"{metric} 只有一期可用", "items": [],
                    "identity_ok": None, "delta_e": None, "residual": None}
        delta = (_d(b["value"]) - _d(a["value"])) * weight
        total += delta
        items.append({"metric": metric, "weight": weight,
                      "prev_value": a["value"], "value": b["value"],
                      "delta": _f(_d(b["value"]) - _d(a["value"])),
                      "contribution": _f(delta),
                      "prev_fact_id": a.get("fact_id", ""), "fact_id": b.get("fact_id", "")})
    delta_e = _d(row["e"]) - _d(prev["e"])
    residual = delta_e - total
    items.sort(key=lambda x: -abs(_d(x["contribution"])))
    return {"t": t, "prev_year": prev_year, "state": "ok",
            "items": items, "top_drivers": [i["metric"] for i in items[:2]],
            "delta_e": _f(delta_e), "sum_of_contributions": _f(total),
            "residual": _f(residual),
            "identity_ok": abs(residual) < Decimal("0.01"),
            "identity_note": "逐项 Δ 贡献之和 = ΔE（同一份输入、同一精度）；"
                             "相等只说明算对，不证明机制成立"}


def short_card(reading: dict, *, composition_row: dict | None = None,
               context: dict | None = None) -> dict:
    """一页研究卡：只写**冻结读数**能支持的内容；后续结果由 evaluate 层另填。

    `context` 允许传入复用既有算子的读数（现金质量/天数冲击/情景阈值）与风险线索；
    缺项如实写"未取得"，不编因果、不写信用等级、不写 alpha。
    """
    ctx = dict(context or {})
    row = composition_row or {}
    t = row.get("t") or ""
    top = list(row.get("top_drivers") or [])
    names = {"gross_profit": "毛利", "selling_expense": "销售费用", "admin_expense": "管理费用",
             "rd_expense": "研发费用", "taxes_and_surcharges": "税金及附加"}
    by_metric = {i["metric"]: i for i in (row.get("items") or [])}

    def _amt(value) -> str:
        if value is None:
            return "不可算"
        d = _d(value)
        return f"{d / Decimal(100000000):+.2f} 亿元"

    drive_bits = [f"{names.get(m, m)} {_amt((by_metric.get(m) or {}).get('contribution'))}"
                  for m in top]
    delta_e = row.get("delta_e")
    judgement = None
    for chain in reading["chains"]:
        if chain["t"] == t:
            judgement = chain
            break
    sign_o = (judgement or {}).get("sign_o", "未知")
    mechanism = ("E 不可算：本期或上期缺项，不发布方向"
                 if row.get("state") != "ok"
                 else f"{t} 年 E 比 {row.get('prev_year')} 年变化 {_amt(delta_e)}，"
                      f"主要来自{'、'.join(drive_bits) or '未取得'}；"
                      f"O_{t} 符号为「{sign_o}」")
    cash = ctx.get("cash") or {}
    risk = ctx.get("risk") or {}
    clues = [{"name": c.get("name"), "value": c.get("value"), "unit": c.get("unit"),
              "alternatives": list(c.get("alternatives") or c.get("limitations") or ())[:2]}
             for c in (risk.get("anomaly_clues") or [])]
    target = ctx.get("target") or {}
    card = {
        "schema": CARD_SCHEMA, "card_version": CARD_VERSION,
        "entity_id": reading.get("entity_id"), "entity": reading.get("entity"),
        "caliber": reading.get("caliber"),
        "identity": {
            "method_version": METHOD_VERSION,
            "formula": reading.get("formula"),
            "formula_status": "computed" if row.get("state") == "ok" else "not_computed",
            "known_as_of": dict(ctx.get("known_as_of") or {}),
            "sources": list(ctx.get("sources") or []),
            "study_id": ctx.get("study_id"), "result_hash": ctx.get("result_hash"),
            "input_fact_ids": ctx.get("input_fact_ids") or [],
        },
        "readings": {
            "t": t, "prev_year": row.get("prev_year"),
            "composition": [{"metric": i["metric"], "prev_value": i["prev_value"],
                             "value": i["value"], "delta": i["delta"],
                             "contribution": i["contribution"]} for i in (row.get("items") or [])],
            "delta_e": delta_e, "e_t": (reading["rows"].get(t) or {}).get("e"),
            "e_prev": (reading["rows"].get(row.get("prev_year")) or {}).get("e"),
            "identity_ok": row.get("identity_ok"),
            "o_t": (judgement or {}).get("o_t"), "sign_o": sign_o,
            "baseline_profit": (judgement or {}).get("baseline_profit"),
        },
        # 研究判断：**只由 O_t 的冻结规则给出**，不看后续结果
        "judgement": {
            "mechanism": mechanism,
            "expected_direction": ("偏改善（sign(Y_op) = 正）" if sign_o == "正" else
                                   "偏承压（sign(Y_op) = 负）" if sign_o == "负" else
                                   "弃权（O=0）" if sign_o == "零" else "未发布方向（读数不可算）"),
            "direction_rule": "sign(O_t) → sign(Y_op)",
            "observation_metric": "下一同口径年度 Y_op = (E_{t+1} − E_t)/收入_t",
            "target_period": f"{t} 年的下一同口径年度",
            # B2：公司级主因/替代解释由研究输入（spec）显式给出；缺省仍是通用替代解释，
            # 不硬编码任何公司的结论句。
            "company_primary_cause": ctx.get("company_primary_cause") or "",
            "alternatives": [str(a) for a in (ctx.get("alternatives") or [
                "毛利变化可来自结构/价格/渠道费用归类，不等同需求改善",
                "费用项变化可来自一次性计提、口径重分类或合并范围变化",
                "税金及附加与消费税/附加税费相关，不随收入等比例变化",
            ])],
            "refutation_conditions": [
                "下一同口径年度 sign(Y_op) 与 sign(O_t) 相反（分母收入为正）",
                "同一 (指标, 期间) 出现重述后的不同值 ⇒ 本期读数作废，重算",
                "可用时点晚于决策时点（版本可用时间晚于决策）",
            ],
        },
        "cash_turnover_alternatives": {
            "period_of_run": ctx.get("run_period") or "",
            "cashflow_coverage_pct": cash.get("cashflow_coverage"),
            "cfo_minus_profit": cash.get("cfo_minus_profit"),
            "cash_reconciliation_status": cash.get("reconciliation_status"),
            "receivables_to_revenue": risk.get("receivables_to_revenue"),
            "inventory_to_cost": risk.get("inventory_to_cost"),
            "liabilities_to_assets": risk.get("liabilities_to_assets"),
            "anomaly_clues": clues,
            "note": "现金与周转是**替代解释**，不是 E 的组成部分；"
                    "收款加快/去库存/延迟付款/季节性都可能改变现金，"
                    "不能仅凭 CFO/净利润认定盈利质量上升",
        },
        "target_condition_to_check": {
            "margin_gap_to_threshold_pp": target.get("margin_gap_to_threshold_pp"),
            "revenue_growth_to_hold_target": target.get("revenue_growth_to_hold_target"),
            "target_source": target.get("target_source"),
            "incremental_capital_occupation": target.get("incremental_capital_occupation"),
            "conditioned_reading": target.get("conditioned_reading")
            or ctx.get("target_note") or "",
            "note": "目标门槛是**替代路径**（收入或毛利率，不同时成立）；"
                    "交易日数冲击是设定情景，不是历史信号；"
                    "研究者设定/回到上年水平都不叫管理层承诺",
        },
        "next_material": [str(m) for m in (ctx.get("next_check") or [
            "下一期同口径年度报告（同主体/同合并范围/同科目口径）",
            "现金流量表补充资料（经营现金/合并净利润与营运资本三行）",
            "若为真实材料：下一期行情（事后反应观察；缺合格行情只停价格域）",
        ])],
        "provenance_kind": ctx.get("provenance_kind") or "历史重建",
        "provenance_note": ctx.get("provenance_note") or "",
        "usage_state": {
            "synthetic": bool(ctx.get("synthetic", False)),
            "internal_only": True,
            "research_state": ctx.get("research_state") or "research_draft",
            "human_review": ctx.get("human_review") or "unreviewed",
            "machine_checks": dict(ctx.get("machine_checks") or {}),
            "external_delivery": dict(ctx.get("external_delivery") or {"allowed": False}),
        },
        "follow_up": ctx.get("follow_up") or {"status": "待数据", "mechanism_verdict": "未决"},
        "no_credit_score": True, "no_alpha_claim": True,
        "scenario_not_signal": "交易日数压力是设定情景，不当历史信号",
        "human_confirmation_note": (
            "人工确认只能确认**卡的内容与来源**，不能把历史重建卡变成事前预测："
            "本卡生成时后续年度结果已公开" if (ctx.get("provenance_kind") or "历史重建")
            != "前瞻冻结" else
            "人工确认后须单独记录确认时间，才可称事前假说"),
    }
    for_hash = {k: v for k, v in card.items() if k != "generated_at"}
    card["card_hash"] = hashlib.sha256(
        json.dumps(for_hash, ensure_ascii=False, sort_keys=True,
                   default=str).encode("utf-8")).hexdigest()
    return card


def card_markdown(card: dict) -> str:
    """卡 → 一页 Markdown（首屏≤1页；明细与定位另附）。"""
    r = card["readings"]
    j = card["judgement"]
    cash = card["cash_turnover_alternatives"]
    target = card["target_condition_to_check"]
    lines = [f"# {card.get('entity') or ''}（{card['entity_id']}）经营机制卡",
             "",
             f"- 方法：`{card['identity']['method_version']}`；口径："
             f"{card['caliber']['scope']}／{card['caliber']['currency']}／{card['caliber']['period']}",
             f"- 身份：**{card['provenance_kind']}**——{card.get('provenance_note') or '—'}",
             f"- 卡hash：`{card['card_hash'][:16]}…`；研究ID：`{card['identity'].get('study_id')}`；"
             f"结果hash：`{str(card['identity'].get('result_hash') or '')[:16]}…`",
             ""]
    if r.get("t"):
        lines += [f"## 组成表（{r['prev_year']} → {r['t']}）", "",
                  "| 项目 | 上期 | 本期 | Δ | 对 ΔE 的贡献 |",
                  "|---|---:|---:|---:|---:|"]
        names = {"gross_profit": "毛利", "selling_expense": "销售费用",
                 "admin_expense": "管理费用", "rd_expense": "研发费用",
                 "taxes_and_surcharges": "税金及附加"}
        for item in r["composition"]:
            lines.append(f"| {names.get(item['metric'], item['metric'])} "
                         f"| {_yi(item.get('prev_value'))} | {_yi(item.get('value'))} "
                         f"| {_yi(item.get('delta'))} | {_yi(item.get('contribution'))} |")
        lines += ["", f"E_{r['prev_year']} = {_yi(r.get('e_prev'))}；E_{r['t']} = {_yi(r.get('e_t'))}；"
                      f"ΔE = {_yi(r['delta_e'])}；逐项贡献和 = "
                      f"{_yi(sum(i['contribution'] or 0 for i in r['composition']))}；"
                      f"恒等式核对：{'通过' if r.get('identity_ok') else '不通过'}。",
                  f"O_{r['t']} = {_pct(r.get('o_t'))}（符号 **{r['sign_o']}**）；"
                  f"归母净利润方向基线：{(r.get('baseline_profit') or {}).get('sign')}"
                  f"（Δ={_yi((r.get('baseline_profit') or {}).get('delta'))}）", ""]
    lines += ["## 研究判断（冻结规则，不看后续结果）", "",
              f"- 机制：{j['mechanism']}",
              f"- 预期方向：{j['expected_direction']}（规则 {j['direction_rule']}）",
              f"- 观察指标／目标期：{j['observation_metric']}；{j['target_period']}",
              f"- 本公司具体主因（来自研究输入，供核查）：{j.get('company_primary_cause') or '未提供'}",
              "- 替代解释：" + "；".join(j["alternatives"]),
              "- 反证条件：" + "；".join(j["refutation_conditions"]), "",
              "## 现金与周转的替代解释", "",
              f"- 读数的期间：{cash.get('period_of_run') or '未标（与组成表同期时为空）'}"
              + ("（**与组成表年度不同**，两处期间分别标注）"
                 if cash.get("period_of_run") and cash["period_of_run"] != f"{r.get('t')}年"
                 else ""),
              f"- 经营现金/归母净利：{_pp(cash.get('cashflow_coverage_pct'))}；"
              f"经营现金 − 归母净利：{_yi(cash.get('cfo_minus_profit'))}",
              f"- 应收/同期收入：{_ratio(cash.get('receivables_to_revenue'))}；"
              f"存货/同期成本：{_ratio(cash.get('inventory_to_cost'))}；"
              f"负债/资产：{_ratio(cash.get('liabilities_to_assets'))}"]
    for clue in cash.get("anomaly_clues") or []:
        lines.append(f"- 核查线索（**不是舞弊概率**）：{clue.get('name')} = {_num(clue.get('value'))}"
                     + (f"；{clue['alternatives'][0]}" if clue.get("alternatives") else ""))
    lines += [f"- {cash['note']}", "",
              "## 值得核查的目标条件（Q2，同一假设，两条替代路径）", "",
              f"- 毛利率距目标阈值：{_pp(target.get('margin_gap_to_threshold_pp'))} 个百分点；"
              f"收入维持目标所需变化：{_pp(target.get('revenue_growth_to_hold_target'))}",
              f"- 目标来源：{target.get('target_source') or '未取得'}",
              f"- 10 天回款＋库存的额外占用：{_yi(target.get('incremental_capital_occupation'))}"]
    if target.get("conditioned_reading"):
        lines.append(f"- 条件下的可承受空间（研究输入）：{target['conditioned_reading']}")
    lines += [f"- {target['note']}", "",
              "## 下一份应核材料", ""]
    lines += [f"- {item}" for item in card["next_material"]]
    fu = card.get("follow_up") or {}
    lines += ["", "## 后续结果（历史重建时另附；未成熟不写读数）", "",
              f"- 状态：{fu.get('status')}；机制判定：{fu.get('mechanism_verdict')}"
              + (f"；{fu.get('note')}" if fu.get("note") else "")]
    reading = fu.get("actual_reading") or {}
    diff = fu.get("difference") or {}
    if reading:
        lines.append(f"- 实际读数：t={reading.get('t')}→{reading.get('next_year')}；"
                     f"Y_op={_pct(reading.get('y_op'))}（{reading.get('sign_y')}）")
        lines.append(f"- 与原卡方向比对：预期 {diff.get('predicted_sign')}，"
                     f"实际 {diff.get('actual_sign')}，"
                     f"{'方向一致' if diff.get('hit') else '方向相反'}")
    lines += ["", "## 使用状态", "",
              f"- 模拟数据：{'是' if card['usage_state']['synthetic'] else '否'}；"
              f"内部研究：是；研究状态：{card['usage_state']['research_state']}；"
              f"人工复核：{card['usage_state']['human_review']}；"
              f"对外交付：不允许",
              f"- {card['human_confirmation_note']}",
              f"- 边界：{card['scenario_not_signal']}；不含综合信用分、不含 alpha 声明",
              "",
              f"来源：{_source_line(card['identity'].get('sources') or [])}"]
    return "\n".join(lines)


def _yi(value) -> str:
    d = _d(value)
    if d is None:
        return "—"
    return f"{d / Decimal(100000000):,.2f} 亿"


def _pct(value) -> str:
    """**比率** → 百分比文本（0.0172 → +1.72%）。"""
    d = _d(value)
    if d is None:
        return "未取得"
    return f"{d * 100:+.2f}%"


def _pp(value) -> str:
    """**已是百分点/%的数** → 文本（-0.83 → -0.83%）。"""
    d = _d(value)
    if d is None:
        return "未取得"
    return f"{d:+.2f}%"


def _ratio(value) -> str:
    d = _d(value)
    if d is None:
        return "未取得"
    return f"{d * 100:.2f}%"


def _num(value) -> str:
    """线索值原样格式化（不做单位换算、不解释成概率）。"""
    d = _d(value)
    if d is None:
        return "未取得"
    text = f"{d:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _source_line(sources) -> str:
    bits = []
    for s in sources if isinstance(sources, list) else []:
        bits.append(f"{s.get('source_label') or s.get('material_id')}"
                    f"（{s.get('disclosed_at') or '披露时间未标'}；"
                    f"sha256 {str(s.get('sha256') or '')[:12]}…）")
    return "；".join(bits) or "未取得"


def cards_markdown(cards: list[dict], *, title: str = "Q1 真实经营机制卡") -> str:
    out = [f"# {title}", "",
           "内部研究草稿；人工未复核；**历史重建**（生成时后续年度结果已公开，"
           "不构成事前预测或 alpha）。", ""]
    for card in cards:
        out += [card_markdown(card), "", "---", ""]
    return "\n".join(out)
