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


def _record_usage(prompt_tokens: int, completion_tokens: int) -> None:
    with _usage_lock:
        _usage["calls"] += 1
        _usage["prompt_tokens"] += int(prompt_tokens or 0)
        _usage["completion_tokens"] += int(completion_tokens or 0)


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
            _usage_pub_client = _redis.Redis(host="localhost", port=6379, decode_responses=True)
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

        last_error: Exception | None = None
        for attempt in range(1, self._MAX_RETRIES + 1):
            try:
                raw = self._send_request(system, user, temp, max_tok)
                if not expect_json:
                    return {"content": raw}
                return self._parse_json(raw)
            except LLMJSONParseError:
                # JSON 解析失败不重试（格式问题重试没用）
                raise
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "LLM call attempt %d/%d failed: %s",
                    attempt,
                    self._MAX_RETRIES,
                    exc,
                )
                if attempt < self._MAX_RETRIES:
                    time.sleep(self._RETRY_BASE * attempt)

        raise LLMCallError(
            f"LLM call failed after {self._MAX_RETRIES} attempts",
            attempt=self._MAX_RETRIES,
        ) from last_error

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
            with urllib.request.urlopen(req, timeout=180) as resp:
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
            _record_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
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
    return get_default_client().call(system, user, expect_json=expect_json)


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
) -> dict[str, Any] | str:
    """Async LLM call using httpx.AsyncClient with connection pooling.
    
    This enables true concurrency in async workers — multiple tasks
    can call the LLM simultaneously without blocking each other.
    """
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

    max_attempts = 3
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            client = _get_async_client()
            url = base_url.rstrip('/') + '/chat/completions'

            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage") or {}
            _record_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
            _publish_usage_snapshot()

            content = data['choices'][0]['message']['content']
            if expect_json:
                return _parse_json_content(content)
            return content

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

    raise LLMCallError(f'LLM async call failed after {max_attempts} attempts') from last_error


async def close_async_client():
    """Close the global async HTTP client."""
    global _async_client
    if _async_client and not _async_client.is_closed:
        await _async_client.aclose()
        _async_client = None
