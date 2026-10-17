#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B1（§17.9）：把**已有官方缓存年报**一次显式投影成 study 可读输入，跑 Q1-v1 真实读数。

这不是第二套导入器：解析仍由现役 `adapters.annual_financial_tables.extract` 完成，本脚本只
把**明确选定的四份材料**里已抽取的事实**投影**成原预览器（`wind_import.preview_file`）可读的
行，再沿原 `study.run_study → write_study → verify/replay` 链计算；不重新让 LLM 读整份年报，
不新造 PDF 抓取/解析器，不写库、不联网、不调用模型。

投影台账（`projection_ledger.json`）逐项保留：原 fact_id、主体、期间、口径、单位与换算因子、
来源材料 id/URL/text_sha256、PDF 定位（locator/表名/原句）、公开与采集时点、投影后的值与
跨材料一致性。单位**不二次换算**（三一原表千元、现役抽取已转元，再乘 1000 就是错）。

用法：
    python scripts/q1_real_study.py --out tmp/research-studies --evidence docs/evidence/20261009-q1-real
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters import annual_financial_tables as aft          # noqa: E402
from quant_research import evaluate_mechanisms as EV         # noqa: E402
from quant_research import mechanisms as M                   # noqa: E402
from quant_research import study as STUDY                    # noqa: E402

EVAL_DIR = ROOT / "evals" / "a2_official_chain_20260929"
PROJECTION_VERSION = "q1_projection/1.0.0"
SOURCE_LABEL = "official_disclosure_cache"

# §17.9 明确选择的四份原件（不遍历缓存猜公司）
MATERIALS = [
    {"key": "002304-2023", "company": "洋河股份", "code": "002304.SZ", "report_year": 2023,
     "doc": "002304-2023/project/materials/77185ce8ec83f3bc/doc.json",
     "material_id": "77185ce8ec83f3bc", "disclosed_at": "2024-04-27"},
    {"key": "002304-2024", "company": "洋河股份", "code": "002304.SZ", "report_year": 2024,
     "doc": "002304/project/materials/f42e747c73850b59/doc.json",
     "material_id": "f42e747c73850b59", "disclosed_at": "2025-04-29"},
    {"key": "002304-2025", "company": "洋河股份", "code": "002304.SZ", "report_year": 2025,
     "doc": "002304-2025/project/materials/ee4429eecac66f50/doc.json",
     "material_id": "ee4429eecac66f50", "disclosed_at": "2026-04-28"},
    {"key": "600031-2024", "company": "三一重工", "code": "600031.SH", "report_year": 2024,
     "doc": "600031/project/materials/0c3e82056e977adc/doc.json",
     "material_id": "0c3e82056e977adc", "disclosed_at": "2025-04-18"},
]

PROJECTED = ["revenue", "operating_cost", "gross_profit", "selling_expense", "admin_expense",
             "rd_expense", "taxes_and_surcharges", "net_profit", "net_profit_consolidated",
             "operating_cashflow", "accounts_receivable", "inventory", "accounts_payable",
             "total_assets", "total_liabilities"]
STATEMENT_TABLES = ("合并利润表", "合并现金流量表", "合并资产负债表")
_SCALE_BY_UNIT = {"元": 1, "千元": 1000, "万元": 10000, "百万元": 1000000}
_NUM_RE = re.compile(r"-?\d[\d,，]*(?:\.\d+)?")


def _d(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:                                        # noqa: BLE001
        return None


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      default=str).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def extract_materials() -> list[dict]:
    """四份材料 → 现役抽取事实（只保留合并口径）。"""
    out = []
    for spec in MATERIALS:
        path = EVAL_DIR / spec["doc"]
        doc = json.loads(path.read_text(encoding="utf-8"))
        raw = aft.extract(doc, company=spec["company"], company_code=spec["code"])
        facts = list(raw["facts"]) + aft.derive_gross_profit(raw["facts"], raw["rejected"])
        out.append({**spec, "text_sha256": raw["text_hash"],
                    "entity_state": raw.get("entity_state"),
                    "rejected": raw["rejected"], "facts": facts,
                    "raw_sha256": doc.get("raw_sha256"), "url": doc.get("url"),
                    "title": doc.get("title"), "retrieved_at": doc.get("retrieved_at"),
                    "doc_path": str(path.relative_to(ROOT)).replace("\\", "/")})
    return out


