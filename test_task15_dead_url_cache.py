# -*- coding: utf-8 -*-
"""任务15（P1 补漏：net_policy 策略拒绝也写死链缓存）测试。

设计：docs/arch_phase2_tasks15_16_17.md（任务15）
- `_FETCH_POLICY_REJECT_MARKS` 覆盖 `require_egress_ok` 的确定性出口拒签文案；
- 死链缓存**按任务隔离**（`_dead_urls_of` / `_forget_dead_urls`），不跨任务污染；
- 记账与门禁共用 `_step_fetch_url`（`[URL: …]` 优先，与 worker `_explicit_url` 同序）；
- 记账点下探到 `_dispatch_step_safe` 的每次尝试返回点：**策略拒绝不再重试**；
- 同一坏 URL 的**第二个抓取步**被门禁拦住，不再实际派发。

注意（任务16 语义预告）：HTTP 5xx / 超时是**瞬时**失败，值得重试——本测试断言
它们仍会重试（只记死链缓存，不拦截重试）；重试短路只对**确定性策略拒绝**生效。
"""
import inspect
import json
import threading
import unittest
from unittest import mock

from test_orchestrator_v2 import FakeMessaging, make_orch

import net_policy
import orchestrator_v2 as ov

BAD = "https://bad.example/annual.pdf"
REJECT = json.dumps({"status": "failed", "error": "解析到非公网地址：10.0.0.5"},
                    ensure_ascii=False)


def _new_orch(task_id="t15", retry=2):
    """供 `_execute_steps` 直跑的 `__new__` 编排器（fixture 对齐 test_p0）。"""
    from orchestrator_v2 import OrchestratorV2
    o = OrchestratorV2.__new__(OrchestratorV2)
    o._task_sources = {}
    o._task_sources_lock = threading.Lock()
    o._task_goals = {task_id: "目标"}
    o._task_prompt_hints = {}
    o._task_user_ids = {}
    o._max_parallel = 3
    o._messaging = FakeMessaging()
    o._now_iso = lambda: "t"
    o._max_retry = retry
    o._replan_depth = 0
    return o


class TestEgressRejectMark(unittest.TestCase):
    """任务15：`proxy_required` 的出口拒签也是确定性策略拒绝，必须能被记账。"""

    def test_require_egress_ok_reason_is_covered(self):
        with mock.patch.dict("os.environ", {"WM_CONTENT_FETCH_MODE": "proxy_required"}):
            with self.assertRaises(net_policy.NetworkPolicyError) as ctx:
                net_policy.require_egress_ok()
        reason = str(ctx.exception)
        self.assertTrue(any(mk in reason for mk in ov._FETCH_POLICY_REJECT_MARKS),
                        f"出口拒签文案未被死链标记覆盖：{reason}")

    def test_dns_failure_is_not_marked_dead(self):
        """DNS 解析失败是网络/离线类失败，不记死链（换时刻/换出口可能成功）。"""
        self.assertFalse(any("DNS" in mk for mk in ov._FETCH_POLICY_REJECT_MARKS))


class TestStepFetchUrl(unittest.TestCase):
    """记账与门禁共用 `_step_fetch_url`：`[URL: …]` 优先（worker 实际抓的地址）。"""

    def test_marker_wins_over_earlier_url(self):
        step = {"capability": "web_fetch",
                "instruction": "任务目标：看 https://first.example/a 与 [URL: https://chosen.example/b]"}
        self.assertEqual(ov.OrchestratorV2._step_fetch_url(step),
                         "https://chosen.example/b")

    def test_without_marker_first_url_is_used(self):
        step = {"capability": "web_fetch",
                "instruction": "抓 https://only.example/a 再看 https://x.example/b"}
        self.assertEqual(ov.OrchestratorV2._step_fetch_url(step),
                         "https://only.example/a")

    def test_marker_fullwidth_and_arrow_are_cleaned(self):
        step = {"capability": "web_fetch",
                "instruction": "任务目标：x\n[URL: https://ｂａｄ.example/a→说明]"}
        self.assertEqual(ov.OrchestratorV2._step_fetch_url(step),
                         "https://bad.example/a")

    def test_no_url_returns_empty(self):
        self.assertEqual(ov.OrchestratorV2._step_fetch_url({"instruction": "没有链接"}), "")


class TestDeadUrlScope(unittest.TestCase):
    """任务15 第 3 步：死链缓存与任务绑定，不跨任务污染。"""

    def test_cache_is_per_task(self):
        o = make_orch()
        self.assertTrue(o._mark_dead_fetch_url("t-a", BAD, "策略拒绝"))
        self.assertTrue(o._is_dead_fetch_url("t-a", BAD))
        self.assertFalse(o._is_dead_fetch_url("t-b", BAD),
                         "A 任务的死链不得传染给 B 任务")
        self.assertIsInstance(o._dead_fetch_urls, dict,
                              "必须是按任务分条的存储，不是实例级单集合")

    def test_mark_is_idempotent(self):
        o = make_orch()
        self.assertTrue(o._mark_dead_fetch_url("t", BAD))
        self.assertFalse(o._mark_dead_fetch_url("t", BAD),
                         "重复记账返回 False（不重复记日志）")

    def test_forget_only_clears_this_task(self):
        o = make_orch()
        o._mark_dead_fetch_url("t-a", BAD)
        o._mark_dead_fetch_url("t-b", BAD)
        o._forget_dead_urls("t-a")
        self.assertFalse(o._is_dead_fetch_url("t-a", BAD))
        self.assertTrue(o._is_dead_fetch_url("t-b", BAD))

    def test_run_clears_cache_at_task_start(self):
        """run() 起始必须清本任务死链缓存：重跑/续跑重新有机会抓（接线守卫）。"""
        src = inspect.getsource(ov.OrchestratorV2.run)
        self.assertIn("_forget_dead_urls(task_id)", src)


