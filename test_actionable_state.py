# -*- coding: utf-8 -*-
"""C3：新人可行动状态的行为测试（`actionable_state`）。

反例（原指令 §4-C3）：任务状态、收执、依赖健康、待材料各存一处，页面各显示一半——
新人看得出"没成功"，看不出"该做什么"；而"未知/过期/别的实例"还常被显示成绿色。
这里把六态映射、行动入口与"未知不算绿"钉死。
"""

from __future__ import annotations

import unittest

import actionable_state as A


def _dep(name, state, reason="", detail="", instance="", checked=0.0):
    return {"name": name, "state": state, "reason": reason, "detail": detail,
            "_instance": instance, "_checked_at": checked}


class TestTaskClassification(unittest.TestCase):
    def test_no_row_is_not_received_and_tells_you_to_retry(self):
        out = A.classify_task(row=None)
        self.assertEqual(out["state"], A.STATE_NOT_RECEIVED)
        self.assertEqual(out["action"], A.ACTION_RETRY)
        self.assertIn("重新提交", out["message"])

    def test_unknown_status_is_not_treated_as_running(self):
        """认不出的状态不许猜成"在跑"（那会让用户一直等一个不存在的任务）。"""
        out = A.classify_task(row={"task_id": "t1", "status": "WEIRD"})
        self.assertEqual(out["state"], A.STATE_NOT_RECEIVED)
        self.assertIn("无法识别", out["message"])

    def test_received_receipt_is_pending_consume(self):
        out = A.classify_task(row={"task_id": "t1", "status": "RECEIVED"},
                              timeline=[{"event": "received"}, {"event": "published"}])
        self.assertEqual(out["state"], A.STATE_PENDING_CONSUME)
        self.assertEqual(out["action"], A.ACTION_WAIT)
        self.assertIn("published", out["events"])

    def test_running_queued_and_pending_all_map_to_running(self):
        for status in ("QUEUED", "RUNNING", "PENDING"):
            with self.subTest(status=status):
                out = A.classify_task(row={"task_id": "t1", "status": status})
                self.assertEqual(out["state"], A.STATE_RUNNING)
                self.assertEqual(out["action"], A.ACTION_WAIT)

    def test_material_pending_wins_over_running(self):
        """等材料的任务必须给"去补材料"，而不是让用户干等。"""
        out = A.classify_task(row={"task_id": "t1", "status": "RUNNING"},
                              material_pending=True)
        self.assertEqual(out["state"], A.STATE_WAITING_MATERIAL)
        self.assertEqual(out["action"], A.ACTION_ADD_MATERIAL)
        self.assertEqual(out["target"], "materials")

    def test_failed_points_at_the_fixable_cause(self):
        """失败时优先指向**能修的那个原因**（模型未配置 → 去设置）。"""
        causes = A.health_causes([_dep("llm", "unavailable", reason="API Key 未配置")])
        out = A.classify_task(row={"task_id": "t1", "status": "FAILED"}, causes=causes)
        self.assertEqual(out["state"], A.STATE_FAILED)
        self.assertEqual(out["action"], A.ACTION_OPEN_SETTINGS)
        self.assertIn("模型未配置", out["message"])

    def test_failed_without_cause_points_at_details(self):
        out = A.classify_task(row={"task_id": "t1", "status": "FAILED"})
        self.assertEqual(out["action"], A.ACTION_VIEW_DETAILS)

    def test_cancelled_is_a_clear_failure_not_success(self):
        out = A.classify_task(row={"task_id": "t1", "status": "CANCELLED"})
        self.assertEqual(out["state"], A.STATE_FAILED)

    def test_success_still_says_human_review_is_separate(self):
        """已完成也要提醒"机器验收 ≠ 研究通过"（三态分离不得被这一步合并）。"""
        out = A.classify_task(row={"task_id": "t1", "status": "SUCCESS"})
        self.assertEqual(out["state"], A.STATE_DONE)
        self.assertIn("人工复核", out["message"])
        out2 = A.classify_task(row={"task_id": "t1", "status": "SUCCESS_WITH_ISSUES"})
        self.assertEqual(out2["state"], A.STATE_DONE)

    def test_failure_action_comes_from_the_task_own_facts_not_the_first_health_item(self):
        """C 批反例：失败在"缺原始披露"，提示却指向**无关的 Docker**。

        实测任务 `ui-29e8ca73b5`：终态 report 写着"研究草稿／待补原始披露（located=0）"，
        而全局健康列表第一条是 `code_sandbox`（容器隔离不可用，unavailable 排最前）——
        旧实现取 `causes[0]` 就把用户引去装 Docker。
        """
        row = {
            "task_id": "ui-29e8ca73b5", "status": "FAILED", "phase": "完成",
            "report": ("# 交付结果\n> **未验收草稿：分析未完成**\n"
                       "> **研究状态：研究草稿／待补原始披露**——没有一条带正文定位的"
                       "原始披露（located=0）"),
            "steps": [{"step_id": "1", "capability": "web_search", "status": "FAILED",
                       "error": "检索未产出可用来源；需补资料后重试"},
                      {"step_id": "4", "capability": "report_generator",
                       "status": "SUCCESS"}],
        }
        causes = A.health_causes([_dep("code_sandbox", "unavailable",
                                       reason="容器隔离不可用")])
        out = A.classify_task(row=row, causes=causes)
        self.assertEqual(out["action"], A.ACTION_ADD_MATERIAL, out)
        self.assertEqual(out["target"], "materials")
        self.assertIn("缺原始披露", out["message"])
        self.assertNotIn("Docker", out["message"])
        # 健康原因仍然如实列出（只是**不再拿它当这次失败的原因**）
        self.assertTrue(out["causes"])

    def test_sandbox_cause_only_when_the_task_really_has_code_steps(self):
        """沙箱/Docker 只有在任务**真的含** `code_execution` 步骤时才算它的原因。"""
        causes = A.health_causes([_dep("code_sandbox", "unavailable",
                                       reason="容器隔离不可用")])
        no_code = {"task_id": "t1", "status": "FAILED", "phase": "完成",
                   "report": "Task failed: 未知",
                   "steps": [{"step_id": "1", "capability": "web_search",
                              "status": "FAILED", "error": ""}]}
        out = A.classify_task(row=no_code, causes=causes)
        self.assertNotIn("code_sandbox", out["message"])
        with_code = dict(no_code, steps=[{"step_id": "3", "capability": "code_execution",
                                          "status": "FAILED", "error": ""}])
        out2 = A.classify_task(row=with_code, causes=causes)
        self.assertEqual(out2["action"], A.ACTION_OPEN_HEALTH)
        self.assertIn("code_sandbox", out2["message"])

    def test_task_own_facts_win_over_health_list(self):
        """模型未配置写在任务自己的失败事实里时，去设置——即使健康首条是 Docker。"""
        causes = A.health_causes([_dep("code_sandbox", "unavailable", reason="隔离不可用")])
        row = {"task_id": "t1", "status": "FAILED", "phase": "完成",
               "report": "> **未验收草稿**\n错误：API Key 未配置（占位符）",
               "steps": [{"step_id": "1", "capability": "web_search",
                          "status": "FAILED", "error": "API Key 未配置"}]}
        out = A.classify_task(row=row, causes=causes)
        self.assertEqual(out["action"], A.ACTION_OPEN_SETTINGS)
        self.assertEqual(out["target"], "/settings")

    def test_cancelled_is_not_reported_as_a_failure_with_steps(self):
        """取消与失败分开说：取消没有"失败步骤"可看，出口是重新提交。"""
        out = A.classify_task(row={"task_id": "t1", "status": "CANCELLED"})
        self.assertEqual(out["state"], A.STATE_FAILED)      # 不是"已完成"
        self.assertTrue(out.get("cancelled"))
        self.assertEqual(out["action"], A.ACTION_RETRY)
        self.assertIn("取消", out["message"])
        self.assertNotIn("失败步骤", out["message"])

    def test_every_state_has_label_and_action_label(self):
        rows = [None, {"task_id": "t", "status": "RECEIVED"},
                {"task_id": "t", "status": "RUNNING"}, {"task_id": "t", "status": "FAILED"},
                {"task_id": "t", "status": "SUCCESS"}]
        seen = set()
        for row in rows:
            out = A.classify_task(row=row)
            self.assertIn(out["state"], A.TASK_STATES)
            self.assertTrue(out["label"])
            self.assertTrue(out["action_label"], out)
            seen.add(out["state"])
        self.assertEqual(seen, {A.STATE_NOT_RECEIVED, A.STATE_PENDING_CONSUME,
                                A.STATE_RUNNING, A.STATE_FAILED, A.STATE_DONE})


