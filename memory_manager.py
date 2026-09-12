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
import re
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


def _config_memory_value(key: str, cast, default):
    """读取 config.json 的 memory.* 设置（设置页有这一项就必须生效）。

    优先级：环境变量 > config.json memory.<key> > 默认值。此前策略去重阈值等
    参数只认环境变量，设置页里改了不生效——页面标着"可调"却不接线，属于
    欺骗性 UI。
    """
    env_name = f"MEMORY_{str(key).upper()}"
    if os.environ.get(env_name) not in (None, ""):
        try:
            return cast(os.environ[env_name])
        except Exception:
            return default
    try:
        cfg = _load_config_file() or {}
        val = (cfg.get("memory") or {}).get(key)
        if val not in (None, ""):
            return cast(val)
    except Exception:
        pass
    return default


def _load_config_file() -> dict:
    """读 config.json（失败返回空 dict；只用于读取治理参数）。"""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


# 记忆待补录队列：embedding 不可用期间写入失败的内容先排队，恢复后回填
MEM_PENDING_KEY = "mem_pending:docs"
MEM_PENDING_MAX = 200


def _read_memory_config() -> None:
    """把 config.json 的 memory.* 应用到模块级常量（进程启动时调用一次）。"""
    global MEMORY_STRATEGY_TTL_DAYS, MEMORY_CONVERSATIONS_MAX
    global MEMORY_STRATEGY_DEDUP_THRESHOLD, MEMORY_CONVERSATION_DEDUP_HOURS
    MEMORY_STRATEGY_TTL_DAYS = _config_memory_value(
        "strategy_ttl_days", int, MEMORY_STRATEGY_TTL_DAYS)
    MEMORY_CONVERSATIONS_MAX = _config_memory_value(
        "conversations_max", int, MEMORY_CONVERSATIONS_MAX)
    MEMORY_STRATEGY_DEDUP_THRESHOLD = _config_memory_value(
        "strategy_dedup_threshold", float, MEMORY_STRATEGY_DEDUP_THRESHOLD)
    MEMORY_CONVERSATION_DEDUP_HOURS = _config_memory_value(
        "conversation_dedup_hours", float, MEMORY_CONVERSATION_DEDUP_HOURS)


def _embedding_degraded() -> bool:
    """embedding 是否已进入降级（连续失败超阈值/欠费）。"""
    try:
        import embed_health
        return bool(embed_health.embedding_health().get("degraded"))
    except Exception:
        return False


_LITERAL_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}")


def _literal_tokens(text: str, limit: int = 6) -> list[str]:
    """检索用切词：优先 jieba（已在依赖内），缺失时退化为中文 2 字滑窗+字母词。"""
    t = str(text or "")
    toks: list[str] = []
    try:
        import jieba
        toks = [w.strip() for w in jieba.cut(t)]
    except Exception:
        toks = _LITERAL_TOKEN_RE.findall(t)
    seen: set[str] = set()
    out: list[str] = []
    for w in toks:
        if len(w) < 2 or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:limit]


def _pending_redis():
    """待补录队列的 Redis 客户端（不可用返回 None）。"""
    try:
        import redis
        client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379") or 6379),
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
        )
        client.ping()
        return client
    except Exception:
        return None


# 启动时把设置页的 memory.* 接到模块常量（环境变量仍优先）
_read_memory_config()



# ============================================================================
# 自定义 Embedding 函数
# ============================================================================


def _note_embed_fail(error: str) -> None:
    """旁路记录 Embedding 失败（供 Health 呈现降级）；任何异常都不得外溢。"""
    try:
        import embed_health
        embed_health.record_embed_fail(error)
    except Exception:
        pass


