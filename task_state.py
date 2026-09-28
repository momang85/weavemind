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
#
# C3/H3b：`RECEIVED` = **已收执但还没被消费**。提交方先把收执落库再触发工作，
# 于是"请求到了但编排器没起来/崩了"是一个**可查、可恢复**的状态，
# 而不是一条 120 秒后消失的 Redis 键（旧行为：用户只看到超时，无法判断发生了什么）。
RECEIVED = "RECEIVED"
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
    if "idempotency_key" not in existing:
        # C3/H3：幂等键。同一键的重复提交（双击、超时重试、代理重放）只产生一个任务。
        con.execute(
            "ALTER TABLE task_history ADD COLUMN idempotency_key TEXT DEFAULT ''")
        added.append("idempotency_key")
    if "submit_timeline_json" not in existing:
        # C3/H3：提交时间线（收到/持久化/发布/消费/开始），含实例与代码版本。
        con.execute(
            "ALTER TABLE task_history ADD COLUMN submit_timeline_json TEXT DEFAULT ''")
        added.append("submit_timeline_json")
    if "accepted_by" not in existing:
        # C3/H3：实例归属——这条任务由哪个实例实例化，用于拒绝重复消费。
        con.execute(
            "ALTER TABLE task_history ADD COLUMN accepted_by TEXT DEFAULT ''")
        added.append("accepted_by")
    if "idem_scope" not in existing:
        # P0-d：幂等作用域（可信用户|工作区|操作）——键只在作用域内唯一，
        # 避免两个用户自造的键互相顶掉。
        con.execute(
            "ALTER TABLE task_history ADD COLUMN idem_scope TEXT DEFAULT ''")
        added.append("idem_scope")
    if "request_fingerprint" not in existing:
        # P0-d：请求内容指纹——同键同内容 = 重试（duplicate），
        # 同键不同内容 = **冲突**（conflict，拒绝执行）。
        con.execute(
            "ALTER TABLE task_history ADD COLUMN request_fingerprint TEXT DEFAULT ''")
        added.append("request_fingerprint")
    return added


# 提交时间线：事件名 → 说明（读者/页面按同一套命名读，不再各处自造）
SUBMIT_EVENTS = ("received", "persisted", "published", "consumed", "started",
                 "deduplicated", "rejected")
_SUBMIT_TIMELINE_MAX = 40


def record_submit_event(task_id: str, event: str, *, instance: str = "",
                        code_version: str = "", detail: str = "",
                        ts: float | None = None, db_path: str | None = None) -> bool:
    """把一条提交时间线事件**追加**到任务行（C3/H3）。

    "收到请求 → 持久化 → 发布 → 消费 → 开始"必须能按时间与实例对账：只靠一条
    会过期的 Redis 收执键，事后无法回答"请求到没到、是谁收的、卡在哪一段"。
    追加在一条 JSON 列里（读少写少，不另建表），最多保留最后 40 条。
    """
    if not task_id:
        return False
    entry = {"event": str(event or "")[:32], "ts": round(float(ts if ts is not None
                                                             else time.time()), 3),
             "instance": str(instance or "")[:64],
             "code_version": str(code_version or "")[:40],
             "detail": str(detail or "")[:200]}
    try:
        con = _connect(db_path)
        try:
            _add_missing_columns(con)
            row = con.execute(
                "SELECT submit_timeline_json FROM task_history WHERE task_id=?",
                (task_id,)).fetchone()
            if row is None:
                return False
            try:
                items = json.loads(row[0] or "[]")
            except Exception:
                items = []
            if not isinstance(items, list):
                items = []
            items.append(entry)
            con.execute(
                "UPDATE task_history SET submit_timeline_json=?, updated_at=CURRENT_TIMESTAMP"
                " WHERE task_id=?", (json.dumps(items[-_SUBMIT_TIMELINE_MAX:],
                                                ensure_ascii=False), task_id))
            con.commit()
            return True
        finally:
            con.close()
    except Exception as exc:                          # noqa: BLE001
        logger.warning("任务 %s 提交时间线写入失败：%s", task_id, str(exc)[:160])
        return False


