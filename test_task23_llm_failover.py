# -*- coding: utf-8 -*-
"""任务23 — LLM 端点熔断与自动切换备援（docs/arch_phase4_task23_llm_failover.md）

验收对照（架构师指定口径，逐条落地为用例）：
① 三态状态机：CLOSED →（连续 5 次端点类失败）→ OPEN，按 backup 列表顺序切换；
   60s 后下一次真实调用打回 primary 探测（HALF_OPEN），成功 → CLOSED、失败 → OPEN。
② worker 集成：`async_worker_base._call_llm` 调用前激活端点、成功/失败回喂熔断器
   （`_call_llm` 原语义保留，重命名为 `_call_llm_transport`）。
③ guardian 心跳：熔断期间发 `llm_circuit_open` 告警（去重：状态指纹没变不重复）。
④ `/api/health` 增 `llm: {state, active_endpoint, consecutive_failures}`，只含
   状态名/端点名/计数，不含任何地址或密钥。

架构约束的回归：`model_ctx`/`model_deny`/`cancelled`/`budget_exhausted` 不计入熔断；
`backup_endpoints` 为空 → 零影响（状态机停在 CLOSED、不写任何环境变量）；
熔断器/Redis 自身故障绝不拦调用、绝不外抛。

注意：本文件全部用 patch 后的状态汇聚（不真连 Redis），无网络依赖、可离线跑。
"""

import asyncio
import inspect
import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

import llm_circuit_breaker as lcb
import worker_guardian as wg

CI_YML = Path(".github/workflows/ci.yml")

# 测试用的双备援端点配置（假地址，绝不发真请求）
_CFG_TWO = {
    "llm": {"base_url": "https://primary.example/v1", "api_key": "pk", "model": "m1"},
    "backup_endpoints": [
        {"base_url": "https://backup1.example/v1", "api_key": "bk1", "model": "m2"},
        {"base_url": "https://backup2.example/v1", "api_key": "bk2", "model": "m3"},
    ],
}

_ENV_KEYS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")


class _Clock:
    """可手动推进的单调时钟（探测窗口 60s 用它推进，不真等）。"""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _EnvGuard:
    """LLM_* 环境变量的快照保护：测试结束后逐字节还原。"""

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        return self

    def __exit__(self, *exc):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _breaker(cfg=None, **kwargs):
    """构造一个不发 Redis 镜像的熔断器（sink 记到本地列表）。"""
    cfg = _CFG_TWO if cfg is None else cfg
    kwargs.setdefault("state_sink", (lambda snap: None))
    return lcb.build_breaker(cfg, **kwargs)


class _BaseSilent(unittest.TestCase):
    """公共底座：Redis 镜像改成本地记录，LLM_* 环境变量受保护。"""

    def setUp(self):
        self.published = []
        p = mock.patch.object(lcb, "_publish_state", self.published.append)
        p.start()
        self.addCleanup(p.stop)
        self.env = _EnvGuard()
        self.env.__enter__()
        self.addCleanup(self.env.__exit__, None, None, None)
        # 单例复位于真实 config.json 的 mtime，防下次 breaker() 把它冲掉
        lcb.reset_for_tests(_CFG_TWO)
        self.addCleanup(lcb.reset_for_tests, _CFG_TWO)


# ---------------------------------------------------------------------------
# 错误归类（不计入熔断的口径）
# ---------------------------------------------------------------------------

