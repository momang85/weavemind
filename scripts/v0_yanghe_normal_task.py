# -*- coding: utf-8 -*-
"""V0 成果：洋河一份**正常任务**的经营研究报告（隔离跑通，产出正文/三图/PDF/包）。

为什么这样跑（如实说明边界）：HTTP 表单入口需要登录会话，运行侧没有可用凭据，也不应复制
会话 token。本脚本调用的是同一条**生产函数链**：

    事实抽取（adapters）→ 研究请求契约（facts.parse_research_request）
    → 底稿（working_paper.build_working_paper，落 `project/working_paper.json`）
    → **data_analyzer 的金融路径**（`DataAnalyzerWorker._run_financial`：冻结数据集→计划→
      注册模型→落盘运行与可复算输入）
    → 三图（`OrchestratorV2._analysis_chart_specs` + 生产渲染脚本）
    → 简报装配/验收/版本（`delivery_pipeline.assemble_and_verify`）
    → 导出（`web_ui._task_pdf_bytes` + 导出清单）

未覆盖的只有「登录会话 → POST /api/tasks → 步骤派发/worker 回包」这一层（由现有离线用例
与实机门禁覆盖）。本脚本**不读密钥、不伪造认证**，跑完在证据里标「实机登录待验」。

产出：
- `docs/evidence/v0_yanghe_normal_task.json`：读数（三段分析、三图、PDF 页数/内嵌图、
  交付状态与包内清单、缺口）；
- `docs/evidence/v0_yanghe_normal_task/`：`report.md`、`report.pdf`、`charts/`、`package/`。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                                  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOC = (ROOT / "evals" / "a2_official_chain_20260929" / "002304" / "project"
       / "materials" / "f42e747c73850b59" / "doc.json")
OUT_DIR = ROOT / "docs" / "evidence" / "v0_yanghe_normal_task"
REPORT = ROOT / "docs" / "evidence" / "v0_yanghe_normal_task.json"

COMPANY, COMPANY_ID = "洋河股份", "002304.SZ"
PERIODS = (2023, 2024)
AS_OF, CALIBER = "2025-04-30", "合并"
GOAL = (f"洋河股份 {PERIODS[0]}/{PERIODS[1]} 经营研究：利润由何而来、现金为何变化、"
        f"什么条件会改变判断；{CALIBER}口径，数据截至 {AS_OF}。"
        "每个数字须能回溯到来源位置并可重算；缺证据的如实标缺口。")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def _pdf_verdict(pdf: Path, *, expected_images: int) -> dict:
    """PDF 里到底有没有图（沿用验收脚本的判据，不靠"文件非空"）。"""
    try:
        import report_pdf
        raw = pdf.read_bytes()
        counts = report_pdf.pdf_page_image_counts(raw)
        return {"pages_with_images": sum(1 for n in counts if n),
                "total_images": sum(counts),
                "expected_images": expected_images,
                "images_ok": sum(counts) >= expected_images > 0}
    except Exception as exc:                       # noqa: BLE001
        return {"error": str(exc)[:160]}


def main() -> int:
    import delivery_pipeline as dp
    import facts as F
    import report_brief
    import task_state
    import workspace as ws_mod
    import working_paper as WP
    from orchestrator_v2 import OrchestratorV2
    from workers.data_analyzer_worker import DataAnalyzerWorker

    tmp = Path(tempfile.mkdtemp(prefix="v0_yanghe_"))
    old_root, old_db = ws_mod.WORKSPACE_ROOT, task_state.DB_PATH
    ws_mod.configure_workspace_root(str(tmp))
    task_state.DB_PATH = str(tmp / "v0.db")
    tid = "v0-yanghe"
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {"case": "V0 洋河正常任务（经营研究：利润／现金／情景）",
                    "goal": GOAL, "task_id": tid}
    try:
        # ① 研究请求 + 任务登记（与场景/实机同一条入库路径）
        doc = json.loads(DOC.read_text(encoding="utf-8"))
        req = F.parse_research_request(GOAL, company=COMPANY, company_id=COMPANY_ID,
                                       market="cn", periods=list(PERIODS),
                                       caliber=CALIBER, as_of=AS_OF,
                                       identity_source="v0-isolated")
        task_state.mark_queued(tid, goal=GOAL, research_request=req.to_payload(),
                               db_path=task_state.DB_PATH)
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        ws = ws_mod.task_workspace(tid)

        # ② 事实（生产适配器读缓存年报）→ 底稿（生产构造器）→ 落 project/working_paper.json
        facts = F.facts_from_annual_tables(doc, company=COMPANY,
                                           company_code="002304",
                                           periods=PERIODS, as_of=AS_OF,
                                           disclosed_at="2025-04-28")
        paper = WP.build_working_paper(facts, req)
        paper_path = proj / "working_paper.json"
        paper_path.write_text(json.dumps(paper.as_dict(), ensure_ascii=False, indent=1),
                              encoding="utf-8")
        report["facts"] = {"count": len(facts),
                           "with_locator": sum(1 for f in facts
                                               if getattr(f, "source_locator", None)),
                           "paper_rows": len(paper.rows), "paper_derived": len(paper.derived)}

        # ②′ 结构化财务载荷（交付装配的生产入口读它建底稿）：数值**全部来自上面抽取的事实**，
        #     不写死；单位与口径随 metadata 声明。
        want = ("revenue", "net_profit", "operating_cashflow", "total_assets",
                "total_liabilities")
        by_year: dict = {}
        for f in facts:
            metric = str(getattr(f, "metric", "") or "")
            period = str(getattr(f, "period", "") or "")
            # **只取合并口径**：同一指标在合并/母公司两套报表里都有（母公司经营现金流为负），
            # 不筛口径就会把母公司数当成公司数（这正是 K0-a 的 report_scope 纪律）。
            if str(getattr(f, "caliber", "") or "") != CALIBER:
                continue
            if metric not in want or not period.endswith("年"):
                continue
            try:
                year = int(period[:4])
            except ValueError:
                continue
            if year not in PERIODS:
                continue
            if getattr(f, "value", None) is None:
                continue
            by_year.setdefault(year, {})[metric] = float(f.value)
        if len(by_year) >= 2:
            payload = {
                "financials": [dict({"year": y, "report_type": "年报",
                                     "disclosure_date": "2025-04-28"}, **by_year[y])
                               for y in sorted(by_year)],
                "metadata": {"source": "cninfo_annual_report_tables", "company": COMPANY,
                             "company_id": COMPANY_ID, "currency": "CNY", "unit": "元",
                             "caliber": CALIBER,
                             "caliber_evidence": "合并利润表/合并现金流量表标题",
                             "as_of": AS_OF},
                "raw": {"url": str(doc.get("url") or "cached:002304"),
                        "text": "{}"},
            }
            (proj / "financials.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            report["structured_payload"] = {
                "years": sorted(by_year), "metrics": sorted(
                    {m for v in by_year.values() for m in v})}

        # ③ 金融分析步（**worker 的同一条路径**）：冻结→计划→注册模型→落运行与可复算输入
        out = DataAnalyzerWorker._run_financial(
            DataAnalyzerWorker, ws, GOAL, {"goal": GOAL, "task_id": tid,
                                           "context": {"root_task_id": tid}},
            ("working_paper", paper_path))
        report["analysis_step"] = {
            "status": out.get("status"), "plan": out.get("plan"),
            "dataset": out.get("dataset"),
            "runs": [{"model_id": r["model_id"], "status": r["status"],
                      "run_id": r["run_id"]} for r in (out.get("runs") or [])],
            "note": out.get("note"),
        }
        ctx = (ws / "analysis" / "context.json")
        loc = json.loads(ctx.read_text(encoding="utf-8")).get("fact_locators") if ctx.is_file() else {}
        report["fact_locators"] = {"count": len(loc or {}),
                                   "with_page": sum(1 for v in (loc or {}).values()
                                                    if "页" in str(v.get("locator") or ""))}

        # ④ 三图（生产规格 + 生产渲染器）：与正文共用同一组选定运行
        specs = OrchestratorV2._analysis_chart_specs(object(), tid)
        (proj / "chart_data.json").write_text(
            json.dumps({"charts": specs}, ensure_ascii=False, indent=1), encoding="utf-8")
        from charts_pipeline import ChartPipelineMixin
        ChartPipelineMixin._render_chart_data(OrchestratorV2, tid, GOAL)
        charts_dir = OUT_DIR / "charts"
        charts_dir.mkdir(parents=True, exist_ok=True)
        for png in sorted((ws / "charts").glob("*.png")):
            shutil.copy2(png, charts_dir / png.name)
        manifest = {}
        if (proj / "chart_manifest.json").is_file():
            manifest = json.loads((proj / "chart_manifest.json")
                                  .read_text(encoding="utf-8"))
        report["charts"] = [{"file": c.get("file"), "type": c.get("type"),
                             "grade": c.get("grade"),
                             "observation": (c.get("observation") or "")[:160]}
                            for c in (manifest.get("charts") or [])]

        # ⑤ 正文与摘要（报告链的同一批函数）
        note = report_brief._analysis_note_block(tid, ws_dir=ws)
        summary = report_brief._analysis_card_block(tid, ws_dir=ws)
        (OUT_DIR / "analysis_note.md").write_text(note, encoding="utf-8")
        report["text"] = {
            "note_chars": len(note), "note_lines": len(note.splitlines()),
            "summary_lines": [l for l in summary.splitlines() if l.startswith("- ")][:4],
            "has_three_sections": all(k in note for k in ("**利润**", "**现金**",
                                                          "**反向情景**")),
            "locators_in_note": sum(1 for l in note.splitlines()
                                    if l.startswith("| ") and "页" in l),
            "engineering_ids_in_body": sum(
                1 for l in note.split("### 附：底稿索引")[0].splitlines()
                if "output `" in l or "run `" in l),
        }
        body = ("本报告由确定性分析链装配：利润、现金与情景三段见『分析摘要』与"
                "『经营驱动分析正文』；底稿与来源随包提供。")
        res = dp.assemble_and_verify(tid, GOAL, body, project="default", ws_dir=str(ws))
        report["delivery"] = {"status": res.get("status"),
                              "reason": res.get("reason"),
                              "hard_fail": res.get("hard_fail"),
                              "review_valid": res.get("review_valid"),
                              "review_reason": res.get("review_reason"),
                              "acceptance_overall": res.get("acceptance_overall"),
                              "note": ("隔离验证不含 Critic 评审与检索来源两条轴："
                                       "交付正文与三图按生产函数产出，"
                                       "验收状态如实停在草稿（不伪造评审通过）")}

        # ⑥ 导出（真实 markdown/PDF/清单）
        import web_ui
        try:
            task_state.update_delivery_projection(
                tid, report=str(res.get("report") or ""),
                status="SUCCESS" if res.get("status") != "draft" else "SUCCESS_WITH_ISSUES",
                db_path=task_state.DB_PATH)
        except Exception as exc:                   # noqa: BLE001
            report["projection_warning"] = str(exc)[:160]
        (OUT_DIR / "report.md").write_text(str(res.get("report") or ""), encoding="utf-8")
        try:
            pdf = web_ui._task_pdf_bytes(tid)
            (OUT_DIR / "report.pdf").write_bytes(pdf)
            report["pdf"] = {"bytes": len(pdf), **_pdf_verdict(OUT_DIR / "report.pdf",
                                                               expected_images=len(
                                                                   report["charts"]))}
            exp = web_ui._read_export_manifest(tid) or {}
            (OUT_DIR / "export_manifest.json").write_text(
                json.dumps(exp, ensure_ascii=False, indent=1), encoding="utf-8")
            report["export"] = {"files": list((exp.get("files") or {}).keys())
                                if isinstance(exp.get("files"), dict)
                                else list(exp.get("files") or [])}
        except Exception as exc:                   # noqa: BLE001
            report["pdf"] = {"error": str(exc)[:200]}

        # ⑦ 包与最小截图（真实冻结包 + 页面级截图：图确实进了 PDF，不靠"文件非空"）
        arc = []
        adir = ws / "analysis"
        if adir.is_dir():
            arc = sorted(p.name for p in adir.iterdir() if p.is_file())
        report["package_analysis_files"] = arc
        try:
            pack = dp.repack_adopted(
                tid, md_bytes=(OUT_DIR / "report.md").read_bytes(),
                pdf_bytes=(OUT_DIR / "report.pdf").read_bytes(), ws_dir=str(ws))
            zp = None
            for key in ("zip", "zip_path", "path", "file"):
                if pack.get(key) and str(pack[key]).endswith(".zip"):
                    zp = Path(str(pack[key]))
                    break
            if zp is not None and zp.is_file():
                import zipfile
                with zipfile.ZipFile(zp) as zf:
                    names = zf.namelist()
                (OUT_DIR / "package").mkdir(parents=True, exist_ok=True)
                shutil.copy2(zp, OUT_DIR / "package" / zp.name)
                report["package"] = {
                    "zip": zp.name, "bytes": zp.stat().st_size,
                    "entries": len(names),
                    "has_analysis": sorted(n for n in names
                                           if n.startswith("analysis/"))[:10],
                    "has_charts": sorted(n for n in names if n.lower().endswith(".png"))[:8],
                    "has_pdf": [n for n in names if n.lower().endswith(".pdf")],
                }
            else:
                report["package"] = {"keys": sorted(pack.keys())[:12],
                                     "note": "未取到 zip 路径（如实记录返回值）"}
        except Exception as exc:                   # noqa: BLE001
            report["package"] = {"error": str(exc)[:200]}
        try:
            import fitz                                  # PyMuPDF：页面 → PNG
            with fitz.open(str(OUT_DIR / "report.pdf")) as pdfdoc:
                pages = list(range(len(pdfdoc)))
                with_img = [i for i in pages
                            if pdfdoc[i].get_images(full=True)]
                shot = (with_img or pages)[0] if pages else None
                if shot is not None:
                    pix = pdfdoc[shot].get_pixmap(dpi=110)
                    out_png = OUT_DIR / f"screenshot_p{shot + 1}.png"
                    pix.save(str(out_png))
                    report["screenshot"] = {"file": out_png.name,
                                            "page": shot + 1,
                                            "pages_with_images": [i + 1 for i in with_img],
                                            "bytes": out_png.stat().st_size}
        except Exception as exc:                   # noqa: BLE001
            report["screenshot"] = {"error": str(exc)[:160]}
        report["artifacts"] = {p.name: {"bytes": p.stat().st_size, "sha256": _sha(p)}
                               for p in sorted(OUT_DIR.rglob("*")) if p.is_file()}
    finally:
        ws_mod.WORKSPACE_ROOT = old_root
        task_state.DB_PATH = old_db
        report["workspace"] = str(tmp)
    report["verification_boundary"] = {
        "verified": ["事实抽取→底稿→analysis worker 金融路径→三图→正文/摘要→装配验收"
                     "→PDF/导出（同一条生产函数链，离线、零模型调用）"],
        "not_verified": ["登录会话下的 POST /api/tasks 与页面点击",
                         "步骤派发/worker 回包（由既有离线用例覆盖）",
                         "实机重启后的运行"],
        "credentials": "未读取密钥、未复制会话 token、未伪造认证",
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"分析步 {report['analysis_step']['status']}；"
          f"运行 {[r['model_id'] for r in report['analysis_step']['runs']]}")
    print(f"三图 {[c['file'] for c in report['charts']]}")
    print(f"正文 {report['text']['note_chars']} 字符；摘要三条："
          f"{len(report['text']['summary_lines'])}；正文内工程标识 "
          f"{report['text']['engineering_ids_in_body']}")
    print(f"交付 {report['delivery']['status']}；PDF {report.get('pdf')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
