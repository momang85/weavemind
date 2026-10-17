"""Only the new offline entry point: hand calculations and frozen replay boundaries."""
from __future__ import annotations

import copy
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from quant_research.study import run_study, write_study, verify_study, replay_study

ROOT = Path(__file__).resolve().parent
EXAMPLE = ROOT / "examples/research_study"


class ResearchStudyTest(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / "tmp/research-study-tests"
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.tmp.name)
        self.inputs = self.root / "inputs"
        shutil.copytree(EXAMPLE, self.inputs)
        self.spec = json.loads((self.inputs / "study.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_case(self, spec=None):
        return run_study(spec or self.spec, input_dir=self.inputs)

    def test_two_company_models_and_money_units_match_hand_calculation(self):
        result = self.run_case()
        expected = {"900001.SH": (1200 + 840) * 10000 / 365 * 10,
                    "900002.SH": (2200 + 1430) * 10000 / 365 * 10}
        for code, amount in expected.items():
            runs = result["financial"][code]["runs"]
            self.assertTrue(all(r["status"] == "validated" for r in runs), runs)
            cash = next(r for r in runs if r["model_id"] == "cash_pressure")
            output = next(o for o in cash["outputs"] if o["metric"] == "incremental_capital_occupation")
            self.assertAlmostEqual(output["value"], amount, places=2)  # declared yuan_2 rounding
            original = next(r for r in result["previews"]["financial"]["records"]
                            if r["entity_id"] == code and r["metric"] == "revenue")
            self.assertEqual(original["unit"], "万元")
            self.assertTrue(result["financial"][code]["unit_conversion"])
        clues = result["risk"]["900001.SH"]["anomaly_clues"]
        ar_clue = next(c for c in clues if c["name"] == "receivables_outpace_revenue")
        self.assertAlmostEqual(ar_clue["value"], .8 - .2)
        self.assertTrue(ar_clue["limitations"])
        self.assertFalse(any(g["code"] == "entity_mismatch"
                             for r in result["risk"].values() for g in r["gaps"]))

    def test_feature_cutoff_excludes_future_news_but_outcome_prices_are_kept(self):
        result = self.run_case()
        self.assertEqual(len(result["events"]["events"]), 2)
        self.assertTrue(result["events"]["excluded"])
        event_result = result["quant"]["event_returns"]
        self.assertEqual(event_result["status"], "ok")
        first = event_result["readings"][0]
        day_one = next(p for p in first["points"] if p["offset"] == 0)
        self.assertEqual(day_one["date"], "2025-04-28")
        self.assertAlmostEqual(day_one["return"], 10.55 / 10.45 - 1, places=6)
        self.assertTrue(any(point.get("date", "") > self.spec["feature_cutoff"]
                            for reading in event_result["readings"]
                            for point in reading["points"]))
        self.assertTrue(all(r["date"] <= self.spec["evaluation_as_of"]
                            for r in result["previews"]["market"]["records"]))

    def test_shared_assumptions_propagate_and_conflicts_are_rejected(self):
        spec = copy.deepcopy(self.spec)
        spec["financial"]["scenario_params"]["revenue_growth"] = .1
        r = self.run_case(spec)
        runs = r["financial"]["900001.SH"]["runs"]
        for model in ("scenario_sensitivity", "cash_pressure"):
            self.assertEqual(next(x for x in runs if x["model_id"] == model)["params"]["revenue_growth"], .1)
        spec["financial"]["params"]["cash_pressure"]["revenue_growth"] = .2
        with self.assertRaisesRegex(ValueError, "共享假设"):
            self.run_case(spec)

    def test_internal_freeze_replay_without_original_source_files(self):
        result = self.run_case()
        out = write_study(result, output_dir=self.root / "output")
        self.assertTrue(verify_study(out)["ok"])
        # The original inputs are genuinely absent when replay recomputes the model.
        moved = self.root / "originals-away"
        self.inputs.rename(moved)
        self.assertTrue(replay_study(out)["ok"])

    def test_changed_parameters_make_new_version_and_cannot_overwrite_old(self):
        first = self.run_case()
        out = write_study(first, output_dir=self.root / "output")
        original_summary = (out / "summary.md").read_bytes()
        with self.assertRaises(FileExistsError):
            write_study(first, output_dir=self.root / "output")
        spec = copy.deepcopy(self.spec)
        spec["financial"]["params"]["cash_pressure"]["collection_days_delta"] = 15
        second = self.run_case(spec)
        self.assertNotEqual(first["study_id"], second["study_id"])
        self.assertNotEqual(first["result_hash"], second["result_hash"])
        write_study(second, output_dir=self.root / "output")
        self.assertEqual((out / "summary.md").read_bytes(), original_summary)

    def test_tampered_frozen_inputs_fail_before_replay(self):
        out = write_study(self.run_case(), output_dir=self.root / "output")
        with (out / "inputs.json").open("a", encoding="utf-8") as f:
            f.write(" ")
        self.assertFalse(verify_study(out)["ok"])
        self.assertFalse(replay_study(out)["ok"])

    def test_missing_domains_do_not_generate_returns_or_risk_scores(self):
        r = self.run_case({"feature_cutoff": "2025-04-30"})
        self.assertEqual(r["quant"]["event_returns"]["status"], "waiting_data")
        self.assertEqual(r["label_readiness"]["credit"]["status"], "waiting_labels")
        self.assertEqual(r["financial"], {})
        self.assertEqual(r["risk"], {})

    def test_unregistered_future_model_is_a_gap_not_an_uncaught_exception(self):
        spec = copy.deepcopy(self.spec)
        spec["financial"]["models"] = ["future_unregistered_model"]
        r = self.run_case(spec)
        self.assertEqual(r["financial"]["900001.SH"]["runs"][0]["status"], "unavailable")

    def test_invalid_market_prices_stay_out_of_existing_calculators(self):
        with (self.inputs / "daily_prices.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            row["open"] = "-1"
        with (self.inputs / "daily_prices.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        r = self.run_case()
        self.assertTrue(r["excluded"]["market"])
        self.assertEqual(r["quant"]["event_returns"]["status"], "unavailable")
        self.assertEqual(r["quant"]["backtest"]["status"], "unavailable")

    def test_synthetic_identity_and_licence_are_preserved_in_standalone_outputs(self):
        r = self.run_case()
        self.assertTrue(r["manifest"]["synthetic"])
        self.assertFalse(r["manifest"]["external_delivery"]["allowed"])
        self.assertTrue(r["events"]["synthetic"])
        self.assertTrue(all(x["synthetic"] for x in r["financial"].values()))
        self.assertTrue(all(x["status"] == "synthetic_research_features" for x in r["risk"].values()))
        self.assertTrue(all(not x["delivery_eligible"] for x in r["quant"].values()))
        self.assertEqual(r["label_readiness"]["credit"]["status"], "synthetic_contract_only")
        self.assertIn("模拟数据", r["summary_markdown"])

    def test_precise_version_available_time_excludes_restatement(self):
        with (self.inputs / "financials.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            row["version_available_at"] = "2025-05-01"
        with (self.inputs / "financials.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        r = self.run_case()
        self.assertTrue(r["excluded"]["financial"])
        self.assertTrue(all(x["status"] == "missing_input"
                            for company in r["financial"].values() for x in company["runs"]))

    def test_explicit_file_boundary_and_boolean_markers(self):
        spec = copy.deepcopy(self.spec)
        spec["events"]["file"] = "../outside.json"
        with self.assertRaisesRegex(ValueError, "input_dir"):
            self.run_case(spec)
        spec = copy.deepcopy(self.spec)
        spec["synthetic"] = "false"
        with self.assertRaisesRegex(ValueError, "布尔"):
            self.run_case(spec)

    def _q1_fixture(self, evaluation_as_of: str) -> dict:
        """Q1 夹具：两公司费用项 + 900001 一条 2025 年度行（2026-04-28 披露）；幂等，可重复调用。"""
        with (self.inputs / "financials.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        extras = {("900001.SH", "2023-12-31"): (50, 40, 30, 10),
                  ("900001.SH", "2024-12-31"): (60, 45, 35, 12),
                  ("900002.SH", "2023-12-31"): (80, 70, 60, 20),
                  ("900002.SH", "2024-12-31"): (90, 75, 65, 25)}
        new_rows = []
        for row in rows:
            row = dict(row)
            key = (row["证券代码"], row["报告期"])
            if key in extras:
                selling, admin, rd, taxes = extras[key]
                row.update({"销售费用": selling, "管理费用": admin,
                            "研发费用": rd, "税金及附加": taxes})
                new_rows.append(row)
            elif key != ("900001.SH", "2025-12-31"):
                new_rows.append(row)
        late = next(dict(r) for r in new_rows
                    if (r["证券代码"], r["报告期"]) == ("900001.SH", "2024-12-31"))
        late.update({"报告期": "2025-12-31", "披露日期": "2026-04-28", "下载日期": "2026-04-28",
                     "营业收入": 900, "营业成本": 650, "归母净利润": 80, "合并净利润": 90,
                     "经营活动产生的现金流量净额": 70, "应收账款": 110, "存货": 120,
                     "应付账款": 70, "总资产": 2200, "总负债": 1200,
                     "销售费用": 55, "管理费用": 50, "研发费用": 40, "税金及附加": 15})
        new_rows.append(late)
        with (self.inputs / "financials.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(new_rows[0]))
            writer.writeheader()
            writer.writerows(new_rows)
        spec = copy.deepcopy(self.spec)
        spec["evaluation_as_of"] = evaluation_as_of
        spec["q1"] = {"target_year": "2024", "split_at": "2025-04-30",
                      "provenance_kind": "历史重建",
                      "companies": {"900001.SH": {"company_primary_cause": "夹具主因甲"},
                                    "900002.SH": {"company_primary_cause": "夹具主因乙"}}}
        return spec

    def test_q1_block_freezes_2024_card_and_gates_followup_by_evaluation_as_of(self):
        spec = self._q1_fixture("2026-06-30")
        result = self.run_case(spec)
        q1 = result["q1"]
        self.assertEqual((q1["target_year"], q1["split_at"]), ("2024", "2025-04-30"))
        card1 = q1["cards"]["900001.SH"]
        # 手算（万元）：E_2023=300−130=170；E_2024=360−152=208；E_2025=250−160=90。
        self.assertEqual(card1["readings"]["t"], "2024")
        self.assertAlmostEqual(float(card1["readings"]["o_t"]), .038, places=10)
        self.assertEqual(card1["judgement"]["expected_direction"], "偏改善（sign(Y_op) = 正）")
        follow = card1["follow_up"]
        self.assertEqual((follow["status"], follow["mechanism_verdict"]), ("已核验", "削弱"))
        self.assertEqual(follow["verify_by"], "2026-04-28")
        # 2025 结果（Y_op=−118/1200 负）只进后续层：冻结判断不回改。
        self.assertEqual(card1["judgement"]["expected_direction"], "偏改善（sign(Y_op) = 正）")
        card2 = q1["cards"]["900002.SH"]
        self.assertAlmostEqual(float(card2["readings"]["o_t"]), .0725, places=10)
        self.assertEqual((card2["follow_up"]["status"], card2["follow_up"]["mechanism_verdict"]),
                         ("待数据", "未决"))
        self.assertEqual(q1["evaluation"]["increment"]["verdict"], "no_evidence")
        self.assertFalse(q1["evaluation"]["split"]["holdout"])
        out = write_study(result, output_dir=self.root / "output")
        self.assertIn("q1.json", verify_study(out)["checked"])
        self.assertTrue(replay_study(out)["ok"])

    def test_base_fingerprint_binds_base_substance_not_study_or_evaluation(self):
        # 衔接点B（B3 首例）反例：仅推进 evaluation_as_of ⇒ 基期实质指纹不变、card_hash 变；
        # 指纹排除 follow_up/评价截止/研究ID（无环形哈希），独立续页引用同一指纹，原版不覆盖。
        base = self._q1_fixture("2026-06-30")
        r1 = self.run_case(copy.deepcopy(base))
        self._q1_fixture("2026-10-09")
        r2 = self.run_case(copy.deepcopy(self.spec) | {"evaluation_as_of": "2026-10-09",
                                                       "q1": base["q1"]})
        fp1 = r1["q1"]["cards"]["900001.SH"]["identity"]["base_fingerprint"]
        fp2 = r2["q1"]["cards"]["900001.SH"]["identity"]["base_fingerprint"]
        self.assertTrue(fp1)
        self.assertEqual(fp1, fp2)
        self.assertNotEqual(r1["q1"]["cards"]["900001.SH"]["card_hash"],
                            r2["q1"]["cards"]["900001.SH"]["card_hash"])
        self.assertNotEqual(fp1, r1["q1"]["cards"]["900001.SH"]["card_hash"])
        # 指纹绑定基期实质：feature_cutoff 收紧到 2024 年报披露前 ⇒ 基期读数变化 ⇒ 指纹变。
        cut = self._q1_fixture("2026-06-30")
        cut["feature_cutoff"] = "2025-04-24"
        r3 = self.run_case(cut)
        self.assertNotEqual(r3["q1"]["cards"]["900001.SH"]["identity"]["base_fingerprint"], fp1)
        # 独立续页与 followups 均引用该指纹。
        out1 = write_study(r1, output_dir=self.root / "out1")
        fu_md = (out1 / "q1_followup_900001.SH.md").read_text(encoding="utf-8")
        self.assertIn(fp1, fu_md)
        self.assertIn("基期实质指纹", fu_md)
        self.assertEqual(r2["q1"]["followups"]["900001.SH"]["base_fingerprint"], fp1)

    def test_q1_block_requires_explicit_year_and_companies(self):
        spec = copy.deepcopy(self.spec)
        spec["q1"] = {"target_year": "24", "split_at": "2025-04-30",
                      "companies": {"900001.SH": {}}}
        with self.assertRaisesRegex(ValueError, "target_year"):
            self.run_case(spec)
        spec = copy.deepcopy(self.spec)
        spec["q1"] = {"target_year": "2024", "split_at": "2025-04-30", "companies": {}}
        with self.assertRaisesRegex(ValueError, "companies"):
            self.run_case(spec)
        spec = copy.deepcopy(self.spec)
        spec["q1"] = True
        with self.assertRaisesRegex(ValueError, "显式"):
            self.run_case(spec)


if __name__ == "__main__":
    unittest.main()