class TestErrorClassification(unittest.TestCase):
    def test_connection_refused_is_network_not_deny(self):
        # "refused" 一词会被关键词法误判成 content 拒绝 → model_deny；
        # 类型判断必须先于关键词判断
        self.assertEqual(
            lcb.classify_endpoint_error(ConnectionRefusedError("connection refused")),
            "network")

    def test_timeout_and_oserror_are_network(self):
        self.assertEqual(lcb.classify_endpoint_error(TimeoutError("t")), "network")
        self.assertEqual(lcb.classify_endpoint_error(OSError("socket closed")), "network")

    def test_http_code_and_rate_limit(self):
        self.assertEqual(lcb.classify_endpoint_error(RuntimeError("HTTP 503")), "http_503")
        self.assertEqual(lcb.classify_endpoint_error(RuntimeError("429 too many")),
                         "rate_limit")

    def test_request_problem_classes(self):
        self.assertEqual(lcb.classify_endpoint_error(RuntimeError("context too long")),
                         "model_ctx")
        self.assertEqual(lcb.classify_endpoint_error(RuntimeError("content policy refuse")),
                         "model_deny")

    def test_cancelled_and_budget(self):
        import asyncio
        exc = RuntimeError("nope")
        exc.budget_exhausted = True
        self.assertEqual(lcb.classify_endpoint_error(exc), "budget_exhausted")
        self.assertEqual(lcb.classify_endpoint_error(asyncio.CancelledError()), "cancelled")

    def test_generic(self):
        self.assertEqual(lcb.classify_endpoint_error(ValueError("weird")), "generic")

    def test_counts_for_circuit_excludes_request_problems(self):
        for not_counted in ("model_ctx", "model_deny", "cancelled", "budget_exhausted"):
            self.assertFalse(lcb.counts_for_circuit(not_counted), not_counted)
        for counted in ("network", "http_503", "rate_limit", "generic", ""):
            self.assertTrue(lcb.counts_for_circuit(counted), counted or "<empty>")


# ---------------------------------------------------------------------------
# 配置装载
# ---------------------------------------------------------------------------

class TestConfigLoading(unittest.TestCase):
    def test_backup_endpoints_list_in_order(self):
        eps = lcb.load_backups(_CFG_TWO)
        self.assertEqual([e["name"] for e in eps], ["backup_1", "backup_2"])
        self.assertEqual(eps[0]["base_url"], "https://backup1.example/v1")
        self.assertEqual(eps[1]["model"], "m3")

    def test_legacy_single_backup_object(self):
        # config.json 现状没有 backup_endpoints 键，只有单个 backup 对象：
        # 按单元素列表兜底，功能保持可用（对架构师文档的偏差，见交付报告）
        eps = lcb.load_backups({"backup": {"base_url": "https://b/v1", "api_key": "k",
                                           "model": "bm"}})
        self.assertEqual([e["name"] for e in eps], ["backup_1"])
        self.assertEqual(eps[0]["base_url"], "https://b/v1")

    def test_placeholder_backup_filtered(self):
        eps = lcb.load_backups({"backup": {"base_url": "https://b/v1",
                                           "api_key": "YOUR_API_KEY"}})
        self.assertEqual(eps, [])
        eps = lcb.load_backups({"backup_endpoints": [
            {"base_url": "", "api_key": "k"}, None,
            {"base_url": "https://ok/v1", "api_key": "k"}]})
        self.assertEqual([e["name"] for e in eps], ["backup_1"])

    def test_no_backup_means_disabled(self):
        b = _breaker({"llm": {"base_url": "https://p/v1"}})
        self.assertFalse(b.enabled)
        self.assertEqual(b.fail_threshold, 5)

    def test_primary_from_config_then_env(self):
        prim = lcb.load_primary({"llm": {"base_url": "https://p/v1", "api_key": "k",
                                         "model": "m"}})
        self.assertEqual(prim["base_url"], "https://p/v1")
        with _EnvGuard():
            os.environ.update(LLM_BASE_URL="https://env.example/v1")
            prim = lcb.load_primary({})
            self.assertEqual(prim["base_url"], "https://env.example/v1")

    def test_defaults_and_config_override(self):
        b = _breaker()
        self.assertEqual(b.fail_threshold, 5)
        self.assertEqual(b.reset_timeout, 60.0)
        b = _breaker({"llm": {"circuit_breaker": {"fail_threshold": 3,
                                                  "reset_timeout": 30}}})
        self.assertEqual(b.fail_threshold, 3)
        self.assertEqual(b.reset_timeout, 30.0)


# ---------------------------------------------------------------------------
# ① 三态状态机
# ---------------------------------------------------------------------------

