"""织光 (ZhiGuang) - 分布式智能体系统公共基础库

提供消息通信、能力注册和核心数据结构。所有模块依赖本库，不包含任何 AI 调用逻辑。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

import redis
import redis.exceptions

logger = logging.getLogger(__name__)


# ============================================================================
# 数据结构定义
# ============================================================================


class TaskStatus(str, Enum):
    """任务状态枚举。"""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


@dataclass
class Task:
    """单个任务的数据结构。"""

    task_id: str
    parent_id: str | None
    capability: str = "web_search"
    instruction: str = ""
    result: Any = None
    status: TaskStatus = TaskStatus.PENDING


@dataclass
class Plan:
    """规划结果：目标与步骤列表。"""

    goal: str
    steps: list[Task] = field(default_factory=list)


# ============================================================================
# 消息客户端
# ============================================================================


class MessagingClient:
    """基于 Redis 的消息通信客户端（发布/订阅 + 任务队列）。

    所有方法均包含连接异常的重试与断线重连逻辑。
    """

    _MAX_RETRIES: int = 3
    _RETRY_BASE_DELAY: float = 0.5

    def __init__(
        self,
        redis_host: str,
        redis_port: int,
        _redis_client: redis.Redis | None = None,
    ) -> None:
        self._redis_host = redis_host
        self._redis_port = redis_port
        self._redis: redis.Redis = _redis_client or self._connect()

    @property
    def redis(self) -> redis.Redis:
        """暴露底层 Redis 客户端（供策略部署/读取等直接操作）。"""
        return self._redis
        if not _redis_client:
            logger.info("MessagingClient connected to %s:%d", redis_host, redis_port)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _connect(self) -> redis.Redis:
        """建立 Redis 连接，失败时抛出异常而非返回 None。"""
        last_exception: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                r = redis.Redis(
                    host=self._redis_host,
                    port=self._redis_port,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_keepalive=True,
                    health_check_interval=30,
                )
                r.ping()
                return r
            except (redis.exceptions.ConnectionError, ConnectionError, OSError) as exc:
                last_exception = exc
                logger.warning(
                    "Redis connection attempt %d/%d failed: %s",
                    attempt,
                    self._MAX_RETRIES,
                    exc,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._RETRY_BASE_DELAY * attempt)
        raise redis.exceptions.ConnectionError(
            f"Failed to connect to Redis at {self._redis_host}:{self._redis_port} "
            f"after {self._MAX_RETRIES} attempts"
        ) from last_exception

    def _serialize(self, message: dict[str, Any]) -> str:
        try:
            return json.dumps(message, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            logger.error("Failed to serialize message: %s", exc)
            raise

    def _deserialize(self, raw: str) -> dict[str, Any]:
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError(f"Expected JSON object, got {type(result).__name__}")
            return result
        except json.JSONDecodeError as exc:
            logger.error("Failed to deserialize message: %s", exc)
            raise

    # ------------------------------------------------------------------
    # 发布 / 订阅
    # ------------------------------------------------------------------

    def publish(self, channel: str, message: dict[str, Any]) -> bool:
        """序列化消息并发布到指定频道，带重试。"""
        payload = self._serialize(message)
        last_exception: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                self._redis.publish(channel, payload)
                return True
            except (redis.exceptions.ConnectionError, ConnectionError, OSError) as exc:
                last_exception = exc
                logger.warning(
                    "publish to '%s' attempt %d/%d failed: %s",
                    channel,
                    attempt,
                    self._MAX_RETRIES,
                    exc,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._RETRY_BASE_DELAY * attempt)
                    self._reconnect_if_needed()
        raise redis.exceptions.ConnectionError(
            f"Failed to publish to channel '{channel}' after {self._MAX_RETRIES} attempts"
        ) from last_exception

    def subscribe(self, channel: str) -> Iterator[dict[str, Any]]:
        """订阅指定频道，断线自动重连。"""
        reconnect_delay = 1.0
        while True:
            pubsub = None
            try:
                pubsub = self._redis.pubsub()
                pubsub.subscribe(channel)
                logger.info("Subscribed to channel '%s'", channel)
                for raw_message in pubsub.listen():
                    if raw_message["type"] == "message":
                        try:
                            yield self._deserialize(raw_message["data"])
                        except (json.JSONDecodeError, ValueError) as exc:
                            logger.error(
                                "Skipping malformed message on '%s': %s", channel, exc
                            )
                            continue
            except (redis.exceptions.ConnectionError, ConnectionError, OSError) as exc:
                logger.warning(
                    "Subscription to '%s' lost (%s), reconnecting in %.1fs...",
                    channel,
                    exc,
                    reconnect_delay,
                )
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, 30.0)
                self._reconnect_if_needed()
            except GeneratorExit:
                logger.info("Subscription to '%s' closed by caller.", channel)
                return
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # 任务队列（List 结构）
    # ------------------------------------------------------------------

    def push_task(self, agent_id: str, task: dict[str, Any]) -> bool:
        """向指定智能体的标准任务队列（task_queue:{agent_id}）压入任务。"""
        return self.push_task_to_queue(f"task_queue:{agent_id}", task)

    def push_task_to_queue(self, queue_key: str, task: dict[str, Any]) -> bool:
        """向任意 Redis 列表队列压入任务（完整 key，如 task_queue:{id} / task:high:{id}）。"""
        payload = self._serialize(task)
        last_exception: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                self._redis.lpush(queue_key, payload)
                return True
            except (redis.exceptions.ConnectionError, ConnectionError, OSError) as exc:
                last_exception = exc
                logger.warning(
                    "push_task_to_queue '%s' attempt %d/%d failed: %s",
                    queue_key,
                    attempt,
                    self._MAX_RETRIES,
                    exc,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._RETRY_BASE_DELAY * attempt)
                    self._reconnect_if_needed()
        raise redis.exceptions.ConnectionError(
            f"Failed to push task to '{queue_key}' after {self._MAX_RETRIES} attempts"
        ) from last_exception

    def pop_task(self, agent_id: str, timeout: int = 5) -> dict[str, Any] | None:
        """从标准任务队列 BRPOP 弹出任务，超时返回 None。"""
        queue_key = f"task_queue:{agent_id}"
        last_exception: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                result = self._redis.brpop(queue_key, timeout=timeout)
                if result is None:
                    return None
                _, payload = result
                return self._deserialize(payload)
            except (redis.exceptions.ConnectionError, ConnectionError, OSError) as exc:
                last_exception = exc
                logger.warning(
                    "pop_task from '%s' attempt %d/%d failed: %s",
                    queue_key,
                    attempt,
                    self._MAX_RETRIES,
                    exc,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._RETRY_BASE_DELAY * attempt)
                    self._reconnect_if_needed()
        raise redis.exceptions.ConnectionError(
            f"Failed to pop task from '{queue_key}' after {self._MAX_RETRIES} attempts"
        ) from last_exception

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def _reconnect_if_needed(self) -> None:
        try:
            self._redis.ping()
        except Exception:
            logger.info("Redis connection lost, reconnecting...")
            self._redis = self._connect()

    def close(self) -> None:
        try:
            self._redis.close()
            logger.info("MessagingClient connection closed.")
        except Exception as exc:
            logger.warning("Error closing Redis connection: %s", exc)


# ============================================================================
# 能力注册表
# ============================================================================


class AgentRegistry:
    """基于 SQLite 的能力注册表（线程安全，每次操作使用独立游标）。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._init_schema()
            logger.info("AgentRegistry initialized with db '%s'", db_path)
        except sqlite3.Error as exc:
            logger.error("Failed to initialize AgentRegistry: %s", exc)
            raise

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id        TEXT PRIMARY KEY,
                    capabilities    TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'idle',
                    last_heartbeat  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def register(
        self,
        agent_id: str,
        capabilities: list[str],
        status: str = "idle",
    ) -> None:
        """注册或更新智能体信息（含心跳重置）。"""
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        if not capabilities:
            raise ValueError("capabilities must not be empty")

        caps_str = ",".join(capabilities)
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO agents (agent_id, capabilities, status, last_heartbeat)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(agent_id) DO UPDATE SET
                        capabilities = excluded.capabilities,
                        status = excluded.status,
                        last_heartbeat = CURRENT_TIMESTAMP
                    """,
                    (agent_id, caps_str, status),
                )
                self._conn.commit()
            logger.info(
                "Registered agent '%s' with capabilities %s, status '%s'",
                agent_id,
                capabilities,
                status,
            )
        except sqlite3.Error as exc:
            logger.error("Failed to register agent '%s': %s", agent_id, exc)
            raise

    def find_capable_agent(self, required_capability: str) -> str | None:
        """查找一个空闲且具备指定能力的智能体。"""
        if not required_capability:
            raise ValueError("required_capability must not be empty")
        try:
            with self._lock:
                rows = self._conn.execute(
                    """
                    SELECT agent_id, capabilities
                    FROM agents
                    WHERE (status LIKE 'idle%' OR status LIKE 'active:%')
                    ORDER BY last_heartbeat ASC
                    """
                ).fetchall()
            for row in rows:
                agent_caps = [c.strip() for c in row["capabilities"].split(",")]
                if required_capability in agent_caps:
                    logger.info(
                        "Found agent '%s' for capability '%s'",
                        row["agent_id"],
                        required_capability,
                    )
                    return row["agent_id"]
            logger.info("No idle agent found for capability '%s'", required_capability)
            return None
        except sqlite3.Error as exc:
            logger.error(
                "Failed to find capable agent for '%s': %s", required_capability, exc
            )
            raise

    def update_heartbeat(self, agent_id: str) -> None:
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE agents SET last_heartbeat = CURRENT_TIMESTAMP WHERE agent_id = ?",
                    (agent_id,),
                )
                if cur.rowcount == 0:
                    logger.warning(
                        "update_heartbeat: agent '%s' not found in registry", agent_id
                    )
                self._conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to update heartbeat for agent '%s': %s", agent_id, exc)
            raise

    def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
                ).fetchone()
            if row is None:
                return None
            return {
                "agent_id": row["agent_id"],
                "capabilities": [c.strip() for c in row["capabilities"].split(",") if c.strip()],
                "status": row["status"],
                "last_heartbeat": row["last_heartbeat"],
            }
        except sqlite3.Error as exc:
            logger.error("Failed to get agent '%s': %s", agent_id, exc)
            raise

    def list_agents(self) -> list[dict[str, Any]]:
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM agents ORDER BY agent_id"
                ).fetchall()
            return [
                {
                    "agent_id": row["agent_id"],
                    "capabilities": [c.strip() for c in row["capabilities"].split(",") if c.strip()],
                    "status": row["status"],
                    "last_heartbeat": row["last_heartbeat"],
                }
                for row in rows
            ]
        except sqlite3.Error as exc:
            logger.error("Failed to list agents: %s", exc)
            raise

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
            logger.info("AgentRegistry connection closed.")
        except Exception as exc:
            logger.warning("Error closing SQLite connection: %s", exc)


# ============================================================================
# 共享异步 LLM 助手（httpx 连接池，真正的并发）
# ============================================================================


async def async_call_llm(
    system_prompt: str = "",
    user_prompt: str = "",
    instruction: str = "",
    expect_json: bool = False,
) -> str:
    """供所有 Worker 使用的异步 LLM 调用（复用全局 httpx 连接池）。"""
    try:
        from llm_client import call_llm_async

        result = await call_llm_async(
            system_prompt, user_prompt or instruction, expect_json=expect_json
        )
        if isinstance(result, str):
            return result
        return str(result)
    except Exception as exc:
        logger.error("async_call_llm failed: %s", exc)
        return f"LLM error: {exc}"


# ============================================================================
# RedisAgentRegistry - 基于 Redis 的零锁注册表
# ============================================================================


class RedisAgentRegistry:
    """Redis 能力注册表，所有操作均为 O(1) Redis 命令。

    键设计：
        agent:{id}          -> Hash: {agent_id, capabilities, status, last_heartbeat}
        agents:all          -> Set: 所有已注册 agent_id
        capability:{name}   -> Set: 具备该能力的 agent_id
    """

    def __init__(self, redis_client):
        self._r = redis_client

    def _status_idle(self, status: str) -> bool:
        if not status:
            return False
        return status.startswith("idle") or status.startswith("active:")

    def register(self, agent_id: str, capabilities: list[str], status: str = "idle") -> None:
        try:
            caps_str = ",".join(capabilities)
            pipe = self._r.pipeline()
            pipe.hset(
                f"agent:{agent_id}",
                mapping={
                    "agent_id": agent_id,
                    "capabilities": caps_str,
                    "status": status,
                    "last_heartbeat": str(time.time()),
                    "registered_at": str(int(time.time())),
                },
            )
            pipe.sadd("agents:all", agent_id)
            for cap in capabilities:
                pipe.sadd(f"capability:{cap}", agent_id)
            pipe.execute()
        except Exception as exc:
            logger.warning("RedisAgentRegistry.register(%s): %s", agent_id, exc)

    def unregister(self, agent_id: str) -> None:
        try:
            info = self._r.hgetall(f"agent:{agent_id}") or {}
            caps = (info.get("capabilities") or "").split(",")
            pipe = self._r.pipeline()
            pipe.delete(f"agent:{agent_id}")
            pipe.srem("agents:all", agent_id)
            for cap in caps:
                if cap.strip():
                    pipe.srem(f"capability:{cap}", agent_id)
            pipe.execute()
        except Exception as exc:
            logger.warning("RedisAgentRegistry.unregister(%s): %s", agent_id, exc)

    def find_capable_agent(self, required_capability: str) -> str | None:
        try:
            for _ in range(5):
                candidate = self._r.srandmember(f"capability:{required_capability}")
                if not candidate:
                    return None
                info = self._r.hgetall(f"agent:{candidate}")
                status = info.get("status", b"")
                if isinstance(status, bytes):
                    status = status.decode()
                if self._status_idle(status):
                    return candidate
            return None
        except Exception as exc:
            logger.warning(
                "RedisAgentRegistry.find_capable_agent(%s): %s", required_capability, exc
            )
            return None

    def update_status(self, agent_id: str, status: str) -> None:
        try:
            self._r.hset(f"agent:{agent_id}", "status", status)
            self._r.hset(f"agent:{agent_id}", "last_heartbeat", str(time.time()))
        except Exception as exc:
            logger.warning("RedisAgentRegistry.update_status(%s): %s", agent_id, exc)

    def update_heartbeat(self, agent_id: str) -> None:
        try:
            self._r.hset(f"agent:{agent_id}", "last_heartbeat", str(time.time()))
        except Exception as exc:
            logger.warning("RedisAgentRegistry.update_heartbeat(%s): %s", agent_id, exc)

    def list_agents(self) -> list[dict]:
        try:
            ids = self._r.smembers("agents:all")
            result = []
            for aid in ids:
                info = self._r.hgetall(f"agent:{aid}")
                if info:
                    decoded = {}
                    for k, v in info.items():
                        key = k.decode() if isinstance(k, bytes) else k
                        val = v.decode() if isinstance(v, bytes) else v
                        decoded[key] = val
                    result.append(decoded)
            return result
        except Exception:
            return []

    def get_agent_info(self, agent_id: str) -> dict | None:
        try:
            info = self._r.hgetall(f"agent:{agent_id}")
            if not info:
                return None
            decoded = {}
            for k, v in info.items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                decoded[key] = val
            return decoded
        except Exception:
            return None
