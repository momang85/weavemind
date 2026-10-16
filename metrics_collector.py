"""
织光 (ZhiGuang) — 运营指标收集器 (MetricsCollector)

订阅所有系统关键事件，将指标写入 CSV 文件以供分析和演示。

收集的指标：
    - 任务成功率 (按能力类型分)
    - 平均任务完成时间
    - Replan 触发频率和成功率
    - Memory 注入采纳率
    - Worker 负载分布
"""

from __future__ import annotations

import csv
import json
import logging
import os
import signal
import time
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock

import db_paths

import redis

# Redis 客户端统一关掉 redis-py 的内建重试：默认重试会把 socket_connect_timeout
# 叠成 26~48 秒才失败（实测 127.0.0.1 26s / localhost 48s），Redis 不在时
# 表现为"服务没崩但处处卡"。NoBackoff + 0 次重试 → 稳定 2 秒内失败并可被上层降级。
from redis.backoff import NoBackoff as _NoBackoff  # noqa: E402
from redis.retry import Retry as _Retry  # noqa: E402

_NO_REDIS_RETRY = _Retry(_NoBackoff(), 0)

logger = logging.getLogger(__name__)

# 输出文件。SUMMARY_FILE 与 web_ui._METRICS_SUMMARY 读同一环境变量、
# 同一默认值（模块目录）——写读两端必须同源，否则容器里会出现
# "写 /data 读 /app"的路径分叉（Dockerfile 设 METRICS_SUMMARY=/data/...）
METRICS_FILE = os.environ.get("METRICS_FILE", "metrics.csv")
SUMMARY_FILE = os.environ.get(
    "METRICS_SUMMARY",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "metrics_summary.json"))

# 监听频道
WATCH_CHANNELS = [
    "orchestrator:response",   # 任务最终结果
    "orchestrator:plan_review", # 评审结果
    "orchestrator:alert",       # 告警事件
    "orchestrator:evolution_result", # 演化结果
    "task_result:*",            # 单个任务结果（pattern）
]


def _db_task_totals() -> dict:
    """任务状态分布：**一次查询、同一范围（task_history 全表）**给出全部计数。

    口径（返回值的 `scope` 字段同步写明）：
    - 终态 = SUCCESS + SUCCESS_WITH_ISSUES + FAILED + CANCELLED
    - running / queued 是未结束态；其余状态计入 unknown
    - 成功率分母是**终态数**，不是总任务数（此前用总数当分母，运行中的任务会稀释成功率，
      前端还不得不用"最近 50 条"的运行数去凑，100 个任务全在运行时会被算成 100% 成功）

    失败/库不存在时返回 `available=False` 的同形状结果：调用方据此显示"未知"，
    不能把缺失数据当成 0（`available=False` 与"确实 0 个失败"是两件事）。
    """
    zero = {
        "total": 0, "success": 0, "with_issues": 0, "failed": 0, "cancelled": 0,
        "running": 0, "queued": 0, "unknown": 0, "terminal": 0,
        "available": False, "scope": "task_history 全表",
    }
    try:
        import sqlite3
        path = db_paths.resolve_db_path()
        db = sqlite3.connect(path, timeout=5)
        try:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM task_history GROUP BY status"
            ).fetchall()
        finally:
            db.close()
    except Exception:
        return zero
    counts: dict[str, int] = {}
    for status, n in rows or []:
        counts[str(status or "").upper()] = int(n or 0)
    out = dict(zero)
    out["available"] = True
    out["success"] = counts.get("SUCCESS", 0)
    out["with_issues"] = counts.get("SUCCESS_WITH_ISSUES", 0)
    out["failed"] = counts.get("FAILED", 0)
    out["cancelled"] = counts.get("CANCELLED", 0) + counts.get("CANCELED", 0)
    out["running"] = counts.get("RUNNING", 0)
    out["queued"] = counts.get("QUEUED", 0) + counts.get("PENDING", 0)
    out["total"] = sum(counts.values())
    out["terminal"] = (out["success"] + out["with_issues"]
                       + out["failed"] + out["cancelled"])
    known = out["terminal"] + out["running"] + out["queued"]
    out["unknown"] = max(0, out["total"] - known)
    return out


