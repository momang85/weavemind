# -*- coding: utf-8 -*-
"""V2 证据：两条业务杠杆 + 简明费用/税项假设 + 现金单项敏感性（洋河／三一，正常入口）。

要点（不写死数字，全部来自运行输出）：
- **利润侧两条杠杆**：给定目标归母净利（如回到上年水平），算"收入要变化多少"与
  "毛利率要多高"两个方向的**单因素反推**（`revenue_growth_to_hold_target` /
  `margin_threshold_to_hold_base_profit`）；
- **费用/税项假设**：明细模式（`below_gross_mode=detail`）逐项给"固定/随收入/单独假设"，
  看得见税前利润、税率来源与**残差**；
- **现金侧单项敏感性**：用 V1 的 ΔOCF 单项拆解，算"某一项增量减半/反转"时现金变化会怎样
  （会计口径推演，不是现金流预测）；
- **基准复现**：两种模式在参数全 0 下给出同一个基期净利（`base_reproduction_gap=0`）。

产出 `docs/evidence/v2_levers.json`。三一这里**不复制**洋河的 −12.83%：三一的方向由它自己的
上年基数决定（利润高于上年 ⇒ 反推出的收入变化为负）。
"""
from __future__ import annotations

import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                  # noqa: BLE001
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "v2_levers.json")

CASES = {
    "洋河股份": {"entity_id": "002304.SZ", "code": "002304",
                 "doc": "evals/a2_official_chain_20260929/002304/project/materials/"
                        "f42e747c73850b59/doc.json",
                 "cash_lever": "operating_payable_increase"},
    "三一重工": {"entity_id": "600031.SH", "code": "600031",
                 "doc": "evals/a2_official_chain_20260929/600031/project/materials/"
                        "0c3e82056e977adc/doc.json",
                 "cash_lever": "operating_payable_increase"},
}


