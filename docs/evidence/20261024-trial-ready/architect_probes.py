import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch


def main():
    repository = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(repository))
    findings = []

    with tempfile.TemporaryDirectory(prefix="weavemind_trial_ready_review_") as directory:
        isolated = Path(directory)
        settings = {
            "WEAVEMIND_TEST_MODE": "1",
            "WEAVEMIND_DATA_DIR": str(isolated / "data"),
            "WEAVEMIND_LOG_DIR": str(isolated / "logs"),
            "WEAVEMIND_SCREEN_DIR": str(isolated / "screen"),
            "WEAVEMIND_SCREEN_CACHE": str(isolated / "cache.json"),
            "WEAVEMIND_WORKSPACE_ROOT": str(isolated / "workspace"),
            "MEMORY_DIR": str(isolated / "memory"),
            "AGENTS_DB": str(isolated / "agents.db"),
        }
        os.environ.update(settings)
        import research_materials
        import screen_prepare
        import screen_store
        from adapters import eastmoney

        scope = {"user": "synthetic-review", "project": "personal"}
        code = "600031.SH"
        cache = Path(settings["WEAVEMIND_SCREEN_CACHE"])

        def row(year, **changes):
            result = {
                "entity": "合成复核公司（非真实财务数据）", "code": code,
                "period": f"{year}-12-31", "period_type": "年报",
                "revenue": 100, "gross_profit": 30, "net_profit": 10,
                "operating_cashflow": 12, "total_assets": 200,
                "total_liabilities": 60, "unit": "元", "currency": "CNY",
                "caliber": "合并", "available_at": f"{year + 1}-04-20",
                "disclosed_at": f"{year + 1}-04-20",
                "source_url": "https://example.invalid/synthetic-review",
            }
            result.update(changes)
            return result

        def write_cache(rows):
            cache.write_text(json.dumps({"metadata": {"synthetic": True}, "rows": rows},
                                        ensure_ascii=False), encoding="utf-8")

        def execute(field, threshold=0, dataset_id=None):
            result, errors = screen_store.execute(
                scope=scope, pool=[code], as_of="2026-10-24",
                rules_raw=[{"field": field, "op": ">", "value": threshold}],
                period_policy_raw={"mode": "pinned", "report_period": "2024"},
                dataset_id=dataset_id, store=False,
            )
            if errors:
                raise AssertionError(errors)
            return result

        write_cache([row(2022), row(2024, revenue=150)])
        result = execute("revenue_growth", 40)
        findings.append({
            "id": "G1", "name": "non_adjacent_years_reported_as_yoy",
            "reproduced": result["counts"]["pass"] == 1,
            "input": {"2022_revenue": 100, "2024_revenue": 150, "2023": "missing"},
            "observed": result["rows"],
            "expected": "2023 missing must not produce 2024 year-on-year growth",
        })

        write_cache([row(2023), row(2024, total_assets=0)])
        plan = screen_prepare.prepare_plan(scope, [code], ["2024"])
        cell = plan["items"][0]["periods"][0]
        result = execute("debt_ratio")
        findings.append({
            "id": "G2", "name": "coverage_ignores_noncomputable_denominator",
            "reproduced": cell["computable"] and result["counts"]["insufficient"] == 1,
            "observed": {"preview_status": cell["status"], "debt_field": cell["fields"]["debt_ratio"],
                         "actual_counts": result["counts"]},
            "expected": "zero denominator must not be previewed as computable",
        })

        write_cache([])
        for name, rows in [("synthetic-source-a", [row(2023)]),
                           ("synthetic-source-b", [row(2024, revenue=150)])]:
            metadata, reused, errors = screen_store.save_dataset_from_source(
                scope=scope, name=name, rows=rows,
                provenance={"kind": "synthetic_demo", "synthetic": True, "label": name},
            )
            if errors:
                raise AssertionError(errors)
        plan = screen_prepare.prepare_plan(scope, [code], ["2024"])
        cell = plan["items"][0]["periods"][0]
        result = execute("revenue_growth", dataset_id=metadata["dataset_id"])
        findings.append({
            "id": "G3", "name": "coverage_combines_separate_datasets",
            "reproduced": cell["fields"]["revenue_growth"]["status"] == "ok"
                          and result["counts"]["insufficient"] == 1,
            "observed": {"preview_growth": cell["fields"]["revenue_growth"],
                         "selected_dataset_periods": metadata["periods"],
                         "actual_counts": result["counts"]},
            "expected": "a separate dataset must not silently supply the previous year",
        })

        raw_row = {
            "REPORT_DATE": "2024-12-31", "REPORT_TYPE": "年报", "NOTICE_DATE": "2025-04-20",
            "SECUCODE": code, "SECURITY_CODE": "600031", "SECURITY_NAME_ABBR": "合成公司",
            "TOTALOPERATEREVE": 123456789, "PARENTNETPROFIT": 12345678,
        }
        raw_response = json.dumps({"success": True, "result": {"data": [raw_row]}})
        with patch.object(eastmoney, "_get", return_value=raw_response):
            collected, errors = screen_prepare.collect(
                scope=scope, source_id="adapter:eastmoney", codes=[code], periods=["2024"],
            )
        if errors or not collected.get("dataset"):
            raise AssertionError(errors or collected)
        metadata, payload, errors = screen_store.load_dataset(scope, collected["dataset"]["dataset_id"])
        if errors:
            raise AssertionError(errors)
        findings.append({
            "id": "G4", "name": "source_currency_missing_defaults_upstream",
            "reproduced": payload["rows"][0].get("currency") == "CNY",
            "observed": {"raw_currency_present": "CURRENCY" in raw_row,
                         "stored_currency": payload["rows"][0].get("currency")},
            "expected": "unknown or explicit source-contract evidence, not a silent default",
        })

        metadata, reused, errors = screen_store.save_dataset_from_source(
            scope=scope, name="synthetic-research-provenance", rows=[row(2024)],
            provenance={"kind": "user_provided", "user_provided": True,
                        "official": False, "synthetic": True,
                        "kind_label": "本人研究材料（合成探针）", "parent_task_id": "synthetic-task"},
        )
        if errors:
            raise AssertionError(errors)
        metadata, payload, errors = screen_store.load_dataset(scope, metadata["dataset_id"])
        findings.append({
            "id": "G5", "name": "research_provenance_flags_disagree",
            "reproduced": metadata.get("user_provided") is False
                          and payload["metadata"]["provenance"]["user_provided"] is True,
            "observed": {"dataset_user_provided": metadata.get("user_provided"),
                         "provenance_user_provided": payload["metadata"]["provenance"]["user_provided"],
                         "kind": metadata.get("provenance_kind")},
            "expected": "canonical research-origin identity across storage, snapshot and UI",
        })

        with closing(sqlite3.connect(settings["AGENTS_DB"])) as connection:
            connection.execute("CREATE TABLE task_history (task_id TEXT, goal TEXT, status TEXT, "
                               "created_at TEXT, updated_at TEXT, user TEXT, project TEXT, "
                               "research_request_json TEXT)")
            connection.execute("INSERT INTO task_history VALUES (?, '', '', ?, '', ?, 'default', '')",
                               ("synthetic-owned", "2020-01-01", scope["user"]))
            connection.executemany("INSERT INTO task_history VALUES (?, '', '', ?, '', ?, 'default', '')",
                                   [(f"synthetic-other-{index}", "2026-01-01", "synthetic-other")
                                    for index in range(801)])
            connection.commit()
        tasks, error, counts = research_materials._scoped_task_rows(scope, 200)
        findings.append({
            "id": "G6", "name": "other_users_recent_tasks_hide_owned_materials",
            "reproduced": not error and not tasks,
            "observed": {"owned_tasks_in_database": 1, "returned_owned_tasks": len(tasks),
                         "scanned": counts["scanned"]},
            "expected": "filter ownership before pagination so other users cannot hide owned materials",
        })

    fingerprints = {
        name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
        for name in ["stock_screen.py", "screen_prepare.py", "screen_store.py", "research_materials.py",
                     "adapters/eastmoney.py", "frontend/src/pages/StockScreen.tsx", "tools/build_rc2_package.py"]
    }
    print(json.dumps({"date": "2026-10-24", "runtime": sys.version.split()[0],
                      "method": "offline synthetic data; mocked transport; isolated temporary paths",
                      "fingerprints": fingerprints, "findings": findings,
                      "reproduced_count": sum(item["reproduced"] for item in findings)},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
