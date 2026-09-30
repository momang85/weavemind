#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""A2/S2 补缺：**在线**取回 000711「更正后」年报正文，走产品自身的材料入口。

为什么单独一条：`evals/a2_official_chain_20260929/000711-corrected/` 里只有
`meta.json` / `index.json`，**没有 doc.json 与 raw.bin**——更正稿正文缓存缺失，
"更正版基线"因此无法离线复验。本脚本用产品自己的通道取件（不另开散口）：

    material_intake.store(web_link) → material_intake.admit
      → net_policy.fetch_document（严格出域校验 + 已验 IP 直连，不跟随重定向）

取回后按**同一处缓存布局**落盘（`<slug>/project/materials/<mid>/{doc.json,meta.json,raw.bin}`），
供 `scripts/l1_official_facts_chain.py` 离线复验"官方原文 → 财务事实 → 底稿 → 数据集 → 模型"。

只联网取件、**不调模型**（注册模型是确定性算子）；失败时如实记录且**不覆盖缓存**。

用法：`python scripts/a2_corrected_report_online.py [--out docs/evidence]`
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SLUG = "000711-corrected"
TASK_ID = "a2-000711-2020-corrected"
URL = "http://static.cninfo.com.cn/finalpage/2025-09-05/1224639904.PDF"
TITLE = "京蓝科技股份有限公司2020年年度报告（更正后）"
COMPANY = "京蓝科技"
COMPANY_CODE = "000711.SZ"
PERIODS = (2019, 2020)
CACHE = ROOT / "evals" / "a2_official_chain_20260929" / SLUG
METRICS = ("revenue", "net_profit", "gross_profit", "operating_cashflow")


def _publish_to_cache(meta: dict, doc: dict, raw: bytes) -> dict:
    """把取回的材料按缓存布局落盘（与既有三个 slug 同构，便于离线复验）。"""
    import material_intake as mi
    mid = str(meta.get("material_id"))
    d = CACHE / "project" / "materials" / mid
    d.mkdir(parents=True, exist_ok=True)
    (d / "doc.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if raw:
        (d / f"raw.{str(meta.get('raw_ext') or 'bin')}").write_bytes(raw)
    (CACHE / "project" / "materials" / "index.json").write_text(
        json.dumps([mi._entry_of(meta)], ensure_ascii=False, indent=1), encoding="utf-8")
    return {"dir": str(d), "files": sorted(p.name for p in d.iterdir())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="把读数写到该目录（JSON）")
    args = ap.parse_args()

    import material_intake as mi
    import task_state
    import workspace as ws_mod

    tmp = Path(tempfile.mkdtemp(prefix="a2_corrected_"))
    old_root = ws_mod.WORKSPACE_ROOT
    old_db = task_state.DB_PATH
    ws_mod.configure_workspace_root(str(tmp))
    task_state.DB_PATH = str(tmp / "tasks.db")

    rec: dict = {"slug": SLUG, "company": COMPANY, "code": COMPANY_CODE,
                 "url": URL, "periods": list(PERIODS), "online": True}
    try:
        stored = mi.store(task_id=TASK_ID, channel=mi.CHANNEL_LINK, url=URL,
                          title=TITLE, period="2020", doc_type="年度报告",
                          declared_disclosed_at="2025-09-05",
                          provenance="official_discovery",
                          note="A2 更正版演示（2025-09-05）",
                          project="default")
        rec["store"] = {k: stored.get(k) for k in ("ok", "duplicate", "material_id",
                                                   "error")}
        if not stored.get("ok"):
            rec["status"] = "store_failed"
            print(json.dumps(rec, ensure_ascii=False, indent=1))
            return 1
        mid = str(stored["material_id"])
        res = mi.admit(task_id=TASK_ID, mid=mid, company=COMPANY,
                       company_code=COMPANY_CODE, periods=PERIODS,
                       as_of="", metrics=METRICS, doc_type="年度报告",
                       goal=f"研究{COMPANY}（{COMPANY_CODE}）2019/2020 年年度报告研究",
                       project="default")
        meta = mi.load(TASK_ID, mid, project="default") or {}
        doc = mi.load_doc(TASK_ID, mid, project="default") or {}
        rec["admit"] = {k: res.get(k) for k in ("ok", "status", "reused", "detail",
                                                "evidence_count", "rules_version")}
        rec["material"] = {k: meta.get(k) for k in
                           ("title", "url", "host", "bytes", "raw_sha256", "text_sha256",
                            "kind", "kind_label", "status", "reason", "period", "doc_type",
                            "disclosure_date", "date_precision", "date_basis",
                            "source_class", "provenance", "provenance_label",
                            "section_count", "evidence_count", "http_status", "egress",
                            "rules_version")}
        rec["parse"] = dict(meta.get("parse") or {})
        rec["metric_states"] = dict(meta.get("metric_states") or {})
        rec["evidence_sample"] = [
            {k: e.get(k) for k in ("metric", "page", "quote", "label", "value")}
            for e in list(meta.get("evidence") or [])[:3]]
        if str(meta.get("status")) != mi.STATE_ADMITTED or doc.get("kind") not in ("", None):
            pass
        raw = b""
        rp = mi.raw_path(TASK_ID, meta, project="default")
        if rp.is_file():
            raw = rp.read_bytes()
        if str(meta.get("status")) == mi.STATE_ADMITTED and doc:
            rec["cache"] = _publish_to_cache(meta, doc, raw)
            rec["status"] = "admitted"
        else:
            rec["status"] = "not_admitted"
            rec["cache"] = None
            rec["note"] = "未准入：不覆盖缓存（保持缺失比留下假正文更诚实）"
    except Exception as exc:                                   # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        task_state.DB_PATH = old_db
        ws_mod.WORKSPACE_ROOT = old_root
        shutil.rmtree(tmp, ignore_errors=True)

    print(json.dumps(rec, ensure_ascii=False, indent=1))
    if args.out:
        p = Path(args.out) / "a2_corrected_report_online.json"
        p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"读数已写入 {p}")
    return 0 if rec.get("status") == "admitted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
