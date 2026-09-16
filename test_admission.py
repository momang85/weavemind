# -*- coding: utf-8 -*-
"""M0-d 经验与模板准入回归：显式谓词、待复核留档、自迭代准入、同版本只计一次。

离线测试，不连 Redis、不调模型。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import admission as adm  # noqa: E402
import prompt_registry as pr  # noqa: E402


class TestAdmissionPredicate(unittest.TestCase):
    def _d(self, **kw):
        base = dict(status="SUCCESS", acceptance={"overall": "pass"},
                    review={"verdict": "PASS"}, mode="local", version_bound=True)
        base.update(kw)
        return adm.admit_success(**base)

    def test_full_evidence_is_verified(self):
        d = self._d()
        self.assertTrue(d.admitted)
        self.assertTrue(d.verified)
        self.assertEqual(d.reasons, [])

    def test_success_with_issues_is_not_success(self):
        for status in ("SUCCESS_WITH_ISSUES", "FAILED", "CANCELLED", "PARTIAL", ""):
            d = self._d(status=status)
            self.assertFalse(d.admitted, status)
            self.assertFalse(d.verified, status)
            self.assertTrue(d.reasons)

    def test_missing_acceptance_is_unknown_not_pass(self):
        d = self._d(acceptance=None)
        self.assertFalse(d.admitted)
        self.assertIn("未知", " ".join(d.reasons))
        d2 = self._d(acceptance={})
        self.assertFalse(d2.admitted)

    def test_failed_acceptance_is_rejected(self):
        d = self._d(acceptance={"overall": "fail"})
        self.assertFalse(d.admitted)
        self.assertIn("验收未通过", " ".join(d.reasons))

    def test_hard_constraint_failure_is_rejected(self):
        d = self._d(hard_ok=False, hard_reasons=["交付一致性守卫未通过"])
        self.assertFalse(d.admitted)
        self.assertIn("交付一致性", " ".join(d.reasons))

    def test_version_unbound_acceptance_is_rejected(self):
        d = self._d(version_bound=False)
        self.assertFalse(d.admitted)
        self.assertIn("版本", " ".join(d.reasons))

    def test_degraded_review_is_admitted_but_not_verified(self):
        d = self._d(review={"verdict": "DEGRADED", "degraded_reason": "评审超时"})
        self.assertTrue(d.admitted, "个人模式可作经验参考")
        self.assertFalse(d.verified, "评审降级的不是已验证成功")
        self.assertIn("评审未完成", " ".join(d.reasons))

    def test_missing_review_is_not_verified(self):
        d = self._d(review=None)
        self.assertTrue(d.admitted)
        self.assertFalse(d.verified)

    def test_bank_mode_requires_bound_pass(self):
        d = self._d(mode="bank", review={"verdict": "DEGRADED"})
        self.assertFalse(d.admitted, "银行口径没有 PASS 不该进经验池")
        self.assertIn("银行", " ".join(d.reasons))

    def test_bool_of_decision_follows_admitted(self):
        self.assertTrue(bool(self._d()))
        self.assertFalse(bool(self._d(acceptance=None)))


class TestMemoryAdmissionWiring(unittest.TestCase):
    """策略沉淀与模板固化都要走准入；未验证的经验标待复核。"""

    def test_memory_skips_strategy_without_acceptance(self):
        import memory_manager as mm

        class _C:
            def __init__(self):
                self.docs = []

            def add(self, documents, metadatas, ids):
                self.docs.append((documents, metadatas, ids))

            def count(self):
                return len(self.docs)

            def query(self, **kw):
                return {"documents": [[]]}

        m = mm.MemoryManager.__new__(mm.MemoryManager)
        m._strategies, m._conversations = _C(), _C()
        m._lock = __import__("threading").RLock()
        from unittest import mock
        with mock.patch.object(mm.MemoryManager, "_extract_strategy_pattern",
                               lambda self, *a, **k: "pattern"), \
                mock.patch.object(mm.MemoryManager, "_find_recent_conversation",
                                  lambda self, *a, **k: False), \
                mock.patch.object(mm.MemoryManager, "enforce_conversation_cap",
                                  lambda self: None), \
                mock.patch.object(mm.MemoryManager, "_find_similar_strategy",
                                  lambda self, *a, **k: None), \
                mock.patch.object(mm.MemoryManager, "pending_count", lambda self: 0):
            m.consolidate_memory("无验收目标", [{"capability": "web_search"}], "报告")
        self.assertEqual(m._strategies.count(), 0, "未知证据不得进策略池")
        self.assertEqual(m._conversations.count(), 1, "对话保留（可追溯）")

    def test_template_requires_acceptance_true(self):
        src = (ROOT / "templates_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("acc is not True", src,
                      "模板固化必须要求本次验收**通过**（未知不算）")


class TestRefineryAdmission(unittest.TestCase):
    def test_pending_review_override_does_not_apply(self):
        entry = {"prompt": "p", "version": 2, "scope": "global",
                 "status": "pending_review"}
        self.assertFalse(pr._entry_applies(entry, "任意目标"))
        entry["status"] = "active"
        self.assertTrue(pr._entry_applies(entry, "任意目标"))

    def test_record_override_defaults_to_active(self):
        import inspect
        sig = inspect.signature(pr.record_override)
        self.assertEqual(sig.parameters["status"].default, "active")

    def test_resolve_override_skips_pending_entries(self):
        from unittest import mock
        data = {"web_search": [
            {"prompt": "待复核", "version": 5, "scope": "global",
             "status": "pending_review"},
            {"prompt": "生效", "version": 1, "scope": "global", "status": "active"},
        ]}
        with mock.patch.object(pr, "load_overrides", lambda: data):
            ov = pr.resolve_override("web_search", "目标")
        self.assertIsNotNone(ov)
        self.assertEqual(ov["prompt"], "生效", "待复核条目不得抢先生效")

    def test_refinery_marks_unverified_as_pending(self):
        src = (ROOT / "prompt_refinery.py").read_text(encoding="utf-8")
        self.assertIn('status = "active" if', src)
        self.assertIn('status=status', src)


class TestVersionBoundDedupe(unittest.TestCase):
    """同任务同版本只计一次；未知不计入。"""

    def setUp(self):
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        self.ws_mod = ws_mod
        self.mk = OrchestratorV2
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_adm_"))
        self.old_root = ws_mod.WORKSPACE_ROOT
        self.old_stats = OrchestratorV2.CONSOLIDATION_STATS_FILE
        OrchestratorV2.CONSOLIDATION_STATS_FILE = str(self.tmp / "stats.json")
        ws_mod.configure_workspace_root(str(self.tmp))
        self.o = OrchestratorV2.__new__(OrchestratorV2)
        self.o._now_iso = lambda: "T"
        self.goal = "分析某公司历年财报"
        self.steps = [{"capability": "web_search"}]

    def tearDown(self):
        self.mk.CONSOLIDATION_STATS_FILE = self.old_stats
        self.ws_mod.WORKSPACE_ROOT = self.old_root
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _task(self, tid: str, body: str, verified: bool):
        from report_version import VersionStore
        ws = self.ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        store = VersionStore(ws, tid)
        v = store.record(body)
        store.bind_acceptance({"overall": "pass", "gaps": [],
                               "report_sha256": v.version_id})
        store.adopt(store.get(v.version_id), reason="t")
        return adm.AdmissionDecision(admitted=True, verified=verified)

    def test_same_task_same_version_counted_once(self):
        domain, chain = self.o._consolidation_key(self.goal, self.steps)
        d = self._task("t-a", "正文", verified=True)
        self.o._record_consolidation_stat("t-a", self.goal, self.steps, admission=d)
        self.assertEqual(self.o._count_verified_chain(domain, chain, "t-x"), 1)
        self.o._record_consolidation_stat("t-a", self.goal, self.steps, admission=d)
        self.assertEqual(self.o._count_verified_chain(domain, chain, "t-x"), 1,
                         "同任务同版本重复收尾不得刷高计数")

    def test_new_version_counts_again(self):
        domain, chain = self.o._consolidation_key(self.goal, self.steps)
        self.o._record_consolidation_stat("t-b", self.goal, self.steps,
                                          admission=self._task("t-b", "版本一", True))
        self.o._record_consolidation_stat("t-b", self.goal, self.steps,
                                          admission=self._task("t-b", "版本二", True))
        self.assertEqual(self.o._count_verified_chain(domain, chain, "t-x"), 1,
                         "同一任务的新版本在统计里只保留最新一条")

    def test_unverified_not_counted(self):
        domain, chain = self.o._consolidation_key(self.goal, self.steps)
        self.o._record_consolidation_stat("t-c", self.goal, self.steps,
                                          admission=self._task("t-c", "正文", False))
        self.assertEqual(self.o._count_verified_chain(domain, chain, "t-x"), 0)

    def test_legacy_entries_without_verified_key_are_not_counted(self):
        """历史样例只有 acceptance 字段：留档但不再计入（隔离而非删除）。"""
        path = Path(self.mk.CONSOLIDATION_STATS_FILE)
        domain, chain = self.o._consolidation_key(self.goal, self.steps)
        path.write_text(json.dumps([
            {"task_id": "old-1", "domain": domain, "chain": list(chain),
             "acceptance": True, "ts": "2026-01-01"},
        ], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.o._count_verified_chain(domain, chain, "t-x"), 0)
        kept = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(kept), 1, "历史条目保留审计，不做批量删")


class TestAdmissionDecisionFromOrchestrator(unittest.TestCase):
    """`_admission_decision`：把版本/交付/评审证据合成准入结论。"""

    def setUp(self):
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        self.ws_mod = ws_mod
        self.mk = OrchestratorV2
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_dec_"))
        self.old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.o = OrchestratorV2.__new__(OrchestratorV2)
        self.o._now_iso = lambda: "T"
        self.o._task_admission = {}
        self.o._task_review = {}
        self.o._delivery_draft_reason = ""

    def tearDown(self):
        self.ws_mod.WORKSPACE_ROOT = self.old_root
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, tid: str, *, bind: bool = True, delivery_ok: bool = True,
              verdict: str = "PASS"):
        from report_version import VersionStore
        ws = self.ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        store = VersionStore(ws, tid)
        body = "研究正文"
        v = store.record(body)
        if bind:
            store.bind_acceptance({"overall": "pass", "gaps": [],
                                   "report_sha256": v.version_id})
        store.adopt(store.get(v.version_id) or v, reason="交付")
        store.record_delivery(body + "\n\n---\n\n交付说明", accepted_body=body,
                              ok=delivery_ok)
        self.o._review_state(tid).update({"verdict": verdict, "policy_version":
                                          "review-policy/v1", "mode": "local"})
        return tid

    def test_verified_when_all_evidence_present(self):
        tid = self._seed("t-ok")
        d = self.o._admission_decision(tid, "SUCCESS", {"overall": "pass"})
        self.assertTrue(d.verified, d.reasons)

    def test_draft_delivery_blocks_admission(self):
        tid = self._seed("t-draft", delivery_ok=False)
        d = self.o._admission_decision(tid, "SUCCESS", {"overall": "pass"})
        self.assertFalse(d.admitted)
        self.assertIn("交付", " ".join(d.reasons))

    def test_unbound_acceptance_blocks_admission(self):
        tid = self._seed("t-unbound", bind=False)
        d = self.o._admission_decision(tid, "SUCCESS", None)
        self.assertFalse(d.admitted)

    def test_issues_status_blocks_admission(self):
        tid = self._seed("t-issues")
        d = self.o._admission_decision(tid, "SUCCESS_WITH_ISSUES", {"overall": "pass"})
        self.assertFalse(d.admitted)

    def test_no_acceptance_blocks_admission(self):
        tid = self._seed("t-noacc")
        d = self.o._admission_decision(tid, "SUCCESS", None)
        self.assertFalse(d.admitted)

    def test_bank_mode_without_pass_blocks(self):
        tid = self._seed("t-bank", verdict="DEGRADED")
        orig = self.o._identity_mode
        self.o._identity_mode = lambda: "bank"
        try:
            d = self.o._admission_decision(tid, "SUCCESS", {"overall": "pass"})
        finally:
            self.o._identity_mode = orig
        self.assertFalse(d.admitted)


class TestStatsPathSafety(unittest.TestCase):
    def test_env_override_is_ignored(self):
        """统计文件位置不接受环境变量覆盖（计数依据的载体不能被环境改写）。"""
        import os
        from unittest import mock
        from orchestrator_v2 import OrchestratorV2

        default = OrchestratorV2.CONSOLIDATION_STATS_FILE
        with mock.patch.dict(os.environ, {"WEAVEMIND_CONSOLIDATION_STATS": "x.json"}):
            p = OrchestratorV2._consolidation_stats_path()
        self.assertEqual(p, default, "环境变量不得改变统计文件位置")


class TestEndpointCooldownIsPerEndpoint(unittest.TestCase):
    """额度冷却按端点生效：欠费的备用端点不得冻结健康的主端点（M0-e）。"""

    def _run(self, reasons: dict):
        import llm_client as lc
        from unittest import mock

        lc._clear_balance_cache()
        probes = []
        with mock.patch.object(lc, "_ensure_cfg_fresh"),                 mock.patch.dict(__import__("os").environ,
                                {"LLM_BASE_URL": "https://primary.test/v1",
                                 "LLM_API_KEY": "k", "LLM_MODEL": "m"}),                 mock.patch.object(lc, "_BACKUP_CFG",
                                  {"base_url": "https://backup.test/v1",
                                   "api_key": "k", "model": "m"}),                 mock.patch.object(lc, "_probe_endpoint_status",
                                  lambda base, key, model, endpoint="": (
                                      probes.append(endpoint) or
                                      {"ok": reasons.get(endpoint) == "ok",
                                       "reason": reasons.get(endpoint, "ok")})),                 mock.patch.object(lc, "_mark_endpoint", lambda *a, **k: None):
            first = lc.get_balance_status(use_cache=False)
            probes.clear()
            # 让整份缓存的 30 秒常规 TTL 过期（终态端点另有 600 秒冷却）：
            # 这才走到"健康端点重新探测、欠费端点复用结论"这条路径
            lc._balance_cache["ts"] = __import__("time").time() - 60
            second = lc.get_balance_status(use_cache=True)
        return first, second, probes

    def test_delinquent_backup_does_not_freeze_healthy_primary(self):
        first, second, probes = self._run({"primary": "ok",
                                           "backup": "insufficient_balance"})
        self.assertTrue(first["primary"]["ok"])
        self.assertTrue(second["primary"]["ok"], "健康主端点必须仍可用")
        self.assertIn("primary", probes, "主端点应照常探测，不被备用端点拖住")
        self.assertNotIn("backup", probes, "欠费备用端点在其冷却期内不再白打")
        self.assertTrue(second["backup"].get("frozen"), "复用冻结结论应带标记可辨")

    def test_both_terminal_uses_long_cooldown(self):
        import llm_client as lc
        first, second, probes = self._run({"primary": "insufficient_balance",
                                           "backup": "unauthorized"})
        self.assertFalse(first["primary"]["ok"])
        self.assertEqual(probes, [], "两端点都在冷却期内：不重复探测")
        self.assertEqual(second["primary"]["reason"], "insufficient_balance")
        self.assertGreaterEqual(lc._balance_cache["ttl"], 60,
                                "两端点都终态失败 → 整份结果用长冷却")


if __name__ == "__main__":
    unittest.main()
