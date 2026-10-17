"""任务17 测试：capability×executor 交叉表进 metrics + mismatch 告警。

覆盖四层：
1. `capability_routing.py` 成为白名单唯一定义，orchestrator 仍以同名暴露；
2. `metrics_collector._db_capability_stats()` 给出每能力的
   `executor_distribution` 与 `mismatch_count`（表外能力不设限）；
3. `_write_summary` 落 `routing_health`（含 by_capability 交叉表透传）；
4. `worker_guardian` 心跳读 routing_health，错配发一次
   `capability_routing_mismatch` 告警（同指纹去重、消失后复位）。

按文件运行：python test_task17_routing_metrics.py
"""

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import capability_routing as cr
import metrics_collector as mc
import worker_guardian as wg

EXPECTED_MAP = {
    "web_search": {"search_agent"},
    "web_fetch": {"webfetchworker"},
    "code_execution": {"codeexecworker"},
    "react_agent": {"reactagentworker"},
    "report_generator": {"reportgeneratorworker"},
    "data_analyzer": {"dataanalyzerworker"},
}


class TestSharedRoutingMap(unittest.TestCase):
    """任务17 步骤4：白名单只留一份定义。"""

    def test_routing_module_holds_the_map(self):
        self.assertEqual(cr.CAPABILITY_WORKER_MAP, EXPECTED_MAP)

    def test_orchestrator_still_exposes_the_same_object(self):
        import orchestrator_v2 as ov
        self.assertEqual(ov.CAPABILITY_WORKER_MAP, EXPECTED_MAP)
        # 不是复制了一份，而是同一对象：改一处两处都变
        self.assertIs(ov.CAPABILITY_WORKER_MAP, cr.CAPABILITY_WORKER_MAP)

    def test_orchestrator_has_no_hardcoded_copy(self):
        src = Path(__file__).with_name("orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertNotIn('"web_search": {"search_agent"}', src)

    def test_expected_executors_uses_none_for_unrestricted(self):
        self.assertEqual(cr.expected_executors("web_search"), {"search_agent"})
        self.assertIsNone(cr.expected_executors("content_summary"))
        self.assertIsNone(cr.expected_executors(""))
        self.assertIsNone(cr.expected_executors(None))


class TestExecutorCrossTable(unittest.TestCase):
    """_db_capability_stats：每个能力的执行者分布与错配计数。"""

    def _stats(self, steps):
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
                return mc._db_capability_stats()

    def test_distribution_and_mismatch_count(self):
        steps = [
            # web_search：2 步 lawful + 1 步被代做（任务12 复盘的真实形态）
            {"capability": "web_search", "result": {
                "status": "SUCCESS", "agent_id": "search_agent"}},
            {"capability": "web_search", "result": {
                "status": "SUCCESS", "agent_id": "search_agent"}},
            {"capability": "web_search", "result": {
                "status": "SUCCESS", "agent_id": "content_summarizer"}},
            # web_fetch：白名单外 1 步 + 未派发 1 步（不进交叉表）
            {"capability": "web_fetch", "result": {
                "status": "FAILED", "agent_id": "content_summarizer"}},
            {"capability": "web_fetch", "result": {
                "status": "FAILED", "agent_id": None}},
            # 表外能力：任何执行者都不算错配
            {"capability": "content_summary", "result": {
                "status": "SUCCESS", "agent_id": "someoneelse"}},
        ]
        stats = self._stats(steps)
        self.assertTrue(stats["available"])

        ws = stats["by_capability"]["web_search"]
        self.assertEqual(ws["executor_distribution"],
                         {"search_agent": 2, "content_summarizer": 1})
        self.assertEqual(ws["mismatch_count"], 1)
        self.assertEqual(ws["total_dispatched"], 3)

        wf = stats["by_capability"]["web_fetch"]
        self.assertEqual(wf["executor_distribution"], {"content_summarizer": 1})
        self.assertEqual(wf["mismatch_count"], 1)
        self.assertEqual(wf["total_dispatched"], 1)   # agent_id 为空的未派发步不计

        cs = stats["by_capability"]["content_summary"]
        self.assertEqual(cs["executor_distribution"], {"someoneelse": 1})
        self.assertEqual(cs["mismatch_count"], 0)     # 表外能力不设限

        self.assertIn("capability_routing.py", stats["scope"])

    def test_distribution_sorted_by_step_count_desc(self):
        steps = [
            {"capability": "web_fetch", "result": {
                "status": "SUCCESS", "agent_id": "webfetchworker"}},
            {"capability": "web_fetch", "result": {
                "status": "SUCCESS", "agent_id": "webfetchworker"}},
            {"capability": "web_fetch", "result": {
                "status": "SUCCESS", "agent_id": "webfetchworker"}},
            {"capability": "web_fetch", "result": {
                "status": "FAILED", "agent_id": "content_summarizer"}},
            {"capability": "web_fetch", "result": {
                "status": "FAILED", "agent_id": "content_summarizer"}},
            {"capability": "web_fetch", "result": {
                "status": "FAILED", "agent_id": "otherworker"}},
        ]
        wf = self._stats(steps)["by_capability"]["web_fetch"]
        self.assertEqual(list(wf["executor_distribution"]),
                         ["webfetchworker", "content_summarizer", "otherworker"])
        self.assertEqual(wf["mismatch_count"], 3)     # 白名单外的两步组 + otherworker

    def test_no_agent_id_means_no_mismatch(self):
        """降级兜底的无执行者步：既不在分布里，也不算错配。"""
        wf = self._stats([
            {"capability": "web_fetch", "result": {"status": "SUCCESS"}},
            {"capability": "web_fetch"},          # 未执行
        ])["by_capability"]["web_fetch"]
        self.assertEqual(wf["executor_distribution"], {})
        self.assertEqual(wf["mismatch_count"], 0)


class TestRoutingHealth(unittest.TestCase):
    """_routing_health：汇总面的判据与明细。"""

    def _cap_stats(self, rows, available=True):
        return {
            "available": available,
            "by_capability": {
                k: mc._finalize_cap_row(dict(mc._new_cap_row(), **v))
                for k, v in rows.items()
            },
        }

    def test_no_mismatch_is_explicitly_false(self):
        rh = mc._routing_health(self._cap_stats({
            "web_search": {"executor_distribution": {"search_agent": 3}},
            "package": {"mismatch_count": 0},
        }))
        self.assertFalse(rh["has_mismatch"])
        self.assertEqual(rh["details"], [])
        self.assertTrue(rh["available"])

    def test_details_only_list_mismatched_capabilities(self):
        rh = mc._routing_health(self._cap_stats({
            "web_search": {"mismatch_count": 24, "executor_distribution": {
                "search_agent": 65, "content_summarizer": 19, "codeexecworker": 5}},
            "web_fetch": {"mismatch_count": 0, "executor_distribution": {
                "webfetchworker": 12}},
        }))
        self.assertTrue(rh["has_mismatch"])
        self.assertEqual([d["capability"] for d in rh["details"]], ["web_search"])
        d = rh["details"][0]
        self.assertEqual(d["mismatch_count"], 24)
        self.assertEqual(d["unexpected_executors"],
                         ["content_summarizer", "codeexecworker"])
        self.assertEqual(d["expected_executors"], ["search_agent"])
        self.assertEqual(d["executor_distribution"]["search_agent"], 65)

    def test_unavailable_is_not_reported_as_healthy(self):
        """库不可用时 has_mismatch 只能是 False + available=False，
        不能伪装成"查过了，没错配"。"""
        rh = mc._routing_health({"available": False, "by_capability": {}})
        self.assertFalse(rh["has_mismatch"])
        self.assertFalse(rh["available"])

    def test_empty_cap_stats_does_not_crash(self):
        rh = mc._routing_health({})
        self.assertFalse(rh["has_mismatch"])


class TestSummaryCarriesRoutingHealth(unittest.TestCase):
    """_write_summary 端到端：交叉表随能力一起下发 + routing_health 落地。"""

    def test_summary_json(self):
        cap_stats = {
            "available": True,
            "scope": "task_history.steps_json 全表聚合（测试桩）… capability_routing.py",
            "by_capability": {
                "web_search": mc._finalize_cap_row(dict(
                    mc._new_cap_row(), total_planned=92, total_dispatched=89,
                    success=80, success_dispatched=78, genuine_failed=8,
                    mismatch_count=24,
                    executor_distribution={"search_agent": 65,
                                           "content_summarizer": 19,
                                           "codeexecworker": 5})),
                "package": mc._finalize_cap_row(dict(
                    mc._new_cap_row(), total_planned=163, total_dispatched=121,
                    success=127, success_dispatched=121)),
            },
        }
        db_totals = {"available": True, "total": 2, "success": 2,
                     "with_issues": 0, "failed": 0, "cancelled": 0,
                     "running": 0, "queued": 0, "unknown": 0, "terminal": 2,
                     "scope": "task_history 全表"}
        with tempfile.TemporaryDirectory() as tmp:
            summary_path = Path(tmp) / "summary.json"
            with mock.patch.object(mc, "METRICS_FILE",
                                   str(Path(tmp) / "metrics.csv")), \
                 mock.patch.object(mc, "SUMMARY_FILE", str(summary_path)), \
                 mock.patch.object(mc, "_db_capability_stats",
                                   return_value=cap_stats), \
                 mock.patch.object(mc, "_db_task_totals",
                                   return_value=db_totals):
                collector = mc.MetricsCollector()
                collector._write_summary()
                collector.shutdown()      # Windows：先关 CSV 句柄再让临时目录清理
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

        ws = summary["by_capability"]["web_search"]
        self.assertEqual(ws["mismatch_count"], 24)
        self.assertEqual(ws["executor_distribution"]["content_summarizer"], 19)
        # 旧口径（运行时标记）仍在，两条口径并存
        self.assertEqual(ws["mismatch_steps"], 0)
        self.assertEqual(summary["routing_health"]["has_mismatch"], True)
        self.assertEqual(summary["routing_health"]["details"][0]["capability"],
                         "web_search")
        self.assertFalse(summary["by_capability"]["package"]["mismatch_count"])


class _FakeMessaging:
    def __init__(self):
        self.published = []

    def publish(self, channel, message):
        self.published.append((channel, message))


class _FakeRegistry:
    def list_agents(self):
        return []

    def register(self, *a, **k):
        pass

    def close(self):
        pass


class TestGuardianRoutingAlert(unittest.TestCase):
    """worker_guardian 心跳：错配告警 + 去重 + 复位。"""

    def _guardian(self, summary):
        """summary=None → 不写汇总文件（metrics 未跑）。"""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = Path(tmp) / "summary.json"
        if summary is not None:
            path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
        g = wg.WorkerGuardian(_FakeMessaging(), _FakeRegistry(), simulate=True)
        patcher = mock.patch.object(mc, "SUMMARY_FILE", str(path))
        patcher.start()
        self.addCleanup(patcher.stop)
        return g, path

    def _alerts(self, g):
        return [m for c, m in g._messaging.published
                if c == "orchestrator:alert"]

    def _mismatch_summary(self, count=24):
        return {"routing_health": {
            "has_mismatch": True,
            "details": [{
                "capability": "web_search",
                "mismatch_count": count,
                "unexpected_executors": ["content_summarizer", "codeexecworker"],
                "expected_executors": ["search_agent"],
                "executor_distribution": {"search_agent": 65,
                                          "content_summarizer": count},
            }],
        }}

    def test_mismatch_raises_one_alert_and_dedupes(self):
        g, _ = self._guardian(self._mismatch_summary())
        g._check_routing_health()
        alerts = self._alerts(g)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "capability_routing_mismatch")
        self.assertIn("web_search", alerts[0]["message"])
        self.assertIn("content_summarizer", alerts[0]["message"])
        # 心跳每 30s 一次：同一错配状态不得反复告警
        g._check_routing_health()
        g._check_routing_health()
        self.assertEqual(len(self._alerts(g)), 1)

    def test_worsening_mismatch_realerts(self):
        g, path = self._guardian(self._mismatch_summary(24))
        g._check_routing_health()
        path.write_text(json.dumps(self._mismatch_summary(30), ensure_ascii=False),
                        encoding="utf-8")
        g._check_routing_health()
        self.assertEqual(len(self._alerts(g)), 2)

    def test_resolved_then_recurring_alerts_again(self):
        g, path = self._guardian(self._mismatch_summary())
        g._check_routing_health()
        path.write_text(json.dumps({"routing_health": {
            "has_mismatch": False, "details": []}}, ensure_ascii=False),
            encoding="utf-8")
        g._check_routing_health()
        self.assertEqual(len(self._alerts(g)), 1)      # 恢复健康不告警
        path.write_text(json.dumps(self._mismatch_summary(), ensure_ascii=False),
                        encoding="utf-8")
        g._check_routing_health()
        self.assertEqual(len(self._alerts(g)), 2)      # 再次出现要能再报

    def test_missing_or_broken_summary_is_silent(self):
        g, path = self._guardian(None)
        g._check_routing_health()                      # 文件不存在
        path.write_text("{ not json", encoding="utf-8")
        g._check_routing_health()                      # 损坏的 JSON
        path.write_text(json.dumps({"total_tasks": 1}, ensure_ascii=False),
                        encoding="utf-8")
        g._check_routing_health()                      # 旧版汇总没有 routing_health
        self.assertEqual(self._alerts(g), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
