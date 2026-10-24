"""年份提取的数字边界（regression）：证券代码不得被当成年份。

实机反例（2026-10-22，任务 ui-b23642e5de）：研究伊利股份（600887）的检索里，
`extract_keywords` 用裸 `\\d{4}` 把 600887 切出前 4 位 `6008` 当年份发进查询，
Bing 返回 SKF 深沟球轴承 6008 / Windows 事件日志 6008 等无关结果；
`_query_tokens` 的年份集合同样把 6008 当查询年份，给垃圾结果加分。
"""
import unittest

from adapters.search_quality import _year_tokens, _query_tokens, extract_keywords


GOAL = ("研究伊利股份（600887）2023 与 2024 两个年度的营业收入、归母净利润，"
        "合并报表口径，数据截至 2025-04-30")


class TestYearTokenBoundary(unittest.TestCase):
    def test_stock_code_never_yields_a_year(self):
        self.assertEqual(_year_tokens("600887"), [])
        self.assertEqual(_year_tokens("（600887）"), [])
        self.assertEqual(_year_tokens("代码600887.SH"), [])

    def test_real_years_survive(self):
        self.assertEqual(_year_tokens("2023年"), ["2023"])
        self.assertEqual(_year_tokens(GOAL), ["2023", "2024", "2025"])

    def test_longer_and_shorter_digit_runs(self):
        self.assertEqual(_year_tokens("12345"), [])          # 5 位不是年份
        self.assertEqual(_year_tokens("1234567"), [])        # 7 位不是年份
        self.assertEqual(_year_tokens("营收1157.80亿元"), [])

    def test_extract_keywords_drops_bogus_code_year(self):
        kws = extract_keywords(GOAL)
        self.assertNotIn("6008", kws.split())
        self.assertNotIn("600887"[:4], kws.split())
        self.assertIn("2023", kws)
        self.assertIn("伊利股份", kws)

    def test_query_tokens_year_set_excludes_code_fragment(self):
        _, years, _ = _query_tokens("6008 2023 2024 2025 研究伊利股份")
        self.assertNotIn("6008", years)
        self.assertEqual(years, {"2023", "2024", "2025"})


if __name__ == "__main__":
    unittest.main()
