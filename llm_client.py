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
    "primary": {"healthy": True, "fails": 0},
    "backup": {"healthy": True, "fails": 0},
}
_endpoint_health_lock = threading.Lock()
_health_monitor_started = False
_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_cfg_mtime: float | None = None
_cfg_lock = threading.Lock()


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
    global _BACKUP_CFG
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


def _mark_endpoint(endpoint: str, ok: bool) -> None:
    with _endpoint_health_lock:
        st = _endpoint_health.setdefault(endpoint, {"healthy": True, "fails": 0})
        if ok:
            st["healthy"] = True
            st["fails"] = 0
        else:
            st["fails"] += 1
            st["healthy"] = st["fails"] < _ENDPOINT_FAIL_THRESHOLD


def _primary_healthy() -> bool:
    with _endpoint_health_lock:
        return _endpoint_health.get("primary", {}).get("healthy", True)


def get_endpoint_health() -> dict:
    with _endpoint_health_lock:
        return {
            k: dict(v) for k, v in _endpoint_health.items()
        }


def _probe_endpoint(base_url: str, api_key: str, model: str) -> bool:
    """健康探测：极短请求（4 token）验证端点可用。"""
    try:
        client = LLMClient(base_url=base_url, api_key=api_key, model=model)
        raw = client._send_request("你是连通性探测器，只回复：ok", "ping", 0.0, 8)
        return bool(raw and str(raw).strip())
    except Exception:
        return False


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


def _record_usage(prompt_tokens: int, completion_tokens: int, model: str = "") -> None:
    with _usage_lock:
        _usage["calls"] += 1
        _usage["prompt_tokens"] += int(prompt_tokens or 0)
        _usage["completion_tokens"] += int(completion_tokens or 0)
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
        _task_usage_client.hincrby(key, f"pt:{model or 'unknown'}", int(prompt_tokens or 0))
        _task_usage_client.hincrby(key, f"ct:{model or 'unknown'}", int(completion_tokens or 0))
        _task_usage_client.expire(key, 7200)
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


