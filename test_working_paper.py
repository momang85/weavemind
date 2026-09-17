# -*- coding: utf-8 -*-
"""S1-3 退出关卡：一公司两年度 × 三核心指标 × 可重算底稿。

基准输入**冻结**在本文件（合成夹具，非现实公司事实），断言按架构指令的退出关卡：
- 六个组合齐备、同比可从底稿重算（数值自己算，不采信报告）；
- 指定缺失项如实缺失（记缺口，不推断）；
- **错主体 / 错期 / 错币种 / 累计与单季混用不得被验证通过**；
- 缺证据（无来源位置）不得算已核验；
- **正确输入不得因无关的同值记录失败**（判定只看必需组合自身的行）。
"""

from __future__ import annotations

import csv
import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import facts as F  # noqa: E402
import working_paper as W  # noqa: E402

# ── 冻结基准输入（合成；两年度 × 三核心指标）────────────────
FIXTURE_COMPANY = "示例制造股份有限公司"
FIXTURE_CODE = "000001.SZ"
BASELINE_PAYLOAD = {
    "financials": [
        {"year": 2023, "report_type": "年报", "caliber": "合并",
         "revenue": 1200.0, "net_profit": 150.0, "operating_cashflow": 210.0},
        {"year": 2024, "report_type": "年报", "caliber": "合并",
         "revenue": 1380.0, "net_profit": 174.0, "operating_cashflow": 231.0},
    ],
    "metadata": {"source": "eastmoney", "company": FIXTURE_COMPANY,
                 "code": FIXTURE_CODE, "currency": "CNY", "unit": "亿元"},
    "raw": {"url": "https://example.invalid/financials/000001.json",
            "text": '{"data": [{"TOTALOPERATEREVE": 138000000000}]}'},
}


def _request(**over) -> F.ResearchRequest:
    req = F.parse_research_request(
        "研究示例制造股份有限公司 2023 与 2024 两个年度的营业收入、归母净利润、"
        "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
        company=FIXTURE_COMPANY, company_id=FIXTURE_CODE, market="cn",
        caliber="合并", as_of="2025-04-30")
    for k, v in over.items():
        setattr(req, k, v)
    req.gaps = [g for g in req.gaps]
    return req


def _paper(payload=None, **over) -> W.WorkingPaper:
    data = payload if payload is not None else BASELINE_PAYLOAD
    return W.build_working_paper(F.facts_from_financials(data), _request(**over))


class TestBaselineClosesLoop(unittest.TestCase):
    """基准输入：闭环成立——六个组合齐备、同比可重算、无缺口无问题。"""

    def setUp(self):
        self.paper = _paper()

    def test_six_combos_present(self):
        # 材料里标记"已核实"才无问题？不——基准输入也应保持 unverified（结构化源≠已核实），
        # 但那不是"问题"；这里断言的是组合齐备与无缺口。
        self.assertEqual(self.paper.completeness["present"], 6,
                         self.paper.completeness)
        self.assertEqual(self.paper.completeness["missing"], [])
        self.assertEqual([g for g in self.paper.gaps if g["kind"] == "fact"], [])

    def test_yoy_recomputable_from_paper(self):
        yoy = {d["metric"]: d for d in self.paper.derived}
        self.assertIn("revenue_yoy", yoy)
        d = yoy["revenue_yoy"]
        # 自己算一遍： (1380 - 1200) / 1200 * 100 = 15.0
        self.assertAlmostEqual(d["value"], 15.0, places=2)
        self.assertIn("1380", d["formula"])
        self.assertEqual(len(d["derived_from"]), 2, "同比必须带两个输入 fact_id")
        self.assertEqual(set(d["metric"] for d in self.paper.derived),
                         {"revenue_yoy", "net_profit_yoy", "operating_cashflow_yoy"})

    def test_derived_values_match_hand_computation(self):
        want = {"revenue_yoy": 15.0,
                "net_profit_yoy": (174.0 - 150.0) / 150.0 * 100,
                "operating_cashflow_yoy": (231.0 - 210.0) / 210.0 * 100}
        for d in self.paper.derived:
            self.assertAlmostEqual(d["value"], round(want[d["metric"]], 2), places=2,
                                   msg=f"{d['metric']} 可从底稿重算")

    def test_rows_carry_source_position_and_currency(self):
        for r in self.paper.rows:
            self.assertEqual(r["currency"], "CNY")
            self.assertEqual(r["unit"], "亿元")
            self.assertTrue(r["source_url"])
            self.assertTrue(r["source_locator"], "每条事实要有来源位置")


