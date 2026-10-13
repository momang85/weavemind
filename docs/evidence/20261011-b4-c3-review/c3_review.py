# -*- coding: utf-8 -*-
"""C3 复核探针（只读）：两份新候选包的最小核验，结果落 results.json。

A 三一 600031.SH（同研究 4dba42a6… 新版包）：
   四格精确符号/三点/λ*（对照 innovation_reference.json）/域外不可达/现金主因前置/
   机制卡 pending/白酒字样=0/三态/run 身份 5d4b99cc。
B 洋河 002304.SZ（同研究 0af20919… 新版包）：
   四格精确值/run 身份 370dba92/机制卡 5+1（单位收入托底入卡，税费缓冲不重复计卡）/
   披露更新（旧截止 2025-04-30→新截止 2025-08-19、历史重建、销量 −32.35%/库存 −33.96%）/
   旧包 sha 不变。
运行：python -X utf8 docs/evidence/20261011-b4-c3-review/c3_review.py
"""
import hashlib
import io
import json
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parents[3]
TASKS = Path(r"C:\Users\ding0\AppData\Local\Temp\agent_workspace\tasks\projects\default")
WS = {"sany": TASKS / "study-report-4dba42a6bdde9227-600031.SH",
      "yanghe": TASKS / "study-report-0af20919e6245b88-002304.SZ"}
STUDY = {"sany": "4dba42a6bdde92276c4dabe428e9e4e77c1a31d39f3959b8631e0f6061ae8548",
         "yanghe": "0af20919e6245b888974ee17321f327f99580ab745687e80aca6dae6921e7f4f"}
REF = json.loads((ROOT / "docs/evidence/20261011-c2e-accept/innovation_reference.json")
                 .read_text(encoding="utf-8"))
RUN_REF = {"sany": "5d4b99ccb64c21faff4fe4a5c1ffaec09acccca0036e6ad5a87bbd62bd9dd24d",
           "yanghe": "370dba92a40e36a3acdfee14644085602e088e625db536b8111f2da9ac2329b4"}
# 旧 C2e 包（字节必须原样保留）与更早基线包
OLD_ZIPS = [
    (TASKS / "study-report-4dba42a6bdde9227-600031.SH" / "deliverables_20261011_171519_344cb4.zip",
     "d6d6b7def6b6ce44ad637d5df1d3ce648d81105c3e3081426cb3a05ff60db4bf"),
    (TASKS / "study-report-0af20919e6245b88-002304.SZ" / "deliverables_20261011_171523_8c5000.zip",
     "6d8c3914d3f4a387a8321dd4dfa125e8aa120cfd2f9610f99341f61312b51e75"),
    (TASKS / "study-report-d93e5c4a07e0fcbd-002304.SZ" / "deliverables_20261011_142232_080384.zip",
     "f79861e758f570affdea023c14602444a906e636f937afa617554a0c31e774df"),
]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def yi_key(s: str) -> str:
    """λ 档位键的规范化：'0.0'→'0'、'1.0'→'1'（reference 用小写十进制键）。"""
    return s.rstrip("0").rstrip(".") if "." in s else s


def ck(name, ok, detail):
    return {"check": name, "ok": bool(ok), "detail": detail}


