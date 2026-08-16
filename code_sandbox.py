# -*- coding: utf-8 -*-
"""code_execution 沙箱（对标标准 C4-4.4 指令注入防护）。

三种模式（环境变量 CODE_EXECUTION_SANDBOX）：
- docker    ：容器隔离（--network none、只读系统盘、仅挂载任务工作区）
- restricted：受限模式（剥离密钥环境变量 + 工作区限定 + 超时）——默认
- none      ：不隔离（仅用于明确关闭）

默认 restricted 即比裸跑更安全（密钥不可见）；部署环境建议构建沙箱镜像后切 docker。
"""

import os
import shutil
import subprocess
import sys

SECRET_PREFIXES = ("LLM_", "OPENAI_", "EMBEDDING_", "API_KEY", "SERPAPI", "TOKEN", "SECRET")

DEFAULT_IMAGE = "weavemind-code-sandbox:latest"


def sandbox_mode() -> str:
    mode = os.environ.get("CODE_EXECUTION_SANDBOX", "restricted").strip().lower()
    if mode not in ("docker", "restricted", "none"):
        mode = "restricted"
    return mode


def docker_available() -> bool:
    return bool(shutil.which("docker"))


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
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        img, "python", f"{work}/{rel}",
    ]


def run_script(
    script_path: str, cwd: str, timeout: int = 60,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """运行 Python 脚本（沙箱感知）。返回 CompletedProcess。"""
    mode = sandbox_mode()
    clean_env = sanitize_env(env)
    if mode == "docker" and docker_available():
        cmd = docker_run_command(script_path, cwd)
        return subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            cwd=cwd, env=clean_env,
        )
    # restricted / none：用剥离密钥后的环境运行
    return subprocess.run(
        [sys.executable, os.path.abspath(script_path)],
        capture_output=True, timeout=timeout, cwd=cwd, env=clean_env,
    )


async def run_script_async(script_path: str, cwd: str, env: dict | None = None):
    """异步运行 Python 脚本（沙箱感知）。返回 (proc, {}) 供 wait_for 使用。"""
    import asyncio

    mode = sandbox_mode()
    clean_env = sanitize_env(env)
    if mode == "docker" and docker_available():
        cmd = docker_run_command(script_path, cwd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=clean_env,
        )
        return proc, {}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, os.path.abspath(script_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=clean_env,
    )
    return proc, {}
