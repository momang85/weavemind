# -*- coding: utf-8 -*-
"""任务16（P2：步结果带 `retryable`，逻辑错误不再重试）测试。

设计：docs/arch_phase2_tasks15_16_17.md（任务16）
判定表（架构师指定，不得自行调整）：
  可重试 True  = 网络超时 / HTTP 5xx / Redis 连接失败；
  不可重试 False = URL 无法解析 / 内容拒绝 / 逻辑错误 / 级联阻塞 / 空结果。

覆盖四条接缝：
1. worker 侧异常分类 `worker_base.classify_step_retryable`；
2. 上行协议：`_publish_result` / `_publish_failure` 的**兄弟字段** `retryable`
   （不带该字段 = 旧行为不变），以及 `async_worker_base` 异常路径；
3. `workers/web_fetch_worker.py` 的失败 JSON 体带 `retryable`（4xx/无 URL=False、5xx/超时=True）；
4. 编排器 `_step_retryable` 读取三个来源 + `_dispatch_step_safe` 的重试短路，
   并守住专项 §5 的**检索补查重试**（空结果但契约还有新查询时照旧重试）。
"""
import asyncio
import json
import unittest
from unittest import mock

import redis.exceptions

from test_orchestrator_v2 import make_orch

import async_worker_base as awb
import net_policy
import orchestrator_v2 as ov
import worker_base as wb
from execution_contract import ExecutionContract

GOAL = "调研洋河股份 2024 年度报告"
BAD = "https://bad.example/annual.pdf"


# ---------------------------------------------------------------------------
# 1. worker 侧异常分类
# ---------------------------------------------------------------------------

class TestClassifyStepRetryable(unittest.TestCase):
    def test_network_errors_are_retryable(self):
        for exc in (ConnectionError("connection refused"),
                    ConnectionResetError(10054, "远程主机强迫关闭连接"),
                    TimeoutError("read timed out"),
                    redis.exceptions.ConnectionError("redis is down"),
                    redis.exceptions.TimeoutError("redis timeout")):
            self.assertTrue(wb.classify_step_retryable(exc), f"{exc!r} 应可重试")

    def test_dns_gaierror_is_retryable(self):
        """DNS 解析失败是瞬时网络类（与任务15 死链语义一致：不写死）。"""
        import socket
        self.assertTrue(wb.classify_step_retryable(socket.gaierror("temporary failure")))

    def test_bare_oserror_is_not_auto_retryable(self):
        """裸 OSError 无更具体子类：不按网络类判（避免把文件类错误错算进去）。"""
        self.assertFalse(wb.classify_step_retryable(OSError("generic os failure")))

    def test_logic_errors_are_not_retryable(self):
        for exc in (ValueError("bad literal"), KeyError("rows"),
                    TypeError("unsupported operand"), RuntimeError("解析 PDF 失败"),
                    AttributeError("'NoneType' has no attribute 'get'")):
            self.assertFalse(wb.classify_step_retryable(exc), f"{exc!r} 不应重试")

    def test_dns_gaierror_is_retryable(self):
        import socket
        self.assertTrue(wb.classify_step_retryable(socket.gaierror("temporary failure")))


# ---------------------------------------------------------------------------
# 2. 上行协议：兄弟字段
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self):
        self.items = []

    def rpush(self, channel, payload):
        self.items.append(json.loads(payload))


class _FakeAsyncRedis:
    """`async_worker_base._handle` 直接 `await rpush(...)` 的最小替身。"""

    def __init__(self):
        self.items = []

    async def rpush(self, channel, payload):
        self.items.append(json.loads(payload))


class _FakeMessaging:
    def __init__(self):
        self._redis = _FakeRedis()


class _FakeAsyncMessaging:
    def __init__(self):
        self._redis = _FakeAsyncRedis()


def _sync_worker():
    from worker_base import BaseWorker

    class _W(BaseWorker):
        def execute(self, instruction: str) -> str:
            return "ok"

    w = _W.__new__(_W)
    w.agent_id = "t16-worker"
    w._messaging = _FakeMessaging()
    w._search_status = None
    w._permit = None
    w._current_ctx = None
    w._current_gaps = []
    return w


