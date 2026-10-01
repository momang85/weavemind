# -*- coding: utf-8 -*-
"""X1 首例：**洋河 2022–2024 研究续页**（较早 2023 年年报的假说 × 2024 年年报的同口径读数）。

阶段X §4 的算法（本脚本一步不省，全部由确定性算子/既有判断 ID 完成）：

1. 分别形成 2022→2023（2023 年年报）与 2023→2024（2024 年年报）两期 Dataset，
   复用经营驱动、现金桥与同一情景求值器；年份取原件期间（`periods` 显式给出）。
2. 从**较早**材料写并冻结最多三条持续性假说（沿用既有判断 ID 与它自己的观察条件）。
3. 用**较新**材料的同口径读数比较，确定性给出 加强／削弱／无法判断。
4. 历史观察与假说分开；先冻结旧材料也不包装成事前盲测（限制写进续页）。
5. 用现有 fixed/detail 结果解释一次**假设为什么重要**。
6. 把一页落进工作区 `analysis/continuation.json` —— 正常报告/版本出口随后渲染它。

产出：
- `docs/evidence/x1_yanghe_continuation.json`：续页结构 + 判定 + 核对项；
- `docs/evidence/x1_yanghe_continuation/continuation.json` / `.md`：交给正常装配的那一份。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                  # noqa: BLE001
    pass

CACHE = ROOT / "evals" / "a2_official_chain_20260929"
EARLIER_DOC = CACHE / "002304-2023" / "project" / "materials" / "77185ce8ec83f3bc" / "doc.json"
LATER_DOC = (CACHE / "002304" / "project" / "materials"
             / "f42e747c73850b59" / "doc.json")
OUT_DIR = ROOT / "docs" / "evidence" / "x1_yanghe_continuation"
OUT_JSON = ROOT / "docs" / "evidence" / "x1_yanghe_continuation.json"

COMPANY, COMPANY_ID, CODE = "洋河股份", "002304.SZ", "002304"
EARLIER = {"periods": (2022, 2023), "as_of": "2024-04-27", "disclosed_at": "2024-04-27",
           "label": "洋河股份 2022–2023（2023 年年度报告）"}
LATER = {"periods": (2023, 2024), "as_of": "2025-04-30", "disclosed_at": "2025-04-28",
         "label": "洋河股份 2023–2024（2024 年年度报告）"}


def main() -> int:
    from financial_analysis import continuation as cont

    if not EARLIER_DOC.is_file():
        print(f"缺较早材料：{EARLIER_DOC}（先跑 scripts/x1_import_yanghe_2023.py）")
        return 1
    earlier_doc = json.loads(EARLIER_DOC.read_text(encoding="utf-8"))
    later_doc = json.loads(LATER_DOC.read_text(encoding="utf-8"))

    e_snap = cont.snapshot(earlier_doc, company=COMPANY, company_id=COMPANY_ID, code=CODE,
                           periods=EARLIER["periods"], as_of=EARLIER["as_of"],
                           disclosed_at=EARLIER["disclosed_at"], label=EARLIER["label"],
                           limit=3)
    l_snap = cont.snapshot(later_doc, company=COMPANY, company_id=COMPANY_ID, code=CODE,
                           periods=LATER["periods"], as_of=LATER["as_of"],
                           disclosed_at=LATER["disclosed_at"], label=LATER["label"],
                           limit=3)
    page = cont.compare(e_snap, l_snap, limit=3)
    # 假设影响：同一目标下 fixed／detail 两种规则（用**较新材料**的数据集）
    page["assumption_impact"] = cont.assumption_impact(l_snap["_ds"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "continuation.json").write_text(
        json.dumps(page, ensure_ascii=False, indent=1), encoding="utf-8")
    md = cont.render_page(page)
    (OUT_DIR / "continuation.md").write_text(md, encoding="utf-8")

    hyp = page.get("hypotheses") or []
    decisive = [h for h in hyp if h.get("status") in (cont.STATUS_STRONGER, cont.STATUS_WEAKER)]
    checks = {
        "two_materials_loaded": bool(e_snap.get("dataset", {}).get("hash")
                                     and l_snap.get("dataset", {}).get("hash")),
        "periods_from_original_material": ([int(p) for p in e_snap["periods"]] == [2022, 2023]
                                           and [int(p) for p in l_snap["periods"]] == [2023, 2024]),
        "hypotheses_frozen_with_conditions": bool(hyp) and all(
            h.get("observation_condition") and h.get("basis_source") for h in hyp),
        "at_least_one_decisive": bool(decisive),
        "same_caliber_overlap_checked": bool((page.get("overlap_check") or {}).get("items")),
        "assumption_impact_explained": bool(
            (page.get("assumption_impact") or {}).get("modes")),
        "historical_replay_not_forecast": any(
            "历史材料回放" in t for t in (page.get("limits") or ())),
        "locators_present": any(h.get("basis_source", {}).get("url") for h in hyp),
    }
    evidence = {
        "case": "X1 洋河 2022–2024 研究续页（历史材料回放）",
        "earlier": {"label": e_snap["label"], "source": e_snap["source"],
                    "dataset": e_snap["dataset"],
                    "judgment_ids": [j["judgment_id"] for j in e_snap["judgments"]]},
        "later": {"label": l_snap["label"], "source": l_snap["source"],
                  "dataset": l_snap["dataset"],
                  "judgment_ids": [j["judgment_id"] for j in l_snap["judgments"]]},
        "hypotheses": [{"judgment_id": h["judgment_id"], "hypothesis": h["hypothesis"],
                        "status": h["status"], "reason": h["status_reason"],
                        "new_readings": h["new_readings"]} for h in hyp],
        "overlap_check": page.get("overlap_check"),
        "assumption_impact": page.get("assumption_impact"),
        "checks": checks,
        "passed": sum(1 for v in checks.values() if v), "total": len(checks),
        "artifacts": {"continuation_json": str((OUT_DIR / "continuation.json").relative_to(ROOT)),
                      "continuation_md": str((OUT_DIR / "continuation.md").relative_to(ROOT))},
        "boundary": ("较早材料先写假说、再拿较新材料读数比较——这是**回放**，"
                     "不构成事前盲测或投资预测；将来真实时间前向更新才谈研究效用。"),
    }
    OUT_JSON.write_text(json.dumps(evidence, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"较早 {e_snap['label']}：判断 {len(e_snap['judgments'])} 条；"
          f"较新 {l_snap['label']}：判断 {len(l_snap['judgments'])} 条")
    for h in hyp:
        print(f"  [{h['status']}] {h['hypothesis']} —— {h['status_reason']}")
    print(f"假设影响：{json.dumps(page['assumption_impact'].get('modes'), ensure_ascii=False)}")
    print(f"核对：{sum(1 for v in checks.values() if v)}/{len(checks)}；证据 "
          f"{OUT_JSON.relative_to(ROOT)}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
