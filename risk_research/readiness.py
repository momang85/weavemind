"""Admit labelled records for future offline credit/fraud research.

This module does not fit a model. Raw public statements lack credit outcomes
and transaction fraud labels, so they must not silently become negative cases.
Synthetic records test plumbing separately and can never establish readiness
for validation on real customers. All functions are offline and standard-library.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timezone, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math

SCHEMA = "weavemind.risk_readiness/1"
_LOCAL = timezone(timedelta(hours=8))


def _moment(value, *, cutoff=False):
    text = str(value or "").strip()
    if len(text) == 10:
        day = datetime.strptime(text, "%Y-%m-%d").date()
        return datetime.combine(day, time.max if cutoff else time.min, _LOCAL)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=_LOCAL) if parsed.tzinfo is None else parsed


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def _label(row):
    value = row.get("label")
    # Reject booleans, strings and missing/pending outcomes; no implicit int().
    if type(value) is int and value in (0, 1):
        return value
    return None


def _record(row, task, evaluation):
    reasons = []
    if not isinstance(row, dict):
        return None, ["record_must_be_mapping"]
    record_id = str(row.get("record_id") or "")
    group_key = "entity_id" if task == "credit" else "account_id"
    group_id = str(row.get(group_key) or "")
    if not record_id or not group_id:
        reasons.append("record_and_subject_identity_required")
    decision = None
    try:
        decision = _moment(row.get("decision_at"))
        if decision > evaluation:
            reasons.append("decision_after_evaluation")
    except (ValueError, TypeError):
        reasons.append("decision_at_missing_or_invalid")
    auth = row.get("authorization", {})
    if not isinstance(auth, dict) or auth.get("internal") is not True or auth.get("training") is not True:
        reasons.append("internal_and_training_authorization_required")
    elif not (auth.get("source") or auth.get("reference")):
        reasons.append("authorization_reference_required")
    label = _label(row)
    if label is None:
        reasons.append("label_pending_or_missing" if row.get("label") in (None, "pending", "")
                       else "label_must_be_binary_integer")
    definition = str(row.get("label_definition") or "").strip()
    if not definition:
        reasons.append("label_definition_required")
    # First version has one verifiable duration representation. Descriptions
    # such as "one month" or arbitrary horizon text are not calculation inputs.
    horizon_days = row.get("horizon_days")
    valid_horizon = type(horizon_days) is int and horizon_days > 0
    if not valid_horizon:
        reasons.append("horizon_days_must_be_positive_integer")
    label_available = outcome_end = None
    try:
        label_available = _moment(row.get("label_available_at"), cutoff=True)
        if label_available > evaluation:
            reasons.append("label_not_available_at_evaluation")
        if decision is not None and label_available < decision:
            reasons.append("label_precedes_decision")
    except (ValueError, TypeError):
        reasons.append("label_available_at_missing_or_invalid")
    try:
        # Date-only windows mature after the full outcome day, not at its start.
        outcome_end = _moment(row.get("outcome_end"), cutoff=True)
        if outcome_end > evaluation:
            reasons.append("outcome_window_not_mature")
        if decision is not None and outcome_end <= decision:
            reasons.append("outcome_window_must_follow_decision")
    except (ValueError, TypeError):
        reasons.append("outcome_end_missing_or_invalid")
    if label == 0 and label_available is not None and outcome_end is not None:
        if label_available < outcome_end:
            reasons.append("negative_label_available_before_outcome_end")
    if decision and outcome_end and valid_horizon:
        if (outcome_end.date() - decision.date()).days != horizon_days:
            reasons.append("outcome_end_does_not_match_horizon_days")
    features = row.get("features")
    admitted_features = {}
    if not isinstance(features, list) or not features:
        reasons.append("dated_feature_list_required")
    else:
        for feature in features:
            if not isinstance(feature, dict):
                reasons.append("feature_must_be_mapping")
                continue
            name = str(feature.get("name") or "")
            if not name or name in admitted_features:
                reasons.append("feature_names_required_and_unique")
                continue
            feature_subject = feature.get(group_key)
            if feature_subject is None:
                feature_subject = feature.get("subject_id")
            if str(feature_subject or "") != group_id:
                reasons.append("feature_subject_mismatch_or_missing")
            source = feature.get("source", {})
            source = source if isinstance(source, dict) else {}
            dates = [v for key in ("disclosed_at", "available_at", "version_available_at")
                     for v in (feature.get(key), source.get(key)) if v]
            if not dates:
                reasons.append("feature_availability_required")
            else:
                try:
                    available = max(_moment(v, cutoff=True) for v in dates)
                    if decision is not None and available > decision:
                        reasons.append("feature_not_known_at_decision")
                except (ValueError, TypeError):
                    reasons.append("feature_availability_invalid")
            try:
                value = feature.get("value")
                if isinstance(value, bool) or value is None:
                    raise ValueError("missing or bool")
                value = Decimal(str(value))
                if not value.is_finite():
                    raise ValueError("not finite")
                converted = float(value)
                if not math.isfinite(converted):
                    raise ValueError("not representable as finite model input")
                admitted_features[name] = converted
            except (InvalidOperation, TypeError, ValueError, OverflowError):
                reasons.append("feature_value_missing_or_invalid")
    if reasons:
        return None, sorted(set(reasons))
    return {"record_id": record_id, "group_id": group_id, group_key: group_id,
            "decision_at": decision.astimezone(timezone.utc).isoformat(),
            "features": admitted_features, "label": label, "label_definition": definition,
            "horizon_days": horizon_days, "label_available_at": label_available.isoformat(),
            "outcome_end": outcome_end.isoformat(), "synthetic": row.get("synthetic") is True}, []


def assess(records, *, task, evaluation_as_of):
    """Check input contract before any future model training.

    A record has record_id, entity_id (credit) or account_id (fraud), decision_at,
    features [{name,value,entity_id/account_id,available_at}], label 0/1,
    label_available_at, outcome_end, label_definition, positive integer horizon_days,
    authorization {internal:true,training:true,reference/source:...}, synthetic.
    At least two groups, three decision dates and both classes are the minimum
    *schema demonstration* for offline validation, not statistical adequacy.
    """
    rows = list(records)
    result = {"schema": SCHEMA, "task": str(task), "status": "blocked",
              "evaluation_as_of": str(evaluation_as_of), "eligible_records": [],
              "synthetic_records": [], "excluded_records": [], "requirements": [],
              "input_hash": hashlib.sha256(_canonical(rows).encode()).hexdigest(),
              "limitations": ["只检查离线建模资料，不代表模型已训练、已校准或适合生产",
                               "不输出PD、信用等级、授信决策或反欺诈评分",
                               "至少两类、多主体与多日期只是最小流程条件，真实样本量和代表性须另行评估",
                               "合成样本只证明流程，公开财报不能替代真实违约/交易标签"],
              "validation_contract": {"split": "forward_time_with_subject_group_control",
                                      "group_key": "entity_id" if task == "credit" else "account_id",
                                      "require_train_labels_known_before_validation_decision": True,
                                      "label_missing_policy": "exclude_never_fill_zero",
                                      "feature_missing_policy": "explicit_gap_no_zero_fill"}}
    if task not in ("credit", "fraud"):
        result["requirements"].append("task_must_be_credit_or_fraud")
        return result
    try:
        evaluation = _moment(evaluation_as_of, cutoff=True)
    except (ValueError, TypeError):
        result["requirements"].append("evaluation_as_of_missing_or_invalid")
        return result
    seen = set()
    for index, row in enumerate(rows):
        admitted, reasons = _record(row, task, evaluation)
        ident = str(row.get("record_id") or f"row:{index}") if isinstance(row, dict) else f"row:{index}"
        if admitted is not None and ident in seen:
            admitted, reasons = None, ["duplicate_record_id"]
        seen.add(ident)
        if reasons:
            result["excluded_records"].append({"record_id": ident, "reasons": reasons,
                                               "synthetic": isinstance(row, dict) and row.get("synthetic") is True})
        elif admitted["synthetic"]:
            result["synthetic_records"].append(admitted)
        else:
            result["eligible_records"].append(admitted)
    admitted = result["eligible_records"]
    result["counts"] = {"total": len(rows), "eligible": len(admitted),
                        "synthetic_eligible": len(result["synthetic_records"]),
                        "excluded": len(result["excluded_records"]),
                        "groups": len({r["group_id"] for r in admitted}),
                        "decision_dates": len({r["decision_at"][:10] for r in admitted}),
                        "label_counts": dict(Counter(r["label"] for r in admitted))}
    if not admitted:
        if not rows:
            result["status"] = "waiting_labels"
            result["requirements"].append("authorized_real_labelled_records_required")
            return result
        real_rows = [r for r in rows if not isinstance(r, dict) or r.get("synthetic") is not True]
        waiting_codes = {"label_pending_or_missing", "label_not_available_at_evaluation", "outcome_window_not_mature"}
        real_exclusions = [r for r in result["excluded_records"] if not r["synthetic"]]
        if not real_rows and result["synthetic_records"]:
            result["status"] = "synthetic_contract_only"
            result["requirements"].append("authorized_real_labelled_data_required")
        elif real_exclusions and all(waiting_codes.intersection(r["reasons"]) for r in real_exclusions):
            result["status"] = "waiting_labels"
            result["requirements"].append("mature_authorized_binary_outcomes_required")
        else:
            result["requirements"].append("eligible_real_records_required")
        return result
    if len({r["label"] for r in admitted}) < 2:
        result["requirements"].append("both_label_classes_required")
    if result["counts"]["groups"] < 2:
        result["requirements"].append("multiple_subjects_required")
    if result["counts"]["decision_dates"] < 3:
        result["requirements"].append("at_least_three_decision_dates_required")
    definitions = {(r["label_definition"], r["horizon_days"]) for r in admitted}
    if len(definitions) > 1:
        result["requirements"].append("one_label_definition_and_horizon_per_cohort_required")
    feature_sets = {tuple(sorted(r["features"])) for r in admitted}
    if len(feature_sets) > 1:
        result["requirements"].append("consistent_feature_schema_required_no_zero_padding")
    if not result["requirements"]:
        result["status"] = "ready_for_offline_validation"
    return result
