# -*- coding: utf-8 -*-
"""三类固定离线场景：跑**真实装配/渲染/导出路径**，产出可跨修订比对的产物清单。

为什么单独一个跑法：既有的研究路径离线用例是**测试私有**的（临时目录、跑完即删、
没有产物清单），跨修订比较只能手工 diff 一堆 JSON。这里把三份**冻结输入**
（`evals/scenarios/*.json`）驱动同一套生产函数，产物写进
`.weavemind/scenarios/<name>/`（运行期目录，不入库），并输出 `manifest.json`：

- 每个产物的 sha256 与字节数（正文、底稿、证据、图表、PDF、导出清单）；
- 交付状态与版本号（`delivery_pipeline.assemble_and_verify` 的真实返回）；
- 缺口（必需数据/证据/引用）与图表分级；
- 场景 `expect` 的**确定性核对**结果（数字/单位/引用/图表/缺口/版本）。

范围说明（照实写）：本跑法覆盖**装配→证据→简报→图表渲染→导出**的真实代码路径；
步骤派发与 worker 回包由既有离线用例（`test_offline_delivery`）覆盖，这里不重复。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import chart_assembly                                    # noqa: E402
import chart_specs                                       # noqa: E402
import delivery_pipeline as dp                           # noqa: E402
import narrative_evidence as ne                          # noqa: E402
import report_brief                                      # noqa: E402
import task_state                                        # noqa: E402
import workspace as ws_mod                               # noqa: E402
from facts import parse_research_request                 # noqa: E402

SCENARIO_DIR = ROOT / "evals" / "scenarios"
OUT_ROOT = ROOT / ".weavemind" / "scenarios"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _pdf_verdict(pdf_path: Path) -> dict:
    """导出 PDF 的**确定性**视觉判定：页数、空白页、文本量（不依赖视觉模型）。

    只判"能不能读"这一层：无页 / 空白页 / 整册几乎无文字 → fail 并给证据；
    排版观感（字号、拥挤、图文比例）由渲染后的 PNG 视觉评审负责，不在这里假装判过。
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        pages = []
        for i, page in enumerate(reader.pages, 1):
            try:
                chars = len((page.extract_text() or "").strip())
            except Exception:
                chars = 0
            pages.append({"page": i, "chars": chars})
    except Exception as exc:                          # noqa: BLE001 - 读不了照实记
        return {"verdict": "fail", "issues": [f"PDF 无法解析：{str(exc)[:120]}"], "pages": []}
    issues = []
    if not pages:
        issues.append("PDF 没有页")
    blank = [p["page"] for p in pages if p["chars"] < 50]
    if blank:
        issues.append(f"疑似空白页：{blank[:5]}")
    if pages and sum(p["chars"] for p in pages) < 200:
        issues.append("整册文字过少（<200 字符）")
    return {"verdict": "pass" if not issues else "fail", "issues": issues,
            "pages": pages, "page_count": len(pages)}


