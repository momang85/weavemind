# -*- coding: utf-8 -*-
"""利润到现金的转化（L3 第一条纵向成果）：**本期利润增长有没有转成现金、哪些因素还没被解释**。

本算子刻意不是「再跑一遍利润桥 + 现金质量」：它把两族串成**一条**可核对的链——

1. `profit_change`：Δ归母净利 = Δ毛利 + Δ(归母净利 − 毛利)，两项相加**恰好闭合**
   （`residual = 0` 是恒等式的结果，不是「误差」）；其中「毛利端变化」回答「利润变化里
   有多少来自毛利端」这个对照问题；
2. `cash_profit_gap_change`：Δ(经营现金流 − 归母净利)——缺口本身**变大还是变小**，
   这是「利润有没有转成现金」的金额读数（单期缺口的水平值已由 `cash_quality` 给出，
   这里不重复它的输出，只看**变化**）；
3. `cash_conversion_change`：两期各自的「经营现金流 ÷ 归母净利」之比的变化（**百分点**）。
   **仅当两期归母净利都为正时给**——任一非正就不给，并在 `diagnostics` 里写明原因
   （负/零分母没有可比含义，这是既有裁决）；
4. **一个数一个名字**：缺口变化**就是**「尚未解释的差额」——不再另立一个同值的
   `unexplained_cash_gap` 指标（两个数字相同、名字不同的读数会让读者以为是两件事）。
   模型手里只有两个总额观察，没有现金流量表调节表，所以这个数只能叫「未解释额」——
   它**含**营运资本变动、折旧摊销、减值等非现金项，缺调节表就不能称已解释。

口径纪律（与既有算子一致，直接复用公共契约，不另写一套）：

- 入口对**全部六个**输入观察一次核完整身份（主体/币种/报表范围/金额量纲），
  `same_scale=True`（本模型内部要相减）；不一致 → `NotApplicable`。反例是 D 批实机：
  跨指标只比报表范围与量纲时，「洋河/CNY 净利 + 茅台/USD 毛利」照样 validated；
- 归母净利（归属母公司口径）与经营现金流（合并现金流量表口径）**归属层不同**，
  写进 `limits`/`assumptions`；**不得**把它写成现金流量表调节恒等式；
- 只给声明式素材，不在算子层下自然语言结论、不做因果判断；
- **零概率/零预测**：不给置信区间、不给预测值。
"""
from __future__ import annotations

from decimal import Decimal

from ..contracts import (
    InputRequirement, ModelSpec, NotApplicable, OutputSpec, full_identity_ok,
)

IMPL_VERSION = "profit_to_cash/1.0.0"

LIMITS = (
    "这是**两期变化**的读数，不是现金流量表调节表：没有调节表就不能称「已解释」",
    "经营现金流（合并现金流量表口径）与归母净利（归属母公司口径）**归属层不同**，"
    "两者之差只是观察差额，**不是**会计恒等式，也不是调节表闭合",
    "`cash_profit_gap_change` 就是把 Δ(经营现金流 − 归母净利) 作为「未解释额」：它**含**"
    "营运资本变动、折旧摊销、减值等非现金项，模型没有这些明细，故不作「已解释」拆分",
    "归母净利非正时不给现金转化变化（负/零分母的分母没有可比含义），只给金额差额",
    "会计分解只说明金额构成，**不等于**因果解释；方向（增/减）不构成业务归因",
    "零概率零预测：不给发生概率、不给置信区间、不外推未来期间",
)

# 输出的 label 必须让读者看懂**方向与口径**（不只写英文 slug），所以标签写成整句。
_OUT_LABELS = {
    "profit_change": "两期归母净利润变化（= 毛利端变化 + 毛利线以下净额变化）",
    # 一个数一个名字（L3 定稿）：缺口变化**就是**尚未解释的差额——同一个值不挂两个指标名，
    # 免得正文里出现两个数字相同、名字不同的读数（读者会以为是两件事）。
    "cash_profit_gap_change": ("现金—利润缺口变化（= Δ(经营现金流 − 归母净利润)，"
                               "即**尚未解释的差额**：含营运资本变动/折旧摊销/减值等"
                               "非现金项与归属层差异）"),
    "cash_conversion_change": "现金转化变化（经营现金流 ÷ 归母净利润，两期之比的变化，百分点）",
}

