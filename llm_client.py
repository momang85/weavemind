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

logger = logging.getLogger(__name__)


# ============================================================================
# Token 用量统计（可观测性）
# ============================================================================

_usage_lock = threading.Lock()
_usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
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

_ENDPOINT_FAIL_THRESHOLD = 2


def _mark_endpoint(endpoint: str, ok: bool, reason: str = "") -> None:
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
            )
        key = f"llm_degradation:{task_id}"
        _task_usage_client.rpush(key, json.dumps({
            "ts": time.time(),
            "reason": reason,
            "both_failed": both_failed,
        }, ensure_ascii=False))
        _task_usage_client.ltrim(key, -50, -1)
        _task_usage_client.expire(key, 7200)
    except Exception:
        pass


def get_task_llm_degradation(task_id: str) -> dict:
    """读取任务的 LLM 降级汇总：{switches, reasons, both_failed, events}。"""
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
        for ev in events:
            r = str(ev.get("reason") or "")
            if r == "switch_to_backup":
                switches += 1
            elif r and r not in reasons:
                reasons.append(r)
            if ev.get("both_failed"):
                both_failed = True
        return {
            "switches": switches,
            "reasons": reasons,
            "both_failed": both_failed,
            "events": events[-10:],
        }
    except Exception:
        return {}


def _mark_auth_error(code: int, body: str) -> None:
    """记录鉴权/余额错误（401/402/403），供编排器/前端提醒用户检查 API 配置。"""
    with _auth_error_lock:
        _last_auth_error["ts"] = time.time()
        _last_auth_error["message"] = (
            f"LLM 端点鉴权/余额错误 HTTP {code}：{body[:150]}"
        )


def get_endpoint_warning() -> str:
    """返回 10 分钟内最近一次鉴权/余额错误消息（空串=无）。"""
    with _auth_error_lock:
        if _last_auth_error["ts"] and time.time() - _last_auth_error["ts"] < 600:
            return _last_auth_error["message"]
        return ""


def _primary_healthy() -> bool:
    with _endpoint_health_lock:
        return _endpoint_health.get("primary", {}).get("healthy", True)


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


def _probe_endpoint_status(base_url: str, api_key: str, model: str) -> dict:
    """余额感知探测：极短请求（max_tokens=1）验证端点可用，
    返回 {ok, reason}；reason ∈ ok/insufficient_balance/unauthorized/unreachable。"""
    try:
        client = LLMClient(base_url=base_url, api_key=api_key, model=model)
        raw = client._send_request("你是连通性探测器，只回复：ok", "ping", 0.0, 1)
        if bool(raw and str(raw).strip()):
            return {"ok": True, "reason": "ok"}
        return {"ok": False, "reason": "unreachable"}
    except Exception as exc:
        return {"ok": False, "reason": _classify_probe_error(exc)}


def _probe_endpoint(base_url: str, api_key: str, model: str) -> bool:
    """健康探测布尔包装（后台守护线程/旧调用兼容）。"""
    return _probe_endpoint_status(base_url, api_key, model)["ok"]


_BALANCE_CACHE_TTL = 30.0
_balance_cache_lock = threading.Lock()
_balance_cache = {"ts": 0.0, "data": None}


def _clear_balance_cache() -> None:
    """清空余额预检缓存（配置热更新/测试后调用）。"""
    with _balance_cache_lock:
        _balance_cache["ts"] = 0.0
        _balance_cache["data"] = None


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
            and now - _balance_cache["ts"] < _BALANCE_CACHE_TTL
        ):
            return {
                k: dict(v) for k, v in _balance_cache["data"].items()
            }
    result: dict = {}
    primary_base = os.environ.get("LLM_BASE_URL") or ""
    if primary_base:
        st = _probe_endpoint_status(
            primary_base,
            os.environ.get("LLM_API_KEY") or "",
            os.environ.get("LLM_MODEL") or "gpt-4o",
        )
        _mark_endpoint("primary", st["ok"], st["reason"])
        result["primary"] = st
    else:
        result["primary"] = {"ok": False, "reason": "unreachable"}
    if _BACKUP_CFG.get("base_url"):
        st = _probe_endpoint_status(
            _BACKUP_CFG.get("base_url", ""),
            _BACKUP_CFG.get("api_key", ""),
            _BACKUP_CFG.get("model") or "gpt-4o",
        )
        _mark_endpoint("backup", st["ok"], st["reason"])
        result["backup"] = st
    else:
        result["backup"] = {"ok": False, "reason": "unreachable"}
    with _balance_cache_lock:
        _balance_cache["ts"] = time.time()
        _balance_cache["data"] = result
    return {k: dict(v) for k, v in result.items()}


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
    """后台守护：主端点不健康时持续探测，恢复后自动切回。"""
    while True:
        time.sleep(interval)
        try:
            if _primary_healthy():
                continue
            base_url = os.environ.get("LLM_BASE_URL") or ""
            api_key = os.environ.get("LLM_API_KEY") or ""
            model = os.environ.get("LLM_MODEL") or "gpt-4o"
            ok = bool(base_url) and _probe_endpoint(base_url, api_key, model)
            _mark_endpoint("primary", ok)
            if ok:
                logger.info("LLM primary endpoint recovered, traffic switched back")
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
            )
        key = f"stream:{tid}"
        _task_usage_client.rpush(key, text)
        _task_usage_client.ltrim(key, -4000, -1)
        _task_usage_client.expire(key, 600)
    except Exception:
        pass


