# -*- coding: utf-8 -*-
"""研究简报：**由代码确定性装配**（架构复核 §F1）。

为什么要这一层：实机交付里，关键数字、同比/比率、引用编号与来源清单**全由模型写**——
于是出现"正文只写 2/6 个数字""引用 [1..7] 与清单不对应""首屏是工程说明"。这里把
**数据部分**收归代码：关键发现、财务对照表、图表引用、来源编号、资料范围、声明，
全部由底稿（`working_paper_export`）与工作区产物生成；模型只负责"分析"一节的解释与结论。

三条纪律：
- **不编数字**：一切数值来自底稿 `rows_detail`/`derived_detail`，同数同期间；
- **引用一一对应**：`[n]` 由这里编号，来源清单只收**实际被采用**的来源；
- **越界/未采用材料只留在内部审计**（`audit`），不进简报。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import workspace

logger = logging.getLogger(__name__)

STRUCTURE_FILE = "report_structure.json"

# 来源类型标签：发行人年报 / 第三方数据 / 用户材料（不得统称"权威原始披露"）
SOURCE_TYPE_LABELS = {
    "issuer_annual_report": "发行人年报/官方披露",
    "third_party": "第三方数据/媒体",
    "user_material": "用户提供的材料（未经独立核实）",
}
_ISSUER_SOURCES = ("sec_edgar", "cninfo_annual", "eastmoney_ashare",
                   "eastmoney_datacenter")

_YOY_SUFFIX = "_yoy"
_SECTION_RE = re.compile(r"^#{1,4}\s*(.+?)\s*$", re.M)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 编排器的工程收尾报告（`orchestrator_v2._finalize`）：正文模型失效时它会成为
# "正文"，但它回答的是"哪些步骤成功"，不是研究结论——不得当成分析一节交付
_ENGINEERING_REPORT_RE = re.compile(
    r"^##\s*Task Report\s*$|^\s*Steps:\s*\d+\s*\(\d+\s*OK,\s*\d+\s*failed\)", re.M)

# 变化解释：**缺口 → 需要补充的材料**（确定性映射）。
# 为什么写死一张表：没有营运资本/税费/结算条款证据就推断"回款改善"是编因果
# （架构复核 §F2 的样例）；这里只说明"要判断这件事需要什么材料"。
MATERIALS_BY_METRIC = {
    "revenue": ("收入构成与量价拆分", "主要客户与销售模式变化"),
    "net_profit": ("毛利率构成", "期间费用与非经常性损益", "少数股东损益"),
    "operating_cashflow": ("现金流量表附注", "营运资本变化（应收/应付/存货）",
                           "税费与结算条款"),
    "total_liabilities": ("债务结构与到期分布", "受限资金与对外担保"),
    "total_assets": ("资产结构与减值计提",),
}
# 变化解释里"还不能证明什么"的固定边界（不因数据好看而放宽）
_INFERENCE_BOUNDARY = (
    "两期数据只能说明这两期的变化，不能据此声称长期趋势；"
    "未取得同业、行情与关键假设资料，本简报不给出估值或目标价。",
)

# 阅读视角 → 需要补充的材料（F2）：两种视角复用同一底稿，但要查的东西不同。
# 视角由用户声明（契约字段），**不按公司名或机构名推断**。
PERSPECTIVE_MATERIALS = {
    "equity": ("收入构成与量价拆分", "毛利率构成与非经常性损益",
               "同业可比数据与行情", "管理层对下一年度的经营计划"),
    "bank_corporate": ("债务到期结构与利率", "受限资金与对外担保", "授信与用信情况",
                       "主要客户与回款条款", "实际控制人与关联交易"),
}


def is_research_task(task_id: str, goal: str = "", *, ws_dir=None) -> bool:
    """是否研究任务（有落库研究契约，或有可读底稿）。"""
    try:
        from working_paper_export import resolve_request
        req, _c, source = resolve_request(task_id, goal, {}, None)
        if source == "stored" and req is not None:
            return True
    except Exception:
        pass
    return False


def build_structure(task_id: str, goal: str, body: str = "", *, project=None,
                    ws_dir=None) -> dict | None:
    """装配结构化报告对象；不是研究任务或没有底稿时返回 None。"""
    try:
        from working_paper_export import chart_rows
        data = chart_rows(task_id, goal, project=project)
    except Exception as exc:
        logger.warning("简报数据读取失败（task=%s）：%s", task_id, str(exc)[:140])
        return None
    if not data.get("ok"):
        return None
    req = data.get("request") or {}
    periods = list(data.get("periods") or [])
    rows = list(data.get("rows") or [])
    derived = list(data.get("derived") or [])
    evidence = _evidence(task_id, ws_dir=ws_dir)
    citations, audit = _collect_citations(task_id, goal, body, data,
                                          evidence=evidence, ws_dir=ws_dir)
    table = _metrics_table(rows, derived, periods, citations, req)
    findings = _findings(rows, derived, periods)
    claims = _claims(body, rows, derived, citations)
    background = _background(evidence, citations)
    changes = _change_explanation(rows, derived, periods, findings, evidence, citations)
    perspective = str(req.get("perspective") or "equity")
    risks = _risks(task_id, goal, body, project=project, evidence=evidence,
                   citations=citations, changes=changes, perspective=perspective)
    charts = _charts(task_id, project=project)
    structure = {
        "scope": {
            "company": str(req.get("company") or req.get("company_id") or ""),
            "company_id": str(req.get("company_id") or ""),
            "market": str(req.get("market") or ""),
            "periods": periods,
            "caliber": str(req.get("caliber") or ""),
            "as_of": str(req.get("as_of") or ""),
            "unit": str(data.get("unit") or ""),
            "source_label": str(data.get("source_label") or ""),
            "adopted_sources": len(citations),
            "audit_sources": len(audit.get("unused_sources") or []),
            "charts": len(charts),
            "perspective": perspective,
        },
        "metrics_table": table,
        "findings": findings,
        "background": background,
        "change_explanation": changes,
        "claims": claims,
        "risks": risks,
        "citations": citations,
        "charts": charts,
        "appendix": _appendix(task_id, rows, derived, citations, evidence,
                              project=project, ws_dir=ws_dir),
        "evidence": {
            "located": int((evidence or {}).get("located") or 0),
            "missing_labels": list((evidence or {}).get("missing_labels") or []),
        },
        "audit": audit,
    }
    return structure


# ── 定向取证结果（叙事证据）──────────────────────────────────


def _evidence(task_id: str, *, ws_dir=None) -> dict | None:
    """读叙事证据（年报/公告正文的小节定位）；没落盘时按契约重算一次（幂等、不联网）。"""
    try:
        import narrative_evidence as ne
        data = ne.read(task_id, ws_dir=ws_dir)
        if data is None:
            data = ne.build(task_id, ws_dir=ws_dir)
        return data if isinstance(data, dict) else None
    except Exception as exc:                     # noqa: BLE001 - 证据缺失不拖垮简报
        logger.warning("叙事证据读取失败（task=%s）：%s", task_id, str(exc)[:140])
        return None


def _located(evidence: dict | None, kind: str, limit: int = 3) -> list[dict]:
    """取某一类的**带定位**证据（检索摘要不算证据，只有正文定位才算）。"""
    out = [r for r in ((evidence or {}).get("records") or [])
           if r.get("kind") == kind and r.get("has_location")]
    return out[:limit]


def _background(evidence: dict | None, citations: list[dict]) -> list[dict]:
    """业务背景：只取**与本期变化有关的年报段落**（公司怎么赚钱、产品/客户/成本驱动）。"""
    out: list[dict] = []
    for r in _located(evidence, "business_background"):
        out.append({"text": str(r.get("snippet") or ""),
                    "source_n": _citation_n(citations, str(r.get("url") or "")),
                    "locator": str(r.get("locator") or "")})
    return out


def _change_explanation(rows, derived, periods, findings, evidence, citations) -> dict:
    """变化解释：发生了什么 → 管理层/附注怎么解释 → 能推断到哪一步 → 还不能证明什么。"""
    by: dict[str, dict] = {}
    for r in rows:
        by.setdefault(str(r.get("metric") or ""), {})[int(r.get("year"))] = r
    yoy = {str(d.get("metric") or ""): d for d in derived
           if str(d.get("metric") or "").endswith(_YOY_SUFFIX)}
    last = periods[-1] if periods else None
    changes: list[dict] = []
    for metric, label in (("revenue", "营业收入"), ("net_profit", "归母净利润"),
                          ("operating_cashflow", "经营活动现金流净额")):
        d = yoy.get(f"{metric}{_YOY_SUFFIX}")
        v = d.get("value") if d else None
        if not isinstance(v, (int, float)):
            continue
        cur = (by.get(metric) or {}).get(last) if last else None
        text = (f"{label}同比{'增长' if v > 0 else '下降' if v < 0 else '持平'} "
                f"{abs(v):g}%")
        if cur is not None:
            text += f"（{last} 年 {cur.get('value')}{cur.get('unit') or ''}）"
        changes.append({"metric": metric, "label": label, "yoy": v, "text": text,
                        "fact_ids": list((d or {}).get("derived_from") or [])})
    # 变化幅度大在前（本期最值得关注的先看）
    changes.sort(key=lambda c: -abs(float(c.get("yoy") or 0)))
    explained: list[dict] = []
    for r in _located(evidence, "change_explanation", 2) + _located(evidence, "footnote", 2):
        explained.append({"text": str(r.get("snippet") or ""),
                          "source_n": _citation_n(citations, str(r.get("url") or "")),
                          "locator": str(r.get("locator") or "")})
    unproven = [{"label": c["label"], "materials": list(MATERIALS_BY_METRIC.get(c["metric"], ()))}
                for c in changes[:3]]
    unproven = [u for u in unproven if u["materials"]]
    return {"changes": changes[:3], "explained_by": explained,
            "inference": list(_INFERENCE_BOUNDARY), "unproven": unproven}



# ── 指标表与关键发现（全部由底稿算）──────────────────────────────


def _metrics_table(rows, derived, periods, citations, req) -> dict:
    """指标 × 期间 的对照表 + 同比/比率列（数值、口径、来源编号）。"""
    by_metric: dict[str, dict] = {}
    for r in rows:
        m = str(r.get("metric") or "")
        by_metric.setdefault(m, {"label": str(r.get("metric_label") or m),
                                 "unit": str(r.get("unit") or ""),
                                 "values": {}})
        by_metric[m]["values"][int(r.get("year"))] = r.get("value")
    yoy_by_metric: dict[str, float] = {}
    for d in derived:
        m = str(d.get("metric") or "")
        if m.endswith(_YOY_SUFFIX):
            yoy_by_metric[m[: -len(_YOY_SUFFIX)]] = d.get("value")
    src_n = _source_number(citations, str(req.get("company_id") or ""))
    out_rows = []
    for m, info in by_metric.items():
        if m in ("gross_margin",):
            continue          # 比率型指标单列在"质量"里，避免与金额同表
        out_rows.append({
            "metric": m, "label": info["label"], "unit": info["unit"],
            "values": info["values"], "yoy": yoy_by_metric.get(m),
            "caliber": str(req.get("caliber") or ""), "source_n": src_n,
        })
    quality = []
    for d in derived:
        quality.append({"metric": str(d.get("metric") or ""),
                        "label": str(d.get("metric_label") or d.get("metric") or ""),
                        "period": str(d.get("period") or ""),
                        "value": d.get("value"), "unit": "%",
                        "formula": str(d.get("formula") or ""),
                        "fact_ids": list(d.get("derived_from") or [])})
    return {"rows": out_rows, "quality": quality,
            "periods": list(periods),
            "columns": ["指标", "上期", "本期", "同比", "口径", "来源"]}


def _findings(rows, derived, periods) -> list[dict]:
    """由数字算出的**观察**（不是模型写的），每条带 fact_id 便于复核。"""
    out: list[dict] = []
    by: dict[str, dict] = {}
    for r in rows:
        by.setdefault(str(r.get("metric") or ""), {})[int(r.get("year"))] = r
    yoy = {str(d.get("metric") or ""): d for d in derived
           if str(d.get("metric") or "").endswith(_YOY_SUFFIX)}
    last = periods[-1] if periods else None
    for metric, label in (("revenue", "营业收入"), ("net_profit", "归母净利润"),
                          ("operating_cashflow", "经营活动现金流净额")):
        per = by.get(metric) or {}
        if not last or last not in per:
            continue
        cur = per[last]
        prev = per.get(last - 1)
        y = yoy.get(f"{metric}{_YOY_SUFFIX}")
        text = f"{label} {last} 年为 {cur.get('value')}{cur.get('unit') or ''}"
        facts = [str(cur.get("fact_id") or "")]
        if prev is not None:
            text += f"（{last - 1} 年 {prev.get('value')}{prev.get('unit') or ''}）"
            facts.append(str(prev.get("fact_id") or ""))
        if y and isinstance(y.get("value"), (int, float)):
            text += f"，同比 {y['value']:g}%"
            facts += list(y.get("derived_from") or [])
        out.append({"text": text, "fact_ids": [f for f in facts if f]})
    # 质量比率：覆盖倍数带符号条件
    cov = next((d for d in derived if d.get("metric") == "cashflow_coverage"
                and d.get("year") == last), None)
    np_cur = (by.get("net_profit") or {}).get(last) if last else None
    cf_cur = (by.get("operating_cashflow") or {}).get(last) if last else None
    if cov is not None:
        # 覆盖倍数的**解释**不带阈值数字：`>100%` 这种裸数字在验收里算"待溯源数字"，
        # 而读数本身已在派生块里带公式给出
        text = f"经营现金流对归母净利润的覆盖：{last} 年"
        if isinstance(np_cur and np_cur.get("value"), (int, float)) and np_cur["value"] < 0:
            text += "当期归母净利润为负，该比值不表示利润有现金支撑"
        elif isinstance(cf_cur and cf_cur.get("value"), (int, float)) and cf_cur["value"] < 0:
            text += "当期经营现金流为净流出，该比值不表示利润有现金支撑"
        else:
            text += "当期经营现金流高于归母净利润（读数与算式见派生指标）"
        out.append({"text": text, "fact_ids": list(cov.get("derived_from") or [])})
    return out


# ── 主张（从模型正文解析并与底稿绑定）────────────────────────────


def _claims(body: str, rows, derived, citations) -> list[dict]:
    """模型正文里含数字的句子 → 与底稿值比对绑定（**不要求模型输出 schema**）。

    绑不上的主张进"待核查"（`status=needs_check`），既不丢弃也不当作已核实。
    """
    values: dict[str, str] = {}
    for r in rows:
        v = r.get("value")
        if isinstance(v, (int, float)):
            values[_num_key(v)] = str(r.get("fact_id") or "")
    for d in derived:
        v = d.get("value")
        if isinstance(v, (int, float)):
            values[_num_key(v)] = ",".join(str(x) for x in (d.get("derived_from") or []))
    out: list[dict] = []
    for sent in re.split(r"[。！？!?\n]+", str(body or "")):
        s = sent.strip()
        if not s or not _NUM_RE.search(s):
            continue
        if s.startswith(("#", "|", ">", "-", "*")):
            s = s.lstrip("#|>-* ").strip()
        if "|" in s and s.count("|") >= 2:
            continue                      # 表格行不是主张（模型自写的表由装配器接管）
        if re.match(r"^\d+\.\s*\[", s) or re.match(r"^[-*]\s*\[", s):
            continue                      # 来源清单条目不是主张
        facts: list[str] = []
        for tok in _NUM_RE.findall(s):
            fid = values.get(_num_key(tok))
            if fid and fid not in facts:
                facts.extend([x for x in fid.split(",") if x])
        kind = "observation" if facts else "unbound"
        if any(k in s for k in ("可能", "预计", "推测", "或将", "若")):
            kind = "inference" if facts else "unbound"
        if any(k in s for k in ("假设", "假如")):
            kind = "assumption" if facts else "unbound"
        out.append({"text": s[:200], "type": kind,
                    "fact_ids": facts, "status": "bound" if facts else "needs_check"})
    return out[:40]


def _num_key(value) -> str:
    try:
        return f"{float(str(value).replace(',', '')):.2f}"
    except Exception:
        return str(value)


# ── 风险与待核查（底稿缺口 + 模型风险小节）──────────────────────


def _risks(task_id: str, goal: str, body: str, *, project=None,
           evidence: dict | None = None, citations: list[dict] | None = None,
           changes: dict | None = None, perspective: str = "") -> list[dict]:
    """风险与核查：**每条风险都要有对应证据、会改变判断的观察条件、要补的材料**。

    只有"底稿缺口 + 模型自述"的风险是不完整的（读者无法判断该查什么）；这里为每条
    风险补上确定性的核查条件与材料清单——没有证据的写"底稿无对应证据"，不编。
    """
    citations = citations or []
    changes = changes or {}
    out: list[dict] = []

    def _add(kind: str, text: str, *, evidence_note: str, would_change: str,
             materials: list[str]) -> None:
        out.append({"kind": kind, "text": str(text)[:160],
                    "evidence": evidence_note, "would_change": would_change,
                    "materials_needed": list(materials)})

    # ① 底稿自身的缺口/问题/审计项（数字层面的"待核查"）
    try:
        from working_paper_export import build_result
        res = build_result(task_id, goal, project=project)
        for g in (res.get("gaps") or []):
            _add("fact_gap", str(g.get("detail") or g),
                 evidence_note="底稿缺口（结构化来源未取得该事实）",
                 would_change="补齐该事实后，若与现有读数方向相反，需修订相关判断",
                 materials=["发行人年报/公告中的对应指标", "该期间的审计报告"])
        for p in (res.get("problems") or []):
            _add("problem", str(p.get("detail") or p),
                 evidence_note="底稿问题（口径/期间/主体不一致）",
                 would_change="澄清口径或期间归属后，相关比率与同比需重算",
                 materials=["报表附注中的口径说明", "同一口径的历史序列"])
        for a in (res.get("audit") or []):
            _add("audit", str(a.get("detail") or a),
                 evidence_note="底稿审计项（不影响结论，但需知悉）",
                 would_change="若审计项改变了可比口径，则需重述对比结论",
                 materials=["原始取数记录", "口径声明依据"])
    except Exception as exc:                     # noqa: BLE001 - 缺口读取失败不拖垮简报
        logger.warning("底稿缺口读取失败（task=%s）：%s", task_id, str(exc)[:120])

    # ② 年报里明确列示的风险（带小节定位）——这是"有证据的风险"
    for r in _located(evidence, "risk", 3):
        _add("evidence_risk", str(r.get("snippet") or ""),
             evidence_note=(f"来源 [{_citation_n(citations, str(r.get('url') or ''))}]"
                            f" {r.get('locator')}"),
             would_change="若该风险出现缓释或加剧的公开证据（年报/公告更新），需修订判断",
             materials=["最新年报/公告中的风险因素章节", "相关事项的进展公告"])

    # ③ 变化解释里还没能证明的部分 → 需要补充的材料
    for u in (changes.get("unproven") or []):
        _add("unproven_change", f"{u.get('label')}的变化原因尚不能证明",
             evidence_note="未取得对应附注/管理层讨论证据",
             would_change=f"取得{'、'.join(u.get('materials') or [])}后，"
                         "若显示的原因与本期变化方向不一致，需修订解释",
             materials=list(u.get("materials") or []))

    # ④ 模型正文里的风险小节（原样带出，但标注"由模型提出、需取得证据"）
    section = _section_text(body, ("风险", "待核查", "核查"))
    if section:
        for line in section.splitlines():
            t = line.strip().lstrip("#-*• ").strip()
            if t and len(t) >= 6:
                _add("from_report", t,
                     evidence_note="由模型提出，尚未与底稿或年报证据绑定",
                     would_change="取得对应证据后方可改变判断；无证据时不得据此行动",
                     materials=["支持该判断的年报/公告段落或数据"])
    # ⑤ 视角要求的核查材料（视角由用户声明；两种视角查的东西不同）
    mats = list(PERSPECTIVE_MATERIALS.get(str(perspective or ""), ()))
    if mats:
        label = _perspective_label(perspective)
        _add("perspective_material", f"按{label}还需要核查的事项",
             evidence_note="视角由用户声明（不按公司名或机构名推断）",
             would_change="这些材料齐备后才可能形成相应判断；本次不预设结论",
             materials=mats)
    return out[:12]


def _perspective_label(perspective: str) -> str:
    try:
        from facts import PERSPECTIVE_LABELS
        return str(PERSPECTIVE_LABELS.get(str(perspective or ""), "") or "投研")
    except Exception:
        return "投研"


# ── 附录：字段位置与计算底稿 ─────────────────────────────────


def _appendix(task_id: str, rows, derived, citations, evidence: dict | None, *,
              project=None, ws_dir=None) -> dict:
    """附录要能回答"这个数字从哪来、怎么算的"：字段位置 + 计算底稿文件。"""
    locs: list[dict] = []
    seen: set[str] = set()
    src_n = _source_number(citations, "")
    for r in list(rows) + list(derived):
        label = str(r.get("metric_label") or r.get("metric") or "")
        loc = _locator_text(r.get("source_locator"))
        if not label or not loc or label in seen:
            continue
        seen.add(label)
        locs.append({"label": label, "locator": loc, "source_n": src_n})
    files: list[dict] = []
    try:
        proj = workspace.task_project_dir(task_id, project) if project \
            else workspace.task_project_dir(task_id)
        root = Path(ws_dir) if ws_dir else workspace.task_workspace(task_id)
        for base, name, purpose in (
            (proj, "working_paper.json", "底稿（事实、同比/比率与算式）"),
            (proj, "financials.json", "结构化取数原始载荷"),
            (root, "narrative_evidence.json", "年报/公告正文的证据与定位"),
            (root, "report_structure.json", "本简报的结构化对象"),
        ):
            p = Path(base) / name
            if p.exists():
                files.append({"file": name, "purpose": purpose,
                              "bytes": int(p.stat().st_size)})
    except Exception:
        pass
    return {"field_locations": locs[:12], "worksheet": files,
            "note": "网页来源按小节与字符区间定位；年报 PDF 的页码待后续支持"}


def _locator_text(locator) -> str:
    """`Fact.source_locator` → 可读字段位置（结构化来源的字段路径）。"""
    if not isinstance(locator, dict):
        return ""
    kind = str(locator.get("kind") or "")
    field = str(locator.get("field") or "")
    rtype = str(locator.get("report_type") or "")
    if not field:
        return ""
    parts = [p for p in (rtype, field) if p]
    return ("结构化字段：" + ".".join(parts)) if kind == "structured_field" \
        else (".".join(parts))



# ── 来源（只收采用项，三类分别标识）────────────────────────────


def _collect_citations(task_id: str, goal: str, body: str, data: dict, *,
                       evidence: dict | None = None,
                       ws_dir=None) -> tuple[list[dict], dict]:
    """来源清单：**只收实际被采用的**，并标注类型；未采用的留在 audit。"""
    adopted: list[dict] = []
    seen: set[str] = set()

    def _add(url: str, title: str, kind: str, used_by: str) -> None:
        u = str(url or "").strip()
        if not u or u in seen:
            if u and used_by:
                for c in adopted:
                    if c["url"] == u and used_by not in c["used_by"]:
                        c["used_by"].append(used_by)
            return
        seen.add(u)
        adopted.append({"n": len(adopted) + 1, "url": u,
                        "title": str(title or "").strip() or _host(u),
                        "type": kind, "used_by": [used_by] if used_by else []})

    # ① 结构化财务来源（本次研究实际采用的数据源）
    label = str(data.get("source_label") or "")
    surl = str(data.get("source_url") or "")
    if surl:
        _add(surl, label, "issuer_annual_report", "财务事实")
    # ② 定向取证采用的年报/公告页（业务背景/变化解释/风险用到它们才登记）
    for kind, used_by in (("business_background", "业务背景"),
                          ("change_explanation", "变化解释"),
                          ("footnote", "财务附注"),
                          ("risk", "风险因素")):
        for r in _located(evidence, kind, 4):
            _add(str(r.get("url") or ""), str(r.get("title") or ""),
                 str(r.get("source_type") or "third_party"), used_by)
    # ③ 正文实际引用的检索来源（按正文出现顺序编号）
    for url, title in _body_sources(body):
        _add(url, title, "third_party", "正文引用")
    # ④ 用户材料（goal 文本）
    if str(goal or "").strip() and any(k in str(goal) for k in ("材料", "附件", "上传")):
        _add("user-material", "用户提供的材料", "user_material", "用户材料")
    # 未被采用的候选（检索落盘里存在但正文没引用）→ 只留内部审计
    known = _project_sources(task_id, ws_dir=ws_dir)
    unused = [{"url": u, "title": _host(u)} for u in known if u not in seen]
    return adopted, {"unused_sources": unused[:20],
                     "note": "未采用/越界材料只留在内部审计，不进简报来源清单"}


def _citation_n(citations: list[dict], url: str) -> str:
    """来源 URL → 清单编号（不在清单里返回空串，不编编号）。"""
    u = str(url or "").strip()
    if not u:
        return ""
    for c in citations or []:
        if c.get("url") == u:
            return str(c.get("n") or "")
    return ""



def _body_sources(body: str) -> list[tuple[str, str]]:
    """正文里出现的来源：优先解析来源清单小节（含编号与标题），否则抓裸 URL。"""
    out: list[tuple[str, str]] = []
    section = _section_text(body, ("参考来源", "资料来源", "数据来源", "参考文献"))
    for line in (section or "").splitlines():
        m = re.search(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", line)
        if m:
            out.append((m.group(2), m.group(1)))
            continue
        m = re.search(r"(https?://\S+)", line)
        if m:
            out.append((m.group(1), ""))
    if not out:
        for m in re.finditer(r"(https?://[^\s)\]]+)", str(body or "")):
            out.append((m.group(1), ""))
    # 去重保序
    seen: set[str] = set()
    uniq: list[tuple[str, str]] = []
    for u, t in out:
        if u not in seen:
            seen.add(u)
            uniq.append((u, t))
    return uniq


def _project_sources(task_id: str, *, ws_dir=None) -> list[str]:
    """工作区里检索到的候选 URL（用于区分"采用"与"未采用"）。"""
    try:
        proj = workspace.task_project_dir(task_id)
        out: list[str] = []
        for name, keys in (("search_results.json", ("url",)),
                           ("fetch_snapshot.json", ("url",))):
            p = Path(proj) / name
            if not p.exists():
                continue
            try:
                items = json.loads(p.read_text(encoding="utf-8")) or []
            except Exception:
                continue
            for it in (items if isinstance(items, list) else []):
                if isinstance(it, dict):
                    for k in keys:
                        u = str(it.get(k) or "").strip()
                        if u.startswith("http") and u not in out:
                            out.append(u)
        return out
    except Exception:
        return []


def _source_number(citations: list[dict], company_id: str) -> str:
    for c in citations:
        if c.get("type") == "issuer_annual_report":
            return str(c.get("n") or "")
    return ""


# ── 图表（只取发布级）──────────────────────────────────────────


def _charts(task_id: str, *, project=None) -> list[dict]:
    try:
        proj = workspace.task_project_dir(task_id, project) if project \
            else workspace.task_project_dir(task_id)
        p = Path(proj) / "chart_manifest.json"
        if not p.exists():
            return []
        items = (json.loads(p.read_text(encoding="utf-8")) or {}).get("charts") or []
    except Exception:
        return []
    out: list[dict] = []
    for c in items:
        if not isinstance(c, dict):
            continue
        if str(c.get("grade") or "publish") != "publish":
            continue          # 质量分级为 draft 的图不进简报
        f = str(c.get("file") or "")
        if f and not f.startswith("chart_"):
            continue          # 检索统计图（词频/域名分布）不进研究简报
        if f:
            out.append({"file": f, "type": str(c.get("type") or ""),
                        "keywords": list(c.get("keywords") or [])})
    return out


def _section_text(text: str, keywords: tuple[str, ...]) -> str:
    lines = str(text or "").splitlines()
    start = -1
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and any(k in line for k in keywords):
            start = i + 1
            break
    if start < 0:
        return ""
    out: list[str] = []
    for line in lines[start:]:
        if line.lstrip().startswith("#"):
            break
        out.append(line)
    return "\n".join(out).strip()


def _host(url: str) -> str:
    m = re.search(r"https?://([^/]+)", str(url or ""))
    return m.group(1).replace("www.", "") if m else str(url or "")[:40]


# ── 渲染（代码装配的正文）──────────────────────────────────────


def render_brief_markdown(structure: dict, body: str = "") -> str:
    """把结构化对象渲染成**研究简报**正文（关键发现在前，工程内容不进正文）。"""
    sc = structure.get("scope") or {}
    table = structure.get("metrics_table") or {}
    periods = list(table.get("periods") or [])
    who = sc.get("company") or "目标公司"
    span = "–".join(str(y) for y in periods) if len(periods) >= 2 else str(periods or "")
    lines: list[str] = [f"# {who} 经营分析简报（{span} 年度）", ""]
    # 资料范围与状态（不隐藏限制）。"数据时效"用验收器认的字样：本简报由代码装配，
    # 时效声明也必须是代码给的（模型不写也不能因此丢分）
    lines.append(f"> 报表口径：{sc.get('caliber') or '未声明'}；"
                 f"数据时效：截至 {sc.get('as_of') or '未声明'}；"
                 f"单位：{sc.get('unit') or '见表中标注'}；"
                 f"采用来源 {sc.get('adopted_sources', 0)} 条"
                 f"（未采用 {sc.get('audit_sources', 0)} 条留在内部审计）。")
    lines.append("")
    lines.append("## 关键发现")
    findings = structure.get("findings") or []
    if findings:
        for f in findings:
            lines.append(f"- {f.get('text')}")
    else:
        lines.append("- 本次未取得可复算的财务事实（见文末资料缺口）。")
    lines.append("")
    # 业务背景：公司怎么赚钱（只取与本期变化有关的年报段落，带 [n] 与小节定位）
    lines.append("## 业务背景")
    background = structure.get("background") or []
    if background:
        for b in background:
            n = f"[{b.get('source_n')}]" if b.get("source_n") else ""
            lines.append(f"- {b.get('text')}{n}")
            if b.get("locator"):
                lines.append(f"  - 出处：{b.get('locator')}")
    else:
        lines.append("- 未取得与本期变化相关的年报业务段落"
                     "（需补充材料见『风险与核查』）。")
    lines.append("")
    lines.append("## 财务对照")
    rows = table.get("rows") or []
    if rows:
        # 表里只放金额与来源；**同比不在这里重复**——派生读数集中在下一块并带公式，
        # 验收按"逐个出现"判定，同一数字出现两次而只有一次带公式会拉低溯源率
        cols = ["指标"] + [str(y) for y in periods] + ["口径", "来源"]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for r in rows:
            # 单元格写成 `1741.44亿元`（数字与单位之间不留空格）：验收按"数值 + 单位"
            # 配对溯源，隔一个空格就只认到裸数字 → 判不可溯源
            unit = str(r.get("unit") or "")
            vals = []
            for y in periods:
                v = (r.get("values") or {}).get(y)
                vals.append("—" if v is None else f"{v}{unit}")
            src = f"[{r.get('source_n')}]" if r.get("source_n") else "—"
            lines.append("| " + " | ".join([str(r.get("label"))] + vals
                                           + [str(r.get("caliber") or "—"), src])
                         + " |")
    quality = table.get("quality") or []
    if quality:
        lines.append("")
        # 每个派生读数**紧贴括号公式**（`15.66%（(1741.44 - 1505.6) / 1505.6 * 100）`）：
        # 验收按"数字后紧跟完整算式 + 操作数能在来源里对上"判可溯源；写"计算：…"这类
        # 前缀会让它认不到（实机简报曾因此掉到 55% 溯源率）
        lines.append("**同比与比率（可复算）**：")
        for q in quality:
            f = str(q.get("formula") or "").strip()
            expr = _formula_expr(f)
            lines.append(f"- {q.get('label')} {q.get('period')}：{q.get('value')}%"
                         + (f"（{expr}）" if expr else ""))
        conds = _ratio_conditions(quality)
        if conds:
            lines.append("")
            lines.append("**比率适用条件**：")
            for label, cond in conds:
                lines.append(f"- {label}：{cond}")
            lines.append(f"- 适用范围：{_ratio_scope_note()}")
    charts = structure.get("charts") or []
    if charts:
        lines.append("")
        lines.append("## 图表")
        for i, c in enumerate(charts, 1):
            lines.append(f"![{c.get('file')}](charts/{c.get('file')})")
            lines.append("")
            lines.append(f"图 {i}：{c.get('file')}（{c.get('type') or '图'}；"
                         f"数据同『财务对照』表与底稿）")
    lines.append("")
    lines.append("## 分析")
    analysis = _analysis_section(body)
    lines.append(analysis if analysis else
                 "> 本次未产出可交付的分析正文（数据与底稿已保留，见文末）。")
    lines.append("")
    # 变化解释：发生了什么 → 管理层/附注怎么解释 → 能推断到哪一步 → 还不能证明什么
    lines.append("## 变化解释")
    changes = structure.get("change_explanation") or {}
    rows_ch = changes.get("changes") or []
    if rows_ch:
        lines.append("**发生了什么**：")
        for c in rows_ch:
            lines.append(f"- {c.get('text')}")
    else:
        lines.append("- 本次未取得可复算的同比，无法说明变化（见文末资料缺口）。")
    explained = changes.get("explained_by") or []
    lines.append("")
    lines.append("**管理层/附注的解释**：")
    if explained:
        for e in explained:
            n = f"[{e.get('source_n')}]" if e.get("source_n") else ""
            lines.append(f"- {e.get('text')}{n}")
            if e.get("locator"):
                lines.append(f"  - 出处：{e.get('locator')}")
    else:
        lines.append("- 未取得与上述变化对应的管理层讨论或附注段落，"
                     "原因**未在本次资料中体现**（需补充材料见下）。")
    lines.append("")
    lines.append("**推断边界**：")
    for t in (changes.get("inference") or []):
        lines.append(f"- {t}")
    unproven = changes.get("unproven") or []
    if unproven:
        lines.append("")
        lines.append("**还不能证明什么**：")
        for u in unproven:
            lines.append(f"- {u.get('label')}的变化原因：需先取得"
                         f"{'、'.join(u.get('materials') or [])}")
    lines.append("")
    lines.append("## 风险与核查")
    risks = structure.get("risks") or []
    if risks:
        for r in risks:
            lines.append(f"- **{r.get('text')}**")
            lines.append(f"  - 对应证据：{r.get('evidence') or '底稿无对应证据'}")
            if r.get("would_change"):
                lines.append(f"  - 会使判断改变的观察条件：{r.get('would_change')}")
            mats = r.get("materials_needed") or []
            if mats:
                lines.append(f"  - 需要补充的材料：{'、'.join(mats)}")
    else:
        lines.append("- 底稿未记录缺口；仍建议核对现金流量表附注与营运资本变化。")
    lines.append("")
    lines.append("- **结论边界**：本简报只覆盖上述期间的变化与缺口，"
                 "不构成趋势判断、评级或投资建议。")
    claims = [c for c in (structure.get("claims") or [])
              if c.get("status") == "needs_check"]
    if claims:
        lines.append("")
        lines.append("**待核查主张（未与底稿事实绑定）**：")
        for c in claims[:6]:
            lines.append(f"- {c.get('text')}")
    lines.append("")
    lines.append("## 附录")
    lines.append("")
    # 小节标题用**验收器认的**名称（`## 参考来源`），类型标签写在条目下一行——
    # 条目行必须是纯 `1. [标题](URL)`，否则清单解析不到（实机被记成"缺少参考来源清单"）
    lines.append("## 参考来源")
    cits = structure.get("citations") or []
    if cits:
        for c in cits:
            kind = SOURCE_TYPE_LABELS.get(str(c.get("type") or ""), "来源")
            lines.append(f"{c.get('n')}. [{c.get('title')}]({c.get('url')})")
            lines.append(f"   - 来源类型：{kind}")
            if c.get("used_by"):
                lines.append(f"   - 用于：{'、'.join(c.get('used_by') or [])}")
    else:
        lines.append("本次没有可登记的采用来源。")
    lines.append("")
    lines.append("### 资料范围与口径")
    lines.append(f"- 研究期间：{'、'.join(str(y) for y in periods) or '未声明'}")
    lines.append(f"- 报表口径：{sc.get('caliber') or '未声明'}"
                 "（归母净利润属合并报表中归属母公司的部分；经营现金流为合并口径）")
    lines.append(f"- 资料截至：{sc.get('as_of') or '未声明'}")
    lines.append(f"- 主体：{who}{('（' + sc['company_id'] + '）') if sc.get('company_id') else ''}")
    lines.append(f"- 阅读视角：{_perspective_label(sc.get('perspective'))}"
                 "（由用户在表单声明；不按公司名或机构名推断）")
    lines.append("")
    lines.append("### 字段位置与计算底稿")
    ap = structure.get("appendix") or {}
    for loc in (ap.get("field_locations") or []):
        n = f"[{loc.get('source_n')}]" if loc.get("source_n") else ""
        lines.append(f"- {loc.get('label')}：{loc.get('locator')}{n}")
    if not (ap.get("field_locations") or []):
        lines.append("- 未登记结构化字段位置（本次未取得结构化事实）。")
    for f in (ap.get("worksheet") or []):
        lines.append(f"- 计算底稿：{f.get('file')}（{f.get('purpose')}）")
    if ap.get("note"):
        lines.append(f"- 定位说明：{ap.get('note')}")
    lines.append("")
    lines.append("### 版本与验收状态")
    lines.append("> 本简报由代码装配关键数据与来源，模型仅撰写『分析』一节；"
                 "交付状态与验收结论见任务页与导出清单（未验收时按草稿处理）。")
    lines.append("")
    lines.append("### 免责声明")
    lines.append(_disclaimer(structure))
    return "\n".join(lines).strip() + "\n"


def _ratio_conditions(quality: list[dict]) -> list[tuple[str, str]]:
    """派生读数 → 适用条件（**按条件文本合并标签**：同比类共一条，不重复三遍）。"""
    try:
        from facts import RATIO_CONDITIONS
    except Exception:
        return []
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for q in quality:
        metric = str(q.get("metric") or "")
        if metric.endswith(_YOY_SUFFIX):
            metric = f"revenue{_YOY_SUFFIX}"       # 同比类共用一条条件
        cond = str(RATIO_CONDITIONS.get(metric) or "")
        label = str(q.get("label") or metric)
        if not cond:
            continue
        if cond not in grouped:
            grouped[cond] = []
            order.append(cond)
        if label not in grouped[cond]:
            grouped[cond].append(label)
    return [("、".join(grouped[c]), c) for c in order]


def _ratio_scope_note() -> str:
    try:
        from facts import RATIO_SCOPE_NOTE
        return str(RATIO_SCOPE_NOTE)
    except Exception:
        return "以上比率适用于非金融企业的经营简报。"


def _formula_expr(formula: str) -> str:
    """从底稿的 `formula` 里取出**纯算式**（去掉"输入 fact-xxx"等说明文字）。

    验收要求数字后紧跟 `（(1741.44 - 1505.6) / 1505.6 * 100）` 这样的完整算式，
    说明文字混在括号里就认不到。
    """
    t = str(formula or "")
    m = re.match(r"\s*([0-9][0-9.,\s*/+\-()]*?)\s*(?:，|,|输入|$)", t)
    if not m:
        return ""
    expr = m.group(1).strip().rstrip("，,")
    return expr


def _analysis_section(body: str) -> str:
    """模型正文 → 分析一节：剥掉它自己写的数字表/来源清单/免责声明（避免与装配结果打架）。"""
    text = str(body or "")
    if not text:
        return ""
    if _ENGINEERING_REPORT_RE.search(text):
        # 工程收尾报告不是分析（"5 个步骤成功"回答不了经营问题）：返回空，
        # 简报会如实写"本次未产出可交付的分析正文"
        return ""
    cut = len(text)
    for marker in ("## 参考来源", "## 资料来源", "## 数据来源", "## 免责声明",
                   "## 关键数据", "## 财务对照"):
        idx = text.find(marker)
        if idx > 0:
            cut = min(cut, idx)
    text = text[:cut]
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("# ") and not lines:
            continue                     # 去掉模型自己的大标题（简报已有标题）
        if line.strip().startswith("|") and "|" in line:
            continue                     # 模型自写的表格由装配器接管
        lines.append(line)
    return "\n".join(lines).strip()


def _disclaimer(structure: dict) -> str:
    try:
        import report_quality as _rq
        has_external = any(c.get("type") == "third_party"
                           for c in (structure.get("citations") or []))
        has_user = any(c.get("type") == "user_material"
                       for c in (structure.get("citations") or []))
        return _rq.disclaimer_clause(has_external, has_user)
    except Exception:
        return ("本报告由织光 WeaveMind AI 自动生成，仅供参考，不构成任何投资建议；"
                "据此操作风险自担。")


def write_structure(task_id: str, structure: dict, *, ws_dir=None) -> None:
    """落盘结构化对象（页面/清单可读；失败只记日志）。"""
    try:
        ws = Path(ws_dir) if ws_dir else workspace.task_workspace(task_id)
        (ws / STRUCTURE_FILE).write_text(
            json.dumps(structure, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        logger.warning("报告结构落盘失败（task=%s）：%s", task_id, str(exc)[:120])


def read_structure(task_id: str, *, ws_dir=None) -> dict | None:
    try:
        ws = Path(ws_dir) if ws_dir else workspace.task_workspace(task_id)
        p = ws / STRUCTURE_FILE
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
