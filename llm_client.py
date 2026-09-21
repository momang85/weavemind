"""
织光 (ZhiGuang) — LLM 调用客户端

基于 OpenAI 兼容 API (/v1/chat/completions)，支持任意兼容服务。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

from common import extract_json_object, loads_loose

# Redis 客户端统一关掉 redis-py 的内建重试：默认重试会把 socket_connect_timeout
# 叠成 26~48 秒才失败（实测 127.0.0.1 26s / localhost 48s），Redis 不在时
# 表现为"服务没崩但处处卡"。NoBackoff + 0 次重试 → 稳定 2 秒内失败并可被上层降级。
from redis.backoff import NoBackoff as _NoBackoff  # noqa: E402
from redis.retry import Retry as _Retry  # noqa: E402

_NO_REDIS_RETRY = _Retry(_NoBackoff(), 0)

logger = logging.getLogger(__name__)


# ============================================================================
# Token 用量统计（可观测性）
# ============================================================================

_usage_lock = threading.Lock()
_usage = {
    "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
    # B3：按端点（base_url）分账台账，供 /api/metrics 与账单告警
    "by_endpoint": {},
}
_usage_pub_client = None
_task_usage_client = None
_task_ctx = None
_endpoint_health = {
    "primary": {"healthy": True, "fails": 0,
                "last_degradation_reason": "", "last_degradation_ts": 0},
    "backup": {"healthy": True, "fails": 0,
               "last_degradation_reason": "", "last_degradation_ts": 0},
}
_endpoint_health_lock = threading.Lock()
_auth_error_lock = threading.Lock()
_last_auth_error = {"ts": 0.0, "message": ""}
_health_monitor_started = False
_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_cfg_mtime: float | None = None
_cfg_lock = threading.Lock()


# ---------------------------------------------------------------------------
# B1 模型分级路由：调用用途 -> llm.model_roles 配置键
# planner = 规划/反思/评审；exec = 步骤执行；judge = 评测
# ---------------------------------------------------------------------------
MODEL_ROLES: dict[str, str] = {
    "plan": "planner",
    "reflect": "planner",
    "review": "planner",
    "exec": "exec",
    "judge": "judge",
}
# llm.model_roles 配置（config.json 热重载时刷新），如 {"planner": "deepseek-chat", ...}
_MODEL_ROLES_CFG: dict[str, str] = {}

# B2 LLM 调用缓存：Redis 客户端（测试可替换为假客户端）
_llm_cache_client = None


def _task_context_var():
    global _task_ctx
    if _task_ctx is None:
        import contextvars
        _task_ctx = contextvars.ContextVar("weavemind_task_id", default="")
    return _task_ctx


def set_task_context(task_id: str) -> None:
    """设置当前调用所属任务（Worker 处理任务时调用），用于每任务 token 台账。"""
    try:
        _task_context_var().set(str(task_id or ""))
    except Exception:
        pass


def clear_task_context() -> None:
    try:
        _task_context_var().set("")
    except Exception:
        pass


def get_task_context() -> str:
    try:
        return _task_context_var().get()
    except Exception:
        return ""


def _apply_cfg_to_env() -> None:
    """把 config.json 的 llm/embedding/backup 段重新应用到 os.environ。
    修复"前端改端点，后端进程仍用旧端点"：各进程在调用前按 mtime 热重载。"""
    global _BACKUP_CFG, _MODEL_ROLES_CFG
    try:
        with open(_CFG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return
    llm = cfg.get("llm") or {}
    if llm.get("api_key"):
        os.environ["LLM_API_KEY"] = str(llm["api_key"])
    if llm.get("base_url"):
        os.environ["LLM_BASE_URL"] = str(llm["base_url"])
    if llm.get("model"):
        os.environ["LLM_MODEL"] = str(llm["model"])
    # B1：模型分级路由表（缺省回退 llm.model）
    _MODEL_ROLES_CFG = {
        str(k): str(v)
        for k, v in (llm.get("model_roles") or {}).items()
        if v
    }
    emb = cfg.get("embedding") or {}
    if emb.get("api_key"):
        os.environ["EMBEDDING_API_KEY"] = str(emb["api_key"])
    if emb.get("base_url"):
        os.environ["EMBEDDING_BASE_URL"] = str(emb["base_url"])
    if emb.get("model"):
        os.environ["EMBEDDING_MODEL"] = str(emb["model"])
    b = cfg.get("backup") or {}
    _BACKUP_CFG = dict(b) if b.get("base_url") and b.get("api_key") else {}
    pl = cfg.get("planner") or {}
    if pl.get("base_url"):
        os.environ["PLANNER_LLM_BASE_URL"] = str(pl["base_url"])
    if pl.get("api_key"):
        os.environ["PLANNER_LLM_API_KEY"] = str(pl["api_key"])
    if pl.get("model"):
        os.environ["PLANNER_LLM_MODEL"] = str(pl["model"])


def _ensure_cfg_fresh() -> None:
    """config.json 变更热重载（mtime 检测，进程内生效，无需重启）。"""
    global _cfg_mtime, _default_client
    try:
        m = os.path.getmtime(_CFG_PATH)
    except Exception:
        return
    with _cfg_lock:
        if _cfg_mtime is None:
            _cfg_mtime = m
            _apply_cfg_to_env()  # 首次调用即同步（自愈已运行进程）
            return
        if m == _cfg_mtime:
            return
        _cfg_mtime = m
    _apply_cfg_to_env()
    _default_client = None  # 让 get_default_client() 用新 env 重建
    logger.info("LLM config hot-reloaded from config.json")


# ---------------------------------------------------------------------------
# LLM 端点健康检查与自动切流（O-29，对标标准 C4-4.3 稳定性）
# ---------------------------------------------------------------------------

# 端点失败阈值：连续 N 次失败才标记不健康（env LLM_ENDPOINT_FAIL_THRESHOLD，
# 默认 2）。阈值 1 时一次瞬时抖动（timeout/504/连接重置）即判死端点，
# 主备互切抖动；2 次连续失败能过滤绝大多数瞬时错误。恢复侧已由
# _health_monitor_loop 的"连续 N 次探测成功"防抖（LLM_HEALTH_RESTORE）。
_ENDPOINT_FAIL_THRESHOLD = max(
    1, int(os.environ.get("LLM_ENDPOINT_FAIL_THRESHOLD", "2") or 2)
)


def _mark_endpoint(endpoint: str, ok: bool, reason: str = "") -> None:
    record_degradation = None
    with _endpoint_health_lock:
        st = _endpoint_health.setdefault(
            endpoint,
            {"healthy": True, "fails": 0,
             "last_degradation_reason": "", "last_degradation_ts": 0},
        )
        if ok:
            st["healthy"] = True
            st["fails"] = 0
        else:
            st["fails"] += 1
            st["healthy"] = st["fails"] < _ENDPOINT_FAIL_THRESHOLD
            if reason:
                st["last_degradation_reason"] = reason
                st["last_degradation_ts"] = time.time()
                # P2-3：记录 last_degradation_reason 时同步写入 Redis 任务级
                # 降级记录，保证"主端点由他处标记不健康"也有根因可查
                # （_record_task_degradation 对同因短窗口去重，避免调用点重复记）
                # Redis 写在锁外执行：_record_task_degradation 的连接无
                # socket_timeout，Redis 卡顿时会拖死所有 LLM 调用
                record_degradation = (get_task_context(), reason)
    if record_degradation:
        _record_task_degradation(record_degradation[0], record_degradation[1], both_failed=False)


def _degradation_reason(exc: Exception) -> str:
    """把 LLM 调用异常归类为稳定降级原因（供健康路由与任务汇总）。"""
    text = str(exc or "")
    if "Empty content" in text or "Empty choices" in text:
        return "empty_content"
    m = re.search(r"HTTP[ _-]?(\d{3})", text)
    if m and m.group(1) in ("401", "402", "403"):
        return f"HTTP_{m.group(1)}"
    low = text.lower()
    if "network error" in low:
        return "timeout" if ("timeout" in low or "timed out" in low) else "network_error"
    return "generic"


def _record_task_degradation(task_id: str, reason: str, both_failed: bool = False) -> None:
    """把降级事件写入 Redis（llm_degradation:{task_id}），供任务完成汇总。"""
    if not task_id:
        return
    global _task_usage_client
    try:
        if _task_usage_client is None:
            import redis as _redis
            _task_usage_client = _redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2, retry=_NO_REDIS_RETRY,
            )
        key = f"llm_degradation:{task_id}"
        # P2-3：同根因 2 秒内去重（_mark_endpoint 与调用点各记一次），
        # 避免同一失败被计成两条事件
        try:
            _last = _task_usage_client.lindex(key, -1)
            if _last:
                _prev = json.loads(_last)
                if (
                    str(_prev.get("reason") or "") == str(reason or "")
                    and bool(_prev.get("both_failed")) == bool(both_failed)
                    and time.time() - float(_prev.get("ts") or 0) < 2.0
                ):
                    return
        except Exception:
            pass
        _task_usage_client.rpush(key, json.dumps({
            "ts": time.time(),
            "reason": reason,
            "both_failed": both_failed,
        }, ensure_ascii=False))
        _task_usage_client.ltrim(key, -50, -1)
        _task_usage_client.expire(key, 7200)
    except Exception:
        pass


def _error_shape(exc: Exception) -> tuple[int, str]:
    """把调用异常压成**脱敏形状**：`(http_status, error_class)`。

    只看类别与状态码：HTTP 响应体、提示词、密钥都不进诊断记录（`LLMCallError`
    的文本里可能带上游响应片段，不能原样落到诊断/日志里）。
    """
    text = str(exc or "")
    if isinstance(exc, LLMCancelledError):
        return 0, "cancelled"
    if getattr(exc, "budget_exhausted", False):
        return 0, "budget_exhausted"
    if isinstance(exc, LLMJSONParseError):
        return 0, "bad_json"
    # httpx 异常：状态码在 response 上，类名区分超时/连接（文本里往往没有 "HTTP 504"）
    _code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(_code, int) and _code:
        return _code, f"http_{_code}"
    _name = type(exc).__name__.lower()
    if "timeout" in _name:
        return 0, "timeout"
    if "connect" in _name:
        return 0, "network_error"
    m = re.search(r"HTTP[ _-]?(\d{3})", text)
    if m:
        return int(m.group(1)), f"http_{m.group(1)}"
    low = text.lower()
    if "timeout" in low or "timed out" in low:
        return 0, "timeout"
    if "network error" in low:
        return 0, "network_error"
    if "empty content" in low or "empty choices" in low:
        return 0, "empty_content"
    return 0, "generic"


def describe_error(exc: Exception) -> str:
    """给用户可见的进度流/日志用的**脱敏**错误类别（如 `http_500`、`timeout`）。

    提示词与响应体不进这里：`LLMCallError` 的文本可能带上游响应片段，
    原样写进进度流会随 `logs_json` 落库并在界面上显示。
    """
    status, cls = _error_shape(exc)
    if status:
        return f"{cls}(HTTP {status})"
    return cls or type(exc).__name__


# ── 流式传输（SSE）─────────────────────────────────────────
#
# 为什么要有流式：网关（实测响应体里的 alb）在 ~60s 处切断**整段**非流式响应，
# 而报告/总结这类长生成（4096–8192 tokens 的中文）普遍超过这个窗口——于是每次都
# 504、重试、强制重做，任务根本跑不到终态。流式让字节持续到达，网关不再按"整段
# 响应耗时"判超时。
#
# 设计要点：流式只是**传输**层的变化——累积完成后还原成与非流式**同形**的响应体，
# 下游的解析、用量记账、缓存、健康标记、调用形状记录全都不用改。

_STREAM_ENV = "WM_LLM_STREAM"
_STREAM_UNSUPPORTED_CODES = (400, 404, 405, 415, 422)


def _stream_enabled() -> bool:
    """流式开关（默认开）；个别供应商不支持时由调用方回退非流式。"""
    return os.environ.get(_STREAM_ENV, "1") != "0"


def _stream_unsupported(exc: Exception) -> bool:
    """异常是不是"供应商不支持流式"（400/404/405/415/422）——只认这些码，
    其它错误（超时/5xx/鉴权）如实上报，不当成"回退就能好"。"""
    status, _cls = _error_shape(exc)
    return status in _STREAM_UNSUPPORTED_CODES


def _sse_payload(line: str) -> str:
    """从一行 SSE 里取出 data 载荷；非 data 行（注释/空行）返回空串。"""
    s = str(line or "").strip()
    if not s.startswith("data:"):
        return ""
    return s[5:].strip()


def _merge_stream_chunk(acc: dict, chunk: dict) -> None:
    """把一条 SSE chunk 并入累积结果（内容/思考内容/结束原因/用量）。"""
    if not isinstance(chunk, dict):
        return
    if isinstance(chunk.get("usage"), dict) and chunk["usage"]:
        acc["usage"] = chunk["usage"]
    for ch in chunk.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta") or {}
        if delta.get("content"):
            acc["content"].append(str(delta["content"]))
        # 思考模型的 reasoning_content 也要收：否则"思考烧光预算"的判据会失效
        if delta.get("reasoning_content"):
            acc["reasoning"].append(str(delta["reasoning_content"]))
        if ch.get("finish_reason"):
            acc["finish_reason"] = str(ch["finish_reason"])


def _new_stream_acc() -> dict:
    return {"content": [], "reasoning": [], "finish_reason": "", "usage": {}}


def _stream_result(acc: dict) -> dict:
    """累积结果 → 与非流式同形的响应体。"""
    return {
        "choices": [{
            "index": 0,
            "finish_reason": acc.get("finish_reason") or "stop",
            "message": {
                "role": "assistant",
                "content": "".join(acc.get("content") or []),
                "reasoning_content": "".join(acc.get("reasoning") or []),
            },
        }],
        "usage": acc.get("usage") or {},
    }


def _with_stream(body: dict, stream: bool) -> dict:
    """按需加上/去掉流式参数（回退非流式时要去干净）。"""
    out = dict(body)
    if stream:
        out["stream"] = True
        # 用量在流式下要显式索取，否则拿不到 token 数
        out["stream_options"] = {"include_usage": True}
    else:
        out.pop("stream", None)
        out.pop("stream_options", None)
    return out


# ── LLM 端点守卫 ────────────────────────────────────────────
#
# 模型端点地址来自配置/环境（运维写），不是用户输入；但配错就会把提示词与数据
# 发到内网服务（元数据、本机管理口）。因此发请求前按项目既有策略校验：
# 仅 http/https、不得带用户凭据、公网端点解析后**全部**地址必须是公网；
# 本机/私网端点必须由运维显式登记（`network.endpoints`）或显式开关放行。

_ENDPOINT_OK_CACHE: dict[tuple, str] = {}
_ENDPOINT_CACHE_LOCK = threading.Lock()
_LOCAL_ENDPOINT_ENV = "WM_LLM_ALLOW_LOCAL"


def _local_endpoint_allowed() -> bool:
    """本机/私网 LLM 端点是否被显式允许（CI 替身、本机 Ollama/LoRA 用）。

    默认**不允许**：端点写错时不得静默把请求发到内网。
    """
    return os.environ.get(_LOCAL_ENDPOINT_ENV, "0") == "1"


def _registered_hosts() -> set:
    """运维登记表里的 (scheme, host, port)：登记即视为授权（含 loopback 标记的本机服务）。"""
    try:
        import net_policy
        out = set()
        for ep in (net_policy.load_registry() or {}).values():
            out.add((str(ep.scheme).lower(), str(ep.host).lower(), int(ep.port or 0)))
        return out
    except Exception:
        return set()


def _endpoint_guard(url: str) -> str:
    """校验 LLM 端点 URL，返回可用 URL；不通过抛 `LLMCallError`。

    结果按 (scheme, host, port) 缓存：每次调用都解析 DNS 不值得。
    """
    import urllib.parse
    raw = str(url or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except Exception as exc:
        raise LLMCallError(f"LLM 端点无法解析：{str(exc)[:80]}") from exc
    scheme = str(parts.scheme or "").lower()
    host = str(parts.hostname or "").lower().rstrip(".")
    port = int(parts.port or (443 if scheme == "https" else 80))
    if scheme not in ("http", "https"):
        raise LLMCallError(f"LLM 端点协议不允许：{scheme or '(空)'}")
    if parts.username or parts.password:
        raise LLMCallError("LLM 端点 URL 不得携带用户凭据")
    if not host:
        raise LLMCallError("LLM 端点缺少主机名")
    key = (scheme, host, port)
    with _ENDPOINT_CACHE_LOCK:
        if _ENDPOINT_OK_CACHE.get(key):
            return raw
    if key in _registered_hosts():
        with _ENDPOINT_CACHE_LOCK:
            _ENDPOINT_OK_CACHE[key] = raw
        return raw
    if _local_endpoint_allowed():
        with _ENDPOINT_CACHE_LOCK:
            _ENDPOINT_OK_CACHE[key] = raw
        return raw
    try:
        import net_policy
        decision = net_policy.validate_public_url(raw)
    except Exception as exc:                    # 策略模块不可用：按"无法证明"拒绝
        raise LLMCallError(f"LLM 端点策略校验不可用：{str(exc)[:80]}") from exc
    if not decision.ok:
        raise LLMCallError(f"LLM 端点被网络策略拒绝：{decision.reason}")
    with _ENDPOINT_CACHE_LOCK:
        _ENDPOINT_OK_CACHE[key] = raw
    return raw


_LLM_CALLS_MAX = 200          # 每任务保留的调用形状条数上限


# ── D5：根任务账本（每次**实际供应商请求**入账）────────────────────
# 为什么放在这里：worker 内部的多次请求（摘要/报告生成）此前只算一张派发票据，
# 根账本因此对不上供应商实际收到的请求数。账本以"一次请求"为单位开票/结算，
# 主备切换与重试各记一次；不限额度也如实计数，只有配了上限才会拒绝发送。
_root_budgets: dict[str, Any] = {}


def _budget_limits_from_file() -> Any:
    """读 `config.json` 的 `system.budget`（与编排器同源）+ `WM_TASK_MAX_*` 环境覆盖。"""
    from root_budget import limits_from_config
    cfg: dict = {}
    try:
        with open(_CFG_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh) or {}
    except Exception:
        cfg = {}
    return limits_from_config(cfg)


def _root_budget_for_task():
    """当前任务的根预算账本；没有任务上下文时返回 None（不记账、不拒绝）。"""
    tid = get_task_context()
    if not tid:
        return None
    cached = _root_budgets.get(tid)
    if cached is not None:
        return cached
    try:
        from root_budget import RootBudget
        from workspace import task_workspace
        ws = task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)   # 账本落在工作区；首次调用时目录可能还没建
        b = RootBudget(tid, ws, _budget_limits_from_file())
    except Exception as exc:                 # noqa: BLE001 - 账本不可用不阻断调用
        logger.warning("根任务账本不可用（task=%s）：本次不记账：%s",
                       str(tid)[:40], str(exc)[:100])
        return None
    _root_budgets[tid] = b
    return b


def _budget_open(stage: str, attempt: int, max_tokens: int, *, usage: str = ""):
    """发送前开票（`stage`：llm / backup）；**预算不足时不发送**。

    抛带 `budget_exhausted` 标记的错误，调用方据此停止后续尝试而不是重试。
    """
    b = _root_budget_for_task()
    if b is None:
        return None, ""
    try:
        ticket = b.reserve(stage, calls=1, tokens=int(max_tokens or 0),
                           detail={"usage": str(usage or ""), "attempt": int(attempt)})
    except Exception as exc:                 # noqa: BLE001 - 含 BudgetExceeded
        _record_llm_call(get_task_context(), stage=usage or stage, attempt=attempt,
                         max_tokens=max_tokens, error_class="budget_exceeded",
                         end_reason="budget_refused")
        err = LLMCallError(f"根任务预算不足，拒绝发送该请求：{str(exc)[:120]}")
        setattr(err, "budget_exhausted", True)
        raise err
    return b, ticket


def _budget_close(b, ticket: str, *, ok: bool, max_tokens: int,
                  note: str = "") -> None:
    """结算一次请求。token 记**上界**（本次未取到供应商实际用量，保守记账）。"""
    if b is None or not ticket:
        return
    try:
        b.settle(ticket, tokens=int(max_tokens or 0), ok=ok,
                 note=note or ("llm:ok" if ok else "llm:failed"))
    except Exception:                        # noqa: BLE001 - 结算失败不影响调用结果
        pass


def _record_llm_call(task_id: str, *, stage: str = "", attempt: int = 0,
                     elapsed_ms: int = 0, input_chars: int = 0,
                     max_tokens: int = 0, http_status: int = 0,
                     error_class: str = "", end_reason: str = "",
                     endpoint: str = "primary") -> None:
    """把一次 LLM 调用的**形状**写入 Redis（`llm_calls:{task_id}`）。

    只记形状：阶段 / 第几次 / 耗时 / 输入长度 / 输出上限 / HTTP 状态或错误类别 /
    结束原因。**不记提示词、响应体、密钥**——失败取证不需要正文。写失败只吞掉，
    诊断不得拖累主线。
    """
    if not task_id:
        return
    global _task_usage_client
    try:
        if _task_usage_client is None:
            import redis as _redis
            _task_usage_client = _redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2, retry=_NO_REDIS_RETRY,
            )
        key = f"llm_calls:{task_id}"
        payload = json.dumps({
            "ts": time.time(),
            "stage": str(stage or ""),
            "attempt": int(attempt or 0),
            "elapsed_ms": int(elapsed_ms or 0),
            "input_chars": int(input_chars or 0),
            "max_tokens": int(max_tokens or 0),
            "http_status": int(http_status or 0),
            "error_class": str(error_class or ""),
            "end_reason": str(end_reason or ""),
            "endpoint": str(endpoint or ""),
        }, ensure_ascii=False)
        # 一次往返写三条（每次调用都记，不能一个调用三次 RTT）
        pipe = _task_usage_client.pipeline()
        pipe.rpush(key, payload)
        pipe.ltrim(key, -_LLM_CALLS_MAX, -1)
        pipe.expire(key, 7200)
        pipe.execute()
    except Exception:
        pass


def get_task_llm_calls(task_id: str) -> dict:
    """读任务的调用形状汇总：`{calls, failed, by_stage, total_elapsed_ms,
    total_input_chars, max_tokens, events}`（events 为脱敏后的逐次记录）。"""
    if not task_id:
        return {}
    global _task_usage_client
    try:
        if _task_usage_client is None:
            import redis as _redis
            _task_usage_client = _redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2, retry=_NO_REDIS_RETRY,
            )
        raw = _task_usage_client.lrange(f"llm_calls:{task_id}", 0, -1) or []
    except Exception:
        return {}
    events: list[dict] = []
    for item in raw:
        try:
            ev = json.loads(item)
        except Exception:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    by_stage: dict[str, int] = {}
    failed = 0
    total_elapsed = 0
    total_input = 0
    max_tok = 0
    for ev in events:
        stage = str(ev.get("stage") or "")
        by_stage[stage] = by_stage.get(stage, 0) + 1
        if str(ev.get("end_reason") or "") != "ok":
            failed += 1
        try:
            total_elapsed += int(ev.get("elapsed_ms") or 0)
            total_input += int(ev.get("input_chars") or 0)
            max_tok = max(max_tok, int(ev.get("max_tokens") or 0))
        except (TypeError, ValueError):
            pass
    return {
        "calls": len(events), "failed": failed, "by_stage": by_stage,
        "total_elapsed_ms": total_elapsed, "total_input_chars": total_input,
        "max_tokens": max_tok, "events": events,
    }


def _primary_degradation_root() -> str:
    """返回主端点当前降级根因；无具体失败时回填 inherited_unhealthy。"""
    with _endpoint_health_lock:
        reason = str(
            _endpoint_health.get("primary", {}).get("last_degradation_reason") or ""
        )
    return reason or "inherited_unhealthy"


def get_task_llm_degradation(task_id: str) -> dict:
    """读取任务的 LLM 降级汇总：{switches, reasons, both_failed, events}。

    P2-3：切换发生但无具体失败事件时，reasons 至少回填一条
    inherited_unhealthy（携带最近失败时间）；events 截断上限与 switches
    对齐，保证补记的根因事件不会把 switch 事件挤出。"""
    if not task_id:
        return {}
    global _task_usage_client
    try:
        if _task_usage_client is None:
            import redis as _redis
            _task_usage_client = _redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2, retry=_NO_REDIS_RETRY,
            )
        raw = _task_usage_client.lrange(f"llm_degradation:{task_id}", 0, -1) or []
        events: list[dict] = []
        for item in raw:
            try:
                events.append(json.loads(item))
            except Exception:
                continue
        reasons: list[str] = []
        switches = 0
        both_failed = False
        last_degradation_ts = 0.0
        for ev in events:
            r = str(ev.get("reason") or "")
            if r == "switch_to_backup":
                switches += 1
            elif r and r not in reasons:
                reasons.append(r)
            if ev.get("both_failed"):
                both_failed = True
            try:
                ts = float(ev.get("ts") or 0)
                if ts > last_degradation_ts:
                    last_degradation_ts = ts
            except (TypeError, ValueError):
                pass
        # P2-3：只有 switch 事件（主端点由他处标记不健康）→ 回填根因
        if not reasons and events:
            reasons.append("inherited_unhealthy")
        # events 上限对齐 switches 计数：每切换最多补记 1 条根因 + 1 条 switch
        events_cap = max(10, switches * 3 + 2)
        return {
            "switches": switches,
            "reasons": reasons,
            "both_failed": both_failed,
            "last_degradation_ts": round(last_degradation_ts, 3),
            "events": events[-events_cap:],
        }
    except Exception:
        return {}


def _is_balance_error(body: str) -> bool:
    """401/402/403 响应体是否余额/额度类错误（Insufficient balance/CreditsError 等）。

    余额不足是确定性故障，不会因重试自愈——命中后应把该端点标记为不健康，
    避免每次调用都先撞一次 401 再切换，白耗一次请求。
    """
    low = str(body or "").lower()
    return any(k in low for k in (
        "insufficient", "balance", "credit", "quota", "额度", "余额",
    ))


def _mark_auth_error(code: int, body: str, endpoint: str = "") -> None:
    """记录鉴权/余额错误（401/402/403），供编排器/前端提醒用户检查 API 配置。

    endpoint 非空且为余额类错误时，同步把该端点标记为不健康（确定性故障，
    避免留在重试/轮换链里每次白耗一次请求）。
    """
    with _auth_error_lock:
        _last_auth_error["ts"] = time.time()
        # 面向用户的消息只保留状态码：供应商响应体可能含账户标识等
        # 敏感回显，仅进日志，不随 /api/status 的 llm_warning 下发给任何
        # 登录用户（含 viewer）
        _last_auth_error["message"] = f"LLM 端点鉴权/余额错误 HTTP {code}"
    if body:
        logger.warning(
            "LLM auth/balance error %s endpoint=%s body=%s",
            code, endpoint, str(body)[:150],
        )
    if endpoint and _is_balance_error(body):
        # 余额不足是确定性故障：立即标记不健康（不走 2 次失败阈值），
        # 后续调用直接跳过该端点，避免每次白耗一次请求
        with _endpoint_health_lock:
            st = _endpoint_health.setdefault(
                endpoint,
                {"healthy": True, "fails": 0,
                 "last_degradation_reason": "", "last_degradation_ts": 0},
            )
            st["healthy"] = False
            st["fails"] = _ENDPOINT_FAIL_THRESHOLD
            st["last_degradation_reason"] = f"HTTP_{code}_insufficient_balance"
            st["last_degradation_ts"] = time.time()


def get_endpoint_warning() -> str:
    """返回 10 分钟内最近一次鉴权/余额错误消息（空串=无）。"""
    with _auth_error_lock:
        if _last_auth_error["ts"] and time.time() - _last_auth_error["ts"] < 600:
            return _last_auth_error["message"]
        return ""


def _primary_healthy() -> bool:
    with _endpoint_health_lock:
        return _endpoint_health.get("primary", {}).get("healthy", True)


def _backup_healthy() -> bool:
    """备用端点健康状态：未配置 backup 或已标记不健康时返回 False。"""
    if not (_BACKUP_CFG.get("base_url") and _BACKUP_CFG.get("api_key")):
        return False
    with _endpoint_health_lock:
        return _endpoint_health.get("backup", {}).get("healthy", True)


def get_endpoint_health() -> dict:
    with _endpoint_health_lock:
        return {
            k: dict(v) for k, v in _endpoint_health.items()
        }


def _classify_probe_error(exc: Exception) -> str:
    """把探测异常归类为余额感知原因。
    reason ∈ ok / insufficient_balance / unauthorized / unreachable。
    401/403 响应体含 insufficient/balance/credits → 余额不足；
    402 Payment Required 语义即计费/额度问题 → 余额不足。"""
    text = str(exc or "")
    m = re.search(r"HTTP[ _-]?(\d{3})", text)
    if m and m.group(1) in ("401", "402", "403"):
        low = text.lower()
        if any(k in low for k in ("insufficient", "balance", "credit")):
            return "insufficient_balance"
        if m.group(1) == "402":
            return "insufficient_balance"
        return "unauthorized"
    return "unreachable"


def _probe_endpoint_status(
    base_url: str, api_key: str, model: str, endpoint: str = "primary",
) -> dict:
    """余额感知探测：极短请求验证端点可用，
    返回 {ok, reason}；reason ∈ ok/insufficient_balance/unauthorized/unreachable。

    预算给 32 而不是 1：本项目接入的推理模型（deepseek-flash 等）会把极小的
    max_tokens 全烧在 reasoning 上，content 恒为空。即便如此，"HTTP 往返成功 +
    鉴权通过"已足以判定端点可达——因此把 thinking_budget_exhausted 视为 ok，
    否则探测会永久误报 unreachable（设置页一直显示"主备均不健康"，
    并可能让任务预检直接拒绝任务）。
    """
    try:
        client = LLMClient(base_url=base_url, api_key=api_key, model=model)
        raw = client._send_request(
            "你是连通性探测器，只回复：ok", "ping", 0.0, 32,
            endpoint=endpoint, timeout=float(
                os.environ.get("LLM_PROBE_TIMEOUT", "20") or 20),
        )
        return {"ok": True, "reason": "ok"}
    except Exception as exc:
        if getattr(exc, "thinking_budget_exhausted", False):
            # 端点可达且鉴权通过，只是探测预算被推理吃光
            return {"ok": True, "reason": "ok"}
        return {"ok": False, "reason": _classify_probe_error(exc)}


def _probe_endpoint(base_url: str, api_key: str, model: str) -> bool:
    """健康探测布尔包装（后台守护线程/旧调用兼容）。"""
    return _probe_endpoint_status(base_url, api_key, model)["ok"]


_BALANCE_CACHE_TTL = 30.0
# 终态类失败（欠费/鉴权失败）不会在几十秒内自愈：给长冷却，避免每 30 秒白打一次
# 已知不可用的端点（实测日志里每 32 秒刷一条 402 INSUFFICIENT_BALANCE）。
_BALANCE_COOLDOWN = float(os.environ.get("LLM_BALANCE_COOLDOWN", "") or 600.0)
_BALANCE_TERMINAL_REASONS = ("insufficient_balance", "unauthorized")
_balance_cache_lock = threading.Lock()
_balance_cache: dict = {"ts": 0.0, "data": None, "ttl": _BALANCE_CACHE_TTL, "ep_ts": {}}


def _clear_balance_cache() -> None:
    """清空余额预检缓存（配置热更新/测试后调用）。"""
    with _balance_cache_lock:
        _balance_cache["ts"] = 0.0
        _balance_cache["data"] = None
        _balance_cache["ep_ts"] = {}


def _frozen_terminal_endpoints(now: float) -> dict:
    """仍处长冷却期的终态失败端点 → 复用上次结论，**不重复探测**（M0-e）。

    冷却按端点判定：欠费的备用端点在自己的冷却期内不再被白打，健康的主端点
    照常探测——此前只要任一端点是终态失败就把整份结果缓存 600 秒，
    等于用欠费的备用端点把健康主端点一起冻住。
    """
    frozen: dict = {}
    with _balance_cache_lock:
        data = dict(_balance_cache.get("data") or {})
        ep_ts = dict(_balance_cache.get("ep_ts") or {})
    for ep in ("primary", "backup"):
        st = data.get(ep) or {}
        if (str(st.get("reason") or "") in _BALANCE_TERMINAL_REASONS
                and now - float(ep_ts.get(ep, 0.0)) < _BALANCE_COOLDOWN):
            frozen[ep] = dict(st)
            frozen[ep]["frozen"] = True
    return frozen


def get_balance_status(use_cache: bool = True) -> dict:
    """主/备端点余额预检：{primary: {ok, reason}, backup: {ok, reason}}，
    reason ∈ ok/insufficient_balance/unauthorized/unreachable。
    探测结果同步到端点健康状态（余额不足/鉴权失败即视为不健康，走降级路由）；
    结果带 30s TTL 缓存，避免 /api/status 高频轮询反复打端点。"""
    _ensure_cfg_fresh()
    now = time.time()
    with _balance_cache_lock:
        if (
            use_cache
            and _balance_cache["data"] is not None
            and now - _balance_cache["ts"] < _balance_cache.get("ttl", _BALANCE_CACHE_TTL)
        ):
            return {
                k: dict(v) for k, v in _balance_cache["data"].items()
            }
    result: dict = _frozen_terminal_endpoints(now) if use_cache else {}
    probes: list[tuple[str, str, str, str]] = []
    primary_base = os.environ.get("LLM_BASE_URL") or ""
    if primary_base and "primary" not in result:
        probes.append((
            "primary", primary_base,
            os.environ.get("LLM_API_KEY") or "",
            os.environ.get("LLM_MODEL") or "gpt-4o",
        ))
    if _BACKUP_CFG.get("base_url") and "backup" not in result:
        probes.append((
            "backup", _BACKUP_CFG.get("base_url", ""),
            _BACKUP_CFG.get("api_key", ""),
            _BACKUP_CFG.get("model") or "gpt-4o",
        ))
    # 主/备同探并行：顺序探测时两个端点都不可达会让提交路径
    # 阻塞 2×LLM_PROBE_TIMEOUT（默认 40s），并行把最坏延迟减半
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # futures 在脚本层线性引用 as_completed（保证该引用在运行期解析）
    if probes:
        with ThreadPoolExecutor(max_workers=2) as _pool:
            _futures = {}
            for _ep, _base, _key, _model in probes:
                _futures[_pool.submit(
                    _probe_endpoint_status, _base, _key, _model,
                    endpoint=_ep,
                )] = _ep
            for _fut in as_completed(_futures):
                _ep = _futures[_fut]
                _st = _fut.result()
                _mark_endpoint(_ep, _st["ok"], _st["reason"])
                result[_ep] = _st
    if not result.get("primary"):
        result["primary"] = {"ok": False, "reason": "unreachable"}
    if not result.get("backup"):
        result["backup"] = {"ok": False, "reason": "unreachable"}
    with _balance_cache_lock:
        _now = time.time()
        _balance_cache["ts"] = _now
        _balance_cache["data"] = result
        ep_ts = dict(_balance_cache.get("ep_ts") or {})
        for _ep, _st in result.items():
            if _st.get("frozen"):
                continue                      # 冻结复用的结论保留原探测时间
            ep_ts[_ep] = _now
        _balance_cache["ep_ts"] = ep_ts
        # 整体 TTL 只在**所有**端点都处于终态失败时才拉长；只要还有一个端点可用，
        # 就保持 30 秒的常规刷新（终态端点另有 per-endpoint 冷却，不会被打爆）
        _all_terminal = all(
            (result.get(ep) or {}).get("reason") in _BALANCE_TERMINAL_REASONS
            for ep in ("primary", "backup")
        )
        _balance_cache["ttl"] = _BALANCE_COOLDOWN if _all_terminal else _BALANCE_CACHE_TTL
    return {k: dict(v) for k, v in result.items()}


def endpoint_hosts() -> dict[str, str]:
    """主/备端点主机名（小写，含端口前域名）。"""
    def _host(url: str) -> str:
        try:
            from urllib.parse import urlparse
            return (urlparse(str(url)).hostname or "").lower()
        except Exception:
            return ""

    _ensure_cfg_fresh()
    return {
        "primary": _host(os.environ.get("LLM_BASE_URL") or ""),
        "backup": _host(_BACKUP_CFG.get("base_url") or ""),
    }


# 厂商识别：host 后缀 → 厂商标识。仅比较域名相等发现不了"同厂商不同域名"
# （api.siliconflow.cn vs api-inner.siliconflow.cn），故按后缀归组；
# 未知厂商回退 hostname 本身（保证永远有可比对的值）。
_VENDOR_MAP = (
    (".tokenrhythm.studio", "TokenRhythm"), ("tokenrhythm.studio", "TokenRhythm"),
    ("siliconflow.cn", "SiliconFlow"),
    ("deepseek.com", "DeepSeek"),
    ("moonshot.cn", "Moonshot"),
    ("bigmodel.cn", "智谱"),
    ("zhipuai.cn", "智谱"),
    ("dashscope.aliyuncs.com", "阿里云百炼"),
    ("volces.com", "火山引擎"),
    ("openai.com", "OpenAI"),
    ("anthropic.com", "Anthropic"),
    ("generativelanguage.googleapis.com", "Google"),
    ("openrouter.ai", "OpenRouter"),
)


def _vendor_of(host: str) -> str:
    """host → 厂商标识；支持 LLM_VENDOR_MAP 环境变量覆盖（JSON: 后缀→厂商）。"""
    h = str(host or "").lower()
    if not h:
        return ""
    try:
        raw = os.environ.get("LLM_VENDOR_MAP") or ""
        if raw:
            custom = json.loads(raw)
            if isinstance(custom, dict):
                for suffix, name in custom.items():
                    if suffix and str(suffix).lower() in h:
                        return str(name)
    except Exception:
        pass
    for suffix, name in _VENDOR_MAP:
        if h == suffix or h.endswith(suffix):
            return name
    return h


def endpoint_vendors() -> dict[str, str]:
    """主/备端点厂商标识（{primary, backup}）；未知厂商回退 hostname。"""
    hosts = endpoint_hosts()
    return {
        "primary": _vendor_of(hosts.get("primary") or ""),
        "backup": _vendor_of(hosts.get("backup") or ""),
    }


def check_endpoint_diversity() -> tuple[bool, str]:
    """端点多样性校验：(是否多样, 原因)。

    - 主备未成对配置 → ("", "backup_not_configured")，不算风险（单端点模式）
    - host 完全相同 → "same_host"（同源，最高风险）
    - 厂商相同但域名不同 → "same_vendor"（同厂商不同接入点，故障域相同）
    - 其余 → 多样，ok
    """
    hosts = endpoint_hosts()
    p_host, b_host = hosts.get("primary") or "", hosts.get("backup") or ""
    if not b_host:
        return False, "backup_not_configured"
    if p_host and p_host == b_host:
        return False, "same_host"
    vendors = endpoint_vendors()
    p_v, b_v = vendors.get("primary") or "", vendors.get("backup") or ""
    if p_v and p_v == b_v:
        return False, "same_vendor"
    return True, "ok"


def endpoint_diversity_notice() -> str:
    """主备同源风险提示文案（空串=无风险）；供提交预检与状态接口复用。"""
    ok, reason = check_endpoint_diversity()
    vendors = endpoint_vendors()
    if ok or reason == "backup_not_configured":
        return ""
    if reason == "same_host":
        return (f"主备端点同一域名（{vendors.get('primary') or '?'}），"
                "同源风险：该平台故障会同时打挂主备，建议主备分属不同厂商")
    return (f"主备端点同一供应商（{vendors.get('primary') or '?'}），"
            "同源风险：建议主备分属不同厂商")


def endpoints_available() -> tuple[bool, str]:
    """返回 (是否可用, 消息)。仅当主/备用都已被判定不健康时才做一次真实探测。
    供编排器在任务开始前做 LLM 健康预检：不可用 → 终止任务并向前端弹警告。
    探测对 401/402/403 做余额感知：401/403 响应体含 insufficient/balance/credits
    或 402 均识别为余额不足，给出明确的充值提示而非笼统的"端点不可用"。"""
    _ensure_cfg_fresh()
    with _endpoint_health_lock:
        ph = _endpoint_health.get("primary", {}).get("healthy", True)
        bh = (
            _endpoint_health.get("backup", {}).get("healthy", True)
            if _BACKUP_CFG.get("base_url") else False
        )
    if ph or bh:
        return True, ""
    # 双端点标记不健康 → 真实探测备用端点（余额感知极短请求）
    if _BACKUP_CFG.get("base_url"):
        st = _probe_endpoint_status(
            _BACKUP_CFG.get("base_url", ""),
            _BACKUP_CFG.get("api_key", ""),
            _BACKUP_CFG.get("model") or "gpt-4o",
            endpoint="backup",
        )
        ok = st["ok"]
        _mark_endpoint("backup", ok, st["reason"])
        if ok:
            return True, ""
        if st["reason"] == "insufficient_balance":
            return False, (
                "LLM 端点不可用：主端点和备用端点均余额不足，"
                "请充值或检查 API 设置后重试。"
            )
        return False, (
            "LLM 端点不可用：主端点和备用端点均调用失败"
            "（可能余额不足/密钥失效或无响应）。请检查前端 API 设置后重试。"
        )
    return False, "LLM 主端点不可用且未配置备用端点，请检查前端 API 设置后重试。"


def _health_monitor_loop(interval: float = 60.0) -> None:
    """后台守护：端点不健康时持续探测，连续 N 次成功才恢复（防抖），
    主/备端点都监控且各自独立计数——共享一个计数变量会让主端点
    恢复期间的抖动被备份探测成功"喂饱"，反之亦然。"""
    _restore_needed = max(
        1, int(os.environ.get("LLM_HEALTH_RESTORE", "2") or 2))
    _consecutive = {"primary": 0, "backup": 0}
    while True:
        time.sleep(interval)
        try:
            for ep in ("primary", "backup"):
                if ep == "primary" and _primary_healthy():
                    _consecutive[ep] = 0
                    continue
                if ep == "backup" and (
                    not _BACKUP_CFG.get("base_url") or _backup_healthy()
                ):
                    _consecutive[ep] = 0
                    continue
                if ep == "primary":
                    base_url = os.environ.get("LLM_BASE_URL") or ""
                    api_key = os.environ.get("LLM_API_KEY") or ""
                    model = os.environ.get("LLM_MODEL") or "gpt-4o"
                else:
                    base_url = _BACKUP_CFG.get("base_url", "")
                    api_key = _BACKUP_CFG.get("api_key", "")
                    model = _BACKUP_CFG.get("model") or "gpt-4o"
                if base_url and _probe_endpoint(base_url, api_key, model):
                    _consecutive[ep] += 1
                    if _consecutive[ep] >= _restore_needed:
                        _mark_endpoint(ep, True)
                        _consecutive[ep] = 0
                        logger.info(
                            "LLM %s endpoint recovered (%d 次连续探测成功)",
                            ep, _restore_needed,
                        )
                else:
                    _consecutive[ep] = 0
        except Exception:
            pass


def start_health_monitor(interval: float = 60.0) -> None:
    """幂等启动健康探测守护线程。"""
    global _health_monitor_started
    if _health_monitor_started:
        return
    _health_monitor_started = True
    threading.Thread(
        target=_health_monitor_loop, args=(interval,), daemon=True
    ).start()


# ---------------------------------------------------------------------------
# 步骤级流式输出（O-21）
# ---------------------------------------------------------------------------


def _publish_stream_chunk(text: str) -> None:
    tid = get_task_context()
    if not tid or os.environ.get("STREAM_OUTPUT", "1") == "0":
        return
    global _task_usage_client
    try:
        if _task_usage_client is None:
            import redis as _redis
            _task_usage_client = _redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2, retry=_NO_REDIS_RETRY,
            )
        key = f"stream:{tid}"
        _task_usage_client.rpush(key, text)
        _task_usage_client.ltrim(key, -4000, -1)
        _task_usage_client.expire(key, 600)
    except Exception:
        pass


# ============================================================================
# 任务级 LLM 预算熔断（#3：防止失控任务无限烧 API 配额）
# 实测发现：code_execution 等步骤在 LLM 端点连续失败时，以 60s 为周期无限
# 重试，任务长时间 PENDING 且持续消耗配额。本熔断按任务维度限制
# 调用次数/总时长/成本，超限抛 LLMUnavailableError（编排器已对该异常做
# 任务终止处理），把"无限重试"变成"确定性终止"。
# ============================================================================

_task_budget_lock = threading.Lock()
_task_budget_state: dict[str, dict] = {}  # task_id -> {"start_ts", "calls", "cost_usd"}

# 默认预算：60 次调用 / 30 分钟 / 无成本上限（成本需配置价格表后才准确）。
# 可用 config.json llm.task_budget 或环境变量覆盖：
#   LLM_TASK_BUDGET_CALLS / LLM_TASK_BUDGET_SECONDS / LLM_TASK_BUDGET_USD
# 任一维度为 0 表示不限制该维度。
_DEFAULT_TASK_BUDGET = {"max_calls": 60, "max_seconds": 1800, "max_cost_usd": 0.0}


def _task_budget_limits() -> dict:
    try:
        cfg = (_LLM_CFG or {}).get("task_budget") or {}
    except Exception:
        cfg = {}
    out = {}
    for key, env, dflt in (
        ("max_calls", "LLM_TASK_BUDGET_CALLS", _DEFAULT_TASK_BUDGET["max_calls"]),
        ("max_seconds", "LLM_TASK_BUDGET_SECONDS", _DEFAULT_TASK_BUDGET["max_seconds"]),
        ("max_cost_usd", "LLM_TASK_BUDGET_USD", _DEFAULT_TASK_BUDGET["max_cost_usd"]),
    ):
        try:
            v = os.environ.get(env)
            if v is None or v == "":
                v = cfg.get(key, dflt)
            out[key] = max(0.0, float(v))
        except (TypeError, ValueError):
            out[key] = dflt
    return out


def _check_task_budget() -> None:
    """LLM 调用前检查当前任务预算；超限抛 LLMUnavailableError（任务终止）。"""
    tid = _task_context_var().get()
    if not tid:
        return
    limits = _task_budget_limits()
    now = time.time()
    with _task_budget_lock:
        st = _task_budget_state.setdefault(
            tid, {"start_ts": now, "calls": 0, "cost_usd": 0.0},
        )
        if limits["max_calls"] > 0 and st["calls"] >= limits["max_calls"]:
            raise LLMUnavailableError(
                f"任务 LLM 预算熔断：调用次数 {st['calls']} 已达上限 {limits['max_calls']:.0f}"
            )
        if limits["max_seconds"] > 0 and now - st["start_ts"] >= limits["max_seconds"]:
            raise LLMUnavailableError(
                f"任务 LLM 预算熔断：运行时长 {now - st['start_ts']:.0f}s 已达上限 {limits['max_seconds']:.0f}s"
            )
        if limits["max_cost_usd"] > 0 and st["cost_usd"] >= limits["max_cost_usd"]:
            raise LLMUnavailableError(
                f"任务 LLM 预算熔断：成本 ${st['cost_usd']:.2f} 已达上限 ${limits['max_cost_usd']:.2f}"
            )


def _bump_task_budget(prompt_tokens: int, completion_tokens: int, model: str = "") -> None:
    """LLM 调用成功后累计任务预算状态（调用次数 + 成本）。"""
    tid = _task_context_var().get()
    if not tid:
        return
    try:
        from costs import estimate_cost
        cost = estimate_cost(model, int(prompt_tokens or 0), int(completion_tokens or 0))
    except Exception:
        cost = 0.0
    with _task_budget_lock:
        st = _task_budget_state.setdefault(
            tid, {"start_ts": time.time(), "calls": 0, "cost_usd": 0.0},
        )
        st["calls"] += 1
        st["cost_usd"] += float(cost or 0.0)


def reset_task_budget(task_id: str = "") -> None:
    """清除任务预算状态（任务开始/结束或测试时调用）。"""
    with _task_budget_lock:
        if task_id:
            _task_budget_state.pop(task_id, None)
        else:
            _task_budget_state.clear()


def _record_usage(
    prompt_tokens: int, completion_tokens: int,
    model: str = "", cached: bool = False, endpoint: str = "",
) -> None:
    """记录一次 LLM 调用用量。
    cached=True 表示缓存命中：只记调用次数与 cached 标记，不计 token 成本。
    endpoint：按端点（base_url）分账台账键（B3）。"""
    with _usage_lock:
        _usage["calls"] += 1
        _usage["prompt_tokens"] += int(prompt_tokens or 0)
        _usage["completion_tokens"] += int(completion_tokens or 0)
        if cached:
            _usage["cached"] = _usage.get("cached", 0) + 1
        if endpoint:
            _be = _usage.setdefault("by_endpoint", {})
            _e = _be.setdefault(
                endpoint, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
            )
            _e["calls"] += 1
            _e["prompt_tokens"] += int(prompt_tokens or 0)
            _e["completion_tokens"] += int(completion_tokens or 0)
    # 每任务台账（Redis Hash）：llm_usage_task:{task_id}
    tid = _task_context_var().get()
    if not tid:
        return
    global _task_usage_client
    try:
        if _task_usage_client is None:
            import redis as _redis
            _task_usage_client = _redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2, retry=_NO_REDIS_RETRY,
            )
        key = f"llm_usage_task:{tid}"
        _task_usage_client.hincrby(key, "calls", 1)
        if cached:
            # 缓存命中不计 token 成本，仅标记命中次数
            _task_usage_client.hincrby(key, "cached", 1)
        else:
            _task_usage_client.hincrby(key, f"pt:{model or 'unknown'}", int(prompt_tokens or 0))
            _task_usage_client.hincrby(key, f"ct:{model or 'unknown'}", int(completion_tokens or 0))
        _task_usage_client.expire(key, 7200)
    except Exception:
        pass
    # Roadmap 余项④：月度预算累计（与任务台账并行，静默降级）
    if not cached:
        try:
            from costs import record_monthly_usage
            record_monthly_usage(model, prompt_tokens, completion_tokens)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# B1 模型解析 + B2 缓存工具
# ---------------------------------------------------------------------------


def get_model_for_usage(usage: str = "", default_model: str = "") -> str:
    """按调用用途解析模型名：llm.model_roles[用途] > default_model > LLM_MODEL。
    未配置用途或缺省回退到默认模型，保证老调用行为不变。
    月度预算超限时高价角色自动降级（Roadmap 余项④）。"""
    _ensure_cfg_fresh()
    role = MODEL_ROLES.get(str(usage or "").lower(), "")
    model = ""
    if role:
        model = str(_MODEL_ROLES_CFG.get(role) or "")
    if not model:
        model = default_model or os.environ.get("LLM_MODEL") or ""
    try:
        from costs import resolve_model_with_budget
        model = resolve_model_with_budget(usage, model)
    except Exception:
        pass
    return model


def _get_cache_ttl() -> int:
    """LLM_CACHE_TTL 环境变量：默认 0 表示缓存关闭；开启后按秒设置 TTL。"""
    try:
        return max(0, int(os.environ.get("LLM_CACHE_TTL", "0") or 0))
    except Exception:
        return 0


def _get_llm_cache_client():
    """获取同步 Redis 缓存客户端（本地延迟小；测试可整体替换 _llm_cache_client）。"""
    global _llm_cache_client
    if _llm_cache_client is None:
        import redis as _redis
        _llm_cache_client = _redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2, retry=_NO_REDIS_RETRY,
        )
    return _llm_cache_client


def _cache_redis_key(cache_key: str, user: str) -> str:
    """缓存键：调用方 cache_key 命名空间 + prompt 前 500 字符的哈希。"""
    import hashlib
    digest = hashlib.sha256(
        str(user or "")[:500].encode("utf-8")
    ).hexdigest()
    return f"llm_cache:{str(cache_key or 'default')}:{digest}"


def _cache_get(cache_key: str, user: str):
    """命中返回缓存结果（dict/str），未命中或缓存不可用返回 None。"""
    if _get_cache_ttl() <= 0 or not cache_key:
        return None
    key = _cache_redis_key(cache_key, user)
    try:
        raw = _get_llm_cache_client().get(key)
        if raw is None:
            return None
        logger.info("LLM cache hit: %s", key)
        try:
            return json.loads(raw)
        except Exception:
            return {"content": raw}
    except Exception:
        return None


def _cache_set(cache_key: str, user: str, result) -> None:
    """把调用结果写入缓存（失败静默降级为不缓存）。"""
    ttl = _get_cache_ttl()
    if ttl <= 0 or not cache_key:
        return
    key = _cache_redis_key(cache_key, user)
    try:
        _get_llm_cache_client().set(
            key, json.dumps(result, ensure_ascii=False), ex=ttl,
        )
    except Exception:
        pass


def get_usage_stats() -> dict:
    """返回全局 LLM 用量统计（调用次数、输入/输出 token）。"""
    with _usage_lock:
        return dict(_usage)


def _publish_usage_snapshot() -> None:
    """把本进程的累计用量写入 Redis（带 TTL），供跨进程聚合。"""
    global _usage_pub_client
    try:
        if _usage_pub_client is None:
            import redis as _redis
            _usage_pub_client = _redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2, retry=_NO_REDIS_RETRY,
            )
        _usage_pub_client.set(
            f"llm_usage:{os.getpid()}",
            json.dumps(get_usage_stats()),
            ex=3600,
        )
    except Exception:
        pass


# ============================================================================
# 异常定义
# ============================================================================



class LLMCallError(Exception):
    """LLM 调用失败异常"""

    def __init__(self, message: str, attempt: int = 0, response: Any = None) -> None:
        self.attempt = attempt
        self.response = response
        super().__init__(message)


class LLMJSONParseError(LLMCallError):
    """LLM 返回非 JSON 或 JSON 无法解析"""

    pass


class LLMEmptyResponseError(LLMCallError):
    """LLM 返回 200 但内容为空（部分推理模型偶发）"""

    pass


class LLMUnavailableError(LLMCallError):
    """主/备用端点均不可用（余额不足、鉴权失败或无响应）——任务应终止并弹警告，
    而不是带着死端点空转十几分钟。"""

    pass


class LLMCancelledError(LLMCallError):
    """任务已被取消：**不再重试、也不再切备用**（R2）。

    取消与"调用失败"是两回事：失败可以重试/换端点，取消只应更快停下来。
    此前取消只在派发边界被检查，模型调用内部的重试与主备切换完全看不见它——
    用户点了停止，客户端仍会把 3 次重试 + 备用端点各跑一遍，白花钱。
    """

    def __init__(self, message: str = "任务已取消，不再发起 LLM 请求") -> None:
        super().__init__(message)
        self.cancelled = True


# 取消守卫（R2）：由编排器注册"这个任务是否已请求停止"，客户端在**每次尝试前**
# 与**切备用前**复查。没注册守卫时行为与以前一致（不假装知道取消状态）。
_cancel_guard = None


def set_cancel_guard(fn) -> None:
    """注册取消守卫：`fn(task_id) -> bool`。传 None 取消注册。"""
    global _cancel_guard
    _cancel_guard = fn if callable(fn) else None


def _cancelled() -> bool:
    """当前任务是否已请求停止（无守卫时恒为 False）。"""
    if _cancel_guard is None:
        return False
    task_id = get_task_context()
    if not task_id:
        return False
    try:
        return bool(_cancel_guard(task_id))
    except Exception as exc:          # 守卫异常不得影响正常调用
        logger.warning("取消守卫异常（按未取消继续）：%s", str(exc)[:100])
        return False


def _parse_json_content(raw: str) -> dict[str, Any]:
    """模块级 JSON 解析（async 路径使用）：统一走 common.extract_json_object。"""
    result = extract_json_object(raw)
    if isinstance(result, dict):
        return result
    if isinstance(result, list):
        return {"items": result}
    raise LLMJSONParseError(
        f"Failed to parse LLM response as JSON. Raw: {raw[:500]}..."
    )


# ============================================================================
# LLM 客户端
# ============================================================================



def _load_llm_config():
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        with open(cfg_path, 'r') as f:
            return json.load(f).get('llm', {})
    except Exception: return {}

_LLM_CFG = _load_llm_config()
_MODEL_ROLES_CFG = {
    str(k): str(v)
    for k, v in (_LLM_CFG.get("model_roles") or {}).items()
    if v
}


def _load_backup_config() -> dict:
    """读取备用 LLM 端点/模型配置（config.json 顶层 backup 段）。"""
    try:
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
        with open(cfg_path, 'r') as f:
            return json.load(f).get('backup', {}) or {}
    except Exception:
        return {}


_BACKUP_CFG = _load_backup_config()

class LLMClient:
    """OpenAI 兼容的 LLM 调用客户端。

    支持 OpenAI、DeepSeek、Ollama 等所有兼容 /v1/chat/completions 的服务。

    Usage:
        client = LLMClient()
        result = client.call(system="你是...", user="请计划...")
    """

    # 最大重试次数
    _MAX_RETRIES: int = max(1, int(os.environ.get("LLM_MAX_ATTEMPTS", "2") or 2))
    # 重试间隔基数（秒）
    _RETRY_BASE: float = 0.3

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        is_planner: bool = False,
    ) -> None:
        """初始化 LLM 客户端。

        Args:
            base_url: API 基础 URL。默认读取环境变量 LLM_BASE_URL，
                      再用 OPENAI_BASE_URL，最后回退到 https://api.openai.com/v1。
            api_key: API 密钥。默认读取 LLM_API_KEY 或 OPENAI_API_KEY。
            model: 模型名称。默认读取 LLM_MODEL，回退到 gpt-4o。
            temperature: 生成温度。
            max_tokens: 最大输出 token 数。
        """
        self.base_url = (
            base_url
            or os.environ.get("LLM_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        )
        self.api_key = (
            api_key
            or os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        self.model = (
            model
            or os.environ.get("LLM_MODEL")
            or "gpt-4o"
        )
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._is_planner = is_planner
        self._backup_cfg = (
            dict(_BACKUP_CFG)
            if _BACKUP_CFG.get("base_url") and _BACKUP_CFG.get("api_key")
            else {}
        )

        # 延迟导入，避免强依赖
        self._http_module: Any = None

        logger.info(
            "LLMClient: model=%s, base_url=%s", self.model, self.base_url
        )

    # ------------------------------------------------------------------
    # 公共方法
    # ------------------------------------------------------------------

    def _resolve_model(self, usage: str = "", model_override: str | None = None) -> str:
        """解析本次调用的模型：显式 model_override > llm.model_roles[用途] > 客户端默认模型。"""
        if model_override:
            return str(model_override)
        role_model = get_model_for_usage(usage, self.model)
        if role_model:
            return role_model
        return self.model

    def call(
        self,
        system: str,
        user: str,
        *,
        expect_json: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
        usage: str = "",
        model_override: str | None = None,
        cache_key: str | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """调用 LLM 并返回解析结果。

        Args:
            system: 系统提示词。
            user: 用户提示词。
            expect_json: 是否期望 JSON 响应（默认 True）。
            temperature: 覆盖默认温度。
            max_tokens: 覆盖默认输出长度。

        Returns:
            解析后的字典（always a dict）。

        Raises:
            LLMCallError: 所有重试耗尽后抛出。
            LLMJSONParseError: JSON 解析失败。
        """
        temp = temperature if temperature is not None else self.temperature
        max_tok = max_tokens if max_tokens is not None else self.max_tokens
        model = self._resolve_model(usage, model_override)
        # 诊断用输入长度（**只记长度不记内容**）：提示词不进任何诊断记录
        _input_chars = len(str(system or "")) + len(str(user or ""))

        def _remaining() -> float | None:
            """距 deadline 的剩余预算（秒）；未设 deadline 返回 None。"""
            if deadline is None:
                return None
            return float(deadline) - time.time()

        def _attempt_timeout(default: float | None = None) -> float | None:
            """单次请求超时：不超过剩余预算（至少 5s，避免瞬间放弃）。"""
            remaining = _remaining()
            if remaining is None:
                return default
            if remaining <= 0:
                return 0.0
            base = default if default is not None else 600.0
            return max(5.0, min(base, remaining))

        def _budget_exhausted() -> bool:
            remaining = _remaining()
            return remaining is not None and remaining <= 0

        _ensure_cfg_fresh()
        if self._is_planner:
            self.base_url = os.environ.get("PLANNER_LLM_BASE_URL") or self.base_url
            self.api_key = os.environ.get("PLANNER_LLM_API_KEY") or self.api_key
            self.model = os.environ.get("PLANNER_LLM_MODEL") or self.model
        else:
            self.base_url = os.environ.get("LLM_BASE_URL") or self.base_url
            self.api_key = os.environ.get("LLM_API_KEY") or self.api_key
            self.model = os.environ.get("LLM_MODEL") or self.model
        # 配置热重载后模型可能变化，重新解析一次（优先级：override > model_roles > 默认）
        model = self._resolve_model(usage, model_override)
        self._backup_cfg = (
            dict(_BACKUP_CFG)
            if _BACKUP_CFG.get("base_url") and _BACKUP_CFG.get("api_key")
            else {}
        )

        # B2：缓存命中直接返回，不触发网络调用，也不计 token 成本
        cached = _cache_get(cache_key, user) if cache_key else None
        if cached is not None:
            _record_usage(0, 0, model=model, cached=True)
            return cached

        last_error: Exception | None = None
        # 健康路由（O-29）：主端点已被判定不健康 → 优先走备用，避免每次白白等待超时
        if _cancelled():
            # R2：取消后连这一次"健康路由到备用"的请求都不发
            raise LLMCancelledError()
        if not _primary_healthy():
            try:
                _bk_timeout = _attempt_timeout()
                return self._call_backup(system, user, temp, max_tok, expect_json,
                                         timeout=_bk_timeout)
            except LLMJSONParseError:
                raise
            except Exception as exc:
                _mark_endpoint("backup", False, _degradation_reason(exc))
                _record_task_degradation(
                    get_task_context(), _degradation_reason(exc), both_failed=True,
                )
                logger.warning("Health-routed backup failed: %s", str(exc)[:150])
        for attempt in range(1, self._MAX_RETRIES + 1):
            if _cancelled():
                # R2：取消优先于预算——重试只会让"停止"更晚生效
                logger.info("任务已取消：不再重试 LLM 调用（usage=%s, attempt=%d）",
                            usage, attempt)
                _record_llm_call(get_task_context(), stage=usage, attempt=attempt,
                                 input_chars=_input_chars, max_tokens=max_tok,
                                 error_class="cancelled", end_reason="cancelled")
                raise LLMCancelledError()
            if _budget_exhausted():
                # 时间预算耗尽：不再重试/切备用，给调用方一个明确信号去降级
                exc = LLMCallError(
                    f"LLM time budget exhausted before attempt {attempt}"
                )
                setattr(exc, "budget_exhausted", True)
                logger.warning("LLM time budget exhausted (usage=%s)", usage)
                _record_llm_call(get_task_context(), stage=usage, attempt=attempt,
                                 input_chars=_input_chars, max_tokens=max_tok,
                                 error_class="budget_exhausted",
                                 end_reason="budget_exhausted")
                raise exc
            # D5：发送前开票——**在 try 之外**，预算不足时直接抛给调用方
            # （放进 try 会被本地的重试处理吞掉，变成一次"端点失败"）
            _rb, _ticket = _budget_open("llm", attempt, max_tok, usage=usage)
            try:
                _t = _attempt_timeout()
                # 无预算时不传 timeout：保持调用形状与改动前一致
                # （既有打桩/自定义 _send_request 不必为新增特性适配）
                _req_kw = {"timeout": _t} if _t is not None else {}
                _t0 = time.monotonic()
                raw = self._send_request(system, user, temp, max_tok, model=model,
                                         **_req_kw)
                _mark_endpoint("primary", True)
                if not expect_json:
                    result: dict[str, Any] = {"content": raw}
                else:
                    result = self._parse_json(raw)
                _cache_set(cache_key, user, result)
                _record_llm_call(get_task_context(), stage=usage, attempt=attempt,
                                 elapsed_ms=int((time.monotonic() - _t0) * 1000),
                                 input_chars=_input_chars, max_tokens=max_tok,
                                 end_reason="ok")
                _budget_close(_rb, _ticket, ok=True, max_tokens=max_tok)
                return result
            except LLMJSONParseError as exc:
                # JSON 解析失败不重试（格式问题重试没用）
                _record_llm_call(get_task_context(), stage=usage, attempt=attempt,
                                 elapsed_ms=int((time.monotonic() - _t0) * 1000),
                                 input_chars=_input_chars, max_tokens=max_tok,
                                 error_class="bad_json", end_reason="bad_json")
                _budget_close(_rb, _ticket, ok=False, max_tokens=max_tok,
                              note="llm:bad_json")
                raise
            except Exception as exc:
                # 思考耗尽（reasoning 模型烧光预算）→ 放大 max_tokens 立即重试，
                # 不计入端点失败（不是端点故障）
                if (
                    getattr(exc, "thinking_budget_exhausted", False)
                    and max_tok
                    and max_tok < 8192
                ):
                    max_tok = min(max_tok * 2, 8192)
                    logger.warning(
                        "thinking budget exhausted, retry with max_tokens=%d",
                        max_tok,
                    )
                    # 这次请求已经发出（供应商可能计费）：结算后再放大重试
                    _budget_close(_rb, _ticket, ok=False, max_tokens=max_tok,
                                  note="llm:thinking_budget_exhausted")
                    continue
                _reason = _degradation_reason(exc)
                _mark_endpoint("primary", False, _reason)
                _record_task_degradation(get_task_context(), _reason, both_failed=False)
                _http_status, _err_class = _error_shape(exc)
                _record_llm_call(
                    get_task_context(), stage=usage, attempt=attempt,
                    elapsed_ms=int((time.monotonic() - _t0) * 1000),
                    input_chars=_input_chars, max_tokens=max_tok,
                    http_status=_http_status, error_class=_err_class,
                    end_reason="retry" if attempt < self._MAX_RETRIES else "exhausted",
                )
                _budget_close(_rb, _ticket, ok=False, max_tokens=max_tok,
                              note=f"llm:{_err_class or 'failed'}")
                last_error = exc
                logger.warning(
                    "LLM call attempt %d/%d failed: %s",
                    attempt,
                    self._MAX_RETRIES,
                    exc,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._RETRY_BASE * attempt)

        # 主端点失败 → 自动切换备用端点/模型（时间预算耗尽时不再切，直接交给调用方降级）
        # R2：取消后也不再切备用——换端点等于再发一次请求
        if _cancelled():
            raise LLMCancelledError()
        if self._backup_cfg and not _budget_exhausted():
            try:
                _bk_timeout = _attempt_timeout()
                _t0 = time.monotonic()
                out = self._call_backup(system, user, temp, max_tok, expect_json,
                                        timeout=_bk_timeout)
                _record_llm_call(get_task_context(), stage=usage, attempt=attempt,
                                 elapsed_ms=int((time.monotonic() - _t0) * 1000),
                                 input_chars=_input_chars, max_tokens=max_tok,
                                 end_reason="ok", endpoint="backup")
                return out
            except LLMJSONParseError:
                raise
            except Exception as exc:
                _reason = _degradation_reason(exc)
                _mark_endpoint("backup", False, _reason)
                _record_task_degradation(get_task_context(), _reason, both_failed=True)
                _http_status, _err_class = _error_shape(exc)
                _record_llm_call(get_task_context(), stage=usage, attempt=attempt,
                                 elapsed_ms=int((time.monotonic() - _t0) * 1000),
                                 input_chars=_input_chars, max_tokens=max_tok,
                                 http_status=_http_status, error_class=_err_class,
                                 end_reason="exhausted", endpoint="backup")
                logger.error("Backup LLM also failed: %s", str(exc)[:200])
        raise LLMCallError(
            f"LLM call failed after {self._MAX_RETRIES} attempts",
            attempt=self._MAX_RETRIES,
        ) from last_error

    def _call_backup(
        self, system: str, user: str, temperature: float, max_tokens: int,
        expect_json: bool, timeout: float | None = None,
    ) -> dict[str, Any]:
        """调用备用端点并标记健康状态。

        timeout 继承主端点的**剩余预算**：否则切备后就按自己的 600s 超时跑，
        调用方设的时间预算形同虚设（实测切备后总耗时被拖到 50s+）。"""
        if not self._backup_cfg:
            raise LLMCallError("No backup endpoint configured")
        backup = LLMClient(
            base_url=self._backup_cfg.get("base_url"),
            api_key=self._backup_cfg.get("api_key"),
            model=self._backup_cfg.get("model") or self.model,
        )
        _bk_kw = {"timeout": timeout} if timeout is not None else {}
        # D5：备端点的请求同样入账（切流不是"没花钱"）；预算不足时不发送
        _rb, _ticket = _budget_open("backup", 1, max_tokens)
        try:
            raw = backup._send_request(system, user, temperature, max_tokens,
                                       endpoint="backup", **_bk_kw)
        except Exception:
            _budget_close(_rb, _ticket, ok=False, max_tokens=max_tokens,
                          note="llm:backup_failed")
            raise
        _budget_close(_rb, _ticket, ok=True, max_tokens=max_tokens, note="llm:backup_ok")
        _mark_endpoint("backup", True)
        # P2-3：切换发生时把主端点 last_degradation_reason（为空则记
        # inherited_unhealthy）作为根因，避免 llm_degraded 只有 switch 事件
        _record_task_degradation(
            get_task_context(), _primary_degradation_root(), both_failed=False,
        )
        _record_task_degradation(get_task_context(), "switch_to_backup", both_failed=False)
        logger.warning("Switched to backup LLM: %s", self._backup_cfg.get("base_url"))
        if not expect_json:
            return {"content": raw}
        return backup._parse_json(raw)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _send_request(
        self,
        system: str,
        user: str,
        temperature: float,
        max_tokens: int,
        model: str | None = None,
        endpoint: str = "primary",
        timeout: float | None = None,
    ) -> str:
        """发送 HTTP 请求到 LLM 服务。

        Args:
            system: 系统提示词。
            user: 用户提示词。
            temperature: 温度。
            max_tokens: 最大 token。
            timeout: socket 读超时秒数；None 时取 LLM_REQUEST_TIMEOUT（默认 600）。
                     探测类调用传短值（如 20），避免预检被挂死。

        Returns:
            LLM 的文本响应。

        Raises:
            LLMCallError: 请求失败。
        """
        url = f"{self.base_url.rstrip('/')}/chat/completions"

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "WeaveMind/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body: dict[str, Any] = {
            "model": model or self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # 使用 urllib 避免 requests 依赖（标准库可用）
        import urllib.request
        import urllib.error

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=headers, method="POST"
        )

        # 流式优先（复用既有流式实现 `_call_llm_stream_once`，不新增请求站点）：
        # 网关在 ~60s 处切断**整段**非流式响应，报告/总结这类长生成因此必 504
        # （实机复现：8192 tokens 中文生成非流式 60.6s 被 504，流式 76.7s 正常返回）。
        if _stream_enabled():
            _info: dict = {}
            try:
                text = _call_llm_stream_once(
                    self.base_url, self.api_key, model or self.model, system, user,
                    temperature=temperature, max_tokens=max_tokens,
                    publish=False, out=_info,
                )
            except LLMCallError as exc:
                if not _stream_unsupported(exc):
                    raise
                logger.warning("流式请求被拒（%s），回退非流式", describe_error(exc))
            else:
                text = str(text or "").strip()
                if not text:
                    # 与下面非流式分支同判据：思考烧光预算（content 空 + length）
                    # 不是端点故障，交由调用方放大 max_tokens 重试
                    exc = LLMCallError("Empty content in LLM response")
                    if _info.get("reasoning") and _info.get("finish_reason") == "length":
                        exc = LLMCallError(
                            "Empty content in LLM response (thinking budget exhausted)"
                        )
                        exc.thinking_budget_exhausted = True
                    raise exc
                return text

        try:
            # 非流式响应的 socket 读超时：模型计算/生成期间无数据到达即触发。
            # 长文生成（glm 类慢模型实测单次 60-75s，长文 >300s）会被默认 60s
            # 误杀，默认放宽到 600，可用 LLM_REQUEST_TIMEOUT 环境变量覆盖；
            # 探测类调用显式传短超时（timeout 参数）
            timeout = float(
                timeout if timeout is not None
                else os.environ.get("LLM_REQUEST_TIMEOUT", "600") or 600
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 402, 403):
                # 余额类 401/402/403 → 该端点标记不健康，后续调用直接走切换
                _mark_auth_error(exc.code, error_body, endpoint=endpoint)
                if _is_balance_error(error_body):
                    _mark_endpoint(endpoint, False, f"HTTP_{exc.code}_insufficient_balance")
            raise LLMCallError(
                f"HTTP {exc.code}: {error_body[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMCallError(f"Network error: {exc}") from exc

        # 提取 content
        choices = response_data.get("choices", [])
        if not choices:
            raise LLMCallError(
                f"Empty choices in response: {json.dumps(response_data, ensure_ascii=False)[:300]}"
            )

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            # 思考模型（reasoning）把预算烧在 reasoning_content 上导致
            # content 空 + finish_reason=length：这不是端点故障，交由调用方
            # 放大 max_tokens 重试（见 call() 的 thinking 重试分支）
            reasoning = choices[0].get("message", {}).get("reasoning_content") or ""
            finish = choices[0].get("finish_reason") or ""
            if reasoning and finish == "length":
                exc = LLMCallError(
                    "Empty content in LLM response (thinking budget exhausted)"
                )
                exc.thinking_budget_exhausted = True
                raise exc
            raise LLMCallError("Empty content in LLM response")

        # 记录用量
        usage = response_data.get("usage", {})
        if usage:
            _record_usage(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                model=model or self.model, endpoint=self.base_url,
            )
            _bump_task_budget(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                model or self.model,
            )
            _publish_usage_snapshot()
            logger.debug(
                "LLM usage: prompt=%d, completion=%d, total=%d",
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
            )

        return content.strip()

    def _parse_json(self, raw: str) -> dict[str, Any]:
        """从 LLM 响应中提取 JSON（sync 路径）：统一走 common.extract_json_object。

        支持：
        - 纯 JSON 字符串
        - Markdown ```json ... ``` 代码块
        - 前导/后缀文本中的 JSON 对象

        Args:
            raw: LLM 原始文本响应。

        Returns:
            解析后的字典。

        Raises:
            LLMJSONParseError: 无法解析为 JSON。
        """
        return _parse_json_content(raw)


# ============================================================================
# 便捷函数
# ============================================================================

# 全局默认客户端（懒初始化）
_default_client: LLMClient | None = None


def get_default_client() -> LLMClient:
    """获取全局默认 LLM 客户端实例。"""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client


def call_llm(
    system: str, user: str, expect_json: bool = True, *,
    usage: str = "", model_override: str | None = None,
    cache_key: str | None = None,
) -> dict[str, Any]:
    """便捷函数：调用 LLM 并返回解析结果。

    Args:
        system: 系统提示词。
        user: 用户提示词。
        expect_json: 是否期望 JSON 响应。
        usage: 调用用途（plan/exec/judge），用于模型分级路由。
        model_override: 调用级模型覆盖。
        cache_key: 可选缓存键（配合 LLM_CACHE_TTL 使用）。

    Returns:
        解析后的字典。
    """
    _ensure_cfg_fresh()
    _check_task_budget()
    return get_default_client().call(
        system, user, expect_json=expect_json,
        usage=usage, model_override=model_override, cache_key=cache_key,
    )


def _record_stream_usage(usage: dict, model: str) -> None:
    """流式响应的用量记账（`stream_options.include_usage` 时在末尾 chunk 里给出）。"""
    if not isinstance(usage, dict) or not usage:
        return
    try:
        _record_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                      model=model)
        _bump_task_budget(usage.get("prompt_tokens", 0),
                          usage.get("completion_tokens", 0), model)
        _publish_usage_snapshot()
    except Exception:
        pass


def _call_llm_stream_once(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    on_chunk=None,
    temperature: float = 0.1,
    max_tokens: int = 2000,
    publish: bool = True,
    out: dict | None = None,
) -> str:
    """单次 SSE 流式请求（同步，urllib）：逐块回调 on_chunk，返回累计文本。
    空响应/网络错误在此层统一抛 LLMCallError；端点健康标记与备用切换由
    call_llm_stream 负责（与 call()/call_llm_async 语义对齐）。

    `publish=False`：只做传输（不往步骤流推显示片段）——`_send_request` 这类
    非展示用途用它；`out` 用于把用量与结束原因回传给调用方（空内容的
    "思考烧光预算"判据需要 finish_reason）。"""
    import urllib.error
    import urllib.request

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "WeaveMind/1.0",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
        # 用量要显式索取：流式下 token 数只出现在末尾 chunk
        "stream_options": {"include_usage": True},
    }
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        # 流式响应同样放宽：慢模型首块前可能长时间无数据
        timeout = float(os.environ.get("LLM_REQUEST_TIMEOUT", "600") or 600)
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise LLMCallError(
            f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:300]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise LLMCallError(f"Network error: {exc}") from exc

    chunks: list[str] = []
    first = resp.readline()
    head = first.decode("utf-8", errors="replace")
    if head.strip().startswith("data:"):
        lines = [head] + [
            l.decode("utf-8", errors="replace") for l in resp
        ]
        for line in lines:
            t = line.strip()
            if not t.startswith("data:"):
                continue
            payload = t[5:].strip()
            if payload == "[DONE]":
                break
            try:
                obj = json.loads(payload)
                if isinstance(obj.get("usage"), dict) and obj["usage"]:
                    _record_stream_usage(obj["usage"], model)
                    if out is not None:
                        out["usage"] = obj["usage"]
                choices = obj.get("choices") or [{}]
                delta = (choices[0] or {}).get("delta", {}).get("content", "")
                if (choices[0] or {}).get("finish_reason") and out is not None:
                    out["finish_reason"] = str(choices[0]["finish_reason"])
                if (choices[0] or {}).get("delta", {}).get("reasoning_content") and out is not None:
                    out["reasoning"] = True
            except Exception:
                delta = ""
            if delta:
                chunks.append(delta)
                if publish:
                    _publish_stream_chunk(delta)
                if on_chunk is not None:
                    try:
                        on_chunk(delta)
                    except Exception:
                        pass
    else:
        # 非流式端点：整段读取返回
        body_text = head + resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body_text)
            content = data["choices"][0]["message"]["content"]
            chunks.append(str(content))
            if data.get("usage") and out is not None:
                out["usage"] = data["usage"]
            if publish:
                _publish_stream_chunk(str(content))
            if on_chunk is not None:
                try:
                    on_chunk(str(content))
                except Exception:
                    pass
        except Exception:
            chunks.append(body_text)
    return "".join(chunks)


def call_llm_stream(
    system: str,
    user: str,
    on_chunk=None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    usage: str = "",
    model_override: str | None = None,
) -> str:
    """SSE 流式调用（同步，urllib）：逐块回调 on_chunk，并按任务上下文自动发布
    到 Redis stream:{task_id}（O-21 步骤级流式输出）。非流式端点回退整段输出。
    与 call()/call_llm_async 一致：空响应抛 LLMCallError 并标记端点、
    主端点失败（含空响应/超时/连接错误）自动切备用端点重发一次。"""
    _ensure_cfg_fresh()
    _check_task_budget()
    client = get_default_client()
    temp = temperature if temperature is not None else client.temperature
    max_tok = max_tokens or client.max_tokens
    # B1：流式执行调用也按用途选择模型（如 exec）
    model = model_override or get_model_for_usage(usage, client.model)
    # 备用端点配置：与 call()/_call_backup 同源 _BACKUP_CFG（最简复用路径）
    backup_cfg = (
        dict(_BACKUP_CFG)
        if _BACKUP_CFG.get("base_url") and _BACKUP_CFG.get("api_key")
        else {}
    )

    def _attempt(
        base_url: str, api_key: str, request_model: str, endpoint: str,
    ) -> str:
        """单次流式请求：空响应抛 LLMCallError；成功/失败同步端点健康标记。"""
        try:
            text = _call_llm_stream_once(
                base_url, api_key, request_model,
                system, user, on_chunk, temp, max_tok,
            )
        except Exception as exc:
            reason = _degradation_reason(exc)
            _mark_endpoint(endpoint, False, reason)
            if isinstance(exc, LLMCallError):
                raise
            # urllib 超时/连接错误等统一转为 LLMCallError，便于调用方重试
            raise LLMCallError(f"Network error: {exc}") from exc
        if not text.strip():
            # 空响应检测：与 call() 一致，空内容视为端点故障信号
            _mark_endpoint(endpoint, False, "empty_content")
            raise LLMCallError("Empty content in LLM stream response")
        _mark_endpoint(endpoint, True)
        return text

    # 健康路由（O-29 同 sync 路径）：主端点已不健康且备用健康 → 直接走备用
    if not _primary_healthy() and _backup_healthy():
        try:
            text = _attempt(
                backup_cfg["base_url"], backup_cfg.get("api_key", ""),
                backup_cfg.get("model") or model, "backup",
            )
            _record_task_degradation(
                get_task_context(), _primary_degradation_root(), both_failed=False,
            )
            _record_task_degradation(
                get_task_context(), "switch_to_backup", both_failed=False,
            )
            logger.warning(
                "Switched to backup LLM (stream): %s", backup_cfg.get("base_url")
            )
            return text
        except Exception as exc:
            # 健康路由下备用也失败：调用方已有重试逻辑，直接抛出
            _record_task_degradation(
                get_task_context(), _degradation_reason(exc), both_failed=True,
            )
            raise

    # 主端点请求：失败后切备用重发一次
    try:
        return _attempt(client.base_url, client.api_key, model, "primary")
    except LLMCallError:
        if not backup_cfg:
            raise
        try:
            text = _attempt(
                backup_cfg["base_url"], backup_cfg.get("api_key", ""),
                backup_cfg.get("model") or model, "backup",
            )
            _record_task_degradation(
                get_task_context(), _primary_degradation_root(), both_failed=False,
            )
            _record_task_degradation(
                get_task_context(), "switch_to_backup", both_failed=False,
            )
            logger.warning(
                "Switched to backup LLM (stream): %s", backup_cfg.get("base_url")
            )
            return text
        except Exception as exc:
            # 备用也失败：调用方重试逻辑已有，这里直接抛错
            _record_task_degradation(
                get_task_context(), _degradation_reason(exc), both_failed=True,
            )
            raise


# ============================================================================
# Async LLM Client (httpx with connection pooling)
# ============================================================================

import httpx
import asyncio

# Global async client with connection pooling
_async_client: 'httpx.AsyncClient | None' = None

def _get_async_client() -> httpx.AsyncClient:
    global _async_client
    if _async_client is None or _async_client.is_closed:
        limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
        # 与同步路径统一超时口径（此前 120s 硬编码，慢模型长文生成会被误杀）
        _t = float(os.environ.get("LLM_REQUEST_TIMEOUT", "600") or 600)
        timeout = httpx.Timeout(_t, connect=10.0)
        _async_client = httpx.AsyncClient(limits=limits, timeout=timeout)
    return _async_client

async def call_llm_async(
    system_prompt: str,
    user_prompt: str,
    expect_json: bool = True,
    temperature: float = 0.1,
    max_tokens: int = 2000,
    max_attempts: int = 3,
    usage: str = "",
    model_override: str | None = None,
    cache_key: str | None = None,
) -> dict[str, Any] | str:
    """Async LLM call using httpx.AsyncClient with connection pooling.
    
    This enables true concurrency in async workers — multiple tasks
    can call the LLM simultaneously without blocking each other.
    """
    _ensure_cfg_fresh()
    _check_task_budget()
    api_key = os.environ.get('LLM_API_KEY', '')
    base_url = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')
    # B1：按调用用途选择模型（exec/judge/plan），缺省回退 LLM_MODEL
    model = model_override or get_model_for_usage(
        usage, os.environ.get('LLM_MODEL', 'gpt-4'),
    )

    # B2：缓存命中直接返回，不触发网络调用，也不计 token 成本
    cached = _cache_get(cache_key, user_prompt) if cache_key else None
    if cached is not None:
        _record_usage(0, 0, model=model, cached=True)
        return cached

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }

    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }

    # Disabled: response_format not supported by all providers
    # Let the prompt ask for JSON instead

    last_error = None
    # 健康路由（O-29 同 sync 路径）：主端点已被判定不健康 → 优先走备用
    if not _primary_healthy():
        try:
            return await _async_call_backup(payload, model, expect_json)
        except LLMJSONParseError:
            raise
        except Exception as exc:
            logger.warning("Health-routed async backup failed: %s", str(exc)[:150])

    # 诊断用：阶段标签与输入长度必须在**被覆盖之前**取好——下面循环里
    # `usage` 会被响应里的 token 用量覆盖，异步路径的 stage 就丢了
    _stage = str(usage or "")
    _input_chars = len(str(system_prompt or "")) + len(str(user_prompt or ""))

    for attempt in range(1, max_attempts + 1):
        _t0 = time.monotonic()
        try:
            client = _get_async_client()
            # 端点先过网络策略（协议/凭据/解析后地址边界）：拒绝本机、内网与元数据服务
            url = _endpoint_guard(base_url.rstrip('/') + '/chat/completions')

            data = await _async_chat_once(client, url, payload, headers)
            usage = data.get("usage") or {}
            _record_usage(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                model=model,
            )
            _bump_task_budget(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                model,
            )
            _publish_usage_snapshot()

            content = data['choices'][0]['message']['content']
            if content is None or not str(content).strip():
                _mark_endpoint("primary", False)  # 空响应视为端点故障信号
                raise LLMEmptyResponseError("Empty content in LLM response")
            _mark_endpoint("primary", True)
            if expect_json:
                result = _parse_json_content(content)
            else:
                result = content
            _cache_set(cache_key, user_prompt, result)
            _record_llm_call(get_task_context(), stage=_stage, attempt=attempt,
                             elapsed_ms=int((time.monotonic() - _t0) * 1000),
                             input_chars=_input_chars, max_tokens=max_tokens,
                             end_reason="ok")
            return result

        except LLMEmptyResponseError as exc:
            last_error = exc
            _record_llm_call(get_task_context(), stage=_stage, attempt=attempt,
                             elapsed_ms=int((time.monotonic() - _t0) * 1000),
                             input_chars=_input_chars, max_tokens=max_tokens,
                             error_class="empty_content",
                             end_reason="retry" if attempt < max_attempts else "exhausted")
            logger.warning('LLM async empty response, retry %d/%d', attempt, max_attempts)
            await asyncio.sleep(1)
            continue

        except httpx.HTTPStatusError as exc:
            last_error = exc
            _mark_endpoint("primary", False)
            _code = int(exc.response.status_code)
            _record_llm_call(get_task_context(), stage=_stage, attempt=attempt,
                             elapsed_ms=int((time.monotonic() - _t0) * 1000),
                             input_chars=_input_chars, max_tokens=max_tokens,
                             http_status=_code, error_class=f"http_{_code}",
                             end_reason=("auth" if _code in (401, 402, 403)
                                         else ("retry" if attempt < max_attempts
                                               else "exhausted")))
            if exc.response.status_code == 429:
                backoff = min(2 ** attempt, 30)
                logger.warning('LLM async rate limited (429), retry %d/%d in %ds', attempt, max_attempts, backoff)
                await asyncio.sleep(backoff)
                continue
            if exc.response.status_code >= 500:
                logger.warning('LLM async server error, retry %d/%d', attempt, max_attempts)
                await asyncio.sleep(1)
                continue
            if exc.response.status_code in (401, 402, 403):
                # 鉴权/余额错误：重试无意义，直接切备用端点
                _mark_auth_error(
                    exc.response.status_code,
                    str(exc.response.text)[:150],
                    endpoint="primary",
                )
                last_error = LLMCallError(
                    f"主端点鉴权/余额错误 HTTP {exc.response.status_code}"
                )
                break
            raise LLMCallError(f'LLM HTTP {exc.response.status_code}')

        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
            _mark_endpoint("primary", False)
            _record_llm_call(get_task_context(), stage=_stage, attempt=attempt,
                             elapsed_ms=int((time.monotonic() - _t0) * 1000),
                             input_chars=_input_chars, max_tokens=max_tokens,
                             error_class="timeout",
                             end_reason="retry" if attempt < max_attempts else "exhausted")
            logger.warning('LLM async timeout/connect, retry %d/%d', attempt, max_attempts)
            await asyncio.sleep(1)
            continue

    # 主端点失败 → 备用端点/模型（单次尝试）
    if _BACKUP_CFG and _BACKUP_CFG.get("base_url") and _BACKUP_CFG.get("api_key"):
        _tb = time.monotonic()
        try:
            out = await _async_call_backup(payload, model, expect_json)
            _record_llm_call(get_task_context(), stage=_stage, attempt=max_attempts,
                             elapsed_ms=int((time.monotonic() - _tb) * 1000),
                             input_chars=_input_chars, max_tokens=max_tokens,
                             end_reason="ok", endpoint="backup")
            return out
        except Exception as exc:
            _hs, _cls = _error_shape(exc)
            _record_llm_call(get_task_context(), stage=_stage, attempt=max_attempts,
                             elapsed_ms=int((time.monotonic() - _tb) * 1000),
                             input_chars=_input_chars, max_tokens=max_tokens,
                             http_status=_hs, error_class=_cls, end_reason="exhausted",
                             endpoint="backup")
            logger.error("Backup LLM async also failed: %s", str(exc)[:150])
    raise LLMCallError(f'LLM async call failed after {max_attempts} attempts') from last_error


async def _async_chat_once(client, url: str, payload: dict, headers: dict) -> dict:
    """发一次 chat 请求，返回响应体；流式时把 SSE 累积**还原成同形响应体**。

    流式的意义（实机复现）：网关在 ~60s 处切断**整段**非流式响应，报告/总结这类
    长生成（4096–8192 tokens 中文）因此必 504、重试、强制重做，任务跑不到终态；
    流式让字节持续到达，不再按整段耗时判超时。

    供应商不支持流式（400/404/405/415/422）时**如实回退**非流式，不当成端点故障。
    """
    import httpx
    # 注入的替身/自定义客户端可能只有 post（测试与私有部署会替换客户端）：
    # 没有 stream 就按非流式走，不因为"换了客户端"而整个调用失败
    if _stream_enabled() and hasattr(client, "stream"):
        acc = _new_stream_acc()
        try:
            async with client.stream("POST", url, json=_with_stream(payload, True),
                                     headers=headers) as resp:
                if resp.status_code >= 400:
                    await resp.aread()          # 流式响应必须先读完再抛，否则连接不释放
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    payload_line = _sse_payload(line)
                    if not payload_line:
                        continue
                    if payload_line == "[DONE]":
                        break
                    try:
                        _merge_stream_chunk(acc, json.loads(payload_line))
                    except Exception:
                        continue                # 单条坏 chunk 不废掉整段
            return _stream_result(acc)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _STREAM_UNSUPPORTED_CODES:
                raise
            logger.warning("流式请求被拒（HTTP %s），回退非流式",
                           exc.response.status_code)
    response = await client.post(url, json=_with_stream(payload, False),
                                 headers=headers)
    response.raise_for_status()
    return response.json()


async def _async_call_backup(payload: dict, fallback_model: str, expect_json: bool):
    """调用备用端点（async），标记健康状态，带清晰日志。"""
    client = _get_async_client()
    url = _endpoint_guard(_BACKUP_CFG["base_url"].rstrip("/") + "/chat/completions")
    b_headers = {
        "Authorization": f"Bearer {_BACKUP_CFG.get('api_key', '')}",
        "Content-Type": "application/json",
    }
    b_payload = dict(payload)
    b_payload["model"] = _BACKUP_CFG.get("model") or fallback_model
    try:
        response = await client.post(url, json=b_payload, headers=b_headers)
    except Exception as exc:
        _mark_endpoint("backup", False)
        raise
    if response.status_code in (401, 402, 403):
        _mark_endpoint("backup", False)
        _mark_auth_error(
            response.status_code,
            str(response.text)[:150],
            endpoint="backup",
        )
        raise LLMCallError(
            f"备用端点鉴权/余额错误 HTTP {response.status_code}"
        )
    try:
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        _mark_endpoint("backup", False)
        raise
    usage = data.get("usage") or {}
    _record_usage(
        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
        model=b_payload["model"],
    )
    _bump_task_budget(
        usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
        b_payload["model"],
    )
    _publish_usage_snapshot()
    content = data["choices"][0]["message"]["content"]
    if content is None or not str(content).strip():
        _mark_endpoint("backup", False)
        raise LLMEmptyResponseError("Backup LLM returned empty content")
    _mark_endpoint("backup", True)
    # P2-3：async 切换同样回填主端点根因（inherited_unhealthy 兜底）
    _record_task_degradation(
        get_task_context(), _primary_degradation_root(), both_failed=False,
    )
    _record_task_degradation(get_task_context(), "switch_to_backup", both_failed=False)
    logger.warning("Switched to backup LLM (async): %s", _BACKUP_CFG.get("base_url"))
    if expect_json:
        return _parse_json_content(content)
    return content


async def close_async_client():
    """Close the global async HTTP client."""
    global _async_client
    if _async_client and not _async_client.is_closed:
        await _async_client.aclose()
        _async_client = None
