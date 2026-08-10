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
    def test_charset_meta_injected_once(self):
        from workers.code_execution_worker import CodeExecutionWorker

        with_head = "<!DOCTYPE html><html><head><title>t</title></head><body>x</body></html>"
        out = CodeExecutionWorker._ensure_charset_meta(with_head)
        self.assertIn('<meta charset="utf-8">', out)
        self.assertLess(out.index('<meta charset="utf-8">'), out.index("<title>"))
        # 已有 charset 不再重复注入
        has_meta = '<!DOCTYPE html><html><head><meta charset="gbk"></head></html>'
        self.assertEqual(CodeExecutionWorker._ensure_charset_meta(has_meta), has_meta)
        # 无 head 时插到 doctype 之后
        no_head = "<!DOCTYPE html><html><body>x</body></html>"
        out2 = CodeExecutionWorker._ensure_charset_meta(no_head)
        self.assertIn('<meta charset="utf-8">', out2)
        self.assertLess(out2.index("<!DOCTYPE"), out2.index('<meta charset="utf-8">'))

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

    def test_html_intent_detection(self):
        from workers.code_execution_worker import CodeExecutionWorker

        self.assertTrue(CodeExecutionWorker._html_intent("生成一个愤怒的小鸟 HTML 游戏"))
        self.assertTrue(CodeExecutionWorker._html_intent("编写网页版游戏"))
        self.assertFalse(CodeExecutionWorker._html_intent(
            "运行Python验证脚本对 angry_birds.html 做静态检查与测试确认"
        ))
        self.assertFalse(CodeExecutionWorker._html_intent(
            "编写冒烟测试验证 index.html 可访问"
        ))

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

    def test_replan_generation_exhausted_stays_code(self):
        from orchestrator_v2 import OrchestratorV2

        class _FakeMsg:
            def publish(self, *a, **k):
                pass

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = _FakeMsg()
        alt = o._replan_step(
            "做一个贪吃蛇游戏",
            {"step_id": "3", "capability": "code_execution",
             "instruction": "实现游戏并生成 index.html"},
            "No valid code after generation/verify/review loop",
            "test-task",
        )
        # 代码生成循环耗尽必须回到代码生成，不得降级成文本摘要步骤
        self.assertIsNotNone(alt)
        self.assertEqual(alt["capability"], "code_execution")
        self.assertIn("index.html", alt["instruction"])

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


class TestStrategyDeployment(unittest.TestCase):
    def test_search_worker_applies_filter_blocks(self):
        from worker_base import SearchAgent

        sa = SearchAgent.__new__(SearchAgent)
        sa._strategy_blocks = ["pinterest"]
        sa._strategy_boosts = []
        results = [
            {"title": "Pinterest pin", "url": "https://www.pinterest.com/pin/1", "snippet": "python code"},
            {"title": "GitHub repo", "url": "https://github.com/foo/bar", "snippet": "python code"},
        ]
        kept = sa._filter_results("python code github", results)
        self.assertTrue(all("pinterest" not in r["url"] for r in kept))
        self.assertEqual(len(kept), 1)

    def test_search_worker_applies_boosts(self):
        from worker_base import SearchAgent

        sa = SearchAgent.__new__(SearchAgent)
        sa._strategy_blocks = []
        sa._strategy_boosts = ["github"]
        results = [
            {"title": "普通文章", "url": "https://example.com/a", "snippet": "python code"},
            {"title": "GitHub repo", "url": "https://github.com/foo/bar", "snippet": "python code"},
        ]
        kept = sa._filter_results("python code", results)
        self.assertEqual(kept[0]["url"], "https://github.com/foo/bar")

    def test_load_active_strategy_parses_rules(self):
        import json as _json
        from worker_base import SearchAgent

        class FakeRedis:
            def get(self, key):
                return _json.dumps({
                    "strategy_id": "s1", "agent_type": "search_agent",
                    "max_sources": 8,
                    "filter_rules": ["排除:pinterest", "优先:github"],
                })

        class FakeMsg:
            def __init__(self):
                self.redis = FakeRedis()

        sa = SearchAgent.__new__(SearchAgent)
        sa._messaging = FakeMsg()
        sa._load_active_strategy()
        self.assertEqual(sa._strategy_max_sources, 8)
        self.assertIn("pinterest", sa._strategy_blocks)
        self.assertIn("github", sa._strategy_boosts)

    def test_safety_gate_persists_pending_request(self):
        import json as _json
        from evolution_sandbox import EvolutionSandbox, StrategyConfig

        pushed = []

        class FakeRedis:
            def rpush(self, key, val):
                pushed.append((key, val))

        class FakeMsg:
            def __init__(self):
                self.redis = FakeRedis()

            def publish(self, *a, **k):
                pass

        sb = EvolutionSandbox.__new__(EvolutionSandbox)
        sb._messaging = FakeMsg()
        sb._poison_list = set()
        winner = StrategyConfig(
            strategy_id="s-win", agent_type="search_agent",
            temperature=0.5, max_sources=7, filter_rules=["排除:x"],
        )
        ok = sb._safety_gate_and_deploy(winner)
        self.assertTrue(ok)
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][0], "evolution:pending")
        data = _json.loads(pushed[0][1])
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["strategy_id"], "s-win")