class TestCircuitStateMachine(_BaseSilent):
    def test_threshold_reached_opens_and_switches_to_backup_1(self):
        b = _breaker()
        for _ in range(4):
            b.record_failure("network")
        self.assertEqual(b.state(), "CLOSED")
        b.record_failure("http_503")                      # 第 5 次（不同端点类也计）
        self.assertEqual(b.state(), "OPEN")
        self.assertEqual(b.current_endpoint(), "backup_1")
        snap = b.snapshot()
        self.assertEqual(snap["consecutive_failures"], 5)
        self.assertEqual(snap["switches"], 1)

    def test_request_problem_failures_never_trip(self):
        b = _breaker()
        for _ in range(20):
            b.record_failure("model_ctx")
            b.record_failure("model_deny")
        self.assertEqual(b.state(), "CLOSED")
        self.assertEqual(b.snapshot()["consecutive_failures"], 0)

    def test_backup_list_advances_in_order(self):
        b = _breaker()
        for _ in range(5):
            b.record_failure("network")
        for _ in range(5):                                # backup_1 也连续失败 → 换 backup_2
            b.record_failure("network")
        self.assertEqual(b.state(), "OPEN")
        self.assertEqual(b.current_endpoint(), "backup_2")
        for _ in range(5):                                # 没有下一个 → 留在 backup_2
            b.record_failure("network")
        self.assertEqual(b.current_endpoint(), "backup_2")
        self.assertEqual(b.snapshot()["switches"], 2)     # OPEN ×2（换备援才算新切换）

    def test_probe_after_reset_timeout_success_closes(self):
        clock = _Clock()
        b = _breaker(now_fn=clock)
        for _ in range(5):
            b.record_failure("network")
        clock.advance(59.9)
        self.assertEqual(b.current_endpoint(), "backup_1")   # 窗口未到
        clock.advance(0.2)
        self.assertEqual(b.current_endpoint(), "primary")    # 60s 窗口过 → 探测
        self.assertEqual(b.state(), "HALF_OPEN")             # （读已推进后的状态）
        b.record_success()
        self.assertEqual(b.state(), "CLOSED")
        self.assertEqual(b.snapshot()["consecutive_failures"], 0)

    def test_probe_failure_reopens_with_fresh_window(self):
        clock = _Clock()
        b = _breaker(now_fn=clock)
        for _ in range(5):
            b.record_failure("network")
        clock.advance(61)
        self.assertEqual(b.current_endpoint(), "primary")    # 进入 HALF_OPEN
        b.record_failure("network")                          # 探测失败
        self.assertEqual(b.state(), "OPEN")
        self.assertEqual(b.current_endpoint(), "backup_1")
        clock.advance(30)                                    # 重新计时：30s < 60s 不探测
        self.assertEqual(b.current_endpoint(), "backup_1")

    def test_no_backup_stays_closed_zero_switches(self):
        b = _breaker({"llm": {"base_url": "https://p/v1"}})
        for _ in range(9):
            b.record_failure("network")
        self.assertEqual(b.state(), "CLOSED")
        self.assertEqual(b.current_endpoint(), "primary")
        snap = b.snapshot()
        self.assertEqual(snap["backups_configured"], 0)
        self.assertEqual(snap["switches"], 0)

    def test_sink_called_only_on_transitions(self):
        seen = []
        b = _breaker(state_sink=seen.append)
        for _ in range(4):
            b.record_failure("network")
        self.assertEqual(len(seen), 0)                    # 未到阈值：无状态变化
        b.record_failure("network")                       # → OPEN
        self.assertEqual([s["state"] for s in seen], ["OPEN"])
        b.record_success()                                # → CLOSED
        self.assertEqual([s["state"] for s in seen], ["OPEN", "CLOSED"])
        for _ in range(5):                                # → OPEN（换备援也算变化）
            b.record_failure("network")
        self.assertEqual([s["state"] for s in seen],
                         ["OPEN", "CLOSED", "OPEN"])

    def test_success_resets_counters_and_endpoint(self):
        b = _breaker()
        for _ in range(5):
            b.record_failure("network")
        b.record_success()
        snap = b.snapshot()
        self.assertEqual(snap["state"], "CLOSED")
        self.assertEqual(snap["active_endpoint"], "primary")
        self.assertEqual(snap["consecutive_failures"], 0)

    def test_snapshot_has_no_secrets(self):
        b = _breaker()
        for _ in range(5):
            b.record_failure("network")
        text = json.dumps(b.snapshot())
        self.assertNotIn("http", text)
        self.assertNotIn("api_key", text.lower())
        self.assertFalse(any("url" in k or "key" in k for k in b.snapshot()))


