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

import io
import logging
import socket
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

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
    tried_keys: set = field(default_factory=set)    # 本轮**已打出去**的 (提供方,后端,查询)
    overrun_calls: int = 0                          # 超预算返回的调用数（结果不计入）
    overrun_seconds: float = 0.0                    # 最严重一次超出多少秒
    refused_calls: int = 0                          # 因剩余时间低于可行下限而**没发出去**的请求数

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
            "submitted": len(self.tried_keys or ()),
            "overrun_calls": self.overrun_calls,
            "overrun_seconds": self.overrun_seconds,
            "refused_calls": self.refused_calls,
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
        # 不再有 1.0 秒下限：短预算（如 0.1 秒）必须如实传给 provider，
        # 否则"剩 0.05 秒也给 1 秒"等于没有硬截止（指令 §3.3 反例）
        return max(0.05, self.time_left())


def _classify(exc: BaseException) -> str:
    """异常 → 类别（与 S0 同一套；判不出归 parse_error）。"""
    # 不可终止的路径是**拒绝**，不是超时、也不是解析错误：必须在委派 `search_diag`
    # 之前判（它是按文本判的，"…socket deadline…"这类描述会被它归到别处；而重试
    # 只会再发一次不可取消的请求）。
    if isinstance(exc, UnboundedReadError):
        return "refused_unbounded"
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
# 超预算容差：调度抖动允许的秒数；超过即判"超时返回"，结果不计入
OVERRUN_TOLERANCE = 0.25

# ── 受控的可终止边界（H1）────────────────────────────────────────────────────
# 同步 SDK / urllib 没法中途取消，所以剩余截止必须落到**请求自己的参数**上：
# 1) 本次请求的超时 = min(提供方上限, 剩余时间)——**不加任何下限**。任何高于剩余时间的
#    下限（旧的 Bing 固定 12/15s、ddgs 的 `max(3.0, …)`）都等于没有硬截止；
# 2) 剩余时间低于该提供方的可行下限时**不发请求**（明确拒绝这条无法取消的有界路径），
#    而不是发出去再等它超时返回，更不起"超时后继续出网"的后台线程。
MIN_VIABLE_CALL_SECONDS = 0.05
PROVIDER_TIMEOUT_CAPS = {"bing": 12.0, "ddgs": 8.0}
# 可行下限只给**无法取消**的提供方抬高：ddgs 是同步 SDK，剩余不到 1 秒时发出去只会
# 超预算返回；Bing 走 read_with_deadline（块间可切断），所以不抬下限、只用 MIN_VIABLE。
PROVIDER_MIN_WAIT = {"ddgs": 1.0}


def provider_timeout(provider: str, wait) -> float:
    """本次请求自己的超时：`min(提供方上限, 剩余时间)`，**不做下限抬升**。"""
    cap = float(PROVIDER_TIMEOUT_CAPS.get(str(provider), 8.0))
    try:
        left = float(wait)
    except (TypeError, ValueError):
        left = cap
    return max(0.0, min(cap, left))


def provider_min_wait(provider: str, declared=None) -> float:
    """该提供方**可行**的剩余时间下限；低于它就不发请求（含提供方自己声明的下限）。"""
    if declared not in (None, ""):
        try:
            return max(MIN_VIABLE_CALL_SECONDS, float(declared))
        except (TypeError, ValueError):
            pass
    return max(MIN_VIABLE_CALL_SECONDS,
               float(PROVIDER_MIN_WAIT.get(str(provider), MIN_VIABLE_CALL_SECONDS)))


