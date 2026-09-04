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

from common import AgentRegistry, MessagingClient

# ---------------------------------------------------------------------------
# 搜索引擎健康状态（对标 O-29：多引擎熔断 + 冷却 + 失败重试）
# ---------------------------------------------------------------------------
_ENGINE_HEALTH: dict[str, dict] = {}
_ENGINE_LOCK = threading.Lock()
_ENGINE_FAIL_THRESHOLD = 2
_ENGINE_COOLDOWN = float(os.environ.get("SEARCH_ENGINE_COOLDOWN", "120") or 120)
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
    """把引擎健康快照写入 Redis（TTL 120s），供 web_ui / metrics 跨进程读取。"""
    try:
        r = getattr(messaging, "redis", None) or getattr(messaging, "_redis", None)
        if r is None:
            return
        r.set(
            "search_engine_health",
            json.dumps(get_engine_health(), ensure_ascii=False),
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

    def _task_loop(self) -> None:
        """主任务循环：阻塞式拉取任务 → 执行 → 回传结果。"""
        logger.info("'%s' task loop started.", self.agent_id)
        while self._running and not self._shutting_down:
            task: dict[str, Any] | None = None
            try:
                # 阻塞等待任务
                task = self._messaging.pop_task(self.agent_id, timeout=self._POP_TIMEOUT)
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

                # 处理任务
                self._process_task(task)

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

        try:
            # 调用子类的 execute 方法
            result = self.execute(instruction)
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
        等包装词会混进查询词，导致搜索结果与主题无关）。"""
        import re as _re
        m = _re.search(r"用户目标：([^\n]+)", str(text))
        if m:
            return m.group(1).strip()
        t = _re.sub(r"^(任务目标|原始指令|用户目标)[：:]\s*", "", str(text).strip())
        # 信封（【角色】…）对搜索无意义，截断到信封之前
        idx = t.find("\n【角色】")
        if idx > 0:
            t = t[:idx]
        return t.strip()

    def _extract_keywords(self, text: str) -> str:
        """从用户目标中提取核心搜索词：去指令包装与停用词、保留年份、
        按停用词切分出完整词段（避免 2-4 字滑窗把"新能源汽车"拆成碎片）。"""
        import re as _re

        text = self._clean_search_text(text)
        # 年份单独保留："2026年" → "2026"
        text = _re.sub(r"(\d{4})年", r"\1 ", text)
        for w in self._ZH_STOP:
            text = text.replace(w, " ")
        zh = [
            s for s in _re.split(
                r"[\s\u3000，。、；：！？（）()【】《》\"'“”‘’,.…]+", text,
            )
            if _re.search(r"[\u4e00-\u9fff]", s) and len(s) >= 2
        ]
        en = [
            w.lower()
            for w in _re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", text)
            if w.lower() not in self._EN_STOP
        ]
        years = _re.findall(r"\d{4}", text)
        merged = list(dict.fromkeys(years + zh + en))
        return " ".join(merged)[:150]

    def _query_variants(self, instruction: str) -> list[str]:
        """生成多个查询变体（关键词组合优先 + 整句 + 中英混合），显著提升召回。

        关键词组合排首位：完整目标文本常含"生成一份董事会汇报：需包含…"等
        指令性文字，整句直发搜索引擎会被拒/超时（wikipedia 超时、mojeek 403
        的实测根因）。整句变体截断到 60 字符，并剥离指令尾段。
        """
        import re as _re
        import time

        goal = self._clean_search_text(instruction)
        kws = [k for k in self._extract_keywords(instruction).split() if k]
        variants: list[str] = []
        # 关键词组合优先（高召回、搜索引擎友好）；无关键词才回退整句
        if kws:
            variants.append(" ".join(kws)[:120])
        # 整句变体：截断 + 剥离"生成…汇报/需包含/请"等指令尾段
        if goal and len(goal) >= 4:
            # 在指令性标记处截断（生成/汇报/需包含/列出/注明/撰写/给出等）
            cut = _re.split(
                r"(生成|撰写|输出|需包含|请给出|列出|注明|要求|必须|包含[^，。]{0,10}图表|汇报[：:])",
                goal, maxsplit=1,
            )[0].strip()
            trimmed = (cut or goal)[:60]
            if trimmed not in variants:
                variants.append(trimmed)
            # 时效性（修复"最新财报"返回旧年份）：目标要求最新时补当前年份
            if any(k in instruction for k in ("最新", "最近", "latest", "current")):
                variants.append(f"{trimmed[:50]} {time.localtime().tm_year}")
            # 财报/财务类目标：引导结果页含具体数字（营收/净利润/亿元），
            # 否则 snippet 常只有叙事没有数值，清洗层无数据可洗
            if any(k in instruction for k in (
                "财报", "年报", "季报", "营收", "净利润", "负债", "财务", "业绩",
                "financial", "revenue", "earnings",
            )):
                variants.append(f"{trimmed[:50]} 年报 营收 净利润 亿元")
                variants.append(f"{trimmed[:50]} 财务数据 亿元")
            # 调研/研报类目标（市场规模/竞争格局/预测/份额/趋势）：追加权威
            # 机构定向查询（Gartner/IDC/TrendForce 等）。实测发现报告大量引用
            # 权威机构但搜索结果从未命中——源头是查询未定向，LLM 只能编造来源。
            # 机构白名单命中后按域名追加变体，让搜索引擎直接返回机构页面。
            if any(k in instruction for k in (
                "调研", "市场规模", "竞争格局", "预测", "趋势", "行业报告",
                "市场份额", "占比", "研报", "analysis", "forecast", "market size",
            )):
                for dom in ("gartner.com", "idc.com", "trendforce.com",
                            "statista.com", "counterpointresearch.com",
                            "canalys.com", "macrotrends.net"):
                    variants.append(f"{trimmed[:40]} site:{dom}")
                # 公司维度：追加官方财报/IR 页定向（营收/净利/出货量）
                # 公司关键词用完整指令判断（可能落在截断位置之后）
                if any(k in str(instruction).lower() for k in (
                    "英伟达", "nvidia", "amd", "英特尔", "intel",
                    "台积电", "tsmc", "苹果", "apple", "微软", "microsoft",
                )):
                    variants.append(f"{trimmed[:40]} 财报 营收 净利润 site:ir.nvidia.com site:investor.amd.com")
            # A股行情排行类目标：追加财经站点定向查询模板，并排除无关平台
            # （YouTube/百度百科/美股平台），避免通用搜索返回无关来源。
            if any(k in instruction for k in (
                "成交量排行", "成交额排行", "成交量前十", "成交额前十",
                "涨停", "跌幅榜", "a股今日", "今日a股", "前十股", "排名榜",
                "股票排行", "a股排行", "股票排名",
            )):
                metric = (
                    "成交额"
                    if "成交额" in instruction and "成交量" not in instruction
                    else "成交量"
                )
                variants.append(
                    f"今日 A股 {metric} 排行 前十 东方财富 "
                    "-site:youtube.com -site:baike.baidu.com"
                )
                variants.append(
                    f"{trimmed[:50]} {metric} 排行 site:eastmoney.com"
                )
                variants.append(
                    f"{trimmed[:50]} 东方财富 同花顺 新浪财经 雪球"
                )
        # 域名定向（ReAct 兜底）：指令含 site:xxx 时追加定向查询变体，
        # 让"官方 IR / SEC"类重检索指令真正落地
        for m in _re.finditer(r"site:\s*([a-zA-Z0-9.\-]+)", str(instruction)):
            dom = m.group(1).strip()
            variants.append(f"{goal[:110]} site:{dom}")
            if kws:
                variants.append(f"{' '.join(kws[:3])} site:{dom}")
        if kws:
            for i in range(1, min(len(kws), 5)):
                sub = " ".join(kws[: i + 1])
                if sub:
                    variants.append(sub[:120])
        en = [
            w.lower()
            for w in _re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", instruction)
            if w.lower() not in self._EN_STOP
        ]
        if en and kws:
            mix = " ".join(kws[:2] + en[:2])
            variants.append(mix[:120])
        seen: set[str] = set()
        out: list[str] = []
        for v in variants:
            if v and v not in seen:
                seen.add(v)
                out.append(v)
        # 上限 10：关键词 + 整句 + 7 个机构定向 + 公司 IR 定向
        # （旧上限 6 会挤掉 trendforce/公司 IR 等新增定向，白名单形同虚设）
        return out[:10]

    def _filter_results(self, query: str, results: list[dict], min_score: int = 2) -> list[dict]:
        """按主题相关性过滤并排序搜索结果：
        英文词按词边界匹配（避免 star 误中 Stars），中文按 2/3/4 字片段计分，
        得分不足的结果剔除，最终按相关度降序返回。"""
        import re as _re

        clean = self._clean_search_text(query)
        # 中文按 2/3/4 字滑窗生成匹配 token（findall 不会滑窗，长词串
        # 会变成单一巨型 token，导致真实结果几乎无法命中而被误过滤）
        tokens: set[str] = set()
        for run in _re.findall(r"[\u4e00-\u9fff]+", clean):
            for size in (4, 3, 2):
                if len(run) >= size:
                    tokens.update(run[i : i + size] for i in range(len(run) - size + 1))
        year_tokens = {y for y in _re.findall(r"\d{4}", clean)}
        en_tokens = {w.lower() for w in _re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", clean)}
        kept: list[tuple[int, dict]] = []
        for r in results:
            title = str(r.get("title") or "")
            url = str(r.get("url") or "")
            snip = str(r.get("snippet") or "")
            if not title or not url or not url.startswith("http"):
                continue
            if self._is_garbage_result(title, url, snip):
                logger.info("Search result filtered as garbage: %s | %s", title[:40], url[:60])
                continue
            if any(d in url for d in self._SPAM_DOMAINS):
                continue
            if title.lower().strip() in self._JUNK_TITLES:
                continue
            url_title = (url + " " + title).lower()
            if any(b in url_title for b in self._strategy_blocks):
                continue
            hay = title + " " + snip
            hay_lower = hay.lower()
            score = 0
            for tok in tokens:
                if tok in hay:
                    score += 1 if len(tok) == 2 else 2
            for y in year_tokens:
                if y in hay:
                    score += 1
            for tok in en_tokens:
                if len(tok) >= 3 and _re.search(
                    rf"(?<![a-z0-9]){_re.escape(tok)}(?![a-z0-9])", hay_lower
                ):
                    score += 2
            if any(b in url_title for b in self._strategy_boosts):
                score += 3
            if score < min_score:
                continue
            kept.append((score, r))
        kept.sort(key=lambda x: -x[0])
        return [r for _, r in kept]

    @staticmethod
    def _is_garbage_result(title: str, url: str, snip: str = "") -> bool:
        """通用垃圾识别：博彩/娱乐导航/下载站/假页。
        域名特征 + URL 路径特征 + 标题/摘要博彩词，任一命中即剔除。"""
        import re as _re

        u = str(url or "").lower()
        t = str(title or "").lower()
        s = str(snip or "").lower()
        if any(d in u for d in SearchAgent._LOW_AUTHORITY_DOMAINS):
            return True
        for pat in SearchAgent._JUNK_URL_PATTERNS:
            if _re.search(pat, u):
                return True
        if any(d in u for d in SearchAgent._SPAM_DOMAINS):
            return True
        hay = t + " " + s
        if any(k in hay for k in SearchAgent._GAMBLING_KEYWORDS):
            return True
        # 假页/空页
        if any(m in hay for m in ("404 not found", "page not found", "无法访问该页面",
                                  "页面不存在", "您访问的页面不存在")):
            return True
        return False

    def _search_bing(self, query: str) -> list[dict]:
        """备用搜索源：Bing HTML 结果解析（无需 API Key）。"""
        import re as _re
        import urllib.parse
        import urllib.request

        url = "https://www.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=zh-hans"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120 Safari/537.36",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
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
                        target = base64.urlsafe_b64decode(target[2:].encode()).decode("utf-8", errors="replace")
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

    def execute(self, instruction: str) -> str:
        """多查询变体 × 多源搜索：DuckDuckGo → Bing，合并去重，
        严格过滤为空时放宽阈值再试，全部源不可用才 mock。"""
        logger.info("SearchAgent searching: %s", instruction)
        self._load_active_strategy()
        variants = self._query_variants(instruction) or [instruction[:120]]
        collected: list[dict] = []
        seen_urls: set[str] = set()

        def _add(results) -> None:
            items = results if isinstance(results, list) else [results]
            for r in items:
                if not isinstance(r, dict):
                    continue
                u = str(r.get("url") or "")
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    collected.append(r)

        def _collect_ddg() -> tuple[bool, bool]:
            """返回 (是否新增结果, 是否出错)。"""
            added = False
            error = False
            try:
                from ddgs import DDGS
                with DDGS() as ddgs:
                    for q in variants:
                        try:
                            for r in ddgs.text(q, max_results=self._strategy_max_sources):
                                if isinstance(r, dict):
                                    _add({
                                        "title": r.get("title", ""),
                                        "url": r.get("href", ""),
                                        "snippet": r.get("body", ""),
                                    })
                                    added = True
                                else:
                                    logger.warning("DDG returned non-dict item: %r", str(r)[:80])
                        except Exception as e:
                            logger.warning("DDG query '%s' failed: %s", q[:40], e)
                            error = True
                return added, error
            except Exception as e:
                logger.warning("DuckDuckGo unavailable: %s", e)
                return added, True

        def _collect_bing() -> tuple[bool, bool]:
            added = False
            error = False
            for q in variants:
                try:
                    _add(self._search_bing(q))
                    added = True
                except Exception as e:
                    logger.warning("Bing query '%s' failed: %s", q[:40], e)
                    error = True
            return added, error

        def _run_pass() -> None:
            """跑一轮健康引擎（跳过冷却中引擎），并按结果更新健康状态。"""
            if _engine_healthy("ddg"):
                added, err = _collect_ddg()
                _mark_engine("ddg", added or not err)
            if _engine_healthy("bing"):
                added, err = _collect_bing()
                _mark_engine("bing", added or not err)

        def _emit() -> str | None:
            strict = self._filter_results(instruction, collected)
            if strict:
                return json.dumps(strict[:10], ensure_ascii=False, indent=2)
            relaxed = self._filter_results(instruction, collected, min_score=1)
            if relaxed:
                logger.warning(
                    "Strict filter empty; %d results kept with relaxed threshold",
                    len(relaxed),
                )
                return json.dumps(relaxed[:10], ensure_ascii=False, indent=2)
            return None

        _run_pass()
        out = _emit()
        if out:
            return out
        # 失败重试（对标 ReAct）：短暂退避后对健康引擎再跑一轮
        logger.warning(
            "Search first pass yielded nothing; retrying after %.0fs",
            _SEARCH_RETRY_BACKOFF,
        )
        time.sleep(_SEARCH_RETRY_BACKOFF)
        _run_pass()
        out = _emit()
        if out:
            return out
        # 全部失败/无结果：诚实返回空列表（不再用 Mock 假数据）。
        # 空列表会被输出契约标记 → 编排器带错误重试 → 反思驱动重检索。
        logger.warning("Search empty after retry; engine health: %s", get_engine_health())
        return json.dumps([])


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
    db_path = os.environ.get("REGISTRY_DB", "agents.db")

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
