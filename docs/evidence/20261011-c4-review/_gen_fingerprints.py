# -*- coding: utf-8 -*-
"""生成 C4 复核指纹（一次性）：把本会话涉及的实现文件、研究目录关键文件与
三个已验收 C3 交付包的 sha256 记录成 review_fingerprints.json（证据包内只读产物）。"""
import hashlib
import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from workspace import task_workspace  # noqa: E402

OUT = Path(__file__).resolve().parent / "review_fingerprints.json"
TASKS = task_workspace("", project="default").parent  # …/tasks/projects/default（不写死用户路径）
WS = {"sany": TASKS / "study-report-4dba42a6bdde9227-600031.SH",
      "yanghe": TASKS / "study-report-0af20919e6245b88-002304.SZ",
      "c2c": TASKS / "study-report-d93e5c4a07e0fcbd-002304.SZ"}
PACKS = {
    "c3_sany": WS["sany"] / "deliverables_20261011_183959_f38391.zip",
    "c3_yanghe": WS["yanghe"] / "deliverables_20261011_184706_13b232.zip",
    "c2e_sany": WS["sany"] / "deliverables_20261011_171519_344cb4.zip",
    "c2c_yanghe": WS["c2c"] / "deliverables_20261011_142232_080384.zip",
}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


impl = {name: {"sha256": sha(ROOT / name), "diff_numstat": git("diff", "--numstat", "--", name)}
        for name in ("web_ui.py", "delivery_pipeline.py", "financial_analysis/continuation.py",
                     "financial_analysis/operators/cash_reconciliation.py",
                     "frontend/src/pages/ResearchStudy.tsx", "docs/DeepSeek执行状态.md")}
untracked = {name: {"sha256": sha(ROOT / name), "lines": len((ROOT / name).read_text(encoding="utf-8").splitlines())}
             for name in ("financial_analysis/research_experiment.py",
                          "research_report_adapter.py", "scripts/c3_experiment.py")}
studies = ROOT / "tmp" / "research-studies"
frozen = {
    "yanghe_c4_experiment": {"files": {
        f"0af20919e6245b88/experiments/245bcd5e56ef7c5eafaf11e2{ext}":
            sha(studies / f"0af20919e6245b88/experiments/245bcd5e56ef7c5eafaf11e2{ext}")
        for ext in (".json", ".md", ".pdf")}},
    "c3_baselines": {"files": {
        "0af20919e6245b88/c3_experiment_002304.SZ.json": sha(studies / "0af20919e6245b88/c3_experiment_002304.SZ.json"),
        "4dba42a6bdde9227/c3_experiment_600031.SH.json": sha(studies / "4dba42a6bdde9227/c3_experiment_600031.SH.json")}},
    "c3_accepted_packages": {"files": {f"{k}: {v.name}": {"sha256": sha(v), "bytes": v.stat().st_size}
                                       for k, v in PACKS.items()}},
}
dist = {f"frontend/dist/{p.relative_to(ROOT / 'frontend/dist')}": sha(p)
        for p in sorted((ROOT / "frontend/dist").rglob("*")) if p.is_file()
        and (p.name.startswith("index-") or p.name.startswith("ResearchStudy-"))}
evidence = {p.name: sha(p) for p in sorted(Path(__file__).resolve().parent.iterdir())
            if p.suffix in (".py", ".json") and not p.name.startswith(("review_fingerprints", "_"))
            and p.name != "results.json"} | {
    "results.json": sha(Path(__file__).resolve().parent / "results.json")}
head = git("log", "-1", "--format=%H %s")
protected = ["README.md", "README.en.md", "acceptance_checker.py", "financial_analysis/dataset.py",
             "docs/M0完成复核与下一步修订_20260916.md", "docs/总架构师执行指令.md",
             "docs/架构监督记录.md", "evals/cases/auto_grown.json", "templates.json",
             "trial_tasks.json", "financial_analysis/operators/operating_drivers.py",
             "test_research_study_entry.py", "quant_research/study.py", "adapters/wind_import.py"]
doc = {
    "captured_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    "head": head,
    "instruction": {"path": "docs/evidence/20261011-c3-accept/dsh-c4-instruction.txt",
                    "sha256": sha(ROOT / "docs/evidence/20261011-c3-accept/dsh-c4-instruction.txt")},
    "previous_evidence": "docs/evidence/20261011-b4-c3-review/",
    "implementation": {"tracked_modified": impl, "untracked_new": untracked,
                       "frontend_dist": dist,
                       "note": "diff_numstat 是相对 HEAD c85371e 的**累计**改动（含 C3 会话遗留："
                               "cash_reconciliation.py/continuation.py），C4 本会话新增为 "
                               "web_ui.py 的窄 POST＋路由、delivery_pipeline.py 的 glob 收录、"
                               "ResearchStudy.tsx 的实验区，以及三个新文件。"},
    "study_files": frozen,
    "review_evidence": evidence,
    "protected_untouched_this_session": {n: {"sha256": sha(ROOT / n)} for n in protected
                                         if (ROOT / n).is_file()},
    "new_delivery_packages": "无（C4 不新增报告交付/离线样品/模型；C3 两包原样保留）",
}
OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
print(f"written {OUT} ({OUT.stat().st_size}B)")
for k in ("head", "instruction", "review_evidence"):
    print(" ", k, "->", str(doc[k])[:150])
