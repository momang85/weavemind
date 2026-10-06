# -*- coding: utf-8 -*-
"""Y2a 验收项：**含"更正/重述"的手算小样**（架构规划 §12.4 的现实案例）。

为什么用京蓝：材料库里已有一份 **更正后** 年报 ——
`000711.SZ 京蓝科技股份有限公司2020年年度报告（更正后）`，披露日 **2025-09-05**，
`material_id=2db15147bad934f9`，`date_basis=source_url_format`。原报告是 2021 年披露的，
更正版 2025 年才出来 —— 这正是"**后来的更正不得倒灌成当时已知**"的实例。

本脚本做三件事，全部**只读**、不调模型、不联网：

1. **事件日从材料证据取**（`event_calendar.load_materials`），不手打；
2. **两段语义各演示一次**：
   - 特征侧：把 `feature_cutoff` 设在 2021-06-30（更正版出现之前），锚点必须 ≤ 该日 —— 证明
     2025 年的更正信息进不了"当时的判断"；
   - 结果侧：事件＝更正版披露 2025-09-05，`t0` 取**之后第一个交易日**（此日若为交易日，
     当日收盘**只作价格起点、不作 t0**）——这就是"盘后公告"的保守处理。
3. **手算小样（gold）**：用 `Decimal` 从**原始收盘价**独立手算 `t+1/+5/+20` 的个股收益、
   基准收益与几何超额，与算子输出**逐点比对**；不一致就非零退出（不静默）。
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters import market_history as mh                      # noqa: E402
from adapters import trading_calendar as tc                    # noqa: E402
from quant_research import event_calendar as ec                # noqa: E402
from quant_research import event_returns as er                 # noqa: E402

Q = lambda x: Decimal(str(x)).quantize(Decimal("0.000001"))    # noqa: E731


def hand_compute(payload: dict, code: str, event_date: str, benchmark: str, offsets) -> dict:
    """**独立手算**：直接从载荷的收盘价推窗口收益（不走算子的切片/聚合代码）。"""
    def ser(c):
        rows = [r for r in payload["data"] if str(r["code"]) == str(c)]
        rows.sort(key=lambda r: str(r["date"]))
        return [(str(r["date"])[:10], float(r["close"])) for r in rows]

    sub, ben = ser(code), ser(benchmark)
    before = [i for i, (d, _) in enumerate(sub) if d <= event_date]
    anchor_i = before[-1]
    base_i = anchor_i + 1
    a_sub, b_sub = sub[anchor_i], sub[base_i]
    b_map = dict(ben)
    a_ben = b_map[a_sub[0]]
    out = {"anchor_date": a_sub[0], "anchor_close": a_sub[1], "t0_date": b_sub[0],
           "points": {}}
    for off in offsets:
        i = base_i + off
        d, c = sub[i]
        r = Q(Decimal(str(c)) / Decimal(str(a_sub[1])) - 1)
        br = Q(Decimal(str(b_map[d])) / Decimal(str(a_ben)) - 1)
        ex = Q((1 + r) / (1 + br) - 1)
        out["points"][off] = {"date": d, "close": c, "return": float(r),
                              "benchmark_return": float(br), "excess_return": float(ex)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--materials-dir", required=True)
    ap.add_argument("--code", default="000711")
    ap.add_argument("--benchmark", default="000300")
    ap.add_argument("--feature-cutoff", default="2021-06-30",
                    help="更正版出现之前的「当时的判断」截点")
    ap.add_argument("--out", default="docs/evidence/y2a_jinglan_restatement_sample_20261005.json")
    args = ap.parse_args()

    payload = mh.load(args.dataset)
    cal = ec.from_records(ec.load_materials(args.materials_dir), codes=[args.code])
    events = ec.to_event_list(cal)
    if not events:
        print(json.dumps({"ok": False, "reason": "材料库里没有该标的的可用事件",
                          "excluded": cal["excluded"]}, ensure_ascii=False, indent=1))
        return 1
    ev = events[0]

    cal = tc.load()      # 独立交易日历：把"披露日那天停牌"与"休市"严格分开
    # ① 结果侧：事件日之后第一个交易日为 t0（披露日当日只作价格起点）
    outcome = er.compute(payload, [ev], benchmark=args.benchmark, offsets=(0, 1, 5, 20),
                         calendar=cal)
    # ② 特征侧：截点设在更正版之前 —— 锚点必须 ≤ 截点，证明更正信息进不了当时的判断
    feature = er.compute(payload, [ev], benchmark=args.benchmark, offsets=(0,),
                         feature_cutoff=args.feature_cutoff, calendar=cal)

    it = (outcome.get("readings") or [{}])[0]
    fit = (feature.get("readings") or [{}])[0]
    gold = hand_compute(payload, args.code, ev["date"], args.benchmark, (1, 5, 20))

    diffs: list[str] = []
    for off in (1, 5, 20):
        got = next((p for p in it.get("points") or [] if p["offset"] == off), {})
        want = gold["points"][off]
        for k in ("return", "benchmark_return", "excess_return"):
            if got.get(k) is None or abs(float(got[k]) - want[k]) > 1e-9:
                diffs.append(f"t{off:+d}.{k}: 算子={got.get(k)} 手算={want[k]}")
        if got.get("date") != want["date"]:
            diffs.append(f"t{off:+d}.date: 算子={got.get('date')} 手算={want['date']}")

    leak = bool(fit.get("anchor", {}).get("date", "") > args.feature_cutoff)
    if leak:
        diffs.append(f"特征泄漏：锚点 {fit['anchor']['date']} 晚于截点 {args.feature_cutoff}")

    doc = {"schema": "weavemind.y2a_restatement_sample/0",
           "case": {"code": args.code, "material_id": ev.get("_material_id"),
                    "material_url": ev.get("_url"), "date_basis": ev.get("_date_basis"),
                    "disclosed_at": ev["date"], "label": ev["label"]},
           "dataset": {"dataset_id": payload.get("dataset_id"), "rows": payload.get("rows"),
                       "date_range": payload.get("date_range"),
                       "license": payload.get("license"), "adj_basis": payload.get("adj_basis")},
           "outcome": {"status": outcome.get("status"), "t0_date": it.get("t0_date"),
                       "anchor": it.get("anchor"), "window_state": it.get("window_state"),
                       "points": it.get("points"), "kind": outcome.get("kind"),
                       "executable": outcome.get("executable"),
                       "suspension_suspect": it.get("suspension_suspect"),
                       "calendar_proxy_used": outcome.get("calendar_proxy_used", ""),
                       "reading_hash": outcome.get("reading_hash")},
           "feature_side": {"feature_cutoff": args.feature_cutoff,
                            "anchor": fit.get("anchor"), "t0_date": fit.get("t0_date"),
                            "no_lookahead": not leak},
           "calendar": {"dataset_id": cal.get("dataset_id"), "range": cal.get("range"),
                        "ok": tc.ok(cal),
                        "suspension_dates": it.get("suspension_dates"),
                        "suspension_certain": it.get("suspension_certain")},
           "hand_computed": gold, "gold_diffs": diffs,
           "note": ("披露只有日精度：不臆造盘中时间，t0 取披露日之后第一个交易日；"
                    "披露日当日收盘仅作价格起点。事件反应统计（不可执行）与可执行收益分开标识。")}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    print(json.dumps({"case": doc["case"]["label"], "disclosed_at": ev["date"],
                      "t0_date": it.get("t0_date"), "anchor": it.get("anchor"),
                      "window_state": it.get("window_state"),
                      "suspension_suspect": it.get("suspension_suspect"),
                      "feature_anchor": fit.get("anchor"), "no_lookahead": not leak,
                      "gold_diffs": diffs, "out": args.out}, ensure_ascii=False, indent=1))
    for p in it.get("points") or []:
        g = lambda v: ("--" if v is None else f"{v * 100:+.2f}%")     # noqa: E731
        print(f"  t{p['offset']:+d} {p['date']} close={p['close']} state={p['state']} "
              f"个股={g(p['return'])} 基准={g(p['benchmark_return'])} 超额={g(p['excess_return'])}")
    return 0 if not diffs else 1


if __name__ == "__main__":
    raise SystemExit(main())
