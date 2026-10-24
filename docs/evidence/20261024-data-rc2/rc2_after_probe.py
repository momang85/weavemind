# -*- coding: utf-8 -*-
"""DATA-RC2-1024：修复后复核脚本（与 architect_probes.py 同场景、断言「已修复」）。

保留架构师原始探针不改写；本脚本用同一批合成输入复核 F1–F7 现在是否拒绝/未知/保全。
全部临时目录 + 模拟传输：无真实外网、无生产写入、无真实凭据。
运行：python docs/evidence/20261024-data-rc2/rc2_after_probe.py  （退出码 0 = 七项都按期望行为）
"""
import builtins
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    findings = []
    failures = []

    def expect(probe, condition, detail, expected):
        findings.append({"probe": probe, "fixed": bool(condition),
                         "expected": expected, "observed": detail})
        if not condition:
            failures.append(probe)

    with tempfile.TemporaryDirectory(prefix="weavemind_data_rc2_after_") as directory:
        isolated = Path(directory)
        for key, value in {
            "WEAVEMIND_TEST_MODE": "1",
            "WEAVEMIND_DATA_DIR": str(isolated / "data"),
            "WEAVEMIND_LOG_DIR": str(isolated / "logs"),
            "MEMORY_DIR": str(isolated / "memory"),
            "WEAVEMIND_SCREEN_DIR": str(isolated / "screen"),
            "WEAVEMIND_SCREEN_CACHE": str(isolated / "synthetic-cache.json"),
        }.items():
            os.environ[key] = value
        import screen_prepare as preparation
        import screen_store as store
        from adapters import eastmoney

        scope = {"user": "synthetic-review", "project": "personal"}
        payload = {
            "metadata": {"source": "synthetic-contract-review", "synthetic": True},
            "rows": [
                {"entity": "合成甲", "code": "002304.SZ", "period": "2023-12-31",
                 "revenue": "123456789", "unit": "元", "caliber": "合并",
                 "available_at": "2024-04-20"},
                {"entity": "合成乙", "code": "600031.SH", "period": "2024-12-31",
                 "revenue": "234567891", "unit": "元", "caliber": "合并",
                 "available_at": "2025-04-20"},
            ],
        }
        Path(os.environ["WEAVEMIND_SCREEN_CACHE"]).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        # F1：甲只有 2023、乙只有 2024 → 预览「甲 2024」必须是缺材料（不做集合笛卡尔积）
        plan = preparation.prepare_plan(scope, ["002304.SZ"], ["2024"])
        status = plan["items"][0]["periods"][0]["status"]
        expect("F1_coverage_cross_company_year", status == "missing",
               {"status": status}, "missing")

        # F2：半年度请求 → 明确拒绝，不静默物化成年报
        half, half_errors = preparation.collect(
            scope=scope, source_id="official_cache", codes=["600031.SH"],
            periods=["2024-06-30"])
        expect("F2_half_year_rejected",
               half is None and any("年报" in e for e in half_errors),
               {"dataset": bool(half), "errors": half_errors},
               "dataset=None + 明确年报口径错误")

        # F3：源响应主体 ≠ 请求代码 → 拒绝入库，不按请求代码改姓
        raw_row = {"REPORT_DATE": "2024-12-31", "REPORT_TYPE": "年报",
                   "NOTICE_DATE": "2025-04-20", "SECUCODE": "600031.SH",
                   "SECURITY_CODE": "600031", "SECURITY_NAME_ABBR": "合成乙",
                   "CURRENCY": "CNY",
                   "TOTALOPERATEREVE": 123456789, "PARENTNETPROFIT": 12345678,
                   "MLR": 34567890, "XSMLL": 28.0, "TOTAL_ASSETS_PK": 400000000,
                   "LIABILITY": 100000000, "NETCASH_OPERATE_PK": 20000000}
        response = json.dumps({"success": True, "result": {"data": [raw_row]}})
        with patch.object(eastmoney, "_get", return_value=response):
            mismatch, mismatch_errors = preparation.collect(
                scope=scope, source_id="adapter:eastmoney",
                codes=["002304.SZ"], periods=["2024"])
        company = (mismatch or {}).get("companies", [{}])[0]
        expect("F3_adapter_subject_mismatch_rejected",
               mismatch is not None and company.get("status") == "failed"
               and company.get("category") == "subject_mismatch"
               and not mismatch.get("dataset"),
               {"status": company.get("status"), "category": company.get("category"),
                "dataset": (mismatch or {}).get("dataset")},
               "per-company failed/subject_mismatch，无数据集")

        # F4：全精度金额进入计算（123456789 元 ≠ 123000000）
        with patch.object(eastmoney, "_get", return_value=response):
            collected, errors = preparation.collect(
                scope=scope, source_id="adapter:eastmoney",
                codes=["600031.SH"], periods=["2024"])
        if errors or not collected.get("dataset"):
            expect("F4_amount_full_precision", False,
                   {"errors": errors}, "适配器契约可物化数据集")
        else:
            meta, adapter_payload, _err = store.load_dataset(
                scope, collected["dataset"]["dataset_id"])
            row = adapter_payload["rows"][0]
            expect("F4_amount_full_precision",
                   row.get("revenue") == 123456789
                   and row.get("precision") == "source_exact",
                   {"stored_revenue_yuan": row.get("revenue"),
                    "difference_yuan": (row.get("revenue") or 0) - 123456789,
                    "precision": row.get("precision")},
                   "revenue == 123456789（source_exact）")

            # F5：rows.json 字节改变后再次同参采集 → 拒绝复用（StoreWriteError→503
            # store_unavailable）、不覆盖坏字节、读取仍报损
            dataset_id = meta["dataset_id"]
            dataset_file = Path(store.datasets_root(scope)) / dataset_id / "rows.json"
            original_bytes = dataset_file.read_bytes()
            dataset_file.write_bytes(original_bytes + b" ")
            repeat_error = ""
            try:
                with patch.object(eastmoney, "_get", return_value=response):
                    repeated, repeated_errors = preparation.collect(
                        scope=scope, source_id="adapter:eastmoney",
                        codes=["600031.SH"], periods=["2024"])
                if repeated_errors:
                    repeat_error = "；".join(repeated_errors)
            except store.StoreWriteError as exc:
                repeat_error = str(exc)
            _, _, read_error = store.load_dataset(scope, dataset_id)
            expect("F5_corrupt_dataset_reuse_rejected",
                   "不一致" in repeat_error and
                   dataset_file.read_bytes() == original_bytes + b" "
                   and bool(read_error),
                   {"collect_write_error": repeat_error[:120],
                    "bytes_unchanged_after_refusal":
                        dataset_file.read_bytes() == original_bytes + b" ",
                    "load_rejects": bool(read_error)},
                   "拒绝复用（HTTP 503 store_unavailable）+ 原坏字节不被重写 + 读取仍报损")

        # F6：传输前失败 → 真实 HTTP 请求数未知，不再拿调用数冒充
        with patch.object(eastmoney, "fetch_ashare",
                          side_effect=OSError("getaddrinfo failed before connection")):
            failed, _errors = preparation.collect(
                scope=scope, source_id="adapter:eastmoney",
                codes=["600031.SH"], periods=["2024"])
        counters = (failed or {}).get("counters", {})
        expect("F6_pre_connection_not_counted_as_connected",
               counters.get("adapter_calls") == 1
               and counters.get("network_requests") is None
               and bool(counters.get("network_requests_note")),
               {k: counters.get(k) for k in
                ("adapter_calls", "network_requests", "network_requests_note")},
               "adapter_calls=1, network_requests=None + 说明")

        # F7：账本不可读 → 拒绝覆盖、原字节保留、累计不归零
        cost_scope = {"user": "synthetic-ledger", "project": "personal"}
        store.record_cost(cost_scope, runs_total=7)
        ledger = Path(store._cost_path(cost_scope))
        original_ledger = ledger.read_bytes()
        original_open = builtins.open

        def blocked_read(filename, mode="r", *args, **kwargs):
            if os.fspath(filename) == str(ledger) and mode == "rb":
                raise PermissionError("simulated read denial; replacement remains writable")
            return original_open(filename, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=blocked_read):
            written = store.record_cost(cost_scope, runs_total=1)
        restored, _ = store.load_cost(cost_scope)
        expect("F7_unreadable_ledger_not_overwritten",
               written.get("recorded") is False
               and written.get("recording", {}).get("reason") == "ledger_unreadable"
               and ledger.read_bytes() == original_ledger
               and restored.get("runs_total") == 7,
               {"recorded": written.get("recorded"),
                "reason": written.get("recording", {}).get("reason"),
                "bytes_preserved": ledger.read_bytes() == original_ledger,
                "runs_total": restored.get("runs_total")},
               "recorded=False/ledger_unreadable，bytes 不变，runs_total=7")

    report = {
        "date": "2026-10-24", "runtime": sys.version.split()[0],
        "method": "offline synthetic contracts + mocked transport; temporary dirs only",
        "findings": findings,
        "all_fixed": not failures,
        "failed_probes": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
