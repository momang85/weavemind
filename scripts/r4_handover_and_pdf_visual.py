#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""R4：**同包研究交接** + **PDF 全页视觉**（离线、无 LLM、无付费）。

为什么单独一条（09-30 下午复核 §6）：前四批修的是"能不能算对"，这一批要回答"同材料下
新报告**到底多回答了哪些问题、还剩哪些残差**"，并且把利润—现金的**总额观察**与真正的
"现金流调节明细"分清楚。做法全走**正常入口**：

    已准入官方原文 → `working_paper_export.write_working_paper`（现役抽取器）
      → `DataAnalyzerWorker._freeze_dataset`（冻结数据集）
      → `financial_analysis.compile_plan`（问题驱动，含逐子句意图）
      → `financial_analysis.run`（注册算子 + 独立验证）
      → `report_brief.build_structure` / `render_brief_markdown`（同包正文）
      → `report_pdf.markdown_to_pdf` → **逐页栅格化视觉检查**（pymupdf）

两个经营特点不同的公司（白酒高毛利 / 装备制造）+ 一个亏损期公司作反例：
多答了什么、哪里没答（含原因）、哪些只是总额观察。

用法：`python scripts/r4_handover_and_pdf_visual.py [--out docs/evidence] [--pages 3]`
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
# 缓存目录 → (公司, 代码, 期间, 经营特点)
COMPANIES = (
    ("002304", "洋河股份", "002304.SZ", (2023, 2024), "白酒：高毛利、轻资产、渠道预收"),
    ("600031", "三一重工", "600031.SH", (2023, 2024), "装备制造：重资产、存货与应收占款重"),
    ("000711", "京蓝科技", "000711.SZ", (2019, 2020), "亏损期：净利为负、毛利率为负"),
)
QUESTION = ("{company} {p0}/{p1} 年经营分析：利润变化来自哪里、利润有没有转成现金、"
            "营运资金占用怎样、如果收入 +5% 毛利率 +1pp 会怎样")


def _admit(tid: str, slug: str) -> dict:
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
    meta.update({"material_id": mid, "status": mi.STATE_ADMITTED, "reason": "",
                 "kind": str(meta.get("kind") or "pdf"),
                 "title": str(meta.get("title") or f"{slug} 年年度报告"),
                 "url": str(meta.get("url") or doc.get("url") or ""),
                 "doc_type": "年度报告",
                 "disclosure_date": str(meta.get("disclosure_date") or "")})
    mi.save_meta(tid, meta, project="default")
    return meta


