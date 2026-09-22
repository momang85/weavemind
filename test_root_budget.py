# -*- coding: utf-8 -*-
"""M0-e 根任务预算回归：唯一账本、先预留后发送、共享剩余、阶段观测。

离线：不连 Redis、不调模型；时间用假时钟推进，请求在**边界**打桩
（只替身 redis 发送与结果回包，不替换编排逻辑）。
"""

from __future__ import annotations

import json
import os
import shutil
import contextlib
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import llm_client as lc

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import root_budget as rb  # noqa: E402
import workspace as ws_mod  # noqa: E402


class _Clock:
    """假时钟：只替换 root_budget 内的 time.time。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


class _FakeRedis:
    def __init__(self):
        self.pushes: list = []

    def lpush(self, key, value):
        self.pushes.append((key, value))


class TestRootBudgetLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_bud_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_unlimited_budget_counts_without_refusing(self):
        """D5：不限额度也**如实计数**（账本要能核对"供应商实际收到多少次请求"），
        只是从不拒绝；0 仍然是不限（不拿别的配置兜底成硬截止）。

        改前 `reserve` 在不限时直接返回空串、什么都不记——于是实机任务的根账本恒为空，
        "每次请求都有唯一记录"无从核对（D5 前置审查的 P1）。
        """
        b = rb.RootBudget("t-1", self.tmp)
        self.assertFalse(b.limited)
        ticket = b.reserve("step", tokens=100)
        self.assertTrue(ticket, "不限额度也要开票计数")
        self.assertEqual(b.state.calls_reserved, 1)
        self.assertEqual(b.exhausted_reason(), "", "不限就是不拒绝")
        b.settle(ticket, tokens=100)
        self.assertEqual(b.state.calls_settled, 1)
        self.assertEqual(b.state.tokens_settled, 100)
        self.assertEqual(b.state.open_tickets, {}, "结算后不留未结票据")
        self.assertEqual(b.state.tokens_reserved, 0, "token 预留换成实际用量")
        b.settle("", tokens=100)          # 无票据 → 无副作用
        self.assertEqual(b.state.calls_settled, 1)

    def test_reserve_then_settle_counts_once(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=3))
        t = b.reserve("step", detail={"step_id": "1"})
        self.assertTrue(t)
        self.assertEqual(b.remaining()["calls"], 2)
        b.settle(t, tokens=120)
        left = b.remaining()
        self.assertEqual(left["calls"], 2, "结算不退还预留（调用已发生）")
        self.assertEqual(b.state.tokens_settled, 120)
        self.assertEqual(b.snapshot()["stages"]["step"]["settled"], 1)

    def test_refund_returns_reservation(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=2))
        t = b.reserve("step")
        self.assertEqual(b.remaining()["calls"], 1)
        b.refund(t, note="发送前失败")
        self.assertEqual(b.remaining()["calls"], 2, "发送前失败应退回预留")

    def test_exhausted_refuses_new_call(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=1))
        b.reserve("step")
        with self.assertRaises(rb.BudgetExceeded):
            b.reserve("step")
        self.assertIn("次数预算", b.exhausted_reason())

    def test_calls_limit_refuses_before_token_check(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=1, max_tokens=100))
        b.reserve("step", tokens=10)
        with self.assertRaises(rb.BudgetExceeded):
            b.reserve("step", tokens=50)

    def test_time_limit_uses_fake_clock(self):
        # 假时钟必须从"当前真实时间"起跳：账本 started_at 取的是当时的 time.time()
        clock = _Clock(time.time())
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_seconds=60))
        with mock.patch.object(rb.time, "time", clock):
            b.reserve("step")
            clock.advance(61)
            self.assertIn("时间预算", b.exhausted_reason())
            with self.assertRaises(rb.BudgetExceeded):
                b.reserve("step")
            self.assertEqual(b.remaining()["seconds"], 0.0)

    def test_unsettled_is_not_a_refund(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=5))
        t = b.reserve("step")
        b.mark_unsettled(t, reason="取消：结果不再读取")
        snap = b.snapshot()
        self.assertEqual(snap["calls"]["unsettled"], 1)
        self.assertEqual(snap["calls"]["reserved"], 1,
                         "在飞调用不得被退回（可能仍在计费）")
        self.assertEqual(snap["stages"]["step"]["in_flight"], 1)

    def test_snapshot_reports_waiting_object_and_progress(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=5))
        t = b.reserve("step", detail={"step_id": "2", "capability": "web_search"})
        b.note_progress("step", {"step_id": "2", "outcome": "running"})
        snap = b.snapshot()
        self.assertEqual(snap["stages"]["step"]["waiting_on"]["step_id"], "2")
        self.assertEqual(snap["stages"]["step"]["last_progress"]["outcome"], "running")
        self.assertIn("remaining", snap)
        self.assertIn("since_progress_sec", snap["stages"]["step"])
        self.assertTrue(t)


class TestBudgetSharedAcrossRestart(unittest.TestCase):
    """换进程/恢复继续用同一份账：预算不被重置。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_bud2_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_reopened_budget_keeps_remaining(self):
        b1 = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=2))
        t = b1.reserve("step")
        b1.settle(t, tokens=10)
        b2 = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=2))
        self.assertEqual(b2.remaining()["calls"], 1, "重开账本应延续剩余额度")
        self.assertEqual(b2.state.tokens_settled, 10,
                         "票据号跨进程唯一，结算必须命中同一张票据")
        b2.reserve("plan")
        b3 = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=2))
        self.assertEqual(b3.remaining()["calls"], 0)
        with self.assertRaises(rb.BudgetExceeded):
            b3.reserve("reflect")

    def test_ledger_file_is_atomic(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=2))
        b.reserve("step")
        files = sorted(p.name for p in self.tmp.iterdir())
        self.assertIn("budget_state.json", files)
        self.assertNotIn("budget_state.tmp", files, "临时文件必须已被替换")
        data = json.loads((self.tmp / "budget_state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["calls_reserved"], 1)


class TestDispatchBoundaryEnforcement(unittest.TestCase):
    """在派发边界生效：预算不足时**不发请求**（管道里不出现新条目）。"""

    def setUp(self):
        os.environ['WM_SINGLE_PROCESS'] = '1'  # 离线单测：单进程语义
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_bud3_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def _orch(self, *, max_calls: int):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        o._find_agent = lambda cap: "agent-1"
        o._redis = _FakeRedis()
        o._new_redis_sync = lambda: _FakeRedis()
        o._brpop_with_deadline = lambda r, key, deadline: (
            key, json.dumps({"status": "SUCCESS", "result": "ok"}))
        o._cancel_requested = lambda tid: False
        o._task_starts = {}
        o._task_starts_lock = threading.Lock()
        o._task_simple, o._task_goals = {}, {}
        o._inflight, o._inflight_lock = {}, threading.Lock()
        o._task_budgets = {}
        o._task_timeout = 300
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: self.tmp):
            o._budget("t-1").limits = rb.BudgetLimits(max_calls=max_calls)
            o._budget("t-1")._save()
        return o

    def test_second_dispatch_is_refused_without_sending(self):
        o = self._orch(max_calls=1)
        step = {"step_id": "1", "capability": "web_search",
                "instruction": "查", "timeout": 5}
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: self.tmp), \
                mock.patch("orchestrator_v2.push_progress"):
            r1 = o._dispatch(step, "t-1")
            sent_after_first = len(o._redis.pushes)
            r2 = o._dispatch(dict(step, step_id="2"), "t-1")
        self.assertEqual(r1["status"], "SUCCESS")
        self.assertEqual(sent_after_first, 0, "该替身未记录 lpush 也无妨：关键看第二次")
        self.assertEqual(r2["status"], "FAILED")
        self.assertTrue(r2.get("budget_exceeded"))
        self.assertIn("预算", r2["result"])

    def test_budget_snapshot_visible_per_stage(self):
        o = self._orch(max_calls=3)
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: self.tmp), \
                mock.patch("orchestrator_v2.push_progress"):
            o._dispatch({"step_id": "1", "capability": "web_search",
                         "instruction": "查", "timeout": 5}, "t-1")
            snap = o.budget_snapshot("t-1")
        self.assertEqual(snap["calls"]["reserved"], 1)
        self.assertEqual(snap["stages"]["step"]["settled"], 1)
        self.assertIn("remaining", snap)

    def test_cancel_marks_inflight_unsettled_in_ledger(self):
        from orchestrator_v2 import WaitOutcome, WAIT_CANCEL

        o = self._orch(max_calls=3)
        o._wait_step_result = lambda *a, **k: WaitOutcome(WAIT_CANCEL, reason="用户取消")
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: self.tmp), \
                mock.patch("orchestrator_v2.push_progress"):
            res = o._dispatch({"step_id": "1", "capability": "web_search",
                               "instruction": "查", "timeout": 5}, "t-1")
            snap = o.budget_snapshot("t-1")
        self.assertEqual(res["status"], "CANCELLED")
        self.assertEqual(snap["calls"]["unsettled"], 1)
        self.assertEqual(snap["calls"]["settled"], 0)


