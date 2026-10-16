# -*- coding: utf-8 -*-
"""任务4：验收阈值按报告体裁配置化（acceptance.profiles）。

覆盖 docs/arch_task4_acceptance_config.md 的验收项与约束：
- 档位从 config.json 的 `acceptance` 段加载，键缺失回落 default_profile，配置不可用回落内置兜底表；
- 阈值只换来源，判定规则不变：financial/research 两轴按体裁取，crypto/macro/news 仍用域阈值；
- event_study（freshness_required=false）改判 freshness_alternatives：
  命中即通过；未命中只提示、**不判失败**；
- placeholder_max 由档位给出（年报 3 / 半年报·事件研究 5）；
- run_acceptance 结果带 report_type，验收事件同步记录。

数值以设计文档为准（年报 0.7/0.5、半年报 0.55/0.4、事件研究 0.5/0.3）。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import acceptance_checker as ac


def _mk_ws(tag: str) -> str:
    """建一个最小工作区（project/ 存在，无检索结果）。"""
    tmp = Path(tempfile.mkdtemp(prefix=f"t4acc_{tag}_"))
    proj = tmp / "project"
    proj.mkdir(parents=True)
    (proj / "search_results.json").write_text("[]", encoding="utf-8")
    return str(tmp)


# 半年报场景：目标里给了 4 个数字（=用户材料，可溯源），报告另有 2 个数字不在来源里；
# 溯源率 4/6≈0.667 —— 落在 [0.55, 0.70) 区间：年报阈值 0.70 判失败、半年报 0.55 判通过。
_SEMI_GOAL = ("分析贵州茅台2026年半年度经营情况：营业总收入120亿元、归母净利润18亿元、"
              "毛利率35%、经营活动现金流净额25亿元")
_SEMI_REPORT = (
    "# 贵州茅台2026年半年度经营回顾\n\n"
    "2026年上半年贵州茅台实现营业总收入120亿元，归母净利润18亿元，毛利率35%，"
    "经营活动现金流净额25亿元。\n\n"
    "同期研发投入9亿元，经销商数量2300家。\n\n"
    "## 数据说明\n数据截止2026-06-30。\n\n"
    "## 免责声明\n本报告基于公开信息整理，不构成投资建议。\n"
)

# 事件研究场景：只有"公告日/事件日"这类时效锚点，天然没有"数据截止"块。
_EVENT_GOAL = "研究贵州茅台收购公告日前后的股价异常收益（事件研究）"
_EVENT_REPORT = (
    "# 事件研究：贵州茅台收购公告\n\n"
    "本次事件窗口以公告日2026-03-15为事件日 t0，前后各 5 个交易日为窗口。\n\n"
    "累计异常收益 3.2%，换手率上升 1.5 个百分点。\n\n"
    "## 免责声明\n本报告基于公开信息整理，不构成投资建议。\n"
)


class TestAcceptanceProfileConfig(unittest.TestCase):
    """档位加载：config.json → 模板 → 内置兜底，未知体裁回落 default_profile。"""

    def setUp(self):
        self._saved = (ac._CONFIG_PATH, ac._CONFIG_TEMPLATE_PATH)
        self.addCleanup(self._restore)

    def _restore(self):
        ac._CONFIG_PATH, ac._CONFIG_TEMPLATE_PATH = self._saved

    def _point_at(self, cfg: dict) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="t4cfg_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = tmp / "config.json"
        p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        missing = tmp / "absent.json"
        ac._CONFIG_PATH, ac._CONFIG_TEMPLATE_PATH = p, missing
        return p

    def test_profiles_match_doc_values(self):
        """仓库配置（config.json 或随包模板）必须给出文档规定的三档与数值。"""
        profiles, default = ac.load_acceptance_profiles()
        self.assertEqual(default, "annual_report")
        self.assertEqual(
            {k: profiles[k]["traceability"] for k in
             ("annual_report", "semi_annual", "event_study")},
            {"annual_report": {"financial": 0.7, "research": 0.5},
             "semi_annual": {"financial": 0.55, "research": 0.4},
             "event_study": {"financial": 0.5, "research": 0.3}},
        )
        self.assertEqual([profiles[k]["placeholder_max"] for k in
                          ("annual_report", "semi_annual", "event_study")], [3, 5, 5])
        self.assertTrue(profiles["annual_report"]["freshness_required"])
        self.assertTrue(profiles["semi_annual"]["freshness_required"])
        self.assertFalse(profiles["event_study"]["freshness_required"])
        self.assertEqual(profiles["event_study"]["freshness_alternatives"],
                         ["公告日", "事件日", "披露日"])

    def test_builtin_fallback_when_config_unavailable(self):
        """两份配置都不可用 → 内置兜底表（=文档值），验收不能因为配置坏了而崩。"""
        tmp = Path(tempfile.mkdtemp(prefix="t4non_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ac._CONFIG_PATH = tmp / "nope.json"
        ac._CONFIG_TEMPLATE_PATH = tmp / "nope2.json"
        profiles, default = ac.load_acceptance_profiles()
        self.assertEqual(default, "annual_report")
        self.assertEqual(profiles["semi_annual"]["traceability"]["financial"], 0.55)
        self.assertEqual(profiles["event_study"]["freshness_alternatives"],
                         ["公告日", "事件日", "披露日"])

    def test_partial_profile_override_falls_back_to_builtin(self):
        """部署只覆盖一个轴时，其余键必须回落内置（文档）值，而不是丢档位。"""
        self._point_at({"acceptance": {
            "default_profile": "semi_annual",
            "profiles": {"annual_report": {"traceability": {"financial": 0.8}}},
        }})
        profiles, default = ac.load_acceptance_profiles()
        self.assertEqual(default, "semi_annual")   # 未在 profiles 里 → 用内置档位
        self.assertEqual(profiles["annual_report"]["traceability"]["financial"], 0.8)
        self.assertEqual(profiles["annual_report"]["traceability"]["research"], 0.5)
        self.assertEqual(profiles["annual_report"]["placeholder_max"], 3)

    def test_broken_config_json_does_not_raise(self):
        tmp = Path(tempfile.mkdtemp(prefix="t4bad_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bad = tmp / "config.json"
        bad.write_text("{ 这不是 JSON", encoding="utf-8")
        ac._CONFIG_PATH, ac._CONFIG_TEMPLATE_PATH = bad, tmp / "absent.json"
        profiles, default = ac.load_acceptance_profiles()
        self.assertIn(default, profiles)
        self.assertEqual(profiles["annual_report"]["traceability"]["financial"], 0.7)

    def test_unknown_report_type_falls_back_to_default(self):
        self._point_at({"acceptance": {"default_profile": "event_study"}})
        name, prof = ac.acceptance_profile("no_such_genre")
        self.assertEqual(name, "event_study")
        self.assertFalse(prof["freshness_required"])


class TestTraceabilityThresholdByProfile(unittest.TestCase):
    """阈值来源切换：只有 financial/research 两轴受体裁影响。"""

    def test_thresholds_per_genre(self):
        cases = {
            "annual_report": (0.7, 0.5),
            "semi_annual": (0.55, 0.4),
            "event_study": (0.5, 0.3),
        }
        for genre, (fin, res) in cases.items():
            _name, prof = ac.acceptance_profile(genre)
            self.assertEqual(ac.traceability_threshold_for("financial", prof), fin, genre)
            self.assertEqual(ac.traceability_threshold_for("research", prof), res, genre)

    def test_other_domains_keep_domain_threshold(self):
        """crypto/macro/news 不是"报告体裁"问题：域阈值不得被体裁改写。"""
        _name, prof = ac.acceptance_profile("event_study")
        for domain in ("crypto", "macro", "news"):
            self.assertEqual(ac.traceability_threshold_for(domain, prof),
                             ac._TRACEABILITY_THRESHOLDS[domain], domain)
        self.assertEqual(ac.traceability_threshold_for(None, prof), 0.7)

    def test_unknown_genre_uses_default_profile(self):
        _name, prof = ac.acceptance_profile("whatever")
        self.assertEqual(ac.traceability_threshold_for("financial", prof), 0.7)


class TestReportTypeResolution(unittest.TestCase):

    def test_goal_text_markers(self):
        self.assertEqual(ac.resolve_report_type("分析贵州茅台2026年半年度经营情况"), "semi_annual")
        self.assertEqual(ac.resolve_report_type("分析贵州茅台2025年年度报告"), "annual_report")
        self.assertEqual(ac.resolve_report_type("研究某公司发行股份购买资产公告的市场反应"),
                         "event_study")
        self.assertEqual(ac.resolve_report_type("比特币最新价格"), "annual_report")

    def test_meta_declaration_wins_over_text(self):
        """元数据显式声明优先（当前链路尚无该字段，声明了就采信）。"""
        meta = {"run_options_json": json.dumps({"report_type": "event_study"})}
        meta["run_options"] = json.loads(meta["run_options_json"])
        self.assertEqual(ac.resolve_report_type("分析贵州茅台2025年年度报告", meta),
                         "event_study")
        # 顶层键同样识别；未知取值不猜，回落文本判定
        self.assertEqual(ac.resolve_report_type("", {"report_type": "semi_annual"}), "semi_annual")
        self.assertEqual(ac.resolve_report_type("分析贵州茅台2025年年度报告",
                                               {"report_type": "made_up"}), "annual_report")

    def test_delivery_meta_helper_degrades_gracefully(self):
        """任务元数据读不到时返回空 dict（不抛），验收照常进行。"""
        import delivery_pipeline as dp
        meta = dp.accept_task_meta("no-such-task-id-for-t4")
        self.assertIsInstance(meta, dict)


class TestSemiAnnualThresholdInRunAcceptance(unittest.TestCase):
    """同一份正文：年报阈值判失败、半年报阈值判通过（阈值来源切换的端到端证据）。"""

    def setUp(self):
        self.ws = _mk_ws("semi")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_annual_fails_and_semi_passes(self):
        annual = ac.run_acceptance("t4-semi", _SEMI_GOAL, _SEMI_REPORT, self.ws,
                                   report_type="annual_report")
        semi = ac.run_acceptance("t4-semi", _SEMI_GOAL, _SEMI_REPORT, self.ws,
                                report_type="semi_annual")
        a_nt, s_nt = annual["checks"]["number_traceability"], semi["checks"]["number_traceability"]
        self.assertEqual((a_nt["threshold"], s_nt["threshold"]), (0.7, 0.55))
        self.assertGreaterEqual(s_nt["traceable"] if isinstance(s_nt["traceable"], int)
                                else len(s_nt["traceable"]), 4)
        self.assertFalse(a_nt["pass"], f"年报阈值下应判失败：{a_nt['details']}")
        self.assertTrue(s_nt["pass"], f"半年报阈值下应判通过：{s_nt['details']}")
        self.assertEqual(annual["overall"], "fail")
        self.assertEqual(semi["overall"], "pass")
        self.assertEqual(semi["report_type"], "semi_annual")
        self.assertEqual(annual["report_type"], "annual_report")
        # 判定规则未变：通过时文案仍如实报告溯源率（仅阈值来源随体裁变化）
        self.assertTrue(s_nt["details"].startswith("数字溯源率"), s_nt["details"])

    def test_report_type_recorded_in_acceptance_event(self):
        r = ac.run_acceptance("t4-evt", _SEMI_GOAL, _SEMI_REPORT, self.ws,
                              report_type="semi_annual")
        event = ac.build_acceptance_event(r, trigger="报告步骤", iteration=1)
        self.assertEqual(event["report_type"], "semi_annual")
        self.assertEqual(event["profile"], r["profile"])
        self.assertEqual(event["overall"], r["overall"])


class TestEventStudyFreshnessExemption(unittest.TestCase):
    """freshness_required=false：改判替代词表，命中通过、未命中仅提示不判失败。"""

    def setUp(self):
        self.ws = _mk_ws("event")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_alternative_marker_passes_without_deadline_block(self):
        r = ac.run_acceptance("t4-es", _EVENT_GOAL, _EVENT_REPORT, self.ws,
                              report_type="event_study")
        fb = r["checks"]["freshness_block"]
        self.assertFalse(fb["freshness_required"])
        self.assertTrue(fb["has_alternative"])
        self.assertTrue(fb["pass"], fb["details"])
        self.assertFalse(fb["has_freshness_block"], "事件研究天然没有'数据截止'块")
        self.assertFalse([g for g in r["gaps"] if "数据时效" in g],
                         "豁免体裁不得把数据时效计入缺口")

    def test_same_report_fails_under_annual_report(self):
        """对照：同一份事件研究正文按年报口径验收 → 数据时效判失败（豁免确实是体裁导致的）。"""
        r = ac.run_acceptance("t4-es-an", _EVENT_GOAL, _EVENT_REPORT, self.ws,
                              report_type="annual_report")
        fb = r["checks"]["freshness_block"]
        self.assertTrue(fb["freshness_required"])
        self.assertFalse(fb["pass"])
        self.assertTrue([g for g in r["gaps"] if "数据时效" in g])

    def test_missing_alternative_is_hint_only(self):
        report = _EVENT_REPORT.replace("以公告日2026-03-15为事件日 t0", "窗口起点为研究设定")
        r = ac.run_acceptance("t4-es-miss", _EVENT_GOAL, report, self.ws,
                              report_type="event_study")
        fb = r["checks"]["freshness_block"]
        self.assertTrue(fb["pass"], "未命中替代词也不得判失败")
        self.assertTrue(fb["gaps"], "应留下提示性缺口（供人工看）")
        self.assertFalse([g for g in r["gaps"] if "事件时点" in g],
                         "提示性缺口不得进入 overall 缺口列表")

    def test_no_freshness_semantics_still_skipped(self):
        """豁免不改变"无时效语义即跳过"的原行为。"""
        r = ac.run_acceptance("t4-es-none", "写一个排序函数", "def sort(x): return sorted(x)",
                              self.ws, report_type="event_study")
        fb = r["checks"]["freshness_block"]
        self.assertFalse(fb["enabled"])
        self.assertTrue(fb["pass"])


class TestPlaceholderMaxByProfile(unittest.TestCase):

    def setUp(self):
        self.ws = _mk_ws("ph")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.report = ("营业收入：数据缺失\n净利润：未披露\n毛利率：待补充\n现金流：数据缺失\n"
                       "## 数据说明\n数据截止2026-04-30\n\n"
                       "## 免责声明\n本报告不构成投资建议。\n")
        self.goal = "分析贵州茅台年报"   # financial 域目标（占位上限只对 financial 域计数）

    def test_placeholder_max_from_profile(self):
        annual = ac.run_acceptance("t4-ph", self.goal, self.report, self.ws,
                                   report_type="annual_report")
        semi = ac.run_acceptance("t4-ph", self.goal, self.report, self.ws,
                                 report_type="semi_annual")
        a_dc = annual["checks"]["deliverable_completeness"]
        s_dc = semi["checks"]["deliverable_completeness"]
        self.assertEqual((a_dc["placeholder_count"], a_dc["placeholder_max"]), (4, 3))
        self.assertFalse(a_dc["pass"], a_dc["details"])
        self.assertEqual(s_dc["placeholder_max"], 5)
        self.assertTrue(s_dc["pass"], s_dc["details"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
