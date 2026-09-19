# -*- coding: utf-8 -*-
"""S1-3：可重算底稿（working paper）与退出关卡的判定。

底稿是"可复核"的载体：报告里的每个关键数字都能在这里找到行、看到单位与主体、
顺着来源 URL/快照 hash/结构化字段位置回到原始条目，并**自己把同比算一遍**。

	exit 关卡（架构指令）在这里逐条落地：
- 一公司两年度 × 三核心指标（六个组合）齐备才谈"目标达成"；缺的**如实记缺口**；
- 同比可重算：派生行带公式与输入 `fact_id`，数值由本模块算（不采信报告里的数）；
- **错主体/错期/错币种/累计与单季混用不得被验证通过**；
- 缺证据（未核实/来源缺失）的报告不能算目标完成；
- **正确输入不能因无关的同值记录而失败**（判定只看必需组合自身的行）。
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field

from facts import (CALIBERS, CORE_METRICS, UNKNOWN, VERIFY_VERIFIED,
                   Fact, ResearchRequest, amount_scale, check_subject,
                   derived_fact, market_of_code, metric_label)

# 判定类别（给缺口表/报告用，字符串稳定，便于测试与前端展示）
PROBLEM_MISSING = "missing_required"
PROBLEM_UNVERIFIED = "unverified_required"
PROBLEM_SUBJECT = "subject_mismatch"
PROBLEM_PERIOD = "period_mismatch"
PROBLEM_CURRENCY = "currency_mismatch"
PROBLEM_CUMULATIVE = "cumulative_mixed"
PROBLEM_CALIBER = "caliber_mismatch"        # 口径未知或与请求不符
PROBLEM_UNRELATED = "unrelated_subject"     # 载荷含其他公司的记录
PROBLEM_CONFLICT = "conflicting_candidates"  # 同一组合多条互相冲突的候选
PROBLEM_NOT_COMPUTABLE = "not_computable"   # 明确"不可算"（如分母为 0）
PROBLEM_DOC_SCOPE = "document_scope_unbound"  # 文档级主体作用域缺失/张冠李戴
PROBLEM_AS_OF = "as_of_unverifiable"        # 截至日无法成立（未披露/日期未知）


@dataclass
class Problem:
    kind: str
    detail: str
    metric: str = ""
    period: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail,
                "metric": self.metric, "period": self.period}


@dataclass
class WorkingPaper:
    request: ResearchRequest
    rows: list[dict] = field(default_factory=list)
    derived: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    completeness: dict = field(default_factory=dict)
    selection: dict = field(default_factory=dict)
    audit: list[dict] = field(default_factory=list)   # 只读提示：不参与 ok 判定

    @property
    def ok(self) -> bool:
        """目标达成的底稿侧判据：无缺口、无实质问题（审计提示不算）。"""
        return not self.gaps and not self.problems

    def as_dict(self) -> dict:
        return {
            "request": self.request.as_dict(),
            "rows": self.rows, "derived": self.derived, "gaps": self.gaps,
            "problems": [p.as_dict() for p in self.problems],
            "completeness": self.completeness, "selection": self.selection,
            "audit": self.audit, "ok": self.ok,
        }


@dataclass
class Conflict:
    """同一（指标, 期间）下互相冲突的候选：**全部列出**，不靠列表先后决定谁生效。

    只列 `fact_id` 不够：`make_fact_id` 与**值无关**（同一实体的同一指标/期间/口径
    必然是同一个 id），所以冲突候选的 id 会相同——必须连**值和来源**一起列出来，
    人才能看出"到底哪两个数在打架"。
    """

    metric: str
    period: str
    candidates: list[dict] = field(default_factory=list)
    detail: str = ""

    @property
    def fact_ids(self) -> list[str]:
        return [str(c.get("fact_id") or "") for c in self.candidates]

    def as_dict(self) -> dict:
        return {"metric": self.metric, "period": self.period,
                "candidates": [dict(c) for c in self.candidates],
                "fact_ids": self.fact_ids, "detail": self.detail}


@dataclass
class Selection:
    """按完整身份选出的**一份**事实集合：校验、完整度、计算共用它。

    四种产出分开记（A′2）：
    - `selected`：主体、口径、币种单位都**与请求兼容**的候选（同一组合只留一条，
      且该条携带全部一致来源）；
    - `pending`：主体是本公司的，但口径未知/不符等**不能用于认证**的记录——
      可以展示为待核验事实，不参与达标与派生；
    - `unrelated`：其他主体的记录（**只作审计提示**，不否决本公司结果）；
    - `conflicts`：同一组合存在互不相容的候选（值/口径悬而未决），该组合视为未满足。
    """

    selected: list[Fact] = field(default_factory=list)
    pending: list[Fact] = field(default_factory=list)
    unrelated: list[Fact] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)

    def by_combo(self) -> dict[tuple[str, str], Fact]:
        out: dict[tuple[str, str], Fact] = {}
        conflict_keys = {(c.metric, c.period) for c in self.conflicts}
        for f in self.selected:
            key = (f.metric, f.period)
            if key in conflict_keys:
                continue
            out.setdefault(key, f)
        return out

    def by_metric_year(self) -> dict[str, dict[int, Fact]]:
        """metric → {year → fact}；只收**年报/未标类型**（累计口径），季报不参与同比。"""
        out: dict[str, dict[int, Fact]] = {}
        for f in self.selected:
            year = _annual_year(f)
            if year is None:
                continue
            out.setdefault(f.metric, {})[year] = f
        return out

    def as_dict(self) -> dict:
        return {
            "selected": [f.fact_id for f in self.selected],
            "pending": [{"fact_id": f.fact_id, "caliber": str(f.caliber or UNKNOWN),
                         "reason": "口径未知或与请求不符：仅展示，不用于认证"}
                        for f in self.pending],
            "unrelated": [{"fact_id": f.fact_id, "entity": f.entity,
                           "entity_id": f.entity_id, "market": f.market}
                          for f in self.unrelated],
            "conflicts": [c.as_dict() for c in self.conflicts],
        }


def _annual_year(f: Fact) -> int | None:
    """这条事实对应的**年度**；只认年报/未标类型（季报/中报是累计口径，不进同比）。"""
    if f.period_type and f.period_type not in ("10-K", "年报", "annual", ""):
        return None
    m = str(f.period or "")
    if m.endswith("年") and m[:-1].isdigit():
        return int(m[:-1])
    return None


def _caliber_ok(request: ResearchRequest, f: Fact) -> bool:
    """这条事实的口径能否用于**认证**（未知/不符都不能）。"""
    want = str(request.caliber or UNKNOWN)
    got = str(f.caliber or UNKNOWN)
    if want in CALIBERS:
        return got == want
    return got in CALIBERS          # 请求没声明口径时，只有来源真声明的才敢用


def select_facts(facts: list[Fact], request: ResearchRequest) -> Selection:
    """按 **主体 → 口径/币种单位兼容 → 去重/冲突** 选出一份事实集合。

    本批（A′2）修两件事：
    1) **不能有顺序依赖**：去重键必须含口径与来源——同值但口径不同（合并 vs 母公司）
       此前被当成"同一条"取列表第一条，于是合并在前 ok、母公司在前不 ok；
    2) **无关记录只作审计**：别家公司的记录不参与本公司任何计算，也不否决本公司的
       正确结果（此前的 `PROBLEM_UNRELATED` 让 paper.ok 从 true 变 false）。

    策略（显式，不靠"第一条有没有来源"）：
    - 候选按 (metric, period, caliber, 值, 币种, 单位) 分组：
      只有一组 → 认证候选（合并该组全部来源，来源数≥1 记录在案）；
      多组 → 冲突（列出全部候选及其值与来源），该组合**不产生**认证结果；
    - 口径与请求不符/未知的候选进 `pending`：只展示，不参与达标与派生。
    """
    sel = Selection()
    for f in facts:
        ok, _why = check_subject(request, f.entity, f.entity_id, f.market)
        if not ok:
            sel.unrelated.append(f)
            continue
        if not _caliber_ok(request, f):
            sel.pending.append(f)
            continue
        sel.selected.append(f)

    groups: dict[tuple[str, str], list[Fact]] = {}
    for f in sel.selected:
        groups.setdefault((f.metric, f.period), []).append(f)
    keep: list[Fact] = []
    for (metric, period), items in groups.items():
        # 按"值 + 币种 + 单位"分组：口径已在上一步统一，这一层判"数据是否一致"
        buckets: dict[tuple, list[Fact]] = {}
        for i in items:
            buckets.setdefault((str(i.value), str(i.currency), str(i.unit)), []).append(i)
        if len(buckets) == 1:
            group = next(iter(buckets.values()))
            chosen = group[0]
            # 多个来源给出同一个值 = 更强的证据：来源全部记在这一条上（可审计）
            urls = sorted({str(g.source_url or "") for g in group if g.source_url})
            if len(group) > 1:
                chosen = group[0]
                chosen.source_locator = dict(chosen.source_locator or {})
                chosen.source_locator["agreeing_sources"] = [
                    {"fact_id": g.fact_id, "url": str(g.source_url or "")} for g in group]
                chosen.source_locator["agreeing_count"] = len(group)
            del urls
            keep.append(chosen)
            continue
        cands = []
        for (value, currency, unit), group in sorted(buckets.items()):
            cands.append({
                "fact_id": group[0].fact_id, "value": group[0].value,
                "currency": currency, "unit": unit,
                "period_type": str(group[0].period_type or ""),
                "caliber": str(group[0].caliber or UNKNOWN),
                "source_url": str(group[0].source_url or ""),
                "source_count": len(group),
            })
        shown = "、".join(
            f"{c['value']}{c['unit']}（{c['source_url'] or '无来源'}）" for c in cands)
        sel.conflicts.append(Conflict(
            metric=metric, period=period, candidates=cands,
            detail=(f"{metric_label(metric)} {period} 有 {len(cands)} 组互不相容的候选"
                    f"（{shown}）：不按列表先后取用，需人工确认后重跑")))
    ordered = {id(f) for f in keep}
    sel.selected = [f for f in sel.selected if id(f) in ordered]
    return sel


def _row(f: Fact) -> dict:
    d = f.as_dict()
    d["metric_label"] = f.metric_label or metric_label(f.metric)
    return d


def build_working_paper(facts: list[Fact], request: ResearchRequest) -> WorkingPaper:
    """从事实记录生成底稿：明细行 + 可重算同比 + 缺口 + 问题清单。

    **校验、完整度与计算共用同一个 `select_facts` 结果**——不再各取各的行。
    """
    selection = select_facts(facts, request)
    # 明细展示"选中 + 待核验"两类（待核验的只展示，不进达标与派生）
    paper = WorkingPaper(
        request=request,
        rows=[_row(f) for f in list(selection.selected) + list(selection.pending)],
        selection=selection.as_dict())

    # 1) 缺口：契约缺口 + 必需组合缺失
    for g in request.gaps:
        paper.gaps.append({"kind": "contract", "detail": g})
    by_combo = selection.by_combo()
    combos = [(m, f"{y}年") for y in request.periods for m, _ in CORE_METRICS]
    missing = [c for c in combos if c not in by_combo]
    paper.completeness = {
        "required": len(combos),
        "present": len(combos) - len(missing),
        "missing": [f"{metric_label(m)} {p}" for m, p in missing],
    }
    for metric, period in missing:
        paper.gaps.append({
            "kind": "fact", "metric": metric, "period": period,
            "detail": f"缺少必需事实：{metric_label(metric)} {period}（如实缺失，不推断）",
        })

    # 1a) 其他公司的记录：**只作审计提示**，不否决本公司的正确结果（A′2）
    if selection.unrelated:
        names = sorted({(f.entity or f.entity_id or "（无主体）")
                        for f in selection.unrelated})
        paper.audit.append({
            "kind": PROBLEM_UNRELATED,
            "detail": (f"载荷含 {len(selection.unrelated)} 条非本次研究主体的记录"
                       f"（{'、'.join(names)[:120]}）：已排除，不参与任何判定与计算"),
            "fact_ids": [f.fact_id for f in selection.unrelated][:50],
        })
    # 1a2) 主体是本公司但口径不能用于认证的记录：只展示（待核验）
    if selection.pending:
        paper.audit.append({
            "kind": PROBLEM_CALIBER,
            "detail": (f"{len(selection.pending)} 条记录口径未知或与请求不符："
                       "已列在明细中供核对，不用于达标与同比"),
            "fact_ids": [f.fact_id for f in selection.pending][:50],
        })

    # 1b) 冲突候选：列全部候选（不先到先得）
    for c in selection.conflicts:
        paper.problems.append(Problem(PROBLEM_CONFLICT, c.detail, c.metric, c.period))

    # 1c) 截至日与研究期间的基本冲突：报告期末还没到，就不可能"截至该日已披露"
    _as_of = str(getattr(request, "as_of", "") or "").strip()[:10]
    if _as_of and request.periods:
        _last_end = f"{max(request.periods)}-12-31"
        if _as_of < _last_end:
            paper.gaps.append({
                "kind": "contract",
                "detail": (f"资料截至日 {_as_of} 早于研究期末 {_last_end}："
                           "该期间的年报在该时点尚未披露，时点不可核实"),
            })

    # 1d) 年度要求只能由**年报**满足：只有季报/中报时不能当年度口径（累计 vs 单季不可比）
    for y in request.periods:
        for metric, _label in CORE_METRICS:
            if (metric, f"{y}年") in by_combo:
                continue
            others = [f for f in selection.selected
                      if f.metric == metric and str(f.period or "").startswith(f"{y}")
                      and f.period != f"{y}年"]
            if others:
                paper.problems.append(Problem(
                    PROBLEM_CUMULATIVE,
                    f"{metric_label(metric)} {y}年 只有 {others[0].period}"
                    "（季报/中报属累计口径）：不能当年度数字，需补年报",
                    metric, f"{y}年"))

    # 2) 问题判定（只针对**必需组合自身的认证行**，无关记录与本条目已排除）
    want_caliber = str(request.caliber or UNKNOWN)
    for metric, period in combos:
        f = by_combo.get((metric, period))
        if f is None:
            continue
        ok_subject, why_subject = check_subject(request, f.entity, f.entity_id, f.market)
        if not ok_subject:
            paper.problems.append(Problem(
                PROBLEM_SUBJECT,
                f"{metric_label(metric)} {period} 的主体无法确认属于本次研究：{why_subject}",
                metric, period))
        # 口径：能进 by_combo 的已经过兼容筛选；这里再确认一遍请求口径已知时不缺声明
        got_caliber = str(f.caliber or UNKNOWN)
        if want_caliber in CALIBERS and got_caliber != want_caliber:
            paper.problems.append(Problem(
                PROBLEM_CALIBER,
                f"{metric_label(metric)} {period} 的口径是「{got_caliber}」，"
                f"与请求的「{want_caliber}」不符：未声明或不同的口径不得算已核验",
                metric, period))
        if want_caliber not in CALIBERS and got_caliber not in CALIBERS:
            paper.problems.append(Problem(
                PROBLEM_CALIBER,
                f"{metric_label(metric)} {period} 未声明报表口径（合并/母公司）："
                "口径不明不得算已核验",
                metric, period))
        if f.currency in ("", UNKNOWN):
            paper.problems.append(Problem(
                PROBLEM_CURRENCY,
                f"{metric_label(metric)} {period} 未声明币种：口径不明，不得算已核验",
                metric, period))
        if f.unit in ("", UNKNOWN):
            paper.problems.append(Problem(
                PROBLEM_CURRENCY,
                f"{metric_label(metric)} {period} 未声明单位：不得算已核验",
                metric, period))
        if not f.source_url and f.verify_state != VERIFY_VERIFIED:
            # 缺来源位置：**无论核实状态**都不得算已核验（verify_state=unknown 也不例外）。
            # 注意结构化定位符（payload 里的字段名）不是"来源位置"——它说明数据从哪一格来，
            # 不说明来自哪份可回溯的资料，所以这里只认 source_url。
            paper.problems.append(Problem(
                PROBLEM_UNVERIFIED,
                f"{metric_label(metric)} {period} 没有来源位置：不得算已核验",
                metric, period))
        # 截至日证据（A′4）：报告期末 ≠ 抓取时间 ≠ 披露时间。只有能证明"截至该日
        # 这份数据已可用"时才算满足；披露日缺失 → 时点未核实，不得宣称目标达成。
        if _as_of:
            disclosed = str(f.disclosed_at or "")[:10]
            if not disclosed:
                paper.problems.append(Problem(
                    PROBLEM_AS_OF,
                    f"{metric_label(metric)} {period} 没有披露/可用日期："
                    f"无法证明截至 {_as_of} 该数据已可用（时点未核实）",
                    metric, period))
            elif disclosed > _as_of:
                paper.problems.append(Problem(
                    PROBLEM_AS_OF,
                    f"{metric_label(metric)} {period} 的披露日是 {disclosed}，"
                    f"晚于请求的资料截至日 {_as_of}：该时点尚未披露",
                    metric, period))

    # 3) 同指标同期多行：币种不一致 / 累计与单季混用
    seen: dict[tuple[str, str], list[Fact]] = {}
    for f in selection.selected:
        seen.setdefault((f.metric, f.period), []).append(f)
    for (metric, period), items in seen.items():
        cur = {str(i.currency or UNKNOWN) for i in items}
        if len(cur) > 1:
            paper.problems.append(Problem(
                PROBLEM_CURRENCY,
                f"{metric_label(metric)} {period} 出现多种币种 {sorted(cur)}："
                "需换算并记录公式，否则不得算已核验",
                metric, period))
        types = {str(i.period_type or "") for i in items if i.period_type}
        if len(types) > 1:
            paper.problems.append(Problem(
                PROBLEM_CUMULATIVE,
                f"{metric_label(metric)} {period} 混用多种报告期类型 {sorted(types)}："
                "累计与单季不可直接比较",
                metric, period))

    # 4) 同比：可重算（公式 + 输入 fact_id），只对**相邻年度**的同年报行算
    years = sorted(request.periods)
    series = selection.by_metric_year()
    for metric, _label in CORE_METRICS:
        per_year = series.get(metric) or {}
        for prev, cur in zip(years, years[1:]):
            f0, f1 = per_year.get(prev), per_year.get(cur)
            if not f0 or not f1:
                continue
            if cur != prev + 1:
                paper.problems.append(Problem(
                    PROBLEM_PERIOD,
                    f"{metric_label(metric)} {prev}→{cur} 跨年不是相邻年度："
                    "不得标成同比（需给相邻年度或明确说明口径）",
                    metric, f"{prev}→{cur}"))
                continue
            if not isinstance(f0.value, (int, float)) or not isinstance(f1.value, (int, float)):
                continue
            if float(f0.value) == 0.0:
                paper.problems.append(Problem(
                    PROBLEM_NOT_COMPUTABLE,
                    f"{metric_label(metric)} {prev}→{cur} 的基期为 0：同比不可算"
                    "（不编造、不跳过）",
                    metric, f"{cur}年同比"))
                continue
            if float(f0.value) < 0.0:
                # 负基期：同比（比例变化）没有可比含义，如实记为不可算而不是硬算一个
                # 会误导的百分比（亏损转正/亏损扩大要**分别描述**，不能塞进"同比"）
                paper.problems.append(Problem(
                    PROBLEM_NOT_COMPUTABLE,
                    f"{metric_label(metric)} {prev}→{cur} 的基期为负（{f0.value}）："
                    "同比不具可比含义（应分别描述亏损/转正与绝对额变化），不计算",
                    metric, f"{cur}年同比"))
                continue
            if str(f0.currency) != str(f1.currency) or str(f0.unit) != str(f1.unit):
                paper.problems.append(Problem(
                    PROBLEM_CURRENCY,
                    f"{metric_label(metric)} {prev}→{cur} 的两期币种/单位不一致"
                    f"（{f0.currency}/{f0.unit} vs {f1.currency}/{f1.unit}）：不可直接算同比",
                    metric, f"{cur}年同比"))
                continue
            rate = (float(f1.value) - float(f0.value)) / float(f0.value) * 100.0
            formula = (f"({f1.value} - {f0.value}) / {f0.value} * 100"
                       f"，输入 {f0.fact_id} / {f1.fact_id}")
            # 同比是**百分比**：显式给单位，不继承输入的"亿元/亿美元"
            d = derived_fact([f0, f1], f"{metric}_yoy", formula=formula,
                             value=round(rate, 2), period=f"{cur}年同比",
                             inputs=[f0, f1], unit="%", unit_source="derived")
            paper.derived.append(_row(d))

    # 5) 同年比率（报告要有"经济含义"，不能只有绝对数）：净利率、经营现金流对净利润
    #    的覆盖、资产负债率、研发投入强度——全部由**已选事实**算出，带公式与输入
    #    fact_id，可复核。两条纪律（与同比区分开）：
    #    - 缺输入**不生成、不记问题**：契约可能没要求那个指标，不能因此把交付判成
    #      不达标（同比是契约要求的一部分，比率是增强项）；
    #    - 分母为 0 / 输入口径不一致只记**审计提示**（`audit` 不参与 ok 判定）：
    #      如实说"不可算"，不编造，也不因一个比率算不出来就把整份交付打回。
    for _metric, _num_key, _den_key, _desc in _RATIO_SPECS:
        _num_series = series.get(_num_key) or {}
        _den_series = series.get(_den_key) or {}
        for year in years:
            num, den = _num_series.get(year), _den_series.get(year)
            if not num or not den:
                continue
            if (not isinstance(num.value, (int, float))
                    or not isinstance(den.value, (int, float))):
                continue
            if float(den.value) == 0.0:
                paper.audit.append({
                    "kind": PROBLEM_NOT_COMPUTABLE,
                    "detail": (f"{metric_label(_metric)} {year}年 的分母为 0（{_desc}）："
                               "不可算，不编造"),
                    "fact_ids": [num.fact_id, den.fact_id],
                })
                continue
            if (str(num.caliber) != str(den.caliber)
                    or str(num.currency) != str(den.currency)):
                paper.audit.append({
                    "kind": PROBLEM_CALIBER,
                    "detail": (f"{metric_label(_metric)} {year}年 的两个输入口径/币种不一致"
                               f"（{num.caliber}/{num.currency} vs "
                               f"{den.caliber}/{den.currency}）：不计算"),
                    "fact_ids": [num.fact_id, den.fact_id],
                })
                continue
            # **单位归一**（架构复核 P1）：先换算到同一量级再相除。此前只比币种/口径，
            # "净利润 150 亿元 ÷ 营收 1500000 万元" 会静默放大 1e4 倍且毫无提示；
            # 换算不出来（百分比/未知单位）就不生成这个比率——不猜、不硬算。
            _s_num, _s_den = amount_scale(num.unit), amount_scale(den.unit)
            if _s_num <= 0 or _s_den <= 0:
                paper.audit.append({
                    "kind": PROBLEM_NOT_COMPUTABLE,
                    "detail": (f"{metric_label(_metric)} {year}年 的输入单位不可换算为金额"
                               f"（{num.unit} / {den.unit}）：不生成该比率"),
                    "fact_ids": [num.fact_id, den.fact_id],
                })
                continue
            factor = _s_num / _s_den
            ratio = float(num.value) * factor / float(den.value) * 100.0
            formula = f"{num.value} / {den.value} * 100，输入 {num.fact_id} / {den.fact_id}"
            if abs(factor - 1.0) > 1e-12:
                # 换算过程要留在公式里，读者才能复核（不能只给一个变了量级的结果）
                formula = (f"{num.value}{num.unit} 换算为 {float(num.value) * factor:g}"
                           f"{den.unit}，再 / {den.value} * 100"
                           f"（输入 {num.fact_id} / {den.fact_id}）")
            d = derived_fact([num, den], _metric, formula=formula,
                             value=round(ratio, 2), period=f"{year}年",
                             inputs=[num, den], unit="%", unit_source="derived")
            paper.derived.append(_row(d))
    return paper


# 同年比率：(派生指标, 分子, 分母, 口径说明)。分母为 0 / 单位不可换算只记审计提示。
# 说明里必须写清**归属口径**：`net_profit` 是归母净利润（合并报表里归属母公司的部分），
# 而经营现金流是合并现金流量表的数——两者归属层不同，读者不能当成同口径比值。
_RATIO_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("net_margin", "net_profit", "revenue", "归母净利润 / 营业收入 × 100"),
    ("cashflow_coverage", "operating_cashflow", "net_profit",
     "经营活动现金流净额（合并口径） / 归母净利润 × 100"
     "（>100% 仅表示当期经营现金流高于归母净利润；分子或分母为负时该倍数不表示"
     "'利润有现金支撑'）"),
    ("debt_ratio", "total_liabilities", "total_assets", "总负债 / 总资产 × 100"),
    ("rd_intensity", "rd_expense", "revenue", "研发投入 / 营业收入 × 100"),
)


_NUM_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _number_forms(value) -> set[str]:
    """一个数值可能出现的写法（去千分位后的规范串 + 常见保留位数）。"""
    if value is None or isinstance(value, bool):
        return set()
    if isinstance(value, (int, float)):
        out = set()
        for d in (0, 1, 2, 3, 4):
            s = f"{float(value):.{d}f}".rstrip("0").rstrip(".")
            if s:
                out.add(s)
        out.add(str(value))
        return {o for o in out if o}
    return {str(value)}


# 标题/表头的作用域分类（A′3）：只覆盖首发报告模板，不做通用语言理解。
_NEUTRAL_SECTION_WORDS = (
    "指标", "财务", "数据", "分析", "风险", "结论", "摘要", "要点", "同比", "概览",
    "时效", "来源", "免责", "附录", "表格", "图表", "说明", "口径", "期间", "年度",
    "业绩", "现金流", "利润", "收入", "估值", "展望", "对比", "概况", "简介", "背景",
    "方法", "总览", "明细", "注", "目录", "序言",
)
_COMPANY_HINT_WORDS = (
    "公司", "集团", "股份", "控股", "银行", "证券", "保险", "酒业", "科技", "汽车",
    "医药", "能源", "地产", "实业", "有限", "厂商", "标的",
)


def _classify_heading(request: ResearchRequest, text: str, names_subject) -> str:
    """标题/表头属于哪一类作用域：`subject` / `other` / `neutral`。

    - 点名请求主体（名称或代码）→ `subject`；
    - 明显是章节词（指标/财务/分析/风险…）→ `neutral`（**继承**父作用域）；
    - 带公司后缀、或带交易所代码、或是短的专名（如裸的"比亚迪"）→ `other`（**切断**继承）。

    为什么要自己判：`acceptance_checker._subject_of` 依赖 `task_classifier._extract_company`，
    它对"## 比亚迪"这种裸专名给不出公司（实测），于是错公司小节会被当成中性标题、
    继承到母公司的作用域——这正是"错公司小节的 1741.44 漏检"的根因。
    """
    s = str(text or "").strip()
    if not s:
        return "neutral"
    if names_subject(s):
        return "subject"
    if any(w in s for w in _NEUTRAL_SECTION_WORDS):
        return "neutral"
    if market_of_code(s)[0] or re.search(r"\.(SZ|SH|SS|HK|US)$", s, re.I):
        return "other"
    if any(w in s for w in _COMPANY_HINT_WORDS):
        return "other"
    if 2 <= len(s) <= 8 and re.fullmatch(r"[\u4e00-\u9fffA-Za-z·]+", s):
        return "other"          # 短专名：首发模板里就是"别家公司小节"
    return "neutral"


def _heading_tree(text: str) -> list[tuple[int, int, str]]:
    """(行起点, 级别, 标题文本) 列表。"""
    return [(m.start(), len(m.group(1)), m.group(2).strip())
            for m in re.finditer(r"(?m)^(#{1,6})\s*(.+?)\s*$", text)]


def document_subject_scope(report_text: str, request: ResearchRequest,
                           paper: WorkingPaper | None = None) -> list[Problem]:
    """文档级主体作用域：标题层级与承载必需数值的作用域必须绑定**请求主体**。

    A′3 重写（此前四条都能漏）：
    - 用**标题层级**判定作用域：中性子标题（如"财务指标"）继承父标题的公司作用域，
      只有出现**别家公司**标题才切断；不再"最近标题必须点名本公司"；
    - 逐个**出现**判定：同一数值可能在文中出现多次，任何一次落在别家/无主作用域都要报，
      正确出现不能抵消错误出现；判定用数值 token 归一，`1741.44` 与 `1,741.44` 同结论；
    - 表格：表头行声明的主体（列作用域）同样生效。

    本批只覆盖三项核心指标（不做通用语言理解）。
    """
    problems: list[Problem] = []
    text = str(report_text or "")
    want = str(getattr(request, "company", "") or "").strip()
    want_id = str(getattr(request, "company_id", "") or "").strip()
    if not (want or want_id) or not text.strip():
        return problems

    def _names_subject(segment: str) -> bool:
        if not str(segment or "").strip():
            return False
        ok, _why = check_subject(request, segment, "")
        if ok:
            return True
        from facts import _norm_name   # 同一套归一，避免两处判据分叉
        n = _norm_name(want)
        return bool(n) and n in _norm_name(segment)

    heads = _heading_tree(text)
    if not heads:
        problems.append(Problem(
            PROBLEM_DOC_SCOPE,
            f"报告没有任何标题：无法确认这份文档是写给「{want or want_id}」的", "", ""))
    else:
        first = heads[0][2]
        if not _names_subject(first):
            problems.append(Problem(
                PROBLEM_DOC_SCOPE,
                f"报告首个标题是「{first[:60]}」，未点名请求主体"
                f"「{want or want_id}」：文档主体作用域未绑定", "", ""))

    def _scope_state(pos: int) -> tuple[bool, str]:
        """pos 处的作用域：返回 (是否属本公司, 说明)。按标题层级继承。

        中性子标题（"财务指标"）**继承**父标题的作用域；只有"别家公司"标题才切断。
        """
        stack: list[tuple[int, str, str]] = []       # (级别, 标题, 类别)
        for start, level, title in heads:
            if start > pos:
                break
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title, _classify_heading(request, title, _names_subject)))
        for _level, title, kind in reversed(stack):
            if kind == "other":
                return False, title
            if kind == "subject":
                return True, title
        return False, (stack[-1][1] if stack else "")

    def _table_head_for(pos: int) -> str:
        """该行上方最近的表格表头行（含"指标/年份"这类表头时返回它）。"""
        line_start = text.rfind("\n", 0, pos) + 1
        lines = text[:line_start].rstrip("\n").split("\n")
        for line in reversed(lines[-8:]):
            s = line.strip()
            if s.startswith("|") and re.search(r"(指标|科目|项目|年份|期间)", s):
                return s
            if s.startswith("|"):
                continue
            break
        return ""

    rows = (paper.rows if paper is not None else [])
    for r in rows:
        metric = str(r.get("metric") or "")
        if metric not in {m for m, _ in CORE_METRICS}:
            continue
        label = str(r.get("metric_label") or metric_label(metric))
        forms = _number_forms(r.get("value"))
        if not forms:
            continue
        # 逐个**数值 token** 出现位置判定：任何一次落在别家/无主作用域都记账
        offenders: list[str] = []
        for m in _NUM_TOKEN_RE.finditer(text):
            token = m.group(0)
            norm = token.replace(",", "").rstrip("0").rstrip(".") if "." in token else token
            if norm not in forms and token not in forms and token.replace(",", "") not in forms:
                continue
            pos = m.start()
            line_start = text.rfind("\n", 0, pos) + 1
            line_end = text.find("\n", pos)
            line = text[line_start:line_end if line_end >= 0 else len(text)]
            # 指标要对得上（同一行或同表头提到该指标），避免把别的指标的数字算进来
            head_row = _table_head_for(pos)
            if label not in line and label not in head_row \
                    and metric not in line and metric not in head_row:
                continue
            if _names_subject(line):
                continue                     # 本行显式点名本公司 → 在作用域内
            if head_row and _names_subject(head_row):
                continue                     # 表头列作用域点名本公司
            ok_scope, scope_name = _scope_state(pos)
            if ok_scope:
                continue
            offenders.append(f"{token}（作用域：{scope_name[:40] or '无标题'}）")
        if offenders:
            problems.append(Problem(
                PROBLEM_DOC_SCOPE,
                f"{label} {r.get('period')} 的数值在报告中出现于未绑定本次研究主体"
                f"「{want or want_id}」的作用域：{('；'.join(offenders[:3]))}",
                metric, str(r.get("period") or "")))
    return problems


def paper_csv(paper: WorkingPaper) -> str:
    """底稿导出（CSV 文本）：明细 + 派生 + 缺口三段，便于人工核对与二次计算。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["# 明细（可重算底稿）"])
    w.writerow(["fact_id", "主体", "主体标识", "指标", "期间", "报告期类型",
                "币种", "单位", "单位来源", "数值", "口径", "核实状态",
                "来源URL", "来源快照hash", "来源位置"])
    for r in paper.rows:
        w.writerow([r.get("fact_id"), r.get("entity"), r.get("entity_id"),
                    r.get("metric_label") or r.get("metric"), r.get("period"),
                    r.get("period_type"), r.get("currency"), r.get("unit"),
                    r.get("unit_source"), r.get("value"), r.get("caliber"),
                    r.get("verify_state"), r.get("source_url"), r.get("source_hash"),
                    json.dumps(r.get("source_locator") or {}, ensure_ascii=False)])
    w.writerow([])
    w.writerow(["# 派生值（公式 + 输入）"])
    # 派生行也带主体与单位：同比的单位是 %，与输入金额的"亿元"不是一回事
    w.writerow(["fact_id", "主体", "指标", "期间", "数值", "单位", "单位来源",
                "公式", "输入 fact_id"])
    for r in paper.derived:
        w.writerow([r.get("fact_id"), r.get("entity"), r.get("metric"),
                    r.get("period"), r.get("value"), r.get("unit"),
                    r.get("unit_source"), r.get("formula"),
                    " ".join(r.get("derived_from") or [])])
    w.writerow([])
    w.writerow(["# 缺口与问题"])
    w.writerow(["类型", "指标", "期间", "说明"])
    for g in paper.gaps:
        w.writerow([g.get("kind"), g.get("metric", ""), g.get("period", ""),
                    g.get("detail", "")])
    for p in paper.problems:
        w.writerow([p.kind, p.metric, p.period, p.detail])
    if paper.audit:
        w.writerow([])
        w.writerow(["# 审计提示（不参与达标判定）"])
        w.writerow(["类型", "说明"])
        for a in paper.audit:
            w.writerow([a.get("kind"), a.get("detail")])
    return buf.getvalue()