SPEC = ModelSpec(
    model_id="profit_to_cash",
    version="1.0.0",
    family="利润到现金的转化",
    question="本期利润增长是否转化为现金、哪些因素尚未解释",
    operator="profit_to_cash_v1",
    inputs=(
        InputRequirement("net_profit_cur", "net_profit", period_offset=0),
        InputRequirement("net_profit_prev", "net_profit", period_offset=-1),
        InputRequirement("operating_cashflow_cur", "operating_cashflow", period_offset=0),
        InputRequirement("operating_cashflow_prev", "operating_cashflow", period_offset=-1),
        InputRequirement("gross_profit_cur", "gross_profit", period_offset=0),
        InputRequirement("gross_profit_prev", "gross_profit", period_offset=-1),
    ),
    outputs=(
        # 可闭合的加总贡献桥：分项相加**恰好等于**总额，`residual = 0`。
        OutputSpec("profit_change", _OUT_LABELS["profit_change"], kind="amount",
                   structure="bridge"),
        # 差值（无分项）：`identity` 的「合计=总量」对它不适用，由 `unit`/`cash_conversion_sign` 守。
        OutputSpec("cash_profit_gap_change", _OUT_LABELS["cash_profit_gap_change"],
                   kind="amount"),
        OutputSpec("cash_conversion_change", _OUT_LABELS["cash_conversion_change"],
                   unit="%", kind="pct"),
    ),
    allowed_params={"rounding": ("yi_2", "yuan_2")},
    tolerance=0.005,
    validations=("gold", "closure", "identity", "unit", "cash_conversion_sign"),
    budget={"steps": 1, "seconds": 5},
    limits=LIMITS,
    # L3 的主问题：利润增长有没有转成现金、哪些还没被解释（现金转化这一类的主模型）
    question_types=("cash_conversion",),
    component_ids={"profit_change": ("gross_profit_change", "below_gross_line_change")},
)


def _d(value) -> Decimal:
    return Decimal(str(value))


def _inputs(dataset):
    """六个输入观察 + 两期期间（期间取数据集自己声明的顺序，**不按字符串猜「最新」**）。"""
    periods = [p for p in (dataset.manifest.periods or ()) if p]
    if len(periods) < 2:
        raise NotApplicable("数据集少于两个年度期间：两期转化链不适用（不给单期「转化」）")
    prev_p, cur_p = periods[-2], periods[-1]
    if prev_p == cur_p:
        raise NotApplicable(f"两期期间相同（{cur_p}）：两期变化需要一个以上的期间")
    np_cur = dataset.require("net_profit", cur_p)
    np_prev = dataset.require("net_profit", prev_p)
    ocf_cur = dataset.require("operating_cashflow", cur_p)
    ocf_prev = dataset.require("operating_cashflow", prev_p)
    gp_cur = dataset.require("gross_profit", cur_p)
    gp_prev = dataset.require("gross_profit", prev_p)
    # **完整身份**一次核全部实际参与输入的观察：主体、币种、报表范围、金额量纲。
    # `same_scale=True` 是必须的：本模型内部要做减法，量纲不同就必须先显式换算，
    # 不能在算子里静默缩放（否则「亿元 − 元」这种差会被当真）。
    ident_ok, ident_why = full_identity_ok(np_cur, np_prev, ocf_cur, ocf_prev,
                                           gp_cur, gp_prev, same_scale=True)
    if not ident_ok:
        raise NotApplicable(ident_why)
    return (prev_p, cur_p, np_cur, np_prev, ocf_cur, ocf_prev, gp_cur, gp_prev)


