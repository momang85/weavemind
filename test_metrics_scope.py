# -*- coding: utf-8 -*-
"""指标统计范围与口径（架构审查 F1 补修 ①）。

背景：前端曾用 `/api/metrics` 的全局任务数**减去** `/tasks` 最近 50 条里的运行数来
"推算"成功率——反例是 100 个任务全在运行、列表只回 50 条时被算成 100% 成功。
现在统计全部由后端**一次分组查询、同一范围（task_history 全表）**给出，前端只做展示。

本文件钉住四条：
- 运行中任务数必须完整（不受任何列表分页影响）
- 全部在运行（终态 0）时成功率/失败率为 None（未知），不是 0 也不是 100
- 混合终态时分母 = 终态数；SUCCESS_WITH_ISSUES 与 CANCELLED 单列，不算"成功"
- 数据不可用（库缺失）时 `available=False`，不得当作"0 个失败"
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import metrics_collector as mc


def _make_db(rows: list[tuple[str, str]]) -> str:
    tmp = tempfile.mkdtemp(prefix="wm_metrics_scope_")
    path = str(Path(tmp) / "agents.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE task_history(task_id TEXT PRIMARY KEY, status TEXT)")
    con.executemany("INSERT INTO task_history VALUES(?,?)", rows)
    con.commit()
    con.close()
    return path


def _env_for_db(path: str) -> dict:
    # 三个变量都指到同一份临时库：resolve_db_path 的优先级与真实部署一致
    return {"WEAVEMIND_DB": path, "REGISTRY_DB": path, "AGENTS_DB": path}


class TestTaskStatusDistribution(unittest.TestCase):
    def _totals(self, rows: list[tuple[str, str]]) -> dict:
        path = _make_db(rows)
        self.addCleanup(shutil.rmtree, os.path.dirname(path), ignore_errors=True)
        with mock.patch.dict(os.environ, _env_for_db(path), clear=False):
            return mc._db_task_totals()

    def test_more_than_fifty_running_are_all_counted(self):
        """100 个运行中：必须全部计入（前端曾只能看到最近 50 条 → 反例的根源）。"""
        rows = [(f"r-{i}", "RUNNING") for i in range(100)] + [("s-1", "SUCCESS")]
        t = self._totals(rows)
        self.assertTrue(t["available"])
        self.assertEqual(t["running"], 100)
        self.assertEqual(t["terminal"], 1)
        self.assertEqual(t["total"], 101)
        self.assertEqual(t["success"], 1)
        # 成功率分母是终态：1/1 = 100%，但这是"只有 1 个终态任务"的真实含义，
        # 与"100 个在跑也算 100% 成功"的伪结论不同——前端必须同时显示分母。
        self.assertEqual(t["terminal"], 1)

    def test_all_running_has_zero_terminal(self):
        t = self._totals([(f"r-{i}", "RUNNING") for i in range(12)])
        self.assertEqual(t["running"], 12)
        self.assertEqual(t["terminal"], 0)
        self.assertEqual(t["success"], 0)
        self.assertEqual(t["failed"], 0)

    def test_mixed_terminal_classification(self):
        rows = [("a", "SUCCESS"), ("b", "SUCCESS"), ("c", "SUCCESS_WITH_ISSUES"),
                ("d", "FAILED"), ("e", "CANCELLED"), ("f", "RUNNING"),
                ("g", "QUEUED"), ("h", "PENDING"), ("i", "完全没见过的状态")]
        t = self._totals(rows)
        self.assertEqual((t["success"], t["with_issues"], t["failed"], t["cancelled"]),
                         (2, 1, 1, 1))
        self.assertEqual(t["terminal"], 5, "终态 = 成功 + 有缺口 + 失败 + 已取消")
        self.assertEqual((t["running"], t["queued"]), (1, 2))
        self.assertEqual(t["unknown"], 1, "未登记状态计入 unknown，不得并入成功或失败")
        self.assertEqual(t["total"], 9)

    def test_unavailable_is_not_zero_failure(self):
        missing = str(Path(tempfile.gettempdir()) / "definitely_missing_wm_scope.db")
        with mock.patch.dict(os.environ, _env_for_db(missing), clear=False):
            t = mc._db_task_totals()
        self.assertFalse(t["available"], "库不可读必须标 available=False")
        self.assertEqual(t["failed"], 0)
        self.assertEqual(t["terminal"], 0)
        self.assertIn("scope", t)


class TestSummaryRateContract(unittest.TestCase):
    """`_write_summary` 的成功/失败率：分母 = 终态；无终态或数据不可用 → None（未知）。"""

    def _summary(self, rows: list[tuple[str, str]] | None = None,
                 db_missing: bool = False) -> dict:
        out = tempfile.mkdtemp(prefix="wm_metrics_sum_")
        self.addCleanup(shutil.rmtree, out, ignore_errors=True)
        summary_path = str(Path(out) / "summary.json")

        collector = mc.MetricsCollector.__new__(mc.MetricsCollector)
        collector._lock = threading.Lock()          # noqa: SLF001 - 测试替身按需补齐属性
        collector._success_tasks = 0
        collector._total_tasks = 0
        collector._failed_tasks = 0
        collector._recent_tasks = []
        collector._critic_pass = 0
        collector._critic_fail = 0
        collector._replan_count = 0
        collector._replan_success = 0
        collector._alerts = 0
        collector._by_capability = {}

        if db_missing:
            db_path = str(Path(tempfile.gettempdir()) / "definitely_missing_wm_sum.db")
        else:
            db_path = _make_db(rows or [])
            self.addCleanup(shutil.rmtree, os.path.dirname(db_path), ignore_errors=True)

        env = _env_for_db(db_path)
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(mc, "SUMMARY_FILE", summary_path):
            collector._write_summary()
        return json.loads(Path(summary_path).read_text(encoding="utf-8"))

    def test_rates_use_terminal_denominator(self):
        s = self._summary([("a", "SUCCESS"), ("b", "SUCCESS"), ("c", "FAILED"),
                           ("d", "SUCCESS_WITH_ISSUES"), ("e", "RUNNING")])
        self.assertEqual(s["tasks"]["terminal"], 4)
        self.assertEqual(s["tasks"]["running"], 1)
        self.assertEqual(s["success_rate"], 50.0, "2/4：运行中不进分母")
        self.assertEqual(s["failure_rate"], 25.0, "1/4：不再用 100-success 推算")
        self.assertEqual(s["total_tasks"], 5)
        self.assertIn("scope", s["tasks"])

    def test_all_running_rates_are_none(self):
        s = self._summary([("a", "RUNNING"), ("b", "QUEUED")])
        self.assertIsNone(s["success_rate"], "无终态任务时不得给 0 或 100")
        self.assertIsNone(s["failure_rate"])

    def test_db_unavailable_marks_unknown(self):
        s = self._summary(db_missing=True)
        self.assertFalse(s["tasks"]["available"])
        self.assertIsNone(s["success_rate"])
        self.assertIsNone(s["failure_rate"])


if __name__ == "__main__":
    unittest.main()
