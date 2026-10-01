# -*- coding: utf-8 -*-
"""U3 交付件证据：三一重工 2023/2024 的**正文 + 三图底稿**（与洋河同一条主线）。

复用 `scripts/case_deliverables.py`：同一套「事实→冻结→运行→出图→成篇」路径，只换材料与
输出目录。产出：

- `docs/evidence/u3_sany_charts/`：三张 PNG（+ 规格与图注清单）；
- `docs/evidence/u3_sany_note.md` / `u3_sany_note.json`：4–6 页正文与读数；
- `docs/evidence/u3_sany_deliverables.json`：两家对照（洋河与三一同一脚本、同一判据）。

脚本不写任何财务数字：全部来自运行输出。
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import case_deliverables as cd  # noqa: E402

EVIDENCE = os.path.join(ROOT, "docs", "evidence")
SANY = os.path.join(EVIDENCE, "u3_sany")
DOC = os.path.join(ROOT, "evals", "a2_official_chain_20260929", "600031",
                   "project", "materials", "0c3e82056e977adc", "doc.json")


def main() -> int:
    built = cd.build_case(root=ROOT, name="三一重工", entity_id="600031.SH",
                          doc_path=DOC, out_dir=SANY,
                          case_title="三一重工 600031 2023→2024 经营驱动正文与三图底稿")
    note = built["note"]
    with open(os.path.join(EVIDENCE, "u3_sany_note.md"), "w", encoding="utf-8") as f:
        f.write(note)
    stats = cd.note_stats(note)
    report = {
        "case": built["case"],
        "source_material": built["doc"],
        "dataset": built["dataset"],
        "runs": built["runs"],
        "note": {"path": "docs/evidence/u3_sany_note.md", **stats},
        "figures": built["figures"],
    }
    with open(os.path.join(EVIDENCE, "u3_sany_note.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    with open(os.path.join(EVIDENCE, "u3_sany_deliverables.json"), "w",
              encoding="utf-8") as f:
        json.dump({"batch": "U3", "case": built["case"],
                   "mainline": "facts → freeze → run → charts → note（与洋河同一脚本）",
                   "dataset": built["dataset"], "runs": built["runs"],
                   "note": stats,
                   "figures": [{k: v for k, v in fig.items() if k != "spec"}
                               for fig in built["figures"]],
                   "chart_conclusions": [fig["spec"].get("conclusion")
                                         for fig in built["figures"]]},
                  f, ensure_ascii=False, indent=1)
    print(f"正文 {stats['chars']} 字符 / {stats['lines']} 行 / {len(stats['sections'])} 小节 / "
          f"来源表行 {stats['table_rows']} / 引用运行 {stats['run_refs']}")
    manifest = json.load(open(os.path.join(SANY, "chart_manifest.json"), encoding="utf-8"))
    grades = {c.get("file"): c.get("grade") for c in (manifest.get("charts") or ())}
    for fig in built["figures"]:
        base = os.path.basename(fig["file"])
        print(f"  图 {fig['file']} {fig['bytes']} B grade={grades.get(base)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