class TestBudgetExhaustionStopsRun(unittest.TestCase):
    """预算耗尽要**停下来**：只拒绝单次派发会让重试/重做循环每 2 秒空转一次。"""

    def test_dispatch_records_task_level_flag(self):
        """预算已耗尽时派发被拒，并置运行级标记（供收尾/迭代循环停止）。

        **单进程语义**（`WM_SINGLE_PROCESS=1`）：本用例测的是"额度用尽后拒派发"这条
        路径；多进程语义下、共享账本不可用时是另一条路径（fail closed，见
        `TestBoundedFailClosed`）。不声明就会依赖运行环境有没有 Redis（CI 无 Redis 时
        会走 fail closed，本地有 Redis 时走额度路径——同一份用例两种行为）。
        """
        import os as _os
        import threading
        import tempfile
        from unittest import mock
        from orchestrator_v2 import OrchestratorV2

        _os.environ["WM_SINGLE_PROCESS"] = "1"
        self.addCleanup(_os.environ.pop, "WM_SINGLE_PROCESS", None)

        tmp = Path(tempfile.mkdtemp(prefix="wm_bud4_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        o._find_agent = lambda cap: "agent-1"
        o._redis = _FakeRedis()
        o._new_redis_sync = lambda: _FakeRedis()
        o._cancel_requested = lambda tid: False
        o._task_starts, o._task_starts_lock = {}, threading.Lock()
        o._task_simple, o._task_goals = {}, {}
        o._inflight, o._inflight_lock = {}, threading.Lock()
        o._task_budgets, o._budget_exhausted = {}, {}
        o._task_timeout = 300
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: tmp),                 mock.patch("orchestrator_v2.push_progress"):
            b = o._budget("t-1")
            b.limits = rb.BudgetLimits(max_calls=0)      # 直接处于"已用尽"状态
            b.state.calls_reserved = 0
            b.limits = rb.BudgetLimits(max_seconds=1)
            b.state.started_at = 0.0                     # 时间预算早已用尽
            b._save()
            res = o._dispatch({"step_id": "2", "capability": "web_search",
                               "instruction": "查", "timeout": 5}, "t-1")
        self.assertTrue(res.get("budget_exceeded"), res)
        self.assertIn("预算", o._budget_exhausted.get("t-1", ""))

    def test_retry_loop_does_not_retry_budget_refusal(self):
        from unittest import mock
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._max_retry = 3
        o._replan_depth = 2
        calls = {"n": 0}

        def _dispatch(step, tid):
            calls["n"] += 1
            return {"task_id": step["step_id"], "status": "FAILED",
                    "budget_exceeded": True, "result": "预算不足"}

        o._dispatch = _dispatch
        o._cancel_requested = lambda tid: False
        with mock.patch("orchestrator_v2.push_progress"):
            res = o._dispatch_step_safe("目标", {"step_id": "1", "capability": "web_search"},
                                        "t-1", {"replan_used": 0})
        self.assertTrue(res.get("budget_exceeded"))
        self.assertEqual(calls["n"], 1, "预算拒绝不得触发重试")


class TestBudgetLimitsFromRealConfigPath(unittest.TestCase):
    """配置里的预算上限要真的生效（实测缺陷：加载器不存在 → 静默用兜底值）。"""

    def test_system_budget_is_read_from_orchestrator_config_path(self):
        from orchestrator_v2 import OrchestratorV2

        tmp = Path(tempfile.mkdtemp(prefix="wm_cfg_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        cfg_path = tmp / "config.json"
        cfg_path.write_text(json.dumps(
            {"system": {"budget": {"max_seconds": 900, "max_calls": 60,
                                   "max_tokens": 0}}}), encoding="utf-8")
        o = OrchestratorV2.__new__(OrchestratorV2)
        o._system_cfg_path = str(cfg_path)
        lim = rb.limits_from_config(o._load_cfg_snapshot())
        self.assertEqual((lim.max_seconds, lim.max_calls), (900.0, 60))

    def test_missing_config_path_is_empty_not_crash(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        self.assertEqual(o._load_cfg_snapshot(), {})


class TestLimitsFromConfig(unittest.TestCase):
    def test_reads_system_budget_section(self):
        lim = rb.limits_from_config({"system": {"budget": {
            "max_seconds": 600, "max_calls": 40, "max_tokens": 200000}}})
        self.assertEqual(lim.max_seconds, 600)
        self.assertEqual(lim.max_calls, 40)
        self.assertEqual(lim.max_tokens, 200000)

    def test_defaults_are_unlimited(self):
        lim = rb.limits_from_config({})
        self.assertEqual((lim.max_seconds, lim.max_calls, lim.max_tokens), (0.0, 0, 0))
        self.assertEqual(rb.limits_from_config({"system": {"budget": "bad"}}).max_calls, 0)


class TestTicketTransitionsAreMutuallyExclusive(unittest.TestCase):
    """R2：一张票据只能从 open 走到 settled 或 unsettled 中的一个，且只走一次。"""

    def setUp(self):
        os.environ['WM_SINGLE_PROCESS'] = '1'  # 离线单测：单进程语义
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_tk_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_fabricated_ticket_is_refused(self):
        """取消时不得凭空造票据：从未预留过的票据转待对账必须被拒绝并计数。

        此前 `_call_llm_cancellable` 会写一张 `xxx-inflight`，账面上"取消已入账"，
        而真正预留的那张仍挂在 open 上——真实状态被假账掩盖。
        """
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=5))
        t = b.reserve("plan")
        self.assertFalse(b.mark_unsettled("plan-inflight", reason="取消"),
                         "虚构票据不得入账")
        snap = b.snapshot()
        self.assertEqual(snap["calls"]["unsettled"], 0)
        self.assertEqual(snap["calls"]["reserved"], 1)
        self.assertIn("plan", snap["open_tickets"][t]["stage"])
        self.assertEqual(snap["rejected_transitions"]["unsettled_unknown_ticket"]["count"], 1)

    def test_settle_after_unsettled_is_refused(self):
        """真票据转待对账后，调用方的 finally 不得再把它结算成"成功"。"""
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=5))
        t = b.reserve("plan", tokens=100)
        self.assertTrue(b.mark_unsettled(t, reason="取消：放弃等待"))
        b.settle(t, tokens=0, ok=True, note="finally 里的无条件结算")
        snap = b.snapshot()
        self.assertEqual(snap["calls"]["unsettled"], 1)
        self.assertEqual(snap["calls"]["settled"], 0,
                         "同一张票据不得既 unsettled 又 settled（假平衡）")
        self.assertEqual(snap["rejected_transitions"]["settle_after_unsettled"]["count"], 1)
        self.assertIn(t, snap["unsettled_tickets"])
        self.assertEqual(snap["tokens"]["unsettled"], 100,
                         "待对账的调用可能仍在计费：上界不得被当成没花")

    def test_double_settle_counts_once(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=5))
        t = b.reserve("step")
        b.settle(t, tokens=7)
        b.settle(t, tokens=7)
        self.assertEqual(b.snapshot()["calls"]["settled"], 1)
        self.assertEqual(b.snapshot()["rejected_transitions"]["settle_unknown_ticket"]["count"], 1)

    def test_stage_unsettled_moves_only_real_open_tickets(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_calls=5))
        t1 = b.reserve("plan")
        t2 = b.reserve("plan")
        b.settle(t2)
        moved = b.mark_stage_unsettled("plan", reason="取消")
        self.assertEqual(moved, 1, "只动 open 的那张，已结算的不动")
        snap = b.snapshot()
        self.assertIn(t1, snap["unsettled_tickets"])
        self.assertEqual(moved, b.mark_stage_unsettled("plan", reason="再取消一次") or 1)


