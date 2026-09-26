# -*- coding: utf-8 -*-
"""外部依赖健康注册表：把散落的健康源收成一个统一视图。

改造前的状态：5 套健康源各自为政，字段与存储方式都不一样
（`llm_health` 进程内 dict、`search_health` Redis 键、`source_health` 进程内 dict、
`embed_health` Redis 键、`code_sandbox` 现算），字段名在
`healthy`/`ok`/`degraded`/`cooldown_until` 之间混用，而且 orchestrator 与 webui
是**两个进程**——`/api/status` 读不到编排侧的进程内状态（如行情适配器熔断）。

统一字段（每个依赖一条）：
    {name, state, ok, reason, since, source_process, instance, checked_at, stale, detail}

`state` 是权威判定，`ok` 仅为旧调用方保留（`ok = state ∈ {available, degraded}`）：

- `available`   ：有证据表明可用；
- `degraded`    ：可用但有已知降级（如备用端点不可用、部分源熔断）——**部分可用不等于全不可用**；
- `unavailable` ：有证据表明不可用/未就绪；
- `unknown`     ：**没有证据**（无快照、快照过期、快照来自别的实例）——不得显示为可用。

后两条都能让页面变红：把"没快照"读成"健康"正是专项 §6 要修的那个假绿。
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

# 状态语义（专项 §6）：缺快照与过期都不能是绿色可用
STATE_AVAILABLE = "available"
STATE_DEGRADED = "degraded"
STATE_UNAVAILABLE = "unavailable"
STATE_UNKNOWN = "unknown"
STATES = (STATE_AVAILABLE, STATE_DEGRADED, STATE_UNAVAILABLE, STATE_UNKNOWN)
# 快照有效期：跨进程快照是"观测"，不是"实时"；超过这个时长按过期（unknown）处理
SNAPSHOT_TTL_SECONDS = float(os.environ.get("WM_HEALTH_SNAPSHOT_TTL", "") or 300)

# 跨进程快照键（由拥有该状态的进程写入）
SEARCH_HEALTH_KEY = "search_engine_health"
SOURCE_HEALTH_KEY = "wm:source:health"
EMBED_HEALTH_KEY = "wm:embed:health"
# 快照自带的元信息键（下划线开头；旧读取方按"非 dict 值"忽略即可）
META_INSTANCE = "_instance"
META_CHECKED_AT = "_checked_at"


def _redis_get(key: str) -> dict:
    try:
        import redis
        # 关掉 redis-py 内建重试（common._NO_REDIS_RETRY）：默认重试会把
        # socket_connect_timeout=2 叠成 26~48 秒才失败，而本函数在
        # /api/config/requirements 与 /api/status 的同步路径上，Redis 不可达时
        # 表现为页面长时间卡死，而不是"这一项不可用"。
        from common import _NO_REDIS_RETRY
        client = redis.Redis(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379") or 6379),
            decode_responses=True, socket_connect_timeout=2, socket_timeout=2,
            retry=_NO_REDIS_RETRY,
        )
        raw = client.get(key)
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def instance_id() -> str:
    """本实例标识：`WM_INSTANCE_ID` 优先，否则由工作区根目录 + 端口指纹生成。

    为什么要它：运行包实例与源码实例可能共用一台 Redis（历史上确实串过任务），
    健康快照是"某个进程观测到的状态"——不带实例身份的快照会让 A 实例拿 B 实例的读数
    当自己的健康度。指纹只取路径与端口，不含用户名等信息。
    """
    explicit = str(os.environ.get("WM_INSTANCE_ID", "") or "").strip()
    if explicit:
        return explicit
    try:
        import hashlib
        root = os.environ.get("WM_WORKSPACE_ROOT", "") or ""
        if not root:
            try:
                import workspace
                root = str(getattr(workspace, "WORKSPACE_ROOT", "") or "")
            except Exception:
                root = os.path.dirname(os.path.abspath(__file__))
        port = os.environ.get("WEB_PORT", "") or os.environ.get("REDIS_PORT", "")
        raw = f"{os.path.abspath(root)}|{port}".encode("utf-8", "replace")
        return "inst-" + hashlib.sha256(raw).hexdigest()[:10]
    except Exception:
        return "inst-unknown"


def snapshot_meta(checked_at: float | None = None) -> dict:
    """写快照时附带的元信息（实例身份 + 观测时刻）。"""
    return {META_INSTANCE: instance_id(),
            META_CHECKED_AT: float(checked_at if checked_at is not None else time.time())}


def split_snapshot(data: dict) -> tuple[dict, str, float]:
    """拆快照 → `(条目表, 实例, 观测时刻)`；缺元信息的旧快照按"实例未知、时刻为 0"处理。"""
    body = {k: v for k, v in (data or {}).items() if not str(k).startswith("_")}
    inst = str((data or {}).get(META_INSTANCE) or "")
    try:
        checked = float((data or {}).get(META_CHECKED_AT) or 0.0)
    except (TypeError, ValueError):
        checked = 0.0
    return body, inst, checked


def snapshot_freshness(checked_at: float, *, ttl: float | None = None,
                       inst: str = "") -> tuple[bool, str]:
    """快照是否新鲜可用 → `(fresh, 问题说明)`；问题非空时按 unknown 处理。"""
    if inst and inst != instance_id():
        return False, f"快照来自其它实例（{inst}），本实例（{instance_id()}）没有观测"
    if not checked_at:
        return False, "快照未带观测时刻（旧格式），无法判断新鲜度"
    age = max(0.0, time.time() - float(checked_at))
    limit = float(ttl if ttl is not None else SNAPSHOT_TTL_SECONDS)
    if age > limit:
        return False, f"快照已过期（{int(age)} 秒前，上限 {int(limit)} 秒）"
    return True, ""


def _entry(name: str, ok: bool, reason: str = "", since: float = 0.0,
           detail: str = "", *, state: str = "", checked_at: float | None = None,
           stale: bool = False) -> dict:
    """统一的健康条目。`state` 未显式给出时按 `ok` 折算，避免旧调用点写出非法状态。"""
    if state not in STATES:
        state = STATE_AVAILABLE if ok else STATE_UNAVAILABLE
    entry = {
        "name": name,
        "state": state,
        "ok": state in (STATE_AVAILABLE, STATE_DEGRADED),
        "reason": str(reason or "")[:200],
        "since": float(since or 0.0),
        "source_process": "webui" if _in_webui() else "orchestrator",
        "instance": instance_id(),
        "checked_at": float(checked_at if checked_at is not None else time.time()),
        "stale": bool(stale),
        "detail": str(detail or "")[:300],
    }
    return entry


def _unknown_entry(name: str, reason: str, detail: str = "") -> dict:
    """没证据就是没证据：无快照 / 过期 / 来自别的实例 —— 一律 unknown，不显示为可用。"""
    return _entry(name, False, reason, detail=detail, state=STATE_UNKNOWN)


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
    """搜索引擎健康（worker 写 Redis 快照，webui 也能读到）。

    快照必须**新鲜且属于本实例**：无快照 / 过期 / 来自别的实例一律 `unknown`——此前这三
    种情况都返回绿色（专项 §6"缺快照和过期不能绿色可用"）。单个引擎坏 = `degraded`
    （还有可用引擎），全坏才 `unavailable`。
    """
    data = _redis_get(SEARCH_HEALTH_KEY)
    if not data:
        return _unknown_entry(_NAME_SEARCH, "无快照（未观测到，非健康）", detail="no snapshot")
    engines, inst, checked = split_snapshot(data)
    fresh, why = snapshot_freshness(checked, inst=inst)
    if not fresh:
        return _unknown_entry(_NAME_SEARCH, why, detail=f"engines={len(engines)}")
    engines = {k: v for k, v in engines.items() if isinstance(v, dict)}
    if not engines:
        return _unknown_entry(_NAME_SEARCH, "快照为空（没有引擎记录）")
    unhealthy = [k for k, v in engines.items() if not v.get("healthy", True)]
    detail = ", ".join(
        f"{k}={'ok' if v.get('healthy', True) else 'down'}" for k, v in engines.items()
    )
    if not unhealthy:
        return _entry(_NAME_SEARCH, True, "", detail=detail, checked_at=checked,
                      state=STATE_AVAILABLE)
    state = STATE_UNAVAILABLE if len(unhealthy) == len(engines) else STATE_DEGRADED
    return _entry(_NAME_SEARCH, state == STATE_DEGRADED,
                  "引擎不健康：" + ", ".join(unhealthy), detail=detail,
                  checked_at=checked, state=state)


def probe_market_source() -> dict:
    """行情适配器熔断状态（跨进程快照优先，缺失时回退本进程）。

    与搜索同理：快照过期/异实例 = `unknown`；部分源熔断 = `degraded`（研究仍可做，
    只是少一路行情），全部熔断才 `unavailable`。
    """
    data = _redis_get(SOURCE_HEALTH_KEY)
    checked = 0.0
    inst = ""
    if data:
        data, inst, checked = split_snapshot(data)
        fresh, why = snapshot_freshness(checked, inst=inst)
        if not fresh:
            return _unknown_entry(_NAME_SOURCE, why, detail=f"sources={len(data)}")
    if not data:
        try:
            from adapters.source_health import get_health
            data = get_health() or {}
        except Exception:
            data = {}
        checked = time.time()          # 本进程读数：就是刚刚观测的
    if not data:
        return _unknown_entry(_NAME_SOURCE, "无快照（未观测到，非健康）", detail="no snapshot")
    cooling = [
        f"{name}({info.get('reason') or 'cooling'})"
        for name, info in data.items()
        if isinstance(info, dict) and info.get("cooldown_until", 0) > time.time()
    ]
    detail = f"{len(data)} 个数据源"
    if not cooling:
        return _entry(_NAME_SOURCE, True, "", detail=detail, checked_at=checked,
                      state=STATE_AVAILABLE)
    state = STATE_UNAVAILABLE if len(cooling) >= len(data) else STATE_DEGRADED
    return _entry(_NAME_SOURCE, state == STATE_DEGRADED,
                  "熔断冷却：" + ", ".join(cooling), detail=detail,
                  checked_at=checked, state=state)


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
    """代码沙箱的隔离状态（现算）。

    健康判据是**隔离是否就绪**，不是"能不能跑"：restricted / none 能在宿主机跑但
    没有任何隔离，对机构部署属于不达标状态；默认模式下隔离不可用则代码执行会被拒绝。
    两个事实分开报：`isolation_ready` 与 `execution_available`。
    """
    try:
        from code_sandbox import sandbox_status
        status = sandbox_status() or {}
        mode = str(status.get("mode") or "unknown")
        ready = bool(status.get("isolation_ready"))
        exec_ok = bool(status.get("execution_available"))
        reason = str(status.get("isolation_reason") or status.get("isolation_note") or "")
        return _entry(
            _NAME_SANDBOX, ready,
            "" if ready else (reason or "隔离未就绪"),
            detail=(f"mode={mode}, isolation_ready={'yes' if ready else 'no'}, "
                    f"execution_available={'yes' if exec_ok else 'no'}"),
        )
    except Exception as exc:
        return _entry(_NAME_SANDBOX, False, f"状态不可读：{str(exc)[:80]}")


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
            out.append(_unknown_entry(getattr(probe, "__name__", "unknown"),
                                      f"探测失败：{str(exc)[:80]}"))
    return out


def degraded_items() -> list[dict]:
    """可用但有降级的依赖（页面应按"部分可用"展示，不当作全部不可用）。"""
    return [item for item in snapshot() if item.get("state") == STATE_DEGRADED]


def unknown_items() -> list[dict]:
    """没有观测证据的依赖（无快照 / 过期 / 异实例）——不得显示为可用。"""
    return [item for item in snapshot() if item.get("state") == STATE_UNKNOWN]


def unhealthy() -> list[dict]:
    """仅返回不可用的依赖（供告警/横幅使用）：`unavailable` 与 `unknown` 都算。

    `unknown` 也算：把"没快照"读成健康正是要修的假绿；`degraded` 不算——它仍可用。
    """
    return [item for item in snapshot()
            if item.get("state") in (STATE_UNAVAILABLE, STATE_UNKNOWN)]
