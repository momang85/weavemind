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


class TestRedisDataDirAndPersistence(unittest.TestCase):
    """实测缺陷回归：便携 Redis 不给 `--dir` 时，RDB 落不了盘 → MISCONF 挡掉所有写命令。

    症状是"16 个服务都在跑，但一个任务都提交不了"，而且 `dir` 是 Redis 8 的 protected
    config，运行期改不了——只能在启动参数里给对，所以这里把启动参数与启动后自检都钉住。
    """

    def test_argv_carries_explicit_dir_when_given(self):
        argv = dc._redis_start_argv(Path("redis-server.exe"), 6379,
                                   Path("C:/data/redis"))
        self.assertIn("--dir", argv)
        i = argv.index("--dir")
        self.assertEqual(argv[i + 1], "C:/data/redis")
        self.assertEqual(argv[i + 2:i + 4], ["--dbfilename", "dump.rdb"])
        # 反斜杠路径要转成正斜杠（msys2 版对反斜杠解析不可靠）
        argv2 = dc._redis_start_argv(Path("r.exe"), 6379, Path(r"C:\x\y"))
        self.assertEqual(argv2[argv2.index("--dir") + 1], "C:/x/y")

    def test_argv_without_dir_is_unchanged(self):
        """不传 data_dir 时保持旧参数形态（向后兼容，便于单独诊断）。"""
        argv = dc._redis_start_argv(Path("redis-server.exe"), 6379)
        self.assertNotIn("--dir", argv)
        self.assertEqual(argv[:3], ["redis-server.exe", "--port", "6379"])

    def test_data_dir_follows_data_root(self):
        with mock.patch.dict(os.environ, {"WEAVEMIND_DATA_DIR": tempfile.gettempdir()}):
            d = dc._redis_data_dir()
        self.assertTrue(str(d).replace("\\", "/").endswith("/redis"), d)

    def test_data_dir_defaults_under_runtime_dir(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEAVEMIND_DATA_DIR", None)
            d = dc._redis_data_dir()
        # 仓库路径含非 ASCII 时退到纯 ASCII 位置（msys2 对非 ASCII 参数不可靠）
        try:
            str(dc.REDIS_DIR / "data").encode("ascii")
            ascii_repo = True
        except UnicodeEncodeError:
            ascii_repo = False
        if ascii_repo:
            self.assertEqual(d, dc.REDIS_DIR / "data")
        else:
            self.assertEqual(d, Path(os.environ.get("LOCALAPPDATA") or Path.home())
                             / "WeaveMind" / "redis")
            str(d).encode("ascii")       # 退路必须是纯 ASCII

    def test_ascii_fallback_is_used_when_repo_path_is_non_ascii(self):
        """反例：把数据根指到含中文的目录 → 必须退到 ASCII 位置，而不是照给 Redis。"""
        cjk = tempfile.mkdtemp(prefix="wm_redis_cjk_") + "\\中文目录"
        with mock.patch.dict(os.environ, {"WEAVEMIND_DATA_DIR": cjk}):
            d = dc._redis_data_dir()
        try:
            str(d).encode("ascii")
        except UnicodeEncodeError:
            self.fail(f"含中文的数据根必须退到 ASCII 位置，实际给了 {d}")

    def test_persistence_check_flags_wrong_dir(self):
        """dir 不是我们给的那个 → 明确报错（这正是当初 `/portable/…` 的情形）。"""
        with mock.patch.object(dc, "_redis_inline") as inline:
            inline.return_value = "$8\r\ndir\r\n$34\r\n/portable/Redis-x64-msys2\r\n"
            ok, why = dc.verify_redis_persistence(6379, Path("C:/data/redis"))
        self.assertFalse(ok)
        self.assertIn("/portable/Redis-x64-msys2", why)
        self.assertIn("落盘目录不对", why)

    def test_persistence_check_passes_when_bgsave_ok(self):
        def fake(host, port, command, timeout=2.0):
            if command.startswith("CONFIG GET dir"):
                return "$19\r\ndir\r\n$17\r\nC:/data/redis\r\n"
            if command.startswith("INFO persistence"):
                return "rdb_last_bgsave_status:ok\r\nrdb_bgsave_in_progress:0\r\n"
            return "+Background saving started\r\n"

        with mock.patch.object(dc, "_redis_inline", side_effect=fake):
            ok, why = dc.verify_redis_persistence(6379, Path("C:/data/redis"))
        self.assertTrue(ok, why)
        self.assertIn("通过", why)

    def test_persistence_check_reports_failed_bgsave_with_actionable_hint(self):
        """bgsave 失败要给出**可执行**建议（中文路径就换 WEAVEMIND_DATA_DIR）。"""
        def fake(host, port, command, timeout=2.0):
            if command.startswith("CONFIG GET dir"):
                return "$19\r\ndir\r\n$17\r\nC:/data/redis\r\n"
            return "rdb_last_bgsave_status:err\r\nrdb_bgsave_in_progress:0\r\n"

        with mock.patch.object(dc, "_redis_inline", side_effect=fake):
            ok, why = dc.verify_redis_persistence(6379, Path("C:/data/redis"))
        self.assertFalse(ok)
        self.assertIn("err", why)
        self.assertIn("WEAVEMIND_DATA_DIR", why)


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
            # 落盘自检必须也替身掉：否则会去连**真实存在**的 Redis（本机环境耦合），
            # 在 CI 上则变成一次无谓的连接失败。
            mock.patch.object(dc, "verify_redis_persistence",
                              lambda port, d, **k: (True, "自检替身")),
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

    def test_hints_cover_powershell_syntax(self):
        """`set X=Y` 是 cmd 语法，贴进 PowerShell 不会设成环境变量。

        实测踩坑：用户把指引里的 `set WM_PIP_INDEX_URL=...` 与 `%WM_PIP_INDEX_URL%`
        原样贴进 PowerShell，pip 把它当成**本地路径**（`Location '.../redis/' is ignored`），
        看起来像网络不通，其实那一轮根本没联网。指引必须同时给出 PowerShell 写法。
        """
        launcher_src = (ROOT / "launcher.py").read_text(encoding="utf-8")
        for text in (self.hint, launcher_src):
            self.assertIn("PowerShell", text)
            self.assertIn("$env:", text)

    def test_hints_do_not_recommend_a_redis5_build(self):
        """tporadowski/redis 是 Redis 5.x，与"需 Redis ≥6"自相矛盾，只能当反例出现。

        实测后果：照着一键指引装它，服务"启动即崩、日志报 unknown command HELLO"。
        依赖自检与启动预检两处文案都要带这个告诫，避免又被当成"任选其一"的方案。
        """
        launcher_src = (ROOT / "launcher.py").read_text(encoding="utf-8")
        for text in (self.hint, launcher_src):
            self.assertIn("tporadowski", text)
            self.assertIn("Redis 5", text, "要点明它是 Redis 5.x")
            self.assertTrue("不能用" in text or "不要用" in text,
                            "必须明确说不能用，而不是作为方案列出")
            self.assertIn("6", text)

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


class TestRedisSkipSwitch(unittest.TestCase):
    """SKIP_REDIS_CHECK=1 必须真的能让**依赖自检**放行（start.bat 的 [4/6] 是闸门）。

    失败指引让用户设这个开关，但此前只有 launcher 的启动预检认它：照做的人在
    start.bat 里仍被"必需依赖缺失"挡住，只能绕过 start.bat 直接起 launcher。
    语义与 launcher 一致（显式设 1 即跳过），差别是这里必须**打印代价**，不静默。
    """

    def _env_without_skip(self):
        return {k: v for k, v in os.environ.items() if k != "SKIP_REDIS_CHECK"}

    def test_skip_passes_and_says_what_it_costs(self):
        import contextlib
        import io as _io
        buf = _io.StringIO()
        with mock.patch.dict(os.environ, {"SKIP_REDIS_CHECK": "1"}):
            with contextlib.redirect_stdout(buf):
                res = dc.ensure_redis(auto=True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["action"], "skipped")
        printed = buf.getvalue()
        self.assertIn("SKIP_REDIS_CHECK", printed, "跳过必须打印，不静默")
        self.assertIn("worker", printed, "要说清代价：worker/队列不可用")

    def test_skip_does_not_try_to_download(self):
        """跳过时不应再走下载（新手常没有可用通道，白等 60 秒预算）。"""
        with mock.patch.dict(os.environ, {"SKIP_REDIS_CHECK": "1"}), \
                mock.patch.object(dc, "fetch_portable_redis",
                                  side_effect=AssertionError("不应发起下载")), \
                mock.patch.object(dc, "redis_ping", lambda *a, **k: False), \
                mock.patch("builtins.print"):
            res = dc.ensure_redis(auto=True)
        self.assertTrue(res["ok"])

    def test_without_skip_missing_redis_still_fails(self):
        with mock.patch.dict(os.environ, self._env_without_skip(), clear=True), \
                mock.patch.dict(os.environ, {"WM_NO_AUTO_DOWNLOAD": "1"}), \
                mock.patch.object(dc, "redis_ping", lambda *a, **k: False), \
                mock.patch.object(dc, "_system_redis_exe", lambda: None), \
                mock.patch.object(dc, "_usable_portable_redis", lambda: None), \
                mock.patch("builtins.print"):
            res = dc.ensure_redis(auto=True)
        self.assertFalse(res["ok"], "没设开关时必须照旧失败")

    def test_report_only_mode_also_honours_the_switch(self):
        with mock.patch.dict(os.environ, {"SKIP_REDIS_CHECK": "1"}), \
                mock.patch.object(dc, "_migrate_legacy_runtime_dir", lambda: None), \
                mock.patch("builtins.print"):
            rep = dc.ensure_all(auto=False)
        self.assertTrue(rep["redis"]["ok"], rep["redis"])
        self.assertEqual(rep["redis"]["action"], "skipped")


if __name__ == "__main__":
    unittest.main()
