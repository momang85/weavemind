import json
import tempfile
import unittest
from pathlib import Path

from report_version import VersionStore


class TestHumanReviewBinding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wm_human_binding_")
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.task_id = "human-binding"
        self.store = VersionStore(self.workspace, self.task_id)
        self.version = self.store.record("已完成的研究正文。", sources_fingerprint="source-a")
        self.store.adopt(self.version)
        self.review_path = self.workspace / "human_review.json"

    def project(self, version_id, *, approver="研究员甲"):
        import web_ui
        self.review_path.write_text(json.dumps({
            "status": "recorded", "approver": approver,
            "at": "2026-10-21T20:00:00+08:00", "version_id": version_id,
        }, ensure_ascii=False), encoding="utf-8")
        before = self.review_path.read_bytes()
        payload = web_ui._research_payload(self.task_id, self.workspace)
        self.assertEqual(self.review_path.read_bytes(), before)
        return payload["review"]["human"]

    def test_matching_full_identity_is_reviewed(self):
        human = self.project(self.version.identity_id())
        self.assertEqual(human["status"], "recorded")
        self.assertTrue(human["bound"])

    def test_new_adoption_does_not_inherit_old_approval(self):
        replacement = self.store.record("修订后的研究正文。")
        self.store.adopt(replacement)
        human = self.project(self.version.identity_id())
        self.assertEqual(human["status"], "pending")
        self.assertFalse(human["bound"])
        self.assertEqual(human["approver"], "研究员甲")
        self.assertIn("不属于当前采纳版", human["reason"])

    def test_same_body_with_new_sources_is_not_approved(self):
        replacement = self.store.record(self.version.body, sources_fingerprint="source-b")
        self.store.adopt(replacement)
        self.assertEqual(replacement.version_id, self.version.version_id)
        human = self.project(self.version.identity_id())
        self.assertEqual(human["status"], "pending")
        self.assertFalse(human["bound"])

    def test_missing_short_or_body_only_identity_is_not_approved(self):
        for identity in ("", self.version.identity_id()[:12], self.version.version_id):
            with self.subTest(identity=identity):
                human = self.project(identity)
                self.assertEqual(human["status"], "pending")
                self.assertFalse(human["bound"])

    def test_no_adopted_version_cannot_be_approved(self):
        self.store.path.unlink()
        human = self.project(self.version.identity_id())
        self.assertEqual(human["status"], "pending")
        self.assertFalse(human["bound"])

    def test_anonymous_approval_is_not_approved(self):
        human = self.project(self.version.identity_id(), approver=" ")
        self.assertEqual(human["status"], "pending")
        self.assertFalse(human["bound"])


if __name__ == "__main__":
    unittest.main()
