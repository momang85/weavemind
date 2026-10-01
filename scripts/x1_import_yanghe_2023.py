# -*- coding: utf-8 -*-
"""X1 有限导入：**洋河 2023 年年度报告**（官方原件，走产品自身的材料入口）。

为什么需要：X1 首例做洋河 2022–2024 的研究续页。缓存里只有 **2024 年年报**（本期 2024／
上期 2023），2022/2023 的经营与现金明细主要在 **2023 年年报**（本期 2023／上期 2022）里。
阶段X §4 允许"按正常资料入口有限取得/导入一份官方 2023 年年报"。

通道（不另开散口）：
    adapters.cninfo 公告查询（契约 `cninfo-hisannouncement-v1`，先取候选 URL）
      → material_intake.store(web_link) → material_intake.admit
      → net_policy.fetch_document（出域校验 + 已验 IP 直连）
      → 按**同一处缓存布局**落盘 `<slug>/project/materials/<mid>/{doc.json,meta.json,raw.*}`

只取件与准入，**不调模型**（注册模型都是确定性算子）。未准入时不写缓存（宁可缺，不留假正文）。
用法：`python scripts/x1_import_yanghe_2023.py [--url <直接给定的官方 URL>]`
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

SLUG = "002304-2023"
TASK_ID = "x1-002304-2023"
COMPANY, COMPANY_CODE = "洋河股份", "002304.SZ"
PERIODS = (2022, 2023)
CACHE = ROOT / "evals" / "a2_official_chain_20260929" / SLUG
OUT = ROOT / "docs" / "evidence" / "x1_yanghe_2023_import.json"
METRICS = ("revenue", "net_profit", "gross_profit", "operating_cashflow")


def _pick_url() -> dict:
    """公告查询 → 2023 年年度报告（正文，非摘要/非英文版）候选。"""
    from adapters import cninfo
    res = cninfo.query_announcements("002304", years=(2023,))
    items = list(res.get("items") or [])
    cand = [i for i in items
            if "2023年年度报告" in str(i.get("title") or "")
            and not i.get("is_summary") and "英文" not in str(i.get("title") or "")]
    chosen = cand[0] if cand else {}
    return {"contract": res.get("contract"), "total": res.get("total"),
            "candidates": [{"title": i.get("title"), "url": i.get("url"),
                            "disclosed_at": i.get("disclosed_at")} for i in items
                           if "2023年年度报告" in str(i.get("title") or "")],
            "chosen": {"title": chosen.get("title"), "url": chosen.get("url"),
                       "disclosed_at": chosen.get("disclosed_at")}}


def _publish_to_cache(meta: dict, doc: dict, raw: bytes) -> dict:
    """按既有缓存布局落盘（与 002304 / 600031 / 000711-corrected 同构，便于离线复验）。"""
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
    return {"dir": str(d.relative_to(ROOT)), "files": sorted(p.name for p in d.iterdir())}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="", help="跳过公告查询，直接给定官方 URL")
    args = ap.parse_args()

    import material_intake as mi
    import task_state
    import workspace as ws_mod

    rec: dict = {"slug": SLUG, "company": COMPANY, "code": COMPANY_CODE,
                 "periods": list(PERIODS), "online": True,
                 "purpose": "X1 研究续页：补 2022/2023 经营与现金明细（本期 2023／上期 2022）"}
    if args.url:
        rec["discovery"] = {"chosen": {"title": "（命令行给定）", "url": args.url}}
    else:
        try:
            rec["discovery"] = _pick_url()
        except Exception as exc:                   # noqa: BLE001
            rec["discovery"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    url = args.url or str(((rec.get("discovery") or {}).get("chosen") or {}).get("url") or "")
    rec["url"] = url
    if not url:
        rec["status"] = "no_candidate"
        OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(rec, ensure_ascii=False, indent=1))
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="x1_yanghe_2023_"))
    old_root, old_db = ws_mod.WORKSPACE_ROOT, task_state.DB_PATH
    ws_mod.configure_workspace_root(str(tmp))
    task_state.DB_PATH = str(tmp / "tasks.db")
    try:
        title = str(((rec.get("discovery") or {}).get("chosen") or {}).get("title")
                    or "洋河股份2023年年度报告")
        stored = mi.store(task_id=TASK_ID, channel=mi.CHANNEL_LINK, url=url,
                          title=title, period="2023", doc_type="年度报告",
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
        res = mi.admit(task_id=TASK_ID, mid=mid, company=COMPANY,
                       company_code=COMPANY_CODE, periods=PERIODS,
                       as_of="", metrics=METRICS, doc_type="年度报告",
                       goal=(f"研究{COMPANY}（{COMPANY_CODE}）2022/2023 年年度报告"
                             "（X1 研究续页的**较早一期**材料）"),
                       project="default")
        rec["admit"] = {k: res.get(k) for k in ("ok", "status", "reused", "detail",
                                                "evidence_count", "rules_version")}
        meta = mi.load(TASK_ID, mid, project="default") or {}
        doc = mi.load_doc(TASK_ID, mid, project="default") or {}
        rec["material"] = {k: meta.get(k) for k in
                           ("title", "url", "host", "bytes", "raw_sha256", "text_sha256",
                            "kind", "kind_label", "status", "reason", "period", "doc_type",
                            "disclosure_date", "date_precision", "date_basis",
                            "source_class", "provenance", "provenance_label",
                            "section_count", "evidence_count", "http_status", "egress",
                            "rules_version", "periods")}
        rec["parse"] = dict(meta.get("parse") or {})
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
            rec["note"] = "未准入：不写缓存（保持缺失比留下假正文更诚实）"
    except Exception as exc:                       # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        task_state.DB_PATH = old_db
        ws_mod.WORKSPACE_ROOT = old_root
        shutil.rmtree(tmp, ignore_errors=True)

    OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: rec.get(k) for k in
                      ("status", "url", "admit", "material", "cache", "error", "note")},
                     ensure_ascii=False, indent=1))
    print(f"证据：{OUT.relative_to(ROOT)}")
    return 0 if rec.get("status") == "admitted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
