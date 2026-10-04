# -*- coding: utf-8 -*-
"""思考型供应商的逃生门 + 线程池记账归属（2026-10-04 实机 ui-1b155d4f84 反例）。

两条真实现象（不是推测）：
1. `deepseek-flash` 在该网关上**是思考模型**：长输入+大输出时它把输出预算全烧在
   `reasoning_content` 上，`content` 恒空且 `finish_reason=length`。而放大分支只覆盖
   `max_tok < 8192`，content_summary 的合并调用本来就要 8192 → 一次都不放大，直接切
   备用；备用端点又是**同一网关同一模型** → "Backup LLM also failed" 必然发生，
   两步 content_summary 各 300s×3 轮超时，整单跑不出报告。
2. worker 的同步 LLM 调用跑在 `run_in_executor` 线程里：contextvars 不跨线程 →
   一小时 52 条「模型调用没有根任务归属…本次不记账」，根任务额度账本对最贵的那条
   路径形同虚设。

本文件只打桩传输层，**不发任何真实请求**。
"""
import asyncio
import contextlib
import json
import os
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import llm_client as lc  # noqa: E402
from async_worker_base import AsyncWorkerBase  # noqa: E402


def _thinking_exc():
    exc = lc.LLMCallError("Empty content in LLM response (thinking budget exhausted)")
    exc.thinking_budget_exhausted = True
    return exc


def _fresh_health():
    return {
        "primary": {"healthy": True, "fails": 0,
                    "last_degradation_reason": "", "last_degradation_ts": 0},
        "backup": {"healthy": True, "fails": 0,
                   "last_degradation_reason": "", "last_degradation_ts": 0},
    }


@contextlib.contextmanager
def _offline():
    """离线环境：无备用端点、端点判定健康、不热重载、不真等待。"""
    with mock.patch.object(lc, "_BACKUP_CFG", {}), \
            mock.patch.object(lc, "_primary_healthy", lambda: True), \
            mock.patch.object(lc, "_backup_healthy", lambda: False), \
            mock.patch.object(lc, "_endpoint_health", _fresh_health()), \
            mock.patch.object(lc, "_ensure_cfg_fresh"), \
            mock.patch.object(lc.time, "sleep"):
        yield


class TestSyncLadderAndEscape(unittest.TestCase):
    """`LLMClient.call()`：先放大（不再止步 8192），到顶后关思考重发一次。"""

    def setUp(self):
        lc.clear_task_context()
        self.addCleanup(lc.clear_task_context)

    def _client(self, send, retries=2):
        class _C(lc.LLMClient):
            _MAX_RETRIES = retries
            _RETRY_BASE = 0

            def _send_request(self, *a, **k):
                return send(*a, **k)

        return _C(base_url="https://api.example/v1", api_key="k", model="m")

    def test_scales_past_8192_then_disables_thinking(self):
        """调用方给 8192：必须还能放大到 16384；二次仍空则关思考重发并成功。"""
        seen = []

        def _send(system, user, temperature, max_tokens, model=None,
                  endpoint="primary", timeout=None, no_thinking=False):
            seen.append((max_tokens, bool(no_thinking)))
            if no_thinking:
                return '{"ok": true}'
            raise _thinking_exc()

        client = self._client(_send)
        with _offline():
            out = client.call("sys", "u", max_tokens=8192, expect_json=True)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(
            seen, [(8192, False), (16384, False), (16384, True)],
            "必须是：放大一次 → 到顶后关思考重发一次（旧实现 8192 起一次都不放大）",
        )

    def test_escalation_success_skips_thinking_escape(self):
        """放大就能出正文时，不得顺手把思考关掉（保持原有质量口径）。"""
        seen = []

        def _send(system, user, temperature, max_tokens, model=None,
                  endpoint="primary", timeout=None, no_thinking=False):
            seen.append((max_tokens, bool(no_thinking)))
            if max_tokens >= 16384:
                return '{"ok": true}'
            raise _thinking_exc()

        client = self._client(_send)
        with _offline():
            out = client.call("sys", "u", max_tokens=8192, expect_json=True)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(seen, [(8192, False), (16384, False)])

    def test_escape_failure_is_not_endpoint_failure(self):
        """关思考也失败：不得把主端点标成不健康（端点是通的，问题在模型行为）。"""
        def _send(system, user, temperature, max_tokens, model=None,
                  endpoint="primary", timeout=None, no_thinking=False):
            raise _thinking_exc()

        client = self._client(_send)
        with _offline():
            with self.assertRaises(lc.LLMCallError):
                client.call("sys", "u", max_tokens=8192, expect_json=True)
            self.assertTrue(lc._endpoint_health["primary"]["healthy"],
                            "思考烧光预算不是端点故障，不得标记不健康")


