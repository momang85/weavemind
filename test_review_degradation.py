# -*- coding: utf-8 -*-
"""V2-2 评审退化回归：超时/异常/降级裁决不得被当作"通过"。

依据视觉实测（A/B/C 均有 Critic 30 秒 timeout 后 proceeding）与任务规划 V2：
个人模式可降级交付但必须如实标注；银行口径下必需评审未完成要**按策略拒绝**，
不能把超时/不可用当通过。离线测试，不连 Redis、不调模型。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import orchestrator_v2 as ov  # noqa: E402

STEPS = [{"step_id": "1", "capability": "web_search", "instruction": "查"}]


def _orch(review_payload, *, mode="local"):
    o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
    o._critic_timeout = 1
    o._new_redis_sync = lambda: object()
    o._messaging = mock.MagicMock()
    o._now_iso = lambda: "2026-09-15T00:00:00Z"
    if review_payload is None:
        o._brpop_with_deadline = lambda r, key, deadline: None
    else:
        o._brpop_with_deadline = lambda r, key, deadline: ("k", json.dumps(review_payload))
    o._identity_mode = lambda: "bank" if mode == "bank" else "personal"
    o._review_is_required = lambda: mode == "bank"
    o._revise_plan = mock.MagicMock(return_value=None)
    return o


class TestReviewTimeout(unittest.TestCase):
    def test_personal_mode_marks_degraded_and_continues(self):
        o = _orch(None, mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            out = o._review_plan("目标", STEPS, "t-1")
        self.assertEqual(out, STEPS, "个人模式可继续，但必须标记降级")
        self.assertIn("超时", o._review_state("t-1")["degraded_reason"])
        o._revise_plan.assert_not_called()

    def test_bank_mode_refuses_on_timeout(self):
        o = _orch(None, mode="bank")
        with mock.patch("orchestrator_v2.push_progress"):
            with self.assertRaises(ov.ReviewRequiredError):
                o._review_plan("目标", STEPS, "t-2")
        self.assertEqual(o._review_state("t-2")["verdict"], "", "拒绝路径不应被标成可降级继续")


class TestReviewException(unittest.TestCase):
    def test_personal_mode_marks_degraded_on_exception(self):
        o = _orch({"verdict": "PASS"}, mode="local")
        o._brpop_with_deadline = mock.MagicMock(side_effect=RuntimeError("boom"))
        with mock.patch("orchestrator_v2.push_progress"):
            out = o._review_plan("目标", STEPS, "t-3")
        self.assertEqual(out, STEPS)
        self.assertIn("评审异常", o._review_state("t-3")["degraded_reason"])

    def test_bank_mode_refuses_on_exception(self):
        o = _orch({"verdict": "PASS"}, mode="bank")
        o._brpop_with_deadline = mock.MagicMock(side_effect=RuntimeError("boom"))
        with mock.patch("orchestrator_v2.push_progress"):
            with self.assertRaises(ov.ReviewRequiredError):
                o._review_plan("目标", STEPS, "t-4")


class TestDegradedAndErrorVerdicts(unittest.TestCase):
    def test_error_verdict_is_not_a_pass(self):
        """Critic 报 ERROR（含身份/协议拒绝）：按评审未完成处置，不进修订分支。"""
        o = _orch({"verdict": "ERROR", "error": "身份校验未通过"}, mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            out = o._review_plan("目标", STEPS, "t-5")
        self.assertEqual(out, STEPS)
        self.assertIn("ERROR", o._review_state("t-5")["degraded_reason"])
        o._revise_plan.assert_not_called()

    def test_error_verdict_refuses_in_bank_mode(self):
        o = _orch({"verdict": "ERROR", "error": "身份校验未通过"}, mode="bank")
        with mock.patch("orchestrator_v2.push_progress"):
            with self.assertRaises(ov.ReviewRequiredError):
                o._review_plan("目标", STEPS, "t-6")

    def test_degraded_verdict_does_not_trigger_paid_revision(self):
        o = _orch({"verdict": "DEGRADED", "summary": "评审系统不可用"}, mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            out = o._review_plan("目标", STEPS, "t-7")
        self.assertEqual(out, STEPS)
        self.assertIn("DEGRADED", o._review_state("t-7")["degraded_reason"])
        o._revise_plan.assert_not_called()

    def test_pass_and_fail_are_unchanged(self):
        o = _orch({"verdict": "PASS", "scores": {}}, mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            self.assertEqual(o._review_plan("目标", STEPS, "t-8"), STEPS)
            self.assertEqual(o._review_state("t-8")["degraded_reason"], "")

        o2 = _orch({"verdict": "FAIL", "suggestions": ["补充来源"]}, mode="local")
        o2._revise_plan = mock.MagicMock(return_value=[{"step_id": "9"}])
        with mock.patch("orchestrator_v2.push_progress"):
            self.assertEqual(o2._review_plan("目标", STEPS, "t-9"), [{"step_id": "9"}])
        o2._revise_plan.assert_called_once()


class TestFallbackReviewDoesNotPass(unittest.TestCase):
    def test_fallback_verdict_is_degraded(self):
        import critic_agent
        agent = critic_agent.CriticAgent.__new__(critic_agent.CriticAgent)
        review = agent._fallback_review("plan-1", "LLM 不可用")
        self.assertNotEqual(review["verdict"], "PASS", "评审不可用不得默认通过")
        self.assertEqual(review["verdict"], "DEGRADED")
        self.assertIn("未完成评审", review["summary"])

    def test_orchestrator_wires_degraded_state(self):
        src = (ROOT / "orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn("_review_state", src)
        self.assertIn("ReviewRequiredError", src)
        self.assertNotIn('message": f"Review timeout ({self._critic_timeout}s), proceeding"', src,
                         "超时不得再直接 proceeding")


if __name__ == "__main__":
    unittest.main()
