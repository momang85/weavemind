# -*- coding: utf-8 -*-
"""冻结数据集：把"已经取得的事实"固化成 `AnalysisDataset`（纯函数，不发请求、不调模型）。

复用面（不重造）：输入可以是现役 `facts.Fact` 对象、`working_paper.json` 的 `rows`，
或任何带同名属性的对象——本模块只读属性，不反向依赖编排器/HTTP 层。

冻结时**不问"哪个数更好"**：同一 (指标, 期间, 口径) 出现互不相容的值就记 `conflicting`
并进 `manifest.conflicts`，不静默取最大/平均/第一条；缺的指标记 `missing` 进缺口清单。
"""
from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

from .contracts import (
    SCHEMA_VERSION, AnalysisDataset, DatasetManifest, Observation, State, _hash,
    _money_scale,
)

# 关键元数据：缺了它这条观察只能算"未知"，不能参与认证与计算
_REQUIRED_META = ("entity_id", "currency", "caliber", "unit")


def _attr(obj, name, default=""):
    if isinstance(obj, dict):
        v = obj.get(name, default)
    else:
        v = getattr(obj, name, default)
    return default if v is None else v


def _decimal_or_none(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal, str)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    return None


def _observation_of(row: dict, *, as_of: str, restatement: str) -> Observation:
    """一行原始记录 → 观察（状态由**事实本身**决定，不靠调用方声明）。"""
    metric = str(_attr(row, "metric") or "")
    period = str(_attr(row, "period") or "")
    value = _attr(row, "value", None)
    num = _decimal_or_none(value)
    state, note = State.OK, ""
    if num is None:
        state, note = State.INVALID, "值不是数"
    elif num == 0:
        state, note = State.ZERO, "真实零（有值）"
    missing_meta = [k for k in _REQUIRED_META if not str(_attr(row, k) or "")]
    if state in (State.OK, State.ZERO) and missing_meta:
        state, note = State.UNKNOWN, f"关键元数据未知：{'、'.join(missing_meta)}"
    if not period or period == "unknown":
        state, note = (State.INVALID if state == State.OK else state), (note or "期间未知")
    return Observation(
        fact_id=str(_attr(row, "fact_id") or ""),
        metric=metric,
        period=period,
        value=(float(num) if num is not None else None),
        state=state,
        entity=str(_attr(row, "entity") or ""),
        entity_id=str(_attr(row, "entity_id") or ""),
        market=str(_attr(row, "market") or ""),
        metric_label=str(_attr(row, "metric_label") or ""),
        dimension=str(_attr(row, "dimension") or ""),
        currency=str(_attr(row, "currency") or ""),
        unit=str(_attr(row, "unit") or ""),
        caliber=str(_attr(row, "caliber") or ""),
        period_type=str(_attr(row, "period_type") or ""),
        period_start=str(_attr(row, "period_start") or ""),
        period_end=str(_attr(row, "period_end") or ""),
        as_of=str(_attr(row, "as_of") or as_of),
        restatement=str(_attr(row, "restatement") or restatement),
        source_url=str(_attr(row, "source_url") or ""),
        source_hash=str(_attr(row, "source_hash") or ""),
        derived_from=tuple(str(x) for x in (_attr(row, "derived_from", ()) or ())
                           if str(x).strip()),
        formula_version=str(_attr(row, "formula_version") or ""),
        verify_state=str(_attr(row, "verify_state") or ""),
        note=note,
    )


def _resolve_conflicts(observations: list[Observation]) -> tuple[list[Observation], list[str]]:
    """同一 (指标, 期间, 口径) 出现不相容值 → 全部标 `conflicting` 并列出（不择一）。"""
    buckets: dict[tuple, list[Observation]] = {}
    for o in observations:
        if o.state in (State.OK, State.ZERO):
            buckets.setdefault((o.metric, o.period, o.caliber), []).append(o)
    conflicts: list[str] = []
    bad: set[str] = set()
    for key, group in buckets.items():
        hashes = {o.observation_hash for o in group}
        if len(hashes) > 1:
            detail = ("；".join(f"{o.fact_id}={o.value}{o.unit}（{o.currency}/{o.caliber}）"
                                for o in group))
            conflicts.append(f"{key[0]} {key[1]} 口径 {key[2] or '未标'}：{detail}")
            bad.update(o.fact_id for o in group)
    out = []
    for o in observations:
        if o.fact_id in bad:
            o = Observation(**{**o.__dict__, "state": State.CONFLICTING,
                               "note": "同一(指标,期间,口径)多个不相容值：不择一，不参与计算"})
        out.append(o)
    return out, conflicts


