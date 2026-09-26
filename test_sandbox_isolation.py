# -*- coding: utf-8 -*-
"""代码沙箱的隔离语义（架构审查 P1-4 + 架构师指令 2026-09-14）。

面向机构使用的安全默认值：代码由模型生成，属不可信代码。

- **未设置 / `docker`**：要求容器隔离。隔离不可用（CLI 缺失、守护进程不可达、镜像未构建、
  执行层启动异常、容器层失败）时**拒绝执行并报错**，宿主进程创建次数必须为 0；
- **取值非法**：按配置错误拒绝执行，不静默放宽；
- **`restricted` / `none`**：只能由操作者显式选择，结果里明确"无操作系统级隔离"；
- **容器内脚本自身报错**：原样返回，不当作设施故障重跑、不拒绝；
- 拒绝提示只给"恢复隔离"的出路，不诱导关闭隔离。

同步与异步入口同一策略；另覆盖 worker 的失败回报链（失败可见、不假报成功）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import code_sandbox

_SANDBOX_VARS = ("CODE_EXECUTION_SANDBOX", "CODE_SANDBOX_IMAGE")
_LAYER_ERR = b"Cannot connect to the Docker daemon at npipe:////./pipe/docker_engine"


def _env(**over):
    """干净环境（去掉沙箱相关变量）后按需覆盖。"""
    base = {k: v for k, v in os.environ.items() if k not in _SANDBOX_VARS}
    base.update(over)
    return mock.patch.dict(os.environ, base, clear=True)


class _FakeProc:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err
        self.pid = 4321

    async def communicate(self, input=None):
        return self.stdout, self.stderr

    def kill(self):
        pass


class _SandboxTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_sandbox_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        self.script = tmp / "s.py"
        self.script.write_text("print('ok-42')\n", encoding="utf-8")
        code_sandbox.clear_sandbox_caches()
        self.addCleanup(code_sandbox.clear_sandbox_caches)

    @staticmethod
    def _host_runs(mock_run) -> list:
        """从 subprocess.run 调用记录里挑出"宿主解释器执行脚本"的那些。"""
        return [c for c in mock_run.call_args_list
                if c.args and isinstance(c.args[0], list) and c.args[0]
                and c.args[0][0] == sys.executable]

    @staticmethod
    def _docker_unavailable():
        return mock.patch.object(code_sandbox.shutil, "which", return_value=None)

    @staticmethod
    def _docker_ok_image_ok():
        """docker 可用且镜像存在（预检通过，用于测试后续失败分支）。"""
        return (
            mock.patch.object(code_sandbox, "docker_available", return_value=True),
            mock.patch.object(code_sandbox, "ensure_sandbox_image", return_value=True),
        )

    def _assert_no_host_execution(self, mock_run):
        self.assertEqual(self._host_runs(mock_run), [],
                         "隔离不可用时不得改在宿主解释器里执行模型生成的代码")


class TestDefaultRequiresIsolation(_SandboxTest):
    """默认（未设置 CODE_EXECUTION_SANDBOX）即要求隔离。"""

    def test_refuses_when_docker_cli_missing(self):
        with _env(), self._docker_unavailable(), \
                mock.patch.object(code_sandbox.subprocess, "run") as m_run:
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                code_sandbox.run_script(str(self.script), str(self.tmp), timeout=5)
        m_run.assert_not_called()
        self._assert_no_host_execution(m_run)
        msg = str(ctx.exception)
        self.assertIn("恢复隔离", msg)
        self.assertIn("Dockerfile.sandbox", msg)
        # 不得在失败提示里诱导用户关闭隔离
        self.assertNotIn("restricted", msg)
        self.assertNotIn("none", msg)

    def test_refuses_when_daemon_unreachable(self):
        def fake_run(cmd, *a, **kw):
            return subprocess.CompletedProcess(cmd, 1, b"", _LAYER_ERR)

        with _env(), \
                mock.patch.object(code_sandbox.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(code_sandbox.subprocess, "run", side_effect=fake_run) as m_run:
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                code_sandbox.run_script(str(self.script), str(self.tmp), timeout=5)
        self._assert_no_host_execution(m_run)
        self.assertIn("docker 不可用", str(ctx.exception))

    def test_refuses_when_image_missing(self):
        def fake_run(cmd, *a, **kw):
            rc = 0 if list(cmd[:2]) == ["docker", "version"] else 1
            return subprocess.CompletedProcess(cmd, rc, b"", b"")

        with _env(), \
                mock.patch.object(code_sandbox.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(code_sandbox.subprocess, "run", side_effect=fake_run) as m_run:
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                code_sandbox.run_script(str(self.script), str(self.tmp), timeout=5)
        self._assert_no_host_execution(m_run)
        self.assertIn("镜像", str(ctx.exception))

    def test_policy_mode_is_docker_even_without_docker(self):
        with _env(), self._docker_unavailable():
            self.assertEqual(code_sandbox.sandbox_mode(), "docker")
            self.assertTrue(code_sandbox.isolation_required())
            self.assertFalse(code_sandbox.isolation_ready()[0])


class TestExplicitDockerRefuses(_SandboxTest):
    def test_refuses_on_spawn_exception(self):
        p1, p2 = self._docker_ok_image_ok()
        with _env(CODE_EXECUTION_SANDBOX="docker"), p1, p2, \
                mock.patch.object(code_sandbox.subprocess, "run",
                                  side_effect=FileNotFoundError("docker 不存在")) as m_run:
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                code_sandbox.run_script(str(self.script), str(self.tmp), timeout=5)
        self._assert_no_host_execution(m_run)
        self.assertIn("启动失败", str(ctx.exception))

    def test_refuses_on_container_layer_failure(self):
        p1, p2 = self._docker_ok_image_ok()
        with _env(CODE_EXECUTION_SANDBOX="docker"), p1, p2, \
                mock.patch.object(code_sandbox.subprocess, "run",
                                  return_value=subprocess.CompletedProcess(
                                      ["docker", "run"], 125, b"", _LAYER_ERR)) as m_run:
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                code_sandbox.run_script(str(self.script), str(self.tmp), timeout=5)
        self._assert_no_host_execution(m_run)
        self.assertIn("docker 执行层失败", str(ctx.exception))

    def test_container_script_error_is_returned_as_is(self):
        """容器内脚本自身报错（非 0 + Traceback）原样返回：不拒绝、不重跑。"""
        p1, p2 = self._docker_ok_image_ok()
        failed = subprocess.CompletedProcess(
            ["docker", "run"], 1, b"", b"Traceback (most recent call last):\nValueError: boom")
        with _env(CODE_EXECUTION_SANDBOX="docker"), p1, p2, \
                mock.patch.object(code_sandbox.subprocess, "run", return_value=failed) as m_run:
            r = code_sandbox.run_script(str(self.script), str(self.tmp), timeout=5)
        self.assertEqual(r.returncode, 1)
        self.assertIn(b"ValueError", r.stderr)
        self.assertEqual(r.sandbox_mode, "docker")
        self.assertEqual(m_run.call_count, 1, "容器层失败才拒绝；脚本报错不得重跑")


class TestInvalidConfigRefuses(_SandboxTest):
    def test_invalid_value_is_config_error_and_refuses(self):
        with _env(CODE_EXECUTION_SANDBOX="banana"), self._docker_unavailable(), \
                mock.patch.object(code_sandbox.subprocess, "run") as m_run:
            with self.assertRaises(code_sandbox.SandboxConfigError):
                code_sandbox.sandbox_mode()
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                code_sandbox.run_script(str(self.script), str(self.tmp), timeout=5)
        m_run.assert_not_called()
        self._assert_no_host_execution(m_run)
        self.assertIn("取值非法", str(ctx.exception))

    def test_status_reports_config_error(self):
        with _env(CODE_EXECUTION_SANDBOX="banana"):
            st = code_sandbox.sandbox_status()
        self.assertEqual(st["mode"], "invalid")
        self.assertTrue(st["isolation_required"])
        self.assertFalse(st["isolation_ready"])
        self.assertIn("取值非法", st["config_error"])


class TestAsyncSamePolicy(_SandboxTest):
    def test_async_default_refuses_without_spawning(self):
        async def _spawn(*a, **kw):
            raise AssertionError("隔离不可用时不得创建任何进程")

        with _env(), self._docker_unavailable(), \
                mock.patch("asyncio.create_subprocess_exec", side_effect=_spawn):
            with self.assertRaises(code_sandbox.SandboxUnavailable):
                asyncio.run(code_sandbox.run_script_async(str(self.script), str(self.tmp)))

    def test_async_refuses_on_spawn_exception(self):
        p1, p2 = self._docker_ok_image_ok()

        async def _spawn(*a, **kw):
            raise FileNotFoundError("docker 不存在")

        with _env(CODE_EXECUTION_SANDBOX="docker"), p1, p2, \
                mock.patch("asyncio.create_subprocess_exec", side_effect=_spawn):
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                asyncio.run(code_sandbox.run_script_async(str(self.script), str(self.tmp)))
        self.assertIn("启动失败", str(ctx.exception))

    def test_async_container_layer_failure_refuses_instead_of_rerunning(self):
        wrapper = code_sandbox._AsyncSandboxProc(_FakeProc(125, err=_LAYER_ERR))

        async def _spawn(*a, **kw):
            raise AssertionError("容器层失败不得改跑宿主")

        with _env(CODE_EXECUTION_SANDBOX="docker"), \
                mock.patch("asyncio.create_subprocess_exec", side_effect=_spawn):
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                asyncio.run(wrapper.communicate())
        self.assertIn("docker 执行层失败", str(ctx.exception))

    def test_async_script_error_returned_as_is(self):
        wrapper = code_sandbox._AsyncSandboxProc(
            _FakeProc(1, out=b"", err=b"Traceback (most recent call last):\nValueError: boom"))
        with _env(CODE_EXECUTION_SANDBOX="docker"):
            out, err = asyncio.run(wrapper.communicate())
        self.assertEqual(wrapper.returncode, 1)
        self.assertIn(b"ValueError", err)

    def test_async_explicit_restricted_uses_host(self):
        spawned = []

        async def _spawn(*args, **kw):
            spawned.append(args)
            return _FakeProc(0, out=b"ok-42")

        with _env(CODE_EXECUTION_SANDBOX="restricted"), \
                mock.patch("asyncio.create_subprocess_exec", side_effect=_spawn):
            proc, meta = asyncio.run(
                code_sandbox.run_script_async(str(self.script), str(self.tmp)))
            out, _ = asyncio.run(proc.communicate())
        self.assertEqual(len(spawned), 1)
        self.assertEqual(meta["sandbox_mode"], "restricted")
        self.assertIn("无操作系统级隔离", meta["sandbox_note"])
        self.assertIn(b"ok-42", out)


class TestExplicitNonIsolatedModes(_SandboxTest):
    """restricted / none 仅限操作者显式选择，且必须如实标注无 OS 级隔离。"""

    def _run(self, mode: str):
        def fake_run(cmd, *a, **kw):
            return subprocess.CompletedProcess(cmd, 0, b"ok-42", b"")

        with _env(CODE_EXECUTION_SANDBOX=mode), \
                mock.patch.object(code_sandbox.subprocess, "run", side_effect=fake_run) as m_run:
            r = code_sandbox.run_script(str(self.script), str(self.tmp), timeout=30)
        return r, m_run

    def test_restricted_runs_on_host_with_no_isolation_note(self):
        r, m_run = self._run("restricted")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.sandbox_mode, "restricted")
        self.assertEqual(len(self._host_runs(m_run)), 1)
        self.assertIn("无操作系统级隔离", r.sandbox_note)
        with _env(CODE_EXECUTION_SANDBOX="restricted"):
            self.assertFalse(code_sandbox.isolation_required())

    def test_none_runs_on_host_with_no_isolation_note(self):
        r, _ = self._run("none")
        self.assertEqual(r.sandbox_mode, "none")
        self.assertIn("无操作系统级隔离", r.sandbox_note)

    def test_status_never_claims_isolation_for_non_isolated_modes(self):
        """点 1：无隔离绝不能报"隔离就绪"——能执行 ≠ 有隔离，两个事实分开报。"""
        for mode in ("restricted", "none"):
            with self.subTest(mode=mode):
                with _env(CODE_EXECUTION_SANDBOX=mode), self._docker_unavailable():
                    st = code_sandbox.sandbox_status()
                self.assertEqual(st["mode"], mode)
                self.assertFalse(st["isolation_ready"], "无隔离模式不得报隔离就绪")
                self.assertTrue(st["isolation_required"] is False)
                self.assertIn("无操作系统级隔离", st["isolation_reason"])
                self.assertTrue(st["execution_available"], "宿主执行能力仍在")
                self.assertEqual(st["execution_reason"], "")

    def test_status_distinguishes_ready_and_not_ready(self):
        p1, p2 = self._docker_ok_image_ok()
        with _env(), p1, p2:
            st = code_sandbox.sandbox_status()
        self.assertEqual(st["mode"], "docker")
        self.assertEqual(st["mode_source"], "default")
        self.assertTrue(st["isolation_required"])
        self.assertTrue(st["isolation_ready"])
        self.assertTrue(st["execution_available"])

        with _env(), self._docker_unavailable():
            st2 = code_sandbox.sandbox_status()
        self.assertTrue(st2["isolation_required"])
        self.assertFalse(st2["isolation_ready"])
        self.assertFalse(st2["execution_available"], "要求隔离却不可用时执行能力也不可用")
        self.assertIn("拒绝", st2["isolation_note"])
        self.assertIn("docker 不可用", st2["isolation_reason"])

    def test_health_entry_judges_isolation_not_execution(self):
        """健康项以隔离为判据：restricted 能跑但没有隔离 → 不算健康。"""
        import health_registry
        with _env(CODE_EXECUTION_SANDBOX="restricted"), self._docker_unavailable():
            entry = health_registry.probe_sandbox()
        blob = json.dumps(entry, ensure_ascii=False)
        self.assertIn("isolation_ready=no", blob)
        self.assertIn("execution_available=yes", blob)
        with _env(CODE_EXECUTION_SANDBOX="restricted"):
            st = code_sandbox.sandbox_status()
        self.assertFalse(st["isolation_ready"])


class TestWorkerFailureChain(_SandboxTest):
    """拒绝必须经 worker 上报为步骤失败：失败可见、不假报成功。"""

    def test_refusal_becomes_failed_step_with_reason(self):
        import async_worker_base

        class _FakeRegistry:
            async def register(self, *a, **kw):
                return None

        class _FakeRedis:
            def __init__(self):
                self.pushed = []

            async def rpush(self, key, value):
                self.pushed.append((key, value))

        class _FakeMessaging:
            def __init__(self):
                self._redis = _FakeRedis()

        class _RefusingWorker(async_worker_base.AsyncWorkerBase):
            async def execute(self, instruction, task=None):
                raise code_sandbox.SandboxUnavailable(
                    code_sandbox.refusal_message("docker 不可用（CLI 缺失或守护进程未响应）"))

        msg = _FakeMessaging()
        worker = _RefusingWorker("code_execution", ["code_execution"],
                                 _FakeRegistry(), msg)
        task = {"task_id": "ui-sbx-1", "instruction": "跑个脚本"}

        asyncio.run(worker._handle(task))

        self.assertEqual(len(msg._redis.pushed), 1, "必须回报一次结果（不挂起）")
        key, payload = msg._redis.pushed[0]
        self.assertEqual(key, "task_result:ui-sbx-1")
        data = json.loads(payload)
        self.assertEqual(data["status"], "FAILED", "拒绝执行不得报成功")
        self.assertIn("代码执行被拒绝", data["result"])
        self.assertIn("恢复隔离", data["result"])


class TestWorkerDoesNotSwallowFacilityRefusal(_SandboxTest):
    """点 3：设施/配置拒绝必须**直接上报失败并保留原因**，不触发模型修复循环。

    用真实 `CodeExecutionWorker`（只 mock 模型与进程），避免"只测基类"的假覆盖。
    """

    def _worker(self):
        from workers.code_execution_worker import CodeExecutionWorker

        worker = CodeExecutionWorker.__new__(CodeExecutionWorker)
        worker.workspace = self.tmp / "ws"
        worker.workspace.mkdir(parents=True, exist_ok=True)
        calls = {"gen": 0}

        async def fake_call_llm(system="", prompt="", instruction="",
                                max_attempts=3, max_tokens=2000, **kw):
            calls["gen"] += 1
            return "print('hello from worker')\n"

        async def fake_tdd(*a, **k):
            return False, ""

        worker._call_llm = fake_call_llm
        worker._tdd_pilot = fake_tdd
        return worker, calls

    def test_run_smoke_propagates_facility_refusal(self):
        import asyncio

        worker, _ = self._worker()
        with mock.patch("code_sandbox.run_script_async",
                        side_effect=code_sandbox.SandboxUnavailable(
                            code_sandbox.refusal_message("docker 不可用"))):
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                asyncio.run(worker._run_smoke("print('hi')\n"))
        self.assertIn("代码执行被拒绝", str(ctx.exception))

    def test_execute_reports_refusal_without_regeneration(self):
        """默认模式下隔离不可用：一次生成后即失败，不进入"让模型改代码"的循环。"""
        import asyncio

        worker, calls = self._worker()
        with _env(), self._docker_unavailable():
            with self.assertRaises(code_sandbox.SandboxUnavailable) as ctx:
                asyncio.run(worker.execute("写一个脚本打印 hello"))
        msg = str(ctx.exception)
        self.assertIn("代码执行被拒绝", msg)
        self.assertIn("恢复隔离", msg)
        self.assertEqual(calls["gen"], 1, "设施拒绝不得触发模型重生成")
        self.assertNotIn("No valid code", msg, "不得被包装成通用失败而丢掉隔离根因")


class TestNoDockerDoesNotBlockStartup(_SandboxTest):
    """边界：缺 Docker 只停用代码执行，不阻断工作台启动（历史要求：减少 Docker 依赖）。"""

    def test_launcher_still_starts_services_without_docker(self):
        import launcher

        started = []

        def fake_spawn(name, argv, cwd, out_path):
            started.append(name)
            return 4321

        with mock.patch.object(code_sandbox, "docker_available", return_value=False), \
                mock.patch.object(launcher, "instance_state",
                                  return_value={"running": False, "services": {},
                                                "stale": {}, "port": 8080,
                                                "url": "http://localhost:8080"}), \
                mock.patch.object(launcher, "stop_services", return_value=[]), \
                mock.patch.object(launcher, "_ensure_redis_available"), \
                mock.patch.object(launcher, "_write_pids"), \
                mock.patch.object(launcher, "build_services",
                                  return_value=[("web_ui", ["python", "web_ui.py"],
                                                 str(self.tmp), str(self.tmp / "o.log"))]), \
                mock.patch.object(launcher, "_spawn_service", side_effect=fake_spawn), \
                mock.patch.object(launcher, "verify_services",
                                  return_value={"total": 1, "alive": 1, "down": []}), \
                mock.patch.object(launcher, "readiness_report",
                                  return_value={"workbench": {"ok": True, "detail": "x",
                                                              "port": 8080,
                                                              "url": "http://localhost:8080"},
                                                "research": {"ok": True, "redis": True,
                                                             "redis_major": 8, "missing": [],
                                                             "stale": [], "orchestrator": True,
                                                             "required": []},
                                                "code_sandbox": {"ok": False, "note": "",
                                                                 "execution_available": False,
                                                                 "isolation_required": True,
                                                                 "reason": "docker 不可用"},
                                                "port": 8080, "url": "http://localhost:8080",
                                                "ready": True}), \
                mock.patch.object(launcher, "print_readiness"), \
                mock.patch.object(launcher.time, "sleep"), \
                mock.patch.dict(os.environ):
            pids = launcher.start_services()          # 不得抛 SystemExit
        self.assertEqual(started, ["web_ui"], "缺 Docker 时仍应正常拉起服务")
        self.assertIn("web_ui", pids["services"])

    def test_gap_is_reported_without_claiming_total_failure(self):
        with _env(), self._docker_unavailable():
            st = code_sandbox.sandbox_status()
            msg = code_sandbox.refusal_message(st["isolation_reason"])
        self.assertFalse(st["isolation_ready"])
        self.assertIn("其余功能", st["isolation_note"])
        self.assertIn("其余功能", msg)
        self.assertIn("工作台可正常启动", msg)


if __name__ == "__main__":
    unittest.main()
