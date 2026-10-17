# -*- coding: utf-8 -*-
"""有原文定位的金融事件候选；纯函数、标准库、无模型调用。

首期只识别业绩指引、订单需求、融资偿债。规则命中不是事件事实认证，
情绪标签不是经校准的预测。调用者需提供源记录自己的 code，不能用任务
公司替换。source_hash 的契约是原文 text 的 UTF-8 SHA256（非 PDF 文件 hash）。
显式 candidates 同样必须携带可在 text 中核准的 quote/start/end。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone
from collections.abc import Mapping

SCHEMA = "weavemind.financial_events/0"
IMPL_VERSION = "financial_events/0.1.0"
EVENT_TYPES = ("earnings_guidance", "orders_demand", "financing_repayment")
TYPE_LABELS = {
    "earnings_guidance": "业绩指引", "orders_demand": "订单需求",
    "financing_repayment": "融资偿债",
}
_RULES = {
    "earnings_guidance": r"业绩预告|业绩指引|预增|预减|预亏|预盈|预计.{0,25}(?:净利润|营业收入|业绩)|(?:净利润|营业收入).{0,25}(?:预计|目标)",
    "orders_demand": r"订单|中标|签订.{0,12}合同|签署.{0,12}合同|需求(?:增长|下降|回暖|减少)|销售需求|采购需求",
    "financing_repayment": r"融资|发行.{0,12}债券|发行.{0,12}票据|借款|贷款|偿还|兑付|违约|逾期",
}
_PLAN = re.compile(r"拟|计划|预计|目标|意向|框架协议|尚未|有望|可能|待.{0,6}(?:审批|批准)")
_NEGATED = re.compile(r"尚未|并未|未(?:签署|签订|中标|完成|发生)|不构成|否认|不存在|没有")
_ALLEGED = re.compile(r"指控|涉嫌|传闻|据称|被指")
_NEGATIVE = re.compile(r"下降|下滑|减少|预亏|预减|亏损|违约|逾期|取消|终止|下降|需求疲软")
_POSITIVE = re.compile(r"增长|增加|预增|预盈|扭亏|回暖|中标|签订|签署|完成兑付|按期偿还")
_SOURCE_ROLES = ("company_statement", "observation", "plan", "allegation")
_LOCAL = timezone(timedelta(hours=8))
LIMITS = (
    "三类词典候选，不是通用事件抽取模型；命中可能包含历史回顾、引用他人或歧义，需人工核对主体。",
    "情绪为实体级规则候选，没有校准概率；不代表投资信号、因果或未来收益。",
    "签约/中标不等于确认收入；计划与业绩指引不等于实现；负面报道/指控不等于舞弊成立。",
    "仅按提供的公开时点筛选；历史更正应独立提供版本公开时间，不得用旧日期倒灌修订正文。",
    "只有日期的材料按该日结束才视为可得，未臆造盘中时间；晚采集的原文是历史重建，不证明当时系统已持有。",
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash(value) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str))


def _moment(value, *, end_of_day=False):
    """(比较时点, 精度, 时区依据)。日精度按中国市场本地日界，原串另存。"""
    raw = str(value or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            d = datetime.strptime(raw, "%Y-%m-%d").date()
            return (datetime.combine(d, time.max if end_of_day else time.min, _LOCAL),
                    "day", "market_local_day:Asia/Shanghai")
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if "T" not in raw and " " not in raw:
            return None, "unknown", ""
        if dt.tzinfo is None:
            return None, "timestamp", "timezone_unknown"
        return dt, "timestamp", "source_offset"
    except (TypeError, ValueError):
        return None, "unknown", ""


def _time_identity(value):
    _, precision, basis = _moment(value)
    return {"value": str(value or ""), "precision": precision, "basis": basis}


def _automatic(text):
    # 不改正文：引文与字符位置始终指向输入的实际 text。
    for match in re.finditer(r"[^。！？!?\n]+[。！？!?]?", text):
        quote = match.group()
        stripped = quote.strip()
        if not stripped:
            continue
        start = match.start() + len(quote) - len(quote.lstrip())
        for kind, rule in _RULES.items():
            if re.search(rule, stripped):
                yield {"type": kind, "quote": stripped,
                       "start": start, "end": start + len(stripped)}


def _span(candidate, text):
    quote = str(candidate.get("quote") or "")
    if not quote:
        return None, "missing_quote"
    start, end = candidate.get("start"), candidate.get("end")
    if start is None and end is None:
        if text.count(quote) != 1:
            return None, "ambiguous_quote_span"
        start, end = text.find(quote), text.find(quote) + len(quote)
    if (type(start) is not int or type(end) is not int or
            start < 0 or end <= start or end > len(text) or text[start:end] != quote):
        return None, "quote_span_mismatch"
    return {"quote": quote, "start": start, "end": end,
            "offset_unit": "unicode_character"}, ""


def _tone(quote, record, candidate):
    if _ALLEGED.search(quote):
        role, modality = "allegation", "alleged"
    elif _NEGATED.search(quote):
        role, modality = "plan", "negated"
    elif _PLAN.search(quote):
        role, modality = "plan", "planned"
    else:
        declared = str(candidate.get("role") or record.get("evidence_role") or "")
        role = declared if declared in _SOURCE_ROLES else (
            "company_statement" if record.get("source_kind") in (
                "company", "official_disclosure") else "observation")
        modality = "alleged" if role == "allegation" else (
            "planned" if role == "plan" else "asserted")
    negative, positive = bool(_NEGATIVE.search(quote)), bool(_POSITIVE.search(quote))
    direction = ("unknown" if modality in ("negated", "alleged") or (negative and positive)
                 else "negative" if negative else "positive" if positive else "neutral")
    return role, modality, direction


def extract(records, *, as_of="") -> dict:
    """源材料列表 → 可核准引文的候选。既不落库也不修改输入。

    record 必需 code/text/available_at/source；发布时间、published_at 也可作
    available_at 来源。correction_of 独立保留，不删除原版。
    若提供 as_of，它必须为 YYYY-MM-DD 或带 UTC offset 的 ISO 时间。
    提供 candidates 时不再自动补候选，避免把人工范围外的句子混入。
    """
    records = list(records or [])
    cutoff, _, _ = _moment(as_of, end_of_day=True) if as_of else (None, "", "")
    batch = {"schema": SCHEMA, "impl_version": IMPL_VERSION,
             "input_hash": _hash({"records": records, "as_of": as_of}),
             "as_of": str(as_of or ""), "status": "unavailable", "events": [],
             "excluded": [], "limits": list(LIMITS)}
    if as_of and cutoff is None:
        batch["excluded"].append({"reason": "invalid_as_of", "as_of": as_of})
        return batch
    seen = {}
    for index, record in enumerate(records):
        meta = {"record_index": index}
        if not isinstance(record, Mapping):
            batch["excluded"].append({**meta, "reason": "invalid_record"})
            continue
        code = str(record.get("code") or "").strip()
        meta.update({"code": code, "material_id": str(record.get("material_id") or "")})
        def exclude(reason, **details):
            batch["excluded"].append({**meta, "reason": reason, **details})
        if not code:
            exclude("missing_source_subject")
            continue
        text = str(record.get("text") or "")
        if not text.strip():
            exclude("needs_original", title=str(record.get("title") or ""))
            continue
        source = str(record.get("source") or "").strip()
        if not source:
            exclude("missing_source")
            continue
        source_hash = _sha(text)
        declared_hash = str(record.get("source_hash") or "").removeprefix("sha256:")
        if declared_hash and declared_hash.lower() != source_hash:
            exclude("source_hash_mismatch", computed_source_hash=source_hash)
            continue
        available = str(record.get("available_at") or record.get("published_at") or
                        record.get("发布时间") or "").strip()
        known_at, precision, time_basis = _moment(available, end_of_day=True)
        if known_at is None:
            exclude("unverifiable_public_time", available_at=available)
            continue
        if cutoff is not None and known_at > cutoff:
            exclude("not_yet_public", available_at=available)
            continue
        candidates = record.get("candidates")
        if candidates is None:
            candidates = list(_automatic(text))
        if not isinstance(candidates, (list, tuple)):
            exclude("invalid_candidates")
            continue
        if not candidates:
            exclude("no_supported_event")
        for ci, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping):
                exclude("invalid_candidate", candidate_index=ci)
                continue
            kind = str(candidate.get("type") or "")
            if kind not in EVENT_TYPES:
                exclude("unsupported_event_type", candidate_index=ci, type=kind)
                continue
            if candidate.get("code") and str(candidate["code"]) != code:
                exclude("candidate_subject_mismatch", candidate_index=ci)
                continue
            if (candidate.get("company") and str(candidate["company"]) !=
                    str(record.get("company") or "")):
                exclude("candidate_subject_mismatch", candidate_index=ci)
                continue
            candidate_hash = str(candidate.get("source_hash") or "").removeprefix("sha256:")
            if candidate_hash and candidate_hash.lower() != source_hash:
                exclude("candidate_source_hash_mismatch", candidate_index=ci)
                continue
            if (candidate.get("material_id") and str(candidate["material_id"]) !=
                    str(record.get("material_id") or "")):
                exclude("candidate_material_mismatch", candidate_index=ci)
                continue
            span, reason = _span(candidate, text)
            if reason:
                exclude(reason, candidate_index=ci)
                continue
            origin = str(record.get("origin_id") or "")
            correction = str(record.get("correction_of") or "")
            event_at = str(candidate.get("event_at") or record.get("event_at") or "")
            quote_key = re.sub(r"\s+", "", span["quote"])
            # 转载按同主体/同原件/同候选去重；更正永远是独立版本。
            revision = ((correction, str(record.get("version_id") or source_hash), available)
                        if correction else ())
            key = (code, kind, origin or source_hash, event_at, quote_key,
                   revision, available[:10] if not origin else "")
            if key in seen:
                exclude("duplicate_reprint", candidate_index=ci,
                        duplicate_of=seen[key], source_hash=source_hash)
                continue
            event_key = _hash([code, kind, str(candidate.get("event_id") or ""),
                               origin or source_hash, event_at, quote_key])
            version = str(record.get("version_id") or _hash(
                [source_hash, available, correction]))
            role, modality, direction = _tone(span["quote"], record, candidate)
            ingestion = str(record.get("ingested_at") or "")
            ingested_time, _, _ = _moment(ingestion, end_of_day=True)
            event = {
                "event_id": event_key, "version_id": version, "type": kind,
                "label": TYPE_LABELS[kind], "code": code,
                "company": str(record.get("company") or ""),
                "title": str(record.get("title") or ""), "status": "candidate",
                "event_at": event_at, "available_at": available, "ingested_at": ingestion,
                "times": {"event": _time_identity(event_at),
                          "public": {"value": available, "precision": precision,
                                     "basis": time_basis},
                          "ingested": _time_identity(ingestion)},
                "source": source, "source_hash": source_hash,
                "url": str(record.get("url") or ""),
                "material_id": str(record.get("material_id") or ""),
                "origin_id": origin, "correction_of": correction,
                "evidence": {**span, "source_hash": source_hash, "role": role},
                "modality": modality,
                "sentiment": {"entity": code, "label": direction,
                              "method": "rule_candidate", "confidence": None,
                              "calibration": "not_calibrated"},
                "historical_reconstruction": bool(cutoff is not None and
                                                   ingested_time is not None and
                                                   ingested_time > cutoff),
            }
            seen[key] = event_key
            batch["events"].append(event)
    if batch["events"]:
        batch["status"] = "partial" if batch["excluded"] else "ok"
    elif batch["excluded"] and all(e["reason"] == "needs_original" for e in batch["excluded"]):
        batch["status"] = "needs_original"
    return batch


def to_event_list(batch) -> list[dict]:
    """供 quant_research.event_returns.compute 的日级观察入口；非交易信号。

    日期用中国市场公开日，而非事件发生日/抓取日；既有读数采用披露后
    下一交易日。同一材料版本的多句/多类候选只出一条行情观察，避免虚增样本。
    保留代码原串（包括交易所后缀），调用者应让行情使用同一代码身份。
    """
    grouped = {}
    for event in (batch or {}).get("events", []):
        available = str(event.get("available_at") or "")
        moment = _moment(available)[0]
        if not event.get("code") or moment is None:
            continue
        day = moment.astimezone(_LOCAL).date().isoformat()
        key = (event["code"], day, event["source_hash"], event["version_id"])
        if key not in grouped:
            grouped[key] = {"code": event["code"], "date": day,
                "label": "", "material_id": event["material_id"],
                "url": event["url"], "raw_sha256": event["source_hash"],
                "event_id": "", "event_ids": [], "types": [],
                "version_id": event["version_id"], "public_time": available,
                "date_precision": event["times"]["public"]["precision"],
                "date_basis": "source_public_time:Asia/Shanghai",
                "kind": "event_reaction_observation", "candidate": True,
                "timing_rule": "next_trading_day_after_public_date"}
        row = grouped[key]
        row["event_ids"].append(event["event_id"])
        if event["type"] not in row["types"]:
            row["types"].append(event["type"])
    for row in grouped.values():
        row["label"] = "/".join(TYPE_LABELS[t] for t in row["types"])
        row["event_id"] = _hash(row["event_ids"])
    return list(grouped.values())
