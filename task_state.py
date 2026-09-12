# -*- coding: utf-8 -*-
"""任务状态投影：`task_history` 的**唯一写入模块**与状态派生规则。

改造前的问题（结构审计已有记录）：
- `task_history` 由 webui 独家写；编排器只发 Redis 消息，终态消息里的 `acceptance`
  因表里没有该列被直接丢弃 → "任务状态"与"验收状态"长期可能不一致且无法对账；
- 运行期全程 `PENDING`（排队与运行不分），前端只能把 PENDING 当"进行中"；
- 状态有 5 份副本（内存 `_task_results`、DB、acceptance_report.json、
  metrics 汇总、Redis 两键），读者按"就近副本"取值；
- stale 清理只看 Redis 键是否存在、不校验 pid，编排器崩溃后键存活 24h，
  任务永远 PENDING。

本模块提供：
- `ensure_schema()`：加列（acceptance_json / rules_fingerprint / phase / updated_at），
  幂等；
- `derive_status()`：状态派生规则**唯一实现**（编排器 `_resolve_final_status` 委托此处）；
- `mark_queued/mark_running/record_completion()`：状态迁移的唯一入口；
- `read_task()`：含 acceptance 的统一读取；
- `is_running()`：供 stale 豁免使用（DB 状态，配合 pid 校验的 Redis 标记）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

DB_PATH = os.environ.get("AGENTS_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "agents.db")

# 状态词表（项目此前只有常用 4 值的 TaskStatus 枚举且未被编排链路使用；
# 这里显式区分"排队"与"运行"，终结状态沿用既有词表以免破坏前端映射）
QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
SUCCESS_WITH_ISSUES = "SUCCESS_WITH_ISSUES"
FAILED = "FAILED"
TERMINAL = (SUCCESS, SUCCESS_WITH_ISSUES, FAILED)

_NEW_COLUMNS = (
    ("acceptance_json", "TEXT DEFAULT ''"),
    ("rules_fingerprint", "TEXT DEFAULT ''"),
    ("phase", "TEXT DEFAULT ''"),
    ("updated_at", "TIMESTAMP"),
)


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(db_path or DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema(db_path: str | None = None) -> list[str]:
    """补齐列（幂等）。返回本次新增的列名，便于日志/测试观察。"""
    added: list[str] = []
    try:
        con = _connect(db_path)
        try:
            existing = {r[1] for r in con.execute("PRAGMA table_info(task_history)")}
            if not existing:
                return []          # 表还没建（webui 的 _init_db 负责）
            for name, decl in _NEW_COLUMNS:
                if name in existing:
                    continue
                con.execute(f"ALTER TABLE task_history ADD COLUMN {name} {decl}")
                added.append(name)
            if added:
                con.commit()
        finally:
            con.close()
    except Exception:
        return added
    return added


def derive_status(step_statuses=None, acceptance: dict | None = None,
                  llm_degraded: dict | None = None) -> str:
    """状态派生规则（唯一实现）。

    - 任一步骤非 SUCCESS（含 PARTIAL/FAILED）→ FAILED；
    - 验收报告存在且 overall != pass → SUCCESS_WITH_ISSUES；
    - 主备端点均失败（llm_degraded.both_failed）→ SUCCESS_WITH_ISSUES；
    - 其余 → SUCCESS。
    """
    statuses = [str(s or "").upper() for s in (step_statuses or [])]
    if any(s and s != "SUCCESS" for s in statuses):
        return FAILED
    accept_fail = bool(acceptance and acceptance.get("overall") != "pass")
    both_failed = bool((llm_degraded or {}).get("both_failed"))
    if accept_fail or both_failed:
        return SUCCESS_WITH_ISSUES
    return SUCCESS


def mark_queued(task_id: str, goal: str, project: str = "default",
                conversation_id: str = "", parent_task_id: str = "",
                context: str = "", user: str = "", db_path: str | None = None) -> None:
    """登记排队中的任务（提交时调用）。"""
    try:
        con = _connect(db_path)
        try:
            con.execute(
                "INSERT INTO task_history"
                "(task_id,goal,status,project,conversation_id,parent_task_id,context,user,phase)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (task_id, goal, QUEUED, project, conversation_id,
                 parent_task_id, context, user, "排队"),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def mark_running(task_id: str, phase: str = "执行", db_path: str | None = None) -> None:
    """标记进入执行（排队 → 运行），让历史/状态接口能区分两种阶段。"""
    try:
        con = _connect(db_path)
        try:
            con.execute(
                "UPDATE task_history SET status=?, phase=?, updated_at=CURRENT_TIMESTAMP"
                " WHERE task_id=? AND status IN (?,?)",
                (RUNNING, phase, task_id, QUEUED, "PENDING"),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def set_phase(task_id: str, phase: str, db_path: str | None = None) -> None:
    """更新阶段名（规划/执行/反思/交付），不改状态。"""
    try:
        con = _connect(db_path)
        try:
            con.execute(
                "UPDATE task_history SET phase=?, updated_at=CURRENT_TIMESTAMP"
                " WHERE task_id=?",
                (str(phase)[:40], task_id),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def record_completion(task_id: str, *, goal: str = "", status: str = "",
                      report: str = "", steps: list | None = None,
                      logs: list | None = None, acceptance: dict | None = None,
                      db_path: str | None = None) -> None:
    """写入终态：状态 + 报告 + 步骤/日志 + **验收摘要与规则指纹**（不再丢弃）。"""
    acceptance = acceptance or {}
    try:
        con = _connect(db_path)
        try:
            con.execute(
                "INSERT INTO task_history"
                "(task_id,goal,status,report,steps_json,logs_json,"
                " acceptance_json,rules_fingerprint,phase,completed_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                " ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,"
                " report=excluded.report,steps_json=excluded.steps_json,"
                " logs_json=excluded.logs_json,acceptance_json=excluded.acceptance_json,"
                " rules_fingerprint=excluded.rules_fingerprint,phase=excluded.phase,"
                " completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP",
                (task_id, goal, str(status or SUCCESS), report,
                 json.dumps(steps or [], ensure_ascii=False),
                 json.dumps((logs or [])[-200:], ensure_ascii=False),
                 json.dumps(acceptance, ensure_ascii=False) if acceptance else "",
                 str(acceptance.get("rules_fingerprint") or ""), "完成"),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def read_task(task_id: str, db_path: str | None = None) -> dict:
    """统一读取（含解析后的 acceptance 与 phase）。"""
    try:
        con = _connect(db_path)
        try:
            row = con.execute(
                "SELECT * FROM task_history WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return {}
    if not row:
        return {}
    out = dict(row)
    raw = out.get("acceptance_json") or ""
    try:
        out["acceptance"] = json.loads(raw) if raw else {}
    except Exception:
        out["acceptance"] = {}
    return out


def is_running(task_id: str, db_path: str | None = None) -> bool:
    """DB 口径的"运行中"（供 stale 豁免；配合 Redis 标记的 pid 校验）。"""
    return str(read_task(task_id, db_path).get("status") or "").upper() == RUNNING


def mark_stale_failed(task_id: str, db_path: str | None = None) -> bool:
    """stale 清理：把长期无终态的排队任务翻成 FAILED。"""
    try:
        con = _connect(db_path)
        try:
            cur = con.execute(
                "UPDATE task_history SET status=?, phase='过期',"
                " updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND status IN (?,?)",
                (FAILED, task_id, QUEUED, "PENDING"),
            )
            con.commit()
            return bool(cur.rowcount)
        finally:
            con.close()
    except Exception:
        return False