class TestMissingStaysMissing(unittest.TestCase):
    def test_missing_metric_is_reported_not_inferred(self):
        payload = {"financials": [
            {"year": 2023, "report_type": "年报", "caliber": "合并",
             "revenue": 1200.0, "net_profit": 150.0},
            {"year": 2024, "report_type": "年报", "caliber": "合并",
             "revenue": 1380.0, "net_profit": 174.0},
        ], "metadata": BASELINE_PAYLOAD["metadata"], "raw": BASELINE_PAYLOAD["raw"]}
        paper = _paper(payload)
        self.assertFalse(paper.ok)
        missing = [g for g in paper.gaps if g["kind"] == "fact"]
        self.assertEqual(len(missing), 2, [g["detail"] for g in missing])
        self.assertTrue(all("经营现金流" in g["detail"] or "经营活动现金流" in g["detail"]
                            for g in missing), [g["detail"] for g in missing])
        self.assertFalse(any(d["metric"].startswith("operating_cashflow")
                             for d in paper.derived),
                         "缺一期就不许编同比")


class TestNeverVerifiedNegatives(unittest.TestCase):
    """错主体/错期/错币种/累计混用/无来源 —— 一律不得算已核验。"""

    def _with_row(self, **row_over) -> W.WorkingPaper:
        rows = [dict(r) for r in BASELINE_PAYLOAD["financials"]]
        rows[1].update(row_over)
        return _paper({"financials": rows,
                       "metadata": BASELINE_PAYLOAD["metadata"],
                       "raw": BASELINE_PAYLOAD["raw"]})

    def test_wrong_subject_fails(self):
        rows = [dict(r) for r in BASELINE_PAYLOAD["financials"]]
        payload = {"financials": rows,
                   "metadata": dict(BASELINE_PAYLOAD["metadata"],
                                    company="另一家公司", code="000002.SZ"),
                   "raw": BASELINE_PAYLOAD["raw"]}
        paper = _paper(payload)
        kinds = {p.kind for p in paper.problems}
        self.assertIn(W.PROBLEM_SUBJECT, kinds, [p.detail for p in paper.problems])
        self.assertFalse(paper.ok)

    def test_mixed_currency_across_years_fails(self):
        """两期币种不同（行级声明）→ 不得直接算同比，也不得算已核验。"""
        rows = [dict(r) for r in BASELINE_PAYLOAD["financials"]]
        rows[1].update({"currency": "USD", "unit": "亿美元"})
        paper = _paper({"financials": rows,
                        "metadata": BASELINE_PAYLOAD["metadata"],
                        "raw": BASELINE_PAYLOAD["raw"]})
        kinds = {p.kind for p in paper.problems}
        self.assertIn(W.PROBLEM_CURRENCY, kinds, [p.detail for p in paper.problems])
        self.assertFalse(any(d["metric"] == "revenue_yoy" for d in paper.derived),
                         "币种不一致时不得给出同比")
        self.assertFalse(paper.ok)

    def test_unknown_unit_fails(self):
        """来源没声明单位 → 不得算已核验（口径不明）。"""
        payload = {"financials": [{"year": 2023, "report_type": "年报", "caliber": "合并",
                                   "revenue": 1200.0},
                                  {"year": 2024, "report_type": "年报", "caliber": "合并",
                                   "revenue": 1380.0}],
                   "metadata": {"company": FIXTURE_COMPANY, "code": FIXTURE_CODE,
                                "currency": "CNY"},
                   "raw": BASELINE_PAYLOAD["raw"]}
        paper = _paper(payload)
        self.assertIn(W.PROBLEM_CURRENCY, {p.kind for p in paper.problems},
                      [p.detail for p in paper.problems])

    def test_extra_quarterly_row_is_not_used_in_yoy(self):
        """同期多一条三季报（累计口径）时：它不参与年报同比，也不影响判定。"""
        rows = [dict(r) for r in BASELINE_PAYLOAD["financials"]]
        rows.append({"year": 2024, "report_type": "三季报", "caliber": "合并",
                     "revenue": 1000.0, "net_profit": 120.0})
        paper = _paper({"financials": rows,
                        "metadata": BASELINE_PAYLOAD["metadata"],
                        "raw": BASELINE_PAYLOAD["raw"]})
        self.assertFalse(any("三季报" in str(d["period"]) for d in paper.derived),
                         "季报不得混进年报同比")
        rev = [d for d in paper.derived if d["metric"] == "revenue_yoy"][0]
        inputs = set(rev.get("derived_from") or [])
        quarterly = [r["fact_id"] for r in paper.rows
                     if "三季报" in str(r.get("period"))]
        self.assertTrue(quarterly, "夹具里应有三季报那一行")
        self.assertFalse(inputs & set(quarterly),
                         "同比的输入必须是年报行，不能是季报行")

    def test_quarterly_only_cannot_satisfy_annual_requirement(self):
        """某年只有三季报、没有年报 → 不得当年度口径用（累计 vs 单季不可比）。"""
        rows = [r for r in BASELINE_PAYLOAD["financials"]
                if not (r["year"] == 2024)]           # 拿掉 2024 年报
        rows.append({"year": 2024, "report_type": "三季报", "caliber": "合并",
                     "revenue": 1000.0, "net_profit": 120.0,
                     "operating_cashflow": 180.0})
        paper = _paper({"financials": rows,
                        "metadata": BASELINE_PAYLOAD["metadata"],
                        "raw": BASELINE_PAYLOAD["raw"]})
        kinds = {p.kind for p in paper.problems}
        self.assertIn(W.PROBLEM_CUMULATIVE, kinds, [p.detail for p in paper.problems])
        self.assertFalse(paper.ok, "只有季报时不得算目标达成")
        self.assertFalse(any(str(d["period"]).startswith("2024")
                             for d in paper.derived),
                         "缺 2024 年报时不得算 2024 同比")

    def test_missing_source_url_is_flagged(self):
        paper = _paper({"financials": [{"year": 2024, "report_type": "年报",
                                        "caliber": "合并", "revenue": 1380.0}],
                        "metadata": BASELINE_PAYLOAD["metadata"], "raw": {}})
        self.assertIn(W.PROBLEM_UNVERIFIED, {p.kind for p in paper.problems})