def _table_rank(fact: dict) -> int:
    """同值多表出现时优先**具体报表**（合并利润表/现金流量表/资产负债表）而非摘要表。"""
    name = str(fact.get("table_name") or fact.get("table") or "")
    return 0 if name in STATEMENT_TABLES else 1


def _printed_scale(fact: dict) -> int:
    m = re.search(r"单位[:：]\s*(元|千元|万元|百万元)", str(fact.get("unit_source") or ""))
    return _SCALE_BY_UNIT.get(m.group(1), 0) if m else 0


def unit_check(fact: dict) -> dict:
    """单位账：原句里的印刷数 × 表头单位因子 必须与抽取值相容（防二次换算/漏换算）。"""
    scale = _printed_scale(fact)
    tokens = [n.replace(",", "").replace("，", "")
              for n in _NUM_RE.findall(str(fact.get("quote") or ""))]
    value = _d(fact.get("value"))
    ok, hit = False, None
    if scale and value is not None and tokens:
        for token in tokens:
            d = _d(token)
            if d is not None and d * scale == value:
                ok, hit = True, token
                break
    return {"unit": fact.get("unit"), "printed_unit": f"×{scale}" if scale else "未标注",
            "printed_number": hit, "factor_applied_by_extractor": scale,
            "double_conversion": False, "ok": ok}


def select_primary(materials: list[dict]) -> dict:
    """(主体, 指标, 年度) → 选定事实 + 同值替代（跨材料一致性另行核对）。"""
    buckets: dict[tuple, list[dict]] = {}
    for mat in materials:
        for fact in mat["facts"]:
            if str(fact.get("caliber")) != "合并" or fact.get("metric") not in PROJECTED:
                continue
            year = re.search(r"(19|20)\d{2}", str(fact.get("period") or ""))
            if not year:
                continue
            buckets.setdefault((mat["code"], fact["metric"], year.group(0)), []).append(
                {**fact, "_material": mat})

    ledgers: dict[tuple, dict] = {}
    for key, candidates in buckets.items():
        code, metric, year = key

        def rank(item):
            mat = item["_material"]
            same_year = 0 if mat["report_year"] == int(year) else 1
            return (_table_rank(item), same_year, mat["report_year"])

        ordered = sorted(candidates, key=rank)
        chosen = ordered[0]
        alts = [{"material_id": a["_material"]["material_id"],
                 "report_year": a["_material"]["report_year"],
                 "value": a.get("value"), "table": a.get("table_name") or a.get("table"),
                 "fact_id": a.get("fact_id")}
                for a in ordered[1:]]
        agree = all(_d(a["value"]) == _d(chosen.get("value")) for a in alts)
        ledgers[key] = {"chosen": chosen, "alternatives": alts,
                        "cross_material_agree": agree}
    return ledgers


