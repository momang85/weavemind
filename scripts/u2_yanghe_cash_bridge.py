# -*- coding: utf-8 -*-
"""U2 案例证据：洋河 2023/2024 现金调节桥 + 正文段落（官方入口，不写任何数字）。

产出 `docs/evidence/u2_yanghe_cash_bridge.json`：事实规模、调节表闭合、分组贡献、
最大支撑/拖累、缺口变化，以及报告链渲染出的分析卡段落。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "u2_yanghe_cash_bridge.json")
DOC_PATH = os.path.join(ROOT, "evals", "a2_official_chain_20260929", "002304",
                        "project", "materials", "f42e747c73850b59", "doc.json")


def main() -> int:
    import financial_analysis as fa
    import facts as F
    import report_brief
    import workspace as ws_mod
    from adapters import cashflow_supplement_tables as cst
    from financial_analysis import store as fa_store

    doc = json.load(open(DOC_PATH, encoding="utf-8"))
    sup = cst.extract_cashflow_supplement(doc, company="洋河股份",
                                         company_code="002304", periods=(2023, 2024))
    print(f"补充资料抽取：ok={sup['ok']} 事实={len(sup['facts'])} "
          f"交叉核对={[c['metric'] for c in sup['cross_checks']]} "
          f"被拒={len(sup['rejected'])}")
    fs = F.facts_from_annual_tables(doc, company="洋河股份", company_code="002304",
                                    periods=(2023, 2024), as_of="2025-04-30",
                                    disclosed_at="2025-04-28")
    ds = fa.freeze_from_facts(fs, periods=(2023, 2024), entity="洋河股份",
                              entity_id="002304.SZ", as_of="2025-04-30",
                              source_label="evals:a2_official_chain_20260929/002304")
    print(f"冻结：观察 {ds.manifest.observations} 可用 {ds.manifest.usable} "
          f"冲突 {len(ds.manifest.conflicts or ())}")
    run = fa.run("cash_reconciliation", ds)
    print(f"运行：{run.status} failed={run.validation.get('failed')}")
    outputs = {}
    for o in run.outputs:
        outputs.setdefault(o.metric, []).append({
            "value": o.value, "unit": o.unit, "output_period": o.output_period,
            "label": o.label,
            "components": [{"component_id": c.get("component_id"),
                            "label": c.get("label"), "value": c.get("value"),
                            "unit": c.get("unit")} for c in (o.components or ())]})
        print(f"  {o.metric:44} {o.value:>20,.2f}")
    diag = dict((run.outputs[0].diagnostics or {})) if run.outputs else {}
    brief = ""
    tmp = Path(tempfile.mkdtemp(prefix="u2_evidence_"))
    old = ws_mod.WORKSPACE_ROOT
    try:
        ws_mod.configure_workspace_root(str(tmp))
        tid = "u2-evidence"
        ws = ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        fa_store.save_run(ws, run)
        brief = report_brief._analysis_card_block(tid, ws_dir=ws)
    finally:
        ws_mod.WORKSPACE_ROOT = old
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"正文（分析卡）{len(brief)} 字符")
    report = {
        "case": "洋河股份 002304 2023→2024 现金调节桥（补充资料，官方入口）",
        "source_material": os.path.relpath(DOC_PATH, ROOT).replace("\\", "/"),
        "supplement_extraction": {
            "ok": sup["ok"], "facts": len(sup["facts"]),
            "periods": list(sup.get("periods") or ()),
            "cross_checks": sup["cross_checks"],
            "rejected": sup["rejected"],
        },
        "dataset": {"observations": ds.manifest.observations,
                    "usable": ds.manifest.usable,
                    "conflicts": list(ds.manifest.conflicts or ()),
                    "dataset_hash": ds.manifest.dataset_hash},
        "run": {"model_id": "cash_reconciliation", "status": str(run.status),
                "validation_failed": list(run.validation.get("failed") or []),
                "checks": {k: v.get("ok") for k, v
                           in (run.validation.get("checks") or {}).items()},
                "outputs": outputs},
        "diagnostics": {
            k: diag.get(k) for k in
            ("unit", "reconciliation", "largest_support", "largest_drag",
             "items_missing", "closure_note")
        },
        "brief_analysis_card": brief[:8000],
        "brief_analysis_card_chars": len(brief),
    }
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"证据：{os.path.relpath(EVIDENCE, ROOT)}")
    ok = (run.status == fa.contracts.RunStatus.VALIDATED
          and not (run.validation.get("failed") or []))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
