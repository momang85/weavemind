# -*- coding: utf-8 -*-
"""任务14（P1 编排 dispatch 错配修复 + URL 规范化）测试。

设计：docs/arch_phase2_p1_dispatch_routing.md
- `CAPABILITY_WORKER_MAP` + `_routing_mismatch`：能力→执行者白名单判据；
- `_find_agent_in`：按**名字**在合法名单里找在册且空闲的执行者（错配改路由）；
- `_mark_step_routing`：错配记账（worker_mismatch / search_degraded，不改状态正文）；
- `task_state.derive_status(step_degraded=...)`：检索步被代做 → SUCCESS_WITH_ISSUES；
- `_clean_url` / `_clean_fetch_url`：NFKC 归一 + 箭头截断（两道同实现）；
- `_FETCH_POLICY_REJECT_MARKS`：net_policy 确定性拒绝文案覆盖面（策略拒绝也记死链）。
"""
import sys
import unittest
from unittest import mock

from test_orchestrator_v2 import FakeMessaging, make_orch

import orchestrator_v2 as ov
import task_state
from workers import web_fetch_worker

GOOD = "https://static.cninfo.com.cn/finalpage/2025-04-01/1223370519.PDF"


class TestCapabilityWorkerMap(unittest.TestCase):
    """设计里的白名单：6 个受限能力 + 不设限能力不得出现在键里。"""

    def test_map_matches_design(self):
        self.assertEqual(ov.CAPABILITY_WORKER_MAP, {
            "web_search": {"search_agent"},
            "web_fetch": {"webfetchworker"},
            "code_execution": {"codeexecworker"},
            "react_agent": {"reactagentworker"},
            "report_generator": {"reportgeneratorworker"},
            "data_analyzer": {"dataanalyzerworker"},
        })

    def test_content_summary_and_package_unrestricted(self):
        self.assertNotIn("content_summary", ov.CAPABILITY_WORKER_MAP)
        self.assertNotIn("package", ov.CAPABILITY_WORKER_MAP)


class TestRoutingMismatch(unittest.TestCase):
    """判据：能力有指定执行者、实际执行者不在其中（执行者为空不算错配）。"""

    def test_correct_worker_is_not_mismatch(self):
        pred = ov.OrchestratorV2._routing_mismatch
        self.assertFalse(pred("web_search", "search_agent"))
        self.assertFalse(pred("web_fetch", "webfetchworker"))
        self.assertFalse(pred("react_agent", "reactagentworker"))

    def test_wrong_worker_is_mismatch(self):
        pred = ov.OrchestratorV2._routing_mismatch
        # 任务12 实机错配：web_search 由摘要/代码执行者代做
        self.assertTrue(pred("web_search", "content_summarizer"))
        self.assertTrue(pred("web_search", "codeexecworker"))
        self.assertTrue(pred("web_fetch", "content_summarizer"))
        self.assertTrue(pred("react_agent", "content_summarizer"))
        self.assertTrue(pred("code_execution", "reportgeneratorworker"))

    def test_missing_agent_is_not_mismatch(self):
        pred = ov.OrchestratorV2._routing_mismatch
        self.assertFalse(pred("web_search", ""))
        self.assertFalse(pred("web_search", None))

    def test_unrestricted_capabilities(self):
        pred = ov.OrchestratorV2._routing_mismatch
        for cap in ("content_summary", "package", "data_loader", "file_io",
                    "model_trainer", "未知能力", ""):
            self.assertFalse(pred(cap, "anyone"), cap)


class _FakeRedisReg:
    def __init__(self, info=None, raises=False):
        self._info = dict(info or {})
        self._raises = raises

    def get_agent_info(self, agent_id):
        if self._raises:
            raise RuntimeError("redis down")
        return self._info.get(agent_id)


class _FakeSqliteReg:
    def __init__(self, info=None):
        self._info = dict(info or {})

    def get_agent(self, agent_id):
        return self._info.get(agent_id)


class TestFindAgentIn(unittest.TestCase):
    """按**名字**在名单里找在册且空闲的执行者；找不到返回 None（不阻断任务）。"""

    def _orch(self, redis_info=None, sqlite_info=None, redis_raises=False):
        o = make_orch()
        o._redis_reg = _FakeRedisReg(redis_info, raises=redis_raises)
        o._sqlite_reg = _FakeSqliteReg(sqlite_info)
        return o

    def test_redis_idle_worker_found(self):
        o = self._orch(redis_info={"search_agent": {"status": "idle"}})
        self.assertEqual(o._find_agent_in({"search_agent"}), "search_agent")

    def test_busy_redis_falls_back_to_sqlite(self):
        o = self._orch(
            redis_info={"search_agent": {"status": "busy"}},
            sqlite_info={"search_agent": {"status": "idle:step-1"}},
        )
        self.assertEqual(o._find_agent_in({"search_agent"}), "search_agent")

    def test_all_busy_returns_none(self):
        o = self._orch(
            redis_info={"search_agent": {"status": "busy"}},
            sqlite_info={"search_agent": {"status": "busy"}},
        )
        self.assertIsNone(o._find_agent_in({"search_agent"}))

    def test_registry_errors_are_tolerated(self):
        o = self._orch(redis_raises=True,
                       sqlite_info={"search_agent": {"status": "idle"}})
        self.assertEqual(o._find_agent_in({"search_agent"}), "search_agent")

    def test_only_listed_names_are_consulted(self):
        """名单外的空闲 worker 不参与选择（按名字查，不按能力查）。"""
        o = self._orch(redis_info={"content_summarizer": {"status": "idle"}})
        self.assertIsNone(o._find_agent_in({"search_agent"}))

    def test_empty_allowed_set(self):
        o = self._orch(redis_info={"search_agent": {"status": "idle"}})
        self.assertIsNone(o._find_agent_in(set()))
        self.assertIsNone(o._find_agent_in(None))


