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


class TestContractPersistenceAndSubject(unittest.TestCase):
    """A 批：契约要能原样落库/读回，主体比对要以**稳定标识**为先。"""

    def _req(self) -> F.ResearchRequest:
        return F.parse_research_request(
            "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            company="贵州茅台", company_id="600519.SH", market="cn",
            caliber="合并", as_of="2025-04-30", identity_source="form")

    def test_payload_round_trip_keeps_identity(self):
        req = self._req()
        back = F.ResearchRequest.from_payload(req.to_payload())
        self.assertIsNotNone(back)
        self.assertEqual(back.company, "贵州茅台")
        self.assertEqual(back.company_id, "600519.SH")
        self.assertEqual(back.market, "cn")
        self.assertEqual(back.caliber, "合并")
        self.assertEqual(back.periods, [2023, 2024])
        self.assertEqual(back.as_of, "2025-04-30")
        self.assertEqual(back.identity_source, "form")
        self.assertEqual(back.gaps, req.gaps)

    def test_broken_payload_is_none_not_guessed(self):
        self.assertIsNone(F.ResearchRequest.from_payload(None))
        self.assertIsNone(F.ResearchRequest.from_payload({}))
        self.assertIsNone(F.ResearchRequest.from_payload("not a dict"))

    def test_identity_source_marks_text_fallback(self):
        """没有结构化字段时从目标解析：来源标成 text（审计看得出契约是怎么来的）。"""
        req = F.parse_research_request(
            "贵州茅台（600519.SH）2023 与 2024 年年度报告研究")
        self.assertEqual(req.identity_source, "text")
        self.assertEqual(req.company, "贵州茅台")

    def test_text_entry_keeps_gaps_instead_of_guessing(self):
        """自由文本入口识别不出公司时**留缺口**，不猜、也不让数据源来定。

        实测（A 批记录）：`task_classifier._extract_company` 对"研究贵州茅台 2023 与
        2024 年营业收入"这类短名给不出公司（对全称还会截成"台酒股份有限"）——这正是
        这份契约要求表单送结构化字段的原因；文本入口只作为兼容通道，歧义留 `gaps`。
        """
        req = F.parse_research_request("研究贵州茅台 2023 与 2024 年营业收入")
        self.assertEqual(req.company, "")
        self.assertTrue(req.needs_confirmation)
        self.assertTrue(any("未识别到公司" in g for g in req.gaps), req.gaps)

    def test_caller_identity_is_not_overwritten_by_text(self):
        """调用方（表单）给的身份不被目标文本改掉——文本解析只在缺身份时兜底。"""
        req = F.parse_research_request(
            "研究比亚迪 2023 与 2024 年营业收入",
            company="贵州茅台", company_id="600519.SH", identity_source="form")
        self.assertEqual(req.company, "贵州茅台")
        self.assertEqual(req.company_id, "600519.SH")
        self.assertEqual(req.identity_source, "form")

    def test_check_subject_id_first(self):
        req = self._req()
        # 全称/简称：靠归一名称
        self.assertTrue(F.check_subject(req, "贵州茅台酒股份有限公司", "600519")[0])
        # 稳定标识优先：名称不同但标识相同（别名/改名场景）也算一致
        self.assertTrue(F.check_subject(req, "贵州茅台（旧名）", "600519.SH")[0])
        # 标识不同 → 不一致（即使名称相同）
        ok, why = F.check_subject(req, "贵州茅台", "002594")
        self.assertFalse(ok)
        self.assertIn("标识", why)

    def test_check_subject_refuses_when_subject_missing(self):
        req = self._req()
        ok, why = F.check_subject(req, "", "")
        self.assertFalse(ok)
        self.assertIn("缺主体", why)
        req_no_name = F.parse_research_request("研究某公司", company="", company_id="")
        self.assertFalse(F.check_subject(req_no_name, "贵州茅台", "600519")[0])

    def test_check_subject_rejects_unrelated_company(self):
        req = self._req()
        ok, why = F.check_subject(req, "比亚迪", "002594")
        self.assertFalse(ok, why)


