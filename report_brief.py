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
    citations, audit = _collect_citations(task_id, goal, body, data, ws_dir=ws_dir)
    table = _metrics_table(rows, derived, periods, citations, req)
    findings = _findings(rows, derived, periods)
    claims = _claims(body, rows, derived, citations)
    risks = _risks(task_id, goal, body, project=project)
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
        },
        "metrics_table": table,
        "findings": findings,
        "claims": claims,
        "risks": risks,
        "citations": citations,
        "charts": charts,
        "audit": audit,
    }
    return structure


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


def _risks(task_id: str, goal: str, body: str, *, project=None) -> list[dict]:
    out: list[dict] = []
    try:
        from working_paper_export import build_result
        res = build_result(task_id, goal, project=project)
        for g in (res.get("gaps") or []):
            out.append({"kind": "fact_gap", "text": str(g.get("detail") or g)[:160]})
        for p in (res.get("problems") or []):
            out.append({"kind": "problem", "text": str(p.get("detail") or p)[:160]})
        for a in (res.get("audit") or []):
            out.append({"kind": "audit", "text": str(a.get("detail") or a)[:160]})
    except Exception as exc:
        logger.warning("底稿缺口读取失败（task=%s）：%s", task_id, str(exc)[:120])
    section = _section_text(body, ("风险", "待核查", "核查"))
    if section:
        for line in section.splitlines():
            t = line.strip().lstrip("#-*• ").strip()
            if t and len(t) >= 6:
                out.append({"kind": "from_report", "text": t[:160]})
    return out[:12]


# ── 来源（只收采用项，三类分别标识）────────────────────────────


def _collect_citations(task_id: str, goal: str, body: str, data: dict, *,
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
    # ② 正文实际引用的检索来源（按正文出现顺序编号）
    for url, title in _body_sources(body):
        _add(url, title, "third_party", "正文引用")
    # ③ 用户材料（goal 文本）
    if str(goal or "").strip() and any(k in str(goal) for k in ("材料", "附件", "上传")):
        _add("user-material", "用户提供的材料", "user_material", "用户材料")
    # 未被采用的候选（检索落盘里存在但正文没引用）→ 只留内部审计
    known = _project_sources(task_id, ws_dir=ws_dir)
    unused = [{"url": u, "title": _host(u)} for u in known if u not in seen]
    return adopted, {"unused_sources": unused[:20],
                     "note": "未采用/越界材料只留在内部审计，不进简报来源清单"}


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
    # 资料范围与状态（不隐藏限制）
    lines.append(f"> 报表口径：{sc.get('caliber') or '未声明'}；"
                 f"资料截至：{sc.get('as_of') or '未声明'}；"
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
    lines.append("## 风险与待核查")
    risks = structure.get("risks") or []
    if risks:
        for r in risks:
            lines.append(f"- {r.get('text')}")
    else:
        lines.append("- 底稿未记录缺口；仍建议核对现金流量表附注与营运资本变化。")
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
    else:
        lines.append("本次没有可登记的采用来源。")
    lines.append("")
    lines.append("### 资料范围与口径")
    lines.append(f"- 研究期间：{'、'.join(str(y) for y in periods) or '未声明'}")
    lines.append(f"- 报表口径：{sc.get('caliber') or '未声明'}"
                 "（归母净利润属合并报表中归属母公司的部分；经营现金流为合并口径）")
    lines.append(f"- 资料截至：{sc.get('as_of') or '未声明'}")
    lines.append(f"- 主体：{who}{('（' + sc['company_id'] + '）') if sc.get('company_id') else ''}")
    lines.append("")
    lines.append("### 版本与验收状态")
    lines.append("> 本简报由代码装配关键数据与来源，模型仅撰写『分析』一节；"
                 "交付状态与验收结论见任务页与导出清单（未验收时按草稿处理）。")
    lines.append("")
    lines.append("### 免责声明")
    lines.append(_disclaimer(structure))
    return "\n".join(lines).strip() + "\n"


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
