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


_TEST_PORTS_DIR = None
_MODULE_PATCHERS: list = []


def setUpModule():
    """把**运行期端口记录**重定向到临时文件：测试不得改写真实运行状态。

    实测缺陷：有些用例（`start_services` 的假进程路径）没有把让位记录隔离掉，而本机
    8080 上有真在跑的工作台 → 用例真的把 `.weavemind/runtime_ports.json` 写成
    `{"web": 8081, "preferred": 8080}`。之后 `launcher.py status` 就打出自相矛盾的
    读数（`URL: …:8081` 且"工作台未响应"），而实例其实在 8080 上好好服务——
    这正是"探活端口 8081 与实际 8080 不一致"的来源之一。
    """
    global _TEST_PORTS_DIR
    _TEST_PORTS_DIR = tempfile.mkdtemp(prefix="wm_runtime_ports_")
    patcher = mock.patch.object(launcher, "RUNTIME_PORTS_FILE",
                                Path(_TEST_PORTS_DIR) / "runtime_ports.json")
    patcher.start()
    _MODULE_PATCHERS.append(patcher)


def tearDownModule():
    for p in _MODULE_PATCHERS:
        p.stop()
    _MODULE_PATCHERS.clear()
    if _TEST_PORTS_DIR:
        shutil.rmtree(_TEST_PORTS_DIR, ignore_errors=True)


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
        self.assertIn("print_readiness(rep, quiet=False)", src)
        # URL 与探活必须来自同一次读取：此前两处各自解析端口，端口记录过期时会打出
        # "URL: …:8081" 而探活写 "@ 8080" 的自相矛盾读数（架构师列为小修）。
        self.assertIn("URL: {rep['url']}", src)


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


