# -*- coding: utf-8 -*-
"""code_execution 沙箱（对标标准 C4-4.4 指令注入防护）。

三种模式（环境变量 CODE_EXECUTION_SANDBOX）：
- docker    ：容器隔离（--network none、只读系统盘、仅挂载任务工作区）
- restricted：受限模式（剥离密钥环境变量 + 工作区限定 + 超时）
- none      ：不隔离（仅用于明确关闭）

默认策略为「docker-first 自动降级」：
- 未显式设置 CODE_EXECUTION_SANDBOX 时自动探测：docker CLI/守护进程可用则 docker，否则 restricted；
- 显式设置时按显式值执行；docker 模式若执行层失败（守护进程未启动、镜像缺失、权限不足等），
  自动降级 restricted 重跑本次脚本，并通过结果字段/日志标记降级原因；
- 镜像缺失时打印构建提示（docker build -f Dockerfile.sandbox -t ... .），
  但不自动拉取/构建，避免长时间阻塞任务。
"""

import logging
import os
import shutil
import subprocess
import sys
import threading
import time

_logger = logging.getLogger(__name__)

SECRET_PREFIXES = ("LLM_", "OPENAI_", "EMBEDDING_", "API_KEY", "SERPAPI", "TOKEN", "SECRET")

DEFAULT_IMAGE = "weavemind-code-sandbox:latest"

# docker 执行层失败特征：仅当返回码为 125/126/127 且输出命中这些标记时才判定为
# docker 层失败（守护进程未启动/权限不足/镜像缺失等）；脚本自身报错（通常
# returncode=1 + Traceback）不会被误判，从而不吞掉真正的脚本执行失败。
_DOCKER_LAYER_ERROR_MARKERS = (
    b"cannot connect to the docker daemon",
    b"is the docker daemon running",
    b"error during connect",
    b"permission denied",
    b"unable to find image",
    b"no such image",
    b"docker: command not found",
)

# 探测缓存：避免每次脚本执行都反复调用 docker version / image inspect
_CACHE_TTL = float(os.environ.get("CODE_SANDBOX_CACHE_TTL", "10"))
_PROBE_TIMEOUT = float(os.environ.get("CODE_SANDBOX_PROBE_TIMEOUT", "3"))
_docker_cache = {"ts": 0.0, "ok": False}
_image_cache = {"ts": 0.0, "present": False}
_cache_lock = threading.Lock()
_image_hint_printed = set()


def sandbox_mode_explicit() -> str | None:
    """返回用户显式设置的有效模式；未设置或值非法时返回 None。"""
    raw = os.environ.get("CODE_EXECUTION_SANDBOX")
    if raw is None:
        return None
    mode = raw.strip().lower()
    return mode if mode in ("docker", "restricted", "none") else None


def sandbox_mode() -> str:
    """有效沙箱模式：显式设置按显式值；未设置时自动探测（docker 可用则 docker，否则 restricted）。"""
    explicit = sandbox_mode_explicit()
    if explicit:
        return explicit
    try:
        return "docker" if docker_available() else "restricted"
    except Exception:
        _logger.exception("sandbox 自动探测异常，降级 restricted")
        return "restricted"


def docker_available() -> bool:
    """docker 是否真正可用：CLI 存在且守护进程可响应（带短缓存）。"""
    if not shutil.which("docker"):
        return False
    with _cache_lock:
        if time.time() - _docker_cache["ts"] < _CACHE_TTL:
            return _docker_cache["ok"]
    ok = _probe_docker_daemon()
    with _cache_lock:
        _docker_cache.update(ts=time.time(), ok=ok)
    return ok


def _probe_docker_daemon() -> bool:
    """探测 docker 守护进程：docker version 能返回 Server 段才算可用。"""
    try:
        p = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
        return p.returncode == 0
    except Exception:
        return False


def clear_sandbox_caches() -> None:
    """清空 docker 探测/镜像检查缓存（测试与手动验证用）。"""
    with _cache_lock:
        _docker_cache["ts"] = 0.0
        _image_cache["ts"] = 0.0


def image_exists(image: str | None = None) -> bool:
    """沙箱镜像是否存在（docker image inspect，带短缓存，不打印提示）。"""
    img = image or os.environ.get("CODE_SANDBOX_IMAGE") or DEFAULT_IMAGE
    with _cache_lock:
        if time.time() - _image_cache["ts"] < _CACHE_TTL:
            return _image_cache["present"]
    try:
        p = subprocess.run(
            ["docker", "image", "inspect", img],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
        )
        present = p.returncode == 0
    except Exception:
        present = False
    with _cache_lock:
        _image_cache.update(ts=time.time(), present=present)
    return present


