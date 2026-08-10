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
import tempfile
import threading
import time
import unittest
import zipfile

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
    o._task_starts = {"test-other-task": 0.0}  # 模拟并发，避免单测触发真实工作区清理
    o._task_simple = {}
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
