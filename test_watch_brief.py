# -*- coding: utf-8 -*-
"""WATCH-BRIEF-1024 W2：自选变化简报（watch_brief）契约测试。

隔离：WEAVEMIND_SCREEN_DIR/WEAVEMIND_SCREEN_CACHE 全部指向临时 mkdtemp；
不碰生产根、不写审计、零模型/零外网（用桩断言生成期间无 URL 打开）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import screen_store as st
import stock_screen as ss
import watch_brief

SCOPE = {"user": "wb-user", "project": "wb"}
SCOPE_B = {"user": "wb-other", "project": "wb"}


def _row(code, period, *, avail, revenue, net_profit=10_000_000, **extra):
    base = {"entity": "合成公司", "code": code, "period": period, "unit": "元",
            "currency": "CNY", "caliber": "合并", "period_type": "年度",
            "source_url": "https://example.invalid/synthetic-wb",
            "available_at": avail, "disclosed_at": avail,
            "revenue": revenue, "net_profit": net_profit,
            "gross_profit": int(revenue * 0.3), "operating_cashflow": int(revenue * 0.1),
            "total_assets": revenue * 2, "total_liabilities": revenue}
    base.update(extra)
    return base


def _write_cache(path, rows):
    payload = {"schema": "screen_cache/1", "label": "wb-test", "official": False,
               "synthetic": True, "rows": rows}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


ROWS_A = [_row("600001.SH", "2023-12-31", avail="2024-04-20", revenue=100_000_000),
          _row("600001.SH", "2024-12-31", avail="2025-04-20", revenue=120_000_000)]
ROWS_B = [_row("600001.SH", "2023-12-31", avail="2024-04-20", revenue=100_000_000),
          _row("600001.SH", "2024-12-31", avail="2025-04-20", revenue=150_000_000)]
RULES_40 = [{"field": "revenue_growth", "op": ">", "value": 40}]
RULES_10 = [{"field": "revenue_growth", "op": ">", "value": 10}]


class BriefCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="weavemind_wb_test_")
        self._saved = {k: os.environ.get(k) for k in
                       ("WEAVEMIND_SCREEN_DIR", "WEAVEMIND_SCREEN_CACHE",
                        "WEAVEMIND_SCREEN_LEGACY_RUNS", "WEAVEMIND_SCREEN_LEGACY_PRESETS")}
        os.environ["WEAVEMIND_SCREEN_DIR"] = os.path.join(self._tmp, "store")
        self.cache_a = _write_cache(os.path.join(self._tmp, "a.json"), ROWS_A)
        self.cache_b = _write_cache(os.path.join(self._tmp, "b.json"), ROWS_B)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, *, cache=None, rules=None, scope=None, period=None, as_of="2026-01-31"):
        os.environ["WEAVEMIND_SCREEN_CACHE"] = cache or self.cache_b
        run, errors = st.execute(scope=scope or SCOPE, pool=["600001.SH"], as_of=as_of,
                                 rules_raw=rules or RULES_10,
                                 period_policy_raw=period or {"mode": "uniform"},
                                 source_path=cache or self.cache_b)
        self.assertEqual(errors, [], errors)
        return run

    def _load(self, run):
        loaded, _m, err = st.load_run(SCOPE, run["run_id"])
        self.assertIsNone(err)
        return loaded


class TestBriefContent(BriefCase):
    def test_comparable_change_lists_value_delta_and_rule_transition(self):
        base = self._load(self._run(cache=self.cache_a))
        current = self._load(self._run(cache=self.cache_b))
        brief, errors = watch_brief.build_brief(base, current,
                                                watchlist_items=[{"code": "600001.SH"}],
                                                watchlist_rev="rev-1")
        self.assertEqual(errors, [])
        self.assertEqual(brief["overview"]["data_changed"], 1)
        card = next(c for c in brief["cards"] if c["entity_id"] == "600001.SH")
        revenues = [v for v in card["value_changes"] if v["metric"] == "revenue"]
        self.assertTrue(revenues, card["value_changes"])
        changed = [v for v in revenues if v["change"] == "changed"][0]
        self.assertTrue(str(changed["period"]).startswith("2024"), changed["period"])
        self.assertAlmostEqual(changed["delta"], 30_000_000.0)
        self.assertIn("data_updated", card["change_kinds"])
        self.assertTrue(brief["identity"]["base"]["run_id"])
        self.assertEqual(brief["identity"]["watchlist_rev"], "rev-1")
        self.assertEqual(brief["cost"]["model_calls"], 0)

    def test_no_observed_change_does_not_fabricate(self):
        base = self._load(self._run(cache=self.cache_b))
        current = self._load(self._run(cache=self.cache_b, as_of="2026-02-01"))
        brief, errors = watch_brief.build_brief(base, current,
                                                watchlist_items=[{"code": "600001.SH"}])
        self.assertEqual(errors, [])
        card = next(c for c in brief["cards"] if c["entity_id"] == "600001.SH")
        self.assertTrue(card.get("no_observed_change"))
        self.assertEqual(card["value_changes"], [])
        self.assertEqual(brief["overview"]["unchanged"], 1)
        self.assertEqual(brief["overview"]["data_changed"], 0)

    def test_rule_change_is_not_reported_as_business_change(self):
        base = self._load(self._run(cache=self.cache_b, rules=RULES_40))
        current = self._load(self._run(cache=self.cache_b, rules=RULES_10))
        brief, errors = watch_brief.build_brief(base, current,
                                                watchlist_items=[{"code": "600001.SH"}])
        self.assertEqual(errors, [])
        card = next(c for c in brief["cards"] if c["entity_id"] == "600001.SH")
        self.assertTrue(card["value_changes"] == [])
        self.assertEqual([v for v in card["value_changes"] if v["change"] == "changed"], [],
                         "只改规则时不得出现数据变化")
        self.assertEqual(brief["comparability"]["flags"].get("rules_changed"), True)
        self.assertTrue(brief["overview"]["rules_changed"])
        self.assertEqual(brief["overview"]["data_changed"], 0)

    def test_period_switch_is_not_comparable(self):
        base = self._load(self._run(cache=self.cache_b, period={"mode": "pinned", "report_period": "2023"}))
        current = self._load(self._run(cache=self.cache_b, period={"mode": "pinned", "report_period": "2024"}))
        brief, errors = watch_brief.build_brief(base, current,
                                                watchlist_items=[{"code": "600001.SH"}])
        self.assertEqual(errors, [])
        self.assertTrue(brief["comparability"]["not_comparable_reasons"])
        flags = brief["comparability"]["flags"]
        self.assertTrue(flags.get("period_policy_changed"))

    def test_brief_id_is_idempotent_and_old_files_unchanged(self):
        base = self._load(self._run(cache=self.cache_a))
        current = self._load(self._run(cache=self.cache_b))
        path = os.path.join(st.runs_root(SCOPE), base["run_id"], "run.json")
        before = hashlib.sha256(open(path, "rb").read()).hexdigest()
        one, _ = watch_brief.build_brief(base, current)
        two, _ = watch_brief.build_brief(base, current)
        self.assertEqual(one["brief_id"], two["brief_id"])
        after = hashlib.sha256(open(path, "rb").read()).hexdigest()
        self.assertEqual(before, after, "简报不得改写旧结果文件")

    def test_generation_makes_no_network_or_model_calls(self):
        base = self._load(self._run(cache=self.cache_a))
        current = self._load(self._run(cache=self.cache_b))
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("不应联网")):
            brief, errors = watch_brief.build_brief(base, current)
        self.assertEqual(errors, [])
        self.assertEqual(brief["cost"]["model_calls"], 0)
        self.assertEqual(brief["cost"]["network_requests"],
                         "未知（本操作不发起外部请求）")


class TestBaseline(BriefCase):
    def test_set_baseline_and_revision_conflict(self):
        run = self._run(cache=self.cache_b)
        data, errors = st.watchlist_set_baseline(scope=SCOPE, run_token=run["run_id"])
        self.assertEqual(errors, [])
        self.assertEqual(data["baseline_run"], run["run_id"])
        rev = data["rev"]
        # 过期 rev → 409 语义（VersionConflict），不覆盖对方内容
        with self.assertRaises(st.VersionConflict):
            st.watchlist_set_baseline(scope=SCOPE, run_token=run["run_id"], if_rev="stale-rev")
        again, _ = st.load_watchlist(SCOPE)
        self.assertEqual(again["rev"], rev)
        cleared, errors = st.watchlist_set_baseline(scope=SCOPE, clear=True, if_rev=rev)
        self.assertEqual(errors, [])
        self.assertEqual(cleared["baseline_run"], "")

    def test_other_scope_run_cannot_be_baseline(self):
        other = self._run(cache=self.cache_b, scope=SCOPE_B)
        _data, errors = st.watchlist_set_baseline(scope=SCOPE, run_token=other["run_id"])
        self.assertTrue(errors, "跨作用域记录不得设为基准")
        self.assertIn("不存在", "".join(errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
