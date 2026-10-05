# -*- coding: utf-8 -*-
"""Y2a 退出关卡：事件窗口收益读数——**防未来函数、口径显式、重叠不去重就不聚合、可复算**。

断言按架构指令的退出标准（每条对应一个会被算错的现实）：
- 切片只取 `date > 事件日` 的行，`t-1` 是事件日当日或之前最后一个交易日（防未来函数）；
- `adj_basis` 缺失 ⇒ `unavailable`；标的为不复权时默认**拒给**收益读数；
- 基准缺该交易日 ⇒ 超额收益留空，**不补 0、不前值填充**；
- 重叠只按**同一标的**判（跨标的是两个实验单元），被排除的事件**仍出单列读数**；
- 样本不足不产统计量（n<2 无均值、n<3 无比例字段）；跨标的不合并平均；
- 同输入必得同 `reading_hash`；换输入则 `replay` 报出差异**且不改写旧读数**；
- 免费源/个人非商业许可 `delivery_eligible=False`（不得进对外下载包）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from quant_research import event_returns as er  # noqa: E402


def _payload(rows, *, adj_basis="前复权(qfq)", dataset_id="ds-1",
             license_="免费源·仅内部试验"):
    return {"schema": "weavemind.market_history/0", "dataset_id": dataset_id,
            "source": "akshare", "license": license_, "adj_basis": adj_basis,
            "imported_at": "2026-10-05T00:00:00+08:00", "instruments": sorted(
                {r["code"] for r in rows}),
            "date_range": [min(r["date"] for r in rows), max(r["date"] for r in rows)],
            "rows": len(rows), "issues": [], "unavailable": [], "data": rows}


def _mk(code, date, close, avail=None):
    return {"code": code, "date": date, "close": close,
            "available_at": avail or f"{date}T15:00:00+08:00", "source": "test"}


# 交易日序列（含周末跳过，故意让自然日 ≠ 交易日）
DAYS = ["2025-04-17", "2025-04-18", "2025-04-21", "2025-04-22", "2025-04-23",
        "2025-04-24", "2025-04-25", "2025-04-28", "2025-04-29", "2025-04-30",
        "2025-05-06", "2025-05-07", "2025-05-08", "2025-05-09", "2025-05-12",
        "2025-05-13", "2025-05-14", "2025-05-15", "2025-05-16", "2025-05-19",
        "2025-05-20", "2025-05-21", "2025-05-22", "2025-05-23", "2025-05-26"]


def _rows(code, base=20.0, step=0.1):
    return [_mk(code, d, round(base + i * step, 2)) for i, d in enumerate(DAYS)]


class FutureFilterTest(unittest.TestCase):
    def test_slice_only_uses_rows_after_event_date(self):
        p = _payload(_rows("600031"))
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "e"}],
                       offsets=(0, 1, 5))
        self.assertEqual(r["status"], "ok")
        it = r["readings"][0]
        # 锚点 = 事件日当日（2025-04-18 是交易日）；t0 = 之后第一个交易日
        self.assertEqual(it["anchor"]["date"], "2025-04-18")
        self.assertEqual(it["t0_date"], "2025-04-21")
        for pt in it["points"]:
            if pt["offset"] > 0:
                self.assertGreater(pt["date"], "2025-04-18",
                                   "窗口里出现了事件日当日或之前的行 ⇒ 未来函数")
        self.assertEqual(it["points"][0]["offset"], -1)
        self.assertIsNone(it["points"][0]["return"])          # 锚点无当日收益

    def test_event_on_non_trading_day_anchors_previous_session(self):
        p = _payload(_rows("600031"))
        # 2025-04-20 是周日：锚点应取 04-18，t0 取 04-21
        r = er.compute(p, [{"code": "600031", "date": "2025-04-20", "label": "e"}],
                       offsets=(0,))
        it = r["readings"][0]
        self.assertEqual(it["anchor"]["date"], "2025-04-18")
        self.assertEqual(it["t0_date"], "2025-04-21")

    def test_no_row_after_event_is_unavailable_not_zero(self):
        p = _payload(_rows("600031"))
        r = er.compute(p, [{"code": "600031", "date": "2030-01-01", "label": "e"}])
        self.assertEqual(r["status"], "unavailable")
        self.assertTrue(r["reason"])
        self.assertIn("unavailable", r)


class CaliberGateTest(unittest.TestCase):
    def test_missing_adj_basis_refuses(self):
        p = _payload(_rows("600031"), adj_basis="")
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "e"}])
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("复权口径", r["reason"])

    def test_unadjusted_refused_by_default(self):
        p = _payload(_rows("600031"), adj_basis="不复权")
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "e"}])
        self.assertEqual(r["status"], "unavailable")
        self.assertIn("不复权", r["reason"])

    def test_unadjusted_allowed_explicitly_adds_caveat(self):
        p = _payload(_rows("600031"), adj_basis="不复权")
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "e"}],
                       allow_unadjusted=True)
        self.assertEqual(r["status"], "ok")
        self.assertTrue(any("不复权" in x for x in r["limits"]),
                        "显式放行不复权必须在 limits 里留痕")

    def test_unadjusted_with_adj_factor_column_is_not_treated_as_raw(self):
        rows = _rows("600031")
        for r_ in rows:
            r_["adj_factor"] = 1.0
        p = _payload(rows, adj_basis="不复权+adj_factor 自算")
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "e"}])
        self.assertEqual(r["status"], "ok", "带 adj_factor 可自算复权 ⇒ 不该被当作纯不复权拒掉")

    def test_no_data_is_unavailable(self):
        p = {"schema": "weavemind.market_history/0", "source": "unavailable",
             "reason": "没有可用数据集", "data": [], "rows": 0}
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "e"}])
        self.assertEqual(r["status"], "unavailable")
        self.assertEqual(r["unavailable"][0]["reason"], "没有可用数据集")

    def test_no_events_is_unavailable(self):
        r = er.compute(_payload(_rows("600031")), [])
        self.assertEqual(r["status"], "unavailable")


class BenchmarkAlignmentTest(unittest.TestCase):
    def test_benchmark_missing_day_leaves_excess_empty_not_zero(self):
        subj = _rows("600031")
        bench = [r for r in _rows("000300", base=4000.0, step=5.0)
                 if r["date"] != "2025-04-22"]                    # 基准故意缺一天
        p = _payload(subj + bench)
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "e"}],
                       benchmark="000300", offsets=(0, 1))
        it = r["readings"][0]
        pt = next(x for x in it["points"] if x["offset"] == 1)
        self.assertIsNone(pt["excess_return"])
        self.assertTrue(it["benchmark_missing"])

    def test_excess_is_geometric_not_arithmetic_difference(self):
        subj = _rows("600031")
        bench = [r for r in _rows("000300", base=100.0, step=1.0)]
        p = _payload(subj + bench)
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "e"}],
                       benchmark="000300", offsets=(0,))
        pt = r["readings"][0]["points"][0]
        self.assertEqual(pt["offset"], -1)
        pt0 = r["readings"][0]["points"][1]
        expect = (1 + pt0["return"]) / (1 + pt0["benchmark_return"]) - 1
        self.assertAlmostEqual(pt0["excess_return"], expect, places=5)
        self.assertNotAlmostEqual(pt0["excess_return"],
                                  pt0["return"] - pt0["benchmark_return"], places=5)

    def test_uses_verified_disclosure_dates_not_guessed(self):
        """真实事件日必须来自证据文档里的公开披露日（防"我记得是那天"）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "x1_event_returns_run", ROOT / "scripts" / "x1_event_returns_run.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        got = {(e["code"], e["date"]) for e in mod.DEFAULT_EVENTS}
        self.assertIn(("002304", "2025-04-29"), got)   # 洋河 2024 年报（证据文档公开日）
        self.assertIn(("002304", "2024-04-27"), got)   # 洋河 2023 年报
        self.assertIn(("600031", "2025-04-18"), got)   # 三一 2024 年报
        self.assertNotIn(("002304", "2025-04-28"), got,
                         "2025-04-28 不是证据文档里的公开披露日 ⇒ 不得回退成旧口径")


