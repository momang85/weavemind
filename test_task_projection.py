# -*- coding: utf-8 -*-
"""C 包回归：DB 投影 + Redis 运行期快照，把 `_task_results` 降级为纯缓存。

关键场景：服务重启（内存清空）后，运行中任务的计划树/日志、终态任务的验收缺口
都必须还能读到；否则前端会退化成一直显示 "Planning (LLM thinking)…"。
"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import task_state


class TestTaskProjection(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_proj_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        con = sqlite3.connect(self.db)
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

    def test_projection_of_terminal_task(self):
        task_state.ensure_schema(self.db)
        task_state.mark_queued("t-1", "目标", db_path=self.db)
        steps = [{"step_id": "1", "capability": "web_search",
                  "result": {"status": "SUCCESS"}}]
        logs = [{"message": "step 1 ok"}]
        acceptance = {"overall": "fail", "gaps": ["缺来源"], "rules_fingerprint": "abc12345"}
        task_state.record_completion(
            "t-1", goal="目标", status=task_state.SUCCESS_WITH_ISSUES, report="报告正文",
            steps=steps, logs=logs, acceptance=acceptance, db_path=self.db)
        proj = task_state.task_projection("t-1", self.db)
        self.assertEqual(proj["status"], task_state.SUCCESS_WITH_ISSUES)
        self.assertEqual(proj["report"], "报告正文")
        self.assertEqual(proj["steps"], steps)
        self.assertEqual(proj["logs"], logs)
        # 验收成为对象（此前从 DB 兜底时恒为 None，前端看不到缺口）
        self.assertEqual(proj["acceptance"]["overall"], "fail")
        self.assertEqual(proj["acceptance"]["gaps"], ["缺来源"])
        self.assertEqual(proj["rules_fingerprint"], "abc12345")

    def test_projection_of_running_task_has_empty_live_fields(self):
        """运行中：DB 只有状态与阶段；计划树/日志由快照提供（本层返回空）。"""
        task_state.ensure_schema(self.db)
        task_state.mark_queued("t-2", "目标", db_path=self.db)
        task_state.mark_running("t-2", db_path=self.db)
        proj = task_state.task_projection("t-2", self.db)
        self.assertEqual(proj["status"], task_state.RUNNING)
        self.assertEqual(proj["steps"], [])
        self.assertEqual(proj["logs"], [])

    def test_projection_tolerates_corrupt_json(self):
        task_state.ensure_schema(self.db)
        task_state.mark_queued("t-3", "目标", db_path=self.db)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE task_history SET steps_json=?, logs_json=? WHERE task_id=?",
                    ("{not json", "[also broken", "t-3"))
        con.commit()
        con.close()
        proj = task_state.task_projection("t-3", self.db)
        self.assertEqual(proj["steps"], [])
        self.assertEqual(proj["logs"], [])

    def test_projection_unknown_task_is_empty(self):
        task_state.ensure_schema(self.db)
        self.assertEqual(task_state.task_projection("nope", self.db), {})


class TestSnapshotAndOverlay(unittest.TestCase):
    """快照读写与叠加优先级（Redis 不可用时回退内存 overlay，不得抛错）。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_snap_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        con = sqlite3.connect(self.db)
        con.execute(
            "CREATE TABLE task_history(task_id TEXT PRIMARY KEY, goal TEXT, status TEXT,"
            " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP,"
            " report TEXT, conversation_id TEXT DEFAULT '', parent_task_id TEXT DEFAULT '',"
            " context TEXT DEFAULT '', project TEXT DEFAULT 'default', user TEXT DEFAULT '',"
            " steps_json TEXT DEFAULT '', logs_json TEXT DEFAULT '')"
        )
        con.commit()
        con.close()
        task_state.ensure_schema(self.db)
        task_state.mark_queued("t-9", "目标", db_path=self.db)
        task_state.mark_running("t-9", db_path=self.db)

    def test_snapshot_roundtrip_with_redis(self):
        live = {"steps": [{"step_id": "1", "status": "RUNNING"}],
                "logs": [{"message": "规划中"}], "revision": True}
        if not task_state.write_snapshot("t-9", live, ttl=60):
            self.skipTest("Redis 不可用，跳过快照往返")
        try:
            got = task_state.read_snapshot("t-9")
            self.assertEqual(got["steps"], live["steps"])
            self.assertEqual(got["logs"], live["logs"])
            self.assertTrue(got["revision"])
        finally:
            task_state.drop_snapshot("t-9")

    def test_merge_projection_prefers_snapshot_for_live_fields(self):
        snap = {"steps": [{"step_id": "1", "status": "RUNNING"}],
                "logs": [{"message": "来自快照"}], "revision": True}
        with mock.patch.object(task_state, "read_snapshot", return_value=snap):
            merged = task_state.merge_projection("t-9", db_path=self.db)
        self.assertEqual(merged["status"], task_state.RUNNING)   # 事实层来自 DB
        self.assertEqual(merged["logs"][0]["message"], "来自快照")
        self.assertTrue(merged["steps"])
        self.assertTrue(merged["revision"])

    def test_merge_projection_falls_back_to_memory_overlay(self):
        with mock.patch.object(task_state, "read_snapshot", return_value={}):
            merged = task_state.merge_projection(
                "t-9", overlay={"steps": [{"step_id": "2"}], "logs": [{"message": "内存"}]},
                db_path=self.db)
        self.assertEqual(merged["logs"][0]["message"], "内存")

    def test_terminal_facts_win_over_stale_snapshot(self):
        """终态事实以 DB 为准：过期快照不得把已完成任务显示成运行中。"""
        task_state.record_completion("t-9", goal="目标", status=task_state.SUCCESS,
                                     report="ok", steps=[], logs=[], db_path=self.db)
        with mock.patch.object(task_state, "read_snapshot",
                               return_value={"status": "RUNNING", "logs": [{"message": "旧"}]}):
            merged = task_state.merge_projection("t-9", db_path=self.db)
        self.assertEqual(merged["status"], task_state.SUCCESS)


