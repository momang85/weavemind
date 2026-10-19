# -*- coding: utf-8 -*-
"""任务26（🟡 成本 / 🟢 卫生）验收：步骤级断点续跑 ＋ 死链持久化 ＋ 恢复入口。

根因：`task_state.steps_json` **早就**逐步落库了（终态 `record_completion` 写、
运行中 Redis 快照写），但 `_execute_steps` 从不读回——编排器重启/人工恢复后把
全部步骤重跑一遍（实机 `ui-1c42195dae`：计划 6 步、1133 秒全步重跑，已成功的
抓取与计算白花钱，重跑还可能产出与首轮不一致的正文）。四处收口：
  ① `_execute_steps(resume_completed=True)` 入口读回 `SUCCESS` 步骤（DB 权威、
     快照补"跑到一半被杀"的窗口），只认**当前计划**里的 step_id；
  ② 派发循环跳过这些步骤并留审计痕迹（`step_resumed` 进度事件）；
  ③ `_dead_fetch_urls` 持久化到 Redis `dead_fetch:{task_id}`，编排器启动/续跑
     时恢复——续跑任务不再重抓已判死 URL（任务24 的报告标注也依赖它）；
  ④ 入口 `POST /api/task/<id>/resume`：FAILED/CRASHED → RUNNING（`mark_resumed`
     的 CAS 保证只有一个恢复赢家）＋ 发 `resume_task` 消息请编排器重新接管。
     分流原因：终态任务的收执早被 `promote_received` 消费，走普通提交会被判
     "already" 而跳过。
账目：AGENTS.md 的 test_*.py 计数 + 本次两个 evals 用例（ag-047/ag-049）。
"""
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import orchestrator_v2 as ov  # noqa: E402
import task_state as ts  # noqa: E402
from test_orchestrator_v2 import FakeMessaging, make_orch  # noqa: E402

# 真实序列化形状（`_finalize_task` 落 steps_json 的结构）：状态在 **result 内**
_STEP1 = {"step_id": "1", "capability": "web_search",
          "instruction": "搜索年报",
          "result": {"task_id": "1", "status": "SUCCESS",
                     "result": "[{\"title\":\"2024年报\",\"url\":\"https://e.com/a\"}]"}}
_STEP2 = {"step_id": "2", "capability": "content_analysis",
          "instruction": "分析两期",
          "result": {"task_id": "2", "status": "SUCCESS", "result": "营收增长 8%"}}
_STEP3_FAILED = {"step_id": "3", "capability": "report_generator",
                 "instruction": "写报告",
                 "result": {"task_id": "3", "status": "FAILED", "result": "超时"}}


def _plan():
    """当前计划（3 步）——与落库步骤同 step_id，续跑判定的前提。"""
    return [
        {"step_id": "1", "capability": "web_search", "instruction": "搜索年报",
         "depends_on": [], "timeout": 30},
        {"step_id": "2", "capability": "content_analysis", "instruction": "分析两期",
         "depends_on": ["1"], "timeout": 30},
        {"step_id": "3", "capability": "report_generator", "instruction": "写报告",
         "depends_on": ["2"], "timeout": 30},
    ]


class _FakeRedis:
    """只实现本任务用到的五个方法；值存 dict，可断言持久化内容。"""

    def __init__(self, store=None):
        self.store = store if store is not None else {}
        self.setex_calls = []
        self.deleted = []
        self.published = []

    def setex(self, key, ttl, value):
        self.setex_calls.append((key, ttl, value))
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        self.deleted.extend(keys)
        return n

    def publish(self, channel, message):
        self.published.append((channel, message))
        return 1

    def scan_iter(self, match=None, count=None):
        prefix = str(match or "").rstrip("*")
        return iter([k for k in sorted(self.store) if k.startswith(prefix)])