class OverlapTest(unittest.TestCase):
    def test_same_code_overlap_excluded_from_aggregate_but_still_read(self):
        p = _payload(_rows("600031"))
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "a"},
                           {"code": "600031", "date": "2025-04-25", "label": "b"}],
                       offsets=(0, 5))
        # 两个事件都出读数（单列），但后一个不进聚合
        self.assertEqual(len(r["readings"]), 2)
        self.assertEqual([x["aggregate_eligible"] for x in r["readings"]], [True, False])
        self.assertTrue(r["readings"][1]["aggregate_exclusion_reason"])
        self.assertEqual(r["aggregate"]["readings_used"], 1)
        self.assertEqual(r["aggregate"]["excluded_from_aggregate"],
                         ["600031@2025-04-25"])

    def test_cross_code_overlap_is_not_dedup(self):
        p = _payload(_rows("600031", base=20.0) + _rows("002304", base=60.0))
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "a"},
                           {"code": "002304", "date": "2025-04-21", "label": "b"}],
                       offsets=(0, 5))
        self.assertTrue(all(x["aggregate_eligible"] for x in r["readings"]),
                        "跨标的日历重叠不是重复计数 ⇒ 不该被排除")
        self.assertIn("跨标的", r["aggregate"]["cross_code_pooling"])

    def test_keep_all_policy_marks_overlap_without_excluding(self):
        p = _payload(_rows("600031"))
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "a"},
                           {"code": "600031", "date": "2025-04-25", "label": "b"}],
                       offsets=(0, 5), policy="keep_all")
        self.assertTrue(all(x["aggregate_eligible"] for x in r["readings"]))
        self.assertEqual(r["aggregate"]["readings_used"], 2)


