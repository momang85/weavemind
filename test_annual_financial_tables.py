# -*- coding: utf-8 -*-
"""年报 PDF 财务行抽取：语义准入（A0）的正例与"宁可拒绝"反例（全部离线）。"""

from __future__ import annotations

import unittest

from adapters import annual_financial_tables as aft

_TITLE = "京蓝科技股份有限公司2020年年度报告（更正后）"
_URL = "http://static.cninfo.com.cn/finalpage/2025-09-05/1224639904.PDF"


def _doc(text: str) -> dict:
    return {"title": _TITLE, "url": _URL, "text": text,
            "page_offsets": [(0, 1), (len(text) // 2, 2)]}


class TestNumberParsing(unittest.TestCase):
    def test_variants(self):
        for tok, want in {"1,234.56": 1234.56, "(1,234.56)": -1234.56,
                          "-1,234.56": -1234.56, "１２３４．５６": 1234.56,
                          "0.00": 0.0}.items():
            self.assertAlmostEqual(float(aft.parse_number(tok)), want, places=2, msg=tok)
        for tok in ("—", "-", "不适用", "无", ""):
            self.assertIsNone(aft.parse_number(tok), tok)

    def test_percent_and_date_tokens_are_not_amounts(self):
        self.assertEqual([float(v) for v in aft.numbers_in(
            "995,410,211.62 1,901,408,713.75 -47.65% 2,490,857,777.77")],
            [995410211.62, 1901408713.75, 2490857777.77])
        self.assertEqual(aft.numbers_in("2019 年 2 月 25 日 至 2020 年 12 月 31 日"), [])


class TestLabelSemantics(unittest.TestCase):
    def test_real_rows_are_recognised(self):
        for line, slug in (("营业收入(元) 995,410,211.62 1,901,408,713.75", "revenue"),
                           ("归属于上市公司股东的净利润", "net_profit"),
                           ("经营活动产生的现金流量净额（元） 60,169,476.13 1",
                            "operating_cashflow"),
                           ("资产总计 9,671,022,194.26 12,118,205,650.23", "total_assets")):
            self.assertEqual(aft._label_of(line)[0], slug, line)

    def test_prefix_lookalikes_are_not_that_metric(self):
        """`营业收入扣除金额/扣除后金额` 前缀同"营业收入"，但不是营业收入。"""
        for line in ("营业收入扣除金额(元) 26,765,393.81 39,439,349.17 无",
                     "营业收入扣除后金额（元） 968,644,817.81 1,861,969,364.58 无",
                     "应收账款账龄 1,279,570,429.23", "存货跌价准备 680,900,264.45"):
            self.assertEqual(aft._label_of(line)[0], "", line)

    def test_total_cost_is_not_operating_cost(self):
        """`营业总成本` ≠ `营业成本`：不能用于毛利替代。"""
        self.assertEqual(aft._label_of("营业总成本 1,234.00 1,000.00")[0], "")
        self.assertEqual(aft._label_of("营业成本 800.00 700.00")[0], "operating_cost")


class TestEvidenceGates(unittest.TestCase):
    def _body(self, rows: str, title: str = "1、合并资产负债表",
              unit: str = "单位：元") -> str:
        return f"{title}\n{unit}\n2020 年 2019 年\n{rows}\n"

    def test_positive_with_title_and_unit(self):
        out = aft.extract(_doc(self._body("营业收入(元) 995,410,211.62 1,901,408,713.75")),
                          company="京蓝科技", company_code="000711.SZ",
                          periods=(2019, 2020))
        f = out["facts"][0]
        self.assertEqual(f["caliber"], "合并")
        self.assertEqual(f["caliber_source"], "表名「合并资产负债表」")
        self.assertEqual(f["currency"], "CNY")
        self.assertTrue(f["unit_source"])
        self.assertTrue(f["fact_id"], "接受的事实必须有非空稳定身份")
        self.assertIn("PDF 第", f["locator"])

    def test_parent_company_caliber_is_not_written_as_consolidated(self):
        out = aft.extract(_doc(self._body("应收账款 18,600,000.00 18,600,000.00",
                                          title="2、母公司资产负债表")),
                          company="京蓝科技", company_code="000711.SZ", periods=(2020,))
        self.assertEqual(out["facts"][0]["caliber"], "母公司")

    def test_no_unit_evidence_is_rejected(self):
        out = aft.extract(_doc("1、合并资产负债表\n2020 年 2019 年\n存货 100.00 90.00\n"),
                          company="京蓝科技", company_code="000711.SZ",
                          periods=(2020, 2019))
        self.assertEqual(out["facts"], [])
        self.assertIn("no_unit_evidence", {r["reason"] for r in out["rejected"]})

    def test_unrecognised_table_is_rejected(self):
        """附注表/政策调整表：不取（2020 存货曾被 2020-01-01 政策调整表冒充年末数）。"""
        text = ("2、母公司资产负债表\n单位：元\n2020 年 2019 年\n"
                "存货 73,916.00 861,856.68\n"
                "3、会计政策变更及追溯调整说明\n单位：元\n2020 年 2019 年\n"
                "存货 680,900,264.45 4,354,696,634.50\n")
        out = aft.extract(_doc(text), company="京蓝科技", company_code="000711.SZ",
                          periods=(2020, 2019))
        got = {(f["period"], f["value"], f["caliber"]) for f in out["facts"]
               if f["metric"] == "inventory"}
        self.assertEqual(got, {("2019年", 861856.68, "母公司"),
                               ("2020年", 73916.0, "母公司")},
                         "政策调整表里的存货不得作为年末事实")
        self.assertTrue(any(r["reason"] == "table_unrecognized" for r in out["rejected"]))

    def test_split_number_is_rejected_not_glued(self):
        out = aft.extract(_doc(self._body("应收账款\n1,279,570,42\n9.23 1,966,154,875.23")),
                          company="京蓝科技", company_code="000711.SZ",
                          periods=(2020, 2019))
        self.assertEqual([f for f in out["facts"] if f["metric"] == "accounts_receivable"],
                         [])
        self.assertTrue({"split_number", "column_mismatch"} &
                        {r["reason"] for r in out["rejected"]})

    def test_note_column_is_excluded_only_when_declared(self):
        text = self._body("应收账款 附注 28 100.00 90.00").replace(
            "2020 年 2019 年", "附注 2020 年 2019 年")
        out = aft.extract(_doc(text), company="京蓝科技", company_code="000711.SZ",
                          periods=(2020, 2019))
        vals = sorted(f["value"] for f in out["facts"]
                      if f["metric"] == "accounts_receivable")
        self.assertEqual(vals, [90.0, 100.0], "排除附注号列，取 100/90")
        # 表头没声明附注列时多一个数 → 拒绝，绝不截断
        out2 = aft.extract(_doc(self._body("应收账款 28 100.00 90.00")),
                           company="京蓝科技", company_code="000711.SZ",
                           periods=(2020, 2019))
        self.assertEqual(out2["facts"], [])
        self.assertIn("column_mismatch", {r["reason"] for r in out2["rejected"]})

    def test_entity_must_match_material(self):
        out = aft.extract(_doc(self._body("营业收入(元) 1.00 2.00")),
                          company="贵州茅台", company_code="600519.SH", periods=(2020,))
        self.assertEqual(out["facts"], [])
        self.assertEqual(out["rejected"][0]["reason"], "entity_unverified")

    def test_page_locator_maps_back_to_the_pdf(self):
        text = "1、合并资产负债表\n单位：元\n2020 年 2019 年\n" + "存货 100.00 90.00\n" * 40
        doc = _doc(text)
        out = aft.extract(doc, company="京蓝科技", company_code="000711.SZ", periods=(2020,))
        self.assertIn("PDF 第 1 页", out["facts"][0]["locator"])
        self.assertEqual(aft.page_of(doc, 0), 1)
        self.assertEqual(aft.page_of(doc, len(text) - 1), 2)


class TestDerivation(unittest.TestCase):
    def _f(self, metric, value, **kw):
        base = {"metric": metric, "period": "2020年", "value": value, "unit": "元",
                "currency": "CNY", "caliber": "合并", "entity": "京蓝科技",
                "entity_id": "000711.SZ", "fact_id": f"fact-{metric}"}
        base.update(kw)
        return base

    def test_gross_profit_needs_same_entity_currency_unit_caliber(self):
        ok = aft.derive_gross_profit([self._f("revenue", 100.0),
                                      self._f("operating_cost", 60.0)])
        self.assertEqual(len(ok), 1)
        self.assertAlmostEqual(ok[0]["value"], 40.0)
        self.assertEqual(len(ok[0]["derived_from"]), 2)
        self.assertEqual(ok[0]["formula_version"], "gross_profit_v1")
        for bad in ([self._f("revenue", 100.0),
                     self._f("operating_cost", 60.0, entity_id="000002.SZ")],
                    [self._f("revenue", 100.0),
                     self._f("operating_cost", 60.0, currency="USD")],
                    [self._f("revenue", 100.0),
                     self._f("operating_cost", 60.0, caliber="母公司")],
                    [self._f("revenue", 100.0, fact_id=""),
                     self._f("operating_cost", 60.0)]):
            self.assertEqual(aft.derive_gross_profit(bad), [], bad)

    def test_no_cost_no_gross_profit(self):
        self.assertEqual(aft.derive_gross_profit([self._f("revenue", 100.0)]), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