def _loads_loose(text: str) -> dict | list:
    """先严格解析，失败后允许字符串内未转义控制字符（LLM 常在长指令中插入字面换行）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text, strict=False)


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

    def call(
        self,
        system: str,
        user: str,
        *,
        expect_json: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
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

        # 配置热重载：前端保存新端点后，本进程无需重启即可生效
        _ensure_cfg_fresh()
        if self._is_planner:
            self.base_url = os.environ.get("PLANNER_LLM_BASE_URL") or self.base_url
            self.api_key = os.environ.get("PLANNER_LLM_API_KEY") or self.api_key
            self.model = os.environ.get("PLANNER_LLM_MODEL") or self.model
        else:
            self.base_url = os.environ.get("LLM_BASE_URL") or self.base_url
            self.api_key = os.environ.get("LLM_API_KEY") or self.api_key
            self.model = os.environ.get("LLM_MODEL") or self.model
        self._backup_cfg = (
            dict(_BACKUP_CFG)
            if _BACKUP_CFG.get("base_url") and _BACKUP_CFG.get("api_key")
            else {}
        )

        last_error: Exception | None = None
        # 健康路由（O-29）：主端点已被判定不健康 → 优先走备用，避免每次白白等待超时
        if not _primary_healthy():
            try:
                return self._call_backup(system, user, temp, max_tok, expect_json)
            except LLMJSONParseError:
                raise
            except Exception as exc:
                logger.warning("Health-routed backup failed: %s", str(exc)[:150])
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                raw = self._send_request(system, user, temp, max_tok)
                _mark_endpoint("primary", True)
                if not expect_json:
                    return {"content": raw}
                return self._parse_json(raw)
            except LLMJSONParseError:
                # JSON 解析失败不重试（格式问题重试没用）
                raise
            except Exception as exc:
                _mark_endpoint("primary", False)
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
                _mark_endpoint("backup", False)
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
            "model": self.model,
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
                model=self.model,
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


def call_llm(system: str, user: str, expect_json: bool = True) -> dict[str, Any]:
    """便捷函数：调用 LLM 并返回解析结果。

    Args:
        system: 系统提示词。
        user: 用户提示词。
        expect_json: 是否期望 JSON 响应。

    Returns:
        解析后的字典。
    """
    _ensure_cfg_fresh()
    return get_default_client().call(system, user, expect_json=expect_json)


def call_llm_stream(
    system: str,
    user: str,
    on_chunk=None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> str:
    """SSE 流式调用（同步，urllib）：逐块回调 on_chunk，并按任务上下文自动发布
    到 Redis stream:{task_id}（O-21 步骤级流式输出）。非流式端点回退整段输出。"""
    import urllib.error
    import urllib.request

    _ensure_cfg_fresh()
    client = get_default_client()
    temp = temperature if temperature is not None else client.temperature
    max_tok = max_tokens or client.max_tokens
    url = f"{client.base_url.rstrip('/')}/chat/completions"
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "WeaveMind/1.0",
    }
    if client.api_key:
        headers["Authorization"] = f"Bearer {client.api_key}"
    body = {
        "model": client.model,
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
) -> dict[str, Any] | str:
    """Async LLM call using httpx.AsyncClient with connection pooling.
    
    This enables true concurrency in async workers — multiple tasks
    can call the LLM simultaneously without blocking each other.
    """
    _ensure_cfg_fresh()
    api_key = os.environ.get('LLM_API_KEY', '')
    base_url = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')
    model = os.environ.get('LLM_MODEL', 'gpt-4')

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
                model=self.model,
            )
            _publish_usage_snapshot()

            content = data['choices'][0]['message']['content']
            if content is None or not str(content).strip():
                raise LLMEmptyResponseError("Empty content in LLM response")
            if expect_json:
                return _parse_json_content(content)
            return content

        except LLMEmptyResponseError as exc:
            last_error = exc
            logger.warning('LLM async empty response, retry %d/%d', attempt, max_attempts)
            await asyncio.sleep(1)
            continue

        except httpx.HTTPStatusError as exc:
            last_error = exc
            if exc.response.status_code == 429:
                backoff = min(2 ** attempt, 30)
                logger.warning('LLM async rate limited (429), retry %d/%d in %ds', attempt, max_attempts, backoff)
                await asyncio.sleep(backoff)
                continue
            if exc.response.status_code >= 500:
                logger.warning('LLM async server error, retry %d/%d', attempt, max_attempts)
                await asyncio.sleep(1)
                continue
            raise LLMCallError(f'LLM HTTP {exc.response.status_code}')

        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            last_error = exc
            logger.warning('LLM async timeout/connect, retry %d/%d', attempt, max_attempts)
            await asyncio.sleep(1)
            continue

    # 主端点失败 → 备用端点/模型（单次尝试）
    if _BACKUP_CFG and _BACKUP_CFG.get("base_url") and _BACKUP_CFG.get("api_key"):
        try:
            client = _get_async_client()
            url = _BACKUP_CFG["base_url"].rstrip("/") + "/chat/completions"
            b_headers = {
                "Authorization": f"Bearer {_BACKUP_CFG.get('api_key', '')}",
                "Content-Type": "application/json",
            }
            b_payload = dict(payload)
            b_payload["model"] = _BACKUP_CFG.get("model") or model
            response = await client.post(url, json=b_payload, headers=b_headers)
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") or {}
            _record_usage(
                usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                model=self.model,
            )
            _publish_usage_snapshot()
            content = data["choices"][0]["message"]["content"]
            if content is None or not str(content).strip():
                raise LLMEmptyResponseError("Backup LLM returned empty content")
            logger.warning("Switched to backup LLM (async): %s", _BACKUP_CFG.get("base_url"))
            if expect_json:
                return _parse_json_content(content)
            return content
        except Exception as exc:
            logger.error("Backup LLM async also failed: %s", str(exc)[:150])
    raise LLMCallError(f'LLM async call failed after {max_attempts} attempts') from last_error


async def close_async_client():
    """Close the global async HTTP client."""
    global _async_client
    if _async_client and not _async_client.is_closed:
        await _async_client.aclose()
        _async_client = None
