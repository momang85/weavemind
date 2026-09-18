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
CALIBERS = _CALIBERS          # 公开别名：判定侧要用同一份枚举（不各写一遍）


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
    # 身份来自哪里：form（表单结构化字段）/ text（自由文本解析）/ candidate（抓取候选，仅用于比对）
    identity_source: str = ""

    @property
    def needs_confirmation(self) -> bool:
        """有缺口就要先确认（歧义不猜）。"""
        return bool(self.gaps)

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["needs_confirmation"] = self.needs_confirmation
        out["required_metrics"] = list(self.required_metrics)
        return out

    def to_payload(self) -> dict:
        """落库用（JSON 安全，不依赖 pickle）；与 `from_payload` 成对。"""
        return {
            "goal": str(self.goal or ""),
            "company": str(self.company or ""),
            "company_id": str(self.company_id or ""),
            "market": str(self.market or UNKNOWN),
            "periods": [int(y) for y in (self.periods or [])],
            "caliber": str(self.caliber or UNKNOWN),
            "required_metrics": [str(m) for m in (self.required_metrics or [])],
            "as_of": str(self.as_of or ""),
            "source_requirements": [str(s) for s in (self.source_requirements or [])],
            "budget": dict(self.budget or {}),
            "gaps": [str(g) for g in (self.gaps or [])],
            "identity_source": str(self.identity_source or ""),
        }

    @classmethod
    def from_payload(cls, raw: dict | None) -> "ResearchRequest | None":
        """从落库载荷恢复契约；结构不对（不是 dict / 没有可用字段）返回 None，不猜。"""
        if not isinstance(raw, dict) or not raw:
            return None
        periods = []
        for y in raw.get("periods") or []:
            try:
                periods.append(int(str(y).strip()))
            except Exception:
                continue
        return cls(
            goal=str(raw.get("goal") or ""),
            company=str(raw.get("company") or "").strip(),
            company_id=str(raw.get("company_id") or "").strip(),
            market=str(raw.get("market") or UNKNOWN).strip().lower() or UNKNOWN,
            periods=sorted(set(periods)),
            caliber=str(raw.get("caliber") or UNKNOWN).strip() or UNKNOWN,
            required_metrics=[str(m) for m in (raw.get("required_metrics") or [])
                              if str(m).strip()] or [m for m, _ in CORE_METRICS],
            as_of=str(raw.get("as_of") or "").strip(),
            source_requirements=[str(s) for s in (raw.get("source_requirements") or [])],
            budget=dict(raw.get("budget") or {}),
            gaps=[str(g) for g in (raw.get("gaps") or [])],
            identity_source=str(raw.get("identity_source") or ""),
        )


_YEAR_RE = re.compile(r"(20\d{2})")
_ASOF_RE = re.compile(r"(?:截至|截至日|数据截至|as\s*of)\s*(20\d{2}[-/年.]\d{1,2}(?:[-/月.]\d{1,2})?)")


