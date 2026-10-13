"""Point-in-time financial clues, with explicit missing-data boundaries.

Input is existing Observation-like objects or dictionaries. A source envelope
must preserve disclosed_at/available_at and a locator/hash. Observation.as_of
is a research snapshot date, not evidence that the source was public then.
All amounts are converted to their currency's base monetary unit; currencies
are never converted into one another. Ratios use closing balances, not averages.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time, timezone, timedelta
from decimal import Decimal, DecimalException
import hashlib
import json
import math

SCHEMA = "weavemind.risk_features/1"
_LOCAL = timezone(timedelta(hours=8))
_UNITS = {"元": 1, "千元": 1000, "万元": 10000, "百万元": 1000000,
          "亿元": 100000000, "人民币元": 1, "人民币万元": 10000,
          "人民币亿元": 100000000, "yuan": 1, "CNY": 1,
          "USD": 1, "HKD": 1, "EUR": 1, "GBP": 1,
          "thousand": 1000, "million": 1000000, "billion": 1000000000}
_ALIASES = {"operating_cash_flow": "operating_cashflow",
            "consolidated_net_profit": "net_profit_consolidated",
            "short_term_interest_bearing_debt": "interest_bearing_debt_due_within_one_year"}
_ROLE = {"operating_cashflow": "flow", "net_profit_consolidated": "flow",
         "revenue": "flow", "operating_cost": "flow",
         "accounts_receivable": "stock", "inventory": "stock",
         "total_assets": "stock", "total_liabilities": "stock",
         "available_cash": "stock", "interest_bearing_debt_due_within_one_year": "stock"}


def _get(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    default=str, separators=(",", ":")).encode()).hexdigest()


def _finite_number(value):
    try:
        converted = float(value)
        return converted if math.isfinite(converted) else None
    except (OverflowError, ValueError):
        return None


def _moment(value, *, cutoff=False):
    """ISO date/time; naive values use Asia/Shanghai, date cutoff includes day."""
    text = str(value or "").strip()
    if len(text) == 10:
        day = datetime.strptime(text, "%Y-%m-%d").date()
        return datetime.combine(day, time.max if cutoff else time.min, _LOCAL)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=_LOCAL) if parsed.tzinfo is None else parsed


def _source(record, cutoff):
    source = _get(record, "source", {})
    # Some importers use a source label string and retain dates/hash at top level.
    source = source if isinstance(source, dict) else {}
    # Import envelopes may keep these at top level. A restated value cannot
    # borrow the first publication date of the original filing.
    date_keys = ("disclosed_at", "available_at", "version_available_at")
    dates = [value for key in date_keys
             for value in (source.get(key), _get(record, key)) if value]
    locator = any(source.get(k) for k in ("url", "path", "file", "source_id", "hash", "sha256"))
    locator = locator or _get(record, "source_url") or _get(record, "source_hash")
    if not dates or not locator:
        return None, "public_availability_evidence_missing"
    try:
        # Date-only publication is conservatively known after that full day;
        # it must not create an invented midnight or intraday publication time.
        available = max(_moment(v, cutoff=True) for v in dates).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None, "public_availability_invalid"
    if available > cutoff:
        return None, "not_available_at_cutoff"
    return available.isoformat(), ""


def _normalise(record, entity_id, cutoff):
    metric = _ALIASES.get(str(_get(record, "metric", "")), str(_get(record, "metric", "")))
    ident = str(_get(record, "fact_id") or _get(record, "observation_id") or "")
    if metric not in _ROLE:
        return None, "unsupported_metric", ident
    if str(_get(record, "entity_id", "")) != entity_id:
        return None, "entity_mismatch", ident
    if _get(record, "state", "ok") not in ("ok", "zero"):
        return None, "observation_not_usable", ident
    if _get(record, "caliber") not in ("合并", "consolidated"):
        return None, "consolidated_scope_required", ident
    currency = str(_get(record, "currency", "")).upper()
    currency = "CNY" if currency == "RMB" else currency
    unit = str(_get(record, "unit", ""))
    if not ident or not currency or unit not in _UNITS:
        return None, "amount_identity_or_unit_missing", ident
    if (unit in ("CNY", "USD", "HKD", "EUR", "GBP") and unit != currency or
            (unit.startswith("人民币") or unit == "yuan") and currency != "CNY"):
        return None, "unit_currency_mismatch", ident
    try:
        value = _get(record, "value")
        if isinstance(value, bool) or value is None:
            raise ValueError("not amount")
        amount = Decimal(str(value)) * Decimal(_UNITS[unit])
        if not amount.is_finite():
            raise ValueError("not finite")
    except (DecimalException, TypeError, ValueError):
        return None, "invalid_amount", ident
    if _get(record, "period_kind") != _ROLE[metric]:
        return None, "period_kind_missing_or_wrong", ident
    try:
        end = datetime.strptime(str(_get(record, "period_end", "")), "%Y-%m-%d").date()
        start = None
        if _ROLE[metric] == "flow":
            start = datetime.strptime(str(_get(record, "period_start", "")), "%Y-%m-%d").date()
            if start > end:
                raise ValueError("reversed interval")
        if not _get(record, "period") or end > cutoff.date():
            raise ValueError("period missing or future")
    except (ValueError, TypeError):
        return None, "period_identity_invalid", ident
    available, reason = _source(record, cutoff)
    if reason:
        return None, reason, ident
    if _moment(available).astimezone(_LOCAL).date() < end:
        return None, "financial_period_not_complete_at_availability", ident
    return {"id": ident, "metric": metric, "amount": amount, "currency": currency,
            "period": str(_get(record, "period")), "start": start, "end": end,
            "available_at": available, "input": record}, "", ident


def build(observations, *, entity_id, as_of, events=()):
    """Build current ratios and two-period clues; never return a rating or PD.

    Source metadata is mandatory, even for synthetic observations. Derived
    ratios retain input IDs and cut-off identity. Missing values remain gaps.
    """
    raw = list(observations)
    event_rows = list(events)
    result = {"schema": SCHEMA, "status": "insufficient_data", "entity_id": str(entity_id),
              "feature_cutoff": str(as_of), "features": [], "gaps": [], "anomaly_clues": [],
              "input_hash": _hash({"entity_id": entity_id, "as_of": as_of,
                                   "observations": raw, "events": event_rows}),
              "limitations": ["研究线索，不是信用评级、违约概率或舞弊认定",
                               "日期精度只证明该日可知，不证明日内任一交易时点可知",
                               "余额比率采用期末余额，不是平均余额周转指标"]}
    synthetic = [bool(_get(row, "synthetic", False)) for row in raw]
    result["data_kind"] = ("synthetic" if synthetic and all(synthetic) else
                           "mixed" if any(synthetic) else "observed")
    try:
        cutoff = _moment(as_of, cutoff=True)
    except (ValueError, TypeError):
        result["gaps"].append({"code": "invalid_feature_cutoff"})
        return result
    if not entity_id:
        result["gaps"].append({"code": "entity_id_required"})
        return result
    normal = []
    for record in raw:
        row, reason, ident = _normalise(record, str(entity_id), cutoff)
        if row is not None:
            normal.append(row)
        elif reason != "unsupported_metric":
            result["gaps"].append({"code": reason, "input_id": ident})
    buckets = defaultdict(list)
    for row in normal:
        buckets[(row["metric"], row["end"])].append(row)
    usable = {}
    for key, group in buckets.items():
        identities = {(r["amount"], r["currency"], r["start"]) for r in group}
        if len(identities) > 1:
            result["gaps"].append({"code": "conflicting_metric_period", "metric": key[0],
                                   "input_ids": [r["id"] for r in group]})
        else:
            # Same amount duplicates are not averaged; all source IDs remain visible.
            chosen = dict(group[0])
            chosen["ids"] = sorted({r["id"] for r in group})
            chosen["available_at"] = max(r["available_at"] for r in group)
            chosen["synthetic"] = any(_get(r["input"], "synthetic", False) for r in group)
            usable[key] = chosen
    dates = sorted({end for _, end in buckets})
    if not dates:
        result["gaps"].append({"code": "no_admitted_financial_observations"})
        return result
    current = dates[-1]

    def pick(metric, end=current):
        return usable.get((metric, end))

    def compatible(rows):
        return (len({r["currency"] for r in rows}) == 1 and
                len({r["end"] for r in rows}) == 1 and
                len({r["start"] for r in rows if r["start"] is not None}) <= 1)

    def ratio(name, numerator, denominator, limitation):
        a, b = pick(numerator), pick(denominator)
        missing = [metric for metric, row in ((numerator, a), (denominator, b)) if row is None]
        if missing:
            result["gaps"].append({"code": "feature_inputs_missing", "feature": name, "metrics": missing})
            return
        if not compatible([a, b]):
            result["gaps"].append({"code": "feature_input_scope_mismatch", "feature": name})
            return
        if b["amount"] <= 0 or (numerator != "operating_cashflow" and a["amount"] < 0):
            result["gaps"].append({"code": "nonpositive_denominator_or_negative_balance", "feature": name})
            return
        try:
            value = _finite_number(a["amount"] / b["amount"])
        except DecimalException:
            value = None
        if value is None:
            result["gaps"].append({"code": "derived_value_not_finite", "feature": name})
            return
        result["features"].append({"name": name, "value": value,
                                   "entity_id": str(entity_id), "unit": "ratio", "period": b["period"],
                                   "period_end": current.isoformat(),
                                   "input_ids": a["ids"] + b["ids"], "currency": a["currency"],
                                   "formula": f"{numerator} / {denominator} (base monetary units)",
                                   "available_at": max(a["available_at"], b["available_at"]),
                                   "synthetic": bool(a["synthetic"] or b["synthetic"]),
                                   "limitations": [limitation]})

    ratio("operating_cash_to_consolidated_profit", "operating_cashflow", "net_profit_consolidated",
          "合并净利润必须为正；现金流受营运资本和非现金项目影响，低比率不等于失信")
    ratio("receivables_to_revenue", "accounts_receivable", "revenue",
          "期末应收/同期收入，受季节性与结算节奏影响，不是平均应收周转率")
    ratio("inventory_to_cost", "inventory", "operating_cost",
          "期末库存/同期成本，备货与交付周期可能形成正常差异")
    ratio("liabilities_to_assets", "total_liabilities", "total_assets",
          "总负债包含经营性负债；该比率不能替代有息负债结构与偿债期限分析")
    ratio("cash_to_short_debt", "available_cash", "interest_bearing_debt_due_within_one_year",
          "仅明确可用现金与一年内到期有息债务；不把货币资金替代可用现金，不含未承诺授信")

    for stock, flow, clue_name, explanation in (
        ("accounts_receivable", "revenue", "receivables_outpace_revenue",
         "正常替代解释：结算日期、信用期变化、客户结构或合并范围变化；补账龄与合同条款核实"),
        ("inventory", "operating_cost", "inventory_outpaces_cost",
         "正常替代解释：季节备货、产能投放、交付延后或成本价格变化；补库龄与订单核实"),
    ):
        s1, f1 = pick(stock), pick(flow)
        if s1 is None or f1 is None:
            continue
        prior_dates = [d for d in dates if d.year == current.year - 1 and
                       (d.month, d.day) == (current.month, current.day)]
        if not prior_dates:
            result["gaps"].append({"code": "comparable_prior_period_missing", "clue": clue_name})
            continue
        previous = prior_dates[-1]
        s0, f0 = pick(stock, previous), pick(flow, previous)
        if not s0 or not f0:
            result["gaps"].append({"code": "comparable_prior_inputs_missing", "clue": clue_name})
            continue
        pairs_ok = compatible([s1, f1]) and compatible([s0, f0])
        starts_ok = ((f1["start"].month, f1["start"].day) == (f0["start"].month, f0["start"].day) and
                     f1["start"].year == f0["start"].year + 1)
        if not pairs_ok or not starts_ok or s0["currency"] != s1["currency"]:
            result["gaps"].append({"code": "prior_period_scope_mismatch", "clue": clue_name})
            continue
        if s0["amount"] <= 0 or f0["amount"] <= 0 or s1["amount"] < 0 or f1["amount"] <= 0:
            result["gaps"].append({"code": "growth_baseline_nonpositive", "clue": clue_name})
            continue
        try:
            sg = s1["amount"] / s0["amount"] - 1
            fg = f1["amount"] / f0["amount"] - 1
            values = tuple(_finite_number(v) for v in (sg - fg, sg, fg))
        except DecimalException:
            values = (None, None, None)
        if any(v is None for v in values):
            result["gaps"].append({"code": "derived_value_not_finite", "clue": clue_name})
            continue
        if sg > fg:
            result["anomaly_clues"].append({"name": clue_name, "period": f1["period"],
                                            "prior_period": f0["period"], "value": values[0],
                                            "unit": "growth_rate_difference", "stock_growth": values[1],
                                            "flow_growth": values[2], "severity": "review_clue",
                                            "synthetic": any(r["synthetic"] for r in (s0, s1, f0, f1)),
                                            "input_ids": sum((r["ids"] for r in (s0, s1, f0, f1)), []),
                                            "formula": f"growth({stock}) - growth({flow})",
                                            "limitations": [explanation, "差异线索不是舞弊或违约标签"]})
    for event in event_rows:
        event_id = str(_get(event, "event_id", ""))
        if str(_get(event, "entity_id", "")) != str(entity_id) or not event_id:
            result["gaps"].append({"code": "event_identity_invalid", "input_id": event_id})
            continue
        available, reason = _source(event, cutoff)
        if reason:
            result["gaps"].append({"code": reason, "input_id": event_id})
            continue
        result["anomaly_clues"].append({"name": "disclosed_event_to_review", "input_ids": [event_id],
                                        "event_type": _get(event, "event_type", "unknown"),
                                        "available_at": available, "severity": "review_clue",
                                        "synthetic": bool(_get(event, "synthetic", False)),
                                        "limitations": ["来源事件只作为复核入口，不能自动转为信用或舞弊结论"]})
    if result["features"]:
        result["status"] = ("synthetic_research_features" if result["data_kind"] == "synthetic"
                            else "research_features_available")
    return result