# ── 步级分类（任务13，设计见 docs/arch_phase2_p0_metrics_split.md）──
# 一次失败的"含噪"三因：门禁跳过（检索没产出可用来源）、级联阻塞（上游失败）、
# 真实失败（worker 派发后仍 FAILED）。任务12 复盘：by_capability 的"失败"
# 里 93% 是编排记账噪音，必须分层后才能读出真实能力健康度。
_STEP_MARK_GATE = "无候选 URL"        # 编排器检索门禁跳过（未派发抓取）
_STEP_MARK_CASCADE = "Blocked by failed"  # 依赖失败被阻断（未派发）

# 分类桶：键顺序即输出顺序；`other` 是设计五桶之外的兜底（防止未归类步骤
# 让 total_planned ≠ 各桶之和而无人察觉），正常数据里应为 0
_STEP_BUCKETS = (
    "success", "unexecuted", "gate_blocked", "cascade_blocked",
    "genuine_failed", "other",
)


def _step_bucket(ri) -> str:
    """单个 `step.result` 的步级分类（**纯函数**，任务13 口径的唯一实现）。

    判定顺序有讲究（设计文件 line 26 与 line 37-39 的表述冲突，按此裁定）：
    - SUCCESS 最先：抓取成功但页面正文恰好含这些短语的步骤是成功，不是失败；
    - cascade 在 gate 之前：级联阻塞的步骤同样满足 `elapsed_sec==0 且无
      agent_id`（任务12 核实），先判门禁会把级联误记成门禁跳过；
    - genuine = 已派发（agent_id 非空）且 FAILED —— 这是唯一值得修的桶。
    """
    if not isinstance(ri, dict):
        return "unexecuted"
    status = str(ri.get("status") or "").strip().upper()
    if not status or status in ("CANCELLED", "CANCELED"):
        # 任务13 起编排器收尾会把"没跑到的步骤"补写成 CANCELLED/unexecuted；
        # 旧数据是缺 status 键 —— 两种形态都归"未执行"。
        # 两种拼写同收：库里历史行存在单 L 的 "CANCELED"（与 _db_task_totals 同口径）
        return "unexecuted"
    if status == "SUCCESS":
        return "success"
    text = str(ri.get("result") or "")
    if _STEP_MARK_CASCADE in text:
        return "cascade_blocked"
    if _STEP_MARK_GATE in text:
        return "gate_blocked"
    if str(ri.get("agent_id") or "").strip():
        if status == "FAILED":
            return "genuine_failed"
        return "other"  # 已派发但非 SUCCESS/FAILED（库中暂无此态，兜底防漏）
    try:
        elapsed = float(ri.get("elapsed_sec") or 0)
    except (TypeError, ValueError):
        elapsed = 0.0
    if elapsed == 0:
        # 未派发即被编排器拒绝（租约/许可/预算等）：无执行者、零耗时
        return "gate_blocked"
    return "other"


def _new_cap_row() -> dict:
    row = {b: 0 for b in _STEP_BUCKETS}
    row.update({
        "total_planned": 0, "total_dispatched": 0,
        "success_dispatched": 0,
        "success_rate_dispatched": None, "success_rate_planned": None,
    })
    return row


def _finalize_cap_row(row: dict) -> dict:
    """补齐两个口径的成功率（分母不同，分子都只算**真实派发成功**）：
    - success_rate_dispatched：派发步成功 / 派发步数（真实能力健康度）；
    - success_rate_planned：成功步 / 全部计划步（历史口径，含未执行/门禁/级联噪音）。
    """
    d, s, n = row["total_dispatched"], row["success"], row["total_planned"]
    sd = row["success_dispatched"]
    row["success_rate_dispatched"] = round(sd / d * 100, 1) if d else None
    row["success_rate_planned"] = round(s / n * 100, 1) if n else None
    return row


