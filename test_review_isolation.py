# -*- coding: utf-8 -*-
"""R1：评审与待复核内容的隔离。

架构师给的三条边界，本文件逐条按**真实链路**断言（不是单测某个函数）：

1. 待复核的提示词改进经验：生成后不得在后续任务的检索/注入里出现（注册表与 RAG
   两条读取路径都要隔离），人工复核放行后才可用；
2. 交付状态按**根任务**归属：任务 A 的草稿理由不得把任务 B 的 verified 翻掉；
3. 恢复（checkpoint）不得复用绑定在**另一版计划**上的 PASS。

夹具是内存桩（不碰 Chroma/Redis/网络）；断言看的是"检索/注入结果里有没有它"，
这与真实链路里 `mem.inject_context` / `query_prompt_refinements` 的返回值同源。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_manager as M


class FakeCollection:
    """带 ids 的最小 Chroma 集合桩：add / get(ids|where_document) / update / query。"""

    def __init__(self, docs=None, query_raises=None):
        self._docs = {i: {"document": d, "metadata": dict(m or {})}
                      for i, d, m in (docs or [])}
        self._query_raises = query_raises

    @staticmethod
    def _first_value(filters):
        for _v in (filters or {}).values():
            return str(_v)
        return ""

    def _match(self, meta_filter=None, text_filter=None, ids=None):
        if ids is not None:
            return [i for i in ids if i in self._docs]
        token = self._first_value(text_filter)
        out = []
        for i, row in self._docs.items():
            if meta_filter and any((row["metadata"] or {}).get(k) != v
                                   for k, v in meta_filter.items()):
                continue
            if token and token not in row["document"]:
                continue
            out.append(i)
        return out

    def get(self, **kw):
        ids = self._match(kw.get("where"), kw.get("where_document"), kw.get("ids"))
        return {
            "ids": ids,
            "documents": [self._docs[i]["document"] for i in ids],
            "metadatas": [self._docs[i]["metadata"] for i in ids],
        }

    def add(self, documents=None, metadatas=None, ids=None):
        for d, m, i in zip(documents or [], metadatas or [], ids or []):
            self._docs[i] = {"document": d, "metadata": dict(m or {})}
        return True

    def update(self, ids=None, metadatas=None, documents=None):
        for i, m in zip(ids or [], metadatas or []):
            if i in self._docs:
                self._docs[i]["metadata"] = dict(m or {})
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

    def count(self):
        return len(self._docs)


def _mk_manager(refinements=None):
    import threading
    m = M.MemoryManager.__new__(M.MemoryManager)
    m._similarity_threshold = 0.6
    m._persist_directory = ":memory:"
    m._conversations = FakeCollection()
    m._strategies = FakeCollection()
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


class TestPendingReviewIsolatedFromRAG(unittest.TestCase):
    """链路：生成（未准入）→ 下一任务检索/注入为零 → 人工放行后可用。"""

    GOAL = "分析某公司2024年报的营业收入来源构成"

    def test_chain_generate_pending_then_approve(self):
        m = _mk_manager()
        # 1) 未准入的运行沉淀改进 → 必须写 pending_review（默认拒绝）
        m.add_prompt_refinement(
            goal=self.GOAL, key="step:web_search",
            issue="检索粒度太粗", fix_prompt="按指标分别检索并标注来源",
            rationale="反思轮结论", task_id="task-a", version=1,
            outcome="reflection",
        )
        stored = m._prompt_refinements.get(include=["metadatas"])
        entry_id = stored["ids"][0]
        self.assertEqual(stored["metadatas"][0].get("status"), "pending_review",
                         "调用方未声明已验证成功时必须默认写 pending_review")
        self.assertEqual(m.count_pending_refinements(), 1)

        # 2) 下一个任务：RAG 检索与注入都不得看到它
        with mock.patch.object(M, "_embedding_degraded", return_value=False):
            self.assertEqual(m.query_prompt_refinements(self.GOAL), [],
                             "待复核的改进不得被检索出来")
        self.assertEqual(m.query_prompt_refinements(self.GOAL), [],
                         "字面兜底路径同样不得返回待复核条目")

        # 3) 注入路径（真实入口）不得出现该内容
        ctx = m.inject_context(self.GOAL)
        self.assertNotIn("检索粒度太粗", ctx)
        self.assertNotIn("按指标分别检索", ctx)

        # 4) 人工复核放行后 → 可用
        changed = m.set_prompt_refinement_status([entry_id], "active")
        self.assertEqual(changed, 1)
        self.assertEqual(m.count_pending_refinements(), 0)
        with mock.patch.object(M, "_embedding_degraded", return_value=False):
            hits = m.query_prompt_refinements(self.GOAL)
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("step:web_search", hits[0])
        ctx2 = m.inject_context(self.GOAL)
        self.assertIn("按指标分别检索", ctx2, "放行后必须真正进入注入")

    def test_status_only_accepts_two_states(self):
        m = _mk_manager()
        m.add_prompt_refinement(
            goal=self.GOAL, key="k", issue="i", fix_prompt="f", task_id="t",
        )
        with self.assertRaises(ValueError):
            m.set_prompt_refinement_status(["prf-t-1"], "whatever")

    def test_approved_entry_survives_degraded_embedding(self):
        """放行后的条目在 embedding 欠费（字面兜底）时同样可检索。"""
        m = _mk_manager()
        m.add_prompt_refinement(
            goal=self.GOAL, key="k", issue="i", fix_prompt="f",
            task_id="t", status="active",
        )
        with mock.patch.object(M, "_embedding_degraded", return_value=True):
            hits = m.query_prompt_refinements(self.GOAL)
        self.assertEqual(len(hits), 1, hits)


class TestDeliveryStateIsPerRootTask(unittest.TestCase):
    """交付状态按根任务：A 写草稿不得把 B 的 verified 翻掉。"""

    def test_draft_reason_does_not_cross_tasks(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._now_iso = lambda: "T"
        o._task_admission = {}
        o._task_review = {}
        o._task_delivery = {}
        with mock.patch("orchestrator_v2.push_progress"):
            o._delivery("A")["reason"] = "步骤 s1 重做劣化已回退"
            self.assertEqual(o._delivery("B").get("reason"), "",
                             "B 的交付状态不得被 A 的草稿理由污染")
            self.assertTrue(o._delivery("A")["reason"])
            o._delivery("B").update({"status": "verified", "reason": ""})
            self.assertEqual(o._delivery("B")["status"], "verified")
            self.assertEqual(o._delivery("A")["status"], "",
                             "反向同理：B 的通过也不得写进 A 的状态")


class TestAdmissionUsesBoundReview(unittest.TestCase):
    """准入看的是"PASS 是否覆盖本版计划"，不是"有没有 PASS 这个字符串"。"""

    def _seed(self, tmp: Path, tid: str, *, plan_version: int, verdict: str = "PASS"):
        import workspace as ws_mod
        from report_version import VersionStore

        ws = ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        store = VersionStore(ws, tid)
        v = store.record("研究正文")
        store.bind_acceptance({"overall": "pass", "gaps": [],
                               "report_sha256": v.version_id})
        store.adopt(store.get(v.version_id) or v, reason="交付")
        store.record_delivery("研究正文\n\n---\n\n交付说明", accepted_body="研究正文",
                              ok=True)
        return store

    def test_unbound_pass_is_not_verified_locally(self):
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2
        from admission import admit_success

        tmp = Path(tempfile.mkdtemp(prefix="wm_r1_"))
        old = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            o = OrchestratorV2.__new__(OrchestratorV2)
            o._now_iso = lambda: "T"
            o._task_admission = {}
            o._task_review = {}
            o._task_delivery = {}
            o._identity_mode = lambda: "local"
            o._bump_plan_version("t-1", "任务起始规划")
            # PASS 绑在 v1 上，随后计划被改写（v2）→ 该 PASS 不再覆盖当前版本
            o._review_state("t-1").update({
                "verdict": "PASS", "plan_version": 1,
                "policy_version": "review-policy/v1", "mode": "local"})
            o._bump_plan_version("t-1", "反思追加/替换步骤")
            self.assertFalse(o.review_scope_ok("t-1"))
            # 谓词本身：只有 PASS + 未绑定 → 视为未完成评审
            d = admit_success(status="SUCCESS", acceptance={"overall": "pass"},
                              review={"verdict": "PASS"}, review_bound=False,
                              mode="local")
            self.assertTrue(d.admitted)
            self.assertFalse(d.verified)
            d2 = admit_success(status="SUCCESS", acceptance={"overall": "pass"},
                               review={"verdict": "PASS"}, review_bound=False,
                               mode="bank")
            self.assertFalse(d2.admitted, "银行口径：异版 PASS 连经验池都不进")
        finally:
            ws_mod.WORKSPACE_ROOT = old
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class TestEvolutionApproveIsClaimedAtomically(unittest.TestCase):
    """审批 API 的并发保护（R1）：一条待审策略只能被一个请求认领并部署。"""

    class _Redis:
        def __init__(self):
            self.lists = {}
            self.kv = {}
            self.lrem_calls = []

        def lrange(self, key, a, b):
            return list(self.lists.get(key, []))

        def lrem(self, key, count, value):
            items = self.lists.get(key, [])
            self.lrem_calls.append((key, count, value))
            if count == 0:
                n = items.count(value)
                self.lists[key] = [x for x in items if x != value]
                return n
            if value in items:
                items.remove(value)
                return 1
            return 0

        def get(self, key):
            return self.kv.get(key)

        def set(self, key, value):
            self.kv[key] = value
            return True

    def _handler(self, redis, body, item=None):
        import web_ui as W

        if item is not None:
            redis.lists.setdefault("evolution:pending", []).append(json.dumps(item))
        responses: list[tuple] = []

        class _Self:
            path = "/api/evolution/approve"

            @staticmethod
            def _json(payload, status=200):
                responses.append((payload, status))
                return payload

        with mock.patch.object(W, "_redis_ready", return_value=True), \
                mock.patch.object(W, "_new_redis", return_value=redis):
            W._post_evolution_approve(_Self, "/api/evolution/approve", body, {"role": "admin"})
        return responses, redis

    ITEM = {"strategy_id": "s-1", "agent_type": "search_agent", "rollout": 0.5}

    def test_approve_claims_and_deploys(self):
        redis = self._Redis()
        res, redis = self._handler(redis, {"strategy_id": "s-1", "approve": True},
                                   item=self.ITEM)
        self.assertEqual(res[-1][1], 200, res)
        self.assertTrue(res[-1][0]["deployed"])
        self.assertEqual(redis.lists["evolution:pending"], [],
                         "认领必须真的把待审条目移除")
        self.assertEqual(json.loads(redis.kv["strategy:active:search_agent"])["strategy_id"],
                         "s-1")
        # 认领用的是 count=1（原子认领凭据），不是 count=0 的无条件清除
        self.assertIn(("evolution:pending", 1, json.dumps(self.ITEM)), redis.lrem_calls)

    def test_second_concurrent_approve_loses_the_claim(self):
        """两个审批请求读到同一条待审策略 → 只有真正删掉它的那个能继续。"""

        class _StaleRedis(self._Redis):
            """模拟并发：lrange 拿到的是上一次读的快照，lrem 时该条已被别人删掉。"""

            def __init__(self, snapshot):
                super().__init__()
                self.lists["evolution:pending"] = [snapshot]

            def lrem(self, key, count, value):
                self.lrem_calls.append((key, count, value))
                self.lists["evolution:pending"] = []      # 已被另一个请求认领
                return 0

        redis = _StaleRedis(json.dumps(self.ITEM))
        res, _ = self._handler(redis, {"strategy_id": "s-1", "approve": True})
        self.assertEqual(res[-1][1], 409, res)
        self.assertIn("并发冲突", res[-1][0]["error"])
        self.assertNotIn("strategy:active:search_agent", redis.kv,
                         "没拿到认领凭据就不得部署")

    def test_unknown_pending_strategy_is_404(self):
        res, _ = self._handler(self._Redis(), {"strategy_id": "nope", "approve": True})
        self.assertEqual(res[-1][1], 404)

    def test_deployed_strategy_is_not_silently_overwritten(self):
        redis = self._Redis()
        redis.kv["strategy:active:search_agent"] = json.dumps(
            {"strategy_id": "s-0", "agent_type": "search_agent", "rollout": 1.0})
        res, redis = self._handler(redis, {"strategy_id": "s-1", "approve": True},
                                   item=self.ITEM)
        self.assertEqual(res[-1][1], 409, res)
        self.assertIn("s-0", res[-1][0]["active_strategy_id"])
        self.assertEqual(json.loads(redis.kv["strategy:active:search_agent"])["strategy_id"],
                         "s-0", "冲突时不得覆盖已部署策略")
        self.assertEqual(len(redis.lists["evolution:pending"]), 1,
                         "冲突时待审策略不得被认领掉（否则凭空消失）")

    def test_force_replaces_deployed_strategy(self):
        redis = self._Redis()
        redis.kv["strategy:active:search_agent"] = json.dumps(
            {"strategy_id": "s-0", "agent_type": "search_agent", "rollout": 1.0})
        res, redis = self._handler(redis, {"strategy_id": "s-1", "approve": True,
                                           "force": True}, item=self.ITEM)
        self.assertEqual(res[-1][1], 200, res)
        self.assertEqual(json.loads(redis.kv["strategy:active:search_agent"])["strategy_id"],
                         "s-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
