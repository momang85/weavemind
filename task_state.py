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
- `is_running()`：供 stale 豁免使用（DB 状态，配合 pid 校验的 Redis 标记）；
- `pid_same_process()`：运行标记持有者探活的**唯一实现**（webui 看护线程与
  编排器检查点恢复共用同一条判据）；
- `mark_persist_failed()`：终态落库失败的可查标记（失败不能只留一行日志）。

两条不变量：
- 任务库路径由 `db_paths.resolve_db_path()` 统一解析，各模块不再自读
  `REGISTRY_DB`/`AGENTS_DB`（Docker 下曾因此把状态写进另一个文件）；
- `mark_queued`/`record_completion` 返回**是否真的写入成功**——提交收执据此判定，
  不允许"落库失败仍回 accepted"。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

# Redis 客户端统一关掉 redis-py 的内建重试：默认重试会把 socket_connect_timeout
# 叠成 26~48 秒才失败（实测 127.0.0.1 26s / localhost 48s），Redis 不在时
# 表现为"服务没崩但处处卡"。NoBackoff + 0 次重试 → 稳定 2 秒内失败并可被上层降级。
from redis.backoff import NoBackoff as _NoBackoff  # noqa: E402
from redis.retry import Retry as _Retry  # noqa: E402

import db_paths as _db_paths  # noqa: E402

_NO_REDIS_RETRY = _Retry(_NoBackoff(), 0)

logger = logging.getLogger(__name__)

# 任务库路径：唯一解析入口（见 db_paths）。此前本模块读 AGENTS_DB、web_ui 读
# REGISTRY_DB，Dockerfile 只设了后者——状态被写进既没建表、也不在挂载卷里的
# 另一个文件，表现为"提交 accepted、历史查不到、状态永远缺失"。
DB_PATH = _db_paths.resolve_db_path()

# 状态词表（项目此前只有常用 4 值的 TaskStatus 枚举且未被编排链路使用；
# 这里显式区分"排队"与"运行"，终结状态沿用既有词表以免破坏前端映射）
QUEUED = "QUEUED"
RUNNING = "RUNNING"
SUCCESS = "SUCCESS"
SUCCESS_WITH_ISSUES = "SUCCESS_WITH_ISSUES"
FAILED = "FAILED"
# V2-1：用户主动停止是一个**独立终态**。此前取消被写成 FAILED，前端 statusMeta 的
# "已取消"映射永远收不到值，指标页统计的 CANCELLED 也恒为 0。
CANCELLED = "CANCELLED"
TERMINAL = (SUCCESS, SUCCESS_WITH_ISSUES, FAILED, CANCELLED)

