# -*- coding: utf-8 -*-
"""独立交易日历：把**停牌**与**休市**分开（退出关卡）。

断言按"缺日历会算错什么"逐条来：
- **覆盖区间外一律 `unknown`（None）**：不把"日历太旧/没查到"说成"休市"；
- 停牌 = **日历判定为交易日、标的当日无行情** ⇒ 有日历时是**结论**，无日历时只能报**疑似**；
- `t±N` 按**日历**定位：标的停牌不会让窗口静默错位；该点记 `missing` 并写明"不是休市"；
- `pending` 与 `missing` 分开：超出**行情数据覆盖**⇒待成熟，落在覆盖内却无行情⇒停牌；
- **不传日历时行为完全不变**（零存量影响，旧结论不被悄悄改写）；
- 日历数据带来源/许可，缺数据 `unavailable`（**不返回"全都不开市"的空日历**）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from adapters import trading_calendar as tc  # noqa: E402
from quant_research import event_returns as er  # noqa: E402

# 一段小日历：跨越一个周末与一个"长假"（10-01~10-03 休市）；10 月多给些交易日，
# 便于构造"日历知道那天是交易日、但行情数据还没覆盖到"的 pending 场景。
SESSIONS = ["2025-09-01", "2025-09-02", "2025-09-03", "2025-09-04", "2025-09-05",
            "2025-09-08", "2025-09-09", "2025-09-10", "2025-09-11", "2025-09-12",
            "2025-10-06", "2025-10-07", "2025-10-08", "2025-10-09", "2025-10-10",
            "2025-10-13", "2025-10-14", "2025-10-15", "2025-10-16", "2025-10-17",
            "2025-10-20", "2025-10-21", "2025-10-22", "2025-10-23", "2025-10-24",
            "2025-10-27", "2025-10-28", "2025-10-29", "2025-10-30", "2025-10-31"]


def _cal():
    return {"schema": tc.SCHEMA, "dataset_id": "cal-1", "source": "test",
            "license": "免费源·仅内部试验", "market": "CN", "range": [SESSIONS[0], SESSIONS[-1]],
            "rows": len(SESSIONS), "sessions": list(SESSIONS), "unavailable": []}


def _row(code, date, close, o=None):
    return {"code": code, "date": date, "open": o if o is not None else close,
            "close": close, "available_at": f"{date}T15:00:00+08:00", "source": "test"}


def _payload(rows):
    return {"schema": "weavemind.market_history/0", "dataset_id": "ds-1", "source": "test",
            "license": "免费源·仅内部试验", "adj_basis": "前复权(qqf)".replace("qqf", "qfq"),
            "rows": len(rows), "instruments": sorted({r["code"] for r in rows}), "data": rows}


class QueryTest(unittest.TestCase):
    def test_unknown_outside_coverage(self):
        c = _cal()
        self.assertTrue(tc.is_session(c, "2025-09-05"))
        self.assertFalse(tc.is_session(c, "2025-10-01"))
        self.assertIsNone(tc.is_session(c, "2030-01-01"), "覆盖外必须 unknown，不得当休市")
        self.assertIsNone(tc.is_session(c, "1990-01-01"))
        self.assertFalse(tc.covers(c, "2030-01-01"))

    def test_no_calendar_is_unavailable_not_empty(self):
        p = tc.load("does-not-exist")
        self.assertEqual(p["source"], "unavailable")
        self.assertTrue(p["reason"])
        self.assertIsNone(tc.is_session(p, "2025-09-05"))
        self.assertFalse(tc.ok(p))

    def test_next_prev_and_offsets(self):
        c = _cal()
        self.assertEqual(tc.next_session(c, "2025-09-05"), "2025-09-08")
        self.assertEqual(tc.prev_session(c, "2025-09-05"), "2025-09-05")
        self.assertEqual(tc.prev_session(c, "2025-09-06"), "2025-09-05")
        self.assertEqual(tc.session_at_offset(c, "2025-09-05", 1), "2025-09-08")
        self.assertEqual(tc.session_at_offset(c, "2025-09-05", -1), "2025-09-04")
        self.assertIsNone(tc.session_at_offset(c, "2025-09-05", 99))
        self.assertEqual(tc.sessions_between(c, "2025-09-05", "2025-09-12"), 5,
                         "(左开右闭]：09-08/09/10/11/12 共 5 个")

    def test_sessions_between_none_when_coverage_missing(self):
        self.assertIsNone(tc.sessions_between(_cal(), "2025-09-01", "2030-01-01"))


class SuspendVerdictTest(unittest.TestCase):
    def test_suspension_is_definitive_with_calendar(self):
        c = _cal()
        subject = ["2025-09-01", "2025-09-02", "2025-09-08"]      # 缺 09-03/04/05
        v = tc.suspend_dates(c, subject, "2025-09-01", "2025-09-08")
        self.assertTrue(v["ok"])
        self.assertEqual(v["suspension_dates"], ["2025-09-03", "2025-09-04", "2025-09-05"])
        self.assertIn("停牌", v["basis"])

    def test_range_outside_coverage_refuses_to_conclude(self):
        v = tc.suspend_dates(_cal(), [], "2030-01-01", "2030-02-01")
        self.assertFalse(v["ok"])
        self.assertTrue(v["unavailable"])
        self.assertIn("区间外不下", v["reason"])

    def test_no_calendar_refuses_to_conclude(self):
        v = tc.suspend_dates(None, [], "2025-09-01", "2025-09-05")
        self.assertFalse(v["ok"])
        self.assertIn("无法把停牌与休市分开", v["reason"])


class ImportLoadTest(unittest.TestCase):
    def test_import_roundtrip_and_license_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "cal.csv"
            raw.write_text("date,session\n" + "\n".join(f"{d},1" for d in SESSIONS),
                           encoding="utf-8")
            old = os.environ.get("WEAVEMIND_CALENDAR_DIR")
            os.environ["WEAVEMIND_CALENDAR_DIR"] = str(Path(td) / "store")
            try:
                r = tc.import_file(raw, source="test/sina")
                self.assertTrue(r["ok"])
                self.assertEqual(r["meta"]["rows"], len(SESSIONS))
                self.assertEqual(r["meta"]["range"], [SESSIONS[0], SESSIONS[-1]])
                self.assertEqual(r["meta"]["license"], tc.LICENSE_FREE_TRIAL)
                p = tc.load(r["dataset_id"])
                self.assertTrue(tc.ok(p))
                self.assertEqual(p["sessions"], SESSIONS)
            finally:
                if old is None:
                    os.environ.pop("WEAVEMIND_CALENDAR_DIR", None)
                else:
                    os.environ["WEAVEMIND_CALENDAR_DIR"] = old

    def test_missing_date_column_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "bad.csv"
            raw.write_text("day,session\n2025-09-01,1\n", encoding="utf-8")
            self.assertFalse(tc.import_file(raw, source="x")["ok"])

    def test_empty_calendar_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "empty.csv"
            raw.write_text("date,session\n", encoding="utf-8")
            self.assertFalse(tc.import_file(raw, source="x")["ok"])


class EventReturnsWithCalendarTest(unittest.TestCase):
    def _rows(self):
        # 标的缺 2025-09-05（锚点侧的停牌）与 2025-09-09（**窗口内**停牌）；
        # 行情数据覆盖到 2025-09-12。
        subj = [_row("600031", d, 10.0 + i * 0.1)
                for i, d in enumerate(SESSIONS)
                if d <= "2025-09-12" and d not in ("2025-09-05", "2025-09-09")]
        bench = [_row("000300", d, 100.0 + i * 0.01)
                 for i, d in enumerate(SESSIONS) if d <= "2025-09-12"]
        return _payload(subj + bench)

    def test_offsets_are_calendar_anchored_and_suspension_is_missing(self):
        """窗口内停牌 ⇒ 该点 `missing` 且写明"不是休市"；`t±N` 仍按日历真实交易日走。"""
        p = self._rows()
        ev = [{"code": "600031", "date": "2025-09-05", "label": "e"}]
        r = er.compute(p, ev, benchmark="000300", offsets=(0, 1, 2), calendar=_cal())
        it = r["readings"][0]
        # 约定：t0 ＝ 截点**之后**第一个交易日（截点当日只作价格起点）⇒ 这里是 09-08
        self.assertEqual(it["anchor"]["date"], "2025-09-04")
        self.assertEqual(it["t0_date"], "2025-09-08")
        p0 = next(x for x in it["points"] if x["offset"] == 0)
        self.assertEqual((p0["date"], p0["state"]), ("2025-09-08", "observed"))
        # t+1 ＝ 09-09：日历确认是交易日、标的当日无行情 ⇒ 停牌（不是休市），且**日期要带出来**
        p1 = next(x for x in it["points"] if x["offset"] == 1)
        self.assertEqual(p1["date"], "2025-09-09")
        self.assertEqual(p1["state"], "missing")
        self.assertIn("交易日历确认", p1["unavailable"])
        self.assertIn("不是休市", p1["unavailable"])
        self.assertIsNone(p1["return"])
        # t+2 恢复交易
        p2 = next(x for x in it["points"] if x["offset"] == 2)
        self.assertEqual((p2["date"], p2["state"]), ("2025-09-10", "observed"))
        # 两处停牌都进了"确证"清单
        self.assertEqual(it["suspension_dates"], ["2025-09-05", "2025-09-09"])
        self.assertTrue(it["suspension_certain"])

    def test_pending_when_beyond_market_data_coverage(self):
        """日历知道那天是交易日、但**行情数据还没覆盖到** ⇒ `pending`（不是 `missing`）。"""
        p = self._rows()                                  # 行情只到 2025-09-12
        ev = [{"code": "600031", "date": "2025-09-01", "label": "e"}]
        r = er.compute(p, ev, benchmark="000300", offsets=(0, 20), calendar=_cal())
        pt = next(x for x in r["readings"][0]["points"] if x["offset"] == 20)
        self.assertGreater(pt["date"], "2025-09-12")
        self.assertEqual(pt["state"], "pending")
        self.assertIn("窗口未满", pt["unavailable"])
        self.assertIsNone(pt["return"])

    def test_offset_beyond_calendar_coverage_is_pending_not_missing(self):
        """偏移超出**日历覆盖**时不能叫 `missing`（那会暗示停牌）：如实记"无法确定"，按未成熟处理。"""
        p = self._rows()
        ev = [{"code": "600031", "date": "2025-09-01", "label": "e"}]
        r = er.compute(p, ev, benchmark="000300", offsets=(0, 400), calendar=_cal())
        pt = next(x for x in r["readings"][0]["points"] if x["offset"] == 400)
        self.assertEqual(pt["state"], "pending")
        self.assertIn("交易日历覆盖", pt["unavailable"])
        self.assertIsNone(pt["date"])

    def test_same_numbers_with_and_without_calendar_when_no_suspension(self):
        """无停牌时两种口径必须同数字——否则"加日历"会悄悄改写旧结论。"""
        subj = [_row("600031", d, 10.0 + i * 0.1)
                for i, d in enumerate(SESSIONS) if d <= "2025-09-12"]
        bench = [_row("000300", d, 100.0 + i * 0.01)
                 for i, d in enumerate(SESSIONS) if d <= "2025-09-12"]
        p = _payload(subj + bench)
        ev = [{"code": "600031", "date": "2025-09-01", "label": "e"}]
        a = er.compute(p, ev, benchmark="000300", offsets=(0, 1, 5))
        b = er.compute(p, ev, benchmark="000300", offsets=(0, 1, 5), calendar=_cal())
        fa = [(x["offset"], x["date"], x["excess_return"]) for x in a["readings"][0]["points"]]
        fb = [(x["offset"], x["date"], x["excess_return"]) for x in b["readings"][0]["points"]]
        self.assertEqual(fa, fb)

    def test_without_calendar_says_so_and_keeps_suspect_only(self):
        p = self._rows()
        ev = [{"code": "600031", "date": "2025-09-05", "label": "e"}]
        r = er.compute(p, ev, benchmark="000300", offsets=(0,), calendar=None)
        it = r["readings"][0]
        self.assertFalse(it["suspension_certain"])
        self.assertIn("无独立交易日历", it["suspension_basis"])
        self.assertIn("无交易日历", r["params"]["calendar"])
        # 无日历时 t0 会"滑"到下一根可用 K 线（旧行为），这里如实锁住而不是假装正确
        p0 = next(x for x in it["points"] if x["offset"] == 0)
        self.assertEqual(p0["date"], "2025-09-08")
        self.assertEqual(p0["state"], "observed")


class BacktestWithCalendarTest(unittest.TestCase):
    """回测侧：日历把"静默跳过的再平衡"变成"看得见的停牌拒绝"。"""

    def _bt_rows(self):
        # 标的缺 **2025-10-06**（10 月第一个交易日）——正是再平衡日
        subj = [_row("600031", d, 10.0, o=10.0) for d in SESSIONS if d != "2025-10-06"]
        return _payload(subj)

    def test_without_calendar_the_rebalance_is_silently_skipped(self):
        from quant_research import backtest as bt
        r = bt.run(self._bt_rows(), {"universe": ["600031"], "rebalance": "monthly",
                                     "initial_cash": 100000.0},
                   license="免费源·仅内部试验")           # 无基准、无日历
        self.assertIn("标的并集", r["calendar_basis"])
        self.assertFalse([x for x in r["rejections"] if x["date"] == "2025-10-06"],
                         "无日历时该日根本不在日历里 ⇒ 连拒绝记录都没有（静默跳过）")

    def test_with_calendar_the_suspension_is_recorded_not_skipped(self):
        from quant_research import backtest as bt
        r = bt.run(self._bt_rows(), {"universe": ["600031"], "rebalance": "monthly",
                                     "initial_cash": 100000.0},
                   license="免费源·仅内部试验", calendar=_cal())
        self.assertIn("独立交易日历", r["calendar_basis"])
        rej = [x for x in r["rejections"] if x["date"] == "2025-10-06"]
        self.assertTrue(rej, "有日历时该次再平衡必须留下拒绝记录")
        self.assertIn("停牌", rej[0]["reason"])
        self.assertIn("交易日历确认", rej[0]["reason"])
        self.assertEqual(r["calendar"]["sessions_in_span"], len(SESSIONS))

    def test_calendar_does_not_change_numbers_without_suspension(self):
        from quant_research import backtest as bt
        rows = _payload([_row("600031", d, 10.0, o=10.0) for d in SESSIONS])
        spec = {"universe": ["600031"], "rebalance": "monthly", "initial_cash": 100000.0}
        a = bt.run(rows, spec, license="免费源·仅内部试验")
        b = bt.run(rows, spec, license="免费源·仅内部试验", calendar=_cal())
        self.assertEqual(a["metrics"]["total_return"], b["metrics"]["total_return"])
        self.assertEqual(a["metrics"]["orders"], b["metrics"]["orders"])
        self.assertTrue(b["audit"]["ok"], b["audit"]["failed"])


if __name__ == "__main__":
    unittest.main()
