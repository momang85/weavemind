# -*- coding: utf-8 -*-
"""`event_calendar_v1`：把**已准入材料／官方发现候选**变成有日期的研究事件。

为什么需要它：`event_returns` 要的是"事件日 + 标的"，而事件日必须**可溯源**。手打 3 个日期能跑通
演示，但扩到 ~20 份披露就会开始出错（本轮就出过一次：口算的洋河事件日 `2025-04-28` 与证据文档的
`2025-04-29` 差一天，t0 整段错一个交易日）。所以事件日**只从材料自带的证据里取**：

- 优先材料/候选里的**来源字段**（`disclosure_date` / `declared_disclosed_at` / `disclosed_at` /
  `published_at` / `notice_date`）；
- 其次**已知来源格式的 URL**（巨潮路径里的 `YYYY-MM-DD`）；
- 最后才是**操作者声明**（`operator_disclosed_at`），且依据如实写 `operator_declared`，不冒充来源证据。

判"什么是披露日的证据"这件事**只有一份实现**：复用 `adapters.disclosure_ingest._declared_disclosure`
（它按专项 §3.2 把 `?asof=` 这类查询参数排除在外）。本模块**不重写**这套判据。

其余纪律：

- **日级精度才入事件表**：精度不足（月/年）时无法确定"之后第一个交易日"，逐条列为排除并写原因，不猜。
- **类别靠规则、不靠模型**：命中哪条关键词就写哪条，一条都不命中记 `未分类`，**不硬塞进某一类**
  （与"事件类按材料判定、不按公司凑数"同一条纪律）。
- 输出带 `material_id` / `url` / `raw_sha256`：读数要能回到具体材料。
"""
from __future__ import annotations

import json
from pathlib import Path

OPERATOR = "event_calendar_v1"
SCHEMA = "weavemind.event_calendar/0"
DAY_PRECISION = "day"
UNCLASSIFIED = "未分类"

# 规则是**数据**：命中顺序自上而下，第一条命中的胜出，`matched` 记录命中的关键词。
CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("业绩预告/快报", ("业绩预告", "业绩快报", "预亏", "预盈", "预增", "预减")),
    ("利润分配", ("利润分配", "权益分派", "分红", "派息", "转增")),
    ("定期报告", ("年度报告", "半年度报告", "第一季度报告", "第三季度报告", "年报", "半年报",
                  "季报")),
    ("融资", ("可转换公司债券", "可转债", "向特定对象发行", "非公开发行", "配股", "公司债券",
              "中期票据", "超短期融资券", "募集资金")),
    ("回购/增减持", ("回购", "减持", "增持", "权益变动", "要约收购")),
    ("经营合同", ("重大合同", "中标", "订单", "框架协议", "战略合作")),
    ("风险事项", ("诉讼", "仲裁", "行政处罚", "监管", "问询", "警示", "立案", "停牌", "退市",
                  "风险提示")),
    ("治理", ("董事会决议", "股东大会", "监事会", "章程", "高级管理人员", "辞职", "聘任",
              "独立董事")),
)

_DATE_KEYS = ("disclosure_date", "declared_disclosed_at", "disclosed_at", "published_at",
              "notice_date")


def classify(title: str) -> dict:
    """标题 → 类别（规则命中）；一条都不命中就是 `未分类`，不硬塞。"""
    t = str(title or "")
    for cat, words in CATEGORY_RULES:
        for w in words:
            if w in t:
                return {"category": cat, "matched": w, "rule": f"title:{w}"}
    return {"category": UNCLASSIFIED, "matched": "", "rule": "no_rule_matched"}


def _date_evidence(rec: dict) -> tuple[str, str, str]:
    """(日期, 精度, 依据)：先看材料自带字段，再退回**唯一那份**披露日判据实现。"""
    for k in _DATE_KEYS:
        val = str((rec or {}).get(k) or "").strip()[:10]
        if val:
            return (val, str(rec.get("date_precision") or "").strip(),
                    str(rec.get("date_basis") or "").strip() or f"source_field:{k}")
    try:
        from adapters.disclosure_ingest import _declared_disclosure   # noqa: PLC0415
        return _declared_disclosure(rec)
    except Exception:                                                  # noqa: BLE001
        return ("", "", "")


