# -*- coding: utf-8 -*-
"""任务13（P0 by_capability 口径分层）测试。

设计：docs/arch_phase2_p0_metrics_split.md
- `_step_bucket`：单步分类纯函数（unexecuted/success/cascade/gate/genuine/other）；
- `_is_search_gap_skip`：「无候选 URL」谓词式可选跳过；
- `_normalize_unexecuted_steps`：收尾补写 CANCELLED/unexecuted；
- `_write_summary`：旧键名兼容（前端 MetricsPage 读 success_rate）+ 新分层键。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import metrics_collector as mc
import orchestrator_v2


class TestStepBucket(unittest.TestCase):
    """步级分类矩阵（判定顺序是验收口径的一部分）。"""

    def test_missing_status_or_cancelled_is_unexecuted(self):
        # 旧数据：result 键缺失/为空 dict；新数据：编排器补写 CANCELLED
        self.assertEqual(mc._step_bucket(None), "unexecuted")
        self.assertEqual(mc._step_bucket({}), "unexecuted")
        self.assertEqual(mc._step_bucket({"status": ""}), "unexecuted")
        self.assertEqual(mc._step_bucket(
            {"status": "CANCELLED", "reason": "unexecuted"}), "unexecuted")
        self.assertEqual(mc._step_bucket({"status": "canceled"}), "unexecuted")

    def test_success_wins_before_text_marks(self):
        """SUCCESS 最先判定：正文恰好含级联/门禁短语的成功步不是失败。"""
        self.assertEqual(mc._step_bucket(
            {"status": "SUCCESS", "agent_id": "search_agent",
             "result": "正文提到 Blocked by failed 的案例"}), "success")
        self.assertEqual(mc._step_bucket(
            {"status": "SUCCESS", "agent_id": "webfetchworker",
             "result": "页面含 无候选 URL 字样"}), "success")

    def test_cascade_blocked_before_gate(self):
        """级联在门禁之前：级联步同样 elapsed==0 且无 agent_id（任务12 核实）。"""
        self.assertEqual(mc._step_bucket(
            {"status": "FAILED", "agent_id": None, "elapsed_sec": 0,
             "result": "Blocked by failed dependency: 2"}), "cascade_blocked")

    def test_gate_blocked_by_text_or_zero_elapsed_no_agent(self):
        self.assertEqual(mc._step_bucket(
            {"status": "FAILED", "agent_id": None, "elapsed_sec": 0.1,
             "result": "无候选 URL（检索未产出可用来源），已跳过抓取"}),
            "gate_blocked")
        self.assertEqual(mc._step_bucket(
            {"status": "FAILED", "agent_id": None, "elapsed_sec": 0,
             "result": "根任务预算不足，未派发"}), "gate_blocked")

    def test_genuine_failed_requires_dispatched_agent(self):
        self.assertEqual(mc._step_bucket(
            {"status": "FAILED", "agent_id": "webfetchworker", "elapsed_sec": 3.2,
             "result": "URL 无法解析"}), "genuine_failed")
        # 无执行者但耗时 >0、无已知短语：落 other（全表 6 步的残渣）
        self.assertEqual(mc._step_bucket(
            {"status": "FAILED", "agent_id": None, "elapsed_sec": 300,
             "result": "Timeout after 300s"}), "other")

    def test_dispatched_non_success_non_failed_falls_to_other(self):
        self.assertEqual(mc._step_bucket(
            {"status": "PARTIAL", "agent_id": "search_agent", "elapsed_sec": 2}),
            "other")


class TestSearchGapPredicate(unittest.TestCase):
    def test_marker_and_text_agree(self):
        pred = orchestrator_v2.OrchestratorV2._is_search_gap_skip
        self.assertTrue(pred({"gate_skipped": True}))
        self.assertTrue(pred(
            {"result": "无候选 URL（检索未产出可用来源），已跳过抓取"}))
        self.assertFalse(pred({"result": "正常抓取完成"}))
        self.assertFalse(pred(None))
        self.assertFalse(pred("无候选 URL"))

    def test_downstream_step_quoting_the_phrase_is_not_a_gap(self):
        """P1-5（20261021 复验）：写作步正文引用「无候选 URL」不是检索缺口步。

        真机反例（`ui-8b28c95ab1`）：`step_id=5`（content_summary）的成稿里引用了
        检索缺口叙述，裸子串匹配把它标成缺口步 → 交付与日志写
        「资料缺口：步骤 5 检索未获取到有效来源」，用户按提示去找步骤 5，
        找到的是写作步（真正的检索步是步骤 1）。
        """
        pred = orchestrator_v2.OrchestratorV2._is_search_gap_skip
        downstream = {"status": "SUCCESS",
                      "result": "检索步无候选 URL，本次按现有资料写作……"}
        self.assertFalse(pred(downstream, cap="content_summary"))
        self.assertFalse(pred(downstream, cap="report_generator"))
        # 显式标记仍然成立（与能力无关：门禁自己打的标记是权威）
        self.assertTrue(pred({"gate_skipped": True,
                              "result": "无候选 URL"},
                             cap="content_summary"))
        # 检索类步按文本判定（历史数据没有 gate_skipped 标记）
        self.assertTrue(pred({"result": "无候选 URL（检索未产出可用来源），已跳过抓取"},
                             cap="web_fetch"))
        self.assertTrue(pred({"result": "无候选 URL"}, cap="web_search"))


class TestNormalizeUnexecutedSteps(unittest.TestCase):
    def test_missing_status_becomes_cancelled_unexecuted(self):
        steps = [
            {"step_id": "1", "capability": "web_search",
             "result": {"status": "SUCCESS", "result": "ok"}},
            {"step_id": "2", "capability": "web_fetch"},  # result 键缺失
            {"step_id": "2b", "capability": "web_fetch", "result": {}},  # 空字典
        ]
        out = orchestrator_v2._normalize_unexecuted_steps(steps)
        self.assertIs(out, steps)
        self.assertEqual(steps[0]["result"]["status"], "SUCCESS")  # 不覆盖真值
        self.assertEqual(steps[1]["result"]["status"], "CANCELLED")
        self.assertEqual(steps[1]["result"]["reason"], "unexecuted")
        self.assertEqual(steps[2]["result"]["status"], "CANCELLED")
        self.assertEqual(steps[2]["result"]["reason"], "unexecuted")

    def test_existing_terminal_states_not_overwritten(self):
        steps = [
            {"step_id": "1", "capability": "web_fetch",
             "result": {"status": "FAILED", "result": "URL 无法解析"}},
            {"step_id": "2", "capability": "web_search",
             "result": {"status": "CANCELLED", "result": "任务已取消"}},
        ]
        orchestrator_v2._normalize_unexecuted_steps(steps)
        self.assertEqual(steps[0]["result"]["status"], "FAILED")
        self.assertNotIn("reason", steps[0]["result"])
        self.assertEqual(steps[1]["result"]["status"], "CANCELLED")
        self.assertNotIn("reason", steps[1]["result"])

    def test_none_and_non_dict_steps_tolerated(self):
        self.assertEqual(orchestrator_v2._normalize_unexecuted_steps(None), [])
        orchestrator_v2._normalize_unexecuted_steps(["junk", 42])


class TestCapabilityRow(unittest.TestCase):
    def test_finalize_row_rates_from_dispatched_success(self):
        """派发口径的分子是**派发成功步**：package 127/121=105% 的荒谬值不得复现。"""
        row = mc._new_cap_row()
        row["total_planned"] = 101
        row["total_dispatched"] = 74
        row["success"] = 69
        row["success_dispatched"] = 69
        row["unexecuted"] = 12
        row["gate_blocked"] = 10
        row["cascade_blocked"] = 5
        row["genuine_failed"] = 5
        out = mc._finalize_cap_row(row)
        self.assertEqual(out["success_rate_dispatched"], 93.2)
        self.assertEqual(out["success_rate_planned"], 68.3)
        # 分层桶之和 == 计划步数（other 兜底防漏计）
        self.assertEqual(sum(out[b] for b in mc._STEP_BUCKETS),
                         out["total_planned"])

    def test_no_dispatched_steps_means_unknown_rate(self):
        row = mc._new_cap_row()
        row["total_planned"] = 3
        out = mc._finalize_cap_row(row)
        self.assertIsNone(out["success_rate_dispatched"])
        self.assertEqual(out["success_rate_planned"], 0.0)


class TestDbCapabilityStatsClassification(unittest.TestCase):
    def test_classification_over_synthetic_db(self):
        """端到端：临时 task_history 表 → 分层输出与设计口径一致。"""
        steps = [
            # web_fetch：2 派发成功 + 1 门禁 + 1 级联 + 1 真实失败 + 1 未执行
            {"capability": "web_fetch", "result": {"status": "SUCCESS",
             "agent_id": "webfetchworker", "elapsed_sec": 1.0, "result": "ok"}},
            {"capability": "web_fetch", "result": {"status": "SUCCESS",
             "agent_id": "webfetchworker", "elapsed_sec": 1.0, "result": "ok"}},
            {"capability": "web_fetch", "result": {"status": "FAILED",
             "agent_id": None, "elapsed_sec": 0.1,
             "result": "无候选 URL（检索未产出可用来源），已跳过抓取"}},
            {"capability": "web_fetch", "result": {"status": "FAILED",
             "agent_id": None, "elapsed_sec": 0,
             "result": "Blocked by failed dependency: 1"}},
            {"capability": "web_fetch", "result": {"status": "FAILED",
             "agent_id": "webfetchworker", "elapsed_sec": 2.0,
             "result": "URL 无法解析"}},
            {"capability": "web_fetch"},  # 未执行（无 status）
            # content_summary：1 无执行者成功（降级兜底）→ 计划口径分子，不进派发分子
            {"capability": "content_summary", "result": {"status": "SUCCESS",
             "agent_id": None, "elapsed_sec": 0.5, "result": "ok"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "agents.db")
            db = sqlite3.connect(db_path)
            db.execute("CREATE TABLE task_history (steps_json TEXT)")
            db.execute("INSERT INTO task_history VALUES (?)",
                       (json.dumps(steps, ensure_ascii=False),))
            db.commit()
            db.close()
            with mock.patch.object(mc.db_paths, "resolve_db_path",
                                   return_value=db_path):
                stats = mc._db_capability_stats()
        self.assertTrue(stats["available"])
        self.assertEqual(stats["step_total"], 7)
        wf = stats["by_capability"]["web_fetch"]
        self.assertEqual(wf["total_planned"], 6)
        self.assertEqual(wf["total_dispatched"], 3)
        self.assertEqual(wf["success"], 2)
        self.assertEqual(wf["success_dispatched"], 2)
        self.assertEqual(wf["unexecuted"], 1)
        self.assertEqual(wf["gate_blocked"], 1)
        self.assertEqual(wf["cascade_blocked"], 1)
        self.assertEqual(wf["genuine_failed"], 1)
        self.assertEqual(wf["other"], 0)
        self.assertEqual(wf["success_rate_dispatched"], 66.7)
        self.assertEqual(wf["success_rate_planned"], 33.3)
        cs = stats["by_capability"]["content_summary"]
        # 无执行者的成功步不算派发成功（否则出现 105% 的荒谬派发率）
        self.assertEqual(cs["total_dispatched"], 0)
        self.assertEqual(cs["success"], 1)
        self.assertIsNone(cs["success_rate_dispatched"])
        self.assertEqual(cs["success_rate_planned"], 100.0)
        # 口径声明随结果下发
        self.assertIn("success_rate_planned", stats["scope"])
        self.assertIn("2026-09", stats["scope"])


class TestWriteSummaryByCapability(unittest.TestCase):
    def test_summary_keeps_legacy_keys_and_adds_split(self):
        """旧键名（total/success/success_rate）保留：前端 MetricsPage 在读；
        `success_rate` 换成真实派发口径，历史口径进 `success_rate_planned`。"""
        cap_stats = {
            "available": True,
            "scope": "task_history.steps_json 全表聚合（测试桩）… "
                     "success_rate_planned 为历史口径（含噪音）",
            "by_capability": {
                "web_fetch": mc._finalize_cap_row(dict(
                    mc._new_cap_row(), total_planned=101, total_dispatched=74,
                    success=69, success_dispatched=69, unexecuted=12,
                    gate_blocked=10, cascade_blocked=5, genuine_failed=5)),
                "package": mc._finalize_cap_row(dict(
                    mc._new_cap_row(), total_planned=163, total_dispatched=121,
                    success=127, success_dispatched=121, unexecuted=12,
                    cascade_blocked=24, genuine_failed=0)),
            },
        }
        db_totals = {"available": True, "total": 2, "success": 2,
                     "with_issues": 0, "failed": 0, "cancelled": 0,
                     "running": 0, "queued": 0, "unknown": 0, "terminal": 2,
                     "scope": "task_history 全表"}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(mc, "METRICS_FILE",
                                   str(Path(tmp) / "metrics.csv")), \
                 mock.patch.object(mc, "SUMMARY_FILE",
                                   str(Path(tmp) / "summary.json")), \
                 mock.patch.object(mc, "_db_capability_stats",
                                   return_value=cap_stats), \
                 mock.patch.object(mc, "_db_task_totals",
                                   return_value=db_totals):
                collector = mc.MetricsCollector()
                collector._write_summary()
                # Windows：先关 CSV 句柄再让 TemporaryDirectory 清理
                collector.shutdown()
            summary = json.loads(
                (Path(tmp) / "summary.json").read_text(encoding="utf-8"))
        by = summary["by_capability"]
        # 旧键名兼容
        for cap in ("web_fetch", "package"):
            self.assertIn("total", by[cap])
            self.assertIn("success", by[cap])
            self.assertIn("success_rate", by[cap])
        self.assertEqual(by["web_fetch"]["success_rate"], 93.2)
        self.assertEqual(by["web_fetch"]["success_rate_planned"], 68.3)
        self.assertEqual(by["package"]["success_rate"], 100.0)
        self.assertEqual(by["package"]["success_rate_planned"], 77.9)
        # 新分层键
        self.assertEqual(by["web_fetch"]["genuine_failed"], 5)
        self.assertEqual(by["web_fetch"]["total_dispatched"], 74)
        self.assertEqual(by["web_fetch"]["gate_blocked"], 10)
        self.assertEqual(by["web_fetch"]["cascade_blocked"], 5)
        self.assertEqual(by["web_fetch"]["unexecuted"], 12)
        self.assertEqual(by["package"]["genuine_failed"], 0)
        self.assertIn("by_capability_scope", summary)
        self.assertTrue(summary["by_capability_available"])
        self.assertIn("success_rate_planned", summary["by_capability_metric_note"])
        self.assertIn("含噪音", summary["by_capability_metric_note"])


if __name__ == "__main__":
    unittest.main()
