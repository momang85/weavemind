# -*- coding: utf-8 -*-
"""把底稿落到任务产物里（S1 收尾）。

为什么单独一步：`working_paper.py` 会算，但只有**落进任务工作区**才算交付的一部分——
用户要能拿到 `working_paper.csv/json`（可重算底稿 + 缺口表），而不是只在日志里看到结论。

出口（都写在任务的 project 目录，与 `financials.json` 同级）：
- `working_paper.json`：明细 + 派生 + 缺口 + 问题 + 请求契约 + 完备度；
- `working_paper.csv`：同一内容的三段式表格，便于人工核对与二次计算。

没有结构化财务时不产出底稿（返回 `skipped`）——**不编一份空底稿**冒充已复核。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from facts import UNKNOWN, ResearchRequest, parse_research_request
from workspace import task_project_dir
from working_paper import build_working_paper, paper_csv
import facts as _facts

logger = logging.getLogger(__name__)

PAPER_JSON = "working_paper.json"
PAPER_CSV = "working_paper.csv"


def _market_of(metadata: dict, resolution: dict | None) -> str:
    """市场：优先适配器/解析器给的语义值，其次按代码形状推断（cn/hk/us）。"""
    for src in (metadata or {}, resolution or {}):
        mk = str(src.get("market") or "").strip().lower()
        if mk in ("cn", "hk", "us"):
            return mk
        code = str(src.get("ticker") or src.get("code") or src.get("stock_code") or "")
        if code:
            up = code.upper()
            if up.endswith((".SZ", ".SH", ".SS")):
                return "cn"
            if up.endswith(".HK"):
                return "hk"
            if up.isalpha() and 1 <= len(up) <= 5:
                return "us"
    return ""


def resolve_request(task_id: str, goal: str, metadata: dict,
                    resolution: dict | None = None) -> tuple:
    """取本次研究的**请求契约**，以及抓取到的候选主体。

    顺序（A 批）：
    1. **提交时落库的契约**（`task_state.read_task(...)` 的 `research_request`）——用户请求的
       公司/市场/两年度/口径/截至日以它为准；
    2. 没有（自由文本入口、旧任务）→ 从目标文本解析，`identity_source="text"`；
    3. 抓取到的 `metadata`/`resolution` **只作为候选**返回，用于比对与缺口说明，
       **不参与构造契约**——否则取错公司会把请求一起改成错公司（架构复核 P1）。

    返回 `(request, candidates, source)`；`candidates` 形如
    `{"company": ..., "company_id": ..., "market": ...}`。
    """
    candidates = {
        "company": str((metadata or {}).get("company")
                       or (metadata or {}).get("name") or "").strip(),
        "company_id": str((metadata or {}).get("ticker") or (metadata or {}).get("code")
                          or (metadata or {}).get("stock_code") or "").strip(),
        "market": _market_of(metadata or {}, resolution),
    }
    persisted = {}
    try:
        import task_state as _ts
        row = _ts.read_task(task_id) or {}
        persisted = row.get("research_request") or {}
    except Exception as exc:                     # 读不到就按"没有契约"处理，不猜
        logger.warning("读取研究契约失败（task=%s）：%s", task_id, str(exc)[:120])
        persisted = {}
    req = ResearchRequest.from_payload(persisted)
    if req is not None and (req.company or req.company_id or req.periods or req.caliber != UNKNOWN):
        req.goal = req.goal or str(goal or "")
        return req, candidates, "stored"
    parsed = parse_research_request(goal, identity_source="text")
    return parsed, candidates, "text"


_VERSION_WORDS = ("更正", "修订", "重述", "更新后", "更正后")


def _is_corrected(title: str) -> bool:
    """标题里出现更正/修订/重述 → 这是**同一份披露的后续版本**（版本裁决要用它排序）。"""
    return any(w in str(title or "") for w in _VERSION_WORDS)


def _official_material_facts(task_id: str, request, *, project: str | None = None,
                             ws_dir=None) -> tuple[list, list[str]]:
    """**已准入的官方原文** → 财务表事实（L1，2026-09-30 复核 S2 / R2-a 版本裁决）。

    为什么需要它：`build_result` 此前没有 `financials.json` 就直接
    "skipped：没有结构化财务"——而结构化 API 不可用时，任务里其实**已经有**官方年报原文
    （K1 已经取件并准入），模型却一个数都拿不到。这里把同一份原文的财务表按**现役抽取器**
    变成事实，交给同一条底稿 → 数据集 → 受控分析链。

    纪律（R2-a，2026-09-30 下午复核）：只用**已准入**材料；主体必须在材料里得到验证；
    披露日晚于 `as_of` 的不取；**目录/索引顺序不得决定金融结论**——多份材料按
    `(披露日, 是否更正稿, material_id)` 确定性排序后**逐版本裁决**：

    - 同一 (指标, 期间, 口径) 由**披露更晚的版本**决定；更正/修订稿替代同口径原稿并留痕；
    - 两个版本数值不同 → 记"版本差异"（两个值与两份材料 id 都留下），不静默择一；
    - 单份材料缺某一期/某字段 → **继续用其它已准入材料补足**（此前抽到第一份就 `break`，
      于是"先读到更正稿还是先读到原稿"会改变净利读数）。
    """
    notes: list[str] = []
    company = str(getattr(request, "company", "") or "")
    code = str(getattr(request, "company_id", "") or "")
    periods = tuple(getattr(request, "periods", ()) or ())
    as_of = str(getattr(request, "as_of", "") or "")
    if not company and not code:
        return [], ["没有研究契约主体：不从官方原文取财务事实"]
    try:
        import material_intake as mi
    except Exception as exc:                        # noqa: BLE001 - 材料层不可用
        return [], [f"材料层不可读：{str(exc)[:100]}"]
    try:
        items = mi.read_index(task_id, project=project, ws_dir=ws_dir) or []
    except Exception as exc:                        # noqa: BLE001
        return [], [f"材料清单不可读：{str(exc)[:100]}"]

    # ① 逐份抽取（不 break）：把"哪份材料给出哪些事实"整表读出来，供版本裁决
    extracted: list[dict] = []
    for it in items:
        mid = str(it.get("material_id") or "")
        if not mid or str(it.get("status") or "") != "admitted":
            continue
        if str(it.get("kind") or "") not in ("pdf", "html", "text"):
            continue
        disc = str(it.get("disclosure_date") or "")
        if as_of and disc and disc > as_of:
            notes.append(f"官方材料 {mid}：披露日 {disc} 晚于 as_of {as_of}，不取事实")
            continue
        try:
            doc = mi.load_doc(task_id, mid, project=project, ws_dir=ws_dir) or {}
        except Exception as exc:                    # noqa: BLE001
            notes.append(f"官方材料 {mid} 正文不可读：{str(exc)[:80]}")
            continue
        title = str(it.get("title") or "")
        got = _facts.facts_from_annual_tables(
            doc, company=company, company_code=code, periods=periods, as_of=as_of,
            disclosed_at=disc, url=str(it.get("url") or ""))
        if not got:
            notes.append(f"官方材料 {mid}：财务表没抽出可用事实"
                         "（主体未验证/无三表/单位缺失）")
            continue
        # **同一主体/口径准入**（L1）：抽取器已核过"材料里能验证该主体"，
        # 这里再按研究契约的稳定标识与口径核一遍，不合格的观察一条不进底稿
        want_cal = str(getattr(request, "caliber", "") or "")
        kept, drop = [], {"subject": 0, "caliber": 0}
        for f in got:
            ok, _why = _facts.check_subject(request, f.entity, f.entity_id, f.market)
            if not ok:
                drop["subject"] += 1
                continue
            if want_cal and str(getattr(f, "caliber", "") or "") != want_cal:
                drop["caliber"] += 1
                continue
            kept.append(f)
        if not kept:
            notes.append(f"官方材料 {mid}：抽到 {len(got)} 条但都不符合契约"
                         f"（主体/口径，见排除计数 {drop}）")
            continue
        notes.append(f"官方材料 {mid}（{title[:40]}）：抽取到 {len(kept)} 条与契约相容的"
                     "财务事实"
                     + ("；已排除 " + "、".join(f"{k} {v} 条"
                                               for k, v in drop.items() if v)
                        if any(drop.values()) else ""))
        extracted.append({"material_id": mid, "title": title, "disclosure_date": disc,
                          "corrected": _is_corrected(title), "facts": kept})
    if not extracted:
        return [], notes

    # ② **版本裁决**：确定性排序（披露日 → 更正稿优先在后 → material_id），与索引顺序无关
    extracted.sort(key=lambda e: (str(e["disclosure_date"]), 1 if e["corrected"] else 0,
                                  str(e["material_id"])))
    chosen: dict[tuple, object] = {}
    source_of: dict[tuple, dict] = {}
    for entry in extracted:
        for f in entry["facts"]:
            key = (str(getattr(f, "metric", "")), str(getattr(f, "period", "")),
                   str(getattr(f, "caliber", "") or ""))
            prev = source_of.get(key)
            if prev is not None and prev["material_id"] != entry["material_id"]:
                a, b = getattr(chosen[key], "value", None), getattr(f, "value", None)
                try:
                    same = a is not None and b is not None and abs(float(a) - float(b)) <= max(
                        abs(float(b)) * 0.0005, 0.01)
                except (TypeError, ValueError):
                    same = False
                if not same:
                    notes.append(
                        f"版本差异（{key[0]} {key[1]}）：{prev['material_id']}"
                        f"（披露 {prev['disclosure_date'] or '未知'}）={a} vs "
                        f"{entry['material_id']}（披露 {entry['disclosure_date'] or '未知'}"
                        f"{'，更正/修订稿' if entry['corrected'] else ''}）={b}；"
                        "按**披露更晚的版本**取值（目录顺序不参与裁决）")
                elif entry["corrected"] and not prev["corrected"]:
                    notes.append(f"更正/修订稿 {entry['material_id']} 与原稿 "
                                 f"{prev['material_id']} 在 {key[0]} {key[1]} 上数值一致")
            chosen[key] = f
            source_of[key] = entry
    # ③ 供数说明：哪些材料**真的进了底稿**（多份材料补足时逐条列出）
    used = sorted({source_of[k]["material_id"] for k in chosen})
    if len(used) > 1:
        notes.append("多份已准入材料共同供数（缺期/缺字段互相补足）：" + "、".join(used))
    elif extracted and len(extracted) > 1:
        notes.append("已准入材料 " + str(len(extracted)) + " 份，"
                     "按版本裁决只采用 " + "、".join(used))
    return list(chosen.values()), notes


def _financials_payload_usable(payload) -> tuple[bool, str]:
    """`financials.json` 的载荷**按内容**判可用（R2-b，2026-09-30 下午复核）。

    复核反例：文件存在但内容为 `{"error":"upstream unavailable","data":[]}` 时，旧实现只看
    `fin_path.exists()` → 直接走结构化分支 → `rows=0`、`paper_ok=false`，**官方年报退路
    一次都没被调用**（helper 调用 0 次）。"文件存在"不等于"有可用事实"。
    """
    if payload is None:
        return False, "载荷不可解析"
    if not isinstance(payload, dict):
        return False, "载荷不是对象"
    if payload.get("error"):
        return False, f"载荷带 error：{str(payload.get('error'))[:60]}"
    rows = payload.get("financials")
    if not isinstance(rows, list) or not rows:
        extra = payload.get("data")
        return False, ("载荷没有 financials 行"
                       + (f"（data={type(extra).__name__}"
                          + (f"，{len(extra)} 项" if hasattr(extra, "__len__") else "")
                          + "）" if extra is not None else ""))
    try:
        got = _facts.facts_from_financials(payload)
    except Exception as exc:                        # noqa: BLE001
        return False, f"解析失败：{str(exc)[:60]}"
    if not got:
        return False, "载荷解析后没有可用事实"
    return True, ""


def build_result(task_id: str, goal: str, *, project: str | None = None) -> dict:
    """**只算不写**的底稿结果（供读取侧复用：交付状态/硬门槛在页面与导出里也要能重算）。

    与 `write_working_paper` 同源：那边 = 本函数 + 落盘两件套。分开是为了让
    "读一次任务状态"不必顺手改写工作区文件。

    L1（2026-09-30 复核 S2）：没有 `financials.json` 时不再直接"skipped"——先看任务里
    有没有**已准入的官方年报原文**，有就用它抽财务表事实、照样产出底稿（结构化 API 不可用
    但官方 PDF 足够时，确定性模型仍能运行）。
    """
    proj = task_project_dir(task_id, project) if project else task_project_dir(task_id)
    fin_path = Path(proj) / "financials.json"
    payload = None
    fin_why = "文件不存在"
    if fin_path.exists():
        try:
            payload = json.loads(fin_path.read_text(encoding="utf-8"))
        except Exception as exc:                    # noqa: BLE001
            payload = None
            fin_why = f"载荷不可解析：{str(exc)[:60]}"
        else:
            _ok, fin_why = _financials_payload_usable(payload)
            if _ok:
                fin_why = ""
    if fin_why:
        # **空/错误载荷不得阻断官方年报退路**（R2-b）：按内容判不可用 → 走官方原文那条路，
        # 并把"为什么没用结构化载荷"如实写进 notes（读者能分清"没有"与"坏了"）。
        request, _c, _s = resolve_request(task_id, goal, {}, {})
        off_facts, off_notes = _official_material_facts(task_id, request, project=project)
        off_notes = [f"结构化财务载荷未采用：{fin_why}"] + list(off_notes)
        if not off_facts:
            return {"ok": False, "skipped": True,
                    "reason": ("没有可用的结构化财务（" + fin_why + "），"
                               "也没有可用的已准入官方原文财务表：不产出底稿"),
                    "official_notes": off_notes}
        paper = build_working_paper(list(off_facts), request)
        return {"ok": True, "request": request.as_dict(), "request_source": "official_material",
                "candidates": [], "selection": paper.selection,
                "facts": [{"metric": str(getattr(f, "metric", "")),
                           "metric_label": str(getattr(f, "metric_label", "")),
                           "period": str(getattr(f, "period", "")),
                           "value": getattr(f, "value", None),
                           "unit": str(getattr(f, "unit", "")),
                           "currency": str(getattr(f, "currency", "")),
                           "caliber": str(getattr(f, "caliber", "")),
                           "fact_id": str(getattr(f, "fact_id", "")),
                           "period_kind": str(getattr(f, "period_kind", "")),
                           # R1-a（09-30 下午复核）：底稿也必须带上**列头原文与区间起点**——
                           # 只在抽取器补字段、底稿再丢掉，等于期间语义没贯通
                           # （复核原文：不只在抽取器补字段而冻结时再丢掉）。
                           "period_label": str(getattr(f, "period_label", "")),
                           "period_start": str(getattr(f, "period_start", "")),
                           "period_end": str(getattr(f, "period_end", "")),
                           "verify_state": str(getattr(f, "verify_state", "")),
                           "source_locator": dict(getattr(f, "source_locator", {}) or {}),
                           "formula": str(getattr(f, "formula", "")),
                           "derived_from": list(getattr(f, "derived_from", []) or [])}
                          for f in off_facts],
                "operating_facts": [], "scope_notes": [],
                "rows": len(paper.rows), "derived": len(paper.derived),
                "gaps": paper.gaps, "problems": [p.as_dict() for p in paper.problems],
                "audit": paper.audit,
                "completeness": paper.completeness, "paper_ok": paper.ok,
                "official_notes": off_notes,
                "paper": paper,                 # 仅内存用；落盘时由 write_working_paper 使用
                "note": "底稿来自**已准入官方年报原文**的财务表（结构化 API 不可用时的正常入口）"}
    payload = json.loads(fin_path.read_text(encoding="utf-8"))
    md = dict(payload.get("metadata") or {})
    if str(payload.get("source") or "") == "multi_entity":
        md = dict((payload.get("companies") or [{}])[0].get("metadata") or {})
    resolution = payload.get("resolution") or {}

    request, candidates, request_source = resolve_request(task_id, goal, md, resolution)
    facts = _facts.facts_from_financials(payload)
    # R3：把已准入年报片段里的**量价/结构**事实并入底稿（含产品/渠道/地区维度、
    # 原表行列定位、组内合计闭合校验）。缺材料时为空，不填零、不编。
    op_facts: list = []
    scope_notes: list = []
    try:
        import narrative_evidence as _ne
        material = _ne.read(task_id) or {}
        _periods = [int(y) for y in (getattr(request, "periods", None) or [])]
        _last = max(_periods) if _periods else None
        _total = None
        if _last is not None:
            for _f in facts:
                if (str(getattr(_f, "metric", "")) == "revenue"
                        and str(getattr(_f, "period", "")) == f"{_last}年"):
                    try:
                        _total = float(_f.value) * 1e8        # 亿元 → 元（与年报表同单位）
                    except (TypeError, ValueError):
                        _total = None
                    break
        op_facts, scope_notes = _facts.facts_from_operating(
            material, request, total_revenue_yuan=_total)
    except Exception as exc:                     # noqa: BLE001 - 经营事实是增强，不拖垮主线
        logger.warning("经营维度事实并入失败（task=%s）：%s", task_id, str(exc)[:140])
    paper = build_working_paper(list(facts) + list(op_facts), request)
    return {
        "ok": True,
        "request": request.as_dict(),
        "request_source": request_source,
        "candidates": candidates,
        "selection": paper.selection,
        # 交付硬门槛要用它做"文档主体作用域"判定（只需要指标/期间/值）；
        # 口径与依据一并带出：报告步骤的"已选事实"块要能把口径证据讲给读者
        "rows_detail": [{"metric": r.get("metric"),
                         "metric_label": r.get("metric_label"),
                         "period": r.get("period"),
                         "value": r.get("value"),
                         "unit": r.get("unit"),
                         "caliber": r.get("caliber"),
                         "caliber_source": r.get("caliber_source"),
                         "caliber_evidence": r.get("caliber_evidence"),
                         # D1：稳定 fact_id 必须带出来——报告侧主张绑定要按
                         # "指标/期间/口径/单位"逐条对上底稿事实；没有 id 就只能按数值猜
                         # （历史反例："2023 净利润 100 万元"被"2024 收入 100 亿元"支持）
                         "fact_id": r.get("fact_id"),
                         # 字段位置：简报附录要回答"这个数字取自哪个字段"
                         "source_locator": r.get("source_locator")}
                        for r in paper.rows],
        # 可重算的派生行（同比等，单位 %）：注入给模型解释，也便于读侧核对
        # `formula` 一并带出：报告里要能复核"这个百分比怎么算出来的"（溯源按公式认）
        "derived_detail": [{"metric": r.get("metric"),
                            "metric_label": r.get("metric_label"),
                            "period": r.get("period"),
                            "value": r.get("value"),
                            "unit": r.get("unit"),
                            "caliber": r.get("caliber"),
                            "caliber_evidence": r.get("caliber_evidence"),
                            "formula": r.get("formula"),
                            "fact_id": r.get("fact_id"),
                            "source_locator": r.get("source_locator"),
                            "derived_from": list(r.get("derived_from") or [])}
                           for r in paper.derived],
        # R3：经营维度事实（产品/渠道/地区 + 实物量 + 推算）+ 组内闭合说明
        "operating_detail": [{"metric": r.get("metric"),
                              "metric_label": r.get("metric_label"),
                              "period": r.get("period"),
                              "value": r.get("value"),
                              "unit": r.get("unit"),
                              "caliber": r.get("caliber"),
                              "caliber_evidence": r.get("caliber_evidence"),
                              "dimensions": ((r.get("source_locator") or {})
                                             .get("dimensions") or {}),
                              "table": ((r.get("source_locator") or {})
                                        .get("table") or {}),
                              "locator": ((r.get("source_locator") or {})
                                          .get("locator") or ""),
                              "fact_id": r.get("fact_id"),
                              "verify_state": r.get("verify_state"),
                              "formula": r.get("formula") or ""}
                             for r in paper.rows
                             if str(r.get("metric") or "") in _facts.OPERATING_METRICS],
        "scope_notes": scope_notes,
        "rows": len(paper.rows), "derived": len(paper.derived),
        "gaps": paper.gaps, "problems": [p.as_dict() for p in paper.problems],
        "audit": paper.audit,
        "completeness": paper.completeness, "paper_ok": paper.ok,
        "paper": paper,                      # 仅内存用；落盘时由 write_working_paper 使用
    }


def chart_rows(task_id: str, goal: str, *, project: str | None = None) -> dict:
    """底稿 → **图表数据行**（只算不写）。

    为什么不让图表继续吃 `financials.json`（`_merge_structured_financials` 那条）：
    那份只有原始指标、**没有同比与比率**，也没有"契约期间"的概念——搜索清洗带进来的
    越界期间（如 2025 半年报）会一起进图，报告正文却声明"未予采用"（实机
    `ui-2084c2c9cc` 的 `market_trends.png` 就是这样把越界数据画进了交付）。

    这里以底稿为准：只保留**契约期间**的行，并带上派生指标（同比/比率），
    图上才有经济含义。

    返回 `{"ok", "rows", "derived", "periods", "unit", "source_label", "source_url",
    "request"}`；没有结构化财务时 `ok=False`（不编数据）。
    """
    res = build_result(task_id, goal, project=project)
    if not res.get("ok"):
        return {"ok": False,
                "reason": str(res.get("reason") or "没有结构化财务，无图表数据"),
                "rows": [], "derived": []}
    request = res.get("request") or {}
    periods = sorted({int(y) for y in (request.get("periods") or [])
                      if str(y).strip().isdigit()})

    def _year(text: str) -> int | None:
        m = re.search(r"(20\d{2})", str(text or ""))
        return int(m.group(1)) if m else None

    def _keep(period: str) -> bool:
        y = _year(period)
        return y is None or not periods or y in set(periods)

    rows = [dict(r, year=_year(r.get("period")))
            for r in (res.get("rows_detail") or [])
            if _keep(str(r.get("period") or ""))]
    derived = [dict(d, year=_year(d.get("period")))
               for d in (res.get("derived_detail") or [])
               if _keep(str(d.get("period") or ""))]
    # 表头单位只能取自**金额类**指标行，且不挑一个冒充全表（P1：标签不得说谎）。
    # 旧实现取"第一行有单位的"——底稿里还可能有实物量（吨）与单价（元/吨）行，
    # 谁先出现就把整个报告的单位写成"吨"：**数字没错、标签错**，读者据此换算全错。
    # 已复现的资料面：补材料带来的量价事实行排在结构化财务行之前（离线夹具；
    # 冻结样本 ui-a06a005c9b 恰是财务行在前，所以当时没踩到，但**行序一变就会**）。
    # 优先按**契约必需指标**判（金额口径以它们为准）；判不出来时留空，
    # 正文照实写"见表中标注"——不猜。
    _AMOUNT_UNITS = {"元", "万元", "亿元", "万美元", "亿美元", "港元", "万港元"}

    def _amount_unit_of(rs) -> str:
        seen = {str(r.get("unit") or "").strip() for r in rs}
        seen = {u for u in seen if u in _AMOUNT_UNITS}
        return next(iter(seen)) if len(seen) == 1 else ""

    _required = [str(m) for m in ((request or {}).get("required_metrics") or [])]
    unit = _amount_unit_of([r for r in rows
                            if str(r.get("metric") or "") in _required]) \
        or _amount_unit_of(rows)
    src_label, src_url = _source_labels(task_id, project)
    return {
        "ok": bool(rows or derived), "rows": rows, "derived": derived,
        "periods": periods, "unit": unit,
        "source_label": src_label, "source_url": src_url,
        "request": request,
    }


def _source_labels(task_id: str, project: str | None = None) -> tuple[str, str]:
    """`financials.json` 的来源 → (可读名, 原始 URL)。

    图上要写"东方财富数据中心"这类**可读来源名**；把接口 URL 原样印在图注里
    对读者是噪音（实机图注里出现过整串 API 地址）。
    """
    try:
        proj = task_project_dir(task_id, project) if project else task_project_dir(task_id)
        payload = json.loads((Path(proj) / "financials.json").read_text(encoding="utf-8"))
    except Exception:
        return "", ""
    md = payload.get("metadata") or {}
    raw = payload.get("raw") or {}
    if str(payload.get("source") or "") == "multi_entity":
        md = ((payload.get("companies") or [{}])[0].get("metadata") or {}) or md
        raw = ((payload.get("companies") or [{}])[0].get("raw") or {}) or raw
    key = str(md.get("source") or "")
    label = key
    try:
        import orchestrator_v2 as _ov
        label = _ov._STRUCTURED_SOURCE_LABELS.get(key, key or "结构化数据源")
    except Exception:
        pass
    return label or "结构化数据源", str(raw.get("url") or "")


def write_working_paper(task_id: str, goal: str, *,
                        project: str | None = None) -> dict:
    """读任务的结构化财务 → 生成事实与底稿 → 落盘两件套。

    返回 {ok, skipped?, rows, derived, gaps, problems, files}；任何异常都不抛出
    （底稿是交付增强，不能拖垮主线），失败时记日志并把原因放进返回值。
    """
    try:
        result = build_result(task_id, goal, project=project)
        if result.get("skipped") or not result.get("ok"):
            return result
        proj = task_project_dir(task_id, project) if project else task_project_dir(task_id)
        paper = result.pop("paper")
        out_json = Path(proj) / PAPER_JSON
        out_csv = Path(proj) / PAPER_CSV
        out_json.write_text(
            json.dumps(paper.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        out_csv.write_text(paper_csv(paper), encoding="utf-8")
        logger.info("底稿已落盘（task=%s）：%d 条事实、%d 条同比、%d 项缺口、%d 项问题",
                    task_id, len(paper.rows), len(paper.derived),
                    len(paper.gaps), len(paper.problems))
        result["files"] = {"json": str(out_json), "csv": str(out_csv)}
        return result
    except Exception as exc:                     # noqa: BLE001 - 交付增强不得拖垮主线
        logger.warning("底稿产出失败（task=%s）：%s", task_id, str(exc)[:160])
        return {"ok": False, "reason": str(exc)[:200]}


def gaps_note(result: dict) -> str:
    """把缺口/问题整理成交付物里可读的一段（缺证据要说出来，不能只留文件）。"""
    if not result or not result.get("ok"):
        return ""
    gaps = result.get("gaps") or []
    problems = result.get("problems") or []
    if not gaps and not problems:
        return ""
    lines = ["## 底稿缺口与待核验项", ""]
    for g in gaps:
        lines.append(f"- 缺口：{g.get('detail') or g}")
    for p in problems:
        lines.append(f"- 待核验：{p.get('detail') or p}")
    lines.append("")
    lines.append("> 以上项目未取得可核验证据，报告不得据此声称已达成；"
                 "底稿见 `working_paper.csv`（明细 / 派生 / 缺口三段）。")
    return "\n".join(lines)
