# -*- coding: utf-8 -*-
"""盈利与现金质量：利润有没有转成现金、差额集中在哪里。

口径纪律（架构 §5.3）：

- CFO/归母净利只是**观察比率**，**不是**现金流量表调节的闭合恒等式——两者归属层不同
  （经营现金流是合并现金流量表口径，归母净利是归属母公司口径），名字必须写出来；
- 差额只说明"有多少没被利润解释"，**缺现金流量表调节表就不能称完整解释**；
- 归母净利 ≤ 0 时不给覆盖率（负/零分母不展示误导性比率），只给差额与限制。
"""
from __future__ import annotations

from decimal import Decimal

from ..contracts import (
    InputRequirement, ModelSpec, NotApplicable, NotComputable, OutputSpec,
    report_scope_ok,
)

IMPL_VERSION = "cash_quality/1.0.0"

LIMITS = (
    "经营现金流（合并现金流量表口径）÷ 归母净利润 = 观察比率，**不是**现金流量表调节恒等式",
    "差额里含营运资本变动、折旧摊销、减值等非现金项：没有调节表就不能称“完整解释”",
    "归母净利 ≤ 0 时不给覆盖率（负/零分母不展示误导性比率）",
)

SPEC = ModelSpec(
    model_id="cash_quality",
    version="1.0.0",
    family="盈利与现金质量",
    question="本期利润有没有转成现金、差额集中在哪里",
    operator="cash_quality_v1",
    inputs=(
        InputRequirement("operating_cashflow", "operating_cashflow", period_offset=0),
        InputRequirement("net_profit", "net_profit", period_offset=0),
    ),
    outputs=(
        OutputSpec("cfo_minus_profit", "经营现金流 − 归母净利润", kind="amount"),
        OutputSpec("cashflow_coverage", "经营现金流对归母净利润的覆盖", unit="%", kind="pct"),
    ),
    allowed_params={"rounding": ("yi_2", "yuan_2")},
    tolerance=0.005,
    # 本模型的输出不是金额分解（差额 + 覆盖率），`identity` 的"合计=总量"不适用；
    # 同一性由 `cash_quality_sign`（分母非正时不得给覆盖率）与 `unit` 守住。
    validations=("gold", "unit", "cash_quality_sign"),
    budget={"steps": 1, "seconds": 5},
    limits=LIMITS,
)


def _d(v) -> Decimal:
    return Decimal(str(v))


def compute(dataset, params: dict | None = None) -> dict:
    params = dict(params or {})
    period = dataset.period_at(0)
    ocf = dataset.require("operating_cashflow", period)
    np_ = dataset.require("net_profit", period)
    if ocf.entity_id != np_.entity_id:
        raise NotApplicable(f"经营现金流与归母净利润主体不同（{ocf.entity_id} vs {np_.entity_id}）")
    if ocf.currency != np_.currency:
        raise NotApplicable(f"币种不同（{ocf.currency} vs {np_.currency}）")
    # 报表范围必须相容（K0-a 反例：合并净利 + 母公司经营现金流会算出"覆盖良好"）
    scope_ok, scope_why = report_scope_ok(ocf, np_)
    if not scope_ok:
        raise NotApplicable(scope_why)
    if ocf.money_scale <= 0 or np_.money_scale <= 0:
        raise NotApplicable(f"金额量纲不可换算（{ocf.unit}/{np_.unit}）")
    # 量纲不一致时先显式换算（不静默放大/缩小）：值是按**自己单位**写的数，
    # 从 `ocf.unit` 换到 `np_.unit` 要乘 (源量纲 / 目标量纲)。
    factor = Decimal(str(ocf.money_scale)) / Decimal(str(np_.money_scale))
    ocf_in_np_unit = _d(ocf.value) * factor
    diff = ocf_in_np_unit - _d(np_.value)
    unit = str(np_.unit or "")
    q = lambda x: float(x.quantize(Decimal("0.01")))          # noqa: E731
    outputs = [{"metric": "cfo_minus_profit", "label": "经营现金流 − 归母净利润",
                "value": q(diff), "unit": unit, "output_period": period,
                "components": [], "residual": None}]
    coverage_note = ""
    if _d(np_.value) > 0:
        cov = ocf_in_np_unit / _d(np_.value) * 100
        outputs.append({"metric": "cashflow_coverage",
                        "label": "经营现金流对归母净利润的覆盖",
                        "value": float(cov.quantize(Decimal("0.01"))), "unit": "%",
                        "output_period": period, "components": [], "residual": None})
    else:
        coverage_note = ("归母净利润非正（" + str(np_.value) + f"{unit}）："
                         "不给覆盖率——负/零分母的分母没有可比含义")
    return {
        "periods": (period,),
        "formula": (f"({ocf.value}{ocf.unit} 换算为 {q(ocf_in_np_unit)}{unit}) - "
                    f"{np_.value}{unit}；覆盖率 = 换算后 / {np_.value} * 100"),
        "inputs": (ocf.fact_id, np_.fact_id),
        "assumptions": (
            f"同一期间 {period}；经营现金流已按 {ocf.unit}→{unit} 显式换算（factor={factor}）",
            "两个数归属层不同：经营现金流为合并现金流量表口径，归母净利为归属母公司口径",
        ),
        "diagnostics": {
            "unit": unit, "currency": np_.currency, "caliber": np_.caliber,
            "entity": np_.entity, "entity_id": np_.entity_id,
            "periods": [period],
            "input_hashes": {f"{o.metric}@{o.period}": o.observation_hash
                             for o in (ocf, np_)},
            "coverage_skipped": coverage_note,
            "sign_ok": bool(_d(np_.value) > 0 or coverage_note),
        },
        "outputs": outputs,
        "limits": LIMITS + ((coverage_note,) if coverage_note else ()),
    }


def gold(dataset, params: dict | None = None) -> dict:
    """独立金样（Decimal 手算）：差额；仅当分母为正时给覆盖率。"""
    period = dataset.period_at(0)
    ocf = dataset.require("operating_cashflow", period)
    np_ = dataset.require("net_profit", period)
    scope_ok, scope_why = report_scope_ok(ocf, np_)     # 独立路径同样拒绝跨范围混算
    if not scope_ok:
        raise NotApplicable(scope_why)
    factor = Decimal(str(ocf.money_scale)) / Decimal(str(np_.money_scale))
    diff = _d(ocf.value) * factor - _d(np_.value)
    out = {"cfo_minus_profit": float(diff.quantize(Decimal("0.01")))}
    if _d(np_.value) > 0:
        out["cashflow_coverage"] = float(
            (_d(ocf.value) * factor / _d(np_.value) * 100).quantize(Decimal("0.01")))
    return out
