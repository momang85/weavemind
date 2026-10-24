# -*- coding: utf-8 -*-
"""TRIAL-READY-1024 · T0 计算一致性回归（后端 G1–G4）。

逐条钉住 `docs/evidence/20261024-trial-ready/architect_probes.py` 的 G1–G4 反例
（探针文件只读，本文件不改它、也不依赖它跑）：

- G1 同比只认**严格相邻的上一个自然年**：只有 2022 与 2024 时必须报「缺上一年度
  2023-…」，不能给出看起来正常的 +50%（`stock_screen.judge_growth` 单点判据）；
- G2 预览与执行**同一条**可计算判据：资产总计=0 的行在预览里也不能是「已覆盖」；
- G3 预览绑定**与执行同一份输入**（`dataset_id`／官方缓存），不跨数据集拼行；
- G4 适配器在源响应缺 `CURRENCY` 时保持**未知**，不默认 CNY/HKD，且全精度元金额不受影响。

隔离：`tempfile.mkdtemp(prefix="t0-")` 下建全部路径，env 先设、模块后导入（与探针
同一做法）；离线合成数据、mock 传输、不联网、不写仓库数据目录、不碰 8080。

运行：`C:/Python314/python.exe -m unittest test_trial_t0`
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_TMP = tempfile.mkdtemp(prefix="t0-")
atexit.register(shutil.rmtree, _TMP, True)

_SETTINGS = {
    "WEAVEMIND_TEST_MODE": "1",
    "WEAVEMIND_DATA_DIR": os.path.join(_TMP, "data"),
    "WEAVEMIND_LOG_DIR": os.path.join(_TMP, "logs"),
    "WEAVEMIND_SCREEN_DIR": os.path.join(_TMP, "screen"),
    "WEAVEMIND_SCREEN_CACHE": os.path.join(_TMP, "cache.json"),
    "WEAVEMIND_WORKSPACE_ROOT": os.path.join(_TMP, "workspace"),
    "MEMORY_DIR": os.path.join(_TMP, "memory"),
    "AGENTS_DB": os.path.join(_TMP, "agents.db"),
}
os.environ.update(_SETTINGS)

import screen_prepare as sp                  # noqa: E402  （env 先设再导入）
import screen_store as st                    # noqa: E402
import stock_screen as ss                    # noqa: E402
from adapters import eastmoney               # noqa: E402

SCOPE = {"user": "t0-synthetic", "project": "personal"}
CODE = "600031.SH"
AS_OF = "2026-10-24"
CACHE = _SETTINGS["WEAVEMIND_SCREEN_CACHE"]


# ── 合成输入（非真实财务数据）──────────────────────────────────────────────
def _row(year, **changes):
    result = {
        "entity": "合成复核公司（非真实财务数据）", "code": CODE,
        "period": f"{year}-12-31", "period_type": "年报",
        "revenue": 100, "gross_profit": 30, "net_profit": 10,
        "operating_cashflow": 12, "total_assets": 200, "total_liabilities": 60,
        "unit": "元", "currency": "CNY", "caliber": "合并",
        "available_at": f"{year + 1}-04-20", "disclosed_at": f"{year + 1}-04-20",
        "source_url": "https://example.invalid/t0-synthetic",
    }
    result.update(changes)
    return result


def _write_cache(rows):
    with open(CACHE, "w", encoding="utf-8") as fh:
        json.dump({"metadata": {"synthetic": True}, "rows": rows}, fh,
                  ensure_ascii=False)


def _save_dataset(name, rows):
    metadata, _reused, errors = st.save_dataset_from_source(
        scope=SCOPE, name=name, rows=rows,
        provenance={"kind": "synthetic_demo", "synthetic": True, "label": name},
    )
    if errors:
        raise AssertionError(errors)
    return metadata["dataset_id"]


def _execute(field, threshold=0, dataset_id=None, as_of=AS_OF, period="2024"):
    result, errors = st.execute(
        scope=SCOPE, pool=[CODE], as_of=as_of,
        rules_raw=[{"field": field, "op": ">", "value": threshold}],
        period_policy_raw={"mode": "pinned", "report_period": period},
        dataset_id=dataset_id, store=False,
    )
    if errors:
        raise AssertionError(f"执行报错：{errors}")
    return result


def _plan(dataset_id=None, period="2024", as_of=None):
    plan = sp.prepare_plan(SCOPE, [CODE], [period], dataset_id=dataset_id,
                           as_of=as_of)
    if plan.get("errors"):
        raise AssertionError(f"预览报错：{plan['errors']}")
    return plan, plan["items"][0]["periods"][0]


def _rows(run):
    out = []
    for bucket in ("pass", "fail", "insufficient"):
        out.extend(run["rows"].get(bucket) or [])
    return out


def _reading(run, field):
    for row in _rows(run):
        for reading in row.get("readings") or []:
            if reading.get("field") == field:
                return reading, row
    raise AssertionError(f"读数里没有 {field}")


# ── G1：相邻年度 ───────────────────────────────────────────────────────────
class G1AdjacentYearGrowth(unittest.TestCase):
    def test_non_adjacent_years_are_not_yoy(self):
        """探针 G1 复现：2022 收入 100、2024 收入 150、**没有 2023**。

        旧实现按 manifest 相邻位取上一期 → 2022 当"上一年" → 同比 +50% 且 pass。
        """
        _write_cache([_row(2022), _row(2024, revenue=150)])
        run = _execute("revenue_growth", 40)
        self.assertEqual(run["counts"]["pass"], 0, "跨年度增速不得判通过")
        self.assertEqual(run["counts"]["insufficient"], 1)
        reading, row = _reading(run, "revenue_growth")
        self.assertEqual(reading["status"], "missing")
        self.assertIn("缺上一年度 2023年", reading["reason"])
        self.assertIsNone(reading["value"], "不给 50.0 这种看起来正常的百分比")
        self.assertEqual(row["verdict"], "insufficient")

    def test_adjacent_years_do_compute_yoy(self):
        """正控：2023 收入 100、2024 收入 150 → 同比 50%（真相邻才给数）。"""
        _write_cache([_row(2023), _row(2024, revenue=150)])
        run = _execute("revenue_growth", 40)
        reading, _row_ = _reading(run, "revenue_growth")
        self.assertEqual(reading["status"], "ok")
        self.assertAlmostEqual(reading["value"], 50.0)
        self.assertEqual(run["counts"]["pass"], 1)
        self.assertIn("2023年", reading["formula"])

    def test_single_period_reports_missing_prior_year(self):
        """只有 2024 一年：说缺 2023，不退回"更早年度"也不当前值补齐。"""
        _write_cache([_row(2024)])
        run = _execute("revenue_growth", 0)
        reading, _row_ = _reading(run, "revenue_growth")
        self.assertEqual(reading["status"], "missing")
        self.assertIn("缺上一年度 2023年", reading["reason"])

    def test_non_positive_base_is_not_applicable(self):
        """上一年度为 0 / 负数：增速不适用（不编造比值）。"""
        for prior_revenue in (0, -50):
            with self.subTest(prior_revenue=prior_revenue):
                _write_cache([_row(2023, revenue=prior_revenue),
                              _row(2024, revenue=150)])
                run = _execute("revenue_growth", 0)
                reading, _row_ = _reading(run, "revenue_growth")
                self.assertEqual(reading["status"], "not_applicable")
                self.assertIn("非正", reading["reason"])

    def test_cross_currency_years_conflict(self):
        """两个年度币种不同：报冲突，不换算、不猜汇率。"""
        _write_cache([_row(2023, currency="HKD"), _row(2024, revenue=150)])
        run = _execute("revenue_growth", 0)
        reading, _row_ = _reading(run, "revenue_growth")
        self.assertEqual(reading["status"], "conflicting")
        self.assertIn("币种不一致", reading["reason"])

    def test_preview_says_missing_prior_year_too(self):
        """预览同判据：官方缓存里只有 2022+2024 → 同比不可算（G1×G2）。"""
        _write_cache([_row(2022), _row(2024, revenue=150)])
        _plan_result, cell = _plan()
        growth = cell["fields"]["revenue_growth"]
        self.assertEqual(growth["status"], "missing")
        self.assertIn("缺上一年度 2023-12-31", growth["reason"])
        self.assertFalse(cell["computable"])
        self.assertNotEqual(cell["status"], "covered")


# ── G2：预览与执行同一条可计算判据 ─────────────────────────────────────────
class G2SharedComputability(unittest.TestCase):
    def test_zero_denominator_not_computable_in_both_views(self):
        """探针 G2 复现：资产总计=0 → 预览不可算、执行资料不足（同一状态词）。"""
        _write_cache([_row(2023), _row(2024, total_assets=0)])
        _plan_result, cell = _plan()
        preview = cell["fields"]["debt_ratio"]
        self.assertEqual(preview["status"], "not_applicable")
        self.assertIn("分母为 0", preview["reason"])
        self.assertFalse(cell["computable"])
        run = _execute("debt_ratio")
        reading, _row_ = _reading(run, "debt_ratio")
        self.assertEqual(reading["status"], preview["status"],
                         "预览与执行必须给出同一状态（判据只有一条）")
        self.assertIn("分母为 0", reading["reason"])
        self.assertEqual(run["counts"]["insufficient"], 1)

    def test_preview_and_execution_agree_on_every_field_shape(self):
        """同一份输入下，五个字段的预览状态与执行读数状态逐条相同。"""
        cases = [
            ("debt_ratio", {"total_assets": 0}),
            ("gross_margin", {"revenue": 0}),
            ("cashflow_coverage", {"net_profit": 0}),
            ("operating_cashflow", {"operating_cashflow": None}),
            ("revenue_growth", {"revenue": 150}),
        ]
        for field, changes in cases:
            with self.subTest(field=field, changes=changes):
                rows = [_row(2023), _row(2024, **changes)]
                _write_cache(rows)
                _plan_result, cell = _plan()
                run = _execute(field)
                reading, _row_ = _reading(run, field)
                self.assertEqual(cell["fields"][field]["status"],
                                 reading["status"],
                                 f"{field}：预览与执行状态不一致")

    def test_row_without_declared_currency_is_unknown_in_both_views(self):
        """缺币种不是"默认人民币"：预览与执行都按 unknown 处理。"""
        _write_cache([_row(2023), _row(2024, currency="")])
        _plan_result, cell = _plan()
        preview = cell["fields"]["debt_ratio"]
        self.assertEqual(preview["status"], "unknown")
        self.assertIn("币种", preview["reason"])
        run = _execute("debt_ratio")
        reading, _row_ = _reading(run, "debt_ratio")
        self.assertEqual(reading["status"], "unknown")
        self.assertEqual(run["counts"]["insufficient"], 1)

    def test_judges_are_the_single_entry(self):
        """判据单点：`judge_ratio`/`judge_growth` 直接调用即给出与执行同一套词。"""
        num = ss.value_from_row(_row(2024), "total_liabilities")
        zero_den = ss.ComputableValue(metric="total_assets", period="2024-12-31",
                                      state=ss.State.ZERO, yuan=Decimal("0"),
                                      currency="CNY", caliber="合并", unit="元")
        state, reason = ss.judge_ratio(num, zero_den, label="资产负债率")
        self.assertEqual(state, "not_applicable")
        self.assertIn("分母为 0", reason)
        cur = ss.value_from_row(_row(2024, revenue=150), "revenue")
        old = ss.value_from_row(_row(2022), "revenue")
        state, reason = ss.judge_growth(cur, old)
        self.assertEqual(state, "not_applicable")
        self.assertIn("不是相邻年度", reason)
        missing = ss.value_from_row(_row(2024, revenue=None), "revenue")
        self.assertEqual(missing.state, ss.State.MISSING)
        unknown = ss.value_from_row(_row(2024, unit="手"), "revenue")
        self.assertEqual(unknown.state, ss.State.UNKNOWN)
        self.assertIn("单位", unknown.note)

    def test_invalid_value_is_rejected_not_guessed(self):
        """非数值：预览与执行都点名「不是数」，主体判资料不足（两边都不猜）。"""
        _write_cache([_row(2023), _row(2024, revenue="不是数")])
        _plan_result, cell = _plan()
        for field in ("gross_margin", "revenue_growth"):
            self.assertEqual(cell["fields"][field]["status"], "invalid", field)
            self.assertIn("不是数", cell["fields"][field]["reason"], field)
        self.assertEqual(cell["fields"]["debt_ratio"]["status"], "ok",
                         "无效只影响涉事字段：不拖累无关字段，也不掩盖缺口")
        self.assertNotEqual(cell["status"], "covered")
        run = _execute("revenue_growth", 0)
        reading, row = _reading(run, "revenue_growth")
        self.assertEqual(reading["status"], "invalid")
        self.assertIn("不是数", reading["reason"])
        self.assertEqual(row["verdict"], "insufficient")
        # 直读字段也一样：值无效 → 该字段不给数，主体判不足
        run2 = _execute("operating_cashflow", 0)
        self.assertEqual(_reading(run2, "operating_cashflow")[0]["status"], "ok")


# ── G3：预览绑定与执行同一份输入 ───────────────────────────────────────────
class G3PreviewBinding(unittest.TestCase):
    def test_preview_does_not_splice_separate_datasets(self):
        """探针 G3 复现：2023 在数据集 A、2024 在数据集 B；绑定 B 时同比不可算。"""
        _write_cache([])
        id_a = _save_dataset("t0-source-a", [_row(2023)])
        id_b = _save_dataset("t0-source-b", [_row(2024, revenue=150)])
        plan, cell = _plan(dataset_id=id_b)
        self.assertEqual(plan["input_binding"]["dataset_id"], id_b)
        growth = cell["fields"]["revenue_growth"]
        self.assertEqual(growth["status"], "missing")
        self.assertIn("缺上一年度 2023-12-31", growth["reason"])
        self.assertNotIn("previous_period", growth)
        run = _execute("revenue_growth", 40, dataset_id=id_b)
        self.assertEqual(run["counts"]["insufficient"], 1)
        # 把两年放进同一份数据集 → 预览与执行同时转为可算/通过
        id_both = _save_dataset("t0-source-both", [_row(2023), _row(2024, revenue=150)])
        _plan2, cell2 = _plan(dataset_id=id_both)
        self.assertEqual(cell2["fields"]["revenue_growth"]["status"], "ok")
        run2 = _execute("revenue_growth", 40, dataset_id=id_both)
        self.assertEqual(run2["counts"]["pass"], 1)
        self.assertNotEqual(id_a, id_b)

    def test_materials_inventory_is_not_the_execution_view(self):
        """「我有什么材料」可列多个数据集；「这次能算什么」只认选定输入。"""
        _write_cache([])
        _save_dataset("t0-inventory-a", [_row(2022)])
        _save_dataset("t0-inventory-b", [_row(2023), _row(2024)])
        inventory = sp.materials_projection(SCOPE)
        self.assertGreaterEqual(len(inventory["datasets"]), 2,
                                "库存视图列全部数据集")
        plan, _cell_ = _plan(dataset_id=_save_dataset("t0-inventory-c", [_row(2024)]))
        self.assertEqual(plan["input_binding"]["periods"], ["2024-12-31"],
                         "计算视图只认绑定输入的期间")

    def test_as_of_pit_agrees_between_preview_and_execution(self):
        """同一截止日：预览标出"晚于截止日不纳入"，执行同样拒绝。"""
        _write_cache([_row(2024, available_at="2027-04-20", disclosed_at="2027-04-20")])
        _plan_result, cell = _plan(as_of=AS_OF)
        self.assertEqual(cell["pit"]["state"], "excluded")
        self.assertIn("晚于截止日", cell["pit"]["reason"])
        self.assertFalse(cell["computable"])
        self.assertNotEqual(cell["status"], "covered")
        result, errors = st.execute(
            scope=SCOPE, pool=[CODE], as_of=AS_OF,
            rules_raw=[{"field": "debt_ratio", "op": ">", "value": 0}],
            period_policy_raw={"mode": "pinned", "report_period": "2024"},
            store=False,
        )
        self.assertIsNone(result, "晚于截止日的行执行侧不得纳入")
        self.assertTrue(errors)

    def test_without_as_of_pit_is_not_pretended(self):
        """不给 as_of 就明说"没判 PIT"，不假装判过。"""
        _write_cache([_row(2023), _row(2024)])
        plan, cell = _plan()
        self.assertEqual(cell["pit"]["state"], "not_evaluated")
        self.assertIn("未提供 as_of", plan["input_binding"]["pit_note"])


# ── G4：币种不默认 ─────────────────────────────────────────────────────────
class G4AdapterCurrency(unittest.TestCase):
    _HK_ROW = {
        "REPORT_DATE": "2024-12-31", "REPORT_TYPE": "年报", "NOTICE_DATE": "2025-04-20",
        "OPERATE_INCOME": 1000000000, "HOLDER_PROFIT": 200000000,
        "GROSS_PROFIT": 300000000, "TOTAL_ASSETS": 2000000000,
        "TOTAL_LIABILITIES": 600000000, "NETCASH_OPERATE": 120000000,
    }
    _A_ROW = {
        "REPORT_DATE": "2024-12-31", "REPORT_TYPE": "年报", "NOTICE_DATE": "2025-04-20",
        "SECUCODE": f"{CODE}", "SECURITY_CODE": CODE.split(".")[0],
        "SECURITY_NAME_ABBR": "合成公司",
        "TOTALOPERATEREVE": 123456789, "PARENTNETPROFIT": 12345678,
        "TOTAL_ASSETS_PK": 2000000000, "LIABILITY": 600000000,
        "NETCASH_OPERATE_PK": 120000000, "MLR": 30000000,
    }

    def _collect(self, raw_row):
        response = json.dumps({"success": True, "result": {"data": [dict(raw_row)]}})
        with patch.object(eastmoney, "_get", return_value=response):
            collected, errors = sp.collect(
                scope=SCOPE, source_id="adapter:eastmoney", codes=[CODE],
                periods=["2024"],
            )
        if errors:
            raise AssertionError(errors)
        dataset_id = collected["dataset"]["dataset_id"]
        _metadata, payload, errors = st.load_dataset(SCOPE, dataset_id)
        if errors:
            raise AssertionError(errors)
        return dataset_id, payload

    def test_missing_currency_stays_unknown(self):
        """探针 G4 复现：源响应无 CURRENCY → 存空串未知，不写 CNY。"""
        dataset_id, payload = self._collect(self._A_ROW)
        row = payload["rows"][0]
        self.assertEqual(row.get("currency"), "",
                         "缺 CURRENCY 时不得默认币种（旧实现写成 CNY）")
        self.assertNotIn(row.get("currency"), ("CNY", "HKD", "RMB"))
        provenance = (payload.get("metadata") or {}).get("provenance") or {}
        self.assertEqual(provenance.get("kind"), "adapter_collected")
        self.assertFalse(provenance.get("original_verified"), "未核验照实标注")
        self.assertEqual(row.get("unit"), "元")
        self.assertEqual(row.get("revenue"), 123456789, "金额保持全精度元")
        self.assertEqual(row.get("precision"), "source_exact")

    def test_unknown_currency_blocks_computation(self):
        """币种未知 → 计算不进行（预览/执行都按 unknown），不按 CNY 硬算。"""
        dataset_id, payload = self._collect(self._A_ROW)
        self.assertEqual(payload["rows"][0].get("currency"), "")
        run = _execute("debt_ratio", 0, dataset_id=dataset_id)
        reading, _row_ = _reading(run, "debt_ratio")
        self.assertEqual(reading["status"], "unknown")
        self.assertEqual(run["counts"]["insufficient"], 1)
        _plan_result, cell = _plan(dataset_id=dataset_id)
        self.assertEqual(cell["fields"]["debt_ratio"]["status"], "unknown")

    def test_declared_currency_passes_through(self):
        """正控：源响应声明了 CURRENCY 就照抄（不改成别的、也不丢）。"""
        raw = dict(self._A_ROW, CURRENCY="HKD")
        dataset_id, payload = self._collect(raw)
        self.assertEqual(payload["rows"][0].get("currency"), "HKD")

    def test_hk_fetch_without_currency_is_unknown(self):
        """港股路径同一条规则：缺 CURRENCY → metadata 币种为空 + 说明。"""
        response = json.dumps({"success": True,
                               "result": {"data": [dict(self._HK_ROW)]}})
        with patch.object(eastmoney, "_get", return_value=response):
            entry = eastmoney.fetch("合成公司", "00700")
        self.assertEqual(entry["metadata"]["currency"], "")
        self.assertEqual(entry["metadata"]["currency_source"], "unknown")
        self.assertIn("不默认", entry["metadata"]["currency_note"])
        with_currency = dict(self._HK_ROW, CURRENCY="HKD")
        response = json.dumps({"success": True,
                               "result": {"data": [with_currency]}})
        with patch.object(eastmoney, "_get", return_value=response):
            entry = eastmoney.fetch("合成公司", "00700")
        self.assertEqual(entry["metadata"]["currency"], "HKD")
        self.assertEqual(entry["metadata"]["currency_source"], "source_CURRENCY")


# ── 版本与可追溯 ───────────────────────────────────────────────────────────
class VersionAndTraceability(unittest.TestCase):
    def test_impl_version_bumped_for_semantic_change(self):
        """G1/G2 改动输出语义 → 实现版本必须前进（旧冻结结果留在旧版本下可读）。"""
        self.assertEqual(ss.IMPL_VERSION, "stock_screen/1.3.0")

    def test_run_identity_carries_impl_version(self):
        """运行身份含实现版本：同输入换实现版本 → 新身份，不与旧结果混为一谈。"""
        _write_cache([_row(2023), _row(2024, revenue=150)])
        run = _execute("revenue_growth", 0)
        self.assertEqual(run["impl_version"], ss.IMPL_VERSION)
        self.assertTrue(run["snapshot_id"] and run["run_id"])
        scope_key = st.scope_key(st.normalize_scope(SCOPE))
        policy = run["period_policy"]
        now = st.run_identity(run["snapshot_id"], run["rules_hash"], policy, scope_key)
        self.assertEqual(now, run["run_id"])
        with patch.object(ss, "IMPL_VERSION", "stock_screen/1.2.1"):
            before = st.run_identity(run["snapshot_id"], run["rules_hash"], policy,
                                     scope_key)
        self.assertNotEqual(before, now, "实现版本进身份：旧版本结果不会被当成今天的")


if __name__ == "__main__":
    unittest.main(verbosity=2)