class TestPublishCarriesRetryable(unittest.TestCase):
    def test_failure_publishes_retryable_sibling(self):
        w = _sync_worker()
        w._publish_failure("t-1", "ValueError: bad", retryable=False)
        msg = w._messaging._redis.items[-1]
        self.assertEqual(msg["status"], "FAILED")
        self.assertIs(msg["retryable"], False)
        self.assertEqual(msg["result"], "ValueError: bad")

    def test_field_absent_when_not_given(self):
        """不带 `retryable` 的旧路径：消息里不得凭空造字段（默认按可重试处理）。"""
        w = _sync_worker()
        w._publish_result("t-1", "SUCCESS", "[]")
        self.assertNotIn("retryable", w._messaging._redis.items[-1])

    def test_success_may_declare_retryable_true(self):
        w = _sync_worker()
        w._publish_result("t-1", "FAILED", "Timeout after 60s", retryable=True)
        self.assertIs(w._messaging._redis.items[-1]["retryable"], True)


class TestProcessTaskFlags(unittest.TestCase):
    """`_process_task` 的失败出口按任务16 判定表带上 `retryable`。"""

    def _worker(self, exc):
        from worker_base import BaseWorker

        class _W(BaseWorker):
            def execute(self, instruction: str) -> str:
                raise exc

        w = _W.__new__(_W)
        w.agent_id = "t16-pt"
        w._contract = None
        w._permit = None
        w._search_status = None
        w._current_ctx = None
        w._current_gaps = []
        w._task_permit = lambda task: object()
        w._permit_gate = lambda permit: ""
        w._install_permit_guard = lambda permit: None
        w._clear_permit_guard = lambda: None
        published = []
        w._publish_failure = lambda tid, err, **kw: published.append((tid, err, kw.get("retryable")))
        w._publish_result = lambda tid, status, result, **kw: published.append((tid, status, result))
        return w, published

    def _run(self, exc):
        w, published = self._worker(exc)
        w._process_task({"task_id": "t-1", "instruction": "查一下"})
        self.assertEqual(len(published), 1, f"只发布一次：{published}")
        return published[0]

    def test_model_deny_is_not_retryable(self):
        tid, err, flag = self._run(RuntimeError("request refused by content policy"))
        self.assertTrue(err.startswith("model_deny: "))
        self.assertIs(flag, False, "内容拒绝重试同样被拒")

    def test_model_ctx_is_not_retryable(self):
        tid, err, flag = self._run(RuntimeError("context too long for model"))
        self.assertTrue(err.startswith("model_ctx: "))
        self.assertIs(flag, False)

    def test_connection_error_is_retryable(self):
        tid, err, flag = self._run(ConnectionResetError(10054, "远程主机强迫关闭了一个现有连接"))
        self.assertIs(flag, True, "网络类失败应交给重试")

    def test_logic_error_is_not_retryable(self):
        tid, err, flag = self._run(ValueError("无法解析返回值"))
        self.assertIs(flag, False)

    def test_empty_instruction_is_not_retryable(self):
        w, published = self._worker(AssertionError("不该执行"))
        w._process_task({"task_id": "t-1", "instruction": ""})
        self.assertEqual(published[0][2], False, "空指令重发同样空")


