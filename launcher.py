"""织光 (ZhiGuang) - 统一服务进程管理器。

用法：
    python launcher.py             # 启动全部服务（先依赖自检 + 清理旧进程 + 启动后校验）
    python launcher.py start       # 同上
    python launcher.py deps        # 依赖自检（只报告）
    python launcher.py deps --fix  # 依赖自检并自动补齐（装包 / 获取 Redis）
    python launcher.py supervise   # 守护模式：启动全部服务后循环巡检，崩溃自动重启
    python launcher.py stop        # 按 PID 文件精确停止全部服务（含本项目启动的便携 Redis）
    python launcher.py status      # 查看运行状态（含依赖与 Redis 来源摘要）

所有服务 PID 写入 .weavemind/pids.json（同一目录还存放自动获取的 Redis 与下载缓存），
stop 时按 PID 精确结束，不再使用 taskkill /IM python.exe 之类的全杀方案。
start 时若环境变量 WEAVEMIND_SUPERVISE=1 同样进入守护模式（start.bat 默认不开）。
启动前会跑依赖自检（缺失的 pip 包自动补装、Redis 缺失按平台自动获取），
SKIP_DEP_CHECK=1 可跳过自检。
输出编码：自动适配 UTF-8（Windows 下尝试切到 65001 代码页）；老旧终端可设
WM_PLAIN_TEXT=1 输出纯英文。启动后校验：WM_START_VERIFY_WAIT 秒后汇总存活情况，
WM_START_STRICT=1 时未全部存活则以非零码退出。
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import db_paths
import logging_setup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
PID_DIR = BASE_DIR / ".weavemind"
PID_FILE = PID_DIR / "pids.json"
LOG_DIR = BASE_DIR / "logs"

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

logger = logging.getLogger(__name__)

# C1 守护模式参数：默认每 30 秒巡检一次（可用 SUPERVISE_INTERVAL 或
# config.json system.supervise_interval 覆盖）；连续重启失败 3 次后隔离 5 分钟防崩溃循环
SUPERVISE_INTERVAL_DEFAULT = 30.0
SUPERVISE_MAX_RESTARTS = 3
SUPERVISE_QUARANTINE_SECONDS = 300

# 织光服务已知脚本名（兜底清理时仅匹配本项目 BASE_DIR 下的这些脚本，避免误杀其他 Python 项目）
_RESIDUAL_SCRIPT_NAMES = {
    "orchestrator_v2.py",
    "web_ui.py",
    "critic_agent.py",
    "worker_guardian.py",
    "metrics_collector.py",
    "worker_base.py",
    "scheduler.py",
}


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _apply_env(cfg: dict) -> None:
    llm = cfg.get("llm", {})
    if llm.get("api_key"):
        os.environ["LLM_API_KEY"] = llm["api_key"]
    if llm.get("base_url"):
        os.environ["LLM_BASE_URL"] = llm["base_url"]
    if llm.get("model"):
        os.environ["LLM_MODEL"] = llm["model"]
    emb = cfg.get("embedding", {})
    if emb.get("api_key"):
        os.environ["EMBEDDING_API_KEY"] = emb["api_key"]
    if emb.get("base_url"):
        os.environ["EMBEDDING_BASE_URL"] = emb["base_url"]
    if emb.get("model"):
        os.environ["EMBEDDING_MODEL"] = emb["model"]
    # redis 段此前只是"文档里的配置"（生产代码只读 REDIS_HOST/PORT 环境变量，
    # 改 config.json 不生效）——这里补上映射：配置生效，且不覆盖已设的环境变量
    redis_cfg = cfg.get("redis", {})
    if redis_cfg.get("host") and not os.environ.get("REDIS_HOST"):
        os.environ["REDIS_HOST"] = str(redis_cfg["host"])
    if redis_cfg.get("port") and not os.environ.get("REDIS_PORT"):
        os.environ["REDIS_PORT"] = str(redis_cfg["port"])
    os.environ["PYTHONIOENCODING"] = "utf-8"
    # 任务库路径：所有子进程（编排器 / worker / web / 守护 / 调度）必须指向同一个文件，
    # 否则会出现"提交受理、状态缺失"。历史上 web 侧读 REGISTRY_DB、task_state 读
    # AGENTS_DB，Dockerfile 只设了前者，状态因此被写进既没建表、也不在挂载卷里的库。
    # 这里统一解析后注入：WEAVEMIND_DB 为唯一入口，另两个变量保留给外部脚本读取。
    db_file = db_paths.resolve_db_path()
    os.environ["WEAVEMIND_DB"] = db_file
    os.environ.setdefault("REGISTRY_DB", db_file)
    os.environ.setdefault("AGENTS_DB", db_file)


REDIS_SETUP_HINT = """\
无法连接 Redis（{host}:{port}）——织光的消息总线/任务队列依赖它，服务无法启动。

无需 Docker 的三种方案（任选其一，装好保持 6379 端口后重跑本命令）：
  1) Memurai（Redis 兼容的 Windows 服务，开发者版免费，Redis 7 兼容）：https://www.memurai.com
  2) redis-windows（Redis 8.x 的 Windows 构建，与自动获取的便携版同源）：
     GitHub 搜 redis-windows/redis-windows，Releases 下载 zip 解压后双击
     redis-server.exe，或 redis-server.exe --service-install 注册服务
  3) WSL2：wsl --install 后 sudo apt install redis-server && sudo service redis-server start
  或使用 Docker 方式：docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine

注意：Redis 需 >= 6（本项目用 redis-py 8 的 RESP3/HELLO 握手）。tporadowski/redis
发布的是 Redis 5.x，装上去会"启动即崩、日志报 unknown command HELLO"，不要用它。
详见 docs/部署指南.md「无 Docker 的完整路径」；也可用 REDIS_HOST/REDIS_PORT
指向其它机器上的 Redis，或用 SKIP_REDIS_CHECK=1 跳过本检查（依赖自检也认这个开关，
但消息总线不可用：worker 与任务队列不会工作）。
环境变量按 cmd 写（set X=Y）；PowerShell 里写 $env:X = "Y"。"""


def _redis_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    """socket + PING 探测 Redis 可达性。

    localhost 会同时尝试 127.0.0.1 与 ::1（便携 Redis 默认只监听 IPv4 回环，
    而 localhost 可能先解析到 ::1 → 曾经的"服务在跑却探测不通"假阴性）。"""
    import socket as _socket
    candidates = [host] if host and host != "localhost" else ["127.0.0.1", "::1"]
    for candidate in candidates:
        try:
            with _socket.create_connection((candidate, int(port)),
                                           timeout=timeout) as sock:
                sock.sendall(b"PING\r\n")
                if sock.recv(64).startswith(b"+PONG"):
                    return True
        except Exception:
            continue
    return False


def _check_redis_or_exit() -> None:
    """启动前 Redis 预检：不可达则打印明确指引并退出（避免"打印 started 后
    各服务静默崩溃"）。SKIP_REDIS_CHECK=1 可跳过。"""
    if os.environ.get("SKIP_REDIS_CHECK", "0") == "1":
        return
    host = os.environ.get("REDIS_HOST", "localhost")
    try:
        port = int(os.environ.get("REDIS_PORT", "6379") or 6379)
    except Exception:
        port = 6379
    if _redis_reachable(host, port):
        return
    logging.getLogger(__name__).error("Redis unreachable at %s:%s", host, port)
    print(REDIS_SETUP_HINT.format(host=host, port=port))
    sys.exit(1)