def run_scenario(spec: dict, *, out_root: Path = OUT_ROOT) -> dict:
    """跑一个场景，返回 manifest（含确定性核对结果）。"""
    name = str(spec.get("name") or "scenario")
    contract = spec.get("contract") or {}
    goal = str(spec.get("goal") or "")
    tmp = Path(tempfile.mkdtemp(prefix=f"scen_{name}_"))
    old_root, old_db = ws_mod.WORKSPACE_ROOT, task_state.DB_PATH
    ws_mod.configure_workspace_root(str(tmp))
    task_state.DB_PATH = str(tmp / "scenario.db")
    tid = f"scen-{name}"
    out_dir = out_root / name
    if out_dir.exists():
        shutil.rmtree(out_dir)
    try:
        req = parse_research_request(
            goal, company=str(contract.get("company") or ""),
            company_id=str(contract.get("company_id") or ""),
            market=str(contract.get("market") or ""),
            periods=[int(y) for y in (contract.get("periods") or [])],
            caliber=str(contract.get("caliber") or ""),
            as_of=str(contract.get("as_of") or ""),
            perspective=str(contract.get("perspective") or ""),
            identity_source=str(contract.get("identity_source") or "scenario"))
        task_state.mark_queued(tid, goal=goal, research_request=req.to_payload(),
                               db_path=task_state.DB_PATH)
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        _write(proj / "financials.json", spec.get("financials") or {})
        _write(proj / "fetch_snapshot.json", spec.get("docs") or [])
        ws = ws_mod.task_workspace(tid)

        # ① 底稿（真实读侧）
        import working_paper_export as WPX
        wp = WPX.write_working_paper(tid, goal, project="default")

        # ② 叙事证据（真实提取 + 契约校验）
        ev = ne.build(tid, goal=goal, ws_dir=ws)

        # ③ 图表（真实规格 + 真实渲染脚本子进程）
        data = WPX.chart_rows(tid, goal, project="default")
        specs = []
        if data.get("ok"):
            r0 = data.get("request") or {}
            try:
                from facts import subject_type_of
                stype, _src = subject_type_of(declared=str(r0.get("subject_type") or ""),
                                              company=str(r0.get("company") or ""),
                                              company_id=str(r0.get("company_id") or ""))
            except Exception:
                stype = "unknown"
            specs = chart_specs.financial_research_specs(
                data.get("rows") or [], data.get("derived") or [],
                unit=str(data.get("unit") or ""), source=str(data.get("source_label") or ""),
                company=str(r0.get("company") or ""), caliber=str(r0.get("caliber") or ""),
                periods=list(data.get("periods") or []),
                core_metrics=list(r0.get("required_metrics") or []), subject_type=stype)
        charts_dir = out_dir / "charts"
        charts_dir.mkdir(parents=True, exist_ok=True)
        render_log = ""
        if specs:
            work = Path(tempfile.mkdtemp(prefix=f"scenchart_{name}_"))
            (work / "render_charts.py").write_text(
                chart_assembly.RENDER_CHART_SCRIPT.replace("__REPO_ROOT__", str(ROOT)),
                encoding="utf-8")
            _write(work / "chart_data.json", {"charts": specs})
            proc = subprocess.run([sys.executable, "render_charts.py"], cwd=str(work),
                                  capture_output=True, text=True, timeout=600)
            render_log = (proc.stdout or "").strip().splitlines()[-1:] and \
                (proc.stdout or "").strip().splitlines()[-1] or ""
            for png in sorted(work.glob("chart_*.png")):
                shutil.copy2(png, charts_dir / png.name)
            if (work / "chart_manifest.json").exists():
                shutil.copy2(work / "chart_manifest.json", proj / "chart_manifest.json")
            shutil.rmtree(work, ignore_errors=True)

        # ④ 交付装配（真实顺序：装配 → 验收 → 版本 → 记录）；评审结论是**场景输入**
        #    （真实编排器把本次运行的评审事实传进来，这里同样显式传入，不假装有评审）
        body = str(spec.get("model_body") or "")
        review = spec.get("review")
        res = dp.assemble_and_verify(
            tid, goal, body, project="default", ws_dir=str(ws),
            review_facts=dict(review) if isinstance(review, dict) else None)

        # ⑤ 导出（真实 markdown 导出 + PDF + 导出清单）；导出读的是**任务库里的送达正文**，
        #    真实编排器会回写投影，这里同样回写（否则导出报 "report not found"）
        import web_ui
        try:
            task_state.update_delivery_projection(
                tid, report=str(res.get("report") or ""),
                status="SUCCESS" if res.get("status") != "draft" else "SUCCESS_WITH_ISSUES",
                db_path=task_state.DB_PATH)
        except Exception as exc:                      # noqa: BLE001 - 回写失败照实记
            print(f"[warn] 投影回写失败：{str(exc)[:120]}", file=sys.stderr)
        _write(out_dir / "report.md", str(res.get("report") or ""))
        pdf_note = ""
        try:
            pdf_bytes = web_ui._task_pdf_bytes(tid)
            (out_dir / "report.pdf").write_bytes(pdf_bytes)
            _write(out_dir / "export_manifest.json",
                   web_ui._read_export_manifest(tid) or {})
        except Exception as exc:                      # noqa: BLE001 - 导出失败照实记
            pdf_note = f"PDF/清单导出失败：{str(exc)[:120]}"

        # ⑥ 结构对象与产物清单
        st = report_brief.read_structure(tid, ws_dir=ws) or {}
        _acc_path = ws / "acceptance_report.json"
        acc = json.loads(_acc_path.read_text(encoding="utf-8")) if _acc_path.exists() else {}
        _write(out_dir / "report_structure.json", st)
        _write(out_dir / "working_paper.json",
               json.loads((proj / "working_paper.json").read_text(encoding="utf-8"))
               if (proj / "working_paper.json").exists() else {})
        _write(out_dir / "narrative_evidence.json", ev)

        quality = (st.get("metrics_table") or {}).get("quality") or []
        findings = [f.get("text") or "" for f in (st.get("findings") or [])]
        coverage = next((t for t in findings if "覆盖" in t), "")
        audit = [str(a.get("detail") or "") if isinstance(a, dict) else str(a)
                 for a in (st.get("audit") or [])]
        charts = [{"file": c["file"], "grade": c.get("grade"), "question": c.get("question"),
                   "observation": c.get("observation")}
                  for c in (st.get("charts") or [])]
        artifacts = {}
        for p in sorted(out_dir.rglob("*")):
            if p.is_file():
                artifacts[str(p.relative_to(out_dir)).replace("\\", "/")] = {
                    "sha256": _sha256(p), "bytes": p.stat().st_size}
        manifest = {
            "scenario": name,
            "note": str(spec.get("note") or ""),
            "contract": req.to_payload(),
            "goal": goal,
            "delivery": {"status": res.get("status"), "reason": res.get("reason"),
                         "version_id": res.get("version_id"),
                         "acceptance_overall": res.get("acceptance_overall"),
                         "hard_fail": res.get("hard_fail")},
            "acceptance": {"overall": acc.get("overall"),
                           "gaps": list(acc.get("gaps") or [])[:8],
                           "failed_checks": [k for k, c in (acc.get("checks") or {}).items()
                                             if not c.get("pass") and c.get("counted", True)]},
            "paper": {"ok": bool(wp.get("ok")), "rows": wp.get("rows"),
                      "derived": wp.get("derived"),
                      "gaps": len(wp.get("gaps") or []),
                      "problems": len(wp.get("problems") or [])},
            "paper_problems": [str(p.get("detail") or p) if isinstance(p, dict) else str(p)
                               for p in (wp.get("problems") or [])],
            "evidence": {"located": ev.get("located"),
                         "missing_labels": ev.get("missing_labels"),
                         "excluded": [{"title": e.get("title"), "url": e.get("url"),
                                       "validation_status": e.get("validation_status")}
                                      for e in (ev.get("excluded") or [])],
                         "rules_version": ev.get("rules_version")},
            "citations": [{"n": c.get("n"), "type": c.get("type"),
                           "admission": c.get("admission"), "url": c.get("url")}
                          for c in (st.get("citations") or [])],
            "findings": findings,
            "coverage_finding": coverage,
            "quality_metrics": sorted({str(q.get("metric")) for q in quality}),
            "audit": audit,
            "charts": charts,
            "render_log": render_log,
            "pdf_note": pdf_note,
            "visual_verdict": (_pdf_verdict(out_dir / "report.pdf")
                               if (out_dir / "report.pdf").exists()
                               else {"verdict": "unknown", "issues": [pdf_note or "未生成 PDF"],
                                     "pages": []}),
            "artifacts": artifacts,
        }
        manifest["checks"] = _check(manifest, spec.get("expect") or {})
        _write(out_dir / "manifest.json", manifest)
        return manifest
    finally:
        ws_mod.configure_workspace_root(str(old_root))
        task_state.DB_PATH = old_db
        shutil.rmtree(tmp, ignore_errors=True)


