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
from dataclasses import dataclass, field

from facts import (CORE_METRICS, UNKNOWN, VERIFY_UNVERIFIED, VERIFY_VERIFIED,
                   Fact, ResearchRequest, derived_fact, metric_label)

# 判定类别（给缺口表/报告用，字符串稳定，便于测试与前端展示）
PROBLEM_MISSING = "missing_required"
PROBLEM_UNVERIFIED = "unverified_required"
PROBLEM_SUBJECT = "subject_mismatch"
PROBLEM_PERIOD = "period_mismatch"
PROBLEM_CURRENCY = "currency_mismatch"
PROBLEM_CUMULATIVE = "cumulative_mixed"


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

    @property
    def ok(self) -> bool:
        """目标达成的底稿侧判据：无缺口、无实质问题。"""
        return not self.gaps and not self.problems

    def as_dict(self) -> dict:
        return {
            "request": self.request.as_dict(),
            "rows": self.rows, "derived": self.derived, "gaps": self.gaps,
            "problems": [p.as_dict() for p in self.problems],
            "completeness": self.completeness, "ok": self.ok,
        }


def _period_years(facts: list[Fact]) -> dict[str, dict[int, Fact]]:
    """metric → {year → fact}。只收**年报/未标类型**的行（累计口径），
    季报/中报不参与同比，避免"累计 vs 单季"混算。"""
    out: dict[str, dict[int, Fact]] = {}
    for f in facts:
        if f.period_type and f.period_type not in ("10-K", "年报", "annual", ""):
            continue
        year = None
        m = str(f.period or "")
        if m.endswith("年") and m[:-1].isdigit():
            year = int(m[:-1])
        if year is None:
            continue
        out.setdefault(f.metric, {})[year] = f
    return out


def _row(f: Fact) -> dict:
    d = f.as_dict()
    d["metric_label"] = f.metric_label or metric_label(f.metric)
    return d


def build_working_paper(facts: list[Fact], request: ResearchRequest) -> WorkingPaper:
    """从事实记录生成底稿：明细行 + 可重算同比 + 缺口 + 问题清单。"""
    paper = WorkingPaper(request=request, rows=[_row(f) for f in facts])

    # 1) 缺口：契约缺口 + 必需组合缺失
    for g in request.gaps:
        paper.gaps.append({"kind": "contract", "detail": g})
    by_combo: dict[tuple[str, str], Fact] = {}
    for f in facts:
        by_combo.setdefault((f.metric, f.period), f)
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

    # 1b) 年度要求只能由**年报**满足：只有季报/中报时不能当年度口径（累计 vs 单季不可比）
    for y in request.periods:
        for metric, _label in CORE_METRICS:
            if (metric, f"{y}年") in by_combo:
                continue
            others = [f for f in facts
                      if f.metric == metric and str(f.period or "").startswith(f"{y}")
                      and f.period != f"{y}年"]
            if others:
                paper.problems.append(Problem(
                    PROBLEM_CUMULATIVE,
                    f"{metric_label(metric)} {y}年 只有 {others[0].period}"
                    "（季报/中报属累计口径）：不能当年度数字，需补年报",
                    metric, f"{y}年"))

    # 2) 问题判定（只针对**必需组合自身的行**，无关的同值记录不得影响）
    want_entity = str(request.company or "")
    for metric, period in combos:
        f = by_combo.get((metric, period))
        if f is None:
            continue
        if want_entity and f.entity and want_entity not in f.entity and f.entity not in want_entity:
            paper.problems.append(Problem(
                PROBLEM_SUBJECT,
                f"{metric_label(metric)} {period} 的主体是「{f.entity}」，"
                f"与请求的公司「{want_entity}」不一致：不得算已核验",
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
        if f.verify_state != VERIFY_VERIFIED and f.verify_state == VERIFY_UNVERIFIED \
                and not f.source_url:
            paper.problems.append(Problem(
                PROBLEM_UNVERIFIED,
                f"{metric_label(metric)} {period} 没有来源位置：不得算已核验",
                metric, period))

    # 3) 同指标同期多行：币种不一致 / 累计与单季混用
    seen: dict[tuple[str, str], list[Fact]] = {}
    for f in facts:
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

    # 4) 同比：可重算（公式 + 输入 fact_id），只对同一主体/币种的相邻年报算
    years = sorted(request.periods)
    for metric, _label in CORE_METRICS:
        series = _period_years(facts).get(metric) or {}
        for prev, cur in zip(years, years[1:]):
            f0, f1 = series.get(prev), series.get(cur)
            if not f0 or not f1:
                continue
            if not isinstance(f0.value, (int, float)) or not isinstance(f1.value, (int, float)):
                continue
            if f0.value in (0, None):
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
            d = derived_fact([f0, f1], f"{metric}_yoy", formula=formula,
                             value=round(rate, 2), period=f"{cur}年同比", inputs=[f0, f1])
            paper.derived.append(_row(d))
    return paper


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
    w.writerow(["fact_id", "指标", "期间", "数值", "公式", "输入 fact_id"])
    for r in paper.derived:
        w.writerow([r.get("fact_id"), r.get("metric"), r.get("period"), r.get("value"),
                    r.get("formula"), " ".join(r.get("derived_from") or [])])
    w.writerow([])
    w.writerow(["# 缺口与问题"])
    w.writerow(["类型", "指标", "期间", "说明"])
    for g in paper.gaps:
        w.writerow([g.get("kind"), g.get("metric", ""), g.get("period", ""),
                    g.get("detail", "")])
    for p in paper.problems:
        w.writerow([p.kind, p.metric, p.period, p.detail])
    return buf.getvalue()
