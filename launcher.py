"""织光 (ZhiGuang) - 统一服务进程管理器。

用法：
    python launcher.py             # 启动全部服务（先清理旧进程）
    python launcher.py start       # 同上
    python launcher.py stop        # 按 PID 文件精确停止全部服务
    python launcher.py status      # 查看运行状态

所有服务 PID 写入 .weavemind/pids.json，stop 时按 PID 精确结束，
不再使用 taskkill /IM python.exe 之类的全杀方案。
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
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
            return True
        except Exception:
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
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


def stop_services() -> list[str]:
    """按 PID 文件停止全部服务，返回已停止的服务名列表。"""
    pids = _read_pids()
    services = pids.get("services", {})
    stopped: list[str] = []
    for name, pid in services.items():
        if _kill_pid(pid):
            stopped.append(name)
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
        try:
            if out_path:
                fh = open(out_path, "a", encoding="utf-8")
            else:
                fh = subprocess.DEVNULL
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd or BASE_DIR),
                env=os.environ.copy(),
                stdout=fh,
                stderr=fh,
                creationflags=_CREATE_NO_WINDOW,
            )
            pids["services"][name] = proc.pid
            logger.info("[%s] started pid=%s", name, proc.pid)
        except Exception as exc:
            logger.error("[%s] failed to start: %s", name, exc)

    _write_pids(pids)
    front_url = (
        "http://localhost:8080"
        if (BASE_DIR / "frontend" / "dist" / "index.html").exists()
        else "http://localhost:5173"
    )
    logger.info("All services started. WebUI: http://localhost:8080  Frontend: %s", front_url)
    return pids


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
        start_services()
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
