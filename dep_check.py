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
# 运行时目录与 launcher 的 PID 目录统一为 .weavemind（曾用近名 .weavimind，极易混淆）
RUNTIME_DIR = BASE_DIR / ".weavemind"
LEGACY_RUNTIME_DIR = BASE_DIR / ".weavimind"   # 旧目录：仅用于一次性迁移
DOWNLOAD_DIR = RUNTIME_DIR / "downloads"
REDIS_DIR = RUNTIME_DIR / "redis"
# 便携版解压到隔离子目录：避免与历史遗留目录（可能被占用/版本过旧）互相干扰
PORTABLE_DIR = REDIS_DIR / "portable"
REDIS_PID_FILE = RUNTIME_DIR / "redis.pid"
LOG_DIR = BASE_DIR / "logs"
FRONTEND_INDEX = BASE_DIR / "frontend" / "dist" / "index.html"

# 便携版 Redis 下载源（redis-windows 官方 release；可用环境变量换镜像，
# 但仍须通过 _validate_download_url 的白名单与公网地址校验）
# 兼容性下限：项目用的 redis-py 8 默认 RESP3（HELLO 3），Redis 5 不支持该命令，
# 会表现为"服务启动即崩、日志报 unknown command HELLO"——故必须取 Redis 6+。
REDIS_ZIP_URL = os.environ.get(
    "WM_REDIS_ZIP_URL",
    "https://github.com/redis-windows/redis-windows/releases/download/8.10.1/"
    "Redis-8.10.1-Windows-x64-msys2.zip",
)
# 国内网络下 GitHub release 常下不动：按顺序试多个镜像（每个源都有独立超时，
# 总预算见 WM_REDIS_FETCH_BUDGET）。镜像只是**同一条 release 资源的转发**，
# 第三方转发不构成信任来源——因此：
#   1) 仍走 https + host 白名单 + 重定向逐跳复校 + 体积上限；
#   2) 可用 WM_REDIS_ZIP_SHA256 固定摘要，下载后强校验（推荐；摘要请与官方 release 页核对）；
#   3) 未固定摘要时把实际摘要打进日志，便于事后对账。
REDIS_MIRROR_PREFIXES = (
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
)
# 镜像前缀随源地址一起使用，因此这些转发主机也要在白名单内（仅用于本文件的下载器）
MIRROR_HOSTS = ("ghproxy.net", "gh-proxy.com", "ghfast.top")
REDIS_BIN = "redis-server.exe"
REDIS_MIN_MAJOR = 6
DOWNLOAD_HOSTS = (
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",      # release 下载的实际 302 目标
    "github-releases.githubusercontent.com",      # 同一资源域的旧名
    "codeload.github.com",
    # 可重定位 Python 运行时（Windows 新人运行包构建用；python.org 官方发布页）。
    # 注意：校验器会先剥掉 "www." 前缀，所以白名单里写裸域名。
    "python.org",
) + MIRROR_HOSTS
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024  # 60MB（Redis zip 约 14MB）
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


def _t(zh: str, en: str) -> str:
    """按输出模式取文案（WM_PLAIN_TEXT=1 时用英文）；cli_text 缺失时回退中文。"""
    try:
        import cli_text
        return cli_text.msg(zh, en)
    except Exception:
        return zh


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


def _probe_hosts() -> list[str]:
    """探测用的主机列表：localhost 同时试 IPv4/IPv6。

    Windows 防火墙按程序+路径放行，且便携 Redis 默认监听 IPv4；而
    `localhost` 可能先解析到 IPv6 `::1`，导致"服务在跑却探测不通"的假阴性。
    """
    host = _env_host()
    if host in ("localhost", ""):
        return ["127.0.0.1", "::1"]
    return [host]


def redis_bind_addr() -> str:
    """便携 Redis 的绑定地址（默认仅本机回环，IPv4+IPv6 双栈）。

    只绑回环的两个好处：不对局域网暴露（安全），且**不会触发 Windows
    防火墙的入站放行询问**（此前绑 0.0.0.0 会反复弹框，用户点了取消还会
    被直接拦掉连接）。用 WM_REDIS_BIND 可覆盖（如 0.0.0.0 供局域网共享）。"""
    return (os.environ.get("WM_REDIS_BIND") or "127.0.0.1 -::1").strip()


# ─────────────────────────────────────────────
# 检测
# ─────────────────────────────────────────────

def _migrate_legacy_runtime_dir() -> str:
    """把旧运行时目录 .weavimind 的内容迁到 .weavemind（幂等；失败静默）。

    返回迁移说明（空串=无需迁移）。便携 Redis 目录被迁移后，运行中的实例
    仍指向旧路径——由 ensure_redis 的版本/存活校验自然处理（必要时重启）。"""
    try:
        if not LEGACY_RUNTIME_DIR.exists():
            return ""
        moved: list[str] = []
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        for child in list(LEGACY_RUNTIME_DIR.iterdir()):
            target = RUNTIME_DIR / child.name
            if target.exists():
                continue
            try:
                shutil.move(str(child), str(target))
                moved.append(child.name)
            except Exception:
                continue  # 被占用（如运行中的 redis）：保留旧位置，不阻断启动
        try:
            if not any(LEGACY_RUNTIME_DIR.iterdir()):
                LEGACY_RUNTIME_DIR.rmdir()
        except Exception:
            pass
        return f"migrated: {', '.join(moved)}" if moved else ""
    except Exception:
        return ""


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
    """socket + PING 探测（不依赖 redis 包，装依赖前也可用）。

    host 为空时按 _probe_hosts() 依次尝试（localhost 会同时试 127.0.0.1 与 ::1），
    避免 IPv4 监听 + IPv6 解析造成的假不可达。"""
    import socket
    hosts = [host] if host else _probe_hosts()
    target_port = _valid_port(port or _env_port())
    for candidate in hosts:
        try:
            with socket.create_connection((candidate, target_port),
                                          timeout=timeout) as conn:
                conn.sendall(b"PING\r\n")
                if conn.recv(64).startswith(b"+PONG"):
                    return True
        except Exception:
            continue
    return False


