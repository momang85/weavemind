# -*- coding: utf-8 -*-
"""生成依赖锁文件（对标标准 C1 环境标准化）。

两种口径，勿混用：

- 默认（`python scripts/make_lock.py`）→ `requirements.lock`
  当前机器的整机 `pip freeze` 快照。**包含训练与平台专属包**（torch/CUDA/pywin32 等），
  只用于复现本机训练环境，不能直接拿去装 Linux 容器或 CI。

- `--runtime`（`python scripts/make_lock.py --runtime`）→ `requirements-runtime.lock`
  `requirements.txt` 的运行依赖闭包，按目标平台与 Python 版本解析（默认 linux/cp311，
  与 CI 和镜像一致）。解析时要求目标平台存在 wheel，因此同时验证了"这份锁在目标平台装得上"。
  无 wheel、只能走 sdist 的纯 Python 包（jieba）单列并注明，安装时会现场构建。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# 无目标平台 wheel、只能由 sdist 构建的纯 Python 包；版本单独解析后并入。
SDIST_ONLY = ("jieba",)

DEFAULT_PLATFORM = "manylinux2014_x86_64"
DEFAULT_PYTHON_VERSION = "3.11"


def _pip(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True, text=True, timeout=900,
    )


def _report_pins(report_path: Path) -> list[tuple[str, str]]:
    """从 pip --report 的 JSON 里取出 (名称, 版本)。"""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    pins = []
    for item in data.get("install", []):
        meta = item.get("metadata", {})
        name, version = meta.get("name"), meta.get("version")
        if name and version:
            pins.append((name, version))
    return pins


def _resolve_runtime(root: Path, platform: str, py_version: str,
                     index_url: str | None) -> tuple[list[tuple[str, str]], list[str]]:
    """解析运行依赖闭包，返回 (固定版本列表, sdist-only 包列表)。"""
    req_lines = [
        l for l in (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    wheel_reqs = [
        l for l in req_lines
        if l.split("=")[0].split(">")[0].split("<")[0].strip() not in SDIST_ONLY
    ]
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        req_file = tmp / "requirements.wheels.txt"
        req_file.write_text("\n".join(wheel_reqs) + "\n", encoding="utf-8")
        report = tmp / "report.json"
        args = [
            "install", "--dry-run", "--ignore-installed",
            "--only-binary=:all:",
            "--platform", platform,
            "--python-version", py_version,
            "--implementation", "cp",
            "--report", str(report),
            "-r", str(req_file),
        ]
        if index_url:
            args += ["--index-url", index_url]
        proc = _pip(args)
        if proc.returncode != 0 or not report.exists():
            raise RuntimeError(
                f"解析失败（{platform}/cp{py_version.replace('.', '')}）："
                f"{(proc.stderr or proc.stdout).strip()[-500:]}"
            )
        pins = _report_pins(report)

        # sdist-only 包：不带 --platform 解析版本（纯 Python，任何平台同版本）
        for pkg in SDIST_ONLY:
            single = tmp / f"report-{pkg}.json"
            args2 = [
                "install", "--dry-run", "--ignore-installed", "--no-deps",
                "--report", str(single), pkg,
            ]
            if index_url:
                args2 += ["--index-url", index_url]
            proc2 = _pip(args2)
            if proc2.returncode != 0 or not single.exists():
                raise RuntimeError(
                    f"解析 {pkg} 版本失败：{(proc2.stderr or proc2.stdout).strip()[-300:]}"
                )
            pins.extend(_report_pins(single))
    return pins, list(SDIST_ONLY)


def write_runtime_lock(root: Path, platform: str, py_version: str,
                       index_url: str | None, out_name: str = "requirements-runtime.lock") -> int:
    """生成运行闭包锁。

    `out_name` 可改：**平台不同必须写不同文件**——此前固定写
    `requirements-runtime.lock`，用 `--platform win_amd64` 生成 Windows 锁时会把
    Linux（manylinux）锁覆盖掉，CI/镜像随后按 Windows 锁安装。Windows 新人运行包
    用 `--out requirements-runtime-win.lock`。
    """
    try:
        pins, sdist_only = _resolve_runtime(root, platform, py_version, index_url)
    except Exception as exc:
        print(f"生成运行依赖锁失败: {exc}")
        return 1
    if not out_name or "/" in out_name or "\\" in out_name or ".." in out_name:
        print(f"输出文件名非法：{out_name!r}")
        return 1
    out = root / out_name
    header = (
        "# 运行依赖锁（由 scripts/make_lock.py --runtime 生成，勿手改）\n"
        f"# 生成时间：{datetime.now(timezone.utc).isoformat()}\n"
        f"# 目标平台：{platform} / cp{py_version.replace('.', '')}\n"
        f"# 用法：pip install -r {out_name}\n"
        f"# 重新生成：python scripts/make_lock.py --runtime --platform {platform}\n"
        "# 说明：这是 requirements.txt 的运行闭包，解析时要求目标平台存在 wheel；\n"
        "#       整机训练快照在 requirements.lock，两者不可互换；\n"
        "#       平台不同的锁写在不同文件里（Linux → requirements-runtime.lock，\n"
        "#       Windows → requirements-runtime-win.lock），不要互相覆盖。\n"
    )
    body = "\n".join(f"{n}=={v}" for n, v in sorted(set(pins), key=lambda x: x[0].lower()))
    out.write_text(header + body + "\n", encoding="utf-8")
    print(f"已生成 {out}（{len(set(pins))} 个包，其中 sdist-only: {', '.join(sdist_only) or '无'}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="生成依赖锁文件")
    ap.add_argument("--runtime", action="store_true",
                    help="生成 requirements-runtime.lock（运行闭包，目标平台可安装）")
    ap.add_argument("--platform", default=DEFAULT_PLATFORM,
                    help=f"目标平台（默认 {DEFAULT_PLATFORM}）")
    ap.add_argument("--python-version", default=DEFAULT_PYTHON_VERSION,
                    help=f"目标 Python 版本（默认 {DEFAULT_PYTHON_VERSION}）")
    ap.add_argument("--index-url", default=None,
                    help="可选：解析使用的包索引（国内网络可传镜像地址）")
    ap.add_argument("--out", default="requirements-runtime.lock",
                    help="输出文件名（平台不同请写不同文件，勿覆盖 Linux 锁）")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    if args.runtime:
        return write_runtime_lock(root, args.platform, args.python_version,
                                  args.index_url, out_name=args.out)

    out = root / "requirements.lock"
    try:
        freeze = _pip(["freeze"])
        if freeze.returncode != 0:
            print(f"pip freeze 失败: {freeze.stderr[:300]}")
            return 1
    except Exception as exc:
        print(f"pip freeze 异常: {exc}")
        return 1
    header = (
        "# 已知可用的依赖快照（由 scripts/make_lock.py 生成，勿手改）\n"
        f"# 生成时间：{datetime.now(timezone.utc).isoformat()}\n"
        "# 用法：pip install -r requirements.lock\n"
        "# 重新生成：python scripts/make_lock.py\n"
        "# 注意：整机快照，含训练与平台专属包（torch/CUDA/pywin32 等）；\n"
        "#       容器与 CI 请用 requirements-runtime.lock（python scripts/make_lock.py --runtime）。\n"
    )
    body = [l for l in freeze.stdout.splitlines() if l.strip() and not l.startswith("#")]
    out.write_text(header + "\n".join(sorted(set(body))) + "\n", encoding="utf-8")
    print(f"已生成 {out}（{len(set(body))} 个包）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