class TestMarkStepRouting(unittest.TestCase):
    """错配记账：打标记 + 告警，不改 status、不改正文、幂等。"""

    def _orch(self):
        o = make_orch()
        o._now_iso = lambda: "t"
        return o

    def _warnings(self, o):
        # push_progress 外层 type 是 "log"，告警语义在内层 payload.type
        return [
            m.get("payload", {}).get("message", "")
            for _, m in o._messaging.published
            if isinstance(m, dict)
            and m.get("payload", {}).get("type") == "warning"
        ]

    def test_search_mismatch_marked_and_degraded(self):
        o = self._orch()
        res = {"task_id": "1", "status": "SUCCESS",
               "agent_id": "content_summarizer", "result": "要点总结…"}
        out = o._mark_step_routing("web_search", "1", res, "t-mis1")
        self.assertIs(out, res)
        self.assertTrue(res["worker_mismatch"])
        self.assertEqual(res["mismatch_capability"], "web_search")
        self.assertEqual(res["mismatch_agent"], "content_summarizer")
        self.assertTrue(res["search_degraded"])
        self.assertEqual(res["status"], "SUCCESS")        # 不改状态
        self.assertEqual(res["result"], "要点总结…")       # 不改正文
        warns = self._warnings(o)
        self.assertEqual(len(warns), 1)
        self.assertIn("能力-执行者错配", warns[0])
        self.assertIn("web_search", warns[0])

    def test_fetch_mismatch_marked_without_search_degraded(self):
        o = self._orch()
        res = {"task_id": "2", "status": "SUCCESS",
               "agent_id": "content_summarizer", "result": "ok"}
        o._mark_step_routing("web_fetch", "2", res, "t-mis2")
        self.assertTrue(res["worker_mismatch"])
        self.assertNotIn("search_degraded", res)

    def test_correct_worker_untouched(self):
        o = self._orch()
        res = {"task_id": "3", "status": "SUCCESS",
               "agent_id": "search_agent", "result": "ok"}
        o._mark_step_routing("web_search", "3", res, "t-mis3")
        self.assertNotIn("worker_mismatch", res)
        self.assertNotIn("search_degraded", res)
        self.assertEqual(self._warnings(o), [])

    def test_missing_agent_and_non_dict_tolerated(self):
        o = self._orch()
        res = {"task_id": "4", "status": "SUCCESS", "result": "ok"}
        self.assertIs(o._mark_step_routing("web_search", "4", res, "t-mis4"), res)
        self.assertNotIn("worker_mismatch", res)
        junk = "plain text"
        self.assertEqual(o._mark_step_routing("web_search", "5", junk, "t-mis5"), junk)

    def test_idempotent_no_duplicate_alerts(self):
        o = self._orch()
        res = {"task_id": "6", "status": "SUCCESS",
               "agent_id": "codeexecworker", "result": "ok"}
        o._mark_step_routing("web_search", "6", res, "t-mis6")
        o._mark_step_routing("web_search", "6", res, "t-mis6")
        self.assertEqual(len(self._warnings(o)), 1)
        self.assertEqual(res["mismatch_agent"], "codeexecworker")