class TestWebuiProjectionFirst(unittest.TestCase):
    """webui 读取点：内存为空（模拟重启）时仍能给出计划树/日志/验收。"""

    def _fake_handler(self, captured):
        class _H:
            def _json(self, data, code=200, extra_headers=None):
                captured.update(data=data, code=code)
        return _H()

    def test_task_page_survives_empty_memory(self):
        import web_ui
        captured = {}
        task = {
            "task_id": "t-x", "status": "SUCCESS_WITH_ISSUES", "goal": "目标",
            "steps": [{"step_id": "1"}], "report": "正文", "logs": [{"message": "ok"}],
            "project": "default", "revision": False,
            "acceptance": {"overall": "fail", "gaps": ["缺来源"]}, "llm_degraded": None,
        }
        with mock.patch.object(web_ui, "_task_results", {}), \
                mock.patch("task_state.merge_projection", return_value=task):
            web_ui._get_task_page(self._fake_handler(captured), "/task/t-x")
        self.assertEqual(captured["data"]["status"], "SUCCESS_WITH_ISSUES")
        self.assertEqual(captured["data"]["steps"], [{"step_id": "1"}])
        self.assertEqual(captured["data"]["acceptance"]["gaps"], ["缺来源"])

    def test_merge_progress_message_handles_acceptance_and_warning(self):
        """此前 acceptance / warning 两类推送没有分支，整条消息被静默丢弃。"""
        import web_ui
        existing = {"logs": [], "steps": []}
        web_ui._merge_progress_message(existing, {
            "type": "acceptance",
            "payload": {"acceptance": {"overall": "fail", "gaps": ["x"]}},
        })
        self.assertEqual(existing["acceptance"]["overall"], "fail")
        web_ui._merge_progress_message(existing, {
            "type": "warning",
            "payload": {"message": "阶段停滞", "agent": "orchestrator"},
        })
        self.assertEqual(existing["logs"][-1]["type"], "warning")
        self.assertEqual(existing["logs"][-1]["message"], "阶段停滞")

    def test_listener_writes_snapshot_after_merge(self):
        """监听器合并后必须写快照（这是运行期实时态在重启后可重建的唯一来源）。"""
        src = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertIn("write_snapshot(tid, existing)", src)
        self.assertIn("merge_projection", src)

    def test_stale_exemption_no_longer_reads_memory(self):
        """内存已降级为缓存：stale 豁免不得再以内存 RUNNING 为真源。"""
        src = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertNotIn('str(st.get("status") or "").upper() == "RUNNING"', src)


if __name__ == "__main__":
    unittest.main()