def read_submit_timeline(task_id: str, db_path: str | None = None) -> list[dict]:
    """读提交时间线（读不到返回空列表，不抛）。"""
    try:
        con = _connect(db_path)
        try:
            _add_missing_columns(con)
            row = con.execute(
                "SELECT submit_timeline_json FROM task_history WHERE task_id=?",
                (task_id,)).fetchone()
        finally:
            con.close()
    except Exception:
        return []
    if not row:
        return []
    try:
        items = json.loads(row[0] or "[]")
    except Exception:
        return []
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def receipt_scope(*, user: str = "", project: str = "default",
                  operation: str = "task.submit") -> str:
    """幂等作用域：**可信用户 + 工作区(项目) + 操作**。

    为什么必须有作用域：只用 `idempotency_key` 去重时，两个**不同用户**的键撞车
    （客户端自造键、短键、时间戳键）会互相顶掉——A 的提交被判成 B 的重复提交而丢弃。
    作用域把"谁、在哪个工作区、做什么"绑进裁决，键只在这个作用域内唯一。
    `user` 必须来自**会话身份**（网页提交方已按 `admin["user"]` 取），不接受请求体自报。
    """
    return "|".join((str(user or "").strip(), str(project or "default").strip(),
                     str(operation or "").strip()))


def request_fingerprint(goal: str, *, project: str = "default",
                        research_request: dict | None = None) -> str:
    """请求内容指纹：判定"同键**是否同一个请求**"。

    只取**决定研究是什么**的字段（目标 + 项目 + 研究契约）；不含 `conversation_id` /
    `parent_task_id` / `context`——那些是会话与提示，重试时本来就可能不同，
    算进去会把正常重试误判成冲突。
    """
    import hashlib
    req = research_request if isinstance(research_request, dict) else {}
    try:
        payload = json.dumps({
            "goal": str(goal or "").strip(),
            "project": str(project or "default").strip(),
            "research_request": req,
        }, ensure_ascii=False, sort_keys=True)
    except Exception:                                 # noqa: BLE001
        payload = f"{goal}|{project}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def claim_receipt(task_id: str, goal: str, *, project: str = "default",
                  conversation_id: str = "", parent_task_id: str = "",
                  context: str = "", user: str = "",
                  research_request: dict | None = None,
                  idempotency_key: str = "", operation: str = "task.submit",
                  instance: str = "", code_version: str = "",
                  submit_events: list | None = None,
                  db_path: str | None = None) -> tuple[str, str]:
    """**原子**裁决"这条请求的收执归谁"：查重与落收执合成一个事务。

    返回 `(verdict, 关联任务 id)`：

    | verdict | 含义 | 调用方义务 |
    |---|---|---|
    | `created` | 本次落了新收执 | **拥有**这次请求，继续发布/执行 |
    | `duplicate` | 同作用域同键**且内容相同** | 复用既有任务，**不重复发布/执行** |
    | `conflict` | 同作用域同键但**内容不同** | **明确冲突**，拒绝执行并如实回报 |
    | `error` | 数据库异常 | 不落库、不执行（保留恢复状态） |

    为什么必须原子（`BEGIN IMMEDIATE`）：旧实现是"先 `find_by_idempotency` 查、
    再 `INSERT`"两步——两个并发请求可以**各落一行 RECEIVED**；随后消费 A 映射到 B，
    库里留下 A，启动恢复又执行 A，同一份提交被执行两次（重复付费）。
    写锁把"查 + 插"变成串行裁决，同一 `(scope, key)` 只可能有一行。

    无幂等键时行为与旧路径一致：总是落一条新收执（`created`）。
    """
    request_json = ""
    if isinstance(research_request, dict) and research_request:
        try:
            request_json = json.dumps(research_request, ensure_ascii=False)
        except Exception:                             # noqa: BLE001
            request_json = ""
    key = str(idempotency_key or "").strip()
    scope = receipt_scope(user=user, project=project, operation=operation)
    fp = request_fingerprint(goal, project=project, research_request=research_request) if key else ""
    events = [e for e in (_norm_submit_event(x) for x in (submit_events or [])) if e]
    events.append(_norm_submit_event({
        "event": "received", "instance": instance, "code_version": code_version,
        "detail": "收执已落库（等待编排器消费）"}) or {})
    timeline_json = json.dumps(events[-_SUBMIT_TIMELINE_MAX:], ensure_ascii=False)
    con = None
    try:
        con = _connect(db_path)
        _add_missing_columns(con)
        con.isolation_level = None                    # 自己管事务，用显式 BEGIN IMMEDIATE
        con.execute("BEGIN IMMEDIATE")                # ← 写锁：查+插 串行化
        if key:
            row = con.execute(
                "SELECT task_id, request_fingerprint FROM task_history"
                " WHERE idem_scope=? AND idempotency_key=? LIMIT 1",
                (scope, key)).fetchone()
            if row:
                existing_id = str(row[0] or "")
                same = str(row[1] or "") == fp
                con.execute("COMMIT")
                if same:
                    return "duplicate", existing_id
                logger.warning("幂等键冲突：作用域 %s 的键 %s 已属于任务 %s，"
                               "但本次请求内容不同 → 明确冲突（不执行）",
                               scope, key, existing_id)
                return "conflict", existing_id
        dup = con.execute("SELECT task_id FROM task_history WHERE task_id=? LIMIT 1",
                          (task_id,)).fetchone()
        if dup:
            existing_id = str(dup[0] or "")
            con.execute("COMMIT")
            return "duplicate", existing_id
        con.execute(
            "INSERT INTO task_history"
            "(task_id,goal,status,project,conversation_id,parent_task_id,context,user,"
            " phase,research_request_json,idempotency_key,submit_timeline_json,"
            " accepted_by,idem_scope,request_fingerprint)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, goal, RECEIVED, project, conversation_id, parent_task_id,
             context, user, "待消费", request_json, key, timeline_json, "",
             scope, fp))
        con.execute("COMMIT")
        return "created", task_id
    except Exception as exc:                          # noqa: BLE001
        logger.error("任务 %s 收执原子裁决失败（不落库、不执行）：%s", task_id, str(exc)[:200])
        try:
            if con is not None:
                con.execute("ROLLBACK")
        except Exception:                             # noqa: BLE001
            pass
        return "error", ""
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:                             # noqa: BLE001
            pass


