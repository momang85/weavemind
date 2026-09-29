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
from .validation import validate_output


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
    """问题 → 计划：能用哪些模型、为什么拒绝其它模型（每一拒绝都带原因）。"""
    avail = available_for(dataset)
    adopted: list[PlanItem] = []
    rejected: list[dict] = []
    order = [m for m in (prefer or ()) if m in avail] + \
            [m for m in avail if m not in (prefer or ())]
    for mid in order:
        m = spec(mid)
        need = [f"{i.metric}@{i.period_offset}" for i in m.inputs]
        missing = [i.metric for i in m.inputs
                   if dataset.get(i.metric, dataset.period_at(i.period_offset)) is None]
        if missing:
            rejected.append({"model_id": mid, "reason": "缺输入",
                             "missing": sorted(set(missing)), "needs": need})
            continue
        adopted.append(PlanItem(model_id=mid, question=m.question,
                                reason=f"输入齐备（{', '.join(need)}）"))
    # 其余**已注册**模型：逐个说明为什么没被采用（缺哪些指标 / 期间不够 / 不适用）——
    # 不能只写一句"输入不齐备"，那对"该补什么材料"没有帮助。
    _periods = [p for p in (dataset.manifest.periods or ()) if p]
    for m in specs():
        if any(a.model_id == m.model_id for a in adopted) \
                or any(r["model_id"] == m.model_id for r in rejected):
            continue
        missing = sorted({i.metric for i in m.inputs
                          if dataset.get(i.metric,
                                         dataset.period_at(i.period_offset)) is None})
        if missing:
            reason = "缺输入"
        elif len(_periods) < 2:
            reason = "期间不足两期（两期桥接需要一个以上的年度期间）"
        else:
            reason = "当前数据形态不适用"
        rejected.append({"model_id": m.model_id, "reason": reason, "missing": missing,
                         "needs": [f"{i.metric}@{i.period_offset}" for i in m.inputs]})
    return AnalysisPlan(question=str(question or ""), dataset_hash=dataset.dataset_hash,
                        adopted=tuple(adopted), rejected=tuple(rejected))


def run(model_id: str, dataset, *, params: dict | None = None,
        question: str = "") -> ModelRun:
    """跑一个注册模型 → `ModelRun`（含独立验证结论；**不写任何库、不发网络请求**）。"""
    from .models import profit_bridge as _pb

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
                environment=f"{os.name}")
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
    """旧结果对**当前**数据集是否还有效 → `(state, 说明)`；state ∈ ok/expired。

    过期只标"旧结果不能再当当前结果"，**不删旧版本**（保留可追溯）。
    """
    if run.expired(dataset):
        return "expired", (f"数据集已变（run 绑定 {run.dataset_hash[:12]}，"
                           f"当前 {dataset.dataset_hash[:12]}）：旧结果作废，需重算")
    return "ok", "数据集未变，结果仍有效"


def ratio_run(label: str, num_metric: str, den_metric: str, dataset, *, period: str = ""):
    """同年比率的受控入口（复用既有比率口径；零/负分母 → `not_computable`）。"""
    from .models import profit_bridge as _pb
    periods = [p for p in (dataset.manifest.periods or ()) if p]
    p = str(period or (periods[-1] if periods else ""))
    run_id = _hash({"ratio": label, "dataset": dataset.dataset_hash, "period": p})[:24]
    base = dict(run_id=run_id, model_id=f"ratio:{label}", model_version="1.0.0",
                impl_version="ratio/1.0.0", dataset_hash=dataset.dataset_hash,
                params_hash=_params_hash({"period": p}), params={"period": p},
                started_at=_now(), environment=os.name)
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