class TestStatusPortReadingIsSelfConsistent(unittest.TestCase):
    """`launcher.py status` 的 URL 与探活端口必须一致。

    背景（架构师列的小修）：让位过端口的实例上，`status` 曾同时打出两个端口——
    `URL: http://localhost:8081` 而探活写 `HTTP 200 @ 8080`。根因是 URL 与探活各自
    解析一次端口；当 `.weavemind/runtime_ports.json` 的记录与真正在响应的实例不一致时，
    两次解析可以给出不同答案。修法：一次读取解析端口，并把**真正响应的端口**当事实。
    """

    def _patch(self, *, answers, recorded=8081, preferred=8080, owned=True):
        import launcher as L
        probed: list = []

        def fake_probe(port, timeout):
            probed.append(int(port))
            if int(port) in answers:
                return (True, f"HTTP 200 @ {port}", 200)
            return (False, f"未响应（ConnectionRefusedError）@ {port}", 0)

        patchers = [
            mock.patch.object(L, "web_port", return_value=recorded),
            mock.patch.object(L, "web_port_candidates", return_value=[recorded, preferred]),
            mock.patch.object(L, "_probe_workbench", side_effect=fake_probe),
            mock.patch.object(L, "_webui_owned_here", return_value=owned),
            mock.patch.object(L, "_registry_heartbeats", return_value={}),
            mock.patch.object(L, "_redis_reachable", return_value=False),
            mock.patch.object(L, "instance_state",
                              return_value={"running": False, "services": {}, "stale": {},
                                            "port": recorded,
                                            "url": f"http://localhost:{recorded}"}),
        ]
        return patchers, probed

    def test_the_answering_port_wins_and_the_mismatch_is_stated(self):
        import launcher as L
        patchers, probed = self._patch(answers={8080})
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        rep = L.readiness_report()
        self.assertEqual(probed[0], 8081, "先探记录里的端口")
        self.assertTrue(rep["workbench"]["ok"])
        self.assertEqual(rep["port"], 8080, "报出的必须是真正在响应的端口")
        self.assertTrue(rep["url"].endswith(":8080"))
        self.assertEqual(rep["workbench"]["reported_port"], 8081)
        self.assertEqual(rep["workbench"]["port_mismatch"],
                         {"reported": 8081, "answered": 8080})
        self.assertIn("8080", rep["workbench"]["note"])
        self.assertIn("8081", rep["workbench"]["note"])

    def test_recorded_port_is_kept_when_it_answers(self):
        import launcher as L
        patchers, probed = self._patch(answers={8081})
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        rep = L.readiness_report()
        self.assertEqual(rep["port"], 8081)
        self.assertTrue(rep["url"].endswith(":8081"))
        self.assertNotIn("port_mismatch", rep["workbench"])
        self.assertEqual(probed, [8081], "已响应就不再探活别的端口")

    def test_no_candidate_answers_is_reported_plainly(self):
        import launcher as L
        patchers, _probed = self._patch(answers=set())
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        rep = L.readiness_report()
        self.assertFalse(rep["workbench"]["ok"])
        self.assertEqual(rep["port"], 8081)
        self.assertIn("未响应", rep["workbench"]["detail"])
        self.assertNotIn("port_mismatch", rep["workbench"], "都没响应就不是端口记录问题")

    def test_foreign_listener_is_never_reported_as_our_workbench(self):
        """本实例的 webui 不在运行（归属校验失败）→ 默认端口上响应的可能是**别人的**程序。

        此时不得把那个端口当成"我们的工作台"报出去（否则用户被带到错误的页面）。
        """
        import launcher as L
        patchers, probed = self._patch(answers={8080}, owned=False)
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        rep = L.readiness_report()
        self.assertEqual(probed, [8081], "无归属时不检查备选端口")
        self.assertFalse(rep["workbench"]["ok"])
        self.assertEqual(rep["port"], 8081)
        self.assertNotIn("port_mismatch", rep["workbench"])

    def test_url_command_follows_the_answering_port(self):
        """`launcher.py url` 必须给出打得开的地址（start.bat 据此打开浏览器）。"""
        import launcher as L
        read = {"recorded": 8081, "port": 8080, "detail": "HTTP 200 @ 8080",
                "answered": True, "mismatch": True,
                "probes": [{"port": 8081, "ok": False, "detail": "未响应 @ 8081"}]}
        with mock.patch.object(L, "answering_web_port", return_value=read), \
                mock.patch.object(sys, "argv", ["launcher.py", "url"]), \
                mock.patch("builtins.print") as out:
            L.main()
        printed = " ".join(str(c) for c in out.call_args_list)
        self.assertIn("http://localhost:8080", printed)
        self.assertNotIn("http://localhost:8081", printed)

    def test_url_command_keeps_a_single_line_on_stdout(self):
        """stdout 只能是地址本身：start.bat 用 for /f 逐行取值，最后一行会覆盖 URL。"""
        import launcher as L
        read = {"recorded": 8081, "port": 8080, "detail": "HTTP 200 @ 8080",
                "answered": True, "mismatch": True, "probes": []}
        out_lines: list = []
        err_lines: list = []

        def fake_print(*args, **kwargs):
            (err_lines if kwargs.get("file") is sys.stderr else out_lines).append(args)

        with mock.patch.object(L, "answering_web_port", return_value=read), \
                mock.patch.object(sys, "argv", ["launcher.py", "url"]), \
                mock.patch("builtins.print", side_effect=fake_print):
            L.main()
        self.assertEqual(out_lines, [("http://localhost:8080",)],
                         "stdout 只允许一行地址（说明必须走 stderr）")
        self.assertEqual(len(err_lines), 1, "端口不一致的说明写在 stderr")

        """`status` 打出的 URL 必须与探活同源：不得用 web_port() 另算一次。"""
        import launcher as L
        stub = {"workbench": {"ok": True, "detail": "HTTP 200 @ 8080", "port": 8080,
                              "url": "http://localhost:8080", "reported_port": 8081,
                              "answered_port": 8080,
                              "port_mismatch": {"reported": 8081, "answered": 8080},
                              "note": "端口记录与运行实例不一致：记录的 8081 无响应，"
                                      "实际在 8080 响应"},
                 "research": {"ok": False, "redis": False, "redis_major": None,
                              "missing": [], "stale": [], "orchestrator": False,
                              "required": []},
                 "code_sandbox": {"ok": False, "note": "", "execution_available": False,
                                  "reason": "docker 不可用"},
                 "port": 8080, "url": "http://localhost:8080", "ready": False}
        with mock.patch.object(L, "readiness_report", return_value=stub), \
                mock.patch.object(L, "web_port", return_value=8081), \
                mock.patch("builtins.print") as out:
            L.print_status()
        printed = " ".join(str(c) for c in out.call_args_list)
        self.assertIn("URL: http://localhost:8080", printed,
                      "URL 必须用探活那次读取的端口，而不是再解析一次")
        self.assertIn("HTTP 200 @ 8080", printed)
        self.assertIn("不一致", printed, "端口记录不一致必须如实打印，不静默")


