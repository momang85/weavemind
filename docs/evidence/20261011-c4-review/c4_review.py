# -*- coding: utf-8 -*-
"""C4 复核探针（只读研究目录；staging 用临时目录）：普通研究页
"选研究→挑战判断→新披露更新→保存行动"。

A 新通路值 == C3/C2e 精确参考（直接调 `financial_analysis.research_experiment`）：
  三一 600031.SH（study frozen run 5d4b99cc…）本期/同比；洋河 002304.SZ（续页 run
  370dba92…，候选工作区现场解析）本期/同比：C、a、0/50/100%、λ*、可达性逐值相等。
B 判据单一定义（文案/求值同源）：三一旧快照下现金卡条件写"营运资本项"、不写"利润仍主导"；
  洋河五卡状态、每卡带 window_count/单窗口削弱/确认门槛与判据哈希（与冻结产物一致）。
C 冻结/幂等/一页：以冻结产物自身记录的输入重放 → 同 experiment_id、同内容哈希；
  再冻结不新增文件、不改字节；重开同版；摘要 PDF 实页数 == 1。
D 接缝（C4-3）：完整实验 JSON 真实入包——adapter 优先 C4 冻结实验、staging 逐字节同版、
  `exact_values_in` 指向真实成员、交付管线收录 `analysis/c3_experiment_*.json`。
E 另一个合格 run／另一个候选 ID 无需改代码：研究冻结＋续页两条 run 现场解析、
  显式候选项（非默认）与三一另一个 run 各自可算，λ*==C/a。
F 守卫：未知视角不造数（ladder 守卫）、身份不符不装配、内容身份对同一产物稳定。

运行：python -X utf8 docs/evidence/20261011-c4-review/c4_review.py
"""
import hashlib
import io
import json
import shutil
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from financial_analysis import research_experiment as R  # noqa: E402
from financial_analysis.operators.cash_reconciliation import reversion_ladder  # noqa: E402
import research_report_adapter as AD  # noqa: E402
import report_pdf  # noqa: E402
from workspace import task_workspace  # noqa: E402

STUDY_ROOT = ROOT / "tmp" / "research-studies"
REF = json.loads((ROOT / "docs/evidence/20261011-c2e-accept/innovation_reference.json")
                 .read_text(encoding="utf-8"))
STUDY = {"sany": "4dba42a6bdde92276c4dabe428e9e4e77c1a31d39f3959b8631e0f6061ae8548",
         "yanghe": "0af20919e6245b888974ee17321f327f99580ab745687e80aca6dae6921e7f4f"}
ENTITY = {"sany": "600031.SH", "yanghe": "002304.SZ"}
RUN_REF = {"sany": REF["sany"]["yoy"]["run_id"], "yanghe": REF["yanghe"]["yoy"]["run_id"]}
YH_EID = "245bcd5e56ef7c5eafaf11e2"
SANY_OLD_MATERIAL = "0c3e82056e977adc"          # 三一 2024 年报（2025-04-18，旧机制依据）
results = {"checks": [], "detail": {}}


def check(name, ok, detail=None):
    results["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail is not None else ""))
    return bool(ok)


def dec(v):
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v)).normalize()
    except (InvalidOperation, ValueError):
        return str(v)


def study_dir(kind):
    return STUDY_ROOT / STUDY[kind][:16]


def manifest(kind):
    return json.loads((study_dir(kind) / "study_manifest.json").read_text(encoding="utf-8")) or {}


def ws_of(kind, entity=None):
    return Path(task_workspace(f"study-report-{STUDY[kind][:16]}-{entity or ENTITY[kind]}",
                               project="default"))


def prepare(kind, *, view, metric=None, run_id=None, old_material_id=None,
            new_material_id=None, technical_source=None):
    entity = ENTITY[kind]
    return R.prepare_company_research_experiment(
        study_dir=study_dir(kind), study_id=STUDY[kind],
        result_hash=str(manifest(kind).get("result_hash") or ""), entity_id=entity,
        run_id=run_id or RUN_REF[kind], view=view, metric=metric,
        old_material_id=old_material_id, new_material_id=new_material_id,
        technical_source=technical_source, workspace_dir=ws_of(kind),
        version_workspace=ws_of(kind), version_task_id=f"study-report-{STUDY[kind][:16]}-{entity}")


