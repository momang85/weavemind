"""Small offline contract checks; all fixture companies/labels are synthetic."""
import copy
import unittest

from financial_analysis.contracts import Observation
from risk_research.features import build
from risk_research.readiness import assess


def observation(metric, value, *, year=2024, unit="元", **overrides):
    flow = metric in ("revenue", "operating_cost", "net_profit_consolidated", "operating_cashflow")
    row = {"fact_id": f"fixture:{metric}:{year}", "entity_id": "fixture-A", "metric": metric,
           "value": value, "period": f"{year}年", "caliber": "合并", "currency": "CNY",
           "unit": unit, "state": "ok", "period_kind": "flow" if flow else "stock",
           "period_start": f"{year}-01-01" if flow else "", "period_end": f"{year}-12-31",
           "synthetic": True, "source": {"source_id": "synthetic-financial-fixture",
                                          "disclosed_at": f"{year + 1}-04-20"}}
    row.update(overrides)
    return row


def financial_rows():
    return [observation(metric, value) for metric, value in (
        ("revenue", 100), ("operating_cost", 60), ("net_profit_consolidated", 10),
        ("operating_cashflow", 12), ("accounts_receivable", 20), ("inventory", 15),
        ("total_liabilities", 40), ("total_assets", 80),
        ("available_cash", 18), ("interest_bearing_debt_due_within_one_year", 6))]


def label_record(index=0, *, task="credit", synthetic=False):
    day = index % 3 + 1
    key = "entity_id" if task == "credit" else "account_id"
    subject = f"fixture-{index % 2}"
    return {"record_id": f"fixture-label-{index}", key: subject,
            "decision_at": f"2024-01-0{day}", "synthetic": synthetic,
            "features": [{"name": "cash_ratio", "value": 1.2, key: subject,
                          "available_at": "2023-12-31"}], "label": index % 2,
            "label_available_at": "2024-04-01", "outcome_end": f"2024-02-0{day}",
            "label_definition": "synthetic confirmed outcome during observation window",
            "horizon_days": 31,
            "authorization": {"internal": True, "training": True,
                              "reference": "test fixture permission; no real customer"}}


