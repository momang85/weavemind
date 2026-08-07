# -*- coding: utf-8 -*-
"""真实交付链回归测试：搜索相关性过滤、file_io 落盘逻辑、code_execution 命名。"""
import unittest


class TestSearchQuality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from worker_base import SearchAgent

        class _Reg:
            def close(self):
                pass

        class _Msg:
            def close(self):
                pass

        sa = SearchAgent.__new__(SearchAgent)
        sa._registry = _Reg()
        sa._messaging = _Msg()
        cls.sa = sa

    QUERY = (
        "搜索GitHub上完整的Python/Pygame愤怒的小鸟开源项目，优先选择项目文件完整、"
        "README标明依赖和运行方式、有许可证且star较高的仓库；同时记录GitHub仓库地址"
    )

    def test_extract_keywords_compact(self):
        kw = self.sa._extract_keywords(self.QUERY)
        self.assertIn("python", kw)
        self.assertIn("pygame", kw)
        self.assertIn("github", kw)
        # 大小写开头不被截断（回归：曾提取成 "ython"/"ygame"）
        words = set(kw.split())
        self.assertNotIn("ython", words)
        self.assertNotIn("ygame", words)
        self.assertIn("python", words)
        self.assertIn("pygame", words)

    def test_filter_drops_garbage(self):
        results = [
            {"title": "《看见缘分的少女 Love Is Written In The Stars》 - YouTube",
             "url": "https://www.youtube.com/watch?v=jiasdf", "snippet": "剧情介绍"},
            {"title": "Google", "url": "https://www.google.com/", "snippet": ""},
        ]
        kept = self.sa._filter_results(self.QUERY, results)
        self.assertEqual(kept, [])

    def test_filter_keeps_relevant_with_word_boundary(self):
        results = [
            {"title": "angry-birds · GitHub Topics", "url": "https://github.com/topics/angry-birds",
             "snippet": "Python pygame angry birds games repositories"},
            {"title": "How to build Angry Birds in Pygame", "url": "https://realpython.com/angry-birds-pygame/",
             "snippet": "tutorial pygame python"},
        ]
        kept = self.sa._filter_results(self.QUERY, results)
        self.assertEqual(len(kept), 2)
        # "star" 不应误中 "Stars"
        self.assertFalse(any("youtube.com" in r["url"] for r in kept))


class TestFileIoWorker(unittest.TestCase):
    def test_loads_json_loose(self):
        from workers.file_io_worker import _loads_json_loose

        fenced = '```json\n{"files": [{"filename": "a.py", "content": "x"}]}\n```'
        self.assertEqual(_loads_json_loose(fenced)["files"][0]["filename"], "a.py")
        wrapped = '说明文字 {"files": []} 结尾说明'
        self.assertEqual(_loads_json_loose(wrapped), {"files": []})
        self.assertEqual(_loads_json_loose("no json"), {})

    def test_safe_path_blocks_escape(self):
        from workers.file_io_worker import FileIoWorker

        w = FileIoWorker.__new__(FileIoWorker)
        with self.assertRaises(ValueError):
            w._safe_path("..\\..\\evil.py")
        with self.assertRaises(ValueError):
            w._safe_path("../../evil.py")

    def test_sanitize_filename_strips_verb_prefix(self):
        from workers.file_io_worker import _sanitize_filename

        self.assertEqual(_sanitize_filename("保存为angry_birds.html"), "angry_birds.html")
        self.assertEqual(_sanitize_filename("保存到assets/图片.png"), "assets/图片.png")
        self.assertEqual(_sanitize_filename("main.py"), "main.py")