def compute(dataset, params: dict | None = None) -> dict:
    """算一次「利润 → 现金」转化链 → `{periods, formula, inputs, assumptions,
    diagnostics, outputs, limits}`（与既有算子同一返回形状与字段名）。"""
    params = dict(params or {})
    (prev_p, cur_p, np_cur, np_prev, ocf_cur, ocf_prev,
     gp_cur, gp_prev) = _inputs(dataset)
    # 两期同主体/币种/口径/量纲已由 `full_identity_ok` 核过，所以这里同单位直接相减；
    # 单位取自**净利**（本链的「利润」一端），其它输入的金额量纲与它一致。
    unit = str(np_cur.unit or "")
    d_np = _d(np_cur.value) - _d(np_prev.value)          # Δ归母净利
    d_gp = _d(gp_cur.value) - _d(gp_prev.value)          # Δ毛利（毛利端变化）
    below = d_np - d_gp                                  # 毛利线以下净额变化
    # 现金—利润缺口：Δ(经营现金流 − 归母净利) = Δ经营现金流 − Δ归母净利。
    # 两个总额观察都在同一量纲/币种/期间下，差额就是读者要看的「缺口变化」。
    d_gap = (_d(ocf_cur.value) - _d(ocf_prev.value)) - d_np

    def _q(x: Decimal) -> float:
        # 与既有算子同一舍入策略（金额十进制、两位），避免两套舍入规则互相打架。
        return float(x.quantize(Decimal("0.01")))

    gap_formula = (f"Δ(经营现金流 − 归母净利) = ({ocf_cur.value} - {ocf_prev.value})"
                   f"[经营现金流] - ({np_cur.value} - {np_prev.value})[归母净利]")
    period_pair = f"{cur_p}较{prev_p}"
    outputs = [
        {"metric": "profit_change", "label": _OUT_LABELS["profit_change"],
         "value": _q(d_np), "unit": unit, "output_period": period_pair,
         "residual": 0.0,
         "components": [
             {"component_id": "gross_profit_change",
              "label": "毛利端变化（Δ毛利；回答「利润变化里有多少来自毛利端」）",
              "value": _q(d_gp), "unit": unit,
              "formula": f"({gp_cur.value} - {gp_prev.value})"},
             {"component_id": "below_gross_line_change",
              "label": ("毛利线以下净额变化（= Δ归母净利 − Δ毛利；含费用/税项/投资收益/"
                        "少数股东等，需明细表才能解释）"),
              "value": _q(below), "unit": unit,
              "formula": (f"({np_cur.value} - {np_prev.value}) - "
                          f"({gp_cur.value} - {gp_prev.value})")},
         ]},
        {"metric": "cash_profit_gap_change", "label": _OUT_LABELS["cash_profit_gap_change"],
         "value": _q(d_gap), "unit": unit, "output_period": period_pair,
         "components": [], "residual": None, "formula": gap_formula},
    ]
    # 现金转化变化：两期各自的 经营现金流/归母净利 之比的变化（百分点）。
    # **两期都为正**才给：负/零分母的比率没有可比含义（既有裁决，不做「取绝对值」这类补救）。
    np_cur_v, np_prev_v = _d(np_cur.value), _d(np_prev.value)
    coverage_skipped = ""
    if np_cur_v > 0 and np_prev_v > 0:
        ratio_cur = _d(ocf_cur.value) / np_cur_v * 100
        ratio_prev = _d(ocf_prev.value) / np_prev_v * 100
        outputs.append({
            "metric": "cash_conversion_change",
            "label": _OUT_LABELS["cash_conversion_change"],
            "value": float((ratio_cur - ratio_prev).quantize(Decimal("0.01"))),
            "unit": "%", "output_period": period_pair, "components": [], "residual": None,
            "formula": (f"({ocf_cur.value}/{np_cur.value}*100) - "
                        f"({ocf_prev.value}/{np_prev.value}*100)（单位：百分点）")})
    else:
        # 哪一期害的要说清楚（只说「有一期非正」读者还得自己找），并给读者可行动的方向。
        bad = [f"{p}={v}{unit}" for p, v in ((prev_p, np_prev_v), (cur_p, np_cur_v))
               if v <= 0]
        coverage_skipped = ("cash_conversion_change 不给：归母净利为负或零的期间（"
                            + "、".join(bad) + "）——负/零分母之比没有可比含义，"
                            "不由模型换算或取绝对值兜底")
    return {
        "periods": (prev_p, cur_p),
        "formula": ("Δ归母净利 = Δ毛利 + Δ(归母净利 − 毛利)（恒等式，残差 0）；"
                    + gap_formula + "；现金转化 = 经营现金流/归母净利 两期之比之差（百分点）"),
        "inputs": (np_cur.fact_id, np_prev.fact_id, ocf_cur.fact_id, ocf_prev.fact_id,
                   gp_cur.fact_id, gp_prev.fact_id),
        "assumptions": (
            # 口径**从已验证输入生成**，不固定写「合并」：母公司口径的数据集也能跑，
            # 说明里必须写它真实的那个范围。
            f"两期均为{np_cur.caliber}口径、{np_cur.currency}、{unit}；六个输入一次核完"
            "主体/币种/报表范围/金额量纲（same_scale）",
            "期间取自数据集声明的期间顺序的最后两期（不按字符串猜「最新」）",
            "归母净利为归属母公司口径、经营现金流为合并现金流量表口径：两者**归属层不同**，"
            "相减得到的只是观察差额",
            "现金转化变化的分子分母都是同一期同一主体的两个数，只做除法与相减，不换算、不外推",
        ),
        "diagnostics": {
            "unit": unit, "currency": np_cur.currency, "caliber": np_cur.caliber,
            "entity": np_cur.entity, "entity_id": np_cur.entity_id,
            "periods": [prev_p, cur_p],
            "input_hashes": {f"{o.metric}@{o.period}": o.observation_hash
                             for o in (np_cur, np_prev, ocf_cur, ocf_prev,
                                       gp_cur, gp_prev)},
            # 恒等式闭合差：字符串化的**精确 0**（浮点化会带来假非零）。
            "closure": str(d_np - (d_gp + below)),
            # 未解释额 = 缺口本身；把「它含什么」写进诊断，读者不必猜口径（L3 要求）。
            "unexplained_includes": ("营运资本变动、折旧摊销、减值等非现金项，"
                                     "以及归属层差异（归母净利 vs 合并现金流量表）；"
                                     "缺现金流量表调节表则不能称已解释"),
            "cash_conversion_available": bool(not coverage_skipped),
            # 非正分母时的原因（沿用 cash_quality 的字段名，验证层按它判签）。
            "coverage_skipped": coverage_skipped,
        },
        "outputs": outputs,
        "limits": LIMITS,
    }


