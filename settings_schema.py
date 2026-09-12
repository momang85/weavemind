# -*- coding: utf-8 -*-
"""系统所需外部配置的**唯一清单**：设置页渲染、写回白名单、健康状态挂载共用一份。

动机：设置页此前只渲染 `llm`(3 字段) / `redis`(2) / `system`(4/14)，而系统实际需要
的端点远不止这些——`embedding`（欠费会让记忆与提示词经验检索静默失效）、`planner`、
`backup` 三段既无 UI 也无健康探针，用户只能从"记忆命中率 0%"反推。

约定：
- `path` 是 config.json 的点号路径（如 `embedding.api_key`），也是写回白名单的键；
- `status` 标明该项的真实地位，避免"模板里有、代码不读"的字段被误当成必需项：
  - `active`   代码实际读取
  - `env_only` 只认环境变量，设置页改不了（必须显式标注，否则用户会白改）
  - `reserved` 预留未接线
  - `unused`   模板字段、代码未读取
- `health` 指向 `health_registry` / `/api/status` 里的健康源名，供设置页显示状态徽标。
"""

from __future__ import annotations

import os
from typing import Any

# ---------------------------------------------------------------- 段与条目

# 密钥类 kind：GET 永不下发明文，写回时空值表示"保持不变"
SECRET_KINDS = ("secret",)