class TestCodeExecutionNaming(unittest.TestCase):
    def test_write_marker_uses_target_name(self):
        from workers.code_execution_worker import CodeExecutionWorker

        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        name = w._target_filename("实现愤怒的小鸟核心代码（Python+Pygame），生成 main.py 文件")
        self.assertEqual(name, "main.py")

    def test_html_target_name_supported(self):
        from workers.code_execution_worker import CodeExecutionWorker

        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        name = w._target_filename("生成一个单文件 HTML 游戏 index.html")
        self.assertEqual(name, "index.html")

    def test_html_instruction_defaults_to_index_html(self):
        from workers.code_execution_worker import CodeExecutionWorker

        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        name = w._target_filename("生成一个自包含的单HTML文件游戏")
        self.assertEqual(name, "index.html")

    def test_template_fallback_delivers_html_when_llm_empty(self):
        import asyncio
        import json
        import os
        import tempfile
        from pathlib import Path
        from workers.code_execution_worker import CodeExecutionWorker

        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        w.workspace = Path(tempfile.mkdtemp(prefix="weavemind_test_"))

        async def run():
            async def fail(system="", prompt="", instruction=""):
                return ""

            w._call_llm = fail
            return await w.execute("实现一个愤怒的小鸟游戏，包含弹弓与碰撞")

        res = json.loads(asyncio.run(run()))
        self.assertEqual(res["fallback"], "template")
        self.assertTrue(os.path.exists(res["path"]))
        with open(res["path"], "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("愤怒的小鸟", content)

    def test_test_instruction_gets_unique_name(self):
        from workers.code_execution_worker import CodeExecutionWorker

        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        name = w._target_filename("编写并运行冒烟测试：导入 angry_birds.py 并执行")
        self.assertNotEqual(name, "angry_birds.py")
        self.assertTrue(name.startswith("generated_"))


class TestSearchFetchWiring(unittest.TestCase):
    def test_fetch_without_deps_wired_to_search(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "搜索开源项目"},
            {"step_id": "2", "capability": "web_fetch", "instruction": "抓取代码"},
            {"step_id": "3", "capability": "file_io", "instruction": "保存文件"},
        ]
        out = o._wire_search_fetch_deps(steps)
        self.assertEqual(out[1]["depends_on"], ["1"])

    def test_fetch_with_existing_deps_untouched(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "搜索"},
            {"step_id": "2", "capability": "web_fetch", "instruction": "抓取", "depends_on": ["3"]},
        ]
        out = o._wire_search_fetch_deps(steps)
        self.assertEqual(out[1]["depends_on"], ["3"])


