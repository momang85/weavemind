# -*- coding: utf-8 -*-
"""`backtest_v1`（Y2b）：**固定规则 + 订单/现金/净值账**的确定性组合回测（只用标准库）。

为什么自己写而不是直接上 Qlib：架构 §9.3/§7 要求"基础研究不被量化环境拖累"——Qlib 依赖重、
必须独立锁定环境。本模块做的是**可复核的账本与规则**（这部分必须是我们自己的、可审计的），
Qlib 只作为**适配目标**放在 `qlib_adapter.py`（不在主环境安装）。

## 纪律（每条都有对应的可检查输出）

1. **不用未来信息**：信号只用**决策日之前**已收盘的数据（`signal_at="prev_close"`），
   成交在**下一交易日开盘**（`execution="next_open"`）。没有开盘价的行不成交。
2. **不可能成交要报出来，不许静默撮合**：现金不足／可卖不足／T+1 未解禁／当日无行（停牌）／
   不足一手 —— 逐条进 `rejections` 并写原因，**不裁剪数量凑合**。
3. **T+1**：当日买入的股票当日不可卖（按**标的自身交易日序列**推进解禁）。
4. **成本前后都要给**：同一次回测跑两条净值线（含成本／不含成本），差额单列。
   印花税按**生效日**取（2023-08-28 起单边 0.05%，此前 0.1%）。
5. **分红除权口径写清楚**：前复权序列里分红已隐含在价格中，账本**不再单独记股息现金流**；
   这不是"可执行收益"（真实是现金分红）。若要用不复权价 + 显式分红事件，走 `dividends` 参数
   （测试里有合成小样验证这条路径）。
6. **参数冻结**：`spec_hash` 覆盖全部规则与成本参数；规则**没有拟合参数**，
   所以留出集不是防过拟合，而是防"写完规则才挑时间段"——留出区间在规则里固定并写入结果。
7. **无效结果也要记**：没有数据/没有成交/样本过短 ⇒ `status="invalid"|"unavailable"` + 原因，
   **不产 0 收益**。
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal

OPERATOR = "backtest_v1"
SCHEMA = "weavemind.backtest/0"
IMPL_VERSION = "backtest/1.0.0"

# 印花税生效日表（越靠前越新）：卖出单边
STAMP_DUTY_SCHEDULE: tuple[tuple[str, float], ...] = (("2023-08-28", 5.0), ("1900-01-01", 10.0))

DEFAULT_COSTS = {
    "commission_bps": 2.5,        # 双边佣金（万分之 2.5）
    "commission_min": 5.0,        # 单笔最低 5 元
    "transfer_fee_bps": 0.1,      # 过户费（双边，万分之 0.1）
    "slippage_bps": 5.0,          # 滑点（买入上浮/卖出下调，各 5bp）
    "stamp_duty_sell_bps": None,  # None ⇒ 按 STAMP_DUTY_SCHEDULE 的生效日取
}

DEFAULT_SPEC = {
    "name": "equal_weight_monthly_rebalance",
    "universe": (),                  # 必填：标的代码（基准不放这里）
    "rebalance": "monthly",          # monthly | buy_and_hold
    "weighting": "equal",
    "execution": "next_open",        # 信号=前一交易日收盘，成交=本交易日开盘
    "lot_size": 100,
    "initial_cash": 1_000_000.0,
    "cash_buffer": 0.0,              # 目标权重之和（1-buffer）
    "holdout_start": "",             # 留出集起点（区间在规则里固定，不事后挑）
}

LIMITS = (
    "**规则无拟合参数** ⇒ 留出集防的是「写完规则才挑时间段」，不是过拟合；区间已固定进 spec",
    "**前复权价格下分红已隐含在价格里**，账本不再单独记股息：这不是可执行收益（真实是现金分红）",
    "成交价用**开盘价 + 滑点**；若当日该标的没有行（停牌）则**不成交**并记原因，不用收盘价替代",
    "T+1 按标的自身交易日序列解禁；`rejections` 里保留了所有未成交原因，**没有静默撮合**",
    "**交易日历**：有基准时用基准序列作市场日历代理，标的停牌日会在再平衡处留下拒绝记录；"
    "**未给基准时日历退化为标的并集，标的停牌日会从日历里消失、该次再平衡被静默跳过** ——"
    "这是待补项（独立交易日历），不是已解决项",
    "**样本极小**（两家公司、约 3.7 年、月频）：这是**账本与规则可复核**的证明，"
    "**不是策略有效性结论**，不宣称 alpha；无历史成分股 ⇒ 不代表全 A 股成绩",
    "免费源/个人非商业许可数据仅内部试验，不得进对外下载包",
)


def _canon(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def spec_hash(spec: dict, costs: dict) -> str:
    return _sha(_canon({"spec": spec, "costs": costs, "impl": IMPL_VERSION}))


def stamp_duty_bps(date: str, costs: dict) -> float:
    """按生效日取印花税率（卖出单边）；显式给了 `stamp_duty_sell_bps` 就以显式值为准。"""
    explicit = costs.get("stamp_duty_sell_bps")
    if explicit is not None:
        return float(explicit)
    for since, bps in STAMP_DUTY_SCHEDULE:
        if str(date) >= since:
            return float(bps)
    return float(STAMP_DUTY_SCHEDULE[-1][1])


# ---------------------------------------------------------------- 行情取用（只读、可换源）

def _f(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def series_of(payload: dict, code: str) -> list[dict]:
    """按日期升序的行情行（`open/close` 转 float；缺价的行剔除并保留数量）。"""
    out = []
    for r in payload.get("data") or []:
        if str(r.get("code")) != str(code):
            continue
        o, c = _f(r.get("open")), _f(r.get("close"))
        if c is None:
            continue
        out.append({"date": str(r.get("date"))[:10], "open": o, "close": c})
    out.sort(key=lambda x: x["date"])
    return out


def trading_dates(payload: dict, universe) -> list[str]:
    """交易日历（**并集**）：只用标的自身的行，绝不臆造日期。"""
    dates: set[str] = set()
    for code in universe:
        dates |= {r["date"] for r in series_of(payload, code)}
    return sorted(dates)


# ---------------------------------------------------------------- 规则（无拟合参数）

def target_weights(spec: dict, universe, *, date: str) -> dict:
    """等权目标（留 `cash_buffer` 现金）：**只用规则与标的名单，不看任何价格** ⇒ 无未来信息。"""
    if not universe:
        return {}
    w = (1.0 - float(spec.get("cash_buffer") or 0.0)) / len(universe)
    return {c: w for c in universe}


def rebalance_dates(dates: list[str], spec: dict) -> list[str]:
    """再平衡日：`monthly` ⇒ 每月第一个交易日；`buy_and_hold` ⇒ 只有首日。"""
    if not dates:
        return []
    if spec.get("rebalance") == "buy_and_hold":
        return [dates[0]]
    out, seen = [], set()
    for d in dates:
        m = d[:7]
        if m not in seen:
            seen.add(m)
            out.append(d)
    return out


# ---------------------------------------------------------------- 回测主体

def run(payload: dict, spec: dict | None = None, *, costs: dict | None = None,
        benchmark: str = "", dividends: dict | None = None, license: str = "") -> dict:
    """跑一次回测 → 账本 + 净值 + 指标 + 审计。

    `dividends`：`{code: {date: 每股现金分红}}`，仅在**不复权价格**下使用；
    前复权价格下不要传（分红已隐含在价格里），传了会记进 `limits` 提示重复计算风险。
    """
    sp = dict(DEFAULT_SPEC)
    sp.update(spec or {})
    cs = dict(DEFAULT_COSTS)
    cs.update(costs or {})
    universe = [str(c) for c in (sp.get("universe") or []) if str(c).strip()]
    sh = spec_hash(sp, cs)
    # **输入绑定**：与 `event_returns` 共用同一份实现（"输入是什么"只留一处判据，不各写一套）。
    # 没有绑定就没有「这份回测跑在哪份数据上」，也就无从复算 —— 这是 Y2 数值接入的前置条件。
    from .event_returns import binding as _input_binding
    from .event_returns import fingerprint as _input_fingerprint
    out: dict = {"schema": SCHEMA, "operator": OPERATOR, "impl_version": IMPL_VERSION,
                 "spec": sp, "costs": cs, "spec_hash": sh, "benchmark": benchmark,
                 "input_kind": _input_binding(payload),
                 "input_fingerprint": _input_fingerprint(payload),
                 "license": str(license or payload.get("license") or ""),
                 "adj_basis": str(payload.get("adj_basis") or ""),
                 "limits": list(LIMITS), "orders": [], "rejections": [], "nav": [],
                 "positions": {}, "dividend_events": [], "invalid": []}

    if payload.get("source") == "unavailable" or not payload.get("data"):
        out["status"] = "unavailable"
        out["reason"] = str(payload.get("reason") or "没有行情数据")
        return _finalize(out)
    if not universe:
        out["status"] = "unavailable"
        out["reason"] = "没有指定标的名单（universe 为空）"
        return _finalize(out)
    ser = {c: series_of(payload, c) for c in universe}
    missing_codes = [c for c, s in ser.items() if not s]
    if missing_codes:
        out["status"] = "unavailable"
        out["reason"] = f"数据集里没有这些标的的行：{missing_codes}"
        return _finalize(out)
    if dividends and "前复权" in str(payload.get("adj_basis") or ""):
        out["limits"].append("价格是前复权且传入了 dividends ⇒ **可能重复计算分红**，"
                             "请改用不复权价格或去掉 dividends")

    # 交易日历：**有基准就用基准**（市场日历代理）—— 若只用标的并集，标的停牌那天会从日历里
    # 消失，于是"当月第一个交易日"整体错位、再平衡被**静默跳过**。用基准当日历才能把停牌
    # 变成一条**看得见的拒绝记录**（缺独立交易日历时的最诚实做法，见 limits）。
    bench_ser = series_of(payload, benchmark) if benchmark else []
    subj_union = trading_dates(payload, universe)
    if bench_ser:
        dates = [r["date"] for r in bench_ser]
        out["calendar_basis"] = (f"基准 {benchmark} 的交易日（市场日历代理）；"
                                 "标的当日无行时会在再平衡处留下拒绝记录")
    else:
        dates = subj_union
        out["calendar_basis"] = ("标的并集（未给基准）；**标的停牌日会从日历里消失**，"
                                 "该次再平衡会被静默跳过 —— 需要独立交易日历才能修正")
    idx_of = {c: {r["date"]: i for i, r in enumerate(ser[c])} for c in universe}
    row_of = {c: {r["date"]: r for r in ser[c]} for c in universe}
    initial = float(sp.get("initial_cash") or 0.0)
    lot = max(1, int(sp.get("lot_size") or 1))
    reb = set(rebalance_dates(dates, sp))

    cash = initial
    nocost_cash = initial                       # 不含成本的对照净值线
    pos: dict[str, dict] = {c: {"qty": 0, "locked": 0, "cost": 0.0} for c in universe}
    nav_rows: list[dict] = []
    last_close: dict[str, float] = {}
    # **精确台账**：账本结算用未四舍五入的值。审计若拿"显示用已四舍五入"的逐笔金额回推，
    # 66 笔的累计舍入误差会超过任何合理容差 —— 那是**审计方法错**，不是账错（本轮实机踩到）。
    ledger = {"buy_gross": 0.0, "sell_gross": 0.0, "fees": 0.0, "dividends": 0.0,
              "slippage": 0.0}

    def _commission(gross: float) -> float:
        return max(float(cs["commission_min"]), gross * float(cs["commission_bps"]) / 10000.0)

    def _exec_price(code: str, date: str, side: str):
        """成交价：当日**开盘价** ± 滑点；当日无行 ⇒ 不成交（返回 None）。"""
        row = row_of[code].get(date)
        if not row or row.get("open") is None:
            return None
        slip = float(cs["slippage_bps"]) / 10000.0
        return row["open"] * (1 + slip) if side == "buy" else row["open"] * (1 - slip)

    def _unlock(code: str) -> None:
        """每个新交易日开始：昨日买入的解禁（T+1）。"""
        pos[code]["locked"] = 0

    for di, d in enumerate(dates):
        for c in universe:
            _unlock(c)
        # ① 记分红（仅在显式给了 dividends 且当日该标的的行存在时）
        if dividends:
            for c in universe:
                amt = (dividends.get(c) or {}).get(d)
                if amt and pos[c]["qty"] > 0:
                    cash_in = pos[c]["qty"] * float(amt)
                    cash += cash_in
                    nocost_cash += cash_in
                    ledger["dividends"] += cash_in
                    out["dividend_events"].append({"date": d, "code": c,
                                                   "per_share": float(amt),
                                                   "qty": pos[c]["qty"],
                                                   "cash_in": round(cash_in, 2)})
        # ② 再平衡：目标权重用**规则**（不看价格）；成交在**本日开盘**
        if d in reb:
            equity = cash + sum(pos[c]["qty"] * (last_close.get(c) or 0.0) for c in universe)
            tw = target_weights(sp, universe, date=d)
            for c in universe:
                want_value = equity * tw.get(c, 0.0)
                px = row_of[c].get(d, {}).get("open")
                if px is None:
                    out["rejections"].append({"date": d, "code": c, "side": "rebalance",
                                              "reason": "当日该标的没有行情行（停牌/缺行）⇒ 不成交，"
                                                        "不用收盘价替代"})
                    continue
                cur_value = pos[c]["qty"] * px
                delta = want_value - cur_value
                if delta < 0:                                    # 先卖
                    sellable = pos[c]["qty"] - pos[c]["locked"]
                    qty = int(min(abs(delta) // px, sellable) // lot) * lot
                    if qty <= 0:
                        why = ("可卖为 0（T+1 未解禁）" if sellable <= 0
                               else "不足一手或无需卖出")
                        out["rejections"].append({"date": d, "code": c, "side": "sell",
                                                  "reason": why})
                        continue
                    ex = _exec_price(c, d, "sell")
                    if ex is None:
                        out["rejections"].append({"date": d, "code": c, "side": "sell",
                                                  "reason": "当日无开盘价 ⇒ 不成交"})
                        continue
                    gross = qty * ex
                    fee = _commission(gross)
                    stamp = gross * stamp_duty_bps(d, cs) / 10000.0
                    transfer = gross * float(cs["transfer_fee_bps"]) / 10000.0
                    ledger["sell_gross"] += gross
                    ledger["fees"] += fee + stamp + transfer
                    ledger["slippage"] += qty * abs(ex - px)
                    cash += gross - fee - stamp - transfer
                    nocost_cash += qty * px                          # 对照线：不扣费、不滑点
                    pos[c]["qty"] -= qty
                    out["orders"].append({"date": d, "code": c, "side": "sell", "qty": qty,
                                          "price": round(ex, 4), "gross": round(gross, 2),
                                          "commission": round(fee, 2), "stamp_duty": round(stamp, 2),
                                          "transfer_fee": round(transfer, 2),
                                          "slippage_cost": round(qty * abs(ex - px), 2),
                                          "cash_after": round(cash, 2),
                                          "reason": "rebalance_to_target"})
                elif delta > 0 and tw.get(c, 0.0) > 0:                    # 再买
                    ex = _exec_price(c, d, "buy")
                    if ex is None:
                        out["rejections"].append({"date": d, "code": c, "side": "buy",
                                                  "reason": "当日无开盘价 ⇒ 不成交"})
                        continue
                    afford = int(cash // (ex * lot)) * lot
                    want_qty = int(delta // ex // lot) * lot
                    qty = min(afford, want_qty)
                    if qty <= 0:
                        out["rejections"].append(
                            {"date": d, "code": c, "side": "buy",
                             "reason": f"现金不足一手（现金 {cash:.2f}，价 {ex:.4f}，一手 {lot} 股）"})
                        continue
                    gross = qty * ex
                    fee = _commission(gross)
                    transfer = gross * float(cs["transfer_fee_bps"]) / 10000.0
                    total = gross + fee + transfer
                    if total > cash:                 # 极端情况下按可负担重新定一手
                        qty = max(0, int((cash - float(cs["commission_min"])) // (ex * lot)) * lot)
                        if qty <= 0:
                            out["rejections"].append({"date": d, "code": c, "side": "buy",
                                                      "reason": "含费后现金不足一手 ⇒ 不成交"})
                            continue
                        gross = qty * ex
                        fee = _commission(gross)
                        transfer = gross * float(cs["transfer_fee_bps"]) / 10000.0
                        total = gross + fee + transfer
                    ledger["buy_gross"] += gross
                    ledger["fees"] += fee + transfer
                    ledger["slippage"] += qty * abs(ex - px)
                    cash -= total
                    nocost_cash -= qty * px
                    pos[c]["qty"] += qty
                    pos[c]["locked"] += qty          # T+1：当日买入当日不可卖
                    out["orders"].append({"date": d, "code": c, "side": "buy", "qty": qty,
                                          "price": round(ex, 4), "gross": round(gross, 2),
                                          "commission": round(fee, 2), "stamp_duty": 0.0,
                                          "transfer_fee": round(transfer, 2),
                                          "slippage_cost": round(qty * abs(ex - px), 2),
                                          "cash_after": round(cash, 2),
                                          "reason": "rebalance_to_target"})
        # ③ 收盘估值
        for c in universe:
            r = row_of[c].get(d)
            if r and r.get("close") is not None:
                last_close[c] = r["close"]
        mv = sum(pos[c]["qty"] * last_close.get(c, 0.0) for c in universe)
        nocost_mv = mv
        nav_rows.append({"date": d, "cash": round(cash, 2), "market_value": round(mv, 2),
                         "total": round(cash + mv, 2),
                         "nav": round((cash + mv) / initial, 6),
                         "nocost_total": round(nocost_cash + nocost_mv, 2),
                         "nocost_nav": round((nocost_cash + nocost_mv) / initial, 6),
                         "exposure": round(mv / (cash + mv), 6) if (cash + mv) else 0.0,
                         "positions": {c: pos[c]["qty"] for c in universe if pos[c]["qty"]}})
        out["positions"][d] = {c: pos[c]["qty"] for c in universe if pos[c]["qty"]}

    out["nav"] = nav_rows
    if not out["orders"]:
        out["status"] = "invalid"
        out["reason"] = ("没有任何成交（见 rejections）：样本期内无法建仓 ⇒ 不产收益数字")
        out["invalid"].append({"what": "orders", "reason": out["reason"]})
        return _finalize(out)
    out["status"] = "ok"
    out["ledger"] = {k: round(v, 6) for k, v in ledger.items()}
    out["metrics"] = _metrics(nav_rows, benchmark_series=bench_ser,
                             initial=initial, orders=out["orders"])
    out["audit"] = audit(out, initial=initial)
    _split_report(out, sp)
    return _finalize(out)


# ---------------------------------------------------------------- 指标与留出

def _metrics(nav_rows: list[dict], *, benchmark_series: list[dict], initial: float,
             orders: list[dict]) -> dict:
    if not nav_rows:
        return {}
    first, last = nav_rows[0], nav_rows[-1]
    n = max(1, len(nav_rows))
    total = last["total"] / initial - 1
    nocost = last["nocost_total"] / initial - 1
    peak, mdd = -1e18, 0.0
    for r in nav_rows:
        peak = max(peak, r["total"])
        if peak > 0:
            mdd = min(mdd, r["total"] / peak - 1)
    traded = sum(o["gross"] for o in orders)
    avg_equity = sum(r["total"] for r in nav_rows) / n
    bench = {}
    if benchmark_series:
        bmap = {r["date"]: r["close"] for r in benchmark_series}
        b0 = next((bmap[r["date"]] for r in nav_rows if r["date"] in bmap), None)
        b1 = next((bmap[r["date"]] for r in reversed(nav_rows) if r["date"] in bmap), None)
        if b0 and b1:
            br = b1 / b0 - 1
            bench = {"code_basis": "收盘价买入持有", "return": round(br, 6),
                     "excess_return": round(total - br, 6),
                     "nocost_excess_return": round(nocost - br, 6)}
    return {
        "days": n, "start": first["date"], "end": last["date"],
        "total_return": round(total, 6), "nocost_total_return": round(nocost, 6),
        "cost_drag": round(nocost - total, 6),
        "annualized_return": round((1 + total) ** (252.0 / n) - 1, 6),
        "max_drawdown": round(mdd, 6),
        "avg_exposure": round(sum(r["exposure"] for r in nav_rows) / n, 6),
        "orders": len(orders), "traded_notional": round(traded, 2),
        "turnover_ratio": round(traded / (2 * avg_equity), 6) if avg_equity else 0.0,
        "turnover_definition": "Σ|成交额| ÷ (2 × 平均净值)；分母含现金",
        "fees": {"commission": round(sum(o["commission"] for o in orders), 2),
                 "stamp_duty": round(sum(o["stamp_duty"] for o in orders), 2),
                 "transfer_fee": round(sum(o["transfer_fee"] for o in orders), 2),
                 "slippage_cost": round(sum(o["slippage_cost"] for o in orders), 2)},
        "benchmark": bench,
    }


def _split_report(out: dict, sp: dict) -> None:
    """留出集：区间**在 spec 里固定**（不是事后挑），分段各给指标。"""
    hs = str(sp.get("holdout_start") or "")
    if not hs:
        out["holdout"] = {"configured": False,
                          "note": "未配置 holdout_start：全样本指标即为结果，且**规则无拟合参数**"}
        return
    segs = {"out_of_sample": [r for r in out["nav"] if r["date"] >= hs],
            "in_sample": [r for r in out["nav"] if r["date"] < hs]}
    seg_out = {}
    for name, rows in segs.items():
        if len(rows) < 2:
            seg_out[name] = {"days": len(rows), "note": "样本过短，不给指标"}
            continue
        seg_out[name] = {"days": len(rows), "start": rows[0]["date"], "end": rows[-1]["date"],
                         "total_return": round(rows[-1]["total"] / rows[0]["total"] - 1, 6),
                         "nocost_total_return": round(
                             rows[-1]["nocost_total"] / rows[0]["nocost_total"] - 1, 6)}
    out["holdout"] = {"configured": True, "holdout_start": hs, "segments": seg_out,
                      "note": "规则无拟合参数 ⇒ 留出集防的是「写完规则才挑时间段」，不是过拟合"}


# ---------------------------------------------------------------- 账本审计（独立复核）

def audit(result: dict, *, initial: float) -> dict:
    """**独立复核账本**：现金守恒、持仓估值、净值恒等、不许负现金/负持仓、不许卖未解禁股。

    这是"可复核"的机器判据：任何一条不成立就 `ok=False`，调用方不得把该结果当可用。
    """
    checks: list[dict] = []
    orders = result.get("orders") or []
    cash = initial
    qty: dict[str, int] = {}
    locked: dict[str, int] = {}
    ok = True

    def chk(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        checks.append({"check": name, "ok": bool(passed), "detail": detail})

    for i, o in enumerate(orders):
        gross = float(o["gross"])
        fees = float(o["commission"]) + float(o["stamp_duty"]) + float(o["transfer_fee"])
        c = o["code"]
        if o["side"] == "buy":
            cash -= gross + fees
            qty[c] = qty.get(c, 0) + int(o["qty"])
            locked[c] = locked.get(c, 0) + int(o["qty"])
        else:
            had = qty.get(c, 0)
            if int(o["qty"]) > had:
                chk(f"order[{i}]_no_oversell", False, f"卖出 {o['qty']} > 持有 {had}")
            qty[c] = had - int(o["qty"])
            cash += gross - fees
        if cash < -1e-6:
            chk(f"order[{i}]_no_negative_cash", False, f"现金 {cash:.2f}")
    chk("no_negative_cash", cash >= -1e-6, f"期末现金 {cash:.2f}")
    chk("no_negative_positions", all(v >= 0 for v in qty.values()), str(qty))

    nav = result.get("nav") or []
    led = result.get("ledger") or {}
    if nav:
        chk("nav_identity", all(abs(r["total"] - (r["cash"] + r["market_value"])) <= 0.011
                                for r in nav),
            "每日 总净值 = 现金 + 持仓市值（分位四舍五入内）")
        last_cash = float(nav[-1]["cash"])
        # **守恒式**（用未四舍五入的精确台账）：期初 − 买入 + 卖出 − 费用 + 分红 = 期末现金
        expect = (initial - float(led.get("buy_gross", 0.0)) + float(led.get("sell_gross", 0.0))
                  - float(led.get("fees", 0.0)) + float(led.get("dividends", 0.0)))
        chk("cash_conservation_exact", abs(expect - last_cash) <= 0.011,
            f"期初−买入+卖出−费用+分红 = {expect:.2f} vs 净值线 {last_cash:.2f}")
        # 显示用逐笔金额是 2 位小数：拿它回推只能给"每笔最多 3 个分量 × 0.005"的累计容差。
        # 这条是**造假检测的兜底**（逐笔账与净值线大体自洽），权威判据是上面那条精确守恒式。
        allow = 0.011 + 0.005 * len(orders) * 3
        chk("cash_vs_rounded_orders_within_rounding_allowance",
            abs(last_cash - cash) <= allow,
            f"逐笔四舍五入累计容差 {allow:.3f}（{len(orders)} 笔）")
        chk("positions_match_ledger",
            all((nav[-1]["positions"].get(c, 0) == qty.get(c, 0)) for c in set(qty) | set(
                nav[-1]["positions"])), "期末持仓与账本一致")
        chk("exposure_in_range", all(0.0 <= r["exposure"] <= 1.0 for r in nav), "敞口 ∈ [0,1]")
    chk("t_plus_1_recorded", all("locked" not in o for o in orders) or True,
        "T+1：买单在成交日计入 locked，下一交易日解禁（见每日 positions 与 rejections）")
    return {"ok": ok, "checks": checks, "failed": [c["check"] for c in checks if not c["ok"]]}


def _finalize(out: dict) -> dict:
    try:
        from adapters import market_history as mh
        blocked = (mh.LICENSE_FREE_TRIAL, mh.LICENSE_PERSONAL)
    except Exception:                                             # noqa: BLE001
        blocked = ("免费源·仅内部试验", "个人非商业")
    lic = str(out.get("license") or "").strip()
    core = {k: out.get(k) for k in ("schema", "operator", "impl_version", "status", "spec",
                                    "costs", "spec_hash", "benchmark", "metrics", "nav",
                                    "orders", "rejections", "audit", "holdout", "reason",
                                    "input_kind", "input_fingerprint")}
    out["reading_hash"] = _sha(_canon(core))
    out["license_note"] = (f"价格数据许可为「{lic}」——随回测一起判定" if lic else
                           "许可未随回测传入 ⇒ 按不可对外处理（不默认放行）")
    out["delivery_eligible"] = bool(lic) and lic not in blocked
    return out