def _db_capability_stats() -> dict:
    """按能力类型的**步级分层**统计：从 `task_history.steps_json` 全表聚合。

    为什么换成任务库（`by_capability` 此前恒为 `{}` 的根因）：原实现只在
    进程内累计 `task_complete` 事件 payload 里的 `steps`，但编排器当前 10 个
    `task_complete` 发布点**没有一个带 steps**（唯一带 steps 的是
    AWAITING_CONFIRM / RUNNING 等非终态推送，被 `_process` 的终态 gate 挡掉），
    而且进程内累计重启即清零 —— 于是统计代码在、输入永远为空。
    任务库里其实有真实数据（每步含 `capability` 与 `result.status`）。

    任务13 起每个能力给出分层桶（`_step_bucket` 口径）：
    `total_planned`（计划步）/ `total_dispatched`（agent_id 非空的真实派发步）/
    `success` / `unexecuted` / `gate_blocked` / `cascade_blocked` /
    `genuine_failed`（+ 兜底 `other`）。两个成功率：
    - `success_rate_dispatched`（真实指标）：派发步成功 / 派发步数；
    - `success_rate_planned`（历史口径，含噪音）：成功步 / 全部计划步。

    覆盖范围必须显式声明在 `scope` 里：`steps_json` 自 2026-09 才开始落库，
    更早的任务没有这个字段，所以它是**有 steps 落库的那批任务**的能力分布，
    不是全量任务。
    """
    out = {
        "available": False,
        "scope": "task_history.steps_json",
        "by_capability": {},
        "task_rows": 0,
        "step_rows": 0,
        "step_total": 0,
    }
    try:
        import sqlite3
        db = sqlite3.connect(db_paths.resolve_db_path(), timeout=5)
        try:
            grand = int(db.execute(
                "SELECT COUNT(*) FROM task_history").fetchone()[0] or 0)
            rows = db.execute(
                "SELECT steps_json FROM task_history"
                " WHERE steps_json IS NOT NULL AND steps_json != ''"
            ).fetchall()
        finally:
            db.close()
    except Exception:
        return out
    caps: dict[str, dict] = defaultdict(_new_cap_row)
    step_total = 0
    for (raw,) in rows or []:
        try:
            steps = json.loads(raw)
        except Exception:
            continue
        if not isinstance(steps, list):
            continue
        for s in steps:
            if not isinstance(s, dict):
                continue
            cap = str(s.get("capability") or "").strip()
            if not cap:
                continue
            step_total += 1
            row = caps[cap]
            row["total_planned"] += 1
            ri = s.get("result")
            agent = ""
            if isinstance(ri, dict):
                agent = str(ri.get("agent_id") or "").strip()
            if agent:
                row["total_dispatched"] += 1
            bucket = _step_bucket(ri)
            row[bucket] += 1
            # 派发成功的分子：SUCCESS 且 agent_id 非空。此前若用全部成功步
            # 当分子、派发步当分母，package 会出现 127/121=105% 的荒谬值
            # （降级兜底产生的无执行者成功步混进分子）
            if bucket == "success" and agent:
                row["success_dispatched"] += 1
    out.update({
        "available": True,
        "by_capability": {k: _finalize_cap_row(v)
                          for k, v in sorted(caps.items())},
        "task_rows": grand,
        "step_rows": len(rows or []),
        "step_total": step_total,
        "scope": (
            f"task_history.steps_json 全表聚合：{len(rows or [])}/{grand} 个任务"
            f"落有 steps_json，共 {step_total} 步。分层口径见"
            " docs/arch_phase2_p0_metrics_split.md：success_rate 为真实派发口径"
            "（派发步成功/派发步数），success_rate_planned 为历史口径"
            "（含未执行/门禁/级联噪音）。该字段自 2026-09 起才写入，"
            "更早任务不在内"),
    })
    return out


