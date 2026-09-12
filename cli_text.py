# -*- coding: utf-8 -*-
"""终端输出适配（stdlib-only，可被 launcher / dep_check 等复用）。

解决两类乱码：
1. Windows 控制台默认代码页 936(GBK) 下，UTF-8 中文输出会乱码
   —— 优先把自身 stdout/stderr 设为 UTF-8，并尝试切到 65001 代码页；
2. 老旧终端 / 重定向 / CI 场景无法正确渲染非 ASCII
   —— 提供 WM_PLAIN_TEXT=1（或 --plain）纯英文回退。

约定：面向用户的文案用 msg(zh, en) 取，plain 模式返回 en。
"""

from __future__ import annotations

import os
import sys

_setup_done = False


def is_plain() -> bool:
    """是否使用纯 ASCII 输出（老旧终端/CI 兜底）。"""
    val = str(os.environ.get("WM_PLAIN_TEXT", "")).strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    return "--plain" in sys.argv[1:] if sys.argv else False


def msg(zh: str, en: str) -> str:
    """按输出模式返回中文或英文文案。"""
    return en if is_plain() else zh


def _try_switch_codepage() -> None:
    """Windows 控制台下尝试切到 UTF-8 代码页（失败静默，不影响功能）。"""
    if os.name != "nt":
        return
    try:
        import subprocess
        subprocess.run(["chcp", "65001"], shell=False, capture_output=True,
                       timeout=5)
    except Exception:
        pass


def setup_console_encoding(force_utf8: bool | None = None) -> None:
    """把 stdout/stderr 调整为 UTF-8，必要时切代码页（幂等、异常静默）。

    策略（避免"修了控制台、坏了管道"）：
    - 真控制台（isatty）→ 输出改 UTF-8 并尝试 chcp 65001：
      Windows 控制台按 UTF-8 渲染中文，不再乱码；
    - 管道/重定向/CI → **保持平台默认编码**（消费方通常按本机代码页解码，
      强行 UTF-8 反而乱码）；除非环境已显式要求（PYTHONIOENCODING/
      PYTHONUTF8，start.sh / start.bat 会设置）；
    - force_utf8 显式传入时按传入值执行。
    """
    global _setup_done
    if _setup_done:
        return
    _setup_done = True
    if force_utf8 is None:
        env_utf8 = (
            str(os.environ.get("PYTHONIOENCODING", "")).lower().startswith("utf")
            or str(os.environ.get("PYTHONUTF8", "")).strip() == "1"
        )
        try:
            is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        except Exception:
            is_tty = False
        force_utf8 = bool(env_utf8 or is_tty)
    if force_utf8:
        for stream in (sys.stdout, sys.stderr):
            try:
                if hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    try:
        if os.name == "nt" and getattr(sys.stdout, "isatty", lambda: False)():
            _try_switch_codepage()
    except Exception:
        pass


def make_stream_writer_utf8(stream):
    """给 logging 等场景用的兜底：尝试把流切到 UTF-8，失败原样返回。"""
    try:
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return stream


def ok_bad(flag: bool) -> str:
    """统一的状态前缀（避免各文件各写一套，便于 ASCII 模式下也整齐）。"""
    return "[OK]" if flag else "[!!]"
