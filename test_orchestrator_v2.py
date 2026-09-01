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
import shutil
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import orchestrator_v2
from orchestrator_v2 import OrchestratorV2
import workspace as ws_mod


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

    def inject_context(self, goal):
        return ""


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
    o._max_reflection_steps = 3
    o._reflection_accept_score = 6.0
    o._max_redo_rounds = 2
    o._max_redo_steps = 2
    o._plan_confirm_timeout = 300
    o._stall_timeout = 60
    o._critic_enabled = False
    o._messaging = FakeMessaging()
    o._memory = FakeMemory()
    o._memory_lock = threading.Lock()
    o._redis = FakeRedis()
    o._planner_llm = None
    o._task_starts = {"test-other-task": 0.0}  # 模拟并发，避免单测触发真实工作区清理
    o._task_simple = {}
    o._task_sources = {}
    o._task_sources_lock = threading.Lock()
    o._task_starts_lock = threading.Lock()
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

    def test_low_risk_human_in_loop_forced_to_pipeline(self):
        """P1-2：package/file_io/data_loader 等低风险步骤被规划器标
        human_in_loop 时强制改为 pipeline；web_fetch 等允许保留。"""
        steps = self.o._normalize_steps([
            {"step_id": "1", "capability": "package",
             "instruction": "打包\n验收：x", "mode": "human_in_loop"},
            {"step_id": "2", "capability": "file_io",
             "instruction": "删除\n验收：x", "mode": "human_in_loop"},
            {"step_id": "3", "capability": "data_loader",
             "instruction": "加载\n验收：x", "mode": "human_in_loop"},
            {"step_id": "4", "capability": "web_fetch",
             "instruction": "抓取\n验收：x", "mode": "human_in_loop"},
            {"step_id": "5", "capability": "report_generator",
             "instruction": "报告\n验收：x", "mode": "human_in_loop"},
        ])
        by_cap = {s["capability"]: s["mode"] for s in steps}
        self.assertEqual(by_cap["package"], "pipeline")
        self.assertEqual(by_cap["file_io"], "pipeline")
        self.assertEqual(by_cap["data_loader"], "pipeline")
        self.assertEqual(by_cap["web_fetch"], "human_in_loop")
        self.assertEqual(by_cap["report_generator"], "human_in_loop")


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