def build_rows(ledgers: dict, materials: list[dict]) -> tuple[list[dict], dict]:
    """投影 → 原预览器可读的行（一行 = 一个主体一个年度）＋ 逐项台账。"""
    by_entity: dict[tuple, dict] = {}
    ledger_items: list[dict] = []
    for (code, metric, year), entry in sorted(ledgers.items()):
        chosen = entry["chosen"]
        mat = chosen["_material"]
        row = by_entity.setdefault((code, year), {
            "code": code, "entity": mat["company"], "period": f"{year}-12-31",
            "period_type": "年度", "period_basis": "累计",
            "disclosed_at": mat["disclosed_at"], "available_at": mat["disclosed_at"],
            "retrieved_at": str(mat.get("retrieved_at") or "")[:19],
            "unit": "元", "currency": "CNY", "caliber": "合并", "restatement": "",
            "source": SOURCE_LABEL, "source_url": mat.get("url") or "",
            "material_ids": [], "orig_fact_ids": [],
            "projection_version": PROJECTION_VERSION,
        })
        row[metric] = float(chosen["value"])
        row["material_ids"] = sorted(set(row["material_ids"] + [mat["material_id"]]))
        row["orig_fact_ids"] = sorted(set(row["orig_fact_ids"] + [str(chosen.get("fact_id"))]))
        check = unit_check(chosen)
        ledger_items.append({
            "entity_id": code, "metric": metric, "year": year,
            "projected_value": chosen.get("value"), "projected_unit": "元",
            "source_value": chosen.get("value"), "source_unit": chosen.get("unit"),
            "unit_factor": check["factor_applied_by_extractor"],
            "unit_check": check, "not_double_converted": True,
            "orig_fact_id": chosen.get("fact_id"), "material_id": mat["material_id"],
            "material_key": mat["key"], "material_text_sha256": chosen.get("source_hash"),
            "source_url": mat.get("url"), "source_label": SOURCE_LABEL,
            "disclosed_at": mat["disclosed_at"], "retrieved_at": mat.get("retrieved_at"),
            "report_year": mat["report_year"], "basis": chosen.get("caliber"),
            "period": chosen.get("period"), "period_kind": chosen.get("period_kind"),
            "table_name": chosen.get("table_name") or chosen.get("table"),
            "locator": chosen.get("locator"), "quote": chosen.get("quote"),
            "caliber_source": chosen.get("caliber_source"),
            "period_source": chosen.get("period_source"),
            "unit_source": chosen.get("unit_source"),
            "extracted_by": chosen.get("extracted_by"),
            "derived_from": chosen.get("derived_from") or [],
            "formula": chosen.get("formula") or "",
            "alternatives": entry["alternatives"],
            "cross_material_agree": entry["cross_material_agree"],
            "restated": not entry["cross_material_agree"],
        })
    rows = [by_entity[k] for k in sorted(by_entity)]
    ledger = {
        "schema": "weavemind.q1_projection_ledger/1",
        "projection_version": PROJECTION_VERSION,
        "source_label": SOURCE_LABEL,
        "materials": [{"key": m["key"], "material_id": m["material_id"], "title": m["title"],
                       "company": m["company"], "code": m["code"], "url": m["url"],
                       "doc_path": m["doc_path"], "raw_sha256": m["raw_sha256"],
                       "text_sha256": m["text_sha256"], "disclosed_at": m["disclosed_at"],
                       "retrieved_at": m["retrieved_at"], "report_year": m["report_year"],
                       "entity_state": m["entity_state"],
                       "rejected_count": len(m["rejected"])} for m in materials],
        "items": ledger_items,
        "notes": [
            "解析由现役 annual_financial_tables.extract 完成；本投影**不重新解析 PDF**",
            "三一原表千元、抽取事实已转元：unit_factor=1000 只作为**核对**记录，投影不再乘",
            "同一 (主体, 指标, 年度) 的选定规则：具体报表 > 摘要表，本年度报告 > 比较列年度报告",
            "跨材料同值核对记录在 alternatives/ cross_material_agree；不一致即 restated=true",
            "synthetic=false；来源为官方披露缓存（official_disclosure_cache），不冒称 Wind",
        ],
        "unresolved": [{"code": m["code"], "material_id": m["material_id"],
                        "rejected": m["rejected"][:12]} for m in materials
                       if m["rejected"]],
    }
    return rows, ledger


def build_spec(ledger: dict, *, cutoff: str, as_of: str, name: str) -> dict:
    return {
        "schema": STUDY.SPEC_SCHEMA,
        "name": name,
        "synthetic": False,
        "feature_cutoff": cutoff,
        "evaluation_as_of": as_of,
        "financial": {
            "file": "financials.json",
            "metadata": {
                "source": SOURCE_LABEL,
                "synthetic": False,
                "license": "official_public_disclosure",
                "source_note": "巨潮官方披露缓存原件；不冒称 Wind 导出",
                "projection_version": PROJECTION_VERSION,
                "projection_note": "现役官方年报表抽取 → 原预览器可读行的显式投影",
                "materials": [{"material_id": m["material_id"], "url": m["url"],
                               "text_sha256": m["text_sha256"],
                               "disclosed_at": m["disclosed_at"]}
                              for m in ledger["materials"]],
            },
            "periods": [2022, 2023, 2024, 2025],
            "models": ["operating_drivers", "cash_reconciliation", "scenario_sensitivity",
                       "cash_pressure", "cash_quality"],
            "scenario_params": {"revenue_growth": 0.0, "gross_margin_delta": 0.0},
            "params": {"cash_pressure": {"collection_days_delta": 10,
                                         "inventory_days_delta": 10}},
        },
    }