def _norm_date(value: str) -> str:
    """把常见日期写法归一成 `YYYY-MM-DD`；解析不了返回空串（不猜）。"""
    s = str(value or "").strip()
    if not s:
        return ""
    digits = re.findall(r"\d+", s)
    if len(digits) < 3:
        return ""
    y, m, d = (int(digits[0]), int(digits[1]), int(digits[2]))
    if not (1900 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{m:02d}-{d:02d}"


def parse_research_request(goal: str, *, company: str = "", company_id: str = "",
                           market: str = "", periods: list[int] | None = None,
                           caliber: str = "", as_of: str = "",
                           budget: dict | None = None,
                           identity_source: str = "") -> ResearchRequest:
    """把目标文本（+ 调用方已知的解析结果）整理成契约，并把不确定项列成缺口。

    不做"猜身份"：公司名/代码/市场这些**必须**来自目标文本或调用方，缺了就记缺口。
    """
    req = ResearchRequest(goal=str(goal or ""))
    text = str(goal or "")

    # 公司：优先调用方给的（表单结构化字段/解析器），否则从目标里取一个候选。
    # **抓取来的元数据不得走这里**：那样用户请求会被数据源反向决定（A 批门槛的前提）。
    req.company = str(company or "").strip()
    req.company_id = str(company_id or "").strip()
    req.identity_source = str(identity_source or ("caller" if req.company else ""))
    if not req.company:
        try:
            from task_classifier import _extract_company  # 本地导入，避免循环依赖
            req.company = str(_extract_company(text) or "").strip()
            if req.company:
                req.identity_source = identity_source or "text"
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
    # 代码形输入：表单提示"公司名或代码"，用户可能只在公司栏里写 `600519.SH`。
    # 带明确市场后缀的代码可以**确定性**识别（补出 company_id 与市场）；裸 1–5 位
    # 数字有市场歧义 → 留缺口请确认，不静默猜公司（A′1 一起补的正常入口）。
    if req.company:
        _shape_market, _ambiguous = market_of_code(req.company)
        if _shape_market and not _ambiguous:
            if not req.company_id:
                req.company_id = req.company
            if req.market == UNKNOWN:
                req.market = _shape_market
            elif req.market != _shape_market:
                req.gaps.append(
                    f"公司代码后缀（{_shape_market}）与所选市场（{req.market}）不一致："
                    "请确认后再研究")
            req.identity_source = req.identity_source or "code"
        elif _ambiguous and not req.company_id:
            req.gaps.append(
                f"「{req.company}」是裸代码，无法确定市场（A股/港股/美股）："
                "请带交易所后缀（如 00700.HK）或选择市场")
    if req.market == UNKNOWN:
        req.gaps.append("未确定市场（A股/港股/美股）：口径与数据源不同，需确认")

    # 资料截至日：**必须在取年份之前解析**——否则"数据截至 2025-04-30"里的 2025
    # 会被当成研究年度（6 个必需组合凭空变成 9 个）。调用方传入的 as_of 同样要先记下来。
    req.as_of = str(as_of or "").strip()
    if not req.as_of:
        m = _ASOF_RE.search(text)
        req.as_of = m.group(1) if m else ""
    if req.as_of:
        # 格式要能解析（`2025-04-30` / `2025/4/30` / `2025.4.30` / `2025年4月30日`），
        # 归一成 ISO；解析不了的日期不是"截至日"，是错误输入（A′4）
        _norm = _norm_date(req.as_of)
        if _norm:
            req.as_of = _norm
        else:
            req.gaps.append(f"资料截至日「{req.as_of}」不是有效日期：请写成 2025-04-30")
            req.as_of = ""
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


def bare_code(code: str) -> str:
    """去掉交易所后缀的**本地代码**：`600519.SH` → `600519`、`00700.HK` → `00700`。

    契约里的 `company_id` 是稳定身份（带后缀，跨市场不撞车），而各市场适配器只认
    本地代码（东财 A 股接口收到 `600519.SH` 直接报"无数据"）。美股 ticker
    （`AAPL`、`BRK.B`）不带交易所后缀，原样返回。
    """
    s = str(code or "").strip().upper()
    m = re.match(r"^([0-9A-Z.]+?)[.\-]([A-Z]{2,6})$", s)
    if m and m.group(2) in _SUFFIX_MARKET:
        return m.group(1)
    return s


def research_subject(request: ResearchRequest | None) -> bool:
    """契约里有没有**明确主体**（公司名或稳定标识）——研究门槛的最低条件。"""
    return bool(request is not None and (request.company or request.company_id))


def research_shaped(request: ResearchRequest | None) -> bool:
    """**研究任务**判据：有主体 + ≥2 个期间 + 已声明口径。

    只声明主体、说不清期间或口径的请求不算：连取数区间都不确定，不能据此选路径
    （口径未知按未知处理，不默认合并）。固定研究路径与交付硬门槛共用这一处定义。
    """
    if not research_subject(request):
        return False
    if len(list(request.periods or [])) < 2:
        return False
    return str(request.caliber or UNKNOWN) in _CALIBERS


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
    # 主体所在市场（cn/hk/us）与披露日期：前者是主体标识的一部分（A′1），
    # 后者是"截至日能否成立"的证据（A′4，与期末 period_end、抓取时间分开记）
    market: str = ""
    disclosed_at: str = ""
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
        # 市场：元数据显式给 > 从代码后缀读（都不猜）；主体标识的一部分
        md_market = str(md.get("market") or "").strip().lower()
        if md_market not in MARKETS:
            md_market = market_of_code(entity_id)[0] or market_of_code(entity)[0]
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
                # 报表口径只认**来源声明的**：行级优先，其次实体级；没有就是 unknown
                # （不默认成"合并"，也不接受把报告期描述当口径——那是另一个维度）
                caliber = str(row.get("caliber") or md.get("caliber") or UNKNOWN)
                # 行级声明优先于元数据：同一载荷里不同年度/不同来源的币种可能不同，
                # 只有行级能如实表达；缺失仍记 unknown。
                row_currency = str(row.get("currency") or currency or UNKNOWN)
                row_unit = str(row.get("unit") or unit or UNKNOWN)
                # 披露日期（有就记）：报告期末 ≠ 抓取时间 ≠ 披露时间，三者分开
                row_disclosed = str(row.get("disclosure_date")
                                    or row.get("disclosed_at")
                                    or row.get("report_date") or "").strip()[:10]
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
                    market=md_market,
                    disclosed_at=row_disclosed,
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
                 value: Any, period: str, inputs: list[Fact],
                 unit: str | None = None, unit_source: str | None = None) -> Fact:
    """派生事实：公式 + 输入 fact_id 都要记，值由调用方算好（本函数不做算术）。

    `unit`/`unit_source` 可显式给出：**同比是百分比**，不能继承输入的"亿元"——否则
    底稿上会出现"同比 = 15.66 亿元"这种把比率当金额的单位错位（实测缺陷）。
    未显式给出时才沿用首个输入（例如同为金额的加减派生）。
    """
    first = inputs[0] if inputs else None
    return Fact(
        fact_id=make_fact_id(
            (first.entity_id if first else ""), (first.entity if first else ""),
            metric, period, (first.caliber if first else "")),
        entity=(first.entity if first else ""), entity_id=(first.entity_id if first else ""),
        metric=metric, metric_label=metric_label(metric),
        period=period, currency=(first.currency if first else UNKNOWN),
        unit=(unit if unit is not None else (first.unit if first else UNKNOWN)),
        unit_source=(unit_source if unit_source is not None
                     else ("derived" if unit is not None else UNKNOWN)),
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


# ── 主体比对（稳定标识优先，其次名称归一）────────────────────
#
# 为什么不用裸字符串包含：`"贵州茅台" in "贵州茅台酒股份有限公司"` 这类判断对
# 全称/简称好使，但抓错公司时同样能"包含"成功（如"贵州茅台镇某酒业"），而且
# 从不看稳定标识。这里先比 id，再比归一后的名称；两边都缺就返回 unknown（不算不符）。

_NAME_NOISE = ("股份有限公司", "有限责任公司", "有限公司", "控股集团", "集团公司",
               "集团", "股份", "公司", "控股")

# 交易所后缀 → 市场。**唯一映射来源**：不从别处猜。
_SUFFIX_MARKET = {"SZ": "cn", "SH": "cn", "SS": "cn", "BJ": "cn", "HK": "hk",
                  "US": "us", "NASDAQ": "us", "NYSE": "us"}


def market_of_code(code: str) -> tuple[str, bool]:
    """从代码读市场：返回 `(market, ambiguous)`。

    - `600519.SH` / `000001.SZ` → cn；`00700.HK` → hk；`AAPL` / `BRK.B` → us；
    - 裸 6 位数字 → cn（港股票代码最多 5 位，6 位无歧义）；
    - 裸 1–5 位数字 → **有歧义**（港股/其它），不猜；
    - 其余 → 未知。
    """
    s = str(code or "").strip().upper()
    if not s:
        return "", False
    m = re.match(r"^([0-9A-Z.]+?)[.\-]([A-Z]{2,6})$", s)
    if m and m.group(2) in _SUFFIX_MARKET:
        return _SUFFIX_MARKET[m.group(2)], False
    if re.match(r"^\d{6}$", s):
        return "cn", False
    if re.match(r"^\d{1,5}$", s):
        return "", True                     # 裸代码：市场不明，请确认
    if re.match(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$", s):
        return "us", False
    return "", False


def _norm_code(code: str, market: str) -> str:
    """按**已知市场**归一代码：只在同一市场内做别名归一（跨市场绝不相同）。

    - cn：保留 6 位（`000001` 是有效代码，**不能**去前导零）；
    - hk：去前导零（`00700` 与 `700` 是同一个）；
    - us：大写 ticker 原样。
    """
    s = re.sub(r"[^0-9A-Z.]", "", str(code or "").upper())
    s = re.sub(r"\.(SZ|SH|SS|BJ|HK|US|NASDAQ|NYSE)$", "", s)
    if market == "hk":
        return s.lstrip("0") or "0"
    if market == "us":
        return s
    return s


@dataclass(frozen=True)
class SubjectKey:
    """带命名空间的主体标识：`market` 与 `code` 必须一起看。"""

    market: str = ""
    code: str = ""
    name: str = ""

    @property
    def has_code(self) -> bool:
        return bool(self.code)


def canonical_subject(*, name: str = "", code: str = "", market: str = "") -> SubjectKey:
    """把 (名称, 代码, 市场) 归一成可核对的主体标识。

    关键点（A′1）：**市场是标识的一部分**——`000001.SZ`（平安银行）与 `00001.HK`
    是两家公司，此前把后缀与前导零都剥掉后两者都剩 `1`，于是被判成同一主体。
    """
    mk = str(market or "").strip().lower()
    if mk in ("unknown", "none"):
        mk = ""
    code_s = str(code or "").strip()
    name_s = str(name or "").strip()
    # 名称栏里写了带后缀的代码（表单提示"公司名或代码"）→ 当作代码处理
    if not code_s and name_s:
        shape_market, ambiguous = market_of_code(name_s)
        if shape_market and not ambiguous:
            code_s, mk = name_s, (mk or shape_market)
    shape_market, ambiguous = market_of_code(code_s) if code_s else ("", False)
    if shape_market and not mk:
        mk = shape_market
    elif shape_market and not ambiguous and mk and mk != shape_market:
        # 代码后缀与显式市场冲突：两个都是声明，不能替用户选一个
        return SubjectKey(market=f"conflict:{mk}>{shape_market}", code=code_s, name=name_s)
    elif ambiguous and not mk:
        return SubjectKey(market="ambiguous", code=code_s, name=name_s)
    return SubjectKey(market=mk, code=_norm_code(code_s, mk) if code_s else "", name=name_s)


def _norm_name(value: str) -> str:
    s = re.sub(r"[\s·・,，.。()（）\-—/]", "", str(value or ""))
    s = s.lower()
    for word in _NAME_NOISE:
        s = s.replace(word.lower(), "")
    return s


def check_subject(request: ResearchRequest, entity: str, entity_id: str = "",
                  market: str = "") -> tuple[bool, str]:
    """事实主体是否就是请求主体。返回 `(是否一致, 说明)`。

    规则（A′1，确定性、离线）：
    - 双方都有稳定标识 → **先比市场/交易所**：都不明或相同才继续比代码；
      市场明确冲突 → 直接不一致（**不允许**用名称包含关系补救）；
    - 同一市场内按该市场的规则比代码（cn 保留前导零、hk 去前导零、us 大写）；
    - 一侧没有可用代码才退回名称比对，且仍要求市场不冲突；
    - 任一侧缺主体 / 代码市场有歧义 → `False`（缺证据不能算已核验）。
    """
    want = canonical_subject(name=str(getattr(request, "company", "") or ""),
                             code=str(getattr(request, "company_id", "") or ""),
                             market=str(getattr(request, "market", "") or ""))
    got = canonical_subject(name=entity, code=entity_id, market=market)
    if not (got.name or got.has_code):
        return False, "事实没有主体（缺主体不得算已核验）"
    if want.market.startswith("conflict:"):
        return False, f"请求自身的市场与代码后缀冲突（{want.market.split(':', 1)[1]}）"
    if got.market.startswith("conflict:"):
        return False, f"事实的市场与代码后缀冲突（{got.market.split(':', 1)[1]}）"
    if want.market == "ambiguous" or got.market == "ambiguous":
        return False, "代码市场有歧义（请确认市场：A股/港股/美股）"
    # 市场明确冲突 → 直接拒绝，不看名称
    if want.market and got.market and want.market != got.market:
        return False, (f"市场/交易所冲突：请求 {want.market} vs 事实 {got.market}"
                       "（跨市场同名/同码不是同一主体）")
    if want.has_code and got.has_code:
        if want.code and got.code and want.code == got.code:
            return True, ""
        return False, f"标识不一致（请求 {want.code or want.name} vs 事实 {got.code or got.name}）"
    if not want.name and not want.has_code:
        return False, "请求没有公司主体，无法比对"
    if not got.name:
        return False, "事实没有主体名称，无法比对"
    a, b = _norm_name(want.name), _norm_name(got.name)
    if a and b and (a in b or b in a):
        return True, ""
    return False, f"主体不一致（请求「{want.name or want.code}」vs 事实「{got.name or got.code}」）"
