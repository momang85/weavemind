# -*- coding: utf-8 -*-
"""实机任务驱动器（M0-f ③）：走 webui 同一条通道提交任务并跟踪到终态。

与前端"提交任务"完全一致：向 `orchestrator:main` 发布载荷、等 `task_ack:{tid}` 收执；
进度从 `task_snapshot:{tid}` 与任务库投影读取。只读+提交，不改任何配置。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import uuid

import redis

GOAL = sys.argv[1]
PREFIX = sys.argv[2] if len(sys.argv) > 2 else "ui"
MAX_WAIT = float(sys.argv[3]) if len(sys.argv) > 3 else 950.0


def _db_path() -> str:
    # 脚本位于 docs/tools/：仓库根必须进 sys.path，否则解析不到同一个任务库
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import db_paths
        return db_paths.resolve_db_path()
    except Exception as exc:
        print("DB_PATH_ERROR", exc, flush=True)
        return ""


def _status(db: str, tid: str) -> str:
    if not db or not os.path.exists(db):
        return ""
    try:
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT status, phase FROM task_history WHERE task_id=?", (tid,)
            ).fetchone()
        finally:
            con.close()
        return f"{row[0]}/{row[1]}" if row else ""
    except Exception as exc:
        return f"<db error: {exc}>"


def main() -> int:
    r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
    tid = f"{PREFIX}-" + uuid.uuid4().hex[:10]
    r.delete(f"task_ack:{tid}")
    payload = {
        "task_id": tid, "goal": GOAL, "project": "default", "context": "",
        "auto_run": True, "template_steps": None, "user_id": "",
        "report_confirm": False, "conversation_id": "", "parent_task_id": "",
    }
    r.publish("orchestrator:main", json.dumps(payload, ensure_ascii=False))
    print("TASK_ID", tid, flush=True)

    ack_deadline = time.time() + 30
    ack = None
    while time.time() < ack_deadline:
        ack = r.get(f"task_ack:{tid}")
        if ack:
            break
        time.sleep(0.3)
    print("ACK", ack, flush=True)

    db = _db_path()
    started = time.time()
    last = ""
    while time.time() - started < MAX_WAIT:
        time.sleep(3)
        row = _status(db, tid)
        line = f"{int(time.time() - started):4d}s db={row}"
        snap = r.get(f"task_snapshot:{tid}")
        if snap:
            try:
                data = json.loads(snap)
                line += f" phase={data.get('phase', '')}"
                line += f" steps={len(data.get('steps') or [])}"
                budget = data.get("budget") or {}
                if budget:
                    line += f" remaining={budget.get('remaining')}"
            except Exception:
                pass
        if line != last:
            print(line, flush=True)
            last = line
        if row.split("/")[0] in ("SUCCESS", "SUCCESS_WITH_ISSUES",
                                 "FAILED", "CANCELLED"):
            print("TERMINAL", row, f"elapsed={time.time() - started:.0f}s", flush=True)
            return 0
    print("NOT_TERMINAL_AFTER", int(MAX_WAIT), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
