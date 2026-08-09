"""织光 (ZhiGuang) — 环境自举：按需自动安装 Python 包 / 浏览器依赖。

目标：缺依赖不能成为任务失败的墙——worker 生成的脚本缺模块时自动 pip
安装后重跑；浏览器级"可玩性验证"缺 Playwright 时自动安装（含 Chromium）。
"""

import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

_MODULE_TO_PACKAGE = {
    "playwright": "playwright",
    "PIL": "pillow",
    "requests": "requests",
    "bs4": "beautifulsoup4",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
}


def pip_install(packages: list[str], timeout: int = 300) -> tuple[bool, str]:
    """安装 Python 包；返回 (是否成功, 输出摘要)。"""
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--timeout", "60", "--retries", "2", *packages],
            capture_output=True, timeout=timeout,
        )
        out = (p.stderr or p.stdout or b"").decode("utf-8", errors="replace")
        return p.returncode == 0, out[-600:]
    except subprocess.TimeoutExpired:
        return False, f"pip install 超时（>{timeout}s）"
    except Exception as exc:
        return False, f"pip install 异常: {exc}"


def module_package_name(module: str) -> str:
    return _MODULE_TO_PACKAGE.get(module, module)


def ensure_module(module: str, timeout: int = 300) -> tuple[bool, str]:
    """确保模块可导入；不可则自动 pip 安装（返回 是否就绪, 说明）。"""
    try:
        __import__(module)
        return True, f"{module} 已可用"
    except ImportError:
        pass
    pkg = module_package_name(module)
    ok, msg = pip_install([pkg], timeout=timeout)
    if not ok:
        return False, f"自动安装 {pkg} 失败: {msg}"
    try:
        __import__(module)
        return True, f"已自动安装 {pkg}"
    except ImportError as exc:
        return False, f"安装后仍无法导入 {module}: {exc}"


def ensure_playwright(install_browser: bool = True, timeout: int = 900) -> tuple[bool, str]:
    """确保 Playwright 可用（含 Chromium 浏览器）；自动安装。"""
    ok, msg = ensure_module("playwright", timeout=300)
    if not ok:
        return False, msg
    if not install_browser:
        return True, "playwright 已可用（浏览器未安装）"
    try:
        p = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, timeout=timeout,
        )
        if p.returncode == 0:
            return True, "Playwright + Chromium 就绪"
        out = (p.stderr or p.stdout or b"").decode("utf-8", errors="replace")
        return False, f"Chromium 安装失败: {out[-300:]}"
    except subprocess.TimeoutExpired:
        return False, f"Chromium 下载超时（>{timeout}s）"
    except Exception as exc:
        return False, f"Chromium 安装异常: {exc}"


def auto_install_and_retry_import(module: str) -> tuple[bool, str]:
    """供 worker 使用：脚本缺模块时自动安装并返回是否就绪。"""
    return ensure_module(module)
