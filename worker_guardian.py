"""
织光 (ZhiGuang) — Worker 守护进程 (WorkerGuardian)

自愈与守护：
    1. 每 15s 扫描所有 Worker 心跳
    2. 超过 45s 无心跳 → 判定死亡 → 自动复活
    3. 5 分钟内重启超过 3 次 → 隔离 (quarantined)
    4. 所有事件发布到告警频道
    5. Guardian 自身发布心跳，支持外部监控

安全原则：
    - 隔离的 Worker 永不自动复活，必须人工介入
    - 降级策略防止无限重启循环
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


from common import MessagingClient, AgentRegistry

logger = logging.getLogger(__name__)

# ============================================================================
# 配置
# ============================================================================

# 扫描间隔（秒）
CHECK_INTERVAL = 15

# 心跳超时（秒）—— 超过此时间无心跳判定死亡
HEARTBEAT_TIMEOUT = 20

# 隔离阈值：在此时间内重启次数
QUARANTINE_WINDOW = 300  # 5 分钟
QUARANTINE_MAX_RESTARTS = 3

# Guardian 自身心跳间隔
GUARDIAN_HEARTBEAT_INTERVAL = 30

# Worker 镜像
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "zhiguang-worker:latest")


# ============================================================================
# Worker 状态记录
# ============================================================================


@dataclass
class WorkerRecord:
    """单个 Worker 的健康追踪记录。"""

    agent_id: str
    capabilities: list[str]
    last_heartbeat: datetime
    restarts: list[float] = field(default_factory=list)  # UTC timestamp 列表
    status: str = "healthy"  # healthy / dead / quarantined


# ============================================================================
# Worker 守护进程
# ============================================================================


class WorkerGuardian:
    """Worker 健康守护：监控 → 复活 → 隔离。

    Usage:
        guardian = WorkerGuardian(messaging, registry)
        guardian.run()
    """

    def __init__(
        self,
        messaging: MessagingClient,
        registry: AgentRegistry,
        simulate: bool = False,
    ) -> None:
        self._messaging = messaging
        self._registry = registry
        self._simulate = simulate

        # 进程重启模式：agent_id -> 启动脚本（仅 launcher 管理的 Worker）
        _base = Path(__file__).resolve().parent
        self._managed_scripts: dict[str, Path] = {
            "search_agent": _base / "worker_base.py",
            "critic": _base / "critic_agent.py",
            "webfetchworker": _base / "workers" / "web_fetch_worker.py",
            "content_summarizer": _base / "workers" / "content_summary_worker.py",
            "codeexecworker": _base / "workers" / "code_execution_worker.py",
            "fileioworker": _base / "workers" / "file_io_worker.py",
            "packaging_worker": _base / "workers" / "packaging_worker.py",
            "dataloaderworker": _base / "workers" / "data_loader_worker.py",
            "dataanalyzerworker": _base / "workers" / "data_analyzer_worker.py",
            "modeltrainerworker": _base / "workers" / "model_trainer_worker.py",
            "reportgeneratorworker": _base / "workers" / "report_generator_worker.py",
        }
        self._mode = os.environ.get("GUARDIAN_MODE", "process").lower()

        # Worker 追踪
        self._workers: dict[str, WorkerRecord] = {}

        # 全局重启计数器（用于跨复活追踪）
        # Docker
        self._docker = None
        if not simulate:
            try:
                import docker
                self._docker = docker.from_env()
            except Exception:
                self._simulate = True

        self._running = False
        self._shutting_down = False

        logger.info(
            "WorkerGuardian ready (mode=%s). "
            "Heartbeat timeout=%ds, quarantine=%d restarts / %ds",
            "simulate" if self._simulate else ("process" if self._mode == "process" else "docker"),
            HEARTBEAT_TIMEOUT,
            QUARANTINE_MAX_RESTARTS,
            QUARANTINE_WINDOW,
        )

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> None:
        """启动守护循环。"""
        self._setup_signal_handlers()
        self._running = True

        # 注册 Guardian 自己的心跳
        self._publish_guardian_heartbeat()

        logger.info("WorkerGuardian started (check every %ds)", CHECK_INTERVAL)

        last_guardian_hb = time.time()

        while self._running:
            try:
                self._health_check()
            except Exception as exc:
                logger.error("Health check error: %s", exc, exc_info=True)

            # Guardian 自身心跳
            if time.time() - last_guardian_hb >= GUARDIAN_HEARTBEAT_INTERVAL:
                self._publish_guardian_heartbeat()
                last_guardian_hb = time.time()

            # 分段 sleep
            for _ in range(CHECK_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

        logger.info("WorkerGuardian stopped.")

    def shutdown(self, signum=None, frame=None) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._running = False
        logger.info("WorkerGuardian shutting down...")

    # ------------------------------------------------------------------
    # 健康检查
    # ------------------------------------------------------------------

    def _health_check(self) -> None:
        """扫描所有 Worker，检测死亡并复活。"""
        now = datetime.now(timezone.utc)

        # 从注册表获取所有活跃 Worker（排除已标记 offline/terminated/quarantined 的）
        agents = self._registry.list_agents()
        active_agents = [
            a for a in agents
            if a["status"] not in ("offline", "terminated", "quarantined")
        ]

        # 更新已知 Worker 的心跳
        seen_ids: set[str] = set()
        for agent in active_agents:
            aid = agent["agent_id"]
            seen_ids.add(aid)

            hb_str = agent.get("last_heartbeat", "")
            try:
                hb = datetime.fromisoformat(hb_str)
                # SQLite 存的是本地时间，加 UTC 标记
                if hb.tzinfo is None:
                    hb = hb.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                hb = now

            if aid not in self._workers:
                self._workers[aid] = WorkerRecord(
                    agent_id=aid,
                    capabilities=agent["capabilities"],
                    last_heartbeat=hb,
                )
                self._workers[aid].restarts = [
                    t for t in self._workers[aid].restarts
                    if now.timestamp() - t < QUARANTINE_WINDOW
                ]
            else:
                # 更新心跳时间
                if hb > self._workers[aid].last_heartbeat:
                    self._workers[aid].last_heartbeat = hb
                # 清理过期重启记录
                self._workers[aid].restarts = [
                    t for t in self._workers[aid].restarts
                    if now.timestamp() - t < QUARANTINE_WINDOW
                ]

        # 检查所有已知 Worker（包括可能已不在注册表中的）
        for aid, record in list(self._workers.items()):
            # 跳过已隔离的
            if record.status == "quarantined":
                continue

            # 检查心跳超时
            age = (now - record.last_heartbeat).total_seconds()
            if age > HEARTBEAT_TIMEOUT:
                # 确认在注册表中确实不可见（双重确认）
                still_alive = any(
                    a["agent_id"] == aid and "idle" in a.get("status", "")
                    for a in agents
                )

                if not still_alive or age > HEARTBEAT_TIMEOUT * 2:
                    # 判定死亡
                    if record.status != "dead":
                        logger.warning(
                            "Worker '%s' appears DEAD (last heartbeat %.0fs ago)",
                            aid, age
                        )
                        record.status = "dead"

                    # 尝试复活
                    self._try_revive(record)

        logger.debug(
            "Health check: %d workers (active=%d, dead=%d, quarantined=%d)",
            len(self._workers),
            sum(1 for r in self._workers.values() if r.status == "healthy"),
            sum(1 for r in self._workers.values() if r.status == "dead"),
            sum(1 for r in self._workers.values() if r.status == "quarantined"),
        )

    # ------------------------------------------------------------------
    # 复活逻辑
    # ------------------------------------------------------------------

    def _try_revive(self, record: WorkerRecord) -> None:
        """尝试复活一个死亡 Worker。

        检查隔离条件 → 重启 → 追踪重启次数。

        Args:
            record: Worker 的健康记录。
        """
        now = time.time()

        # 只复活 launcher 管理的 Worker，避免误拉已停服的服务
        if record.agent_id not in self._managed_scripts:
            logger.info(
                "Worker '%s' not managed by launcher, skipping auto-revive",
                record.agent_id,
            )
            return

        # 清理过期重启记录
        record.restarts = [t for t in record.restarts if now - t < QUARANTINE_WINDOW]

        # 检查隔离条件
        if len(record.restarts) >= QUARANTINE_MAX_RESTARTS:
            logger.error(
                "Worker '%s' restarted %d times in %ds → QUARANTINED",
                record.agent_id,
                len(record.restarts),
                QUARANTINE_WINDOW,
            )
            record.status = "quarantined"
            self._quarantine_worker(record)
            return

        # 执行复活
        logger.info("Reviving worker '%s' (restart #%d)", record.agent_id, len(record.restarts) + 1)
        success = self._restart_worker(record)

        if success:
            record.restarts.append(now)
            record.status = "healthy"  # 乐观假设复活成功
            self._alert(
                "worker_revived",
                f"Worker '{record.agent_id}' 已复活 (第{len(record.restarts)}次重启)",
            )
        else:
            logger.error("Failed to revive worker '%s'", record.agent_id)
            self._alert(
                "revive_failed",
                f"Worker '{record.agent_id}' 复活失败",
            )

    def _restart_worker(self, record: WorkerRecord) -> bool:
        """重启 Worker：标记旧记录 → 启动新容器。

        Args:
            record: Worker 健康记录。

        Returns:
            True 表示重启成功。
        """
        # 标记旧记录
        try:
            self._registry.register(
                record.agent_id, record.capabilities, status="terminated"
            )
        except Exception as exc:
            logger.warning("Failed to mark '%s' terminated: %s", record.agent_id, exc)

        # 启动新实例
        if self._simulate or self._mode == "simulate":
            logger.info("[SIMULATE] Restarted worker '%s'", record.agent_id)
            # 模拟：更新注册表为新实例
            self._registry.register(
                record.agent_id, record.capabilities, status="idle:0/10"
            )
            return True

        if self._mode == "process":
            script = self._managed_scripts.get(record.agent_id)
            if script and script.exists():
                try:
                    creationflags = 0x08000000 if os.name == "nt" else 0
                    proc = subprocess.Popen(
                        [sys.executable, str(script)],
                        cwd=str(Path(__file__).resolve().parent),
                        env=os.environ.copy(),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=creationflags,
                    )
                    # 把复活的新进程 PID 记录进 launcher 的 pid 文件，保证 stop 能清理
                    try:
                        import json as _json
                        pid_file = Path(__file__).resolve().parent / ".weavemind" / "pids.json"
                        if pid_file.exists():
                            data = _json.loads(pid_file.read_text(encoding="utf-8"))
                            data.setdefault("services", {})[f"{record.agent_id}@restart"] = proc.pid
                            pid_file.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                    logger.info("[PROCESS] Revived worker '%s' -> %s", record.agent_id, script)
                    return True
                except Exception as exc:
                    logger.error("[PROCESS] Failed to restart '%s': %s", record.agent_id, exc)
                    return False
            logger.error("[PROCESS] No managed script for '%s'", record.agent_id)
            return False

        # Docker 模式
        try:
            container = self._docker.containers.run(
                image=WORKER_IMAGE,
                detach=True,
                network=os.environ.get("WORKER_NETWORK", "bridge"),
                environment={
                    "AGENT_TYPE": record.capabilities[0] if record.capabilities else "worker",
                    "REDIS_HOST": os.environ.get("REDIS_HOST", "host.docker.internal"),
                    "REDIS_PORT": os.environ.get("REDIS_PORT", "6379"),
                    "LLM_API_KEY": os.environ.get("LLM_API_KEY", ""),
                    "LLM_BASE_URL": os.environ.get("LLM_BASE_URL", ""),
                    "LLM_MODEL": os.environ.get("LLM_MODEL", ""),
                },
                labels={
                    "zhiguang.role": "worker",
                    "zhiguang.revived": "true",
                    "zhiguang.predecessor": record.agent_id,
                },
                mem_limit="256m",
                nano_cpus=1_000_000_000,
                remove=True,
            )
            logger.info("[DOCKER] Revived worker '%s' → %s", record.agent_id, container.id[:12])
            return True
        except Exception as exc:
            logger.error("[DOCKER] Failed to restart: %s", exc)
            return False

    def _quarantine_worker(self, record: WorkerRecord) -> None:
        """将 Worker 标记为隔离，永不自动复活。

        Args:
            record: Worker 记录。
        """
        try:
            self._registry.register(
                record.agent_id, record.capabilities, status="quarantined"
            )
        except Exception as exc:
            logger.warning("Failed to quarantine '%s': %s", record.agent_id, exc)

        self._alert(
            "worker_quarantined",
            f"CRITICAL: Worker '{record.agent_id}' 在 {QUARANTINE_WINDOW}s 内重启 "
            f"{QUARANTINE_MAX_RESTARTS} 次，已隔离。需要人工介入！",
        )

    # ------------------------------------------------------------------
    # 告警与心跳
    # ------------------------------------------------------------------

    def _alert(self, event_type: str, message: str) -> None:
        """发送告警到 orchestrator:alert 频道。"""
        alert = {
            "type": event_type,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "service": "worker_guardian",
        }
        try:
            self._messaging.publish("orchestrator:alert", alert)
        except Exception as exc:
            logger.warning("Alert publish failed: %s", exc)

    def _publish_guardian_heartbeat(self) -> None:
        """发布 Guardian 自身心跳。"""
        try:
            self._messaging.publish("guardian.heartbeat", {
                "service": "worker_guardian",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "workers_tracked": len(self._workers),
                "workers_healthy": sum(1 for r in self._workers.values() if r.status == "healthy"),
                "workers_dead": sum(1 for r in self._workers.values() if r.status == "dead"),
                "workers_quarantined": sum(1 for r in self._workers.values() if r.status == "quarantined"),
            })
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _setup_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.shutdown)
            except Exception:
                pass


# ============================================================================
# 启动入口
# ============================================================================


def main() -> None:
    from logging_setup import setup_logging
    setup_logging("guardian")

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    db_path = os.environ.get("REGISTRY_DB", "agents.db")
    simulate = os.environ.get("GUARDIAN_SIMULATE", "0").lower() in ("1", "true", "yes")

    logger.info("Starting WorkerGuardian (Redis=%s:%d, simulate=%s)", redis_host, redis_port, simulate)

    messaging = MessagingClient(redis_host, redis_port)
    registry = AgentRegistry(db_path)
    guardian = WorkerGuardian(messaging, registry, simulate=simulate)

    try:
        guardian.run()
    except KeyboardInterrupt:
        guardian.shutdown()
    except Exception as exc:
        logger.critical("Fatal: %s", exc, exc_info=True)
        guardian.shutdown()
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
