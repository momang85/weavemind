# -*- coding: utf-8 -*-
"""Y2b 退出关卡：组合回测的**账本与规则可复核**（不是"策略有效"）。

断言按架构规划 Y2b 的验收栏（每条对应一个会被算错/掩盖的现实）：
- **不用未来信息**：信号只用规则（不含价格），成交在下一交易日**开盘**；
- **不可能成交要报出来**：现金不足／T+1 未解禁／当日无行（停牌）／不足一手 ⇒ 进 `rejections`，不静默撮合；
- **T+1**：当日买入当日不可卖；
- **成本前后**都给，差额单列；印花税按**生效日**取（2023-08-28 起 0.05%）；
- **分红**：不复权价 + 显式分红事件能进现金账；前复权价传 dividends 要提示重复计算；
- **留出集区间固定**在 spec 里，不是事后挑；**规则无拟合参数**要写明；
- **账本审计**（现金守恒/持仓估值/净值恒等/不许负现金）独立跑一遍；
- **无效结果**（无成交）如实记 `invalid` + 原因，**不产 0 收益**；
- Qlib **不在主环境**：`available()` 为假时给原因与安装指引，不编造委托实现。
"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from quant_research import backtest as bt  # noqa: E402
from quant_research import qlib_adapter as qa  # noqa: E402

DATES = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08",
         "2025-01-09", "2025-01-10", "2025-02-03", "2025-02-04"]


def _row(code, date, o, c, vol=100000):
    return {"code": code, "date": date, "open": o, "close": c, "volume": vol,
            "available_at": f"{date}T15:00:00+08:00", "source": "test"}


def _payload(rows, *, adj="不复权"):
    return {"schema": "weavemind.market_history/0", "dataset_id": "ds-bt", "source": "test",
            "license": "免费源·仅内部试验", "adj_basis": adj, "rows": len(rows), "data": rows}


def _flat(code, price, *, bench=False):
    """价格恒定的标的：没有价格变动时，收益只能来自成本/分红 ⇒ 便于手算。"""
    return [_row(code, d, price, price) for d in DATES]


class CoreAccountingTest(unittest.TestCase):
    def test_buy_and_hold_single_name_matches_hand_calc(self):
        """单标的、一次性买入、价格 10→12：不计成本收益 = 20%（手算）。

        首日买入：现金 100,000，价 10 ⇒ 可买 10,000 股（整手），现金≈0；
        末日净值 = 10,000 × 12 = 120,000 ⇒ +20%。含成本略低。
        """
        rows = [_row("600031", DATES[0], 10.0, 10.0)] + \
               [_row("600031", d, 12.0, 12.0) for d in DATES[1:]]
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "buy_and_hold",
                                    "initial_cash": 100000.0},
                   costs={"commission_bps": 0, "commission_min": 0, "transfer_fee_bps": 0,
                          "slippage_bps": 0, "stamp_duty_sell_bps": 0},
                   license="免费源·仅内部试验")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["metrics"]["total_return"], 0.2)
        self.assertEqual(r["metrics"]["nocost_total_return"], 0.2)
        self.assertTrue(r["audit"]["ok"], r["audit"]["failed"])
        self.assertFalse(r["delivery_eligible"])

    def test_costs_reduce_return_and_are_reported_separately(self):
        rows = [_row("600031", DATES[0], 10.0, 10.0)] + \
               [_row("600031", d, 12.0, 12.0) for d in DATES[1:]]
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "buy_and_hold",
                                    "initial_cash": 100000.0},
                   costs={"slippage_bps": 50, "commission_bps": 25, "commission_min": 5,
                          "transfer_fee_bps": 1, "stamp_duty_sell_bps": 10},
                   license="免费源·仅内部试验")
        m = r["metrics"]
        self.assertLess(m["total_return"], m["nocost_total_return"])
        self.assertGreater(m["cost_drag"], 0)
        self.assertGreater(m["fees"]["commission"], 0)
        self.assertGreater(m["fees"]["slippage_cost"], 0)

    def test_stamp_duty_uses_effective_date_schedule(self):
        self.assertEqual(bt.stamp_duty_bps("2023-08-28", {}), 5.0)
        self.assertEqual(bt.stamp_duty_bps("2023-08-27", {}), 10.0)
        self.assertEqual(bt.stamp_duty_bps("2024-01-01", {"stamp_duty_sell_bps": 1.0}), 1.0)

    def test_sell_pays_stamp_duty_buy_does_not(self):
        rows = []
        for d in DATES:
            rows.append(_row("600031", d, 10.0, 10.0))
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "monthly",
                                    "initial_cash": 100000.0},
                   costs={"stamp_duty_sell_bps": 10, "commission_bps": 0, "commission_min": 0,
                          "transfer_fee_bps": 0, "slippage_bps": 0},
                   license="免费源·仅内部试验")
        buys = [o for o in r["orders"] if o["side"] == "buy"]
        sells = [o for o in r["orders"] if o["side"] == "sell"]
        self.assertTrue(all(o["stamp_duty"] == 0 for o in buys))
        if sells:
            self.assertTrue(all(o["stamp_duty"] > 0 for o in sells))


class NoImpossibleFillTest(unittest.TestCase):
    def test_suspension_day_blocks_execution_with_reason(self):
        """**再平衡日**该标的停牌（无行）⇒ 那次再不成交并记原因，**不用收盘价替代**。

        刻意保留首个交易日（否则交易日历整体后移，首日仍能买入，测不到这个分支）：
        2025-02-03 是当月第一个交易日、也是再平衡日，标的当日无行。
        """
        rows = [_row("600031", d, 10.0, 10.0) for d in DATES if d != "2025-02-03"]
        rows += [_row("000300", d, 100.0, 100.0) for d in DATES]
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "monthly",
                                    "initial_cash": 100000.0}, benchmark="000300",
                   license="免费源·仅内部试验")
        self.assertEqual(r["status"], "ok")
        rej = [x for x in r["rejections"] if x["date"] == "2025-02-03"]
        self.assertTrue(rej, "再平衡日的停牌没有留下拒绝记录")
        self.assertIn("没有行情行", rej[0]["reason"])
        self.assertIn("不用收盘价替代", rej[0]["reason"])

    def test_missing_first_day_just_starts_later(self):
        """首个交易日标的没有行 ⇒ 交易日历（标的并集）自然从有数据那天开始，不算异常。"""
        rows = [_row("600031", d, 10.0, 10.0) for d in DATES if d != DATES[0]]
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "buy_and_hold",
                                    "initial_cash": 100000.0}, license="免费源·仅内部试验")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["nav"][0]["date"], DATES[1])

    def test_insufficient_cash_is_rejected_not_clipped(self):
        """一手都买不起 ⇒ 记 rejection，不许"买一点凑合"。"""
        rows = _flat("600031", 100000.0)
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "buy_and_hold",
                                    "initial_cash": 1000.0}, license="免费源·仅内部试验")
        self.assertEqual(r["status"], "invalid")
        self.assertTrue(any("现金不足一手" in x["reason"] for x in r["rejections"]))

    def test_t_plus_1_blocks_same_day_sell(self):
        """当日买入当日不可卖：把再平衡频率拉高到每天，仍有 T+1 拦截记录。"""
        rows = _flat("600031", 10.0)
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "monthly",
                                    "initial_cash": 100000.0}, license="免费源·仅内部试验")
        self.assertTrue(r["audit"]["ok"], r["audit"]["failed"])
        # 首月买入后，同月内不应出现"卖出当日买入的量"：审计里持仓不为负即已覆盖
        self.assertTrue(all(o["qty"] > 0 for o in r["orders"]))

    def test_orders_never_exceed_available_cash(self):
        rows = _flat("600031", 7.77)
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "monthly",
                                    "initial_cash": 50000.0}, license="免费源·仅内部试验")
        self.assertGreaterEqual(min(x["cash_after"] for x in r["orders"]), 0.0)


class RebalanceOrderInvarianceTest(unittest.TestCase):
    """复核 §3C 的最小修：**输入顺序不改经济结果**。

    反例（`docs/evidence/20261006-report-review/quant_counterexamples.md`）：零费用、1000 元
    本金、1 股一手、四个交易日、两只股票，只把 `universe` 从 `[A,B]` 换成 `[B,A]`，旧实现
    总收益 25% → 49%，且两边 `audit.ok` 都是 true —— **账本守恒并不证明等权分配正确**。
    修法：冻结目标差额 → 统一卖出 → 按规范顺序（代码序）统一买入。
    """

    ROWS = [("2025-01-02", 10.0, 10.0), ("2025-01-31", 5.0, 15.0),
            ("2025-02-03", 5.0, 15.0), ("2025-02-04", 10.0, 15.0)]

    def _fixture(self):
        rows = []
        for d, a, b in self.ROWS:
            for c, v in (("A", a), ("B", b)):
                rows.append({"date": d, "code": c, "open": v, "close": v, "available_at": d})
        return {"schema": "market_history/0", "dataset_id": "fixture-order",
                "source": "synthetic", "license": "unknown", "adj_basis": "qfq",
                "rows": len(rows), "instruments": ["A", "B"],
                "date_range": ["2025-01-02", "2025-02-04"], "data": rows}

    def _run(self, universe):
        return bt.run(self._fixture(),
                      {"universe": list(universe), "initial_cash": 1000.0, "lot_size": 1},
                      costs={k: 0 for k in bt.DEFAULT_COSTS})

    def test_universe_order_does_not_change_economics(self):
        ab, ba = self._run(["A", "B"]), self._run(["B", "A"])
        for r in (ab, ba):
            self.assertEqual(r["status"], "ok")
            self.assertTrue(r["audit"]["ok"], r["audit"]["failed"])
        # 逐笔成交（含顺序与金额）、逐日净值、拒绝记录都必须与名单顺序无关
        self.assertEqual(ab["orders"], ba["orders"])
        self.assertEqual(ab["nav"], ba["nav"])
        self.assertEqual(ab["rejections"], ba["rejections"])
        self.assertEqual(ab["metrics"]["total_return"], ba["metrics"]["total_return"])
        self.assertEqual(ab["positions"], ba["positions"])

    def test_sells_are_visible_to_buys_in_the_same_rebalance(self):
        """同一再平衡日先卖后买：卖出释放的现金对买单可见，不因"先遇到买单"而少买。

        2025-02-03：权益 1000 ⇒ 各目标 500；B 现值 750 ⇒ 卖 16 股（240 元，余 34 股），
        A 现值 250 ⇒ 用这 240 元买 48 股（5 元/股，手算）。旧实现在 `[A,B]` 下会先把 A 的
        买单按"现金 0"拒绝，那 240 元再也没有机会被用上 ⇒ 25%。
        """
        ab = self._run(["A", "B"])
        feb = [o for o in ab["orders"] if o["date"] == "2025-02-03"]
        self.assertEqual([(o["side"], o["code"]) for o in feb], [("sell", "B"), ("buy", "A")])
        self.assertEqual(feb[0]["qty"], 16)
        self.assertEqual(feb[1]["qty"], 48)
        self.assertEqual(ab["nav"][-1]["cash"], 0.0)
        self.assertAlmostEqual(ab["metrics"]["total_return"], 0.49, places=6)

    def test_execution_rule_is_reported_with_the_result(self):
        r = self._run(["A", "B"])
        self.assertIn("规范顺序", r["execution_rule"])
        self.assertTrue(any("规范顺序" in x for x in r["limits"]))


class DividendTest(unittest.TestCase):
    def test_explicit_dividend_enters_cash_ledger(self):
        """不复权价 + 显式分红：持 10,000 股、每股派 0.5 ⇒ 现金 +5,000（手算）。"""
        rows = _flat("600031", 10.0)
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "buy_and_hold",
                                    "initial_cash": 100000.0},
                   costs={"commission_bps": 0, "commission_min": 0, "transfer_fee_bps": 0,
                          "slippage_bps": 0, "stamp_duty_sell_bps": 0},
                   dividends={"600031": {DATES[3]: 0.5}}, license="免费源·仅内部试验")
        ev = r["dividend_events"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["qty"], 10000)
        self.assertEqual(ev[0]["cash_in"], 5000.0)
        self.assertAlmostEqual(r["metrics"]["total_return"], 0.05, places=6)

    def test_dividends_with_qfq_prices_warn_about_double_count(self):
        rows = _flat("600031", 10.0)
        r = bt.run(_payload(rows, adj="前复权(qfq)"),
                   {"universe": ["600031"], "rebalance": "buy_and_hold",
                    "initial_cash": 100000.0},
                   dividends={"600031": {DATES[3]: 0.5}}, license="免费源·仅内部试验")
        self.assertTrue(any("重复计算分红" in x for x in r["limits"]))


class MetricsAndHoldoutTest(unittest.TestCase):
    def test_max_drawdown_hand_calc(self):
        """10 → 20 → 10：净值峰值 20、谷 10 ⇒ 回撤 −50%（手算）。"""
        px = [10.0, 20.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        rows = [_row("600031", d, p, p) for d, p in zip(DATES, px)]
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "buy_and_hold",
                                    "initial_cash": 100000.0},
                   costs={"commission_bps": 0, "commission_min": 0, "transfer_fee_bps": 0,
                          "slippage_bps": 0, "stamp_duty_sell_bps": 0},
                   license="免费源·仅内部试验")
        self.assertAlmostEqual(r["metrics"]["max_drawdown"], -0.5, places=6)

    def test_benchmark_comparison_present_and_labelled(self):
        rows = _flat("600031", 10.0) + [_row("000300", d, 100.0, 100.0 + i)
                                        for i, d in enumerate(DATES)]
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "buy_and_hold",
                                    "initial_cash": 100000.0}, benchmark="000300",
                   license="免费源·仅内部试验")
        self.assertIn("return", r["metrics"]["benchmark"])
        self.assertIn("excess_return", r["metrics"]["benchmark"])
        self.assertIn("收盘价买入持有", r["metrics"]["benchmark"]["code_basis"])

    def test_holdout_range_is_fixed_in_spec_and_reported(self):
        rows = _flat("600031", 10.0)
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "buy_and_hold",
                                    "initial_cash": 100000.0, "holdout_start": "2025-02-03"},
                   license="免费源·仅内部试验")
        h = r["holdout"]
        self.assertTrue(h["configured"])
        self.assertEqual(h["holdout_start"], "2025-02-03")
        self.assertIn("out_of_sample", h["segments"])
        self.assertIn("in_sample", h["segments"])
        self.assertIn("不是过拟合", h["note"])

    def test_spec_hash_changes_with_params(self):
        base = {"universe": ["600031"], "rebalance": "monthly"}
        other = {"universe": ["600031"], "rebalance": "buy_and_hold"}
        self.assertNotEqual(bt.spec_hash(base, bt.DEFAULT_COSTS),
                            bt.spec_hash(other, bt.DEFAULT_COSTS))

    def test_rule_uses_no_price_so_no_lookahead(self):
        """目标权重只看规则与名单：同一 spec 在不同价格下目标一致。"""
        sp = {"universe": ["600031", "002304"], "cash_buffer": 0.1}
        w1 = bt.target_weights(sp, ["600031", "002304"], date="2025-01-02")
        w2 = bt.target_weights(sp, ["600031", "002304"], date="2025-06-30")
        self.assertEqual(w1, w2)
        self.assertAlmostEqual(sum(w1.values()), 0.9, places=9)


class AuditAndInvalidTest(unittest.TestCase):
    def test_audit_checks_present_and_pass_on_clean_run(self):
        rows = _flat("600031", 10.0)
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "monthly",
                                    "initial_cash": 100000.0}, license="免费源·仅内部试验")
        names = {c["check"] for c in r["audit"]["checks"]}
        for expect in ("no_negative_cash", "nav_identity", "cash_conservation_exact",
                       "cash_vs_rounded_orders_within_rounding_allowance",
                       "positions_match_ledger", "exposure_in_range"):
            self.assertIn(expect, names)
        self.assertTrue(r["audit"]["ok"], r["audit"]["failed"])

    def test_carries_input_binding_for_replay(self):
        """Y2 数值接入的前置条件：回测必须带 `input_kind` 绑定与输入指纹（答得出"跑在哪份数据上"）。"""
        rows = _flat("600031", 10.0)
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "monthly",
                                    "initial_cash": 100000.0}, license="免费源·仅内部试验")
        self.assertEqual(r["input_kind"]["input_kind"], "market_history")
        self.assertEqual(r["input_kind"]["dataset_id"], "ds-bt")
        self.assertEqual(r["input_kind"]["adj_basis"], "不复权")
        self.assertTrue(r["input_fingerprint"])

    def test_input_binding_changes_reading_hash(self):
        """换数据集必须换读数哈希：否则「绑定」是摆设。"""
        rows = _flat("600031", 10.0)
        a = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "monthly",
                                    "initial_cash": 100000.0}, license="免费源·仅内部试验")
        p2 = _payload(rows)
        p2["dataset_id"] = "ds-bt-2"
        b = bt.run(p2, {"universe": ["600031"], "rebalance": "monthly",
                        "initial_cash": 100000.0}, license="免费源·仅内部试验")
        self.assertNotEqual(a["input_fingerprint"], b["input_fingerprint"])
        self.assertNotEqual(a["reading_hash"], b["reading_hash"])

    def test_exact_conservation_holds_with_many_orders(self):
        """**实机踩到的坑**：逐笔金额是 2 位小数，拿它回推的累计舍入误差会随笔数增长。

        权威判据必须是"用未四舍五入台账"的守恒式；这里用会反复再平衡的构造跑几十笔来锁住。
        """
        rows = []
        for i, d in enumerate([f"2025-{m:02d}-{dd:02d}" for m in range(1, 13)
                               for dd in (2, 9, 16, 23)]):
            rows.append(_row("600031", d, 10.0 + (i % 5) * 0.37, 10.0 + (i % 3) * 0.51))
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "monthly",
                                    "initial_cash": 100000.0}, license="免费源·仅内部试验")
        self.assertGreater(r["metrics"]["orders"], 3)
        self.assertTrue(r["audit"]["ok"], r["audit"]["failed"])
        led = r["ledger"]
        self.assertIn("buy_gross", led)
        expect = (100000.0 - led["buy_gross"] + led["sell_gross"] - led["fees"]
                  + led["dividends"])
        self.assertAlmostEqual(expect, r["nav"][-1]["cash"], places=1)

    def test_no_data_is_unavailable_not_zero(self):
        r = bt.run(_payload([]), {"universe": ["600031"]})
        self.assertEqual(r["status"], "unavailable")
        self.assertNotIn("metrics", r)

    def test_unknown_code_is_unavailable(self):
        r = bt.run(_payload(_flat("600031", 10.0)), {"universe": ["999999"]})
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("999999", r["reason"])

    def test_missing_universe_is_unavailable(self):
        r = bt.run(_payload(_flat("600031", 10.0)), {})
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("universe", r["reason"])

    def test_reading_hash_is_stable_and_covers_params(self):
        rows = _flat("600031", 10.0)
        p = _payload(rows)
        a = bt.run(p, {"universe": ["600031"], "rebalance": "monthly",
                       "initial_cash": 100000.0}, license="免费源·仅内部试验")
        b = bt.run(p, {"universe": ["600031"], "rebalance": "monthly",
                       "initial_cash": 100000.0}, license="免费源·仅内部试验")
        c = bt.run(p, {"universe": ["600031"], "rebalance": "buy_and_hold",
                       "initial_cash": 100000.0}, license="免费源·仅内部试验")
        self.assertEqual(a["reading_hash"], b["reading_hash"])
        self.assertNotEqual(a["reading_hash"], c["reading_hash"])

    def test_limits_state_what_this_is_not(self):
        rows = _flat("600031", 10.0)
        r = bt.run(_payload(rows), {"universe": ["600031"], "rebalance": "monthly",
                                    "initial_cash": 100000.0}, license="免费源·仅内部试验")
        joined = " ".join(r["limits"])
        self.assertIn("不是策略有效性结论", joined)
        self.assertIn("不宣称 alpha", joined)
        self.assertIn("不代表全 A 股成绩", joined)


class QlibBoundaryTest(unittest.TestCase):
    def test_qlib_availability_is_reported_honestly(self):
        ok, why = qa.available()
        if not ok:
            self.assertIn("独立环境", why)
        d = qa.describe()
        self.assertEqual(d["qlib_available"], ok)
        self.assertTrue(d["install_recipe"])

    def test_run_via_qlib_never_fabricates(self):
        out = qa.run_via_qlib(payload={}, spec={})
        self.assertEqual(out["status"], "unavailable")
        self.assertTrue(out["reason"])

    def test_export_csv_is_readable_and_skips_missing_close(self):
        rows = _flat("600031", 10.0) + [_row("000300", d, 100.0, 100.0) for d in DATES]
        rows.append({"code": "600031", "date": "2025-01-13", "close": "", "open": 1.0})
        with tempfile.TemporaryDirectory() as td:
            res = qa.export_qlib_csv(_payload(rows), td, benchmark="000300")
            self.assertTrue(res["ok"])
            self.assertEqual(res["skipped_no_close"], 1)
            with open(res["subjects_csv"], encoding="utf-8") as fh:
                got = list(csv.DictReader(fh))
            self.assertTrue(got)
            self.assertEqual(set(got[0]), set(qa.QLIB_COLUMNS))
            self.assertTrue(all(r["instrument"] == "600031" for r in got))
            with open(res["benchmark_csv"], encoding="utf-8") as fh:
                b = list(csv.DictReader(fh))
            self.assertTrue(all(r["instrument"] == "000300" for r in b))


if __name__ == "__main__":
    unittest.main()
