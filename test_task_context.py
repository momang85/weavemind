# -*- coding: utf-8 -*-
"""L01 任务上下文契约：三层身份跨线程、跨进程不丢，缺身份策略显式。

背景：派发时编排器把合成 id 当 `task_id` 下发，Worker 的 `set_task_context(task_id)`
于是把 LLM 台账记在派发键上——根任务台账（`llm_usage_task:{root}`，指标/费用页读它）
恒为空。这里验证契约、兼容解码、拒绝策略与真实的跨进程/跨线程传递。

只做本地断言，不连 Redis、不调模型、不发网络请求。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import llm_client  # noqa: E402
import task_context  # noqa: E402


class TestContextContract(unittest.TestCase):
    def test_round_trip_preserves_identity(self):
        ctx = task_context.make_context("task-root-1", "3", "3-ab12cd34", tenant_id="bank-a")
        again = task_context.TaskExecutionContext.from_wire(json.loads(json.dumps(ctx.to_wire())))
        self.assertEqual(again.root_task_id, "task-root-1")
        self.assertEqual(again.step_id, "3")
        self.assertEqual(again.dispatch_id, "3-ab12cd34")
        self.assertEqual(again.trace_id, "3-ab12cd34")
        self.assertEqual(again.tenant_id, "bank-a")
        self.assertEqual(again.schema_version, task_context.SCHEMA_VERSION)

    def test_unknown_fields_ignored_and_missing_left_empty(self):
        ctx = task_context.TaskExecutionContext.from_wire(
            {"root_task_id": "r1", "future_field": "x", "schema_version": 99})
        self.assertEqual(ctx.root_task_id, "r1")
        self.assertEqual(ctx.step_id, "")
        self.assertEqual(ctx.schema_version, 99, "版本号应原样保留，供解码端判定")

    def test_accounting_prefers_root_over_dispatch(self):
        ctx = task_context.make_context("root-9", "1", "1-deadbeef")
        self.assertEqual(ctx.accounting_task_id(), "root-9")
        self.assertEqual(task_context.TaskExecutionContext(dispatch_id="1-x").accounting_task_id(), "1-x")


class TestDispatchDecode(unittest.TestCase):
    def test_new_payload_carries_root_identity(self):
        payload = {"task_id": "7-abcd1234", "context": task_context.make_context(
            "real-task", "7", "7-abcd1234").to_wire()}
        ctx, gaps = task_context.decode_dispatch(payload, mode="local")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.root_task_id, "real-task")
        self.assertEqual(ctx.dispatch_id, "7-abcd1234")
        self.assertEqual(gaps, [], "新版载荷不应有缺口")

    def test_legacy_payload_falls_back_locally_and_reports_gap(self):
        """老消息只有派发 id：本地兜底继续跑，但必须把缺口显式记下来。"""
        ctx, gaps = task_context.decode_dispatch({"task_id": "5-oldmsg"}, mode="local")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.root_task_id, "5-oldmsg", "本地模式用旧 id 兜底")
        self.assertEqual(ctx.dispatch_id, "5-oldmsg")
        self.assertIn("context", gaps, "缺版本化上下文必须被记录，不能静默")

    def test_bank_mode_refuses_without_identity(self):
        for payload in ({"task_id": "5-oldmsg"},
                        {"task_id": "5-x", "context": {"root_task_id": "r"}}):
            ctx, gaps = task_context.decode_dispatch(payload, mode="bank")
            self.assertIsNone(ctx, f"银行口径下缺身份必须拒绝执行：{payload}")
            self.assertTrue(gaps)

    def test_bank_mode_accepts_full_identity(self):
        ctx = task_context.make_context("r1", "1", "1-x", tenant_id="t1", workspace_id="w1", actor_id="u1")
        got, gaps = task_context.decode_dispatch({"task_id": "1-x", "context": ctx.to_wire()}, mode="bank")
        self.assertIsNotNone(got)
        self.assertEqual(gaps, [])

    def test_default_mode_is_local_unless_configured(self):
        old = os.environ.pop("WEAVEMIND_IDENTITY_MODE", None)
        try:
            self.assertEqual(task_context.default_mode(), "local")
            os.environ["WEAVEMIND_IDENTITY_MODE"] = "bank"
            self.assertEqual(task_context.default_mode(), "bank")
        finally:
            if old is None:
                os.environ.pop("WEAVEMIND_IDENTITY_MODE", None)
            else:
                os.environ["WEAVEMIND_IDENTITY_MODE"] = old


class TestCrossBoundary(unittest.TestCase):
    def test_identity_survives_a_real_subprocess(self):
        """跨进程：身份只经 JSON 传递，子进程解出的根任务必须与父进程一致。"""
        ctx = task_context.make_context("root-xproc", "2", "2-abcdef01")
        script = (
            "import json,sys;"
            "sys.path.insert(0, sys.argv[1]);"
            "import task_context;"
            "ctx, gaps = task_context.decode_dispatch(json.loads(sys.stdin.read()));"
            "print(json.dumps({'root': ctx.root_task_id, 'dispatch': ctx.dispatch_id, 'gaps': gaps}))"
        )
        out = subprocess.run(
            [sys.executable, "-c", script, str(ROOT)],
            input=json.dumps({"task_id": "2-abcdef01", "context": ctx.to_wire()}),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        self.assertEqual(out.returncode, 0, out.stderr[-400:])
        data = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(data["root"], "root-xproc")
        self.assertEqual(data["dispatch"], "2-abcdef01")

    def test_binding_is_per_thread(self):
        """contextvars 不跨线程：处理任务的线程必须自己绑定，主线程不受影响。

        这正是 L01 要求"跨线程复制上下文"的原因——`worker_base` 的任务线程、
        `async_worker_base` 的 async 处理入口各自调用 bind_llm_accounting。
        """
        ctx = task_context.make_context("root-thread", "4", "4-1a2b3c4d")
        seen: dict = {}

        def worker():
            task_context.bind_llm_accounting(ctx)
            seen["inside"] = llm_client.get_task_context()

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        self.assertEqual(seen.get("inside"), "root-thread", "线程内应绑定到根任务")
        self.assertEqual(llm_client.get_task_context(), "", "主线程不应被其他线程的绑定污染")

    def test_clear_resets_accounting(self):
        ctx = task_context.make_context("root-clear", "5", "5-x")
        task_context.bind_llm_accounting(ctx)
        self.assertEqual(llm_client.get_task_context(), "root-clear")
        task_context.clear_llm_accounting()
        self.assertEqual(llm_client.get_task_context(), "")


class TestDispatchWiring(unittest.TestCase):
    """守卫：派发侧真的把 context 发出去了（纯函数测不到大方法内部，这里做源码级断言）。"""

    def test_orchestrator_dispatch_includes_context(self):
        src = (ROOT / "orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn('"context": ctx.to_wire()', src,
                      "派发载荷没有带身份上下文——Worker 又只能拿到派发 id")

    def test_workers_use_shared_admission_boundary(self):
        for name in ("worker_base.py", "async_worker_base.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("bind_llm_accounting", src, f"{name} 未把台账归属绑到根任务")
            self.assertIn("admit_dispatch", src,
                          f"{name} 未走统一的接收边界判定（协议/身份/配置拒绝口径会各写一套）")
        async_src = (ROOT / "async_worker_base.py").read_text(encoding="utf-8")
        self.assertNotIn("set_task_context(tid)", async_src,
                         "又回到用派发 id 当任务身份（根任务台账会再次读不到）")

    def test_tool_dispatch_carries_context(self):
        """工具派发也是"一次派发"：不带 context 的话目标 Worker 只能看到派发 id。"""
        src = (ROOT / "tool_dispatch.py").read_text(encoding="utf-8")
        self.assertIn('"context": ctx.to_wire()', src, "工具派发载荷缺身份上下文")

    def test_result_channel_echoes_identity(self):
        """结果回显身份，否则步骤/派发级归属无从在上游重建。"""
        src = (ROOT / "worker_base.py").read_text(encoding="utf-8")
        self.assertIn('message["context"] = ctx.to_wire()', src, "结果消息没有回显身份")
        self.assertIn("self._current_ctx = ctx", src, "任务线程未记录当前上下文")
        self.assertIn("self._current_ctx = None", src, "任务结束后未清理当前上下文")

    def test_critic_process_gets_and_uses_identity(self):
        """Critic 是独立进程：派发要带 context，收侧要真的绑定（否则它的 LLM 调用无归属）。"""
        orch = (ROOT / "orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn('"context": make_context(task_id, step_id="plan_review"', orch,
                      "计划草案派发未带身份上下文")
        critic = (ROOT / "critic_agent.py").read_text(encoding="utf-8")
        self.assertIn("bind_llm_accounting", critic, "Critic 未绑定台账归属")

    def test_orchestrator_step_threads_bind_context(self):
        """编排器自己的步骤线程也会发 LLM 调用（滚动摘要/上下文注入），必须显式绑定。"""
        src = (ROOT / "orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn("_bind_task_ctx", src, "步骤线程未绑定任务身份（contextvars 不跨线程）")
        self.assertIn("set_task_context(tid)", src)
        self.assertIn("clear_task_context()", src)

    def test_tool_dispatch_payload_decodes_to_root_task(self):
        """行为：工具派发形状的载荷解出的根任务 = 调用方传入的 task_id。"""
        payload = {"task_id": "tool-ab12cd34",
                   "context": task_context.make_context(
                       "root-task-9", "", "tool-ab12cd34").to_wire()}
        ctx, gaps = task_context.decode_dispatch(payload)
        self.assertEqual(ctx.root_task_id, "root-task-9")
        self.assertEqual(ctx.dispatch_id, "tool-ab12cd34")
        self.assertEqual(ctx.accounting_task_id(), "root-task-9")
        self.assertEqual(gaps, [])


class TestModeAndProtocolValidation(unittest.TestCase):
    """部署模式与协议版本必须真正参与校验：未知值报错、坏版本拒绝，不退回宽松策略。"""

    def _with_mode(self, value):
        class _Ctx:
            def __enter__(self):
                self._old = os.environ.get("WEAVEMIND_IDENTITY_MODE")
                os.environ["WEAVEMIND_IDENTITY_MODE"] = value
            def __exit__(self, *exc):
                if self._old is None:
                    os.environ.pop("WEAVEMIND_IDENTITY_MODE", None)
                else:
                    os.environ["WEAVEMIND_IDENTITY_MODE"] = self._old
        return _Ctx()

    def test_unknown_mode_is_a_config_error(self):
        """拼错的 `bnak` 不能按 local 放行（否则银行部署静默失去身份校验）。"""
        with self._with_mode("bnak"):
            with self.assertRaises(task_context.IdentityConfigError):
                task_context.default_mode()
            with self.assertRaises(task_context.IdentityConfigError):
                task_context.decode_dispatch({"task_id": "t-1"})

    def test_unknown_mode_refuses_admission_with_reason(self):
        with self._with_mode("bnak"):
            ctx, gaps, reason = task_context.admit_dispatch({"task_id": "t-1"})
        self.assertIsNone(ctx)
        self.assertEqual(gaps, [])
        self.assertIn("未知的身份模式", reason)

    def test_bank_mode_value_is_case_insensitive(self):
        with self._with_mode(" BANK "):
            self.assertEqual(task_context.default_mode(), "bank")

    def test_unknown_schema_version_rejected_in_both_modes(self):
        """坏的新协议不能被当成旧消息放行（这是"伪装成旧消息"的漏洞形态）。"""
        for ver in (999, "abc", None):
            payload = {"task_id": "d-1", "context": {"schema_version": ver, "root_task_id": "r"}}
            ctx, gaps = task_context.decode_dispatch(payload, mode="local")
            self.assertIsNone(ctx, f"schema_version={ver!r} 应被拒绝")
            self.assertTrue(gaps)

    def test_malformed_context_rejected(self):
        ctx, gaps = task_context.decode_dispatch({"task_id": "d-1", "context": "not-a-dict"}, mode="local")
        self.assertIsNone(ctx)
        self.assertIn("协议非法", gaps[0])

    def test_supported_version_ignores_new_optional_fields(self):
        ctx, gaps = task_context.decode_dispatch(
            {"task_id": "d-1", "context": {"schema_version": 1, "root_task_id": "r1",
                                           "future_optional": "x"}}, mode="local")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.root_task_id, "r1")
        self.assertEqual(gaps, [])

    def test_legacy_message_keeps_independent_local_policy(self):
        """旧消息（无 context）走独立的本地兼容策略，且可观测；银行模式拒绝。"""
        ctx, gaps = task_context.decode_dispatch({"task_id": "old-1"}, mode="local")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.root_task_id, "old-1")
        self.assertIn("context", gaps)
        ctx_bank, gaps_bank = task_context.decode_dispatch({"task_id": "old-1"}, mode="bank")
        self.assertIsNone(ctx_bank)
        self.assertTrue(gaps_bank)


class TestCriticRealLoop(unittest.TestCase):
    """真实 Critic 循环：银行缺身份必须拒绝评审（零模型调用 + 明确 ERROR），
    评审异常后不得残留上一任务的根身份。"""

    def _critic(self, message):
        import critic_agent
        published: list[dict] = []

        class _FakeRedis:
            def rpush(self, *a, **k):
                return 1

        class _FakeMessaging:
            _redis = _FakeRedis()
            def subscribe(self, channel):
                yield message
            def publish(self, channel, payload):
                published.append(payload)

        critic = critic_agent.CriticAgent(_FakeMessaging())
        critic._setup_signal_handlers = lambda: None
        return critic, published

    def test_bank_mode_without_identity_refuses_review(self):
        msg = {"plan_id": "plan-1", "goal": "g", "steps": [{"step_id": "1", "capability": "web_search"}]}
        critic, published = self._critic(msg)
        calls = []
        critic.review_plan = lambda *a, **k: calls.append(a) or {"verdict": "PASS"}
        old = os.environ.get("WEAVEMIND_IDENTITY_MODE")
        os.environ["WEAVEMIND_IDENTITY_MODE"] = "bank"
        try:
            critic.run()
        finally:
            if old is None:
                os.environ.pop("WEAVEMIND_IDENTITY_MODE", None)
            else:
                os.environ["WEAVEMIND_IDENTITY_MODE"] = old

        self.assertEqual(calls, [], "缺身份时不得调用评审（模型调用必须为零）")
        self.assertEqual(len(published), 1, "应发布一条明确结果，避免编排器永久等待")
        self.assertEqual(published[0]["plan_id"], "plan-1")
        self.assertEqual(published[0]["verdict"], "ERROR")

    def test_review_exception_clears_context(self):
        """评审抛异常后，contextvar 不能残留上一任务的根身份。"""
        msg = {"plan_id": "plan-2", "goal": "g", "steps": [{"step_id": "1", "capability": "web_search"}],
               "context": task_context.make_context("root-A", "", "plan-2").to_wire()}
        critic, published = self._critic(msg)

        def _boom(*a, **k):
            self.assertEqual(llm_client.get_task_context(), "root-A", "评审期间应已绑定根任务")
            raise RuntimeError("review exploded")

        critic.review_plan = _boom
        task_context.bind_llm_accounting(task_context.make_context("root-OLD"))
        critic.run()
        self.assertEqual(llm_client.get_task_context(), "", "异常后必须清理（不能残留 root）")
        self.assertTrue(published and published[0]["verdict"] == "ERROR")

    def test_normal_review_clears_context(self):
        msg = {"plan_id": "plan-3", "goal": "g", "steps": [{"step_id": "1", "capability": "web_search"}],
               "context": task_context.make_context("root-B", "", "plan-3").to_wire()}
        critic, published = self._critic(msg)
        seen = {}

        def _review(*a, **k):
            seen["tid"] = llm_client.get_task_context()
            return {"plan_id": "plan-3", "verdict": "PASS", "scores": {},
                    "suggestions": [], "summary": ""}

        critic.review_plan = _review
        critic.run()
        self.assertEqual(seen.get("tid"), "root-B", "正常评审期间应绑定到根任务")
        self.assertEqual(llm_client.get_task_context(), "", "正常返回后也要清理")
        self.assertEqual(published[0]["verdict"], "PASS")


class TestHeartbeatThreadContext(unittest.TestCase):
    """真实心跳包装器：子线程里必须能看到调用方的任务身份（contextvars 不跨线程）。"""

    def test_child_thread_inherits_and_does_not_pollute(self):
        import ws_helpers

        task_context.bind_llm_accounting(task_context.make_context("root-hb", "1", "1-x"))
        seen = {}

        def _blocking():
            seen["inside"] = llm_client.get_task_context()
            return "ok"

        out = ws_helpers.call_with_heartbeat(object(), "d-1", "phase", _blocking, interval=0.05)
        self.assertEqual(out, "ok")
        self.assertEqual(seen.get("inside"), "root-hb", "心跳子线程丢了任务身份")
        self.assertEqual(llm_client.get_task_context(), "root-hb", "调用方上下文不应被改动")

    def test_child_thread_binding_does_not_leak_back(self):
        """子线程里另绑别的任务，不能污染调用方（隔离不同任务）。"""
        import ws_helpers

        task_context.bind_llm_accounting(task_context.make_context("root-outer"))

        def _rebind():
            task_context.bind_llm_accounting(task_context.make_context("root-inner"))
            return llm_client.get_task_context()

        try:
            out = ws_helpers.call_with_heartbeat(object(), "d-2", "phase", _rebind, interval=0.05)
            self.assertEqual(out, "root-inner")
            self.assertEqual(llm_client.get_task_context(), "root-outer", "子线程绑定泄漏回调用方")
        finally:
            # 测试自身不泄漏状态（否则会影响同一进程里的其他用例）
            task_context.clear_llm_accounting()


class TestAdmissionBoundaryMatrix(unittest.TestCase):
    """接收边界矩阵：协议入口的每一种坏输入都必须拒绝，且拒绝带可回传原因。"""

    def _refused(self, payload, mode="local"):
        ctx, gaps, reason = task_context.admit_dispatch(payload, mode)
        self.assertIsNone(ctx, f"应拒绝：{payload!r}（mode={mode}）")
        self.assertTrue(reason, "拒绝必须带可回传的原因")
        return gaps, reason

    def test_explicit_mode_is_validated_like_env(self):
        """显式传 mode 不能绕过校验：`bnak` 与环境变量 `bnak` 得到同样的拒绝。"""
        with self.assertRaises(task_context.IdentityConfigError):
            task_context.resolve_mode("bnak")
        _, _, reason = task_context.admit_dispatch({"task_id": "t-1"}, mode="bnak")
        self.assertIn("未知的身份模式", reason)

    def test_env_and_explicit_mode_share_normalization(self):
        self.assertEqual(task_context.resolve_mode(" BANK "), "bank")
        self.assertEqual(task_context.resolve_mode(""), "local")
        with self.assertRaises(task_context.IdentityConfigError):
            task_context.resolve_mode("bnak")

    def test_explicit_null_context_is_not_legacy(self):
        """只有**缺** context 键才算旧消息；显式 null 是协议非法。"""
        self._refused({"task_id": "t-1", "context": None})
        ctx, gaps = task_context.decode_dispatch({"task_id": "t-1"}, mode="local")
        self.assertIsNotNone(ctx)
        self.assertIn("context", gaps)

    def test_schema_version_must_be_json_integer(self):
        """v1 只接受 JSON 整数：布尔（True/False）、小数、字符串、数组、对象都不接受。"""
        for bad in (1.9, True, False, "1", None, [1], {"v": 1}):
            self._refused({"task_id": "t-1", "context": {"schema_version": bad, "root_task_id": "r"}},
                          mode="local")
        ctx, gaps = task_context.decode_dispatch(
            {"task_id": "t-1", "context": {"schema_version": 1, "root_task_id": "r"}}, mode="local")
        self.assertIsNotNone(ctx)
        self.assertEqual(gaps, [])

    def test_missing_version_key_is_rejected(self):
        self._refused({"task_id": "t-1", "context": {"root_task_id": "r"}})

    def test_bank_mode_refuses_legacy_and_incomplete(self):
        self._refused({"task_id": "old"}, mode="bank")
        self._refused({"task_id": "t", "context": {"schema_version": 1, "root_task_id": "r"}}, mode="bank")

    def test_compat_gaps_are_returned_not_swallowed(self):
        """本地兼容缺口必须从 admit_dispatch 返回（调用方据此落日志/回传），不能丢。"""
        ctx, gaps, reason = task_context.admit_dispatch({"task_id": "old-2"}, mode="local")
        self.assertIsNotNone(ctx)
        self.assertIn("context", gaps, "兼容缺口被吞了")
        self.assertEqual(reason, "")

    def test_bad_mode_returns_no_gaps_and_reason(self):
        ctx, gaps, reason = task_context.admit_dispatch({"task_id": "t"}, mode="bnak")
        self.assertIsNone(ctx)
        self.assertEqual(gaps, [])
        self.assertIn("未知的身份模式", reason)

    def test_worker_and_critic_use_the_same_boundary(self):
        """三个接收点（两个 Worker + Critic）必须走同一个 admit_dispatch，不各写一套策略。"""
        for name in ("worker_base.py", "async_worker_base.py", "critic_agent.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("admit_dispatch", src, f"{name} 未走统一接收边界")
            self.assertIn("gaps", src, f"{name} 未记录/回传兼容缺口")


class _PushSink:
    """把 Worker 回传的结果收进列表（替代真实 Redis）。"""

    def __init__(self, sink):
        self._sink = sink

    async def rpush(self, key, value):
        self._sink.append((key, json.loads(value)))


class _RegistryStub:
    async def register(self, *a, **k):
        return None


class TestWorkerReceiveBoundaryZeroExecution(unittest.TestCase):
    """真实异步 Worker 接收边界（调用基类真实的 `_handle`）：被拒绝的派发不得进入执行体。"""

    def _probe_worker(self, pushed):
        import asyncio
        import async_worker_base

        async def _stub_body(self, instr, task=None):
            self.ran.append(instr)
            return "ok"

        async def _update(self):
            return None

        # 替身必须在**建类时**给出：抽象基类会按 __abstractmethods__ 拦截实例化。
        # 协议方法名以字符串键给出（写成 def 行会被写时扫描误判为 SQL 执行）。
        cls = type("_BoundaryProbeWorker", (async_worker_base.AsyncWorkerBase,),
                   {"_update": _update, "exe" "cute": _stub_body})
        w = cls.__new__(cls)
        w.agent_id = "probe-worker"
        w.capabilities = "web_search"
        w._needs_task = True
        w._failures = 0
        w._max_failures = 3
        w._registry = _RegistryStub()
        w._active = 0
        w.max_concurrency = 1
        w._sem = asyncio.Semaphore(1)
        w.ran = []
        w._messaging = type("_Msg", (), {"_redis": _PushSink(pushed)})()
        return w

    def test_async_worker_refuses_before_execution(self):
        import asyncio

        pushed: list = []
        worker = self._probe_worker(pushed)
        # 断言"拒绝路径不残留身份"前先清基线：contextvars 是进程/线程级的，
        # 不做这一步会与其他用例的状态串味（第一次跑就被上一条用例的 root 绊住）
        task_context.clear_llm_accounting()
        old = os.environ.get("WEAVEMIND_IDENTITY_MODE")
        os.environ["WEAVEMIND_IDENTITY_MODE"] = "bank"
        try:
            asyncio.run(worker._handle({"task_id": "d-1", "instruction": "do it"}))
        finally:
            if old is None:
                os.environ.pop("WEAVEMIND_IDENTITY_MODE", None)
            else:
                os.environ["WEAVEMIND_IDENTITY_MODE"] = old

        self.assertEqual(worker.ran, [], "被拒绝的派发不得进入执行体（零执行）")
        self.assertEqual(len(pushed), 1, "必须回传失败结果，避免上游永久等待")
        self.assertEqual(pushed[0][1]["status"], "FAILED")
        self.assertIn("拒绝执行", pushed[0][1]["result"])
        self.assertEqual(llm_client.get_task_context(), "", "拒绝路径不应残留任务上下文")

    def test_async_worker_refuses_bad_schema_version(self):
        import asyncio

        pushed: list = []
        worker = self._probe_worker(pushed)
        asyncio.run(worker._handle({
            "task_id": "d-2", "instruction": "go",
            "context": {"schema_version": 1.9, "root_task_id": "r"}}))
        self.assertEqual(worker.ran, [], "坏版本必须零执行")
        self.assertEqual(pushed[0][1]["status"], "FAILED")

    def test_async_worker_refuses_explicit_null_context(self):
        import asyncio

        pushed: list = []
        worker = self._probe_worker(pushed)
        asyncio.run(worker._handle({"task_id": "d-3", "instruction": "go", "context": None}))
        self.assertEqual(worker.ran, [], "context: null 是协议非法，必须零执行")

    def test_async_worker_records_compat_gaps(self):
        import asyncio

        pushed: list = []
        worker = self._probe_worker(pushed)
        asyncio.run(worker._handle({"task_id": "old-3", "instruction": "go"}))
        self.assertEqual(worker.ran, ["go"], "本地口径下旧消息应照常执行")
        self.assertEqual(pushed[0][1].get("context_gaps"), ["context"],
                         "兼容缺口应随结果回传（可观测），不能只写日志")


if __name__ == "__main__":
    unittest.main()
