# -*- coding: utf-8 -*-
"""案例交付件生成器（脚本层共享）：同一主线在任意公司上产出**正文 + 三图底稿**。

为什么抽出来：U1/U2 的洋河交付件（`scripts/u2_yanghe_charts.py` / `u2_yanghe_note.py`）与
U3 复制到三一的那一套，逻辑必须是**同一条**——事实→冻结→运行注册模型→（已验证运行）出图与
成篇正文。差别只在材料路径与输出目录，写成参数，不再各写一份。

不写任何财务数字：数字全部来自运行输出；本模块只负责「怎么装配」。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

VARIANTS = (("收入持平", {"revenue_growth": 0.0, "gross_margin_delta": 0.0}),
            ("收入 +5%", {}),
            ("收入 −12.83%", {"revenue_growth": -0.1283, "gross_margin_delta": 0.0}))
CHART_NAMES = ("1_profit_waterfall.png", "2_cash_bridge.png",
               "3_scenario_thresholds.png")


def sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _log(*parts) -> None:
    print(" ".join(str(p) for p in parts))


def build_case(*, root: str, name: str, entity_id: str, doc_path: str,
               out_dir: str, case_title: str) -> dict:
    """跑一家公司：返回 `{specs, note, figures, runs, dataset, stats}`（同时落盘证据）。"""
    import chart_assembly as CA
    import chart_specs as CS
    import financial_analysis as fa
    import facts as F

    doc = json.load(open(doc_path, encoding="utf-8"))
    facts = F.facts_from_annual_tables(doc, company=name, company_code=entity_id,
                                       periods=(2023, 2024), as_of="2025-04-30",
                                       disclosed_at="2025-04-28")
    ds = fa.freeze_from_facts(facts, periods=(2023, 2024), entity=name,
                              entity_id=entity_id, as_of="2025-04-30",
                              source_label=f"evals:{entity_id}")
    _log(f"[{name}] 事实 {len(facts)} / 冻结 观察 {ds.manifest.observations} "
         f"可用 {ds.manifest.usable} 冲突 {len(ds.manifest.conflicts or ())} "
         f"hash={ds.manifest.dataset_hash[:12]}")

    od_run = fa.run("operating_drivers", ds)
    cash_run = fa.run("cash_reconciliation", ds)
    scen = [(label, fa.run("scenario_sensitivity", ds, params=params))
            for label, params in VARIANTS]
    _log(f"[{name}] 运行 operating_drivers={od_run.status} "
         f"cash_reconciliation={cash_run.status} "
         f"scenario={[str(r.status) for _l, r in scen]}")

    specs = [fa.charts.profit_waterfall(od_run, ds),
             fa.charts.cash_bridge_waterfall(cash_run, which="cur"),
             fa.charts.scenario_threshold_comparison(scen)]
    problems = []
    for i, spec in enumerate(specs, 1):
        if not spec.get("available"):
            problems.append(f"图{i} 不可用：{spec.get('reason')}")
            continue
        issues = CS.validate_spec(spec)
        if issues:
            problems.append(f"图{i} 规格不合法：{issues}")
    if problems:
        raise RuntimeError("；".join(problems))

    figures = _render(specs, out_dir, root, CA)
    prov = fa.narrative.provenance_from_facts(facts, label_of=F.metric_label)
    note = fa.narrative.research_note([od_run, cash_run] + [r for _l, r in scen],
                                      provenance=prov, charts=specs,
                                      label_of=F.metric_label)
    return {"case": case_title, "entity_id": entity_id,
            "doc": os.path.relpath(doc_path, root).replace("\\", "/"),
            "dataset": {"observations": ds.manifest.observations,
                        "usable": ds.manifest.usable,
                        "conflicts": list(ds.manifest.conflicts or ()),
                        "dataset_hash": ds.manifest.dataset_hash},
            "runs": {"operating_drivers": {"run_id": od_run.run_id,
                                           "status": str(od_run.status)},
                     "cash_reconciliation": {"run_id": cash_run.run_id,
                                             "status": str(cash_run.status)},
                     "scenario_sensitivity": [
                         {"label": label, "run_id": r.run_id, "status": str(r.status),
                          "params": dict(r.params or {})} for label, r in scen]},
            "specs": specs, "figures": figures, "note": note}


def _render(specs, out_dir, root, CA) -> list:
    import shutil
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    for stale in Path(out_dir).glob("*.png"):
        stale.unlink()
    (Path(out_dir) / "chart_data.json").write_text(
        json.dumps({"charts": specs}, ensure_ascii=False, indent=1), encoding="utf-8")
    renderer = Path(out_dir) / "render_charts.py"
    renderer.write_text(CA.RENDER_CHART_SCRIPT.replace("__REPO_ROOT__", root),
                        encoding="utf-8")
    try:
        proc = subprocess.run([sys.executable, renderer.name], cwd=out_dir,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=900)
    finally:
        try:
            os.remove(str(renderer))
        except OSError:
            pass
    if proc.returncode != 0:
        raise RuntimeError("渲染失败：\n" + proc.stderr[-800:])
    _log("  渲染：" + proc.stdout.strip().replace("\n", " | "))

    figures: list = []
    renamed: dict = {}
    for i, tail in enumerate(CHART_NAMES, 1):
        src = Path(out_dir) / f"chart_{i}.png"
        if not src.exists():
            raise RuntimeError(f"缺少 {src}")
        dst = Path(out_dir) / tail
        if dst.exists():
            dst.unlink()
        src.rename(dst)
        renamed[f"chart_{i}.png"] = tail
        figures.append({"file": os.path.relpath(str(dst), root).replace("\\", "/"),
                        "bytes": dst.stat().st_size,
                        "sha256": sha256(str(dst))[:16], "spec": specs[i - 1]})
    man_path = Path(out_dir) / "chart_manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    for c in manifest.get("charts") or ():
        if str(c.get("file")) in renamed:
            c["file"] = renamed[str(c["file"])]
    man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    return figures


def note_stats(note: str) -> dict:
    return {"chars": len(note), "lines": len(note.splitlines()),
            "sections": re.findall(r"^### (.+)$", note, re.M),
            "table_rows": len([l for l in note.splitlines() if l.startswith("| ")]),
            "facts_with_locator": len(sorted(set(re.findall(r"`(fact-[^`]+)`", note)))),
            "run_refs": len(set(re.findall(r"run ([0-9a-f]{6,})", note)))}
