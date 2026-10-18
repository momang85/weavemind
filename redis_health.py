"""织光 (ZhiGuang) — Redis 可用性探针与降级标志（任务20）

Redis 在本系统里同时承担**队列/消息总线/预算账本**三件事。它一旦在运行中崩溃：
    1. 编排器 `lpush task_queue:{agent}` 抛连接异常 → 步骤重试到耗尽，界面只说"失败"；
    2. worker 的 BRPOP 拿不到任务，整条流水线停摆；
    3. webui 的检索/行情/embedding 健康快照全部读不到 → 依赖健康视图显示"未知"，
       用户看不出"是 Redis 挂了"还是"上游数据源没数据"。

本模块只做两件事，别的一概不做：
    - `check_redis()`：**真探测**一次（2s 连接超时、关掉 redis-py 内建重试）；
    - 进程内降级标志 `set_degraded_mode()` / `is_degraded()`：
      "Redis 现在不可用"这件事要在本进程内可判定、可展示、可自愈。

**降级标志是进程内的**（架构约束：不写 DB，DB 本身不依赖 Redis）。织光是多进程单体：
guardian 置位的那份标志**别的进程看不见**（唯一能跨进程的通道恰好就是挂掉的 Redis）。
因此每个需要它的进程（webui / orchestrator）都用 `check_redis_cached()` 从**自己的**
探测结果刷新本进程标志——标志是"派生自探测"的，不是"靠别人通知"的。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time

logger = logging.getLogger("redis_health")

# 连接超时（秒）：本机 Redis 挂掉时是立刻 ECONNREFUSED；2s 用于兜住"Docker 端口代理
# 静默丢包"那类假连接（历史上实测 redis-py 默认重试会把它叠成 26~48s）。
CONNECT_TIMEOUT = 2.0

# 探测结果缓存（秒）：`/api/status` 前端约 3s 轮询一次，不能每次都真连 2s。
PROBE_TTL = 5.0

_LOCK = threading.Lock()
_STATE: dict = {
    "degraded": False,     # 本进程当前是否处于 Redis 降级模式
    "since": 0.0,          # 进入降级模式的时刻（time.time()）
    "error": "",           # 最近一次探测失败原因（已脱敏）
    "probe": None,         # 最近一次探测结果
    "probe_at": 0.0,       # 最近一次探测时刻（time.monotonic()）
}

_URL_RE = re.compile(r"(?:redis|rediss)://[^\s'\"，。；]+")
_HOSTPORT_RE = re.compile(r"\b[\w.\-]+:\d{2,5}\b")


def redis_url(url: str | None = None) -> str:
    """解析 Redis 地址：显式入参 > REDIS_URL > REDIS_HOST/REDIS_PORT > 默认本机。"""
    if url:
        return str(url)
    env_url = os.environ.get("REDIS_URL")
    if env_url:
        return str(env_url)
    host = os.environ.get("REDIS_HOST") or "127.0.0.1"
    port = os.environ.get("REDIS_PORT") or "6379"
    return f"redis://{host}:{port}"


def sanitize_error(err) -> str:
    """去掉地址/端口：错误文本会进**公开**健康接口与告警，不该泄漏内部拓扑。"""
    text = str(err or "")
    text = _URL_RE.sub("<redis>", text)
    text = _HOSTPORT_RE.sub("<redis>", text)
    return text[:160]


def _client(url: str | None = None):
    """构造一次性客户端（关掉内建重试，避免把 2s 超时叠成分钟级）。"""
    import redis as _redis

    kwargs: dict = {"socket_connect_timeout": CONNECT_TIMEOUT,
                    "socket_timeout": CONNECT_TIMEOUT}
    try:
        from common import _NO_REDIS_RETRY
        kwargs["retry"] = _NO_REDIS_RETRY
    except Exception:                                  # noqa: BLE001 - 无 common 也能探测
        pass
    return _redis.from_url(redis_url(url), **kwargs)


def check_redis(url: str | None = None) -> dict:
    """真探测：`{available, latency_ms, error}`（error 已脱敏，失败原因最多 160 字）。"""
    try:
        client = _client(url)
        t0 = time.monotonic()
        try:
            client.ping()
        finally:
            try:
                client.close()
            except Exception:                          # noqa: BLE001 - 关不掉不影响结论
                pass
        return {"available": True,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "error": None}
    except Exception as exc:                           # noqa: BLE001 - 探测失败就是不可用
        return {"available": False, "latency_ms": None,
                "error": sanitize_error(exc) or exc.__class__.__name__}


def apply_probe(result: dict, *, source: str = "") -> dict:
    """把一次探测结果写进本进程降级标志（只在**状态变化**时打日志）。"""
    available = bool((result or {}).get("available"))
    if available:
        set_degraded_mode(False)
    else:
        set_degraded_mode(True, error=str((result or {}).get("error") or ""),
                          source=source)
    return result


def check_redis_cached(url: str | None = None, *, ttl: float = PROBE_TTL) -> dict:
    """带缓存的探测（**并据此刷新本进程降级标志**）。返回 `{available, latency_ms, error, cached}`。"""
    now = time.monotonic()
    with _LOCK:
        cached = _STATE.get("probe")
        fresh = bool(cached) and (now - float(_STATE.get("probe_at") or 0.0)) < max(0.0, ttl)
    if fresh:
        return dict(cached, cached=True)
    result = check_redis(url)
    with _LOCK:
        _STATE["probe"] = dict(result)
        _STATE["probe_at"] = time.monotonic()
    apply_probe(result, source="probe")
    return dict(result, cached=False)


def set_degraded_mode(degraded: bool, *, error: str = "", source: str = "") -> bool:
    """置位/清除降级模式，返回**是否发生了状态变化**（调用方据此决定要不要告警）。"""
    degraded = bool(degraded)
    with _LOCK:
        changed = degraded != bool(_STATE.get("degraded"))
        if degraded:
            _STATE["degraded"] = True
            _STATE["error"] = sanitize_error(error) or _STATE.get("error") or ""
            if changed:
                _STATE["since"] = time.time()
        else:
            _STATE["degraded"] = False
            _STATE["since"] = 0.0
            _STATE["error"] = ""
        err_text = str(_STATE.get("error") or "")
    if changed:
        if degraded:
            logger.warning("Redis 降级模式开启（%s）：%s",
                           source or "未知来源", err_text or "连接失败")
        else:
            logger.info("Redis 降级模式解除（%s）：连接已恢复", source or "未知来源")
    return changed


def is_degraded() -> bool:
    """本进程是否处于 Redis 降级模式。"""
    with _LOCK:
        return bool(_STATE.get("degraded"))


def degraded_state() -> dict:
    """降级模式快照（供健康接口展示）。`duration_s` 便于判断"挂了多久"。"""
    with _LOCK:
        degraded = bool(_STATE.get("degraded"))
        since = float(_STATE.get("since") or 0.0)
        return {"degraded": degraded,
                "since": since,
                "duration_s": round(time.time() - since, 1) if (degraded and since) else 0.0,
                "error": str(_STATE.get("error") or "")}


def status_snapshot(url: str | None = None, *, ttl: float = PROBE_TTL) -> dict:
    """给健康接口用的一份状态：探测结果 + 降级标志（探测异常绝不上抛）。"""
    try:
        probe = check_redis_cached(url, ttl=ttl)
    except Exception as exc:                           # noqa: BLE001 - 健康接口不因探测炸掉
        probe = {"available": False, "latency_ms": None,
                 "error": sanitize_error(exc) or "探测失败"}
    state = degraded_state()
    return {"available": bool(probe.get("available")),
            "latency_ms": probe.get("latency_ms"),
            "error": sanitize_error(probe.get("error")) if probe.get("error") else None,
            "cached": bool(probe.get("cached")),
            "degraded_mode": bool(state["degraded"]),
            "degraded_since": state["since"],
            "degraded_duration_s": state["duration_s"]}


def public_snapshot(url: str | None = None) -> dict:
    """公开健康接口可暴露的最小事实：**不含**错误文本/地址/降级起点。"""
    snap = status_snapshot(url)
    return {"available": snap["available"],
            "degraded_mode": snap["degraded_mode"],
            "latency_ms": snap["latency_ms"]}


def dispatch_gate() -> str:
    """派发闸门：降级模式下**不再**往队列里塞任务（塞不进去只会白等超时）。

    返回空串表示可以派发；否则返回给用户看的原因。

    降级标志是"上次探测的结论"，可能已经过期几十秒——所以这里先按缓存 TTL 复探一次：
    Redis 已经恢复就直接放行（`check_redis_cached` 会同时清掉标志）。否则一个短抖动
    会把整个任务卡在"永远降级"上，直到有人重启服务。
    """
    if not is_degraded():
        return ""
    try:
        probe = check_redis_cached()
    except Exception:                                  # noqa: BLE001 - 复探失败按仍不可用
        probe = {"available": False}
    if probe.get("available"):
        return ""
    state = degraded_state()
    return ("Redis 不可用（降级模式）：队列/结果通道中断，本步骤未派发"
            f"（不可用已持续 {state['duration_s']:.0f}s）。"
            "恢复后心跳会自动解除降级，无需重启服务。")


def reset_for_tests() -> None:
    """测试专用：清空进程内状态（含探测缓存）。"""
    with _LOCK:
        _STATE.update({"degraded": False, "since": 0.0, "error": "",
                       "probe": None, "probe_at": 0.0})
