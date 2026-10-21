# -*- coding: utf-8 -*-
"""F2 回归：流式回退的根任务归属 + 非 SSE 坏响应 + 探测重复请求。

背景（实机，非推测）：
1. `llm:stream_fallback 没有根任务归属` 的 23 次来自**端点探测**：健康守护线程
   （每 60s）与 `get_balance_status` 的 ThreadPoolExecutor 都没有任务上下文，
   而探测此前复用了 `_send_request` 的"流式优先"路径——网关对 `stream:true` 回
   404，再回退非流式，于是每次探测多一个被拒请求并触发一次未归属记账告警。
2. 日志里相隔 60 秒的两条告警是**两轮探测**，不是一次回退多等 60 秒；代码里
   回退路径没有等待（`_RETRY_BASE=0.3` 只在整次尝试失败后）。
3. 非 SSE 响应此前 `except: chunks.append(body_text)`：HTML 登录页 / 坏 JSON /
   `{"error": ...}` 会被当正文返回，`expect_json=False` 的调用还会写成功缓存。

本文件只打桩传输层与诊断写出，**不发任何真实 HTTP**，也不读写 config/.env。
"""

from __future__ import annotations

import contextlib
import email.message
import json
import shutil
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import llm_client as lc  # noqa: E402
import root_budget as rb  # noqa: E402


LEGAL_BODY = json.dumps({
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "choices": [{
        "index": 0,
        "finish_reason": "stop",
        "message": {"role": "assistant", "content": "正文"},
    }],
    "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
}, ensure_ascii=False).encode("utf-8")

ERROR_OBJECT_BODY = json.dumps({
    "error": {"message": "invalid api key", "type": "authentication_error"},
}).encode("utf-8")

BAD_JSON_BODY = b'{"choices": [{"message": {"content": "half'
HTML_BODY = b"<!DOCTYPE html>\n<html><body>please login</body></html>\n"


class _Resp:
    """最小 HTTP 响应替身：支持 readline/read/迭代/上下文管理器。"""

    def __init__(self, body: bytes, content_type: str = "application/json"):
        self._body = body
        self._pos = 0
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def readline(self) -> bytes:
        end = self._body.find(b"\n", self._pos)
        if end < 0:
            end = len(self._body)
        chunk = self._body[self._pos:end + 1]
        self._pos = min(len(self._body), end + 1)
        return chunk

    def read(self) -> bytes:
        chunk = self._body[self._pos:]
        self._pos = len(self._body)
        return chunk

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _urlopen(body: bytes, content_type: str = "application/json"):
    def _fake(req, timeout=None):
        return _Resp(body, content_type)
    return _fake


@contextlib.contextmanager
def _offline():
    """离线：无备用端点、端点健康、不热重载、不真等待、不写 Redis。"""
    rec = {
        "calls": [], "published": [], "cache": [],
        "stream_usage": [], "sleeps": [],
    }

    def _record_call(task_id=None, **fields):
        rec["calls"].append(dict(fields, task_id=task_id))

    def _sleep(seconds):
        rec["sleeps"].append(seconds)

    with mock.patch.object(lc, "_ensure_cfg_fresh"), \
            mock.patch.object(lc, "_primary_healthy", lambda: True), \
            mock.patch.object(lc, "_backup_healthy", lambda: False), \
            mock.patch.object(lc, "_BACKUP_CFG", {}), \
            mock.patch.object(lc, "_mark_endpoint"), \
            mock.patch.object(lc, "_record_task_degradation"), \
            mock.patch.object(lc, "_record_llm_call", side_effect=_record_call), \
            mock.patch.object(lc, "_record_usage"), \
            mock.patch.object(lc, "_bump_task_budget"), \
            mock.patch.object(lc, "_publish_usage_snapshot"), \
            mock.patch.object(
                lc, "_publish_stream_chunk",
                side_effect=lambda text: rec["published"].append(text)), \
            mock.patch.object(
                lc, "_record_stream_usage",
                side_effect=lambda usage, model: rec["stream_usage"].append(
                    (dict(usage), model))), \
            mock.patch.object(
                lc, "_cache_set",
                side_effect=lambda *args: rec["cache"].append(args)), \
            mock.patch.object(lc.time, "sleep", side_effect=_sleep):
        yield rec


class _F2Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_f2_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        lc.clear_task_context()
        lc._root_budgets.clear()
        self.addCleanup(lc.clear_task_context)
        self.addCleanup(lc._root_budgets.clear)

    def _ledger(self, task_id: str) -> rb.RootBudget:
        """真实账本（无 Redis、无上限），只让 LLM 客户端按根任务记账。"""
        budget = rb.RootBudget(task_id, self.tmp, rb.BudgetLimits())
        lc._root_budgets[task_id] = budget
        return budget

    def _client(self, retries: int = 1) -> lc.LLMClient:
        client = lc.LLMClient(
            base_url="https://api.example/v1", api_key="k", model="m")
        client._MAX_RETRIES = retries
        client._RETRY_BASE = 0
        return client


