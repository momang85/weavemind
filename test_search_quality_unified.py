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


class TestDdgBackendRegistry(unittest.TestCase):
    """ddgs 备后端必须**问注册表**再打，不能照策略清单直打（S0 实测 + S1 修复）。

    背景：ddgs 9.16 把可用后端做成自动发现的 `ddgs.engines.ENGINES`（按类属性
    `disabled` 过滤，包内 `bing`/`yandex` 已停用），类上既无 `get_available_backends`
    也无 `BACKENDS`。照旧清单打一个已停用的名字时，`DDGS._get_engines` 会**静默回落
    auto**（全引擎重扫）——包内实测 `backend="yandex"` 白等 32 秒后 ConnectError。
    因此"清单"只管偏好顺序，"能不能打"一律以注册表为准；注册表读不到（版本未知）
    与注册表可读但没有清单内后端，是两种必须分开的状态。
    """

    def test_registry_hit_keeps_policy_order(self):
        with mock.patch.object(sq, "ddg_text_backends",
                               return_value=("brave", "duckduckgo")):
            name, basis = sq.select_ddg_backend(("yandex", "duckduckgo", "brave"))
        self.assertEqual((name, basis), ("duckduckgo", "registry"),
                         "命中注册表的后端里取清单顺序首个")

    def test_unreadable_registry_marks_policy_pick_unverified(self):
        with mock.patch.object(sq, "ddg_text_backends", return_value=()):
            name, basis = sq.select_ddg_backend(("yandex", "brave"))
        self.assertEqual((name, basis), ("yandex", "policy"))

    def test_no_policy_backend_enabled_means_no_request(self):
        with mock.patch.object(sq, "ddg_text_backends", return_value=("mojeek2",)):
            name, basis = sq.select_ddg_backend(("yandex", "brave"))
        self.assertEqual((name, basis), ("", "none"),
                         "注册表可读但清单全不在：空名交给调用方跳过（不发请求）")

    def test_worker_uses_registry_and_skips_ddgs_when_absent(self):
        import worker_base
        sa = _sa()
        with mock.patch.object(worker_base, "_engine_healthy", return_value=True), \
                mock.patch.object(sq, "ddg_text_backends", return_value=("brave", "mojeek")):
            specs = sa._provider_specs()
        self.assertEqual([(s["provider"], s["backend"]) for s in specs],
                         [("bing", "www.bing.com"), ("ddgs", "brave")])
        with mock.patch.object(worker_base, "_engine_healthy", return_value=True), \
                mock.patch.object(sq, "ddg_text_backends", return_value=("mojeek2",)):
            specs = sa._provider_specs()
        self.assertEqual([s["provider"] for s in specs], ["bing"],
                         "没有清单内可用后端时只留主通道，不拿无效名字打 ddgs")

    def test_light_search_issues_no_request_without_backend(self):
        from adapters import text_search
        with mock.patch.object(text_search, "_first_available_engine", return_value=""):
            self.assertEqual(text_search._search_ddg("洋河股份 2024 年度报告", 5, 3.0), [],
                             "无可用后端时不发请求（空后端名会触发 ddgs 的 auto 全扫）")

    def test_selection_is_consistent_with_installed_registry(self):
        """环境无关的硬不变量：选中的后端必须在注册表里，否则宁可不打。"""
        available = sq.ddg_text_backends()
        name, basis = sq.select_ddg_backend()
        if not available:
            self.assertEqual(basis, "policy", "注册表读不到：标注未核实后按清单回落")
        elif name:
            self.assertEqual(basis, "registry")
            self.assertIn(name, available)
        else:
            self.assertEqual(basis, "none", "注册表可读但没有清单内后端：不发请求")


