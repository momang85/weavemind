# -*- coding: utf-8 -*-
"""新人可行动状态：把"任务状态 + 提交时间线 + 依赖健康"翻译成**一句人话 + 一个下一步**。

C3（原指令 §4-C3）要求：新人只需要识别"模型未配置、资料源暂不可用、当前材料不够回答"
这类**可行动状态**，并得到匹配的入口（设置 / 健康 / 补材料 / 重试）；同时区分
**未接收 / 待消费 / 执行中 / 待材料 / 明确失败**。

此前这些信息是散的：任务行只有 `status`/`phase`，收执在提交时间线里，行情源在
`adapters.source_health`，依赖在 `health_registry`，待材料在 Redis 集合
`material_pending_tasks`。页面各显示一半，用户看得出"没成功"，看不出"该做什么"。

本模块只做**翻译**（纯函数，不写库、不发请求），因此可以离线穷举测试：

- `classify_task()`：任务 → 六个可行动状态之一 + 一个行动入口；
- `health_causes()`：依赖健康项 → 新人看得懂的原因 + 入口；
- `unified_health()`：把依赖快照收敛成**唯一**一份健康视图（未知/过期/异实例不算绿）。

四条纪律：
1. **未知不算绿**：没有快照、快照过期、或快照来自别的实例，一律 `unknown` 并说明原因；
2. **不猜**：任务行读不到就说"未接收"，不把缺失当成功，也不把 0 当"全部正常"；
3. **一个状态一个动作**：每个状态都给出唯一推荐入口，避免"给一堆链接让用户自己挑"；
4. **不改判据**：这里只做展示层的翻译，验收/研究状态/judgement 一律不参与。
"""

from __future__ import annotations

# ── 任务侧六个状态（覆盖 C3 的"未接收/待消费/执行中/待材料/明确失败"）──────────
STATE_NOT_RECEIVED = "not_received"
STATE_PENDING_CONSUME = "pending_consume"
STATE_RUNNING = "running"
STATE_WAITING_MATERIAL = "waiting_material"
STATE_FAILED = "failed"
STATE_DONE = "done"

TASK_STATES = (STATE_NOT_RECEIVED, STATE_PENDING_CONSUME, STATE_RUNNING,
               STATE_WAITING_MATERIAL, STATE_FAILED, STATE_DONE)

TASK_STATE_LABELS = {
    STATE_NOT_RECEIVED: "未接收",
    STATE_PENDING_CONSUME: "待消费",
    STATE_RUNNING: "执行中",
    STATE_WAITING_MATERIAL: "待材料",
    STATE_FAILED: "明确失败",
    STATE_DONE: "已完成",
}

# ── 行动入口（与页面已有路由/标签对应，不新造页面）────────────────────────────
ACTION_NONE = "none"
ACTION_WAIT = "wait"
ACTION_RETRY = "retry"
ACTION_ADD_MATERIAL = "add_material"
ACTION_OPEN_SETTINGS = "open_settings"
ACTION_OPEN_HEALTH = "open_health"
ACTION_VIEW_DETAILS = "view_details"

ACTION_LABELS = {
    ACTION_NONE: "无需操作",
    ACTION_WAIT: "等待即可（可离开页面，任务在后台跑）",
    ACTION_RETRY: "重新提交",
    ACTION_ADD_MATERIAL: "去补材料",
    ACTION_OPEN_SETTINGS: "去设置模型",
    ACTION_OPEN_HEALTH: "看健康页",
    ACTION_VIEW_DETAILS: "看任务详情",
}

# 前端可直接用的入口（相对路由或页内标签）；后端不依赖前端，仅给出约定
ACTION_TARGETS = {
    ACTION_OPEN_SETTINGS: "/settings",
    ACTION_OPEN_HEALTH: "/health",
    ACTION_ADD_MATERIAL: "materials",
    ACTION_VIEW_DETAILS: "details",
}

# 依赖健康里的名称 → 新人视角的原因类别
_MODEL_NAMES = ("llm", "planner")
_SOURCE_NAMES = ("search", "market_source", "embedding")

