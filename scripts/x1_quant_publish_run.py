# -*- coding: utf-8 -*-
"""Y2 接入端到端：真实读数 → 工作区 `quant/` → 交付快照成员 → 许可门，一条链跑通。

用法（主环境）：
    python scripts/x1_quant_publish_run.py --dataset <dataset_id> --ws <工作区目录> \
        --out docs/evidence/y2_quant_publish_20261005.json

做四件事（只读行情 + 写指定工作区，不联网、不调模型）：
1. 用真实数据集跑 `event_returns`（3 个已核对事件）与 `backtest`（冻结规则）；
2. 把两条读数 `store_reading` 进工作区 `quant/`；
3. 调**交付链同一个函数** `delivery_pipeline._freeze_payload` 取包成员 —— 证明读数真的进了包；
4. 打印许可门结论（免费源 ⇒ 不允许对外）与底稿要点。

**不改写主文**：本脚本只产出附件；正文侧一行未动。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters import market_history as mh                      # noqa: E402
from adapters import trading_calendar as tc                    # noqa: E402
from quant_research import backtest as bt                      # noqa: E402
from quant_research import event_returns as er                 # noqa: E402
from quant_research import publish as pub                      # noqa: E402

EVENTS = [
    {"code": "002304", "date": "2024-04-27", "label": "洋河股份 2023 年年度报告公开"},
    {"code": "002304", "date": "2025-04-29", "label": "洋河股份 2024 年年度报告公开"},
    {"code": "600031", "date": "2025-04-18", "label": "三一重工 2024 年年度报告公开"},
]
SPEC = {"name": "equal_weight_monthly_rebalance_v1", "universe": ["002304", "600031"],
        "rebalance": "monthly", "weighting": "equal", "execution": "next_open",
        "lot_size": 100, "initial_cash": 1_000_000.0, "cash_buffer": 0.0,
        "holdout_start": "2025-01-01"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="")
    ap.add_argument("--benchmark", default="000300")
    ap.add_argument("--ws", required=True, help="工作区目录（会在其中创建 quant/）")
    ap.add_argument("--out", default="docs/evidence/y2_quant_publish_20261005.json")
    args = ap.parse_args()

    payload = mh.load(args.dataset)
    cal = tc.load()                     # 独立交易日历：停牌与休市分开
    er_reading = er.compute(payload, EVENTS, benchmark=args.benchmark, calendar=cal)
    b_reading = bt.run(payload, SPEC, benchmark=args.benchmark, calendar=cal,
                       license=str(payload.get("license") or ""))
    ws = Path(args.ws)
    ws.mkdir(parents=True, exist_ok=True)
    s1 = pub.store_reading(ws, "event_returns", er_reading)
    s2 = pub.store_reading(ws, "backtest", b_reading)

    members = pub.payload_bytes(ws)
    gate = pub.external_delivery_gate(pub.load_readings(ws))
    # 走**交付链同一个函数**，证明读数确实会被收进快照（而不是只躺在磁盘上）
    import delivery_pipeline as dp
    frozen = dp._freeze_payload("quant-e2e", ws)               # noqa: SLF001

    doc = {"dataset": {"dataset_id": payload.get("dataset_id"),
                       "rows": payload.get("rows"), "license": payload.get("license"),
                       "adj_basis": payload.get("adj_basis")},
           "readings": {"event_returns": {"reading_hash": er_reading.get("reading_hash"),
                                          "input_fingerprint": er_reading.get("input_fingerprint"),
                                          "status": er_reading.get("status")},
                        "backtest": {"reading_hash": b_reading.get("reading_hash"),
                                     "input_fingerprint": b_reading.get("input_fingerprint"),
                                     "spec_hash": b_reading.get("spec_hash"),
                                     "status": b_reading.get("status"),
                                     "audit_ok": (b_reading.get("audit") or {}).get("ok")}},
           "store": {"event_returns": s1, "backtest": s2},
           "payload_members": sorted(members),
           "frozen_snapshot_members": sorted(k for k in frozen if k.startswith("quant/")),
           "external_delivery": gate,
           "manifest": json.loads(members["quant/quant_manifest.json"].decode("utf-8"))
           if "quant/quant_manifest.json" in members else {}}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    print(json.dumps({"readings": doc["readings"],
                      "payload_members": doc["payload_members"],
                      "frozen_snapshot_members": doc["frozen_snapshot_members"],
                      "external_delivery_allowed": gate["allowed"],
                      "external_delivery_reason": gate["reason"][:160],
                      "out": args.out}, ensure_ascii=False, indent=1))
    md = members.get("quant/quant_detail.md", b"").decode("utf-8")
    print("\n--- 底稿（前 900 字）---")
    print(md[:900])
    return 0 if (doc["frozen_snapshot_members"] and not gate["allowed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