class TestLoadResumableSteps(unittest.TestCase):
    """① 读回已持久化 SUCCESS 步骤（两个通道 + 只认当前计划）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_t26_db_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = str(self.tmp / "agents.db")
        self._p_db = mock.patch.object(ts, "DB_PATH", self.db)
        self._p_db.start()
        self.addCleanup(self._p_db.stop)
        self._p_snap = mock.patch.object(ts, "read_snapshot", return_value={})
        self._p_snap.start()
        self.addCleanup(self._p_snap.stop)
        self.o = make_orch()

    def _persist(self, steps, status=ts.FAILED, task_id="T1"):
        ts.mark_queued(task_id, "研究某公司", db_path=self.db)
        self.assertTrue(ts.record_completion(task_id, goal="研究某公司",
                                             status=status,
                                             steps=steps, db_path=self.db))

    def test_reads_success_steps_from_db(self):
        self._persist([_STEP1, _STEP2, _STEP3_FAILED])
        got = self.o._load_resumable_steps("T1", _plan())
        self.assertEqual(sorted(got), ["1", "2"],
                         f"两个 SUCCESS 步骤应可复用：{sorted(got)}")
        self.assertNotIn("3", got, "FAILED 步骤必须重跑")
        self.assertEqual(got["1"]["status"], "SUCCESS")
        self.assertTrue(got["1"]["resumed"], "复用结果要标记 resumed（审计可对账）")
        self.assertIn("2024年报", got["1"]["result"], "结果文本要能注入下游指令")

    def test_tolerates_top_level_status_shape(self):
        """兼容另一种形状（status 在条目顶层）：两条通道都不挑格式。"""
        entry = dict(_STEP1, status="SUCCESS")
        self._persist([entry])
        got = self.o._load_resumable_steps("T1", _plan())
        self.assertEqual(sorted(got), ["1"])

    def test_snapshot_channel_covers_killed_midrun(self):
        """DB 还没落终态（跑到一半被杀）→ Redis 快照通道补上已完成步骤。"""
        ts.mark_queued("T2", "研究某公司", db_path=self.db)
        with mock.patch.object(ts, "read_snapshot", return_value={
                "steps": [dict(_STEP1), dict(_STEP2)]}):
            got = self.o._load_resumable_steps("T2", _plan())
        self.assertEqual(sorted(got), ["1", "2"],
                         "运行中快照里的 SUCCESS 步骤同样可复用")

    def test_only_current_plan_step_ids(self):
        """计划变了（反思重做/修复轮）→ 旧 step_id 不得沿用为"已完成"。"""
        self._persist([_STEP1, _STEP2, _STEP3_FAILED])
        plan = _plan()
        plan[0]["step_id"] = "1b"      # 首轮步骤 1 被重排为 1b
        got = self.o._load_resumable_steps("T1", plan)
        self.assertNotIn("1", got, "当前计划里没有的 step_id 不得复用")
        self.assertEqual(sorted(got), ["2"])

    def test_empty_db_row_returns_empty(self):
        """库里没有这个任务（或 steps_json 为空）→ 全量执行（prescribed case 2）。"""
        self.assertEqual(self.o._load_resumable_steps("nope", _plan()), {})

    def test_corrupt_steps_json_is_tolerated(self):
        ts.mark_queued("T3", "研究某公司", db_path=self.db)
        import sqlite3
        con = sqlite3.connect(self.db)
        con.execute("UPDATE task_history SET steps_json='{{{' WHERE task_id='T3'")
        con.commit()
        con.close()
        self.assertEqual(self.o._load_resumable_steps("T3", _plan()), {},
                         "坏 JSON 按'没有已完成步骤'处理，绝不抛")


class TestExecuteStepsSkipsCompleted(unittest.TestCase):
    """② 派发循环：resume 时跳过已成功步骤，只跑未完步骤。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_t26_exec_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = str(self.tmp / "agents.db")
        for name, val in (("DB_PATH", self.db),):
            p = mock.patch.object(ts, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ts, "read_snapshot", return_value={})
        p.start()
        self.addCleanup(p.stop)
        # 工作区指向临时根：报告步骤会写 dead_sources.json（任务24 联动）
        import workspace as ws_mod
        self._old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp / "ws"))
        self.addCleanup(lambda: setattr(ws_mod, "WORKSPACE_ROOT", self._old_root))

    def _orch_with_persisted(self, steps, task_id="T1", status=ts.FAILED):
        ts.mark_queued(task_id, "研究某公司", db_path=self.db)
        ts.record_completion(task_id, goal="研究某公司", status=status,
                             steps=steps, db_path=self.db)
        o = make_orch()
        o._task_goals = {task_id: "研究某公司"}
        o._task_prompt_hints = {}
        o._task_user_ids = {}
        o._dead_fetch_urls = {}
        o._dead_fetch_reasons = {}
        dispatched = []

        def _safe(_goal, step, _tid, _state):
            dispatched.append(str(step["step_id"]))
            return {"task_id": step["step_id"], "status": "SUCCESS",
                    "result": f"fresh-{step['step_id']}"}

        o._dispatch_step_safe = _safe
        with mock.patch.object(ov, "_NO_REDIS_RETRY", None), \
                mock.patch.object(ov.OrchestratorV2, "_dead_fetch_redis",
                                  side_effect=RuntimeError("离线")):
            return o, dispatched

    def test_resume_dispatches_only_remaining_steps(self):
        """prescribed case 1：3 步计划、1/2 已 SUCCESS → 只派发第 3 步。"""
        o, dispatched = self._orch_with_persisted([_STEP1, _STEP2, _STEP3_FAILED])
        results, has_failure = o._execute_steps(_plan(), "T1", "研究某公司",
                                                resume_completed=True)
        self.assertEqual(sorted(dispatched), ["3"],
                         f"已成功的步骤不得再派发（成本红线）：{dispatched}")
        self.assertEqual(len(results), 3, "结果列表仍要覆盖全部步骤（顺序交付用）")
        by_id = {str(r["task_id"]): r for r in results}
        self.assertEqual(by_id["1"]["status"], "SUCCESS")
        self.assertTrue(by_id["1"].get("resumed"), "复用步骤标 resumed")
        self.assertEqual(by_id["3"]["result"], "fresh-3", "第 3 步是本轮新跑的")
        self.assertFalse(has_failure, "复用步骤不得被判成失败")

    def test_resume_emits_step_resumed_audit(self):
        o, _ = self._orch_with_persisted([_STEP1, _STEP2, _STEP3_FAILED])
        o._execute_steps(_plan(), "T1", "研究某公司", resume_completed=True)
        msgs = [m for _c, m in o._messaging.published]
        self.assertTrue(any("step_resumed" in str(m) for m in msgs),
                        "跳过已完成步骤必须留审计痕迹（step_resumed）")

    def test_fresh_run_dispatches_everything(self):
        """prescribed case 2：不传 resume（新任务）→ 全部步骤照常派发。"""
        o, dispatched = self._orch_with_persisted([_STEP1, _STEP2, _STEP3_FAILED])
        o._execute_steps(_plan(), "T1", "研究某公司")
        self.assertEqual(sorted(dispatched), ["1", "2", "3"],
                         "非续跑路径行为不得改变（新任务全量执行）")

    def test_empty_checkpoint_dispatches_everything(self):
        """库里没有已完成步骤（steps_json 为空）→ 三步全跑。"""
        o, dispatched = self._orch_with_persisted([], task_id="T0")
        o._execute_steps(_plan(), "T0", "研究某公司", resume_completed=True)
        self.assertEqual(sorted(dispatched), ["1", "2", "3"])

    def test_dependency_on_resumed_step_is_satisfied(self):
        """复用步骤进入 completed → 下游依赖判定成立，不被"Blocked by"连锁拒绝。"""
        o, dispatched = self._orch_with_persisted([_STEP1])
        results, _ = o._execute_steps(_plan(), "T1", "研究某公司",
                                      resume_completed=True)
        by_id = {str(r["task_id"]): r for r in results}
        self.assertNotIn("Blocked by", str(by_id["2"].get("result") or ""))
        self.assertIn("2", dispatched)


