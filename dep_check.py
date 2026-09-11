# -*- coding: utf-8 -*-
"""启动依赖自检与自动获取（stdlib-only，可独立运行）。

设计约束（勿破坏）：
- 顶层零副作用：不联网、不安装、不探测；所有动作只在函数调用时发生。
  （test_p0 会 import launcher → import 本模块，CI 只装 requirements.txt。）
- 只用标准库；对第三方（adapters.transport 的公网校验）延迟导入。
- 下载严格校验：仅 https、host 白名单、拒绝环回/私网/保留地址、
  重定向逐跳复校、响应体大小上限。
- 解压不使用 extractall：逐条校验（禁绝对路径/上跳/符号链接/越界）后，
  按"已解析并确认位于目标目录内"的 Path 逐文件写出。
- 子进程一律参数列表 + shell=False；端口经范围校验后取整。

用法：
    python dep_check.py            # 只报告
    python dep_check.py --fix      # 检测并自动补齐（装包 / 获取 Redis）
    python dep_check.py --json     # 机器可读
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".weavimind"
DOWNLOAD_DIR = RUNTIME_DIR / "downloads"
REDIS_DIR = RUNTIME_DIR / "redis"
REDIS_PID_FILE = RUNTIME_DIR / "redis.pid"
LOG_DIR = BASE_DIR / "logs"
FRONTEND_INDEX = BASE_DIR / "frontend" / "dist" / "index.html"

# 便携版 Redis 下载源（tporadowski/redis 官方 release；可用环境变量换镜像，
# 但仍须通过 _validate_download_url 的白名单与公网地址校验）
REDIS_ZIP_URL = os.environ.get(
    "WM_REDIS_ZIP_URL",
    "https://github.com/tporadowski/redis/releases/download/v5.0.14.1/"
    "Redis-x64-5.0.14.1.zip",
)
REDIS_BIN = "redis-server.exe"
DOWNLOAD_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",      # release 下载的实际 302 目标
    "github-releases.githubusercontent.com",      # 同一资源域的旧名
    "codeload.github.com",
)
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024  # 60MB（Redis zip 约 5MB）
_UA = "WeaveMind-DepCheck/1.0"

# 依赖清单：模块名 → (pip 包名, 级别, 用途)
#   required  = 主流程硬依赖，缺失则服务不可用
#   optional  = 特定功能用（缺失只影响该功能）
#   fallback  = 有代码内回退（缺失仅提示）
MODULE_SPECS: tuple[dict, ...] = (
    {"module": "redis", "package": "redis", "level": "required", "why": "消息总线/任务队列"},
    {"module": "chromadb", "package": "chromadb", "level": "required", "why": "长期记忆向量库"},
    {"module": "aiosqlite", "package": "aiosqlite", "level": "required", "why": "worker 注册表"},
    {"module": "httpx", "package": "httpx", "level": "required", "why": "异步 LLM 调用"},
    {"module": "pandas", "package": "pandas", "level": "optional", "why": "数据分析/上传解析"},
    {"module": "sklearn", "package": "scikit-learn", "level": "optional", "why": "model_trainer 建模"},
    {"module": "matplotlib", "package": "matplotlib", "level": "optional", "why": "图表渲染"},
    {"module": "seaborn", "package": "seaborn", "level": "optional", "why": "统计图"},
    {"module": "requests", "package": "requests", "level": "optional", "why": "数据源抓取"},
    {"module": "ddgs", "package": "ddgs", "level": "optional", "why": "备用搜索源"},
    {"module": "pypdf", "package": "pypdf", "level": "optional", "why": "PDF 上传解析"},
    {"module": "docx", "package": "python-docx", "level": "optional", "why": "docx 上传解析"},
    {"module": "openpyxl", "package": "openpyxl", "level": "optional", "why": "xlsx 上传解析"},
    {"module": "jieba", "package": "jieba", "level": "fallback", "why": "中文分词（缺则 bigram 回退）"},
)

PY_MIN = (3, 10)
PY_MAX_INCL = (3, 14)
DEFAULT_PORT = 6379


def _valid_port(value, default: int = DEFAULT_PORT) -> int:
    """端口范围校验（1–65535），非法值回落默认端口。"""
    try:
        port = int(value)
    except Exception:
        return default
    return port if 1 <= port <= 65535 else default


def _env_port() -> int:
    return _valid_port(os.environ.get("REDIS_PORT", DEFAULT_PORT))


def _env_host() -> str:
    return (os.environ.get("REDIS_HOST") or "localhost").strip() or "localhost"


# ─────────────────────────────────────────────
# 检测
# ─────────────────────────────────────────────

def check_python_version(version_info=None) -> dict:
    """Python 版本检查：3.10–3.14 通过（越界给出可操作提示）。"""
    vi = version_info or sys.version_info
    cur = (int(vi[0]), int(vi[1]))
    ok = PY_MIN <= cur <= PY_MAX_INCL
    detail = f"Python {cur[0]}.{cur[1]}"
    if not ok:
        detail += f"（要求 {PY_MIN[0]}.{PY_MIN[1]}–{PY_MAX_INCL[0]}.{PY_MAX_INCL[1]}）"
    return {"ok": ok, "version": f"{cur[0]}.{cur[1]}", "detail": detail}


def _module_available(module: str) -> bool:
    """find_spec 探测：不触发 import 副作用（不执行模块代码）。"""
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:
        return False


def check_packages(specs=None) -> dict:
    """区分"必需缺失/可选缺失/已就绪"，返回结构化结果。"""
    specs = specs if specs is not None else MODULE_SPECS
    missing_required, missing_optional = [], []
    ready = 0
    for spec in specs:
        if _module_available(spec["module"]):
            ready += 1
            continue
        item = {k: spec[k] for k in ("module", "package", "level", "why")}
        (missing_required if spec["level"] == "required" else missing_optional).append(item)
    return {
        "ready": ready,
        "total": len(specs),
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }


def redis_ping(host: str = "", port: int = 0, timeout: float = 2.0) -> bool:
    """socket + PING 探测（不依赖 redis 包，装依赖前也可用）。"""
    import socket
    host = host or _env_host()
    target_port = _valid_port(port or _env_port())
    try:
        with socket.create_connection((host, target_port), timeout=timeout) as conn:
            conn.sendall(b"PING\r\n")
            return conn.recv(64).startswith(b"+PONG")
    except Exception:
        return False


def check_frontend() -> dict:
    if FRONTEND_INDEX.exists():
        return {"ok": True, "detail": "前端产物已就绪（frontend/dist，免 Node）"}
    has_node = shutil.which("node") is not None
    hint = ("前端未构建：可执行 cd frontend && npm install && npm run build"
            if has_node else "前端未构建且本机无 Node：将显示自包含状态页（功能受限）")
    return {"ok": False, "detail": hint, "has_node": has_node}


def check_dependencies() -> dict:
    py = check_python_version()
    pkgs = check_packages()
    redis_ok = redis_ping()
    report = {
        "python": py,
        "packages": pkgs,
        "redis": {
            "ok": redis_ok,
            "detail": (f"Redis 可达（{_env_host()}:{_env_port()}）"
                       if redis_ok else "Redis 不可达"),
        },
        "frontend": check_frontend(),
    }
    report["ok"] = bool(py["ok"] and not pkgs["missing_required"] and redis_ok)
    return report


# ─────────────────────────────────────────────
# 安全下载（https + host 白名单 + 公网地址 + 逐跳复校 + 大小上限）
# ─────────────────────────────────────────────

def _validate_download_url(url: str) -> tuple[bool, str]:
    """下载目标校验：仅 https、命中 host 白名单、解析后为公网地址。"""
    try:
        parts = urllib.parse.urlsplit(str(url or ""))
    except Exception:
        return False, "URL 解析失败"
    scheme = (parts.scheme or "").lower()
    if scheme != "https":
        return False, f"仅允许 https 下载（实际：{scheme or '空'}）"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return False, "缺少主机名"
    if host not in DOWNLOAD_HOSTS:
        return False, f"主机不在下载白名单：{host}"
    try:
        from adapters.transport import _validate_public_url
    except Exception as exc:  # 校验器不可用时拒绝下载（fail-closed）
        return False, f"公网校验不可用：{str(exc)[:60]}"
    if not _validate_public_url(url):
        return False, f"目标非公网地址（拒绝环回/私网/保留）：{host}"
    return True, "ok"


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向逐跳复校：任何一跳落到非白名单/私网即拒绝。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        ok, why = _validate_download_url(newurl)
        if not ok:
            raise urllib.error.HTTPError(
                newurl, code, f"blocked redirect: {why}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _safe_download(url: str, dest: Path, max_bytes: int = MAX_DOWNLOAD_BYTES,
                   timeout: float = 60.0) -> tuple[bool, str]:
    """把白名单公网 https 资源下载到 dest（超限/失败即中止并清理）。"""
    ok, why = _validate_download_url(url)
    if not ok:
        return False, why
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_SafeRedirectHandler())
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with opener.open(req, timeout=timeout) as resp:
            final_ok, final_why = _validate_download_url(resp.geturl())
            if not final_ok:
                return False, f"重定向后地址不合规：{final_why}"
            payload = bytearray()
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                payload.extend(chunk)
                if len(payload) > max_bytes:
                    return False, f"下载超过大小上限 {max_bytes // 1024 // 1024}MB"
        dest.write_bytes(bytes(payload))
        return True, f"已下载 {len(payload)} 字节 → {dest.name}"
    except Exception as exc:
        return False, f"下载失败：{str(exc)[:140]}"


def _archive_member_path(root: Path, member_name: str) -> Path | None:
    """压缩包条目 → 目标 Path；非法（绝对路径/上跳）或越界返回 None。"""
    normalized = str(member_name or "").replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return None
    parts = PurePosixPath(normalized).parts
    if not parts or ".." in parts:
        return None
    root_resolved = root.resolve()
    candidate = (root_resolved / PurePosixPath(*parts)).resolve()
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        return None
    return candidate


def _safe_extract_zip(zip_path: Path, dest_dir: Path,
                      expect_name: str = "") -> tuple[bool, str]:
    """安全解压（不使用 extractall）：先全量校验，再按已确认的 Path 写出。"""
    dest_root = Path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            if expect_name and not any(
                Path(i.filename).name == expect_name for i in infos
            ):
                return False, f"压缩包内未找到 {expect_name}"
            plan: list[tuple[zipfile.ZipInfo, Path]] = []
            for info in infos:
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    return False, f"压缩包含符号链接：{info.filename!r}"
                target = _archive_member_path(dest_root, info.filename)
                if target is None:
                    return False, f"压缩包含非法或越界路径：{info.filename!r}"
                plan.append((info, target))
            for info, target in plan:
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info))
        return True, f"已解压到 {dest_root}"
    except Exception as exc:
        return False, f"解压失败：{str(exc)[:140]}"


# ─────────────────────────────────────────────
# 修复：装包 / 获取 Redis
# ─────────────────────────────────────────────

def pip_install(packages: list[str], timeout: int = 600) -> tuple[bool, str]:
    """用当前解释器 pip 安装指定包（只装缺失项，参数列表 + shell=False）。"""
    if not packages:
        return True, "无需安装"
    cmd = [sys.executable, "-m", "pip", "install", "-q",
           "--timeout", "60", "--retries", "2", *packages]
    try:
        proc = subprocess.run(cmd, shell=False, capture_output=True, text=True,
                              timeout=timeout)
        if proc.returncode == 0:
            return True, f"已安装：{', '.join(packages)}"
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [""]
        return False, f"pip 安装失败（{' '.join(packages)}）：{tail[0][:120]}"
    except subprocess.TimeoutExpired:
        return False, f"pip 安装超时（{' '.join(packages)}）"
    except Exception as exc:
        return False, f"pip 安装异常：{str(exc)[:120]}"


def install_missing(pkgs: dict, include_optional: bool = True) -> dict:
    """补齐缺失包：必需项 +（可选）可选项；返回逐项结果。"""
    targets: list[str] = []
    for item in pkgs.get("missing_required") or []:
        targets.append(item["package"])
    if include_optional:
        for item in pkgs.get("missing_optional") or []:
            targets.append(item["package"])
    unique = sorted(set(targets))
    if not unique:
        return {"attempted": [], "installed": [], "failed": [], "detail": "依赖齐备"}
    ok, msg = pip_install(unique)
    return {
        "attempted": unique,
        "installed": unique if ok else [],
        "failed": [] if ok else unique,
        "detail": msg,
    }


REDIS_HINT = """\
Redis 未运行且无法自动获取时的三种方案（任选其一，保持 6379 端口即可）：
  1) Memurai（Redis 兼容的 Windows 服务，开发者版免费）：https://www.memurai.com
  2) tporadowski/redis：GitHub 搜 tporadowski/redis，解压后运行 redis-server.exe
  3) WSL2 / Linux：sudo apt install redis-server && sudo service redis-server start
