#!/usr/bin/env python3
"""C2d：洋河 2025 利润→现金双视角（现金流量表补充资料 16 项×2 年入研究）。

架构师 §18.15（2026-10-11）：
  - ee4429ee（2025 年报，披露 2026-04-28）第 141–142 页现金流量表补充资料已视觉核对；
  - 现役 `adapters/cashflow_supplement_tables.py` → `facts` → `cash_reconciliation`
    已实跑两期/同比闭合（residual 0.00，见 docs/evidence/20261011-c2c-accept/cash_feasibility.json）；
  - 本脚本沿**既有投影**登记：抽取器 32 条事实 → 16 项×2 年 wind_import 行
    （available_at=2026-04-28，单位元，合并口径，公司合计），**新输入/新研究身份**，
    旧主卡不回填；合并净利润/经营现金流两行不重复出事实（已在基础投影里，
    available_at 同为 2026-04-28），只作**逐位交叉核对**；
  - spec 以 C2c 候选（d93e5c4a…，只读）为底：改名、特征截止提到披露日 2026-04-28
    （2025 年报数据自披露日起可用），来源钉住逐字保留；
  - 数字一律从 ModelRun 来，本脚本不写死任何参考数；自检不过就不写盘、不跑。

用法：
    python scripts/c2d_cash_study.py                 # 只打印投影行与自检
    python scripts/c2d_cash_study.py --run           # 写输入目录并跑一次新研究
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

BASE_INPUT_DIR = ROOT / "tmp" / "c2c-inputs"               # C2c 已投影的输入（只读）
BASE_STUDY = "d93e5c4a07e0fcbd"                            # C2c 候选（只读）
OUT_INPUT_DIR = ROOT / "tmp" / "c2d-inputs"
DEFAULT_OUT_STUDY_DIR = ROOT / "tmp" / "research-studies"
MATERIAL_ID = "ee4429eecac66f50"
DOC_PATH = (ROOT / "evals" / "a2_official_chain_20260929" / "002304-2025" /
            "project" / "materials" / MATERIAL_ID / "doc.json")
ENTITY_ID, ENTITY = "002304.SZ", "洋河股份"
LEDGER_VERSION = "cash_supplement_projection/1.0.0"
PERIODS = (2025, 2024)                                     # 本期 / 上期（补充资料两列）
FEATURE_CUTOFF = "2026-04-28"                              # 2025 年报披露日：此后数据方可入研究


def _d(value) -> Decimal:
    return Decimal(str(value))


def _q(value) -> str:
    return str(_d(value).quantize(Decimal("0.01")))


def load_base() -> tuple[list[dict], dict]:
    """C2c 已投影的行（只读复用：B1 基础行＋C2c 量价/分产品行）。"""
    rows = json.loads((BASE_INPUT_DIR / "financials.json").read_text(encoding="utf-8"))["rows"]
    return rows, BASE_INPUT_DIR


def extract_cash() -> tuple[dict, list[dict], dict]:
    """既有抽取器 → 补充资料 32 条事实（16 项×2 年）＋ 台账（含两期/同比闭合自检）。"""
    from adapters.cashflow_supplement_tables import extract_cashflow_supplement
    from financial_analysis.operators.cash_reconciliation import ADJUSTMENT_ITEMS, GROUP_ORDER

    doc = json.loads(DOC_PATH.read_text(encoding="utf-8"))
    res = extract_cashflow_supplement(doc, periods=(f"{PERIODS[0]}-12-31", f"{PERIODS[1]}-12-31"))
    if not res.get("ok"):
        raise SystemExit(f"补充资料未取得（{res.get('reason') or '未识别'}）：不投影、不编数")
    facts = res.get("facts") or []
    disclosed = str(doc.get("disclosed_at") or doc.get("operator_disclosed_at") or "")
    if disclosed != FEATURE_CUTOFF:
        raise SystemExit(f"披露日 {disclosed} 与特征截止 {FEATURE_CUTOFF} 不一致：停止，不投影")
    by = {(f["metric"], f["period"]): f for f in facts}
    # 只登记**算子要求**的项：多则可能混入非调节表口径，缺则如实记缺口（不造数）
    want = [(m, l, g) for m, l, g in ADJUSTMENT_ITEMS]
    got_metrics = sorted({f["metric"] for f in facts})
    extra = [m for m in got_metrics if m not in {w[0] for w in want}]
    if extra:
        raise SystemExit(f"抽取到算子之外的项（{'、'.join(extra)}）：不投影")
    rows: list[dict] = []
    ledger: dict = {"schema": "weavemind.cash_supplement_projection/1", "version": LEDGER_VERSION,
                    "extractor": "adapters.cashflow_supplement_tables.extract_cashflow_supplement",
                    "material_id": MATERIAL_ID, "doc_path": str(DOC_PATH.relative_to(ROOT)),
                    "raw_sha256": str(doc.get("raw_sha256") or ""),
                    "text_sha256": str(res.get("text_hash") or ""),
                    "title": str(doc.get("title") or ""),
                    "disclosed_at": disclosed, "pages": [141, 142],
                    "periods": list(res.get("periods") or []),
                    "unit": "元", "caliber": "合并", "start_caliber": "consolidated net_profit",
                    "sign_convention": "照抄披露符号（表内已注明“增加/减少以“－”号填列”）",
                    "rows": [], "closure": {}, "cross_check": {}, "missing_items": {}}
    commons = {"code": ENTITY_ID, "entity": ENTITY, "period_type": "年度", "period_basis": "累计",
               "disclosed_at": disclosed, "available_at": disclosed,
               "retrieved_at": str(doc.get("retrieved_at") or "")[:19],
               "currency": "CNY", "caliber": "合并", "restatement": "",
               "source": "official_disclosure_cache",
               "source_url": str(doc.get("url") or ""),
               "material_ids": [MATERIAL_ID], "projection_version": LEDGER_VERSION,
               "dimension": "公司合计"}
    for year in PERIODS:
        period = f"{year}-12-31"
        pstart = f"{year}-01-01"
        for metric, label, group in want:
            f = by.get((metric, f"{year}年"))
            if f is None:
                # 洋河本表没有该行（如信用减值调节项）：缺行如实记，不造数、不当零摊平
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
                                   "page": f.get("page"),
                                   "orig_fact_ids": row["orig_fact_ids"]})
        # 两期闭合（独立 Decimal 复算，不用算子）：合并净利润 + Σ已披露带符号调节项 = 经营现金流
        np_key, cf_key = "net_profit_consolidated", "operating_cashflow"
        year_vals = {m: _d(by[(m, f"{year}年")]["value"]) for m, _l, _g in want
                     if (m, f"{year}年") in by}
        ledger["closure"][f"{year}"] = {"items": len(year_vals),
                                        "sum_adjustments": _q(sum(year_vals.values(), Decimal("0")))}
    # 跨材料交叉核对：净利润/经营现金流两行**不重复出事实**（基础投影里已有，
    # 同一材料 ee4429ee、同一 available_at），只核对逐位一致
    base_rows = load_base()[0]
    for metric in ("net_profit_consolidated", "operating_cashflow"):
        cross = next((c for c in (res.get("cross_checks") or []) if c["metric"] == metric), None)
        if cross is None:
            ledger["cross_check"][metric] = {"status": "no_cross_check"}
            continue
        out = {}
        for year, val in ((PERIODS[0], cross.get("cur")), (PERIODS[1], cross.get("prev"))):
            base = next((r for r in base_rows if r.get("code") == ENTITY_ID
                         and str(r.get("period") or "") == f"{year}-12-31"
                         and r.get(metric) is not None), None)
            if base is None:
                out[f"{year}"] = {"status": "no_base_row"}
                continue
            gap = _d(base[metric]) - _d(val)
            out[f"{year}"] = {"base": _q(base[metric]), "extracted": _q(val), "gap_yuan": _q(gap),
                              "consistent": abs(gap) <= Decimal("0.01"),
                              "base_available_at": base.get("available_at"),
                              "base_material": (base.get("material_ids") or [None])[0]}
        ledger["cross_check"][metric] = out
    return res, rows, ledger


def build_spec(base: dict) -> dict:
    """以 C2c 候选 spec 为底：改名＋特征截止提到披露日，其余字段（来源钉住、网格）逐字保留。"""
    spec = json.loads(json.dumps(base, ensure_ascii=False))
    spec["name"] = ("C2d：洋河 2025 利润→现金双视角（官方缓存 2025 年报现金流量表补充资料"
                    "16 项×2 年入研究；特征截止 2026-04-28；旧主卡不回填）")
    spec["feature_cutoff"] = FEATURE_CUTOFF
    return spec


def write_inputs(rows: list[dict], ledger: dict, spec: dict, *, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    base_rows = json.loads((BASE_INPUT_DIR / "financials.json").read_text(encoding="utf-8"))["rows"]
    payload = {"metadata": {"source": "official_disclosure_cache", "synthetic": False,
                            "projection_version": "q1_projection/1.0.0",
                            "r3_projection_version": "r3_operating_projection/1.0.0",
                            "cash_projection_version": LEDGER_VERSION,
                            "cash_note": ("现金流量表补充资料 16 项×2 年由 "
                                          "extract_cashflow_supplement 投影，见 cash_ledger.json") },
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
    ap.add_argument("--run", action="store_true", help="写输入目录并跑一次新研究")
    ap.add_argument("--out-input-dir", default=str(OUT_INPUT_DIR))
    ap.add_argument("--out-study-dir", default=str(DEFAULT_OUT_STUDY_DIR))
    a = ap.parse_args(argv)

    base_rows, _ = load_base()
    frozen = json.loads((Path(a.out_study_dir) / BASE_STUDY / "inputs.json")
                        .read_text(encoding="utf-8"))
    spec = build_spec(frozen["spec"])
    res, rows, ledger = extract_cash()
    # 自检门：两期闭合残差（独立复算）必须为 0；交叉核对必须逐位一致
    bad = []
    for year in PERIODS:
        items = ledger["closure"][f"{year}"]["items"]
        if items != 16:
            bad.append(f"{year}年调节项 {items}≠16")
    for metric, chk in ledger["cross_check"].items():
        for year, c in chk.items():
            if c.get("consistent") is False:
                bad.append(f"{metric} {year} 交叉核对不一致")
    np_c = _d(next(c for c in (res.get("cross_checks") or [])
                   if c["metric"] == "net_profit_consolidated")["cur"])
    cf_c = _d(next(c for c in (res.get("cross_checks") or [])
                   if c["metric"] == "operating_cashflow")["cur"])
    adj_c = _d(ledger["closure"][f"{PERIODS[0]}"]["sum_adjustments"])
    resid = (cf_c - np_c) - adj_c
    print(f"底稿行 {len(base_rows)} ＋ 补充资料行 {len(rows)} ＝ {len(base_rows) + len(rows)}")
    print(f"两期闭合（独立复算）：NP{_q(np_c)} + Σ调节{_q(adj_c)} − OCF{_q(cf_c)} = 残差 {_q(resid)}")
    print(f"缺失项（如实记，不造数）：{json.dumps(ledger['missing_items'], ensure_ascii=False)}")
    print(f"交叉核对：{json.dumps(ledger['cross_check'], ensure_ascii=False)[:400]}")
    print(f"材料 {MATERIAL_ID} text_sha256 {str(ledger['text_sha256'])[:16]}… 披露 {ledger['disclosed_at']}")
    print(f"spec 名字：{spec['name']}")
    if bad or _q(resid) != "0.00":
        print(f"自检未过（{bad or ['两期闭合残差非零']}）：停止，不写输入")
        return 2
    if not a.run:
        print("（未写盘；加 --run 才会写输入目录并跑新研究）")
        return 0
    out = write_inputs(rows, ledger, spec, out_dir=Path(a.out_input_dir))
    print(f"输入目录 {out}")
    from quant_research import study as qstudy
    result = qstudy.run_study(spec, input_dir=out)
    target = qstudy.write_study(result, output_dir=Path(a.out_study_dir))
    mb = (result.get("quant") or {}).get("market_binding") or {}
    print(f"研究ID {result['study_id']}")
    print(f"result_hash {result['result_hash']}")
    print(f"目录 {target}")
    print(f"来源绑定 status={mb.get('status')} 行情来源={mb.get('price_dataset_id_source')} "
          f"日历来源={mb.get('calendar_id_source')}")
    print("RESULT_JSON " + json.dumps({"study_id": result["study_id"],
                                       "result_hash": result["result_hash"],
                                       "dir": str(target)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
