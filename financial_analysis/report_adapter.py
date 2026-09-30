# -*- coding: utf-8 -*-
"""报告适配层：把**已验证输出**变成分析卡与图表声明（只读 `ModelRun`，不猜正文数字）。

分析卡七段（架构 §6）：**判断 → 关键贡献/敏感性 → 计算与原文依据 → 未解释部分/替代解释
→ 对研究的意义 → 下一项验证动作**，外加"这段是什么性质"（观测/会计分解/条件情景/统计估计）。

图表声明与卡共用 `run_id` 与 `output_id`：正文、图、底稿、包必须能对到**同一次运行**，
不允许从报告文字里重新猜数。
"""
from __future__ import annotations

from .contracts import RunStatus
from .registry import spec as _spec

# 输出**结构声明** → 图形（L0-c，2026-09-30 复核 M1）：图型由模型声明的结构决定，
# 不再"分项超过一个就画瀑布"。并行情景不是加总贡献桥，敏感度是排序条形。
_CHART_BY_STRUCTURE = {
    "bridge": "waterfall",        # 可闭合的加总贡献桥
    "scenarios": "bar_grouped",   # 并列情景对比
    "sensitivity": "bar_sorted",  # 单因素敏感度：按影响排序
    "ratio": "bar",
    "trend": "line",
}


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
    "cash_reconciliation": {
        "kind": "现金形成机制（披露调节表复算）",
        "meaning": ("这是现金流量表补充资料的**复算**：说明合并净利润与经营现金流之间的"
                    "会计调节项构成，不表示各项目的经济原因（应付减少不等于融资恶化，"
                    "也不等于回款变差）；起点是**合并净利润**，不是归母净利"),
        "next_action": ("对最大的支撑/拖累项各找一条披露依据（应付/存货/应收的明细或附注），"
                        "再用资产负债表变动与结算条款做旁证；差额非零时先补未取得的调节项"),
    },
    "operating_drivers": {
        "kind": "经营驱动分解（会计分解）",
        "meaning": ("规模/毛利率/逐项费用税项都是**会计分解**：它说明金额从哪来，"
                    "不自动等于业务原因。分产品/分行业/分地区/分销售模式是同一口径的"
                    "**不同切法**，不可相加；均价含产品结构混合，不得命名“提价效果”"),
        "next_action": ("对最大的三项贡献各找一条披露依据（分产品收入/费用明细/附注），"
                        "并检查替代解释（税率、非经营损益、结构变化）后再写结论"),
    },
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


_YUAN_PER_YI = 1e8


