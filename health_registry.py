# -*- coding: utf-8 -*-
"""外部依赖健康注册表：把散落的健康源收成一个统一视图。

改造前的状态：5 套健康源各自为政，字段与存储方式都不一样
（`llm_health` 进程内 dict、`search_health` Redis 键、`source_health` 进程内 dict、
`embed_health` Redis 键、`code_sandbox` 现算），字段名在
`healthy`/`ok`/`degraded`/`cooldown_until` 之间混用，而且 orchestrator 与 webui
是**两个进程**——`/api/status` 读不到编排侧的进程内状态（如行情适配器熔断）。

统一字段（每个依赖一条）：
    {name, ok, reason, since, source_process, detail}
"""

from __future__ import annotations

import json
import os
import time

# 依赖展示名
_NAME_LLM = "llm"
_NAME_SEARCH = "search"
_NAME_SOURCE = "market_source"
_NAME_EMBEDDING = "embedding"
_NAME_SANDBOX = "code_sandbox"
_NAME_PLANNER = "planner"
_NAME_MCP = "mcp"
_NAME_LORA = "lora"

# 跨进程快照键（由拥有该状态的进程写入）
SEARCH_HEALTH_KEY = "search_engine_health"
SOURCE_HEALTH_KEY = "wm:source:health"
EMBED_HEALTH_KEY = "wm:embed:health"


def _redis_get(key: str) -> dict:
    try:
        import redis
        client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379") or 6379),
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
        )
        raw = client.get(key)
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _entry(name: str, ok: bool, reason: str = "", since: float = 0.0,
           detail: str = "") -> dict:
    return {
        "name": name,
        "ok": bool(ok),
        "reason": str(reason or "")[:200],
        "since": float(since or 0.0),
        "source_process": "webui" if _in_webui() else "orchestrator",
        "detail": str(detail or "")[:300],
    }


def _in_webui() -> bool:
    try:
        import sys
        return "web_ui" in (sys.argv[0] or "")
    except Exception:
        return False


def probe_llm() -> dict:
    """LLM 端点（进程内健康状态 + 余额探测结果）。"""
    try:
        from llm_client import get_endpoint_health, get_endpoint_warning
        health = get_endpoint_health() or {}
        primary = health.get("primary") or {}
        backup = health.get("backup") or {}
        ok = bool(primary.get("healthy")) or bool(backup.get("healthy"))
        reason = get_endpoint_warning() or ""
        if not ok:
            reason = reason or "主备端点均不健康"
        since = max(
            float(primary.get("last_degradation_ts") or 0.0),
            float(backup.get("last_degradation_ts") or 0.0),
        )
        return _entry(_NAME_LLM, ok, reason, since,
                      detail=f"primary={'ok' if primary.get('healthy') else 'down'},"
                             f" backup={'ok' if backup.get('healthy') else 'down'}")
    except Exception as exc:
        return _entry(_NAME_LLM, True, f"状态不可读：{str(exc)[:80]}")


def probe_search() -> dict:
    """搜索引擎健康（worker 写 Redis 快照，webui 也能读到）。"""
    data = _redis_get(SEARCH_HEALTH_KEY)
    if not data:
        return _entry(_NAME_SEARCH, True, "无快照（视为未知）", detail="no snapshot")
    engines = {k: v for k, v in data.items() if isinstance(v, dict)}
    unhealthy = [k for k, v in engines.items() if not v.get("healthy", True)]
    return _entry(
        _NAME_SEARCH, not unhealthy,
        "" if not unhealthy else "引擎不健康：" + ", ".join(unhealthy),
        detail=", ".join(
            f"{k}={'ok' if v.get('healthy', True) else 'down'}" for k, v in engines.items()
        ),
    )


