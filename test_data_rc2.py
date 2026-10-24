# -*- coding: utf-8 -*-
"""DATA-RC2-1024：A/B 反例回归（F1–F7 + A1 严格校验 + 响应错主体 + 精度 + 计量）。

全部临时目录 + 模拟传输：无真实外网、无生产写入、无真实凭据。
真实网络探针单独一个用例，网络不可用时 skip，不算通过也不算失败。
"""
from __future__ import annotations

import builtins
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
os.environ.setdefault("WEAVEMIND_TEST_MODE", "1")
_TMP = tempfile.mkdtemp(prefix="weavemind-rc2-regression-")
os.environ["WEAVEMIND_SCREEN_DIR"] = os.path.join(_TMP, "screen")
os.environ["WEAVEMIND_DATA_DIR"] = os.path.join(_TMP, "data")

import screen_prepare as sp          # noqa: E402
import screen_store as st            # noqa: E402
import stock_screen as ss            # noqa: E402
from adapters import eastmoney       # noqa: E402

SCOPE_A = {"user": "rc2a", "project": "personal"}


def _resp_row(**over):
    row = {"REPORT_DATE": "2024-12-31", "REPORT_TYPE": "年报",
           "NOTICE_DATE": "2025-04-20", "SECUCODE": "600031.SH",
           "SECURITY_CODE": "600031", "SECURITY_NAME_ABBR": "合成重工",
           "CURRENCY": "CNY", "TOTALOPERATEREVE": 123456789,
           "PARENTNETPROFIT": 12345678, "MLR": 34567890, "XSMLL": 28.0,
           "TOTAL_ASSETS_PK": 400000000, "LIABILITY": 100000000,
           "NETCASH_OPERATE_PK": 20000000}
    row.update(over)
    return row


def _resp(*rows):
    return json.dumps({"success": True, "result": {"data": list(rows)}})


def _two_year_rows():
    """两家错开年度的合成响应（甲 2023+2024，乙 2024）：A1 错开年度用例。"""
    return [
        _resp_row(SECUCODE="002304.SZ", SECURITY_CODE="002304", SECURITY_NAME_ABBR="合成甲",
                  REPORT_DATE="2023-12-31", NOTICE_DATE="2024-04-10",
                  TOTALOPERATEREVE=900000001, PARENTNETPROFIT=90000001,
                  MLR=250000001, TOTAL_ASSETS_PK=300000001, LIABILITY=150000001,
                  NETCASH_OPERATE_PK=80000001),
        _resp_row(SECUCODE="002304.SZ", SECURITY_CODE="002304", SECURITY_NAME_ABBR="合成甲",
                  REPORT_DATE="2024-12-31", NOTICE_DATE="2025-04-10",
                  TOTALOPERATEREVE=123456789, PARENTNETPROFIT=12345678,
                  MLR=34567890, TOTAL_ASSETS_PK=400000000, LIABILITY=100000000,
                  NETCASH_OPERATE_PK=20000000),
        _resp_row(SECUCODE="600031.SH", SECURITY_CODE="600031", SECURITY_NAME_ABBR="合成乙",
                  REPORT_DATE="2024-12-31", NOTICE_DATE="2025-04-20",
                  TOTALOPERATEREVE=234567891, PARENTNETPROFIT=23456789,
                  MLR=45678901, TOTAL_ASSETS_PK=500000000, LIABILITY=200000000,
                  NETCASH_OPERATE_PK=30000000),
    ]


