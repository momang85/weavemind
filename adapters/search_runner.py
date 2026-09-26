# -*- coding: utf-8 -*-
"""有界检索执行器（S1）：一次任务只用一个预算、一条截止线、最多一个备后端。

专项 §5 的收敛对象是"9 引擎 × 多变体 × 二轮 × 编排重试"这个放大链（实机：检索 9 分钟、
12 个查询、31 次尝试）。本模块把检索收敛成一个小入口：

- **共享预算**：`deadline`（单调时钟）与 `max_calls`（提供方调用次数）贯穿所有变体与提供方；
  到点或到次数即停，不遗留后台检索。
- **不用 auto、不做全后端重扫**：每个提供方只打**显式指定的单个后端**；首个真零结果不等于
  后端故障，因此绝不因为"没结果"改走 `backend=None`（那会让 ddgs 串行扫全部引擎）。
- **真零结果不熔断**：查询成功但零命中记 `no_results`，与"没完成查询"（超时/DNS/被拒…）分开。
- **去重**：同一 (查询, 后端) 组合只打一次；契约查询不会被原样重复提交。
- **结果协议**：`status/items/attempts/elapsed/reason/retryable/provider/backend`，
  并给旧调用方一个明确的数组兼容层（`to_legacy_items()`）。

纯适配器：只用标准库与被注入的"执行函数"，不 import Redis/LLM，便于离线验证。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# 新人默认：检索阶段总墙钟 ≤60 秒、提供方调用 ≤6 次（专项 §5）
DEFAULT_DEADLINE_SECONDS = 60.0
DEFAULT_MAX_CALLS = 6
# 结果类别（与 S0 诊断同一套命名）
STATUSES = ("ok", "no_results", "no_relevant_results", "partial",
            "missing_dependency", "not_configured", "proxy_error", "dns_error",
            "tls_error", "timeout", "policy_blocked", "auth_error", "rate_limited",
            "challenge", "parse_error")


@dataclass
class SearchOutcome:
    """一次有界检索的结果协议。旧调用方用 `to_legacy_items()` 拿数组。"""

    status: str = "no_results"
    items: list = field(default_factory=list)
    attempts: int = 0
    elapsed: float = 0.0
    reason: str = ""
    retryable: bool = False
    provider: str = ""
    backend: str = ""
    errors: dict = field(default_factory=dict)      # provider -> 错误类别（首次/最严重）
    queries_tried: list = field(default_factory=list)

    def to_legacy_items(self) -> list:
        """兼容层：旧输出契约是 JSON 数组（`[{title,url,snippet}...]`）。"""
        return list(self.items or [])

    def as_dict(self) -> dict:
        return {
            "status": self.status, "items": len(self.items or []),
            "attempts": self.attempts, "elapsed": round(self.elapsed, 2),
            "reason": self.reason, "retryable": self.retryable,
            "provider": self.provider, "backend": self.backend,
            "errors": dict(self.errors), "queries_tried": list(self.queries_tried),
        }


class SearchBudget:
    """调用次数 + 单调时钟截止，二者共用；到点后不再发新请求。"""

    def __init__(self, max_calls: int = DEFAULT_MAX_CALLS,
                 deadline_seconds: float = DEFAULT_DEADLINE_SECONDS):
        self.max_calls = max(1, int(max_calls))
        # 下限 0.1s：只是防止零/负值，不放大调用方给的短截止（测试与"到点即停"都依赖它）
        self.deadline = time.monotonic() + max(0.1, float(deadline_seconds))
        self.used = 0

    def time_left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return self.used >= self.max_calls or self.time_left() <= 0

    def take(self) -> float:
        """占用一次调用，返回该次可用秒数（不超过剩余时间）。"""
        if self.expired():
            raise RuntimeError("search budget exhausted")
        self.used += 1
        return max(1.0, self.time_left())


def _classify(exc: BaseException) -> str:
    """异常 → 类别（与 S0 同一套；判不出归 parse_error）。"""
    try:
        import search_diag
        return search_diag.classify_error(exc)
    except Exception:
        pass
    name = type(exc).__name__
    text = f"{name}: {exc}".lower()
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return "missing_dependency"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "getaddrinfo" in text or "nodename" in text:
        return "dns_error"
    return "parse_error"


# 可重试类别：暂时性失败才允许一次退避重试（遵守截止，不换关键词硬刷）
RETRYABLE = ("timeout", "dns_error", "proxy_error", "rate_limited")


def dedupe_queries(queries) -> list[str]:
    """查询去重（保持顺序、去空白、去完全相同项）。"""
    out: list[str] = []
    seen: set[str] = set()
    for q in queries or []:
        s = str(q or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def run_search(
    queries,
    *,
    call_provider,
    providers,
    budget: SearchBudget | None = None,
    max_results: int = 6,
    enough: int = 3,
) -> SearchOutcome:
    """按预算跑一轮检索。

    `call_provider(provider, backend, query, wait) -> list[dict]` 由调用方注入
    （worker 注入 ddgs/Bing 的真实调用；测试注入替身）。**不传 backend=None**：
    每个提供方只用显式指定的单一后端，避免 auto 全引擎串行。
    """
    budget = budget or SearchBudget()
    outcome = SearchOutcome(provider=providers[0].get("provider", "") if providers else "",
                            backend=providers[0].get("backend", "") if providers else "")
    t0 = time.monotonic()
    queries = dedupe_queries(queries)
    collected: list[dict] = []
    seen_urls: set[str] = set()
    tried: set[tuple[str, str, str]] = set()
    any_call_ok = False
    dominant_error = ""

    for query in queries:
        for spec in providers or []:
            provider = str(spec.get("provider") or "")
            backend = str(spec.get("backend") or "")
            key = (provider, backend, query)
            if key in tried:
                continue                      # 去重：同 (查询,后端) 不重复提交
            tried.add(key)
            if budget.expired():
                outcome.reason = "检索预算用尽（到点或到次数），停止继续尝试"
                break
            try:
                wait = budget.take()
            except RuntimeError:
                break
            outcome.attempts += 1
            outcome.queries_tried.append(query)
            try:
                items = call_provider(provider, backend, query, wait) or []
                any_call_ok = True
            except Exception as exc:           # noqa: BLE001 - 单提供方失败不终止整轮
                cls = _classify(exc)
                outcome.errors.setdefault(provider, cls)
                dominant_error = dominant_error or cls
                if cls in RETRYABLE:
                    outcome.retryable = True
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                u = str(it.get("url") or "").strip()
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    collected.append(it)
            if len(collected) >= max_results:
                break
        if len(collected) >= max_results or budget.expired():
            break

    outcome.items = collected[:max_results]
    outcome.elapsed = time.monotonic() - t0
    if collected:
        outcome.status = "ok" if len(collected) >= enough else "partial"
        if outcome.status == "partial":
            outcome.reason = outcome.reason or f"仅命中 {len(collected)} 条（少于期望 {enough} 条）"
    elif any_call_ok:
        outcome.status = "no_results"
        outcome.reason = outcome.reason or "查询成功但零命中（正常没找到，不是后端故障）"
    else:
        outcome.status = dominant_error or "parse_error"
        outcome.reason = outcome.reason or f"全部提供方未完成查询（{outcome.errors}）"
    return outcome
