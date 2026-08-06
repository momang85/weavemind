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

    # 检索数量
    N_CONVERSATIONS = 3
    N_STRATEGIES = 1

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
            metadata={"description": "成功任务的完整规划路径"},
        )

        logger.info(
            "MemoryManager initialized at '%s'. "
            "conversations: %d docs, strategies: %d docs",
            persist_directory,
            self._conversations.count(),
            self._strategies.count(),
        )

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
            relevant = [
                doc for doc, dist in zip(strat_docs, strat_dist)
                if doc and doc.strip() and dist <= self._similarity_threshold
            ]
            if relevant:
                strategy = relevant[0].strip()
                parts.append(f"## 类似任务的成功解决路径，可供参考\n{strategy}")
        except Exception as exc:
            logger.warning("Failed to query strategies: %s", exc)

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
        logger.info(
            "Injected context for goal '%s': %d chars",
            current_goal[:40],
            len(context),
        )
        return context

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
    ) -> None:
        """任务成功执行后，将经验沉淀到记忆中。

        Args:
            goal: 用户原始目标。
            plan_steps: 各步骤的信息列表，每项含 capability, instruction, status。
            final_summary: 最终报告文本。
        """
        # 生成策略模式描述
        strategy_pattern = self._extract_strategy_pattern(goal, plan_steps, final_summary)

        # 存入 conversations 集合
        conversation_entry = f"目标: {goal}"
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

        # 存入 strategies 集合
        try:
            # 提取目标关键词（简单规则：取 goal 前100字符作为关键词）
            keywords = goal[:100]
            self._strategies.add(
                documents=[strategy_pattern],
                metadatas=[{
                    "goal_keywords": keywords,
                    "timestamp": _now_iso(),
                    "step_count": len(plan_steps),
                }],
                ids=[f"strat-{_now_epoch()}"],
            )
            logger.info("Strategy memory stored (%d chars)", len(strategy_pattern))
        except Exception as exc:
            logger.warning("Failed to store strategy: %s", exc)

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

    # ------------------------------------------------------------------
    # 统计信息
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        """返回记忆库统计信息。"""
        try:
            return {
                "conversations": self._conversations.count(),
                "strategies": self._strategies.count(),
            }
        except Exception as exc:
            logger.warning("Failed to get memory stats: %s", exc)
            return {"conversations": -1, "strategies": -1}

    def list_recent(self, collection, limit: int = 50) -> list[dict]:
        """导出某集合最近的文档（内容 + 元数据），用于可视化。"""
        try:
            res = collection.get(include=["documents", "metadatas"], limit=limit)
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            out = []
            for doc, meta in zip(docs, metas):
                out.append({"content": doc, "metadata": meta or {}})
            return out
        except Exception as exc:
            logger.warning("Failed to list memory docs: %s", exc)
            return []

    def list_conversations(self, limit: int = 50) -> list[dict]:
        return self.list_recent(self._conversations, limit)

    def list_strategies(self, limit: int = 50) -> list[dict]:
        return self.list_recent(self._strategies, limit)


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
