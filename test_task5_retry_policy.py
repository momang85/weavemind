# -*- coding: utf-8 -*-
"""任务5 统一重试策略层验收（docs/arch_task5_retry_policy.md）：

- mock ConnectionError 连抛 2 次后成功 → fn 被调用 3 次，等待符合指数退避（2s→4s）；
- mock 429 连抛 6 次 → 第 6 次抛出后失败，前 5 次各等 60s；
- max=0 策略（model_ctx / model_deny）不重试、不等待，异常直接上抛；
- parse 固定等待 1s、重试 2 次（共 3 次调用）；
- 模型异常关键词归类：context/too long → model_ctx，rate/429 → rate_limit，
  refuse/content/policy → model_deny，其余 → None；
- _process_task 汇聚口把 model_ctx / model_deny 标记进失败回传（不重跑 execute）。

离线，不连真实 Redis、不调模型。
"""

from __future__ import annotations

import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import worker_base  # noqa: E402
from worker_base import RETRY_POLICY, classify_model_error, retry_with_policy  # noqa: E402


class TestRetryPolicyTable(unittest.TestCase):
    """策略表数值必须与架构师指定一致，不得自行调整。"""

    def test_table_values_match_design(self):
        self.assertEqual(RETRY_POLICY["network"],
                         {"max": 3, "backoff": "exponential", "base": 2.0})
        self.assertEqual(RETRY_POLICY["rate_limit"],
                         {"max": 5, "backoff": "fixed", "wait": 60})
        self.assertEqual(RETRY_POLICY["parse"],
                         {"max": 2, "backoff": "fixed", "wait": 1})
        self.assertEqual(RETRY_POLICY["model_ctx"], {"max": 0, "backoff": None})
        self.assertEqual(RETRY_POLICY["model_deny"], {"max": 0, "backoff": None})


class TestNetworkExponential(unittest.TestCase):
    """验收①：ConnectionError 连抛 2 次后成功 → 3 次调用，等待 2s→4s。"""

    def test_two_failures_then_success(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) <= 2:
                raise ConnectionError("redis 连接中断")
            return "ok"

        with mock.patch("worker_base.time.sleep") as fake_sleep:
            result = retry_with_policy(flaky, ConnectionError, "network")

        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3, "首次 + 2 次重试 = 3 次调用")
        waits = [c.args[0] for c in fake_sleep.call_args_list]
        self.assertEqual(waits, [2.0, 4.0], "指数退避 2s→4s（base=2.0 × 2**attempt）")

    def test_exhaustion_reraises_last_error(self):
        calls = []

        def always_down():
            calls.append(1)
            raise ConnectionError("still down")

        with mock.patch("worker_base.time.sleep") as fake_sleep:
            with self.assertRaises(ConnectionError):
                retry_with_policy(always_down, ConnectionError, "network")

        self.assertEqual(len(calls), 4, "network max=3 → 首次 + 3 次重试")
        waits = [c.args[0] for c in fake_sleep.call_args_list]
        self.assertEqual(waits, [2.0, 4.0, 8.0], "耗尽前等待 2s→4s→8s")


class TestRateLimitFixed(unittest.TestCase):
    """验收②：429 连抛 6 次 → 第 6 次抛出后失败，前 5 次各等 60s。"""

    def test_six_failures_then_fail(self):
        class RateLimit429(Exception):
            pass

        calls = []

        def always_429():
            calls.append(1)
            raise RateLimit429("429 Too Many Requests")

        with mock.patch("worker_base.time.sleep") as fake_sleep:
            with self.assertRaises(RateLimit429):
                retry_with_policy(always_429, RateLimit429, "rate_limit")

        self.assertEqual(len(calls), 6, "首次 + 5 次重试 = 6 次调用，第 6 次失败")
        waits = [c.args[0] for c in fake_sleep.call_args_list]
        self.assertEqual(waits, [60.0] * 5, "固定等待：前 5 次各等 60s")