def _bound_single_read(resp, seconds: float) -> bool:
    """把**剩余时间**落到响应自己的 socket 上，让**单次** `read()` 也在预算内。

    `read_with_deadline` 原来只在**块间**查时钟——而"块间有空档"这个前提不总成立：
    一次 `read()` 本身就可能把预算用光（连接卡住、慢速连续字节、服务端迟迟不发）。
    传剩余时间只是**必要条件**（真正可终止要有可中断的边界）：能设就设，设不了
    如实返回 False，由调用方按"这个提供方在这条路径上不可终止"处置。

    尽力而为的路径链：`resp.fp.raw._sock` → `resp.fp.raw` → `resp.fp` → `resp`。
    """
    seconds = max(float(seconds), 1e-3)          # 0 表示非阻塞，会把正常读变成空转
    for attrs in (("fp", "raw", "_sock"), ("fp", "raw"), ("fp",), ()):
        obj = resp
        for a in attrs:
            obj = getattr(obj, a, None)
            if obj is None:
                break
        if obj is None:
            continue
        fn = getattr(obj, "settimeout", None)
        if not callable(fn):
            continue
        try:
            fn(seconds)
            return True
        except Exception:                        # noqa: BLE001 - 换下一条路径
            continue
    return False


class ResponseTooLarge(RuntimeError):
    """响应体超过**调用方给的字节上限**：已停止读取并放掉连接（不再整段读进内存）。

    与"超预算"分开：这是**内容规模**问题，不是时间问题；调用方据此报 too_large，
    而不是把它记成超时或网络故障。
    """


class UnboundedReadError(RuntimeError):
    """这条读取路径**无法保证有界**：既设不上 socket 超时，也没有"单次读"语义。

    调用方必须**明确拒绝**这条路（记 `refused_budget` 一类），不得发出去再等它自己返回——
    那等于没有截止线（D-② 裁决：不可保证有界的 provider 明确拒绝，不留后台外呼线程）。
    """


def _has_read1(resp) -> bool:
    """响应体是否支持"单次读"语义（`read1`）。

    `read1(n)` 最多触发**一次**底层读，拿到多少返回多少；而 `read(n)` 会一直循环到
    读满 n 字节或 EOF。后者在"对端每 5ms 发一个字节"时会把缓冲循环拖到对端结束
    （实测：预算 0.02s、0.1076s 才回来）——所以优先用 `read1`，并在循环里按时钟叫停。
    """
    return callable(getattr(resp, "read1", None))


def _close_quietly(resp) -> None:
    """到点后把连接放掉（尽力而为）：不留下还在出网的响应体。"""
    fn = getattr(resp, "close", None)
    if callable(fn):
        try:
            fn()
        except Exception:                        # noqa: BLE001 - 关闭失败不影响"已停止"
            pass


class _DeadlineRaw(io.RawIOBase):
    """把"原响应体"包成一个**每次底层读都查钟**的 raw 流。

    为什么必须做到"每次底层读"（Q0 反例）：chunked 响应的分块头由
    `HTTPResponse._get_chunk_left() → fp.readline()` 读取，而 `readline` 是**一次调用、
    内部连续 recv 直到遇到换行**——外层"每次 `read1` 返回后再查钟"只能等它读完才轮到。
    进程内 socketpair + **真实** `http.client.HTTPResponse` 实测（预算 0.02s）：

    | 形状 | 旧实现 | 现在 |
    |---|---|---|
    | 分块头 6 字节、每 5ms 一字节 | 0.0269s | 0.0216s |
    | 分块头 6 字节、每 20ms 一字节 | **0.1022s** | ≤ 预算 + 容差 |
    | 分块头 60 字节、每 5ms 一字节 | **0.3182s** | ≤ 预算 + 容差 |

    做法：每次 `readinto` 只向**原 fp** 要一次数据（`read1`，最多一次底层读），
    读之前设剩余 socket 超时、读之后查钟——于是"连续 recv"被拆成"每次 recv 之间都有
    一次时钟检查"。**从原 fp 读**而不是从 socket 读：`begin()` 解析响应头时
    `BufferedReader` 可能已经把正文预读进自己的缓冲，绕过它会把已到的正文丢掉。
    """

    def __init__(self, fp, deadline: float, *, clock=time.monotonic):
        super().__init__()
        self._fp = fp
        self._deadline = float(deadline)
        self._clock = clock

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        remain = self._deadline - self._clock()
        if remain <= 0:
            raise TimeoutError("read deadline exceeded (before a single read)")
        _bound_single_read(self._fp, remain)      # 单次读的硬边界（尽力而为）
        data = self._fp.read1(len(b))
        if self._clock() >= self._deadline:
            # 读回来了但已经越界：这一读不算数（调用方按超时处理并放掉连接）
            raise TimeoutError("read deadline exceeded (mid-read)")
        n = len(data or b"")
        if n:
            b[:n] = data
        return n

    def read(self, size: int = -1) -> bytes:
        """`RawIOBase.read` 默认实现会循环 `readinto`；这里保持它（循环里逐次受检）。"""
        return super().read(size)

    def close(self) -> None:
        try:
            self._fp.close()
        finally:
            super().close()


