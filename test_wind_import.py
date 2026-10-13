# -*- coding: utf-8 -*-
"""离线 Wind 导入契约：真实中文宽表、身份缺口、行情口径与来源指纹。"""
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from adapters.wind_import import preview_file


class WindImportTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent
        self.tmp = tempfile.TemporaryDirectory(prefix=".wind_import_test_", dir=root)
        # 清理目标由本测试创建，并验证保持在工作区内。
        self.assertEqual(Path(self.tmp.name).resolve().parent, root)
        self.addCleanup(self.tmp.cleanup)

    def financial_row(self, **changes):
        row = {"证券代码": "002304.SZ", "证券简称": "洋河股份", "报告期": "2024-12-31",
               "披露日期": "2025-04-30", "单位": "元", "币种": "人民币",
               "报表口径": "合并", "报告类型": "年报", "累计/单季类型": "累计",
               "下载日期": "2026-10-08", "营业收入": "28,876,296,993.56",
               "营业成本": "7,751,218,356.66", "归母净利润": "0",
               "净利润": "5,000,000,000", "经营活动产生的现金流量净额": "1,500,000,000"}
        row.update(changes)
        return row

    def write_json(self, rows, name="export.json", **payload):
        path = Path(self.tmp.name) / name
        value = {"rows": rows, **payload} if payload else rows
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def write_csv(self, rows):
        path = Path(self.tmp.name) / "export.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def test_chinese_wide_table_two_companies_and_engine_contract(self):
        rows = [self.financial_row(), self.financial_row(**{"证券代码": "600031.SH", "证券简称": "三一重工",
                "单位": "千元", "营业收入": "77,773,391", "营业成本": "57,239,000"})]
        path = self.write_csv(rows)
        result = preview_file(path, kind="financial")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["metadata"]["instruments"], ["002304.SZ", "600031.SH"])
        self.assertEqual(result["raw_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        by_code = {code: {r["metric"]: r for r in result["records"] if r["entity_id"] == code}
                   for code in result["metadata"]["instruments"]}
        y = by_code["002304.SZ"]
        self.assertEqual(y["net_profit"]["value"], 0)
        self.assertEqual(y["net_profit_consolidated"]["value"], 5e9)
        self.assertAlmostEqual(y["gross_profit"]["value"], 21125078636.90)
        self.assertEqual(y["gross_profit"]["derived_from"], [y["revenue"]["fact_id"], y["operating_cost"]["fact_id"]])
        self.assertEqual(y["revenue"]["period"], "2024年")
        self.assertEqual(y["revenue"]["period_start"], "2024-01-01")
        self.assertEqual(y["revenue"]["available_at"], "2025-04-30")
        self.assertEqual(by_code["600031.SH"]["revenue"]["value"], 77773391)
        self.assertEqual(by_code["600031.SH"]["revenue"]["unit_scale"], 1000)
        from financial_analysis.dataset import freeze
        dataset = freeze(list(y.values()), entity_id="002304.SZ", entity="洋河股份")
        self.assertEqual(dataset.manifest.periods, ("2024年",))
        self.assertEqual(dataset.manifest.usable, 6)

    def test_missing_disclosure_caliber_unit_are_not_guessed(self):
        row = self.financial_row()
        for field in ("披露日期", "报表口径", "单位"):
            row.pop(field)
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertEqual(result["status"], "partial")
        revenue = next(r for r in result["records"] if r["metric"] == "revenue")
        self.assertEqual(revenue["disclosed_at"], "")
        self.assertEqual(revenue["available_at"], "")
        self.assertEqual(revenue["caliber"], "")
        self.assertEqual(revenue["unit"], "")
        self.assertFalse(revenue["analysis_eligible"])
        self.assertNotIn("gross_profit", {r["metric"] for r in result["records"]})

    def test_restatement_version_and_school_license_remain_unverified(self):
        row = self.financial_row(**{"重述版本": "2026更正", "版本可用时间": "2026-09-20"})
        result = preview_file(self.write_json([row], metadata={"license": "机构授权", "authorization_verified": True}), kind="financial")
        self.assertEqual(result["records"][0]["version_available_at"], "2026-09-20")
        self.assertEqual(result["records"][0]["restatement"], "2026更正")
        self.assertEqual(result["metadata"]["license_claim"], "机构授权")
        self.assertEqual(result["metadata"]["license"], "unknown")
        self.assertFalse(result["metadata"]["export_allowed"])
        self.assertEqual(result["metadata"]["human_review"], "unreviewed")

    def test_explicit_mapping_and_unknown_columns(self):
        row = self.financial_row(**{"Wind自定义资产": "20,000", "资料备注": "待核实"})
        result = preview_file(self.write_json([row]), kind="financial", field_map={"Wind自定义资产": "total_assets"})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["metadata"]["unknown_columns"], ["资料备注"])
        total = next(r for r in result["records"] if r["metric"] == "total_assets")
        self.assertEqual(total["value"], 20000)
        self.assertEqual(total["period_kind"], "stock")
        self.assertEqual(total["period_start"], "")
        self.assertEqual(total["sidecar"]["unmapped_fields"]["资料备注"], "待核实")
        bad = preview_file(self.write_json([row]), kind="financial", field_map={"资料备注": "guessed_metric"})
        self.assertEqual(bad["status"], "unavailable")

    def test_conflicting_aliases_do_not_choose_first(self):
        row = self.financial_row(**{"公司代码": "600031.SH", "revenue": "100"})
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertEqual(result["status"], "partial")
        revenue = next(r for r in result["records"] if r["metric"] == "revenue")
        self.assertIsNone(revenue["value"])
        self.assertEqual(revenue["entity_id"], "")
        self.assertFalse(any(r["analysis_eligible"] for r in result["records"]))
        self.assertEqual({i["field"] for i in result["issues"] if i["code"] == "conflicting_columns"}, {"code", "revenue"})

    def test_equivalent_aliases_are_accepted(self):
        row = self.financial_row(**{"公司代码": "002304.sz", "revenue": 28876296993.56})
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertEqual(result["status"], "ready")

    def test_bad_numbers_are_not_zero(self):
        row = self.financial_row(**{"营业收入": True, "营业成本": "NaN", "净利润": "inf",
                                   "归母净利润": "--", "经营活动产生的现金流量净额": "1e9999"})
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(all(r["value"] is None for r in result["records"]))
        row["归母净利润"] = 0
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(next(r for r in result["records"] if r["metric"] == "net_profit")["value"], 0)

    def test_dates_must_be_real_and_codes_need_market(self):
        row = self.financial_row(**{"披露日期": "2025-02-30", "证券代码": "002304"})
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(i["code"] == "invalid_date" for i in result["issues"]))
        self.assertFalse(any(r["analysis_eligible"] for r in result["records"]))

    def test_half_year_and_single_quarter_are_not_annual(self):
        half = self.financial_row(**{"报告期": "2024/6/30", "报告类型": "中报"})
        quarter = self.financial_row(**{"报告期": "20240630", "报告类型": "季报", "累计/单季类型": "单季"})
        result = preview_file(self.write_json([half, quarter]), kind="financial")
        self.assertEqual(result["status"], "ready")
        revenue = [r for r in result["records"] if r["metric"] == "revenue"]
        self.assertEqual(revenue[0]["period_type"], "half_year")
        self.assertEqual(revenue[0]["period_start"], "2024-01-01")
        self.assertEqual(revenue[1]["period_start"], "2024-04-01")
        self.assertEqual(revenue[1]["period_type"], "quarterly")
        self.assertFalse(any(r["period"] == "2024年" for r in revenue))

    def test_explicit_annual_type_and_year_end_define_full_span(self):
        for annual_type in ("年度", "年报", "annual"):
            with self.subTest(annual_type=annual_type):
                row = self.financial_row(**{"报告类型": annual_type})
                row.pop("累计/单季类型")
                result = preview_file(self.write_json([row]), kind="financial")
                revenue = next(r for r in result["records"] if r["metric"] == "revenue")
                self.assertEqual(result["status"], "ready")
                self.assertTrue(revenue["analysis_eligible"])
                self.assertEqual(revenue["period_start"], "2024-01-01")
                self.assertEqual(revenue["period_end"], "2024-12-31")
                self.assertEqual(revenue["period_basis"], "accumulated")
        quarterly = self.financial_row(**{"报告类型": "季报"})
        quarterly.pop("累计/单季类型")
        result = preview_file(self.write_json([quarterly]), kind="financial")
        revenue = next(r for r in result["records"] if r["metric"] == "revenue")
        self.assertFalse(revenue["analysis_eligible"])
        self.assertEqual(revenue["period_start"], "")

    def test_mismatched_period_labels_are_ineligible(self):
        row = self.financial_row(**{"报告期": "2024年", "期间截止日": "2023-12-31"})
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertFalse(any(r["analysis_eligible"] for r in result["records"]))
        self.assertTrue(any(i["code"] == "period_conflict" for i in result["issues"]))

    def market_row(self, **changes):
        row = {"证券代码": "002304.SZ", "交易日期": "2026-09-30", "开盘价": 88,
               "最高价": 91, "最低价": 87, "收盘价": 90, "成交量": 1000,
               "成交额": 90000, "复权因子": 3.2, "复权口径": "未复权", "单位": "元",
               "币种": "CNY", "成交量单位": "股", "成交额单位": "元",
               "可用时间": "2026-09-30T15:30:00+08:00", "停牌": False}
        row.update(changes)
        return row

    def test_market_prices_and_adjustment_are_not_recomputed(self):
        rows = [self.market_row(), self.market_row(**{"复权口径": "前复权", "收盘价": 88})]
        result = preview_file(self.write_json(rows), kind="market")
        self.assertEqual(result["status"], "ready")
        self.assertEqual([r["close"] for r in result["records"]], [90, 88])
        self.assertEqual([r["adj_factor"] for r in result["records"]], [3.2, 3.2])
        self.assertEqual([r["adj_basis"] for r in result["records"]], ["未复权", "前复权"])
        self.assertFalse(result["records"][0]["suspended"])
        from adapters.market_history import normalize
        normalized, issues = normalize(result["records"], source="Wind", license="unknown")
        self.assertEqual(issues, [])
        self.assertEqual(normalized[0]["close"], 90)
        self.assertEqual(normalized[0]["code"], "002304.SZ")

    def test_market_missing_availability_and_bad_ohlc_are_partial(self):
        missing = self.market_row()
        missing.pop("可用时间")
        invalid = self.market_row(**{"最高价": 80, "成交量": -1})
        result = preview_file(self.write_json([missing, invalid]), kind="market")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["records"][0]["available_at"], "")
        self.assertFalse(any(r["analysis_eligible"] for r in result["records"]))
        self.assertTrue(any(i["code"] == "invalid_ohlc" for i in result["issues"]))

    def test_hash_is_raw_bytes_and_preview_has_no_output(self):
        path = self.write_json([self.financial_row()])
        before = set(Path(self.tmp.name).iterdir())
        first = preview_file(path, kind="financial")
        second = preview_file(path, kind="financial")
        self.assertEqual(first, second)
        self.assertEqual(before, set(Path(self.tmp.name).iterdir()))
        previous_id = first["records"][0]["fact_id"]
        self.write_json([self.financial_row(**{"营业收入": "300"})])
        changed = preview_file(path, kind="financial")
        self.assertNotEqual(first["raw_sha256"], changed["raw_sha256"])
        self.assertEqual(previous_id, changed["records"][0]["fact_id"])

    def test_duplicate_headers_and_row_width_are_reported(self):
        path = Path(self.tmp.name) / "bad.csv"
        path.write_text("证券代码,证券代码,营业收入\n002304.SZ,600031.SH,100\n", encoding="utf-8")
        result = preview_file(path, kind="financial")
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(any(i["code"] == "duplicate_header" for i in result["issues"]))
        path.write_text("code,date,close\n002304.SZ,2024-12-31\n", encoding="utf-8")
        result = preview_file(path, kind="market")
        self.assertTrue(any(i["code"] == "row_width" for i in result["issues"]))

    def test_explicit_encoding_and_metadata_defaults(self):
        path = Path(self.tmp.name) / "gb.csv"
        path.write_bytes("证券代码,报告期,营业收入\n002304.SZ,2024年,100\n".encode("gb18030"))
        missing = preview_file(path, kind="financial")
        self.assertEqual(missing["status"], "unavailable")
        result = preview_file(path, kind="financial", metadata={"encoding": "gb18030", "unit": "万元",
            "currency": "CNY", "caliber": "合并", "period_type": "annual", "period_basis": "accumulated",
            "disclosed_at": "2025-04-30", "retrieved_at": "2026-10-08"})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["records"][0]["unit"], "万元")
        self.assertEqual(result["records"][0]["value"], 100)

    def test_earlier_available_date_is_not_used(self):
        row = self.financial_row(**{"可用时间": "2025-03-31"})
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertEqual(result["records"][0]["available_at"], "")
        self.assertFalse(result["records"][0]["analysis_eligible"])

    def test_aware_dates_are_compared_in_actual_time(self):
        row = self.financial_row(**{"披露日期": "2025-04-30T08:30:00+08:00",
                                   "可用时间": "2025-04-30T00:15:00Z"})
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertFalse(result["records"][0]["analysis_eligible"])
        row["可用时间"] = "2025-04-30T00:45:00Z"
        result = preview_file(self.write_json([row]), kind="financial")
        self.assertTrue(result["records"][0]["analysis_eligible"])

    def test_declared_gross_profit_is_never_replaced(self):
        row = self.financial_row(**{"毛利润": "--"})
        result = preview_file(self.write_json([row]), kind="financial")
        gross = [r for r in result["records"] if r["metric"] == "gross_profit"]
        self.assertEqual(len(gross), 1)
        self.assertIsNone(gross[0]["value"])
        self.assertNotIn("derived_from", gross[0])

    def test_rd_investment_is_not_automatically_expensed(self):
        result = preview_file(self.write_json([self.financial_row(**{"研发投入": "100", "研发费用": "70"})]), kind="financial")
        expense = next(r for r in result["records"] if r["metric"] == "rd_expense")
        self.assertEqual(expense["value"], 70)
        self.assertEqual(result["metadata"]["unknown_columns"], ["研发投入"])
        self.assertEqual(expense["sidecar"]["unmapped_fields"]["研发投入"], "100")

    def test_derived_overflow_and_zero_close_are_not_ready(self):
        result = preview_file(self.write_json([self.financial_row(**{"营业收入": "1e308", "营业成本": "-1e308"})]), kind="financial")
        gross = next(r for r in result["records"] if r["metric"] == "gross_profit")
        self.assertIsNone(gross["value"])
        self.assertFalse(gross["analysis_eligible"])
        result = preview_file(self.write_json([self.market_row(**{"收盘价": 0})]), kind="market")
        self.assertEqual(result["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