def _wait_redis_ready(timeout: float | None = None) -> bool:
    """等 Redis 真正能处理命令后再拉起服务（最多等 WM_REDIS_READY_WAIT 秒，默认 20）。

    背景（实测）：`_ensure_redis_available()` 只保证"已启动/在运行"，而便携 Redis 从
    "端口在听"到"能响应命令"还有一段时间；此前固定 `time.sleep(2)` 不够时，编排器的
    MessagingClient 会在启动期连不上 Redis 而进入重试风暴、静默挂住——表现为
    "启动校验 15/16、orchestrator 未存活"且它的日志停在 AgentRegistry 之后没有任何进展。
    """
    host = os.environ.get("REDIS_HOST", "localhost")
    try:
        port = int(os.environ.get("REDIS_PORT", "6379") or 6379)
    except Exception:
        port = 6379
    try:
        budget = float(timeout if timeout is not None
                       else (os.environ.get("WM_REDIS_READY_WAIT", "20") or 20))
    except Exception:
        budget = 20.0
    log = logging.getLogger(__name__)
    started = time.time()
    deadline = started + max(0.0, budget)
    while True:
        if _redis_reachable(host, port):
            waited = time.time() - started
            if waited > 0.5:
                log.info("Redis %s:%d 已就绪（等待 %.1fs）", host, port, waited)
            return True
        if time.time() >= deadline:
            log.warning("Redis %s:%d 在 %.0fs 内仍未就绪，继续启动（服务会自行重试）",
                        host, port, budget)
            return False
        time.sleep(0.5)


def _ensure_redis_available() -> None:
    """启动前的 Redis 再确认：停掉上一轮服务后，若 Redis 已不可达则自动补齐。

    预检（`_check_redis_or_exit`）发生在停服之前，而 stop 会收尾本项目启动的便携
    Redis；此处在真正拉起服务前复核一次，避免"服务全起在无 Redis 的环境里"。"""
    host = os.environ.get("REDIS_HOST", "localhost")
    try:
        port = int(os.environ.get("REDIS_PORT", "6379") or 6379)
    except Exception:
        port = 6379
    if _redis_reachable(host, port):
        return
    try:
        from dep_check import ensure_redis
        result = ensure_redis(auto=True)
        logging.getLogger(__name__).info("Redis 复核：%s", result.get("detail", ""))
        if not result.get("ok"):
            print(result.get("detail", ""))
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        logging.getLogger(__name__).warning("Redis 复核失败：%s", str(exc)[:150])
        _check_redis_or_exit()


def _read_pids() -> dict:
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_pids(pids: dict) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    pids["started_at"] = datetime.now(timezone.utc).isoformat()
    with open(PID_FILE, "w", encoding="utf-8") as f:
        json.dump(pids, f, ensure_ascii=False, indent=2)


def _kill_pid(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            r = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
        # taskkill 无权限/不可用时，兜底直接终止（同用户进程通常可成功）
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except OSError:
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _is_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                text=True,
                timeout=10,
            )
            return f'"{pid}"' in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_residual_command(cmd: str, pid: int) -> bool:
    """判断命令行是否为本项目（BASE_DIR）内的织光服务进程。"""
    if not cmd or pid == os.getpid():
        return False
    cmd_norm = cmd.replace("\\", "/").strip()
    base_norm = str(BASE_DIR).replace("\\", "/")
    if os.name == "nt":
        cmd_norm = cmd_norm.lower()
        base_norm = base_norm.lower()
    # 必须命中 BASE_DIR 路径（后跟分隔符），避免误杀其他 Python 项目
    if f"{base_norm}/" not in cmd_norm:
        return False
    # 已知脚本名（orchestrator/webui/critic/guardian/metrics/worker_base 等）
    if any(name in cmd_norm for name in _RESIDUAL_SCRIPT_NAMES):
        return True
    # workers 目录下的任意 worker 脚本（workers/*.py）
    return f"{base_norm}/workers/" in cmd_norm


def _scan_residual_processes() -> list[tuple[int, str]]:
    """扫描未登记但仍在运行的织光服务进程（排除当前 launcher 自身）。

    优先使用 psutil（若环境已安装）；不可用时：
      Windows -> wmic 解析 python.exe 的 CommandLine/ProcessId
      其他平台 -> pgrep -af python 后按命令行过滤
    """
    try:
        import psutil
    except Exception:
        psutil = None

    if psutil is not None:
        found: list[tuple[int, str]] = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmd = " ".join(proc.info["cmdline"] or [])
            except Exception:
                continue
            if _is_residual_command(cmd, proc.info["pid"]):
                found.append((proc.info["pid"], cmd))
        return found

    if os.name == "nt":
        # wmic 在 Win11 24H2 起已移除 → 先试 tasklist 全量匹配，再退回 wmic
        found = _scan_windows_tasklist()
        return found if found else _scan_windows_wmic()
    return _scan_posix_pgrep()


def _scan_windows_tasklist() -> list[tuple[int, str]]:
    """Windows 兜底（无 psutil）：tasklist 取 python 进程，再用命令行复核。

    tasklist 默认不输出命令行，需 /V；部分系统对非管理员隐藏命令行，
    此时命令行匹配会失败（返回空），由 wmic 分支兜底。"""
    found: list[tuple[int, str]] = []
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH", "/V"],
            text=True, timeout=15, errors="replace",
        )
    except Exception:
        return found
    import csv as _csv
    import io as _io
    try:
        for row in _csv.reader(_io.StringIO(out)):
            if len(row) < 2:
                continue
            try:
                pid = int(row[1])
            except Exception:
                continue
            cmd = " ".join(row[1:])  # /V 末尾列含命令行（可用时有意义）
            if _is_residual_command(cmd, pid):
                found.append((pid, cmd))
    except Exception:
        return found
    return found


def _scan_windows_wmic() -> list[tuple[int, str]]:
    """Windows 兜底：wmic 获取 python.exe 的 ProcessId 与 CommandLine。"""
    found: list[tuple[int, str]] = []
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/VALUE"],
            text=True,
            timeout=15,
            errors="replace",
        )
    except Exception:
        return found

    current: dict[str, str] = {}
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            if current:
                _append_residual(current, found)
                current = {}
            continue
        if line.startswith("CommandLine="):
            current["cmd"] = line[len("CommandLine="):]
        elif line.startswith("ProcessId="):
            current["pid"] = line[len("ProcessId="):]
    if current:
        _append_residual(current, found)
    return found


def _append_residual(current: dict[str, str], found: list[tuple[int, str]]) -> None:
    """从 wmic 单条进程记录中解析 PID/命令行，并过滤出织光残留进程。"""
    cmd = current.get("cmd", "")
    try:
        pid = int((current.get("pid") or "").strip())
    except ValueError:
        return
    if _is_residual_command(cmd, pid):
        found.append((pid, cmd))


def _scan_posix_pgrep() -> list[tuple[int, str]]:
    """非 Windows 兜底：pgrep -af python 后按命令行过滤织光服务进程。"""
    found: list[tuple[int, str]] = []
    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "python"],
            text=True,
            timeout=15,
            errors="replace",
        )
    except Exception:
        return found
    for raw in out.splitlines():
        parts = raw.split(" ", 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0].strip())
        except ValueError:
            continue
        cmd = parts[1].strip()
        if _is_residual_command(cmd, pid):
            found.append((pid, cmd))
    return found


def _stop_portable_redis() -> dict:
    """停止"本项目启动的"便携版 Redis（按 .weavemind/redis.pid）。

    仅当 pid 文件存在且指向我们的便携实例时动作，绝不误杀用户自建 Redis；
    返回 {stopped: bool, detail: str}，异常一律吞掉（停止流程不因此失败）。"""
    try:
        import dep_check
        ok = dep_check._stop_recorded_redis()
        if ok:
            return {"stopped": True, "detail": "便携版 Redis 已停止"}
        return {"stopped": False, "detail": ""}
    except Exception as exc:
        logger.warning("停止便携 Redis 时出错（已忽略）：%s", str(exc)[:120])
        return {"stopped": False, "detail": ""}


