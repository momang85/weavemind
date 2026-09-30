# -*- coding: utf-8 -*-
"""独立验证：不复用生成函数的结论，另走一条路（十进制手算 + 约束 + 变异反例）。

架构要求（§4「ModelRun / ValidatedOutput」）：独立验证用手算金样/约束关系/不同计算路径
及变异反例，**不能只调用生成函数再比较自身**；金额用明确的十进制/舍入策略。

本模块提供：
- `validate_output(spec, dataset, payload)`：逐项跑 `spec.validations` 里的检查；
- `tamper_check(output, expected)`：输出被改成别的数必须能被发现（正文与底稿同源）。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .contracts import State, _canonical, _money_scale, full_identity_ok
from .registry import OPERATORS

# 验证**规则集版本**（L0-a，2026-09-30 复核 F5）：规则本身变了，旧 run 不得被当成
# "按新规则已验证"。它随 `ModelRun.rules_version` 记录（**不进 run_id**：run_id 变更会让
# 既有交付包里的 output_id 全部对不上，K2 的"从 ZIP 字节 7/7 离线复算"会被误伤）。
# 改变本模块的判据 → 必须提升这个版本号。
RULES_VERSION = "validation/1.1.0"


def _amount_scale(unit: str) -> float:
    """金额量纲（与契约同一实现，避免两套换算规则）。"""
    return _money_scale(unit)


def _tolerance(spec) -> Decimal:
    return Decimal(str(spec.tolerance))


def _close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    """相对容差 + 绝对 0.01：与项目既有 `_close` 同尺（金额用十进制，不用二进制浮点）。"""
    return abs(a - b) <= max(abs(b) * tol, Decimal("0.01"))


def _as_decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _year_tokens(text: str) -> set[str]:
    return set(re.findall(r"(?:19|20)\d{2}", str(text or "")))


def input_binding_check(spec, dataset, payload: dict) -> tuple[bool, str]:
    """**输入绑定**复核（L0-a）：载荷声明的输入 identity 必须真的对得上数据集。

    独立验证不能只看模型自报的 `closure/direction_ok`：那些字段是模型自己写的，
    改掉输入却留着结论照样"验证通过"。这里从 `payload["inputs"]` 的 fact_id **反查**
    数据集里的真实观察，再核主体/币种/报表范围/量纲，并与载荷自己声明的身份逐项对照
    （声明为空 = 未声明，不作失败；声明了却与输入矛盾 → 失败）。
    """
    ids = [str(x) for x in (payload.get("inputs") or ())]
    if not ids:
        return False, "载荷没有声明输入 fact_id：无法核对输入绑定（不接受无法追溯的输出）"
    obs = []
    for fid in ids:
        hit = next((o for o in dataset.observations if o.fact_id == fid), None)
        if hit is None:
            return False, f"载荷声明的输入 {fid} 在当前数据集里不存在"
        obs.append(hit)
    unusable = [f"{o.metric}@{o.period}={o.state}" for o in obs if not o.usable]
    if unusable:
        return False, "载荷引用了不可用观察：" + "、".join(unusable)
    ok, why = full_identity_ok(*obs)
    if not ok:
        return False, why
    diag = payload.get("diagnostics") or {}
    for field, label in (("entity_id", "主体"), ("currency", "币种"),
                         ("caliber", "报表范围")):
        declared = str(diag.get(field) or "")
        actual = {str(getattr(o, field) or "") for o in obs}
        if declared and declared not in actual:
            return False, (f"诊断里声明的{label}「{declared}」与实际输入不符"
                           f"（输入为 {'、'.join(sorted(actual))}）")
    du = str(diag.get("unit") or "")
    units = {str(o.unit or "") for o in obs}
    if du and du not in units:
        return False, f"诊断里声明的单位「{du}」与实际输入不符（输入为 {'、'.join(sorted(units))}）"
    return True, ""


def output_shape_check(spec, dataset, payload: dict) -> tuple[bool, str]:
    """**输出类型与身份**复核（L0-a）：每个会进正文/图表的输出与分项都要过关。

    检查：输出在模型声明之内；单位与声明的 `kind` 相符（amount/pct/count 不混）；
    值是数；`output_period` 只能由**数据集自己的期间**构成（防止把 2030 这种不存在的
    期间写进结论）；分项必须带标签/值/单位，且金额父项的分项也必须是金额。
    """
    outs = payload.get("outputs") or []
    if not outs:
        return False, "载荷没有输出"
    declared = {o.metric: o for o in (spec.outputs or ())}
    periods = [str(p) for p in (dataset.manifest.periods or ()) if str(p)]
    years = set()
    for p in periods:
        years |= _year_tokens(p)
    for o in outs:
        metric = str(o.get("metric") or "")
        if not metric:
            return False, "有输出没有 metric"
        if declared and metric not in declared:
            return False, f"输出 {metric} 不在模型声明的输出清单里（{sorted(declared)}）"
        unit = str(o.get("unit") or "")
        if not unit:
            return False, f"{metric} 没有单位"
        kind = str(getattr(declared.get(metric), "kind", "") or "")
        scale = _amount_scale(unit)
        if kind == "amount" and scale <= 0:
            return False, f"{metric} 声明为金额但单位是「{unit}」（不可换算为金额）"
        if kind == "pct" and "%" not in unit:
            return False, f"{metric} 声明为百分比但单位是「{unit}」"
        if kind == "count" and (scale > 0 or "%" in unit):
            return False, f"{metric} 声明为计数/天数但单位是「{unit}」"
        if o.get("value") is None or _as_decimal(o.get("value")) is None:
            return False, f"{metric} 的值不是数：{o.get('value')!r}"
        op = str(o.get("output_period") or "")
        if not op:
            return False, f"{metric} 没有 output_period（不能只说一个数、不说期间）"
        bad_years = sorted(y for y in _year_tokens(op) if years and y not in years)
        if bad_years:
            return False, (f"{metric} 的输出期间「{op}」含数据集里不存在的年度 "
                           f"{bad_years}（数据集期间 {periods}）")
        for c in (o.get("components") or ()):
            if not str(c.get("label") or ""):
                return False, f"{metric} 的分项缺 label"
            if c.get("value") is None or _as_decimal(c.get("value")) is None:
                return False, f"{metric} 的分项「{c.get('label')}」值不是数"
            cu = str(c.get("unit") or "")
            if not cu:
                return False, f"{metric} 的分项「{c.get('label')}」没有单位"
            if kind == "amount" and _amount_scale(cu) <= 0:
                return False, (f"{metric} 是金额输出，但分项「{c.get('label')}」单位"
                               f"「{cu}」不是金额（百分点不能顶金额贡献）")
    return True, ""


def validate_output(spec, dataset, payload: dict) -> dict:
    """→ `{ok, checks: {name: {ok, detail}}, failed: [...]}`。

    `gold` 走**另一条计算路径**（注册表里的独立金样函数，Decimal 手算）；
    其余检查都是约束关系，不调用生成函数。

    `binding` / `output_shape` 是**恒定执行**的两条（L0-a，2026-09-30 复核 F5）：
    输入绑定与输出类型不因模型声明的 `validations` 而豁免——被篡改的分项、主体、
    期间、币种与类型必须在独立验证里失败，不能只信模型自报的结论字段。
    """
    checks: dict[str, dict] = {}
    wanted = set(spec.validations)

    def _add(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    _ok, _why = input_binding_check(spec, dataset, payload)
    _add("binding", _ok, "" if _ok else _why)
    _ok, _why = output_shape_check(spec, dataset, payload)
    _add("output_shape", _ok, "" if _ok else _why)
    if "gold" in wanted:
        _mod, _compute, gold_fn = OPERATORS[spec.operator]
        try:
            expect = gold_fn(dataset, payload.get("params") or {})
            got = {o["metric"]: Decimal(str(o["value"])) for o in payload.get("outputs") or []}
            bad = [m for m, v in expect.items()
                   if m not in got or not _close(got[m], Decimal(str(v)), _tolerance(spec))]
            _add("gold", not bad,
                 "" if not bad else f"与手算金样不符：{bad}（金样 {expect}）")
        except Exception as exc:                       # noqa: BLE001 - 金样跑不动=没验证
            _add("gold", False, f"金样无法执行：{type(exc).__name__}: {str(exc)[:80]}")
    if "closure" in wanted:
        diag = payload.get("diagnostics") or {}
        gap = str(diag.get("closure") or "0")
        _add("closure", Decimal(gap) == 0,
             f"恒等式闭合差 {gap}（分解漏项/重复计项都会让它非零）")
    if "identity" in wanted:
        # **每一个**带分解项的输出都要核"合计=总量"（L0-a）：此前只看首个输出，
        # 于是篡改第二个输出的分项（复核 F5 的 ±999999）不会被发现。
        bad_ident: list[str] = []
        seen_any = False
        for o in (payload.get("outputs") or []):
            comps = o.get("components") or []
            if not comps:
                continue
            seen_any = True
            base = Decimal(str(o.get("value") or 0))
            total = sum(Decimal(str(c.get("value") or 0)) for c in comps)
            if not _close(total, base, _tolerance(spec)):
                bad_ident.append(f"{o.get('metric')} 贡献项合计 {total} vs 总量 {base}")
        if not seen_any:
            # 没有分解项的输出（如差额/覆盖率）："合计=总量"这条不适用，如实说明
            _add("identity", True, "该输出没有分解项，不适用合计核对")
        else:
            _add("identity", not bad_ident, "；".join(bad_ident))
    if "unit" in wanted:
        # 单位检查的**正确形状**：每个输出都要有单位；所有**金额**输出的量纲要一致。
        # 不能要求"全模型只有一个单位"——差额（亿元）+ 覆盖率（%）本来就该同时出现。
        outs = payload.get("outputs") or []
        unnamed = [str(o.get("metric")) for o in outs if not str(o.get("unit") or "")]
        scales = {_amount_scale(str(o.get("unit") or "")) for o in outs
                  if _amount_scale(str(o.get("unit") or "")) > 0}
        ok = not unnamed and len(scales) <= 1
        _add("unit", ok,
             f"缺单位的输出：{unnamed}" if unnamed
             else (f"金额输出量纲不一致：{sorted(scales)}" if len(scales) > 1
                   else f"单位齐备（金额量纲 {sorted(scales) or ['无金额输出']}）"))
    if "no_pp_substitution" in wanted:
        # 金额桥**不得**把"百分点差"当金额项：任一贡献项的单位不能是百分比
        pct = [c for o in (payload.get("outputs") or [])
               for c in (o.get("components") or []) if str(c.get("unit") or "").endswith("%")]
        _add("no_pp_substitution", not pct,
             "" if not pct else f"金额桥里出现百分比贡献项：{[c['label'] for c in pct]}")
    if "cash_quality_sign" in wanted:
        # 归母净利非正时**不得**出现覆盖率读数（负/零分母没有可比含义）
        diag = payload.get("diagnostics") or {}
        has_cov = any(str(o.get("metric")) == "cashflow_coverage"
                      for o in (payload.get("outputs") or []))
        skipped = bool(diag.get("coverage_skipped"))
        _add("cash_quality_sign", (not has_cov) if skipped else has_cov,
             str(diag.get("coverage_skipped") or "分母为正 → 覆盖率已给出"))
    if "cash_conversion_sign" in wanted:
        # 两期中任一期归母净利非正 → **不得**出现现金转化变化（负/零分母没有可比含义）。
        # 这条与 `cash_quality_sign` 同一裁决，但作用于"两期之比的变化"（L3 的 profit_to_cash）。
        outs = payload.get("outputs") or []
        diag = payload.get("diagnostics") or {}
        has_conv = any(str(o.get("metric")) == "cash_conversion_change" for o in outs)
        skipped = str(diag.get("coverage_skipped") or "")
        _add("cash_conversion_sign", (not has_conv) if skipped else has_conv,
             skipped or "两期归母净利均为正 → 现金转化变化已给出")
    if "posture_disclosed" in wanted:
        # 期末口径必须写明（不得让读者以为是平均余额）
        diag = payload.get("diagnostics") or {}
        posture = str(diag.get("posture") or "")
        assumed = " ".join(payload.get("assumptions") or ())
        _add("posture_disclosed",
             posture in ("closing_balance", "average_balance")
             and ("期末" in assumed or "平均" in assumed),
             f"posture={posture or '未标'}")
    if "base_reproduction" in wanted:
        # 情景的基准必须复现基期（参数全 0 → 差额 0）
        diag = payload.get("diagnostics") or {}
        gap = str(diag.get("base_reproduction_gap") or "")
        _add("base_reproduction", gap not in ("", "None") and Decimal(gap) == 0,
             f"基准复现差额 {gap or '未提供'}")
    if "scenario_direction" in wanted:
        # 单因素方向：收入升→利润升、毛利率降→利润降（做反了立刻发现）
        diag = payload.get("diagnostics") or {}
        _add("scenario_direction", bool(diag.get("direction_ok")),
             "收入 +5% 应为升、毛利率 -1pp 应为降")
    failed = [k for k, v in checks.items() if not v["ok"]]
    return {"ok": not failed, "checks": checks, "failed": failed}


def tamper_check(output_value, expected_value, *, tolerance: float = 0.005) -> bool:
    """输出被改成别的数（正文/底稿/包任一环节）能否被发现 → True = 一致。"""
    return _close(Decimal(str(output_value)), Decimal(str(expected_value)),
                  Decimal(str(tolerance)))


def dataset_changed(old_hash: str, dataset) -> bool:
    """数据集是否已变（= 旧结果必须过期）。"""
    return str(old_hash) != str(dataset.dataset_hash)


def unusable_inputs(dataset, facts) -> list[str]:
    """列出被拒的输入及其原因（未知/冲突/无效/真实零分开说）。"""
    out: list[str] = []
    for f in facts or ():
        o = dataset.get(str(getattr(f, "metric", "")), str(getattr(f, "period", "")))
        if o is None:
            out.append(f"{getattr(f, 'metric', '')} {getattr(f, 'period', '')}：无可用观察")
        elif not o.usable:
            out.append(f"{o.metric} {o.period}：{o.state}"
                       + (f"（{o.note}）" if o.note else ""))
        elif o.state == State.ZERO:
            out.append(f"{o.metric} {o.period}：真实零（有值，参与计算但方向性叙述需谨慎）")
    return out