class TestDeadFetchPersistence(unittest.TestCase):
    """③ 死链清单持久化到 Redis `dead_fetch:{task_id}` 并可恢复。"""

    def _orch(self, store):
        o = make_orch()
        o._dead_fetch_urls = {}
        o._dead_fetch_reasons = {}
        fake = _FakeRedis(store)
        o._dead_fetch_redis = lambda: fake
        return o, fake

    def test_mark_persists_to_redis(self):
        """prescribed case 3 的前半：记账即落 Redis（JSON）。"""
        o, fake = self._orch({})
        o._mark_dead_fetch_url("T1", "https://a.com/dead.pdf", "HTTP 404")
        key = f"{ov.OrchestratorV2.DEAD_FETCH_KEY_PREFIX}T1"
        self.assertIn(key, fake.store, "死链必须持久化，进程内集合不是唯一通道")
        data = json.loads(fake.store[key])
        self.assertEqual(data[0]["url"], "https://a.com/dead.pdf")
        self.assertEqual(data[0]["reason"], "HTTP 404")

    def test_restore_into_fresh_orchestrator(self):
        """prescribed case 3：`dead_fetch:T1` 有 url1 → 新实例读得到。"""
        store = {}
        writer, _ = self._orch(store)
        writer._mark_dead_fetch_url("T1", "https://a.com/dead.pdf")
        reader, _ = self._orch(store)          # 模拟重启后的新进程
        urls = reader._get_dead_fetch_urls("T1")
        self.assertIn("https://a.com/dead.pdf", urls)
        self.assertEqual(reader._restore_dead_fetch("T1"), 1)
        self.assertIn("https://a.com/dead.pdf", reader._dead_urls_of("T1"),
                      "恢复要落到内存集合（门禁/报告标注共用）")

    def test_restore_all_scans_prefix(self):
        store = {}
        writer, _ = self._orch(store)
        writer._mark_dead_fetch_url("T1", "https://a.com/dead1")
        writer._mark_dead_fetch_url("T2", "https://a.com/dead2")
        reader, _ = self._orch(store)
        self.assertEqual(reader.restore_dead_fetch_all(), 2, "启动恢复两个任务")
        self.assertTrue(reader._is_dead_fetch_url("T1", "https://a.com/dead1"))
        self.assertTrue(reader._is_dead_fetch_url("T2", "https://a.com/dead2"))

    def test_restore_merges_not_overwrites(self):
        store = {"dead_fetch:T1": json.dumps(
            [{"url": "https://a.com/persisted", "reason": "HTTP 404"}])}
        reader, _ = self._orch(store)
        reader._dead_urls_of("T1").add("https://a.com/newly-dead")
        self.assertEqual(reader._restore_dead_fetch("T1"), 1)
        self.assertEqual(reader._dead_urls_of("T1"),
                         {"https://a.com/persisted", "https://a.com/newly-dead"},
                         "内存里本轮新判死的 URL 不得被覆盖掉")

    def test_terminal_clears_persisted_list(self):
        """终态清理：不留陈旧清单给下次全新运行恢复出假死链。"""
        store = {}
        o, fake = self._orch(store)
        o._mark_dead_fetch_url("T1", "https://a.com/dead")
        self.assertIn("dead_fetch:T1", store)
        o._delete_dead_fetch("T1")
        self.assertNotIn("dead_fetch:T1", store)
        self.assertEqual(o._get_dead_fetch_urls("T1"), set())

    def test_offline_redis_degrades_silently(self):
        """Redis 不可用：记账/恢复都不许抛（辅助持久化绝不挡主流程）。"""
        o = make_orch()
        o._dead_fetch_urls = {}
        o._dead_fetch_reasons = {}

        def _boom():
            raise RuntimeError("redis down")

        o._dead_fetch_redis = _boom
        self.assertTrue(o._mark_dead_fetch_url("T1", "https://a.com/dead"),
                        "内存记账仍要成功")
        self.assertEqual(o._get_dead_fetch_urls("T1"), set())
        self.assertEqual(o._restore_dead_fetch("T1"), 0)
        self.assertEqual(o.restore_dead_fetch_all(), 0)

    def test_bad_payload_is_tolerated(self):
        o, _fake = self._orch({"dead_fetch:T1": "{not json"})
        self.assertEqual(o._get_dead_fetch_urls("T1"), set())
        self.assertEqual(o._restore_dead_fetch("T1"), 0)

    def test_bare_string_list_still_restores(self):
        """兼容早期/外部写入的裸 URL 数组（不挑格式）。"""
        o, _fake = self._orch(
            {"dead_fetch:T1": json.dumps(["https://a.com/dead-x"])})
        self.assertEqual(o._get_dead_fetch_urls("T1"), {"https://a.com/dead-x"})


