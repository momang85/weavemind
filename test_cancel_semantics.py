# -*- coding: utf-8 -*-
"""V2-1 取消语义回归：独立终态 CANCELLED、派发取消闸门、等待提前返回。

依据视觉实测：A 任务停止后约 92 秒才收尾，且最终卡片显示 FAILED 而非"已取消"。
覆盖（离线，不连真实 Redis、不调模型）：
- 终态词表含 CANCELLED，且取消写的是 CANCELLED（不再伪装成 FAILED）；
- **所有派发**都经过取消闸门：取消后不再发起新步骤（也就没有新的付费调用）；
- 等步骤结果期间检查取消并提前返回（这是 92 秒那条路径）；
- 取消后的终态在任务历史/SSE 中一致，指标页能统计到。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import orchestrator_v2 as ov  # noqa: E402
import task_state  # noqa: E402


class _FakeRedis:
    def __init__(self):
        self.pushed: list = []

    def lpush(self, key, value):
        self.pushed.append((key, value))


class _FakeMessaging:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def publish(self, channel, payload):
        self.events.append((channel, payload))


class TestCancelledTerminalState(unittest.TestCase):
    def test_cancelled_is_terminal(self):
        self.assertEqual(task_state.CANCELLED, "CANCELLED")
        self.assertIn(task_state.CANCELLED, task_state.TERMINAL)
        self.assertNotEqual(task_state.CANCELLED, task_state.FAILED)

    def test_derive_status_does_not_invent_cancelled(self):
        """取消是显式终态，不由派生规则产生（取消时可能没有验收报告）。"""
        self.assertEqual(task_state.derive_status(["SUCCESS"], None), task_state.SUCCESS)
        self.assertEqual(task_state.derive_status(["FAILED"], None), task_state.FAILED)

    def _orchestrator_double(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._messaging = _FakeMessaging()
        o._now_iso = lambda: "2026-09-15T00:00:00Z"
        recorded = {}
        o._clear_task_running = lambda tid: recorded.setdefault("cleared_running", tid)
        o._finalize_task = lambda tid, goal, status, **kw: recorded.setdefault("finalize", (tid, status))
        o._notify_done_async = lambda tid, goal, status, note: recorded.setdefault("notify", status)
        o._clear_cancel = lambda tid: recorded.setdefault("cleared_cancel", tid)
        return o, recorded

    def test_finish_cancelled_writes_cancelled(self):
        o, recorded = self._orchestrator_double()
        out = o._finish_cancelled("t-1", "目标")
        self.assertEqual(out["status"], "CANCELLED")
        self.assertEqual(recorded["finalize"], ("t-1", "CANCELLED"))
        self.assertEqual(recorded["notify"], "CANCELLED")
        self.assertEqual(recorded["cleared_cancel"], "t-1", "取消标志必须在终态清理")
        blob = json.dumps(o._messaging.events, ensure_ascii=False)
        self.assertIn("CANCELLED", blob,
                      "SSE 事件里必须携带 CANCELLED，前端才不会显示为失败")


class TestDispatchCancelGate(unittest.TestCase):
    def test_source_guard_gate_before_queue_push(self):
        """取消闸门必须在 `lpush` 之前——所有派发路径共用这一处。"""
        src = (ROOT / "orchestrator_v2.py").read_text(encoding="utf-8")
        gate = src.index("V2-1 取消闸门")
        push = src.index("r.lpush(f\"task_queue:{agent_id}\"")
        self.assertLess(gate, push, "取消闸门必须位于入队之前")
        self.assertIn('"status": "CANCELLED"', src[gate:push + 200])

    def test_cancel_requested_reads_flag(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)

        class _R:
            def __init__(self, hit):
                self.hit = hit

            def get(self, key):
                return "1" if self.hit else None

        o._redis = _R(True)
        self.assertTrue(o._cancel_requested("t-1"))
        o._redis = _R(False)
        self.assertFalse(o._cancel_requested("t-1"))


class TestWaitForResultHonoursCancel(unittest.TestCase):
    def _orch(self, cancelled_after: float):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._new_redis_sync = lambda: object()
        started = time.time()
        # 模拟"结果永不到达"：brpop 只消耗时间片
        o._brpop_with_deadline = lambda r, key, deadline: (time.sleep(0.05), None)[1]
        o._cancel_requested = lambda tid: (time.time() - started) >= cancelled_after
        return o

    def test_returns_early_after_cancel(self):
        """取消到达后应尽快返回，而不是等满整个步骤超时（实测曾等 ~92 秒）。"""
        o = self._orch(cancelled_after=0.3)
        began = time.time()
        out = o._wait_for_result("dispatch-1", timeout=30, cancel_task_id="t-1")
        elapsed = time.time() - began
        self.assertIsNone(out)
        self.assertLess(elapsed, 5.0, f"取消后仍等待了 {elapsed:.1f}s")

    def test_without_cancel_waits_for_result(self):
        o = self._orch(cancelled_after=999)
        o._brpop_with_deadline = lambda r, key, deadline: (
            time.sleep(0.02), (key, json.dumps({"status": "SUCCESS", "result": "ok"})))[1]
        out = o._wait_for_result("dispatch-2", timeout=10, cancel_task_id="t-1")
        self.assertEqual(out["status"], "SUCCESS")

    def test_default_signature_keeps_backward_compat(self):
        """不传 cancel_task_id 时行为不变（老调用点不受影响）。"""
        o = self._orch(cancelled_after=0)
        o._brpop_with_deadline = lambda r, key, deadline: (
            time.sleep(0.02), (key, json.dumps({"status": "SUCCESS"})))[1]
        out = o._wait_for_result("dispatch-3", timeout=5)
        self.assertEqual(out["status"], "SUCCESS")


class TestCancelSurfacesEverywhere(unittest.TestCase):
    def test_cancel_api_rejects_finished_tasks_including_cancelled(self):
        src = (ROOT / "web_ui.py").read_text(encoding="utf-8")
        i = src.index("无需取消")
        window = src[max(0, i - 400):i]
        self.assertIn("CANCELLED", window, "取消接口的终态集合必须包含 CANCELLED")

    def test_metrics_counts_cancelled(self):
        src = (ROOT / "metrics_collector.py").read_text(encoding="utf-8")
        self.assertIn("CANCELLED", src, "指标统计需能计数 CANCELLED（否则取消恒为 0）")

    def test_frontend_maps_cancelled(self):
        src = (ROOT / "frontend" / "src" / "lib" / "statusMeta.ts").read_text(encoding="utf-8")
        self.assertIn("CANCELLED", src)
        self.assertIn("已取消", src)


class TestWaitOutcomeClassification(unittest.TestCase):
    """M0-c：等待结果必须分类——取消/超时/协议错误不能再混成 None。"""

    def _orch(self, msg=None, *, cancelled=False, raises=None):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._new_redis_sync = lambda: object()

        def _pop(r, key, deadline):
            if raises is not None:
                raise raises
            return msg

        o._brpop_with_deadline = _pop
        o._cancel_requested = lambda tid: cancelled
        return o

    def _ok_msg(self, payload):
        return ("k", json.dumps(payload))

    def test_cancel_kind(self):
        o = self._orch(None, cancelled=True)
        out = o._wait_step_result("d-1", timeout=5, cancel_task_id="t-1")
        self.assertEqual(out.kind, ov.WAIT_CANCEL)
        self.assertIsNone(out.result)

    def test_timeout_kind(self):
        o = self._orch(None)
        out = o._wait_step_result("d-1", timeout=5)
        self.assertEqual(out.kind, ov.WAIT_TIMEOUT)
        self.assertIn("超时", out.reason)

    def test_empty_dict_is_protocol_error(self):
        o = self._orch(self._ok_msg({}))
        out = o._wait_step_result("d-1", timeout=5)
        self.assertEqual(out.kind, ov.WAIT_PROTOCOL)

    def test_non_dict_payload_is_protocol_error(self):
        o = self._orch(self._ok_msg([1, 2]))
        out = o._wait_step_result("d-1", timeout=5)
        self.assertEqual(out.kind, ov.WAIT_PROTOCOL)
        self.assertIn("字典", out.reason)

    def test_missing_key_fields_is_protocol_error(self):
        o = self._orch(self._ok_msg({"foo": 1}))
        out = o._wait_step_result("d-1", timeout=5)
        self.assertEqual(out.kind, ov.WAIT_PROTOCOL)

    def test_broken_json_is_protocol_error(self):
        o = self._orch(("k", "{not json"))
        out = o._wait_step_result("d-1", timeout=5)
        self.assertEqual(out.kind, ov.WAIT_PROTOCOL)
        self.assertIn("JSON", out.reason)

    def test_channel_error_is_protocol_error_not_success(self):
        o = self._orch(raises=RuntimeError("redis down"))
        out = o._wait_step_result("d-1", timeout=5)
        self.assertEqual(out.kind, ov.WAIT_PROTOCOL)

    def test_result_passes_through(self):
        o = self._orch(self._ok_msg({"status": "SUCCESS", "result": "ok"}))
        out = o._wait_step_result("d-1", timeout=5)
        self.assertEqual(out.kind, ov.WAIT_RESULT)
        self.assertTrue(out.ok)
        self.assertEqual(out.result["status"], "SUCCESS")

    def test_result_without_status_but_with_body_is_accepted(self):
        """只带 result 的回包仍算结果（兼容旧 worker），不误判成协议错误。"""
        o = self._orch(self._ok_msg({"result": "ok"}))
        out = o._wait_step_result("d-1", timeout=5)
        self.assertEqual(out.kind, ov.WAIT_RESULT)

    def test_cancel_wins_over_timeout(self):
        """取消与超时同时具备时必须报取消（取消不该被写成超时）。"""
        o = self._orch(None, cancelled=True)
        out = o._wait_step_result("d-1", timeout=1, cancel_task_id="t-1")
        self.assertEqual(out.kind, ov.WAIT_CANCEL)


class TestDispatchClassifiesWait(unittest.TestCase):
    """派发路径按分类落日志/落状态：超时才说超时，取消不写 step 失败。"""

    def _orch(self, msg, *, cancelled=False):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._messaging = _FakeMessaging()
        o._now_iso = lambda: "T"
        o._find_agent = lambda cap: "agent-1"
        o._redis = _FakeRedis()
        o._new_redis_sync = lambda: _FakeRedis()
        o._brpop_with_deadline = lambda r, key, deadline: msg
        o._cancel_requested = lambda tid: cancelled
        o._task_starts = {}
        o._task_starts_lock = threading.Lock()
        o._task_simple = {}
        o._task_goals = {}
        o._inflight = {}
        o._inflight_lock = threading.Lock()
        return o

    def _run_dispatch(self, o, outcome=None):
        """跑一次派发，返回（结果, 进度消息文本）。

        `_dispatch` 对步骤超时有 300s 硬下限（真实行为），测试不能真等 5 分钟：
        超时那一路直接注入分类结果，分类本身的判定在 TestWaitOutcomeClassification 里真跑。
        """
        seen: list[str] = []
        if outcome is not None:
            o._wait_step_result = lambda *a, **k: outcome
        with mock.patch("orchestrator_v2.push_progress",
                        lambda *a, **k: seen.append(str((a[3] if len(a) > 3 else {}).get("message") or ""))):
            res = o._dispatch({"step_id": "1", "capability": "web_search",
                               "instruction": "查", "timeout": 5}, "t-1")
        return res, " | ".join(seen)

    def test_cancel_stops_wait_without_step_failure(self):
        o = self._orch(None, cancelled=True)
        res, msgs = self._run_dispatch(o)
        self.assertEqual(res["status"], "CANCELLED")
        self.assertNotIn("timed out", msgs)
        self.assertIn("已取消", msgs)

    def test_timeout_reports_timeout(self):
        o = self._orch(None)
        res, msgs = self._run_dispatch(
            o, ov.WaitOutcome(ov.WAIT_TIMEOUT, reason="等待步骤结果超时（5s）"))
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("timed out", msgs)
        self.assertNotIn("不合协议", msgs)

    def test_protocol_error_is_not_reported_as_timeout(self):
        o = self._orch(("k", json.dumps({})))
        res, msgs = self._run_dispatch(o)
        self.assertEqual(res["status"], "FAILED")
        self.assertTrue(res.get("protocol_error"))
        self.assertIn("不合协议", msgs)
        self.assertNotIn("timed out", msgs)

    def test_cancelled_step_result_is_not_turned_into_step_failure(self):
        """取消后的空结果不能被契约校验改写成步骤失败（那会误报 step_failure）。"""
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._messaging = _FakeMessaging()
        o._now_iso = lambda: "T"
        o._max_retry = 2
        o._replan_depth = 0
        o._cancel_requested = lambda tid: True
        o._dispatch = lambda step, tid: {"task_id": step["step_id"], "status": "CANCELLED",
                                         "result": "任务已取消"}
        o._contract_issue = lambda goal, step, result: "结果太短"
        written = []
        with mock.patch("orchestrator_v2.push_progress"),                 mock.patch("step_diagnosis.write_step_failure",
                           lambda tid, diag: written.append(diag)):
            res = o._dispatch_step_safe("目标", {"step_id": "1", "capability": "web_search"},
                                        "t-1", {"replan_used": 0})
        self.assertEqual(res["status"], "CANCELLED")
        self.assertEqual(written, [], "取消不得写 step_failure 诊断")


class TestCancelledTerminalIsPersistent(unittest.TestCase):
    """取消是持久终态：迟到的成功/失败不得把它覆盖回去。"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.mkdtemp(prefix="wm_cancel_")
        self.db = str(Path(self.tmp) / "tasks.db")
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE task_history(task_id TEXT PRIMARY KEY, goal TEXT, status TEXT,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP,"
            " report TEXT, conversation_id TEXT DEFAULT '', parent_task_id TEXT DEFAULT '',"
            " context TEXT DEFAULT '', project TEXT DEFAULT 'default', user TEXT DEFAULT '',"
            " steps_json TEXT DEFAULT '', logs_json TEXT DEFAULT '')")
        con.commit()
        con.close()
        task_state.ensure_schema(db_path=self.db)
        self._pp = mock.patch("orchestrator_v2.push_progress")
        self._pp.start()
        self.addCleanup(self._pp.stop)

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_late_success_does_not_overwrite_cancelled(self):
        task_state.mark_queued("t-c", "目标", db_path=self.db)
        self.assertTrue(task_state.record_completion(
            "t-c", status=task_state.CANCELLED, report="已取消", db_path=self.db))
        # 迟到的"成功"回包
        self.assertTrue(task_state.record_completion(
            "t-c", status=task_state.SUCCESS, report="迟到的成功", db_path=self.db))
        row = task_state.read_task("t-c", db_path=self.db)
        self.assertEqual(row["status"], task_state.CANCELLED)
        self.assertNotIn("迟到的成功", str(row.get("report") or ""))

    def test_cancelled_can_be_rewritten_with_cancelled(self):
        task_state.mark_queued("t-d", "目标", db_path=self.db)
        task_state.record_completion("t-d", status=task_state.CANCELLED,
                                     report="第一次", db_path=self.db)
        task_state.record_completion("t-d", status=task_state.CANCELLED,
                                     report="补充原因", db_path=self.db)
        row = task_state.read_task("t-d", db_path=self.db)
        self.assertIn("补充原因", str(row.get("report") or ""))

    def test_normal_completion_unaffected(self):
        task_state.mark_queued("t-e", "目标", db_path=self.db)
        task_state.record_completion("t-e", status=task_state.SUCCESS,
                                     report="正常", db_path=self.db)
        self.assertEqual(task_state.read_task("t-e", db_path=self.db)["status"], "SUCCESS")