def check_frontend() -> dict:
    if FRONTEND_INDEX.exists():
        return {"ok": True, "detail": _t("前端产物已就绪（frontend/dist，免 Node）",
                                            "frontend dist ready (no Node needed)")}
    has_node = shutil.which("node") is not None
    hint = _t("前端未构建：可执行 cd frontend && npm install && npm run build",
              "frontend not built: run `cd frontend && npm install && npm run build`"
              ) if has_node else _t(
        "前端未构建且本机无 Node：将显示自包含状态页（功能受限）",
        "frontend not built and no Node: a built-in status page will be shown")
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
            "detail": (_t(f"Redis 可达（{_env_host()}:{_env_port()}）",
                          f"Redis reachable ({_env_host()}:{_env_port()})")
                       if redis_ok else _t("Redis 不可达", "Redis unreachable")),
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


def redis_zip_sources() -> list[str]:
    """便携 Redis 的下载源，按尝试顺序（MKT-P0-2）。

    顺序：用户显式 `WM_REDIS_ZIP_URL` → `WM_REDIS_MIRRORS`（逗号分隔，**镜像前缀**；
    以 `.zip` 结尾的条目按完整地址处理）→ 内置镜像 → 官方 GitHub release。
    用户显式给的排最前（尊重"我知道从哪下"），官方源排最后兜底。
    """
    srcs: list[str] = []

    def _expand(item: str) -> str:
        item = item.strip()
        if not item:
            return ""
        if item.lower().endswith(".zip"):
            return item                 # 已经是完整地址
        return item.rstrip("/") + "/" + REDIS_ZIP_URL

    explicit = str(os.environ.get("WM_REDIS_ZIP_URL") or "").strip()
    if explicit:
        srcs.append(explicit)
    extra = str(os.environ.get("WM_REDIS_MIRRORS") or "").strip()
    if extra:
        srcs.extend(_expand(u) for u in extra.split(","))
    mirror_base = str(os.environ.get("WM_REDIS_MIRROR_BASE") or "").strip()
    prefixes = (mirror_base,) if mirror_base else REDIS_MIRROR_PREFIXES
    srcs.extend(_expand(p) for p in prefixes)
    srcs.append(REDIS_ZIP_URL)
    # 去重且保持顺序
    seen: set[str] = set()
    out: list[str] = []
    for u in srcs:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 512), b""):
                h.update(chunk)
    except Exception:
        return ""
    return h.hexdigest()


def fetch_portable_redis(zip_path: Path, *, budget: float | None = None,
                         per_source: float = 20.0) -> tuple[bool, str, str]:
    """多源获取便携 Redis（MKT-P0-2）：返回 `(ok, 说明, 实际使用的源)`。

    - **离线可复制**：目标 zip 已存在且是合法 zip 时直接用（不联网）；
    - **总预算**：默认 `WM_REDIS_FETCH_BUDGET`（60s），每个源 `per_source` 秒；
      预算耗尽即停，不再无限等某个源；
    - **摘要固定**：给了 `WM_REDIS_ZIP_SHA256` 就强校验，不符即换源；
      没给就把实际摘要打进日志，便于事后与官方 release 对账。
    """
    zip_path = Path(zip_path)
    if zip_path.exists() and zip_path.stat().st_size > 0:
        if zipfile.is_zipfile(zip_path):
            digest = _sha256_file(zip_path)
            return True, f"复用已有下载包（sha256 {digest[:16]}…）", "local-cache"
    try:
        total = float(budget if budget is not None
                      else (os.environ.get("WM_REDIS_FETCH_BUDGET", "60") or 60))
    except Exception:
        total = 60.0
    want_sha = str(os.environ.get("WM_REDIS_ZIP_SHA256") or "").strip().lower()
    deadline = time.time() + max(5.0, total)
    problems: list[str] = []
    for url in redis_zip_sources():
        if time.time() >= deadline:
            problems.append("预算耗尽，剩余源未尝试")
            break
        left = max(3.0, min(float(per_source), deadline - time.time()))
        ok, msg = _safe_download(url, zip_path, timeout=left)
        if not ok:
            problems.append(f"{_host_of(url)}: {msg}")
            continue
        got = _sha256_file(zip_path)
        if want_sha and got != want_sha:
            problems.append(f"{_host_of(url)}: 摘要不符（期望 {want_sha[:16]}…，实际 {got[:16]}…）")
            try:
                zip_path.unlink()
            except Exception:
                pass
            continue
        if not zipfile.is_zipfile(zip_path):
            problems.append(f"{_host_of(url)}: 下载内容不是合法 zip")
            try:
                zip_path.unlink()
            except Exception:
                pass
            continue
        log_line = f"sha256 {got[:16]}…" + ("（已按 WM_REDIS_ZIP_SHA256 校验）" if want_sha else "")
        return True, f"来自 {_host_of(url)}（{log_line}）", url
    return False, "；".join(problems[:6]) or "没有可用下载源", ""