# ---------------------------------------------------------------------------
# 端点切换（环境变量）与零影响约束
# ---------------------------------------------------------------------------

class TestEnvSwitching(_BaseSilent):
    def test_closed_first_apply_writes_nothing(self):
        os.environ.update(LLM_BASE_URL="https://proc.example/v1",
                          LLM_API_KEY="proc_key", LLM_MODEL="proc_model")
        b = _breaker()
        self.assertEqual(b.apply_env()["name"], "primary")
        self.assertEqual(os.environ["LLM_BASE_URL"], "https://proc.example/v1")
        self.assertEqual(os.environ["LLM_API_KEY"], "proc_key")

    def test_open_switches_env_to_backup(self):
        b = _breaker()
        b.apply_env()                                     # CLOSED：记录"已在主端点"
        for _ in range(5):
            b.record_failure("network")
        self.assertEqual(b.apply_env()["name"], "backup_1")
        self.assertEqual(os.environ["LLM_BASE_URL"], "https://backup1.example/v1")
        self.assertEqual(os.environ["LLM_API_KEY"], "bk1")
        self.assertEqual(os.environ["LLM_MODEL"], "m2")

    def test_recovery_restores_primary(self):
        b = _breaker()
        b.apply_env()
        for _ in range(5):
            b.record_failure("network")
        b.apply_env()
        self.assertEqual(os.environ["LLM_BASE_URL"], "https://backup1.example/v1")
        b.record_success()
        self.assertEqual(b.apply_env()["name"], "primary")
        self.assertEqual(os.environ["LLM_BASE_URL"], "https://primary.example/v1")
        self.assertEqual(os.environ["LLM_API_KEY"], "pk")

    def test_disabled_never_touches_env(self):
        before = dict(os.environ)
        b = _breaker({"llm": {"base_url": "https://p/v1"}})
        for _ in range(9):
            b.record_failure("network")
        self.assertEqual(b.apply_env()["name"], "primary")
        for key in _ENV_KEYS:
            self.assertEqual(os.environ.get(key), before.get(key), key)

    def test_external_env_drift_is_repaired(self):
        """_ensure_cfg_fresh() 会在 config mtime 变化时把 env 重写回主端点——
        处于 OPEN 期间发生这种覆盖时，下次调用必须自愈回备援端点。"""
        b = _breaker()
        b.apply_env()                                     # CLOSED：记录"已在主端点"
        for _ in range(5):
            b.record_failure("network")
        b.apply_env()                                     # → backup_1
        self.assertEqual(os.environ["LLM_BASE_URL"], "https://backup1.example/v1")
        os.environ["LLM_BASE_URL"] = "https://primary.example/v1"   # 模拟热重载打回
        self.assertEqual(b.apply_env()["name"], "backup_1")
        self.assertEqual(os.environ["LLM_BASE_URL"], "https://backup1.example/v1")
        self.assertEqual(os.environ["LLM_API_KEY"], "bk1")

    def test_module_activate_uses_singleton(self):
        with _EnvGuard():
            os.environ.update(LLM_BASE_URL="https://p2.example/v1",
                              LLM_API_KEY="pk2", LLM_MODEL="pm2")
            lcb.reset_for_tests(_CFG_TWO)
            self.assertEqual(lcb.activate_for_call("react_agent"), "primary")
            for _ in range(5):
                lcb.report_failure(RuntimeError("HTTP 503"))
            self.assertEqual(lcb.activate_for_call("react_agent"), "backup_1")
            self.assertEqual(os.environ["LLM_BASE_URL"], "https://backup1.example/v1")
            self.assertEqual(lcb.breaker().state(), "OPEN")


# ---------------------------------------------------------------------------
# 跨进程镜像与聚合（全部 patch，不真连 Redis）
# ---------------------------------------------------------------------------