out: dict = {}
for tag, ws in WS.items():
    zips = sorted((ws).glob("deliverables_*.zip"), key=lambda p: p.stat().st_mtime)
    zpath = zips[-1]                     # 最新包＝本批 C3 候选
    z = zipfile.ZipFile(zpath)
    md = z.read("reports/report.md").decode("utf-8")
    runs = json.loads(z.read("analysis/analysis_runs.json").decode("utf-8"))["runs"]
    ctx = json.loads(z.read("analysis/context.json").decode("utf-8"))
    # 同一份 JSON 的三个读口：研究目录（详情入口）/ 包内附件 / 正文
    code = "600031.SH" if tag == "sany" else "002304.SZ"
    src_json = ROOT / "tmp" / "research-studies" / STUDY[tag][:16] / f"c3_experiment_{code}.json"
    c3 = json.loads(src_json.read_text(encoding="utf-8"))
    att = z.read(f"analysis/c3_experiment_{code}.md").decode("utf-8")
    acc = json.loads(z.read("acceptance_report.json").decode("utf-8")) if \
        "acceptance_report.json" in z.namelist() else {}
    res = {"study_id": STUDY[tag], "zip": zpath.name, "zip_sha256": sha(zpath),
           "c3_source_json": str(src_json.relative_to(ROOT)), "checks": []}

    # ① 四格精确值对照 innovation_reference（同 run 身份、同 base/item、三点/λ*/可达性）
    ex = c3["experiment"]
    four_ok, four_det = True, {}
    for view in ("current", "yoy"):
        ref = REF["sany" if tag == "sany" else "yanghe"][view]
        lad = ex["views"][view]
        got = {"run_id": lad["refs"]["run_id"], "base_yuan": lad["inputs_exact"]["base_yuan"],
               "item_metric": lad["metric"], "item_yuan": lad["inputs_exact"]["item_yuan"],
               "zero_lambda": lad["zero_lambda"], "reach": lad["reachable_within_0_1"]}
        pts = {yi_key(str(p.get("reversion_fraction"))): p["value_yi"] for p in lad["points"]}
        for fr in ("0", "0.5", "1"):
            got[f"yi_{fr}"] = pts.get(fr)
        same = (got["run_id"] == ref["run_id"] and Decimal(got["base_yuan"]) == Decimal(ref["base_yuan"])
                and got["item_metric"] == ref["item_metric"]
                and Decimal(got["item_yuan"]) == Decimal(ref["item_yuan"])
                and Decimal(got["zero_lambda"]) == Decimal(ref["zero_lambda"])
                and got["reach"] == ref["reachable_within_0_1"]
                and all(Decimal(got[f"yi_{fr}"]) == Decimal(ref["values_yi"][fr]) for fr in ("0", "0.5", "1")))
        four_ok &= same
        four_det[view] = {"same_as_reference": same, "run_id": got["run_id"][:16] + "…",
                          "zero_lambda": got["zero_lambda"][:12] + "…",
                          "reach": got["reach"]}
    res["checks"].append(ck("四格与 innovation_reference 精确一致（run/base/item/三点/λ*/可达性）",
                            four_ok, four_det))

    # ② 域外不可达的措辞在场（范围不可达，不夹逼）
    res["checks"].append(ck("λ* 域外语义在场（范围不可达/不夹逼/λ∈[0,1]）",
                            "范围不可达" in md and "不夹逼" in md and "[0,1]" in md, {}))

    # ③ 现金主因前置节在前半部且同一 JSON：格式化位置检查
    cut = md.find("## 一、经营结论")
    pre = md[:cut] if cut > 0 else ""
    res["checks"].append(ck("现金主因与回撤实验前置节位于经营结论之前（现金主因前置）",
                            "## 现金主因与回撤实验（前置）" in pre,
                            {"before_section_1": cut > 0}))

    # ④ run 身份：四格来自本公司的冻结/续页 run（正文以 16 位前缀＋… 引用，不借别的 run）
    have = {str(r.get("run_id")) for r in runs}
    res["checks"].append(ck(f"run 身份在包内（{RUN_REF[tag][:16]}…）且正文引用同一身份",
                            RUN_REF[tag] in have and RUN_REF[tag][:16] in md
                            and ex["run_ref"]["run_id"] == RUN_REF[tag],
                            {"run_id": RUN_REF[tag][:16] + "…"}))

    # ⑤ 机制卡：数量与状态
    cards = c3["cards"]
    if tag == "sany":
        res["checks"].append(ck("三一机制卡 1 张、无法判断、pending（不为凑成功取数）",
                                len(cards) == 1 and cards[0]["status"] == "无法判断"
                                and cards[0].get("pending") is True,
                                {"cards": [c["judgment_id"] for c in cards]}))
        bad = [w for w in ("洋河", "白酒", "吨价", "高档酒", "中高档") if w in md]
        res["checks"].append(ck("三一正文无白酒字样（L2218 泛化后仍无残留）", not bad, {"hits": bad}))
    else:
        st = [(c["judgment_id"], c["status"]) for c in cards]
        res["checks"].append(ck("洋河机制卡 5 张（税费缓冲/销量/库存/现金方向/单位收入）",
                                st == [("profit_mechanism_and_buffer", "加强"),
                                       ("volume_contraction", "加强"),
                                       ("finished_goods_inventory_build", "削弱"),
                                       ("cash_from_working_capital", "加强"),
                                       ("unit_revenue_not_proof_of_pricing", "削弱")], st))
        res["checks"].append(ck("cash_buffer_structure 不重复计卡（随更新页呈现）",
                                all(c[0] != "cash_buffer_structure" for c in st), {}))
        # ⑥ 披露更新身份
        v = (((c3.get("disclosure_update") or {}).get("page") or {}).get("version") or {})
        based = str(((v.get("update_identity") or {}).get("based_on") or {})
                    .get("report_version_id") or "")
        vers = json.loads((ws / "report_versions.json").read_text(encoding="utf-8")) \
            .get("versions") or {}
        registered = set(vers) | {str(x.get("version_id")) for x in vers.values()}
        u_ok = (v.get("historical_rebuild") is True and v.get("old_cutoff") == "2025-04-30"
                and v.get("new_cutoff") == "2025-08-19"
                and len(based) == 64 and based in registered)
        res["checks"].append(ck("披露更新历史重建（旧截止 2025-04-30 → 新截止 2025-08-19，"
                               "based_on 为同研究已登记版本）",
                               u_ok, {"old": v.get("old_cutoff"), "new": v.get("new_cutoff"),
                                      "rebuild": v.get("historical_rebuild"),
                                      "based_on": based[:16], "registered": based in registered}))
        mats = v.get("materials") or []
        m_ok = ([(m.get("side"), m.get("material_id")) for m in mats]
                == [("earlier", "f42e747c73850b59"), ("later", "2ce860dddd94e5a6")])
        res["checks"].append(ck("两侧材料身份（f42e747c→2ce860dd，2025-08-19 公开）", m_ok,
                                {"materials": [(m.get("side"), m.get("material_id")) for m in mats]}))
        res["checks"].append(ck("H1 同比读数入正文：销量 -32.35%、库存量 -33.96% 且不年度相减/乘二",
                                "-32.35%" in md and "-33.96%" in md
                                and "半年" in md and "不年度相减" in md, {}))
        res["checks"].append(ck("一支持一削弱（销量收缩加强、库存累积削弱）在卡状态里",
                                ("volume_contraction", "加强") in st
                                and ("finished_goods_inventory_build", "削弱") in st, {}))

    # ⑦ 同版三件套：正文/详情(context.company_view)/附件(analysis/*.md)同一份 JSON
    cv = ((ctx.get("company_view") or {}).get("realization") or {}).get("c3_experiment") or {}
    lad0 = cv.get("views", {}).get("current") or {}
    ref0 = REF["sany" if tag == "sany" else "yanghe"]["current"]
    same_view = (lad0.get("item_yi") and Decimal(lad0["item_yi"]) == Decimal(ref0["item_yuan"]) / Decimal("100000000")
                 and Decimal(lad0["points_yi"][2]) == Decimal(ref0["values_yi"]["1"]))
    att_ok = all(x in att for x in (ref0["values_yi"]["0"], ref0["values_yi"]["0.5"],
                                    ref0["values_yi"]["1"], ref0["item_metric"]))
    res["checks"].append(ck("研究详情视图（company_view.c3_experiment）与正文/附件同一结果（可回撤项 0/50/100%）",
                            same_view and len(lad0.get("candidates") or []) > 0 and att_ok,
                            {"candidates": len(lad0.get("candidates") or []),
                             "item_yi": lad0.get("item_yi"), "attachment": att_ok}))
    res["checks"].append(ck("附件 `analysis/c3_experiment_*.md` 随包且含三点精确值与全部候选回撤项",
                            f"analysis/c3_experiment_{code}.md" in z.namelist()
                            and att.count("|") > 20 and "可选回撤项" in att,
                            {"attachment_lines": att.count(chr(10))}))
    # ⑫ 包内附件 == 现 JSON 确定性渲染（按行规范化：只比内容，换行符差异如实单列）
    sys.path.insert(0, str(ROOT))
    from research_report_adapter import _c3_attachment_md    # noqa: E402
    fresh = _c3_attachment_md(c3)
    same_txt = att.replace("\r\n", "\n").splitlines() == fresh.splitlines()
    res["checks"].append(ck("包内附件内容 == 研究目录同一 JSON 的确定性渲染（行规范化）",
                            same_txt and len(att) > 0,
                            {"bytes_equal": att.encode("utf-8") == fresh.encode("utf-8"),
                             "attachment_bytes": len(att.encode("utf-8")),
                             "fresh_bytes": len(fresh.encode("utf-8"))}))

    # ⑧ 三态（研究状态与验收报告在**工作区**，不在包内——按工作区读，不猜包内）
    st_j = json.loads((ws / "research_state.json").read_text(encoding="utf-8"))
    accw = json.loads((ws / "acceptance_report.json").read_text(encoding="utf-8"))
    res["checks"].append(ck("三态（state=not_applicable／未复核／仅内部）",
                            st_j.get("state") == "not_applicable" and "人工复核=未复核" in md
                            and "仅作内部研究用途" in md, {"state": st_j.get("state")}))

    # ⑨ 机器验收 pass（工作区里的本版验收报告）
    res["checks"].append(ck("本版机器验收 pass", accw.get("overall") == "pass",
                            {"overall": accw.get("overall"), "zip_has_acc": bool(acc)}))

    # ⑩ 附件里 c3 JSON 的边界与正文渲染一致（正文引用该 JSON，不另算）
    res["checks"].append(ck("附件 JSON 的 run_ref 与 company_view.run_ref 相同（同一入口）",
                            ex["run_ref"] == cv.get("run_ref"), {}))

    # ⑪ 旧包字节不变（C2e 两包＋C2c 基线）
    olds = {p.name: sha(p) == h for p, h in OLD_ZIPS}
    res["checks"].append(ck("旧包 sha256 不变（C2e 两包＋C2c 基线）", all(olds.values()),
                            {k[:24] + "…": v for k, v in olds.items()}))
    out[tag] = res