def _wrap_response_deadline(resp, deadline: float, *, clock=time.monotonic):
    """给 `resp.fp` 套上截止线 → `(是否套上, 还原函数)`。

    套不上（没有 fp / 只读属性 / 已是包装 / 不是可包装的流）就返回 False：
    调用方仍按原有 `read1`+时钟检查走，绝不因为"套不上"而放宽判据。
    """
    try:
        fp = getattr(resp, "fp", None)
    except Exception:                                 # noqa: BLE001
        return False, (lambda: None)
    if fp is None or isinstance(fp, (io.RawIOBase, _DeadlineRaw)) \
            or not callable(getattr(fp, "read1", None)):
        return False, (lambda: None)
    # 已经包过截止线（如 `net_policy` 在 `begin()` 之前先包了头解析）→ 不重复套一层：
    # 两层都查同一个绝对截止，套两次只是白白多一层缓冲。
    try:
        inner = getattr(fp, "raw", None)
        if isinstance(inner, _DeadlineRaw):
            return True, (lambda: None)
    except Exception:                                 # noqa: BLE001 - 判断失败按未包处理
        pass
    try:
        wrapped = io.BufferedReader(_DeadlineRaw(fp, deadline, clock=clock))
        setattr(resp, "fp", wrapped)
    except Exception:                                 # noqa: BLE001
        return False, (lambda: None)

    def _restore() -> None:
        try:
            if isinstance(getattr(resp, "fp", None), io.BufferedReader) \
                    and isinstance(getattr(resp.fp, "raw", None), _DeadlineRaw):
                setattr(resp, "fp", fp)
        except Exception:                             # noqa: BLE001
            pass

    return True, _restore


