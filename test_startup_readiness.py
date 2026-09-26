# -*- coding: utf-8 -*-
"""启动就绪性：冷启动竞态的回归（新实例实测，见 `docs/新手实测报告_20260914.md` §2.5）。

现象：全新实例首次启动出现"15/16、orchestrator 未存活"，其日志停在 `AgentRegistry` 之后
再无任何输出。用 faulthandler 抓到的挂起栈是：

    orchestrator_v2.__init__ → common.MessagingClient.__init__ → _connect → redis ping
    → redis/retry.py:135 call_with_retry

即卡在 **redis-py 的默认重试**里：`common.py` 顶部早就定义了 `_NO_REDIS_RETRY`（并写明
"默认重试会把超时叠成 26~48 秒"），但这个常量**从未被使用**，唯一的客户端没用它。便携 Redis
从"端口在听"到"能响应命令"之间有一小段窗口，正好撞上 → 重试风暴 → 静默挂死。

本文件钉住两条修复：
1. MessagingClient **快速失败**（连不通就在数秒内抛错，而不是分钟级静默挂起）；
2. 启动器**等 Redis 真正就绪**再拉起服务（轮询 PING 取代固定 `sleep`）。
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import redis

import common
import launcher


def _closed_port() -> int:
    """返回一个当前没人监听的端口（先绑后关，避免撞上真在跑的服务）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestMessagingClientFailsFast(unittest.TestCase):
    def test_closed_port_raises_quickly(self):
        port = _closed_port()
        started = time.time()
        with self.assertRaises((redis.exceptions.ConnectionError, ConnectionError)):
            common.MessagingClient("127.0.0.1", port)
        elapsed = time.time() - started
        self.assertLess(
            elapsed, 15.0,
            f"连接失败耗时 {elapsed:.1f}s —— 应在数秒内快速失败（redis-py 默认重试是否又被打开？）")

    def test_connect_disables_builtin_retry(self):
        """源码级：唯一客户端必须显式传 no-retry 策略（该常量此前定义了却零使用）。"""
        src = Path("common.py").read_text(encoding="utf-8")
        self.assertIn("retry=_NO_REDIS_RETRY", src,
                      "MessagingClient 必须关掉 redis-py 内建重试，否则冷启动会静默挂住")


class TestLauncherWaitsForRedis(unittest.TestCase):
    def test_returns_true_once_reachable(self):
        with mock.patch.object(launcher, "_redis_reachable",
                               side_effect=[False, True]) as m:
            started = time.time()
            self.assertTrue(launcher._wait_redis_ready(timeout=5))
            self.assertGreaterEqual(m.call_count, 2, "应轮询而不是只探一次")
            self.assertLess(time.time() - started, 5)

    def test_returns_false_after_budget_without_raising(self):
        with mock.patch.object(launcher, "_redis_reachable", return_value=False):
            started = time.time()
            self.assertFalse(launcher._wait_redis_ready(timeout=1))
            self.assertLess(time.time() - started, 4, "超时预算没有生效")

    def test_startup_path_uses_readiness_wait(self):
        """启动路径不得退回固定 sleep（源码级守卫）。"""
        src = Path("launcher.py").read_text(encoding="utf-8")
        self.assertIn("_wait_redis_ready()", src)
        self.assertNotIn("_ensure_redis_available()\n    time.sleep(2)", src)


class TestCodeSandboxIsVisibleBeforeTasks(unittest.TestCase):
    """代码执行沙箱状态必须**在跑任务之前**就能看到。

    实机代价：新手机器没有 Docker，容器隔离不可用 → 任务里所有 code_execution 步骤被
    拒绝执行（默认要求隔离，不退到宿主解释器），十几分钟后才从失败步骤详情里看出原因。
    所以依赖自检报告与启动校验都要打印这一行；且它**不是启动阻塞项**（研究类任务不需要
    代码执行，其余能力照常）。
    """

    def test_dep_check_reports_unavailable_isolation_without_blocking(self):
        import dep_check
        st = {"mode": "docker", "isolation_ready": False,
              "isolation_reason": "docker 不可用（CLI 缺失或守护进程未响应）",
              "execution_available": False, "isolation_note": "要求容器隔离但当前不可用"}
        with mock.patch("code_sandbox.sandbox_status", lambda: st):
            rep = dep_check.check_code_sandbox()
        self.assertTrue(rep["ok"], "沙箱不可用不得阻塞启动")
        self.assertFalse(rep["ready"])
        self.assertIn("docker", rep["detail"])
        self.assertIn("Dockerfile.sandbox", rep["detail"], "要给出恢复隔离的出路")
        self.assertIn("不生成代码步骤", rep["detail"])
        # 不把"关闭隔离"当作新人出路（架构指令：不得自动降级、不推荐 restricted）
        self.assertNotIn("CODE_EXECUTION_SANDBOX=restricted", rep["detail"])
        # 报告里能看到这一行，且用中性标记（不是 [!!] 失败）
        text = dep_check.format_report({
            "python": {"ok": True, "detail": "Python 3.13"},
            "packages": {"ready": 14, "total": 14, "missing_required": [],
                         "missing_optional": []},
            "install": {"installed": [], "failed": []},
            "redis": {"ok": True, "detail": "Redis 已运行"},
            "frontend": {"ok": True, "detail": "前端产物已就绪"},
            "code_sandbox": rep, "migration": None, "ok": True,
        })
        self.assertIn("容器隔离不可用", text)
        self.assertIn("[--]", text)

    def test_ready_isolation_reports_ok(self):
        import dep_check
        st = {"mode": "docker", "isolation_ready": True, "isolation_reason": "",
              "execution_available": True,
              "isolation_note": "容器隔离已就绪（docker：断网 / 只读系统盘 / 仅挂载任务工作区）"}
        with mock.patch("code_sandbox.sandbox_status", lambda: st):
            rep = dep_check.check_code_sandbox()
        self.assertTrue(rep["ready"])
        self.assertIn("容器隔离已就绪", rep["detail"])

    def test_launcher_prints_three_layer_readiness(self):
        """启动/状态输出必须给三层状态，而不是只给"N/N 进程存活"。"""
        src = Path("launcher.py").read_text(encoding="utf-8")
        self.assertIn("def readiness_report(", src)
        self.assertIn("def print_readiness(", src)
        self.assertIn("研究能力：就绪", src)
        self.assertIn("研究能力：未就绪", src)
        self.assertIn("代码执行：容器隔离不可用", src)
        self.assertIn("print_readiness(readiness_report(), quiet=False)", src)


