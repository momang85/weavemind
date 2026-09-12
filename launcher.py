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
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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


REDIS_SETUP_HINT = """\
无法连接 Redis（{host}:{port}）——织光的消息总线/任务队列依赖它，服务无法启动。

无需 Docker 的三种方案（任选其一，装好保持 6379 端口后重跑本命令）：
  1) Memurai（Redis 兼容的 Windows 服务，开发者版免费）：https://www.memurai.com
  2) tporadowski/redis（Redis 5.x Windows 移植版）：GitHub 搜 tporadowski/redis，
     解压后双击 redis-server.exe，或 redis-server.exe --service-install 注册服务
  3) WSL2：wsl --install 后 sudo apt install redis-server && sudo service redis-server start
  或使用 Docker 方式：docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine
详见 docs/部署指南.md「无 Docker 的 Redis 方案」；也可用 REDIS_HOST/REDIS_PORT
指向其它机器上的 Redis，或用 SKIP_REDIS_CHECK=1 跳过本检查。
"""


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

    logger.info("Stopping previous services (if any)...")
    stopped = stop_services(stop_portable_redis=False)
    if stopped:
        logger.info("Stopped: %s", ", ".join(stopped))
    _ensure_redis_available()
    time.sleep(2)

    services = build_services(cfg)
    pids: dict = {"services": {}}
    for name, argv, cwd, out_path in services:
        pid = _spawn_service(name, argv, cwd, out_path)
        if pid:
            pids["services"][name] = pid
            logger.info("[%s] started pid=%s", name, pid)
        else:
            logger.error("[%s] failed to start", name)

    _write_pids(pids)
    front_url = (
        "http://localhost:8080"
        if (BASE_DIR / "frontend" / "dist" / "index.html").exists()
        else "http://localhost:5173"
    )
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
    logger.info("All services started. WebUI: http://localhost:8080  Frontend: %s", front_url)
    return pids


def verify_services(quiet: bool = True) -> dict:
    """启动后校验：等待若干秒后统计服务实际存活情况。

    返回 {total, alive, down:[(pid, name)], waited}；不等严格模式也会如实打印。"""
    try:
        wait = float(os.environ.get("WM_START_VERIFY_WAIT", "8") or 8)
    except Exception:
        wait = 8.0
    wait = max(0.0, min(wait, 60.0))
    if wait:
        time.sleep(wait)
    services = _read_pids().get("services", {})
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
    summary = {"total": len(services), "alive": alive, "down": down, "waited": wait}
    if not quiet or down:
        ok = not down
        line = import_cli_text().msg(
            f"  [{ 'OK' if ok else '!!' }] 启动校验：{alive}/{len(services)} 服务存活"
            + ("" if ok else "；未存活：" + ", ".join(n for _, n in down[:6])),
            f"  [{ 'OK' if ok else '!!' }] Startup check: {alive}/{len(services)} alive"
            + ("" if ok else "; down: " + ", ".join(n for _, n in down[:6])),
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