def write_inputs(target: Path, rows: list[dict], ledger: dict, spec: dict) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / "financials.json").write_bytes(_json_bytes(
        {"metadata": {"source": SOURCE_LABEL, "synthetic": False,
                      "projection_version": PROJECTION_VERSION}, "rows": rows}))
    (target / "study.json").write_bytes(_json_bytes(spec))
    (target / "projection_ledger.json").write_bytes(_json_bytes(ledger))


# ── 读数与卡（复用 study 的冻结产物，不重算财务模型）──────────────────────────

def _run_output(runs: dict, model: str, metric: str):
    for out in (runs.get(model) or {}).get("outputs") or []:
        if out.get("metric") == metric:
            return out
    return None


def _target_source(runs: dict) -> str:
    out = _run_output(runs, "scenario_sensitivity", "margin_gap_to_threshold_pp")
    if not out:
        return ""
    for note in out.get("assumptions") or ():
        if "目标" in str(note) or "回到" in str(note):
            return str(note)
    return "；".join(list(out.get("assumptions") or ())[:2])


def build_context(study_dir: Path, code: str, *, provenance: str,
                  provenance_note: str, ledger: dict | None = None) -> dict:
    fin = json.loads((study_dir / "financial.json").read_text(encoding="utf-8"))
    risk = json.loads((study_dir / "risk.json").read_text(encoding="utf-8"))
    man = json.loads((study_dir / "study_manifest.json").read_text(encoding="utf-8"))
    row = fin.get(code) or {}
    runs = {str(r.get("model_id")): r for r in (row.get("runs") or [])}
    rr = risk.get(code) or {}
    features = {f.get("name"): f.get("value") for f in (rr.get("features") or [])}
    coverage = _run_output(runs, "cash_quality", "cashflow_coverage")
    diff = _run_output(runs, "cash_quality", "cfo_minus_profit")
    gap = _run_output(runs, "scenario_sensitivity", "margin_gap_to_threshold_pp")
    growth = _run_output(runs, "scenario_sensitivity", "revenue_growth_to_hold_target")
    occupation = _run_output(runs, "cash_pressure", "incremental_capital_occupation")
    reconciliation = runs.get("cash_reconciliation") or {}
    run_period = ""
    for model in ("cash_quality", "scenario_sensitivity", "cash_pressure"):
        outs = (runs.get(model) or {}).get("outputs") or []
        if outs:
            run_period = str(outs[0].get("output_period") or "")
            break
    mats = [m for m in ((ledger or {}).get("materials") or []) if m.get("code") == code]
    mat_years = sorted({str(m.get("report_year")) for m in mats if m.get("report_year")})
    note = provenance_note
    if mat_years and "年报" in note:
        note = note.replace("2023/2024/2025", "、".join(mat_years))
    return {
        "study_id": man.get("study_id"), "result_hash": man.get("result_hash"),
        "run_period": run_period,
        "synthetic": bool(man.get("synthetic")),
        "research_state": man.get("research_state"),
        "human_review": man.get("human_review"),
        "external_delivery": man.get("external_delivery") or {},
        "machine_checks": {},
        "provenance_kind": provenance, "provenance_note": note,
        "known_as_of": {"feature_cutoff": man.get("feature_cutoff"),
                        "evaluation_as_of": man.get("evaluation_as_of")},
        "sources": [{"source_label": SOURCE_LABEL, "material_id": s.get("material_id"),
                     "sha256": s.get("text_sha256"), "disclosed_at": s.get("disclosed_at"),
                     "url": s.get("url")}
                    for s in mats],
        "cash": {"cashflow_coverage": (coverage or {}).get("value"),
                 "cfo_minus_profit": (diff or {}).get("value"),
                 "reconciliation_status": reconciliation.get("status")},
        "risk": {"receivables_to_revenue": features.get("receivables_to_revenue"),
                 "inventory_to_cost": features.get("inventory_to_cost"),
                 "liabilities_to_assets": features.get("liabilities_to_assets"),
                 "anomaly_clues": list(rr.get("anomaly_clues") or [])},
        "target": {"margin_gap_to_threshold_pp": (gap or {}).get("value"),
                   "revenue_growth_to_hold_target": (growth or {}).get("value"),
                   "target_source": _target_source(runs),
                   "incremental_capital_occupation": (occupation or {}).get("value")},
        "run_status": {k: v.get("status") for k, v in runs.items()},
    }


