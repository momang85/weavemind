# -*- coding: utf-8 -*-
"""Y2a 实机跑：用**已导入的真实数据集**算事件窗口基准调整收益，落读数 JSON。

只读不抓取、不付费、不动任何人工状态。用法（主环境）：

    python scripts/x1_event_returns_run.py --dataset <dataset_id>

事件来源：`--events` 可传 `docs/evidence/_y1_events.json`（两个真实披露日）；不传就用内置两条。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters import market_history as mh            # noqa: E402
from adapters import trading_calendar as tc          # noqa: E402
from quant_research import event_returns as er       # noqa: E402

# 事件日一律取**已核对证据里的公开披露日（日精度）**，不取"我记得的日期"：
#   洋河 2023 年报 @2024-04-27（事前目标来源）、洋河 2024 年报 @2025-04-29（事后实际来源）
#   三一 2024 年报 @2025-04-18
# 依据：docs/evidence/y1_002304_goal_realization_20261005.md、y1_600031_event_basis_20261005.md
# `t0` 取"披露日之后第一个交易日"是**保守约定**：披露只有日精度，同日收盘价无法证明在信息之后。
DEFAULT_EVENTS = [
    {"code": "002304", "date": "2024-04-27", "label": "洋河股份 2023 年年度报告公开"},
    {"code": "002304", "date": "2025-04-29", "label": "洋河股份 2024 年年度报告公开"},
    {"code": "600031", "date": "2025-04-18", "label": "三一重工 2024 年年度报告公开"},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="", help="market_history dataset_id（默认取最近一个）")
    ap.add_argument("--benchmark", default="000300")
    ap.add_argument("--events", default="", help="事件 JSON 文件；缺省用内置两条")
    ap.add_argument("--out", default="docs/evidence/y2_event_returns_20261005.json")
    ap.add_argument("--allow-unadjusted", action="store_true")
    args = ap.parse_args()

    payload = mh.load(args.dataset)
    cal = tc.load()                     # 有日历就按日历定位 t±N（停牌与休市分开）
    events = (json.loads(Path(args.events).read_text(encoding="utf-8"))
              if args.events else DEFAULT_EVENTS)
    reading = er.compute(payload, events, benchmark=args.benchmark,
                         allow_unadjusted=args.allow_unadjusted, calendar=cal)
    reading["calendar_input"] = {"dataset_id": cal.get("dataset_id"),
                                 "range": cal.get("range"), "license": cal.get("license"),
                                 "ok": tc.ok(cal)}
    payload_out = {"dataset_id": payload.get("dataset_id"), "source": payload.get("source"),
                   "license": payload.get("license"), "adj_basis": payload.get("adj_basis"),
                   "rows": payload.get("rows"), "date_range": payload.get("date_range"),
                   "instruments": payload.get("instruments")}
    doc = {"payload": payload_out, "events": events, "reading": reading}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"status": reading.get("status"), "reason": reading.get("reason", ""),
                      "input_fingerprint": reading.get("input_fingerprint"),
                      "reading_hash": reading.get("reading_hash"),
                      "delivery_eligible": reading.get("delivery_eligible"),
                      "overlap": reading.get("overlap"), "out": args.out},
                     ensure_ascii=False, indent=1))
    for r in reading.get("readings") or []:
        print(f"\n{r['label']}  {r['code']}@{r['event_date']}  窗口 {r['affected_window']}")
        for p in r["points"]:
            g = lambda v: ("--" if v is None else f"{v * 100:+.2f}%")   # noqa: E731
            print(f"  t{p['offset']:+d} {p['date']}  close={p['close']}  "
                  f"个股={g(p['return'])}  基准={g(p['benchmark_return'])}  "
                  f"超额={g(p['excess_return'])}")
        if r.get("benchmark_missing"):
            print(f"  基准缺行 {len(r['benchmark_missing'])} 处（超额留空，未补 0）")
    agg = reading.get("aggregate") or {}
    if agg:
        print(f"\n聚合：{json.dumps(agg, ensure_ascii=False)}")
    return 0 if reading.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
