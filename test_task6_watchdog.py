# -*- coding: utf-8 -*-
"""任务6 watchdog 验收（docs/arch_task6_watchdog.md）：stuck RUNNING 任务自动回收。

覆盖：
- 判定公式 is_stuck = (now - 起点) > task_timeout × 1.5（300 → 450s），且下限 60s；
- task_timeout <= 0 视为不限时，不回收；
- 防误杀双闸门：快照仍新鲜 → 跳过；运行标记持有者存活/不可判定 → 跳过（只认 False）；
- CAS：真实终态恰好在同一瞬间落地时让位（不动 SUCCEEDED 行，也不告警）；
- 回收副作用：状态 CANCELLED、phase "看门狗回收"、completed_at 落下、Redis 占位键释放、
  告警 task_watchdog_timeout、审计 task.watchdog_timeout；
- 接线：run() 启动先回收一轮，之后每次 Guardian 心跳（30s）再回收一轮。

离线且确定性：不连真实 Redis/SQLite（临时库）、不调模型、不改真实 config.json
（配置文本由替身 Path 提供）。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import audit_logger  # noqa: E402
import db_paths  # noqa: E402
import task_state  # noqa: E402
import worker_guardian  # noqa: E402

SCHEMA = (
    "CREATE TABLE task_history("
    " task_id TEXT PRIMARY KEY,"
    " status TEXT,"
    " phase TEXT,"
    " report TEXT,"
    " created_at TEXT,"
    " updated_at TEXT,"
    " completed_at TEXT)"
)


class _StubPath:
    """替身 Path：让任务6 读到的 config.json 文本由测试决定（真实文件一概不碰）。"""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def resolve(self) -> "_StubPath":
        return self

    @property
    def parent(self) -> "_StubPath":
        return self

    def __truediv__(self, _other) -> "_StubPath":
        return self

    def read_text(self, encoding: str | None = None) -> str:
        return self._payload


class _FakeRedis:
    """只实现任务6 用到的 get/delete，并记录删除过的键。"""

    def __init__(self, store: dict | None = None) -> None:
        self.store = dict(store or {})
        self.deleted: list[str] = []

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        removed = 0
        for key in keys:
            self.deleted.append(key)
            if self.store.pop(key, None) is not None:
                removed += 1
        return removed


class _FakeMessaging:
    def __init__(self, store: dict | None = None) -> None:
        self.redis = _FakeRedis(store)
        self.published: list[tuple] = []

    def publish(self, channel, payload):
        self.published.append((channel, payload))

    def alert_types(self) -> list[str]:
        return [p.get("type") for ch, p in self.published
                if ch == "orchestrator:alert"]


class _Harness:
    """临时库 + 无 __init__ 的 Guardian 实例 + 记录型替身。"""

    def __init__(self, db: Path, guardian, messaging: _FakeMessaging,
                 audits: list) -> None:
        self.db = db
        self.guardian = guardian
        self.messaging = messaging
        self.audits = audits

    def add_task(self, task_id: str, status: str | None = None,
                 stale_seconds: int | None = None) -> None:
        status = status or task_state.RUNNING
        con = sqlite3.connect(self.db)
        try:
            if stale_seconds is None:
                con.execute(
                    "INSERT INTO task_history(task_id,status,phase,created_at,updated_at)"
                    " VALUES(?,?,'',datetime('now'),datetime('now'))",
                    (task_id, status))
            else:
                age = f"-{int(stale_seconds)} seconds"
                con.execute(
                    "INSERT INTO task_history(task_id,status,phase,created_at,updated_at)"
                    " VALUES(?,?,'',datetime('now',?),datetime('now',?))",
                    (task_id, status, age, age))
            con.commit()
        finally:
            con.close()

    def set_status(self, task_id: str, status: str) -> None:
        con = sqlite3.connect(self.db)
        try:
            con.execute("UPDATE task_history SET status=? WHERE task_id=?",
                        (status, task_id))
            con.commit()
        finally:
            con.close()

    def row(self, task_id: str):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(
                "SELECT status, phase, report, completed_at FROM task_history"
                " WHERE task_id=?", (task_id,)).fetchone()
        finally:
            con.close()

    def reap(self) -> int:
        return self.guardian._reap_stuck_tasks()


@contextmanager
def harness(task_timeout: int = 300, snapshot_age=None, pid_verdict=None,
            redis_store: dict | None = None, on_pid_check=None):
    """构造隔离环境：临时 agents.db、替身配置、替身闸门判据、替身审计。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "agents.db"
        con = sqlite3.connect(db)
        con.execute(SCHEMA)
        con.commit()
        con.close()

        messaging = _FakeMessaging(redis_store)
        guardian = worker_guardian.WorkerGuardian.__new__(worker_guardian.WorkerGuardian)
        guardian._messaging = messaging
        guardian._running = False
        guardian._grace_seconds = worker_guardian.GRACE_SECONDS_DEFAULT

        audits: list = []
        cfg = json.dumps({"system": {"task_timeout": task_timeout}})

        def _snapshot(_task_id):
            return snapshot_age

        def _pid(_pid_value, _started=""):
            if on_pid_check is not None:
                on_pid_check()
            return pid_verdict

        with mock.patch.object(db_paths, "resolve_db_path", return_value=str(db)), \
                mock.patch.object(task_state, "snapshot_age", side_effect=_snapshot), \
                mock.patch.object(task_state, "pid_same_process", side_effect=_pid), \
                mock.patch.object(audit_logger, "audit_log",
                                  side_effect=lambda *a, **k: audits.append((a, k))), \
                mock.patch.object(worker_guardian, "Path",
                                  lambda *a, **k: _StubPath(cfg)):
            yield _Harness(db, guardian, messaging, audits)


