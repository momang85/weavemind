# -*- coding: utf-8 -*-
"""报告适配层：把**已验证输出**变成分析卡与图表声明（只读 `ModelRun`，不猜正文数字）。

分析卡七段（架构 §6）：**判断 → 关键贡献/敏感性 → 计算与原文依据 → 未解释部分/替代解释
→ 对研究的意义 → 下一项验证动作**，外加"这段是什么性质"（观测/会计分解/条件情景/统计估计）。

图表声明与卡共用 `run_id` 与 `output_id`：正文、图、底稿、包必须能对到**同一次运行**，
不允许从报告文字里重新猜数。
"""
from __future__ import annotations

from .contracts import RunStatus


def _yi(value: float | None, unit: str = "亿元") -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}{unit}"


def _signed(value: float | None, unit: str = "亿元") -> str:
    if value is None:
        return "—"
    return f"{value:+,.2f}{unit}"


# 卡片的**性质与下一步按模型给**（K3，2026-09-30）：此前"下一项验证动作"与"性质"对所有
# 模型都是一句话——现金质量/情景的卡上写着"取得毛利线以下的利润表明细"，读者据此去补
# 的材料跟这张卡要回答的问题无关（实机 `ui-603f626cbe` 的 cash_quality 卡）。
_CARD_TRAITS: dict[str, dict[str, str]] = {
    "profit_bridge": {
        "kind": "会计分解",
        "meaning": ("会计恒等式分解只说明金额构成；是否构成业务原因需要另行证据，"
                    "算术校验不等于因果支持"),
        "next_action": ("取得「毛利线以下」的利润表明细（费用/税项/投资收益/少数股东），"
                        "再判断这一段由哪些项目构成"),
    },
    "cash_quality": {
        "kind": "现金质量（观察比率）",
        "meaning": ("经营现金流与归母净利润归属层不同：这是**观察比率**，不是现金流量表"
                    "调节恒等式；差额里含营运资本变动、折旧摊销、减值等非现金项"),
        "next_action": ("取得现金流量表附注（折旧摊销、营运资本变动、减值）与收款结算条款，"
                        "再判断差额由哪些项目构成"),
    },
    "working_capital": {
        "kind": "营运资金占用",
        "meaning": "占款天数只说明周转快慢，不说明可回收性；账龄与坏账计提要另取证据",
        "next_action": "取得应收/应付/存货的账龄与减值计提明细，再判断占款质量",
    },
    "scenario_sensitivity": {
        "kind": "条件情景（假设成立时）",
        "meaning": ("情景是**假设成立时**的条件推算：不显示发生概率，也不显示预测置信区间；"
                    "假设由使用者设定，未披露的参数不得当成事实"),
        "next_action": ("明确每条假设的来源与区间（谁设定、依据什么），"
                        "并对最敏感的那一项做区间对照"),
    },
}
_CARD_TRAITS_FALLBACK = {
    "kind": "注册模型分析",
    "meaning": "结论只在这份输入与这组假设下成立；换数据或换参数都会产生新的运行",
    "next_action": "按模型说明补齐输入，或在面板上改假设后复算对照",
}


def card_traits(model_id: str) -> dict:
    """该模型的卡片性质/意义/下一步（未注册的模型给通用文案，不冒充已知模型）。"""
    return dict(_CARD_TRAITS.get(str(model_id or ""), _CARD_TRAITS_FALLBACK))