def _system_redis_exe() -> Path | None:
    """Windows 上**系统已装**的 Redis：PATH → 已注册服务名 → 常见安装目录。

    为什么要先找它：能复用本机已有的 Redis 就不该联网下载（国内网络下 GitHub
    release 常下不动，而 Memurai/redis-windows 服务版往往已经装好）。
    版本下限仍按 `REDIS_MIN_MAJOR` 判定，版本过旧会被判为不可用。
    """
    if os.name != "nt":
        return None
    candidates: list[Path] = []
    found = shutil.which("redis-server") or shutil.which("redis-server.exe")
    if found:
        candidates.append(Path(found))
    # 已注册的 Windows 服务（服务名 → 可执行文件路径）
    for svc in ("Memurai", "Redis", "memurai", "redis"):
        try:
            proc = subprocess.run(["sc", "qc", svc], capture_output=True, text=True,
                                  timeout=8)
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        import re as _re
        m = _re.search(r'BINARY_PATH_NAME\s*:\s*(.+?)\s*$', proc.stdout or "", _re.M)
        if not m:
            continue
        raw = m.group(1).strip().strip('"')
        exe = Path(raw.split(" --")[0].strip().strip('"'))
        if exe.suffix.lower() == ".exe":
            candidates.append(exe)
    for base in (r"C:\Program Files\Memurai",
                 r"C:\Program Files\Redis",
                 r"C:\Redis",
                 str(Path(os.environ.get("LOCALAPPDATA") or "") / "Programs" / "Redis")):
        if not base:
            continue
        p = Path(base) / REDIS_BIN
        if p.exists():
            candidates.append(p)
    for path in candidates:
        try:
            if path.exists() and _redis_version_of(path) >= REDIS_MIN_MAJOR:
                return path
        except Exception:
            continue
    return None


def _redis_version_of(exe: Path) -> int:
    """跑一次 `--version` 取主版本号；失败返回 0（= 不可用）。"""
    try:
        proc = subprocess.run([str(exe), "--version"], capture_output=True, text=True,
                              timeout=8)
    except Exception:
        return 0
    import re as _re
    m = _re.search(r"v=(\d+)\.", (proc.stdout or "") + (proc.stderr or ""))
    return int(m.group(1)) if m else 0


def _host_of(url: str) -> str:
    try:
        return urllib.parse.urlsplit(str(url)).hostname or str(url)[:40]
    except Exception:
        return str(url)[:40]


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
    """用当前解释器 pip 安装指定包（只装缺失项，参数列表 + shell=False）。

    顺序：**先国内镜像、再默认源**。此前是"默认源失败后才回退镜像"，而国内
    pypi.org 往往不是"失败"而是"长时间无响应"——实测新手就卡在这一步很久
    （终端还因为 -q 没有任何输出，看起来像死了）。镜像可用 WM_PIP_INDEX_URL
    覆盖（指向自己的内网/厂商镜像），设为 off 则只用默认源。
    镜像地址按仓库既有 SSRF 规则校验：仅 http/https + 公网，环回/内网不采用。
    """
    if not packages:
        return True, "无需安装"
    mirror = _pip_mirror()
    attempts: list[tuple[str, str]] = []
    if mirror:
        attempts.append((f"镜像 {mirror}", mirror))
    attempts.append(("默认源 PyPI", ""))
    detail = ""
    for label, index_url in attempts:
        print(_t(f"      正在安装 {len(packages)} 个依赖（{label}，首次可能要几分钟）…",
                 f"      installing {len(packages)} packages via {label}..."),
              flush=True)
        ok, msg = _pip_install_once(packages, timeout=timeout, index_url=index_url)
        if ok:
            return True, f"{msg}（{label}）"
        detail = f"{detail}{'；' if detail else ''}{label}失败：{msg}"
    return False, f"{detail}（可设 WM_PIP_INDEX_URL 指定可用镜像）"


# pip 镜像默认值：既用于安装（WM_PIP_INDEX_URL），也写进失败指引，
# 避免"代码里的默认源"和"指引里让人试的源"各说各话。
DEFAULT_PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

