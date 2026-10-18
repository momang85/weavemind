# -*- coding: utf-8 -*-
"""任务18 回归测试：机器验收**证据**持久化到任务库并透给 API。

对设计文档（docs/arch_phase3_task18_acceptance_evidence.md）的验收口径：
1. `save_acceptance_evidence` 写 `acceptance_json` + `rules_fingerprint` + `updated_at`；
2. `accept_for_body` 在**最后一次** `run_acceptance`（含虚假标注修复后的复检）之后落一次库；
3. 复检**覆盖**旧证据：库里只留最后一轮的结论；
4. 投影/API 有 `acceptance_evidence` 键；旧任务（库里只有终态摘要）→ `None` 且不报错；
5. 写库失败只 `logger.warning`，不打断交付流水线。

与文档的三处有意偏离（已在校验里写死口径）：
- 文档的"GET /api/task/<id>"在本仓库不存在；任务详情的 JSON 出口是 `GET /task/<id>`
  （`web_ui._get_task_page`），其事实层来自 `task_state.task_projection` → 键加在那里；
- 逐项 `checks` 入库前瘦身（单条 >2000 字符只留判定与短说明）：整份 checks 实测 40~80KB，
  而这一列每次任务页读取都要解析；明细仍在 `acceptance_report.json` 与验收事件流里；
- 终态摘要（`record_completion`）不再把已有证据覆盖成空：见 `_acceptance_write_columns`。
"""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import delivery_pipeline as dp
import task_state as ts


def _fake_result(sha: str = "a" * 64, overall: str = "pass",
                 checks: dict | None = None, fingerprint: str = "fp-task18") -> dict:
    """按 `acceptance_checker.run_acceptance` 的真实返回形状造假。"""
    return {
        "report_id": "t-18",
        "goal": "研究某公司两期经营",
        "checks": checks if checks is not None else {
            "disclaimer": {"pass": True, "details": "有免责声明", "applicable": True},
            "source_labeling": {"pass": True, "details": "标注可信", "mislabeled": []},
        },
        "overall": overall,
        "profile": "research",
        "report_type": "research_brief",
        "gaps": [] if overall == "pass" else ["缺少来源清单"],
        "suggestions": [],
        "rules_version": "2026.09.10",
        "rules_fingerprint": fingerprint,
        "report_sha256": sha,
        "report_sha256_short": sha[:16],
        "evaluated_at": "2026-10-12T00:00:00+00:00",
    }


