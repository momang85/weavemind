# -*- coding: utf-8 -*-
"""年报 PDF 财务表抽取：形态容忍度与"宁可拒绝"的反例（全部离线）。"""

from __future__ import annotations

import unittest

from adapters import annual_financial_tables as aft


def _doc(text: str, title: str = "某公司2020年年度报告", url: str = "http://x/y.PDF") -> dict:
    return {"title": title, "url": url, "text": text}


class TestNumberParsing(unittest.TestCase):
    def test_variants(self):
        cases = {"1,234.56": 1234.56, "(1,234.56)": -1234.56, "-1,234.56": -1234.56,
                 "１２３４．５６": 1234.56, "—": None, "-": None, "0.00": 0.0}
        for tok, want in cases.items():
            got = aft.parse_number(tok)
            if want is None:
                self.assertIsNone(got, tok)
            else:
                self.assertAlmostEqual(float(got), want, places=2, msg=tok)

    def test_percent_and_date_tokens_are_not_amounts(self):
        """百分比列与日期里的数字都不能当金额（否则整列错位）。"""
        vals = aft.numbers_in("995,410,211.62 1,901,408,713.75 -47.65% 2,490,857,777.77")
        self.assertEqual([float(v) for v in vals],
                         [995410211.62, 1901408713.75, 2490857777.77])
        self.assertEqual(aft.numbers_in("2019 年 2 月 25 日 至 2020 年 12 月 31 日"), [])