class TestMirrorAndAggregation(_BaseSilent):
    def test_transition_publishes_mirror_snapshot(self):
        b = _breaker(state_sink=self.published.append)
        for _ in range(5):
            b.record_failure("network")
        self.assertEqual(len(self.published), 1)
        self.assertEqual(self.published[0]["state"], "OPEN")
        self.assertEqual(self.published[0]["active_endpoint"], "backup_1")

    def test_publish_failure_is_silent(self):
        def boom(snap):
            raise RuntimeError("redis down")
        b = _breaker(state_sink=boom)
        for _ in range(5):
            b.record_failure("network")                   # 镜像失败不得外抛
        self.assertEqual(b.state(), "OPEN")

    def test_state_client_failure_is_silent(self):
        with mock.patch.object(lcb, "_state_client",
                               side_effect=RuntimeError("no redis")):
            lcb._publish_state({"state": "OPEN"})         # 不抛
            self.assertEqual(lcb.read_shared_states(), [])

    def test_state_client_passes_no_retry(self):
        # 全仓守卫（test_startup_readiness.TestNoUnretriedRedisClients）同口径：
        # 客户端必须显式传 retry=，且测试里必须能离线构造
        client = lcb._state_client()
        try:
            self.assertIsNotNone(client.get_retry())
        finally:
            client.close()

    def test_aggregate_picks_worst_state(self):
        shared = [
            {"state": "CLOSED", "active_endpoint": "primary", "consecutive_failures": 0},
            {"state": "OPEN", "active_endpoint": "backup_2", "consecutive_failures": 7},
            {"state": "HALF_OPEN", "active_endpoint": "primary", "consecutive_failures": 1},
        ]
        with mock.patch.object(lcb, "read_shared_states", return_value=shared):
            view = lcb.aggregate_view()
        self.assertEqual(view["state"], "OPEN")
        self.assertEqual(view["active_endpoint"], "backup_2")
        self.assertEqual(view["consecutive_failures"], 7)
        self.assertEqual(view["services_reporting"], 2)   # OPEN + HALF_OPEN 两个进程在报

    def test_aggregate_all_closed(self):
        with mock.patch.object(lcb, "read_shared_states", return_value=[]):
            view = lcb.aggregate_view()
        self.assertEqual(view["state"], "CLOSED")
        self.assertEqual(view["active_endpoint"], "primary")
        self.assertEqual(view["consecutive_failures"], 0)
        self.assertEqual(view["services_reporting"], 0)

    def test_aggregate_tolerates_read_failure(self):
        with mock.patch.object(lcb, "read_shared_states",
                               side_effect=RuntimeError("redis down")):
            view = lcb.aggregate_view()                   # 只剩本进程快照，不抛
        self.assertIn(view["state"], ("CLOSED", "OPEN", "HALF_OPEN"))

    def test_health_view_shape_and_no_secrets(self):
        shared = [{"state": "OPEN", "active_endpoint": "backup_1",
                   "consecutive_failures": 5,
                   "base_url": "https://backup1.example/v1", "api_key": "bk1"}]
        with mock.patch.object(lcb, "read_shared_states", return_value=shared):
            view = lcb.health_view()
        self.assertEqual(view["state"], "OPEN")
        self.assertEqual(view["active_endpoint"], "backup_1")
        self.assertEqual(view["consecutive_failures"], 5)
        self.assertEqual(view["backups_configured"], 0)   # 镜像快照没带 → 0
        text = json.dumps(view)
        self.assertNotIn("http", text)
        self.assertNotIn("bk1", text)

    def test_aggregate_counts_own_reporting(self):
        # 本进程单例自己 OPEN（模拟 webui 进程里从未切回）也应被聚合到
        with _EnvGuard():
            b = _breaker()
            for _ in range(5):
                b.record_failure("network")
            with mock.patch.object(lcb, "breaker", return_value=b), \
                 mock.patch.object(lcb, "read_shared_states", return_value=[]):
                view = lcb.aggregate_view()
        self.assertEqual(view["state"], "OPEN")
        self.assertEqual(view["services_reporting"], 1)


# ---------------------------------------------------------------------------
# ③ guardian 心跳
# ---------------------------------------------------------------------------

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


