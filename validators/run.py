# -*- coding: utf-8 -*-
"""验证器 CLI：python -m validators.run --task <task_id>"""

import argparse
import sys

from validators.registry import run_for_task


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--goal", default="")
    ap.add_argument("--caps", default="")
    args = ap.parse_args()
    caps = [c.strip() for c in args.caps.split(",") if c.strip()]
    results = run_for_task(args.task, args.goal, caps)
    for r in results:
        print(f"[{'OK' if r['ok'] else 'FAIL'}] {r['name']}: {r['detail']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
