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
    as_of: str = ""
    restatement: str = ""            # 重述批次/版本（未重述留空）
    source_url: str = ""
    source_hash: str = ""
    verify_state: str = ""
    note: str = ""

    @property
    def money_scale(self) -> float:
        return _money_scale(self.unit)

    @property
    def is_amount(self) -> bool:
        return self.money_scale > 0

    @property
    def observation_hash(self) -> str:
        return _hash({
            "fact_id": self.fact_id, "metric": self.metric, "period": self.period,
            "value": _canonical(self.value), "unit": self.unit, "currency": self.currency,
            "caliber": self.caliber, "entity_id": self.entity_id,
            "restatement": self.restatement, "source_hash": self.source_hash,
            "state": self.state, "schema": SCHEMA_VERSION,
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
        """
        cals = [o.caliber for o in self.observations if o.usable and o.caliber]
        if not cals:
            return ""
        if DEFAULT_CALIBER in cals:
            return DEFAULT_CALIBER
        uniq = sorted(set(cals))
        return uniq[0] if len(uniq) == 1 else ""

    def get(self, metric: str, period: str, *, caliber: str | None = None):
        """取一条观察；`caliber` 未给时用**主口径**，同一口径内多条不同值仍返回 `None`。

        主口径取不到时，退回"只有唯一一条就返回它"（旧规则）：
        - 该条可能是**不可用**的（元数据不全）——必须能被找到并如实报"不可用"，
          不能悄悄变成"缺输入"（两者含义不同）；
        - 也可能这个指标只在另一种口径里——跨口径混用**不会**被这里静默放行：
          算子自己会核对输入口径是否一致（`profit_bridge` 会抛 `NotApplicable`）。
        真正危险的"同一 (指标,期间) 有两条不同值"仍然返回 `None`，绝不任选一条。
        """
        want = self.primary_caliber if caliber is None else str(caliber)
        if want:
            key = (str(metric), str(period), want)
            if key in self.index:
                return self.index[key]
            hits = [o for o in self.observations
                    if o.metric == str(metric) and o.period == str(period)
                    and o.caliber == want]
            if hits:
                return hits[0] if len(hits) == 1 else None
        all_hits = [o for o in self.observations
                    if o.metric == str(metric) and o.period == str(period)]
        return all_hits[0] if len(all_hits) == 1 else None

    def require(self, metric: str, period: str, *, caliber: str | None = None) -> Observation:
        obs = self.get(metric, period, caliber=caliber)
        if obs is None:
            raise MissingInput(f"缺少观察：{metric} {period}"
                               + (f"（口径 {caliber}）" if caliber else ""))
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
    tolerance: float = 0.005
    validations: tuple[str, ...] = ("gold", "closure", "identity", "unit")
    budget: dict = field(default_factory=lambda: {"steps": 1, "seconds": 5})
    limits: tuple[str, ...] = ()
    domain: str = "cn_non_financial"

    def as_dict(self) -> dict:
        return {"model_id": self.model_id, "version": self.version, "family": self.family,
                "question": self.question, "operator": self.operator,
                "inputs": [i.__dict__ for i in self.inputs],
                "outputs": [o.__dict__ for o in self.outputs],
                "allowed_params": dict(self.allowed_params),
                "tolerance": self.tolerance, "validations": list(self.validations),
                "budget": dict(self.budget), "limits": list(self.limits),
                "domain": self.domain}


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


@dataclass(frozen=True)
class AnalysisPlan:
    """分析计划：每个问题采用/拒绝哪些模型及原因、输入快照、参数来源。"""

    question: str
    dataset_hash: str
    adopted: tuple[PlanItem, ...] = ()
    rejected: tuple[dict, ...] = ()

    def as_dict(self) -> dict:
        return {"question": self.question, "dataset_hash": self.dataset_hash,
                "adopted": [a.__dict__ for a in self.adopted],
                "rejected": list(self.rejected)}