def _check(manifest: dict, expect: dict) -> dict:
    """场景预期 → 确定性核对（数字/单位/引用/图表/缺口/版本）。"""
    out: dict = {}
    if "delivery_status" in expect:
        out["delivery_status"] = manifest["delivery"]["status"] == expect["delivery_status"]
    if "delivery_draft" in expect:
        out["delivery_draft"] = (manifest["delivery"]["status"] == "draft") == bool(expect["delivery_draft"])
    if "coverage_contains" in expect:
        out["coverage_contains"] = expect["coverage_contains"] in str(manifest.get("coverage_finding") or "")
    if "evidence_located_min" in expect:
        out["evidence_located_min"] = int(manifest["evidence"].get("located") or 0) >= int(expect["evidence_located_min"])
    if "evidence_missing" in expect:
        out["evidence_missing"] = list(manifest["evidence"].get("missing_labels") or []) == list(expect["evidence_missing"])
    if "evidence_missing_min" in expect:
        out["evidence_missing_min"] = len(manifest["evidence"].get("missing_labels") or []) >= int(expect["evidence_missing_min"])
    if "excluded_reason" in expect:
        out["excluded_reason"] = any(str(e.get("validation_status")) == expect["excluded_reason"]
                                     for e in (manifest["evidence"].get("excluded") or []))
    if "no_yoy_for" in expect:
        got = {m for m in manifest.get("quality_metrics") or [] if m.endswith("_yoy")}
        want_absent = {f"{m}_yoy" for m in expect["no_yoy_for"]}
        out["no_yoy_for"] = not (got & want_absent)
    if "problems_contain" in expect:
        out["problems_contain"] = any(expect["problems_contain"] in str(p)
                                      for p in manifest.get("paper_problems") or [])
    if "revenue_yoy_present" in expect:
        out["revenue_yoy_present"] = ("revenue_yoy" in (manifest.get("quality_metrics") or [])) \
            == bool(expect["revenue_yoy_present"])
    if "audit_contains" in expect:
        out["audit_contains"] = any(expect["audit_contains"] in a for a in manifest.get("audit") or [])
    if "chart_min" in expect:
        out["chart_min"] = len(manifest.get("charts") or []) >= int(expect["chart_min"])
    out["all_passed"] = all(out.values()) if out else True
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    only = argv[0] if argv else ""
    files = sorted(SCENARIO_DIR.glob("*.json"))
    if only:
        files = [f for f in files if f.stem == only]
    if not files:
        print("没有匹配的场景定义（evals/scenarios/*.json）", file=sys.stderr)
        return 2
    failed = []
    for f in files:
        spec = json.loads(f.read_text(encoding="utf-8"))
        m = run_scenario(spec)
        checks = m.get("checks") or {}
        bad = [k for k, v in checks.items() if k != "all_passed" and not v]
        print(f"[{'OK ' if not bad else 'FAIL'}] {m['scenario']}: "
              f"交付={m['delivery']['status']} 证据={m['evidence']['located']} "
              f"缺口={len(m['evidence'].get('missing_labels') or [])} 图={len(m['charts'])} "
              f"| 核对 {len(checks) - 1} 项" + (f"，未过：{bad}" if bad else ""))
        print(f"        产物：{OUT_ROOT / m['scenario']}")
        if bad:
            failed.append((m["scenario"], bad))
    print(f"场景 {len(files)} 个，失败 {len(failed)}：{failed}" if failed else
          f"场景 {len(files)} 个全部通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
