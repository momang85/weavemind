# -*- coding: utf-8 -*-
"""可解释选股 MVP（R1-SCREEN-1023，R1-A）：纯筛选核心 + 输入快照。

设计边界（`docs/员工任务_R1可解释选股MVP_20261023.md` §3-§4、
`docs/选股与量化产品架构路线_20261023.md` §3-§4）：

- **纯计算，零模型调用**：不 import `llm_client`/`web_ui`/`orchestrator_v2`，
  不发请求、不写网络；筛选与排序在后端确定性执行，前端只展示。
- **复用财务 Observation**：观察状态（缺失/未知/冲突/无效与真实零五分）、
  口径不回退、期间身份（flow/stock、半年度不冒年报、同期区间相容）全部走
  `financial_analysis`（`dataset.freeze` + `require`），不另建第二套事实台账。
- **不穿越**：PIT 判据 `available_at <= as_of`（缺 available_at 用 disclosed_at，
  都缺 → 待核验）；`period_end > as_of` 同样不纳入。缺可用时点不假装当时可得，
  更正版本不倒灌到过去。
- **不掩盖缺失**：通过/未通过/资料不足三态分开；缺数据不填零、不前值补齐、
  不把缺财务当"低风险"；零入选也是合法结果。
- **身份不混用**：`net_profit` = 归母净利润（归属于上市公司股东的净利润），
  `net_profit_consolidated` = 合并净利润（含少数股东）；比率字段声明用哪个就是哪个，
  不自动替换含义相近字段。
- **确定性**：同一输入/规则/期间政策/实现/截止日 → 同一结果（哈希可复算）；平局按代码稳定排序；
  显示精度不改变比较的原始精度；代码输入顺序不改变读数与入选集合。
- **期间政策进身份**（R1.1，2026-10-23 架构师复核）：默认「统一报告期」——全池共同的最新年度；
  缺该期或该期冲突 → 资料不足，**不回退到更早年度**。`pinned` 由用户显式指定年度；
  `latest_each` 显式选择逐家最新并标记 `comparable=False`（不同期间不可横比）。
- **排序是展示态**（R1.1）：`sort` 不进结果身份、不改 `result_hash`，只影响返回的行顺序；
  非法排序参数明确 400，不静默回退。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import dataclasses
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from facts import make_fact_id, market_of_code, metric_label
from financial_analysis.contracts import (
    MissingInput, NotApplicable, NotComputable, State, period_role_of,
)
from financial_analysis.dataset import freeze, unit_scale_of
from financial_analysis.operators import profit_bridge

SCHEMA_VERSION = "weavemind.stock_screen/1"
# 版本语义：**输出内容变了必须 bump**。通过理由从「毛利率 71.60%」改成
# 「毛利率 71.60% ≥ 70%」属于输出变化；不 bump 会与同输入的历史结果撞同一
# 身份（run_id），store 按 FrozenCorruptError 409 拒绝覆盖（保护，不是 bug）。
# 1.3.0（T0-G1，2026-10-24）：同比增速改判据——只认严格相邻的上一个自然年，
# 缺上一年度报「缺上一年度」，不再拿"期间列表的前一条"当上一期。这是**语义变化**：
# 同一输入下的同比读数与 1.2.x 可能不同，必须换新身份（run_identity 含 impl），
# 旧 1.2.x 已冻结结果按其自身 impl_version 原样保留可查，绝不原地改写。
IMPL_VERSION = "stock_screen/1.3.0"
# 字段映射版本：字段定义（口径/公式/身份）一变，旧快照/旧结果过期。
FIELD_MAP_VERSION = "1"

# ── 纳入快照的金额字段（wide 行的列 → 观察）；显示标签复用 facts.metric_label ──
AMOUNT_METRICS: tuple[str, ...] = (
    "revenue", "operating_cost", "gross_profit", "net_profit",
    "net_profit_consolidated", "operating_cashflow", "rd_expense",
    "total_assets", "total_liabilities", "accounts_receivable", "inventory",
    "accounts_payable", "admin_expense", "selling_expense", "taxes_and_surcharges",
)

# ── 首版可筛选字段（5 个，全部有真实数据支持；不为凑数猜值）──────────────
OPS: tuple[str, ...] = (">=", "<=", ">", "<")
_AMOUNT_UNITS = ("元", "万", "亿")

# ── 期间政策（R1.1）：统一报告期 / 指定年度 / 逐家最新 ──────────────────────
PERIOD_MODES: tuple[str, ...] = ("uniform", "pinned", "latest_each")
PERIOD_MODE_LABELS = {
    "uniform": "统一报告期（默认，池内最新可用年度，可横比）",
    "pinned": "指定年度（可横比，缺期即资料不足）",
    "latest_each": "逐家最新（不可横比，仅供逐家查看）",
}
# 输入缓存里「年度」的写法（与 financial_analysis.dataset._ANNUAL_TYPES 同一语义）
_ANNUAL_PERIOD_TYPES = ("", "10-K", "年报", "annual")


class PeriodPolicyError(ValueError):
    """期间政策无法给出可比较的评估期（缺共同年度/指定的年度不存在）。"""

    def __init__(self, errors):
        self.errors = list(errors or ())
        super().__init__("；".join(self.errors))


class FieldSpec:
    """一个可筛选字段：口径、单位、公式、身份与分母规则全部显式。"""

    def __init__(self, field_id: str, label: str, kind: str, unit: str, *,
                 metric: str = "", numerator: str = "", denominator: str = "",
                 needs_prev: bool = False, note: str = ""):
        self.field_id = field_id
        self.label = label
        self.kind = kind                # amount | pct
        self.unit = unit                # 元 | %
        self.metric = metric            # 直读字段（amount）
        self.numerator = numerator      # 比率/派生字段
        self.denominator = denominator
        self.needs_prev = needs_prev    # 增速类：需要上一期年度观察
        self.note = note

    def as_dict(self) -> dict:
        return {"field_id": self.field_id, "label": self.label, "kind": self.kind,
                "unit": self.unit, "metric": self.metric, "numerator": self.numerator,
                "denominator": self.denominator, "needs_prev": self.needs_prev,
                "note": self.note}


# 身份声明：net_profit=归母净利润（与 RATIO_MODELS 的 cashflow_coverage 同一口径），
# net_profit_consolidated=合并净利润；两者不得互替。
FIELD_SPECS: dict[str, FieldSpec] = {
    "revenue_growth": FieldSpec(
        "revenue_growth", "营业收入同比增速", "pct", "%",
        metric="revenue", needs_prev=True,
        note="本期与上一期年度营业收入；上一期缺失或非正 → 不适用（不编造增速）"),
    "gross_margin": FieldSpec(
        "gross_margin", "毛利率", "pct", "%",
        numerator="gross_profit", denominator="revenue",
        note="毛利 / 营业收入 × 100；收入非正 → 不适用"),
    "operating_cashflow": FieldSpec(
        "operating_cashflow", "经营活动现金流净额", "amount", "元",
        metric="operating_cashflow",
        note="直读字段；负值是真实读数，不是缺失"),
    "debt_ratio": FieldSpec(
        "debt_ratio", "资产负债率", "pct", "%",
        numerator="total_liabilities", denominator="total_assets",
        note="负债合计 / 资产总计 × 100；资产非正 → 不适用"),
    "cashflow_coverage": FieldSpec(
        "cashflow_coverage", "经营现金流/归母净利润", "pct", "%",
        numerator="operating_cashflow", denominator="net_profit",
        note="分母是归母净利润（net_profit），不用合并净利润顶替；非正 → 不适用"),
}

_FIELD_IDS = tuple(FIELD_SPECS)


# ============================================================
# 计算判据（T0-G1/G2，2026-10-24 试用闭环复核）
# ============================================================
# 为什么单列这一层（复核 G1/G2 的根因是**同一件事**：判据写了两遍）：
#
# - 执行读数要过观察状态、身份（币种/报表范围）、分母符号；预览 `_field_status`
#   只过"字段非空"。于是资产总计=0 的行在预览里是「已覆盖」、执行里是资料不足（G2）；
# - 执行此前按 `manifest.periods` 的**相邻位**取"上一期"，预览按"更早期间里最大的那个"，
#   两边都不是"上一个自然年"：只有 2022 与 2024 也能给出同比 +50%（G1）。
#
# 现在只有一条判据：取值先统一成 `ComputableValue`（元金额 + 状态 + 币种/口径），
# `judge_ratio` / `judge_growth` 是**唯一**判定入口——执行读数（`_ratio_reading` /
# `_growth_reading`）与预览（`screen_prepare._field_status`）都调它们。
# 两边可以取用**不同的证据**（冻结观察 / 宽表行），但不允许有两套判据。

@dataclass(frozen=True)
class ComputableValue:
    """一次判据的输入：某科目在某期间的取值（执行来自 Observation，预览来自宽表行）。"""

    metric: str = ""
    period: str = ""
    state: str = State.MISSING
    yuan: Decimal | None = None          # 已按来源单位换算成元（状态不可用时为 None）
    currency: str = ""
    caliber: str = ""
    unit: str = ""
    note: str = ""

    @property
    def usable(self) -> bool:
        """与 `Observation.usable` 同一条：只有 ok/zero 且能换算成元才参与计算。"""
        return self.state in (State.OK, State.ZERO) and self.yuan is not None

    @property
    def label(self) -> str:
        return metric_label(self.metric) or self.metric

    @property
    def display_amount(self) -> str:
        return _display_amount(self.yuan) if self.yuan is not None else "（无值）"

    def as_dict(self) -> dict:
        return {"metric": self.metric, "period": self.period, "state": self.state,
                "yuan": None if self.yuan is None else format(self.yuan, "f"),
                "currency": self.currency, "caliber": self.caliber,
                "unit": self.unit, "note": self.note}


def adjacent_prior_period(period: str) -> str:
    """严格相邻的**上一个自然年**（保持标签形状）：`2024年`→`2023年`、
    `2024-12-31`→`2023-12-31`；不是年度形态 → 空串（不猜期间、不取列表前一条）。"""
    text = str(period or "").strip()
    if len(text) == 5 and text.endswith("年") and text[:4].isdigit():
        return f"{int(text[:4]) - 1}年"
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if m:
        return f"{int(m.group(1)) - 1}-{m.group(2)}-{m.group(3)}"
    return ""


def value_from_observation(obs, metric: str) -> ComputableValue:
    """冻结观察 → 判据值。不可用就照实标状态，不在这里推断币种/口径。"""
    if obs is None:
        return ComputableValue(metric=metric, state=State.MISSING)
    currency = str(getattr(obs, "currency", "") or "")
    caliber = str(getattr(obs, "caliber", "") or "")
    period = str(getattr(obs, "period", "") or "")
    unit = str(getattr(obs, "unit", "") or "")
    state = str(getattr(obs, "state", "") or State.MISSING)
    note = str(getattr(obs, "note", "") or "")
    if not getattr(obs, "usable", False):
        return ComputableValue(metric=metric, period=period, state=state,
                               currency=currency, caliber=caliber, unit=unit, note=note)
    yuan = Decimal(str(obs.value)) * Decimal(str(obs.money_scale or 1))
    return ComputableValue(metric=metric, period=period,
                           state=(State.ZERO if yuan == 0 else State.OK), yuan=yuan,
                           currency=currency, caliber=caliber, unit=unit, note=note)


def value_from_row(row: dict, metric: str) -> ComputableValue:
    """宽表行 → 判据值（预览侧）。"算不出"的条件与执行一致：缺科目/不是数/
    单位不可换算/币种或报表范围未声明/值为零（零是真实零，仍然参与判据）。"""
    safe = row if isinstance(row, dict) else {}
    period = str(safe.get("period") or "")[:10]
    currency = str(safe.get("currency") or "")
    caliber = str(safe.get("caliber") or "")
    unit = str(safe.get("unit") or "")
    base = {"metric": metric, "period": period, "currency": currency,
            "caliber": caliber, "unit": unit}
    raw = safe.get(metric)
    if raw is None or raw == "":
        return ComputableValue(state=State.MISSING, note="该科目没有值", **base)
    num = _decimal(raw)
    if num is None:
        return ComputableValue(state=State.INVALID, note=f"值「{raw}」不是数", **base)
    scale = unit_scale_of(unit)
    if scale <= 0:
        return ComputableValue(state=State.UNKNOWN,
                               note=f"单位「{unit or '未标'}」不可换算为金额（不猜量纲）", **base)
    missing_meta = [name for name, val in (("币种", currency), ("报表范围", caliber)) if not val]
    if missing_meta:
        return ComputableValue(state=State.UNKNOWN,
                               note=f"关键元数据未知：{'、'.join(missing_meta)}（不默认、不推断）",
                               **base)
    yuan = num * Decimal(str(scale))
    return ComputableValue(state=(State.ZERO if yuan == 0 else State.OK), yuan=yuan, **base)


def unusable_reason(value: ComputableValue, side: str = "") -> str:
    """不可用取值的**具体**原因：缺 / 不是数 / 元数据未知 / 冲突，四类各写各的。

    公开判据的一部分：预览（screen_prepare）也用它写原因，保证两边口径相同。"""
    where = (f"{side}{value.metric}（{value.label}）"
             f"{value.period or '期间未声明'}")
    note = f"：{value.note}" if value.note else ""
    if value.state == State.MISSING:
        return f"缺 {where} 的观察{note}"
    if value.state == State.INVALID:
        return f"{where} 无效{note}（不猜、不补零）"
    if value.state == State.UNKNOWN:
        return f"{where} 观察不可用（unknown）{note}"
    if value.state == State.CONFLICTING:
        return f"{where} 冲突（同一期间多个互不相容的值，不择一）{note}"
    return f"{where} 观察不可用（{value.state}）{note}"


def _identity_clash_reason(left: ComputableValue, right: ComputableValue, *,
                           verb: str) -> str:
    """两个输入的身份是否相容：币种或报表范围不同就点名是哪一项（G1/G4：不换算、不猜）。"""
    for field, name in (("currency", "币种"), ("caliber", "报表范围")):
        a, b = str(getattr(left, field) or ""), str(getattr(right, field) or "")
        if a and b and a != b:
            return (f"{verb}的{name}不一致（{a} vs {b}）：同一次计算只接受同一身份，"
                    "不换算、不猜汇率、不跨报表范围混算")
    return ""


def judge_ratio(numerator: ComputableValue, denominator: ComputableValue, *,
                label: str = "") -> tuple[str, str]:
    """比率是否可算（G2 唯一判据）：两边可用 + 同身份 + 分母为正 → `("ok", "")`。

    返回 `(状态, 原因)`；状态取自 `State` 或 `not_applicable`，与执行读数同一套取值。
    """
    if not numerator.usable:
        return (numerator.state if numerator.state != State.OK else State.MISSING,
                unusable_reason(numerator, "分子"))
    if not denominator.usable:
        return (denominator.state if denominator.state != State.OK else State.MISSING,
                unusable_reason(denominator, "分母"))
    clash = _identity_clash_reason(numerator, denominator, verb="同一次比率计算")
    if clash:
        return State.CONFLICTING, clash
    period = denominator.period or numerator.period
    name = label or f"{numerator.label} / {denominator.label}"
    if denominator.yuan == 0:
        return ("not_applicable",
                f"{name} {period} 的分母为 0：不可算，不编造（{denominator.metric} 是真实零）")
    if denominator.yuan < 0:
        return ("not_applicable",
                f"{denominator.metric}（{denominator.label}）{period} 非正"
                f"（{denominator.display_amount}）：比率不适用，不编造比值")
    return State.OK, ""


def judge_growth(current: ComputableValue, prior: ComputableValue, *,
                 prior_period: str = "") -> tuple[str, str]:
    """同比是否可算（G1 唯一判据）：**只认严格相邻的上一个自然年**。

    `prior` 必须是 `adjacent_prior_period(current.period)` 那一期间的取值。更早的年度
    （2022 对 2024）不是"上一期"，用它算出来的是跨年度增速，必须报「缺上一年度」而不是
    给一个看起来正常的百分比。零/负基数、跨币种、跨报表范围各自给原因。
    """
    expect = prior_period or adjacent_prior_period(current.period)
    if not current.usable:
        return (current.state if current.state != State.OK else State.MISSING,
                 unusable_reason(current, "本期"))
    if not expect:
        return ("not_applicable",
                f"{current.period or '当前期间'} 不是可识别的年度期间：无法确定上一个自然年，"
                "增速不可算（不猜期间）")
    if prior.period and prior.period != expect:
        return ("not_applicable",
                f"{current.period} 的同比只与相邻上一年度 {expect} 比：给来的 {prior.period} "
                "不是相邻年度，不做跨年度同比")
    if not prior.usable:
        if prior.state == State.MISSING:
            hint = f"（{prior.note}）" if prior.note else ""
            return (State.MISSING,
                    f"缺上一年度 {expect} 的{current.label}观察{hint}：同比只与严格相邻的"
                    f"上一自然年比（{current.period} → {expect}），不用更早年度顶替、不前值补齐")
        return (prior.state, unusable_reason(prior, "上一年度"))
    clash = _identity_clash_reason(current, prior, verb="同比两期")
    if clash:
        return State.CONFLICTING, clash
    if prior.yuan <= 0:
        return ("not_applicable",
                f"上一年度（{prior.period}）{prior.label}非正"
                f"（{prior.display_amount}）：增速不适用（不编造比值）")
    return State.OK, ""


# ============================================================
# 小工具
# ============================================================

def _hash(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _iso_date(value: str) -> str:
    """ISO 日期（前 10 位）；无法解析返回空串（不猜）。"""
    s = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        try:
            _dt.date.fromisoformat(s[:10])
            return s[:10]
        except ValueError:
            return ""
    return ""


def iso_date(value: str) -> str:
    """公开别名（预览侧判 `as_of` 形状也用同一条，不各写一套日期解析）。"""
    return _iso_date(value)


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _year_label(period: str) -> str:
    """`2024-12-31` → `2024年`（与既有年度期间标签同一写法）。"""
    return f"{period[:4]}年" if re.match(r"^\d{4}-", period) else ""


# ============================================================
# 输入契约：缓存 / 用户上传（同一 wide 行形状）
# ============================================================

def validate_payload(payload) -> tuple[list[dict], list[str]]:
    """校验输入载荷（缓存与上传同一契约）→ `(行, 错误)`。

    每行必须有 entity/code/period；白名单金额字段至少一个；unit/currency/caliber
    缺失不填默认（观察会按既有规则变 unknown）。值不是数 → 保留行，由 freeze 标 invalid。
    """
    errors: list[str] = []
    if not isinstance(payload, dict):
        return [], ["输入必须是 JSON 对象（{metadata, rows}）"]
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return [], ["rows 必须是非空数组"]
    out: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append(f"rows[{i}] 不是对象")
            continue
        entity = str(row.get("entity") or "").strip()
        code = str(row.get("code") or row.get("entity_id") or "").strip()
        period = str(row.get("period") or "").strip()
        if not entity:
            errors.append(f"rows[{i}] 缺 entity（主体名）")
        if not code:
            errors.append(f"rows[{i}] 缺 code（股票代码，稳定身份）")
        if not re.match(r"^\d{4}-\d{2}-\d{2}", period):
            errors.append(f"rows[{i}] period 必须是 YYYY-MM-DD（期末日），得到「{period}」")
        nums = {k: row[k] for k in AMOUNT_METRICS if k in row and row[k] is not None}
        if not nums:
            errors.append(f"rows[{i}] 没有任何可纳入的金额字段"
                          f"（白名单 {len(AMOUNT_METRICS)} 个之外的不收）")
        out.append(row)
    return out, errors


def load_payload_file(path: str) -> tuple[dict, list[str]]:
    """读一个缓存/上传 JSON 文件 → `(载荷, 错误)`（显式关文件；坏内容照实报）。"""
    name = os.path.basename(str(path or ""))
    try:
        with open(path, encoding="utf-8-sig") as fh:
            payload = json.loads(fh.read())
    except FileNotFoundError:
        return {}, [f"文件不存在：{name}"]
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return {}, [f"文件不是有效 UTF-8 JSON：{exc}"]
    if not isinstance(payload, dict):
        return {}, ["输入必须是 JSON 对象（{metadata, rows}）"]
    rows, errors = validate_payload(payload)
    if errors:
        return {}, errors
    return payload, []


def payload_sha256(path: str) -> str:
    """输入文件字节哈希（进快照身份；读不到返回空串，不代替）。"""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


# ============================================================
# 输入快照：股票池 + 截止日 + 冻结观察（不可变，哈希可复算）
# ============================================================

@dataclass(frozen=True)
class InputSnapshot:
    """一次筛选的输入快照：股票池版本、PIT 过滤后的观察、来源与哈希。**不可变**。"""

    snapshot_id: str
    as_of: str
    pool: tuple[str, ...]               # 请求的代码（规范化，排序后）
    entities: tuple[dict, ...]          # 每主体：{entity, entity_id, dataset, excluded, pending_pit}
    source_label: str
    source_sha256: str
    synthetic: bool
    user_provided: bool
    excluded: tuple[dict, ...]          # PIT 排除 / 界外的行（含原因）
    field_map_version: str = FIELD_MAP_VERSION

    @property
    def entity_ids(self) -> tuple[str, ...]:
        return tuple(e["entity_id"] for e in self.entities)

    def entity(self, entity_id: str):
        return next((e for e in self.entities if e["entity_id"] == entity_id), None)

    @property
    def pending_rows(self) -> tuple[dict, ...]:
        return tuple(p for e in self.entities for p in e["pending_pit"])

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA_VERSION, "impl_version": IMPL_VERSION,
            "snapshot_id": self.snapshot_id, "as_of": self.as_of,
            "pool": list(self.pool), "source_label": self.source_label,
            "source_sha256": self.source_sha256, "synthetic": self.synthetic,
            "user_provided": self.user_provided,
            "field_map_version": self.field_map_version,
            "excluded": [dict(x) for x in self.excluded],
            "entities": [
                {"entity": e["entity"], "entity_id": e["entity_id"],
                 "dataset": e["dataset"].as_dict(),
                 "excluded": [dict(x) for x in e["excluded"]],
                 "pending_pit": [dict(x) for x in e["pending_pit"]]}
                for e in self.entities
            ],
        }


def _entity_market(code: str) -> tuple[str, str]:
    """`(market, 界外原因)`：非 cn 市场或市场不明的裸代码 → 界外/不确认，不静默纳入。

    既有约定（facts.market_of_code）：裸 6 位数字 = cn（无歧义）；1–5 位裸数字有歧义；
    港股/美股后缀非 cn。本版选股只收 A 股（cn）。
    """
    market, ambiguous = market_of_code(code)
    if ambiguous:
        return "", "裸代码市场不明（请用带交易所后缀的代码，如 002304.SZ）"
    if market != "cn":
        return "", f"市场「{market or '未知'}」不是 A 股：本版只支持 A 股主体"
    return "cn", ""


# 期间类型归一：输入缓存用「年度」，数据集判定用「年报」（`dataset._ANNUAL_TYPES`）。
# 只归一"年度报告"这一个语义，半年报/季报原样保留（会被年度身份判定挡下，不冒充年报）。
_ANNUAL_WORDS = {"年度", "年报", "年度报告", "annual", "10-K", "年报（合并）"}
_OTHER_PERIOD_WORDS = {"半年度", "半年报", "中报", "中期", "季度", "季报", "一季报",
                       "三季报", "half_year", "q1", "q2", "q3", "q4", "h1"}


def _period_type_of(row: dict) -> str:
    raw = str(row.get("period_type") or "").strip()
    if raw in _ANNUAL_WORDS:
        return "年报"
    if raw in _OTHER_PERIOD_WORDS:
        return raw                      # 照实保留，让年度身份判定拒绝
    return raw or "年报"


def _wide_to_long(row: dict, *, entity_id: str) -> list[dict]:
    """一行 wide 记录 → 多条长表观察行（每指标一条，fact_id 走既有稳定 ID 约定）。"""
    period = str(row.get("period") or "")[:10]
    year = period[:4]
    period_start = f"{year}-01-01" if re.match(r"^\d{4}-\d{2}-\d{2}$", period) else ""
    caliber = str(row.get("caliber") or "")
    currency = str(row.get("currency") or "")
    unit = str(row.get("unit") or "")
    entity = str(row.get("entity") or "")
    label = _year_label(period)
    period_type = _period_type_of(row)
    out: list[dict] = []
    for metric in AMOUNT_METRICS:
        if metric not in row or row[metric] is None:
            continue
        role = period_role_of(metric)          # flow / stock（未声明指标的期间角色未知 → 不收）
        if not role:
            continue
        out.append({
            "fact_id": make_fact_id(entity_id, entity, metric, label, caliber),
            "metric": metric,
            "metric_label": metric_label(metric),
            "period": label,
            "period_start": period_start,
            "period_end": period,
            "period_type": period_type,
            "period_kind": role,
            "period_label": period,
            "value": row[metric],
            "unit": unit,
            "currency": currency,
            "caliber": caliber,
            "entity": entity,
            "entity_id": entity_id,
            "market": "cn",
            "as_of": "",
            "source_url": str(row.get("source_url") or ""),
            "source_hash": str(row.get("source_hash") or ""),
            "verify_state": str(row.get("verify_state") or ""),
            "restatement": str(row.get("restatement") or ""),
        })
    return out


def row_pit_state(row: dict, as_of: str) -> tuple[str, str, str]:
    """一行的 PIT 判定（G2 配套：预览与执行**同一条**穿越判据）。

    返回 `(状态, 原因, available_at)`：`ok` 纳入；`pending`（缺可用时点）待核验，
    不参与筛选；`excluded`（晚于截止日/期末未到）不纳入。判据与 `build_snapshot`
    的行过滤是同一份（available_at 优先，退 disclosed_at，都缺 = 待核验）。
    """
    period = str((row or {}).get("period") or "")[:10]
    avail = _iso_date((row or {}).get("available_at")
                      or (row or {}).get("disclosed_at") or "")
    as_of_d = _iso_date(as_of)
    if not as_of_d:
        return "", "as_of 必须是 YYYY-MM-DD（资料截止日）", avail
    if not avail:
        return ("pending",
                "缺 available_at/disclosed_at：无法证明资料在截止日时可得"
                "（待核验，不参与筛选）", avail)
    if avail > as_of_d:
        return ("excluded",
                f"资料可用时点 {avail} 晚于截止日 {as_of_d}：不纳入（不倒灌）", avail)
    if period > as_of_d:
        return ("excluded",
                f"报告期期末 {period} 晚于截止日 {as_of_d}：期间尚未披露", avail)
    return "ok", "", avail


def build_snapshot(payload, *, pool, as_of: str, source_label: str = "",
                   source_sha256: str = "", synthetic: bool = False,
                   user_provided: bool = False) -> tuple[InputSnapshot | None, list[str]]:
    """构建输入快照：池过滤 → PIT 过滤 → 逐主体冻结（复用 `dataset.freeze`）。

    返回 `(snapshot, errors)`；契约错误（池空/截止日缺/全部界外）→ `(None, errors)`。
    每个主体一个冻结数据集（数据集身份 = 主体级 `dataset_hash`），池身份 = 各数据集
    哈希 + 池 + 截止日 + 字段映射版本 + 输入字节哈希。
    """
    errors: list[str] = []
    rows, verr = validate_payload(payload)
    errors.extend(verr)
    as_of_d = _iso_date(as_of)
    if not as_of_d:
        errors.append("as_of 必须是 YYYY-MM-DD（资料截止日）")
    if errors:
        return None, errors

    wanted: list[str] = []
    for raw_code in (pool or []):
        code = str(raw_code or "").strip().upper()
        if code and code not in wanted:
            wanted.append(code)
    if not wanted:
        return None, ["股票池为空：请给出至少一个股票代码（如 002304.SZ）"]

    by_code: dict[str, list[dict]] = {}
    out_of_scope: list[dict] = []
    for row in rows:
        code = str(row.get("code") or row.get("entity_id") or "").strip().upper()
        _market, why = _entity_market(code)
        if why:
            out_of_scope.append({"code": code, "entity": row.get("entity") or "",
                                 "period": str(row.get("period") or "")[:10],
                                 "reason": why})
            continue
        by_code.setdefault(code, []).append(row)

    missing_codes: list[str] = []
    entities: list[dict] = []
    excluded_all: list[dict] = list(out_of_scope)
    dataset_hashes: list[str] = []
    for code in sorted(wanted):
        if code not in by_code:
            missing_codes.append(code)
            continue
        ent_rows = by_code[code]
        kept_long: list[dict] = []
        excluded_e: list[dict] = []
        pending_e: list[dict] = []
        for row in ent_rows:
            period = str(row.get("period") or "")[:10]
            # PIT 判据只有一条（row_pit_state）：执行行过滤与预览共用，不各写一套
            state, reason, avail = row_pit_state(row, as_of_d)
            if state == "pending":
                pending_e.append({"code": code, "period": period, "reason": reason})
                continue
            if state == "excluded":
                excluded_e.append({"code": code, "period": period,
                                   "available_at": avail, "reason": reason})
                continue
            kept_long.extend(_wide_to_long(row, entity_id=code))
        entity_name = str(ent_rows[0].get("entity") or code)
        dataset = freeze(
            kept_long, entity=entity_name, entity_id=code, market="cn",
            as_of=as_of_d, source_label=source_label or "screen_input",
        )
        dataset_hashes.append(dataset.dataset_hash)
        excluded_all.extend(excluded_e)
        entities.append({
            "entity": entity_name, "entity_id": code, "dataset": dataset,
            "excluded": tuple(excluded_e), "pending_pit": tuple(pending_e),
        })

    if not entities:
        for item in out_of_scope:
            errors.append(f"{item['code']} {item['period']}：{item['reason']}")
        if missing_codes:
            errors.append(f"输入里没有这些主体的观察：{'、'.join(missing_codes)}")
        if not errors:
            errors.append("股票池内没有任何可纳入主体")
        errors.append("股票池内没有任何可纳入主体（全部界外或输入里无观察）")
        return None, errors

    snapshot_id = _hash({
        "schema": SCHEMA_VERSION, "impl": IMPL_VERSION,
        "field_map_version": FIELD_MAP_VERSION,
        "pool": sorted(wanted), "as_of": as_of_d,
        "source_sha256": source_sha256, "datasets": sorted(dataset_hashes),
    })
    return InputSnapshot(
        snapshot_id=snapshot_id, as_of=as_of_d, pool=tuple(sorted(wanted)),
        entities=tuple(entities), source_label=source_label,
        source_sha256=source_sha256, synthetic=synthetic,
        user_provided=user_provided, excluded=tuple(excluded_all),
    ), errors


def describe_snapshot(snapshot: InputSnapshot) -> dict:
    """输入预览（R1-A「对输入做预览」）：主体、期间、口径、单位、可用时点、缺口。"""
    entities: list[dict] = []
    for ent in snapshot.entities:
        ds = ent["dataset"]
        obs = [o for o in ds.observations if o.usable]
        avails = sorted({o.as_of for o in obs if o.as_of})
        metrics = sorted({o.metric for o in obs})
        entities.append({
            "entity": ent["entity"], "entity_id": ent["entity_id"],
            "periods": list(ds.manifest.periods),
            "caliber": ds.manifest.caliber or "",
            "currency": sorted({o.currency for o in obs if o.currency}),
            "units": sorted({o.unit for o in obs if o.unit}),
            "available_at": avails,
            "observations": len(obs),
            "metrics": metrics,
            "dataset_hash": ds.dataset_hash,
            "gaps": list(ds.manifest.gaps)[:8],
            "conflicts": list(ds.manifest.conflicts)[:8],
            "excluded_rows": len(ent["excluded"]),
            "pending_rows": len(ent["pending_pit"]),
        })
    return {
        "schema": SCHEMA_VERSION, "impl_version": IMPL_VERSION,
        "snapshot_id": snapshot.snapshot_id, "as_of": snapshot.as_of,
        "pool": list(snapshot.pool), "source_label": snapshot.source_label,
        "source_sha256": snapshot.source_sha256,
        "synthetic": snapshot.synthetic, "user_provided": snapshot.user_provided,
        "field_map_version": snapshot.field_map_version,
        "entities": entities,
        "excluded": [dict(x) for x in snapshot.excluded],
        "pending": [dict(x) for x in snapshot.pending_rows],
        "fields": {fid: FIELD_SPECS[fid].as_dict() for fid in _FIELD_IDS},
    }


# ============================================================
# 规则与读数
# ============================================================

@dataclass(frozen=True)
class Rule:
    """一条筛选条件：字段 + 比较符 + 阈值（金额带单位）。"""

    field: str
    op: str
    value: float
    unit: str = ""                  # amount 字段可带单位（元/万/亿）；pct 字段必须空

    def as_dict(self) -> dict:
        return {"field": self.field, "op": self.op, "value": self.value,
                "unit": self.unit}


def parse_rules(raw_rules) -> tuple[tuple[Rule, ...], list[str]]:
    """校验规则列表：未知字段/比较符不自动替换（含义相近也不行），错误逐条列出。"""
    errors: list[str] = []
    rules: list[Rule] = []
    for i, raw in enumerate(raw_rules or []):
        if not isinstance(raw, dict):
            errors.append(f"rules[{i}] 不是对象")
            continue
        fid = str(raw.get("field") or "").strip()
        if fid not in FIELD_SPECS:
            errors.append(f"rules[{i}] 未知字段「{fid}」：可选 {', '.join(_FIELD_IDS)}"
                          "（不自动替换含义相近字段）")
            continue
        op = str(raw.get("op") or "").strip()
        if op not in OPS:
            errors.append(f"rules[{i}] 不支持的比较符「{op}」：可选 {', '.join(OPS)}")
            continue
        dec = _decimal(raw.get("value"))
        if dec is None:
            errors.append(f"rules[{i}] 阈值不是数：{raw.get('value')!r}")
            continue
        spec = FIELD_SPECS[fid]
        unit = str(raw.get("unit") or "").strip()
        if spec.kind == "amount":
            if unit and unit not in _AMOUNT_UNITS:
                errors.append(f"rules[{i}] 金额单位「{unit}」不支持：可选 {'、'.join(_AMOUNT_UNITS)}")
                continue
            rules.append(Rule(fid, op, float(dec), unit or "元"))
        else:
            if unit:
                errors.append(f"rules[{i}] 百分比字段的阈值不带单位（单位固定 %）")
                continue
            rules.append(Rule(fid, op, float(dec)))
    if not rules and not errors:
        errors.append("至少需要一条筛选条件")
    return tuple(rules), errors


def rules_hash(rules: tuple[Rule, ...]) -> str:
    return _hash({"schema": SCHEMA_VERSION, "rules": [r.as_dict() for r in rules]})


@dataclass(frozen=True)
class Reading:
    """一个字段在一次评估里的读数（可追溯：输入 fact_id + 公式 + 状态）。"""

    field: str
    label: str
    status: str                     # ok | missing | conflicting | not_applicable | pending_pit
    value: float | None = None      # 原始精度（比较用这个，不用显示值）
    unit: str = ""
    display: str = ""               # 显示值（量化后的字符串，仅展示）
    formula: str = ""
    reason: str = ""
    inputs: tuple[str, ...] = ()    # 输入观察的 fact_id
    source_url: str = ""

    def as_dict(self) -> dict:
        return {"field": self.field, "label": self.label, "status": self.status,
                "value": self.value, "unit": self.unit, "display": self.display,
                "formula": self.formula, "reason": self.reason,
                "inputs": list(self.inputs), "source_url": self.source_url}


@dataclass(frozen=True)
class EntityRow:
    """一个主体在一次筛选里的结果（主判据一态，原因可多条）。"""

    entity: str
    entity_id: str
    verdict: str                    # pass | fail | insufficient
    eval_period: str
    readings: tuple[Reading, ...]
    reasons: tuple[str, ...]
    dataset_hash: str

    def as_dict(self) -> dict:
        return {"entity": self.entity, "entity_id": self.entity_id,
                "verdict": self.verdict, "eval_period": self.eval_period,
                "readings": [r.as_dict() for r in self.readings],
                "reasons": list(self.reasons), "dataset_hash": self.dataset_hash}


def _display_amount(amount_yuan: Decimal) -> str:
    """金额显示：≥1 万用亿（两位小数），否则元。只影响显示。"""
    if abs(amount_yuan) >= Decimal("10000"):
        return f"{(amount_yuan / Decimal('1e8')).quantize(Decimal('0.01'))} 亿"
    return f"{amount_yuan.quantize(Decimal('0.01'))} 元"


def _amount_reading(spec: FieldSpec, dataset, period: str) -> Reading:
    obs = dataset.get(spec.metric, period)
    if obs is None:
        cals = dataset.calibers_of(spec.metric, period)
        why = f"缺 {spec.metric}（{metric_label(spec.metric)}）{period} 的观察"
        if cals and cals != ["未标"]:
            why += f"；该期间出现过的口径：{'、'.join(cals)}"
        return Reading(spec.field_id, spec.label, State.MISSING, reason=why)
    if not obs.usable:
        return Reading(spec.field_id, spec.label, obs.state,
                       reason=f"观察不可用（{obs.state}）：{obs.note or spec.metric}")
    amount_yuan = Decimal(str(obs.value)) * Decimal(str(obs.money_scale or 1))
    return Reading(spec.field_id, spec.label, State.OK, value=float(amount_yuan),
                   unit="元", display=_display_amount(amount_yuan),
                   formula=(f"{metric_label(spec.metric)}（{period}，原始 "
                            f"{obs.value} {obs.unit or '单位未知'}）"),
                   reason=(obs.note if obs.state == State.ZERO else ""),
                   inputs=(obs.fact_id,), source_url=obs.source_url)


def _caliber_hint(dataset, value: ComputableValue, period: str) -> ComputableValue:
    """缺观察时补一条「该期间还出现过哪些口径」的诊断（不顶替、只给线索）。"""
    if value.state != State.MISSING:
        return value
    cals = dataset.calibers_of(value.metric, period)
    if not cals or cals == ["未标"]:
        return value
    return dataclasses.replace(value, note=f"该期间出现过的口径：{'、'.join(cals)}（跨口径不代替）")


def _ratio_reading(spec: FieldSpec, dataset, period: str) -> Reading:
    num_obs = dataset.get(spec.numerator, period)
    den_obs = dataset.get(spec.denominator, period)
    num = _caliber_hint(dataset, value_from_observation(num_obs, spec.numerator), period)
    den = _caliber_hint(dataset, value_from_observation(den_obs, spec.denominator), period)
    num = dataclasses.replace(num, period=num.period or period)
    den = dataclasses.replace(den, period=den.period or period)
    # G2：判据只有一条（judge_ratio）。这里不再自带"分母"检查的第二套写法。
    status, reason = judge_ratio(num, den, label=spec.label)
    if status != State.OK:
        return Reading(spec.field_id, spec.label, status, reason=reason)
    try:
        got = profit_bridge.compute_ratio(dataset, spec.numerator, spec.denominator,
                                          period, label=spec.label)
    except MissingInput as exc:                 # 兜底：判据已过，仍缺则照实报
        return Reading(spec.field_id, spec.label, State.MISSING, reason=str(exc))
    except NotApplicable as exc:
        return Reading(spec.field_id, spec.label, State.CONFLICTING, reason=str(exc))
    except NotComputable as exc:
        return Reading(spec.field_id, spec.label, "not_applicable", reason=str(exc))
    except Exception as exc:        # noqa: BLE001 - 未知异常也要如实进缺口，不静默
        return Reading(spec.field_id, spec.label, State.MISSING,
                       reason=f"比率计算失败：{exc}")
    # 比较用**未量化**原始精度；display 才量化（compute_ratio 的 0.01 量化仅用于其自有输出）
    raw = num.yuan / den.yuan * 100
    return Reading(spec.field_id, spec.label, State.OK, value=float(raw), unit="%",
                   display=f"{raw.quantize(Decimal('0.01'))}%",
                   formula=str(got["formula"]), inputs=tuple(got["inputs"]),
                   source_url=(num_obs.source_url if num_obs else "")
                               or (den_obs.source_url if den_obs else ""))


def _growth_reading(spec: FieldSpec, dataset, period: str) -> Reading:
    """同比增速：**只取严格相邻的上一个自然年**（G1，2026-10-24 复核）。

    此前按 `manifest.periods` 的相邻位取"上一期"：只有 2022 与 2024 两期时，2024 的
    "上一期"被当成 2022，于是算出 (150−100)/100 = +50% 的跨年度假同比（复核反例）。
    现在 2024年 的上一期**只能是** 2023年：那一年没有可用观察就报「缺上一年度」，
    不回退到更早年度、不用前值补齐、不跨币种/跨报表范围比。
    """
    prior_period = adjacent_prior_period(period)
    cur_obs = dataset.get("revenue", period)
    prior_obs = dataset.get("revenue", prior_period) if prior_period else None
    cur = _caliber_hint(dataset, value_from_observation(cur_obs, "revenue"), period)
    cur = dataclasses.replace(cur, period=cur.period or period)
    if prior_period:
        prior = _caliber_hint(dataset, value_from_observation(prior_obs, "revenue"),
                              prior_period)
        prior = dataclasses.replace(prior, period=prior.period or prior_period)
    else:
        prior = ComputableValue(metric="revenue", state=State.MISSING)
    status, reason = judge_growth(cur, prior, prior_period=prior_period)
    if status != State.OK:
        return Reading(spec.field_id, spec.label, status, reason=reason)
    prev_d = prior.yuan
    raw = (cur.yuan - prev_d) / prev_d * 100
    return Reading(spec.field_id, spec.label, State.OK, value=float(raw), unit="%",
                   display=f"{raw.quantize(Decimal('0.01'))}%",
                   formula=(f"({cur_obs.value}{cur_obs.unit}（{period}） − "
                            f"{prior_obs.value}{prior_obs.unit}（{prior_period}）) / "
                            f"{prior_obs.value}{prior_obs.unit}（{prior_period}） × 100"),
                   inputs=(cur_obs.fact_id, prior_obs.fact_id),
                   source_url=cur_obs.source_url or prior_obs.source_url)


def _reading(spec: FieldSpec, dataset, period: str) -> Reading:
    if spec.kind == "amount":
        return _amount_reading(spec, dataset, period)
    if spec.needs_prev:
        return _growth_reading(spec, dataset, period)
    return _ratio_reading(spec, dataset, period)


_CMP = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}
_OP_TEXT = {">=": "≥", "<=": "≤", ">": ">", "<": "<"}


def _compare(reading: Reading, rule: Rule, spec: FieldSpec) -> tuple[bool | None, str]:
    """读数 × 规则 → `(通过?, 说明)`；读数不可用 → `(None, 原因)`（不参与判定）。"""
    if reading.status != State.OK:
        return None, reading.reason or f"{spec.label} 不可用（{reading.status}）"
    val = Decimal(str(reading.value))
    threshold = Decimal(str(rule.value))
    if spec.kind == "amount" and rule.unit and rule.unit != "元":
        threshold = threshold * Decimal(str(unit_scale_of(rule.unit)))
    ok = _CMP[rule.op](val, threshold)
    shown = rule.value if spec.kind == "amount" else f"{Decimal(str(rule.value)).normalize():f}"
    unit_shown = (rule.unit or "元") if spec.kind == "amount" else "%"
    return ok, (f"{spec.label} {reading.display} {_OP_TEXT[rule.op]} {shown}{unit_shown}"
                f"：{'满足' if ok else '不满足'}")


# ============================================================
# 评估：确定性筛选（AND 条件，平局稳定排序）
# ============================================================

@dataclass(frozen=True)
class ScreenResult:
    """一次筛选的结果：三态分行、读数、原因、身份哈希。**不可变**。"""

    schema: str
    impl_version: str
    as_of: str
    pool: tuple[str, ...]
    rules: tuple[Rule, ...]
    rules_hash: str
    snapshot_id: str
    result_hash: str
    pass_rows: tuple[EntityRow, ...]
    fail_rows: tuple[EntityRow, ...]
    insufficient_rows: tuple[EntityRow, ...]
    notes: tuple[str, ...]
    period_policy: dict = dataclasses.field(
        default_factory=lambda: {"mode": "uniform", "report_period": ""})
    report_period: str = ""
    comparable: bool = True

    @property
    def counts(self) -> dict:
        return {"pass": len(self.pass_rows), "fail": len(self.fail_rows),
                "insufficient": len(self.insufficient_rows),
                "total": len(self.pass_rows) + len(self.fail_rows)
                + len(self.insufficient_rows)}

    def to_dict(self) -> dict:
        return {
            "schema": self.schema, "impl_version": self.impl_version,
            "as_of": self.as_of, "pool": list(self.pool),
            "rules": [r.as_dict() for r in self.rules],
            "rules_hash": self.rules_hash, "snapshot_id": self.snapshot_id,
            "result_hash": self.result_hash,
            "period_policy": dict(self.period_policy),
            "report_period": self.report_period,
            "comparable": self.comparable,
            "counts": self.counts, "notes": list(self.notes),
            "pass_rows": [r.as_dict() for r in self.pass_rows],
            "fail_rows": [r.as_dict() for r in self.fail_rows],
            "insufficient_rows": [r.as_dict() for r in self.insufficient_rows],
        }


def normalize_period(value) -> str:
    """`2024` / `2024年` → `2024年`；其他 → 空串（不猜期间）。"""
    text = str(value or "").strip()
    if len(text) == 5 and text.endswith("年") and text[:4].isdigit():
        return text
    if text.isdigit() and len(text) == 4:
        return f"{text}年"
    return ""


def validate_period_policy(raw) -> tuple[dict, list[str]]:
    """期间政策规范化：`{mode, report_period}`；未知模式/多余字段明确报错（不静默纠正）。"""
    if raw is None or raw == "" or raw == {}:
        return {"mode": "uniform", "report_period": ""}, []
    if not isinstance(raw, dict):
        return {}, ["period_policy 必须是对象 {mode, report_period?}"]
    mode = str(raw.get("mode") or "uniform").strip()
    if mode not in PERIOD_MODES:
        return {}, [f"未知期间政策「{mode}」：可选 {'、'.join(PERIOD_MODES)}"]
    pin_raw = str(raw.get("report_period") or "").strip()
    if mode == "uniform":
        if pin_raw:
            return {}, ["统一报告期由系统取池内最新可用年度；要固定某一年度请用「指定年度」"]
        return {"mode": "uniform", "report_period": ""}, []
    if mode == "latest_each":
        if pin_raw:
            return {}, ["逐家最新模式不接受 report_period（每家取自己的最新年度）"]
        return {"mode": "latest_each", "report_period": ""}, []
    pin = normalize_period(pin_raw)
    if not pin:
        return {}, [f"pinned 模式需要 report_period（如 2024 或 2024年），得到「{pin_raw}」"]
    return {"mode": "pinned", "report_period": pin}, []


def annual_periods_of(dataset) -> list[str]:
    """数据集中真实存在的**年度期间**（升序）。

    刻意从 `observations` 取而不是只取 `manifest.periods`：后者只含"可用"观察覆盖的年度，
    某年全部观测都冲突时会从清单里消失——那正是"冲突被当成没有数据、然后悄悄换到上一年"
    的入口。期间存在就要保留，冲突由读数状态表达（不换年）。
    """
    out: list[str] = []
    for obs in getattr(dataset, "observations", ()) or ():
        if str(getattr(obs, "period_type", "") or "") not in _ANNUAL_PERIOD_TYPES:
            continue
        period = str(getattr(obs, "period", "") or "")
        if period.endswith("年") and period[:-1].isdigit() and period not in out:
            out.append(period)
    out.sort()
    return out


def common_periods(snapshot: InputSnapshot) -> list[str]:
    """全池共同年度期间（交集，升序）：统一报告期的候选。"""
    common: set[str] | None = None
    for ent in snapshot.entities:
        periods = set(annual_periods_of(ent["dataset"]))
        common = periods if common is None else (common & periods)
    return sorted(common or ())


def evaluate(snapshot: InputSnapshot, rules: tuple[Rule, ...], *,
             period_policy=None) -> ScreenResult:
    """确定性筛选：期间政策 → 逐主体读数 → AND 判定 → 三态。

    - 期间政策进身份（`period_policy`/`report_period` 进 `result_hash`）；缺期或冲突 →
      资料不足并说明原因，**绝不回退到更早年度**把它混进通过列表；
    - 缺数据/冲突/不适用/待核验 → 资料不足（不填零、不编造）；
    - 全部读数可用且有规则不满足 → 未通过；
    - 行按代码稳定排序（展示用排序由调用方在**读**时施加，不进结果身份）。
    """
    policy, policy_errors = validate_period_policy(period_policy)
    if policy_errors:
        raise PeriodPolicyError(policy_errors)
    mode = policy["mode"]
    cands = {ent["entity_id"]: annual_periods_of(ent["dataset"])
             for ent in snapshot.entities}
    common = common_periods(snapshot)
    if mode == "uniform":
        # 统一报告期 = 池内**最新**的可用年度报告期；没有该期的主体一律「资料不足」，
        # 而不是各取各家最新年度混进同一张通过列表（那是不可横比的）。
        report = max((per[-1] for per in cands.values() if per), default="")
    elif mode == "pinned":
        report = policy["report_period"]
        if not any(report in per for per in cands.values()):
            allp = sorted({p for per in cands.values() for p in per})
            detail = "；".join(f"{eid}：{'、'.join(per) or '无年度期间'}"
                               for eid, per in sorted(cands.items()))
            raise PeriodPolicyError([
                f"没有任何主体有 {report} 的年度观察"
                f"（可选期间：{'、'.join(allp) or '无'}）",
                f"各主体可用期间：{detail}",
                "指定年度不回退到其他年度；如只想按各家最新比较请改用「逐家最新」（不可横比）",
            ])
    else:
        report = ""

    rows: list[EntityRow] = []
    for ent in snapshot.entities:
        dataset = ent["dataset"]
        eid = ent["entity_id"]
        periods = cands.get(eid) or []
        if mode == "latest_each":
            eval_p = periods[-1] if periods else ""
        else:
            eval_p = report if (report and report in periods) else ""
        readings: list[Reading] = []
        reasons: list[str] = []
        unavailable = False
        failed = False
        if not eval_p:
            unavailable = True
            if not report:
                reasons.append("统一报告期无法确定：池内在截止日前没有任何可用的年度报告期"
                               "（不猜期间、不回退到更早年度）")
            elif mode == "latest_each":
                reasons.append("该主体没有任何年度期间观察：无法评估（不借用别的期间）")
            elif mode == "pinned":
                reasons.append(f"无 {report} 的年度观察：资料不足（不回退到其他年度）")
            else:
                reasons.append(f"统一报告期为 {report}；该主体没有 {report} 的可用观察："
                               "资料不足（不回退到其他年度）")
        else:
            for rule in rules:
                spec = FIELD_SPECS[rule.field]
                reading = _reading(spec, dataset, eval_p)
                readings.append(reading)
                ok, detail = _compare(reading, rule, spec)
                if ok is None:
                    unavailable = True
                    reasons.append(detail)
                elif not ok:
                    failed = True
                    reasons.append(detail)
            if unavailable and dataset.manifest.conflicts:
                for item in list(dataset.manifest.conflicts)[:2]:
                    reasons.append(f"输入冲突：{item}")
                reasons.append(f"报告期 {eval_p} 的读数不可用：不回退到更早期间"
                               f"（期间政策 {mode}）")
        for pending in ent["pending_pit"]:
            if eval_p and str(pending.get("period") or "")[:4] == eval_p[:4]:
                reasons.append(f"{pending['period']} 行：{pending['reason']}")
                unavailable = True
        if unavailable or not readings:
            verdict = "insufficient"
        elif failed:
            verdict = "fail"
        else:
            verdict = "pass"
        if not reasons:
            # 通过也要带阈值：只写「毛利率 71.60%」看不出比的是 ≥70% 还是 ≥20%
            parts = []
            for rule, reading in zip(rules, readings):
                _ok, detail = _compare(reading, rule, FIELD_SPECS[rule.field])
                parts.append(detail.replace("：满足", ""))
            reasons.append("；".join(parts) + "：满足全部条件")
        rows.append(EntityRow(
            entity=ent["entity"], entity_id=eid, verdict=verdict,
            eval_period=eval_p, readings=tuple(readings), reasons=tuple(reasons),
            dataset_hash=dataset.dataset_hash))
    # 池里声明了但输入里没有观察的代码 → 资料不足（不静默丢弃）
    present = {row.entity_id for row in rows}
    for code in snapshot.pool:
        if code not in present:
            rows.append(EntityRow(entity=code, entity_id=code, verdict="insufficient",
                                  eval_period="", readings=(),
                                  reasons=("输入里没有该主体的观察：不编造读数",),
                                  dataset_hash=""))
    # 结果内部顺序固定按代码（展示排序在读取时施加，不进身份、不进哈希）
    rows.sort(key=lambda row: row.entity_id)

    comparable = mode != "latest_each"
    notes: list[str] = []
    if mode == "uniform":
        notes.append(f"统一报告期：{report or '（无法确定：池内没有可用的年度报告期）'}"
                     f"（取池内最新可用年度，缺该期的主体列为资料不足）")
        missing = sorted(eid for eid, per in cands.items() if report and report not in per)
        if missing:
            notes.append(f"{len(missing)} 家主体没有 {report} 的可用年度观察，列为资料不足："
                         + "、".join(missing)
                         + (f"；池内共同年度期间为 {'、'.join(common) or '无'}，"
                            "如只想在共同期间比较请改用「指定年度」" if common else ""))
    elif mode == "pinned":
        notes.append(f"指定报告期：{report}（未回退到其他年度）")
    else:
        notes.append("逐家最新模式：各主体评估期间不同，结果不可直接横比"
                     "（不是同一时点的排行榜）")
    if snapshot.pending_rows:
        notes.append(f"{len(snapshot.pending_rows)} 行输入缺可用时点，待核验（不参与筛选）")
    if snapshot.excluded:
        notes.append(f"{len(snapshot.excluded)} 行输入被 PIT/界外规则排除（详见 excluded）")
    if snapshot.synthetic:
        notes.append("合成演示数据：仅验证算法与页面，不作为真实候选")
    if snapshot.user_provided:
        notes.append("用户提供输入：未独立核验，不自动升级为官方证据")

    rh = rules_hash(rules)
    result_hash = _hash({
        "schema": SCHEMA_VERSION, "impl": IMPL_VERSION,
        "field_map_version": FIELD_MAP_VERSION,
        "snapshot": snapshot.snapshot_id, "rules": rh,
        "period_policy": policy, "report_period": report,
        "rows": [row.as_dict() for row in rows],
    })
    return ScreenResult(
        schema=SCHEMA_VERSION, impl_version=IMPL_VERSION, as_of=snapshot.as_of,
        pool=snapshot.pool, rules=rules, rules_hash=rh,
        snapshot_id=snapshot.snapshot_id, result_hash=result_hash,
        pass_rows=tuple(r for r in rows if r.verdict == "pass"),
        fail_rows=tuple(r for r in rows if r.verdict == "fail"),
        insufficient_rows=tuple(r for r in rows if r.verdict == "insufficient"),
        notes=tuple(notes), period_policy=policy, report_period=report,
        comparable=comparable,
    )


# ============================================================
# 展示排序（R1.1：只是视图，不进身份）
# ============================================================

def validate_sort(raw) -> tuple[dict | None, list[str]]:
    """校验展示排序参数；未知字段/方向明确报错，不静默回退成默认序。"""
    if raw is None or raw == {} or raw == "":
        return None, []
    if not isinstance(raw, dict):
        return None, ["sort 必须是对象 {field, direction}"]
    field = str(raw.get("field") or "").strip()
    direction = str(raw.get("direction") or raw.get("dir") or "").strip().lower()
    errors: list[str] = []
    if field not in FIELD_SPECS:
        errors.append(f"排序字段「{field}」未知：可选 {', '.join(_FIELD_IDS)}")
    if direction not in ("asc", "desc"):
        errors.append(f"排序方向「{direction}」未知：可选 asc、desc")
    if errors:
        return None, errors
    return {"field": field, "direction": direction}, []


def sort_rows(rows, sort: dict | None):
    """按展示排序重排行（缺该字段读数永远排最后；平局按代码）。

    `rows` 可以是 `EntityRow` 或已经 `as_dict()` 的行字典；返回同类型列表。
    """

    def get(obj, key):
        return obj.get(key) if isinstance(obj, dict) else getattr(obj, key)

    items = list(rows)
    if not sort:
        return sorted(items, key=lambda row: get(row, "entity_id"))
    field = str(sort.get("field") or "")
    direction = str(sort.get("direction") or "desc")

    def value_of(row):
        readings = get(row, "readings") or ()
        for reading in readings:
            rf = reading.get("field") if isinstance(reading, dict) else getattr(reading, "field", "")
            if rf != field:
                continue
            status = reading.get("status") if isinstance(reading, dict) else getattr(reading, "status", "")
            value = reading.get("value") if isinstance(reading, dict) else getattr(reading, "value", None)
            if status == State.OK and value is not None:
                return float(value)
            return None
        return None

    with_value = [row for row in items if value_of(row) is not None]
    without = [row for row in items if value_of(row) is None]
    with_value.sort(key=lambda row: get(row, "entity_id"))
    with_value.sort(key=value_of, reverse=(direction == "desc"))
    return with_value + sorted(without, key=lambda row: get(row, "entity_id"))
