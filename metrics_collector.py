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

import redis

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
                        decode_responses=True)
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
            # 跳过进度消息（带 type+payload），只统计最终结果
            if data.get("type") and data.get("payload"):
                return
            self._handle_task_complete(now, data)

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
            if status == "SUCCESS":
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

        self._csv.writerow([
            now, tid, status, 0, replan_triggered,
            memory_chars, 0, len(steps), ""
        ])

    # ------------------------------------------------------------------
    # 汇总输出
    # ------------------------------------------------------------------

    def _write_summary(self):
        with self._lock:
            if self._total_tasks == 0:
                return

            success_rate = self._success_tasks / self._total_tasks * 100
            summary = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "total_tasks": self._total_tasks,
                "success_rate": round(success_rate, 1),
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
                "Metrics: %d tasks, %.1f%% success, %d replans, %d alerts",
                self._total_tasks, success_rate, self._replan_count, self._alerts,
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
