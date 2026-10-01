# -*- coding: utf-8 -*-
"""V2 证据：**改两项假设 → 页面与导出展示同一组结果**（隔离跑生产入口，无会话）。

为什么这样跑（边界如实写明）：页面动作需要登录会话，运行侧没有可用凭据、也不复制会话
token。本脚本调用的是页面「改假设→复算」按钮后面**同一个处理函数**
（`web_ui._post_task_analysis_recompute`），再用**同一份工作区运行**渲染简报并导出 PDF，
最后核对：复算返回的数、正文渲染的数、PDF 提取文本里的数**一致**。

产出：
- `docs/evidence/v2_page_export.json`：两次改假设的读数、正文命中、PDF 命中、页面/导出是否一致；
- `docs/evidence/v2_page_export/`：`report.md`、`report.pdf`、命中页截图。
"""
from __future__ import annotations

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

OUT_DIR = ROOT / "docs" / "evidence" / "v2_page_export"
REPORT = ROOT / "docs" / "evidence" / "v2_page_export.json"
DOC = (ROOT / "evals" / "a2_official_chain_20260929" / "002304" / "project"
       / "materials" / "f42e747c73850b59" / "doc.json")
GOAL = ("洋河股份 2023/2024 经营研究：利润由何而来、现金为何变化、什么条件会改变判断；"
        "合并口径，数据截至 2025-04-30。")
# 两次"用户改假设"：一个偏乐观、一个偏悲观（都在 allowed_params 范围内）
ASSUMPTIONS = (
    {"revenue_growth": 0.05, "gross_margin_delta": 0.01, "expense_change_ratio": 0.0},
    {"revenue_growth": -0.05, "gross_margin_delta": -0.01, "expense_change_ratio": 0.02},
)


class _H:
    """最小 handler 替身：只记录处理函数的返回值（与既有页面用例同法）。"""

    def __init__(self):
        self.last = None

    def _json(self, payload, status=200):
        self.last = (payload, status)
        return payload


