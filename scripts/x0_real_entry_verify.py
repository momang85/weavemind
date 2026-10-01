# -*- coding: utf-8 -*-
"""X0 真实入口核对：在**当前实测任务工作区的副本**上验证两处共享边界。

为什么在副本上做：真实任务 `ui-17947f055b` 仍在运行，架构要求"不重启服务、不改其输入、
不代替用户采纳"。所以本脚本把它的工作区**整目录复制**到临时根下，只读原件、只改副本；
产出的读数与"原件是否被改动"的哈希对照都写进证据。

核对两处（阶段X §6 / 端到端实测 §主线结构问题）：
1. **结构化可用时也共同增强官方事实**：`financials.json`（eastmoney，单位亿元）可用时，
   `working_paper_export.build_result` 是否把已准入官方 2024 年报的三表/现金附注一并并入，
   使营业成本/合并净利/现金附注进入底稿 → 经营驱动/现金调节**自然适用**；
   同时金额统一到**元**（原单位留在 unit_source）。
2. **金额口径一致**：已存在的运行（金额为亿元）经共享格式器渲染，UI/摘要/图注显示
   66.73/80.32 亿元，而不是 0.00。

用法：`python scripts/x0_real_entry_verify.py`
"""
from __future__ import annotations

import hashlib
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

SRC = Path("C:/Users/ding0/AppData/Local/Temp/agent_workspace/tasks")
TID = "ui-17947f055b"
OUT = ROOT / "docs" / "evidence" / "x0_real_entry_verify.json"
GOAL = ("洋河股份 2023/2024 经营研究：利润由何而来、现金为何变化、什么条件会改变判断；"
        "合并口径，数据截至 2025-04-30。")


