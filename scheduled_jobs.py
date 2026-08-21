# -*- coding: utf-8 -*-
"""定时任务（F2）：按 config.json 的 scheduled_jobs 段周期提交用户任务。

config.json 示例：
    "scheduled_jobs": [
      {"name": "每日宏观简报", "cron": "09:30", "goal": "美国 CPI 宏观分析",
       "project": "default", "enabled": true},
      {"name": "每2小时币价", "interval_minutes": 120,
       "goal": "比特币最新价格", "project": "crypto", "enabled": true}
    ]

调度规则（刻意不引入 cron 库）：
- interval_minutes：正整数分钟，从调度器启动时刻起算（避免启动即误触发）；
- cron：每日 HH:MM（按本地时间），同一天只触发一次；
- 每次触发经 submit_fn 提交任务（web_ui 进程内直接发布 Redis），
  并把时间/job/任务id/结果追加到 logs/scheduled_jobs.log。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json",
)
DEFAULT_LOG_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs",
)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def load_jobs(config_path: str | None = None) -> list[dict]:
    """读取 config.json 的 scheduled_jobs 段；缺失/损坏返回空列表。"""
    try:
        with open(config_path or DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        jobs = (cfg or {}).get("scheduled_jobs") or []
        return [j for j in jobs if isinstance(j, dict)]
    except Exception:
        return []


def save_jobs(jobs: list[dict], config_path: str | None = None) -> bool:
    """把 jobs 写回 config.json 的 scheduled_jobs 段（保留其他配置段）。"""
    path = config_path or DEFAULT_CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["scheduled_jobs"] = jobs
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as exc:
        logger.warning("save_jobs failed: %s", exc)
        return False


def normalize_job(job: dict) -> dict | None:
    """校验并归一化一个定时任务；非法返回 None。"""
    if not isinstance(job, dict):
        return None
    name = str(job.get("name") or "").strip()
    goal = str(job.get("goal") or "").strip()
    if not name or not goal:
        return None
    interval = job.get("interval_minutes")
    cron = str(job.get("cron") or "").strip()
    if interval is not None:
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = None
        if interval is not None and interval <= 0:
            return None
    if cron and not _TIME_RE.match(cron):
        return None
    if interval is None and not cron:
        return None
    return {
        "name": name[:80],
        "goal": goal[:2000],
        "project": str(job.get("project") or "default")[:60],
        "interval_minutes": interval,
        "cron": cron or "",
        "enabled": bool(job.get("enabled", True)),
    }


def next_run_time(job: dict, now: datetime) -> datetime:
    """计算 job 相对 now 的下一次触发时间。
    interval_minutes → now + N 分钟；cron HH:MM → 下一个该时刻。"""
    cron = str(job.get("cron") or "").strip()
    if cron and _TIME_RE.match(cron):
        hour, minute = int(cron.split(":")[0]), int(cron.split(":")[1])
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    minutes = int(job.get("interval_minutes") or 0)
    return now + timedelta(minutes=max(1, minutes))


def schedule_label(job: dict) -> str:
    """人类可读的调度说明（用于日志/前端展示）。"""
    cron = str(job.get("cron") or "").strip()
    if cron:
        return f"每日 {cron}"
    return f"每 {job.get('interval_minutes')} 分钟"


def append_log(
    log_path: str | None,
    job: dict,
    task_id: str,
    result: str,
    detail: str = "",
) -> None:
    """追加一行执行记录：时间/job/任务id/结果。"""
    path = Path(log_path or os.path.join(DEFAULT_LOG_DIR, "scheduled_jobs.log"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"job={job.get('name', '?')} | task_id={task_id} | "
            f"result={result}"
            + (f" | detail={detail}" if detail else "")
            + "\n"
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        logger.warning("scheduled job log append failed: %s", exc)


class ScheduledJobsRunner:
    """简单调度循环：轮询 tick(now)，到点经 submit_fn 提交任务。

    可注入时钟与 submit_fn 便于测试；生产由 web_ui 的守护线程运行。
    """

    def __init__(
        self,
        submit_fn: Callable[[dict], str],
        config_path: str | None = None,
        log_path: str | None = None,
        poll_seconds: int = 20,
    ) -> None:
        self._submit_fn = submit_fn
        self._config_path = config_path
        self._log_path = log_path
        self._poll_seconds = max(1, int(poll_seconds))
        self._last_fire: dict[str, datetime] = {}
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now: datetime | None = None, force: bool = False) -> list[dict]:
        """检查一次到期任务并触发；返回本次触发记录（便于测试断言）。
        force=True 时 interval 任务即使无启动基线也立即触发（CLI --once 用）。"""
        now = now or datetime.now()
        fired: list[dict] = []
        jobs = load_jobs(self._config_path)
        for job in jobs:
            if not job.get("enabled", True):
                continue
            name = str(job.get("name") or "")
            cron = str(job.get("cron") or "").strip()
            with self._lock:
                last = self._last_fire.get(name)
            due = self._is_due(job, now, last, force=force)
            if not due:
                # interval 任务首次轮询：建立启动基线（不立即触发）
                if last is None and not cron and job.get("interval_minutes"):
                    with self._lock:
                        self._last_fire.setdefault(name, now)
                continue
            task_id = ""
            result = "error"
            detail = ""
            try:
                task_id = str(self._submit_fn(job) or "")
                result = "submitted"
            except Exception as exc:
                detail = str(exc)[:200]
            with self._lock:
                self._last_fire[name] = now
            append_log(self._log_path, job, task_id, result, detail)
            fired.append({
                "name": name,
                "task_id": task_id,
                "result": result,
                "time": now.isoformat(),
            })
            logger.info(
                "Scheduled job fired: %s (task_id=%s, result=%s)",
                name, task_id, result,
            )
        return fired

    @staticmethod
    def _is_due(
        job: dict, now: datetime, last: datetime | None,
        force: bool = False,
    ) -> bool:
        """判断 job 当前是否到期。
        - interval：首次启动不立即触发，满一个周期后才触发；
        - cron HH:MM：当天到达该时刻且当天未触发过。"""
        cron = str(job.get("cron") or "").strip()
        if cron and _TIME_RE.match(cron):
            if last is not None and last.date() == now.date():
                return False
            hour, minute = int(cron.split(":")[0]), int(cron.split(":")[1])
            return now.hour > hour or (
                now.hour == hour and now.minute >= minute
            )
        minutes = int(job.get("interval_minutes") or 0)
        if minutes <= 0:
            return False
        if last is None:
            return force  # 启动基线：先记时，不立即触发（force 时除外）
        return (now - last).total_seconds() >= minutes * 60 - 0.5

    def run(self) -> None:
        """常驻循环（web_ui 守护线程）。停止事件可用 stop() 触发。"""
        logger.info(
            "ScheduledJobsRunner started (poll every %ds, config=%s)",
            self._poll_seconds, self._config_path or DEFAULT_CONFIG_PATH,
        )
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                logger.warning("ScheduledJobsRunner tick error: %s", exc)
            self._stop.wait(self._poll_seconds)
        logger.info("ScheduledJobsRunner stopped")


def run_once(config_path: str | None = None) -> list[dict]:
    """CLI 辅助：立即触发一次所有到期任务（含调试）。"""
    runner = ScheduledJobsRunner(
        submit_fn=lambda job: _default_submit(job),
        config_path=config_path,
    )
    return runner.tick(force=True)


def _default_submit(job: dict) -> str:
    """独立运行时的兜底提交（直接调 web_ui 的发布逻辑需 Redis 就绪）。"""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import web_ui
    submitted = web_ui._publish_task(
        goal=str(job.get("goal") or ""),
        project=str(job.get("project") or "default"),
        auto_run=True,
        user_id="scheduler",
        prefix="sched",
    )
    return submitted.get("task_id", "")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="织光定时任务调度器")
    parser.add_argument("--once", action="store_true", help="只触发一次到期任务")
    parser.add_argument("--config", default=None, help="config.json 路径")
    args = parser.parse_args()
    if args.once:
        records = run_once(args.config)
        print(json.dumps(records, ensure_ascii=False, indent=2))
    else:
        from logging_setup import setup_logging
        setup_logging("scheduled_jobs")
        runner = ScheduledJobsRunner(
            submit_fn=_default_submit, config_path=args.config,
        )
        try:
            runner.run()
        except KeyboardInterrupt:
            runner.stop()
