# -*- coding: utf-8 -*-
"""引用 URL 状态校验（S1）：把"明确失效"与"暂时访问不到"分开。

为什么要改：旧实现把 403、429、超时、代理错误一律判 `dead`，而编排器据此**删除候选**——
搜索刚找到的材料被本机环境（代理、限流、抖动）再次清空；报告也会把"访问不到"写成"链接失效"。
专项 §5 要求 URL 状态至少区分下列五类，并且：

- 404/410 才算明确失效（可据此剔除候选）；
- 403/429/HEAD 不支持/DNS/超时 **保留候选与原因**，但不能当作已取得证据；
- 策略拒绝（本项目网络策略）仍然禁止抓取；
- 合法且受同一预算的 GET 可替代不被支持的 HEAD；不为"验证"再下载一遍已有完整文件。

请求一律走项目唯一网络策略 `net_policy.fetch_document`：协议/公网地址校验、**用已验 IP 连接**
（防 DNS 重绑定）、不跟随重定向、响应体有上限、不带调用方凭据。因此这里没有第二套边界，
也不需要自己拼 HTTP 请求。代价是校验用 GET（策略路径不支持 HEAD），故只对**待引用的少数候选**
调用，且同一 URL 不重复校验。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

_MAX_WORKERS = 8
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# 状态取值（专项 §5）。`not_found` 是唯一可以据此剔除候选的状态。
STATES = ("reachable", "not_found", "inaccessible", "unknown", "policy_blocked")


def _state_of_http(code: int) -> str:
    if 200 <= int(code) < 400:
        return "reachable"
    if int(code) in (404, 410):
        return "not_found"
    if int(code) in (401, 403, 407, 429):
        return "inaccessible"
    if int(code) >= 500:
        return "unknown"          # 服务端错误：暂时性，保留候选
    return "unknown"


def _state_of_fetch_error(exc: BaseException) -> str:
    """抓取失败 → 状态未知（保留候选）；不因一次失败判"失效"。"""
    return "unknown"


def probe_url(url: str, timeout: float = 8.0) -> str:
    """单次探测，返回 STATES 之一。

    边界不过（非 http(s)/私网/解析失败）→ `policy_blocked`：不重试、不改走直连。
    """
    try:
        import net_policy
    except Exception:
        return "unknown"
    try:
        resp = net_policy.fetch_document(url, timeout=timeout, max_bytes=256 * 1024)
    except net_policy.NetworkPolicyError:
        return "policy_blocked"
    except Exception as exc:                          # noqa: BLE001 - 含 FetchError 与网络异常
        return _state_of_fetch_error(exc)
    try:
        return _state_of_http(int(resp.get("status") or 0))
    except Exception:
        return "unknown"


def check_urls(
    urls,
    timeout: float = 8,
    max_workers: int = _MAX_WORKERS,
) -> dict[str, str]:
    """批量校验 URL，返回 {url: state}（state ∈ STATES）。

    并发最多 8 线程；同一 URL 只校验一次；异常静默归类为 `unknown`（保留候选）。
    非 http(s) 与空 URL 直接跳过（不进入结果）。"""
    targets: list[str] = []
    for u in urls or []:
        s = str(u or "").strip()
        if s and _HTTP_URL_RE.match(s) and s not in targets:
            targets.append(s)
    if not targets:
        return {}

    def _check(url: str) -> tuple[str, str]:
        try:
            return url, probe_url(url, timeout)
        except Exception:                            # noqa: BLE001
            return url, "unknown"

    out: dict[str, str] = {}
    workers = max(1, min(max_workers, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for url, state in pool.map(_check, targets):
            out[url] = state
    return out


def is_definitely_gone(state: str) -> bool:
    """只有 404/410 属于"明确失效"，可据此剔除候选。"""
    return str(state or "") == "not_found"


def is_unverified(state: str) -> bool:
    """暂时访问不到/状态未知/策略拒绝：保留候选，但不得当作已取得证据。"""
    return str(state or "") in ("inaccessible", "unknown", "policy_blocked")
