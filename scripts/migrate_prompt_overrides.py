# -*- coding: utf-8 -*-
"""迁移 prompts/overrides.json：把"单任务教训写成的全局能力覆盖"转成带作用域条目。

背景：自进化此前把每次任务学到的具体要求写成 `step:<capability>` / `<capability>`
的裸全局键（只带 trigger_task），读取侧又不做目标匹配，于是某个财务任务的要求
（"用 matplotlib 画 2014-2025 营收/净利润双 y 轴图"）被追加到所有 code_execution
步骤。本脚本按触发任务的目标派生主题词（match_goal），把旧格式升级为
"键 → 条目列表"，并保持读取向后兼容。

用法：
    python scripts/migrate_prompt_overrides.py           # 演练（只报告）
    python scripts/migrate_prompt_overrides.py --apply   # 备份后写入

幂等：已是新格式且带 match_goal 的条目原样保留。
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prompt_registry as pr  # noqa: E402


def migrate(apply: bool) -> dict:
    path = pr._overrides_path()
    if not path.exists():
        return {"path": str(path), "entries": 0, "migrated": 0, "archived": 0, "applied": False}
    data = pr.load_overrides()
    migrated: list[dict] = []
    archived: list[dict] = []
    out: dict = {}
    for key, raw in data.items():
        new_entries: list[dict] = []
        for entry in pr._normalize_entries(raw):
            match_goal = entry.get("match_goal")
            if isinstance(match_goal, list) and match_goal:
                new_entries.append(entry)          # 已有作用域
                continue
            trigger = str(entry.get("trigger_task") or "").strip()
            trigger_goal = pr._trigger_goal(trigger) if trigger else ""
            topics = pr.topic_keywords(trigger_goal) if trigger_goal else []
            low = str(entry.get("prompt") or "").lower()
            task_specific = any(h.lower() in low for h in pr._TASK_SPECIFIC_HINTS)
            if topics:
                entry = dict(entry, match_goal=topics, scope="scoped")
                new_entries.append(entry)
                migrated.append({"key": key, "trigger_task": trigger, "match_goal": topics})
            elif task_specific or trigger:
                # 派不出主题词又带任务特定内容 → 归档，避免继续全局生效
                archived.append({"key": key, "trigger_task": trigger,
                                 "reason": "无法派生作用域" if not trigger_goal else "无主题词"})
            else:
                entry = dict(entry, scope="global")
                new_entries.append(entry)          # 手写维护的覆盖：保持全局
        if new_entries:
            new_entries.sort(key=lambda e: int(e.get("version") or 0), reverse=True)
            out[key] = new_entries[:pr.MAX_ENTRIES_PER_KEY]

    if apply:
        backup = path.with_suffix(".json.bak")
        shutil.copy2(path, backup)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        if archived:
            arch_path = path.with_name("overrides.archived.json")
            existing = []
            if arch_path.exists():
                try:
                    existing = json.loads(arch_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = []
            existing.append({
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "entries": archived,
            })
            arch_path.write_text(json.dumps(existing, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    return {
        "path": str(path),
        "entries": sum(len(pr._normalize_entries(v)) for v in data.values()),
        "migrated": len(migrated),
        "archived": len(archived),
        "applied": bool(apply),
        "details": migrated,
        "archived_details": archived,
    }


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    result = migrate(apply)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if not apply:
        print("\n演练模式：未写入。确认无误后加 --apply（会先备份为 overrides.json.bak）。")


if __name__ == "__main__":
    main()
