"""任务20：Redis 降级路径（探活 + 降级标志 + 派发闸门 + 自愈 + 可观测）。

设计文档要的四条，逐条落成断言：
1. Redis 不可用时派发**不静默**：编排器拒绝派发并给出可读原因（`redis_degraded` 标记）；
2. 探活有超时上限（2s）且错误文本**脱敏**（公开接口不泄漏内网地址/端口）；
3. guardian 启动 + 每次心跳探活，按**状态变化**告警，恢复后自动退出降级（无需重启）；
4. 健康接口可观测：公开 `/api/health` 只给布尔与延迟，鉴权 `/api/status` 给完整快照。

现实校对（文档与代码不一致处，按代码事实测）：
- 文档说"Redis 未启动 → search_health = {} → 完全静默失败"——冷启动其实**不静默**
  （`launcher.py` 起服务前自检、`web_ui._publish_task` 直接抛错），真正缺的是**运行期**
  宕机检测；所以本文件测的是"起来之后 Redis 挂了"这条路径。
- 文档说 `_search_direct(query)` 直连检索——该函数不存在，worker 靠 BRPOP 取任务，没有
  Redis 就没有"直连"可退；实现改为"闸门拒绝 + 可重试记账 + 自愈"。
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import fakeredis

import redis_health as rh
import search_coverage as sc
import worker_guardian as wg


def _probe(available: bool, latency=1.5, error=None) -> dict:
    return {"available": available, "latency_ms": latency if available else None,
            "error": None if available else (error or "Error 10061 connecting to localhost:6379")}


class _RedisStateCase(unittest.TestCase):
    """每个用例前后都清干净进程内降级标志与探测缓存（它们是模块级单例）。"""

    def setUp(self):
        rh.reset_for_tests()

    def tearDown(self):
        rh.reset_for_tests()


class _FakeMessaging:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.alerts: list = []

    def publish(self, channel, payload):
        if self.fail:
            raise RuntimeError("redis down")
        self.alerts.append(payload)

    def alert_types(self):
        return [a.get("type") for a in self.alerts]


class _Handler:
    """`web_ui._get_health` 只需要一个能收 JSON 的 self。"""

    def __init__(self):
        self.payload = None
        self.code = None

    def _json(self, data, code=200, extra_headers=None):
        self.payload = data
        self.code = code
        return data


# ----------------------------------------------------------------------
# 1. 探活本身
# ----------------------------------------------------------------------

class TestRedisProbe(_RedisStateCase):
    def test_unavailable_probe_is_bounded_and_sanitized(self):
        """连不上 → `available: False`，原因里**不能**留下主机/端口。"""
        boom = ConnectionError(
            "Error 10061 connecting to localhost:6379. Connection refused. "
            "redis://weavemind-redis:6379/0")
        with mock.patch.object(rh, "_client", side_effect=boom):
            out = rh.check_redis()
        self.assertFalse(out["available"])
        self.assertIsNone(out["latency_ms"])
        self.assertIn("<redis>", out["error"])
        self.assertNotIn("6379", out["error"])
        self.assertNotIn("localhost", out["error"])
        self.assertNotIn("weavemind-redis", out["error"])

    def test_available_probe_runs_through_real_code_path(self):
        """可用路径走真实 `check_redis`（只把客户端换成 fakeredis）。"""
        fake = fakeredis.FakeStrictRedis()
        with mock.patch.object(rh, "_client", return_value=fake):
            out = rh.check_redis()
        self.assertTrue(out["available"])
        self.assertIsNotNone(out["latency_ms"])
        self.assertGreaterEqual(out["latency_ms"], 0.0)
        self.assertIsNone(out["error"])

    def test_connect_timeout_is_two_seconds(self):
        """超时必须是"有界"的：默认重试会把 2s 叠成分钟级（历史实测 26~48s）。"""
        self.assertEqual(rh.CONNECT_TIMEOUT, 2.0)

    def test_url_resolution_precedence(self):
        env = dict(os.environ)
        env["REDIS_URL"] = "redis://from-env:6390/1"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(rh.redis_url("redis://explicit:1/0"), "redis://explicit:1/0")
            self.assertEqual(rh.redis_url(), "redis://from-env:6390/1")
        env.pop("REDIS_URL")
        env["REDIS_HOST"] = "10.0.0.9"
        env["REDIS_PORT"] = "6391"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(rh.redis_url(), "redis://10.0.0.9:6391")
        env.pop("REDIS_HOST")
        env.pop("REDIS_PORT")
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(rh.redis_url(), "redis://127.0.0.1:6379")

    def test_sanitize_error_strips_urls_and_hostports(self):
        out = rh.sanitize_error(
            "Error 10061 connecting to localhost:6379. redis://a.b:1234/x 拒绝")
        self.assertNotIn("localhost", out)
        self.assertNotIn("6379", out)
        self.assertNotIn("a.b", out)
        self.assertIn("<redis>", out)


# ----------------------------------------------------------------------
# 2. 降级标志（进程内）与探测缓存
# ----------------------------------------------------------------------

class TestDegradedFlag(_RedisStateCase):
    def test_set_degraded_mode_reports_transitions(self):
        self.assertTrue(rh.set_degraded_mode(True, error="boom", source="t"))
        self.assertFalse(rh.set_degraded_mode(True, error="boom", source="t"),
                         "状态未变化时不得再报一次变化（告警会刷屏）")
        self.assertTrue(rh.is_degraded())
        self.assertTrue(rh.set_degraded_mode(False))
        self.assertFalse(rh.is_degraded())

    def test_degraded_state_carries_duration_and_sanitized_error(self):
        rh.set_degraded_mode(True, error="Error 10061 connecting to localhost:6379")
        st = rh.degraded_state()
        self.assertTrue(st["degraded"])
        self.assertGreater(st["since"], 0.0)
        self.assertGreaterEqual(st["duration_s"], 0.0)
        self.assertNotIn("localhost", st["error"])

    def test_cached_probe_uses_ttl_then_refreshes(self):
        probe = mock.Mock(return_value=_probe(True))
        with mock.patch.object(rh, "check_redis", probe):
            first = rh.check_redis_cached()
            second = rh.check_redis_cached()
        self.assertEqual(probe.call_count, 1, "TTL 内不得重复真连（/api/status 3s 轮询）")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        with mock.patch.object(rh, "check_redis", probe):
            third = rh.check_redis_cached(ttl=0)
        self.assertEqual(probe.call_count, 2)
        self.assertFalse(third["cached"])

    def test_cached_probe_refreshes_flag_both_ways(self):
        with mock.patch.object(rh, "check_redis", return_value=_probe(False)):
            rh.check_redis_cached()
            self.assertTrue(rh.is_degraded())
        with mock.patch.object(rh, "check_redis", return_value=_probe(True)):
            rh.check_redis_cached(ttl=0)
            self.assertFalse(rh.is_degraded())

    def test_public_snapshot_hides_error_and_since(self):
        rh.set_degraded_mode(True, error="Error 10061 connecting to localhost:6379")
        pub = rh.public_snapshot()
        self.assertEqual(set(pub.keys()), {"available", "degraded_mode", "latency_ms"})
        self.assertNotIn("error", pub)
        self.assertNotIn("degraded_since", pub)
        full = rh.status_snapshot()
        self.assertIn("error", full)
        self.assertIn("degraded_since", full)
        self.assertIn("degraded_duration_s", full)
        self.assertTrue(full["degraded_mode"])
        self.assertNotIn("localhost", full["error"])

    def test_status_snapshot_never_raises_when_probe_explodes(self):
        with mock.patch.object(rh, "check_redis", side_effect=RuntimeError("probe bug")):
            snap = rh.status_snapshot()
        self.assertFalse(snap["available"])
        self.assertIn("probe bug", snap["error"])


# ----------------------------------------------------------------------
# 3. 派发闸门
# ----------------------------------------------------------------------

class TestDispatchGate(_RedisStateCase):
    def test_healthy_gate_is_open_and_does_not_probe(self):
        probe = mock.Mock(return_value=_probe(True))
        with mock.patch.object(rh, "check_redis_cached", probe):
            self.assertEqual(rh.dispatch_gate(), "")
        self.assertEqual(probe.call_count, 0, "健康时不该多一次探测")

    def test_degraded_gate_closed_with_readable_reason(self):
        rh.set_degraded_mode(True, error="boom")
        with mock.patch.object(rh, "check_redis", return_value=_probe(False)):
            why = rh.dispatch_gate()
        self.assertIn("降级模式", why)
        self.assertIn("未派发", why)
        self.assertIn("无需重启", why)

    def test_degraded_gate_self_heals_when_redis_is_back(self):
        """短抖动恢复后必须自动放行（否则整个任务永久卡在降级上，要人重启）。

        这里只替换**真探测**那一层，让带缓存的包装器照常跑——清标志正是它的职责。
        """
        rh.set_degraded_mode(True, error="boom")
        with mock.patch.object(rh, "check_redis", return_value=_probe(True)):
            self.assertEqual(rh.dispatch_gate(), "")
        self.assertFalse(rh.is_degraded())

    def test_degraded_gate_stays_closed_when_reprobe_fails(self):
        rh.set_degraded_mode(True, error="boom")
        with mock.patch.object(rh, "check_redis_cached", side_effect=RuntimeError("boom")):
            self.assertIn("降级模式", rh.dispatch_gate())


# ----------------------------------------------------------------------
# 4. guardian：启动探活 + 心跳探活 + 按变化告警 + 自愈
# ----------------------------------------------------------------------

def _guardian(messaging=None) -> wg.WorkerGuardian:
    """只装探活需要的最小状态（绕开 __init__ 的 docker/文件探测）。"""
    g = wg.WorkerGuardian.__new__(wg.WorkerGuardian)
    g._messaging = messaging if messaging is not None else _FakeMessaging()
    g._registry = mock.MagicMock()
    g._workers = {}
    g._redis_degraded = False
    g._simulate = True
    g._grace_seconds = 0.0
    g._running = False
    g._shutting_down = False
    return g


class TestGuardianRedisHealth(_RedisStateCase):
    def test_redis_down_sets_flag_logs_error_and_alerts_once(self):
        g = _guardian()
        with mock.patch.object(rh, "check_redis", return_value=_probe(False)):
            with self.assertLogs("worker_guardian", level="ERROR") as logs:
                out = g._check_redis_health()
            second = g._check_redis_health()
        self.assertFalse(out["available"])
        self.assertFalse(second["available"])
        self.assertTrue(any("进入降级模式" in line for line in logs.output))
        self.assertEqual(g._messaging.alert_types(), ["redis_unavailable"],
                         "持续不可用期间不得每 30s 刷一条告警")
        self.assertTrue(g._redis_degraded)
        self.assertTrue(rh.is_degraded())

    def test_recovery_clears_flag_and_alerts(self):
        g = _guardian()
        with mock.patch.object(rh, "check_redis", return_value=_probe(False)):
            g._check_redis_health()
        with mock.patch.object(rh, "check_redis", return_value=_probe(True, latency=4.2)):
            out = g._check_redis_health()
        self.assertTrue(out["available"])
        self.assertEqual(g._messaging.alert_types(), ["redis_unavailable", "redis_recovered"])
        self.assertFalse(g._redis_degraded)
        self.assertFalse(rh.is_degraded())

    def test_staying_healthy_never_alerts(self):
        g = _guardian()
        with mock.patch.object(rh, "check_redis", return_value=_probe(True)):
            g._check_redis_health()
            g._check_redis_health()
        self.assertEqual(g._messaging.alert_types(), [])

    def test_probe_exception_counts_as_unavailable(self):
        g = _guardian()
        with mock.patch.object(rh, "check_redis", side_effect=RuntimeError("probe bug")):
            out = g._check_redis_health()
        self.assertFalse(out["available"])
        self.assertTrue(rh.is_degraded())

    def test_alert_publish_failure_does_not_break_probe(self):
        """告警通道本身就是 Redis：发不出去也必须继续探活（否则降级永远解除不了）。"""
        g = _guardian(_FakeMessaging(fail=True))
        with mock.patch.object(rh, "check_redis", return_value=_probe(False)):
            with self.assertLogs("worker_guardian", level="ERROR"):
                out = g._check_redis_health()
        self.assertFalse(out["available"])
        self.assertTrue(g._redis_degraded)

    def test_run_probes_at_startup_and_on_every_heartbeat(self):
        """启动探一次 + 心跳探一次：MSG 顺序即"启动 → 心跳"。"""
        calls: list = []
        g = _guardian()

        def _record():
            calls.append(time.time())
            if len(calls) >= 2:
                g.shutdown()          # 第二次（心跳）之后退出主循环，用例即刻结束
            return _probe(True)

        with mock.patch.object(g, "_setup_signal_handlers"), \
                mock.patch.object(g, "_publish_guardian_heartbeat"), \
                mock.patch.object(g, "_health_check"), \
                mock.patch.object(g, "_reap_stuck_tasks"), \
                mock.patch.object(g, "_check_routing_health"), \
                mock.patch.object(g, "_check_redis_health", side_effect=_record), \
                mock.patch.object(wg, "GUARDIAN_HEARTBEAT_INTERVAL", 0):
            g.run()
        self.assertEqual(len(calls), 2, "启动一次 + 心跳一次")

    def test_run_calls_redis_probe_before_reaping_stuck_tasks(self):
        """源码守卫：启动探活必须在第一轮回收之前（启动日志里就有结论）。"""
        src = Path("worker_guardian.py").read_text(encoding="utf-8")
        self.assertIn("self._check_redis_health()", src)
        head = src[src.index("def run(self)"):src.index("def shutdown(")]
        self.assertEqual(head.count("self._check_redis_health()"), 2,
                         "启动一次 + 心跳一次")
        self.assertLess(head.index("self._check_redis_health()"),
                        head.index("self._reap_stuck_tasks()"))


# ----------------------------------------------------------------------
# 5. orchestrator：派发闸门与失败收尾
# ----------------------------------------------------------------------

class _Permit:
    cancelled = False


def _orch() -> "object":
    import orchestrator_v2 as ov

    o = ov.OrchestratorV2.__new__(ov.OrchestratorV2)
    o._messaging = _FakeMessaging()
    o._task_timeout = 300
    o._task_starts = {}
    o._task_starts_lock = threading.Lock()
    o._task_goals = {}
    o._task_prompt_hints = {}
    return o


class TestOrchestratorDispatchGate(_RedisStateCase):
    def _dispatch(self, o, gate):
        import orchestrator_v2 as ov

        progress: list = []
        with mock.patch.object(ov, "_lease_superseded", return_value=""), \
                mock.patch.object(ov, "push_progress",
                                  side_effect=lambda m, t, c, p: progress.append(p)), \
                mock.patch.object(o, "_task_permit", return_value=(_Permit(), "")), \
                mock.patch.object(o, "_task_contract", return_value=None), \
                mock.patch.object(o, "_find_agent", return_value="search_agent"), \
                mock.patch.object(o, "_new_redis_sync") as redis_sync, \
                mock.patch.object(o, "_cancel_requested", return_value=False), \
                mock.patch.object(o, "_send_gate", return_value=""), \
                mock.patch.object(rh, "dispatch_gate", side_effect=gate):
            out = o._dispatch({"step_id": "1", "capability": "web_search",
                               "instruction": "查营收"}, "t-redis")
        return out, progress, redis_sync

    def test_degraded_dispatch_is_refused_with_readable_reason(self):
        reason = "Redis 不可用（降级模式）：队列/结果通道中断，本步骤未派发"
        out, progress, redis_sync = self._dispatch(_orch(), lambda: reason)
        self.assertEqual(out["status"], "FAILED")
        self.assertTrue(out["redis_degraded"])
        self.assertTrue(out["retryable"], "恢复后重做必须能真的跑起来")
        self.assertEqual(out["result"], reason)
        self.assertEqual(redis_sync.return_value.lpush.call_count, 0,
                         "降级时不得再往队列里塞任务（塞不进去只白等一次超时）")
        self.assertTrue(any("未派发" in str(p.get("message")) for p in progress))

    def test_gate_exception_does_not_block_dispatch(self):
        """闸门自身坏掉不能拦住派发（守卫只降级能力，不能变成单点）。"""
        import orchestrator_v2 as ov

        src = Path("orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn("Redis 降级闸门不可用", src)
        block = src[src.index("_redis_down_why = \"\""):src.index("with self._task_starts_lock")]
        self.assertIn("except Exception", block)
        self.assertIn("import redis_health as _redis_health", block)
        self.assertLess(block.index("try:"), block.index("if _redis_down_why:"))

    def test_dispatch_failure_helper_settles_budget_and_marks_degraded(self):
        o = _orch()
        progress: list = []
        import orchestrator_v2 as ov

        with mock.patch.object(o, "_budget_settle") as settle, \
                mock.patch.object(ov, "push_progress",
                                  side_effect=lambda m, t, c, p: progress.append(p)):
            out = o._redis_dispatch_failure("t-redis", "web_search", "1", "ticket-1",
                                            ConnectionError("Connection refused"))
        settle.assert_called_once()
        args, kwargs = settle.call_args
        self.assertEqual(args[0], "t-redis")
        self.assertEqual(args[1], "ticket-1")
        self.assertFalse(kwargs.get("ok", True))
        self.assertEqual(kwargs.get("note"), "redis_unavailable")
        self.assertEqual(out["status"], "FAILED")
        self.assertTrue(out["redis_degraded"])
        self.assertTrue(out["retryable"])
        self.assertIn("Redis 不可用", out["result"])
        self.assertTrue(rh.is_degraded(), "失败收尾必须同时点亮降级标志")
        self.assertTrue(any("Redis 连接失败" in str(p.get("message")) for p in progress))

    def test_dispatch_failure_helper_tolerates_settle_errors(self):
        o = _orch()
        import orchestrator_v2 as ov

        with mock.patch.object(o, "_budget_settle", side_effect=RuntimeError("ledger down")), \
                mock.patch.object(ov, "push_progress"):
            out = o._redis_dispatch_failure("t-redis", "web_search", "1", "tk",
                                            TimeoutError("timeout"))
        self.assertEqual(out["status"], "FAILED")
        self.assertTrue(out["redis_degraded"])

    def test_lpush_connection_error_is_taken_over_but_other_errors_reraised(self):
        """只有 Redis 连接类异常被接管；其它异常照旧上抛，不改既有语义。"""
        src = Path("orchestrator_v2.py").read_text(encoding="utf-8")
        block = src[src.index("except Exception as _lp_exc"):src.index("_track_inflight(task_id, dispatch_id")]
        self.assertIn("_redis_mod.exceptions.ConnectionError", block)
        self.assertIn("_redis_mod.exceptions.TimeoutError", block)
        self.assertIn("if not _redis_down:", block)
        self.assertIn("raise", block)
        self.assertIn("self._redis_dispatch_failure(", block)


# ----------------------------------------------------------------------
# 6. 可观测面：统一依赖健康 + 公开/鉴权健康接口
# ----------------------------------------------------------------------

class TestObservabilitySurfaces(_RedisStateCase):
    def test_health_registry_has_redis_probe(self):
        import health_registry as hr

        names = {getattr(p, "__name__", "") for p in hr._PROBES}
        self.assertIn("probe_redis", names)

    def test_probe_redis_unavailable_is_red(self):
        import health_registry as hr

        with mock.patch.object(rh, "check_redis", return_value=_probe(False)):
            entry = hr.probe_redis()
        self.assertEqual(entry["name"], "redis")
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["state"], "unavailable")
        self.assertNotIn("localhost", entry["reason"])

    def test_probe_redis_available_is_green(self):
        import health_registry as hr

        with mock.patch.object(rh, "check_redis", return_value=_probe(True, latency=3.5)):
            entry = hr.probe_redis()
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["state"], "available")
        self.assertIn("3.5", entry["detail"])

    def test_probe_redis_never_raises(self):
        import health_registry as hr

        with mock.patch.object(rh, "check_redis", side_effect=RuntimeError("probe bug")):
            entry = hr.probe_redis()
        self.assertFalse(entry["ok"])

    def test_public_health_exposes_redis_without_error_text(self):
        import web_ui

        h = _Handler()
        with mock.patch.object(rh, "check_redis", return_value=_probe(False)):
            web_ui._get_health(h, "/api/health")
        redis_part = h.payload["redis"]
        self.assertFalse(redis_part["available"])
        self.assertTrue(redis_part["degraded_mode"])
        self.assertEqual(set(redis_part.keys()), {"available", "degraded_mode", "latency_ms"})
        self.assertNotIn("6379", str(h.payload))

    def test_system_status_carries_full_redis_snapshot(self):
        import llm_client
        import web_ui

        with mock.patch.object(web_ui, "_redis_ready", return_value=False), \
                mock.patch.object(rh, "check_redis", return_value=_probe(False)), \
                mock.patch.object(llm_client, "get_endpoint_health", return_value={}), \
                mock.patch.object(llm_client, "get_balance_status", return_value={}):
            st = web_ui._system_status()
        self.assertFalse(st["redis"]["available"])
        self.assertTrue(st["redis"]["degraded_mode"])
        self.assertIn("degraded_duration_s", st["redis"])
        self.assertIn("<redis>", st["redis"]["error"])
        self.assertEqual(st["search_health"], {}, "Redis 不可用时检索快照本来就读不到")
        deps = {d.get("name"): d for d in st.get("dependencies", [])}
        self.assertIn("redis", deps)
        self.assertFalse(deps["redis"]["ok"])

    def test_web_ui_wiring_source_guard(self):
        src = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertIn('payload["redis"] = _redis_health.public_snapshot()', src)
        self.assertIn('"redis": redis_status,', src)
        self.assertIn("_redis_health.status_snapshot()", src)


# ----------------------------------------------------------------------
# 7. 报告里也要说清楚"检索服务降级运行"（复用任务19 的摘要块）
# ----------------------------------------------------------------------

def _redis_step(step_id: str = "1") -> dict:
    return {"step_id": step_id, "capability": "web_search",
            "result": {"status": "FAILED", "redis_degraded": True,
                       "result": "Redis 不可用，步骤未派发"}}


def _ok_step(step_id: str = "2") -> dict:
    return {"step_id": step_id, "capability": "web_search",
            "result": {"status": "SUCCESS",
                       "search_verdict": {"status": "ok", "completed": True}}}


class TestReportNotesRedisDegraded(unittest.TestCase):
    def test_redis_degraded_step_gets_its_own_bucket(self):
        s = sc.summarize_search_coverage([_redis_step(), _ok_step()])
        self.assertEqual(s["redis_degraded_steps"], 1)
        self.assertEqual(s["incomplete_steps"], 0, "不得既算降级运行又算未完成")
        self.assertEqual(s["total_search_steps"], 2)
        self.assertEqual(s["coverage_rate"], 0.5)

    def test_summary_always_carries_the_key(self):
        s = sc.summarize_search_coverage([])
        self.assertEqual(s["redis_degraded_steps"], 0)

    def test_block_states_redis_degradation_explicitly(self):
        block = sc.render_coverage_block(
            sc.summarize_search_coverage([_redis_step()]), rules_version="v1")
        self.assertIn("降级运行步 | 1（Redis 不可用，检索服务降级运行）", block)
        self.assertIn("检索服务降级运行（Redis 不可用）", block)

    def test_redis_degradation_alert_wins_over_generic_low_coverage(self):
        s = sc.summarize_search_coverage([_redis_step()])
        self.assertIn("检索服务降级运行", sc.coverage_alert(s))

    def test_report_footer_carries_the_note(self):
        import delivery_pipeline as dp

        with mock.patch.object(__import__("task_state"), "task_projection",
                               return_value={"steps": [_redis_step(), _ok_step()]}):
            out = dp._attach_search_coverage("# 研究简报\n\n正文。\n", "t-redis")
        self.assertIn("## 数据来源摘要", out)
        self.assertIn("检索服务降级运行（Redis 不可用）", out)
        self.assertIn("| 降级运行步 | 1", out)

    def test_no_redis_degradation_adds_no_noise(self):
        s = sc.summarize_search_coverage([_ok_step()])
        block = sc.render_coverage_block(s)
        self.assertNotIn("降级运行步", block)
        self.assertNotIn("Redis", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