class TestStreamPathEscape(unittest.TestCase):
    """`call_llm_stream()`（content_summary 合并调用走的正是这条）。"""

    def setUp(self):
        lc.clear_task_context()
        self.addCleanup(lc.clear_task_context)

    def test_thinking_empty_then_disabled_retry_succeeds(self):
        seen = []

        def _fake_stream_once(base_url, api_key, model, system, user, *a,
                              out=None, no_thinking=False, **k):
            seen.append(bool(no_thinking))
            if no_thinking:
                if out is not None:
                    out["usage"] = {"completion_tokens": 7}
                return "正文"
            if out is not None:
                out["reasoning"] = True
                out["finish_reason"] = "length"
            return ""

        with _offline(), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream_once):
            text = lc.call_llm_stream("sys", "u", max_tokens=8192, usage="exec")
        self.assertEqual(text, "正文")
        self.assertEqual(seen, [False, True],
                         "空正文+reasoning 必须先判成思考耗尽，再关思考重发一次")

    def test_exhausted_thinking_keeps_endpoint_healthy(self):
        """关思考仍空：抛错但不把端点标不健康（否则后续调用全被绕去备用）。"""
        def _fake_stream_once(base_url, api_key, model, system, user, *a,
                              out=None, no_thinking=False, **k):
            if out is not None:
                out["reasoning"] = True
                out["finish_reason"] = "length"
            return ""

        with _offline(), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream_once):
            with self.assertRaises(lc.LLMCallError) as ctx:
                lc.call_llm_stream("sys", "u", max_tokens=8192)
            self.assertTrue(getattr(ctx.exception, "thinking_budget_exhausted", False)
                            or "thinking" in str(ctx.exception),
                            f"异常要能看出是思考耗尽：{ctx.exception}")
            self.assertTrue(lc._endpoint_health["primary"]["healthy"],
                            "思考耗尽不是端点故障")


class _FakeSSE:
    """最小 SSE 响应替身：先 readline 再迭代。"""

    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else b""

    def __iter__(self):
        return iter(self._lines)


class TestTransportCarriesThinkingSwitch(unittest.TestCase):
    """关思考必须真的写进请求体（不是只传参不落地）。"""

    def test_stream_body_sets_thinking_disabled(self):
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeSSE([
                'data: {"choices":[{"delta":{"content":"好"}}]}\n'.encode("utf-8"),
                b"data: [DONE]\n",
            ])

        with mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
            text = lc._call_llm_stream_once(
                "https://api.example/v1", "k", "m", "s", "u",
                publish=False, no_thinking=True,
            )
        self.assertEqual(text, "好")
        self.assertEqual(captured["body"].get("thinking"), {"type": "disabled"},
                         "流式请求体必须带 thinking=disabled")

    def test_stream_body_defaults_to_thinking_on(self):
        """默认不关思考：只有确认烧光预算后才用这条出路。"""
        captured = {}

        def _fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeSSE([
                'data: {"choices":[{"delta":{"content":"好"}}]}\n'.encode("utf-8"),
            ])

        with mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
            lc._call_llm_stream_once("https://api.example/v1", "k", "m", "s", "u",
                                     publish=False)
        self.assertNotIn("thinking", captured["body"])


class TestAsyncPathRetriesWithoutThinking(unittest.TestCase):
    """异步路径（报告生成走这条）：空正文后的下一次尝试关思考。"""

    def setUp(self):
        lc.clear_task_context()
        self.addCleanup(lc.clear_task_context)

    def test_second_attempt_disables_thinking(self):
        seen = []

        async def _fake_chat(client, url, payload, headers, info=None):
            seen.append(dict(payload))
            if len(seen) < 3:
                return {"choices": [{"message": {"content": ""}}], "usage": {}}
            return {"choices": [{"message": {"content": "正文"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4}}

        env = {"LLM_API_KEY": "k", "LLM_BASE_URL": "https://api.example/v1",
               "LLM_MODEL": "m"}
        with _offline(), \
                mock.patch.object(lc, "_get_async_client", lambda: object()), \
                mock.patch.object(lc, "_endpoint_guard", lambda u: u), \
                mock.patch.object(lc, "_async_chat_once", _fake_chat), \
                mock.patch.dict(os.environ, env):
            out = asyncio.run(lc.call_llm_async("sys", "u", expect_json=False,
                                                max_attempts=3))
        self.assertEqual(out, "正文")
        self.assertNotIn("thinking", seen[0], "第一次尝试不关思考")
        self.assertEqual(seen[1].get("thinking"), {"type": "disabled"},
                         "空正文后的下一次尝试必须关掉思考")
        self.assertEqual(seen[2].get("thinking"), {"type": "disabled"},
                         "后续尝试保持关思考，直到出正文")


class TestThreadExecutorKeepsAccounting(unittest.TestCase):
    """线程池记账归属：`_run_sync` 必须把任务上下文带进线程。"""

    def setUp(self):
        lc.clear_task_context()
        self.addCleanup(lc.clear_task_context)

    class _Worker(AsyncWorkerBase):
        async def execute(self, instruction: str) -> str:
            return ""

    def test_run_sync_propagates_task_context(self):
        worker = self._Worker.__new__(self._Worker)

        async def main():
            lc.set_task_context("ui-thread-ctx")
            return await worker._run_sync(lc.get_task_context)

        self.assertEqual(asyncio.run(main()), "ui-thread-ctx")

    def test_plain_executor_thread_loses_task_context(self):
        """反例（修复前的现场）：直接 run_in_executor 读不到根任务。"""
        async def main():
            lc.set_task_context("ui-thread-ctx-2")
            return await asyncio.get_running_loop().run_in_executor(
                None, lc.get_task_context)

        self.assertEqual(asyncio.run(main()), "",
                         "contextvars 不跨线程：这正是 52 次未记账的来源")

    def test_llm_workers_use_context_carrying_entry(self):
        """守卫：会发 LLM 调用的线程池入口必须走 `_run_sync`。"""
        for name in ("workers/content_summary_worker.py",
                     "workers/packaging_worker.py"):
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("_run_sync", src,
                          f"{name} 的线程池入口没带任务上下文（记账会再次丢归属）")
            self.assertNotIn("run_in_executor(None, _sync)", src)
            self.assertNotIn("run_in_executor(\n            None, self._sync_package",
                             src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