class StrictValidation(unittest.TestCase):
    """RC2-A1：严格参数校验（非法代码/期间显式拒绝）。"""

    def test_half_year_rejected_not_silently_annualized(self):
        """RC2-F2：2024-06-30 → 显式拒绝，不物化成年报。"""
        data, errs = sp.collect(scope=SCOPE_A, source_id="official_cache",
                                codes=["002304.SZ"], periods=["2024-06-30"])
        self.assertIsNone(data)
        self.assertTrue(errs and "年报" in errs[0], errs)

    def test_illegal_date_and_bad_code_rejected(self):
        data, errs = sp.collect(scope=SCOPE_A, source_id="official_cache",
                                codes=["002304.SZ", "not-a-code"],
                                periods=["2024-02-30"])
        self.assertIsNone(data)
        joined = "；".join(errs)
        self.assertIn("not-a-code", joined, "非法代码要点名，不静默丢")
        self.assertIn("2024-02-30", joined, "非法日期要点名")

    def test_empty_codes_rejected(self):
        data, errs = sp.collect(scope=SCOPE_A, source_id="official_cache",
                                codes=["", None], periods=["2024"])
        self.assertIsNone(data)
        self.assertTrue(any("空" in e for e in errs), errs)

    def test_plan_same_validation_as_collect(self):
        """预览与物化共用同一输入校验（RC2-A1）。"""
        plan = sp.prepare_plan(SCOPE_A, ["600519"], ["2024-06-30"])
        self.assertEqual(plan["status"], "invalid")
        self.assertTrue(len(plan["errors"]) == 2, plan["errors"])

    def test_non_contiguous_years_only_requested_returned(self):
        """非连续年度：只返回明确选择的年度，不自动补中间年。"""
        rows = _two_year_rows()
        fetched = {}
        def fake_fetch(company, stock_code, year_range=None, period="annual", **kw):
            fetched["year_range"] = year_range
            return {"company": "合成甲", "code": stock_code,
                    "financials": [f for f in _financials_from_rows(rows)
                                   if f["source_subject"]["code"] == stock_code],
                    "metadata": {"company": "合成甲", "currency": "CNY",
                                 "caliber": "合并", "retrieved_at": "2026-10-24 00:00:00",
                                 "normalization_version": "amount_yuan/1"},
                    "raw": {"url": "https://example.invalid/ashare", "text": ""}}
        with patch.object(eastmoney, "fetch_ashare", side_effect=fake_fetch):
            data, errs = sp.collect(scope=SCOPE_A, source_id="adapter:eastmoney",
                                    codes=["002304.SZ"], periods=["2023", "2025"])
        self.assertEqual(errs, [])
        collected = [c for c in data["companies"] if c["status"] == "collected"]
        periods = {p for c in collected for p in c.get("periods") or []}
        self.assertNotIn("2025-12-31", periods, "没取到的年份不编")
        self.assertIn("2023-12-31", periods)
        ds = data["dataset"]
        self.assertNotIn("2025-12-31", ds["periods"])


def _financials_from_rows(rows):
    """复用正式适配器的字段抽取（不复制逻辑）：走 _rows_from_adapter 的输入侧。"""
    from adapters.eastmoney import _yuan_exact
    out = []
    for r in rows:
        amounts = {k: _yuan_exact(r.get(f)) for k, f in
                   (("revenue", "TOTALOPERATEREVE"), ("net_profit", "PARENTNETPROFIT"),
                    ("gross_profit", "MLR"), ("operating_profit", "OPERATE_PROFIT_PK"),
                    ("total_assets", "TOTAL_ASSETS_PK"), ("total_liabilities", "LIABILITY"),
                    ("operating_cashflow", "NETCASH_OPERATE_PK"), ("rd_expense", "RDEXPEND"))}
        amounts = {k: v for k, v in amounts.items() if v is not None}
        out.append({
            "year": int(str(r["REPORT_DATE"])[:4]),
            "report_date": r["REPORT_DATE"], "report_type": r["REPORT_TYPE"],
            "disclosure_date": r["NOTICE_DATE"],
            "source_subject": {"code": r["SECUCODE"], "name": r["SECURITY_NAME_ABBR"]},
            "amounts_yuan": amounts,
            "revenue": round(amounts["revenue"] / 1e8, 2) if "revenue" in amounts else None,
            "net_profit": round(amounts["net_profit"] / 1e8, 2) if "net_profit" in amounts else None,
            "gross_profit": round(amounts["gross_profit"] / 1e8, 2) if "gross_profit" in amounts else None,
            "total_assets": round(amounts["total_assets"] / 1e8, 2) if "total_assets" in amounts else None,
            "total_liabilities": round(amounts["total_liabilities"] / 1e8, 2) if "total_liabilities" in amounts else None,
            "operating_cashflow": round(amounts["operating_cashflow"] / 1e8, 2) if "operating_cashflow" in amounts else None,
        })
    return out