def _dataset_hash(observations: list[Observation], *, entity_id: str, as_of: str,
                  source_label: str, periods: tuple[str, ...]) -> str:
    """数据集 hash：**逐条观察身份** + 主体/截止/期间/来源标签。

    刻意不含 `available_models`/缺口这类派生信息：它们是数据的函数，不是数据本身；
    把它们算进 hash 会让"换了注册表"看起来像"数据变了"。
    """
    return _hash({
        "schema": SCHEMA_VERSION,
        "entity_id": entity_id, "as_of": as_of, "periods": list(periods),
        "source_label": source_label,
        "observations": sorted(o.observation_hash for o in observations),
    })


def _year_of(period: str) -> int | None:
    m = re.search(r"(19|20)\d{2}", str(period or ""))
    return int(m.group(0)) if m else None


# 年度累计口径：只有"年报/未标类型"且标签以年结尾才算年度期间（季报/中报是累计口径，
# 不进年度模型的期间清单——它们仍在观察集合里，只是不被年度桥接当"两期"用）。
_ANNUAL_TYPES = ("", "10-K", "年报", "annual")


def _is_annual(obs: Observation) -> bool:
    if str(obs.period_type or "") not in _ANNUAL_TYPES:
        return False
    p = str(obs.period or "")
    return p.endswith("年") and p[:-1].isdigit()


def _annual_periods(observations) -> tuple[str, ...]:
    years = sorted({_year_of(o.period) for o in observations
                    if o.usable and _is_annual(o) and _year_of(o.period) is not None})
    out: list[str] = []
    for y in years:
        for o in observations:
            if _year_of(o.period) == y and _is_annual(o) and o.period not in out:
                out.append(o.period)
    return tuple(out)


def freeze(rows, *, entity: str = "", entity_id: str = "", market: str = "",
           periods=(), as_of: str = "", source_label: str = "",
           restrictions: str = "", restatement: str = "",
           required_metrics=(), available_models=()) -> AnalysisDataset:
    """把原始记录冻结成数据集（唯一入口；`freeze_from_*` 只是它的取数包装）。"""
    observations = [_observation_of(r, as_of=as_of, restatement=restatement)
                    for r in (rows or [])]
    observations, conflicts = _resolve_conflicts(observations)
    observations.sort(key=lambda o: (o.metric, o.period, o.caliber, o.fact_id))
    # 期间清单以**数据里真实存在的年度期间**为准（请求里的年份只作筛选参考）：
    # 观察的 period 是 "2024年"，而请求给的是 2024——直接字符串化会让两者对不上，
    # 于是"输入齐备"被误判成缺输入（本轮实测踩到）。
    _annual = _annual_periods(observations)
    _wanted = [int(str(p).strip()) for p in (periods or ())
               if str(p).strip().isdigit()]
    if _wanted:
        _picked = tuple(p for p in _annual if _year_of(p) in set(_wanted))
        _periods = _picked or _annual
    else:
        _periods = _annual
    entity_id = str(entity_id or next((o.entity_id for o in observations if o.entity_id), ""))
    entity = str(entity or next((o.entity for o in observations if o.entity), ""))
    market = str(market or next((o.market for o in observations if o.market), ""))
    coverage: dict = {}
    for o in observations:
        slot = coverage.setdefault(o.metric, {"total": 0, "usable": 0, "states": {},
                                              "periods": []})
        slot["total"] += 1
        slot["usable"] += 1 if o.usable else 0
        slot["states"][o.state] = slot["states"].get(o.state, 0) + 1
        if o.period not in slot["periods"]:
            slot["periods"].append(o.period)
    gaps: list[str] = [f"缺失指标：{m}" for m in (required_metrics or ())
                       if not any(o.metric == m and o.usable for o in observations)]
    for o in observations:
        if o.state in (State.MISSING, State.UNKNOWN, State.INVALID):
            gaps.append(f"{o.metric} {o.period}：{o.state}"
                        + (f"（{o.note}）" if o.note else ""))
    _non_annual = sorted({o.period for o in observations
                          if o.usable and not _is_annual(o) and o.period})
    if _non_annual:
        gaps.append("非年度期间（不参与年度模型，仍保留在观察集合里）："
                    + "、".join(_non_annual))
    if len(_periods) < 2:
        gaps.append(f"年度期间不足两期（{_periods or '无'}）：两期桥接不适用")
    manifest = DatasetManifest(
        dataset_hash=_dataset_hash(observations, entity_id=entity_id, as_of=as_of,
                                   source_label=source_label, periods=_periods),
        source_label=source_label, as_of=as_of, entity=entity, entity_id=entity_id,
        market=market,
        caliber=str(next((o.caliber for o in observations if o.usable), "")),
        periods=_periods, observations=len(observations),
        usable=sum(1 for o in observations if o.usable),
        field_coverage=coverage, gaps=tuple(gaps), conflicts=tuple(conflicts),
        available_models=tuple(available_models or ()), restrictions=restrictions,
    )
    index = {}
    for o in observations:
        if o.usable:
            index.setdefault((o.metric, o.period, o.caliber), o)
    return AnalysisDataset(manifest=manifest, observations=tuple(observations), index=index)


