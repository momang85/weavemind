"""
织光 (ZhiGuang) — 通用执行智能体基类

Worker 的生命周期：
    启动 → 注册 → 心跳保活 → 循环取任务 → 执行 → 回传结果 → 退出
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

import db_paths
from common import AgentRegistry, MessagingClient

# ---------------------------------------------------------------------------
# 搜索引擎健康状态（对标 O-29：多引擎熔断 + 冷却 + 失败重试）
# ---------------------------------------------------------------------------
_ENGINE_HEALTH: dict[str, dict] = {}
_ENGINE_LOCK = threading.Lock()
_ENGINE_FAIL_THRESHOLD = 2
_ENGINE_COOLDOWN = float(os.environ.get("SEARCH_ENGINE_COOLDOWN", "120") or 120)
# ddgs 支持的 text 引擎全集（backend 参数按名过滤）。
# auto 模式每次查询都尝试全部引擎——环境内 wikipedia/google 等 100% 超时，
# 每次白等 5s×N。任务级健康缓存只查询存活引擎，显著缩短搜索耗时。
# 清单本身**可配置**（config.json 的 `system.search_quality.engines`），
# 默认值取自 adapters.search_quality（检索质量的唯一事实来源）。
_DDG_ENGINES = (
    "brave", "duckduckgo", "google", "grokipedia", "mojeek",
    "startpage", "wikipedia", "yahoo", "yandex",
)


def _ddg_engines() -> tuple:
    """当前生效的引擎清单（策略可覆盖；取回失败用默认清单）。"""
    try:
        from adapters.search_quality import current_policy
        return tuple(current_policy().engines or _DDG_ENGINES)
    except Exception:
        return _DDG_ENGINES
# 异常消息里的 URL → 引擎名（ddgs 异常含失败引擎的 URL；按域名子串匹配）
_DDG_URL_HINTS = (
    ("wikipedia.org", "wikipedia"),
    ("grokipedia.com", "grokipedia"),
    ("google.com", "google"),
    ("brave.com", "brave"),
    ("startpage.com", "startpage"),
    ("yahoo.com", "yahoo"),
    ("duckduckgo.com", "duckduckgo"),
    ("mojeek.com", "mojeek"),
    ("yandex.com", "yandex"),
)


def _ddg_engine_from_error(text: str) -> set[str]:
    """从 ddgs 异常文本提取失败引擎名（URL host → 引擎名）。"""
    low = str(text or "").lower()
    return {name for host, name in _DDG_URL_HINTS if host in low}


_SEARCH_RETRY_BACKOFF = float(os.environ.get("SEARCH_RETRY_BACKOFF", "3") or 3)
_SEARCH_HEALTH_PUB_INTERVAL = float(
    os.environ.get("SEARCH_HEALTH_PUB_INTERVAL", "30") or 30
)
_SEARCH_HEALTH_PUB_STARTED = False
_SEARCH_HEALTH_PUB_LOCK = threading.Lock()


def _engine_healthy(name: str) -> bool:
    """引擎是否可用：连续失败达阈值后进入冷却，冷却期内跳过。"""
    with _ENGINE_LOCK:
        h = _ENGINE_HEALTH.get(name)
        if not h:
            return True
        if not h["healthy"] and time.time() < h["cooldown_until"]:
            return False
        return True


def _mark_engine(name: str, ok: bool) -> None:
    """记录引擎结果；连续失败达阈值 → 熔断进入冷却，冷却到期自动恢复。"""
    with _ENGINE_LOCK:
        h = _ENGINE_HEALTH.setdefault(
            name, {"healthy": True, "fails": 0, "cooldown_until": 0.0}
        )
        if ok:
            h["healthy"] = True
            h["fails"] = 0
        else:
            h["fails"] += 1
            if h["fails"] >= _ENGINE_FAIL_THRESHOLD:
                h["healthy"] = False
                h["cooldown_until"] = time.time() + _ENGINE_COOLDOWN


def get_engine_health() -> dict:
    with _ENGINE_LOCK:
        return {k: dict(v) for k, v in _ENGINE_HEALTH.items()}


def _publish_health_snapshot(messaging) -> None:
    """把引擎健康快照写入 Redis（TTL 120s），供 web_ui / metrics 跨进程读取。

    带上实例身份与观测时刻：读取方据此判断快照是不是本实例的新鲜读数（专项 §6）——
    缺元信息的快照会被按"不知道"处理，不再默认显示为健康。
    """
    try:
        r = getattr(messaging, "redis", None) or getattr(messaging, "_redis", None)
        if r is None:
            return
        payload = dict(get_engine_health())
        try:
            from health_registry import snapshot_meta
            payload.update(snapshot_meta())
        except Exception:
            pass
        r.set(
            "search_engine_health",
            json.dumps(payload, ensure_ascii=False),
            ex=120,
        )
    except Exception:
        pass


def _publish_health_loop(messaging) -> None:
    while True:
        _publish_health_snapshot(messaging)
        time.sleep(_SEARCH_HEALTH_PUB_INTERVAL)


def ensure_health_publisher(messaging) -> None:
    """幂等启动健康快照发布线程。"""
    global _SEARCH_HEALTH_PUB_STARTED
    with _SEARCH_HEALTH_PUB_LOCK:
        if _SEARCH_HEALTH_PUB_STARTED:
            return
        _SEARCH_HEALTH_PUB_STARTED = True
    threading.Thread(
        target=_publish_health_loop, args=(messaging,), daemon=True
    ).start()

logger = logging.getLogger(__name__)


# ============================================================================
# 异常定义
# ============================================================================


class TaskExecutionError(Exception):
    """任务执行失败时抛出的异常"""

    def __init__(self, task_id: str, original: Exception) -> None:
        self.task_id = task_id
        self.original = original
        super().__init__(f"Task '{task_id}' failed: {original}")


# ============================================================================
# 通用执行智能体基类
# ============================================================================


class BaseWorker(ABC):
    """通用执行智能体基类。

    子类只需实现 `execute(instruction) -> str` 方法。
    生命周期管理（注册、心跳、任务循环、优雅退出）由基类处理。

    Usage:
        class MyWorker(BaseWorker):
            def execute(self, instruction: str) -> str:
                return f"done: {instruction}"

        worker = MyWorker(
            agent_id="my_worker",
            capabilities=["search"],
            registry=AgentRegistry("agents.db"),
            messaging=MessagingClient("localhost", 6379),
        )
        worker.run()
    """

    # 本次任务的执行契约（`_process_task` 从派发载荷装入）。类级默认 None：
    # 直接调用 `execute()` 的场景（离线检查/单测）按"无契约"走指令文本路径。
    _contract = None

    # 心跳间隔（秒）
    _HEARTBEAT_INTERVAL: float = 10.0
    # 任务拉取超时（秒）
    _POP_TIMEOUT: int = 5

    def __init__(
        self,
        agent_id: str,
        capabilities: list[str],
        registry: AgentRegistry,
        messaging: MessagingClient,
    ) -> None:
        """初始化 Worker。

        Args:
            agent_id: 智能体唯一标识符。
            capabilities: 能力列表。
            registry: 能力注册表实例。
            messaging: 消息客户端实例。
        """
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        if not capabilities:
            raise ValueError("capabilities must not be empty")

        self.agent_id = agent_id
        self.capabilities = capabilities
        self._registry = registry
        self._messaging = messaging

        # 控制标志
        self._running = False
        self._shutting_down = False
        # 当前正在执行的任务 ID（用于优雅退出时等待）
        self._current_task_id: str | None = None
        self._current_task_lock = threading.Lock()

        # 线程
        self._heartbeat_thread: threading.Thread | None = None
        self._task_thread: threading.Thread | None = None

        logger.info(
            "BaseWorker '%s' initialized, capabilities: %s",
            agent_id,
            capabilities,
        )

    # ------------------------------------------------------------------
    # 抽象方法（子类实现）
    # ------------------------------------------------------------------

    @abstractmethod
    def execute(self, instruction: str) -> str:
        """执行任务的核心方法，由子类实现。

        子类在此方法中完成具体的业务逻辑。抛出任何异常都会被
        _process_task 捕获并记录为 FAILED 结果。

        Args:
            instruction: 任务的自然语言指令。

        Returns:
            执行结果字符串。

        Raises:
            Exception: 执行失败时抛出，会被上层捕获。
        """
        ...

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def run(self) -> None:
        """启动 Worker：注册、心跳、任务循环。

        这是一个阻塞方法，直到 shutdown() 被调用。
        """
        # 注册自己
        self._register()

        # 注册信号处理
        self._setup_signal_handlers()

        # 启动心跳线程
        self._running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"heartbeat-{self.agent_id}",
            daemon=True,
        )
        self._heartbeat_thread.start()
        logger.info("'%s' heartbeat thread started.", self.agent_id)

        # 监听 kill 指令（agent.kill:{id}）
        self._kill_thread = threading.Thread(
            target=self._listen_kill,
            name=f"kill-{self.agent_id}",
            daemon=True,
        )
        self._kill_thread.start()

        # 进入任务循环（阻塞）
        self._task_loop()

    def _listen_kill(self) -> None:
        """监听 agent.kill:{id} 频道，收到 die 指令后优雅退出。"""
        try:
            for msg in self._messaging.subscribe(f"agent.kill:{self.agent_id}"):
                if msg.get("action") == "die":
                    logger.info(
                        "Kill signal received for '%s', shutting down", self.agent_id
                    )
                    self.shutdown()
                    return
        except Exception as exc:
            logger.warning("Kill listener error for '%s': %s", self.agent_id, exc)

    def shutdown(self, signum: int | None = None, frame: Any = None) -> None:
        """优雅退出。停止取新任务，等待当前任务完成，清理。

        Args:
            signum: 信号编号（由 signal handler 传入）。
            frame: 栈帧（由 signal handler 传入）。
        """
        if self._shutting_down:
            return  # 防止重复退出
        self._shutting_down = True
        signal_name = signal.Signals(signum).name if signum else "manual"
        logger.info(
            "'%s' received %s, shutting down gracefully...",
            self.agent_id,
            signal_name,
        )
        self._running = False

        # 等待当前任务完成
        self._wait_for_current_task()

        # 尝试将状态设为 offline
        try:
            self._registry.register(
                self.agent_id, self.capabilities, status="offline"
            )
        except Exception as exc:
            logger.warning("'%s' failed to set offline status: %s", self.agent_id, exc)

        logger.info("'%s' shutdown complete.", self.agent_id)

    # ------------------------------------------------------------------
    # 内部：注册与心跳
    # ------------------------------------------------------------------

    def _register(self) -> None:
        """向能力注册表注册自己（idle 状态，带重试）。"""
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                self._registry.register(
                    self.agent_id, self.capabilities, status="idle"
                )
                logger.info(
                    "'%s' registered successfully (attempt %d).",
                    self.agent_id,
                    attempt,
                )
                return
            except Exception as exc:
                logger.warning(
                    "'%s' registration attempt %d/%d failed: %s",
                    self.agent_id,
                    attempt,
                    max_retries,
                    exc,
                )
                if attempt < max_retries:
                    time.sleep(1.0 * attempt)
        raise RuntimeError(
            f"'{self.agent_id}' failed to register after {max_retries} attempts"
        )

    def _heartbeat_loop(self) -> None:
        """后台心跳线程，每 _HEARTBEAT_INTERVAL 秒更新一次心跳。"""
        while self._running:
            try:
                self._registry.update_heartbeat(self.agent_id)
                logger.debug("'%s' heartbeat sent.", self.agent_id)
            except Exception as exc:
                logger.warning("'%s' heartbeat failed: %s", self.agent_id, exc)
            # 分段 sleep，以便快速响应 shutdown
            for _ in range(int(self._HEARTBEAT_INTERVAL)):
                if not self._running:
                    break
                time.sleep(1.0)

    # ------------------------------------------------------------------
    # 内部：任务循环
    # ------------------------------------------------------------------

    def _install_permit_store(self) -> None:
        """装**执行许可记录**：真实运行放 Redis（跨进程看得见取消/换尝试）。

        只放进程内的话，worker 永远读不到编排器写的取消——那正是 T0-b 反例
        "worker 不继承编排器守卫"的机制根因，不是测试问题。
        """
        import execution_permit as ep
        try:
            r = getattr(self._messaging, "_redis", None)
            if r is not None and hasattr(r, "set") and hasattr(r, "get"):
                ep.configure_store(ep.RedisPermitStore(r))
                return
        except Exception as exc:                  # noqa: BLE001
            logger.warning("'%s' 执行许可记录装配失败：%s", self.agent_id, str(exc)[:120])
        logger.warning("'%s' 执行许可记录退回进程内（看不到别的进程的取消）", self.agent_id)
        ep.configure_store(ep.MemoryPermitStore())

    def _task_loop(self) -> None:
        """主任务循环：阻塞式拉取任务 → 执行 → 回传结果。"""
        logger.info("'%s' task loop started.", self.agent_id)
        try:
            self._install_permit_store()
        except Exception:
            pass
        while self._running and not self._shutting_down:
            task: dict[str, Any] | None = None
            try:
                try:
                    # 阻塞等待任务
                    task = self._messaging.pop_task(self.agent_id, timeout=self._POP_TIMEOUT)
                except Exception as exc:
                    # BRPOP 阻塞时长超过 Redis socket_timeout（POP_TIMEOUT 与
                    # socket_timeout 参数不匹配）时 redis 客户端抛 TimeoutError：
                    # 属正常等待超时，静默续环（此前每 ~2.5 分钟刷一次整屏 ERROR）
                    if type(exc).__name__ == "TimeoutError":
                        continue
                    raise
                if task is None:
                    continue  # 超时，继续下一轮

                logger.info(
                    "'%s' received task '%s': %s",
                    self.agent_id,
                    task.get("task_id", "unknown"),
                    task.get("instruction", "")[:80],
                )

                # 设为 busy
                self._set_status("busy")

                # 记录当前任务 ID
                task_id = task.get("task_id", "unknown")
                with self._current_task_lock:
                    self._current_task_id = task_id

                # L01：LLM 台账归属根任务。contextvars 不跨线程，必须在**本任务线程**里绑定；
                # 接收边界统一走 admit_dispatch（协议非法/银行缺身份/配置非法都拒绝）。
                from task_context import admit_dispatch, bind_llm_accounting, clear_llm_accounting
                ctx, gaps, refuse_reason = admit_dispatch(task)
                if ctx is None:
                    logger.error("'%s' 拒绝派发 %s：%s", self.agent_id, task_id, refuse_reason)
                    self._publish_failure(task_id, refuse_reason)
                    with self._current_task_lock:
                        self._current_task_id = None
                    continue
                if gaps:
                    # 本地兼容/身份不完整的可观测记录：日志 + 随结果回传，不静默吞掉
                    logger.warning("'%s' 派发 %s 身份兼容缺口：%s", self.agent_id, task_id, gaps)
                bind_llm_accounting(ctx)
                with self._current_task_lock:
                    self._current_ctx = ctx
                    self._current_gaps = gaps
                try:
                    # 处理任务
                    self._process_task(task)
                finally:
                    clear_llm_accounting()
                    # T0-b：守卫按任务装、按任务清（否则会把上一个任务的许可
                    # 带到下一个任务上，变成"用旧许可放行新任务"）
                    self._clear_permit_guard()
                    self._permit = None
                    with self._current_task_lock:
                        self._current_ctx = None

            except Exception as exc:
                logger.error(
                    "'%s' unhandled error in task loop: %s",
                    self.agent_id,
                    exc,
                    exc_info=True,
                )
                # 如果任务还在手上，尝试回传失败结果
                if task is not None:
                    self._publish_failure(
                        task.get("task_id", "unknown"), f"Worker error: {exc}"
                    )
            finally:
                # 始终重置状态和能力中的 'busy' 为 'idle'
                with self._current_task_lock:
                    self._current_task_id = None
                self._set_status("idle")

        logger.info("'%s' task loop exited.", self.agent_id)

    def _process_task(self, task: dict[str, Any]) -> None:
        """处理单个任务：执行 → 序列化 → 发布结果。

        Args:
            task: 任务字典，至少包含 task_id 和 instruction。
        """
        task_id = task.get("task_id", "unknown")
        instruction = task.get("instruction", "")

        if not instruction:
            logger.warning("'%s' task '%s' has empty instruction.", self.agent_id, task_id)
            self._publish_failure(task_id, "Empty instruction")
            return

        # 执行契约（派发载荷里的结构化字段）：检索查询由它生成，不依赖指令文本
        # （指令可被模型修订/技能注入改写，结构化字段不会）。结构不对就按"无契约"
        # 处理——退回指令里的 `[检索查询]` 行，而不是拿半个契约去检索。
        self._contract = None
        _wire = task.get("contract")
        if isinstance(_wire, dict) and _wire:
            try:
                from execution_contract import ExecutionContract
                self._contract = ExecutionContract.from_wire(_wire)
            except Exception as exc:                 # noqa: BLE001
                logger.warning("'%s' 派发契约不可用，按无契约处理：%s",
                               self.agent_id, str(exc)[:120])
        if _wire and self._contract is None:
            logger.warning("'%s' 派发契约版本/结构不识别，按无契约处理", self.agent_id)

        # T0-b：排队期间可能已被取消/换尝试 → **不执行**，如实回传。
        # 旧实现在被取消后照样把步骤跑完（付费调用照发），只在结果回来后才被丢弃。
        _permit = self._task_permit(task)
        self._permit = _permit
        _why = self._permit_gate(_permit)
        if _why:
            logger.error("'%s' 拒绝执行 %s：执行许可不通过（%s）",
                         self.agent_id, task_id, _why)
            self._publish_refused(task_id, _why)
            return
        self._install_permit_guard(_permit)

        try:
            # 调用子类的 execute 方法
            result = self.execute(instruction)
            # 执行结束后、发布之前**再核一次**：这段时间里可能被取消或已换尝试，
            # 那样这份结果属于旧尝试，不得发布（编排器也会拒，但这里先挡住）。
            _why2 = self._permit_gate(_permit)
            if _why2:
                logger.error("'%s' 不发布 %s 的结果：执行许可失效（%s）",
                             self.agent_id, task_id, _why2)
                self._publish_refused(task_id, _why2)
                return
            self._publish_result(task_id, "SUCCESS", result)
        except Exception as exc:
            logger.error(
                "'%s' task '%s' execution failed: %s",
                self.agent_id,
                task_id,
                exc,
                exc_info=True,
            )
            self._publish_failure(task_id, str(exc))

    def _task_permit(self, task: dict):
        """从派发载荷解出**执行许可**（T0-b）；结构不识别 = 没有许可（不猜）。"""
        raw = (task or {}).get("permit")
        if not isinstance(raw, dict) or not raw:
            return None
        try:
            import execution_permit as ep
            return ep.ExecutionPermit.from_wire(raw)
        except Exception:
            return None

    def _permit_gate(self, permit) -> str:
        """按**可信当前记录**核许可；空串=放行，否则返回拒绝原因。

        worker 是另一个进程：拿不到编排器内存里的取消标志，只能相信共享真源
        （Redis 上的尝试记录）。没有许可字段的旧派发按离线旁路放行，但会留日志。
        """
        if permit is None:
            return ""
        try:
            import execution_permit as ep
            ok, reason = ep.validate(permit, permit.root_task_id)
            return "" if ok else reason
        except Exception as exc:                  # noqa: BLE001 - 读不到真源=未知
            logger.warning("'%s' 执行许可核验异常（按拒绝处理）：%s",
                           self.agent_id, str(exc)[:120])
            return "permit_store_unavailable"

    def _install_permit_guard(self, permit) -> None:
        """把执行许可装进 LLM 客户端的取消守卫。

        T0-b 反例：worker 不继承编排器守卫 → 取消/失租后同步、异步、重试、备用
        各自还会再发请求。装上之后，`llm_client` 在**每次尝试前**与**切备用前**
        复查（同步与异步两条路都查）。
        """
        if permit is None:
            return
        try:
            import llm_client as _lc
            _lc.set_cancel_guard(lambda _tid, _p=permit: bool(self._permit_gate(_p)))
        except Exception as exc:                  # noqa: BLE001
            logger.warning("'%s' 执行许可守卫注册失败：%s", self.agent_id, str(exc)[:120])

    def _clear_permit_guard(self) -> None:
        try:
            import llm_client as _lc
            _lc.set_cancel_guard(None)
        except Exception:
            pass

    def _publish_refused(self, task_id: str, why: str) -> None:
        """许可不通过：如实回传"未执行"，绝不假装成功。"""
        try:
            import execution_permit as ep
            status = "CANCELLED" if why == ep.R_CANCELLED else "FAILED"
        except Exception:
            status = "FAILED"
        self._publish_result(task_id, status, f"执行许可不通过，未执行：{why}")

    # ------------------------------------------------------------------
    # 内部：结果发布
    # ------------------------------------------------------------------

    def _publish_result(self, task_id: str, status: str, result: str) -> None:
        """发布任务执行结果到结果频道。

        Args:
            task_id: 任务 ID。
            status: SUCCESS 或 FAILED。
            result: 结果字符串。
        """
        channel = f"task_result:{task_id}"
        message = {
            "task_id": task_id,
            "agent_id": self.agent_id,
            "status": status,
            "result": result,
        }
        # P1-c②：检索真值作为**兄弟字段**上行——`result` 仍是 JSON 数组（旧边界不动），
        # 但"查询完成但零命中"与"根本没完成查询"从此跨层可分。
        # 放兄弟字段而不是塞进 `result`：现有消费者按数组解析 `result`（4 处），
        # 改形状会连带改掉它们，而这里只需要"说出来"。
        _ss = getattr(self, "_search_status", None)
        if isinstance(_ss, dict) and _ss.get("status"):
            message["search_status"] = _ss
        # T0-b：结果带上**本步骤所在尝试**的身份，上游据此判"这份结果是不是当前
        # 尝试的"（旧尝试的迟到结果不得采纳，也不得发布终态）。
        _permit = getattr(self, "_permit", None)
        if _permit is not None:
            try:
                import execution_permit as _ep
                message.update(_ep.echo_fields(_permit))
            except Exception:
                pass
        # L01：结果回显身份上下文，否则上游只能看到派发 id，
        # 步骤/派发级归属（花了多少、属于哪一步）无从重建。
        ctx = getattr(self, "_current_ctx", None)
        if ctx is not None:
            try:
                message["context"] = ctx.to_wire()
                gaps = getattr(self, "_current_gaps", None) or []
                if gaps:
                    message["context_gaps"] = list(gaps)
            except Exception:
                pass
        try:
            self._messaging._redis.rpush(channel, json.dumps(message, ensure_ascii=False))
            logger.info(
                "'%s' published result for task '%s': %s",
                self.agent_id,
                task_id,
                status,
            )
        except Exception as exc:
            logger.error(
                "'%s' failed to publish result for task '%s': %s",
                self.agent_id,
                task_id,
                exc,
            )
        # Roadmap 余项②：灰度策略任务结果记录（回滚监控数据）
        try:
            self._record_rollout_result(task_id, status)
        except Exception:
            pass

    def _publish_failure(self, task_id: str, error_msg: str) -> None:
        """发布失败结果的便捷方法。"""
        self._publish_result(task_id, "FAILED", error_msg)

    # ------------------------------------------------------------------
    # 内部：状态管理
    # ------------------------------------------------------------------

    def _set_status(self, status: str) -> None:
        """更新自己在注册表中的状态。

        Args:
            status: 新状态字符串（idle/busy/offline）。
        """
        try:
            self._registry.register(self.agent_id, self.capabilities, status=status)
        except Exception as exc:
            logger.warning(
                "'%s' failed to update status to '%s': %s",
                self.agent_id,
                status,
                exc,
            )

    # ------------------------------------------------------------------
    # 内部：优雅退出辅助
    # ------------------------------------------------------------------

    def _wait_for_current_task(self, timeout: float = 30.0) -> None:
        """等待当前正在执行的任务完成。

        Args:
            timeout: 最长等待秒数。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._current_task_lock:
                if self._current_task_id is None:
                    return
            logger.info(
                "'%s' waiting for current task '%s' to complete...",
                self.agent_id,
                self._current_task_id,
            )
            time.sleep(0.5)
        logger.warning(
            "'%s' timeout waiting for current task, forcing shutdown.",
            self.agent_id,
        )

    def _setup_signal_handlers(self) -> None:
        """注册 SIGTERM / SIGINT 信号处理器。"""
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.shutdown)
            except Exception as exc:
                logger.warning(
                    "'%s' could not register signal handler for %s: %s",
                    self.agent_id,
                    sig,
                    exc,
                )


