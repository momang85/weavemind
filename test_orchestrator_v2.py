"""织光 - OrchestratorV2 回归单测（fakes 模式，不需要真实 Redis/LLM）。

覆盖试运行中暴露并修复的坑：
- 能力字段多值拼接/非法值回退
- 步骤去重/剔空/限步
- 报告步骤兜底（规划自检）
- best_deliverable：Markdown 优先、report_generator 文件读取、跨轮次
- 并行 DAG：并发、依赖顺序、失败阻断、悬空依赖、死锁看门狗
- 自主迭代循环：多轮步骤累积与最佳交付物
"""

import json
import os
import tempfile
import threading
import time
import unittest

from orchestrator_v2 import OrchestratorV2


class FakeMessaging:
    def __init__(self):
        self.published = []

    def publish(self, channel, message):
        self.published.append((channel, message))

    def close(self):
        pass


class FakeMemory:
    def consolidate_memory(self, *args, **kwargs):
        pass


class FakeRedis:
    def set(self, *args, **kwargs):
        pass


def make_orch(**overrides):
    o = object.__new__(OrchestratorV2)
    o._max_retry = 1
    o._replan_depth = 1
    o._max_steps = 8
    o._max_parallel = 3
    o._max_iterations = 2
    o._plan_confirm_timeout = 300
    o._stall_timeout = 60
    o._critic_enabled = False
    o._messaging = FakeMessaging()
    o._memory = FakeMemory()
    o._memory_lock = threading.Lock()
    o._redis = FakeRedis()
    o._planner_llm = None
    for k, v in overrides.items():
        setattr(o, k, v)
    return o


def make_dispatch(results, delays=None, order=None):
    state = {"active": 0, "max_active": 0}

    def dispatch(goal, step, task_id, holder):
        sid = step.get("step_id")
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        if order is not None:
            order.append(("start", sid))
        if delays:
            time.sleep(delays.get(sid, 0))
        state["active"] -= 1
        if order is not None:
            order.append(("end", sid))
        return dict(results.get(sid, {"task_id": sid, "status": "SUCCESS", "result": f"ok-{sid}"}))

    return dispatch, state


class TestNormalizeSteps(unittest.TestCase):
    def setUp(self):
        self.o = make_orch(_max_steps=8)

    def test_capability_join_falls_back_to_first_valid(self):
        steps = self.o._normalize_steps([
            {"step_id": "1", "capability": "web_search, web_fetch", "instruction": "a"},
            {"step_id": "2", "capability": "content_summary, file_io, report_generator", "instruction": "b"},
        ])
        self.assertEqual(steps[0]["capability"], "web_search")
        self.assertEqual(steps[1]["capability"], "content_summary")

    def test_unknown_capability_falls_back(self):
        steps = self.o._normalize_steps([
            {"step_id": "1", "capability": "magic_power", "instruction": "a"},
        ])
        self.assertEqual(steps[0]["capability"], "content_summary")

    def test_duplicate_ids_renumbered_and_empty_dropped(self):
        steps = self.o._normalize_steps([
            {"step_id": "1", "capability": "web_search", "instruction": "a"},
            {"step_id": "1", "capability": "web_search", "instruction": "b"},
            {"step_id": "3", "capability": "web_search", "instruction": "   "},
        ])
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[1]["step_id"], "1-2")

    def test_max_steps_cap(self):
        steps = self.o._normalize_steps([
            {"step_id": str(i), "capability": "web_search", "instruction": f"x{i}"} for i in range(20)
        ])
        self.assertEqual(len(steps), 8)


class TestEnsureReportStep(unittest.TestCase):
    def setUp(self):
        self.o = make_orch()

    def test_adds_report_step_when_missing(self):
        steps = self.o._ensure_report_step(
            [{"step_id": "1", "capability": "web_search", "instruction": "x"}], "t"
        )
        self.assertEqual(steps[-1]["capability"], "report_generator")

    def test_keeps_when_report_present(self):
        steps = self.o._ensure_report_step(
            [{"step_id": "1", "capability": "content_summary", "instruction": "x"}], "t"
        )
        self.assertEqual(len(steps), 1)