def _pdf_visual(pdf_bytes: bytes, *, pages: int, out_dir: Path, tag: str) -> dict:
    """逐页栅格化并给出**可核对的视觉读数**：页数/每页墨水占比/可提取字数/缺字方框数。"""
    import pymupdf
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    rec = {"pages": doc.page_count, "per_page": [], "images": []}
    try:
        for i in range(doc.page_count):
            page = doc.load_page(i)
            text = page.get_text("text") or ""
            pix = page.get_pixmap(dpi=110)
            png = out_dir / f"{tag}-p{i + 1}.png"
            pix.save(str(png))
            # 墨水占比：非白像素比例（整页空白/只有页眉都会被看出来）
            samples = pix.samples
            n = pix.width * pix.height
            nonwhite = 0
            step = max(1, n // 40000)
            for j in range(0, n, step):
                o = j * pix.n
                if samples[o] < 245 or samples[o + 1] < 245 or samples[o + 2] < 245:
                    nonwhite += 1
            rec["per_page"].append({
                "page": i + 1, "chars": len(text.strip()),
                "ink_ratio": round(nonwhite / max(1, (n // step)), 4),
                "tofu": text.count("\ufffd") + text.count("□"),
                "has_table_hint": any(k in text for k in ("指标", "上一期", "本期", "同比")),
            })
            if i < pages:
                rec["images"].append(str(png))
    finally:
        doc.close()
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="把读数写到该目录（JSON）")
    ap.add_argument("--pages", type=int, default=3, help="保留前 N 页 PNG 供人工查看")
    args = ap.parse_args()

    import financial_analysis as fa
    import task_state
    import workspace as ws_mod
    from workers.data_analyzer_worker import DataAnalyzerWorker
    from working_paper_export import write_working_paper

    tmp = Path(tempfile.mkdtemp(prefix="r4_handover_"))
    png_dir = tmp / "pages"
    # **真实任务工作区**：PDF 里的图表走相对路径（charts/chart_1.png），栅格化检查必须用
    # 真实工作区才能验"图是否真的嵌进去了"（用临时目录只会得到"文件不存在"，那是探针的错，
    # 不是产品的读数）。
    try:
        from workspace import task_workspace as _tws
        real_ws = Path(_tws("ui-603f626cbe"))
    except Exception:                                # noqa: BLE001
        real_ws = None
    old_root, old_db = ws_mod.WORKSPACE_ROOT, task_state.DB_PATH
    ws_mod.configure_workspace_root(str(tmp))
    task_state.DB_PATH = str(tmp / "tasks.db")

    out: dict = {"offline": True, "model_calls": 0, "paid": False, "companies": []}
    try:
        for slug, company, code, periods, trait in COMPANIES:
            tid = f"r4-{slug}"
            meta = _admit(tid, slug)
            if not meta:
                out["companies"].append({"slug": slug, "status": "no_cache"})
                continue
            as_of = str(meta.get("disclosure_date") or "")
            task_state.mark_queued(
                tid, goal=f"研究{company}（{code}）{periods[0]}/{periods[1]} 年年度报告研究",
                research_request={"company": company, "company_id": code, "market": "cn",
                                 "periods": list(periods), "caliber": "合并",
                                 "as_of": as_of, "identity_source": "form"})
            ws = Path(ws_mod.task_workspace(tid, "default"))
            (ws / "project").mkdir(parents=True, exist_ok=True)
            wp = write_working_paper(tid, f"研究{company} {periods[0]}/{periods[1]} 年经营情况",
                                     project="default")
            rec: dict = {"slug": slug, "company": company, "code": code, "trait": trait,
                         "as_of": as_of, "paper_ok": bool(wp.get("ok")),
                         "request_source": wp.get("request_source"),
                         "rows": wp.get("rows"), "derived": wp.get("derived")}
            if not wp.get("ok"):
                rec["status"] = "paper_skipped"
                rec["reason"] = str(wp.get("reason") or "")[:160]
                out["companies"].append(rec)
                continue
            ds, label = DataAnalyzerWorker._freeze_dataset(
                "working_paper", ws / "project" / "working_paper.json",
                task={"goal": f"研究{company}", "context": {"root_task_id": tid}},
                required_metrics=("revenue", "net_profit", "gross_profit",
                                  "operating_cashflow"),
                available_models=[m.model_id for m in fa.specs()])
            q = QUESTION.format(company=company, p0=periods[0], p1=periods[1])
            plan = fa.compile_plan(q, ds)
            rec["question"] = q
            rec["dataset"] = {"label": label, "periods": list(ds.manifest.periods),
                              "observations": ds.manifest.observations,
                              "usable": ds.manifest.usable,
                              "gaps": list(ds.manifest.gaps)[:6]}
            rec["subquestions"] = [{"clause": s["clause"], "kind": s["kind"],
                                    "answered": s["answered"], "models": s["models"],
                                    "note": s["note"][:120]} for s in plan.subquestions]
            rec["adopted"] = [a.model_id for a in plan.adopted]
            rec["exploratory"] = list(plan.exploratory)
            rec["notes"] = [n[:160] for n in plan.notes]
            runs, residuals = [], []
            for item in plan.adopted:
                run = fa.run(item.model_id, ds, params=item.params,
                             question=item.question)
                main = {o.metric: o.value for o in run.outputs[:2]}
                runs.append({"model_id": run.model_id, "status": run.status,
                             "reason": str(run.reason or "")[:120],
                             "failed": list((run.validation or {}).get("failed") or []),
                             "main": main})
                for o in run.outputs:
                    if getattr(o, "unexplained", None):
                        residuals.append({"output": o.metric,
                                          "value": o.unexplained.get("value"),
                                          "note": str(o.unexplained.get("note") or "")[:100]})
            rec["runs"] = runs
            rec["residuals"] = residuals[:4]
            rec["status"] = ("validated" if any(r["status"] == "validated" for r in runs)
                             else "no_run")
            # 同包正文：走 report_brief 的正常装配（模型撰写的「分析」节在离线检查里标注为缺失）
            try:
                import report_brief as rb
                body = "## 分析\n\n（离线同包交接检查：本节不含模型撰写的分析文字。）\n"
                structure = rb.build_structure(tid, f"研究{company}", body,
                                               project="default")
                if structure:
                    brief = rb.render_brief_markdown(structure, body, task_id=tid)
                    p = ws / "project" / "r4_brief.md"
                    p.write_text(brief, encoding="utf-8")
                    rec["brief"] = {"path": str(p), "chars": len(brief),
                                    "citations": len(structure.get("citations") or []),
                                    "sections": len(structure.get("sections") or [])}
                    if not out.get("pdf_visual"):
                        from report_pdf import markdown_to_pdf
                        pdf = markdown_to_pdf(brief, title=f"{company} 经营分析简报",
                                              workspace=ws)
                        rec["pdf_bytes"] = len(pdf)
                        out["pdf_visual"] = _pdf_visual(pdf, pages=args.pages,
                                                        out_dir=png_dir, tag=f"{slug}")
                        out["pdf_visual"]["source"] = f"{company} 同包简报（report_brief 装配）"
            except Exception as exc:                 # noqa: BLE001 - 正文装配失败不影响读数
                rec["brief_error"] = f"{type(exc).__name__}: {str(exc)[:140]}"
            out["companies"].append(rec)
    finally:
        task_state.DB_PATH = old_db
        ws_mod.WORKSPACE_ROOT = old_root

    done = [c for c in out["companies"] if c.get("status") == "validated"]
    # ── PDF 全页视觉：优先用**真实交付正文**（已发布任务的 report），否则用同包简报/读数合成 ──
    if not out.get("pdf_visual"):
        src_md, src_tag = "", "synthetic"
        try:
            import task_state as _ts
            row = _ts.read_task("ui-603f626cbe") or {}
            if str(row.get("report") or "").strip():
                src_md, src_tag = str(row["report"]), "ui-603f626cbe"
        except Exception:                            # noqa: BLE001
            pass
        if not src_md:
            for c in out["companies"]:
                p = str((c.get("brief") or {}).get("path") or "")
                if p and Path(p).is_file():
                    src_md, src_tag = Path(p).read_text(encoding="utf-8"), "brief"
                    break
        if not src_md:
            src_md = "# 同包读数（离线合成）\n\n| 公司 | 子问题已答 |\n|---|---|\n" + "\n".join(
                f"| {c['company']} | {sum(1 for s in c['subquestions'] if s['answered'])} |"
                for c in done)
            src_tag = "synthetic"
        try:
            from report_pdf import markdown_to_pdf
            _pdf_ws = real_ws if (src_tag == "ui-603f626cbe" and real_ws is not None) else tmp
            pdf = markdown_to_pdf(src_md, title=f"{src_tag} 交付正文", workspace=_pdf_ws)
            out["pdf_visual"] = _pdf_visual(pdf, pages=args.pages, out_dir=png_dir,
                                            tag="pdf")
            out["pdf_visual"].update({"source": src_tag, "markdown_chars": len(src_md),
                                      "workspace": str(_pdf_ws)})
        except Exception as exc:                     # noqa: BLE001
            out["pdf_visual"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}",
                                 "source": src_tag}
    out["summary"] = {
        "companies": len(out["companies"]),
        "validated": len(done),
        "answered_subquestions": sum(1 for c in done for s in c["subquestions"]
                                     if s["answered"]),
        "unanswered_subquestions": sum(1 for c in done for s in c["subquestions"]
                                       if not s["answered"]),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.out:
        p = Path(args.out) / "r4_handover_and_pdf_visual.json"
        p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"读数已写入 {p}")
        png_keep = Path(args.out) / "r4_pdf_pages"
        png_keep.mkdir(parents=True, exist_ok=True)
        for src in png_dir.glob("*.png"):
            shutil.copy2(src, png_keep / src.name)
        print(f"页面 PNG 已写入 {png_keep}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if len(done) >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
