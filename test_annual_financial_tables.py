# -*- coding: utf-8 -*-
"""年报 PDF 财务行抽取：语义准入（A0）的正例与"宁可拒绝"反例（全部离线）。"""

from __future__ import annotations

import unittest

from adapters import annual_financial_tables as aft

_TITLE = "京蓝科技股份有限公司2020年年度报告（更正后）"
_URL = "http://static.cninfo.com.cn/finalpage/2025-09-05/1224639904.PDF"


def _doc(text: str, *, title: str = _TITLE) -> dict:
    return {"title": title, "url": _URL, "text": text,
            "page_offsets": [(0, 1), (len(text) // 2, 2)]}


_SANY = "三一重工股份有限公司2024年年度报告"
_YANGHE = "洋河股份2024年年度报告"


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

    # ── 2026-09-29（A2）：三种**书写形态**曾挡住完整标签匹配，都是实机版面 ──

    def test_line_item_prefixes_are_stripped(self):
        """行项目编号与"其中/加/减"标记是**格式**，不是指标名的一部分。

        三一：`一、营业总收入 78,383,379 74,018,936`、`其中：营业成本 七、54 ...`。
        """
        for line, slug in (
            ("一、营业总收入 78,383,379 74,018,936", "total_revenue"),
            ("其中：营业收入 七、54 77,773,391 73,221,725", "revenue"),
            ("减：营业成本 十九、4 11,098,401 6,981,802", "operating_cost"),
            ("1.归属于母公司股东的净利润 6,673,388,602.12 10,015,930,040.27", "net_profit"),
            ("（一）应收账款 1,000.00 900.00", "accounts_receivable"),
        ):
            self.assertEqual(aft._label_of(line)[0], slug, line)

    def test_note_reference_column_is_not_part_of_the_label(self):
        """附注列引用（`七、6`）夹在标签与金额之间：旧逻辑把它当"标签后的数字"截断。"""
        self.assertEqual(aft._label_of("应收账款 七、6 25,516,530 24,164,729")[0],
                         "accounts_receivable")
        self.assertEqual(aft._label_of("存货 七、11 19,947,981 19,767,762")[0], "inventory")
        # 去掉的是引用本身：函数交出的 tail 里只剩金额（直接对原行取数会把 `6` 当金额）
        tail = aft._label_of("应收账款 七、6 25,516,530 24,164,729")[1]
        self.assertEqual([float(v) for v in aft.numbers_in(tail)],
                         [25516530.0, 24164729.0])

    def test_total_revenue_is_a_separate_metric(self):
        """`营业总收入` 与 `营业收入` 口径不同（三一差 0.78%）：同挂一个 slug 会双双作废。"""
        self.assertEqual(aft._label_of("营业总收入 78,383,379 74,018,936")[0],
                         "total_revenue")
        self.assertEqual(aft._label_of("营业收入 77,773,391 73,221,725")[0], "revenue")

    def test_wrapped_label_is_recognised_but_needs_the_next_line(self):
        """折行标签（末尾括号没闭合）认标签；金额必须由**折行形态**的下一行提供。"""
        self.assertEqual(aft._label_of("1.归属于母公司股东的净利润(净亏损")[0], "net_profit")
        out = aft.extract(_doc("1、合并利润表\n单位：千元\n项目 附注 2024年度 2023年度\n"
                               "归属于母公司股东的净利润(净亏损\n"
                               "以“-”号填列) 5,975,451 4,527,451\n", title=_SANY),
                          company="三一重工", company_code="600031.SH", periods=(2024,))
        got = {(f["metric"], f["period"]): f["value"] for f in out["facts"]}
        self.assertEqual(got.get(("net_profit", "2024年")), 5975451000.0)
        self.assertIn("折行", out["facts"][0]["locator"], "折行要在定位里写明")

    def test_wrapped_label_without_the_continuation_line_is_rejected(self):
        """没有折行金额行 → 不是"猜一个数"，而是拒绝。"""
        out = aft.extract(_doc("1、合并利润表\n单位：千元\n项目 附注 2024年度 2023年度\n"
                               "归属于母公司股东的净利润(净亏损\n", title=_SANY),
                          company="三一重工", company_code="600031.SH", periods=(2024,))
        self.assertEqual(out["facts"], [])


class TestSilentErrorGuards(unittest.TestCase):
    """K0-b（09-29 夜验收）：**静默错数**的六类反例——宁可拒绝，不得借/倒/截/跨/推/择。

    每条都先在本轮 HEAD 上复现"错得看起来正常"，再修到拒绝或按列标签映射。
    """

    def test_empty_inventory_does_not_borrow_the_next_account(self):
        """`存货` 空行 + `在建工程 200.00 180.00` → 不得把在建工程当存货（未知行标签=新行）。"""
        out = aft.extract(_doc("1、合并资产负债表\n单位：元\n2020 年 2019 年\n"
                              "存货\n在建工程 200.00 180.00\n"),
                          company="京蓝科技", company_code="000711.SZ", periods=(2019, 2020))
        self.assertEqual([f for f in out["facts"] if f["metric"] == "inventory"], [],
                         f"存货不得借下一科目：{out['facts']}")

    def test_end_start_columns_follow_the_label_order(self):
        """`期初余额 期末余额` → 90 是**上一年**、100 才是本期（旧码固定映射成倒年）。"""
        out = aft.extract(_doc("1、合并资产负债表\n单位：元\n2020 年 12 月 31 日\n"
                              "项目 期初余额 期末余额\n存货 90.00 100.00\n"),
                          company="京蓝科技", company_code="000711.SZ", periods=(2019, 2020))
        got = {(f["period"]): f["value"] for f in out["facts"] if f["metric"] == "inventory"}
        self.assertEqual(got, {"2019年": 90.0, "2020年": 100.0}, got)
        self.assertTrue(all(f["period_kind"] == "stock" for f in out["facts"]))

    def test_full_opening_date_is_not_read_as_year_end(self):
        """`2020年1月1日`（= 上年末年初数）不得被截成"2020年"占年末那一格。"""
        out = aft.extract(_doc("1、合并资产负债表\n单位：元\n"
                              "项目 2020年1月1日 2020年12月31日\n存货 90.00 100.00\n"),
                          company="京蓝科技", company_code="000711.SZ", periods=(2020,))
        got = [(f["period"], f["value"]) for f in out["facts"] if f["metric"] == "inventory"]
        self.assertEqual(got, [("2020年", 100.0)], got)
        self.assertIn("2020年12月31日", out["facts"][0]["period_label"])

    def test_new_table_does_not_borrow_previous_header_or_unit(self):
        """母公司表缺年份/单位：不得跨标题借上一张合并表的年份与"万元"。"""
        text = ("1、合并资产负债表\n单位：万元\n2020 年 2019 年\n"
                "营业收入 1,000.00 900.00\n"
                "2、母公司资产负债表\n应收账款 18,600,000.00 18,600,000.00\n")
        out = aft.extract(_doc(text), company="京蓝科技", company_code="000711.SZ",
                          periods=(2019, 2020))
        got = {(f["metric"], f["period"], f["value"]) for f in out["facts"]}
        self.assertIn(("revenue", "2020年", 10000000.0), got, "合并表那一行照常取")
        self.assertEqual([f for f in out["facts"] if f["caliber"] == "母公司"], [],
                         f"母公司表没有自己的表头就不取：{out['facts']}")
        self.assertTrue({"no_periods", "no_unit_evidence"} &
                        {r["reason"] for r in out["rejected"]}, out["rejected"])

    def test_declared_currency_beats_unit_inference(self):
        """`单位：元 币种：美元` 是**美元**（"元"是美元的基本单位），不得推断成 CNY。"""
        out = aft.extract(_doc("1、合并利润表\n单位：元 币种：美元\n2024 年 2023 年\n"
                              "营业收入 100.00 90.00\n"),
                          company="京蓝科技", company_code="000711.SZ", periods=(2023, 2024))
        f = [x for x in out["facts"] if x["metric"] == "revenue"][0]
        self.assertEqual(f["currency"], "USD")
        self.assertIn("币种", f["currency_source"])
        self.assertEqual(f["unit"], "元")
        self.assertIn("单位", f["unit_source"])

    def test_dash_form_opening_column_is_not_the_current_year_end(self):
        """L0-a/复核 F2：`2024-01-01 / 2023-12-31` 的 90/100 **不得**记成 2024/2023 年末。

        `2024-01-01` 是**期初**时点（= 2023 年末）：按完整日期解析后它与 `2023-12-31`
        落在同一期间而值不同 → 冲突拒绝；绝不能截成"2024 年末 90"这种看起来正常的错数。
        """
        out = aft.extract(_doc("1、合并资产负债表\n单位：元\n"
                               "项目 2024-01-01 2023-12-31\n存货 90.00 100.00\n"),
                          company="京蓝科技", company_code="000711.SZ")
        inv = [f for f in out["facts"] if f["metric"] == "inventory"]
        self.assertEqual(inv, [], f"期初列不得占本年期末格：{inv}")
        self.assertTrue([r for r in out["rejected"] if r["reason"] == "conflicting"],
                        f"冲突必须如实报出：{out['rejected']}")

    def test_dash_form_dates_are_parsed_as_full_periods(self):
        """正例：`2023-12-31 / 2022-12-31` 仍按完整日期分别记两年，并带 kind=stock。"""
        out = aft.extract(_doc("1、合并资产负债表\n单位：元\n"
                               "项目 2023-12-31 2022-12-31\n存货 100.00 90.00\n"),
                          company="京蓝科技", company_code="000711.SZ")
        got = {(f["period"], f["value"], f["period_kind"], f["period_end"])
               for f in out["facts"] if f["metric"] == "inventory"}
        self.assertEqual(got, {("2023年", 100.0, "stock", "2023-12-31"),
                               ("2022年", 90.0, "stock", "2022-12-31")}, got)

    def test_currency_on_its_own_line_beats_the_unit(self):
        """L0-a/复核 F3：`单位：元` 与**下一行** `币种：美元` 是两条独立证据 → USD。

        旧码只在带"单位"的那一行里找币种，找不到就按"元→CNY"推断，于是美元表记成 CNY。
        """
        out = aft.extract(_doc("1、合并资产负债表\n单位：元\n币种：美元\n"
                               "2024 年 2023 年\n存货 90.00 100.00\n"),
                          company="京蓝科技", company_code="000711.SZ", periods=(2023, 2024))
        f = [x for x in out["facts"] if x["metric"] == "inventory"]
        self.assertTrue(f, out["rejected"])
        self.assertEqual({x["currency"] for x in f}, {"USD"}, f)
        self.assertEqual({x["unit"] for x in f}, {"元"}, f)
        self.assertTrue(all("币种" in x["currency_source"] for x in f), f)

    def test_value_equal_but_identity_different_parents_do_not_derive(self):
        """L0-a/复核 F4：收入 100 CNY 与 100 USD **数值相同、身份不同** → 仍是冲突，不派生。

        旧码按**数值**去重（`len(values) > 1`），于是挑中第一条当父、派生出"ok"的毛利 40，
        而它的父收入在数据集里是 conflicting。
        """
        facts = [
            {"metric": "revenue", "period": "2024年", "value": 100.0, "unit": "元",
             "currency": "CNY", "caliber": "合并", "entity_id": "X", "fact_id": "r-cny"},
            {"metric": "revenue", "period": "2024年", "value": 100.0, "unit": "元",
             "currency": "USD", "caliber": "合并", "entity_id": "X", "fact_id": "r-usd"},
            {"metric": "operating_cost", "period": "2024年", "value": 60.0, "unit": "元",
             "currency": "CNY", "caliber": "合并", "entity_id": "X", "fact_id": "c-cny"},
        ]
        rej: list = []
        derived = aft.derive_gross_profit(facts, rej)
        self.assertEqual(derived, [], f"身份不同的父不得派生：{derived}")
        self.assertTrue([r for r in rej if r["reason"] == "derivation_input_conflict"],
                        f"要给出不派生的原因：{rej}")

    def test_conflicting_revenue_does_not_derive_gross_profit(self):
        """收入 100/110 冲突 + 成本 60 → 不得先字典择末派生毛利 50（冲突向派生传播）。"""
        text = ("1、合并利润表\n单位：元\n2024 年 2023 年\n"
                "营业收入 100.00 90.00\n营业成本 60.00 50.00\n"
                "1、合并现金流量表\n单位：元\n2024 年 2023 年\n营业收入 110.00 90.00\n")
        out = aft.extract(_doc(text), company="京蓝科技", company_code="000711.SZ",
                          periods=(2023, 2024))
        self.assertEqual([f for f in out["facts"] if f["metric"] == "gross_profit"], [],
                         f"extract 不派生，派生只在 to_dataset 里做：{out['facts']}")
        rej: list = []
        rows = list(out["facts"]) + aft.derive_gross_profit(out["facts"], rej)
        derived = {r["period"]: r["value"] for r in rows if r["metric"] == "gross_profit"}
        # 2024 收入 100/110 冲突 → **不派生**；2023 两张表都是 90 → 已消歧，照常派生 40
        self.assertNotIn("2024年", derived, f"父冲突不得派生：{derived}")
        self.assertEqual(derived.get("2023年"), 40.0, derived)
        self.assertTrue([r for r in rej if r["reason"] == "derivation_input_conflict"],
                        f"要给「为什么不派生」的原因：{rej}")


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
                         [], "被排版截断的数（1,279,570,42 / 9.23）不得当成折行金额")
        self.assertTrue({"split_number", "column_mismatch", "no_periods"} &
                        {r["reason"] for r in out["rejected"]})

    def test_wrapped_row_two_lines_below_is_accepted(self):
        """京蓝主要会计数据：标签行 → `(元)` → 金额行（隔一行也要认，否则归母净利永远缺）。"""
        text = ("六、主要会计数据和财务指标\n"
                "2020 年 2019 年 本年比上年增减 2018 年\n"
                "归属于上市公司股东的净利润\n(元)\n"
                "-2,354,850,607.11 -1,036,745,832.56 -127.14% 102,535,975.63\n")
        out = aft.extract(_doc(text), company="京蓝科技", company_code="000711.SZ",
                          periods=(2019, 2020))
        got = {(f["metric"], f["period"]): f["value"] for f in out["facts"]}
        self.assertEqual(got.get(("net_profit", "2020年")), -2354850607.11)
        self.assertEqual(got.get(("net_profit", "2019年")), -1036745832.56)

    def test_adjustment_table_is_not_an_annual_statement(self):
        """京蓝反例：`首次执行新收入准则调整年初财务报表` 里的表也叫"合并资产负债表"。

        表头 `2019年12月31日 | 2020年01月01日 | 调整数`：两列都不是年度报表数
        （一是调整前、一是调整后的年初数）。照年度口径记账会让"2020年"出现两个值——
        实机已经发生过（真值 1,279,570,429.23 与年初数 1,966,154,875.23 打架，整项作废）。
        """
        text = ("合并资产负债表\n单位：元\n"
                "项目 2019 年 12 月 31 日 2020 年 01 月 01 日 调整数\n"
                "应收账款 1,966,154,875.23 1,966,154,875.23\n"
                "存货 4,354,696,634.50 680,900,264.45\n")
        out = aft.extract(_doc(text), company="京蓝科技", company_code="000711.SZ",
                          periods=(2019, 2020))
        self.assertEqual(out["facts"], [])
        self.assertTrue(any(r["reason"] == "table_unrecognized" for r in out["rejected"]))

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

    # ── 2026-09-29（A2）：表头形态与"报表内向上找" ──────────────────────

    def test_end_start_header_takes_years_from_the_statement_date(self):
        """洋河合并资产负债表用 `期末余额/期初余额` 列头，年份在报表日期行上。"""
        text = ("1、合并资产负债表\n编制单位：江苏洋河酒厂股份有限公司\n"
                "2024 年12 月 31 日\n单位：元\n项目 期末余额 期初余额\n"
                "应收账款 8,994,904.73 3,528,778.28\n存货 19,732,881,051.73 18,954,235,402.25\n")
        out = aft.extract(_doc(text, title=_YANGHE), company="洋河股份", company_code="002304.SZ",
                          periods=(2023, 2024))
        got = {(f["metric"], f["period"]): f["value"] for f in out["facts"]}
        self.assertEqual(got.get(("accounts_receivable", "2024年")), 8994904.73)
        self.assertEqual(got.get(("accounts_receivable", "2023年")), 3528778.28)
        self.assertEqual(out["facts"][0]["header_source"], "end_start")
        self.assertIn("报表日期行", out["facts"][0]["period_source"])

    def test_end_start_header_without_a_date_is_rejected(self):
        """没有报表日期行 → 期初/期末不能凭空映射成年度（拒绝，不猜）。"""
        text = ("1、合并资产负债表\n单位：元\n项目 期末余额 期初余额\n"
                "应收账款 8,994,904.73 3,528,778.28\n")
        out = aft.extract(_doc(text, title=_YANGHE), company="洋河股份", company_code="002304.SZ",
                          periods=(2023, 2024))
        self.assertEqual(out["facts"], [])
        self.assertIn("no_periods", {r["reason"] for r in out["rejected"]})

    def test_header_far_above_is_found_within_the_same_statement(self):
        """报表跨页时页眉重复、数据行离表头很远（三一利润表：表头 4990、数据行 5022）。"""
        filler = "\n".join(f"利息收入 {i},000.00 {i},500.00" for i in range(1, 20))
        text = ("1、合并利润表\n单位：千元\n项目 附注 2024年度 2023年度\n"
                f"其中：营业收入 77,773,391 73,221,725\n{filler}\n"
                "项目 附注 2024年度 2023年度\n"
                "归属于母公司股东的净利润 5,975,451 4,527,451\n")
        out = aft.extract(_doc(text, title=_SANY), company="三一重工", company_code="600031.SH",
                          periods=(2023, 2024))
        got = {(f["metric"], f["period"]): f["value"] for f in out["facts"]}
        self.assertEqual(got.get(("net_profit", "2024年")), 5975451000.0,
                         "同表内向上找表头（宽松窗口），不是只看上方 14 行")

    def test_header_does_not_cross_into_a_non_statement_table(self):
        """`分季度主要财务指标` 这类表不写年份：不得借用上方别的表头当年度列。"""
        text = ("1、合并资产负债表\n单位：元\n项目 2024年 2023年\n"
                "存货 100.00 90.00\n"
                "八、分季度主要财务指标\n单位：元\n第一季度 第二季度 第三季度 第四季度\n"
                "营业收入 16,254,884,718.38 6,620,864,175.19 4,640,733,548.45 1,359,814,551.54\n")
        out = aft.extract(_doc(text, title=_YANGHE), company="洋河股份", company_code="002304.SZ",
                          periods=(2023, 2024))
        got = {(f["metric"], f["period"]) for f in out["facts"]}
        self.assertNotIn(("revenue", "2024年"), got, "分季度行不得当年度收入")

    def test_policy_change_note_is_not_a_statement_even_if_titled_like_one(self):
        """`合并利润表影响`（会计政策变更说明）标题里有"合并利润表"，仍不是报表。

        反例来源：三一政策变更说明表里的营业成本 1,084,472 千元曾被当成合并利润表的
        营业成本，与真表冲突后把整项删掉（真值 57,216,959 千元反而没了）。
        """
        text = ("1、合并利润表\n单位：千元\n项目 附注 2024年度 2023年度\n"
                "其中：营业成本 七、54 57,216,959 54,051,053\n"
                "执行《企业会计准则解释第18号》，公司对相关报表项目追溯调整列报，影响如下。\n"
                "合并利润表影响\n单位：千元\n项目 2024年度影响金额 2023年度影响金额\n"
                "营业成本 1,084,472 1,116,357\n")
        out = aft.extract(_doc(text, title=_SANY), company="三一重工", company_code="600031.SH",
                          periods=(2023, 2024))
        got = {(f["metric"], f["period"]): f["value"] for f in out["facts"]}
        self.assertEqual(got.get(("operating_cost", "2024年")), 57216959000.0)
        self.assertNotIn(1084472000.0, got.values())

    def test_unit_annotation_is_found_across_a_page_break(self):
        """单位标注只在报表开头出现一次：续页的行走"报表内向上找"也能拿到单位。"""
        filler = "\n".join(f"支付其他与经营活动有关的现金 {i},000.00 {i},500.00"
                           for i in range(1, 20))
        text = ("5、合并现金流量表\n单位：千元\n项目 2024年度 2023年度\n"
                f"{filler}\n"
                "经营活动产生的现金流量净额 14,814,278 5,708,220\n")
        out = aft.extract(_doc(text, title=_SANY), company="三一重工", company_code="600031.SH",
                          periods=(2023, 2024))
        got = {(f["metric"], f["period"]): f["value"] for f in out["facts"]}
        self.assertEqual(got.get(("operating_cashflow", "2024年")), 14814278000.0)

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