class TestBestDeliverable(unittest.TestCase):
    def setUp(self):
        self.o = make_orch()

    def test_prefers_markdown_over_long_plain_code(self):
        steps = [
            {"capability": "code_execution", "instruction": "x"},
            {"capability": "content_summary", "instruction": "y"},
        ]
        results = [
            {"status": "SUCCESS", "result": "x" * 3000},          # 纯文本很长
            {"status": "SUCCESS", "result": "# 正式报告" + "y" * 500},  # 含 Markdown 标记但更短
        ]
        best = self.o._best_deliverable(steps, results)
        self.assertTrue(best.startswith("# 正式报告"))

    def test_reads_report_file_from_json_string(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("# 文件报告\n\n表格 |\n|---|---|\n")
            path = f.name
        try:
            o = make_orch()
            steps = [{"capability": "report_generator", "instruction": "x"}]
            results = [{"status": "SUCCESS",
                        "result": json.dumps({"status": "success", "report_path": path})}]
            best = o._best_deliverable(steps, results)
            self.assertIn("# 文件报告", best)
        finally:
            os.unlink(path)

    def test_json_output_excluded_returns_empty(self):
        steps = [{"capability": "code_execution", "instruction": "x"}]
        results = [{"status": "SUCCESS", "result": '{"status": "success", "shape": [1, 2]}'}]
        self.assertEqual(self.o._best_deliverable(steps, results), "")

    def test_report_file_preferred_over_longer_summary(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("# 正式演讲稿\n\n各位评委好，以下是我的实训汇报……" + "内容" * 20)
            path = f.name
        try:
            steps = [
                {"capability": "content_summary", "instruction": "x"},
                {"capability": "report_generator", "instruction": "y"},
            ]
            results = [
                {"status": "SUCCESS", "result": "# 检查摘要" + "长" * 400},  # 更长但只是摘要
                {"status": "SUCCESS",
                 "result": json.dumps({"status": "success", "report_path": path})},
            ]
            best = self.o._best_deliverable(steps, results)
            self.assertTrue(best.startswith("# 正式演讲稿"))
        finally:
            os.unlink(path)


class TestExecuteStepsDag(unittest.TestCase):
    def setUp(self):
        self.o = make_orch(_stall_timeout=3)
        self.o._push_realtime_state = lambda *a, **k: None

    def _run(self, steps, results, delays=None, order=None):
        dispatch, state = make_dispatch(results, delays=delays, order=order)
        self.o._dispatch_step_safe = dispatch
        return self.o._execute_steps(steps, "t", "g"), state

    def test_independent_steps_run_in_parallel(self):
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "a"},
            {"step_id": "2", "capability": "web_search", "instruction": "b"},
            {"step_id": "3", "capability": "web_search", "instruction": "c"},
        ]
        delays = {"1": 0.2, "2": 0.2, "3": 0.2}
        start = time.time()
        (results, _), state = self._run(steps, {}, delays)
        wall = time.time() - start
        self.assertTrue(all(r["status"] == "SUCCESS" for r in results))
        self.assertGreaterEqual(state["max_active"], 2, "应至少 2 个步骤并发")
        self.assertLess(wall, 0.55, f"并行应明显快于串行 0.6s，实际 {wall:.2f}s")

    def test_dependency_chain_serializes(self):
        order = []
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "a"},
            {"step_id": "2", "capability": "content_summary", "instruction": "b", "depends_on": ["1"]},
            {"step_id": "3", "capability": "report_generator", "instruction": "c", "depends_on": ["2"]},
        ]
        (results, _), _ = self._run(steps, {}, order=order)
        self.assertTrue(all(r["status"] == "SUCCESS" for r in results))
        starts = [sid for ev, sid in order if ev == "start"]
        self.assertEqual(starts, ["1", "2", "3"])

    def test_failed_dependency_blocks_dependents(self):
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "a"},
            {"step_id": "2", "capability": "content_summary", "instruction": "b", "depends_on": ["1"]},
        ]
        (results, _), _ = self._run(steps, {"1": {"task_id": "1", "status": "FAILED", "result": "boom"}})
        self.assertEqual(results[0]["status"], "FAILED")
        self.assertEqual(results[1]["status"], "FAILED")
        self.assertIn("Blocked by failed dependency", results[1]["result"])

    def test_dangling_dependency_marked_failed(self):
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "a", "depends_on": ["nope"]},
        ]
        (results, _), _ = self._run(steps, {})
        self.assertEqual(results[0]["status"], "FAILED")
        self.assertIn("Dangling dependency", results[0]["result"])

    def test_cycle_stalls_and_fails(self):
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "a", "depends_on": ["2"]},
            {"step_id": "2", "capability": "web_search", "instruction": "b", "depends_on": ["1"]},
        ]
        (results, _), _ = self._run(steps, {})
        self.assertTrue(all(r["status"] == "FAILED" for r in results))
        self.assertIn("Stalled", results[0]["result"])


class TestRunIteration(unittest.TestCase):
    def test_iteration_accumulates_steps_and_uses_best_report(self):
        o = make_orch()
        o._plan = lambda goal, task_id, context="": [
            {"step_id": "1", "capability": "content_summary", "instruction": "x", "timeout": 120}
        ]
        rounds = [
            [{"task_id": "1", "status": "SUCCESS", "result": "# 第一轮报告" + "A" * 300}],
            [{"task_id": "i1-1", "status": "SUCCESS", "result": "# 第二轮补充" + "B" * 300}],
        ]
        calls = {"n": 0}

        def fake_execute(steps, task_id, goal):
            i = calls["n"]
            calls["n"] += 1
            return (rounds[i] if i < len(rounds) else []), False

        o._execute_steps = fake_execute

        def fake_reflect(goal, report, task_id):
            if calls["n"] <= 1:
                return {"accepted": False, "gaps": ["g"],
                        "next_steps": [{"step_id": "x", "capability": "content_summary",
                                        "instruction": "补充", "timeout": 120}]}
            return {"accepted": True}

        o._reflect = fake_reflect
        o._now_iso = lambda: "t"
        res = o.run("t1", "目标", auto_run=True)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(res["steps"]), 2)
        self.assertTrue(res["final_report"].startswith("#"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