def freeze_from_facts(facts, *, request=None, periods=(), entity="", entity_id="",
                      market="", as_of="", required_metrics=(), source_label="",
                      restrictions="", available_models=(), restatement="") -> AnalysisDataset:
    """从现役 `facts.Fact` 列表冻结（编排器/Worker 的接入点）。

    显式给的字段**优先于** `request`：调用方常常已经知道主体/期间（例如重试批次），
    不该因为 request 是 None 就退化成"主体未知"。
    """
    return freeze(
        list(facts or []),
        entity=str(entity or getattr(request, "company", "") or ""),
        entity_id=str(entity_id or getattr(request, "company_id", "") or ""),
        market=str(market or getattr(request, "market", "") or ""),
        periods=tuple(periods or getattr(request, "periods", ()) or ()),
        as_of=str(as_of or getattr(request, "as_of", "") or ""),
        source_label=str(source_label or "facts"),
        restrictions=str(restrictions or ""),
        restatement=str(restatement or ""),
        required_metrics=tuple(required_metrics or ()),
        available_models=tuple(available_models or ()),
    )


def freeze_from_working_paper(obj, *, required_metrics=(), source_label="",
                              restrictions="", available_models=(),
                              restatement="") -> AnalysisDataset:
    """从 `working_paper.json`（底稿落盘）冻结：底稿是"当时算过的数"的快照。"""
    if isinstance(obj, (str, bytes)):
        obj = json.loads(obj)
    req = (obj or {}).get("request") or {}
    rows = list((obj or {}).get("rows") or [])
    # 派生行也进来（同比/变化/比率）：它们自带公式与输入 fact_id，可作为**交叉核对**输入，
    # 但状态由自身元数据决定——缺主体/期间/口径的派生行同样只会是 unknown。
    rows += [d for d in ((obj or {}).get("derived") or []) if isinstance(d, dict)]
    label = str(source_label or "")
    if not label:
        label = "working_paper.json:" + hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, sort_keys=True,
                       default=str).encode("utf-8")).hexdigest()[:12]
    return freeze(
        rows,
        entity=str(req.get("company") or ""), entity_id=str(req.get("company_id") or ""),
        market=str(req.get("market") or ""), periods=tuple(req.get("periods") or ()),
        as_of=str(req.get("as_of") or ""), source_label=label,
        restrictions=str(restrictions or ""), restatement=str(restatement or ""),
        required_metrics=tuple(required_metrics or ()),
        available_models=tuple(available_models or ()),
    )


def unit_scale_of(unit: str) -> float:
    """对外暴露的量纲口径（与契约里同一实现，避免两套换算规则）。"""
    return _money_scale(unit)
