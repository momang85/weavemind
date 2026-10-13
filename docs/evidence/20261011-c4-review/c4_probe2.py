# -*- coding: utf-8 -*-
"""C4 装配函数冒烟 v2：三一 fy／洋河 fy+H1／保存重开／一页摘要实页数。"""
import hashlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from financial_analysis import research_experiment as rx     # noqa: E402
from workspace import task_workspace                          # noqa: E402

EV = ROOT / "tmp" / "research-studies"
WS = task_workspace("", project="default").parent          # …/projects/default（不写死用户名路径）
SANY = {"study": EV / "4dba42a6bdde9227", "entity": "600031.SH",
        "ws": Path(task_workspace("study-report-4dba42a6bdde9227-600031.SH", project="default"))}
YANGHE = {"study": EV / "0af20919e6245b88", "entity": "002304.SZ",
          "ws": Path(task_workspace("study-report-0af20919e6245b88-002304.SZ", project="default"))}


def full_run(study, entity, prefix, ws=None):
    for r in rx.list_candidate_runs(study_dir=study, entity_id=entity, workspace_dir=ws):
        if r["run_id"].startswith(prefix):
            return r["run_id"]
    raise SystemExit(f"没有 run {prefix}")


def frozen_hashes(study: Path) -> dict:
    out = {}
    for p in sorted(study.glob("*.json")):
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return out


def main() -> int:
    print("== ① 三一：旧快照（营运资本主导）→ 判据不得写成「利润仍主导」 ==")
    art = rx.prepare_company_research_experiment(
        study_dir=SANY["study"], entity_id=SANY["entity"],
        run_id=full_run(SANY["study"], SANY["entity"], "5d4b99cc", SANY["ws"]),
        view="yoy", workspace_dir=SANY["ws"],
        old_material_id="0c3e82056e977adc", new_material_id=None)
    for c in art["cards"]:
        print("   -", c["judgment_id"], c["status"], "|", c["criterion"]["observation_condition"])
    cas = next(c for c in art["cards"] if c["judgment_id"] == "cash_from_working_capital")
    print("   现金卡削弱条件：", cas["criterion"]["weaken_single"])
    assert "营运资本项" in cas["criterion"]["observation_condition"], "三一旧主导项应写成营运资本项"
    assert "利润" not in cas["criterion"]["observation_condition"].replace("利润项", ""), "不得写利润仍主导"
    yoy = next(c for c in art["experiment"]["cells"] if c["view"] == "yoy")
    print("   三一同比：", yoy["points_yi"], "λ*", yoy["zero_lambda"], yoy["reachable_within_0_1"])
    assert yoy["points_yi"] == ["91.06058000000", "32.99465500000", "-25.07127000000"]
    assert yoy["zero_lambda"] == "0.7841137465733991148853652120"

    print("== ② 洋河：旧年报 → H1（本轮正常动作），本期＋同比两格 ==")
    before = frozen_hashes(YANGHE["study"])
    art2 = rx.prepare_company_research_experiment(
        study_dir=YANGHE["study"], entity_id=YANGHE["entity"],
        run_id=full_run(YANGHE["study"], YANGHE["entity"], "370dba92", YANGHE["ws"]),
        view="current", metric="operating_payable_increase", workspace_dir=YANGHE["ws"],
        old_material_id="f42e747c73850b59", new_material_id="2ce860dddd94e5a6",
        version_workspace=YANGHE["ws"])
    for c in art2["experiment"]["cells"]:
        print("   cell", c["view"], c.get("applicable"), c.get("metric"), c.get("points_yi"),
              "λ*", c.get("zero_lambda"), c.get("reachable_within_0_1"))
    cur = next(c for c in art2["experiment"]["cells"] if c["view"] == "current")
    assert cur["points_yi"] == ["-7.63040326790", "15.16157821075", "37.95355968940"], cur["points_yi"]
    assert cur["zero_lambda"] == "0.1673922751088502363681697682", cur["zero_lambda"]
    print("   卡：", [(c["judgment_id"], c["status"]) for c in art2["cards"]])
    print("   基于：", json.dumps({k: v for k, v in art2["based_on"].items()
                                   if k in ("old_material_id", "old_cutoff", "old_snapshot_sha256",
                                            "criteria_sha256", "report_version")},
                                  ensure_ascii=False)[:600])
    rv = art2["based_on"].get("report_version") or {}
    print("   版本引用（真实字段）：", rv.get("version_id", "")[:16], rv.get("acceptance_overall"),
          rv.get("acceptance_ok"), rv.get("adopted"))
    assert len(str(rv.get("version_id") or "")) == 64, "版本引用必须是真实的正文哈希"
    assert rv.get("acceptance_overall") == "pass", "验收取真实 overall 字段"
    print("   三态：", json.dumps(art2["research_state"], ensure_ascii=False)[:220])
    print("   两项动作：", [a["action"][:44] for a in art2["next_actions"]])

    print("== ③ 保存／重开：同版 JSON＋一页摘要（实页数） ==")
    saved = rx.save_experiment(art2, study_dir=YANGHE["study"])
    print("   saved:", json.dumps({k: saved[k] for k in ("experiment_id", "files", "urls")},
                                  ensure_ascii=False))
    re1 = rx.load_experiment(study_dir=YANGHE["study"], experiment_id=saved["experiment_id"])
    saved2 = rx.save_experiment(re1, study_dir=YANGHE["study"])
    assert saved2["experiment_id"] == saved["experiment_id"], "同输入重放必须同内容身份"
    from report_pdf import _page_contents
    pdf = (YANGHE["study"] / "experiments" / f"{saved['experiment_id']}.pdf")
    if pdf.is_file():
        pages = len(_page_contents(pdf.read_bytes()))
        print("   摘要 PDF 实页数:", pages, "bytes:", pdf.stat().st_size)
    else:
        pages = -1
        print("   摘要 PDF 未生成（如实登记）")
    md = (YANGHE["study"] / "experiments" / f"{saved['experiment_id']}.md").read_text(encoding="utf-8")
    print("   摘要行数:", len(md.splitlines()))
    after = frozen_hashes(YANGHE["study"])
    print("   研究目录冻结文件未变:", before == after)
    assert before == after, "保存不得改写研究目录既有文件"
    print("   新增文件:", sorted(p.name for p in (YANGHE["study"] / "experiments").iterdir()))
    print("== ④ 另一个合格 run／另一个主体：同函数无需改代码 ==")
    runs = rx.list_candidate_runs(study_dir=YANGHE["study"], entity_id=YANGHE["entity"],
                                  workspace_dir=YANGHE["ws"])
    for r in runs:
        print("   -", r["run_id"][:12], r["source"], {v: x["applicable"] for v, x in r["views"].items()})
    opts = rx.experiment_options(study_dir=YANGHE["study"], entity_id=YANGHE["entity"],
                                 run_id=runs[1]["run_id"], workspace_dir=YANGHE["ws"])
    print("   候选项（current）:", [(c["metric"], c["item_yi"]) for c in opts["candidates"]["current"]][:3])
    print("   旧材料:", [m["material_id"] + "@" + m["disclosed_at"] for m in opts["old_materials"]])
    print("   新材料:", [m["material_id"] + "@" + m["disclosed_at"] for m in opts["new_materials"]])
    print("   已存实验:", opts["saved_experiments"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