# 未配置类的措辞（health_registry/settings 的占位符判定沿用既有词）
_UNCONFIGURED_HINTS = ("未配置", "placeholder", "yOUR_API_KEY".lower(), "占位", "缺 api_key")


def _is_unconfigured(reason: str, detail: str) -> bool:
    text = f"{reason} {detail}".lower()
    return any(h in text for h in _UNCONFIGURED_HINTS)


def health_causes(items) -> list[dict]:
    """依赖健康项 → 新人可行动原因（按严重度排序，最多三条）。

    返回 `[{name, state, message, action, action_label}]`。
    只有**真需要动作**的项才进列表：`available` 不进；`degraded` 进但说明"仍可用"。
    """
    out: list[dict] = []
    for it in items or ():
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        state = str(it.get("state") or "").lower()
        reason = str(it.get("reason") or "")
        detail = str(it.get("detail") or "")
        if state == "available" or not state:
            continue
        message = ""
        action = ACTION_OPEN_HEALTH
        if name in _MODEL_NAMES and _is_unconfigured(reason, detail):
            message = f"模型未配置（{name}）——填好 API Key 与端点后即可提交任务"
            action = ACTION_OPEN_SETTINGS
        elif name in _MODEL_NAMES:
            message = f"模型不可用（{name}：{reason or state}）——先看健康页与设置里的端点/额度"
            action = ACTION_OPEN_SETTINGS
        elif name in _SOURCE_NAMES:
            if state == "unknown":
                message = (f"资料源状态未知（{name}：{reason or '无快照'}）"
                           "——未知不等于可用，先看健康页")
            elif state == "degraded":
                message = f"资料源部分不可用（{name}）——研究仍可做，只是少一路来源"
            else:
                message = f"资料源暂不可用（{name}：{reason or state}）"
        else:
            if state == "unknown":
                message = f"{name} 状态未知（{reason or '无快照'}）——未知不等于可用"
            elif state == "degraded":
                message = f"{name} 降级（{reason or '部分不可用'}）"
            else:
                message = f"{name} 不可用（{reason or state}）"
        out.append({"name": name, "state": state, "message": message,
                    "action": action, "action_label": ACTION_LABELS.get(action, "")})
    # 严重度：unavailable > degraded > unknown；模型问题优先（没有它什么都做不了）
    sev = {"unavailable": 0, "degraded": 1, "unknown": 2}
    out.sort(key=lambda c: (0 if c["name"] in _MODEL_NAMES else 1,
                            sev.get(c["state"], 3), c["name"]))
    return out[:3]