def _note_embed_ok() -> None:
    try:
        import embed_health
        embed_health.record_embed_ok()
    except Exception:
        pass


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
            _note_embed_fail(f"HTTP {exc.code}: {error_body[:200]}")
            raise RuntimeError(f"Embedding API HTTP {exc.code}: {error_body[:500]}") from exc
        except urllib.error.URLError as exc:
            _note_embed_fail(f"network error: {exc}")
            raise RuntimeError(f"Embedding API network error: {exc}") from exc

        # 提取所有向量
        embeddings: list[list[float]] = []
        for item in data.get("data", []):
            embeddings.append(item["embedding"])
        _note_embed_ok()
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

    # 观测计数器默认值（类级）：测试桩常用 __new__ 绕过 __init__ 构造，
    # 计数读取必须对这些实例仍可用（否则 memory_health 直接抛 AttributeError）
    _injections = 0
    _inject_hits = 0
    _expired_purged = 0
    _degraded_queries = 0
    _degraded_hits = 0
    _last_degraded = False

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
        # 降级观测：字面兜底的查询数与命中数（供 Health/记忆页区分口径）
        self._degraded_queries = 0
        self._degraded_hits = 0
        self._last_degraded = False

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

    def _retrieval_mode(self) -> str:
        """当前检索通道：vector / literal / unavailable（供 Health 与前端口径）。"""
        if _embedding_degraded():
            return "literal"
        if not self._injections:
            return "unavailable"
        return "vector"

    def _literal_search(self, collection, query: str, n: int) -> list[dict]:
        """无向量兜底检索：用词面命中代替语义相似度。

        embedding 欠费/降级时 `collection.query` 直接抛错，读路径会全空——而经验
        其实还在库里，界面上"命中率 0%"会被误读成"没有经验"。这里按分词 token 走
        `where_document={"$contains": tok}`（与向量无关的读取路径），按命中词数
        加权排序；结果标 degraded=True，让上层与前端能区分口径。
        """
        tokens = _literal_tokens(query)
        if not tokens:
            return []
        scored: dict[str, dict] = {}
        for tok in tokens:
            try:
                res = collection.get(
                    where_document={"$contains": tok},
                    include=["documents", "metadatas"],
                )
            except Exception as exc:
                logger.debug("字面检索 token=%r 失败: %s", tok, str(exc)[:80])
                continue
            ids = res.get("ids") or []
            docs = res.get("documents") or []
            metas = res.get("metadatas") or []
            for doc_id, doc, meta in zip(ids, docs, metas):
                if not doc or not str(doc).strip():
                    continue
                key = str(doc_id)
                entry = scored.get(key)
                if entry is None:
                    scored[key] = {"id": doc_id, "document": str(doc),
                                   "metadata": meta or {}, "score": 0}
                    entry = scored[key]
                entry["score"] += 1
        ranked = sorted(
            scored.values(),
            key=lambda e: (-int(e["score"]), -len(_literal_tokens(e["document"], 99))),
        )
        out: list[dict] = []
        for entry in ranked:
            # 过期策略不注入（与向量路径同一条惰性过滤规则）
            if collection is self._strategies and _is_expired(entry["metadata"]):
                continue
            entry["degraded"] = True
            out.append(entry)
            if len(out) >= max(1, n):
                break
        return out

    def _recall(
        self, collection, goal: str, n: int,
        threshold: float | None = None, filter_expired: bool = False,
    ) -> tuple[list[str], bool]:
        """检索记忆文档，返回 (documents, degraded)。

        向量通道异常时**再退一次字面兜底**（而不是直接空手而归）：embedding 故障
        往往表现为超时/报错而非提前置位降级标记，等标记生效前的那批任务不该白丢经验。
        """
        thr = self._similarity_threshold if threshold is None else threshold
        literal_rows: list[dict] = []
        if _embedding_degraded():
            # 降级时字面优先：欠费状态下向量请求必然失败，先走不通网的那条
            literal_rows = self._literal_search(collection, goal, n)
            if literal_rows:
                self._degraded_queries += 1
                self._degraded_hits += 1
                return [r["document"] for r in literal_rows], True
        try:
            res = collection.query(
                query_texts=[goal], n_results=max(1, n),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            logger.warning("向量检索失败，改用字面兜底：%s", str(exc)[:120])
            _note_embed_fail(str(exc))
            if not literal_rows:
                literal_rows = self._literal_search(collection, goal, n)
            self._degraded_queries += 1
            if literal_rows:
                self._degraded_hits += 1
            return [r["document"] for r in literal_rows], True
        docs = res.get("documents", [[]])[0]
        dists = res.get("distances", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        out: list[str] = []
        for doc, dist, meta in zip(docs, dists, metas):
            if not doc or not str(doc).strip() or dist > thr:
                continue
            if filter_expired and _is_expired(meta):
                continue
            out.append(doc)
        return out, False

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
        self._last_degraded = False

        # 查询历史对话
        conv_docs, conv_degraded = self._recall(
            self._conversations, current_goal, self.N_CONVERSATIONS)
        self._last_degraded = self._last_degraded or conv_degraded
        if conv_docs:
            summaries = "\n".join(f"- {doc}" for doc in conv_docs)
            parts.append(f"## 与此任务相关的历史背景\n{summaries}")

        # 查询成功策略（过期策略不注入）
        strat_docs, strat_degraded = self._recall(
            self._strategies, current_goal, self.N_STRATEGIES, filter_expired=True)
        self._last_degraded = self._last_degraded or strat_degraded
        if strat_docs:
            parts.append(
                "## 类似任务的成功解决路径，可供参考\n" + strat_docs[0].strip())

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
        """按目标检索相关提示词改进经验；返回格式化文本列表（已按相似度过滤）。

        embedding 降级时走字面兜底：欠费期一样能读到历史提示词经验。
        """
        thr = self._similarity_threshold if threshold is None else threshold
        top = max(1, n)
        rows: list[tuple[str, dict]] = []
        literal_hits: list[dict] = []
        if _embedding_degraded():
            literal_hits = self._literal_search(self._prompt_refinements, current_goal, top)
            if literal_hits:
                self._degraded_queries += 1
                self._degraded_hits += 1
                rows = [(h["document"], h["metadata"]) for h in literal_hits]
                return [f"[{(m or {}).get('key', '?')}] {d[:500]}" for d, m in rows]
        try:
            res = self._prompt_refinements.query(
                query_texts=[current_goal],
                n_results=top,
                include=["documents", "metadatas", "distances"],
            )
            docs = res.get("documents", [[]])[0]
            dists = res.get("distances", [[]])[0]
            metas = res.get("metadatas", [[]])[0]
            for doc, dist, meta in zip(docs, dists, metas):
                if not doc or not doc.strip() or dist > thr:
                    continue
                rows.append((doc, meta or {}))
        except Exception as exc:
            logger.warning("Failed to query prompt refinements: %s", str(exc)[:120])
            _note_embed_fail(str(exc))
            if not literal_hits:
                literal_hits = self._literal_search(self._prompt_refinements, current_goal, top)
            self._degraded_queries += 1
            if literal_hits:
                self._degraded_hits += 1
            rows = [(h["document"], h["metadata"]) for h in literal_hits]
        return [f"[{(meta or {}).get('key', '?')}] {doc[:500]}" for doc, meta in rows]

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
                _note_embed_fail(str(exc))
                _conv_id = f"conv-{_now_epoch()}"
                self.queue_pending_write(
                    self.COLLECTION_CONVERSATIONS, conversation_entry,
                    {"timestamp": _now_iso(), "goal": goal[:200]}, _conv_id)

        # 对话上限治理（超出后删最旧 10%）
        self.enforce_conversation_cap()

        # embedding 已恢复时顺手回填欠费期排队的记忆（有额度才动，避免无谓重试）
        if not _embedding_degraded() and self.pending_count():
            self.drain_pending()

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
                _strat_id = f"strat-{_now_epoch()}"
                try:
                    self._strategies.add(
                        documents=[strategy_pattern],
                        metadatas=[new_meta],
                        ids=[_strat_id],
                    )
                    logger.info("Strategy memory stored (%d chars)", len(strategy_pattern))
                except Exception as exc:
                    logger.warning("Failed to store strategy: %s", exc)
                    _note_embed_fail(str(exc))
                    self.queue_pending_write(
                        self.COLLECTION_STRATEGIES, strategy_pattern,
                        new_meta, _strat_id)
        except Exception as exc:
            logger.warning("Strategy consolidation failed: %s", exc)

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

        `retrieval_mode`/`degraded_hit_rate` 用于区分口径：embedding 降级时命中率
        走字面兜底，若仍按 vector 口径显示"0%"会被误读成"没有历史经验"。
        """
        stats = self.stats() if counts is None else counts
        injections = max(0, self._injections)
        hits = max(0, self._inject_hits)
        degraded_hits = max(0, self._degraded_hits)
        return {
            "injections": injections,
            "hits": hits,
            "hit_rate": round(hits / injections, 4) if injections else 0.0,
            "strategy_count": int(stats.get("strategies", 0) or 0),
            "conversation_count": int(stats.get("conversations", 0) or 0),
            "expired_purged": max(0, self._expired_purged),
            "retrieval_mode": self._retrieval_mode(),
            "degraded_queries": max(0, self._degraded_queries),
            "degraded_hits": degraded_hits,
            "degraded_hit_rate": (
                round(degraded_hits / max(1, self._degraded_queries), 4)
                if self._degraded_queries else 0.0
            ),
            "pending_writes": self.pending_count(),
        }

    # ------------------------------------------------------------------
    # 待补录队列（embedding 不可用期间的写入不丢）
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        client = _pending_redis()
        if client is None:
            return 0
        try:
            return int(client.llen(MEM_PENDING_KEY) or 0)
        except Exception:
            return 0

    def queue_pending_write(
        self, collection_name: str, document: str, metadata: dict, doc_id: str,
    ) -> bool:
        """写入失败时把待写内容排进 Redis 列表，等 embedding 恢复后回填。

        此前写入失败只记一条 warning 就丢弃——欠费期跑出的经验永久消失，
        且用户无从知晓。排队 + 恢复回填让"降级期不丢数据"成立。
        """
        client = _pending_redis()
        if client is None:
            return False
        try:
            payload = json.dumps({
                "collection": collection_name,
                "document": document,
                "metadata": metadata or {},
                "id": doc_id,
            }, ensure_ascii=False)
            # 入队后立即收敛长度上限：非原子也无妨，最坏只是短暂超限，
            # 下一次 ltrim 会修回来（不值得为此引入事务/管道）
            client.lpush(MEM_PENDING_KEY, payload)
            client.ltrim(MEM_PENDING_KEY, 0, MEM_PENDING_MAX - 1)
            logger.info("记忆写入失败已排队待补录（%s，共 %d 条）",
                        collection_name, self.pending_count())
            return True
        except Exception as exc:
            logger.warning("待补录入队失败：%s", str(exc)[:120])
            return False

    def drain_pending(self, limit: int = 50) -> int:
        """embedding 恢复后回填排队的记忆。返回成功回填条数（幂等）。

        幂等靠"写成功后才 LREM"：中途失败的内容留在队列里等下一轮，
        已写成功的按 payload 原样 LREM，不会重复插入。
        """
        if _embedding_degraded():
            return 0
        client = _pending_redis()
        if client is None:
            return 0
        collections = {
            self.COLLECTION_CONVERSATIONS: self._conversations,
            self.COLLECTION_STRATEGIES: self._strategies,
            self.COLLECTION_PROMPT_REFINEMENTS: self._prompt_refinements,
        }
        done = 0
        for _ in range(max(1, limit)):
            try:
                raw = client.lindex(MEM_PENDING_KEY, -1)  # 取最旧（LPUSH 尾端）
            except Exception:
                break
            if not raw:
                break
            try:
                item = json.loads(raw)
            except Exception:
                client.rpop(MEM_PENDING_KEY)
                continue
            coll = collections.get(str(item.get("collection") or ""))
            if coll is None:
                client.rpop(MEM_PENDING_KEY)
                continue
            try:
                coll.add(
                    documents=[item.get("document") or ""],
                    metadatas=[item.get("metadata") or {}],
                    ids=[str(item.get("id") or f"pending-{int(time.time() * 1000)}")],
                )
            except Exception as exc:
                logger.warning("补录记忆失败，保留队列：%s", str(exc)[:120])
                _note_embed_fail(str(exc))
                break
            try:
                client.lrem(MEM_PENDING_KEY, 1, raw)
            except Exception:
                break
            done += 1
        if done:
            logger.info("记忆待补录回填完成：%d 条（剩余 %d）", done, self.pending_count())
        return done

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
