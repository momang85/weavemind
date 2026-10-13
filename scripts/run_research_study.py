"""Run a local research spec without launching services or contacting providers."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quant_research.study import run_study, write_study, verify_study, replay_study


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="织光离线研究：Wind文件→事件/数学/风险准备")
    parser.add_argument("spec", type=Path, nargs="?", help="study.json 文件")
    parser.add_argument("--input-dir", type=Path, help="输入文件根目录，默认 spec 所在目录")
    parser.add_argument("--output-dir", type=Path, default=Path("tmp/research-studies"))
    parser.add_argument("--preview", action="store_true", help="仅显示结果摘要，不写产物")
    parser.add_argument("--verify", type=Path, help="核对已生成研究目录的冻结文件hash")
    parser.add_argument("--replay", type=Path, help="仅用冻结输入重算研究，不要求原始Wind文件仍在")
    args = parser.parse_args(argv)
    try:
        if args.verify or args.replay:
            check = replay_study(args.replay) if args.replay else verify_study(args.verify)
            print(json.dumps(check, ensure_ascii=False, indent=2))
            return 0 if check["ok"] else 1
        if not args.spec:
            parser.error("需要 spec 文件，或 --verify 研究目录")
        spec = json.loads(args.spec.read_text(encoding="utf-8-sig"))
        result = run_study(spec, input_dir=args.input_dir or args.spec.parent)
        print(result["summary_markdown"])
        if not args.preview:
            folder = write_study(result, output_dir=args.output_dir)
            print(f"内部研究产物：{folder}")
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(f"无法运行研究：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
