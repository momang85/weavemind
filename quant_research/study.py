"""Offline research entry point: explicit files, existing calculators, frozen inputs.

This is an internal research artifact, not another report/adoption/delivery pipeline.
No LLM, provider SDK, database, Redis or service is required. Missing domains stay
visible; synthetic fixtures never imply strategy performance or calibrated risk.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, time, timezone, timedelta
from decimal import Decimal
from pathlib import Path

SCHEMA = "weavemind.research_study/1"
SPEC_SCHEMA = "weavemind.study_spec/1"
IMPL_VERSION = "study/1.0.0"
CN_TZ = timezone(timedelta(hours=8))
REPO_ROOT = Path(__file__).resolve().parents[1]


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode("utf-8")


def _hash(value) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _moment(value: str) -> datetime:
    """A date-only statement is conservatively available at end of that CN day."""
    s = str(value or "").strip()
    if not s:
        raise ValueError("缺少公开可用时点/截止日期")
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if len(s) == 10:
        dt = datetime.combine(dt.date(), time.max)
    return dt.replace(tzinfo=CN_TZ) if dt.tzinfo is None else dt


def _descriptor(value) -> dict:
    if isinstance(value, str):
        return {"file": value}
    if isinstance(value, dict) and value.get("file"):
        return dict(value)
    raise ValueError("输入项须为文件名或含 file 的对象")


def _input_path(root: Path, name: str) -> Path:
    p = (root / name).resolve()
    if not p.is_relative_to(root.resolve()):
        raise ValueError("输入文件必须位于声明的 input_dir 内")
    if p.suffix.lower() not in {".csv", ".json", ".jsonl"}:
        raise ValueError("本入口接受 CSV/JSON/JSONL；Excel 请另存仅值 CSV")
    if not p.is_file():
        raise ValueError(f"输入文件不存在：{name}")
    return p


def _read_records(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    elif path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines()
                if line.strip()]
    else:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = payload.get("records", payload.get("rows")) if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        raise ValueError("输入必须是记录列表，不自动读取任意 JSON 配置")
    return rows


def _implementations() -> dict:
    # Include the reused calculators, so a changed implementation invalidates a study.
    names = ["quant_research/study.py", "quant_research/joint_grid.py",
             "adapters/wind_import.py",
             "financial_analysis/events.py", "risk_research/features.py",
             "risk_research/readiness.py", "quant_research/event_returns.py",
             "quant_research/backtest.py", "quant_research/publish.py",
             "quant_research/mechanisms.py", "quant_research/evaluate_mechanisms.py"]
    names.extend(str(p.relative_to(REPO_ROOT)).replace("\\", "/")
                 for p in sorted((REPO_ROOT / "financial_analysis").glob("*.py"))
                 if p.name != "events.py")
    names.extend(str(p.relative_to(REPO_ROOT)).replace("\\", "/")
                 for p in sorted((REPO_ROOT / "financial_analysis/operators").glob("*.py")))
    return {n: hashlib.sha256((REPO_ROOT / n).read_bytes()).hexdigest() for n in names}


def _visible(records: list[dict], cutoff: str, *, market: bool = False) -> tuple[list, list]:
    kept, excluded = [], []
    end = _moment(cutoff)
    for i, row in enumerate(records, 1):
        times = [row.get(k) for k in ("available_at", "version_available_at") if row.get(k)]
        if not market and row.get("disclosed_at"):
            times.append(row["disclosed_at"])
        reason = ""
        try:
            if not times:
                reason = "没有公开可用时点依据"
            elif max(_moment(t) for t in times) > end:
                reason = "信息在本次截止之后才可用"
            elif market and _moment(str(row.get("date") or "")) > end:
                reason = "行情日期在评价截止之后"
            elif not row.get("analysis_eligible", False):
                reason = "导入预览尚缺计算所需身份或口径"
        except (TypeError, ValueError):
            reason = "可用时点/日期格式无效"
        if reason:
            excluded.append({"row": i, "entity_id": row.get("entity_id", row.get("code")),
                             "metric": row.get("metric"), "reason": reason})
        else:
            kept.append(row)
    return kept, excluded


def _market_payload(preview: dict, evaluation_as_of: str) -> tuple[dict, list]:
    records, excluded = _visible(preview["records"], evaluation_as_of, market=True)
    md = preview.get("metadata") or {}
    declared = md.get("declared") or {}
    # A supplied licence description is not evidence of externally distributable rights.
    payload = {"schema": "weavemind.market_history/0", "source": declared.get("source") or md.get("source") or "来源未标注（未指定数据来源）",
               "license": "unknown", "license_claim": md.get("license_claim", "unknown"),
               "adj_basis": declared.get("adj_basis", ""), "data": records, "rows": len(records),
               "dataset_id": _hash({"raw_sha256": preview["raw_sha256"], "records": records,
                                    "metadata": md}), "issues": preview.get("issues", []),
               "instruments": sorted({r["code"] for r in records}),
               "date_range": ([min(r["date"] for r in records), max(r["date"] for r in records)]
                              if records else []), "evaluation_as_of": evaluation_as_of}
    if not records:
        payload.update(source="unavailable", reason="截止时没有身份/时间有效的行情")
    return payload, excluded


def _calendar(records: list[dict], descriptor: dict, raw_hash: str) -> dict:
    sessions = []
    for row in records:
        value = row.get("date", row.get("日期", ""))
        try:
            s = _moment(str(value)).date().isoformat()
        except (TypeError, ValueError):
            raise ValueError("交易日历含无效日期") from None
        sessions.append(s)
    if len(sessions) != len(set(sessions)):
        raise ValueError("交易日历有重复日期，先在预览中核对")
    sessions.sort()
    return {"schema": "weavemind.trading_calendar/0", "dataset_id": raw_hash,
            "source": (descriptor.get("metadata") or {}).get("source", "file"),
            "license": "unknown", "sessions": sessions,
            "range": [sessions[0], sessions[-1]] if sessions else []}


def _calculation_copy(records: list[dict]) -> tuple[list, list]:
    """Explicit unit bridge; source values remain untouched in frozen_inputs."""
    scales = {"元": 1, "万元": 10000, "亿元": 100000000,
              "千元": 1000, "百万元": 1000000}
    converted, ledger = [], []
    for r in records:
        unit = str(r.get("unit") or "")
        if unit not in scales:
            continue  # unsupported amount units never reach legacy calculators
        scale = Decimal(str(scales[unit]))
        out = dict(r, unit="元", value=float(Decimal(str(r["value"])) * scale))
        ledger.append({"fact_id": r.get("fact_id"), "source_hash": r.get("source_hash"),
                       "source_value": r["value"], "source_unit": unit,
                       "factor": int(scale), "value": out["value"], "unit": "元",
                       "formula": "source_value × explicit_unit_factor", "version": "unit_bridge/1"})
        converted.append(out)
    return converted, ledger


# ---------------------------------------------------------------------------
# Q1 经营机制层（研究输入驱动；主卡=特征截止冻结读数，评估/后续层=evaluation_as_of 门控）
# ---------------------------------------------------------------------------

def _q1_known_at(records: list[dict]) -> dict:
    """实体×年度 → 该年度数字在我方材料集里可获得的最晚时点（同主体同年度取 max）。

    只用**已到我方**的行；不假设报告期末即已知（known_at ≠ 报告期末）。
    """
    out: dict[str, dict[str, str]] = {}
    for row in records:
        code = str(row.get("entity_id") or row.get("code") or "")
        year = str(row.get("period") or row.get("year") or "")[:4]
        when = str(row.get("disclosed_at") or row.get("available_at") or "")
        if not code or len(year) != 4 or not when:
            continue
        try:
            _moment(when)
        except (TypeError, ValueError):
            continue
        bucket = out.setdefault(code, {})
        if when > bucket.get(year, ""):
            bucket[year] = when
    return out


def _q1_run_output(runs: dict, model: str, metric: str):
    for out in ((runs.get(model) or {}).get("outputs") or []):
        if out.get("metric") == metric:
            return out
    return None


def _q1_target_source(runs: dict) -> str:
    out = _q1_run_output(runs, "scenario_sensitivity", "margin_gap_to_threshold_pp")
    if not out:
        return ""
    notes = [str(a) for a in (out.get("assumptions") or ())]
    for note in notes:
        if "目标" in note or "回到" in note:
            return note
    return "；".join(notes[:2])


def _q1_entity_name(obs: list[dict]) -> str:
    for o in obs:
        if o.get("entity"):
            return str(o["entity"])
    return ""


def _q1_context(code: str, entity: str, cspec: dict, qspec: dict, financial: dict,
                risk: dict, *, study_id: str, feature_cutoff: str,
                evaluation_as_of: str, synthetic: bool) -> dict:
    """卡上下文：形状沿用 scripts/q1_real_study.build_context（runs 按 model_id 组织）。

    公司级主因/替代解释/下一核查/目标注记全部来自研究输入（spec），缺项如实留空；
    卡只绑研究ID，result_hash 不回填（无环形 hash）。
    """
    fin_row = financial.get(code) or {}
    runs = {str(r.get("model_id")): r for r in (fin_row.get("runs") or [])}
    rr = risk.get(code) or {}
    feats = {f.get("name"): f.get("value") for f in (rr.get("features") or [])}
    cov = _q1_run_output(runs, "cash_quality", "cashflow_coverage")
    diff = _q1_run_output(runs, "cash_quality", "cfo_minus_profit")
    gap = _q1_run_output(runs, "scenario_sensitivity", "margin_gap_to_threshold_pp")
    growth = _q1_run_output(runs, "scenario_sensitivity", "revenue_growth_to_hold_target")
    occ = _q1_run_output(runs, "cash_pressure", "incremental_capital_occupation")
    recon = runs.get("cash_reconciliation") or {}
    run_period = ""
    for model in ("cash_quality", "scenario_sensitivity", "cash_pressure"):
        outs = (runs.get(model) or {}).get("outputs") or []
        if outs:
            run_period = str(outs[0].get("output_period") or "")
            break
    return {
        "study_id": study_id, "result_hash": None,
        "known_as_of": {"feature_cutoff": feature_cutoff,
                        "evaluation_as_of": evaluation_as_of},
        "sources": [dict(s) for s in (cspec.get("sources") or [])],
        "run_period": run_period,
        "synthetic": synthetic, "research_state": "research_draft",
        "human_review": "unreviewed", "machine_checks": {},
        "company_primary_cause": str(cspec.get("company_primary_cause") or ""),
        "alternatives": [str(a) for a in (cspec.get("alternatives") or [])],
        "refutation_conditions": [str(r) for r in (cspec.get("refutation_conditions") or [])],
        "next_check": [str(n) for n in (cspec.get("next_check") or [])],
        "target_note": str(cspec.get("target_note") or ""),
        "provenance_kind": str(qspec.get("provenance_kind") or "历史重建"),
        "provenance_note": str(cspec.get("provenance_note")
                               or qspec.get("provenance_note") or ""),
        "cash": {"cashflow_coverage": (cov or {}).get("value"),
                 "cfo_minus_profit": (diff or {}).get("value"),
                 "reconciliation_status": (recon or {}).get("status", "")},
        "risk": {k: feats.get(k) for k in ("receivables_to_revenue", "inventory_to_cost",
                                           "liabilities_to_assets")},
        "anomaly_clues": list(rr.get("anomaly_clues") or []),
        "target": {"margin_gap_to_threshold_pp": (gap or {}).get("value"),
                   "revenue_growth_to_hold_target": (growth or {}).get("value"),
                   "target_source": _q1_target_source(runs),
                   "incremental_capital_occupation": (occ or {}).get("value")},
        "run_status": {k: (v or {}).get("status") for k, v in runs.items()},
    }


def _q1_inherited_sources(items: list, raw_by_fact: dict, materials: list,
                          fallback_label: str) -> list[dict]:
    """衔接点A（B3 首例）：spec 未给 sources 时，卡片来源继承**所选 Observation** 的原件出处。

    只做既有字段的原地归并（无新提取器）：composition 项的 fact_id/prev_fact_id →
    冻结前原始观察行（source_url/source_hash/disclosed_at），source_url → 研究输入
    metadata 里已声明的原件 material_id。缺哪项就少写哪项，不编造来源。
    """
    out: dict[tuple, dict] = {}
    for item in items or []:
        for fid in (item.get("prev_fact_id"), item.get("fact_id")):
            fid = str(fid or "")
            rec = raw_by_fact.get(fid)
            if not rec:
                continue
            url = str(rec.get("source_url") or "")
            key = (url, str(rec.get("source_hash") or ""), str(rec.get("disclosed_at") or ""))
            entry = out.setdefault(key, {"source_label": str(rec.get("source_label") or "")
                                         or fallback_label,
                                         "sha256": key[1], "url": url,
                                         "disclosed_at": key[2], "fact_ids": []})
            if fid and fid not in entry["fact_ids"]:
                entry["fact_ids"].append(fid)
    entries = list(out.values())
    for entry in entries:
        for m in materials or []:
            if entry["url"] and str(m.get("url") or "") == entry["url"]:
                entry["material_id"] = str(m.get("material_id") or "")
                break
    return entries


def _q1_pct(value) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _q1_yi(value) -> str:
    try:
        return f"{float(value) / 100000000:,.2f} 亿元"
    except (TypeError, ValueError):
        return "—"


def _q1_yi6(value) -> str:
    """亿、6 位小数：目标核查显示精度（规划 §18.10:825 锚点 22.060589/100.159300）。
    只在渲染时取整，冻结值保持原始精度（与 joint_grid._yi 同一纪律）。"""
    try:
        return f"{float(value) / 100000000:,.6f} 亿元"
    except (TypeError, ValueError):
        return "—"


def _q1_card_hash(card: dict) -> str:
    """与 mechanisms.short_card 同一算法（排除 generated_at；card_hash 旧值随卡进入新 hash）。"""
    return hashlib.sha256(
        json.dumps({k: v for k, v in card.items() if k != "generated_at"},
                   ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _q1_base_fingerprint(card: dict, comp: dict | None, feature_cutoff: str) -> str:
    """衔接点B（B3 首例）：主卡**基期实质指纹**（sha256 全量，存 identity.base_fingerprint）。

    绑定（基期实质）：主体（entity_id/entity）、基期年份（t/上期）、feature_cutoff、
    基期输入来源与事实（来源明细本就被 feature_cutoff 门控，只含披露不晚于截止的原件，
    故不含后期来源）、参数方法（机制版本/口径/公式状态）、读取判定（E 读数、组成表
    逐项贡献、恒等式核对、方向符号）。
    **排除**：后续（follow_up）、评价截止（evaluation_as_of）、全研究ID（study_id/
    result_hash/card_hash，无环形哈希）——仅推进评价截止或换研究ID不改基期指纹；
    独立续页引用它说明对照的是哪份冻结基期，外层 study 版本仍可变化。
    """
    comp = comp or {}
    readings = card.get("readings") or {}
    identity = card.get("identity") or {}
    substance = {
        "entity_id": card.get("entity_id"), "entity": card.get("entity"),
        "caliber": card.get("caliber"),
        "base_year": readings.get("t"), "prev_year": readings.get("prev_year"),
        "feature_cutoff": feature_cutoff,
        "input_sources": [dict(s) for s in (identity.get("sources") or [])],
        "input_fact_ids": list(identity.get("input_fact_ids") or []),
        "params_method": {"method_version": identity.get("method_version"),
                          "formula": identity.get("formula"),
                          "formula_status": identity.get("formula_status")},
        "reading_verdict": {
            "e_t": readings.get("e_t"), "e_prev": readings.get("e_prev"),
            "delta_e": readings.get("delta_e"),
            "identity_ok": readings.get("identity_ok"),
            "composition": [{"metric": i.get("metric"), "prev_value": i.get("prev_value"),
                             "value": i.get("value"), "delta": i.get("delta"),
                             "contribution": i.get("contribution")}
                            for i in (readings.get("composition") or [])],
            "o_t": readings.get("o_t"), "sign_o": readings.get("sign_o"),
        },
    }
    return hashlib.sha256(
        json.dumps(substance, ensure_ascii=False, sort_keys=True, default=str)
        .encode("utf-8")).hexdigest()


def _q1_target_followup(tf_spec: dict, idx: dict, raw_by_fact: dict, *,
                        target: dict | None, entity: str = "",
                        base_fingerprint: str = "",
                        materials: list | None = None) -> dict:
    """§18.10:825 目标期新实际 → 独立续页记录（新读数不回填旧卡）。

    T 已在 joint_grid.target 引用冻结事实；这里补目标期**同属性归母净利**（评价
    截止门控可见的同口径冻结读数，index_observations 同口径过滤与冲突检测）与 T
    对照，结论只说研究者目标达到/未达到——不是管理层承诺落空，年报实际也不硬塞
    为盈利指引事件。缺读数（如三一 2025 年报材料未到）保持待数据、不写实际；
    同格多值冲突同样不择一。来源逐字透传所选格的 fact_id 对应原始行出处，无新
    提取器；基期指纹（衔接点B）说明对照的是哪份冻结基期。
    """
    metric = str(tf_spec.get("metric") or "net_profit")
    period = str(tf_spec.get("period") or "")
    if len(period) != 4 or not period.isdigit():
        raise ValueError("q1.companies[].target_followup.period 须为四位年度")
    nature = str(tf_spec.get("nature") or
                 "研究者设定目标核查：不是管理层指引，也不构成业绩承诺；新读数不回填旧卡")
    block: dict = {"status": "待数据", "metric": metric,
                   "metric_label": str(tf_spec.get("metric_label") or metric),
                   "period": period, "caliber": "合并", "nature": nature,
                   "entity": entity, "base_fingerprint": base_fingerprint,
                   "target": None, "actual": None, "gap": None,
                   "verdict": None, "pending_reason": "", "verified_at": None,
                   "note": ("新读数只进本续页：不回填旧卡，主卡与 card_hash 不变；"
                            "结论是研究者目标核查，不是管理层承诺落空；"
                            "年报实际不硬塞为盈利指引事件。")}
    cells = (idx or {}).get("cells") or {}
    conflict = (cells.get(("__conflict__", f"{metric}:{period}")) or {}).get("conflict")
    cell = cells.get((metric, period))
    if conflict:
        block["pending_reason"] = (f"{period} 年同口径同格多值冲突（{metric}）："
                                   "不择一、不取单值，保持待数据；不写实际读数")
        return block
    if cell is None:
        block["pending_reason"] = (f"评价截止前没有{period}年同口径读数："
                                   "保持待数据，不写实际读数")
        return block
    if not isinstance(target, dict) or target.get("value") is None:
        block["pending_reason"] = "网格缺目标 T（T 须引用冻结事实）：不核目标"
        return block
    a, b = Decimal(str(cell["value"])), Decimal(str(target["value"]))
    raw = raw_by_fact.get(str(cell.get("fact_id") or ""), {})
    # 衔接点A 同法：raw 行不带 material_ids——经 source_url 匹配研究输入
    # metadata 里已声明的原件 material_id（与卡来源继承一致，无新提取器）。
    mid, url = "", str(raw.get("source_url") or "")
    for m in materials or []:
        if url and str(m.get("url") or "") == url:
            mid = str(m.get("material_id") or "")
            break
    block.update({
        "status": "已记录",
        "target": {k: target.get(k) for k in ("metric", "period", "value", "unit",
                                              "source", "goal_note", "nature",
                                              "passed_as")},
        "actual": {"period": str(cell.get("period") or period),
                   "period_end": str(cell.get("period_end") or ""),
                   "metric": metric, "value": float(a), "unit": "元",
                   "fact_id": str(cell.get("fact_id") or ""),
                   "disclosed_at": str(raw.get("disclosed_at") or ""),
                   "available_at": str(raw.get("available_at") or ""),
                   "source": str(raw.get("source") or raw.get("source_label") or ""),
                   "source_url": str(raw.get("source_url") or ""),
                   "source_hash": str(raw.get("source_hash") or ""),
                   "source_row": raw.get("source_row"),
                   "material_ids": ([mid] if mid
                                    else [str(m) for m in (raw.get("material_ids") or ())]),
                   "verify_state": str(raw.get("verify_state") or "")},
        "gap": float(a - b),
        "verdict": ("达到" if a >= b else "未达到") + f"研究者设定的{period}目标",
        "verified_at": str(raw.get("disclosed_at") or ""),
    })
    return block


def _q1_period_label(period) -> str:
    """显示用周期标签：源值可能已带「年」（如 2023年），去掉尾字避免「2023年 年」重复；
    只影响渲染文字，不改冻结数值与任何指纹。"""
    s = str(period or "")
    return s[:-1] if s.endswith("年") else s


def _q1_followup_md(code: str, fu: dict, q1: dict) -> str:
    """独立后续页：原卡方向 → 同口径实际读数 → 支持/削弱/未决；三一缺下年 → pending。"""
    target_year, split_at = q1.get("target_year"), q1.get("split_at")
    lines = [f"# Q1 后续页：{code}（t={target_year}）", "",
             f"- 方法 `{q1.get('method_version')}`；评估 `{q1.get('eval_version')}`；"
             f"结果评价截止 `{q1.get('evaluation_as_of')}`；切分 `{split_at}`。",
             f"- 基期实质指纹（identity.base_fingerprint，绑定主体/年度/特征截止/基期来源/"
             f"参数方法/读数判定；排除后续、评价截止与全研究ID）：`{fu.get('base_fingerprint')}`",
             f"- 性质：**{q1.get('provenance_kind')}**（生成时后续年度结果已公开），"
             "不构成事前预测或 alpha 声明。", "",
             f"## 状态：{fu.get('status')}（{fu.get('mechanism_verdict')}）", ""]
    if fu.get("status") == "待数据":
        lines.append(fu.get("pending_reason") or "下一同口径年度尚未取得：不写实际读数。")
        lines.append("")
    else:
        act = fu.get("actual") or {}
        lines += [f"- 原卡方向：O_{fu.get('t')} = {_q1_pct(fu.get('o_t'))}"
                  f"（{fu.get('sign_o')}）→ 预期 Y_op 方向「{fu.get('predicted_sign')}」",
                  f"- 实际读数（同口径）：{fu.get('next_year')} 年 E = {_q1_yi(act.get('e_next'))}；"
                  f"Y_op = {_q1_pct(act.get('y_op'))}（{act.get('sign_y')}）",
                  f"- 预测 {fu.get('predicted_sign')} vs 实际 {act.get('sign_y')} "
                  f"→ {fu.get('mechanism_verdict')}；核验时点 `{fu.get('verify_by')}`",
                  f"- 注记：{fu.get('note')}", ""]
        prior = fu.get("prior")
        if prior:
            lines += [f"## 同法上一期对照（t={fu.get('prior_year')}）", "",
                      f"- O = {_q1_pct(prior.get('o_t'))}（{prior.get('sign_o')}）→ "
                      f"实际 Y_op = {_q1_pct(prior.get('y_op'))}（{prior.get('sign_y')}）"
                      f"→ {prior.get('mechanism_verdict')}",
                      "- 上一期如误判，**保留**：误判本身写进负结果，不隐藏、不事后换窗口。", ""]
    tf = fu.get("target_followup")
    if isinstance(tf, dict):
        lines += ["## 研究者目标核查（新实际·独立续页，不回填旧卡）", ""]
        tgt = tf.get("target") or {}
        if tgt:
            lines.append(f"- 研究者目标 T：{_q1_period_label(tgt.get('period'))} 年"
                         f"{tf.get('metric_label')} {_q1_yi6(tgt.get('value'))}"
                         f"（{tgt.get('goal_note')}；{tgt.get('nature')}）")
        if tf.get("status") == "已记录":
            tact = tf.get("actual") or {}
            lines.append(f"- 目标期实际：{tf.get('period')} 年"
                         f"{tf.get('metric_label')} {_q1_yi6(tact.get('value'))}"
                         f"（{tf.get('caliber')}口径，披露 {tact.get('disclosed_at') or '—'}）")
            lines.append(f"- 结论：**{tf.get('verdict')}**（实际 − T = {_q1_yi6(tf.get('gap'))}）")
            mats = "、".join(tact.get("material_ids") or [])
            lines.append(f"- 来源：{tact.get('source') or '来源未标注'}；"
                         f"fact_id `{tact.get('fact_id')}`；材料 {mats or '未声明'}；"
                         f"source_hash `{str(tact.get('source_hash') or '')[:16]}…`；"
                         f"verify_state `{tact.get('verify_state') or '—'}`")
            if tact.get("source_url"):
                lines.append(f"- 原件：<{tact['source_url']}>")
        else:
            lines.append(f"- 目标期实际：{tf.get('pending_reason')}")
        lines.append(f"- 边界：{tf.get('note')}")
        lines.append("")
    lines += ["## 下一核查", ""] + [f"- {n}" for n in (fu.get("next_check") or [])]
    lines += ["", f"逐公司卡与方向对照见 `q1_cards.md` / `q1_evaluation.md`；研究ID见 study_manifest.json。"]
    return "\n".join(lines)


def _q1_summary_lines(q1: dict) -> list:
    ev = q1.get("evaluation") or {}
    inc = ev.get("increment") or {}
    split = ev.get("split") or {}
    tallies = ev.get("tallies") or {}
    common = ev.get("common_coverage") or {}
    lines = ["", f"## Q1 经营机制卡（t={q1.get('target_year')}，{q1.get('provenance_kind')}）", ""]
    lines.append(f"- 方法 `{q1.get('method_version')}`；评估 `{q1.get('eval_version')}`；"
                 f"主卡 t=`{q1.get('target_year')}`；特征截止 `{q1.get('feature_cutoff')}`；"
                 f"结果评价截止 `{q1.get('evaluation_as_of')}`；切分 `{q1.get('split_at')}`。")
    for code, card in sorted((q1.get("cards") or {}).items()):
        rd = card.get("readings") or {}
        jud = card.get("judgement") or {}
        fu = card.get("follow_up") or {}
        lines.append(f"- {code}：O_{rd.get('t')}={_q1_pct(rd.get('o_t'))}（{rd.get('sign_o')}）"
                     f"→ 预期{jud.get('expected_direction', '')}；"
                     f"后续 {fu.get('status')}（{fu.get('mechanism_verdict')}）。")
    lines.append(f"- 方向对照增量：`{inc.get('verdict')}`；同集 n={common.get('n', 0)}；"
                 f"规则计分 {tallies.get('rule', {}).get('scored', 0)}"
                 f"（命中 {tallies.get('rule', {}).get('hit', 0)}）。")
    if not (split.get("holdout") or []):
        lines.append("- 留出期计分为 0：不作样本外优势声明。")
    for code, fu in sorted((q1.get("followups") or {}).items()):
        lines.append(f"- 后续页 {code}：{fu.get('status')}（{fu.get('mechanism_verdict')}）"
                     f"→ `q1_followup_{code}.md`。")
        tf = fu.get("target_followup")
        if isinstance(tf, dict):
            tgt = tf.get("target") or {}
            if tf.get("status") == "已记录":
                act = tf.get("actual") or {}
                lines.append(f"- 目标核查 {code}：T={_q1_yi6(tgt.get('value'))} vs "
                             f"实际 {_q1_yi6(act.get('value'))} → {tf.get('verdict')}"
                             "（新读数不回填旧卡）。")
            else:
                lines.append(f"- 目标核查 {code}：{tf.get('status')}——{tf.get('pending_reason')}。")
    lines.append("- 逐公司卡与方向对照见 `q1_cards.md` / `q1_evaluation.md`；"
                 "历史重建下不构成事前预测证明。")
    return lines


def _q1_layer(spec: dict, all_obs: list, financial: dict, risk: dict, *, study_id: str,
              feature_cutoff: str, evaluation_as_of: str) -> dict | None:
    """spec["q1"] 显式块 → 主卡（特征截止冻结）+ 评估/后续层（evaluation_as_of 门控）。

    卡只写冻结读数能支持的内容；后续结果只进 follow_up/后续页，不回改冻结判断；
    卡与评估都不带时间戳（同冻结输入重算同实质 hash）。
    """
    from quant_research import evaluate_mechanisms as EV, mechanisms as M
    from financial_analysis import dataset
    qspec = spec.get("q1")
    if qspec is None:
        return None
    if not isinstance(qspec, dict):
        raise ValueError("q1 须为显式研究输入对象")
    target_year = str(qspec.get("target_year") or "")
    split_at = str(qspec.get("split_at") or "")
    if len(target_year) != 4 or len(split_at) != 10:
        raise ValueError("q1.target_year 须为四位年度，q1.split_at 须为 YYYY-MM-DD")
    companies = qspec.get("companies")
    if not isinstance(companies, dict) or not companies:
        raise ValueError("q1.companies 须按主体代码组织")
    synthetic = spec.get("synthetic", False)
    known_all = _q1_known_at(all_obs)
    eval_visible, _eval_excluded = _visible(all_obs, evaluation_as_of)
    fdesc = _descriptor(spec["financial"]) if spec.get("financial") else {}
    source_label = ((fdesc.get("metadata") or {}).get("source")
                    or "来源未标注（未指定数据来源）")

    base_readings, eval_readings, eval_index, comps, cards, followups, skipped = {}, {}, {}, {}, {}, {}, {}
    cases, windows = [], {}
    for code in sorted(companies):
        cspec = companies[code] or {}
        if not isinstance(cspec, dict):
            raise ValueError(f"q1.companies[{code}] 须为对象")
        fin_row = financial.get(code) or {}
        obs = (fin_row.get("dataset") or {}).get("observations") or []
        entity = _q1_entity_name(obs)
        if obs:
            base_readings[code] = M.readings(obs, entity_id=code, entity=entity)
            comp = M.composition(base_readings[code], target_year)
            if comp.get("state") == "ok":
                comps[code] = comp
        else:
            skipped[code] = "特征截止前没有可算读数的有效材料"
        eobs, _unit_ledger = _calculation_copy(
            [r for r in eval_visible if r.get("entity_id") == code])
        if eobs:
            eds = dataset.freeze(eobs, entity_id=code, as_of=evaluation_as_of,
                                 periods=fdesc.get("periods", ()),
                                 source_label=source_label,
                                 restrictions="内部研究；许可与历史版本范围未认证")
            er = M.readings(eds.as_dict().get("observations") or [], entity_id=code,
                            entity=entity or _q1_entity_name(eobs))
            eval_readings[code] = er
            # 目标核查用：同一冻结评估观察的同口径索引（冲突格如实留冲突标记）。
            eval_index[code] = M.index_observations(
                eds.as_dict().get("observations") or [], caliber="合并")
            erows = EV.build_cases(er, known_at=known_all.get(code, {}), window=code)
            cases.extend(erows)
            windows[code] = [{"window": c["case_id"], "years": [c["t"], c["next_year"]]}
                             for c in erows]
        elif code not in skipped:
            skipped[code] = "结果评价截止前没有可算读数的有效材料"
    cases.sort(key=lambda c: c["case_id"])
    report = None
    if cases:
        report = EV.evaluate(cases, pool=sorted(eval_readings), split_at=split_at,
                             window_note=str(qspec.get("window_note")
                                             or "同一公司的多个年度窗口共享年度，重叠，不是独立样本"),
                             entity_windows=windows)
    by_case = {c["case_id"]: c for c in (report or {}).get("case_rows", [])}
    for code in sorted(companies):
        if code not in base_readings:
            continue
        cspec = companies[code] or {}
        base = base_readings[code]
        comp = comps.get(code)
        ctx = _q1_context(code, base.get("entity"), cspec, qspec, financial, risk,
                          study_id=study_id, feature_cutoff=feature_cutoff,
                          evaluation_as_of=evaluation_as_of, synthetic=synthetic)
        # 衔接点A（B3 首例）：input_fact_ids 覆盖组成表的本期与上期事实（所选 Observation）；
        # spec 没带 sources 时，卡来源继承它们的原件出处（fact_id → 原始行 → 已声明材料），
        # 无新提取器；spec 显式给了则尊重 spec。
        comp_items = (comp or {}).get("items") or []
        ctx["input_fact_ids"] = sorted(
            {fid for i in comp_items
             for fid in (i.get("prev_fact_id"), i.get("fact_id")) if fid})
        if not cspec.get("sources"):
            fin_row = financial.get(code) or {}
            raw_by_fact = {str(r.get("fact_id") or ""): r
                           for r in all_obs if r.get("entity_id") == code}
            ctx["sources"] = _q1_inherited_sources(
                comp_items, raw_by_fact,
                (fdesc.get("metadata") or {}).get("materials") or [], source_label)
        card = M.short_card(base, composition_row=comp, context=ctx)
        card["readings"]["primary_t"] = target_year
        # 衔接点B（B3 首例）：基期实质指纹写进 identity；只排除 follow_up/评价截止/研究ID，
        # 仅推进 evaluation_as_of 不改它——独立续页引用同一指纹即可锁定同一份冻结基期。
        card["identity"]["base_fingerprint"] = _q1_base_fingerprint(
            card, comp, feature_cutoff)
        case = by_case.get(f"{code}::t={target_year}")
        chain = next((c for c in (eval_readings.get(code) or {}).get("chains", [])
                      if c["t"] == target_year), {})
        if case is not None:
            fu = EV.follow_up(case, verify_by=str(case.get("outcome_at") or ""))
            fu["case_id"] = case["case_id"]
            card["follow_up"] = fu
            prev_t = chain.get("prev_year") or ""
            prior = by_case.get(f"{code}::t={prev_t}") if prev_t else None
            followups[code] = {
                "entity_id": code, "entity": card.get("entity") or base.get("entity"),
                "base_fingerprint": card["identity"].get("base_fingerprint"),
                "t": target_year, "verify_by": fu.get("verify_by"),
                "status": fu.get("status"), "mechanism_verdict": fu.get("mechanism_verdict"),
                "case_id": case["case_id"],
                "o_t": case.get("o_t"), "sign_o": case.get("sign_o"),
                "predicted_sign": case.get("sign_o"),
                "actual": ({"y_op": case.get("y_op"), "sign_y": case.get("sign_y"),
                            "e_next": chain.get("e_next"), "next_year": chain.get("next_year")}
                           if fu.get("status") != "待数据" else None),
                "next_year": chain.get("next_year"),
                "prior_year": prev_t or None,
                "prior": ({"case_id": prior["case_id"], "o_t": prior.get("o_t"),
                           "sign_o": prior.get("sign_o"), "y_op": prior.get("y_op"),
                           "sign_y": prior.get("sign_y"),
                           "mechanism_verdict": prior.get("rule_verdict")} if prior else None),
                "pending_reason": (fu.get("note") if fu.get("status") == "待数据" else ""),
                "note": fu.get("note"),
                "next_check": [str(n) for n in (cspec.get("next_check") or [])],
            }
        else:
            followups[code] = {
                "entity_id": code, "entity": card.get("entity") or base.get("entity"),
                "base_fingerprint": card["identity"].get("base_fingerprint"),
                "t": target_year, "status": "待数据",
                "mechanism_verdict": "未决",
                "pending_reason": "切分时点没有成熟结果可核：不写实际读数",
                "next_check": [str(n) for n in (cspec.get("next_check") or [])],
            }
        # §18.10:825 目标期新实际（独立续页）：只进 followups[code]["target_followup"]，
        # 不回填旧卡——card_hash 仍未动；T 来自本家 joint_grid.target（引用冻结事实）。
        tcfg = cspec.get("target_followup")
        if isinstance(tcfg, dict):
            raw_by = {str(r.get("fact_id") or ""): r
                      for r in all_obs if r.get("entity_id") == code}
            followups[code]["target_followup"] = _q1_target_followup(
                tcfg, eval_index.get(code) or {"cells": {}}, raw_by,
                target=(fin_row.get("joint_grid") or {}).get("target"),
                entity=str(followups[code].get("entity") or ""),
                base_fingerprint=str(followups[code].get("base_fingerprint") or ""),
                materials=(fdesc.get("metadata") or {}).get("materials") or [])
        card["card_hash"] = _q1_card_hash(card)
        cards[code] = card

    md_cards = M.cards_markdown([cards[c] for c in sorted(cards)],
                                title=f"Q1 经营机制卡（t={target_year}，同口径）")
    md_eval = (EV.markdown(report, title=f"Q1 方向规则对照（切分 {split_at}）")
               if report else "")
    q1 = {
        "schema": "weavemind.q1_layer/1",
        "method_version": M.METHOD_VERSION, "eval_version": EV.EVAL_VERSION,
        "provenance_kind": str(qspec.get("provenance_kind") or "历史重建"),
        "provenance_note": str(qspec.get("provenance_note") or ""),
        "target_year": target_year, "split_at": split_at,
        "feature_cutoff": feature_cutoff, "evaluation_as_of": evaluation_as_of,
        "known_at": {c: known_all.get(c, {}) for c in sorted(companies)},
        "skipped": skipped,
        "readings": base_readings, "readings_evaluation": eval_readings,
        "compositions": comps,
        "cards": cards, "followups": followups,
        "evaluation": report or {},
        "evaluation_hash": (EV.evaluation_hash(report) if report else ""),
        "card_hashes": {c: cards[c]["card_hash"] for c in sorted(cards)},
        "readings_hash": hashlib.sha256(
            json.dumps(base_readings, ensure_ascii=False, sort_keys=True,
                       default=str).encode("utf-8")).hexdigest(),
        "module_hashes": {n: hashlib.sha256((REPO_ROOT / n).read_bytes()).hexdigest()
                          for n in ("quant_research/mechanisms.py",
                                    "quant_research/evaluate_mechanisms.py")},
    }
    q1["markdown"] = {"cards": md_cards, "evaluation": md_eval,
                      "followup": {c: _q1_followup_md(c, followups[c], q1=q1)
                                   for c in sorted(followups)}}
    return q1


def _model_params(descriptor: dict) -> dict:
    params = descriptor.get("params") or {}
    if not isinstance(params, dict) or any(not isinstance(p, dict) for p in params.values()):
        raise ValueError("financial.params 必须按模型ID组织参数对象")
    params = {k: dict(v) for k, v in params.items()}
    shared = descriptor.get("scenario_params") or {}
    if not isinstance(shared, dict) or any(k not in {"revenue_growth", "gross_margin_delta"}
                                         for k in shared):
        raise ValueError("scenario_params 只接受共享收入/毛利率假设")
    for key in ("revenue_growth", "gross_margin_delta"):
        values = [p[key] for m, p in params.items()
                  if m in {"scenario_sensitivity", "cash_pressure"} and key in p]
        if key in shared:
            values.append(shared[key])
        if values and any(v != values[0] for v in values):
            raise ValueError(f"利润与现金压力的共享假设 {key} 冲突")
        if values:
            for model in ("scenario_sensitivity", "cash_pressure"):
                params.setdefault(model, {})[key] = values[0]
    return params


def run_study(spec: dict, *, input_dir) -> dict:
    """Read explicit inputs and compute an internal study; never writes existing tasks."""
    if not isinstance(spec, dict) or spec.get("schema", SPEC_SCHEMA) != SPEC_SCHEMA:
        raise ValueError("study spec schema 不支持")
    feature_cutoff = str(spec.get("feature_cutoff") or "")
    evaluation_as_of = str(spec.get("evaluation_as_of") or feature_cutoff)
    if _moment(evaluation_as_of) < _moment(feature_cutoff):
        raise ValueError("evaluation_as_of 不得早于 feature_cutoff")
    if "synthetic" in spec and not isinstance(spec["synthetic"], bool):
        raise ValueError("synthetic 必须为布尔值")
    root = Path(input_dir).resolve()
    from adapters.wind_import import preview_file

    sources, previews, source_records = {}, {}, {}
    for key in ("financial", "market", "events", "calendar", "credit_labels", "fraud_labels"):
        if not spec.get(key):
            continue
        desc = _descriptor(spec[key])
        p = _input_path(root, str(desc["file"]))
        raw_sha = hashlib.sha256(p.read_bytes()).hexdigest()
        sources[key] = {"file": str(desc["file"]), "raw_sha256": raw_sha,
                        "metadata": desc.get("metadata", {}), "field_map": desc.get("field_map", {})}
        if key in ("financial", "market"):
            previews[key] = preview_file(p, kind=key, metadata=desc.get("metadata"),
                                         field_map=desc.get("field_map"))
        else:
            source_records[key] = _read_records(p)
    return _compute_study(spec, sources, previews, source_records)


def _market_binding(spec: dict, evaluation_as_of: str) -> dict:
    """spec 钉住的行情数据集/交易日历 → 实际加载并记录绑定（C2c 唯一来源接缝）。

    规则（架构师 2026-10-11）：新候选 spec **显式写**已用的行情 dataset_id 与日历 calendar_id；
    生成路径按这些 id 真加载一次，把「要求什么、实取什么、内容指纹、来源标注」写进冻结底稿；
    id 不一致/加载失败时如实标 `refused`／`unpinned` 并给原因，**绝不替换成别的数据集**。
    老 spec（没有 `market_pin`、没有 `market.dataset_id`）一律记 `unpinned`，不回写、不改旧研究。
    """
    mkt = spec.get("market_pin") if isinstance(spec.get("market_pin"), dict) else {}
    mkt = mkt or (spec.get("market") if isinstance(spec.get("market"), dict) else {})
    ds_id = str(mkt.get("price_dataset_id") or mkt.get("dataset_id") or "").strip()
    cal_id = str(mkt.get("calendar_id") or "").strip()
    out: dict = {"schema": "weavemind.market_binding/1",
                 "evaluation_as_of": str(evaluation_as_of or ""),
                 "price_dataset_id": ds_id,
                 "price_dataset_id_source": "spec" if ds_id else "unpinned",
                 "calendar_id": cal_id,
                 "calendar_id_source": "spec" if cal_id else "unpinned",
                 "source_note": str(mkt.get("note") or ""),
                 "price": {}, "calendar": {}, "reasons": []}
    if not ds_id and not cal_id:
        out["status"] = "unpinned"
        out["reasons"].append("spec 没有钉住行情数据集/交易日历：下游按默认来源取，"
                             "换环境的默认值就会换读数")
        return out
    try:
        from adapters import market_history as mh
        from adapters import trading_calendar as tcal
    except Exception as exc:                             # noqa: BLE001 - 算子不可用如实报
        out["status"] = "refused"
        out["reasons"].append(f"行情/日历模块不可用：{str(exc)[:120]}")
        return out
    if ds_id:
        try:
            payload = mh.load(ds_id)
        except Exception as exc:                          # noqa: BLE001
            payload, err = {}, str(exc)[:120]
        else:
            err = ""
        got = str(payload.get("dataset_id") or "")
        if not got or got != ds_id or str(payload.get("source") or "") == "unavailable":
            out["price"] = {"requested": ds_id, "loaded": got,
                            "reason": str(payload.get("reason") or err or "id 不同或不可用")}
            out["reasons"].append(f"行情数据集不一致（要求 {ds_id[:16]}…，实取 "
                                  f"{(got or '空')[:16]}…，{err or payload.get('reason') or 'id 不同'}）："
                                  "不替换、不读数")
        else:
            rows_file = Path(mh.data_dir()) / ds_id / "rows.csv"
            out["price"] = {"requested": ds_id, "loaded": got or ds_id,
                            "source": str(payload.get("source") or ""),
                            "license": str(payload.get("license") or ""),
                            "adj_basis": str(payload.get("adj_basis") or ""),
                            "rows": payload.get("rows"),
                            "date_range": list(payload.get("date_range") or []),
                            "instruments": list(payload.get("instruments") or []),
                            "rows_file_sha256": (hashlib.sha256(rows_file.read_bytes()).hexdigest()
                                                 if rows_file.is_file() else "")}
    if cal_id:
        try:
            payload = tcal.load(cal_id)
        except Exception as exc:                          # noqa: BLE001
            payload, err = {}, str(exc)[:120]
        else:
            err = ""
        got = str(payload.get("dataset_id") or "")
        # 适配层取不到时返回 `source=unavailable` 且**没有** dataset_id——不能把"没取到"
        # 记成"已绑定"（这正是要防的假绑定）。
        if not got or got != cal_id or str(payload.get("source") or "") == "unavailable":
            out["calendar"] = {"requested": cal_id, "loaded": got,
                               "reason": str(payload.get("reason") or err or "id 不同或不可用")}
            out["reasons"].append(f"交易日历不一致（要求 {cal_id[:16]}…，实取 "
                                  f"{(got or '空')[:16]}…，{err or payload.get('reason') or 'id 不同'}）："
                                  "不替换成最近一份日历")
        else:
            sessions_file = Path(tcal.data_dir()) / cal_id / "sessions.csv"
            out["calendar"] = {"requested": cal_id, "loaded": got,
                               "source": str(payload.get("source") or ""),
                               "market": str(payload.get("market") or ""),
                               "rows": payload.get("rows"),
                               "range": list(payload.get("range") or []),
                               "sessions_file_sha256": (
                                   hashlib.sha256(sessions_file.read_bytes()).hexdigest()
                                   if sessions_file.is_file() else "")}
    ok = (not out["reasons"]) and bool(out["price"] or out["calendar"])
    out["status"] = "pinned" if ok else "refused"
    return out


def _compute_study(spec: dict, sources: dict, previews: dict, source_records: dict) -> dict:
    """The same numerical path serves file ingestion and replay of frozen inputs."""
    from financial_analysis import dataset, events, runner
    from financial_analysis.contracts import NotApplicable
    from quant_research import event_returns, backtest
    from risk_research import features, readiness
    feature_cutoff = str(spec["feature_cutoff"])
    evaluation_as_of = str(spec.get("evaluation_as_of") or feature_cutoff)
    financial, risk, excluded = {}, {}, {}
    synthetic = spec.get("synthetic", False)
    all_obs = [dict(r, synthetic=True) if synthetic else dict(r)
               for r in (previews.get("financial") or {}).get("records", [])]
    visible_obs, excluded["financial"] = _visible(all_obs, feature_cutoff)
    fdesc = _descriptor(spec["financial"]) if spec.get("financial") else {}
    model_ids = fdesc.get("models", ["scenario_sensitivity", "cash_pressure"])
    if not isinstance(model_ids, list) or not all(isinstance(m, str) for m in model_ids):
        raise ValueError("financial.models 必须为已注册模型ID列表")
    params = _model_params(fdesc)
    # Subjects are read from each source; never relabel another company's material.
    company_ids = sorted({str(r.get("entity_id")) for r in all_obs if r.get("entity_id")})
    for code in company_ids:
        source_obs = [r for r in visible_obs if r.get("entity_id") == code]
        obs, conversion_ledger = _calculation_copy(source_obs)
        ds = dataset.freeze(obs, entity_id=code, as_of=feature_cutoff,
                            periods=fdesc.get("periods", ()),
                            # 来源标签反映**实际声明的来源**（B1 接缝②）：官方披露缓存不得
                            # 因为入口模块叫 wind_import 就写成"Wind 文件"。
                            source_label=((fdesc.get("metadata") or {}).get("source")
                                          or "来源未标注（未指定数据来源）"),
                            restrictions="内部研究；许可与历史版本范围未认证")
        runs = []
        for model_id in model_ids:
            try:
                runs.append(runner.run(model_id, ds, params=params.get(model_id, {})).as_dict())
            except (KeyError, NotApplicable):
                runs.append({"model_id": model_id, "status": "unavailable", "reason": "模型未注册"})
        financial[code] = {"dataset": ds.as_dict(), "runs": runs,
                           "synthetic": synthetic or all(r.get("synthetic") is True for r in source_obs),
                           "unit_conversion": conversion_ledger,
                           "unsupported_units": sorted({r.get("unit") for r in source_obs
                                                       if r.get("fact_id") not in
                                                       {o.get("fact_id") for o in obs}})}
        # B3 组合网格（可选，spec 显式声明才启用）：同一冻结 ds 上逐格调用既有算子。
        grid_block = fdesc.get("joint_grid")
        if isinstance(grid_block, dict):
            gspec = (grid_block.get("grids") or {}).get(code)
            if isinstance(gspec, dict):
                from quant_research import joint_grid
                financial[code]["joint_grid"] = joint_grid.build_grid(
                    ds, entity_id=code,
                    growth_values=gspec.get("growth_values", []),
                    margin_deltas=gspec.get("margin_deltas", []),
                    collection_days_delta=grid_block.get("collection_days_delta", 10),
                    inventory_days_delta=grid_block.get("inventory_days_delta", 10),
                    budget=gspec.get("budget"),
                    goal_note=str(gspec.get("goal_note") or ""))
        risk[code] = features.build([r for r in all_obs if r.get("entity_id") == code],
                                    entity_id=code, as_of=feature_cutoff)

    event_records = [dict(r, synthetic=True) if synthetic else dict(r)
                     for r in source_records.get("events", [])]
    event_batch = events.extract(event_records, as_of=feature_cutoff)
    event_batch["synthetic"] = synthetic
    for e in event_batch.get("events", []):
        e["synthetic"] = synthetic or e.get("synthetic", False)
    event_list = events.to_event_list(event_batch)
    calendar = (_calendar(source_records["calendar"], _descriptor(spec["calendar"]),
                          sources["calendar"]["raw_sha256"]) if "calendar" in sources else None)
    quant = {}
    # 来源接缝（C2c，2026-10-11）：spec 显式钉住的行情数据集/交易日历要在**生成路径**里真的
    # 被消费并留下可复核的绑定（id ＋ 实取内容指纹），而不是让下游按环境默认另取一份。
    quant["market_binding"] = _market_binding(spec, evaluation_as_of)
    market_payload = None
    if "market" in previews:
        market_payload, excluded["market"] = _market_payload(previews["market"], evaluation_as_of)
        windows = spec.get("event_windows", [1, 5, 20])
        if (not isinstance(windows, list) or not windows
                or any(type(w) is not int or w < 1 for w in windows)
                or len(set(windows)) != len(windows)):
            raise ValueError("event_windows 须为不重复的正整数交易日窗口")
        quant["event_returns"] = event_returns.compute(
            market_payload, event_list, benchmark=str(spec.get("benchmark") or ""),
            # The existing operator numbers t0 as offset 0, which is holding day 1.
            offsets=[w - 1 for w in windows],
            evaluation_as_of=evaluation_as_of, calendar=calendar,
            allow_unadjusted=spec.get("allow_unadjusted", False) is True)
        if spec.get("backtest"):
            if not isinstance(spec["backtest"], dict):
                raise ValueError("backtest 须显式提供规则，不能用 true 代替")
            quant["backtest"] = backtest.run(market_payload, spec["backtest"],
                                             costs=spec.get("costs"), calendar=calendar,
                                             benchmark=str(spec.get("benchmark") or ""),
                                             dividends=spec.get("dividends"))
            quant["backtest"]["executable"] = False
            quant["backtest"]["kind"] = "internal_ledger_prototype"
    else:
        quant["event_returns"] = {"status": "waiting_data", "reason": "未提供历史行情文件"}
    for reading in quant.values():
        reading["delivery_eligible"] = False
        reading["delivery_block_reason"] = "内部研究入口没有认证学校/供应商对外分发权限"
        reading["synthetic"] = spec.get("synthetic", False)

    label_readiness = {}
    for task in ("credit", "fraud"):
        key = task + "_labels"
        label_records = [dict(r, synthetic=True) if synthetic else dict(r)
                         for r in source_records.get(key, [])]
        label_readiness[task] = readiness.assess(label_records, task=task,
                                               evaluation_as_of=evaluation_as_of)
    impl = _implementations()
    manifest = {"schema": SCHEMA, "impl_version": IMPL_VERSION,
                "sources": sources, "feature_cutoff": feature_cutoff,
                "evaluation_as_of": evaluation_as_of, "synthetic": spec.get("synthetic", False),
                "implementation_hashes": impl, "params_hash": _hash(spec),
                "external_delivery": {"allowed": False, "reason": "本批仅生成内部研究产物"},
                "research_state": "research_draft", "human_review": "unreviewed"}
    study_id = _hash(manifest)
    manifest["study_id"] = study_id
    result = {"schema": SCHEMA, "study_id": study_id, "manifest": manifest,
              "financial": financial, "events": event_batch, "quant": quant,
              "risk": risk, "label_readiness": label_readiness,
              "q1": _q1_layer(spec, all_obs, financial, risk, study_id=study_id,
                              feature_cutoff=feature_cutoff,
                              evaluation_as_of=evaluation_as_of),
              "previews": previews, "excluded": excluded,
              "frozen_inputs": {"spec": spec, "previews": previews,
                                "source_records": source_records,
                                # C2d 唯一持久化接缝（§18.15 接缝③）：`market_binding` 是
                                # **研究元数据**（来源绑定＋内容指纹），不是量化读数——此前它
                                # 只进 `quant`，被 `publish.store_reading` 白名单拒写，冻结
                                # 目录里就**没有**这份绑定，恢复（load_frozen_result）与报告
                                # 拿不到。现在随 inputs.json（既有文件，不加新类型/新平台）
                                # 冻结，冻结→恢复→报告消费**同一份**绑定；老研究 inputs.json
                                # 里没有这个键，照旧不回填、不补造。
                                "market_binding": quant.get("market_binding") or {}},
              "limits": ["仅值/时间/口径有效的材料参与计算；首次披露日不能证明最新重述值当时可得",
                         "事件与情绪为有引用的规则候选，未用人工标注评估中文准确率",
                         "事件收益为事后价格反应观察；组合账本为既有内部原型，未验证策略有效性",
                         "财报异常是核查线索；标签就绪不等于已训练、校准或可部署的信用/反欺诈模型",
                         "尚未接入正常工作台选版/MD-PDF-ZIP交付；内部研究文件不会自动采纳旧任务"]}
    hash_keys = ["financial", "events", "quant", "risk", "label_readiness", "excluded"]
    if result["q1"] is not None:
        hash_keys.append("q1")
    result["result_hash"] = _hash(_substantive({k: result[k] for k in hash_keys}))
    result["summary_markdown"] = summary_markdown(result)
    return result


def summary_markdown(result: dict) -> str:
    m = result["manifest"]
    lines = ["# 织光离线研究", "", "**模拟数据：只验证功能流程，不证明收益或风险模型有效性。**"
             if m["synthetic"] else "**内部研究草稿；人工未复核，对外数据权限未认证。**",
             "", f"特征截止：{m['feature_cutoff']}；结果评价截止：{m['evaluation_as_of']}。",
             "", "## 财务与现金压力", ""]
    if not result["financial"]:
        lines.append("待财务文件；没有用缺失值生成零金额。")
    for code, row in result["financial"].items():
        lines.extend([f"### {code}", ""])
        for run in row["runs"]:
            lines.append(f"- {run['model_id']}：{run['status']}" +
                         (f"；{run['reason']}" if run.get("reason") else ""))
            if run.get("status") == "validated":
                headline_metrics = {"scenario_net_profit", "revenue_growth_to_hold_target",
                                    "margin_gap_to_threshold_pp", "incremental_capital_occupation",
                                    "cashflow_coverage"}
                for o in [o for o in run.get("outputs", []) if o["metric"] in headline_metrics]:
                    value, unit = o["value"], o.get("unit", "")
                    if unit == "元" and abs(value) >= 100000000:
                        value, unit = value / 100000000, "亿元"
                    elif unit == "元" and abs(value) >= 10000:
                        value, unit = value / 10000, "万元"
                    lines.append(f"  - {o.get('label', o['metric'])}：{value:,.2f} {unit}")
        if row.get("joint_grid"):
            from quant_research import joint_grid
            lines.extend(["", joint_grid.markdown(row["joint_grid"])])
    lines.extend(["", "## 事件与情绪候选", ""])
    for e in result["events"].get("events", []):
        sentiment = e.get("sentiment") or {}
        lines.append(f"- {e.get('code')} · {e.get('type')} · {sentiment.get('label', 'unknown')}："
                     f"{e.get('title', '')}（详情/原文位置见 events.json）")
    lines.append(f"候选 {len(result['events'].get('events', []))}；排除/待原文 "
                 f"{len(result['events'].get('excluded', []))}。")
    lines.extend(["", "## 历史检验", ""])
    _mb = result["quant"].get("market_binding") if isinstance(result["quant"], dict) else None
    if isinstance(_mb, dict):
        _p, _c = _mb.get("price") or {}, _mb.get("calendar") or {}
        lines.append(f"- 来源绑定：{_mb.get('status')}（行情 id 来源 "
                     f"{_mb.get('price_dataset_id_source')}、日历 id 来源 "
                     f"{_mb.get('calendar_id_source')}）")
        if _p:
            lines.append(f"  - 行情数据集：{_p.get('loaded') or _p.get('requested')}"
                         f"（来源 {_p.get('source') or '未标注'}；许可 {_p.get('license') or '未标注'}；"
                         f"复权 {_p.get('adj_basis') or '未标注'}；{_p.get('rows')} 行，"
                         f"{_p.get('date_range')}；内容指纹 "
                         f"{str(_p.get('rows_file_sha256') or '')[:16]}…）")
        if _c:
            lines.append(f"  - 交易日历：{_c.get('loaded') or _c.get('requested')}"
                         f"（来源 {_c.get('source') or '未标注'}；{_c.get('rows')} 个交易日，"
                         f"{_c.get('range')}；内容指纹 "
                         f"{str(_c.get('sessions_file_sha256') or '')[:16]}…）")
        for _why in (_mb.get("reasons") or []):
            lines.append(f"  - 未绑定原因：{_why}")
    for name, r in result["quant"].items():
        if name == "market_binding":
            continue
        lines.append(f"- {name}：{r.get('status')}" + (f"；{r['reason']}" if r.get("reason") else ""))
        if name == "event_returns":
            lines.append("  下一交易日为第1日；以下为收盘价格反应，未模拟成交。")
            for reading in r.get("readings", []):
                for point in reading.get("points", []):
                    if point.get("offset", -1) < 0:
                        continue
                    excess = point.get("excess_return")
                    label = (f"基准调整 {excess * 100:+.2f}%" if excess is not None
                             else f"待数据（{point.get('state')}）")
                    lines.append(f"  - {reading['code']} 第{point['offset'] + 1}交易日：{label}。")
    lines.extend(["", "## 风险研究与标签准备", ""])
    for code, r in result["risk"].items():
        lines.append(f"- {code}：{r.get('status')}；有效特征 {len(r.get('features', []))}，"
                     f"缺口 {len(r.get('gaps', []))}。")
        labels = {"operating_cash_to_consolidated_profit": "经营现金/合并净利润",
                  "receivables_to_revenue": "期末应收/同期收入",
                  "inventory_to_cost": "期末存货/同期成本", "liabilities_to_assets": "总负债/总资产"}
        for f in r.get("features", []):
            if f["name"] in labels:
                lines.append(f"  - {labels[f['name']]}：{f['value'] * 100:.2f}%。")
        clue_labels = {"receivables_outpace_revenue": "应收增长超过收入增长",
                       "inventory_outpaces_cost": "库存增长超过成本增长"}
        for clue in r.get("anomaly_clues", []):
            if clue.get("name") in clue_labels:
                lines.append(f"  - 核查线索：{clue_labels[clue['name']]} {clue['value'] * 100:+.2f} 个百分点。")
                if clue.get("limitations"):
                    lines.append(f"    {clue['limitations'][0]}。")
    for task, r in result["label_readiness"].items():
        lines.append(f"- {task} 标签：{r.get('status')}；尚未训练模型。")
    if result.get("q1"):
        lines.extend(_q1_summary_lines(result["q1"]))
    lines.extend(["", "## 适用边界", ""] + [f"- {s}" for s in result["limits"]])
    lines.extend(["", f"研究ID：`{result['study_id']}`；来源与实现hash见 study_manifest.json。", ""])
    return "\n".join(lines)


def write_study(result: dict, *, output_dir) -> Path:
    """Write an isolated internal workspace. Never overwrite an old study or adopt it."""
    base = Path(output_dir).resolve()
    target = base / result["study_id"][:16]
    target.mkdir(parents=True, exist_ok=False)
    files = {"inputs.json": result["frozen_inputs"], "previews.json": result["previews"],
             "financial.json": result["financial"], "events.json": result["events"],
             "risk.json": result["risk"], "label_readiness.json": result["label_readiness"],
             "excluded.json": result["excluded"]}
    if result.get("q1"):
        files["q1.json"] = result["q1"]
    for name, obj in files.items():
        (target / name).write_bytes(_json_bytes(obj))
    (target / "summary.md").write_text(result["summary_markdown"], encoding="utf-8")
    if result.get("q1"):
        q1md = result["q1"].get("markdown") or {}
        for name, text in (("q1_cards.md", q1md.get("cards")),
                           ("q1_evaluation.md", q1md.get("evaluation"))):
            if text:
                (target / name).write_text(text, encoding="utf-8")
        for code, text in sorted((q1md.get("followup") or {}).items()):
            (target / f"q1_followup_{code}.md").write_text(text, encoding="utf-8")
    from quant_research import publish
    for name, reading in result["quant"].items():
        if name == "market_binding":
            # 研究元数据而非量化读数：已随 inputs.json 冻结（见 _compute_study 的
            # frozen_inputs）。不塞进 quant/ 读数面，也不在 store_reading 里扩白名单
            # ——那是给 event_returns/backtest 这类**读数**的位置（§18.15 接缝③）。
            continue
        publish.store_reading(target, name, reading)
    publish.payload_bytes(target)  # existing quant attachment manifest; still internal
    artifacts = {str(p.relative_to(target)).replace("\\", "/"):
                 hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(target.rglob("*")) if p.is_file()}
    manifest = dict(result["manifest"], artifacts=artifacts, result_hash=result["result_hash"])
    (target / "study_manifest.json").write_bytes(_json_bytes(manifest))
    return target


def verify_study(path) -> dict:
    """Verify bytes only; source validity/model effectiveness remain separate."""
    root = Path(path).resolve()
    manifest = json.loads((root / "study_manifest.json").read_text(encoding="utf-8"))
    checked, failed = [], []
    for name, expected in manifest.get("artifacts", {}).items():
        p = (root / name).resolve()
        if not p.is_relative_to(root) or not p.is_file():
            failed.append(name)
        elif hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            failed.append(name)
        else:
            checked.append(name)
    return {"ok": bool(checked) and not failed, "checked": checked, "failed": failed,
            "what_this_verifies": "内部冻结产物字节；不等于事实正确/策略有效/人工复核"}


def _substantive(value):
    if isinstance(value, dict):
        return {k: _substantive(v) for k, v in value.items()
                if k not in {"started_at", "ended_at", "budget_used"}}
    if isinstance(value, (list, tuple)):
        return [_substantive(v) for v in value]
    return value


def replay_study(path) -> dict:
    """Recompute from the actual frozen records, without the original Wind files."""
    root = Path(path)
    integrity = verify_study(root)
    if not integrity["ok"]:
        return {"ok": False, "reason": "冻结文件hash不符", "integrity": integrity}
    manifest = json.loads((root / "study_manifest.json").read_text(encoding="utf-8"))
    if manifest["implementation_hashes"] != _implementations():
        return {"ok": False, "reason": "实现已改变；需明确生成新的研究版本，不给旧版本背书"}
    frozen = json.loads((root / "inputs.json").read_text(encoding="utf-8"))
    result = _compute_study(frozen["spec"], manifest["sources"], frozen["previews"],
                            frozen["source_records"])
    return {"ok": result["result_hash"] == manifest.get("result_hash"),
            "expected_hash": manifest.get("result_hash"), "actual_hash": result["result_hash"],
            "study_id": result["study_id"], "what_this_verifies": "同冻结输入重算同实质结果"}


def restore_study(path) -> dict:
    """按**研究身份**复用冻结 inputs 重算完整结果（接缝②，2026-10-11 §18.12）。

    冷页面/原上传文件不在时：用 study_id 选定的冻结目录恢复 spec／previews／
    source_records／sources，走与 replay 相同的 `_compute_study`（同一数值路径），
    **不要求重新上传、不借用旧上传目录的输入状态**。产物身份校验照旧：
    冻结文件 hash 不符 → 不恢复；实现已改变 → **不重算**（避免用新实现造新数字），
    改走 `load_frozen_result` 直接读冻结产物。返回值与 `run_study` 同形
    （financial/previews/manifest/quant/…），另带 `restored_from`、
    `result_hash_matches`、`restore_basis` 三条恢复事实。"""
    root = Path(path)
    integrity = verify_study(root)
    if not integrity["ok"]:
        return {"ok": False, "reason": "冻结文件hash不符（不恢复，须重新上传或重跑）",
                "integrity": integrity}
    manifest = json.loads((root / "study_manifest.json").read_text(encoding="utf-8"))
    if manifest["implementation_hashes"] != _implementations():
        return {"ok": False, "implementation_changed": True,
                "reason": "实现已改变；不重算（不给旧版本背书，也不造新数字）——直接读冻结产物",
                "frozen": load_frozen_result(path)}
    frozen = json.loads((root / "inputs.json").read_text(encoding="utf-8"))
    result = _compute_study(frozen["spec"], manifest["sources"], frozen["previews"],
                            frozen["source_records"])
    result["restored_from"] = str(root)
    result["result_hash_matches"] = result["result_hash"] == manifest.get("result_hash")
    result["restore_basis"] = ("按 study_id 选定冻结目录恢复（冻结 inputs + 原 _compute_study）；"
                               "未借用旧上传目录，未重新上传")
    return result


_MISSING = object()


def _spec_diff(base: dict, new: dict, *, _path: str = "") -> list[str]:
    """两个 spec 的**逐路径差异**（可读字符串列表）：只为人读，不参与任何计算。"""
    out: list[str] = []
    keys = sorted(set(base) | set(new))
    for k in keys:
        p = f"{_path}.{k}" if _path else str(k)
        b, n = base.get(k, _MISSING), new.get(k, _MISSING)
        if isinstance(b, dict) and isinstance(n, dict):
            out += _spec_diff(b, n, _path=p)
        elif b is _MISSING:
            out.append(f"{p}: 新增 {json.dumps(n, ensure_ascii=False)}")
        elif n is _MISSING:
            out.append(f"{p}: 删除（原 {json.dumps(b, ensure_ascii=False)}）")
        elif b != n:
            out.append(f"{p}: {json.dumps(b, ensure_ascii=False)} → "
                       f"{json.dumps(n, ensure_ascii=False)}")
    return out


def reparam_frozen_study(path, spec: dict) -> dict:
    """冻结输入 ＋ 改参 → **新候选**（§18.13 C2b 接缝：与「查看原版」严格分开）。

    与 `restore_study` 的区别：这里**故意换 spec**（改预算/天数就是改假设），所以：
    ① 身份必须变——`params_hash=_hash(新spec)` → 内容寻址自然给出**新 study_id**
       （若算出同一身份 ⇒ 拒绝，不冒充新候选）；
    ② 输入侧复用**已校验**的冻结 `previews`/`source_records`/`sources`（不重新上传、
       不借旧上传目录、不读原始文件），数值路径仍是**同一个** `_compute_study`；
    ③ 实现用**当前实现**（新候选只对自己的 spec 负责，不给旧版本背书）；
       旧目录**只读**保留，本函数不写、不改、不删它。
    返回 run_study 同形结果，另带 `reparam_from`/`reparam_spec_diff`/`reparam_basis`。"""
    root = Path(path)
    integrity = verify_study(root)
    if not integrity["ok"]:
        return {"ok": False, "reason": "冻结文件hash不符（不取冻结输入，须重新上传或重跑）",
                "integrity": integrity}
    if not isinstance(spec, dict) or spec.get("schema", SPEC_SCHEMA) != SPEC_SCHEMA:
        return {"ok": False, "reason": "改参必须给出完整 spec（schema 不支持）"}
    manifest = json.loads((root / "study_manifest.json").read_text(encoding="utf-8"))
    frozen = json.loads((root / "inputs.json").read_text(encoding="utf-8"))
    base = frozen.get("spec") or {}
    diff = _spec_diff(base if isinstance(base, dict) else {}, spec)
    if not diff:
        return {"ok": False, "code": "spec_unchanged",
                "reason": "给出的 spec 与冻结 spec 逐字段相同：这是「查看原版」，"
                          "不新建候选（要改参数就改一个值再提交）"}
    result = _compute_study(json.loads(json.dumps(spec, ensure_ascii=False)),
                            dict(manifest.get("sources") or {}),
                            frozen.get("previews") or {}, frozen.get("source_records") or {})
    old_id = str(manifest.get("study_id") or root.name)
    new_id = str(result.get("study_id") or "")
    if new_id == old_id:
        return {"ok": False, "code": "identity_unchanged",
                "reason": "改参后研究身份没有变：拒绝把同一身份冒充成新候选"}
    result["reparam_from"] = {"study_id": old_id, "dir": str(root),
                              "result_hash": str(manifest.get("result_hash") or ""),
                              "frozen_integrity_ok": True,
                              "frozen_impl_version": str(manifest.get("impl_version") or "")}
    result["reparam_spec_diff"] = diff
    result["reparam_basis"] = ("冻结输入（已校验）＋改参 → 新候选：同一 `_compute_study` 数值路径、"
                               "当前实现、内容寻址新身份；旧目录只读未动")
    result["spec"] = json.loads(json.dumps(spec, ensure_ascii=False))
    return result


def load_frozen_result(path) -> dict:
    """把冻结产物**原样**重装成 run_study 同形的结果（不重算、不背书）。

    实现已改变时用这条：`_study_preview_payload` 需要的每块（financial/previews/
    events/risk/label_readiness/quant/manifest）都从冻结文件读，数值一字不改；
    `recomputed` 恒为 False，调用方照实标「直接读冻结产物」。"""
    from quant_research import publish
    root = Path(path)

    def _rd(name):
        p = root / name
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except (ValueError, OSError):
            return {}

    man = _rd("study_manifest.json")
    frozen = _rd("inputs.json")
    # C2d 接缝③：来源绑定随 inputs.json 冻结，恢复时**原样送回** `quant`，让冻结—恢复—
    # 报告消费同一份绑定。老研究没有这个键 ⇒ 照旧缺席（不补造、不拿 spec 现算冒充冻结值）。
    quant = publish.load_readings(root)
    _mb = frozen.get("market_binding") or {}
    if _mb:
        quant["market_binding"] = _mb
    return {"ok": True, "recomputed": False,
            "financial": _rd("financial.json"), "previews": _rd("previews.json"),
            "events": _rd("events.json"), "risk": _rd("risk.json"),
            "label_readiness": _rd("label_readiness.json"),
            "quant": quant,
            "study_id": str(man.get("study_id") or root.name),
            "result_hash": str(man.get("result_hash") or ""),
            "manifest": man,
            "restored_from": str(root),
            "result_hash_matches": True,
            "restore_basis": "实现已改变：直接读冻结产物（不重算、不冒充重算结果）"}
