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

# D1：内置"每日 A 股活跃度日报"——已验证的排行链路（全市场数据 → TOP10 →
# 图表 → 报告，约 6-10 分钟）。用户未配置同名任务时作为默认日报任务；
# 可在设置页禁用/删除。project=daily-report 的任务终态会自动生成分享链接。
BUILTIN_DAILY_REPORT = {
    "name": "每日 A 股活跃度日报",
    "goal": (
        "统计今日A股总成交量排名前十的股票，对比其涨跌幅与成交额分布，"
        "生成一份市场活跃度分析报告：需包含成交量TOP10柱状图、量价关系散点图、"
        "活跃板块归纳与市场情绪解读，所有数据注明来源和采集时间"
    ),
    "project": "daily-report",
    "cron": "16:00",
    "enabled": True,
}


def load_jobs(config_path: str | None = None) -> list[dict]:
    """读取 config.json 的 scheduled_jobs 段；缺失/损坏返回空列表。"""
    raw: list = []
    try:
        with open(config_path or DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        raw = (cfg or {}).get("scheduled_jobs") or []
    except Exception:
        raw = []
    jobs = [j for j in raw if isinstance(j, dict)]
    # D1：内置日报并入（用户同名任务优先，默认任务不重复）
    if not any(str(j.get("name") or "") == BUILTIN_DAILY_REPORT["name"] for j in jobs):
        jobs = [dict(BUILTIN_DAILY_REPORT)] + jobs
    return jobs


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


def _read_log(log_path: str | None, limit: int = 50) -> list[dict]:
    """读取调度日志尾部解析为记录（供 last_results / API 展示）。

    行格式与 append_log 对应：时间 | job=名 | task_id=… | result=… | detail=…
    """
    path = Path(log_path or os.path.join(DEFAULT_LOG_DIR, "scheduled_jobs.log"))
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for line in lines[-max(1, int(limit)):]:
        parts = [p.strip() for p in line.split("|")]
        rec = {"time": "", "job": "", "task_id": "", "result": "", "detail": ""}
        if parts:
            rec["time"] = parts[0]
        for p in parts[1:]:
            for key in ("job=", "task_id=", "result=", "detail="):
                if p.startswith(key):
                    rec[key[:-1]] = p[len(key):]
                    break
        out.append(rec)
    return out


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
        outcome_fn: Callable[[str], str | None] | None = None,
        on_failure: Callable[[str, str, str], None] | None = None,
    ) -> None:
        self._submit_fn = submit_fn
        self._config_path = config_path
        self._log_path = log_path
        self._poll_seconds = max(1, int(poll_seconds))
        # T2 结果追踪：outcome_fn(task_id) -> 'SUCCESS'/'FAILED'/.../None（未终态）
        self._outcome_fn = outcome_fn
        # T2 失败回调：on_failure(job_name, task_id, status)——告警/通知由此触发
        self._on_failure = on_failure
        self._last_fire: dict[str, datetime] = {}
        self._retry_at: dict[str, datetime] = {}  # 提交失败后的重试等待点
        self._submit_failures: dict[str, int] = {}  # 提交异常当日计数（重试用）
        self._tracked: dict[str, str] = {}  # task_id -> job_name（等待终态）
        self._alerted: set[str] = set()  # 已告警的 task_id（去重）
        self._retried: set[str] = set()  # 当日已重提的 job:task_id
        self._last_outcome_check = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now: datetime | None = None, force: bool = False) -> list[dict]:
        """检查一次到期任务并触发；返回本次触发记录（便于测试断言）。
        force=True 时 interval 任务即使无启动基线也立即触发（CLI --once 用）。"""
        now = now or datetime.now()
        fired: list[dict] = []
        self._check_outcomes(now)
        jobs = load_jobs(self._config_path)
        for job in jobs:
            if not job.get("enabled", True):
                continue
            name = str(job.get("name") or "")
            cron = str(job.get("cron") or "").strip()
            with self._lock:
                last = self._last_fire.get(name)
                retry_at = self._retry_at.get(name)
            if retry_at is not None:
                # 提交失败后的重试等待：到点即重提（不受 cron 同日判挡）
                if now < retry_at:
                    continue
                due = True
            else:
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
                if result == "submitted":
                    self._last_fire[name] = now
                    self._retry_at.pop(name, None)
                    self._submit_failures.pop(name, None)
                    if task_id:
                        self._tracked[task_id] = name
                else:
                    # 提交失败：不推进 _last_fire，30 分钟后重试（当日最多 2 次）
                    fails = self._submit_failures.get(name, 0) + 1
                    self._submit_failures[name] = fails
                    if fails <= 2:
                        self._retry_at[name] = now + timedelta(minutes=30)
                        detail += f"；将于 30 分钟后重试（{fails}/2）"
                    else:
                        self._last_fire[name] = now
                        self._retry_at.pop(name, None)
                        detail += "；当日重试次数用尽，等待下次调度窗口"
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

    def _check_outcomes(self, now: datetime) -> None:
        """T2 结果追踪：查询已提交任务的终态，FAILED 触发告警回调。

        每 5 分钟一轮（随 poll 循环），按 task_id 去重只告警一次；
        outcome_fn 未注入时静默跳过（CLI/测试路径行为不变）。"""
        import time as _time
        if self._outcome_fn is None:
            return
        if _time.time() - self._last_outcome_check < 300:
            return
        self._last_outcome_check = _time.time()
        with self._lock:
            pending = dict(self._tracked)
        for task_id, name in list(pending.items()):
            try:
                status = self._outcome_fn(task_id)
            except Exception:
                continue
            if not status:
                continue  # 未终态
            with self._lock:
                self._tracked.pop(task_id, None)
            if str(status).upper() != "FAILED":
                continue
            with self._lock:
                if task_id in self._alerted:
                    continue
                self._alerted.add(task_id)
            if self._on_failure:
                try:
                    self._on_failure(name, task_id, str(status))
                except Exception as exc:
                    logger.warning(
                        "Scheduled job failure callback error: %s", exc)

    def last_results(self, limit: int = 50) -> list[dict]:
        """读取调度日志尾部，供 /api/scheduled-jobs 展示上次结果。"""
        try:
            entries = _read_log(self._log_path, limit)
            return entries
        except Exception:
            return []

    @staticmethod
    @staticmethod
    def _cron_passed(job: dict, now: datetime) -> bool:
        """当天该 cron 时刻是否已经过去（严格晚于时刻）。"""
        cron = str(job.get("cron") or "").strip()
        if not (cron and _TIME_RE.match(cron)):
            return False
        hour, minute = int(cron.split(":")[0]), int(cron.split(":")[1])
        return now.hour > hour or (now.hour == hour and now.minute > minute)

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
            if now.hour > hour or (now.hour == hour and now.minute > minute):
                # 服务在当日时刻之后启动：错过当天窗口，不补发（次日按时触发）
                return False
            return now.hour == hour and now.minute >= minute
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