def _verified_stop_report(stopped: list[str], known_pids: set[int]) -> list[tuple[int, str]]:
    """停止后复检：把仍存活的 PID 找出来（供上层如实报告，而非静默）。"""
    survivors: list[tuple[int, str]] = []
    for name, pid in _read_pids().get("services", {}).items():
        try:
            if pid and _is_alive(pid):
                survivors.append((pid, name))
        except Exception:
            continue
    # pids.json 已删时仍可能残留：用进程扫描兜底
    if not survivors:
        try:
            survivors = [(p, c) for p, c in _scan_residual_processes()
                         if p not in known_pids and _is_alive(p)]
        except Exception:
            pass
    return survivors


def stop_services(stop_portable_redis: bool = True) -> list[str]:
    """按 PID 文件停止全部服务，返回已停止的服务名列表。

    `stop_portable_redis=False` 保留本项目启动的便携 Redis：start/restart 流程
    在启动前会先停上一轮服务，但那一步不该把马上要用的 Redis 一起停掉
    （否则服务全部起在没有 Redis 的环境里，表现为启动"成功"却全线连不上）。
    停止后做存活复检：仍有残留时如实打印（含 PID），不再静默。"""
    pids = _read_pids()
    services = pids.get("services", {})
    known_pids = set(p for p in services.values() if p)
    stopped: list[str] = []
    for name, pid in services.items():
        if _kill_pid(pid):
            stopped.append(name)

    # 兜底清理：pids.json 可能被后续启动覆盖或缺失，
    # 扫描并清理仍在运行的织光服务进程
    # （排除当前 launcher 自身，以及刚按 pids.json 处理过、可能尚未从进程表消失的 PID）
    residuals = [
        item for item in _scan_residual_processes()
        if item[0] not in known_pids
    ]
    cleaned = 0
    for pid, _cmd in residuals:
        if _kill_pid(pid):
            cleaned += 1
    if residuals:
        logger.info(
            "清理 %d 个未登记残留进程: %s",
            cleaned,
            ", ".join(str(pid) for pid, _ in residuals),
        )
    else:
        logger.info("清理 0 个未登记残留进程")

    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception:
            pass

    # 便携版 Redis（本项目自动获取并启动的实例）：stop 时应一并收尾，
    # 否则 stop 后 6379 仍被占用、下次启动会误判"已有 Redis 在跑"。
    # start/restart 的"停上一轮"调用会传 False 保留它。
    if stop_portable_redis:
        redis_stop = _stop_portable_redis()
        if redis_stop.get("stopped"):
            logger.info("%s", redis_stop["detail"])

    # 停止后复检：如实报告仍在运行的残留（含 PID），不再静默
    survivors = _verified_stop_report(stopped, known_pids)
    if survivors:
        print(import_cli_text().msg(
            f"  ⚠ 仍有 {len(survivors)} 个进程未停止："
            + ", ".join(f"{name}(pid={pid})" for pid, name in survivors[:8]),
            f"  WARNING: {len(survivors)} process(es) still running: "
            + ", ".join(f"{name}(pid={pid})" for pid, name in survivors[:8]),
        ))
    return stopped


def import_cli_text():
    """延迟导入 cli_text（保持 launcher 顶层仅依赖 stdlib + logging_setup）。"""
    try:
        import cli_text
        return cli_text
    except Exception:
        class _Fallback:
            @staticmethod
            def msg(zh, en):
                return zh
            @staticmethod
            def setup_console_encoding(*_a, **_k):
                return None
        return _Fallback()


def build_services(cfg: dict) -> list[tuple[str, list[str], Path | None, Path | None]]:
    """返回 (名称, argv, cwd, 日志文件) 列表。"""
    py = sys.executable
    services = [
        ("worker-search", [py, str(BASE_DIR / "worker_base.py")], BASE_DIR, None),
        ("worker-web-fetch", [py, str(BASE_DIR / "workers" / "web_fetch_worker.py")], BASE_DIR, None),
        ("worker-content-summary", [py, str(BASE_DIR / "workers" / "content_summary_worker.py")], BASE_DIR, None),
        ("worker-code-execution", [py, str(BASE_DIR / "workers" / "code_execution_worker.py")], BASE_DIR, None),
        ("worker-file-io", [py, str(BASE_DIR / "workers" / "file_io_worker.py")], BASE_DIR, None),
        ("worker-packaging", [py, str(BASE_DIR / "workers" / "packaging_worker.py")], BASE_DIR, None),
        ("worker-data-loader", [py, str(BASE_DIR / "workers" / "data_loader_worker.py")], BASE_DIR, None),
        ("worker-data-analyzer", [py, str(BASE_DIR / "workers" / "data_analyzer_worker.py")], BASE_DIR, None),
        ("worker-model-trainer", [py, str(BASE_DIR / "workers" / "model_trainer_worker.py")], BASE_DIR, None),
        ("worker-report-generator", [py, str(BASE_DIR / "workers" / "report_generator_worker.py")], BASE_DIR, None),
        ("worker-react-agent", [py, str(BASE_DIR / "workers" / "react_agent.py")], BASE_DIR, None),
        ("critic", [py, str(BASE_DIR / "critic_agent.py")], BASE_DIR, None),
        ("orchestrator", [py, str(BASE_DIR / "orchestrator_v2.py")], BASE_DIR, None),
        ("webui", [py, str(BASE_DIR / "web_ui.py")], BASE_DIR, None),
        ("guardian", [py, str(BASE_DIR / "worker_guardian.py")], BASE_DIR, None),
        ("metrics", [py, str(BASE_DIR / "metrics_collector.py")], BASE_DIR, None),
    ]

    # 每日进化调度器：默认关闭，通过 config.json system.scheduler=true 或环境变量 EVOLUTION_SCHEDULE=1 开启
    scheduler_enabled = (
        cfg.get("system", {}).get("scheduler", False)
        or os.environ.get("EVOLUTION_SCHEDULE", "0") == "1"
    )
    if scheduler_enabled:
        services.append(("scheduler", [py, str(BASE_DIR / "scheduler.py")], BASE_DIR, None))

    # 前端 Vite（日志单独落盘）
    frontend_dir = BASE_DIR / "frontend"
    if not (frontend_dir / "dist" / "index.html").exists():
        npx = "npx.cmd" if os.name == "nt" else "npx"
        vite_log = LOG_DIR / "vite.log"
        services.append(
            ("vite", [npx, "vite", "--host", "0.0.0.0"], frontend_dir, vite_log)
        )
    return services


def _spawn_service(name: str, argv: list[str], cwd: Path | None,
                   out_path: Path | None) -> int | None:
    """启动单个服务并返回 PID；失败返回 None（start 与守护模式共用）。"""
    try:
        if out_path:
            fh = open(out_path, "a", encoding="utf-8")
        else:
            fh = subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd or BASE_DIR),
                env=os.environ.copy(),
                stdout=fh,
                stderr=fh,
                creationflags=_CREATE_NO_WINDOW,
                # POSIX：独立进程组，停止时可整组回收（连带 npx→node 子进程）
                start_new_session=(os.name != "nt"),
            )
            return proc.pid
        finally:
            # 父进程关闭自己的句柄；DEVNULL 是共享对象，不能关闭
            if out_path:
                try:
                    fh.close()
                except Exception:
                    pass
    except Exception as exc:
        logger.error("[%s] failed to start: %s", name, exc)
        return None


