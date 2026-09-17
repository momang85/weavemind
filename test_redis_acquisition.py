# -*- coding: utf-8 -*-
"""MKT-P0-2 回归：便携 Redis 的获取顺序、预算、离线复用与指引（离线，不联网）。

判据来自市场评估：国内网络无代理下"依赖就绪 ≤60 秒"，且失败要给出**可执行**的中文指引。
这里用替身换掉真正的下载与探测，只验证编排逻辑本身。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import dep_check as dc  # noqa: E402


def _make_zip(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Redis-x64/redis-server.exe", b"MZ fake")


class TestSourceOrder(unittest.TestCase):
    """顺序：用户显式 → 用户镜像 → 内置镜像 → 官方 GitHub（官方兜底）。"""

    def test_explicit_first_official_last(self):
        with mock.patch.dict(os.environ, {"WM_REDIS_ZIP_URL": "https://github.com/x/y.zip"}):
            srcs = dc.redis_zip_sources()
        self.assertEqual(srcs[0], "https://github.com/x/y.zip")
        self.assertEqual(srcs[-1], dc.REDIS_ZIP_URL)
        self.assertTrue(any("ghproxy" in s for s in srcs), srcs)

    def test_user_mirrors_respected_in_order(self):
        """WM_REDIS_MIRRORS 是**前缀**；以 .zip 结尾的条目按完整地址处理。"""
        with mock.patch.dict(os.environ, {
                "WM_REDIS_MIRRORS": "https://a.example/m/ , https://b.example/full.zip"}):
            srcs = dc.redis_zip_sources()
        a = "https://a.example/m/" + dc.REDIS_ZIP_URL
        self.assertIn(a, srcs)
        self.assertIn("https://b.example/full.zip", srcs)
        self.assertLess(srcs.index(a), srcs.index("https://b.example/full.zip"))
        self.assertLess(srcs.index("https://b.example/full.zip"),
                        srcs.index(dc.REDIS_ZIP_URL))

    def test_mirror_hosts_are_in_download_allowlist(self):
        for host in dc.MIRROR_HOSTS:
            self.assertIn(host, dc.DOWNLOAD_HOSTS)

    def test_sources_are_deduplicated(self):
        with mock.patch.dict(os.environ, {"WM_REDIS_MIRRORS": dc.REDIS_ZIP_URL}):
            srcs = dc.redis_zip_sources()
        self.assertEqual(len(srcs), len(set(srcs)))


class TestFetchFallsThrough(unittest.TestCase):
    """前几个源失败要继续试，且总预算到点就停（不无限等一个源）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_redis_"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.zip_path = self.tmp / "redis-windows.zip"

    def test_falls_through_to_next_source(self):
        calls: list[str] = []

        def _dl(url, dest, max_bytes=None, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                return False, "连接超时"
            _make_zip(Path(dest))
            return True, "ok"

        with mock.patch.object(dc, "_safe_download", _dl):
            ok, msg, used = dc.fetch_portable_redis(self.zip_path, budget=30)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 2, calls)
        self.assertEqual(used, calls[1])
        self.assertIn("来自", msg)

    def test_budget_exhaustion_stops_trying(self):
        calls: list[str] = []
        clock = {"t": 1000.0}

        def _time():
            return clock["t"]

        def _dl(url, dest, max_bytes=None, timeout=None):
            calls.append(url)
            clock["t"] += 40.0          # 每个源都超预算
            return False, "超时"

        with mock.patch.object(dc, "_safe_download", _dl), \
                mock.patch.object(dc.time, "time", _time):
            ok, msg, _ = dc.fetch_portable_redis(self.zip_path, budget=60,
                                                 per_source=50)
        self.assertFalse(ok)
        self.assertLessEqual(len(calls), len(dc.redis_zip_sources()))
        self.assertIn("预算", msg)

    def test_sha256_pin_rejects_and_continues(self):
        seen: list[str] = []

        def _dl(url, dest, max_bytes=None, timeout=None):
            seen.append(url)
            _make_zip(Path(dest))
            return True, "ok"

        with mock.patch.object(dc, "_safe_download", _dl), \
                mock.patch.dict(os.environ, {"WM_REDIS_ZIP_SHA256": "0" * 64}):
            ok, msg, _ = dc.fetch_portable_redis(self.zip_path, budget=30)
        self.assertFalse(ok, "摘要不符必须拒绝")
        self.assertIn("摘要不符", msg)
        self.assertGreaterEqual(len(seen), 2, "摘要不符后应继续试下一个源")
        self.assertFalse(self.zip_path.exists(), "被拒的包要清掉")

    def test_bad_payload_is_rejected(self):
        def _dl(url, dest, max_bytes=None, timeout=None):
            Path(dest).write_bytes(b"not a zip")
            return True, "ok"

        with mock.patch.object(dc, "_safe_download", _dl):
            ok, msg, _ = dc.fetch_portable_redis(self.zip_path, budget=20)
        self.assertFalse(ok)
        self.assertIn("合法 zip", msg)


