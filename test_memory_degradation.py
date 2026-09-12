# -*- coding: utf-8 -*-
"""记忆降级可用性回归：embedding 不可用时仍能读经验、写经验不丢。

此前 embedding 一挂，"读不到也写不进"：向量查询全抛错、写入失败只记 warning 就丢弃，
界面上命中率显示 0%，容易被误读成"没有历史经验"。
"""

import json
import unittest
from unittest import mock

import memory_manager as M


# ---------------------------------------------------------------------------
# 最小桩件
# ---------------------------------------------------------------------------

class FakeCollection:
    """最小 Chroma 集合桩：支持 add / get（按元数据与文本过滤）/ query。"""

    def __init__(self, docs: list[tuple[str, str, dict]] | None = None,
                 query_raises: Exception | None = None) -> None:
        self._docs = {i: {"document": d, "metadata": m} for i, d, m in (docs or [])}
        self._query_raises = query_raises
        self.added: list[tuple[str, dict]] = []

    @staticmethod
    def _first_value(filters: dict) -> str:
        for _v in (filters or {}).values():
            return str(_v)
        return ""

    def _match(self, meta_filter=None, text_filter=None):
        token = self._first_value(text_filter)
        out = []
        for i, row in self._docs.items():
            if meta_filter:
                if any((row["metadata"] or {}).get(k) != v
                       for k, v in meta_filter.items()):
                    continue
            if token and token not in row["document"]:
                continue
            out.append(i)
        return out

    def get(self, **kw):
        ids = self._match(kw.get("where"), kw.get("where_document"))
        return {
            "ids": ids,
            "documents": [self._docs[i]["document"] for i in ids],
            "metadatas": [self._docs[i]["metadata"] for i in ids],
        }

    def add(self, documents=None, metadatas=None, ids=None):
        for d, m, i in zip(documents or [], metadatas or [], ids or []):
            self._docs[i] = {"document": d, "metadata": m}
            self.added.append((i, m))
        return True

    def query(self, query_texts=None, n_results=3, include=None, **kw):
        if self._query_raises:
            raise self._query_raises
        ids = list(self._docs)[:n_results]
        return {
            "ids": [ids],
            "documents": [[self._docs[i]["document"] for i in ids]],
            "metadatas": [[self._docs[i]["metadata"] for i in ids]],
            "distances": [[0.1 for _ in ids]],
        }


class FakeRedis:
    """Redis 列表操作桩（待补录队列只用到 lpush/ltrim/llen/lindex/rpop/lrem）。"""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.fail_push = False

    def lpush(self, key, value):
        if self.fail_push:
            raise RuntimeError("redis down")
        self.lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, stop):
        self.lists[key] = self.lists.get(key, [])[start:stop + 1] if stop >= 0 else []

    def llen(self, key):
        return len(self.lists.get(key, []))

    def lindex(self, key, idx):
        items = self.lists.get(key, [])
        return items[idx] if items else None

    def rpop(self, key):
        items = self.lists.get(key, [])
        return items.pop() if items else None

    def lrem(self, key, count, value):
        items = self.lists.get(key, [])
        if value in items:
            items.remove(value)
            return 1
        return 0


def _mk_manager(conv=None, strat=None, refinements=None) -> M.MemoryManager:
    """绕过 Chroma/网络，构造只带桩集合的 MemoryManager。"""
    import threading
    m = M.MemoryManager.__new__(M.MemoryManager)
    m._similarity_threshold = 0.6
    m._persist_directory = ":memory:"
    m._conversations = conv or FakeCollection()
    m._strategies = strat or FakeCollection()
    m._prompt_refinements = refinements or FakeCollection()
    m._injections = 0
    m._inject_hits = 0
    m._expired_purged = 0
    m._degraded_queries = 0
    m._degraded_hits = 0
    m._last_degraded = False
    m._stats_cache = {"ts": 0.0, "data": {"conversations": 0, "strategies": 0}}
    m._stats_lock = threading.Lock()
    return m


# ---------------------------------------------------------------------------
# 分词与字面检索
# ---------------------------------------------------------------------------