class SourceIdentityAndPrecision(unittest.TestCase):
    """RC2-F3/F4：源身份校验 + 全精度金额。"""

    def _collect(self, *rows, codes=("600031.SH",), periods=("2024",)):
        def fake_fetch(company, stock_code, year_range=None, period="annual", **kw):
            subject = str(rows[0].get("SECUCODE") or "")
            return {"company": str(rows[0].get("SECURITY_NAME_ABBR") or ""),
                    "code": stock_code,
                    "financials": _financials_from_rows(rows),
                    "metadata": {"company": str(rows[0].get("SECURITY_NAME_ABBR") or ""),
                                 "currency": "CNY", "caliber": "合并",
                                 "retrieved_at": "2026-10-24 00:00:00",
                                 "normalization_version": "amount_yuan/1"},
                    "raw": {"url": "https://example.invalid/ashare", "text": ""}}
        with patch.object(eastmoney, "fetch_ashare", side_effect=fake_fetch):
            return sp.collect(scope=SCOPE_A, source_id="adapter:eastmoney",
                              codes=list(codes), periods=list(periods))

    def test_response_subject_mismatch_rejected(self):
        """RC2-F3：请求甲、响应乙 → 拒绝入库，不按请求代码改姓。"""
        data, errs = self._collect(
            _resp_row(SECUCODE="600031.SH", SECURITY_CODE="600031",
                      SECURITY_NAME_ABBR="合成乙"), codes=("002304.SZ",))
        self.assertEqual(errs, [])
        self.assertIsNone(data["dataset"], "主体不符的行不物化")
        item = data["companies"][0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["category"], "subject_mismatch")
        self.assertIn("600031.SH", item["detail"])

    def test_response_subject_missing_rejected(self):
        """源响应缺证券代码 → 主体身份不明，拒绝入库留待核验。"""
        data, errs = self._collect(
            _resp_row(SECUCODE="", SECURITY_CODE=""))
        self.assertEqual(errs, [])
        self.assertIsNone(data["dataset"])
        self.assertEqual(data["companies"][0]["category"], "subject_mismatch")

    def test_amount_full_precision_round_trip(self):
        """RC2-F4：123456789 元进、123456789 元出；展示舍入不回写存储。"""
        data, errs = self._collect(_resp_row())
        self.assertEqual(errs, [])
        _meta, payload, read_err = st.load_dataset(
            SCOPE_A, data["dataset"]["dataset_id"])
        self.assertEqual(read_err, "")
        row = payload["rows"][0]
        self.assertEqual(row["revenue"], 123456789)
        self.assertEqual(row["revenue"] - 123456789, 0, "差值必须为 0 元")
        self.assertEqual(row["precision"], "source_exact")
        self.assertEqual(row["normalization_version"], "amount_yuan/1")
        self.assertEqual(row["source_amount_unit"], "元")

    def test_precision_threshold_exact(self):
        """精度临界：个位数金额也精确（不进亿元两位小数的舍入路径）。"""
        data, errs = self._collect(_resp_row(TOTALOPERATEREVE=7))
        self.assertEqual(errs, [])
        _meta, payload, _err = st.load_dataset(SCOPE_A, data["dataset"]["dataset_id"])
        self.assertEqual(payload["rows"][0]["revenue"], 7)

    def test_display_rounding_does_not_change_result(self):
        """展示量化发生在读数层：存储行全精度，筛选读数不受亿元展示影响。"""
        data, errs = self._collect(_resp_row())
        self.assertEqual(errs, [])
        _meta, payload, _err = st.load_dataset(SCOPE_A, data["dataset"]["dataset_id"])
        snapshot, build_errors = ss.build_snapshot(payload, pool=["600031.SH"],
                                                   as_of="2026-10-24")
        self.assertEqual(build_errors, [])
        entity = snapshot.entity("600031.SH")
        self.assertIsNotNone(entity, f"快照里应有 600031.SH：{[e['entity_id'] for e in snapshot.entities]}")
        observations = [o for o in entity["dataset"].observations
                        if o.metric == "revenue"]
        self.assertTrue(observations, "冻结数据集里应有 revenue 观察")
        self.assertEqual(observations[0].value, 123456789,
                         "冻结读数用原始元金额，不是亿元展示值")