class TestConfirmWaitHonoursCancel(unittest.TestCase):
    """人工确认等待中取消要立即返回，不能卡到确认超时。"""

    def _orch(self, cancelled_after: float):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._messaging = _FakeMessaging()
        o._now_iso = lambda: "T"
        started = time.time()
        o._redis = object()
        o._brpop_with_deadline = lambda r, key, deadline: (time.sleep(0.05), None)[1]
        o._cancel_requested = lambda tid: (time.time() - started) >= cancelled_after
        o._plan_confirm_timeout = 60
        return o

    def test_plan_confirm_reports_cancel_kind(self):
        o = self._orch(cancelled_after=0.2)
        outcome = {}
        began = time.time()
        res = o._wait_plan_confirm("t-1", [{"step_id": "1"}], outcome)
        self.assertIsNone(res)
        self.assertEqual(outcome["kind"], ov.WAIT_CANCEL)
        self.assertLess(time.time() - began, 5.0, "取消后不应继续等确认超时")

    def test_plan_confirm_timeout_kind_differs(self):
        o = self._orch(cancelled_after=999)
        o._plan_confirm_timeout = 1
        outcome = {}
        res = o._wait_plan_confirm("t-1", [{"step_id": "1"}], outcome)
        self.assertIsNone(res)
        self.assertEqual(outcome["kind"], ov.WAIT_TIMEOUT)

    def test_report_confirm_reports_cancel_kind(self):
        o = self._orch(cancelled_after=0.2)
        outcome = {}
        self.assertFalse(o._wait_report_confirm("t-1", "目标", "报告", outcome))
        self.assertEqual(outcome["kind"], ov.WAIT_CANCEL)


