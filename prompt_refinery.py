# -*- coding: utf-8 -*-
"""提示词自迭代（LLM 驱动的四步循环）：
1. v1 提示词运行任务，收集步骤/结果/报告/反思结论/图表校验信息作为证据；
2. 由 LLM 对照六要素标准（详细目标/足够上下文/角色/受众/样例/结构化输出）分析差距；
3. LLM 总结问题并产出改进版提示词（fix_prompt），附理由；
4. 校验后写入 prompt_registry 覆盖，下一轮任务自动生效 → 回到第 1 步。
"""

import json
import logging
import os
import threading
import time

from prompt_registry import record_override

logger = logging.getLogger(__name__)

_LAST_RUN: dict[str, float] = {}
_LOCK = threading.Lock()

REFINERY_SYSTEM = """你是资深提示词工程师。系统会把一次真实任务的执行证据交给你，你的工作是对照标准找出"输出与预期的差距"，并产出改进后的提示词。
受众：你的改进版提示词将直接用于下一轮任务，必须具体、可执行、可直接替换。

## 六要素审计标准（逐项对照）
① 详细任务目标：指令是否写清了要交付什么
② 足够上下文：是否给了前序结果/数据路径/URL 等必要信息
③ 角色：是否定义了执行者身份
④ 受众：是否按目标推断输出面向谁（董事会/CTO/普通用户/工程师）
⑤ 样例/标准：是否有结构化的格式示例或验收标准
⑥ 结构化输出格式：是否明确要求了输出形态（Markdown/JSON/代码文件等）

输出严格JSON：
{"findings":[
  {"target":"planner|reflect|step:web_search|step:code_execution|content_summary|report_generator|critic",
   "issue":"对照六要素的具体问题", "evidence":"证据（输出中的原文/现象）",
   "fix_prompt":"一段可追加在现有提示词末尾的补充要求", "rationale":"为什么这样改"}],
 "apply": true|false}

规则：
1. 只改有证据的问题，禁止无中生有；没有明显差距就 apply=false
2. fix_prompt 是【追加式】补充要求：不要重写整份提示词，保留原有契约，
   只针对发现的缺口增补（例如补上受众定义、输出格式示例、验收标准），
   至少包含 角色/受众/输出/要求/标准 之一
3. 一次最多 3 条 finding，按影响力从大到小排序
4. 不要为修复引入新的风险（如允许 LLM 编造数据、跳过安全校验）
只输出JSON。"""


def _collect_evidence(
    goal: str, all_steps: list[dict], completed_all: dict,
    report: str, flags: dict,
) -> str:
    parts = [f"任务目标：\n{goal}"]
    step_lines = []
    for s in (all_steps or [])[-12:]:
        r = completed_all.get(s.get("step_id"), {}) if completed_all else {}
        st = r.get("status") if isinstance(r, dict) else "?"
        res = str(r.get("result") or "")[:160] if isinstance(r, dict) else ""
        ins = str(s.get("instruction") or "")[:120]
        step_lines.append(f"- [{st}] {s.get('step_id')} ({s.get('capability')}) 指令:{ins} 结果:{res}")
    parts.append("执行步骤与结果：\n" + "\n".join(step_lines))
    parts.append(f"最终报告（节选）：\n{str(report or '')[:2500]}")
    fl = []
    for k in ("reflection_used", "has_failure", "charts_skipped", "replan_used", "iterations"):
        v = (flags or {}).get(k)
        if v:
            fl.append(f"{k}={v}")
    if fl:
        parts.append("任务标志：\n" + "；".join(fl))
    return "\n\n".join(parts)


def _maybe_run(messaging, task_id: str, goal: str, all_steps, completed_all, report, flags) -> dict:
    """满足触发条件才跑自迭代（控成本）：
    任务发生反思/失败/图表跳过/重规划，且距上次运行超过最小间隔。"""
    tid = str(task_id or "")
    forced = bool(os.environ.get("WEAVEMIND_REFINERY_ENABLED"))
    # 只对真实 UI 任务自迭代；测试/自动化任务（t-* 等）不得触发，
    # 防止测试污染生产提示词注册表
    if not forced and not tid.startswith("ui-"):
        return {"ran": False, "reason": "not_ui_task"}
    interval = float(os.environ.get("WEAVEMIND_REFINE_MIN_INTERVAL", "300") or 300)
    now = time.time()
    with _LOCK:
        last = _LAST_RUN.get("global", 0)
        if now - last < interval:
            return {"ran": False, "reason": "rate_limited"}
    trigger = bool(
        (flags or {}).get("reflection_used")
        or (flags or {}).get("has_failure")
        or (flags or {}).get("charts_skipped")
        or (flags or {}).get("replan_used")
    )
    if not trigger and not os.environ.get("WEAVEMIND_REFINE_ALWAYS"):
        return {"ran": False, "reason": "no_trigger"}
    return refine_after_task(messaging, task_id, goal, all_steps, completed_all, report, flags)


def refine_after_task(
    messaging, task_id: str, goal: str, all_steps, completed_all,
    report: str, flags: dict | None = None,
) -> dict:
    """执行一轮自迭代：LLM 分析证据 → 产出改进 → 校验并写入注册表。"""
    try:
        from llm_client import call_llm
        from ws_helpers import push_progress

        evidence = _collect_evidence(goal, all_steps, completed_all, report, flags or {})
        push_progress(messaging, task_id, "log", {
            "type": "prompt_refine", "agent": "prompt_refinery",
            "message": "Prompt refinery: analyzing task output for prompt gaps",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        })
        raw = call_llm(REFINERY_SYSTEM, evidence, expect_json=False)
        text = str((raw or {}).get("content") or "") if isinstance(raw, dict) else str(raw or "")
        data = _loads_loose(text)
        findings = (data or {}).get("findings") or []
        if not data or not findings or not data.get("apply"):
            return {"ran": True, "applied": 0, "findings": 0}
        applied, rejected = 0, []
        for f in findings[:3]:
            ok, issues = record_override(
                str(f.get("target") or "").strip(),
                str(f.get("fix_prompt") or ""),
                str(f.get("rationale") or ""),
                trigger_task=task_id,
            )
            if ok:
                applied += 1
            else:
                rejected.append({"target": f.get("target"), "issues": issues})
        with _LOCK:
            _LAST_RUN["global"] = time.time()
        push_progress(messaging, task_id, "log", {
            "type": "prompt_refine", "agent": "prompt_refinery",
            "message": f"Prompt refinery: applied {applied} override(s), rejected {len(rejected)}",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        })
        logger.info("Prompt refinery applied %d overrides (task %s)", applied, task_id)
        return {"ran": True, "applied": applied, "findings": len(findings), "rejected": rejected}
    except Exception as exc:
        logger.warning("Prompt refinery failed: %s", str(exc)[:200])
        return {"ran": False, "error": str(exc)[:200]}


def _loads_loose(text: str) -> dict | None:
    import re
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i = t.find("{")
    if i >= 0:
        depth = 0
        for j in range(i, len(t)):
            if t[j] == "{":
                depth += 1
            elif t[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[i:j + 1])
                    except Exception:
                        break
    try:
        return json.loads(t, strict=False)
    except Exception:
        return None


def maybe_refine(messaging, task_id: str, goal: str, all_steps, completed_all, report, flags=None) -> dict:
    """安全入口：任何异常都不影响任务收尾。"""
    try:
        return _maybe_run(messaging, task_id, goal, all_steps, completed_all, report, flags)
    except Exception as exc:
        logger.warning("maybe_refine failed: %s", str(exc)[:150])
        return {"ran": False, "error": str(exc)[:150]}