class TestLiteralTokens(unittest.TestCase):
    def test_tokens_filtered_and_deduped(self):
        toks = M._literal_tokens("梳理贵州茅台2025年三季报核心财务数据 贵州茅台")
        self.assertIn("茅台", toks)
        self.assertIn("贵州", toks)
        self.assertTrue(all(len(t) >= 2 for t in toks), f"单词素不得入列：{toks}")
        self.assertEqual(len(toks), len(set(toks)), "token 必须去重")

    def test_limit_applied(self):
        toks = M._literal_tokens("营收 净利润 毛利率 现金流 总资产 负债", limit=3)
        self.assertLessEqual(len(toks), 3)

    def test_jieba_missing_falls_back_to_regex(self):
        with mock.patch.dict("sys.modules", {"jieba": None}):
            self.assertTrue(M._literal_tokens("贵州茅台 revenue2025"),
                            "jieba 缺失时必须有退化切词")

    def test_empty_query(self):
        self.assertEqual(M._literal_tokens(""), [])


class TestLiteralSearch(unittest.TestCase):
    def test_hits_ranked_by_token_overlap(self):
        col = FakeCollection([
            ("one", "贵州茅台2025年三季报营收", {}),
            ("two", "贵州茅台2025年三季报营收 毛利率 现金流", {}),
            ("three", "完全无关的内容", {}),
        ])
        m = _mk_manager(conv=col)
        hits = m._literal_search(col, "贵州茅台2025年三季报 毛利率 现金流", 5)
        self.assertTrue(hits, "字面检索必须有命中")
        self.assertEqual(hits[0]["id"], "two", "命中词更多的应排在前面")
        self.assertTrue(all(h["degraded"] for h in hits), "降级结果必须自标 degraded")
        self.assertNotIn("three", [h["id"] for h in hits])

    def test_expired_strategy_filtered(self):
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        col = FakeCollection([("s1", "贵州茅台策略", {"expires_at": past})])
        m = _mk_manager(strat=col)
        self.assertEqual(m._literal_search(col, "贵州茅台", 3), [],
                         "过期策略不得被兜底检索注入")

    def test_get_unsupported_returns_empty_not_raise(self):
        class NoGet:
            def get(self, **kw):
                raise RuntimeError("filter unsupported")
        m = _mk_manager(conv=NoGet())
        self.assertEqual(m._literal_search(m._conversations, "贵州茅台", 3), [])

    def test_limit_respected(self):
        col = FakeCollection([(str(i), f"贵州茅台 第{i}条", {}) for i in range(10)])
        m = _mk_manager(conv=col)
        self.assertEqual(len(m._literal_search(col, "贵州茅台", 2)), 2)


# ---------------------------------------------------------------------------
# 检索分路
# ---------------------------------------------------------------------------

class TestRecallRouting(unittest.TestCase):
    def test_degraded_uses_literal(self):
        col = FakeCollection([("c1", "贵州茅台历史对话", {})])
        m = _mk_manager(conv=col)
        with mock.patch.object(M, "_embedding_degraded", return_value=True):
            docs, degraded = m._recall(col, "贵州茅台三季报", 3)
        self.assertTrue(degraded)
        self.assertIn("贵州茅台历史对话", docs)
        self.assertEqual(m._degraded_queries, 1)
        self.assertEqual(m._degraded_hits, 1)

    def test_vector_exception_falls_back_to_literal(self):
        col = FakeCollection([("c1", "贵州茅台历史对话", {})],
                             query_raises=RuntimeError("402 insufficient balance"))
        m = _mk_manager(conv=col)
        with mock.patch.object(M, "_embedding_degraded", return_value=False), \
                mock.patch.object(M, "_note_embed_fail") as note:
            docs, degraded = m._recall(col, "贵州茅台三季报", 3)
        self.assertTrue(degraded, "向量异常必须走兜底而不是空手而归")
        self.assertIn("贵州茅台历史对话", docs)
        note.assert_called_once()

    def test_degraded_without_literal_hit_still_tries_vector(self):
        """降级标记可能滞后：字面没命中时仍试一次向量，成功即按 vector 口径。"""
        col = FakeCollection([("c1", "无关内容", {})])
        m = _mk_manager(conv=col)
        with mock.patch.object(M, "_embedding_degraded", return_value=True):
            docs, degraded = m._recall(col, "完全不同的目标", 3)
        self.assertFalse(degraded, "向量成功时不应标降级")
        self.assertIn("无关内容", docs)

    def test_healthy_path_uses_vector_threshold(self):
        col = FakeCollection([("c1", "相关记忆", {})])
        m = _mk_manager(conv=col)
        with mock.patch.object(M, "_embedding_degraded", return_value=False):
            docs, degraded = m._recall(col, "目标", 3)
        self.assertFalse(degraded)
        self.assertEqual(docs, ["相关记忆"])
        self.assertEqual(m._degraded_queries, 0)

    def test_prompt_refinements_degrade_path(self):
        col = FakeCollection([("r1", "把目标拆成三步并标注来源", {"key": "planner_v2"})])
        m = _mk_manager(refinements=col)
        with mock.patch.object(M, "_embedding_degraded", return_value=True):
            out = m.query_prompt_refinements("目标拆解 来源标注")
        self.assertEqual(len(out), 1)
        self.assertIn("planner_v2", out[0])
        self.assertIn("把目标拆成三步", out[0])