class TestNoRetryPolicies(unittest.TestCase):
    """max=0：model_ctx / model_deny 不重试、不等待，异常直接上抛。"""

    def test_model_ctx_no_retry(self):
        calls = []

        def ctx_boom():
            calls.append(1)
            raise RuntimeError("model context too long: 200k > 128k")

        with mock.patch("worker_base.time.sleep") as fake_sleep:
            with self.assertRaises(RuntimeError):
                retry_with_policy(ctx_boom, RuntimeError, "model_ctx")

        self.assertEqual(calls, [1], "max=0 → 只调用一次")
        fake_sleep.assert_not_called()

    def test_model_deny_no_retry(self):
        calls = []

        def deny_boom():
            calls.append(1)
            raise RuntimeError("request refused due to content policy")

        with mock.patch("worker_base.time.sleep") as fake_sleep:
            with self.assertRaises(RuntimeError):
                retry_with_policy(deny_boom, RuntimeError, "model_deny")

        self.assertEqual(calls, [1], "max=0 → 只调用一次")
        fake_sleep.assert_not_called()

    def test_parse_fixed_wait(self):
        calls = []

        def parse_fail_twice():
            calls.append(1)
            if len(calls) <= 2:
                raise ValueError("bad pdf")
            return "parsed"

        with mock.patch("worker_base.time.sleep") as fake_sleep:
            result = retry_with_policy(parse_fail_twice, ValueError, "parse")

        self.assertEqual(result, "parsed")
        self.assertEqual(len(calls), 3)
        waits = [c.args[0] for c in fake_sleep.call_args_list]
        self.assertEqual(waits, [1.0, 1.0], "parse：固定等待 1s × 2 次")


class TestClassifyModelError(unittest.TestCase):
    """关键词归类（docs §4）：ctx 优先，其次 rate，最后 deny；其余 None。"""

    def test_rules(self):
        self.assertEqual(classify_model_error(
            RuntimeError("model context too long: 91281 tokens")), "model_ctx")
        self.assertEqual(classify_model_error(
            RuntimeError("ContextWindowExceededError")), "model_ctx")
        self.assertEqual(classify_model_error(
            RuntimeError("HTTP 429 Too Many Requests")), "rate_limit")
        self.assertEqual(classify_model_error(
            RuntimeError("rate limit exceeded")), "rate_limit")
        self.assertEqual(classify_model_error(
            RuntimeError("request refused due to content policy")), "model_deny")
        self.assertIsNone(classify_model_error(OSError("disk full")))
        self.assertIsNone(classify_model_error(ValueError("bad json")))


class TestProcessTaskWiring(unittest.TestCase):
    """_process_task 汇聚口：model_ctx / model_deny 标记进失败回传，不重跑 execute。"""

    def _worker(self):
        from worker_base import BaseWorker

        class _W(BaseWorker):
            def execute(self, instruction: str) -> str:      # 抽象方法的具体实现
                raise AssertionError("测试替身应替换 execute")

        w = _W.__new__(_W)
        w.agent_id = "retry-test"
        w._contract = None
        w._permit = None
        w._search_status = None
        w._current_ctx = None
        w._current_gaps = []
        # 绕过许可存取（恒通过），只替换"真正发布"两处 I/O
        w._task_permit = lambda task: object()
        w._permit_gate = lambda permit: None
        w._install_permit_guard = lambda permit: None
        w._publish_result = lambda tid, status, result: None
        published = []
        w._publish_failure = lambda tid, err: published.append((tid, err))
        return w, published

    def test_ctx_error_marked_without_rerun(self):
        w, published = self._worker()
        executions = []

        def failing_execute(instruction):
            executions.append(instruction)
            raise RuntimeError("prompt context too long for model")

        w.execute = failing_execute
        w._process_task({"task_id": "t-ctx", "instruction": "查一下"})

        self.assertEqual(executions, ["查一下"], "只执行一次，不重跑 execute")
        self.assertEqual(len(published), 1)
        tid, err = published[0]
        self.assertEqual(tid, "t-ctx")
        self.assertTrue(err.startswith("model_ctx: "), f"失败回传带归类标记：{err}")

    def test_deny_error_marked_without_rerun(self):
        w, published = self._worker()
        executions = []

        def failing_execute(instruction):
            executions.append(instruction)
            raise RuntimeError("request refused due to content policy")

        w.execute = failing_execute
        w._process_task({"task_id": "t-deny", "instruction": "查一下"})

        self.assertEqual(executions, ["查一下"], "只执行一次，不重跑 execute")
        self.assertEqual(len(published), 1)
        tid, err = published[0]
        self.assertEqual(tid, "t-deny")
        self.assertTrue(err.startswith("model_deny: "), f"失败回传带归类标记：{err}")

    def test_unclassified_error_unchanged(self):
        w, published = self._worker()

        def failing_execute(instruction):
            raise ValueError("普通业务错误")

        w.execute = failing_execute
        w._process_task({"task_id": "t-plain", "instruction": "查一下"})

        self.assertEqual(len(published), 1)
        tid, err = published[0]
        self.assertEqual(tid, "t-plain")
        self.assertEqual(err, "普通业务错误", "未归类错误保持原有失败回传")


if __name__ == "__main__":
    unittest.main()