SECTIONS: list[dict] = [
    {
        "key": "llm",
        "label": "主 LLM 端点",
        "note": "所有 Agent 的默认模型；任务提交前会做余额预检，主备都不足会直接拒绝任务。",
        "entries": [
            {"path": "llm.api_key", "label": "API Key", "kind": "secret",
             "env": "LLM_API_KEY", "required": True, "testable": True,
             "health": "llm", "affects": "全部 LLM 调用（规划/执行/评审/报告）"},
            {"path": "llm.base_url", "label": "Base URL", "kind": "url",
             "env": "LLM_BASE_URL", "required": True, "testable": True,
             "health": "llm", "affects": "全部 LLM 调用"},
            {"path": "llm.model", "label": "默认模型", "kind": "string",
             "env": "LLM_MODEL", "required": True,
             "affects": "未在 model_roles 中指定角色时使用"},
            {"path": "llm.model_roles.planner", "label": "规划模型", "kind": "string",
             "affects": "规划与反思（plan/reflect/review）"},
            {"path": "llm.model_roles.exec", "label": "执行模型", "kind": "string",
             "affects": "步骤执行（exec）"},
            {"path": "llm.model_roles.judge", "label": "评审模型", "kind": "string",
             "affects": "评测打分（judge）"},
            {"path": "llm.task_budget.max_calls", "label": "单任务最大调用数", "kind": "int",
             "default": 0, "env": "LLM_TASK_BUDGET_CALLS",
             "affects": "0=不限；超限后该任务 LLM 调用抛错终止"},
            {"path": "llm.task_budget.max_seconds", "label": "单任务最大秒数", "kind": "int",
             "default": 0, "env": "LLM_TASK_BUDGET_SECONDS", "affects": "0=不限"},
            {"path": "llm.task_budget.max_cost_usd", "label": "单任务成本上限(USD)", "kind": "float",
             "default": 0, "env": "LLM_TASK_BUDGET_USD", "affects": "0=不限"},
        ],
    },
    {
        "key": "planner",
        "label": "规划专用端点",
        "note": "留空则复用主端点；配了便宜的模型可显著降低规划成本。",
        "entries": [
            {"path": "planner.api_key", "label": "API Key", "kind": "secret",
             "env": "PLANNER_LLM_API_KEY", "testable": True, "health": "planner",
             "affects": "规划/反思/评审调用（配错会导致规划失败或静默回退主端点）"},
            {"path": "planner.base_url", "label": "Base URL", "kind": "url",
             "env": "PLANNER_LLM_BASE_URL", "testable": True, "health": "planner",
             "affects": "同上"},
            {"path": "planner.model", "label": "模型", "kind": "string",
             "env": "PLANNER_LLM_MODEL", "affects": "同上"},
        ],
    },
    {
        "key": "backup",
        "label": "备用 LLM 端点",
        "note": "主端点连续失败或余额不足时自动切换；建议与主端点分属不同厂商（Health 页会做同源告警）。",
        "entries": [
            {"path": "backup.api_key", "label": "API Key", "kind": "secret",
             "testable": True, "health": "llm",
             "affects": "主端点故障时的自动降级通道（未配置则无兜底）"},
            {"path": "backup.base_url", "label": "Base URL", "kind": "url",
             "testable": True, "health": "llm", "affects": "同上"},
            {"path": "backup.model", "label": "模型", "kind": "string", "affects": "同上"},
        ],
    },
    {
        "key": "embedding",
        "label": "Embedding 端点",
        "note": "长期记忆与提示词经验检索专用。额度不足时记忆命中率会变成 0%，任务仍能跑但经验复用失效。",
        "entries": [
            {"path": "embedding.api_key", "label": "API Key", "kind": "secret",
             "env": "EMBEDDING_API_KEY", "testable": True, "health": "embedding",
             "affects": "记忆写入/检索、提示词改进经验召回（欠费导致命中率 0%）"},
            {"path": "embedding.base_url", "label": "Base URL", "kind": "url",
             "env": "EMBEDDING_BASE_URL", "testable": True, "health": "embedding",
             "affects": "同上"},
            {"path": "embedding.model", "label": "模型", "kind": "string",
             "env": "EMBEDDING_MODEL", "health": "embedding",
             "default": "BAAI/bge-large-zh-v1.5", "affects": "同上"},
        ],
    },
    {
        "key": "image",
        "label": "图片生成端点（预留）",
        "note": "配置已预留但当前没有任何代码读取，填了也不会生效。",
        "entries": [
            {"path": "image.api_key", "label": "API Key", "kind": "secret",
             "status": "reserved", "affects": "当前无影响（预留字段，代码未读取）"},
            {"path": "image.base_url", "label": "Base URL", "kind": "url",
             "status": "reserved", "affects": "当前无影响（预留字段，代码未读取）"},
            {"path": "image.model", "label": "模型", "kind": "string",
             "status": "reserved", "affects": "当前无影响（预留字段，代码未读取）"},
        ],
    },
    {
        "key": "redis",
        "label": "Redis",
        "note": "唯一硬依赖：任务队列、跨进程快照、成本台账都走它。",
        "entries": [
            {"path": "redis.host", "label": "Host", "kind": "string", "env": "REDIS_HOST",
             "default": "localhost", "required": True, "affects": "全部任务派发与状态同步"},
            {"path": "redis.port", "label": "Port", "kind": "int", "env": "REDIS_PORT",
             "default": 6379, "required": True, "affects": "同上"},
        ],
    },
    {
        "key": "system",
        "label": "系统参数",
        "note": "影响任务时长、并发、反思与评审策略；改动下个任务生效（热重载）。",
        "entries": [
            {"path": "system.task_timeout", "label": "任务超时(秒)", "kind": "int",
             "default": 600, "group": "执行", "affects": "单任务整体上限"},
            {"path": "system.max_retry", "affects": "单步失败后的自动重试次数（含备用端点切换）", "label": "步骤最大重试", "kind": "int",
             "default": 2, "group": "执行"},
            {"path": "system.replan_depth", "affects": "计划失败或步骤反复失败时重新规划的层数", "label": "重规划深度", "kind": "int",
             "default": 2, "group": "执行"},
            {"path": "system.max_steps", "affects": "一次任务的步骤上限，防止规划发散", "label": "最大步骤数", "kind": "int",
             "default": 8, "group": "执行"},
            {"path": "system.max_parallel", "affects": "同时派发的步骤数；受 11 个 Worker 数量约束", "label": "最大并行步骤", "kind": "int",
             "default": 3, "group": "执行"},
            {"path": "system.max_iterations", "affects": "反思-重做轮数上限；越大越慢也越可能补齐缺口", "label": "最大迭代轮数", "kind": "int",
             "default": 2, "group": "反思"},
            {"path": "system.reflection_budget.ranking", "affects": "排行类任务的反思轮预算（覆盖 max_iterations）", "label": "排行类反思预算", "kind": "int",
             "default": 2, "group": "反思"},
            {"path": "system.reflection_budget.financial", "affects": "财务类任务的反思轮预算", "label": "财务类反思预算", "kind": "int",
             "default": 3, "group": "反思"},
            {"path": "system.reflection_budget.general", "affects": "通用任务的反思轮预算", "label": "通用反思预算", "kind": "int",
             "default": 3, "group": "反思"},
            {"path": "system.reflect_max_redo_steps", "affects": "单轮反思最多重做几个步骤，控制返工范围", "label": "单轮最多重做步骤", "kind": "int",
             "default": 2, "group": "反思"},
            {"path": "system.critic", "affects": "关闭后不再做计划评审，规划质量全靠 planner 一次成型", "label": "启用评审 Agent", "kind": "bool",
             "default": True, "group": "评审"},
            {"path": "system.critic_timeout", "affects": "评审 Agent 的等待上限；超时按 PASS 放行", "label": "评审超时(秒)", "kind": "int",
             "default": 30, "group": "评审"},
            {"path": "system.stall_timeout", "label": "停滞判定(秒)", "kind": "int",
             "default": 300, "group": "风控",
             "affects": "依赖长期未满足时把剩余步骤判失败；阶段看门狗阈值也参照它"},
            {"path": "system.guardian_heartbeat", "affects": "Worker 守护进程的巡检周期；过大会延迟发现崩溃", "label": "守护心跳(秒)", "kind": "int",
             "default": 20, "group": "风控"},
            {"path": "system.stale_task_timeout", "label": "任务过期阈值(秒)", "kind": "int",
             "default": 3600, "group": "风控", "env": "STALE_TASK_TIMEOUT",
             "affects": "排队/运行中任务长期无终态时被判失败"},
            {"path": "system.plan_confirm_timeout", "label": "计划确认等待(秒)", "kind": "int",
             "default": 1800, "group": "风控", "affects": "开启「先确认计划」时的等待上限"},
            {"path": "system.scheduler", "affects": "是否启动定时任务调度线程（关掉则定时任务不触发）", "label": "启用定时任务调度", "kind": "bool",
             "default": False, "group": "调度"},
            {"path": "system.market_preference", "label": "市场偏好", "kind": "string",
             "default": "hk", "group": "调度", "env": "WEAVEMIND_MARKET_PREFERENCE",
             "affects": "双市场公司代码解析偏好：hk/us/cn/auto"},
            {"path": "system.llm_mode", "label": "LLM 模式", "kind": "string",
             "default": "hybrid", "group": "调度", "env": "WM_LLM_MODE",
             "affects": "cloud（只用云端）/ hybrid（本地优先，云端兜底）"},
        ],
    },
    {
        "key": "mcp_servers",
        "label": "MCP 外部工具",
        "note": "接入 Wind / iFinD 等付费数据工具；每项填 command（本地脚本）或 url（HTTP 端点）。",
        "kind": "list",
        "entries": [
            {"path": "mcp_servers", "label": "服务列表", "kind": "list", "testable": True,
             "health": "mcp", "affects": "外部工具调用（Wind/iFinD 等）；不可用时相关工具静默失败"},
        ],
    },
    {
        "key": "data_sources",
        "label": "数据源密钥（预留）",
        "note": "当前行情/宏观/新闻均为免费公开接口，无需密钥；该字段为未来付费额度预留。",
        "entries": [
            {"path": "data_sources.crypto_api_key", "label": "CoinGecko Key", "kind": "secret",
             "status": "reserved", "affects": "预留，当前不影响任何功能"},
        ],
    },
    {
        "key": "memory",
        "label": "记忆治理参数",
        "note": "模板里有这些字段，但代码目前只认环境变量（下文标注），改 config.json 不生效。",
        "entries": [
            {"path": "memory.strategy_dedup_threshold", "label": "策略去重阈值", "kind": "float",
             "default": 0.95, "status": "unused", "env": "MEMORY_STRATEGY_DEDUP_THRESHOLD",
             "affects": "记忆治理；改 config.json 不生效，需设对应环境变量"},
            {"path": "memory.strategy_ttl_days", "label": "策略过期天数", "kind": "int",
             "default": 90, "status": "unused", "env": "MEMORY_STRATEGY_TTL_DAYS",
             "affects": "同上"},
            {"path": "memory.conversations_max", "label": "对话上限", "kind": "int",
             "default": 2000, "status": "unused", "env": "MEMORY_CONVERSATIONS_MAX",
             "affects": "同上"},
            {"path": "memory.conversation_dedup_hours", "label": "对话去重窗口(小时)", "kind": "float",
             "default": 24, "status": "unused", "env": "MEMORY_CONVERSATION_DEDUP_HOURS",
             "affects": "同上"},
        ],
    },
    {
        "key": "notifications",
        "label": "通知配置",
        "note": "在本页「通知配置」区块编辑（走 /api/notifications，密钥不回显）。",
        "entries": [
            {"path": "notifications.webhook.url", "label": "Webhook URL", "kind": "url",
             "editable": False, "affects": "任务完成推送（企业微信/钉钉/自建）"},
            {"path": "notifications.serverchan.sendkey", "label": "Server酱 SendKey", "kind": "secret",
             "editable": False, "affects": "任务完成推送到微信"},
            {"path": "notifications.email.host", "label": "SMTP Host", "kind": "string",
             "editable": False, "affects": "邮件通知"},
            {"path": "notifications.email.password", "label": "SMTP 密码", "kind": "secret",
             "editable": False, "affects": "邮件通知"},
        ],
    },
    {
        "key": "env_only",
        "label": "仅环境变量（设置页不可改）",
        "note": "这些项只从环境变量读取；列在这里是为了让「系统到底依赖什么」完整可见。",
        "entries": [
            {"path": "PUBLIC_BASE_URL", "label": "PUBLIC_BASE_URL", "kind": "url",
             "status": "env_only", "editable": False,
             "affects": "分享链接的基址；配错会导致外部打不开分享页"},
            {"path": "BUDGET_MONTHLY_USD", "label": "BUDGET_MONTHLY_USD", "kind": "float",
             "status": "env_only", "editable": False, "default": 10,
             "affects": "月度成本上限；超限后自动降级到低价模型"},
            {"path": "WEB_PORT", "label": "WEB_PORT", "kind": "int",
             "status": "env_only", "editable": False, "default": 8080,
             "affects": "WebUI 监听端口"},
            {"path": "LLM_REQUEST_TIMEOUT", "label": "LLM_REQUEST_TIMEOUT", "kind": "float",
             "status": "env_only", "editable": False, "default": 600,
             "affects": "单次 LLM 请求读超时"},
            {"path": "WM_LOCAL_URL", "label": "WM_LOCAL_URL", "kind": "url",
             "status": "env_only", "editable": False, "default": "http://127.0.0.1:8765",
             "health": "lora", "affects": "本地 LoRA 服务地址（hybrid 模式优先走它）"},
            {"path": "WM_LORA_TOKEN", "label": "WM_LORA_TOKEN", "kind": "secret",
             "status": "env_only", "editable": False, "affects": "本地 LoRA 服务鉴权（空=不鉴权）"},
            {"path": "WEAVEMIND_ADMIN_PASSWORD", "label": "WEAVEMIND_ADMIN_PASSWORD", "kind": "secret",
             "status": "env_only", "editable": False,
             "affects": "首次初始化管理员密码（仅初始化时读取）"},
        ],
    },
]


