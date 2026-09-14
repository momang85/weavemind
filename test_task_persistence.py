# -*- coding: utf-8 -*-
"""持久化诚实性与任务库路径统一（架构审查 P1-2）。

审查结论两条，都在这里钉死：

1. **落库失败仍回 accepted**：`mark_queued` 内部 `except: pass` 并正常返回，
   于是 `accept_task_request` 里"靠异常区分 accepted / rejected"的分支永不触发——
   任务库不可写时提交方仍收到 accepted，而任务在现实里从未存在。
2. **任务库路径不统一**：web_ui / checkpointer / workers 读 `REGISTRY_DB`（默认相对
   CWD），task_state / metrics_collector 读 `AGENTS_DB`（默认模块目录）。Dockerfile
   只设 `REGISTRY_DB=/data/agents.db`，编排器因此把状态写进 `/app/agents.db`
   （表不存在、且不在挂载卷里），web_ui 在 `/data/agents.db` 建表。

失败注入用**真实 SQLite 故障**（把库路径指向一个同名目录 → OperationalError），
不用 mock 造异常——mock 恰恰会让"内部吞异常"这类缺陷继续隐形。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import task_state

ROOT = Path(__file__).resolve().parent
DB_VARS = ("WEAVEMIND_DB", "REGISTRY_DB", "AGENTS_DB")
# 必须共用同一任务库的模块（其余 worker/守护进程在下方用全仓扫描覆盖）
STATE_MODULES = ("task_state.py", "web_ui.py", "checkpointer.py",
                 "metrics_collector.py", "orchestrator_v2.py")


def _clean_env(**overrides):
    env = {k: v for k, v in os.environ.items() if k not in DB_VARS}
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


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


class TestDbPathResolution(unittest.TestCase):
    """解析口径：优先级、绝对路径、默认值。"""

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_dbp_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp

    def test_default_is_absolute_module_dir(self):
        import db_paths
        with _clean_env():
            got = db_paths.resolve_db_path()
        self.assertTrue(os.path.isabs(got), "默认路径必须是绝对路径（不得依赖 CWD）")
        self.assertEqual(got, db_paths.default_db_path())
        self.assertTrue(got.endswith("agents.db"))

    def test_precedence(self):
        import db_paths
        a = str(self.tmp / "weave.db")
        b = str(self.tmp / "registry.db")
        c = str(self.tmp / "agents_legacy.db")
        cases = [
            ({"WEAVEMIND_DB": a}, a),
            ({"REGISTRY_DB": b}, b),
            ({"AGENTS_DB": c}, c),
            ({"WEAVEMIND_DB": a, "REGISTRY_DB": b}, a),
            ({"REGISTRY_DB": b, "AGENTS_DB": c}, b),
            ({"WEAVEMIND_DB": a, "REGISTRY_DB": b, "AGENTS_DB": c}, a),
        ]
        for env, expected in cases:
            with self.subTest(env=sorted(env)):
                with _clean_env(**env):
                    self.assertEqual(db_paths.resolve_db_path(), os.path.abspath(expected))

    def test_relative_env_becomes_absolute(self):
        import db_paths
        with _clean_env(WEAVEMIND_DB=os.path.join("sub", "agents.db")):
            got = db_paths.resolve_db_path()
        self.assertTrue(os.path.isabs(got))
        self.assertTrue(got.endswith(os.path.join("sub", "agents.db")))

    def test_state_writer_reads_the_same_file_in_fresh_process(self):
        """新进程里 task_state.DB_PATH 必须等于统一解析结果（导入期取值也一致）。"""
        code = ("import json, os, db_paths, task_state;"
                "print(json.dumps([task_state.DB_PATH, db_paths.resolve_db_path()]))")
        combos = [
            {},
            {"WEAVEMIND_DB": str(self.tmp / "w.db")},
            {"REGISTRY_DB": str(self.tmp / "r.db")},
            {"AGENTS_DB": str(self.tmp / "a.db")},
            {"REGISTRY_DB": str(self.tmp / "r.db"), "AGENTS_DB": str(self.tmp / "a.db")},
        ]
        for env in combos:
            with self.subTest(env=sorted(env)):
                full = {k: v for k, v in os.environ.items() if k not in DB_VARS}
                full.update(env)
                proc = subprocess.run(
                    [sys.executable, "-c", code], cwd=str(ROOT), env=full,
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
                module_path, resolved = json.loads(proc.stdout.strip().splitlines()[-1])
                self.assertEqual(module_path, resolved,
                                 "task_state 必须用统一解析结果，不得自己读环境变量")
                self.assertTrue(os.path.isabs(module_path))


class TestAllDbReadersUseResolver(unittest.TestCase):
    """全仓静态约束：不得再出现各自读 `REGISTRY_DB`/`AGENTS_DB` 的分叉入口。"""

    def test_no_direct_env_reads(self):
        pattern = re.compile(r"os\.environ\.get\(\s*[\"'](REGISTRY_DB|AGENTS_DB)[\"']")
        offenders = []
        candidates = list(ROOT.glob("*.py")) + list((ROOT / "workers").glob("*.py"))
        for path in candidates:
            if path.name.startswith("test_") or path.name == "db_paths.py":
                continue
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(path.name)
        self.assertEqual(
            sorted(offenders), [],
            f"这些文件仍在各自读任务库环境变量，必须改用 db_paths.resolve_db_path()：{offenders}")

    def test_state_modules_reference_resolver(self):
        for name in STATE_MODULES:
            src = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("db_paths.resolve_db_path()", src,
                          f"{name} 必须用统一解析入口")

    def test_web_ui_and_task_state_agree(self):
        import db_paths
        import web_ui
        self.assertTrue(os.path.isabs(web_ui.DB_PATH))
        self.assertEqual(web_ui.DB_PATH, task_state.DB_PATH,
                         "web 历史表与状态写者必须指向同一个库")
        self.assertEqual(web_ui.DB_PATH, db_paths.resolve_db_path())


class TestLauncherInjectsSinglePath(unittest.TestCase):
    """启动器把唯一的库路径注入所有子进程（编排器/worker/web/守护共用一份环境）。"""

    def test_apply_env_sets_one_absolute_db(self):
        import launcher
        with _clean_env():
            launcher._apply_env({})
            vals = {v: os.environ.get(v) for v in DB_VARS}
        self.assertTrue(all(vals.values()), f"三个变量都要有值：{vals}")
        self.assertEqual(len(set(vals.values())), 1,
                         f"子进程看到的库路径必须唯一：{vals}")
        self.assertTrue(os.path.isabs(vals["WEAVEMIND_DB"]))

    def test_apply_env_keeps_existing_config(self):
        """用户已显式指定时不得被改写（只补不覆盖）。"""
        import launcher
        target = os.path.join(tempfile.gettempdir(), "wm_keep.db")
        with _clean_env(REGISTRY_DB=target):
            launcher._apply_env({})
            self.assertEqual(os.path.abspath(os.environ["REGISTRY_DB"]),
                             os.path.abspath(target))
            self.assertEqual(os.path.abspath(os.environ["WEAVEMIND_DB"]),
                             os.path.abspath(target))


class _BadDbMixin:
    """把库路径指向一个目录：SQLite 打开必然失败（真实故障，非 mock）。"""

    def setUp(self):
        super().setUp()
        self.bad_db = tempfile.mkdtemp(prefix="wm_bad_db_")
        self.addCleanup(shutil.rmtree, self.bad_db, ignore_errors=True)


class TestRegistrationFailureIsHonest(_BadDbMixin, unittest.TestCase):
    def test_mark_queued_returns_false_on_real_failure(self):
        self.assertIs(task_state.mark_queued("ui-x", "目标", db_path=self.bad_db), False,
                      "落库失败必须如实返回 False（此前静默返回 None）")

    def test_accept_writes_rejected_ack_on_real_failure(self):
        from orchestrator_v2 import accept_task_request
        redis = mock.MagicMock()
        orch = type("O", (), {"_redis": redis})()
        with mock.patch.object(task_state, "DB_PATH", self.bad_db):
            ok, reason = accept_task_request(orch, {"task_id": "ui-x", "goal": "g"})
        self.assertFalse(ok, "任务库不可写时不得回 accepted（否则界面显示成功、现实里无任务）")
        self.assertIn("登记失败", reason)
        ack = str(redis.setex.call_args.args[2])
        self.assertTrue(ack.startswith("rejected:"), f"收执必须是 rejected：{ack}")

    def test_accept_writes_rejected_when_mark_raises(self):
        from orchestrator_v2 import accept_task_request
        redis = mock.MagicMock()
        orch = type("O", (), {"_redis": redis})()
        with mock.patch.object(task_state, "mark_queued",
                               side_effect=RuntimeError("db locked")):
            ok, reason = accept_task_request(orch, {"task_id": "ui-y", "goal": "g"})
        self.assertFalse(ok)
        self.assertIn("登记失败", reason)


class TestFinalizeNeedsRealWrite(_BadDbMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        tmp = Path(tempfile.mkdtemp(prefix="wm_persist_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        _mk_db(self.db)

    def test_record_completion_returns_false_on_real_failure(self):
        self.assertIs(
            task_state.record_completion("ui-x", goal="g", status="SUCCESS",
                                         report="正文", db_path=self.bad_db),
            False)

    def test_finalize_keeps_task_unfinalized_on_failure(self):
        from orchestrator_v2 import OrchestratorV2
        orch = OrchestratorV2.__new__(OrchestratorV2)
        with mock.patch.object(task_state, "DB_PATH", self.bad_db), \
                mock.patch.object(task_state, "mark_persist_failed") as mark:
            orch._finalize_task("ui-x", "目标", "SUCCESS", report="正文")
        self.assertNotIn("ui-x", getattr(orch, "_finalized_tasks", set()),
                         "落库失败不得标记为已终结——否则库里永远 RUNNING 且不再重试")
        mark.assert_called_once()

    def test_finalize_marks_done_on_success(self):
        from orchestrator_v2 import OrchestratorV2
        orch = OrchestratorV2.__new__(OrchestratorV2)
        with mock.patch.object(task_state, "DB_PATH", self.db):
            orch._finalize_task("ui-ok", "目标", "SUCCESS_WITH_ISSUES",
                                report="正文", acceptance={"overall": "fail"})
            self.assertIn("ui-ok", orch._finalized_tasks)
            row = task_state.read_task("ui-ok", self.db)
        self.assertEqual(row["status"], "SUCCESS_WITH_ISSUES")
        self.assertEqual(row["report"], "正文")


class TestSubmitIdentityComesFromSession(unittest.TestCase):
    """提交人取会话身份，不信请求体（此前 user_id 由客户端自报，可伪造提交人）。"""

    def test_submit_uses_session_user(self):
        src = (ROOT / "web_ui.py").read_text(encoding="utf-8")
        self.assertNotIn('body.get("user_id")', src,
                         "提交人不得取请求体（可伪造），必须取会话用户")
        self.assertIn('user_id=str(admin.get("user") or "")', src)


if __name__ == "__main__":
    unittest.main()