class TestRootAttribution(_F2Base):
    """① 有可靠归属时正常记账（回退那次发送单独一张票）。"""

    def test_fallback_with_root_is_accounted(self):
        budget = self._ledger("t-f2-root")
        lc.set_task_context("t-f2-root")
        sends = []

        def _fake_stream(*_a, **_k):
            sends.append("stream")
            raise lc.LLMCallError("HTTP 404: stream unsupported")

        def _fake_urlopen(req, timeout=None):
            sends.append("nonstream")
            return _Resp(LEGAL_BODY)

        client = self._client()
        with _offline() as rec, \
                mock.patch.object(lc, "_stream_enabled", lambda: True), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream), \
                mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
            out = client.call("sys", "u", expect_json=False)

        self.assertEqual(out, {"content": "正文"})
        self.assertEqual(sends, ["stream", "nonstream"], "两次真实发送")
        snap = budget.snapshot()
        self.assertEqual(snap["stages"]["llm"]["reserved"], 2,
                         "流式 1 张 + 回退 1 张")
        self.assertEqual(snap["stages"]["llm"]["settled"], 2)
        self.assertEqual(snap["tokens"]["actual"], 5,
                         "回退那次按供应商实际 completion_tokens 记账")
        self.assertEqual(snap["tokens"]["unknown_calls"], 1,
                         "没有 usage 的流式那次照实记未知，不填 0")
        self.assertTrue(any(r.get("end_reason") == "stream_fallback_ok"
                            for r in rec["calls"]),
                        "回退成功必须逐次记录")
        self.assertFalse([c for c in rec["cache"] if c and c[0]],
                         "没有 cache_key 时不写缓存")


class TestUnattributedDiagnostic(_F2Base):
    """② 无归属时保留诊断，且费用/token 写未知（不是 0）。"""

    def test_no_root_is_visible_and_unknown_not_zero(self):
        lc.clear_task_context()
        before = lc.get_unattributed_diagnostics()
        before_missing = lc.no_root_call_count()

        budget, ticket = lc._budget_open(
            "llm", 1, 128, usage="llm:stream_fallback")
        after = lc.get_unattributed_diagnostics()

        self.assertIsNone(budget)
        self.assertEqual(ticket, "")
        self.assertEqual(after["calls"], before["calls"] + 1,
                         "未归属调用必须可见，不能静默丢弃")
        self.assertEqual(lc.no_root_call_count(), before_missing + 1,
                         "本应有根却丢失的调用仍要计入告警计数")
        self.assertIsNone(after["tokens"], "没有可靠归属时 token 写未知")
        self.assertIsNone(after["cost_usd"], "没有价格口径时费用写未知，不能写 0")
        self.assertFalse(after["cost_known"])
        self.assertEqual(after["tokens_unknown_calls"],
                         before["tokens_unknown_calls"] + 1)
        self.assertFalse((self.tmp / "budget_state.json").exists(),
                         "不得为无归属调用补造任何任务账本")