# ---------------------------------------------------------------- 查询工具

def iter_entries() -> list[dict]:
    """展平所有条目（附 section 键与标签、补默认状态）。"""
    out: list[dict] = []
    for section in SECTIONS:
        for entry in section.get("entries", []):
            item = dict(entry)
            item.setdefault("status", "active")
            item.setdefault("required", False)
            item.setdefault("editable", item.get("status", "active") not in ("env_only",))
            item.setdefault("testable", False)
            item["section"] = section["key"]
            item["section_label"] = section.get("label", section["key"])
            out.append(item)
    return out


def entry_by_path(path: str) -> dict | None:
    for item in iter_entries():
        if item["path"] == path:
            return item
    return None


def editable_paths() -> set[str]:
    """允许设置页写回的路径白名单（防任意键注入）。"""
    return {e["path"] for e in iter_entries() if e.get("editable")}


def secret_paths() -> set[str]:
    return {e["path"] for e in iter_entries() if e.get("kind") in SECRET_KINDS}


def testable_targets() -> dict[str, str]:
    """可测试目标 → 端点段：{llm|planner|backup|embedding: 段名}。"""
    return {
        "llm": "llm", "planner": "planner",
        "backup": "backup", "embedding": "embedding",
    }


def get_path(cfg: dict, path: str) -> Any:
    """按点号路径取值（env_only 项直接读环境变量）。"""
    node: Any = cfg or {}
    parts = path.split(".")
    if parts and parts[0] in ("PUBLIC_BASE_URL", "BUDGET_MONTHLY_USD", "WEB_PORT",
                              "LLM_REQUEST_TIMEOUT", "WM_LOCAL_URL", "WM_LORA_TOKEN",
                              "WEAVEMIND_ADMIN_PASSWORD"):
        return os.environ.get(path, "")
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def set_path(cfg: dict, path: str, value: Any) -> None:
    """按点号路径写入（仅用于白名单内的路径）。"""
    parts = path.split(".")
    node = cfg
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def resolve(entry: dict, cfg: dict, env: dict | None = None) -> dict:
    """解析单个条目的当前值、来源与"是否已配置"。

    来源优先级与实现保持一致：config.json 有非空值 → `config`；
    否则环境变量有值 → `env`；否则用 default → `default`。
    """
    env = env if env is not None else os.environ
    status = entry.get("status", "active")
    env_name = str(entry.get("env") or "")
    default = entry.get("default")

    config_value = get_path(cfg, entry["path"]) if status != "env_only" else None
    env_value = env.get(env_name) if env_name else None
    if status == "env_only":
        env_value = env.get(entry["path"])

    def _has(v: Any) -> bool:
        if v is None:
            return False
        if isinstance(v, str):
            return bool(v.strip())
        if isinstance(v, (list, dict)):
            return bool(v)
        return True

    value: Any
    source = "default"
    if _has(config_value):
        value, source = config_value, "config"
    elif _has(env_value):
        value, source = env_value, "env"
    else:
        value = default

    configured = _has(value) if entry.get("required") else _has(value)
    return {
        "value": value,
        "source": source,
        "configured": bool(configured),
        "env_name": env_name,
    }