class TestNoteFetchDeadUrl(unittest.TestCase):
    """记账判定：确定性失败记账；瞬时失败不记账。"""

    def test_policy_reject_is_booked(self):
        o = make_orch()
        step = {"capability": "web_fetch", "step_id": "3",
                "instruction": f"抓取 [URL: {BAD}]"}
        url = o._note_fetch_dead_url(step, {"result": REJECT}, "t15-note")
        self.assertEqual(url, BAD)
        self.assertTrue(o._is_dead_fetch_url("t15-note", BAD))

    def test_http_status_field_is_booked(self):
        o = make_orch()
        step = {"capability": "web_fetch", "step_id": "3",
                "instruction": f"抓取 [URL: {BAD}]"}
        txt = json.dumps({"status": "failed", "http_status": 404,
                          "error": "HTTP 404：该地址不可用"}, ensure_ascii=False)
        o._note_fetch_dead_url(step, {"result": txt}, "t15-note")
        self.assertTrue(o._is_dead_fetch_url("t15-note", BAD))

    def test_transient_failure_is_not_booked(self):
        o = make_orch()
        step = {"capability": "web_fetch", "step_id": "3",
                "instruction": f"抓取 [URL: {BAD}]"}
        o._note_fetch_dead_url(step, {"result": "读取超时（60s）"}, "t15-note")
        self.assertFalse(o._is_dead_fetch_url("t15-note", BAD))

    def test_non_fetch_step_is_ignored(self):
        o = make_orch()
        step = {"capability": "web_search", "step_id": "3",
                "instruction": f"搜索 [URL: {BAD}]"}
        self.assertEqual(o._note_fetch_dead_url(step, {"result": REJECT}, "t15-note"), "")
        self.assertFalse(o._is_dead_fetch_url("t15-note", BAD))


class TestNoRetryOnPolicyReject(unittest.TestCase):
    """任务15 核心：确定性策略拒绝只派发一次；瞬时失败仍按 `_max_retry` 重试。"""

    def _orch(self, retry=2, task_id="t15-retry"):
        o = make_orch(_max_retry=retry, _replan_depth=0)
        o._now_iso = lambda: "t"
        return o

    def _step(self, sid="3"):
        return {"step_id": sid, "capability": "web_fetch",
                "instruction": f"抓取年报页面 [URL: {BAD}]", "timeout": 60}

    def _run(self, o, step, result_text, task_id="t15-retry"):
        calls = {"n": 0}

        def dispatch(_step, _tid):
            calls["n"] += 1
            return {"task_id": _step["step_id"], "status": "FAILED",
                    "result": result_text}

        o._dispatch = dispatch
        with mock.patch.object(ov.time, "sleep"):
            res = o._dispatch_step_safe("目标", step, task_id, {"replan_used": 0})
        return res, calls["n"]

    def test_policy_reject_not_retried(self):
        o = self._orch(retry=2)
        res, n = self._run(o, self._step(), REJECT)
        self.assertEqual(res["status"], "FAILED")
        self.assertEqual(n, 1, "策略拒绝是确定性结果：只派发一次，不重试")
        self.assertTrue(o._is_dead_fetch_url("t15-retry", BAD))

    def test_transient_failure_still_retries(self):
        o = self._orch(retry=2)
        _, n = self._run(o, self._step(), "读取超时（60s）")
        self.assertEqual(n, 1 + 2, "瞬时失败（超时）仍按 _max_retry 重试")
        self.assertFalse(o._is_dead_fetch_url("t15-retry", BAD),
                         "超时不是死链，不得记入缓存")

    def test_http_5xx_retried_but_booked(self):
        """5xx 是瞬时失败：仍重试；但同一 URL 记入死链缓存（后续步骤不再抓）。"""
        o = self._orch(retry=2)
        txt = json.dumps({"status": "failed", "http_status": 503,
                          "error": "HTTP 503：该地址暂不可用"}, ensure_ascii=False)
        _, n = self._run(o, self._step(), txt)
        self.assertEqual(n, 1 + 2, "5xx 仍按 _max_retry 重试（任务16 语义）")
        self.assertTrue(o._is_dead_fetch_url("t15-retry", BAD))


class TestSecondStepBlocked(unittest.TestCase):
    """任务15 第 5 步：同一坏 URL 的第二次抓取被门禁拦住，不再实际派发。"""

    def test_second_step_with_same_bad_url_is_not_dispatched(self):
        o = _new_orch("t15-gate", retry=2)
        calls = []

        def fake_safe(_goal, step, tid, state):
            calls.append(step["step_id"])
            return {"task_id": step["step_id"], "status": "FAILED", "result": REJECT}

        o._dispatch_step_safe = fake_safe
        steps = [
            {"step_id": "1", "capability": "web_fetch",
             "instruction": f"抓取 [URL: {BAD}]", "depends_on": [], "mode": "pipeline"},
            {"step_id": "3", "capability": "web_fetch",
             "instruction": f"再抓一次 [URL: {BAD}]", "depends_on": [], "mode": "pipeline"},
        ]
        results, failed = o._execute_steps(steps, "t15-gate", "目标")
        self.assertEqual(calls, ["1"],
                         "同一坏 URL 的第二次抓取必须被门禁拦住，不再派发")
        self.assertTrue(failed)
        by_id = {r.get("task_id"): r for r in results}
        self.assertIn("上次已失败", str(by_id["3"].get("result") or ""))
        self.assertTrue(o._is_dead_fetch_url("t15-gate", BAD))


if __name__ == "__main__":
    unittest.main(verbosity=1)
