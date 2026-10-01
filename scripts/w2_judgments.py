# -*- coding: utf-8 -*-
"""W2 证据：**可检验的研究判断**（洋河／三一，正常入口抽取 + 已验证运行 + 已准入披露）。

每家只做最重要的判断（阶段W §5）：判断 → 数字与贡献 → 原句/原件位置 → 支持边界 →
本公司替代解释 → 后续指标与反转条件 → 缺口。规则由 `financial_analysis.judgments` 提供，
**不按公司名硬编码结论**：读数缺就如实写缺口（宁可少一条，也不编一条）。

产出：
- `docs/evidence/w2_judgments.json`：两家的判断全文（结构）+ 读数摘要；
- `docs/evidence/w2_yanghe_judgments.md` / `w2_sany_judgments.md`：单独可读的判断段。
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
EVIDENCE = os.path.join(ROOT, "docs", "evidence", "w2_judgments.json")

CASES = {
    "洋河股份": {"entity_id": "002304.SZ", "code": "002304",
                 "doc": "evals/a2_official_chain_20260929/002304/project/materials/"
                        "f42e747c73850b59/doc.json"},
    "三一重工": {"entity_id": "600031.SH", "code": "600031",
                 "doc": "evals/a2_official_chain_20260929/600031/project/materials/"
                        "0c3e82056e977adc/doc.json"},
}


def _case(name: str, spec: dict) -> dict:
    import financial_analysis as fa
    import facts as F
    import narrative_evidence as ne
    from financial_analysis import judgments as jd

    doc = json.load(open(os.path.join(ROOT, spec["doc"]), encoding="utf-8"))
    facts = F.facts_from_annual_tables(doc, company=name, company_code=spec["code"],
                                       periods=(2023, 2024), as_of="2025-04-30",
                                       disclosed_at="2025-04-28")
    ds = fa.freeze_from_facts(facts, periods=(2023, 2024), entity=name,
                              entity_id=spec["entity_id"], as_of="2025-04-30",
                              source_label="w2")
    runs = {mid: fa.run(mid, ds) for mid in ("operating_drivers", "cash_reconciliation",
                                             "scenario_sensitivity")}
    vp = ne.extract_volume_price([doc], periods=[2023, 2024])
    records = [{"section": str(r.get("section") or ""),
                "snippet": str(r.get("snippet") or ""),
                "locator": str(r.get("locator") or "")}
               for r in (ne.extract_sections(doc, periods=(2023, 2024), company=name,
                                             company_id=spec["entity_id"],
                                             as_of="2025-04-30") or [])]
    js = jd.research_judgments(list(runs.values()), volume_price=vp, records=records,
                               limit=4)
    out = {"entity_id": spec["entity_id"],
           "dataset": {"observations": ds.manifest.observations,
                       "dataset_hash": ds.manifest.dataset_hash},
           "runs": {k: {"status": str(v.status),
                        "validation_failed": list((v.validation or {}).get("failed") or [])}
                    for k, v in runs.items()},
           "volume_price_ok": bool(vp.get("ok")),
           "volume_price_components": [{"component": c.get("component"),
                                        "state": c.get("state")}
                                       for c in (vp.get("components") or [])],
           "records_admitted": len(records),
           "judgments": js,
           "judgment_text": "\n".join(jd.render_judgments(js))}
    md = os.path.join(ROOT, "docs", "evidence",
                      f"w2_{'yanghe' if '洋河' in name else 'sany'}_judgments.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write(f"# {name} 研究判断（W2，规则驱动；原句与位置见各条）\n\n"
                + out["judgment_text"])
    print(f"{name}: 判断 {len(js)} 条 —— "
          + "；".join(f"{j['judgment_id']}" for j in js))
    return out


def main() -> int:
    report = {"batch": "W2", "case": "可检验的研究判断（判断→数字→原句/位置→边界→替代→反转→缺口）",
              "cases": {}}
    for name, spec in CASES.items():
        report["cases"][name] = _case(name, spec)
    os.makedirs(os.path.dirname(EVIDENCE), exist_ok=True)
    with open(EVIDENCE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"证据：{os.path.relpath(EVIDENCE, ROOT)}")
    # 洋河是本批的第一家公司：它必须给出"量/价/库存/现金"里至少 3 条判断
    yh = report["cases"].get("洋河股份") or {}
    ok = (yh.get("runs", {}).get("operating_drivers", {}).get("status") == "validated"
          and len(yh.get("judgments") or []) >= 3
          and all(j.get("watch") for j in (yh.get("judgments") or [])))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
