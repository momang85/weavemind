# -*- coding: utf-8 -*-
"""财务链路解锁回归：公司名提取 → 报告期粒度 → 合并标签 → 数字溯源。

背景（实测）：目标"梳理贵州茅台2025年三季报核心财务数据"曾拿不到毛利率/现金流，
根因不在数据源（东财本来就返回 XSMLL/NETCASH_OPERATE），而在
`_extract_company` 把公司名提成了 `年三` → resolve_company 失败 →
`route_structured` 返回 None → 结构化财务从未命中。公司名与报告期两个环节
都必须有回归钉子。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import acceptance_checker
from adapters.eastmoney import _select_period_rows
from adapters.router import _wants_quarterly
from structured_pipeline import (
    StructuredPipelineMixin, _caliber_tag, _period_tag,
)
from task_classifier import _extract_company, classify_task


class TestCompanyExtraction(unittest.TestCase):
    """公司名提取：报告期短语不得被当成公司名的一部分。"""

    CASES = [
        # 目标原句（此前取到 `年三` → 整条结构化链路失效）
        ("梳理贵州茅台2025年三季报核心财务数据（营收/净利润/毛利率/现金流）",
         "贵州茅台"),
        ("我要贵州茅台2025年三季报的毛利率和现金流", "贵州茅台"),
        ("宁德时代三季报的营收情况", "宁德时代"),
        ("贵州茅台2025年年报", "贵州茅台"),
        ("分析比亚迪2024年中报", "比亚迪"),
        ("贵州茅台2025年一季报营收", "贵州茅台"),
        # 既有行为必须保持
        ("贵州茅台近三年营收", "贵州茅台"),
        ("宁德时代历年年度净利润", "宁德时代"),
        ("600519（贵州茅台）2025年三季报", "贵州茅台"),
        ("贵州茅台（600519）2025年三季报", "贵州茅台"),
        ("贵州茅台集团2025年三季报", "贵州茅台"),
    ]

    def test_company_names_extracted(self):
        bad = []
        for goal, expect in self.CASES:
            got = _extract_company(goal)
            if got != expect:
                bad.append(f"{goal!r} -> {got!r}（期望 {expect!r}）")
        self.assertEqual(bad, [], "公司名提取回归：" + "；".join(bad))

    def test_period_fragments_never_returned(self):
        """报告期残片（年三/三/年半…）绝不能作为公司名返回。"""
        for goal in ("2025年三季报", "看看三季报", "评估其财务健康",
                     "2025年年度报告摘要"):
            got = _extract_company(goal)
            self.assertNotIn(got, ("年三", "三", "年半", "年一", "季报"), 
                             f"{goal!r} 提取出报告期残片：{got!r}")


class TestPeriodSelection(unittest.TestCase):
    """报告期粒度：默认年报口径不变，季报目标按期取。"""

    ROWS = [
        {"REPORT_DATE": "2025-09-30", "REPORT_TYPE": "三季报"},
        {"REPORT_DATE": "2025-06-30", "REPORT_TYPE": "中报"},
        {"REPORT_DATE": "2025-03-31", "REPORT_TYPE": "一季报"},
        {"REPORT_DATE": "2024-12-31", "REPORT_TYPE": "年报"},
        {"REPORT_DATE": "2024-09-30", "REPORT_TYPE": "三季报"},
        {"REPORT_DATE": "2023-12-31", "REPORT_TYPE": "年报"},
    ]

    def test_annual_default_keeps_only_annual(self):
        got = _select_period_rows(self.ROWS, "annual", 12)
        self.assertEqual([r["REPORT_TYPE"] for r in got], ["年报", "年报"])
        self.assertEqual([r["REPORT_DATE"][:4] for r in got], ["2024", "2023"])

    def test_missing_period_defaults_to_annual(self):
        self.assertEqual(
            [r["REPORT_TYPE"] for r in _select_period_rows(self.ROWS, "", 12)],
            ["年报", "年报"], "未指定 period 时必须保持既有年报行为",
        )

    def test_quarter_drops_annual_and_sorts_newest_first(self):
        got = _select_period_rows(self.ROWS, "quarter", 12)
        self.assertEqual(
            [r["REPORT_TYPE"] for r in got], ["三季报", "中报", "一季报", "三季报"])
        self.assertEqual(got[0]["REPORT_DATE"], "2025-09-30", "最新期次必须排在最前")

    def test_all_keeps_every_period(self):
        self.assertEqual(len(_select_period_rows(self.ROWS, "all", 12)), 6)

    def test_max_years_caps_result(self):
        self.assertEqual(len(_select_period_rows(self.ROWS, "quarter", 2)), 2)


class TestQuarterlyIntent(unittest.TestCase):
    def test_quarter_goals_detected(self):
        for goal in ("茅台三季报", "看中报现金流", "2025年一季报", "单季营收",
                     "半年报毛利率", "季度报告数据"):
            self.assertTrue(_wants_quarterly(goal), f"{goal!r} 应判为季报口径")

    def test_annual_goals_not_misread(self):
        for goal in ("茅台近三年营收", "历年年度净利润", "梳理财务健康度",
                     "最新财报数据"):
            self.assertFalse(_wants_quarterly(goal), f"{goal!r} 不应判为季报口径")


class TestPeriodLabels(unittest.TestCase):
    def test_period_tag(self):
        self.assertEqual(_period_tag({"year": 2025, "report_type": "三季报"}), "2025Q3")
        self.assertEqual(_period_tag({"year": 2025, "report_type": "中报"}), "2025Q2")
        self.assertEqual(_period_tag({"year": 2025, "report_type": "半年报"}), "2025Q2")
        self.assertEqual(_period_tag({"year": 2025, "report_type": "一季报"}), "2025Q1")
        self.assertEqual(_period_tag({"year": 2024, "report_type": "年报"}), "2024年")
        self.assertEqual(_period_tag({"year": None}), "")

    def test_caliber_tag_states_report_period(self):
        got = _caliber_tag({"report_type": "三季报", "report_date": "2025-09-30"})
        self.assertIn("三季报口径", got)
        self.assertIn("2025-09-30", got)
        self.assertIn("年报口径", _caliber_tag({"report_type": "年报"}))


class TestMergeStructuredFinancials(unittest.TestCase):
    """合并标签必须带报告期：否则三季报与年报同年会互相覆盖/误去重。"""

    def test_labels_carry_report_period(self):
        clean = {"market_data": []}
        fin = [{"year": 2025, "report_type": "三季报", "report_date": "2025-09-30",
                "revenue": 1309.04, "gross_margin": 91.29,
                "operating_cashflow": 381.97}]
        out = StructuredPipelineMixin._merge_structured_financials(
            clean, fin, "https://example.invalid/src")
        labels = [r["label"] for r in out["market_data"]]
        self.assertIn("2025Q3营收", labels)
        self.assertIn("2025Q3毛利率", labels)
        self.assertIn("2025Q3经营现金流", labels)
        for r in out["market_data"]:
            self.assertIn("三季报口径", r["caliber"])
        self.assertEqual(out["market_data"][0]["source"], "https://example.invalid/src")

    def test_annual_and_quarter_do_not_collide(self):
        clean = {"market_data": []}
        rows = [
            {"year": 2025, "report_type": "年报", "revenue": 1700.0},
            {"year": 2025, "report_type": "三季报", "revenue": 1309.04},
        ]
        out = StructuredPipelineMixin._merge_structured_financials(
            clean, rows, "u")
        labels = [r["label"] for r in out["market_data"]]
        self.assertEqual(len(labels), 2, "同年不同报告期不得被去重合并")
        self.assertIn("2025年营收", labels)
        self.assertIn("2025Q3营收", labels)

    def test_entity_prefix_kept(self):
        out = StructuredPipelineMixin._merge_structured_financials(
            {"market_data": []},
            [{"year": 2025, "report_type": "三季报", "revenue": 1.0}],
            "u", entity="贵州茅台")
        self.assertEqual(out["market_data"][0]["label"], "贵州茅台2025Q3营收")

    def test_duplicate_rows_deduped(self):
        fin = [{"year": 2025, "report_type": "三季报", "revenue": 10.0}] * 2
        out = StructuredPipelineMixin._merge_structured_financials({"market_data": []}, fin, "u")
        self.assertEqual(len(out["market_data"]), 1)


class TestFinancialsParticipateInTraceability(unittest.TestCase):
    """financials.json 必须参与数字溯源（清洗步骤没跑过时它是唯一来源）。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_fin_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.ws = tmp
        proj = tmp / "project"
        proj.mkdir(parents=True)
        (proj / "financials.json").write_text(json.dumps({
            "financials": [{
                "year": 2025, "report_type": "三季报", "report_date": "2025-09-30",
                "revenue": 1309.04, "gross_margin": 91.29,
                "operating_cashflow": 381.97,
            }],
        }, ensure_ascii=False), encoding="utf-8")

    def test_collect_sources_includes_financials(self):
        src = acceptance_checker._collect_sources(self.ws)
        self.assertIn("financials", src)
        self.assertIn("1309.04", src["financials"])
        self.assertIn("91.29", src["financials"])
        self.assertIn("三季报", src["financials"])

    def test_numbers_traceable_from_financials_only(self):
        src = acceptance_checker._collect_sources(self.ws)
        report = ("## 核心财务数据\n"
                  "- 营业总收入：1309.04 亿元\n"
                  "- 毛利率：91.29%\n"
                  "- 经营性现金流：381.97 亿元\n"
                  "- 虚构数字：7777.77 亿元\n")
        res = acceptance_checker.check_number_traceability(
            report, src, domain="financial")
        traceable = {str(t.get("value")) for t in res["traceable"]}
        self.assertIn("1309.04", traceable, "营收必须可溯源到 financials.json")
        self.assertIn("91.29", traceable, "毛利率必须可溯源到 financials.json")
        self.assertIn("381.97", traceable, "现金流必须可溯源到 financials.json")
        untraceable = {str(t.get("value")) for t in res["untraceable"]}
        self.assertIn("7777.77", untraceable, "编造的数字不得被判可溯源")


