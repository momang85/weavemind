# -*- coding: utf-8 -*-
"""S3：试用记录采集 CLI（非开发者可用；未知字段留空，不当 0）。

用法（Windows cmd / PowerShell 均可，把 python 换成你的解释器即可）：

    # 1) 从任务里自动采集能取到的部分（只关联任务 id，不采集身份）
    python scripts/trial_record.py add --task-id ui-xxxxxxxx --category trial

    # 2) 人工补记（只有人知道的项目；不填就保持 unknown，不填 0）
    python scripts/trial_record.py add --task-id ui-xxxxxxxx --category trial \\
        --edited-fields 2 --review-seconds 300 --cost-usd 0.35 --cost-kind known \\
        --would-again yes --notes "首次试用，按文档走完"

    # 3) 看汇总（已知/未知分别列出，未知不进中位数、不进总额）
    python scripts/trial_record.py summary
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import trial_metrics as T  # noqa: E402


def _num(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cmd_add(args) -> int:
    rec = T.record_from_task(args.task_id, category=args.category)
    if args.edited_fields is not None:
        v = _num(args.edited_fields)
        rec.human_edited_fields = T.Measurement(v, "" if v is not None else "取值非法")
    if args.review_seconds is not None:
        v = _num(args.review_seconds)
        rec.review_seconds = T.Measurement(v, "" if v is not None else "取值非法")
    if args.wrong_certifications is not None:
        v = _num(args.wrong_certifications)
        rec.wrong_certifications = T.Measurement(v, "" if v is not None else "取值非法")
    if args.cost_kind:
        rec.cost_kind = args.cost_kind
    if args.cost_usd is not None:
        v = _num(args.cost_usd)
        rec.cost_usd = T.Measurement(v, "" if v is not None else "取值非法")
        if v is not None and rec.cost_kind == T.COST_UNKNOWN:
            rec.cost_kind = T.COST_KNOWN
    if args.would_again:
        rec.would_do_second_task = args.would_again
    if args.notes:
        rec.notes = args.notes[:400]
    path = T.append_record(rec)
    print(f"已追加试用记录 {rec.trial_id} → {path}")
    payload = rec.as_dict()
    unknown = [k for k, v in payload.items()
               if isinstance(v, dict) and v.get("known") is False]
    if unknown:
        print("仍为未知（需人工补记）：" + "、".join(unknown))
    return 0


def cmd_summary(args) -> int:
    records = T.load_records()
    s = T.summarize(records)
    print(json.dumps(s, ensure_ascii=False, indent=1))
    if not records:
        print("（还没有试用记录：先跑 add）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="试用记录采集（S3）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="追加一条试用记录")
    add.add_argument("--task-id", required=True)
    add.add_argument("--category", default=T.CATEGORY_UNKNOWN,
                     choices=list(T.CATEGORIES))
    add.add_argument("--edited-fields", default=None, help="人工修改的关键字段数")
    add.add_argument("--review-seconds", default=None, help="人工复核耗时（秒）")
    add.add_argument("--wrong-certifications", default=None, help="错误认证数")
    add.add_argument("--cost-kind", default=None,
                     choices=[T.COST_KNOWN, T.COST_ESTIMATED, T.COST_UNKNOWN])
    add.add_argument("--cost-usd", default=None, help="费用（美元）")
    add.add_argument("--would-again", default=None, choices=["yes", "no"],
                     help="是否愿再做第二个任务；不确定就别传（保持 unknown）")
    add.add_argument("--notes", default="", help="备注（不要写姓名/邮箱等身份信息）")

    sub.add_parser("summary", help="打印汇总（已知/未知分别列出）")
    args = ap.parse_args()
    return cmd_add(args) if args.cmd == "add" else cmd_summary(args)


if __name__ == "__main__":
    raise SystemExit(main())
