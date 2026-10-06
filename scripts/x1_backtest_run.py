# -*- coding: utf-8 -*-
"""Y2b 端到端：**真实日线**上跑固定规则组合回测，输出账本/净值/指标/审计 + Qlib 导出。

用法（主环境）：
    python scripts/x1_backtest_run.py --dataset <dataset_id> \
        --out docs/evidence/y2b_backtest_20261005.json

规则（**冻结、无拟合参数**，见 `quant_research.backtest.DEFAULT_SPEC`）：
等权、月度再平衡（每月第一个交易日）、信号只看规则、成交在**当日开盘** ± 滑点；
留出集起点在 spec 里**事先固定**（`holdout_start`），不是事后挑。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters import market_history as mh                      # noqa: E402
from quant_research import backtest as bt                      # noqa: E402
from quant_research import qlib_adapter as qa                  # noqa: E402
from adapters import trading_calendar as tc                    # noqa: E402

SPEC = {
    "name": "equal_weight_monthly_rebalance_v1",
    "universe": ["002304", "600031"],
    "rebalance": "monthly",
    "weighting": "equal",
    "execution": "next_open",
    "lot_size": 100,
    "initial_cash": 1_000_000.0,
    "cash_buffer": 0.0,
    "holdout_start": "2025-01-01",          # 事先固定，不事后挑
}
COSTS = dict(bt.DEFAULT_COSTS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="")
    ap.add_argument("--benchmark", default="000300")
    ap.add_argument("--out", default="docs/evidence/y2b_backtest_20261005.json")
    ap.add_argument("--qlib-export", default="")
    args = ap.parse_args()

    payload = mh.load(args.dataset)
    cal = tc.load()                 # 有独立日历就把停牌与休市分开（不再靠基准代理）
    res = bt.run(payload, SPEC, costs=COSTS, benchmark=args.benchmark,
                 license=str(payload.get("license") or ""), calendar=cal)
    doc = {"dataset": {"dataset_id": payload.get("dataset_id"),
                       "rows": payload.get("rows"), "date_range": payload.get("date_range"),
                       "instruments": payload.get("instruments"),
                       "license": payload.get("license"),
                       "adj_basis": payload.get("adj_basis")},
           "qlib": qa.describe(), "backtest": res}
    if args.qlib_export:
        Path(args.qlib_export).mkdir(parents=True, exist_ok=True)
        doc["qlib_export"] = qa.export_qlib_csv(payload, args.qlib_export,
                                                benchmark=args.benchmark)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    m = res.get("metrics") or {}
    print(json.dumps({"status": res.get("status"), "reason": res.get("reason", ""),
                      "spec_hash": res.get("spec_hash"),
                      "reading_hash": res.get("reading_hash"),
                      "calendar_basis": res.get("calendar_basis"),
                      "delivery_eligible": res.get("delivery_eligible"),
                      "audit_ok": (res.get("audit") or {}).get("ok"),
                      "audit_failed": (res.get("audit") or {}).get("failed"),
                      "offline_redirect": False, "out": args.out},
                     ensure_ascii=False, indent=1)[:800])
    if m:
        print(json.dumps({
            "days": m["days"], "start": m["start"], "end": m["end"],
            "total_return": m["total_return"], "nocost_total_return": m["nocost_total_return"],
            "cost_drag": m["cost_drag"], "annualized_return": m["annualized_return"],
            "max_drawdown": m["max_drawdown"], "avg_exposure": m["avg_exposure"],
            "orders": m["orders"], "turnover_ratio": m["turnover_ratio"],
            "fees": m["fees"], "benchmark": m["benchmark"]},
            ensure_ascii=False, indent=1))
        h = res.get("holdout") or {}
        print(json.dumps({"holdout": h}, ensure_ascii=False, indent=1))
        print(f"拒绝记录 {len(res.get('rejections') or [])} 条：")
        for r in (res.get("rejections") or [])[:8]:
            print(f"  {r['date']} {r['code']} {r['side']}: {r['reason']}")
        print(f"成交 {len(res.get('orders') or [])} 笔，前 3 笔：")
        for o in (res.get("orders") or [])[:3]:
            print(f"  {o['date']} {o['code']} {o['side']} {o['qty']}股 @{o['price']} "
                  f"佣金{o['commission']} 印花{o['stamp_duty']} 滑点成本{o['slippage_cost']}")
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