class TestAsyncHandleCarriesRetryable(unittest.TestCase):
    """async 基类（web_fetch / data_loader 等）异常路径同样上行 `retryable`。"""

    def _run_handle(self, exc):
        class _W(awb.AsyncWorkerBase):
            _class_capabilities = ["t16"]

            async def execute(self, instruction, task=None):
                raise exc

        w = _W.__new__(_W)
        w.agent_id = "t16-async"
        w.capabilities = ["t16"]

        class _Reg:
            async def register(self, *a, **k):
                pass

        w._registry = _Reg()
        w._messaging = _FakeAsyncMessaging()
        w._active = 0
        w._failures = 0
        w._max_failures = 3
        w._context_gaps = []

        async def _go():
            with mock.patch("task_context.admit_dispatch") as madmit, \
                    mock.patch("task_context.bind_llm_accounting"), \
                    mock.patch("task_context.clear_llm_accounting"):
                madmit.return_value = (object(), [], "")
                await w._handle({"task_id": "t-async", "instruction": "查"})
        asyncio.run(_go())
        return w._messaging._redis.items[-1]

    def test_timeout_exception_marks_retryable(self):
        msg = self._run_handle(TimeoutError("connect timed out"))
        self.assertEqual(msg["status"], "FAILED")
        self.assertIs(msg["retryable"], True)

    def test_logic_exception_marks_not_retryable(self):
        msg = self._run_handle(ValueError("unsupported format"))
        self.assertEqual(msg["status"], "FAILED")
        self.assertIs(msg["retryable"], False)

    def test_success_does_not_carry_field(self):
        class _W(awb.AsyncWorkerBase):
            _class_capabilities = ["t16"]

            async def execute(self, instruction, task=None):
                return "[]"

        w = _W.__new__(_W)
        w.agent_id = "t16-ok"
        w.capabilities = ["t16"]

        class _Reg:
            async def register(self, *a, **k):
                pass

        w._registry = _Reg()
        w._messaging = _FakeAsyncMessaging()
        w._active = 0
        w._failures = 0
        w._max_failures = 3
        w._context_gaps = []

        async def _go():
            with mock.patch("task_context.admit_dispatch") as madmit, \
                    mock.patch("task_context.bind_llm_accounting"), \
                    mock.patch("task_context.clear_llm_accounting"):
                madmit.return_value = (object(), [], "")
                await w._handle({"task_id": "t-ok", "instruction": "查"})
        asyncio.run(_go())
        msg = w._messaging._redis.items[-1]
        self.assertEqual(msg["status"], "SUCCESS")
        self.assertNotIn("retryable", msg, "成功结果不带重试标记")


# ---------------------------------------------------------------------------
# 3. web_fetch worker 的失败 JSON
# ---------------------------------------------------------------------------

class TestWebFetchWorkerRetryable(unittest.TestCase):
    def _w(self):
        from workers.web_fetch_worker import WebFetchWorker
        return WebFetchWorker.__new__(WebFetchWorker)

    def test_policy_reject_is_not_retryable(self):
        f = self._w()._fetch_exc_retryable
        self.assertFalse(f(net_policy.NetworkPolicyError("URL 无法解析：nx.example")))
        self.assertFalse(f(net_policy.NetworkPolicyError("解析到非公网地址：10.0.0.5")))

    def test_fetch_error_and_timeouts_are_retryable(self):
        f = self._w()._fetch_exc_retryable
        self.assertTrue(f(net_policy.FetchError("Connection refused")))
        self.assertTrue(f(TimeoutError("timed out")))
        self.assertTrue(f(ConnectionResetError(10054, "reset")))

    def test_non_network_exception_is_not_retryable(self):
        f = self._w()._fetch_exc_retryable
        self.assertFalse(f(ValueError("只支持 GET/POST")))
        self.assertFalse(f(ImportError("no module named x")))

    def test_no_url_result_declares_not_retryable(self):
        out = asyncio.run(self._w().execute("请总结这份年报，指令里没有链接", None))
        data = json.loads(out)
        self.assertEqual(data["status"], "failed")
        self.assertIs(data["retryable"], False)

    def test_http_status_mapping(self):
        """4xx/3xx 确定性不可用 → False；5xx 服务端瞬时 → True（直接验 execute 分支）。"""
        w = self._w()
        for status, expect in ((404, False), (403, False), (503, True), (500, True)):
            payload = {"status": status, "raw": b"", "headers": {}}
            with mock.patch("net_policy.fetch_document", return_value=payload):
                data = json.loads(asyncio.run(
                    w.execute(f"抓取 [URL: {BAD}]", {"task_id": "t-1"})))
            self.assertIn("http_status", data, f"{status} 应带状态码：{data}")
            self.assertIs(data["retryable"], expect, f"HTTP {status} → retryable={expect}")


# ---------------------------------------------------------------------------
# 4. 编排器：读判定 + 重试短路
# ---------------------------------------------------------------------------

