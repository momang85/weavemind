# -*- coding: utf-8 -*-
"""当前加载的代码版本：让"哪份代码处理了这条请求"可回答。

为什么需要（C4 的"实际运行实例及加载版本"）：服务是**长驻进程**，代码改了但没重启，
运行的仍是旧版本。此前时间线只记实例标识，无法回答"这条请求是被哪份代码处理的"，
评审/排障只能靠猜（"我记得重启过"）。

约定：
- 返回**短 HEAD**（如 `761291b`）；工作区有未提交改动时加 `+dirty`；
- 取不到（包内没有 `.git`、没装 git、读失败）→ 返回**空串**：未知就说未知，不编；
- 不依赖 git 命令（直接读 `.git`，无子进程、无管道），必要时才用 git 判脏且失败即忽略。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def _git_dir() -> Path | None:
    """定位 `.git`（支持 worktree：`.git` 是文件、里面写 `gitdir: …`）。"""
    p = BASE_DIR / ".git"
    if p.is_dir():
        return p
    if p.is_file():
        try:
            line = p.read_text(encoding="utf-8", errors="replace").strip()
            if line.lower().startswith("gitdir:"):
                target = Path(line.split(":", 1)[1].strip())
                return target if target.is_absolute() else (BASE_DIR / target)
        except Exception:                             # noqa: BLE001
            return None
    return None


def _head_sha(git_dir: Path) -> str:
    """读 HEAD 指向的提交（不跑 git）。"""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except Exception:                                 # noqa: BLE001
        return ""
    if _SHA_RE.match(head):
        return head                               # detached HEAD
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        for cand in (git_dir / ref, git_dir / "packed-refs"):
            try:
                if cand.name == "packed-refs":
                    for line in cand.read_text(encoding="utf-8",
                                               errors="replace").splitlines():
                        if line.endswith(" " + ref) and _SHA_RE.match(line.split()[0]):
                            return line.split()[0]
                    continue
                sha = cand.read_text(encoding="utf-8", errors="replace").strip()
                if _SHA_RE.match(sha):
                    return sha
            except Exception:                         # noqa: BLE001
                continue
    return ""


def _dirty() -> bool:
    """工作区是否有未提交改动（best-effort：没 git 或失败一律当"未知/不脏"）。

    `encoding="utf-8", errors="replace"`：git 在中文 Windows 上会按本地代码页输出
    未跟踪文件名，用默认编码读取会在读取线程里抛 UnicodeDecodeError（实测噪音一堆）。
    """
    try:
        import subprocess
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(BASE_DIR),
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=5)
        return bool((out.stdout or "").strip())
    except Exception:                                 # noqa: BLE001
        return False


def current(*, with_dirty: bool = True) -> str:
    """短 HEAD（+dirty）；取不到返回空串。"""
    git_dir = _git_dir()
    if git_dir is None:
        return ""
    sha = _head_sha(git_dir)
    if not sha:
        return ""
    short = sha[:7]
    if with_dirty:
        try:
            if _dirty():
                short += "+dirty"
        except Exception:                             # noqa: BLE001
            pass
    return short


def describe() -> str:
    """给日志/时间线用的一行说明（未知时如实说未知）。"""
    ver = current()
    return ver if ver else "版本未知（无 .git 或读取失败）"


if __name__ == "__main__":                            # 便于人工核对
    print(describe())
    _ = os.environ
