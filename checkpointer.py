"""checkpointer.py -- V1.2 断点续跑（对标 LangGraph checkpointer）。

进程崩溃后，orchestrator 可从"最后一个完成步骤"继续执行，避免整轮多 agent
成本浪费。存储双通道：

1. Redis  ``checkpoint:{task_id}``  JSON，TTL 24h（主通道，跨进程共享）；
2. SQLite ``checkpoints`` 表（兜底通道，Redis 不可用时仍可读）。

所有 I/O 均静默降级：Redis/SQLite 不可用时行为与无 checkpointer 完全一致。
"""

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CHECKPOINT_TTL = 86400  # 24h
CHECKPOINT_PREFIX = "checkpoint:"
TERMINAL_STATUSES = {"SUCCESS", "FAILED", "SUCCESS_WITH_ISSUES"}

_redis_client = None
_db_lock = threading.Lock()


def goal_hash(goal: str) -> str:
    """目标哈希（16 hex），用于校验 checkpoint 是否属于当前 goal。"""
    return hashlib.sha256(str(goal or "").encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path() -> str:
    return os.environ.get("REGISTRY_DB", "agents.db")


def _get_redis():
    """惰性创建同步 Redis 客户端（decode_responses=True，短超时）。"""
    global _redis_client
    if _redis_client is None:
        import redis
        _redis_client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
    return _redis_client


def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def _init_sqlite() -> None:
    with _db_lock:
        conn = _db_conn()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS checkpoints ("
                " task_id TEXT PRIMARY KEY,"
                " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                " data TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()


def save_checkpoint(task_id: str, checkpoint: dict) -> None:
    """写 checkpoint：Redis（TTL 24h）+ SQLite 兜底；任何失败静默降级。"""
    if not task_id:
        return
    payload = dict(checkpoint or {})
    payload["task_id"] = str(task_id)
    payload.setdefault("saved_at", _now_iso())
    try:
        raw = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as exc:
        logger.debug("checkpoint serialize failed for %s: %s", task_id, str(exc)[:100])
        return
    try:
        _get_redis().set(f"{CHECKPOINT_PREFIX}{task_id}", raw, ex=CHECKPOINT_TTL)
    except Exception:
        logger.debug("checkpoint redis write failed for %s", task_id)
    try:
        _init_sqlite()
        now_sql = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cutoff_sql = (
            datetime.fromtimestamp(time.time() - CHECKPOINT_TTL, timezone.utc)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
        with _db_lock:
            conn = _db_conn()
            try:
                conn.execute(
                    "INSERT INTO checkpoints(task_id, created_at, data)"
                    " VALUES(?,?,?)"
                    " ON CONFLICT(task_id) DO UPDATE SET"
                    " created_at=excluded.created_at, data=excluded.data",
                    (str(task_id), now_sql, raw),
                )
                conn.execute(
                    "DELETE FROM checkpoints WHERE created_at < ?", (cutoff_sql,)
                )
                conn.commit()
            finally:
                conn.close()
    except Exception:
        logger.debug("checkpoint sqlite write failed for %s", task_id)


def load_checkpoint(task_id: str) -> dict | None:
    """优先 Redis，兜底 SQLite；均失败返回 None。"""
    if not task_id:
        return None
    try:
        raw = _get_redis().get(f"{CHECKPOINT_PREFIX}{task_id}")
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    try:
        with _db_lock:
            conn = _db_conn()
            try:
                row = conn.execute(
                    "SELECT data FROM checkpoints WHERE task_id=?"
                    " ORDER BY created_at DESC LIMIT 1",
                    (str(task_id),),
                ).fetchone()
            finally:
                conn.close()
        if row:
            data = json.loads(row["data"])
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


def clear_checkpoint(task_id: str) -> None:
    """任务完成后清理 checkpoint（Redis + SQLite）。"""
    if not task_id:
        return
    try:
        _get_redis().delete(f"{CHECKPOINT_PREFIX}{task_id}")
    except Exception:
        pass
    try:
        with _db_lock:
            conn = _db_conn()
            try:
                conn.execute("DELETE FROM checkpoints WHERE task_id=?", (str(task_id),))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def is_task_completed(task_id: str, checkpoint: dict | None = None) -> bool:
    """checkpoint 自身 status 或 task_history.status 已是终态 → True。"""
    cp = checkpoint or {}
    if str(cp.get("status") or "") in TERMINAL_STATUSES:
        return True
    try:
        with _db_lock:
            conn = _db_conn()
            try:
                row = conn.execute(
                    "SELECT status FROM task_history WHERE task_id=?", (str(task_id),)
                ).fetchone()
            finally:
                conn.close()
        if row and str(row["status"] or "") in TERMINAL_STATUSES:
            return True
    except Exception:
        pass
    return False


def _parse_time(value) -> float | None:
    """解析 ISO 时间戳（含毫秒/时区）→ epoch 秒；失败返回 None。"""
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def list_pending_checkpoints(age_sec: int = 3600) -> list[dict]:
    """列出有 checkpoint 且未完成的任务（供启动恢复扫描）。

    age_sec>0 时只返回 saved_at 至少早于 age_sec 秒的 checkpoint，避免把
    仍在运行的任务当作崩溃恢复；Redis 与 SQLite 双通道去重合并。
    """
    now = time.time()
    merged: dict[str, dict] = {}

    def _collect(cp: dict) -> None:
        if not isinstance(cp, dict) or not cp.get("task_id"):
            return
        if str(cp.get("status") or "") in TERMINAL_STATUSES:
            return
        old = merged.get(str(cp["task_id"]))
        if old is None or (cp.get("saved_at") or "") >= (old.get("saved_at") or ""):
            merged[str(cp["task_id"])] = cp

    try:
        r = _get_redis()
        for key in r.scan_iter(f"{CHECKPOINT_PREFIX}*", count=200):
            raw = r.get(key)
            if not raw:
                continue
            try:
                _collect(json.loads(raw))
            except Exception:
                continue
    except Exception:
        pass
    try:
        with _db_lock:
            conn = _db_conn()
            try:
                rows = conn.execute(
                    "SELECT task_id, data FROM checkpoints"
                ).fetchall()
            finally:
                conn.close()
        for row in rows:
            try:
                cp = json.loads(row["data"])
                if isinstance(cp, dict) and not cp.get("task_id"):
                    cp = dict(cp)
                    cp["task_id"] = row["task_id"]
                _collect(cp)
            except Exception:
                continue
    except Exception:
        pass

    out: list[dict] = []
    for task_id, cp in merged.items():
        if is_task_completed(task_id, cp):
            continue
        ts = _parse_time(cp.get("saved_at"))
        if ts is None:
            if age_sec > 0:
                continue
        elif age_sec > 0 and (now - ts) < age_sec:
            continue
        out.append(cp)
    out.sort(key=lambda c: str(c.get("saved_at") or ""))
    return out