def probe_market_source() -> dict:
    """行情适配器熔断状态（跨进程快照优先，缺失时回退本进程）。"""
    data = _redis_get(SOURCE_HEALTH_KEY)
    if not data:
        try:
            from adapters.source_health import get_health
            data = get_health() or {}
        except Exception:
            data = {}
    if not data:
        return _entry(_NAME_SOURCE, True, "无快照（视为未知）", detail="no snapshot")
    cooling = [
        f"{name}({info.get('reason') or 'cooling'})"
        for name, info in data.items()
        if isinstance(info, dict) and info.get("cooldown_until", 0) > time.time()
    ]
    return _entry(_NAME_SOURCE, not cooling,
                  "" if not cooling else "熔断冷却：" + ", ".join(cooling),
                  detail=f"{len(data)} 个数据源")


def probe_embedding() -> dict:
    """Embedding 接口（额度/网络）健康：降级会让记忆与提示词经验检索失效。"""
    try:
        import embed_health
        health = embed_health.embedding_health()
        return _entry(
            _NAME_EMBEDDING, bool(health.get("healthy")),
            "" if health.get("healthy") else str(health.get("last_error") or "降级")[:120],
            since=float(health.get("degraded_since") or 0.0),
            detail=f"连续失败 {health.get('fails')} 次（阈值 {health.get('threshold')}）",
        )
    except Exception as exc:
        return _entry(_NAME_EMBEDDING, True, f"状态不可读：{str(exc)[:80]}")


def probe_sandbox() -> dict:
    """代码沙箱可用性（现算）。"""
    try:
        from code_sandbox import sandbox_status
        status = sandbox_status() or {}
        mode = str(status.get("mode") or "unknown")
        docker = bool(status.get("docker_available"))
        return _entry(_NAME_SANDBOX, mode != "disabled",
                      "" if mode != "disabled" else "沙箱不可用",
                      detail=f"mode={mode}, docker={'yes' if docker else 'no'}")
    except Exception as exc:
        return _entry(_NAME_SANDBOX, True, f"状态不可读：{str(exc)[:80]}")


def _config_section(name: str) -> dict:
    """读 config.json 的某个段（探测器要便宜且无副作用，不走 env 热重载）。"""
    try:
        import json
        import os as _os
        path = _os.environ.get("WEAVEMIND_CONFIG") or _os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)), "config.json")
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        section = cfg.get(name)
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def probe_planner() -> dict:
    """规划专用端点（planner.*）的配置状态。

    注意：本函数会被 `/api/status` 每 3 秒调用，**不做真实请求**（真实连通性由
    `/api/config/test` 触发）。此前余额探测只覆盖 primary/backup，规划器端点配错
    时表现为"规划莫名失败/静默回退"，设置页需要能看到它到底配了没有。"""
    try:
        import os as _os
        planner = _config_section("planner")
        base = _os.environ.get("PLANNER_LLM_BASE_URL") or str(planner.get("base_url") or "")
        key = _os.environ.get("PLANNER_LLM_API_KEY") or str(planner.get("api_key") or "")
        model = _os.environ.get("PLANNER_LLM_MODEL") or str(planner.get("model") or "")
        if not (base and key):
            return _entry(_NAME_PLANNER, True, "未单独配置（规划复用主端点）",
                          detail="inherits primary")
        return _entry(_NAME_PLANNER, True, "", detail=f"base={base[:60]}, model={model or '-'}")
    except Exception as exc:
        return _entry(_NAME_PLANNER, True, f"状态不可读：{str(exc)[:80]}")


def probe_mcp() -> dict:
    """MCP 外部工具（Wind/iFinD 等）的配置状态（不做握手，握手在测试端点里做）。"""
    try:
        servers = _config_section("mcp_servers")
        if not servers:
            try:
                import json
                import os as _os
                path = _os.environ.get("WEAVEMIND_CONFIG") or _os.path.join(
                    _os.path.dirname(_os.path.abspath(__file__)), "config.json")
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh).get("mcp_servers")
                servers = raw if isinstance(raw, list) else []
            except Exception:
                servers = []
    except Exception:
        servers = []
    # 只统计"真正有 name 且带 command/url"的条目（模板里的 _comment 项不算）
    names = [
        str(s.get("name")) for s in servers
        if isinstance(s, dict) and s.get("name") and (s.get("command") or s.get("url"))
    ]
    if not names:
        return _entry(_NAME_MCP, True, "未配置 MCP 服务", detail="no servers")
    return _entry(_NAME_MCP, True, "", detail="已配置：" + ", ".join(names))