def _primary_year(reading: dict) -> str:
    """主读数年度：**既有 O 又有成熟 Y_op** 的最晚年度（卡因此能被第三年核验到）；
    没有成熟结果时退回"有 O 的最晚年度"（三一只有两期，属此情形）。"""
    both = [c["t"] for c in reading["chains"]
            if c.get("o_t") is not None and c.get("y_state") == "mature"]
    if both:
        return sorted(both)[-1]
    with_o = [c["t"] for c in reading["chains"] if c.get("o_t") is not None]
    return sorted(with_o)[-1] if with_o else (reading["years"][-1] if reading["years"] else "")


def q1_layer(study_dir: Path, *, provenance: str, provenance_note: str,
             ledger: dict | None = None) -> dict:
    """从**冻结产物**算 Q1 读数、组成表与两张卡（不重跑财务模型）。"""
    fin = json.loads((study_dir / "financial.json").read_text(encoding="utf-8"))
    readings, cards, compositions, names = {}, {}, {}, {}
    for code, row in sorted(fin.items()):
        obs = (row.get("dataset") or {}).get("observations") or []
        entity = ""
        for o in obs:
            if o.get("entity"):
                entity = str(o["entity"])
                break
        reading = M.readings(obs, entity_id=code, entity=entity)
        readings[code] = reading
        compositions[code] = {}
        for chain in reading["chains"]:
            comp = M.composition(reading, chain["t"])
            if comp.get("state") == "ok":
                compositions[code][chain["t"]] = comp
        primary = _primary_year(reading)
        ctx = build_context(study_dir, code, provenance=provenance,
                            provenance_note=provenance_note, ledger=ledger)
        card = M.short_card(reading, composition_row=compositions[code].get(primary),
                            context=ctx)
        card["readings"]["primary_t"] = primary
        card["identity"]["input_fact_ids"] = sorted(
            {i.get("fact_id") for i in (compositions[code].get(primary) or {}).get("items") or []
             if i.get("fact_id")})
        cards[code] = card
        names[code] = entity
    return {"readings": readings, "cards": cards, "compositions": compositions,
            "names": names}


def known_at_from_ledger(ledger: dict, code: str) -> dict:
    """年度 → 该年度数字首次**在我方材料集里**可获得的时点（不是报告期末）。"""
    out: dict[str, str] = {}
    for item in ledger["items"]:
        if item["entity_id"] != code:
            continue
        year, when = str(item["year"]), str(item["disclosed_at"])
        out[year] = max(out.get(year, ""), when)
    return out


def evaluate_q1(layer: dict, ledger: dict, *, split_at: str) -> dict:
    cases, windows = [], {}
    for code, reading in layer["readings"].items():
        known = known_at_from_ledger(ledger, code)
        rows = EV.build_cases(reading, known_at=known, window=code)
        cases.extend(rows)
        windows[code] = [{"window": c["case_id"], "years": [c["t"], c["next_year"]]}
                         for c in rows]
    report = EV.evaluate(cases, pool=sorted(layer["readings"]), split_at=split_at,
                         window_note="同一公司的多个年度窗口共享年度，重叠，不是独立样本",
                         entity_windows=windows)
    report["generated_at"] = _now()
    report["evaluation_hash"] = EV.evaluation_hash(report)
    return report


