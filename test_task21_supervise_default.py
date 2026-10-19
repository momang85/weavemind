# -*- coding: utf-8 -*-
"""任务21 验收：launcher supervise 设为默认启动模式 + 关键进程重启留痕告警。

覆盖 docs/arch_phase4_task21_supervise_default.md 的四条：
  ① start.bat 默认带 SUPERVISE 标志，且 WEAVEMIND_SUPERVISE=0 仍是关闭出口；
  ② 守护检测到进程消失后**立即**重启（首轮巡检不睡眠，重启落在
     WM_START_VERIFY_WAIT 内，而不是等满一个 30 秒巡检间隔）；
  ③ orchestrator/critic 被重启后写 audit_log（action=process_restarted），
     并向 guardian 用的同一通道 orchestrator:alert 发布告警；Redis 不可用时
     告警发不出去，logger.error 与审计仍落地（已知限制）；
  ④ README / docs/部署指南.md 写明守护是推荐启动方式与关闭开关。

全部 mock（_is_alive / _spawn_service / 告警发布 / 时间），不起真实进程、
不连真实 Redis、不写真实审计文件。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent


def _env(**overrides):
    """隔离 WEAVEMIND_SUPERVISE：默认按"未设置"处理，需要时显式给值。

    用 os.environ 的裁剪副本 + clear=True，这样即便 CI/宿主环境里已经设了
    WEAVEMIND_SUPERVISE=0（旧默认语义），也不会污染"未设置=开启"的断言。
    """
    env = dict(os.environ)
    env.pop("WEAVEMIND_SUPERVISE", None)
    env.update(overrides)
    return mock.patch.dict(os.environ, env, clear=True)


def _services(launcher, names=("webui", "orchestrator")):
    """与 build_services 同构的少量服务清单（守护只监督它拿到的列表）。"""
    return [
        (name, [sys.executable, str(launcher.BASE_DIR / f"{name}.py")],
         launcher.BASE_DIR, None)
        for name in names
    ]


class _FakeTime:
    """守护循环的假时钟：记录 sleep 顺序，其余（time/monotonic）委托真实模块。"""

    def __init__(self):
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def __getattr__(self, item):
        return getattr(time, item)


class TestSuperviseEnabledDefault(unittest.TestCase):
    """① 默认开关：未设置即开启，=0/off/false/no 显式关闭。"""

    def test_unset_means_supervise_on(self):
        import launcher
        with _env():
            self.assertTrue(launcher._supervise_enabled(),
                            "任务21 起守护模式是默认（task20 真机暴露无人重启）")

    def test_explicit_on_values_keep_supervise(self):
        import launcher
        for raw in ("1", "true", "TRUE", "yes", "on"):
            with _env(WEAVEMIND_SUPERVISE=raw):
                self.assertTrue(launcher._supervise_enabled(), raw)

    def test_off_values_disable_supervise(self):
        """约束：WEAVEMIND_SUPERVISE=0 保持可用（CI 与单测不需要守护）。"""
        import launcher
        for raw in ("0", "false", "False", "no", "off", " 0 "):
            with _env(WEAVEMIND_SUPERVISE=raw):
                self.assertFalse(launcher._supervise_enabled(), raw)

    def test_start_action_supervises_by_default(self):
        """`launcher.py start` 未设置环境变量也进守护；=0 回到一次性启动。"""
        import launcher
        for env_value, expect_supervise in ((None, True), ("0", False), ("1", True)):
            overrides = {} if env_value is None else {"WEAVEMIND_SUPERVISE": env_value}
            with _env(**overrides), \
                    mock.patch.object(launcher, "_run_dependency_check"), \
                    mock.patch.object(launcher, "_check_redis_or_exit"), \
                    mock.patch.object(launcher, "supervise_services") as sup, \
                    mock.patch.object(launcher, "start_services") as start, \
                    mock.patch.object(sys, "argv", ["launcher.py", "start"]):
                launcher.main()
            if expect_supervise:
                sup.assert_called_once()
                start.assert_not_called()
            else:
                start.assert_called_once()
                sup.assert_not_called()


class TestStartBatSuperviseFlag(unittest.TestCase):
    """① start.bat：新人入口默认开启守护，且保留显式关闭。"""

    def _text(self) -> str:
        return (ROOT / "start.bat").read_text(encoding="utf-8")

    def test_contains_supervise_flag_default(self):
        text = self._text()
        self.assertIn("WEAVEMIND_SUPERVISE", text, "start.bat 必须包含 SUPERVISE 标志")
        self.assertIn(
            'if not defined WEAVEMIND_SUPERVISE set "WEAVEMIND_SUPERVISE=1"', text,
            "默认开启，但不得覆盖用户已经显式设置的值（=0 仍可用）")

    def test_flag_is_set_before_the_launch_command(self):
        import re
        lines = self._text().splitlines()
        flag = next(i for i, l in enumerate(lines) if "WEAVEMIND_SUPERVISE=" in l)
        # 匹配真正的启动命令行（%PY% launcher.py up），别被 REM 注释里的字样骗到
        up = next(i for i, l in enumerate(lines)
                  if re.match(r"^\s*%PY%\s+launcher\.py up\b", l))
        self.assertLess(flag, up, "标志必须在启动命令之前设置")

    def test_start_bat_stays_ascii_only(self):
        """非 ASCII 在 .bat 里按控制台代码页（GBK）解码会变乱码。"""
        (ROOT / "start.bat").read_bytes().decode("ascii")


class TestRestartAuditAndAlert(unittest.TestCase):
    """③ 关键进程重启：审计留痕 + orchestrator:alert 告警 + logger.error 兜底。"""

    def _tmp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_t21_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def _audit_rows(self, audit_file: Path) -> list[dict]:
        if not audit_file.exists():
            return []
        return [json.loads(line) for line in
                audit_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_orchestrator_restart_is_audited_and_alerted(self):
        import launcher
        tmp = self._tmp()
        audit_file = tmp / "audit.jsonl"
        with mock.patch.object(launcher, "_is_alive", return_value=False), \
                mock.patch.object(launcher, "_spawn_service", return_value=333), \
                mock.patch.object(launcher, "_publish_restart_alert") as pub, \
                mock.patch("audit_logger.AUDIT_FILE", str(audit_file)), \
                self.assertLogs("launcher", level="ERROR") as cm:
            out, changed = launcher._supervise_once(
                {"services": {"orchestrator": 222}},
                _services(launcher, ("orchestrator",)), {})
        self.assertTrue(changed)
        self.assertEqual(out["services"]["orchestrator"], 333)
        rows = self._audit_rows(audit_file)
        self.assertEqual(len(rows), 1, "关键进程重启必须留下一条审计")
        self.assertEqual(rows[0]["action"], "process_restarted")
        self.assertEqual(rows[0]["target"], "orchestrator")
        self.assertEqual(rows[0]["user"], "supervise")
        self.assertEqual(rows[0]["result"], "ok")
        self.assertIn("旧 pid=222", rows[0]["detail"])
        pub.assert_called_once()
        self.assertEqual(pub.call_args.args[0], "orchestrator")
        self.assertTrue(any("关键进程 orchestrator 已被守护重启" in line for line in cm.output),
                        "Redis 不可用时至少有本地 logger.error 兜底")

    def test_critic_restart_is_audited_too(self):
        import launcher
        tmp = self._tmp()
        audit_file = tmp / "audit.jsonl"
        with mock.patch.object(launcher, "_is_alive", return_value=False), \
                mock.patch.object(launcher, "_spawn_service", return_value=444), \
                mock.patch.object(launcher, "_publish_restart_alert"), \
                mock.patch("audit_logger.AUDIT_FILE", str(audit_file)), \
                self.assertLogs("launcher", level="ERROR"):
            launcher._supervise_once({"services": {"critic": 55}},
                                     _services(launcher, ("critic",)), {})
        rows = self._audit_rows(audit_file)
        self.assertEqual([r["target"] for r in rows], ["critic"])

    def test_non_key_service_restart_does_not_alert(self):
        """告警只给编排核心（orchestrator/critic）；worker 重启不刷屏。"""
        import launcher
        tmp = self._tmp()
        audit_file = tmp / "audit.jsonl"
        with mock.patch.object(launcher, "_is_alive", return_value=False), \
                mock.patch.object(launcher, "_spawn_service", return_value=777), \
                mock.patch.object(launcher, "_publish_restart_alert") as pub, \
                mock.patch("audit_logger.AUDIT_FILE", str(audit_file)), \
                self.assertLogs("launcher", level="INFO"):
            _out, changed = launcher._supervise_once({"services": {"webui": 11}},
                                                     _services(launcher, ("webui",)), {})
        self.assertTrue(changed)
        pub.assert_not_called()
        self.assertEqual(self._audit_rows(audit_file), [])

    def test_restart_is_not_blocked_when_alert_channel_is_down(self):
        """已知限制：Redis 挂了告警通道也发不出去——巡检不得因此中断或回滚重启。"""
        import launcher
        tmp = self._tmp()
        with mock.patch.object(launcher, "_is_alive", return_value=False), \
                mock.patch.object(launcher, "_spawn_service", return_value=333), \
                mock.patch.object(launcher, "_publish_restart_alert",
                                  side_effect=ConnectionError("redis down")), \
                mock.patch("audit_logger.AUDIT_FILE", str(tmp / "audit.jsonl")), \
                self.assertLogs("launcher", level="WARNING") as cm:
            out, changed = launcher._supervise_once(
                {"services": {"orchestrator": 222}},
                _services(launcher, ("orchestrator",)), {})
        self.assertTrue(changed)
        self.assertEqual(out["services"]["orchestrator"], 333)
        self.assertEqual(self._audit_rows(tmp / "audit.jsonl")[0]["action"],
                         "process_restarted", "告警失败时审计仍然落地")
        self.assertTrue(any("告警发布失败" in line for line in cm.output))

    def test_audit_failure_is_also_tolerated(self):
        import launcher
        with mock.patch.object(launcher, "_is_alive", return_value=False), \
                mock.patch.object(launcher, "_spawn_service", return_value=333), \
                mock.patch("audit_logger.audit_log",
                          side_effect=OSError("disk full")), \
                mock.patch.object(launcher, "_publish_restart_alert"), \
                self.assertLogs("launcher", level="WARNING") as cm:
            _out, changed = launcher._supervise_once(
                {"services": {"orchestrator": 222}},
                _services(launcher, ("orchestrator",)), {})
        self.assertTrue(changed)
        self.assertTrue(any("审计写入失败" in line for line in cm.output))

    def test_alert_uses_guardian_channel_and_no_retry(self):
        """通道与 payload 对齐 guardian._alert；裸客户端必须显式关掉内建重试。"""
        import launcher
        fake_client = mock.MagicMock()
        with mock.patch("redis.Redis", return_value=fake_client) as redis_cls:
            launcher._publish_restart_alert("orchestrator", 222, 333, 1)
        channel, raw = fake_client.publish.call_args.args
        self.assertEqual(channel, "orchestrator:alert")
        payload = json.loads(raw)  # 不抛 = 合法 JSON（缺陷A：修复前直接发 dict）
        self.assertEqual(payload["type"], "process_restarted")
        self.assertEqual(payload["service"], "launcher")
        self.assertEqual(payload["target"], "orchestrator")
        self.assertEqual(payload["new_pid"], 333)
        kwargs = redis_cls.call_args.kwargs
        self.assertIn("retry", kwargs,
                      "全仓守卫：不传 retry 的 redis.Redis 会在 Redis 不可达时挂死巡检线程")
        self.assertLessEqual(kwargs["socket_connect_timeout"], 2.0)
        self.assertLessEqual(kwargs["socket_timeout"], 2.0)

    def test_alert_payload_is_valid_json(self):
        """缺陷A（真机 E2E 复现）：dict 直接交给 redis.publish 会 TypeError
        （"Invalid input of type: 'dict'"），告警从未到达 orchestrator:alert；
        修复后必须发 JSON 字符串，与 guardian._alert / web_ui 的同通道口径一致。"""
        import launcher
        fake_client = mock.MagicMock()
        with mock.patch("redis.Redis", return_value=fake_client):
            launcher._publish_restart_alert("critic", 111, 222, 2)
        channel, raw = fake_client.publish.call_args.args
        self.assertEqual(channel, "orchestrator:alert")
        self.assertIsInstance(raw, str, "发布载荷必须是字符串（redis-py 不收 dict）")
        payload = json.loads(raw)  # 不抛 = 合法 JSON
        self.assertEqual(payload["type"], "process_restarted")
        self.assertEqual(payload["service"], "launcher")
        self.assertEqual(payload["target"], "critic")
        self.assertEqual(payload["old_pid"], 111)
        self.assertEqual(payload["new_pid"], 222)
        self.assertIn("timestamp", payload)


class TestFirstPollRestartsWithinVerifyWait(unittest.TestCase):
    """② 进程消失后在 WM_START_VERIFY_WAIT 内重启：首轮巡检不睡眠。"""

    def _tmp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_t21_loop_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def test_restart_happens_before_any_sleep(self):
        import launcher
        tmp = self._tmp()
        fake = _FakeTime()
        dead = {"services": {"orchestrator": 222}}
        # supervise_services 的读取顺序：单例守卫 → 记录自身 pid → 首轮巡检 → 空记录退出
        reads = [{}, dead, dead, {}]
        order: list[list[float]] = []

        def _spawn(*_args, **_kwargs):
            order.append(list(fake.sleeps))
            return 333

        started = time.monotonic()
        with mock.patch.object(launcher, "PID_FILE", tmp / "pids.json"), \
                mock.patch.object(launcher, "PID_DIR", tmp), \
                mock.patch.object(launcher, "_read_pids",
                                  side_effect=lambda: reads.pop(0) if reads else {}), \
                mock.patch.object(launcher, "_is_alive", return_value=False), \
                mock.patch.object(launcher, "_spawn_service", side_effect=_spawn), \
                mock.patch.object(launcher, "_write_pids") as write, \
                mock.patch.object(launcher, "start_services"), \
                mock.patch.object(launcher, "build_services",
                                  return_value=_services(launcher, ("orchestrator",))), \
                mock.patch.object(launcher, "_supervise_interval", return_value=30.0), \
                mock.patch.object(launcher, "_publish_restart_alert"), \
                mock.patch("audit_logger.AUDIT_FILE", str(tmp / "audit.jsonl")), \
                mock.patch.object(launcher, "time", fake), \
                mock.patch("logging_setup.setup_logging"), \
                self.assertLogs("launcher", level="INFO"):
            launcher.supervise_services()
        elapsed = time.monotonic() - started

        self.assertEqual(len(order), 1, "崩溃的编排器应在首轮巡检就被重启")
        self.assertEqual(order[0], [], "重启必须发生在任何睡眠之前（默认间隔 30 秒 > 验证等待）")
        wait = float(os.environ.get("WM_START_VERIFY_WAIT", "8") or 8)
        self.assertLess(elapsed, wait,
                        f"重启耗时 {elapsed:.2f}s 必须落在 WM_START_VERIFY_WAIT={wait:.0f}s 内")
        # 自己的守护 pid 被登记（stop 要先收掉它），并在退出守护时清空服务记录后不再回写
        self.assertTrue(write.called)

    def test_supervisor_pid_is_recorded_under_supervisor_key(self):
        import launcher
        tmp = self._tmp()
        pid_file = tmp / "pids.json"
        pid_file.write_text(json.dumps({"services": {"orchestrator": 222}}), encoding="utf-8")
        reads: list[dict] = [{}, {"services": {"orchestrator": 222}}, {}]
        with mock.patch.object(launcher, "PID_FILE", pid_file), \
                mock.patch.object(launcher, "PID_DIR", tmp), \
                mock.patch.object(launcher, "_read_pids",
                                  side_effect=lambda: reads.pop(0) if reads else {}), \
                mock.patch.object(launcher, "_is_alive", return_value=True), \
                mock.patch.object(launcher, "start_services"), \
                mock.patch.object(launcher, "build_services", return_value=[]), \
                mock.patch.object(launcher, "_supervise_interval", return_value=0.001), \
                mock.patch.object(launcher, "time", _FakeTime()), \
                mock.patch("logging_setup.setup_logging"), \
                self.assertLogs("launcher", level="INFO"):
            launcher.supervise_services()
        self.assertEqual(json.loads(pid_file.read_text(encoding="utf-8"))["supervisor"],
                         os.getpid(), "守护要把自己的 pid 留给 stop/复用判断")

    def test_singleton_guard_skips_second_supervisor(self):
        """已有存活守护（pids.json supervisor 键）时第二个实例直接退出，不重复巡检。"""
        import launcher
        other_pid = os.getpid() ^ 1  # 必然不等于自身
        with mock.patch.object(launcher, "_read_pids",
                              return_value={"supervisor": other_pid,
                                            "services": {"webui": 111}}), \
                mock.patch.object(launcher, "_is_alive", return_value=True), \
                mock.patch.object(launcher, "start_services") as start, \
                mock.patch("logging_setup.setup_logging"), \
                self.assertLogs("launcher", level="WARNING") as cm:
            launcher.supervise_services()
        start.assert_not_called()
        self.assertTrue(any("已有守护进程" in line for line in cm.output))


class TestUpStartsDetachedSupervisor(unittest.TestCase):
    """①/② start.bat 走 `up`（一次性控制器）：守护必须作为独立后台进程接上。"""

    def _tmp(self, pids: dict):
        tmp = Path(tempfile.mkdtemp(prefix="wm_t21_up_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pid_file = tmp / "pids.json"
        pid_file.write_text(json.dumps(pids), encoding="utf-8")
        return tmp, pid_file

    def _run_up(self, launcher, state: str, tmp: Path, pid_file: Path, **overrides):
        with _env(**overrides), \
                mock.patch.object(launcher, "PID_FILE", pid_file), \
                mock.patch.object(launcher, "PID_DIR", tmp), \
                mock.patch.object(launcher, "startup_controller",
                                  return_value={"state": state, "url": "http://x"}), \
                mock.patch.object(launcher, "web_url", return_value="http://x"), \
                mock.patch.object(launcher, "_is_alive", return_value=False), \
                mock.patch.object(launcher, "_spawn_service", return_value=4242) as spawn, \
                mock.patch.object(sys, "argv", ["launcher.py", "up"]), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit) as cm:
                launcher.main()
        return cm.exception.code, spawn

    def test_up_starts_detached_supervisor_and_records_pid(self):
        import launcher
        tmp, pid_file = self._tmp({"services": {"webui": 111}})
        code, spawn = self._run_up(launcher, "research_ready", tmp, pid_file)
        self.assertEqual(code, 0)
        spawn.assert_called_once()
        name, argv = spawn.call_args.args[0], spawn.call_args.args[1]
        self.assertEqual(name, "supervisor")
        self.assertTrue(any(str(a).endswith("launcher.py") for a in argv))
        self.assertIn("supervise", [str(a) for a in argv])
        self.assertEqual(json.loads(pid_file.read_text(encoding="utf-8"))["supervisor"], 4242,
                         "守护 pid 要落进 pids.json，供 stop 先收掉、供复用判断")

    def test_up_does_not_spawn_a_second_supervisor(self):
        import launcher
        tmp, pid_file = self._tmp({"services": {"webui": 111}, "supervisor": 4242})
        with _env(), \
                mock.patch.object(launcher, "PID_FILE", pid_file), \
                mock.patch.object(launcher, "PID_DIR", tmp), \
                mock.patch.object(launcher, "startup_controller",
                                  return_value={"state": "research_ready", "url": "http://x"}), \
                mock.patch.object(launcher, "web_url", return_value="http://x"), \
                mock.patch.object(launcher, "_is_alive", return_value=True), \
                mock.patch.object(launcher, "_spawn_service") as spawn, \
                mock.patch.object(sys, "argv", ["launcher.py", "up"]), \
                mock.patch("builtins.print"):
            with self.assertRaises(SystemExit):
                launcher.main()
        spawn.assert_not_called()
        self.assertEqual(json.loads(pid_file.read_text(encoding="utf-8"))["supervisor"], 4242)

    def test_up_with_supervise_zero_does_not_spawn(self):
        """约束：WEAVEMIND_SUPERVISE=0 保持可用（调试时不想自动重启）。"""
        import launcher
        tmp, pid_file = self._tmp({"services": {"webui": 111}})
        code, spawn = self._run_up(launcher, "research_ready", tmp, pid_file,
                                   WEAVEMIND_SUPERVISE="0")
        self.assertEqual(code, 0)
        spawn.assert_not_called()
        self.assertNotIn("supervisor", json.loads(pid_file.read_text(encoding="utf-8")))

    def test_failed_startup_does_not_spawn_supervisor(self):
        """起不来的环境不反复 spawn：先按就绪结论的"下一步"处理。"""
        import launcher
        tmp, pid_file = self._tmp({"services": {}})
        _code, spawn = self._run_up(launcher, "failed", tmp, pid_file)
        spawn.assert_not_called()


class TestStopTakesSupervisorDown(unittest.TestCase):
    """守护成为默认后，stop 必须先收守护，否则停止会被"复活"。"""

    def test_supervisor_is_killed_before_services(self):
        import launcher
        tmp = Path(tempfile.mkdtemp(prefix="wm_t21_stop_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        killed: list[int] = []

        def _kill(pid):
            killed.append(pid)
            return True

        with mock.patch.object(launcher, "PID_FILE", tmp / "pids.json"), \
                mock.patch.object(launcher, "_read_pids",
                                  return_value={"supervisor": 4242,
                                                "services": {"webui": 111,
                                                             "orchestrator": 222}}), \
                mock.patch.object(launcher, "_kill_pid", side_effect=_kill), \
                mock.patch.object(launcher, "_scan_residual_processes", return_value=[]), \
                mock.patch.object(launcher, "_stop_portable_redis",
                                  return_value={"stopped": False}), \
                mock.patch.object(launcher, "_verified_stop_report", return_value=[]), \
                self.assertLogs("launcher", level="INFO"):
            stopped = launcher.stop_services()
        self.assertEqual(killed[0], 4242, "守护必须最先被收掉")
        self.assertIn("supervisor", stopped)
        self.assertIn("orchestrator", stopped)


class TestDocsDocumentSuperviseDefault(unittest.TestCase):
    """③ README / 部署指南：推荐方式 + 关闭开关 + 留痕位置都要写明。"""

    def _docs(self):
        return {
            "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
            "docs/部署指南.md": (ROOT / "docs" / "部署指南.md").read_text(encoding="utf-8"),
        }

    def test_both_docs_carry_default_and_off_switch(self):
        for name, text in self._docs().items():
            self.assertIn("WEAVEMIND_SUPERVISE=0", text, f"{name} 必须写明关闭开关")
            self.assertIn("守护", text, f"{name} 必须说明守护模式")
            self.assertIn("默认", text, f"{name} 必须写明这是默认启动方式")

    def test_restart_trail_is_documented(self):
        guide = self._docs()["docs/部署指南.md"]
        self.assertIn("process_restarted", guide, "重启留痕的 action 名要写进运维说明")
        self.assertIn("audit.jsonl", guide)
        self.assertIn("orchestrator:alert", guide)


if __name__ == "__main__":
    unittest.main(verbosity=2)