def probe_lora() -> dict:
    """本地 LoRA 服务状态（只读配置与模式；TCP 探活在测试端点里做）。"""
    try:
        from lora_client import llm_mode, LOCAL_URL
        mode = llm_mode()
        if mode == "cloud":
            return _entry(_NAME_LORA, True, "cloud 模式下不使用本地服务",
                          detail=f"mode={mode}")
        return _entry(_NAME_LORA, True, "",
                      detail=f"mode={mode}, url={LOCAL_URL}（可用性请点「测试」）")
    except Exception as exc:
        return _entry(_NAME_LORA, True, f"状态不可读：{str(exc)[:80]}")


def live_probe(target: str) -> dict:
    """真实连通性探测（仅供 `POST /api/config/test` 调用，带网络开销）。

    target ∈ llm / planner / backup / embedding / mcp / lora。
    返回 {target, ok, reason, latency_ms, detail}。
    """
    started = time.time()

    def _wrap(ok: bool, reason: str = "", detail: str = "") -> dict:
        return {
            "target": str(target), "ok": bool(ok), "reason": str(reason)[:200],
            "detail": str(detail)[:300],
            "latency_ms": int((time.time() - started) * 1000),
        }

    try:
        if target in ("llm", "planner", "backup"):
            return _live_probe_llm(target)
        if target == "embedding":
            return _live_probe_embedding()
        if target == "mcp":
            return _live_probe_mcp()
        if target == "lora":
            return _live_probe_lora()
    except Exception as exc:
        return _wrap(False, f"探测异常：{str(exc)[:120]}")
    return _wrap(False, f"未知探测目标：{target}")


def _live_probe_llm(target: str) -> dict:
    """LLM 类端点：极短请求（max_tokens=1）+ 余额感知归类。"""
    started = time.time()

    def _wrap(ok: bool, reason: str = "", detail: str = "") -> dict:
        return {"target": target, "ok": bool(ok), "reason": str(reason)[:200],
                "detail": str(detail)[:300],
                "latency_ms": int((time.time() - started) * 1000)}

    import os as _os
    from llm_client import _probe_endpoint_status
    if target == "llm":
        base = _os.environ.get("LLM_BASE_URL") or _config_section("llm").get("base_url") or ""
        key = _os.environ.get("LLM_API_KEY") or _config_section("llm").get("api_key") or ""
        model = _os.environ.get("LLM_MODEL") or _config_section("llm").get("model") or ""
    elif target == "planner":
        planner = _config_section("planner")
        base = _os.environ.get("PLANNER_LLM_BASE_URL") or str(planner.get("base_url") or "")
        key = _os.environ.get("PLANNER_LLM_API_KEY") or str(planner.get("api_key") or "")
        model = _os.environ.get("PLANNER_LLM_MODEL") or str(planner.get("model") or "")
        if not (base and key):
            return _wrap(True, "未单独配置", "规划复用主端点，无需单独测试")
    else:  # backup
        backup = _config_section("backup")
        base = str(backup.get("base_url") or "")
        key = str(backup.get("api_key") or "")
        model = str(backup.get("model") or "")
    if not (base and key):
        return _wrap(False, "未配置", "缺少 base_url 或 api_key")
    result = _probe_endpoint_status(base, key, model)
    ok = bool(result.get("ok"))
    reason = "" if ok else str(result.get("reason") or "unreachable")
    return _wrap(ok, reason, f"base={base[:60]}, model={model or '-'}")


