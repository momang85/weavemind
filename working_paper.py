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
                   Fact, ResearchRequest, check_subject, derived_fact, metric_label)

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

    @property
    def ok(self) -> bool:
        """目标达成的底稿侧判据：无缺口、无实质问题。"""
        return not self.gaps and not self.problems

    def as_dict(self) -> dict:
        return {
            "request": self.request.as_dict(),
            "rows": self.rows, "derived": self.derived, "gaps": self.gaps,
            "problems": [p.as_dict() for p in self.problems],
            "completeness": self.completeness, "selection": self.selection,
            "ok": self.ok,
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

    三种产出分开记：
    - `selected`：主体/口径/期间都归属本次研究的行（同一组合去重后只留一条）；
    - `unrelated`：其他主体的行（不参与任何计算，且要作为问题列出来）；
    - `conflicts`：同一组合多条互相冲突的候选（列出全部 fact_id，该组合视为未满足）。
    """

    selected: list[Fact] = field(default_factory=list)
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
            "unrelated": [{"fact_id": f.fact_id, "entity": f.entity,
                           "entity_id": f.entity_id} for f in self.unrelated],
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


def select_facts(facts: list[Fact], request: ResearchRequest) -> Selection:
    """按 **主体 → 组合去重/冲突** 选出一份事实集合；校验与计算共用它。

    这是本批的核心修复：此前校验用"先到先得"、计算用"后到覆盖"，两者看的是**不同的行**
    ——给正确公司的事实后面追加一条别家公司记录，校验看前者、计算看后者，于是
    "贵州茅台 2023→2024 营业收入同比"能算出别家公司带来的 564.12%，底稿却仍然通过。
    """
    sel = Selection()
    for f in facts:
        ok, _why = check_subject(request, f.entity, f.entity_id)
        if not ok:
            sel.unrelated.append(f)
            continue
        sel.selected.append(f)

    groups: dict[tuple[str, str], list[Fact]] = {}
    for f in sel.selected:
        groups.setdefault((f.metric, f.period), []).append(f)
    keep: list[Fact] = []
    for (metric, period), items in groups.items():
        if len(items) == 1:
            keep.append(items[0])
            continue
        # 完全同值/同币种/同单位的重复登记视为同一行（真幂等），否则算冲突
        fingerprints = {(str(i.value), str(i.currency), str(i.unit),
                         str(i.period_type)) for i in items}
        if len(fingerprints) == 1:
            keep.append(items[0])
            continue
        cands = [{"fact_id": i.fact_id, "value": i.value,
                  "currency": str(i.currency or UNKNOWN), "unit": str(i.unit or UNKNOWN),
                  "period_type": str(i.period_type or ""),
                  "source_url": str(i.source_url or "")} for i in items]
        shown = "、".join(
            f"{c['value']}{c['unit']}（{c['source_url'] or '无来源'}）" for c in cands)
        sel.conflicts.append(Conflict(
            metric=metric, period=period, candidates=cands,
            detail=(f"{metric_label(metric)} {period} 有 {len(items)} 条互相冲突的候选"
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
    paper = WorkingPaper(request=request, rows=[_row(f) for f in selection.selected],
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

    # 1a) 其他公司的记录：不参与判定，但要**明确列出来**（取错公司只能产生缺口）
    if selection.unrelated:
        names = sorted({(f.entity or f.entity_id or "（无主体）")
                        for f in selection.unrelated})
        ids = [f.fact_id for f in selection.unrelated]
        paper.problems.append(Problem(
            PROBLEM_UNRELATED,
            f"载荷含 {len(ids)} 条非本次研究主体的记录（{('、'.join(names))[:120]}）："
            f"不参与本公司任何判定与计算；fact_id：{('、'.join(ids))[:200]}"))

    # 1b) 冲突候选：列全部 fact_id（不先到先得）
    for c in selection.conflicts:
        paper.problems.append(Problem(PROBLEM_CONFLICT, c.detail, c.metric, c.period))

    # 1c) 年度要求只能由**年报**满足：只有季报/中报时不能当年度口径（累计 vs 单季不可比）
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

    # 2) 问题判定（只针对**必需组合自身的行**，无关的同值记录不得影响）
    want_caliber = str(request.caliber or UNKNOWN)
    for metric, period in combos:
        f = by_combo.get((metric, period))
        if f is None:
            continue
        ok_subject, why_subject = check_subject(request, f.entity, f.entity_id)
        if not ok_subject:
            paper.problems.append(Problem(
                PROBLEM_SUBJECT,
                f"{metric_label(metric)} {period} 的主体无法确认属于本次研究：{why_subject}",
                metric, period))
        # 口径：请求声明了合并/母公司时，事实必须**声明且相符**；未知或不同一律不得算已核验
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
    return paper


def _value_forms(value) -> list[str]:
    """报告里可能出现的写法：原样 + 千分位。"""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        out = [f"{value:g}"]
        for d in (0, 1, 2):
            out.append(f"{value:,.{d}f}")
        return sorted({s for s in out if s})
    return [str(value)]


def _headings_before(text: str, pos: int) -> list[str]:
    """pos 之前的所有 Markdown 标题（按出现顺序）。"""
    return [m.group(1).strip()
            for m in re.finditer(r"(?m)^#{1,6}\s*(.+?)\s*$", text[:pos])]


def document_subject_scope(report_text: str, request: ResearchRequest,
                           paper: WorkingPaper | None = None) -> list[Problem]:
    """文档级主体作用域：标题与承载必需数值的表格/行必须绑定**请求主体**。

    本批只覆盖三项核心指标（不做通用自然语言证明系统）：
    - 报告首个标题必须点名请求公司；
    - 每个必需数值必须落在"点名了本公司"的作用域里：要么所在行自己带公司名，
      要么它上方的最近标题仍是本公司的（一旦上方出现别家公司标题，该数值不得认证）。

    这正是"给 A 公司报告配 B 公司同值来源"这类张冠李戴能被拦住的地方。
    """
    problems: list[Problem] = []
    text = str(report_text or "")
    want = str(getattr(request, "company", "") or "").strip()
    if not want or not text.strip():
        return problems

    def _names_subject(segment: str) -> bool:
        ok, _why = check_subject(request, segment, "")
        if ok:
            return True
        from facts import _norm_name   # 同一套归一，避免两处判据分叉
        n = _norm_name(want)
        return bool(n) and n in _norm_name(segment)

    heads = [m.group(1).strip() for m in re.finditer(r"(?m)^#{1,6}\s*(.+?)\s*$", text)]
    if not heads:
        problems.append(Problem(
            PROBLEM_DOC_SCOPE,
            "报告没有任何标题：无法确认这份文档是写给「%s」的" % want, "", ""))
    elif not _names_subject(heads[0]):
        problems.append(Problem(
            PROBLEM_DOC_SCOPE,
            f"报告首个标题是「{heads[0][:60]}」，未点名请求主体「{want}」："
            "文档主体作用域未绑定，不得据此认证", "", ""))

    rows = (paper.rows if paper is not None else [])
    for r in rows:
        if str(r.get("metric") or "") not in {m for m, _ in CORE_METRICS}:
            continue
        forms = _value_forms(r.get("value"))
        hit = None
        for form in forms:
            idx = text.find(form)
            while idx >= 0:
                line_start = text.rfind("\n", 0, idx) + 1
                line_end = text.find("\n", idx)
                line = text[line_start:line_end if line_end >= 0 else len(text)]
                if _names_subject(line):
                    hit = None
                    break
                prev = [h for h in _headings_before(text, idx)]
                scope = prev[-1] if prev else ""
                if scope and _names_subject(scope):
                    hit = None
                    break
                hit = (form, scope, line.strip()[:80])
                idx = text.find(form, idx + len(form))
            if hit is None:
                break
        if hit is not None:
            metric_label_text = r.get("metric_label") or metric_label(
                str(r.get("metric") or ""))
            problems.append(Problem(
                PROBLEM_DOC_SCOPE,
                f"{metric_label_text} {r.get('period')} 的数值 {hit[0]} 落在"
                f"「{hit[1] or '无标题'}」作用域下（行：{hit[2]}）："
                f"没有绑定本次研究的主体「{want}」，不得认证",
                str(r.get("metric") or ""), str(r.get("period") or "")))
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
    return buf.getvalue()