class TestIrrelevantSameValueDoesNotBreakCorrectInput(unittest.TestCase):
    """正确输入不得因无关的同值记录失败（判定只看必需组合自身的行）。"""

    def test_unrelated_same_value_record_is_ignored(self):
        payload = dict(BASELINE_PAYLOAD)
        payload["raw"] = dict(BASELINE_PAYLOAD["raw"])
        # 在原文里塞一条**另一家公司**的同值记录（不影响结构化事实）
        payload["raw"]["text"] = (
            '{"data": [{"TOTALOPERATEREVE": 138000000000}],'
            ' "note": "另一家公司2024年营业收入 1380 亿元"}')
        paper = _paper(payload)
        self.assertEqual(paper.problems, [], [p.detail for p in paper.problems])
        self.assertTrue(paper.completeness["present"] == 6)


class TestPaperExport(unittest.TestCase):
    def test_csv_has_three_sections_and_is_parseable(self):
        text = W.paper_csv(_paper())
        rows = list(csv.reader(io.StringIO(text)))
        heads = [r[0] for r in rows if r]
        self.assertIn("# 明细（可重算底稿）", heads)
        self.assertIn("# 派生值（公式 + 输入）", heads)
        self.assertIn("# 缺口与问题", heads)
        body = [r for r in rows if r and r[0].startswith("fact-")]
        self.assertGreaterEqual(len(body), 6 + 3, "明细 6 行 + 同比 3 行")
        self.assertTrue(any("公式" in r[0] or "formula" in r[0] for r in rows if r),
                        "派生段应带公式列")


if __name__ == "__main__":
    unittest.main(verbosity=2)