def ensure_sandbox_image(image: str | None = None) -> bool:
    """检查沙箱镜像；缺失时打印构建提示（不自动拉取/构建，避免阻塞任务）。"""
    img = image or os.environ.get("CODE_SANDBOX_IMAGE") or DEFAULT_IMAGE
    present = image_exists(img)
    if not present and img not in _image_hint_printed:
        _image_hint_printed.add(img)
        print(
            f"[code_sandbox] 沙箱镜像 {img} 不存在，本次执行降级 restricted。\n"
            f"  构建命令：docker build -f Dockerfile.sandbox -t {img} ."
        )
    return present


def sanitize_env(env: dict | None = None) -> dict:
    """剥离密钥类环境变量，防止生成的代码读取。"""
    src = env if env is not None else os.environ
    return {k: v for k, v in src.items() if not any(s in k.upper() for s in SECRET_PREFIXES)}


def docker_run_command(script_path: str, cwd: str, image: str | None = None) -> list[str]:
    """构造 docker 运行命令：只读根文件系统、断网、仅挂载 cwd。"""
    img = image or os.environ.get("CODE_SANDBOX_IMAGE") or DEFAULT_IMAGE
    work = "/work"
    rel = os.path.basename(script_path)
    return [
        "docker", "run", "--rm",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp",
        "--memory", os.environ.get("CODE_SANDBOX_MEM", "512m"),
        "--cpus", os.environ.get("CODE_SANDBOX_CPUS", "1"),
        "-v", f"{os.path.abspath(cwd)}:{work}",
        "-w", work,
        # 只读根文件系统下，Python 字节码缓存、matplotlib 字体缓存等必须落到 tmpfs /tmp
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "HOME=/tmp",
        "--env", "MPLCONFIGDIR=/tmp/.matplotlib",
        "--env", "PYTHONIOENCODING=utf-8",
        img, "python", f"{work}/{rel}",
    ]


class SandboxResult(subprocess.CompletedProcess):
    """沙箱执行结果：在 CompletedProcess 基础上附加沙箱模式与降级标记。"""

    def __init__(
        self,
        args,
        returncode,
        stdout=b"",
        stderr=b"",
        *,
        sandbox_mode=None,
        sandbox_degraded=False,
        sandbox_degrade_reason=None,
    ):
        super().__init__(args, returncode, stdout=stdout, stderr=stderr)
        self.sandbox_mode = sandbox_mode
        self.sandbox_degraded = sandbox_degraded
        self.sandbox_degrade_reason = sandbox_degrade_reason


def _docker_layer_failure(returncode: int, output: bytes) -> bool:
    """仅当返回码为 docker 层错误码且输出含 docker 特征信息时才判定为执行层失败。"""
    if returncode not in (125, 126, 127):
        return False
    low = (output or b"").lower()
    return any(m in low for m in _DOCKER_LAYER_ERROR_MARKERS)


def _docker_failure_reason(returncode: int, output: bytes) -> str:
    text = (output or b"").decode("utf-8", errors="replace").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    snippet = lines[-1] if lines else ""
    return f"docker 执行层失败（exit={returncode}）: {snippet[:200]}"


def _docker_preflight_reason() -> str | None:
    """docker 模式预检：不可用/缺镜像时返回降级原因，否则返回 None。"""
    if not docker_available():
        return "docker 不可用（CLI 缺失或守护进程未响应）"
    if not ensure_sandbox_image():
        return f"沙箱镜像 {os.environ.get('CODE_SANDBOX_IMAGE') or DEFAULT_IMAGE} 不存在"
    return None


def _warn_degrade(reason: str) -> None:
    _logger.warning("[code_sandbox] docker 模式不可用：%s；本次降级 restricted 重跑", reason)


def run_script(
    script_path: str,
    cwd: str,
    timeout: int = 60,
    env: dict | None = None,
) -> SandboxResult:
    """运行 Python 脚本（沙箱感知）。返回 SandboxResult（含 sandbox_mode / sandbox_degraded 标记）。"""
    clean_env = sanitize_env(env)
    mode = sandbox_mode()
    if mode == "docker":
        reason = _docker_preflight_reason()
        if reason is None:
            cmd = docker_run_command(script_path, cwd)
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout,
                    cwd=cwd, env=clean_env,
                )
            except (FileNotFoundError, PermissionError) as exc:
                reason = f"docker 执行层启动失败: {exc}"
            except subprocess.SubprocessError:
                raise  # 超时等属于脚本执行问题，不降级重跑
            else:
                output = (result.stderr or b"") + (result.stdout or b"")
                if not _docker_layer_failure(result.returncode, output):
                    return SandboxResult(
                        result.args, result.returncode, result.stdout, result.stderr,
                        sandbox_mode="docker", sandbox_degraded=False,
                    )
                reason = _docker_failure_reason(result.returncode, output)
        _warn_degrade(reason)
        result = subprocess.run(
            [sys.executable, os.path.abspath(script_path)],
            capture_output=True, timeout=timeout, cwd=cwd, env=clean_env,
        )
        return SandboxResult(
            result.args, result.returncode, result.stdout, result.stderr,
            sandbox_mode="restricted", sandbox_degraded=True,
            sandbox_degrade_reason=reason,
        )
    # restricted / none：用剥离密钥后的环境运行
    result = subprocess.run(
        [sys.executable, os.path.abspath(script_path)],
        capture_output=True, timeout=timeout, cwd=cwd, env=clean_env,
    )
    return SandboxResult(
        result.args, result.returncode, result.stdout, result.stderr,
        sandbox_mode=mode, sandbox_degraded=False,
    )