class TestMultiEntityExtraction(unittest.TestCase):
    """多实体对比拆分：报告期短语不得被当成公司名的一部分。"""

    CASES = [
        ("对比宁德时代与比亚迪三季报营收", ["宁德时代", "比亚迪"]),
        ("对比宁德时代与比亚迪的三季报营收和净利润", ["宁德时代", "比亚迪"]),
        ("对比宁德时代与比亚迪近三年营收和净利润趋势", ["宁德时代", "比亚迪"]),
        ("比较苹果和微软的营收", ["苹果", "微软"]),
        ("苹果vs微软的营收", ["苹果", "微软"]),
        ("分别分析腾讯控股和阿里巴巴集团的近三年营收",
         ["腾讯控股", "阿里巴巴集团"]),
        # 非对比语境不启用拆分（既有行为）
        ("与比亚迪合作开发电池项目", []),
        ("腾讯控股2025年报分析", []),
    ]

    def test_entities_split_without_period_fragments(self):
        bad = []
        for goal, expect in self.CASES:
            got = classify_task(goal).get("companies")
            if got != expect:
                bad.append(f"{goal!r} -> {got!r}（期望 {expect!r}）")
        self.assertEqual(bad, [], "多实体拆分回归：" + "；".join(bad))


class TestRouterPeriodWiring(unittest.TestCase):
    """季报目标的报告期必须一路透传到抓取函数（多实体路径同样）。

    回归背景：`_fetch_financial_entity` 曾引用不存在的 `goal` 变量，NameError 被
    宽 except 吞掉 → 多实体财务"全部实体失败"并静默回退搜索，测试才暴露出来。
    """

    _RES = {"market": "CN", "stock_code": "600519", "name": "贵州茅台",
            "quote_id": "1.600519", "resolved_alternatives": []}

    @staticmethod
    def _payload():
        return {
            "financials": [{"year": 2025, "report_type": "三季报", "revenue": 1.0}],
            "metadata": {"annual_count": 1, "period": "quarter"},
        }

    def _route(self, goal: str):
        import adapters.router as router
        seen: list[str | None] = []

        def _fetch(name, code, **kw):
            seen.append(kw.get("period"))
            return self._payload()

        with mock.patch.object(router, "resolve_company",
                               return_value=dict(self._RES)), \
                mock.patch.object(router, "fetch_cn_or_fallback", side_effect=_fetch):
            out = router.route_structured(goal)
        return out, seen

    def test_quarter_goal_passes_quarter(self):
        out, seen = self._route("贵州茅台2025年三季报营收和毛利率")
        self.assertIsNotNone(out, "季报目标必须能路由到结构化财务")
        self.assertEqual(seen, ["quarter"])

    def test_annual_goal_passes_annual(self):
        out, seen = self._route("贵州茅台近三年营收")
        self.assertIsNotNone(out)
        self.assertEqual(seen, ["annual"])

    def test_multi_entity_quarter_goal_passes_quarter_to_each_entity(self):
        out, seen = self._route("对比宁德时代与比亚迪三季报营收")
        self.assertIsNotNone(out, "多实体财务路由不得因内部异常整体失败")
        self.assertEqual(out.get("source"), "multi_entity")
        self.assertEqual(len(out.get("companies") or []), 2)
        self.assertEqual(seen, ["quarter", "quarter"],
                         "每个实体的抓取都要带 quarter")


class TestEastmoneyMetadata(unittest.TestCase):
    """period 必须回写到 metadata，否则下游无法判断口径。"""

    def test_metadata_declares_period(self):
        src = Path("adapters/eastmoney.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count('"period": str(period or "annual")'), 2,
                                "两个抓取函数（HK/CN）都要回写 period")
        self.assertIn("report_date", src, "行必须带报告期日期")


if __name__ == "__main__":
    unittest.main()