class TestStepRetryableReader(unittest.TestCase):
    def _o(self):
        return make_orch(_now_iso=lambda: "t")

    def test_sibling_field_first(self):
        self.assertFalse(self._o()._step_retryable({"retryable": False}))
        self.assertTrue(self._o()._step_retryable({"retryable": True}))

    def test_inner_json_body(self):
        o = self._o()
        body = json.dumps({"status": "failed", "retryable": False, "error": "x"})
        self.assertFalse(o._step_retryable({"status": "FAILED", "result": body}))
        self.assertTrue(o._step_retryable(
            {"status": "FAILED", "result": json.dumps({"status": "failed", "retryable": True})}))

    def test_search_status_used_when_no_hits(self):
        o = self._o()
        self.assertFalse(o._step_retryable(
            {"status": "SUCCESS", "result": "[]",
             "search_status": {"status": "no_results", "retryable": False}}))
        self.assertTrue(o._step_retryable(
            {"status": "FAILED", "result": "[]",
             "search_status": {"status": "timeout", "retryable": True}}))

    def test_search_ok_verdict_does_not_block_retry(self):
        """`ok`（查询跑完有命中）后的失败属于输出问题：不得按检索真值跳过重试。"""
        self.assertTrue(self._o()._step_retryable(
            {"status": "FAILED", "result": "[{...}]",
             "search_status": {"status": "ok", "retryable": False}}))

    def test_missing_everywhere_defaults_true(self):
        """无标记的旧 worker：行为与任务16 之前一致（照常重试）。"""
        self.assertTrue(self._o()._step_retryable({"status": "FAILED", "result": "boom"}))