def read_with_deadline(resp, deadline: float, chunk: int = 65536,
                       *, clock=time.monotonic, require_bounded: bool = True,
                       max_bytes: int | None = None) -> bytes:
    """按块读取响应，并在**块间**与**块内**都守住同一个墙钟截止；到点即停止读取。

    socket timeout 只管单次操作，慢速分块响应可以每块都小于 timeout、整体却远超截止
    （"慢读取"反例）。这里给出真正的总墙钟边界，不新起线程：到点抛 `TimeoutError`，
    由调用方按超时如实记账，不再继续读。

    **块内**也要守（2026-09-28 反例）：只在块间查时钟时，若**第一次** `read()` 本身就把
    预算用光（实测：预算 0.02 秒、单次 read 耗 0.12 秒）并恰好返回 EOF，循环会直接
    `break` 并把空正文当**成功**返回——"到点停"变成"返回后才说停"。因此：

    - **单次读用 `read1`**（最多一次底层读）：这是"块内可中断"的真正前提。D-② 复核反例：
      进程内 socket pair、对端每 5ms 发一字节、预算 0.02s，`read()` 的缓冲循环要到对端
      结束才回来（实测 **0.1076s** 才抛），而 `read1` 每次几毫秒就返回，循环里的时钟检查
      因此能在预算内叫停；
    - 每次读之前把**剩余时间**设到响应自己的 socket 上（单次读的硬边界，尽力而为）；
    - 每次读**之后**再查一次时钟：超了就抛 `TimeoutError`，**EOF 也不例外**
      （EOF 越界不是"读完了"，是"没读完就到点了"）；
    - 两条边界都给不了（没 `read1` 且设不上超时）时，`require_bounded=True` 直接
      `UnboundedReadError`——**拒绝**这条不可终止的路，而不是发出去再等。

    **Q0 补**：仅靠上面这些仍挡不住 chunked 响应的**慢分块头**——分块头是
    `fp.readline()` 一次调用内部连续 recv 读出来的，外层查钟只在它返回之后才轮到
    （实测预算 0.02s、分块头逐字节到达要 **0.1022s / 0.3182s** 才抛）。所以这里再把
    `resp.fp` 包一层 `_DeadlineFile`，让时钟检查落进**每一次底层读**之间。
    """
    buf: list[bytes] = []
    use_read1 = _has_read1(resp)
    bounded_sock = False
    _wrapped, _restore = _wrap_response_deadline(resp, deadline, clock=clock)
    try:
        while True:
            remain = float(deadline) - clock()
            if remain <= 0:
                _close_quietly(resp)
                raise TimeoutError("read deadline exceeded (slow response body)")
            if _bound_single_read(resp, remain):
                bounded_sock = True
            if not (use_read1 or bounded_sock) and require_bounded:
                _close_quietly(resp)
                raise UnboundedReadError(
                    "this response has no boundable read path (no read1 / no settable "
                    "socket deadline): refusing an unbounded outbound call")
            try:
                block = resp.read1(chunk) if use_read1 else resp.read(chunk)
            except (socket.timeout, TimeoutError) as exc:
                _close_quietly(resp)
                raise TimeoutError(
                    f"read deadline exceeded ({type(exc).__name__})") from exc
            over = clock() >= float(deadline)
            if not block:
                if over:
                    # EOF **越界**：不能当成"正常读完"
                    _close_quietly(resp)
                    raise TimeoutError("read deadline exceeded at EOF "
                                       "(body ended after the deadline)")
                break
            buf.append(block)
            if max_bytes and sum(len(b) for b in buf) > int(max_bytes):
                _close_quietly(resp)
                raise ResponseTooLarge(
                    f"响应体超过上限 {int(max_bytes)} 字节（已停止读取）")
            if over:
                # 已经读过截止还拿到了数据：正文不完整，不得当成功返回
                _close_quietly(resp)
                raise TimeoutError("read deadline exceeded (slow response body)")
        return b"".join(buf)
    finally:
        _restore()
        del _wrapped


# 启动前的预占/状态变量

# ddgs 在"引擎跑完了但一条也没找到"时也抛异常（`DDGSException("No results found.")`）。
# 那不是提供方故障：调用方应把它当**完成但零命中**（返回空列表），否则真零结果会被
# 记成 parse_error，进而被当作后端故障熔断——专项 §5 明确要求"真零结果不熔断"。
_EMPTY_RESULT_MARKERS = ("no results found", "no result found")


