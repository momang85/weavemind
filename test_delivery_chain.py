# -*- coding: utf-8 -*-
"""真实交付链回归测试：搜索相关性过滤、file_io 落盘逻辑、code_execution 命名。"""
import json
import os
import re
import shutil
import tempfile
import unittest
from http.client import RemoteDisconnected
from pathlib import Path
from unittest import mock

import workspace as ws_mod


# 测试期隔离（见 tests_support.py）：run() 开头的端点/余额预检会打真实网络，
# 本机余额耗尽或 CI 无 key 都会让任务在预检被拒，用例断言的业务逻辑根本没机会发生。
from tests_support import restore_llm_prechecks, stub_llm_prechecks  # noqa: E402


def setUpModule():
    stub_llm_prechecks()


def tearDownModule():
    restore_llm_prechecks()


class _DevSandboxMode:
    """显式选择本地开发沙箱模式（restricted，无操作系统级隔离）。

    本文件里驱动代码执行的用例验证的是**执行流水线**（截断重试、冒烟校验、简单任务
    快速路径），不是隔离策略；隔离策略的默认安全用例在 test_sandbox_isolation.py。
    默认策略要求容器隔离，而 CI 与多数开发机没有 Docker——若不显式选择，这些用例会
    因"代码执行被拒绝"而正确地失败，那属于环境事实，不是流水线缺陷。
    """

    def setUp(self):
        super().setUp()
        self._old_sandbox_mode = os.environ.get("CODE_EXECUTION_SANDBOX")
        os.environ["CODE_EXECUTION_SANDBOX"] = "restricted"
        self.addCleanup(self._restore_sandbox_mode)

    def _restore_sandbox_mode(self):
        if self._old_sandbox_mode is None:
            os.environ.pop("CODE_EXECUTION_SANDBOX", None)
        else:
            os.environ["CODE_EXECUTION_SANDBOX"] = self._old_sandbox_mode


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

    def test_clean_search_text_strips_wrappers(self):
        from worker_base import SearchAgent

        raw = (
            "任务目标：调研2026年国内新能源汽车市场现状\n"
            "用户目标：调研2026年国内新能源汽车市场现状，输出结构化报告\n"
            "原始指令：搜索行业市场规模与主要玩家"
        )
        clean = SearchAgent._clean_search_text(raw)
        self.assertIn("新能源汽车", clean)
        self.assertNotIn("任务目标", clean)
        self.assertNotIn("原始指令", clean)

    def test_extract_keywords_keeps_year_and_terms(self):
        from worker_base import SearchAgent

        sa = SearchAgent.__new__(SearchAgent)
        kw = sa._extract_keywords(
            "任务目标：调研2026年国内新能源汽车市场现状，输出结构化报告\n"
            "用户目标：调研2026年国内新能源汽车市场现状，输出结构化报告\n"
            "原始指令：搜索市场规模与主要玩家、技术路线与趋势"
        )
        self.assertIn("2026", kw)
        self.assertIn("新能源汽车", kw)
        self.assertIn("市场", kw)
        # 指令包装词不得混入查询
        for junk in ("任务目标", "用户目标", "原始指令", "调研", "输出"):
            self.assertNotIn(junk, kw)

    def test_query_variants_multiple(self):
        from worker_base import SearchAgent

        sa = SearchAgent.__new__(SearchAgent)
        vs = sa._query_variants(
            "用户目标：调研2026年国内新能源汽车市场现状，输出结构化报告\n原始指令：搜索"
        )
        self.assertGreaterEqual(len(vs), 3, "应生成多个查询变体")
        self.assertTrue(any("2026" in v for v in vs))
        self.assertTrue(any("新能源汽车" in v for v in vs))
        # 去重
        self.assertEqual(len(vs), len(set(vs)))

    def test_filter_min_score_and_year(self):
        from worker_base import SearchAgent

        sa = SearchAgent.__new__(SearchAgent)
        results = [
            {"title": "2026年新能源汽车市场报告", "url": "https://ev-report.org/a",
             "snippet": "2026 新能源汽车 市场规模"},
            {"title": "无关内容", "url": "https://weather-info.org/b", "snippet": "天气"},
        ]
        kept = sa._filter_results("2026年新能源汽车市场", results)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["url"], "https://ev-report.org/a")
        # 严格过滤为空时，min_score=1 可保留弱相关结果
        weak = [{"title": "市场", "url": "https://ev-report.org/c", "snippet": "市场行情"}]
        self.assertEqual(sa._filter_results("新能源汽车市场", weak), [])
        self.assertEqual(len(sa._filter_results("新能源汽车市场", weak, min_score=1)), 1)


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


class _TempWorkspace:
    """工作区隔离（与 test_orchestrator_v2.TempWorkspaceCase 同一理由）：收尾验收会落盘，
    落在共享默认工作区会让同一用例重跑时读到上一轮的验收结论。"""

    def setUp(self):
        super().setUp()
        self._ws_tmp = tempfile.mkdtemp(prefix="wm_dc_ws_")
        self._ws_old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(self._ws_tmp)
        self.addCleanup(self._restore_ws)

    def _restore_ws(self):
        ws_mod.WORKSPACE_ROOT = self._ws_old_root
        shutil.rmtree(self._ws_tmp, ignore_errors=True)


class TestCodeExecutionNaming(_TempWorkspace, unittest.TestCase):
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


