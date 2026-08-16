# -*- coding: utf-8 -*-
"""评测驱动反思（对标标准 C3-3.6）：任务产出先过评测闸门，
达标直接交付，未达标则把评测分数作为反思证据。"""

import json
import logging
import os

logger = logging.getLogger(__name__)


def match_case(goal: str):
    """按目标关键词匹配评测案例；返回案例或 None。"""
    from evals.run import load_cases

    g = str(goal or "")
    best, best_score = None, 0
    for c in load_cases():
        pts = " ".join(str(p) for p in c.get("ground_truth_points", []))
        hay = (str(c.get("goal") or "") + pts).lower()
        score = sum(1 for k in _keywords(g) if k in hay)
        if score > best_score:
            best, best_score = c, score
    return best if best_score > 0 else None


def _keywords(text: str) -> list[str]:
    import re
    out = set()
    for m in re.findall(r"[\u4e00-\u9fff]{2,4}", str(text)):
        for i in range(len(m) - 1):
            bg = m[i:i + 2]
            if bg not in (
                "分析", "生成", "报告", "可视化", "市场", "数据", "训练",
                "模型", "输出", "包括", "主要", "技术", "趋势", "一个",
                "用", "在", "和", "并", "的", "了", "请", "做",
            ):
                out.add(bg)
    return list(out)


def build_record(task_id: str, goal: str, report: str, completed_all: dict) -> dict | None:
    """构造评测记录：question=目标，answer=报告节选，contexts=检索结果，ground_truth=案例要点。"""
    case = match_case(goal)
    if not case:
        return None
    contexts = []
    for r in (completed_all or {}).values():
        if not isinstance(r, dict) or r.get("status") != "SUCCESS":
            continue
        try:
            parsed = json.loads(str(r.get("result") or ""))
        except Exception:
            continue
        if isinstance(parsed, list):
            for it in parsed:
                t = f"{it.get('title', '')}：{it.get('snippet', '')}"[:800]
                if t.strip("：") and t not in contexts:
                    contexts.append(t)
    return {
        "id": f"{task_id}-{case.get('id')}",
        "question": str(goal)[:500],
        "answer": str(report or "")[:2500],
        "contexts": contexts[:6],
        "ground_truth": "\n".join(str(p) for p in case.get("ground_truth_points", [])),
        "case_id": case.get("id"),
    }


def eval_gate(task_id: str, goal: str, report: str, completed_all: dict) -> tuple[bool, dict]:
    """跑评测闸门。返回 (是否匹配到案例, 分数 dict)。LLM 失败时抛异常由调用方兜底。"""
    record = build_record(task_id, goal, report, completed_all)
    if not record:
        return False, {}
    from evals.judge import score_record
    scores = score_record(record)
    return True, scores


def gate_passed(scores: dict) -> bool:
    if not scores:
        return False
    threshold = float(os.environ.get("EVAL_ACCEPT_THRESHOLD", "0.7"))
    vals = [v for v in scores.values() if isinstance(v, (int, float))]
    return bool(vals) and (sum(vals) / len(vals)) >= threshold