class TestWireReportDeps(unittest.TestCase):
    def setUp(self):
        self.o = make_orch()

    def test_report_step_without_deps_wired_to_all(self):
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "a"},
            {"step_id": "2", "capability": "report_generator", "instruction": "b"},
        ]
        out = self.o._wire_report_deps(steps)
        self.assertEqual(out[1]["depends_on"], ["1"])

    def test_summary_without_deps_wired(self):
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "a"},
            {"step_id": "2", "capability": "content_summary", "instruction": "b"},
        ]
        out = self.o._wire_report_deps(steps)
        self.assertEqual(out[1]["depends_on"], ["1"])

    def test_step_with_existing_deps_untouched(self):
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "a"},
            {"step_id": "2", "capability": "report_generator", "instruction": "b", "depends_on": ["1"]},
        ]
        out = self.o._wire_report_deps(steps)
        self.assertEqual(out[1]["depends_on"], ["1"])

    def test_research_chain_no_cycle_after_break(self):
        # 搜索→摘要→报告→打包：接线后不得成环（成环会被 break_cycles 清空依赖，
        # 导致 package 提前执行、无文件可打包而失败）
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "s"},
            {"step_id": "2", "capability": "content_summary", "instruction": "c"},
            {"step_id": "3", "capability": "report_generator", "instruction": "r"},
        ]
        out = self.o._wire_report_deps(steps)
        out = self.o._ensure_package_step(out)
        out = self.o._break_cycles(out)
        by_id = {s["step_id"]: s for s in out}
        self.assertEqual(by_id["2"]["depends_on"], ["1"], "摘要依赖搜索")
        self.assertEqual(set(by_id["3"]["depends_on"]), {"1", "2"}, "报告依赖搜索+摘要")
        self.assertEqual(by_id["package-4"]["depends_on"], ["1", "2", "3"], "打包依赖所有步骤")
        # 依赖不得被 break_cycles 清空（无环）
        for s in out:
            if s.get("capability") != "web_search":
                self.assertTrue(s.get("depends_on"), f"{s['step_id']} 依赖不应被清空")


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
        best = self.o._best_deliverable("", steps, results)
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
            best = o._best_deliverable("", steps, results)
            self.assertIn("# 文件报告", best)
        finally:
            os.unlink(path)

    def test_json_output_excluded_returns_empty(self):
        steps = [{"capability": "code_execution", "instruction": "x"}]
        results = [{"status": "SUCCESS", "result": '{"status": "success", "shape": [1, 2]}'}]
        self.assertEqual(self.o._best_deliverable("", steps, results), "")

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
            best = self.o._best_deliverable("", steps, results)
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
        o._plan = lambda goal, task_id, context="", memory_context="": [
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

        def fake_reflect(goal, report, task_id, all_steps=None, completed_all=None, memory_context="", validator_summary="", eval_scores=""):
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

    def test_reflection_score_gate_accepts_high_score(self):
        o = make_orch()
        o._plan = lambda goal, task_id, context="", memory_context="": [
            {"step_id": "1", "capability": "content_summary", "instruction": "x", "timeout": 120}
        ]
        o._execute_steps = lambda steps, task_id, goal: (
            [{"task_id": s["step_id"], "status": "SUCCESS", "result": "# 报告" + "A" * 300} for s in steps],
            False,
        )
        reflected = {"n": 0}

        def fake_reflect(goal, report, task_id, all_steps=None, completed_all=None, memory_context="", validator_summary="", eval_scores=""):
            reflected["n"] += 1
            return {"accepted": False, "score": 8.0, "gaps": ["可优化"],
                    "next_steps": [{"step_id": "x", "capability": "content_summary",
                                    "instruction": "润色", "timeout": 120}]}

        o._reflect = fake_reflect
        o._now_iso = lambda: "t"
        res = o.run("t-gate-1", "目标", auto_run=True)
        self.assertEqual(reflected["n"], 1, "评分≥6 应只评审一次")
        self.assertEqual(len(res["steps"]), 1, "高评分不应追加步骤")

    def test_acceptance_fail_overrides_reflection_accept(self):
        """P3：确定性验收 fail + 反思评分 accept → 不直接放行，强制产出重做步骤；
        无验收报告时评分 accept → 直接放行（见 test_reflection_score_gate_accepts_high_score）。"""
        import tempfile
        import workspace as ws_mod

        o = make_orch(_max_iterations=3)
        o._plan = lambda goal, task_id, context="", memory_context="": [
            {"step_id": "1", "capability": "report_generator",
             "instruction": "生成报告", "timeout": 120}
        ]
        o._execute_steps = lambda steps, task_id, goal: (
            [{"task_id": s["step_id"], "status": "SUCCESS",
              "result": "# 报告" + "A" * 300} for s in steps],
            False,
        )
        reflected = {"n": 0}

        def fake_reflect(goal, report, task_id, all_steps=None, completed_all=None,
                         memory_context="", validator_summary="", eval_scores=""):
            reflected["n"] += 1
            return {"accepted": True, "score": 9.0, "verdict": "accept",
                    "gaps": [], "next_steps": []}

        o._reflect = fake_reflect
        o._now_iso = lambda: "t"
        tmp = tempfile.mkdtemp(prefix="wm_accfail_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws = ws_mod.task_workspace("t-acc-fail", "default")
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "acceptance_report.json").write_text(json.dumps({
                "overall": "fail",
                "gaps": ["数字溯源率不足：疑似模型知识未标注"],
            }, ensure_ascii=False), encoding="utf-8")
            res = o.run("t-acc-fail", "目标", auto_run=True)
            self.assertGreaterEqual(
                reflected["n"], 2,
                "验收 fail 时反思 accept 评分不得放行，应继续反思/重做",
            )
            self.assertGreaterEqual(
                len(res["steps"]), 2,
                "验收 fail 必须产出重做步骤",
            )
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reflection_steps_capped(self):
        o = make_orch()
        o._plan = lambda goal, task_id, context="", memory_context="": [
            {"step_id": "1", "capability": "content_summary", "instruction": "x", "timeout": 120}
        ]

        def fake_execute(steps, task_id, goal):
            return [{"task_id": s["step_id"], "status": "SUCCESS", "result": "# 报告" + "A" * 300} for s in steps], False

        o._execute_steps = fake_execute
        reflected = {"n": 0}

        def fake_reflect(goal, report, task_id, all_steps=None, completed_all=None, memory_context="", validator_summary="", eval_scores=""):
            reflected["n"] += 1
            if reflected["n"] <= 2:
                return {"accepted": False, "score": 4.0, "gaps": ["缺图"],
                        "next_steps": [
                            {"step_id": f"n{i}", "capability": "content_summary",
                             "instruction": f"补{i}", "timeout": 120}
                            for i in range(5)
                        ]}
            return {"accepted": True, "score": 9.0}

        o._reflect = fake_reflect
        o._now_iso = lambda: "t"
        res = o.run("t-cap-1", "目标", auto_run=True)
        # 每轮最多追加 3 个步骤；2 轮迭代 + 初始 = 1 + 3 + 3 = 7 步
        self.assertEqual(len(res["steps"]), 7, "反思每轮最多追加 3 步")

    def test_reflection_retry_step_redoes_single_step(self):
        o = make_orch()
        o._plan = lambda goal, task_id, context="", memory_context="": [
            {"step_id": "1", "capability": "web_search", "instruction": "搜索", "timeout": 60},
            {"step_id": "2", "capability": "content_summary", "instruction": "总结",
             "depends_on": ["1"], "timeout": 60},
            {"step_id": "3", "capability": "report_generator", "instruction": "报告",
             "depends_on": ["1", "2"], "timeout": 60},
        ]
        dispatch_calls = {"1": 0, "2": 0, "3": 0}

        def fake_dispatch(goal, step, tid, state):
            dispatch_calls[step["step_id"]] = dispatch_calls.get(step["step_id"], 0) + 1
            return {"task_id": step["step_id"], "status": "SUCCESS",
                    "result": f"ok-{step['step_id']}-#{dispatch_calls[step['step_id']]}"}

        o._dispatch_step_safe = fake_dispatch
        o._execute_steps = None  # 单步重做走 _dispatch_step_safe，不走整轮执行
        reflected = {"n": 0}

        def fake_reflect(goal, report, task_id, all_steps, completed_all, memory_context="", validator_summary="", eval_scores=""):
            reflected["n"] += 1
            if reflected["n"] == 1:
                return {"score": 4.0, "verdict": "retry_step",
                        "retry_step_id": "1", "retry_reason": "搜索结果过时，需要最新数据"}
            return {"score": 9.0, "verdict": "accept"}

        o._reflect = fake_reflect
        o._now_iso = lambda: "t"

        # 拦截整轮执行：用自定义实现把初始 3 步直接完成
        def fake_execute(steps, task_id, goal):
            out = []
            for s in steps:
                dispatch_calls[s["step_id"]] = dispatch_calls.get(s["step_id"], 0) + 1
                out.append({"task_id": s["step_id"], "status": "SUCCESS",
                            "result": f"ok-{s['step_id']}-#{dispatch_calls[s['step_id']]}"})
            return out, False

        o._execute_steps = fake_execute
        res = o.run("t-redo-1", "目标", auto_run=True)
        self.assertEqual(reflected["n"], 2, "重做后应再次反思")
        self.assertGreaterEqual(dispatch_calls["1"], 2, "步骤1应被重做（至少2次派发）")
        # 单步重做不应整轮重跑：步骤2/3在初始轮各执行1次，重做后因依赖1被重做 → 也会重跑
        self.assertEqual(res["status"], "SUCCESS")

    def test_inject_memory_context_logs(self):
        o = make_orch()
        o._now_iso = lambda: "t"

        class _Mem:
            def inject_context(self, goal):
                return "历史经验" * 40

            def consolidate_memory(self, *a, **k):
                pass

        o._memory = _Mem()
        ctx = o._inject_memory_context("目标", "t-mem")
        self.assertIn("历史经验", ctx)
        msgs = [m.get("payload", {}).get("message", "") for _, m in o._messaging.published]
        self.assertTrue(any("注入历史经验" in s for s in msgs))

        # 无经验时也推送提示日志
        o._messaging = FakeMessaging()
        o._memory = FakeMemory()
        o._inject_memory_context("目标", "t-mem2")
        msgs2 = [m.get("payload", {}).get("message", "") for _, m in o._messaging.published]
        self.assertTrue(any("未找到相关历史经验" in s for s in msgs2))


class TestFinalReportConfirm(unittest.TestCase):
    """V1.2 关键节点 HITL：报告终稿审批（report_confirm）。"""

    def _orch(self, **overrides):
        o = make_orch(**overrides)
        o._plan = lambda goal, task_id, context="", memory_context="": [
            {"step_id": "1", "capability": "content_summary",
             "instruction": "x", "timeout": 120}
        ]
        o._execute_steps = lambda steps, task_id, goal: (
            [{"task_id": s["step_id"], "status": "SUCCESS",
              "result": "# 报告终稿" + "A" * 5000} for s in steps],
            False,
        )
        o._reflect = lambda *a, **k: {
            "accepted": True, "score": 9.0, "verdict": "accept",
            "gaps": [], "next_steps": [],
        }
        # 跳过结构化预载的真实重试（会 sleep 2s），测试保持轻量
        o._structured_data_preload = lambda *a, **k: None
        o._now_iso = lambda: "t"
        return o

    @staticmethod
    def _awaiting_messages(o):
        return [
            m for _, m in o._messaging.published
            if isinstance(m, dict) and m.get("status") == "AWAITING_CONFIRM"
        ]

    def test_default_report_confirm_skips_approval(self):
        """关键回归：report_confirm=False（默认）不发布 AWAITING_CONFIRM、
        不等待确认，行为与现在完全一致。"""
        o = self._orch()
        o._wait_report_confirm = mock.MagicMock(return_value=True)
        res = o.run("t-rc-default", "目标", auto_run=True)
        self.assertEqual(res["status"], "SUCCESS")
        o._wait_report_confirm.assert_not_called()
        self.assertEqual(self._awaiting_messages(o), [])

    def test_user_confirm_releases_task(self):
        """report_confirm=True + 用户确认 → SUCCESS，审批等待被满足，
        AWAITING_CONFIRM 消息携带 final_report 阶段与 3000 字预览。"""
        o = self._orch()
        keys = []

        def fake_brpop(r, key, deadline):
            keys.append(key)
            return (key, json.dumps({"action": "confirm"}))

        o._brpop_with_deadline = fake_brpop
        res = o.run("t-rc-ok", "目标", auto_run=True, report_confirm=True)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertIn("plan_confirm:t-rc-ok", keys)
        awaits = self._awaiting_messages(o)
        self.assertEqual(len(awaits), 1, "终稿审批只发布一次 AWAITING_CONFIRM")
        self.assertEqual(awaits[0]["revision"], False)
        self.assertEqual(awaits[0]["stage"], "final_report")
        self.assertEqual(len(awaits[0]["report_preview"]), 3000)
        # 确认后最终报告不带取消注记
        self.assertNotIn("用户取消终稿审批", res["final_report"])

    def test_user_cancel_marks_failed_with_note(self):
        """report_confirm=True + 用户取消 → FAILED，报告中注明用户取消终稿审批。"""
        o = self._orch()
        o._brpop_with_deadline = lambda r, key, deadline: (
            key, json.dumps({"action": "cancel"})
        )
        res = o.run("t-rc-cancel", "目标", auto_run=True, report_confirm=True)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("用户取消终稿审批", res["final_report"])
        completes = [
            m.get("payload", {}).get("status")
            for _, m in o._messaging.published
            if isinstance(m, dict) and m.get("type") == "task_complete"
        ]
        self.assertEqual(completes[-1], "FAILED", "任务完成消息应如实标记失败")

    def test_timeout_auto_releases_task(self):
        """report_confirm=True + 超时 → 自动放行 SUCCESS，不卡死。"""
        o = self._orch()
        o._brpop_with_deadline = lambda r, key, deadline: None
        res = o.run("t-rc-timeout", "目标", auto_run=True, report_confirm=True)
        self.assertEqual(res["status"], "SUCCESS")
        msgs = [
            m.get("payload", {}).get("message", "")
            for _, m in o._messaging.published
            if isinstance(m, dict) and m.get("type") == "log"
        ]
        self.assertTrue(any("超时" in s for s in msgs), "超时应有自动放行日志")

    def test_approval_triggered_once_after_final_round(self):
        """反思多轮只触发一次审批：放在反思循环退出、最终报告确定后。"""
        o = make_orch()
        o._plan = lambda goal, task_id, context="", memory_context="": [
            {"step_id": "1", "capability": "content_summary",
             "instruction": "x", "timeout": 120}
        ]
        rounds = [
            [{"task_id": "1", "status": "SUCCESS",
              "result": "# 第一轮报告" + "A" * 300}],
            [{"task_id": "i1-1", "status": "SUCCESS",
              "result": "# 第二轮报告" + "B" * 300}],
        ]
        calls = {"n": 0}

        def fake_execute(steps, task_id, goal):
            i = calls["n"]
            calls["n"] += 1
            return (rounds[i] if i < len(rounds) else []), False

        o._execute_steps = fake_execute

        def fake_reflect(goal, report, task_id, all_steps=None,
                         completed_all=None, memory_context="",
                         validator_summary="", eval_scores=""):
            if calls["n"] <= 1:
                return {"accepted": False, "gaps": ["g"],
                        "next_steps": [
                            {"step_id": "x", "capability": "content_summary",
                             "instruction": "补充", "timeout": 120}
                        ]}
            return {"accepted": True, "score": 9.0, "verdict": "accept"}

        o._reflect = fake_reflect
        o._structured_data_preload = lambda *a, **k: None
        o._now_iso = lambda: "t"
        wait = mock.MagicMock(return_value=True)
        o._wait_report_confirm = wait
        res = o.run("t-rc-once", "目标", auto_run=True, report_confirm=True)
        self.assertEqual(wait.call_count, 1, "多轮反思也只应审批一次")
        self.assertEqual(len(self._awaiting_messages(o)), 0,
                         "审批方法被 mock 时不发布状态消息")
        self.assertEqual(res["status"], "SUCCESS")


class TestMemoryAcceptanceWiring(unittest.TestCase):
    """P0 沉淀准入：验收 fail 时 consolidate_memory 收到验收摘要并提示跳过策略。"""

    def test_acceptance_fail_passes_summary_and_skips_message(self):
        import tempfile
        import workspace as ws_mod

        o = make_orch()
        calls = []

        class RecorderMemory:
            def inject_context(self, goal):
                return ""

            def consolidate_memory(self, *args, **kwargs):
                calls.append((args, kwargs))

        o._memory = RecorderMemory()
        o._plan = lambda goal, task_id, context="", memory_context="": [
            {"step_id": "1", "capability": "content_summary",
             "instruction": "x", "timeout": 120}
        ]
        o._execute_steps = lambda steps, task_id, goal: (
            [{"task_id": s["step_id"], "status": "SUCCESS",
              "result": "# 报告" + "A" * 300} for s in steps],
            False,
        )
        o._reflect = lambda *a, **k: {"accepted": True}
        o._now_iso = lambda: "t"

        tmp = tempfile.mkdtemp(prefix="wm_acc_mem_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws = ws_mod.task_workspace("t-mem-gate", "default")
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "acceptance_report.json").write_text(json.dumps({
                "overall": "fail",
                "gaps": ["缺来源"],
            }, ensure_ascii=False), encoding="utf-8")
            res = o.run("t-mem-gate", "目标", auto_run=True)
            self.assertEqual(res["status"], "SUCCESS_WITH_ISSUES")
            self.assertTrue(calls, "验收 fail 仍应调用 consolidate_memory（对话沉淀）")
            _, kwargs = calls[0]
            self.assertEqual(
                kwargs.get("acceptance_summary", {}).get("overall"), "fail",
            )
            msgs = [m.get("payload", {}).get("message", "")
                    for _, m in o._messaging.published]
            self.assertTrue(any("验收未通过" in s for s in msgs), msgs)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)