def start_services() -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = _load_config()
    _apply_env(cfg)

    logging_setup.setup_logging("launcher")
    logger = logging.getLogger(__name__)

    # 代码执行的隔离强度与"此刻是否真的可用"必须在启动时说明白：默认要求容器隔离，
    # 隔离不可用时代码执行会被拒绝；restricted/none 只能由操作者显式选择。
    try:
        import code_sandbox
        _st = code_sandbox.sandbox_status()
        _ready = bool(_st.get("isolation_ready"))
        logger.info("代码执行沙箱：mode=%s, isolation_ready=%s（%s）",
                    _st.get("mode"), "yes" if _ready else "no",
                    _st.get("isolation_note"))
        if _st.get("isolation_required") and not _ready:
            logger.warning("容器隔离未就绪，涉及代码执行的步骤会被拒绝：%s",
                           _st.get("isolation_reason") or "")
    except Exception as exc:
        logger.warning("沙箱状态检查失败（代码执行会被拒绝）：%s", str(exc)[:120])

    # 重复启动必须**复用**已在本实例上运行的服务：此前无条件 stop_services，
    # 第二次双击会把正在跑的研究任务一起杀掉（N1 首批修复）。
    _force = os.environ.get("WM_FORCE_RESTART", "0") == "1"
    state = instance_state()
    if state["running"] and not _force:
        logger.info("本实例已在运行（%d 个服务），复用而不重启：%s",
                    len(state["services"]), state["url"])
        print(import_cli_text().msg(
            f"  [OK] 已在运行（{len(state['services'])} 个服务）：{state['url']}"
            "——已复用本实例，不重启、不打断进行中的任务",
            f"  [OK] Already running ({len(state['services'])} services): {state['url']}"
            " - reusing this instance (no restart, running tasks untouched)"))
        print_readiness(readiness_report(), quiet=False)
        return {"reused": True, "services": state["services"], "port": state["port"],
                "url": state["url"]}

    logger.info("Stopping previous services (if any)...")
    stopped = stop_services(stop_portable_redis=False)
    if stopped:
        logger.info("Stopped: %s", ", ".join(stopped))
    _ensure_redis_available()
    _wait_redis_ready()

    services = build_services(cfg)
    pids: dict = {"services": {}, "failed": []}
    for name, argv, cwd, out_path in services:
        pid = _spawn_service(name, argv, cwd, out_path)
        if pid:
            pids["services"][name] = pid
            logger.info("[%s] started pid=%s", name, pid)
        else:
            # spawn 失败必须进清单：此前只打日志、不记账，导致"16/16 存活"
            # 实际只统计了启动成功的那些（少一个反而看起来全绿）。
            pids["failed"].append(name)
            logger.error("[%s] failed to start", name)

    _write_pids(pids)
    # 消息里的端口必须反映实际监听端口：此前写死 8080，用户用 WEB_PORT 改了端口
    # 仍被提示 8080（WEB_PORT 本身是被尊重的），实测新环境因此走错端口。
    front_url = web_url()
    # 启动后校验：给子进程一点时间完成 import/连接，然后核对实际存活。
    # 秒退服务在此暴露（此前只打印 started，用户看到"成功"却无服务）。
    summary = verify_services(quiet=False)
    if summary["down"]:
        logger.error(
            "启动校验：%d/%d 存活，未存活：%s（日志见 logs/）",
            summary["alive"], summary["total"],
            ", ".join(name for _, name in summary["down"]),
        )
        if os.environ.get("WM_START_STRICT", "0") == "1":
            print(import_cli_text().msg(
                "  启动校验未通过（WM_START_STRICT=1）：请查看 logs/ 后重试。",
                "  Startup verification failed (WM_START_STRICT=1): see logs/ and retry.",
            ))
            sys.exit(1)
    # 三层就绪：工作台可访问 / 研究能力就绪 / 代码隔离可用——进程数不能代表"可研究"。
    print_readiness(readiness_report(), quiet=False)
    logger.info("All services started. WebUI: %s  Frontend: %s", front_url, front_url)
    return pids


# 研究必需能力（对应 AgentRegistry.capabilities）——缺任一项即"研究能力未就绪"。
# 依据一次公司研究的实际链路：检索 → 抓取 → 摘要 → 出报告 → 打包。
# `data_analyzer` / `file_io` / `code_execution` 不在必需项里：图表由进程内
# charts_pipeline 渲染，代码执行是可选能力（本机没有容器隔离时会被拒绝）。
RESEARCH_REQUIRED_CAPABILITIES = ("web_search", "web_fetch", "content_summary",
                                  "report_generator", "package")
HEARTBEAT_MAX_AGE_SEC = 180.0


def _redis_min_major() -> int:
    """Redis 兼容下限（与 `dep_check.REDIS_MIN_MAJOR` 同源，避免两处漂移）。"""
    try:
        import dep_check
        return int(dep_check.REDIS_MIN_MAJOR)
    except Exception:
        return 6


def _registry_heartbeats() -> dict[str, float]:
    """能力 → 最近心跳年龄（秒）。读不到注册表时返回空 dict（按未就绪处理）。"""
    import sqlite3
    from datetime import datetime, timezone
    db = os.environ.get("WEAVEMIND_DB") or str(BASE_DIR / "agents.db")
    out: dict[str, float] = {}
    now = datetime.now(timezone.utc)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT capabilities, last_heartbeat FROM agents").fetchall()
        con.close()
    except Exception:
        return {}
    for r in rows:
        caps = [c.strip().split(":")[0] for c in str(r["capabilities"] or "").split(",")]
        hb = str(r["last_heartbeat"] or "").strip()
        try:
            ts = datetime.strptime(hb, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            age = max(0.0, (now - ts).total_seconds())
        except Exception:
            age = float("inf")
        for cap in caps:
            if cap:
                out[cap] = min(out.get(cap, float("inf")), age)
    return out


def readiness_report(*, http_timeout: float = 3.0) -> dict:
    """三层状态：工作台可访问 / 研究能力就绪 / 代码隔离可用。

    为什么不能只看"N/N 服务存活"：PID 存活、端口响应、进程数都不能代表**能提交研究任务**。
    研究能力要求 Redis 可达且版本兼容（≥6，redis-py 8 用 RESP3）、注册表里研究必需能力的
    心跳新鲜、编排器进程存活。任何一层不成立都不得对外宣称"可研究"。
    """
    port = web_port()
    # ① 工作台：后端在**实际端口**上响应
    workbench = {"ok": False, "detail": "", "port": port, "url": web_url(port)}
    try:
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=http_timeout) as resp:
            code = int(getattr(resp, "status", 0) or 0)
            workbench["ok"] = 200 <= code < 300
            workbench["detail"] = f"HTTP {code} @ {port}"
    except Exception as exc:
        workbench["detail"] = f"未响应（{str(exc)[:80]}）@ {port}"

    # ② 研究能力
    host = os.environ.get("REDIS_HOST", "localhost")
    try:
        rport = int(os.environ.get("REDIS_PORT", "6379") or 6379)
    except Exception:
        rport = 6379
    redis_ok = _redis_reachable(host, rport)
    major = None
    if redis_ok:
        try:
            import dep_check
            major = dep_check._redis_server_version(host, rport)
        except Exception:
            major = None
    beats = _registry_heartbeats()
    missing = [c for c in RESEARCH_REQUIRED_CAPABILITIES if c not in beats]
    stale = [c for c in RESEARCH_REQUIRED_CAPABILITIES
             if c in beats and beats[c] > HEARTBEAT_MAX_AGE_SEC]
    orchestrator = "orchestrator" in (instance_state()["services"] or {})
    _min_major = _redis_min_major()
    redis_compatible = bool(redis_ok and (major is None or major >= _min_major))
    research_ok = bool(redis_compatible and not missing and not stale and orchestrator)
    research = {"ok": research_ok, "redis": redis_ok, "redis_major": major,
                "missing": missing, "stale": stale, "orchestrator": orchestrator,
                "required": list(RESEARCH_REQUIRED_CAPABILITIES)}

    # ③ 代码隔离（可选能力：研究类任务不需要它）
    sandbox = {"ok": False, "note": "", "execution_available": False}
    try:
        from code_sandbox import isolation_ready, isolation_required, sandbox_status
        st = sandbox_status()
        sandbox = {"ok": bool(st.get("isolation_ready")),
                   "note": st.get("isolation_note") or "",
                   "execution_available": bool(st.get("execution_available")),
                   "isolation_required": bool(isolation_required()),
                   "reason": st.get("isolation_reason") or ""}
    except Exception as exc:
        sandbox = {"ok": False, "note": f"沙箱状态未知（{str(exc)[:60]}）",
                   "execution_available": False, "isolation_required": True, "reason": ""}
    return {"workbench": workbench, "research": research, "code_sandbox": sandbox,
            "port": port, "url": web_url(port), "ready": bool(workbench["ok"] and research_ok)}