# pip 装不上时的可执行指引：国内网络与装过安全软件的机器上，两条通道
# （镜像 / 默认源）可能同时失败——失败报告直接给出下一步，不让人去翻文档。
PIP_HINT = f"""\
安装失败时按顺序排查（国内网络 / 安全软件常见）：
      0) 先看清用的是哪个 shell：下面按 **cmd** 写。PowerShell 里
         `set X=Y` 不是设环境变量、`%X%` 不会被展开、`::` 不是注释，
         要写成 `$env:X = "Y"`（见第 2 步的两种写法）。
      1) 先确认通道本身：python -m pip install -U pip -i {DEFAULT_PIP_MIRROR}
         报错若是 403 / 超时 / DNS，先放行安全软件（会拦 pip 的 TLS）或设置/清空代理再重试
         （cmd: set HTTPS_PROXY=… ｜ PowerShell: $env:HTTPS_PROXY = "…"；清空即等号后留空）；
      2) 换镜像后重跑（cmd 与 PowerShell 各一行，任选其一）：
           cmd:        set WM_PIP_INDEX_URL=<镜像>
           PowerShell: $env:WM_PIP_INDEX_URL = "<镜像>"
         然后 python dep_check.py --fix
         常用镜像：清华 {DEFAULT_PIP_MIRROR} ｜ 阿里 mirrors.aliyun.com/pypi/simple
                 ｜ 腾讯 mirrors.cloud.tencent.com/pypi/simple ｜ 中科大 mirrors.ustc.edu.cn/pypi/simple
      3) 分小批装（14 个包一条命令时，任何一个解析失败都会整批失败；也可以直接把
         镜像地址写在 -i 后面，不用先设环境变量）：
         python -m pip install redis aiosqlite httpx -i <镜像> --timeout 120 --retries 5
         python -m pip install chromadb -i <镜像> --timeout 120 --retries 5
         （chromadb 依赖重、几十 MB，慢是正常的）
      4) 离线兜底：在能上网、Python 版本一致的机器上
           python -m pip download -r requirements.txt -d wheels -i <镜像>
         把 wheels 目录拷过来：python -m pip install --no-index --find-links=wheels -r requirements.txt
      详见 docs/部署指南.md「依赖装不上时的排查与离线安装」。"""

PIP_HINT_EN = f"""\
Installation failed. Try, in order (common with restricted networks):
      1) Check the channel itself: python -m pip install -U pip -i {DEFAULT_PIP_MIRROR}
         On 403/timeout/DNS errors, allow your security software through (it can break
         pip's TLS) or fix/clear the proxy setting, then retry;
      2) Switch mirror and rerun: set WM_PIP_INDEX_URL=<mirror> then python dep_check.py --fix
      3) Install in small batches (one failing requirement fails the whole command):
         python -m pip install redis aiosqlite httpx -i <mirror> --timeout 120 --retries 5
         python -m pip install chromadb -i <mirror> --timeout 120 --retries 5
      4) Fully offline: on a machine with the same Python version
           python -m pip download -r requirements.txt -d wheels -i <mirror>
         copy the wheels directory over and run
           python -m pip install --no-index --find-links=wheels -r requirements.txt
      See the deployment guide in docs/."""


def _pip_mirror() -> str:
    """镜像地址（默认清华源）；非法/关闭/非公网时返回空串表示只用默认源。"""
    raw = str(os.environ.get("WM_PIP_INDEX_URL", DEFAULT_PIP_MIRROR) or "").strip()
    if not raw or raw.lower() in ("off", "0", "none", "no"):
        return ""
    try:
        from urllib.parse import urlparse
        p = urlparse(raw)
        if p.scheme not in ("http", "https") or not p.hostname:
            return ""
    except Exception:
        return ""
    try:
        from adapters.transport import _validate_public_url
        if not _validate_public_url(raw):
            return ""          # 回环/内网/保留地址：不发起请求
    except Exception:
        return ""
    return raw


def _pip_install_once(packages: list[str], timeout: int = 600,
                      index_url: str = "") -> tuple[bool, str]:
    """单次 pip 安装（可指定 index-url）；关进度条但保留错误可见。"""
    cmd = [sys.executable, "-m", "pip", "install",
           "--progress-bar", "off", "--timeout", "30", "--retries", "1"]
    if index_url:
        cmd += ["--index-url", index_url]
    cmd += list(packages)
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


REDIS_HINT_EN = """\
Redis is not running and could not be fetched automatically. Options (any one,
keep it on port 6379):
  1) Memurai (Redis-compatible Windows service, free developer edition): https://www.memurai.com
  2) redis-windows (Redis 8.x Windows builds): github.com/redis-windows/redis-windows
  3) WSL2 / Linux: sudo apt install redis-server && sudo service redis-server start
Commands below use cmd syntax (set X=Y); in PowerShell write $env:X = "Y".
Or point at a Redis elsewhere: set REDIS_HOST=<host> & set REDIS_PORT=<port>, or skip
the check with SKIP_REDIS_CHECK=1 (Redis is the message bus - workers and the task queue
will not work; use it only to look at the UI or to install dependencies first).
NOTE: Redis 6+ is REQUIRED - this project uses redis-py 8 (RESP3/HELLO), which
Redis 5 does not support (services would crash at startup with "unknown command HELLO").
See the deployment guide in docs/ (section 5.1).
"""

