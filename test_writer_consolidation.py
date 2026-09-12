# -*- coding: utf-8 -*-
"""B 包回归：状态写者收口——编排器唯一写者 + 提交收执 + 崩溃兜底。

行为变更（已确认）：编排器不在时提交**直接失败**（503），不再先写一行 QUEUED
等过期；崩溃留下的 RUNNING 由看护线程按"运行标记消失 + 年龄门槛"翻 FAILED。
"""

import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import task_state


def _mk_db(path: str) -> None:
    """建最小可用的 task_history（与 web_ui._init_db 的列集一致）。"""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE task_history(task_id TEXT PRIMARY KEY, goal TEXT, status TEXT,"
        " created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP,"
        " report TEXT, conversation_id TEXT DEFAULT '', parent_task_id TEXT DEFAULT '',"
        " context TEXT DEFAULT '', project TEXT DEFAULT 'default', user TEXT DEFAULT '',"
        " steps_json TEXT DEFAULT '', logs_json TEXT DEFAULT '')"
    )
    con.commit()
    con.close()
    task_state.ensure_schema(path)


class TestAcceptTaskRequest(unittest.TestCase):
    """编排器收到请求：登记 QUEUED（唯一写者）+ 写收执键。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_accept_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        _mk_db(self.db)
        self.redis = mock.MagicMock()
        self.redis.setex = mock.MagicMock()
        self.orch = type("O", (), {"_redis": self.redis})()

    def test_accepts_and_writes_ack(self):
        from orchestrator_v2 import accept_task_request
        with mock.patch.object(task_state, "DB_PATH", self.db):
            ok, reason = accept_task_request(self.orch, {
                "task_id": "ui-acc1", "goal": "目标", "project": "default",
                "conversation_id": "conv-1", "parent_task_id": "ui-prev",
                "context": "ctx", "user_id": "reviewer",
            })
        self.assertTrue(ok, reason)
        row = task_state.read_task("ui-acc1", self.db)
        self.assertEqual(row["status"], task_state.QUEUED)
        self.assertEqual(row["conversation_id"], "conv-1", "会话关系必须落库（此前负载不带）")
        self.assertEqual(row["parent_task_id"], "ui-prev")
        # 收执键：提交方据此判定"是否被接收"
        self.redis.setex.assert_called_once()
        args = self.redis.setex.call_args.args
        self.assertEqual(args[0], "task_ack:ui-acc1")
        self.assertEqual(args[2], "accepted")

    def test_rejects_without_task_id(self):
        from orchestrator_v2 import accept_task_request
        ok, reason = accept_task_request(self.orch, {"goal": "目标"})
        self.assertFalse(ok)
        self.assertIn("task_id", reason)

    def test_registration_failure_is_reported(self):
        from orchestrator_v2 import accept_task_request
        with mock.patch.object(task_state, "mark_queued",
                               side_effect=RuntimeError("db locked")):
            ok, reason = accept_task_request(self.orch, {"task_id": "ui-x", "goal": "g"})
        self.assertFalse(ok)
        self.assertIn("登记失败", reason)
        self.assertTrue(str(self.redis.setex.call_args.args[2]).startswith("rejected:"))


class TestSubmitHandshake(unittest.TestCase):
    """提交必须等收执：收到 accepted 才算成功，超时/被拒直接失败且不写库。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_submit_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        _mk_db(self.db)

    def _fake_redis(self, ack: str):
        fake = mock.MagicMock()
        fake.publish = mock.MagicMock()
        fake.get = mock.MagicMock(side_effect=lambda key: ack if key.startswith("task_ack:") else None)
        fake.delete = mock.MagicMock()
        return fake

    def _task_rows(self):
        con = sqlite3.connect(self.db)
        try:
            return con.execute("SELECT task_id, status FROM task_history").fetchall()
        finally:
            con.close()

    def test_accepted_returns_queued_and_writes_nothing(self):
        import web_ui
        fake = self._fake_redis("accepted")
        with mock.patch.object(web_ui, "_redis_ready", return_value=True), \
                mock.patch.object(web_ui, "_new_redis", return_value=fake), \
                mock.patch.object(web_ui, "DB_PATH", self.db), \
                mock.patch.dict("os.environ", {"WM_SUBMIT_ACK_TIMEOUT": "0.6"}):
            result = web_ui._publish_task("目标", conversation_id="conv-1")
        self.assertEqual(result["status"], "QUEUED")
        fake.publish.assert_called_once()
        payload = json.loads(fake.publish.call_args.args[1])
        self.assertEqual(payload["conversation_id"], "conv-1", "会话关系随请求下发")
        self.assertIn("parent_task_id", payload)
        self.assertEqual(self._task_rows(), [], "webui 不再登记 QUEUED（编排器是唯一写者）")

    def test_timeout_raises_and_writes_nothing(self):
        import web_ui
        fake = self._fake_redis("")
        with mock.patch.object(web_ui, "_redis_ready", return_value=True), \
                mock.patch.object(web_ui, "_new_redis", return_value=fake), \
                mock.patch.object(web_ui, "DB_PATH", self.db), \
                mock.patch.dict("os.environ", {"WM_SUBMIT_ACK_TIMEOUT": "0.3"}):
            with self.assertRaises(RuntimeError) as ctx:
                web_ui._publish_task("目标")
        self.assertIn("编排器未在", str(ctx.exception))
        self.assertEqual(self._task_rows(), [], "超时不得留下幽灵任务行")

    def test_rejected_raises_with_reason(self):
        import web_ui
        fake = self._fake_redis("rejected:DB 不可写")
        with mock.patch.object(web_ui, "_redis_ready", return_value=True), \
                mock.patch.object(web_ui, "_new_redis", return_value=fake), \
                mock.patch.object(web_ui, "DB_PATH", self.db), \
                mock.patch.dict("os.environ", {"WM_SUBMIT_ACK_TIMEOUT": "0.6"}):
            with self.assertRaises(RuntimeError) as ctx:
                web_ui._publish_task("目标")
        self.assertIn("拒绝", str(ctx.exception))
        self.assertIn("DB 不可写", str(ctx.exception))