def is_empty_result_error(exc: BaseException) -> bool:
    """该异常是否只表示"查询完成、零命中"（而不是没完成查询）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(m in text for m in _EMPTY_RESULT_MARKERS)


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
    exclude_keys=(),
) -> SearchOutcome:
    """按预算跑一轮检索。

    `call_provider(provider, backend, query, wait) -> list[dict]` 由调用方注入
    （worker 注入 ddgs/Bing 的真实调用；测试注入替身）。**不传 backend=None**：
    每个提供方只用显式指定的单一后端，避免 auto 全引擎串行。

    `exclude_keys`：**已经打出去过**的 `(提供方, 后端, 查询)` 组合（取自上一轮
    `outcome.tried_keys`）。任务内的重试因此不可能把同一组合再提交一次——专项 §5
    "契约存在时不能再次提交相同 query/provider 组合"。
    """
    budget = budget or SearchBudget()
    outcome = SearchOutcome(provider=providers[0].get("provider", "") if providers else "",
                            backend=providers[0].get("backend", "") if providers else "")
    t0 = time.monotonic()
    queries = dedupe_queries(queries)
    collected: list[dict] = []
    seen_urls: set[str] = set()
    tried: set[tuple[str, str, str]] = set(exclude_keys or ())
    submitted: set[tuple[str, str, str]] = set()
    any_call_ok = False
    overran = False
    dominant_error = ""

    for query in queries:
        for spec in providers or []:
            provider = str(spec.get("provider") or "")
            backend = str(spec.get("backend") or "")
            key = (provider, backend, query)
            if key in tried:
                continue                      # 去重：同 (查询,后端) 不重复提交
            if budget.expired():
                outcome.reason = "检索预算用尽（到点或到次数），停止继续尝试"
                break
            # 受控的可终止边界：剩余时间不够这个提供方跑一次，就**一个请求都不发**
            # （同时不消耗调用额度），而不是发出去再靠 socket 超时兜底。
            min_wait = provider_min_wait(provider, spec.get("min_wait"))
            left = budget.time_left()
            if left < min_wait:
                outcome.refused_calls += 1
                outcome.reason = outcome.reason or (
                    f"剩余 {left:.2f}s 低于 {provider} 可行下限 {min_wait:.2f}s，"
                    f"拒绝发出该请求（同步调用无法取消）")
                logger.warning("provider %s 剩余 %.2fs < 可行下限 %.2fs，本次不发请求",
                               provider, left, min_wait)
                continue
            try:
                wait = budget.take()
            except RuntimeError:
                break
            tried.add(key)
            submitted.add(key)
            outcome.attempts += 1
            outcome.queries_tried.append(query)
            t_call = time.monotonic()
            try:
                items = call_provider(provider, backend, query, wait) or []
            except Exception as exc:           # noqa: BLE001 - 单提供方失败不终止整轮
                cls = _classify(exc)
                if cls == "no_results":
                    # 查询完成但零命中：记"完成"，不进错误表（否则真零结果会被熔断）
                    any_call_ok = True
                    continue
                outcome.errors.setdefault(provider, cls)
                dominant_error = dominant_error or cls
                if cls in RETRYABLE:
                    outcome.retryable = True
                continue
            elapsed_call = time.monotonic() - t_call
            # **真实停止**（指令 §3.3）：同步 SDK 没法中途取消，但"超时返回的结果"不许
            # 当成功——超预算返回的条目一律不入库，并如实记超时次数与超出量。
            if elapsed_call > wait + OVERRUN_TOLERANCE or budget.time_left() <= 0:
                outcome.overrun_calls += 1
                outcome.overrun_seconds = round(
                    max(outcome.overrun_seconds, elapsed_call - wait), 3)
                overran = True
                logger.warning("provider %s 超预算返回（%.2fs > %.2fs），本条不计入结果",
                               provider, elapsed_call, wait)
                continue
            any_call_ok = True                  # 预算内正常返回才记"完成"
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

    outcome.tried_keys = submitted
    outcome.items = collected[:max_results]

    outcome.elapsed = time.monotonic() - t0
    if collected:
        outcome.status = "ok" if len(collected) >= enough else "partial"
        if outcome.status == "partial":
            outcome.reason = outcome.reason or f"仅命中 {len(collected)} 条（少于期望 {enough} 条）"
    elif any_call_ok:
        outcome.status = "no_results"
        outcome.reason = outcome.reason or "查询成功但零命中（正常没找到，不是后端故障）"
    elif outcome.overrun_calls:
        outcome.status = "timeout"
        outcome.reason = (f"{outcome.overrun_calls} 次调用超预算返回（最多超出 "
                          f"{outcome.overrun_seconds}s），结果不计入")
    elif outcome.refused_calls:
        # 到点后没有再发请求：这不是"查询失败"，是**按截止拒绝出发**——如实记 timeout
        outcome.status = "timeout"
        outcome.reason = outcome.reason or (
            f"{outcome.refused_calls} 次请求因剩余时间低于可行下限被拒绝，未发出")
    else:
        outcome.status = dominant_error or "parse_error"
        outcome.reason = outcome.reason or f"全部提供方未完成查询（{outcome.errors}）"
    return outcome
