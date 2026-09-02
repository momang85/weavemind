# -*- coding: utf-8 -*-
"""本地 LoRA 推理客户端（content_summary Worker 优先路由）。

设计：优先本地 QLoRA 微调模型（快、零 API 成本、离线可用），
本地服务不可达/超时/输出不合格时返回 None，由调用方回退云端 API。

开关：WM_USE_LOCAL_LORA=1（默认开）；WM_LOCAL_URL 覆盖服务地址。
"""
import json
import logging
import os
import socket
import urllib.request

logger = logging.getLogger(__name__)

LOCAL_URL = os.environ.get("WM_LOCAL_URL", "http://127.0.0.1:8765")
_ENABLED = os.environ.get("WM_USE_LOCAL_LORA", "1") != "0"


def _service_alive(timeout: float = 0.8) -> bool:
    """快速探活：TCP 连接检查（避免每次调用都等 HTTP 超时）。"""
    if not _ENABLED:
        return False
    try:
        host, _, port = LOCAL_URL.replace("http://", "").partition(":")
        with socket.create_connection((host, int(port or 8765)), timeout=timeout):
            return True
    except Exception:
        return False


def local_generate(instruction: str, max_tokens: int = 4096,
                   timeout: float = 180) -> dict | None:
    """本地 LoRA 生成 {summary, charts}；失败/超时/输出不合格返回 None。"""
    if not _service_alive():
        return None
    payload = json.dumps({
        "instruction": str(instruction or ""),
        "max_tokens": int(max_tokens),
    }, ensure_ascii=False).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{LOCAL_URL}/generate", data=payload,
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
    r = local_generate(sys.argv[1] if len(sys.argv) > 1 else "分析贵州茅台近五年营收趋势")
    print(json.dumps(r, ensure_ascii=False, indent=1) if r else "None (回退云端)")
