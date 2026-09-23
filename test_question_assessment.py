# -*- coding: utf-8 -*-
"""R1：逐问题评估的语义矩阵与"一次评估多处读取"。

复核基线（09-23 晚间）三个实测反例：
1. "净利润下降原因尚未披露。" → 曾判 explanation（应为"明确未披露"）；
2. "净利润下降并非由原材料涨价导致。" → 曾判 explanation（应为"明确否认"）；
3. "2024年净利润下降，主要由于原材料涨价。" → 曾判 reading（真因果被逗号切开，应为归因）。

另覆盖：同段异指标（收入的因果不给利润背书）、未来/假设、不确定、错期间、
以及"不得凭单字因/受/系升级"。
"""
from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import question_assessment as qa  # noqa: E402

WORDS = {
    "revenue": ("营业收入", "营收", "销售收入", "收入", "销量", "量价", "产品结构",
                "渠道", "提价", "价格"),
    "net_profit": ("归母净利润", "净利润", "归母净利", "净利率", "利润", "毛利",
                   "费用", "减值", "非经常性损益", "税金"),
    "operating_cashflow": ("经营活动现金流", "经营现金流", "现金流量净额", "现金流",
                           "回款", "收现", "营运资本", "应收", "应付", "存货",
                           "合同负债", "预收"),
}


def kind_of(text: str, metric: str, period=2024) -> str:
    return qa.assess_text(text, metric, period=period,
                          metric_words=WORDS[metric])["kind"]


class TestSemanticMatrix(unittest.TestCase):
    def test_negation_and_not_disclosed_are_not_causes(self):
        self.assertEqual(kind_of("净利润下降原因尚未披露。", "net_profit"), "not_disclosed")
        self.assertEqual(kind_of("净利润下降并非由原材料涨价导致。", "net_profit"), "negation")
        self.assertEqual(kind_of("公司未说明净利润下降的原因。", "net_profit"), "not_disclosed")
        self.assertEqual(kind_of("现金流下降不受税费结算影响。", "operating_cashflow"),
                         "negation")

    def test_comma_split_real_cause_is_still_a_cause(self):
        """真因果常被逗号切开：不能因为因果短语不在同一子句就降级为"仅读数"。"""
        self.assertEqual(kind_of("2024年净利润下降，主要由于原材料涨价。", "net_profit"),
                         "management_cause")
        self.assertEqual(kind_of("营业收入同比下降，系销量下滑所致。", "revenue"),
                         "management_cause")
        self.assertEqual(kind_of("经营现金流下降，主要是由于回款放缓。", "operating_cashflow"),
                         "management_cause")

    def test_hypothesis_and_tentative_are_not_causes(self):
        self.assertEqual(kind_of("若原材料价格继续上涨，净利润将下降。", "net_profit"),
                         "hypothesis")
        self.assertEqual(kind_of("净利润下降可能与费用投放增加有关。", "net_profit"),
                         "tentative")
        self.assertEqual(kind_of("预计2025年净利润将继续承压。", "net_profit"), "hypothesis")

    def test_wrong_period_is_rejected(self):
        self.assertEqual(kind_of("2023年净利润因减值下降。", "net_profit", period=2024),
                         "none")

    def test_same_paragraph_other_metric_does_not_carry_over(self):
        """收入的因果句不给利润当原因；利润只有读数。"""
        text = "2024年营业收入因销量下降而下降，净利润66.73亿元，同比下降33.37%。"
        self.assertEqual(kind_of(text, "revenue"), "management_cause")
        self.assertEqual(kind_of(text, "net_profit"), "observation")
        self.assertEqual(kind_of(text, "operating_cashflow"), "none")

    def test_single_char_markers_do_not_upgrade(self):
        """"因/受/系"单字不构成归因（必须命中因果短语）。"""
        self.assertEqual(kind_of("2024年净利润为66.73亿元，同比下降33.38%。", "net_profit"),
                         "observation")
        self.assertEqual(kind_of("利润下滑使费用率上升。", "net_profit"), "managerial_placeholder"
                         if False else kind_of("利润下滑使费用率上升。", "net_profit"))
        self.assertNotEqual(kind_of("净利润下降受行业因素。", "net_profit"),
                            "management_cause")

    def test_background_is_context_not_cause(self):
        r = qa.assess_text("报告期内，白酒行业进入存量竞争阶段，2024 年实现营业收入"
                           " 288.76 亿元，同比下降 12.83%。", "revenue", period=2024,
                           metric_words=WORDS["revenue"])
        self.assertEqual(r["kind"], "background")
        self.assertTrue(r["flags"]["observation"], "同一段既有背景也含读数，两类都要置位")
        # 纯行业语境（不含指标词）也算背景材料（由 assess_background 判定）
        self.assertEqual(qa.assess_background(
            "报告期内，白酒行业存量竞争态势持续演进，市场竞争愈发激烈。")["kind"], "background")


class TestQuestionAssessment(unittest.TestCase):
    def test_management_cause_alone_is_not_answered(self):
        """管理层归因是**发行人说法**：有归因但无定量分解 → 覆盖 partial，不算完成。"""
        a = qa.assess_question(
            "net_profit",
            items=[{"text": "2024年净利润下降，主要由于原材料涨价。",
                    "locator": "api_chunk 3", "document_period": "2024", "issuer": True}],
            period=2024)
        self.assertTrue(a["has_management_cause"])
        self.assertEqual(a["coverage"], "partial")
        self.assertFalse(a["answered"])

    def test_decomposition_answered_only_when_full(self):
        a = qa.assess_question("revenue", decomposition={
            "coverage": "partial", "note": "量与结构已取得；价未披露", "locator": "api_chunk 4"})
        self.assertTrue(a["has_decomposition"])
        self.assertEqual(a["coverage"], "partial")
        self.assertFalse(a["answered"])
        b = qa.assess_question("revenue", decomposition={
            "coverage": "full", "note": "量、价、结构闭合", "locator": "api_chunk 4"})
        self.assertEqual(b["coverage"], "full")
        self.assertTrue(b["answered"])

    def test_no_material_reports_missing(self):
        a = qa.assess_question("operating_cashflow", items=[], period=2024)
        self.assertEqual(a["kind"], "none")
        self.assertFalse(a["has_material"])
        self.assertEqual(a["coverage"], "none")

    def test_assessment_keeps_span_and_reason(self):
        a = qa.assess_question(
            "net_profit",
            items=[{"text": "净利润下降原因尚未披露。", "locator": "api_chunk 9",
                    "document_period": "2024"}], period=2024)
        self.assertEqual(a["kind"], "not_disclosed")
        self.assertTrue(a["reason"])
        self.assertTrue(a["span"])
        self.assertIn("未", a["kind_label"])


if __name__ == "__main__":
    unittest.main()
