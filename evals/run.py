# -*- coding: utf-8 -*-
"""评测集运行器（对标标准 C3-3.6 评测驱动开发）。

用法：
  python -m evals.run --dry-run            # 校验评测集 schema（CI 用，不发 LLM）
  python -m evals.run --judge <jsonl>      # 用 evals/judge.py 对记录打分
  python -m evals.run --live <case_id>     # 端到端跑一个评测案例（需服务在线）
"""

import argparse
import json
import sys
from pathlib import Path


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for p in sorted((Path(__file__).parent / "cases").glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"评测集文件损坏 {p.name}: {exc}")
        cases.extend(data.get("cases", []))
    return cases


def validate_case(c: dict) -> list[str]:
    issues = []
    if not c.get("id"):
        issues.append("缺少 id")
    if not c.get("goal"):
        issues.append("缺少 goal")
    if not c.get("expected_deliverable"):
        issues.append("缺少 expected_deliverable")
    if not c.get("ground_truth_points") or not isinstance(c.get("ground_truth_points"), list):
        issues.append("ground_truth_points 必须是非空列表")
    if not c.get("rubric") or not isinstance(c.get("rubric"), dict):
        issues.append("rubric 必须是对象")
    return issues


def dry_run() -> int:
    cases = load_cases()
    bad = 0
    for c in cases:
        issues = validate_case(c)
        if issues:
            bad += 1
            print(f"  !! {c.get('id', '?')}: {'; '.join(issues)}")
    print(f"评测集共 {len(cases)} 条，无效 {bad} 条")
    return 1 if bad else 0


def judge(jsonl: str) -> int:
    from evals.judge import score_record
    rows = [json.loads(line) for line in Path(jsonl).read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rows:
        score = score_record(r)
        print(json.dumps({"id": r.get("id"), **score}, ensure_ascii=False))
    return 0


def live(case_id: str) -> int:
    raise SystemExit("--live 需要服务在线，未实现自动执行；请用 --judge 对记录打分")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--judge", metavar="JSONL")
    ap.add_argument("--live", metavar="CASE_ID")
    args = ap.parse_args()
    if args.dry_run:
        return dry_run()
    if args.judge:
        return judge(args.judge)
    if args.live:
        return live(args.live)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