def _yi_from_yuan(value, unit: str) -> str:
    """金额显示成**亿元**（原始值仍是元；这里只换显示单位并注明）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    if str(unit or "") == "元":
        return f"{v / _YUAN_PER_YI:+,.2f}亿元"
    return f"{v:+,.2f}{unit or ''}"


def render_cash_reconciliation_block(run) -> list[str]:
    """现金调节桥的三段式正文（U2）：起点与分组 → 最大支撑/拖累 → 缺口变化的来源。"""
    from .contracts import RunStatus
    if str(getattr(run, "model_id", "")) != "cash_reconciliation":
        return []
    if run.status != RunStatus.VALIDATED:
        return []
    diag = {}
    for o in (run.outputs or []):
        if o.diagnostics:
            diag = dict(o.diagnostics)
            break
    rec = diag.get("reconciliation") or {}
    unit = str(diag.get("unit") or "")
    lines: list[str] = []
    for period, item in rec.items():
        comps = "、".join(f"{k} {_yi_from_yuan(v, unit)}"
                         for k, v in (item.get("groups") or {}).items())
        lines.append(f"- **{period} 净利润→经营现金流**：合并净利润 "
                     f"{_yi_from_yuan(item.get('net_profit_yuan'), unit)} ＋ 调节项 "
                     f"{_yi_from_yuan(item.get('adjustments_yuan'), unit)} ＝ 经营现金流 "
                     f"{_yi_from_yuan(item.get('cashflow_yuan'), unit)}"
                     f"（未解释差额 {_yi_from_yuan(item.get('residual_yuan'), unit)}）")
        lines.append(f"  - 分组：{comps}")
    sup = diag.get("largest_support") or {}
    drag = diag.get("largest_drag") or {}
    if sup or drag:
        lines.append("- **最大支撑/拖累（本期调节额）**")
        if sup:
            lines.append(f"  - 支撑：{sup.get('label')} "
                         f"{_yi_from_yuan(sup.get('value'), unit)}"
                         f"（{GROUP_NOTE.get(sup.get('group'), '')}）")
        if drag:
            lines.append(f"  - 拖累：{drag.get('label')} "
                         f"{_yi_from_yuan(drag.get('value'), unit)}"
                         f"（{GROUP_NOTE.get(drag.get('group'), '')}）")
    gap = next((o for o in (run.outputs or [])
                if o.metric == "cash_gap_change"), None)
    if gap is not None:
        comps = "、".join(f"{c.get('label')} {_yi_from_yuan(c.get('value'), unit)}"
                         for c in (gap.components or ()))
        lines.append(f"- **现金缺口变化（{gap.output_period}）**：{_yi_from_yuan(gap.value, unit)}"
                     f" ＝ {comps}；缺口＝经营现金流−合并净利润，缺口缩小不等于现金变好")
    if diag.get("closure_note"):
        lines.append(f"- 说明：{diag['closure_note']}")
    miss = diag.get("items_missing") or {}
    if miss.get("cur"):
        lines.append("- **待核查/补料**：本期未取到调节项 " + "、".join(map(str, miss["cur"])))
    return lines


GROUP_NOTE = {
    "non_cash": "非现金项：折旧摊销/减值/递延税/公允价值等，不涉及当期现金收付",
    "working_capital": "营运资本项：存货与经营性应收应付的增减",
    "other": "其他调节项",
}


def render_operating_drivers_block(run) -> list[str]:
    """经营驱动卡的**三段式**正文（U1）：数值贡献 → 支持/替代解释 → 待核查。

    为什么单独渲染：通用卡只列"每个输出 + 分项"，读者仍看不出"哪三项最大、有没有别的解释、
    还缺哪张表"。这里按研究报告的写法组织，每个数字仍然带 `output <output_id>` 可回查；
    替代解释只列**确定性**算出或披露可直接读到的项（税率反事实、非经营因素、均价含结构）。
    """
    from .contracts import RunStatus
    if str(getattr(run, "model_id", "")) != "operating_drivers":
        return []
    if run.status != RunStatus.VALIDATED:
        return []
    extra = _operating_drivers_extra(run)
    first = next((o for o in run.outputs if o.metric == "net_profit_change"), None)
    unit = str(getattr(first, "unit", "") or "")
    oid = str(getattr(first, "output_id", "") or "")
    lines: list[str] = []
    contribs = [c for c in (extra.get("contributions") or [])
                if str(c.get("component_id")) != "unexplained_residual"]
    if contribs:
        top = sorted(contribs, key=lambda c: -abs(float(c.get("value") or 0)))[:3]
        lines.append("- **三项最大利润贡献**（会计分解；金额为元换算成亿元，"
                     f"原始单位 {unit}）")
        for c in top:
            lines.append(f"  - {c.get('label')} {_yi_from_yuan(c.get('value'), unit)}"
                         f"　output {oid}")
        resid = next((c for c in (extra.get("contributions") or [])
                      if str(c.get("component_id")) == "unexplained_residual"), None)
        if resid is not None:
            lines.append(f"  - 未解释差额 {_yi_from_yuan(resid.get('value'), unit)}"
                         f"（{extra.get('contributions_caveat')}）　output {oid}")
    vp = extra.get("volume_price") or {}
    if vp:
        comps = "、".join(f"{c.get('label')} {_yi_from_yuan(c.get('value'), unit)}"
                         for c in (vp.get("components") or ()))
        lines.append(f"- **量价分解（{vp.get('caliber') or ''}）**：{comps}"
                     f"；{vp.get('caveat')}　output {vp.get('output_id')}")
    for seg in (extra.get("segment_cuts") or []):
        comps = "、".join(f"{c.get('label')} {_yi_from_yuan(c.get('value'), unit)}"
                         for c in (seg.get("components") or []))
        lines.append(f"- **{seg.get('cut')}**：{comps}；{extra.get('segment_caveat')}"
                     f"　output {seg.get('output_id')}")
    alt = extra.get("alternative_explanations") or {}
    if alt:
        lines.append("- **另一个可能解释 / 需要反证的地方**")
        rate = alt.get("effective_tax_rate") or {}
        cf = alt.get("tax_at_prior_rate") or {}
        if rate and cf:
            lines.append(
                f"  - 实际税率 {float(rate.get('prev') or 0):.2%} → "
                f"{float(rate.get('cur') or 0):.2%}"
                f"（{float(rate.get('delta_pp') or 0):+.2f}pp）：所得税的“贡献”多来自"
                f"利润下滑本身；按上年税率折算本应 "
                f"{_yi_from_yuan(cf.get('at_prior_rate_yuan'), '元')}，"
                f"税率因素实际多吃掉 {_yi_from_yuan(cf.get('rate_effect_yuan'), '元')}")
        for key, item in (alt.get("non_operating_items") or {}).items():
            lines.append(f"  - {item.get('label')} "
                         f"{_yi_from_yuan(item.get('contribution_yuan'), '元')}："
                         f"{item.get('note')}")
        if alt.get("structure_vs_price"):
            lines.append(f"  - {alt['structure_vs_price']}")
    gaps = extra.get("data_gaps") or {}
    todo: list[str] = []
    if gaps.get("segments_skipped"):
        todo.append("分段缺成本的口径：" + "；".join(
            str(x) for x in gaps["segments_skipped"][:3])
            + "（补 10% 以上表或分部附注才能进分段分解）")
    if gaps.get("volume_price_skipped"):
        todo.append("量价缺销量/收入：" + "；".join(
            str(x) for x in gaps["volume_price_skipped"][:3]))
    if gaps.get("line_items_missing"):
        todo.append("利润表明细未取到：" + "、".join(map(str, gaps["line_items_missing"])))
    if todo:
        lines.append("- **待核查/补料**：" + "；".join(todo))
    return lines


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
    # U1（2026-10-01）：经营驱动卡要把**逐项贡献、分段切法、量价与替代解释**一起给出来——
    # 只给"毛利/毛利线以下"两段，读者仍不知道钱从哪来（这正是 K3 之后仍存在的浅解释）。
    extra = _operating_drivers_extra(run) if str(run.model_id) == "operating_drivers" else {}
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
        **extra,
    }


def _operating_drivers_extra(run) -> dict:
    """经营驱动卡的附加段：逐项贡献、分段切法、量价、替代解释（都取自**同一次运行**）。

    每个数字都带 `output_id`，正文/图/底稿能对回同一次运行；分段与量价只在真有输出时出现。
    """
    def _find(metric):
        return [o for o in run.outputs if o.metric == metric]

    detail = _find("net_profit_change_detail")
    segments = _find("gross_profit_change_by_segment")
    volume = _find("volume_price_decomposition")
    diag = {}
    for o in (run.outputs or []):
        if o.diagnostics:
            diag = dict(o.diagnostics)
            break
    out: dict = {}
    if detail:
        items = list(detail[0].components or ())
        out["contributions"] = [
            {"output_id": detail[0].output_id, "component_id": c.get("component_id"),
             "label": c.get("label"), "value": c.get("value"), "unit": c.get("unit"),
             "formula": c.get("formula")} for c in items
        ]
        out["contributions_caveat"] = (
            "未列出的项目留在“未解释差额”里：既不当零，也不摊到已列项目上")
    if segments:
        out["segment_cuts"] = [
            {"output_id": o.output_id, "cut": str(o.label),
             "components": [{"component_id": c.get("component_id"),
                             "label": c.get("label"), "value": c.get("value"),
                             "unit": c.get("unit")} for c in (o.components or ())],
             "unclassified": o.residual}
            for o in segments
        ]
        out["segment_caveat"] = (
            "每种切法各自覆盖同一口径，**不可跨切法相加**；未分类差额是其他业务/口径差")
    if volume:
        out["volume_price"] = {
            "output_id": volume[0].output_id, "label": volume[0].label,
            "caliber": diag.get("volume_price_caliber") or "",
            "components": [{"component_id": c.get("component_id"),
                            "label": c.get("label"), "value": c.get("value"),
                            "unit": c.get("unit"), "formula": c.get("formula")}
                           for c in (volume[0].components or ())],
            "caveat": "均价＝该口径收入/销量，含产品结构混合，不得命名“提价效果”",
        }
    alt = diag.get("alternative_explanations") or {}
    if alt:
        out["alternative_explanations"] = alt
    gaps = {
        "line_items_missing": diag.get("line_items_missing") or [],
        "line_items_rejected": diag.get("line_items_rejected") or [],
        "segments_skipped": diag.get("segments_skipped") or [],
        "volume_price_skipped": diag.get("volume_price_skipped") or [],
    }
    if any(gaps.values()):
        out["data_gaps"] = gaps
    return out


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
    # 结构来自**注册模型声明**（按 metric 找 OutputSpec），找不到才退回按分项数量判断；
    # 并行情景/敏感度不会被画成瀑布（复核 M1）。
    structure = ""
    try:
        for _ospec in (_spec(run.model_id).outputs or ()):
            if _ospec.metric == out.metric:
                structure = str(getattr(_ospec, "structure", "") or "")
                break
    except Exception:                                   # noqa: BLE001 - 未注册模型
        structure = ""
    kind = _CHART_BY_STRUCTURE.get(structure) or (
        "waterfall" if (len(labels) > 1 and not structure) else "bar")
    return {
        "available": True, "run_id": run.run_id, "output_id": out.output_id,
        "kind": kind, "structure": structure or "未声明（按分项数量回退）",
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