class TestCaliberIsSourceDeclared(unittest.TestCase):
    """口径只认**来源声明的**：行级 > 实体级 > unknown；不从别处借、不默认合并。"""

    def test_row_caliber_wins(self):
        payload = {"financials": [{"year": 2024, "report_type": "年报",
                                   "caliber": "母公司", "revenue": 100.0}],
                   "metadata": {"company": "示例", "caliber": "合并"},
                   "raw": {"url": "https://example.invalid/a", "text": "{}"}}
        fact = F.facts_from_financials(payload)[0]
        self.assertEqual(fact.caliber, "母公司")

    def test_entity_level_caliber_used_as_fallback(self):
        payload = {"financials": [{"year": 2024, "report_type": "年报", "revenue": 100.0}],
                   "metadata": {"company": "示例", "caliber": "合并"},
                   "raw": {"url": "https://example.invalid/a", "text": "{}"}}
        self.assertEqual(F.facts_from_financials(payload)[0].caliber, "合并")

    def test_missing_caliber_stays_unknown(self):
        payload = {"financials": [{"year": 2024, "report_type": "年报", "revenue": 100.0}],
                   "metadata": {"company": "示例"},
                   "raw": {"url": "https://example.invalid/a", "text": "{}"}}
        self.assertEqual(F.facts_from_financials(payload)[0].caliber, F.UNKNOWN)


class TestResearchShape(unittest.TestCase):
    """“是不是研究任务”的唯一定义（固定路径与交付硬门槛共用）。"""

    def _req(self, **kw):
        base = dict(company="贵州茅台", company_id="600519.SH", periods=[2023, 2024],
                    caliber="合并")
        base.update(kw)
        return F.parse_research_request("表单提交", **base)

    def test_shaped_requires_subject_periods_and_caliber(self):
        self.assertTrue(F.research_shaped(self._req()))
        self.assertFalse(F.research_shaped(self._req(periods=[2024])),
                         "单期间不是两年度研究")
        self.assertFalse(F.research_shaped(self._req(caliber="")),
                         "口径未知不得当研究任务（不默认合并）")
        self.assertFalse(F.research_shaped(self._req(company="", company_id="")),
                         "没有主体就没有研究任务")
        self.assertFalse(F.research_shaped(None))

    def test_subject_is_the_looser_gate_criterion(self):
        """门槛口径（有主体即可）比路径选择（严格版）宽：两者同源、各有其用。"""
        only_subject = self._req(periods=[2024], caliber="")
        self.assertTrue(F.research_subject(only_subject))
        self.assertFalse(F.research_shaped(only_subject))
        self.assertTrue(F.research_subject(self._req(company="", company_id="600519.SH")),
                        "有稳定标识也算有主体")

    def test_code_only_contract_still_shaped(self):
        self.assertTrue(F.research_shaped(self._req(company="")))


class TestCaliberEvidence(unittest.TestCase):
    """口径证据链：口径只认来源声明的，且**必须能说出依据**（否则按未知处理）。"""

    def _payload(self, *, md_extra=None, row_extra=None):
        row = {"year": 2024, "report_type": "年报", "revenue": 100.0}
        row.update(row_extra or {})
        md = {"source": "eastmoney_ashare", "company": "示例", "currency": "CNY",
              "unit": "亿元"}
        md.update(md_extra or {})
        return {"financials": [row], "metadata": md,
                "raw": {"url": "https://example.invalid/a", "text": "{}"}}

    def test_metadata_declaration_is_recorded_with_evidence(self):
        """适配器按来源结构声明的口径：采信，并留下依据原文（可复核）。"""
        facts = F.facts_from_financials(self._payload(md_extra={
            "caliber": "合并",
            "caliber_evidence": "来源行含 PARENTNETPROFIT → 合并报表口径"}))
        self.assertEqual(facts[0].caliber, "合并")
        self.assertEqual(facts[0].caliber_source, "metadata")
        self.assertIn("PARENTNETPROFIT", facts[0].caliber_evidence)

    def test_row_declaration_wins_over_metadata(self):
        facts = F.facts_from_financials(self._payload(
            md_extra={"caliber": "合并", "caliber_evidence": "实体级依据"},
            row_extra={"caliber": "母公司",
                       "caliber_evidence": "行内声明：母公司报表"}))
        self.assertEqual(facts[0].caliber, "母公司")
        self.assertEqual(facts[0].caliber_source, "row")
        self.assertIn("行内声明", facts[0].caliber_evidence)

    def test_declaration_without_evidence_is_still_labeled(self):
        """有口径但没写依据：仍采信，但证据要标出"未附依据"（不假装有据）。"""
        facts = F.facts_from_financials(self._payload(md_extra={"caliber": "合并"}))
        self.assertEqual(facts[0].caliber, "合并")
        self.assertIn("未附依据", facts[0].caliber_evidence)

    def test_no_declaration_stays_unknown_fail_closed(self):
        """来源没声明 → unknown 且证据为空（不默认合并、不用请求的口径顶替）。"""
        facts = F.facts_from_financials(self._payload())
        self.assertEqual(facts[0].caliber, F.UNKNOWN)
        self.assertEqual(facts[0].caliber_source, "")
        self.assertEqual(facts[0].caliber_evidence, "")

    def test_period_text_is_not_a_caliber(self):
        """把期间描述塞进口径字段（如"三季报口径"）不采信：那是另一个维度。"""
        facts = F.facts_from_financials(self._payload(
            row_extra={"caliber": "三季报口径"}))
        self.assertEqual(facts[0].caliber, F.UNKNOWN)

    def test_disclosure_date_becomes_disclosed_at(self):
        """公告日期（NOTICE_DATE）是"截至日能否成立"的证据，与报告期末分开记。"""
        facts = F.facts_from_financials(self._payload(
            row_extra={"report_date": "2024-12-31", "disclosure_date": "2025-04-03"}))
        self.assertEqual(facts[0].disclosed_at, "2025-04-03")
        self.assertEqual(facts[0].period_end, "2024-12-31")

    def test_period_end_fallback_is_recorded(self):
        """没有公告日期的来源：回落到报告期末（已知弱点，见证据文档）。"""
        facts = F.facts_from_financials(self._payload(
            row_extra={"report_date": "2024-12-31"}))
        self.assertEqual(facts[0].disclosed_at, "2024-12-31")


