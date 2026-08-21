"""织光 (ZhiGuang) - 统一服务进程管理器。

用法：
    python launcher.py             # 启动全部服务（先清理旧进程）
    python launcher.py start       # 同上
    python launcher.py supervise   # 守护模式：启动全部服务后循环巡检，崩溃自动重启
    python launcher.py stop        # 按 PID 文件精确停止全部服务
    python launcher.py status      # 查看运行状态

所有服务 PID 写入 .weavemind/pids.json，stop 时按 PID 精确结束，
不再使用 taskkill /IM python.exe 之类的全杀方案。
start 时若环境变量 WEAVEMIND_SUPERVISE=1 同样进入守护模式（start.bat 默认不开）。
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
    os.environ["PYTHONIOENCODING"] = "utf-8"


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
        return _scan_windows_wmic()
    return _scan_posix_pgrep()


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


def stop_services() -> list[str]:
    """按 PID 文件停止全部服务，返回已停止的服务名列表。"""
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
    return stopped


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
    stopped = stop_services()
    if stopped:
        logger.info("Stopped: %s", ", ".join(stopped))
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
    logger.info("All services started. WebUI: http://localhost:8080  Frontend: %s", front_url)
    return pids


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


def print_status() -> None:
    pids = _read_pids()
    services = pids.get("services", {})
    if not services:
        print("No services recorded (run `python launcher.py` to start).")
        return
    print(f"Started at: {pids.get('started_at', 'unknown')}")
    alive = 0
    for name, pid in services.items():
        ok = _is_alive(pid)
        alive += 1 if ok else 0
        print(f"  [{'UP' if ok else 'DOWN'}] {name} (pid={pid})")
    print(f"{alive}/{len(services)} services alive")


def main() -> None:
    logging_setup.setup_logging("launcher")
    logger = logging.getLogger(__name__)

    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "start":
        if os.environ.get("WEAVEMIND_SUPERVISE", "0") == "1":
            supervise_services()
        else:
            start_services()
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
        stopped = stop_services()
        logger.info("Stopped: %s", ", ".join(stopped) if stopped else "none")
        time.sleep(2)
        start_services()
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
