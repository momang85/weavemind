# -*- coding: utf-8 -*-
"""内容过滤/隔离的**脱敏**诊断（任务级 JSONL）。

架构复核 §F1 要求：补脱敏诊断（规则 ID、依赖步骤、内容类型、截断前后长度、隔离状态），
**不记录完整客户提示词**。本模块只记形状，不记正文——与 `llm_client._record_llm_call`
（`llm_calls.jsonl`）同一纪律。

为什么单独一份：过滤事件可能多次发生、且大多不是"步骤失败"，塞进 `step_failure.json`
（按 step_id 去重、失败专用）或 `metrics.csv`（无逐内容溯源）都不合适。
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import workspace

logger = logging.getLogger(__name__)

EVENTS_FILE = "content_filter_events.jsonl"
_MAX_EVENTS = 200          # 每任务保留上限（与 llm_calls 的 200 对齐）

# 隔离状态：none=未隔离；line=只隔离命中行；whole=整段替换；
# truncated=按结构截断；skipped=内容不可用被跳过
ISOLATION_STATES = ("none", "line", "whole", "truncated", "skipped")


def build_event(task_id: str, *, rule_id: str = "", stage: str = "",
                dep_step_id: str = "", content_type: str = "",
                chars_before: int = 0, chars_after: int = 0,
                isolation: str = "none", reason: str = "") -> dict:
    """构造一条过滤事件（**只含形状**：没有正文、没有提示词、没有密钥）。"""
    state = str(isolation or "none")
    return {
        "task_id": str(task_id or ""),
        "rule_id": str(rule_id or ""),
        "stage": str(stage or ""),
        "dep_step_id": str(dep_step_id or ""),
        "content_type": str(content_type or ""),
        "chars_before": int(chars_before or 0),
        "chars_after": int(chars_after or 0),
        "isolation": state if state in ISOLATION_STATES else "none",
        "reason": str(reason or "")[:120],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def append_event(task_id: str, event: dict | None = None, *, ws_dir=None,
                 **fields) -> dict:
    """追加一条过滤事件；写失败只记日志，不影响主线。"""
    ev = dict(event or build_event(task_id, **fields))
    ev.setdefault("task_id", str(task_id or ""))
    try:
        p = _events_path(task_id, ws_dir)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        _trim(p)
    except Exception as exc:                     # noqa: BLE001 - 诊断不得拖垮主线
        logger.warning("内容过滤事件落盘失败（task=%s）：%s", task_id, str(exc)[:120])
    return ev


def read_events(task_id: str, *, ws_dir=None) -> list[dict]:
    """读回过滤事件（坏行跳过，不抛）。"""
    p = _events_path(task_id, ws_dir)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if isinstance(ev, dict):
                out.append(ev)
    except Exception as exc:                     # noqa: BLE001
        logger.warning("内容过滤事件读取失败（task=%s）：%s", task_id, str(exc)[:120])
    return out


def summarize(task_id: str, *, ws_dir=None) -> dict:
    """页面用汇总：条数、规则分布、隔离分布、最近 5 条（字段仍是形状）。"""
    events = read_events(task_id, ws_dir=ws_dir)
    if not events:
        return {}
    by_rule: dict[str, int] = {}
    by_isolation: dict[str, int] = {}
    for ev in events:
        by_rule[str(ev.get("rule_id") or "unknown")] = (
            by_rule.get(str(ev.get("rule_id") or "unknown"), 0) + 1)
        by_isolation[str(ev.get("isolation") or "none")] = (
            by_isolation.get(str(ev.get("isolation") or "none"), 0) + 1)
    return {
        "count": len(events),
        "by_rule": by_rule,
        "by_isolation": by_isolation,
        "recent": events[-5:],
    }


def _events_path(task_id: str, ws_dir=None) -> Path:
    ws = Path(ws_dir) if ws_dir else workspace.task_workspace(task_id)
    return ws / EVENTS_FILE


def _trim(p: Path) -> None:
    """只保留最近 `_MAX_EVENTS` 行（避免长任务把文件堆大）。"""
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_EVENTS:
            p.write_text("\n".join(lines[-_MAX_EVENTS:]) + "\n", encoding="utf-8")
    except Exception:
        pass