class TestRuntimeStateIsolation(unittest.TestCase):
    """用例不得改写真实运行状态（`setUpModule` 把端口记录重定向到临时文件）。"""

    def test_start_services_never_writes_the_real_port_record(self):
        import launcher as L
        real = Path(L.PID_DIR) / "runtime_ports.json"
        before = real.read_bytes() if real.exists() else None
        tmp = tempfile.mkdtemp(prefix="wm_pids_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ready = {"workbench": {"ok": True, "detail": "HTTP 200 @ 8081", "port": 8081,
                               "url": "http://localhost:8081"},
                 "research": {"ok": True, "redis": True, "redis_major": 8, "missing": [],
                              "stale": [], "orchestrator": True, "required": []},
                 "code_sandbox": {"ok": False, "note": "", "execution_available": False,
                                  "isolation_required": True, "reason": "docker 不可用"},
                 "port": 8081, "url": "http://localhost:8081", "ready": True}
        with mock.patch.object(L, "PID_FILE", Path(tmp) / "pids.json"), \
                mock.patch.object(L, "_load_config", return_value={}), \
                mock.patch.object(L, "_read_pids", return_value={"services": {}}), \
                mock.patch.object(L, "_webui_owned_here", return_value=False), \
                mock.patch.object(L, "_preferred_web_port", return_value=8080), \
                mock.patch.object(L, "_web_port_is_explicit", return_value=False), \
                mock.patch.object(L, "_port_is_free",
                                  side_effect=lambda p: int(p) != 8080), \
                mock.patch.object(L, "build_services", return_value=[]), \
                mock.patch.object(L, "stop_services", return_value=[]), \
                mock.patch.object(L, "verify_services",
                                  return_value={"total": 0, "alive": 0, "down": [],
                                                "never_started": [], "waited": 0}), \
                mock.patch.object(L, "readiness_report", return_value=ready), \
                mock.patch.object(L, "_ensure_redis_available"), \
                mock.patch.object(L, "_wait_redis_ready", return_value=True), \
                mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch("builtins.print"):
            L.start_services()
        after = real.read_bytes() if real.exists() else None
        self.assertEqual(before, after,
                         "用例把真实的 .weavemind/runtime_ports.json 改写了："
                         "让位记录必须落在隔离的临时文件里")
        recorded = json.loads(Path(L.RUNTIME_PORTS_FILE).read_text(encoding="utf-8"))
        self.assertEqual(recorded["web"], 8081, "让位记录应写进隔离文件（证明该路径确实会写）")


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