class ProvenanceAndUnknowns(unittest.TestCase):
    """RC2-A2：单位/币种/口径不补默认；重述可用时间不倒灌成过去事实。"""

    def test_unknown_unit_caliber_not_defaulted(self):
        rows = _financials_from_rows([_resp_row()])
        for r in rows:
            r["normalization"] = {"version": "amount_yuan/1"}
        def fake_fetch(company, stock_code, **kw):
            return {"company": "合成重工", "code": stock_code, "financials": rows,
                    # 源契约里没有 currency/caliber：不得补 CNY/合并
                    "metadata": {"company": "合成重工", "retrieved_at": "2026-10-24",
                                 "normalization_version": "amount_yuan/1"},
                    "raw": {"url": "https://example.invalid/x", "text": ""}}
        with patch.object(eastmoney, "fetch_ashare", side_effect=fake_fetch):
            data, errs = sp.collect(scope=SCOPE_A, source_id="adapter:eastmoney",
                                    codes=["600031.SH"], periods=["2024"])
        self.assertEqual(errs, [])
        _meta, payload, _err = st.load_dataset(SCOPE_A, data["dataset"]["dataset_id"])
        row = payload["rows"][0]
        self.assertEqual(row.get("currency"), "", "币种缺失不得默认 CNY")
        self.assertEqual(row.get("caliber"), "", "口径缺失不得默认 合并")

    def test_restatement_available_at_respected(self):
        """同一期间两条观察（原披露 2025-04-20 / 重述 2026-08-01）：
        截止 2025-12-31 只能用原值，不能用重述值倒灌过去。"""
        payload = {
            "metadata": {"source": "synthetic-restatement", "synthetic": True},
            "rows": [
                {"entity": "合成丙", "code": "000002.SZ", "period": "2024-12-31",
                 "revenue": 100, "unit": "元", "caliber": "合并",
                 "available_at": "2025-04-20", "source_url": "about:original"},
                {"entity": "合成丙", "code": "000002.SZ", "period": "2024-12-31",
                 "revenue": 120, "unit": "元", "caliber": "合并",
                 "available_at": "2026-08-01", "source_url": "about:restated"},
            ],
        }
        early, errs_early = ss.build_snapshot(payload, pool=["000002.SZ"],
                                              as_of="2025-12-31", synthetic=True)
        self.assertEqual(errs_early, [])
        obs_early = [o for o in early.entity("000002.SZ")["dataset"].observations
                     if o.metric == "revenue"]
        self.assertEqual(len(obs_early), 1, "重述行尚未可用时不得参与")
        self.assertEqual(obs_early[0].value, 100, "截止日只能用当时可见的原值")
        self.assertEqual(obs_early[0].source_url, "about:original")
        late, errs_late = ss.build_snapshot(payload, pool=["000002.SZ"],
                                            as_of="2026-12-31", synthetic=True)
        self.assertEqual(errs_late, [])
        obs_late = [o for o in late.entity("000002.SZ")["dataset"].observations
                    if o.metric == "revenue"]
        late_values = {float(o.value) for o in obs_late}
        self.assertIn(120.0, late_values, "重述可用后才纳入重述值")