class TestHealthCauses(unittest.TestCase):
    def test_available_items_produce_no_cause(self):
        self.assertEqual(A.health_causes([_dep("llm", "available")]), [])

    def test_unconfigured_model_points_at_settings(self):
        causes = A.health_causes([_dep("llm", "unavailable", reason="未配置 api_key")])
        self.assertEqual(len(causes), 1)
        self.assertEqual(causes[0]["action"], A.ACTION_OPEN_SETTINGS)
        self.assertIn("模型未配置", causes[0]["message"])

    def test_model_error_also_points_at_settings(self):
        causes = A.health_causes([_dep("llm", "unavailable", reason="401 unauthorized")])
        self.assertEqual(causes[0]["action"], A.ACTION_OPEN_SETTINGS)

    def test_source_unknown_says_unknown_is_not_available(self):
        """未知必须显式说"不等于可用"，不能显示成绿。"""
        causes = A.health_causes([_dep("search", "unknown", reason="无快照")])
        self.assertIn("未知不等于可用", causes[0]["message"])

    def test_degraded_source_says_still_usable(self):
        causes = A.health_causes([_dep("market_source", "degraded", reason="部分熔断")])
        self.assertIn("仍可做", causes[0]["message"])

    def test_model_causes_come_first(self):
        causes = A.health_causes([
            _dep("search", "unavailable"), _dep("llm", "unavailable"),
            _dep("sandbox", "degraded")])
        self.assertEqual(causes[0]["name"], "llm", "没有模型什么都做不了，必须排第一")

    def test_causes_capped_at_three(self):
        items = [_dep(f"x{i}", "unavailable") for i in range(6)]
        self.assertEqual(len(A.health_causes(items)), 3)


