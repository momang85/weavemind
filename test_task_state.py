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
    """stale 豁免/崩溃兜底必须用可靠的探活判定，不能把报错当成"已死"。

    回归背景：旧实现把 `os.kill(pid, 0)` 的 WinError 87 当"进程已死"，而实测
    该错误对**存活进程**也会出现，于是正在跑的任务被翻成 FAILED、真实报告被覆写。
    """

    def test_liveness_contract(self):
        import web_ui

        src = Path(web_ui.__file__).read_text(encoding="utf-8")
        self.assertIn("_pid_same_process", src, "存活判定必须走统一的探活函数")
        self.assertNotIn("winerror", src, "不得再用 winerror 87 作为'已死'判据")
        # 存活进程（本进程）必须判存活
        self.assertIs(web_ui._pid_same_process(os.getpid(), ""), True)
        # psutil 报"进程不存在"才判死
        import psutil
        with mock.patch.object(psutil, "Process",
                               side_effect=psutil.NoSuchProcess(4_000_000)):
            self.assertIs(web_ui._pid_same_process(4_000_000, ""), False)


if __name__ == "__main__":
    unittest.main()
