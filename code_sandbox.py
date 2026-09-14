# -*- coding: utf-8 -*-
"""code_execution 沙箱（对标标准 C4-4.4 指令注入防护）。

**默认要求隔离**：代码由模型生成，属不可信代码，隔离不可用时宁可拒绝执行。

模式（环境变量 CODE_EXECUTION_SANDBOX）：
- 未设置 / `docker`：容器隔离（--network none、只读系统盘、仅挂载任务工作区）。
  隔离不可用（CLI 缺失、守护进程不可达、镜像未构建、容器层失败）时**拒绝执行并报错**，
  任何情况下都不会改在宿主解释器里跑；
- `restricted`：**无操作系统级隔离**——只剥离密钥类环境变量，生成的代码仍继承服务
  进程的文件访问能力。只能由操作者显式选择，用于本地开发兼容；
- `none`：完全不隔离，同样只能显式选择。

取值非法（拼写错误等）按配置错误处理：**拒绝执行**，不静默放宽为可用模式。

隔离可用性 = docker CLI 存在 + 守护进程可响应 + 沙箱镜像已构建（镜像缺失时打印构建
命令，不自动拉取/构建，避免长时间阻塞任务）。容器层失败与脚本自身报错严格区分：
前者拒绝执行，后者原样返回、不重跑。
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

# 沙箱模式取值；未设置 = ISOLATED_MODE（默认要求隔离）
SANDBOX_MODES = ("docker", "restricted", "none")
ISOLATED_MODE = "docker"


class SandboxUnavailable(RuntimeError):
    """隔离不可用时拒绝执行。

    抛出而不是降级：此时唯一的"替代方案"是在宿主解释器里运行不可信代码。
    """


class SandboxConfigError(RuntimeError):
    """CODE_EXECUTION_SANDBOX 取值非法。按配置错误处理，不得放宽为可执行。"""


def is_facility_error(exc: BaseException) -> bool:
    """是否设施/配置类拒绝（隔离不可用、配置非法）。

    这类错误必须**直接上报失败并保留原因**，不能被当成"脚本跑错了"再让模型改代码：
    模型的修复循环既修不好基础设施，还会把隔离根因冲掉。
    """
    return isinstance(exc, (SandboxUnavailable, SandboxConfigError))

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
    return mode if mode in SANDBOX_MODES else None


def config_error() -> str | None:
    """配置错误描述（取值非法）；未设置或取值合法时返回 None。"""
    raw = os.environ.get("CODE_EXECUTION_SANDBOX")
    if raw is None or not str(raw).strip():
        return None
    val = str(raw).strip().lower()
    if val in SANDBOX_MODES:
        return None
    return (f"CODE_EXECUTION_SANDBOX 取值非法：{str(raw).strip()!r}"
            f"（可选 {' / '.join(SANDBOX_MODES)}）")


def sandbox_mode() -> str:
    """当前**策略**模式：未设置按默认的 docker（要求隔离）；取值非法时抛配置错误。

    这是策略而非"此刻能否隔离"——能否隔离由 `isolation_ready()` 在执行前判定。
    """
    err = config_error()
    if err:
        raise SandboxConfigError(err)
    return sandbox_mode_explicit() or ISOLATED_MODE


def isolation_required() -> bool:
    """是否要求容器隔离（默认与显式 docker 都是；配置非法时按"要求隔离"处理）。"""
    try:
        return sandbox_mode() == ISOLATED_MODE
    except SandboxConfigError:
        return True


def isolation_ready() -> tuple[bool, str]:
    """容器隔离是否可用：(可用?, 不可用原因)。"""
    if not docker_available():
        return False, "docker 不可用（CLI 缺失或守护进程未响应）"
    if not ensure_sandbox_image():
        return False, f"沙箱镜像 {os.environ.get('CODE_SANDBOX_IMAGE') or DEFAULT_IMAGE} 不存在"
    return True, ""


def isolation_note() -> str:
    """人话描述当前隔离强度（状态接口与启动日志共用）。"""
    err = config_error()
    if err:
        return f"配置错误：{err}；代码执行将被拒绝"
    mode = sandbox_mode()
    if mode == ISOLATED_MODE:
        ok, reason = isolation_ready()
        if ok:
            return "容器隔离已就绪（docker：断网 / 只读系统盘 / 仅挂载任务工作区）"
        return (f"要求容器隔离但当前不可用：{reason}；代码执行会被拒绝"
                "（工作台与其余功能不受影响）")
    if mode == "none":
        return "未隔离（操作者显式选择 none）：无操作系统级隔离"
    return "无操作系统级隔离（操作者显式选择 restricted）：仅剥离密钥环境变量"


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
            f"[code_sandbox] 沙箱镜像 {img} 不存在，容器隔离不可用（代码执行将被拒绝）。\n"
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
    """沙箱执行结果：在 CompletedProcess 基础上附加沙箱模式与隔离说明。"""

    def __init__(
        self,
        args,
        returncode,
        stdout=b"",
        stderr=b"",
        *,
        sandbox_mode=None,
        sandbox_note=None,
    ):
        super().__init__(args, returncode, stdout=stdout, stderr=stderr)
        self.sandbox_mode = sandbox_mode
        # 隔离强度的人话说明（restricted / none 会明确写出"无操作系统级隔离"）
        self.sandbox_note = sandbox_note


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


def refusal_message(reason: str) -> str:
    """拒绝执行的说明（用户可见）。

    只给"恢复隔离"的出路：不把关闭隔离当作修复手段（面向机构的安全默认值）。
    """
    img = os.environ.get("CODE_SANDBOX_IMAGE") or DEFAULT_IMAGE
    return (
        f"代码执行被拒绝：{reason}。\n"
        "默认要求容器隔离；隔离不可用时不会改在宿主解释器里运行模型生成的代码。\n"
        "影响范围：仅依赖代码执行的步骤失败，其余功能（检索、结构化数据、报告生成、\n"
        "交付导出等）不受影响，工作台可正常启动与使用。\n"
        "请先恢复隔离后重试：\n"
        "  1) 启动 Docker 守护进程（`docker version` 能返回 Server 版本）\n"
        f"  2) 构建沙箱镜像：docker build -f Dockerfile.sandbox -t {img} .\n"
        "  3) 使用自定义镜像时，用 CODE_SANDBOX_IMAGE 指向已构建的镜像"
    )


def _refuse(reason: str) -> None:
    """拒绝执行：记 ERROR 并抛出（调用方据此把步骤判失败，失败可见）。"""
    msg = refusal_message(reason)
    _logger.error("[code_sandbox] %s", msg)
    raise SandboxUnavailable(msg)


def run_script(
    script_path: str,
    cwd: str,
    timeout: int = 60,
    env: dict | None = None,
) -> SandboxResult:
    """运行 Python 脚本（沙箱感知）。返回 SandboxResult（含 sandbox_mode / sandbox_note）。

    默认与显式 docker 都要求容器隔离：隔离不可用、配置非法或容器层失败时抛
    `SandboxUnavailable`，**不会**改在宿主解释器里执行。宿主只在操作者显式选择
    `restricted` / `none` 时才会被使用（结果里注明"无操作系统级隔离"）。
    """
    clean_env = sanitize_env(env)
    err = config_error()
    if err:
        _refuse(err)
    mode = sandbox_mode()
    if mode == ISOLATED_MODE:
        ready, reason = isolation_ready()
        if not ready:
            _refuse(reason)
        try:
            result = subprocess.run(
                docker_run_command(script_path, cwd),
                capture_output=True, timeout=timeout, cwd=cwd, env=clean_env,
            )
        except (FileNotFoundError, PermissionError) as exc:
            _refuse(f"docker 执行层启动失败: {exc}")
        except subprocess.SubprocessError:
            raise  # 超时等属于脚本执行问题，原样上抛（不重跑、不回退）
        output = (result.stderr or b"") + (result.stdout or b"")
        if _docker_layer_failure(result.returncode, output):
            _refuse(_docker_failure_reason(result.returncode, output))
        return SandboxResult(
            result.args, result.returncode, result.stdout, result.stderr,
            sandbox_mode=ISOLATED_MODE, sandbox_note=isolation_note(),
        )
    # 显式 restricted / none：操作者选择的本地模式（无操作系统级隔离）
    result = subprocess.run(
        [sys.executable, os.path.abspath(script_path)],
        capture_output=True, timeout=timeout, cwd=cwd, env=clean_env,
    )
    return SandboxResult(
        result.args, result.returncode, result.stdout, result.stderr,
        sandbox_mode=mode, sandbox_note=isolation_note(),
    )


class _AsyncSandboxProc:
    """异步 docker 进程包装：容器层失败时拒绝执行（不重跑、不回退宿主）。"""

    def __init__(self, proc):
        self._proc = proc
        self.returncode = None
        self.stdout = b""
        self.stderr = b""
        self.sandbox_mode = ISOLATED_MODE
        self.sandbox_note = isolation_note()

    @property
    def pid(self):
        return getattr(self._proc, "pid", None)

    async def communicate(self, input=None):
        out, err = await self._proc.communicate(input)
        output = (err or b"") + (out or b"")
        # 容器层失败（守护进程中途不可达等）≠ 脚本自身报错：前者拒绝执行，
        # 后者原样返回（returncode/输出交给调用方判断）
        if _docker_layer_failure(self._proc.returncode, output):
            _refuse(_docker_failure_reason(self._proc.returncode, output))
        self.returncode = self._proc.returncode
        self.stdout, self.stderr = out, err
        return out, err

    def kill(self):
        self._proc.kill()


async def run_script_async(script_path: str, cwd: str, env: dict | None = None):
    """异步运行 Python 脚本（沙箱感知）。返回 (proc, meta)；

    meta 含 sandbox_mode / sandbox_note。默认与显式 docker 下隔离不可用即抛
    `SandboxUnavailable`（与同步入口同一策略），不会回退宿主执行。
    """
    import asyncio

    clean_env = sanitize_env(env)
    err = config_error()
    if err:
        _refuse(err)
    mode = sandbox_mode()
    if mode == ISOLATED_MODE:
        ready, reason = isolation_ready()
        if not ready:
            _refuse(reason)
        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_run_command(script_path, cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=clean_env,
            )
        except (FileNotFoundError, PermissionError) as exc:
            _refuse(f"docker 执行层启动失败: {exc}")
        return _AsyncSandboxProc(proc), {
            "sandbox_mode": ISOLATED_MODE,
            "sandbox_note": isolation_note(),
        }
    meta = {"sandbox_mode": mode, "sandbox_note": isolation_note()}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, os.path.abspath(script_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=clean_env,
    )
    return proc, meta


def sandbox_status() -> dict:
    """沙箱状态快照（供 /api/status、/api/health、启动日志展示）。

    要能回答两个问题：当前策略模式是什么、隔离此刻是否真的可用
    （模式正确但 docker 不可用 → 代码执行会被拒绝，必须看得出来）。
    """
    err = config_error()
    mode = "invalid" if err else sandbox_mode()
    docker_ok = docker_available()
    image = os.environ.get("CODE_SANDBOX_IMAGE") or DEFAULT_IMAGE
    explicit = sandbox_mode_explicit()
    if err:
        ready, exec_ok, reason = False, False, err
    elif mode == ISOLATED_MODE:
        ready, reason = isolation_ready()
        exec_ok = ready
    else:
        # 显式非隔离模式：代码能执行，但**没有隔离**——绝不报"隔离就绪"
        ready = False
        exec_ok = True
        reason = f"操作者显式选择 {mode}：无操作系统级隔离"
    return {
        "mode": mode,
        "mode_explicit": explicit,
        "mode_env_raw": os.environ.get("CODE_EXECUTION_SANDBOX"),
        "mode_source": "explicit" if explicit else "default",
        "config_error": err,
        # 默认与显式 docker 都要求隔离；显式 restricted/none 是操作者的选择
        "isolation_required": mode in (ISOLATED_MODE, "invalid"),
        # 隔离是否真的可用（restricted/none 恒为 False：它们本就不提供隔离）
        "isolation_ready": bool(ready),
        "isolation_reason": "" if ready else reason,
        # 代码执行能力是否可用（与隔离分开：能跑 ≠ 有隔离）
        "execution_available": bool(exec_ok),
        "execution_reason": "" if exec_ok else reason,
        "isolation_note": isolation_note(),
        "docker_available": docker_ok,
        "sandbox_image": image,
        "sandbox_image_exists": image_exists(image) if docker_ok else False,
    }
