#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""L1 证据：**缓存真年报正文 → 正常资料入口 → 财务事实 → 底稿 → 冻结数据集 → 模型**。

为什么单独一条（架构复核 S2）：官方 PDF 此前只进叙事证据，`working_paper_export` 没有
`financials.json` 就直接跳过——"官方原文取得"不等于"模型拿到数"。本脚本走**正常入口**：
把缓存正文按 `material_intake` 的材料布局登记为**已准入**材料 → 用
`working_paper_export.write_working_paper` 产出底稿（内部调
`facts.facts_from_annual_tables`，即现役 `adapters.annual_financial_tables` 抽取器）
→ `DataAnalyzerWorker._freeze_dataset` 冻结数据集 → 按研究问题编译计划并跑注册模型。

跨公司：缓存里三家非金融公司（京蓝 000711 / 洋河 002304 / 三一 600031），
**不是同一家公司的两个任务**。结构化 API 在脚本里**不存在**（不放 `financials.json`），
这正是"API 不可用但官方资料入口成功"的边界。

用法：`python scripts/l1_official_facts_chain.py [--out docs/evidence]`
只读缓存、不联网、不调模型（注册模型是确定性算子）。
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

CACHE = ROOT / "evals" / "a2_official_chain_20260929"
# 缓存目录 → (公司, 代码, 期间, 口径)；期间取该份年报覆盖的两个年度
COMPANIES = (
    ("002304", "洋河股份", "002304.SZ", (2023, 2024)),
    ("600031", "三一重工", "600031.SH", (2023, 2024)),
    ("000711", "京蓝科技", "000711.SZ", (2019, 2020)),
)


def _prepare_task(tid: str, company: str, code: str, periods, as_of: str) -> None:
    import task_state
    task_state.mark_queued(
        tid, goal=f"研究{company}（{code}）{'/'.join(str(p) for p in periods)} 年年度报告研究",
        research_request={"company": company, "company_id": code, "market": "cn",
                          "periods": list(periods), "caliber": "合并", "as_of": as_of,
                          "identity_source": "form"})


def _admit_cached_doc(tid: str, slug: str) -> dict:
    """把缓存里的真年报正文按材料布局登记为**已准入**材料（不联网、不重新解析 PDF）。"""
    import material_intake as mi
    docs = sorted((CACHE / slug / "project" / "materials").glob("*/doc.json"))
    if not docs:
        return {}
    doc = json.loads(docs[0].read_text(encoding="utf-8"))
    meta_in = docs[0].with_name("meta.json")
    meta = json.loads(meta_in.read_text(encoding="utf-8")) if meta_in.exists() else {}
    mid = str(meta.get("material_id") or docs[0].parent.name)
    d = mi.materials_dir(tid, project="default") / mid
    d.mkdir(parents=True, exist_ok=True)
    (d / "doc.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    meta_out = dict(meta)
    meta_out.update({"material_id": mid, "status": mi.STATE_ADMITTED, "reason": "",
                     "kind": str(meta.get("kind") or "pdf"),
                     "title": str(meta.get("title") or f"{slug} 年年度报告"),
                     "url": str(meta.get("url") or doc.get("url") or ""),
                     "doc_type": "年度报告",
                     "disclosure_date": str(meta.get("disclosure_date") or ""),
                     "created_at": str(meta.get("created_at") or "2026-09-30T00:00:00")})
    mi.save_meta(tid, meta_out, project="default")
    return meta_out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="把读数写到该目录（JSON）")
    args = ap.parse_args()

    import financial_analysis as fa
    import workspace as ws_mod
    from workers.data_analyzer_worker import DataAnalyzerWorker
    from working_paper_export import write_working_paper

    tmp = Path(tempfile.mkdtemp(prefix="l1_chain_"))
    old_root = ws_mod.WORKSPACE_ROOT
    ws_mod.configure_workspace_root(str(tmp))
    import task_state
    old_db = task_state.DB_PATH
    task_state.DB_PATH = str(tmp / "tasks.db")

    out: list[dict] = []
    try:
        for slug, company, code, periods in COMPANIES:
            meta = _admit_cached_doc(f"l1-{slug}", slug)
            if not meta:
                out.append({"slug": slug, "company": company, "status": "no_cache"})
                continue
            as_of = str(meta.get("disclosure_date") or "")
            tid = f"l1-{slug}"
            _prepare_task(tid, company, code, periods, as_of)
            ws = Path(ws_mod.task_workspace(tid, "default"))
            (ws / "project").mkdir(parents=True, exist_ok=True)
            wp = write_working_paper(tid, f"研究{company} {periods} 年经营情况",
                                     project="default")
            rec: dict = {"slug": slug, "company": company, "code": code,
                         "as_of": as_of, "paper_ok": bool(wp.get("ok")),
                         "request_source": wp.get("request_source"),
                         "official_notes": wp.get("official_notes") or [],
                         "rows": wp.get("rows"), "derived": wp.get("derived"),
                         "gaps": list(wp.get("gaps") or [])[:6]}
            if not wp.get("ok"):
                rec["status"] = "paper_skipped"
                rec["reason"] = str(wp.get("reason") or "")[:160]
                out.append(rec)
                continue
            facts = wp.get("facts") or []
            rec["fact_metrics"] = sorted({str(f.get("metric")) for f in facts})
            rec["fact_periods"] = sorted({str(f.get("period")) for f in facts})
            ds, label = DataAnalyzerWorker._freeze_dataset(
                "working_paper", ws / "project" / "working_paper.json",
                task={"goal": f"研究{company}", "context": {"root_task_id": tid}},
                required_metrics=("revenue", "net_profit", "gross_profit",
                                  "operating_cashflow"),
                available_models=[m.model_id for m in fa.specs()])
            rec["dataset"] = {"label": label, "periods": list(ds.manifest.periods),
                              "observations": ds.manifest.observations,
                              "usable": ds.manifest.usable,
                              "gaps": list(ds.manifest.gaps)[:6]}
            question = (f"{company} {periods} 年经营分析：利润变化来自哪里、"
                        "利润有没有转成现金、营运资金占用怎样")
            plan = fa.compile_plan(question, ds)
            rec["question_types"] = list(plan.question_types)
            rec["adopted"] = [a.model_id for a in plan.adopted]
            runs = []
            for item in plan.adopted:
                run = fa.run(item.model_id, ds, params=item.params,
                             question=item.question)
                main = {o.metric: o.value for o in run.outputs[:2]}
                runs.append({"model_id": run.model_id, "status": run.status,
                             "reason": run.reason[:120],
                             "validation_failed": list(
                                 (run.validation or {}).get("failed") or []),
                             "main": main})
            rec["runs"] = runs
            rec["status"] = ("validated" if any(r["status"] == "validated"
                                                for r in runs) else "no_run")
            out.append(rec)
    finally:
        task_state.DB_PATH = old_db
        ws_mod.WORKSPACE_ROOT = old_root
        shutil.rmtree(tmp, ignore_errors=True)

    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.out:
        p = Path(args.out) / "l1_official_facts_chain.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"读数已写入 {p}")
    ok = [r for r in out if r.get("status") == "validated"]
    print(f"\n跨公司：{len(out)} 家，跑出已验证运行 {len(ok)} 家")
    return 0 if len(ok) >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