class LedgerAndMetering(unittest.TestCase):
    """RC2-F7 + 计量可见性（每个用例独立作用域，互不污染）。"""

    def _scope(self, name):
        return {"user": f"rc2-{name}", "project": "personal"}

    def test_unreadable_ledger_blocks_overwrite(self):
        scope = self._scope("ledger-unreadable")
        st.record_cost(scope, runs_total=7)
        ledger = Path(st._cost_path(scope))
        original = ledger.read_bytes()
        real_open = builtins.open

        def blocked(filename, mode="r", *args, **kwargs):
            if os.fspath(filename) == str(ledger) and mode == "rb":
                raise PermissionError("simulated read denial")
            return real_open(filename, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=blocked):
            written = st.record_cost(scope, runs_total=1)
        self.assertFalse(written["recorded"])
        self.assertEqual(written["recording"]["reason"], "ledger_unreadable")
        self.assertEqual(ledger.read_bytes(), original, "原字节不得被覆盖")
        restored, _warns = st.load_cost(scope)
        self.assertEqual(restored["runs_total"], 7, "累计不归零")
        self.assertEqual(restored["integrity"], "ok",
                         "文件本身仍可读：不可读只发生在测试注入的读取阶段")

    def test_corrupt_ledger_blocks_overwrite_and_rebuild_archives(self):
        scope = self._scope("ledger-corrupt")
        ledger = Path(st._cost_path(scope))
        st.record_cost(scope, runs_total=3)
        good = ledger.read_bytes()
        ledger.write_bytes(b"{not json")
        written = st.record_cost(scope, runs_total=1)
        self.assertFalse(written["recorded"])
        self.assertEqual(written["recording"]["reason"], "ledger_corrupt")
        self.assertEqual(ledger.read_bytes(), b"{not json", "损坏字节保留")
        view, warns = st.load_cost(scope)
        self.assertEqual(view["integrity"], "corrupt")
        self.assertTrue(warns and "未记录" in warns[0])
        self.assertEqual(view["runs_total"], 0, "零默认值只用于「未知」视图")
        rebuilt, errs = st.rebuild_cost(scope, confirm=True)
        self.assertEqual(errs, [])
        self.assertIsNotNone(rebuilt)
        archive = Path(rebuilt["archived"])
        self.assertEqual(archive.read_bytes(), b"{not json", "重建先归档坏字节")
        self.assertEqual(rebuilt["cost"]["runs_total"], 0,
                         "新账从 0 开始，不冒充历史完整统计")
        ledger.write_bytes(good)          # 还原可读账本
        restored, _w = st.load_cost(scope)
        self.assertEqual(restored["runs_total"], 3)

    def test_metering_failure_visible_in_collect_response(self):
        """业务成功 + 计量失败 → 响应里可见（recording 被调用方使用，不静默丢弃）。"""
        scope = self._scope("ledger-metering")
        ledger = Path(st._cost_path(scope))
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_bytes(b"{broken")
        rows = _financials_from_rows([_resp_row()])

        def fake_fetch(company, stock_code, **kw):
            return {"company": "合成重工", "code": stock_code, "financials": rows,
                    "metadata": {"company": "合成重工", "currency": "CNY",
                                 "caliber": "合并", "retrieved_at": "2026-10-24",
                                 "normalization_version": "amount_yuan/1"},
                    "raw": {"url": "https://example.invalid/x", "text": ""}}

        with patch.object(eastmoney, "fetch_ashare", side_effect=fake_fetch):
            data, errs = sp.collect(scope=scope, source_id="adapter:eastmoney",
                                    codes=["600031.SH"], periods=["2024"])
        self.assertEqual(errs, [])
        self.assertEqual(data["status"], "ok", "业务结果可以成功")
        self.assertIsNotNone(data.get("metering"), "计量结果必须透传给调用方")
        self.assertFalse(data["metering"]["recorded"])
        self.assertEqual(data["metering"]["recording"]["reason"], "ledger_corrupt")
        ledger.unlink()                   # 清理本测试临时目录里的坏账本

    def test_cost_counters_separate_adapter_calls_from_requests(self):
        """RC2-F6：账本字段 adapter_calls 与 network_requests 语义分开。"""
        view, _warns = st.load_cost(self._scope("ledger-fields"))
        self.assertIn("adapter_calls", view)


class ConcurrencyRetained(unittest.TestCase):
    """保留上一批并发/原子性回归（不层叠新锁）。"""

    def test_watchlist_concurrent_mutate_no_lost_update(self):
        import threading
        base = st.screen_base()
        before = len((st.load_watchlist(SCOPE_A)[0]).get("items") or [])
        errors = []
        def add(i):
            try:
                meta, _reused, errs = st.watchlist_mutate(
                    scope=SCOPE_A, action="add", code=f"9000{i:02d}.SH",
                    name=f"并发{i}", if_version=None)
                if errs:
                    errors.append(errs)
            except Exception as exc:          # noqa: BLE001
                errors.append(str(exc))
        threads = [threading.Thread(target=add, args=(i,)) for i in range(1, 9)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        items = (st.load_watchlist(SCOPE_A)[0]).get("items") or []
        self.assertEqual(len(items) - before, 8, f"8 个并发 add 全部落账：{errors}")


class RealNetworkProbe(unittest.TestCase):
    """真实网络单独一档：不可用就 skip（不冒充通过/失败）。"""

    def test_real_probe_single_company(self):
        summary = sp.official_cache_summary()
        if not summary.get("available"):
            self.skipTest("本机没有官方缓存样本")
        try:
            data, errs = sp.collect(scope=SCOPE_A, source_id="adapter:eastmoney",
                                    codes=["600519.SH"], periods=["2024"],
                                    probe_only=True)
        except OSError as exc:
            self.skipTest(f"网络不可用：{exc}")
        self.assertEqual(errs, [])
        first = data["companies"][0]
        self.assertIn(first["status"], ("collected", "collected_with_rejects",
                                        "failed", "denied_by_policy"))
        if first["status"].startswith("collected"):
            row_code = first.get("source_subject")
            self.assertEqual(row_code, "600519.SH",
                             "真实响应主体必须与请求一致（RC2-F3）")
            self.assertIn(first.get("precision"), ("source_exact", "display_rounded_yuan"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
