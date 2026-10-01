# -*- coding: utf-8 -*-
"""W0 证据：情景求值器的**经济效果**（手算例子 + 洋河/三一先验），不写死公司结论。

三件事必须能被独立复算（阶段W §3 验收）：

1. **基期残差只算一次并冻结**：detail 模式明细齐备时残差为 0；改税率/费用/少数股东假设，
   残差**不动**，动的是最终归母净利；
2. **假设真正改变最终利润**：`tax_rate`、`minority_share`、`expense_change_ratio` 各自
   改变情景归母净利（旧实现把它们抵在残差里，利润不变）；
3. **正算与两个阈值共用同一求值器**：把反推出的收入变化/毛利率代回 `evaluate()`，
   应恢复同一目标归母净利。

产出 `docs/evidence/w0_scenario_evaluator.json`。手算例子用纯内存数据（不依赖任何公司），
公司先验用缓存年报（正常入口抽取），数字全部来自运行输出。
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
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "w0_scenario_evaluator.json")

CASES = {
    "洋河股份": {"entity_id": "002304.SZ", "code": "002304",
                 "doc": "evals/a2_official_chain_20260929/002304/project/materials/"
                        "f42e747c73850b59/doc.json"},
    "三一重工": {"entity_id": "600031.SH", "code": "600031",
                 "doc": "evals/a2_official_chain_20260929/600031/project/materials/"
                        "0c3e82056e977adc/doc.json"},
}

# 手算例子（元）：收入 1000、毛利 800、归母净利 300、合并净利 320、所得税 80、少数股东 20、
# 随收入变化的税附加 100 与销售费用 200、固定金额的管理费用 50。
HAND_ROWS = (("revenue", 1000.0), ("gross_profit", 800.0), ("net_profit", 300.0),
             ("net_profit_consolidated", 320.0), ("income_tax_expense", 80.0),
             ("minority_interest", 20.0), ("taxes_and_surcharges", 100.0),
             ("selling_expense", 200.0), ("admin_expense", 50.0))
HAND_EXPECT = {
    # 手算：残差 = 300 − [800 + (−100−200−50)] + 80 + 20 = −50（明细齐备下由披露恒等式得出）
    "residual_yuan": -50.0,
    "tax25_minority10_net_profit": 270.0,     # 税前 400；税 100；合并 300；少数 30 → 270
    "expense_10pct_net_profit": 296.25,       # 管理费 55 → 税前 395；税 79；合并 316；少数 19.75
    "revenue_10pct_net_profit": 337.5,        # 收入 1100 → 毛利 880；随收入项 330 → 税前 450
    "target_450_margin_pct": 100.0,           # 750Δm + 300 = 450 → Δm = +20pp → 100%
    "target_450_revenue_growth_pct": 40.0,    # 375(1+g) − 75 = 450 → g = +40%
}


def _hand_case() -> dict:
    """手算例子（纯内存，不依赖公司材料）：五组可手算的读数必须逐个对上。"""
    import financial_analysis as fa
    import facts as F

    rows = [{"fact_id": f"hand-{m}", "entity": "手算示例", "entity_id": "000000.SZ",
             "market": "cn", "metric": m, "metric_label": m, "period": "2024年",
             "period_type": "年报", "currency": "CNY", "unit": "元", "caliber": "合并",
             "value": v, "source_url": "https://example.invalid/hand",
             "source_hash": "hand", "verify_state": "unverified"}
            for m, v in HAND_ROWS]
    ds = fa.freeze_from_facts(rows, periods=(2024,), entity="手算示例",
                              entity_id="000000.SZ", as_of="2025-04-30",
                              source_label="hand")

    def run(**params):
        p = {"revenue_growth": 0.0, "gross_margin_delta": 0.0,
             "below_gross_mode": "detail"}
        p.update(params)
        r = fa.run("scenario_sensitivity", ds, params=p)
        return r

    out: dict = {"dataset": {"observations": ds.manifest.observations,
                             "dataset_hash": ds.manifest.dataset_hash},
                 "rows_yuan": dict(HAND_ROWS), "checks": {}}
    base = run()
    diag = base.outputs[0].diagnostics
    bg = diag["below_gross"]
    out["checks"]["status"] = str(base.status)
    out["checks"]["base_reproduction_gap"] = diag["base_reproduction_gap"]
    out["checks"]["residual_yuan"] = bg["residual_yuan"]
    out["checks"]["residual_frozen_after_assuming"] = None      # 下面填
    out["checks"]["base_pretax_yuan"] = bg["pretax_profit_base_yuan"]

    tax_min = run(tax_rate=0.25, minority_share=0.10)
    tm = tax_min.outputs[0].diagnostics["below_gross"]
    out["checks"]["tax25_minority10_net_profit"] = tax_min.outputs[0].value
    out["checks"]["tax25_minority10_income_tax_yuan"] = tm["income_tax_yuan"]
    out["checks"]["tax25_minority10_minority_yuan"] = tm["minority_interest_yuan"]
    out["checks"]["tax25_minority10_residual_yuan"] = tm["residual_yuan"]

    exp = run(expense_change_ratio=0.10)
    out["checks"]["expense_10pct_net_profit"] = exp.outputs[0].value
    rev = run(revenue_growth=0.10)
    out["checks"]["revenue_10pct_net_profit"] = rev.outputs[0].value

    tgt = run(target_net_profit=450.0)
    th = tgt.outputs[0].diagnostics["thresholds"]
    out["checks"]["target_450_margin_pct"] = th["margin_threshold"]
    out["checks"]["target_450_revenue_growth_pct"] = th["revenue_growth_threshold"]
    out["checks"]["threshold_method"] = th["method"]
    out["checks"]["threshold_reachable"] = th["reachable"]
    out["checks"]["threshold_target_source"] = th["target_source"]

    # 两个阈值**代回同一求值器**：应恢复同一目标 450 元
    from financial_analysis.operators import scenario as SC
    ctx = SC.base_context(ds, ds.period_at(0))
    args = {"mode": "detail", "expense_ratio": SC._d(0.0), "tax_rate": None,
            "minority_share": None}
    m_back = SC.evaluate(ctx, growth=0,
                         margin_delta=SC._d(th["margin_threshold"]) / 100 - ctx["margin"],
                         **args)["net_profit"]
    g_back = SC.evaluate(ctx, growth=SC._d(th["revenue_growth_threshold"]) / 100,
                         margin_delta=0, **args)["net_profit"]
    out["checks"]["margin_threshold_back_to_target_yuan"] = float(m_back)
    out["checks"]["revenue_threshold_back_to_target_yuan"] = float(g_back)
    out["checks"]["residual_frozen_after_assuming"] = tm["residual_yuan"]
    out["expect"] = dict(HAND_EXPECT)
    out["matched"] = {
        "residual_yuan": float(bg["residual_yuan"]) == HAND_EXPECT["residual_yuan"],
        "tax25_minority10_net_profit":
            float(tax_min.outputs[0].value) == HAND_EXPECT["tax25_minority10_net_profit"],
        "expense_10pct_net_profit":
            float(exp.outputs[0].value) == HAND_EXPECT["expense_10pct_net_profit"],
        "revenue_10pct_net_profit":
            float(rev.outputs[0].value) == HAND_EXPECT["revenue_10pct_net_profit"],
        "target_450_margin_pct":
            abs(float(th["margin_threshold"]) - HAND_EXPECT["target_450_margin_pct"]) < 1e-6,
        "target_450_revenue_growth_pct":
            abs(float(th["revenue_growth_threshold"])
                - HAND_EXPECT["target_450_revenue_growth_pct"]) < 1e-6,
        "both_thresholds_back_to_target": (
            abs(float(m_back) - 450.0) < 0.01 and abs(float(g_back) - 450.0) < 0.01),
        "residual_stays_after_assuming":
            float(tm["residual_yuan"]) == float(bg["residual_yuan"]),
    }
    print("手算例子：" + "、".join(f"{k}={'OK' if v else 'FAIL'}"
                                  for k, v in out["matched"].items()))
    return out


def _case(name: str, spec: dict) -> dict:
    """真实材料先验（洋河/三一）：目标＝上一期归母净利，fixed 与 detail 两种模式各给一套。"""
    import financial_analysis as fa
    import facts as F
    from financial_analysis.operators import scenario as SC

    doc = json.load(open(os.path.join(ROOT, spec["doc"]), encoding="utf-8"))
    facts = F.facts_from_annual_tables(doc, company=name, company_code=spec["code"],
                                       periods=(2023, 2024), as_of="2025-04-30",
                                       disclosed_at="2025-04-28")
    ds = fa.freeze_from_facts(facts, periods=(2023, 2024), entity=name,
                              entity_id=spec["entity_id"], as_of="2025-04-30",
                              source_label="w0")
    ctx = SC.base_context(ds, ds.period_at(0))
    np_prev = ds.get("net_profit", "2023年")
    np_cur = ds.get("net_profit", "2024年")
    target = float(np_prev.value)
    out: dict = {"entity_id": spec["entity_id"],
                 "dataset": {"observations": ds.manifest.observations,
                             "usable": ds.manifest.usable,
                             "dataset_hash": ds.manifest.dataset_hash},
                 "readings": {"net_profit_prev_yuan": float(np_prev.value),
                              "net_profit_cur_yuan": float(np_cur.value),
                              "revenue_cur_yuan": float(ctx["revenue"]),
                              "gross_profit_cur_yuan": float(ctx["gross_profit"]),
                              "margin_base": float(ctx["margin"])},
                 "modes": {}}
    for mode in ("fixed", "detail"):
        params = {"revenue_growth": 0.0, "gross_margin_delta": 0.0,
                  "below_gross_mode": mode, "target_net_profit": target}
        run = fa.run("scenario_sensitivity", ds, params=params)
        diag = run.outputs[0].diagnostics if run.outputs else {}
        th = dict(diag.get("thresholds") or {})
        bg = dict(diag.get("below_gross") or {})
        args = {"mode": mode, "expense_ratio": SC._d(0.0), "tax_rate": None,
                "minority_share": None}
        m_back = g_back = None
        if th.get("margin_threshold") is not None:
            m_back = float(SC.evaluate(
                ctx, growth=0,
                margin_delta=SC._d(th["margin_threshold"]) / 100 - ctx["margin"],
                **args)["net_profit"])
        if th.get("revenue_growth_threshold") is not None:
            g_back = float(SC.evaluate(
                ctx, growth=SC._d(th["revenue_growth_threshold"]) / 100,
                margin_delta=0, **args)["net_profit"])
        out["modes"][mode] = {
            "status": str(run.status),
            "validation_failed": list((run.validation or {}).get("failed") or []),
            "base_reproduction_gap": diag.get("base_reproduction_gap"),
            "target_net_profit_yuan": target,
            "target_source": th.get("target_source"),
            "revenue_growth_to_hold_target_pct": th.get("revenue_growth_threshold"),
            "margin_threshold_pct": th.get("margin_threshold"),
            "margin_gap_pp": th.get("margin_gap_pp"),
            "margin_base_pct": (float(ctx["margin"]) * 100),
            "method": th.get("method"),
            "reachable": th.get("reachable"),
            "within_allowed_range": th.get("within_allowed_range"),
            "threshold_notes": th.get("threshold_notes"),
            # 代回同一求值器 → 应恢复同一目标
            "margin_threshold_back_to_target_yuan": m_back,
            "revenue_threshold_back_to_target_yuan": g_back,
            "back_to_target_ok": (m_back is not None and g_back is not None
                                  and abs(m_back - target) < 0.01
                                  and abs(g_back - target) < 0.01),
            "below_gross": {
                "residual_yuan": bg.get("residual_yuan"),
                "residual_frozen": bg.get("residual_frozen"),
                "pretax_profit_base_yuan": bg.get("pretax_profit_base_yuan"),
                "pretax_profit_yuan": bg.get("pretax_profit_yuan"),
                "income_tax_base_yuan": bg.get("income_tax_base_yuan"),
                "income_tax_yuan": bg.get("income_tax_yuan"),
                "tax_rate": bg.get("tax_rate"),
                "tax_rate_source": bg.get("tax_rate_source"),
                "minority_base_yuan": bg.get("minority_base_yuan"),
                "minority_interest_yuan": bg.get("minority_interest_yuan"),
                "minority_share": bg.get("minority_share"),
                "minority_source": bg.get("minority_source"),
                "consolidated_disclosed_gap_yuan": bg.get("consolidated_disclosed_gap_yuan"),
                "below_gross_net_yuan": bg.get("below_gross_net_yuan"),
                "items_used": bg.get("items_used"),
                "items_missing": bg.get("items_missing"),
            },
        }
    # ② 假设**真正**改变最终利润（detail 模式）：三组假设各改一次，看归母净利变化
    effects: dict = {}
    for label, extra in (("费用 +3%", {"expense_change_ratio": 0.03}),
                         ("税率 25%", {"tax_rate": 0.25}),
                         ("少数股东占比 10%", {"minority_share": 0.10})):
        params = {"revenue_growth": 0.0, "gross_margin_delta": 0.0,
                  "below_gross_mode": "detail"}
        base_run = fa.run("scenario_sensitivity", ds, params=dict(params))
        run = fa.run("scenario_sensitivity", ds, params=dict(params, **extra))
        if run.outputs and base_run.outputs:
            d = run.outputs[0].diagnostics["below_gross"]
            effects[label] = {
                "net_profit_base_yuan": base_run.outputs[0].value,
                "net_profit_yuan": run.outputs[0].value,
                "net_profit_delta_yuan": run.outputs[0].value - base_run.outputs[0].value,
                "residual_yuan": d.get("residual_yuan"),
                "income_tax_yuan": d.get("income_tax_yuan"),
                "minority_interest_yuan": d.get("minority_interest_yuan"),
                "pretax_profit_yuan": d.get("pretax_profit_yuan"),
                "status": str(run.status),
            }
    out["assumption_effects"] = effects
    fl = out["modes"]["fixed"]
    print(f"{name}: fixed 收入 {fl['revenue_growth_to_hold_target_pct']}% / "
          f"毛利率 {fl['margin_threshold_pct']}% / 代回 {'OK' if fl['back_to_target_ok'] else 'FAIL'}"
          f" / detail 残差 {out['modes']['detail']['below_gross']['residual_yuan']}")
    return out


def main() -> int:
    report = {"batch": "W0", "case": "情景求值器：正确符号 + 冻结残差 + 同一目标/求值器",
              "hand": _hand_case(), "cases": {}}
    for name, spec in CASES.items():
        report["cases"][name] = _case(name, spec)
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"证据：{os.path.relpath(EVIDENCE, ROOT)}")
    ok = all(report["hand"]["matched"].values())
    ok = ok and all(m["status"] == "validated" and m["back_to_target_ok"]
                    for c in report["cases"].values() for m in c["modes"].values())
    ok = ok and all(abs(v["net_profit_delta_yuan"]) > 0.01
                    for c in report["cases"].values()
                    for v in c["assumption_effects"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
