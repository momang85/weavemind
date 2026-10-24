# -*- coding: utf-8 -*-
"""R1-SCREEN-1023（R1-A）：可解释选股纯筛选核心测试。

覆盖验收标准（docs/员工任务_R1可解释选股MVP_20261023.md §6）：

1. 算得对：正/反/未知/冲突样例逐项手算对照；零分母、负利润、单位差异、
   净利润身份（归母 vs 合并）、等值边界、原始精度、稳定排序。
2. 不穿越：截止日后的披露不纳入；截断未来输入不改变过去已冻结结果；缺时点不假装可得。
3. 不掩盖缺失：未通过与资料不足分开；零候选诚实；全部数据不足仍可解释。
4. 能追溯：每个读数带输入 fact_id 与来源 URL；官方缓存与用户上传身份分开。

测试全程不联网、不调用模型；样本分两类：合成边界样例（验证算法）与
真实缓存样例（tmp/q1-real-inputs/financials.json，验证来源链）。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stock_screen as ss


def _row(entity, code, period, *, avail, unit="元", currency="CNY", caliber="合并",
         **metrics):
    base = {"entity": entity, "code": code, "period": period, "unit": unit,
            "currency": currency, "caliber": caliber, "period_type": "年度"}
    if avail is not None:
        base["available_at"] = avail
        base["disclosed_at"] = avail
    base.update(metrics)
    return base


# 手算基准：收入 200→220（增速 +10%），毛利 50/200（毛利率 25%），
# 负债 80 / 资产 200（资产负债率 40%），经营现金流 30 / 归母净利 25（覆盖 120%），
# 合并净利 27（不得顶替归母 25）。
_A = _row("甲公司", "000001.SZ", "2023-12-31", avail="2024-04-20",
          revenue=200_000_000, gross_profit=50_000_000, net_profit=25_000_000,
          net_profit_consolidated=27_000_000, operating_cashflow=30_000_000,
          total_assets=200_000_000, total_liabilities=80_000_000)
_B = _row("甲公司", "000001.SZ", "2024-12-31", avail="2025-04-18",
          revenue=220_000_000, gross_profit=55_000_000, net_profit=30_000_000,
          net_profit_consolidated=33_000_000, operating_cashflow=36_000_000,
          total_assets=210_000_000, total_liabilities=84_000_000)


def _snap(rows, pool=None, as_of="2026-01-31", **kw):
    payload = {"metadata": {"source": "test", "synthetic": True}, "rows": rows}
    if pool is None:
        pool = sorted({str(r["code"]) for r in rows})
    snap, errors = ss.build_snapshot(
        payload, pool=list(pool), as_of=as_of,
        source_label=kw.pop("source_label", "test"),
        source_sha256=kw.pop("source_sha256", "0" * 64),
        synthetic=kw.pop("synthetic", True),
        user_provided=kw.pop("user_provided", False))
    assert not errors, errors
    return snap


def _reading(row: ss.EntityRow, field: str) -> ss.Reading:
    return next(r for r in row.readings if r.field == field)


class TestHandComputed(unittest.TestCase):
    """算得对：合成样例逐项手算对照。"""

    def setUp(self):
        self.snap = _snap([_A, _B])
        self.rules, errs = ss.parse_rules([
            {"field": "revenue_growth", "op": ">=", "value": 5},
            {"field": "gross_margin", "op": ">=", "value": 20},
            {"field": "debt_ratio", "op": "<=", "value": 50},
            {"field": "cashflow_coverage", "op": ">=", "value": 100},
        ])
        assert not errs, errs
        self.result = ss.evaluate(self.snap, self.rules)

    def test_growth_is_ten_percent(self):
        row = self.result.pass_rows[0]
        r = _reading(row, "revenue_growth")
        self.assertEqual(r.status, "ok")
        # (220 - 200) / 200 × 100 = 10
        self.assertAlmostEqual(r.value, 10.0, places=9)
        self.assertEqual(r.display, "10.00%")

    def test_gross_margin_is_25_percent(self):
        r = _reading(self.result.pass_rows[0], "gross_margin")
        # 50 / 200 × 100 = 25
        self.assertAlmostEqual(r.value, 25.0, places=9)
        self.assertEqual(r.display, "25.00%")

    def test_debt_ratio_is_40_percent(self):
        r = _reading(self.result.pass_rows[0], "debt_ratio")
        # 80 / 200 × 100 = 40
        self.assertAlmostEqual(r.value, 40.0, places=9)
        self.assertEqual(r.display, "40.00%")

    def test_coverage_uses_guimu_not_consolidated(self):
        r = _reading(self.result.pass_rows[0], "cashflow_coverage")
        # 36 / 30 × 100 = 120（归母 30，不是合并 33 → 109.09 是错的）
        self.assertAlmostEqual(r.value, 120.0, places=9)
        self.assertNotAlmostEqual(r.value, 36 / 33 * 100, places=6)

    def test_pass_and_amount_reading(self):
        rules, _ = ss.parse_rules([
            {"field": "gross_margin", "op": ">=", "value": 20},
            {"field": "operating_cashflow", "op": ">=", "value": 0},
        ])
        result = ss.evaluate(self.snap, rules)
        row = result.pass_rows[0]
        self.assertEqual(row.verdict, "pass")
        cfo = _reading(row, "operating_cashflow")
        # 36,000,000 元 = 0.36 亿
        self.assertAlmostEqual(cfo.value, 36_000_000.0, places=6)
        self.assertEqual(cfo.display, "0.36 亿")

    def test_counts_no_double_counting(self):
        c = self.result.counts
        self.assertEqual(c["total"], 1)
        self.assertEqual(c["pass"] + c["fail"] + c["insufficient"], 1)


class TestRuleSemantics(unittest.TestCase):
    """规则语义：等值边界、单位换算、未知字段不替换。"""

    def setUp(self):
        self.snap = _snap([_A, _B])

    def test_equality_boundary_passes_for_ge_and_le(self):
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 25}])
        self.assertEqual(ss.evaluate(self.snap, rules).counts["pass"], 1)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": "<=", "value": 25}])
        self.assertEqual(ss.evaluate(self.snap, rules).counts["pass"], 1)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">", "value": 25}])
        res = ss.evaluate(self.snap, rules)
        self.assertEqual(res.counts["pass"], 0)
        self.assertEqual(res.counts["fail"], 1)

    def test_amount_unit_conversion_is_explicit(self):
        # 0.36 亿 == 36,000,000 元 == 3,600 万：同一读数，三种单位写法都该过
        for unit, value in (("元", 36_000_000), ("亿", 0.36), ("万", 3600)):
            rules, errs = ss.parse_rules(
                [{"field": "operating_cashflow", "op": ">=", "value": value, "unit": unit}])
            self.assertEqual(errs, [], (unit, errs))
            self.assertEqual(ss.evaluate(self.snap, rules).counts["pass"], 1, unit)

    def test_amount_display_precision_does_not_change_comparison(self):
        # 阈值 0.3600001 亿 vs 读数 0.36 亿：显示都是 0.36 亿，但比较按原始精度 → 未通过
        rules, _ = ss.parse_rules(
            [{"field": "operating_cashflow", "op": ">=", "value": 0.3600001, "unit": "亿"}])
        res = ss.evaluate(self.snap, rules)
        self.assertEqual(res.counts["pass"], 0)

    def test_unknown_field_is_rejected_not_substituted(self):
        rules, errs = ss.parse_rules([{"field": "net_margin", "op": ">=", "value": 10}])
        self.assertEqual(rules, ())
        self.assertTrue(any("未知字段" in e for e in errs))

    def test_bad_op_and_non_numeric_threshold_rejected(self):
        _, errs = ss.parse_rules([{"field": "gross_margin", "op": "==", "value": 10}])
        self.assertTrue(any("比较符" in e for e in errs))
        _, errs = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": "abc"}])
        self.assertTrue(any("阈值不是数" in e for e in errs))

    def test_pct_threshold_with_unit_is_rejected(self):
        _, errs = ss.parse_rules(
            [{"field": "gross_margin", "op": ">=", "value": 20, "unit": "亿"}])
        self.assertTrue(any("不带单位" in e for e in errs))


class TestMissingVsFail(unittest.TestCase):
    """不掩盖缺失：未通过与资料不足分开；零候选诚实。"""

    def test_missing_field_is_insufficient_not_fail(self):
        rows = [_row("乙公司", "000002.SZ", "2024-12-31", avail="2025-04-10",
                     revenue=100_000_000)]  # 没有 gross_profit / 负债 / 资产
        snap = _snap(rows)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        self.assertEqual(res.counts["insufficient"], 1)
        self.assertEqual(res.counts["fail"], 0)
        row = res.insufficient_rows[0]
        self.assertIn("gross_profit", "；".join(row.reasons))

    def test_fail_and_missing_coexist_but_entity_counted_once(self):
        rows = [_row("乙公司", "000002.SZ", "2024-12-31", avail="2025-04-10",
                     revenue=100_000_000, gross_profit=10_000_000)]  # 毛利率 10% < 20%
        snap = _snap(rows)
        rules, _ = ss.parse_rules([
            {"field": "gross_margin", "op": ">=", "value": 20},
            {"field": "debt_ratio", "op": "<=", "value": 50},   # 缺 total_assets
        ])
        res = ss.evaluate(snap, rules)
        # 无法完整评估 → 资料不足（原因里仍写明毛利率不满足）
        self.assertEqual(res.counts["insufficient"], 1)
        self.assertEqual(res.counts["total"], 1)
        row = res.insufficient_rows[0]
        self.assertIn("不满足", "；".join(row.reasons))
        self.assertIn("total_liabilities", "；".join(row.reasons))

    def test_zero_candidates_is_honest(self):
        rows = [_row("乙公司", "000002.SZ", "2024-12-31", avail="2025-04-10",
                     revenue=100_000_000, gross_profit=50_000_000)]
        snap = _snap(rows)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": "<", "value": 10}])
        res = ss.evaluate(snap, rules)
        self.assertEqual(res.counts["pass"], 0)
        self.assertEqual(res.counts["fail"], 1)   # 诚实：未通过，不给热门公司

    def test_zero_is_a_value_not_missing(self):
        rows = [_row("丙公司", "000003.SZ", "2024-12-31", avail="2025-04-10",
                     revenue=100_000_000, gross_profit=0)]
        snap = _snap(rows)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": "<=", "value": 0}])
        res = ss.evaluate(snap, rules)
        self.assertEqual(res.counts["pass"], 1)   # 真实零：0 ≤ 0 满足
        r = _reading(res.pass_rows[0], "gross_margin")
        self.assertEqual(r.status, "ok")
        self.assertEqual(r.display, "0.00%")


class TestDenominatorRules(unittest.TestCase):
    """零/负分母 → 不适用（不编造比值冲榜）。"""

    def _eval(self, rows, field="cashflow_coverage"):
        snap = _snap(rows)
        rules, _ = ss.parse_rules([{ "field": field, "op": ">=", "value": 1}])
        return ss.evaluate(snap, rules)

    def test_zero_guimu_profit_not_applicable(self):
        rows = [_row("丁公司", "000004.SZ", "2024-12-31", avail="2025-04-10",
                     revenue=100_000_000, net_profit=0, operating_cashflow=10_000_000)]
        res = self._eval(rows)
        self.assertEqual(res.counts["insufficient"], 1)
        r = _reading(res.insufficient_rows[0], "cashflow_coverage")
        self.assertEqual(r.status, "not_applicable")
        self.assertIn("分母为 0", r.reason)

    def test_negative_guimu_profit_not_applicable(self):
        rows = [_row("丁公司", "000004.SZ", "2024-12-31", avail="2025-04-10",
                     revenue=100_000_000, net_profit=-5_000_000,
                     operating_cashflow=10_000_000)]
        res = self._eval(rows)
        r = _reading(res.insufficient_rows[0], "cashflow_coverage")
        self.assertEqual(r.status, "not_applicable")

    def test_growth_with_negative_prev_revenue_not_applicable(self):
        rows = [
            _row("戊公司", "000005.SZ", "2023-12-31", avail="2024-04-10",
                 revenue=-10_000_000),
            _row("戊公司", "000005.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=100_000_000),
        ]
        res = self._eval(rows, field="revenue_growth")
        r = _reading(res.insufficient_rows[0], "revenue_growth")
        self.assertEqual(r.status, "not_applicable")
        self.assertIn("非正", r.reason)

    def test_growth_with_single_period_missing_prior_year(self):
        # G1（2026-10-24 复核）：只有 2024 一期 → 上一自然年 2023 没有观察 →
        # 「缺上一年度」（missing），不再报"最早期间不适用"，也不拿别的期顶替。
        rows = [_row("己公司", "000006.SZ", "2024-12-31", avail="2025-04-10",
                     revenue=100_000_000)]
        res = self._eval(rows, field="revenue_growth")
        r = _reading(res.insufficient_rows[0], "revenue_growth")
        self.assertEqual(r.status, "missing")
        self.assertIn("缺上一年度", r.reason)
        self.assertIn("2023年", r.reason)

    def test_growth_non_adjacent_years_not_yoy(self):
        # G1 复核反例：只有 2022 与 2024 两期（2023 缺）→ 2024 的同比**不可算**，
        # 绝不拿 2022 当"上一期"算出 +50%。上一条测试同款输入在旧实现给出 ok/50。
        rows = [
            _row("复点公司", "600031.SH", "2022-12-31", avail="2023-04-20",
                 revenue=100_000_000),
            _row("复点公司", "600031.SH", "2024-12-31", avail="2025-04-20",
                 revenue=150_000_000),
        ]
        res = self._eval(rows, field="revenue_growth")
        self.assertEqual(res.counts["insufficient"], 1)
        self.assertEqual(res.counts["pass"], 0)
        r = _reading(res.insufficient_rows[0], "revenue_growth")
        self.assertEqual(r.status, "missing")
        self.assertIn("缺上一年度 2023年", r.reason)
        self.assertNotIn("+50", r.display)
        self.assertNotEqual(round(r.value or 0, 2), 50.0)

    def test_growth_cross_currency_conflicts_not_yoy(self):
        # G1：同比两期币种不同（CNY vs HKD）→ 冲突并点名币种，不换算、不猜汇率。
        rows = [
            _row("币异公司", "600032.SH", "2023-12-31", avail="2024-04-20",
                 revenue=100_000_000, currency="HKD"),
            _row("币异公司", "600032.SH", "2024-12-31", avail="2025-04-20",
                 revenue=120_000_000, currency="CNY"),
        ]
        res = self._eval(rows, field="revenue_growth")
        r = _reading(res.insufficient_rows[0], "revenue_growth")
        self.assertEqual(r.status, "conflicting")
        self.assertIn("币种不一致", r.reason)


class TestUnitIdentity(unittest.TestCase):
    """单位与净利润身份：不同 unit 行不顶替；归母/合并不互替。"""

    def test_unit_difference_becomes_conflict(self):
        # 同一 (指标, 期间, 口径) 两行：一行元、一行亿元 → 值身份不同 → 冲突，不任选
        rows = [
            _row("庚公司", "000007.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=100_000_000),
            _row("庚公司", "000007.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=1, unit="亿"),
        ]
        snap = _snap(rows)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 1}])
        res = ss.evaluate(snap, rules)
        # revenue 冲突（元 vs 亿未声明换算）→ 该期间无可用年度观察 → 资料不足，
        # 且原因里点名冲突（不择一、不任选一行）
        self.assertEqual(res.counts["insufficient"], 1)
        self.assertIn("冲突", "；".join(res.insufficient_rows[0].reasons))

    def test_same_value_twice_is_not_conflict(self):
        rows = [
            _row("辛公司", "000008.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=100_000_000, gross_profit=20_000_000),
            _row("辛公司", "000008.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=100_000_000, gross_profit=20_000_000),
        ]
        snap = _snap(rows)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        self.assertEqual(res.counts["pass"], 1)   # 完全相同的两条不是冲突

    def test_conflict_in_latest_period_does_not_fall_back(self):
        # R1.1 起：2023 干净、2024 冲突 → **不回退到 2023** 让该公司混进通过列表；
        # 统一报告期仍是 2024年，该公司列为资料不足并点名冲突（架构师探针 ③ 的核心缺陷）。
        rows = [
            _row("壬公司", "000009.SZ", "2023-12-31", avail="2024-04-10",
                 revenue=100_000_000, gross_profit=20_000_000),
            _row("壬公司", "000009.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=120_000_000, gross_profit=24_000_000),
            _row("壬公司", "000009.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=1.2, gross_profit=0.24, unit="亿"),
        ]
        snap = _snap(rows)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        self.assertEqual(res.pass_rows, ())
        self.assertEqual(res.counts["insufficient"], 1)
        self.assertEqual(res.report_period, "2024年")
        row = res.insufficient_rows[0]
        self.assertEqual(row.eval_period, "2024年")
        joined = "；".join(row.reasons)
        self.assertIn("冲突", joined)
        self.assertIn("2024年", joined)
        self.assertNotIn("已回退", "；".join(res.notes))

    def test_conflict_latest_only_under_pinned_older_year(self):
        # 显式指定 2023 年时，2023 干净可用（用户自己选的基准，不是系统偷偷降年）
        rows = [
            _row("壬公司", "000009.SZ", "2023-12-31", avail="2024-04-10",
                 revenue=100_000_000, gross_profit=20_000_000),
            _row("壬公司", "000009.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=1.2, gross_profit=0.24, unit="亿"),
        ]
        snap = _snap(rows)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules,
                          period_policy={"mode": "pinned", "report_period": "2023"})
        self.assertEqual(res.counts["pass"], 1)
        self.assertEqual(res.pass_rows[0].eval_period, "2023年")
        self.assertTrue(any("指定报告期" in n for n in res.notes))


class TestPitBoundary(unittest.TestCase):
    """不穿越：截止日后的披露不纳入；缺时点待核验；截断不改过去。"""

    def test_future_disclosure_excluded(self):
        rows = [_A, _B, _row("甲公司", "000001.SZ", "2025-12-31",
                             avail="2026-04-30", revenue=500_000_000)]
        snap = _snap(rows, as_of="2026-01-31")
        # 2025 年报 2026-04-30 披露，晚于截止日 2026-01-31 → 不纳入
        ent = snap.entity("000001.SZ")
        self.assertEqual(ent["dataset"].manifest.periods, ("2023年", "2024年"))
        self.assertTrue(any("晚于截止日" in x["reason"] for x in ent["excluded"]))

    def test_period_end_after_asof_excluded(self):
        rows = [_A, _B,
                _row("甲公司", "000001.SZ", "2026-12-31", avail="2026-01-01",
                     revenue=10)]
        snap = _snap(rows, as_of="2026-01-31")
        ent = snap.entity("000001.SZ")
        self.assertTrue(any("期间尚未披露" in x["reason"] for x in ent["excluded"]))
        self.assertEqual(ent["dataset"].manifest.periods, ("2023年", "2024年"))

    def test_missing_availability_is_pending_not_usable(self):
        row = dict(_B)
        row.pop("available_at")
        row.pop("disclosed_at")
        rows = [_A, _B, row]
        snap = _snap(rows, as_of="2026-01-31")
        self.assertEqual(len(snap.pending_rows), 1)
        self.assertIn("待核验", snap.pending_rows[0]["reason"])
        # 该行不参与数据集（不假装当时可得）：2024 年只剩 _B 一条可用观察
        ent = snap.entity("000001.SZ")
        self.assertEqual(ent["dataset"].manifest.periods, ("2023年", "2024年"))

    def test_future_row_does_not_change_past_snapshot_hash(self):
        past = _snap([_A, _B], as_of="2026-01-31")
        with_future = _snap([_A, _B, _row("甲公司", "000001.SZ", "2025-12-31",
                                          avail="2026-04-30", revenue=9)],
                            as_of="2026-01-31")
        # 截止日前的快照身份不因后来新增的未来行而改变
        self.assertEqual(past.snapshot_id, with_future.snapshot_id)

    def test_disclosed_at_fallback_when_available_at_missing(self):
        row = dict(_A)
        row["disclosed_at"] = "2024-04-20"
        row.pop("available_at")
        snap = _snap([row], as_of="2025-01-01")
        self.assertEqual(snap.pending_rows, ())   # disclosed_at 可用 → 不缺时点


class TestPoolAndScope(unittest.TestCase):
    """股票池：精确代码匹配、界外不静默纳入、缺观察诚实。"""

    def test_pool_code_exact_match(self):
        snap = _snap([_A, _B], pool=("000001.SZ",))
        self.assertEqual(snap.entity_ids, ("000001.SZ",))

    def test_hk_code_is_out_of_scope(self):
        payload = {"metadata": {}, "rows": [_row("腾讯控股", "00700.HK", "2024-12-31",
                                                 avail="2025-04-01", revenue=1)]}
        snap, errors = ss.build_snapshot(payload, pool=["00700.HK"], as_of="2026-01-31")
        self.assertIsNone(snap)
        self.assertTrue(any("不是 A 股" in e for e in errors), errors)

    def test_bare_short_code_is_ambiguous(self):
        # 既有约定：1–5 位裸数字市场不明；6 位裸数字 = cn（可纳入）
        payload = {"metadata": {}, "rows": [_row("某公司", "700", "2024-12-31",
                                                 avail="2025-04-01", revenue=1)]}
        snap, errors = ss.build_snapshot(payload, pool=["700"], as_of="2026-01-31")
        self.assertIsNone(snap)
        self.assertTrue(any("裸代码" in e or "市场不明" in e for e in errors), errors)

    def test_bare_six_digit_code_is_cn(self):
        # 既有约定（facts.market_of_code）：裸 6 位数字 = cn，无歧义 → 可纳入
        payload = {"metadata": {}, "rows": [_row("某公司", "000001", "2024-12-31",
                                                 avail="2025-04-01", revenue=1)]}
        snap, errors = ss.build_snapshot(payload, pool=["000001"], as_of="2026-01-31")
        self.assertEqual(errors, [], errors)
        self.assertEqual(snap.entity_ids, ("000001",))

    def test_pool_code_without_observations_is_honest(self):
        snap = _snap([_A, _B], pool=("000001.SZ", "999999.SZ"))
        res = ss.evaluate(snap, ())
        ghost = next(r for r in res.insufficient_rows if r.entity_id == "999999.SZ")
        self.assertIn("不编造", "；".join(ghost.reasons))

    def test_no_usable_data_at_asof_is_insufficient(self):
        # 三一真实缓存的情形：两年报都 2025-04-18 披露 → 截止日 2024-12-31 时无数据
        rows = [_row("壬公司", "000009.SZ", "2023-12-31", avail="2025-04-18",
                     revenue=100_000_000),
                _row("壬公司", "000009.SZ", "2024-12-31", avail="2025-04-18",
                     revenue=110_000_000)]
        snap = _snap(rows, as_of="2024-12-31")
        res = ss.evaluate(snap, ())
        self.assertEqual(res.counts["insufficient"], 1)
        self.assertEqual(res.pass_rows, ())


class TestDeterminism(unittest.TestCase):
    """确定性：同输入同结果；平局稳定；代码顺序不影响读数。"""

    def setUp(self):
        self.rows = [_A, _B]
        self.rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])

    def test_same_input_same_result_hash(self):
        snap1 = _snap(self.rows)
        snap2 = _snap(list(reversed(self.rows)))     # 输入行顺序不影响
        r1 = ss.evaluate(snap1, self.rules)
        r2 = ss.evaluate(snap2, self.rules)
        self.assertEqual(r1.result_hash, r2.result_hash)
        self.assertEqual(r1.snapshot_id, r2.snapshot_id)

    def test_rules_change_changes_hash_not_overwrites(self):
        snap = _snap(self.rows)
        r1 = ss.evaluate(snap, self.rules)
        rules2, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 30}])
        r2 = ss.evaluate(snap, rules2)
        self.assertNotEqual(r1.rules_hash, r2.rules_hash)
        self.assertNotEqual(r1.result_hash, r2.result_hash)

    def test_tie_breaks_by_code_stably(self):
        rows = [
            _row("乙公司", "000002.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=100_000_000, gross_profit=30_000_000),
            _row("甲公司", "000001.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=100_000_000, gross_profit=30_000_000),
        ]
        snap = _snap(rows, pool=("000001.SZ", "000002.SZ"))
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 1}])
        res = ss.evaluate(snap, rules)
        for sort in ({"field": "gross_margin", "direction": "desc"},
                     {"field": "gross_margin", "direction": "asc"}):
            ids = [r["entity_id"] for r in ss.sort_rows(
                [r.as_dict() for r in res.pass_rows], sort)]
            self.assertEqual(ids, ["000001.SZ", "000002.SZ"], sort)

    def test_missing_sort_reading_goes_last_in_both_directions(self):
        rows = [
            _A, _B,
            _row("乙公司", "000002.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=100_000_000),          # 缺毛利 → 增速可算但毛利率缺
        ]
        snap = _snap(rows, pool=("000001.SZ", "000002.SZ"))
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        ordered = list(res.pass_rows) + list(res.fail_rows) + list(res.insufficient_rows)
        for sort in ({"field": "gross_margin", "direction": "desc"},
                     {"field": "gross_margin", "direction": "asc"}):
            ids = [r["entity_id"] for r in ss.sort_rows(
                [r.as_dict() for r in ordered], sort)]
            self.assertEqual(ids[-1], "000002.SZ", sort)

    def test_sort_is_view_only_and_never_changes_identity(self):
        # 架构师探针 ① 的核心契约：排序只改展示顺序，不改 result_hash / 行内容
        snap = _snap([_A, _B])
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        base = [r.entity_id for r in res.pass_rows]
        for sort in ({"field": "gross_margin", "direction": "desc"},
                     {"field": "gross_margin", "direction": "asc"},
                     {"field": "revenue", "direction": "asc"}):
            got = [r["entity_id"] for r in ss.sort_rows(
                    [r.as_dict() for r in res.pass_rows], sort)]
            self.assertEqual(sorted(got), sorted(base), sort)
            self.assertEqual(res.result_hash, ss.evaluate(snap, rules).result_hash, sort)

    def test_invalid_sort_is_an_explicit_error(self):
        self.assertEqual(ss.validate_sort(None), (None, []))
        self.assertEqual(ss.validate_sort({"field": "gross_margin", "direction": "desc"})[1], [])
        _, errs = ss.validate_sort({"field": "net_margin", "direction": "desc"})
        self.assertTrue(any("排序字段" in e for e in errs), errs)
        _, errs = ss.validate_sort({"field": "gross_margin", "direction": "up"})
        self.assertTrue(any("排序方向" in e for e in errs), errs)
        _, errs = ss.validate_sort("gross_margin")
        self.assertTrue(errs)


class TestTraceability(unittest.TestCase):
    """能追溯：读数带 fact_id 与来源；官方缓存与用户上传分开标注。"""

    def test_readings_carry_fact_ids_and_source(self):
        snap = _snap([_A, _B])
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        r = _reading(res.pass_rows[0], "gross_margin")
        self.assertTrue(all(fid.startswith("fact-") for fid in r.inputs))
        self.assertEqual(len(r.inputs), 2)

    def test_official_cache_and_user_upload_flagged_separately(self):
        snap_official = _snap([_A, _B], synthetic=False, user_provided=False)
        snap_user = _snap([_A, _B], synthetic=False, user_provided=True)
        self.assertFalse(snap_official.user_provided)
        self.assertTrue(snap_user.user_provided)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap_user, rules)
        self.assertTrue(any("未独立核验" in n for n in res.notes))

    def test_synthetic_demo_flagged(self):
        snap = _snap([_A, _B], synthetic=True)
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        self.assertTrue(any("合成演示" in n for n in res.notes))


class TestRealCacheSample(unittest.TestCase):
    """真实缓存样例（tmp/q1-real-inputs，官方披露缓存投影）：来源链可用。"""

    @classmethod
    def setUpClass(cls):
        path = os.path.join("tmp", "q1-real-inputs", "financials.json")
        if not os.path.isfile(path):
            raise unittest.SkipTest("真实缓存样例不在本机")
        payload, errors = ss.load_payload_file(path)
        assert not errors, errors
        cls.payload = payload

    def test_two_companies_loaded(self):
        snap, errors = ss.build_snapshot(
            self.payload, pool=["002304.SZ", "600031.SH"], as_of="2026-10-23",
            source_label="official_disclosure_cache",
            source_sha256=ss.payload_sha256(
                os.path.join("tmp", "q1-real-inputs", "financials.json")))
        self.assertEqual(errors, [])
        self.assertEqual(len(snap.entities), 2)
        self.assertFalse(snap.synthetic)
        yanghe = snap.entity("002304.SZ")
        self.assertEqual(yanghe["entity"], "洋河股份")
        self.assertEqual(yanghe["dataset"].manifest.periods,
                         ("2022年", "2023年", "2024年", "2025年"))

    def test_hand_check_yanghe_2024_margin(self):
        snap, _ = ss.build_snapshot(self.payload, pool=["002304.SZ"],
                                    as_of="2026-10-23", source_label="cache")
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 70}])
        res = ss.evaluate(snap, rules,
                          period_policy={"mode": "pinned", "report_period": "2024"})
        row = res.pass_rows[0]
        r = _reading(row, "gross_margin")
        # 手算：211.2508 / 288.763 × 100 ≈ 73.16
        self.assertAlmostEqual(r.value, 211.2508 / 288.763 * 100, places=2)
        self.assertEqual(row.eval_period, "2024年")
        # 溯源：cninfo 原文 URL
        self.assertIn("cninfo.com.cn", r.source_url)

    def test_sany_has_no_data_before_2025_04_18(self):
        snap, _ = ss.build_snapshot(self.payload, pool=["600031.SH"],
                                    as_of="2024-12-31", source_label="cache")
        res = ss.evaluate(snap, ())
        self.assertEqual(res.counts["insufficient"], 1)
        self.assertEqual(res.report_period, "")
        self.assertIn("没有任何可用的年度报告期", "；".join(
            res.insufficient_rows[0].reasons))

    def test_period_pin_absent_in_whole_pool_is_an_error(self):
        # 显式指定池内**完全不存在**的年度：明确报错（不悄悄给空表、不降年）
        snap, _ = ss.build_snapshot(self.payload, pool=["002304.SZ"],
                                    as_of="2026-10-23", source_label="cache")
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 1}])
        with self.assertRaises(ss.PeriodPolicyError) as ctx:
            ss.evaluate(snap, rules, period_policy={"mode": "pinned",
                                                    "report_period": "2021"})
        joined = "；".join(ctx.exception.errors)
        self.assertIn("2021年", joined)
        self.assertIn("不回退", joined)

    def test_negative_operating_cashflow_is_real_reading(self):
        # 洋河 2025 经营现金流为负（-7.63 亿）：真实读数，不是缺失
        snap, _ = ss.build_snapshot(self.payload, pool=["002304.SZ"],
                                    as_of="2026-10-23", source_label="cache")
        rules, _ = ss.parse_rules([{"field": "operating_cashflow", "op": "<", "value": 0}])
        res = ss.evaluate(snap, rules,
                          period_policy={"mode": "pinned", "report_period": "2025"})
        self.assertEqual(res.counts["pass"], 1)
        r = _reading(res.pass_rows[0], "operating_cashflow")
        self.assertAlmostEqual(r.value, -763_040_000.0, delta=1_000_000.0)



class TestPeriodPolicy(unittest.TestCase):
    """R1.1：期间政策（uniform/pinned/latest_each）——不降年、不混合期间、错误明确。"""

    def _two(self):
        # 甲 2023+2024；乙只有 2023 → 统一报告期 = 2024年，乙资料不足（不借用 2023）
        rows = [
            _A, _B,
            _row("乙公司", "000002.SZ", "2023-12-31", avail="2024-04-10",
                 revenue=100_000_000, gross_profit=30_000_000),
        ]
        return _snap(rows, pool=("000001.SZ", "000002.SZ"))

    def test_default_policy_is_uniform_latest(self):
        snap = self._two()
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        self.assertEqual(res.period_policy, {"mode": "uniform", "report_period": ""})
        self.assertEqual(res.report_period, "2024年")
        ids = [r.entity_id for r in res.insufficient_rows]
        self.assertEqual(ids, ["000002.SZ"], ids)
        self.assertTrue(any("没有 2024年" in r for r in res.insufficient_rows[0].reasons))

    def test_pinned_older_year_is_explicit_not_a_downgrade(self):
        snap = self._two()
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules,
                          period_policy={"mode": "pinned", "report_period": "2023"})
        self.assertEqual(res.report_period, "2023年")
        self.assertEqual({r.entity_id for r in res.pass_rows},
                         {"000001.SZ", "000002.SZ"})
        self.assertTrue(any("2023年" in n for n in res.notes))

    def test_pinned_is_normalizing_the_year_label(self):
        policy, errs = ss.validate_period_policy({"mode": "pinned", "report_period": "2024"})
        self.assertEqual((policy, errs), ({"mode": "pinned", "report_period": "2024年"}, []))

    def test_latest_each_is_marked_not_comparable(self):
        snap = self._two()
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules, period_policy={"mode": "latest_each"})
        self.assertFalse(res.comparable)
        self.assertEqual(res.report_period, "")
        self.assertTrue(any("不可直接横比" in n for n in res.notes))
        row2023 = next(r for r in res.pass_rows if r.entity_id == "000002.SZ")
        self.assertEqual(row2023.eval_period, "2023年")
        row2024 = next(r for r in res.pass_rows if r.entity_id == "000001.SZ")
        self.assertEqual(row2024.eval_period, "2024年")

    def test_policy_validation_errors(self):
        for bad in ({"mode": "auto"}, {"mode": "uniform", "report_period": "2024"},
                    {"mode": "latest_each", "report_period": "2024"},
                    {"mode": "pinned"}, {"mode": "pinned", "report_period": "2024-13"},
                    "uniform", 42):
            _, errs = ss.validate_period_policy(bad)
            self.assertTrue(errs, bad)
        for good in (None, "", {}, {"mode": "uniform"},
                     {"mode": "pinned", "report_period": "2024年"},
                     {"mode": "latest_each"}):
            policy, errs = ss.validate_period_policy(good)
            self.assertEqual(errs, [], good)

    def test_policy_change_changes_result_hash(self):
        snap = self._two()
        r1 = ss.evaluate(snap, ())
        r2 = ss.evaluate(snap, (), period_policy={"mode": "pinned",
                                                  "report_period": "2023"})
        self.assertNotEqual(r1.result_hash, r2.result_hash)
        r3 = ss.evaluate(snap, (), period_policy={"mode": "latest_each"})
        self.assertNotEqual(r1.result_hash, r3.result_hash)
        self.assertNotEqual(r2.result_hash, r3.result_hash)

    def test_uniform_conflict_confines_to_insufficient_not_downgrade(self):
        # 甲乙都在 2024 正常；甲 2024 同时有一行冲突 → 甲 2024 读数不可用 →
        # 甲资料不足（点名冲突），乙照常评估；谁都不降回 2023。
        rows = [
            _A, _B,
            _row("甲公司", "000001.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=2.2, gross_profit=0.55, unit="亿"),
            _row("乙公司", "000002.SZ", "2023-12-31", avail="2024-04-10",
                 revenue=100_000_000, gross_profit=30_000_000),
            _row("乙公司", "000002.SZ", "2024-12-31", avail="2025-04-10",
                 revenue=110_000_000, gross_profit=33_000_000),
        ]
        snap = _snap(rows, pool=("000001.SZ", "000002.SZ"))
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        self.assertEqual(res.report_period, "2024年")
        self.assertEqual({r.entity_id for r in res.pass_rows}, {"000002.SZ"})
        row = next(r for r in res.insufficient_rows if r.entity_id == "000001.SZ")
        self.assertEqual(row.eval_period, "2024年")
        self.assertTrue(any("冲突" in r for r in row.reasons))



class TestSnapshotSerialization(unittest.TestCase):
    """复现：快照/结果可序列化；改规则/输入是新执行（哈希不同），历史不被覆盖。"""

    def test_snapshot_roundtrip_keeps_identity(self):
        snap = _snap([_A, _B])
        d = snap.to_dict()
        self.assertEqual(d["snapshot_id"], snap.snapshot_id)
        self.assertEqual(d["schema"], ss.SCHEMA_VERSION)
        self.assertEqual(d["entities"][0]["dataset"]["manifest"]["dataset_hash"],
                         snap.entity("000001.SZ")["dataset"].dataset_hash)

    def test_result_roundtrip_keeps_rows(self):
        snap = _snap([_A, _B])
        rules, _ = ss.parse_rules([{"field": "gross_margin", "op": ">=", "value": 20}])
        res = ss.evaluate(snap, rules)
        d = res.to_dict()
        self.assertEqual(d["result_hash"], res.result_hash)
        self.assertEqual(len(d["pass_rows"]), 1)
        self.assertEqual(d["counts"]["total"], 1)

    def test_asof_change_is_new_snapshot(self):
        s1 = _snap([_A, _B], as_of="2026-01-31")
        s2 = _snap([_A, _B], as_of="2026-06-30")
        self.assertNotEqual(s1.snapshot_id, s2.snapshot_id)


if __name__ == "__main__":
    unittest.main(verbosity=1)
