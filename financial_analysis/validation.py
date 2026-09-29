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

from .contracts import State, _canonical
from .registry import OPERATORS


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
        total = sum(Decimal(str(c.get("value") or 0)) for c in comps)
        base = Decimal(str(first.get("value") or 0))
        _add("identity", _close(total, base, _tolerance(spec)),
             f"贡献项合计 {total} vs 总量 {base}")
    if "unit" in wanted:
        units = {str(o.get("unit") or "") for o in (payload.get("outputs") or [])}
        _add("unit", len(units) == 1 and "" not in units, f"输出单位不一致：{sorted(units)}")
    if "no_pp_substitution" in wanted:
        # 金额桥**不得**把"百分点差"当金额项：任一贡献项的单位不能是百分比
        pct = [c for o in (payload.get("outputs") or [])
               for c in (o.get("components") or []) if str(c.get("unit") or "").endswith("%")]
        _add("no_pp_substitution", not pct,
             "" if not pct else f"金额桥里出现百分比贡献项：{[c['label'] for c in pct]}")
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
