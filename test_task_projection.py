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
from datetime import datetime, timezone
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

    def test_actionable_state_is_not_silently_none(self):
        """P1-d：`actionable` 不得因未定义变量被吞成 None（"该做什么"永远不显示）。

        实测缺口：`_get_task_page` 里读 `health.get("causes")`，而 `health` **从未在
        该函数里定义** → 每次调用 NameError → 被 except 吞成一条 warning →
        `actionable` 恒为 `None`。这里走**真实** `_get_task_page`，并且断言健康视图
        给出的原因（`causes`）确实进了分类输入——否则"任务原因驱动"只是名义上的。
        """
        import actionable_state
        import web_ui
        captured = {}
        task = {
            "task_id": "t-act", "status": "SUCCESS_WITH_ISSUES", "goal": "目标",
            "steps": [], "report": "正文", "logs": [], "project": "default",
            "revision": False, "acceptance": {"overall": "pass", "gaps": []},
            "llm_degraded": None,
        }
        seen = {}

        def _classify(**kw):
            seen.update(kw)
            return {"state": "待材料", "reason": "缺关键事实"}

        causes = [{"name": "redis", "state": "degraded", "detail": "连不上"}]
        with mock.patch.object(web_ui, "_task_results", {}), \
                mock.patch("task_state.merge_projection", return_value=task), \
                mock.patch("actionable_state.unified_health",
                           return_value={"items": [], "summary": {}, "ok": False,
                                         "causes": causes}), \
                mock.patch("actionable_state.classify_task", side_effect=_classify):
            web_ui._get_task_page(self._fake_handler(captured), "/task/t-act")
        self.assertIsNotNone(captured["data"].get("actionable"),
                             "actionable 不得恒为 None")
        self.assertEqual(captured["data"]["actionable"]["state"], "待材料")
        self.assertEqual(seen.get("causes"), causes,
                         "健康视图的原因必须进分类输入（任务原因驱动）")
        self.assertIs(actionable_state.classify_task, actionable_state.classify_task)

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

    def test_log_timestamp_fallback_is_iso_not_bare_clock(self):
        """没带 timestamp 的推送要落成 UTC ISO，而不是裸 `HH:MM:SS`。

        裸时钟取的是**服务器**本地时间；实测服务器时区为 UTC 时，同一列日志一半
        渲染成本地时间、一半渲染成 UTC（真实页面：21:56 与 13:56 交替出现）。
        """
        import web_ui
        for ptype in ("log", "warning"):
            existing = {}
            web_ui._merge_progress_message(existing, {
                "type": ptype, "payload": {"message": "阶段停滞", "agent": "orchestrator"}})
            ts = str(existing["logs"][-1]["timestamp"])
            self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T", f"{ptype} 的时间戳应为 ISO：{ts}")
            self.assertIsNotNone(web_ui._parse_utc_ts(ts), ts)


class TestElapsedTimezone(unittest.TestCase):
    """elapsed_sec 的口径：任务时间戳是 UTC，显示口径不能擅自加本地偏移。

    实测反例（真实运行中任务）：创建于 13:29:14Z、已运行 2 分钟，接口返回
    elapsed_sec=28854（≈8 小时）——因为 SQLite 的朴素 UTC 串被 `.timestamp()`
    按本地时区解释，UTC+8 上凭空多出一天里的 8 小时。
    """

    def setUp(self):
        import web_ui
        self.web_ui = web_ui
        # 固定"现在"，避免依赖真实时钟
        self.now = datetime(2026, 9, 17, 13, 31, 14, tzinfo=timezone.utc)

    def _elapsed(self, task):
        import web_ui
        captured = {}

        class _H:
            def _json(self, data, code=200, extra_headers=None):
                captured.update(data=data, code=code)

        with mock.patch.object(web_ui, "_task_results", {}), \
                mock.patch("task_state.merge_projection", return_value=task), \
                mock.patch.object(web_ui.time, "time", return_value=self.now.timestamp()):
            web_ui._get_task_page(_H(), "/task/t-tz")
        return captured["data"].get("elapsed_sec")

    def test_sqlite_naive_utc_not_offset(self):
        """SQLite `CURRENT_TIMESTAMP` 写法（朴素 UTC）：2 分钟就是 2 分钟。"""
        got = self._elapsed({"task_id": "t-tz", "status": "RUNNING",
                             "created_at": "2026-09-17 13:29:14"})
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, 120.0, delta=1.0)

    def test_iso_with_offset_same_result(self):
        """内存快照写法（带 +00:00）：与朴素 UTC 同解。"""
        got = self._elapsed({"task_id": "t-tz", "status": "RUNNING",
                             "created_at": "2026-09-17T13:29:14.000000+00:00"})
        self.assertAlmostEqual(got, 120.0, delta=1.0)

    def test_missing_or_broken_created_at_is_unknown_not_zero(self):
        """拿不到 created_at 时是 None（前端显示未知），不是 0。"""
        self.assertIsNone(self._elapsed({"task_id": "t-tz", "status": "RUNNING"}))
        self.assertIsNone(self._elapsed({"task_id": "t-tz", "status": "RUNNING",
                                         "created_at": "昨天"}))

    def test_finished_task_uses_completed_at(self):
        got = self._elapsed({"task_id": "t-tz", "status": "SUCCESS",
                             "created_at": "2026-09-17 13:20:00",
                             "completed_at": "2026-09-17 13:25:00"})
        self.assertAlmostEqual(got, 300.0, delta=1.0)

    def test_helper_parses_both_shapes_and_refuses_garbage(self):
        h = self.web_ui._parse_utc_ts
        self.assertAlmostEqual(h("2026-09-17 13:29:14"),
                               datetime(2026, 9, 17, 13, 29, 14,
                                        tzinfo=timezone.utc).timestamp(), places=6)
        self.assertAlmostEqual(h("2026-09-17T13:29:14Z"),
                               datetime(2026, 9, 17, 13, 29, 14,
                                        tzinfo=timezone.utc).timestamp(), places=6)
        self.assertIsNone(h(None))
        self.assertIsNone(h(""))
        self.assertIsNone(h("not-a-time"))


