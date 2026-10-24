"""P95 延迟分位数（最近秩法）的回归测试。

来源（2026-10-22 指标看板实机复验）：页面出现「平均耗时」与「P95」**精确同值**
（1274.4 秒）。定位到 `metrics_collector.py` 的 `int(len(latencies) * 0.95) - 1`：
该下标只有在样本数为 20 的倍数时才等于最近秩 `ceil(n*0.95)-1`，其余情况**低一个秩**，
n=2 时会把最小值当成 P95。本文件把每个规模下的正确取值钉住，并显式断言
"旧实现会给出错误答案"，防止回退。
"""
import unittest

from metrics_collector import percentile_nearest_rank


class TestNearestRankP95(unittest.TestCase):
    def test_empty_sample_is_none(self):
        # 空样本必须是不存在（None），不能兜底 0.0 —— 那会把"没有数据"伪装成"0 秒完成"
        self.assertIsNone(percentile_nearest_rank([]))
        self.assertIsNone(percentile_nearest_rank([0, -1, None, "x"]))

    def test_single_sample_returns_that_sample(self):
        # n=1：最近秩下标 clamp 到 0；旧实现的 -1 也恰好取到唯一元素（巧合）
        self.assertEqual(percentile_nearest_rank([42]), 42)

    def test_two_samples_take_the_maximum(self):
        # 旧实现在 n=2 时取**最小值**：把 10 当 P95，低估 10 倍
        self.assertEqual(percentile_nearest_rank([10, 100]), 100)

    def test_three_samples_take_maximum(self):
        # ceil(3*0.95)-1 = 2 → 最大值；旧实现 int(2.85)-1 = 1 → 中位数
        self.assertEqual(percentile_nearest_rank([1, 2, 3]), 3)

    def test_small_sample_sizes_are_not_one_rank_low(self):
        # 逐规模钉住：p95 下标必须是 ceil(n*0.95)-1
        import math
        for n in range(1, 40):
            vals = list(range(1, n + 1))
            want = vals[max(0, math.ceil(n * 0.95) - 1)]
            self.assertEqual(percentile_nearest_rank(vals), want, f"n={n}")

    def test_multiple_of_twenty_matches_old_formula(self):
        # n=20/100 是旧实现恰好正确的规模，改动不应影响它们
        self.assertEqual(percentile_nearest_rank(list(range(1, 21))), 19)
        self.assertEqual(percentile_nearest_rank(list(range(1, 101))), 95)

    def test_p95_is_never_below_median(self):
        for n in range(1, 30):
            vals = list(range(1, n + 1))
            med = vals[(n - 1) // 2]
            self.assertGreaterEqual(percentile_nearest_rank(vals), med, f"n={n}")

    def test_old_formula_really_was_wrong(self):
        """把缺陷本身写成断言：旧下标在 n=2/3/9 上确实取错。"""
        def old(values):
            vals = sorted(values)
            return vals[int(len(vals) * 0.95) - 1] if vals else None

        self.assertEqual(old([10, 100]), 10)          # 最小值
        self.assertNotEqual(old([10, 100]), percentile_nearest_rank([10, 100]))
        self.assertEqual(old([1, 2, 3]), 2)           # 中位数
        self.assertNotEqual(old([1, 2, 3]), percentile_nearest_rank([1, 2, 3]))

    def test_boolean_values_are_ignored(self):
        # bool 是 int 的子类；True/False 混进延迟样本会污染分位数
        self.assertEqual(percentile_nearest_rank([True, 10, 20, False]), 20)


class TestSummaryUsesNearestRank(unittest.TestCase):
    def test_write_summary_calls_the_helper(self):
        """调用点必须走最近秩（防回退到旧表达式）。"""
        import inspect
        import metrics_collector
        src = inspect.getsource(metrics_collector)
        self.assertIn("p95 = percentile_nearest_rank(latencies)", src)
        self.assertNotIn("int(len(latencies) * 0.95) - 1", src)


if __name__ == "__main__":
    unittest.main()
