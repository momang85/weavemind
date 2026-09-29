# -*- coding: utf-8 -*-
"""Q0 溯源回归回放：对**已冻结的真实任务**重跑机器验收，只读、不写任务库、不调模型。

为什么要有这个脚本：Q0 的两处修改（派生输出侧契约、正文指标/期间匹配）都会改变
"哪些数字算可溯源"。改判据必须能在**真实语料**上对照，而不是只跑构造用例；
上一轮把这段逻辑放在临时目录的一次性脚本里，事后无法复跑（架构师明确指出过）。
本脚本进库保留，任何人可对同一批冻结样本复算同一组读数。

用法：
    python scripts/q0_traceability_replay.py                # 默认三个冻结样本
    python scripts/q0_traceability_replay.py ui-xxx ui-yyy  # 指定任务 id

读数口径（与 `acceptance_checker` 一致，不另立）：
- `report_text` 取该任务**当前采纳版**正文（没有采纳版则取 `reports/report.md`）；
- 数据来自任务工作区（`project/working_paper.json` 等），**不连 Redis、不连 agents.db**。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_TASKS = ("ui-a06a005c9b", "ui-29e8ca73b5", "ui-c5loop-1")


def _workspace_root() -> Path:
    """任务工作区根（与 db_paths 的默认一致；可用环境变量覆盖）。"""
    env = os.environ.get("WEAVEMIND_WORKSPACE_ROOT") or os.environ.get("WEAVEMIND_TASKS_ROOT")
    if env:
        return Path(env)
    base = os.environ.get("WEAVEMIND_DATA_DIR") or str(Path(os.environ.get("TEMP", ".")) / "agent_workspace")
    return Path(base) / "tasks" / "projects" / "default"


def _db_path() -> Path:
    env = os.environ.get("WEAVEMIND_DB")
    if env:
        return Path(env)
    base = os.environ.get("WEAVEMIND_DATA_DIR") or str(ROOT)
    return Path(base) / "agents.db"


def _goal_of(task_id: str) -> str:
    """从任务库**只读**取目标（缺失/不可读就退回空串，不猜）。"""
    db = _db_path()
    if not db.exists():
        return ""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT goal FROM task_history WHERE task_id=?",
                              (task_id,)).fetchone()
        finally:
            con.close()
        return str((row or [""])[0] or "")
    except Exception:                                  # noqa: BLE001
        return ""


def _adopted_body(ws: Path) -> tuple[str, str]:
    """当前采纳版正文 → `(正文, 来源说明)`。没有版本库/没有采纳版就看 reports/report.md。"""
    try:
        from report_version import VersionStore
        store = VersionStore(ws)
        v = store.adopted()
        if v is not None and str(v.body or "").strip():
            return str(v.body), f"adopted:{v.version_id[:12]}"
    except Exception as exc:                           # noqa: BLE001
        print(f"  [warn] 读版本库失败：{str(exc)[:80]}")
    md = ws / "reports" / "report.md"
    if md.exists():
        return md.read_text(encoding="utf-8", errors="replace"), "reports/report.md"
    return "", ""


def _numbers_of(acc: dict) -> dict:
    """直接取数字溯源率明细（与验收同一实现，不自算第二套口径）。"""
    nt = acc.get("number_traceability") or {}
    if isinstance(nt, dict):
        return nt
    for check in (acc.get("checks") or []):
        if isinstance(check, dict) and "traceable" in check:
            return check
    return {}


def replay(task_id: str) -> dict:
    ws = _workspace_root() / task_id
    if not ws.exists():
        return {"task_id": task_id, "error": "workspace_missing", "workspace": str(ws)}
    goal = _goal_of(task_id)
    body, origin = _adopted_body(ws)
    if not body.strip():
        return {"task_id": task_id, "error": "no_report_text", "workspace": str(ws)}
    import acceptance_checker as ac
    # 验收的**详细**结果 = run_acceptance 的返回值 + 数字溯源明细（与页面同一实现）
    acc = ac.run_acceptance(task_id, goal, body, ws)
    detail = ac.check_number_traceability(body, ac._collect_sources(ws), goal=goal)
    out = {
        "task_id": task_id,
        "report_origin": origin,
        "report_chars": len(body),
        "overall": acc.get("overall"),
        "trace_ok": detail.get("pass"),
        "traceable": detail.get("traceable_count"),
        "total": detail.get("total_count"),
        "ratio": detail.get("covered_ratio"),
        "unverifiable": detail.get("unverifiable_count"),
        "untraceable_items": [str(i.get("raw") or i)[:40]
                              for i in (detail.get("untraceable") or [])],
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks", nargs="*", default=list(DEFAULT_TASKS))
    args = ap.parse_args()
    tasks = args.tasks or list(DEFAULT_TASKS)
    print(f"workspace root: {_workspace_root()}")
    print(f"db (只读取 goal): {_db_path()}")
    rows = []
    for tid in tasks:
        try:
            rows.append(replay(tid))
        except Exception as exc:                        # noqa: BLE001
            rows.append({"task_id": tid, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