class TestResumeTakeoverGate(unittest.TestCase):
    """④ 编排器侧接管裁决：状态口径与"仍在运行"判据（从消息循环抽出以便单测）。"""

    def _orch(self, running=False):
        o = make_orch()
        o._task_is_running = lambda tid: running
        return o

    def test_running_status_is_allowed(self):
        """入口已先把 FAILED 原子翻成 RUNNING → 这里必须放行。

        首轮实现要求 DB 状态等于 FAILED，于是每一次合法恢复都被编排器自己
        拒掉（任务永远停在 RUNNING，且用户看不到拒绝原因）。
        """
        ok, why = ov.resume_takeover_allowed(self._orch(), "T1", {"status": "RUNNING"})
        self.assertTrue(ok, why)

    def test_failed_and_crashed_allowed(self):
        for status in (ts.FAILED, "CRASHED"):
            ok, why = ov.resume_takeover_allowed(self._orch(), "T1", {"status": status})
            self.assertTrue(ok, f"{status}: {why}")

    def test_irreversible_terminals_refused(self):
        for status in (ts.SUCCESS, ts.SUCCESS_WITH_ISSUES, ts.CANCELLED):
            ok, why = ov.resume_takeover_allowed(self._orch(), "T1", {"status": status})
            self.assertFalse(ok, f"{status} 不得被恢复改写")
            self.assertIn(status, why)

    def test_live_task_refused(self):
        ok, why = ov.resume_takeover_allowed(self._orch(running=True), "T1",
                                             {"status": "RUNNING"})
        self.assertFalse(ok)
        self.assertIn("运行中", why)

    def test_missing_row_refused(self):
        self.assertFalse(ov.resume_takeover_allowed(self._orch(), "T1", {})[0])
        self.assertFalse(ov.resume_takeover_allowed(self._orch(), "T1", None)[0])