def main() -> int:
    import delivery_pipeline as dp
    import facts as F
    import report_brief
    import task_state
    import web_ui
    import workspace as ws_mod
    from financial_analysis import store as fa_store

    tmp = Path(tempfile.mkdtemp(prefix="v2_page_"))
    old_root, old_db = ws_mod.WORKSPACE_ROOT, task_state.DB_PATH
    ws_mod.configure_workspace_root(str(tmp))
    task_state.DB_PATH = str(tmp / "v2.db")
    tid = "v2-page"
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {"case": "V2 改两项假设 → 页面与导出同一组结果（隔离跑通）",
                    "task_id": tid, "goal": GOAL}
    try:
        doc = json.loads(DOC.read_text(encoding="utf-8"))
        # **研究契约**要落库：装配层据此走研究简报路径（含分析摘要/正文/三图），
        # 否则只会渲染通用正文（本脚本第一次就是这么错的）。
        req = F.parse_research_request(
            GOAL, company="洋河股份", company_id="002304.SZ", market="cn",
            periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
            identity_source="v2-consistency")
        task_state.mark_queued(tid, goal=GOAL, research_request=req.to_payload(),
                               db_path=task_state.DB_PATH)
        facts = F.facts_from_annual_tables(doc, company="洋河股份", company_code="002304",
                                           periods=(2023, 2024), as_of="2025-04-30",
                                           disclosed_at="2025-04-28")
        import financial_analysis as fa
        ds = fa.freeze_from_facts(facts, periods=(2023, 2024), entity="洋河股份",
                                  entity_id="002304.SZ", as_of="2025-04-30",
                                  source_label="v2-page")
        ws = ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        # 交付装配从 `project/financials.json` 建底稿（生产同一入口）：数值全部来自上面抽取
        # 的事实，只取**合并口径**（母公司数不能当公司数）。
        want = ("revenue", "net_profit", "operating_cashflow", "total_assets",
                "total_liabilities")
        by_year: dict = {}
        for f in facts:
            if str(getattr(f, "caliber", "") or "") != "合并":
                continue
            metric = str(getattr(f, "metric", "") or "")
            period = str(getattr(f, "period", "") or "")
            if metric not in want or not period.endswith("年") or getattr(f, "value", None) is None:
                continue
            try:
                year = int(period[:4])
            except ValueError:
                continue
            if year in (2023, 2024):
                by_year.setdefault(year, {})[metric] = float(f.value)
        if len(by_year) >= 2:
            (proj / "financials.json").write_text(json.dumps({
                "financials": [dict({"year": y, "report_type": "年报",
                                     "disclosure_date": "2025-04-28"}, **by_year[y])
                               for y in sorted(by_year)],
                "metadata": {"source": "cninfo_annual_report_tables", "company": "洋河股份",
                             "company_id": "002304.SZ", "currency": "CNY", "unit": "元",
                             "caliber": "合并",
                             "caliber_evidence": "合并利润表/合并现金流量表标题",
                             "as_of": "2025-04-30"},
                "raw": {"url": str(doc.get("url") or "cached:002304"), "text": "{}"},
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        fa_store.save_inputs(ws, dataset=ds, plan=fa.compile_plan(GOAL, ds))
        # 隔离跑法的小坑：`mark_queued` 之后 `task_project_dir(tid,"default")` 指向
        # `<root>/projects/default/<tid>/project`，而页面处理函数按 `task_workspace(tid)`
        # 取工作区。两处都放一份可复算输入，保证复算读得到（生产里两者是同一个位置）。
        _alt = proj.parent
        if _alt != ws:
            fa_store.save_inputs(_alt, dataset=ds, plan=fa.compile_plan(GOAL, ds))
        # 自检：可复算输入确实落盘（复算处理函数读的就是这一份；缺了会 409 no_inputs）
        report["inputs_written"] = {
            "ws": str(ws),
            "dataset_json": (ws / "analysis" / "dataset.json").is_file(),
            "payload_keys": sorted((fa_store.input_payload_bytes(ws) or {}).keys()),
        }
        # 三个模型的运行（与正常路径同一组），其中情景一条作为"初始假设"
        for mid in ("operating_drivers", "cash_reconciliation"):
            fa_store.save_run(ws, fa.run(mid, ds))
        fa_store.save_run(ws, fa.run("scenario_sensitivity", ds, params={}))

        # ① 页面动作：改假设 → 复算（生产处理函数，两次假设）
        # `_task_exists` 读的是运行期任务库；隔离目录里没有注册任务，这里按既有页面用例
        # 的做法打桩为 True（只跳过"任务是否在库"这一步，处理逻辑本身不替换）。
        import unittest.mock as _mock
        _patch = _mock.patch.object(web_ui, "_task_exists", lambda _tid: True)
        _patch.start()
        calls = []
        for params in ASSUMPTIONS:
            h = _H()
            web_ui._post_task_analysis_recompute(
                h, f"/api/task/{tid}/analysis/recompute",
                {"model_id": "scenario_sensitivity", "params": dict(params)}, {"user": "admin"})
            payload, status = h.last or ({}, 0)
            outs = {o.get("metric"): o.get("value") for o in (payload.get("outputs") or ())}
            calls.append({"params": dict(params), "status": status,
                          "run_id": payload.get("run_id"),
                          "error": payload.get("error"),
                          "error_code": payload.get("code"),
                          "scenario_net_profit_yuan": outs.get("scenario_net_profit"),
                          "revenue_threshold_pct":
                              outs.get("revenue_growth_to_hold_target"),
                          "adopted": payload.get("adopted")})
        report["recompute_calls"] = calls

        # ② 采纳第一次复算的运行 → 渲染正文（与页面同一条链）
        first_run = None
        for r in fa_store.validated_runs(ws):
            if str(r.run_id) == str(calls[0].get("run_id") or ""):
                first_run = r
                break
        body = ("本报告由确定性分析链装配：利润、现金与情景三段见『分析摘要』与"
                "『经营驱动分析正文』；底稿与来源随包提供。")
        # 采纳这条运行，使正文/导出都读它（同版绑定）
        if first_run is not None:
            fa_store.save_selection(ws, {
                "model_id": "scenario_sensitivity", "run_id": first_run.run_id,
                "dataset_hash": ds.dataset_hash, "params": dict(first_run.params),
                "rules_version": fa.validation.RULES_VERSION,
            }, note="V2 页面/导出一致性验证")
        res = dp.assemble_and_verify(tid, GOAL, body, project="default", ws_dir=str(ws))
        md = str(res.get("report") or "")
        (OUT_DIR / "report.md").write_text(md, encoding="utf-8")
        try:
            task_state.update_delivery_projection(
                tid, report=md, status="SUCCESS_WITH_ISSUES", db_path=task_state.DB_PATH)
        except Exception:                          # noqa: BLE001
            pass
        pdf = web_ui._task_pdf_bytes(tid)
        (OUT_DIR / "report.pdf").write_bytes(pdf)

        # ③ 三处对照：复算返回值 / 正文文本 / PDF 提取文本
        def _plain(v):
            try:
                return f"{float(v) / 1e8:,.2f}"
            except (TypeError, ValueError):
                return ""

        target = _plain(calls[0].get("scenario_net_profit_yuan"))
        in_md = bool(target) and target in md
        import pypdf
        text = "".join((pg.extract_text() or "")
                       for pg in pypdf.PdfReader(str(OUT_DIR / "report.pdf")).pages)
        in_pdf = bool(target) and target in text
        report["consistency"] = {
            "recomputed_scenario_net_profit_yuan": calls[0].get("scenario_net_profit_yuan"),
            "formatted_yi": target,
            "in_report_md": in_md, "in_pdf_text": in_pdf,
            "same_result": bool(in_md and in_pdf),
            "note": ("同一组假设下：页面复算返回值、正文、PDF 三处必须显示同一个数；"
                     "两次复算都不自动采纳（adopted=false），采纳由用户显式动作完成"),
        }
        try:
            import fitz
            with fitz.open(str(OUT_DIR / "report.pdf")) as pdfdoc:
                page = next((i for i, pg in enumerate(pdfdoc)
                             if target and target in (pg.get_text() or "")), 0)
                pix = pdfdoc[page].get_pixmap(dpi=110)
                shot = OUT_DIR / f"screenshot_p{page + 1}.png"
                pix.save(str(shot))
                report["screenshot"] = {"file": shot.name, "page": page + 1,
                                        "bytes": shot.stat().st_size}
        except Exception as exc:                   # noqa: BLE001
            report["screenshot"] = {"error": str(exc)[:160]}
        report["artifacts"] = sorted(p.name for p in OUT_DIR.iterdir() if p.is_file())
    finally:
        try:
            _patch.stop()
        except Exception:                          # noqa: BLE001
            pass
        ws_mod.WORKSPACE_ROOT = old_root
        task_state.DB_PATH = old_db
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for c in report["recompute_calls"]:
        print(f"假设 {c['params']} → 情景归母净利 {c['scenario_net_profit_yuan']} "
              f"（HTTP {c['status']}，adopted={c.get('adopted')}）")
    print(f"三处一致：{report['consistency']['same_result']} "
          f"（正文 {report['consistency']['in_report_md']}／PDF "
          f"{report['consistency']['in_pdf_text']}）")
    return 0 if report["consistency"]["same_result"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