def _live_probe_embedding() -> dict:
    """Embedding 端点：发一条极小 embeddings 请求，区分欠费/鉴权/不可达。"""
    started = time.time()

    def _wrap(ok: bool, reason: str = "", detail: str = "") -> dict:
        return {"target": "embedding", "ok": bool(ok), "reason": str(reason)[:200],
                "detail": str(detail)[:300],
                "latency_ms": int((time.time() - started) * 1000)}

    import json as _json
    import os as _os
    import urllib.error
    import urllib.request
    from llm_client import _classify_probe_error

    section = _config_section("embedding")
    base = (_os.environ.get("EMBEDDING_BASE_URL") or str(section.get("base_url") or "")
            or _os.environ.get("LLM_BASE_URL") or "")
    key = (_os.environ.get("EMBEDDING_API_KEY") or str(section.get("api_key") or "")
           or _os.environ.get("LLM_API_KEY") or "")
    model = (_os.environ.get("EMBEDDING_MODEL") or str(section.get("model") or "")
             or "BAAI/bge-large-zh-v1.5")
    if not base:
        return _wrap(False, "未配置", "缺少 base_url")
    url = base.rstrip("/") + "/embeddings"
    body = _json.dumps({"model": model, "input": ["ping"]}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(
            req, timeout=float(_os.environ.get("LLM_PROBE_TIMEOUT", "20") or 20)
        ) as resp:
            raw = resp.read()
        ok = bool(raw)
        return _wrap(ok, "" if ok else "空响应", f"base={base[:60]}, model={model}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        reason = _classify_probe_error(
            RuntimeError(f"HTTP {exc.code}: {detail}"))
        return _wrap(False, reason, detail)
    except Exception as exc:
        return _wrap(False, _classify_probe_error(exc), str(exc)[:200])


def _live_probe_mcp() -> dict:
    """MCP：实际握手（list_tools），逐项报告可用性。"""
    started = time.time()
    try:
        from mcp_client import load_mcp_servers
        clients = load_mcp_servers() or []
    except Exception as exc:
        return {"target": "mcp", "ok": False, "reason": f"加载失败：{str(exc)[:120]}",
                "detail": "", "latency_ms": int((time.time() - started) * 1000)}
    if not clients:
        return {"target": "mcp", "ok": True, "reason": "未配置 MCP 服务",
                "detail": "", "latency_ms": int((time.time() - started) * 1000)}
    bad = []
    for client in clients:
        try:
            client.list_tools()
        except Exception as exc:
            bad.append(f"{getattr(client, 'name', '?')}({str(exc)[:40]})")
    return {"target": "mcp", "ok": not bad,
            "reason": "" if not bad else "不可用：" + "; ".join(bad),
            "detail": f"{len(clients)} 个服务",
            "latency_ms": int((time.time() - started) * 1000)}


def _live_probe_lora() -> dict:
    """本地 LoRA：TCP 探活（hybrid 模式下优先走它）。"""
    started = time.time()
    try:
        from lora_client import _service_alive, llm_mode, LOCAL_URL
        mode = llm_mode()
        alive = _service_alive()
        reason = "" if alive else (
            "cloud 模式下不使用本地服务" if mode == "cloud" else "本地服务未监听（将回退云端）")
        return {"target": "lora", "ok": bool(alive) or mode == "cloud",
                "reason": reason, "detail": f"mode={mode}, url={LOCAL_URL}",
                "latency_ms": int((time.time() - started) * 1000)}
    except Exception as exc:
        return {"target": "lora", "ok": False, "reason": f"探测异常：{str(exc)[:120]}",
                "detail": "", "latency_ms": int((time.time() - started) * 1000)}


_PROBES = (
    probe_llm, probe_search, probe_market_source, probe_embedding, probe_sandbox,
    probe_planner, probe_mcp, probe_lora,
)


def snapshot() -> list[dict]:
    """一次列出所有外部依赖状态（失败项不抛，逐条降级为"状态不可读"）。"""
    out: list[dict] = []
    for probe in _PROBES:
        try:
            out.append(probe())
        except Exception as exc:
            out.append(_entry(getattr(probe, "__name__", "unknown"), True,
                              f"probe failed: {str(exc)[:80]}"))
    return out


def unhealthy() -> list[dict]:
    """仅返回不健康的依赖（供告警/横幅使用）。"""
    return [item for item in snapshot() if not item.get("ok")]