class TestStuckFormula(unittest.TestCase):
    """判定公式：超 task_timeout × 1.5 才算 stuck，下限 60s。"""

    def test_stale_running_task_is_reaped(self):
        with harness(task_timeout=300) as h:
            h.add_task("t-stale", stale_seconds=600)
            self.assertEqual(h.reap(), 1)
            status, phase, report, completed_at = h.row("t-stale")
            self.assertEqual(status, task_state.CANCELLED)
            self.assertEqual(phase, "看门狗回收")
            self.assertIn("watchdog_timeout", report or "")
            self.assertTrue(completed_at, "回收必须落下 completed_at")

    def test_fresh_running_task_is_untouched(self):
        with harness(task_timeout=300) as h:
            h.add_task("t-fresh", stale_seconds=10)
            self.assertEqual(h.reap(), 0)
            self.assertEqual(h.row("t-fresh")[0], task_state.RUNNING)

    def test_threshold_is_timeout_times_one_point_five(self):
        """300s 配置 → 450s 阈值：449s 不动、451s 回收。"""
        with harness(task_timeout=300) as h:
            h.add_task("t-449", stale_seconds=449)
            h.add_task("t-451", stale_seconds=451)
            self.assertEqual(h.reap(), 1)
            self.assertEqual(h.row("t-449")[0], task_state.RUNNING)
            self.assertEqual(h.row("t-451")[0], task_state.CANCELLED)

    def test_threshold_floor_is_sixty_seconds(self):
        """小配置（30s → 45s）被下限兜到 60s：59s 不动、61s 回收。"""
        with harness(task_timeout=30) as h:
            h.add_task("t-59", stale_seconds=59)
            h.add_task("t-61", stale_seconds=61)
            self.assertEqual(h.reap(), 1)
            self.assertEqual(h.row("t-59")[0], task_state.RUNNING)
            self.assertEqual(h.row("t-61")[0], task_state.CANCELLED)

    def test_non_running_rows_are_ignored(self):
        with harness(task_timeout=300) as h:
            h.add_task("t-done", status=task_state.SUCCESS, stale_seconds=6000)
            h.add_task("t-cancelled", status=task_state.CANCELLED, stale_seconds=6000)
            self.assertEqual(h.reap(), 0)

    def test_timeout_zero_or_negative_disables_reaping(self):
        for value in (0, -1):
            with self.subTest(task_timeout=value), \
                    harness(task_timeout=value) as h:
                h.add_task("t-stale", stale_seconds=6000)
                self.assertEqual(h.reap(), 0)
                self.assertEqual(h.row("t-stale")[0], task_state.RUNNING)