class TestGuardianCircuitAlert(unittest.TestCase):
    def _guardian(self):
        return wg.WorkerGuardian(_FakeMessaging(), _FakeRegistry(), simulate=True)

    def _alerts(self, g):
        return [m for c, m in g._messaging.published if c == "orchestrator:alert"]

    def _patch_view(self, view):
        patcher = mock.patch.object(lcb, "aggregate_view", return_value=view)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_open_state_alerts_once_and_dedupes(self):
        g = self._guardian()
        self._patch_view({"state": "OPEN", "active_endpoint": "backup_1",
                          "consecutive_failures": 5})
        g._check_llm_circuit()
        g._check_llm_circuit()                            # 同一指纹 → 不重复
        alerts = self._alerts(g)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "llm_circuit_open")
        self.assertIn("backup_1", alerts[0]["message"])
        self.assertIn("5", alerts[0]["message"])

    def test_changed_failures_realert(self):
        g = self._guardian()
        self._patch_view({"state": "OPEN", "active_endpoint": "backup_1",
                          "consecutive_failures": 5})
        g._check_llm_circuit()
        self._patch_view({"state": "OPEN", "active_endpoint": "backup_2",
                          "consecutive_failures": 5})
        g._check_llm_circuit()                            # 换了备援 → 新告警
        alerts = self._alerts(g)
        self.assertEqual(len(alerts), 2)
        self.assertIn("backup_2", alerts[1]["message"])

    def test_recovery_alerts_and_resets(self):
        g = self._guardian()
        self._patch_view({"state": "OPEN", "active_endpoint": "backup_1",
                          "consecutive_failures": 5})
        g._check_llm_circuit()
        self._patch_view({"state": "CLOSED", "active_endpoint": "primary",
                          "consecutive_failures": 0})
        g._check_llm_circuit()
        alerts = self._alerts(g)
        self.assertEqual([a["type"] for a in alerts],
                         ["llm_circuit_open", "llm_circuit_recovered"])
        g._check_llm_circuit()                            # CLOSED→CLOSED 无告警
        self.assertEqual(len(self._alerts(g)), 2)

    def test_closed_without_prior_alert_is_silent(self):
        g = self._guardian()
        self._patch_view({"state": "CLOSED", "active_endpoint": "primary",
                          "consecutive_failures": 0})
        g._check_llm_circuit()
        self.assertEqual(self._alerts(g), [])

    def test_read_failure_is_silent(self):
        g = self._guardian()
        patcher = mock.patch.object(lcb, "aggregate_view",
                                    side_effect=RuntimeError("redis down"))
        patcher.start()
        self.addCleanup(patcher.stop)
        g._check_llm_circuit()                            # 读不到 ≠ 没熔断：不告警不抛
        self.assertEqual(self._alerts(g), [])

    def test_heartbeat_calls_llm_circuit_check(self):
        src = inspect.getsource(wg.WorkerGuardian.run)
        self.assertIn("self._check_llm_circuit()", src)
        self.assertLess(src.index("self._check_routing_health()"),
                        src.index("self._check_llm_circuit()"))


# ---------------------------------------------------------------------------
# ④ /api/health 的 llm 字段
# ---------------------------------------------------------------------------

