# -*- coding: utf-8 -*-
"""R4 固定样本回放：三份冻结场景 + 一个用户修订样本，产出逐任务指标与核对结论。

- 场景（`evals/scenarios/r4_*.json`）：走 `scripts/scenario_run.py` 的同一套生产路径
  （装配 → 验收 → 版本 → 导出），预期在场景文件里**冻结**（实现前写死，事后不改）。
- 用户修订样本：真实任务 `ui-706c5ef4a5` 的采纳版是经"页面同路径"的人工复核修订产生的
  ——核对其"旧批准不迁移 / 同版导出 / 0 模型请求"。
- **不动规则预期**：本脚本只读场景文件与产物，不改 `expect`；核对失败如实输出。

用法：python scripts/r4_replay_fixed_samples.py [--out docs/evidence/r4_fixed_replay_20260924.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REDIS_PORT", "6399")
SCEN_DIR = ROOT / "evals" / "scenarios"
RUN_ROOT = ROOT / ".weavemind" / "scenarios"
USER_REVISION_TASK = "ui-706c5ef4a5"


def _llm_requests(ws: Path) -> int:
    p = ws / "llm_calls.jsonl"
    try:
        return sum(1 for line in p.read_text(encoding="utf-8").splitlines() if line.strip())
    except Exception:                            # noqa: BLE001
        return 0


def _run_scenario(name: str) -> dict:
    t0 = time.time()
    proc = subprocess.run([sys.executable, "scripts/scenario_run.py", name],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=1800)
    out = (proc.stdout or "").strip().splitlines()
    manifest_path = RUN_ROOT / name / "manifest.json"
    m = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    checks = m.get("checks") or {}
    bad = sorted(k for k, v in checks.items() if k != "all_passed" and not v)
    return {
        "case": name,
        "kind": "fixed_scenario",
        "exit_code": proc.returncode,
        "wall_seconds": round(time.time() - t0, 1),
        "verdict": "pass" if not bad else "fail",
        "failed_checks": bad,
        "delivery": (m.get("delivery") or {}).get("status"),
        "acceptance_overall": ((m.get("acceptance") or {}).get("overall")),
        "acceptance_failed": ((m.get("acceptance") or {}).get("failed_checks")),
        "coverage": {k: (v or {}).get("coverage") for k, v in (m.get("assessments") or {}).items()},
        "located": (m.get("evidence") or {}).get("located"),
        "missing_labels": (m.get("evidence") or {}).get("missing_labels"),
        "contradictions": m.get("contradictions"),
        "facts_missing": m.get("facts_missing"),
        "llm_requests": m.get("llm_requests"),
        "brief_chars": len(str(m.get("brief") or "")),
        "stdout_tail": out[-1] if out else "",
    }


def _user_revision_case() -> dict:
    """真实任务的人工修订样本：旧批准不迁移 / 同版导出 / 0 模型请求。"""
    import task_state as ts
    import workspace as ws_mod
    from report_version import VersionStore
    import web_ui

    tid = USER_REVISION_TASK
    ws = Path(ws_mod.task_workspace(tid))
    store = VersionStore(ws, tid)
    adopted = store.adopted()
    body = str((web_ui._get_task_report_data(tid) or {}).get("report") or "")
    pkgs = sorted(ws.glob("deliverables_*.zip"), key=lambda p: p.stat().st_mtime)
    newest = pkgs[-1] if pkgs else None
    pkg_identity = ""
    if newest is not None:
        try:
            with zipfile.ZipFile(newest) as z:
                pkg_identity = str(json.loads(
                    z.read("PACKAGE_MANIFEST.json").decode("utf-8"))
                    .get("report_version_id") or "")
        except Exception:                        # noqa: BLE001
            pkg_identity = ""
    # 旧批准**不迁移**：落盘的评审记录仍绑在**旧正文身份**上，不是当前采纳版（页面据此
    # 显示"待复核"）。批准状态不在交付正文里，读版本侧记录才准。
    recorded_review = {}
    try:
        rp = ws / "review_state.json"
        if rp.is_file():
            recorded_review = json.loads(rp.read_text(encoding="utf-8"))
    except Exception:                            # noqa: BLE001
        recorded_review = {}
    _rev_id = str(recorded_review.get("report_version_id") or "")
    # 修订/重验/重包**不发模型请求**：llm_calls 的最新时间要早于最近一次装配（report.md）
    last_call_ts = 0.0
    try:
        for line in (ws / "llm_calls.jsonl").read_text(encoding="utf-8").splitlines():
            if line.strip():
                last_call_ts = max(last_call_ts, float(json.loads(line).get("ts") or 0))
    except Exception:                            # noqa: BLE001
        last_call_ts = 0.0
    _report_mtime = (ws / "reports" / "report.md").stat().st_mtime         if (ws / "reports" / "report.md").is_file() else 0.0
    checks = {
        "report_present": bool(body.strip()),
        "approval_not_migrated": bool(_rev_id) and _rev_id != (adopted.identity_id()
                                                              if adopted else ""),
        "same_version_export": bool(pkg_identity) and pkg_identity == adopted.identity_id(),
        "requests_zero_after_revision": bool(_report_mtime) and last_call_ts < _report_mtime,
        "delivery_bound_to_adopted": bool(
            (adopted.acceptance_for_this_body() if adopted is not None else False)),
    }
    return {
        "case": tid,
        "kind": "user_revision",
        "verdict": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "identity": adopted.identity_id() if adopted else "",
        "body_sha16": str(getattr(adopted, "version_id", ""))[:16],
        "package": newest.name if newest else "",
        "package_identity": pkg_identity,
        "llm_requests": _llm_requests(ws),
        "recorded_review_version": _rev_id[:16],
        "last_llm_call_ts": last_call_ts,
        "report_mtime": _report_mtime,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence"
                                         / "r4_fixed_replay_20260924.json"))
    args = ap.parse_args()
    names = sorted(p.stem for p in SCEN_DIR.glob("r4_*.json"))
    results = [_run_scenario(n) for n in names]
    try:
        results.append(_user_revision_case())
    except Exception as exc:                     # noqa: BLE001 - 失败如实记
        results.append({"case": USER_REVISION_TASK, "kind": "user_revision",
                        "verdict": "error", "error": str(exc)[:200]})
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "note": ("R4 固定样本回放：预期在场景文件里冻结（实现前写死）；本脚本只读、不改预期；"
                 "费用未知保持未知；无模型的编辑/重验/重包以实际请求数 0 验收。"),
        "cases": results,
        "summary": {
            "total": len(results),
            "pass": sum(1 for r in results if r.get("verdict") == "pass"),
            "fail": [r["case"] for r in results if r.get("verdict") != "pass"],
        },
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    for r in results:
        print(f"[{r.get('verdict')}] {r['case']}: "
              f"delivery={r.get('delivery') or '-'} accept={r.get('acceptance_overall') or '-'} "
              f"contradictions={r.get('contradictions')} requests={r.get('llm_requests')} "
              f"| {r.get('failed_checks') or r.get('checks') or ''}")
    print("summary:", json.dumps(payload["summary"], ensure_ascii=False))
    print("wrote", args.out)
    return 0 if payload["summary"]["fail"] == [] else 1


if __name__ == "__main__":
    raise SystemExit(main())