def _code_of(rec: dict) -> str:
    subj = rec.get("subject") if isinstance(rec.get("subject"), dict) else {}
    raw = (str((subj or {}).get("company_code") or "")
           or str(rec.get("company_code") or "") or str(rec.get("ts_code") or "")
           or str(rec.get("code") or ""))
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def normalize(rec: dict) -> dict:
    """一条材料/候选 → 日历条目（**不改写**原始字段，只做归类与提取）。"""
    date, prec, basis = _date_evidence(rec)
    cls = classify(rec.get("title"))
    return {"code": _code_of(rec), "date": date, "date_precision": prec, "date_basis": basis,
            "title": str(rec.get("title") or "").strip(),
            "category": cls["category"], "matched": cls["matched"], "rule": cls["rule"],
            "material_id": str(rec.get("material_id") or "").strip(),
            "url": str(rec.get("url") or "").strip(),
            "raw_sha256": str(rec.get("raw_sha256") or "").strip(),
            "period": str(rec.get("period") or "").strip()}


def from_records(records, *, codes=(), include_unclassified: bool = False) -> dict:
    """一批材料/候选 → 事件日历。**每一条排除都带原因**，不静默丢。"""
    want = {str(c).strip().zfill(6) for c in codes if str(c).strip()}
    events: list[dict] = []
    excluded: list[dict] = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        item = normalize(r)
        label = item["title"] or item["material_id"] or "(无标题)"
        why = ""
        if not item["code"]:
            why = "材料没有标的代码（无法绑定行情序列）"
        elif want and item["code"] not in want:
            why = f"标的 {item['code']} 不在本次研究范围内"
        elif not item["date"]:
            why = "材料没有可用的披露日证据（来源字段/URL 格式/操作者声明都没有）"
        elif item["date_precision"] != DAY_PRECISION:
            why = (f"披露日精度为「{item['date_precision'] or '未标注'}」，不是日级："
                   "无法确定『事件日之后第一个交易日』，不入事件表（不猜）")
        elif item["category"] == UNCLASSIFIED and not include_unclassified:
            why = "标题没有命中任何事件类别规则 ⇒ 记未分类，不硬塞进某一类"
        if why:
            excluded.append({**item, "reason": why})
            continue
        events.append(item)
    events.sort(key=lambda e: (e["date"], e["code"], e["title"]))
    return {"schema": SCHEMA, "operator": OPERATOR, "events": events, "excluded": excluded,
            "stats": {"total": len(records or []), "events": len(events),
                      "excluded": len(excluded),
                      "by_category": _count(events, "category"),
                      "by_code": _count(events, "code"),
                      "date_basis": _count(events, "date_basis")}}


def _count(items: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for it in items:
        out[str(it.get(key) or "")] = out.get(str(it.get(key) or ""), 0) + 1
    return dict(sorted(out.items()))


def to_event_list(calendar: dict, *, categories=()) -> list[dict]:
    """日历 → `event_returns.compute` 的 `events`（`label` 写明类别与标题，便于读数回溯）。"""
    cats = {str(c) for c in categories if str(c)}
    out = []
    for e in calendar.get("events") or []:
        if cats and e["category"] not in cats:
            continue
        out.append({"code": e["code"], "date": e["date"],
                    "label": f"{e['category']}｜{e['title']}",
                    "_material_id": e["material_id"], "_date_basis": e["date_basis"],
                    "_url": e["url"]})
    return out


def load_materials(materials_dir) -> list[dict]:
    """材料目录 → 记录列表（读 `index.json`；每条材料读自己的 `meta.json` 覆盖）。"""
    base = Path(materials_dir)
    recs: list[dict] = []
    if not base.is_dir():
        return recs
    idx = base / "index.json"
    if idx.is_file():
        try:
            payload = json.loads(idx.read_text(encoding="utf-8"))
            items = payload if isinstance(payload, list) else payload.get("materials") or []
            recs.extend([i for i in items if isinstance(i, dict)])
        except Exception:                                              # noqa: BLE001
            pass
    for d in sorted([p for p in base.iterdir() if p.is_dir()]):
        mp = d / "meta.json"
        if not mp.is_file():
            continue
        try:
            recs.append(json.loads(mp.read_text(encoding="utf-8")))
        except Exception:                                              # noqa: BLE001
            continue
    # 同一材料（material_id / URL / 标题+日期）只留一条：meta.json 比 index 更全
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in sorted(recs, key=lambda x: len(x), reverse=True):
        key = (str(r.get("material_id") or ""), str(r.get("url") or ""),
               str(r.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