class TestRetrySkippedInDispatch(unittest.TestCase):
    def _orch(self, retry=2):
        o = make_orch(_max_retry=retry, _replan_depth=0)
        o._now_iso = lambda: "t"
        o._cancel_requested = lambda tid: False
        return o

    def _run(self, o, step, payload, task_id="t16-disp"):
        calls = {"n": 0, "instrs": []}

        def dispatch(s, _tid):
            calls["n"] += 1
            calls["instrs"].append(s.get("instruction", ""))
            return dict({"task_id": s["step_id"], "status": "FAILED"}, **payload)

        o._dispatch = dispatch
        with mock.patch.object(ov.time, "sleep"):
            res = o._dispatch_step_safe(GOAL, step, task_id, {"replan_used": 0})
        return res, calls

    def _fetch_step(self):
        return {"step_id": "3", "capability": "web_fetch",
                "instruction": f"抓取年报 [URL: {BAD}]", "timeout": 60}

    def test_not_retryable_skips_retry(self):
        o = self._orch()
        res, calls = self._run(o, self._fetch_step(),
                               {"retryable": False, "result": "KeyError: 'rows'"})
        self.assertEqual(calls["n"], 1, "worker 判定不可重试：只派发一次")
        self.assertEqual(res["status"], "FAILED")
        self.assertTrue(res.get("retry_skipped"))
        self.assertIn("[NOT_RETRYABLE]", res["result"])

    def test_retryable_true_still_retries(self):
        o = self._orch()
        res, calls = self._run(o, self._fetch_step(),
                               {"retryable": True, "result": "读取超时（60s）"})
        self.assertEqual(calls["n"], 1 + o._max_retry, "瞬时失败按 _max_retry 重试")
        self.assertFalse(res.get("retry_skipped"))

    def test_fetch_4xx_json_skips_retry(self):
        o = self._orch()
        body = json.dumps({"status": "failed", "retryable": False, "http_status": 404,
                           "error": "HTTP 404：该地址不可用"}, ensure_ascii=False)
        res, calls = self._run(o, self._fetch_step(), {"result": body})
        self.assertEqual(calls["n"], 1, "4xx 是确定性结果：不再重试")
        self.assertTrue(o._is_dead_fetch_url("t16-disp", BAD), "4xx 仍记死链（任务15）")

    def test_fetch_5xx_json_retries(self):
        o = self._orch()
        body = json.dumps({"status": "failed", "retryable": True, "http_status": 503,
                           "error": "HTTP 503：该地址不可用"}, ensure_ascii=False)
        _, calls = self._run(o, self._fetch_step(), {"result": body})
        self.assertEqual(calls["n"], 1 + o._max_retry, "5xx 可重试（任务16 判定表）")

    def test_unmarked_failure_keeps_old_behavior(self):
        o = self._orch()
        _, calls = self._run(o, {"step_id": "2", "capability": "data_analyzer",
                                 "instruction": "分析"}, {"result": "解析失败"})
        self.assertEqual(calls["n"], 1 + o._max_retry)

    def test_search_zero_hits_with_pending_queries_still_retries(self):
        """专项 §5 守门：空结果但契约还有没发过的下一批查询 → 照旧补查重试。"""
        o = self._orch(retry=1)
        contract = ExecutionContract(company="洋河股份", company_id="002304.SZ",
                                     market="A股", periods=(2024,), as_of="2025-04-30")
        o._task_contracts = {"t16-search": contract}
        res, calls = self._run(o, {"step_id": "1", "capability": "web_search",
                                   "instruction": "检索", "timeout": 60},
                               {"result": "[]",
                                "search_status": {"status": "no_results",
                                                  "retryable": False, "attempts": 3}},
                               task_id="t16-search")
        self.assertEqual(calls["n"], 2, "还有新查询：补查重试不得被掐死")
        self.assertIn("[重试检索查询]", calls["instrs"][1])
        self.assertFalse(res.get("retry_skipped"))

    def test_search_zero_hits_without_new_queries_skips(self):
        """计划用尽（无契约、非行情目标）：retryable=False 直接记 FAILED，不再空转。"""
        o = self._orch(retry=2)
        o._task_contracts = {}
        res, calls = self._run(o, {"step_id": "1", "capability": "web_search",
                                   "instruction": "检索", "timeout": 60},
                               {"result": "[]",
                                "search_status": {"status": "no_results",
                                                  "retryable": False, "attempts": 3}})
        self.assertEqual(calls["n"], 1, "没有可发的新查询：重试同样零命中")
        self.assertTrue(res.get("retry_skipped"))
        self.assertIn("[NOT_RETRYABLE]", res["result"])

    def test_search_refused_budget_skips_even_with_pending_queries(self):
        """预算拒绝不是"零命中"：不属 §5 补查例外，重试同样被拒 → 跳过。"""
        o = self._orch(retry=1)
        contract = ExecutionContract(company="洋河股份", company_id="002304.SZ",
                                     market="A股", periods=(2024,), as_of="2025-04-30")
        o._task_contracts = {"t16-budget": contract}
        res, calls = self._run(o, {"step_id": "1", "capability": "web_search",
                                   "instruction": "检索", "timeout": 60},
                               {"result": "[]",
                                "search_status": {"status": "refused_budget",
                                                  "retryable": False, "attempts": 0}},
                               task_id="t16-budget")
        self.assertEqual(calls["n"], 1)
        self.assertTrue(res.get("retry_skipped"))

    def test_skip_is_logged_to_progress(self):
        o = self._orch()
        msg = {"retryable": False, "result": "内容拒绝"}
        res, _ = self._run(o, {"step_id": "9", "capability": "content_summary",
                               "instruction": "概括"}, msg)
        self.assertEqual(res["status"], "FAILED")
        logs = [p for _c, m in o._messaging.published if isinstance(m, dict)
                for p in [m.get("payload") or {}]
                if "跳过重试" in str(p.get("message", ""))]
        self.assertTrue(logs, "跳过重试必须写进任务日志（页面能回答为什么没重试）")


class TestSearchNewQueriesProbe(unittest.TestCase):
    def test_no_contract_non_market(self):
        o = make_orch(_now_iso=lambda: "t")
        o._task_contracts = {}
        self.assertFalse(o._search_new_queries_available(GOAL, "t-x", set()))

    def test_market_goal_has_directed_plan(self):
        o = make_orch(_now_iso=lambda: "t")
        o._task_contracts = {}
        self.assertTrue(o._search_new_queries_available(
            "今日A股成交额排名前十", "t-x", set()))

    def test_exhausted_plan_reports_none(self):
        o = make_orch(_now_iso=lambda: "t")
        c = ExecutionContract(company="洋河股份", company_id="002304.SZ",
                              market="A股", periods=(2024,), as_of="2025-04-30")
        o._task_contracts = {"t-x": c}
        seen = set(c.queries()) | set(c.retry_queries(tried=set()))
        self.assertFalse(o._search_new_queries_available(GOAL, "t-x", seen))
        self.assertTrue(o._search_new_queries_available(GOAL, "t-x", set(c.queries())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