def replay_frozen(kind, eid):
    """以**冻结产物自己记录的输入**重放（run／视角／候选项／旧新材料／技术来源）。"""
    froz = R.load_experiment(study_dir=study_dir(kind), experiment_id=eid)
    ex, bo = froz.get("experiment") or {}, froz.get("based_on") or {}
    sel = ex.get("selection") or {}
    art = prepare(kind, view=str(sel.get("view") or "current"), metric=str(sel.get("metric") or "") or None,
                  run_id=str((ex.get("run_ref") or {}).get("run_id") or ""),
                  old_material_id=str(bo.get("old_material_id") or "") or None,
                  new_material_id=str(bo.get("new_material_id") or "") or None,
                  technical_source=(bo.get("technical_source")
                                    if isinstance(bo.get("technical_source"), dict) else None))
    return froz, art


def cell(art, view):
    lad = ((art.get("experiment") or {}).get("views") or {}).get(view) or {}
    return {"applicable": bool(lad.get("applicable")), "metric": str(lad.get("metric") or ""),
            "base_yuan": dec((lad.get("inputs_exact") or {}).get("base_yuan")),
            "item_yuan": dec((lad.get("inputs_exact") or {}).get("item_yuan")),
            "points": [dec(p.get("value_yuan")) for p in (lad.get("points") or [])],
            "zero_lambda": dec(lad.get("zero_lambda")),
            "reachable_within_0_1": bool(lad.get("reachable_within_0_1"))}


def ref_cell(kind, view):
    r = REF[kind][view]
    return {"applicable": True, "metric": r["item_metric"], "base_yuan": dec(r["base_yuan"]),
            "item_yuan": dec(r["item_yuan"]),
            "points": [dec(Decimal(str(r["values_yi"][k])) * Decimal("100000000"))
                       for k in ("0", "0.5", "1")],
            "zero_lambda": dec(r["zero_lambda"]),
            "reachable_within_0_1": bool(r["reachable_within_0_1"])}


def flat(d):
    return {k: (str(v) if not isinstance(v, list) else [str(x) for x in v]) for k, v in d.items()}


print("== A 新通路值 == 精确参考（C3/C2e，四格逐值相等） ==")
arts = {
    ("sany", "current"): prepare("sany", view="current"),
    ("sany", "yoy"): prepare("sany", view="yoy"),
}
froz_yh, arts[("yanghe", "current")] = replay_frozen("yanghe", YH_EID)
for view in ("current", "yoy"):
    arts[("yanghe", view)] = arts[("yanghe", "current")]
for kind, view in arts:
    got, want = cell(arts[(kind, view)], view), ref_cell(kind, view)
    check(f"A {kind}.{view} 四格精确一致", got == want,
          {"got": flat(got), "want": flat(want)})
results["detail"]["cells"] = {f"{k}.{v}": flat(cell(a, v)) for (k, v), a in arts.items()}

print("\n== B 判据单一定义（文案/求值同源） ==")
sy = prepare("sany", view="yoy", old_material_id=SANY_OLD_MATERIAL, new_material_id=None)
sy_cards = {c.get("judgment_id"): c for c in (sy.get("cards") or [])}
cash = sy_cards.get("cash_from_working_capital") or {}
crit = cash.get("criterion") or {}
check("B 三一旧快照下现金卡条件写「营运资本项」",
      "营运资本项" in str(crit.get("observation_condition") or ""),
      str(crit.get("observation_condition") or "")[:110])
check("B 三一不再写「利润仍主导」（跨类型不可照抄旧说法）",
      all(s not in json.dumps(cash, ensure_ascii=False) for s in ("利润仍主导", "仍是利润", "仍以利润")),
      str(crit.get("weaken_single") or "")[:110])
check("B 三一（新材料未取得）如实 pending/无法判断",
      str((sy_cards.get("cash_from_working_capital") or {}).get("status")) in
      ("无法判断", "待观察", "pending", "") or bool(cash.get("pending")),
      str(cash.get("status")))
yh_cards = {c.get("judgment_id"): c for c in (froz_yh.get("cards") or [])}
check("B 洋河五卡（H1 回放：一支持一削弱）", len(yh_cards) == 5,
      {k: v.get("status") for k, v in yh_cards.items()})
check("B 洋河成品库存卡《削弱》", (yh_cards.get("finished_goods_inventory_build") or {}).get("status") == "削弱")
check("B 每卡带单窗口削弱/确认门槛/判据哈希",
      all(isinstance(c.get("criterion"), dict)
          and {"window_count", "weaken_single", "weaken_confirmed", "criterion_sha256"}
          <= set(c["criterion"]) for c in (froz_yh.get("cards") or [])))
