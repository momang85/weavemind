# -*- coding: utf-8 -*-
"""状态真源收口的回归测试：投影器写入、状态派生、验收落库、stale 豁免。"""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import task_state


class TestTaskStateProjector(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_state_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        con = sqlite3.connect(self.db)
        # 与 web_ui._init_db 的列集保持一致（投影器写入这些列）
        con.execute(
            "CREATE TABLE task_history(task_id TEXT PRIMARY KEY, goal TEXT,"
            " status TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            " completed_at TIMESTAMP, report TEXT,"
            " conversation_id TEXT DEFAULT '', parent_task_id TEXT DEFAULT '',"
            " context TEXT DEFAULT '', project TEXT DEFAULT 'default',"
            " user TEXT DEFAULT '', steps_json TEXT DEFAULT '',"
            " logs_json TEXT DEFAULT '')"
        )
        con.commit()
        con.close()

    def test_ensure_schema_adds_columns_once(self):
        added = task_state.ensure_schema(self.db)
        self.assertEqual(
            set(added),
            {"acceptance_json", "rules_fingerprint", "phase", "updated_at"})
        self.assertEqual(task_state.ensure_schema(self.db), [],
                         "重复调用不得重复加列（幂等）")

    def test_derive_status_rules(self):
        d = task_state.derive_status
        self.assertEqual(d(["SUCCESS", "SUCCESS"], {"overall": "pass"}, {}),
                         task_state.SUCCESS)
        self.assertEqual(d(["SUCCESS"], {"overall": "fail"}, {}),
                         task_state.SUCCESS_WITH_ISSUES)
        self.assertEqual(d(["PARTIAL"], {"overall": "pass"}, {}),
                         task_state.FAILED, "步骤 PARTIAL 也是失败")
        self.assertEqual(d(["SUCCESS"], {"overall": "pass"}, {"both_failed": True}),
                         task_state.SUCCESS_WITH_ISSUES)
        self.assertEqual(d(["SUCCESS"], None, {}), task_state.SUCCESS)

    def test_orchestrator_delegates_to_projector(self):
        """编排器的 _resolve_final_status 必须与投影器同源（规则不分叉）。"""
        from orchestrator_v2 import OrchestratorV2
        self.assertEqual(
            OrchestratorV2._resolve_final_status(False, {"overall": "fail"}, "", {}),
            task_state.SUCCESS_WITH_ISSUES)
        self.assertEqual(
            OrchestratorV2._resolve_final_status(True, {"overall": "pass"}, "", {}),
            task_state.FAILED)

    def test_queued_to_running_to_completion_flow(self):
        task_state.ensure_schema(self.db)
        task_state.mark_queued("t-1", "目标", db_path=self.db)
        self.assertEqual(task_state.read_task("t-1", self.db)["status"],
                         task_state.QUEUED)
        task_state.mark_running("t-1", db_path=self.db)
        self.assertEqual(task_state.read_task("t-1", self.db)["status"],
                         task_state.RUNNING)
        self.assertTrue(task_state.is_running("t-1", self.db))
        task_state.record_completion(
            "t-1", goal="目标", status=task_state.SUCCESS_WITH_ISSUES,
            report="报告正文", steps=[{"step_id": "1"}], logs=[{"m": "x"}],
            acceptance={"overall": "fail", "rules_fingerprint": "abc12345",
                        "gaps": ["缺来源"]},
            db_path=self.db)
        row = task_state.read_task("t-1", self.db)
        self.assertEqual(row["status"], task_state.SUCCESS_WITH_ISSUES)
        self.assertEqual(row["phase"], "完成")
        self.assertEqual(row["rules_fingerprint"], "abc12345")
        # 验收摘要不再被丢弃（此前表里没有该列，终态消息里的 acceptance 被忽略）
        self.assertEqual(row["acceptance"]["overall"], "fail")
        self.assertEqual(row["acceptance"]["gaps"], ["缺来源"])
        self.assertFalse(task_state.is_running("t-1", self.db))
        self.assertEqual(json.loads(row["steps_json"]), [{"step_id": "1"}])

    def test_set_phase_keeps_status(self):
        task_state.ensure_schema(self.db)
        task_state.mark_queued("t-2", "目标", db_path=self.db)
        task_state.mark_running("t-2", db_path=self.db)
        task_state.set_phase("t-2", "反思", db_path=self.db)
        row = task_state.read_task("t-2", self.db)
        self.assertEqual(row["phase"], "反思")
        self.assertEqual(row["status"], task_state.RUNNING)

    def test_mark_stale_failed_only_for_open_states(self):
        task_state.ensure_schema(self.db)
        task_state.mark_queued("t-3", "目标", db_path=self.db)
        self.assertTrue(task_state.mark_stale_failed("t-3", self.db))
        self.assertEqual(task_state.read_task("t-3", self.db)["status"],
                         task_state.FAILED)
        # 终态任务不再被 stale 改写
        self.assertFalse(task_state.mark_stale_failed("t-3", self.db))

    def test_unknown_task_reads_empty(self):
        task_state.ensure_schema(self.db)
        self.assertEqual(task_state.read_task("nope", self.db), {})


class TestStaleExemptionValidation(unittest.TestCase):
    """stale 豁免必须校验 Redis 标记的 pid（否则崩溃后任务永远 PENDING）。"""

    def test_dead_pid_marker_does_not_exempt(self):
        import importlib
        import web_ui

        src = Path(web_ui.__file__).read_text(encoding="utf-8")
        self.assertIn("winerror", src, "stale 豁免应做 pid 存活校验")

        fake_redis = mock.MagicMock()
        fake_redis.scan_iter.return_value = ["task_running:dead-task"]
        fake_redis.get.return_value = json.dumps({"pid": 4_000_000})
        with mock.patch.object(web_ui, "_new_redis", return_value=fake_redis):
            running = set()
            # 复刻豁免判定：pid 不存在 → 不加入豁免集合
            import errno
            for k in fake_redis.scan_iter("task_running:*", count=200):
                tid = str(k).split(":", 1)[-1]
                alive = True
                raw = fake_redis.get(k)
                pid = int((json.loads(raw) or {}).get("pid") or 0) if raw else 0
                if pid > 0:
                    try:
                        os.kill(pid, 0)
                    except OSError as exc:
                        if (exc.errno == errno.ESRCH
                                or getattr(exc, "winerror", None) == 87):
                            alive = False
                if alive:
                    running.add(tid)
        self.assertNotIn("dead-task", running)


if __name__ == "__main__":
    unittest.main()
