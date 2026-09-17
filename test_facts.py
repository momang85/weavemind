# -*- coding: utf-8 -*-
"""S1-2：研究请求契约与最小事实记录的回归。

夹具是**合成的**（非现实公司事实）；要钉住的是"契约与事实记录不许编造"：
说不清的公司/期间/口径/币种一律记缺口或 unknown，来自结构化源也**不等于**已核实。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import facts as F  # noqa: E402

# 固定夹具：合成的美股 10-K 载荷（非现实公司事实）
SEC_PAYLOAD = {
    "financials": [
        {"year": 2023, "report_type": "10-K", "revenue": 210.5, "net_profit": 31.2,
         "operating_cashflow": 55.3},
        {"year": 2024, "report_type": "10-K", "revenue": 245.8, "net_profit": 36.9,
         "operating_cashflow": 61.2},
    ],
    "metadata": {"source": "sec_edgar", "company": "Contoso Semiconductors Inc.",
                 "ticker": "CSTX", "currency": "USD", "unit": "亿美元"},
    "raw": {"url": "https://data.example.invalid/facts/CSTX.json",
            "text": '{"facts": {"us-gaap": {"Revenues": []}}}'},
}


class TestResearchContract(unittest.TestCase):
    """契约：能定的定下来，不能定的**列缺口**，不靠模型猜。"""

    def test_explicit_goal_fills_contract(self):
        req = F.parse_research_request(
            "研究贵州茅台（600519.SH）2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，按合并报表口径，数据截至 2024-12-31",
            company="贵州茅台", company_id="600519.SH", market="cn")
        self.assertEqual(req.company, "贵州茅台")
        self.assertEqual(req.company_id, "600519.SH")
        self.assertEqual(req.market, "cn")
        self.assertEqual(req.periods, [2023, 2024])
        self.assertEqual(req.caliber, "合并")
        self.assertEqual(req.as_of, "2024-12-31")
        self.assertEqual([m for m, _ in F.CORE_METRICS], req.required_metrics)
        self.assertFalse(req.needs_confirmation, req.gaps)

    def test_missing_company_is_a_gap_not_a_guess(self):
        req = F.parse_research_request("研究 2023 与 2024 年的营业收入对比，A股")
        self.assertEqual(req.company, "", "识别不到公司就不能编一个出来")
        self.assertTrue(req.needs_confirmation)
        self.assertTrue(any("未识别到公司" in g for g in req.gaps), req.gaps)

    def test_missing_market_and_caliber_are_gaps(self):
        req = F.parse_research_request("研究贵州茅台 2023 与 2024 年的经营情况",
                                       company="贵州茅台")
        self.assertEqual(req.market, F.UNKNOWN)
        self.assertEqual(req.caliber, F.UNKNOWN, "没声明口径就不能默认成合并报表")
        self.assertTrue(any("未确定市场" in g for g in req.gaps), req.gaps)
        self.assertTrue(any("报表口径" in g for g in req.gaps), req.gaps)

    def test_single_year_is_a_gap(self):
        req = F.parse_research_request("分析贵州茅台 2024 年报营收",
                                       company="贵州茅台", market="cn")
        self.assertEqual(req.periods, [2024])
        self.assertTrue(any("两个明确年度" in g for g in req.gaps), req.gaps)

    def test_missing_as_of_is_a_gap(self):
        req = F.parse_research_request("研究贵州茅台 2023 与 2024 年营收",
                                       company="贵州茅台", market="cn")
        self.assertTrue(any("资料截至日" in g for g in req.gaps), req.gaps)


class TestFactRecord(unittest.TestCase):
    """事实记录：主体/期间/币种单位/来源位置/核实状态都要如实。"""

    def setUp(self):
        self.facts = F.facts_from_financials(SEC_PAYLOAD)

    def test_core_metrics_extracted_with_subject_and_period(self):
        keys = {(f.metric, f.period) for f in self.facts}
        self.assertIn(("revenue", "2023年"), keys)
        self.assertIn(("revenue", "2024年"), keys)
        self.assertEqual(len(self.facts), 6, [f.fact_id for f in self.facts])
        for f in self.facts:
            self.assertEqual(f.entity, "Contoso Semiconductors Inc.")
            self.assertEqual(f.entity_id, "CSTX")
            self.assertEqual(f.currency, "USD")
            self.assertEqual(f.unit, "亿美元")
            self.assertEqual(f.unit_source, "source")
            self.assertEqual(f.period_type, "10-K")
            self.assertTrue(f.source_url.endswith("CSTX.json"))
            self.assertTrue(f.source_hash, "有原文就要算快照 hash")
            self.assertEqual(f.source_locator.get("kind"), "structured_field")

    def test_fact_id_is_stable_across_layers(self):
        """同一事实在各层必须同 ID：重算一遍载荷，ID 不变。"""
        again = F.facts_from_financials(SEC_PAYLOAD)
        self.assertEqual([f.fact_id for f in self.facts],
                         [f.fact_id for f in again])
        rev = [f for f in self.facts if f.metric == "revenue" and f.period == "2024年"][0]
        self.assertEqual(
            rev.fact_id,
            F.make_fact_id("CSTX", "Contoso Semiconductors Inc.", "revenue",
                           "2024年", F.UNKNOWN),
            "ID 只由身份/指标/期间/口径决定，与数值无关（夹具没声明口径 → unknown）")

    def test_value_change_keeps_identity(self):
        """上游修正数值时它仍是同一条事实（改的是值，不是身份）。"""
        fixed = dict(SEC_PAYLOAD)
        fixed["financials"] = [dict(r) for r in SEC_PAYLOAD["financials"]]
        fixed["financials"][1]["revenue"] = 246.9
        new = F.facts_from_financials(fixed)
        old_rev = [f for f in self.facts if f.metric == "revenue" and f.period == "2024年"][0]
        new_rev = [f for f in new if f.metric == "revenue" and f.period == "2024年"][0]
        self.assertEqual(old_rev.fact_id, new_rev.fact_id)
        self.assertNotEqual(old_rev.value, new_rev.value)

    def test_missing_metadata_stays_unknown(self):
        """来源没声明币种/单位 → 记 unknown，不许回落成"亿元"这种具体口径。"""
        payload = {
            "financials": [{"year": 2024, "revenue": 245.8}],
            "metadata": {"company": "示例公司", "code": "000001"},
            "raw": {},
        }
        f = F.facts_from_financials(payload)[0]
        self.assertEqual(f.currency, F.UNKNOWN)
        self.assertEqual(f.unit, F.UNKNOWN)
        self.assertEqual(f.unit_source, F.UNKNOWN)
        self.assertEqual(f.caliber, F.UNKNOWN)
        self.assertEqual(f.source_hash, "", "没有原文就不算 hash，不编一个")

    def test_structured_source_is_not_marked_verified(self):
        for f in self.facts:
            self.assertEqual(f.verify_state, F.VERIFY_UNVERIFIED,
                             "来自结构化源 ≠ 已核实")

    def test_multi_entity_payload_keeps_subjects_apart(self):
        payload = {
            "companies": [
                {"name": "甲公司", "financials": [{"year": 2024, "revenue": 100.0}],
                 "metadata": {"company": "甲公司", "code": "000001",
                              "currency": "CNY", "unit": "亿元"}, "raw": {}},
                {"name": "乙公司", "financials": [{"year": 2024, "revenue": 100.0}],
                 "metadata": {"company": "乙公司", "code": "000002",
                              "currency": "CNY", "unit": "亿元"}, "raw": {}},
            ],
            "metadata": {"source": "multi_entity"},
        }
        out = F.facts_from_financials(payload)
        self.assertEqual(len(out), 2)
        self.assertEqual({f.entity for f in out}, {"甲公司", "乙公司"})
        self.assertEqual(len({f.fact_id for f in out}), 2,
                         "同值不同主体必须是两条事实（否则底稿会把两家混成一家）")


class TestDerivedFact(unittest.TestCase):
    def test_derived_records_formula_and_inputs(self):
        facts = F.facts_from_financials(SEC_PAYLOAD)
        base = [f for f in facts if f.metric == "revenue"]
        yoy = F.derived_fact(base, "revenue_yoy", value=16.77,
                             formula="(245.8 - 210.5) / 210.5 * 100",
                             period="2024年同比", inputs=base)
        self.assertTrue(yoy.derived)
        self.assertEqual(yoy.extracted_by, "derived")
        self.assertEqual(sorted(yoy.derived_from), sorted(f.fact_id for f in base))
        self.assertIn("210.5", yoy.formula)
        self.assertEqual(yoy.verify_state, F.VERIFY_UNVERIFIED,
                         "派生值同样不是已核实")


if __name__ == "__main__":
    unittest.main(verbosity=2)
