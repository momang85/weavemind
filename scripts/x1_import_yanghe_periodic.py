# -*- coding: utf-8 -*-
"""X1 有限导入（同一处入口）：洋河**后续定期披露**——2025H1 半年报 / 2025 年报。

为什么需要：阶段X §7 的续页要用**一份后续公开披露**检验上一期写下的持续性假说。
上一期材料是 2024 年年度报告（本期 2024／上期 2023，缓存 slug `002304`）。

**期间口径（实测得出，写进代码）**：`financial_analysis` 的数据集是**年度模型**
（`dataset._is_annual`：只有 `年报` 且流量区间 ≥300 天才算年度期间），
所以能真正复用既有算子做"同口径两期"比较的后续材料是 **2025 年年度报告**；
半年报先取进来只用于检验**准入侧的期间识别**（旧实现只认年度报告标题，
半年报一律以"标题里没有报告期"被拒——这是实测踩到的盲区，已修）。

通道（与 `x1_import_yanghe_2023.py` **同一处**，不另开散口）：
    adapters.cninfo 公告查询（契约 `cninfo-hisannouncement-v1`，先取候选 URL）
      → material_intake.store(web_link) → material_intake.admit
      → net_policy.fetch_document（出域校验 + 已验 IP 直连）
      → 按**同一处缓存布局**落盘 `<slug>/project/materials/<mid>/{doc.json,meta.json,raw.*}`

只取件与准入，**不调模型**。未准入时不写缓存（宁可缺，不留假正文）。
用法：`python scripts/x1_import_yanghe_periodic.py --case 2025 [--url <官方 URL>]`
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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                  # noqa: BLE001
    pass

CACHE = ROOT / "evals" / "a2_official_chain_20260929"
COMPANY, COMPANY_CODE = "洋河股份", "002304.SZ"
METRICS = ("revenue", "net_profit", "gross_profit", "operating_cashflow")

# 每个 case：缓存 slug、公告分类、标题匹配、准入期间关键字、期间标签、文种与用途
CASES: dict[str, dict] = {
    "2025": {
        "slug": "002304-2025", "task_id": "x1-002304-2025",
        "category": "category_ndbg_szsh", "title_match": "2025年年度报告",
        "admit_periods": ("2025",), "period_label": "2025",
        "doc_type": "年度报告",
        "periods": (2024, 2025),
        "purpose": ("X1 续页：用 2025 年年度报告（本期 2025／上期 2024）检验上一期"
                    "（2024 年年报）写下的持续性假说——**年度对年度**同口径"),
    },
    "2025h1": {
        "slug": "002304-2025h1", "task_id": "x1-002304-2025h1",
        "category": "category_bndbg_szsh", "title_match": "2025年半年度报告",
        "admit_periods": ("2025H1",), "period_label": "2025H1",
        "doc_type": "半年度报告",
        "periods": (2024, 2025),
        "purpose": ("X1：中期披露的**期间识别**检验（半年报不是年度材料：期间关键字必须"
                    "是 `2025H1`，不能与 `2025` 混同）"),
    },
}


def _pick_url(case: dict) -> dict:
    """公告查询 → 指定文种（正文，非摘要/非英文版）候选。"""
    from adapters import cninfo
    years = tuple(int(y) for y in (str(case["period_label"])[:4],))
    res = cninfo.query_announcements("002304", years=years,
                                     category=str(case["category"]))
    items = list(res.get("items") or [])
    want = str(case["title_match"])
    cand = [i for i in items
            if want in str(i.get("title") or "")
            and not i.get("is_summary") and "英文" not in str(i.get("title") or "")]
    chosen = cand[0] if cand else {}
    return {"contract": res.get("contract"), "total": res.get("total"),
            "params": res.get("params"),
            "candidates": [{"title": i.get("title"), "url": i.get("url"),
                            "disclosed_at": i.get("disclosed_at")} for i in items
                           if want in str(i.get("title") or "")],
            "chosen": {"title": chosen.get("title"), "url": chosen.get("url"),
                       "disclosed_at": chosen.get("disclosed_at")}}


def _publish_to_cache(case: dict, meta: dict, doc: dict, raw: bytes) -> dict:
    """按既有缓存布局落盘（与 002304 / 002304-2023 / 600031 同构，便于离线复验）。"""
    import material_intake as mi
    mid = str(meta.get("material_id"))
    d = CACHE / str(case["slug"]) / "project" / "materials" / mid
    d.mkdir(parents=True, exist_ok=True)
    (d / "doc.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if raw:
        (d / f"raw.{str(meta.get('raw_ext') or 'bin')}").write_bytes(raw)
    (CACHE / str(case["slug"]) / "project" / "materials" / "index.json").write_text(
        json.dumps([mi._entry_of(meta)], ensure_ascii=False, indent=1), encoding="utf-8")
    return {"dir": str(d.relative_to(ROOT)), "files": sorted(p.name for p in d.iterdir())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="2025", choices=sorted(CASES))
    ap.add_argument("--url", default="", help="跳过公告查询，直接给定官方 URL")
    args = ap.parse_args()
    case = CASES[str(args.case)]
    out_path = ROOT / "docs" / "evidence" / f"x1_yanghe_{args.case}_import.json"

    import material_intake as mi
    import task_state
    import workspace as ws_mod

    rec: dict = {"case": str(args.case), "slug": case["slug"], "company": COMPANY,
                 "code": COMPANY_CODE, "periods": list(case["periods"]),
                 "period_label": case["period_label"], "doc_type": case["doc_type"],
                 "online": True, "purpose": case["purpose"]}
    if args.url:
        rec["discovery"] = {"chosen": {"title": "（命令行给定）", "url": args.url}}
    else:
        try:
            rec["discovery"] = _pick_url(case)
        except Exception as exc:                   # noqa: BLE001
            rec["discovery"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    url = args.url or str(((rec.get("discovery") or {}).get("chosen") or {}).get("url") or "")
    rec["url"] = url
    if not url:
        rec["status"] = "no_candidate"
        out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        return 1

    tmp = Path(tempfile.mkdtemp(prefix=f"x1_yanghe_{args.case}_"))
    old_root, old_db = ws_mod.WORKSPACE_ROOT, task_state.DB_PATH
    ws_mod.configure_workspace_root(str(tmp))
    task_state.DB_PATH = str(tmp / "tasks.db")
    try:
        title = str(((rec.get("discovery") or {}).get("chosen") or {}).get("title")
                    or f"洋河股份{case['title_match']}")
        stored = mi.store(task_id=str(case["task_id"]), channel=mi.CHANNEL_LINK, url=url,
                          title=title, period=str(case["period_label"]),
                          doc_type=str(case["doc_type"]),
                          declared_disclosed_at=str(
                              ((rec.get("discovery") or {}).get("chosen") or {})
                              .get("disclosed_at") or ""),
                          provenance="official_discovery",
                          note="X1 官方发现（契约 cninfo-hisannouncement-v1）",
                          project="default")
        rec["store"] = {k: stored.get(k) for k in ("ok", "duplicate", "material_id",
                                                   "error")}
        if not stored.get("ok"):
            rec["status"] = "store_failed"
            return 1
        mid = str(stored["material_id"])
        res = mi.admit(task_id=str(case["task_id"]), mid=mid, company=COMPANY,
                       company_code=COMPANY_CODE, periods=tuple(case["admit_periods"]),
                       as_of="", metrics=METRICS, doc_type=str(case["doc_type"]),
                       goal=(f"研究{COMPANY}（{COMPANY_CODE}）{case['title_match']}"
                             "（X1 续页的**较新一期**材料）"),
                       project="default")
        rec["admit"] = {k: res.get(k) for k in ("ok", "status", "reused", "detail",
                                                "evidence_count", "rules_version")}
        meta = mi.load(str(case["task_id"]), mid, project="default") or {}
        doc = mi.load_doc(str(case["task_id"]), mid, project="default") or {}
        rec["material"] = {k: meta.get(k) for k in
                           ("title", "url", "host", "bytes", "raw_sha256", "text_sha256",
                            "kind", "kind_label", "status", "reason", "period", "doc_type",
                            "disclosure_date", "date_precision", "date_basis",
                            "source_class", "provenance", "provenance_label",
                            "section_count", "evidence_count", "http_status", "egress",
                            "rules_version", "periods")}
        rec["parse"] = dict(meta.get("parse") or {})
        raw = b""
        rp = mi.raw_path(str(case["task_id"]), meta, project="default")
        if rp.is_file():
            raw = rp.read_bytes()
        if str(meta.get("status")) == mi.STATE_ADMITTED and doc:
            rec["cache"] = _publish_to_cache(case, meta, doc, raw)
            rec["status"] = "admitted"
        else:
            rec["status"] = "not_admitted"
            rec["cache"] = None
            rec["note"] = "未准入：不写缓存（保持缺失比留下假正文更诚实）"
    except Exception as exc:                       # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        task_state.DB_PATH = old_db
        ws_mod.WORKSPACE_ROOT = old_root
        shutil.rmtree(tmp, ignore_errors=True)

    out_path.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: rec.get(k) for k in
                      ("status", "url", "admit", "cache", "error", "note")},
                     ensure_ascii=False, indent=1))
    print(f"证据：{out_path.relative_to(ROOT)}")
    return 0 if rec.get("status") == "admitted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
