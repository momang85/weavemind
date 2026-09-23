# -*- coding: utf-8 -*-
"""候选正文的**只读**验收（不改采纳版本、不写验收产物、不联网、不调用模型）。

用与生产同一条确定性验收（`acceptance_checker.run_acceptance`）对候选跑一遍，
打印结论、缺口与关键分项（溯源率、来源标注、比率算术、必答问题支持），供采纳前判断。

用法：python scripts/verify_candidate_20260923.py [--md 路径] [task_id]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REDIS_PORT", "6399")
DEFAULT_MD = Path(r"C:\Users\ding0\AppData\Local\Temp\candidate_from_material_20260923.md")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task", nargs="?", default="ui-706c5ef4a5")
    ap.add_argument("--md", default=str(DEFAULT_MD))
    args = ap.parse_args()
    md_path = Path(args.md)
    if not md_path.is_file():
        print("候选文件不存在：", md_path, file=sys.stderr)
        return 2
    report = md_path.read_text(encoding="utf-8")

    import task_state as ts
    import workspace as ws_mod
    from acceptance_checker import run_acceptance

    ws = Path(ws_mod.task_workspace(args.task))
    goal = str((ts.read_task(args.task) or {}).get("goal") or "")
    res = run_acceptance(args.task, goal, report, ws)
    checks = res.get("checks") or {}
    print("overall:", res.get("overall"), "| sha:", str(res.get("report_sha256"))[:16],
          "| 字符:", len(report))
    print("gaps:", json.dumps(res.get("gaps") or [], ensure_ascii=False))
    for key in ("number_traceability", "source_labeling", "ratio_arithmetic",
                "attribution_support", "analysis_completeness", "freshness_block",
                "chart_references", "claim_atoms"):
        c = checks.get(key)
        if not c:
            continue
        print(f"[{key}] pass={c.get('pass')} :: {str(c.get('details'))[:220]}")
    nt = checks.get("number_traceability") or {}
    print("溯源：", nt.get("traceable_count"), "/", nt.get("total_count"),
          "=", nt.get("covered_ratio"), "| 域", nt.get("domain"))
    un = nt.get("untraceable") or []
    if un:
        print("不可溯源示例：", json.dumps([u.get("raw") for u in un[:12]], ensure_ascii=False))
    out = Path(r"C:\Users\ding0\AppData\Local\Temp\verify_candidate_20260923.json")
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
