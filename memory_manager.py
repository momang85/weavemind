"""
织光 (ZhiGuang) — 液态记忆管理器

基于 ChromaDB 的长期记忆系统：
- conversations: 历史对话摘要
- successful_strategies: 成功的任务规划路径

使用 SiliconFlow Embedding API 进行向量化。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
from typing import Any

import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 环境变量默认值
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")
EMBEDDING_BASE_URL = (
    os.environ.get("EMBEDDING_BASE_URL")
    or os.environ.get("LLM_BASE_URL")
    or "https://api.siliconflow.cn/v1"
)
EMBEDDING_API_KEY = (
    os.environ.get("EMBEDDING_API_KEY")
    or os.environ.get("LLM_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
    or ""
)


def _env_int(name: str, default: int) -> int:
    """安全读取整型环境变量（非法值回退默认）。"""
    try:
        return int(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """安全读取浮点环境变量（非法值回退默认）。"""
    try:
        return float(os.environ.get(name, ""))
    except (TypeError, ValueError):
        return default


# 记忆治理可配置项（均可被环境变量覆盖）
MEMORY_STRATEGY_TTL_DAYS = _env_int("MEMORY_STRATEGY_TTL_DAYS", 90)
MEMORY_CONVERSATIONS_MAX = _env_int("MEMORY_CONVERSATIONS_MAX", 2000)
MEMORY_STRATEGY_DEDUP_THRESHOLD = _env_float(
    "MEMORY_STRATEGY_DEDUP_THRESHOLD", 0.95
)
MEMORY_CONVERSATION_DEDUP_HOURS = _env_float(
    "MEMORY_CONVERSATION_DEDUP_HOURS", 24.0
)


# ============================================================================
# 自定义 Embedding 函数
# ============================================================================


class SiliconFlowEmbeddingFunction(EmbeddingFunction):
    """使用 SiliconFlow（兼容 OpenAI）Embedding API 进行文本向量化。

    API 格式: POST /v1/embeddings
    """

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model

    def __call__(self, input: Documents) -> Embeddings:
        """对一批文本进行 embedding。

        Args:
            input: 文本列表。

        Returns:
            向量列表，每个向量是一个 float 列表。
        """
        url = f"{self._base_url}/embeddings"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        body = json.dumps({
            "model": self._model,
            "input": input,
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Embedding API HTTP {exc.code}: {error_body[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Embedding API network error: {exc}") from exc

        # 提取所有向量
        embeddings: list[list[float]] = []
        for item in data.get("data", []):
            embeddings.append(item["embedding"])
        return embeddings


# ============================================================================
# 记忆管理器
# ============================================================================


class MemoryManager:
    """基于 ChromaDB 的长期记忆管理器。

    Usage:
        mem = MemoryManager("./chroma_memory")
        context = mem.inject_context("搜索最新AI论文")
        # ... 在任务成功后 ...
        mem.consolidate_memory(goal, plan_steps, final_summary)
    """

    # 集合名称
    COLLECTION_CONVERSATIONS = "conversations"
    COLLECTION_STRATEGIES = "successful_strategies"
    COLLECTION_PROMPT_REFINEMENTS = "prompt_refinements"

    # 检索数量
    N_CONVERSATIONS = 3
    N_STRATEGIES = 1
    N_REFINEMENTS = 3

    SIMILARITY_THRESHOLD = 0.6

    def __init__(self, persist_directory: str, similarity_threshold: float = 0.6) -> None:
        """初始化记忆管理器。

        Args:
            persist_directory: ChromaDB 持久化目录路径。
            similarity_threshold: 相似度阈值 (0-1)。余弦距离 < 此值才注入。降低可减少噪音。
        """
        self._similarity_threshold = similarity_threshold
        self._persist_directory = persist_directory

        # 初始化 embedding 函数
        self._embedding_fn = SiliconFlowEmbeddingFunction(
            api_key=EMBEDDING_API_KEY,
            base_url=EMBEDDING_BASE_URL,
            model=EMBEDDING_MODEL,
        )

        # 初始化 ChromaDB 客户端
        os.makedirs(persist_directory, exist_ok=True)
        self._client = chromadb.PersistentClient(path=persist_directory)

        # 创建或获取集合
        self._conversations = self._client.get_or_create_collection(
            name=self.COLLECTION_CONVERSATIONS,
            embedding_function=self._embedding_fn,
            metadata={"description": "历史对话摘要"},
        )
        self._strategies = self._client.get_or_create_collection(
            name=self.COLLECTION_STRATEGIES,
            embedding_function=self._embedding_fn,
            # 显式声明 HNSW 距离空间为 L2（Chroma 默认），
            # 去重/注入的距离阈值语义据此保持一致
            metadata={"description": "成功任务的完整规划路径", "hnsw:space": "l2"},
        )
        self._prompt_refinements = self._client.get_or_create_collection(
            name=self.COLLECTION_PROMPT_REFINEMENTS,
            embedding_function=self._embedding_fn,
            metadata={"description": "反思/自迭代产生的提示词改进记录（进化系统）"},
        )

        # 进程内观测计数器（命中率统计；Redis 持久化留待后续）
        self._injections = 0
        self._inject_hits = 0
        self._expired_purged = 0

        # 启动时惰性治理：清理过期策略 + 收敛对话上限（失败仅记日志，不影响启动）。
        # 改为后台线程执行：构造/请求路径绝不做全量 ChromaDB 遍历。
        self._stats_cache: dict[str, Any] = {
            "ts": 0.0,
            "data": {"conversations": 0, "strategies": 0},
        }
        self._stats_lock = threading.Lock()
        threading.Thread(target=self._startup_maintenance, daemon=True).start()
        logger.info("MemoryManager initialized at '%s'.", persist_directory)

    # ------------------------------------------------------------------
    # 记忆注入
    # ------------------------------------------------------------------

    def inject_context(self, current_goal: str) -> str:
        """根据当前目标检索相关记忆，生成上下文注入文本。

        从 conversations 中获取最相似的 N 条相关对话摘要，
        从 successful_strategies 中获取最相似的 1 条成功策略。

        Args:
            current_goal: 当前用户目标。

        Returns:
            格式化的上下文文本，可直接追加到 LLM 的 user_prompt 前面。
            如果无相关记忆，返回空字符串。
        """
        self._injections += 1
        parts: list[str] = []

        # 查询历史对话
        try:
            conv_results = self._conversations.query(
                query_texts=[current_goal],
                n_results=self.N_CONVERSATIONS,
                include=["documents", "metadatas", "distances"],
            )
            conv_docs = conv_results.get("documents", [[]])[0]
            conv_dist = conv_results.get("distances", [[]])[0]
            relevant = [
                doc for doc, dist in zip(conv_docs, conv_dist)
                if doc and doc.strip() and dist <= self._similarity_threshold
            ]
            if relevant:
                summaries = "\n".join(f"- {doc}" for doc in relevant)
                parts.append(f"## 与此任务相关的历史背景\n{summaries}")
        except Exception as exc:
            logger.warning("Failed to query conversations: %s", exc)

        # 查询成功策略
        try:
            strat_results = self._strategies.query(
                query_texts=[current_goal],
                n_results=self.N_STRATEGIES,
                include=["documents", "metadatas", "distances"],
            )
            strat_docs = strat_results.get("documents", [[]])[0]
            strat_dist = strat_results.get("distances", [[]])[0]
            strat_metas = strat_results.get("metadatas", [[]])[0]
            relevant = []
            for doc, dist, meta in zip(strat_docs, strat_dist, strat_metas):
                if not doc or not doc.strip() or dist > self._similarity_threshold:
                    continue
                if _is_expired(meta):
                    continue  # 过期策略不注入（惰性过滤）
                relevant.append(doc)
            if relevant:
                strategy = relevant[0].strip()
                parts.append(f"## 类似任务的成功解决路径，可供参考\n{strategy}")
        except Exception as exc:
            logger.warning("Failed to query strategies: %s", exc)

        # 查询历史提示词改进经验（进化系统产物）
        refinements = self.query_prompt_refinements(current_goal)
        if refinements:
            parts.append(
                "## 历史提示词改进经验（RAG，来自反思/自迭代）\n"
                + "\n".join(f"- {r}" for r in refinements)
            )

        if not parts:
            logger.info("No relevant memories found for goal: %s", current_goal[:60])
            return ""

        # 可选：LLM 二次相关性过滤（MEMORY_LLM_FILTER=1 时开启）
        if os.environ.get("MEMORY_LLM_FILTER", "0") == "1" and parts:
            filtered = []
            for part in parts:
                try:
                    if self._llm_relevance_check(current_goal, part):
                        filtered.append(part)
                except Exception:
                    filtered.append(part)
            parts = filtered
            if not parts:
                logger.info("LLM filter removed all memories for goal: %s", current_goal[:40])
                return ""

        context = "\n\n".join(parts)
        self._inject_hits += 1
        logger.info(
            "Injected context for goal '%s': %d chars",
            current_goal[:40],
            len(context),
        )
        return context

    # ------------------------------------------------------------------
    # 提示词改进记忆（反思 / 自迭代 → RAG → 反哺后续提示词）
    # ------------------------------------------------------------------

    def add_prompt_refinement(
        self,
        goal: str,
        key: str,
        issue: str,
        fix_prompt: str,
        rationale: str = "",
        task_id: str = "",
        version: int = 1,
        outcome: str = "applied",
    ) -> None:
        """沉淀一条提示词改进记录（反思结论或自迭代覆盖），供后续任务 RAG 检索。
        任何异常都不抛出（进化系统不能拖垮任务主线）。"""
        import uuid
        try:
            doc = (
                f"提示词进化记录（{key} v{version}）\n"
                f"任务目标：{goal[:300]}\n"
                f"问题/反思结论：{issue}\n"
                f"改进后的提示词（追加/覆盖）：{fix_prompt}\n"
                f"改进理由：{rationale}\n"
                f"结果：{outcome}"
            )
            self._prompt_refinements.add(
                ids=[f"prf-{task_id or 'x'}-{uuid.uuid4().hex[:8]}"],
                documents=[doc],
                metadatas=[{
                    "key": key,
                    "version": int(version or 1),
                    "outcome": outcome,
                    "task_id": str(task_id)[:40],
                    "created_at": _now_iso(),
                }],
            )
            logger.info("Prompt refinement recorded: %s v%s (task %s)", key, version, task_id)
        except Exception as exc:
            logger.warning("Failed to record prompt refinement: %s", str(exc)[:150])

    def query_prompt_refinements(
        self, current_goal: str, n: int = 3, threshold: float | None = None
    ) -> list[str]:
        """按目标检索相关提示词改进经验；返回格式化文本列表（已按相似度过滤）。"""
        try:
            thr = self._similarity_threshold if threshold is None else threshold
            res = self._prompt_refinements.query(
                query_texts=[current_goal],
                n_results=max(1, n),
                include=["documents", "metadatas", "distances"],
            )
            docs = res.get("documents", [[]])[0]
            dists = res.get("distances", [[]])[0]
            metas = res.get("metadatas", [[]])[0]
            out: list[str] = []
            for doc, dist, meta in zip(docs, dists, metas):
                if not doc or not doc.strip() or dist > thr:
                    continue
                key = (meta or {}).get("key", "?")
                out.append(f"[{key}] {doc[:500]}")
            return out
        except Exception as exc:
            logger.warning("Failed to query prompt refinements: %s", str(exc)[:120])
            return []

    # ------------------------------------------------------------------
    # 记忆沉淀
    # ------------------------------------------------------------------


    def _llm_relevance_check(self, goal: str, context: str) -> bool:
        """LLM 二次过滤：判断检索记忆是否真正相关。

        Args:
            goal: 当前用户目标。
            context: 检索到的上下文。

        Returns:
            True 表示相关，可以注入。
        """
        try:
            from llm_client import call_llm
            system = (
                "你是一个相关性判断助手。判断以下历史记忆是否与用户当前目标真正相关。"
                "如果历史记忆能提供有用的参考，回复 YES。如果完全无关或可能误导，回复 NO。"
                "只回复 YES 或 NO。"
            )
            nl = chr(10)
            user = f"用户目标: {goal}{nl}{nl}历史记忆:{nl}{context}{nl}{nl}这些记忆是否与用户目标相关？"
            result = call_llm(system, user, expect_json=False)
            answer = result.get("content", "NO").strip().upper()
            return answer.startswith("YES")
        except Exception as exc:
            logger.warning("LLM relevance check failed, defaulting to pass: %s", exc)
            return True  # 降级：LLM 不可用时默认通过
    def consolidate_memory(
        self,
        goal: str,
        plan_steps: list[dict[str, Any]],
        final_summary: str,
        acceptance_summary: dict | None = None,
        task_id: str = "",
    ) -> None:
        """任务成功执行后，将经验沉淀到记忆中。

        Args:
            goal: 用户原始目标。
            plan_steps: 各步骤的信息列表，每项含 capability, instruction, status。
            final_summary: 最终报告文本。
            acceptance_summary: 验收摘要（{overall, gaps}）。验收 fail 时只沉淀
                对话（追溯用），跳过策略沉淀，避免空壳报告污染 successful_strategies。
            task_id: 任务 ID（写入策略元数据，便于追溯）。
        """
        # 生成策略模式描述
        strategy_pattern = self._extract_strategy_pattern(goal, plan_steps, final_summary)

        # 存入 conversations 集合（验收 fail 也记录对话；24h 内同目标去重）
        conversation_entry = f"目标: {goal}"
        if self._find_recent_conversation(goal):
            logger.info(
                "Conversation duplicate within %.0fh, skipped: %s",
                MEMORY_CONVERSATION_DEDUP_HOURS, goal[:60],
            )
        else:
            try:
                self._conversations.add(
                    documents=[conversation_entry],
                    metadatas=[{
                        "timestamp": _now_iso(),
                        "goal": goal[:200],
                    }],
                    ids=[f"conv-{_now_epoch()}"],
                )
                logger.info("Conversation memory stored: %s", goal[:60])
            except Exception as exc:
                logger.warning("Failed to store conversation: %s", exc)

        # 对话上限治理（超出后删最旧 10%）
        self.enforce_conversation_cap()

        # P0 验收准入：存在验收报告且 overall != pass → 跳过策略沉淀
        if acceptance_summary is not None and (
            acceptance_summary.get("overall") or ""
        ) != "pass":
            logger.warning(
                "Acceptance failed (overall=%s), skip strategy consolidation: %s",
                (acceptance_summary or {}).get("overall"), goal[:60],
            )
            return

        # 存入 strategies 集合（P1 去重：相似策略更新而非新增）
        try:
            # 提取目标关键词（简单规则：取 goal 前100字符作为关键词）
            keywords = goal[:100]
            now_iso = _now_iso()
            new_meta = {
                "goal_keywords": keywords,
                "timestamp": now_iso,
                "step_count": len(plan_steps),
                "task_id": str(task_id)[:40],
                "expires_at": _expires_at_iso(),
            }
            existing_id = self._find_similar_strategy(
                strategy_pattern, threshold=MEMORY_STRATEGY_DEDUP_THRESHOLD
            )
            if existing_id:
                self._update_strategy_metadata(existing_id, new_meta)
                logger.info(
                    "Similar strategy %s found, refreshed instead of inserting: %s",
                    existing_id, goal[:60],
                )
            else:
                self._strategies.add(
                    documents=[strategy_pattern],
                    metadatas=[new_meta],
                    ids=[f"strat-{_now_epoch()}"],
                )
                logger.info("Strategy memory stored (%d chars)", len(strategy_pattern))
        except Exception as exc:
            logger.warning("Failed to store strategy: %s", exc)

    def add_note(self, goal: str, note: str, key: str = "") -> None:
        """主动记忆（对标标准 3.4 agent_control）：反思/规划器决定记录的经验。"""
        import uuid
        try:
            self._conversations.add(
                documents=[f"主动经验：{note}"],
                metadatas=[{
                    "timestamp": _now_iso(),
                    "goal": str(key or goal)[:200],
                    "note": True,
                }],
                ids=[f"note-{_now_epoch()}-{uuid.uuid4().hex[:6]}"],
            )
            logger.info("Active memory note stored: %s", str(note)[:60])
        except Exception as exc:
            logger.warning("Failed to store active memory note: %s", str(exc)[:120])

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _extract_strategy_pattern(
        self,
        goal: str,
        plan_steps: list[dict[str, Any]],
        final_summary: str,
    ) -> str:
        """从执行结果中提取通用的策略模式描述。

        不依赖 LLM，基于步骤信息规则生成简洁摘要。

        Args:
            goal: 原始目标。
            plan_steps: 步骤信息。
            final_summary: 最终报告。

        Returns:
            策略模式描述字符串。
        """
        # 规则化提取：直接用步骤序列描述
        steps_desc = []
        for s in plan_steps:
            capability = s.get("capability", "unknown")
            instruction = s.get("instruction", "")[:80]
            status = s.get("status", "unknown")
            steps_desc.append(f"  [{capability}] {instruction} → {status}")

        pattern = (
            f"任务目标: {goal}\n"
            f"执行步骤:\n"
            + "\n".join(steps_desc)
        )

        # 如果最终报告存在，截取前200字作为成果摘要
        if final_summary:
            summary_snippet = final_summary[:300].replace("\n", " ")
            pattern += f"\n成果: {summary_snippet}"

        # 保持在 500 字以内
        if len(pattern) > 500:
            pattern = pattern[:497] + "..."

        return pattern

    def _find_recent_conversation(self, goal: str, hours: float | None = None) -> bool:
        """对话去重：同 goal 在最近 N 小时内已存在则返回 True（防连跑污染）。"""
        hours = MEMORY_CONVERSATION_DEDUP_HOURS if hours is None else hours
        try:
            res = self._conversations.get(
                where={"goal": goal[:200]}, include=["metadatas"],
            )
            metas = res.get("metadatas") or []
            now_ts = time.time()
            for m in metas:
                ts = _parse_iso((m or {}).get("timestamp", ""))
                if ts is not None and now_ts - ts <= hours * 3600:
                    return True
        except Exception as exc:
            logger.warning("Conversation dedup check failed: %s", str(exc)[:120])
        return False

    def _find_similar_strategy(
        self, pattern: str, threshold: float = 0.95,
    ) -> str | None:
        """策略向量去重：查询最相似策略，L2 距离 ≤ threshold 视为重复。

        距离语义说明：Chroma 默认 HNSW 距离空间为 L2（完全一致≈0，越小越相似），
        因此直接用「距离 ≤ 阈值」判定稳定可测，无需再归一化。
        空库或查询异常返回 None（降级为新增）。
        """
        try:
            res = self._strategies.query(
                query_texts=[pattern],
                n_results=1,
                include=["distances"],
            )
            dists = res.get("distances", [[]])[0] or []
            ids = res.get("ids", [[]])[0] or []
            if not dists or not ids:
                return None
            distance = float(dists[0])
            if distance <= threshold:
                return str(ids[0])
            return None
        except Exception as exc:
            logger.warning("Strategy similarity check failed: %s", str(exc)[:120])
            return None

    def _update_strategy_metadata(self, strategy_id: str, new_meta: dict) -> bool:
        """合并更新既有策略元数据（保留旧字段，刷新 timestamp/expires_at 等）。"""
        try:
            res = self._strategies.get(ids=[strategy_id], include=["metadatas"])
            old = ((res or {}).get("metadatas") or [{}])[0] or {}
            merged = dict(old or {})
            merged.update(new_meta)
            self._strategies.update(ids=[strategy_id], metadatas=[merged])
            return True
        except Exception as exc:
            logger.warning("Strategy metadata update failed: %s", str(exc)[:120])
            return False

    def purge_expired(self) -> int:
        """批量清理过期策略；返回删除条数并累计到 expired_purged 观测计数。"""
        try:
            res = self._strategies.get(include=["metadatas"])
            ids = res.get("ids") or []
            metas = res.get("metadatas") or []
            expired = [
                doc_id for doc_id, meta in zip(ids, metas) if _is_expired(meta)
            ]
            if not expired:
                return 0
            deleted = self.delete_by_ids(self._strategies, expired)
            self._expired_purged += deleted
            if deleted:
                logger.info("Purged %d expired strategies", deleted)
            return deleted
        except Exception as exc:
            logger.warning("Expired strategy purge failed: %s", str(exc)[:120])
            return 0

    def enforce_conversation_cap(self, max_count: int | None = None) -> int:
        """对话超上限治理：按 timestamp 升序删除最旧 10%（至少 1 条）。"""
        try:
            limit = MEMORY_CONVERSATIONS_MAX if max_count is None else max_count
            total = self._conversations.count()
            if total <= limit:
                return 0
            res = self._conversations.get(include=["metadatas"])
            ids = res.get("ids") or []
            metas = res.get("metadatas") or []
            items = list(zip(ids, metas))

            def _sort_key(item: tuple) -> float:
                ts = _parse_iso((item[1] or {}).get("timestamp", ""))
                # 无时间戳视为最旧，优先清理
                return ts if ts is not None else -1.0

            items.sort(key=_sort_key)
            n_delete = max(1, int((len(items) or total) * 0.1))
            oldest_ids = [item[0] for item in items[:n_delete]]
            deleted = self.delete_by_ids(self._conversations, oldest_ids)
            if deleted:
                logger.info(
                    "Conversation cap: %d > %d, deleted %d oldest",
                    total, limit, deleted,
                )
            return deleted
        except Exception as exc:
            logger.warning("Conversation cap enforcement failed: %s", str(exc)[:120])
            return 0

    # ------------------------------------------------------------------
    # 统计信息
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """返回记忆库统计信息。"""
        cached = getattr(self, "_stats_cache", None)
        now = time.time()
        if cached is not None and now - cached.get("ts", 0) < 60:
            return dict(cached.get("data", {"conversations": 0, "strategies": 0}))
        try:
            data = {
                "conversations": self._conversations.count(),
                "strategies": self._strategies.count(),
            }
        except Exception as exc:
            logger.warning("Failed to get memory stats: %s", exc)
            data = {"conversations": -1, "strategies": -1}
        if cached is None:
            self._stats_cache = {"ts": now, "data": data}
        else:
            with getattr(self, "_stats_lock", threading.Lock()):
                cached["ts"] = now
                cached["data"] = data
        return data

    def memory_health(self, counts: dict[str, int] | None = None) -> dict[str, Any]:
        """返回记忆健康度观测：注入/命中计数、命中率、集合规模、过期清理累计。

        命中定义：inject_context 返回非空上下文（检索到至少一条相关记忆）。
        计数为进程内累计，随服务重启归零（Redis 持久化留待后续）。
        counts 可由上层传入已缓存的 stats（如 web_ui 的 60s 统计缓存），
        避免 memory_health 每次都在请求路径重新 count Chroma。
        """
        stats = self.stats() if counts is None else counts
        injections = max(0, self._injections)
        hits = max(0, self._inject_hits)
        return {
            "injections": injections,
            "hits": hits,
            "hit_rate": round(hits / injections, 4) if injections else 0.0,
            "strategy_count": int(stats.get("strategies", 0) or 0),
            "conversation_count": int(stats.get("conversations", 0) or 0),
            "expired_purged": max(0, self._expired_purged),
        }

    def _startup_maintenance(self) -> None:
        """后台执行启动维护：过期清理 + 对话上限收敛 + 预热统计缓存。"""
        try:
            self.purge_expired()
            self.enforce_conversation_cap()
            stats = {
                "conversations": self._conversations.count(),
                "strategies": self._strategies.count(),
            }
            with self._stats_lock:
                self._stats_cache = {"ts": time.time(), "data": stats}
            logger.info(
                "Memory maintenance done: conversations: %d docs, strategies: %d docs",
                stats["conversations"], stats["strategies"],
            )
        except Exception as exc:
            logger.warning("Lazy memory cleanup at startup failed: %s", str(exc)[:120])

    def list_recent(self, collection, limit: int = 50) -> list[dict]:
        """导出某集合最近的文档（内容 + 元数据），用于可视化。"""
        try:
            # Chroma 的 get 恒返回 ids，include 只允许 documents/metadatas 等
            res = collection.get(include=["documents", "metadatas"], limit=limit)
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            ids = res.get("ids") or []
            out = []
            for doc, meta, doc_id in zip(docs, metas, ids):
                out.append({"id": doc_id, "content": doc, "metadata": meta or {}})
            return out
        except Exception as exc:
            logger.warning("Failed to list memory docs: %s", exc)
            return []

    def list_conversations(self, limit: int = 50) -> list[dict]:
        return self.list_recent(self._conversations, limit)

    def list_strategies(self, limit: int = 50) -> list[dict]:
        return self.list_recent(self._strategies, limit)

    # ------------------------------------------------------------------
    # 记忆治理（对标标准 3.4：持续治理）
    # ------------------------------------------------------------------

    def delete_by_ids(self, collection, ids: list[str]) -> int:
        """按 id 删除记忆条目。返回删除数量。"""
        try:
            if not ids:
                return 0
            collection.delete(ids=ids)
            return len(ids)
        except Exception as exc:
            logger.warning("Memory delete failed: %s", str(exc)[:120])
            return 0

    def delete_where(self, collection, where: dict) -> int:
        """按元数据条件删除（如 {"key": "step:code_execution"}）。"""
        try:
            res = collection.get(where=where)
            ids = (res or {}).get("ids") or []
            return self.delete_by_ids(collection, ids)
        except Exception as exc:
            logger.warning("Memory delete_where failed: %s", str(exc)[:120])
            return 0

    def delete_all(self, collection) -> int:
        try:
            res = collection.get()
            ids = (res or {}).get("ids") or []
            return self.delete_by_ids(collection, ids)
        except Exception as exc:
            logger.warning("Memory delete_all failed: %s", str(exc)[:120])
            return 0

    def delete_conversations(self, ids: list[str]) -> int:
        return self.delete_by_ids(self._conversations, ids)

    def delete_strategies(self, ids: list[str]) -> int:
        return self.delete_by_ids(self._strategies, ids)

    def delete_prompt_refinements(self, where: dict | None = None) -> int:
        if where:
            return self.delete_where(self._prompt_refinements, where)
        try:
            res = self._prompt_refinements.get()
            return self.delete_by_ids(self._prompt_refinements, (res or {}).get("ids") or [])
        except Exception as exc:
            logger.warning("Prompt refinement purge failed: %s", str(exc)[:120])
            return 0


# ============================================================================
# 辅助函数
# ============================================================================


def _now_iso() -> str:
    """返回当前 UTC 时间 ISO 格式字符串。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> str:
    """返回当前时间戳（秒），作为唯一 ID 后缀。"""
    import time
    return str(int(time.time() * 1000))


def _expires_at_iso(days: int | None = None) -> str:
    """返回策略过期时间（默认 MEMORY_STRATEGY_TTL_DAYS 天后的 UTC ISO）。"""
    from datetime import datetime, timedelta, timezone
    days = MEMORY_STRATEGY_TTL_DAYS if days is None else int(days)
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def _parse_iso(value: Any) -> float | None:
    """解析 ISO 时间戳为 epoch 秒；解析失败返回 None。"""
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _is_expired(meta: dict | None, now_ts: float | None = None) -> bool:
    """判断元数据是否已过期（无 expires_at 字段视为未过期，兼容旧数据）。"""
    expires_at = (meta or {}).get("expires_at")
    if not expires_at:
        return False
    ts = _parse_iso(expires_at)
    if ts is None:
        return False
    if now_ts is None:
        now_ts = time.time()
    return ts <= now_ts