class TestMemoryReportSyncAfterRewrite(unittest.TestCase):
    """就地改写交付正文的入口必须同步**内存投影**。

    实机反例 `ui-433f4cf9e2`：分析采纳 A 之后连续三次 `POST /package` 都 409
    「导出期间发生修订（交付正文与采纳版本不一致）」——采纳只写了 DB/Redis 投影，
    而 `_get_task_report_data` 优先读内存里的旧正文；同时 MD/PDF 下载出现
    「身份头=采纳版 / 字节=采纳前」(`X-Report-Draft: 1`)。重启 webui（清空内存）后
    同一请求 200、draft=0——守卫判据没错，错在缓存。
    """

    def setUp(self):
        import web_ui
        self.addCleanup(mock.patch.object(web_ui, "_task_results", {}).stop)
        self._patcher = mock.patch.object(web_ui, "_task_results", {})
        self.mem = self._patcher.start()
        self.web_ui = web_ui
        self.addCleanup(self._patcher.stop)

    def test_cached_body_wins_over_projection(self):
        """先把"缓存优先"这件事本身钉住（缺陷正来自这条优先级）。"""
        self.mem["t-mem"] = {"report": "采纳前的正文"}
        got = self.web_ui._get_task_report_data("t-mem")
        self.assertEqual(got["report"], "采纳前的正文")

    def test_sync_replaces_cached_report_body(self):
        self.mem["t-mem2"] = {"report": "采纳前的正文", "final_report": "采纳前的正文"}
        self.assertTrue(self.web_ui._sync_memory_report("t-mem2", "采纳后的正文"))
        self.assertEqual(self.mem["t-mem2"]["report"], "采纳后的正文")
        self.assertEqual(self.mem["t-mem2"]["final_report"], "采纳后的正文")
        self.assertEqual(self.web_ui._get_task_report_data("t-mem2")["report"],
                         "采纳后的正文")

    def test_sync_without_cache_entry_is_noop(self):
        """没有内存条目时不动手：下一次读取本来就会从 DB/Redis 投影重建。"""
        self.assertFalse(self.web_ui._sync_memory_report("t-absent", "X"))
        self.assertNotIn("t-absent", self.mem)

    def test_sync_failure_does_not_raise(self):
        class _Boom(dict):
            def get(self, *_a, **_k):
                raise RuntimeError("boom")

        self.mem["t-boom"] = _Boom()
        self.assertFalse(self.web_ui._sync_memory_report("t-boom", "X"))

    def test_all_rewrite_entry_points_sync_memory(self):
        """源码级守卫：三个改写交付正文的入口都要同步内存投影。"""
        src = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count("_sync_memory_report("), 4,
                                "分析采纳/候选采纳/人工修订应共用同一处同步实现")
        for fn in ("_post_task_analysis_adopt", "_post_task_candidate_adopt",
                   "_post_task_review_edit"):
            idx = src.find("def %s(" % fn)
            self.assertGreater(idx, 0, f"找不到 {fn}")
            nxt = src.find("\ndef ", idx + 1)
            body = src[idx:nxt if nxt > 0 else len(src)]
            self.assertIn("_sync_memory_report(", body,
                          f"{fn} 改写了交付正文却没同步内存投影（导出会恒 409）")


if __name__ == "__main__":
    unittest.main()
