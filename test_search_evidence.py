# -*- coding: utf-8 -*-
"""RUNTIME-1022 P1-D：检索尝试与采纳证据分离、阶段预算、无收益停损（离线）。

验证：
1. 阶段预算显式分配：总和不超过任务级上限，后阶段预留不能被前一阶段吃光，
   环境覆盖不能突破上限（不无限重置）；
2. 采纳证据只算**通过准入**的来源（未准入候选不算有效证据）；
3. 跨轮保留：预算拒绝的空数组、摘要代做、换查询后命中变差，都不覆盖既有采纳
   证据，且尝试轨迹逐条留痕（审计不删）；
4. 无新增材料 + 无新查询 → 停损并如实呈现证据缺口；
5. 审计两本账（尝试/采纳）进交付包；
6. 降级提示不再隐瞒（"不要提及搜索失败"已移除）。
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import search_evidence as se
from orchestrator_v2 import OrchestratorV2
import delivery_pipeline
import workspace as ws_mod


CONTRACT = {
    "company": "伊利股份", "company_id": "600887", "market": "a_share",
    "periods": [2023, 2024], "as_of": "2025-04-30",
}
YILI_ANNUAL = {
    "title": "伊利股份 2024 年年度报告",
    "url": "https://static.cninfo.com.cn/finalpage/2025-04-29/8.PDF",
    "snippet": "公司 2024 年度实现营业总收入 1200 亿元",
}
MACRO = {
    "title": "国家统计局：2024年国民经济运行情况",
    "url": "https://www.stats.gov.cn/sj/zxfb/202501/t1.html",
    "snippet": "国内生产总值同比增长 5.0%",
}
SKF = {
    "title": "SKF 6008 深沟球轴承 产品页",
    "url": "https://www.skf.com/cn/products/6008",
    "snippet": "6008 轴承 内径 40mm",
}


def ok_result(items):
    return {"status": "SUCCESS", "result": json.dumps(items, ensure_ascii=False)}


class _FakeMessaging:
    def __init__(self):
        self.events = []

    def publish(self, channel, msg):
        self.events.append((channel, msg))


def make_orch(**overrides):
    o = object.__new__(OrchestratorV2)
    o._max_retry = 2
    o._replan_depth = 0
    o._messaging = _FakeMessaging()
    o._task_sources = {}
    o._task_sources_lock = __import__("threading").Lock()
    o._now_iso = lambda: "2026-10-23T00:00:00+08:00"
    o._goal_contract = lambda goal: CONTRACT
    o._contract_issue = lambda goal, step, result: ""
    o._cancel_requested = lambda task_id: False
    o._note_fetch_dead_url = lambda step, result, task_id: None
    o._step_fetch_url = lambda step: ""
    o._fetch_result_reject_mark = lambda result: ""
    o._step_retryable = lambda result: True
    o._step_retryable_reason = lambda result: "瞬时失败"
    o._search_new_queries_available = lambda goal, task_id, seen: False
    o._search_retry_instruction = lambda *a, **k: "retry"
    o._build_step_diagnosis = lambda *a, **k: {}
    for k, v in overrides.items():
        setattr(o, k, v)
    return o


class TestStageBudget(unittest.TestCase):
    def test_plan_sums_within_cap(self):
        for total in (1, 2, 3, 4, 6, 9):
            plan = se.stage_plan(total)
            self.assertLessEqual(sum(plan.values()), total, plan)
            self.assertGreaterEqual(sum(plan.values()), 1, plan)
            self.assertEqual(set(plan), set(se.STAGES))

    def test_reverse_stage_reserve(self):
        plan = se.stage_plan(6)
        self.assertEqual(plan, {"initial": 4, "retry": 1, "repair": 1})
        self.assertEqual(se.stage_reserve(plan, se.STAGE_RETRY), 1)
        self.assertEqual(se.stage_reserve(plan, se.STAGE_INITIAL), 2)
        self.assertEqual(se.stage_reserve(plan, se.STAGE_REPAIR), 0)

    def test_env_override_cannot_exceed_cap(self):
        with mock.patch.dict(os.environ,
                             {"WM_SEARCH_STAGE_PLAN": "initial:9,retry:9"}, clear=False):
            plan = se.environment_stage_plan(6)
            self.assertLessEqual(sum(plan.values()), 6, plan)
            self.assertEqual(plan, se.stage_plan(6))

    def test_env_override_within_cap_is_used(self):
        with mock.patch.dict(os.environ,
                             {"WM_SEARCH_STAGE_PLAN": "initial:3,retry:2,repair:1"},
                             clear=False):
            plan = se.environment_stage_plan(6)
            self.assertEqual(plan, {"initial": 3, "retry": 2, "repair": 1})


class TestAdmittedCounting(unittest.TestCase):
    def test_unadmitted_candidates_are_not_evidence(self):
        res = ok_result([YILI_ANNUAL, MACRO, SKF])
        self.assertEqual(se.candidates_of(res), 3)
        # 公司研究契约：只有通过准入的年报算有效证据
        self.assertEqual(se.admitted_of(res, CONTRACT), 1)

    def test_market_contract_counts_candidates(self):
        res = ok_result([{"title": "A股成交额排行", "url": "https://x/rank"}])
        self.assertEqual(se.admitted_of(res, {"market": "a_share"}), 1)

    def test_non_success_or_bad_json_is_zero(self):
        self.assertEqual(se.candidates_of({"status": "FAILED", "result": "[]"}), 0)
        self.assertEqual(se.admitted_of({"status": "SUCCESS", "result": "not json"},
                                        CONTRACT), 0)


class TestAdoptedStore(unittest.TestCase):
    def test_first_adopt_then_keep_worse(self):
        st = se.AdoptedStore()
        first = st.offer("s1", ok_result([YILI_ANNUAL]), contract=CONTRACT,
                         round_no=0, at="t0")
        self.assertTrue(first["adopted"])
        worse = st.offer("s1", ok_result([MACRO, SKF]), contract=CONTRACT,
                         round_no=1, at="t1")
        self.assertFalse(worse["adopted"], "未准入候选不能替换已采纳证据")
        self.assertEqual(worse["chosen"]["admitted"], 1)
        self.assertEqual(st.totals()["admitted"], 1)
        self.assertIn("attempts", st.items["s1"])

    def test_summary_substitution_does_not_replace(self):
        st = se.AdoptedStore()
        st.offer("s1", ok_result([YILI_ANNUAL]), contract=CONTRACT, round_no=0)
        summary = {"status": "SUCCESS", "result": "基于模型知识的摘要（无来源）"}
        offer = st.offer("s1", summary, contract=CONTRACT, round_no=1, at="t1")
        self.assertFalse(offer["adopted"])
        kept = st.result_for("s1")
        self.assertEqual(len(json.loads(kept["result"])), 1)

    def test_better_result_replaces_with_superseded_trail(self):
        st = se.AdoptedStore()
        st.offer("s1", ok_result([MACRO]), contract=CONTRACT, round_no=0)
        better = st.offer("s1", ok_result([YILI_ANNUAL]), contract=CONTRACT,
                          round_no=1)
        self.assertTrue(better["adopted"])
        self.assertEqual(st.items["s1"]["superseded"][0]["candidates"], 1)
        self.assertEqual(st.totals()["admitted"], 1)

    def test_zero_admitted_search_is_pending_not_rejected(self):
        """实机 e2e 观察：搜索候选未抓正文前 admitted=0 但**是 pending 不是废料**——
        采纳仍成立（供下游抓取转正），计数如实；返回值带 adopted_note 不带存疑章。"""
        st = se.AdoptedStore()
        junk = ok_result([MACRO, SKF])
        first = st.offer("s1", junk, contract=CONTRACT, round_no=0, at="t0")
        self.assertTrue(first["adopted"])
        self.assertEqual(first["chosen"]["admitted"], 0)
        self.assertEqual(st.totals()["admitted"], 0)
        # 之后拿到真年报 → 更优结果替换（旧记录进 superseded）
        better = st.offer("s1", ok_result([YILI_ANNUAL]), contract=CONTRACT,
                          round_no=1, at="t1")
        self.assertTrue(better["adopted"])
        self.assertEqual(better["previous_admitted"], 0)
        self.assertEqual(st.items["s1"]["superseded"][0]["admitted"], 0)
        self.assertEqual(st.totals()["admitted"], 1)


class TestCrossRoundRetention(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="se-round-"))
        self._old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(self.base)

    def tearDown(self):
        ws_mod.configure_workspace_root(self._old_root)
        shutil.rmtree(self.base, ignore_errors=True)

    def test_worse_later_round_keeps_adopted_evidence(self):
        orch = make_orch()
        seq = [ok_result([YILI_ANNUAL]), {"status": "SUCCESS", "result": "[]"}]
        orch._dispatch = lambda step, task_id: seq.pop(0)
        step = {"step_id": "1", "capability": "web_search",
                "instruction": "检索伊利 2024 年报", "iteration": 0}
        state = {"replan_used": 0}
        first = orch._dispatch_step_safe("研究伊利股份", step, "t-se-1", state)
        self.assertEqual(first["status"], "SUCCESS")
        second = orch._dispatch_step_safe("研究伊利股份", step, "t-se-1", state)
        self.assertEqual(second["status"], "SUCCESS", "保留的采纳证据仍是成功结果")
        self.assertTrue(second.get("search_evidence_retained"))
        self.assertEqual(second["retained_admitted"], 1)
        self.assertEqual(len(json.loads(second["result"])), 1)
        # 两本账都落盘：两次尝试 + 一条采纳
        proj = ws_mod.task_project_dir("t-se-1")
        attempts = json.loads((proj / se.ATTEMPTS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(attempts["count"], 2)
        self.assertEqual([a["stage"] for a in attempts["attempts"]],
                         [se.STAGE_INITIAL, se.STAGE_INITIAL])
        adopted = json.loads((proj / se.ADOPTED_FILE).read_text(encoding="utf-8"))
        self.assertEqual(adopted["totals"]["admitted"], 1)
        self.assertEqual(adopted["steps"]["1"]["admitted"], 1)

    def test_zero_admitted_flow_is_marked_pending_not_flagged(self):
        """首轮 0 准入：采纳成立（供抓取转正），带 adopted_note，不盖存疑章。"""
        orch = make_orch()
        seq = [ok_result([MACRO, SKF]), ok_result([MACRO, SKF])]
        orch._dispatch = lambda step, task_id: seq.pop(0)
        step = {"step_id": "1", "capability": "web_search",
                "instruction": "检索伊利 2024 年报", "iteration": 0}
        state = {"replan_used": 0}
        first = orch._dispatch_step_safe("研究伊利股份", step, "t-se-zero", state)
        self.assertEqual(first["status"], "SUCCESS")
        self.assertTrue(first.get("search_evidence_adopted"))
        self.assertEqual(first.get("adopted_admitted"), 0)
        self.assertIn("pending", str(first.get("adopted_note") or ""))
        self.assertNotIn("search_quality_untrusted", first)
        second = orch._dispatch_step_safe("研究伊利股份", step, "t-se-zero", state)
        self.assertEqual(second["status"], "SUCCESS")
        # 同分不覆盖：第二轮同等 0 准入，保留首轮结果（不进 retained 分支，
        # 因 retained 只在既有证据 admitted>0 时成立——0 准入谈不上"保住证据"）。
        self.assertTrue(second.get("search_evidence_adopted"))
        self.assertEqual(second.get("adopted_admitted"), 0)
        adopted = json.loads((ws_mod.task_project_dir("t-se-zero")
                              / se.ADOPTED_FILE).read_text(encoding="utf-8"))
        self.assertEqual(adopted["totals"]["admitted"], 0)
        self.assertEqual(adopted["totals"]["candidates"], 2)

    def test_budget_reserve_stops_retry_before_empty_dispatch(self):
        orch = make_orch()
        calls = []

        def _dispatch(step, task_id):
            calls.append(step)
            return {"status": "FAILED", "result": "No results"}

        orch._dispatch = _dispatch
        orch._search_budget_left = lambda task_id: (1, 25.0)   # 只剩补洞预留
        step = {"step_id": "1", "capability": "web_search",
                "instruction": "检索伊利 2024 年报", "iteration": 0}
        res = orch._dispatch_step_safe("研究伊利股份", step, "t-se-2",
                                       {"replan_used": 0})
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(len(calls), 1, "预算只够预留时不得再派发检索重试")

    def test_attempt_trail_classifies_failure(self):
        orch = make_orch()
        orch._dispatch = lambda step, task_id: {
            "status": "FAILED", "result": "no_results",
            "retryable": False, "retry_skipped": True}
        step = {"step_id": "1", "capability": "web_search",
                "instruction": "检索伊利 2024 年报", "iteration": 0}
        orch._dispatch_step_safe("研究伊利股份", step, "t-se-3",
                                 {"replan_used": 0})
        proj = ws_mod.task_project_dir("t-se-3")
        attempts = json.loads((proj / se.ATTEMPTS_FILE).read_text(encoding="utf-8"))
        self.assertGreaterEqual(attempts["count"], 1)
        rec = attempts["attempts"][0]
        self.assertEqual(rec["status"], "FAILED")
        self.assertEqual(rec["candidates"], 0)
        self.assertEqual(rec["admitted"], 0)
        self.assertIn("no_results", rec["reason"])


class TestStopDecision(unittest.TestCase):
    def setUp(self):
        self.orch = make_orch()
        self.orch._task_search_attempts = {
            "t-stop": [se.attempt_record(
                round_no=0, step_id="1", stage=se.STAGE_INITIAL,
                capability="web_search", query="伊利 2024 年报")]}
        self.orch._search_stale_rounds = {"t-stop": 1}

    def test_repeated_query_is_not_new_material(self):
        steps = [{"capability": "web_search",
                  "instruction": "[检索查询] 伊利 2024 年报"}]
        self.assertFalse(self.orch._next_round_has_new_material("t-stop", steps))
        stop, why = self.orch._should_stop_search_redo("t-stop", steps)
        self.assertTrue(stop)
        self.assertIn("没有新增合格来源", why)

    def test_new_query_is_new_material(self):
        steps = [{"capability": "web_search",
                  "instruction": "[检索查询] 伊利 2023 年报 现金流量"}]
        self.assertTrue(self.orch._next_round_has_new_material("t-stop", steps))
        self.assertFalse(self.orch._should_stop_search_redo("t-stop", steps)[0])

    def test_fetch_counts_as_new_material(self):
        steps = [{"capability": "web_fetch", "instruction": "抓取 https://x/1"}]
        self.assertTrue(self.orch._next_round_has_new_material("t-stop", steps))

    def test_report_only_round_is_not_stopped_by_this_rule(self):
        steps = [{"capability": "report_generator", "instruction": "重做报告"}]
        self.assertFalse(self.orch._should_stop_search_redo("t-stop", steps)[0])

    def test_stop_decision_pure(self):
        self.assertTrue(se.stop_decision(stale_rounds=1)[0])
        self.assertFalse(se.stop_decision(stale_rounds=0)[0])
        self.assertFalse(se.stop_decision(
            stale_rounds=3, new_search_material=True)[0])


class TestAuditPackaging(unittest.TestCase):
    def test_attempts_and_adopted_enter_package(self):
        base = Path(tempfile.mkdtemp(prefix="se-pkg-"))
        try:
            proj = base / "project"
            proj.mkdir(parents=True)
            (proj / se.ATTEMPTS_FILE).write_text('{"count": 1}',
                                                 encoding="utf-8")
            (proj / se.ADOPTED_FILE).write_text('{"totals": {}}',
                                                encoding="utf-8")
            members = dict((dst, src) for src, dst in
                           delivery_pipeline._exclusion_audit_members(base))
            self.assertIn("audit/search_attempts.json", members)
            self.assertIn("audit/search_adopted.json", members)
        finally:
            shutil.rmtree(base, ignore_errors=True)


class TestDegradationDisclosure(unittest.TestCase):
    def test_fallback_prompt_discloses_degradation(self):
        o = object.__new__(OrchestratorV2)
        o._messaging = _FakeMessaging()
        o._goal_wants_code = lambda goal: False
        o._sandbox_blocker = lambda: ""
        step = {"step_id": "2", "capability": "web_fetch",
                "instruction": "抓取财报页面"}
        alt = o._generation_fallback_step("分析某公司经营情况", step, "")
        ins = alt["instruction"]
        self.assertNotIn("不要提及搜索", ins)
        self.assertNotIn("基于已有知识直接完成", ins)
        self.assertIn("没有取得可核验的联网来源", ins)
        self.assertIn("未在本次检索中验证", ins)


if __name__ == "__main__":
    unittest.main(verbosity=2)
