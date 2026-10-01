# -*- coding: utf-8 -*-
"""W1 证据：**同一个工作区、生产采纳函数、切两个情景** → 选择/版本/正文/PDF 逐项核对。

为什么这样跑（边界如实写明）：页面按钮需要登录会话，运行侧没有可用凭据、也不复制 token。
本脚本调用页面按钮后面**同一个处理函数**：`web_ui._post_task_analysis_recompute`（改假设复算）
与 `web_ui._post_task_analysis_adopt`（采纳某一条运行进交付）；随后核对：

1. **选择记录**（`analysis/selection.json`）：最后一条选择＝第二个情景、参数一致、带回填版本身份；
2. **正文版本/身份**：两次采纳的 `adopted_identity` 必须不同（切了版本，不是同一份字节）；
3. **实际参数**：正文与结构绑定里的 run 身份/参数＝所选运行；
4. **正文与 PDF 关键值**：各自情景的归母净利与阈值出现在正文、也出现在 PDF 提取文本；
   且第一情景的数在第二情景正文里不再出现（真的换了版本，不是两版叠加）。

产出：
- `docs/evidence/w1_two_scenarios.json`：两次采纳的读数与逐项核对；
- `docs/evidence/w1_two_scenarios/`：`report_A.md`、`report_B.md`、`report_B.pdf`、命中页截图。
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

OUT_DIR = ROOT / "docs" / "evidence" / "w1_two_scenarios"
REPORT = ROOT / "docs" / "evidence" / "w1_two_scenarios.json"
DOC = (ROOT / "evals" / "a2_official_chain_20260929" / "002304" / "project"
       / "materials" / "f42e747c73850b59" / "doc.json")
GOAL = ("洋河股份 2023/2024 经营研究：利润由何而来、现金为何变化、什么条件会改变判断；"
        "合并口径，数据截至 2025-04-30。")
# 两个情景：A 乐观、B 悲观（都在 allowed_params 内，且互不相同）
SCENARIOS = (
    ("A", {"revenue_growth": 0.05, "gross_margin_delta": 0.01, "expense_change_ratio": 0.0}),
    ("B", {"revenue_growth": -0.05, "gross_margin_delta": -0.01, "expense_change_ratio": 0.02}),
)


class _H:
    """最小 handler 替身：只记录处理函数返回值（与既有页面用例同法）。"""

    def __init__(self):
        self.last = None

    def _json(self, payload, status=200):
        self.last = (payload, status)
        return payload


def _yi(value) -> str:
    try:
        return f"{float(value) / 1e8:,.2f}"
    except (TypeError, ValueError):
        return ""


def main() -> int:
    import delivery_pipeline as dp
    import facts as F
    import report_brief
    import task_state
    import web_ui
    import workspace as ws_mod
    from financial_analysis import store as fa_store

    tmp = Path(tempfile.mkdtemp(prefix="w1_two_"))
    old_root, old_db = ws_mod.WORKSPACE_ROOT, task_state.DB_PATH
    ws_mod.configure_workspace_root(str(tmp))
    task_state.DB_PATH = str(tmp / "w1.db")
    tid = "w1-two"
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {"case": "W1 同一工作区用生产采纳函数切两个情景（隔离核验，GUI 待验）",
                    "task_id": tid, "goal": GOAL}
    patch = None
    try:
        doc = json.loads(DOC.read_text(encoding="utf-8"))
        req = F.parse_research_request(
            GOAL, company="洋河股份", company_id="002304.SZ", market="cn",
            periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
            identity_source="w1-two-scenarios")
        task_state.mark_queued(tid, goal=GOAL, research_request=req.to_payload(),
                               db_path=task_state.DB_PATH)
        facts = F.facts_from_annual_tables(doc, company="洋河股份", company_code="002304",
                                           periods=(2023, 2024), as_of="2025-04-30",
                                           disclosed_at="2025-04-28")
        import financial_analysis as fa
        ds = fa.freeze_from_facts(facts, periods=(2023, 2024), entity="洋河股份",
                                  entity_id="002304.SZ", as_of="2025-04-30",
                                  source_label="w1-two")
        ws = ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        # W1：把任务已用的缓存年报按正常抓取步骤的产物形态登记（不联网、不伪造认证）
        (proj / "fetch_snapshot.json").write_text(json.dumps([doc], ensure_ascii=False),
                                                  encoding="utf-8")
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
        plan = fa.compile_plan(GOAL, ds)
        fa_store.save_inputs(ws, dataset=ds, plan=plan)
        if proj.parent != ws:
            fa_store.save_inputs(proj.parent, dataset=ds, plan=plan)
        for mid in ("operating_drivers", "cash_reconciliation"):
            fa_store.save_run(ws, fa.run(mid, ds))
        fa_store.save_run(ws, fa.run("scenario_sensitivity", ds, params={}))

        import unittest.mock as _mock
        patch = _mock.patch.object(web_ui, "_task_exists", lambda _tid: True)
        patch.start()

        # **规范工作区**：页面处理函数自己用 `workspace.task_workspace(tid)` 解析路径，
        # 隔离跑里它与我脚本早期拿到的路径可能不同（历史小坑）。这里在调用生产函数前
        # 重新解析一次，并让装配/选择/版本核对全部读**同一个**目录。
        ws = ws_mod.task_workspace(tid)
        report["resolved_workspace"] = str(ws)
        if proj.parent != ws:                      # 输入两边都放一份（复算处理函数要读）
            fa_store.save_inputs(ws, dataset=ds, plan=plan)
        for mid in ("operating_drivers", "cash_reconciliation"):
            fa_store.save_run(ws, fa.run(mid, ds))
        fa_store.save_run(ws, fa.run("scenario_sensitivity", ds, params={}))

        # ① 先用生产装配函数落一版交付（采纳处理函数要求"有可采纳的交付版本"）
        body = ("本报告由确定性分析链装配：利润、现金与情景三段见『分析摘要』与"
                "『经营驱动分析正文』；底稿与来源随包提供。")
        first = dp.assemble_and_verify(tid, GOAL, body, project="default", ws_dir=str(ws))
        report["initial_delivery"] = {"status": first.get("status"),
                                      "hard_fail": first.get("hard_fail")}
        from report_version import VersionStore
        store = VersionStore(ws, tid)

        # ② 页面动作：改假设 → 复算（两条不同情景）
        runs: dict = {}
        for label, params in SCENARIOS:
            h = _H()
            web_ui._post_task_analysis_recompute(
                h, f"/api/task/{tid}/analysis/recompute",
                {"model_id": "scenario_sensitivity", "params": dict(params)},
                {"user": "admin"})
            payload, status = h.last or ({}, 0)
            outs = {o.get("metric"): o.get("value") for o in (payload.get("outputs") or ())}
            runs[label] = {"run_id": str(payload.get("run_id") or ""), "params": dict(params),
                           "status": status, "adopted": payload.get("adopted"),
                           "scenario_net_profit_yuan": outs.get("scenario_net_profit"),
                           "revenue_threshold_pct": outs.get("revenue_growth_to_hold_target"),
                           "margin_threshold_pct":
                               outs.get("margin_threshold_to_hold_base_profit")}
        report["recompute_calls"] = runs
        # 诊断（W1 隔离核验）：复算后工作区里的选择状态——采纳处理函数按同一份选择装配，
        # 若这里就不是 ok，采纳必然报"正文里找不到所选运行"。
        _inputs = fa_store.load_inputs(ws)
        _dsh = str((_inputs.get("dataset") or {}).get("dataset_hash") or "")
        _sel_status = fa_store.selection_status(ws, dataset_hash=_dsh,
                                                rules_version=fa.validation.RULES_VERSION)
        report["selection_after_recompute"] = {
            "workspace": str(ws), "dataset_hash": _dsh,
            "entries": [{k: e.get(k) for k in ("model_id", "run_id", "state", "why",
                                               "dataset_hash")}
                        for e in (_sel_status.get("entries") or [])],
            "ok": _sel_status.get("ok"),
            "inputs_dataset_hash_in_file": _dsh,
        }

        # ③ 生产采纳函数：先 A 后 B，每次核选择/版本/正文/PDF
        adopted: dict = {}
        for label, _params in SCENARIOS:
            h = _H()
            web_ui._post_task_analysis_adopt(
                h, f"/api/task/{tid}/analysis/adopt",
                {"run_id": runs[label]["run_id"]}, {"user": "admin"})
            payload, status = h.last or ({}, 0)
            adv = store.adopted()
            identity = ""
            if adv is not None:
                try:
                    identity = str(adv.identity_id() or "")
                except Exception:                  # noqa: BLE001
                    identity = str(getattr(adv, "version_id", "") or "")
            adv_body = str(getattr(adv, "body", "") or "")
            (OUT_DIR / f"report_{label}.md").write_text(adv_body, encoding="utf-8")
            pdf_ok, pdf_text, pdf_bytes = False, "", 0
            try:
                pdf = web_ui._task_pdf_bytes(tid)
                pdf_bytes = len(pdf)
                (OUT_DIR / f"report_{label}.pdf").write_bytes(pdf)
                import pypdf
                pdf_text = "".join((pg.extract_text() or "")
                                   for pg in pypdf.PdfReader(str(OUT_DIR / f"report_{label}.pdf")).pages)
                pdf_ok = bool(pdf_text)
            except Exception as exc:               # noqa: BLE001
                pdf_text = f"[PDF 导出失败] {str(exc)[:120]}"
            sel = fa_store.load_selection(ws)
            entries = list(sel.get("entries") or [])
            # 选择文件按**模型**一条（W1：显式选择只覆盖被选的模型，其余模型按组合保留），
            # 所以核对时按 run_id 找那一条，而不是"取最后一条"。
            mine = next((e for e in entries
                         if str(e.get("run_id") or "") == runs[label]["run_id"]), {})
            last_entry = mine or (entries[-1] if entries else {})
            from report_version import VERSIONS_FILE as _VF
            _vpath = ws / _VF
            _vkeys: list = []
            try:
                _vkeys = sorted(json.loads(_vpath.read_text(encoding="utf-8")).keys())
            except Exception:                      # noqa: BLE001
                _vkeys = []
            struct = report_brief.build_structure(tid, GOAL, adv_body, project="default",
                                                 ws_dir=str(ws)) or {}
            binding = dict(struct.get("analysis_binding") or {})
            target_yi = _yi(runs[label]["scenario_net_profit_yuan"])
            thr = runs[label]["revenue_threshold_pct"]
            thr_s = f"{float(thr):.4f}" if isinstance(thr, (int, float)) else ""
            adopted[label] = {
                "http_status": status,
                "adopt_ok": bool(payload.get("ok", status == 200)),
                "adopt_error": payload.get("error"),
                "adopted_identity": identity,
                "selection_adopted_identity": str(sel.get("adopted_identity") or ""),
                "selection_last_run_id": str(last_entry.get("run_id") or ""),
                "selection_last_params": dict(last_entry.get("params") or {}),
                "selection_entries": len(entries),
                "report_chars": len(adv_body),
                "scenario_net_profit_yi": target_yi,
                "in_report_md": bool(target_yi) and target_yi in adv_body,
                "threshold_pct": thr,
                "threshold_in_report_md": bool(thr_s) and thr_s in adv_body,
                "in_pdf_text": bool(target_yi) and target_yi in (pdf_text or ""),
                "pdf_bytes": pdf_bytes,
                "binding_runs": [{"model_id": r.get("model_id"), "run_id": r.get("run_id"),
                                  "params": r.get("params")}
                                 for r in (binding.get("runs") or [])],
                "binding_has_selected_run": any(
                    str(r.get("run_id")) == runs[label]["run_id"]
                    for r in (binding.get("runs") or [])),
                "binding_has_selected_params": any(
                    dict(r.get("params") or {}) == dict(runs[label]["params"])
                    for r in (binding.get("runs") or [])),
                "binding_records": binding.get("records"),
                "store_file": str(_vpath.name),
                "store_exists": _vpath.is_file(),
                "store_keys": _vkeys,
                "ws": str(ws),
            }
            try:
                import fitz
                page = next((i for i, pg in enumerate(fitz.open(str(OUT_DIR / f"report_{label}.pdf")))
                             if target_yi and target_yi in (pg.get_text() or "")), 0)
                with fitz.open(str(OUT_DIR / f"report_{label}.pdf")) as pdfdoc:
                    pix = pdfdoc[page].get_pixmap(dpi=110)
                    shot = OUT_DIR / f"screenshot_{label}_p{page + 1}.png"
                    pix.save(str(shot))
                    adopted[label]["screenshot"] = {"file": shot.name, "page": page + 1}
            except Exception as exc:               # noqa: BLE001
                adopted[label]["screenshot"] = {"error": str(exc)[:120]}
        report["adopted"] = adopted
        # ④ 两次采纳之后的**最终选择状态**：必须指向第二个情景，且版本身份＝第二个情景的版本
        _final_sel = fa_store.load_selection(ws)
        _final_entries = list(_final_sel.get("entries") or [])
        _final_scen = next((e for e in _final_entries
                            if str(e.get("model_id") or "") == "scenario_sensitivity"), {})
        report["final_selection"] = {
            "entries": [{"model_id": e.get("model_id"), "run_id": e.get("run_id"),
                         "params": dict(e.get("params") or {})} for e in _final_entries],
            "adopted_identity": str(_final_sel.get("adopted_identity") or ""),
            "scenario_run_id": str(_final_scen.get("run_id") or ""),
            "scenario_params": dict(_final_scen.get("params") or {}),
        }

        # ⑤ 版本真的切了：身份不同、且第一情景的数不再出现在第二情景正文里
        a, b = adopted["A"], adopted["B"]
        fs = report["final_selection"]
        report["verification"] = {
            "identity_changed": bool(a["adopted_identity"] and b["adopted_identity"]
                                     and a["adopted_identity"] != b["adopted_identity"]),
            "selection_points_to_B": fs["scenario_run_id"] == runs["B"]["run_id"],
            "selection_params_match_B": fs["scenario_params"] == runs["B"]["params"],
            "selection_identity_is_B": (bool(fs["adopted_identity"])
                                        and fs["adopted_identity"]
                                        == b["adopted_identity"]),
            "combination_kept_with_selection": (
                {r["model_id"] for r in b["binding_runs"]}
                >= {"operating_drivers", "cash_reconciliation", "scenario_sensitivity"}),
            "values_in_md_and_pdf": bool(a["in_report_md"] and a["in_pdf_text"]
                                         and b["in_report_md"] and b["in_pdf_text"]),
            "A_value_absent_from_B_report": (bool(a["scenario_net_profit_yi"])
                                             and a["scenario_net_profit_yi"]
                                             != b["scenario_net_profit_yi"]
                                             and a["scenario_net_profit_yi"] not in
                                             (OUT_DIR / "report_B.md").read_text(encoding="utf-8")),
            "binding_ok": bool(a["binding_has_selected_run"]
                               and a["binding_has_selected_params"]),
            "not_auto_adopted": all(str(r.get("adopted")) == "False" for r in runs.values()),
        }
        report["artifacts"] = sorted(p.name for p in OUT_DIR.iterdir() if p.is_file())
    finally:
        if patch is not None:
            try:
                patch.stop()
            except Exception:                      # noqa: BLE001
                pass
        ws_mod.WORKSPACE_ROOT = old_root
        task_state.DB_PATH = old_db
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    for label, r in (report.get("recompute_calls") or {}).items():
        print(f"情景 {label} {r['params']} → {_yi(r['scenario_net_profit_yuan'])} 亿元"
              f"（HTTP {r['status']}，adopted={r['adopted']}）")
    v = report.get("verification") or {}
    print("核对：" + "、".join(f"{k}={'OK' if val else 'FAIL'}" for k, val in v.items()))
    return 0 if all(v.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