class TestPerspectiveAndRatioConditions(unittest.TestCase):
    """F2：阅读视角**只认声明**（不按公司名/机构名推断），比率要写适用条件。"""

    GOAL = ("研究中国工商银行 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30")

    def _req(self, **over):
        kw = dict(company="中国工商银行", company_id="601398.SH", market="cn",
                  periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
                  identity_source="form")
        kw.update(over)
        return F.parse_research_request(self.GOAL, **kw)

    def test_perspective_round_trips_through_payload(self):
        req = self._req(perspective="bank_corporate")
        self.assertEqual(req.perspective, "bank_corporate")
        back = F.ResearchRequest.from_payload(req.to_payload())
        self.assertEqual(back.perspective, "bank_corporate")

    def test_perspective_is_not_inferred_from_subject(self):
        """银行做权益投研时仍用权益视角：公司名里带"银行"不得自动切成信用分析。"""
        self.assertEqual(self._req().perspective, F.DEFAULT_PERSPECTIVE)
        self.assertEqual(F.ResearchRequest.from_payload(
            {"company": "中国工商银行"}).perspective, F.DEFAULT_PERSPECTIVE)

    def test_invalid_perspective_falls_back_to_default(self):
        self.assertEqual(self._req(perspective="credit").perspective,
                         F.DEFAULT_PERSPECTIVE)
        self.assertEqual(self._req(perspective="").perspective, F.DEFAULT_PERSPECTIVE)
        self.assertEqual(F.ResearchRequest.from_payload(
            {"company": "X", "perspective": "信用分析"}).perspective,
            F.DEFAULT_PERSPECTIVE)

    def test_every_derived_ratio_has_applicability_conditions(self):
        for metric, _label in F.DERIVED_METRICS:
            self.assertIn(metric, F.RATIO_CONDITIONS, metric)
        self.assertIn("同为正", F.RATIO_CONDITIONS["cashflow_coverage"],
                      "覆盖倍数的符号条件必须写明")
        self.assertIn("金融机构", F.RATIO_CONDITIONS["debt_ratio"])
        self.assertIn("非金融企业", F.RATIO_SCOPE_NOTE)

    def test_perspective_requirements_are_declared_only(self):
        from orchestrator_v2 import perspective_requirements
        self.assertIn("投研", perspective_requirements("equity"))
        self.assertIn("银行对公客户研究", perspective_requirements("bank_corporate"))
        self.assertEqual(perspective_requirements(""), "")
        self.assertEqual(perspective_requirements("credit"), "")

    def test_perspective_labels_avoid_the_word_source(self):
        """视角标签里不得出现"来源"：验收器的来源标注修复会把"来源：X"改写成
        "来源：基于模型知识…"，实机里把"增长来源与盈利质量"改成了模型知识声明。"""
        for key, label in F.PERSPECTIVES:
            self.assertNotIn("来源", label, key)


class TestResearchRequestSanitizer(unittest.TestCase):
    """表单白名单：视角只收枚举值，非法值丢弃（宁可少字段，也不把脏值当契约）。"""

    def _sanitize(self, raw):
        from web_ui import _sanitize_research_request
        return _sanitize_research_request(raw)

    def test_perspective_is_whitelisted(self):
        self.assertEqual(
            self._sanitize({"company": "贵州茅台", "perspective": "BANK_CORPORATE"}),
            {"company": "贵州茅台", "perspective": "bank_corporate"})
        self.assertEqual(
            self._sanitize({"company": "贵州茅台", "perspective": "credit"}),
            {"company": "贵州茅台"}, "非法视角必须丢弃，不得当契约")
        self.assertEqual(
            self._sanitize({"company": "贵州茅台", "perspective": "equity"}),
            {"company": "贵州茅台", "perspective": "equity"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