def _case(name: str, spec: dict) -> dict:
    import financial_analysis as fa
    import facts as F

    doc = json.load(open(os.path.join(ROOT, spec["doc"]), encoding="utf-8"))
    facts = F.facts_from_annual_tables(doc, company=name, company_code=spec["code"],
                                       periods=(2023, 2024), as_of="2025-04-30",
                                       disclosed_at="2025-04-28")
    ds = fa.freeze_from_facts(facts, periods=(2023, 2024), entity=name,
                              entity_id=spec["entity_id"], as_of="2025-04-30",
                              source_label="v2")
    out: dict = {"entity_id": spec["entity_id"],
                 "dataset": {"observations": ds.manifest.observations,
                             "usable": ds.manifest.usable,
                             "dataset_hash": ds.manifest.dataset_hash}}
    # ① 上年归母净利（目标水平）与本年基期读数：都取自运行输入的真实观察
    np_prev = ds.get("net_profit", "2023年")
    np_cur = ds.get("net_profit", "2024年")
    out["readings"] = {"net_profit_prev_yuan": float(np_prev.value),
                       "net_profit_cur_yuan": float(np_cur.value)}
    target = float(np_prev.value)

    # ② 利润侧两条杠杆：收入侧反推 + 毛利率侧阈值（同一组目标/基期）
    run = fa.run("scenario_sensitivity", ds,
                 params={"revenue_growth": 0.0, "gross_margin_delta": 0.0,
                         "target_net_profit": target})
    vals = {o.metric: o.value for o in (run.outputs or [])}
    diag = dict((run.outputs[0].diagnostics or {})) if run.outputs else {}
    base_out = next((o for o in (run.outputs or []) if o.metric == "scenario_net_profit"),
                    None)
    out["profit_levers"] = {
        "status": str(run.status), "validation_failed": list(run.validation.get("failed") or []),
        "target_net_profit_yuan": target,
        "revenue_growth_to_hold_target_pct": vals.get("revenue_growth_to_hold_target"),
        "margin_threshold_to_hold_base_profit_pct":
            vals.get("margin_threshold_to_hold_base_profit"),
        "margin_gap_pp": vals.get("margin_gap_to_threshold_pp"),
        "base_reproduction_gap": diag.get("base_reproduction_gap"),
        "base_component": (next((c["value"] for c in (base_out.components or ())
                                 if c["component_id"] == "base"), None)
                           if base_out is not None else None),
        "note": ("两条杠杆是**单因素反推**：一个答“收入要变化多少”、一个答“毛利率要多高”，"
                 "都给出“需要什么”，不表示可达"),
    }

    # ③ 费用/税项假设（明细模式）：费用 +3%、税率按基期实际税率（不给 tax_rate）
    det = fa.run("scenario_sensitivity", ds,
                 params={"revenue_growth": 0.0, "gross_margin_delta": 0.0,
                         "expense_change_ratio": 0.03, "below_gross_mode": "detail"})
    det_out = next((o for o in (det.outputs or [])
                    if o.metric == "scenario_below_gross_detail"), None)
    det_diag = dict(((det.outputs[0].diagnostics or {}).get("below_gross") or {})) \
        if det.outputs else {}
    out["cost_tax_assumption"] = {
        "status": str(det.status),
        "validation_failed": list(det.validation.get("failed") or []),
        "params": {"expense_change_ratio": 0.03, "below_gross_mode": "detail"},
        "block_yuan": (det_out.value if det_out is not None else None),
        "rules": {c.get("label"): {"rule": c.get("rule"), "value": c.get("value")}
                  for c in ((det_out.components or ()) if det_out is not None else ())},
        "pretax_profit_yuan": det_diag.get("pretax_profit_yuan"),
        "income_tax_yuan": det_diag.get("income_tax_yuan"),
        "tax_rate": det_diag.get("tax_rate"),
        "tax_rate_source": det_diag.get("tax_rate_source"),
        "residual_yuan": det_diag.get("residual_yuan"),
        "items_missing": det_diag.get("items_missing"),
    }

    # ④ 现金侧单项敏感性：ΔOCF 桥里"某一项增量减半"会怎样（会计口径推演）
    cash = fa.run("cash_reconciliation", ds)
    chg = next((o for o in (cash.outputs or [])
                if o.metric == "operating_cashflow_change"), None)
    items = dict(((cash.outputs[0].diagnostics or {}).get("cash_change_items") or {})) \
        if cash.outputs else {}
    wc = {r["metric"]: r["delta_yuan"] for r in (items.get("working_capital") or ())}
    d_ocf = float(chg.value) if chg is not None else None
    lever = spec["cash_lever"]
    half = (d_ocf - 0.5 * wc.get(lever, 0.0)) if d_ocf is not None else None
    out["cash_single_item"] = {
        "status": str(cash.status),
        "d_ocf_yuan": d_ocf,
        "working_capital_items": wc,
        "lever_metric": lever,
        "if_lever_half_yuan": half,
        "note": ("单项敏感性：假设该项增量减半、其它不变时 ΔOCF 变为多少——"
                 "会计口径推演，不是现金流预测；能否持续要看结算条款与后续报表"),
    }
    print(f"{name}: 收入侧 {out['profit_levers']['revenue_growth_to_hold_target_pct']}% / "
          f"毛利率阈值 {out['profit_levers']['margin_threshold_to_hold_base_profit_pct']}% / "
          f"ΔOCF {d_ocf} / 应付减半后 {half}")
    return out


def main() -> int:
    report = {"batch": "V2", "case": "两条业务杠杆 + 费用/税项假设 + 现金单项敏感性",
              "cases": {}}
    for name, spec in CASES.items():
        report["cases"][name] = _case(name, spec)
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"证据：{os.path.relpath(EVIDENCE, ROOT)}")
    ok = all(c["profit_levers"]["status"] == "validated"
             and c["cost_tax_assumption"]["status"] == "validated"
             and c["cash_single_item"]["status"] == "validated"
             for c in report["cases"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