def analysis_card(run, output_id: str = "") -> dict:
    """一张卡：字段齐备、性质明确、每个数字带 `output_id`。"""
    if run.status != RunStatus.VALIDATED:
        return {
            "kind": "会计分解", "status": run.status, "run_id": run.run_id,
            "title": f"{run.model_id}：未产出可交付结论",
            "judgement": "本次不产出结论",
            "why_not": run.reason,
            "next_action": _next_action(run.status),
            "run_status": run.status,
        }
    out = run.output(output_id) if output_id else (run.outputs[0] if run.outputs else None)
    if out is None:
        return {"kind": "会计分解", "status": RunStatus.FAILED, "run_id": run.run_id,
                "title": f"{run.model_id}：找不到该 output_id（{output_id}）",
                "judgement": "不产出结论", "why_not": "output_id 不在本次运行里",
                "next_action": "核对 output_id 与 run_id 是否同版"}
    comps = list(out.components or ())
    named = "、".join(f"{c.get('label')} {_signed(c.get('value'), out.unit)}" for c in comps)
    unexplained = [c for c in comps if "未解释" in str(c.get("label") or "")
                   or "以下" in str(c.get("label") or "")]
    traits = card_traits(run.model_id)
    return {
        "kind": traits["kind"],
        "status": run.status,
        "run_id": run.run_id,
        "output_id": out.output_id,
        "title": f"{out.entity or out.entity_id} {out.output_period} {out.label}",
        "judgement": (f"{out.label} {_signed(out.value, out.unit)}；"
                      f"其中 {named or '无明细贡献项'}"),
        "drivers": [{"output_id": out.output_id, "label": c.get("label"),
                     "value": c.get("value"), "unit": c.get("unit") or out.unit,
                     "formula": c.get("formula")} for c in comps],
        "basis": {
            "formula": out.formula,
            "inputs": list(out.inputs),
            "input_periods": list(out.input_periods),
            "unit": out.unit, "currency": out.currency, "caliber": out.caliber,
            "observation_hashes": (out.diagnostics or {}).get("input_hashes") or {},
        },
        "unexplained": {
            "value": (unexplained[0].get("value") if unexplained else out.residual),
            "unit": out.unit,
            "note": ("这一段含费用、税项、投资收益、少数股东等：**缺明细表时只能说"
                     "“观察成立、原因待证”**，不得把毛利率的百分点差当成金额归因"),
            "items": [c.get("label") for c in unexplained],
        },
        "meaning": traits["meaning"],
        "assumptions": list(out.assumptions),
        "limits": list(out.limits),
        "reproduction": {
            "run_id": run.run_id, "model_id": run.model_id,
            "model_version": run.model_version, "impl_version": run.impl_version,
            "dataset_hash": run.dataset_hash, "params_hash": run.params_hash,
            "validation": run.validation.get("checks") if run.validation else {},
        },
        "next_action": traits["next_action"],
    }


def _next_action(status: str) -> str:
    return {
        RunStatus.MISSING_INPUT: "补齐缺失的原始披露（两期的归母净利润与毛利，同主体同口径）",
        RunStatus.NOT_APPLICABLE: "换适用模型或换研究问题（当前数据形态不适用两期金额桥）",
        RunStatus.NOT_COMPUTABLE: "先补齐分母或改写口径，不要用 0 或空值兜底",
        RunStatus.VALIDATION_FAILED: "复核输入与算式；验证未过前不得把结果写进正文",
    }.get(status, "查看运行诊断，或按缺口清单补材料")


def chart_spec(run, output_id: str = "") -> dict:
    """图的**声明**（不渲染）：只消费验证输出，单位/期间/口径/run_id 一并带走。"""
    out = run.output(output_id) if output_id else (run.outputs[0] if run.outputs else None)
    if run.status != RunStatus.VALIDATED or out is None:
        return {"available": False, "reason": run.reason or "无已验证输出",
                "run_id": run.run_id}
    labels = [c.get("label") for c in (out.components or ())] or [out.label]
    values = [c.get("value") for c in (out.components or ())] or [out.value]
    return {
        "available": True, "run_id": run.run_id, "output_id": out.output_id,
        "kind": "waterfall" if len(labels) > 1 else "bar",
        "title": f"{out.entity or out.entity_id} {out.output_period} {out.label}",
        "categories": labels, "series": [{"name": out.label, "values": values}],
        "unit": out.unit, "currency": out.currency, "period": out.output_period,
        "caliber": out.caliber,
        "source": {"run_id": run.run_id, "dataset_hash": run.dataset_hash,
                   "inputs": list(out.inputs)},
        "note": "图与卡共用同一次运行：改数据即改 run_id，旧图不得复用",
    }


def delivery_binding(run, output_ids=()) -> dict:
    """交给交付链的身份块：本次运行引用到**哪些 output_id / 数据集 hash**。"""
    outs = [o for o in run.outputs if not output_ids or o.output_id in set(output_ids)]
    return {
        "run_id": run.run_id, "model_id": run.model_id,
        "model_version": run.model_version, "impl_version": run.impl_version,
        "dataset_hash": run.dataset_hash, "params_hash": run.params_hash,
        "status": run.status,
        "validation_ok": bool((run.validation or {}).get("ok")),
        "outputs": [{"output_id": o.output_id, "metric": o.metric,
                     "output_hash": o.output_hash} for o in outs],
    }
