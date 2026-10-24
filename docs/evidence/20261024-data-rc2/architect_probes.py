import builtins
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import zipfile


def main():
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix="weavemind_data_rc2_review_") as directory:
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
                {"entity": "Synthetic A", "code": "002304.SZ", "period": "2023-12-31",
                 "revenue": "123456789", "unit": "元", "caliber": "合并",
                 "available_at": "2024-04-20"},
                {"entity": "Synthetic B", "code": "600031.SH", "period": "2024-12-31",
                 "revenue": "234567891", "unit": "元", "caliber": "合并",
                 "available_at": "2025-04-20"},
            ],
        }
        Path(os.environ["WEAVEMIND_SCREEN_CACHE"]).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        findings = []
        plan = preparation.prepare_plan(scope, ["002304.SZ"], ["2024"])
        findings.append({"probe": "coverage_cross_company_year", "expected": "missing",
                         "actual": plan["items"][0]["periods"][0]["status"]})
        collected, errors = preparation.collect(
            scope=scope, source_id="official_cache", codes=["600031.SH"],
            periods=["2024-06-30"])
        findings.append({"probe": "half_year_silently_becomes_annual",
                         "requested": "2024-06-30", "errors": errors,
                         "returned_periods": collected["dataset"]["periods"] if collected else []})

        raw_row = {"REPORT_DATE": "2024-12-31", "REPORT_TYPE": "年报",
                   "NOTICE_DATE": "2025-04-20", "SECUCODE": "600031.SH",
                   "SECURITY_CODE": "600031", "SECURITY_NAME_ABBR": "Synthetic B",
                   "TOTALOPERATEREVE": 123456789, "PARENTNETPROFIT": 12345678,
                   "MLR": 34567890, "TOTAL_ASSETS_PK": 400000000,
                   "LIABILITY": 100000000, "NETCASH_OPERATE_PK": 20000000}
        response = json.dumps({"success": True, "result": {"data": [raw_row]}})
        with patch.object(eastmoney, "_get", return_value=response):
            collected, errors = preparation.collect(
                scope=scope, source_id="adapter:eastmoney", codes=["002304.SZ"], periods=["2024"])
        if errors or not collected.get("dataset"):
            raise RuntimeError("Synthetic adapter contract fixture did not reach dataset persistence")
        metadata, adapter_payload, error = store.load_dataset(scope, collected["dataset"]["dataset_id"])
        adapter_row = adapter_payload["rows"][0]
        findings.append({"probe": "adapter_subject_mismatch_accepted",
                         "requested": "002304.SZ", "response_subject": "600031.SH",
                         "stored_subject": adapter_row["code"], "errors": errors})
        findings.append({"probe": "adapter_amount_round_trip_precision",
                         "raw_revenue_yuan": raw_row["TOTALOPERATEREVE"],
                         "stored_revenue_yuan": adapter_row["revenue"],
                         "difference_yuan": adapter_row["revenue"] - raw_row["TOTALOPERATEREVE"]})
        dataset_file = Path(store.datasets_root(scope)) / metadata["dataset_id"] / "rows.json"
        dataset_file.write_bytes(dataset_file.read_bytes() + b" ")
        with patch.object(eastmoney, "_get", return_value=response):
            repeated, repeated_errors = preparation.collect(
                scope=scope, source_id="adapter:eastmoney", codes=["002304.SZ"], periods=["2024"])
        _, _, read_error = store.load_dataset(scope, metadata["dataset_id"])
        findings.append({"probe": "source_dataset_corrupt_reuse",
                         "collect_errors": repeated_errors, "reused": repeated.get("reused"),
                         "load_rejects_after_reuse": bool(read_error)})

        with patch.object(eastmoney, "fetch_ashare", side_effect=OSError("getaddrinfo failed before connection")):
            failed, errors = preparation.collect(
                scope=scope, source_id="adapter:eastmoney", codes=["002304.SZ"], periods=["2024"])
        findings.append({"probe": "pre_connection_failure_counted_as_connected",
                         "fixture": "adapter fails before any transport call",
                         "actual_network_requests": failed["counters"]["network_requests"]})

        cost_scope = {"user": "synthetic-ledger", "project": "personal"}
        store.record_cost(cost_scope, runs_total=7)
        ledger = Path(store._cost_path(cost_scope))
        original_bytes = ledger.read_bytes()
        original_open = builtins.open

        def blocked_read(filename, mode="r", *args, **kwargs):
            if os.fspath(filename) == str(ledger) and mode == "rb":
                raise PermissionError("simulated read denial; file replacement remains writable")
            return original_open(filename, mode, *args, **kwargs)

        with patch("builtins.open", side_effect=blocked_read):
            written = store.record_cost(cost_scope, runs_total=1)
        restored, _ = store.load_cost(cost_scope)
        findings.append({"probe": "unreadable_ledger_overwrite",
                         "recorded": written["recorded"], "original_bytes_preserved": ledger.read_bytes() == original_bytes,
                         "runs_total_after": restored["runs_total"]})

        package = root / "tmp" / "rc-package-1024" / "weavemind-rc-20261024.zip"
        with zipfile.ZipFile(package) as archive:
            names = archive.namelist()
        package_review = {
            "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
            "file_count": len(names),
            "configured_cache_included": any(name.endswith("data/screen_cache/official_disclosure_cache.json") for name in names),
            "default_frontend_index_included": any(name.endswith("frontend/dist/index.html") for name in names),
            "check_build_index_included": any(name.endswith("frontend/dist-screen-check/index.html") for name in names),
        }
        report = {
            "date": "2026-10-24", "runtime": sys.version.split()[0],
            "method": "offline synthetic contracts, mocked transport, temporary storage; no production mutation or real network",
            "fingerprints": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                             for name in ("screen_prepare.py", "screen_store.py", "adapters/eastmoney.py", "web_ui.py")},
            "findings": findings, "package_review": package_review,
        }
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
