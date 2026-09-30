#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""官方入口**公网在线**实测：巨潮公告查询端点（无模型、无付费 API）。

为什么单独一条：此前"官方入口可达"只有离线替身读数（`test_cninfo_discovery.py` 等），
真实公网是否可达、参数契约是否仍然有效，一直没有实机证据。本脚本直接问官方端点：

    disclosure_ingest.discover → adapters.cninfo.discover_annual_reports
      → net_policy.fetch_document（POST 表单查询，出域校验 + 已验 IP 直连）

含**反例**：不存在的公司代码必须如实返回"查得到端点但没有对应公告"，不得编造候选。
结果只读，不落任何缓存、不调模型。

用法：`python scripts/a2_official_entry_online.py [--out docs/evidence]`
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUERIES = (
    # (公司, 代码, 期间) —— 000711 的更正稿是 2025-09-05 披露的，不带 until 时应出现在候选里
    ("京蓝科技", "000711", (2019, 2020)),
    ("三一重工", "600031", (2023, 2024)),
)
NEGATIVE = ("不存在的公司", "999999", (2023,))


def _one(company: str, code: str, periods) -> dict:
    from adapters import disclosure_ingest as di
    got = di.discover(company, code, periods=periods, doc_type="年度报告")
    cands = []
    for c in list(got.get("candidates") or [])[:10]:
        cands.append({"title": c.get("title"), "url": c.get("url"),
                      "disclosed_at": c.get("disclosed_at") or c.get("disclosure_date"),
                      "doc_type": c.get("doc_type"), "version": c.get("version"),
                      "why": str(c.get("why") or "")[:80]})
    return {"company": company, "code": code, "periods": list(periods),
            "status": got.get("status"), "reason_code": got.get("reason_code"),
            "reason": str(got.get("reason") or "")[:200],
            "total": got.get("total"), "contract": got.get("contract"),
            "params": dict(got.get("params") or {}),
            "probe": {k: v for k, v in (got.get("probe") or {}).items()
                      if k in ("url", "method", "status", "egress", "records", "elapsed_ms")},
            "candidate_count": len(got.get("candidates") or []),
            "candidates": cands,
            "next_steps": list(got.get("next_steps") or [])[:3]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="把读数写到该目录（JSON）")
    args = ap.parse_args()

    out: dict = {"entry": "cninfo 公告查询（官方披露发现）", "model_calls": 0, "paid": False,
                 "queries": [], "negative_control": None}
    for company, code, periods in QUERIES:
        rec = _one(company, code, periods)
        print(f"[{rec['status']}] {company} {code} 候选 {rec['candidate_count']} 条"
              f"（reason_code={rec['reason_code']}）")
        out["queries"].append(rec)
    nc = _one(*NEGATIVE)
    print(f"[反例 {nc['status']}] {NEGATIVE[0]} {NEGATIVE[1]} 候选 {nc['candidate_count']} 条"
          f"（reason_code={nc['reason_code']}）")
    out["negative_control"] = nc
    out["reachable"] = all(q["status"] != "unavailable" for q in out["queries"])
    out["corrected_visible"] = any(
        "更正" in str(c.get("title") or "")
        for q in out["queries"] for c in q["candidates"])

    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.out:
        p = Path(args.out) / "a2_official_entry_online.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"读数已写入 {p}")
    return 0 if out["reachable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
