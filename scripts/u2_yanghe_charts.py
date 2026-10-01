# -*- coding: utf-8 -*-
"""U2 三图底稿：洋河 2023/2024 利润瀑布／现金桥／反向情景比较（官方入口，不写任何数字）。

走**正常入口**：年报 doc → 事实 → 冻结数据集 → 运行注册模型 → 已验证据此出图规格
（`financial_analysis.charts`）→ 交给既有确定性渲染脚本（`chart_assembly.RENDER_CHART_SCRIPT`，
含 `waterfall` 一类）出 PNG。产出：

- `docs/evidence/u2_charts/u2_1_profit_waterfall.png` 等三张图（+ `chart_data.json` 规格、
  `chart_manifest.json` 图注）；
- `docs/evidence/u2_charts.json`：规格、图注、PNG 尺寸/哈希、渲染读数。

脚本不写死任何财务数字：全部来自运行输出。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "docs", "evidence", "u2_charts")
REPORT = os.path.join(ROOT, "docs", "evidence", "u2_charts.json")
DOC_PATH = os.path.join(ROOT, "evals", "a2_official_chain_20260929", "002304",
                        "project", "materials", "f42e747c73850b59", "doc.json")

# 三张图的稳定文件名（渲染脚本按顺序写 chart_N.png，这里再改名，图名带语义）
NAMES = ("u2_1_profit_waterfall.png", "u2_2_cash_bridge.png",
         "u2_3_scenario_thresholds.png")

VARIANTS = (("收入持平", {"revenue_growth": 0.0, "gross_margin_delta": 0.0}),
            ("收入 +5%", {}),
            ("收入 −12.83%", {"revenue_growth": -0.1283, "gross_margin_delta": 0.0}))


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    import chart_assembly as CA
    import chart_specs as CS
    import financial_analysis as fa
    import facts as F

    doc = json.load(open(DOC_PATH, encoding="utf-8"))
    fs = F.facts_from_annual_tables(doc, company="洋河股份", company_code="002304",
                                    periods=(2023, 2024), as_of="2025-04-30",
                                    disclosed_at="2025-04-28")
    ds = fa.freeze_from_facts(fs, periods=(2023, 2024), entity="洋河股份",
                              entity_id="002304.SZ", as_of="2025-04-30",
                              source_label="evals:a2_official_chain_20260929/002304")
    print(f"冻结：观察 {ds.manifest.observations} 可用 {ds.manifest.usable} "
          f"冲突 {len(ds.manifest.conflicts or ())} hash={ds.manifest.dataset_hash}")

    od_run = fa.run("operating_drivers", ds)
    cash_run = fa.run("cash_reconciliation", ds)
    print(f"运行：operating_drivers={od_run.status} cash_reconciliation={cash_run.status} "
          f"failed={list(od_run.validation.get('failed') or [])}"
          f"/{list(cash_run.validation.get('failed') or [])}")

    scen_runs = []
    for label, params in VARIANTS:
        srun = fa.run("scenario_sensitivity", ds, params=params)
        print(f"情景 {label}: {srun.status} "
              f"failed={list(srun.validation.get('failed') or [])}")
        scen_runs.append((label, srun))

    specs = [
        fa.charts.profit_waterfall(od_run, ds),
        fa.charts.cash_bridge_waterfall(cash_run, which="cur"),
        fa.charts.scenario_threshold_comparison(scen_runs),
    ]
    problems: list[str] = []
    for i, spec in enumerate(specs, 1):
        if not spec.get("available"):
            problems.append(f"图{i} 不可用：{spec.get('reason')}")
            continue
        issues = CS.validate_spec(spec)
        if issues:
            problems.append(f"图{i} 规格不合法：{issues}")
    if problems:
        print("出图失败：" + "；".join(problems))
        return 1

    Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
    for stale in Path(OUT_DIR).glob("*.png"):
        stale.unlink()
    # 渲染端会合并已有 chart_manifest.json；重跑同一组文件名会累积重复条目（见
    # `scripts/case_deliverables.py` 的同一条注释），证据目录只要"本次三张"。
    try:
        (Path(OUT_DIR) / "chart_manifest.json").unlink()
    except OSError:
        pass
    (Path(OUT_DIR) / "chart_data.json").write_text(
        json.dumps({"charts": specs}, ensure_ascii=False, indent=1), encoding="utf-8")
    (Path(OUT_DIR) / "render_charts.py").write_text(
        CA.RENDER_CHART_SCRIPT.replace("__REPO_ROOT__", ROOT), encoding="utf-8")
    try:
        proc = subprocess.run([sys.executable, "render_charts.py"], cwd=OUT_DIR,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=600)
    finally:
        try:
            os.remove(os.path.join(OUT_DIR, "render_charts.py"))
        except OSError:
            pass
    print("渲染输出：")
    print(proc.stdout.strip())
    if proc.returncode != 0:
        print("渲染失败：\n" + proc.stderr[-800:])
        return 1

    rendered = []
    renamed: dict[str, str] = {}
    for i, name in enumerate(NAMES, 1):
        src = Path(OUT_DIR) / f"chart_{i}.png"
        if not src.exists():
            print(f"缺少 {src}")
            return 1
        dst = Path(OUT_DIR) / name
        if dst.exists():
            dst.unlink()
        src.rename(dst)
        renamed[f"chart_{i}.png"] = name
        rel = os.path.relpath(str(dst), ROOT).replace("\\", "/")
        rendered.append({"file": rel, "bytes": dst.stat().st_size,
                         "sha256": _sha256(str(dst))[:16],
                         "spec": specs[i - 1]})
        print(f"  {rel} {dst.stat().st_size} B")

    # 图注清单里的文件名跟着改名（否则清单指向已不存在的 chart_N.png）
    man_path = Path(OUT_DIR) / "chart_manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    for c in manifest.get("charts") or ():
        if str(c.get("file")) in renamed:
            c["file"] = renamed[str(c["file"])]
    man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    report = {
        "case": "洋河股份 002304 2023→2024 三图底稿（利润瀑布／现金桥／反向情景）",
        "source_material": os.path.relpath(DOC_PATH, ROOT).replace("\\", "/"),
        "dataset": {"observations": ds.manifest.observations,
                    "usable": ds.manifest.usable,
                    "conflicts": list(ds.manifest.conflicts or ()),
                    "dataset_hash": ds.manifest.dataset_hash},
        "runs": {
            "operating_drivers": {"run_id": od_run.run_id, "status": str(od_run.status)},
            "cash_reconciliation": {"run_id": cash_run.run_id, "status": str(cash_run.status)},
            "scenario_sensitivity": [{"label": lab, "run_id": r.run_id,
                                      "status": str(r.status)} for lab, r in scen_runs],
        },
        "render": {"script": "chart_assembly.RENDER_CHART_SCRIPT",
                   "chart_types": ["waterfall", "waterfall", "grouped_bar"],
                   "stdout": proc.stdout.strip().splitlines()[-6:],
                   "manifest_captions": [
                       {"file": c.get("file"), "observation": c.get("observation"),
                        "grade": c.get("grade")} for c in manifest.get("charts") or ()]},
        "figures": rendered,
    }
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"写入 {os.path.relpath(REPORT, ROOT)}（{len(rendered)} 图）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