def mark_received(task_id: str, goal: str, *, project: str = "default",
                  conversation_id: str = "", parent_task_id: str = "",
                  context: str = "", user: str = "",
                  research_request: dict | None = None,
                  idempotency_key: str = "", instance: str = "",
                  code_version: str = "", submit_events: list | None = None,
                  db_path: str | None = None) -> bool:
    """**提交方**先落一条收执行（C3/H3b）：状态 `RECEIVED`，表示"收到了，还没被消费"。

    为什么由提交方写：指令要求"先保存可追踪收执，再触发工作"。原来是编排器收到消息
    才登记——编排器没起来/在崩溃窗口里，这条请求就**什么都不剩**（只有一条会过期的
    Redis 键），用户看到超时，无从判断是没到、到了没跑、还是跑了失败。

    只写**收执**这一个状态：QUEUED 及之后的所有迁移仍由编排器独占
    （`mark_queued` / `mark_running` / `record_completion`）。
    同一 task_id 重复写视为幂等（返回 True，不覆盖已有行）。
    """
    # P0-d：查重与落收执走**同一个原子裁决**（claim_receipt），不再"先查后插"。
    verdict, _ref = claim_receipt(
        task_id, goal, project=project, conversation_id=conversation_id,
        parent_task_id=parent_task_id, context=context, user=user,
        research_request=research_request, idempotency_key=idempotency_key,
        instance=instance, code_version=code_version,
        submit_events=submit_events, db_path=db_path)
    # 兼容旧布尔契约：created/duplicate 都表示"收执在库"；
    # conflict（同键不同内容）与 error 表示**这条请求没落库**，必须如实返回 False。
    if verdict == "conflict":
        logger.error("任务 %s 因幂等键冲突被拒绝落库（同作用域同键但内容不同）", task_id)
        return False
    return verdict in ("created", "duplicate")