class TestOrchestratorOwnership(unittest.TestCase):
    """C3/H3：同一 Redis 上只允许一个编排器消费任务通道。

    反例（原指令 §3.4）：两个编排器订阅同一条 `orchestrator:main` 时，
    pub/sub 是广播——同一条任务请求被**执行两次**（重复付费、重复写库）。
    """

    def setUp(self):
        """每条用例前后都保存/恢复**进程级** `_OWNER_STATE`。

        它是模块级单例：认领/拒绝都会改它，而"曾经持有过 + 现在没有"会让
        **后续无关用例**里的任务被判成"被取代的旧持有者"（实测：`test_offline_delivery`
        的正常落库被误拦、交付状态变 unknown）。测试之间必须互不污染。
        """
        import orchestrator_v2 as _ov2
        self._ov2 = _ov2
        with _ov2._OWNER_LOCK:
            self._saved_owner = dict(_ov2._OWNER_STATE)
        with _ov2._OWNER_LOCK:
            _ov2._OWNER_STATE.clear()
            _ov2._OWNER_STATE.update({"token": "", "instance": "", "held": False,
                                      "reason": "未认领", "renewed_at": 0.0,
                                      "renewed_mono": 0.0, "gen": 0,
                                      "ever_held": False})

    def tearDown(self):
        with self._ov2._OWNER_LOCK:
            self._ov2._OWNER_STATE.clear()
            self._ov2._OWNER_STATE.update(self._saved_owner)

    class _R:
        """最小 Redis 替身：建模**带 TTL 的租约**（eval 三合一 / get）。

        `lease_alive` 表示"当前持有者的租约是否仍在有效期内"——续租由实现负责，
        替身只回答"过期没有"，这样才测得出"心跳只写一次"这类洞。
        """

        def __init__(self, owner=None, lease_alive=False, legacy_no_lease=False):
            self.owner = owner
            self.lease_alive = lease_alive
            self.legacy_no_lease = legacy_no_lease
            self.evals = []
            self._gen = 0

        def incr(self, k):
            """持有期代号：每次成功认领自增（与真 Redis 的 INCR 同语义）。"""
            self._gen += 1
            return self._gen

        def pttl(self, k):
            """租约剩余时间（毫秒）：模型里"活着的租约"就是满 TTL。"""
            return 30000 if (self.owner and self.lease_alive) else -2

        def eval(self, script, numkeys, key, token, ttl_ms):
            self.evals.append((key, token, ttl_ms))
            if "PTTL" in script:                     # 遗留无租约键的接管脚本
                if self.legacy_no_lease:
                    self.owner = token
                    self.lease_alive = True
                    self.legacy_no_lease = False
                    return 1
                return 0
            if self.owner is None or not self.lease_alive:
                self.owner = token               # 空租约或已过期 → 认领/接管
                self.lease_alive = True
                return 1
            return 1 if self.owner == token else 0   # 自己的 → 续期；别人的活租约 → 拒绝

        def get(self, k):
            return self.owner if self.lease_alive else None

    def test_first_claim_wins(self):
        from orchestrator_v2 import claim_orchestrator_ownership, ownership_held
        r = self._R()
        ok, why = claim_orchestrator_ownership(r, instance="inst-a")
        self.assertTrue(ok, why)
        self.assertTrue(ownership_held(), "认领成功后闸门必须打开")
        self.assertTrue(str(r.owner).startswith("inst-a:"),
                        "租约里必须写**每进程唯一令牌**（实例名 + pid + 随机串）")

    def test_same_instance_name_in_another_process_is_refused(self):
        """同名实例**不再**是放行理由（指令 §2.3）。

        两个进程完全可能配成同一个实例名（同机两份拷贝/同配置跑两次）。旧实现
        `cur == inst` 即放行，于是 B 把 A 的**活租约**当成"我自己的旧租约"，
        两个编排器同时消费 orchestrator:main；令牌不同就必须走"是否过期"这一条。
        """
        from orchestrator_v2 import claim_orchestrator_ownership
        r = self._R(owner="inst-a:111:aaaaaa", lease_alive=True)
        ok, why = claim_orchestrator_ownership(r, instance="inst-a")
        self.assertFalse(ok, "同名但不同进程不得顶掉活租约")
        self.assertIn("不允许两个编排器", why)

    def test_second_live_instance_is_refused(self):
        """别的实例租约仍在有效期内 → 明确拒绝，不默默重复消费。"""
        from orchestrator_v2 import claim_orchestrator_ownership, ownership_held
        r = self._R(owner="inst-other:222:bbbbbb", lease_alive=True)
        ok, why = claim_orchestrator_ownership(r, instance="inst-b")
        self.assertFalse(ok, why)
        self.assertIn("inst-other", why)
        self.assertIn("不允许两个编排器", why)
        self.assertFalse(ownership_held(), "被拒绝时闸门必须是关的")

    # ── A-3（09-28 下午复核）：租约丢失要有本地有效期与派发/落库闸门 ──────────

    def test_stale_lease_is_not_held_after_the_local_validity_window(self):
        """反例：**续租时间过期一小时**却仍报 `held=True`。

        `ownership_held()` 原来只读那个布尔量：续租线程死掉、进程被挂起、Redis 长时间
        不可达之后，它会一直停在 True，而真实租约早就过期、别人可能已经接管——
        "入口检查"因此形同虚设。现在用**单调钟**算本地有效截止
        （`expires_mono`；Q0 起由服务端剩余 TTL **扣**余量得到，不再加宽限）。
        """
        import orchestrator_v2 as ov2
        with self._env():
            r = self._R()
            ov2.claim_orchestrator_ownership(r, instance="inst-stale")
            self.assertTrue(ov2.ownership_held())
            # 把"本地有效截止"推到一小时前（模拟续租线程已死/进程被挂起）。
            # **用固定时钟**：单调钟在刚开机的机器上只有几百秒（CI runner 实测），
            # 直接 `monotonic() - 3600` 会得到负数，`age` 变成 None，断言报的是
            # `'>' not supported between NoneType and int`——查的是环境，不是行为。
            with mock.patch.object(ov2.time, "monotonic", lambda: 10_000.0):
                with ov2._OWNER_LOCK:
                    ov2._OWNER_STATE["renewed_mono"] = 6_400.0
                    ov2._OWNER_STATE["expires_mono"] = 6_400.0
                self.assertFalse(ov2.ownership_held(),
                                 "本地有效期已过必须判失租（不能只看布尔量）")
                self.assertTrue(ov2.ownership_lost(),
                                "曾经持有、现在失去 → 落库闸门要能识别")
                lease = ov2.ownership_lease()
                self.assertFalse(lease["valid"])
                self.assertGreater(lease["age"], ov2.OWNER_HB_TTL)
                self.assertLess(lease["expires_in"], 0.0)

    def test_queue_keys_are_derived_from_the_registered_agent_id(self):
        """服务名≠注册名≠队列键：`launcher.py queues` 必须按**注册表**给队列键。

        实机踩到（2026-09-29）：往 `task_queue:worker-web-fetch`（服务标签）投任务没人取，
        正确键是 `task_queue:webfetchworker`（注册名）。外部探针只能靠猜——这条命令
        把这个对应关系说清，避免再把"投错队列"当成"worker 挂了"。
        """
        import contextlib
        import io
        import launcher
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            launcher.print_queues()
        out = buf.getvalue()
        self.assertIn("队列", out)
        for line in out.splitlines():
            if "注册名=" in line:
                agent = line.split("注册名=")[1].split()[0]
                self.assertIn(f"task_queue:{agent}", line,
                              "队列键必须由注册名推导，不能用服务标签")

    def test_lease_lost_is_not_the_same_as_never_held(self):
        """从未持有（单机直跑/Redis 不可用）**不等于**失租——不能因此拦住落库。"""
        import orchestrator_v2 as ov2
        with self._env():
            with ov2._OWNER_LOCK:
                ov2._OWNER_STATE.update({"held": False, "ever_held": False,
                                         "renewed_mono": 0.0})
            self.assertFalse(ov2.ownership_held())
            self.assertFalse(ov2.ownership_lost(),
                             "从没持有过不该触发落库闸门（否则单机部署会卡在 RUNNING）")

    def test_dispatch_is_refused_when_the_task_start_lease_was_superseded(self):
        """A-3：入口闸门管"新任务"，**已启动任务**的后续步骤必须另有闸门。

        场景：任务在持有者 A 名下启动（`started` 事件记了 A 的指纹），随后租约被 B
        接管；A 进程里这条任务还在跑 → 它的后续派发必须被拒（旧持有者不得继续产生
        有效结果，也不能继续花钱）。
        """
        import orchestrator_v2 as ov2
        import task_state as ts_m
        from orchestrator_v2 import OrchestratorV2
        with self._env():
            # 本进程**没有**租约（held=False），任务启动时记的是"别人"的指纹
            o = object.__new__(OrchestratorV2)
            sent: list = []
            o._messaging = type("M", (), {
                "publish": lambda s, ch, payload: sent.append((ch, payload))})()
            o._now_iso = lambda: "t"
            o._new_redis_sync = lambda: (_ for _ in ()).throw(
                AssertionError("被取代后不得再发任何请求"))
            with mock.patch.object(ov2, "_task_start_binding",
                                   return_value={"owner": "deadbeefdeadbeef", "gen": 1}):
                out = o._dispatch({"step_id": "s1", "capability": "web_search",
                                   "instruction": "检索"}, "t-lost")
            self.assertEqual(out["status"], "FAILED")
            self.assertIn("租约", out["result"])
            self.assertIn("拒绝派发", str(sent))
            # 反向：本进程**持有活租约**，且任务绑定的代次不新于本段持有期 → 不拦
            with mock.patch.object(ov2, "_task_start_binding",
                                   return_value={"owner": ov2._owner_fingerprint(),
                                                 "gen": ov2.ownership_generation()}):
                ov2._OWNER_STATE.update({"held": True, "ever_held": True,
                                         "expires_mono": ov2.time.monotonic() + 20.0})
                self.assertEqual(ov2._lease_superseded("t-mine"), "",
                                 "持有有效租约时不得判成被取代")
            # 反向：任务启动时**没有**租约概念（旧行/单机直跑）→ 不拦
            with mock.patch.object(ov2, "_task_start_binding", return_value={}):
                self.assertEqual(ov2._lease_superseded("t-legacy"), "",
                                 "没有启动绑定的旧行不得被拦（否则单机部署会卡住）")
        self.assertIsNotNone(ts_m)

    def test_same_owner_fingerprint_is_refused_once_the_lease_is_lost(self):
        """Q0 反例①：**同 owner 指纹直接放行** → 失租的旧持有者恢复后照跑。

        旧 `_lease_superseded` 的第一条就是 `start_owner == _owner_fingerprint()`
        → 返回空串。于是"旧 owner 被挂起 → 租约过期 → 新 owner 接管 → 旧 owner 恢复"
        这一串里，旧持有者**指纹没变**，它继续派发、继续落终态，与新 owner 的进度
        互相覆盖。判据必须改成"**先证明执行权**（活租约），再看任务绑定"。
        """
        import orchestrator_v2 as ov2
        with self._env():
            r = self._R()
            ov2.claim_orchestrator_ownership(r, instance="inst-old")
            fp = ov2._owner_fingerprint()
            self.assertTrue(ov2.ownership_held())
            with mock.patch.object(ov2, "_task_start_binding",
                                   return_value={"owner": fp, "gen": 1}):
                self.assertEqual(ov2._lease_superseded("t-own"), "",
                                 "持活租约 + 同指纹 → 放行")
                # 被挂起：本地有效截止已过（服务端租约同样已过期，B 可合法接管）
                ov2._OWNER_STATE.update(
                    {"expires_mono": ov2.time.monotonic() - 1.0})
                self.assertFalse(ov2.ownership_held())
                why = ov2._lease_superseded("t-own")
                self.assertNotEqual(why, "", "同指纹但已失租 → 必须拦（迟到结果）")
                self.assertIn("指纹", why)
            # 反向：从没持有过租约的进程**也要**拦"别人名下"的任务（消息是广播的、
            # 启动恢复也扫库：证明不了执行权却去跑别人的任务 = 重复消费）
            with ov2._OWNER_LOCK:
                ov2._OWNER_STATE.update({"held": False, "ever_held": False,
                                         "expires_mono": 0.0})
            with mock.patch.object(ov2, "_task_start_binding",
                                   return_value={"owner": "someone-else", "gen": 3}):
                self.assertNotEqual(ov2._lease_superseded("t-other"), "",
                                    "别人名下的任务不得被未持有租约的进程接手")
            # 唯一保留的旁路：任务启动时**没有**任何执行权绑定（旧行/离线单机）
            with mock.patch.object(ov2, "_task_start_binding", return_value={}):
                self.assertEqual(ov2._lease_superseded("t-unbound"), "",
                                 "没有绑定的旧行继续按离线处理（且日志可见）")

    def test_local_validity_never_exceeds_the_server_lease(self):
        """Q0 反例②：本地有效期 = `TTL + 宽限 10s`，比服务端租约**长 10 秒**。

        服务端 `SET … PX 30000` 一到期，B 就能合法认领；而 A 的本地计时到 40 秒才
        判失租——中间那 10 秒两个 owner 并存（A 仍在派发/落库）。修正后本地有效期
        由**服务端剩余 TTL 扣保守余量**得到，只会更短。
        """
        import orchestrator_v2 as ov2
        with self._env():
            ov2.claim_orchestrator_ownership(self._R(), instance="inst-ttl")
            lease = ov2.ownership_lease()
            self.assertIsNotNone(lease["expires_in"])
            self.assertLessEqual(lease["expires_in"], lease["ttl"],
                                 "本地有效期绝不能长于服务端 TTL")
            self.assertEqual(lease["grace"], 0.0,
                             "兼容字段 grace 不得再是可加宽限（已更正为 0）")
            base = ov2.time.monotonic()
            eff = float(lease["expires_in"])
            for age, want in ((eff * 0.5, True), (eff + 0.2, False),
                              (ov2.OWNER_HB_TTL + 2.0, False)):
                with ov2._OWNER_LOCK:
                    ov2._OWNER_STATE["expires_mono"] = base + eff - age
                self.assertEqual(ov2.ownership_held(), want,
                                 f"距上次续租 {age:.1f}s 时的闸门状态不对")

    def test_resumed_old_owner_cannot_dispatch_or_finalize_after_takeover(self):
        """Q0 隔离演练（进程内两套本地视图 + 一个共享租约）：旧 owner **恢复**后不得继续。

        真实拓扑里 A、B 是**两个进程**，各自只看得见自己的 `_OWNER_STATE`；这里用一个
        Redis 替身 + 保存/恢复进程级状态来分别扮演两套视图：

        A 认领（gen1）→ 任务在 A 名下 started（绑定 owner=fp_A, gen=1）
        → A 被挂起（本地有效截止过期）→ B 在**同一替身**上接管（gen2）
        → A 恢复：派发与终态落库都必须被拒；B 自己的任务照常放行（控制面）。
        """
        import orchestrator_v2 as ov2
        from orchestrator_v2 import OrchestratorV2
        with self._env():
            r = self._R()
            ov2.claim_orchestrator_ownership(r, instance="inst-A")
            gen_a, fp_a = ov2.ownership_generation(), ov2._owner_fingerprint()
            self.assertEqual(gen_a, 1)
            binding_a = {"owner": fp_a, "gen": gen_a, "instance": "inst-A"}
            with mock.patch.object(ov2, "_task_start_binding", return_value=binding_a):
                self.assertEqual(ov2._lease_superseded("t-A"), "",
                                 "A 持活租约时自己的任务放行")

            # ── A 被挂起：本地有效截止过期（服务端租约同样过期）──
            with ov2._OWNER_LOCK:
                view_a = dict(ov2._OWNER_STATE)
                ov2._OWNER_STATE["expires_mono"] = ov2.time.monotonic() - 1.0
            self.assertFalse(ov2.ownership_held())
            with mock.patch.object(ov2, "_task_start_binding", return_value=binding_a):
                why_a = ov2._lease_superseded("t-A")
                self.assertNotEqual(why_a, "",
                                    "A 失租后（指纹仍与绑定相同）不得继续派发")
                self.assertIn("指纹", why_a)
                o = object.__new__(OrchestratorV2)
                wrote: list = []
                with mock.patch("task_state.read_task", return_value={"status": "RUNNING"}), \
                        mock.patch("task_state.record_completion",
                                   side_effect=lambda *a, **k: wrote.append(a) or True):
                    o._finalize_task("t-A", "目标", "SUCCESS", report="A 的迟到结果")
                self.assertEqual(wrote, [], "A 失租后不得落终态（迟到结果）")

            # ── B 接管：同一替身上旧租约已过期 → 认领成功，代号前进 ──
            with ov2._OWNER_LOCK:
                state_a = dict(ov2._OWNER_STATE)
            r.lease_alive = False
            ok_b, why_b = ov2.claim_orchestrator_ownership(r, instance="inst-B")
            self.assertTrue(ok_b, why_b)
            self.assertEqual(ov2.ownership_generation(), gen_a + 1,
                             "接管者必须拿到新的持有期代号")
            binding_b = {"owner": ov2._owner_fingerprint(), "gen": ov2.ownership_generation(),
                         "instance": "inst-B"}
            with mock.patch.object(ov2, "_task_start_binding", return_value=binding_b):
                self.assertEqual(ov2._lease_superseded("t-B"), "",
                                 "接管者 B 处理自己的任务不得被拦")

            # ── 切回 A 的视图（恢复后的旧进程）：两条闸门都必须仍然拦 ──
            with ov2._OWNER_LOCK:
                ov2._OWNER_STATE.clear()
                ov2._OWNER_STATE.update(state_a)
            self.assertFalse(ov2.ownership_held(), "A 的视图里租约已失")
            self.assertTrue(ov2.ownership_lost(), "A 是'曾经持有、现在失去'")
            with mock.patch.object(ov2, "_task_start_binding", return_value=binding_a):
                self.assertNotEqual(ov2._lease_superseded("t-A"), "",
                                    "B 接管后 A 更不得继续")
                o2 = object.__new__(OrchestratorV2)
                wrote2: list = []
                with mock.patch("task_state.read_task", return_value={"status": "RUNNING"}), \
                        mock.patch("task_state.record_completion",
                                   side_effect=lambda *a, **k: wrote2.append(a) or True):
                    o2._finalize_task("t-A", "目标", "SUCCESS", report="A 的迟到结果")
                self.assertEqual(wrote2, [], "接管后 A 的终态落库必须被拒")
            # 控制面：A 的执行权没有把 B 的租约弄丢（替身上的令牌仍是 B 的）
            r.lease_alive = True
            self.assertTrue(str(r.owner).startswith("inst-B:"),
                            f"替身上的租约仍是 B 的令牌（实测 {r.owner!r}）")

    def test_finalize_is_refused_when_the_task_start_lease_was_superseded(self):
        """A-3 落库闸门：迟到结果不得覆盖新 owner；已取消是终态不得被覆盖。"""
        import orchestrator_v2 as ov2
        from orchestrator_v2 import OrchestratorV2
        with self._env():
            o = object.__new__(OrchestratorV2)
            called: list = []
            with mock.patch("task_state.read_task", return_value={"status": "RUNNING"}), \
                    mock.patch("task_state.record_completion",
                               side_effect=lambda *a, **k: called.append(a) or True), \
                    mock.patch.object(ov2, "_task_start_binding",
                                      return_value={"owner": "deadbeefdeadbeef", "gen": 1}):
                o._finalize_task("t-late", "目标", "SUCCESS", report="迟到结果")
            self.assertEqual(called, [], "被取代的旧持有者不得落终态")
        with self._env():
            o2 = object.__new__(OrchestratorV2)
            called2: list = []
            with mock.patch("task_state.read_task", return_value={"status": "CANCELLED"}), \
                    mock.patch("task_state.record_completion",
                               side_effect=lambda *a, **k: called2.append(a) or True), \
                    mock.patch.object(ov2, "_task_start_binding", return_value={}):
                o2._finalize_task("t-cancel", "目标", "SUCCESS", report="迟到结果")
            self.assertEqual(called2, [], "已取消是终态，不得被迟到结果覆盖")

    def test_dead_owner_is_taken_over(self):
        """本类多数用例直接改进程级 `_OWNER_STATE`：用上下文管理器做保存/恢复。"""
        import contextlib
        import orchestrator_v2 as ov2

        @contextlib.contextmanager
        def _cm():
            with ov2._OWNER_LOCK:
                saved = dict(ov2._OWNER_STATE)
            try:
                with ov2._OWNER_LOCK:
                    ov2._OWNER_STATE.clear()
                    ov2._OWNER_STATE.update({"token": "", "instance": "", "held": False,
                                             "reason": "未认领", "renewed_at": 0.0,
                                             "renewed_mono": 0.0, "gen": 0,
                                             "ever_held": False})
                yield
            finally:
                with ov2._OWNER_LOCK:
                    ov2._OWNER_STATE.clear()
                    ov2._OWNER_STATE.update(saved)
        return _cm()

    def _env(self):
        """A-3 用例会直接改进程级 `_OWNER_STATE`：用上下文管理器做保存/恢复。

        不恢复的话，"曾经持有过"这类状态会**粘**到同一进程里后续无关用例上
        （实测：`test_offline_delivery` 的正常落库会被误拦）。
        """
        import contextlib
        import orchestrator_v2 as ov2

        @contextlib.contextmanager
        def _cm():
            with ov2._OWNER_LOCK:
                saved = dict(ov2._OWNER_STATE)
            try:
                with ov2._OWNER_LOCK:
                    ov2._OWNER_STATE.clear()
                    ov2._OWNER_STATE.update({"token": "", "instance": "", "held": False,
                                             "reason": "未认领", "renewed_at": 0.0,
                                             "renewed_mono": 0.0, "gen": 0,
                                             "ever_held": False})
                yield
            finally:
                with ov2._OWNER_LOCK:
                    ov2._OWNER_STATE.clear()
                    ov2._OWNER_STATE.update(saved)
        return _cm()

    def test_dead_owner_is_taken_over(self):
        """上一位持有者租约已过期（崩溃/重启）→ 允许接管。"""
        from orchestrator_v2 import claim_orchestrator_ownership, ownership_held
        r = self._R(owner="inst-dead:333:cccccc", lease_alive=False)
        ok, why = claim_orchestrator_ownership(r, instance="inst-c")
        self.assertTrue(ok, why)
        self.assertTrue(str(r.owner).startswith("inst-c:"), "接管后租约要落到本进程令牌")
        self.assertTrue(ownership_held())

    def test_redis_failure_does_not_block_startup_but_stops_dispatch(self):
        """认领异常：**不阻断启动**（可见性），但**停止新派发**（安全性）。"""
        from orchestrator_v2 import claim_orchestrator_ownership, ownership_held

        class _Bad:
            def eval(self, *a, **k):
                raise RuntimeError("redis down")

            def get(self, *a, **k):
                raise RuntimeError("redis down")

        ok, why = claim_orchestrator_ownership(_Bad(), instance="inst-d")
        self.assertTrue(ok, "认领失败不得阻断启动")
        self.assertIn("未确认", why)
        self.assertFalse(ownership_held(),
                         "归属未知时闸门必须关闭：证明不了唯一执行权就不派发")

    def test_legacy_owner_key_without_ttl_is_migrated(self):
        """旧实现写的归属键**没有 TTL**：不迁移的话新实现会**永久拒绝启动**。

        旧代码是 `r.set(OWNER_KEY, inst)`（无过期），那个键永远在，也没有任何存活信息。
        新实现要求"活租约"才算被占，所以必须有一条只覆盖"无 TTL 键"的原子迁移路径。
        """
        from orchestrator_v2 import claim_orchestrator_ownership, ownership_held
        r = self._R(owner="inst-old:1:ffffff", lease_alive=True, legacy_no_lease=True)
        ok, why = claim_orchestrator_ownership(r, instance="inst-new")
        self.assertTrue(ok, why)
        self.assertIn("接管遗留键", why)
        self.assertTrue(str(r.owner).startswith("inst-new:"), "迁移后租约归本进程")
        self.assertTrue(ownership_held())

    def test_renew_requires_still_owning_the_lease(self):
        """续租必须带令牌比对：被接管之后旧持有者不能再给自己续命。"""
        from orchestrator_v2 import claim_orchestrator_ownership, renew_orchestrator_ownership
        r = self._R()
        claim_orchestrator_ownership(r, instance="inst-e")
        self.assertTrue(renew_orchestrator_ownership(r), "自己持有时应能续期")
        r.owner = "inst-f:444:dddddd"           # 被他人接管
        self.assertFalse(renew_orchestrator_ownership(r), "已被接管不得续期成功")

    def test_release_only_deletes_own_lease(self):
        from orchestrator_v2 import claim_orchestrator_ownership, release_orchestrator_ownership

        class _R2(self._R):
            def eval(self, script, numkeys, key, token, *rest):
                return 1 if self.owner == token else 0

        r = _R2(owner="someone-else:555:eeeeee", lease_alive=True)
        claim_orchestrator_ownership(r, instance="inst-g")     # 活租约 → 认领失败
        self.assertFalse(release_orchestrator_ownership(r), "不得误删他人的租约")
        self.assertEqual(r.owner, "someone-else:555:eeeeee")

    def test_dispatch_gate_blocks_accept_and_resume_when_not_held(self):
        """未持有归属 → accept 明确拒绝、resume 不捡任何任务（都不执行）。"""
        from orchestrator_v2 import accept_task_request, resume_received_tasks
        redis = mock.MagicMock()
        orch = type("O", (), {"_redis": redis})()
        with mock.patch("orchestrator_v2.ownership_held", return_value=False):
            ok, reason = accept_task_request(orch, {"task_id": "t-1", "goal": "g"})
            self.assertFalse(ok, "未持有归属不得接收任务")
            self.assertIn("未持有编排器归属", reason)
            self.assertEqual(resume_received_tasks(orch), 0,
                             "未持有归属不得恢复执行任何收执")


if __name__ == "__main__":
    unittest.main()
