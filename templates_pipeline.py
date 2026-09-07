# -*- coding: utf-8 -*-
"""templates_pipeline——计划规范化与主题词域（T10 第一阶段，从 orchestrator_v2 抽取）。

normalize_steps / topic_tokens 原样搬迁为模块函数；orchestrator 保留同名薄委托。
常量（KNOWN_CAPABILITIES / _HUMAN_IN_LOOP_ALLOWED / _TOPIC_STOPWORDS）仍以
orchestrator_v2 为单一来源，函数内晚导入，避免双份漂移与循环导入。
route_template / consolidate_template 深度依赖 messaging/LLM/验收回调，
待依赖注入改造后搬迁（T10b）。
"""
import logging

logger = logging.getLogger("orchestrator_v2")


def normalize_steps(steps: list, max_steps: int) -> list[dict]:
    """规范化计划步骤：去重 ID、剔除空指令、补齐默认字段、限制步数。"""
    from orchestrator_v2 import KNOWN_CAPABILITIES, _HUMAN_IN_LOOP_ALLOWED

    out: list[dict] = []
    seen: set[str] = set()
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("step_id") or i)
        if sid in seen:
            sid = f"{sid}-{i}"
        seen.add(sid)
        instruction = str(s.get("instruction") or "").strip()
        if not instruction:
            continue
        # 验收点兜底（对标标准 3.2 Plan & Execute）：planner 漏写时自动补
        if "验收：" not in instruction:
            instruction = instruction + "\n验收：步骤完成后输出可验证的结果（文件/数据/文本均可）。"
        s["instruction"] = instruction
        s["step_id"] = sid
        # 校验能力字段：非法/多值拼接时回退到 content_summary
        cap = str(s.get("capability") or "content_summary").strip()
        if "," in cap:
            cap = next((c.strip() for c in cap.split(",") if c.strip() in KNOWN_CAPABILITIES), "content_summary")
        elif cap not in KNOWN_CAPABILITIES:
            cap = "content_summary"
        # 能力纠偏：安装依赖的"package"步骤实为环境准备，改派 code_execution 执行 pip
        if cap == "package" and any(
            k in instruction for k in ("安装", "install", "依赖", "pip")
        ):
            cap = "code_execution"
            s["instruction"] = (
                "使用 pip 安装所需依赖（已安装则跳过），并验证 import 成功："
                f"{instruction}"
            )
            instruction = s["instruction"]
        s["capability"] = cap
        mode = str(s.get("mode") or "parallel").strip().lower()
        if mode not in ("pipeline", "parallel", "human_in_loop"):
            mode = "parallel"
        # P1-2：低风险内部能力步骤不允许 human_in_loop，强制串行 pipeline，
        # 避免执行前等人工确认导致整轮超时（如 package 被规划器误标）。
        if mode == "human_in_loop" and cap not in _HUMAN_IN_LOOP_ALLOWED:
            logger.info("低风险步骤强制 pipeline：%s（step %s）", cap, sid)
            mode = "pipeline"
        s["mode"] = mode
        s.setdefault("timeout", 300)
        out.append(s)
    if len(out) > max_steps:
        logger.warning("Plan normalized from %d to %d steps (max_steps=%d)",
                       len(out), max_steps, max_steps)
        out = out[:max_steps]
    return out


def topic_tokens(text: str) -> set[str]:
    """从目标中提取主题词（中文 2-4 字片段 + 英文词，剔除停用词）。"""
    import re as _re

    from orchestrator_v2 import _TOPIC_STOPWORDS

    tokens = set()
    for w in _re.findall(r"[\u4e00-\u9fff]{2,4}", text):
        if w not in _TOPIC_STOPWORDS:
            tokens.add(w)
    tokens |= {w.lower() for w in _re.findall(r"[a-z][a-z0-9-]{2,}", text)}
    return tokens