class TestPackageFallback(unittest.TestCase):
    def test_package_step_added_when_file_producers_exist(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        steps = [
            {"step_id": "1", "capability": "code_execution", "instruction": "生成 main.py"},
            {"step_id": "2", "capability": "report_generator", "instruction": "写报告"},
        ]
        out = o._ensure_package_step(steps)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[-1]["capability"], "package")
        self.assertEqual(sorted(out[-1]["depends_on"]), ["1", "2"])

    def test_package_step_not_duplicated(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        steps = [
            {"step_id": "1", "capability": "code_execution", "instruction": "x"},
            {"step_id": "2", "capability": "package", "instruction": "打包"},
        ]
        out = o._ensure_package_step(steps)
        self.assertEqual(len(out), 2)


class TestSearchFailureFallback(unittest.TestCase):
    def test_generation_fallback_code_for_game_instruction(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        alt = o._generation_fallback_step(
            "写一个愤怒的小鸟",
            {"capability": "web_fetch", "instruction": "获取愤怒的小鸟游戏源代码并生成 main.py"},
        )
        self.assertEqual(alt["capability"], "code_execution")
        self.assertIn("main.py", alt["instruction"])

    def test_generation_fallback_summary_for_doc_instruction(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        alt = o._generation_fallback_step(
            "调研市场现状",
            {"capability": "web_search", "instruction": "搜索市场报告"},
        )
        self.assertEqual(alt["capability"], "content_summary")

    def test_replan_fetch_failure_skips_llm(self):
        from orchestrator_v2 import OrchestratorV2

        class _FakeMsg:
            def publish(self, *a, **k):
                pass

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = _FakeMsg()
        alt = o._replan_step(
            "写一个愤怒的小鸟",
            {"step_id": "2", "capability": "web_fetch", "instruction": "获取愤怒的小鸟游戏源代码并生成 main.py"},
            "No URL found in instruction",
            "test-task",
        )
        self.assertIsNotNone(alt)
        self.assertEqual(alt["capability"], "code_execution")

    def test_replan_code_failure_stays_code(self):
        from orchestrator_v2 import OrchestratorV2

        class _FakeMsg:
            def publish(self, *a, **k):
                pass

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = _FakeMsg()
        alt = o._replan_step(
            "写一个愤怒的小鸟",
            {"step_id": "3", "capability": "code_execution", "instruction": "实现游戏并生成 main.py"},
            "Code execution failed: No code generated by LLM",
            "test-task",
        )
        self.assertIsNotNone(alt)
        self.assertEqual(alt["capability"], "code_execution")

    def test_generation_fallback_html_keeps_html(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        alt = o._generation_fallback_step(
            "写一个愤怒的小鸟",
            {"capability": "code_execution", "instruction": "生成一个单文件 HTML 游戏"},
        )
        self.assertEqual(alt["capability"], "code_execution")
        self.assertIn("index.html", alt["instruction"])


class TestSearchRevisionFlow(unittest.TestCase):
    def test_build_revision_replaces_pending_fetch(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        pending = {
            "2": {"step_id": "2", "capability": "web_fetch", "instruction": "获取愤怒的小鸟游戏代码", "depends_on": ["1"]},
            "3": {"step_id": "3", "capability": "file_io", "instruction": "保存文件", "depends_on": ["2"]},
        }
        rev = o._build_search_revision(pending, "写一个愤怒的小鸟")
        self.assertEqual(len(rev), 1)
        self.assertEqual(rev[0]["step_id"], "2")
        self.assertEqual(rev[0]["capability"], "code_execution")
        self.assertEqual(rev[0]["depends_on"], ["1"])

    def test_confirm_revision_timeout_auto_applies(self):
        from orchestrator_v2 import OrchestratorV2

        class _FakeMsg:
            def publish(self, *a, **k):
                pass

        class _FakeRedis:
            def brpop(self, *a, **k):
                raise ConnectionError("simulated redis unavailable")  # 走无结果→自动采用路径

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = _FakeMsg()
        o._redis = _FakeRedis()
        o._plan_confirm_timeout = 300
        revision = [{"step_id": "2", "capability": "code_execution", "instruction": "生成代码", "timeout": 180}]
        result = o._confirm_revision("t", "写一个愤怒的小鸟", [{"step_id": "1"}, {"step_id": "2"}], {}, revision)
        self.assertEqual(result, revision)

    def test_apply_revision_updates_pending_and_steps(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "搜索"},
            {"step_id": "2", "capability": "web_fetch", "instruction": "抓取"},
        ]
        pending = {s["step_id"]: s for s in steps}
        confirmed = [
            {"step_id": "2", "capability": "code_execution", "instruction": "直接生成", "timeout": 180},
        ]
        o._apply_revision(steps, pending, {}, confirmed)
        self.assertEqual(pending["2"]["capability"], "code_execution")
        self.assertEqual(steps[1]["capability"], "code_execution")

    def test_execute_steps_revision_flow_end_to_end(self):
        import json as _json
        from orchestrator_v2 import OrchestratorV2

        published = []

        class _FakeMsg:
            def publish(self, channel, message):
                published.append(message)

        class _FakeRedis:
            def brpop(self, keys, timeout=0):
                steps = [
                    {"step_id": "1", "capability": "web_search", "instruction": "搜索", "timeout": 60},
                    {"step_id": "2", "capability": "code_execution", "instruction": "直接生成游戏", "timeout": 180},
                ]
                return (keys[0], _json.dumps({"action": "confirm", "steps": steps}, ensure_ascii=False))

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = _FakeMsg()
        o._redis = _FakeRedis()
        o._plan_confirm_timeout = 300
        o._stall_timeout = 300
        o._max_parallel = 2
        o._max_retry = 0
        o._replan_depth = 0
        o._find_agent = lambda cap: "fake-agent"
        o._dispatch_step_safe = lambda goal, step, tid, state: {
            "task_id": step.get("step_id"),
            "status": "SUCCESS",
            "result": "[]" if step.get("capability") == "web_search" else "ok",
        }
        o._push_realtime_state = lambda *a, **k: None

        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "搜索开源项目", "timeout": 60},
            {"step_id": "2", "capability": "web_fetch", "instruction": "获取代码", "depends_on": ["1"], "timeout": 60},
        ]
        results, failed = o._execute_steps(steps, "test-task", "写一个愤怒的小鸟")

        confirm_msg = next((m for m in published if m.get("status") == "AWAITING_CONFIRM"), None)
        self.assertIsNotNone(confirm_msg)
        self.assertTrue(confirm_msg.get("revision"))
        by_id = {r.get("task_id"): r for r in results}
        self.assertEqual(by_id["2"]["status"], "SUCCESS")
        self.assertFalse(failed)

    def test_transitive_block_propagation(self):
        import time as _time
        from orchestrator_v2 import OrchestratorV2

        class _FakeMsg:
            def publish(self, channel, message):
                pass

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = _FakeMsg()
        o._plan_confirm_timeout = 300
        o._stall_timeout = 300
        o._max_parallel = 2
        o._max_retry = 0
        o._replan_depth = 0
        o._find_agent = lambda cap: "fake-agent"

        def fake_dispatch(goal, step, tid, state):
            if step.get("step_id") == "2":
                return {"task_id": "2", "status": "FAILED", "result": "boom"}
            return {"task_id": step.get("step_id"), "status": "SUCCESS", "result": "ok"}

        o._dispatch_step_safe = fake_dispatch
        o._push_realtime_state = lambda *a, **k: None
        steps = [
            {"step_id": "1", "capability": "code_execution", "instruction": "a", "depends_on": []},
            {"step_id": "2", "capability": "code_execution", "instruction": "b", "depends_on": ["1"]},
            {"step_id": "3", "capability": "file_io", "instruction": "c", "depends_on": ["2"]},
            {"step_id": "4", "capability": "report_generator", "instruction": "d", "depends_on": ["1", "2", "3"]},
        ]
        t0 = _time.time()
        results, failed = o._execute_steps(steps, "t", "goal")
        self.assertTrue(failed)
        self.assertLess(_time.time() - t0, 30)  # 不应卡到 stall_timeout
        by_id = {r.get("task_id"): r for r in results}
        self.assertEqual(by_id["3"]["status"], "FAILED")
        self.assertEqual(by_id["4"]["status"], "FAILED")


class TestPlanNormalization(unittest.TestCase):
    def test_install_dependency_step_rerouted_to_code_execution(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._max_steps = 8
        steps = o._normalize_steps([
            {"step_id": "1", "capability": "package", "instruction": "安装 pygame 库"},
        ])
        self.assertEqual(steps[0]["capability"], "code_execution")

    def test_best_deliverable_prefers_on_topic(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        off = "加州房价数据分析报告。本报告基于 fetch_california_housing 数据集完成探索性数据分析。" * 12
        on = "愤怒的小鸟游戏开发报告。使用 Python 实现弹弓物理、小鸟发射与碰撞检测等玩法。" * 12
        steps = [
            {"capability": "content_summary"},
            {"capability": "content_summary"},
        ]
        results = [
            {"status": "SUCCESS", "result": off},
            {"status": "SUCCESS", "result": on},
        ]
        best = o._best_deliverable("写一个愤怒的小鸟", steps, results)
        self.assertIn("愤怒的小鸟", best)

    def test_cycle_deps_broken(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        steps = [
            {"step_id": "1", "capability": "code_execution", "instruction": "a", "depends_on": ["2"]},
            {"step_id": "2", "capability": "code_execution", "instruction": "b", "depends_on": ["1"]},
        ]
        out = o._break_cycles(steps)
        self.assertEqual(out[0]["depends_on"], [])
        self.assertEqual(out[1]["depends_on"], [])

    def test_no_cycle_untouched(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        steps = [
            {"step_id": "1", "capability": "code_execution", "instruction": "a", "depends_on": []},
            {"step_id": "2", "capability": "code_execution", "instruction": "b", "depends_on": ["1"]},
        ]
        out = o._break_cycles(steps)
        self.assertEqual(out[1]["depends_on"], ["1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