class TestSearchDiagnostics(unittest.TestCase):
    """S0 同环境诊断：错误分类、有界预算、脱敏与"零结果≠故障"的区分（全离线）。"""

    def setUp(self):
        import search_diag
        self.d = search_diag

    def test_error_taxonomy_distinguishes_cause(self):
        """类别必须能分开"正常没找到"与"根本没完成查询"（专项 §4）。"""
        import io as _io
        import socket
        import ssl
        import urllib.error

        def _http_err(code, msg):
            # fp 用 BytesIO：避免 HTTPError 携带临时文件（ResourceWarning）
            return urllib.error.HTTPError("u", code, msg, {}, _io.BytesIO(b""))

        cases = [
            (_http_err(401, "unauthorized"), "auth_error"),
            (_http_err(403, "forbidden"), "policy_blocked"),
            (_http_err(429, "slow down"), "rate_limited"),
            (_http_err(404, "gone"), "not_found"),
            (ModuleNotFoundError("No module named 'ddgs'"), "missing_dependency"),
            (socket.gaierror("getaddrinfo failed"), "dns_error"),
            (ssl.SSLError("certificate verify failed"), "tls_error"),
            (TimeoutError("timed out"), "timeout"),
            (RuntimeError("proxy connect failed"), "proxy_error"),
        ]
        for exc, want in cases:
            self.assertEqual(self.d.classify_error(exc), want, f"{exc!r} 应归 {want}")
        # 零结果不是异常，但类别表里必须存在，且不能被归成 timeout/parse_error
        self.assertIn("no_results", self.d.ERROR_CLASSES)
        self.assertIn("no_relevant_results", self.d.ERROR_CLASSES)

    def test_budget_is_shared_between_calls_and_deadline(self):
        b = self.d.Budget(calls=2, seconds=30)
        self.assertFalse(b.expired())
        b.take()
        b.take()
        self.assertTrue(b.expired(), "次数用尽即不可再发请求")
        with self.assertRaises(RuntimeError):
            b.take()
        # 短 deadline 能中止：等待到期后 take 直接抛
        b2 = self.d.Budget(calls=5, seconds=0.05)
        import time as _t
        _t.sleep(0.08)
        self.assertTrue(b2.expired())
        with self.assertRaises(RuntimeError):
            b2.take()

    def test_challenge_page_is_not_counted_as_ok(self):
        self.assertTrue(self.d.looks_like_challenge("<html>请输入验证码</html>"))
        self.assertTrue(self.d.looks_like_challenge("Please complete the security check"))
        self.assertFalse(self.d.looks_like_challenge("<html><li class='b_algo'>…</li></html>"))

    def test_offline_mode_sends_no_request(self):
        with mock.patch.object(self.d, "probe_search_html") as p1, \
                mock.patch.object(self.d, "probe_search_sdk") as p2, \
                mock.patch.object(self.d, "probe_structured") as p3, \
                mock.patch.object(self.d, "probe_document") as p4:
            facts = self.d.run_diagnostics(network=False)
        for p in (p1, p2, p3, p4):
            p.assert_not_called()
        self.assertEqual(facts["probes"], [])
        self.assertIn("runtime_identity", facts)

    def test_facts_never_contain_credentials(self):
        """事实表不得带出密钥/代理凭据：只允许布尔与版本号。"""
        secret = "sk-" + "z" * 24
        with mock.patch("launcher._load_config",
                        return_value={"llm": {"api_key": secret, "base_url": "https://x/v1"},
                                      "search_api_key": secret}), \
                mock.patch.dict("os.environ", {"HTTP_PROXY": "http://user:pw@proxy:8080"}):
            facts = self.d.environment_facts()
        blob = json.dumps(facts, ensure_ascii=False)
        self.assertNotIn(secret, blob)
        self.assertNotIn("user:pw", blob)
        self.assertNotIn("proxy:8080", blob)
        self.assertTrue(facts["config"]["llm_key_set"], "只报是否设置，不报值")
        self.assertTrue(facts["proxy_env_present"]["HTTP_PROXY"])

    def test_search_probe_classifies_failures_and_zero_results(self):
        """探测把异常映射成类别、把"HTTP200 但无结果块"记成 no_results，不混为一谈。"""
        from adapters import text_search
        with mock.patch.object(text_search, "_fetch_bing_html",
                               side_effect=TimeoutError("timed out")):
            rec = self.d.probe_search_html(self.d.Budget())
        self.assertEqual(rec["status"], "timeout")
        with mock.patch.object(text_search, "_fetch_bing_html", return_value="<html>空页</html>"):
            rec = self.d.probe_search_html(self.d.Budget())
        self.assertEqual(rec["status"], "no_results")
        with mock.patch.object(text_search, "_fetch_bing_html",
                               return_value='<li class="b_algo"><h2>t</h2></li>' * 3):
            rec = self.d.probe_search_html(self.d.Budget())
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["items"], 3)
        with mock.patch.object(text_search, "_fetch_bing_html",
                               return_value="<html>请输入验证码</html>"):
            rec = self.d.probe_search_html(self.d.Budget())
        self.assertEqual(rec["status"], "challenge", "验证码页不得算 ok")

    def test_document_probe_reports_what_came_back(self):
        """取回 HTML 查看页要记 kind=html，不是 parse_error——候选不是 PDF ≠ 通道故障。"""
        with mock.patch("adapters.transport.get_via_urllib",
                        return_value="<html><body>notice viewer</body></html>"), \
                mock.patch.object(self.d, "_frozen_document_url",
                                  return_value=("https://data.eastmoney.com/notices/detail/x.html",
                                                "洋河股份:2024年年度报告")):
            rec = self.d.probe_document(self.d.Budget())
        self.assertEqual(rec["status"], "ok")
        self.assertEqual(rec["kind"], "html")
        self.assertFalse(rec["is_pdf"])
        self.assertIn("不是 PDF", rec["reason"])
        with mock.patch("adapters.transport.get_via_urllib",
                        return_value="%PDF-1.7\nbody"), \
                mock.patch.object(self.d, "_frozen_document_url",
                                  return_value=("https://data.eastmoney.com/x.pdf", "t")):
            rec = self.d.probe_document(self.d.Budget())
        self.assertTrue(rec["is_pdf"])
        self.assertEqual(rec["content_type"], "application/pdf")