class _TempDbCase(unittest.TestCase):
    """临时任务库 + 临时工作区，并把 `task_state.DB_PATH` 指过去。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_t18_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = str(self.tmp / "agents.db")
        self.ws = self.tmp / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)
        ts.ensure_schema(self.db)
        self._patches = []

        def _p(target, **kwargs):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self._patches.append(patcher)

        _p("task_state.DB_PATH", new=self.db)
        _p("workspace.task_reports_dir", return_value=self.ws / "reports")
        _p("acceptance_checker.append_acceptance_event", return_value=None)
        env = mock.patch.dict(os.environ, {"URL_HEALTH_CHECK": "0"}, clear=False)
        env.start()
        self._patches.append(env)
        for p in self._patches:
            self.addCleanup(p.stop)

    def _row(self, task_id: str) -> sqlite3.Row:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(
                "SELECT * FROM task_history WHERE task_id=?", (task_id,)).fetchone()
        finally:
            con.close()

    def _queued(self, task_id: str = "t-18") -> str:
        ts.mark_queued(task_id, "研究某公司两期经营", db_path=self.db)
        return task_id

    def run_accept(self, task_id: str, result: dict, body: str = "正文 v1") -> dict | None:
        """跑一次真实链路（`accept_for_body`），只把 `run_acceptance` 换成假的。"""
        with mock.patch("acceptance_checker.run_acceptance", return_value=result):
            return dp.accept_for_body(task_id, "研究某公司两期经营", body,
                                      trigger="测试", verify_body_as_is=True,
                                      ws_dir=str(self.ws))


class TestSaveAcceptanceEvidence(_TempDbCase):
    def test_writes_columns_and_fingerprint(self):
        tid = self._queued()
        evidence = _acceptance_evidence_dict()
        self.assertTrue(ts.save_acceptance_evidence(tid, evidence, db_path=self.db))
        row = self._row(tid)
        self.assertEqual(json.loads(row["acceptance_json"])["overall"], "pass")
        self.assertEqual(row["rules_fingerprint"], "fp-task18")
        self.assertTrue(row["updated_at"], "updated_at 必须一起刷新")

    def test_no_row_no_insert(self):
        """库里没有这一行（任务未入库）→ 只 UPDATE、返回 False，不伪造行。"""
        self.assertFalse(ts.save_acceptance_evidence("t-ghost",
                                                     _acceptance_evidence_dict(),
                                                     db_path=self.db))
        self.assertIsNone(self._row("t-ghost"))

    def test_empty_or_bad_input_is_noop(self):
        tid = self._queued()
        self.assertFalse(ts.save_acceptance_evidence(tid, {}, db_path=self.db))
        self.assertFalse(ts.save_acceptance_evidence(tid, None, db_path=self.db))
        self.assertFalse(ts.save_acceptance_evidence(tid, ["not a dict"],
                                                     db_path=self.db))

    def test_db_error_returns_false_and_never_raises(self):
        """写不进去（库坏了）只返回 False + warning：不得把异常抛给交付流水线。"""
        with mock.patch("task_state._connect", side_effect=sqlite3.OperationalError("busy")) as con:
            self.assertFalse(ts.save_acceptance_evidence(
                "t-18", _acceptance_evidence_dict(), db_path=self.db))
            con.assert_called_once()

    def test_unserializable_evidence_is_caught(self):
        tid = self._queued()
        bad = dict(_acceptance_evidence_dict())
        bad["checks"] = { "循环": bad }           # 自引用 → json.dumps 必失败
        self.assertFalse(ts.save_acceptance_evidence(tid, bad, db_path=self.db))


def _acceptance_evidence_dict() -> dict:
    return dp._acceptance_evidence(_fake_result())


class TestPipelinePersistsEvidence(_TempDbCase):
    def test_accept_for_body_writes_evidence(self):
        tid = self._queued()
        result = self.run_accept(tid, _fake_result())
        self.assertIsNotNone(result)
        row = self._row(tid)
        stored = json.loads(row["acceptance_json"] or "{}")
        self.assertEqual(stored.get("overall"), "pass")
        self.assertIn("checks", stored, "逐项结论必须入库（此前只有文件与审计事件有）")
        self.assertEqual(stored.get("rules_fingerprint"), "fp-task18")
        self.assertEqual(stored.get("report_type"), "research_brief")
        self.assertTrue(stored.get("evaluated_at"))
        self.assertEqual(row["rules_fingerprint"], "fp-task18")
        # 投影/API 透传
        proj = ts.task_projection(tid, self.db)
        self.assertIsNotNone(proj["acceptance_evidence"])
        self.assertIn("disclaimer", proj["acceptance_evidence"]["checks"])

    def test_recheck_overwrites_previous_evidence(self):
        """复检（修复后那一轮）必须**覆盖**旧证据，库里只留最后一轮。"""
        tid = self._queued()
        self.run_accept(tid, _fake_result(overall="fail", fingerprint="fp-old"))
        self.run_accept(tid, _fake_result(overall="pass", fingerprint="fp-new"))
        stored = json.loads(self._row(tid)["acceptance_json"] or "{}")
        self.assertEqual(stored.get("overall"), "pass")
        self.assertEqual(stored.get("rules_fingerprint"), "fp-new")
        self.assertEqual(self._row(tid)["rules_fingerprint"], "fp-new")

    def test_write_failure_does_not_break_delivery(self):
        tid = self._queued()
        with mock.patch("task_state.save_acceptance_evidence",
                        side_effect=RuntimeError("boom")) as saver:
            result = self.run_accept(tid, _fake_result())
        saver.assert_called_once()
        self.assertIsNotNone(result, "证据入库失败不得让验收/交付链路返回 None")

    def test_no_score_key_when_accepter_has_none(self):
        """验收器不产出分数：键如实缺席（不编 0 分）。"""
        tid = self._queued()
        self.run_accept(tid, _fake_result())
        stored = json.loads(self._row(tid)["acceptance_json"] or "{}")
        self.assertNotIn("score", stored)


class TestCompaction(unittest.TestCase):
    def test_oversized_check_trimmed_verdict_kept(self):
        big = {"pass": False, "status": "fail", "details": "x" * 5000,
               "traces": [{"n": i} for i in range(2000)], "gaps": ["缺 3 笔追溯"]}
        out = dp._compact_acceptance_checks({"number_traceability": big})
        slim = out["number_traceability"]
        self.assertTrue(slim["trimmed"])
        self.assertIs(slim["pass"], False)
        self.assertEqual(slim["gaps"], ["缺 3 笔追溯"])
        self.assertLessEqual(len(slim["details"]), 400)
        self.assertNotIn("traces", slim, "逐笔明细不入库")

    def test_small_checks_untouched(self):
        checks = {"disclaimer": {"pass": True, "details": "有"}}
        self.assertEqual(dp._compact_acceptance_checks(checks), checks)


class TestTerminalStateKeepsEvidence(_TempDbCase):
    """终态落库不得把已入库的逐项证据抹掉（失败任务最需要这份账）。"""

    def _seed_evidence(self, tid: str, sha: str = "b" * 64) -> None:
        evidence = dp._acceptance_evidence(_fake_result(sha=sha))
        self.assertTrue(ts.save_acceptance_evidence(tid, evidence, db_path=self.db))

    def test_empty_summary_keeps_prior_evidence(self):
        tid = self._queued()
        self._seed_evidence(tid)
        self.assertTrue(ts.record_completion(tid, status=ts.FAILED, report="",
                                             acceptance=None, db_path=self.db))
        stored = json.loads(self._row(tid)["acceptance_json"] or "{}")
        self.assertIn("checks", stored, "FAILED 终态不带摘要时不得清空证据")

    def test_summary_same_body_carries_checks(self):
        tid = self._queued()
        sha = "c" * 64
        self._seed_evidence(tid, sha=sha)
        self.assertTrue(ts.record_completion(
            tid, status=ts.SUCCESS, report="正文",
            acceptance={"overall": "pass", "gaps": [], "report_sha256": sha,
                        "rules_version": "2026.09.10", "rules_fingerprint": "fp-task18"},
            db_path=self.db))
        stored = json.loads(self._row(tid)["acceptance_json"] or "{}")
        self.assertIn("checks", stored)
        self.assertEqual(stored["rules_fingerprint"], "fp-task18")

    def test_projection_update_keeps_evidence_for_same_body(self):
        """人工修订的投影更新（`update_delivery_projection`）同样不抹同正文证据。"""
        tid = self._queued()
        sha = "g" * 64
        self._seed_evidence(tid, sha=sha)
        self.assertTrue(ts.update_delivery_projection(
            tid, report="修订后的正文",
            acceptance={"overall": "pass", "gaps": [], "report_sha256": sha,
                        "rules_fingerprint": "fp-task18"}, db_path=self.db))
        stored = json.loads(self._row(tid)["acceptance_json"] or "{}")
        self.assertIn("checks", stored)

    def test_summary_other_body_does_not_claim_old_checks(self):
        """正文换了（人工修订/换版）：旧明细属于别的版本，不得冒充本版结论。"""
        tid = self._queued()
        self._seed_evidence(tid, sha="d" * 64)
        self.assertTrue(ts.record_completion(
            tid, status=ts.SUCCESS, report="新正文",
            acceptance={"overall": "fail", "gaps": ["缺来源"],
                        "report_sha256": "e" * 64, "rules_fingerprint": "fp-2"},
            db_path=self.db))
        stored = json.loads(self._row(tid)["acceptance_json"] or "{}")
        self.assertNotIn("checks", stored)
        self.assertEqual(stored["overall"], "fail")
        self.assertEqual(self._row(tid)["rules_fingerprint"], "fp-2")


class TestLegacyTaskHasNoEvidence(_TempDbCase):
    def test_old_task_projection_returns_none(self):
        """旧任务：只有终态摘要、没有逐项证据 → None，不抛（API 键缺席即"未入库"）。"""
        tid = self._queued("t-old")
        self.assertTrue(ts.record_completion(
            tid, status=ts.SUCCESS, report="正文",
            acceptance={"overall": "pass", "gaps": [], "report_sha256": "f" * 64,
                        "rules_fingerprint": "fp-old"}, db_path=self.db))
        row = self._row(tid)
        self.assertNotIn("checks", json.loads(row["acceptance_json"] or "{}"))
        proj = ts.task_projection(tid, self.db)
        self.assertIsNone(proj["acceptance_evidence"])
        self.assertEqual(proj["acceptance"]["overall"], "pass", "摘要语义不变")

    def test_detail_endpoint_passes_the_key(self):
        """`GET /task/<id>` 的 payload 必须带上这个键（本仓库没有 /api/task/<id> 详情路由）。"""
        src = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertIn('"acceptance_evidence": data.get("acceptance_evidence")', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