class TestResumeApi(unittest.TestCase):
    """④ `POST /api/task/<id>/resume`：FAILED → RUNNING ＋ 请编排器重新接管。"""

    class _Fake:
        def __init__(self, path=""):
            self.path = path

        def _json(self, payload, code=200, **kw):
            return (code, payload)

        def _client_ip(self):
            return "127.0.0.1"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_t26_api_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = str(self.tmp / "agents.db")
        p = mock.patch.object(ts, "DB_PATH", self.db)
        p.start()
        self.addCleanup(p.stop)

    def _seed(self, status, task_id="ui-resume", goal="研究某公司"):
        ts.mark_queued(task_id, goal, db_path=self.db)
        ts.record_completion(task_id, goal=goal, status=status, db_path=self.db)

    def _call(self, path="/api/task/ui-resume/resume", *, exists=True,
              redis_ready=True):
        import web_ui
        fake = _FakeRedis({})
        with mock.patch.object(web_ui, "_task_exists", return_value=exists), \
                mock.patch.object(web_ui, "_redis_ready", return_value=redis_ready), \
                mock.patch.object(web_ui, "_new_redis", return_value=fake), \
                mock.patch.object(web_ui, "audit_log") as audit:
            res = web_ui._post_task_resume(self._Fake(path), path, {},
                                           {"user": "u1"})
        return res, fake, audit

    def test_failed_task_flips_to_running_and_publishes(self):
        """prescribed case 4：FAILED → RUNNING，并发出 resume_task 消息。"""
        self._seed(ts.FAILED)
        res, fake, audit = self._call()
        self.assertEqual(res[0], 200, res[1])
        self.assertEqual(res[1]["status"], "ok")
        row = ts.read_task("ui-resume", db_path=self.db)
        self.assertEqual(row["status"], ts.RUNNING,
                         f"状态必须翻回 RUNNING：{row['status']}")
        self.assertIsNone(row.get("completed_at"), "恢复要清掉终态时间戳")
        chan, payload = fake.published[-1][0], fake.published[-1][1]
        self.assertEqual(chan, "orchestrator:main")
        data = json.loads(payload)
        self.assertEqual(data["type"], "resume_task")
        self.assertEqual(data["task_id"], "ui-resume")
        self.assertEqual(data["goal"], "研究某公司", "目标要带上，免二次查询")
        audit.assert_called_once()

    def test_success_is_409_and_untouched(self):
        """不可逆终态：409，且库里状态一字不改（复核红线）。"""
        self._seed(ts.SUCCESS, task_id="ui-ok")
        res, fake, _ = self._call("/api/task/ui-ok/resume")
        self.assertEqual(res[0], 409)
        self.assertEqual(ts.read_task("ui-ok", db_path=self.db)["status"],
                         ts.SUCCESS)
        self.assertEqual(fake.published, [], "被拒绝的恢复不得通知编排器")
        for st in (ts.SUCCESS_WITH_ISSUES, ts.CANCELLED):
            self._seed(st, task_id="ui-x")
            res, _f, _a = self._call("/api/task/ui-x/resume")
            self.assertEqual(res[0], 409, st)

    def test_crashed_task_is_resumable(self):
        self._seed(ts.FAILED, task_id="ui-cr")
        import sqlite3
        con = sqlite3.connect(self.db)
        con.execute("UPDATE task_history SET status='CRASHED' WHERE task_id='ui-cr'")
        con.commit()
        con.close()
        res, _fake, _ = self._call("/api/task/ui-cr/resume")
        self.assertEqual(res[0], 200, res[1])
        self.assertEqual(ts.read_task("ui-cr", db_path=self.db)["status"],
                         ts.RUNNING)

    def test_double_resume_second_is_409(self):
        """两个恢复入口竞争：CAS 只让一个赢家重新接管，第二个 409。"""
        self._seed(ts.FAILED, task_id="ui-twice")
        first, _f1, _a1 = self._call("/api/task/ui-twice/resume")
        second, f2, _a2 = self._call("/api/task/ui-twice/resume")
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 409, "第二次不得重复接管（否则跑两遍）")
        self.assertEqual(f2.published, [])

    def test_unknown_task_404_and_empty_id_400(self):
        res, _, _ = self._call("/api/task/ui-nope/resume", exists=False)
        self.assertEqual(res[0], 404)
        res, _, _ = self._call("/api/task//resume")
        self.assertEqual(res[0], 400)

    def test_no_redis_503_and_status_untouched(self):
        self._seed(ts.FAILED, task_id="ui-nr")
        res, _, _ = self._call("/api/task/ui-nr/resume", redis_ready=False)
        self.assertEqual(res[0], 503)
        self.assertEqual(ts.read_task("ui-nr", db_path=self.db)["status"],
                         ts.FAILED, "Redis 不在时不得先把状态改掉")

    def test_publish_failure_does_not_leave_running(self):
        """翻成功但消息发不出去 → 报 503（状态由看护线程按 stale RUNNING 收口）。"""
        import web_ui
        self._seed(ts.FAILED, task_id="ui-pf")

        class _Boom(_FakeRedis):
            def publish(self, *a, **k):
                raise RuntimeError("redis down")

        with mock.patch.object(web_ui, "_task_exists", return_value=True), \
                mock.patch.object(web_ui, "_redis_ready", return_value=True), \
                mock.patch.object(web_ui, "_new_redis",
                                  return_value=_Boom({})), \
                mock.patch.object(web_ui, "audit_log"):
            res = web_ui._post_task_resume(self._Fake(), "/api/task/ui-pf/resume",
                                           {}, {"user": "u"})
        self.assertEqual(res[0], 503)

    def test_non_resume_path_returns_none(self):
        import web_ui
        self.assertIsNone(web_ui._post_task_resume(
            self._Fake(), "/api/task/ui-1/cancel", {}, {"user": "u"}))

    def test_route_registered_and_message_branch(self):
        """接线守卫：路由表注册 + 编排器 `resume_task` 分支 + 走 gate（非旧内联判断）。"""
        src = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertIn('p.endswith("/resume"), _post_task_resume', src)
        orch_src = Path("orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn('"type": "resume_task"', src)
        self.assertIn('== "resume_task"', orch_src)
        self.assertIn("resume_takeover_allowed(orch", orch_src,
                      "消息分支必须用可单测的裁决函数，不是内联状态比较")
        self.assertIn("resume=True", orch_src, "恢复必须把 resume 传给 run()")
        self.assertIn("resume_completed", orch_src)

    def test_run_accepts_resume_flag(self):
        """`run(resume=True)` → `_execute_steps(resume_completed=True)` 接线。"""
        import inspect
        self.assertIn("resume", inspect.signature(ov.OrchestratorV2.run).parameters)
        self.assertIn("resume", inspect.signature(ov.run_and_finalize).parameters)
        body = inspect.getsource(ov.OrchestratorV2.run)
        self.assertIn("_resume_this_round = _is_resume_run and not _resume_steps_applied",
                      body, "续跑只对**首轮**生效（反思重做的新步骤必须照常跑）")
        self.assertIn("resume_completed", body)
        # 关键词只在续跑首轮传入（默认 False 语义相同）：`_execute_steps` 的
        # 既有 3 参替身（test_delivery_chain / test_orchestrator_v2 /
        # test_task_time_optimization 按 `run()` 整链打补丁）不受新参数影响。
        self.assertIn("if _resume_this_round else {}", body)
        self.assertIn("_restore_dead_fetch(task_id)", body)
        self.assertIn("if not _is_resume_run:", body,
                      "续跑不得清空工作区（会把已完成步骤的产物删掉）")


class TestAccounting(unittest.TestCase):
    """账目：AGENTS.md 计数与 evals 用例（随本 commit 一并结清）。"""

    def test_agents_md_test_count_matches_repo(self):
        text = Path("AGENTS.md").read_text(encoding="utf-8")
        claimed = [int(m.group(1)) for m in
                   __import__("re").finditer(r"(\d+) 个 test_\*\.py", text)]
        self.assertTrue(claimed, "AGENTS.md 应写明 test_*.py 数量")
        actual = len(list(Path(".").glob("test_*.py")))
        self.assertIn(actual, claimed,
                      f"AGENTS.md 说 {claimed}，实际 {actual} 个 test_*.py")

    def test_new_suites_registered_in_ci(self):
        ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        for name in ("test_task24_dead_source_annotation.py",
                     "test_task25_disclaimer_enforcement.py",
                     "test_task26_checkpointer.py"):
            self.assertIn(name, ci, f"{name} 必须进 CI（测试只增不减）")

    def test_grown_eval_cases_present_and_valid(self):
        data = json.loads(Path("evals/cases/auto_grown.json").read_text(
            encoding="utf-8"))
        cases = data.get("cases") if isinstance(data, dict) else data
        by_id = {str(c.get("id")): c for c in cases}
        for cid in ("ag-047", "ag-049"):
            self.assertIn(cid, by_id, f"缺 eval 用例 {cid}")
            case = by_id[cid]
            self.assertTrue(str(case.get("title") or case.get("goal") or "").strip(),
                            f"{cid} 必须有标题/目标")
            self.assertTrue(case.get("expect") or case.get("checks")
                            or case.get("rubric") or case.get("criteria"),
                            f"{cid} 必须有可判定的期望")


if __name__ == "__main__":
    unittest.main(verbosity=2)