# ⑫ 纯函数护栏：域外/缺项/零项/无诊断块一律 applicable=False ＋原因（不造数）
sys.path.insert(0, str(ROOT))
from financial_analysis.operators import cash_reconciliation as CR   # noqa: E402
_z = zipfile.ZipFile(sorted((WS["sany"]).glob("deliverables_*.zip"),
                            key=lambda p: p.stat().st_mtime)[-1])
_runs = json.loads(_z.read("analysis/analysis_runs.json").decode("utf-8"))["runs"]
_sany_run = [r for r in _runs if str(r.get("run_id")) == RUN_REF["sany"]][0]
_zero = json.loads(json.dumps(_sany_run))
for _per in (_zero["outputs"][0]["diagnostics"]["reconciliation"].values()):
    for _it in _per.get("items", []):
        _it["value_yuan"] = 0.0
    _per["cashflow_yuan"] = 0.0
guards = {"checks": []}
for name, kw, want in [
    ("未知视角", {"view": "next"}, "未知视角"),
    ("λ 越域", {"view": "current", "fractions": (1.5,)}, "越域"),
    ("指定项缺项", {"view": "current", "metric": "no_such_item"}, "不在该 run 的候选里"),
    ("全零项（无同向项）", {"view": "current"}, "同向"),
]:
    got = CR.reversion_ladder(_zero if "全零" in name else _sany_run, **kw)
    guards["checks"].append(ck(f"护栏：{name} → applicable=False 且原因含「{want}」（不造数）",
                               got.get("applicable") is False and want in str(got.get("reason") or "")
                               and not got.get("points"),
                               {"reason": str(got.get("reason"))[:70]}))
