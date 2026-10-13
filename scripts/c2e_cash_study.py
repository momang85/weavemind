# -*- coding: utf-8 -*-
"""C2e 两案：①洋河续页时点修正；②三一 600031 异行业现金判断（主力）。

架构师裁定（docs/总架构师执行指令.md）：
- 80% = 三一 600031 异行业现金判断（2024/2023 两期，官方缓存 2024 年报补充资料 17 项×2 年）；
- ≤20% = 洋河续页时点修正：**主特征截止回到 2025-04-30**（C2d 把它抬到 2026-04-28 是错的），
  原 T／基期／目标／g／m／B／10+10 与九点格值逐字保留；16 项×2 年补充资料只进续页 cash run。

两案都用**既有**抽取器（adapters.cashflow_supplement_tables）与既有投影形状；
本脚本只做：读官方缓存原件 → 抽取 → 独立 Decimal 闭合自检 → 写输入 → 跑研究。

用法：
    python -X utf8 scripts/c2e_cash_study.py --case sany               # 只自检、不写盘
    python -X utf8 scripts/c2e_cash_study.py --case sany --run
    python -X utf8 scripts/c2e_cash_study.py --case yanghe-followup --run
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_INPUT_DIR = ROOT / "tmp" / "c2c-inputs"          # C2c 已投影输入（只读复用）
BASE_STUDY = "d93e5c4a07e0fcbd"                        # C2c 候选（只读；父假设来源）
DEFAULT_OUT_STUDY_DIR = ROOT / "tmp" / "research-studies"
LEDGER_VERSION = "cash_supplement_projection/1.0.0"
OFFICIAL = ROOT / "evals" / "a2_official_chain_20260929"

CASES: dict[str, dict] = {
    # —— ② ≤20%：洋河续页时点修正（主截止不动，只把现金资料接进续页 run） ——
    "yanghe-followup": {
        "entity_id": "002304.SZ", "entity": "洋河股份",
        "material_id": "ee4429eecac66f50",
        "doc": OFFICIAL / "002304-2025" / "project" / "materials" / "ee4429eecac66f50" / "doc.json",
        "periods": (2025, 2024), "disclosed": "2026-04-28",
        "feature_cutoff": "2025-04-30",       # 父候选原值：不移动主截止
        "cutoff_relation": "after",            # 披露晚于主截止 ⇒ 只进续页 dataset
        "expect_items": 16,                    # 洋河本表无「信用减值损失」行（如实缺）
        "out_input_dir": ROOT / "tmp" / "c2e-yanghe-inputs",
        "base_filter": None,                   # 与父候选同输入（两主体都保留）
        "name": ("C2e：洋河 2025 利润→现金双视角（时点修正版：主特征截止回到父候选的 "
                 "2025-04-30，T／基期／目标／g／m／B／10+10 与九点格值逐字继承 d93e5c4a；"
                 "2025 年报现金流量表补充资料 16 项×2 年只进续页 cash run，不改主卡/主网格）"),
    },
    # —— ① 80%：三一 600031 异行业现金判断（新研究、新包；不套白酒量价/洋河方向） ——
    "sany": {
        "entity_id": "600031.SH", "entity": "三一重工",
        "material_id": "0c3e82056e977adc",
        "doc": OFFICIAL / "600031" / "project" / "materials" / "0c3e82056e977adc" / "doc.json",
        "periods": (2024, 2023), "disclosed": "2025-04-18",
        "feature_cutoff": "2025-04-30",       # 2024 年报披露日 2025-04-18 早于主截止 ⇒ 进主运行
        "cutoff_relation": "before",
        "expect_items": 17,                    # 三一表 17 项全在（含信用减值损失）
        "out_input_dir": ROOT / "tmp" / "c2e-sany-inputs",
        "base_filter": ("600031.SH",),         # 只保留三一：不把洋河行带进本包
        "name": ("C2e：三一重工 2024 利润→现金双视角（异行业现金判断：官方缓存 2024 年报"
                 "现金流量表补充资料 17 项×2 年入研究；特征截止 2025-04-30；"
                 "量价口径行缺 → 拒出、不拿行业台数配分产品收入）"),
    },
}


def _d(value) -> Decimal:
    return Decimal(str(value))


def _q(value) -> str:
    return str(_d(value).quantize(Decimal("0.01")))


def load_base(case: dict) -> list[dict]:
    """C2c 已投影行（只读复用：B1 基础行＋量价/分产品行；按案过滤主体）。"""
    rows = json.loads((BASE_INPUT_DIR / "financials.json").read_text(encoding="utf-8"))["rows"]
    keep = case.get("base_filter")
    if keep:
        rows = [r for r in rows if str(r.get("code")) in set(keep)]
    return rows


def _table_factor(facts: list[dict]) -> tuple[str, Decimal]:
    """表的原始单位 → 换算因子（千元→×1000）；从抽取器的 unit_raw 读，不假设。"""
    raws = {str(f.get("unit_raw") or "") for f in facts}
    if len(raws) != 1:
        raise SystemExit(f"表头原始单位不唯一（{sorted(raws)}）：停止，不投影")
    raw = raws.pop()
    factors = {"千元": Decimal(1000), "万元": Decimal(10000), "元": Decimal(1)}
    if raw not in factors:
        raise SystemExit(f"表头原始单位 {raw!r} 未登记：停止，不投影")
    return raw, factors[raw]


def extract_cash(case: dict, base_rows: list[dict]) -> tuple[dict, list[dict], dict]:
    """既有抽取器 → 补充资料事实＋投影行＋台账（含两期闭合/交叉核对独立自检）。"""
    from adapters.cashflow_supplement_tables import extract_cashflow_supplement
    from financial_analysis.operators.cash_reconciliation import ADJUSTMENT_ITEMS

    doc = json.loads(case["doc"].read_text(encoding="utf-8"))
    years = case["periods"]
    res = extract_cashflow_supplement(doc, periods=(f"{years[0]}-12-31", f"{years[1]}-12-31"))
    if not res.get("ok"):
        raise SystemExit(f"补充资料未取得（{res.get('reason') or '未识别'}）：不投影、不编数")
    facts = res.get("facts") or []
    disclosed = str(doc.get("disclosed_at") or doc.get("operator_disclosed_at") or "")
    if disclosed != case["disclosed"]:
        raise SystemExit(f"披露日 {disclosed} ≠ 裁定日 {case['disclosed']}：停止，不投影")
    raw_unit, factor = _table_factor(facts)
    entity_id, entity = case["entity_id"], case["entity"]
    if not all(str(f.get("caliber") or "") == "合并" for f in facts):
        raise SystemExit("存在非合并口径的调节项：停止，不投影")
    by = {(f["metric"], f["period"]): f for f in facts}
    want = list(ADJUSTMENT_ITEMS)
    got_metrics = sorted({f["metric"] for f in facts})
    extra = [m for m in got_metrics if m not in {w[0] for w in want}]
    if extra:
        raise SystemExit(f"抽取到算子之外的项（{'、'.join(extra)}）：不投影")
    pages = sorted({int(f["page"]) for f in facts if f.get("page") is not None})
    rows: list[dict] = []
    ledger: dict = {"schema": "weavemind.cash_supplement_projection/1", "version": LEDGER_VERSION,
                    "extractor": "adapters.cashflow_supplement_tables.extract_cashflow_supplement",
                    "case": case["name"], "material_id": case["material_id"],
                    "doc_path": str(case["doc"].relative_to(ROOT)),
                    "raw_sha256": str(doc.get("raw_sha256") or ""),
                    "text_sha256": str(res.get("text_hash") or ""),
                    "title": str(doc.get("title") or ""),
                    "disclosed_at": disclosed, "pages": pages,
                    "periods": list(res.get("periods") or []),
                    "unit_raw": raw_unit, "unit_factor": str(factor), "unit": "元",
                    "caliber": "合并", "start_caliber": "consolidated net_profit",
                    "sign_convention": "照抄披露符号（表内已注明“增加/减少以“－”号填列”）",
                    "rows": [], "closure": {}, "cross_check": {}, "missing_items": {},
                    "cutoff_relation": case["cutoff_relation"],
                    "feature_cutoff": case["feature_cutoff"]}
    commons = {"code": entity_id, "entity": entity, "period_type": "年度", "period_basis": "累计",
               "disclosed_at": disclosed, "available_at": disclosed,
               "retrieved_at": str(doc.get("retrieved_at") or "")[:19],
               "currency": "CNY", "caliber": "合并", "restatement": "",
               "source": "official_disclosure_cache",
               "source_url": str(doc.get("url") or ""),
               "material_ids": [case["material_id"]], "projection_version": LEDGER_VERSION,
               "dimension": "公司合计"}
    for year in years:
        period = f"{year}-12-31"
        pstart = f"{year}-01-01"
        for metric, label, group in want:
            f = by.get((metric, f"{year}年"))
            if f is None:
                # 本表缺该行（如洋河无信用减值调节项）：缺行如实记，不造数、不当零摊平
                ledger["missing_items"].setdefault(f"{year}年", []).append(metric)
                continue
            row = {**commons, "period": period, "period_end": period, "period_start": pstart,
                   "unit": "元", metric: float(_d(f["value"])),
                   "metric_label": label,
                   "quote": str(f.get("quote") or f.get("source_line") or ""),
                   "locator": str(f.get("source_locator") or f.get("locator") or ""),
                   "table_name": "现金流量表补充资料（将净利润调节为经营活动现金流量）",
                   "sign_convention": str(f.get("sign_convention") or ""),
                   "orig_fact_ids": [f"cashflow_supplement:{metric}:{year}年"],
                   "supplement_group": group, "cash_row": len(rows) + 1}
            rows.append(row)
            ledger["rows"].append({"period": period, "metric": metric, "label": label,
                                   "group": group, "value": _q(f["value"]), "unit": "元",
                                   "quote": row["quote"], "locator": row["locator"],
                                   "page": f.get("page"), "unit_raw": f.get("unit_raw"),
                                   "orig_fact_ids": row["orig_fact_ids"]})
        # 两期闭合（独立 Decimal 复算，不用算子）：合并净利润 + Σ已披露带符号调节项 = 经营现金流
        year_vals = {m: _d(by[(m, f"{year}年")]["value"]) for m, _l, _g in want
                     if (m, f"{year}年") in by}
        ledger["closure"][f"{year}"] = {"items": len(year_vals),
                                        "sum_adjustments": _q(sum(year_vals.values(), Decimal("0")))}
    # 跨材料交叉核对：净利润/经营现金流两行**不重复出事实**（基础投影里已有，同一材料、
    # 同一 available_at），只核对逐位一致；表的原始单位由 unit_raw 给定，核对时同步换算。
    for metric in ("net_profit_consolidated", "operating_cashflow"):
        cross = next((c for c in (res.get("cross_checks") or []) if c["metric"] == metric), None)
        if cross is None:
            ledger["cross_check"][metric] = {"status": "no_cross_check"}
            continue
        out = {}
        for year, val in ((years[0], cross.get("cur")), (years[1], cross.get("prev"))):
            base = next((r for r in base_rows if r.get("code") == entity_id
                         and str(r.get("period") or "") == f"{year}-12-31"
                         and r.get(metric) is not None), None)
            if base is None:
                out[f"{year}"] = {"status": "no_base_row"}
                continue
            conv = _d(val) * factor
            gap = _d(base[metric]) - conv
            out[f"{year}"] = {"base": _q(base[metric]), "table_raw": _q(val),
                              "table_converted": _q(conv), "gap_yuan": _q(gap),
                              "consistent": abs(gap) <= Decimal("0.01"),
                              "base_available_at": base.get("available_at"),
                              "base_material": (base.get("material_ids") or [None])[0]}
        ledger["cross_check"][metric] = out
    ledger["closure_residual"] = {}
    for year in years:
        cc = {c["metric"]: c for c in (res.get("cross_checks") or [])}
        np_c = _d(cc["net_profit_consolidated"]["cur" if year == years[0] else "prev"]) * factor
        cf_c = _d(cc["operating_cashflow"]["cur" if year == years[0] else "prev"]) * factor
        adj = _d(ledger["closure"][f"{year}"]["sum_adjustments"])
        ledger["closure_residual"][f"{year}"] = {"np": _q(np_c), "ocf": _q(cf_c),
                                                 "sum_adjustments": _q(adj),
                                                 "residual": _q((cf_c - np_c) - adj)}
    return res, rows, ledger


def build_spec(case: dict, parent: dict, *, parent_study_dir: Path) -> dict:
    """父候选 spec 为底：只改名字与（三一案的）主体范围——截止/网格/参数逐字保留。

    另外把**父卡指纹**（card_hash／base_fingerprint）显式登记进 spec.metadata：
    本候选因新增输入行而 source_hash 是文件级 hash（wind_import: raw_hash = sha256(整个
    financials.json)），卡片身份与 base_fingerprint 都会变——不称跨候选 hash 不变，
    继承的是主数值与假设，父卡指纹只作继承来源登记。
    """
    spec = json.loads(json.dumps(parent, ensure_ascii=False))
    if str(spec.get("feature_cutoff")) != case["feature_cutoff"]:
        raise SystemExit(f"父候选特征截止 {spec.get('feature_cutoff')} 与本案 "
                         f"{case['feature_cutoff']} 不一致：停止（不移动主截止）")
    spec["name"] = case["name"]
    pq = json.loads((parent_study_dir / "q1.json").read_text(encoding="utf-8"))
    pc = pq["cards"][case["entity_id"]]
    spec["financial"]["metadata"]["c2e_parent"] = {
        "study_id": BASE_STUDY, "entity_id": case["entity_id"],
        "card_hash": pc.get("card_hash"),
        "base_fingerprint": (pc.get("identity") or {}).get("base_fingerprint"),
        "feature_cutoff": parent.get("feature_cutoff"),
        "inheritance": ("主数值与假设逐字继承（readings／formula／九点格／目标与基期／"
                        "g/m/B 与 10+10）；本候选因新增输入行而 source_hash（文件级）"
                        "与候选哈希不同——不称跨候选 hash 不变；父卡指纹如上登记，"
                        "仅作继承来源，不是同一哈希")}
    if case["entity_id"] != "002304.SZ":
        # 单主体研究：只留本主体卡与网格（两者的**值**逐字保留，见 main 的继承自检）
        spec["q1"]["companies"] = {case["entity_id"]: spec["q1"]["companies"][case["entity_id"]]}
        spec["q1"]["provenance_note"] = (
            f"{case['entity']}卡生成时 {case['periods'][0]} 年报材料已公开；"
            "是历史重建不是事前预测")
        spec["financial"]["joint_grid"]["grids"] = {
            case["entity_id"]: spec["financial"]["joint_grid"]["grids"][case["entity_id"]]}
        mats = spec["financial"]["metadata"].get("materials") or []
        keep = {case["material_id"]}
        spec["financial"]["metadata"]["materials"] = [m for m in mats
                                                      if str(m.get("material_id")) in keep]
    return spec


def write_inputs(case: dict, rows: list[dict], ledger: dict, spec: dict, *,
                 base_rows: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = case["expect_items"]
    payload = {"metadata": {"source": "official_disclosure_cache", "synthetic": False,
                            "projection_version": "q1_projection/1.0.0",
                            "r3_projection_version": "r3_operating_projection/1.0.0",
                            "cash_projection_version": LEDGER_VERSION,
                            "cash_note": (f"现金流量表补充资料 {n} 项×2 年由 "
                                          "extract_cashflow_supplement 投影，见 cash_ledger.json；"
                                          f"材料 {case['material_id']}（{case['disclosed']}）"),
                            "c2e_case": case["entity_id"],
                            "c2e_inherit": ("主特征截止与网格/参数逐字继承父候选 d93e5c4a；"
                                            "新增输入只进续页时点数据集"
                                            if case["cutoff_relation"] == "after" else
                                            f"新研究：主体 {case['entity_id']} 单主体；"
                                            "主特征截止 2025-04-30；补充资料在截止内可见")},
               "rows": list(base_rows) + list(rows)}
    (out_dir / "financials.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "study.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    (out_dir / "cash_ledger.json").write_text(json.dumps(ledger, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    for name in ("r3_ledger.json", "projection_ledger.json"):
        src = BASE_INPUT_DIR / name
        if src.is_file():
            (out_dir / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return out_dir


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, choices=sorted(CASES))
    ap.add_argument("--run", action="store_true", help="写输入目录并跑一次研究")
    ap.add_argument("--out-input-dir", default="")
    ap.add_argument("--out-study-dir", default=str(DEFAULT_OUT_STUDY_DIR))
    a = ap.parse_args(argv)

    case = CASES[a.case]
    study_dir = Path(a.out_study_dir) / BASE_STUDY
    parent = json.loads((study_dir / "inputs.json").read_text(encoding="utf-8"))["spec"]
    base_rows = load_base(case)
    spec = build_spec(case, parent, parent_study_dir=study_dir)
    res, rows, ledger = extract_cash(case, base_rows)
    n = case["expect_items"]
    years = case["periods"]

    bad: list[str] = []
    if len(rows) != n * 2:
        bad.append(f"投影行 {len(rows)}≠{n * 2}")
    for year in years:
        got = ledger["closure"][f"{year}"]["items"]
        if got != n:
            bad.append(f"{year}年调节项 {got}≠{n}")
        if ledger["closure_residual"].get(f"{year}", {}).get("residual") != "0.00":
            bad.append(f"{year}年闭合残差非零")
    for metric, chk in ledger["cross_check"].items():
        if not isinstance(chk, dict) or "consistent" not in json.dumps(chk):
            continue
        for year, c in chk.items():
            if isinstance(c, dict) and c.get("consistent") is False:
                bad.append(f"{metric} {year} 交叉核对不一致")
    # 截止关系：披露日晚于主截止 ⇒ 只进续页（不进主运行）；早于 ⇒ 进主运行
    disclosed, cutoff = case["disclosed"], case["feature_cutoff"]
    rel_ok = (disclosed > cutoff) if case["cutoff_relation"] == "after" else (disclosed <= cutoff)
    if not rel_ok:
        bad.append(f"披露 {disclosed} 与主截止 {cutoff} 的关系与案声明 "
                   f"{case['cutoff_relation']} 不符")
    if any(str(r.get("period") or "") == f"{years[0] + 1}-12-31" for r in rows):
        bad.append("出现目标期下一年的行：不造未披露数")
    # 继承自检：洋河案 spec 除名字与父指纹登记外与父候选逐字一致；三一案单主体块逐字一致
    if case["cutoff_relation"] == "after":
        _a = json.loads(json.dumps(parent, ensure_ascii=False))
        _a["name"] = spec["name"]
        _a["financial"]["metadata"]["c2e_parent"] = spec["financial"]["metadata"]["c2e_parent"]
        if json.dumps(_a, ensure_ascii=False, sort_keys=True) != \
                json.dumps(spec, ensure_ascii=False, sort_keys=True):
            bad.append("时点修正案除名字/父指纹登记外与父候选 spec 不一致（不得移动主截止）")
    else:
        for path in (("q1", "companies"), ("financial", "joint_grid", "grids")):
            got = spec
            ref = parent
            for k in path:
                got, ref = got[k], ref[k]
            if json.dumps(got.get(case["entity_id"]), ensure_ascii=False, sort_keys=True) != \
                    json.dumps(ref[case["entity_id"]], ensure_ascii=False, sort_keys=True):
                bad.append(f"单主体案 {'.'.join(path)}[{case['entity_id']}] 与父候选不一致")

    print(f"== C2e 案 {a.case}：{case['entity']} {case['periods'][0]}/{case['periods'][1]} ==")
    print(f"基础行 {len(base_rows)} ＋ 补充资料行 {len(rows)} ＝ {len(base_rows) + len(rows)}")
    print(f"材料 {case['material_id']} 披露 {disclosed} 页 {ledger['pages']} "
          f"表头单位 {ledger['unit_raw']}（×{ledger['unit_factor']} → 元）")
    print(f"text_sha256 {str(ledger['text_sha256'])[:16]}…  "
          f"缺失项（如实记，不造数）：{json.dumps(ledger['missing_items'], ensure_ascii=False)}")
    for year in years:
        r = ledger["closure_residual"][f"{year}"]
        print(f"{year} 闭合（独立复算）：NP {r['np']} + Σ调节 {r['sum_adjustments']} "
              f"− OCF {r['ocf']} = 残差 {r['residual']}")
    print(f"交叉核对：{json.dumps(ledger['cross_check'], ensure_ascii=False)[:420]}")
    print(f"spec 名字：{spec['name']}")
    if bad:
        print(f"自检未过（{bad}）：停止，不写输入")
        return 2
    if not a.run:
        print("（未写盘；加 --run 才会写输入目录并跑新研究）")
        return 0
    out = write_inputs(case, rows, ledger, spec, base_rows=base_rows,
                       out_dir=Path(a.out_input_dir or case["out_input_dir"]))
    print(f"输入目录 {out}")
    from quant_research import study as qstudy
    result = qstudy.run_study(spec, input_dir=out)
    target = qstudy.write_study(result, output_dir=Path(a.out_study_dir))
    mb = (result.get("quant") or {}).get("market_binding") or {}
    print(f"研究ID {result['study_id']}")
    print(f"研究目录 {target}")
    print(f"来源绑定 status={mb.get('status')} 行情来源={mb.get('price_dataset_id_source')} "
          f"日历来源={mb.get('calendar_id_source')}")
    print("RESULT_JSON " + json.dumps({"case": a.case, "study_id": result["study_id"],
                                       "result_hash": result["result_hash"],
                                       "dir": str(target)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