REDIS_HINT = """\
Redis 未运行且无法自动获取。按"最省事优先"试这几步：

  0) 下面的 `set X=Y` 按 **cmd** 写。在 PowerShell 里它不是设环境变量、`%X%` 也不会展开，
     要写成 `$env:X = "Y"`（例：$env:WM_REDIS_MIRROR_BASE = "https://ghproxy.net"）。

  1) 先用系统已装的（推荐，不用联网）：
     - Windows 服务版：Memurai（https://www.memurai.com，Redis 7 兼容）装上即用
     - WSL2 / Linux：sudo apt install redis-server && sudo service redis-server start
     - 已有 Docker：docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine
     （tporadowski/redis 发布的是 Redis 5.x，本项目**不能用**——见下面版本说明）

  2) 下载慢/超时（国内网络常见）：换镜像或把包放到本机
     a. 指定镜像源后重跑（会依次尝试，60s 预算）：
          cmd: set WM_REDIS_MIRROR_BASE=https://ghproxy.net
          PowerShell: $env:WM_REDIS_MIRROR_BASE = "https://ghproxy.net"
     b. 或者手动下载 zip（约 14MB，来自 redis-windows 的 release）后放到
          {_zip_dir}/redis-windows.zip
        再重跑——**已存在的合法 zip 会直接复用，不再联网**。
     c. 有官方 release 页给出的 sha256 时，建议固定摘要再下载：
          cmd: set WM_REDIS_ZIP_SHA256=<官方 release 的 sha256>
          PowerShell: $env:WM_REDIS_ZIP_SHA256 = "<sha256>"
        （镜像只是转发，固定摘要才能真正校验内容；未固定时日志会打印实际摘要便于对账）

  3) 把 Redis 放在别的机器/端口（cmd 写法；PowerShell 换成 $env:REDIS_HOST / $env:REDIS_PORT）：
          set REDIS_HOST=<host>  &  set REDIS_PORT=<port>
     或者显式跳过检查（依赖自检与启动预检都认这个开关）：
          set SKIP_REDIS_CHECK=1
       注意：这是**放弃** Redis，不是"降级运行"——消息总线没了，worker 与任务队列
       不会工作，只适合先看界面或先装依赖；装好 Redis 后请去掉这个变量。

注意：需要 **Redis 6 及以上**——本项目用的 redis-py 8 默认 RESP3（HELLO 命令），
Redis 5 不支持，会表现为"服务启动即崩、日志报 unknown command HELLO"。
详见 docs/部署指南.md「无 Docker 的完整路径」。
"""

# 指引里的路径用**真实**下载目录拼出来：此前写死成 `.weavind/downloads/...`
# （少了 me），照着做的用户会把包放进一个永远不会被读取的目录。
REDIS_HINT = REDIS_HINT.replace("{_zip_dir}", f"{RUNTIME_DIR.name}/downloads")


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


def _redis_server_version(host: str = "", port: int = 0,
                          timeout: float = 2.0) -> int | None:
    """读取"正在运行的" Redis 主版本（inline INFO server，不依赖 redis 包）。

    raw PING 对 Redis 5 也会成功应答，但 redis-py 8 默认 RESP3（HELLO 3）
    不被 Redis 5 支持——因此必须校验运行实例的真实版本。
    """
    import socket
    target_host = host or _env_host()
    target_port = _valid_port(port or _env_port())
    try:
        with socket.create_connection((target_host, target_port),
                                      timeout=timeout) as conn:
            conn.sendall(b"INFO server\r\n")
            data = b""
            while b"redis_version" not in data and len(data) < 16384:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
        import re as _re
        m = _re.search(rb"redis_version:(\d+)\.", data)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _stop_recorded_redis() -> bool:
    """停掉本项目记录在案的便携版 Redis（仅按 redis.pid，避免误杀用户服务）。"""
    try:
        pid = int(REDIS_PID_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            proc = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                  shell=False, capture_output=True, timeout=10)
            return proc.returncode == 0
        import signal as _signal
        os.kill(pid, _signal.SIGTERM)
        return True
    except Exception:
        return False


def _find_redis_binary(root: Path) -> Path | None:
    """在解压目录内定位 redis-server.exe（发行包常有一层同名子目录）。"""
    try:
        if not root.exists():
            return None
        for candidate in root.rglob(REDIS_BIN):
            if candidate.is_file():
                return candidate
    except Exception:
        return None
    return None


def _redis_binary_version(exe: Path) -> int | None:
    """读取 redis-server 主版本号（--version，不绑定端口）；失败返回 None。"""
    try:
        proc = subprocess.run([str(exe), "--version"], shell=False,
                              capture_output=True, text=True, timeout=8)
        text = f"{proc.stdout} {proc.stderr}"
        import re as _re
        m = _re.search(r"v=(\d+)\.", text) or _re.search(r"(\d+)\.\d+\.\d+", text)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _usable_portable_redis() -> Path | None:
    """返回可用的便携版 redis-server（要求版本 ≥ 兼容下限）。

    检索顺序：新目录 portable/ → 新目录 → 旧运行时目录（迁移时被占用
    未能移动的实例），避免同一份二进制被重复下载。
    """
    legacy_redis = LEGACY_RUNTIME_DIR / "redis"
    for base in (PORTABLE_DIR, REDIS_DIR, legacy_redis / "portable", legacy_redis):
        exe = _find_redis_binary(base)
        if exe is None:
            continue
        major = _redis_binary_version(exe)
        if major is not None and major >= REDIS_MIN_MAJOR:
            return exe
    return None


def _redis_start_argv(exe_path: Path, port: int) -> list[str]:
    """便携 Redis 启动参数：默认只绑本机回环（IPv6 用 - 前缀，绑不上不报错）。

    只绑回环 → 不对局域网暴露，也不会触发 Windows 防火墙的入站放行询问。"""
    argv = [str(exe_path), "--port", str(port)]
    bind = redis_bind_addr()
    if bind:
        argv.append("--bind")
        argv.extend(bind.split())   # 支持 "127.0.0.1 -::1" 这类多地址写法
    return argv


