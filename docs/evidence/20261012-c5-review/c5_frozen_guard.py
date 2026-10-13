# -*- coding: utf-8 -*-
"""C5 冻结守护：把「旧基线只读、未被本轮改写」写成可复核的哈希清单。

范围：
  * C4 两个已验收研究的冻结文件（financial/inputs/manifest/events）——C5 只**读**它们
  * C4 冻结实验产物 `experiments/245bcd….json/.md/.pdf`（含“本轮未被改写”的字节哈希）
  * C4 期交付包（C5 之前出的包）成员清单与包内附件哈希——旧包不动
  * C5 期四个包（含许可门移出与完整 JSON 两种形态）
  * 受保护的既有脏文件/未跟踪文件哈希（供后续复核“没被顺手改/删”）

用法：`python -X utf8 docs/evidence/20261012-c5-review/c5_frozen_guard.py`
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

STUDIES = ROOT / "tmp" / "research-studies"
YH = STUDIES / "0af20919e6245b88"
SY = STUDIES / "4dba42a6bdde9227"
WS = (Path(os.environ["TEMP"]) / "agent_workspace" / "tasks" / "projects" / "default"
      / "study-report-0af20919e6245b88-002304.SZ")
OLD_EXP = "245bcd5e56ef7c5eafaf11e2"
NEW_EXP = "80693f9870e8337f394fcf49"
C3_ARC = "analysis/c3_experiment_002304.SZ.json"
NOTE_ARC = "quant/EXTERNAL_DELIVERY_RESTRICTION.md"

# C4 冻结实验的**期望**字节哈希（上一轮已交审、本轮不得改写）
EXPECTED = {f"{OLD_EXP}.json": "8176dca48ade57ed0a7da86d976597a09007a5d7c3e8767815848471f723f245"}
# C4 期（本轮之前）就已存在的包，按名字前缀 + 时间戳识别
C4_ZIPS = ["20261011_171024_232b45", "20261011_171523_8c5000", "20261011_183122_3ca2dd",
           "20261011_183242_ddca43", "20261011_183316_27100b", "20261011_183807_dbcbdf",
           "20261011_183919_97000d", "20261011_184001_ff570f", "20261011_184205_25ef2e",
           "20261011_184706_13b232"]
C5_ZIPS = ["20261012_114516_71c308", "20261012_115427_9434fb",
           "20261012_115504_387b7f", "20261012_115505_6b1386"]

# 受保护的既有文件（脏/未跟踪）：本轮**只允许加法改** 7 个实现文件，其余按原样保留
KEEP = ["README.md", "README.en.md", "acceptance_checker.py", "financial_analysis/dataset.py",
        "docs/M0完成复核与下一步修订_20260916.md", "docs/总架构师执行指令.md",
        "docs/架构监督记录.md", "evals/cases/auto_grown.json", "templates.json",
        "trial_tasks.json", "financial_analysis/operators/operating_drivers.py",
        "test_research_study_entry.py", "quant_research/study.py", "adapters/wind_import.py"]
# 本轮允许改的实现面（加法；记录哈希以便复核改动范围）
TOUCHED_OK = ["web_ui.py", "delivery_pipeline.py", "financial_analysis/research_experiment.py",
              "financial_analysis/continuation.py", "research_report_adapter.py",
              "frontend/src/pages/ResearchStudy.tsx"]


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def file_row(p: Path) -> dict:
    """记录**相对路径**（发布到公开仓库不暴露本机用户名/目录布局）：
    仓库内相对 `ROOT`；仓库外（如 `%TEMP%` 下的候选工作区）记 `%TEMP%` 相对形式。"""
    tmp = Path(os.environ.get("TEMP", ""))
    try:
        rel = str(p.relative_to(ROOT))
    except ValueError:
        rel = ("%TEMP%\\" + str(p.relative_to(tmp))) if tmp and p.is_relative_to(tmp) else str(p)
    return {"path": rel, "exists": p.is_file(),
            "sha256": sha(p) if p.is_file() else "",
            "bytes": p.stat().st_size if p.is_file() else 0,
            "mtime": int(p.stat().st_mtime) if p.is_file() else 0}


def zip_row(name: str) -> dict:
    p = WS / f"deliverables_{name}.zip"
    if not p.is_file():
        return {"zip": p.name, "exists": False}
    with zipfile.ZipFile(p) as z:
        names = sorted(z.namelist())
        mem = {n: z.read(n) for n in names}
    man = {}
    try:
        man = json.loads(mem.get("PACKAGE_MANIFEST.json", b"{}").decode("utf-8"))
    except Exception:                             # noqa: BLE001
        man = {}
    ext = man.get("external_delivery") or {}
    return {"zip": p.name, "exists": True, "sha256": sha(p), "bytes": p.stat().st_size,
            "members": names,
            "has_experiment_json": C3_ARC in mem,
            "c3_md_sha256": hashlib.sha256(mem.get(C3_ARC.replace(".json", ".md"), b"")).hexdigest(),
            "note_present": NOTE_ARC in mem,
            "manifest_withheld": sorted((ext.get("withheld") or {}).keys()),
            "manifest_allowed": ext.get("allowed"),
            "research_body_sha256": str(man.get("research_body_sha256") or ""),
            "delivered_md_sha256": str(man.get("delivered_md_sha256") or "")}


out: dict = {"scope": "C5 冻结守护：旧基线与原 8 项未被改写/删除（只读核对）",
             "c4_studies": {}, "c4_frozen_experiment": [], "c5_experiment": [],
             "zips_c4_era": [], "zips_c5_era": [], "kept_files": [], "touched_ok_files": [],
             "verdict": {}}
for tag, d in (("yanghe", YH), ("sanyi", SY)):
    out["c4_studies"][tag] = {name: file_row(d / name)
                              for name in ("financial.json", "inputs.json",
                                           "study_manifest.json", "events.json")
                              if (d / name).exists()}
for name in (f"{OLD_EXP}.json", f"{OLD_EXP}.md", f"{OLD_EXP}.pdf"):
    row = file_row(YH / "experiments" / name)
    row["expected_sha256"] = EXPECTED.get(name, "")
    row["matches_expected"] = (not EXPECTED.get(name)) or row["sha256"] == EXPECTED[name]
    out["c4_frozen_experiment"].append(row)
for name in (f"{NEW_EXP}.json", f"{NEW_EXP}.md"):
    out["c5_experiment"].append(file_row(YH / "experiments" / name))
out["zips_c4_era"] = [zip_row(n) for n in C4_ZIPS]
out["zips_c5_era"] = [zip_row(n) for n in C5_ZIPS]
out["kept_files"] = [file_row(ROOT / r) for r in KEEP]
out["touched_ok_files"] = [file_row(ROOT / r) for r in TOUCHED_OK]

out["verdict"] = {
    "c4_frozen_json_unchanged":
        all(r["matches_expected"] for r in out["c4_frozen_experiment"] if r["expected_sha256"]),
    "c4_era_zips_all_lack_experiment_json":
        all((not r.get("exists")) or (not r["has_experiment_json"]) for r in out["zips_c4_era"]),
    "c4_era_zips_untouched_note_absent":
        all((not r.get("exists")) or (not r["note_present"]) for r in out["zips_c4_era"]),
    "c5_zip_387b7f_has_full_json":
        next((r.get("has_experiment_json") for r in out["zips_c5_era"]
              if r.get("zip", "").endswith("387b7f.zip")), None),
    "c5_zip_6b1386_withheld_json":
        next((not r.get("has_experiment_json") and r.get("note_present")
              for r in out["zips_c5_era"] if r.get("zip", "").endswith("6b1386.zip")), None),
    "kept_files_all_present": all(r["exists"] for r in out["kept_files"]),
}
(OUT / "c5_frozen_guard.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps(out["verdict"], ensure_ascii=False, indent=1))
print("kept files:", sum(1 for r in out["kept_files"] if r["exists"]), "/", len(out["kept_files"]))
