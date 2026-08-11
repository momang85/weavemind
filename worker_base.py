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

    _SPAM_DOMAINS = ("susmeat.com", "aydvjch.cc", "example.com")
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
    }
    _EN_STOP = {
        "the", "and", "with", "from", "for", "that", "this", "not",
        "are", "was", "were", "output", "only", "json", "using", "your",
    }
    # 已部署策略（人工审批后写入 strategy:active:search_agent，每次执行时刷新）
    _strategy_max_sources = 5
    _strategy_blocks: list[str] = []
    _strategy_boosts: list[str] = []

    def _load_active_strategy(self) -> None:
        """读取已部署策略并解析为过滤规则：排除词（黑名单）与优先词（排序加分）。"""
        self._strategy_max_sources = 5
        self._strategy_blocks = []
        self._strategy_boosts = []
        try:
            raw = self._messaging.redis.get("strategy:active:search_agent")
            if not raw:
                return
            data = json.loads(raw)
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
                "Active strategy applied: max_sources=%d blocks=%s boosts=%s",
                self._strategy_max_sources, self._strategy_blocks, self._strategy_boosts,
            )
        except Exception as exc:
            logger.warning("Failed to load active strategy: %s", exc)

    @staticmethod
    def _clean_search_text(text: str) -> str:
        """去掉指令包装，取"用户目标"作为查询基础（否则"任务目标/用户目标/原始指令"
        等包装词会混进查询词，导致搜索结果与主题无关）。"""
        import re as _re
        m = _re.search(r"用户目标：([^\n]+)", str(text))
        if m:
            return m.group(1).strip()
        return _re.sub(r"^(任务目标|原始指令|用户目标)[：:]\s*", "", str(text).strip())

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
        """生成多个查询变体（整句 + 关键词组合 + 中英混合），显著提升召回。"""
        import re as _re

        goal = self._clean_search_text(instruction)
        kws = [k for k in self._extract_keywords(instruction).split() if k]
        variants: list[str] = []
        if goal and len(goal) >= 4:
            variants.append(goal[:120])
        if kws:
            variants.append(" ".join(kws)[:120])
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
        return out[:6]

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

        def _collect_ddg() -> bool:
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
                                else:
                                    logger.warning("DDG returned non-dict item: %r", str(r)[:80])
                        except Exception as e:
                            logger.warning("DDG query '%s' failed: %s", q[:40], e)
                return True
            except Exception as e:
                logger.warning("DuckDuckGo unavailable: %s", e)
                return False

        def _collect_bing() -> bool:
            ok = False
            for q in variants:
                try:
                    _add(self._search_bing(q))
                    ok = True
                except Exception as e:
                    logger.warning("Bing query '%s' failed: %s", q[:40], e)
            return ok

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

        source_ok = _collect_ddg()
        out = _emit()
        if out:
            return out
        if _collect_bing():
            source_ok = True
        out = _emit()
        if out:
            return out
        # 两个来源均失败：mock 兜底；有返回但全部被过滤：返回空列表（诚实说明未找到相关资料）
        if not source_ok:
            return json.dumps([
                {"title": f"Mock: {instruction}", "url": "https://example.com/mock",
                 "snippet": "搜索源不可用"}
            ], ensure_ascii=False)
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