# 列补丁的 DDL 直接写在 _add_missing_columns 里（字面量、不拼接），此处不再维护映射表。


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    con = sqlite3.connect(db_path or DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _ensure_task_table(con: sqlite3.Connection) -> None:
    """确保 `task_history` 存在（幂等）。**写入方自己保证表在**。

    实测（CI 部署烟测）：任务跑完了却写不进库——`no such table: task_history`，
    编排器连续 3 次落库失败后"不标记为已终结"，任务永远停在 RUNNING，门禁 900 秒超时。
    库文件是存在的（agents/sessions/checkpoints 由别的写入方建了），只缺这张表：
    建表此前只由 `web_ui._init_db()` 负责，任务提交走 Redis 而不经 HTTP，
    初始化没跑到的那些进程就只能干看着写失败。

    基础列与 `web_ui._init_db()` 的建表保持一致（两边都是 `IF NOT EXISTS`，谁先来都安全）；
    其余列由 `_add_missing_columns` 在此之后补。
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS task_history("
        "task_id TEXT PRIMARY KEY, goal TEXT, status TEXT,"
        " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        " completed_at TIMESTAMP, report TEXT,"
        " conversation_id TEXT DEFAULT '', parent_task_id TEXT DEFAULT '',"
        " context TEXT DEFAULT '', project TEXT DEFAULT 'default',"
        " user TEXT DEFAULT '', phase TEXT DEFAULT '',"
        " steps_json TEXT DEFAULT '', logs_json TEXT DEFAULT '',"
        " acceptance_json TEXT DEFAULT '',"
        " rules_fingerprint TEXT DEFAULT '', updated_at TIMESTAMP,"
        " research_request_json TEXT DEFAULT '')"
    )


def _add_missing_columns(con: sqlite3.Connection) -> list[str]:
    """先确保表在，再在既有连接上补列（都是幂等操作）。

    DDL 逐条写成字面量、直接执行：不做 SQL 文本拼接，也不把变量交给 execute，
    既避免注入面，也避免被安全扫描判为动态构造。
    """
    _ensure_task_table(con)
    existing = {r[1] for r in con.execute("PRAGMA table_info(task_history)")}
    if not existing:
        return []
    added: list[str] = []
    if "acceptance_json" not in existing:
        con.execute("ALTER TABLE task_history ADD COLUMN acceptance_json TEXT DEFAULT ''")
        added.append("acceptance_json")
    if "rules_fingerprint" not in existing:
        con.execute("ALTER TABLE task_history ADD COLUMN rules_fingerprint TEXT DEFAULT ''")
        added.append("rules_fingerprint")
    if "phase" not in existing:
        con.execute("ALTER TABLE task_history ADD COLUMN phase TEXT DEFAULT ''")
        added.append("phase")
    if "updated_at" not in existing:
        con.execute("ALTER TABLE task_history ADD COLUMN updated_at TIMESTAMP")
        added.append("updated_at")
    if "research_request_json" not in existing:
        # 研究契约在提交时先落库（A 批）：底稿只读它，抓取元数据不得反向决定研究对象
        con.execute(
            "ALTER TABLE task_history ADD COLUMN research_request_json TEXT DEFAULT ''")
        added.append("research_request_json")
    return added


def ensure_schema(db_path: str | None = None) -> list[str]:
    """补齐列（幂等）。返回本次新增的列名，便于日志/测试观察。"""
    added: list[str] = []
    try:
        con = _connect(db_path)
        try:
            added = _add_missing_columns(con)
            if added:
                con.commit()
        finally:
            con.close()
    except Exception:
        return added
    return added


def derive_status(step_statuses=None, acceptance: dict | None = None,
                  llm_degraded: dict | None = None,
                  draft_delivery: str = "") -> str:
    """状态派生规则（唯一实现）。

    - 任一步骤非 SUCCESS（含 PARTIAL/FAILED）→ FAILED；
    - 验收报告存在且 overall != pass → SUCCESS_WITH_ISSUES；
    - 主备端点均失败（llm_degraded.both_failed）→ SUCCESS_WITH_ISSUES；
    - **交付按未验收草稿处理**（draft_delivery 非空：正文没有对应自身的验收、
      或最终交付文档与选中版本不一致）→ SUCCESS_WITH_ISSUES —— 缺证据不得显示"通过"；
    - 其余 → SUCCESS。

    用户取消是**显式终态**（`CANCELLED`），不由本函数派生：取消时可能没有验收报告，
    也不应被记成"步骤失败"。
    """
    statuses = [str(s or "").upper() for s in (step_statuses or [])]
    if any(s and s != "SUCCESS" for s in statuses):
        return FAILED
    accept_fail = bool(acceptance and acceptance.get("overall") != "pass")
    both_failed = bool((llm_degraded or {}).get("both_failed"))
    # R0.2：验收没能证明属于**选中的那版正文**（短 hash / 身份不符 / 只读到文件）
    # → 证据不可信，按有缺口处理，不得显示"通过"
    unbound = bool(acceptance) and acceptance.get("version_bound") is False
    if accept_fail or both_failed or unbound or str(draft_delivery or "").strip():
        return SUCCESS_WITH_ISSUES
    return SUCCESS


def mark_queued(task_id: str, goal: str, project: str = "default",
                conversation_id: str = "", parent_task_id: str = "",
                context: str = "", user: str = "",
                research_request: dict | None = None,
                db_path: str | None = None) -> bool:
    """登记排队中的任务（提交时调用）。返回是否**真的写入成功**。

    返回值的意义：提交收执（`task_ack`）必须依据"是否真的登记成功"。此前本函数
    吞掉一切异常后正常返回 None，于是 `accept_task_request` 里"靠异常区分
    accepted / rejected"的分支永不触发——任务库不可写时提交方仍拿到 accepted。

    `research_request`：研究契约（公司/市场/两期/口径/截至日/来源要求/预算）。
    **在提交时先落库**，后续底稿只读它——抓取到的公司名/代码只能作为候选比对，
    不能反过来改写用户请求（否则请求会被数据源决定）。
    """
    request_json = ""
    if isinstance(research_request, dict) and research_request:
        try:
            request_json = json.dumps(research_request, ensure_ascii=False)
        except Exception as exc:
            logger.warning("任务 %s 研究契约无法序列化（按空处理）：%s", task_id, str(exc)[:120])
            request_json = ""
    try:
        con = _connect(db_path)
        try:
            # 编排器可能先于 web_ui 初始化库；缺列会让写入静默失败
            _add_missing_columns(con)
            con.execute(
                "INSERT INTO task_history"
                "(task_id,goal,status,project,conversation_id,parent_task_id,context,user,"
                " phase,research_request_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, goal, QUEUED, project, conversation_id,
                 parent_task_id, context, user, "排队", request_json),
            )
            con.commit()
            return True
        finally:
            con.close()
    except Exception as exc:
        logger.error("任务 %s 登记失败（任务库不可写）：%s", task_id, str(exc)[:200])
        return False


def mark_running(task_id: str, phase: str = "执行", db_path: str | None = None) -> bool:
    """标记进入执行（排队 → 运行），让历史/状态接口能区分两种阶段。

    返回是否写入成功；失败只记日志不回抛（阶段推进不该拖垮任务执行）。
    """
    try:
        con = _connect(db_path)
        try:
            con.execute(
                "UPDATE task_history SET status=?, phase=?, updated_at=CURRENT_TIMESTAMP"
                " WHERE task_id=? AND status IN (?,?)",
                (RUNNING, phase, task_id, QUEUED, "PENDING"),
            )
            con.commit()
            return True
        finally:
            con.close()
    except Exception as exc:
        logger.warning("任务 %s 标记运行失败：%s", task_id, str(exc)[:200])
        return False


def set_phase(task_id: str, phase: str, db_path: str | None = None) -> bool:
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
            return True
        finally:
            con.close()
    except Exception as exc:
        logger.warning("任务 %s 阶段写入失败：%s", task_id, str(exc)[:200])
        return False


def update_report(task_id: str, report: str, db_path: str | None = None) -> bool:
    """只改**交付正文**（人工复核修订用）：状态/步骤/日志/验收一律不动。

    页面、Markdown/PDF 导出、分享页都读这处正文；修订若只写进版本库，
    用户改完看到的仍是旧文（版本库是审计轨迹，不是展示源）。

    CANCELLED 是持久终态，与 `review/edit` 的 409 口径一致：不改。
    """
    try:
        con = _connect(db_path)
        try:
            _add_missing_columns(con)
            row = con.execute(
                "SELECT status FROM task_history WHERE task_id=?", (task_id,)
            ).fetchone()
            if row and str(row[0] or "") == CANCELLED:
                logger.warning("任务 %s 已取消，忽略正文修订（不覆盖交付）", task_id)
                return False
            cur = con.execute(
                "UPDATE task_history SET report=?, updated_at=CURRENT_TIMESTAMP"
                " WHERE task_id=?",
                (str(report), task_id),
            )
            con.commit()
            return bool(cur.rowcount)
        finally:
            con.close()
    except Exception as exc:
        logger.warning("任务 %s 正文写入失败：%s", task_id, str(exc)[:200])
        return False


def update_delivery_projection(task_id: str, *, report: str = "",
                               acceptance: dict | None = None,
                               status: str = "", db_path: str | None = None) -> bool:
    """人工修订后同步**投影**：交付正文 + 验收摘要 + 状态（B 批）。

    为什么不能只用 `update_report`：页面顶部的状态与验收缺口读的是任务行里的
    `status`/`acceptance_json`，只改正文会让"正文换了、状态还是旧结论"同时出现。

    纪律：
    - CANCELLED/FAILED 一律不改（取消是持久终态；失败任务不得被改成成功）；
    - 只写这三处，步骤/日志/规则指纹不动（修订不重跑流水线）；
    - 状态由调用方按 `derive_status` 算好后传入，本函数不自行派生。
    """
    try:
        con = _connect(db_path)
        try:
            _add_missing_columns(con)
            row = con.execute(
                "SELECT status FROM task_history WHERE task_id=?", (task_id,)
            ).fetchone()
            cur_status = str(row[0] or "").upper() if row else ""
            if cur_status in (CANCELLED, FAILED):
                logger.warning("任务 %s 是终态（%s），忽略交付投影更新", task_id, cur_status)
                return False
            sets = ["report=?", "updated_at=CURRENT_TIMESTAMP"]
            params: list = [str(report)]
            if acceptance is not None:
                sets.append("acceptance_json=?")
                params.append(json.dumps(acceptance, ensure_ascii=False))
            if status:
                sets.append("status=?")
                params.append(str(status))
            params.append(task_id)
            cur = con.execute(
                f"UPDATE task_history SET {', '.join(sets)} WHERE task_id=?", params)
            con.commit()
            return bool(cur.rowcount)
        finally:
            con.close()
    except Exception as exc:
        logger.warning("任务 %s 交付投影写入失败：%s", task_id, str(exc)[:200])
        return False


def record_completion(task_id: str, *, goal: str = "", status: str = "",
                      report: str = "", steps: list | None = None,
                      logs: list | None = None, acceptance: dict | None = None,
                      db_path: str | None = None) -> bool:
    """写入终态：状态 + 报告 + 步骤/日志 + **验收摘要与规则指纹**（不再丢弃）。

    返回是否真的写入成功——调用方（编排器的 `_finalize_task`）据此决定是否把任务
    记为已终结：写失败却记为已终结，库里就会永远停在 RUNNING 且不再重试。
    """
    acceptance = acceptance or {}
    try:
        con = _connect(db_path)
        try:
            _add_missing_columns(con)
            # 取消是**可持久终态**（M0-c）：迟到的 SUCCESS/FAILED 不得把它覆盖回去
            # （用户主动停止后又被"结果回来了"改写成成功，等于把停止当没发生）
            row = con.execute(
                "SELECT status FROM task_history WHERE task_id=?", (task_id,)
            ).fetchone()
            if row and str(row[0] or "") == CANCELLED and str(status or "") != CANCELLED:
                # 返回 True：终态已经落定（CANCELLED），没有"写失败"这回事——
                # 返回 False 会让调用方重试并写 persist_failed，把拒绝误报成故障
                logger.warning(
                    "任务 %s 已是 CANCELLED 终态，忽略迟到终态 %s（不覆盖）",
                    task_id, status,
                )
                return True
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
            return True
        finally:
            con.close()
    except Exception as exc:
        logger.error("任务 %s 终态落库失败（任务库不可写）：%s", task_id, str(exc)[:200])
        return False


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
    # 研究契约（提交时先落库的那份）：读不出就是没有，不回退成"从财务载荷反推"
    req_raw = out.get("research_request_json") or ""
    try:
        out["research_request"] = json.loads(req_raw) if req_raw else {}
    except Exception:
        out["research_request"] = {}
    return out


def is_running(task_id: str, db_path: str | None = None) -> bool:
    """DB 口径的"运行中"（供 stale 豁免；配合 Redis 标记的 pid 校验）。"""
    return str(read_task(task_id, db_path).get("status") or "").upper() == RUNNING


# ---------------------------------------------------------------- 任务投影

# 运行期快照键：内存字典之外的实时态（计划树/日志/审批标记）落 Redis，
# 服务重启或多 webui 进程都能重建，不必把运行期数据写进 SQLite（避免写放大
# 与终态写竞争）。
SNAPSHOT_KEY = "task_snapshot:{tid}"
SNAPSHOT_TTL = int(os.environ.get("WM_TASK_SNAPSHOT_TTL", "86400"))


def write_snapshot(task_id: str, data: dict, ttl: int | None = None) -> bool:
    """把内存合并态写入 Redis 快照（失败静默：不影响任务执行）。

    payload 里带 `updated_ts`（写入时刻的 epoch）：快照的新鲜度是"任务是否仍在
    被驱动"的独立佐证——崩溃兜底判定不能只看运行标记的 pid（实测某平台
    `os.kill(pid, 0)` 对**存活进程**也抛 WinError 87，据此判死会误杀运行中的任务）。
    """
    try:
        import redis
        client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379") or 6379),
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2, retry=_NO_REDIS_RETRY,
        )
        payload = dict(data or {})
        payload["updated_ts"] = time.time()
        client.setex(SNAPSHOT_KEY.format(tid=task_id),
                     int(ttl or SNAPSHOT_TTL),
                     json.dumps(payload, ensure_ascii=False, default=str))
        return True
    except Exception:
        return False


def snapshot_age(task_id: str) -> float | None:
    """快照距上次更新的秒数；无快照返回 None。"""
    data = read_snapshot(task_id)
    if not data:
        return None
    try:
        ts = float(data.get("updated_ts") or 0.0)
    except Exception:
        ts = 0.0
    if ts <= 0:
        return None
    return max(0.0, time.time() - ts)


def read_snapshot(task_id: str) -> dict:
    """读取运行期快照（无 Redis/无快照返回空 dict）。"""
    try:
        import redis
        client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379") or 6379),
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2, retry=_NO_REDIS_RETRY,
        )
        raw = client.get(SNAPSHOT_KEY.format(tid=task_id))
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def drop_snapshot(task_id: str) -> None:
    try:
        import redis
        client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379") or 6379),
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2, retry=_NO_REDIS_RETRY,
        )
        client.delete(SNAPSHOT_KEY.format(tid=task_id))
    except Exception:
        pass


# 终态落库失败标记：独立键，不覆盖运行期快照（计划树/日志还用着那个键）
PERSIST_FAIL_KEY = "task_state_persist_failed:{tid}"
PERSIST_FAIL_TTL = int(os.environ.get("WM_PERSIST_FAIL_TTL", "") or 7 * 86400)


def _redis_client():
    """任务状态用的 Redis 客户端（短超时 + 不重试，Redis 不在时 2 秒内失败）。"""
    import redis
    return redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379") or 6379),
        decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
        retry=_NO_REDIS_RETRY,
    )


def mark_persist_failed(task_id: str, reason: str) -> bool:
    """记录"终态没能落库"：除日志外再留一个可查标记，避免只剩一行 warning 没人看见。"""
    try:
        _redis_client().setex(
            PERSIST_FAIL_KEY.format(tid=task_id), PERSIST_FAIL_TTL,
            json.dumps({"reason": str(reason)[:300], "ts": time.time()},
                       ensure_ascii=False))
        return True
    except Exception:
        return False


def read_persist_failed(task_id: str) -> dict:
    """读取落库失败标记（无标记或无 Redis 返回空 dict）。"""
    try:
        raw = _redis_client().get(PERSIST_FAIL_KEY.format(tid=task_id))
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def task_projection(task_id: str, db_path: str | None = None) -> dict:
    """DB 投影：`/task/{id}` 的**事实层**（与前端既有键名保持一致）。

    解析 steps_json / logs_json / acceptance_json；`report` 保留
    `final_report or report` 语义；`acceptance` 变成对象（此前从 DB 兜底时
    只取 `data["acceptance"]`，重启后恒为 None——属于真实缺陷）。

    运行期的计划树/日志/审批标记不在库里，由调用方用 Redis 快照叠加。
    """
    row = read_task(task_id, db_path)
    if not row:
        return {}
    steps: list = []
    logs: list = []
    for key, target in (("steps_json", "steps"), ("logs_json", "logs")):
        raw = row.get(key) or ""
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = []
        if target == "steps":
            steps = parsed if isinstance(parsed, list) else []
        else:
            logs = parsed if isinstance(parsed, list) else []
    return {
        "task_id": task_id,
        "status": row.get("status") or "PENDING",
        "goal": row.get("goal") or "",
        "steps": steps,
        "report": row.get("report") or "",
        "logs": logs,
        "project": row.get("project") or "",
        "revision": bool(row.get("revision")) if "revision" in row else False,
        "acceptance": row.get("acceptance") or None,
        "llm_degraded": row.get("llm_degraded") if "llm_degraded" in row else None,
        "phase": row.get("phase") or "",
        "rules_fingerprint": row.get("rules_fingerprint") or "",
        "created_at": row.get("created_at") or "",
        "completed_at": row.get("completed_at") or "",
        "conversation_id": row.get("conversation_id") or "",
    }


def merge_projection(task_id: str, overlay: dict | None = None,
                     db_path: str | None = None) -> dict:
    """投影 + 实时叠加层：快照优先、其次传入的 overlay，最后内存。

    只叠加"实时字段"（steps/logs/revision/agent_status），终态事实以 DB 为准，
    避免过期快照把已完成任务显示成运行中。
    """
    data = task_projection(task_id, db_path)
    live = read_snapshot(task_id) or {}
    if not live and overlay:
        live = overlay
    if not data:
        # 未知任务且无实时层 → 返回空，**不要伪造**一条 PENDING 骨架：
        # 调用方（报告页/任务页）要靠"空"来判定"库里没有"，再走自己的回退链；
        # 伪造骨架会让它们误判任务存在、最后给出空白页面。
        if not live:
            return {}
        data = {"task_id": task_id, "status": "PENDING", "goal": "", "steps": [],
                "report": "", "logs": [], "project": "", "revision": False,
                "acceptance": None, "llm_degraded": None}
    if live:
        for key in ("steps", "logs", "revision", "agent_status", "plan_stage"):
            if live.get(key) not in (None, [], {}):
                data[key] = live[key]
    return data


def pid_same_process(pid, started_raw: str = "") -> bool | None:
    """运行标记的持有者进程是否仍存活、且仍是当初那个进程。

    返回 ``True``（存活）/ ``False``（确认已死）/ ``None``（无法判定）。

    为什么不用 `os.kill(pid, 0)` 判死：Windows 上对**存活进程**该调用亦可能抛
    `WinError 87`（实测编排器 pid 与标记一致、psutil 可正常读取 create_time，
    但 os.kill 报 87）。把 87 当"已死"会误杀正在跑的任务并让检查点恢复到运行中
    的任务上，因此 psutil 可用时以它为准（能区分"进程不存在"与"存在但不是同一个
    进程"），不可用时 os.kill 只能证明存活、报错一律归为不可判定。

    调用方必须**只把 False 当作已死**：None 表示判据不足，宁可晚处理也不误判。
    """
    try:
        pid = int(pid or 0)
    except Exception:
        return None
    if pid <= 0:
        return None
    started_ts = 0.0
    raw = str(started_raw or "")
    if raw:
        try:
            import datetime as _dt
            started_ts = _dt.datetime.fromisoformat(
                raw.replace("Z", "+00:00")).timestamp()
        except Exception:
            started_ts = 0.0
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            created = psutil.Process(pid).create_time()
        except Exception as exc:
            if type(exc).__name__ == "NoSuchProcess":
                return False          # 进程不存在：确认为死
            # AccessDenied 等：进程很可能存在但读不到，无法做 PID 复用比对
            return True
        if started_ts and created > started_ts + 5:
            return False              # PID 复用：这不是当初那个进程
        return True
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return None                   # 无 psutil 时不敢据"报错"判死


def mark_dead_running_failed(task_id: str, reason: str = "编排器进程已退出",
                             min_age_seconds: int = 600,
                             db_path: str | None = None) -> bool:
    """崩溃兜底：DB 仍是 RUNNING、Redis 运行标记已消失 → 翻 FAILED。

    此前 webui 只"豁免"RUNNING、从不翻转，编排器崩溃后任务既不过期也不完成。

    `min_age_seconds` 是安全门槛：只有超过该时长没有更新的 RUNNING 行才会被翻转，
    避免把"刚写 RUNNING 但标记尚未落 Redis"的正常任务误杀（标记 TTL 24h，
    超长任务也不会因此被误判，因为它的 updated_at 会随阶段刷新）。

    第二道门槛是**快照新鲜度**：编排器仍活着时，监听器会持续把实时态写进
    `task_snapshot:{tid}`（每次进度/心跳一条）。快照在 `min_age_seconds` 内更新过
    → 任务显然仍被驱动 → 拒绝翻转。"持有者已死"的结论可能来自不可靠的进程探活
    （实测某平台对存活进程 `os.kill(pid, 0)` 也抛 WinError 87 被误当已死），
    而快照更新是与进程探活完全无关的独立信号，故此处不采信任何单一判据。"""
    try:
        _snap_age = snapshot_age(task_id)
        if _snap_age is not None and _snap_age < max(60, int(min_age_seconds)):
            return False
        con = _connect(db_path)
        try:
            cur = con.execute(
                "UPDATE task_history SET status=?, phase='崩溃', report=?,"
                " updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND status=?"
                " AND COALESCE(updated_at, created_at) < datetime('now', ?)",
                (FAILED, f"Task failed: {reason}", task_id, RUNNING,
                 f"-{int(max(60, min_age_seconds))} seconds"),
            )
            con.commit()
            return bool(cur.rowcount)
        finally:
            con.close()
    except Exception:
        return False


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
