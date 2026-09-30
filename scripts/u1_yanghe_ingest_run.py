# -*- coding: utf-8 -*-
"""U1 摄取＋产品路径复跑：洋河缓存年报 → 事实 → 冻结数据集 → operating_drivers。

与 `scripts/u1_yanghe_drivers.py`（手写常量试算）的区别：本脚本**不写任何数字**，
全部走现役抽取器与算子链，用来证明"报告链自己算得出这张桥"。

输出：`docs/evidence/u1_yanghe_ingest_run.json`（事实清单、被拒原因统计、运行输出与验证结论）。
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "u1_yanghe_ingest_run.json")
DOC_PATH = os.path.join(ROOT, "evals", "a2_official_chain_20260929", "002304",
                        "project", "materials", "f42e747c73850b59", "doc.json")
LINE_SLUGS = (
    "taxes_and_surcharges", "selling_expense", "admin_expense", "rd_expense",
    "finance_expense", "other_income", "investment_income", "fair_value_change",
    "credit_impairment", "asset_impairment", "asset_disposal_income",
    "non_operating_income", "non_operating_expense", "income_tax_expense",
    "minority_interest",
)


def main() -> int:
    import financial_analysis as fa
    import facts as F

    with open(DOC_PATH, encoding="utf-8") as f:
        doc = json.load(f)
    # **正常入口**：官方年报原文 → 事实（三张报表 + MD&A 经营明细表）
    fs = F.facts_from_annual_tables(doc, company="洋河股份", company_code="002304",
                                    periods=(2023, 2024), as_of="2025-04-30",
                                    disclosed_at="2025-04-28")
    print(f"事实：{len(fs)} 条；指标 {len({x.metric for x in fs})} 个")
    seg = [x for x in fs if str(x.caliber).startswith(
        ("分产品:", "分地区:", "分行业:", "分销售模式:"))]
    derived = [x for x in seg if x.derived_from]
    print(f"分段/量价事实：{len(seg)} 条（其中推算输入 {len(derived)} 条）；"
          f"口径 {sorted({str(x.caliber) for x in seg})}")

    ds = fa.freeze_from_facts(fs, periods=(2023, 2024), entity="洋河股份",
                              entity_id="002304.SZ", market="cn", as_of="2025-04-30",
                              source_label="evals:a2_official_chain_20260929/002304")
    print(f"冻结：期间={ds.manifest.periods} 观察={ds.manifest.observations} "
          f"可用={ds.manifest.usable} 冲突={len(ds.manifest.conflicts or ())} "
          f"hash={ds.manifest.dataset_hash[:12]}…")

    consolidated = {}
    for metric in ("revenue", "operating_cost", "net_profit",
                   "net_profit_consolidated") + LINE_SLUGS:
        row = {}
        for period in ("2023年", "2024年"):
            o = ds.get(metric, period)
            if o is not None:
                row[period] = {"value": o.value, "unit": o.unit,
                               "caliber": o.caliber,
                               "fact_id": o.fact_id}
        consolidated[metric] = row
    missing = [m for m, r in consolidated.items() if len(r) < 2]
    print(f"合并口径取到两期的指标：{len(consolidated) - len(missing)}/"
          f"{len(consolidated)}；缺：{missing}")

    run = fa.run("operating_drivers", ds)
    print(f"运行：{run.status} {run.reason or ''}")
    print(f"验证：failed={run.validation.get('failed')}")
    outputs = {}
    for o in run.outputs:
        outputs.setdefault(o.metric, []).append({
            "value": o.value, "unit": o.unit, "output_period": o.output_period,
            "label": o.label,
            "components": [{"component_id": c.get("component_id"),
                            "label": c.get("label"), "value": c.get("value"),
                            "unit": c.get("unit")} for c in (o.components or [])],
        })
        print(f"  {o.metric:34} {o.value:>22,.2f} {o.unit} ({o.label[:18]})")
    diag = {}
    try:
        diag = dict(run.outputs[0].diagnostics or {})
    except Exception:
        pass
    report = {
        "case": "洋河股份 002304 2023→2024 经营驱动桥（官方入口→事实→冻结→算子）",
        "source_material": os.path.relpath(DOC_PATH, ROOT).replace("\\", "/"),
        "facts": {
            "count": len(fs), "metrics": sorted({x.metric for x in fs}),
            "segment_facts": len(seg), "derived_segment_facts": len(derived),
            "segment_calibers": sorted({str(x.caliber) for x in seg}),
        },
        "dataset": {
            "periods": list(ds.manifest.periods),
            "observations": ds.manifest.observations,
            "usable": ds.manifest.usable,
            "conflicts": list(ds.manifest.conflicts or ()),
            "dataset_hash": ds.manifest.dataset_hash,
        },
        "consolidated_inputs": consolidated,
        "missing_metrics": missing,
        "run": {
            "model_id": "operating_drivers", "status": str(run.status),
            "reason": run.reason or "",
            "validation_failed": list(run.validation.get("failed") or []),
            "checks": {k: v.get("ok") for k, v in (run.validation.get("checks") or {}).items()},
            "outputs": outputs,
        },
        "diagnostics_subset": {
            k: diag.get(k) for k in
            ("gross_margin", "closure", "line_items_used", "line_items_missing",
             "line_items_rejected", "unexplained_residual_yuan", "segments_used",
             "segments_skipped", "volume_price_caliber")
        },
    }
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"证据：{os.path.relpath(EVIDENCE, ROOT)}")
    ok = (run.status == fa.contracts.RunStatus.VALIDATED and not missing
          and not (run.validation.get("failed") or []))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