class _AsyncSandboxProc:
    """异步 docker 进程包装：若 docker 执行层失败（如守护进程中途不可用），
    自动降级 restricted 重跑，并向调用方呈现降级后的结果。"""

    def __init__(self, proc, fallback_argv, cwd, env, meta):
        self._proc = proc
        self._fallback_argv = fallback_argv
        self._cwd = cwd
        self._env = env
        self._meta = meta
        self.returncode = None
        self.stdout = b""
        self.stderr = b""
        self.sandbox_mode = "docker"
        self.sandbox_degraded = False
        self.sandbox_degrade_reason = None

    @property
    def pid(self):
        return getattr(self._proc, "pid", None)

    async def communicate(self, input=None):
        import asyncio

        out, err = await self._proc.communicate(input)
        output = (err or b"") + (out or b"")
        if _docker_layer_failure(self._proc.returncode, output):
            reason = _docker_failure_reason(self._proc.returncode, output)
            _warn_degrade(reason)
            proc2 = await asyncio.create_subprocess_exec(
                *self._fallback_argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env,
            )
            out, err = await proc2.communicate(input)
            self.returncode = proc2.returncode
            self.sandbox_mode = "restricted"
            self.sandbox_degraded = True
            self.sandbox_degrade_reason = reason
            self._meta.update(
                sandbox_mode="restricted",
                sandbox_degraded=True,
                sandbox_degrade_reason=reason,
            )
        else:
            self.returncode = self._proc.returncode
        self.stdout, self.stderr = out, err
        return out, err

    def kill(self):
        self._proc.kill()


async def run_script_async(script_path: str, cwd: str, env: dict | None = None):
    """异步运行 Python 脚本（沙箱感知）。返回 (proc, meta)；
    meta 含 sandbox_mode / sandbox_degraded / sandbox_degrade_reason。"""
    import asyncio

    clean_env = sanitize_env(env)
    mode = sandbox_mode()
    fallback_argv = [sys.executable, os.path.abspath(script_path)]
    if mode == "docker":
        reason = _docker_preflight_reason()
        if reason is None:
            cmd = docker_run_command(script_path, cwd)
            meta = {
                "sandbox_mode": "docker",
                "sandbox_degraded": False,
                "sandbox_degrade_reason": None,
            }
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=clean_env,
                )
            except (FileNotFoundError, PermissionError) as exc:
                reason = f"docker 执行层启动失败: {exc}"
            else:
                return _AsyncSandboxProc(proc, fallback_argv, cwd, clean_env, meta), meta
        _warn_degrade(reason)
        meta = {
            "sandbox_mode": "restricted",
            "sandbox_degraded": True,
            "sandbox_degrade_reason": reason,
        }
        proc = await asyncio.create_subprocess_exec(
            *fallback_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=clean_env,
        )
        return proc, meta
    meta = {
        "sandbox_mode": mode,
        "sandbox_degraded": False,
        "sandbox_degrade_reason": None,
    }
    proc = await asyncio.create_subprocess_exec(
        *fallback_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=clean_env,
    )
    return proc, meta


def sandbox_status() -> dict:
    """沙箱状态快照（供 /api/status、/api/health 展示）。"""
    docker_ok = docker_available()
    image = os.environ.get("CODE_SANDBOX_IMAGE") or DEFAULT_IMAGE
    explicit = sandbox_mode_explicit()
    return {
        "mode": sandbox_mode(),
        "mode_explicit": explicit,
        "mode_env_raw": os.environ.get("CODE_EXECUTION_SANDBOX"),
        "mode_source": "explicit" if explicit else "auto",
        "docker_available": docker_ok,
        "sandbox_image": image,
        "sandbox_image_exists": image_exists(image) if docker_ok else False,
    }
