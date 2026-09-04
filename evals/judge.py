# -*- coding: utf-8 -*-
"""Ragas 式答案质量评测（对标标准 C2-2.4 / C3-3.6，LLM-as-Judge）。

指标：
- answer_correctness ：答案与 Ground Truth 的事实正确性
- faithfulness       ：回答是否忠实于检索到的上下文（防幻觉）
- context_recall      ：Ground Truth 中的关键事实有多少被检索召回
- context_precision   ：召回的参考资料中信噪比（相关且排位靠前）

用法：python -m evals.run --judge evals/sample_outputs.jsonl
"""

import json

from common import extract_json_object
from evals import zh_prompts


def _loads_loose(text) -> dict | None:
    """宽松 JSON 解析：统一走 common.extract_json_object（容忍围栏/前后缀文字）。"""
    return extract_json_object(text)


def _call(system: str, user: str) -> dict | None:
    from llm_client import call_llm
    raw = call_llm(system, user, expect_json=False)
    text = str((raw or {}).get("content") or "") if isinstance(raw, dict) else str(raw or "")
    return _loads_loose(text)


def _frac(items: list | None) -> float:
    if not items:
        return 0.0
    return sum(1 for x in items if x is True) / len(items)


def answer_correctness(answer: str, ground_truth: str) -> float:
    data = _call(
        zh_prompts.ANSWER_CORRECTNESS,
        zh_prompts.ANSWER_CORRECTNESS.format(
            ground_truth=str(ground_truth)[:1500], answer=str(answer)[:1500],
        ),
    )
    try:
        return float((data or {}).get("score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def faithfulness(answer: str, contexts: list[str]) -> float:
    claims_data = _call(
        zh_prompts.CLAIM_EXTRACTION,
        zh_prompts.CLAIM_EXTRACTION.format(answer=str(answer)[:2000]),
    )
    claims = (claims_data or {}).get("claims") or []
    if not claims:
        return 1.0 if not str(answer or "").strip() else 0.0
    nli = _call(
        zh_prompts.FAITHFULNESS_NLI,
        zh_prompts.FAITHFULNESS_NLI.format(
            context="\n".join(str(c)[:800] for c in contexts[:6]),
            claims=json.dumps(claims, ensure_ascii=False),
        ),
    )
    return _frac((nli or {}).get("supported"))


def context_recall(ground_truth: str, contexts: list[str]) -> float:
    data = _call(
        zh_prompts.CONTEXT_RECALL,
        zh_prompts.CONTEXT_RECALL.format(
            context="\n".join(str(c)[:800] for c in contexts[:6]),
            ground_truth=str(ground_truth)[:1200],
        ),
    )
    return _frac((data or {}).get("attributable"))


def context_precision(question: str, contexts: list[str]) -> float:
    data = _call(
        zh_prompts.CONTEXT_PRECISION,
        zh_prompts.CONTEXT_PRECISION.format(
            question=str(question)[:500],
            contexts="\n".join(f"[{i + 1}] {str(c)[:500]}" for i, c in enumerate(contexts[:8])),
        ),
    )
    relevant = (data or {}).get("relevant") or []
    if not relevant:
        return 0.0
    n = len(relevant)
    total = 0.0
    num_relevant = 0
    for k, r in enumerate(relevant, 1):
        if r is True:
            num_relevant += 1
            total += num_relevant / k
    return total / max(1, num_relevant) if num_relevant else 0.0


def score_record(record: dict) -> dict:
    q = str(record.get("question") or "")
    a = str(record.get("answer") or "")
    ctx = list(record.get("contexts") or [])
    gt = str(record.get("ground_truth") or "")
    return {
        "answer_correctness": round(answer_correctness(a, gt), 3),
        "faithfulness": round(faithfulness(a, ctx), 3),
        "context_recall": round(context_recall(gt, ctx), 3),
        "context_precision": round(context_precision(q, ctx), 3),
    }