check("B 判据哈希与冻结产物一致",
      str((froz_yh.get("identity") or {}).get("criteria_sha256")) ==
      str((arts[("yanghe", "current")].get("identity") or {}).get("criteria_sha256")),
      str((froz_yh.get("identity") or {}).get("criteria_sha256"))[:16] + "…")

print("\n== C 冻结/幂等/一页 ==")
frozen_path = study_dir("yanghe") / R.EXPERIMENTS_DIR / f"{YH_EID}.json"
before_hash = hashlib.sha256(frozen_path.read_bytes()).hexdigest()
before_files = sorted(p.name for p in (study_dir("yanghe") / R.EXPERIMENTS_DIR).iterdir())
ident = R.experiment_identity(arts[("yanghe", "current")])
check("C 重放内容身份 == 冻结 id", ident["experiment_id"] == YH_EID, ident["experiment_id"])
check("C 重放内容哈希 == 冻结内容哈希",
      ident["content_sha256"] == str((froz_yh.get("identity") or {}).get("content_sha256")))
saved = R.save_experiment(arts[("yanghe", "current")], study_dir=study_dir("yanghe"))
after_files = sorted(p.name for p in (study_dir("yanghe") / R.EXPERIMENTS_DIR).iterdir())
check("C 再冻结同 id／同字节／不新增文件",
      saved["experiment_id"] == YH_EID and hashlib.sha256(frozen_path.read_bytes()).hexdigest() == before_hash
      and after_files == before_files, {"files": after_files})
reopened = R.load_experiment(study_dir=study_dir("yanghe"), experiment_id=YH_EID)
check("C 重开同版（内容身份一致＋摘要非空）",
      R.experiment_identity(reopened)["experiment_id"] == YH_EID
      and len(str(reopened.get("summary_markdown") or "")) > 0)
pdf = study_dir("yanghe") / R.EXPERIMENTS_DIR / f"{YH_EID}.pdf"
pages = len(report_pdf._page_contents(pdf.read_bytes())) if pdf.is_file() else 0
check("C 摘要 PDF 实页数 == 1", pages == 1,
      f"{pages} 页 / {pdf.stat().st_size if pdf.is_file() else 0}B")
base_jsons = sorted(study_dir("yanghe").glob("c3_experiment_*.json")) + \
    sorted(study_dir("sany").glob("c3_experiment_*.json"))
results["detail"]["baseline_hashes"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                        for p in base_jsons}
check("C C3 基线 JSON 两份仍在（只读未覆盖）", len(base_jsons) == 2,
      list(results["detail"]["baseline_hashes"]))

print("\n== D 接缝：完整实验 JSON 真实入包（C4-3） ==")
art_res, src = AD._c3_resolve(study_dir("yanghe"), "002304.SZ")
check("D adapter 优先 C4 冻结实验", bool(art_res) and src is not None
      and src.parent.name == R.EXPERIMENTS_DIR and src.stem == YH_EID, str(src))
blk = AD._c3_view_block(art_res)
check("D 视图块带判据/内容身份/依据/下一步动作",
      bool(((blk.get("cards") or [{}])[0] or {}).get("criterion"))
      and blk.get("experiment_id") == YH_EID
      and (blk.get("based_on") or {}).get("old_cutoff") == "2025-04-30"
      and bool(blk.get("next_actions")),
      {"experiment_id": blk.get("experiment_id"),
       "old_cutoff": (blk.get("based_on") or {}).get("old_cutoff"),
       "next_actions": len(blk.get("next_actions") or [])})
tmpws = Path(tempfile.mkdtemp(prefix="c4review_"))
try:
    AD._stage_candidate(study_dir("yanghe"), "002304.SZ", {}, {}, {}, tmpws)
    staged = tmpws / "analysis" / "c3_experiment_002304.SZ.json"
    check("D staging 的 JSON 与冻结产物逐字节同版",
          staged.is_file() and staged.read_bytes() == frozen_path.read_bytes(),
          f"{staged.stat().st_size if staged.exists() else 0}B")
    members = [f"analysis/{p.name}" for p in sorted((tmpws / "analysis").glob("c3_experiment_*.json"))]
    check("D exact_values_in 指向真实成员", blk.get("exact_values_in") in members,
          {"pointer": blk.get("exact_values_in"), "members": members})
    check("D 附件渲染稿同时生成", (tmpws / "analysis" / "c3_experiment_002304.SZ.md").is_file())
