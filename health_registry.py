# -*- coding: utf-8 -*-
"""外部依赖健康注册表：把散落的健康源收成一个统一视图。

改造前的状态：5 套健康源各自为政，字段与存储方式都不一样
（`llm_health` 进程内 dict、`search_health` Redis 键、`source_health` 进程内 dict、
`embed_health` Redis 键、`code_sandbox` 现算），字段名在
`healthy`/`ok`/`degraded`/`cooldown_until` 之间混用，而且 orchestrator 与 webui
是**两个进程**——`/api/status` 读不到编排侧的进程内状态（如行情适配器熔断）。

统一字段（每个依赖一条）：
    {name, ok, reason, since, source_process, detail}
"""

from __future__ import annotations

import json
import os
import time

# 依赖展示名
_NAME_LLM = "llm"
_NAME_SEARCH = "search"
_NAME_SOURCE = "market_source"
_NAME_EMBEDDING = "embedding"
_NAME_SANDBOX = "code_sandbox"

# 跨进程快照键（由拥有该状态的进程写入）
SEARCH_HEALTH_KEY = "search_engine_health"
SOURCE_HEALTH_KEY = "wm:source:health"
EMBED_HEALTH_KEY = "wm:embed:health"


def _redis_get(key: str) -> dict:
    try:
        import redis
        client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379") or 6379),
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
        )
        raw = client.get(key)
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _entry(name: str, ok: bool, reason: str = "", since: float = 0.0,
           detail: str = "") -> dict:
    return {
        "name": name,
        "ok": bool(ok),
        "reason": str(reason or "")[:200],
        "since": float(since or 0.0),
        "source_process": "webui" if _in_webui() else "orchestrator",
        "detail": str(detail or "")[:300],
    }


def _in_webui() -> bool:
    try:
        import sys
        return "web_ui" in (sys.argv[0] or "")
    except Exception:
        return False


def probe_llm() -> dict:
    """LLM 端点（进程内健康状态 + 余额探测结果）。"""
    try:
        from llm_client import get_endpoint_health, get_endpoint_warning
        health = get_endpoint_health() or {}
        primary = health.get("primary") or {}
        backup = health.get("backup") or {}
        ok = bool(primary.get("healthy")) or bool(backup.get("healthy"))
        reason = get_endpoint_warning() or ""
        if not ok:
            reason = reason or "主备端点均不健康"
        since = max(
            float(primary.get("last_degradation_ts") or 0.0),
            float(backup.get("last_degradation_ts") or 0.0),
        )
        return _entry(_NAME_LLM, ok, reason, since,
                      detail=f"primary={'ok' if primary.get('healthy') else 'down'},"
                             f" backup={'ok' if backup.get('healthy') else 'down'}")
    except Exception as exc:
        return _entry(_NAME_LLM, True, f"状态不可读：{str(exc)[:80]}")


def probe_search() -> dict:
    """搜索引擎健康（worker 写 Redis 快照，webui 也能读到）。"""
    data = _redis_get(SEARCH_HEALTH_KEY)
    if not data:
        return _entry(_NAME_SEARCH, True, "无快照（视为未知）", detail="no snapshot")
    engines = {k: v for k, v in data.items() if isinstance(v, dict)}
    unhealthy = [k for k, v in engines.items() if not v.get("healthy", True)]
    return _entry(
        _NAME_SEARCH, not unhealthy,
        "" if not unhealthy else "引擎不健康：" + ", ".join(unhealthy),
        detail=", ".join(
            f"{k}={'ok' if v.get('healthy', True) else 'down'}" for k, v in engines.items()
        ),
    )


def probe_market_source() -> dict:
    """行情适配器熔断状态（跨进程快照优先，缺失时回退本进程）。"""
    data = _redis_get(SOURCE_HEALTH_KEY)
    if not data:
        try:
            from adapters.source_health import get_health
            data = get_health() or {}
        except Exception:
            data = {}
    if not data:
        return _entry(_NAME_SOURCE, True, "无快照（视为未知）", detail="no snapshot")
    cooling = [
        f"{name}({info.get('reason') or 'cooling'})"
        for name, info in data.items()
        if isinstance(info, dict) and info.get("cooldown_until", 0) > time.time()
    ]
    return _entry(_NAME_SOURCE, not cooling,
                  "" if not cooling else "熔断冷却：" + ", ".join(cooling),
                  detail=f"{len(data)} 个数据源")


def probe_embedding() -> dict:
    """Embedding 接口（额度/网络）健康：降级会让记忆与提示词经验检索失效。"""
    try:
        import embed_health
        health = embed_health.embedding_health()
        return _entry(
            _NAME_EMBEDDING, bool(health.get("healthy")),
            "" if health.get("healthy") else str(health.get("last_error") or "降级")[:120],
            since=float(health.get("degraded_since") or 0.0),
            detail=f"连续失败 {health.get('fails')} 次（阈值 {health.get('threshold')}）",
        )
    except Exception as exc:
        return _entry(_NAME_EMBEDDING, True, f"状态不可读：{str(exc)[:80]}")


def probe_sandbox() -> dict:
    """代码沙箱可用性（现算）。"""
    try:
        from code_sandbox import sandbox_status
        status = sandbox_status() or {}
        mode = str(status.get("mode") or "unknown")
        docker = bool(status.get("docker_available"))
        return _entry(_NAME_SANDBOX, mode != "disabled",
                      "" if mode != "disabled" else "沙箱不可用",
                      detail=f"mode={mode}, docker={'yes' if docker else 'no'}")
    except Exception as exc:
        return _entry(_NAME_SANDBOX, True, f"状态不可读：{str(exc)[:80]}")


_PROBES = (
    probe_llm, probe_search, probe_market_source, probe_embedding, probe_sandbox,
)


def snapshot() -> list[dict]:
    """一次列出所有外部依赖状态（失败项不抛，逐条降级为"状态不可读"）。"""
    out: list[dict] = []
    for probe in _PROBES:
        try:
            out.append(probe())
        except Exception as exc:
            out.append(_entry(getattr(probe, "__name__", "unknown"), True,
                              f"probe failed: {str(exc)[:80]}"))
    return out


def unhealthy() -> list[dict]:
    """仅返回不健康的依赖（供告警/横幅使用）。"""
    return [item for item in snapshot() if not item.get("ok")]