def promote_received(task_id: str, *, instance: str = "", code_version: str = "",
                     db_path: str | None = None) -> str:
    """把 `RECEIVED` 收执**原子地**推进到 `QUEUED`（谁赢谁执行）。

    返回四种裁决（**异常不是"缺行"**）：
    - `"promoted"`：本次赢得执行权；
    - `"already"`：行存在但已不是 RECEIVED（别人推进过，含 RUNNING/终态）
      → **不要重复执行**；
    - `"absent"`：**确实没有这一行**（老路径或直接调用编排器的场景）；
    - `"error"`：**数据库异常**——不是"没有收执"。调用方必须**拒绝本次执行并保留
      恢复状态**，绝不能靠异常回退到旧路径：那会把一条正在 RUNNING 的任务再次启动
      （内存反例：一次连接异常 → 返回 accepted 且无 skip → 允许再次执行）。

    `UPDATE ... WHERE status='RECEIVED'` 是唯一的裁决点：网页提交的消息与
    "启动时恢复未消费收执"两条路径同时存在时，只有一个能把 RECEIVED 改成 QUEUED，
    因此同一条请求不会被执行两次。
    """
    try:
        con = _connect(db_path)
        try:
            _add_missing_columns(con)
            cur = con.execute(
                "UPDATE task_history SET status=?, phase=?, accepted_by=?,"
                " updated_at=CURRENT_TIMESTAMP"
                " WHERE task_id=? AND status=?",
                (QUEUED, "排队", str(instance or "")[:64], task_id, RECEIVED))
            won = int(cur.rowcount or 0) > 0
            con.commit()
        finally:
            con.close()
    except Exception as exc:                          # noqa: BLE001
        logger.error("任务 %s 收执推进失败（报 error，不回退旧路径）：%s",
                     task_id, str(exc)[:160])
        return "error"
    if won:
        record_submit_event(task_id, "consumed", instance=instance,
                            code_version=code_version,
                            detail="编排器已消费该收执并登记为 QUEUED", db_path=db_path)
        return "promoted"
    # 没推进成功 → 分清"行在但不是 RECEIVED"（already）与"真的没有行"（absent）。
    # 读也失败时同样报 error：读不出来不等于没有收执，不得据此放行重复执行。
    try:
        row = read_task(task_id, db_path)
    except Exception as exc:                          # noqa: BLE001
        logger.error("任务 %s 收执状态读取失败（报 error）：%s", task_id, str(exc)[:160])
        return "error"
    return "already" if row else "absent"


def list_received(*, older_than: float = 0.0, limit: int = 50,
                  db_path: str | None = None) -> list[dict]:
    """列出**已收执但从未被消费**的任务（C3/H3b 启动恢复用）。

    `older_than`：只取落库超过这么多秒的（默认 0 = 全部）。调用方用一个小阈值
    避开"刚刚提交、正在飞"的那几秒。
    """
    out: list[dict] = []
    try:
        con = _connect(db_path)
        try:
            _add_missing_columns(con)
            rows = con.execute(
                "SELECT task_id,goal,project,conversation_id,parent_task_id,context,"
                " user,research_request_json,idempotency_key,"
                " CAST(strftime('%s','now') AS INTEGER)"
                " - CAST(strftime('%s',created_at) AS INTEGER) AS age"
                " FROM task_history WHERE status=? ORDER BY rowid ASC LIMIT ?",
                (RECEIVED, int(limit))).fetchall()
        finally:
            con.close()
    except Exception:                                 # noqa: BLE001
        return []
    for r in rows:
        try:
            age = float(r[9] or 0)
        except (TypeError, ValueError):
            age = 0.0
        if age < float(older_than):
            continue
        req: dict = {}
        try:
            req = json.loads(r[7] or "{}")
        except Exception:
            req = {}
        out.append({"task_id": str(r[0] or ""), "goal": str(r[1] or ""),
                    "project": str(r[2] or "default"),
                    "conversation_id": str(r[3] or ""),
                    "parent_task_id": str(r[4] or ""), "context": str(r[5] or ""),
                    "user_id": str(r[6] or ""), "research_request": req,
                    "idempotency_key": str(r[8] or ""), "age": age})
    return out