def _sha(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:                              # noqa: BLE001
        return ""


def main() -> int:
    src_task = SRC / "projects" / "default" / TID
    if not src_task.is_dir():
        print(f"找不到实测任务工作区：{src_task}")
        return 1
    rec: dict = {"case": "X0 真实入口核对（实测任务工作区副本）",
                 "source_workspace": str(src_task), "task_id": TID,
                 "read_only_on_source": True}
    # 原件指纹（核对结束再算一次，证明没动过原件）
    before = {str(p.relative_to(src_task)): (p.stat().st_size, _sha(p) if p.is_file() else "")
              for p in sorted(src_task.rglob("*")) if p.is_file()}

    tmp = Path(tempfile.mkdtemp(prefix="x0_real_"))
    root = tmp / "tasks"
    copy_task = root / "projects" / "default" / TID
    copy_task.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_task, copy_task)
    rec["copy"] = {"root": str(root), "files": sum(1 for _ in copy_task.rglob("*"))}

    import task_state
    import workspace as ws_mod
    old_root, old_db = ws_mod.WORKSPACE_ROOT, task_state.DB_PATH
    ws_mod.configure_workspace_root(str(root))
    task_state.DB_PATH = str(tmp / "tasks.db")
    # 副本核对用的任务库是**空的**，而底稿装配要从任务库读研究契约（主体/期间/口径/as_of）。
    # 这里按实测任务表单的同一契约登记一次（不改原件、不读账号）——否则 request_source
    # 会退化成"从目标文本解析"、主体为空，官方原文路径会如实报"没有研究契约主体"。
    try:
        task_state.mark_queued(
            TID, goal=GOAL, db_path=task_state.DB_PATH,
            research_request={"company": "洋河股份", "company_id": "002304.SZ",
                              "market": "cn", "periods": [2023, 2024], "caliber": "合并",
                              "as_of": "2025-04-30", "identity_source": "form"})
    except Exception as exc:                       # noqa: BLE001
        rec["contract_seed_error"] = str(exc)[:160]
    try:
        import financial_analysis as fa
        import working_paper_export as wpe
        from workers.data_analyzer_worker import DataAnalyzerWorker

        # ① 底稿：结构化载荷 + 已准入官方三表**共同增强**吗？
        res = wpe.build_result(TID, GOAL, project="default")
        rows = list(res.get("rows_detail") or [])
        metrics = sorted({str(r.get("metric")) for r in rows})
        by_key = {(str(r.get("metric")), str(r.get("period"))): r for r in rows}
        rev24 = by_key.get(("revenue", "2024年")) or {}
        rec["working_paper"] = {
            "ok": bool(res.get("ok")), "request_source": res.get("request_source"),
            "rows": res.get("rows"), "paper_ok": res.get("paper_ok"),
            "metrics": metrics,
            "completeness": res.get("completeness"),
            "official_notes": res.get("official_notes"),
            "revenue_2024": {"value": rev24.get("value"), "unit": rev24.get("unit"),
                             "agreeing_count": ((rev24.get("source_locator") or {})
                                                .get("agreeing_count")),
                             "locator": ((rev24.get("source_locator") or {})
                                         .get("locator") or "")[:120]},
            "has_operating_cost": "operating_cost" in metrics,
            "has_consolidated_net_profit": "net_profit_consolidated" in metrics,
            "has_cash_supplement": any(m in metrics for m in
                                       ("depreciation", "operating_payable_increase",
                                        "operating_receivable_decrease")),
            "conflicts": [p for p in (res.get("problems") or [])
                          if str(p.get("kind")) == "conflicting_candidates"],
        }
        # ② 用**修正后**的底稿落盘到副本 → 冻结 → 注册模型是否自然适用
        wp = wpe.write_working_paper(TID, GOAL, project="default")
        ds, label, locators = DataAnalyzerWorker._freeze_dataset(
            "working_paper", copy_task / "project" / "working_paper.json",
            task={"goal": GOAL, "context": {"root_task_id": TID}},
            required_metrics=("revenue", "net_profit", "gross_profit",
                              "operating_cashflow"),
            available_models=[m.model_id for m in fa.specs()])
        plan = fa.compile_plan(GOAL, ds)

        def _a(obj, name, default=""):
            return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)

        runs = {}
        for item in list(plan.adopted):
            mid = str(_a(item, "model_id"))
            run = fa.run(mid, ds)
            runs[mid] = {"status": str(run.status),
                         "reason": str(run.reason)[:160],
                         "unit": str((run.outputs[0].unit if run.outputs else ""))}
        rec["dataset_and_models"] = {
            "working_paper_written": bool(wp.get("ok")),
            "dataset_periods": list(ds.manifest.periods),
            "dataset_hash": ds.manifest.dataset_hash,
            "observations": ds.manifest.observations,
            "adopted_models": [str(_a(i, "model_id")) for i in plan.adopted],
            "rejected_models": [{"model_id": str(_a(r, "model_id")),
                                 "reason": str(_a(r, "reason"))[:120]}
                                for r in plan.rejected][:6],
            "runs": runs,
            "operating_drivers_validated": runs.get("operating_drivers", {}).get("status")
            == "validated",
            "cash_reconciliation_validated": runs.get("cash_reconciliation", {}).get("status")
            == "validated",
            "locators": len(locators),
        }
        # ③ 已存在的运行（金额=亿元）经共享格式器渲染：不得出现 0.00
        import report_brief as rb
        from financial_analysis import store as _fa_store
        pack = _fa_store.select_for_report(copy_task)
        picked = list(pack[0]) if isinstance(pack, tuple) else []
        scens = [r for r in picked if str(getattr(r, "model_id", "")) == "scenario_sensitivity"]
        od = next((r for r in picked if str(getattr(r, "model_id", "")) == "operating_drivers"),
                  None)
        cash = next((r for r in picked if str(getattr(r, "model_id", ""))
                     == "cash_reconciliation"), None)
        disp: dict = {"picked": [str(getattr(r, "model_id", "")) for r in picked]}
        if scens:
            run = scens[0]
            out = next((o for o in (run.outputs or ())
                        if o.metric == "scenario_net_profit"), None)
            from financial_analysis import charts as ch
            from financial_analysis import narrative as nt
            chart = ch.scenario_outcome_bars(run)
            comp = {str(c.get("component_id")): c.get("value")
                    for c in ((out.components or ()) if out is not None else ())}
            disp["scenario"] = {
                "run_unit": str(getattr(out, "unit", "")) if out is not None else "",
                "base_value": comp.get("base"), "user_value": comp.get("user"),
                "base_shown": nt._yi(comp.get("base", 0), str(getattr(out, "unit", "元")))
                if out is not None else "",
                "user_shown": nt._yi(comp.get("user", 0), str(getattr(out, "unit", "元")))
                if out is not None else "",
                "chart_conclusion": str(chart.get("conclusion") or "")[:220],
                "chart_available": bool(chart.get("available")),
            }
        if od is not None or cash is not None:
            ctx = rb._analysis_context(TID, ws_dir=copy_task)
            disp["note_excerpt"] = [
                line for line in str(ctx.get("note") or "").splitlines()
                if ("经营现金流变化" in line or "情景归母净利" in line
                    or "归母净利润变化" in line)][:4]
            disp["front_excerpt"] = [
                line for line in str(ctx.get("front") or "").splitlines()
                if "关键读数" in line][:3]
        rec["display"] = disp
        rec["checks"] = {
            "structured_branch_present": bool(rev24),
            "official_reading_used": str(rev24.get("unit")) == "元",
            "official_tables_merged": bool(rec["working_paper"]["has_operating_cost"]
                                          and rec["working_paper"]
                                          ["has_consolidated_net_profit"]),
            "cash_supplement_merged": bool(rec["working_paper"]["has_cash_supplement"]),
            "two_sources_recorded": ("一致" in " ".join(
                rec["working_paper"].get("official_notes") or [])),
            "operating_drivers_applicable": bool(rec["dataset_and_models"]
                                                 ["operating_drivers_validated"]),
            "cash_reconciliation_applicable": bool(rec["dataset_and_models"]
                                                    ["cash_reconciliation_validated"]),
            "scenario_shows_real_numbers": (
                not disp.get("scenario") or
                ("0.00" not in str(disp["scenario"].get("base_shown"))
                 and "0.00" not in str(disp["scenario"].get("chart_conclusion")))
            ),
        }
    except Exception as exc:                       # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        task_state.DB_PATH = old_db
        ws_mod.WORKSPACE_ROOT = old_root

    after = {str(p.relative_to(src_task)): (p.stat().st_size, _sha(p) if p.is_file() else "")
             for p in sorted(src_task.rglob("*")) if p.is_file()}
    rec["source_unchanged"] = before == after
    checks = rec.get("checks") or {}
    rec["passed"] = sum(1 for v in checks.values() if v)
    rec["total"] = len(checks)
    OUT.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    for k, v in checks.items():
        print(f"  {k}: {'OK' if v else 'FAIL'}")
    print(f"X0 真实入口核对 {rec['passed']}/{rec['total']}；原件未改动={rec['source_unchanged']}")
    if rec.get("error"):
        print("错误：", rec["error"])
    print(f"证据：{OUT.relative_to(ROOT)}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if rec["passed"] == rec["total"] and rec["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