class TestBadNonSSEResponse(_F2Base):
    """③④⑤ 非 SSE 坏响应必须失败，且不能发布成正文/进入成功缓存。"""

    def test_html_is_failure_and_not_published(self):
        with _offline() as rec, \
                mock.patch.object(urllib.request, "urlopen",
                                  _urlopen(HTML_BODY, "text/html")):
            with self.assertRaises(lc.LLMBadResponseError):
                lc._call_llm_stream_once(
                    "https://api.example/v1", "k", "m", "sys", "u",
                    publish=True)
        self.assertEqual(rec["published"], [], "HTML 不得作为成功片段发布")

    def test_html_is_not_returned_as_content_or_cached(self):
        client = self._client()
        with _offline() as rec, \
                mock.patch.object(lc, "_stream_enabled", lambda: False), \
                mock.patch.object(urllib.request, "urlopen",
                                  _urlopen(HTML_BODY, "text/html")):
            with self.assertRaises(lc.LLMCallError):
                client.call("sys", "u", expect_json=False,
                            cache_key="f2-html")
        self.assertEqual(rec["cache"], [], "HTML 不得进入成功缓存")
        self.assertFalse(any(r.get("end_reason") == "ok"
                             for r in rec["calls"]))
        self.assertTrue(any(r.get("error_class") == "bad_response"
                            for r in rec["calls"]),
                        "必须按 bad_response 记账")

    def test_bad_json_is_failure_and_not_published(self):
        with _offline() as rec, \
                mock.patch.object(urllib.request, "urlopen",
                                  _urlopen(BAD_JSON_BODY)):
            with self.assertRaises(lc.LLMBadResponseError):
                lc._call_llm_stream_once(
                    "https://api.example/v1", "k", "m", "sys", "u",
                    publish=True)
        self.assertEqual(rec["published"], [])

    def test_vendor_error_object_is_failure_and_fallback_ticket_fails(self):
        budget = self._ledger("t-f2-vendor")
        lc.set_task_context("t-f2-vendor")
        sends = []

        def _fake_stream(*_a, **_k):
            sends.append("stream")
            raise lc.LLMCallError("HTTP 404: stream unsupported")

        def _fake_urlopen(req, timeout=None):
            sends.append("nonstream")
            return _Resp(ERROR_OBJECT_BODY)

        notes: list = []
        _real_close = lc._budget_close

        def _close(_b, _t, **kwargs):
            notes.append(str(kwargs.get("note") or ""))
            return _real_close(_b, _t, **kwargs)

        client = self._client()
        with _offline() as rec, \
                mock.patch.object(lc, "_budget_close", _close), \
                mock.patch.object(lc, "_stream_enabled", lambda: True), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream), \
                mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
            with self.assertRaises(lc.LLMCallError):
                client.call("sys", "u", expect_json=False,
                            cache_key="f2-vendor")

        self.assertEqual(sends, ["stream", "nonstream"])
        self.assertEqual(rec["cache"], [], "供应商错误对象不得进入成功缓存")
        fails = [r for r in rec["calls"]
                 if r.get("end_reason") == "stream_fallback_failed"]
        self.assertTrue(fails, "回退失败必须逐次记录")
        self.assertFalse([r for r in rec["calls"]
                          if r.get("end_reason") == "stream_fallback_ok"],
                         "错误对象不得记成 stream_fallback_ok")
        self.assertTrue(all(r.get("error_class") == "bad_response"
                            for r in fails))
        entry = budget.state.stages["llm"]
        self.assertTrue(entry.get("last_failed"),
                        "回退这张票必须按失败结算")
        self.assertIn("llm:nonstream_failed", notes,
                      "回退那张票必须按失败结算")
        self.assertNotIn("llm:nonstream_ok", notes,
                         "供应商错误对象不得按成功结算回退票")
        self.assertEqual(budget.snapshot()["stages"]["llm"]["settled"], 2,
                         "两次发送都结算（不是漏记）")

    def test_async_fallback_error_object_fails_ticket(self):
        """异步回退同样先校验再结算：error 对象不得记成 stream_fallback_ok。"""
        import asyncio

        budget = self._ledger("t-f2-async")
        lc.set_task_context("t-f2-async")
        req = httpx.Request(
            "POST", "https://api.example/v1/chat/completions")

        class _StreamCtx:
            async def __aenter__(self):
                return httpx.Response(415, request=req)

            async def __aexit__(self, *_exc):
                return False

        class _AsyncClient:
            def stream(self, *_a, **_k):
                return _StreamCtx()

            async def post(self, *_a, **_k):
                return httpx.Response(
                    200, request=req,
                    json=json.loads(ERROR_OBJECT_BODY.decode("utf-8")))

        notes: list = []
        _real_close = lc._budget_close

        def _close(_b, _t, **kwargs):
            notes.append(str(kwargs.get("note") or ""))
            return _real_close(_b, _t, **kwargs)

        with _offline(), \
                mock.patch.object(lc, "_budget_close", _close), \
                mock.patch.object(lc, "_stream_enabled", lambda: True):
            with self.assertRaises(lc.LLMBadResponseError):
                asyncio.run(lc._async_chat_once(
                    _AsyncClient(),
                    "https://api.example/v1/chat/completions",
                    {"max_tokens": 128,
                     "messages": [{"role": "user", "content": "u"}]},
                    {},
                ))

        self.assertIn("llm:nonstream_failed", notes)
        self.assertNotIn("llm:nonstream_ok", notes,
                         "error 对象不得按成功结算异步回退票")
        snap = budget.snapshot()
        self.assertEqual(snap["stages"]["llm"]["reserved"], 1)
        self.assertEqual(snap["stages"]["llm"]["settled"], 1,
                         "失败也要结算（不是漏记）")

    def test_async_call_bad_response_is_recorded_failure(self):
        """异步整链：HTML 200 → 失败、记 bad_response 形状、不缓存。"""
        import asyncio

        budget = self._ledger("t-f2-async-call")
        lc.set_task_context("t-f2-async-call")
        req = httpx.Request(
            "POST", "https://api.example/v1/chat/completions")

        class _HtmlClient:
            async def post(self, *_a, **_k):
                return httpx.Response(
                    200, request=req, content=HTML_BODY,
                    headers={"content-type": "text/html"})

        with _offline() as rec, \
                mock.patch.object(lc, "_get_async_client",
                                  lambda: _HtmlClient()), \
                mock.patch.object(lc, "_endpoint_guard", lambda u: u), \
                mock.patch.object(lc, "_task_budget_limits",
                                  lambda: {"max_calls": 0, "max_seconds": 0.0,
                                           "max_cost_usd": 0.0}), \
                mock.patch("asyncio.sleep", new=mock.AsyncMock()):
            with self.assertRaises(lc.LLMCallError):
                asyncio.run(lc.call_llm_async(
                    "sys", "u", expect_json=False, max_attempts=1,
                    cache_key="f2-async-html"))

        self.assertEqual(rec["cache"], [], "HTML 不得进入成功缓存")
        bad = [r for r in rec["calls"]
               if r.get("error_class") == "bad_response"]
        self.assertTrue(bad, "异步路径也要留调用形状")
        self.assertEqual(bad[-1].get("end_reason"), "exhausted")
        self.assertEqual(
            budget.snapshot()["stages"]["llm"]["settled"], 1,
            "失败也要结算（不是漏记）")