class TestDeliverySummary(unittest.TestCase):
    def test_summary_includes_files_run_and_launch(self):
        import json as _json
        import os
        import tempfile
        import zipfile
        from orchestrator_v2 import OrchestratorV2
        import workspace as ws_mod

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_sum_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws_mod.task_project_dir("t-sum-1")
            zip_path = os.path.join(tmp, "deliverables.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("index.html", "<html>game</html>")
                zf.writestr("main.py", "print('ok')")
            steps = [
                {"step_id": "1", "capability": "code_execution", "instruction": "生成并运行游戏"},
                {"step_id": "2", "capability": "package", "instruction": "打包交付"},
            ]
            completed = {
                "1": {"status": "SUCCESS", "result": _json.dumps({
                    "status": "success", "output": "pygame ok", "returncode": 0,
                })},
                "2": {"status": "SUCCESS", "result": f"[PACKAGED] x.zip\nDownload: file://{zip_path}"},
            }
            summary, e2e = o._build_delivery_summary("t-sum-1", "写一个愤怒的小鸟", steps, completed)
            self.assertIn("项目交付结果", summary)
            self.assertIn("index.html", summary)
            self.assertIn("main.py", summary)
            self.assertIn("运行验证", summary)
            self.assertIn("如何启动", summary)
            self.assertIn("成果文件夹", summary)
            self.assertIsInstance(e2e, list)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            try:
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass


class TestTaskWorkspaceIsolation(unittest.TestCase):
    """每任务独立成果文件夹：互不污染、可整体移动。"""

    def test_distinct_task_dirs(self):
        import tempfile
        import workspace as ws_mod

        tmp = tempfile.mkdtemp(prefix="weavemind_ws_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            a = ws_mod.task_project_dir("ui-task-a")
            b = ws_mod.task_project_dir("ui-task-b")
            self.assertNotEqual(a, b)
            self.assertTrue(a.parent.name.startswith("ui-task-a"))
            self.assertTrue(b.parent.name.startswith("ui-task-b"))
            # 写入 A 的文件不应出现在 B
            (a / "index.html").write_text("<html>a</html>", encoding="utf-8")
            self.assertTrue((a / "index.html").exists())
            self.assertFalse((b / "index.html").exists())
            # 同名但不同任务的工作区互不影响
            self.assertTrue(str(a).startswith(str(tmp)))
            self.assertTrue(str(b).startswith(str(tmp)))
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_task_id_sanitized(self):
        import workspace as ws_mod

        ws = ws_mod.task_workspace("../../etc/passwd")
        self.assertNotIn("..", ws.name)
        self.assertNotIn("/", ws.name)


class TestE2EGoalTyping(unittest.TestCase):
    """贯通测试按目标类型选择验证强度：游戏走"可玩性"，普通页面走"渲染"。"""

    def test_game_goal_detected(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        self.assertTrue(o._is_game_goal("做一个极简的贪吃蛇游戏，确保能在浏览器里玩"))
        self.assertTrue(o._is_game_goal("用 pygame 实现愤怒的小鸟"))
        self.assertTrue(o._is_game_goal("写一个可玩的打砖块 HTML 游戏"))

    def test_plain_page_not_game(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        self.assertFalse(o._is_game_goal("生成一个单文件 HTML 欢迎页（含标题和按钮）"))
        self.assertFalse(o._is_game_goal("调研工业AI视觉市场并输出报告"))
        self.assertFalse(o._is_game_goal("整理数据科学实训汇报条目"))

    def test_plain_page_passes_render_verify(self):
        import os
        import tempfile
        from pathlib import Path
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        d = tempfile.mkdtemp(prefix="weavemind_e2e_")
        try:
            fp = os.path.join(d, "welcome.html")
            Path(fp).write_text(
                "<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head>"
                "<body><h1>Hello</h1><p>内容</p></body></html>",
                encoding="utf-8",
            )
            ok, detail, _shot = o._playwright_verify(d, "welcome.html", fp, require_game=False)
            self.assertTrue(ok, detail)
            # 同页面走"游戏级"验证必须失败（无 canvas），证明两种模式确实分流
            ok2, detail2, _ = o._playwright_verify(d, "welcome.html", fp, require_game=True)
            self.assertFalse(ok2, detail2)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_non_utf8_page_rejected(self):
        import os
        import tempfile
        from pathlib import Path
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        d = tempfile.mkdtemp(prefix="weavemind_e2e_")
        try:
            fp = os.path.join(d, "no_charset.html")
            Path(fp).write_text(
                "<!DOCTYPE html><html><body><h1>中文标题</h1></body></html>",
                encoding="utf-8",
            )
            ok, detail, _ = o._playwright_verify(d, "no_charset.html", fp, require_game=False)
            self.assertFalse(ok, detail)
            self.assertIn("UTF-8", detail)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_game_that_freezes_on_game_over_rejected(self):
        import os
        import tempfile
        from pathlib import Path
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        d = tempfile.mkdtemp(prefix="weavemind_e2e_")
        broken = """<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body><canvas id="c" width="400" height="400"></canvas><script>
const cv=document.getElementById('c'),ctx=cv.getContext('2d');
let x=10,y=10,dx=0,dy=0;
document.addEventListener('keydown',e=>{
  if(e.key==='ArrowUp'){dx=0;dy=-1;}
  if(e.key==='ArrowRight'){dx=1;dy=0;}
});
function loop(){
  x+=dx;y+=dy;
  ctx.fillStyle='#fff';ctx.fillRect(0,0,400,400);
  ctx.fillStyle='#0a0';ctx.fillRect(x*20,y*20,18,18);
  if(y<0||y>19||x<0||x>19){ alert('over'); reset(); return; }
  setTimeout(loop,100);
}
function reset(){x=10;y=10;dx=0;dy=0;}
loop();
</script></body></html>"""
        fp = os.path.join(d, "broken.html")
        Path(fp).write_text(broken, encoding="utf-8")
        ok, detail, _ = o._playwright_verify(d, "broken.html", fp, require_game=True)
        self.assertFalse(ok, detail)
        self.assertIn("未重启", detail)

        working = broken.replace(
            "if(y<0||y>19||x<0||x>19){ alert('over'); reset(); return; }",
            "if(y<0||y>19||x<0||x>19){ alert('over'); reset(); }",
        )
        fp2 = os.path.join(d, "working.html")
        Path(fp2).write_text(working, encoding="utf-8")
        ok2, detail2, _ = o._playwright_verify(d, "working.html", fp2, require_game=True)
        self.assertTrue(ok2, detail2)
        try:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


class TestPackageTaskIsolation(unittest.TestCase):
    def test_fresh_files_excludes_pre_task_files(self):
        import os
        import tempfile
        import time
        from pathlib import Path
        from workers.packaging_worker import PackagingWorker

        root = Path(tempfile.mkdtemp(prefix="weavemind_pkg_"))
        old_f = root / "old.html"
        new_f = root / "new.html"
        old_f.write_text("old", encoding="utf-8")
        new_f.write_text("new", encoding="utf-8")
        # 旧文件时间戳设为任务开始前 10 分钟
        os.utime(old_f, (time.time() - 600, time.time() - 600))
        try:
            w = PackagingWorker.__new__(PackagingWorker)
            files = w._fresh_files(root, {"task_start_ts": time.time() - 120})
            names = [f[1] for f in files]
            self.assertIn("new.html", names)
            self.assertNotIn("old.html", names)
        finally:
            try:
                old_f.unlink()
                new_f.unlink()
                root.rmdir()
            except Exception:
                pass


class TestTemplateConsolidation(unittest.TestCase):
    def test_consolidate_template_from_successful_task(self):
        import json
        import os
        import tempfile
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = os.path.join(tempfile.mkdtemp(prefix="weavemind_tpl_"), "templates.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"templates": []}, f)
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "任务目标：调研市场\n搜索市场规模与玩家"},
            {"step_id": "2", "capability": "code_execution", "instruction": "计算成本与ROI"},
            {"step_id": "3", "capability": "content_summary", "instruction": "整合摘要"},
            {"step_id": "4", "capability": "report_generator", "instruction": "写报告"},
        ]
        try:
            o._consolidate_template("调研市场规模并测算ROI", steps, tpl_path=tmp)
            data = json.load(open(tmp, encoding="utf-8"))
            self.assertEqual(len(data["templates"]), 1)
            tpl = data["templates"][0]
            self.assertTrue(tpl["name"].startswith("auto-"))
            caps = [s["capability"] for s in tpl["steps"]]
            self.assertIn("web_search", caps)
            self.assertIn("code_execution", caps)
            self.assertIn("content_summary", caps)
            self.assertNotIn("report_generator", caps)
            self.assertNotIn("package", caps)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def test_off_topic_task_not_consolidated(self):
        import json
        import os
        import tempfile
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = os.path.join(tempfile.mkdtemp(prefix="weavemind_tpl_"), "templates.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"templates": []}, f)
        # 目标是新能源车调研，但步骤全是房价——跑偏任务不应沉淀
        goal = "调研2026年国内新能源汽车市场"
        steps = [
            {"step_id": "1", "capability": "data_loader",
             "instruction": f"用户目标：{goal}\n原始指令：加载加州房价数据集"},
            {"step_id": "2", "capability": "model_trainer",
             "instruction": f"用户目标：{goal}\n原始指令：训练房价预测模型"},
        ]
        try:
            o._consolidate_template(
                goal, steps, tpl_path=tmp,
            )
            data = json.load(open(tmp, encoding="utf-8"))
            self.assertEqual(len(data.get("templates", [])), 0)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass


class TestSimpleTaskFastPath(unittest.TestCase):
    """简单任务快速路径：只影响直达型任务，复杂任务逻辑保持不变。"""

    def test_simple_plan_detected(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        simple = [
            {"capability": "code_execution", "instruction": "生成 index.html"},
            {"capability": "report_generator", "instruction": "写报告"},
            {"capability": "package", "instruction": "打包"},
        ]
        self.assertTrue(o._is_simple_task(simple))

    def test_complex_plan_not_simple(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        with_search = [
            {"capability": "web_search", "instruction": "搜索"},
            {"capability": "code_execution", "instruction": "写代码"},
        ]
        self.assertFalse(o._is_simple_task(with_search))
        two_code = [
            {"capability": "code_execution", "instruction": "a"},
            {"capability": "code_execution", "instruction": "b"},
        ]
        self.assertFalse(o._is_simple_task(two_code))
        data_pipeline = [
            {"capability": "data_loader", "instruction": "加载数据"},
            {"capability": "model_trainer", "instruction": "训练"},
        ]
        self.assertFalse(o._is_simple_task(data_pipeline))
        self.assertFalse(o._is_simple_task([]))

    def test_html_intent_prefers_user_goal(self):
        from workers.code_execution_worker import CodeExecutionWorker

        # 规划器通用后缀"并验证通过"不得把 HTML 任务改判为 Python 任务
        instr = (
            "用户目标：生成一个简单的单文件HTML欢迎页（index.html，包含标题、段落和一个按钮），保存为index.html，确保浏览器能打开\n"
            "原始指令：根据目标生成完整可运行的自包含交付物（单文件 HTML 或 Python 脚本），确保能直接在浏览器/命令行运行并验证通过"
        )
        self.assertTrue(CodeExecutionWorker._html_intent(instr))
        # 纯验证类指令（无用户目标）仍判为 Python 验证任务
        self.assertFalse(CodeExecutionWorker._html_intent(
            "运行Python验证脚本对 angry_birds.html 做静态检查与测试确认"
        ))
        self.assertFalse(CodeExecutionWorker._html_intent(
            "编写冒烟测试验证 index.html 可访问"
        ))

    def test_packaging_skips_llm_for_simple(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest import mock
        from workers.packaging_worker import PackagingWorker

        ws = Path(tempfile.mkdtemp(prefix="weavemind_pkgfast_"))
        (ws / "project").mkdir(parents=True, exist_ok=True)
        (ws / "project" / "index.html").write_text("<html>hi</html>", encoding="utf-8")
        w = PackagingWorker.__new__(PackagingWorker)
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise RuntimeError("LLM should not be called for simple task")

        with mock.patch("llm_client.call_llm", side_effect=boom):
            res = w._sync_package("打包", {"workspace": str(ws), "simple": True})
        self.assertEqual(calls["n"], 0, "简单任务不应调用 LLM 解析路径")
        self.assertIn("Download: file://", res)
        self.assertIn(str(ws), res)
        self.assertTrue(list(ws.glob("*.zip")))

    def test_packaging_complex_keeps_llm(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from workers.packaging_worker import PackagingWorker

        ws = Path(tempfile.mkdtemp(prefix="weavemind_pkgfast_"))
        (ws / "project").mkdir(parents=True, exist_ok=True)
        (ws / "project" / "index.html").write_text("<html>hi</html>", encoding="utf-8")
        w = PackagingWorker.__new__(PackagingWorker)
        calls = {"n": 0}

        def fail_llm(*a, **k):
            calls["n"] += 1
            raise RuntimeError("simulated LLM failure")

        with mock.patch("llm_client.call_llm", side_effect=fail_llm):
            res = w._sync_package("打包", {"workspace": str(ws), "simple": False})
        self.assertEqual(calls["n"], 1, "复杂任务仍走 LLM 路径解析（失败回退工作区）")
        self.assertIn("Download: file://", res)

    def test_code_execution_simple_skips_tdd_and_review(self):
        import asyncio
        import json
        import tempfile
        from pathlib import Path
        from workers.code_execution_worker import CodeExecutionWorker

        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        ws = Path(tempfile.mkdtemp(prefix="weavemind_cfast_"))
        w.workspace = ws
        calls = {"tdd": 0, "review": 0}

        async def fake_llm(system="", prompt="", instruction="", max_attempts=3, max_tokens=2000):
            self.assertEqual(max_attempts, 2, "简单任务应减少主端点尝试次数")
            self.assertEqual(max_tokens, 2000)
            return "print('hello from simple task')"

        async def fake_tdd(*a, **k):
            calls["tdd"] += 1
            return False, ""

        async def fake_review(*a, **k):
            calls["review"] += 1
            return True, ""

        w._call_llm = fake_llm
        w._tdd_pilot = fake_tdd
        w._review_code = fake_review
        res = json.loads(asyncio.run(w.execute(
            "用户目标：写一个 Python 脚本输出 hello\n原始指令：生成脚本",
            {"workspace": str(ws), "simple": True},
        )))
        self.assertEqual(res["status"], "success")
        self.assertEqual(calls["tdd"], 0, "简单任务跳过 TDD pilot")
        self.assertEqual(calls["review"], 0, "简单任务跳过代码审查")

    def test_orchestrator_simple_skips_reflection(self):
        import os
        import tempfile
        import zipfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2
        from test_orchestrator_v2 import make_orch

        tmp = tempfile.mkdtemp(prefix="weavemind_reflect_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        html = ('<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
                "<body><h1>hi</h1></body></html>")
        o = make_orch()
        o._plan = lambda goal, task_id, context="": [
            {"step_id": "1", "capability": "code_execution", "instruction": "x", "timeout": 120},
            {"step_id": "2", "capability": "report_generator", "instruction": "r", "timeout": 120},
            {"step_id": "3", "capability": "package", "instruction": "p", "timeout": 120},
        ]
        reflected = {"n": 0}

        def fake_reflect(goal, report, task_id):
            reflected["n"] += 1
            return {"accepted": False, "gaps": ["more"],
                    "next_steps": [{"step_id": "x", "capability": "code_execution", "instruction": "补", "timeout": 120}]}

        o._reflect = fake_reflect

        def fake_execute(steps, task_id, goal):
            # 模拟真实落盘：code 步骤写文件、package 步骤打包（run() 开头会清空工作区，
            # 所以文件必须在执行阶段创建）
            proj = ws_mod.task_project_dir("t-simple-1")
            (proj / "index.html").write_text(html, encoding="utf-8")
            zip_path = os.path.join(tmp, "deliverables.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("index.html", html)
            out = []
            for s in steps:
                if s.get("capability") == "package":
                    out.append({"task_id": s["step_id"], "status": "SUCCESS",
                                "result": f"[PACKAGED]\nDownload: file://{zip_path}"})
                else:
                    out.append({"task_id": s["step_id"], "status": "SUCCESS",
                                "result": f"ok-{s['step_id']}"})
            return out, False

        o._execute_steps = fake_execute
        o._now_iso = lambda: "t"
        try:
            res = o.run("t-simple-1", "生成一个 HTML 欢迎页", auto_run=True)
            self.assertEqual(res["status"], "SUCCESS")
            self.assertEqual(reflected["n"], 0, "简单任务跳过反射评审")
            fast_logs = [
                m for _, m in o._messaging.published
                if "fast path enabled" in str(m.get("payload", {}).get("message", ""))
            ]
            self.assertTrue(fast_logs, "应推送 fast path 进度消息")
            self.assertNotIn("t-simple-1", o._task_simple, "任务结束后标志应被清理")
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_report_links_rewritten(self):
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_links_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws = str(ws_mod.task_workspace("t-link-1"))
            report = (
                f"![heatmap]({ws}\\charts\\heatmap.png)\n\n"
                f"![散点]({ws}/data/scatter.png)\n\n"
                f"**成果文件夹**：`{ws}`\n\n"
                "引用 [报告](https://example.com/a) 不应改动"
            )
            out = o._rewrite_report_links(report, "t-link-1")
            self.assertIn("](/files/t-link-1/charts/heatmap.png)", out)
            self.assertIn("](/files/t-link-1/data/scatter.png)", out)
            self.assertIn(f"**成果文件夹**：`{ws}`", out, "正文绝对路径保持不变")
            self.assertIn("https://example.com/a", out)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sweep_keeps_only_newest_zip(self):
        import os
        import tempfile
        import time
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_sweep_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws = ws_mod.task_workspace("t-sweep-1")
            ws.mkdir(parents=True, exist_ok=True)
            for i, name in enumerate(("a.zip", "b.zip", "c.zip")):
                p = ws / name
                p.write_text(f"zip-{i}", encoding="utf-8")
                os.utime(p, (time.time() + i, time.time() + i))
            o._sweep_workspace_artifacts("t-sweep-1")
            zips = sorted(p.name for p in ws.glob("*.zip"))
            self.assertEqual(zips, ["c.zip"], "只保留最新交付包")
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_packaging_includes_charts_and_data(self):
        import os
        import tempfile
        import time
        from pathlib import Path
        from workers.packaging_worker import PackagingWorker

        ws = Path(tempfile.mkdtemp(prefix="weavemind_chartpkg_"))
        (ws / "project").mkdir(parents=True)
        (ws / "charts").mkdir(parents=True)
        (ws / "data").mkdir(parents=True)
        now = time.time()
        (ws / "project" / "index.html").write_text("<html>hi</html>", encoding="utf-8")
        (ws / "charts" / "heatmap.png").write_bytes(b"png")
        (ws / "data" / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        for p in ws.rglob("*"):
            if p.is_file():
                os.utime(p, (now, now))
        w = PackagingWorker.__new__(PackagingWorker)
        files = w._fresh_files(ws / "project", {"workspace": str(ws), "task_start_ts": now - 60})
        names = [rel for _, rel in files]
        self.assertIn("index.html", names)
        self.assertIn("charts/heatmap.png", names, "图表应进入交付包")
        self.assertIn("data/x.csv", names, "数据应进入交付包")

    def test_workspace_path_safe_helper(self):
        import tempfile
        import workspace as ws_mod
        import web_ui

        tmp = tempfile.mkdtemp(prefix="weavemind_wsafe_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws = ws_mod.task_workspace("t-safe-1")
            (ws / "charts").mkdir(parents=True)
            (ws / "charts" / "a.png").write_bytes(b"x")
            self.assertIsNotNone(web_ui._safe_workspace_path("charts/a.png", "t-safe-1"))
            self.assertIsNone(web_ui._safe_workspace_path("../escape.txt", "t-safe-1"))
            self.assertIsNone(web_ui._safe_workspace_path("..\\escape.txt", "t-safe-1"))
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_delivery_must_contain_code_files(self):
        import os
        import tempfile
        import zipfile
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_dlv_")
        try:
            # 只有报告 → 判定为无代码交付物
            z1 = os.path.join(tmp, "only_report.zip")
            with zipfile.ZipFile(z1, "w") as zf:
                zf.writestr("reports/report.md", "# 报告")
            steps = [{"step_id": "p", "capability": "package", "instruction": "x"}]
            done = {"p": {"status": "SUCCESS", "result": f"Download: file://{z1}"}}
            self.assertFalse(o._delivery_has_code_files(steps, done))
            # 含 index.html → 有代码交付物
            z2 = os.path.join(tmp, "with_html.zip")
            with zipfile.ZipFile(z2, "w") as zf:
                zf.writestr("index.html", "<html></html>")
                zf.writestr("reports/report.md", "# 报告")
            done2 = {"p": {"status": "SUCCESS", "result": f"Download: file://{z2}"}}
            self.assertTrue(o._delivery_has_code_files(steps, done2))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
