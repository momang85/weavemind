# -*- coding: utf-8 -*-
"""引用 URL 存活校验（V1.2 竞品启示：社区最大痛点=引用链接失效）。

用 urllib HEAD/GET 批量探测 URL：
- ThreadPoolExecutor 并发（最多 8 线程）；
- 单请求超时 5s（可调），重试 1 次；
- HEAD 被拒（405/501）自动降级 GET；
- 所有异常静默返回 dead，绝不抛给调用方（任务主线不受影响）。
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_MAX_WORKERS = 8
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _probe(url: str, timeout: float) -> bool:
    """单次探测：HEAD 优先，被拒则 GET；返回是否存活（2xx/3xx）。"""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = getattr(resp, "status", None)
                if code is None:
                    code = getattr(resp, "code", None)
                if code is not None and 200 <= int(code) < 400:
                    return True
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (405, 501):
                continue  # HEAD 不被支持 → 降级 GET 再试一次
            return False
        except Exception:
            return False
    return False


def check_urls(
    urls,
    timeout: float = 5,
    max_workers: int = _MAX_WORKERS,
) -> dict[str, str]:
    """批量校验 URL 存活，返回 {url: 'alive'|'dead'}。

    超时 5s、并发最多 8 线程、重试 1 次；所有异常静默返回 dead。
    非 http(s) 与空 URL 直接跳过（不进入结果）。"""
    targets: list[str] = []
    for u in urls or []:
        s = str(u or "").strip()
        if s and _HTTP_URL_RE.match(s) and s not in targets:
            targets.append(s)
    if not targets:
        return {}

    def _check(url: str) -> tuple[str, str]:
        alive = False
        try:
            # 重试 1 次（HEAD/GET 降级在 _probe 内部处理）
            alive = _probe(url, timeout) or _probe(url, timeout)
        except Exception:
            alive = False
        return url, "alive" if alive else "dead"

    out: dict[str, str] = {}
    workers = max(1, min(max_workers, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for url, state in pool.map(_check, targets):
            out[url] = state
    return out