class TestWebUiHealthField(unittest.TestCase):
    def test_health_endpoint_reports_llm_field(self):
        import web_ui

        class _Stub:
            def __init__(self):
                self.payload = None

            def _json(self, data, *a, **k):
                self.payload = data
                return data

        fake = lcb.build_breaker(_CFG_TWO, state_sink=lambda s: None)
        for _ in range(5):
            fake.record_failure("network")                # 让接口暴露真实熔断态
        stub = _Stub()
        with mock.patch.object(lcb, "read_shared_states", return_value=[]), \
                mock.patch.object(lcb, "breaker", return_value=fake):
            view = lcb.health_view()
            web_ui._get_health(stub, "/api/health")
        payload = stub.payload
        self.assertEqual(payload["status"], "ok")
        self.assertIn("llm", payload)
        llm = payload["llm"]
        self.assertEqual(llm["state"], "OPEN")
        self.assertEqual(llm["state"], view["state"])
        self.assertEqual(llm["active_endpoint"], "backup_1")
        self.assertEqual(llm["active_endpoint"], view["active_endpoint"])
        self.assertEqual(llm["consecutive_failures"], 5)
        self.assertEqual(llm["consecutive_failures"], view["consecutive_failures"])
        text = json.dumps(llm)
        self.assertNotIn("http", text)                    # 不含任何地址
        self.assertNotIn("api_key", text.lower())         # 不含任何密钥

    def test_health_endpoint_survives_breaker_failure(self):
        import web_ui

        class _Stub:
            def __init__(self):
                self.payload = None

            def _json(self, data, *a, **k):
                self.payload = data
                return data

        stub = _Stub()
        with mock.patch.object(lcb, "health_view", side_effect=RuntimeError("boom")):
            web_ui._get_health(stub, "/api/health")
        self.assertEqual(stub.payload["status"], "ok")    # 熔断快照失败不坏探活接口
        self.assertNotIn("llm", stub.payload)


# ---------------------------------------------------------------------------
# ② worker 集成
# ---------------------------------------------------------------------------

class TestWorkerBaseIntegration(unittest.TestCase):
    def _worker(self):
        from async_worker_base import AsyncWorkerBase

        class _Dummy(AsyncWorkerBase):
            async def execute(self, *a, **k):             # 抽象方法齐了才能实例化
                pass

            async def run(self):
                pass

        w = _Dummy.__new__(_Dummy)                        # 跳过完整构造（只要 agent_id）
        w.agent_id = "react_agent"
        return w

    def test_transport_renamed_and_hooks_present(self):
        from async_worker_base import AsyncWorkerBase
        self.assertTrue(hasattr(AsyncWorkerBase, "_call_llm_transport"))
        src = inspect.getsource(AsyncWorkerBase._call_llm)
        self.assertIn("activate_for_call", src)
        self.assertIn("report_failure", src)
        self.assertIn("report_success", src)
        # 熔断器自身故障绝不拦调用：激活/记账都包在 try/except 里
        self.assertTrue(re.search(r"except Exception:\s*\n\s*pass", src))

    def test_success_feeds_breaker_and_activates(self):
        import llm_circuit_breaker as lb
        w = self._worker()
        activated, succeeded = [], []

        async def fake_transport(**kw):
            return "ok"

        def fake_activate(service=""):
            activated.append(service)
            return "primary"

        def fake_success():
            succeeded.append(True)

        with mock.patch.object(lb, "activate_for_call", fake_activate), \
             mock.patch.object(lb, "report_success", fake_success):
            w._call_llm_transport = fake_transport        # 实例属性替换：原语义保留
            result = asyncio.run(w._call_llm(prompt="hi"))
        self.assertEqual(result, "ok")
        self.assertEqual(activated, ["react_agent"])      # 用自己的 agent_id 记账
        self.assertEqual(succeeded, [True])

    def test_failure_feeds_breaker_and_reraises(self):
        import llm_circuit_breaker as lb
        w = self._worker()
        failures = []

        async def fake_transport(**kw):
            raise RuntimeError("HTTP 503")

        def fake_failure(exc):
            failures.append(exc)
            return "http_503"

        with mock.patch.object(lb, "activate_for_call", return_value="primary"), \
             mock.patch.object(lb, "report_failure", fake_failure):
            w._call_llm_transport = fake_transport
            with self.assertRaises(RuntimeError):
                asyncio.run(w._call_llm(prompt="hi"))
        self.assertEqual(len(failures), 1)                # 一次调用 = 一次记账

    def test_breaker_failure_never_blocks_the_call(self):
        import llm_circuit_breaker as lb
        w = self._worker()

        async def fake_transport(**kw):
            return "ok"

        with mock.patch.object(lb, "activate_for_call",
                               side_effect=RuntimeError("breaker down")), \
             mock.patch.object(lb, "report_success",
                               side_effect=RuntimeError("breaker down")):
            w._call_llm_transport = fake_transport
            self.assertEqual(asyncio.run(w._call_llm(prompt="hi")), "ok")

    def test_backup_period_passes_model_override_to_transport(self):
        """备援期间：备援自己的模型名作为 model_override 传给两个分支
        （model_roles 优先于 LLM_MODEL，只改 env 换不掉模型名，会 404）。"""
        import llm_circuit_breaker as lb
        w = self._worker()
        seen = []

        async def fake_transport(**kw):
            seen.append(kw)
            return "ok"

        with mock.patch.object(lb, "activate_for_call", return_value="primary"), \
             mock.patch.object(lb, "active_model_override",
                               return_value="backup_model_x"):
            w._call_llm_transport = fake_transport
            asyncio.run(w._call_llm(prompt="hi"))
        self.assertEqual(seen[0]["model_override"], "backup_model_x")

    def test_primary_period_keeps_override_none(self):
        import llm_circuit_breaker as lb
        w = self._worker()
        seen = []

        async def fake_transport(**kw):
            seen.append(kw)
            return "ok"

        with mock.patch.object(lb, "activate_for_call", return_value="primary"), \
             mock.patch.object(lb, "active_model_override", return_value=None):
            w._call_llm_transport = fake_transport
            asyncio.run(w._call_llm(prompt="hi"))
        self.assertIsNone(seen[0]["model_override"])

    def test_active_model_override_follows_active_endpoint(self):
        with _EnvGuard():
            lcb.reset_for_tests(_CFG_TWO)
            self.assertIsNone(lcb.active_model_override())
            b = lcb.breaker()
            for _ in range(5):
                b.record_failure("network")
            self.assertEqual(lcb.active_model_override(), "m2")
            lcb.report_success()
            self.assertIsNone(lcb.active_model_override())

    def test_active_model_override_disabled_or_broken(self):
        with _EnvGuard():
            lcb.reset_for_tests({"llm": {"base_url": "https://p/v1"}})   # 无备援
            self.assertIsNone(lcb.active_model_override())
        with mock.patch.object(lcb, "breaker", side_effect=RuntimeError("down")):
            self.assertIsNone(lcb.active_model_override())               # 故障不抛