class TestTableExtraction(unittest.TestCase):
    def test_year_header_with_pct_growth_column_aligns_by_period(self):
        """真实形态：`2020 年 2019 年 本年比上年增减 2018 年` + 行内带百分比列。

        百分比列若被当金额，2018 会拿到 -47.65——错位的数看不出来，所以必须排除。
        """
        text = ("六、主要会计数据和财务指标\n"
                "2020 年 2019 年 本年比上年增减 2018 年\n"
                "营业收入（元） 995,410,211.62 1,901,408,713.75 -47.65% 2,490,857,777.77\n"
                "归属于上市公司股东的净利润\n（元）\n"
                "-2,399,698,095.52 -1,036,745,832.56 -131.46% 102,535,975.63\n")
        out = aft.extract(_doc(text), company="京蓝科技", company_code="000711.SZ",
                          periods=(2019, 2020))
        got = {(f["metric"], f["period"]): f["value"] for f in out["facts"]}
        self.assertAlmostEqual(got[("revenue", "2020年")], 995410211.62, places=2)
        self.assertAlmostEqual(got[("revenue", "2019年")], 1901408713.75, places=2)
        self.assertAlmostEqual(got[("net_profit", "2020年")], -2399698095.52, places=2)
        self.assertNotIn(("revenue", "2018年"), got, "契约外期间不入账")

    def test_unit_annotation_is_converted_explicitly(self):
        text = ("单位：万元\n2020 年 2019 年\n"
                "营业收入（万元） 99,541.02 190,140.87\n")
        out = aft.extract(_doc(text), company="X", company_code="000001.SZ", periods=(2020,))
        got = {f["period"]: f["value"] for f in out["facts"] if f["metric"] == "revenue"}
        self.assertAlmostEqual(got["2020年"], 995410200.0, places=0)

    def test_split_number_is_rejected_not_glued(self):
        """PDF 把 `1,279,570,429.23` 断成 `1,279,570,42` + `9.23`：拼起来是错数，必须拒绝。"""
        text = ("2020 年 2019 年\n应收账款\n1,279,570,42\n9.23 1,966,154,875.23\n")
        out = aft.extract(_doc(text), company="X", company_code="000001.SZ", periods=(2020, 2019))
        self.assertEqual([f for f in out["facts"] if f["metric"] == "accounts_receivable"], [])
        self.assertIn("split_number", {r["reason"] for r in out["rejected"]})

    def test_same_table_conflict_drops_only_that_table_item(self):
        text = ("2020 年 2019 年\n营业收入（元） 100.00 90.00\n"
                "营业收入（元） 200.00 90.00\n")
        out = aft.extract(_doc(text), company="X", company_code="000001.SZ", periods=(2020, 2019))
        got = {(f["period"], f["value"]) for f in out["facts"] if f["metric"] == "revenue"}
        self.assertEqual(got, {("2019年", 90.0)},
                         "冲突的那个期间（2020）整体不入账；一致的期间（2019）照常")
        self.assertTrue(any(r["reason"] == "conflicting" for r in out["rejected"]))

    def test_cross_table_disagreement_keeps_priority_and_records_it(self):
        """跨表分歧：按表优先级取一个，并把分歧**记下来**（不静默择大/取平均）。"""
        text = ("1、合并资产负债表\n2020 年 2019 年\n应付账款 1,743,811,151.80 1,988,429,964.49\n"
                "2、母公司资产负债表\n2020 年 2019 年\n应付账款 75,715,552.98 37,447,657.99\n")
        out = aft.extract(_doc(text), company="X", company_code="000001.SZ", periods=(2020, 2019))
        got = {f["period"]: f["value"] for f in out["facts"]
               if f["metric"] == "accounts_payable"}
        self.assertAlmostEqual(got["2020年"], 1743811151.80, places=2, msg="合并表优先")
        self.assertTrue(out["cross_table_conflicts"], "分歧必须可见")
        self.assertEqual(out["cross_table_conflicts"][0]["kept"], "合并资产负债表")

    def test_missing_header_is_rejected(self):
        text = "应收账款 1,279,570,429.23 1,966,154,875.23\n"
        out = aft.extract(_doc(text), company="X", company_code="000001.SZ", periods=(2020, 2019))
        self.assertEqual(out["facts"], [])
        self.assertEqual({r["reason"] for r in out["rejected"]}, {"no_periods"})

    def test_anchor_rule_is_off_by_default(self):
        """期末/期初式表头默认**不锚定**：真实报告上它会误配附注表（差 ~2680 倍）。"""
        text = ("1、合并资产负债表\n单位：元\n项目 期末余额 期初余额\n"
                "应付账款 1,743,811,151.80 1,988,429,964.49\n")
        off = aft.extract(_doc(text), company="X", company_code="000001.SZ",
                          periods=(2020, 2019), anchor_year=2020)
        self.assertEqual(off["facts"], [], "默认关闭：宁可少一个数，不可错一个数")
        on = aft.extract(_doc(text), company="X", company_code="000001.SZ",
                         periods=(2020, 2019), anchor_year=2020, allow_anchor=True)
        self.assertTrue(on["facts"], "显式打开时才用锚定规则")
        self.assertEqual(on["facts"][0]["period_basis"], "closing_opening_anchor")


class TestDerivation(unittest.TestCase):
    def test_gross_profit_is_a_declared_formula_with_inputs(self):
        rows = [{"metric": "revenue", "period": "2020年", "value": 100.0, "unit": "元",
                 "entity": "X", "entity_id": "000001.SZ", "caliber": "合并"},
                {"metric": "operating_cost", "period": "2020年", "value": 60.0,
                 "unit": "元", "entity": "X", "entity_id": "000001.SZ", "caliber": "合并"}]
        gp = aft.derive_gross_profit(rows)
        self.assertEqual(len(gp), 1)
        self.assertAlmostEqual(gp[0]["value"], 40.0)
        self.assertEqual(len(gp[0]["derived_from"]), 2, "算式派生必须带输入 fact_id")
        self.assertIn("-", gp[0]["formula"])

    def test_no_cost_no_gross_profit(self):
        rows = [{"metric": "revenue", "period": "2020年", "value": 100.0, "unit": "元",
                 "entity": "X", "entity_id": "000001.SZ", "caliber": "合并"}]
        self.assertEqual(aft.derive_gross_profit(rows), [], "缺营业成本就不给毛利")


if __name__ == "__main__":
    unittest.main(verbosity=1)