class TestBoundedSearchRunner(unittest.TestCase):
    """S1 有界检索执行器：共享预算、不 auto 重扫、零结果≠故障、去重、结果协议（全离线）。"""

    def setUp(self):
        from adapters import search_runner as sr
        self.sr = sr
        self.specs = [{"provider": "bing", "backend": "www.bing.com"},
                      {"provider": "ddgs", "backend": "yandex"}]

    def _item(self, i):
        return {"title": f"t{i}", "url": f"https://e.example/{i}", "snippet": "s"}

    def test_call_budget_caps_attempts(self):
        """调用次数上限是硬闸：3 变体 × 2 提供方也只能打 2 次。"""
        calls = []

        def call(provider, backend, q, wait):
            calls.append((provider, backend, q))
            return [self._item(len(calls))]

        out = self.sr.run_search(["q1", "q2", "q3"], call_provider=call,
                                 providers=self.specs,
                                 budget=self.sr.SearchBudget(max_calls=2,
                                                             deadline_seconds=30),
                                 max_results=10)
        self.assertEqual(out.attempts, 2)
        self.assertEqual(len(calls), 2)
        self.assertIn(out.status, ("ok", "partial"))

    def test_short_deadline_can_stop_the_whole_run(self):
        """短 deadline 能中止整个执行：到点后不再发新请求。"""
        import time as _t
        calls = []

        def call(provider, backend, q, wait):
            calls.append(q)
            _t.sleep(0.05)
            return []

        out = self.sr.run_search(["q1", "q2", "q3", "q4"], call_provider=call,
                                 providers=self.specs,
                                 budget=self.sr.SearchBudget(max_calls=99,
                                                             deadline_seconds=0.12),
                                 max_results=10)
        self.assertLess(len(calls), 8, "到点后不得继续尝试")
        self.assertTrue(out.attempts <= 8)

    def test_never_falls_back_to_auto_backend(self):
        """全失败也不得回落 auto（backend=None）——那会串行扫全部引擎。"""
        seen = []

        def call(provider, backend, q, wait):
            seen.append(backend)
            raise TimeoutError("timed out")

        out = self.sr.run_search(["q1", "q2"], call_provider=call, providers=self.specs,
                                 budget=self.sr.SearchBudget(max_calls=6, deadline_seconds=30),
                                 max_results=10)
        self.assertTrue(all(b for b in seen), f"出现空后端（auto）：{seen}")
        self.assertEqual(out.status, "timeout")
        self.assertTrue(out.retryable, "暂时性失败才可重试")

    def test_zero_results_is_not_a_backend_failure(self):
        """真零结果记 no_results（不是故障类），也不可重试。"""
        def call(provider, backend, q, wait):
            return []

        out = self.sr.run_search(["q1"], call_provider=call, providers=self.specs,
                                 budget=self.sr.SearchBudget(max_calls=6,
                                                             deadline_seconds=30),
                                 max_results=10)
        self.assertEqual(out.status, "no_results")
        self.assertFalse(out.retryable)
        self.assertIn("零命中", out.reason)

    def test_parse_error_is_not_retryable(self):
        def call(provider, backend, q, wait):
            raise ValueError("bad payload")

        out = self.sr.run_search(["q1"], call_provider=call, providers=self.specs,
                                 budget=self.sr.SearchBudget(max_calls=6, deadline_seconds=30),
                                 max_results=10)
        self.assertEqual(out.status, "parse_error")
        self.assertFalse(out.retryable, "非暂时性失败不该触发重试")

    def test_dedupes_query_provider_pairs(self):
        calls = []

        def call(provider, backend, q, wait):
            calls.append((provider, backend, q))
            return []

        self.sr.run_search(["q1", "q1", " q1 ", "q2"], call_provider=call,
                           providers=self.specs,
                           budget=self.sr.SearchBudget(max_calls=20, deadline_seconds=30),
                           max_results=10)
        self.assertEqual(len(calls), len(set(calls)), f"存在重复提交：{calls}")

    def test_partial_and_legacy_compat_layer(self):
        def call(provider, backend, q, wait):
            return [self._item(1)]        # 只有 1 条，少于 enough=3

        out = self.sr.run_search(["q1"], call_provider=call, providers=self.specs,
                                 budget=self.sr.SearchBudget(max_calls=6, deadline_seconds=30),
                                 max_results=10, enough=3)
        self.assertEqual(out.status, "partial")
        self.assertEqual(out.to_legacy_items(), out.items, "旧数组契约要有兼容层")
        payload = json.dumps(out.to_legacy_items(), ensure_ascii=False)
        self.assertTrue(payload.startswith("["))

    def test_exclude_keys_stops_replaying_previous_combos(self):
        """任务内重试不得再次提交同一 (提供方, 后端, 查询)（专项 §5）。"""
        calls = []

        def call(provider, backend, q, wait):
            calls.append((provider, backend, q))
            return []

        budget = self.sr.SearchBudget(max_calls=20, deadline_seconds=30)
        first = self.sr.run_search(["q1"], call_provider=call, providers=self.specs,
                                   budget=budget, max_results=10)
        second = self.sr.run_search(["q1", "q2"], call_provider=call, providers=self.specs,
                                    budget=budget, max_results=10,
                                    exclude_keys=first.tried_keys)
        self.assertEqual(second.attempts, 2, "只有新查询被提交（q2 × 两个提供方）")
        self.assertEqual(len(calls), len(set(calls)), f"存在重复提交：{calls}")
        self.assertEqual(sum(1 for c in calls if c[2] == "q1"), len(self.specs),
                         "q1 只在第一轮打过（每个提供方一次），重试不得再打")
        self.assertEqual(sum(1 for c in calls if c[2] == "q2"), len(self.specs))

    def test_completed_but_empty_call_is_not_a_failure(self):
        """ddgs 的 'No results found.' = 查询完成但零命中：不进错误表、不触发熔断。"""
        def call(provider, backend, q, wait):
            raise RuntimeError("No results found.")

        out = self.sr.run_search(["洋河股份 2024年年度报告"], call_provider=call,
                                 providers=[{"provider": "ddgs", "backend": "brave"}],
                                 budget=self.sr.SearchBudget(max_calls=3,
                                                             deadline_seconds=30),
                                 max_results=10)
        self.assertEqual(out.status, "no_results")
        self.assertEqual(out.errors, {}, "零命中不是后端故障，不该进错误表")
        self.assertFalse(out.retryable)

    def test_empty_result_error_detector_matches_only_zero_hits(self):
        self.assertTrue(self.sr.is_empty_result_error(RuntimeError("No results found.")))
        self.assertFalse(self.sr.is_empty_result_error(RuntimeError("Connection refused")))