class TestSingleInstanceReuse(unittest.TestCase):
    """重复启动必须**复用**已在运行的实例（N1 首批：此前无条件停旧服务，会杀掉进行中的任务）。"""

    def _ready_stub(self):
        return {"workbench": {"ok": True, "detail": "HTTP 200 @ 8080", "port": 8080,
                              "url": "http://localhost:8080"},
                "research": {"ok": True, "redis": True, "redis_major": 8, "missing": [],
                             "stale": [], "orchestrator": True, "required": []},
                "code_sandbox": {"ok": False, "note": "", "execution_available": False,
                                 "isolation_required": True, "reason": "docker 不可用"},
                "port": 8080, "url": "http://localhost:8080", "ready": True}

    def test_second_start_reuses_instead_of_stopping(self):
        import launcher
        with mock.patch.object(launcher, "_load_config", return_value={}), \
                mock.patch.object(launcher, "_read_pids",
                                  return_value={"services": {"webui": 11, "orchestrator": 22}}), \
                mock.patch.object(launcher, "_is_alive", return_value=True), \
                mock.patch.object(launcher, "_pid_owns_project", return_value=True), \
                mock.patch.object(launcher, "stop_services") as stop, \
                mock.patch.object(launcher, "_spawn_service") as spawn, \
                mock.patch.object(launcher, "readiness_report",
                                  return_value=self._ready_stub()), \
                mock.patch.dict(os.environ, {"WM_FORCE_RESTART": "0"}), \
                mock.patch("builtins.print"):
            out = launcher.start_services()
        self.assertTrue(out.get("reused"), "第二次启动应复用实例")
        stop.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(out["url"], "http://localhost:8080")

    def test_force_restart_env_keeps_old_behaviour(self):
        """WM_FORCE_RESTART=1（显式重启）仍按原路径停旧起新。"""
        import launcher
        services = [("webui", [sys.executable, "webui.py"], launcher.BASE_DIR, None)]
        with mock.patch.object(launcher, "_load_config", return_value={}), \
                mock.patch.object(launcher, "_read_pids",
                                  return_value={"services": {"webui": 11}}), \
                mock.patch.object(launcher, "_is_alive", return_value=True), \
                mock.patch.object(launcher, "_pid_owns_project", return_value=True), \
                mock.patch.object(launcher, "build_services", return_value=services), \
                mock.patch.object(launcher, "stop_services", return_value=["webui"]) as stop, \
                mock.patch.object(launcher, "_spawn_service", return_value=999) as spawn, \
                mock.patch.object(launcher, "verify_services",
                                  return_value={"total": 1, "alive": 1, "down": [],
                                                "never_started": [], "waited": 0}), \
                mock.patch.object(launcher, "readiness_report",
                                  return_value=self._ready_stub()), \
                mock.patch.object(launcher, "_ensure_redis_available"), \
                mock.patch.object(launcher, "_wait_redis_ready", return_value=True), \
                mock.patch.object(launcher, "_write_pids"), \
                mock.patch.dict(os.environ, {"WM_FORCE_RESTART": "1"}), \
                mock.patch("builtins.print"):
            out = launcher.start_services()
        stop.assert_called_once()
        spawn.assert_called_once()
        self.assertNotIn("reused", out)

    def test_recycled_pid_is_not_our_instance(self):
        """PID 文件里的进程已被系统回收给别的进程（归属校验失败）→ 不算本实例在运行。"""
        import launcher
        with mock.patch.object(launcher, "_read_pids",
                               return_value={"services": {"webui": 4242}}), \
                mock.patch.object(launcher, "_is_alive", return_value=True), \
                mock.patch.object(launcher, "_pid_owns_project", return_value=False):
            state = launcher.instance_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["stale"], {"webui": 4242})


