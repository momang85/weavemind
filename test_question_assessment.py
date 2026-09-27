# -*- coding: utf-8 -*-
"""逐问题契约与判据表的回归（C2）。

三件事必须同源、可核对：
1. **判据表**：每种问题类型"需要什么证据 / 什么算完整 / 什么算部分 / 什么算没有"，
   以及证据性质（发行人说法 vs 计算自披露数值），都在 `QUESTION_RULES` 里；
2. **完成条件按问题真正问什么算**：组成部分逐项绑定才算 full；材料载荷自报的
   `coverage="full"` 不作数；定性归因不得冒充量化分解；
3. **问题集在任务创建时固定并版本化**：题目/类型/所需证据随执行契约落盘，
   问题集变了指纹就变（旧绑定不能继续判"当前"）。

素材：`evals/real/yanghe_ar2024_pages_20260922.json`（洋河 2024 年报的有界取件，
接口片段定位）——真实文本只用来验**部分覆盖**与"缺哪一项"，不用它假装完整分解。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import question_assessment as qa  # noqa: E402


def _frozen_doc() -> dict:
    fx = json.loads((ROOT / "evals" / "real"
                     / "yanghe_ar2024_pages_20260922.json").read_text(encoding="utf-8"))
    text = "\n".join(str(p.get("text") or "") for p in fx["pages"])
    offsets, pos = [], 0
    for p in fx["pages"]:
        offsets.append([pos, int(p.get("api_chunk") or 0)])
        pos += len(str(p["text"])) + 1
    return {"title": fx["notice_title_meta"], "url": fx["pages"][0]["url"], "text": text,
            "chunk_offsets": offsets, "published_at": fx["published_at"]}


class TestCriteriaTable(unittest.TestCase):
    """判据表本身：四类问题都要有完整条目，且被判据消费者认得。"""

    SUPPORTED = (qa.QTYPE_NUMERIC_CHANGE, qa.QTYPE_ISSUER_EXPLANATION,
                 qa.QTYPE_QUANT_DECOMPOSITION, qa.QTYPE_RELATION)

    def test_every_supported_type_has_full_partial_none_and_nature(self):
        for qtype in self.SUPPORTED:
            rule = qa.QUESTION_RULES.get(qtype) or {}
            for key in ("need", "nature", "full", "partial", "none"):
                self.assertTrue(str(rule.get(key) or "").strip(),
                                f"{qtype} 缺 {key}：{rule}")
            self.assertIn(qtype, qa.QTYPE_LABELS)

    def test_fixed_three_questions_have_types_and_requirements(self):
        for metric in ("revenue", "net_profit", "operating_cashflow"):
            qtype = qa.question_type_of(metric)
            self.assertIn(qtype, self.SUPPORTED, metric)
            self.assertTrue(qa.QUESTION_REQUIREMENTS.get(metric),
                            f"{metric} 必须声明所需组成部分/序列")

    def test_question_set_is_capped_at_three(self):
        qs = qa.question_set()
        self.assertLessEqual(len(qs), 3, qs)
        self.assertEqual([q["metric"] for q in qs][:3],
                         ["revenue", "net_profit", "operating_cashflow"])
        for q in qs:
            self.assertTrue(q["question"])
            self.assertTrue(q["question_type"])
            self.assertTrue(q["requires"])
            self.assertEqual(q["rule"], qa.QUESTION_RULES[q["question_type"]])

    def test_bank_perspective_does_not_change_subject_type_or_question_type(self):
        """银行阅读视角不改变研究对象类型，也不改变问题类型与要求。"""
        equity = {q["metric"]: q["question_type"] for q in qa.question_set()}
        bank = {q["metric"]: q["question_type"]
                for q in qa.question_set(perspective="bank_corporate",
                                         subject_type="non_financial")}
        self.assertEqual(equity, bank)


class TestCoverageByRule(unittest.TestCase):
    """覆盖由组成部分的绑定状态算；**自报覆盖不作数**。"""

    def test_components_drive_coverage(self):
        full, why = qa._coverage_by_rule([{"component": "A", "state": "bound"},
                                          {"component": "B", "state": "bound"}])
        self.assertEqual(full, "full")
        self.assertIn("2", why)
        partial, why2 = qa._coverage_by_rule([{"component": "A", "state": "bound"},
                                              {"component": "B", "state": "missing"}])
        self.assertEqual(partial, "partial")
        self.assertIn("B", why2, "缺口必须点名")
        none, _ = qa._coverage_by_rule([{"component": "A", "state": "missing"}])
        self.assertEqual(none, "none")
        self.assertEqual(qa._coverage_by_rule([])[0], "none")

    def test_declared_full_is_not_trusted(self):
        a = qa.assess_question("revenue", decomposition={"coverage": "full",
                                                         "note": "自报完整"})
        self.assertEqual(a["coverage"], "none")
        self.assertFalse(a["answered"])

    def test_relation_question_full_needs_both_series(self):
        both = qa.assess_question(
            "operating_cashflow",
            components=[{"component": "经营现金流净额（两期）", "state": "bound",
                         "locator": "fact-1"},
                        {"component": "归母净利润（两期）", "state": "bound",
                         "locator": "fact-2"}])
        self.assertEqual(both["coverage"], "full")
        self.assertTrue(both["answered"])
        self.assertEqual(both["kind"], qa.KIND_RELATION)
        self.assertFalse(both["has_decomposition"], "关系类不是分解类")
        one = qa.assess_question(
            "operating_cashflow",
            components=[{"component": "经营现金流净额（两期）", "state": "bound"},
                        {"component": "归母净利润（两期）", "state": "missing",
                         "note": "缺 2023 年"}])
        self.assertEqual(one["coverage"], "partial")
        self.assertIn("归母净利润", one["coverage_reason"])


class TestQuestionTypeJudgements(unittest.TestCase):
    """各类型的完成条件：发行人解释逐项对应才算 full；定性归因不冒充分解。"""

    def test_issuer_explanation_full_only_when_itemised(self):
        itemised = qa.assess_question(
            "net_profit", question_type=qa.QTYPE_ISSUER_EXPLANATION,
            issuer_items=["毛利", "费用"],
            items=[{"text": "2024年净利润下降，主要由于毛利率下降与期间费用增加。",
                    "locator": "api_chunk 5", "document_period": "2024", "issuer": True}],
            period=2024)
        self.assertEqual(itemised["coverage"], "full", itemised)
        vague = qa.assess_question(
            "net_profit", question_type=qa.QTYPE_ISSUER_EXPLANATION,
            issuer_items=["毛利", "费用"],
            items=[{"text": "2024年净利润下降，主要由于外部环境变化。",
                    "locator": "api_chunk 5", "document_period": "2024", "issuer": True}],
            period=2024)
        self.assertEqual(vague["coverage"], "partial",
                         "笼统归因覆盖不到所列各项 → 只能 partial")

    def test_management_cause_alone_never_reaches_full(self):
        a = qa.assess_question(
            "net_profit",
            items=[{"text": "2024年净利润下降，主要由于原材料涨价。",
                    "locator": "api_chunk 3", "document_period": "2024", "issuer": True}],
            period=2024)
        self.assertTrue(a["has_management_cause"])
        self.assertEqual(a["coverage"], "partial")
        self.assertFalse(a["answered"])
        self.assertEqual(a["question_type"], qa.QTYPE_QUANT_DECOMPOSITION)

    def test_wrong_period_and_wrong_subject_do_not_support(self):
        wrong_period = qa.assess_question(
            "net_profit",
            items=[{"text": "2022年净利润下降，主要由于毛利率下降。",
                    "locator": "api_chunk 2", "document_period": "2022", "issuer": True}],
            period=2024)
        self.assertNotEqual(wrong_period["coverage"], "full", wrong_period)
        self.assertNotEqual(wrong_period["kind"], qa.KIND_MANAGEMENT, wrong_period)

    def test_no_material_reports_none(self):
        a = qa.assess_question("operating_cashflow", items=[], period=2024)
        self.assertEqual(a["coverage"], "none")
        self.assertFalse(a["answered"])
        self.assertEqual(a["kind"], qa.KIND_NONE)


class TestRealFrozenMaterial(unittest.TestCase):
    """真实冻结材料：给出**部分覆盖**与"缺哪一项"，不假装完整。"""

    def setUp(self):
        self.doc = _frozen_doc()

    def test_revenue_partial_because_price_is_not_disclosed(self):
        import narrative_evidence as ne
        vp = ne.extract_volume_price([self.doc], periods=[2023, 2024])
        comps = [c for c in (vp.get("components") or [])]
        states = {c["component"]: c["state"] for c in comps}
        self.assertEqual(states.get("量"), "bound", comps)
        self.assertEqual(states.get("结构"), "bound", comps)
        self.assertEqual(states.get("价"), "missing",
                         "发行人未披露价格口径，不得算取得")
        a = qa.assess_question("revenue", components=comps, period=2024)
        self.assertEqual(a["coverage"], "partial")
        self.assertFalse(a["answered"])
        self.assertIn("价", a["coverage_reason"])

    def test_profit_partial_with_gross_margin_and_expenses_bound(self):
        import narrative_evidence as ne
        pd = ne.extract_profit_decomposition([self.doc], periods=[2023, 2024])
        states = {c["component"]: c["state"] for c in (pd.get("components") or [])}
        self.assertEqual(states.get("毛利端"), "bound")
        self.assertEqual(states.get("期间费用"), "bound")
        self.assertEqual(states.get("税项与非经常性损益"), "missing")
        # 毛利额与费用合计都是**可复算**的推导量，且带公式与来源行
        labels = {d["label"]: d for d in (pd.get("derived") or [])}
        self.assertTrue(any("毛利额" in k for k in labels), labels)
        _gm = next(v for k, v in labels.items() if "毛利额" in k)
        self.assertTrue(_gm.get("formula"))
        self.assertTrue(_gm.get("cur") and _gm.get("prev"))
        a = qa.assess_question("net_profit", components=pd.get("components"),
                               period=2024)
        self.assertEqual(a["coverage"], "partial")
        self.assertFalse(a["answered"], "毛利线以下未取得时不判完成")

    def test_profit_components_never_sum_across_groups(self):
        """毛利端只取**一组**切法：各分组是同一笔收入的不同切法，跨组相加会重复计入。"""
        import narrative_evidence as ne
        pd = ne.extract_profit_decomposition([self.doc], periods=[2023, 2024])
        _gm = next((d for d in (pd.get("derived") or []) if "毛利额" in str(d.get("label"))),
                   None)
        self.assertIsNotNone(_gm)
        # 毛利额必须小于表内营业收入合计（跨组相加会得到远大于合计的数）
        self.assertLess(float(_gm["cur"]), 3.0e10, _gm)
        self.assertTrue(any("跨组" in str(b) for b in (pd.get("boundary") or [])),
                        pd.get("boundary"))


class TestContractQuestionSet(unittest.TestCase):
    """问题集随执行契约落盘，并参与指纹（问题集变了 → 旧绑定不再判"当前"）。"""

    def _contract(self, **over):
        from execution_contract import ExecutionContract
        base = dict(company="洋河股份", company_id="002304.SZ", market="cn",
                    periods=(2023, 2024), caliber="合并", as_of="2025-04-30",
                    required_metrics=("revenue", "net_profit", "operating_cashflow"),
                    perspective="equity", subject_type="non_financial")
        base.update(over)
        return ExecutionContract(**base)

    def test_questions_are_part_of_the_wire_and_identity(self):
        c = self._contract()
        wire = c.to_wire()
        qs = wire.get("questions") or []
        self.assertEqual(len(qs), 3, qs)
        self.assertEqual([q["metric"] for q in qs],
                         ["revenue", "net_profit", "operating_cashflow"])
        self.assertEqual([q["question_type"] for q in qs],
                         ["quant_decomposition", "quant_decomposition", "relation"])
        self.assertIn(["revenue", "quant_decomposition"], c.question_identity())

    def test_fingerprint_changes_when_question_set_changes(self):
        a = self._contract()
        b = self._contract(required_metrics=("revenue",))
        self.assertNotEqual(a.fingerprint(), b.fingerprint(),
                            "问题集不同 → 指纹必须不同")
        self.assertNotEqual(a.fingerprint(), self._contract(as_of="2025-05-31").fingerprint())

    def test_wire_roundtrip_keeps_questions(self):
        from execution_contract import ExecutionContract
        c = self._contract()
        back = ExecutionContract.from_wire(c.to_wire())
        self.assertIsNotNone(back)
        self.assertEqual(back.question_identity(), c.question_identity())
        self.assertTrue(back.matches(c.to_wire()))


class TestAssemblyDeterminism(unittest.TestCase):
    """同一输入连续评估两次必须完全一致（装配稳定性的评估侧）。"""

    def test_two_consecutive_assessments_are_identical(self):
        comps = [{"component": "毛利端", "state": "bound", "locator": "api_chunk 5"},
                 {"component": "期间费用", "state": "bound", "locator": "api_chunk 5"},
                 {"component": "税项与非经常性损益", "state": "missing"}]
        items = [{"text": "2024年净利润下降，主要由于毛利率下降。", "locator": "api_chunk 5",
                  "document_period": "2024", "issuer": True}]
        a = qa.assess_question("net_profit", items=items, components=comps, period=2024)
        b = qa.assess_question("net_profit", items=items, components=comps, period=2024)
        self.assertEqual(json.dumps(a, ensure_ascii=False, sort_keys=True),
                         json.dumps(b, ensure_ascii=False, sort_keys=True))


class TestSubjectTypeApplicability(unittest.TestCase):
    """四个固定适用性用例（C2）：类型未确认不得标"已适用"；金融主体只留原始事实。"""

    def test_state_only_declared_counts_as_confirmed(self):
        from facts import subject_type_of, subject_type_state
        for declared, name, code, want_state, want_type in (
                ("non_financial", "洋河股份", "002304.SZ", "confirmed", "non_financial"),
                ("financial", "招商银行", "600036.SH", "confirmed", "financial"),
                ("", "招商银行", "600036.SH", "suggested", "financial"),
                ("", "中国平安", "601318.SH", "unconfirmed", "unknown"),
                ("", "", "601318.SH", "unconfirmed", "unknown"),
                ("", "", "", "unconfirmed", "unknown")):
            stype, src = subject_type_of(declared=declared, company=name, company_id=code)
            self.assertEqual(stype, want_type, (declared, name, code))
            self.assertEqual(subject_type_state(stype, src), want_state,
                             (declared, name, code))

    def test_unconfirmed_type_does_not_pass_corporate_ratios(self):
        from facts import ratio_applies
        self.assertTrue(ratio_applies("net_margin", "non_financial"))
        self.assertFalse(ratio_applies("net_margin", "financial"),
                         "金融机构不套企业口径比率")
        # 类型未确认时数值可算（可核对），但**不得标"已适用"**——这条在装配层断言
        # （见 test_delivery_chain 的四用例：metrics_table.ratio_applicability）


if __name__ == "__main__":
    unittest.main(verbosity=2)