def _record_usage(
    prompt_tokens: int, completion_tokens: int,
    model: str = "", cached: bool = False,
) -> None:
    """记录一次 LLM 调用用量。
    cached=True 表示缓存命中：只记调用次数与 cached 标记，不计 token 成本。"""
    with _usage_lock:
        _usage["calls"] += 1
        _usage["prompt_tokens"] += int(prompt_tokens or 0)
        _usage["completion_tokens"] += int(completion_tokens or 0)
        if cached:
            _usage["cached"] = _usage.get("cached", 0) + 1
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


# ---------------------------------------------------------------------------
# B1 模型解析 + B2 缓存工具
# ---------------------------------------------------------------------------


def get_model_for_usage(usage: str = "", default_model: str = "") -> str:
    """按调用用途解析模型名：llm.model_roles[用途] > default_model > LLM_MODEL。
    未配置用途或缺省回退到默认模型，保证老调用行为不变。"""
    _ensure_cfg_fresh()
    role = MODEL_ROLES.get(str(usage or "").lower(), "")
    if role:
        m = _MODEL_ROLES_CFG.get(role)
        if m:
            return str(m)
    return default_model or os.environ.get("LLM_MODEL") or ""


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


def _loads_loose(text: str) -> dict | list:
    """先严格解析，失败后允许字符串内未转义控制字符（LLM 常在长指令中插入字面换行）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text, strict=False)


def _parse_json_content(raw: str) -> dict[str, Any]:
    """模块级 JSON 解析（async 路径使用）：兼容 markdown 代码块与前后缀文本。"""
    try:
        result = _loads_loose(raw)
        if isinstance(result, dict):
            return result
        if isinstance(result, list):
            return {"items": result}
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", raw, re.DOTALL)
    if m:
        try:
            result = _loads_loose(m.group(1))
            if isinstance(result, dict):
                return result
            if isinstance(result, list):
                return {"items": result}
        except json.JSONDecodeError:
            pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return _loads_loose(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end > start:
        try:
            result = _loads_loose(raw[start:end + 1])
            if isinstance(result, list):
                return {"items": result}
        except json.JSONDecodeError:
            pass
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
    _MAX_RETRIES: int = 1
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
        if not _primary_healthy():
            try:
                return self._call_backup(system, user, temp, max_tok, expect_json)
            except LLMJSONParseError:
                raise
            except Exception as exc:
                _mark_endpoint("backup", False, _degradation_reason(exc))
                _record_task_degradation(
                    get_task_context(), _degradation_reason(exc), both_failed=True,
                )
                logger.warning("Health-routed backup failed: %s", str(exc)[:150])
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                raw = self._send_request(system, user, temp, max_tok, model=model)
                _mark_endpoint("primary", True)
                if not expect_json:
                    result: dict[str, Any] = {"content": raw}
                else:
                    result = self._parse_json(raw)
                _cache_set(cache_key, user, result)
                return result
            except LLMJSONParseError:
                # JSON 解析失败不重试（格式问题重试没用）
                raise
            except Exception as exc:
                _reason = _degradation_reason(exc)
                _mark_endpoint("primary", False, _reason)
                _record_task_degradation(get_task_context(), _reason, both_failed=False)
                last_error = exc
                logger.warning(
                    "LLM call attempt %d/%d failed: %s",
                    attempt,
                    self._MAX_RETRIES,
                    exc,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._RETRY_BASE * attempt)

        # 主端点失败 → 自动切换备用端点/模型
        if self._backup_cfg:
            try:
                return self._call_backup(system, user, temp, max_tok, expect_json)
            except LLMJSONParseError:
                raise
            except Exception as exc:
                _reason = _degradation_reason(exc)
                _mark_endpoint("backup", False, _reason)
                _record_task_degradation(get_task_context(), _reason, both_failed=True)
                logger.error("Backup LLM also failed: %s", str(exc)[:200])
        raise LLMCallError(
            f"LLM call failed after {self._MAX_RETRIES} attempts",
            attempt=self._MAX_RETRIES,
        ) from last_error

    def _call_backup(
        self, system: str, user: str, temperature: float, max_tokens: int,
        expect_json: bool,
    ) -> dict[str, Any]:
        """调用备用端点并标记健康状态。"""
        if not self._backup_cfg:
            raise LLMCallError("No backup endpoint configured")
        backup = LLMClient(
            base_url=self._backup_cfg.get("base_url"),
            api_key=self._backup_cfg.get("api_key"),
            model=self._backup_cfg.get("model") or self.model,
        )
        raw = backup._send_request(system, user, temperature, max_tokens)
        _mark_endpoint("backup", True)
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
    ) -> str:
        """发送 HTTP 请求到 LLM 服务。

        Args:
            system: 系统提示词。
            user: 用户提示词。
            temperature: 温度。
            max_tokens: 最大 token。

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

        try:
            timeout = float(os.environ.get("LLM_REQUEST_TIMEOUT", "60") or 60)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                response_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if exc.code in (401, 402, 403):
                _mark_auth_error(exc.code, error_body)
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
            raise LLMCallError("Empty content in LLM response")

        # 记录用量
        usage = response_data.get("usage", {})
        if usage:
            _record_usage(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                model=model or self.model,
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
        """从 LLM 响应中提取 JSON。

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
        # 尝试1: 直接解析
        try:
            result = _loads_loose(raw)
            if isinstance(result, dict):
                return result
            # 如果是列表，包装
            if isinstance(result, list):
                return {"items": result}
        except json.JSONDecodeError:
            pass

        # 尝试2: 提取 ```json ... ``` 代码块
        m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", raw, re.DOTALL)
        if m:
            try:
                result = _loads_loose(m.group(1))
                if isinstance(result, dict):
                    return result
                if isinstance(result, list):
                    return {"items": result}
            except json.JSONDecodeError:
                pass

        # 尝试3: 查找第一个 { 和最后一个 }
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return _loads_loose(raw[start : end + 1])
            except json.JSONDecodeError:
                pass

        # 尝试4: 查找第一个 [ 和最后一个 ]
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                result = _loads_loose(raw[start : end + 1])
                if isinstance(result, list):
                    return {"items": result}
            except json.JSONDecodeError:
                pass

        raise LLMJSONParseError(
            f"Failed to parse LLM response as JSON. Raw: {raw[:500]}..."
        )


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
    return get_default_client().call(
        system, user, expect_json=expect_json,
        usage=usage, model_override=model_override, cache_key=cache_key,
    )


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
    到 Redis stream:{task_id}（O-21 步骤级流式输出）。非流式端点回退整段输出。"""
    import urllib.error
    import urllib.request

    _ensure_cfg_fresh()
    client = get_default_client()
    temp = temperature if temperature is not None else client.temperature
    max_tok = max_tokens or client.max_tokens
    # B1：流式执行调用也按用途选择模型（如 exec）
    model = model_override or get_model_for_usage(usage, client.model)
    url = f"{client.base_url.rstrip('/')}/chat/completions"
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "WeaveMind/1.0",
    }
    if client.api_key:
        headers["Authorization"] = f"Bearer {client.api_key}"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temp,
        "max_tokens": max_tok,
        "stream": True,
    }
    req = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        timeout = float(os.environ.get("LLM_REQUEST_TIMEOUT", "180") or 180)
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
                delta = obj.get("choices", [{}])[0].get("delta", {}).get("content", "")
            except Exception:
                delta = ""
            if delta:
                chunks.append(delta)
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
            _publish_stream_chunk(str(content))
            if on_chunk is not None:
                try:
                    on_chunk(str(content))
                except Exception:
                    pass
        except Exception:
            chunks.append(body_text)
    return "".join(chunks)


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
        timeout = httpx.Timeout(120.0, connect=10.0)
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

    for attempt in range(1, max_attempts + 1):
        try:
            client = _get_async_client()
            url = base_url.rstrip('/') + '/chat/completions'

            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") or {}
            _record_usage(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                model=model,
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
            return result

        except LLMEmptyResponseError as exc:
            last_error = exc
            logger.warning('LLM async empty response, retry %d/%d', attempt, max_attempts)
            await asyncio.sleep(1)
            continue

        except httpx.HTTPStatusError as exc:
            last_error = exc
            _mark_endpoint("primary", False)
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
                )
                last_error = LLMCallError(
                    f"主端点鉴权/余额错误 HTTP {exc.response.status_code}"
                )
                break
            raise LLMCallError(f'LLM HTTP {exc.response.status_code}')

        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
            _mark_endpoint("primary", False)
            logger.warning('LLM async timeout/connect, retry %d/%d', attempt, max_attempts)
            await asyncio.sleep(1)
            continue

    # 主端点失败 → 备用端点/模型（单次尝试）
    if _BACKUP_CFG and _BACKUP_CFG.get("base_url") and _BACKUP_CFG.get("api_key"):
        try:
            return await _async_call_backup(payload, model, expect_json)
        except Exception as exc:
            logger.error("Backup LLM async also failed: %s", str(exc)[:150])
    raise LLMCallError(f'LLM async call failed after {max_attempts} attempts') from last_error


async def _async_call_backup(payload: dict, fallback_model: str, expect_json: bool):
    """调用备用端点（async），标记健康状态，带清晰日志。"""
    client = _get_async_client()
    url = _BACKUP_CFG["base_url"].rstrip("/") + "/chat/completions"
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
    _publish_usage_snapshot()
    content = data["choices"][0]["message"]["content"]
    if content is None or not str(content).strip():
        _mark_endpoint("backup", False)
        raise LLMEmptyResponseError("Backup LLM returned empty content")
    _mark_endpoint("backup", True)
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
