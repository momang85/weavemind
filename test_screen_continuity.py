# -*- coding: utf-8 -*-
"""BETA-1023 B3：持续研究的存储层测试（研究清单 / 规则模板 / 成本分账）。

覆盖任务书 §6 的四条：
1. 清单 + 备注：所有权按服务端作用域隔离、代码与备注长度边界、清单是观察对象不是持仓；
2. 重复同参不产生假变化 → 由 `execute` 复用保证（见 test_screen_store），这里只验清单不掺和结果；
3. 规则模板：只含规则/期间策略/版本/说明；导入只校验+预览，**不执行、不落盘**；
   畸形/超量/未知字段给中文错误；
4. 成本分账：请求数、缓存命中、确定性计算耗时、模型调用（恒 0）、外部费用 unknown。

全程 `WEAVEMIND_SCREEN_DIR` 指向临时目录，不碰生产持久根与真实缓存。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

import screen_store as st
import stock_screen as ss

SCOPE_A = {"user": "研究甲", "project": "personal"}
SCOPE_B = {"user": "研究乙", "project": "personal"}
RULES = [{"field": "gross_margin", "op": ">=", "value": 60},
         {"field": "debt_ratio", "op": "<=", "value": 50}]


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="weavemind_screen_b3_")
        self._saved = {k: os.environ.get(k) for k in
                       ("WEAVEMIND_SCREEN_DIR", "WEAVEMIND_SCREEN_CACHE")}
        os.environ["WEAVEMIND_SCREEN_DIR"] = os.path.join(self._tmp, "store")
        os.environ["WEAVEMIND_SCREEN_CACHE"] = os.path.join(self._tmp, "none.json")
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestWatchlist(_Case):
    """个人研究清单：观察对象、按作用域隔离、输入长度有界。"""

    def test_empty_when_never_saved(self):
        data, errors = st.load_watchlist(SCOPE_A)
        self.assertEqual(errors, [])
        self.assertEqual(data["items"], [])
        self.assertEqual(data["limits"]["max_note"], 500)
        self.assertEqual(data["schema"], st.WATCHLIST_SCHEMA)

    def test_add_note_remove_roundtrip_and_persistence(self):
        data, errors = st.watchlist_mutate(scope=SCOPE_A, action="add",
                                           code="002304.sz", name="洋河股份")
        self.assertEqual(errors, [])
        self.assertEqual([i["code"] for i in data["items"]], ["002304.SZ"])
        self.assertEqual(data["items"][0]["name"], "洋河股份")
        data, errors = st.watchlist_mutate(scope=SCOPE_A, action="note",
                                           code="002304.SZ", note="渠道库存要盯")
        self.assertEqual(errors, [])
        self.assertEqual(data["items"][0]["note"], "渠道库存要盯")
        again, _ = st.load_watchlist(SCOPE_A)          # 落盘后可重开
        self.assertEqual(again["items"][0]["note"], "渠道库存要盯")
        self.assertTrue(again["items"][0]["added_at"])
        data, errors = st.watchlist_mutate(scope=SCOPE_A, action="remove",
                                           code="002304.SZ")
        self.assertEqual(errors, [])
        self.assertEqual(data["items"], [])

    def test_add_is_idempotent_for_same_code(self):
        st.watchlist_mutate(scope=SCOPE_A, action="add", code="600519.SH", name="贵州茅台")
        data, _ = st.watchlist_mutate(scope=SCOPE_A, action="add",
                                      code="600519.SH", name="贵州茅台")
        self.assertEqual(len(data["items"]), 1)

    def test_bad_code_rejected(self):
        data, errors = st.watchlist_mutate(scope=SCOPE_A, action="add", code="600519")
        self.assertIsNone(data)
        self.assertIn("不合法", errors[0])
        self.assertIn("600519.SH", errors[0])

    def test_note_length_boundary(self):
        _data, errors = st.watchlist_mutate(scope=SCOPE_A, action="add",
                                            code="600519.SH", note="字" * 501)
        self.assertTrue(errors)
        self.assertIn("500", errors[0])
        data, errors = st.watchlist_mutate(scope=SCOPE_A, action="add",
                                           code="600519.SH", note="字" * 500)
        self.assertEqual(errors, [])
        self.assertEqual(len(data["items"][0]["note"]), 500)

    def test_watchlist_is_per_scope(self):
        st.watchlist_mutate(scope=SCOPE_A, action="add", code="600519.SH")
        other, errors = st.load_watchlist(SCOPE_B)
        self.assertEqual(errors, [])
        self.assertEqual(other["items"], [])           # 跨账号不可见

    def test_max_items_enforced(self):
        for i in range(200):
            code = f"{600000 + i:06d}.SH"
            data, errors = st.watchlist_mutate(scope=SCOPE_A, action="add", code=code)
            self.assertEqual(errors, [], (i, errors))
        _data, errors = st.watchlist_mutate(scope=SCOPE_A, action="add",
                                            code="601999.SH")
        self.assertTrue(errors)
        self.assertIn("最多 200", errors[0])

    def test_note_on_missing_code_errors(self):
        _data, errors = st.watchlist_mutate(scope=SCOPE_A, action="note",
                                            code="600519.SH", note="x")
        self.assertTrue(errors)
        self.assertIn("没有 600519.SH", errors[0])

    def test_unknown_action_rejected(self):
        _data, errors = st.watchlist_mutate(scope=SCOPE_A, action="promote",
                                            code="600519.SH")
        self.assertTrue(errors)
        self.assertIn("add / remove / note", errors[0])


class TestRulesTemplate(_Case):
    """规则模板：可分享的最小集合；导入只校验+预览，不执行。"""

    def _export(self, rules=None, policy=None, note=""):
        return st.export_rules_template(
            scope=SCOPE_A, rules_raw=RULES if rules is None else rules,
            period_policy_raw=policy, note=note)

    def test_roundtrip_export_then_import(self):
        data, errors = self._export(note="高毛利低负债")
        self.assertEqual(errors, [])
        template = data["template"]
        self.assertEqual(template["schema"], st.RULES_TEMPLATE_SCHEMA)
        normalized = [{"field": r["field"], "op": r["op"], "value": r["value"]}
                      for r in template["rules"]]
        self.assertEqual(normalized, RULES)
        self.assertEqual(template["period_policy"]["mode"], "uniform")
        imported, errors = st.import_rules_template(SCOPE_B, template)
        self.assertEqual(errors, [])
        self.assertEqual([(r["field"], r["op"], r["value"])
                          for r in imported["preview"]["rules"]],
                         [("gross_margin", ">=", 60.0), ("debt_ratio", "<=", 50.0)])
        self.assertEqual(imported["preview"]["rules_count"], 2)
        self.assertEqual(imported["warnings"], [])
        self.assertTrue(imported["note"].startswith("只做校验与预览"))

    def test_export_has_no_private_content(self):
        data, _errors = self._export()
        blob = json.dumps(data["template"], ensure_ascii=False)
        for leak in ("002304", "600519", "scope", "run_id", "dataset_id", "pool"):
            self.assertNotIn(leak, blob, f"模板不应含 {leak}")

    def test_export_requires_rules(self):
        data, errors = self._export(rules=[])
        self.assertIsNone(data)
        self.assertTrue(errors)
        self.assertIn("至少需要一条", errors[0])

    def test_export_rejects_bad_rule(self):
        data, errors = self._export(rules=[{"field": "net_profit", "op": ">=", "value": 1}])
        self.assertIsNone(data)
        self.assertTrue(errors)

    def test_export_rejects_percent_unit(self):
        data, errors = self._export(rules=[{"field": "gross_margin", "op": ">=",
                                            "value": 60, "unit": "%"}])
        self.assertIsNone(data)
        self.assertTrue(any("单位" in e for e in errors), errors)

    def test_import_rejects_private_keys(self):
        data, errors = self._export()
        template = dict(data["template"], pool=["002304.SZ"], run_id="abc")
        preview, errors = st.import_rules_template(SCOPE_B, template)
        self.assertIsNone(preview)
        self.assertIn("私人", errors[0])
        self.assertIn("pool", errors[0])

    def test_import_rejects_unknown_keys_and_schema(self):
        template = {"schema": "other/1", "rules": RULES}
        _p, errors = st.import_rules_template(SCOPE_B, template)
        self.assertIn("schema 不匹配", errors[0])
        template = {"schema": st.RULES_TEMPLATE_SCHEMA, "rules": RULES, "extra": 1}
        _p, errors = st.import_rules_template(SCOPE_B, template)
        self.assertIn("未知键", errors[0])

    def test_import_rejects_unknown_field_and_too_many(self):
        template = {"schema": st.RULES_TEMPLATE_SCHEMA,
                    "rules": [{"field": "net_profit", "op": ">=", "value": 1}]}
        _p, errors = st.import_rules_template(SCOPE_B, template)
        self.assertTrue(errors)
        self.assertIn("模板规则不合法", errors[0])
        many = {"schema": st.RULES_TEMPLATE_SCHEMA,
                "rules": [dict(RULES[0]) for _ in range(21)]}
        _p, errors = st.import_rules_template(SCOPE_B, many)
        self.assertIn("最多 20 条", errors[0])

    def test_import_rejects_bad_period_policy(self):
        template = {"schema": st.RULES_TEMPLATE_SCHEMA, "rules": RULES,
                    "period_policy": {"mode": "uniform", "unknown": 1}}
        _p, errors = st.import_rules_template(SCOPE_B, template)
        self.assertIn("未知键", errors[0])
        template = {"schema": st.RULES_TEMPLATE_SCHEMA, "rules": RULES,
                    "period_policy": {"mode": "uniform", "report_period": "2024"}}
        _p, errors = st.import_rules_template(SCOPE_B, template)
        self.assertIn("期间策略不合法", errors[0])

    def test_import_warns_on_version_mismatch(self):
        data, _ = self._export()
        template = dict(data["template"], impl_version="stock_screen/0.9.0",
                        field_map_version="0")
        preview, errors = st.import_rules_template(SCOPE_B, template)
        self.assertEqual(errors, [])
        joined = " ".join(preview["warnings"])
        self.assertIn("stock_screen/0.9.0", joined)
        self.assertIn("字段口径版本", joined)

    def test_import_does_not_run_or_write_results(self):
        data, _ = self._export()
        st.import_rules_template(SCOPE_B, data["template"])
        self.assertFalse(os.path.isdir(st.runs_root(SCOPE_B)))
        self.assertFalse(os.path.isdir(st.presets_root(SCOPE_B)))


class TestCostLedger(_Case):
    """成本分账：请求/缓存/计算/模型/外部费用分开记，不猜数字。"""

    def test_initial_ledger_is_explicit(self):
        cost, errors = st.load_cost(SCOPE_A)
        self.assertEqual(errors, [])
        self.assertEqual(cost["model_calls"], 0)
        self.assertEqual(cost["external_fee"], "unknown")
        self.assertEqual(cost["network_requests"], 0)
        self.assertEqual(cost["runs_total"], 0)

    def test_record_accumulates_and_persists(self):
        st.record_cost(SCOPE_A, prepare_requests=1, previews=2)
        st.record_cost(SCOPE_A, runs_total=1, compute_runs=1, compute_ms=37)
        st.record_cost(SCOPE_A, runs_total=1, cache_hits=1)
        cost, errors = st.load_cost(SCOPE_A)
        self.assertEqual(errors, [])
        self.assertEqual(cost["prepare_requests"], 1)
        self.assertEqual(cost["previews"], 2)
        self.assertEqual(cost["runs_total"], 2)
        self.assertEqual(cost["compute_runs"], 1)
        self.assertEqual(cost["compute_ms"], 37)
        self.assertEqual(cost["cache_hits"], 1)
        self.assertTrue(cost["updated_at"])

    def test_unknown_keys_are_ignored(self):
        st.record_cost(SCOPE_A, model_calls=5, bogus=9)
        cost, _ = st.load_cost(SCOPE_A)
        self.assertEqual(cost["model_calls"], 5)       # 允许记录（当前功能不该有）
        self.assertNotIn("bogus", cost)

    def test_ledger_per_scope(self):
        st.record_cost(SCOPE_A, runs_total=3)
        cost_b, _ = st.load_cost(SCOPE_B)
        self.assertEqual(cost_b["runs_total"], 0)


if __name__ == "__main__":
    unittest.main()