class FinancialFeaturesTests(unittest.TestCase):
    def test_ratios_units_and_synthetic_boundary(self):
        rows = financial_rows()
        rows[0] = observation("revenue", .01, unit="万元")
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        values = {x["name"]: x["value"] for x in result["features"]}
        self.assertAlmostEqual(values["receivables_to_revenue"], .2)
        self.assertAlmostEqual(values["operating_cash_to_consolidated_profit"], 1.2)
        self.assertAlmostEqual(values["inventory_to_cost"], .25)
        self.assertEqual(values["liabilities_to_assets"], .5)
        self.assertEqual(values["cash_to_short_debt"], 3)
        self.assertEqual(result["status"], "synthetic_research_features")

    def test_missing_available_cash_cannot_use_cash_balance(self):
        rows = [r for r in financial_rows() if r["metric"] != "available_cash"]
        rows.append(observation("cash", 18))
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertNotIn("cash_to_short_debt", {x["name"] for x in result["features"]})
        self.assertTrue(any("available_cash" in x.get("metrics", []) for x in result["gaps"]))

    def test_missing_maturing_debt_not_generic_liabilities(self):
        rows = [r for r in financial_rows() if r["metric"] != "interest_bearing_debt_due_within_one_year"]
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertNotIn("cash_to_short_debt", {x["name"] for x in result["features"]})

    def test_nonpositive_profit_is_gap_not_zero_coverage(self):
        for value in (-2, 0):
            rows = financial_rows()
            rows[2]["value"] = value
            result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
            self.assertNotIn("operating_cash_to_consolidated_profit", {x["name"] for x in result["features"]})
        rows[2]["value"] = 10
        rows[3]["value"] = -5
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertEqual(next(x["value"] for x in result["features"]
                              if x["name"] == "operating_cash_to_consolidated_profit"), -.5)

    def test_entity_scope_currency_and_unit_rejected(self):
        for changes, code in (({"entity_id": "fixture-B"}, "entity_mismatch"),
                              ({"caliber": "母公司"}, "consolidated_scope_required"),
                              ({"unit": "吨"}, "amount_identity_or_unit_missing"),
                              ({"currency": "USD", "unit": "人民币元"}, "unit_currency_mismatch")):
            rows = financial_rows()
            rows[0].update(changes)
            result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
            self.assertTrue(any(x["code"] == code for x in result["gaps"]))
            self.assertNotIn("receivables_to_revenue", {x["name"] for x in result["features"]})

    def test_mixed_currency_is_not_converted(self):
        rows = financial_rows()
        rows[0].update(currency="USD", unit="USD")
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertTrue(any(x["code"] == "feature_input_scope_mismatch" for x in result["gaps"]))

    def test_snapshot_as_of_is_not_publication_proof(self):
        row = observation("revenue", 100)
        obj = Observation(**{k: v for k, v in row.items() if k not in ("source", "synthetic")},
                          as_of="2025-04-30", source_hash="fixture-hash")
        result = build([obj], entity_id="fixture-A", as_of="2025-05-01")
        self.assertFalse(result["features"])
        self.assertTrue(any(x["code"] == "public_availability_evidence_missing" for x in result["gaps"]))

    def test_future_publication_and_restatement_version_rejected(self):
        rows = financial_rows()
        rows[0]["source"]["version_available_at"] = "2026-01-01"
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertNotIn("receivables_to_revenue", {x["name"] for x in result["features"]})
        self.assertTrue(any(x["code"] == "not_available_at_cutoff" for x in result["gaps"]))

    def test_day_only_publication_does_not_invent_intraday_availability(self):
        rows = financial_rows()
        result = build(rows, entity_id="fixture-A", as_of="2025-04-20T12:00:00+08:00")
        self.assertFalse(result["features"])
        result = build(rows, entity_id="fixture-A", as_of="2025-04-20")
        self.assertEqual(len(result["features"]), 5)

    def test_top_level_import_metadata_and_hash_supported(self):
        rows = financial_rows()
        for row in rows:
            row["disclosed_at"] = row.pop("source")["disclosed_at"]
            row["source"] = "Wind school export"
            row["source_hash"] = "synthetic-csv-hash"
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertEqual(len(result["features"]), 5)

    def test_incompatible_flow_periods_and_conflicts_block(self):
        rows = financial_rows()
        rows[2]["period_start"] = "2024-07-01"
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertNotIn("operating_cash_to_consolidated_profit", {x["name"] for x in result["features"]})
        rows = financial_rows()
        rows.append(observation("revenue", 101, fact_id="conflicting-revenue"))
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertTrue(any(x["code"] == "conflicting_metric_period" for x in result["gaps"]))

    def test_growth_clues_have_alternative_explanations(self):
        rows = financial_rows() + [observation(metric, value, year=2023) for metric, value in (
            ("accounts_receivable", 10), ("revenue", 90), ("inventory", 10), ("operating_cost", 55))]
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertEqual(len(result["anomaly_clues"]), 2)
        clue = result["anomaly_clues"][0]
        self.assertAlmostEqual(clue["value"], 1 - (100 / 90 - 1))
        self.assertTrue(any("正常替代解释" in x for x in clue["limitations"]))
        self.assertEqual(len(clue["input_ids"]), 4)

    def test_input_hash_tracks_disclosure_and_values(self):
        rows = financial_rows()
        a = build(rows, entity_id="fixture-A", as_of="2025-05-01")["input_hash"]
        rows[0]["value"] = 101
        b = build(rows, entity_id="fixture-A", as_of="2025-05-01")["input_hash"]
        self.assertNotEqual(a, b)

    def test_ratio_overflow_is_gap_not_infinity(self):
        rows = financial_rows()
        rows[0]["value"] = "1e-300"
        rows[4]["value"] = "1e300"
        result = build(rows, entity_id="fixture-A", as_of="2025-05-01")
        self.assertNotIn("receivables_to_revenue", {x["name"] for x in result["features"]})
        self.assertTrue(any(x.get("feature") == "receivables_to_revenue" and
                            x["code"] == "derived_value_not_finite" for x in result["gaps"]))


