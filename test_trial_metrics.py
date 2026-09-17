# -*- coding: utf-8 -*-
"""S3：试用最小指标的回归——**未知不当 0**、不推断、匿名。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import trial_metrics as T  # noqa: E402


class TestUnknownIsNotZero(unittest.TestCase):
    def test_missing_measurement_requires_reason(self):
        m = T.Measurement()
        self.assertFalse(m.known)
        self.assertTrue(m.reason, "未采集必须说明原因")
        self.assertIsNone(m.as_dict()["value"])
        self.assertFalse(m.as_dict()["known"])

    def test_summary_counts_unknowns_separately(self):
        records = [
            T.TrialRecord(trial_id="t1", category="trial",
                          time_to_reviewable_report_sec=T.Measurement(120.0),
                          human_edited_fields=T.Measurement(3.0),
                          cost_kind=T.COST_KNOWN, cost_usd=T.Measurement(0.42)),
            T.TrialRecord(trial_id="t2", category="trial",
                          human_edited_fields=T.Measurement(None, "未复核"),
                          cost_kind=T.COST_UNKNOWN,
                          cost_usd=T.Measurement(None, "端点未返回用量")),
        ]
        s = T.summarize(records)
        self.assertEqual(s["trials"], 2)
        m = s["metrics"]["human_edited_fields"]
        self.assertEqual(m["known"], 1)
        self.assertEqual(m["unknown"], 1, "未知单列，不能当 0 参与统计")
        self.assertEqual(m["median"], 3.0)
        t = s["metrics"]["time_to_reviewable_report_sec"]
        self.assertEqual(t["known"], 1)
        self.assertEqual(t["unknown"], 1)
        self.assertEqual(t["median"], 120.0)
        # 费用：未知不并入总额
        self.assertEqual(s["cost"]["known"], 1)
        self.assertEqual(s["cost"]["unknown"], 1)
        self.assertAlmostEqual(s["cost"]["usd_total"], 0.42, places=6)

    def test_all_unknown_gives_no_median_and_null_total(self):
        s = T.summarize([T.TrialRecord(trial_id="t1")])
        self.assertIsNone(s["metrics"]["human_edited_fields"]["median"])
        self.assertEqual(s["metrics"]["human_edited_fields"]["known"], 0)
        self.assertIsNone(s["cost"]["usd_total"],
                          "一次都没测到金额时总额是未知（None），不是 0")

    def test_required_metrics_coverage_uses_known_samples_only(self):
        records = [
            T.TrialRecord(trial_id="a",
                          required_metrics_present=T.Measurement(6.0),
                          required_metrics_total=T.Measurement(6.0)),
            T.TrialRecord(trial_id="b",
                          required_metrics_present=T.Measurement(4.0),
                          required_metrics_total=T.Measurement(6.0)),
            T.TrialRecord(trial_id="c"),          # 没底稿 → 不参与比例
        ]
        cov = T.summarize(records)["metrics"]["required_metrics_coverage"]
        self.assertEqual(cov["known"], 2)
        self.assertEqual(cov["unknown"], 1)
        self.assertAlmostEqual(cov["median"], (1.0 + 4 / 6) / 2, places=6)


class TestCategoryAndIdentity(unittest.TestCase):
    def test_unknown_category_is_not_trial(self):
        self.assertEqual(T.normalize_category(""), T.CATEGORY_UNKNOWN)
        self.assertEqual(T.normalize_category("真实试用"), T.CATEGORY_UNKNOWN,
                         "认不出的类别记 unknown，不默认成真实试用")
        self.assertEqual(T.normalize_category("trial"), "trial")

    def test_trial_id_is_anonymous_and_unique(self):
        ids = {T.new_trial_id() for _ in range(20)}
        self.assertEqual(len(ids), 20)
        for i in ids:
            self.assertTrue(i.startswith("trial-"))
            self.assertNotIn("@", i)

    def test_record_has_no_user_identity_field(self):
        fields = set(T.TrialRecord(trial_id="t").as_dict())
        for banned in ("user", "username", "email", "ip"):
            self.assertNotIn(banned, fields, "试用记录不得包含身份字段")


class TestRecordFromTask(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_trial_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_reads_completeness_from_working_paper(self):
        import workspace as ws_mod
        old = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old)
        proj = ws_mod.task_project_dir("trial-task", "default")
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "working_paper.json").write_text(
            '{"ok": true, "completeness": {"present": 6, "required": 6}}',
            encoding="utf-8")
        rec = T.record_from_task("trial-task", category="trial")
        self.assertEqual(rec.required_metrics_present.value, 6.0)
        self.assertEqual(rec.required_metrics_total.value, 6.0)
        # 需人判断的项保持未知并写明原因
        self.assertIsNone(rec.wrong_certifications.value)
        self.assertTrue(rec.wrong_certifications.reason)
        self.assertEqual(rec.cost_kind, T.COST_UNKNOWN)

    def test_no_paper_means_unknown_not_zero(self):
        import workspace as ws_mod
        old = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old)
        rec = T.record_from_task("no-paper-task", category="demo")
        self.assertIsNone(rec.required_metrics_present.value)
        self.assertEqual(rec.required_metrics_present.reason,
                         "该任务没有底稿（无结构化财务）")


class TestAppendAndLoad(unittest.TestCase):
    def test_roundtrip_jsonl(self):
        root = Path(tempfile.mkdtemp(prefix="wm_trialjson_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        rec = T.TrialRecord(trial_id="t-1", category="trial", task_id="ui-x",
                            cost_kind=T.COST_ESTIMATED,
                            cost_usd=T.Measurement(0.1, "按 token 估算"))
        T.append_record(rec, root=root)
        back = T.load_records(root=root)
        self.assertEqual(len(back), 1)
        self.assertEqual(back[0].trial_id, "t-1")
        self.assertEqual(back[0].cost_kind, T.COST_ESTIMATED)
        self.assertTrue(back[0].cost_usd.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