class MetricsCollector:
    """实时指标收集器。"""

    def __init__(self):
        self._running = False
        self._lock = Lock()

        # 计数器
        self._total_tasks = 0
        self._success_tasks = 0
        self._failed_tasks = 0
        self._total_latency = 0.0
        self._total_cost = 0.0
        self._replan_count = 0
        self._replan_success = 0
        self._memory_injections = 0
        self._memory_adopted = 0
        self._critic_pass = 0
        self._critic_fail = 0
        self._alerts = 0

        # 能力分布**不在进程内累计**：见 _db_capability_stats（_write_summary
        # 从 task_history.steps_json 全表取）。此前的 _by_capability 累计器
        # 从未拿到过输入（task_complete payload 不带 steps）且重启清零。

        # 时间窗口内的任务（延迟样本来源）
        self._recent_tasks: list[dict] = []

        # CSV 初始化
        self._csv_file = open(METRICS_FILE, "a", newline="", encoding="utf-8")
        self._csv = csv.writer(self._csv_file)
        if os.path.getsize(METRICS_FILE) == 0:
            self._csv.writerow([
                "timestamp", "task_id", "status", "latency_sec",
                "replan_triggered", "memory_injected_chars",
                "critic_min_score", "step_count", "alert_type",
            ])

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self):
        self._running = True
        r = redis.Redis(host=os.environ.get("REDIS_HOST", "localhost"),
                        port=int(os.environ.get("REDIS_PORT", "6379")),
                        decode_responses=True,
                        socket_connect_timeout=2, socket_timeout=2,
                        retry=_NO_REDIS_RETRY)
        # 实际的 psubscribe 支持 pattern
        logger.info("MetricsCollector started, writing to %s", METRICS_FILE)

        # 简单轮询
        import threading
        def listen_exact():
            sub = r.pubsub()
            sub.subscribe("orchestrator:response", "orchestrator:plan_review",
                          "orchestrator:alert", "orchestrator:evolution_result")
            for msg in sub.listen():
                if msg["type"] == "message":
                    try:
                        self._process(msg["channel"], json.loads(msg["data"]))
                    except Exception:
                        pass

        def listen_pattern():
            sub = r.pubsub()
            sub.psubscribe("task_result:*")
            for msg in sub.listen():
                if msg["type"] == "pmessage":
                    try:
                        self._process(msg["channel"], json.loads(msg["data"]))
                    except Exception:
                        pass

        t1 = threading.Thread(target=listen_exact, daemon=True); t1.start()
        t2 = threading.Thread(target=listen_pattern, daemon=True); t2.start()

        # 定期输出汇总
        last_summary = time.time()
        while self._running:
            time.sleep(30)
            if time.time() - last_summary >= 30:
                self._write_summary()
                last_summary = time.time()

        t1.join(timeout=2)
        t2.join(timeout=2)
        self._csv_file.close()

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _process(self, channel: str, data: dict):
        now = datetime.now(timezone.utc).isoformat()

        if channel == "orchestrator:response":
            # 仅终态 task_complete 参与统计。此前"无 type/无 payload"的
            # 消息（RUNNING 全量推送、AWAITING_CONFIRM 等直接 publish 到
            # 本频道的进度消息）会落到 _handle_task_complete，被计成
            # "RUNNING→失败"任务——这是 total/failed 虚高的根因
            if data.get("type") != "task_complete" or not data.get("payload"):
                return
            _payload = data.get("payload") or {}
            tid = data.get("task_id", "")
            _el = _payload.get("elapsed_sec")
            _latency = round(float(_el), 2) if isinstance(_el, (int, float)) and _el else 0
            # 延迟样本直接用 payload 的 elapsed_sec（编排器主终态已算好并随
            # task_complete 发出）。此前 gate 在 `tid in _task_start_times` 上，
            # 而那个 dict 自初始提交起从未被赋值 —— 死代码，在线延迟样本
            # 全被丢弃，avg/p95_latency_sec 恒为 0.0。
            if _latency and tid:
                with self._lock:
                    self._recent_tasks.append({
                        "task_id": tid,
                        "status": str(_payload.get("status") or ""),
                        "latency": _latency,
                    })
                    if len(self._recent_tasks) > 100:
                        self._recent_tasks = self._recent_tasks[-50:]
            self._handle_task_complete(now, {
                "task_id": tid,
                "status": str(
                    _payload.get("status")
                    or data.get("status") or "UNKNOWN"
                ),
                "steps": _payload.get("steps") or [],
                "latency": _latency,
            })
            return

        elif channel == "orchestrator:plan_review":
            scores = data.get("scores", {})
            min_score = min(scores.values()) if scores else 10
            if data.get("verdict") == "PASS":
                self._critic_pass += 1
            else:
                self._critic_fail += 1
            self._csv.writerow([now, data.get("plan_id", ""), "CRITIC",
                                0, 0, 0, min_score, 0, ""])

        elif channel == "orchestrator:alert":
            self._alerts += 1
            self._csv.writerow([now, "", "ALERT", 0, 0, 0, 0, 0,
                              data.get("type", "")])

        elif "task_result" in channel:
            # worker 级子任务结果：过去仅在 `tid in _task_start_times` 时记一条
            # 延迟（该 dict 从未被赋值 → 分支恒不生效，已删）。子任务没有
            # 主任务终态语义，顶层任务的延迟统一由 orchestrator:response 的
            # task_complete 采集，这里不再重复计。
            return

    def _handle_task_complete(self, now: str, data: dict):
        tid = data.get("task_id", "")
        status = data.get("status", "UNKNOWN")
        steps = data.get("steps", [])

        with self._lock:
            self._total_tasks += 1
            # SUCCESS_WITH_ISSUES 是"完成但有验收缺口"，同样计成功；
            # 此前只认 SUCCESS，导致历史任务全部被统计成失败（成功率恒 0%）
            if status in ("SUCCESS", "SUCCESS_WITH_ISSUES"):
                self._success_tasks += 1
            else:
                self._failed_tasks += 1

        # 统计 Replan（能力分布改由 _db_capability_stats 从任务库全量聚合，
        # 见 _write_summary；此处不再进程内累计——payload 从不带 steps）
        replan_triggered = 0
        for s in steps:
            if "alt-" in str(s.get("step_id", "")):
                replan_triggered += 1

        self._replan_count += replan_triggered
        if status == "SUCCESS":
            self._replan_success += replan_triggered

        # Memory 注入检测（从 steps 推断）
        memory_chars = 0
        for s in steps:
            ri = s.get("result", {})
            if isinstance(ri, dict):
                res = str(ri.get("result", ""))
                if "历史背景" in res or "成功解决路径" in res:
                    memory_chars += len(res)

        # 每任务成本（O-19 台账 → O-22 汇总）
        cost_usd = 0.0
        try:
            r = redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=2, socket_timeout=2, retry=_NO_REDIS_RETRY,
            )
            ledger = r.hgetall(f"llm_usage_task:{tid}") or {}
            from costs import ledger_cost
            cost_usd = ledger_cost(ledger)
        except Exception:
            pass
        self._total_cost += cost_usd
        if self._recent_tasks:
            for t in self._recent_tasks:
                if t.get("task_id") == tid:
                    t["cost_usd"] = cost_usd
                    break
            else:
                self._recent_tasks.append({"task_id": tid, "status": status, "cost_usd": cost_usd})

        # latency_sec 列：此前硬编码 0（metrics.csv 因此无法复核延迟），
        # 现在写 task_complete 带来的 elapsed_sec
        self._csv.writerow([
            now, tid, status, data.get("latency", 0), replan_triggered,
            memory_chars, 0, len(steps), ""
        ])
        # 逐行落盘：metrics 进程被 kill 时不丢已统计数据
        # （此前 metrics.csv 长期 0 字节）
        try:
            self._csv_file.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 汇总输出
    # ------------------------------------------------------------------

    def _write_summary(self):
        with self._lock:
            # 无任务时仍写汇总：累计成本来自月度台账（与预算页同本账），
            # 不依赖本进程任务计数；成功率等在 0 任务时显示 0
            success_rate = (
                self._success_tasks / self._total_tasks * 100
                if self._total_tasks else 0.0
            )
            # 0 任务时失败率也应为 0（不能 100-0=100 误导成"全失败"）
            # 延迟样本：只认 >0 的有效值；无样本 → 下面写 None（未知），
            # 不再兜底 0.0（那会把"没有数据"伪装成"任务 0 秒完成"）
            latencies = [t.get("latency", 0) for t in self._recent_tasks
                         if isinstance(t.get("latency"), (int, float))
                         and t.get("latency") > 0]
            p95 = (sorted(latencies)[int(len(latencies) * 0.95) - 1]
                   if latencies else None)
            # Redis 可达性一次判定，决定两处的"真 0"与"不可用(null)"之分：
            # - search_engine_health：不可达 → 空 dict（原行为）
            # - cost_usd_total：get_monthly_spend 在 Redis 不在时降级返回 0.0
            #   （预算逻辑的契约，这里不改它），但把"没拿到台账"写进汇总就是
            #   "本月花了 0 美元"的伪数据 → 不可达时写 None
            redis_ok = False
            try:
                _rc = redis.Redis(
                    host=os.environ.get("REDIS_HOST", "localhost"),
                    port=int(os.environ.get("REDIS_PORT", "6379")),
                    decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2,
                    retry=_NO_REDIS_RETRY,
                )
                _rc.ping()
                redis_ok = True
            except Exception:
                redis_ok = False
            search_health = {}
            if redis_ok:
                try:
                    raw = _rc.get("search_engine_health")
                    if raw:
                        import json as _json
                        search_health = _json.loads(raw)
                except Exception:
                    pass
            cost_total = None
            if redis_ok:
                # 累计成本与预算页同本账：月度台账（Redis llm_usage_month，
                # 全进程记账、跨重启累计）。此前用"进程内累加 + 顶层任务键"
                # 口径，worker 记账落在 dispatch_id 键上读不到，恒显示 0。
                try:
                    from costs import get_monthly_spend
                    cost_total = get_monthly_spend()
                except Exception:
                    cost_total = None
            # 累计任务数/成功率：与 /api/status 同源（agents.db 的 task_history），
            # 且**同一快照、同一范围**给出全部分类计数——前端不再自行推断成功数。
            db_totals = _db_task_totals()
            available = bool(db_totals.get("available"))
            tasks = {k: db_totals.get(k, 0) for k in (
                "total", "success", "with_issues", "failed", "cancelled",
                "running", "queued", "unknown", "terminal")}
            tasks["available"] = available
            tasks["scope"] = db_totals.get("scope", "task_history 全表")
            # 能力分布：与 tasks 一样取自任务库（全表快照），口径见 scope
            cap_stats = _db_capability_stats()
            terminal = tasks["terminal"] if available else 0
            # 分母是终态数；无终态任务（或数据不可用）时成功/失败率均为 None → 前端显示"未知"
            success_rate = (tasks["success"] / terminal * 100) if terminal else None
            failure_rate = (tasks["failed"] / terminal * 100) if terminal else None
            total_tasks = tasks["total"] if available else self._total_tasks
            failed_tasks = tasks["failed"] if available else self._failed_tasks
            summary = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_tasks": total_tasks,
                "failed_tasks": failed_tasks,
                "success_rate": round(success_rate, 1) if success_rate is not None else None,
                "failure_rate": round(failure_rate, 1) if failure_rate is not None else None,
                "tasks": tasks,
                "search_health": search_health,
                "avg_latency_sec": round(
                    sum(latencies) / len(latencies), 2
                ) if latencies else None,
                "p95_latency_sec": round(float(p95), 2) if p95 is not None else None,
                "latency_sample_size": len(latencies),
                "latency_scope": (
                    "仅统计本 metrics 进程启动后收到 elapsed_sec 的主终态任务，"
                    "重启即清零，故为空 = 本进程还没结算过任务，而不是延迟为 0。"
                    "历史任务不能直接取均值：task_history.completed_at 会被后续落库"
                    "重盖（task_state ON CONFLICT DO UPDATE），需要历史值请取分位数"),
                "cost_usd_total": round(float(cost_total), 4)
                if cost_total is not None else None,
                "cost_usd_scope": (
                    "Redis llm_usage_month 月度台账（与预算页同本账）；"
                    "null = Redis 不可达，不是本月 0 花费"),
                "critic": {
                    "pass": self._critic_pass,
                    "fail": self._critic_fail,
                },
                "replan": {
                    "total": self._replan_count,
                    "success": self._replan_success,
                },
                "alerts": self._alerts,
                # 与 task_history 的全表口径不同：这三个是进程内计数，
                # 重启（含离线重跑汇总）即归零，别读成"历史上从未发生"
                "process_counters_scope": (
                    "critic / replan / alerts 均为本 metrics 进程内计数，"
                    "重启归零；0 = 本进程尚未观察到该类事件，不是历史总数"),
                "by_capability": {
                    # 键名兼容：前端 MetricsPage 读 `by_capability[cap].success_rate`
                    # （>=80/50 上色），所以 `total`/`success`/`success_rate` 三个
                    # 旧键保留不动名；语义按任务13 分层收口：
                    # - `success_rate` = **真实派发口径**（此前是含噪音的计划口径
                    #   68.3%，实为 93.2%），前端显示的即真实健康度；
                    # - `success_rate_planned` = 历史口径（**含噪音**：把未执行、
                    #   门禁跳过、级联阻塞都算成失败），只供对比/追溯；
                    # - 其余分层桶与两个总数为新增字段，便于定位缺口来源。
                    k: {
                        "total": v["total_planned"],
                        "success": v["success"],
                        "success_rate": v["success_rate_dispatched"],
                        "success_rate_planned": v["success_rate_planned"],
                        "total_planned": v["total_planned"],
                        "total_dispatched": v["total_dispatched"],
                        "success_dispatched": v["success_dispatched"],
                        "unexecuted": v["unexecuted"],
                        "gate_blocked": v["gate_blocked"],
                        "cascade_blocked": v["cascade_blocked"],
                        "genuine_failed": v["genuine_failed"],
                        "other": v["other"],
                    }
                    for k, v in sorted(cap_stats["by_capability"].items())
                },
                # 覆盖范围声明：steps_json 自 2026-09 才落库，空字典 = 库里
                # 一条 steps_json 都没有（不是"没有能力统计代码"）
                "by_capability_scope": cap_stats["scope"],
                "by_capability_available": cap_stats["available"],
                # 分层口径说明随汇总下发：`success_rate` 换口径后，读汇总的人
                # 必须能看出"计划口径"与"派发口径"的区别，否则会拿旧数字对新数字
                "by_capability_metric_note": (
                    "success_rate = 真实派发口径（派发步成功/派发步数）；"
                    "success_rate_planned = 历史口径（成功步/计划步，**含噪音**："
                    "未执行 + 门禁跳过 + 级联阻塞均计入失败）；"
                    "genuine_failed = agent_id 非空且 status=FAILED，是唯一"
                    "值得修的能力失败桶。判定顺序：unexecuted → success → "
                    "cascade_blocked → gate_blocked → genuine_failed → other。"
                    "other = 未归类残渣（全表 6 步）：`_dispatch` 在超时/协议错误/"
                    "预算拒绝等路径直接返回结果且**不带 agent_id**，故落不进"
                    " genuine_failed（含 300s/600s 超时 3 步、协议错误 1 步、"
                    "预算拒绝 2 步）——要修的是 `_dispatch` 的记账，不是分类口径"),
            }

            with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)

            logger.info(
                "Metrics: %d tasks (%d terminal), %.1f%% success, %d replans, %d alerts",
                tasks["total"], terminal, success_rate or 0.0,
                self._replan_count, self._alerts,
            )

    def shutdown(self):
        self._running = False
        self._csv_file.close()


def main():
    from logging_setup import setup_logging
    setup_logging("metrics")
    collector = MetricsCollector()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, lambda s, f: collector.shutdown())
        except Exception:
            pass

    try:
        collector.run()
    except KeyboardInterrupt:
        collector.shutdown()


if __name__ == "__main__":
    main()