class TestUnifiedHealth(unittest.TestCase):
    def test_recognised_states_pass_through_with_labels(self):
        h = A.unified_health([_dep("llm", "available"), _dep("search", "degraded")])
        self.assertEqual(h["summary"]["available"], 1)
        self.assertEqual(h["summary"]["degraded"], 1)
        self.assertFalse(h["ok"], "有降级项就不算整体可用")
        by = {i["name"]: i for i in h["items"]}
        self.assertEqual(by["search"]["label"], "降级（仍可用）")

    def test_unknown_state_is_not_green(self):
        """核心反例：三项全 unknown 时 `ok` 必须是 False（未知 ≠ 可用）。"""
        h = A.unified_health([_dep("llm", "unknown"), _dep("search", "unknown")])
        self.assertFalse(h["ok"])
        self.assertEqual(h["summary"]["unknown"], 2)
        self.assertIn("未知", h["items"][0]["label"])

    def test_unrecognised_state_downgrades_to_unknown(self):
        """上游给个没见过的状态（或空）时按未知处理，不许当绿。"""
        h = A.unified_health([{"name": "llm", "state": "totally-fine"},
                              {"name": "search"}])
        self.assertEqual([i["state"] for i in h["items"]], ["unknown", "unknown"])
        self.assertFalse(h["ok"])

    def test_empty_health_is_not_ok(self):
        """一条都没有时不能算"全部可用"（空 ≠ 绿）。"""
        h = A.unified_health([])
        self.assertFalse(h["ok"])
        self.assertEqual(h["items"], [])

    def test_all_available_is_ok(self):
        h = A.unified_health([_dep("llm", "available"), _dep("search", "available")])
        self.assertTrue(h["ok"])

    def test_legacy_source_health_is_folded_in_once(self):
        """旧 source_health 只在依赖快照没覆盖该源时补条目（同一件事不显示两遍）。"""
        deps = [_dep("llm", "available")]
        h = A.unified_health(deps, market_sources={
            "eastmoney": {"healthy": True},
            "sina": {"healthy": False, "remaining": 120.0, "fails": 3, "last_error": "403"},
        })
        names = [i["name"] for i in h["items"]]
        self.assertIn("source:eastmoney", names)
        self.assertIn("source:sina", names)
        by = {i["name"]: i for i in h["items"]}
        self.assertEqual(by["source:eastmoney"]["state"], "available")
        self.assertEqual(by["source:sina"]["state"], "unavailable")
        self.assertTrue(by["source:sina"]["legacy"], "标注来源便于迁移后删除")
        self.assertFalse(h["ok"], "有源熔断就不算整体可用")

    def test_legacy_source_not_duplicated_when_snapshot_covers_it(self):
        deps = [_dep("source:eastmoney", "available")]
        h = A.unified_health(deps, market_sources={"eastmoney": {"healthy": True}})
        self.assertEqual([i["name"] for i in h["items"]], ["source:eastmoney"])

    def test_summary_counts_match_items(self):
        h = A.unified_health([_dep("a", "available"), _dep("b", "degraded"),
                              _dep("c", "unavailable"), _dep("d", "unknown")])
        self.assertEqual(h["summary"], {"available": 1, "degraded": 1,
                                        "unavailable": 1, "unknown": 1})
        self.assertEqual(sum(h["summary"].values()), len(h["items"]))

    def test_health_view_carries_causes_for_newcomers(self):
        h = A.unified_health([_dep("llm", "unavailable", reason="未配置")])
        self.assertTrue(h["causes"])
        self.assertEqual(h["causes"][0]["action"], A.ACTION_OPEN_SETTINGS)


