# -*- coding: utf-8 -*-
"""实机取消验收驱动器（M0-f ③）：提交任务 → 指定延迟后请求停止 → 计时到终态。

与前端"停止"完全一致：`setex task_cancel:{tid} 3600 1`。
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
CANCEL_AFTER = float(sys.argv[2]) if len(sys.argv) > 2 else 25.0
MAX_WAIT = float(sys.argv[3]) if len(sys.argv) > 3 else 300.0


def _db_path() -> str:
    # 脚本位于 docs/tools/，必须显式把仓库根加入 sys.path 才能解析到同一个任务库
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
    from common import _NO_REDIS_RETRY
    r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True,
                    socket_connect_timeout=5, socket_timeout=5,
                    retry=_NO_REDIS_RETRY)
    tid = "ui-" + uuid.uuid4().hex[:10]
    r.delete(f"task_ack:{tid}")
    r.publish("orchestrator:main", json.dumps({
        "task_id": tid, "goal": GOAL, "project": "default", "context": "",
        "auto_run": True, "template_steps": None, "user_id": "",
        "report_confirm": False, "conversation_id": "", "parent_task_id": "",
    }, ensure_ascii=False))
    print("TASK_ID", tid, flush=True)

    deadline = time.time() + 30
    ack = None
    while time.time() < deadline:
        ack = r.get(f"task_ack:{tid}")
        if ack:
            break
        time.sleep(0.3)
    print("ACK", ack, flush=True)

    db = _db_path()
    time.sleep(max(1.0, CANCEL_AFTER))
    before = _status(db, tid)
    t_cancel = time.time()
    r.setex(f"task_cancel:{tid}", 3600, "1")
    print(f"CANCEL_REQUESTED at {time.strftime('%H:%M:%S')} (status before={before})",
          flush=True)

    while time.time() - t_cancel < MAX_WAIT:
        time.sleep(1.0)
        row = _status(db, tid)
        if row.split("/")[0] in ("SUCCESS", "SUCCESS_WITH_ISSUES", "FAILED", "CANCELLED"):
            print("TERMINAL", row, f"stop_latency={time.time() - t_cancel:.1f}s", flush=True)
            return 0
    print("NOT_TERMINAL", _status(db, tid),
          f"after={MAX_WAIT:.0f}s", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
