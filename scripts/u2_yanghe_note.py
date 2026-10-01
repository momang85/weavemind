# -*- coding: utf-8 -*-
"""U1/U2 成篇证据：洋河 2023/2024 经营驱动分析正文（4–6 页，正常入口，不写任何数字）。

走**正常入口**：年报 doc → 事实 → 冻结数据集 → 运行注册模型（经营驱动/现金桥/反向情景）
→ `financial_analysis.narrative.research_note` 装配成篇正文（含来源定位与三图引用）。
产出：

- `docs/evidence/u2_research_note.md`：正文（markdown，可直接读）；
- `docs/evidence/u2_research_note.json`：结构统计（字符/小节/引用的事实数/各段是否存在）。

脚本不写死财务数字：全部来自运行输出与事实层。
"""
from __future__ import annotations

import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NOTE_MD = os.path.join(ROOT, "docs", "evidence", "u2_research_note.md")
NOTE_JSON = os.path.join(ROOT, "docs", "evidence", "u2_research_note.json")
DOC_PATH = os.path.join(ROOT, "evals", "a2_official_chain_20260929", "002304",
                        "project", "materials", "f42e747c73850b59", "doc.json")
CHARTS = os.path.join(ROOT, "docs", "evidence", "u2_charts.json")

VARIANTS = (("收入持平", {"revenue_growth": 0.0, "gross_margin_delta": 0.0}),
            ("收入 +5%", {}),
            ("收入 −12.83%", {"revenue_growth": -0.1283, "gross_margin_delta": 0.0}))


def main() -> int:
    import financial_analysis as fa
    import facts as F

    doc = json.load(open(DOC_PATH, encoding="utf-8"))
    facts = F.facts_from_annual_tables(doc, company="洋河股份", company_code="002304",
                                       periods=(2023, 2024), as_of="2025-04-30",
                                       disclosed_at="2025-04-28")
    ds = fa.freeze_from_facts(facts, periods=(2023, 2024), entity="洋河股份",
                              entity_id="002304.SZ", as_of="2025-04-30",
                              source_label="evals:a2_official_chain_20260929/002304")
    od_run = fa.run("operating_drivers", ds)
    cash_run = fa.run("cash_reconciliation", ds)
    scen_runs = [(label, fa.run("scenario_sensitivity", ds, params=params))
                 for label, params in VARIANTS]
    for label, r in scen_runs:
        print(f"情景 {label}: {r.status}")
    print(f"运行：operating_drivers={od_run.status} cash_reconciliation={cash_run.status}")
    if str(od_run.status) != "validated" or str(cash_run.status) != "validated":
        print("运行未通过验证，正文不装配")
        return 1

    charts = []
    if os.path.exists(CHARTS):
        charts = [f.get("spec") for f in (json.load(open(CHARTS, encoding="utf-8"))
                                          .get("figures") or [])]
    runs = [od_run, cash_run] + [r for _label, r in scen_runs]
    prov = fa.narrative.provenance_from_facts(facts, label_of=F.metric_label)
    note = fa.narrative.research_note(runs, provenance=prov, charts=charts,
                                      label_of=F.metric_label)
    with open(NOTE_MD, "w", encoding="utf-8") as f:
        f.write(note)
    sections = re.findall(r"^### (.+)$", note, re.M)
    table_rows = len([l for l in note.splitlines() if l.startswith("| ")])
    fact_ids = sorted({m for m in re.findall(r"`(fact-[^`]+)`", note)})
    stats = {
        "case": "洋河股份 002304 2023/2024 经营驱动分析正文（成篇）",
        "source_material": os.path.relpath(DOC_PATH, ROOT).replace("\\", "/"),
        "dataset": {"observations": ds.manifest.observations,
                    "usable": ds.manifest.usable,
                    "conflicts": list(ds.manifest.conflicts or ()),
                    "dataset_hash": ds.manifest.dataset_hash},
        "runs": {"operating_drivers": {"run_id": od_run.run_id,
                                       "status": str(od_run.status)},
                 "cash_reconciliation": {"run_id": cash_run.run_id,
                                         "status": str(cash_run.status)},
                 "scenario_sensitivity": [
                     {"label": label, "run_id": r.run_id, "status": str(r.status),
                      "params": dict(r.params or {})} for label, r in scen_runs]},
        "note": {"path": os.path.relpath(NOTE_MD, ROOT).replace("\\", "/"),
                 "chars": len(note),
                 "lines": len(note.splitlines()),
                 "sections": sections,
                 "table_rows": table_rows,
                 "facts_with_locator": len(fact_ids),
                 "fact_ids_sample": fact_ids[:6],
                 "figures_referenced": len([c for c in charts if c]),
                 "unavailable_models": [m for m, r in
                                        (("operating_drivers", od_run),
                                         ("cash_reconciliation", cash_run))
                                        if str(r.status) != "validated"]},
        "note_text": note,
    }
    with open(NOTE_JSON, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(f"正文 {len(note)} 字符 / {len(note.splitlines())} 行 / {len(sections)} 小节 / "
          f"来源表行 {table_rows} / 带定位事实 {len(fact_ids)} / 引用图 "
          f"{stats['note']['figures_referenced']}")
    print("小节：" + " | ".join(sections))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