class _StubHandler:
    """最小请求处理器替身：只需要 `_json`。"""

    def _json(self, payload, code=200):
        return payload, code


class TestActionableEndpoint(unittest.TestCase):
    """`GET /api/task/<id>/actionable`：轻量端点，口径与任务页同一份实现。"""

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="wm_act_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        # 用 task_state 自己的建表（列集与运行期一致）；**不要**先手搓一张简化表——
        # `CREATE TABLE IF NOT EXISTS` 会因此跳过，缺列会让写入静默失败。
        import task_state
        task_state.ensure_schema(self.db)

    def _call(self, path):
        import web_ui
        from unittest import mock
        with mock.patch.object(web_ui, "_redis_ready", return_value=False), \
                mock.patch("health_registry.snapshot", return_value=[]):
            return web_ui._get_task_actionable(_StubHandler(), path)

    def test_wrong_path_returns_none(self):
        import web_ui
        self.assertIsNone(web_ui._get_task_actionable(_StubHandler(),
                                                     "/api/task/x/materials"))

    def test_missing_task_id_is_400(self):
        out = self._call("/api/task//actionable")
        self.assertEqual(out[1], 400)

    def test_unknown_task_says_not_received_with_retry(self):
        payload, code = self._call("/api/task/ui-none/actionable")
        self.assertEqual(code, 200)
        self.assertEqual(payload["state"], A.STATE_NOT_RECEIVED)
        self.assertEqual(payload["action"], A.ACTION_RETRY)
        self.assertEqual(payload["task_id"], "ui-none")

    def test_received_receipt_is_pending_consume(self):
        import task_state
        from unittest import mock
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_received("ui-recv", "目标")
            payload, code = self._call("/api/task/ui-recv/actionable")
        self.assertEqual(code, 200)
        self.assertEqual(payload["state"], A.STATE_PENDING_CONSUME)
        self.assertEqual(payload["action"], A.ACTION_WAIT)

    def test_failed_task_points_at_settings_when_model_unconfigured(self):
        import task_state
        from unittest import mock
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_queued("ui-fail", "目标", db_path=self.db)
            con = __import__("sqlite3").connect(self.db)
            con.execute("UPDATE task_history SET status='FAILED' WHERE task_id='ui-fail'")
            con.commit()
            con.close()
            from unittest import mock as _m
            with _m.patch("health_registry.snapshot",
                          return_value=[{"name": "llm", "state": "unavailable",
                                         "reason": "未配置 api_key"}]):
                import web_ui
                with _m.patch.object(web_ui, "_redis_ready", return_value=False):
                    payload, code = web_ui._get_task_actionable(
                        _StubHandler(), "/api/task/ui-fail/actionable")
        self.assertEqual(code, 200)
        self.assertEqual(payload["state"], A.STATE_FAILED)
        self.assertEqual(payload["action"], A.ACTION_OPEN_SETTINGS)
        self.assertIn("模型未配置", payload["message"])


if __name__ == "__main__":
    unittest.main()