def print_readiness(rep: dict, quiet: bool = False) -> None:
    """打印三层就绪状态。失败不得显示"可研究"（只打印当前步骤与下一步）。"""
    if quiet:
        return
    t = import_cli_text().msg
    wb, rs, sb = rep["workbench"], rep["research"], rep["code_sandbox"]
    print(f"  [{'OK' if wb['ok'] else '!!'}] "
          + t(f"工作台：{wb['detail']}", f"Workbench: {wb['detail']}"))
    if rs["ok"]:
        print("  [OK] " + t(
            f"研究能力：就绪（Redis {rs['redis_major'] or '版本未知'}、"
            f"编排器与 {len(rs['required'])} 项必需 Worker 心跳新鲜）",
            "Research: ready (Redis, orchestrator and required workers heartbeating)"))
    else:
        why = []
        if not rs["redis"]:
            why.append(t("Redis 不可达", "Redis unreachable"))
        elif rs["redis_major"] is not None and rs["redis_major"] < _redis_min_major():
            why.append(t(f"Redis {rs['redis_major']} 版本过低（需 ≥{_redis_min_major()}）",
                         f"Redis {rs['redis_major']} too old"))
        if rs["missing"]:
            why.append(t("缺少能力：" + ", ".join(rs["missing"]),
                         "missing capabilities: " + ", ".join(rs["missing"])))
        if rs["stale"]:
            why.append(t("心跳过期：" + ", ".join(rs["stale"]),
                         "stale heartbeats: " + ", ".join(rs["stale"])))
        if not rs["orchestrator"]:
            why.append(t("编排器未存活", "orchestrator not running"))
        print("  [!!] " + t("研究能力：未就绪——" + "；".join(why),
                            "Research: NOT ready - " + "; ".join(why)))
        print("       " + t("下一步：查看 logs/ 里未就绪服务的日志后重试；"
                            "工作台可打开不等于能提交研究任务。",
                            "Next: check logs/ for the unhealthy service and retry."))
    if sb.get("execution_available") and not sb.get("ok"):
        print("  [--] " + t(f"代码执行：{sb['note']}",
                            f"Code execution: {sb['note']}"))
    elif sb.get("ok"):
        print("  [OK] " + t(f"代码执行：{sb['note']}", f"Code execution: {sb['note']}"))
    else:
        print("  [--] " + t(
            f"代码执行：容器隔离不可用（{sb.get('reason') or '原因未知'}）——"
            "涉及代码执行的步骤会被拒绝；检索/结构化数据/图表/报告/交付不受影响。",
            "Code execution: container isolation unavailable - code steps will be refused."))


def web_port() -> int:
    """Web 端口的**唯一来源**：WEB_PORT 环境变量（config.json 的 `web.port` 作默认值）。

    start.bat、就绪探测、提示消息、打开浏览器都必须用它——此前 start.bat 里硬写
    8080，用户改过端口就会打开错误的页面。
    """
    cfg = _load_config()
    web_cfg = cfg.get("web") if isinstance(cfg.get("web"), dict) else {}
    raw = os.environ.get("WEB_PORT") or (web_cfg or {}).get("port") or 8080
    try:
        port = int(str(raw).strip())
    except Exception:
        port = 8080
    return port if 0 < port < 65536 else 8080


def web_url(port: int | None = None) -> str:
    """工作台地址（后端端口；前端产物缺失时后端会给出回退状态页）。"""
    return f"http://localhost:{port or web_port()}"


def _pid_owns_project(pid: int) -> bool:
    """该 PID 是否确实是本项目的服务进程（避免 PID 复用被误认成"实例在跑"）。"""
    try:
        return any(int(p) == int(pid) for p, _cmd in _scan_residual_processes())
    except Exception:
        return False


def instance_state() -> dict:
    """本实例状态：PID 文件里仍存活**且归属校验通过**的服务。

    重复双击要复用这个实例而不是停掉它；归属校验保证不会把别人的进程当成自己的。
    """
    pids = _read_pids().get("services") or {}
    alive: dict = {}
    stale: dict = {}
    for name, pid in pids.items():
        try:
            ok = bool(_is_alive(pid) and _pid_owns_project(pid))
        except Exception:
            ok = False
        (alive if ok else stale)[name] = pid
    return {"running": bool(alive), "services": alive, "stale": stale,
            "port": web_port(), "url": web_url()}


def verify_services(quiet: bool = True) -> dict:
    """启动后校验：等待若干秒后统计服务实际存活情况。

    返回 {total, alive, down:[(pid, name)], never_started:[name], waited}；
    不等严格模式也会如实打印。**spawn 失败的服务计入 total 与 down**——此前只统计
    启动成功的那些，少启动一个反而显示"15/15 存活"。"""
    try:
        wait = float(os.environ.get("WM_START_VERIFY_WAIT", "8") or 8)
    except Exception:
        wait = 8.0
    wait = max(0.0, min(wait, 60.0))
    if wait:
        time.sleep(wait)
    recorded = _read_pids()
    services = recorded.get("services", {})
    never_started = [str(n) for n in (recorded.get("failed") or [])]
    down: list[tuple[int, str]] = []
    alive = 0
    for name, pid in services.items():
        try:
            if _is_alive(pid):
                alive += 1
            else:
                down.append((pid, name))
        except Exception:
            down.append((pid, name))
    down.extend((0, name) for name in never_started)
    total = len(services) + len(never_started)
    summary = {"total": total, "alive": alive, "down": down,
               "never_started": never_started, "waited": wait}
    if not quiet or down:
        ok = not down
        detail = ", ".join(
            (f"{name}（未启动）" if pid == 0 else name) for pid, name in down[:6])
        line = import_cli_text().msg(
            f"  [{ 'OK' if ok else '!!' }] 启动校验：{alive}/{total} 服务存活"
            + ("" if ok else "；未存活：" + detail),
            f"  [{ 'OK' if ok else '!!' }] Startup check: {alive}/{total} alive"
            + ("" if ok else "; down: " + detail),
        )
        print(line)
    return summary


def _supervise_interval() -> float:
    """守护巡检间隔：环境变量 SUPERVISE_INTERVAL 优先，
    其次 config.json system.supervise_interval，缺省 30 秒。"""
    raw = os.environ.get("SUPERVISE_INTERVAL", "")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    try:
        v = float(_load_config().get("system", {}).get("supervise_interval", 0) or 0)
        if v > 0:
            return v
    except Exception:
        pass
    return SUPERVISE_INTERVAL_DEFAULT


