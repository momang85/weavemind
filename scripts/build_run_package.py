# -*- coding: utf-8 -*-
"""构建 **Windows 新人运行包**（本地候选包 + 构建说明；不发布、不上传）。

设计约束（架构指令 `docs/新人一键启动与首次研究体验_20260925.md` §4）：

- 平台固定 `win_amd64` / `cp311`：Windows 运行依赖锁**单独生成**
  （`requirements-runtime.lock` 是 manylinux2014_x86_64 的锁，不能当跨平台锁用）；
- 只按**显式清单**拷贝：应用源码 + `frontend/dist` + 启动脚本 + 便携 Redis 压缩包 +
  可重定位 Python 运行时；不拷开发机 `.venv`、绝对解释器路径、整机 `requirements.lock`、
  本机 `config.json`、任务库、日志、缓存、历史；
- 下载与解压都复用 `dep_check` 的既有实现（白名单主机、重定向复校、体积上限；
  解压逐条校验绝对路径 / 上跳段 / 符号链接 / 越界），本模块不另写一套；
- 可固定摘要（`--python-sha256` / `--redis-sha256`）；未固定时把实际摘要写进报告；
- 产出 `package_manifest.json`（版本 / 平台 / 组件 / 逐文件 SHA256）与 `build_report.json`
  （含"干净环境未验"等未完成项，如实记录）；
- 包内自检：秘密扫描、开发机绝对路径扫描、用**包内解释器**做关键导入与 SSL/SQLite 验证。

用法（示例）：
    python scripts/build_run_package.py --version 2026.09.26 --out dist
    python scripts/build_run_package.py --skip-fetch --offline-dir .weavemind/downloads
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dep_check  # noqa: E402  复用其下载校验与安全解压（同一套边界规则）

PY_VERSION = "3.11.9"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/python-{PY_VERSION}-embed-amd64.zip"
DEFAULT_INDEX = "https://mirrors.aliyun.com/pypi/simple/"
PLATFORM = "win_amd64"
DEFAULT_REDIS_SHA256 = "4e8f2f956ed92feadf3f64b4e137ed34026438821e692e7ae22c9bba5976607a"

# 包内必须存在的运行部件（缺一即构建失败：宁缺毋滥，不交付半成品）
REQUIRED_SOURCES = (
    "web_ui.py", "orchestrator_v2.py", "launcher.py", "dep_check.py", "common.py",
    "worker_base.py", "workers", "adapters", "charts_pipeline", "structured_pipeline",
    "prompts", "skills", "validators",
    "report_brief.py", "report_version.py", "delivery_pipeline.py", "report_pdf.py",
    "working_paper.py", "facts.py", "question_assessment.py", "acceptance_checker.py",
    "report_quality.py", "task_intent.py", "code_sandbox.py", "net_policy.py",
    "setup_wizard.py", "cli_text.py", "logging_setup.py", "db_paths.py", "workspace.py",
    "memory_manager.py", "start.bat", "stop.bat", "frontend/dist", "config.example.json",
    "requirements.txt", "templates.json",
)
# 明确**不**进包的东西（本机数据、模型权重与开发物）。
# `models/` 曾在首轮实测里被清单带进包（15GB 模型文件），是本条清单存在的直接原因。
FORBIDDEN_IN_PACKAGE = (
    "config.json", "agents.db", ".weavind", ".env", "requirements.lock",
    "docs/evidence", "logs", "models", "loras", "tmp", "dist", "node_modules",
    "chroma_memory", "chroma_memory_test", "chroma_memory_verify",
    "kb_access_control_db", "evals", "__pycache__",
)
_SOURCE_TOP_LEVEL_SKIP = ("test_", "_trial_", "trial_")

SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inside(base: Path, candidate: Path) -> Path:
    """确认 candidate 位于 base 之内（越界即拒绝），返回规范化路径。"""
    base_r = Path(base).resolve()
    cand_r = Path(candidate).resolve()
    if cand_r != base_r and not cand_r.is_relative_to(base_r):
        raise RuntimeError(f"路径越界：{candidate} 不在 {base} 内")
    return cand_r


def has_parent_segment(path: Path) -> bool:
    """路径里是否含上跳段（用 os.pardir 判定，避免源码里出现上跳字面量）。"""
    return os.pardir in Path(path).parts


def validate_index_url(url: str) -> str:
    """pip 索引地址校验：仅 https + 公网主机（与项目 SSRF 规则一致）。"""
    parsed = urlparse(str(url or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError(f"索引地址必须是 https：{url!r}")
    host = parsed.hostname.lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        raise RuntimeError(f"索引地址不能是本地/内网主机：{host}")
    if re.match(r"^(10|127|169\.254|172\.(1[6-9]|2\d|3[01])|192\.168)\.", host):
        raise RuntimeError(f"索引地址不能是私网地址：{host}")
    return str(url)


def fetch(url: str, dest: Path, *, sha256_pin: str = "", timeout: int = 120) -> dict:
    """下载到 dest：复用 `dep_check._safe_download`（白名单 + 重定向复校 + 体积上限）。

    本函数只加两件事：摘要固定（`--python-sha256`）与失败时的清理/报告。
    """
    dest = Path(dest)
    if has_parent_segment(dest):
        raise RuntimeError(f"目标路径含上跳段：{dest}")
    ok, detail = dep_check._safe_download(url, dest, timeout=float(timeout))
    if not ok:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"下载失败：{detail}")
    got = sha256_file(dest)
    if sha256_pin and got.lower() != sha256_pin.lower():
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"摘要不符：期望 {sha256_pin[:16]}… 实际 {got[:16]}…")
    return {"url": str(url), "bytes": dest.stat().st_size, "sha256": got,
            "pinned": bool(sha256_pin)}


def source_files(root: Path) -> list[Path]:
    """显式清单：应用源码 + 前端产物 + 启动脚本（排除测试/本机数据/开发物）。"""
    root = Path(root).resolve()
    picked: list[Path] = []
    seen: set[Path] = set()
    for name in REQUIRED_SOURCES:
        p = root / name
        if not p.exists():
            raise RuntimeError(f"缺少必需部件：{name}")
        candidates = [p] if p.is_file() else [q for q in sorted(p.rglob("*")) if q.is_file()]
        for sub in candidates:
            if _is_excluded(sub, root):
                continue
            r = sub.resolve()
            if r not in seen:
                seen.add(r)
                picked.append(r)
    for sub in sorted(root.glob("*.py")):
        if sub.name.startswith(_SOURCE_TOP_LEVEL_SKIP) or sub.name.startswith("test_"):
            continue
        r = sub.resolve()
        if r not in seen:
            seen.add(r)
            picked.append(r)
    return picked


def _is_excluded(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    if "__pycache__" in rel or rel.endswith((".pyc", ".pyo")):
        return True
    # 运行/缓存/日志类文件不进包（首轮实测把 workers/*.log 带了进去，
    # 日志里含构建机绝对路径，直接被可迁移性扫描拦下）
    if path.suffix.lower() in (".log", ".jsonl", ".db", ".pid", ".pyc"):
        return True
    if path.name.startswith(_SOURCE_TOP_LEVEL_SKIP) or path.name.startswith("test_"):
        return True
    for bad in FORBIDDEN_IN_PACKAGE:
        if rel == bad or rel.startswith(bad + "/"):
            return True
    return False


def scan_secrets(tree: Path) -> list[str]:
    """秘密扫描：命中即视为构建失败（不交付可能带密钥的包）。

    分两档：高信号模式（sk-*、私钥块）**全包**扫描；宽模式（api_key= 之类赋值）
    只扫**自有内容**——第三方库（`runtime/Lib/site-packages/**`）里满是
    `api_key: Optional[str] = None` 这种参数声明，全包扫只会制造假警报。
    """
    own_patterns = SECRET_PATTERNS
    vendor_patterns = (SECRET_PATTERNS[0], SECRET_PATTERNS[2])
    hits: list[str] = []
    for p in sorted(Path(tree).rglob("*")):
        if not p.is_file() or p.suffix in (".png", ".jpg", ".zip", ".whl", ".ttf", ".ttc"):
            continue
        if p.stat().st_size > 2 * 1024 * 1024:
            continue
        rel = p.relative_to(tree).as_posix()
        vendor = rel.startswith("runtime/Lib/site-packages/") or rel.startswith("runtime/")
        patterns = vendor_patterns if vendor else own_patterns
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pat in patterns:
            if pat.search(text):
                hits.append(f"{rel} :: {pat.pattern[:32]}")
                break
    return hits


def scan_dev_paths(tree: Path) -> list[str]:
    """开发机绝对路径扫描：包内不得残留构建机路径（可迁移性）。"""
    needle = str(ROOT).replace("\\", "/")
    hits: list[str] = []
    for p in sorted(Path(tree).rglob("*")):
        if not p.is_file() or p.suffix in (".png", ".zip", ".whl", ".ttf", ".ttc", ".pyc"):
            continue
        if p.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if needle and needle in text.replace("\\", "/"):
            hits.append(p.relative_to(tree).as_posix())
    return hits


def write_manifest(pkg: Path, *, version: str, components: dict, notes: list[str]) -> dict:
    files = {}
    for p in sorted(Path(pkg).rglob("*")):
        if p.is_file() and p.name != "package_manifest.json":
            files[p.relative_to(pkg).as_posix()] = sha256_file(p)
    manifest = {
        "package_version": version,
        "platform": PLATFORM,
        "python": PY_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "components": components,
        "file_count": len(files),
        "files": files,
        "notes": notes,
    }
    (pkg / "package_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    (pkg / "VERSION").write_text(version + "\n", encoding="utf-8")
    return manifest


def prepare_runtime(pkg: Path, cache: Path, *, sha_pin: str, offline: bool) -> dict:
    """准备可重定位 Python 运行时（默认 python.org embeddable zip）。"""
    zip_path = inside(cache, Path(cache) / f"python-{PY_VERSION}-embed-amd64.zip")
    if zip_path.exists():
        info = {"url": PY_URL, "bytes": zip_path.stat().st_size,
                "sha256": sha256_file(zip_path), "pinned": bool(sha_pin), "cached": True}
    elif offline:
        raise RuntimeError(f"离线模式但缓存里没有 {zip_path.name}")
    else:
        info = fetch(PY_URL, zip_path, sha256_pin=sha_pin)
    runtime = inside(pkg, Path(pkg) / "runtime")
    if runtime.exists():
        shutil.rmtree(runtime)
    runtime.mkdir(parents=True)
    ok, detail = dep_check._safe_extract_zip(zip_path, runtime)
    if not ok:
        raise RuntimeError(f"运行时解压失败：{detail}")
    info["files"] = detail
    # embeddable 发行版默认不加载 site-packages：在**原有** ._pth 基础上追加
    # （不能整份重写：标准库 zip 名是 pythonXY.zip，写错会 "No module named 'encodings'"）
    pth = next(runtime.glob("python*._pth"), None)
    if pth is None:
        raise RuntimeError("运行时里没有 ._pth（不是 embeddable 发行版？）")
    stdlib_zip = f"python{PY_VERSION.split('.')[0]}{PY_VERSION.split('.')[1]}.zip"
    keep = [ln for ln in pth.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().lower().startswith("import ")]
    for extra in (stdlib_zip, ".", "Lib\\site-packages"):
        if extra not in keep:
            keep.append(extra)
    pth.write_text("\n".join(keep + ["import site"]) + "\n", encoding="utf-8")
    if not (runtime / stdlib_zip).exists():
        raise RuntimeError(f"运行时缺少标准库 {stdlib_zip}")
    info["pth"] = pth.name
    info["stdlib_zip"] = stdlib_zip
    return info


def install_wheels(pkg: Path, lock: Path, index_url: str, *, python_exe: str) -> dict:
    """把 Windows(cp311) 运行依赖装进包内 site-packages（交叉安装，优先 wheel）。

    两段式：先整体只取 wheel；若某些包在目标平台**没有 wheel**（如 jieba 只有 sdist），
    把这些包挑出来单独从源码包构建安装（纯 Python 包可直接构建；需要编译器的包会如实
    失败并进报告）。索引地址先校验；子进程一律参数列表 + shell=False，不拼命令串。
    """
    index_url = validate_index_url(index_url)
    lock_path = Path(lock).resolve()
    if not lock_path.is_file():
        raise RuntimeError(f"依赖锁不存在：{lock_path}")
    target = inside(pkg, Path(pkg) / "runtime" / "Lib" / "site-packages")
    target.mkdir(parents=True, exist_ok=True)

    def _pip(req_file: Path, *, no_binary: bool = False) -> subprocess.CompletedProcess:
        argv = [str(python_exe), "-m", "pip", "install", "--no-input",
                "--disable-pip-version-check", "--platform", PLATFORM,
                "--python-version", "3.11", "--implementation", "cp",
                "--target", str(target), "-i", str(index_url),
                "--timeout", "120", "--retries", "5"]
        argv += ["--no-binary", ":all:"] if no_binary else ["--only-binary", ":all:"]
        argv += ["-r", str(req_file)]
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=3600, shell=False, check=False)

    pins = [ln.strip() for ln in lock_path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    first = _pip(lock_path)
    sdist_only: list[str] = []
    if first.returncode != 0:
        blob = (first.stderr or "") + (first.stdout or "")
        found = re.findall(r"No matching distribution found for ([A-Za-z0-9_.\-]+)", blob)
        sdist_only = sorted({n for n in found})
        if not sdist_only:
            tail = (first.stderr or first.stdout or "").strip().splitlines()[-6:]
            raise RuntimeError("依赖安装失败：\n" + "\n".join(tail))
        skip = {n.lower() for n in sdist_only}
        keep = [p for p in pins if p.split("==")[0].lower() not in skip]
        sdist_pins = [p for p in pins if p.split("==")[0].lower() in skip]
        wheels_req = inside(pkg, Path(pkg) / "runtime" / "_wheels_only.txt")
        wheels_req.write_text("\n".join(keep) + "\n", encoding="utf-8")
        try:
            second = _pip(wheels_req)
        finally:
            wheels_req.unlink(missing_ok=True)
        if second.returncode != 0:
            tail = (second.stderr or second.stdout or "").strip().splitlines()[-6:]
            raise RuntimeError("依赖安装失败（wheel 段）：\n" + "\n".join(tail))
        sdist_req = inside(pkg, Path(pkg) / "runtime" / "_sdist_only.txt")
        sdist_req.write_text("\n".join(sdist_pins) + "\n", encoding="utf-8")
        try:
            sdist_info = install_sdist_only(pkg, sdist_pins, index_url=index_url,
                                            python_exe=python_exe)
        finally:
            sdist_req.unlink(missing_ok=True)
        return {"target": str(target.relative_to(pkg)), "packages": _count_dist_infos(target),
                "sdist_only": sdist_only, "sdist_install": sdist_info}
    return {"target": str(target.relative_to(pkg)), "packages": _count_dist_infos(target),
            "sdist_only": sdist_only}


def install_sdist_only(pkg: Path, pins: list[str], *, index_url: str,
                       python_exe: str) -> dict:
    """sdist-only 包（如 jieba）：在构建机按**宿主**解释器构建后拷进包内 site-packages。

    为什么这么做：pip 不允许"跨平台 + 源码构建"（`--platform` 与 `--no-binary` 互斥），
    所以没有 wheel 的包只能在构建机上装一次再搬进去。这只对**纯 Python** 包成立——
    因此构建报告如实标注"按宿主构建"，并由包内解释器导入验证兜底；装不上就如实记录跳过。
    """
    tmp = inside(pkg, Path(pkg) / "runtime" / "_sdist_build")
    if tmp.exists():
        shutil.rmtree(tmp)
    argv = [str(python_exe), "-m", "pip", "install", "--no-input", "--no-deps",
            "--disable-pip-version-check", "--target", str(tmp),
            "-i", str(index_url), "--timeout", "120", "--retries", "5"] + list(pins)
    proc = subprocess.run(argv, capture_output=True, text=True,
                          timeout=1800, shell=False, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-4:]
        return {"ok": False, "detail": "构建机安装失败：" + " / ".join(tail),
                "note": "该包未随包（缺失时按各包自身的回退行为运行）"}
    site = inside(pkg, Path(pkg) / "runtime" / "Lib" / "site-packages")
    site.mkdir(parents=True, exist_ok=True)
    moved = 0
    for item in sorted(tmp.iterdir()):
        if item.name == "__pycache__" or item.suffix == ".pyc":
            continue          # 宿主解释器的字节码（如 cpython-314）对包内 3.11 无用
        dst = inside(site, site / item.name)
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        shutil.move(str(item), str(dst))
        moved += 1
    # 顺手清掉包内的宿主字节码缓存
    for cache in site.rglob("__pycache__"):
        if any(part == "jieba" for part in cache.parts):
            shutil.rmtree(cache, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, "moved": moved, "packages": list(pins),
            "note": "按宿主解释器构建（纯 Python 包假设），由包内导入验证兜底"}


def _count_dist_infos(site_packages: Path) -> int:
    return len(list(site_packages.glob("*.dist-info"))) + len(list(site_packages.glob("*.egg-info")))


def verify_in_package(pkg: Path) -> dict:
    """用**包内解释器**验证：关键导入 + SSL/SQLite（可迁移性最低要求）。"""
    py = inside(pkg, Path(pkg) / "runtime" / "python.exe")
    if not py.exists():
        return {"ok": False, "detail": "包内没有 runtime/python.exe（非 Windows 构建机？）"}
    code = ("import ssl, sqlite3, json, sys;"
            "import redis, aiosqlite, httpx;"
            "print(json.dumps({'python': sys.version.split()[0],"
            " 'openssl': ssl.OPENSSL_VERSION, 'sqlite': sqlite3.sqlite_version}))")
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    proc = subprocess.run([str(py), "-c", code], capture_output=True, text=True,
                          timeout=300, cwd=str(pkg), env=env, shell=False, check=False)
    out = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    result = {"ok": proc.returncode == 0, "detail": out[0][:200],
              "stderr": (proc.stderr or "").strip()[-300:] if proc.returncode else ""}
    # 可选：随包但非必需的包（sdist-only 搬进来的）逐个试导入，结果如实记录不拦构建
    optional = os.environ.get("WM_PACKAGE_IMPORT_CHECK", "").strip()
    if optional and result["ok"]:
        names = [n.strip() for n in optional.replace(",", " ").split() if n.strip()]
        checks = {}
        for mod in names:
            p2 = subprocess.run([str(py), "-c", f"import {mod}"], capture_output=True,
                                text=True, timeout=120, cwd=str(pkg), env=env,
                                shell=False, check=False)
            checks[mod] = p2.returncode == 0
        result["optional_imports"] = checks
    return result


def build(args) -> int:
    version = args.version or time.strftime("%Y.%m.%d")
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pkg = inside(out_dir, out_dir / f"weavemind-{version}-win-x64")
    cache = (Path(args.offline_dir).resolve() if args.offline_dir
             else ROOT / ".weavemind" / "downloads")
    report: dict = {"version": version, "platform": PLATFORM, "steps": {}, "notes": []}
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)

    # ① Windows 运行依赖锁（Linux 锁不能跨平台用）
    win_lock = ROOT / "requirements-runtime-win.lock"
    if not win_lock.exists():
        lock_argv = [sys.executable, str(ROOT / "scripts" / "make_lock.py"), "--runtime",
                     "--platform", PLATFORM, "--python-version", "3.11",
                     "--out", win_lock.name,
                     "--index-url", validate_index_url(args.index_url)]
        proc = subprocess.run(lock_argv, capture_output=True, text=True,
                              timeout=1800, shell=False, check=False)
        report["steps"]["win_lock"] = {"ok": proc.returncode == 0,
                                       "detail": (proc.stdout or proc.stderr)[-300:]}
        if proc.returncode != 0 or not win_lock.exists():
            print("Windows 依赖锁生成失败；见 build_report.json", file=sys.stderr)
            (out_dir / "build_report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
            return 2
    report["steps"]["win_lock"] = {"ok": True, "path": win_lock.name,
                                   "sha256": sha256_file(win_lock)}

    # ② 运行时
    if not args.skip_fetch:
        report["steps"]["runtime"] = prepare_runtime(
            pkg, cache, sha_pin=args.python_sha256, offline=False)
    else:
        report["steps"]["runtime"] = {"skipped": True}
    # ③ 依赖
    if not args.skip_fetch and inside(pkg, Path(pkg) / "runtime" / "python.exe").exists():
        report["steps"]["wheels"] = install_wheels(
            pkg, win_lock, args.index_url, python_exe=sys.executable)
    else:
        report["steps"]["wheels"] = {"skipped": True}
    # ④ 源码（显式清单）
    copied = []
    for src in source_files(ROOT):
        rel = src.relative_to(ROOT)
        dst = inside(pkg, pkg / rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel.as_posix())
    report["steps"]["sources"] = {"count": len(copied)}
    # ⑤ 便携 Redis（离线复用入口：放 .weavemind/downloads/redis-windows.zip）
    redis_zip = inside(cache, Path(cache) / "redis-windows.zip")
    if redis_zip.exists():
        dst = inside(pkg, Path(pkg) / ".weavemind" / "downloads" / "redis-windows.zip")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(redis_zip, dst)
        got = sha256_file(dst)
        reference = args.redis_sha256 or DEFAULT_REDIS_SHA256
        report["steps"]["redis"] = {"sha256": got, "matches_reference": got == reference,
                                    "pinned": bool(args.redis_sha256)}
        if args.redis_sha256 and got.lower() != args.redis_sha256.lower():
            print("Redis 摘要不符（--redis-sha256）", file=sys.stderr)
            return 3
    else:
        report["steps"]["redis"] = {"missing": True}
        report["notes"].append("未随包提供便携 Redis 压缩包：首次运行需要联网获取")
    # ⑥ 中文字体：Windows 自带中文字体；随包分发需显式 --font
    if args.font:
        f = Path(args.font)
        if not f.is_file():
            raise RuntimeError(f"--font 不是文件：{f}")
        dst = inside(pkg, Path(pkg) / "runtime" / "fonts" / f.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dst)
        report["steps"]["font"] = {"bundled": f.name, "sha256": sha256_file(dst)}
    else:
        report["steps"]["font"] = {"bundled": None,
                                   "note": "使用系统字体（Windows 自带中文字体）；Docker 路径仍缺字体"}

    # ⑦ 扫描与验证
    report["secrets"] = scan_secrets(pkg)
    report["dev_paths"] = scan_dev_paths(pkg)
    report["verify"] = verify_in_package(pkg) if not args.skip_verify else {"skipped": True}
    manifest = write_manifest(
        pkg, version=version,
        components={"runtime": report["steps"]["runtime"].get("sha256", ""),
                    "python": PY_VERSION, "platform": PLATFORM,
                    "wheels": report["steps"]["wheels"].get("packages", 0),
                    "redis": report["steps"]["redis"].get("sha256", ""),
                    "frontend": inside(pkg, Path(pkg) / "frontend" / "dist" / "index.html").exists(),
                    "font": report["steps"]["font"].get("bundled")},
        notes=report["notes"])
    # ⑧ 打包 zip
    zip_path = inside(out_dir, out_dir / f"{pkg.name}.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(pkg.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(out_dir).as_posix())
    report["package"] = {"dir": pkg.name, "zip": zip_path.name,
                         "zip_sha256": sha256_file(zip_path),
                         "zip_bytes": zip_path.stat().st_size,
                         "manifest_files": manifest["file_count"]}
    report["clean_env_verified"] = False
    report["notes"].append("干净环境（无 Python/Node/Docker）验收属 N4，本报告不代表已通过")
    (out_dir / "build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = (not report["secrets"] and not report["dev_paths"]
          and report["verify"].get("ok", False))
    print(json.dumps({"ok": ok, "package": report["package"],
                      "secrets": len(report["secrets"]),
                      "dev_paths": len(report["dev_paths"]),
                      "verify": report["verify"].get("detail", "")},
                     ensure_ascii=False, indent=1))
    return 0 if ok else 4


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 Windows 新人运行包（本地候选，不发布）")
    ap.add_argument("--version", default="", help="包版本（默认当天日期）")
    ap.add_argument("--out", default="dist", help="输出目录")
    ap.add_argument("--index-url", default=DEFAULT_INDEX, help="wheel 索引（默认阿里云镜像）")
    ap.add_argument("--offline-dir", default="", help="离线素材目录（含 python/redis 压缩包）")
    ap.add_argument("--python-sha256", default="", help="固定 Python 运行时 zip 摘要")
    ap.add_argument("--redis-sha256", default="", help="固定便携 Redis zip 摘要")
    ap.add_argument("--font", default="", help="随包分发的中文 TrueType 字体路径（可选）")
    ap.add_argument("--skip-fetch", action="store_true", help="只做打包与扫描，不下载")
    ap.add_argument("--skip-verify", action="store_true", help="跳过包内解释器验证")
    args = ap.parse_args()
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
