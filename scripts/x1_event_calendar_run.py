# -*- coding: utf-8 -*-
"""Y2a 实机跑：巨潮公告 → 事件日历 → 事件窗口收益读数（免费通道、0 元 API）。

用法（主环境）：
    python scripts/x1_event_calendar_run.py --codes 002304,600031 --years 2024,2025,2026 \
        --dataset <dataset_id> --out docs/evidence/y2_event_calendar_20261005.json

**类别码靠实测、不靠猜**：巨潮 `category=` 是枚举，写错会**静默空**（HTTP 200 / total=0）。
所以本脚本对每个候选类别码都记录 `total` 与实取行数，取回 0 行的类别**如实显示为 0**
（不因为"我以为是这个码"就当成"这家公司没有这类公告"）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters import cninfo                                    # noqa: E402
from adapters import market_history as mh                      # noqa: E402
from quant_research import event_calendar as ec                # noqa: E402
from quant_research import event_returns as er                 # noqa: E402

# 候选类别码：**待实测**（命中即验证；0 行不等于"没有这类公告"，只等于"这个码没拿到行"）
CATEGORY_CANDIDATES = {
    "年度报告": cninfo.CATEGORY_ANNUAL,
    "半年度报告": "category_bndbg_szsh",
    "一季度报告": "category_yjdbg_szsh",
    "三季度报告": "category_sjdbg_szsh",
    "业绩预告": "category_yjygjxz_szsh",
    "利润分配/权益分派": "category_qyfpxzcs_szsh",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="002304,600031")
    ap.add_argument("--years", default="2024,2025,2026")
    ap.add_argument("--dataset", default="")
    ap.add_argument("--benchmark", default="000300")
    ap.add_argument("--out", default="docs/evidence/y2_event_calendar_20261005.json")
    ap.add_argument("--materials-dir", default="",
                    help="只从**本地已准入材料**建日历（离线、可重复；不走网络）")
    args = ap.parse_args()

    codes = [c.strip() for c in str(args.codes).split(",") if c.strip()]
    years = tuple(y.strip() for y in str(args.years).split(",") if y.strip())

    probe: dict = {"categories": {}, "errors": []}
    records: list[dict] = []
    if args.materials_dir:
        # 离线路径：事件日只来自材料自己记录的披露日（含精度与依据），不手打、不联网。
        records = ec.load_materials(args.materials_dir)
        probe["mode"] = "local_materials"
        probe["materials_dir"] = str(args.materials_dir)
        probe["records"] = len(records)
    else:
        probe["mode"] = "cninfo_announcement_query"
        for label, cat in CATEGORY_CANDIDATES.items():
            got = 0
            for code in codes:
                try:
                    res = cninfo.query_announcements(code, years=years, category=cat)
                except Exception as exc:                        # noqa: BLE001
                    probe["errors"].append(
                        f"{code}/{label}: {type(exc).__name__}: {str(exc)[:90]}")
                    continue
                rows = res.get("announcements") or []
                got += len(rows)
                records += [{**r, "company_code": code,
                             "date_basis": "source_field:notice_date",
                             "date_precision": "day"} for r in rows]
                if not rows:
                    probe["categories"].setdefault(
                        label, {"total": res.get("total"), "rows": 0,
                                "note": "该类别码未取到行（不等于没有此类公告）"})
            if got:
                probe["categories"][label] = {"rows": got, "verified": True}
    probe["verified_categories"] = sorted(k for k, v in probe["categories"].items()
                                          if v.get("verified"))

    cal = ec.from_records(records, codes=codes)
    cal_out = {"schema": cal["schema"], "operator": cal["operator"], "stats": cal["stats"],
               "events": cal["events"], "excluded_sample": cal["excluded"][:20],
               "excluded_total": len(cal["excluded"]),
               "source_probe": probe}

    payload = mh.load(args.dataset)
    have = {str(c) for c in (payload.get("instruments") or [])}
    events = [e for e in ec.to_event_list(cal) if e["code"] in have]
    reading = (er.compute(payload, events, benchmark=args.benchmark, offsets=(0, 1, 5, 20))
               if events else {"status": "unavailable",
                               "reason": "日历里没有与行情数据集重合的标的"})

    doc = {"payload": {"dataset_id": payload.get("dataset_id"), "source": payload.get("source"),
                       "license": payload.get("license"), "adj_basis": payload.get("adj_basis"),
                       "rows": payload.get("rows"), "instruments": payload.get("instruments")},
           "calendar": cal_out, "reading": reading}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    print(json.dumps({"calendar_stats": cal["stats"], "category_probe": probe,
                      "reading_status": reading.get("status"),
                      "reading_hash": reading.get("reading_hash"),
                      "overlap": (reading.get("overlap") or {}).get("aggregate_dropped"),
                      "out": args.out}, ensure_ascii=False, indent=1)[:4000])
    return 0 if reading.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
