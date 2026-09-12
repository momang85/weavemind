# -*- coding: utf-8 -*-
"""Embedding 接口健康状态：跨进程记录降级并供 WebUI 呈现。

背景：Embedding 走的是独立于 LLM 的额度（SiliconFlow 兼容接口），额度耗尽时
记忆检索、提示词经验检索会全部落空，但调用方只打 WARNING 日志——界面上的表现
是"记忆命中率 0%"，看不出是接口欠费。本模块把失败计数与最近错误写进 Redis，
让 webui 进程（与 orchestrator/worker 不同进程）能读到并在 Health 上给出明确
提示。

设计约束：
- 任何异常都必须静默：健康记录失败绝不能影响记忆检索主流程；
- 阈值内不判定降级（偶发网络抖动不应打扰用户）；
- 记录带 TTL，进程重启或长期无失败时状态自动过期，避免陈旧告警。
"""

from __future__ import annotations

import json
import os
import time

HEALTH_KEY = "wm:embed:health"
HEALTH_TTL_SECONDS = int(os.environ.get("EMBED_HEALTH_TTL_SECONDS", "3600"))
FAIL_THRESHOLD = int(os.environ.get("EMBED_FAIL_THRESHOLD", "3"))

_client = None
_client_failed = False
_local: dict = {}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except Exception:
        return default


def _connect():
    """惰性连接 Redis；连接失败后不再重试（本模块只做旁路观测）。"""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    try:
        import redis
        host = os.environ.get("REDIS_HOST", "127.0.0.1")
        port = int(os.environ.get("REDIS_PORT", "6379") or 6379)
        _client = redis.Redis(
            host=host, port=port, decode_responses=True,
            socket_connect_timeout=2, socket_timeout=2,
        )
        _client.ping()
    except Exception:
        _client = None
        _client_failed = True
    return _client


def _load() -> dict:
    client = _connect()
    if client is None:
        return dict(_local)
    try:
        raw = client.get(HEALTH_KEY)
        return json.loads(raw) if raw else {}
    except Exception:
        return dict(_local)


def _store(state: dict) -> None:
    global _local
    _local = dict(state)
    client = _connect()
    if client is None:
        return
    try:
        client.set(HEALTH_KEY, json.dumps(state, ensure_ascii=False),
                   ex=HEALTH_TTL_SECONDS)
    except Exception:
        pass


def record_embed_fail(error: str) -> dict:
    """记一次失败；返回更新后的状态（含是否刚跨过降级阈值）。"""
    now = time.time()
    state = _load()
    fails = int(state.get("fails") or 0) + 1
    was_degraded = bool(state.get("degraded"))
    # 连续失败计数：record_embed_ok 会把 fails 清零，所以这里只需看阈值
    state.update({
        "fails": fails,
        "last_error": str(error)[:300],
        "last_fail_ts": now,
        "degraded": fails >= FAIL_THRESHOLD,
    })
    if not was_degraded and state["degraded"]:
        state["degraded_since"] = now
    _store(state)
    return state


def record_embed_ok() -> dict:
    """记一次成功：清零失败计数并解除降级（曾降级则标记恢复）。"""
    state = _load()
    was_degraded = bool(state.get("degraded"))
    state.update({
        "fails": 0,
        "last_ok_ts": time.time(),
        "degraded": False,
    })
    state.pop("degraded_since", None)
    state["recovered"] = bool(was_degraded)
    _store(state)
    return state


def embedding_health() -> dict:
    """读取当前状态（供 /api/status 使用）。"""
    state = _load()
    return {
        "healthy": not bool(state.get("degraded")),
        "degraded": bool(state.get("degraded")),
        "fails": int(state.get("fails") or 0),
        "threshold": FAIL_THRESHOLD,
        "last_error": str(state.get("last_error") or ""),
        "last_fail_ts": float(state.get("last_fail_ts") or 0.0),
        "last_ok_ts": float(state.get("last_ok_ts") or 0.0),
        "degraded_since": float(state.get("degraded_since") or 0.0),
    }


def degradation_notice() -> str:
    """降级时给用户看的一句话（含错误摘要）；健康时返回空串。"""
    health = embedding_health()
    if health["healthy"]:
        return ""
    err = health["last_error"]
    hint = ""
    if "402" in err or "insufficient" in err.lower():
        hint = "（接口返回额度不足，需为该 Embedding 服务充值）"
    return (
        f"Embedding 接口连续失败 {health['fails']} 次{hint}："
        "历史记忆与提示词经验检索已降级（记忆命中率会显示为 0%），"
        "不影响本次任务执行，但经验复用失效。"
    )


def reset_health() -> None:
    """仅供测试/运维手工复位。"""
    _store({})
