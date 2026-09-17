# -*- coding: utf-8 -*-
"""S1：研究请求契约 + 最小事实记录（fact）。

两件事，合在一个模块里是为了让"契约要什么"和"事实记了什么"能对上：

1. **研究请求契约**（`ResearchRequest`）：公司稳定标识、市场、期间、报表口径、
   必需指标、资料截至日、来源要求、预算。目标里说不清的部分**列成缺口**并标
   `needs_confirmation`，不靠模型把身份/期间猜出来（架构指令明确要求：
   歧义公司/期间要求确认或标缺口）。
2. **最小事实记录**（`Fact`）：`fact_id`、`entity_id`、指标、期间起止/类型、
   币种/单位、原始值/规范化值、口径、来源快照 hash 与位置、提取/核实状态；
   派生值再记公式与输入 `fact_id`。**元数据缺失一律 unknown**，不从模型补造"已核实"。

兼容性：`facts_from_financials` 只读现有 `financials.json` 载荷形状
（单实体 `{financials, metadata, raw}` / 多实体 `{companies: [...], metadata, raw}`），
不要求上游改结构。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# ── 契约常量 ────────────────────────────────────────────────

# 首发路径的三个核心指标（架构指令：收入、归母净利润、经营现金流）
CORE_METRICS: tuple[tuple[str, str], ...] = (
    ("revenue", "营业收入"),
    ("net_profit", "归母净利润"),
    ("operating_cashflow", "经营活动现金流净额"),
)
ALL_METRICS: tuple[tuple[str, str], ...] = CORE_METRICS + (
    ("gross_profit", "毛利润"),
    ("gross_margin", "毛利率"),
    ("operating_profit", "经营利润"),
    ("total_assets", "总资产"),
    ("total_liabilities", "总负债"),
    ("rd_expense", "研发投入"),
)
METRIC_LABELS = dict(ALL_METRICS)

MARKETS = ("cn", "hk", "us")
UNKNOWN = "unknown"

VERIFY_UNKNOWN = "unknown"          # 无足够证据
VERIFY_UNVERIFIED = "unverified"    # 有来源但未被独立核实
VERIFY_VERIFIED = "verified"        # 经人工/独立渠道核实

_CALIBERS = ("合并", "母公司")


def _norm_metric(key: str) -> str:
    return str(key or "").strip()


def metric_label(key: str) -> str:
    return METRIC_LABELS.get(_norm_metric(key), _norm_metric(key))


# ── 研究请求契约 ────────────────────────────────────────────


@dataclass
class ResearchRequest:
    """一次公司研究的输入契约；说不清的部分留在 `gaps` 里。"""

    goal: str = ""
    company: str = ""                  # 显示名（识别不到就是空）
    company_id: str = ""               # 稳定标识：股票代码 / ticker / CIK（空=未知）
    market: str = UNKNOWN              # cn / hk / us / unknown
    periods: list[int] = field(default_factory=list)
    caliber: str = UNKNOWN             # 合并 / 母公司 / unknown
    required_metrics: list[str] = field(default_factory=lambda: [m for m, _ in CORE_METRICS])
    as_of: str = ""                    # 资料截至日（空=未知）
    source_requirements: list[str] = field(default_factory=list)
    budget: dict = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)

    @property
    def needs_confirmation(self) -> bool:
        """有缺口就要先确认（歧义不猜）。"""
        return bool(self.gaps)

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["needs_confirmation"] = self.needs_confirmation
        out["required_metrics"] = list(self.required_metrics)
        return out


_YEAR_RE = re.compile(r"(20\d{2})")
_ASOF_RE = re.compile(r"(?:截至|截至日|数据截至|as\s*of)\s*(20\d{2}[-/年.]\d{1,2}(?:[-/月.]\d{1,2})?)")


def parse_research_request(goal: str, *, company: str = "", company_id: str = "",
                           market: str = "", periods: list[int] | None = None,
                           caliber: str = "", as_of: str = "",
                           budget: dict | None = None) -> ResearchRequest:
    """把目标文本（+ 调用方已知的解析结果）整理成契约，并把不确定项列成缺口。

    不做"猜身份"：公司名/代码/市场这些**必须**来自目标文本或调用方，缺了就记缺口。
    """
    req = ResearchRequest(goal=str(goal or ""))
    text = str(goal or "")

    # 公司：优先调用方给的（来自 classifier/解析器），否则从目标里取一个候选
    req.company = str(company or "").strip()
    req.company_id = str(company_id or "").strip()
    if not req.company:
        try:
            from task_classifier import _extract_company  # 本地导入，避免循环依赖
            req.company = str(_extract_company(text) or "").strip()
        except Exception:
            req.company = ""
        if not req.company:
            req.gaps.append("未识别到公司：请给出公司名或股票代码（不猜身份）")

    # 市场：调用方给 > 目标文本关键词 > unknown
    mk = str(market or "").strip().lower()
    if mk not in MARKETS:
        if re.search(r"(A股|沪深|上交所|深交所)", text):
            mk = "cn"
        elif re.search(r"(港股|港交所|H股)", text):
            mk = "hk"
        elif re.search(r"(美股|纳斯达克|纽交所|SEC|10-K)", text, re.I):
            mk = "us"
        else:
            mk = UNKNOWN
    req.market = mk
    if mk == UNKNOWN:
        req.gaps.append("未确定市场（A股/港股/美股）：口径与数据源不同，需确认")

    # 资料截至日：**必须在取年份之前解析**——否则"数据截至 2025-04-30"里的 2025
    # 会被当成研究年度（6 个必需组合凭空变成 9 个）。调用方传入的 as_of 同样要先记下来。
    req.as_of = str(as_of or "").strip()
    if not req.as_of:
        m = _ASOF_RE.search(text)
        req.as_of = m.group(1) if m else ""
    if not req.as_of:
        req.gaps.append("未给资料截至日：报告须明示数据时效")

    # 期间：调用方给 > 目标里的年份（同一公司两个年度是首发默认）
    #
    # **先把"资料截至日"那段摘掉**再取年份：否则 "数据截至 2025-04-30" 里的 2025
    # 会被当成研究年度，凭空多出一个组合（实测：6 个必需组合变成 9 个，任务永远缺 3 个）。
    years = [int(y) for y in (periods or []) if str(y).isdigit()]
    if not years:
        _scan = text
        if req.as_of:
            _scan = _scan.replace(req.as_of, " ")
            _scan = re.sub(r"(?:截至|截至日|数据截至|as\s*of)\s*", " ", _scan)
        years = sorted({int(m) for m in _YEAR_RE.findall(_scan)})
    req.periods = sorted(set(years))
    if len(req.periods) < 2:
        req.gaps.append(
            f"需要两个明确年度（当前 {req.periods or '无'}）：缺年度则无法做同比与两期对照")

    # 口径：显式写了才认，否则 unknown（不默认成合并报表）
    cal = str(caliber or "").strip()
    if not cal:
        for c in _CALIBERS:
            if c in text:
                cal = c
                break
    req.caliber = cal if cal in _CALIBERS else UNKNOWN
    if req.caliber == UNKNOWN:
        req.gaps.append("未声明报表口径（合并/母公司）：按未知处理，不默认合并")


    # 来源要求与预算：给了就记，没给不编
    req.source_requirements = ["公开年报", "可定位到来源位置"]
    if re.search(r"(用户提供|附件|上传)", text):
        req.source_requirements.append("用户提供的材料（真实性未独立核实）")
    req.budget = dict(budget or {})
    return req


# ── 最小事实记录 ────────────────────────────────────────────


@dataclass
class Fact:
    """一条可复核的事实。缺什么就写 unknown，不用模型编。"""

    fact_id: str
    entity: str
    entity_id: str
    metric: str
    metric_label: str
    period: str = ""
    period_start: str = ""
    period_end: str = ""
    period_type: str = ""
    currency: str = UNKNOWN
    unit: str = UNKNOWN
    unit_source: str = UNKNOWN
    value: Any = None                 # 规范化值（已按 unit 缩放）
    raw_value: Any = None             # 原始值（源里怎么写就怎么记）
    caliber: str = UNKNOWN
    source_url: str = ""
    source_hash: str = ""
    source_locator: dict = field(default_factory=dict)
    verify_state: str = VERIFY_UNVERIFIED
    extracted_by: str = "adapter"
    formula: str = ""
    derived_from: list[str] = field(default_factory=list)

    @property
    def derived(self) -> bool:
        return bool(self.formula or self.derived_from)

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["derived"] = self.derived
        return out


def make_fact_id(entity_id: str, entity: str, metric: str, period: str,
                 caliber: str = "") -> str:
    """稳定 ID：同一条事实在**各层**（financials → clean → 底稿）都不变。

    刻意只用"身份 + 指标 + 期间 + 口径"，不含数值：数值被上游修正时，
    它仍是同一条事实（改的是值，不是身份），底稿才能做"值变了"的对照。
    """
    payload = "|".join([
        str(entity_id or entity or "").strip().lower(),
        str(metric or "").strip().lower(),
        str(period or "").strip(),
        str(caliber or "").strip(),
    ])
    return "fact-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _snapshot_hash(raw: Any) -> str:
    """来源快照 hash：有原文就按原文算，没有留空（不编）。"""
    if isinstance(raw, dict):
        text = str(raw.get("text") or "")
    else:
        text = str(raw or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:32]


def _iter_entities(payload: dict) -> list[dict]:
    """把 financials.json 的两种形状归一成实体列表。"""
    if not isinstance(payload, dict):
        return []
    companies = payload.get("companies")
    if isinstance(companies, list) and companies:
        out = []
        for ent in companies:
            if not isinstance(ent, dict):
                continue
            md = dict(ent.get("metadata") or {})
            md.setdefault("company", ent.get("name") or "")
            out.append({
                "metadata": md,
                "financials": ent.get("financials") or [],
                "raw": ent.get("raw") or payload.get("raw") or {},
            })
        return out
    return [{
        "metadata": dict(payload.get("metadata") or {}),
        "financials": payload.get("financials") or [],
        "raw": payload.get("raw") or {},
    }]


def _period_of(row: dict) -> tuple[str, str, str, str]:
    """(期标签, 起, 止, 类型)。缺 start/end 就留空——不推算。"""
    year = row.get("year")
    report_type = str(row.get("report_type") or "")
    start = str(row.get("start") or "")
    end = str(row.get("end") or "")
    if report_type in ("10-K", "年报", "annual"):
        label = f"{year}年" if year else ""
    elif report_type:
        label = f"{year}{report_type}" if year else report_type
    else:
        label = f"{year}年" if year else ""
    return label, start, end, report_type


def facts_from_financials(payload: dict, *, source_kind: str = "",
                          metrics: tuple[tuple[str, str], ...] = ALL_METRICS,
                          verify_state: str = VERIFY_UNVERIFIED) -> list[Fact]:
    """把 `financials.json` 载荷转成事实记录（兼容现有形状，不改上游）。

    - 主体/币种/单位取自 `metadata`（S1-1 已保证来源声明随行走）；
    - `metadata` 缺币种/单位 → 记 unknown（**不**回落成"亿元"这种具体口径，
      那会在底稿里伪造出一个来源没说过的事实）；
    - `verify_state` 默认 `unverified`：来自结构化源 ≠ 已核实。
    """
    facts: list[Fact] = []
    for ent in _iter_entities(payload):
        md = ent["metadata"]
        rows = ent["financials"]
        if not isinstance(rows, list):
            continue
        entity = str(md.get("company") or md.get("name") or "")
        entity_id = str(md.get("ticker") or md.get("code") or md.get("stock_code") or "")
        currency = str(md.get("currency") or UNKNOWN)
        unit = str(md.get("unit") or UNKNOWN)
        url = str((ent["raw"] or {}).get("url") or "")
        snap = _snapshot_hash(ent["raw"])
        kind = str(source_kind or md.get("source") or "")
        for row in rows:
            if not isinstance(row, dict):
                continue
            period, p_start, p_end, p_type = _period_of(row)
            for key, label in metrics:
                if key not in row or row.get(key) is None:
                    continue
                caliber = str(row.get("caliber") or UNKNOWN)
                # 行级声明优先于元数据：同一载荷里不同年度/不同来源的币种可能不同，
                # 只有行级能如实表达；缺失仍记 unknown。
                row_currency = str(row.get("currency") or currency or UNKNOWN)
                row_unit = str(row.get("unit") or unit or UNKNOWN)
                facts.append(Fact(
                    fact_id=make_fact_id(entity_id, entity, key, period, caliber),
                    entity=entity, entity_id=entity_id,
                    metric=key, metric_label=label,
                    period=period, period_start=p_start, period_end=p_end,
                    period_type=p_type,
                    currency=row_currency, unit=row_unit,
                    unit_source=("row" if str(row.get("unit") or "")
                                 else ("source" if str(md.get("unit") or "") else UNKNOWN)),
                    value=row.get(key), raw_value=row.get(key),
                    caliber=caliber,
                    source_url=url, source_hash=snap,
                    source_locator={
                        "kind": "structured_field",
                        "field": f"{kind}.{key}" if kind else key,
                        "report_type": p_type,
                    },
                    verify_state=verify_state,
                    extracted_by="adapter",
                ))
    return facts


def derived_fact(base: list[Fact], metric: str, *, formula: str,
                 value: Any, period: str, inputs: list[Fact]) -> Fact:
    """派生事实：公式 + 输入 fact_id 都要记，值由调用方算好（本函数不做算术）。"""
    first = inputs[0] if inputs else None
    return Fact(
        fact_id=make_fact_id(
            (first.entity_id if first else ""), (first.entity if first else ""),
            metric, period, (first.caliber if first else "")),
        entity=(first.entity if first else ""), entity_id=(first.entity_id if first else ""),
        metric=metric, metric_label=metric_label(metric),
        period=period, currency=(first.currency if first else UNKNOWN),
        unit=(first.unit if first else UNKNOWN),
        value=value, raw_value=value,
        caliber=(first.caliber if first else UNKNOWN),
        source_url=(first.source_url if first else ""),
        source_hash=(first.source_hash if first else ""),
        verify_state=VERIFY_UNVERIFIED,
        extracted_by="derived",
        formula=formula,
        derived_from=[f.fact_id for f in inputs],
    )


def facts_to_json(facts: list[Fact]) -> str:
    return json.dumps([f.as_dict() for f in facts], ensure_ascii=False, indent=1)