# ============================================================================
# 示例：搜索智能体
# ============================================================================


class SearchAgent(BaseWorker):
    """Real web search agent using DuckDuckGo (free, no API key)."""

    # 通用垃圾域名/URL 特征（博彩、娱乐导航、下载站、爬虫假页等，不只针对恒大）
    _SPAM_DOMAINS = (
        "susmeat.com", "aydvjch.cc", "example.com", "imty-web.com",
        "zhxsg.com", "mmzx2.cn", "ng28gaming.com", "online-28quan.com",
        "28quan.com", "365qp", "88qp", "h888", "ky777", "lywl",
    )
    # 低权威文档站：个人上传/文库类，对调研任务无权威性，直接排除
    _LOW_AUTHORITY_DOMAINS = (
        "wenku.baidu.com", "book118.com", "max.book118.com", "doc88.com",
        "docin.com", "jz.docin.com", "mbd.baidu.com", "wenku.so.com",
    )
    _JUNK_URL_PATTERNS = (
        r"/works/\d+\.html",      # 博彩/短视频垃圾站的典型路径
        r"/tiyu-toutiao/",        # 借"体育头条"外衣的博彩页
        r"/login|/register|/agent",  # 博彩代理/注册页
    )
    _GAMBLING_KEYWORDS = (
        "博彩", "六合彩", "彩票", "投注", "下注", "返水", "棋牌", "电玩",
        "真人视讯", "娱乐城", "时时彩", "开户送", "注册送", "秒到账",
        "提现", "抢庄", "龙虎", "牛牛", "百家乐", "老虎机", "赌场",
        "casino", "lottery", "bet365", "betting", "gambling",
    )
    _JUNK_TITLES = ("google", "bing", "microsoft", "登录", "403", "404")
    _ZH_STOP = {
        "一个", "我们", "你们", "他们", "完成", "输出", "生成", "要求",
        "进行", "需要", "可以", "是否", "如何", "什么", "请", "帮", "并",
        "与", "和", "在", "用", "把", "将", "给", "让", "这", "那", "为",
        "对", "其", "及", "或", "等", "做", "写", "的", "了", "是", "我",
        "你", "他", "她", "它", "搜索", "获取", "项目", "文件", "内容",
        "结果", "报告", "选择", "优先", "记录", "完整", "基于", "上一步",
        "编写", "实现", "包含", "以及", "使用", "提供", "相关", "信息",
        "资料", "地址", "链接", "来源", "开源", "同时", "需要", "并且",
        "如果", "找到", "查看", "说明", "运行", "方式", "明确", "给出",
        # 指令包装词与动词，避免污染查询
        "任务", "目标", "原始", "指令", "调研", "现状", "国内", "国外",
        "关于", "针对", "请根据", "确保", "然后", "随后", "接下来",
        # 步骤信封词（中枢追加的角色/受众/质量标准），避免污染查询
        "角色", "受众", "质量标准", "输出要求", "任务目标", "用户目标",
        "自迭代改进", "一份", "董事会", "汇报", "注明", "预测", "建议",
        "需包含", "风险分析", "对比", "数据", "图表", "指标",
    }
    _EN_STOP = {
        "the", "and", "with", "from", "for", "that", "this", "not",
        "are", "was", "were", "output", "only", "json", "using", "your",
    }
    # 已部署策略（人工审批后写入 strategy:active:search_agent，每次执行时刷新）
    _strategy_max_sources = 5
    _strategy_blocks: list[str] = []
    _strategy_boosts: list[str] = []
    # Roadmap 余项②：策略灰度（rollout 0-1）+ 自动回滚监控
    _strategy_id = ""
    _strategy_rollout = 1.0
    _rollout_checked_at = 0.0

    def _load_active_strategy(self) -> None:
        """读取已部署策略并解析为过滤规则：排除词（黑名单）与优先词（排序加分）。
        支持灰度：rollout<1 时按任务 id 哈希分流，仅部分任务应用新策略；
        灰度期间记录任务结果，成功率过低自动回滚（Roadmap 余项②）。"""
        ensure_health_publisher(self._messaging)  # 幂等：启动健康快照发布线程
        self._strategy_max_sources = 5
        self._strategy_blocks = []
        self._strategy_boosts = []
        self._strategy_id = ""
        self._strategy_rollout = 1.0
        try:
            raw = self._messaging.redis.get("strategy:active:search_agent")
            if not raw:
                return
            data = json.loads(raw)
            self._strategy_id = str(data.get("strategy_id") or "")
            try:
                self._strategy_rollout = min(1.0, max(0.0, float(data.get("rollout", 1.0) or 1.0)))
            except (TypeError, ValueError):
                self._strategy_rollout = 1.0
            self._strategy_max_sources = max(1, int(data.get("max_sources", 5)))
            for rule in (data.get("filter_rules") or []):
                rule = str(rule)
                low = rule.lower()
                if ":" in rule:
                    word = rule.split(":", 1)[1].strip()
                elif rule.startswith("排除"):
                    word = rule[2:].strip()
                elif rule.startswith("优先"):
                    word = rule[2:].strip()
                else:
                    word = rule
                if not word:
                    continue
                if "排除" in rule or low.startswith("exclude"):
                    self._strategy_blocks.append(word.lower())
                elif "优先" in rule or low.startswith("prefer"):
                    self._strategy_boosts.append(word.lower())
            logger.info(
                "Active strategy applied: id=%s rollout=%.2f max_sources=%d blocks=%s boosts=%s",
                self._strategy_id, self._strategy_rollout, self._strategy_max_sources,
                self._strategy_blocks, self._strategy_boosts,
            )
            # 灰度期间顺带检查回滚（节流 60s，多进程安全）
            self._maybe_rollback_strategy()
        except Exception as exc:
            logger.warning("Failed to load active strategy: %s", exc)

    def _strategy_applies(self, task_id: str) -> bool:
        """灰度分流：rollout=1 全量应用；否则按 task_id 哈希进入灰度桶。"""
        if self._strategy_rollout >= 1.0:
            return True
        if self._strategy_rollout <= 0.0:
            return False
        try:
            import hashlib
            h = int(hashlib.md5(str(task_id).encode("utf-8")).hexdigest()[:8], 16)
            return (h % 1000) < int(self._strategy_rollout * 1000)
        except Exception:
            return False

    def _record_rollout_result(self, task_id: str, status: str) -> None:
        """灰度期间记录任务结果到 Redis：strategy:rollout:{sid} (task_id -> status)。
        仅当当前任务实际应用了灰度策略时记录。"""
        if not self._strategy_id or self._strategy_rollout >= 1.0:
            return
        if not self._strategy_applies(task_id):
            return
        try:
            key = f"strategy:rollout:{self._strategy_id}"
            self._messaging.redis.hset(key, str(task_id), str(status or "SUCCESS"))
            self._messaging.redis.expire(key, 7 * 24 * 3600)
        except Exception as exc:
            logger.warning("rollout result record failed: %s", str(exc)[:100])

    def _maybe_rollback_strategy(self) -> None:
        """灰度回滚检查：样本 ≥5 且成功率 <50% → 自动回滚（删除 active + 发布事件）。
        60s 节流 + Redis 锁防止多 worker 并发回滚。"""
        if not self._strategy_id or self._strategy_rollout >= 1.0:
            return
        now = time.time()
        if now - self._rollout_checked_at < 60:
            return
        self._rollout_checked_at = now
        try:
            key = f"strategy:rollout:{self._strategy_id}"
            data = self._messaging.redis.hgetall(key)
            if not data or len(data) < 5:
                return
            total = len(data)
            ok = sum(1 for s in data.values() if str(s or "").upper() == "SUCCESS")
            rate = ok / total
            logger.info("Rollout monitor: strategy=%s sample=%d success_rate=%.0f%%",
                        self._strategy_id, total, rate * 100)
            if rate >= 0.5:
                return
            # 自动回滚：加锁防止并发
            lock = self._messaging.redis.set(
                "strategy:rollback_lock", "1", nx=True, ex=300,
            )
            if not lock:
                return
            self._messaging.redis.delete("strategy:active:search_agent")
            self._messaging.redis.delete(key)
            try:
                self._messaging.publish("registry.capability.update", {
                    "type": "strategy_rollback",
                    "strategy_id": self._strategy_id,
                    "reason": f"灰度成功率 {rate * 100:.0f}% 低于 50%（样本 {total}）",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                })
            except Exception:
                pass
            logger.warning(
                "AUTO-ROLLBACK strategy=%s success_rate=%.0f%% sample=%d",
                self._strategy_id, rate * 100, total,
            )
            self._strategy_id = ""
            self._strategy_blocks = []
            self._strategy_boosts = []
            self._strategy_max_sources = 5
        except Exception as exc:
            logger.warning("rollback check failed: %s", str(exc)[:100])

    @staticmethod
    def _clean_search_text(text: str) -> str:
        """去掉指令包装，取"用户目标"作为查询基础（否则"任务目标/用户目标/原始指令"
        等包装词会混进查询词，导致搜索结果与主题无关）。

        实现收敛到 `adapters.search_quality`（唯一事实来源）：本方法此前与
        轻量检索那条路径各有一份近似实现，改一处忘一处就会两边判定不一致。
        """
        from adapters.search_quality import clean_search_text
        return clean_search_text(text)

    def _extract_keywords(self, text: str) -> str:
        """从用户目标中提取核心搜索词：去指令包装与停用词、保留年份、
        按停用词切分出完整词段（避免 2-4 字滑窗把"新能源汽车"拆成碎片）。"""
        from adapters.search_quality import extract_keywords
        return extract_keywords(text, self._search_policy())

    @staticmethod
    def _search_policy():
        """当前检索质量策略（阈值/词表/引擎清单，来自 config + 环境变量）。

        取回失败时用模块默认值：检索路径不能因为配置读取问题整体失效。
        """
        try:
            from adapters.search_quality import current_policy
            return current_policy()
        except Exception:
            from adapters.search_quality import _DEFAULT_POLICY
            return _DEFAULT_POLICY

    # 步骤指令里的**显式短查询**行（批次3-1）：编排器从结构化契约（主体/期间/文档类型）
    # 生成 `[检索查询] …`，检索只按它构造变体——整段任务要求（含样板句）扔给检索器会
    # 让引擎大面积无结果、候选里没有年报正文（实机 ui-750185076a）。

    def _search_budget(self, allowance: int | None = None, wall_left: float | None = None):
        """任务级检索预算：总墙钟与提供方调用次数共用同一条截止线（专项 §5）。

        `allowance` / `wall_left` 由任务级台账（`_search_ledger`）给出余额：编排器的重试是
        **新派发**，各给一份默认额度会把一个任务的总调用量放大成"重试次数 × 6"。台账在
        Redis 上按任务累计，因此重试只拿到余额；余额为 0 时调用方直接不发请求。
        """
        from adapters.search_runner import (
            DEFAULT_DEADLINE_SECONDS, DEFAULT_MAX_CALLS, SearchBudget)
        try:
            secs = float(os.environ.get("WM_SEARCH_DEADLINE_SECONDS", "")
                         or DEFAULT_DEADLINE_SECONDS)
        except Exception:
            secs = DEFAULT_DEADLINE_SECONDS
        try:
            calls = int(os.environ.get("WM_SEARCH_MAX_CALLS", "") or DEFAULT_MAX_CALLS)
        except Exception:
            calls = DEFAULT_MAX_CALLS
        if allowance is not None:
            calls = min(calls, int(allowance))
        if wall_left is not None:
            secs = min(secs, max(0.1, float(wall_left)))
        return SearchBudget(max_calls=calls, deadline_seconds=secs)

    # ── 任务级检索台账（跨派发共享；只记次数与首次检索时刻）──────────────────
    _SEARCH_LEDGER_FIELD_USED = "used"
    _SEARCH_LEDGER_FIELD_STARTED = "started"
    _SEARCH_LEDGER_TTL = 7200

    def _search_ledger_key(self) -> str:
        ctx = getattr(self, "_current_ctx", None)
        task_id = str(getattr(ctx, "root_task_id", "") or "") or str(
            getattr(ctx, "dispatch_id", "") or "")
        return f"search_ledger:{task_id}" if task_id else ""

    def _ledger_pick(self, raw, field: str):
        """从 HGETALL 结果里取字段（Redis 可能给 bytes 键，两种都认）。"""
        for k, v in (raw.items() if isinstance(raw, dict) else ()):
            if str(k).endswith(field):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None
        return None

    def _search_ledger_read(self) -> tuple[int, float, bool]:
        """(已用次数, 起点时刻, 台账可读)。**不可读**与"零使用"必须分开：读不到不许发新额度。"""
        key = self._search_ledger_key()
        if not key:
            return 0, 0.0, False
        try:
            raw = self._messaging._redis.hgetall(key) or {}
            return (int(self._ledger_pick(raw, self._SEARCH_LEDGER_FIELD_USED) or 0),
                    float(self._ledger_pick(raw, self._SEARCH_LEDGER_FIELD_STARTED) or 0.0),
                    True)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("检索台账读取失败：%s", str(exc)[:120])
            return 0, 0.0, False

    def _reserve_search_calls(self, want: int, *, total_calls: int,
                              total_secs: float) -> tuple[int, float, str]:
        """首次请求**之前**原子预占次数并落下起点；返回 `(本次可用次数, 剩余秒, 说明)`。

        为什么必须预占（指令 §3.3 反例）：跑完才写台账时，首轮耗 59 秒只用 1 次，
        下一派发仍按"整份额度"发 5 次/60 秒——次数与墙钟都对不上。预占把两件事都钉在
        **首次请求**这一刻：`HSETNX started` 只写一次（根任务起点），`HINCRBY used`
        给并发派发定序（先占者得额度，超额回滚），未用额度收尾时按实数退回。

        台账不可读或不可写 → 返回可用 0、说明原因（**不因为读不到而获得新额度**）。
        """
        key = self._search_ledger_key()
        want = max(0, int(want))
        if not key or want <= 0:
            return 0, 0.0, "无任务身份/无需额度"
        try:
            r = self._messaging._redis
            now = time.time()
            taken = int(r.hincrby(key, self._SEARCH_LEDGER_FIELD_USED, want))
            r.hsetnx(key, self._SEARCH_LEDGER_FIELD_STARTED, now)
            r.expire(key, self._SEARCH_LEDGER_TTL)
            if taken > int(total_calls):
                r.hincrby(key, self._SEARCH_LEDGER_FIELD_USED, -(taken - int(total_calls)))
            granted = max(0, int(total_calls) - (taken - want))
            granted = min(granted, want)
            raw = r.hgetall(key) or {}
            started = float(self._ledger_pick(raw, self._SEARCH_LEDGER_FIELD_STARTED) or now)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("检索额度预占失败（本次不发请求）：%s", str(exc)[:120])
            return 0, 0.0, f"台账预占失败：{str(exc)[:60]}"
        left = max(0.0, float(total_secs) - max(0.0, now - started))
        return granted, left, ""

    def _release_search_calls(self, unused: int) -> None:
        """收尾：把预占但没真正发出的额度退回（best-effort，失败只记日志）。"""
        key = self._search_ledger_key()
        unused = int(unused)
        if not key or unused <= 0:
            return
        try:
            self._messaging._redis.hincrby(
                key, self._SEARCH_LEDGER_FIELD_USED, -unused)
        except Exception as exc:                     # noqa: BLE001
            logger.warning("检索额度退回失败：%s", str(exc)[:120])

    def _search_ledger_limits(self) -> tuple[int, float]:
        """任务级上限：默认 6 次 provider 调用 / 60 秒（指令明确不放宽）。"""
        from adapters.search_runner import (
            DEFAULT_DEADLINE_SECONDS, DEFAULT_MAX_CALLS)
        try:
            total_calls = int(os.environ.get("WM_SEARCH_MAX_CALLS", "")
                              or DEFAULT_MAX_CALLS)
        except Exception:
            total_calls = DEFAULT_MAX_CALLS
        try:
            total_secs = float(os.environ.get("WM_SEARCH_DEADLINE_SECONDS", "")
                               or DEFAULT_DEADLINE_SECONDS)
        except Exception:
            total_secs = DEFAULT_DEADLINE_SECONDS
        return max(1, total_calls), max(1.0, total_secs)

    def _ddg_backend(self) -> tuple:
        """`(ddgs 备后端名, 依据)`：registry=已核实可用；policy=注册表读不到（未核实）；
        none=清单里的后端在当前 ddgs 版本里一个都没启用——返回空名，**不发请求**。

        为什么不能照清单直接打：ddgs 的 `_get_engines` 遇到不在注册表里的后端名会
        静默回落 auto（全引擎重扫）。包内实测 `backend="yandex"`（9.16 已停用）
        白等 32 秒才 ConnectError，正是这条回落路径。
        """
        from adapters.search_quality import ddg_text_backends, select_ddg_backend

        name, basis = select_ddg_backend(_ddg_engines())
        if not name:
            logger.warning(
                "no policy ddgs backend enabled in this ddgs build (%s); "
                "ddgs left out of this task's search providers",
                ",".join(ddg_text_backends()) or "registry unreadable",
            )
        elif basis == "policy":
            logger.warning("ddgs backend registry unreadable; using %r unverified", name)
        return name, basis

    def _provider_specs(self) -> list:
        """提供方规格：Bing 主 + ddgs 单备后端（都是显式单一后端，从不 auto）。

        S0 事实：Bing HTML 两侧环境都可用且快（0.5s）；ddgs 首个可用后端在源码环境 2.9s
        出结果、而包内清单首位引擎已失效。因此把实践证明可用的排在前面，且只带一个备后端。

        `min_wait` 交给执行器做发请求前的可行下限判定（H1）：低于它就不发、不消耗额度。
        """
        from adapters.search_runner import provider_min_wait

        specs = [{"provider": "bing", "backend": "www.bing.com",
                  "min_wait": provider_min_wait("bing")}]
        name, _basis = self._ddg_backend()
        if name:
            specs.append({"provider": "ddgs", "backend": name,
                          "min_wait": provider_min_wait("ddgs")})
        # 冷却中的提供方跳过（跨任务的健康记忆；真零结果不进冷却，见 _mark_engine 调用处）
        return [s for s in specs
                if _engine_healthy("bing" if s["provider"] == "bing" else "ddg")]

    def _emit_payload(self, instruction: str, collected: list):
        """严格相关性优先；为空时放宽一档并如实记 warning（不放宽到"任意候选"）。"""
        seen: set = set()
        uniq: list = []
        for it in collected:
            u = str((it or {}).get("url") or "").strip()
            if u and u not in seen:
                seen.add(u)
                uniq.append(it)
        strict = self._filter_results(instruction, uniq)
        if strict:
            return json.dumps(strict[:10], ensure_ascii=False, indent=2)
        relaxed = self._filter_results(instruction, uniq, min_score=1)
        if relaxed:
            logger.warning("Strict filter empty; %d results kept with relaxed threshold",
                           len(relaxed))
            return json.dumps(relaxed[:10], ensure_ascii=False, indent=2)
        return None

    def _record_search_status(self, status: str, reason: str = "", *,
                              retryable: bool = False, attempts: int = 0,
                              queries=(), providers=(), elapsed: float = 0.0,
                              outcome=None, refused_calls: int = 0) -> dict:
        """把本次检索的**真值**记成结构化状态（P1-c②：状态跨层不丢）。

        为什么必须结构化：`_execute_bounded` 的四条出口（提供方冷却 / 预算不可用 /
        重试后仍空 / 真零命中）此前**都**返回 `json.dumps([])`，外层只看到"成功、空数组"，
        再由编排器反复判失败——"查询完成但零命中"与"根本没完成查询"在跨层时不可分。
        这里记的字段全部来自**真实执行**：

        - `attempts`：`run_search` 记的**真实发出的提供方调用数**（不是 dispatch 次数，
          也不是重试轮数——不凭四次 dispatch 推断四次外部请求）；
        - `queries`：实际送出去的查询串（`outcome.queries_tried`）；
        - `refused_calls`：因剩余时间低于可行下限而**没发出去**的请求数。

        兼容：`execute()` 的返回值**仍是 JSON 数组**（旧边界不动），结构化状态作为
        **兄弟字段**随 worker 结果消息一起上行（见 `_publish_result`）。
        """
        payload = {"status": str(status), "reason": str(reason or "")[:200],
                   "retryable": bool(retryable), "attempts": int(attempts or 0),
                   "queries": [str(q) for q in (queries or ()) if str(q or "").strip()],
                   "providers": [str(p) for p in (providers or ())],
                   "refused_calls": int(refused_calls or 0),
                   "elapsed": round(float(elapsed or 0.0), 2)}
        if outcome is not None:
            try:
                payload["outcome"] = outcome.as_dict()
            except Exception:                    # noqa: BLE001 - 状态记录失败不改处置
                pass
        self._search_status = payload
        return payload

    def _execute_bounded(self, instruction: str) -> str:
        """有界检索（S1）：一个预算、一条截止线、Bing 主 + 至多一个 ddgs 备后端。

        与旧实现（9 引擎 × 多变体 × 二轮 × 编排重试）的区别：
        - **不再 auto 全扫**：全失败不回落 `backend=None`（那会串行扫全部引擎）；
        - **不回炉两轮**：至多一次有界重试，且受同一截止线约束；
        - **真零结果 ≠ 后端故障**：零命中照实返回，不触发熔断；
        - 结果协议（status/attempts/elapsed/reason…）写进日志，对外仍是 JSON 数组（兼容层）。
        """
        from adapters.search_runner import run_search
        import re as _re_retry
        self._search_status = {}
        # **明确停**（P1-c②）：编排器说"重试计划已用尽"时必须停下来，不得回退到契约
        # 常规查询（上一批已证明打不出结果），更不得拿整段指令当查询。
        # 位置在发请求之前：停就是真的停，不消耗额度、不出网。
        _retry_lines = [q.strip() for q in
                        _re_retry.findall(r"\[重试检索查询\]\s*(.+)", str(instruction or ""))]
        try:
            from execution_contract import NO_NEW_QUERY_MARK
        except Exception:                        # noqa: BLE001
            NO_NEW_QUERY_MARK = "[无新查询]"
        if NO_NEW_QUERY_MARK in str(instruction or "") and not _retry_lines:
            logger.warning("结构化重试计划已用尽：本次不发检索请求，也不回退契约查询")
            self._record_search_status(
                "stopped_no_new_queries",
                "编排器已声明没有可用的新查询（结构化重试计划用尽）：不回退到上一批查询")
            return json.dumps([])
        self._load_active_strategy()
        variants = self._query_variants(instruction) or [instruction[:120]]
        specs = self._provider_specs()
        if not specs:
            # 全部提供方都在冷却期：不发新请求（没有新条件就不重复同类尝试，专项 §5）
            logger.warning("all search providers cooling down; no request issued")
            self._record_search_status(
                "providers_cooling", "全部提供方都在冷却期：本次未发出任何检索请求",
                retryable=True)
            return json.dumps([])
        # 任务级预算：**首次请求之前**原子预占次数并落下根起点（指令 §3.3）——
        # 预占失败即不发请求；剩余墙钟按根起点算，不由"首轮跑完的时刻"起算。
        total_calls, total_secs = self._search_ledger_limits()
        want = max(1, int(self._strategy_max_sources) + 1)
        granted, left_secs, why = self._reserve_search_calls(
            min(want, total_calls), total_calls=total_calls, total_secs=total_secs)
        if granted <= 0 or left_secs <= 0:
            logger.warning("任务级检索预算不可用（%s；额度 %d 次 / 剩余 %.0f 秒），本次不发请求",
                           why or "已用尽", granted, left_secs)
            self._record_search_status(
                "refused_budget",
                f"任务级检索预算不可用：{why or '已用尽'}（额度 {granted} 次 / 剩余 {left_secs:.0f} 秒）",
                queries=variants[:3])
            return json.dumps([])
        budget = self._search_budget(allowance=granted, wall_left=left_secs)
        max_results = max(1, int(self._strategy_max_sources))
        collected: list = []

        def _invoke(provider: str, backend: str, q: str, wait: float) -> list:
            """单提供方单后端调用（显式后端，绝不 auto）。

            剩余截止必须落到**请求自己的参数**上（H1）：Bing 走 `_search_bing(..., wait)`
            （内部按块读取、块间查截止），ddgs 走 `provider_timeout("ddgs", wait)`——
            两者都不再有固定 12/15 秒或 `max(3.0, …)` 的下限抬升。
            """
            from adapters.search_runner import provider_timeout
            if provider == "bing":
                return self._search_bing(q, timeout=wait)
            from ddgs import DDGS
            from adapters.search_runner import is_empty_result_error
            try:
                with DDGS(timeout=provider_timeout("ddgs", wait)) as ddgs:
                    rows = list(ddgs.text(q, backend=backend,
                                          max_results=max_results * 2))
            except Exception as exc:  # noqa: BLE001
                # "No results found." = 引擎跑完但零命中，不是后端故障：
                # 当异常抛出去会被记成 parse_error 并熔断（专项 §5）
                if is_empty_result_error(exc):
                    logger.info("ddgs %s：查询完成但零命中（不熔断）", backend)
                    return []
                raise
            out: list = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                title = str(r.get("title") or "").strip()
                href = str(r.get("href") or "").strip()
                if title and href.startswith("http"):
                    out.append({"title": title, "url": href,
                                "snippet": str(r.get("body") or ""),
                                "engine": f"duckduckgo:{backend}"})
            return out

        def _add(items) -> None:
            for it in items or []:
                if isinstance(it, dict) and str(it.get("url") or "").strip():
                    collected.append(it)

        outcome = run_search(variants, call_provider=_invoke, providers=specs,
                             budget=budget, max_results=max_results * 2)
        _add(outcome.items)
        passes = [outcome]
        logger.info("search outcome: %s", json.dumps(outcome.as_dict(), ensure_ascii=False))

        out = self._emit_payload(instruction, collected)
        if out:
            self._record_search_status(
                "ok", f"取得 {len(collected)} 条候选", attempts=outcome.attempts,
                queries=outcome.queries_tried,
                providers=[str(s.get("provider") or "") for s in specs],
                elapsed=getattr(outcome, "elapsed", 0.0), outcome=outcome,
                refused_calls=outcome.refused_calls)
            return self._finish_search(out, specs, collected, passes, budget)
        # 第二次尝试的**唯一**理由是"有还没打过的查询"。契约在时由契约生成结构化下一批
        # （主体/期间/文档类型/截止不变，只换资料面），并排除已打出去的 (提供方,后端,查询)
        # 组合——专项 §5："契约存在时不能再次提交相同 query/provider 组合"。无契约时沿用
        # 旧行为：只有暂时性失败才退避重打一次。
        contract = getattr(self, "_contract", None)
        retry_variants = self._retry_variants(contract, outcome)
        if retry_variants and not budget.expired():
            logger.warning("Search empty (%s); retrying with %d structured new queries",
                           outcome.status, len(retry_variants))
            outcome2 = run_search(retry_variants, call_provider=_invoke, providers=specs,
                                  budget=budget, max_results=max_results * 2,
                                  exclude_keys=outcome.tried_keys)
            _add(outcome2.items)
            passes.append(outcome2)
            logger.info("search retry outcome: %s",
                        json.dumps(outcome2.as_dict(), ensure_ascii=False))
            out = self._emit_payload(instruction, collected)
            if out:
                self._record_search_status(
                    "ok", f"重试后取得 {len(collected)} 条候选",
                    attempts=outcome.attempts + outcome2.attempts,
                    queries=list(outcome.queries_tried) + list(outcome2.queries_tried),
                    providers=[str(s.get("provider") or "") for s in specs],
                    elapsed=getattr(outcome, "elapsed", 0.0) + getattr(outcome2, "elapsed", 0.0),
                    outcome=outcome2,
                    refused_calls=outcome.refused_calls + outcome2.refused_calls)
                return self._finish_search(out, specs, collected, passes, budget)
        elif contract is None and outcome.retryable and not budget.expired():
            logger.warning("Search transient failure (%s); one bounded retry after %.0fs",
                           outcome.status, _SEARCH_RETRY_BACKOFF)
            time.sleep(min(_SEARCH_RETRY_BACKOFF, max(0.0, budget.time_left())))
            if not budget.expired():
                outcome2 = run_search(variants, call_provider=_invoke, providers=specs,
                                      budget=budget, max_results=max_results * 2,
                                      exclude_keys=outcome.tried_keys)
                _add(outcome2.items)
                passes.append(outcome2)
                logger.info("search retry outcome: %s",
                            json.dumps(outcome2.as_dict(), ensure_ascii=False))
                out = self._emit_payload(instruction, collected)
                if out:
                    self._record_search_status(
                        "ok", f"重试后取得 {len(collected)} 条候选",
                        attempts=outcome.attempts + outcome2.attempts,
                        queries=list(outcome.queries_tried) + list(outcome2.queries_tried),
                        providers=[str(s.get("provider") or "") for s in specs],
                        outcome=outcome2,
                        refused_calls=outcome.refused_calls + outcome2.refused_calls)
                    return self._finish_search(out, specs, collected, passes, budget)
        # 全部失败/无结果：诚实返回空列表（不再用 Mock 假数据）。
        # 空列表会被输出契约标记 → 编排器据此判定本步无可用来源（不再拖下游）。
        # **但"空"不是一种状态**（P1-c②）：零命中（no_results，查询完成）与
        # timeout/rate_limited/refused_calls（未完成）必须分开报，否则外层只能
        # 把两者都读成"成功但没结果"，再由编排反复判失败。
        logger.warning("Search empty (%s); engine health: %s",
                       outcome.status, get_engine_health())
        _st = str(outcome.status or "") or "no_results"
        _head = ("查询完成但零命中" if _st == "no_results"
                 else f"检索未完成：{_st}")
        _final = self._record_search_status(
            _st, _head + (f"；{outcome.reason}" if outcome.reason else ""),
            retryable=bool(outcome.retryable),
            attempts=outcome.attempts, queries=outcome.queries_tried,
            providers=[str(s.get("provider") or "") for s in specs],
            elapsed=getattr(outcome, "elapsed", 0.0), outcome=outcome,
            refused_calls=outcome.refused_calls)
        logger.info("search status payload: %s",
                    json.dumps(_final, ensure_ascii=False))
        return self._finish_search(json.dumps([]), specs, collected, passes, budget)

    def _finish_search(self, payload, specs, collected, passes, budget):
        """检索收尾：引擎健康 + 把**预占但没真正发出**的额度退回（指令 §3.3）。"""
        self._mark_search_health(specs, collected, passes)
        try:
            self._release_search_calls(max(0, int(budget.max_calls) - int(budget.used)))
        except Exception:                            # noqa: BLE001
            pass
        return payload

    def _retry_variants(self, contract, outcome) -> list:
        """下一批查询：契约生成的结构化重试计划；无契约返回空（不改查询纪律）。"""
        if contract is None:
            return []
        try:
            tried = sorted({str(q) for (_p, _b, q) in (outcome.tried_keys or ())})
            return list(contract.retry_queries(tried=tried))
        except Exception as exc:                     # noqa: BLE001
            logger.warning("结构化重试查询生成失败，按无重试处理：%s", str(exc)[:140])
            return []

    def _mark_search_health(self, specs, collected, passes) -> None:
        """引擎健康：任一轮成功即算可用；"每轮都失败"才记失败。

        为什么不能在首轮就判：单变体异常不得把引擎标成健康，但**真零结果也不得熔断**——
        零命中在 `run_search` 里是"完成"，不进错误表，所以这里只看错误表与产出。
        """
        for spec in specs:
            name = "bing" if spec["provider"] == "bing" else "ddg"
            errs = [p.errors.get(spec["provider"]) for p in passes
                    if p.errors.get(spec["provider"])]
            got = bool(collected) if spec["provider"] == "bing" else any(
                str(it.get("engine") or "").startswith("duckduckgo") for it in collected)
            _mark_engine(name, bool(got) or not errs)


    def _query_variants(self, instruction: str) -> list[str]:
        """生成多个查询变体（关键词组合优先 + 整句 + 定向模板 + 中英混合）。

        实现收敛到 `adapters.search_quality.build_query_variants(rich=True)`：
        整句截断、时效年份、财经/A 股定向模板与机构/公司 IR 定向变体原先写死在
        这里，与轻量检索的变体逻辑是两套；现在只有一套，上限与机构白名单可配置。

        **查询来源的优先级**（批次B）：
        1. 派发载荷里的执行契约（`self._contract`）——结构化字段，模型删不掉；
           按年度生成短查询，逐条作为变体；
        2. 指令里的 `[检索查询] …` 行（旧路径兼容）；
        3. 整段指令（兜底，仅在两者都没有时）。

        **例外**：指令里带 `[重试检索查询] …` 行时只打这一批（S1 补查重试）：那是编排器
        按同一契约生成的**结构化下一批**（换资料面，不动主体/期间/截止），回落到上一批
        已证明打不出结果的查询就等于原地重试。

        变体里出现契约外期间（如 2025三季报/2026）时**直接剔除**并记日志：那是
        历史提示/技能教训混进来的串，送给引擎只会污染候选（实机 ui-706c5ef4a5）。
        """
        from adapters.search_quality import build_query_variants
        import re as _re
        pol = self._search_policy()
        contract = getattr(self, "_contract", None)
        text = str(instruction or "")
        retry_lines = [q.strip() for q in _re.findall(r"\[重试检索查询\]\s*(.+)", text)]
        retry_lines = [q for q in retry_lines if q]
        sources: list[str] = []
        if retry_lines:
            logger.info("采用结构化重试查询 %d 条（上一批已证明无结果）", len(retry_lines))
            sources = retry_lines
        elif contract is not None:
            try:
                sources = [q for q in contract.queries() if q]
            except Exception as exc:                 # noqa: BLE001 - 契约异常退回文本路径
                logger.warning("执行契约查询生成失败，退回指令文本：%s", str(exc)[:120])
                sources = []
        m = None
        if not sources:
            m = _re.search(r"\[检索查询\]\s*(.+)", text)
            sources = [m.group(1).strip()] if (m and m.group(1).strip()) else []
        if not sources:
            sources = [text[:120]]

        out: list[str] = []
        for src in sources:
            # 契约短查询本身排最前（保持契约里的年度顺序）：变体列表的第一个会被用来
            # 探测存活引擎（`execute` 里 `qs.pop(0)`），干净查询必须先试
            out.append(src[:120])
            out.extend(build_query_variants(src, policy=pol, rich=True))
        # 去重且保序
        seen: set[str] = set()
        uniq = [v for v in out if v and not (v in seen or seen.add(v))]
        if contract is not None:
            uniq, dropped = self._drop_conflicting_queries(contract, uniq)
            if dropped:
                logger.warning("检索变体剔除契约外期间：%s", dropped[:3])
        return uniq or [sources[0][:120]]

    @staticmethod
    def _drop_conflicting_queries(contract, variants: list[str]) -> tuple[list[str], list[str]]:
        """剔除含契约外期间的变体（保留原文记录，不静默丢弃）。"""
        kept: list[str] = []
        dropped: list[str] = []
        for v in variants:
            try:
                hits = contract.conflicting_periods(v)
            except Exception:                        # noqa: BLE001
                hits = []
            if hits:
                dropped.append(f"{v[:60]}（{hits[:2]}）")
                continue
            kept.append(v)
        return kept, dropped

    def _filter_results(self, query: str, results: list[dict],
                        min_score: int | None = None) -> list[dict]:
        """按主题相关性过滤并排序搜索结果。

        实现收敛到 `adapters.search_quality.score_results`：中文按 2/3/4 字滑窗、
        英文按词边界计分，长连读要求、权威域加权与垃圾过滤都取自策略；
        已部署策略的 blocks/boosts（个性化域名黑/白名单）作为参数传入。
        """
        from adapters.search_quality import score_results
        pol = self._search_policy()
        return score_results(
            query, results,
            min_score=pol.min_score if min_score is None else min_score,
            policy=pol,
            blocks=tuple(getattr(self, "_strategy_blocks", ()) or ()),
            boosts=tuple(getattr(self, "_strategy_boosts", ()) or ()),
        )

    @staticmethod
    def _is_garbage_result(title: str, url: str, snip: str = "") -> bool:
        """通用垃圾识别（博彩/娱乐导航/下载站/假页）——实现取自策略共享模块。"""
        from adapters.search_quality import is_garbage_result
        return is_garbage_result(title, url, snip)

    def _search_bing(self, query: str, timeout=None) -> list[dict]:
        """备用搜索源：Bing HTML 结果解析（无需 API Key）。

        `timeout` 是本次请求自己的墙钟上限（调用方把剩余预算传进来）。旧的固定 15 秒
        在剩余预算更小时等于没有硬截止；现在按 `provider_timeout("bing", …)` 取
        `min(提供方上限, 剩余时间)`，并按块读取、块间查同一个截止线（H1）。
        """
        import re as _re
        import time as _time
        import urllib.parse
        import urllib.request

        from adapters.search_runner import provider_timeout, read_with_deadline

        secs = provider_timeout("bing", timeout)
        if secs <= 0:
            raise TimeoutError("no remaining budget for bing search")
        deadline = _time.monotonic() + secs
        url = "https://www.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=zh-hans"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120 Safari/537.36",
        })
        with urllib.request.urlopen(req, timeout=secs) as resp:
            html = read_with_deadline(resp, deadline).decode("utf-8", errors="replace")
        results: list[dict] = []
        for block in _re.findall(r'<li class="b_algo".*?</li>', html, _re.S)[:5]:
            m_title = _re.search(r'<h2[^>]*>(.*?)</h2>', block, _re.S)
            m_url = _re.search(r'<h2[^>]*>.*?<a[^>]+href="(https?://[^"]+)"', block, _re.S)
            if not m_title or not m_url:
                m_url = _re.search(r'<a[^>]+href="(https?://[^"]+)"', block)
                if not m_url:
                    continue
            m_snip = _re.search(r'<p[^>]*>(.*?)</p>', block, _re.S)
            strip = lambda s: _re.sub(r"<[^>]+>", "", s or "").strip()
            url = m_url.group(1)
            # 解码 Bing 的 /ck/a 跳转链接
            if "bing.com/ck/a" in url:
                q = urllib.parse.urlparse(url).query
                params = urllib.parse.parse_qs(q)
                target = params.get("u", [""])[0]
                if target:
                    try:
                        import base64
                        # Bing 跳转负载为无 padding 的 urlsafe base64，补足后再解
                        payload = target[2:]
                        payload += "=" * (-len(payload) % 4)
                        target = base64.urlsafe_b64decode(
                            payload.encode(),
                        ).decode("utf-8", errors="replace")
                    except Exception:
                        target = urllib.parse.unquote(target)
                if target.startswith("http"):
                    url = target
            results.append({
                "title": strip(m_title.group(1) if m_title else ""),
                "url": url,
                "snippet": strip(m_snip.group(1) if m_snip else ""),
            })
        return results

    # 检索入口：有界执行器（S1）。旧的多引擎探测 + `alive=None → auto` + 二轮重试
    # 已移除——实机"检索 9 分钟 / 12 查询 / 31 次尝试"就是它放大的（专项 §5、S0 证据）。
    execute = _execute_bounded


# ============================================================================
# 启动入口
# ============================================================================


def main() -> None:
    """启动 SearchAgent 的主入口。"""
    from logging_setup import setup_logging

    setup_logging("worker-search")

    # 从环境变量读取配置，提供合理的默认值
    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    db_path = db_paths.resolve_db_path()

    logger.info(
        "Starting SearchAgent (Redis: %s:%d, DB: %s)",
        redis_host,
        redis_port,
        db_path,
    )

    registry = AgentRegistry(db_path)
    messaging = MessagingClient(redis_host, redis_port)

    worker = SearchAgent(
        agent_id="search_agent",
        capabilities=["web_search"],
        registry=registry,
        messaging=messaging,
    )

    try:
        worker.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down...")
        worker.shutdown()
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        worker.shutdown()
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