def _rehash_card(card: dict) -> str:
    return hashlib.sha256(
        json.dumps({k: v for k, v in card.items() if k != "generated_at"},
                   ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def attach_verify(layer: dict, verification: dict) -> None:
    """把 verify_study 的**真实**结果填进卡（字节核对 ≠ 事实正确/人工复核）。"""
    for card in layer["cards"].values():
        card["usage_state"]["machine_checks"] = {
            "verify_study": {"ok": bool(verification.get("ok")),
                             "checked": len(verification.get("checked") or []),
                             "failed": list(verification.get("failed") or []),
                             "note": "字节核对只证明产物一致，不证明事实正确/人工复核"}}


def attach_cards(layer: dict, evaluation: dict, ledger: dict) -> None:
    """把"旧卡→实际→支持/削弱/未决"填回卡（历史重建下的核验，不是样本外预测）。"""
    by_case = {c["case_id"]: c for c in evaluation["case_rows"]}
    for code, card in layer["cards"].items():
        case = by_case.get(f"{code}::t={card['readings']['t']}")
        if case is None:
            continue
        follow = EV.follow_up(case, verify_by=case["outcome_at"])
        follow["case_id"] = case["case_id"]
        card["follow_up"] = follow
        card["_card_hash_before_follow_up"] = card["card_hash"]
        card["card_hash"] = _rehash_card(card)


def _write_extra(study_dir: Path, layer: dict, evaluation: dict, ledger: dict) -> dict:
    """把 Q1 层写进同一个 study 目录，并把这些文件纳入 manifest 的 hash 清单。"""
    (study_dir / "mechanisms.json").write_bytes(_json_bytes(
        {"readings": layer["readings"], "compositions": layer["compositions"],
         "cards": layer["cards"]}))
    (study_dir / "mechanism_evaluation.json").write_bytes(_json_bytes(evaluation))
    (study_dir / "projection_ledger.json").write_bytes(_json_bytes(ledger))
    (study_dir / "mechanism_cards.md").write_text(
        M.cards_markdown([layer["cards"][c] for c in sorted(layer["cards"])]),
        encoding="utf-8")
    (study_dir / "mechanism_evaluation.md").write_text(EV.markdown(evaluation),
                                                       encoding="utf-8")
    (study_dir / "research.md").write_text(_research_md(layer, evaluation, ledger),
                                           encoding="utf-8")
    man = json.loads((study_dir / "study_manifest.json").read_text(encoding="utf-8"))
    man["q1"] = {
        "method_version": M.METHOD_VERSION, "eval_version": EV.EVAL_VERSION,
        "module_hashes": {
            "quant_research/mechanisms.py": _sha256(ROOT / "quant_research/mechanisms.py"),
            "quant_research/evaluate_mechanisms.py":
                _sha256(ROOT / "quant_research/evaluate_mechanisms.py"),
        },
        "evaluation_hash": evaluation["evaluation_hash"],
        "card_hashes": {c: layer["cards"][c]["card_hash"] for c in layer["cards"]},
        "primary_t": {c: layer["cards"][c]["readings"].get("primary_t")
                      for c in layer["cards"]},
        "readings_hash": _hash_readings(layer["readings"]),
        "provenance_kind": (layer["cards"][sorted(layer["cards"])[0]]["provenance_kind"]
                            if layer["cards"] else ""),
    }
    man["artifacts"] = {str(p.relative_to(study_dir)).replace("\\", "/"): _sha256(p)
                        for p in sorted(study_dir.rglob("*"))
                        if p.is_file() and p.name != "study_manifest.json"}
    (study_dir / "study_manifest.json").write_bytes(_json_bytes(man))
    return man


def _hash_readings(readings: dict) -> str:
    return hashlib.sha256(json.dumps(readings, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()


def _yi(value) -> str:
    d = _d(value)
    return "—" if d is None else f"{d / Decimal(100000000):,.2f}"


def _pct(value) -> str:
    d = _d(value)
    return "—" if d is None else f"{d * 100:+.2f}%"


def _research_md(layer: dict, evaluation: dict, ledger: dict) -> str:
    lines = ["# Q1-v1 真实经营机制研究（洋河/三一，官方缓存年报）", "",
             "内部研究草稿；人工未复核；**历史重建**（生成时后续年度结果已公开），"
             "不构成事前预测或 alpha 声明。", "",
             f"- 来源：`{SOURCE_LABEL}`；四份原件见 `projection_ledger.json`",
             f"- 方法：`{M.METHOD_VERSION}`；评估：`{EV.EVAL_VERSION}`",
             "- 逐公司组成表与卡见 `mechanism_cards.md`；方向对照见 `mechanism_evaluation.md`",
             "", "## 逐公司读数", ""]
    for code in sorted(layer["readings"]):
        reading = layer["readings"][code]
        lines += [f"### {reading['entity_id']}（{reading['entity'] or '主体名未标'}）", "",
                  "| 年度 | E（亿元） | 收入（亿元） | 归母净利（亿元） | 状态 |",
                  "|---|---:|---:|---:|---|"]
        for year in reading["years"]:
            row = reading["rows"][year]
            lines.append(f"| {year} | {_yi(row['e'])} | {_yi(row['revenue'])} "
                         f"| {_yi(row['net_profit'])} | {row['e_state']} |")
        lines.append("")
        for chain in reading["chains"]:
            if chain.get("o_t") is None and chain.get("y_op") is None:
                continue
            lines.append(f"- t={chain['t']}：O={_pct(chain.get('o_t'))}"
                         f"（{chain['sign_o']}）；Y_op={_pct(chain.get('y_op'))}"
                         f"（{chain['sign_y']}，{chain['y_state']}）")
        lines.append("")
    lines += [EV.markdown(evaluation), ""]
    return "\n".join(lines)


def run_study_once(*, out_root: Path, rows: int, ledger: dict, spec: dict,
                   input_dir: Path, provenance: str, provenance_note: str,
                   split_at: str) -> dict:
    result = STUDY.run_study(spec, input_dir=input_dir)
    study_dir = STUDY.write_study(result, output_dir=out_root)
    layer = q1_layer(study_dir, provenance=provenance, provenance_note=provenance_note,
                     ledger=ledger)
    evaluation = evaluate_q1(layer, ledger, split_at=split_at)
    attach_cards(layer, evaluation, ledger)
    # 两遍：第一遍落盘后拿到 verify 的真实结果，填进卡再落第二遍（hash 清单同步更新）。
    _write_extra(study_dir, layer, evaluation, ledger)
    attach_verify(layer, STUDY.verify_study(study_dir))
    for card in layer["cards"].values():
        card["card_hash"] = _rehash_card(card)
    manifest = _write_extra(study_dir, layer, evaluation, ledger)
    verify = STUDY.verify_study(study_dir)
    replay = STUDY.replay_study(study_dir)
    return {"study_dir": str(study_dir), "study_id": manifest["study_id"],
            "result_hash": manifest.get("result_hash"), "verify": verify, "replay": replay,
            "q1_replay": replay_q1(study_dir), "layer": layer, "evaluation": evaluation,
            "manifest": manifest, "spec": spec, "rows": rows}


def replay_q1(study_dir: Path) -> dict:
    """Q1 层重放：从同一冻结产物重算读数并比 hash（study.replay 不含本层模块）。"""
    man = json.loads((study_dir / "study_manifest.json").read_text(encoding="utf-8"))
    layer = q1_layer(study_dir, provenance="历史重建", provenance_note="replay",
                     ledger=None)
    actual = _hash_readings(layer["readings"])
    expected = (man.get("q1") or {}).get("readings_hash")
    modules_ok = all(_sha256(ROOT / name) == digest
                     for name, digest in ((man.get("q1") or {}).get("module_hashes") or {}).items())
    return {"ok": bool(expected) and actual == expected and modules_ok,
            "expected_hash": expected, "actual_hash": actual,
            "module_hashes_ok": modules_ok,
            "what_this_verifies": "同一冻结产物的 Q1 读数可复算（study.replay 不含本层模块）"}


def excluded_of(run: dict) -> dict:
    payload = json.loads((Path(run["study_dir"]) / "excluded.json").read_text(encoding="utf-8"))
    return payload.get("financial") or {}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "tmp" / "research-studies"))
    parser.add_argument("--inputs", default=str(ROOT / "tmp" / "q1-real-inputs"))
    parser.add_argument("--evidence", default="")
    parser.add_argument("--cutoff", default="2026-10-09")
    parser.add_argument("--split-at", default="2025-04-30")
    args = parser.parse_args(argv)

    out_root = Path(args.out).resolve()
    inputs = Path(args.inputs).resolve()
    materials = extract_materials()
    rows, ledger = build_rows(select_primary(materials), materials)
    ledger["generated_at"] = _now()

    spec = build_spec(ledger, cutoff=args.cutoff, as_of=args.cutoff,
                      name="Q1-v1 真实两公司经营机制研究（官方缓存年报；全量可见）")
    write_inputs(inputs, rows, ledger, spec)
    full = run_study_once(out_root=out_root, rows=len(rows), ledger=ledger, spec=spec,
                          input_dir=inputs, provenance="历史重建",
                          provenance_note=("用已公开的 2023/2024/2025 年报在结果公开**之后**"
                                           "生成：不因人工确认变成事前预测"),
                          split_at=args.split_at)

    # 未来材料反例：把截止退回到 2024-04-27（此时只有洋河 2023 年报可见）
    pit_spec = build_spec(ledger, cutoff="2024-04-27", as_of="2024-04-27",
                          name="Q1-v1 时点对照（截止 2024-04-27：后期材料不得倒灌）")
    pit_inputs = inputs.parent / (inputs.name + "-pit")
    write_inputs(pit_inputs, rows, ledger, pit_spec)
    pit = run_study_once(out_root=out_root, rows=len(rows), ledger=ledger, spec=pit_spec,
                         input_dir=pit_inputs, provenance="历史重建",
                         provenance_note="时点对照研究：只允许截止前已公开的材料进入特征",
                         split_at=args.split_at)

    summary = {
        "generated_at": _now(), "inputs_dir": str(inputs), "out_root": str(out_root),
        "full": {"study_dir": full["study_dir"], "study_id": full["study_id"],
                 "result_hash": full["result_hash"], "rows": full["rows"],
                 "verify": full["verify"], "replay": full["replay"],
                 "q1_replay": full["q1_replay"],
                 "cards": {c: full["layer"]["cards"][c]["card_hash"]
                           for c in full["layer"]["cards"]},
                 "evaluation": {"verdict": full["evaluation"]["increment"]["verdict"],
                                "maturity": full["evaluation"]["maturity"],
                                "coverage": full["evaluation"]["coverage_denominator"]}},
        "pit": {"study_dir": pit["study_dir"], "study_id": pit["study_id"],
                "result_hash": pit["result_hash"], "verify": pit["verify"],
                "replay": pit["replay"],
                "excluded_financial": excluded_of(pit),
                "readings": {c: {"years": pit["layer"]["readings"][c]["years"],
                                 "chains": [{k: ch.get(k) for k in
                                             ("t", "o_t", "sign_o", "y_op", "y_state")}
                                            for ch in pit["layer"]["readings"][c]["chains"]]}
                             for c in pit["layer"]["readings"]}},
    }
    if args.evidence:
        write_evidence(Path(args.evidence).resolve(), full, pit, rows, ledger, summary)
    print(json.dumps({"full": summary["full"]["study_id"],
                      "pit": summary["pit"]["study_id"],
                      "verify_full": full["verify"]["ok"], "replay_full": full["replay"]["ok"],
                      "q1_replay_full": full["q1_replay"]["ok"],
                      "verify_pit": pit["verify"]["ok"], "replay_pit": pit["replay"]["ok"],
                      "verdict": full["evaluation"]["increment"]["verdict"]},
                     ensure_ascii=False))
    return 0


def write_evidence(evidence: Path, full: dict, pit: dict, rows: list[dict], ledger: dict,
                   summary: dict) -> None:
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "financials_projection.json").write_bytes(_json_bytes(rows))
    (evidence / "projection_ledger.json").write_bytes(_json_bytes(ledger))
    (evidence / "run_summary.json").write_bytes(_json_bytes(summary))
    for tag, run in (("full", full), ("pit", pit)):
        (evidence / f"verify_replay_{tag}.json").write_bytes(_json_bytes(
            {"verify": run["verify"], "replay": run["replay"], "q1_replay": run["q1_replay"],
             "study_dir": run["study_dir"], "study_id": run["study_id"]}))
        (evidence / f"study_manifest_{tag}.json").write_bytes(_json_bytes(run["manifest"]))
    (evidence / "mechanisms.json").write_bytes(_json_bytes(
        {"readings": full["layer"]["readings"], "compositions": full["layer"]["compositions"],
         "cards": full["layer"]["cards"]}))
    (evidence / "mechanism_cards.md").write_text(
        M.cards_markdown([full["layer"]["cards"][c] for c in sorted(full["layer"]["cards"])]),
        encoding="utf-8")
    (evidence / "mechanism_evaluation.json").write_bytes(_json_bytes(full["evaluation"]))
    (evidence / "mechanism_evaluation.md").write_text(EV.markdown(full["evaluation"]),
                                                      encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