class TestStructuredRetryQueries(unittest.TestCase):
    """S1 补查重试的真正输入：下一批查询由契约结构化生成，且不重复已打过的组合。"""

    def setUp(self):
        from adapters import search_runner as sr
        self.sr = sr

    def _contract(self):
        from execution_contract import ExecutionContract
        return ExecutionContract(company="洋河股份", company_id="002304.SZ",
                                 market="A股", periods=(2024,), as_of="2025-04-30")

    def test_retry_queries_keep_subject_and_period_change_facet(self):
        c = self._contract()
        base = c.queries()
        plan = c.retry_queries(tried=base)
        self.assertTrue(plan, "契约应能给出下一批查询")
        for q in plan:
            self.assertIn("洋河股份（002304.SZ）", q, "主体不得变化")
            self.assertIn("2024年年度报告", q, "期间/文档类型不得变化")
            self.assertNotIn(q, base, "不得重复上一批查询")
        self.assertNotIn("2023", " ".join(plan), "不得引入契约外期间")

    def test_retry_plan_advances_and_reports_exhaustion(self):
        from execution_contract import ExecutionContract
        c = ExecutionContract(company="洋河股份", company_id="002304.SZ", market="A股",
                              periods=(2023, 2024), as_of="2025-04-30")
        seen = list(c.queries())
        plan1 = c.retry_queries(tried=seen)
        seen += plan1
        plan2 = c.retry_queries(tried=seen)
        self.assertTrue(plan2, "第二个期间也应有重试计划")
        self.assertFalse(set(plan2) & set(plan1), "第二批复试必须是新查询")
        self.assertEqual(c.retry_queries(tried=seen + plan2), [],
                         "计划用尽时如实返回空，不假装还有换词方案")

    def test_worker_retry_variants_come_from_contract_plan(self):
        sa = _sa()
        c = self._contract()
        sa._contract = c
        tried = {("bing", "www.bing.com", c.queries()[0])}
        out = self.sr.SearchOutcome(status="no_results", tried_keys=tried)
        variants = sa._retry_variants(c, out)
        self.assertTrue(variants)
        self.assertNotIn(c.queries()[0], variants, "已打过的查询不得再进重试批")
        self.assertTrue(all("2024年年度报告" in v for v in variants))

    def test_worker_honours_retry_marker_over_contract_queries(self):
        sa = _sa()
        sa._contract = self._contract()
        instr = ("研究洋河股份（002304.SZ） 2024年年度报告\n"
                 "[重试检索查询] 洋河股份（002304.SZ） 2024年年度报告 经营情况讨论与分析")
        got = sa._query_variants(instr)
        self.assertTrue(got)
        self.assertIn("经营情况讨论与分析", got[0],
                      "带 [重试检索查询] 行时只打这批新查询，不回落到常规契约查询")

    def test_worker_without_contract_keeps_query_discipline(self):
        sa = _sa()
        sa._contract = None
        out = self.sr.SearchOutcome(status="no_results", tried_keys={("bing", "b", "q")})
        self.assertEqual(sa._retry_variants(None, out), [],
                         "无契约 = 不生成新查询（不做无据的换词）")


