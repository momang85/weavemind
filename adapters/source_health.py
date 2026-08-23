# -*- coding: utf-8 -*-
"""行情数据源健康注册表（熔断 + 冷却，最终泛化层）。

对标 worker_base 的搜索引擎健康路由（_ENGINE_HEALTH）：为 eastmoney /
tencent / sina 行情源统一管理健康状态，避免任务内连续请求触发风控后
每次"碰运气"重试（RemoteDisconnected / HTTP 456 / 超时）。

设计：
- 进程内全局单例 + 线程锁，所有适配器共享同一份健康状态；
- mark_failure 连续 2 次失败 → cooldown_until = now + SOURCE_COOLDOWN
  （默认 300s，实测新浪 456 约 5 分钟恢复，可用环境变量覆盖）；
- mark_success 清零失败计数并解除冷却；冷却到期自动恢复；
- is_available 返回 (ok, reason)，冷却期内 reason 带剩余秒数与上次错误；
- get_health() 供 /api/status 或日志查看。

数据源命名与 router.DATA_CAPABILITIES 保持一致：
    eastmoney_ranking / tencent_ranking / tencent_us_ranking / sina_ranking
"""

from __future__ import annotations

import os
import threading
import time

# 连续失败阈值：2 次 → 进入冷却（与搜索引擎熔断先例一致）
SOURCE_FAIL_THRESHOLD = 2
# 冷却时长（秒）：默认 300s，可用 SOURCE_COOLDOWN 环境变量覆盖
SOURCE_COOLDOWN = 300.0
# last_error 保存上限，避免错误堆栈撑爆内存
_MAX_ERROR_LEN = 240

_HEALTH: dict[str, dict] = {}
_LOCK = threading.Lock()


class SourceInCooldownError(RuntimeError):
    """源处于冷却期：调用方应跳过该源（快失败，不浪费请求）。"""

    def __init__(self, source: str, reason: str):
        super().__init__(f"source {source} in cooldown: {reason}")
        self.source = source
        self.reason = reason


def _cooldown_seconds() -> float:
    """读取冷却秒数：每次调用读取，支持运行期改环境变量后生效。"""
    try:
        return max(0.0, float(os.environ.get("SOURCE_COOLDOWN", "300") or 300))
    except (TypeError, ValueError):
        return SOURCE_COOLDOWN


def reset() -> None:
    """清空健康注册表（测试隔离 / 手动恢复用）。"""
    with _LOCK:
        _HEALTH.clear()


def _blank() -> dict:
    return {"fails": 0, "cooldown_until": 0.0, "last_error": ""}


def mark_failure(source: str, error: Exception | str) -> None:
    """记录一次失败；连续失败达阈值 → 进入冷却。

    冷却期内继续失败只更新 last_error，不延长冷却窗口，避免高频请求
    反复刷新 5 分钟冷却。恢复后下一次失败会重新触发冷却。
    """
    name = str(source or "").strip()
    if not name:
        return
    msg = str(error)[:_MAX_ERROR_LEN]
    now = time.time()
    with _LOCK:
        h = _HEALTH.setdefault(name, _blank())
        h["fails"] += 1
        h["last_error"] = msg
        if h["fails"] >= SOURCE_FAIL_THRESHOLD:
            if h["cooldown_until"] <= now:
                h["cooldown_until"] = now + _cooldown_seconds()


def mark_success(source: str) -> None:
    """记录一次成功：失败计数清零，解除冷却。"""
    name = str(source or "").strip()
    if not name:
        return
    with _LOCK:
        _HEALTH[name] = _blank()


def is_available(source: str) -> tuple[bool, str]:
    """源是否可用：返回 (ok, reason)。

    ok=False 时 reason 含冷却剩余秒数、失败次数与上次错误；
    冷却到期自动恢复，不依赖显式 mark_success。
    """
    name = str(source or "").strip()
    now = time.time()
    with _LOCK:
        h = _HEALTH.get(name)
        if not h:
            return True, ""
        if h.get("cooldown_until", 0.0) > now:
            remaining = int(h["cooldown_until"] - now) + 1
            err = h.get("last_error") or "unknown"
            return False, (
                f"source {name} in cooldown (剩余 {remaining}s, "
                f"连续失败 {h.get('fails', 0)} 次, 上次错误: {err})"
            )
        return True, ""


def ensure_available(source: str) -> None:
    """不可用直接抛 SourceInCooldownError（快失败，不浪费请求）。"""
    ok, reason = is_available(source)
    if not ok:
        raise SourceInCooldownError(source, reason)


def get_health() -> dict:
    """健康快照：{source: {fails, cooldown_until, last_error, healthy, remaining}}。

    供 /api/status 或日志查看；返回副本，外部修改不影响注册表。
    """
    now = time.time()
    with _LOCK:
        out: dict[str, dict] = {}
        for name, h in _HEALTH.items():
            entry = dict(h)
            entry["healthy"] = entry.get("cooldown_until", 0.0) <= now
            entry["remaining"] = max(
                0.0, entry.get("cooldown_until", 0.0) - now,
            )
            out[name] = entry
        return out