# ---------------------------------------------------------------------------
# CI 注册与模块 API
# ---------------------------------------------------------------------------

class TestCiAndModuleApi(unittest.TestCase):
    def test_ci_registers_task23_suite(self):
        text = CI_YML.read_text(encoding="utf-8")
        self.assertIn("python test_task23_llm_failover.py", text)
        # 只减不增：既有任务的注册行必须原样都在
        self.assertIn("python test_task21_supervise_default.py", text)
        self.assertIn("python test_task22_report_compaction.py", text)

    def test_module_singleton_rebuilds_on_config_change(self):
        with _EnvGuard():
            lcb.reset_for_tests(_CFG_TWO)
            first = lcb.breaker()
            self.assertIs(lcb.breaker(), first)           # mtime 未变 → 复用
            lcb.reset_for_tests(_CFG_TWO, fail_threshold=3)
            second = lcb.breaker()
            self.assertIs(lcb.breaker(), second)
            self.assertEqual(second.fail_threshold, 3)

    def test_report_failure_returns_class_and_does_not_raise(self):
        with _EnvGuard():
            lcb.reset_for_tests(_CFG_TWO)
            self.assertEqual(lcb.report_failure(RuntimeError("HTTP 503")), "http_503")
            self.assertEqual(lcb.report_failure("model_ctx"), "model_ctx")
            lcb.report_success()

    def test_report_failure_on_broken_breaker_is_silent(self):
        with mock.patch.object(lcb, "breaker",
                               side_effect=RuntimeError("no breaker")):
            self.assertEqual(lcb.report_failure(RuntimeError("HTTP 503")), "http_503")
            lcb.report_success()                          # 都不抛

    def test_set_service_name_names_the_mirror_key(self):
        lcb.set_service_name("probe_worker")
        self.assertEqual(lcb._service_name(), "probe_worker")
        snap = {"state": "OPEN", "service": "react_agent"}
        self.assertEqual(lcb._service_name(snap), "react_agent")   # 快照优先
        lcb.set_service_name("")


if __name__ == "__main__":
    unittest.main(verbosity=2)