详见 docs/部署指南.md「无 Docker 的完整路径」。
"""


def _spawn_background(argv: list[str], log_path: Path, cwd: Path | None = None):
    """后台启动外部程序（进程分离，父进程退出后仍存活）。

    Windows 需 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP：否则父 shell 退出时
    子进程随控制台作业一起被结束（曾导致"自检刚启动的 Redis 随即消失"）。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("a", encoding="utf-8")
    extra = {}
    if os.name == "nt":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # DETACHED_PROCESS 本身无控制台；再叠加 CREATE_NO_WINDOW 会冲突，故分开取用
        extra["creationflags"] = detached | new_group if detached else no_window
    return subprocess.Popen(
        argv, shell=False, stdout=log_handle, stderr=subprocess.STDOUT,
        cwd=str(cwd or BASE_DIR), **extra,
    )


def _write_redis_pid(pid: int) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        REDIS_PID_FILE.write_text(str(pid), encoding="utf-8")
    except Exception:
        pass


def _wait_redis(host: str, port: int, wait_sec: float) -> bool:
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if redis_ping(host, port):
            return True
        time.sleep(0.6)
    return False


def ensure_redis(auto: bool = True, wait_sec: float = 12.0) -> dict:
    """确保 Redis 可用：已运行→通过；否则按平台获取并启动。"""
    host = _env_host()
    port = _env_port()
    if redis_ping(host, port):
        return {"ok": True, "action": "already_running",
                "detail": f"Redis 已运行（{host}:{port}）"}

    # 非 Windows：优先 PATH 上的 redis-server（不自动 apt/yum，需 root 交用户）
    if os.name != "nt":
        found = shutil.which("redis-server")
        if found:
            proc = _spawn_background(
                [str(Path(found).resolve()), "--port", str(port)],
                LOG_DIR / "redis.log", cwd=RUNTIME_DIR)
            _write_redis_pid(proc.pid)
            if _wait_redis(host, port, wait_sec):
                return {"ok": True, "action": "started",
                        "detail": f"已启动本机 redis-server（pid={proc.pid}）"}
            return {"ok": False, "action": "failed",
                    "detail": f"redis-server 已启动但探测失败（pid={proc.pid}）"}
        return {"ok": False, "action": "no_binary",
                "detail": f"未找到 redis-server 且本机无 Redis。\n{REDIS_HINT}"}

    # Windows：已下载的直接复用，否则从白名单源下载（WM_NO_AUTO_DOWNLOAD=1 可关闭）
    redis_exe = REDIS_DIR / REDIS_BIN
    if not redis_exe.exists():
        if not auto or os.environ.get("WM_NO_AUTO_DOWNLOAD", "0") == "1":
            return {"ok": False, "action": "download_disabled",
                    "detail": f"Redis 缺失且自动下载已关闭。\n{REDIS_HINT}"}
        zip_path = DOWNLOAD_DIR / "redis-windows.zip"
        ok, msg = _safe_download(REDIS_ZIP_URL, zip_path)
        if not ok:
            return {"ok": False, "action": "download_failed",
                    "detail": f"{msg}\n{REDIS_HINT}"}
        ok, msg = _safe_extract_zip(zip_path, REDIS_DIR, expect_name=REDIS_BIN)
        if not ok:
            return {"ok": False, "action": "extract_failed",
                    "detail": f"{msg}\n{REDIS_HINT}"}
    proc = _spawn_background([str(redis_exe), "--port", str(port)],
                             LOG_DIR / "redis.log", cwd=REDIS_DIR)
    _write_redis_pid(proc.pid)
    if _wait_redis(host, port, wait_sec):
        return {"ok": True, "action": "started",
                "detail": f"已启动便携版 Redis（pid={proc.pid}，目录 {REDIS_DIR}）"}
    return {"ok": False, "action": "failed",
            "detail": f"Redis 已启动但探测失败（pid={proc.pid}，见 logs/redis.log）"}