def components_gold(dataset, params: dict | None = None) -> dict:
    """**分项**的独立计算（R1-b）：毛利端与毛利线以下两段从输入 Decimal 重算。

    与 `gold` 同一纪律（独立路径、同一身份判据）；返回
    `{输出指标: {component_id: (value, unit)}}`。合计校验只是附加，逐项才是判据。
    """
    (prev_p, cur_p, np_cur, np_prev, ocf_cur, ocf_prev,
     gp_cur, gp_prev) = _inputs(dataset)
    d_gp = _d(gp_cur.value) - _d(gp_prev.value)
    d_np = _d(np_cur.value) - _d(np_prev.value)
    below = d_np - d_gp

    def _q(x: Decimal) -> float:
        return float(x.quantize(Decimal("0.01")))

    unit = str(np_cur.unit or "")
    return {"profit_change": {"gross_profit_change": (_q(d_gp), unit),
                              "below_gross_line_change": (_q(below), unit)}}


def gold(dataset, params: dict | None = None) -> dict:
    """**独立金样**：用 `Decimal` 从原始观察逐项手算，**只给 compute 真会产生的 key**。

    `validation.py` 的 gold 检查是「金样里每个 metric 都要在输出里且接近」，所以非正分母
    时这里也**不能**给 `cash_conversion_change`——多了会被判失败（与 compute 保持一致）。
    """
    (prev_p, cur_p, np_cur, np_prev, ocf_cur, ocf_prev,
     gp_cur, gp_prev) = _inputs(dataset)
    np_c, np_p = _d(np_cur.value), _d(np_prev.value)
    d_np = np_c - np_p
    d_gap = (_d(ocf_cur.value) - _d(ocf_prev.value)) - d_np

    def _q(x: Decimal) -> float:
        return float(x.quantize(Decimal("0.01")))

    out = {"profit_change": _q(d_np),
           "cash_profit_gap_change": _q(d_gap)}
    if np_c > 0 and np_p > 0:
        out["cash_conversion_change"] = _q(
            (_d(ocf_cur.value) / np_c - _d(ocf_prev.value) / np_p) * 100)
    return out
