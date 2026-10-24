# -*- coding: utf-8 -*-
"""自选变化简报 V1（WATCH-BRIEF-1024 §5）：两份本人冻结结果的**确定性投影**。

红线：
- 生成过程零模型调用、零外部网络请求（本模块只 import 标准库 + screen_store 读函数）；
- 不重算、不覆盖、不改写任何旧结果；比较只读 `screen_store.load_run`；
- 没有可比较变化就如实说「未观察到变化」，不编造新闻/因果；缺记录/损坏不算「无变化」；
- 变化原因按证据分类（data_updated / source_version / period_switch / rules_changed /
  scope_changed / impl_version / pending_review），不写买卖建议或黑箱评分。

身份：`brief_id = sha256(规范 JSON{schema, base/current 身份, 清单版本, 比较口径})`，
同输入 → 同 brief_id → 幂等。
"""

from __future__ import annotations

import hashlib
import json

MODULE_VERSION = "watch_brief/1"
BRIEF_SCHEMA = "watch_brief/1"

# 数值变化的证据分类（§5.2）；概览按类别可同时出现，不强行归一原因
CHANGE_KINDS = {
    "data_updated": "数据/来源版本更新",
    "source_version": "来源切换（不同数据集/文件）",
    "period_switch": "报告期切换",
    "rules_changed": "条件修改",
    "scope_changed": "清单/股票池范围变化",
    "impl_version": "计算版本变化",
    "pending_review": "待核对（修订时点/口径未知等）",
}


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _f(v):
    try:
        if v is None or v == "":
            return None
        f = float(v)
        return f if f == f else None  # NaN → None
    except (TypeError, ValueError):
        return None


def _status_of(row: dict | None) -> str:
    """行 → 状态：pass / fail / insufficient / missing（缺记录不是「无变化」）。"""
    if not row:
        return "missing"
    verdict = str(row.get("verdict") or "insufficient")
    return verdict if verdict in ("pass", "fail", "insufficient") else "insufficient"


def _readings_by_field(row: dict | None) -> dict:
    out = {}
    for item in (row or {}).get("readings") or []:
        field = str(item.get("field") or "")
        if field:
            out[field] = item
    return out


def _observations_index(run: dict, eid: str) -> dict:
    """(metric, period, period_kind) → observation dict（同一实体的可用观察）。"""
    for ent in (run.get("entities") or []):
        if str(ent.get("entity_id") or "").upper() == str(eid or "").upper():
            idx = {}
            for o in (ent.get("observations") or []):
                if str(o.get("state") or "") not in ("", "ok"):
                    continue
                key = (str(o.get("metric") or ""), str(o.get("period") or "")[:10],
                       str(o.get("period_kind") or ""))
                idx.setdefault(key, o)
            return idx
    return {}


def _rules_fingerprint(rules) -> str:
    return _canonical(list(rules or ()))


def _comparability(base_sum: dict, current_sum: dict) -> dict:
    """两份记录的比较口径：哪些身份不同就明确列出来（不静默相减）。"""
    flags = {}
    if str(base_sum.get("rules_hash") or "") != str(current_sum.get("rules_hash") or ""):
        flags["rules_changed"] = True
    bp, cp = base_sum.get("period_policy") or {}, current_sum.get("period_policy") or {}
    if (str(bp.get("mode") or ""), str(bp.get("report_period") or "")) != \
            (str(cp.get("mode") or ""), str(cp.get("report_period") or "")):
        flags["period_policy_changed"] = True
    if str(base_sum.get("impl_version") or "") != str(current_sum.get("impl_version") or ""):
        flags["impl_version_changed"] = True
    if str(base_sum.get("field_map_version") or "") != str(current_sum.get("field_map_version") or ""):
        flags["field_map_changed"] = True
    bs, cs = base_sum.get("source") or {}, current_sum.get("source") or {}
    if str(bs.get("dataset_id") or "") != str(cs.get("dataset_id") or ""):
        flags["source_dataset_changed"] = True
    elif str(bs.get("sha256") or "") != str(cs.get("sha256") or ""):
        flags["source_file_changed"] = True
    if list(base_sum.get("eval_periods") or {}) != list(current_sum.get("eval_periods") or {}):
        flags["eval_periods_differ"] = True
    return flags