def _supervise_once(
    pids: dict,
    services: list[tuple[str, list[str], Path | None, Path | None]],
    state: dict,
) -> tuple[dict, bool]:
    """巡检一轮：进程不存在则重启并更新 pids；连续失败 3 次进入 5 分钟隔离。

    只监督 build_services 列出的服务（与 start/stop 一致）。
    返回 (更新后的 pids, 是否发生变更)；state 记录连续失败次数与隔离截止时间。
    """
    state.setdefault("restart_counts", {})
    state.setdefault("quarantine_until", {})
    state.setdefault("quarantine_skip_logged", set())
    now = time.time()
    changed = False
    for name, argv, cwd, out_path in services:
        q_until = state["quarantine_until"].get(name, 0.0)
        if now < q_until:
            # 隔离期内跳过重启（只在首次进入隔离期时打日志，避免每轮刷屏）
            if name not in state["quarantine_skip_logged"]:
                logger.warning(
                    "supervise: 服务 %s 处于隔离期，跳过重启（剩余 %.0f 秒）",
                    name, q_until - now,
                )
                state["quarantine_skip_logged"].add(name)
            continue
        if q_until:
            # 隔离期结束：清空计数并恢复监督
            logger.info("supervise: 服务 %s 隔离期结束，恢复监督", name)
            state["quarantine_until"].pop(name, None)
            state["quarantine_skip_logged"].discard(name)
            state["restart_counts"].pop(name, None)

        pid = pids.get("services", {}).get(name)
        if pid and _is_alive(pid):
            # 存活一轮即视为已恢复，清零连续失败计数
            if state["restart_counts"].get(name):
                state["restart_counts"][name] = 0
            continue
        # 进程不存在：按连续失败次数决定重启或隔离
        fail_count = state["restart_counts"].get(name, 0)
        if fail_count >= SUPERVISE_MAX_RESTARTS:
            state["quarantine_until"][name] = now + SUPERVISE_QUARANTINE_SECONDS
            state["restart_counts"][name] = 0
            state["quarantine_skip_logged"].discard(name)
            logger.warning(
                "supervise: 服务 %s 连续重启 %d 次仍失败，隔离 %d 秒防崩溃循环",
                name, SUPERVISE_MAX_RESTARTS, SUPERVISE_QUARANTINE_SECONDS,
            )
            continue

        new_pid = _spawn_service(name, argv, cwd, out_path)
        state["restart_counts"][name] = fail_count + 1
        if new_pid:
            pids.setdefault("services", {})[name] = new_pid
            # 重启成功即从"未启动"清单里移除，否则 verify_services 会一直把它算作失败
            try:
                pids["failed"] = [n for n in (pids.get("failed") or []) if n != name]
            except Exception:
                pass
            changed = True
            logger.info(
                "supervise: 重启服务 %s pid=%s（连续第 %d 次）",
                name, new_pid, fail_count + 1,
            )
        else:
            logger.error(
                "supervise: 重启服务 %s 失败（连续第 %d 次）",
                name, fail_count + 1,
            )
    return pids, changed


def supervise_services() -> None:
    """守护模式：启动全部服务后循环巡检，崩溃自动重启，连续失败隔离防崩溃循环。"""
    logging_setup.setup_logging("launcher")
    logger = logging.getLogger(__name__)
    logger.info("supervise: 守护模式启动（WEAVEMIND_SUPERVISE=1）")
    start_services()
    cfg = _load_config()
    services = build_services(cfg)
    interval = _supervise_interval()
    state: dict = {}
    logger.info(
        "supervise: 巡检间隔 %s 秒；连续失败 %d 次后隔离 %d 秒",
        interval, SUPERVISE_MAX_RESTARTS, SUPERVISE_QUARANTINE_SECONDS,
    )
    try:
        while True:
            time.sleep(interval)
            pids = _read_pids()
            if not pids.get("services"):
                # pids.json 无服务记录（可能已被 stop 清理），守护退出避免与 stop 冲突
                logger.warning("supervise: pids.json 无服务记录（可能已 stop），守护退出")
                return
            pids, changed = _supervise_once(pids, services, state)
            if changed:
                _write_pids(pids)
    except KeyboardInterrupt:
        logger.info("supervise: 收到中断，退出守护模式（已启动的服务保持运行）")


def _redis_source() -> str:
    """Redis 来源摘要：便携（本项目下载）/ Docker 容器 / 系统服务 / 不可达。"""
    try:
        import dep_check
        if not dep_check.redis_ping():
            return "unreachable"
        exe = dep_check._usable_portable_redis()
        if exe is not None:
            major = dep_check._redis_binary_version(exe)
            return f"portable (v{major or '?'}, {exe.parent.name})"
        ver = dep_check._redis_server_version()
        return f"external (v{ver or '?'})"
    except Exception:
        return "unknown"


def _pids_file_state() -> str:
    """pids.json 状态：ok / missing（从未启动） / corrupt（损坏，勿静默）。"""
    if not PID_FILE.exists():
        return "missing"
    try:
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
        return "ok" if isinstance(data, dict) else "corrupt"
    except Exception:
        return "corrupt"


def print_status() -> None:
    state = _pids_file_state()
    if state != "ok":
        msg = (
            f"No services recorded ({PID_FILE.name} {state}; run `python launcher.py` to start)."
            if state == "missing"
            else f"WARNING: {PID_FILE.name} is corrupt (cannot be parsed); "
                 "run `python launcher.py stop` then start again."
        )
        print(msg)
        print(f"Redis: {_redis_source()}")
        return
    pids = _read_pids()
    services = pids.get("services", {})
    if not services:
        print("No services recorded (run `python launcher.py` to start).")
        print(f"Redis: {_redis_source()}")
        return
    print(f"Started at: {pids.get('started_at', 'unknown')}")
    alive = 0
    down: list[str] = []
    for name, pid in services.items():
        ok = _is_alive(pid)
        alive += 1 if ok else 0
        if not ok:
            down.append(name)
        print(f"  [{'UP' if ok else 'DOWN'}] {name} (pid={pid})")
    print(f"{alive}/{len(services)} services alive")
    if down:
        print(import_cli_text().msg(
            "  未存活服务的日志在 logs/ 下（worker-*.log / *.log）；"
            "可 `python launcher.py restart` 重启。",
            "  Logs for down services are under logs/ (worker-*.log); "
            "run `python launcher.py restart`.",
        ))
    print(f"Redis: {_redis_source()}")
    # 进程数不等于"可研究"：状态里同时给三层就绪与**实际端口**（N1）
    print(f"URL: {web_url()}")
    print_readiness(readiness_report(), quiet=False)


# ── N1：统一启动控制器（状态机 + 单实例锁 + 断点恢复 + 脱敏诊断） ─────────────
# 目标：新人只点一个入口，且任何失败只给**一个可执行的下一步**。
# 状态取值（对外只显示当前步骤与下一步；技术详情可展开）：
#   checking_runtime → preparing_deps → awaiting_config → starting_services
#   → waiting_ready → research_ready ／ limited_experience（能看不能研究）／ failed
STARTUP_STATES = ("checking_runtime", "preparing_deps", "awaiting_config",
                  "starting_services", "waiting_ready", "research_ready",
                  "limited_experience", "failed")
STARTUP_STATE_FILE = PID_DIR / "startup_state.json"
INSTANCE_LOCK_FILE = PID_DIR / "instance.lock"


def runtime_identity() -> str:
    """运行包身份：便携包清单版本 → VERSION 文件 → git 短哈希 → 源码运行标记。

    用于"包版本变化可以触发迁移/修复，但不能覆盖用户配置或历史"：
    身份变化即让已完成步骤的缓存失效（重新检查），身份不变则复用已验证结论。
    """
    for name in ("package_manifest.json", "run_package_manifest.json"):
        p = BASE_DIR / name
        try:
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                ver = str(data.get("version") or data.get("package_version") or "").strip()
                if ver:
                    return f"package:{ver}"
        except Exception:
            pass
    try:
        vf = BASE_DIR / "VERSION"
        if vf.exists():
            ver = vf.read_text(encoding="utf-8").strip()
            if ver:
                return f"version:{ver}"
    except Exception:
        pass
    try:
        import subprocess as _sp
        out = _sp.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(BASE_DIR),
                      capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return f"git:{out.stdout.strip()}"
    except Exception:
        pass
    return "source:unknown"