class LabelReadinessTests(unittest.TestCase):
    def test_empty_label_data_is_waiting_not_execution_blocked(self):
        result = assess([], task="credit", evaluation_as_of="2025-06-30")
        self.assertEqual(result["status"], "waiting_labels")
        self.assertIn("authorized_real_labelled_records_required", result["requirements"])
        self.assertEqual(result["counts"]["total"], 0)

    def test_contract_eligible_cohort_not_model_acceptance(self):
        result = assess([label_record(i) for i in range(6)], task="credit", evaluation_as_of="2025-01-01")
        self.assertEqual(result["status"], "ready_for_offline_validation")
        self.assertEqual(result["counts"]["eligible"], 6)
        self.assertTrue(result["validation_contract"]["require_train_labels_known_before_validation_decision"])
        self.assertNotIn("score", result)

    def test_pending_outcomes_never_become_negative_labels(self):
        row = label_record()
        row["label"] = None
        result = assess([row], task="credit", evaluation_as_of="2025-01-01")
        self.assertEqual(result["status"], "waiting_labels")
        self.assertFalse(result["eligible_records"])

    def test_future_label_or_unmature_window_waits(self):
        for change in ({"label_available_at": "2026-01-01"}, {"outcome_end": "2026-01-01"}):
            row = label_record()
            row.update(change)
            result = assess([row], task="credit", evaluation_as_of="2025-01-01")
            self.assertEqual(result["status"], "waiting_labels")
            self.assertFalse(result["eligible_records"])

    def test_future_features_and_wrong_subject_excluded(self):
        for change, code in (({"available_at": "2024-01-02"}, "feature_not_known_at_decision"),
                             ({"entity_id": "fixture-other"}, "feature_subject_mismatch_or_missing"),
                             ({"version_available_at": "2024-12-01"}, "feature_not_known_at_decision")):
            row = label_record()
            row["features"][0].update(change)
            result = assess([row], task="credit", evaluation_as_of="2025-01-01")
            self.assertIn(code, result["excluded_records"][0]["reasons"])
            self.assertEqual(result["status"], "blocked")

    def test_training_authorization_is_explicit(self):
        row = label_record()
        row["authorization"]["training"] = False
        result = assess([row], task="credit", evaluation_as_of="2025-01-01")
        self.assertIn("internal_and_training_authorization_required", result["excluded_records"][0]["reasons"])

    def test_day_only_feature_date_cannot_imply_intraday_knowledge(self):
        row = label_record()
        row["decision_at"] = "2024-01-01T10:00:00+08:00"
        row["features"][0]["available_at"] = "2024-01-01"
        result = assess([row], task="credit", evaluation_as_of="2025-01-01")
        self.assertIn("feature_not_known_at_decision", result["excluded_records"][0]["reasons"])

    def test_synthetic_labels_only_prove_plumbing(self):
        rows = [label_record(i, synthetic=True) for i in range(6)]
        result = assess(rows, task="credit", evaluation_as_of="2025-01-01")
        self.assertEqual(result["status"], "synthetic_contract_only")
        self.assertEqual(len(result["synthetic_records"]), 6)
        self.assertFalse(result["eligible_records"])

    def test_labels_need_two_classes_groups_and_dates(self):
        rows = [label_record(i) for i in range(6)]
        for row in rows:
            row["label"] = 0
        result = assess(rows, task="credit", evaluation_as_of="2025-01-01")
        self.assertIn("both_label_classes_required", result["requirements"])
        result = assess([label_record()], task="credit", evaluation_as_of="2025-01-01")
        self.assertIn("multiple_subjects_required", result["requirements"])
        self.assertIn("at_least_three_decision_dates_required", result["requirements"])

    def test_fraud_account_contract_and_no_padding(self):
        rows = [label_record(i, task="fraud") for i in range(6)]
        result = assess(rows, task="fraud", evaluation_as_of="2025-01-01")
        self.assertEqual(result["status"], "ready_for_offline_validation")
        rows[0]["features"].append({"name": "new_feature", "value": 3, "account_id": "fixture-0",
                                   "available_at": "2023-12-31"})
        result = assess(rows, task="fraud", evaluation_as_of="2025-01-01")
        self.assertIn("consistent_feature_schema_required_no_zero_padding", result["requirements"])

    def test_horizon_and_duplicate_ids_rejected(self):
        row = label_record()
        row["horizon_days"] = 1
        result = assess([row], task="credit", evaluation_as_of="2025-01-01")
        self.assertIn("outcome_end_does_not_match_horizon_days", result["excluded_records"][0]["reasons"])
        row = label_record()
        result = assess([row, copy.deepcopy(row)], task="credit", evaluation_as_of="2025-01-01")
        self.assertIn("duplicate_record_id", result["excluded_records"][0]["reasons"])

    def test_negative_outcome_label_cannot_be_known_before_window_ends(self):
        row = label_record()
        row["label_available_at"] = "2024-01-15"
        result = assess([row], task="credit", evaluation_as_of="2025-01-01")
        self.assertIn("negative_label_available_before_outcome_end",
                      result["excluded_records"][0]["reasons"])
        row["label"] = 1
        result = assess([row], task="credit", evaluation_as_of="2025-01-01")
        self.assertEqual(len(result["eligible_records"]), 1)

    def test_arbitrary_horizon_text_cannot_replace_positive_integer_days(self):
        for value in (None, "31", True, 31.0, 0, -1):
            row = label_record()
            row["horizon_days"] = value
            row["horizon"] = "nonsense"
            result = assess([row], task="credit", evaluation_as_of="2025-01-01")
            self.assertIn("horizon_days_must_be_positive_integer",
                          result["excluded_records"][0]["reasons"])

    def test_unrepresentable_feature_cannot_enter_model_input(self):
        row = label_record()
        row["features"][0]["value"] = "1e400"
        result = assess([row], task="credit", evaluation_as_of="2025-01-01")
        self.assertIn("feature_value_missing_or_invalid", result["excluded_records"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