class TestLegalNonSSE(_F2Base):
    """⑥ 合法非 SSE JSON 仍然成功（并发布一次、记 usage）。"""

    def test_valid_non_sse_json_still_works(self):
        out_info: dict = {}
        with _offline() as rec, \
                mock.patch.object(urllib.request, "urlopen",
                                  _urlopen(LEGAL_BODY)):
            text = lc._call_llm_stream_once(
                "https://api.example/v1", "k", "m", "sys", "u",
                publish=True, out=out_info)

        self.assertEqual(text, "正文")
        self.assertEqual(rec["published"], ["正文"])
        self.assertEqual(out_info["usage"]["completion_tokens"], 5)
        self.assertEqual(out_info["finish_reason"], "stop")
        self.assertEqual(rec["stream_usage"][0][0]["prompt_tokens"], 3,
                         "非 SSE 的 usage 也要进全局用量")

    def test_valid_non_sse_json_succeeds_through_call_and_caches(self):
        client = self._client()
        with _offline() as rec, \
                mock.patch.object(lc, "_stream_enabled", lambda: False), \
                mock.patch.object(urllib.request, "urlopen",
                                  _urlopen(LEGAL_BODY)):
            out = client.call("sys", "u", expect_json=False,
                              cache_key="f2-legal")
        self.assertEqual(out, {"content": "正文"})
        self.assertEqual(len(rec["cache"]), 1, "合法结果可以进成功缓存")


class TestRequestAndRetryPath(_F2Base):
    """B3：定位请求/重试路径——只有一次被拒流式 + 一次非流式，没有额外等待。"""

    def test_404_fallback_is_one_extra_request_without_retry_sleep(self):
        self._ledger("t-f2-404")
        lc.set_task_context("t-f2-404")
        sends = []

        def _fake_stream(*_a, **_k):
            sends.append("stream")
            raise lc.LLMCallError("HTTP 404: stream unsupported")

        def _fake_urlopen(req, timeout=None):
            sends.append("nonstream")
            return _Resp(LEGAL_BODY)

        client = self._client()
        with _offline() as rec, \
                mock.patch.object(lc, "_stream_enabled", lambda: True), \
                mock.patch.object(lc, "_call_llm_stream_once", _fake_stream), \
                mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
            client.call("sys", "u", expect_json=False)

        self.assertEqual(sends, ["stream", "nonstream"])
        self.assertFalse([s for s in rec["sleeps"] if s >= client._RETRY_BASE],
                         "回退路径不得插入重试等待")

    def test_probe_is_explicitly_non_streaming_and_unattributed(self):
        seen: dict = {}

        class _FakeClient:
            def __init__(self, **_kw):
                pass

            def _send_request(self, *_a, **kwargs):
                seen.update(kwargs)
                self._last_usage = {"prompt_tokens": 2, "completion_tokens": 1}
                return "ok"

        lc.clear_task_context()
        before = lc.get_unattributed_diagnostics()
        before_missing = lc.no_root_call_count()
        with mock.patch.object(lc, "LLMClient", _FakeClient):
            status = lc._probe_endpoint_status(
                "https://api.example/v1", "k", "m")
        after = lc.get_unattributed_diagnostics()

        self.assertEqual(status, {"ok": True, "reason": "ok"})
        self.assertIs(seen.get("stream"), False,
                      "探测必须显式非流式（不制造 404→回退）")
        self.assertEqual(after["calls"], before["calls"] + 1,
                         "探测仍是未归属调用：留诊断，不写任务账本")
        self.assertEqual(lc.no_root_call_count(), before_missing,
                         "探测不是『丢了根』的任务调用，不计入告警计数")
        self.assertIsNone(after["cost_usd"])
        self.assertEqual(after["tokens_actual"],
                         before["tokens_actual"] + 3,
                         "探测取到用量就记实际 token")


if __name__ == "__main__":
    unittest.main()