def effective_config() -> dict:
    """在依赖/Redis 检查**之前**形成统一有效值（端口 / Redis 目标 / 数据库 / 配置状态）。

    此前 config.json 只在 `start_services()` 里映射进环境变量，而脚本层的 Redis 探测
    已经按默认 6379 跑过——改了 redis.port 的用户会被探测"本机没有 Redis"。
    """
    cfg = _load_config()
    _apply_env(cfg)                      # config.json → 环境变量（不覆盖已设）
    llm = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    api_key = str(llm.get("api_key") or os.environ.get("LLM_API_KEY") or "").strip()
    base_url = str(llm.get("base_url") or os.environ.get("LLM_BASE_URL") or "").strip()
    model = str(llm.get("model") or os.environ.get("LLM_MODEL") or "").strip()
    host = os.environ.get("REDIS_HOST", "localhost")
    try:
        rport = int(os.environ.get("REDIS_PORT", "6379") or 6379)
    except Exception:
        rport = 6379
    return {
        "port": web_port(),
        "url": web_url(),
        "redis_host": host,
        "redis_port": rport,
        "db": os.environ.get("WEAVEMIND_DB") or str(BASE_DIR / "agents.db"),
        "config_complete": bool(api_key and base_url and model),
        "model": model,
        "base_url": base_url,
        "frontend_dist": (BASE_DIR / "frontend" / "dist" / "index.html").exists(),
        "identity": runtime_identity(),
        "python": sys.version.split()[0],
    }