class TestFinalStatusStepDegraded(unittest.TestCase):
    """检索步被代做（search_degraded）→ 任务不再静默 SUCCESS。"""

    def test_derive_status_downgrades_on_step_degraded(self):
        self.assertEqual(
            task_state.derive_status(["SUCCESS"], step_degraded=True),
            task_state.SUCCESS_WITH_ISSUES)
        self.assertEqual(
            task_state.derive_status(
                ["SUCCESS"], acceptance={"overall": "pass"}, step_degraded=True),
            task_state.SUCCESS_WITH_ISSUES)

    def test_failure_still_wins(self):
        self.assertEqual(
            task_state.derive_status(["FAILED"], step_degraded=True),
            task_state.FAILED)

    def test_default_param_keeps_legacy_callers(self):
        """web_ui.py 与 scripts/ 的既有调用不传 step_degraded → 行为不变。"""
        self.assertEqual(task_state.derive_status(["SUCCESS"]), task_state.SUCCESS)
        self.assertEqual(
            task_state.derive_status(["SUCCESS"], {"overall": "pass"}),
            task_state.SUCCESS)
        self.assertEqual(
            task_state.derive_status(["SUCCESS"], None, None, "未验收草稿"),
            task_state.SUCCESS_WITH_ISSUES)

    def test_resolve_final_status_passes_degraded_through(self):
        self.assertEqual(
            ov.OrchestratorV2._resolve_final_status(
                False, {}, "", {}, step_degraded=True),
            "SUCCESS_WITH_ISSUES")
        self.assertEqual(
            ov.OrchestratorV2._resolve_final_status(False, {}, "", {}),
            "SUCCESS")

    def test_resolve_final_status_fallback_honors_degraded(self):
        """状态真源不可用时的兜底同样尊重 step_degraded。"""
        with mock.patch.dict(sys.modules, {"task_state": None}):
            self.assertEqual(
                ov.OrchestratorV2._resolve_final_status(
                    False, {}, "", {}, step_degraded=True),
                "SUCCESS_WITH_ISSUES")
            self.assertEqual(
                ov.OrchestratorV2._resolve_final_status(False, {}, "", {}),
                "SUCCESS")


class TestCleanUrlNormalization(unittest.TestCase):
    """NFKC 归一 + 箭头截断；两道实现（派发侧 / worker 侧）必须一致。"""

    CASES = (
        GOOD,
        GOOD + "；本次研究以该年报为准，请核对口径。",   # 既有 CJK 尾句（NFKC 后仍截断）
        GOOD + "）",
        "  " + GOOD + "  ",
        GOOD + "→本次按年报口径核对",                     # 任务14：箭头残留在原实现会漏
        "https://ｗｗｗ．eastmoney．com/quote",           # NFKC：全角 host 归一成 ASCII
        "https://www.eastmoney.com/data/2025Ｑ1.html",    # 全角字母/数字混排
    )

    def test_arrow_tail_is_cut(self):
        """U+2192 不在原 CJK 类内且 NFKC 无兼容分解 → 必须靠字符类截断。"""
        self.assertEqual(web_fetch_worker._clean_url(GOOD + "→说明"), GOOD)
        self.assertEqual(ov._clean_fetch_url(GOOD + "→说明"), GOOD)

    def test_nfkc_normalizes_fullwidth_host(self):
        self.assertEqual(web_fetch_worker._clean_url("https://ｗｗｗ．example．com/a"),
                         "https://www.example.com/a")

    def test_ascii_url_unchanged(self):
        self.assertEqual(web_fetch_worker._clean_url(GOOD), GOOD)
        self.assertEqual(ov._clean_fetch_url(GOOD), GOOD)

    def test_dispatch_and_worker_rules_agree(self):
        for raw in self.CASES:
            with self.subTest(raw=raw):
                self.assertEqual(ov._clean_fetch_url(raw),
                                 web_fetch_worker._clean_url(raw))


class TestPolicyRejectMarks(unittest.TestCase):
    """策略拒绝也记死链：标记元组覆盖 net_policy 的全部确定性拒绝文案。"""

    def _reject_reasons(self):
        import net_policy
        candidates = (
            "ftp://example.com/x",                  # 协议不允许
            "http://user:pass@example.com/x",       # 不得携带用户凭据
            "http:///path",                         # 缺少主机名
            "http://localhost/x",                   # 本机
            "http://169.254.169.254/latest",        # 链路本地
            "http://127.0.0.1:8080/x",              # 环回
            "http://[invalid",                      # urlsplit 异常 → URL 无法解析
        )
        reasons = []
        for u in candidates:
            try:
                d = net_policy.validate_public_url(u)
            except Exception:
                continue
            if not d.ok:
                reasons.append(str(d.reason or ""))
        return reasons

    def test_marks_cover_all_rejection_texts(self):
        import net_policy
        self.assertTrue(len(self._reject_reasons()) >= 6,
                        f"可离线构造的拒绝文案不足：{self._reject_reasons()}")
        for reason in self._reject_reasons():
            with self.subTest(reason=reason):
                self.assertTrue(
                    any(m in reason for m in ov._FETCH_POLICY_REJECT_MARKS),
                    f"拒绝文案未被标记覆盖：{reason}")

    def test_marks_are_specific_not_generic(self):
        """标记只描述 URL/协议/主机/凭据/公网判定，不含"端点/余额"等无关网络词
        （"无法解析"必须带 URL 前缀，避免命中 LLM 端点错误）。"""
        for m in ov._FETCH_POLICY_REJECT_MARKS:
            self.assertNotIn("端点", m)
            self.assertNotIn("余额", m)
            if "无法解析" in m:
                self.assertEqual(m, "URL 无法解析")


if __name__ == "__main__":
    unittest.main()
