# -*- coding: utf-8 -*-
"""用户自加数据源（设置页）：登记 → 存密钥 → 出站授权 → 可调用工具 → 连通性探测。

资料获取顺序里的第③级（"有标识的结构化补充源"）向**用户**开放：此前只有代码里写死的
免费公开源（东财/腾讯/新浪/SEC/FRED/CoinGecko），付费源（Wind/iFinD/自建 API）只能靠
改代码。本模块把"加一个源"变成设置页操作，同时守住既有边界：

1. **密钥与配置分家**：base_url/市场/指标名在 config.json 的 `data_sources` 列表里；
   api_key 在本机密钥仓（credentials.json，0600，不进 git），用同一条点号路径对应
   （`data_sources.<id>.api_key`）。HTTP 层只回"是否已配置"，明文不下发、不进日志。
2. **出站走登记**：启用的 http_json 源由 net_policy 按端点登记（`data_source:<id>`），
   抓取走 `net_policy.fetch_service`（已验 IP、不跟随重定向、响应有上限）；调用方不能
   用参数提权。停用/删除即撤销登记。
3. **结果如实标注来源**：工具返回带 `source.id/host/class`，进入任务的来源清单时按
   `custom_source` 记账——"这份数据是谁的 API 给的"可追溯，不冒充官方披露。
4. **许可提示一次**（用户决定）：自定义源正文默认进入模型上下文（`model_context: full`）；
   授权数据（Wind 等）是否允许送第三方模型由用户在设置页确认，本模块只如实透传，
   不做静默降级。

kind 两种：`http_json`（用户自建/中转 JSON API，本模块直接抓）与 `mcp`（经
`mcp_servers` 里的 MCP 工具，如 Wind/iFinD 官方插件；本模块只做转发，密钥由
mcp_client 的鉴权头支持读取）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse

logger = logging.getLogger(__name__)

# 类型与取值域
VALID_KINDS = ("http_json", "mcp")
AUTH_MODES = ("bearer", "header", "query", "none")
MODEL_CONTEXT_MODES = ("full", "derived")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TOOL_PREFIX = "finance_custom_"

DEFAULT_AUTH_HEADER = "X-API-Key"
DEFAULT_QUERY_PARAM = "api_key"

MAX_SOURCES = 20


def config_path() -> str:
    """config.json 路径（与 net_policy 同一解析：WEAVEMIND_CONFIG 优先）。"""
    env = os.environ.get("WEAVEMIND_CONFIG") or ""
    return env if env.strip() else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------- 校验/规范化

def _clean_str(v, limit: int = 400) -> str:
    return str(v or "").strip()[:limit]


def validate_items(items) -> tuple[list[dict], list[str]]:
    """校验设置页提交的数据源列表：返回 (规范化列表, 问题清单)。

    密钥**不在这里**处理（调用方负责改道密钥仓）；这里只校验非密钥字段。
    """
    issues: list[str] = []
    clean: list[dict] = []
    if items is None:
        return [], issues
    if not isinstance(items, list):
        return [], ["data_sources 必须是列表"]
    if len(items) > MAX_SOURCES:
        issues.append(f"数据源最多 {MAX_SOURCES} 个")
    seen: set[str] = set()
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            issues.append(f"第 {idx + 1} 个数据源必须是对象")
            continue
        sid = _clean_str(raw.get("id"), 40).lower()
        if not sid or not _ID_RE.match(sid):
            issues.append(f"第 {idx + 1} 个数据源 id 非法（小写字母/数字/-/_，≤32 字符）：{sid or '(空)'}")
            continue
        if sid in seen:
            issues.append(f"数据源 id 重复：{sid}")
            continue
        seen.add(sid)
        kind = _clean_str(raw.get("kind"), 20)
        if kind not in VALID_KINDS:
            issues.append(f"数据源 {sid} 的 kind 必须是 {'/'.join(VALID_KINDS)}，收到 {kind or '(空)'}")
            continue
        item: dict = {
            "id": sid,
            "label": _clean_str(raw.get("label"), 80) or sid,
            "kind": kind,
            "enabled": bool(raw.get("enabled", True)),
            "note": _clean_str(raw.get("note"), 300),
        }
        if kind == "http_json":
            base = _clean_str(raw.get("base_url"), 300)
            parts = urllib.parse.urlsplit(base)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                issues.append(f"数据源 {sid} 的 base_url 必须是 http(s) 地址：{base or '(空)'}")
                continue
            item["base_url"] = base
            auth = _clean_str(raw.get("auth"), 12) or "bearer"
            if auth not in AUTH_MODES:
                issues.append(f"数据源 {sid} 的 auth 必须是 {'/'.join(AUTH_MODES)}，收到 {auth}")
                continue
            item["auth"] = auth
            if auth == "header":
                item["auth_header"] = _clean_str(raw.get("auth_header"), 60) or DEFAULT_AUTH_HEADER
            if auth == "query":
                item["query_param"] = _clean_str(raw.get("query_param"), 60) or DEFAULT_QUERY_PARAM
            method = (_clean_str(raw.get("method"), 6) or "GET").upper()
            if method not in ("GET", "POST"):
                issues.append(f"数据源 {sid} 的 method 只支持 GET/POST")
                continue
            item["method"] = method
            params = raw.get("params")
            if params is not None:
                if not isinstance(params, dict) or not all(
                        isinstance(k, str) for k in params):
                    issues.append(f"数据源 {sid} 的 params 必须是字符串键对象")
                    continue
                item["params"] = {str(k)[:60]: str(v)[:200] for k, v in params.items()}
            extra_headers = raw.get("headers")
            if extra_headers is not None:
                if not isinstance(extra_headers, dict) or not all(
                        isinstance(k, str) for k in extra_headers):
                    issues.append(f"数据源 {sid} 的 headers 必须是字符串键对象")
                    continue
                item["headers"] = {str(k)[:60]: str(v)[:200] for k, v in extra_headers.items()}
            path_suffix = _clean_str(raw.get("path"), 200)
            if path_suffix:
                item["path"] = path_suffix
            markets = raw.get("markets")
            if isinstance(markets, list):
                item["markets"] = [str(m)[:10] for m in markets if str(m).strip()][:6]
            metrics = raw.get("metrics")
            if isinstance(metrics, list):
                item["metrics"] = [str(m)[:40] for m in metrics if str(m).strip()][:12]
            loopback = bool(raw.get("loopback"))
            if loopback:
                item["loopback"] = True   # 私网/环回例外必须显式声明（net_policy 只认登记项）
            model_context = _clean_str(raw.get("model_context"), 10) or "full"
            if model_context not in MODEL_CONTEXT_MODES:
                issues.append(f"数据源 {sid} 的 model_context 必须是 {'/'.join(MODEL_CONTEXT_MODES)}")
                continue
            item["model_context"] = model_context
        else:  # kind == "mcp"
            tool = _clean_str(raw.get("tool"), 60)
            server = _clean_str(raw.get("server"), 60)
            if not tool and not server:
                issues.append(f"数据源 {sid}（MCP 型）必须填 tool（MCP 工具名）或 server（服务名）")
                continue
            if tool:
                item["tool"] = tool
            if server:
                item["server"] = server
        clean.append(item)
    return clean, issues[:10]


def normalize_single(raw: dict) -> dict | None:
    """静默规范化单条（读侧用：坏条目返回 None，不抛）。"""
    clean, issues = validate_items([raw])
    return clean[0] if clean and not issues else None


# ---------------------------------------------------------------- 读侧

def all_sources(cfg: dict | None = None) -> list[dict]:
    data = cfg if cfg is not None else load_config()
    raw = data.get("data_sources")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        got = normalize_single(item) if isinstance(item, dict) else None
        if got:
            out.append(got)
    return out


def enabled_sources(cfg: dict | None = None) -> list[dict]:
    return [s for s in all_sources(cfg) if s.get("enabled")]


def source_by_id(sid: str, cfg: dict | None = None) -> dict | None:
    for s in all_sources(cfg):
        if s.get("id") == str(sid or ""):
            return s
    return None


def secret_path(sid: str) -> str:
    return f"data_sources.{sid}.api_key"


def source_secret(src: dict) -> str:
    """该源的密钥：环境变量 `WEAVEMIND_DATA_SOURCE_<ID>_KEY` 优先，其次密钥仓。"""
    sid = str(src.get("id") or "")
    env_name = "WEAVEMIND_DATA_SOURCE_" + re.sub(r"[^A-Z0-9]", "_", sid.upper()) + "_KEY"
    env_val = os.environ.get(env_name) or ""
    if env_val.strip():
        return env_val.strip()
    try:
        import credentials
        return credentials.get_secret(secret_path(sid))
    except Exception:
        return ""


def endpoint_items(cfg: dict | None = None) -> list[dict]:
    """启用的 http_json 源 → net_policy 登记项（`data_source:<id>`，用途=data_source）。"""
    out: list[dict] = []
    for src in enabled_sources(cfg):
        if src.get("kind") != "http_json":
            continue
        out.append({
            "id": f"data_source:{src['id']}",
            "purpose": "data_source",
            "url": src.get("base_url") or "",
            "operations": [str(src.get("method") or "GET").upper()],
            "loopback": bool(src.get("loopback")),
        })
    return out


# ---------------------------------------------------------------- 工具暴露

def tool_name(src: dict) -> str:
    return TOOL_PREFIX + str(src.get("id") or "")


def tool_registry(cfg: dict | None = None) -> list[dict]:
    """启用的源 → MCP-lite 风格工具定义（规划器/工具目录可见）。"""
    tools: list[dict] = []
    for src in enabled_sources(cfg):
        metrics = "、".join(src.get("metrics") or []) or "未声明指标"
        host = urllib.parse.urlsplit(str(src.get("base_url") or "")).hostname or "-"
        desc = (f"用户自加数据源「{src.get('label') or src.get('id')}」"
                f"（{src.get('kind')}，host={host}）：声明指标 {metrics}。"
                f"{src.get('note') or ''}").strip()
        tools.append({
            "name": tool_name(src),
            "description": desc[:400],
            "parameters": {"instruction": "查询要求，可含代码/期间/口径（作为该源查询参数发送）"},
            "returns": "JSON：{\"status\": \"success|failed\", \"result\": <该 API 的返回>, "
                       "\"source\": {\"id\", \"host\", \"class\": \"custom_source\"}}",
            "required": ["status"],
        })
    return tools


def is_source_tool(name: str, cfg: dict | None = None) -> bool:
    return str(name or "").startswith(TOOL_PREFIX)


def _apply_auth(src: dict, key: str) -> tuple[dict, dict]:
    """按 auth 模式把密钥放进 headers 或 query params；返回 (headers, params)。"""
    auth = str(src.get("auth") or "bearer")
    headers: dict = dict(src.get("headers") or {})
    params: dict = dict(src.get("params") or {})
    if not key or auth == "none":
        return headers, params
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif auth == "header":
        headers[str(src.get("auth_header") or DEFAULT_AUTH_HEADER)] = key
    elif auth == "query":
        params[str(src.get("query_param") or DEFAULT_QUERY_PARAM)] = key
    return headers, params


def _redact_error(msg: str) -> str:
    try:
        from llm_client import _scrub_secrets
        return _scrub_secrets(str(msg))[:300]
    except Exception:
        return str(msg)[:300]


def call_source_tool(name: str, instruction: str, timeout: float = 60,
                     cfg: dict | None = None) -> dict | None:
    """调用一个用户自加数据源工具；不是数据源工具返回 None（交回调用方）。

    失败如实返回 `{"status": "failed", "error": ...}`（脱敏），不伪造数据、
    不把失败升级成空结果。
    """
    if not is_source_tool(name):
        return None
    sid = str(name)[len(TOOL_PREFIX):]
    src = source_by_id(sid, cfg)
    if src is None:
        return {"status": "failed", "error": f"数据源未配置或已删除：{sid}",
                "source": {"id": sid, "class": "custom_source"}}
    if not src.get("enabled"):
        return {"status": "failed", "error": f"数据源已停用：{sid}",
                "source": {"id": sid, "class": "custom_source"}}
    key = source_secret(src)
    endpoint = f"data_source:{sid}"
    meta = {"id": sid, "host": urllib.parse.urlsplit(
        str(src.get("base_url") or "")).hostname or "-",
        "class": "custom_source", "label": str(src.get("label") or sid)}
    if src.get("kind") == "mcp":
        tool = str(src.get("tool") or "")
        server = str(src.get("server") or "")
        if not tool:
            return {"status": "failed",
                    "error": f"数据源 {sid}（MCP 型）没有指定 tool，无法调用",
                    "source": meta}
        try:
            from mcp_client import call_external_tool, EXTERNAL_TOOLS
            got = call_external_tool(tool, {"instruction": str(instruction or ""),
                                            "timeout": timeout})
            if got is None:
                hint = f"（MCP 工具 {tool} 未发现"
                if server:
                    hint += f"；确认 mcp_servers 里 {server} 可用"
                return {"status": "failed", "error": hint + "）", "source": meta}
            return {"status": "success" if str(got.get("status", "")).upper()
                    in ("SUCCESS", "OK") else "failed",
                    "result": got, "source": meta}
        except Exception as exc:
            return {"status": "failed", "error": _redact_error(f"MCP 调用失败：{exc}"),
                    "source": meta}
    # http_json：走登记服务通道（鉴权头/查询参数按 auth 模式注入）
    headers, params = _apply_auth(src, key)
    auth_mode = str(src.get("auth") or "bearer")
    text_instruction = str(instruction or "").strip()
    method = str(src.get("method") or "GET").upper()
    body = None
    if method == "POST":
        body = json.dumps({"query": text_instruction, **params}, ensure_ascii=False)
        params = {}
    elif text_instruction and auth_mode != "query" and src.get("query_param"):
        # auth=query 时 query_param 是**密钥**参数，不能被检索词覆盖；
        # 检索词固定落到 `q`（用户可在 params 里自定义其余参数）。
        params[str(src["query_param"])] = text_instruction
    elif text_instruction and auth_mode == "query":
        params.setdefault("q", text_instruction)
    try:
        from net_policy import fetch_service
        got = fetch_service(endpoint, path=str(src.get("path") or ""), params=params,
                            headers=headers, method=method, body=body,
                            timeout=float(timeout))
        status = int(got.get("status") or 0)
        text = str(got.get("text") or "")
        parsed = None
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if 200 <= status < 300:
            return {"status": "success", "result": parsed if parsed is not None
                    else text[:4000], "source": meta}
        return {"status": "failed",
                "error": f"{meta['host']} 返回 {status}：{text[:200] if parsed is None else '（响应体见 result）'}",
                "result": parsed if parsed is not None else text[:2000],
                "source": meta}
    except Exception as exc:
        return {"status": "failed", "error": _redact_error(exc), "source": meta}


# ---------------------------------------------------------------- 连通性探测

def probe_source(sid: str, timeout: float = 10, cfg: dict | None = None) -> dict:
    """逐源连通性测试：发一个最小请求，按状态码归类成"人话"（与 LLM 探测同口径）。

    返回 `{"ok", "reason", "message"}`；message 永不含密钥。
    """
    src = source_by_id(sid, cfg)
    if src is None:
        return {"ok": False, "reason": "not_configured", "message": f"数据源未配置：{sid}"}
    if src.get("kind") == "mcp":
        try:
            from mcp_client import load_mcp_servers
            want_tool = str(src.get("tool") or "")
            want_server = str(src.get("server") or "")
            clients = load_mcp_servers()
            if not clients:
                return {"ok": False, "reason": "not_configured",
                        "message": "还没有配置任何 MCP 服务（config.json 的 mcp_servers）"}
            last = ""
            for client in clients:
                if want_server and client.name != want_server:
                    continue
                try:
                    client.initialize()
                    tools = client.list_tools()
                    names = [str(t.get("name") or "") for t in tools]
                    if want_tool and want_tool not in names:
                        last = f"{client.name} 里没有工具 {want_tool}"
                        continue
                    return {"ok": True, "reason": "ok",
                            "message": f"MCP 服务 {client.name} 正常，发现 {len(names)} 个工具"}
                except Exception as exc:
                    last = _redact_error(f"{client.name} 连不上：{exc}")
                finally:
                    client.close()
            return {"ok": False, "reason": "unreachable", "message": last or "MCP 服务不可达"}
        except Exception as exc:
            return {"ok": False, "reason": "unreachable",
                    "message": _redact_error(f"MCP 探测失败：{exc}")}
    key = source_secret(src)
    if not key and str(src.get("auth") or "bearer") != "none":
        return {"ok": False, "reason": "missing_key",
                "message": "还没有填密钥（或环境变量未设置）——鉴权 API 会 401"}
    headers, params = _apply_auth(src, key)
    method = str(src.get("method") or "GET").upper()
    body = json.dumps({"query": ""}, ensure_ascii=False) if method == "POST" else None
    try:
        from net_policy import fetch_service
        got = fetch_service(f"data_source:{sid}", path=str(src.get("path") or ""),
                            params=params, headers=headers, method=method, body=body,
                            timeout=float(timeout))
        status = int(got.get("status") or 0)
        host = urllib.parse.urlsplit(str(src.get("base_url") or "")).hostname or "-"
        if 200 <= status < 300:
            return {"ok": True, "reason": "ok", "message": ""}
        reason, msg = _classify_http_status(status, host, str(got.get("text") or ""))
        return {"ok": False, "reason": reason, "message": msg}
    except Exception as exc:
        from llm_client import _probe_failure_message
        base = str(src.get("base_url") or "")
        return {"ok": False, "reason": "unreachable",
                "message": _probe_failure_message(exc, base, "")}


def _classify_http_status(status: int, host: str, body: str = "") -> tuple[str, str]:
    """HTTP 状态码 → (reason, 人话)。与 llm_client._probe_failure_message 同口径。"""
    tail = (body or "").strip()[:120]
    if status == 401:
        return "unauthorized", (f"{host} 返回 401：这把密钥不被接受——"
                                "密钥与 base_url 必须是同一家服务商")
    if status == 403:
        return "forbidden", (f"{host} 返回 403：密钥被拒绝——可能是权限域/IP 限制，"
                             "也可能不是这家服务商的密钥")
    if status == 404:
        return "not_found", f"{host} 返回 404：base_url 没有这个路径（检查路径后缀与 /v1 结尾）"
    if status == 429:
        return "rate_limited", f"{host} 返回 429：限流/额度超限（稍后再试，或检查套餐额度）"
    if status in (402,):
        return "insufficient_balance", f"{host} 返回 402：余额/额度不足"
    if 400 <= status < 500:
        return "bad_request", f"{host} 返回 {status}：请求参数不被接受（检查 params/查询参数）{('：' + tail) if tail else ''}"
    if status >= 500:
        return "server_error", f"{host} 返回 {status}：服务端错误（稍后再试）"
    return "http_error", f"{host} 返回 {status}"


# ---------------------------------------------------------------- 目录文本

def catalog_text(cfg: dict | None = None) -> str:
    """规划器可见的工具目录段（用户自加数据源）。"""
    tools = tool_registry(cfg)
    if not tools:
        return ""
    lines = ["## 用户自加数据源（设置页配置，密钥本地存储）"]
    for t in tools:
        lines.append(f"- {t['name']}: {t['description']} 返回: {t['returns']}")
    return "\n".join(lines)
