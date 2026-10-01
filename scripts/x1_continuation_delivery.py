# -*- coding: utf-8 -*-
"""X1 交付核对：**研究续页真的从正常报告/版本出口交出来了**（阶段X §4 验收）。

前置：`python scripts/x1_yanghe_continuation.py`（算续页）
    → `python scripts/v0_yanghe_normal_task.py --case yanghe-cont`（正常装配出报告/PDF/包）

本脚本只读产物并逐条判定，写到 `docs/evidence/x1_continuation_delivery.json`：
1. 续页在正文里出现**一次**，且排在『关键发现』之前（前两页）；
2. 四列样式齐全（上次假说/本期新读数/判断怎样改变/下一观察）；
3. 至少一条假说被明确**加强或削弱**，且写出理由；
4. 有**旧材料引用**（较早材料的标题/日期/期间）与**原件定位**（页/字符区间）；
5. 假设影响（fixed/detail 同一目标）写进续页；历史回放边界写进续页；
6. PDF 里续页在第 1–2 页；交付包含 `analysis/continuation.md`（续页随包提供）。
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

TASK_DIR = ROOT / "docs" / "evidence" / "x1_yanghe_continuation_task"
TASK_JSON = ROOT / "docs" / "evidence" / "x1_yanghe_continuation_task.json"
PAGE_JSON = ROOT / "docs" / "evidence" / "x1_yanghe_continuation.json"
OUT = ROOT / "docs" / "evidence" / "x1_continuation_delivery.json"


def main() -> int:
    md = (TASK_DIR / "report.md").read_text(encoding="utf-8")
    ev = json.loads(TASK_JSON.read_text(encoding="utf-8"))
    page = json.loads(PAGE_JSON.read_text(encoding="utf-8"))
    layout = ((ev.get("pdf") or {}).get("layout") or {})
    pkg = ((ev.get("package") or {}).get("has_analysis") or [])
    statuses = [h.get("status") for h in (page.get("hypotheses") or [])]
    decisive = [s for s in statuses if s in ("加强", "削弱")]
    cont_idx, find_idx = md.find("## 研究续页（可检验）"), md.find("## 关键发现")
    checks = {
        "page_rendered_once_before_findings": (
            md.count("## 研究续页（可检验）") == 1 and 0 <= cont_idx < find_idx),
        "four_column_style": all(k in md for k in
                                 ("上次持续性假说", "本期关键新读数",
                                  "判断怎样改变", "下一观察")),
        "at_least_one_decisive": bool(decisive),
        "old_material_cited": ("2023年年度报告" in md and "比较期 2022/2023" in md),
        "locators_present": ("字符 " in md and "第 " in md),
        "assumption_impact_written": ("假设为什么重要" in md
                                      and "fixed 模式" in md and "detail 模式" in md),
        "historical_replay_boundary": "历史材料回放" in md and "事前预测" in md,
        "page_on_pdf_page_1_or_2": (layout.get("first_page_of") or {})
        .get("研究续页") in (1, 2),
        "continuation_md_packed": "analysis/continuation.md" in pkg,
    }
    out = {
        "case": "X1 洋河研究续页：正常报告/版本出口",
        "statuses": statuses,
        "decisive_count": len(decisive),
        "pdf_layout": layout,
        "package_analysis_files": pkg,
        "checks": {k: bool(v) for k, v in checks.items()},
        "passed": sum(1 for v in checks.values() if v), "total": len(checks),
        "boundary": ("续页由 case 脚本算好后交给正常装配；本核对只读产物，"
                     "不重算、不写交付"),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in out["checks"].items():
        print(f"  {k}: {'OK' if v else 'FAIL'}")
    print(f"X1 交付核对 {out['passed']}/{out['total']}；判断状态 {statuses}；"
          f"PDF {layout.get('pages')} 页（续页第 "
          f"{(layout.get('first_page_of') or {}).get('研究续页')} 页）")
    print(f"证据：{OUT.relative_to(ROOT)}")
    return 0 if out["passed"] == out["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