class AggregateDisciplineTest(unittest.TestCase):
    def test_single_event_gives_no_statistic(self):
        p = _payload(_rows("600031"))
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "a"}],
                       offsets=(0, 5))
        cell = r["aggregate"]["by_code"]["600031"]["by_offset"]["t+0"]
        self.assertNotIn("mean_excess_return", cell)
        self.assertIn("样本不足", cell["note"])
        self.assertIn("no_proportion_stat", cell)

    def test_two_non_overlapping_events_give_mean_and_median(self):
        rows = ([_mk("600031", d, 20.0 + i * 0.1) for i, d in enumerate(DAYS)]
                + [_mk("600031", f"2026-{d[5:]}", 30.0 + i * 0.1)
                   for i, d in enumerate(DAYS)]
                + [_mk("000300", d, 4000.0 + i * 5.0) for i, d in enumerate(DAYS)]
                + [_mk("000300", f"2026-{d[5:]}", 4100.0 + i * 5.0)
                   for i, d in enumerate(DAYS)])
        p = _payload(rows)
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "a"},
                           {"code": "600031", "date": "2026-04-18", "label": "b"}],
                       benchmark="000300", offsets=(0, 5))
        self.assertEqual(r["aggregate"]["readings_used"], 2, r["overlap"])
        cell = r["aggregate"]["by_code"]["600031"]["by_offset"]["t+0"]
        self.assertEqual(cell["n_excess"], 2)
        self.assertIn("mean_excess_return", cell)
        self.assertIn("median_excess_return", cell)
        self.assertIn("no_proportion_stat", cell)      # n<3 仍不给比例
        self.assertNotIn("win_rate", json.dumps(r))

    def test_cross_code_is_never_pooled(self):
        p = _payload(_rows("600031", base=20.0) + _rows("002304", base=60.0))
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "a"},
                           {"code": "002304", "date": "2025-04-21", "label": "b"}],
                       offsets=(0, 5))
        self.assertEqual(sorted(r["aggregate"]["by_code"]), ["002304", "600031"])
        self.assertEqual(r["aggregate"]["by_code"]["600031"]["n_events"], 1)
        self.assertNotIn("mean_excess_return",
                         r["aggregate"]["by_code"]["600031"]["by_offset"]["t+0"])

    def test_no_causal_language_in_outputs(self):
        p = _payload(_rows("600031"))
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "a"}],
                       offsets=(0, 20))
        self.assertTrue(any("不是因果结论" in x for x in r["limits"]))
        self.assertTrue(any("不等于 alpha" in x for x in r["limits"]))
        self.assertNotIn("alpha", json.dumps(r["aggregate"]))
        self.assertNotIn("signal", json.dumps(r["readings"]))