def _publish_redis_host_env(bind: str) -> None:
    """把实际绑定地址写进 REDIS_HOST，供随后启动的服务子进程继承。

    只在本机回环时生效：避免子进程仍按 localhost(可能解析到 ::1) 去连。"""
    if not bind:
        return
    if bind not in ("127.0.0.1", "::1", "localhost") and bind != "0.0.0.0":
        return
    if os.environ.get("REDIS_HOST"):
        return  # 用户显式指定过，不覆盖
    if bind == "127.0.0.1":
        os.environ["REDIS_HOST"] = "127.0.0.1"


FIREWALL_HINT = """\
若刚弹出过"Windows 防火墙已阻止此应用"的对话框（或曾点过取消）：
  · 便携 Redis 现已默认只监听 127.0.0.1，正常情况不会再弹框；
  · 若仍有残留的"阻止"规则，用管理员 PowerShell 允许该程序（路径固定）：
      netsh advfirewall firewall add rule name="WeaveMind Redis" ^
        dir=in action=allow program="%PROGRAM%" enable=yes
    或直接在弹框里点"允许访问"。
"""


def redis_skip_requested() -> bool:
    """SKIP_REDIS_CHECK=1：显式跳过 Redis 检查（与 launcher 的启动预检同一个开关）。

    为什么依赖自检也要认：`start.bat` 的 [4/6] 步是**闸门**，不认这个开关时，
    用户照着失败指引 `set SKIP_REDIS_CHECK=1` 仍然被"必需依赖缺失"挡住，只能绕过
    start.bat 直接起 launcher——指引给了一条走不通的路。
    两处语义保持一致：显式设 1 即视为通过；区别是这里**打印警告**，不静默跳过。
    """
    return str(os.environ.get("SKIP_REDIS_CHECK", "0")).strip() == "1"


def _redis_skipped_result() -> dict:
    """跳过 Redis 检查时的统一结果（打印代价，绝不静默）。"""
    print(_t("      [!!] 已按 SKIP_REDIS_CHECK=1 跳过 Redis 检查：Redis 是消息总线，"
             "跳过不是「降级运行」——worker 与任务队列不会工作，只适合先看界面/先装依赖。",
             "      [!] Redis check skipped (SKIP_REDIS_CHECK=1): the message bus is "
             "unavailable, so workers and the task queue will not work."), flush=True)
    return {"ok": True, "action": "skipped",
            "detail": _t("已按 SKIP_REDIS_CHECK=1 跳过 Redis 检查（消息总线不可用："
                         "worker 与任务队列不会工作；装好 Redis 后请去掉该变量）",
                         "Redis check skipped (SKIP_REDIS_CHECK=1); remove it once Redis "
                         "is installed")}


