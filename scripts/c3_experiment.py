# -*- coding: utf-8 -*-
"""C3/C4 研究实验脚本 —— C4-1 之后**不再是唯一生产路径**。

装配已提成 `financial_analysis.research_experiment`（普通研究页面
`POST /api/research-study/experiment` 与 `research_report_adapter` 走同一份公开函数）。
本脚本现在只做**重放校验**，默认不写任何文件：

    python -X utf8 scripts/c3_experiment.py            # 只读：重放并与冻结基线比对
    python -X utf8 scripts/c3_experiment.py --write    # 另把实验冻结进 experiments/（幂等）

对 `tmp/research-studies/*/`（有 study_manifest.json + financial.json）逐个研究、逐个主体：
1. 候选 run、候选项、登记材料**现场解析**（study manifest + 登记材料登记表 +
   候选工作区；不写死 sid／run 前缀／Temp 路径）；
2. 对每份冻结实验（C3 基线 `c3_experiment_<code>.json` 与 C4 产物 `experiments/<id>.json`）
   以它自己记录的 run／视角／候选项重放，比对**精确数值**（C、a、0/50/100%、λ*、可达性）
   与判据身份哈希——判据文案 C4-3 已改为单一定义（文案/求值同源），故不比卡片文本；
3. C4 产物另校验内容身份：同组输入重放应得同一个 `experiment_id`；
4. `--write` 用 `save_experiment` 把重放实验冻结进 `experiments/`（幂等，同组输入同 id）；
   **永不写 `c3_experiment_<code>.json`**（C3 验收基线只读，不覆盖）。

退出码：全部一致＝0；任一不一致＝1（如实报告，不掩盖）。
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from financial_analysis import research_experiment as R  # noqa: E402

STUDY_ROOT = ROOT / "tmp" / "research-studies"


def _workspace_of(study_id: str, entity_id: str) -> Path:
    """候选工作区：与 API handler 同一解析（task_workspace，不写死路径）。"""
    from workspace import task_workspace
    return Path(task_workspace(f"study-report-{study_id[:16]}-{entity_id}", project="default"))


def _studies() -> list[Path]:
    if not STUDY_ROOT.is_dir():
        return []
    return sorted(p for p in STUDY_ROOT.iterdir()
                  if (p / "study_manifest.json").is_file() and (p / "financial.json").is_file())


def _frozen_experiments(study_dir: Path, entity_id: str) -> list[dict]:
    """本研究的冻结实验（C4 产物），不写死 id。"""
    out = []
    for p in sorted((study_dir / R.EXPERIMENTS_DIR).glob("*.json")):
        try:
            art = R.load_experiment(study_dir=study_dir, experiment_id=p.stem)
        except (ValueError, OSError):
            continue
        if isinstance(art, dict) and str(art.get("entity_id") or "") == str(entity_id):
            out.append(art)
    return out


def _frozen_baselines(study_dir: Path, entity_id: str) -> list[dict]:
    """C3 验收基线 `c3_experiment_<code>.json`（旧脚本产物，只读）。"""
    out = []
    for p in sorted(study_dir.glob("c3_experiment_*.json")):
        try:
            art = json.loads(p.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            continue
        if str(art.get("schema") or "") == R.SCHEMA and str(art.get("entity_id") or "") == str(entity_id):
            out.append(art)
    return out


def _numeric_view(art: dict, view: str) -> dict:
    """只取数值层：C、a、points（元，按 Decimal 比数值不比尾部零）、λ*、可达性。"""
    lad = ((art.get("experiment") or {}).get("views") or {}).get(view) or {}

    def _dec(v) -> str:
        if v in (None, ""):
            return ""
        try:
            return str(Decimal(str(v)).normalize())
        except (InvalidOperation, ValueError):
            return str(v)

    return {"applicable": bool(lad.get("applicable")),
            "metric": str(lad.get("metric") or ""),
            "base_yuan": _dec((lad.get("inputs_exact") or {}).get("base_yuan")),
            "item_yuan": _dec((lad.get("inputs_exact") or {}).get("item_yuan")),
            "points": [_dec(p.get("value_yuan")) for p in (lad.get("points") or [])],
            "zero_lambda": _dec(lad.get("zero_lambda")),
            "reachable_within_0_1": bool(lad.get("reachable_within_0_1"))}


def _replay(art: dict, study_dir: Path, study_id: str, result_hash: str, entity_id: str,
            ws: Path, write: bool) -> dict:
    """以冻结产物自己记录的 run／视角／候选项重放，返回（重放产物, 比对结论）。"""
    ex = art.get("experiment") or {}
    sel = dict(ex.get("selection") or {})
    bo = art.get("based_on") if isinstance(art.get("based_on"), dict) else {}
    run_id = str((ex.get("run_ref") or {}).get("run_id") or "")
    if not run_id:
        return {"art": None, "mismatches": ["run_ref.run_id 缺失，无法重放"]}
    metric = str(sel.get("metric") or "") or None
    fractions = tuple(str(f) for f in (sel.get("fractions") or ("0", "0.5", "1")))
    new = R.prepare_company_research_experiment(
        study_dir=study_dir, study_id=study_id, result_hash=result_hash,
        entity_id=entity_id, run_id=run_id, metric=metric, fractions=fractions,
        # 冻结产物自己记录的重放输入：旧/新材料与父报告技术来源（缺则按默认解析）
        old_material_id=(str(bo.get("old_material_id") or "") or None),
        new_material_id=(str(bo.get("new_material_id") or "") or None),
        technical_source=(bo.get("technical_source")
                          if isinstance(bo.get("technical_source"), dict) else None),
        workspace_dir=ws, version_workspace=ws,
        version_task_id=f"study-report-{study_id[:16]}-{entity_id}")
    mismatches = []
    for view in ("current", "yoy"):
        a, b = _numeric_view(art, view), _numeric_view(new, view)
        if a != b:
            mismatches.append(f"{view}: 基线={a} 重放={b}")
    # 判据身份与内容身份：用不落盘也算得出的同一口径（R.experiment_identity）
    ai = art.get("identity") or {}
    bi = R.experiment_identity(new)
    if ai.get("criteria_sha256") and ai.get("criteria_sha256") != bi.get("criteria_sha256"):
        mismatches.append(f"criteria_sha256: 基线={ai.get('criteria_sha256')} 重放={bi.get('criteria_sha256')}")
    if ai.get("experiment_id") and ai.get("experiment_id") != bi.get("experiment_id"):
        mismatches.append(f"experiment_id: 基线={ai.get('experiment_id')} 重放={bi.get('experiment_id')}")
    if ai.get("content_sha256") and ai.get("content_sha256") != bi.get("content_sha256"):
        mismatches.append("content_sha256: 基线≠重放")
    # 只有**重放身份与冻结身份一致**时才允许 re-freeze（纯幂等：同 id／同字节），
    # 绝不因为校验就凭空多造一份实验产物（C3 基线与未冻结的重放都不写）。
    same_identity = bool(ai.get("experiment_id")) and ai.get("experiment_id") == bi.get("experiment_id")
    saved = R.save_experiment(new, study_dir=study_dir) if (write and same_identity and not mismatches) else None
    return {"art": new, "mismatches": mismatches, "saved": saved}


def main() -> int:
    write = "--write" in sys.argv[1:]
    fails: list[str] = []
    studies = _studies()
    print(f"研究目录 {len(studies)} 个（{'重放＋写入' if write else '只读重放'}）")
    for d in studies:
        man = json.loads((d / "study_manifest.json").read_text(encoding="utf-8")) or {}
        fin = json.loads((d / "financial.json").read_text(encoding="utf-8")) or {}
        study_id = str(man.get("study_id") or d.name)
        result_hash = str(man.get("result_hash") or "")
        print(f"\n== {d.name}（{str(man.get('title') or study_id)[:40]}）result_hash={result_hash[:16]}…")
        if not result_hash:
            fails.append(f"{d.name}: manifest 无 result_hash")
            continue
        for eid in sorted(str(k) for k in fin.keys()):
            ws = _workspace_of(study_id, eid)
            arts = _frozen_experiments(d, eid) + _frozen_baselines(d, eid)
            if not arts:
                print(f"   - {eid}: 无冻结实验（跳过，不造数）")
                continue
            for art in arts:
                tag = ("C4 " + str((art.get("identity") or {}).get("experiment_id") or "")
                       if (art.get("identity") or {}).get("experiment_id") else "C3 基线")
                r = _replay(art, d, study_id, result_hash, eid, ws, write)
                if r["mismatches"]:
                    fails.append(f"{d.name}/{eid}/{tag}: " + "；".join(r["mismatches"]))
                    print(f"   - {eid} {tag}: FAIL — {'；'.join(r['mismatches'])[:200]}")
                else:
                    extra = ""
                    if r.get("saved"):
                        extra = f"（写入 {r['saved'].get('experiment_id')}，同版幂等={r['saved'].get('reopened_same_bytes')}）"
                    print(f"   - {eid} {tag}: 数值与判据身份一致{extra}")
    # 旧写法把含反斜杠的字符串写进 f-string 表达式，Python ≤3.11 报语法错误（PEP 701 才放宽）；
    # 提成局部变量，输出不变。
    tail = "ALL PASS" if not fails else "FAILED:\n  " + "\n  ".join(fails)
    print(f"\n{tail}")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