nod = CR.reversion_ladder({"run_id": "x", "outputs": []}, view="current")
guards["checks"].append(ck("护栏：无诊断块 → applicable=False（不拿别的 run 数）",
                           nod.get("applicable") is False and "诊断块" in str(nod.get("reason")), {}))
det = CR.reversion_ladder(_sany_run, view="current")
det2 = CR.reversion_ladder(_sany_run, view="current")
guards["checks"].append(ck("冻结重读确定性（同一 run 两次调用逐字相同）",
                           json.dumps(det, ensure_ascii=False, sort_keys=True)
                           == json.dumps(det2, ensure_ascii=False, sort_keys=True), {}))
guards["checks"].append(ck("λ* 与 C/a 精确一致（Decimal，容差 1e-20）",
                           abs(Decimal(det["zero_lambda"])
                               - Decimal(det["inputs_exact"]["base_yuan"])
                               / Decimal(det["inputs_exact"]["item_yuan"])) < Decimal("1e-20"), {}))
out["guards"] = guards

OUT = Path(__file__).resolve().parent / "results.json"
OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
bad = [(t, c["check"], c["detail"]) for t, res in out.items() for c in res["checks"] if not c["ok"]]
print("检查数:", sum(len(r["checks"]) for r in out.values()), "| 未通过:", len(bad))
for t, name, det in bad:
    print("FAIL", t, name, json.dumps(det, ensure_ascii=False)[:400])
print("结果已写", OUT)
sys.exit(1 if bad else 0)