def ensure_redis(auto: bool = True, wait_sec: float = 12.0) -> dict:
    """确保 Redis 可用：已运行→通过；否则按平台获取并启动。"""
    if redis_skip_requested():
        return _redis_skipped_result()
    host = _env_host()
    port = _env_port()
    if redis_ping("", port):
        major = _redis_server_version(host, port)
        if major is None or major >= REDIS_MIN_MAJOR:
            _publish_redis_host_env(redis_bind_addr())
            return {"ok": True, "action": "already_running",
                    "detail": _t(f"Redis 已运行（{host}:{port}，版本 {major or '未知'}）",
                              f"Redis already running ({host}:{port}, v{major or '?'})")}
        # 运行中的版本过低（Redis 5 不支持 HELLO）→ 停掉本项目启动的实例并换便携版
        replaced = _stop_recorded_redis()
        if not replaced:
            return {"ok": False, "action": "version_too_old",
                    "detail": (f"检测到 Redis {major}（需 ≥{REDIS_MIN_MAJOR}："
                               f"redis-py 8 用 RESP3/HELLO 握手，Redis 5 不支持），"
                               f"且该实例非本项目启动、不会自动停止。\n{REDIS_HINT}")}
        time.sleep(1.0)

    # 非 Windows：优先 PATH 上的 redis-server（不自动 apt/yum，需 root 交用户）
    if os.name != "nt":
        found = shutil.which("redis-server")
        if found:
            proc = _spawn_background(
                _redis_start_argv(Path(found).resolve(), port),
                LOG_DIR / "redis.log", cwd=RUNTIME_DIR)
            _write_redis_pid(proc.pid)
            if _wait_redis("", port, wait_sec):
                _publish_redis_host_env(redis_bind_addr())
                return {"ok": True, "action": "started",
                        "detail": _t(f"已启动本机 redis-server（pid={proc.pid}，绑定 {redis_bind_addr()}）",
                                      f"started system redis-server (pid={proc.pid}, bind {redis_bind_addr()})")}
            return {"ok": False, "action": "failed",
                    "detail": _t(f"redis-server 已启动但探测失败（pid={proc.pid}）",
                                  f"redis-server started but probe failed (pid={proc.pid})")}
        return {"ok": False, "action": "no_binary",
                "detail": _t(f"未找到 redis-server 且本机无 Redis。\n{REDIS_HINT}",
                             f"no redis-server on PATH and no local Redis.\n{REDIS_HINT_EN}")}

    # Windows：**先看系统已装的 Redis**（PATH / 已注册服务 / 常见安装目录），
    # 再复用便携版，最后才是多源下载（MKT-P0-2：不许"能复用却先联网下载"）。
    sys_redis = _system_redis_exe()
    if sys_redis is not None:
        proc = _spawn_background(_redis_start_argv(sys_redis, port),
                                 LOG_DIR / "redis.log", cwd=sys_redis.parent)
        _write_redis_pid(proc.pid)
        if _wait_redis("", port, wait_sec):
            _publish_redis_host_env(redis_bind_addr())
            return {"ok": True, "action": "started_system",
                    "detail": _t(f"已启动系统已装的 Redis（pid={proc.pid}，{sys_redis}）",
                                 f"started system-installed Redis (pid={proc.pid}, {sys_redis})")}
    redis_exe = _usable_portable_redis()
    if redis_exe is None:
        if not auto or os.environ.get("WM_NO_AUTO_DOWNLOAD", "0") == "1":
            return {"ok": False, "action": "download_disabled",
                    "detail": _t(f"Redis 缺失且自动下载已关闭。\n{REDIS_HINT}",
                                 f"Redis missing and auto-download disabled.\n{REDIS_HINT_EN}")}
        zip_path = DOWNLOAD_DIR / "redis-windows.zip"
        print(_t("      正在获取便携版 Redis（约 14MB；依次尝试镜像与官方源，"
                 "总预算 60s，可用 WM_REDIS_FETCH_BUDGET 调整）…",
                 "      fetching portable Redis (~14MB; trying mirrors then the "
                 "official source, 60s budget)..."), flush=True)
        t0 = time.time()
        ok, msg, used = fetch_portable_redis(zip_path)
        if not ok:
            return {"ok": False, "action": "download_failed",
                    "detail": _t(f"下载失败（已试 {len(redis_zip_sources())} 个源，"
                                 f"耗时 {time.time() - t0:.0f}s）：{msg}\n{REDIS_HINT}",
                                 f"download failed: {msg}\n{REDIS_HINT_EN}")}
        print(_t(f"      已获取：{msg}", f"      fetched: {msg}"), flush=True)
        ok, msg = _safe_extract_zip(zip_path, PORTABLE_DIR, expect_name=REDIS_BIN)
        if not ok:
            return {"ok": False, "action": "extract_failed",
                    "detail": _t(f"{msg}\n{REDIS_HINT}", f"{msg}\n{REDIS_HINT_EN}")}
        redis_exe = _usable_portable_redis()
        if redis_exe is None:
            return {"ok": False, "action": "extract_failed",
                    "detail": _t(f"解压后未找到可用 {REDIS_BIN}（{PORTABLE_DIR}）\n{REDIS_HINT}",
                                 f"no usable {REDIS_BIN} after extract ({PORTABLE_DIR})\n{REDIS_HINT_EN}")}
        _ = used
    proc = _spawn_background(_redis_start_argv(redis_exe, port),
                             LOG_DIR / "redis.log", cwd=redis_exe.parent)
    _write_redis_pid(proc.pid)
    if _wait_redis("", port, wait_sec):
        _publish_redis_host_env(redis_bind_addr())
        return {"ok": True, "action": "started",
                "detail": _t(f"已启动便携版 Redis（pid={proc.pid}，绑定 {redis_bind_addr()}，{redis_exe.parent}）",
                          f"started portable Redis (pid={proc.pid}, bind {redis_bind_addr()}, {redis_exe.parent})")}
    return {"ok": False, "action": "failed",
            "detail": _t(f"Redis 已启动但探测失败（pid={proc.pid}，见 logs/redis.log）\n"
                         + FIREWALL_HINT.replace("%PROGRAM%", str(redis_exe)),
                         f"Redis started but probe failed (pid={proc.pid}, see logs/redis.log)\n"
                         + FIREWALL_HINT.replace("%PROGRAM%", str(redis_exe)))}


def check_code_sandbox() -> dict:
    """代码执行沙箱状态（只报告，不改配置、不阻塞启动）。

    为什么放进依赖自检：默认要求容器隔离，本机隔离不可用时任何 `code_execution` 步骤
    都会被拒绝执行（不会退到宿主解释器跑模型生成的代码）。用户应当**在跑任务之前**就看到
    这件事，而不是等任务跑十几分钟、从失败步骤的详情里才发现。
    研究类任务不需要代码执行，所以这里**不作为启动阻塞项**（ok 恒为 True）。
    """
    try:
        from code_sandbox import sandbox_status
        st = sandbox_status()
    except Exception as exc:                     # noqa: BLE001 - 只报告
        return {"ok": True, "mode": "unknown", "ready": False,
                "detail": _t(f"沙箱状态未知（{str(exc)[:60]}）", "sandbox status unknown"),
                "status": None}
    ready = bool(st.get("isolation_ready"))
    exec_ok = bool(st.get("execution_available"))
    mode = str(st.get("mode") or "?")
    if ready:
        detail = _t(f"容器隔离已就绪（{mode}）：模型生成的代码在容器里运行",
                    f"container isolation ready ({mode})")
    elif exec_ok:
        detail = _t(f"已显式选择 {mode}：代码可执行，但**没有操作系统级隔离**",
                    f"explicit mode {mode}: code runs without OS-level isolation")
    else:
        detail = _t(
            f"容器隔离不可用（{st.get('isolation_reason') or '原因未知'}）："
            "涉及代码执行的步骤会被拒绝，其余能力（检索/结构化数据/图表/报告/交付）不受影响。\n"
            "       出路：① 安装并启动 Docker 后构建沙箱镜像 "
            "（docker build -f Dockerfile.sandbox -t weavimind-code-sandbox:latest .）；\n"
            "             ② 让任务不生成代码步骤（研究类任务默认如此）。\n"
            "       注意：不要用关闭隔离来解决（restricted/none 只能由操作者显式选择，"
            "不是给新人的出路）。",
            "container isolation unavailable: code steps will be refused; "
            "other capabilities are unaffected")
    return {"ok": True, "mode": mode, "ready": ready, "detail": detail, "status": st}