# ---------------------------------------------------------------------------
# 健康口径
# ---------------------------------------------------------------------------

class TestHealthFields(unittest.TestCase):
    def test_health_exposes_degraded_mode(self):
        m = _mk_manager(conv=FakeCollection([("c1", "贵州茅台历史", {})]))
        with mock.patch.object(M, "_embedding_degraded", return_value=True), \
                mock.patch.object(m, "pending_count", return_value=2):
            m.inject_context("贵州茅台")
            h = m.memory_health({"strategies": 0, "conversations": 1})
        self.assertEqual(h["retrieval_mode"], "literal")
        self.assertEqual(h["degraded_queries"], 1)
        self.assertEqual(h["degraded_hits"], 1)
        self.assertEqual(h["degraded_hit_rate"], 1.0)
        self.assertEqual(h["pending_writes"], 2)
        self.assertIn("hit_rate", h, "既有字段不得消失")

    def test_health_mode_unavailable_before_any_injection(self):
        m = _mk_manager()
        with mock.patch.object(M, "_embedding_degraded", return_value=False), \
                mock.patch.object(m, "pending_count", return_value=0):
            h = m.memory_health({"strategies": 0, "conversations": 0})
        self.assertEqual(h["retrieval_mode"], "unavailable")

    def test_health_mode_vector_when_healthy(self):
        m = _mk_manager(conv=FakeCollection([("c1", "相关记忆", {})]))
        with mock.patch.object(M, "_embedding_degraded", return_value=False), \
                mock.patch.object(m, "pending_count", return_value=0):
            m.inject_context("目标")
            h = m.memory_health({"strategies": 0, "conversations": 0})
        self.assertEqual(h["retrieval_mode"], "vector")
        self.assertEqual(h["degraded_hits"], 0)


# ---------------------------------------------------------------------------
# 待补录队列
# ---------------------------------------------------------------------------

