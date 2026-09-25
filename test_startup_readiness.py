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
import socket
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
        self.assertIn("CODE_EXECUTION_SANDBOX=restricted", rep["detail"], "要给出路")
        self.assertIn("Dockerfile.sandbox", rep["detail"])
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

    def test_launcher_startup_prints_the_note(self):
        src = Path("launcher.py").read_text(encoding="utf-8")
        self.assertIn("代码执行：容器隔离不可用", src)
        self.assertIn("isolation_note()", src)


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


if __name__ == "__main__":
    unittest.main()
