# -*- coding: utf-8 -*-
"""CI 确定性评测闸门（对标标准 3.6/3.8）：无 LLM 的核心质量不变量检查。"""

import sys


def main() -> int:
    failures: list[str] = []

    from evals.run import dry_run
    if dry_run() != 0:
        failures.append("评测集 schema 无效")

    from skill_registry import list_skills
    skills = list_skills()
    if len(skills) < 3:
        failures.append(f"skills 数量不足（{len(skills)} < 3）")
    if not all(s.get("description") and s.get("applies") for s in skills):
        failures.append("存在缺少 description/applies 的 skill")

    from validators.registry import list_validators
    if len(list_validators()) < 4:
        failures.append("validators 数量不足")

    from evals.judge import _loads_loose
    if _loads_loose('{"a": 1}') != {"a": 1}:
        failures.append("judge JSON 解析异常")

    if failures:
        print("!! CI 评测闸门未通过：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("OK: CI 评测闸门通过（评测集 / skills / validators / judge）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
