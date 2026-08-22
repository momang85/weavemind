# -*- coding: utf-8 -*-
"""真实交付链回归测试：搜索相关性过滤、file_io 落盘逻辑、code_execution 命名。"""
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock


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
            ) as fr, mock.patch("orchestrator_v2.time.sleep") as sl:
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
            ) as fr, mock.patch("orchestrator_v2.time.sleep") as sl, \
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