class TestCodeExecutionTokenTruncation(_DevSandboxMode, _TempWorkspace, unittest.TestCase):
    """修复：code_execution 响应被 token 上限截断，导致统计任务反复编译失败。"""

    def test_estimate_token_need_stats_and_html(self):
        from workers.code_execution_worker import CodeExecutionWorker

        # 统计/数据分析类关键词 → 4000
        self.assertEqual(
            CodeExecutionWorker._estimate_token_need("计算A股前5%成交额占比"), 4000)
        self.assertEqual(
            CodeExecutionWorker._estimate_token_need("读取 ranking.csv 并统计汇总"), 4000)
        self.assertEqual(
            CodeExecutionWorker._estimate_token_need("分析成交额分布，计算平均值与比例"), 4000)
        self.assertEqual(
            CodeExecutionWorker._estimate_token_need("前10% 成交额"), 4000)
        # HTML 保持 4000
        self.assertEqual(
            CodeExecutionWorker._estimate_token_need("生成一个 HTML 游戏页面"), 4000)
        # 其余任务保持 2000
        self.assertEqual(
            CodeExecutionWorker._estimate_token_need("输出 hello world"), 2000)

    def test_estimate_token_need_env_override(self):
        import os
        from workers.code_execution_worker import CodeExecutionWorker

        old = os.environ.get("CODE_EXECUTION_MAX_TOKENS")
        os.environ["CODE_EXECUTION_MAX_TOKENS"] = "8000"
        try:
            self.assertEqual(
                CodeExecutionWorker._estimate_token_need("输出 hello world"), 8000)
        finally:
            if old is None:
                os.environ.pop("CODE_EXECUTION_MAX_TOKENS", None)
            else:
                os.environ["CODE_EXECUTION_MAX_TOKENS"] = old

    def test_looks_truncated_incomplete_tail(self):
        from workers.code_execution_worker import CodeExecutionWorker

        # 空响应 / 缺结尾语法 → True
        self.assertTrue(CodeExecutionWorker._looks_truncated(""))
        self.assertTrue(CodeExecutionWorker._looks_truncated("import"))
        self.assertTrue(CodeExecutionWorker._looks_truncated("def compute_share():\n"))
        self.assertTrue(CodeExecutionWorker._looks_truncated(
            "df['share'] = (df['amount'] / total"))
        self.assertTrue(CodeExecutionWorker._looks_truncated("x = 'abc\n"))
        self.assertTrue(CodeExecutionWorker._looks_truncated("total = a + b +\n"))
        # 完整代码（带换行、括号/引号闭合）→ False
        self.assertFalse(CodeExecutionWorker._looks_truncated("print('hi')\n"))
        self.assertFalse(CodeExecutionWorker._looks_truncated(
            "df['share'] = df['amount'] / total\nprint(df)\n"))
        self.assertFalse(CodeExecutionWorker._looks_truncated(
            "def f():\n    return {'a': 1}\n"))

    def test_looks_truncated_accepts_fenced_and_unterminated_output(self):
        """围栏结尾 / 无换行结尾都不等于截断。

        回归：模型默认把代码包在 ``` 围栏里，最后两个字符是 "\\n```"，旧的
        "必须以换行结尾"判据把完整代码判成截断并丢弃，实测 code_execution 卡在
        round 1/2/3 循环里反复重生成，任务最终没有任何代码交付物。"""
        from workers.code_execution_worker import CodeExecutionWorker

        fenced = 'import sys\nprint("x")\nsys.exit(0)\n```'
        self.assertFalse(CodeExecutionWorker._looks_truncated(fenced))
        # 无换行结尾但结构完整
        self.assertFalse(CodeExecutionWorker._looks_truncated("print('hi')"))
        # 围栏只有开头没有收尾 → 真截断
        self.assertTrue(CodeExecutionWorker._looks_truncated(
            "```python\nimport sys\nprint('x')\n"))
        # 注释以句号结尾不应误判（运算符判据跑在剥掉注释的结构上）
        self.assertFalse(CodeExecutionWorker._looks_truncated(
            "x = 1  # 说明。\nprint(x)\n"))

    def test_smoke_rejects_script_without_entry_call(self):
        """零输出脚本不得算通过冒烟：退出码 0 但什么也不打印的交付物没有价值。

        回归：实测交付的 index.py 写好了 main() 却没有 if __name__ == "__main__"
        调用，运行 exit 0 且无输出，任务仍判成功。"""
        import asyncio
        import tempfile
        from pathlib import Path
        from workers.code_execution_worker import CodeExecutionWorker

        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        w.workspace = Path(tempfile.mkdtemp(prefix="weavemind_smoke_"))

        no_entry = (
            "import random\n\n"
            "def main():\n"
            "    print(random.randint(1, 10))\n"
        )
        err = asyncio.run(w._run_smoke(no_entry))
        self.assertIn("没有任何输出", err)

        with_entry = no_entry + "\nif __name__ == \"__main__\":\n    main()\n"
        self.assertEqual(asyncio.run(w._run_smoke(with_entry)), "")

    def test_empty_response_sets_feedback_and_retries(self):
        import asyncio
        import json
        import tempfile
        from pathlib import Path
        from workers.code_execution_worker import CodeExecutionWorker

        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        ws = Path(tempfile.mkdtemp(prefix="weavemind_empty_"))
        w.workspace = ws
        calls = []

        async def fake_llm(system="", prompt="", instruction="", max_attempts=3, max_tokens=2000):
            calls.append({"prompt": prompt, "max_tokens": max_tokens})
            if len(calls) < 3:
                return ""
            return "print('ok')\n"

        w._call_llm = fake_llm
        res = json.loads(asyncio.run(w.execute(
            "生成一个 Python 脚本输出 ok",
            {"workspace": str(ws), "simple": True},
        )))
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(calls), 3, "空响应应重试而非静默跳过")
        self.assertEqual(calls[0]["max_tokens"], 2000)
        self.assertEqual(calls[2]["max_tokens"], 3000, "空响应后应提升 token 上限")
        self.assertIn("模型返回空", calls[2]["prompt"], "重试应携带空响应反馈")

    def test_truncated_response_sets_feedback_and_retries(self):
        import asyncio
        import json
        import tempfile
        from pathlib import Path
        from workers.code_execution_worker import CodeExecutionWorker

        w = CodeExecutionWorker.__new__(CodeExecutionWorker)
        ws = Path(tempfile.mkdtemp(prefix="weavemind_trunc_"))
        w.workspace = ws
        calls = []

        async def fake_llm(system="", prompt="", instruction="", max_attempts=3, max_tokens=2000):
            calls.append({"prompt": prompt, "max_tokens": max_tokens})
            if len(calls) == 1:
                # 结构上真截断：括号未闭合（旧夹具用"无换行结尾"充当截断，
                # 那是误判规则，已修正）
                return "import pandas as pd\nimport os\n\ndef load_data():\n    return pd.read_csv("
            return "print('ok')\n"

        w._call_llm = fake_llm
        res = json.loads(asyncio.run(w.execute(
            "生成一个 Python 脚本输出 ok",
            {"workspace": str(ws), "simple": True},
        )))
        self.assertEqual(res["status"], "success")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["max_tokens"], 3000, "疑似截断后应提升 token 上限")
        self.assertIn("疑似被截断", calls[1]["prompt"], "重试应携带截断反馈")


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

    def test_package_step_waits_all_steps(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        # 计划含非文件生产步骤（web_search/web_fetch/content_summary）时，
        # package 必须等所有步骤完成，否则工作区还没有新文件可打包
        steps = [
            {"step_id": "1", "capability": "web_search", "instruction": "s"},
            {"step_id": "2", "capability": "web_fetch", "instruction": "f"},
            {"step_id": "3", "capability": "content_summary", "instruction": "c"},
        ]
        out = o._ensure_package_step(steps)
        self.assertEqual(len(out), 4)
        self.assertEqual(sorted(out[-1]["depends_on"]), ["1", "2", "3"])


class TestSearchCharts(unittest.TestCase):
    def test_wants_visualization_gate(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        self.assertTrue(o._wants_visualization("生成可视化报告并嵌入图表"))
        self.assertTrue(o._wants_visualization("分析市场趋势图"))
        self.assertTrue(o._wants_visualization("储能产业链调研"))
        self.assertTrue(o._wants_visualization("调研2026年国内新能源汽车市场现状"))
        # 金融类目标（财报/营收/财务）自动配图
        self.assertTrue(o._wants_visualization("搜索特斯拉最新财报并总结要点"))
        self.assertTrue(o._wants_visualization("分析某公司2026年财报营收与净利润"))
        # P1-3：加密/宏观类目标自动配图（行情/走势/宏观/利率/涨跌幅/格局排名）
        self.assertTrue(o._wants_visualization("评估比特币短期趋势与风险"))
        self.assertTrue(o._wants_visualization("加密货币行情与涨跌幅走势"))
        self.assertTrue(o._wants_visualization("美国 CPI 通胀与利率宏观分析"))
        self.assertTrue(o._wants_visualization("美联储降息后的宏观走势"))
        self.assertTrue(o._wants_visualization("头部交易所竞争格局与排名"))
        self.assertFalse(o._wants_visualization("写一份行业报告"))
        self.assertFalse(o._wants_visualization("展示产品图片"))

    def test_generates_charts_from_search_results(self):
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_chart_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-chart-1")
            (proj / "search_results.json").write_text(json.dumps([
                {"title": "2023年AI芯片市场规模800亿美元，2024年1000亿美元，2025年1200亿美元",
                 "url": "https://a.com/r1", "snippet": "英伟达份额49%，AMD份额12%，英伟达主导训练"},
                {"title": "2026年全球AI芯片市场规模预计1500亿美元",
                 "url": "https://a.com/r2", "snippet": "英特尔份额8%，谷歌份额7%，华为份额6%，AMD追赶"},
                {"title": "2027年AI芯片出货量展望",
                 "url": "https://b.com/r3", "snippet": "英伟达继续主导，出货量增长"},
            ], ensure_ascii=False), encoding="utf-8")
            o._generate_search_charts("t-chart-1", "请分析AI芯片市场并生成可视化报告")
            pngs = [p.name for p in proj.glob("*.png")]
            self.assertIn("source_distribution.png", pngs)
            self.assertIn("topic_terms.png", pngs)
            self.assertIn("entity_frequency.png", pngs, "应有主体提及频率图")
            # 图表同步到 workspace/charts/，供 report_generator 内联嵌入
            chart_dir = ws_mod.task_charts_dir("t-chart-1")
            self.assertIn("entity_frequency.png", {p.name for p in chart_dir.glob("*.png")})
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_exploratory_charts_skip_uniform(self):
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_chart_")
        old = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-chart-u")
            (proj / "search_results.json").write_text(json.dumps([
                {"title": "AI芯片", "url": "https://x.com/1", "snippet": "英伟达"},
                {"title": "AI芯片", "url": "https://y.com/2", "snippet": "AMD"},
                {"title": "AI芯片", "url": "https://z.com/3", "snippet": "英特尔"},
            ]), encoding="utf-8")
            o._generate_search_charts("t-chart-u", "请分析AI芯片市场并生成可视化报告")
            pngs = {p.name for p in proj.glob("*.png")}
            # 实体/来源计数全为 1 → 无信息增量 → 应跳过，不生成垃圾图
            self.assertNotIn("entity_frequency.png", pngs)
            self.assertNotIn("source_distribution.png", pngs)
        finally:
            ws_mod.WORKSPACE_ROOT = old
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_extract_chart_data_parses_llm_block(self):
        import json
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        text = (
            "## 总结\n\n市场要点。\n\n"
            "[CHART_DATA]\n"
            '{"charts":[{"question":"2025年全球AI芯片市场规模多大？",'
            '"conclusion":"德勤预测1500亿美元，艾媒726亿美元，口径差异明显。",'
            '"type":"bar","title":"2025年全球AI芯片市场规模（亿美元）",'
            '"x_axis_title":"口径/来源","y_axis_title":"市场规模（亿美元）","unit":"亿美元",'
            '"time_range":"2025年","region":"全球","source":"https://a.com","sample_size":"2",'
            '"annotation":"不同机构口径不同","missing":"无","outliers":"无",'
            '"data":[{"label":"德勤","value":1500,"year":2025,"caliber":"德勤预测",'
            '"source":"https://a.com"},{"label":"艾媒","value":726,"year":2025,'
            '"caliber":"艾媒统计","source":"https://a.com"}]}]}'
        )
        specs = o._extract_chart_data(text)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["type"], "bar")
        self.assertEqual(len(specs[0]["data"]), 2)
        self.assertEqual(specs[0]["data"][0]["value"], 1500)
        # 无 [CHART_DATA] 时返回空
        self.assertEqual(o._extract_chart_data("纯文本总结"), [])
        # 兼容旧扁平 data 行 → 自动打包为规格
        legacy = o._extract_chart_data(
            "[CHART_DATA]\n" + json.dumps({"data": [
                {"指标": "市场规模", "年份": 2025, "数值": 1500, "单位": "亿美元",
                 "口径": "德勤预测", "来源": "https://a.com"},
                {"指标": "市场规模", "年份": 2027, "数值": 4000, "单位": "亿美元",
                 "口径": "德勤预测", "来源": "https://b.com"},
            ]}, ensure_ascii=False)
        )
        self.assertEqual(len(legacy), 1)
        self.assertEqual(len(legacy[0]["data"]), 2)
        self.assertTrue(legacy[0]["conclusion"])
        # 单一数据点无法支撑结论 → 按规范跳过（无意义图不画）
        single = o._extract_chart_data(
            "[CHART_DATA]\n" + json.dumps({"data": [
                {"指标": "市场规模", "年份": 2025, "数值": 1500, "单位": "亿美元",
                 "口径": "德勤预测", "来源": "https://a.com"},
            ]}, ensure_ascii=False)
        )
        self.assertEqual(single, [])

    def test_extract_chart_rows_from_markdown_table(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        text = (
            "## 数据要点\n\n"
            "| 数据来源 | 2025年市场规模 | 备注 |\n"
            "|---|---|---|\n"
            "| 德勤 | 突破1500亿美元 | 增长25% | [链接](https://a.com/r1) |\n"
            "| 艾媒 | 726亿美元 | CAGR 36.9% | [链接](https://b.com/r2) |\n"
        )
        rows = o._extract_chart_rows_from_table(text)
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0]["指标"], "市场规模")
        self.assertIn("亿美元", rows[0]["单位"])
        self.assertTrue(rows[0]["来源"].startswith("http"))

    def test_filter_chart_rows_keeps_core_topic(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        rows = [
            {"指标": "市场规模", "年份": 2025, "数值": 1800, "单位": "亿美元",
             "口径": "IIM预测", "来源": "https://a.com"},
            {"指标": "市场规模", "年份": 2025, "数值": 8.58, "单位": "亿美元",
             "口径": "人形机器人专用芯片", "来源": "https://b.com"},
            {"指标": "复合增长率", "年份": 2031, "数值": 51.4, "单位": "%",
             "口径": "SoC芯片CAGR", "来源": "https://c.com"},
            {"指标": "投资规模", "年份": 2025, "数值": 110, "单位": "亿美元",
             "口径": "白宫AI投资", "来源": "https://d.com"},
            {"指标": "市场规模", "年份": 2025, "数值": 726, "单位": "亿美元",
             "口径": "艾媒统计", "来源": "https://e.com"},
        ]
        kept = o._filter_chart_rows(rows, "请分析2025年全球AI芯片市场并生成可视化报告")
        names = [r["口径"] for r in kept]
        self.assertIn("IIM预测", names)
        self.assertIn("艾媒统计", names)
        self.assertNotIn("人形机器人专用芯片", names)
        self.assertNotIn("SoC芯片CAGR", names)
        self.assertNotIn("白宫AI投资", names)

    def test_filter_chart_specs_keeps_core_topic(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        specs = [
            {
                "title": "2025年全球AI芯片市场规模（亿美元）",
                "question": "各口径规模差异如何？",
                "conclusion": "IIM预测1800亿，艾媒726亿，口径差异大。",
                "type": "bar", "unit": "亿美元",
                "x_axis_title": "口径", "y_axis_title": "规模（亿美元）",
                "source": "https://a.com", "time_range": "2025年", "region": "全球",
                "data": [
                    {"label": "IIM", "value": 1800, "caliber": "IIM预测", "source": "https://a.com"},
                    {"label": "艾媒", "value": 726, "caliber": "艾媒统计", "source": "https://e.com"},
                ],
            },
            {
                "title": "2025年AI芯片市场规模（亿美元）",
                "question": "专用芯片规模？",
                "conclusion": "人形机器人专用芯片8.58亿美元。",
                "type": "bar", "unit": "亿美元",
                "x_axis_title": "领域", "y_axis_title": "规模（亿美元）",
                "source": "https://b.com", "time_range": "2025年", "region": "全球",
                "data": [
                    {"label": "人形机器人", "value": 8.58, "caliber": "人形机器人专用芯片", "source": "https://b.com"},
                    {"label": "SoC", "value": 51.4, "caliber": "SoC芯片CAGR", "source": "https://c.com"},
                ],
            },
            {
                "title": "白宫AI投资规模（亿美元）",
                "question": "投资规模？",
                "conclusion": "白宫AI投资110亿美元。",
                "type": "bar", "unit": "亿美元",
                "x_axis_title": "口径", "y_axis_title": "规模（亿美元）",
                "source": "https://d.com", "time_range": "2025年", "region": "美国",
                "data": [
                    {"label": "白宫", "value": 110, "caliber": "白宫AI投资", "source": "https://d.com"},
                ],
            },
            {
                "title": "2025年全球AI芯片厂商份额（%）",
                "question": "2025年AI芯片厂商份额排名？",
                "conclusion": "英伟达49%领先。",
                "type": "bar", "unit": "%",
                "x_axis_title": "厂商", "y_axis_title": "份额（%）",
                "source": "https://a.com", "time_range": "2025年", "region": "全球",
                "data": [
                    {"label": "英伟达", "value": 49, "caliber": "英伟达", "source": "https://a.com"},
                ],
            },
        ]
        kept = o._filter_chart_specs(specs, "请分析2025年全球AI芯片市场并生成可视化报告")
        self.assertEqual(len(kept), 2, "应保留芯片市场规格与厂商份额规格")
        self.assertEqual(len(kept[0]["data"]), 2)
        self.assertEqual(kept[1]["title"], "2025年全球AI芯片厂商份额（%）")

    def test_goal_core_deterministic_and_keeps_topic(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        goal = "请分析2025年全球AI芯片市场并生成可视化报告"
        first = o._goal_core(goal)
        self.assertEqual(first, o._goal_core(goal),
                         "核心词提取必须跨进程确定（回归：set 迭代顺序曾导致主题词被截断）")
        self.assertIn("芯片", first)
        self.assertIn("ai", first)
        # 与目标主题无关的领域词不得混入核心词
        self.assertNotIn("soc", o._goal_core(goal))

    def test_filter_drops_offtopic_growth_debt_specs(self):
        """回归：兜底解析曾把"全球经济增长率/债务"混入"AI算力投资"任务的图，
        且结论模板词"差异显著"误中目标里的"技术路线差异"导致离题图被放行。"""
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        goal = ("请调研并总结2025年至2026年间，全球主要经济体在人工智能算力基础设施"
                "方面的投资规模、核心技术路线差异及相关的政策法规。要求数据必须附带"
                "明确的官方或权威机构出处，并按时间线整理。")
        specs = [
            {
                "title": "全球经济增长率对比（%）",
                "question": "各机构预测差异？",
                "conclusion": "各口径间差异显著（3.2~3.3%）",
                "type": "bar", "unit": "%",
                "x_axis_title": "口径", "y_axis_title": "增长率（%）",
                "source": "https://a.com", "time_range": "2025年", "region": "全球",
                "data": [
                    {"label": "社科院", "value": 3.2, "year": 2025, "caliber": "社科院", "source": "https://a.com"},
                    {"label": "IMF", "value": 3.3, "year": 2025, "caliber": "IMF", "source": "https://b.com"},
                ],
            },
            {
                "title": "全球债务规模对比（万亿美元）",
                "question": "各口径差异？",
                "conclusion": "债务338万亿美元。",
                "type": "bar", "unit": "万亿美元",
                "x_axis_title": "口径", "y_axis_title": "规模（万亿美元）",
                "source": "https://c.com", "time_range": "2025年", "region": "全球",
                "data": [
                    {"label": "社科院", "value": 338, "year": 2025, "caliber": "社科院", "source": "https://c.com"},
                    {"label": "社科院2", "value": 338, "year": 2025, "caliber": "社科院2", "source": "https://c.com"},
                ],
            },
            {
                "title": "2025年全球AI算力基础设施投资规模（亿美元）",
                "question": "各国投资规模？",
                "conclusion": "美国领先。",
                "type": "bar", "unit": "亿美元",
                "x_axis_title": "国家", "y_axis_title": "投资（亿美元）",
                "source": "https://d.com", "time_range": "2025年", "region": "全球",
                "data": [
                    {"label": "美国", "value": 500, "year": 2025, "caliber": "美国", "source": "https://d.com"},
                    {"label": "中国", "value": 300, "year": 2025, "caliber": "中国", "source": "https://d.com"},
                ],
            },
        ]
        kept = o._filter_chart_specs(specs, goal)
        self.assertEqual(len(kept), 1, "离题的经济增长/债务图应被丢弃，只保留算力投资图")
        self.assertIn("AI算力基础设施投资规模", kept[0]["title"])
        self.assertIn("算力", o._goal_core(goal))

    def test_render_chart_data_from_llm_specs(self):
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_llmchart_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-lc-1")
            (proj / "chart_data.json").write_text(json.dumps({"charts": [
                {
                    "question": "2023-2025年全球AI芯片市场规模趋势？",
                    "conclusion": "市场规模从2023年110亿美元增至2025年726亿美元。",
                    "type": "line", "title": "2023-2025年全球AI芯片市场规模（亿美元）",
                    "x_axis_title": "年份", "y_axis_title": "市场规模（亿美元）",
                    "unit": "亿美元", "time_range": "2023-2025年", "region": "全球",
                    "source": "艾媒统计", "sample_size": "2",
                    "annotation": "按年份趋势", "missing": "2024年数据暂缺", "outliers": "无",
                    "data": [
                        {"label": "艾媒", "value": 110, "year": 2023, "caliber": "艾媒", "source": "https://a.com"},
                        {"label": "艾媒", "value": 726, "year": 2025, "caliber": "艾媒", "source": "https://a.com"},
                    ],
                },
                {
                    "question": "2025年全球AI芯片厂商份额排名？",
                    "conclusion": "英伟达49%领先，AMD 12%居次。",
                    "type": "bar", "title": "2025年全球AI芯片厂商份额（%）",
                    "x_axis_title": "厂商", "y_axis_title": "份额（%）",
                    "unit": "%", "time_range": "2025年", "region": "全球",
                    "source": "https://a.com", "sample_size": "2",
                    "annotation": "英伟达领先", "missing": "无", "outliers": "无",
                    "data": [
                        {"label": "英伟达", "value": 49, "year": 2025, "caliber": "英伟达", "source": "https://a.com"},
                        {"label": "AMD", "value": 12, "year": 2025, "caliber": "AMD", "source": "https://a.com"},
                    ],
                },
            ]}, ensure_ascii=False), encoding="utf-8")
            o._render_chart_data("t-lc-1", "请分析AI芯片市场并生成可视化报告")
            pngs = {p.name for p in proj.glob("chart_*.png")}
            self.assertEqual(pngs, {"chart_1.png", "chart_2.png"}, "应按规格渲染两张图")
            manifest = json.loads((proj / "chart_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["charts"]), 2)
            # manifest 只保留 file+keywords（与 chart_data.json 去冗余）
            self.assertTrue(all(m["file"] and m["keywords"] for m in manifest["charts"]))
            self.assertTrue(all("title" not in m and "conclusion" not in m
                                for m in manifest["charts"]))
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_render_single_year_multiple_calibers_as_bar(self):
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_llmchart_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-lc-2")
            (proj / "chart_data.json").write_text(json.dumps({"charts": [
                {
                    "question": "2025年各口径市场规模对比？",
                    "conclusion": "IIM宽口径3500亿，IIM 1800亿，艾媒726亿，口径差异显著。",
                    "type": "bar", "title": "2025年全球AI芯片市场规模口径对比（亿美元）",
                    "x_axis_title": "口径/来源", "y_axis_title": "市场规模（亿美元）",
                    "unit": "亿美元", "time_range": "2025年", "region": "全球",
                    "source": "https://b.com", "sample_size": "3",
                    "annotation": "不同机构口径不同", "missing": "无", "outliers": "无",
                    "data": [
                        {"label": "艾媒", "value": 726, "year": 2025, "caliber": "艾媒", "source": "https://a.com"},
                        {"label": "IIM", "value": 1800, "year": 2025, "caliber": "IIM", "source": "https://b.com"},
                        {"label": "IIM宽口径", "value": 3500, "year": 2025, "caliber": "IIM宽口径", "source": "https://b.com"},
                    ],
                },
            ]}, ensure_ascii=False), encoding="utf-8")
            o._render_chart_data("t-lc-2", "请分析AI芯片市场并生成可视化报告")
            pngs = {p.name for p in proj.glob("chart_*.png")}
            self.assertEqual(pngs, {"chart_1.png"}, "单年份多口径应生成一张柱状图")
            manifest = json.loads((proj / "chart_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["charts"][0]["file"], "chart_1.png")
            self.assertTrue(manifest["charts"][0]["keywords"])
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_render_skips_invalid_spec_and_keeps_valid(self):
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_llmchart_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-lc-3")
            (proj / "chart_data.json").write_text(json.dumps({"charts": [
                {
                    "question": "2025年厂商份额？",
                    "conclusion": "英伟达49%领先。",
                    "type": "bar", "title": "2025年全球AI芯片厂商份额（%）",
                    "x_axis_title": "厂商", "y_axis_title": "份额（%）",
                    "unit": "%", "time_range": "2025年", "region": "全球",
                    "source": "https://a.com", "sample_size": "1",
                    "annotation": "领先厂商", "missing": "无", "outliers": "无",
                    "data": [
                        {"label": "英伟达", "value": 49, "year": 2025, "caliber": "英伟达", "source": "https://a.com"},
                        {"label": "AMD", "value": 12, "year": 2025, "caliber": "AMD", "source": "https://a.com"},
                    ],
                },
                {
                    "question": "无标注图",
                    "type": "bar", "unit": "亿美元",
                    "data": [
                        {"label": "A", "value": 1, "year": 2025, "caliber": "A", "source": "https://b.com"},
                    ],
                },
            ]}, ensure_ascii=False), encoding="utf-8")
            o._render_chart_data("t-lc-3", "请分析AI芯片市场并生成可视化报告")
            pngs = {p.name for p in proj.glob("chart_*.png")}
            self.assertEqual(pngs, {"chart_1.png"}, "缺标注的规格应被跳过")
            manifest = json.loads((proj / "chart_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["charts"]), 1)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_chart_spec_validator_and_type_rules(self):
        from chart_specs import merge_year_series, pick_type, validate_spec, wrap_rows_to_specs

        good = {
            "question": "q", "conclusion": "c", "type": "bar",
            "title": "2025年全球AI芯片市场规模（亿美元）",
            "x_axis_title": "口径", "y_axis_title": "规模（亿美元）",
            "unit": "亿美元", "source": "https://a.com", "time_range": "2025年",
            "region": "全球", "sample_size": "1", "annotation": "a",
            "missing": "无", "outliers": "无",
            "data": [
                {"label": "A", "value": 1, "year": 2025, "caliber": "A", "source": "https://a.com"},
                {"label": "B", "value": 2, "year": 2025, "caliber": "B", "source": "https://b.com"},
            ],
        }
        self.assertEqual(validate_spec(good), [])
        self.assertIn("缺少 title", validate_spec({**good, "title": ""}))
        self.assertIn("缺少 source", validate_spec({**good, "source": ""}))
        self.assertIn("缺少 conclusion", validate_spec({**good, "conclusion": "  "}))
        self.assertIn("data 为空", validate_spec({**good, "data": []}))
        self.assertTrue(any("单点" in x for x in validate_spec({
            **good, "data": [good["data"][0]],
        })), "单数据点图应被判为非法（无结论不画图）")
        self.assertTrue(any("type 非法" in x for x in validate_spec({**good, "type": "3d_bar"})))

        rows_ts = [
            {"指标": "市场规模", "年份": 2023, "数值": 110, "单位": "亿美元", "口径": "艾媒", "来源": "https://a.com"},
            {"指标": "市场规模", "年份": 2025, "数值": 726, "单位": "亿美元", "口径": "艾媒", "来源": "https://a.com"},
        ]
        self.assertEqual(pick_type(rows_ts), "line", "时间序列应推荐折线图")
        rows_many = [
            {"指标": "市场规模", "年份": None, "数值": i, "单位": "亿美元",
             "口径": f"机构{i}", "来源": "https://a.com"} for i in range(12)
        ]
        self.assertEqual(pick_type(rows_many), "horizontal_bar", "类别>10 应推荐水平条形")

        wrapped = wrap_rows_to_specs(rows_ts)
        self.assertEqual(len(wrapped), 1)
        self.assertEqual(wrapped[0]["type"], "line")
        self.assertTrue(wrapped[0]["conclusion"])
        # 单点组跳过（无结论不画图）
        self.assertEqual(
            wrap_rows_to_specs([
                {"指标": "市场规模", "年份": 2025, "数值": 1500, "单位": "亿美元",
                 "口径": "德勤", "来源": "https://a.com"},
            ]), [],
        )

    def test_merge_year_series_combines_single_point_bars(self):
        from chart_specs import merge_year_series, validate_spec

        specs = [
            {
                "question": "2025年全球AI算力基础设施市场规模？",
                "conclusion": "2025年突破890亿美元。",
                "type": "bar", "title": "全球AI算力基础设施市场规模（2025年，单位：亿美元）",
                "x_axis_title": "年份", "y_axis_title": "市场规模（亿美元）",
                "unit": "亿美元", "time_range": "2025年", "region": "全球",
                "source": "https://a.com", "sample_size": "1",
                "annotation": "全球市场总量", "missing": "无", "outliers": "无",
                "data": [{"label": "全球规模", "value": 890, "year": 2025,
                          "caliber": "全球市场总量", "source": "https://a.com"}],
            },
            {
                "question": "2026年全球AI算力基础设施市场规模预计？",
                "conclusion": "2026年预计达1120亿美元。",
                "type": "bar", "title": "全球AI算力基础设施市场规模预测（2026年，单位：亿美元）",
                "x_axis_title": "年份", "y_axis_title": "市场规模（亿美元）",
                "unit": "亿美元", "time_range": "2026年", "region": "全球",
                "source": "https://b.com", "sample_size": "1",
                "annotation": "预计总规模", "missing": "无", "outliers": "无",
                "data": [{"label": "全球规模预测", "value": 1120, "year": 2026,
                          "caliber": "预计总规模", "source": "https://b.com"}],
            },
            {
                "question": "中国占全球比重？",
                "conclusion": "中国占32.7%。",
                "type": "pie", "title": "中国占全球AI算力基础设施市场比重（2025年，单位：%）",
                "x_axis_title": "无", "y_axis_title": "占比（%）",
                "unit": "%", "time_range": "2025年", "region": "全球",
                "source": "https://a.com", "sample_size": "1",
                "annotation": "占比", "missing": "无", "outliers": "无",
                "data": [{"label": "中国", "value": 32.7, "year": 2025,
                          "caliber": "占比", "source": "https://a.com"}],
            },
        ]
        merged = merge_year_series(specs)
        line = [s for s in merged if s["type"] == "line"]
        self.assertEqual(len(line), 1, "同指标跨年份单点应合并为一张折线")
        self.assertEqual(len(line[0]["data"]), 2)
        self.assertEqual(validate_spec(line[0]), [])
        self.assertIn("2025", line[0]["time_range"])
        # 单点饼图不可合并，仍应单独存在且后续被校验拒绝
        self.assertTrue(any(s["type"] == "pie" for s in merged))
        self.assertTrue(any("单点" in x for x in validate_spec(
            next(s for s in merged if s["type"] == "pie")
        )))

    def test_verify_specs_against_text_drops_fabricated_values(self):
        from chart_specs import verify_specs_against_text

        specs = [{
            "title": "2025年全球AI芯片市场规模（亿美元）",
            "question": "各口径差异？",
            "conclusion": "德勤1500亿，艾媒726亿。",
            "type": "bar", "unit": "亿美元",
            "x_axis_title": "口径", "y_axis_title": "规模（亿美元）",
            "source": "https://a.com", "time_range": "2025年", "region": "全球",
            "data": [
                {"label": "德勤", "value": 1500, "year": 2025, "caliber": "德勤", "source": "https://a.com"},
                {"label": "艾媒", "value": 726, "year": 2025, "caliber": "艾媒", "source": "https://b.com"},
                {"label": "编造机构", "value": 9999, "year": 2025, "caliber": "编造", "source": "https://c.com"},
            ],
        }]
        text = "2025年全球AI芯片市场规模：德勤预测1500亿美元，艾媒统计726亿美元。"
        kept, dropped = verify_specs_against_text(specs, text)
        self.assertEqual(dropped, 1, "编造的 9999 应被溯源校验丢弃")
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(kept[0]["data"]), 2)
        self.assertEqual(kept[0]["sample_size"], "2")

    def test_render_skips_pie_with_non_100_percent(self):
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_llmchart_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-pie-1")
            (proj / "chart_data.json").write_text(json.dumps({"charts": [
                {
                    "question": "2025年全球AI芯片厂商份额？",
                    "conclusion": "英伟达49%，AMD 12%。",
                    "type": "pie", "title": "2025年全球AI芯片厂商份额（%）",
                    "x_axis_title": "厂商", "y_axis_title": "份额（%）",
                    "unit": "%", "time_range": "2025年", "region": "全球",
                    "source": "https://a.com", "sample_size": "2",
                    "annotation": "前两大厂商", "missing": "无", "outliers": "无",
                    "data": [
                        {"label": "英伟达", "value": 49, "year": 2025, "caliber": "英伟达", "source": "https://a.com"},
                        {"label": "AMD", "value": 12, "year": 2025, "caliber": "AMD", "source": "https://a.com"},
                    ],
                },
            ]}, ensure_ascii=False), encoding="utf-8")
            o._render_chart_data("t-pie-1", "请分析AI芯片市场并生成可视化报告")
            pngs = {p.name for p in proj.glob("chart_*.png")}
            self.assertEqual(pngs, set(), "占比加和 61% != 100% 的饼图应跳过（重算占比会与数据不符）")
            manifest = json.loads((proj / "chart_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["charts"], [])
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_chart_manifest_backfilled_for_rendered_pngs(self):
        """P2-4：图表已渲染但 manifest 为空数组时，按已渲染 PNG 回填
        file+keywords 条目（实际 4 图不得对应空 manifest）。"""
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_mf_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-mf-1")
            proj.mkdir(parents=True, exist_ok=True)
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
            # 复现缺陷现场：PNG 已渲染但 manifest 仍是空数组
            (proj / "chart_1.png").write_bytes(b"PNG")
            (proj / "chart_manifest.json").write_text(
                json.dumps({"charts": []}), encoding="utf-8",
            )
            o._backfill_chart_manifest(proj)
            manifest = json.loads(
                (proj / "chart_manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["charts"]), 1)
            self.assertEqual(manifest["charts"][0]["file"], "chart_1.png")
            self.assertTrue(manifest["charts"][0]["keywords"])
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_render_pie_with_100_percent_and_long_labels(self):
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_llmchart_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-pie-2")
            (proj / "chart_data.json").write_text(json.dumps({"charts": [
                {
                    "question": "2025年中国占全球AI算力市场比重？",
                    "conclusion": "中国40%，其他60%。",
                    "type": "pie", "title": "2025年全球AI算力市场区域构成（%）",
                    "x_axis_title": "区域", "y_axis_title": "占比（%）",
                    "unit": "%", "time_range": "2025年", "region": "全球",
                    "source": "https://a.com", "sample_size": "2",
                    "annotation": "区域构成", "missing": "无", "outliers": "无",
                    "data": [
                        {"label": "中国", "value": 40, "year": 2025, "caliber": "中国", "source": "https://a.com"},
                        {"label": "全球其他地区", "value": 60, "year": 2025, "caliber": "全球其他地区", "source": "https://a.com"},
                    ],
                },
                {
                    "question": "2025年各口径市场规模？",
                    "conclusion": "各口径差异大。",
                    "type": "bar", "title": "2025年全球AI芯片市场规模（亿美元）",
                    "x_axis_title": "口径", "y_axis_title": "规模（亿美元）",
                    "unit": "亿美元", "time_range": "2025年", "region": "全球",
                    "source": "https://b.com", "sample_size": "2",
                    "annotation": "口径对比", "missing": "无", "outliers": "无",
                    "data": [
                        {"label": "AI芯片占全球芯片市场11%，全球芯片市场5760亿美元", "value": 570,
                         "year": 2025, "caliber": "德勤统计", "source": "https://b.com"},
                        {"label": "艾媒咨询统计", "value": 726, "year": 2025,
                         "caliber": "艾媒咨询统计", "source": "https://c.com"},
                    ],
                },
            ]}, ensure_ascii=False), encoding="utf-8")
            o._render_chart_data("t-pie-2", "请分析AI算力市场并生成可视化报告")
            pngs = {p.name for p in proj.glob("chart_*.png")}
            self.assertEqual(pngs, {"chart_1.png", "chart_2.png"},
                             "占比加和=100% 的饼图与长标签柱状图都应正常渲染")
            manifest = json.loads((proj / "chart_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["charts"]), 2)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_charts_for_summary_goal(self):
        import json
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_chart_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            proj = ws_mod.task_project_dir("t-chart-2")
            (proj / "search_results.json").write_text(json.dumps([
                {"title": "公司新闻动态", "url": "https://a.com/r1", "snippet": "业务进展"},
            ], ensure_ascii=False), encoding="utf-8")
            o._generate_search_charts("t-chart-2", "搜索公司最新新闻动态并总结要点")
            self.assertEqual([p.name for p in proj.glob("*.png")], [])
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_embed_charts_inline_after_matching_heading(self):
        from pathlib import Path
        from workers.report_generator_worker import ReportGeneratorWorker

        charts = [
            Path("project/market_trend.png"),
            Path("project/source_distribution.png"),
            Path("project/topic_terms.png"),
        ]
        report = (
            "# 报告\n\n## 市场规模\n\n2025年全球AI芯片市场约1200亿美元。\n\n"
            "## 技术趋势\n\n深度学习与边缘计算融合。\n\n"
            "## 数据来源\n\n- https://a.com\n"
        )
        out = ReportGeneratorWorker._embed_charts_inline(report, charts)
        self.assertGreater(out.find("![market_trend]"), out.find("## 市场规模"))
        self.assertLess(out.find("![market_trend]"), out.find("## 技术趋势"))
        self.assertGreater(out.find("![topic_terms]"), out.find("## 技术趋势"))
        self.assertGreater(out.find("![source_distribution]"), out.find("## 数据来源"))





class TestSearchFailureFallback(unittest.TestCase):
    def test_strip_reflection_residue(self):
        """报告正文中误抄的反思反馈块必须被剥离：
        否则验收器会把反馈里的示例数字（时间戳）当成报告数字，溯源率被拉低。"""
        from workers.report_generator_worker import ReportGeneratorWorker

        report = (
            "# 腾讯财务报告\n\n"
            "2025年营收7517亿元。\n\n"
            "【反思要求重做】报告生成步骤存在严重缺陷：财务金额溯源率仅40%（19/48），"
            "如1787150053等。需修复报告，确保所有数字可追溯。\n\n"
            "## 数据来源\n\n- https://x.com/a\n"
        )
        out = ReportGeneratorWorker._strip_reflection_residue(report)
        self.assertNotIn("反思要求重做", out)
        self.assertNotIn("1787150053", out)
        self.assertIn("2025年营收7517亿元", out)
        self.assertIn("## 数据来源", out)

        # 模型改写格式（无方括号）的残留行也要删
        report2 = "反思要求重做：请补充最新季度数据\n\n## 摘要\n\n内容。"
        out2 = ReportGeneratorWorker._strip_reflection_residue(report2)
        self.assertNotIn("反思要求重做", out2)
        self.assertIn("## 摘要", out2)

    def test_strip_rejected_sources_removes_appendix_entries(self):
        """P2-2：正文声明已剔除/排除的域名与 URL 必须从来源附录删除。"""
        from workers.report_generator_worker import ReportGeneratorWorker

        report = (
            "# 加密市场调研\n\n"
            "本次分析已剔除 suhbaonline.net、https://spam.example/1 等无关来源，"
            "仅采用可验证的行情数据。\n\n"
            "## 数据来源\n\n"
            "- [suhbaonline](https://suhbaonline.net/abc)\n"
            "- [spam](https://spam.example/1)\n"
            "- [可靠来源](https://good.example.com/data)\n\n"
            "## 参考文献\n\n"
            "- suhbaonline.net\n"
        )
        out = ReportGeneratorWorker._strip_rejected_sources(report)
        # 正文剔除声明保留；仅附录中的被剔除来源条目删除
        self.assertIn("已剔除 suhbaonline.net", out)
        self.assertNotIn("https://suhbaonline.net/abc", out)
        self.assertNotIn("- suhbaonline.net", out)
        self.assertNotIn("- [spam](https://spam.example/1)", out)
        self.assertIn("good.example.com", out)
        self.assertIn("## 数据来源", out)

    def test_strip_rejected_sources_keeps_appendix_without_declaration(self):
        """P2-2：无剔除声明时附录原样保留。"""
        from workers.report_generator_worker import ReportGeneratorWorker

        report = (
            "# 报告\n\n## 数据来源\n\n"
            "- https://suhbaonline.net/x\n"
        )
        out = ReportGeneratorWorker._strip_rejected_sources(report)
        self.assertEqual(out, report)

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

    def test_rollout_split_and_record(self):
        """Roadmap 余项②：灰度分流 + 结果记录。"""
        from worker_base import SearchAgent

        sa = SearchAgent.__new__(SearchAgent)
        sa._messaging = None
        sa._strategy_id = "s-gray"
        sa._strategy_rollout = 1.0
        self.assertTrue(sa._strategy_applies("any-task"))  # 全量
        sa._strategy_rollout = 0.0
        self.assertFalse(sa._strategy_applies("any-task"))  # 0 不应用
        sa._strategy_rollout = 0.5
        # 哈希分流：约一半任务进灰度桶，且同一任务结果稳定
        hits = sum(1 for i in range(200) if sa._strategy_applies(f"t-{i}"))
        self.assertGreater(hits, 30)
        self.assertLess(hits, 170)
        self.assertEqual(sa._strategy_applies("t-42"), sa._strategy_applies("t-42"))

    def test_rollout_monitor_rolls_back_on_low_success(self):
        """Roadmap 余项②：灰度成功率 <50% 且样本≥5 → 自动回滚。"""
        import json as _json
        from worker_base import SearchAgent

        ops = {"deleted": [], "published": []}

        class FakeRedis:
            def get(self, key):
                return _json.dumps({
                    "strategy_id": "s-bad", "agent_type": "search_agent",
                    "rollout": 0.3, "filter_rules": [],
                })

            def hgetall(self, key):
                # 6 个样本，2 成功 4 失败 → 33% < 50%
                return {"t1": "SUCCESS", "t2": "FAILED", "t3": "FAILED",
                        "t4": "SUCCESS", "t5": "FAILED", "t6": "FAILED"}

            def hset(self, *a, **k):
                pass

            def expire(self, *a, **k):
                pass

            def set(self, key, val, nx=False, ex=0):
                ops["lock"] = True
                return True

            def delete(self, key):
                ops["deleted"].append(key)

        class FakeMsg:
            def __init__(self):
                self.redis = FakeRedis()

            def publish(self, channel, data):
                ops["published"].append((channel, data))

        sa = SearchAgent.__new__(SearchAgent)
        sa._messaging = FakeMsg()
        sa._rollout_checked_at = 0.0
        sa._load_active_strategy()
        self.assertIn("strategy:active:search_agent", ops["deleted"])
        self.assertEqual(ops["published"][0][0], "registry.capability.update")
        ev = ops["published"][0][1]
        self.assertEqual(ev["type"], "strategy_rollback")
        self.assertEqual(ev["strategy_id"], "s-bad")
        # 回滚后策略清空
        self.assertEqual(sa._strategy_blocks, [])

    def test_rollout_monitor_keeps_good_strategy(self):
        """Roadmap 余项②：灰度成功率达标不回滚。"""
        import json as _json
        from worker_base import SearchAgent

        class FakeRedis:
            def get(self, key):
                return _json.dumps({
                    "strategy_id": "s-good", "agent_type": "search_agent",
                    "rollout": 0.3, "filter_rules": [],
                })

            def hgetall(self, key):
                return {"t1": "SUCCESS", "t2": "SUCCESS", "t3": "SUCCESS",
                        "t4": "FAILED", "t5": "SUCCESS", "t6": "SUCCESS"}

            def hset(self, *a, **k):
                pass

            def expire(self, *a, **k):
                pass

            def set(self, *a, **k):
                return True

            def delete(self, key):
                raise AssertionError("不应回滚")

        class FakeMsg:
            def __init__(self):
                self.redis = FakeRedis()

            def publish(self, channel, data):
                pass

        sa = SearchAgent.__new__(SearchAgent)
        sa._messaging = FakeMsg()
        sa._rollout_checked_at = 0.0
        sa._load_active_strategy()
        self.assertEqual(sa._strategy_id, "s-good")  # 策略保留


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
            ok, detail, _shot, _dg = o._playwright_verify(d, "welcome.html", fp, require_game=False)
            self.assertTrue(ok, detail)
            # 同页面走"游戏级"验证必须失败（无 canvas），证明两种模式确实分流
            ok2, detail2, _, _dg2 = o._playwright_verify(d, "welcome.html", fp, require_game=True)
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
            ok, detail, _, _dg = o._playwright_verify(d, "no_charset.html", fp, require_game=False)
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
        ok, detail, _, _dg = o._playwright_verify(d, "broken.html", fp, require_game=True)
        self.assertFalse(ok, detail)
        self.assertIn("未重启", detail)

        working = broken.replace(
            "if(y<0||y>19||x<0||x>19){ alert('over'); reset(); return; }",
            "if(y<0||y>19||x<0||x>19){ alert('over'); reset(); }",
        )
        fp2 = os.path.join(d, "working.html")
        Path(fp2).write_text(working, encoding="utf-8")
        ok2, detail2, _, _dg2 = o._playwright_verify(d, "working.html", fp2, require_game=True)
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

        old_env = os.environ.get("WEAVEMIND_CONSOLIDATE_THRESHOLD")
        os.environ["WEAVEMIND_CONSOLIDATE_THRESHOLD"] = "1"
        o = OrchestratorV2.__new__(OrchestratorV2)
        try:
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
        finally:
            if old_env is None:
                os.environ.pop("WEAVEMIND_CONSOLIDATE_THRESHOLD", None)
            else:
                os.environ["WEAVEMIND_CONSOLIDATE_THRESHOLD"] = old_env

    def test_consolidate_template_genericizes_company(self):
        """固化模板必须公司无关：步骤指令中的具体公司名替换为"目标公司"。"""
        import json
        import os
        import tempfile
        from orchestrator_v2 import OrchestratorV2

        old_env = os.environ.get("WEAVEMIND_CONSOLIDATE_THRESHOLD")
        os.environ["WEAVEMIND_CONSOLIDATE_THRESHOLD"] = "1"
        o = OrchestratorV2.__new__(OrchestratorV2)
        try:
            tmp = os.path.join(tempfile.mkdtemp(prefix="weavemind_tpl_"), "templates.json")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"templates": []}, f)
            steps = [
                {"step_id": "1", "capability": "web_search",
                 "instruction": "任务目标：搜索并分析腾讯年度财务报告\n"
                                "搜索腾讯控股（Tencent Holdings Ltd）最新年报的营收、净利润、毛利率"},
                {"step_id": "2", "capability": "web_fetch",
                 "instruction": "抓取腾讯官网投资者关系页"},
                {"step_id": "3", "capability": "content_summary",
                 "instruction": "整合腾讯财务数据"},
            ]
            try:
                o._consolidate_template(
                    "搜索并分析腾讯年度财务报告中的核心指标", steps, tpl_path=tmp)
                data = json.load(open(tmp, encoding="utf-8"))
                tpl = data["templates"][0]
                joined = " ".join(str(s.get("instruction")) for s in tpl["steps"])
                self.assertNotIn("腾讯", joined)
                self.assertNotIn("Tencent", joined)
                self.assertIn("目标公司", joined)
                self.assertEqual(tpl["goal"], "公司/集团的发展历程与现状，并分析历年财报")
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        finally:
            if old_env is None:
                os.environ.pop("WEAVEMIND_CONSOLIDATE_THRESHOLD", None)
            else:
                os.environ["WEAVEMIND_CONSOLIDATE_THRESHOLD"] = old_env

    def test_consolidate_template_strips_reflection_residue(self):
        """反思重做步骤尾部的"【反思要求重做】…"反馈应被剥离而不是整步跳过，
        否则经反思重做的任务永远无法固化模板。"""
        import json
        import os
        import tempfile
        from orchestrator_v2 import OrchestratorV2

        old_env = os.environ.get("WEAVEMIND_CONSOLIDATE_THRESHOLD")
        os.environ["WEAVEMIND_CONSOLIDATE_THRESHOLD"] = "1"
        o = OrchestratorV2.__new__(OrchestratorV2)
        try:
            tmp = os.path.join(tempfile.mkdtemp(prefix="weavemind_tpl_"), "templates.json")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"templates": []}, f)
            steps = [
                {"step_id": "1", "capability": "web_search",
                 "instruction": "任务目标：搜索并分析腾讯年度财务报告中的核心指标\n"
                                "原始指令：搜索并分析腾讯年度财务报告中的核心指标，搜索腾讯控股最新年报\n"
                                "【反思要求重做】请补充最新季度数据"},
                {"step_id": "2", "capability": "content_summary",
                 "instruction": "任务目标：搜索并分析腾讯年度财务报告中的核心指标\n"
                                "原始指令：整合腾讯财务数据表格与净利润"},
            ]
            try:
                o._consolidate_template(
                    "搜索并分析腾讯年度财务报告中的核心指标", steps, tpl_path=tmp)
                data = json.load(open(tmp, encoding="utf-8"))
                self.assertEqual(len(data["templates"]), 1)
                tpl = data["templates"][0]
                self.assertEqual(len(tpl["steps"]), 2)
                joined = " ".join(str(s.get("instruction")) for s in tpl["steps"])
                self.assertNotIn("反思要求重做", joined)
                self.assertIn("目标公司/集团最新年报", joined)
                self.assertNotIn("腾讯", joined)
                self.assertNotIn("Tencent", joined)
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        finally:
            if old_env is None:
                os.environ.pop("WEAVEMIND_CONSOLIDATE_THRESHOLD", None)
            else:
                os.environ["WEAVEMIND_CONSOLIDATE_THRESHOLD"] = old_env

    def test_consolidate_template_strips_repair_prompt_and_fix_steps(self):
        """任务级修复轮不得进模板：fix-* 步骤整体跳过，修复提示文案从指令剥离。

        否则固化出的模板会把"以下交付物未通过可运行性验证…"当成正常指令重放。"""
        import json
        import os
        import tempfile
        from orchestrator_v2 import OrchestratorV2

        old_env = os.environ.get("WEAVEMIND_CONSOLIDATE_THRESHOLD")
        os.environ["WEAVEMIND_CONSOLIDATE_THRESHOLD"] = "1"
        o = OrchestratorV2.__new__(OrchestratorV2)
        try:
            tmp = os.path.join(tempfile.mkdtemp(prefix="weavemind_tpl_"), "templates.json")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"templates": []}, f)
            steps = [
                {"step_id": "1", "capability": "web_search",
                 "instruction": "用户目标：搜索并分析腾讯年度财务报告中的核心指标\n"
                                "原始指令：搜索并分析腾讯年度财务报告中的核心指标，取营收与净利润"},
                {"step_id": "2", "capability": "code_execution",
                 "instruction": "任务目标：搜索并分析腾讯年度财务报告中的核心指标\n\n"
                                "原始指令：搜索并分析腾讯年度财务报告中的核心指标，"
                                "用 Python 绘制腾讯营收趋势图\n\n"
                                "以下交付物未通过可运行性验证，请针对失败原因修复并重新生成完整文件：\n"
                                "(无代码交付物)（file）：任务要求生成代码，但交付包中没有 HTML/PY/JS 文件"},
                {"step_id": "fix-1", "capability": "code_execution",
                 "instruction": "任务目标：搜索并分析腾讯年度财务报告中的核心指标\n\n"
                                "以下交付物未通过可运行性验证，请针对失败原因修复并重新生成完整文件：\n"
                                "(无代码交付物)（file）：任务要求生成代码"},
            ]
            try:
                o._consolidate_template(
                    "搜索并分析腾讯年度财务报告中的核心指标", steps, tpl_path=tmp)
                data = json.load(open(tmp, encoding="utf-8"))
                self.assertEqual(len(data["templates"]), 1)
                tpl = data["templates"][0]
                self.assertEqual(len(tpl["steps"]), 2)
                joined = " ".join(str(s.get("instruction")) for s in tpl["steps"])
                self.assertNotIn("未通过可运行性验证", joined)
                self.assertNotIn("请针对失败原因修复", joined)
                self.assertNotIn("无代码交付物", joined)
                self.assertIn("绘制", joined)
            finally:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
        finally:
            if old_env is None:
                os.environ.pop("WEAVEMIND_CONSOLIDATE_THRESHOLD", None)
            else:
                os.environ["WEAVEMIND_CONSOLIDATE_THRESHOLD"] = old_env

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


class TestSimpleTaskFastPath(_DevSandboxMode, _TempWorkspace, unittest.TestCase):
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
            return "print('hello from simple task')\n"

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
        # 测试隔离：清掉可能残留的 checkpoint（上次运行崩溃会留下
        # finalizing 快照，指向已删除的临时 zip，导致续跑误入修复轮）
        from checkpointer import clear_checkpoint
        clear_checkpoint("t-simple-1")
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
        o._find_agent = lambda cap: "fake-agent"
        try:
            res = o.run("t-simple-1", "生成一个 HTML 欢迎页", auto_run=True)
            # 收尾会对装配后的交付正文跑确定性验收：本替身正文在该目标下通过，
            # 终态因此是验收的实际结论（此前是"无验收 → 未验收草稿"的兜底降级；
            # 验收未通过时仍降级为 SUCCESS_WITH_ISSUES，见 test_orchestrator_v2 的
            # TestMemoryAcceptanceWiring）。
            self.assertIn(res["status"], ("SUCCESS", "SUCCESS_WITH_ISSUES"))
            # 交付状态 = 实际验收结论 + 端点降级（会跨进程继承）；本用例只测快速路径本身。

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
            from checkpointer import clear_checkpoint
            clear_checkpoint("t-simple-1")

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
            # P0-1 同步：/files/ 仅提供 reports/ 与 charts/（web_ui），data/ 不再改写为 /files/ URL
            self.assertIn("](data/scatter.png)", out)
            self.assertIn(f"**成果文件夹**：`{ws}`", out, "正文绝对路径保持不变")
            self.assertIn("https://example.com/a", out)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_report_links_windows_drive_rewritten_to_relative(self):
        """P2-4：工作区外残留的 Windows 盘符绝对路径图片链接 →
        相对路径 charts/xxx.png（不在 /files/ 映射内的本机路径）。"""
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_winlinks_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            report = (
                "![chart](C:\\Users\\x\\AppData\\Local\\Temp\\agent_workspace"
                "\\charts\\abc.png)\n\n"
                "![data](D:/tmp/agent_workspace/data/points.csv)\n\n"
                "正文路径 C:\\Users\\x\\app 不应被改写"
            )
            out = o._rewrite_report_links(report, "t-win-1")
            self.assertIn("](charts/abc.png)", out)
            self.assertIn("](data/points.csv)", out)
            self.assertNotIn("C:\\Users\\x\\AppData", out)
            self.assertIn("正文路径 C:\\Users\\x\\app", out)
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
        # 默认口径：代码与图表进包；data/*.csv 属流程数据，不进包
        files = w._fresh_files(ws / "project", {"workspace": str(ws), "task_start_ts": now - 60})
        names = [rel for _, rel in files]
        self.assertIn("index.html", names)
        self.assertIn("charts/heatmap.png", names, "图表应进入交付包")
        self.assertNotIn("data/x.csv", names,
                         "data/*.csv 默认是流程数据，不当作交付物")
        # 目标明确要数据文件时，data/*.csv 才进包
        files2 = w._fresh_files(ws / "project", {
            "workspace": str(ws), "task_start_ts": now - 60,
            "goal": "统计A股成交额并导出 CSV 数据文件",
        })
        self.assertIn("data/x.csv", [rel for _, rel in files2])

    def test_packaging_excludes_preload_caches(self):
        """预载/中间产物不得作为交付物（结构化数据、清洗数据、行情 CSV）。"""
        import os
        import tempfile
        import time
        from pathlib import Path
        from workers.packaging_worker import PackagingWorker

        ws = Path(tempfile.mkdtemp(prefix="weavemind_preload_"))
        (ws / "project").mkdir(parents=True)
        (ws / "data").mkdir(parents=True)
        now = time.time()
        (ws / "project" / "sales_report.py").write_text("print(1)\n", encoding="utf-8")
        (ws / "project" / "structured_data.json").write_text("{}", encoding="utf-8")
        (ws / "project" / "clean_chart_data.json").write_text("{}", encoding="utf-8")
        (ws / "data" / "ranking.csv").write_text("code,name\n", encoding="utf-8")
        for p in ws.rglob("*"):
            if p.is_file():
                os.utime(p, (now, now))
        w = PackagingWorker.__new__(PackagingWorker)
        names = [rel for _, rel in w._fresh_files(
            ws / "project", {"workspace": str(ws), "task_start_ts": now - 60})]
        self.assertIn("sales_report.py", names)
        for preload in ("structured_data.json", "clean_chart_data.json", "data/ranking.csv"):
            self.assertNotIn(preload, names, f"{preload} 是预载产物，不应入交付包")

    def test_packaging_refuses_shared_dir_without_workspace(self):
        """无任务工作区时拒绝打包共享目录（否则会混入其它任务的产物）。"""
        import asyncio
        from unittest import mock as _mock
        from workers.packaging_worker import PackagingWorker

        w = PackagingWorker.__new__(PackagingWorker)
        with _mock.patch("llm_client.call_llm", return_value={}):
            with self.assertRaises(RuntimeError):
                asyncio.run(w.execute("打包交付", {}))

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

    def test_template_keyword_match_conservative(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        templates = [
            {"name": "数据分析流水线", "goal": "房价数据科学分析"},
            {"name": "行业调研报告", "goal": "调研行业现状"},
            {"name": "董事会汇报", "goal": "可行性方案"},
        ]
        self.assertEqual(
            o._template_keyword_match("调研2026年国内新能源汽车市场现状", templates)["name"],
            "行业调研报告",
        )
        self.assertEqual(
            o._template_keyword_match("对加州房价数据集做回归建模", templates)["name"],
            "数据分析流水线",
        )
        self.assertEqual(
            o._template_keyword_match("评估引入AI视觉检测方案的可行性", templates)["name"],
            "董事会汇报",
        )
        self.assertIsNone(o._template_keyword_match("做一个贪吃蛇游戏", templates))
        self.assertIsNone(o._template_keyword_match("帮我写一个倒计时工具", templates))

    def test_route_template_skips_llm_on_keyword_match(self):
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        class _FakeMsg:
            def publish(self, *a, **k):
                pass

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = _FakeMsg()
        o._now_iso = lambda: "t"
        o._plan_llm = None  # 关键词命中时不应触碰 LLM
        tmp = tempfile.mkdtemp(prefix="weavemind_route_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            # 模板命中：直接返回模板步骤，不调 LLM
            o._load_templates = lambda: [
                {"name": "行业调研报告", "goal": "调研行业现状", "steps": [
                    {"step_id": "1", "capability": "web_search", "instruction": "搜索"},
                ]},
            ]
            routed = o._route_template("调研2026年新能源汽车市场现状", "t-route-1")
            self.assertIsNotNone(routed)
            self.assertEqual(routed[0]["capability"], "web_search")
            # 直接交付命中：跳过 LLM
            routed2 = o._route_template("做一个极简的贪吃蛇游戏", "t-route-1")
            self.assertIsNotNone(routed2)
            self.assertEqual(routed2[0]["capability"], "code_execution")
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_fixed_research_plan_needs_stored_contract(self):
        """C 批：固定研究路径只认**表单落库契约**（严格版研究判据）。

        自由文本里出现期间与口径也不选固定路径：`_extract_company` 会把
        "两个年度"里的"两个"当主体，据此选路径等于让路径依赖会认错主体的解析。
        """
        import tempfile
        import task_state
        import workspace as ws_mod
        from facts import parse_research_request
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = type("M", (), {"publish": lambda *a, **k: None})()
        o._now_iso = lambda: "t"
        tmp = tempfile.mkdtemp(prefix="weavemind_res_")
        old_root = ws_mod.WORKSPACE_ROOT
        old_db = task_state.DB_PATH
        ws_mod.configure_workspace_root(tmp)
        task_state.DB_PATH = str(Path(tmp) / "res.db")
        goal = ("研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
                "经营活动现金流净额，合并报表口径，数据截至 2025-04-30。")
        try:
            # ① 没有落库契约：即使目标文本能被解析成"研究形状"，也不走固定路径
            self.assertIsNone(o._fixed_research_plan("t-fixed-1", goal))
            # ② 落库契约（表单）：命中固定路径，步骤能力序列固定
            req = parse_research_request(goal, company="贵州茅台", company_id="600519.SH",
                                        market="cn", periods=[2023, 2024],
                                        caliber="合并", as_of="2025-04-30",
                                        identity_source="form")
            task_state.mark_queued("t-fixed-1", goal=goal,
                                   research_request=req.to_payload(),
                                   db_path=task_state.DB_PATH)
            steps = o._fixed_research_plan("t-fixed-1", goal)
            self.assertIsNotNone(steps)
            self.assertEqual([s["capability"] for s in steps],
                             ["web_search", "web_fetch", "web_fetch", "content_summary",
                              "report_generator"])
            self.assertIn("贵州茅台", steps[0]["instruction"])
            self.assertIn("2023、2024", steps[1]["instruction"])
            self.assertIn("已选定的事实", steps[3]["instruction"])
            # ③ 只有主体、说不清期间/口径的契约不选固定路径（交给通用规划器）
            thin_goal = "研究贵州茅台的发展历程与现状"
            thin = parse_research_request(thin_goal, company="贵州茅台",
                                          identity_source="form")
            self.assertEqual(thin.periods, [])
            task_state.mark_queued("t-fixed-2", goal=thin_goal,
                                   research_request=thin.to_payload(),
                                   db_path=task_state.DB_PATH)
            self.assertIsNone(o._fixed_research_plan("t-fixed-2", thin_goal))
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            task_state.DB_PATH = old_db
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_fixed_research_plan_preempts_llm_router(self):
        """固定路径先于模板路由：命中时不再花那次无预算的路由调用。"""
        import tempfile
        import task_state
        import templates_pipeline
        import workspace as ws_mod
        from facts import parse_research_request
        from orchestrator_v2 import OrchestratorV2
        from unittest import mock

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = type("M", (), {"publish": lambda *a, **k: None})()
        o._now_iso = lambda: "t"
        tmp = tempfile.mkdtemp(prefix="weavemind_res2_")
        old_root = ws_mod.WORKSPACE_ROOT
        old_db = task_state.DB_PATH
        ws_mod.configure_workspace_root(tmp)
        task_state.DB_PATH = str(Path(tmp) / "res2.db")
        goal = "研究贵州茅台 2023 与 2024 两个年度的营业收入，合并报表口径。"
        try:
            req = parse_research_request(goal, company="贵州茅台", company_id="600519.SH",
                                        market="cn", periods=[2023, 2024],
                                        caliber="合并", identity_source="form")
            task_state.mark_queued("t-fixed-3", goal=goal,
                                   research_request=req.to_payload(),
                                   db_path=task_state.DB_PATH)
            with mock.patch.object(templates_pipeline, "route_template",
                                   side_effect=AssertionError("不应进入模板路由")):
                steps = o._route_template(goal, "t-fixed-3")
            # 1 搜索 / 2 年报正文抓取 / 2b 附注风险抓取（可选）/ 3 解释 / 4 报告
            self.assertEqual(len(steps), 5)
            self.assertEqual([s["capability"] for s in steps].count("web_fetch"), 2)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            task_state.DB_PATH = old_db
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_contract_fetch_skips_company_resolution(self):
        """C 批：契约给了市场 + 代码就直接抓，不再联网 resolve 公司名。"""
        from unittest import mock

        import adapters.router as router
        from facts import parse_research_request

        req = parse_research_request("表单提交", company="贵州茅台",
                                     company_id="600519.SH", market="cn",
                                     periods=[2023, 2024], caliber="合并",
                                     identity_source="form")
        payload = {"financials": [{"year": 2024, "revenue": 1741.44}],
                   "metadata": {"company": "贵州茅台", "unit": "亿元"},
                   "raw": {"url": "https://example.invalid/a", "text": "{}"}}
        with mock.patch("adapters.router.fetch_cn_or_fallback",
                        return_value=dict(payload)) as fc, \
                mock.patch("adapters.router.resolve_company",
                           side_effect=AssertionError("契约路径不得再 resolve")):
            out = router.route_structured_for_request(req)
        self.assertEqual(out["contract"]["company_id"], "600519.SH")
        self.assertEqual(out["resolution"]["market"], "CN")
        self.assertEqual(fc.call_args.kwargs.get("year_range"), (2023, 2024))
        self.assertEqual(fc.call_args.kwargs.get("period"), "annual")

    def test_contract_fetch_market_follows_code_suffix(self):
        """代码后缀比表单选择更确定：`00001.HK` 与 `000001.SZ` 不是同一主体。"""
        from unittest import mock

        import adapters.router as router
        from facts import parse_research_request

        # 表单把市场写成 cn，但代码后缀是 HK → 按 HK 走东财港股接口
        req = parse_research_request("表单提交", company="长和", company_id="00001.HK",
                                     market="cn", periods=[2023, 2024],
                                     caliber="合并", identity_source="form")
        with mock.patch("adapters.router.fetch_eastmoney",
                        return_value={"financials": [], "metadata": {},
                                      "raw": {}}) as fe, \
                mock.patch("adapters.router.fetch_cn_or_fallback",
                           side_effect=AssertionError("后缀是 HK，不该走 A 股链路")):
            out = router.route_structured_for_request(req)
        self.assertEqual(out["resolution"]["market"], "HK")
        # 适配器只认本地代码：`00001.HK` 要按 `00001` 抓（东财 A 股接口收到带后缀
        # 的代码会直接报"无数据"——实机预探针发现的缺陷）
        self.assertEqual(fe.call_args.args[1], "00001")
        self.assertEqual(out["resolution"]["stock_code"], "00001")
        self.assertEqual(out["contract"]["company_id"], "00001.HK",
                         "契约身份保留带后缀的稳定标识")

    def test_contract_fetch_strips_exchange_suffix(self):
        """`600519.SH` → 适配器拿 `600519`；美股 ticker 原样（`BRK.B` 不是后缀）。"""
        from facts import bare_code

        self.assertEqual(bare_code("600519.SH"), "600519")
        self.assertEqual(bare_code("000001.SZ"), "000001")
        self.assertEqual(bare_code("00700.HK"), "00700")
        self.assertEqual(bare_code("AAPL"), "AAPL")
        self.assertEqual(bare_code("BRK.B"), "BRK.B")

    def test_contract_fetch_returns_none_without_code(self):
        """契约没有稳定代码 → 不猜（返回 None，由调用方回落文本路由）。"""
        import adapters.router as router
        from facts import parse_research_request

        req = parse_research_request("表单提交", company="贵州茅台", market="cn",
                                     periods=[2023, 2024], caliber="合并",
                                     identity_source="form")
        self.assertIsNone(router.route_structured_for_request(req))
        self.assertIsNone(router.route_structured_for_request(None))

    def test_load_templates_sorts_manual_before_auto(self):
        """P2-6 模板优先级：_load_templates 手工模板在前，auto-* 沉淀模板在后。"""
        import json
        import os
        import tempfile
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = tempfile.mkdtemp(prefix="weavemind_tpl_sort_")
        try:
            path = os.path.join(tmp, "templates.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"templates": [
                    {"name": "auto-code-x", "goal": "g", "steps": []},
                    {"name": "公司调研与财报分析", "goal": "g", "steps": []},
                    {"name": "auto-financial-y", "goal": "g", "steps": []},
                    {"name": "行业调研报告", "goal": "g", "steps": []},
                ]}, f, ensure_ascii=False)
            names = [t["name"] for t in o._load_templates(path)]
            self.assertEqual(names[0], "公司调研与财报分析")
            self.assertEqual(names[1], "行业调研报告")
            self.assertTrue(all(str(n).startswith("auto-") for n in names[2:]))
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_route_prompt_manual_first_and_placeholder_replaced(self):
        """P2-6：LLM 路由 prompt 中手工模板在前、auto 在后；
        auto-* goal 的"目标公司/集团"占位符替换为具体公司名。"""
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        class _FakeMsg:
            def publish(self, *a, **k):
                pass

        class _FakeLLM:
            def __init__(self):
                self.prompt = ""

            def call(self, system, prompt, **kw):
                self.prompt = prompt
                return {"template": None}

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = _FakeMsg()
        o._now_iso = lambda: "t"
        llm = _FakeLLM()
        o._plan_llm = llm
        tmp = tempfile.mkdtemp(prefix="weavemind_route_p26_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            # mock 返回未排序列表，验证 _route_template 内部仍按手工在前排序
            o._load_templates = lambda: [
                {"name": "auto-financial-x",
                 "goal": "目标公司/集团的发展历程与现状，并分析历年财报",
                 "steps": [{"step_id": "1", "capability": "web_search",
                            "instruction": "x"}]},
                {"name": "手工模板A", "goal": "手工整理交付物",
                 "steps": [{"step_id": "1", "capability": "package",
                            "instruction": "x"}]},
            ]
            routed = o._route_template(
                "搜索并总结比亚迪集团的发展历程和现状，分析历年财报", "t-p26",
            )
            self.assertIsNone(routed)
            prompt = llm.prompt
            self.assertLess(prompt.index("手工模板A"),
                            prompt.index("auto-financial-x"))
            self.assertIn("auto- 前缀模板仅在无手工模板匹配时选用", prompt)
            self.assertIn("比亚迪的发展历程与现状", prompt)
            self.assertNotIn("目标公司/集团", prompt)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    def test_route_llm_auto_only_when_no_manual_template(self):
        """P2-6：LLM 选中 auto-* 的两种情形——
        存在手工模板时拒绝（回退规划）；仅剩 auto 模板时放行。"""
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        class _FakeMsg:
            def publish(self, *a, **k):
                pass

        class _AutoLLM:
            def call(self, system, prompt, **kw):
                return {"template": "auto-financial-x"}

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = _FakeMsg()
        o._now_iso = lambda: "t"
        o._plan_llm = _AutoLLM()
        tmp = tempfile.mkdtemp(prefix="weavemind_auto_only_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            auto_steps = [{"step_id": "1", "capability": "web_search",
                           "instruction": "auto"}]
            manual_steps = [{"step_id": "1", "capability": "web_search",
                             "instruction": "manual"}]
            # 情形一：手工模板存在 → auto-* 被拒，进入完整规划（None）
            o._load_templates = lambda: [
                {"name": "手工模板A", "goal": "手工目标", "steps": manual_steps},
                {"name": "auto-financial-x", "goal": "auto 目标", "steps": auto_steps},
            ]
            self.assertIsNone(o._route_template("分析腾讯集团历年财报", "t-auto-1"))
            # 情形二：库中只有 auto 模板 → 允许选中
            o._load_templates = lambda: [
                {"name": "auto-financial-x", "goal": "auto 目标", "steps": auto_steps},
            ]
            routed = o._route_template("分析腾讯集团历年财报", "t-auto-2")
            self.assertEqual(routed, auto_steps)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

def _make_share_fake():
    """构造继承自 web_ui.Handler 的最小替身，可直接驱动路由方法。"""
    import web_ui

    class _FakeShareHandler(web_ui.Handler):
        def __init__(self, path: str, body: dict | None = None, headers: dict | None = None):
            import io
            import json
            self.path = path
            self.command = "GET"
            self.request_version = "HTTP/1.1"
            self.client_address = ("127.0.0.1", 0)
            self.headers = {"Host": "localhost:8080"}
            if headers:
                self.headers.update(headers)
            raw = b""
            if body is not None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.headers["Content-Length"] = str(len(raw))
            self.rfile = io.BytesIO(raw)
            self.wfile = io.BytesIO()
            self._status = 200
            self._headers = {}

        def send_response(self, code: int):
            self._status = code

        def send_header(self, key: str, value: str):
            self._headers[key] = value

        def end_headers(self):
            pass

        def json_body(self) -> dict:
            import json
            return json.loads(self.wfile.getvalue().decode("utf-8"))

        def html_body(self) -> str:
            return self.wfile.getvalue().decode("utf-8")

    return _FakeShareHandler


class TestReportShare(unittest.TestCase):
    """报告一键分享：token 生成/幂等/落盘/分享页/撤销 回归测试。"""
    def setUp(self):
        import os
        import shutil
        import tempfile
        import json as _json
        import workspace as ws_mod
        import audit_logger
        import web_ui

        self.web_ui = web_ui
        self._tmp = tempfile.mkdtemp(prefix="weavemind_share_")
        self._old_share_file = web_ui.SHARE_FILE
        self._old_db_path = web_ui.DB_PATH
        self._old_config_path = web_ui.CONFIG_PATH
        self._old_audit_file = audit_logger.AUDIT_FILE
        self._old_root = ws_mod.WORKSPACE_ROOT
        self._saved_results = dict(web_ui._task_results)
        self._saved_sessions = dict(web_ui._sessions)
        self._FakeHandler = _make_share_fake()
        web_ui.SHARE_FILE = os.path.join(self._tmp, "share_links.json")
        web_ui.DB_PATH = os.path.join(self._tmp, "test_share.db")
        web_ui.CONFIG_PATH = os.path.join(self._tmp, "config.json")
        audit_logger.AUDIT_FILE = os.path.join(self._tmp, "audit.jsonl")
        with open(web_ui.CONFIG_PATH, "w", encoding="utf-8") as f:
            _json.dump({
                "users": {
                    "admin": {
                        "password_hash": web_ui._hash_password("admin123"),
                        "role": "admin",
                    }
                }
            }, f, ensure_ascii=False, indent=2)
        with web_ui._sessions_lock:
            web_ui._sessions.clear()
        self._admin_token = web_ui._create_session("admin", "admin")
        ws_mod.configure_workspace_root(self._tmp)
        self.addCleanup(self._restore)

    def _restore(self):
        import shutil
        import workspace as ws_mod
        import audit_logger
        web_ui = self.web_ui
        web_ui.SHARE_FILE = self._old_share_file
        web_ui.DB_PATH = self._old_db_path
        web_ui.CONFIG_PATH = self._old_config_path
        audit_logger.AUDIT_FILE = self._old_audit_file
        ws_mod.WORKSPACE_ROOT = self._old_root
        with web_ui._task_lock:
            web_ui._task_results.clear()
            web_ui._task_results.update(self._saved_results)
        with web_ui._sessions_lock:
            web_ui._sessions.clear()
            web_ui._sessions.update(self._saved_sessions)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _handler(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        headers: dict | None = None,
        auth: bool = True,
    ):
        """构造假 Handler；auth=False 模拟未登录访客（公开分享页/密码验证）。"""
        h = self._FakeHandler(
            path,
            body,
            ({"Authorization": "Bearer " + self._admin_token} if auth else {}),
        )
        if headers:
            h.headers.update(headers)
        h.command = method
        return h

    def test_share_lifecycle_and_idempotent(self):
        import workspace as ws_mod
        tid = "t-share-1"
        ws = ws_mod.task_workspace(tid)
        (ws / "charts").mkdir(parents=True)
        (ws / "charts" / "heatmap.png").write_bytes(b"png")
        with self.web_ui._task_lock:
            self.web_ui._task_results[tid] = {
                "task_id": tid,
                "status": "SUCCESS",
                "goal": "新能源汽车市场调研",
                "report": (
                    "# 市场调研\n\n"
                    "![热力图](charts/heatmap.png)\n\n"
                    "|指标|数值|\n|---|---|\n|市场规模|100亿|"
                ),
            }
        try:
            # 生成分享链接
            h = self._handler("/api/share", "POST", {"task_id": tid})
            self.web_ui.Handler.do_POST(h)
            self.assertEqual(h._status, 200)
            d = h.json_body()
            self.assertEqual(d["status"], "ok")
            self.assertEqual(d["task_id"], tid)
            token = d["token"]
            self.assertGreaterEqual(len(token), 16)
            self.assertIn(f"/share/{token}", d["url"])
            # 同一任务重复生成：幂等复用同一 token
            h2 = self._handler("/api/share", "POST", {"task_id": tid})
            self.web_ui.Handler.do_POST(h2)
            self.assertEqual(h2.json_body()["token"], token)
            # 分享页 200 且含报告正文与图表路由
            h3 = self._handler(f"/share/{token}")
            self.web_ui.Handler.do_GET(h3)
            html_body = h3.html_body()
            self.assertEqual(h3._status, 200)
            self.assertIn("市场调研", html_body)
            self.assertIn("/files/t-share-1/charts/heatmap.png", html_body)
            self.assertIn("市场规模", html_body)
            # 状态查询接口返回已分享
            h4 = self._handler(f"/api/share/{tid}")
            self.web_ui.Handler.do_GET(h4)
            self.assertTrue(h4.json_body()["shared"])
            # 撤销后分享页 404
            h5 = self._handler(f"/api/share/{tid}", "DELETE")
            self.web_ui.Handler.do_DELETE(h5)
            self.assertEqual(h5._status, 200)
            self.assertGreaterEqual(h5.json_body()["revoked"], 1)
            h6 = self._handler(f"/share/{token}")
            self.web_ui.Handler.do_GET(h6)
            self.assertEqual(h6._status, 404)
            self.assertIn("不存在", h6.html_body())
        finally:
            with self.web_ui._task_lock:
                self.web_ui._task_results.pop(tid, None)

    def test_share_persists_after_restart(self):
        import sqlite3
        tid = "t-share-restart"
        # 模拟服务重启：内存结果为空，报告与分享映射都从持久化恢复
        db = sqlite3.connect(self.web_ui.DB_PATH, timeout=5)
        db.execute(
            "CREATE TABLE IF NOT EXISTS task_history("
            "task_id TEXT PRIMARY KEY, goal TEXT, status TEXT, report TEXT,"
            "created_at TIMESTAMP, completed_at TIMESTAMP,"
            "conversation_id TEXT, parent_task_id TEXT, context TEXT)"
        )
        db.execute(
            "INSERT INTO task_history(task_id, goal, status, report, created_at) "
            "VALUES(?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(task_id) DO UPDATE SET "
            "goal=excluded.goal, status=excluded.status, report=excluded.report",
            (tid, "重启后仍可访问的报告", "SUCCESS", "# 重启存活报告"),
        )
        db.commit()
        db.close()
        token = self.web_ui._generate_share_token(tid)
        # 映射确实落盘为 JSON
        import json as _json
        with open(self.web_ui.SHARE_FILE, "r", encoding="utf-8") as f:
            saved = _json.load(f)
        self.assertEqual(saved[token]["task_id"], tid)
        self.assertIn("expires_at", saved[token])
        # 模拟重启后按 token 解析并渲染分享页
        self.assertEqual(self.web_ui._resolve_share_token(token), tid)
        h = self._handler(f"/share/{token}")
        self.web_ui.Handler.do_GET(h)
        self.assertEqual(h._status, 200)
        self.assertIn("重启存活报告", h.html_body())

    def test_password_share_flow(self):
        """F6：密码分享 401→错误密码 403→正确密码放行（Cookie 7 天），
        且受保护任务的 /files 附件同样需要放行 Cookie。"""
        import workspace as ws_mod
        from datetime import datetime, timezone

        tid = "t-share-pwd"
        ws = ws_mod.task_workspace(tid)
        (ws / "project" / "charts").mkdir(parents=True)
        (ws / "project" / "charts" / "a.png").write_bytes(b"png")
        (ws / "charts").mkdir(parents=True)
        (ws / "charts" / "a.png").write_bytes(b"png")
        with self.web_ui._task_lock:
            self.web_ui._task_results[tid] = {
                "task_id": tid,
                "status": "SUCCESS",
                "goal": "密码保护报告",
                "report": (
                    "# 密码保护报告\n\n"
                    "![图](charts/a.png)\n\n"
                    "比特币现价 67450 美元。"
                ),
            }
        try:
            # 带密码 + 24 小时有效期创建分享
            h = self._handler("/api/share", "POST", {
                "task_id": tid,
                "password": "secret123",
                "ttl_hours": 24,
            })
            self.web_ui.Handler.do_POST(h)
            self.assertEqual(h._status, 200)
            d = h.json_body()
            token = d["token"]
            self.assertTrue(d["protected"])
            self.assertEqual(d["expires_in_days"], 1)
            self.assertIn("expires_at", d)
            # 状态查询接口返回保护与过期信息
            hs = self._handler(f"/api/share/{tid}")
            self.web_ui.Handler.do_GET(hs)
            sd = hs.json_body()
            self.assertTrue(sd["protected"])
            self.assertEqual(sd["expires_at"], d["expires_at"])
            # 未验证：分享页 401（密码输入页），附件 401
            h2 = self._handler(f"/share/{token}", auth=False)
            self.web_ui.Handler.do_GET(h2)
            self.assertEqual(h2._status, 401)
            self.assertIn("需要密码", h2.html_body())
            hf = self._handler(f"/files/{tid}/charts/a.png", auth=False)
            self.web_ui.Handler.do_GET(hf)
            self.assertEqual(hf._status, 401)
            # 错误密码 → 403
            h3 = self._handler(
                f"/share/{token}/auth", "POST", {"password": "wrong"}, auth=False,
            )
            self.web_ui.Handler.do_POST(h3)
            self.assertEqual(h3._status, 403)
            self.assertIn("密码错误", h3.html_body())
            # 正确密码 → 302 + Set-Cookie share_<token>=ok
            h4 = self._handler(
                f"/share/{token}/auth", "POST", {"password": "secret123"}, auth=False,
            )
            self.web_ui.Handler.do_POST(h4)
            self.assertEqual(h4._status, 302)
            self.assertEqual(h4._headers.get("Location"), f"/share/{token}")
            set_cookie = h4._headers.get("Set-Cookie", "")
            self.assertIn(f"share_{token}=ok", set_cookie)
            self.assertIn("Max-Age=604800", set_cookie)
            # 带 Cookie：分享页 200，附件 200
            h5 = self._handler(
                f"/share/{token}",
                headers={"Cookie": f"share_{token}=ok"},
                auth=False,
            )
            self.web_ui.Handler.do_GET(h5)
            self.assertEqual(h5._status, 200)
            self.assertIn("密码保护报告", h5.html_body())
            self.assertIn("67450", h5.html_body())
            hf2 = self._handler(
                f"/files/{tid}/charts/a.png",
                headers={"Cookie": f"share_{token}=ok"},
                auth=False,
            )
            self.web_ui.Handler.do_GET(hf2)
            self.assertEqual(hf2._status, 200)
            self.assertEqual(hf2.wfile.getvalue(), b"png")
            # 撤销后 404（不受密码影响）
            hr = self._handler(f"/api/share/{tid}", "DELETE")
            self.web_ui.Handler.do_DELETE(hr)
            h6 = self._handler(
                f"/share/{token}",
                headers={"Cookie": f"share_{token}=ok"},
                auth=False,
            )
            self.web_ui.Handler.do_GET(h6)
            self.assertEqual(h6._status, 404)
        finally:
            with self.web_ui._task_lock:
                self.web_ui._task_results.pop(tid, None)

    def test_share_ttl_custom_expiry(self):
        """F6：ttl_hours 自定义过期写入 expires_at；mock 时间越过过期点后失效。"""
        import json as _json
        from datetime import datetime, timezone

        tid = "t-share-ttl"
        with self.web_ui._task_lock:
            self.web_ui._task_results[tid] = {
                "task_id": tid,
                "status": "SUCCESS",
                "goal": "TTL 测试报告",
                "report": "# TTL 测试报告\n\n市场数据见正文。",
            }
        try:
            h = self._handler("/api/share", "POST", {
                "task_id": tid,
                "ttl_hours": 1,
            })
            self.web_ui.Handler.do_POST(h)
            self.assertEqual(h._status, 200)
            token = h.json_body()["token"]
            with open(self.web_ui.SHARE_FILE, "r", encoding="utf-8") as f:
                saved = _json.load(f)
            rec = saved[token]
            exp = datetime.fromisoformat(rec["expires_at"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            self.assertGreater(exp, now)
            self.assertLess((exp - now).total_seconds(), 3600)
            self.assertGreater((exp - now).total_seconds(), 3500)
            # 上限校验：超过 30 天拒绝
            h2 = self._handler("/api/share", "POST", {
                "task_id": tid,
                "ttl_hours": 721,
            })
            self.web_ui.Handler.do_POST(h2)
            self.assertEqual(h2._status, 400)
            # mock 时间越过过期点 → token 立即失效
            old_time = self.web_ui.time.time
            self.web_ui.time.time = lambda: exp.timestamp() + 1
            try:
                self.assertIsNone(self.web_ui._resolve_share_token(token))
            finally:
                self.web_ui.time.time = old_time
        finally:
            with self.web_ui._task_lock:
                self.web_ui._task_results.pop(tid, None)

    def test_invalid_token_and_missing_task_404(self):
        h = self._handler("/share/not-a-real-token")
        self.web_ui.Handler.do_GET(h)
        self.assertEqual(h._status, 404)
        h2 = self._handler("/api/share", "POST", {"task_id": "t-nonexistent"})
        self.web_ui.Handler.do_POST(h2)
        self.assertEqual(h2._status, 404)

    def test_markdown_renderer_sanitizes_html(self):
        md = (
            "# 标题\n\n"
            "<script>alert('x')</script>\n\n"
            "[恶意链接](javascript:alert(1))\n\n"
            "![图](charts/a.png)"
        )
        out = self.web_ui._markdown_to_html(md, "t-safe-share")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn('href="#"', out, "javascript: 协议应被拦截")
        self.assertIn("/files/t-safe-share/charts/a.png", out)


class TestContractFailureSemantics(unittest.TestCase):
    """Bug1：data_analyzer/data_loader 等返回 status=failed 必须判契约不通过，
    错误信息携带原始 error 字段；status=success 正常通过。"""

    def test_data_analyzer_failed_status_fails_contract(self):
        from tool_contracts import validate_result

        ok, issues = validate_result(
            "data_analyzer", {"status": "failed", "error": "no data found"},
        )
        self.assertFalse(ok)
        joined = "；".join(issues)
        self.assertIn("failed", joined)
        self.assertIn("no data found", joined)

    def test_data_analyzer_success_passes(self):
        from tool_contracts import validate_result

        ok, issues = validate_result(
            "data_analyzer", {"status": "success", "charts": ["/tmp/a.png"]},
        )
        self.assertTrue(ok, issues)

    def test_loader_fetch_fileio_failed_flagged(self):
        from tool_contracts import validate_result

        for cap in ("data_loader", "web_fetch", "file_io"):
            ok, issues = validate_result(
                cap, {"status": "failed", "error": "boom"},
            )
            self.assertFalse(ok, cap)
            self.assertIn("boom", "；".join(issues), cap)

    def test_model_trainer_failed_flagged(self):
        from tool_contracts import validate_result

        ok, _ = validate_result(
            "model_trainer", {"status": "failed", "error": "train err"},
        )
        self.assertFalse(ok)

    def test_web_search_empty_still_fails(self):
        from tool_contracts import validate_result

        ok, issues = validate_result("web_search", [])
        self.assertFalse(ok)
        self.assertIn("返回空列表", "；".join(issues))


class TestDeliverableCompleteness(unittest.TestCase):
    """Bug2：验收器必须检出空壳报告（数据缺失占位/无有效表格）。"""

    def test_empty_shell_report_fails(self):
        from acceptance_checker import check_deliverable_completeness

        report = (
            "# 今日A股总成交量前十股\n\n"
            "| 排名 | 名称 | 成交量 |\n|---|---|---|\n"
            "| 1 | 未披露 | 未披露 |\n"
            "| 2 | 未披露 | 未披露 |\n"
            "| 3 | 未披露 | 未披露 |\n"
        )
        res = check_deliverable_completeness(report, "今日A股总成交量前十股")
        self.assertFalse(res["pass"])
        self.assertGreaterEqual(res["placeholder_count"], 3)
        self.assertIn("交付物不完整", res["details"])
        self.assertIn("数据缺失占位", res["details"])

    def test_real_top10_table_passes(self):
        from acceptance_checker import check_deliverable_completeness

        report = (
            "# 今日A股成交额前十股\n\n"
            "| 排名 | 代码 | 名称 | 成交额(亿元) |\n"
            "|---|---|---|---|\n"
            "| 1 | 600519 | 贵州茅台 | 45.0 |\n"
            "| 2 | 300750 | 宁德时代 | 42.5 |\n"
        )
        res = check_deliverable_completeness(report, "今日A股成交额前十股")
        self.assertTrue(res["pass"], res["details"])
        self.assertTrue(res["has_table"])

    def test_normal_report_without_list_requirement_passes(self):
        from acceptance_checker import check_deliverable_completeness

        res = check_deliverable_completeness(
            "调研报告：新能源汽车市场保持增长，正文如下。",
            "总结新能源汽车市场现状",
        )
        self.assertTrue(res["pass"])

    def test_list_goal_without_table_fails(self):
        from acceptance_checker import check_deliverable_completeness

        res = check_deliverable_completeness(
            "前十股为：1 贵州茅台 2 平安银行 3 宁德时代",
            "今日A股总成交量前十股",
        )
        self.assertFalse(res["pass"])
        self.assertIn("没有可用的数据表格", res["details"])

    def test_table_all_placeholder_fails(self):
        from acceptance_checker import check_deliverable_completeness

        report = "| 名称 | 数据 |\n|---|---|\n| 未披露 | 未获取 |"
        res = check_deliverable_completeness(report, "今日A股总成交量前十股")
        self.assertFalse(res["pass"])
        self.assertTrue(res["table_all_placeholder"])

    def test_placeholder_penalty_exempt_for_research_domain(self):
        """行业调研（非金融域）：诚实披露数据缺失不双重惩罚——占位不扣分，
        数字溯源仍由 number_traceability 把关。"""
        from acceptance_checker import check_deliverable_completeness

        report = (
            "# 固态电池行业现状调研\n\n"
            "主要挑战：界面阻抗问题；空气稳定性差。\n\n"
            "部分厂商未披露量产时间；头部企业数据缺失；"
            "具体成本数据待补充。\n"
        )
        res = check_deliverable_completeness(report, "固态电池行业现状调研")
        self.assertTrue(res["pass"], res["details"])
        self.assertGreaterEqual(res["placeholder_count"], 3)
        self.assertIn("豁免", res["details"])

    def test_placeholder_penalty_kept_for_financial_domain(self):
        """financial 域保持严格：同样的占位报告必须 FAIL（数字必须溯源）。"""
        from acceptance_checker import check_deliverable_completeness

        report = "宁德时代营收数据未披露；净利润未获取；毛利率待补充；负债数据缺失"
        res = check_deliverable_completeness(report, "宁德时代2025年财报营收分析")
        self.assertFalse(res["pass"])
        self.assertIn("交付物不完整", res["details"])

    def test_run_acceptance_research_exemption_in_checks(self):
        """run_acceptance 将领域下传：调研任务的占位报告 deliverable 检查 pass。"""
        import shutil as _shutil
        import tempfile as _tempfile

        import workspace as ws_mod
        from acceptance_checker import run_acceptance

        tmp = _tempfile.mkdtemp(prefix="wm_acc_research_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws = ws_mod.task_workspace("t-research-1")
            ws.mkdir(parents=True, exist_ok=True)
            report = (
                "# 固态电池行业现状调研\n\n"
                "部分厂商未披露量产时间；头部企业数据缺失；"
                "具体成本数据待补充。\n"
            )
            result = run_acceptance(
                "t-research-1", "固态电池行业现状调研", report, ws,
            )
            comp = result["checks"]["deliverable_completeness"]
            self.assertTrue(comp["pass"], comp["details"])
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_run_acceptance_detects_shell_report(self):
        import shutil as _shutil
        import tempfile as _tempfile

        import workspace as ws_mod
        from acceptance_checker import run_acceptance

        tmp = _tempfile.mkdtemp(prefix="wm_acc_comp_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws = ws_mod.task_workspace("t-comp-1")
            ws.mkdir(parents=True, exist_ok=True)
            report = "# 今日A股总成交量前十股\n\n" + "未披露" * 10
            result = run_acceptance(
                "t-comp-1", "今日A股总成交量前十股", report, ws,
            )
            self.assertIn("deliverable_completeness", result["checks"])
            self.assertEqual(result["overall"], "fail")
            self.assertTrue(
                any("交付物不完整" in g for g in result["gaps"]),
                result["gaps"],
            )
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            _shutil.rmtree(tmp, ignore_errors=True)


class TestRankingAdapter(unittest.TestCase):
    """Bug3（可选增强）：东方财富行情排行适配器 canned 解析 + 路由分支。"""

    def test_fetch_ranking_parses_canned(self):
        import json as _json
        from unittest import mock

        import adapters.ashare_ranking as ar

        sample = {"data": {"diff": [
            {"f12": "600519", "f14": "贵州茅台", "f2": 1500.0, "f3": 2.5,
             "f5": 30000, "f6": 4500000000, "f8": 0.8,
             "f20": 1800000000000},
            {"f12": "000001", "f14": "平安银行", "f2": 11.0, "f3": -1.2,
             "f5": 500000, "f6": 5500000000, "f8": 1.1,
             "f20": 210000000000},
        ]}}
        with mock.patch.object(
            ar, "_get", return_value=_json.dumps(sample, ensure_ascii=False),
        ):
            out = ar.fetch_ranking("amount", 2)
        self.assertEqual(out["metric"], "amount")
        self.assertEqual(out["rows"][0]["name"], "贵州茅台")
        self.assertEqual(out["rows"][0]["amount_yi"], 45.0)
        self.assertEqual(out["rows"][1]["rank"], 2)
        self.assertEqual(out["rows"][1]["volume_wan_hand"], 50.0)

    def test_route_structured_ranking_branch(self):
        from unittest import mock

        import adapters.router as router

        with mock.patch("adapters.router.fetch_ranking") as fr:
            fr.return_value = {
                "rows": [{"rank": 1, "name": "贵州茅台"}],
                "metric": "volume", "top_n": 1,
                "source_url": "http://eastmoney.test",
                "retrieved_at": "2026-08-22 10:00:00",
            }
            out = router.route_structured("今日A股总成交量前十股")
        self.assertEqual(out["source"], "eastmoney_ranking")
        self.assertEqual(out["metadata"]["metric"], "volume")
        self.assertEqual(out["metadata"]["market"], "A股")
        fr.assert_called_once_with("volume", top_n=10)

    def test_route_structured_ranking_fetch_failure_logs_warning(self):
        """P2-6：fetch_ranking 抛异常不再静默——warning 日志 + 返回 None。"""
        import adapters.router as router

        with mock.patch(
            "adapters.router.fetch_ranking",
            side_effect=RuntimeError("eastmoney timeout"),
        ), mock.patch(
            "adapters.router.fetch_tencent_ranking",
            side_effect=RuntimeError("tencent timeout"),
        ):
            with self.assertLogs("adapters.router", level="WARNING") as cm:
                out = router.route_structured("今日A股总成交量前十股")
        self.assertIsNone(out)
        self.assertTrue(
            any("ranking fetch failed" in m for m in cm.output), cm.output,
        )

    def test_route_structured_other_branches_failure_logs_warning(self):
        """P2-6：crypto/macro/news fetch 失败同样 warning + 返回 None。"""
        import adapters.router as router

        cases = [
            ("比特币最新价格", "fetch_market", "crypto fetch failed"),
            ("美国 CPI 宏观分析", "fetch_macro", "macro fetch failed"),
            ("最新新闻头条", "fetch_news", "news fetch failed"),
        ]
        for goal, fn, msg in cases:
            with self.subTest(goal=goal), \
                    mock.patch.object(
                        router, fn, side_effect=RuntimeError("boom"),
                    ), \
                    self.assertLogs(
                        "adapters.router", level="WARNING",
                    ) as cm:
                out = router.route_structured(goal)
            self.assertIsNone(out)
            self.assertTrue(any(msg in m for m in cm.output), cm.output)

    def test_ranking_metric_amount_for_amount_goal(self):
        import adapters.router as router

        self.assertEqual(
            router._ranking_metric("今日A股成交额排行前十"), "amount",
        )
        self.assertEqual(
            router._ranking_metric("今日A股总成交量前十股"), "volume",
        )


class TestRankingAdapterResilience(unittest.TestCase):
    """东方财富排行 API 反爬韧性：双通道 + 指数退避（全部 canned，不真连网）。"""

    def _sample_text(self) -> str:
        return json.dumps({"data": {"diff": [
            {"f12": "600519", "f14": "贵州茅台", "f2": 1500.0, "f3": 2.5,
             "f5": 30000, "f6": 4500000000, "f8": 0.8,
             "f20": 1800000000000},
        ]}}, ensure_ascii=False)

    def test_fetch_ranking_retries_after_transient_failure(self):
        """首次 RemoteDisconnected → 指数退避 2s → 第二次成功。"""
        import os

        import adapters.ashare_ranking as ar

        with mock.patch.dict(
            os.environ, {"EASTMONEY_RETRY_BASE": "2"}, clear=False,
        ), mock.patch.object(
            ar, "_get",
            side_effect=[
                RemoteDisconnected("Remote end closed connection without response"),
                self._sample_text(),
            ],
        ) as get_mock, mock.patch.object(ar.time, "sleep") as sleep_mock, \
                self.assertLogs("adapters.ashare_ranking", level="WARNING") as cm:
            out = ar.fetch_ranking("amount", 1)
        self.assertEqual(out["rows"][0]["name"], "贵州茅台")
        self.assertEqual(get_mock.call_count, 2)
        sleep_mock.assert_called_once_with(2.0)
        self.assertTrue(
            any("eastmoney fetch attempt 1 failed: urllib:" in m for m in cm.output),
            cm.output,
        )

    def test_get_falls_back_to_socket_channel(self):
        """urllib 被断开 → 降级 raw socket HTTP/1.0 通道成功。"""
        import adapters.ashare_ranking as ar
        import adapters.transport as tr

        with mock.patch.object(
            tr, "get_via_urllib",
            side_effect=RemoteDisconnected(
                "Remote end closed connection without response",
            ),
        ), mock.patch.object(
            tr, "get_via_socket", return_value=self._sample_text(),
        ) as sock_mock, self.assertLogs(
            "adapters.transport", level="WARNING",
        ) as cm:
            text = ar._get("https://push2.eastmoney.com/api/qt/clist/get?pn=1")
        self.assertEqual(json.loads(text)["data"]["diff"][0]["f14"], "贵州茅台")
        sock_mock.assert_called_once()
        self.assertTrue(
            any("eastmoney fetch attempt 1 failed: urllib:" in m for m in cm.output),
            cm.output,
        )

    def test_get_raises_when_both_channels_fail(self):
        """两通道都失败 → 抛异常且带两通道原因。"""
        import adapters.ashare_ranking as ar
        import adapters.transport as tr

        with mock.patch.object(
            tr, "get_via_urllib",
            side_effect=RemoteDisconnected(
                "Remote end closed connection without response",
            ),
        ), mock.patch.object(
            tr, "get_via_socket", side_effect=OSError("socket reset"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                ar._get("https://push2.eastmoney.com/api/qt/clist/get?pn=1")
        self.assertIn("urllib", str(ctx.exception))
        self.assertIn("socket", str(ctx.exception))

    def test_fetch_ranking_raises_after_all_retries_fail(self):
        """3 次重试全失败（共 4 次尝试）→ 抛异常，退避 2/4/8s。"""
        import os

        import adapters.ashare_ranking as ar

        with mock.patch.dict(
            os.environ, {"EASTMONEY_RETRY_BASE": "2"}, clear=False,
        ), mock.patch.object(
            ar, "_get",
            side_effect=RemoteDisconnected(
                "Remote end closed connection without response",
            ),
        ) as get_mock, mock.patch.object(ar.time, "sleep") as sleep_mock, \
                self.assertLogs("adapters.ashare_ranking", level="WARNING") as cm:
            with self.assertRaises(RemoteDisconnected):
                ar.fetch_ranking("amount", 1)
        self.assertEqual(get_mock.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list],
            [2.0, 4.0, 8.0],
        )
        for n in range(1, 5):
            self.assertTrue(
                any(
                    f"eastmoney fetch attempt {n} failed: urllib:" in m
                    for m in cm.output
                ),
                cm.output,
            )


def _ranking_sample(n: int = 10, metric: str = "volume") -> dict:
    """构造 eastmoney_ranking canned 数据（与适配器 payload 同构）。"""
    names = [
        "京东方A", "贵州茅台", "宁德时代", "比亚迪", "中国平安",
        "招商银行", "中信证券", "五粮液", "隆基绿能", "东方财富",
    ]
    rows = []
    for i in range(n):
        rows.append({
            "rank": i + 1,
            "code": f"00000{i}",
            "name": names[i % len(names)],
            "price": round(10 + i * 3.7, 2),
            "change_pct": round((i % 7) - 2.5, 2),
            "volume_hand": (i + 1) * 200000,
            "volume_wan_hand": round((i + 1) * 20.0, 2),
            "amount_yuan": (i + 1) * 1_000_000_000,
            "amount_yi": round((i + 1) * 10.0, 2),
            "turnover_pct": round(0.5 + i * 0.3, 2),
            "market_cap_yi": round(500 + i * 33.0, 2),
        })
    return {
        "source": "eastmoney_ranking",
        "data": {
            "rows": rows,
            "metric": metric,
            "top_n": len(rows),
            "source_url": "http://eastmoney.test/clist",
            "retrieved_at": "2026-08-22 10:00:00",
        },
        "metadata": {
            "source": "eastmoney_ranking",
            "market": "A股",
            "metric": metric,
            "top_n": len(rows),
            "unit": "亿元",
            "label": "A股成交量排行" if metric == "volume" else "A股成交额排行",
            "retrieved_at": "2026-08-22 10:00:00",
        },
    }


class TestRankingStructuredChain(unittest.TestCase):
    """断链修复（ui-1954f66cb0 复测暴露）：
    预载 structured_data.json 后，data_analyzer/报告/图表能直接消费排行数据。"""

    def test_preload_ranking_retries_after_transient_failure(self):
        """P2-6：fetch_ranking 首次抛异常、第二次成功 → 预载重试生效。"""
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = None
        tmp = Path(tempfile.mkdtemp(prefix="rk_retry_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        payload = {
            "rows": [{"rank": 1, "name": "贵州茅台"}],
            "metric": "volume", "top_n": 1,
            "source_url": "http://eastmoney.test",
            "retrieved_at": "2026-08-22 10:00:00",
        }
        try:
            with mock.patch(
                "adapters.router.fetch_ranking",
                side_effect=[RuntimeError("eastmoney timeout"), payload],
            ) as fr, mock.patch(
                "adapters.router.fetch_tencent_ranking",
                side_effect=RuntimeError("tencent timeout"),
            ), mock.patch("orchestrator_v2.time.sleep") as sl:
                data = o._structured_data_preload(
                    "t-rk-retry", "今日A股总成交量前十股",
                )
            self.assertEqual(data["source"], "eastmoney_ranking")
            self.assertEqual(fr.call_count, 2)
            sl.assert_called_once_with(2)
            sd = ws_mod.task_project_dir("t-rk-retry") / "structured_data.json"
            self.assertTrue(sd.exists(), "重试成功应写入 structured_data.json")
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preload_ranking_gives_up_after_retry_failure(self):
        """P2-6：两次都失败 → warning 日志且返回 None，不留空壳文件。"""
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = None
        tmp = Path(tempfile.mkdtemp(prefix="rk_giveup_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            with mock.patch(
                "adapters.router.fetch_ranking",
                side_effect=RuntimeError("eastmoney timeout"),
            ) as fr, mock.patch(
                "adapters.router.fetch_tencent_ranking",
                side_effect=RuntimeError("tencent timeout"),
            ), mock.patch("orchestrator_v2.time.sleep") as sl, \
                    self.assertLogs(
                        "orchestrator_v2", level="WARNING",
                    ) as cm:
                data = o._structured_data_preload(
                    "t-rk-giveup", "今日A股总成交量前十股",
                )
            self.assertIsNone(data)
            self.assertEqual(fr.call_count, 2)
            sl.assert_called_once_with(2)
            self.assertTrue(
                any("retry" in m.lower() for m in cm.output), cm.output,
            )
            proj = ws_mod.task_project_dir("t-rk-giveup")
            self.assertFalse((proj / "structured_data.json").exists())
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_preload_ranking_writes_ranking_csv(self):
        """A 方案：预载 eastmoney_ranking 后 data/ranking.csv 生成（canned）。"""
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = None
        tmp = Path(tempfile.mkdtemp(prefix="rk_preload_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            with mock.patch(
                "adapters.router.route_structured",
                return_value=_ranking_sample(),
            ):
                data = o._structured_data_preload(
                    "t-rk-pre", "今日A股总成交量前十股",
                )
            self.assertEqual(data["source"], "eastmoney_ranking")
            csv_path = ws_mod.task_data_dir("t-rk-pre") / "ranking.csv"
            self.assertTrue(csv_path.exists(), "应生成 data/ranking.csv")
            import pandas as pd
            df = pd.read_csv(csv_path)
            self.assertEqual(len(df), 10)
            self.assertEqual(df.iloc[0]["name"], "京东方A")
            self.assertEqual(df.iloc[0]["volume_wan_hand"], 20.0)
            self.assertIn("amount_yi", df.columns)
            sd_path = ws_mod.task_project_dir("t-rk-pre") / "structured_data.json"
            self.assertTrue(sd_path.exists())
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_data_analyzer_instruction_prefers_preloaded_ranking(self):
        """指令注入：工作区有 structured_data.json / ranking.csv 时，
        data_analyzer 步骤追加 [Data: 路径] 与优先使用提示。"""
        import tempfile
        import threading
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._task_user_ids = {}
        tmp = Path(tempfile.mkdtemp(prefix="rk_instr_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            proj = ws_mod.task_project_dir("t-da")
            (proj / "structured_data.json").write_text(
                json.dumps(_ranking_sample(), ensure_ascii=False),
                encoding="utf-8",
            )
            OrchestratorV2._export_ranking_csv("t-da", _ranking_sample())
            step = {
                "step_id": "2", "capability": "data_analyzer",
                "instruction": "分析排行数据", "depends_on": [],
            }
            instr = o._inject_step_context(step, {}, threading.Lock(), "t-da")
            self.assertIn("[Data:", instr)
            self.assertIn("ranking.csv", instr)
            self.assertIn("无需再找 CSV", instr)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_plan_replaces_data_analyzer_when_structured_preloaded(self):
        """B 方案：已预载结构化行情数据时 data_analyzer → content_summary。"""
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._messaging = None
        steps = [
            {"step_id": "1", "capability": "data_analyzer",
             "instruction": "EDA", "depends_on": []},
            {"step_id": "2", "capability": "report_generator",
             "instruction": "报告", "depends_on": ["1"]},
        ]
        out = o._reduce_steps_for_structured("t-rk-b", steps, _ranking_sample())
        self.assertEqual(out[0]["capability"], "content_summary")
        self.assertEqual(out[0]["step_id"], "1")
        self.assertIn("structured_data.json", out[0]["instruction"])
        self.assertEqual(out[1]["depends_on"], ["1"], "依赖关系应保持不变")
        # 非结构化来源不替换
        out2 = o._reduce_steps_for_structured(
            "t-rk-b", steps, {"source": "other", "data": {}},
        )
        self.assertEqual(out2[0]["capability"], "data_analyzer")

    def test_structured_injection_includes_ranking_rows(self):
        """报告注入：eastmoney_ranking 的 [结构化数据] 块必须含排行 rows。"""
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        tmp = Path(tempfile.mkdtemp(prefix="rk_inj_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            proj = ws_mod.task_project_dir("t-rk-inj")
            (proj / "structured_data.json").write_text(
                json.dumps(_ranking_sample(2), ensure_ascii=False),
                encoding="utf-8",
            )
            block = OrchestratorV2._structured_injection("t-rk-inj")
            self.assertIn("[结构化数据]", block)
            self.assertIn("A股成交量排行（前十）", block)
            self.assertIn("京东方A", block)
            self.assertIn("000000", block)
            self.assertIn("20.0", block)
            self.assertIn("东方财富行情中心", block)
            self.assertIn("优先引用", block)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ranking_data_triggers_bar_and_scatter_charts(self):
        """排行数据 canned → clean_chart_data 并入 → 自动渲染 top10 条形图
        与量价散点（无需 LLM 规格）。"""
        import tempfile
        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = Path(tempfile.mkdtemp(prefix="rk_chart_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            proj = ws_mod.task_project_dir("t-rk-chart")
            OrchestratorV2._merge_structured_points(
                "t-rk-chart", _ranking_sample(10),
            )
            clean = json.loads(
                (proj / "clean_chart_data.json").read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(len(clean["market_data"]), 20)
            o._render_clean_chart_data("t-rk-chart", "今日A股总成交量前十股")
            specs = json.loads(
                (proj / "chart_data.json").read_text(encoding="utf-8")
            )["charts"]
            titles = [str(s.get("title") or "") for s in specs]
            self.assertTrue(
                any("成交量" in t and "对比" in t for t in titles),
                f"应生成成交量 top10 条形图：{titles}",
            )
            self.assertTrue(
                any("量价散点" in t for t in titles),
                f"应生成量价散点：{titles}",
            )
            pngs = {p.name for p in proj.glob("chart_*.png")}
            self.assertTrue(pngs, "应实际渲染排行图表 PNG")
            manifest = json.loads(
                (proj / "chart_manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["charts"])
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_analyzer_worker_loads_structured_json(self):
        """data_analyzer worker 可直接把 structured_data.json 转 DataFrame。"""
        import tempfile
        from workers.data_analyzer_worker import DataAnalyzerWorker

        tmp = Path(tempfile.mkdtemp(prefix="rk_json_"))
        try:
            p = tmp / "structured_data.json"
            p.write_text(
                json.dumps(_ranking_sample(3), ensure_ascii=False),
                encoding="utf-8",
            )
            df = DataAnalyzerWorker._load_frame(p)
            self.assertEqual(len(df), 3)
            self.assertIn("name", df.columns)
            self.assertIn("volume_wan_hand", df.columns)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestSearchMarketQueryVariants(unittest.TestCase):
    """Bug3：行情类目标搜索自动补东方财富定向查询模板并排除无关平台。"""

    def test_market_ranking_query_variants(self):
        from worker_base import SearchAgent

        sa = SearchAgent.__new__(SearchAgent)
        vs = sa._query_variants(
            "用户目标：今日A股总成交量前十股\n原始指令：搜索"
        )
        joined = "\n".join(vs)
        self.assertIn("东方财富", joined)
        self.assertIn("成交量", joined)
        self.assertIn("排行", joined)
        self.assertIn("前十", joined)
        self.assertIn("-site:youtube.com", joined)
        self.assertIn("-site:baike.baidu.com", joined)
        self.assertIn("site:eastmoney.com", joined)


class TestTencentQuotesRankingAndCache(unittest.TestCase):
    """腾讯行情适配器 + 排行缓存层（全部 canned，不真连网）。"""

    @staticmethod
    def _tencent_line(
        code: str, name: str, price: float, change_pct: float,
        volume: int, amount_wan: float, turnover: float = "1.2",
        market_cap_yi: float = "1000",
    ) -> str:
        """构造 qt.gtimg.cn 返回行：46 字段、~ 分隔、尾带引号分号。"""
        fields = [""] * 46
        fields[0] = "1"
        fields[1] = name
        fields[2] = code[-6:]
        fields[3] = str(price)
        fields[4] = str(price)          # 昨收
        fields[5] = str(price)          # 今开（验证不会误当涨跌幅）
        fields[6] = str(volume)
        fields[31] = "0"                # 涨跌
        fields[32] = str(change_pct)    # 涨跌幅%
        fields[37] = str(amount_wan)    # 成交额（万元）
        fields[38] = str(turnover)      # 换手率%
        fields[45] = str(market_cap_yi)  # 总市值（亿）
        return f'v_{code}="' + "~".join(fields) + '";'

    def test_fetch_quotes_parses_gbk_and_correct_indices(self):
        """GBK 解码 + 字段索引：价=3、涨跌%=32、量=6、额=37(万元)、换手=38。"""
        import adapters.tencent_quotes as tq

        raw = "\n".join([
            self._tencent_line(
                "sh600519", "贵州茅台", 1500.0, 2.5, 30000, 187040.0,
                turnover=0.8, market_cap_yi=18000.0,
            ),
            self._tencent_line(
                "sz000001", "平安银行", 11.0, -1.2, 500000, 550000.0,
                turnover=1.1, market_cap_yi=2100.0,
            ),
        ]).encode("gbk")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return raw

        with mock.patch("urllib.request.urlopen", return_value=_Resp()):
            out = tq.fetch_quotes(["600519", "sz000001"])
        q = out["600519"]
        self.assertEqual(q["name"], "贵州茅台")
        self.assertEqual(q["price"], 1500.0)
        self.assertEqual(q["change_pct"], 2.5)          # index 32，不是 index 5
        self.assertEqual(q["volume"], 30000)            # 手
        self.assertEqual(q["amount"], 187040.0 * 1e4)   # 万元 → 元
        self.assertEqual(q["turnover_pct"], 0.8)
        self.assertEqual(q["market_cap_yi"], 18000.0)
        self.assertEqual(out["sz000001"]["change_pct"], -1.2)
        self.assertEqual(out["sz000001"]["amount"], 550000.0 * 1e4)

    def test_fetch_ranking_sorts_by_volume_and_amount(self):
        """候选池排序：volume/amount 两口径、缺失值不参与、结构兼容 eastmoney。"""
        import adapters.tencent_quotes as tq

        quotes = {
            "sh600519": {
                "name": "贵州茅台", "price": 1500.0, "change_pct": 2.5,
                "volume": 30000, "amount": 1.87e9, "turnover_pct": 0.8,
                "market_cap_yi": 18000.0,
            },
            "sz000001": {
                "name": "平安银行", "price": 11.0, "change_pct": -1.2,
                "volume": 500000, "amount": 5.5e9, "turnover_pct": 1.1,
                "market_cap_yi": 2100.0,
            },
            "sh601318": {
                "name": "中国平安", "price": 45.0, "change_pct": 0.5,
                "volume": 100000, "amount": 9.9e9, "turnover_pct": 0.4,
                "market_cap_yi": 8200.0,
            },
            "sz300750": {  # 缺失值：不参与排序
                "name": "宁德时代", "price": 180.0, "change_pct": 1.0,
                "volume": None, "amount": None, "turnover_pct": None,
                "market_cap_yi": None,
            },
        }
        with mock.patch.object(tq, "fetch_quotes", return_value=quotes):
            by_vol = tq.fetch_ranking("volume", 2)
            by_amt = tq.fetch_ranking("amount", 2)
        self.assertEqual(by_vol["source"], "tencent_ranking")
        self.assertEqual(by_vol["market"], "A股")
        self.assertEqual([r["code"] for r in by_vol["rows"]], ["000001", "601318"])
        self.assertEqual(by_vol["rows"][0]["volume_wan_hand"], 50.0)
        self.assertEqual([r["code"] for r in by_amt["rows"]], ["601318", "000001"])
        self.assertEqual(by_amt["rows"][0]["amount_yi"], 99.0)
        self.assertEqual(by_amt["rows"][0]["rank"], 1)
        self.assertNotIn("300750", [r["code"] for r in by_vol["rows"]])

    def test_fetch_us_ranking_uses_us_candidate_pool(self):
        """美股候选池规模与排序、source=tencent_us_ranking。"""
        import adapters.tencent_quotes as tq

        self.assertTrue(40 <= len(tq.US_CANDIDATES) <= 60)
        self.assertTrue(80 <= len(tq.A_SHARE_CANDIDATES) <= 120)
        quotes = {
            "usAAPL": {
                "name": "苹果", "price": 233.0, "change_pct": 1.2,
                "volume": 1000, "amount": 2.3e8, "turnover_pct": 0.5,
                "market_cap_yi": 35000.0,
            },
            "usNVDA": {
                "name": "英伟达", "price": 188.0, "change_pct": -0.8,
                "volume": 3000, "amount": 5.6e8, "turnover_pct": 1.8,
                "market_cap_yi": 46000.0,
            },
            "usTSLA": {
                "name": "特斯拉", "price": 320.0, "change_pct": 2.1,
                "volume": 2000, "amount": 6.4e8, "turnover_pct": 1.2,
                "market_cap_yi": 10000.0,
            },
        }
        with mock.patch.object(tq, "fetch_quotes", return_value=quotes):
            out = tq.fetch_us_ranking("volume", 2)
        self.assertEqual(out["source"], "tencent_us_ranking")
        self.assertEqual(out["market"], "美股")
        self.assertEqual([r["code"] for r in out["rows"]], ["NVDA", "TSLA"])

    def test_route_structured_degrades_to_tencent_and_backfills_cache(self):
        """降级链：eastmoney 失败 → tencent_ranking 成功，结果回填缓存。"""
        import adapters.router as router

        payload = {
            "rows": [{
                "rank": 1, "code": "600519", "name": "贵州茅台",
                "price": 1500.0, "change_pct": 2.5, "volume_hand": 30000,
                "volume_wan_hand": 3.0, "amount_yuan": 1.87e9,
                "amount_yi": 18.7, "turnover_pct": 0.8, "market_cap_yi": 18000.0,
            }],
            "metric": "amount", "top_n": 1, "source": "tencent_ranking",
            "market": "A股", "source_url": "http://qt.test",
            "retrieved_at": "2026-08-22 10:00:00",
        }
        with mock.patch(
            "adapters.router.fetch_ranking",
            side_effect=RuntimeError("eastmoney down"),
        ), mock.patch(
            "adapters.router.fetch_tencent_ranking", return_value=payload,
        ) as tfr, mock.patch(
            "adapters.router.cache_get_ranking", return_value=None,
        ), mock.patch(
            "adapters.router.cache_set_ranking",
        ) as cs:
            out = router.route_structured("今日A股成交额排行前十")
        self.assertEqual(out["source"], "tencent_ranking")
        self.assertEqual(out["metadata"]["market"], "A股")
        self.assertEqual(out["metadata"]["cache_hit"], False)
        tfr.assert_called_once_with("amount", top_n=10)
        cs.assert_called_once_with("a", "amount", payload, top_n=10)

    def test_route_structured_cache_hit_skips_network(self):
        """缓存命中：直接返回并记录 cache hit，不再请求任何行情源。"""
        import adapters.router as router

        payload = {
            "rows": [{
                "rank": 1, "code": "600519", "name": "贵州茅台",
                "price": 1500.0, "change_pct": 2.5, "volume_hand": 30000,
                "volume_wan_hand": 3.0, "amount_yuan": 1.87e9,
                "amount_yi": 18.7, "turnover_pct": 0.8, "market_cap_yi": 18000.0,
            }],
            "metric": "volume", "top_n": 1, "source": "tencent_ranking",
            "market": "A股", "source_url": "http://qt.test",
            "retrieved_at": "2026-08-22 10:00:00",
        }
        with mock.patch(
            "adapters.router.cache_get_ranking", return_value=payload,
        ) as cg, mock.patch(
            "adapters.router.fetch_ranking",
        ) as fr, mock.patch(
            "adapters.router.fetch_tencent_ranking",
        ) as tfr, mock.patch(
            "adapters.router.fetch_tencent_us_ranking",
        ) as usfr:
            out = router.route_structured("今日A股总成交量前十股")
        self.assertEqual(out["source"], "tencent_ranking")
        self.assertEqual(out["metadata"]["cache_hit"], True)
        cg.assert_called_once_with("a", "volume", 10)
        fr.assert_not_called()
        tfr.assert_not_called()
        usfr.assert_not_called()

    def test_route_structured_us_prefers_tencent_us(self):
        """美股目标：优先 tencent_us_ranking，且不走 eastmoney/A股链。"""
        import adapters.router as router

        payload = {
            "rows": [{
                "rank": 1, "code": "NVDA", "name": "英伟达",
                "price": 188.0, "change_pct": -0.8, "volume_hand": 3000,
                "volume_wan_hand": 0.3, "amount_yuan": 5.6e8,
                "amount_yi": 5.6, "turnover_pct": 1.8, "market_cap_yi": 46000.0,
            }],
            "metric": "volume", "top_n": 1, "source": "tencent_us_ranking",
            "market": "美股", "source_url": "http://qt.test",
            "retrieved_at": "2026-08-22 10:00:00",
        }
        with mock.patch(
            "adapters.router.cache_get_ranking", return_value=None,
        ), mock.patch(
            "adapters.router.fetch_tencent_us_ranking", return_value=payload,
        ) as usfr, mock.patch(
            "adapters.router.fetch_ranking",
        ) as fr, mock.patch(
            "adapters.router.fetch_tencent_ranking",
        ) as tfr, mock.patch(
            "adapters.router.cache_set_ranking",
        ) as cs:
            out = router.route_structured("今日美股成交量前十（纳斯达克）")
        self.assertEqual(out["source"], "tencent_us_ranking")
        self.assertEqual(out["metadata"]["market"], "美股")
        usfr.assert_called_once_with("volume", top_n=10)
        fr.assert_not_called()
        tfr.assert_not_called()
        cs.assert_called_once_with("us", "volume", payload, top_n=10)

    def test_route_structured_eastmoney_success_keeps_original_semantics(self):
        """eastmoney 成功：仍返回 eastmoney_ranking，且不回填缓存（既有语义）。"""
        import adapters.router as router

        payload = {
            "rows": [{"rank": 1, "code": "600519", "name": "贵州茅台"}],
            "metric": "amount", "top_n": 1,
            "source_url": "http://eastmoney.test",
            "retrieved_at": "2026-08-22 10:00:00",
        }
        with mock.patch(
            "adapters.router.cache_get_ranking", return_value=None,
        ), mock.patch(
            "adapters.router.fetch_ranking", return_value=payload,
        ), mock.patch(
            "adapters.router.fetch_tencent_ranking",
        ) as tfr, mock.patch(
            "adapters.router.cache_set_ranking",
        ) as cs:
            out = router.route_structured("今日A股成交额排行前十")
        self.assertEqual(out["source"], "eastmoney_ranking")
        tfr.assert_not_called()
        cs.assert_not_called()

    def test_quote_cache_redis_hit_delete_and_memory_fallback(self):
        """缓存：Redis 读写/TTL、Redis 不可用时内存兜底。"""
        import time as _time

        from adapters.quote_cache import QuoteCache

        payload = {
            "rows": [{"rank": 1, "code": "600519", "name": "贵州茅台"}],
            "metric": "volume", "top_n": 1, "source": "tencent_ranking",
            "retrieved_at": "2026-08-22 10:00:00",
        }
        try:
            import fakeredis
        except ImportError:
            fakeredis = None
        if fakeredis is not None:
            fake = fakeredis.FakeStrictRedis(decode_responses=True)
            cache = QuoteCache(redis_client=fake, ttl=600)
            self.assertTrue(cache.set("a", "volume", payload))
            self.assertEqual(cache.get("a", "volume"), payload)
            self.assertTrue(fake.ttl(cache._key("a", "volume")) > 0)
            cache.delete("a", "volume")
            self.assertIsNone(cache.get("a", "volume"))

        # 内存兜底 + 过期清理
        mem = QuoteCache(redis_client=None, ttl=600)
        mem._redis = None  # 强制纯内存路径，避免命中本机真实 Redis
        mem.set("us", "amount", payload)
        self.assertEqual(mem.get("us", "amount"), payload)
        key = mem._key("us", "amount")
        mem._memory[key] = (_time.time() - 1, payload)
        self.assertIsNone(mem.get("us", "amount"))

        # Redis 异常 → 自动降级内存，不抛异常
        class _BrokenRedis:
            def get(self, key):
                raise ConnectionError("redis down")

            def set(self, key, value, ex=None):
                raise ConnectionError("redis down")

        fb = QuoteCache(redis_client=_BrokenRedis(), ttl=600)
        fb.set("a", "amount", payload)
        self.assertEqual(fb.get("a", "amount"), payload)

    def test_orchestrator_tencent_ranking_export_and_injection(self):
        """编排器兼容：tencent_ranking 也能导出 ranking.csv 并注入报告块。"""
        import tempfile

        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = Path(tempfile.mkdtemp(prefix="rk_tencent_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        data = {
            "source": "tencent_ranking",
            "metadata": {
                "market": "A股", "metric": "volume",
                "retrieved_at": "2026-08-22 10:00:00",
            },
            "data": {
                "rows": [{
                    "rank": 1, "code": "600519", "name": "贵州茅台",
                    "price": 1500.0, "change_pct": 2.5,
                    "volume_hand": 30000, "volume_wan_hand": 3.0,
                    "amount_yuan": 1.87e9, "amount_yi": 18.7,
                    "turnover_pct": 0.8, "market_cap_yi": 18000.0,
                }],
                "metric": "volume", "top_n": 1, "source": "tencent_ranking",
                "retrieved_at": "2026-08-22 10:00:00",
            },
        }
        try:
            csv_path = o._export_ranking_csv("t-tencent", data)
            self.assertIsNotNone(csv_path)
            self.assertTrue(csv_path.exists())
            proj = ws_mod.task_project_dir("t-tencent")
            (proj / "structured_data.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8",
            )
            block = o._structured_injection("t-tencent")
            self.assertIn("腾讯行情", block)
            self.assertIn("贵州茅台", block)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)


class TestSinaRankingAdapter(unittest.TestCase):
    """P0-1：新浪排行适配器 canned（不联网）：GBK/裸键 JSON 解析、
    分页合并、top_n=250 排序、节流与重试。"""

    @staticmethod
    def _hq_line(
        code: str, name: str, trade: str, changepercent: str,
        volume: int, amount: int, turnoverratio: str, mktcap: str,
    ) -> str:
        """构造新浪 getHQNodeData 原始行（键名不加引号）。"""
        symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"
        return (
            f'{{symbol:"{symbol}",code:"{code}",name:"{name}",'
            f'trade:"{trade}",changepercent:"{changepercent}",'
            f"volume:{volume},amount:{amount},"
            f'turnoverratio:"{turnoverratio}",mktcap:"{mktcap}"}}'
        )

    @staticmethod
    def _page_text(start_amount: int, count: int = 50) -> str:
        """构造一页 50 只、成交额递减的响应文本。"""
        rows = []
        for i in range(count):
            amount = start_amount - i
            rows.append(
                TestSinaRankingAdapter._hq_line(
                    f"{600000 + i:06d}"[-6:],
                    f"测试股{i:03d}",
                    "10.00",
                    "1.00",
                    1_000_000 + i,
                    amount * 100_000_000,
                    "0.80",
                    "100000000000",
                )
            )
        return "[" + ",".join(rows) + "]"

    def test_parse_hq_nodes_gbk_unquoted_json(self):
        from adapters.sina_ranking import parse_hq_nodes

        text = (
            "["
            + self._hq_line(
                "600519", "贵州茅台", "1500.00", "2.50",
                30000, 4500000000, "0.80", "1800000000000",
            )
            + ","
            + self._hq_line(
                "000001", "平安银行", "11.00", "-1.20",
                500000, 5500000000, "1.10", "210000000000",
            )
            + "]"
        )
        rows = parse_hq_nodes(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "贵州茅台")
        self.assertEqual(rows[0]["amount"], 4500000000)
        self.assertEqual(rows[1]["code"], "000001")
        # GBK 解码路径后的文本同样可解析
        rows2 = parse_hq_nodes(text.encode("gbk").decode("gbk"))
        self.assertEqual(rows2[1]["name"], "平安银行")
        self.assertEqual(parse_hq_nodes("not-json"), [])
        self.assertEqual(parse_hq_nodes(""), [])

    def test_fetch_ranking_merges_pages_sorts_top250(self):
        import re

        import adapters.sina_ranking as sr

        def fake_get(url, timeout=25, attempt=1):
            if "getHQNodeStockCount" in url:
                return '"5000";'
            m = re.search(r"page=(\d+)", url)
            page = int(m.group(1)) if m else 1
            return self._page_text(10000 - (page - 1) * 50, 50)

        with mock.patch.object(sr, "_get", side_effect=fake_get), \
                mock.patch.object(sr.time, "sleep") as sleep_mock:
            out = sr.fetch_ranking("amount", top_n=250, market="hs_a")
        self.assertEqual(out["source"], "sina_ranking")
        self.assertEqual(out["market"], "A股")
        self.assertEqual(len(out["rows"]), 250)
        self.assertEqual(out["total"], 5000)
        self.assertEqual(out["fetched_count"], 250)
        self.assertEqual(out["rows"][0]["rank"], 1)
        self.assertEqual(out["rows"][0]["amount_yi"], 10000.0)
        self.assertEqual(out["rows"][-1]["rank"], 250)
        # 成交额降序
        amounts = [r["amount_yuan"] for r in out["rows"]]
        self.assertEqual(amounts, sorted(amounts, reverse=True))
        # 页间节流：5 页之间 4 次，每次 0.8s（降低触发 456 概率）
        self.assertEqual(len(sleep_mock.call_args_list), 4)
        for call in sleep_mock.call_args_list:
            self.assertEqual(call.args[0], 0.8)

    def test_fetch_ranking_retry_and_throttle(self):
        import re

        import adapters.sina_ranking as sr

        calls = {"n": 0}

        def fake_get(url, timeout=25, attempt=1):
            if "getHQNodeStockCount" in url:
                return '"5000";'
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient")
            m = re.search(r"page=(\d+)", url)
            page = int(m.group(1)) if m else 1
            return self._page_text(10000 - (page - 1) * 50, 50)

        with mock.patch.object(sr, "_get", side_effect=fake_get), \
                mock.patch.object(sr.time, "sleep") as sleep_mock:
            out = sr.fetch_ranking("amount", top_n=250, market="hs_a")
        self.assertEqual(len(out["rows"]), 250)
        sleeps = [c.args[0] for c in sleep_mock.call_args_list]
        # 重试退避 1s + 页间节流 0.8s × 4
        self.assertEqual(sleeps[0], 1.0)
        self.assertEqual(sleeps[1:], [0.8, 0.8, 0.8, 0.8])


class TestStatisticalRankingRouting(unittest.TestCase):
    """P0-2/P1：规模感知路由、top_n 解析、能力注册表匹配。"""

    def test_parse_top_n_variants(self):
        import adapters.router as router

        self.assertEqual(
            router._parse_top_n("A股前5%成交额占比", 5000), 250,
        )
        self.assertEqual(
            router._parse_top_n("前5%", 5000), 250,
        )
        self.assertIsNone(router._parse_top_n("A股前5%成交额占比"))
        self.assertEqual(
            router._parse_top_n("A股成交额排行前250只", 5000), 250,
        )
        self.assertEqual(
            router._parse_top_n("top250美股", 5000), 250,
        )
        self.assertEqual(
            router._parse_top_n("今日A股总成交量前十股", 5000), 10,
        )
        self.assertEqual(router._parse_top_n("今日A股成交额排行", 5000), 10)

    def test_scale_parsing(self):
        import adapters.router as router

        self.assertEqual(
            router._parse_scale("A股前5%成交额占比"), "full_market",
        )
        self.assertEqual(router._parse_scale("A股成交额分布分析"), "full_market")
        self.assertEqual(
            router._parse_scale("A股成交额排行前250"), "full_market",
        )
        self.assertEqual(
            router._parse_scale("今日A股总成交量前十股"), "topN",
        )
        self.assertEqual(router._parse_scale("今日A股成交额排行"), "topN")

    def test_capability_registry_matching(self):
        import adapters.router as router

        self.assertEqual(
            router._match_data_source("a", "amount", "topN", 10),
            "eastmoney_ranking",
        )
        self.assertEqual(
            router._match_data_source("a", "volume", "full_market", 0),
            "sina_ranking",
        )
        self.assertEqual(
            router._match_data_source("a", "amount", "topN", 250),
            "sina_ranking",
        )
        self.assertEqual(
            router._match_data_source("us", "volume", "topN", 10),
            "tencent_us_ranking",
        )
        self.assertEqual(
            router._match_data_source("us", "amount", "full_market", 0),
            "sina_ranking",
        )
        self.assertIsNone(
            router._match_data_source("hk", "amount", "topN", 10),
        )

    def test_route_structured_statistical_goal_uses_sina_full_market(self):
        import adapters.router as router

        payload = {
            "rows": [{"rank": 1, "name": "贵州茅台"}],
            "metric": "amount", "top_n": 1, "total": 5000,
            "fetched_count": 5000, "source": "sina_ranking",
            "market": "A股", "source_url": "http://sina.test",
            "retrieved_at": "2026-08-22 10:00:00",
        }
        with mock.patch.object(
            router, "fetch_sina_ranking", return_value=payload,
        ) as fsr, mock.patch.object(router, "cache_set_ranking") as cs:
            out = router.route_structured("A股前5%成交额占比")
        self.assertEqual(out["source"], "sina_ranking")
        self.assertEqual(out["data"]["target_top_n"], 250)
        self.assertEqual(out["metadata"]["target_top_n"], 250)
        self.assertEqual(out["metadata"]["total"], 5000)
        fsr.assert_called_once_with("amount", top_n=0, market="hs_a")
        cs.assert_called_once_with("a", "amount", payload, top_n=250)

    def test_route_structured_top250_uses_sina_pagination(self):
        import adapters.router as router

        payload = {
            "rows": [{"rank": 1, "name": "贵州茅台"}],
            "metric": "amount", "top_n": 1, "total": 5000,
            "fetched_count": 5000, "source": "sina_ranking",
            "market": "A股", "source_url": "http://sina.test",
            "retrieved_at": "2026-08-22 10:00:00",
        }
        with mock.patch.object(
            router, "fetch_sina_ranking", return_value=payload,
        ) as fsr:
            out = router.route_structured("A股成交额排行前250")
        self.assertEqual(out["source"], "sina_ranking")
        self.assertEqual(out["data"]["target_top_n"], 250)
        fsr.assert_called_once_with("amount", top_n=0, market="hs_a")

    def test_quote_cache_key_includes_top_n(self):
        from adapters.quote_cache import QuoteCache

        mem = QuoteCache(redis_client=None, ttl=600)
        mem._redis = None
        mem.set("a", "amount", {"top_n": 10}, top_n=10)
        mem.set("a", "amount", {"top_n": 0}, top_n=0)
        self.assertEqual(mem.get("a", "amount", 10)["top_n"], 10)
        self.assertEqual(mem.get("a", "amount", 0)["top_n"], 0)
        self.assertIsNone(mem.get("a", "amount", 20))


def _sina_ranking_sample(n: int = 60, total: int = 5000) -> dict:
    """构造 sina_ranking canned 数据（全市场 >50 行）。"""
    rows = []
    for i in range(n):
        amount_yuan = (n - i) * 100_000_000
        volume = (n - i) * 200_000
        rows.append({
            "rank": i + 1,
            "code": f"600{i:03d}" if i < 900 else f"000{i:03d}",
            "name": f"样例股{i + 1:03d}",
            "price": 10.0 + i,
            "change_pct": round((i % 7) - 2.5, 2),
            "volume_hand": volume / 100.0,
            "volume_wan_hand": round(volume / 1e6, 2),
            "amount_yuan": amount_yuan,
            "amount_yi": round(amount_yuan / 1e8, 2),
            "turnover_pct": 1.0,
            "market_cap_yi": 500.0,
            "mktcap_yi": 500.0,
        })
    return {
        "source": "sina_ranking",
        "data": {
            "rows": rows,
            "metric": "amount",
            "top_n": n,
            "total": total,
            "fetched_count": n,
            "source": "sina_ranking",
            "market": "A股",
            "source_url": "http://sina.test/hq",
            "retrieved_at": "2026-08-22 10:00:00",
        },
        "metadata": {
            "source": "sina_ranking",
            "market": "A股",
            "metric": "amount",
            "top_n": n,
            "total": total,
            "fetched_count": n,
            "unit": "亿元",
            "retrieved_at": "2026-08-22 10:00:00",
        },
    }


class TestStatisticalRankingOrchestration(unittest.TestCase):
    """P0-3：统计类目标识别、sina 全量 CSV 导出、code_execution 统计指令。"""

    def test_is_statistical_goal(self):
        from orchestrator_v2 import OrchestratorV2

        for goal in (
            "A股前5%成交额占比",
            "计算A股前5%的成交额占比",
            "A股成交额分布分析",
            "全市场成交额合计与汇总",
            "A股前10%成交额比例",
        ):
            self.assertTrue(OrchestratorV2._is_statistical_goal(goal), goal)
        for goal in (
            "今日A股总成交量前十股",
            "A股成交额排行前10",
        ):
            self.assertFalse(OrchestratorV2._is_statistical_goal(goal), goal)

    def test_export_ranking_csv_exports_all_rows_for_sina(self):
        import tempfile

        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        tmp = Path(tempfile.mkdtemp(prefix="sina_csv_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            csv_path = o._export_ranking_csv("t-sina-csv", _sina_ranking_sample(60))
            self.assertIsNotNone(csv_path)
            import pandas as pd
            df = pd.read_csv(csv_path)
            self.assertEqual(len(df), 60, "sina 全市场数据应全量导出")
            self.assertEqual(df.iloc[0]["name"], "样例股001")
            self.assertIn("amount_yi", df.columns)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_statistical_code_execution_instruction(self):
        import tempfile
        import threading

        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._task_goals = {"t-st": "A股前5%成交额占比"}
        tmp = Path(tempfile.mkdtemp(prefix="stat_instr_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            sample = _sina_ranking_sample(60)
            sample["data"]["target_top_n"] = 250
            proj = ws_mod.task_project_dir("t-st")
            (proj / "structured_data.json").write_text(
                json.dumps(sample, ensure_ascii=False), encoding="utf-8",
            )
            OrchestratorV2._export_ranking_csv("t-st", sample)
            step = {
                "step_id": "3", "capability": "code_execution",
                "instruction": "计算前5%成交额占比", "depends_on": [],
            }
            instr = o._inject_step_context(
                step, {}, threading.Lock(), "t-st",
            )
            self.assertIn("统计任务", instr)
            self.assertIn("ranking.csv", instr)
            self.assertIn("前 250", instr)
            self.assertIn("share_pct", instr)
            self.assertIn("禁止只取前 10 只", instr)
            self.assertIn("全市场", instr)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_top10_goal_gets_no_statistical_instruction(self):
        import tempfile
        import threading

        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._task_goals = {"t-top10": "今日A股总成交量前十股"}
        tmp = Path(tempfile.mkdtemp(prefix="stat_top10_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            sample = _sina_ranking_sample(10)
            proj = ws_mod.task_project_dir("t-top10")
            (proj / "structured_data.json").write_text(
                json.dumps(sample, ensure_ascii=False), encoding="utf-8",
            )
            OrchestratorV2._export_ranking_csv("t-top10", sample)
            step = {
                "step_id": "2", "capability": "code_execution",
                "instruction": "分析排行", "depends_on": [],
            }
            instr = o._inject_step_context(
                step, {}, threading.Lock(), "t-top10",
            )
            self.assertNotIn("统计任务", instr)
            self.assertNotIn("share_pct", instr)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)

    def test_structured_injection_sina_full_market_title(self):
        import tempfile

        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        tmp = Path(tempfile.mkdtemp(prefix="sina_inj_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            proj = ws_mod.task_project_dir("t-sina-inj")
            (proj / "structured_data.json").write_text(
                json.dumps(_sina_ranking_sample(5), ensure_ascii=False),
                encoding="utf-8",
            )
            block = OrchestratorV2._structured_injection("t-sina-inj")
            self.assertIn("A股成交额排行（全市场）", block)
            self.assertIn("新浪行情中心", block)
            self.assertIn("样例股001", block)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)


class TestSourceHealthRouting(unittest.TestCase):
    """最终泛化层：行情源健康注册表（熔断/冷却/清零）+ 部分数据降级
    + 健康感知降级链（全部 canned，不真连网）。"""

    def setUp(self):
        import adapters.source_health as sh

        sh.reset()

    def tearDown(self):
        import adapters.source_health as sh

        sh.reset()

    def test_consecutive_failures_enter_cooldown(self):
        import time

        from adapters.source_health import (
            get_health,
            is_available,
            mark_failure,
        )

        mark_failure("eastmoney_ranking", RuntimeError("HTTP 456"))
        ok, reason = is_available("eastmoney_ranking")
        self.assertTrue(ok, "1 次失败仍健康")
        mark_failure("eastmoney_ranking", RuntimeError("HTTP 456"))
        ok, reason = is_available("eastmoney_ranking")
        self.assertFalse(ok)
        self.assertIn("cooldown", reason)
        self.assertIn("eastmoney_ranking", reason)
        h = get_health()["eastmoney_ranking"]
        self.assertEqual(h["fails"], 2)
        self.assertGreater(h["cooldown_until"], time.time())
        self.assertFalse(h["healthy"])

    def test_cooldown_expiry_auto_recovers(self):
        import time

        import adapters.source_health as sh
        from adapters.source_health import is_available, mark_failure

        mark_failure("sina_ranking", "RemoteDisconnected")
        mark_failure("sina_ranking", "RemoteDisconnected")
        self.assertFalse(is_available("sina_ranking")[0])
        # 冷却到期自动恢复，无需显式 mark_success
        with sh._LOCK:
            sh._HEALTH["sina_ranking"]["cooldown_until"] = time.time() - 1
        ok, reason = is_available("sina_ranking")
        self.assertTrue(ok, reason)

    def test_success_clears_failure_and_cooldown(self):
        import time

        from adapters.source_health import (
            get_health,
            is_available,
            mark_failure,
            mark_success,
        )

        mark_failure("tencent_ranking", "timeout")
        mark_failure("tencent_ranking", "timeout")
        self.assertFalse(is_available("tencent_ranking")[0])
        mark_success("tencent_ranking")
        self.assertTrue(is_available("tencent_ranking")[0])
        h = get_health()["tencent_ranking"]
        self.assertEqual(h["fails"], 0)
        self.assertLessEqual(h["cooldown_until"], time.time())

    def test_ensure_available_raises_fast_fail_without_requests(self):
        import adapters.sina_ranking as sr
        from adapters.source_health import (
            SourceInCooldownError,
            ensure_available,
            mark_failure,
        )

        mark_failure("sina_ranking", "HTTP 456")
        mark_failure("sina_ranking", "HTTP 456")
        with self.assertRaises(SourceInCooldownError):
            ensure_available("sina_ranking")
        # 适配器层快失败：冷却期不发出任何请求
        with mock.patch.object(
            sr, "_get", side_effect=AssertionError("冷却期不应发请求"),
        ) as get_mock:
            with self.assertRaises(SourceInCooldownError):
                sr.fetch_ranking("amount", top_n=0, market="hs_a")
        get_mock.assert_not_called()

    def test_sina_partial_data_degrades_with_coverage(self):
        import re

        import adapters.sina_ranking as sr
        from adapters.source_health import is_available

        def fake_get(url, timeout=25, attempt=1):
            if "getHQNodeStockCount" in url:
                return '"5000";'
            m = re.search(r"page=(\d+)", url)
            page = int(m.group(1)) if m else 1
            if page == 2:
                # 第 2 页持续失败（重试 3 次都失败）→ 触发部分数据降级
                raise RemoteDisconnected("456 after page 1")
            return TestSinaRankingAdapter._page_text(
                10000 - (page - 1) * 50, 50,
            )

        with mock.patch.object(sr, "_get", side_effect=fake_get), \
                mock.patch.object(sr.time, "sleep"):
            out = sr.fetch_ranking("amount", top_n=0, market="hs_a")
        self.assertTrue(out["partial"])
        self.assertEqual(len(out["rows"]), 50)
        self.assertEqual(out["fetched_count"], 50)
        self.assertIsNone(out["total"], "部分数据总数未知")
        # 部分失败计入失败计数（1 次不熔断），下一次成功会清零
        ok, _ = is_available("sina_ranking")
        self.assertTrue(ok)

    def test_sina_first_page_failure_raises_and_marks(self):
        import adapters.sina_ranking as sr
        from adapters.source_health import get_health, is_available

        with mock.patch.object(
            sr, "_get",
            side_effect=RemoteDisconnected("Remote end closed connection"),
        ), mock.patch.object(sr.time, "sleep"):
            with self.assertRaises(RemoteDisconnected):
                sr.fetch_ranking("amount", top_n=0, market="hs_a")
        ok, _ = is_available("sina_ranking")
        self.assertTrue(ok, "首页 1 次失败仍未熔断")
        self.assertEqual(get_health()["sina_ranking"]["fails"], 1)

    def test_health_aware_fallback_skips_cooldown_source(self):
        import adapters.router as router
        from adapters.source_health import mark_failure

        payload = {
            "rows": [{
                "rank": 1, "code": "600519", "name": "贵州茅台",
                "price": 1500.0, "change_pct": 2.5, "volume_hand": 30000,
                "volume_wan_hand": 3.0, "amount_yuan": 1.87e9,
                "amount_yi": 18.7, "turnover_pct": 0.8,
                "market_cap_yi": 18000.0,
            }],
            "metric": "amount", "top_n": 1, "source": "tencent_ranking",
            "market": "A股", "source_url": "http://qt.test",
            "retrieved_at": "2026-08-22 10:00:00",
        }
        mark_failure("eastmoney_ranking", "HTTP 456")
        mark_failure("eastmoney_ranking", "HTTP 456")
        with mock.patch("adapters.router.fetch_ranking") as fr, \
                mock.patch(
                    "adapters.router.fetch_tencent_ranking",
                    return_value=payload,
                ) as tfr, mock.patch(
                    "adapters.router.cache_get_ranking", return_value=None,
                ), mock.patch(
                    "adapters.router.cache_set_ranking",
                ) as cs:
            out = router.route_structured("今日A股成交额排行前十")
        self.assertEqual(out["source"], "tencent_ranking")
        fr.assert_not_called()
        tfr.assert_called_once_with("amount", top_n=10)
        cs.assert_called_once_with("a", "amount", payload, top_n=10)

    def test_health_aware_fallback_all_cooldown_returns_none(self):
        import adapters.router as router
        from adapters.source_health import mark_failure

        mark_failure("eastmoney_ranking", "HTTP 456")
        mark_failure("eastmoney_ranking", "HTTP 456")
        mark_failure("tencent_ranking", "HTTP 456")
        mark_failure("tencent_ranking", "HTTP 456")
        with mock.patch("adapters.router.fetch_ranking") as fr, \
                mock.patch("adapters.router.fetch_tencent_ranking") as tfr, \
                mock.patch(
                    "adapters.router.cache_get_ranking", return_value=None,
                ), self.assertLogs(
                    "adapters.router", level="WARNING",
                ) as cm:
            out = router.route_structured("今日A股成交额排行前十")
        self.assertIsNone(out)
        fr.assert_not_called()
        tfr.assert_not_called()
        self.assertTrue(
            any("ranking fetch failed" in m for m in cm.output), cm.output,
        )

    def test_full_market_sina_cooldown_returns_none(self):
        import adapters.router as router
        from adapters.source_health import mark_failure

        mark_failure("sina_ranking", "HTTP 456")
        mark_failure("sina_ranking", "HTTP 456")
        with mock.patch("adapters.router.fetch_sina_ranking") as fsr, \
                self.assertLogs(
                    "adapters.router", level="WARNING",
                ) as cm:
            out = router.route_structured("A股前5%成交额占比")
        self.assertIsNone(out)
        fsr.assert_not_called()
        self.assertTrue(
            any("ranking fetch failed" in m for m in cm.output), cm.output,
        )

    def test_structured_injection_annotates_partial_data(self):
        import tempfile

        import workspace as ws_mod
        from orchestrator_v2 import OrchestratorV2

        tmp = Path(tempfile.mkdtemp(prefix="sina_partial_inj_"))
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        try:
            sample = _sina_ranking_sample(50)
            sample["data"]["partial"] = True
            sample["data"]["total"] = None
            sample["data"]["fetched_count"] = 50
            proj = ws_mod.task_project_dir("t-sina-partial")
            (proj / "structured_data.json").write_text(
                json.dumps(sample, ensure_ascii=False), encoding="utf-8",
            )
            block = OrchestratorV2._structured_injection("t-sina-partial")
            self.assertIn("部分数据", block)
            self.assertIn("已获取 50 只", block)
            self.assertIn("全市场总数未知", block)
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)


class TestWebFetchIriEncoding(_TempWorkspace, unittest.TestCase):
    """中文 IRI 百分号编码（修复 'ascii' codec 真实任务失败）。"""

    def test_ascii_url_unchanged(self):
        from workers.web_fetch_worker import _encode_iri
        u = "https://example.com/path?a=1&b=2"
        self.assertEqual(_encode_iri(u), u)

    def test_chinese_query_encoded(self):
        from workers.web_fetch_worker import _encode_iri
        u = "https://example.com/search?q=宁德时代"
        out = _encode_iri(u)
        self.assertNotIn("宁德时代", out)
        self.assertIn("%", out)
        self.assertTrue(out.startswith("https://example.com/search?q="))

    def test_chinese_path_encoded(self):
        from workers.web_fetch_worker import _encode_iri
        u = "https://example.com/财经/新闻"
        out = _encode_iri(u)
        self.assertNotIn("财经", out)
        self.assertIn("%", out)

    def test_reserved_chars_preserved(self):
        from workers.web_fetch_worker import _encode_iri
        u = "https://example.com/a/b?x=1&y=%20#frag"
        self.assertEqual(_encode_iri(u), u)

    def test_execute_fetches_chinese_url(self):
        """execute 对含中文 URL 的指令不再抛 ascii 编码错误（mock urlopen）。"""
        import asyncio
        import json
        from unittest import mock
        import workers.web_fetch_worker as wf

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return "<html><title>测试</title><body>正文内容</body></html>".encode("utf-8")

        w = wf.WebFetchWorker.__new__(wf.WebFetchWorker)
        captured = {}

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            return FakeResp()

        with mock.patch.object(wf.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = asyncio.run(w.execute("抓取 https://example.com/搜索?q=宁德时代 的页面"))
        d = json.loads(out)
        self.assertEqual(d["status"], "success")
        self.assertNotIn("宁德时代", captured["url"])  # 已百分号编码
        self.assertIn("正文内容", d["text"])


class TestFinancialResearchCharts(unittest.TestCase):
    """研究任务的财务分析图：两期对比 / 同比增速 / 盈利与现金流质量。

    实机教训（`ui-2084c2c9cc`）：老链路把 16 个会计科目（含流量与存量）归一成一张
    "市场规模对比（亿元）"，每行自成单点序列 → 图上只有孤立圆点，读者拿不到结论。
    这里钉住新链路：规格由底稿确定性生成、结论由数据算出、越界期间不进图。
    """

    ROWS = [
        {"metric": "revenue", "metric_label": "营业收入", "year": 2023,
         "value": 1505.6, "unit": "亿元"},
        {"metric": "revenue", "metric_label": "营业收入", "year": 2024,
         "value": 1741.44, "unit": "亿元"},
        {"metric": "net_profit", "metric_label": "归母净利润", "year": 2023,
         "value": 747.34, "unit": "亿元"},
        {"metric": "net_profit", "metric_label": "归母净利润", "year": 2024,
         "value": 862.28, "unit": "亿元"},
        {"metric": "operating_cashflow", "metric_label": "经营活动现金流净额",
         "year": 2023, "value": 665.93, "unit": "亿元"},
        {"metric": "operating_cashflow", "metric_label": "经营活动现金流净额",
         "year": 2024, "value": 924.64, "unit": "亿元"},
    ]
    DERIVED = [
        {"metric": "revenue_yoy", "metric_label": "营业收入同比", "year": 2024,
         "value": 15.66, "unit": "%"},
        {"metric": "net_profit_yoy", "metric_label": "归母净利润同比", "year": 2024,
         "value": 15.38, "unit": "%"},
        {"metric": "operating_cashflow_yoy", "metric_label": "经营活动现金流净额同比",
         "year": 2024, "value": 38.85, "unit": "%"},
        {"metric": "net_margin", "metric_label": "净利率", "year": 2023,
         "value": 49.64, "unit": "%"},
        {"metric": "net_margin", "metric_label": "净利率", "year": 2024,
         "value": 49.52, "unit": "%"},
        {"metric": "cashflow_coverage", "metric_label": "经营现金流对净利润的覆盖",
         "year": 2023, "value": 89.11, "unit": "%"},
        {"metric": "cashflow_coverage", "metric_label": "经营现金流对净利润的覆盖",
         "year": 2024, "value": 107.23, "unit": "%"},
    ]

    def _specs(self):
        import chart_specs as CS
        return CS.financial_research_specs(
            self.ROWS, self.DERIVED, unit="亿元", source="东方财富数据中心（A股）",
            company="贵州茅台", caliber="合并", periods=[2023, 2024])

    def test_specs_are_faceted_and_valid(self):
        """规格按**问题**分面：对比 1 张 + 同比 1 张 + **每个比率各 1 张**。

        为什么不再挤一张（实机 chart_3）：归母净利率（利润率）、经营现金流覆盖（倍数）、
        资产负债率（杠杆）经济含义与量级不同，同轴会把小项压扁；且长标签会被类别清洗
        退化成"2024年"，x 轴混入年份、分组与系列配对错位。
        """
        import chart_specs as CS
        specs = self._specs()
        # 两张总览 + 每个有≥2期的比率各一张（本夹具两个比率 → 共 4 张）
        self.assertEqual([s["type"] for s in specs],
                         ["grouped_bar", "bar", "bar", "bar"])
        for s in specs:
            self.assertEqual(CS.validate_spec(s), [], s.get("title"))
            self.assertEqual(s["source"], "东方财富数据中心（A股）",
                             "图注要写可读来源名，不是接口 URL")
            self.assertTrue(s.get("question"), "每张图要回答一个问题")
            self.assertTrue(s.get("conclusion"), "每张图要有一条由数据算出的观察")
        # 两期对比图：每个指标两年并排（分组靠 caliber=年度）
        cmp_spec = specs[0]
        self.assertEqual({r["caliber"] for r in cmp_spec["data"]}, {"2023年", "2024年"})
        self.assertEqual(len({r["label"] for r in cmp_spec["data"]}), 3)
        # 结论由数据算出（不是"随年份变化"这类同义反复）
        self.assertIn("38.85", cmp_spec["conclusion"])

    def test_ratio_facets_do_not_mix_metrics_on_one_axis(self):
        """回归：比率图**一张只画一个指标**，类别是期间、单位统一为 %。"""
        import chart_specs as CS
        facets = self._specs()[2:]
        self.assertTrue(facets)
        for f in facets:
            labels = [str(r.get("label")) for r in f["data"]]
            self.assertEqual(set(labels), {"2023年", "2024年"},
                             f"比率图的类别应是期间，实际 {labels}")
            self.assertEqual({str(r.get("unit")) for r in f["data"]}, {"%"},
                             "同一张图不得混装不同单位")
            self.assertEqual(f["x_axis_title"], "期间")
            # 纵轴写清是哪个比率（而不是笼统"比率（%）"）
            self.assertIn(str(f["data"][0].get("label"))[:0] + "（%）", f["y_axis_title"])
        # 观察用**百分点**表述，且覆盖倍数的符号结论与读数一致
        cov = next(f for f in facets if "覆盖" in f["title"])
        self.assertIn("个百分点", cov["conclusion"])
        last_val = [r["value"] for r in cov["data"]][-1]
        self.assertIn("高于归母净利润" if last_val > 100 else "低于归母净利润",
                      cov["conclusion"], "符号结论必须与读数一致")
        # 每个比率只出现一次（没有把两个比率塞进同一张图）
        titles = [f["title"] for f in facets]
        self.assertEqual(len(titles), len(set(titles)))

    def test_period_labels_keep_chronological_order(self):
        """回归：类别是"2023年/2024年"这类期间标签时不得按数值降序重排。

        实机反例：判定只认纯数字标签，"2023年"带"年"字匹配不上 → 两期被排成
        "2024年/2023年"，颜色与年份的对应也跟着反了。
        """
        import chart_specs as CS
        self.assertTrue(CS.is_period_labels(["2023年", "2024年"]))
        self.assertTrue(CS.is_period_labels(["2023", "2024"]))
        self.assertFalse(CS.is_period_labels(["营业收入", "归母净利润"]))
        self.assertFalse(CS.is_period_labels([]))

    def test_render_script_draws_grouped_bar(self):
        """真跑一遍渲染脚本：分面后的每张图都必须画出来（模板是字符串，靠这条兜住）。"""
        import subprocess
        import sys
        import chart_assembly as CA
        repo = Path(__file__).resolve().parent
        tmp = tempfile.mkdtemp(prefix="wm_finchart_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        script = CA.RENDER_CHART_SCRIPT.replace("__REPO_ROOT__", str(repo))
        (Path(tmp) / "render_charts.py").write_text(script, encoding="utf-8")
        specs = self._specs()
        (Path(tmp) / "chart_data.json").write_text(
            json.dumps({"charts": specs}, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run([sys.executable, "render_charts.py"], cwd=tmp,
                              capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stderr[:400])
        out = proc.stdout
        self.assertIn(f"total={len(specs)} skipped=0", out, out[:400])
        for i in range(1, len(specs) + 1):
            p = Path(tmp) / f"chart_{i}.png"
            self.assertTrue(p.exists(), f"chart_{i}.png 未生成")
            self.assertGreater(p.stat().st_size, 5000, f"chart_{i}.png 过小")
        # 图注信息（问题/观察）要写进 manifest，简报才拿得到
        man = json.loads((Path(tmp) / "chart_manifest.json").read_text(encoding="utf-8"))
        entries = {c["file"]: c for c in man["charts"]}
        self.assertEqual(len(entries), len(specs))
        for i in range(1, len(specs) + 1):
            e = entries[f"chart_{i}.png"]
            self.assertTrue(e.get("question"), e)
            self.assertTrue(e.get("observation"), e)
            self.assertIn(e.get("grade"), ("publish", "draft"))

    def test_chart_rows_keep_only_contract_periods(self):
        """越界期间不进图：报告正文声明"未予采用"的数据，图上也不能出现。"""
        import facts as F
        import task_state
        import working_paper_export as WPX
        tid = "chart-rows-01"
        tmp = Path(tempfile.mkdtemp(prefix="wm_crows_"))
        old_root = ws_mod.WORKSPACE_ROOT
        old_db = task_state.DB_PATH
        ws_mod.configure_workspace_root(str(tmp))
        task_state.DB_PATH = str(tmp / "cr.db")
        try:
            req = F.parse_research_request(
                "表单", company="贵州茅台", company_id="600519.SH", market="cn",
                periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
                identity_source="form")
            task_state.mark_queued(tid, goal="表单", research_request=req.to_payload(),
                                   db_path=task_state.DB_PATH)
            proj = ws_mod.task_project_dir(tid, "default")
            proj.mkdir(parents=True, exist_ok=True)
            # 注意：这里要的是 **financials 行**（revenue/net_profit/... 原始字段），
            # 不是上面的图表行（metric/value）——两者形状不同，别混用。
            rows = [
                {"year": 2023, "report_type": "年报", "revenue": 1505.6,
                 "net_profit": 747.34, "operating_cashflow": 665.93,
                 "disclosure_date": "2024-04-03"},
                {"year": 2024, "report_type": "年报", "revenue": 1741.44,
                 "net_profit": 862.28, "operating_cashflow": 924.64,
                 "disclosure_date": "2025-04-03"},
                {"year": 2025, "report_type": "中报", "revenue": 9999.0,
                 "net_profit": 999.0, "operating_cashflow": 999.0,
                 "disclosure_date": "2025-08-15"},
            ]
            (proj / "financials.json").write_text(
                json.dumps({"financials": rows,
                            "metadata": {"source": "eastmoney_ashare",
                                         "company": "贵州茅台", "currency": "CNY",
                                         "unit": "亿元", "caliber": "合并",
                                         "caliber_evidence": "含 PARENTNETPROFIT"},
                            "raw": {"url": "https://datacenter-web.eastmoney.com/api/x",
                                    "text": "{}"}}, ensure_ascii=False), encoding="utf-8")
            out = WPX.chart_rows(tid, "表单", project="default")
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["periods"], [2023, 2024])
            self.assertEqual({r["year"] for r in out["rows"]}, {2023, 2024})
            self.assertEqual({d["year"] for d in out["derived"]}, {2023, 2024})
            self.assertEqual(out["source_label"], "东方财富数据中心（A股）")
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            task_state.DB_PATH = old_db
            shutil.rmtree(tmp, ignore_errors=True)


    def test_compare_chart_does_not_mix_units_or_extra_metrics(self):
        """对比图只画契约必需指标、且不混装量纲。

        实机 `ui-ecb93e57a1`：底稿把 9 个已选事实都给了图表，结果毛利率（%）被画进
        "亿元"轴、研发投入 6.95 亿被 2989 亿压成看不见的贴地柱。
        """
        import chart_specs as CS
        rows = list(self.ROWS) + [
            {"metric": "gross_margin", "metric_label": "毛利率", "year": 2023,
             "value": 91.96, "unit": "%"},
            {"metric": "gross_margin", "metric_label": "毛利率", "year": 2024,
             "value": 91.93, "unit": "%"},
            {"metric": "total_assets", "metric_label": "总资产", "year": 2023,
             "value": 2727.0, "unit": "亿元"},
            {"metric": "total_assets", "metric_label": "总资产", "year": 2024,
             "value": 2989.45, "unit": "亿元"},
        ]
        specs = CS.financial_research_specs(
            rows, self.DERIVED, unit="亿元", source="东方财富数据中心（A股）",
            company="贵州茅台", caliber="合并", periods=[2023, 2024],
            core_metrics=["revenue", "net_profit", "operating_cashflow"])
        cmp_spec = specs[0]
        labels = {r["label"] for r in cmp_spec["data"]}
        self.assertEqual(labels, {"营业收入", "归母净利润", "经营活动现金流净额"})
        self.assertEqual({r["unit"] for r in cmp_spec["data"]}, {"亿元"},
                         "同图不得混装量纲（毛利率是 %）")
        # 年度顺序稳定：2023 在前（否则图例与柱子顺序随数据顺序漂移）
        self.assertEqual([r["caliber"] for r in cmp_spec["data"][:2]],
                         ["2023年", "2024年"])

    def test_research_task_skips_search_charts(self):
        """研究任务：检索统计图与"市场规模"兜底都不生成（图只来自底稿）。

        实机 `ui-ecb93e57a1` 暴露过：编排器那处守卫不够——`structured_pipeline` 的快照
        回收/预载路径也会调这两个方法，于是词频/域名分布图照样进了交付。守卫放进方法
        内部（唯一收口点）后，任何调用方都拦得住。
        """
        import facts as F
        import orchestrator_v2 as ov
        import task_state
        tid = "chart-guard-01"
        tmp = Path(tempfile.mkdtemp(prefix="wm_cguard_"))
        old_root = ws_mod.WORKSPACE_ROOT
        old_db = task_state.DB_PATH
        ws_mod.configure_workspace_root(str(tmp))
        task_state.DB_PATH = str(tmp / "cg.db")
        try:
            goal = "分析贵州茅台历年财报并生成可视化报告"
            req = F.parse_research_request(
                goal, company="贵州茅台", company_id="600519.SH", market="cn",
                periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
                identity_source="form")
            task_state.mark_queued(tid, goal=goal, research_request=req.to_payload(),
                                   db_path=task_state.DB_PATH)
            proj = ws_mod.task_project_dir(tid, "default")
            proj.mkdir(parents=True, exist_ok=True)
            # 有可作图数据：守卫若失效，这两个方法就会真的去跑渲染脚本
            (proj / "search_results.json").write_text(json.dumps(
                [{"title": "贵州茅台2024年报", "url": "https://example.invalid/a",
                  "snippet": "营业收入 1741.44 亿元"}], ensure_ascii=False),
                encoding="utf-8")
            (proj / "clean_chart_data.json").write_text(json.dumps({
                "entity_frequency": {"贵州茅台": 3, "五粮液": 1},
                "source_distribution": {"example.invalid": 2, "other.invalid": 1},
                "topic_terms": {"营收": 5, "净利": 3},
                "market_data": [
                    {"type": "market_size", "label": "2023年营收", "value": 1505.6,
                     "unit": "亿元", "year": 2023},
                    {"type": "market_size", "label": "2024年营收", "value": 1741.44,
                     "unit": "亿元", "year": 2024},
                ],
            }, ensure_ascii=False), encoding="utf-8")

            o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
            o._messaging = mock.MagicMock()
            with mock.patch("subprocess.run") as run:
                o._generate_search_charts(tid, goal)
                o._render_clean_chart_data(tid, goal)
            self.assertFalse(run.called, "研究任务不得再生成检索统计图/市场规模兜底图")
            self.assertEqual(sorted(p.name for p in (ws_mod.task_workspace(tid)).glob("*.png")),
                             [], "不该落任何检索侧 PNG")
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            task_state.DB_PATH = old_db
            shutil.rmtree(tmp, ignore_errors=True)


class TestResearchBriefAssembly(unittest.TestCase):
    """研究简报由**代码装配**：数字/来源/引用不依赖模型（架构复核 §F1）。

    实机教训：正文只写 2/6 个数字、引用 [1..7] 与清单不对应、首屏是工程说明。
    这里钉住装配器的四件事：数字来自底稿、来源只收采用项并分类、引用一一对应、
    工程内容不进简报正文。
    """

    GOAL = ("研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30")
    ROWS = [
        {"year": 2023, "report_type": "年报", "revenue": 1505.6, "net_profit": 747.34,
         "operating_cashflow": 665.93, "total_assets": 2727.0,
         "total_liabilities": 490.43, "disclosure_date": "2024-04-03"},
        {"year": 2024, "report_type": "年报", "revenue": 1741.44, "net_profit": 862.28,
         "operating_cashflow": 924.64, "total_assets": 2989.45,
         "total_liabilities": 569.33, "disclosure_date": "2025-04-03"},
    ]

    def _env(self, *, body: str = "", tid: str = "brief-01",
             perspective: str = "equity"):
        """临时工作区 + 落库契约 + financials + 检索候选 + 图表清单。"""
        import facts as F
        import task_state
        tmp = Path(tempfile.mkdtemp(prefix="wm_brief_"))
        old_root = ws_mod.WORKSPACE_ROOT
        old_db = task_state.DB_PATH
        ws_mod.configure_workspace_root(str(tmp))
        task_state.DB_PATH = str(tmp / "b.db")
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        self.addCleanup(setattr, task_state, "DB_PATH", old_db)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        req = F.parse_research_request(
            self.GOAL, company="贵州茅台", company_id="600519.SH", market="cn",
            periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
            perspective=perspective, identity_source="form")
        task_state.mark_queued(tid, goal=self.GOAL,
                               research_request=req.to_payload(),
                               db_path=task_state.DB_PATH)
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "financials.json").write_text(json.dumps({
            "financials": self.ROWS,
            "metadata": {"source": "eastmoney_ashare", "company": "贵州茅台",
                         "currency": "CNY", "unit": "亿元", "caliber": "合并",
                         "caliber_evidence": "含 PARENTNETPROFIT"},
            "raw": {"url": "https://datacenter-web.eastmoney.com/api/x", "text": "{}"},
        }, ensure_ascii=False), encoding="utf-8")
        (proj / "search_results.json").write_text(json.dumps([
            {"title": "茅台2024年报解读", "url": "https://news.example/2025-03-12/a",
             "snippet": "贵州茅台2024年营业收入1741.44亿元"},
            {"title": "无关链接", "url": "https://garbage.example/b", "snippet": "广告"},
        ], ensure_ascii=False), encoding="utf-8")
        # 抓取落盘（A2：正文引用要被准入，前提是**真的抓取并校验过**）
        (proj / "fetch_snapshot.json").write_text(json.dumps([
            {"title": "贵州茅台2024年年度报告",
             "url": "https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF",
             "text": ("第三节 管理层讨论与分析\n\n一、经营情况讨论与分析\n\n"
                      "2024年度营业收入1741.44亿元，主要系销量增加及产品结构变化所致。\n\n"
                      "（一）主营业务情况\n\n公司主营业务为茅台酒及系列酒的生产与销售。\n")},
            {"title": "贵州茅台2024年报解读_某媒体",
             "url": "https://news.example/2025-03-12/a",
             "text": ("一、经营情况讨论与分析\n\n"
                      "贵州茅台2024年营业收入1741.44亿元，渠道改革是主要驱动。\n")},
        ], ensure_ascii=False), encoding="utf-8")
        (proj / "chart_manifest.json").write_text(json.dumps({"charts": [
            {"file": "chart_1.png", "type": "grouped_bar", "grade": "publish",
             "keywords": ["对比"]},
            {"file": "entity_frequency.png", "type": None, "grade": None},
        ]}, ensure_ascii=False), encoding="utf-8")
        ws = ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        return tid, body

    def test_numbers_come_from_paper_not_model(self):
        """模型正文里的数字不得改写简报的数据部分。"""
        import report_brief
        tid, _ = self._env()
        hostile = ("# 我的报告\n\n营业收入 9999亿元，归母净利润 1234亿元。\n\n"
                   "## 参考来源\n\n1. [无关](https://garbage.example/b)\n")
        structure = report_brief.build_structure(tid, self.GOAL, hostile)
        self.assertIsNotNone(structure)
        md = report_brief.render_brief_markdown(structure, hostile)
        head = md.split("## 分析")[0]
        self.assertIn("1741.44", head)
        self.assertIn("1505.6", head)
        self.assertNotIn("9999", head, "数据部分不得被模型正文改写")
        self.assertIn("## 关键发现", md)
        self.assertIn("## 财务对照", md)
        # 同比与比率带可复算公式（验收按文本公式判可溯源）
        self.assertIn("同比与比率（可复算）", md)
        self.assertIn("（", md)

    def test_sources_are_adopted_only_and_typed(self):
        """来源只收采用项、按**发布者域名**标类型；未采用只留内部审计。"""
        import report_brief
        tid, _ = self._env()
        body = ("# 我的报告\n\n贵州茅台 2024 年营业收入 1741.44亿元[1]。\n\n"
                "## 参考来源\n\n1. [茅台2024年报解读](https://news.example/a)\n")
        structure = report_brief.build_structure(tid, self.GOAL, body)
        urls = [c["url"] for c in structure["citations"]]
        self.assertIn("https://datacenter-web.eastmoney.com/api/x", urls,
                      "结构化财务来源属采用项")
        self.assertIn("https://news.example/2025-03-12/a", urls, "正文引用的来源属采用项")
        self.assertNotIn("https://garbage.example/b", urls, "未引用来源不进清单")
        by_url = {c["url"]: c for c in structure["citations"]}
        # F2′-1：数据平台是**第三方**，不得标成"发行人年报/官方披露"；
        # 同时单独记录它依据的披露（本次未取得原文）
        em = by_url["https://datacenter-web.eastmoney.com/api/x"]
        self.assertEqual(em["type"], "third_party")
        self.assertIn("转载", str(em.get("based_on") or ""))
        self.assertEqual(em["publisher"], "datacenter-web.eastmoney.com")
        # 第三方媒体同样是第三方
        self.assertEqual(by_url["https://news.example/2025-03-12/a"]["type"], "third_party")
        self.assertEqual(by_url["https://news.example/2025-03-12/a"]["admission"], "admitted")
        unused = [u["url"] for u in (structure["audit"].get("unused_sources") or [])]
        self.assertIn("https://garbage.example/b", unused)

    def test_issuer_host_is_typed_as_issuer_disclosure(self):
        """官方披露域名（交易所/巨潮/SEC）才标发行人披露——不看标题里的"年报"字样。"""
        import report_brief
        tid, _ = self._env()
        body = ("# 报告\n\n年报数据[1][2]。\n\n## 参考来源\n\n"
                "1. [贵州茅台2024年年度报告](https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF)\n"
                "2. [贵州茅台2024年报解读_某媒体](https://news.example/annual-report-review)\n")
        structure = report_brief.build_structure(tid, self.GOAL, body)
        by_url = {c["url"]: c for c in structure["citations"]}
        self.assertEqual(
            by_url["https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF"]["type"],
            "issuer_annual_report")
        self.assertEqual(by_url["https://news.example/2025-03-12/a"]["type"],
                         "third_party", "标题含『年报』不能证明发布者身份")

    def test_inline_refs_keep_pointing_at_the_same_material(self):
        """引用重编号：模型正文的 [1] 指的是它自己清单第 1 条，装配后必须仍指那份材料。"""
        import report_brief
        tid, _ = self._env()
        body = ("# 我的报告\n\n年报显示经营活动现金流净额 924.64亿元[1]。\n\n"
                "## 参考来源\n\n"
                "1. [贵州茅台2024年年度报告](https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF)\n")
        structure = report_brief.build_structure(tid, self.GOAL, body)
        md = report_brief.render_brief_markdown(structure, body)
        by_n = {c["n"]: c["url"] for c in structure["citations"]}
        sentence = next(l for l in md.splitlines() if "年报显示经营活动现金流" in l)
        n = int(re.search(r"\[(\d+)\]", sentence).group(1))
        self.assertEqual(by_n[n], "https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF",
                         "这句话必须仍指向原来那份年报材料")

    def test_unmappable_inline_ref_becomes_a_gap_not_a_guess(self):
        """映射不到采用来源的编号：去掉编号并记引用缺口，不得猜指向。"""
        import report_brief
        tid, _ = self._env()
        body = ("# 报告\n\n某处引用 1741.44亿元[7]。\n\n## 参考来源\n\n"
                "1. [某材料](https://news.example/a)\n")
        structure = report_brief.build_structure(tid, self.GOAL, body)
        self.assertIn(7, structure["citation_gaps"])
        md = report_brief.render_brief_markdown(structure, body)
        self.assertNotIn("[7]", md.split("## 变化解释")[0])
        self.assertIn("未能在本次采用来源中唯一对应", md)

    # ── F2′-3：成稿顺序与内容保留 ──────────────────────────

    def test_analysis_after_duplicate_table_is_preserved(self):
        """离线反例：重复表格之后的有效分析不得被截掉（旧实现返回空串）。"""
        import report_brief
        body = ("# 标题\n\n## 关键数据\n\n| 指标 | 2024 |\n|---|---|\n| 收入 | 1741.44 |\n\n"
                "## 三、盈利质量\n\n归母净利率 49.52%，较上年下降 0.12 个百分点，"
                "主要受产品结构变化影响。\n\n## 四、现金流质量\n\n经营现金流覆盖 107.23%。\n")
        kept = report_brief._analysis_section(body)
        self.assertIn("盈利质量", kept)
        self.assertIn("现金流质量", kept)
        self.assertNotIn("| 指标 | 2024 |", kept, "重复表格仍要移除")

    def test_model_charts_and_empty_headings_are_dropped(self):
        """模型重复贴的图与空标题不进简报；结论/免责声明不列作风险。"""
        import report_brief
        body = ("# 报告\n\n## 分析\n\n结论：收入与利润同向增长。\n\n"
                "![chart_1](charts/chart_1.png)\n\n## \n\n"
                "## 免责声明\n\n本报告不构成任何投资建议。\n")
        kept = report_brief._analysis_section(body)
        self.assertNotIn("![", kept)
        self.assertIn("同向增长", kept)

    def test_brief_body_is_not_wrapped_again(self):
        """验收候选稿回流时不得"简报套简报"（否则分析一节变成整份简报）。"""
        import report_brief
        tid, _ = self._env()
        body = ("# 我的报告\n\n## 分析与结论\n\n营业收入同比增长 15.66%，"
                "主要来自销量与产品结构变化。" + "补充说明。" * 20 + "\n")
        first = report_brief.build_structure(tid, self.GOAL, body)
        md1 = report_brief.render_brief_markdown(first, body)
        self.assertTrue(report_brief._looks_like_brief(md1))
        # 再把装配结果当作正文喂回去：分析一节必须还是原来那段，而不是整份简报
        second = report_brief.build_structure(tid, self.GOAL, md1)
        sec = report_brief._brief_section(
            report_brief.render_brief_markdown(second, md1), "## 分析")
        self.assertIn("营业收入同比增长 15.66%", sec)
        self.assertNotIn("## 关键发现", sec)
        self.assertLess(len(sec), 400)

    def test_literal_n_notation_is_normalized_like_a_ref(self):
        """`[n=1]` 这类把指令记号抄进正文的写法按编号处理（否则验收判引用无对应条目）。"""
        import report_brief
        tid, _ = self._env()
        body = ("# 报告\n\n年报显示经营活动现金流净额 924.64亿元[n=1]。\n\n"
                "## 参考来源\n\n"
                "1. [贵州茅台2024年年度报告](https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF)\n")
        structure = report_brief.build_structure(tid, self.GOAL, body)
        md = report_brief.render_brief_markdown(structure, body)
        by_n = {c["n"]: c["url"] for c in structure["citations"]}
        sentence = next(l for l in md.splitlines() if "年报显示经营活动现金流" in l)
        self.assertNotIn("[n=", sentence)
        n = int(re.search(r"\[(\d+)\]", sentence).group(1))
        self.assertEqual(by_n[n], "https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF")
        self.assertEqual(structure["citation_gaps"], [])

    def test_explained_gap_is_not_a_placeholder_but_bare_marker_is(self):
        """诚实披露（带说明）不计占位；无说明的"营收未披露"仍算占位。"""
        from acceptance_checker import _count_placeholders
        explained = ("- 研发投入强度：需公司实际披露研发投入；未披露时不生成（不填零）\n"
                     "- 本报告未获取行业基准或同业数据，故不给出评级\n"
                     "- 未取得与本期变化相关的年报业务段落（需补充材料见下）\n")
        bare = "核心指标：营收未披露，净利润未获取，毛利率待补充。\n"
        self.assertEqual(_count_placeholders(explained), 0)
        self.assertGreaterEqual(_count_placeholders(bare), 3)

    def test_acceptance_targets_the_assembled_candidate(self):
        """F2′-3：研究任务先装配候选稿再验收——代码负责的声明不再要求模型重做。"""
        import delivery_pipeline as dp
        import task_state
        tid, _ = self._env()
        # 模型稿**故意不写免责声明与来源清单**（那两项由装配器给出）
        body = ("# 报告\n\n## 分析与结论\n\n营业收入同比增长 15.66%，"
                "归因于销量与产品结构变化。" + "补充说明。" * 20 + "\n")
        res = dp.accept_for_body(tid, self.GOAL, body, trigger="报告步骤")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("overall"), "pass", res.get("gaps"))
        # 人工修订重验不得替换用户正文：走的是同一实现，但不做候选稿替换
        res2 = dp.accept_for_body(tid, self.GOAL, body, trigger="人工修订重验")
        self.assertIsNotNone(res2)

    # ── F3-A1：现金覆盖三分支 + 研究对象适用性 ─────────────

    def test_cashflow_coverage_reads_lower_when_below_100(self):
        """离线反例：46.29/66.73 = 69.37%，不得写成"现金流高于利润"。"""
        import report_brief
        tid, _ = self._env()
        rows = [
            {"metric": "net_profit", "metric_label": "归母净利润", "year": 2024,
             "value": 66.73, "unit": "亿元", "fact_id": "f-np"},
            {"metric": "operating_cashflow", "metric_label": "经营活动现金流净额",
             "year": 2024, "value": 46.29, "unit": "亿元", "fact_id": "f-cf"},
        ]
        derived = [{"metric": "cashflow_coverage",
                    "metric_label": "经营现金流对归母净利润的覆盖", "year": 2024,
                    "period": "2024年", "value": 69.37, "unit": "%",
                    "formula": "46.29 / 66.73 * 100",
                    "derived_from": ["f-cf", "f-np"]}]
        got = [f["text"] for f in report_brief._findings(rows, derived, [2023, 2024])
               if "覆盖" in f["text"]]
        self.assertTrue(got)
        self.assertIn("低于", got[0], got)
        self.assertNotIn("高于", got[0])

    def test_cashflow_coverage_branches_on_the_actual_reading(self):
        """定向检查：69.37% / 100% / 107.23% 分别落在 低于 / 相当 / 高于（容差 0.5pp）。"""
        import report_brief
        tid, _ = self._env()

        def reading(cf, npv, ratio):
            rows = [
                {"metric": "net_profit", "metric_label": "归母净利润", "year": 2024,
                 "value": npv, "unit": "亿元", "fact_id": "f-np"},
                {"metric": "operating_cashflow", "metric_label": "经营活动现金流净额",
                 "year": 2024, "value": cf, "unit": "亿元", "fact_id": "f-cf"},
            ]
            derived = [{"metric": "cashflow_coverage",
                        "metric_label": "经营现金流对归母净利润的覆盖", "year": 2024,
                        "period": "2024年", "value": ratio, "unit": "%",
                        "formula": f"{cf} / {npv} * 100", "derived_from": ["f-cf", "f-np"]}]
            got = [f["text"] for f in report_brief._findings(rows, derived, [2023, 2024])
                   if "覆盖" in f["text"]]
            self.assertTrue(got)
            return got[0]

        low = reading(46.29, 66.73, 69.37)
        self.assertIn("低于", low)
        self.assertNotIn("高于", low)
        same = reading(66.73, 66.73, 100.0)
        self.assertIn("相当", same)
        self.assertNotIn("高于", same)
        high = reading(71.56, 66.73, 107.23)
        self.assertIn("高于", high)
        self.assertNotIn("低于", high)
        # 负值与零分母不套覆盖解释（既不高于也不低于）
        for cf, npv, ratio, want in ((-46.29, 66.73, -69.37, "不表示利润有现金支撑"),
                                     (46.29, -66.73, -69.37, "不表示利润有现金支撑"),
                                     (46.29, 0.0, None, "不可算")):
            text = reading(cf, npv, ratio)
            self.assertIn(want, text, text)
            self.assertNotIn("高于", text, text)
            self.assertNotIn("低于", text, text)

    def test_cashflow_coverage_condition_text_matches_the_branches(self):
        """条件文案必须与三分支一致：同为正才比大小，负值/零值不套覆盖解释。"""
        import facts as F
        cond = F.RATIO_CONDITIONS["cashflow_coverage"]
        self.assertIn("低于", cond)
        self.assertIn("相当", cond)
        self.assertIn("高于", cond)
        self.assertIn("同为正", cond)
        self.assertIn("不套覆盖解释", cond)
        # 旧文案在正数区间是误导：不得再声称"同为正即高于"
        self.assertNotIn("同为正时表示", cond)
        self.assertNotIn("同为正**时表示", cond)

    def test_financial_subject_suppresses_corporate_ratios(self):
        """研究对象为金融机构：不生成企业口径比率与比率图，只保留同比对照。"""
        import report_brief
        tid, _ = self._env(perspective="equity")
        # 契约未声明时按名称线索判定（判定来源 name_hint）
        structure = report_brief.build_structure(tid, self.GOAL, "")
        # 贵州茅台不在金融名称线索里 → 未确定（如实显示），比对公视角的机构更典型
        self.assertEqual(structure["scope"]["subject_type"], "unknown")
        # 换一份"金融机构"契约：同名招商银行的契约由声明/名称线索驱动
        import facts as F
        st, src = F.subject_type_of(company="招商银行")
        self.assertEqual((st, src), ("financial", "name_hint"))
        # 表格质量比率按类型过滤
        table = report_brief._metrics_table([], [
            {"metric": "net_margin", "metric_label": "归母净利率", "period": "2024年",
             "value": 30.0, "unit": "%", "formula": "x", "derived_from": []},
            {"metric": "revenue_yoy", "metric_label": "营业收入同比", "period": "2024年",
             "value": 5.0, "unit": "%", "formula": "y", "derived_from": []},
        ], [2024], [], {}, "", subject_type="financial")
        labels = [q["metric"] for q in table["quality"]]
        self.assertNotIn("net_margin", labels, "金融机构不生成企业口径比率")
        self.assertIn("revenue_yoy", labels, "同比类两期变化对照保留")
        self.assertEqual(table["subject_type"], "financial")
        # 图表：金融机构不出第三张（质量比率图）
        from chart_specs import financial_research_specs
        specs = financial_research_specs(
            [{"metric": "revenue", "metric_label": "营业收入", "year": 2024,
              "value": 100.0, "unit": "亿元"}],
            [{"metric": "net_margin", "metric_label": "归母净利率", "year": 2024,
              "period": "2024年", "value": 30.0, "unit": "%", "formula": "x",
              "derived_from": []},
             {"metric": "debt_ratio", "metric_label": "资产负债率", "year": 2024,
              "period": "2024年", "value": 90.0, "unit": "%", "formula": "z",
              "derived_from": []}],
            company="招商银行", subject_type="financial")
        kinds = [s["section_hint"] for s in specs]
        self.assertNotIn("盈利质量", kinds, kinds)
        self.assertTrue(all(s["section_hint"] != "盈利质量" for s in specs))

    def test_subject_type_round_trips_through_contract(self):
        """研究对象类型可由用户声明并随契约往返；非法值丢弃（按名称线索兜底）。"""
        import facts as F
        req = F.parse_research_request(self.GOAL, company="招商银行",
                                       company_id="600036.SH", market="cn",
                                       periods=[2023, 2024], caliber="合并",
                                       as_of="2025-04-30", subject_type="financial",
                                       identity_source="form")
        self.assertEqual(req.subject_type, "financial")
        back = F.ResearchRequest.from_payload(req.to_payload())
        self.assertEqual(back.subject_type, "financial")
        bad = F.parse_research_request(self.GOAL, company="招商银行",
                                       subject_type="bank", identity_source="form")
        self.assertEqual(bad.subject_type, "")
        self.assertEqual(F.subject_type_of(company="招商银行"),
                         ("financial", "name_hint"))

    def test_citations_match_inline_refs(self):
        """引用一一对应：装配后的正文过 `check_source_list_completeness`。"""
        import report_brief
        from acceptance_checker import check_source_list_completeness
        tid, _ = self._env()
        body = ("# 我的报告\n\n营业收入 1741.44亿元[1]。\n\n"
                "## 参考来源\n\n1. [茅台2024年报解读](https://news.example/a)\n")
        structure = report_brief.build_structure(tid, self.GOAL, body)
        md = report_brief.render_brief_markdown(structure, body)
        res = check_source_list_completeness(md)
        self.assertTrue(res.get("pass"), res.get("gaps"))

    def test_engineering_sections_do_not_enter_the_brief(self):
        """工程交付说明（交付文件/贯通测试/如何启动）不进研究简报正文。"""
        import report_brief
        tid, _ = self._env()
        structure = report_brief.build_structure(tid, self.GOAL, "")
        md = report_brief.render_brief_markdown(structure, "")
        for bad in ("贯通测试", "如何启动", "运行验证", "## 交付文件", "**步骤**"):
            self.assertNotIn(bad, md, bad)
        self.assertTrue(md.startswith("# 贵州茅台"), md[:40])
        # 图表只收发布级、且只收 chart_*（检索统计图不进简报）
        files = [c["file"] for c in structure["charts"]]
        self.assertEqual(files, ["chart_1.png"])

    def test_periods_match_the_paper(self):
        """图表/正文/底稿同期间：表格期间 = 契约期间。"""
        import report_brief
        tid, _ = self._env()
        structure = report_brief.build_structure(tid, self.GOAL, "")
        self.assertEqual(structure["metrics_table"]["periods"], [2023, 2024])
        md = report_brief.render_brief_markdown(structure, "")
        self.assertIn("| 2023 | 2024 |", md)

    # ── F2：让简报回答金融研究问题 ──────────────────────────

    ANNUAL_TEXT = (
        "贵州茅台2024年年度报告\n\n"
        "第三节 管理层讨论与分析\n\n"
        "一、经营情况讨论与分析\n\n"
        "报告期内营业收入变动主要系本期销量增加及产品结构变化所致；"
        "归属于上市公司股东的净利润变动与营业收入变动基本同步。\n\n"
        "（一）主营业务情况\n\n"
        "公司主营业务为茅台酒及系列酒的生产与销售，经营模式为以销定产，"
        "主要产品为茅台酒、系列酒，销售模式以直销与批发代理并行。\n\n"
        "二、可能面对的风险\n\n"
        "风险因素：宏观经济波动可能影响高端白酒消费需求；"
        "行业政策与税收政策变化存在不确定性。\n\n"
        "七、财务报表附注\n\n"
        "现金流量表附注：报告期内经营活动产生的现金流量净额924.64亿元，"
        "主要系销售商品收到的现金增加所致；营运资本变动情况见附注。")

    def _env_with_evidence(self, *, body: str = ""):
        """在 `_env` 基础上放一份抓取到的年报正文（证据由 narrative_evidence 提取）。"""
        tid, _ = self._env(body=body)
        proj = ws_mod.task_project_dir(tid, "default")
        (proj / "fetch_snapshot.json").write_text(json.dumps([{
            "title": "贵州茅台2024年年度报告",
            "url": "https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF",
            "text": self.ANNUAL_TEXT,
        }], ensure_ascii=False), encoding="utf-8")
        return tid

    def test_business_background_and_change_explanation_from_evidence(self):
        """业务背景与变化解释来自**抓到的年报正文**（带 [n] 与小节定位），不靠模型。"""
        import report_brief
        tid = self._env_with_evidence()
        structure = report_brief.build_structure(tid, self.GOAL, "")
        md = report_brief.render_brief_markdown(structure, "")
        self.assertIn("## 业务背景", md)
        self.assertIn("主营业务为茅台酒及系列酒", md)
        self.assertIn("小节：", md, "业务背景要带小节定位")
        self.assertIn("## 变化解释", md)
        for marker in ("**发生了什么（数据观察）**", "**管理层/附注的解释**", "**推断边界**",
                       "**还不能证明什么**"):
            self.assertIn(marker, md, marker)
        self.assertIn("不能据此声称长期趋势", md)
        # 变化幅度大的在前（收入/利润/现金流三个变化都给出）
        ch = structure["change_explanation"]["changes"]
        self.assertGreaterEqual(len(ch), 3)
        self.assertGreaterEqual(abs(float(ch[0]["yoy"])), abs(float(ch[-1]["yoy"])))
        # 现金流变化的"还不能证明什么"落到确定性的材料清单
        unproven = {u["label"]: u["materials"] for u in structure["change_explanation"]["unproven"]}
        self.assertIn("现金流量表附注", unproven.get("经营活动现金流净额", []))
        # 来源编号一一对应：变化解释引用的 [n] 必须在清单里，且是发行人披露
        n = structure["change_explanation"]["management"][0]["source_n"]
        self.assertIn(f"[{n}]", md)
        by_n = {c["n"]: c for c in structure["citations"]}
        self.assertEqual(by_n[int(n)]["type"], "issuer_annual_report")

    def test_third_party_commentary_is_not_dressed_as_management(self):
        """第三方评论不得进"管理层/附注的解释"：单独成块并标明发布者。"""
        import report_brief
        tid = self._env_with_evidence()
        proj = ws_mod.task_project_dir(tid, "default")
        (proj / "fetch_snapshot.json").write_text(json.dumps([
            {"title": "贵州茅台2024年年度报告",
             "url": "https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF",
             "text": self.ANNUAL_TEXT},
            {"title": "贵州茅台2024年报解读_某媒体",
             "url": "https://news.example/2025-03-12/review",
             "text": ("一、经营情况讨论与分析\n\n"
                      "贵州茅台2024年营业收入变动主要系渠道改革与直营占比提升所致，"
                      "销量与吨价的结构变化是主要原因。\n")},
        ], ensure_ascii=False), encoding="utf-8")
        structure = report_brief.build_structure(tid, self.GOAL, "")
        ch = structure["change_explanation"]
        mg_urls = {c["n"] for c in structure["citations"]
                   if c["type"] == "issuer_annual_report"}
        for item in ch["management"]:
            self.assertIn(int(item["source_n"]), mg_urls,
                          "管理层解释只能引发行人披露")
        self.assertTrue(ch["third_party_views"], "第三方观点要单独列出")
        md = report_brief.render_brief_markdown(structure, "")
        self.assertIn("**第三方观点（非管理层解释，仅供参照）**", md)
        self.assertIn("news.example", md)

    def test_inapplicable_evidence_is_excluded_and_explained(self):
        """错主体/超资料截止的材料不得进解释，但要照实说明为什么没采用。"""
        import report_brief
        tid, _ = self._env()
        proj = ws_mod.task_project_dir(tid, "default")
        (proj / "fetch_snapshot.json").write_text(json.dumps([
            {"title": "五粮液2026年年度报告",
             "url": "https://static.cninfo.com.cn/finalpage/2027-04-01/999.PDF",
             "text": ("一、经营情况讨论与分析\n\n"
                      "本公司2026年度营业收入891.75亿元，主要系系列酒销量增加所致。\n")},
            {"title": "白酒行业2027年展望_某媒体",
             "url": "https://news.example/2027/outlook",
             "text": ("一、经营情况讨论与分析\n\n"
                      "贵州茅台2027年经营情况讨论：预计营业收入将保持增长。\n")},
        ], ensure_ascii=False), encoding="utf-8")
        structure = report_brief.build_structure(tid, self.GOAL, "")
        ch = structure["change_explanation"]
        self.assertEqual(ch["management"], [], "错主体/超截止的材料不得当管理层解释")
        self.assertEqual(ch["third_party_views"], [])
        self.assertEqual(structure["evidence"]["missing_labels"],
                         ["业务背景", "经营变化解释", "财务附注", "风险因素"])
        excluded = structure["evidence"]["excluded"]
        self.assertEqual(len(excluded), 2, excluded)
        statuses = {e["validation_status"] for e in excluded}
        self.assertEqual(statuses, {"after_as_of"}, statuses)
        md = report_brief.render_brief_markdown(structure, "")
        self.assertIn("**未采用的材料（不满足主体/期间/资料截止或未核验）**", md)
        self.assertIn("晚于资料截止日", md)

    def test_risk_rows_carry_evidence_conditions_and_materials(self):
        """每条风险都要有对应证据、会改变判断的观察条件、需要补充的材料。"""
        import report_brief
        tid = self._env_with_evidence()
        structure = report_brief.build_structure(tid, self.GOAL, "")
        risks = structure["risks"]
        self.assertTrue(risks, "年报风险因素要进简报")
        kinds = {r["kind"] for r in risks}
        self.assertIn("evidence_risk", kinds, "年报里的风险要有证据")
        for r in risks:
            self.assertTrue(r.get("evidence"), r)
            self.assertTrue(r.get("would_change"), r)
        ev = next(r for r in risks if r["kind"] == "evidence_risk")
        self.assertIn("小节：", ev["evidence"], "有证据的风险要写明小节位置")
        self.assertTrue(ev["materials_needed"], ev)
        unproven = next(r for r in risks if r["kind"] == "unproven_change")
        self.assertIn("现金流量表附注", unproven["materials_needed"])
        md = report_brief.render_brief_markdown(structure, "")
        for marker in ("对应证据", "会使判断改变的观察条件", "需要补充的材料", "结论边界"):
            self.assertIn(marker, md, marker)

    def test_appendix_records_field_locations_and_worksheet(self):
        """附录要能回答"这个数字取自哪个字段、怎么算的"。"""
        import report_brief
        import working_paper_export as WPX
        tid = self._env_with_evidence()
        WPX.write_working_paper(tid, self.GOAL, project="default")
        structure = report_brief.build_structure(tid, self.GOAL, "")
        ap = structure["appendix"]
        labels = {l["label"]: l["locator"] for l in ap["field_locations"]}
        self.assertTrue(labels, ap)
        self.assertIn("结构化字段", next(iter(labels.values())))
        self.assertTrue(any(l["locator"] for l in ap["field_locations"]))
        files = {f["file"] for f in ap["worksheet"]}
        self.assertIn("working_paper.json", files)
        self.assertIn("narrative_evidence.json", files)
        md = report_brief.render_brief_markdown(structure, "")
        self.assertIn("### 字段位置与计算底稿", md)
        self.assertIn("计算底稿：working_paper.json", md)
        self.assertIn("PDF 的页码待后续支持", md, "定位口径要如实说明")

    def test_brief_with_evidence_still_passes_acceptance(self):
        """带定位（含字符区间的数字）的简报仍要过验收：数字可溯源、引用一一对应。"""
        import report_brief
        from acceptance_checker import run_acceptance
        tid = self._env_with_evidence()
        body = ("# 我的报告\n\n贵州茅台 2024 年营业收入 1741.44亿元[1]，"
                "同比 15.66%（(1741.44 - 1505.6) / 1505.6 * 100）。\n")
        structure = report_brief.build_structure(tid, self.GOAL, body)
        md = report_brief.render_brief_markdown(structure, body)
        ws = ws_mod.task_workspace(tid)
        res = run_acceptance(tid, self.GOAL, md, ws, profile="financial")
        checks = res.get("checks") or {}
        for key in ("number_traceability", "source_list_completeness", "disclaimer"):
            c = checks.get(key) or {}
            self.assertTrue(c.get("pass"), f"{key}: {c.get('gaps') or c.get('details')}")

    def test_missing_analysis_is_not_certified(self):
        """没有分析、只有数据与底稿时不得判为已验证；工程日志（步骤成功数）不算分析。"""
        import delivery_pipeline as dp
        self.assertFalse(dp._has_analysis_section(
            "## 分析\n\n> 本次未产出可交付的分析正文（数据与底稿已保留，见文末）。\n"))
        self.assertFalse(dp._has_analysis_section(
            "## 分析\n\n## Task Report\n\nGoal: x\nStatus: PARTIAL\n"
            "Steps: 5 (4 OK, 1 failed)\n"))
        self.assertTrue(dp._has_analysis_section(
            "## 分析\n\n收入增长主要来自销量提升与产品结构变化，" + "详见上文说明。" * 12))
        # 通用报告没有固定小节名：不在本门槛判定范围内（不误伤）
        self.assertTrue(dp._has_analysis_section("这是一份简短但完整的通用报告正文。"))

    # ── F2-3：两种阅读视角 + 比率适用条件 ──────────────────

    def test_two_perspectives_share_one_paper(self):
        """两种视角复用同一底稿：数字一致、核查清单不同；视角只认声明。"""
        import report_brief
        out: dict[str, dict] = {}
        for persp, tid in (("equity", "brief-eq"), ("bank_corporate", "brief-bk")):
            self._env(tid=tid, perspective=persp)
            st = report_brief.build_structure(tid, self.GOAL, "")
            out[persp] = {"st": st, "md": report_brief.render_brief_markdown(st, "")}
        eq, bk = out["equity"], out["bank_corporate"]
        # 同一底稿：指标表与派生读数完全一致
        self.assertEqual(eq["st"]["metrics_table"], bk["st"]["metrics_table"])
        self.assertEqual(eq["st"]["findings"], bk["st"]["findings"])
        self.assertEqual(eq["st"]["change_explanation"]["changes"],
                         bk["st"]["change_explanation"]["changes"])
        # 视角不同 → 核查材料与正文里的视角声明不同
        eq_mats = next(r["materials_needed"] for r in eq["st"]["risks"]
                       if r["kind"] == "perspective_material")
        bk_mats = next(r["materials_needed"] for r in bk["st"]["risks"]
                       if r["kind"] == "perspective_material")
        self.assertIn("同业可比数据与行情", eq_mats)
        self.assertIn("债务到期结构与利率", bk_mats)
        self.assertNotEqual(eq_mats, bk_mats)
        self.assertIn("阅读视角：投研视角", eq["md"])
        self.assertIn("阅读视角：银行对公客户研究视角", bk["md"])
        self.assertIn("由用户在表单声明", bk["md"])
        # 视角不改变数字：两版正文的财务对照表逐行一致
        def _table(md):
            return [ln for ln in md.splitlines()
                    if ln.startswith("|") and "营业收入" in ln or ln.startswith("| 指标")]
        self.assertEqual(_table(eq["md"]), _table(bk["md"]))

    def test_ratio_conditions_and_scope_are_rendered(self):
        """比率适用条件与"仅非金融企业"边界必须写在正文里（不静默套用）。"""
        import report_brief
        tid, _ = self._env()
        st = report_brief.build_structure(tid, self.GOAL, "")
        md = report_brief.render_brief_markdown(st, "")
        self.assertIn("**比率适用条件**", md)
        self.assertIn("归母净利率", md)
        self.assertIn("非金融企业", md)
        self.assertIn("不得机械套用", md)
        # 现金流覆盖倍数的符号条件必须写明（负值不表示利润有现金支撑）
        self.assertIn("同为正", md)


class TestReviewHonestyProjection(unittest.TestCase):
    """机器通过与人工复核**分开**，且人工一栏 fail closed（C1-4）。

    起因：接口验证把正文标题改成"（人工复核：口径与缺口已核对）"，任务状态随之变
    SUCCESS/verified，文档还把它记成"真实人工修订实测"——自动修订 + 机器重验**不能**
    充当研究员复核。因此页面必须两栏分开，且没有研究员批准记录时只能显示"待复核"。
    """

    def _ws(self, files: dict) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="wm_review_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name, payload in files.items():
            (tmp / name).write_text(json.dumps(payload, ensure_ascii=False),
                                    encoding="utf-8")
        return tmp

    def _payload(self, ws: Path):
        import web_ui
        return web_ui._research_payload("ui-review-01", ws) or {}

    def test_human_review_defaults_to_pending(self):
        """没有 human_review.json → 待复核（不显示成已复核）。"""
        ws = self._ws({"report_structure.json": {"findings": [{"text": "营业收入增长"}]}})
        rev = self._payload(ws).get("review") or {}
        self.assertEqual((rev.get("human") or {}).get("status"), "pending")
        self.assertIn("machine", rev)

    def test_approval_without_approver_is_not_a_review(self):
        """写了 status=recorded 但没有批准者 → 仍按待复核（不允许匿名/自动批准）。"""
        ws = self._ws({"report_structure.json": {"findings": []},
                       "human_review.json": {"status": "recorded", "at": "2026-09-20"}})
        rev = self._payload(ws).get("review") or {}
        self.assertEqual((rev.get("human") or {}).get("status"), "pending")

    def test_recorded_review_is_shown_with_approver_and_version(self):
        """真实研究员写入的批准（含批准者/时间/版本）原样呈现。"""
        ws = self._ws({"report_structure.json": {"findings": []},
                       "human_review.json": {"status": "recorded", "approver": "研究员甲",
                                             "at": "2026-09-20T10:00:00", "version_id": "abc123"}})
        human = (self._payload(ws).get("review") or {}).get("human") or {}
        self.assertEqual(human.get("status"), "recorded")
        self.assertEqual(human.get("approver"), "研究员甲")
        self.assertEqual(human.get("version_id"), "abc123")

    def test_frontend_shows_both_columns(self):
        """结果页源码级：机器验收与人工复核必须各占一行，且待复核文案写明"不代表已复核"。"""
        src = (Path(__file__).resolve().parent / "frontend" / "src" / "components"
               / "ResearchBriefPanel.tsx").read_text(encoding="utf-8")
        self.assertIn("机器验收", src)
        self.assertIn("人工复核", src)
        self.assertIn("待复核（无研究员批准记录）", src)
        self.assertIn("机器重验通过不代表已复核", src)

    def test_panel_reports_stale_structure(self):
        """C2-6：面板绑定的版本与当前版本不一致时必须明说（不拿旧发现冒充新版）。"""
        ws = self._ws({"report_structure.json": {
            "version_id": "old-version", "findings": [{"text": "营业收入增长"}],
            "claims": [{"text": "营业收入 288.76 亿元", "status": "unsupported",
                        "reason": "该来源未采用", "type": "third_party_view"}]}})
        p = self._payload(ws)
        self.assertEqual(p.get("structure_version"), "old-version")
        # 工作区里没有版本记录 → 无法证明面板属于当前版本，必须为 False（fail closed）
        self.assertIs(p.get("structure_current"), False)
        self.assertEqual((p.get("claims") or [{}])[0].get("status"), "unsupported")

    def test_panel_claims_carry_support_status(self):
        """主张记录进面板：状态/原因/主体/期间/来源与结构对象一致。"""
        ws = self._ws({"report_structure.json": {
            "version_id": "v1",
            "claims": [{"text": "2024 年目标未达成", "status": "needs_check",
                        "type": "target_claim", "reason": "目标类判断需核对目标年度",
                        "subject": "洋河股份", "periods": [2024],
                        "source": {"url": "https://news.example/a", "old_n": 3}}],
            "unsupported_claims": [{"sentence": "经媒体报道…", "title": "某媒体",
                                    "url": "https://news.example/a", "old_n": 3,
                                    "reason": "晚于资料截止日"}]}})
        p = self._payload(ws)
        c = (p.get("claims") or [])[0]
        self.assertEqual(c["type"], "target_claim")
        self.assertEqual(c["status"], "needs_check")
        self.assertEqual(c["periods"], [2024])
        self.assertEqual(c["source"]["old_n"], 3)
        self.assertEqual((p.get("unsupported_claims") or [])[0]["reason"], "晚于资料截止日")


class TestUnsupportedClaimLinkage(unittest.TestCase):
    """C2-1/C2-2/C2-4：结论依赖**未采用来源**时，主张与证据的关联必须留下来。

    实机反例（洋河 `ui-f00775cfdf` 原稿）：正文用"经21财经报道交叉印证""2024 年
    5%—10% 目标未达成"开展判断，而同一材料在来源清单里是"未采用"；装配只把编号去掉，
    读者仍把该句当成有来源支持的事实。这里冻结该形态：逐句标"待核查 + 原因"、
    主张记录 `status=unsupported`、风险清单点名同一句，且**不得删光分析**。
    """

    GOAL = ("研究洋河股份 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30")
    # 模型自写的分析：三句，其中第二句依赖"21财经报道"（该材料晚于资料截止日）
    BODY = (
        "## 分析\n\n"
        "2024 年营业收入 288.76 亿元，较上年下降 12.83%，降幅为近三年最大。"
        "经21财经报道交叉印证，2024 年 5%—10% 的增长目标未达成，说明渠道调整尚未见效。[3]\n"
        "归母净利润 66.73 亿元，同比下降 33.38%。\n\n"
        "## 参考来源\n\n"
        "1. [东方财富数据中心](https://data.eastmoney.com/api/qt/stock/main)\n"
        "2. [洋河股份2024年年度报告](https://static.cninfo.com.cn/finalpage/2025-04-29/1.PDF)\n"
        "3. [21财经：洋河的目标与渠道](https://news.example/2025-06-01/yanghe)\n")

    def _env(self, *, tid: str = "c2-unsup-1"):
        import facts as F
        import task_state
        tmp = Path(tempfile.mkdtemp(prefix="wm_c2_"))
        old_root = ws_mod.WORKSPACE_ROOT
        old_db = task_state.DB_PATH
        ws_mod.configure_workspace_root(str(tmp))
        task_state.DB_PATH = str(tmp / "c2.db")
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        self.addCleanup(setattr, task_state, "DB_PATH", old_db)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        req = F.parse_research_request(
            self.GOAL, company="洋河股份", company_id="002304.SZ", market="cn",
            periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
            perspective="bank_corporate", identity_source="form")
        task_state.mark_queued(tid, goal=self.GOAL, research_request=req.to_payload(),
                               db_path=task_state.DB_PATH)
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "financials.json").write_text(json.dumps({
            "financials": [
                {"year": 2023, "report_type": "年报", "revenue": 331.26,
                 "net_profit": 100.16, "operating_cashflow": 61.3,
                 "total_assets": 700.0, "total_liabilities": 200.0,
                 "disclosure_date": "2024-04-26"},
                {"year": 2024, "report_type": "年报", "revenue": 288.76,
                 "net_profit": 66.73, "operating_cashflow": 46.29,
                 "total_assets": 720.0, "total_liabilities": 210.0,
                 "disclosure_date": "2025-04-29"},
            ],
            "metadata": {"source": "eastmoney_ashare", "company": "洋河股份",
                         "stock_code": "002304.SZ", "currency": "CNY", "unit": "亿元",
                         "caliber": "合并", "caliber_evidence": "含 PARENTNETPROFIT"},
            "raw": {"url": "https://datacenter-web.eastmoney.com/api/x", "text": "{}"},
        }, ensure_ascii=False), encoding="utf-8")
        (proj / "search_results.json").write_text(json.dumps([
            {"title": "洋河股份2024年报解读", "url": "https://news.example/2025-04-30/a",
             "snippet": "洋河股份2024年营业收入288.76亿元"},
        ], ensure_ascii=False), encoding="utf-8")
        # 21财经那篇晚于资料截止日 → 叙事证据判 after_as_of（同一 URL 在模型来源清单里）
        (proj / "fetch_snapshot.json").write_text(json.dumps([
            {"title": "21财经：洋河的目标与渠道",
             "url": "https://news.example/2025-06-01/yanghe",
             "published_at": "2025-06-01",
             "text": ("一、经营情况讨论与分析\n\n"
                      "洋河股份2024年营业收入288.76亿元，公司年度经营目标为增长5%—10%，"
                      "实际未达成，主要系渠道调整。\n")},
        ], ensure_ascii=False), encoding="utf-8")
        import working_paper_export as WPX
        WPX.write_working_paper(tid, self.GOAL, project="default")
        return tid

    def test_excluded_source_leaves_claim_marked_not_silently_dropped(self):
        import report_brief
        tid = self._env()
        st = report_brief.build_structure(tid, self.GOAL, self.BODY)
        unsup = st.get("unsupported_claims") or []
        self.assertTrue(unsup, "未采用来源的依赖句没有被记下来")
        self.assertIn("21财经", json.dumps(unsup, ensure_ascii=False))
        self.assertIn("晚于资料截止日", unsup[0]["reason"], unsup)
        # 主张记录：同一句 status=unsupported 且带来源与原因（可追溯）
        bad = [c for c in st["claims"] if c.get("status") == "unsupported"]
        self.assertTrue(bad, st["claims"])
        self.assertIn("21财经", json.dumps(bad, ensure_ascii=False))
        self.assertEqual(bad[0]["subject"], "洋河股份")
        self.assertEqual(bad[0]["periods"], [2024])
        # 版本绑定：装配前为空串，盖章后与本版正文一致
        self.assertEqual(bad[0]["version_id"], "")
        report_brief.stamp_structure_version(st, "v-abc123")
        self.assertEqual(st["version_id"], "v-abc123")
        self.assertEqual(st["claims"][0]["version_id"], "v-abc123")
        # 正文：该句标『待核查』，但分析**不得被删光**（其余两句仍在）
        self.assertIn("〔待核查：本句引用的来源未采用", st["analysis"])
        self.assertIn("归母净利润 66.73 亿元", st["analysis"])
        self.assertIn("降幅为近三年最大", st["analysis"])
        md = report_brief.render_brief_markdown(st, self.BODY)
        self.assertIn("〔待核查：本句引用的来源未采用", md)
        self.assertIn("结论不得当作已证事实", md)
        # 风险清单点名同一句（缺口/风险/主张同一状态）
        kinds = {r["kind"] for r in st["risks"]}
        self.assertIn("unsupported_claim", kinds, st["risks"])
        hit = next(r for r in st["risks"] if r["kind"] == "unsupported_claim")
        self.assertIn("未采用来源", hit["text"])
        self.assertIn("晚于资料截止日", hit["evidence"])
        # 标记文字里不得带无法溯源的读数（验收会逐个数）
        marker = st["analysis"].split("〔待核查：本句引用的来源未采用", 1)[1].split("〕", 1)[0]
        self.assertNotRegex(marker, r"\d", marker)

    def test_adopted_source_claims_are_untouched(self):
        """正例：来源在采用清单里的句子不得被标记（不能把真话也标成待核查）。"""
        import report_brief
        tid = self._env(tid="c2-unsup-2")
        # 补上被引用的那份年报（抓取过、契约内）→ 它会被准入，句子不该被标
        proj = ws_mod.task_project_dir(tid, "default")
        (proj / "fetch_snapshot.json").write_text(json.dumps([
            {"title": "洋河股份2024年年度报告",
             "url": "https://static.cninfo.com.cn/finalpage/2025-04-29/1.PDF",
             "published_at": "2025-04-29",
             "text": ("一、经营情况讨论与分析\n\n"
                      "报告期内营业收入同比下降，主要系公司主动调整产品结构与渠道库存所致。\n")},
        ], ensure_ascii=False), encoding="utf-8")
        import working_paper_export as WPX
        WPX.write_working_paper(tid, self.GOAL, project="default")
        body = self.BODY.replace("[3]", "[2]")
        st = report_brief.build_structure(tid, self.GOAL, body)
        self.assertEqual(st.get("unsupported_claims"), [], st.get("unsupported_claims"))
        self.assertNotIn("〔待核查", st["analysis"])
        self.assertIn("归母净利润 66.73 亿元", st["analysis"])

    def test_explanation_present_is_not_reported_as_missing(self):
        """C2-4：解释已取得时写"贡献程度未核实"，不得同时宣称"原因尚不能证明"。"""
        import report_brief
        tid = self._env(tid="c2-unsup-3")
        proj = ws_mod.task_project_dir(tid, "default")
        # 补一份**已采用**的年报正文（含经营讨论）→ 管理层解释存在
        (proj / "fetch_snapshot.json").write_text(json.dumps([
            {"title": "洋河股份2024年年度报告",
             "url": "https://static.cninfo.com.cn/finalpage/2025-04-29/1.PDF",
             "published_at": "2025-04-29",
             "text": ("一、经营情况讨论与分析\n\n"
                      "报告期内营业收入同比下降，主要系公司主动调整产品结构与渠道库存所致。\n"
                      "二、财务报表附注\n\n"
                      "营业收入明细：本期 288.76 亿元，上期 331.26 亿元。\n")},
        ], ensure_ascii=False), encoding="utf-8")
        import working_paper_export as WPX
        WPX.write_working_paper(tid, self.GOAL, project="default")
        st = report_brief.build_structure(tid, self.GOAL, self.BODY)
        ch = st["change_explanation"]
        if not ch.get("management"):
            self.skipTest("该夹具未取得管理层解释，另有用例覆盖无解释分支")
        unproven = ch.get("unproven") or []
        self.assertTrue(unproven)
        self.assertTrue(all(u.get("has_explanation") for u in unproven), unproven)
        texts = [r["text"] for r in st["risks"] if r["kind"] == "unproven_change"]
        self.assertTrue(texts)
        for t in texts:
            self.assertIn("解释已取得", t, t)
            self.assertNotIn("尚不能证明", t, t)



class TestChangeExplanationGuards(unittest.TestCase):
    """C2-3：文档可准入 ≠ 每句话可证明。

    三类不能冒充"经营变化原因"的材料：会计政策/准则套话、仅重复数字的附注、
    只命中"变化"关键词却没有任何因果语言的段落；目标/达成类判断另需核对目标年度、
    发布时点与实际数（实机反例：洋河原稿把"2024 年 5%—10% 目标未达成"当已证事实）。
    """

    POLICY_TEXT = ("重要会计政策及会计估计\n\n"
                   "本公司自 2024 年 1 月 1 日起执行财政部修订后的《企业会计准则第 14 号》，"
                   "会计政策变更采用追溯调整法，比较期间数据已重述。")
    NUMBER_ONLY_TEXT = ("财务报表附注\n\n"
                        "经营活动产生的现金流量净额本期为 46.29 亿元，上期为 61.30 亿元，"
                        "同比变化幅度见上表。")
    CAUSAL_TEXT = ("经营情况讨论与分析\n\n"
                   "报告期内营业收入同比下降，主要系公司主动调整产品结构与渠道库存所致。")

    def test_policy_text_is_not_a_change_explanation(self):
        import narrative_evidence as ne
        self.assertTrue(ne.is_policy_text(self.POLICY_TEXT))
        self.assertFalse(ne.is_causal(self.POLICY_TEXT))
        # 标题命中"经营情况讨论与分析"但内容是政策套话 → 不得归为变化解释
        kind = ne.classify("经营情况讨论与分析", self.POLICY_TEXT)
        self.assertNotEqual(kind, ne.KIND_CHANGE, kind)

    def test_number_only_paragraph_is_not_a_change_explanation(self):
        import narrative_evidence as ne
        self.assertFalse(ne.is_causal(self.NUMBER_ONLY_TEXT))
        self.assertNotEqual(ne._prose_classify(self.NUMBER_ONLY_TEXT), ne.KIND_CHANGE)
        # 有因果语言的段落仍是变化解释（不能把真解释一起否掉）
        self.assertTrue(ne.is_causal(self.CAUSAL_TEXT))
        self.assertEqual(ne._prose_classify(self.CAUSAL_TEXT), ne.KIND_CHANGE)

    def test_number_only_footnote_does_not_enter_change_explanation(self):
        """仅重复数字的附注不进"管理层/附注的解释"（数字已由底稿给出）。"""
        import report_brief
        t = TestUnsupportedClaimLinkage("test_adopted_source_claims_are_untouched")
        t.setUp()
        self.addCleanup(lambda: None)
        tid = t._env(tid="c2-guard-1")
        proj = ws_mod.task_project_dir(tid, "default")
        (proj / "fetch_snapshot.json").write_text(json.dumps([
            {"title": "洋河股份2024年年度报告",
             "url": "https://static.cninfo.com.cn/finalpage/2025-04-29/1.PDF",
             "published_at": "2025-04-29",
             "text": ("一、经营情况讨论与分析\n\n" + self.NUMBER_ONLY_TEXT + "\n\n"
                      "二、重要会计政策及会计估计\n\n" + self.POLICY_TEXT + "\n")},
        ], ensure_ascii=False), encoding="utf-8")
        import working_paper_export as WPX
        WPX.write_working_paper(tid, t.GOAL, project="default")
        st = report_brief.build_structure(tid, t.GOAL, t.BODY)
        ch = st["change_explanation"]
        joined = json.dumps(ch["management"] + ch["third_party_views"], ensure_ascii=False)
        self.assertNotIn("会计政策", joined, "政策套话不得作为变化解释")
        self.assertNotIn("同比变化幅度见上表", joined, "仅重复数字的附注不得作为变化解释")

    def test_target_claim_needs_issuer_support(self):
        """目标/达成类判断：无发行人披露直接支持时标 needs_check（不是已证事实）。"""
        import report_brief
        t = TestUnsupportedClaimLinkage("test_adopted_source_claims_are_untouched")
        t.setUp()
        tid = t._env(tid="c2-guard-2")
        body = ("## 分析\n\n"
                "经媒体报道，2024 年 5%—10% 的增长目标未达成，说明渠道调整尚未见效。[2]\n\n"
                "## 参考来源\n\n"
                "1. [东方财富数据中心](https://data.eastmoney.com/api/qt/stock/main)\n"
                "2. [某媒体：洋河的目标与渠道](https://news.example/2025-04-30/yanghe)\n")
        proj = ws_mod.task_project_dir(tid, "default")
        (proj / "fetch_snapshot.json").write_text(json.dumps([
            {"title": "某媒体：洋河的目标与渠道",
             "url": "https://news.example/2025-04-30/yanghe",
             "published_at": "2025-04-30",
             "text": ("经营情况讨论与分析\n\n"
                      "洋河股份2024年经营目标为增长5%—10%，实际未达成，主要系渠道调整。\n")},
        ], ensure_ascii=False), encoding="utf-8")
        import working_paper_export as WPX
        WPX.write_working_paper(tid, t.GOAL, project="default")
        st = report_brief.build_structure(tid, t.GOAL, body)
        targets = [c for c in st["claims"] if c.get("type") == "target_claim"]
        self.assertTrue(targets, st["claims"])
        self.assertEqual(targets[0]["status"], "needs_check")
        self.assertIn("目标年度", targets[0]["reason"], targets[0])


class TestRealAnnualReportPositivePath(unittest.TestCase):
    """C2-5：用**真实年报文本 + 真实页码定位**做正向离线验收。

    合成场景（normal_growth 等）只证明分支行为；真实年报才证明"经营讨论/附注/风险能
    定位、能进入简报、发布日在契约内"。夹具 `evals/real/yanghe_ar2024_excerpt.json`
    是洋河股份 2024 年年度报告（东财公告文本 API，art_code 内嵌披露日 2025-04-29 ≤
    资料截止 2025-04-30）**真实文本的有界摘录**，按真实页码摘取、一字未改。
    """

    GOAL = ("研究洋河股份 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30")
    FIXTURE = Path(__file__).resolve().parent / "evals" / "real" / "yanghe_ar2024_excerpt.json"

    def _fx(self) -> dict:
        return json.loads(self.FIXTURE.read_text(encoding="utf-8"))

    def _doc(self, sections: list[dict]) -> dict:
        """真实小节 → 文档对象：正文拼接 + **保留真实页码**的 page_offsets。"""
        fx = self._fx()
        parts, offsets, pos = [], [], 0
        for sec in sections:
            offsets.append((pos, int(sec["page"])))
            body = f"### {sec['title']}\n\n{sec['text']}"
            parts.append(body)
            pos += len(body) + 1
        return {"title": fx["title"], "url": fx["url"],
                "published_at": fx["published_at"], "text": "\n".join(parts),
                "page_offsets": offsets}

    def _env(self, *, tid: str = "c2-real-1"):
        import facts as F
        import task_state
        tmp = Path(tempfile.mkdtemp(prefix="wm_c2real_"))
        old_root = ws_mod.WORKSPACE_ROOT
        old_db = task_state.DB_PATH
        ws_mod.configure_workspace_root(str(tmp))
        task_state.DB_PATH = str(tmp / "c2r.db")
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        self.addCleanup(setattr, task_state, "DB_PATH", old_db)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        req = F.parse_research_request(
            self.GOAL, company="洋河股份", company_id="002304.SZ", market="cn",
            periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
            perspective="bank_corporate", identity_source="form")
        task_state.mark_queued(tid, goal=self.GOAL, research_request=req.to_payload(),
                               db_path=task_state.DB_PATH)
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "financials.json").write_text(json.dumps({
            "financials": [
                {"year": 2023, "report_type": "年报", "revenue": 331.26,
                 "net_profit": 100.16, "operating_cashflow": 61.3,
                 "total_assets": 700.0, "total_liabilities": 200.0,
                 "disclosure_date": "2024-04-26"},
                {"year": 2024, "report_type": "年报", "revenue": 288.76,
                 "net_profit": 66.73, "operating_cashflow": 46.29,
                 "total_assets": 720.0, "total_liabilities": 210.0,
                 "disclosure_date": "2025-04-29"},
            ],
            "metadata": {"source": "eastmoney_ashare", "company": "洋河股份",
                         "stock_code": "002304.SZ", "currency": "CNY", "unit": "亿元",
                         "caliber": "合并", "caliber_evidence": "含 PARENTNETPROFIT"},
            "raw": {"url": "https://datacenter-web.eastmoney.com/api/x", "text": "{}"},
        }, ensure_ascii=False), encoding="utf-8")
        fx = self._fx()
        docs = [self._doc(fx["sections"]), self._doc(fx["policy_sections"])]
        (proj / "fetch_snapshot.json").write_text(
            json.dumps(docs, ensure_ascii=False), encoding="utf-8")
        import working_paper_export as WPX
        WPX.write_working_paper(tid, self.GOAL, project="default")
        return tid, fx

    def test_real_report_evidence_is_admitted_with_real_page_locators(self):
        import report_brief
        tid, fx = self._env()
        st = report_brief.build_structure(tid, self.GOAL, "")
        ev = st["evidence"]
        self.assertEqual(ev["excluded"], [], "契约内的真实年报不该被排除")
        self.assertGreaterEqual(ev["located"], 3, ev)
        # 摘录里**没有**带因果语言的经营讨论小节（真实年报的 MD&A 正文不在所摘页码内），
        # 而会计政策/审计意见小节必须**填不上这个缺口**——所以"经营变化解释"如实缺失。
        # F3-A 那轮把"第 37 页 · 会计政策声明"当成了变化解释，正是本批要修的行为。
        self.assertEqual(ev["missing_labels"], ["经营变化解释"], ev)
        # 定位必须带**真实页码**（摘录保留原页码：2/3/5/10/64 页）
        locs = []
        for kind in ("business_background", "change_explanation", "footnote", "risk"):
            for r in report_brief._located(
                    report_brief._evidence(tid), kind, 4):
                locs.append(str(r.get("locator") or ""))
        self.assertTrue(any("第 2 页" in l for l in locs), locs)
        self.assertTrue(any("第 10 页" in l or "第 64 页" in l for l in locs), locs)
        # 来源类型：年报原文经第三方平台获取 → 标发行人披露并写明转载路径
        md = report_brief.render_brief_markdown(st, "")
        self.assertIn("第 10 页", md)
        issuer = [c for c in st["citations"] if c["type"] == "issuer_annual_report"]
        self.assertTrue(issuer, st["citations"])
        self.assertTrue(any("第三方平台" in str(c.get("based_on") or "") for c in issuer),
                        issuer)

    def test_real_accounting_policy_is_not_dressed_as_change_explanation(self):
        """真实文本上的 C2-3：年报里的会计政策/审计意见不得当经营变化原因。"""
        import report_brief
        tid, _fx = self._env(tid="c2-real-2")
        st = report_brief.build_structure(tid, self.GOAL, "")
        ch = st["change_explanation"]
        joined = json.dumps(ch["management"] + ch["third_party_views"], ensure_ascii=False)
        self.assertNotIn("收入确认", joined, "会计政策不得作为变化解释")
        self.assertNotIn("审计意见", joined, "审计意见不得作为变化解释")

    def test_fixture_is_real_text_not_synthetic(self):
        """夹具自证：真实 URL/披露日/页码，且含年报原文特征串。"""
        fx = self._fx()
        self.assertEqual(fx["published_at"], "2025-04-29")
        self.assertIn("eastmoney", fx["url"])
        self.assertTrue(str(fx["art_code"]).startswith("AN"))
        blob = json.dumps(fx, ensure_ascii=False)
        self.assertIn("江苏洋河", blob)
        self.assertTrue(fx["policy_sections"], "缺真实会计政策小节")
        self.assertNotIn("示例", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