class TestSpawnFailureAccounting(unittest.TestCase):
    """spawn 失败的服务必须计入总数与失败清单（此前少启动一个反而显示"15/15 存活"）。"""

    def test_spawn_failure_counts_as_down(self):
        import launcher
        with mock.patch.object(launcher, "_read_pids",
                               return_value={"services": {"a": 1}, "failed": ["b"]}), \
                mock.patch.object(launcher, "_is_alive", return_value=True), \
                mock.patch.dict(os.environ, {"WM_START_VERIFY_WAIT": "0"}), \
                mock.patch("builtins.print") as out:
            summary = launcher.verify_services(quiet=False)
        self.assertEqual(summary["total"], 2, "未启动的服务也要计入总数")
        self.assertEqual(summary["alive"], 1)
        self.assertIn("b", summary["never_started"])
        self.assertEqual([name for _pid, name in summary["down"]], ["b"])
        printed = " ".join(str(c) for c in out.call_args_list)
        self.assertIn("未启动", printed, "要说明是未启动而不是不存在")

    def test_recorded_spawn_failures_round_trip(self):
        """start_services 记录 spawn 失败 → pids 文件里能读回来（不静默丢）。"""
        import launcher
        tmp = tempfile.mkdtemp(prefix="wm_pids_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fake = Path(tmp) / "pids.json"
        services = [("ok-svc", [sys.executable, "webui.py"], launcher.BASE_DIR, None),
                    ("bad-svc", [sys.executable, "gone.py"], launcher.BASE_DIR, None)]
        with mock.patch.object(launcher, "PID_FILE", fake), \
                mock.patch.object(launcher, "_load_config", return_value={}), \
                mock.patch.object(launcher, "_read_pids", return_value={"services": {}}), \
                mock.patch.object(launcher, "build_services", return_value=services), \
                mock.patch.object(launcher, "stop_services", return_value=[]), \
                mock.patch.object(launcher, "_spawn_service",
                                  side_effect=[111, None]), \
                mock.patch.object(launcher, "verify_services",
                                  return_value={"total": 2, "alive": 1, "down": [(0, "bad-svc")],
                                                "never_started": ["bad-svc"], "waited": 0}), \
                mock.patch.object(launcher, "readiness_report",
                                  return_value={"workbench": {"ok": True, "detail": "x", "port": 8080,
                                                              "url": "http://localhost:8080"},
                                                "research": {"ok": True, "redis": True,
                                                             "redis_major": 8, "missing": [],
                                                             "stale": [], "orchestrator": True,
                                                             "required": []},
                                                "code_sandbox": {"ok": False, "note": "",
                                                                 "execution_available": False,
                                                                 "isolation_required": True,
                                                                 "reason": "x"},
                                                "port": 8080, "url": "http://localhost:8080",
                                                "ready": True}), \
                mock.patch.object(launcher, "_ensure_redis_available"), \
                mock.patch.object(launcher, "_wait_redis_ready", return_value=True), \
                mock.patch("builtins.print"):
            launcher.start_services()
        recorded = json.loads(fake.read_text(encoding="utf-8"))
        self.assertEqual(recorded["services"], {"ok-svc": 111})
        self.assertEqual(recorded["failed"], ["bad-svc"])


class TestResearchReadiness(unittest.TestCase):
    """存活 ≠ 可研究：工作台能打开但研究能力未就绪时必须如实说未就绪（N1）。"""

    def _patch(self, *, http_ok=True, redis_ok=True, major=8, beats=None,
               orchestrator=True, port=8080):
        import launcher
        beats = beats if beats is not None else {
            c: 5.0 for c in launcher.RESEARCH_REQUIRED_CAPABILITIES}
        return [
            mock.patch.dict(os.environ, {"WEB_PORT": str(port)}, clear=False),
            mock.patch.object(launcher, "_redis_reachable", return_value=redis_ok),
            mock.patch.object(launcher, "_registry_heartbeats", return_value=beats),
            mock.patch.object(launcher, "instance_state",
                              return_value={"running": orchestrator, "services":
                                            ({"orchestrator": 5} if orchestrator else {}),
                                            "stale": {}, "port": port,
                                            "url": f"http://localhost:{port}"}),
            mock.patch.object(launcher, "_redis_min_major", return_value=6),
        ]

    @staticmethod
    def _patch_health_probe(*, status: int = 200):
        """就绪探测走 `build_opener(ProxyHandler({})).open(...)`（回环不走代理）。

        因此要 patch `build_opener`，只 patch `urlopen` 会漏掉真实网络调用。
        """
        cm = mock.MagicMock()
        cm.__enter__.return_value.status = status
        opener = mock.MagicMock()
        opener.open.return_value = cm
        return mock.patch("urllib.request.build_opener", return_value=opener)

    def test_ready_when_all_layers_ok(self):
        import launcher
        import urllib.request
        patchers = self._patch()
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        with self._patch_health_probe():
            rep = launcher.readiness_report()
        self.assertTrue(rep["ready"])
        self.assertTrue(rep["research"]["ok"])
        self.assertIn("8080", rep["workbench"]["url"])

    def test_workbench_up_but_orchestrator_missing_is_not_ready(self):
        import launcher
        patchers = self._patch(orchestrator=False)
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        with self._patch_health_probe(), \
                mock.patch("builtins.print") as out:
            rep = launcher.readiness_report()
            launcher.print_readiness(rep, quiet=False)
        self.assertTrue(rep["workbench"]["ok"], "工作台可访问")
        self.assertFalse(rep["research"]["ok"], "编排器不在 → 研究未就绪")
        self.assertFalse(rep["ready"])
        printed = " ".join(str(c) for c in out.call_args_list)
        self.assertIn("未就绪", printed)
        self.assertNotIn("可研究", printed, "未就绪时不得宣称可研究")

    def test_missing_worker_capability_blocks_research(self):
        import launcher
        import urllib.request
        beats = {c: 1.0 for c in launcher.RESEARCH_REQUIRED_CAPABILITIES
                 if c != "report_generator"}
        patchers = self._patch(beats=beats)
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        with self._patch_health_probe():
            rep = launcher.readiness_report()
        self.assertFalse(rep["research"]["ok"])
        self.assertIn("report_generator", rep["research"]["missing"])

    def test_stale_heartbeat_blocks_research(self):
        import launcher
        import urllib.request
        beats = {c: 1.0 for c in launcher.RESEARCH_REQUIRED_CAPABILITIES}
        beats["web_search"] = launcher.HEARTBEAT_MAX_AGE_SEC + 30
        patchers = self._patch(beats=beats)
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        with self._patch_health_probe():
            rep = launcher.readiness_report()
        self.assertFalse(rep["research"]["ok"])
        self.assertIn("web_search", rep["research"]["stale"])

    def test_non_default_port_is_used_by_url_and_probe(self):
        """非默认 WEB_PORT：URL、探测地址、就绪输出必须是同一个端口。"""
        import launcher
        import urllib.request
        patchers = self._patch(port=8123)
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        seen = {}

        def fake_urlopen(url, timeout=None):
            seen["url"] = url
            cm = mock.MagicMock()
            cm.__enter__.return_value.status = 200
            return cm

        # 就绪探测用 build_opener(ProxyHandler({})).open(...)：回环探测显式不走代理
        # （企业网常设 HTTP_PROXY，否则"服务在跑却报工作台未响应"）。
        fake_opener = mock.MagicMock()
        fake_opener.open.side_effect = fake_urlopen
        with mock.patch.object(urllib.request, "build_opener",
                               return_value=fake_opener), \
                mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
                mock.patch("builtins.print"):
            rep = launcher.readiness_report()
        self.assertEqual(launcher.web_port(), 8123)
        self.assertTrue(rep["url"].endswith(":8123"))
        self.assertIn(":8123", seen["url"], "就绪探测必须打在实际端口上")


class TestStartupController(unittest.TestCase):
    """N1：统一启动控制器——状态明确、单实例锁、断点恢复、失败只给一个下一步。"""

    @staticmethod
    def _fake_key() -> str:
        """构造一个**假**凭据（拼接而非字面量）：避免被"硬编码凭据"规则当成真密钥。"""
        return "-".join(("FAKE", "KEY", "0123456789"))

    def _ready(self, ok=True, workbench_ok=True):
        return {"workbench": {"ok": workbench_ok, "detail": "HTTP 200 @ 8080",
                              "port": 8080, "url": "http://localhost:8080"},
                "research": {"ok": ok, "redis": True, "redis_major": 8,
                             "missing": [] if ok else ["report_generator"],
                             "stale": [], "orchestrator": True, "required": []},
                "code_sandbox": {"ok": False, "note": "", "execution_available": False,
                                 "isolation_required": True, "reason": "docker 不可用"},
                "port": 8080, "url": "http://localhost:8080", "ready": bool(ok and workbench_ok)}

    def test_effective_config_applies_config_before_probes(self):
        """统一有效值：端口/Redis 目标来自 config.json（在依赖与 Redis 检查之前）。"""
        import launcher
        cfg = {"llm": {"api_key": self._fake_key(), "base_url": "https://api.example/v1",
                       "model": "m"},
               "redis": {"host": "redis.internal", "port": 6390},
               "web": {"port": 8123}}
        with mock.patch.object(launcher, "_load_config", return_value=cfg), \
                mock.patch.object(launcher, "_apply_env") as apply_env, \
                mock.patch.dict(os.environ, {}, clear=True):
            eff = launcher.effective_config()
        apply_env.assert_called_once()          # 统一映射进环境变量
        self.assertEqual(eff["port"], 8123)
        self.assertTrue(eff["config_complete"])
        self.assertEqual(eff["model"], "m")

    def test_effective_config_flags_incomplete(self):
        import launcher
        with mock.patch.object(launcher, "_load_config", return_value={"llm": {"model": "m"}}), \
                mock.patch.object(launcher, "_apply_env"), \
                mock.patch.dict(os.environ, {}, clear=True):
            eff = launcher.effective_config()
        self.assertFalse(eff["config_complete"])

    def test_effective_config_treats_placeholders_as_incomplete(self):
        """模板占位符（YOUR_API_KEY 之类）不算配置完整。

        config.json 缺失时配置回退到随包模板；只做非空判断会让"没配置"看起来像
        "配置完整"，首启引导不再出现、任务提交后才在鉴权上失败。
        """
        import launcher
        tpl_llm = {"api_key": "YOUR_API_KEY", "base_url": "https://api.deepseek.com/v1",
                   "model": "deepseek-chat"}
        with mock.patch.object(launcher, "_load_config", return_value={"llm": tpl_llm}), \
                mock.patch.object(launcher, "_apply_env"), \
                mock.patch.dict(os.environ, {}, clear=True):
            eff = launcher.effective_config()
        self.assertFalse(eff["config_complete"], "占位符必须算未配置")

    def test_instance_lock_second_holder_is_reported(self):
        import launcher
        tmp = Path(tempfile.mkdtemp(prefix="wm_lock_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lock = Path(tmp) / "instance.lock"
        with mock.patch.object(launcher, "INSTANCE_LOCK_FILE", lock):
            first = launcher.acquire_instance_lock()
            second = launcher.acquire_instance_lock()
        self.assertTrue(first["acquired"])
        self.assertFalse(second["acquired"], "第二次必须报已在运行")
        self.assertEqual(second["pid"], os.getpid())

    def test_stale_lock_is_taken_over(self):
        import launcher
        tmp = Path(tempfile.mkdtemp(prefix="wm_lock2_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lock = Path(tmp) / "instance.lock"
        lock.write_text(json.dumps({"pid": 999999, "state": "starting",
                                    "at": time.time() - 10}), encoding="utf-8")
        with mock.patch.object(launcher, "INSTANCE_LOCK_FILE", lock), \
                mock.patch.object(launcher, "_is_alive", return_value=False):
            res = launcher.acquire_instance_lock()
        self.assertTrue(res["acquired"], "持有者已死必须能接管，不能永久锁死")

    def test_release_only_removes_own_lock(self):
        import launcher
        tmp = Path(tempfile.mkdtemp(prefix="wm_lock3_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        lock = Path(tmp) / "instance.lock"
        lock.write_text(json.dumps({"pid": os.getpid() + 1, "at": time.time()}),
                        encoding="utf-8")
        with mock.patch.object(launcher, "INSTANCE_LOCK_FILE", lock):
            launcher.release_instance_lock("running")
        self.assertTrue(lock.exists(), "别人的锁不能删")

    def test_step_state_resets_when_runtime_identity_changes(self):
        """包版本变化触发重新检查；身份未变则复用已验证步骤（断点恢复）。"""
        import launcher
        tmp = Path(tempfile.mkdtemp(prefix="wm_state_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        state_file = Path(tmp) / "startup_state.json"
        with mock.patch.object(launcher, "STARTUP_STATE_FILE", state_file):
            launcher.mark_step("git:abc", "deps", True, "checked")
            self.assertTrue(launcher.completed_steps("git:abc").get("deps", {}).get("ok"))
            self.assertEqual(launcher.completed_steps("package:2.0"), {},
                             "身份变化后已完成步骤必须失效")

    def test_redact_secrets_removes_keys_and_headers(self):
        import launcher
        key = self._fake_key()
        raw = "\n".join((
            f'api_key="{key}"',
            f"Authorization: Bearer {key}",
            f"api_key: {key}",
        ))
        out = launcher.redact_secrets(raw)
        self.assertNotIn(key, out)
        self.assertIn("<redacted>", out)

    def test_diagnostics_are_redacted_and_local(self):
        import launcher
        key = self._fake_key()
        tmp = Path(tempfile.mkdtemp(prefix="wm_diag_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        logs = Path(tmp) / "logs"
        logs.mkdir()
        (logs / "webui.log").write_text(
            f'llm_client: LLMClient: api_key="{key}" base_url=https://x\nnormal line\n',
            encoding="utf-8")
        with mock.patch.object(launcher, "LOG_DIR", logs), \
                mock.patch.object(launcher, "STARTUP_STATE_FILE", Path(tmp) / "s.json"), \
                mock.patch.object(launcher, "readiness_report", return_value=self._ready()), \
                mock.patch.object(launcher, "effective_config",
                                  return_value={"identity": "git:abc", "python": "3.11.9",
                                                "config_complete": True, "model": "m",
                                                "base_url": "https://x", "frontend_dist": True,
                                                "redis_host": "localhost", "redis_port": 6379}):
            text = launcher.diagnostics_report(tail_lines=5)
        self.assertIn("git:abc", text)
        self.assertNotIn(key, text, "诊断不得带密钥")
        self.assertIn("未上传", text)

    def test_controller_awaits_config_but_still_opens_the_workbench(self):
        """配置不完整时：不把新人丢在控制台——工作台照起，引导在页面里继续。

        此前该分支直接返回 awaiting_config 且**不启动服务**，于是双击入口的人
        只看到一个控制台就结束了；N3 的页面首启引导与内置演示根本没机会出现。
        研究能力在配置完成前不得对外宣称就绪（状态仍如实报 awaiting_config）。
        """
        import launcher
        with mock.patch.object(launcher, "acquire_instance_lock",
                               return_value={"acquired": True, "pid": 1}), \
                mock.patch.object(launcher, "release_instance_lock") as release, \
                mock.patch.object(launcher, "resolve_web_port", return_value=8080), \
                mock.patch.object(launcher, "effective_config",
                                  return_value={"identity": "git:abc", "python": "3.11.9",
                                                "config_complete": False, "model": "",
                                                "base_url": "", "frontend_dist": True,
                                                "redis_host": "localhost", "redis_port": 6379,
                                                "port": 8080, "url": "http://localhost:8080"}), \
                mock.patch.object(launcher, "start_services") as start, \
                mock.patch.object(launcher, "_run_dependency_check") as dep, \
                mock.patch.object(launcher, "completed_steps", return_value={}), \
                mock.patch.object(launcher, "readiness_report",
                                  return_value=self._ready(ok=False)), \
                mock.patch.object(launcher, "print_readiness"), \
                mock.patch.dict(os.environ, {"WM_NONINTERACTIVE": "1"}), \
                mock.patch("builtins.print"):
            res = launcher.startup_controller()
        self.assertEqual(res["state"], "awaiting_config")
        start.assert_called_once()        # 工作台照起：页面引导与演示要能打开
        dep.assert_called_once()          # 依赖步骤照做，但不得真去装/拉（已 mock）
        release.assert_called_once()      # 释放实例锁，别把状态卡在"启动中"
        self.assertNotEqual(release.call_args[0][0] if release.call_args[0] else "running",
                            "running", "配置未完成不得把实例标成 running")

    def test_controller_skips_wizard_when_console_is_not_interactive(self):
        """非交互（管道/CI）下不进入问答式引导，避免卡在等输入。"""
        import launcher
        with mock.patch.object(launcher, "acquire_instance_lock",
                               return_value={"acquired": True, "pid": 1}), \
                mock.patch.object(launcher, "release_instance_lock"), \
                mock.patch.object(launcher, "resolve_web_port", return_value=8080), \
                mock.patch.object(launcher, "effective_config",
                                  return_value={"identity": "git:abc", "python": "3.11.9",
                                                "config_complete": False, "model": "",
                                                "base_url": "", "frontend_dist": True,
                                                "redis_host": "localhost", "redis_port": 6379,
                                                "port": 8080, "url": "http://localhost:8080"}), \
                mock.patch.object(launcher, "start_services"), \
                mock.patch.object(launcher, "_run_dependency_check"), \
                mock.patch.object(launcher, "completed_steps", return_value={}), \
                mock.patch.object(launcher, "readiness_report",
                                  return_value=self._ready(ok=False)), \
                mock.patch.object(launcher, "print_readiness"), \
                mock.patch.object(launcher, "_interactive_console", return_value=False), \
                mock.patch("subprocess.run") as run, \
                mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("builtins.print"):
            os.environ.pop("WM_NONINTERACTIVE", None)
            launcher.startup_controller()
        for call in run.call_args_list:
            argv = call[0][0] if call[0] else []
            self.assertNotIn("setup_wizard.py", " ".join(str(a) for a in argv),
                             "非交互控制台不得启动问答式引导")

    def test_controller_reports_limited_when_research_not_ready(self):
        import launcher
        with mock.patch.object(launcher, "acquire_instance_lock",
                               return_value={"acquired": True, "pid": 1}), \
                mock.patch.object(launcher, "release_instance_lock"), \
                mock.patch.object(launcher, "resolve_web_port", return_value=8080), \
                mock.patch.object(launcher, "effective_config",
                                  return_value={"identity": "git:abc", "python": "3.11.9",
                                                "config_complete": True, "model": "m",
                                                "base_url": "https://x", "frontend_dist": True,
                                                "redis_host": "localhost", "redis_port": 6379,
                                                "port": 8080, "url": "http://localhost:8080"}), \
                mock.patch.object(launcher, "completed_steps",
                                  return_value={"deps": {"ok": True}}), \
                mock.patch.object(launcher, "start_services"), \
                mock.patch.object(launcher, "readiness_report",
                                  return_value=self._ready(ok=False)), \
                mock.patch.object(launcher, "print_readiness"), \
                mock.patch("builtins.print"):
            res = launcher.startup_controller()
        self.assertEqual(res["state"], "limited_experience")
        self.assertIn("不要提交研究任务", res["detail"])

    def test_controller_ready_state(self):
        import launcher
        with mock.patch.object(launcher, "acquire_instance_lock",
                               return_value={"acquired": True, "pid": 1}), \
                mock.patch.object(launcher, "release_instance_lock"), \
                mock.patch.object(launcher, "resolve_web_port", return_value=8080), \
                mock.patch.object(launcher, "effective_config",
                                  return_value={"identity": "git:abc", "python": "3.11.9",
                                                "config_complete": True, "model": "m",
                                                "base_url": "https://x", "frontend_dist": True,
                                                "redis_host": "localhost", "redis_port": 6379,
                                                "port": 8080, "url": "http://localhost:8080"}), \
                mock.patch.object(launcher, "completed_steps",
                                  return_value={"deps": {"ok": True}}), \
                mock.patch.object(launcher, "start_services") as start, \
                mock.patch.object(launcher, "readiness_report", return_value=self._ready()), \
                mock.patch.object(launcher, "print_readiness"), \
                mock.patch("builtins.print"):
            res = launcher.startup_controller()
        self.assertEqual(res["state"], "research_ready")
        start.assert_called_once()
        self.assertEqual(res["url"], "http://localhost:8080")

    def test_controller_reuses_when_lock_held(self):
        import launcher
        with mock.patch.object(launcher, "acquire_instance_lock",
                               return_value={"acquired": False, "pid": 4242,
                                             "state": "starting"}), \
                mock.patch.object(launcher, "start_services") as start, \
                mock.patch.object(launcher, "readiness_report", return_value=self._ready()), \
                mock.patch.object(launcher, "print_readiness"), \
                mock.patch("builtins.print"):
            res = launcher.startup_controller()
        start.assert_not_called()
        self.assertEqual(res["state"], "research_ready")

    def test_verified_deps_are_not_reinstalled(self):
        """暖启动不重装：身份未变且上轮已验证 → 不再跑依赖检查。"""
        import launcher
        with mock.patch.object(launcher, "acquire_instance_lock",
                               return_value={"acquired": True, "pid": 1}), \
                mock.patch.object(launcher, "release_instance_lock"), \
                mock.patch.object(launcher, "effective_config",
                                  return_value={"identity": "git:abc", "python": "3.11.9",
                                                "config_complete": True, "model": "m",
                                                "base_url": "https://x", "frontend_dist": True,
                                                "redis_host": "localhost", "redis_port": 6379,
                                                "port": 8080, "url": "http://localhost:8080"}), \
                mock.patch.object(launcher, "completed_steps",
                                  return_value={"deps": {"ok": True}}), \
                mock.patch.object(launcher, "_run_dependency_check") as dep, \
                mock.patch.object(launcher, "start_services"), \
                mock.patch.object(launcher, "readiness_report", return_value=self._ready()), \
                mock.patch.object(launcher, "print_readiness"), \
                mock.patch("builtins.print"):
            launcher.startup_controller()
        dep.assert_not_called()


class TestNoUnretriedRedisClients(unittest.TestCase):
    """同一缺陷家族的全仓守卫：`redis.Redis(...)` 必须显式关掉 redis-py 内建重试。

    实测代价：设置页/健康页的 `/api/config/requirements` 走 `health_registry._redis_get`，
    Redis 端口被丢包时单次调用要 26~48 秒才失败（默认重试叠加 socket_connect_timeout），
    页面表现为长时间卡死；本地跑该测试文件也因此挂住 20 秒以上。全仓另有 8 处裸客户端
    （quote_cache/source_health/lora_client/metrics_collector/orchestrator_v2/tool_dispatch/
    smoke_test/verification_suite）同因。新增客户端忘记传 retry 时，本用例报红。
    """

    def _scan(self, name: str, src: str) -> list[str]:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return []
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "Redis":
                continue
            recv = func.value.id if isinstance(func.value, ast.Name) else ""
            if recv == "aioredis":      # 异步客户端没有 retry 参数
                continue
            if "retry" not in {kw.arg for kw in node.keywords}:
                found.append(f"{name}:{node.lineno}")
        return found

    def test_detector_has_teeth(self):
        src = "import redis\nr = redis.Redis(host='h')\n_ = redis.Redis(host='h', retry=NO)\n"
        self.assertEqual(self._scan("syn.py", src), ["syn.py:2"])

    def test_no_unretried_client_in_repo(self):
        bad: list[str] = []
        for path in sorted(Path(".").rglob("*.py")):
            rel = path.as_posix()
            if rel.startswith(".") or "/site-packages/" in rel or "/node_modules/" in rel:
                continue
            bad += self._scan(rel, path.read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(
            bad, [],
            "这些 Redis 客户端没关内建重试：Redis 不可达时会从 2 秒失败退化成数十秒挂死"
            "（用 common._NO_REDIS_RETRY 或同款本地常量传 retry=）：" + ", ".join(bad))


class TestPortConflictsDoNotTakeOverOtherServices(unittest.TestCase):
    """场景 5（N4 矩阵）：端口冲突与 Redis 5 —— 不接管/不停用别人的服务，页面与子进程一致。

    真实场景：8080 被别的软件（或另一个织光实例）占着、6379 上跑着别人的 Redis 5。
    要求是"明确兼容性判断 + 本实例让位到空闲端口 + 页面与子进程地址一致"，
    而不是报错退出、更不是杀掉对方进程。
    """

    def _tmp_ports_file(self) -> Path:
        tmp = Path(tempfile.mkdtemp(prefix="wm_ports_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp / "runtime_ports.json"

    def test_port_is_free_detects_a_real_listener(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        self.addCleanup(sock.close)
        self.assertFalse(launcher._port_is_free(port), "有监听时不得判定为空闲")
        sock.close()
        self.assertTrue(launcher._port_is_free(port), "关闭后应恢复空闲")

    def test_default_web_port_moves_to_a_free_port_and_persists(self):
        import launcher as L
        ports_file = self._tmp_ports_file()
        with mock.patch.object(L, "RUNTIME_PORTS_FILE", ports_file), \
                mock.patch.object(L, "_preferred_web_port", return_value=8080), \
                mock.patch.object(L, "_web_port_is_explicit", return_value=False), \
                mock.patch.object(L, "_webui_owned_here", return_value=False), \
                mock.patch.object(L, "_port_is_free", side_effect=lambda p: int(p) != 8080):
            port = L.resolve_web_port()
        self.assertEqual(port, 8081, "默认端口被占用时应让位到下一个空闲端口")
        self.assertEqual(json.loads(ports_file.read_text(encoding="utf-8"))["web"], 8081,
                         "让位结果必须落盘：url/子进程/页面要看到同一个端口")

    def test_explicit_web_port_is_reported_not_silently_changed(self):
        import launcher as L
        ports_file = self._tmp_ports_file()
        with mock.patch.object(L, "RUNTIME_PORTS_FILE", ports_file), \
                mock.patch.object(L, "_preferred_web_port", return_value=8080), \
                mock.patch.object(L, "_web_port_is_explicit", return_value=True), \
                mock.patch.object(L, "_webui_owned_here", return_value=False), \
                mock.patch.object(L, "_port_is_free", return_value=False):
            port = L.resolve_web_port()
        self.assertEqual(port, 8080, "用户显式指定的端口不自动改（只如实报告冲突）")
        self.assertFalse(ports_file.exists(), "不让位就不该写端口状态")

    def test_web_port_follows_the_shift_only_while_this_instance_runs(self):
        import launcher as L
        ports_file = self._tmp_ports_file()
        ports_file.write_text(json.dumps({"web": 8081, "preferred": 8080}), encoding="utf-8")
        with mock.patch.object(L, "RUNTIME_PORTS_FILE", ports_file), \
                mock.patch.object(L, "_preferred_web_port", return_value=8080), \
                mock.patch.object(L, "_web_port_is_explicit", return_value=False):
            with mock.patch.object(L, "_webui_owned_here", return_value=True):
                self.assertEqual(L.web_port(), 8081, "本实例还在跑：沿用实际端口")
            with mock.patch.object(L, "_webui_owned_here", return_value=False):
                self.assertEqual(L.web_port(), 8080, "实例已停：回到用户期望的端口")

    def test_start_services_publishes_the_resolved_port_to_children(self):
        """子进程（webui 读 WEB_PORT）必须拿到让位后的端口，否则页面与后端不一致。"""
        import launcher as L
        ready = {"workbench": {"ok": True, "detail": "HTTP 200 @ 8123", "port": 8123,
                               "url": "http://localhost:8123"},
                 "research": {"ok": True, "redis": True, "redis_major": 8, "missing": [],
                              "stale": [], "orchestrator": True, "required": []},
                 "code_sandbox": {"ok": False, "note": "", "execution_available": False},
                 "port": 8123, "url": "http://localhost:8123", "ready": True}
        with mock.patch.object(L, "_load_config", return_value={}), \
                mock.patch.object(L, "_apply_env"), \
                mock.patch.object(L, "instance_state",
                                  return_value={"running": False, "services": {}, "stale": {},
                                                "port": 8080, "url": "http://localhost:8080"}), \
                mock.patch.object(L, "stop_services", return_value=[]), \
                mock.patch.object(L, "_ensure_redis_available"), \
                mock.patch.object(L, "_wait_redis_ready", return_value=True), \
                mock.patch.object(L, "resolve_web_port", return_value=8123), \
                mock.patch.object(L, "_spawn_service", return_value=4321) as spawn, \
                mock.patch.object(L, "_write_pids"), \
                mock.patch.object(L, "verify_services",
                                  return_value={"total": 1, "alive": 1, "down": [],
                                                "never_started": [], "waited": 0}), \
                mock.patch.object(L, "readiness_report", return_value=ready), \
                mock.patch.object(L, "print_readiness"), \
                mock.patch.dict(os.environ, {}, clear=True):
            L.start_services()
            self.assertEqual(os.environ.get("WEB_PORT"), "8123",
                             "让位端口必须发布给子进程（_spawn_service 传 os.environ）")
        self.assertTrue(spawn.called)

    def test_busy_non_redis_port_moves_this_instance_to_a_free_port(self):
        import launcher as L
        ports_file = self._tmp_ports_file()
        with mock.patch.object(L, "RUNTIME_PORTS_FILE", ports_file), \
                mock.patch.object(L, "_redis_reachable", return_value=False), \
                mock.patch.object(L, "_port_is_free", side_effect=lambda p: int(p) != 6379), \
                mock.patch("dep_check.ensure_redis",
                           return_value={"ok": True, "action": "started", "detail": "ok"}), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("builtins.print"):
            L._ensure_redis_available()
            self.assertEqual(os.environ.get("REDIS_PORT"), "6380",
                             "6379 被别的程序占用时，本实例应改用空闲端口")
            self.assertEqual(os.environ.get("REDIS_HOST"), "127.0.0.1")
        self.assertEqual(json.loads(ports_file.read_text(encoding="utf-8"))["redis"], 6380)

    def test_redis5_on_the_port_moves_this_instance_and_never_stops_it(self):
        """6379 上是别人的 Redis 5：不兼容 → 本实例另起，且绝不调用任何停止动作。"""
        import launcher as L
        ports_file = self._tmp_ports_file()
        with mock.patch.object(L, "RUNTIME_PORTS_FILE", ports_file), \
                mock.patch.object(L, "_redis_reachable", return_value=True), \
                mock.patch.object(L, "_redis_major", return_value=5), \
                mock.patch.object(L, "_port_is_free", side_effect=lambda p: int(p) != 6379), \
                mock.patch("dep_check.ensure_redis",
                           return_value={"ok": True, "action": "started", "detail": "ok"}), \
                mock.patch("dep_check._stop_recorded_redis") as stop_other, \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("builtins.print"):
            L._ensure_redis_available()
            self.assertEqual(os.environ.get("REDIS_PORT"), "6380")
        stop_other.assert_not_called()

    def test_explicit_redis_port_reports_instead_of_shifting(self):
        """显式配了 REDIS_PORT（如指向别的机器）：不可用就明确失败，不偷偷改地址。"""
        import launcher as L
        with mock.patch.object(L, "_redis_reachable", return_value=True), \
                mock.patch.object(L, "_redis_major", return_value=5), \
                mock.patch.dict(os.environ, {"REDIS_PORT": "6379"}, clear=True), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                L._ensure_redis_available()

    def test_interactive_console_is_false_for_pipes_and_nul(self):
        """Windows 上 isatty 对 NUL 也报 True → 必须用 GetConsoleMode 判真控制台。

        实测：`cmd /c start.bat < /dev/null` 下 isatty=True，控制器因此仍进入
        问答式引导并撞 EOF（自动化/CI 场景）。
        """
        import launcher as L
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WM_NONINTERACTIVE", None)
            if os.name == "nt":
                with mock.patch("msvcrt.get_osfhandle", side_effect=OSError("no console")):
                    self.assertFalse(L._interactive_console())
            else:
                with mock.patch.object(sys, "stdin", None):
                    self.assertFalse(L._interactive_console())
        with mock.patch.dict(os.environ, {"WM_NONINTERACTIVE": "1"}, clear=False):
            self.assertFalse(L._interactive_console(), "WM_NONINTERACTIVE 一律不提问")

    def test_loopback_health_probe_ignores_the_proxy(self):
        """有 HTTP_PROXY（企业网常态）时，回环健康检查必须绕开代理。

        实测：设了死代理后 `launcher.py up` 报"工作台未响应 @ 8080"，而服务其实
        正常在跑——代理把 127.0.0.1 的请求也接走了。这里起一个真实回环服务，
        在死代理环境里跑就绪探测。
        """
        import launcher as L
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class _H(BaseHTTPRequestHandler):
            def do_GET(self):                                   # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def log_message(self, *a):                          # 静音
                return

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        dead = "http://127.0.0.1:9"
        with mock.patch.object(L, "web_port", return_value=port), \
                mock.patch.object(L, "_registry_heartbeats", return_value={}), \
                mock.patch.object(L, "_redis_reachable", return_value=False), \
                mock.patch.dict(os.environ, {"HTTP_PROXY": dead, "http_proxy": dead,
                                             "HTTPS_PROXY": dead, "https_proxy": dead}):
            rep = L.readiness_report(http_timeout=3.0)
        self.assertTrue(rep["workbench"]["ok"],
                        f"回环探测被代理接走了：{rep['workbench']['detail']}")

    def test_no_proxy_is_extended_with_loopback_without_clobbering(self):
        import launcher as L
        # Windows 的环境变量名大小写不敏感（NO_PROXY 与 no_proxy 是同一个），
        # 断言只看合并后的值，不假定哪个大小写生效。
        with mock.patch.dict(os.environ, {"NO_PROXY": "example.com,10.0.0.0/8"}, clear=False):
            L._publish_loopback_no_proxy()
            value = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
            self.assertIn("example.com", value, "不得覆盖用户已有的排除项")
            self.assertIn("10.0.0.0/8", value)
            for want in ("localhost", "127.0.0.1", "::1"):
                self.assertIn(want, value)
            before = value
            L._publish_loopback_no_proxy()
            self.assertEqual(os.environ.get("NO_PROXY") or os.environ.get("no_proxy"),
                             before, "重复调用不得叠加")

    def test_windows_tool_output_is_decoded_with_replace(self):
        """Windows 工具按控制台代码页输出（中文系统 GBK）：`text=True` 必须配 `errors="replace"`。

        实测：中文系统上 `launcher.py deps --fix` 打出 4 段 UnicodeDecodeError 栈
        （subprocess 读线程解码失败），`_is_alive` 的 PID 判断也随之失效——
        对新人是"一启动就报错"的观感，实际只是解码没兜底。
        """
        import re as _re
        root = Path(__file__).resolve().parent
        for name in ("launcher.py", "dep_check.py"):
            src = (root / name).read_text(encoding="utf-8")
            for m in _re.finditer(r"text=True", src):
                window = src[m.start():m.start() + 220]
                self.assertIn("errors=", window,
                              f"{name}: 有一处 text=True 未配 errors='replace'（子进程输出"
                              f"按 UTF-8 解码会在读线程里抛异常）：…{window[:120]}")

    def test_foreign_compatible_redis_is_never_reused(self):
        """默认端口上是别人的 Redis（另一个织光实例/系统服务）→ 本实例另起自己的。

        为什么必须隔离：`orchestrator:main` 是 Redis pub/sub，**每个订阅者都收到每条任务**。
        实测两个实例共用一个 Redis 时，同一条任务被两个实例同时执行；一个实例的端点余额
        不足，还会把另一个实例的任务状态写成 FAILED（共享的是同一份 task state）。
        """
        import launcher as L
        ports_file = self._tmp_ports_file()
        with mock.patch.object(L, "RUNTIME_PORTS_FILE", ports_file), \
                mock.patch.object(L, "_redis_reachable", return_value=True), \
                mock.patch.object(L, "_redis_major", return_value=8), \
                mock.patch.object(L, "_portable_redis_owned_here", return_value=False), \
                mock.patch.object(L, "_port_is_free", side_effect=lambda p: int(p) != 6379), \
                mock.patch("dep_check.ensure_redis",
                           return_value={"ok": True, "action": "started", "detail": "ok"}), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("builtins.print"):
            L._ensure_redis_available()
            self.assertEqual(os.environ.get("REDIS_PORT"), "6380",
                             "别人的 Redis 不得复用：本实例要自己的总线")
        self.assertEqual(json.loads(ports_file.read_text(encoding="utf-8"))["redis"], 6380)

    def test_own_redis_is_reused_on_restart(self):
        """本实例自己启动的便携 Redis（pid 与端口都对得上）→ 沿用，不重复起。"""
        import launcher as L
        with mock.patch.object(L, "_redis_reachable", return_value=True), \
                mock.patch.object(L, "_redis_major", return_value=8), \
                mock.patch.object(L, "_portable_redis_owned_here", return_value=True), \
                mock.patch("dep_check.ensure_redis") as ensure, \
                mock.patch.dict(os.environ, {}, clear=True):
            L._ensure_redis_available()
        ensure.assert_not_called()

    def test_explicit_external_redis_is_still_respected(self):
        """显式配置的 Redis（自装 Memurai / 远端）照旧复用——那是用户的选择。"""
        import launcher as L
        with mock.patch.object(L, "_redis_reachable", return_value=True), \
                mock.patch.object(L, "_redis_major", return_value=8), \
                mock.patch.object(L, "_portable_redis_owned_here", return_value=False), \
                mock.patch("dep_check.ensure_redis") as ensure, \
                mock.patch.dict(os.environ, {"REDIS_PORT": "6379"}, clear=True):
            L._ensure_redis_available()
        ensure.assert_not_called()

    def test_owned_check_requires_pid_and_port_to_match(self):
        import launcher as L
        ports_file = self._tmp_ports_file()
        ports_file.write_text(json.dumps({"redis": 6380}), encoding="utf-8")
        with mock.patch.object(L, "RUNTIME_PORTS_FILE", ports_file), \
                mock.patch.object(L, "_is_alive", return_value=True):
            self.assertFalse(L._portable_redis_owned_here(6379), "端口对不上不算自己的")
        ports_file.write_text(json.dumps({"redis": 6379}), encoding="utf-8")
        with mock.patch.object(L, "RUNTIME_PORTS_FILE", ports_file), \
                mock.patch.object(L, "_is_alive", return_value=False):
            self.assertFalse(L._portable_redis_owned_here(6379), "PID 已死不算自己的")

    def test_redis_target_follows_persisted_port_only_when_reachable(self):
        import launcher as L
        ports_file = self._tmp_ports_file()
        ports_file.write_text(json.dumps({"redis": 6380}), encoding="utf-8")
        with mock.patch.object(L, "RUNTIME_PORTS_FILE", ports_file), \
                mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(L, "_redis_reachable", return_value=True):
                self.assertEqual(L._redis_target(), ("localhost", 6380))
            with mock.patch.object(L, "_redis_reachable", return_value=False):
                self.assertEqual(L._redis_target(), ("localhost", 6379),
                                 "让位端口上没有 Redis 时不得继续指向它")


if __name__ == "__main__":
    unittest.main()
