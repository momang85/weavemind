# -*- coding: utf-8 -*-
"""U3 案例证据：同一主线复制到三一重工（并与洋河对照）。

不写任何数字：全部走 `facts.facts_from_annual_tables` 与现役算子链。
产出 `docs/evidence/u3_sany_same_mainline.json`：两家的
① 事实/分段覆盖 ② 利润驱动读数 ③ 现金调节桥（是否闭合、最大支撑/拖累）④ 反向阈值。
"""
from __future__ import annotations

import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "u3_sany_same_mainline.json")

CASES = {
    "洋河股份": {
        "doc": os.path.join(ROOT, "evals", "a2_official_chain_20260929", "002304",
                            "project", "materials", "f42e747c73850b59", "doc.json"),
        "entity_id": "002304.SZ",
    },
    "三一重工": {
        "doc": os.path.join(ROOT, "evals", "a2_official_chain_20260929", "600031",
                            "project", "materials", "0c3e82056e977adc", "doc.json"),
        "entity_id": "600031.SH",
    },
}


def _case(name: str, spec: dict) -> dict:
    import financial_analysis as fa
    import facts as F
    from adapters import cashflow_supplement_tables as cst
    from adapters import operating_detail_tables as odt

    doc = json.load(open(spec["doc"], encoding="utf-8"))
    sup = cst.extract_cashflow_supplement(doc, company=name,
                                         company_code=spec["entity_id"],
                                         periods=(2023, 2024))
    det = odt.extract_operating_detail(doc, company=name,
                                      company_code=spec["entity_id"],
                                      periods=(2023, 2024))
    fs = F.facts_from_annual_tables(doc, company=name,
                                    company_code=spec["entity_id"],
                                    periods=(2023, 2024), as_of="2025-04-30",
                                    disclosed_at="2025-04-28")
    seg = [x for x in fs if str(x.caliber).startswith(
        ("分产品:", "分地区:", "分行业:", "分销售模式:"))]
    ds = fa.freeze_from_facts(fs, periods=(2023, 2024), entity=name,
                              entity_id=spec["entity_id"], as_of="2025-04-30",
                              source_label=f"evals:{spec['entity_id']}")
    out = {
        "entity_id": spec["entity_id"],
        "facts": {"total": len(fs), "metrics": sorted({x.metric for x in fs}),
                  "segments": len(seg),
                  "segment_calibers": sorted({str(x.caliber) for x in seg}),
                  "supplement_facts": len(sup["facts"]),
                  "supplement_ok": sup["ok"],
                  "supplement_rejected": sup["rejected"][:3],
                  "detail_ok": det["ok"],
                  "detail_duplicates": det.get("duplicates", [])[:3]},
        "dataset": {"periods": list(ds.manifest.periods),
                    "observations": ds.manifest.observations,
                    "usable": ds.manifest.usable,
                    "conflicts": len(ds.manifest.conflicts or ()),
                    "dataset_hash": ds.manifest.dataset_hash},
        "models": {},
    }
    for mid in ("operating_drivers", "cash_reconciliation"):
        run = fa.run(mid, ds)
        o = {x.metric: x.value for x in (run.outputs or [])}
        diag = dict((run.outputs[0].diagnostics or {})) if run.outputs else {}
        entry = {"status": str(run.status),
                 "validation_failed": list(run.validation.get("failed") or []),
                 "outputs": {k: v for k, v in o.items()
                             if k != "operating_cashflow_reconciliation_prev"}}
        if mid == "cash_reconciliation":
            entry["reconciliation"] = diag.get("reconciliation")
            entry["largest_support"] = diag.get("largest_support")
            entry["largest_drag"] = diag.get("largest_drag")
        else:
            entry["segments_used"] = diag.get("segments_used")
            entry["segments_skipped"] = (diag.get("segments_skipped") or [])[:6]
            entry["volume_price_caliber"] = diag.get("volume_price_caliber")
            entry["unexplained_residual_yuan"] = diag.get("unexplained_residual_yuan")
            entry["alternative_explanations"] = diag.get("alternative_explanations")
            entry["components"] = {
                x.metric: [{"component_id": c.get("component_id"),
                            "value": c.get("value")} for c in (x.components or [])]
                for x in (run.outputs or [])
                if x.metric in ("net_profit_change", "gross_profit_change")}
        out["models"][mid] = entry
    srun = fa.run("scenario_sensitivity", ds,
                  params={"revenue_growth": -0.1283, "gross_margin_delta": 0.0})
    th = ((srun.outputs[0].diagnostics or {}).get("thresholds")
          if srun.outputs else {})
    out["models"]["scenario_sensitivity"] = {
        "status": str(srun.status),
        "thresholds": {k: th.get(k) for k in
                       ("assumed_revenue", "margin_base", "margin_threshold",
                        "margin_gap_pp", "capital_per_day")},
    }
    # 注：f-string 里不写跨行表达式——本地 Python 3.14 允许（PEP 701），
    # CI 用 3.11 会直接编译失败（本脚本第一版就因此红过）。
    _rec = (out["models"]["cash_reconciliation"].get("reconciliation") or {}).get(
        "2024年", {})
    print(f"{name}: 事实 {len(fs)}（分段 {len(seg)}）"
          f" 现金桥 {out['models']['cash_reconciliation']['status']}"
          f" 残差 {_rec.get('residual_yuan')}")
    return out


def main() -> int:
    report = {"batch": "U3", "case": "同一主线复制到三一重工（并与洋河对照）",
              "cases": {}}
    for name, spec in CASES.items():
        report["cases"][name] = _case(name, spec)
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"证据：{os.path.relpath(EVIDENCE, ROOT)}")
    ok = all(c["models"]["operating_drivers"]["status"] == "validated"
             and c["models"]["cash_reconciliation"]["status"] == "validated"
             for c in report["cases"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
