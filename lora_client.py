# -*- coding: utf-8 -*-
"""本地 LoRA 推理客户端（content_summary Worker 优先路由）。

设计：优先本地 QLoRA 微调模型（快、零 API 成本、离线可用），
本地服务不可达/超时/输出不合格时返回 None，由调用方回退云端 API。

多 Worker 支持：server_url(name) 从 lora_servers.json 按名查端口，
各 Worker 用不同 WM_LOCAL_URL 指向自己的 LoRA 服务。

开关：WM_USE_LOCAL_LORA=1（默认开）；WM_LOCAL_URL 覆盖服务地址。
"""
import json
import logging
import os
import socket
import urllib.request

logger = logging.getLogger(__name__)

CONFIG_FILE = os.environ.get("WM_LORA_CONFIG", "lora_servers.json")
LOCAL_URL = os.environ.get("WM_LOCAL_URL", "http://127.0.0.1:8765")
_ENABLED = os.environ.get("WM_USE_LOCAL_LORA", "1") != "0"
# 系统 LLM 模式（web_ui 切换）："cloud"=全商业 API；"hybrid"=本地 LoRA 参与部分 Worker。
# 优先级：环境变量 WM_LLM_MODE > Redis(llm_mode) > config.json(system.llm_mode) > hybrid(默认)
_mode_cache: dict = {"mode": None, "ts": 0.0}

_cache = {"url": LOCAL_URL}


def _mode(timeout: float = 0.5) -> str:
    """读取当前 LLM 模式（带 2s TTL 缓存，避免每次调用都查 Redis）。"""
    import time as _t
    now = _t.time()
    if _mode_cache["mode"] is not None and now - _mode_cache["ts"] < 2.0:
        return _mode_cache["mode"]
    mode = ""
    try:
        env_mode = os.environ.get("WM_LLM_MODE", "")
        if env_mode:
            mode = env_mode
    except Exception:
        pass
    if not mode:
        try:
            import redis as _redis
            r = _redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True, socket_connect_timeout=timeout,
                socket_timeout=timeout,
            )
            mode = str(r.get("llm_mode") or "")
        except Exception:
            mode = ""
    if not mode:
        # 回退 config.json（无 Redis 环境，如测试）
        try:
            with open("config.json", encoding="utf-8") as f:
                cfg = json.load(f)
            mode = str((cfg.get("system") or {}).get("llm_mode") or "")
        except Exception:
            mode = ""
    if mode not in ("cloud", "hybrid"):
        mode = "hybrid"
    _mode_cache.update({"mode": mode, "ts": now})
    return mode


def llm_mode() -> str:
    """外部可读：当前模式（cloud=全商业 API / hybrid=本地 LoRA 混合）。"""
    return _mode()


def server_url(name: str) -> str:
    """按 Worker 名查 LoRA 服务地址（从 lora_servers.json）。"""
    name = str(name or "")
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        servers = cfg.get("servers") or {}
        s = servers.get(name) if isinstance(servers, dict) else None
        if s and s.get("enabled", True):
            port = int(s.get("port") or 0)
            if port > 0:
                return f"http://127.0.0.1:{port}"
    except Exception:
        pass
    return LOCAL_URL


def set_server(name: str) -> None:
    """当前客户端指向指定 Worker 的 LoRA 服务。"""
    _cache["url"] = server_url(name)


def _service_alive(timeout: float = 0.8) -> bool:
    """快速探活：TCP 连接检查（避免每次调用都等 HTTP 超时）。
    cloud 模式（全商业 API）→ 直接 False（不走本地）。"""
    if not _ENABLED:
        return False
    if _mode() == "cloud":
        return False
    url = _cache["url"]
    try:
        host, _, port = url.replace("http://", "").partition(":")
        with socket.create_connection((host, int(port or 8765)), timeout=timeout):
            return True
    except Exception:
        return False


def local_generate(instruction: str, max_tokens: int = 4096,
                   timeout: float = 180) -> dict | None:
    """本地 LoRA 生成 {summary, charts, sources}；失败/超时/输出不合格返回 None。"""
    if not _service_alive():
        return None
    payload = json.dumps({
        "instruction": str(instruction or ""),
        "max_tokens": int(max_tokens),
    }, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{_cache['url']}/generate", data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != "success":
            return None
        summary = str(data.get("summary") or "").strip()
        charts = data.get("charts") if isinstance(data.get("charts"), list) else []
        sources = data.get("sources") if isinstance(data.get("sources"), list) else []
        if len(summary) < 20:
            logger.info("Local LoRA output too short (%d chars), fallback cloud", len(summary))
            return None
        return {"summary": summary, "charts": charts, "sources": sources}
    except Exception as exc:
        logger.info("Local LoRA unavailable (%s), fallback cloud", str(exc)[:80])
        return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--server":
        print(server_url(sys.argv[2] if len(sys.argv) > 2 else "content_summary"))
        sys.exit(0)
    r = local_generate(sys.argv[1] if len(sys.argv) > 1 else "分析贵州茅台近五年营收趋势")
    print(json.dumps(r, ensure_ascii=False, indent=1) if r else "None (回退云端)")
