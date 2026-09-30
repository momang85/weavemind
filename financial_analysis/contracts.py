# -*- coding: utf-8 -*-
"""最小契约：观察 → 数据集 → 模型规格 → 运行 → 已验证输出。

三条纪律写进类型里，而不是留给调用方自觉：

1. **观察不是"一个数"**：`Observation` 同时带主体/市场/指标/维度/期间/频率/币种/单位/
   合并或母公司/归母或全体/重述批次/来源坐标，以及**状态**——缺失、未知、冲突、无效与
   **真实零**是五个不同的东西（`State`）。
2. **身份可算不可猜**：`observation_hash`（值+单位+期间+来源+重述）与
   `DatasetManifest.dataset_hash` 都是哈希；`fact_id` 刻意不含数值（既有约定），
   所以"改一个数仍沿用旧结果"必须由 hash 拦住。
3. **结果可追溯**：`ValidatedOutput` 逐项带 `output_id`/`run_id`/输入 fact_id/公式/假设/
   限制/独立验证结论；正文与图只消费它，不再从报告文字里反猜数字。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

SCHEMA_VERSION = "weavemind.financial_analysis/1"
# 研究主口径：合并报表。真实年报同时披露合并与母公司，模型必须有一个**显式**的默认口径，
# 否则"同一指标多条观察"会被当成冲突，数据齐备也跑不出模型（见 primary_caliber 注释）。
DEFAULT_CALIBER = "合并"

# 观察状态（互斥；缺失/未知/冲突/无效与真实零分开）
class State:
    OK = "ok"                       # 有值且元数据齐备
    ZERO = "zero"                   # **真实零**（有值，就是 0；不是缺失）
    MISSING = "missing"             # 该指标/期间没有记录
    UNKNOWN = "unknown"             # 有值但关键元数据未知（口径/币种/单位缺）
    CONFLICTING = "conflicting"     # 同一 (指标, 期间, 口径) 多个互不相容的值
    INVALID = "invalid"             # 值不是数 / 期间无法解析

_UNSET = object()


def _money_scale(unit: str) -> float:
    """金额单位量纲（与项目既有口径一致：元=1/万=1e4/亿=1e8）；非金额返回 0。"""
    u = str(unit or "")
    for marker, factor in (("万亿", 1e12), ("千亿", 1e11), ("百亿", 1e10),
                           ("亿", 1e8), ("万", 1e4), ("元", 1.0)):
        if marker in u:
            return factor
    return 0.0


def _canonical(value) -> str:
    """把值规范成字符串参与 hash：浮点用 repr，避免 0.1+0.2 这类尾数差异造成假变更。"""
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return repr(round(value, 10))
    return str(value)


def _hash(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Observation:
    """一条可复算观察（`fact_id` 是稳定语义 ID；数值身份在 `observation_hash` 里）。"""

    fact_id: str
    metric: str
    period: str
    value: float | None = None
    state: str = State.OK
    entity: str = ""
    entity_id: str = ""
    market: str = ""
    metric_label: str = ""
    dimension: str = ""
    currency: str = ""
    unit: str = ""
    caliber: str = ""
    period_type: str = ""
    period_start: str = ""
    period_end: str = ""
    # 存量（stock，余额/时点）还是流量（flow，发生额/区间）：同一 (指标, 期间) 的存量与
    # 流量是两个不同的观察，不能互相当替代（L0-a，2026-09-30 复核 F2）。
    period_kind: str = ""
    period_label: str = ""           # 列头原文（`2024年12月31日` / `期末余额`），不截年
    as_of: str = ""
    restatement: str = ""            # 重述批次/版本（未重述留空）
    source_url: str = ""
    source_hash: str = ""
    # 血缘：派生观察的输入事实 id（如毛利 = 营业收入 − 营业成本）。
    # 此前只有抽取器输出里带着 `derived_from`，冻结成观察时就丢了——"这个数是算出来的"
    # 这件事在数据集与输出里都看不见。现在随观察一起走，并**计入观察 hash**：
    # 血缘是溯源信息，改它就等于换了一份观察，不能让它不参与指纹。
    derived_from: tuple[str, ...] = ()
    formula_version: str = ""
    verify_state: str = ""
    note: str = ""

    @property
    def money_scale(self) -> float:
        return _money_scale(self.unit)

    @property
    def is_amount(self) -> bool:
        return self.money_scale > 0

    @property
    def is_derived(self) -> bool:
        return bool(self.derived_from)

    @property
    def observation_hash(self) -> str:
        return _hash({
            "fact_id": self.fact_id, "metric": self.metric, "period": self.period,
            "value": _canonical(self.value), "unit": self.unit, "currency": self.currency,
            "caliber": self.caliber, "entity_id": self.entity_id,
            "restatement": self.restatement, "source_hash": self.source_hash,
            "derived_from": list(self.derived_from), "formula_version": self.formula_version,
            "state": self.state, "schema": SCHEMA_VERSION,
            # 完整期间身份（L0-a/R1-a）：存量/流量、起止日期与**列头原文**都参与指纹——
            # 同一 (指标, 期间) 的"期末余额"与"本期发生额"、半年列与全年列是不同观察，
            # 改任一项就是换了一份观察（复核：只改 kind/label 必须让身份失效）。
            "period_kind": self.period_kind,
            "period_label": self.period_label,
            "period_start": self.period_start, "period_end": self.period_end,
        })

    @property
    def usable(self) -> bool:
        """能否参与计算：只有 `ok`/`zero` 可以；未知/冲突/缺失/无效一律不可用。"""
        return self.state in (State.OK, State.ZERO)

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["observation_hash"] = self.observation_hash
        out["usable"] = self.usable
        return out


@dataclass(frozen=True)
class DatasetManifest:
    """数据集的"当时我知道什么"：契约、准入状态、覆盖、缺口、快照 hash、使用限制。"""

    dataset_hash: str
    source_label: str
    as_of: str = ""
    entity: str = ""
    entity_id: str = ""
    market: str = ""
    caliber: str = ""
    periods: tuple[str, ...] = ()
    observations: int = 0
    usable: int = 0
    field_coverage: dict = field(default_factory=dict)
    gaps: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    available_models: tuple[str, ...] = ()
    restrictions: str = ""
    schema: str = SCHEMA_VERSION

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class AnalysisDataset:
    """冻结数据集：观察集合 + 清单。**不可变**（变更即新数据集、新 hash）。"""

    manifest: DatasetManifest
    observations: tuple[Observation, ...]
    index: dict = field(default_factory=dict)

    @property
    def dataset_hash(self) -> str:
        return self.manifest.dataset_hash

    @property
    def primary_caliber(self) -> str:
        """本数据集的主口径：有合并就用合并，只有一种口径时才默认那一种，否则 `""`。

        为什么需要它（2026-09-29 A2 实机）：真实年报**同时**披露合并与母公司报表，
        于是同一 (指标, 期间) 天然有多条观察；`get()` 的"多条=冲突，不任选"规则会让
        所有模型都报"缺输入"——数据齐备却跑不出任何模型（三一/洋河都踩到）。
        解决办法不是"随便挑一条"，而是**把口径选择显式化**：主口径优先，
        要别的口径必须显式传 `caliber=…`，且同一口径内仍不允许在两个不同值之间任选。

        **不可用观察也参与判定**（K0-a）：只有一条"口径已声明但元数据不可用"的观察时，
        主口径仍是它声明的那个——否则 `get()` 会返回 `None`，把"观察不可用"错报成
        "缺输入"，两种情况对读者的含义不同。
        """
        cals = [o.caliber for o in self.observations if o.caliber]
        if not cals:
            return ""
        if DEFAULT_CALIBER in cals:
            return DEFAULT_CALIBER
        uniq = sorted(set(cals))
        return uniq[0] if len(uniq) == 1 else ""

    def get(self, metric: str, period: str, *, caliber: str | None = None):
        """取一条观察；**口径不回退**（K0-a，2026-09-29 夜验收反例）。

        规则（两条都是"要么这个口径，要么没有"）：

        - 显式 `caliber` → 只认这一个口径。该口径下唯一一条就返回它（哪怕**不可用**——
          调用方才能如实报"观察不可用"而不是"缺输入"，两者含义不同）；同一口径内多条
          **身份不同**的观察 → `None`（不任选）。**绝不回退到别的口径**：此前显式
          "合并"缺项时会静默返回唯一那条母公司值，于是"合并净利 10/12 + 母公司毛利
          30/40"照样算出 validated 的利润桥（跨报表范围混算）。
        - 未给 `caliber` → 用 `primary_caliber`（默认研究口径，合并优先），**一以贯之**：
          不能逐指标挑"哪个口径有就用哪个"。主口径为空（多口径且没声明默认）时按
          "口径不明确"处理，返回 `None`，由调用方报缺输入。
        """
        want = self.primary_caliber if caliber is None else str(caliber)
        if not want:
            return None
        hits = [o for o in self.observations
                if o.metric == str(metric) and o.period == str(period)
                and o.caliber == want]
        if not hits:
            # 口径**未声明**的观察只作**诊断**返回（它必然是"不可用"，`require` 会拒），
            # 好让调用方能如实说"观察不可用"而不是"缺输入"；**绝不**返回另一个已声明口径
            # 的值——那正是"显式合并回退母公司"的跨报表范围混算（K0-a 反例）。
            und = [o for o in self.observations
                   if o.metric == str(metric) and o.period == str(period)
                   and str(o.caliber or "") in _UNDECLARED_CALIBERS]
            return und[0] if len(und) == 1 and not und[0].usable else None
        if len(hits) == 1:
            return hits[0]
        # 同一口径内多条：只有**身份完全一致**才算同一条观察（否则是冲突，不任选）
        hashes = {o.observation_hash for o in hits}
        return hits[0] if len(hashes) == 1 else None

    def calibers_of(self, metric: str, period: str) -> list[str]:
        """该 (指标, 期间) 出现在哪些口径下（缺输入时给读者一条可行动线索）。"""
        return sorted({str(o.caliber or "") or "未标"
                       for o in self.observations
                       if o.metric == str(metric) and o.period == str(period)})

    def require(self, metric: str, period: str, *, caliber: str | None = None) -> Observation:
        obs = self.get(metric, period, caliber=caliber)
        if obs is None:
            _want = self.primary_caliber if caliber is None else str(caliber)
            _others = [c for c in self.calibers_of(metric, period)
                       if c != (_want or "未标")]
            _hint = (f"；该 (指标, 期间) 另有其它口径（{'、'.join(_others)}）——"
                     "跨口径不得代替" if _others else "")
            raise MissingInput(f"缺少观察：{metric} {period}"
                               + (f"（口径 {caliber}）" if caliber else "")
                               + _hint)
        if not obs.usable:
            raise MissingInput(f"观察不可用（{obs.state}）：{metric} {period}" + (
                f"：{obs.note}" if obs.note else ""))
        return obs

    def periods_of(self, metric: str) -> tuple[str, ...]:
        return tuple(o.period for o in self.observations if o.metric == str(metric))

    def period_at(self, offset: int = 0) -> str:
        """期间取用：`offset=0` = **最新一期**，`-1` = 上一期（按清单声明顺序，不猜字符串）。"""
        periods = [p for p in (self.manifest.periods or ()) if p]
        idx = len(periods) - 1 + int(offset)
        return periods[idx] if 0 <= idx < len(periods) else ""

    def as_dict(self) -> dict:
        return {"manifest": self.manifest.as_dict(),
                "observations": [o.as_dict() for o in self.observations]}


class MissingInput(RuntimeError):
    """输入缺失/不可用：**停止该分析**，输出补料清单，不用别的数顶上。"""


# 未声明口径的两种写法：空串与抽取层给的 "unknown"。
_UNDECLARED_CALIBERS = ("", "unknown")


def report_scope_ok(*observations) -> tuple[bool, str]:
    """一批观察的**报表范围**是否相容（跨指标/跨期共用同一判据）。

    为什么单独成判据（2026-09-29 夜验收反例）：利润桥原本只比"同指标两期"的主体/
    币种/量纲，跨指标只比量纲——于是"合并净利 10/12 + 母公司毛利 30/40 + 母公司 CFO 20"
    照样 validated。**"归母净利 vs 全部权益"的归属层差异，不等于允许母公司报表与合并
    报表混算**：跨指标比较必须在同一报表范围（合并/母公司）内，且该范围要**有声明**。

    返回 `(是否相容, 原因)`；原因直接进 `NotApplicable` 的读者可见说明。
    """
    obs = [o for o in observations if o is not None]
    cals = {str(getattr(o, "caliber", "") or "") for o in obs}
    if cals & set(_UNDECLARED_CALIBERS):
        shown = "、".join(sorted(c or "空" for c in cals))
        return False, (f"跨指标报表范围未声明（{shown}）：无法证明范围相容，"
                       "先补口径证据再入模型")
    if len(cals) > 1:
        shown = "、".join(sorted(cals))
        return False, (f"跨指标报表范围不一致（{shown}）：合并报表与母公司报表不得混算，"
                       "同一模型只接受同一范围的输入")
    return True, ""


# **指标的期间角色**（R1-a，2026-09-30 下午复核）：流量（区间发生额）还是存量（时点余额）。
# 为什么必须显式声明：`dataset` 此前只按"标签以年结尾"判年度期间，于是
#   - 同一指标两期一条记 flow、另一条记 stock（毛利半年当全年）照样进年度桥；
#   - "毛利截至 6/30、净利全年"照样 validated。
# 角色与声明出来的 `period_kind` 不符 → 不计算（缺声明时不猜，交给下面的"未知即受限"）。
METRIC_PERIOD_ROLE: dict[str, str] = {
    # 利润表/现金流量表项目：区间发生额
    "revenue": "flow", "operating_cost": "flow", "gross_profit": "flow",
    "net_profit": "flow", "operating_profit": "flow", "operating_cashflow": "flow",
    "rd_expense": "flow", "expense": "flow", "tax": "flow",
    # 资产负债表项目：时点余额（期末/期初）
    "accounts_receivable": "stock", "inventory": "stock", "accounts_payable": "stock",
    "total_assets": "stock", "total_liabilities": "stock", "equity": "stock",
}


def period_role_of(metric: str) -> str:
    """该指标的期间角色（flow/stock）；未声明返回空串（不猜）。"""
    return METRIC_PERIOD_ROLE.get(str(metric or ""), "")


def _period_span_days(obs) -> int | None:
    """起止日期齐备时给出区间天数；缺任一端返回 None（未知，不按全年补）。"""
    import datetime as _dt
    s = str(getattr(obs, "period_start", "") or "")
    e = str(getattr(obs, "period_end", "") or "")
    if not s or not e:
        return None
    try:
        d0 = _dt.date.fromisoformat(s[:10])
        d1 = _dt.date.fromisoformat(e[:10])
    except Exception:                                # noqa: BLE001
        return None
    return (d1 - d0).days


def period_identity_ok(*observations) -> tuple[bool, str]:
    """**期间身份**是否自洽（R1-a）：角色↔kind 相符、同期区间相容、半年度不冒年报。

    只判输入自己声明出来的东西：缺 `period_kind`/日期时不下断言（"未知即受限"，
    不默认补全年）——这条纪律写在返回说明里，不靠调用方各自记得。
    """
    obs = [o for o in observations if o is not None and getattr(o, "usable", False)]
    if not obs:
        return True, ""
    bad: list[str] = []
    spans_by_period: dict[str, set[tuple]] = {}
    for o in obs:
        role = period_role_of(getattr(o, "metric", ""))
        kind = str(getattr(o, "period_kind", "") or "")
        if role and kind and kind != role:
            bad.append(f"{o.metric} {o.period} 标为 {kind}，但该指标是 {role}"
                       + (f"（列头「{o.period_label}」）" if getattr(o, "period_label", "")
                          else ""))
            continue
        days = _period_span_days(o)
        if role == "flow":
            # 0 天 = 只记了期末日期（区间未知）→ 不强断言；0<天<300 = 半年/部分区间 → 不得当年度
            if days is not None and 0 < days < 300:
                bad.append(f"{o.metric} {o.period} 的区间只有 {days} 天"
                           f"（{o.period_start}→{o.period_end}）：不是年度流量，"
                           "不得按年度期间使用")
            elif days is not None and days > 0:
                spans_by_period.setdefault(str(o.period), set()).add(
                    (str(o.period_start), str(o.period_end)))
        elif role == "stock":
            end = str(getattr(o, "period_end", "") or "")
            if end and not end.endswith("12-31"):
                bad.append(f"{o.metric} {o.period} 的期末为 {end}：期末余额只能是年末时点，"
                           "半年末/季末余额不得当年度余额")
    # **同期区间必须相容**（R1-a）：同一期间里既有整年流量又有半年度流量 → 不得混算。
    # 只比区间、不比数值；区间未声明（空）不参与。
    for period, spans in spans_by_period.items():
        if len(spans) > 1:
            bad.append(f"{period} 同一期间出现不同区间：{sorted(spans)}"
                       "（同一期的各指标区间必须相容，半年度不得与全年混算）")
    if bad:
        return False, "期间身份不自洽：" + "；".join(sorted(set(bad))[:4])
    return True, ""


def full_identity_ok(*observations, require_amount: bool = True,
                     same_scale: bool = False) -> tuple[bool, str]:
    """**全部实际参与输入**的完整身份是否一致（L0-a，统一判据）。

    为什么单列一条（2026-09-30 架构复核 F1）：利润桥此前只核"同指标两期"的主体/币种/口径，
    **跨指标**（净利 vs 毛利）只比了报表范围与量纲——于是"两期洋河/CNY 净利 + 两期茅台/USD
    毛利"照样 validated，且全部输出标成洋河/CNY。跨指标输入必须与同指标一样过完整身份：
    主体、币种、报表范围、金额量纲（`require_amount` 时还要求可换算为金额）。

    `report_scope_ok` 只判其中一项（报表范围），保留它是为了让"跨范围混算"这条错误信息
    仍然具体；本函数是**算子入口与独立验证共用的那一条**，两处都调用，不各写一套。

    返回 `(是否相容, 原因)`；不可用观察（缺失/冲突/未知/无效）同样判不相容。
    """
    obs = [o for o in observations if o is not None]
    if not obs:
        return False, "没有任何输入观察：不计算"
    unusable = [f"{getattr(o, 'metric', '')} {getattr(o, 'period', '')}={getattr(o, 'state', '')}"
                for o in obs if not getattr(o, "usable", False)]
    if unusable:
        return False, ("输入观察不可用（缺失/冲突/未知/无效不作数）："
                       + "、".join(unusable))
    for field, label in (("entity_id", "主体"), ("currency", "币种"), ("caliber", "报表范围")):
        vals = {str(getattr(o, field, "") or "") for o in obs}
        if "" in vals:
            return False, (f"输入的{label}未声明：完整身份核不过（不默认、不推断）")
        if len(vals) > 1:
            return False, (f"输入的{label}不一致（{'、'.join(sorted(vals))}）："
                           "同一次计算只接受同一身份，不得跨主体/币种/报表范围混算")
    scope_ok, scope_why = report_scope_ok(*obs)
    if not scope_ok:
        return False, scope_why
    scales = {float(getattr(o, "money_scale", 0) or 0) for o in obs}
    if require_amount and any(s <= 0 for s in scales):
        return False, ("输入里存在不可换算为金额的单位（"
                       + "、".join(sorted({str(getattr(o, 'unit', '')) for o in obs})) + "）")
    if same_scale and len(scales) > 1:
        return False, (f"输入金额量纲不一致（{sorted(scales)}）：先显式换算再入模型")
    # **期间身份**（R1-a）：角色↔kind、区间是否满一年、期末是否年末、同指标区间是否相容。
    # 与主体/币种/口径一样属于"入模型前必须相容"的身份，不是算子各自记得的事。
    p_ok, p_why = period_identity_ok(*obs)
    if not p_ok:
        return False, p_why
    return True, ""


class NotApplicable(RuntimeError):
    """模型对这份数据集不适用（主体/期间/口径/币种/量纲不匹配）：如实说不适用。"""


class NotComputable(RuntimeError):
    """缺数据但模型本身适用（如分母为 0）：说明为什么不可算。"""


@dataclass(frozen=True)
class InputRequirement:
    """一个输入槽位：指标 + 期间关系（同期 / 上一期 / 任意两期）+ 必须同维度的字段。"""

    role: str
    metric: str
    period_offset: int = 0          # 0=当期，-1=上一期（按 dataset.periods 顺序）
    must_match: tuple[str, ...] = ("entity_id", "currency", "caliber")
    allow_zero: bool = True


@dataclass(frozen=True)
class OutputSpec:
    metric: str
    label: str
    unit: str = ""
    kind: str = "amount"            # amount / pct / count
    per_input: bool = False
    # 输出的**结构声明**（L0-c）：决定它能画成什么图，而不是按"分项数量"猜。
    # bridge=可闭合的加总贡献桥（瀑布）；scenarios=并行情景（并列对比）；
    # sensitivity=单因素敏感度（排序条形）；ratio=比率；trend=多期趋势。
    structure: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ModelSpec:
    """注册模型：适用问题、输入输出类型/量纲/口径、算子、参数界限、容差、验证方案、预算。"""

    model_id: str
    version: str
    family: str
    question: str
    operator: str
    inputs: tuple[InputRequirement, ...]
    outputs: tuple[OutputSpec, ...]
    allowed_params: dict = field(default_factory=dict)
    # 不传参数时**实际会用的值**（显式给出：页面不让读者猜"不改会用什么"）
    default_params: dict = field(default_factory=dict)
    tolerance: float = 0.005
    validations: tuple[str, ...] = ("gold", "closure", "identity", "unit")
    budget: dict = field(default_factory=lambda: {"steps": 1, "seconds": 5})
    limits: tuple[str, ...] = ()
    domain: str = "cn_non_financial"
    # **能回答哪些问题类型**（L2，2026-09-30 复核 M2）：`questions.QUESTION_TYPES` 的 qid。
    # 空 = 未声明（按"不知道它回答什么"处理，不加入任何问题驱动的选择）。
    question_types: tuple[str, ...] = ()
    # **分项契约**（R1-b，2026-09-30 下午复核）：`{输出指标: (稳定 component_id, …)}`。
    # 声明之后，独立验证会**逐项**从输入重算每个分项并比对——"合计相等"不再是充分条件
    # （实机反例：两分项 +10/−10、情景基准 +10/使用者情景 −10，合计不变、旧验证全过）。
    component_ids: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"model_id": self.model_id, "version": self.version, "family": self.family,
                "question": self.question, "operator": self.operator,
                "inputs": [i.__dict__ for i in self.inputs],
                "outputs": [o.__dict__ for o in self.outputs],
                "allowed_params": dict(self.allowed_params),
                "default_params": dict(self.default_params),
                "tolerance": self.tolerance, "validations": list(self.validations),
                "budget": dict(self.budget), "limits": list(self.limits),
                "domain": self.domain,
                "question_types": list(self.question_types)}


class RunStatus:
    VALIDATED = "validated"                  # 已验证（独立验证全过）
    VALIDATION_FAILED = "validation_failed"  # 验证失败（含被篡改）
    NOT_APPLICABLE = "not_applicable"
    MISSING_INPUT = "missing_input"
    NOT_COMPUTABLE = "not_computable"
    FAILED = "failed"


@dataclass(frozen=True)
class ValidatedOutput:
    """模型输出（可追溯工件）：每个正文数字绑 `output_id`，不只按数值相等匹配。"""

    output_id: str
    run_id: str
    metric: str
    label: str
    value: float
    unit: str
    entity: str = ""
    entity_id: str = ""
    input_periods: tuple[str, ...] = ()
    output_period: str = ""
    caliber: str = ""
    currency: str = ""
    components: tuple[dict, ...] = ()
    residual: float | None = None
    formula: str = ""
    inputs: tuple[str, ...] = ()          # 输入观察的 fact_id
    assumptions: tuple[str, ...] = ()
    limits: tuple[str, ...] = ()
    diagnostics: dict = field(default_factory=dict)

    @property
    def output_hash(self) -> str:
        return _hash({"run": self.run_id, "metric": self.metric,
                      "value": _canonical(self.value), "unit": self.unit,
                      "period": self.output_period, "components": self.components,
                      "residual": _canonical(self.residual),
                      "inputs": list(self.inputs)})

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["output_hash"] = self.output_hash
        return out


@dataclass(frozen=True)
class ModelRun:
    """一次运行的身份与结果：数据集 hash、模型/实现版本、参数 hash、验证结论。"""

    run_id: str
    model_id: str
    model_version: str
    impl_version: str
    dataset_hash: str
    params_hash: str
    params: dict = field(default_factory=dict)
    status: str = RunStatus.VALIDATED
    reason: str = ""
    outputs: tuple[ValidatedOutput, ...] = ()
    validation: dict = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""
    seed: int | None = None
    budget: dict = field(default_factory=dict)
    budget_used: dict = field(default_factory=dict)
    environment: str = ""
    random_seed_used: bool = False
    # 独立验证**规则集版本**（L0-a）：规则变了，旧 run 不得被当成"按新规则已验证"。
    # 刻意不进 `run_id`：改 run_id 会让既有交付包里的 output_id 全部对不上，
    # K2 的"从 ZIP 字节 7/7 离线复算"会被误伤（复核要求保留该证据）。
    rules_version: str = ""
    # **按当前规则重算时的留痕**（R0-b，09-30 下午复核）：历史 run 没记规则版本，
    # 采纳前按当前规则确定性重算会覆盖同 run_id 的记录——旧记录（规则版本/状态/验收摘要）
    # 必须留在这里，历史验证不得被静默抹去。同样**不进 `run_id`**（同上，保 K2）。
    revalidated_from: dict = field(default_factory=dict)

    def expired(self, dataset: AnalysisDataset) -> bool:
        """数据集一变，旧结果立刻过期（保留旧版本，但不得再当当前结果）。"""
        return self.dataset_hash != dataset.dataset_hash

    def output(self, output_id: str) -> ValidatedOutput | None:
        return next((o for o in self.outputs if o.output_id == output_id), None)

    def as_dict(self) -> dict:
        out = dict(self.__dict__)
        out["outputs"] = [o.as_dict() for o in self.outputs]
        return out


@dataclass(frozen=True)
class PlanItem:
    model_id: str
    question: str
    reason: str = ""
    params: dict = field(default_factory=dict)
    # 这次运行**产出什么**（L2 要求"计划绑定问题与输出"）：metric 清单，来自 ModelSpec。
    outputs: tuple[str, ...] = ()
    question_types: tuple[str, ...] = ()     # 这条计划是在回答哪些问题类型


@dataclass(frozen=True)
class AnalysisPlan:
    """分析计划：每个问题采用/拒绝哪些模型及原因、输入快照、参数来源。"""

    question: str
    dataset_hash: str
    adopted: tuple[PlanItem, ...] = ()
    rejected: tuple[dict, ...] = ()
    # L2 增量：命中的问题类型、按问题需要的材料缺口、以及"这次为什么这样选"的说明。
    # 全部**复用**现有结构，不新建空壳。
    question_types: tuple[str, ...] = ()
    question_type_labels: tuple[str, ...] = ()
    needs: tuple[dict, ...] = ()
    gaps: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"question": self.question, "dataset_hash": self.dataset_hash,
                "adopted": [a.__dict__ for a in self.adopted],
                "rejected": list(self.rejected),
                "question_types": list(self.question_types),
                "question_type_labels": list(self.question_type_labels),
                "needs": [dict(n) for n in self.needs],
                "gaps": list(self.gaps),
                "notes": list(self.notes)}