def classify_task(*, row=None, timeline=(), material_pending: bool = False,
                  causes=()) -> dict:
    """任务 → `{state, label, action, action_label, message, causes}`。

    `row`：`task_state.read_task()` 的结果（没有就传 None/{}）。
    `timeline`：`task_state.read_submit_timeline()` 的结果（用于区分"到没到/发布没发布"）。
    `material_pending`：任务是否在等材料（Redis 集合 `material_pending_tasks`）。
    `causes`：`health_causes()` 的结果（可选；失败时用来说明"为什么失败、去哪修"）。
    """
    row = row or {}
    causes = list(causes or [])
    status = str(row.get("status") or "").upper()
    events = [str((e or {}).get("event") or "") for e in (timeline or ())
              if isinstance(e, dict)]

    def _out(state: str, message: str, action: str) -> dict:
        return {"state": state, "label": TASK_STATE_LABELS.get(state, state),
                "action": action, "action_label": ACTION_LABELS.get(action, ""),
                "target": ACTION_TARGETS.get(action, ""),
                "message": message, "causes": causes,
                "events": events}

    if not str(row.get("task_id") or ""):
        return _out(STATE_NOT_RECEIVED,
                    "请求还没被接收（服务未启动或正在重启）；确认服务在线后重新提交",
                    ACTION_RETRY)
    if status == "RECEIVED":
        return _out(STATE_PENDING_CONSUME,
                    "已收到，正在等待执行服务消费；若长时间如此，启动时会自动恢复重跑",
                    ACTION_WAIT)
    if status in ("QUEUED", "RUNNING", "PENDING", "QRUNNING"):
        if material_pending:
            return _out(STATE_WAITING_MATERIAL,
                        "任务在等材料：补上原始披露后会自动继续，已完成的步骤不会重跑",
                        ACTION_ADD_MATERIAL)
        return _out(STATE_RUNNING, "正在执行；可以离开页面，进度会在回到页面后恢复",
                    ACTION_WAIT)
    if status in ("FAILED", "CANCELLED"):
        # 失败时优先指向"能修的那个原因"（模型未配置 → 去设置；源不可用 → 健康页）
        first = causes[0] if causes else {}
        action = str(first.get("action") or ACTION_RETRY)
        msg = "任务明确失败"
        if first.get("message"):
            msg += f"：{first['message']}"
        else:
            msg += "；先看任务详情里的失败步骤，再决定重试还是补材料"
            action = ACTION_VIEW_DETAILS
        return _out(STATE_FAILED, msg, action)
    if status in ("SUCCESS", "SUCCESS_WITH_ISSUES"):
        return _out(STATE_DONE, "已完成；请人工复核后再采用（机器验收不等于研究通过）",
                    ACTION_VIEW_DETAILS)
    # 认不出的状态：不猜成"正常"，按未知处理
    return _out(STATE_NOT_RECEIVED,
                f"状态无法识别（{status or '空'}）；按未接收处理，请不要据此认为任务在跑",
                ACTION_VIEW_DETAILS)


def unified_health(dependencies=(), market_sources=None) -> dict:
    """唯一一份健康视图：四态 + 实例 + 观测时间 + 新人原因 + 汇总。

    `dependencies`：`health_registry.snapshot()`（四态）。
    `market_sources`：**旧** `adapters.source_health.get_health()` 的键值形态；
    只在依赖快照里没有对应源条目时才补一条，避免"同一件事两处显示"（C3：统一旧
    source_health 与 dependencies）。补进来的条目同样按四态表述，且**未知不算绿**。
    """
    states = ("available", "degraded", "unavailable", "unknown")
    labels = {"available": "可用", "degraded": "降级（仍可用）",
              "unavailable": "不可用", "unknown": "未知（无快照/已过期/别的实例）"}
    items: list[dict] = []
    for it in dependencies or ():
        if not isinstance(it, dict):
            continue
        state = str(it.get("state") or "").lower()
        if state not in states:
            state = "unknown"          # 不认的状态不许当绿
        items.append({
            "name": str(it.get("name") or ""),
            "state": state, "label": labels[state],
            "reason": str(it.get("reason") or ""),
            "detail": str(it.get("detail") or ""),
            "instance": str(it.get("_instance") or ""),
            "checked_at": it.get("_checked_at") or 0,
            "legacy": False,
        })
    if market_sources:
        have = {i["name"] for i in items}
        for name, info in (market_sources or {}).items():
            key = f"source:{name}"
            if key in have or name in have:
                continue               # 依赖快照已经表达过这件事
            info = info if isinstance(info, dict) else {}
            healthy = bool(info.get("healthy"))
            remaining = float(info.get("remaining") or 0)
            state = "available" if healthy else ("degraded" if remaining <= 0 else "unavailable")
            items.append({
                "name": key, "state": state, "label": labels[state],
                "reason": str(info.get("last_error") or "")[:200],
                "detail": f"连续失败 {info.get('fails') or 0} 次",
                "instance": "", "checked_at": 0, "legacy": True,
            })
    summary = {s: sum(1 for i in items if i["state"] == s) for s in states}
    # 只有"全部可用且至少有一条"才算整体可用；未知/空一律不算绿
    ok = bool(items) and summary["available"] == len(items)
    return {"items": items, "summary": summary, "ok": ok,
            "causes": health_causes(items)}
