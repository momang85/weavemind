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
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=120)
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
        self.assertIn("收执推进失败", reason,
                      "库不可写必须在**收执裁决点**被拒（P0-a 后不再回落旧路径）")
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


class TestDataRootBoundaries(unittest.TestCase):
    """运行时可写路径要能整体挂到数据根下（审查 P1-3）。

    compose 只挂 `/data`，而任务产物默认落在系统临时目录、分享记录与审计日志默认落在
    应用目录——容器重建后历史、附件、审计一起消失，这几样恰恰是"可核验"的依赖。
    这些路径都是导入期常量，所以用子进程取真实取值。
    """

    _CODE = (
        "import json, workspace, web_ui, notifications, audit_logger, prompt_registry;"
        "print(json.dumps({"
        " 'workspace': str(workspace.WORKSPACE_ROOT),"
        " 'share': web_ui.SHARE_FILE,"
        " 'notify_share': notifications.SHARE_FILE,"
        " 'audit': audit_logger.AUDIT_FILE,"
        " 'prompts': str(prompt_registry._overrides_path())}))"
    )
    _OVERRIDE_VARS = ("WEAVEMIND_DATA_DIR", "WEAVEMIND_WORKSPACE_ROOT", "SHARE_FILE",
                      "AUDIT_FILE", "WEAVEMIND_PROMPTS_DIR")

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_dataroot_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.root = str(tmp / "data")

    def _paths(self, **env):
        base = {k: v for k, v in os.environ.items() if k not in self._OVERRIDE_VARS}
        base.update(env)
        proc = subprocess.run([sys.executable, "-c", self._CODE], cwd=str(ROOT),
                              env=base, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_local_defaults_unchanged(self):
        """本地不设数据根时沿用既有位置——不动用户机器上已有的产物目录。"""
        paths = self._paths()
        self.assertEqual(os.path.dirname(os.path.abspath(paths["share"])), str(ROOT))
        self.assertIn("agent_workspace", paths["workspace"].replace("\\", "/"))
        self.assertTrue(paths["audit"].replace("\\", "/").endswith("logs/audit.jsonl"))
        self.assertIn("prompts", paths["prompts"].replace("\\", "/"))

    def test_all_runtime_state_follows_data_root(self):
        paths = self._paths(WEAVEMIND_DATA_DIR=self.root)
        expected = (
            ("workspace", "workspace"),
            ("share", "share_links.json"),
            ("notify_share", "share_links.json"),
            ("audit", "logs/audit.jsonl"),
            ("prompts", "prompts/overrides.json"),
        )
        for key, rel in expected:
            with self.subTest(key=key):
                got = os.path.abspath(paths[key])
                self.assertTrue(got.startswith(os.path.abspath(self.root) + os.sep),
                                f"{key} 未挂到数据根：{got}")
                self.assertTrue(got.endswith(rel.replace("/", os.sep)), got)
        self.assertEqual(os.path.abspath(paths["share"]),
                         os.path.abspath(paths["notify_share"]),
                         "web_ui 与 notifications 必须读写同一份分享记录")


class TestSubmitIdentityComesFromSession(unittest.TestCase):
    """提交人取会话身份，不信请求体（此前 user_id 由客户端自报，可伪造提交人）。"""

    def test_submit_uses_session_user(self):
        src = (ROOT / "web_ui.py").read_text(encoding="utf-8")
        self.assertNotIn('body.get("user_id")', src,
                         "提交人不得取请求体（可伪造），必须取会话用户")
        self.assertIn('user_id=str(admin.get("user") or "")', src)


class TestSubmitIdempotencyAndTimeline(unittest.TestCase):
    """C3/H3：持久化收执、幂等提交、实例归属与提交时间线。

    反例（原指令 §3.4）：`web_ui` 每次提交都生成新 UUID，编排器也不看身份——
    重复提交（双击/超时重试/重放）会变成**第二次真实执行**；而收执只有一条
    120 秒 TTL 的 Redis 键，事后无法回答"请求到没到、谁收的、卡在哪一段"。
    """

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_idem_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        _mk_db(self.db)
        self.redis = mock.MagicMock()
        self.redis.get.return_value = None
        self.orch = type("O", (), {"_redis": self.redis})()

    def _accept(self, task_id: str, key: str = "", events=None):
        from orchestrator_v2 import accept_task_request
        data = {"task_id": task_id, "goal": "研究目标", "idempotency_key": key,
                "submit_events": events or []}
        with mock.patch.object(task_state, "DB_PATH", self.db):
            ok, reason = accept_task_request(self.orch, data)
        return ok, reason, data

    def test_same_key_creates_exactly_one_task(self):
        """反例：同幂等键提交两次 → 只登记一个任务、不再执行第二次。"""
        ok1, _r1, d1 = self._accept("ui-a1", key="k-1")
        ok2, _r2, d2 = self._accept("ui-a2", key="k-1")
        self.assertTrue(ok1)
        self.assertTrue(ok2, "重复提交不算失败：应回 accepted（去重）")
        self.assertNotIn("_effective_task_id", d1)
        self.assertEqual(d2.get("_effective_task_id"), "ui-a1",
                         "第二次必须指向已有任务，调用方据此跳过执行")
        # 第二次的收执指明去重
        ack2 = str(self.redis.setex.call_args.args[2])
        self.assertTrue(ack2.startswith("accepted:dedup:"), ack2)
        self.assertIn("ui-a1", ack2)
        # 任务库只有一行
        con = sqlite3.connect(self.db)
        try:
            rows = con.execute("SELECT task_id FROM task_history").fetchall()
        finally:
            con.close()
        self.assertEqual([r[0] for r in rows], ["ui-a1"],
                         f"同幂等键不得产生第二个任务：{rows}")

    def test_different_keys_create_two_tasks(self):
        """不同幂等键是两次真实提交（不能把用户主动再跑一次当成重复）。"""
        self._accept("ui-b1", key="k-a")
        self._accept("ui-b2", key="k-b")
        con = sqlite3.connect(self.db)
        try:
            n = con.execute("SELECT COUNT(*) FROM task_history").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(n, 2)

    def test_no_key_keeps_previous_behaviour(self):
        """没有幂等键时行为与之前一致（不去重）——不改变既有调用方语义。"""
        ok1, _r1, d1 = self._accept("ui-c1")
        ok2, _r2, d2 = self._accept("ui-c2")
        self.assertTrue(ok1 and ok2)
        self.assertNotIn("_effective_task_id", d1)
        self.assertNotIn("_effective_task_id", d2)

    def test_timeline_records_five_stages_with_instance(self):
        """时间线要能回答"收到/持久化/发布/消费"各发生在何时、由哪个实例。"""
        events = [{"event": "received", "ts": 1000.0, "instance": "inst-x"},
                  {"event": "published", "ts": 1001.0, "instance": "inst-x"}]
        self._accept("ui-d1", key="k-d", events=events)
        with mock.patch.object(task_state, "DB_PATH", self.db):
            tl = task_state.read_submit_timeline("ui-d1")
        names = [e.get("event") for e in tl]
        for want in ("received", "published", "persisted", "consumed"):
            self.assertIn(want, names, f"时间线缺 {want}：{names}")
        self.assertEqual(tl[0]["ts"], 1000.0, "请求自带时刻要原样保留")
        self.assertTrue(all(e.get("instance") for e in tl), tl)
        con = sqlite3.connect(self.db)
        try:
            owner = con.execute("SELECT accepted_by FROM task_history"
                                " WHERE task_id=?", ("ui-d1",)).fetchone()[0]
        finally:
            con.close()
        self.assertTrue(str(owner or "").strip(), "要记下这条任务由哪个实例实例化")

    def test_receipt_survives_restart_and_is_queryable(self):
        """接收后进程重启：收执与幂等键仍在库里可查（不再只靠会过期的 Redis 键）。"""
        self._accept("ui-e1", key="k-e")
        # 模拟重启：重新解析库路径后再读（收执、幂等键、时间线都要还在）
        with mock.patch.object(task_state, "DB_PATH", self.db):
            found = task_state.find_by_idempotency("k-e")
            row = task_state.read_task("ui-e1")
            timeline = task_state.read_submit_timeline("ui-e1")
        self.assertEqual(str(found.get("task_id")), "ui-e1")
        self.assertEqual(str(row.get("task_id")), "ui-e1")
        self.assertTrue(timeline, "收执时间线必须随任务行持久化，不能只活在 Redis 键里")
        self.assertIn("persisted", [e.get("event") for e in timeline])

    def test_rejected_write_records_no_fake_acceptance(self):
        """库不可写：收执必须是 rejected，且不得留下"已消费"的假时间线。"""
        bad = tempfile.mkdtemp(prefix="wm_idem_bad_")
        self.addCleanup(shutil.rmtree, bad, ignore_errors=True)
        from orchestrator_v2 import accept_task_request
        with mock.patch.object(task_state, "DB_PATH", bad):
            ok, reason = accept_task_request(self.orch,
                                            {"task_id": "ui-f1", "goal": "g",
                                             "idempotency_key": "k-f"})
        self.assertFalse(ok)
        self.assertIn("收执推进失败", reason)
        ack = str(self.redis.setex.call_args.args[2])
        self.assertTrue(ack.startswith("rejected:"), ack)


class TestReceiptRecovery(unittest.TestCase):
    """C3/H3b：先落收执再触发工作；未消费的收执在启动时被捡回来（且只执行一次）。

    反例：编排器没起来（或在崩溃窗口里）时 pub/sub 的消息丢了，旧实现什么都不剩——
    只有一条 120 秒后过期的 Redis 键，用户看到超时却查不到任何东西。
    """

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_recv_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.db = str(tmp / "agents.db")
        _mk_db(self.db)
        self.redis = mock.MagicMock()
        self.redis.get.return_value = None
        self.orch = type("O", (), {"_redis": self.redis})()

    def test_mark_received_writes_received_status_and_timeline(self):
        with mock.patch.object(task_state, "DB_PATH", self.db):
            ok = task_state.mark_received(
                "ui-r1", "研究目标", idempotency_key="k1", instance="inst-a",
                submit_events=[{"event": "published", "ts": 5.0, "instance": "inst-a"}])
            row = task_state.read_task("ui-r1")
            tl = task_state.read_submit_timeline("ui-r1")
        self.assertTrue(ok)
        self.assertEqual(row["status"], "RECEIVED", "收执状态必须是 RECEIVED（待消费）")
        names = [e.get("event") for e in tl]
        self.assertIn("published", names, "请求自带的时刻要保留")
        self.assertIn("received", names)
        self.assertEqual(
            str(task_state.find_by_idempotency("k1", self.db)["task_id"]), "ui-r1")

    def test_promote_is_the_single_execution_right_arbiter(self):
        """同一收执只允许被推进一次——这是"不重复执行"的裁决点。"""
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_received("ui-r2", "目标", idempotency_key="k2")
            first = task_state.promote_received("ui-r2", instance="inst-a")
            second = task_state.promote_received("ui-r2", instance="inst-b")
            row = task_state.read_task("ui-r2")
        self.assertEqual(first, "promoted", "第一次推进者获得执行权")
        self.assertEqual(second, "already", "第二次必须报 already（不得重复执行）")
        self.assertEqual(row["status"], "QUEUED")

    def test_promote_absent_when_no_receipt(self):
        with mock.patch.object(task_state, "DB_PATH", self.db):
            self.assertEqual(task_state.promote_received("ui-none"), "absent")

    def test_promote_db_error_is_error_not_absent(self):
        """数据库异常**不是**"没有收执"。

        此前异常返回 `absent`，调用方据此走旧路径 → 一条正在 RUNNING 的任务会被
        再次启动（内存反例）。异常必须是独立的 `error` 裁决，绝不伪装成缺行。
        """
        with mock.patch.object(task_state, "DB_PATH", self.db), \
                mock.patch.object(task_state, "_connect",
                                  side_effect=RuntimeError("database is locked")):
            verdict = task_state.promote_received("ui-r8", instance="inst-a")
        self.assertEqual(verdict, "error")
        self.assertNotEqual(verdict, "absent", "异常不得伪装成缺行")

    def test_promote_read_error_also_reports_error(self):
        """推进未成功且**读**也失败时同样报 error：读不出来 ≠ 没有收执，不得据此放行。"""
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_received("ui-r8b", "目标")
            task_state.promote_received("ui-r8b")      # → QUEUED
            task_state.mark_running("ui-r8b")          # → RUNNING（已不是 RECEIVED，走读分支）
            with mock.patch.object(task_state, "read_task",
                                   side_effect=RuntimeError("read down")):
                verdict = task_state.promote_received("ui-r8b", instance="inst-a")
        self.assertEqual(verdict, "error")

    def test_promote_already_for_running_task(self):
        """任务已在跑时同一请求再次到达 → `already`（不得再次启动）。"""
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_received("ui-r9", "目标")
            task_state.promote_received("ui-r9")
            task_state.mark_running("ui-r9")
            verdict = task_state.promote_received("ui-r9")
            row = task_state.read_task("ui-r9")
        self.assertEqual(verdict, "already")
        self.assertEqual(row["status"], "RUNNING", "不得把 RUNNING 改回 QUEUED")

    def test_accept_refuses_and_keeps_recovery_state_on_receipt_error(self):
        """推进异常 → 拒绝本次执行、**不置 skip**、收执仍是 RECEIVED（等下次恢复）。

        内存反例：一次连接异常后仍返回 accepted 且无 skip → 允许再次启动任务。
        """
        from orchestrator_v2 import accept_task_request
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_received("ui-r10", "目标", idempotency_key="k10")
            data = {"task_id": "ui-r10", "goal": "目标", "idempotency_key": "k10"}
            with mock.patch.object(task_state, "promote_received", return_value="error"):
                ok, reason = accept_task_request(self.orch, data)
            row = task_state.read_task("ui-r10")
        self.assertFalse(ok, "收执推进异常时不得报 accepted")
        self.assertNotIn("_skip_run", data, "异常路径不得被当成『已消费』")
        self.assertIn("收执推进失败", reason)
        self.assertEqual(row["status"], "RECEIVED", "恢复状态必须保留")

    def test_mark_queued_promotes_receipt_without_pk_conflict(self):
        """收执行已存在时，登记必须**推进**它（旧实现 INSERT 会主键冲突 → 登记失败）。"""
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_received("ui-r3", "目标", idempotency_key="k3")
            ok = task_state.mark_queued("ui-r3", "目标", idempotency_key="k3",
                                        instance="inst-a", db_path=self.db)
            row = task_state.read_task("ui-r3")
        self.assertTrue(ok, "收执已存在时登记不得失败")
        self.assertEqual(row["status"], "QUEUED")
        self.assertEqual(row["accepted_by"], "inst-a")

    def test_mark_queued_never_downgrades_a_running_task(self):
        """已经在跑/已终结的任务不得被"登记"改回 QUEUED。"""
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_received("ui-r4", "目标")
            task_state.promote_received("ui-r4")
            task_state.mark_running("ui-r4")
            task_state.mark_queued("ui-r4", "目标", db_path=self.db)
            row = task_state.read_task("ui-r4")
        self.assertEqual(row["status"], "RUNNING", "不得把 RUNNING 改回 QUEUED")

    def test_list_received_excludes_consumed_and_filters_by_age(self):
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_received("ui-r5", "目标甲")
            task_state.mark_received("ui-r6", "目标乙")
            task_state.promote_received("ui-r6")           # 这条已被消费
            fresh = task_state.list_received(older_than=0)
            stale = task_state.list_received(older_than=600)
        self.assertEqual([r["task_id"] for r in fresh], ["ui-r5"], fresh)
        self.assertEqual([r["task_id"] for r in stale], [],
                         "太新的收执不抢（那条消息自己会推进）")

    def test_accept_promotes_receipt_then_skips_second_time(self):
        """消息路径：推进收执后执行；已被推进过则只回执、不执行第二遍。"""
        from orchestrator_v2 import accept_task_request
        with mock.patch.object(task_state, "DB_PATH", self.db):
            task_state.mark_received("ui-r7", "目标", idempotency_key="k7")
            d1: dict = {"task_id": "ui-r7", "goal": "目标", "idempotency_key": "k7"}
            ok1, _ = accept_task_request(self.orch, d1)
            d2: dict = {"task_id": "ui-r7", "goal": "目标", "idempotency_key": "k7"}
            ok2, _ = accept_task_request(self.orch, d2)
            row = task_state.read_task("ui-r7")
        self.assertTrue(ok1 and ok2)
        self.assertFalse(d1.get("_skip_run"), "第一次要执行")
        self.assertTrue(d2.get("_skip_run"), "第二次必须跳过执行")
        self.assertEqual(row["status"], "QUEUED")

    def test_resume_received_runs_stale_receipts_once(self):
        """启动恢复：把"收执已落库但从未消费"的任务捡回来执行（只一次）。"""
        import orchestrator_v2
        calls: list = []

        class _FakeThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None):
                calls.append({"target": target, "args": args, "kwargs": kwargs or {}})

            def start(self):
                pass

        with mock.patch.object(task_state, "DB_PATH", self.db), \
                mock.patch.object(orchestrator_v2.threading, "Thread", _FakeThread), \
                mock.patch.object(orchestrator_v2, "_instance_identity",
                                  lambda: ("inst-test", "ver-test")):
            # `_instance_identity` 也要打桩：它会去读 `.git`（`code_version`），
            # 而 subprocess 内部用 `threading.Thread` 读管道——上面把 Thread 换成替身
            # 后，git 的读取线程也会被记进来，断言"只起了一个执行线程"就会误判。
            task_state.mark_received("ui-r8", "目标", user="u1", project="p1")
            # 阈值 600 秒：刚落的收执不算"陈旧"，避免抢正在飞的那条消息
            n0 = orchestrator_v2.resume_received_tasks(self.orch, older_than=600)
            n1 = orchestrator_v2.resume_received_tasks(self.orch, older_than=0)
            n2 = orchestrator_v2.resume_received_tasks(self.orch, older_than=0)
            row = task_state.read_task("ui-r8")
        self.assertEqual(n0, 0, "太新的收执不该被恢复")
        self.assertEqual(n1, 1, "未消费的收执要被捡起来")
        self.assertEqual(n2, 0, "恢复过一次后不得重复执行")
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["args"][1], "ui-r8")
        self.assertEqual(row["status"], "QUEUED")


if __name__ == "__main__":
    unittest.main()