class TestCancellableLlmWait(unittest.TestCase):
    """规划/反思这类长模型调用：等待必须可取消（M0-f 实机缺陷：端点退化时
    请求停止 300s 仍未终结，因为调用阻塞在 llm_client 内部）。"""

    def _orch(self, *, cancelled: bool):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._messaging = _FakeMessaging()
        o._now_iso = lambda: "T"
        o._cancel_requested = lambda tid: cancelled
        o._task_budgets = {}
        return o

    def test_cancel_abandons_wait_and_records_inflight(self):
        import tempfile
        import shutil
        tmp = Path(tempfile.mkdtemp(prefix="wm_llmwait_"))
        try:
            o = self._orch(cancelled=True)
            started = {"v": False}

            def _slow_call(*a, **k):
                started["v"] = True
                time.sleep(30)          # 模拟退化端点上的长调用
                return "too-late"

            with mock.patch("orchestrator_v2.task_workspace", lambda tid: tmp),                     mock.patch("orchestrator_v2.push_progress"):
                began = time.time()
                with self.assertRaises(ov.TaskCancelled):
                    o._call_llm_cancellable("t-1", "规划", _slow_call)
                elapsed = time.time() - began
            self.assertTrue(started["v"], "调用应已发出（放弃等待不等于没发）")
            self.assertLess(elapsed, 5.0, f"取消后应立刻返回，实际 {elapsed:.1f}s")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normal_call_passes_result_through(self):
        o = self._orch(cancelled=False)
        out = o._call_llm_cancellable("t-1", "规划", lambda x: x * 2, 21)
        self.assertEqual(out, 42)

    def test_exception_is_reraised(self):
        o = self._orch(cancelled=False)

        def _boom(*a, **k):
            raise RuntimeError("模型不可用")

        with self.assertRaises(RuntimeError):
            o._call_llm_cancellable("t-1", "规划", _boom)