def list_unstarted_queued(*, older_than: float = 120.0, limit: int = 20,
                          db_path: str | None = None) -> list[dict]:
    """列出"已登记 `QUEUED` 但**从未开始执行**"的任务（P0-c 崩溃窗口恢复）。

    覆盖的窗口：`promote_received` 把 RECEIVED→QUEUED 之后、执行线程真正起来之前
    进程崩了。这类行状态是 **QUEUED**，而旧恢复只扫 RECEIVED —— 于是它**永久漏掉**：
    任务卡在"排队中"，既没人执行也不会失败，用户永远等不到结果。

    "未开始"的判据用**时间线**（有没有 `started` 事件），不是靠猜：
    `run_and_finalize` 一进线程就写 `started`，所以"没有 started 的 QUEUED"= 确实没跑过。
    正在跑的行是 RUNNING，一并不在候选里。

    `older_than` 默认 120 秒：给正常路径（消息驱动）足够时间把行推进到 RUNNING，
    避免和"刚登记、消息正在飞"的那几秒抢同一个任务。
    """
    out: list[dict] = []
    try:
        con = _connect(db_path)
        try:
            _add_missing_columns(con)
            rows = con.execute(
                "SELECT task_id,goal,project,conversation_id,parent_task_id,context,"
                " user,research_request_json,idempotency_key,submit_timeline_json,"
                " CAST(strftime('%s','now') AS INTEGER)"
                " - CAST(strftime('%s',updated_at) AS INTEGER) AS age"
                " FROM task_history WHERE status=? ORDER BY rowid ASC LIMIT ?",
                (QUEUED, int(limit))).fetchall()
        finally:
            con.close()
    except Exception as exc:                          # noqa: BLE001
        logger.warning("未开始 QUEUED 扫描失败：%s", str(exc)[:160])
        return []
    for r in rows:
        try:
            age = float(r[10] or 0)
        except (TypeError, ValueError):
            age = 0.0
        if age < float(older_than):
            continue
        try:
            events = json.loads(r[9] or "[]")
        except Exception:                             # noqa: BLE001
            events = []
        if not isinstance(events, list):
            events = []
        if any(isinstance(e, dict) and str(e.get("event")) == "started" for e in events):
            continue                                  # 已经开始过（可能在跑），不抢
        req: dict = {}
        try:
            req = json.loads(r[7] or "{}")
        except Exception:                             # noqa: BLE001
            req = {}
        out.append({"task_id": str(r[0] or ""), "goal": str(r[1] or ""),
                    "project": str(r[2] or "default"),
                    "conversation_id": str(r[3] or ""),
                    "parent_task_id": str(r[4] or ""), "context": str(r[5] or ""),
                    "user_id": str(r[6] or ""), "research_request": req,
                    "idempotency_key": str(r[8] or ""), "age": age})
    return out