finally:
    shutil.rmtree(tmpws, ignore_errors=True)
dp_src = (ROOT / "delivery_pipeline.py").read_text(encoding="utf-8")
check("D 交付管线收录 analysis/c3_experiment_*.json", 'glob("c3_experiment_*.json")' in dp_src)
check("D 旧脚本产物仍回落（三一，没有 C4 冻结实验）",
      AD._c3_resolve(study_dir("sany"), "600031.SH")[1] is not None)

print("\n== E 另一个合格 run／另一个候选 ID 无需改代码 ==")
yh_opts = R.experiment_options(study_dir=study_dir("yanghe"), entity_id="002304.SZ",
                               run_id=RUN_REF["yanghe"], workspace_dir=ws_of("yanghe"))
runs = {r["run_id"]: r for r in yh_opts["runs"]}
check("E 候选 run 现场解析（冻结＋续页）且逐条给出视角可用性",
      len(runs) >= 2 and runs[RUN_REF["yanghe"]]["source"] == "report_continuation"
      and any(r["source"] == "study_frozen" for r in runs.values())
      and runs[[r for r in runs if runs[r]["source"] == "study_frozen"][0]]["views"]["current"]["applicable"] is False,
      {r[:8]: (runs[r]["source"], runs[r]["views"]["current"]["applicable"]) for r in runs})
cands = (yh_opts.get("candidates") or {}).get("current") or []
alt = next((str(c["metric"]) for c in cands
            if c["metric"] != "operating_payable_increase" and dec(c["item_yuan"]) not in (None, Decimal(0))), "")
alt_art = prepare("yanghe", view="current", metric=alt)
al = cell(alt_art, "current")
check("E 显式候选项（非默认，按 id 传）可算", bool(alt) and al["applicable"] and al["metric"] == alt
      and len(al["points"]) == 3, {"metric": alt, "points": [str(p) for p in al["points"]]})
check("E λ* == C/a（同源求值，非另算）",
      al["base_yuan"] is not None and al["item_yuan"] not in (None, Decimal(0))
      and abs(al["zero_lambda"] - (al["base_yuan"] / al["item_yuan"])) < Decimal("1e-25"),
      str(al["zero_lambda"]))
sy_opts = R.experiment_options(study_dir=study_dir("sany"), entity_id="600031.SH",
                               run_id=RUN_REF["sany"], workspace_dir=ws_of("sany"))
check("E 三一另一 run／另一主体同样可算（无需改代码）",
      bool(((sy_opts.get("views") or {}).get("yoy") or {}).get("applicable"))
      and ((sy_opts.get("views") or {}).get("current") or {}).get("selected_metric")
      == "operating_payable_increase",
      {v: (sy_opts["views"][v]["applicable"], sy_opts["views"][v]["selected_metric"])
       for v in sy_opts.get("views", {})})

print("\n== F 守卫 ==")
ent = ENTITY["sany"]
run_obj = next(r for r in R.list_candidate_runs(study_dir=study_dir("sany"), entity_id=ent,
                                                workspace_dir=ws_of("sany")) if r["run_id"] == RUN_REF["sany"])
lad_bad = reversion_ladder(R._find_run_object(study_dir=study_dir("sany"), entity_id=ent,
                                              run_id=run_obj["run_id"], workspace_dir=ws_of("sany")),
                           view="__nope__")
check("F 未知视角：不造数（applicable=False ＋原因）",
      lad_bad.get("applicable") is False and "未知视角" in str(lad_bad.get("reason") or ""),
      str(lad_bad.get("reason"))[:80])
check("F 身份不符不装配",
      AD._c3_resolve(study_dir("yanghe"), "002304.SZ", runs={"x": {"run_id": "f" * 64}})[0] == {})
check("F 内容身份对同一产物稳定",
      R.experiment_identity(froz_yh) == R.experiment_identity(json.loads(json.dumps(froz_yh))))

ok = all(c["ok"] for c in results["checks"])
results["summary"] = {"total": len(results["checks"]),
                      "passed": sum(c["ok"] for c in results["checks"]), "all_pass": ok}
out = Path(__file__).resolve().parent
(out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n",
                                  encoding="utf-8", newline="\n")
print(f"\n{'ALL PASS' if ok else 'FAILED'} — {results['summary']['passed']}/{results['summary']['total']}")
print(f"结果写入 {out / 'results.json'}")
sys.exit(0 if ok else 1)
