"""
织光 (ZhiGuang) — 演化调度器 (Scheduler)

在系统低负载时段自动触发策略演化：
    - 默认每天凌晨 3:00 执行
    - 从记忆库抽取历史任务作为测试集
    - 调用 EvolutionSandbox 运行锦标赛
    - 将优胜策略推送到部署频道

用法:
    python scheduler.py                    # 前台运行，等待调度时间
    python scheduler.py --now              # 立即执行一次
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from typing import Any

from common import MessagingClient, AgentRegistry
from evolution_sandbox import EvolutionSandbox
from memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# 默认调度时间（凌晨 3:00）
SCHEDULE_HOUR = int(os.environ.get("EVOLUTION_HOUR", "3"))
SCHEDULE_MINUTE = int(os.environ.get("EVOLUTION_MINUTE", "0"))

# 每次演化使用的测试任务数
TEST_TASK_COUNT = 10


class EvolutionScheduler:
    """定时策略演化调度器。"""

    def __init__(
        self,
        messaging: MessagingClient,
        registry: AgentRegistry,
        memory: MemoryManager,
        sandbox: EvolutionSandbox,
    ) -> None:
        self._messaging = messaging
        self._registry = registry
        self._memory = memory
        self._sandbox = sandbox
        self._running = False
        self._last_run: datetime | None = None

    def run(self) -> None:
        """启动调度循环。"""
        self._setup_signal_handlers()
        self._running = True

        logger.info(
            "EvolutionScheduler started. Next run: daily at %02d:%02d",
            SCHEDULE_HOUR, SCHEDULE_MINUTE,
        )

        while self._running:
            now = datetime.now()
            next_run = self._next_scheduled_time(now)

            wait_seconds = (next_run - now).total_seconds()
            if wait_seconds > 0:
                logger.info(
                    "Next evolution run in %.0f minutes (at %s)",
                    wait_seconds / 60,
                    next_run.strftime("%Y-%m-%d %H:%M"),
                )
                # 分段 sleep
                for _ in range(int(min(wait_seconds, 3600))):
                    if not self._running:
                        break
                    time.sleep(1)
                if not self._running:
                    break

            # 到了预定时间
            if self._last_run and (datetime.now() - self._last_run).total_seconds() < 300:
                # 5 分钟内已执行过，跳过
                time.sleep(60)
                continue

            self._run_evolution()

        logger.info("EvolutionScheduler stopped.")

    def run_now(self) -> dict[str, Any]:
        """立即执行一次演化（用于手动触发）。"""
        return self._run_evolution()

    def _run_evolution(self) -> dict[str, Any]:
        """执行一轮策略演化。"""
        logger.info("=" * 50)
        logger.info("EVOLUTION RUN STARTED")
        logger.info("=" * 50)

        self._last_run = datetime.now()

        # 从记忆库抽取测试任务
        test_tasks = self._extract_test_tasks()
        if not test_tasks:
            logger.warning("No test tasks available, using defaults")
            test_tasks = self._sandbox._default_test_tasks()

        logger.info("Test tasks: %d", len(test_tasks))

        # 运行演化
        result = self._sandbox.evolve(
            agent_type="search_agent",
            base_strategy=None,  # 使用默认基准
            test_tasks=test_tasks[:TEST_TASK_COUNT],
        )

        # 发布结果
        self._messaging.publish("orchestrator:evolution_result", result)

        if result.get("winner"):
            logger.info(
                "Evolution winner: %s (stable=%s, deployed=%s)",
                result["winner"]["strategy_id"],
                result["stable"],
                result["deployed"],
            )
        else:
            logger.warning("Evolution produced no winner")

        logger.info("EVOLUTION RUN COMPLETED")
        return result

    def _extract_test_tasks(self) -> list[dict[str, Any]]:
        """从记忆库中抽取历史成功任务作为测试集。

        Returns:
            去敏化的测试任务列表。
        """
        tasks: list[dict[str, Any]] = []

        try:
            # 从 successful_strategies 集合中提取任务描述
            # ChromaDB 查询获取最近的策略记录
            results = self._memory._strategies.get(
                include=["documents", "metadatas"],
                limit=20,
            )

            for doc, meta in zip(
                results.get("documents", []),
                results.get("metadatas", []),
            ):
                # 提取目标关键词作为测试任务
                keywords = meta.get("goal_keywords", "")
                if keywords:
                    tasks.append({
                        "instruction": f"搜索{keywords}",
                        "expected_capability": "web_search",
                    })
        except Exception as exc:
            logger.warning("Failed to extract test tasks from memory: %s", exc)

        return tasks

    def _next_scheduled_time(self, now: datetime) -> datetime:
        """计算下一次调度时间。"""
        target = now.replace(hour=SCHEDULE_HOUR, minute=SCHEDULE_MINUTE, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    def shutdown(self, signum=None, frame=None) -> None:
        self._running = False
        logger.info("EvolutionScheduler shutting down...")

    def _setup_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.shutdown)
            except Exception:
                pass


def main() -> None:
    from logging_setup import setup_logging
    setup_logging("scheduler")

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    db_path = os.environ.get("REGISTRY_DB", "agents.db")
    memory_dir = os.environ.get("MEMORY_DIR", "./chroma_memory")
    run_now = "--now" in sys.argv

    messaging = MessagingClient(redis_host, redis_port)
    registry = AgentRegistry(db_path)
    memory = MemoryManager(memory_dir)
    sandbox = EvolutionSandbox(messaging, registry)

    scheduler = EvolutionScheduler(messaging, registry, memory, sandbox)

    try:
        if run_now:
            logger.info("Running evolution immediately (--now flag)")
            result = scheduler.run_now()
            logger.info("Result: %s", json.dumps(result.get("summary", ""), ensure_ascii=False))
        else:
            scheduler.run()
    except KeyboardInterrupt:
        scheduler.shutdown()
    except Exception as exc:
        logger.critical("Fatal: %s", exc, exc_info=True)
        scheduler.shutdown()
        sys.exit(1)
    finally:
        try:
            messaging.close()
        except Exception:
            pass
        try:
            registry.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