class BindingAndReplayTest(unittest.TestCase):
    def test_fingerprint_changes_with_dataset(self):
        rows = _rows("600031")
        a = _payload(rows, dataset_id="ds-1")
        b = _payload(rows, dataset_id="ds-2")
        self.assertNotEqual(er.fingerprint(a), er.fingerprint(b))

    def test_same_input_same_hash(self):
        p = _payload(_rows("600031"))
        ev = [{"code": "600031", "date": "2025-04-18", "label": "a"}]
        r1 = er.compute(p, ev, offsets=(0, 5))
        r2 = er.compute(p, ev, offsets=(0, 5))
        self.assertEqual(r1["reading_hash"], r2["reading_hash"])
        self.assertEqual(r1["input_fingerprint"], r2["input_fingerprint"])

    def test_replay_detects_changed_input_without_rewriting(self):
        p = _payload(_rows("600031"))
        ev = [{"code": "600031", "date": "2025-04-18", "label": "a"}]
        r = er.compute(p, ev, offsets=(0, 5))
        old_hash = r["reading_hash"]
        p2 = _payload(_rows("600031", base=99.0), dataset_id="ds-1")
        out = er.replay(r, p2, ev)
        self.assertFalse(out["same"])
        self.assertTrue(out["diffs"])
        self.assertEqual(r["reading_hash"], old_hash, "replay 不得改写旧读数")

    def test_replay_same_input_is_same(self):
        p = _payload(_rows("600031"))
        ev = [{"code": "600031", "date": "2025-04-18", "label": "a"}]
        r = er.compute(p, ev, offsets=(0, 5))
        self.assertTrue(er.replay(r, p, ev)["same"])

    def test_reading_carries_input_kind_binding(self):
        p = _payload(_rows("600031"))
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "a"}])
        self.assertEqual(r["input_kind"]["input_kind"], "market_history")
        self.assertEqual(r["input_kind"]["dataset_id"], "ds-1")
        self.assertEqual(r["input_kind"]["adj_basis"], "前复权(qfq)")
        self.assertTrue(r["input_fingerprint"])


class LicenseGateTest(unittest.TestCase):
    def test_free_trial_and_personal_are_not_delivery_eligible(self):
        for lic in ("免费源·仅内部试验", "个人非商业", "", "unknown"):
            ok, why = er.delivery_eligible({"license": lic})
            self.assertFalse(ok, f"{lic!r} 不该可对外")
            self.assertTrue(why)

    def test_institutional_is_delivery_eligible(self):
        ok, why = er.delivery_eligible({"license": "机构授权"})
        self.assertTrue(ok)
        self.assertEqual(why, "")

    def test_reading_flags_delivery_ineligibility(self):
        p = _payload(_rows("600031"), license_="个人非商业")
        r = er.compute(p, [{"code": "600031", "date": "2025-04-18", "label": "a"}])
        self.assertFalse(r["delivery_eligible"])
        self.assertTrue(any("不得进对外下载包" in x for x in r["limits"]))


class RealDatasetTest(unittest.TestCase):
    """有真实数据集就跑真数据；没有就跳过（测试不依赖本机是否抓过数据）。"""

    def test_real_dataset_if_present(self):
        from adapters import market_history as mh
        payload = mh.load("")
        if payload.get("source") == "unavailable" or not payload.get("data"):
            self.skipTest("本机没有 market_history 数据集")
        ev = [{"code": "600031", "date": "2025-04-18", "label": "三一 2024 年报"},
              {"code": "002304", "date": "2024-04-27", "label": "洋河 2023 年报"},
              {"code": "002304", "date": "2025-04-29", "label": "洋河 2024 年报"}]
        r = er.compute(payload, ev, benchmark="000300", offsets=(0, 1, 5, 20))
        self.assertEqual(r["status"], "ok", r.get("reason"))
        self.assertEqual(len(r["readings"]), 3)
        for it in r["readings"]:
            self.assertGreater(it["t0_date"], it["event_date"])
        self.assertTrue(er.replay(r, payload, ev)["same"])
        self.assertFalse(r["delivery_eligible"])


if __name__ == "__main__":
    unittest.main()