class TestFinalizeAndCrashFallback(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_final_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        _mk_db(self.db)

    def test_finalize_is_idempotent_and_persists_acceptance(self):
        from orchestrator_v2 import OrchestratorV2
        orch = OrchestratorV2.__new__(OrchestratorV2)
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_queued("ui-fin", "目标", db_path=self.db)
            orch._finalize_task("ui-fin", "目标", "SUCCESS_WITH_ISSUES",
                                report="正文", steps=[{"step_id": "1"}],
                                logs=[{"message": "x"}],
                                acceptance={"overall": "fail", "gaps": ["g1"],
                                            "rules_fingerprint": "abc12345"})
            # 第二次调用（wrapper 兜底路径）不得覆盖已写终态
            orch._finalize_task("ui-fin", "目标", "FAILED", report="不该覆盖")
            row = task_state.read_task("ui-fin", self.db)
        self.assertEqual(row["status"], "SUCCESS_WITH_ISSUES", "重复调用必须幂等")
        self.assertEqual(row["report"], "正文")
        self.assertEqual(row["acceptance"]["overall"], "fail")
        self.assertEqual(row["rules_fingerprint"], "abc12345")

    def test_wrapper_covers_early_return_and_exception_paths(self):
        """提前 return 与异常都由 _run_task 兜底落库（单一收口点）。"""
        src = Path("orchestrator_v2.py").read_text(encoding="utf-8")
        i = src.index("def _run_task(tid, g, ctx, ar, tpl_steps, uid, proj, rc):")
        block = src[i:i + 1600]
        self.assertIn("orch._finalize_task(", block)
        self.assertGreaterEqual(block.count("orch._finalize_task("), 2,
                                "try 与 except 两条路径都要落库")

    def test_crash_fallback_respects_age_guard(self):
        task_state.mark_queued("ui-run", "目标", db_path=self.db)
        task_state.mark_running("ui-run", db_path=self.db)
        # 刚进入 RUNNING（updated_at 很新）→ 不得被误杀
        self.assertFalse(task_state.mark_dead_running_failed("ui-run", db_path=self.db))
        # 人为把更新时间推到久远 → 允许翻转
        con = sqlite3.connect(self.db)
        con.execute("UPDATE task_history SET updated_at=datetime('now','-2 hours')"
                    " WHERE task_id=?", ("ui-run",))
        con.commit()
        con.close()
        self.assertTrue(task_state.mark_dead_running_failed("ui-run", db_path=self.db))
        row = task_state.read_task("ui-run", self.db)
        self.assertEqual(row["status"], task_state.FAILED)
        self.assertEqual(row["phase"], "崩溃")

    def test_crash_fallback_ignores_non_running(self):
        task_state.mark_queued("ui-q", "目标", db_path=self.db)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE task_history SET updated_at=datetime('now','-5 hours')")
        con.commit()
        con.close()
        self.assertFalse(task_state.mark_dead_running_failed("ui-q", db_path=self.db),
                         "只翻 RUNNING，排队/终态不动")


class TestWebuiIsSubscriberOnly(unittest.TestCase):
    """webui 侧静态约束：不再写 task_history 状态列。"""

    def test_no_task_history_insert_in_webui(self):
        src = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertNotIn("INSERT INTO task_history", src,
                         "提交登记已归编排器：webui 不得再插入任务行")
        self.assertNotIn("record_completion(", src,
                         "终态落库已归编排器：webui 不得再写状态")
        self.assertNotIn("mark_queued(", src)

    def test_orchestrator_is_the_writer(self):
        src = Path("orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn("accept_task_request", src)
        self.assertIn("_finalize_task", src)
        self.assertIn("record_completion", src, "终态落库必须在编排器侧调用")
        self.assertIn("_ts.record_completion", src)


class TestCleanupAndDedupe(unittest.TestCase):
    """收尾项：webui 不再写状态、孤儿键清理、通知跨进程去重、报告页兜底。"""

    def test_webui_has_no_state_writes(self):
        """webui 不得再写 task_history 的状态列（只允许 DELETE 任务）。"""
        src = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertNotIn("INSERT INTO task_history", src)
        self.assertNotIn("record_completion(", src)
        self.assertNotIn("UPDATE task_history SET status", src,
                         "stale 翻转必须走 task_state（此前是 webui 内联裸 SQL）")
        self.assertIn("mark_stale_failed", src)

    def test_delete_task_clears_task_scoped_redis_keys(self):
        import web_ui
        fake = mock.MagicMock()
        with mock.patch.object(web_ui, "_revoke_share_token"), \
                mock.patch.object(web_ui, "_new_redis", return_value=fake), \
                mock.patch("task_state.drop_snapshot") as drop, \
                mock.patch.object(web_ui, "_task_results", {}):
            web_ui._delete_task("ui-del1")
        drop.assert_called_once_with("ui-del1")
        deleted = fake.delete.call_args.args
        self.assertIn("task_running:ui-del1", deleted)
        self.assertIn("task_ack:ui-del1", deleted)

    def test_notify_claim_is_cross_process(self):
        """抢占走 Redis SET NX EX：第二个进程/第二次调用拿不到。"""
        from notifications import claim_notify
        fake = mock.MagicMock()
        fake.set = mock.MagicMock(side_effect=[True, False])
        with mock.patch("redis.Redis", return_value=fake):
            self.assertTrue(claim_notify("failed", "ui-n1"))
            self.assertFalse(claim_notify("failed", "ui-n1"), "同一 kind+task 只能抢到一次")
        key = fake.set.call_args.args[0]
        self.assertEqual(key, "notify_claim:failed:ui-n1")

    def test_notify_claim_falls_back_without_redis(self):
        from notifications import claim_notify, _CLAIMED
        _CLAIMED.clear()
        with mock.patch("redis.Redis", side_effect=RuntimeError("no redis")):
            self.assertTrue(claim_notify("done", "ui-n2"))
            self.assertFalse(claim_notify("done", "ui-n2"))
        _CLAIMED.clear()

    def test_orchestrator_skips_daily_report_and_claims_others(self):
        """日报交给 webui（带分享链接）；其它任务由编排器按状态抢占。"""
        src = Path("orchestrator_v2.py").read_text(encoding="utf-8")
        block = src[src.index("def _notify_done_async"):]
        block = block[:block.index("def _read_acceptance_summary")]
        self.assertIn('== "daily-report"', block)
        self.assertIn("claim_notify", block)

    def test_progress_channel_gated_off_by_default(self):
        from ws_helpers import push_progress
        events = []
        messaging = mock.MagicMock()
        messaging.publish = lambda ch, msg: events.append(ch)
        with mock.patch.dict("os.environ", {"WM_PUBLISH_PROGRESS_CHANNEL": "0"}):
            push_progress(messaging, "t-1", "log", {"message": "x"})
        self.assertEqual(events, ["orchestrator:response"],
                         "默认不再发无人订阅的 orchestrator:progress")

    def test_task_report_route_uses_projection(self):
        """报告页在内存为空时也要能读（此前靠最近 100 条裸行兜底会 404）。"""
        import web_ui
        captured = {}
        handler = type("H", (), {
            "_html": lambda self, html, code=200: captured.update(html=html),
            "_json": lambda self, data, code=200: captured.update(json=data, code=code),
        })()
        task = {"task_id": "ui-rep1", "goal": "目标", "status": "SUCCESS",
                "report": "# 报告正文\n\n结论。\n", "steps": [], "logs": [],
                "acceptance": {"overall": "pass"}, "project": "default"}
        with mock.patch.object(web_ui, "_task_results", {}), \
                mock.patch("task_state.merge_projection", return_value=task), \
                mock.patch.object(web_ui, "_list_tasks",
                                  side_effect=AssertionError("不应退到裸行兜底")):
            web_ui._get_task_report(handler, "/task/ui-rep1/report")
        self.assertIn("报告正文", captured.get("html", ""))


if __name__ == "__main__":
    unittest.main()
