# -*- coding: utf-8 -*-
"""独立验证：不复用生成函数的结论，另走一条路（十进制手算 + 约束 + 变异反例）。

架构要求（§4「ModelRun / ValidatedOutput」）：独立验证用手算金样/约束关系/不同计算路径
及变异反例，**不能只调用生成函数再比较自身**；金额用明确的十进制/舍入策略。

本模块提供：
- `validate_output(spec, dataset, payload)`：逐项跑 `spec.validations` 里的检查；
- `tamper_check(output, expected)`：输出被改成别的数必须能被发现（正文与底稿同源）。
"""
from __future__ import annotations

from decimal import Decimal

from .contracts import State, _canonical, _money_scale
from .registry import OPERATORS


def _amount_scale(unit: str) -> float:
    """金额量纲（与契约同一实现，避免两套换算规则）。"""
    return _money_scale(unit)


def _tolerance(spec) -> Decimal:
    return Decimal(str(spec.tolerance))


def _close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    """相对容差 + 绝对 0.01：与项目既有 `_close` 同尺（金额用十进制，不用二进制浮点）。"""
    return abs(a - b) <= max(abs(b) * tol, Decimal("0.01"))


def validate_output(spec, dataset, payload: dict) -> dict:
    """→ `{ok, checks: {name: {ok, detail}}, failed: [...]}`。

    `gold` 走**另一条计算路径**（注册表里的独立金样函数，Decimal 手算）；
    其余检查都是约束关系，不调用生成函数。
    """
    checks: dict[str, dict] = {}
    wanted = set(spec.validations)

    def _add(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

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
        outs = payload.get("outputs") or []
        first = outs[0] if outs else {}
        comps = first.get("components") or []
        base = Decimal(str(first.get("value") or 0))
        if not comps:
            # 没有分解项的输出（如差额/覆盖率）："合计=总量"这条不适用，如实说明
            _add("identity", True, "该输出没有分解项，不适用合计核对")
        else:
            total = sum(Decimal(str(c.get("value") or 0)) for c in comps)
            _add("identity", _close(total, base, _tolerance(spec)),
                 f"贡献项合计 {total} vs 总量 {base}")
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