class TestPlanTopicGuard(unittest.TestCase):
    def setUp(self):
        self.o = make_orch()

    def test_on_topic_plan_ok(self):
        steps = [{"capability": "code_execution", "instruction": "用 pygame 实现愤怒的小鸟游戏"}]
        self.assertTrue(self.o._plan_topic_ok("写一个愤怒的小鸟", steps))

    def test_off_topic_plan_detected(self):
        steps = [{"capability": "web_search", "instruction": "搜索 2026 AI 行业三大趋势"}]
        self.assertFalse(self.o._plan_topic_ok("写一个愤怒的小鸟", steps))

    def test_generic_word_bypass_blocked(self):
        # 漂移计划含"文件/html"等通用词，不得被误判为对题
        goal = "做一个极简的贪吃蛇游戏（单文件HTML，含画布、键盘控制和得分）"
        todo_steps = [{"capability": "code_execution", "instruction": "生成一个交互式待办事项列表应用 HTML 文件（todo.html）"}]
        self.assertFalse(self.o._plan_topic_ok(goal, todo_steps))
        snake_steps = [{"capability": "code_execution", "instruction": "生成贪吃蛇游戏，含画布和键盘控制"}]
        self.assertTrue(self.o._plan_topic_ok(goal, snake_steps))

    def test_parse_plan_response_fences_and_loose(self):
        out = self.o._parse_plan_response('```json\n{"steps": [{"a": "b"}]}\n```')
        self.assertEqual(out["steps"][0]["a"], "b")