class TestAntiFalseKillGates(unittest.TestCase):
    """双闸门：进度仍在流动或持有者未确认已死 → 保守跳过。"""

    def test_fresh_snapshot_blocks_reap(self):
        with harness(task_timeout=300, snapshot_age=12.0) as h:
            h.add_task("t-live-progress", stale_seconds=600)
            self.assertEqual(h.reap(), 0)
            self.assertEqual(h.row("t-live-progress")[0], task_state.RUNNING)

    def test_stale_snapshot_does_not_block_reap(self):
        with harness(task_timeout=300, snapshot_age=900.0) as h:
            h.add_task("t-no-progress", stale_seconds=600)
            self.assertEqual(h.reap(), 1)

    def test_live_holder_blocks_reap(self):
        store = {"task_running:t-live": json.dumps({"pid": 4242,
                                                    "started": "2026-01-01T00:00:00Z"})}
        with harness(task_timeout=300, pid_verdict=True, redis_store=store) as h:
            h.add_task("t-live", stale_seconds=600)
            self.assertEqual(h.reap(), 0)
            self.assertEqual(h.row("t-live")[0], task_state.RUNNING)

    def test_undetermined_holder_blocks_reap(self):
        """None = 无法判定，宁可晚处理也不误判。"""
        store = {"task_running:t-unknown": json.dumps({"pid": 4242})}
        with harness(task_timeout=300, pid_verdict=None, redis_store=store) as h:
            h.add_task("t-unknown", stale_seconds=600)
            self.assertEqual(h.reap(), 0)
            self.assertEqual(h.row("t-unknown")[0], task_state.RUNNING)

    def test_dead_holder_allows_reap_and_releases_keys(self):
        store = {"task_running:t-dead": json.dumps({"pid": 4242}),
                 "task_snapshot:t-dead": "{}"}
        with harness(task_timeout=300, pid_verdict=False, redis_store=store) as h:
            h.add_task("t-dead", stale_seconds=600)
            self.assertEqual(h.reap(), 1)
            self.assertEqual(h.row("t-dead")[0], task_state.CANCELLED)
            self.assertIn("task_running:t-dead", h.messaging.redis.deleted)
            self.assertIn("task_snapshot:t-dead", h.messaging.redis.deleted)

    def test_terminal_landing_wins_over_reap(self):
        """CAS 让位：同一瞬间任务真的结算了 → 不覆盖终态、不告警。"""
        store = {"task_running:t-race": json.dumps({"pid": 4242})}
        with harness(task_timeout=300, pid_verdict=False, redis_store=store) as h:
            h.add_task("t-race", stale_seconds=600)

            def _finish(*_a, **_k):
                h.set_status("t-race", task_state.SUCCESS)
                return False

            with mock.patch.object(task_state, "pid_same_process",
                                   side_effect=_finish):
                self.assertEqual(h.reap(), 0)
            self.assertEqual(h.row("t-race")[0], task_state.SUCCESS)
            self.assertEqual(h.messaging.alert_types(), [],
                             "真实终态在场时不该报 watchdog 告警")


class TestReapSideEffects(unittest.TestCase):
    """回收必须留下可追溯痕迹：告警 + 审计。"""

    def test_alert_and_audit_are_emitted(self):
        with harness(task_timeout=300) as h:
            h.add_task("t-audit", stale_seconds=600)
            self.assertEqual(h.reap(), 1)
            self.assertEqual(h.messaging.alert_types(), ["task_watchdog_timeout"])
            actions = [args[2] for args, _kw in h.audits]
            self.assertIn("task.watchdog_timeout", actions)
            targets = [kw.get("target") for _args, kw in h.audits]
            self.assertIn("t-audit", targets)

    def test_no_alert_when_nothing_reaped(self):
        with harness(task_timeout=300) as h:
            h.add_task("t-fresh", stale_seconds=5)
            self.assertEqual(h.reap(), 0)
            self.assertEqual(h.messaging.alert_types(), [])
            self.assertEqual(h.audits, [])


class TestHeartbeatWiring(unittest.TestCase):
    """接线：run() 启动回收一轮；之后每次 Guardian 心跳（30s）再回收一轮。"""

    def test_run_reaps_on_startup_and_on_each_heartbeat(self):
        with harness(task_timeout=300) as h:
            guardian = h.guardian
            calls = []

            def _fake_reap():
                calls.append("reap")
                if len(calls) >= 2:
                    guardian._running = False
                return 0

            clock = {"now": 1000.0}

            class _FakeTime:
                @staticmethod
                def time():
                    clock["now"] += 31.0
                    return clock["now"]

                @staticmethod
                def sleep(_seconds):
                    return None

            with mock.patch.object(guardian, "_setup_signal_handlers"), \
                    mock.patch.object(guardian, "_publish_guardian_heartbeat") as hb, \
                    mock.patch.object(guardian, "_health_check"), \
                    mock.patch.object(guardian, "_reap_stuck_tasks",
                                      side_effect=_fake_reap), \
                    mock.patch.object(worker_guardian, "time", _FakeTime):
                guardian.run()

            self.assertEqual(len(calls), 2,
                             "启动一轮 + 首个心跳（30s）一轮")
            self.assertEqual(hb.call_count, 2,
                             "启动与心跳各发一次 Guardian 心跳")


if __name__ == "__main__":
    unittest.main()