class TestOfflineReuse(unittest.TestCase):
    """已有的合法 zip 直接复用：不再联网（离线搬运路径）。"""

    def test_existing_zip_is_reused_without_network(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_redis2_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        zp = tmp / "redis-windows.zip"
        _make_zip(zp)
        with mock.patch.object(dc, "_safe_download",
                               side_effect=AssertionError("不应联网")):
            ok, msg, used = dc.fetch_portable_redis(zp, budget=10)
        self.assertTrue(ok)
        self.assertEqual(used, "local-cache")
        self.assertIn("复用", msg)


class TestSystemRedisPreference(unittest.TestCase):
    """能复用系统已装的 Redis 就不要联网下载。

    "系统已装的 Redis"在两端是**两条不同分支**：Windows 走 `_system_redis_exe()`
    （PATH → 注册服务 → 常见安装目录，action=`started_system`），Linux/其它平台走
    PATH 上的 `redis-server`（action=`started`）。此前只有 Windows 那条用例，
    在 Linux CI 上必然失败（实测：`no_binary`——该分支在 Linux 上根本不看
    `_system_redis_exe`），而补的 POSIX 用例又把 action 名猜成了 `started_system`。

    因此按平台各留一条、各断言**本平台分支**的真实取值：拿一端的行为套另一端
    已经错过两次（一次漏了另一端，一次名字抄错）。用 `os.name` 替身强行跨平台跑
    也不通——它会连带改掉 `pathlib` 的 flavor（Python 3.14 下 `Path()` 直接抛
    `UnsupportedOperation`）。
    """

    def _run(self, extras: tuple = ()):
        """跑一遍 `ensure_redis`：系统探测全部打桩，只留被测分支。

        `extras` 是 (点号目标, 替身值) —— `shutil.which` 在 shutil 模块上、
        `_system_redis_exe` 在 dep_check 上，不能都按 dep_check 的属性找。
        """
        for pat in (
            mock.patch.object(dc, "_spawn_background",
                              lambda argv, log, cwd=None: mock.Mock(pid=4242)),
            mock.patch.object(dc, "_write_redis_pid", lambda pid: None),
            mock.patch.object(dc, "_wait_redis", lambda h, p, w: True),
            mock.patch.object(dc, "_publish_redis_host_env", lambda a: None),
            mock.patch.object(dc, "redis_ping", lambda h, p: False),
            mock.patch.object(dc, "_usable_portable_redis", lambda: None),
            mock.patch.object(dc, "fetch_portable_redis",
                              side_effect=AssertionError("不该下载")),
            mock.patch.object(dc, "_redis_server_version", lambda h, p: 8),
        ):
            pat.start()
            self.addCleanup(pat.stop)
        for target, value in extras:
            pat = mock.patch(target, value)
            pat.start()
            self.addCleanup(pat.stop)
        return dc.ensure_redis(auto=True)

    @unittest.skipUnless(os.name == "nt", "Windows 分支：_system_redis_exe 探测")
    def test_windows_prefers_system_binary(self):
        fake = Path(tempfile.mkdtemp(prefix="wm_redis3_")) / "redis-server.exe"
        fake.write_bytes(b"MZ")
        self.addCleanup(__import__("shutil").rmtree, fake.parent, ignore_errors=True)
        res = self._run((("dep_check._system_redis_exe", lambda: fake),))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "started_system")

    @unittest.skipIf(os.name == "nt", "POSIX 分支：PATH 上的 redis-server")
    def test_posix_prefers_path_binary(self):
        fake = Path(tempfile.mkdtemp(prefix="wm_redis5_")) / "redis-server"
        fake.write_text("#!/bin/sh\n", encoding="utf-8")
        self.addCleanup(__import__("shutil").rmtree, fake.parent, ignore_errors=True)
        res = self._run((("dep_check.shutil.which",
                          lambda name: str(fake) if name == "redis-server" else None),))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "started",
                         "POSIX 分支的 action 是 started（Windows 才是 started_system）")
        self.assertIn("redis-server", res["detail"])

    @unittest.skipIf(os.name == "nt", "POSIX 分支：PATH 上没有就给出可执行指引")
    def test_posix_without_binary_reports_guidance(self):
        res = self._run((("dep_check.shutil.which", lambda name: None),))
        self.assertFalse(res["ok"])
        self.assertEqual(res["action"], "no_binary")
        self.assertIn("redis-server", res["detail"])

    def test_version_probe_parses_major(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_redis4_"))
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        exe = tmp / "redis-server.exe"
        exe.write_text("", encoding="utf-8")
        with mock.patch.object(dc.subprocess, "run",
                               lambda *a, **k: mock.Mock(stdout="Redis server v=8.10.1",
                                                         stderr="", returncode=0)):
            self.assertEqual(dc._redis_version_of(exe), 8)


class TestGuidanceIsActionable(unittest.TestCase):
    """失败指引必须给可执行步骤（市场评估的判据之一）。"""

    def setUp(self):
        self.hint = dc.REDIS_HINT

    def test_mentions_system_options_mirror_and_offline(self):
        for token in ("Memurai", "apt install redis-server", "docker run",
                      "WM_REDIS_MIRROR_BASE", "redis-windows.zip", "直接复用",
                      "WM_REDIS_ZIP_SHA256", "SKIP_REDIS_CHECK"):
            self.assertIn(token, self.hint, f"指引缺少可执行步骤：{token}")

    def test_mentions_minimum_version_reason(self):
        self.assertIn("Redis 6", self.hint)
        self.assertIn("HELLO", self.hint)

    def test_offline_zip_path_is_the_real_runtime_dir(self):
        """指引里的落地路径必须与代码实际读取的目录一致（跨平台可跑）。

        实测事故：指引写的是 `.weavind/downloads/redis-windows.zip`（少一个 me），
        照着做的人会把包放进一个**永远不会被读取**的目录，然后继续卡在"Redis 缺失"。
        所以这里同时断言"含真实路径"和"不含错字"，并按 `DOWNLOAD_DIR` 拼出来——
        目录名以后改了，测试会跟着改而不是漂移。
        """
        real = f"{dc.RUNTIME_DIR.name}/downloads/redis-windows.zip"
        self.assertIn(real, self.hint,
                      f"指引应写真实下载目录（{real}）")
        self.assertNotIn(".weavind/", self.hint, "不得出现少了 me 的错字路径")
        self.assertEqual(dc.DOWNLOAD_DIR.name, "downloads")


class TestFetchBudgetDefault(unittest.TestCase):
    def test_default_budget_is_60s(self):
        src = (ROOT / "dep_check.py").read_text(encoding="utf-8")
        self.assertIn('WM_REDIS_FETCH_BUDGET", "60"', src)


if __name__ == "__main__":
    unittest.main()
