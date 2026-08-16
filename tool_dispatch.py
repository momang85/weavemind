# -*- coding: utf-8 -*-
"""工具派发与审计（MCP-lite / React Agent 共用，对标标准 3.1）。

- dispatch_tool：把单步工具调用经 Redis 派发给现有 worker 并等待结果；
- 每次调用写审计日志（入参/出参哈希 + 耗时 + 状态），供可观测与评测。
"""

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

AUDIT_FILE = Path(__file__).resolve().parent / "logs" / "tool_audit.jsonl"


def _redis():
    import redis as _redis
    return _redis.Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        decode_responses=True,
        socket_connect_timeout=5,
    )


def find_agent(capability: str) -> str | None:
    """按能力找 worker：Redis 注册表优先，SQLite 兜底。"""
    try:
        from common import AgentRegistry, RedisAgentRegistry
        r = _redis()
        ra = RedisAgentRegistry(r)
        agent = ra.find_capable_agent(capability)
        if agent:
            return agent
    except Exception:
        pass
    try:
        from common import AgentRegistry
        reg = AgentRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
        return reg.find_capable_agent(capability)
    except Exception:
        return None


def dispatch_tool(
    capability: str,
    instruction: str,
    task_id: str = "",
    timeout: int = 300,
    workspace: str = "",
) -> dict:
    """派发单工具调用并等待结果；无论成败都写审计日志。"""
    t0 = time.time()
    # 第三方 MCP 工具优先路由（即插即用）
    try:
        from mcp_client import call_external_tool
        ext = call_external_tool(capability, {"instruction": str(instruction),
                                              "task_id": task_id, "timeout": timeout})
        if ext is not None:
            _audit(capability, instruction, ext, time.time() - t0, task_id)
            return ext
    except Exception:
        pass
    agent = find_agent(capability)
    if not agent:
        return {"task_id": "?", "status": "FAILED", "result": f"No worker for {capability}"}
    r = _redis()
    dispatch_id = f"tool-{uuid.uuid4().hex[:8]}"
    r.lpush(f"task_queue:{agent}", json.dumps({
        "task_id": dispatch_id,
        "instruction": str(instruction),
        "task_start_ts": time.time(),
        "workspace": str(workspace),
        "simple": False,
    }, ensure_ascii=False))
    deadline = time.time() + max(10, int(timeout))
    result = None
    while time.time() < deadline:
        try:
            msg = r.brpop(f"task_result:{dispatch_id}", timeout=2)
        except Exception:
            msg = None
        if msg:
            try:
                result = json.loads(msg[1])
            except Exception:
                result = {"status": "FAILED", "result": f"bad result: {msg[1][:100]}"}
            break
    if result is None:
        result = {"task_id": dispatch_id, "status": "FAILED", "result": "tool call timeout"}
    _audit(capability, instruction, result, time.time() - t0, task_id)
    return result


def _audit(capability, instruction, result, duration, task_id=""):
    """审计日志：入参/出参哈希 + 状态 + 耗时。"""
    try:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "task_id": str(task_id)[:40],
            "capability": capability,
            "instr_hash": hashlib.sha256(str(instruction).encode("utf-8", errors="replace")).hexdigest()[:12],
            "result_hash": hashlib.sha256(str(result.get("result", "")).encode("utf-8", errors="replace")).hexdigest()[:12],
            "status": result.get("status"),
            "duration_ms": int(duration * 1000),
        }
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_audit(limit: int = 50) -> list[dict]:
    if not AUDIT_FILE.exists():
        return []
    lines = AUDIT_FILE.read_text(encoding="utf-8").splitlines()
    out = []
    for l in lines[-limit:]:
        if l.strip():
            try:
                out.append(json.loads(l))
            except Exception:
                pass
    return out