def ensure_all(auto: bool = True, include_optional: bool = True) -> dict:
    """完整自检 + 修复：Python 版本 / 依赖包 / Redis / 前端。"""
    migration = _migrate_legacy_runtime_dir()
    py = check_python_version()
    pkgs = check_packages()
    install_result = {"attempted": [], "installed": [], "failed": [],
                      "detail": _t("未启用修复", "fix not enabled")}
    if auto and (pkgs["missing_required"] or pkgs["missing_optional"]):
        install_result = install_missing(pkgs, include_optional=include_optional)
        pkgs = check_packages()  # 复检
    if auto:
        redis_result = ensure_redis(auto=True)
    elif redis_skip_requested():
        # 只报告模式也要认同一个开关：否则 `deps`（不带 --fix）仍会判"缺失必需项"
        redis_result = _redis_skipped_result()
    else:
        reachable = redis_ping()
        redis_result = {"ok": reachable, "action": "checked",
                        "detail": (_t("Redis 可达", "Redis reachable") if reachable
                                   else _t(f"Redis 不可达。\n{REDIS_HINT}",
                                           f"Redis unreachable.\n{REDIS_HINT_EN}"))}
    frontend = check_frontend()
    sandbox = check_code_sandbox()
    ok = bool(py["ok"] and not pkgs["missing_required"] and redis_result["ok"])
    return {
        "python": py,
        "packages": pkgs,
        "install": install_result,
        "redis": redis_result,
        "frontend": frontend,
        "code_sandbox": sandbox,
        "migration": migration,
        "ok": ok,
    }


# ─────────────────────────────────────────────
# 报告 / CLI
# ─────────────────────────────────────────────

def format_report(rep: dict) -> str:
    """人类可读报告（WM_PLAIN_TEXT=1 时输出纯英文，适配老旧终端/CI）。"""
    try:
        import cli_text
        t = cli_text.msg
    except Exception:
        t = lambda zh, en: zh  # noqa: E731
    lines = [t("依赖自检结果：", "Dependency check:")]
    lines.append(f"  [{'OK' if rep['python']['ok'] else '!!'}] {rep['python']['detail']}")
    pk = rep["packages"]
    lines.append(
        f"  [{'OK' if not pk['missing_required'] else '!!'}] "
        + t(f"依赖包 {pk['ready']}/{pk['total']} 就绪",
            f"python packages {pk['ready']}/{pk['total']} ready")
    )
    for item in pk["missing_required"]:
        lines.append("       " + t(f"必需缺失：{item['package']}（{item['why']}）",
                                  f"REQUIRED missing: {item['package']} ({item['why']})"))
    for item in pk["missing_optional"]:
        lines.append("       " + t(f"可选缺失：{item['package']}（{item['why']}）",
                                  f"optional missing: {item['package']} ({item['why']})"))
    ins = rep.get("install") or {}
    if ins.get("installed"):
        lines.append("       " + t(f"已自动安装：{', '.join(ins['installed'])}",
                                  f"auto-installed: {', '.join(ins['installed'])}"))
    if ins.get("failed"):
        lines.append("       " + t(f"安装失败：{', '.join(ins['failed'])}（{ins.get('detail', '')}）",
                                  f"install failed: {', '.join(ins['failed'])} ({ins.get('detail', '')})"))
        lines.append("       " + _t(PIP_HINT, PIP_HINT_EN))
    lines.append(f"  [{'OK' if rep['redis']['ok'] else '!!'}] {rep['redis']['detail']}")
    lines.append(f"  [{'OK' if rep['frontend']['ok'] else '-'}] {rep['frontend']['detail']}")
    _sb = rep.get("code_sandbox") or {}
    if _sb.get("detail"):
        # 沙箱不是启动阻塞项（研究类任务不需要代码执行），所以用中性标记而不是 [!!]
        lines.append(f"  [{'OK' if _sb.get('ready') else '--'}] {_sb['detail']}")
    if rep.get("migration"):
        lines.append("       " + t(f"运行时目录迁移：{rep['migration']}",
                                  f"runtime dir migration: {rep['migration']}"))
    lines.append(t("结论：", "Result: ") + (
        t("全部就绪，可以启动", "all set, ready to start") if rep["ok"]
        else t("存在必需项缺失（见上）", "required items missing (see above)")
    ))
    return "\n".join(lines)


def main(argv=None) -> int:
    try:
        import cli_text
        cli_text.setup_console_encoding()
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="织光启动依赖自检")
    ap.add_argument("--fix", action="store_true", help="自动补齐缺失依赖（装包/获取 Redis）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="仅输出结论行")
    ap.add_argument("--plain", action="store_true", help="纯英文输出（WM_PLAIN_TEXT=1 等价）")
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