class TestPruneSuperseded(unittest.TestCase):
    """迭代补洞残留清理：同基础名失败交付物有已通过兄弟文件时移除失败版。"""

    def _make_workspace(self):
        base = tempfile.mkdtemp(prefix="zhiguang_prune_")
        project = os.path.join(base, "t1", "project")
        os.makedirs(project)
        bad = os.path.join(project, "index.html")
        good = os.path.join(project, "index_1786354743.html")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("<html><body>blank canvas</body></html>")
        with open(good, "w", encoding="utf-8") as f:
            f.write("<html><canvas></canvas><script>ok()</script></html>")
        return base, project, bad, good

    def _zip_with(self, base, entries):
        zip_path = os.path.join(base, "delivery.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for name, content in entries:
                if isinstance(content, bytes):
                    zf.writestr(name, content)
                else:
                    zf.writestr(name, content)
        return zip_path

    def _pkg(self, zip_path):
        steps = [{"step_id": "pkg", "capability": "package", "instruction": "pack"}]
        completed = {"pkg": {"task_id": "pkg", "status": "SUCCESS",
                             "result": f"Download: file://{zip_path}"}}
        return steps, completed

    def test_prunes_failed_when_superseded_sibling_passes(self):
        base, project, bad, good = self._make_workspace()
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(base)
        try:
            with open(bad, encoding="utf-8") as f:
                bad_content = f.read()
            with open(good, encoding="utf-8") as f:
                good_content = f.read()
            zip_path = self._zip_with(base, [
                ("index.html", bad_content),
                ("index_1786354743.html", good_content),
                ("reports/report.md", "# report"),
            ])
            o = make_orch()
            o._now_iso = lambda: "t"
            steps, completed = self._pkg(zip_path)
            e2e = [
                {"name": "index.html", "type": "html", "ok": False, "detail": "canvas 空白"},
                {"name": "index_1786354743.html", "type": "html", "ok": True, "detail": "ok"},
            ]
            changed = o._prune_superseded_files("t1", steps, completed, e2e)
            self.assertTrue(changed)
            self.assertFalse(os.path.exists(bad), "失败旧文件应从磁盘删除")
            self.assertTrue(os.path.exists(good), "通过的新文件应保留")
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
            self.assertNotIn("index.html", names, "失败旧文件应从交付 zip 剔除")
            self.assertIn("index_1786354743.html", names)
            self.assertIn("reports/report.md", names)
            pushed = [m for _, m in o._messaging.published
                      if m.get("payload", {}).get("message", "").startswith("清理")]
            self.assertTrue(pushed, "应推送清理进度到前端")
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(base, ignore_errors=True)

    def test_keeps_failed_when_no_passing_sibling(self):
        base, project, bad, _good = self._make_workspace()
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(base)
        try:
            with open(bad, encoding="utf-8") as f:
                bad_content = f.read()
            zip_path = self._zip_with(base, [("index.html", bad_content)])
            o = make_orch()
            o._now_iso = lambda: "t"
            steps, completed = self._pkg(zip_path)
            e2e = [{"name": "index.html", "type": "html", "ok": False, "detail": "canvas 空白"}]
            self.assertFalse(o._prune_superseded_files("t1", steps, completed, e2e))
            self.assertTrue(os.path.exists(bad), "没有通过兄弟文件时不得删除")
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(base, ignore_errors=True)

    def test_keeps_failed_with_distinct_stem(self):
        base, project, bad, good = self._make_workspace()
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(base)
        try:
            about = os.path.join(project, "about.html")
            with open(about, "w", encoding="utf-8") as f:
                f.write("<html>about</html>")
            with open(bad, encoding="utf-8") as f:
                bad_content = f.read()
            with open(good, encoding="utf-8") as f:
                good_content = f.read()
            with open(about, encoding="utf-8") as f:
                about_content = f.read()
            zip_path = self._zip_with(base, [
                ("index.html", bad_content),
                ("index_1786354743.html", good_content),
                ("about.html", about_content),
            ])
            o = make_orch()
            o._now_iso = lambda: "t"
            steps, completed = self._pkg(zip_path)
            # about.html 通过、index 系列全失败：基础名不同，不得误删
            e2e = [
                {"name": "index.html", "type": "html", "ok": False, "detail": "空白"},
                {"name": "index_1786354743.html", "type": "html", "ok": False, "detail": "空白"},
                {"name": "about.html", "type": "html", "ok": True, "detail": "ok"},
            ]
            self.assertFalse(o._prune_superseded_files("t1", steps, completed, e2e))
            self.assertTrue(os.path.exists(bad))
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(base, ignore_errors=True)


class TestRedoStepLimit(unittest.TestCase):
    """B3：单轮反思重做最多 N 步（默认 2，REFLECT_MAX_REDO_STEPS 可配）。"""

    def _make(self, max_redo_steps=2):
        o = make_orch(_max_redo_steps=max_redo_steps)
        o._now_iso = lambda: "t"
        o._diagnosis_for_step = lambda *a, **k: None
        o._record_reflection_refinement = lambda *a, **k: None
        return o

    def test_redo_caps_dependents_to_n_steps(self):
        o = self._make(max_redo_steps=2)
        all_steps = [
            {"step_id": "1", "capability": "web_search",
             "instruction": "s1", "depends_on": []},
            {"step_id": "2", "capability": "content_summary",
             "instruction": "s2", "depends_on": ["1"]},
            {"step_id": "3", "capability": "report_generator",
             "instruction": "s3", "depends_on": ["1", "2"]},
        ]
        completed_all = {
            s["step_id"]: {"status": "SUCCESS", "result": f"old-{s['step_id']}"}
            for s in all_steps
        }
        dispatched = []

        def fake_dispatch(goal, step, tid, state):
            dispatched.append(step["step_id"])
            return {"task_id": step["step_id"], "status": "SUCCESS",
                    "result": f"new-{step['step_id']}"}

        o._dispatch_step_safe = fake_dispatch
        ok = o._redo_step_and_dependents(
            "t-redo-cap", "目标", all_steps, completed_all, "1", "修复",
        )
        self.assertTrue(ok)
        self.assertEqual(dispatched, ["1", "2"],
                         "单轮重做最多 2 步：目标步骤 + 最近的 1 个下游")
        self.assertEqual(completed_all["1"]["result"], "new-1")
        self.assertEqual(completed_all["2"]["result"], "new-2")
        self.assertEqual(completed_all["3"]["result"], "old-3",
                         "超出上限的下游步骤不应被重做")

    def test_max_redo_steps_one_caps_to_single_step(self):
        o = self._make(max_redo_steps=1)
        all_steps = [
            {"step_id": "1", "capability": "web_search",
             "instruction": "s1", "depends_on": []},
            {"step_id": "2", "capability": "content_summary",
             "instruction": "s2", "depends_on": ["1"]},
        ]
        completed_all = {
            s["step_id"]: {"status": "SUCCESS", "result": f"old-{s['step_id']}"}
            for s in all_steps
        }
        dispatched = []

        def fake_dispatch(goal, step, tid, state):
            dispatched.append(step["step_id"])
            return {"task_id": step["step_id"], "status": "SUCCESS",
                    "result": f"new-{step['step_id']}"}

        o._dispatch_step_safe = fake_dispatch
        o._redo_step_and_dependents(
            "t-redo-cap1", "目标", all_steps, completed_all, "1", "修复",
        )
        self.assertEqual(dispatched, ["1"])
        self.assertEqual(completed_all["2"]["result"], "old-2")


class TestReflectionFailureStopsIteration(unittest.TestCase):
    """B3：反思 LLM 调用失败（空内容/超时）不再重试整轮，停止迭代并记录日志。"""

    def test_reflect_llm_failure_returns_none_and_logs(self):
        o = make_orch()

        class FailingLLM:
            def call(self, *args, **kwargs):
                raise RuntimeError("simulated empty/timeout")

        o._planner_llm = FailingLLM()
        with self.assertLogs("orchestrator_v2", level="WARNING") as cm:
            res = o._reflect("目标", "报告", "t-fail", [], {})
        self.assertIsNone(res, "反思失败应返回 None，外层循环据此停止迭代")
        self.assertTrue(
            any("反思 LLM 失败，停止迭代" in line for line in cm.output),
            f"应有停止迭代日志: {cm.output}",
        )

    def test_run_stops_after_one_reflection_failure(self):
        o = make_orch()
        o._plan = lambda goal, task_id, context="", memory_context="": [
            {"step_id": "1", "capability": "content_summary",
             "instruction": "x", "timeout": 120}
        ]
        o._execute_steps = lambda steps, task_id, goal: (
            [{"task_id": s["step_id"], "status": "SUCCESS",
              "result": "# 报告" + "A" * 300} for s in steps],
            False,
        )
        reflected = {"n": 0}

        def fake_reflect(goal, report, task_id, all_steps=None,
                         completed_all=None, memory_context="",
                         validator_summary="", eval_scores=""):
            reflected["n"] += 1
            return None  # 模拟反思 LLM 失败后的行为

        o._reflect = fake_reflect
        o._now_iso = lambda: "t"
        res = o.run("t-stop-fail", "目标", auto_run=True)
        self.assertEqual(reflected["n"], 1, "反思失败后不应再进入下一轮")
        self.assertEqual(len(res["steps"]), 1)


class TestSystemConfigHotReload(unittest.TestCase):
    """C3 system 段热重载回归：mtime 变化才重新赋值，未变化不重复加载。"""

    def _make(self, cfg_path):
        o = OrchestratorV2.__new__(OrchestratorV2)
        o._system_cfg_path = str(cfg_path)
        o._system_cfg_mtime = None
        o._max_retry = 2
        o._replan_depth = 2
        o._critic_enabled = False
        o._critic_timeout = 30
        o._max_steps = 8
        o._max_parallel = 3
        o._max_iterations = 2
        o._task_timeout = 300
        o._stall_timeout = 60
        o._plan_confirm_timeout = 300
        return o

    @staticmethod
    def _write(path, **system):
        path.write_text(json.dumps({"system": system}), encoding="utf-8")

    def test_reload_applies_system_section(self):
        """system 段 mtime 变化：全部旋钮逐个热更新，并打一条变化日志。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            # 首次写入与实例默认一致的值：只缓存 mtime，不产生变化
            self._write(p, max_steps=8, max_parallel=3, max_iterations=2,
                        critic=False, critic_timeout=30, max_retry=2,
                        replan_depth=2, task_timeout=300, stall_timeout=60,
                        plan_confirm_timeout=300)
            o = self._make(p)
            mt = {"v": 100.0}
            with mock.patch("orchestrator_v2.os.path.getmtime",
                            side_effect=lambda *a, **k: mt["v"]), \
                    mock.patch.object(orchestrator_v2.logger, "info") as info:
                o._reload_system_config()  # 首次：缓存 mtime，无变化
                self._write(p, max_steps=5, max_parallel=2, max_iterations=1,
                            critic=True, critic_timeout=45, max_retry=1,
                            replan_depth=3, task_timeout=120, stall_timeout=90,
                            plan_confirm_timeout=180)
                mt["v"] = 200.0
                o._reload_system_config()  # mtime 变化：热重载并打日志
            self.assertEqual(o._max_steps, 5)
            self.assertEqual(o._max_parallel, 2)
            self.assertEqual(o._max_iterations, 1)
            self.assertTrue(o._critic_enabled)
            self.assertEqual(o._critic_timeout, 45)
            self.assertEqual(o._max_retry, 1)
            self.assertEqual(o._replan_depth, 3)
            self.assertEqual(o._task_timeout, 120)
            self.assertEqual(o._stall_timeout, 90)
            self.assertEqual(o._plan_confirm_timeout, 180)
            hot = [c for c in info.call_args_list
                   if c.args and "system 配置热重载" in c.args[0]]
            self.assertEqual(len(hot), 1, "仅 mtime 变化时应打一次热重载日志")
            self.assertIn("max_steps", str(hot[0]))
            self.assertIn("task_timeout", str(hot[0]))

    def test_mtime_unchanged_skips_reassignment(self):
        """mtime 未变：即使文件内容已改也不重复赋值、不打热重载日志。"""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            self._write(p, max_steps=5, max_parallel=2)
            o = self._make(p)
            mt = {"v": 100.0}
            with mock.patch("orchestrator_v2.os.path.getmtime",
                            side_effect=lambda *a, **k: mt["v"]), \
                    mock.patch.object(orchestrator_v2.logger, "info") as info:
                o._reload_system_config()
                self.assertEqual(o._max_steps, 5)
                # 内容变化但 mtime 相同（粗粒度文件系统场景）→ 应跳过
                self._write(p, max_steps=12, max_parallel=2)
                o._reload_system_config()
            self.assertEqual(o._max_steps, 5, "mtime 未变不应重新赋值")
            self.assertFalse(
                any(c.args and "system 配置热重载" in c.args[0]
                    for c in info.call_args_list),
                "mtime 未变时不应触发热重载日志",
            )

    def test_missing_config_is_safe(self):
        """config.json 不存在：热重载静默跳过，实例属性保持默认。"""
        with tempfile.TemporaryDirectory() as d:
            o = self._make(Path(d) / "no-config.json")
            o._reload_system_config()
            self.assertEqual(o._max_steps, 8)
            self.assertEqual(o._task_timeout, 300)

    def test_plan_entry_triggers_reload(self):
        """计划生成入口（_plan）会调用一次热重载。"""
        o = make_orch()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            self._write(p, max_steps=4, max_parallel=2)
            o._system_cfg_path = str(p)
            o._system_cfg_mtime = None
            with mock.patch("orchestrator_v2.os.path.getmtime", return_value=100.0), \
                    mock.patch.object(
                        o, "_reload_system_config",
                        wraps=o._reload_system_config) as reload_spy:
                steps = o._plan("做一个贪吃蛇游戏", "t-hot-plan", "", "")
        reload_spy.assert_called_once()
        self.assertEqual(o._max_steps, 4, "热重载应在计划生成前生效")
        self.assertTrue(steps)


class TestDispatchContractRetry(unittest.TestCase):
    """Bug1+Bug3：契约失败语义接入编排器重试；搜索无关结果触发换词重试。"""

    def _make_o(self):
        o = make_orch(_max_retry=1, _replan_depth=1)
        o._now_iso = lambda: "t"
        return o

    def test_status_success_no_retry(self):
        o = self._make_o()
        calls = {"n": 0}

        def dispatch(step, task_id):
            calls["n"] += 1
            return {
                "task_id": step["step_id"], "status": "SUCCESS",
                "result": json.dumps({"status": "success", "charts": ["a.png"]}),
            }

        o._dispatch = dispatch
        step = {"step_id": "1", "capability": "data_analyzer",
                "instruction": "分析", "timeout": 60}
        res = o._dispatch_step_safe(
            "目标", step, "t-ok-1", {"replan_used": 0},
        )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(calls["n"], 1)

    def test_status_failed_triggers_retry_and_succeeds(self):
        import orchestrator_v2 as ov2

        o = self._make_o()
        calls = {"n": 0}

        def dispatch(step, task_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "task_id": step["step_id"], "status": "SUCCESS",
                    "result": json.dumps(
                        {"status": "failed", "error": "no data"},
                    ),
                }
            return {
                "task_id": step["step_id"], "status": "SUCCESS",
                "result": json.dumps(
                    {"status": "success", "charts": ["a.png"]},
                ),
            }

        o._dispatch = dispatch
        step = {"step_id": "1", "capability": "data_analyzer",
                "instruction": "分析", "timeout": 60}
        with mock.patch.object(ov2.time, "sleep"):
            res = o._dispatch_step_safe(
                "目标", step, "t-retry-1", {"replan_used": 0},
            )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(calls["n"], 2, "status=failed 必须触发契约重试")

    def test_status_failed_after_retries_not_treated_as_success(self):
        import orchestrator_v2 as ov2

        o = self._make_o()

        def dispatch(step, task_id):
            return {
                "task_id": step["step_id"], "status": "SUCCESS",
                "result": json.dumps(
                    {"status": "failed", "error": "still bad"},
                ),
            }

        o._dispatch = dispatch
        o._replan_step = lambda goal, step, error, task_id: None
        step = {"step_id": "1", "capability": "data_analyzer",
                "instruction": "分析", "timeout": 60}
        with mock.patch.object(ov2.time, "sleep"):
            res = o._dispatch_step_safe(
                "目标", step, "t-bad-1", {"replan_used": 0},
            )
        self.assertEqual(res["status"], "FAILED")
        self.assertTrue(res.get("contract_violation"))

    def test_search_irrelevant_triggers_retry_with_market_query(self):
        import orchestrator_v2 as ov2

        o = self._make_o()
        calls = {"n": 0, "instructions": []}
        irrelevant = [
            {"title": "《演唱会》- YouTube",
             "url": "https://youtube.com/watch?v=1", "snippet": "视频"},
            {"title": "moomoo 开户",
             "url": "https://moomoo.com/a", "snippet": "美股"},
            {"title": "百度百科_白酒",
             "url": "https://baike.baidu.com/item/x", "snippet": "词条"},
        ]
        relevant = [
            {"title": "东方财富：今日A股成交额排名前十",
             "url": "https://eastmoney.com/a", "snippet": "A股 成交 排行"},
        ]

        def dispatch(step, task_id):
            calls["n"] += 1
            calls["instructions"].append(step.get("instruction", ""))
            return {
                "task_id": step["step_id"], "status": "SUCCESS",
                "result": json.dumps(
                    irrelevant if calls["n"] == 1 else relevant,
                    ensure_ascii=False,
                ),
            }

        o._dispatch = dispatch
        step = {"step_id": "1", "capability": "web_search",
                "instruction": "搜索", "timeout": 60}
        with mock.patch.object(ov2.time, "sleep"):
            res = o._dispatch_step_safe(
                "今日A股总成交量前十股", step, "t-search-1",
                {"replan_used": 0},
            )
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(calls["n"], 2, "无关结果必须触发换词重试")
        self.assertIn("搜索重试", calls["instructions"][1])
        self.assertIn("行情目标换词", calls["instructions"][1])
        self.assertIn("东方财富", calls["instructions"][1])


class TestMarketSearchInstruction(unittest.TestCase):
    """Bug3：行情类目标给 web_search 步骤追加财经站点限定。"""

    def test_market_goal_adds_site_restriction_to_search_steps(self):
        o = make_orch()
        steps = [
            {"step_id": "1", "capability": "web_search",
             "instruction": "搜索排行"},
            {"step_id": "2", "capability": "content_summary",
             "instruction": "总结"},
        ]
        out = o._inject_goal_into_steps(steps, "今日A股总成交量前十股")
        self.assertIn("东方财富", out[0]["instruction"])
        self.assertIn("同花顺", out[0]["instruction"])
        self.assertIn("新浪财经", out[0]["instruction"])
        self.assertIn("雪球", out[0]["instruction"])
        self.assertIn("禁止 YouTube", out[0]["instruction"])
        self.assertIn("查询词模板", out[0]["instruction"])
        self.assertNotIn("行情数据源限定", out[1]["instruction"])

    def test_non_market_goal_unchanged(self):
        o = make_orch()
        steps = [{"step_id": "1", "capability": "web_search",
                  "instruction": "搜索"}]
        out = o._inject_goal_into_steps(steps, "调研新能源汽车市场现状")
        self.assertNotIn("行情数据源限定", out[0]["instruction"])

    def test_search_results_irrelevant_detection(self):
        o = make_orch()
        irrelevant = [
            {"title": "《演唱会》- YouTube",
             "url": "https://youtube.com/watch?v=1", "snippet": "视频"},
            {"title": "moomoo 开户",
             "url": "https://moomoo.com/a", "snippet": "美股"},
            {"title": "百度百科_白酒",
             "url": "https://baike.baidu.com/item/x", "snippet": "词条"},
        ]
        relevant = [
            {"title": "东方财富：今日A股成交额排名前十",
             "url": "https://eastmoney.com/a", "snippet": "A股 成交 排行"},
        ]
        self.assertTrue(
            o._search_results_irrelevant("今日A股总成交量前十股", irrelevant),
        )
        self.assertEqual(
            o._search_results_irrelevant("今日A股总成交量前十股", relevant),
            "",
        )


class TestV12ReportFormatAndUrlHealth(unittest.TestCase):
    """V1.2 竞品启示：报告格式强制要求注入 + 来源 URL 存活校验。"""

    def test_report_and_summary_prompts_include_v12_requirements(self):
        """report_generator / content_summary 指令必须注入三级溯源链、
        数据时效与免责声明要求。"""
        o = make_orch()
        o._task_goals = {}
        o._task_user_ids = {}
        for cap in ("report_generator", "content_summary"):
            instr = o._inject_step_context(
                {"step_id": "1", "capability": cap,
                 "instruction": "生成报告", "depends_on": []},
                {}, threading.Lock(), "t-v12",
            )
            self.assertIn("三级溯源链", instr, cap)
            self.assertIn("[n]", instr, cap)
            self.assertIn("## 参考来源", instr, cap)
            self.assertIn("数据时效", instr, cap)
            self.assertIn("免责声明", instr, cap)
            self.assertIn("不构成任何投资建议", instr, cap)

    def _make_acceptance_workspace(self, report_text):
        tmp = tempfile.mkdtemp(prefix="wm_v12_acc_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        rep = ws_mod.task_reports_dir("t-v12-url")
        rep.mkdir(parents=True, exist_ok=True)
        (rep / "report.md").write_text(report_text, encoding="utf-8")
        return tmp, old_root

    def test_dead_source_urls_hint_in_gaps_without_failing(self):
        """dead URL 只在 gaps 提示，不改变 overall（避免网络抖动误伤）。"""
        from adapters import url_health

        report = (
            "## 数据时效\n\n行情数据截至 2026-08-30 15:00 收盘；"
            "腾讯行情接口，日终刷新。\n\n"
            "正文引用[1]。\n\n"
            "## 参考来源\n\n1. [来源](https://dead.example/a)\n\n"
            "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
            "不构成任何投资建议；数据来源于公开渠道，可能存在延迟或误差；"
            "据此操作风险自担。"
        )
        tmp, old_root = self._make_acceptance_workspace(report)
        try:
            o = make_orch()
            o._now_iso = lambda: "t"
            with mock.patch.object(
                url_health, "check_urls",
                return_value={"https://dead.example/a": "dead"},
            ):
                result = o._run_acceptance_check("t-v12-url", "分析腾讯股票行情")
            self.assertIsNotNone(result)
            self.assertEqual(result["overall"], "pass")
            self.assertIn("来源链接失效: 1 条", result["gaps"])
            self.assertTrue(result["checks"]["url_health"]["hint"])
            self.assertEqual(
                result["checks"]["url_health"]["dead_count"], 1,
            )
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_url_health_disabled_by_env(self):
        """URL_HEALTH_CHECK=0 时跳过存活校验。"""
        from adapters import url_health

        report = (
            "## 数据时效\n\n行情数据截至 2026-08-30 15:00 收盘。\n\n"
            "正文引用[1]。\n\n"
            "## 参考来源\n\n1. [来源](https://example.com/a)\n\n"
            "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
            "不构成任何投资建议。"
        )
        tmp, old_root = self._make_acceptance_workspace(report)
        try:
            o = make_orch()
            o._now_iso = lambda: "t"
            with mock.patch.dict(os.environ, {"URL_HEALTH_CHECK": "0"}), \
                    mock.patch.object(url_health, "check_urls") as chk:
                result = o._run_acceptance_check("t-v12-url", "分析腾讯股票行情")
            self.assertIsNotNone(result)
            chk.assert_not_called()
            self.assertNotIn("url_health", result["checks"])
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_alive_urls_no_gap_hint(self):
        """全部 URL 存活 → 无失效提示、overall 不受影响。"""
        from adapters import url_health

        report = (
            "## 数据时效\n\n行情数据截至 2026-08-30 15:00 收盘。\n\n"
            "正文引用[1]。\n\n"
            "## 参考来源\n\n1. [来源](https://alive.example/a)\n\n"
            "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
            "不构成任何投资建议。"
        )
        tmp, old_root = self._make_acceptance_workspace(report)
        try:
            o = make_orch()
            o._now_iso = lambda: "t"
            with mock.patch.object(
                url_health, "check_urls",
                return_value={"https://alive.example/a": "alive"},
            ):
                result = o._run_acceptance_check("t-v12-url", "分析腾讯股票行情")
            self.assertEqual(result["overall"], "pass")
            self.assertNotIn("来源链接失效", result["gaps"])
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)


class TestArtifactWhitelistInjection(unittest.TestCase):
    """P0：报告"抄产物"泄漏修复——产物文件注入白名单。

    仅 .md/.txt/.csv/.json 数据类素材读取正文注入；
    HTML/JS/CSS/PY/图片等源码或二进制只保留"存在 + 路径"提示，禁止原文入指令。
    """

    def _inject_with_artifact(self, fname: str, body: str) -> str:
        tmp = Path(tempfile.mkdtemp(prefix="wm_art_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fpath = tmp / fname
        fpath.write_text(body, encoding="utf-8")
        o = make_orch()
        o._task_goals = {}
        o._task_user_ids = {}
        completed = {
            "s-code": {
                "status": "SUCCESS",
                "result": json.dumps({"path": str(fpath)}, ensure_ascii=False),
            },
        }
        return o._inject_step_context(
            {"step_id": "s-report", "capability": "report_generator",
             "instruction": "生成报告", "depends_on": ["s-code"]},
            completed, threading.Lock(), "t-art",
        )

    def test_html_artifact_skipped_with_path_hint(self):
        """index.html 产物只给跳过提示，不注入 HTML 源码/占位数据。"""
        html = (
            "<html><body><h1>模板占位 2023-11-15</h1>"
            "<script>var secret = 'x';</script></body></html>"
        )
        instr = self._inject_with_artifact("index.html", html)
        self.assertNotIn("<html>", instr)
        self.assertNotIn("<script>", instr)
        self.assertNotIn("2023-11-15", instr)
        self.assertIn(
            "[产物文件 s-code (index.html)]"
            "（源码/二进制，跳过正文注入，仅保留路径）",
            instr,
        )

    def test_markdown_artifact_content_injected(self):
        """.md 数据类素材正常注入正文。"""
        instr = self._inject_with_artifact("notes.md", "# 标题\n\n真实素材正文")
        self.assertIn("[产物文件 s-code (notes.md)]:", instr)
        self.assertIn("# 标题", instr)
        self.assertIn("真实素材正文", instr)
        self.assertNotIn("跳过正文注入", instr)

    def test_csv_artifact_content_injected(self):
        """.csv 数据类素材正常注入正文。"""
        instr = self._inject_with_artifact("data.csv", "rank,name\n1,贵州茅台")
        self.assertIn("[产物文件 s-code (data.csv)]:", instr)
        self.assertIn("rank,name", instr)
        self.assertIn("贵州茅台", instr)
        self.assertNotIn("跳过正文注入", instr)

    def test_missing_artifact_path_silently_skipped(self):
        """路径不存在时静默跳过，无异常、无产物说明。"""
        tmp = Path(tempfile.mkdtemp(prefix="wm_art_miss_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        missing = tmp / "nope.html"
        o = make_orch()
        o._task_goals = {}
        o._task_user_ids = {}
        completed = {
            "s-code": {
                "status": "SUCCESS",
                "result": json.dumps({"path": str(missing)}, ensure_ascii=False),
            },
        }
        instr = o._inject_step_context(
            {"step_id": "s-report", "capability": "report_generator",
             "instruction": "生成报告", "depends_on": ["s-code"]},
            completed, threading.Lock(), "t-art-miss",
        )
        self.assertNotIn("产物文件", instr)


class TestBackfillChartManifest(unittest.TestCase):
    """多实体任务交付缺口回归：make_charts 语义图也必须进入 chart_manifest.json。"""

    def test_semantic_pngs_backfilled_and_idempotent(self):
        """chart_1 保持原条目；语义图新增中文关键词；other.png 跳过；重复调用幂等。"""
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="wm_mf_sem_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-mf-sem")
            proj.mkdir(parents=True, exist_ok=True)
            (proj / "chart_1.png").write_bytes(b"PNG")
            (proj / "entity_frequency.png").write_bytes(b"PNG")
            (proj / "financial_trends.png").write_bytes(b"PNG")
            (proj / "other.png").write_bytes(b"PNG")
            (proj / "chart_manifest.json").write_text(json.dumps({
                "charts": [
                    {"file": "chart_1.png", "keywords": ["规模", "市场"],
                     "section_hint": "市场规模"},
                ],
            }, ensure_ascii=False), encoding="utf-8")
            (proj / "chart_data.json").write_text(json.dumps({"charts": [
                {
                    "title": "2023-2025年全球AI芯片市场规模（亿美元）",
                    "question": "市场规模趋势？",
                    "conclusion": "市场规模持续增长。",
                    "type": "line", "unit": "亿美元",
                    "x_axis_title": "年份", "y_axis_title": "规模（亿美元）",
                    "source": "https://a.com", "section_hint": "市场规模",
                    "data": [
                        {"label": "A", "value": 110, "year": 2023,
                         "source": "https://a.com"},
                        {"label": "B", "value": 726, "year": 2025,
                         "source": "https://a.com"},
                    ],
                },
            ]}, ensure_ascii=False), encoding="utf-8")

            o._backfill_chart_manifest(proj)
            manifest = json.loads(
                (proj / "chart_manifest.json").read_text(encoding="utf-8")
            )
            files = [c["file"] for c in manifest["charts"]]
            self.assertEqual(
                files,
                ["chart_1.png", "entity_frequency.png", "financial_trends.png"],
            )
            by_name = {c["file"]: c for c in manifest["charts"]}
            self.assertEqual(
                by_name["chart_1.png"],
                {"file": "chart_1.png", "keywords": ["规模", "市场"],
                 "section_hint": "市场规模"},
                "已存在条目不得被覆盖或改写",
            )
            self.assertIn(
                "主体提及频率",
                by_name["entity_frequency.png"]["keywords"],
            )
            self.assertEqual(by_name["entity_frequency.png"]["section_hint"], "")
            self.assertIn(
                "财务指标趋势",
                by_name["financial_trends.png"]["keywords"],
            )
            self.assertNotIn("other.png", files, "不认识的 PNG 不得纳入 manifest")

            o._backfill_chart_manifest(proj)
            manifest2 = json.loads(
                (proj / "chart_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [c["file"] for c in manifest2["charts"]],
                files,
                "重复回填不得重复添加条目",
            )
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_generate_search_charts_backfills_manifest_after_sync(self):
        """调用点回归：make_charts 成功后同步 PNG 并回填 manifest。

        时序 bug 复现：chart pipeline 先回填 chart_N，make_charts 语义图
        后生成；若同步后不触发 _backfill_chart_manifest，语义图永远缺失。
        这里 mock subprocess.run 返回成功，验证 _generate_search_charts
        执行后 manifest 已包含语义图条目。
        """
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="wm_mf_sync_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-mf-sync")
            proj.mkdir(parents=True, exist_ok=True)
            (proj / "search_results.json").write_text(
                json.dumps({"results": []}), encoding="utf-8")
            (proj / "clean_chart_data.json").write_text(
                json.dumps({}), encoding="utf-8")
            (proj / "chart_data.json").write_text(
                json.dumps({"charts": []}), encoding="utf-8")
            (proj / "chart_manifest.json").write_text(
                json.dumps({"charts": []}), encoding="utf-8")
            for name in ("entity_frequency.png", "financial_trends.png",
                         "source_distribution.png", "topic_terms.png"):
                (proj / name).write_bytes(b"PNG")

            fake_proc = mock.Mock(
                returncode=0, stdout=b"charts generated", stderr=b"")
            with mock.patch("subprocess.run", return_value=fake_proc):
                o._generate_search_charts("t-mf-sync", "市场规模趋势")

            manifest = json.loads(
                (proj / "chart_manifest.json").read_text(encoding="utf-8"))
            files = [c["file"] for c in manifest["charts"]]
            self.assertEqual(
                sorted(files),
                ["entity_frequency.png", "financial_trends.png",
                 "source_distribution.png", "topic_terms.png"],
                "make_charts 同步后语义图必须回填 chart_manifest.json",
            )
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_render_charts_merge_keeps_semantic_entries(self):
        """render_charts.py 重跑时合并写入：语义图条目不得被覆盖（反思重做场景）。"""
        import re as _re
        import subprocess as _subprocess
        from pathlib import Path as _Path
        import orchestrator_v2 as ov_mod

        tmp = _Path(tempfile.mkdtemp(prefix="wm_mf_merge_"))
        try:
            # 提取 render_charts 模板脚本
            src = _Path("orchestrator_v2.py").read_text(encoding="utf-8")
            start = src.find("script = r'''# -*- coding: utf-8 -*-")
            end = src.find("'''", src.find("if __name__ == \"__main__\":", start))
            script = src[start + len("script = r"):end].strip()
            if script.startswith("'''"):
                script = script[3:]
            if script.endswith("'''"):
                script = script[:-3]
            script = script.replace(
                "__REPO_ROOT__", str(_Path(".").resolve()).replace("\\", "/"))

            (tmp / "chart_manifest.json").write_text(json.dumps({"charts": [
                {"file": "chart_1.png", "keywords": ["营收"], "section_hint": ""},
                {"file": "financial_trends.png", "keywords": ["财务指标趋势", "financial"],
                 "section_hint": ""},
            ]}, ensure_ascii=False), encoding="utf-8")
            (tmp / "chart_data.json").write_text(json.dumps({"charts": [
                {"type": "bar", "title": "营收对比",
                 "data": [{"label": "A", "value": 1}, {"label": "B", "value": 2}],
                 "unit": "亿元", "source": "test", "x_axis_title": "年份",
                 "y_axis_title": "亿元", "conclusion": "对比明显"},
            ]}, ensure_ascii=False), encoding="utf-8")
            (tmp / "render_charts.py").write_text(script, encoding="utf-8")
            proc = _subprocess.run(
                [sys.executable, "render_charts.py"], cwd=str(tmp),
                capture_output=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", errors="replace")[:300])
            manifest = json.loads(
                (tmp / "chart_manifest.json").read_text(encoding="utf-8"))
            files = [c["file"] for c in manifest["charts"]]
            self.assertIn("financial_trends.png", files, "语义图被 render_charts 覆盖")
            self.assertTrue(any(f.startswith("chart_") for f in files), "chart_N 应存在")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
