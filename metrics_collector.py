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

# 输出文件
METRICS_FILE = os.environ.get("METRICS_FILE", "metrics.csv")
SUMMARY_FILE = os.environ.get("METRICS_SUMMARY", "metrics_summary.json")

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

        # 按能力类型
        self._by_capability: dict[str, dict] = defaultdict(
            lambda: {"total": 0, "success": 0}
        )

        # 时间窗口内的任务
        self._recent_tasks: list[dict] = []
        self._task_start_times: dict[str, float] = {}

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
            if _el and tid and tid in self._task_start_times:
                self._recent_tasks.append({
                    "task_id": tid,
                    "status": str(_payload.get("status") or ""),
                    "latency": round(float(_el), 2),
                })
                self._task_start_times.pop(tid, None)
            self._handle_task_complete(now, {
                "task_id": tid,
                "status": str(
                    _payload.get("status")
                    or data.get("status") or "UNKNOWN"
                ),
                "steps": _payload.get("steps") or [],
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
            tid = data.get("task_id", "")
            if tid and tid in self._task_start_times:
                latency = time.time() - self._task_start_times.pop(tid)
                with self._lock:
                    self._recent_tasks.append({
                        "task_id": tid, "status": data.get("status"),
                        "latency": round(latency, 2),
                    })
                    if len(self._recent_tasks) > 100:
                        self._recent_tasks = self._recent_tasks[-50:]

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

        # 统计 Replan
        replan_triggered = 0
        for s in steps:
            cap = s.get("capability", "")
            self._by_capability[cap]["total"] += 1
            ri = s.get("result", {})
            if isinstance(ri, dict) and ri.get("status") == "SUCCESS":
                self._by_capability[cap]["success"] += 1
            if "alt-" in s.get("step_id", ""):
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

        self._csv.writerow([
            now, tid, status, 0, replan_triggered,
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
            latencies = [t.get("latency", 0) for t in self._recent_tasks
                         if isinstance(t.get("latency"), (int, float))]
            p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1] if latencies else 0.0
            search_health = {}
            try:
                r = redis.Redis(
                    host=os.environ.get("REDIS_HOST", "localhost"),
                    port=int(os.environ.get("REDIS_PORT", "6379")),
                    decode_responses=True,
                    socket_connect_timeout=2, socket_timeout=2, retry=_NO_REDIS_RETRY,
                )
                raw = r.get("search_engine_health")
                if raw:
                    import json as _json
                    search_health = _json.loads(raw)
            except Exception:
                pass
            # 累计成本与预算页同本账：月度台账（Redis llm_usage_month，
            # 全进程记账、跨重启累计）。此前用"进程内累加 + 顶层任务键"
            # 口径，worker 记账落在 dispatch_id 键上读不到，恒显示 0。
            try:
                from costs import get_monthly_spend
                cost_total = get_monthly_spend()
            except Exception:
                cost_total = 0.0
            # 累计任务数/成功率：与 /api/status 同源（agents.db 的 task_history），
            # 且**同一快照、同一范围**给出全部分类计数——前端不再自行推断成功数。
            db_totals = _db_task_totals()
            available = bool(db_totals.get("available"))
            tasks = {k: db_totals.get(k, 0) for k in (
                "total", "success", "with_issues", "failed", "cancelled",
                "running", "queued", "unknown", "terminal")}
            tasks["available"] = available
            tasks["scope"] = db_totals.get("scope", "task_history 全表")
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
                ) if latencies else 0.0,
                "p95_latency_sec": round(float(p95), 2),
                "cost_usd_total": round(float(cost_total), 4),
                "critic": {
                    "pass": self._critic_pass,
                    "fail": self._critic_fail,
                },
                "replan": {
                    "total": self._replan_count,
                    "success": self._replan_success,
                },
                "alerts": self._alerts,
                "by_capability": {
                    k: {"success_rate": round(v["success"] / v["total"] * 100, 1) if v["total"] else 0}
                    for k, v in self._by_capability.items()
                },
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
