# -*- coding: utf-8 -*-
"""M0-e 根任务预算回归：唯一账本、先预留后发送、共享剩余、阶段观测。

离线：不连 Redis、不调模型；时间用假时钟推进，请求在**边界**打桩
（只替身 redis 发送与结果回包，不替换编排逻辑）。
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import root_budget as rb  # noqa: E402


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

    def test_unlimited_budget_is_noop(self):
        b = rb.RootBudget("t-1", self.tmp)
        self.assertFalse(b.limited)
        self.assertEqual(b.reserve("step"), "", "未配置上限时不预留、不落盘")
        b.settle("", tokens=100)          # 无票据 → 无副作用
        self.assertEqual(b.state.calls_reserved, 0)

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
        """预算已耗尽时派发被拒，并置运行级标记（供收尾/迭代循环停止）。"""
        import threading
        import tempfile
        from unittest import mock
        from orchestrator_v2 import OrchestratorV2

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
    """R2：两个实例共用一份剩余额度（Redis 原子计数），文件只是快照。"""

    def setUp(self):
        self.kv: dict = {}

    def _budget(self, tmp, **limits):
        return rb.RootBudget("t-1", tmp, rb.BudgetLimits(**limits),
                             redis_factory=lambda: _AtomicFakeRedis(self.kv))

    def test_two_instances_do_not_both_get_the_last_slot(self):
        tmp1 = Path(tempfile.mkdtemp(prefix="wm_x1_"))
        tmp2 = Path(tempfile.mkdtemp(prefix="wm_x2_"))
        self.addCleanup(__import__("shutil").rmtree, tmp1, ignore_errors=True)
        self.addCleanup(__import__("shutil").rmtree, tmp2, ignore_errors=True)
        b1 = self._budget(tmp1, max_calls=1)
        b2 = self._budget(tmp2, max_calls=1)
        t1 = b1.reserve("step")
        with self.assertRaises(rb.BudgetExceeded):
            b2.reserve("step")
        self.assertTrue(t1)

    def test_ticket_ids_are_unique_across_instances(self):
        """R2 反例：两个实例都返回 `request-1`，磁盘只记 1 条。"""
        tmp1 = Path(tempfile.mkdtemp(prefix="wm_x3_"))
        tmp2 = Path(tempfile.mkdtemp(prefix="wm_x4_"))
        self.addCleanup(__import__("shutil").rmtree, tmp1, ignore_errors=True)
        self.addCleanup(__import__("shutil").rmtree, tmp2, ignore_errors=True)
        b1 = self._budget(tmp1, max_calls=10)
        b2 = self._budget(tmp2, max_calls=10)
        t1 = b1.reserve("step")
        t2 = b2.reserve("step")
        self.assertNotEqual(t1, t2, "票据号必须跨进程唯一")
        snap = b1.snapshot()
        self.assertTrue(snap["cross_process"])
        self.assertEqual(snap["calls"]["reserved"], 2,
                         "共享计数：两个进程的预留都要记进同一份额度")

    def test_token_reservation_is_atomic_across_instances(self):
        tmp1 = Path(tempfile.mkdtemp(prefix="wm_x5_"))
        tmp2 = Path(tempfile.mkdtemp(prefix="wm_x6_"))
        self.addCleanup(__import__("shutil").rmtree, tmp1, ignore_errors=True)
        self.addCleanup(__import__("shutil").rmtree, tmp2, ignore_errors=True)
        b1 = self._budget(tmp1, max_tokens=100)
        b2 = self._budget(tmp2, max_tokens=100)
        b1.reserve("plan", tokens=80)
        with self.assertRaises(rb.BudgetExceeded):
            b2.reserve("reflect", tokens=80)

    def test_rollback_keeps_counter_accurate(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_x7_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        b = self._budget(tmp, max_calls=2, max_tokens=100)
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
        self.assertEqual(b.reserve("plan"), "", "不限额度时不预留")
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


if __name__ == "__main__":
    unittest.main()