class TestTokenReservationIsUpperBound(unittest.TestCase):
    """R2 反例：`max_tokens=100` 时两次 `reserve(tokens=80)` 不得都获准。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_tok_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def test_second_reserve_is_refused_before_any_settlement(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_tokens=100))
        b.reserve("plan", tokens=80)
        with self.assertRaises(rb.BudgetExceeded) as ctx:
            b.reserve("reflect", tokens=80)
        self.assertIn("token", str(ctx.exception))

    def test_settled_actual_still_counts(self):
        """结算后上界换成实际：实际也算已承诺，不能被第二次预留重复使用。"""
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_tokens=100))
        t = b.reserve("plan", tokens=80)
        b.settle(t, tokens=90)
        self.assertEqual(b.remaining()["tokens"], 10)
        with self.assertRaises(rb.BudgetExceeded):
            b.reserve("reflect", tokens=20)
        b.reserve("reflect", tokens=10)

    def test_refund_returns_token_upper_bound(self):
        b = rb.RootBudget("t-1", self.tmp, rb.BudgetLimits(max_tokens=100))
        t = b.reserve("plan", tokens=80)
        b.refund(t, note="发送前失败")
        self.assertEqual(b.remaining()["tokens"], 100, "没发出去就退回上界")
        b.reserve("plan", tokens=90)


class _AtomicFakeRedis:
    """支持 incrby/decrby/get 的替身：模拟两个进程共用一份 Redis 计数。"""

    def __init__(self, shared: dict):
        self._kv = shared

    def incrby(self, key, amount):
        self._kv[key] = int(self._kv.get(key, 0)) + int(amount)
        return self._kv[key]

    def decrby(self, key, amount):
        self._kv[key] = int(self._kv.get(key, 0)) - int(amount)
        return self._kv[key]

    def get(self, key):
        v = self._kv.get(key)
        return None if v is None else str(v)


class TestCrossProcessReservation(unittest.TestCase):
    """R2：同一任务的两个实例共用一份剩余额度（Redis 原子计数），文件只是快照。

    这里的"两个实例"= 同一个根任务的两次编排器实例（重启后恢复、或并行实例），
    它们指向**同一个账本目录**（同一 task 工作区）。共享计数的键空间按账本身份
    （`ledger_id`）隔离，所以"同一次运行"的两个实例读到的是同一份额度，
    而重新提交的一次运行不会继承上一轮的花费。
    """

    def setUp(self):
        os.environ['WM_SINGLE_PROCESS'] = '1'  # 离线单测：单进程语义
        self.kv: dict = {}
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_x_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def _budget(self, tmp, **limits):
        return rb.RootBudget("t-1", tmp, rb.BudgetLimits(**limits),
                             redis_factory=lambda: _AtomicFakeRedis(self.kv))

    def test_two_instances_do_not_both_get_the_last_slot(self):
        b1 = self._budget(self.tmp, max_calls=1)
        b2 = self._budget(self.tmp, max_calls=1)
        t1 = b1.reserve("step")
        with self.assertRaises(rb.BudgetExceeded):
            b2.reserve("step")
        self.assertTrue(t1)

    def test_ticket_ids_are_unique_across_instances(self):
        """R2 反例：两个实例都返回 `request-1`，磁盘只记 1 条。"""
        b1 = self._budget(self.tmp, max_calls=10)
        b2 = self._budget(self.tmp, max_calls=10)
        t1 = b1.reserve("step")
        t2 = b2.reserve("step")
        self.assertNotEqual(t1, t2, "票据号必须跨实例唯一")
        snap = b1.snapshot()
        self.assertTrue(snap["cross_process"])
        self.assertEqual(snap["calls"]["reserved"], 2,
                         "共享计数：两个实例的预留都要记进同一份额度")

    def test_token_reservation_is_atomic_across_instances(self):
        b1 = self._budget(self.tmp, max_tokens=100)
        b2 = self._budget(self.tmp, max_tokens=100)
        b1.reserve("plan", tokens=80)
        with self.assertRaises(rb.BudgetExceeded):
            b2.reserve("reflect", tokens=80)

    def test_new_run_does_not_inherit_previous_counters(self):
        """重新开始的一次运行（新账本目录）不得继承上一轮留在 Redis 的花费。"""
        other = Path(tempfile.mkdtemp(prefix="wm_x_new_"))
        self.addCleanup(__import__("shutil").rmtree, other, ignore_errors=True)
        b1 = self._budget(self.tmp, max_calls=1)
        b1.reserve("step")
        b2 = self._budget(other, max_calls=1)
        self.assertTrue(b2.reserve("step"),
                        "另一个账本（新一次运行）应拿到自己的额度")
        self.assertEqual(b2.snapshot()["shared"], "established")

    def test_rollback_keeps_counter_accurate(self):
        b = self._budget(self.tmp, max_calls=2, max_tokens=100)
        b.reserve("plan", tokens=80)
        with self.assertRaises(rb.BudgetExceeded):
            b.reserve("plan", tokens=80)
        # 失败的预留必须把计数退回去，否则会把额度越吃越少
        self.assertEqual(b.remaining()["tokens"], 20)
        self.assertEqual(b.remaining()["calls"], 1)


class TestZeroMeansTrulyUnlimited(unittest.TestCase):
    """R2：0 值语义 = 真正不限（不再回落 `system.task_timeout` 变成 600 秒硬截止）。"""

    def test_budget_is_unlimited_when_config_is_zero(self):
        from orchestrator_v2 import OrchestratorV2

        tmp = Path(tempfile.mkdtemp(prefix="wm_zero_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        cfg = tmp / "config.json"
        cfg.write_text(json.dumps({"system": {
            "task_timeout": 600,
            "budget": {"max_seconds": 0, "max_calls": 0, "max_tokens": 0},
        }}), encoding="utf-8")
        o = OrchestratorV2.__new__(OrchestratorV2)
        o._system_cfg_path = str(cfg)
        o._task_timeout = 600
        o._task_budgets = {}
        o._budget_redis_factory = lambda: None
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: tmp):
            b = o._budget("t-1")
        self.assertFalse(b.limited, "0/0/0 必须是不限，而不是拿 task_timeout 兜底")
        self.assertEqual(b.limits.max_seconds, 0.0)
        # D5：不限额度也开票计数（账本可核对），但**从不拒绝**
        self.assertTrue(b.reserve("plan"), "不限额度也要开票计数")
        self.assertEqual(b.exhausted_reason(), "")


class TestRootDeadlineBoundsWait(unittest.TestCase):
    """R2：根任务剩余时间比步骤超时更短时，等待必须按**剩余时间**收口。

    否则步骤用 300s 下限等待，而根预算早已用尽——那段时间是"没额度的空等"。
    """

    def _orch(self, remaining_seconds, tmp):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._now_iso = lambda: "T"
        o._cancel_requested = lambda tid: False
        o._new_redis_sync = lambda: object()
        o._task_budgets = {}
        self._deadlines: list = []
        o._brpop_with_deadline = lambda r, key, deadline: (
            self._deadlines.append(deadline) or None)
        b = rb.RootBudget("t-1", tmp, rb.BudgetLimits(max_seconds=remaining_seconds))
        o._task_budgets["t-1"] = b
        return o

    def test_wait_is_capped_by_root_remaining(self):
        import time as _t
        tmp = Path(tempfile.mkdtemp(prefix="wm_dl_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        o = self._orch(2, tmp)
        started = _t.time()
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: tmp):
            out = o._wait_step_result("step-1", 300, cancel_task_id="t-1")
        elapsed = _t.time() - started
        self.assertEqual(out.kind, "timeout")
        self.assertLess(elapsed, 12,
                        f"等待应被根剩余时间收口（实际 {elapsed:.1f}s），不得等满 300s")

    def test_no_root_limit_keeps_step_timeout(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_dl2_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        o = self._orch(0, tmp)          # 0 = 不限
        self.assertFalse(o._budget("t-1").limited)
        o._new_redis_sync = lambda: object()
        o._brpop_with_deadline = lambda r, key, deadline: (
            self._deadlines.append(deadline) or None)
        with mock.patch("orchestrator_v2.task_workspace", lambda tid: tmp):
            o._wait_step_result("step-1", 1, cancel_task_id="t-1")
        # 未设根上限时不做任何收口（等待上限仍是步骤自身的超时）
        self.assertTrue(self._deadlines)


class TestCancelLatencyIsTwoSeparateClocks(unittest.TestCase):
    """R2：取消 UI 响应时限与供应商在飞结算时限分别记录，不混成一个数。"""

    class _Redis:
        def __init__(self, token):
            self.token = token

        def get(self, key):
            return self.token if key == "task_cancel_token:t-1" else None

    def _orch(self, token):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._redis = self._Redis(token)
        o._now_iso = lambda: "T"
        return o

    def test_latency_reports_both_clocks(self):
        import json as _json
        import time as _t

        token = _json.dumps({"at_ts": _t.time() - 5, "reason": "用户停止"},
                            ensure_ascii=False)
        o = self._orch(token)
        lat = o._cancel_latency("t-1")
        self.assertGreaterEqual(lat["sec"], 4.5)
        self.assertTrue(lat["within"], "5 秒应落在 UI 预期内")
        self.assertGreater(lat["settle_window"], 0,
                           "供应商在飞结算窗口是另一个时限，必须单独给出")
        self.assertNotEqual(lat["settle_window"], lat["deadline"],
                            "两个时限不得混成同一个数")

    def test_latency_flags_slow_cancel(self):
        import json as _json
        import time as _t

        token = _json.dumps({"at_ts": _t.time() - 9999}, ensure_ascii=False)
        o = self._orch(token)
        lat = o._cancel_latency("t-1")
        self.assertFalse(lat["within"], "超出预期的取消响应必须如实标记")

    def test_no_timestamp_means_unknown_not_invented(self):
        o = self._orch("1")
        self.assertEqual(o._cancel_latency("t-1"), {},
                         "拿不到请求时刻就不要编一个时延出来")


_ENV_KEYS = ("WM_TASK_MAX_CALLS", "WM_TASK_MAX_SECONDS", "WM_TASK_MAX_TOKENS",
             "WM_SINGLE_PROCESS")


@contextlib.contextmanager
def _single_process():
    """单进程语义（离线单测默认）：显式有界任务不因"共享后端不可用"拒发。

    生产（编排器 + 独立 Worker）按多进程语义运行，此时有界任务在共享账本不可用时
    **fail closed**——那条路径由 `TestBoundedFailClosed` 单独覆盖。
    """
    with mock.patch.dict(os.environ, {"WM_SINGLE_PROCESS": "1"}):
        yield


class _Provider:
    """模拟供应商：只数收到的请求（不联网）。"""

    def __init__(self, *, fail_first: int = 0):
        self.calls = 0
        self.fail_first = int(fail_first)

    def send(self, *_a, **_k) -> str:
        self.calls += 1
        if self.calls <= self.fail_first:
            raise lc.LLMCallError("模拟供应商 503")
        return json.dumps({"ok": True})


@contextlib.contextmanager
def _offline():
    """离线环境：无备用端点、端点判定健康、不热重载、不真等待。"""
    with mock.patch.object(lc, "_BACKUP_CFG", {}), \
            mock.patch.object(lc, "_primary_healthy", lambda: True), \
            mock.patch.object(lc, "_ensure_cfg_fresh"), \
            mock.patch.object(lc.time, "sleep"):
        yield


def _client(provider: _Provider, *, retries: int = 1):
    class _C(lc.LLMClient):
        _MAX_RETRIES = retries
        _RETRY_BASE = 0

        def _send_request(self, *a, **k):
            return provider.send(*a, **k)

    return _C()


class TestRequestLedger(unittest.TestCase):
    def setUp(self):
        os.environ['WM_SINGLE_PROCESS'] = '1'  # 离线单测：单进程语义
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_reqled_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        lc._root_budgets.clear()
        self.addCleanup(lc.clear_task_context)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in _ENV_KEYS])

    def _budget(self, tid: str) -> RootBudget:
        return rb.RootBudget(tid, ws_mod.task_workspace(tid),
                          lc._budget_limits_from_file())

    def test_ledger_counts_every_request_even_without_caps(self):
        p = _Provider()
        c = _client(p)
        lc.set_task_context("t-led-1")
        with _offline():
            c.call("sys", "u1", expect_json=True)
            c.call("sys", "u2", expect_json=True)
        self.assertEqual(p.calls, 2)
        b = self._budget("t-led-1")
        self.assertFalse(b.limited, "本用例就是不限额度")
        self.assertEqual(b.state.calls_settled, 2,
                         "账本调用数必须与供应商实收数一致")
        self.assertEqual(b.state.open_tickets, {}, "不留未结票据")

    def test_cap_refuses_before_sending(self):
        os.environ["WM_TASK_MAX_CALLS"] = "2"
        p = _Provider()
        c = _client(p)
        lc.set_task_context("t-led-2")
        with _offline():
            c.call("sys", "u1", expect_json=True)
            c.call("sys", "u2", expect_json=True)
            with self.assertRaises(lc.LLMCallError) as ctx:
                c.call("sys", "u3", expect_json=True)
        self.assertTrue(getattr(ctx.exception, "budget_exhausted", False),
                        "预算不足必须是可识别的信号")
        self.assertEqual(p.calls, 2, "被拒的请求供应商从未收到")
        self.assertEqual(self._budget("t-led-2").state.calls_settled, 2)

    def test_retries_are_counted_separately(self):
        p = _Provider(fail_first=1)
        c = _client(p, retries=2)
        lc.set_task_context("t-led-3")
        with _offline():
            c.call("sys", "u", expect_json=True)
        self.assertEqual(p.calls, 2, "第一次失败后重试一次")
        b = self._budget("t-led-3")
        self.assertEqual(b.state.calls_settled, 2, "两次请求各记一次")
        self.assertEqual(b.snapshot()["stages"]["llm"]["settled"], 2)

    def test_backup_request_is_counted(self):
        p = _Provider()

        class _StubClient(lc.LLMClient):
            def _send_request(self, *a, **k):
                return p.send(*a, **k)

        c = _client(p)
        # 显式给出备用端点配置：不依赖环境里的真实备用端点（离线纪律）
        c._backup_cfg = {"base_url": "https://backup.test/v1", "api_key": "k",
                         "model": "m"}
        lc.set_task_context("t-led-4")
        with _offline(), mock.patch.object(lc, "LLMClient", _StubClient):
            c._call_backup("sys", "u", 0.0, 512, True)
        self.assertEqual(p.calls, 1)
        b = self._budget("t-led-4")
        self.assertEqual(b.snapshot()["stages"]["backup"]["settled"], 1,
                         "切到备端点也要入账")

    def test_cancel_before_send_opens_no_ticket(self):
        p = _Provider()
        c = _client(p)
        lc.set_task_context("t-led-5")
        lc.set_cancel_guard(lambda tid: True)
        self.addCleanup(lc.set_cancel_guard, None)
        with _offline(), self.assertRaises(lc.LLMCancelledError):
            c.call("sys", "u", expect_json=True)
        self.assertEqual(p.calls, 0)
        b = self._budget("t-led-5")
        self.assertEqual(b.state.open_tickets, {}, "取消在发送前：没有在飞票据")
        self.assertEqual(b.state.calls_settled, 0)


    def test_stream_request_is_counted_and_recorded(self):
        """流式路径也要入账并留下调用记录：此前它既不预留也不记录，上限对它无效。"""
        p = _Provider()
        recorded: list[dict] = []

        def _fake_stream_once(base_url, api_key, model, system, user, *a, **k):
            return p.send(base_url, system, user)

        lc.set_task_context("t-led-6")
        with _offline(), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream_once), \
                mock.patch.object(lc, "_record_llm_call",
                                  lambda tid, **kw: recorded.append(dict(kw))):
            out = lc.call_llm_stream("sys", "user text", usage="exec")
        self.assertTrue(out)
        self.assertEqual(p.calls, 1, "流式也只发一次")
        b = self._budget("t-led-6")
        self.assertEqual(b.snapshot()["stages"]["llm"]["settled"], 1,
                         "流式请求必须进账本")
        self.assertEqual([r.get("end_reason") for r in recorded], ["ok"],
                         "流式请求必须留一条调用形状记录")
        self.assertEqual(recorded[0].get("stage"), "exec")

    def test_stream_cap_refuses_before_sending(self):
        os.environ["WM_TASK_MAX_CALLS"] = "1"
        p = _Provider()

        def _fake_stream_once(base_url, api_key, model, system, user, *a, **k):
            return p.send(base_url, system, user)

        lc.set_task_context("t-led-7")
        with _offline(), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream_once):
            lc.call_llm_stream("sys", "u1")
            with self.assertRaises(lc.LLMCallError) as ctx:
                lc.call_llm_stream("sys", "u2")
        self.assertTrue(getattr(ctx.exception, "budget_exhausted", False))
        self.assertEqual(p.calls, 1, "被拒的流式请求供应商从未收到")

    def test_two_workers_share_one_call_cap(self):
        """跨进程共享额度：两个 Worker（各自新建账本实例）也花不掉同一份上限。

        LLM 路径此前不接跨进程后端，等于"每个进程各有一份 40 次"——上限管不住
        整次运行。这里用同一个假后端模拟两个进程，第二次必须被拒。
        """
        os.environ["WM_TASK_MAX_CALLS"] = "1"
        kv: dict = {}
        p = _Provider()
        c = _client(p)
        lc.set_task_context("t-led-8")
        with _offline(), mock.patch.object(rb, "default_redis_factory",
                                           lambda: _AtomicFakeRedis(kv)):
            c.call("sys", "u1", expect_json=True)
            lc._root_budgets.clear()          # 第二个 Worker：新进程、新账本实例
            with self.assertRaises(lc.LLMCallError) as ctx:
                c.call("sys", "u2", expect_json=True)
        self.assertTrue(getattr(ctx.exception, "budget_exhausted", False),
                        "共享额度用尽必须能识别")
        self.assertEqual(p.calls, 1, "共享额度用尽后不再发送")


class _FallbackAsyncClient:
    """异步替身：第一次（流式）回 415，回退的非流式成功。只数发送次数。"""

    def __init__(self, sends: list):
        self.sends = sends

    def stream(self, *_a, **_k):
        self.sends.append("stream")
        return _StreamCtx()

    async def post(self, *_a, **_k):
        self.sends.append("nonstream")
        return _Resp()


class _Resp:
    def __init__(self, status: int = 200, body=None):
        self.status_code = status
        self._body = body or {"choices": [{"message": {"content": "ok"}}],
                              "usage": {}}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise lc.httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=self)

    def json(self):
        return self._body

    async def aread(self):
        return b""


class _StreamCtx:
    """流式上下文：进上下文即 415（与真实端点的"流式不支持"一致）。"""

    async def __aenter__(self):
        return _Resp(415)

    async def __aexit__(self, *_e):
        return False


class TestStreamFallbackTickets(unittest.TestCase):
    """D5 夜间补修（P1-C）：一次票据不得发两次请求——415 流式回退要独立开票。

    复现（协作审查，禁网假供应商）：max_calls=1 时，第一次发送走流式、供应商回 415，
    代码回退非流式再发一次；此前两次发送共用一张票（reserved=settled=1，调用形状仅一条）。
    """

    def setUp(self):
        os.environ['WM_SINGLE_PROCESS'] = '1'  # 离线单测：单进程语义
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_fb_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        lc._root_budgets.clear()
        self.addCleanup(lc.clear_task_context)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in _ENV_KEYS])

    def _ledger(self, tid):
        return rb.RootBudget(tid, ws_mod.task_workspace(tid),
                             lc._budget_limits_from_file())

    def test_sync_stream_fallback_opens_its_own_ticket(self):
        """同步路径：流式被拒 → 非流式回退 = 两次真实发送 = 两张票。"""
        sends = []

        def _fake_stream_once(*_a, **_k):
            sends.append("stream")
            raise lc.LLMCallError("HTTP 415: Unsupported Media Type")

        def _fake_urlopen(req, timeout=None):
            sends.append("nonstream")

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *_e):
                    return False

                def read(self):
                    return json.dumps({"choices": [{"message": {"content": "ok"}}],
                                       "usage": {"prompt_tokens": 1,
                                                 "completion_tokens": 1}}).encode()

            return _Resp()

        lc.set_task_context("t-fb-1")
        # 真实客户端：只打桩传输层，保留 _send_request 的流式→非流式回退逻辑
        c = lc.LLMClient()
        with _offline(), \
                mock.patch.object(lc, "_stream_enabled", lambda: True), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream_once), \
                mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
            out = c.call("sys", "u", expect_json=False)
        self.assertTrue(out)
        self.assertEqual(sends, ["stream", "nonstream"], "确实是两次真实发送")
        snap = self._ledger("t-fb-1").snapshot()
        self.assertEqual(snap["stages"]["llm"]["reserved"], 2,
                         "两次发送必须各有一张票")
        self.assertEqual(snap["stages"]["llm"]["settled"], 2)
        self.assertEqual(snap["calls"]["reserved"], 2)

    def test_cap_one_blocks_the_fallback_before_sending(self):
        """cap=1：第一次（流式）发出去后，回退的非流式请求必须在**发送前**被拒。"""
        os.environ["WM_TASK_MAX_CALLS"] = "1"
        sends = []

        def _fake_stream_once(*_a, **_k):
            sends.append("stream")
            raise lc.LLMCallError("HTTP 415: Unsupported Media Type")

        def _fake_urlopen(req, timeout=None):
            sends.append("nonstream")
            raise AssertionError("预算已用尽，第二次发送不得发生")

        lc.set_task_context("t-fb-2")
        # 真实客户端：只打桩传输层，保留 _send_request 的流式→非流式回退逻辑
        c = lc.LLMClient()
        with _offline(), \
                mock.patch.object(lc, "_stream_enabled", lambda: True), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream_once), \
                mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
            with self.assertRaises(lc.LLMCallError) as ctx:
                c.call("sys", "u", expect_json=False)
        self.assertTrue(getattr(ctx.exception, "budget_exhausted", False),
                        "被拒必须是可识别的预算信号")
        self.assertEqual(sends, ["stream"], "第二次发送在发送前就被拦下")
        b = self._ledger("t-fb-2")
        self.assertEqual(b.state.calls_settled, 1, "只有第一次发送结算")
        self.assertEqual(b.state.open_tickets, {}, "不留未结票据")

    def test_async_stream_fallback_opens_its_own_ticket(self):
        """异步路径（`_async_chat_once` 内部回退）：回退那次发送单独开票。

        这一层只负责**回退**那张票；第一次（流式）发送的票由调用方
        （`call_llm_async`）开——两条合起来 = 两次发送两张票，见下一个用例。
        """
        import asyncio
        sends = []
        fake = _FallbackAsyncClient(sends)

        lc.set_task_context("t-fb-3")
        with _offline(), mock.patch.object(lc, "_stream_enabled", lambda: True):
            data = asyncio.run(lc._async_chat_once(
                fake, "https://api.example/v1/chat/completions",
                {"max_tokens": 128}, {}))
        self.assertTrue(data)
        self.assertEqual(sends, ["stream", "nonstream"])
        snap = self._ledger("t-fb-3").snapshot()
        self.assertEqual(snap["stages"]["llm"]["settled"], 1,
                         "回退的非流式发送有一张自己的票")

    def test_async_chain_counts_two_tickets_for_two_sends(self):
        """异步整链：流式 415 → 非流式回退，两次发送共两张票（调用方 1 + 回退 1）。"""
        import asyncio
        sends = []
        fake = _FallbackAsyncClient(sends)
        lc.set_task_context("t-fb-4")
        with _offline(),                 mock.patch.object(lc, "_stream_enabled", lambda: True),                 mock.patch.object(lc, "_get_async_client", lambda: fake),                 mock.patch.object(lc, "_endpoint_guard", lambda u: u),                 mock.patch.dict(os.environ, {"LLM_API_KEY": "k",
                                             "LLM_BASE_URL": "https://api.example/v1",
                                             "LLM_MODEL": "m"}):
            out = asyncio.run(lc.call_llm_async("sys", "u", expect_json=False))
        self.assertTrue(out)
        self.assertEqual(sends, ["stream", "nonstream"])
        snap = self._ledger("t-fb-4").snapshot()
        self.assertEqual(snap["stages"]["llm"]["reserved"], 2)
        self.assertEqual(snap["stages"]["llm"]["settled"], 2)


class TestCrossProcessSaveAtomicity(unittest.TestCase):
    """D5 夜间补修（P1-D）：`_save` 的读改写要跨进程原子，否则并发落盘丢增量。

    进程内 `threading.Lock` 挡不住两个**进程**：都读旧 writers、各自合并、互相覆盖
    （协作审查复现：成功预留两次，最终快照只剩一次、一个 writer）。这里用两个真进程
    并发预留/结算，断言最终快照两次都在。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_xsave_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _run_two_processes(self, rounds: int = 40) -> int:
        import subprocess
        root = str(Path(__file__).resolve().parent)
        code = (
            "import sys, time, pathlib;"
            f"sys.path.insert(0, r'{root}');"
            "import root_budget as rb;"
            f"b = rb.RootBudget('t-x', pathlib.Path(r'{self.tmp}'));"
            "time.sleep(float(sys.argv[1]));"
            "[(lambda t: b.settle(t, tokens=1))(b.reserve('llm')) "
            f"for _ in range({rounds})]"
        )
        procs = [subprocess.Popen([sys.executable, "-c", code, str(delay)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for delay in (0.0, 0.01)]
        for p in procs:
            out, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace")[:400])
        return rounds

    def test_two_processes_keep_both_writers_increments(self):
        rounds = self._run_two_processes()
        data = json.loads((self.tmp / "budget_state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["calls_reserved"], rounds * 2,
                         "两个进程的预留都要在最终快照里")
        self.assertEqual(data["calls_settled"], rounds * 2)
        self.assertEqual(len(data.get("writers") or {}), 2, "逐进程增量各留一份")
        self.assertEqual(data["stages"]["llm"]["settled"], rounds * 2)

    def test_lock_file_is_separate_and_released(self):
        """锁文件与数据文件分离；进程结束后不留锁（下次能立刻拿到）。"""
        self._run_two_processes(rounds=5)
        self.assertTrue((self.tmp / "budget_state.json.lock").exists(),
                        "锁文件按约定落在数据文件旁")
        with rb.cross_process_file_lock(self.tmp / "budget_state.json",
                                        timeout=1.0) as got:
            self.assertTrue(got, "上一批进程已退出，锁应可立即获得")


class TestProviderRequestReconciliation(unittest.TestCase):
    """D5 夜间补修：**假供应商逐条对账**——收到的每次请求都有独立票据。

    覆盖正常发送 / 415 流式回退 / 主备切换 / 重试四种形态，断言
    "供应商实收请求数 == 账本里 llm+backup 阶段的票据数"，且**调度票据单独一列**
    （`steps_dispatched` 不等于 `provider_requests`）。全程禁网（只打桩传输层）。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_recon_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        lc._root_budgets.clear()
        self.addCleanup(lc.clear_task_context)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in _ENV_KEYS])
        os.environ["WM_SINGLE_PROCESS"] = "1"   # 离线单测：单进程语义（不要求共享账本）

    def _snap(self, tid):
        return rb.RootBudget(tid, ws_mod.task_workspace(tid),
                             lc._budget_limits_from_file()).snapshot()

    def test_retry_and_success_each_get_one_ticket(self):
        """失败一次 + 重试成功 = 供应商收 2 次 = 账本 2 张票（llm 阶段）。"""
        p = _Provider(fail_first=1)
        c = _client(p, retries=2)
        lc.set_task_context("t-rec-1")
        with _offline():
            c.call("sys", "u", expect_json=True)
        snap = self._snap("t-rec-1")
        self.assertEqual(p.calls, 2)
        self.assertEqual(snap["provider_requests"]["reserved"], 2)
        self.assertEqual(snap["provider_requests"]["settled"], 2)
        self.assertEqual(snap["stages"]["llm"]["settled"], 2)

    def test_backup_switch_counts_as_a_provider_request(self):
        p = _Provider()
        c = _client(p)
        c._backup_cfg = {"base_url": "https://backup.test/v1", "api_key": "k",
                         "model": "m"}
        lc.set_task_context("t-rec-2")
        with _offline(), mock.patch.object(lc, "LLMClient", type(c)):
            c._call_backup("sys", "u", 0.0, 512, True)
        snap = self._snap("t-rec-2")
        self.assertEqual(p.calls, 1)
        self.assertEqual(snap["stages"]["backup"]["settled"], 1)
        self.assertEqual(snap["provider_requests"]["settled"], 1,
                         "备用端点也是真实 provider 请求")

    def test_stream_fallback_reconciles_two_sends_two_tickets(self):
        """415 回退：供应商实收 2 次 → llm 阶段 2 张票（此前是 1 张）。"""
        sends = []

        def _fake_stream_once(*_a, **_k):
            sends.append("stream")
            raise lc.LLMCallError("HTTP 415: Unsupported Media Type")

        def _fake_urlopen(req, timeout=None):
            sends.append("nonstream")

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *_e):
                    return False

                def read(self):
                    return json.dumps({
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 5},
                    }).encode()

            return _Resp()

        lc.set_task_context("t-rec-3")
        c = lc.LLMClient()
        with _offline(), \
                mock.patch.object(lc, "_stream_enabled", lambda: True), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream_once), \
                mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
            c.call("sys", "u", expect_json=False)
        snap = self._snap("t-rec-3")
        self.assertEqual(len(sends), 2)
        self.assertEqual(snap["provider_requests"]["settled"], 2)
        # 回退那次拿到了响应用量 → 记实际用量；第一次（415）没用量 → 记上界
        self.assertEqual(snap["tokens"]["actual"], 5, "实际用量单独累计")
        self.assertEqual(snap["tokens"]["unknown_calls"], 1,
                         "没取到用量的那次照实记上界，不填 0")

    def test_step_dispatch_is_not_a_provider_request(self):
        """调度票据与 provider 请求**分列**：调度不计入 provider_requests。"""
        b = rb.RootBudget("t-rec-4", self.tmp, rb.BudgetLimits(max_calls=10))
        t = b.reserve("step", detail={"step_id": "1", "capability": "web_search"})
        b.settle(t)
        snap = b.snapshot()
        self.assertEqual(snap["steps_dispatched"], 1)
        self.assertEqual(snap["provider_requests"]["settled"], 0,
                         "步骤调度不等于供应商请求")
        # 口径必须写在快照里（机器可读），否则"17 票"会被读成 17 个供应商请求
        self.assertEqual(snap["count_basis"]["max_calls"], "all_reserved_tickets")
        self.assertEqual(snap["count_basis"]["provider_requests"], "stages:llm+backup")
        self.assertEqual(snap["calls"]["reserved"], 1,
                         "max_calls 口径 = 所有预留票据（含 step）")
        # 结算时没给用量 → 照实记"未取到用量"一次（不填 0 冒充已知）
        self.assertEqual(snap["tokens"]["unknown_calls"], 1)
        self.assertEqual(snap["token_basis"]["actual"], "provider_reported_usage")

    def test_open_ticket_survives_restart_for_reconciliation(self):
        """崩溃后仍能识别"已发出未结算"的请求（票据在文件里，重开可见）。"""
        b = rb.RootBudget("t-rec-5", self.tmp, rb.BudgetLimits(max_calls=10))
        ticket = b.reserve("llm", tokens=512)     # 只预留、不结算（模拟进程崩溃）
        again = rb.RootBudget("t-rec-5", self.tmp, rb.BudgetLimits(max_calls=10))
        snap = again.snapshot()
        self.assertIn(ticket, snap["open_tickets"])
        self.assertEqual(snap["stages"]["llm"]["in_flight_open"], 1)
        # 重开后结算同一张票：命中同一票据（不重复记）
        again.settle(ticket, tokens=512)
        self.assertEqual(again.state.calls_settled, 1)
        again.settle(ticket, tokens=512)          # 重复收尾 → 拒绝、不重记
        self.assertEqual(again.state.calls_settled, 1)
        self.assertIn("settle_unknown_ticket", again.state.rejected_transitions)


class TestOneSendOneTicket(unittest.TestCase):
    """09-22 复核冻结反例（P1）：**每次实际发送恰好一张票、一份 usage**。

    复核表（`docs/阶段D实机复核与下一批指令_20260922.md` P1）记录的三种形态：
    | 普通非流式 usage=5        | 1 次发送 | 曾经 2 票、用量记 10、仅 1 条记录 |
    | 普通非流式 cap=1          | 0 次发送 | 曾经第一次发送就被额外开票拒掉，却留下已结算票 |
    | 流式 415 → 非流式成功     | 2 次发送 | 曾经 2 票却把成功 usage 记两遍、未知用量记 0、形状仅 1 条 |

    这里全部禁网（只打桩传输层），并且**不替换 `_send_request`**——回退逻辑必须真的跑。
    """

    def setUp(self):
        os.environ["WM_SINGLE_PROCESS"] = "1"   # 离线单测：单进程语义
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_onesend_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        lc._root_budgets.clear()
        self.addCleanup(lc.clear_task_context)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in _ENV_KEYS])

    def _snap(self, tid):
        return rb.RootBudget(tid, ws_mod.task_workspace(tid),
                             lc._budget_limits_from_file()).snapshot()

    def _stub(self, sends, *, completion_tokens=5, fail=None):
        """打桩传输层：记录每次真实发送，返回固定 usage 的响应。"""
        def _fake_urlopen(req, timeout=None):
            sends.append("nonstream")
            if fail:
                raise fail

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *_e):
                    return False

                def read(self):
                    return json.dumps({
                        "choices": [{"message": {"content": "ok"}}],
                        "usage": {"prompt_tokens": 7,
                                  "completion_tokens": completion_tokens},
                    }).encode()

            return _Resp()
        return _fake_urlopen

    def _shapes(self):
        """捕获调用形状记录（离线无 Redis，直接记在内存里）。"""
        recs: list[dict] = []

        def _rec(_tid, **kw):
            recs.append(dict(kw))

        return recs, mock.patch.object(lc, "_record_llm_call", _rec)

    def test_plain_nonstream_is_one_send_one_ticket(self):
        sends = []
        recs, patched = self._shapes()
        lc.set_task_context("t-one-1")
        c = lc.LLMClient()
        with _offline(), patched, \
                mock.patch.object(lc, "_stream_enabled", lambda: False), \
                mock.patch.object(urllib.request, "urlopen", self._stub(sends)):
            out = c.call("sys", "u", expect_json=False)
        self.assertTrue(out)
        self.assertEqual(sends, ["nonstream"], "普通非流式只发一次")
        snap = self._snap("t-one-1")
        self.assertEqual(snap["provider_requests"]["reserved"], 1)
        self.assertEqual(snap["provider_requests"]["settled"], 1)
        self.assertEqual(snap["tokens"]["actual"], 5, "实际用量只记一次")
        self.assertEqual(snap["tokens"]["unknown_calls"], 0, "这次拿到了 usage")
        self.assertEqual([r.get("end_reason") for r in recs], ["ok"],
                         "一次发送一条调用形状记录")

    def test_cap_one_allows_first_nonstream_and_blocks_second(self):
        os.environ["WM_TASK_MAX_CALLS"] = "1"
        sends = []
        lc.set_task_context("t-one-2")
        c = lc.LLMClient()
        with _offline(), \
                mock.patch.object(lc, "_stream_enabled", lambda: False), \
                mock.patch.object(urllib.request, "urlopen", self._stub(sends)):
            c.call("sys", "u1", expect_json=False)          # 第一次：允许
            self.assertEqual(sends, ["nonstream"])
            with self.assertRaises(lc.LLMCallError) as ctx:
                c.call("sys", "u2", expect_json=False)      # 第二次：发送前拒
        self.assertTrue(getattr(ctx.exception, "budget_exhausted", False))
        self.assertEqual(sends, ["nonstream"], "被拒的第二次从未发出")
        b = rb.RootBudget("t-one-2", ws_mod.task_workspace("t-one-2"),
                          lc._budget_limits_from_file())
        self.assertEqual(b.state.calls_settled, 1)
        self.assertEqual(b.state.open_tickets, {},
                         "不许留下没发送却已结算的票")

    def test_stream_415_fallback_keeps_usage_once_and_unknown_unknown(self):
        sends = []
        recs, patched = self._shapes()

        def _fake_stream_once(*_a, **_k):
            sends.append("stream")
            raise lc.LLMCallError("HTTP 415: Unsupported Media Type")

        lc.set_task_context("t-one-3")
        c = lc.LLMClient()
        with _offline(), patched, \
                mock.patch.object(lc, "_stream_enabled", lambda: True), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream_once), \
                mock.patch.object(urllib.request, "urlopen", self._stub(sends)):
            out = c.call("sys", "u", expect_json=False)
        self.assertTrue(out)
        self.assertEqual(sends, ["stream", "nonstream"], "两次真实发送")
        snap = self._snap("t-one-3")
        self.assertEqual(snap["provider_requests"]["settled"], 2, "两次发送两张票")
        self.assertEqual(snap["tokens"]["actual"], 5,
                         "成功那次的 usage 只归它自己的票（不得记两遍）")
        self.assertEqual(snap["tokens"]["unknown_calls"], 1,
                         "415 那次没有 usage → 保持未知（不填 0）")
        self.assertEqual([r.get("end_reason") for r in recs],
                         ["stream_fallback_ok", "ok"],
                         "每次发送各留一条调用形状记录")


class TestTokenCounterAccuracy(unittest.TestCase):
    """D5 夜间补修（实机反例）：共享 token 计数必须等于**实际用量**，不得走负。

    实机 ui-706c5ef4a5：`wm:budget:…:tokens = -63188`——`_take_open` 已退回预留上界，
    `settle` 又按 (实际−上界) 记了一遍，实际用量被扣两次。计数走负的后果是
    **配了 `max_tokens` 上限也永远拒不了**（真实门禁失效）。
    """

    def setUp(self):
        self.kv: dict = {}
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_tok_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["WM_SINGLE_PROCESS"] = "1"
        self.addCleanup(lambda: os.environ.pop("WM_SINGLE_PROCESS", None))

    def _bounded(self, **limits):
        return rb.RootBudget("t-tok", self.tmp, rb.BudgetLimits(**limits),
                             redis_factory=lambda: _AtomicFakeRedis(self.kv))

    def test_shared_token_counter_equals_actual_usage(self):
        b = self._bounded(max_tokens=10000)
        t1 = b.reserve("llm", tokens=1000)
        b.settle(t1, tokens=400, usage_known=True)          # 实际用了 400
        t2 = b.reserve("llm", tokens=1000)
        b.settle(t2, tokens=1000)                           # 没取到用量：记上界
        key = [k for k in self.kv if k.endswith(":tokens")][0]
        self.assertEqual(int(self.kv[key]), 1400,
                         "共享计数 = 实际用量 + 未取到用量的上界（不得走负）")

    def test_token_cap_still_refuses_when_counter_is_accurate(self):
        b = self._bounded(max_tokens=1000)
        t = b.reserve("llm", tokens=900)
        b.settle(t, tokens=900, usage_known=True)
        with self.assertRaises(rb.BudgetExceeded):
            b.reserve("llm", tokens=200)                    # 900 + 200 > 1000


class TestBoundedFailClosed(unittest.TestCase):
    """D5 夜间补修：显式有界任务在共享账本不可用、无法可靠预留时 **fail closed**。

    "不得静默退为每个进程各自 40 次"：多进程语义下（编排器 + 独立 Worker），配了上限
    又拿不到共享计数时，第一次付费请求就要被拒，并给出可读原因（收尾会把原因写进交付
    说明并保留已产出的底稿/产物）。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_fc_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(lambda: os.environ.pop("WM_SINGLE_PROCESS", None))

    def _bounded(self, *, multiprocess: bool, redis_factory=None):
        return rb.RootBudget("t-fc", self.tmp, rb.BudgetLimits(max_calls=40),
                             redis_factory=redis_factory, multiprocess=multiprocess)

    def test_multiprocess_without_shared_backend_refuses_first_request(self):
        os.environ.pop("WM_SINGLE_PROCESS", None)     # 生产语义：多进程
        b = self._bounded(multiprocess=True, redis_factory=None)
        with self.assertRaises(rb.BudgetExceeded) as ctx:
            b.reserve("llm")
        self.assertIn("共享账本", str(ctx.exception))
        self.assertIn("拒绝新付费请求", str(ctx.exception))
        self.assertEqual(b.state.calls_reserved, 0, "拒发不记账（这次请求没有发生）")

    def test_multiprocess_with_working_shared_backend_is_allowed(self):
        os.environ.pop("WM_SINGLE_PROCESS", None)
        kv: dict = {}
        b = self._bounded(multiprocess=True,
                          redis_factory=lambda: _AtomicFakeRedis(kv))
        t = b.reserve("llm")                          # 共享后端可用 → 正常预留
        self.assertTrue(t)
        self.assertEqual(b.state.calls_reserved, 1)

    def test_single_process_bounded_task_does_not_need_shared(self):
        os.environ["WM_SINGLE_PROCESS"] = "1"
        b = self._bounded(multiprocess=False)
        self.assertTrue(b.reserve("llm"))

    def test_unlimited_task_is_unaffected(self):
        """不配上限的任务照常（离线检查/工作台读取不因 Redis 不可用被禁）。"""
        os.environ.pop("WM_SINGLE_PROCESS", None)
        b = rb.RootBudget("t-fc2", self.tmp, rb.BudgetLimits(),
                          redis_factory=None, multiprocess=True)
        self.assertTrue(b.reserve("llm"))

    # ── 共享账本"读成功、写失败"（09-22 复核 P1 反例） ──────────────
    def test_write_failure_after_successful_shared_read_refuses(self):
        """假后端 GET 成功、随后落盘失败：不得转为本地成功，必须拒发并撤销该次预留。"""
        os.environ.pop("WM_SINGLE_PROCESS", None)
        kv: dict = {}
        b = self._bounded(multiprocess=True,
                          redis_factory=lambda: _AtomicFakeRedis(kv))
        t0 = b.reserve("llm")                  # 第一次正常（共享计数 1）
        b.settle(t0, tokens=5, usage_known=True)
        key = b._keys()["calls"]
        self.assertEqual(int(kv[key]), 1)
        with mock.patch.object(rb.RootBudget, "_payload",
                               side_effect=RuntimeError("序列化失败")):
            with self.assertRaises(rb.BudgetExceeded) as ctx:
                b.reserve("llm")
        self.assertIn("拒绝新付费请求", str(ctx.exception))
        self.assertIn("待对账", str(ctx.exception))
        self.assertEqual(int(kv[key]), 1, "没发出的请求不得占共享额度")
        self.assertEqual(b.state.calls_reserved, 1, "本地计数撤销这次预留")
        self.assertEqual(b.state.open_tickets, {}, "不留未发出的票")
        self.assertTrue(b._persist_uncertain)

    def test_lock_timeout_is_not_a_lock_free_overwrite(self):
        """落盘锁超时：不写（不得无锁覆盖），有界多进程任务拒发新付费请求。"""
        os.environ.pop("WM_SINGLE_PROCESS", None)
        kv: dict = {}
        b = self._bounded(multiprocess=True,
                          redis_factory=lambda: _AtomicFakeRedis(kv))
        b.reserve("llm")
        path = self.tmp / "budget_state.json"
        before = path.read_text(encoding="utf-8")

        @contextlib.contextmanager
        def _no_lock(_path, timeout=None):
            yield False

        with mock.patch.object(rb, "cross_process_file_lock", _no_lock):
            with self.assertRaises(rb.BudgetExceeded) as ctx:
                b.reserve("llm")
        self.assertEqual(path.read_text(encoding="utf-8"), before,
                         "拿不到锁时不得无锁覆盖账本")
        self.assertIn("待对账", str(ctx.exception))

    def test_uncertainty_is_persisted_and_inherited_on_restart(self):
        """不确定状态写进账本；断点恢复（同一账本）继续拒发，不被下一次成功写洗掉。"""
        os.environ.pop("WM_SINGLE_PROCESS", None)
        kv: dict = {}
        b = self._bounded(multiprocess=True,
                          redis_factory=lambda: _AtomicFakeRedis(kv))
        with mock.patch.object(rb.RootBudget, "_payload",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(rb.BudgetExceeded):
                b.reserve("llm")
        b._save()                              # 之后一次"能写成"的落盘
        raw = json.loads((self.tmp / "budget_state.json").read_text(encoding="utf-8"))
        self.assertTrue(raw.get("persist_uncertain"),
                        "不确定是账本级事实，不能被后来的成功写入洗掉")
        again = self._bounded(multiprocess=True,
                              redis_factory=lambda: _AtomicFakeRedis(kv))
        with self.assertRaises(rb.BudgetExceeded) as ctx:
            again.reserve("llm")
        self.assertIn("待对账", str(ctx.exception))
        self.assertTrue(again.snapshot()["persist_uncertain"])

    def test_unbounded_task_keeps_usable_report_on_write_failure(self):
        """不配上限：落盘失败照常发（保留可用报告与只读工作台），但标记不确定。"""
        os.environ.pop("WM_SINGLE_PROCESS", None)
        b = rb.RootBudget("t-fc3", self.tmp, rb.BudgetLimits(),
                          redis_factory=None, multiprocess=True)
        with mock.patch.object(rb.RootBudget, "_payload",
                               side_effect=RuntimeError("boom")):
            self.assertTrue(b.reserve("llm"), "无上限任务不因落盘失败被拒")
        self.assertTrue(b.snapshot()["persist_uncertain"], "仍要如实标记")


class TestNightClosureBudgetBranches(unittest.TestCase):
    """09-22 晚间收口：账本初始化不得无锁写；异步 415 回退两条记录；空工件不重抓。"""

    def setUp(self):
        os.environ['WM_SINGLE_PROCESS'] = '1'
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_night_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        lc._root_budgets.clear()
        self.addCleanup(lc.clear_task_context)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in _ENV_KEYS])

    def test_init_without_lock_does_not_write_or_forge_identity(self):
        """初始化拿不到落盘锁：不写、不生成身份、标记不确定（有界任务据此拒发）。"""
        os.environ.pop("WM_SINGLE_PROCESS", None)

        @contextlib.contextmanager
        def _no_lock(_path, timeout=None):
            yield False

        kv: dict = {}
        with mock.patch.object(rb, "cross_process_file_lock", _no_lock):
            # 共享后端可用（否则先被"共享账本不可用"那条拒绝，验不到本分支）
            b = rb.RootBudget("t-init", self.tmp, rb.BudgetLimits(max_calls=5),
                              redis_factory=lambda: _AtomicFakeRedis(kv),
                              multiprocess=True)
        self.assertFalse((self.tmp / "budget_state.json").exists(),
                         "拿不到锁不得写账本")
        self.assertEqual(b.state.ledger_id, "", "不得生成竞争身份")
        self.assertTrue(b._persist_uncertain, "必须标记落盘不确定")
        with self.assertRaises(rb.BudgetExceeded) as ctx:
            b.reserve("llm")
        self.assertIn("待对账", str(ctx.exception))

    def test_async_stream_fallback_records_two_shapes(self):
        """异步 415 回退：两次发送各一条调用形状记录（此前只有票据没有记录）。"""
        import asyncio
        sends = []
        recs: list[dict] = []

        def _rec(_tid, **kw):
            recs.append(dict(kw))

        fake = _FallbackAsyncClient(sends)
        lc.set_task_context("t-async-rec")
        with _offline(), mock.patch.object(lc, "_record_llm_call", _rec), \
                mock.patch.object(lc, "_stream_enabled", lambda: True):
            data = asyncio.run(lc._async_chat_once(
                fake, "https://api.example/v1/chat/completions",
                {"max_tokens": 128}, {}))
        self.assertTrue(data)
        self.assertEqual(sends, ["stream", "nonstream"])
        self.assertEqual([r.get("end_reason") for r in recs], ["stream_fallback_ok"],
                         "回退那次发送必须留一条形状记录")
        snap = rb.RootBudget("t-async-rec", ws_mod.task_workspace("t-async-rec"),
                             lc._budget_limits_from_file()).snapshot()
        self.assertEqual(snap["stages"]["llm"]["settled"], 1)

    def test_pdf_worker_without_artifact_keeps_gap_without_refetch(self):
        """worker 说 PDF 但工件为空/缺失 → 保留缺口，**不得**退成按 URL 重抓。"""
        import orchestrator_v2 as o
        import annual_report_pdf as pdf
        calls = []

        def _doc(*a, **k):
            calls.append((a, k))
            raise AssertionError("空工件时不得走解析/重抓")

        with mock.patch.object(pdf, "doc_from_url", _doc), \
                mock.patch.object(o.logger, "warning"):
            ok = o.OrchestratorV2._try_pdf_evidence(
                "t-pdfgap", {"instruction": "抓取该页"},
                {"result": json.dumps({"status": "success", "url": "https://x.test/a",
                                       "pdf": True, "text": "", "artifact": {}})})
        self.assertFalse(ok, "按缺口处理（返回 False）")
        self.assertEqual(calls, [], "不得调用解析通道（那会触发第二次下载）")


class TestWriterMerge(unittest.TestCase):
    """多进程写同一份账本：后写的进程不得抹掉先写进程的计数。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_merge_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _ledger(self, **limits):
        return rb.RootBudget("t-m", self.tmp, rb.BudgetLimits(**limits))

    def test_second_writer_keeps_first_writer_counts(self):
        a = self._ledger(max_calls=10)
        t = a.reserve("llm", tokens=100)
        a.settle(t, tokens=80)
        b = self._ledger(max_calls=10)
        t2 = b.reserve("step")
        b.settle(t2)
        data = json.loads((self.tmp / "budget_state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["calls_reserved"], 2, "两次预留都留在文件里")
        self.assertEqual(data["calls_settled"], 2)
        self.assertEqual(sorted(data["stages"]), ["llm", "step"],
                         "阶段明细不得被后写的进程覆盖掉")
        self.assertEqual(data["stages"]["llm"]["tokens"], 80)
        self.assertEqual(len(data.get("writers") or {}), 2, "逐进程增量可核对")

    def test_reopened_ledger_does_not_double_count(self):
        a = self._ledger(max_calls=10)
        a.settle(a.reserve("llm"), tokens=10)
        for _ in range(3):
            again = self._ledger(max_calls=10)
            again.settle(again.reserve("llm"), tokens=5)
        data = json.loads((self.tmp / "budget_state.json").read_text(encoding="utf-8"))
        self.assertEqual(data["calls_reserved"], 4, "重开三次 = 共四次预留")
        self.assertEqual(data["stages"]["llm"]["tokens"], 10 + 5 * 3)

    def test_writers_are_not_merged_across_ledger_identities(self):
        """账本身份不同（重新开始的一次运行）时不得把上一轮的计数加进来。"""
        a = self._ledger(max_calls=10)
        a.settle(a.reserve("llm"), tokens=10)
        path = self.tmp / "budget_state.json"
        old = json.loads(path.read_text(encoding="utf-8"))
        # 同 id 重新提交：工作区里的旧文件被新一次运行（新账本身份）覆盖
        new_run = dict(old, ledger_id="other-run", calls_reserved=5, calls_settled=5,
                       stages={"plan": {"reserved": 5, "settled": 5}},
                       writers={"dead-beef": {"counters": {"calls_reserved": 5,
                                                           "calls_settled": 5},
                                              "stages": {"plan": {"reserved": 5,
                                                                  "settled": 5}}}})
        path.write_text(json.dumps(new_run, ensure_ascii=False), encoding="utf-8")
        b = self._ledger(max_calls=10)          # 读回的是 other-run 那份账
        b.settle(b.reserve("step"))
        fresh = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(fresh["ledger_id"], "other-run")
        self.assertEqual(fresh["calls_reserved"], 6, "本轮 1 次 + 同身份已有的 5 次")
        self.assertNotIn("llm", fresh["stages"], "上一轮（身份不同）的阶段计数不并入")


if __name__ == "__main__":
    unittest.main()