def build_brief(base_run: dict, current_run: dict,
                *, watchlist_items=None, watchlist_rev: int | str = "") -> tuple[dict, list[str]]:
    """两份**已通过完整性校验**的冻结结果 → 简报投影。只读，不写盘、不重算。"""
    errors: list[str] = []
    base_sum = dict(base_run.get("summary") or base_run)
    current_sum = dict(current_run.get("summary") or current_run)
    flags = _comparability(base_sum, current_sum)
    not_comparable_reasons: list[str] = []
    if flags.get("period_policy_changed"):
        not_comparable_reasons.append("期间策略/报告期不同：读数不可直接横比")
    if flags.get("impl_version_changed") or flags.get("field_map_changed"):
        not_comparable_reasons.append("计算实现或字段口径变了：不能当作数据或经营变化")
    if flags.get("eval_periods_differ"):
        not_comparable_reasons.append("实际评估期不同：读数不可横比")

    base_rows: dict[str, dict] = {}
    for bucket in ("pass", "fail", "insufficient"):
        for row in ((base_run.get("rows") or {}).get(bucket) or []):
            base_rows[str(row.get("entity_id") or "").upper()] = row
    current_rows: dict[str, dict] = {}
    for bucket in ("pass", "fail", "insufficient"):
        for row in ((current_run.get("rows") or {}).get(bucket) or []):
            current_rows[str(row.get("entity_id") or "").upper()] = row

    watch = [str((w or {}).get("code") or "").upper() for w in (watchlist_items or [])]
    scope_ids = [c for c in dict.fromkeys(
        list(current_rows.keys()) + list(watch)) if c]  # 本次清单范围 = 记录覆盖 ∪ 清单
    base_only = sorted(set(base_rows) - set(current_rows))
    current_only = sorted(set(current_rows) - set(base_rows))

    cards: list[dict] = []
    for eid in scope_ids:
        brow, crow = base_rows.get(eid), current_rows.get(eid)
        b_status, c_status = _status_of(brow), _status_of(crow)
        card: dict = {"entity_id": eid,
                      "entity": ((crow or brow or {}).get("entity") or ""),
                      "base_status": b_status, "current_status": c_status,
                      "status_transition": [b_status, c_status],
                      "evidence": {"base": {"run_id": base_run.get("run_id"), "entity_id": eid},
                                   "current": {"run_id": current_run.get("run_id"),
                                               "entity_id": eid}},
                      "value_changes": [], "reading_changes": [],
                      "change_kinds": [], "warnings": []}
        if eid not in base_rows and eid not in current_rows:
            card["warnings"].append("资料不足：清单里的公司未出现在两份记录中（未覆盖，不算无变化）")
            card["change_kinds"].append("pending_review")
            cards.append(card)
            continue
        if eid not in current_rows:
            card["warnings"].append("本次记录未覆盖该公司：不算「失去资格」，先确认范围/数据集")
            card["change_kinds"].append("scope_changed")
            cards.append(card)
            continue
        if eid not in base_rows:
            card["warnings"].append("上次记录未覆盖该公司：没有可比基准读数")
            card["change_kinds"].append("scope_changed")
        # 1) 条件读数与状态转换
        b_read, c_read = _readings_by_field(brow), _readings_by_field(crow)
        for field in sorted(set(b_read) | set(c_read)):
            bi, ci = b_read.get(field), c_read.get(field)
            if (bi and ci) and \
                    _canonical({k: bi.get(k) for k in ("status", "value", "unit", "reason")}) == \
                    _canonical({k: ci.get(k) for k in ("status", "value", "unit", "reason")}):
                continue  # 该字段读数没变，不造内容
            card["reading_changes"].append({
                "field": field,
                "label": (ci or bi or {}).get("label") or field,
                "base": {"status": (bi or {}).get("status") or "missing",
                         "value": (bi or {}).get("value"), "unit": (bi or {}).get("unit"),
                         "display": (bi or {}).get("display"), "reason": (bi or {}).get("reason") or ""},
                "current": {"status": (ci or {}).get("status") or "missing",
                            "value": (ci or {}).get("value"), "unit": (ci or {}).get("unit"),
                            "display": (ci or {}).get("display"),
                            "reason": (ci or {}).get("reason") or ""},
            })
            card["change_kinds"].append(
                "rules_changed" if flags.get("rules_changed") else
                ("pending_review" if not flags else "data_updated"))
        # 2) 财务观察数值变化（同 (metric, period, period_kind) 且单位/币种/口径一致才给差值）
        b_obs, c_obs = _observations_index(base_run, eid), _observations_index(current_run, eid)
        for key in sorted(set(b_obs) | set(c_obs)):
            bo, co = b_obs.get(key), c_obs.get(key)
            metric, period, kind = key
            if not co and bo:
                card["value_changes"].append({"metric": metric, "metric_label":
                                              bo.get("metric_label") or metric, "period": period,
                                              "change": "removed_from_current",
                                              "note": "本次记录没有这条观察（不推断经营含义）"})
                card["change_kinds"].append("scope_changed")
                continue
            if not bo and co:
                card["value_changes"].append({"metric": metric, "metric_label":
                                              co.get("metric_label") or metric, "period": period,
                                              "change": "new_in_current",
                                              "note": "上次记录没有这条观察（可能范围/数据不同）"})
                card["change_kinds"].append("scope_changed")
                continue
            same_ctx = all(str(bo.get(k) or "") == str(co.get(k) or "")
                           for k in ("unit", "currency", "caliber"))
            bv, cv = _f(bo.get("value")), _f(co.get("value"))
            if bv is None or cv is None:
                continue
            if bv == cv:
                continue
            if not same_ctx:
                card["value_changes"].append({
                    "metric": metric, "metric_label": co.get("metric_label") or metric,
                    "period": period, "change": "not_comparable",
                    "base": {"value": bv, "unit": bo.get("unit"), "currency": bo.get("currency"),
                             "caliber": bo.get("caliber")},
                    "current": {"value": cv, "unit": co.get("unit"), "currency": co.get("currency"),
                                "caliber": co.get("caliber")},
                    "note": "单位/币种/口径不同：不直接相减；需先确认口径再比较"})
                card["change_kinds"].append("pending_review")
                continue
            delta = cv - bv
            item = {"metric": metric, "metric_label": co.get("metric_label") or metric,
                    "period": period, "change": "changed",
                    "base": {"value": bv, "unit": bo.get("unit")},
                    "current": {"value": cv, "unit": co.get("unit")},
                    "delta": delta,
                    "delta_pct_points": delta if str(co.get("unit") or "") == "%" else None,
                    "source_urls": sorted({str(bo.get("source_url") or ""),
                                           str(co.get("source_url") or "")} - {""})}
            card["value_changes"].append(item)
            if "data_updated" not in card["change_kinds"]:
                card["change_kinds"].append("data_updated")
        if card["change_kinds"]:
            cards.append(card)
        else:
            card["no_observed_change"] = True
            cards.append(card)

    changed_cards = [c for c in cards if c.get("change_kinds")]
    overview = {
        "conditions_changed": sum(1 for c in cards if c.get("reading_changes")),
        "data_changed": sum(1 for c in cards
                            if "data_updated" in (c.get("change_kinds") or [])),
        "scope_changed": sum(1 for c in cards
                             if "scope_changed" in (c.get("change_kinds") or [])),
        "insufficient": sum(1 for c in cards
                            if c.get("current_status") == "insufficient"
                            or c.get("base_status") == "insufficient"),
        "missing": sum(1 for c in cards
                       if c.get("base_status") == "missing"
                       or c.get("current_status") == "missing"),
        "rules_changed": bool(flags.get("rules_changed")),
        "impl_version_changed": bool(flags.get("impl_version_changed")),
        "source_changed": bool(flags.get("source_dataset_changed")
                               or flags.get("source_file_changed")),
        "period_policy_changed": bool(flags.get("period_policy_changed")),
        "unchanged": sum(1 for c in cards if c.get("no_observed_change")),
        "base_only_companies": base_only, "current_only_companies": current_only,
    }
    identity = {
        "schema": BRIEF_SCHEMA, "module_version": MODULE_VERSION,
        "base": {"run_id": base_run.get("run_id"), "result_hash": base_run.get("result_hash"),
                 "created_at": base_run.get("created_at"), "as_of": base_run.get("as_of"),
                 "report_period": base_run.get("report_period"),
                 "impl_version": base_run.get("impl_version"),
                 "field_map_version": base_run.get("field_map_version"),
                 "source": base_sum.get("source") or {}},
        "current": {"run_id": current_run.get("run_id"),
                    "result_hash": current_run.get("result_hash"),
                    "created_at": current_run.get("created_at"), "as_of": current_run.get("as_of"),
                    "report_period": current_run.get("report_period"),
                    "impl_version": current_run.get("impl_version"),
                    "field_map_version": current_run.get("field_map_version"),
                    "source": current_sum.get("source") or {}},
        "watchlist_rev": watchlist_rev,
        "comparability_flags": flags,
        "scope_entity_ids": scope_ids,
    }
    brief = {
        "schema": BRIEF_SCHEMA, "title": "本次记录相对上次记录",
        "identity": identity,
        "brief_id": hashlib.sha256(_canonical(identity).encode("utf-8")).hexdigest(),
        "comparability": {"flags": flags, "not_comparable_reasons": not_comparable_reasons},
        "overview": overview,
        "cards": cards,
        "change_kinds_legend": CHANGE_KINDS,
        "notes": [
            "本简报是对两份冻结结果的只读投影：零模型调用、零外部网络请求；不重算、不改旧结果。",
            "没有修订标记不等于「财报更正」；没有因果证据不写经营结论；缺记录/不可读不算无变化。",
            "本简报不构成买卖建议、目标价或收益承诺。",
        ],
        "cost": {"model_calls": 0, "network_requests": "未知（本操作不发起外部请求）"},
    }
    return brief, errors
