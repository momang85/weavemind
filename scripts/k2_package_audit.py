#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""K2 交付包审计与冻结：**同版判定 + 可复算输入 + 离线复算**（不调模型、不联网）。

用法：
    python scripts/k2_package_audit.py ui-603f626cbe ui-fa2cb73e59            # 只读审计
    python scripts/k2_package_audit.py <tid> --freeze                        # 按采纳稿冻结一份当前包
    python scripts/k2_package_audit.py <tid> --recompute                     # 包内 dataset 离线复算

只读分支不写任何文件；`--freeze` 走 `delivery_pipeline.repack_adopted`（确定性重包：
旧包不动、新包带时间戳+随机后缀，发布前复核采纳身份未变）。
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import delivery_pipeline as dp          # noqa: E402
import workspace                        # noqa: E402
from financial_analysis import store as fa_store   # noqa: E402


def _zip_manifest(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            if "PACKAGE_MANIFEST.json" in zf.namelist():
                return json.loads(zf.read("PACKAGE_MANIFEST.json").decode("utf-8")) or {}
    except Exception as exc:                        # noqa: BLE001
        return {"_error": str(exc)[:120]}
    return {}


def _package_report(path: Path) -> dict:
    man = _zip_manifest(path)
    ai = dict(man.get("analysis_inputs") or {})
    ar = dict(man.get("analysis_runs") or {})
    names: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = sorted(zf.namelist())
    except Exception:                               # noqa: BLE001
        pass
    return {
        "name": path.name,
        "bytes": path.stat().st_size if path.exists() else 0,
        "identity": str(man.get("report_version_id") or ""),
        "body_sha256": str(man.get("research_body_sha256") or man.get("body_sha256") or ""),
        "has_pdf": bool(man.get("pdf_sha256")),
        "has_paper": "working_paper.json" in names,
        "charts": len([n for n in names if n.startswith("charts/")]),
        "analysis_runs": int(ar.get("count") or 0),
        "analysis_validated": int(ar.get("validated") or 0),
        "analysis_inputs_present": list(ai.get("present") or []),
        "dataset_hash": str(ai.get("dataset_hash") or ""),
        "dataset_source": ai.get("dataset_source") or {},
        "contract": ai.get("contract") or {},
        "recomputable": bool(ai.get("recomputable")),
        "files_checked": len(man.get("files") or {}),
        "names": names,
    }


def materialize_inputs(tid: str) -> dict:
    """在**已存在任务**上补落可复算输入（确定性、零模型调用、不联网）。

    K2 之前的运行只落了 `analysis_runs.json`；这里用同一个分析器的金融分支（读任务工作区
    里已有的底稿/载荷）重算一次数据集与计划并落盘——这正是"用已存在任务和缓存材料先做
    离线接缝验证"的做法：run_id 由 dataset+参数决定，重跑是幂等的。
    """
    from workers.data_analyzer_worker import DataAnalyzerWorker

    ws = workspace.task_workspace(tid)
    w = DataAnalyzerWorker(agent_id="dataanalyzerworker", capabilities=["data_analyzer"],
                           registry=None, messaging=None)
    src = w._financial_source(ws)
    if src is None:
        return {"ok": False, "reason": "工作区没有 working_paper.json / financials.json"}
    try:
        out = w._run_financial(ws, "补落可复算输入（K2 审计）", {}, src)
    except Exception as exc:                        # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {"ok": True, "status": out.get("status"), "runs": len(out.get("runs") or []),
            "dataset_source": out.get("dataset_source") or {},
            "inputs": sorted(fa_store.input_payload_bytes(ws))}


def audit(tid: str, *, freeze: bool, recompute: bool, out_dir: Path | None,
          materialize: bool = False) -> dict:
    ws = workspace.task_workspace(tid)
    result: dict = {"task_id": tid, "workspace": str(ws)}
    if materialize:
        result["materialize"] = materialize_inputs(tid)
    before = dp.package_statuses(tid)
    result["before"] = {"adopted_identity": before.get("adopted_identity"),
                        "has_current": before.get("has_current"),
                        "current": before.get("current"),
                        "packages": [{k: p.get(k) for k in
                                      ("name", "status", "identity", "bytes", "reason")}
                                     for p in (before.get("packages") or [])]}
    frozen = ""
    if freeze:
        try:
            import task_state
            import web_ui
            report = str((task_state.read_task(tid) or {}).get("report") or "")
            # 真实导出口径：正文/PDF 都走生产渲染入口（K2 要求包内有同版 PDF）
            md_bytes, _mf = web_ui._task_markdown_export(tid)
            try:
                pdf_bytes = web_ui._task_pdf_bytes(tid)
            except Exception as exc:                # noqa: BLE001 - PDF 失败如实记
                pdf_bytes = b""
                result["pdf_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            snap = dp.export_snapshot(tid, delivered_text=report,
                                      md_bytes=md_bytes, pdf_bytes=pdf_bytes)
            rep = dp.repack_adopted(tid, snapshot=snap)
            frozen = str(rep.get("zip") or rep.get("path") or "")
        except Exception as exc:                    # noqa: BLE001
            result["freeze_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    after = dp.package_statuses(tid)
    result["after"] = {"adopted_identity": after.get("adopted_identity"),
                       "has_current": after.get("has_current"),
                       "current": after.get("current"),
                       "note": after.get("note"),
                       "packages": [{k: p.get(k) for k in
                                     ("name", "status", "identity", "bytes", "reason")}
                                    for p in (after.get("packages") or [])]}
    cur = dp.current_package(tid)
    result["frozen_zip"] = frozen
    result["current_zip"] = str(cur) if cur else ""
    if cur is not None:
        result["package"] = _package_report(cur)
        if recompute:
            import shutil
            import tempfile
            with tempfile.TemporaryDirectory(prefix="k2_unzip_") as tmp:
                t = Path(tmp)
                with zipfile.ZipFile(cur) as zf:
                    zf.extractall(tmp)
                # 包内布局：`analysis/` 下是运行记录 + 可复算输入；工作区布局是
                # 运行记录在根、输入在 `analysis/` —— 复算前把两者对齐（只读重建）
                _runs = t / "analysis" / "analysis_runs.json"
                if _runs.is_file():
                    shutil.copy2(_runs, t / "analysis_runs.json")
                runs = fa_store.load_runs(t)
                rec: list[dict] = []
                for r in runs:
                    rec.append(fa_store.recompute_run(t, str(r.get("run_id") or "")))
                result["recompute"] = rec
                result["recompute_summary"] = {
                    "runs": len(rec),
                    "ok": sum(1 for x in rec if x.get("ok")),
                    "failed": [x for x in rec if not x.get("ok")],
                }
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"k2_audit_{tid}.json"
        p.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        result["written"] = str(p)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("task_ids", nargs="+")
    ap.add_argument("--freeze", action="store_true")
    ap.add_argument("--recompute", action="store_true")
    ap.add_argument("--materialize-inputs", action="store_true",
                    help="先按已有底稿补落 analysis/{dataset,plan,context}.json（零模型）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    out_dir = Path(args.out) if args.out else None
    all_out = []
    for tid in args.task_ids:
        rep = audit(tid, freeze=args.freeze, recompute=args.recompute, out_dir=out_dir,
                    materialize=args.materialize_inputs)
        all_out.append(rep)
        print(json.dumps({k: rep.get(k) for k in
                          ("task_id", "frozen_zip", "current_zip", "freeze_error",
                           "before", "after", "package", "recompute")},
                         ensure_ascii=False, indent=1))
    if out_dir is not None:
        (out_dir / "k2_audit_all.json").write_text(
            json.dumps(all_out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
