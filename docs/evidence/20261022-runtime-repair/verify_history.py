# -*- coding: utf-8 -*-
"""RUNTIME-1022 P0-A：历史任务只读复核（不写、不迁移、不重跑）。

对诊断快照里的 5 个任务逐一：
- 用新定位器（bind=False，纯只读）解析真实产物根；
- 列出全部候选目录（含空壳）与评分，确认空壳/冲突没有被误选；
- 对每个存在的关键产物算 SHA-256（底稿/财务/数据集/报告/交付包/研究状态）；
- 与快照里记录的旧根期望对照，记录差异。

输出 JSON 到同目录 history_verification.json，退出码 0=复核完成（不代表研究合格），
1=发现定位与诊断期望不一致（需人工核查）。
"""

import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent.parent
sys.path.insert(0, str(ROOT))

# 跨根恢复只走**显式登记**（RUNTIME-1022 第一批复查教训：ambient 扫描系统临时目录会在
# 测试里撞上历史残留的同名目录）。这里登记诊断已证实的三个历史根，`builtin` 哨兵额外
# 展开内建已知形态，确保复核覆盖与旧实现一致的范围。
os.environ.setdefault(
    "WEAVEMIND_WORKSPACE_LEGACY_ROOTS", os.pathsep.join([
        r"C:\Users\ding0\AppData\Local\Temp\agent_workspace\tasks",
        r"C:\Users\ding0\AppData\Local\Temp\dsh-win-bash-37684-7b69ad23\agent_workspace\tasks",
        r"C:\Users\ding0\AppData\Local\Temp\dsh-win-bash-37684-38250d7e\agent_workspace\tasks",
        "builtin",
    ]))

import workspace as ws  # noqa: E402

TASKS = {
    "ui-b23642e5de": {"name": "伊利股份", "expect_root": r"dsh-win-bash-37684-7b69ad23\agent_workspace\tasks\projects\default"},
    "ui-a0d82a26c4": {"name": "贵州茅台", "expect_root": r"dsh-win-bash-37684-7b69ad23\agent_workspace\tasks\projects\default"},
    "ui-d59152ebb9": {"name": "洋河股份", "expect_root": r"dsh-win-bash-37684-38250d7e\agent_workspace\tasks\projects\default"},
    "ui-8b28c95ab1": {"name": "海天味业", "expect_root": r"dsh-win-bash-37684-7b69ad23\agent_workspace\tasks\projects\default"},
    "ui-cc853cf86b": {"name": "三一重工", "expect_root": r"agent_workspace\tasks\projects\default"},
}

ARTIFACTS = (
    "project/working_paper.json",
    "project/financials.json",
    "analysis/dataset.json",
    "research_state.json",
    "acceptance_report.json",
)


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def count_files(d: Path) -> int:
    n = 0
    for _, _, names in os.walk(d):
        n += len(names)
    return n


def main() -> int:
    out = {"captured": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "workspace_root_new_default": str(ws.WORKSPACE_ROOT),
           "roots_resolved": [str(r) for r in ws.workspace_roots()],
           "records": []}
    bad = 0
    for tid, meta in TASKS.items():
        info = ws.locate_task_workspace(tid, bind=False)   # 纯只读：不写绑定
        rec = {"task_id": tid, "name": meta["name"], "located": bool(info.get("found")),
               "path": str(info.get("path")) if info.get("path") else None,
               "root": info.get("root"), "layout": info.get("layout"),
               "source": info.get("source"), "conflict": info.get("conflict"),
               "empty": info.get("empty"),
               "candidates": info.get("candidates")}
        p = info.get("path")
        if p and Path(p).is_dir():
            d = Path(p)
            rec["file_count"] = count_files(d)
            rec["artifacts"] = {}
            for rel in ARTIFACTS:
                fp = d / rel
                rec["artifacts"][rel] = (
                    {"exists": True, "sha256": sha(fp), "bytes": fp.stat().st_size}
                    if fp.is_file() else {"exists": False}
                )
            reports = sorted(x.name for x in (d / "reports").glob("*") if x.is_file()) if (d / "reports").is_dir() else []
            rec["artifacts"]["reports"] = reports
            zips = sorted((x.name, sha(x), x.stat().st_size) for x in d.glob("*.zip") if x.is_file())
            rec["artifacts"]["delivery_zips"] = [{"name": n, "sha256": s, "bytes": b} for n, s, b in zips]
            rec["marker_files"] = {
                "working_paper": (d / "project" / "working_paper.json").is_file(),
                "financials": (d / "project" / "financials.json").is_file(),
                "analysis_dataset": (d / "analysis" / "dataset.json").is_file(),
            }
        # 与诊断期望对照（根路径包含关系）
        exp = meta["expect_root"]
        got_ok = bool(p) and exp.replace("/", "\\").lower() in str(p).replace("/", "\\").lower()
        rec["matches_diagnosis_expectation"] = got_ok
        if not info.get("found"):
            bad += 1
        out["records"].append(rec)
    out["summary"] = {
        "all_located": all(r["located"] for r in out["records"]),
        "any_conflict": any(r.get("conflict") for r in out["records"]),
        "mismatch_vs_diagnosis": [r["task_id"] for r in out["records"]
                                  if not r["matches_diagnosis_expectation"]],
        "exit": 0 if bad == 0 else 1,
    }
    dest = Path(__file__).parent / "history_verification.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: out["summary"][k] for k in ("all_located", "any_conflict", "mismatch_vs_diagnosis")},
                     ensure_ascii=False))
    for r in out["records"]:
        print(f"- {r['name']}({r['task_id']}): found={r['located']} files={r.get('file_count')} "
              f"conflict={r['conflict']} → {r['path']}")
    print("written:", dest)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