def ensure_all(auto: bool = True, include_optional: bool = True) -> dict:
    """完整自检 + 修复：Python 版本 / 依赖包 / Redis / 前端。"""
    py = check_python_version()
    pkgs = check_packages()
    install_result = {"attempted": [], "installed": [], "failed": [], "detail": "未启用修复"}
    if auto and (pkgs["missing_required"] or pkgs["missing_optional"]):
        install_result = install_missing(pkgs, include_optional=include_optional)
        pkgs = check_packages()  # 复检
    if auto:
        redis_result = ensure_redis(auto=True)
    else:
        reachable = redis_ping()
        redis_result = {"ok": reachable, "action": "checked",
                        "detail": "Redis 可达" if reachable else f"Redis 不可达。\n{REDIS_HINT}"}
    frontend = check_frontend()
    ok = bool(py["ok"] and not pkgs["missing_required"] and redis_result["ok"])
    return {
        "python": py,
        "packages": pkgs,
        "install": install_result,
        "redis": redis_result,
        "frontend": frontend,
        "ok": ok,
    }


# ─────────────────────────────────────────────
# 报告 / CLI
# ─────────────────────────────────────────────

def format_report(rep: dict) -> str:
    lines = ["依赖自检结果："]
    lines.append(f"  [{'OK' if rep['python']['ok'] else '!!'}] {rep['python']['detail']}")
    pk = rep["packages"]
    lines.append(f"  [{'OK' if not pk['missing_required'] else '!!'}] "
                 f"依赖包 {pk['ready']}/{pk['total']} 就绪")
    for item in pk["missing_required"]:
        lines.append(f"       必需缺失：{item['package']}（{item['why']}）")
    for item in pk["missing_optional"]:
        lines.append(f"       可选缺失：{item['package']}（{item['why']}）")
    ins = rep.get("install") or {}
    if ins.get("installed"):
        lines.append(f"       已自动安装：{', '.join(ins['installed'])}")
    if ins.get("failed"):
        lines.append(f"       安装失败：{', '.join(ins['failed'])}（{ins.get('detail', '')}）")
    lines.append(f"  [{'OK' if rep['redis']['ok'] else '!!'}] {rep['redis']['detail']}")
    lines.append(f"  [{'OK' if rep['frontend']['ok'] else '-'}] {rep['frontend']['detail']}")
    lines.append("结论：" + ("全部就绪，可以启动" if rep["ok"] else "存在必需项缺失（见上）"))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="织光启动依赖自检")
    ap.add_argument("--fix", action="store_true", help="自动补齐缺失依赖（装包/获取 Redis）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="仅输出结论行")
    args = ap.parse_args(argv)

    rep = ensure_all(auto=args.fix)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    elif args.quiet:
        print("依赖自检：" + ("OK" if rep["ok"] else "缺失必需项"))
    else:
        print(format_report(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
