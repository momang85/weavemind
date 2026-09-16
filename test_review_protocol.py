# -*- coding: utf-8 -*-
"""M0-b 评审协议回归：裁决白名单、修订后复评、状态按根任务隔离、各入口共用策略。

离线测试，不连 Redis、不调模型（评审回包用替身注入）。
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


def _orch(replies, *, mode="local", task_id="t-1"):
    """replies：按顺序返回的评审回包（None 表示超时，str 表示畸形回包）。"""
    o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
    o._critic_timeout = 1
    o._critic_enabled = True
    o._new_redis_sync = lambda: object()
    o._messaging = mock.MagicMock()
    o._now_iso = lambda: "2026-09-16T00:00:00Z"
    o._identity_mode = lambda: mode
    queue = list(replies)

    def _pop(r, key, deadline):
        if not queue:
            return None
        item = queue.pop(0)
        if item is None:
            return None
        return ("k", item if isinstance(item, str) else json.dumps(item))

    o._brpop_with_deadline = _pop
    o._revise_plan = mock.MagicMock(return_value=[{"step_id": "9", "capability": "web_search"}])
    return o


class TestVerdictWhitelist(unittest.TestCase):
    """只认 PASS/FAIL：别的裁决既不通过，也不进付费修订分支。"""

    def test_unknown_verdict_is_protocol_error(self):
        o = _orch([{"verdict": "MAYBE"}, {"verdict": "PASS"}], mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            out = o._review_plan("目标", STEPS, "t-1")
        self.assertEqual(out, STEPS)
        st = o._review_state("t-1")
        self.assertEqual(st["verdict"], "DEGRADED")
        self.assertIn("MAYBE", st["degraded_reason"])
        o._revise_plan.assert_not_called()
        self.assertFalse(o.review_passed_for("t-1", STEPS))

    def test_missing_verdict_is_protocol_error(self):
        o = _orch([{"scores": {}}, {"verdict": "PASS"}], mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            o._review_plan("目标", STEPS, "t-1")
        self.assertIn("缺失", o._review_state("t-1")["degraded_reason"])

    def test_non_dict_payload_is_protocol_error(self):
        o = _orch(["[1,2,3]"], mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            out = o._review_plan("目标", STEPS, "t-1")
        self.assertEqual(out, STEPS)
        self.assertIn("非字典", o._review_state("t-1")["degraded_reason"])
        o._revise_plan.assert_not_called()

    def test_bank_refuses_unknown_verdict(self):
        o = _orch([{"verdict": "MAYBE"}], mode="bank")
        with mock.patch("orchestrator_v2.push_progress"):
            with self.assertRaises(ov.ReviewRequiredError):
                o._review_plan("目标", STEPS, "t-1")


class TestRevisionNeedsItsOwnPass(unittest.TestCase):
    """FAIL 走修订，但修订稿必须**再评一次**并拿到 PASS。"""

    def test_revised_plan_must_pass_review(self):
        o = _orch([{"verdict": "FAIL", "suggestions": ["补来源"]},
                   {"verdict": "PASS", "scores": {}}], mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            out = o._review_plan("目标", STEPS, "t-1")
        o._revise_plan.assert_called_once()
        self.assertEqual(out, [{"step_id": "9", "capability": "web_search"}])
        self.assertTrue(o.review_passed_for("t-1", out), "PASS 应绑定在修订后的计划上")
        self.assertFalse(o.review_passed_for("t-1", STEPS),
                         "PASS 不得回溯适用于被否掉的初稿")

    def test_revised_plan_failing_review_is_not_a_pass(self):
        o = _orch([{"verdict": "FAIL", "suggestions": ["补来源"]},
                   {"verdict": "FAIL", "suggestions": ["还是不行"]}], mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            out = o._review_plan("目标", STEPS, "t-1")
        self.assertEqual(out, [{"step_id": "9", "capability": "web_search"}])
        self.assertFalse(o.review_passed_for("t-1", out))
        self.assertIn("修订后仍未通过", o._review_state("t-1")["degraded_reason"])

    def test_bank_refuses_revised_plan_without_pass(self):
        o = _orch([{"verdict": "FAIL", "suggestions": ["补来源"]},
                   {"verdict": "FAIL", "suggestions": ["还是不行"]}], mode="bank")
        with mock.patch("orchestrator_v2.push_progress"):
            with self.assertRaises(ov.ReviewRequiredError):
                o._review_plan("目标", STEPS, "t-1")

    def test_revision_budget_is_bounded(self):
        o = _orch([{"verdict": "FAIL", "suggestions": ["x"]},
                   {"verdict": "FAIL", "suggestions": ["y"]},
                   {"verdict": "PASS"}], mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            o._review_plan("目标", STEPS, "t-1")
        self.assertEqual(o._revise_plan.call_count, 1, "最多一次付费修订")


class TestPerRootTaskState(unittest.TestCase):
    """评审状态按根任务隔离：另一任务的降级不得清掉本任务的结论。"""

    def test_degradation_does_not_leak_between_tasks(self):
        o = _orch([{"verdict": "PASS"}], mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            o._review_plan("目标", STEPS, "task-A")
        self.assertTrue(o.review_passed_for("task-A", STEPS))

        o2 = _orch([None], mode="local")           # 超时 → 降级
        o2._task_review = o._task_review              # 同一实例上的两个任务
        with mock.patch("orchestrator_v2.push_progress"):
            o2._review_plan("目标", STEPS, "task-B")
        self.assertEqual(o._review_state("task-A")["verdict"], "PASS",
                         "task-A 的评审结论不应被 task-B 的降级覆盖")
        self.assertEqual(o._review_state("task-B")["verdict"], "DEGRADED")
        self.assertTrue(o.review_passed_for("task-A", STEPS))


class TestPolicyVersionAndFingerprint(unittest.TestCase):
    def test_old_policy_pass_is_not_reused(self):
        o = _orch([{"verdict": "PASS"}], mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            o._review_plan("目标", STEPS, "t-1")
        st = o._review_state("t-1")
        st["policy_version"] = "review-policy/v0"
        self.assertFalse(o.review_passed_for("t-1", STEPS))

    def test_fingerprint_detects_plan_change(self):
        o = _orch([{"verdict": "PASS"}], mode="local")
        with mock.patch("orchestrator_v2.push_progress"):
            o._review_plan("目标", STEPS, "t-1")
        self.assertFalse(o.review_passed_for("t-1", [{"step_id": "99"}]))


class TestIdentityModeIsNotSwallowed(unittest.TestCase):
    """配置非法/不可判定 → 拒绝，绝不默认个人模式。"""

    def test_illegal_mode_raises(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        with mock.patch("task_context.default_mode", side_effect=RuntimeError("mode=bnak 非法")):
            with self.assertRaises(ov.ReviewRequiredError):
                o._identity_mode()

    def test_unknown_mode_value_raises(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        with mock.patch("task_context.default_mode", return_value="bnak"):
            with self.assertRaises(ov.ReviewRequiredError):
                o._identity_mode()

    def test_bank_and_personal_resolve(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        with mock.patch("task_context.default_mode", return_value="bank"):
            self.assertTrue(o._review_is_required())
        with mock.patch("task_context.default_mode", return_value="local"):
            self.assertFalse(o._review_is_required())


class TestSharedPolicyAcrossEntryPoints(unittest.TestCase):
    """critic 关闭 / 直出计划 / 模板 / 恢复 都要走同一策略。"""

    def test_critic_disabled_refuses_in_bank(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: "bank"
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        with mock.patch("orchestrator_v2.push_progress"):
            with self.assertRaises(ov.ReviewRequiredError):
                o._require_review_or_refuse("t-1", "critic 已关闭", plan=STEPS)

    def test_critic_disabled_marks_degraded_in_personal(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: "local"
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        with mock.patch("orchestrator_v2.push_progress"):
            o._require_review_or_refuse("t-1", "critic 已关闭", plan=STEPS)
        st = o._review_state("t-1")
        self.assertEqual(st["verdict"], "DEGRADED")
        self.assertIn("critic 已关闭", st["degraded_reason"])
        self.assertFalse(o.review_passed_for("t-1", STEPS))

    def test_template_and_direct_plan_call_the_same_policy(self):
        src = (ROOT / "orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("_require_review_or_refuse("), 5,
                                "critic 关闭/直出/模板两条路径都要接入统一策略")

    def test_resume_reuses_only_valid_pass(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: "local"
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        # 复用的前提：PASS 绑定在**恢复后的计划版本**上（R1 起按版本号判定）
        pv = o._bump_plan_version("t-1", "任务起始规划")
        saved = {"verdict": "PASS", "policy_version": ov.REVIEW_POLICY_VERSION,
                 "mode": "local", "plan_fingerprint": o._plan_fingerprint(STEPS),
                 "plan_version": pv, "rounds": 1}
        with mock.patch("orchestrator_v2.push_progress"):
            self.assertTrue(o._restore_review_state("t-1", saved, STEPS))
        self.assertEqual(o._review_state("t-1")["verdict"], "PASS")
        self.assertTrue(o.review_passed_for("t-1", STEPS),
                        "恢复后的 PASS 仍应按指纹绑定在这版计划上")
        self.assertTrue(o.review_scope_ok("t-1"))

    def test_resume_rejects_pass_bound_to_other_plan_version(self):
        """R1：checkpoint 的 PASS 绑定在别的计划版本上 → 不复用（异版 PASS 不放行）。"""
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: "local"
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        o._bump_plan_version("t-1", "任务起始规划")
        saved = {"verdict": "PASS", "policy_version": ov.REVIEW_POLICY_VERSION,
                 "mode": "local", "plan_fingerprint": o._plan_fingerprint(STEPS),
                 "plan_version": 1}
        # 落盘之后计划被改写（反思追加/替换步骤）→ 当前版本变成 v2
        o._bump_plan_version("t-1", "反思追加/替换步骤")
        with mock.patch("orchestrator_v2.push_progress"):
            self.assertFalse(o._restore_review_state("t-1", saved, STEPS))
        self.assertEqual(o._review_state("t-1")["verdict"], "DEGRADED")
        self.assertFalse(o.review_scope_ok("t-1"), "异版 PASS 不得当作覆盖本版计划")

    def test_resume_rejects_legacy_pass_without_plan_version(self):
        """旧 checkpoint 没有 plan_version → 无法证明覆盖本版计划，按不复用处理。"""
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: "local"
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        o._bump_plan_version("t-1", "任务起始规划")
        legacy = {"verdict": "PASS", "policy_version": ov.REVIEW_POLICY_VERSION,
                  "mode": "local", "plan_fingerprint": o._plan_fingerprint(STEPS)}
        with mock.patch("orchestrator_v2.push_progress"):
            self.assertFalse(o._restore_review_state("t-1", legacy, STEPS))

    def test_review_scope_tracks_plan_rewrite(self):
        """PASS → 计划被反思改写 → 该 PASS 不再覆盖当前版本计划。"""
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: "local"
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        o._bump_plan_version("t-1", "任务起始规划")
        with mock.patch("orchestrator_v2.push_progress"):
            o._review_bind_pass("t-1", STEPS)
            self.assertTrue(o.review_scope_ok("t-1"))
            o._bump_plan_version("t-1", "反思追加/替换步骤")
        self.assertFalse(o.review_scope_ok("t-1"),
                         "计划改写后旧 PASS 不得再被当成覆盖本版计划")

    def test_mechanical_postprocessing_keeps_pass_valid(self):
        """机械加工（依赖连线/目标注入等回填字段）不算改版：PASS 必须仍然有效。

        否则每一次真实运行都会被判成"没有绑定的 PASS"——判定本身失去意义。
        """
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: "local"
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        o._bump_plan_version("t-1", "任务起始规划")
        reviewed = [dict(s) for s in STEPS]
        with mock.patch("orchestrator_v2.push_progress"):
            o._review_bind_pass("t-1", reviewed)
        executed = [dict(s) for s in reviewed]
        for s in executed:
            s["iteration"] = 1
            s["status"] = "SUCCESS"
            s["result"] = {"status": "SUCCESS", "result": "内容"}
            s["depends_on"] = list(s.get("depends_on") or [])
        self.assertEqual(o._plan_fingerprint(reviewed), o._plan_fingerprint(executed),
                         "执行期回填的字段不得改变计划指纹")
        self.assertTrue(o.review_passed_for("t-1", executed))
        self.assertTrue(o.review_scope_ok("t-1"))

    def test_resume_rejects_stale_or_other_mode_pass(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: "local"
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        pv = o._bump_plan_version("t-1", "任务起始规划")
        stale = {"verdict": "PASS", "policy_version": "review-policy/v0",
                 "mode": "local", "plan_fingerprint": o._plan_fingerprint(STEPS),
                 "plan_version": pv}
        with mock.patch("orchestrator_v2.push_progress"):
            self.assertFalse(o._restore_review_state("t-1", stale, STEPS))
        self.assertEqual(o._review_state("t-1")["verdict"], "DEGRADED")

    def test_resume_with_bank_mode_refuses_without_valid_pass(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: "bank"
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        with mock.patch("orchestrator_v2.push_progress"):
            with self.assertRaises(ov.ReviewRequiredError):
                o._restore_review_state("t-1", {}, STEPS)

    def test_checkpoint_carries_review_state(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._now_iso = lambda: "T"
        o._review_state("t-1").update({"verdict": "PASS", "plan_fingerprint": "fp"})
        cp = o._checkpoint_payload("t-1", "目标", None, [], {})
        self.assertEqual(cp["review"]["verdict"], "PASS")
        self.assertIn("plan_version", cp,
                      "checkpoint 必须记下当前计划版本（与 review.plan_version 对比用）")


class TestReviewStatusIsVisible(unittest.TestCase):
    """降级不能只留日志：交付物本体要写明，且状态落盘可对账。"""

    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="wm_rev_"))

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _orch(self, mode="local"):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._identity_mode = lambda: mode
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "2026-09-16T00:00:00Z"
        return o

    def test_degraded_case_is_written_into_delivery_and_state_file(self):
        o = self._orch()
        o._review_mark_degraded("t-1", "评审超时（30s）", plan=STEPS)
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: self.tmp):
            out = o._with_review_note("t-1", "交付说明")
        self.assertIn("评审未完成（降级）", out)
        self.assertIn("评审超时（30s）", out)
        self.assertIn("须经人工复核", out)
        data = json.loads((self.tmp / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["verdict"], "DEGRADED")
        self.assertEqual(data["task_id"], "t-1")

    def test_critic_disabled_label_is_distinct(self):
        o = self._orch()
        with mock.patch("orchestrator_v2.push_progress"):
            o._require_review_or_refuse("t-2", "critic 已关闭（system.critic=false）", plan=STEPS)
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: self.tmp):
            out = o._with_review_note("t-2", "交付说明")
        self.assertIn("未经评审（critic 关闭）", out)

    def test_pass_adds_no_noise_but_still_records_state(self):
        o = self._orch()
        o._review_bind_pass("t-3", STEPS)
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: self.tmp):
            out = o._with_review_note("t-3", "交付说明")
        self.assertEqual(out, "交付说明")
        data = json.loads((self.tmp / "review_state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["label"], "PASS")


if __name__ == "__main__":
    unittest.main()