def find_by_idempotency(idempotency_key: str,
                        db_path: str | None = None) -> dict | None:
    """按幂等键找**已存在**的任务（最近的优先）。找不到返回 None。

    用途有两个：提交前查一次（避免重复发布），以及编排器收执时再查一次
    （两个提交竞争时，先落库的那个赢）。
    """
    key = str(idempotency_key or "").strip()
    if not key:
        return None
    try:
        con = _connect(db_path)
        try:
            _add_missing_columns(con)
            row = con.execute(
                "SELECT task_id,status,phase,project FROM task_history"
                " WHERE idempotency_key=? ORDER BY rowid DESC LIMIT 1",
                (key,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None
    if not row:
        return None
    return {"task_id": str(row[0] or ""), "status": str(row[1] or ""),
            "phase": str(row[2] or ""), "project": str(row[3] or ""),
            "idempotency_key": key}


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


def _norm_submit_event(ev) -> dict | None:
    """提交时间线事件规范化（字段与长度上限统一在一处）。"""
    if not isinstance(ev, dict) or not ev.get("event"):
        return None
    try:
        ts = float(ev.get("ts") if ev.get("ts") is not None else time.time())
    except (TypeError, ValueError):
        ts = time.time()
    return {"event": str(ev.get("event"))[:32], "ts": round(ts, 3),
            "instance": str(ev.get("instance") or "")[:64],
            "code_version": str(ev.get("code_version") or "")[:40],
            "detail": str(ev.get("detail") or "")[:200]}


def mark_queued(task_id: str, goal: str, project: str = "default",
                conversation_id: str = "", parent_task_id: str = "",
                context: str = "", user: str = "",
                research_request: dict | None = None,
                idempotency_key: str = "", instance: str = "",
                code_version: str = "", submit_events: list | None = None,
                db_path: str | None = None) -> bool:
    """登记排队中的任务（提交时调用）。返回是否**真的写入成功**。

    返回值的意义：提交收执（`task_ack`）必须依据"是否真的登记成功"。此前本函数
    吞掉一切异常后正常返回 None，于是 `accept_task_request` 里"靠异常区分
    accepted / rejected"的分支永不触发——任务库不可写时提交方仍拿到 accepted。

    `research_request`：研究契约（公司/市场/两期/口径/截至日/来源要求/预算）。
    **在提交时先落库**，后续底稿只读它——抓取到的公司名/代码只能作为候选比对，
    不能反过来改写用户请求（否则请求会被数据源决定）。

    C3/H3 新增：`idempotency_key`（重复提交只算一个任务）、`instance`/`code_version`
    （实例归属，写入 `accepted_by`）、`submit_events`（收到的请求自带的
    received/published 时刻，与本次 persisted 一起落到同一列，便于事后对账）。
    """
    request_json = ""
    if isinstance(research_request, dict) and research_request:
        try:
            request_json = json.dumps(research_request, ensure_ascii=False)
        except Exception as exc:
            logger.warning("任务 %s 研究契约无法序列化（按空处理）：%s", task_id, str(exc)[:120])
            request_json = ""
    events = [e for e in (_norm_submit_event(x) for x in (submit_events or [])) if e]
    events.append(_norm_submit_event({
        "event": "persisted", "instance": instance, "code_version": code_version,
        "detail": "task_history 落库成功（队列已登记）"}) or {})
    timeline_json = json.dumps(events[-_SUBMIT_TIMELINE_MAX:], ensure_ascii=False)
    try:
        con = _connect(db_path)
        try:
            # 编排器可能先于 web_ui 初始化库；缺列会让写入静默失败
            _add_missing_columns(con)
            # C3/H3b：提交方可能已经落了收执行（RECEIVED）——那就**推进**它，
            # 不能 INSERT（主键冲突会变成"登记失败"）也不能 REPLACE（会抹掉收执时间线）。
            # 只允许从 RECEIVED/QUEUED 推进，绝不把 RUNNING/终态改回 QUEUED。
            row = con.execute("SELECT status FROM task_history WHERE task_id=?",
                              (task_id,)).fetchone()
            if row is not None:
                cur_status = str(row[0] or "")
                if cur_status in (RECEIVED, QUEUED):
                    con.execute(
                        "UPDATE task_history SET status=?, phase=?, accepted_by=?,"
                        " updated_at=CURRENT_TIMESTAMP WHERE task_id=?",
                        (QUEUED, "排队", str(instance or "")[:64], task_id))
                    con.commit()
                    record_submit_event(
                        task_id, "persisted", instance=instance,
                        code_version=code_version,
                        detail=f"收执推进为 QUEUED（原状态 {cur_status}）", db_path=db_path)
                else:
                    # 已经在跑或已终结：登记视为幂等成功，但**不改状态**
                    logger.info("任务 %s 已处于 %s，登记按幂等处理", task_id, cur_status)
                return True
            con.execute(
                "INSERT INTO task_history"
                "(task_id,goal,status,project,conversation_id,parent_task_id,context,user,"
                " phase,research_request_json,idempotency_key,submit_timeline_json,"
                " accepted_by)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, goal, QUEUED, project, conversation_id,
                 parent_task_id, context, user, "排队", request_json,
                 str(idempotency_key or "").strip(), timeline_json,
                 str(instance or "")[:64]),
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