def mask(entry: dict, value: Any, api_key_set: bool = False) -> Any:
    """秘密字段不下发明文（只回是否已配置）。"""
    if entry.get("kind") in SECRET_KINDS:
        if entry.get("status") == "env_only":
            return "***set***" if api_key_set else ""
        return ""
    if isinstance(value, (dict, list)):
        return value
    return value


# ---------------------------------------------------------------- 写回校验

# 允许写回但不出现在清单里的"容器"段（清单按需要只登记了部分字段）
_ALLOWED_WRITE_EXTRA = {
    "llm.model_roles", "llm.task_budget", "system.reflection_budget",
}

# 有独立写入端点 / 服务端托管的段：在 /api/config 里**静默忽略**而不是报错。
# 例如 users 只能经 /api/users 管理（注入必须无效），notifications 走
# /api/notifications；报错会让"只多带了一个字段"的整个保存失败。
_SERVER_MANAGED_SECTIONS = {"users", "audit", "notifications"}


def validate_payload(cfg: dict) -> list[str]:
    """校验前端提交的配置负载：只允许写回白名单内的路径。

    返回问题清单（空 = 通过）。拒绝未声明的键，避免任意键注入 config.json；
    拒绝 env_only 项（只认环境变量，写回 config.json 会误导用户）；
    `_SERVER_MANAGED_SECTIONS` 里的段直接忽略（各有独立写入端点）。
    """
    issues: list[str] = []
    if not isinstance(cfg, dict):
        return ["负载必须是 JSON 对象"]
    editable = editable_paths()
    env_only = {e["path"] for e in iter_entries() if e.get("status") == "env_only"}
    allowed_containers = set(_ALLOWED_WRITE_EXTRA)

    def _ignored(path: str) -> bool:
        return path.split(".")[0] in _SERVER_MANAGED_SECTIONS

    def walk(node: Any, prefix: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).startswith("_comment"):
                    continue
                # GET /api/config 会下发 `api_key_set` 这类"已配置"标记，前端原样回传；
                # 它们是状态位而非配置项，校验时跳过（服务端会重新计算）
                if str(key).endswith("_set"):
                    continue
                path = f"{prefix}.{key}" if prefix else str(key)
                if _ignored(path):
                    continue
                if isinstance(value, dict):
                    # 段本身必须存在（否则可能是拼错的段名）
                    if path in env_only:
                        issues.append(f"{path} 仅支持环境变量，不能写入 config.json")
                        continue
                    if not any(p == path or p.startswith(path + ".")
                               or path.startswith(p + ".")
                               for p in editable | allowed_containers):
                        issues.append(f"未声明的配置段：{path}")
                        continue
                    walk(value, path)
                else:
                    if path not in editable:
                        issues.append(f"未声明的配置项：{path}")
    walk(cfg)
    return issues[:10]