def _read_startup_state() -> dict:
    try:
        return json.loads(STARTUP_STATE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _write_startup_state(state: dict) -> None:
    try:
        STARTUP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state = dict(state)
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        STARTUP_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        logging.getLogger(__name__).warning("启动状态写入失败：%s", str(exc)[:120])


def completed_steps(identity: str) -> dict:
    """身份未变时返回已验证完成的步骤（断点恢复；身份变化即整体失效）。"""
    st = _read_startup_state()
    if str(st.get("identity") or "") != str(identity or ""):
        return {}
    return dict(st.get("steps") or {})


def mark_step(identity: str, name: str, ok: bool, detail: str = "") -> None:
    st = _read_startup_state()
    if str(st.get("identity") or "") != str(identity or ""):
        st = {"identity": identity, "steps": {}}
    st.setdefault("steps", {})[name] = {
        "ok": bool(ok), "detail": str(detail)[:300],
        "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    _write_startup_state(st)


def acquire_instance_lock() -> dict:
    """单实例锁：第二次双击（或启动中再次双击）复用同一实例、显示同一进度。

    原子创建 + 持有者存活校验；持有者已死或锁过期则接管（避免崩溃后永久锁死）。
    """
    me = os.getpid()
    for attempt in (1, 2):
        try:
            fd = os.open(str(INSTANCE_LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": me, "state": "starting",
                           "at": time.time()}, f)
            return {"acquired": True, "pid": me, "state": "starting"}
        except FileExistsError:
            try:
                info = json.loads(INSTANCE_LOCK_FILE.read_text(encoding="utf-8")) or {}
            except Exception:
                info = {}
            holder = int(info.get("pid") or 0)
            age = time.time() - float(info.get("at") or 0)
            alive = False
            try:
                alive = bool(holder) and _is_alive(holder)
            except Exception:
                alive = False
            if alive and age < 6 * 3600:
                return {"acquired": False, "pid": holder,
                        "state": str(info.get("state") or "running"), "age": age}
            try:
                INSTANCE_LOCK_FILE.unlink()
            except Exception:
                pass
            if attempt == 2:
                return {"acquired": False, "pid": holder, "state": "stale"}
        except Exception as exc:
            return {"acquired": True, "pid": me, "state": "unlocked",
                    "warning": str(exc)[:120]}
    return {"acquired": False, "pid": 0, "state": "unknown"}


def release_instance_lock(state: str = "running") -> None:
    """释放（或改状态）单实例锁：只处理本进程持有的锁。"""
    try:
        info = json.loads(INSTANCE_LOCK_FILE.read_text(encoding="utf-8")) or {}
        if int(info.get("pid") or 0) != os.getpid():
            return
        if state == "running":
            INSTANCE_LOCK_FILE.unlink()
        else:
            info["state"] = state
            INSTANCE_LOCK_FILE.write_text(json.dumps(info, ensure_ascii=False),
                                          encoding="utf-8")
    except Exception:
        pass


_SECRET_PATTERNS = (
    # Authorization 头：整行剩余部分一律替换（不能只吃掉 "Bearer" 一个词）
    (re.compile(r"(?i)authorization\s*[:=]\s*\S.*$", re.M), "Authorization: <redacted>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer <redacted>"),
    # key/token/secret 赋值
    (re.compile(r"(?i)(api[_-]?key|token|secret)\"?\s*[:=]\s*\"?[^\"\s,}]+"),
     r"\1: <redacted>"),
    (re.compile(r"sk-[A-Za-z0-9]{8,}"), "<redacted>"),
)


def redact_secrets(text: str) -> str:
    """脱敏：密钥、Authorization、Bearer token 一律替换（诊断导出前必过）。"""
    out = str(text or "")
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def diagnostics_report(tail_lines: int = 40) -> str:
    """脱敏诊断（版本 / 步骤与错误类别 / 日志尾部）：不含密钥、研究正文与私人文件。

    只读本仓库 logs/ 下的日志尾部，并按正则脱敏；不自动上传任何内容。
    """
    cfg = effective_config()
    state = _read_startup_state()
    rep = readiness_report()
    lines = ["织光诊断（本地生成，未上传）", "=" * 40,
             f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"运行身份：{cfg['identity']}　Python：{cfg['python']}",
             f"工作台：{rep['url']}（{'可访问' if rep['workbench']['ok'] else '未响应'}）",
             f"研究能力：{'就绪' if rep['research']['ok'] else '未就绪'}"
             + (f"（缺 {', '.join(rep['research']['missing'])}）"
                if rep['research']['missing'] else ""),
             f"代码隔离：{'就绪' if rep['code_sandbox'].get('ok') else '不可用'}"
             f"（{rep['code_sandbox'].get('reason') or rep['code_sandbox'].get('note') or ''}）",
             f"配置：{'完整' if cfg['config_complete'] else '不完整（缺模型/密钥）'}"
             f"　前端产物：{'有' if cfg['frontend_dist'] else '缺'}",
             f"Redis：{cfg['redis_host']}:{cfg['redis_port']}",
             f"启动步骤：{json.dumps(state.get('steps') or {}, ensure_ascii=False)[:400]}",
             "", "日志尾部（已脱敏）："]
    for fn in sorted(os.listdir(LOG_DIR)) if LOG_DIR.exists() else []:
        if not fn.endswith(".log"):
            continue
        p = LOG_DIR / fn
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-tail_lines:]
        except Exception:
            continue
        lines.append(f"--- logs/{fn} ---")
        for ln in tail:
            ln = ln.rstrip()
            if re.search(r"(?i)(api_key|authorization|bearer|sk-)", ln):
                ln = redact_secrets(ln)
            lines.append(ln)
    return "\n".join(lines)


def startup_controller(*, allow_config_wizard: bool = True) -> dict:
    """统一启动控制器：检查运行包 → 准备依赖 → 等待首次配置 → 启动服务 → 等待就绪。

    返回 {state, steps, url, detail}；state ∈ STARTUP_STATES。
    任何失败只给**一个**可执行的下一步（不反复自动重试同类动作）。
    """
    t = import_cli_text().msg
    steps: list[tuple[str, bool, str]] = []

    def step(label: str, ok: bool, detail: str = "") -> None:
        steps.append((label, ok, detail))
        print(f"  [{'OK' if ok else '!!'}] {label}：{detail}" if detail
              else f"  [{'OK' if ok else '!!'}] {label}")

    lock = acquire_instance_lock()
    if not lock.get("acquired"):
        detail = t(f"本实例已在运行（pid={lock.get('pid')}，状态 {lock.get('state')}）；"
                   f"复用同一实例，不重复启动",
                   f"instance already running (pid={lock.get('pid')})")
        step(t("实例", "Instance"), True, detail)
        print_readiness(readiness_report(), quiet=False)
        return {"state": "research_ready" if readiness_report()["ready"]
                else "limited_experience", "steps": steps,
                "url": web_url(), "detail": detail}

    try:
        cfg = effective_config()
        step(t("运行包", "Runtime"),
             True, f"{cfg['identity']}　Python {cfg['python']}")

        done = completed_steps(cfg["identity"])
        # ② 依赖（身份未变且上轮已验证 → 复用结论，不重复联网）
        if done.get("deps", {}).get("ok") and os.environ.get("WM_SKIP_VERIFIED_DEPS") != "0":
            step(t("依赖", "Dependencies"), True,
                 t("上轮已验证就绪（未重复联网安装）", "verified in a previous run (no reinstall)"))
        else:
            _run_dependency_check(fix=True, fatal=False)
            mark_step(cfg["identity"], "deps", True, "checked")
            step(t("依赖", "Dependencies"), True, t("已检查并补齐缺失项", "checked / missing installed"))

        # ③ 配置（不完整就交给引导；不消耗模型额度做探针）
        if not cfg["config_complete"]:
            step(t("配置", "Configuration"), False,
                 t("模型/密钥不完整：先完成引导再启动", "model/key incomplete: run the wizard first"))
            if allow_config_wizard and os.environ.get("WM_NONINTERACTIVE") != "1":
                try:
                    import subprocess as _sp
                    _sp.run([sys.executable, str(BASE_DIR / "setup_wizard.py")],
                            cwd=str(BASE_DIR))
                    cfg = effective_config()
                except Exception as exc:
                    logging.getLogger(__name__).warning("引导启动失败：%s", str(exc)[:120])
            if not cfg["config_complete"]:
                mark_step(cfg["identity"], "config", False, "incomplete")
                release_instance_lock("failed")
                return {"state": "awaiting_config", "steps": steps, "url": cfg["url"],
                        "detail": t("完成模型配置后再次运行本入口（或打开页面引导）",
                                    "finish model configuration and run this entry again")}
        step(t("配置", "Configuration"), True,
             f"{cfg['model']} @ {cfg['base_url']}")

        # ④ 启动服务（内部会复用已运行实例）
        mark_step(cfg["identity"], "config", True, cfg["model"])
        step(t("服务", "Services"), True, t("启动/复用中（详见下方逐项校验）",
                                            "starting/reusing (see checks below)"))
        start_services()

        # ⑤ 就绪
        rep = readiness_report()
        print_readiness(rep, quiet=False)
        if rep["ready"]:
            step(t("可研究", "Research ready"), True,
                 t("工作台可访问、研究能力就绪", "workbench up and research capabilities ready"))
            release_instance_lock("running")
            return {"state": "research_ready", "steps": steps, "url": rep["url"],
                    "detail": t("打开工作台开始研究", "open the workbench and start")}
        if rep["workbench"]["ok"]:
            step(t("受限体验", "Limited"), False,
                 t("工作台可访问但研究能力未就绪（见上）", "workbench up, research not ready"))
            release_instance_lock("limited")
            return {"state": "limited_experience", "steps": steps, "url": rep["url"],
                    "detail": t("按上面未就绪项处理后重试；此时不要提交研究任务",
                                "fix the items above and retry; do not submit research yet")}
        step(t("失败", "Failed"), False, t("工作台未响应", "workbench not responding"))
        release_instance_lock("failed")
        return {"state": "failed", "steps": steps, "url": rep["url"],
                "detail": t("查看 logs/ 后重试；诊断：python launcher.py diagnostics",
                            "check logs/ and retry; diagnostics: python launcher.py diagnostics")}
    except Exception as exc:
        logging.getLogger(__name__).error("启动控制器异常：%s", str(exc)[:200])
        step(t("失败", "Failed"), False, str(exc)[:160])
        release_instance_lock("failed")
        return {"state": "failed", "steps": steps, "url": web_url(),
                "detail": t("查看 logs/ 后重试；诊断：python launcher.py diagnostics",
                            "check logs/ and retry; diagnostics: python launcher.py diagnostics")}


def _run_dependency_check(fix: bool, fatal: bool) -> None:
    """启动前依赖自检（缺失自动补齐）；fatal=True 时必需项缺失即退出。

    SKIP_DEP_CHECK=1 可跳过（进阶/CI 场景）。依赖自检失败不静默——打印
    分项报告，把"缺什么/装了什么/是否成功"显式暴露。"""
    if os.environ.get("SKIP_DEP_CHECK", "0") == "1":
        return
    logger = logging.getLogger(__name__)
    try:
        import dep_check
    except Exception as exc:
        logger.warning("依赖自检模块不可用，跳过：%s", str(exc)[:120])
        return
    try:
        report = dep_check.ensure_all(auto=fix)
        print(dep_check.format_report(report))
    except Exception as exc:
        logger.warning("依赖自检异常（已忽略）：%s", str(exc)[:150])
        return
    if fatal and not report.get("ok"):
        print(import_cli_text().msg(
            "必需依赖未就绪：请按上面的提示处理后重试（或用 SKIP_DEP_CHECK=1 跳过自检）。",
            "Required dependencies are not ready: fix per the report above and retry "
            "(or set SKIP_DEP_CHECK=1 to skip this check).",
        ))
        sys.exit(1)


def main() -> None:
    # 终端编码适配：UTF-8 输出 + Windows 下尝试切 65001（老旧终端可用 WM_PLAIN_TEXT=1）
    import_cli_text().setup_console_encoding()
    logging_setup.setup_logging("launcher")
    logger = logging.getLogger(__name__)

    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "start":
        _run_dependency_check(fix=True, fatal=True)
        _check_redis_or_exit()
        if os.environ.get("WEAVEMIND_SUPERVISE", "0") == "1":
            supervise_services()
        else:
            start_services()
    elif action == "deps":
        fix = "--fix" in sys.argv[2:]
        _run_dependency_check(fix=fix, fatal=False)
    elif action == "supervise":
        supervise_services()
    elif action == "stop":
        stopped = stop_services()
        if stopped:
            logger.info("Stopped services: %s", ", ".join(stopped))
        else:
            logger.info("No running services recorded in %s", PID_FILE)
    elif action == "status":
        print_status()
    elif action == "url":
        # 启动脚本与页面共用同一端口来源：`python launcher.py url` 打印实际地址，
        # start.bat 据此打开浏览器（此前脚本里硬写 8080）。
        print(web_url())
    elif action == "up":
        # 统一启动控制器：检查运行包 → 依赖 → 配置 → 服务 → 就绪（新人入口只调它）
        result = startup_controller()
        print(f"  URL: {result.get('url') or web_url()}")
        sys.exit(0 if result.get("state") in ("research_ready", "limited_experience") else 1)
    elif action == "diagnostics":
        out_path = sys.argv[2] if len(sys.argv) > 2 else ""
        text = diagnostics_report()
        if out_path:
            Path(out_path).write_text(text, encoding="utf-8")
            print(import_cli_text().msg(f"已写入脱敏诊断：{out_path}（未上传）",
                                        f"redacted diagnostics written: {out_path}"))
        else:
            print(text)
    elif action == "readiness":
        _rep = readiness_report()
        print_readiness(_rep, quiet=False)
        sys.exit(0 if _rep["ready"] else 1)
    elif action == "restart":
        _run_dependency_check(fix=True, fatal=True)
        _check_redis_or_exit()
        stopped = stop_services(stop_portable_redis=False)
        logger.info("Stopped: %s", ", ".join(stopped) if stopped else "none")
        _ensure_redis_available()
        time.sleep(2)
        start_services()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