class TestInFlightAccounting(unittest.TestCase):
    """等待提前返回 ≠ worker 停止：取消时在飞调用要转入待对账。"""

    def test_cancel_marks_inflight_unsettled(self):
        import tempfile
        import shutil

        tmp = Path(tempfile.mkdtemp(prefix="wm_inflight_"))
        try:
            o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
            o._inflight = {}
            o._inflight_lock = __import__("threading").Lock()
            with __import__("unittest").mock.patch(
                    "orchestrator_v2.task_workspace", lambda tid: tmp):
                o._track_inflight("t-1", "d-1", time.time())
                o._mark_inflight_unsettled("t-1", "d-1")
            data = json.loads((tmp / "inflight_unsettled.json").read_text(encoding="utf-8"))
            self.assertEqual(len(data), 1)
            self.assertEqual(data[0]["dispatch_id"], "d-1")
            self.assertIn("对账", data[0]["reason"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_normal_result_clears_inflight(self):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._inflight = {}
        o._inflight_lock = __import__("threading").Lock()
        o._track_inflight("t-1", "d-2", time.time())
        o._untrack_inflight("t-1", "d-2")
        self.assertEqual(o._inflight.get("t-1"), {})


class TestCancelIsNotResettable(unittest.TestCase):
    """R2：取消不是"可以清掉的提示键"，而是不可复位的令牌 + 持久终态。"""

    class _Redis:
        def __init__(self):
            self.kv = {}

        def get(self, key):
            return self.kv.get(key)

        def set(self, key, value, nx=False):
            if nx and key in self.kv:
                return None
            self.kv[key] = value
            return True

        def setex(self, key, ttl, value):
            self.kv[key] = value
            return True

        def delete(self, key):
            self.kv.pop(key, None)
            return 1

        def lpush(self, *a, **k):
            return 1

        def expire(self, *a, **k):
            return True

    def _orch(self, redis, status=""):
        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._redis = redis
        o._now_iso = lambda: "T"
        o._messaging = _FakeMessaging()
        self._status = status
        return o

    def test_request_cancel_writes_token_without_ttl(self):
        r = self._Redis()
        o = self._orch(r)
        with mock.patch("orchestrator_v2.push_progress"):
            o.request_cancel("t-1", reason="用户点了停止")
        self.assertIn("task_cancel:t-1", r.kv)
        self.assertIn("task_cancel_token:t-1", r.kv, "必须同时写下不可复位令牌")
        self.assertTrue(o._cancel_requested("t-1"))

    def test_clearing_hint_key_does_not_reset_cancel(self):
        """清掉提示键之后，取消状态仍然成立（此前清键=取消被抹掉）。"""
        r = self._Redis()
        o = self._orch(r)
        o.request_cancel("t-1")
        o._clear_cancel("t-1")
        self.assertNotIn("task_cancel:t-1", r.kv, "提示键按约定清掉")
        self.assertIn("task_cancel_token:t-1", r.kv)
        self.assertTrue(o._cancel_requested("t-1"),
                        "提示键被清不代表用户没停过：令牌仍在")

    def test_persisted_cancelled_terminal_is_a_cancel_source(self):
        """Redis 全空（重启/被清）时，已落库的 CANCELLED 终态仍然算取消。"""
        import task_state
        r = self._Redis()
        o = self._orch(r)
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": task_state.CANCELLED}):
            self.assertTrue(o._cancel_requested("t-1"),
                            "CANCELLED 是持久终态，不得被当成可以继续跑")
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS"}):
            self.assertFalse(o._cancel_requested("t-2"))

    def test_repeated_cancel_keeps_first_timestamp(self):
        r = self._Redis()
        o = self._orch(r)
        o.request_cancel("t-1", reason="第一次")
        first = r.kv["task_cancel_token:t-1"]
        o.request_cancel("t-1", reason="第二次")
        self.assertEqual(r.kv["task_cancel_token:t-1"], first,
                         "令牌不可复位：重复取消不改写既有记录")


class TestLlmClientRechecksCancel(unittest.TestCase):
    """R2：取消后不再重试、不再切备用（此前客户端完全看不见取消）。"""

    def tearDown(self):
        import llm_client as lc
        lc.set_cancel_guard(None)
        lc.clear_task_context()

    def _client(self, failures: int = 5):
        import llm_client as lc

        class _C(lc.LLMClient):
            def __init__(self):
                super().__init__()
                self._MAX_RETRIES = 3
                self._RETRY_BASE = 0
                self.calls = 0

            def _send_request(self, *a, **k):
                self.calls += 1
                raise lc.LLMCallError("端点故障")

            def _call_backup(self, *a, **k):
                # 离线纪律：测试里绝不允许真的切到配置里的备用端点（会外呼真实模型）
                self.backup_attempted = True
                raise lc.LLMCallError("测试替身：不应切备用")

        c = _C()
        c.backup_attempted = False
        return c

    def test_no_retry_after_cancel(self):
        import llm_client as lc

        c = self._client()
        lc.set_task_context("t-1")
        lc.set_cancel_guard(lambda tid: True)
        with mock.patch.object(lc, "_BACKUP_CFG", {}), \
                mock.patch.object(lc, "_primary_healthy", lambda: True), \
                mock.patch.object(lc, "_ensure_cfg_fresh"), \
                mock.patch.object(lc.time, "sleep"):
            with self.assertRaises(lc.LLMCancelledError):
                c.call("sys", "user", expect_json=False)
        self.assertEqual(c.calls, 0, "取消后一次请求都不该发出")

    def test_no_failover_after_cancel(self):
        """主端点已失败、取消在重试期间到达 → 不切备用。"""
        import llm_client as lc

        c = self._client()
        # 精确表达"取消在重试期间到达"：第一次请求发出之前还没取消，之后才取消
        lc.set_task_context("t-1")
        lc.set_cancel_guard(lambda tid: c.calls > 0)
        with mock.patch.object(lc, "_BACKUP_CFG",
                               {"base_url": "https://backup.test/v1", "api_key": "k"}), \
                mock.patch.object(lc, "_primary_healthy", lambda: True), \
                mock.patch.object(lc, "_ensure_cfg_fresh"), \
                mock.patch.object(lc, "_record_task_degradation"), \
                mock.patch.object(lc.time, "sleep"):
            with self.assertRaises(lc.LLMCancelledError):
                c.call("sys", "user", expect_json=False)
        self.assertEqual(c.calls, 1, "只允许取消前的那一次尝试")
        self.assertFalse(c.backup_attempted, "取消后不得切备用端点")

    def test_health_routed_backup_is_blocked_after_cancel(self):
        """主端点已被判定不健康时走的是**另一条分支**（直接路由备用，不入重试循环）：
        取消同样要拦住它，否则"停止"之后还会有一次备用端点请求。"""
        import llm_client as lc

        c = self._client()
        lc.set_task_context("t-1")
        lc.set_cancel_guard(lambda tid: True)
        with mock.patch.object(lc, "_BACKUP_CFG",
                               {"base_url": "https://backup.test/v1", "api_key": "k"}), \
                mock.patch.object(lc, "_primary_healthy", lambda: False), \
                mock.patch.object(lc, "_ensure_cfg_fresh"), \
                mock.patch.object(lc.time, "sleep"):
            with self.assertRaises(lc.LLMCancelledError):
                c.call("sys", "user", expect_json=False)
        self.assertFalse(c.backup_attempted, "取消后连健康路由的备用请求都不发")
        self.assertEqual(c.calls, 0)

    def test_guard_is_ignored_without_task_context(self):
        import llm_client as lc

        c = self._client()
        lc.clear_task_context()
        lc.set_cancel_guard(lambda tid: True)
        with mock.patch.object(lc, "_BACKUP_CFG", {}), \
                mock.patch.object(lc, "_primary_healthy", lambda: True), \
                mock.patch.object(lc, "_ensure_cfg_fresh"), \
                mock.patch.object(lc.time, "sleep"):
            with self.assertRaises(lc.LLMCallError) as ctx:
                c.call("sys", "user", expect_json=False)
        self.assertNotIsInstance(ctx.exception, lc.LLMCancelledError,
                                "没有任务上下文时不得假装知道取消状态")


class TestThreadGetsContextAtCreation(unittest.TestCase):
    """R2：新线程默认不继承 contextvars —— 必须在**创建边界**把上下文传进去。"""

    def test_cancellable_call_keeps_task_context_in_thread(self):
        import contextvars
        import llm_client as lc

        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._messaging = _FakeMessaging()
        o._now_iso = lambda: "T"
        o._cancel_requested = lambda tid: False
        o._task_budgets = {}
        seen = {}

        def _capture():
            seen["task"] = lc.get_task_context()
            seen["var"] = contextvars.copy_context().get(
                _probe_var, "missing") if False else seen.get("var")
            return "ok"

        probe = contextvars.ContextVar("probe", default="missing")
        _probe_var = probe

        def _with_probe():
            probe.set("propagated")
            return _capture()

        out = o._call_llm_cancellable("root-1", "规划", _with_probe)
        self.assertEqual(out, "ok")
        self.assertEqual(seen["task"], "root-1",
                         "工作线程里必须能看到根任务上下文（台账/取消守卫都依赖它）")

    def test_contextvar_value_propagates_into_thread(self):
        import contextvars

        o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
        o._messaging = _FakeMessaging()
        o._now_iso = lambda: "T"
        o._cancel_requested = lambda tid: False
        o._task_budgets = {}
        var = contextvars.ContextVar("probe2", default="missing")
        var.set("inherited")
        seen = {}

        def _read():
            seen["v"] = var.get()
            return 1

        o._call_llm_cancellable("root-2", "规划", _read)
        self.assertEqual(seen["v"], "inherited",
                         "创建边界拷上下文：线程内读到的应是调用方的值")


if __name__ == "__main__":
    unittest.main()