class TestTaskScopedSearchBudget(unittest.TestCase):
    """任务级检索台账：一次任务一份预算，重试/重做派发只拿余额（专项 §5）。"""

    class _FakeRedis:
        def __init__(self):
            self.h = {}

        def hgetall(self, k):
            return dict(self.h.get(k, {}))

        def hincrby(self, k, f, n):
            d = self.h.setdefault(k, {})
            d[f] = int(d.get(f, 0)) + int(n)
            return d[f]

        def hsetnx(self, k, f, v):
            d = self.h.setdefault(k, {})
            if f in d:
                return 0
            d[f] = v
            return 1

        def expire(self, k, ttl):
            return 1

    def _sa(self, task="t-ledger"):
        from types import SimpleNamespace

        from worker_base import SearchAgent
        sa = SearchAgent.__new__(SearchAgent)
        sa._strategy_blocks, sa._strategy_boosts = [], []
        sa._messaging = SimpleNamespace(_redis=self._FakeRedis())
        sa._current_ctx = SimpleNamespace(root_task_id=task, dispatch_id=f"{task}-d1")
        return sa

    def setUp(self):
        self._env = mock.patch.dict("os.environ", {
            "WM_SEARCH_MAX_CALLS": "", "WM_SEARCH_DEADLINE_SECONDS": ""})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_first_dispatch_gets_the_full_allowance(self):
        self.assertEqual(self._sa()._search_allowance(), (6, 60.0))

    def test_retry_dispatch_gets_only_the_remainder(self):
        sa = self._sa()
        sa._record_search_ledger(4)
        left_calls, left_secs = sa._search_allowance()
        self.assertEqual(left_calls, 2, "重试派发只拿余额")
        self.assertLessEqual(left_secs, 60.0)

    def test_exhausted_task_budget_issues_no_request(self):
        sa = self._sa()
        sa._record_search_ledger(6)
        self.assertEqual(sa._search_allowance()[0], 0)
        sa._load_active_strategy = lambda: None
        sa._query_variants = lambda instr: ["洋河股份 2024年年度报告"]
        sa._provider_specs = lambda: [{"provider": "bing", "backend": "www.bing.com"}]
        calls = []

        def _bing(q):
            calls.append(q)
            return []

        sa._search_bing = _bing
        self.assertEqual(json.loads(sa._execute_bounded("检索")), [])
        self.assertEqual(calls, [], "任务级预算用完不得再发检索请求")

    def test_budget_capped_by_remainder(self):
        sa = self._sa()
        sa._record_search_ledger(5)
        calls, secs = sa._search_allowance()
        self.assertEqual(sa._search_budget(allowance=calls, wall_left=secs).max_calls, 1)

    def test_ledger_failure_degrades_to_default_allowance(self):
        """台账读不到 = "不知道"，按默认额度走（不伪造结论，也不阻塞检索）。"""
        from types import SimpleNamespace
        sa = self._sa()
        sa._messaging = SimpleNamespace(_redis=None)
        self.assertEqual(sa._search_allowance(), (6, 60.0))

    def test_ledger_is_per_task(self):
        a, b = self._sa("t-a"), self._sa("t-b")
        a._record_search_ledger(6)
        self.assertEqual(a._search_allowance()[0], 0)
        self.assertEqual(b._search_allowance()[0], 6, "台账不得跨任务串")


if __name__ == "__main__":
    unittest.main(verbosity=2)