class TestPendingQueue(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        patcher = mock.patch.object(M, "_pending_redis", return_value=self.redis)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_queue_push_and_count(self):
        m = _mk_manager()
        self.assertTrue(m.queue_pending_write(
            M.MemoryManager.COLLECTION_CONVERSATIONS, "目标: X", {"goal": "X"}, "c1"))
        self.assertEqual(m.pending_count(), 1)
        payload = json.loads(self.redis.lists[M.MEM_PENDING_KEY][0])
        self.assertEqual(payload["id"], "c1")
        self.assertEqual(payload["collection"], "conversations")

    def test_queue_failure_is_swallowed(self):
        self.redis.fail_push = True
        m = _mk_manager()
        self.assertFalse(m.queue_pending_write("conversations", "d", {}, "c1"))

    def test_queue_is_trimmed_to_cap(self):
        m = _mk_manager()
        for i in range(M.MEM_PENDING_MAX + 5):
            m.queue_pending_write(m.COLLECTION_CONVERSATIONS, f"d{i}", {}, f"c{i}")
        self.assertEqual(m.pending_count(), M.MEM_PENDING_MAX,
                         "队列长度必须收敛在上限内")

    def test_drain_writes_and_clears_queue(self):
        m = _mk_manager()
        m.queue_pending_write(m.COLLECTION_CONVERSATIONS, "目标: A", {"goal": "A"}, "c1")
        m.queue_pending_write(m.COLLECTION_CONVERSATIONS, "目标: B", {"goal": "B"}, "c2")
        with mock.patch.object(M, "_embedding_degraded", return_value=False):
            self.assertEqual(m.drain_pending(), 2)
        self.assertEqual(m.pending_count(), 0)
        self.assertEqual({i for i, _ in m._conversations.added}, {"c1", "c2"})

    def test_drain_is_idempotent_when_rerun(self):
        m = _mk_manager()
        m.queue_pending_write(m.COLLECTION_STRATEGIES, "策略正文", {}, "s1")
        with mock.patch.object(M, "_embedding_degraded", return_value=False):
            self.assertEqual(m.drain_pending(), 1)
            self.assertEqual(m.drain_pending(), 0, "已回填的条目不得重复写入")
        self.assertEqual(len(m._strategies.added), 1)

    def test_drain_keeps_item_when_write_still_fails(self):
        col = FakeCollection()
        col.add = mock.Mock(side_effect=RuntimeError("402"))
        m = _mk_manager(strat=col)
        m.queue_pending_write(m.COLLECTION_STRATEGIES, "策略正文", {}, "s1")
        with mock.patch.object(M, "_embedding_degraded", return_value=False), \
                mock.patch.object(M, "_note_embed_fail"):
            self.assertEqual(m.drain_pending(), 0)
        self.assertEqual(m.pending_count(), 1, "写失败必须留在队列里等下一轮")

    def test_drain_noop_while_degraded(self):
        m = _mk_manager()
        m.queue_pending_write(m.COLLECTION_CONVERSATIONS, "d", {}, "c1")
        with mock.patch.object(M, "_embedding_degraded", return_value=True):
            self.assertEqual(m.drain_pending(), 0)
        self.assertEqual(m.pending_count(), 1)

    def test_unknown_collection_dropped(self):
        m = _mk_manager()
        self.redis.lpush(M.MEM_PENDING_KEY, json.dumps(
            {"collection": "不存在的集合", "document": "d", "id": "x"}))
        with mock.patch.object(M, "_embedding_degraded", return_value=False):
            self.assertEqual(m.drain_pending(), 0)
        self.assertEqual(m.pending_count(), 0, "坏条目必须清掉避免卡死队列")

    def test_consolidation_queues_on_write_failure(self):
        """写入失败的内容必须进队列（consolidate 里还会尝试回填）。"""
        conv = FakeCollection()
        conv.add = mock.Mock(side_effect=RuntimeError("402"))
        m = _mk_manager(conv=conv)
        with mock.patch.object(m, "_find_recent_conversation", return_value=False), \
                mock.patch.object(m, "enforce_conversation_cap"), \
                mock.patch.object(m, "_extract_strategy_pattern", return_value="pat"), \
                mock.patch.object(M, "_note_embed_fail"), \
                mock.patch.object(M, "_embedding_degraded", return_value=True):
            M.MemoryManager.consolidate_memory(
                m, "目标 A", [{"status": "success"}], "总结")
        self.assertEqual(m.pending_count(), 1, "对话写入失败必须排队")


class TestConfigWiring(unittest.TestCase):
    """设置页里标着可调的 memory.* 参数必须真的生效。"""

    def test_config_used_when_env_absent(self):
        cfg = {"memory": {"strategy_dedup_threshold": 0.42}}
        with mock.patch.object(M, "_load_config_file", return_value=cfg), \
                mock.patch.dict("os.environ", {}, clear=False):
            self.assertEqual(
                M._config_memory_value("strategy_dedup_threshold", float, 0.95), 0.42)

    def test_env_overrides_config(self):
        cfg = {"memory": {"strategy_dedup_threshold": 0.42}}
        with mock.patch.object(M, "_load_config_file", return_value=cfg), \
                mock.patch.dict("os.environ",
                                {"MEMORY_STRATEGY_DEDUP_THRESHOLD": "0.77"}):
            self.assertEqual(
                M._config_memory_value("strategy_dedup_threshold", float, 0.95), 0.77)

    def test_default_when_absent(self):
        with mock.patch.object(M, "_load_config_file", return_value={}), \
                mock.patch.dict("os.environ", {}, clear=False):
            self.assertEqual(
                M._config_memory_value("conversations_max", int, 2000), 2000)

    def test_bad_value_falls_back(self):
        cfg = {"memory": {"conversations_max": "abc"}}
        with mock.patch.object(M, "_load_config_file", return_value=cfg), \
                mock.patch.dict("os.environ", {}, clear=False):
            self.assertEqual(
                M._config_memory_value("conversations_max", int, 2000), 2000)


if __name__ == "__main__":
    unittest.main()
