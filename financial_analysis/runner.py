# -*- coding: utf-8 -*-
"""受控执行器：编译分析计划 → 跑白名单算子 → 带身份与独立验证的 `ModelRun`。

身份（§4）：`run_id` 绑定 **dataset hash + 模型/实现版本 + 参数 hash + as_of**。
数据集、参数、实现任一改变 → 新 run_id，旧 run 用 `revalidate()` 会如实报 `expired`。
状态至少区分：缺数据 / 不适用 / 不可计算 / 运行失败 / 验证失败 / 已验证——
它们与任务 SUCCESS、研究 ready、人工批准**分别记录**，不互相代替。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from .contracts import (
    AnalysisPlan, MissingInput, ModelRun, NotApplicable, NotComputable, PlanItem,
    RunStatus, ValidatedOutput, _hash,
)
from .registry import OPERATORS, available_for, ratio_specs, spec, specs
from .validation import RULES_VERSION, validate_output


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _params_hash(params: dict) -> str:
    return _hash(params or {})[:16]


def _run_id(spec_, dataset, params: dict, impl_version: str) -> str:
    return _hash({
        "model_id": spec_.model_id, "model_version": spec_.version,
        "impl_version": impl_version, "dataset_hash": dataset.dataset_hash,
        "params": params or {}, "as_of": dataset.manifest.as_of,
        "environment": os.name,
    })


def compile_plan(question: str, dataset, *, prefer=()) -> AnalysisPlan:
    """问题 → 计划：**先按问题类型选模型**，再核输入；每一次采纳与拒绝都有理由（L2）。

    2026-09-30 架构复核 M2 的反例：此前只按"字段齐备"选模型，于是
    "只研究现金转换，不做情景预测"与"只分析营收变动的量价因素"选出来的三模型一模一样。
    现在：

    1. `questions.classify(question)` 把**研究者原话**映射到问题类型（保留原问题，不改写）；
    2. 只有 `ModelSpec.question_types` 与命中类型相交的注册模型才可能被采用——输入齐备
       但**与所问问题无关**的模型进 `rejected`，理由写明"它回答的是哪一类问题"；
    3. 问题类型需要的材料在数据里没有 → 记 `gaps`（缺什么、去哪补），**不换跑无关模型**
       （量价结构目前没有注册模型能回答：如实说，不拿利润桥充数）；
    4. 问题没有命中任何已规则化类型 → 退回**既有**行为（按输入齐备性给可用模型），并在
       `notes` 里明说"问题未规则化，本轮按输入齐备性选择"，读者不会误以为问题被理解了；
    5. 同一 (dataset, 模型, 参数, 规则版本) 的运行按 `run_id` **身份复用**（`save_run`
       同 id 覆盖），只有受影响的节点会重算——不在计划层另造一套缓存。
    """
    from . import questions as _q

    avail = available_for(dataset)
    hits = _q.classify(question)
    # R3（09-30 下午复核）：**逐子句**判意图。同一子句里出现预测意图时，该子句是"预测子问题"，
    # 不得拿同子句的指标词去启动历史模型（"预测明年的经营现金流"不是历史现金问题）。
    clause_hits: list[dict] = []
    for c in (_q.clauses(question) or [str(question or "")]):
        ch = _q.classify(c)
        clause_hits.append({"clause": c, "hits": ch,
                            "forecast": any(h["qid"] == _q._FORECAST_QID for h in ch)})
    qids = [h["qid"] for h in hits]
    labels = [h["label"] for h in hits]
    _answerable = [h["qid"] for ch in clause_hits if not ch["forecast"] for h in ch["hits"]]
    relevant = _q.models_for(dict.fromkeys(_answerable))
    needs = _q.needs_for(qids)
    notes: list[str] = []
    gaps: list[str] = []

    # 问题需要的材料在数据里没有 → 如实列缺口（量价这类"没有模型也没有资料"的最典型）
    for n in needs:
        missing = [m for m in n["metrics"]
                   if dataset.get(m, dataset.period_at(0)) is None
                   and dataset.get(m, dataset.period_at(-1)) is None]
        if missing:
            gaps.append(f"{n['label']}：缺材料 {missing}——{n['how']}")

    avail_set = set(avail)
    adopted: list[PlanItem] = []
    rejected: list[dict] = []
    exploratory: list[str] = []
    if qids:
        order = [m for m in (prefer or ()) if m in relevant] + \
                [m for m in relevant if m not in (prefer or ())]
        notes.append("按问题类型选择模型：" + "、".join(labels)
                     + f"（命中词：{'、'.join(w for h in hits for w in h['matched'])}）")
        _neg = sorted({w for h in hits for w in (h.get("excluded") or [])})
        if _neg:
            notes.append("以下词出现在**否定**语境里，未据此选模型："
                         + "、".join(_neg) + "（字符串规则，读者可核对）")
        for mid in order:
            try:
                m = spec(mid)
            except Exception:                        # noqa: BLE001 - 未注册按无关处理
                continue
            need = [f"{i.metric}@{i.period_offset}" for i in m.inputs]
            missing = [i.metric for i in m.inputs
                       if dataset.get(i.metric, dataset.period_at(i.period_offset)) is None]
            if missing:
                rejected.append({"model_id": mid, "reason": "缺输入",
                                 "missing": sorted(set(missing)), "needs": need,
                                 "detail": "该模型能回答这个问题类型，但输入不齐备"})
                continue
            adopted.append(PlanItem(
                model_id=mid, question=m.question,
                reason=(f"适用问题：{'、'.join(labels)}；输入齐备（{', '.join(need)}）"),
                outputs=tuple(o.metric for o in m.outputs),
                question_types=tuple(m.question_types)))
        # 其余**已注册**模型：逐个说明为什么没被采用（与问题无关 / 缺输入 / 期间不足）
        for m in specs():
            if any(a.model_id == m.model_id for a in adopted) \
                    or any(r["model_id"] == m.model_id for r in rejected):
                continue
            _mine = tuple(m.question_types)
            missing = sorted({i.metric for i in m.inputs
                              if dataset.get(i.metric,
                                             dataset.period_at(i.period_offset)) is None})
            if _mine and not (set(_mine) & set(qids)):
                rejected.append({"model_id": m.model_id, "reason": "与所问问题无关",
                                 "missing": missing,
                                 "answers": "、".join(_mine),
                                 "detail": (f"本次问题是「{_q.describe(qids)}」，"
                                            f"本模型回答「{_q.describe(_mine)}」："
                                            "数据齐备也不等于适用于这个问题")})
                continue
            if missing:
                reason = "缺输入"
            elif len([p for p in (dataset.manifest.periods or ()) if p]) < 2:
                reason = "期间不足两期（两期桥接需要一个以上的年度期间）"
            else:
                reason = "当前数据形态不适用"
            rejected.append({"model_id": m.model_id, "reason": reason, "missing": missing,
                             "needs": [f"{i.metric}@{i.period_offset}" for i in m.inputs]})
    else:
        # 问题没有命中任何已规则化类型 → **受限计划**（R3，09-30 下午复核）：不自动把
        # "输入齐备"的模型采入正文（那等于拿无关探索结果当答案），只把它们列为**可选探索**。
        notes.append("问题未命中任何已规则化的问题类型（**待澄清**）：本轮不采用任何模型、"
                     "不把探索性读数写进正文；请在问题里点名要分析什么"
                     "（利润变化/现金转化/营运资金/条件情景），或先在问题类型表里补一条")
        order = [m for m in (prefer or ()) if m in avail] + \
                [m for m in avail if m not in (prefer or ())]
        for mid in order:
            try:
                m = spec(mid)
            except Exception:                        # noqa: BLE001
                continue
            exploratory.append(mid)
            missing = sorted({i.metric for i in m.inputs
                              if dataset.get(i.metric,
                                             dataset.period_at(i.period_offset)) is None})
            rejected.append({"model_id": mid, "reason": "问题未规则化（可选探索）",
                             "missing": missing,
                             "detail": ("问题没被规则化：本模型只是**可选探索**，"
                                        "本轮不采入正文")})
        if exploratory:
            notes.append("可选探索模型（不进正文、不作为回答）：" + "、".join(exploratory))

    # ── R3：子问题状态 + **与实际计划同源**的说明 ──────────────────────────────
    # 说明在采用/拒绝都定下来之后再生成，因此不会再出现"采用了现金模型，却写不采用任何模型"
    # 这种自相矛盾（复核原文的反例）。
    _adopted_ids = [a.model_id for a in adopted]
    subquestions: list[dict] = []
    for ch in clause_hits:
        ch_qids = [h["qid"] for h in ch["hits"]]
        if not ch_qids:
            continue
        _fq = _q._FORECAST_QID in ch_qids
        _models = [] if _fq else [m for m in _q.models_for(ch_qids) if m in _adopted_ids]
        if _fq:
            _note = ("预测/趋势外推（含概率、回归、目标价、估值）本版本**不开放**："
                     "这一子问题保持**未回答**，不出预测数；门槛与禁用清单见 "
                     "docs/统计预测门槛与禁用清单_20260930.md")
        elif _models:
            _note = "已采用 " + "、".join(_models) + " 回答这一子问题"
        elif "volume_price" in ch_qids:
            _note = ("量价结构分解目前**没有注册模型**：只给资料清单与缺口，"
                     "不给推测性结论")
        else:
            _note = "命中了问题类型，但没有可采用的模型（见 rejected 的原因）"
        subquestions.append({
            "clause": ch["clause"], "qids": ch_qids,
            "labels": [h["label"] for h in ch["hits"]],
            "kind": ("forecast" if _fq else
                     ("scenario" if "scenario" in ch_qids else "history")),
            "answered": bool(_models), "models": _models, "note": _note})
    if any(s["kind"] == "forecast" for s in subquestions):
        notes.append("预测子问题**保持未回答**（本版本不开放预测）："
                     + "；".join(f"「{s['clause'][:24]}」" for s in subquestions
                                 if s["kind"] == "forecast")
                     + "。同一次请求里的历史/情景子问题照常回答；门槛与禁用清单见 "
                     "docs/统计预测门槛与禁用清单_20260930.md")
    # 每个**未回答**的子问题都要在计划说明里逐条给出原因（量价"没有注册模型"、
    # 预测"不开放"、其余"没有可采用的模型"）——说明与实际计划同源，不再自相矛盾。
    for s in subquestions:
        if not s["answered"]:
            notes.append(f"未回答的子问题「{s['clause'][:28]}」：{s['note']}")
    if qids and not _adopted_ids:
        notes.append("本次**没有任何模型被采用**：所问问题要么当前不开放（预测/量价），"
                     "要么没有可采用的模型——不拿无关读数充数")
    if gaps:
        notes.append("问题所需材料缺口见 gaps：缺料时**只停缺输入的模型**，不换跑无关模型")
    return AnalysisPlan(question=str(question or ""), dataset_hash=dataset.dataset_hash,
                        adopted=tuple(adopted), rejected=tuple(rejected),
                        question_types=tuple(qids), question_type_labels=tuple(labels),
                        needs=tuple(needs), gaps=tuple(gaps), notes=tuple(notes),
                        subquestions=tuple(subquestions),
                        exploratory=tuple(exploratory))


def run(model_id: str, dataset, *, params: dict | None = None,
        question: str = "") -> ModelRun:
    """跑一个注册模型 → `ModelRun`（含独立验证结论；**不写任何库、不发网络请求**）。"""
    from .operators import profit_bridge as _pb

    m = spec(model_id)
    impl, compute, _gold = OPERATORS[m.operator]
    params = dict(params or {})
    bad = [k for k in params if k not in m.allowed_params]
    impl_version = str(getattr(impl, "IMPL_VERSION", "unknown"))
    run_id = _run_id(m, dataset, params, impl_version)
    started, t0 = _now(), time.monotonic()
    base = dict(run_id=run_id, model_id=m.model_id, model_version=m.version,
                impl_version=impl_version, dataset_hash=dataset.dataset_hash,
                params_hash=_params_hash(params), params=params,
                started_at=started, budget=dict(m.budget),
                environment=f"{os.name}", rules_version=RULES_VERSION)
    if bad:
        return ModelRun(**base, status=RunStatus.FAILED,
                        reason=f"参数不在允许集合内：{bad}（允许 {sorted(m.allowed_params)}）",
                        ended_at=_now())
    try:
        payload = compute(dataset, params)
    except MissingInput as exc:
        return ModelRun(**base, status=RunStatus.MISSING_INPUT, reason=str(exc),
                        ended_at=_now(), budget_used=_used(t0, m))
    except NotApplicable as exc:
        return ModelRun(**base, status=RunStatus.NOT_APPLICABLE, reason=str(exc),
                        ended_at=_now(), budget_used=_used(t0, m))
    except NotComputable as exc:
        return ModelRun(**base, status=RunStatus.NOT_COMPUTABLE, reason=str(exc),
                        ended_at=_now(), budget_used=_used(t0, m))
    except Exception as exc:                            # noqa: BLE001 - 运行失败如实记
        return ModelRun(**base, status=RunStatus.FAILED,
                        reason=f"{type(exc).__name__}: {str(exc)[:160]}",
                        ended_at=_now(), budget_used=_used(t0, m))

    # 参数随载荷一起交给**独立验证**：金样要用**同一组假设**复算（K3 实机反例：
    # 验证层读 `payload["params"]`，而算子返回的载荷里没有它 → 金样恒用默认假设，
    # 用户在页面上改假设后 compute 用新值、gold 用旧值 → 一律判 validation_failed）。
    payload = dict(payload)
    payload["params"] = dict(params or {})
    verification = validate_output(m, dataset, payload)
    outs: list[ValidatedOutput] = []
    for i, o in enumerate(payload.get("outputs") or []):
        outs.append(ValidatedOutput(
            output_id=f"{run_id[:12]}-{i:02d}-{o.get('metric', '')}",
            run_id=run_id, metric=str(o.get("metric") or ""),
            label=str(o.get("label") or o.get("metric") or ""),
            value=float(o.get("value") or 0.0), unit=str(o.get("unit") or ""),
            entity=str((payload.get("diagnostics") or {}).get("entity") or ""),
            entity_id=str((payload.get("diagnostics") or {}).get("entity_id") or ""),
            input_periods=tuple(payload.get("periods") or ()),
            output_period=str(o.get("output_period") or ""),
            caliber=str((payload.get("diagnostics") or {}).get("caliber") or ""),
            currency=str((payload.get("diagnostics") or {}).get("currency") or ""),
            components=tuple(o.get("components") or ()),
            residual=(None if o.get("residual") is None else float(o["residual"])),
            formula=str(payload.get("formula") or ""),
            inputs=tuple(payload.get("inputs") or ()),
            assumptions=tuple(payload.get("assumptions") or ()),
            limits=tuple(payload.get("limits") or m.limits),
            diagnostics=dict(payload.get("diagnostics") or {}),
        ))
    status = RunStatus.VALIDATED if verification["ok"] else RunStatus.VALIDATION_FAILED
    return ModelRun(**base, status=status,
                    reason="" if verification["ok"]
                    else f"独立验证未通过：{verification['failed']}",
                    outputs=tuple(outs), validation=verification,
                    ended_at=_now(), budget_used=_used(t0, m))


def _used(t0: float, m) -> dict:
    return {"steps": 1, "seconds": round(time.monotonic() - t0, 4),
            "budget_seconds": float((m.budget or {}).get("seconds") or 0)}


def raise_for_status(run: ModelRun) -> None:
    """把运行状态翻成调用方能处理的异常（缺数据/不适用/不可计算各自可分）。"""
    if run.status == RunStatus.VALIDATED:
        return
    msg = f"{run.model_id} {run.status}：{run.reason}"
    if run.status == RunStatus.MISSING_INPUT:
        raise MissingInput(msg)
    if run.status == RunStatus.NOT_APPLICABLE:
        raise NotApplicable(msg)
    if run.status == RunStatus.NOT_COMPUTABLE:
        raise NotComputable(msg)
    raise RuntimeError(msg)


def revalidate(run: ModelRun, dataset) -> tuple[str, str]:
    """旧结果对**当前**数据集与**当前验证规则**是否还有效 → `(state, 说明)`。

    过期只标"旧结果不能再当当前结果"，**不删旧版本**（保留可追溯）。两种过期：
    - `expired`：数据集已变；
    - `rules_changed`：数据集没变，但独立验证的**规则集版本**变了——旧 run 不能冒充
      "按新规则已验证"（L0-a，2026-09-30 复核 F5）。
    """
    if run.expired(dataset):
        return "expired", (f"数据集已变（run 绑定 {run.dataset_hash[:12]}，"
                           f"当前 {dataset.dataset_hash[:12]}）：旧结果作废，需重算")
    _rv = str(getattr(run, "rules_version", "") or "")
    if not _rv:
        # R0-b（2026-09-30 下午复核）：**空规则版本不是"仍然有效"**。旧运行没记验证规则
        # 版本时，无法证明它按当前规则验证过——只能当历史读数读，不得冒充当前已验证。
        return "unknown_rules", ("该运行未记录验证规则版本（历史记录）：可作历史读数，"
                                 "但不得当作按当前规则已验证，需按当前输入与规则重算")
    if _rv != RULES_VERSION:
        return "rules_changed", (f"验证规则集已更新（run 为 {_rv}，当前 {RULES_VERSION}）："
                                 "旧结果保留可追溯，但不得当作按新规则已验证，需重算")
    return "ok", "数据集未变，结果仍有效"


def ratio_run(label: str, num_metric: str, den_metric: str, dataset, *, period: str = ""):
    """同年比率的受控入口（复用既有比率口径；零/负分母 → `not_computable`）。"""
    from .operators import profit_bridge as _pb
    periods = [p for p in (dataset.manifest.periods or ()) if p]
    p = str(period or (periods[-1] if periods else ""))
    run_id = _hash({"ratio": label, "dataset": dataset.dataset_hash, "period": p})[:24]
    base = dict(run_id=run_id, model_id=f"ratio:{label}", model_version="1.0.0",
                impl_version="ratio/1.0.0", dataset_hash=dataset.dataset_hash,
                params_hash=_params_hash({"period": p}), params={"period": p},
                started_at=_now(), environment=os.name, rules_version=RULES_VERSION)
    try:
        got = _pb.compute_ratio(dataset, num_metric, den_metric, p, label=label)
    except MissingInput as exc:
        return ModelRun(**base, status=RunStatus.MISSING_INPUT, reason=str(exc), ended_at=_now())
    except NotApplicable as exc:
        return ModelRun(**base, status=RunStatus.NOT_APPLICABLE, reason=str(exc), ended_at=_now())
    except NotComputable as exc:
        return ModelRun(**base, status=RunStatus.NOT_COMPUTABLE, reason=str(exc), ended_at=_now())
    out = ValidatedOutput(output_id=f"{run_id[:12]}-00-{label}", run_id=run_id,
                          metric=label, label=label, value=float(got["value"]), unit="%",
                          output_period=p, formula=str(got["formula"]),
                          inputs=tuple(got["inputs"]),
                          limits=("比率是观察口径，不是现金流量表调节的闭合恒等式",))
    return ModelRun(**base, status=RunStatus.VALIDATED, outputs=(out,), ended_at=_now(),
                    validation={"ok": True, "checks": {}, "failed": []})


def known_ratios() -> tuple[tuple[str, str, str, str], ...]:
    return ratio_specs()
