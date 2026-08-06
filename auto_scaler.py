"""
织光 (ZhiGuang) — 弹性扩缩容控制器 (AutoScaler)

呼吸灯式自动伸缩：
    1. 每10s扫描任务队列长度 (LLEN)
    2. 队列堆积 → 扩容 (Docker 启动新 Worker)
    3. 空闲持续 → 缩容 (优雅退出旧 Worker)
    4. 预热池：提前启动待注册容器，冷启动 < 1秒
    5. 安全边界：每种能力 [1, 20] 实例
    6. 所有操作记录 + 告警频道

运行模式：
    - 真实模式 (Docker 可用): docker.from_env() 管理容器
    - 模拟模式 (无 Docker): 日志模拟，用于开发测试
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import redis.exceptions

from common import MessagingClient, AgentRegistry

logger = logging.getLogger(__name__)


# ============================================================================
# 配置
# ============================================================================

# 扫描间隔（秒）
SCAN_INTERVAL = 10

# 触发扩容的队列长度阈值
SCALE_UP_THRESHOLD = 5

# 触发缩容的空闲持续时间（秒）
# 测试模式 30 秒，生产模式 300 秒（5 分钟）
SCALE_DOWN_IDLE_SECONDS = int(os.environ.get("SCALE_DOWN_IDLE_SECONDS", "300"))

# 缩容检查间隔比（相对于 SCAN_INTERVAL）
SCALE_DOWN_CHECKS = max(1, SCALE_DOWN_IDLE_SECONDS // SCAN_INTERVAL)

# 每种能力的最小/最大实例数
MIN_INSTANCES = 1
MAX_INSTANCES = 20

# 预热池大小（每种能力）
WARM_POOL_SIZE = 1

# Worker 镜像
WORKER_IMAGE = os.environ.get("WORKER_IMAGE", "zhiguang-worker:latest")

# Worker 网络
WORKER_NETWORK = os.environ.get("WORKER_NETWORK", "bridge")

# 支持的能力类型（默认监控所有）
WATCHED_CAPABILITIES = ["web_search", "content_summary", "code_execution"]


# ============================================================================
# 预热池条目
# ============================================================================


@dataclass
class WarmInstance:
    """预热池中的 Worker 实例（已启动但未注册）。"""

    container_id: str
    agent_type: str
    capabilities: list[str]
    created_at: float = field(default_factory=time.time)


# ============================================================================
# 自动扩缩容控制器
# ============================================================================


class AutoScaler:
    """弹性扩缩容控制器。

    Usage:
        scaler = AutoScaler(messaging, registry)
        scaler.run()
    """

    def __init__(
        self,
        messaging: MessagingClient,
        registry: AgentRegistry,
        simulate: bool = False,
    ) -> None:
        """初始化扩缩容控制器。

        Args:
            messaging: 消息客户端。
            registry: 能力注册表。
            simulate: True 为模拟模式（不操作真实 Docker）。
        """
        self._messaging = messaging
        self._registry = registry
        self._simulate = simulate

        # Docker 客户端
        self._docker = None
        if not simulate:
            try:
                import docker
                self._docker = docker.from_env()
                logger.info("Docker client connected: %s", self._docker.version().get("Version", "?"))
            except Exception as exc:
                logger.warning("Docker unavailable, falling back to simulate mode: %s", exc)
                self._simulate = True

        # 预热池
        self._warm_pool: dict[str, list[WarmInstance]] = defaultdict(list)

        # 空闲追踪：{agent_id: 连续空闲轮数}
        self._idle_counter: dict[str, int] = defaultdict(int)

        # 运行状态
        self._running = False
        self._shutting_down = False

        logger.info(
            "AutoScaler ready (mode=%s, interval=%ds). "
            "Thresholds: scale_up=%d, idle=%ds, min=%d, max=%d",
            "simulate" if self._simulate else "docker",
            SCAN_INTERVAL,
            SCALE_UP_THRESHOLD,
            SCALE_DOWN_IDLE_SECONDS,
            MIN_INSTANCES,
            MAX_INSTANCES,
        )

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> None:
        """启动扩缩容主循环。"""
        self._setup_signal_handlers()
        self._running = True

        logger.info("AutoScaler loop started (interval=%ds)", SCAN_INTERVAL)

        # 初始化：填充预热池
        for cap in WATCHED_CAPABILITIES:
            self._ensure_warm_pool(cap)

        while self._running:
            try:
                self._tick()
            except Exception as exc:
                logger.error("AutoScaler tick error: %s", exc, exc_info=True)
                self._alert("scaler_error", f"自动扩缩容异常: {exc}")

            # 分段 sleep，响应退出信号
            for _ in range(SCAN_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

        # 退出时清理预热池
        self._drain_warm_pool()
        logger.info("AutoScaler stopped.")

    def shutdown(self, signum=None, frame=None) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self._running = False
        logger.info("AutoScaler shutting down...")

    # ------------------------------------------------------------------
    # 每轮扫描
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        """执行一轮监控与扩缩容决策。"""
        tick_start = time.time()

        # 1. 扫描所有队列
        queue_lengths: dict[str, int] = {}
        for cap in WATCHED_CAPABILITIES:
            queue_lengths[cap] = self._get_queue_length(cap)

        # 2. 获取当前各能力的实例数
        instance_counts: dict[str, int] = defaultdict(int)
        idle_counts: dict[str, int] = defaultdict(int)
        active_agents = self._registry.list_agents()

        for agent in active_agents:
            # 从 capabilities 中解析主能力
            caps = agent["capabilities"]
            status = agent["status"]
            for cap in caps:
                if cap in WATCHED_CAPABILITIES:
                    instance_counts[cap] += 1
                    if "idle" in status:
                        idle_counts[cap] += 1

        logger.debug(
            "Tick: queues=%s, instances=%s, idle=%s",
            dict(queue_lengths),
            dict(instance_counts),
            dict(idle_counts),
        )

        # 3. 决策
        for cap in WATCHED_CAPABILITIES:
            qlen = queue_lengths.get(cap, 0)
            count = instance_counts.get(cap, 0)
            idle = idle_counts.get(cap, 0)

            # 扩容决策
            if qlen > SCALE_UP_THRESHOLD and idle == 0:
                if count < MAX_INSTANCES:
                    self._scale_up(cap, qlen, count)
                else:
                    logger.warning(
                        "[%s] Queue=%d but already at max instances (%d), cannot scale up",
                        cap, qlen, MAX_INSTANCES
                    )
                    self._alert("scale_blocked", f"{cap} 已达上限 {MAX_INSTANCES}，队列 {qlen} 堆积中")

            # 缩容决策
            if qlen == 0 and count > MIN_INSTANCES:
                self._idle_counter[cap] += 1
                if self._idle_counter[cap] >= SCALE_DOWN_CHECKS:
                    self._scale_down(cap)
                    self._idle_counter[cap] = 0
            else:
                self._idle_counter[cap] = 0

            # 维护预热池
            self._ensure_warm_pool(cap)

        elapsed = time.time() - tick_start
        if elapsed > SCAN_INTERVAL * 0.5:
            logger.warning("Tick took %.1fs (may miss next interval)", elapsed)

    # ------------------------------------------------------------------
    # 扩容
    # ------------------------------------------------------------------

    def _scale_up(self, capability: str, queue_len: int, current_count: int) -> None:
        """扩容：从预热池取出实例注册，或启动新容器。

        Args:
            capability: 需要扩容的能力类型。
            queue_len: 当前队列长度。
            current_count: 当前实例数。
        """
        logger.info(
            "[%s] SCALING UP: queue=%d, instances=%d -> %d",
            capability, queue_len, current_count, current_count + 1,
        )

        # 优先从预热池取
        warm = self._pop_warm(capability)
        if warm:
            logger.info("[%s] Using warm instance %s (cold start avoided)", capability, warm.container_id[:12])
            self._alert("scale_up", f"{capability}: 从预热池启动 (queue={queue_len})")
            # 模拟模式：注册一个新 Agent
            self._register_simulated_agent(capability, warm.container_id)
            return

        # 启动新容器
        instance_id = self._start_worker_container(capability)
        if instance_id:
            self._alert("scale_up", f"{capability}: 新容器 {instance_id[:12]} (queue={queue_len})")
            # 模拟模式：注册一个新 Agent
            self._register_simulated_agent(capability, instance_id)
        else:
            logger.error("[%s] Failed to start new container", capability)
            self._alert("scale_failed", f"{capability}: 启动容器失败 (queue={queue_len})")

    def _register_simulated_agent(self, capability: str, instance_id: str) -> None:
        """模拟模式下注册一个扩容出来的 Worker。"""
        if not self._simulate:
            return
        import random
        agent_id = f"auto-{capability}-{random.randint(1000, 9999)}"
        try:
            self._registry.register(agent_id, [capability], status="idle:0/10")
            logger.info("[SIMULATE] Registered %s for %s", agent_id, capability)
        except Exception as exc:
            logger.warning("[SIMULATE] Failed to register agent: %s", exc)

    def _start_worker_container(self, capability: str) -> str | None:
        """启动一个新的 Worker Docker 容器。

        Args:
            capability: 能力类型。

        Returns:
            容器 ID，失败返回 None。
        """
        if self._simulate:
            container_id = f"sim-{capability}-{int(time.time())}"
            logger.info("[SIMULATE] Started container %s for %s", container_id[:16], capability)
            return container_id

        try:
            container = self._docker.containers.run(
                image=WORKER_IMAGE,
                detach=True,
                network=WORKER_NETWORK,
                environment={
                    "AGENT_TYPE": capability,
                    "REDIS_HOST": os.environ.get("REDIS_HOST", "host.docker.internal"),
                    "REDIS_PORT": os.environ.get("REDIS_PORT", "6379"),
                    "LLM_API_KEY": os.environ.get("LLM_API_KEY", ""),
                    "LLM_BASE_URL": os.environ.get("LLM_BASE_URL", ""),
                    "LLM_MODEL": os.environ.get("LLM_MODEL", ""),
                },
                labels={
                    "zhiguang.role": "worker",
                    "zhiguang.capability": capability,
                    "zhiguang.auto": "true",
                },
                # 资源限制
                mem_limit="256m",
                nano_cpus=1_000_000_000,  # 1 CPU
                remove=True,  # 退出后自动删除
            )
            logger.info("[DOCKER] Started %s for %s", container.id[:12], capability)
            return container.id
        except Exception as exc:
            logger.error("[DOCKER] Failed to start container: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 缩容
    # ------------------------------------------------------------------

    def _scale_down(self, capability: str) -> None:
        """缩容：选择一个空闲 Worker 发送退出指令。

        Args:
            capability: 能力类型。
        """
        # 找该能力的一个空闲实例
        agents = self._registry.list_agents()
        targets = [
            a for a in agents
            if capability in a["capabilities"] and "idle" in a["status"]
        ]

        if not targets:
            logger.info("[%s] No idle worker to scale down", capability)
            return

        target = targets[0]  # 选第一个空闲的
        logger.info(
            "[%s] SCALING DOWN: removing %s (idle)",
            capability, target["agent_id"]
        )

        # 发送退出指令（通过 Redis 发布）
        try:
            self._messaging.publish(f"worker:control:{target['agent_id']}", {
                "command": "shutdown",
                "reason": "auto_scale_down",
                "timestamp": _now_iso(),
            })
            self._alert("scale_down", f"{capability}: 关闭 {target['agent_id']}")
        except Exception as exc:
            logger.error("[%s] Scale-down publish failed: %s", capability, exc)

        # 在模拟模式下直接标记 offline
        if self._simulate:
            self._registry.register(target["agent_id"], target["capabilities"], "offline")

    # ------------------------------------------------------------------
    # 预热池
    # ------------------------------------------------------------------

    def _ensure_warm_pool(self, capability: str) -> None:
        """确保预热池中有足够的待命容器。

        Args:
            capability: 能力类型。
        """
        current = len(self._warm_pool.get(capability, []))
        needed = WARM_POOL_SIZE - current

        for _ in range(needed):
            if self._simulate:
                warm = WarmInstance(
                    container_id=f"warm-{capability}-{int(time.time())}",
                    agent_type=capability,
                    capabilities=[capability],
                )
                self._warm_pool[capability].append(warm)
            else:
                try:
                    container = self._docker.containers.run(
                        image=WORKER_IMAGE,
                        detach=True,
                        network=WORKER_NETWORK,
                        environment={
                            "AGENT_TYPE": capability,
                            "WARM_MODE": "true",
                            "REDIS_HOST": os.environ.get("REDIS_HOST", "host.docker.internal"),
                            "REDIS_PORT": os.environ.get("REDIS_PORT", "6379"),
                        },
                        labels={
                            "zhiguang.role": "worker",
                            "zhiguang.capability": capability,
                            "zhiguang.warm": "true",
                        },
                        mem_limit="128m",
                        nano_cpus=500_000_000,
                        remove=True,
                    )
                    warm = WarmInstance(
                        container_id=container.id,
                        agent_type=capability,
                        capabilities=[capability],
                    )
                    self._warm_pool[capability].append(warm)
                    logger.debug("[%s] Warm instance %s ready", capability, container.id[:12])
                except Exception as exc:
                    logger.warning("[%s] Warm pool fill failed: %s", capability, exc)

    def _pop_warm(self, capability: str) -> WarmInstance | None:
        """从预热池弹出一个实例。

        Args:
            capability: 能力类型。

        Returns:
            预热实例，池为空返回 None。
        """
        pool = self._warm_pool.get(capability, [])
        if pool:
            instance = pool.pop(0)
            logger.info("[%s] Warm instance %s activated", capability, instance.container_id[:12])
            return instance
        return None

    def _drain_warm_pool(self) -> None:
        """退出时清空预热池。"""
        for cap, pool in self._warm_pool.items():
            for instance in pool:
                if not self._simulate:
                    try:
                        c = self._docker.containers.get(instance.container_id)
                        c.stop(timeout=5)
                    except Exception:
                        pass
                logger.info("[%s] Drained warm instance %s", cap, instance.container_id[:12])
            pool.clear()
        logger.info("Warm pool drained.")

    # ------------------------------------------------------------------
    # 队列监控
    # ------------------------------------------------------------------

    def _get_queue_length(self, capability: str) -> int:
        """获取指定能力的任务队列当前长度。

        扫描所有以此能力注册的 Worker 的队列。

        Args:
            capability: 能力名称。

        Returns:
            队列总长度。
        """
        total = 0
        agents = self._registry.list_agents()
        for agent in agents:
            if capability in agent["capabilities"]:
                queue_key = f"task_queue:{agent['agent_id']}"
                try:
                    # 用 messing 的底层 Redis 连接执行 LLEN
                    length = self._messaging._redis.llen(queue_key)
                    total += length
                except redis.exceptions.RedisError as exc:
                    logger.debug("LLEN %s failed: %s", queue_key, exc)
                except Exception:
                    pass
        return total

    # ------------------------------------------------------------------
    # 告警
    # ------------------------------------------------------------------

    def _alert(self, event_type: str, message: str) -> None:
        """发送告警到 orchestrator:alert 频道。

        Args:
            event_type: 事件类型 (scale_up/scale_down/scale_failed/...)。
            message: 告警信息。
        """
        alert = {
            "type": event_type,
            "message": message,
            "timestamp": _now_iso(),
            "service": "auto_scaler",
        }
        try:
            self._messaging.publish("orchestrator:alert", alert)
        except Exception as exc:
            logger.warning("Alert publish failed: %s", exc)

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
# 辅助函数
# ============================================================================


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# 启动入口
# ============================================================================


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    db_path = os.environ.get("REGISTRY_DB", "agents.db")
    simulate = os.environ.get("AUTOSCALER_SIMULATE", "1").lower() in ("1", "true", "yes")

    logger.info(
        "Starting AutoScaler (Redis=%s:%d, simulate=%s)",
        redis_host, redis_port, simulate,
    )

    messaging = MessagingClient(redis_host, redis_port)
    registry = AgentRegistry(db_path)
    scaler = AutoScaler(messaging, registry, simulate=simulate)

    try:
        scaler.run()
    except KeyboardInterrupt:
        scaler.shutdown()
    except Exception as exc:
        logger.critical("Fatal: %s", exc, exc_info=True)
        scaler.shutdown()
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
