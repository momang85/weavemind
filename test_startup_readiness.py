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


if __name__ == "__main__":
    unittest.main()
