# -*- coding: utf-8 -*-
"""行情排行缓存：Redis 优先，进程内内存 dict 兜底。

key 规则：ranking:{market}:{metric}（如 ranking:a:amount / ranking:us:volume）；
TTL 默认 600 秒，可用环境变量 QUOTE_CACHE_TTL 覆盖（秒）。
Redis 不可用/读写异常时自动降级内存缓存（带过期时间），
保证任务间复用排行结果、减少上游行情接口请求量。
"""

import json
import logging
import os
import threading
import time

try:
    import redis as _redis_lib
except Exception:  # pragma: no cover - 无 redis 依赖时仅内存缓存
    _redis_lib = None

_logger = logging.getLogger(__name__)

_DEFAULT_TTL = 600
_KEY_PREFIX = "ranking"
_MAX_MEMORY_ENTRIES = 512


def cache_ttl() -> int:
    """QUOTE_CACHE_TTL 环境变量 → TTL（秒）；非法值回退默认 600。"""
    try:
        return max(30, int(os.environ.get("QUOTE_CACHE_TTL", str(_DEFAULT_TTL))))
    except (TypeError, ValueError):
        return _DEFAULT_TTL


def _new_redis_client():
    """环境变量感知的 Redis 客户端（连接超时短，失败快速回落内存）。"""
    return _redis_lib.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
        socket_connect_timeout=1.0,
        socket_timeout=1.5,
        socket_keepalive=True,
    )


class QuoteCache:
    """排行结果缓存：Redis 可用走 Redis，不可用走内存 dict。

    内存兜底始终启用（同一进程内跨任务复用）；Redis 路径用于跨进程复用。
    """

    def __init__(self, redis_client=None, ttl: int | None = None):
        self._redis = redis_client
        if self._redis is None and _redis_lib is not None:
            try:
                self._redis = _new_redis_client()
            except Exception as exc:
                _logger.warning("quote cache redis client init failed: %s", exc)
                self._redis = None
        self._ttl = ttl if ttl is not None else cache_ttl()
        self._memory: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(market: str, metric: str) -> str:
        return f"{_KEY_PREFIX}:{market}:{metric}"

    def _memory_get(self, key: str) -> dict | None:
        with self._lock:
            item = self._memory.get(key)
            if not item:
                return None
            expires_at, payload = item
            if expires_at <= time.time():
                del self._memory[key]
                return None
            return payload

    def _memory_set(self, key: str, payload: dict, ttl: int) -> None:
        with self._lock:
            self._memory[key] = (time.time() + ttl, payload)
            if len(self._memory) > _MAX_MEMORY_ENTRIES:
                now = time.time()
                expired = [k for k, (exp, _) in self._memory.items() if exp <= now]
                for k in expired:
                    del self._memory[k]
                while len(self._memory) > _MAX_MEMORY_ENTRIES:
                    oldest = min(
                        self._memory, key=lambda k: self._memory[k][0],
                    )
                    del self._memory[oldest]

    def get(self, market: str, metric: str) -> dict | None:
        """命中返回 payload，未命中/过期返回 None。"""
        key = self._key(market, metric)
        if self._redis is not None:
            try:
                raw = self._redis.get(key)
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                _logger.warning("quote cache redis get failed, fallback memory: %s", exc)
                self._redis = None
        return self._memory_get(key)

    def set(self, market: str, metric: str, payload: dict, ttl: int | None = None) -> bool:
        """回填缓存；返回是否成功写入 Redis（内存兜底失败不算失败）。"""
        key = self._key(market, metric)
        ttl = ttl if ttl is not None else self._ttl
        redis_ok = False
        if self._redis is not None:
            try:
                self._redis.set(
                    key, json.dumps(payload, ensure_ascii=False), ex=ttl,
                )
                redis_ok = True
            except Exception as exc:
                _logger.warning("quote cache redis set failed, fallback memory: %s", exc)
                self._redis = None
        self._memory_set(key, payload, ttl)
        return redis_ok

    def delete(self, market: str, metric: str) -> None:
        """删除指定缓存（测试/运维清理用）。"""
        key = self._key(market, metric)
        if self._redis is not None:
            try:
                self._redis.delete(key)
            except Exception:
                pass
        with self._lock:
            self._memory.pop(key, None)

    def clear(self) -> None:
        """清空进程内内存缓存；Redis 侧仅删除本模块已知 key 前缀。"""
        with self._lock:
            self._memory.clear()
        if self._redis is not None:
            try:
                for key in self._redis.keys(f"{_KEY_PREFIX}:*"):
                    self._redis.delete(key)
            except Exception:
                pass


# 模块级单例：router 直接复用，测试可注入假 Redis / 调用 delete 隔离。
quote_cache = QuoteCache()


def get_ranking(market: str, metric: str) -> dict | None:
    """快捷入口：读取排行缓存。"""
    return quote_cache.get(market, metric)


def set_ranking(market: str, metric: str, payload: dict, ttl: int | None = None) -> bool:
    """快捷入口：回填排行缓存。"""
    return quote_cache.set(market, metric, payload, ttl=ttl)


def delete_ranking(market: str, metric: str) -> None:
    """快捷入口：删除排行缓存。"""
    quote_cache.delete(market, metric)
