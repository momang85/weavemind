# -*- coding: utf-8 -*-
"""检索质量统一：唯一实现 + 策略可配置（同时带上项）。

背景（架构师原文）：`worker_base` 用自家相关性评分与查询扩展，`adapters/search_quality`
只被 `adapters/text_search` 用——两条路径判定不一致，阈值/关键词硬编码。
本文件断言统一之后的三件事：

1. **两条路径同源**：轻量检索（text_search）与 SearchAgent 走同一份实现，
   同一份结果集得到同样的保留/剔除结论；
2. **策略可配置**：阈值、词表、引擎清单、变体上限都能从
   `system.search_quality` 段（+ 环境变量）覆盖，且默认值与迁移前一致；
3. **长连读要求不误杀英文结果**（迁移中实测到的假阴性）：
   中文指令 + 英文结果的混合查询必须能保留合法英文条目。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters import search_quality as sq
from worker_base import SearchAgent


def _sa() -> SearchAgent:
    sa = SearchAgent.__new__(SearchAgent)
    sa._strategy_blocks = []
    sa._strategy_boosts = []
    return sa


class TestSingleImplementation(unittest.TestCase):
    """两条路径共用同一实现：同样的输入必须得到同样的结论。"""

    RESULTS = [
        {"title": "2026年新能源汽车市场报告", "url": "https://www.chinairn.com/a",
         "snippet": "2026 新能源汽车 市场规模 万辆"},
        {"title": "固态硬盘 SSD 百科", "url": "https://baike.baidu.com/x",
         "snippet": "固态 存储"},
        {"title": "博彩娱乐城", "url": "https://28quan.com/y", "snippet": "下注"},
        {"title": "2026年无关天气", "url": "https://weather-info.org/z",
         "snippet": "天气"},
    ]

    def test_same_query_same_verdict(self):
        q = "2026年新能源汽车市场"
        via_worker = _sa()._filter_results(q, self.RESULTS)
        via_shared = sq.score_results(q, self.RESULTS)
        self.assertEqual([r["url"] for r in via_worker], [r["url"] for r in via_shared],
                         "两条路径必须给出同一批结果（同一实现、同一策略）")
        self.assertEqual(len(via_worker), 1)
        self.assertIn("chinairn.com", via_worker[0]["url"])

    def test_keywords_and_clean_text_shared(self):
        raw = ("任务目标：调研2026年国内新能源汽车市场现状\n"
               "用户目标：调研2026年国内新能源汽车市场现状，输出结构化报告\n"
               "原始指令：搜索行业市场规模")
        self.assertEqual(_sa()._extract_keywords(raw), sq.extract_keywords(raw))
        self.assertEqual(SearchAgent._clean_search_text(raw), sq.clean_search_text(raw))

    def test_garbage_verdict_shared(self):
        self.assertTrue(_sa()._is_garbage_result("博彩", "https://28quan.com/a", "下注"))
        self.assertTrue(sq.is_garbage_result("博彩", "https://28quan.com/a", "下注"))
        self.assertFalse(_sa()._is_garbage_result("研报", "https://www.chinairn.com/a", ""))

    def test_strategy_blocks_and_boosts_flow_through(self):
        """已部署策略的域名黑/白名单作为参数进入同一实现。"""
        sa = _sa()
        sa._strategy_blocks = ["weather-info.org"]
        out = sa._filter_results("2026年新能源汽车市场", self.RESULTS)
        self.assertFalse(any("weather-info" in r["url"] for r in out), "黑名单域名被剔除")
        sa._strategy_boosts = ["chinairn.com"]
        boosted = sa._filter_results("2026年新能源汽车市场", self.RESULTS)
        self.assertTrue(boosted[0].get("score", 0) > 0, "白名单加分可见")


class TestEnglishResultsSurviveMixedQuery(unittest.TestCase):
    """长连读要求只作用于中文结果：混合查询里的合法英文条目不得被误杀。"""

    QUERY = ("搜索GitHub上完整的Python/Pygame愤怒的小鸟开源项目，优先选择项目文件完整、"
             "README标明依赖和运行方式、有许可证且star较高的仓库")

    RESULTS = [
        {"title": "angry-birds · GitHub Topics",
         "url": "https://github.com/topics/angry-birds",
         "snippet": "Python pygame angry birds games repositories"},
        {"title": "How to build Angry Birds in Pygame",
         "url": "https://realpython.com/angry-birds-pygame/",
         "snippet": "tutorial pygame python"},
    ]

    def test_english_results_kept(self):
        kept = _sa()._filter_results(self.QUERY, self.RESULTS)
        self.assertEqual(len(kept), 2, [r.get("url") for r in kept])

    def test_cjk_mismatch_still_dropped(self):
        """中文结果仍受长连读约束（"固态电池"查询不得命中"固态硬盘"）。"""
        out = sq.score_results("固态电池 市场规模", [
            {"title": "固态硬盘 SSD 百科", "url": "https://baike.baidu.com/x",
             "snippet": "固态 存储 硬盘"},
        ])
        self.assertEqual(out, [])

    def test_explicit_relaxation_bypasses_long_run_requirement(self):
        """调用方显式放宽 min_score（"严格过滤为空 → 放宽再试"）时不再套硬规则。"""
        weak = [{"title": "市场", "url": "https://ev-report.org/c", "snippet": "市场行情"}]
        self.assertEqual(sq.score_results("新能源汽车市场", weak), [])
        self.assertEqual(len(sq.score_results("新能源汽车市场", weak, min_score=1)), 1)


class TestPolicyIsConfigurable(unittest.TestCase):
    """阈值/词表/引擎清单/变体上限来自配置，默认值与迁移前一致。"""

    def test_defaults_match_previous_behaviour(self):
        p = sq.load_policy({})
        self.assertEqual(p.min_score, 2)
        self.assertEqual(p.require_hit_min_run, 3)
        self.assertEqual(p.max_variants, 10)
        self.assertEqual(p.authority_official, 4)
        self.assertEqual(p.authority_industry, 3)
        self.assertEqual(p.authority_junk, -2)
        self.assertEqual(tuple(p.engines), sq.DDG_ENGINES)
        self.assertIn("wenku.baidu.com", p.low_authority_domains)

    def test_config_section_overrides(self):
        p = sq.load_policy({"system": {"search_quality": {
            "min_score": 5,
            "require_hit_min_run": 0,
            "max_variants": 4,
            "engines": ["brave", "mojeek"],
            "authority_junk": -9,
            "junk_titles": ["自定义垃圾标题"],
        }}})
        self.assertEqual(p.min_score, 5)
        self.assertEqual(p.require_hit_min_run, 0)
        self.assertEqual(p.max_variants, 4)
        self.assertEqual(tuple(p.engines), ("brave", "mojeek"))
        self.assertEqual(p.authority_junk, -9)
        self.assertEqual(tuple(p.junk_titles), ("自定义垃圾标题",))

    def test_env_overrides_scalar_items(self):
        with mock.patch.dict("os.environ", {"WM_SEARCH_MIN_SCORE": "7",
                                            "WM_SEARCH_MAX_VARIANTS": "2"}):
            p = sq.load_policy({})
        self.assertEqual(p.min_score, 7)
        self.assertEqual(p.max_variants, 2)

    def test_min_score_from_policy_is_applied(self):
        results = [{"title": "2026年新能源汽车市场报告",
                    "url": "https://ev-report.org/a", "snippet": "2026 市场"}]
        strict = sq.load_policy({"system": {"search_quality": {"min_score": 99}}})
        self.assertEqual(sq.score_results("2026年新能源汽车市场", results,
                                          policy=strict), [])
        self.assertEqual(len(sq.score_results("2026年新能源汽车市场", results)), 1)

    def test_require_hit_can_be_disabled(self):
        off = sq.load_policy({"system": {"search_quality": {"require_hit_min_run": 0}}})
        out = sq.score_results("固态电池 市场规模", [
            {"title": "固态硬盘", "url": "https://x.org/ssd", "snippet": "固态 电池 硬盘"}],
            policy=off, min_score=1)
        self.assertEqual(len(out), 1, "关掉长连读要求后弱相关结果按 min_score 判定")

    def test_engine_list_reaches_worker(self):
        import worker_base
        with mock.patch.object(sq, "current_policy",
                               return_value=sq.load_policy({"system": {
                                   "search_quality": {"engines": ["brave"]}}})):
            self.assertEqual(worker_base._ddg_engines(), ("brave",))

    def test_current_policy_reads_config_file_and_reloads(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_sq_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        cfg = tmp / "config.json"
        cfg.write_text(json.dumps({"system": {"search_quality": {"min_score": 8}}}),
                       encoding="utf-8")
        with mock.patch.object(sq, "_config_path", lambda: cfg), \
                mock.patch.object(sq, "_policy_cache",
                                  {"mtime": None, "policy": None, "path": ""}):
            self.assertEqual(sq.current_policy().min_score, 8)
            cfg.write_text(json.dumps({"system": {"search_quality": {"min_score": 3}}}),
                           encoding="utf-8")
            import os
            import time as _t
            _later = _t.time() + 5
            os.utime(cfg, (_later, _later))     # mtime 前移：模拟用户随后改了配置
            self.assertEqual(sq.current_policy().min_score, 3, "配置变更后自动重载")

    def test_variants_cap_is_configurable(self):
        p = sq.load_policy({"system": {"search_quality": {"max_variants": 2}}})
        out = sq.build_query_variants("调研2026年新能源汽车市场规模与主要玩家",
                                      policy=p, rich=True)
        self.assertEqual(len(out), 2)

    def test_rich_variants_include_research_domains_from_policy(self):
        p = sq.load_policy({"system": {"search_quality": {
            "research_domains": ["example-analyst.com"]}}})
        out = sq.build_query_variants("2026年新能源汽车市场规模调研", policy=p, rich=True)
        self.assertTrue(any("site:example-analyst.com" in v for v in out), out)

    def test_light_search_variant_count_unchanged(self):
        """轻量检索（rich=False）仍是 3 条：统一实现不得悄悄增加查询量。"""
        out = sq.build_query_variants("调研2026年新能源汽车市场规模与主要玩家")
        self.assertEqual(len(out), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
