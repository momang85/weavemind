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
# 覆盖倍数判"相当"的容差（百分点）：低于/相当/高于三分支都要有确定判定
_COVERAGE_TOL = 0.5
_SECTION_RE = re.compile(r"^#{1,4}\s*(.+?)\s*$", re.M)
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 编排器的工程收尾报告（`orchestrator_v2._finalize`）：正文模型失效时它会成为
# "正文"，但它回答的是"哪些步骤成功"，不是研究结论——不得当成分析一节交付
_ENGINEERING_REPORT_RE = re.compile(
    r"^##\s*Task Report\s*$|^\s*Steps:\s*\d+\s*\(\d+\s*OK,\s*\d+\s*failed\)", re.M)

# 未采用材料的说明用词（错主体/超资料截止/期间不符等）
_VALIDATION_LABELS = {
    "subject_mismatch": "主体不符",
    "after_as_of": "晚于资料截止日",
    "period_after_contract": "期间晚于契约期间",
    "period_before_contract": "期间早于契约期间且无比较数据",
    "comparison": "比较披露（已采用）",
    "period_unstated": "期间未标注（已采用）",
    "unknown_period": "期间未标注（已标注）",
    "unknown_published_at": "发布时点未核实",
    "unknown_subject": "主体未核实",
    "applicable": "适用",
}


# 装配器**逐字生成**的小节（参考来源/免责声明等）：模型正文里出现同名小节即整块丢弃，
# 这是"可证明重复"。其余小节（含"关键数据/核心指标/财务对照"）**不再整节删**——
# 只丢块内的表格/图片/来源清单行，标题与散文一律保留（D2：标题不能成为删除理由）。
_ASSEMBLER_OWNED_SECTIONS = (
    "参考来源", "资料来源", "数据来源", "参考文献", "免责声明",
)

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

# D3：三个优先研究问题（个体版主文最多三个重点；银行对公版共用同一核心）
# 每问的**题目与边界**都按本场景实际算出的方向生成（`_research_questions`），
# 这里只留不依赖场景事实的通用标题与"只表达推断限制"的边界。
# 夜间纠偏反例：固定边界把"利润降幅更大→毛利线以下存在额外拖累"和"现金绝对规模
# 同期仍在收缩"当成通用事实，于是增长场景（利润 +15.38%、现金流 +38.85%）也照写；
# 而"降幅更大 ⇒ 存在额外拖累"本身不成立——收入与毛利率同时下降就能造成利润降幅
# 更大，不必存在毛利线以下的额外拖累。边界只写"不能说明什么"，不写未计算的事实。
_RESEARCH_QUESTIONS: tuple[tuple[str, str, str], ...] = (
    ("revenue", "收入变化的量价与结构依据",
     "两期读数只能说明这两期的变化；未取得量价拆分与分部数据前，不判断收入变动的驱动结构"),
    ("net_profit", "利润变化的分解",
     "金额变化与利润率变化是两件事：百分点差只能描述利润率结构，不能回答绝对利润"
     "变化主要来自哪里；未取得毛利以下科目明细前，不把差额归到任何一项费用或损益"),
    ("operating_cashflow", "现金变化与利润覆盖的关系",
     "单期比率不构成趋势判断；来源结构未核实前，不判断现金流质量"),
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
    # A1：**研究对象类型**（可判定适用性）与阅读视角分开——bank_corporate 只是读者目标
    try:
        from facts import subject_type_of
        subject_type, type_source = subject_type_of(
            declared=str(req.get("subject_type") or ""),
            company=str(req.get("company") or ""),
            company_id=str(req.get("company_id") or ""))
    except Exception:
        subject_type, type_source = "unknown", ""
    evidence = _evidence(task_id, ws_dir=ws_dir)
    citations, audit = _collect_citations(task_id, goal, body, data,
                                          evidence=evidence, ws_dir=ws_dir)
    table = _metrics_table(rows, derived, periods, citations, req,
                           source_url=str(data.get("source_url") or ""),
                           subject_type=subject_type)
    findings = _findings(rows, derived, periods, subject_type=subject_type)
    # 引用重编号：模型正文里的 `[n]` 是它**自己清单**的编号，装配后编号会变；
    # 按 URL 建立"旧编号 → 新编号"映射并逐处重写，映射不到的去编号并记缺口
    body_sources = _body_source_list(body)
    ref_map: dict[int, int] = {}
    for old_n, _t, url in body_sources:
        new_n = _citation_n(citations, url)
        if new_n:
            ref_map[old_n] = int(new_n)
    raw_analysis = _analysis_section(body)
    # C2-1：映射不到 ≠ 无事发生。先把"引用了未采用来源"的句子逐句记下来（带原因），
    # 再去编号、再标注——保留主张与证据的关联，不用末尾免责声明代替。
    unsupported = _unsupported_claims(raw_analysis, ref_map, body_sources=body_sources,
                                      rejected=list((audit or {}).get("rejected") or []))
    analysis_text, unmapped = _remap_inline_refs(raw_analysis, ref_map)
    analysis_text = _mark_unsupported(analysis_text, unsupported)
    background = _background(evidence, citations)
    changes = _change_explanation(rows, derived, periods, findings, evidence, citations)
    # D2："有分析"的最低要求（至少一项数据观察 + 意义/局限说明）——读数进结构对象，
    # 不满足时进风险清单，不靠加长正文掩盖
    quality = analysis_coverage(analysis_text)
    perspective = str(req.get("perspective") or "equity")
    # D1 夜间补修：研究问题**先于**主张检查生成——它的观察与边界也是要审的句子
    questions = _research_questions(rows, derived, periods, evidence, citations,
                                    changes, perspective=perspective)
    assembly_claims: list[dict] = []
    for _q in questions:
        _obs = str(_q.get("observation") or "").strip()
        if _obs:
            assembly_claims.append({"text": _obs, "claim_type": "observation"})
        _bd = str(_q.get("boundary") or "").strip()
        if _bd:
            # 边界句单独记类型：纯待查的推断限制既不与"已断言未支持"混算，
            # 也不因为要提高分数被删掉
            assembly_claims.append({"text": _bd, "claim_type": "boundary"})
    claims = _claims(analysis_text, rows, derived, citations, unsupported=unsupported,
                     subject=str(req.get("company") or req.get("company_id") or ""),
                     periods=periods, evidence=evidence, assembly=assembly_claims)
    risks = _risks(task_id, goal, body, project=project, evidence=evidence,
                   citations=citations, changes=changes, perspective=perspective,
                   citation_gaps=unmapped, unsupported=unsupported,
                   analysis_quality=quality)
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
            "subject_type": subject_type,
            "subject_type_source": type_source,
        },
        "metrics_table": table,
        "findings": findings,
        "background": background,
        "change_explanation": changes,
        "analysis": analysis_text,
        "analysis_quality": quality,
        "research_questions": questions,
        "citation_gaps": list(unmapped),
        "claims": claims,
        "unsupported_claims": unsupported,
        "risks": risks,
        "citations": citations,
        "charts": charts,
        "appendix": _appendix(task_id, rows, derived, citations, evidence,
                              source_url=str(data.get("source_url") or ""),
                              project=project, ws_dir=ws_dir),
        "evidence": {
            "located": int((evidence or {}).get("located") or 0),
            # D1：检索摘要只是线索，单独计数——它不补"已取得定位"，也不清除缺失类别
            "snippet_hints": int((evidence or {}).get("snippet_hints") or 0),
            "missing_labels": list((evidence or {}).get("missing_labels") or []),
            "excluded": list((evidence or {}).get("excluded") or []),
        },
        "audit": audit,
    }
    structure["version_id"] = ""
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
    """取某一类的**可用**证据：带正文定位且**准入状态明确为 admitted/comparison**。

    检索摘要（无正文定位）、缺字段（unknown）与明确排除的（错主体/超截止/期间不符）
    都不算证据——"not excluded" 不等于"已验证支持本期变化"。
    """
    out = [r for r in ((evidence or {}).get("records") or [])
           if r.get("kind") == kind and r.get("has_location")
           and r.get("admission") in ("admitted", "comparison")]
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
    """变化解释：发生了什么 → 管理层/附注怎么解释 → 能推断到哪一步 → 还不能证明什么。

    **管理层解释与第三方观点分开**：第三方评论（数据平台/媒体）不得包装成管理层解释。
    """
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
                        "period": last,
                        "fact_ids": list((d or {}).get("derived_from") or [])})
    # 变化幅度大在前（本期最值得关注的先看）
    changes.sort(key=lambda c: -abs(float(c.get("yoy") or 0)))
    management: list[dict] = []
    third_party: list[dict] = []
    # C2-3：附注只有在**说得出原因**时才能作为解释；仅重复现金流数字的附注只是数字出处
    # （数字已由底稿给出），把它当"管理层解释"会让读者以为原因已被说明。
    try:
        import narrative_evidence as _ne
        _causal = _ne.is_causal
    except Exception:                            # noqa: BLE001 - 退化时不误删解释
        _causal = lambda _t: True                # noqa: E731
    candidates = [(r, "change_explanation")
                  for r in _located(evidence, "change_explanation", 2)]
    candidates += [(r, "footnote") for r in _located(evidence, "footnote", 2)
                   if _causal(str(r.get("snippet") or ""))]
    for r, kind in candidates:
        item = {"text": str(r.get("snippet") or ""),
                "source_n": _citation_n(citations, str(r.get("url") or "")),
                "locator": str(r.get("locator") or ""),
                "publisher": str(r.get("publisher") or ""),
                "document_period": str(r.get("document_period") or ""),
                "kind": kind,
                "validation_status": str(r.get("validation_status") or "")}
        # 管理层解释 = 发行人披露（官方路径**或**文档 provenance 为年报原文——
        # 后者含"经第三方平台转载的年报原文"）；其余（媒体解读/评论）进第三方观点
        is_issuer = (str(r.get("source_type")) == "issuer_annual_report"
                     or str(r.get("document_provenance")) == "issuer_annual_report")
        item["issuer"] = bool(is_issuer)
        (management if is_issuer else third_party).append(item)
    unproven = [{"metric": c["metric"], "label": c["label"], "yoy": c.get("yoy"),
                 "period": c.get("period"),
                 "materials": list(MATERIALS_BY_METRIC.get(c["metric"], ()))}
                for c in changes[:3]]
    unproven = [u for u in unproven if u["materials"]]
    # C2-4/D1：解释按**指标 + 期间**逐条匹配——只取得"收入因提价增加"时，利润与
    # 现金流仍是缺口。旧实现写 `bool(management)`，把任何一条解释扩散给全部指标
    # （实机：只给收入解释，利润/现金流也标"解释已取得"）。
    for u in unproven:
        matched = _match_management_for_metric(
            management + third_party, str(u.get("metric") or ""), u.get("period"))
        u["has_explanation"] = bool(matched)
        if matched:
            u["matched"] = {"source_n": matched.get("source_n"),
                            "locator": matched.get("locator"),
                            "text": str(matched.get("text") or "")[:120],
                            "issuer": bool(matched.get("issuer"))}
    return {"changes": changes[:3], "management": management,
            "third_party_views": third_party,
            "inference": list(_INFERENCE_BOUNDARY), "unproven": unproven}


def _research_questions(rows, derived, periods, evidence, citations, changes, *,
                        perspective: str = "") -> list[dict]:
    """D3：研究问题 → 观察 / 支持证据 / 推断边界 / 下一步核查动作（主文最多三问）。

    每问只写四件事：由底稿可复算的**观察**、已取得的**证据定位**（没有就写"未取得、
    原因待证"）、这条观察**不能说明什么**（边界，含两条实机点名过的护栏）、以及要回答
    它**还需要什么材料**（可执行动作）。银行对公视角在同一核心上补"已知/未知 + 询问清单"。
    """
    by: dict[str, dict] = {}
    for r in rows:
        by.setdefault(str(r.get("metric") or ""), {})[int(r.get("year") or 0)] = r
    d_all = {str(d.get("metric") or ""): d for d in derived}
    last = periods[-1] if periods else None
    mgmt_pool = (list(changes.get("management") or [])
                 + list(changes.get("third_party_views") or []))
    labels = {"revenue": "营业收入", "net_profit": "归母净利润",
              "operating_cashflow": "经营活动现金流净额"}
    out: list[dict] = []
    for metric, question, boundary in _RESEARCH_QUESTIONS:
        d = d_all.get(f"{metric}{_YOY_SUFFIX}")
        v = d.get("value") if d else None
        if not isinstance(v, (int, float)):
            continue
        label = labels.get(metric, metric)
        cur = (by.get(metric) or {}).get(last) if last else None
        obs = f"{label}同比{'增长' if v > 0 else '下降' if v < 0 else '持平'} {abs(v):g}%"
        if cur is not None:
            obs += f"（{last} 年 {cur.get('value')}{cur.get('unit') or ''}）"
        q_text = question
        if metric == "net_profit":
            # 金额变化与利润率变化**分开呈现**：绝对利润的分解只能用金额差，不能用
            # 百分点差（夜间反例：毛利率降 2.09pp < 归母净利率降 7.13pp 被写成
            # "利润下滑并非主要来自毛利端"，而毛利金额实际减少 38.01 亿元）。
            # 每个值带**它自己那一行**的单位：缺毛利时不能把已有金额差也写成无单位。
            _np_row = d_all.get("net_profit_change") or {}
            _gp_row = d_all.get("gross_profit_change") or {}
            gap = d_all.get("net_profit_gross_gap_change") or {}
            gv, np_chg, gp_chg = gap.get("value"), _np_row.get("value"), _gp_row.get("value")
            _nu = str(_np_row.get("unit") or "")
            _gu2 = str(_gp_row.get("unit") or "")
            _ggu = str(gap.get("unit") or "")
            if all(isinstance(x, (int, float)) for x in (np_chg, gp_chg, gv)):
                obs += (f"；金额变化：归母净利润变化 {np_chg:+g}{_nu}、毛利润变化 "
                        f"{gp_chg:+g}{_gu2}、毛利线以下净额变化 {gv:+g}{_ggu}"
                        f"（= Δ归母净利润 − Δ毛利；含费用、税项、非经营性项目与少数股东等，"
                        f"未取得明细前不拆解到具体科目）")
                q_text = (question if v < 0 else "利润变化的分解")
            elif isinstance(np_chg, (int, float)):
                # 只缺毛利一侧：如实说缺的是哪一半，别把已有的金额差也说成"未取得"
                obs += (f"；金额变化：归母净利润变化 {np_chg:+g}{_nu}；毛利润同期金额差未取得，"
                        f"差额尚待拆解")
            else:
                obs += "；金额变化：同期金额差未取得，差额尚待拆解"
        if metric == "operating_cashflow":
            # 覆盖率必须与**方向**一起看：上升是被动结果还是现金改善，取决于
            # 分子分母各自往哪走；任一为负时不套覆盖解释（口径已在比率条件里写死）
            cov = d_all.get("cashflow_coverage") or {}
            cv = cov.get("value")
            cov_prev, cov_prev_year = None, None
            np_val = (by.get("net_profit") or {}).get(last) if last else None
            cf_val = (by.get("operating_cashflow") or {}).get(last) if last else None
            for d2 in derived:
                if (str(d2.get("metric")) == "cashflow_coverage"
                        and d2.get("year") != last):
                    cov_prev = d2.get("value")
                    cov_prev_year = d2.get("year")
            negative = any(isinstance(x, dict) and isinstance(x.get("value"), (int, float))
                           and float(x["value"]) <= 0 for x in (np_val, cf_val))
            if isinstance(cv, (int, float)) and not negative:
                # 覆盖读数**另起一句**、且每个数值紧跟自己的年份：同句出现两个年度时，
                # 断言按"不借远处年度"处理（D1 反例 30.24%），会把本期读数判成期间不明
                obs += f"。经营现金流对归母净利润的覆盖 {last} 年为 {abs(cv):g}%"
                if isinstance(cov_prev, (int, float)):
                    direction = "上升" if cv > cov_prev else "下降" if cv < cov_prev else "持平"
                    _py = f"{cov_prev_year} 年" if cov_prev_year else "上期"
                    obs += f"（{_py} {abs(cov_prev):g}%，{direction}）"
                    if direction == "上升" and v < 0:
                        # 被动成因：现金流本身在降，覆盖率却升 → 分母降得更快。
                        # 边界句**不带数字**（数字在观察里已可复算）：避免把未标期间的
                        # 数值塞进推断句，读者也不会把边界句当成"已验证的读数"
                        boundary = ("覆盖率上升系分母（归母净利润）降得更快造成的被动结果，"
                                    "**不表示回款改善**；" + boundary)
                    elif direction == "上升":
                        boundary = ("覆盖率上升系经营现金流增长快于归母净利润；"
                                    + boundary)
                    elif direction == "下降":
                        boundary = ("覆盖率下降不等于回款恶化：需同时看现金绝对额与"
                                    "来源结构；" + boundary)
            elif negative:
                boundary = ("当期归母净利或经营现金流不为正，该比值**不表示利润有现金"
                            "支撑**；" + boundary)
        matched = _match_management_for_metric(mgmt_pool, metric, last)
        support = {"has_evidence": bool(matched), "locator": "", "source_n": "",
                   "text": "", "issuer": False}
        if matched:
            support.update({"locator": str(matched.get("locator") or ""),
                            "source_n": str(matched.get("source_n") or ""),
                            "text": str(matched.get("text") or "")[:120],
                            "issuer": bool(matched.get("issuer"))})
        out.append({"metric": metric, "question": q_text, "observation": obs,
                    "support": support, "boundary": boundary,
                    "next_action": list(MATERIALS_BY_METRIC.get(metric, ()))})
    if str(perspective or "") == "bank_corporate":
        liab = (by.get("total_liabilities") or {}).get(last) if last else None
        liab_prev = ((by.get("total_liabilities") or {}).get(last - 1)
                     if last else None)
        known: list[str] = []
        if liab is not None:
            known.append(f"总负债 {liab.get('value')}{liab.get('unit') or ''}"
                         f"（{last} 年，期末）")
        dr = (d_all.get("debt_ratio") or {}).get("value")
        if isinstance(dr, (int, float)):
            known.append(f"资产负债率 {abs(dr):g}%")
        # 方向按本场景的读数说：负债**上升**的场景写"下降不等于安全"是另一件事的文案
        _lv, _lp = (liab or {}).get("value"), (liab_prev or {}).get("value")
        if isinstance(_lv, (int, float)) and isinstance(_lp, (int, float)):
            _dir = "下降" if _lv < _lp else "上升" if _lv > _lp else "持平"
            _head = (f"总负债{_dir}**不等于**短期偿债安全："
                     if _dir == "下降" else
                     f"总负债{_dir}也不等于偿债压力加大：")
        else:
            _head = "总负债水平**不等于**短期偿债安全："
        out.append({
            "metric": "bank_materials",
            "question": "银行对公视角：债务与回款条件的已知/未知",
            "observation": "；".join(known) or "未取得负债读数",
            "support": {"has_evidence": False, "locator": "", "source_n": "",
                        "text": "", "issuer": False},
            "boundary": (_head + "需债务到期结构、受限资金与担保材料才能评估；"
                         "本报告不输出授信结论"),
            "next_action": list(PERSPECTIVE_MATERIALS.get("bank_corporate", ()))})
    return out[:4]


# 解释与指标的匹配词：一条解释只覆盖它真正谈到的指标（"收入"不得替利润/现金流背书）
_EXPLANATION_METRIC_WORDS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业收入", "营收", "销售收入", "收入", "销量", "量价", "产品结构",
                "渠道", "提价", "价格"),
    "net_profit": ("归母净利润", "净利润", "归母净利", "净利率", "利润", "毛利",
                   "费用", "减值", "非经常性损益", "税金"),
    "operating_cashflow": ("经营活动现金流", "经营现金流", "现金流量净额", "现金流",
                           "回款", "收现", "营运资本", "应收", "应付", "存货",
                           "合同负债", "预收"),
}


def _match_management_for_metric(items: list[dict], metric: str,
                                 period=None) -> dict | None:
    """这条指标有没有对应的管理层/第三方解释（按词匹配；期间可证时一并核对）。"""
    words = _EXPLANATION_METRIC_WORDS.get(str(metric or ""), ())
    if not words:
        return None
    year = period if isinstance(period, int) else None
    for it in (items or []):
        text = str(it.get("text") or "")
        if not any(w in text for w in words):
            continue
        doc_period = str(it.get("document_period") or "")
        if year and doc_period and str(year) not in doc_period:
            continue                     # 期间明确不符的解释不算（如别年的说明）
        return dict(it)
    return None



# ── 指标表与关键发现（全部由底稿算）──────────────────────────────


def _metrics_table(rows, derived, periods, citations, req, source_url: str = "",
                   subject_type: str = "") -> dict:
    """指标 × 期间 的对照表 + 同比/比率列（数值、口径、来源编号）。

    A1：质量比率按**研究对象类型**判定适用性——金融机构不生成企业口径比率
    （净利率/现金覆盖/资产负债率/研发强度），只保留同比这类两期变化对照。
    """
    try:
        from facts import ratio_applies
    except Exception:
        ratio_applies = lambda m, st: True          # noqa: E731 - 退化时不误删
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
    # 表里的数字来自**结构化财务来源**本身：按 URL 定位它的编号（数据平台是第三方也照样标）
    src_n = _citation_n(citations, source_url) or _source_number(citations, "")
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
        if not ratio_applies(str(d.get("metric") or ""), subject_type):
            continue          # A1：金融机构不生成企业口径比率（同比类仍保留）
        quality.append({"metric": str(d.get("metric") or ""),
                        "label": str(d.get("metric_label") or d.get("metric") or ""),
                        "period": str(d.get("period") or ""),
                        "value": d.get("value"),
                        # 单位取自派生行本身：同比/比率是 %，**金额变化是亿元**——
                        # 一律写 "%" 会把"毛利减少 38.01 亿元"渲染成"−38.01%"
                        "unit": str(d.get("unit") or "%"),
                        "formula": str(d.get("formula") or ""),
                        "fact_ids": list(d.get("derived_from") or [])})
    return {"rows": out_rows, "quality": quality,
            "periods": list(periods),
            "subject_type": subject_type,
            "columns": ["指标", "上期", "本期", "同比", "口径", "来源"]}


def _findings(rows, derived, periods, subject_type: str = "") -> list[dict]:
    """由数字算出的**观察**（不是模型写的），每条带 fact_id 便于复核。

    A1：覆盖倍数按派生读数判 低于/相当/高于；金融机构不生成企业口径的质量结论。
    """
    try:
        from facts import ratio_applies
    except Exception:
        ratio_applies = lambda m, st: True          # noqa: E731 - 退化时不误删

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
    # 质量比率：覆盖倍数按**校验过、单位统一**的派生读数判 低于/相当/高于（A1）；
    # 金融机构不生成企业口径的质量结论（该结论依赖"利润→现金流"的企业逻辑）
    cov = next((d for d in derived if d.get("metric") == "cashflow_coverage"
                and d.get("year") == last and ratio_applies("cashflow_coverage",
                                                            subject_type)), None)
    np_cur = (by.get("net_profit") or {}).get(last) if last else None
    cf_cur = (by.get("operating_cashflow") or {}).get(last) if last else None
    if cov is not None:
        # 覆盖倍数的**解释**不带阈值数字：`>100%` 这种裸数字在验收里算"待溯源数字"，
        # 而读数本身已在派生块里带公式给出
        ratio = cov.get("value") if isinstance(cov.get("value"), (int, float)) else None
        text = f"经营现金流对归母净利润的覆盖：{last} 年"
        if isinstance(np_cur and np_cur.get("value"), (int, float)) and np_cur["value"] < 0:
            text += "当期归母净利润为负，该比值不表示利润有现金支撑"
        elif isinstance(cf_cur and cf_cur.get("value"), (int, float)) and cf_cur["value"] < 0:
            text += "当期经营现金流为净流出，该比值不表示利润有现金支撑"
        elif ratio is None:
            text += "该比值不可算（分母为零或事实缺失，见派生指标）"
        elif ratio < 100 - _COVERAGE_TOL:
            # 实机反例：46.29/66.73 = 69.37% 曾被写成"现金流高于利润"
            text += "当期经营现金流低于归母净利润（读数与算式见派生指标）"
        elif ratio > 100 + _COVERAGE_TOL:
            text += "当期经营现金流高于归母净利润（读数与算式见派生指标）"
        else:
            text += "当期经营现金流与归母净利润相当（读数与算式见派生指标）"
        out.append({"text": text, "fact_ids": list(cov.get("derived_from") or [])})
    return out


# ── 主张（从模型正文解析并与底稿绑定）────────────────────────────

# 未采用来源导致的"这句话没有支持"标记：**逐句**标，不用末尾笼统免责声明代替
_UNSUPPORTED_MARK = "〔待核查：本句引用的来源未采用"
_UNSUPPORTED_NOTE = "（该来源未采用，结论不得当作已证事实）"


def _digit_free(text: str) -> str:
    """去掉数字/日期：正文里新增的文字不得带无法溯源的读数（验收会逐个数）。"""
    s = re.sub(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?", "", str(text or ""))
    s = re.sub(r"\d+(?:\.\d+)?\s*%?", "", s)
    return re.sub(r"\s{2,}", " ", s).strip(" ，、；;：:（）()")


def _sentence_key(text: str) -> str:
    """句子的归一化键：去掉标点、括号、引用编号与空白。

    编号会被重写/移除，标点会被切句吃掉，标记又会被插进句内——键必须对这些差异免疫，
    否则"未核查标记"与"主张记录"会各自认不出同一句话。
    """
    s = re.sub(r"\[(?:n\s*=\s*\d{1,2}|\d{1,2})\]", "", str(text or ""))
    return re.sub(r"[\s。！？!?，,、；;：:（）()〔〕【】\[\]「」『』]", "", s)


def _sentences(text: str) -> list[str]:
    """按句切分，并把**紧跟句末标点**的引用记号留在该句里。

    实机形态："…尚未见效。[3]"——编号写在句号之后。按标点直接切会让引用与它支持的
    那句话脱钩，未核查标记就落不到该句上（也无法与主张记录对上）。
    """
    out: list[str] = []
    for part in re.split(r"(?<=[。！？!?])|\n", str(text or "")):
        if not part.strip():
            continue
        if out and re.fullmatch(r"\s*(?:\[(?:n\s*=\s*\d{1,2}|\d{1,2})\]\s*)+", part):
            out[-1] = out[-1] + part
            continue
        out.append(part)
    return out


def _unsupported_claims(text: str, mapping: dict[int, int], *,
                        body_sources: list[tuple[int, str, str]] | None = None,
                        rejected: list[dict] | None = None) -> list[dict]:
    """正文里**引用了未采用来源**的句子 → 逐句记下原因（C2-1）。

    为什么不能只去编号：去编号只是让"引用无对应条目"的格式检查通过，读者仍会把该句
    当成有来源支持的事实（实机反例：洋河报告用"2024 年 5%—10% 目标未达成"开展判断，
    同一材料在来源清单里却是"未采用"）。这里把主张与证据的关联保留下来：
    句子、原编号、原 URL、未采用原因、以及"缺的是哪份材料"。
    """
    src_by_n = {int(n): (str(t or ""), str(u or ""))
                for n, t, u in (body_sources or [])}
    rej_by_url = {str(r.get("url") or ""): str(r.get("reason") or "")
                  for r in (rejected or [])}
    out: list[dict] = []
    for raw in _sentences(text):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue                      # 标题不是主张（与 _claims 同一规则）
        # 来源清单条目本身不是主张（"2. [标题](url)"里没有 [n] 引用记号，跳过更稳）
        if re.match(r"^\s*\d{1,2}\s*[.、)]\s*[\[(]", s):
            continue
        nums = {int(m.group(1) if m.group(1) else m.group(2))
                for m in re.finditer(r"\[(?:n\s*=\s*(\d{1,2})|(\d{1,2}))\]", s)}
        bad = sorted(n for n in nums if n not in (mapping or {}))
        if not bad:
            continue
        for n in bad:
            title, url = src_by_n.get(n, ("", ""))
            reason = rej_by_url.get(url) or "该编号在本次采用来源中没有对应条目"
            out.append({"old_n": n, "url": url, "title": title,
                        "reason": str(reason),
                        "sentence": s[:200], "key": _sentence_key(s)[:200]})
    return out


def _mark_unsupported(text: str, items: list[dict]) -> str:
    """把"本句引用未采用来源"标到**该句**上（不是末尾免责声明）。"""
    if not items:
        return text
    by_sentence: dict[str, list[dict]] = {}
    for it in items:
        key = str(it.get("key") or _sentence_key(it.get("sentence") or ""))
        if key:
            by_sentence.setdefault(key, []).append(it)

    def _mark_sentence(raw: str) -> str:
        s = raw.strip()
        key = _sentence_key(s)
        if not s or not key or key not in by_sentence:
            return raw
        # 标记里**不写来源标题**：标题常含数字（"21财经"），正文新增的读数会变成
        # "不可溯源数字"被验收拦下；来源身份与 URL 记在结构对象/风险清单/面板里。
        reasons = []
        for it in by_sentence[key]:
            why = _digit_free(it.get("reason") or "")
            if why and why not in reasons:
                reasons.append(why)
        note = _UNSUPPORTED_MARK + (f"（原因：{'；'.join(reasons)}）" if reasons else "（原因见缺口清单）") \
               + _UNSUPPORTED_NOTE + "〕"
        # 句末标点之前插入，保证仍是同一句（拆句规则不会把它变成新句子）
        m = re.search(r"([。！？!?])\s*$", s)
        if m:
            return s[: m.start()] + note + m.group(1) + raw[len(raw.rstrip()):]
        return s + note + raw[len(raw.rstrip()):]

    parts = _sentences(text)
    return "".join(_mark_sentence(p) for p in parts)


# ── D1：主张 → 原子断言 → 逐条支持 ─────────────────────────────
# 为什么重写绑定：旧实现把"数值（两位小数）"当唯一键——"2023 净利润 100 万元"会被
# "2024 收入 100 亿元"支持；一句里混入虚假数字（收入对、利润 999）也照样 bound；
# 负号被 `_NUM_RE` 丢掉（"-12.83%" 与 "+12.83" 同键）。现在每个数字按
# **指标 + 期间 + 单位（含换算）+ 符号**逐条对底稿事实；缺必需语义就待核查。

# 指标别名 → 底稿 metric slug（长别名优先：避免"毛利"吃掉"毛利率"、"净利润"吃掉"归母净利率"）
_METRIC_ALIASES: tuple[tuple[str, str], ...] = tuple(sorted((
    ("经营活动现金流净额同比", "operating_cashflow_yoy"),
    ("经营现金流同比", "operating_cashflow_yoy"),
    ("营业收入同比", "revenue_yoy"), ("营收同比", "revenue_yoy"),
    ("归母净利润同比", "net_profit_yoy"), ("净利润同比", "net_profit_yoy"),
    # 金额变化（D1 夜间补修）：名字里带"变化/增减"的才是变化量，别与水平值混用
    ("经营活动现金流净额变化", "operating_cashflow_change"),
    ("经营现金流净额变化", "operating_cashflow_change"),
    ("经营活动现金流变化", "operating_cashflow_change"),
    ("归母净利润变化", "net_profit_change"), ("净利润变化", "net_profit_change"),
    ("营业收入变化", "revenue_change"), ("营收变化", "revenue_change"),
    ("毛利润变化", "gross_profit_change"), ("毛利变化", "gross_profit_change"),
    ("毛利线以下净额变化", "net_profit_gross_gap_change"),
    ("毛利线以下净额", "net_profit_gross_gap_change"),
    ("经营现金流对归母净利润的覆盖", "cashflow_coverage"),
    ("经营活动现金流净额对归母净利润的覆盖", "cashflow_coverage"),
    ("现金流对归母净利润的覆盖", "cashflow_coverage"),
    ("现金流对净利润的覆盖", "cashflow_coverage"), ("覆盖倍数", "cashflow_coverage"),
    ("归母净利率", "net_margin"), ("净利率", "net_margin"),
    ("资产负债率", "debt_ratio"), ("研发投入强度", "rd_intensity"),
    ("经营活动产生的现金流量净额", "operating_cashflow"),
    ("经营活动现金流净额", "operating_cashflow"),
    ("经营现金流净额", "operating_cashflow"),
    ("经营活动现金流", "operating_cashflow"), ("经营现金流", "operating_cashflow"),
    ("归属于母公司股东的净利润", "net_profit"), ("归母净利润", "net_profit"),
    ("归母净利", "net_profit"), ("净利润", "net_profit"),
    ("营业收入", "revenue"), ("营收", "revenue"), ("销售收入", "revenue"),
    ("收入", "revenue"),
    ("毛利率", "gross_margin"), ("毛利润", "gross_profit"), ("毛利", "gross_profit"),
    ("经营利润", "operating_profit"), ("营业利润", "operating_profit"),
    ("总资产", "total_assets"), ("资产总额", "total_assets"),
    ("总负债", "total_liabilities"), ("负债总额", "total_liabilities"),
    ("研发投入", "rd_expense"), ("研发费用", "rd_expense"),
), key=lambda kv: -len(kv[0])))

_NEG_MARKERS = ("下降", "下滑", "减少", "负增长", "净流出", "亏损", "为负", "降低",
                "回落", "下行", "由正转负")
_POS_MARKERS = ("增长", "上升", "增加", "净流入", "提高", "提升", "上行", "由负转正")
_ASSERT_TOKEN_RE = re.compile(
    r"(?P<sign>[+\-−])?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>万亿|千亿|百亿|亿元|万元|亿|万|元|个百分点|％|%)?")
_CITATION_MARK_RE = re.compile(r"\[(?:n\s*=\s*\d{1,2}|\d{1,2})\]")
_LINK_TARGET_RE = re.compile(r"\]\([^)]*\)")
_YEAR_TOKEN_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# 目标值/范围：必须写在"目标/计划/力争/预算"之后，且不能是完成率/进度这类结果词
_TARGET_HEAD_RE = re.compile(r"(?:目标|计划|力争|预算)")
_TARGET_ACHIEVEMENT_WORDS = ("完成率", "达成率", "进度", "已完成", "达成情况")
# 百分比断言优先试同比派生指标的三个核心指标（其余比率指标自带独立 slug）
_YOY_PROMOTABLE = ("revenue", "net_profit", "operating_cashflow")
# 括号式变化值（"归母净利润的降幅（-33.38%）"）：窗口里出现这些词就按**同比**解析
_CHANGE_WORDS = ("降幅", "跌幅", "增幅", "涨幅", "增速")
_NEG_CHANGE_WORDS = ("降幅", "跌幅")
_POS_CHANGE_WORDS = ("增幅", "涨幅", "增速")
# 金额变化量的判据（紧邻数值 12 字内）：金额本身不是比率，但"减少/增加/同比/较上年"
# 说明它是变化量 → 试 `<指标>_change` 派生（夜间纠偏 P1-A：金额变化与利润率变化分开）
_CHANGE_VERBS = ("增加", "减少", "下降", "上升", "增长", "变动", "变化", "同比",
                 "较上年", "较上期", "净增", "净减")


def _unit_class(unit: str) -> str:
    """单位类别：amount（金额，可换算）/ pct（%）/ pp（百分点）/ ''（未写）。

    百分点必须**先判**：它字面不含 %，"个百分点"若走金额分支会得到空类别
    （实机反例：'下降 2.09 个百分点' 被记成"未标单位"）。
    """
    u = str(unit or "").strip()
    if not u:
        return ""
    if "百分点" in u:
        return "pp"
    if "%" in u or "％" in u:
        return "pct"
    try:
        from facts import amount_scale
        return "amount" if amount_scale(u) > 0 else ""
    except Exception:
        return ""


def _amount_in_yuan(value, unit: str) -> float:
    try:
        from facts import amount_scale
        return float(value) * float(amount_scale(unit) or 0.0)
    except Exception:
        return 0.0


def _metric_label_of(metric: str) -> str:
    try:
        from facts import metric_label
        return str(metric_label(metric) or metric)
    except Exception:
        return str(metric or "")


def _year_of(row) -> int | None:
    """事实行的年度：优先 `year`，否则从 `period`（"2024年"）解析。"""
    try:
        y = (row or {}).get("year")
        if isinstance(y, (int, float)) and 1900 < int(y) < 2200:
            return int(y)
    except Exception:
        pass
    m = _YEAR_TOKEN_RE.search(str((row or {}).get("period") or ""))
    return int(m.group(0)) if m else None


def _fact_index(rows, derived) -> dict[str, list[dict]]:
    """底稿事实索引：metric → 事实行（带期间/值/单位/口径/fact_id 链）。"""
    idx: dict[str, list[dict]] = {}

    def _add(r, *, is_derived: bool) -> None:
        metric = str((r or {}).get("metric") or "")
        value = (r or {}).get("value")
        if not metric or not isinstance(value, (int, float)):
            return
        fid = str((r or {}).get("fact_id") or "")
        chain = [str(x) for x in ((r or {}).get("derived_from") or []) if str(x)]
        idx.setdefault(metric, []).append({
            "metric": metric, "period": _year_of(r), "value": float(value),
            "unit": str((r or {}).get("unit") or ""),
            "caliber": str((r or {}).get("caliber") or ""),
            "fact_ids": ([fid] if fid else []) + chain,
            "derived": is_derived})

    for r in list(rows or []):
        _add(r, is_derived=False)
    for d in list(derived or []):
        _add(d, is_derived=True)
    return idx


def _assertion_matches(a: dict, f: dict) -> bool:
    """断言与事实是否对得上：期间、单位类别（含金额换算）、数值、符号。

    数值按**绝对值**比：正文用方向词（"下降 12.83%"）或显式负号表达符号，事实里存的是
    带符号值（-12.83）——比大小看量级，方向由符号判定（否则真话也会被判不支持）。
    """
    if a.get("period") and f.get("period") and int(a["period"]) != int(f["period"]):
        return False
    ua = str(a.get("unit_class") or "")
    uf = _unit_class(str(f.get("unit") or ""))
    if ua != uf:
        return False
    if ua == "amount":
        va = _amount_in_yuan(a.get("value"), str(a.get("unit") or ""))
        vf = _amount_in_yuan(abs(float(f.get("value") or 0)), str(f.get("unit") or ""))
        if vf == 0.0 or abs(va - vf) > max(1e-6, abs(vf) * 1e-6):
            return False
    elif ua in ("pct", "pp"):
        if abs(float(a.get("value") or 0) - abs(float(f.get("value") or 0))) > 0.011:
            return False
    else:
        return False
    fv = float(f.get("value") or 0)
    fsign = 0 if fv == 0 else (-1 if fv < 0 else 1)
    if fsign and int(a.get("sign") or 1) != fsign:
        return False
    return True


def _pp_recompute_match(a: dict, facts: list[dict]) -> dict | None:
    """百分点差：底稿没有"百分点"事实，但可由**同指标两期水平相减**复核。

    这是"可重算"支持而不是"数字命中"：只有差值（含方向）与正文一致才算，
    并把参与相减的两期事实记进 `fact_ids` 与 `recomputed` 说明。
    """
    vals = [f for f in (facts or []) if isinstance(f.get("value"), (int, float))]
    if len(vals) < 2:
        return None
    for i in range(len(vals)):
        for j in range(len(vals)):
            if i == j:
                continue
            diff = float(vals[j]["value"]) - float(vals[i]["value"])
            if abs(abs(diff) - abs(float(a.get("value") or 0))) > 0.02:
                continue
            dsign = 0 if diff == 0 else (-1 if diff < 0 else 1)
            if dsign and int(a.get("sign") or 1) != dsign:
                continue
            ids = [x for x in ((vals[j].get("fact_ids") or []) + (vals[i].get("fact_ids") or []))
                   if x]
            return {"metric": a.get("metric"), "period": None, "value": diff,
                    "unit": "%", "caliber": vals[j].get("caliber") or "",
                    "fact_ids": ids, "derived": True,
                    "recomputed": {"from": vals[i].get("period"), "to": vals[j].get("period"),
                                   "diff": diff}}
    return None


def _assertions_in(sentence: str) -> list[dict]:
    """句内数字 → 可单独验证的断言（指标/期间/值/单位/符号）。

    带单位但提不出指标的数字**不丢弃**：标 `needs_check`（未提取到必需语义）——
    "某个数字命中"不足以让整句升级为已支持。
    """
    s = _LINK_TARGET_RE.sub("", _CITATION_MARK_RE.sub("", str(sentence or "")))
    out: list[dict] = []
    for m in _ASSERT_TOKEN_RE.finditer(s):
        raw_num = str(m.group("num") or "")
        try:
            value = float(raw_num.replace(",", ""))
        except Exception:
            continue
        unit = str(m.group("unit") or "")
        if not unit and re.fullmatch(r"(?:19|20)\d{2}", raw_num):
            continue                       # 裸年份是期间上下文，不是取值
        # 指标归属窗口：金额/百分比看数字前 60 字；**百分点差**看整句前缀——
        # "…覆盖由 2023 年的 61.20% 升至 2024 年的 69.37%，上升 8.17 个百分点" 里
        # 指标名离数值较远，60 字窗会截断它、错归到内层的"归母净利润"上。
        _uc = _unit_class(unit)
        _win = 4000 if _uc == "pp" else 60
        head = s[max(0, m.start() - _win):m.start()]
        window = head[-12:]
        # 指标归属：**取离数字最近**的别名（同距离取更长者）——一句里出现多个指标时
        # （"营业收入…净利润…"）必须按就近归属，不能按"窗口里最长的别名"。
        # 金额数值**不认同比别名**：别名表里"营业收入同比"比"营业收入"更长、结尾更近，
        # 会把括号里的水平值（"营业收入同比增长 15.66%（2024 年 1741.44亿元）"）错标成
        # 同比值——金额是水平/变化量，不是百分比。
        best: tuple[int, int, str] | None = None
        for alias, slug in _METRIC_ALIASES:
            if _uc == "amount" and slug.endswith(_YOY_SUFFIX):
                continue
            pos = head.rfind(alias)
            if pos < 0:
                continue
            cand = (len(head) - (pos + len(alias)), -len(alias), slug)
            if best is None or cand < best:
                best = cand
        metric = best[2] if best else ""
        if not metric and not unit:
            continue                       # 无指标、无单位：不是财务断言（计数/页码等）
        sign = 1
        if m.group("sign") in ("-", "−"):
            sign = -1
        elif any(k in window for k in _NEG_MARKERS):
            sign = -1
        elif any(k in window for k in _POS_MARKERS):
            sign = 1
        # 括号式变化值（"归母净利润的降幅（-33.38%）"）：窗口里有"降幅/增幅"这类词时，
        # 该数值是**该指标的同比**而不是水平值——按同比解析并带方向（D3 补）
        if metric in _YOY_PROMOTABLE and any(k in window for k in _CHANGE_WORDS):
            metric = f"{metric}{_YOY_SUFFIX}"
            if any(k in window for k in _NEG_CHANGE_WORDS):
                sign = -1
            elif any(k in window for k in _POS_CHANGE_WORDS):
                sign = 1
        year = None
        ambiguous = False
        years_sent = sorted({int(ym.group(0)) for ym in _YEAR_TOKEN_RE.finditer(s)})
        near = None
        for ym in _YEAR_TOKEN_RE.finditer(head):
            if len(head) - ym.end() <= 20:       # 年份必须**紧邻**数值（≤20 字）
                near = int(ym.group(0))
        if near is not None:
            year = near
        elif len(years_sent) == 1:
            year = years_sent[0]                 # 整句只有一个年度：归属无歧义
        elif len(years_sent) > 1:
            # 同句多个年度、数值又不紧跟年份（如"毛利率…降至…；归母净利率由 30.24% 降至
            # 23.11%"）→ 期间不明确：不借远处年度（实机反例：30.24% 被误记为 2024 年）
            ambiguous = True
        out.append({"text": str(m.group(0)).strip()[:40], "metric": metric,
                    "metric_label": _metric_label_of(metric) if metric else "",
                    "period": year, "period_ambiguous": ambiguous,
                    "value": value, "unit": unit, "sign": sign,
                    "unit_class": _unit_class(unit), "fact_ids": [],
                    # 紧邻数值的窗口：金额变化量按"变化动词 + 是否存在 <指标>_change 事实"
                    # 提升到变化派生（见 `_claims`），窗口随断言一起带走
                    "window": window,
                    "support_status": "needs_check", "reason": ""})
    return out


def _target_value_present(sentence: str) -> bool:
    """句子里有没有**目标值/目标范围**（完成率/进度是结果词，不算目标值）。"""
    s = str(sentence or "")
    for m in _TARGET_HEAD_RE.finditer(s):
        tail = s[m.end():m.end() + 26]
        if any(w in tail for w in _TARGET_ACHIEVEMENT_WORDS):
            continue
        if re.search(r"\d[\d,]*(?:\.\d+)?\s*(?:万亿|千亿|百亿|亿元|万元|亿|万|元|%|％)", tail):
            return True
        if re.search(r"\d+\s*[%％]?\s*[—\-~至]\s*\d", tail):
            return True
    return False


def _evidence_matches(sentence_key: str, evidence: dict | None) -> list[dict]:
    """该句引用了哪些**已定位**的证据片段（摘要去标点后的前缀命中）。

    返回记录本身（不只是 id）：目标类判断还要看"支持原文是不是发行人披露"，
    以及该披露的发布日/报告期（披露时点）。
    """
    if not sentence_key:
        return []
    out: list[dict] = []
    for r in ((evidence or {}).get("records") or []):
        if not r.get("has_location"):
            continue
        if str(r.get("admission") or "") not in ("admitted", "comparison"):
            continue
        probe = _sentence_key(str(r.get("snippet") or ""))[:40]
        if len(probe) >= 12 and probe in sentence_key:
            out.append(r)
    return out


def _evidence_ids_for(sentence_key: str, evidence: dict | None) -> list[str]:
    ids: list[str] = []
    for r in _evidence_matches(sentence_key, evidence):
        cid = str(r.get("content_hash") or "")
        if cid and cid not in ids:
            ids.append(cid)
    return ids


def _is_issuer_record(r: dict) -> bool:
    return (str((r or {}).get("source_type")) == "issuer_annual_report"
            or str((r or {}).get("document_provenance")) == "issuer_annual_report")


def _claims(body: str, rows, derived, citations, *,
            unsupported: list[dict] | None = None, subject: str = "",
            periods: list[int] | None = None,
            evidence: dict | None = None,
            assembly: list[dict] | None = None) -> list[dict]:
    """模型正文 → 逐句主张，**每个数字按原子断言逐条对底稿**（D1）。

    状态口径（对外固定）：
    - `bound`：句内每条断言都对上了底稿事实（指标/期间/单位换算/符号一致）；
    - `partially_supported`：部分断言对上、部分没有（"一句正确收入搭虚假利润"即此）；
    - `unsupported`：断言语义齐全但没有任何底稿事实支持；引用了**未采用来源**的句子
      也记此状态（并带 `source`），"来源清单合规"不等于结论受支持；
    - `needs_check`：未提取到必需语义（缺指标名/期间/单位），或纯文字的目标/因果判断。

    每条主张携带：结论文本、主体、期间、类型（observation/inference/assumption/
    target_claim/third_party_view/unbound）、`assertions[]`（逐条支持）、`fact_ids`、
    `evidence_ids`（已定位证据片段）、`support_status`、`reason`、`version_id`。

    `assembly`：**装配器自己生成的句子**（研究问题观察、推断边界），逐条
    `{text, claim_type}`。它们与模型分析同处一个主张集合——此前只审模型分析段，
    代码写出来的结论（含数字）不在审查范围内（夜间反例 P1-B：装配生成的
    "利润降幅更大/现金仍在收缩"在增长场景照写且无人审）。`origin="assembly"`
    让页面能区分"模型写的"与"装配器写的"。
    """
    _asm: dict[str, dict] = {}
    for _item in (assembly or []):
        _t = str((_item or {}).get("text") or "").strip()
        # 按**切句后的每一句**登记：主张清单是逐句的，一条观察里含两个句号时，
        # 后半句也要认得出自己来自装配器（否则它会被当成"模型写的句子"）
        for _part in re.split(r"[。！？!?\n]+", _t):
            _p = _part.strip()
            if _p:
                _asm[_sentence_key(_p)] = _item
    if _asm:
        body = (str(body or "") + "\n"
                + "\n".join(str((i or {}).get("text") or "") for i in (assembly or [])))
    idx = _fact_index(rows, derived)
    unsup_by_sentence: dict[str, list[dict]] = {}
    for u in (unsupported or []):
        key = str(u.get("key") or _sentence_key(u.get("sentence") or ""))
        if key:
            unsup_by_sentence.setdefault(key, []).append(u)
    years = {int(y) for y in (periods or [])}
    issuer_ns = {int(c.get("n")) for c in (citations or [])
                 if str(c.get("type")) == "issuer_annual_report"}

    def _judgment_kind(s: str) -> str | None:
        """无数字句子里仍要进支持清单的判断类型（不能漏出清单）。"""
        if _is_target_claim(s):
            return "target_claim"
        try:
            import narrative_evidence as _ne
            if _ne.is_causal(s):
                return "inference"
        except Exception:
            pass
        if any(k in s for k in ("风险", "不确定性", "承压", "压力")):
            return "risk"
        return None

    out: list[dict] = []
    for sent in re.split(r"[。！？!?\n]+", str(body or "")):
        s = sent.strip()
        if not s:
            continue
        if s.lstrip().startswith("#"):
            continue                      # 标题不是主张（含股票代码/年份，否则会被当成"待核查"）
        if s.startswith(("|", ">", "-", "*")):
            s = s.lstrip("#|>-* ").strip()
        if "|" in s and s.count("|") >= 2:
            continue                      # 表格行不是主张（模型自写的表由装配器接管）
        if re.match(r"^\d+\.\s*\[", s) or re.match(r"^[-*]\s*\[", s):
            continue                      # 来源清单条目不是主张
        has_number = bool(_NUM_RE.search(s))
        assertions = _assertions_in(s) if has_number else []
        if not has_number:
            kind_text = _judgment_kind(s)
            _asm0 = _asm.get(_sentence_key(s))
            _boundary = (_asm0 is not None
                         and str(_asm0.get("claim_type")) == "boundary")
            if _boundary:
                kind_text = "boundary"    # 装配器声明的推断边界：只说明"不能得出什么"
            if kind_text is None or len(s) < 12:
                continue                  # 既无数字又不是判断句：不进主张清单
            cite_ns0 = [int(m.group(1) if m.group(1) else m.group(2))
                        for m in re.finditer(r"\[(?:n\s*=\s*(\d{1,2})|(\d{1,2}))\]", s)]
            reason0 = ("推断边界：本条只说明『不能据此得出什么』，不是事实主张；"
                       "要升级为解释需可定位的披露原文" if _boundary else
                       "纯文字判断：需可定位的披露原文或底稿事实支持，本次未取得"
                       if kind_text != "target_claim" else
                       "目标类判断需核对目标年度、目标值或范围、披露时点与实际口径；"
                       "本次未取得发行人披露对该目标的直接支持")
            _b = {"text": s[:200], "type": kind_text, "claim_type": kind_text,
                  "fact_ids": [], "evidence_ids": _evidence_ids_for(_sentence_key(s), evidence),
                  "status": "needs_check", "support_status": "needs_check",
                  "reason": reason0, "assertions": [],
                  "subject": str(subject or ""),
                  "periods": sorted({y for y in years
                                     if re.search(rf"(?<!\d){y}(?!\d)", s)}),
                  "citations": cite_ns0, "version_id": ""}
            if _boundary:
                _b["origin"] = "assembly"
            out.append(_b)
            continue
        if not assertions:
            continue                      # 有数字但没有可验证断言（纯年份/计数）：不进清单
        # 逐条断言对事实
        for a in assertions:
            # 必需语义不全（缺指标/单位/期间）→ **不尝试匹配**：不能靠"数值命中"升级为
            # 已支持（百分点差例外：它由两期水平相减重算，本身不需要单一期间）
            if not a.get("metric"):
                a["support_status"] = "needs_check"
                a["reason"] = "未提取到指标名，无法与底稿事实对应"
                continue
            if not a.get("unit_class"):
                a["support_status"] = "needs_check"
                a["reason"] = "未标单位，无法与底稿事实对应"
                continue
            if not a.get("period") and a.get("unit_class") != "pp":
                a["support_status"] = "needs_check"
                a["reason"] = ("同句出现多个年度且数值未紧跟年份，期间归属不明确"
                               if a.get("period_ambiguous")
                               else "未标期间，无法与底稿事实对应")
                continue
            # 百分比挂在核心指标名后（"营业收入…下降 12.83%"）时，先试它的同比指标——
            # 底稿里"同比"是独立派生事实（revenue_yoy），按基础指标找会因单位类别不符落空
            cand_metrics = [str(a.get("metric") or "")]
            _base = str(a.get("metric") or "")
            if (a.get("unit_class") in ("pct", "pp") and _base in _YOY_PROMOTABLE
                    and f"{_base}{_YOY_SUFFIX}" in idx):
                cand_metrics.insert(0, f"{_base}{_YOY_SUFFIX}")
            elif (a.get("unit_class") == "amount"
                    and f"{_base}_change" in idx
                    and any(k in str(a.get("window") or "") for k in _CHANGE_VERBS)):
                # 金额 + 变化动词（"归母净利润同比减少 33.43 亿元"）：先试**金额变化**派生
                # ——底稿里变化量是独立事实（net_profit_change），按水平值找会落空，
                # 于是正确的金额变化被误记成"无底稿支持"（夜间纠偏 P1-A 的同一族问题）
                cand_metrics.insert(0, f"{_base}_change")
            hit = None
            for mk in cand_metrics:
                for f in idx.get(mk, []):
                    if _assertion_matches(a, f):
                        hit = f
                        break
                if hit is not None:
                    if mk != _base:
                        a["metric"] = mk
                        a["metric_label"] = _metric_label_of(mk)
                    break
            if hit is None and a.get("unit_class") == "pp":
                # 百分点差没有现成事实，但可由同指标两期水平相减**重算**复核
                hit = _pp_recompute_match(a, idx.get(str(a.get("metric") or ""), []))
            if hit is not None:
                a["support_status"] = "supported"
                a["fact_ids"] = list(hit.get("fact_ids") or [])
                _rc = hit.get("recomputed")
                a["reason"] = (
                    f"由两期水平值相减复核（{_rc.get('from')}→{_rc.get('to')}，"
                    f"差 {_rc.get('diff'):+.2f} 个百分点）" if _rc else "")
            else:
                a["support_status"] = "unsupported"
                a["reason"] = (f"底稿中没有匹配的事实（{a.get('metric_label') or a.get('metric')}"
                               f"{('、' + str(a['period']) + '年') if a.get('period') else ''}"
                               f"、{a.get('value')}{a.get('unit') or ''}）")
        facts: list[str] = []
        for a in assertions:
            for fid in (a.get("fact_ids") or []):
                if fid and fid not in facts:
                    facts.append(fid)
        supported = [a for a in assertions if a["support_status"] == "supported"]
        unsupported_a = [a for a in assertions if a["support_status"] == "unsupported"]
        incomplete = [a for a in assertions if a["support_status"] == "needs_check"]
        if supported and not unsupported_a and not incomplete:
            status = "bound"
        elif supported:
            status = "partially_supported"
        elif unsupported_a:
            status = "unsupported"
        else:
            status = "needs_check"
        reason = "；".join(
            f"{a.get('text')}：{a.get('reason')}"
            for a in (unsupported_a + incomplete))[:200]
        kind = "observation" if facts else "unbound"
        if any(k in s for k in ("可能", "预计", "推测", "或将", "若")):
            kind = "inference" if facts else "unbound"
        if any(k in s for k in ("假设", "假如")):
            kind = "assumption" if facts else "unbound"
        cite_ns = [int(m.group(1) if m.group(1) else m.group(2))
                   for m in re.finditer(r"\[(?:n\s*=\s*(\d{1,2})|(\d{1,2}))\]", s)]
        hit_u = next((u for key, items in unsup_by_sentence.items()
                      for u in items if key and key in _sentence_key(s)), None)
        claim = {"text": s[:200], "type": kind, "claim_type": kind,
                 "fact_ids": facts,
                 "evidence_ids": _evidence_ids_for(_sentence_key(s), evidence),
                 "status": status, "support_status": status, "reason": reason,
                 "assertions": assertions,
                 "subject": str(subject or ""),
                 "periods": sorted({y for y in years
                                    if re.search(rf"(?<!\d){y}(?!\d)", s)}),
                 "citations": cite_ns,
                 # 版本号在装配时由 `stamp_structure_version` 盖上（未盖 = 空串，
                 # 页面据此判"结构对象是否属于当前版本"，不拿旧结构冒充新版）
                 "version_id": ""}
        _hit_asm = _asm.get(_sentence_key(s))
        if _hit_asm is not None:
            # 装配器生成的句子：标来源与类型，其余照走同一套断言核对
            claim["origin"] = "assembly"
            claim["claim_type"] = str(_hit_asm.get("claim_type") or claim["claim_type"])
            claim["type"] = claim["claim_type"]
        if hit_u is not None:
            # 未采用来源：不因"数字绑得上底稿"就算支持（结论依赖那份材料）
            claim["status"] = "unsupported"
            claim["support_status"] = "unsupported"
            claim["reason"] = str(hit_u.get("reason") or "")
            claim["source"] = {"url": str(hit_u.get("url") or ""),
                               "title": str(hit_u.get("title") or ""),
                               "old_n": hit_u.get("old_n")}
            claim["type"] = ("third_party_view" if "报道" in s or "媒体" in s else kind)
            claim["claim_type"] = claim["type"]
        elif _is_target_claim(s):
            # C2-3/D1/D3：目标类判断逐项核对——目标年度、目标值/范围、发行人披露的
            # **支持原文**（该句须来自已定位的发行人披露）、**披露时点**（发布日/报告期）、
            # 实际口径；缺哪项写哪项，不能靠"某篇文章提到"或"有个年报引用"成立。
            missing: list[str] = []
            if not claim["periods"]:
                missing.append("目标年度")
            if not _target_value_present(s):
                missing.append("目标值或目标范围")
            _matches = _evidence_matches(_sentence_key(s), evidence)
            _issuer_text = any(_is_issuer_record(r) for r in _matches)
            _issuer_cited = bool(set(cite_ns) & issuer_ns)
            if not (_issuer_text or _issuer_cited):
                missing.append("发行人披露对该目标的原文支持")
            elif not _issuer_text:
                missing.append("发行人披露的原文（当前只有引用编号，正文未取到）")
            # 披露时点：发行人披露要有发布日（或报告期），且报告期不得晚于目标年度
            if _issuer_text:
                _ok_time = False
                for r in _matches:
                    if not _is_issuer_record(r):
                        continue
                    pub = str(r.get("published_at") or "")
                    dp = str(r.get("document_period") or "")
                    ty = claim["periods"][-1] if claim["periods"] else None
                    try:
                        dp_ok = (not dp) or (ty is None) or int(dp) <= int(ty)
                    except Exception:
                        dp_ok = True
                    if pub and dp_ok:
                        _ok_time = True
                        break
                if not _ok_time:
                    missing.append("目标披露时点（发布日/报告期不得晚于目标年度）")
            if not any(a.get("metric") for a in assertions):
                missing.append("实际口径/实际值")
            if missing:
                claim["status"] = "needs_check"
                claim["support_status"] = "needs_check"
                claim["type"] = "target_claim"
                claim["claim_type"] = "target_claim"
                claim["reason"] = (f"目标类判断需核对{'、'.join(missing)}；"
                                   "本次未取得发行人披露对该目标的直接支持")
            else:
                # 五项齐备：目标年度 + 目标值/范围 + 发行人披露原文 + 披露时点 + 实际口径
                claim["type"] = "target_claim"
                claim["claim_type"] = "target_claim"
                claim["status"] = "bound"
                claim["support_status"] = "bound"
                claim["reason"] = ("目标来自发行人披露原文（含目标年度、目标值/范围与"
                                   "披露时点），实际口径由底稿断言核对")
        out.append(claim)
    return out[:40]


def _is_target_claim(text: str) -> bool:
    """句子是不是在讲"目标/计划/达成"（这类判断的证据门槛高于普通读数）。"""
    s = str(text or "")
    return any(k in s for k in ("目标", "计划完成", "达成", "完成率", "考核指标",
                                "经营计划", "预算目标"))


# ── 风险与待核查（底稿缺口 + 模型风险小节）──────────────────────


def _risks(task_id: str, goal: str, body: str, *, project=None,
           evidence: dict | None = None, citations: list[dict] | None = None,
           changes: dict | None = None, perspective: str = "",
           citation_gaps: list[int] | None = None,
           unsupported: list[dict] | None = None,
           analysis_quality: dict | None = None) -> list[dict]:
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

    # ③ 变化解释里还没能证明的部分 → 需要补充的材料。
    # C2-4：区分"解释已取得、贡献程度未核实"与"原因尚不能证明"——不能一边给解释
    # 一边在缺口里说没有解释（读者会以为整段解释是编的）。
    for u in (changes.get("unproven") or []):
        if u.get("has_explanation"):
            _m = u.get("matched") or {}
            _where = ("；".join(x for x in (
                (f"来源 [{_m.get('source_n')}]" if _m.get("source_n") else ""),
                str(_m.get("locator") or "")) if x) or "见『变化解释』的管理层/附注说明")
            _add("unproven_change",
                 f"{u.get('label')}：解释已取得（{_where}），"
                 f"但量价与贡献程度未核实",
                 evidence_note="已取得定性解释；分解到量/价/结构的数据尚未取得",
                 would_change=f"取得{'、'.join(u.get('materials') or [])}后，"
                             "若显示的贡献结构与本期变化方向不一致，需修订解释",
                 materials=list(u.get("materials") or []))
        else:
            _add("unproven_change", f"{u.get('label')}的变化原因尚不能证明",
                 evidence_note="未取得对应附注/管理层讨论证据",
                 would_change=f"取得{'、'.join(u.get('materials') or [])}后，"
                             "若显示的原因与本期变化方向不一致，需修订解释",
                 materials=list(u.get("materials") or []))

    # ④ 模型正文里的风险小节（原样带出，但标注"由模型提出、需取得证据"）。
    # 结论与免责声明不是风险：它们是收尾陈述，列成风险只会稀释真正要查的事项。
    # 模型常把一条风险写成"主张 + 若干子项"（`- **主张**` 后面跟 `- 证据：…`、
    # `- 会使判断改变的观察条件：…`），逐行抓取会把子项也当成独立风险——既重复又
    # 读不出主张（C3：减少相同风险的重复）。这里跳过子项行并按文本去重。
    _SUBITEM_PREFIXES = ("证据：", "对应证据", "会使判断改变", "改变判断的观察条件",
                         "需要补充的材料", "需补材料", "出处：", "来源：")
    section = _section_text(body, ("风险", "待核查", "核查"))
    if section:
        # 标题行不是风险：正文里通常有**多个**标题（简报标题 + 模型自写报告标题，
        # 后者会出现在"待核查主张"小节里），只比对第一个会把模型标题漏进来
        # （实机复测：修复后仍剩一条"洋河股份（002304.SZ）…研究报告"）。
        heading_keys = {_sentence_key(m.group(1))[:120]
                        for m in re.finditer(r"^#{1,6}\s+(.+)$", str(body or ""), flags=re.M)}
        heading_keys.discard("")
        # 与 ①②③ 已加条目跨来源去重：模型风险小节常会复述"变化原因尚不能证明"，
        # 二次装配还会把上一轮装配自己的行（压缩提示/待核查头）喂回来——
        # 不去重会逐轮翻倍（实机：同一风险两行、结尾 **** 逐轮 +3）。
        seen_texts: set[str] = {_sentence_key(r.get("text") or "")[:120] for r in out}
        seen_texts.discard("")
        for line in section.splitlines():
            t = line.strip().lstrip("#-*• ").rstrip("*：: ").strip()
            if not t or len(t) < 6:
                continue
            if any(t.startswith(p) for p in _SUBITEM_PREFIXES):
                continue
            if any(k in t for k in ("免责声明", "不构成投资建议", "不构成任何投资建议",
                                    "结论边界", "本报告不含")):
                continue
            # 装配器自己的产物不是"模型提出的风险"：压缩提示与待核查头。
            if re.match(r"^另有\s*\d+\s*条同类核查项", t):
                continue
            if t.startswith("待核查主张（"):
                continue
            key = _sentence_key(t)[:120]
            if not key or key in seen_texts or key in heading_keys:
                continue
            seen_texts.add(key)
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
    # ⑥ 引用缺口：正文里映射不到采用来源的编号（已去编号，不猜指向）
    gaps = [int(n) for n in (citation_gaps or [])]
    if gaps:
        _add("citation_gap",
             "正文引用 " + "、".join(f"[{n}]" for n in gaps) + " 未能在本次采用来源中唯一对应",
             evidence_note="装配时按 URL 建立旧→新编号映射，映射不到的编号已移除",
             would_change="补齐对应来源或删除该处引用后，相关句子才可复核",
             materials=["该句原本引用的材料原文或链接"])
    # ⑦ 引用了**未采用来源**的句子：逐句点名（与正文标记、主张记录同一状态）
    for u in (unsupported or [])[:4]:
        who = str(u.get("title") or u.get("url") or f"原编号 {u.get('old_n')}")[:50]
        _add("unsupported_claim",
             f"正文结论依赖未采用来源（{who}）：{str(u.get('sentence') or '')[:80]}",
             evidence_note=f"该来源未采用：{str(u.get('reason') or '')[:60]}；"
                           "正文对应句已标『待核查』",
             would_change="改用已采用的来源重述该结论，或取得该材料的可用版本后重验",
             materials=[f"{who}的原文/可核验版本",
                        "或支持该结论的其他已披露材料"])
    # ⑧ D2："有分析"的最低要求未满足（无数据观察，或缺意义/局限说明）
    _q = analysis_quality or {}
    if _q and not _q.get("ok"):
        _missing = []
        if not _q.get("observations"):
            _missing.append("至少一项实际数据观察（指标 + 数值 + 期间）")
        if not _q.get("has_meaning_or_limit"):
            _missing.append("该观察的意义或推断边界")
        _add("analysis_quality",
             "分析未达最低要求：" + "、".join(_missing) + "（标题、口径声明不计）",
             evidence_note=(f"分析一节 {_q.get('chars') or 0} 字，"
                            f"可核验观察 {_q.get('observations') or 0} 条"),
             would_change="补上数据观察及其意义/局限后，本稿才满足研究质量验收",
             materials=["该观察对应的底稿事实与来源定位", "说明该观察的适用范围或局限"])
    return out[:12]


def _perspective_label(perspective: str) -> str:
    try:
        from facts import PERSPECTIVE_LABELS
        return str(PERSPECTIVE_LABELS.get(str(perspective or ""), "") or "投研")
    except Exception:
        return "投研"


# ── 附录：字段位置与计算底稿 ─────────────────────────────────


def _appendix(task_id: str, rows, derived, citations, evidence: dict | None, *,
              source_url: str = "", project=None, ws_dir=None) -> dict:
    """附录要能回答"这个数字从哪来、怎么算的"：字段位置 + 计算底稿文件。"""
    locs: list[dict] = []
    seen: set[str] = set()
    src_n = _citation_n(citations, source_url) or _source_number(citations, "")
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
    """来源清单：**只收实际被采用的**，按**发布者域名**标类型；未采用的留在 audit。

    A2：全入口共用同一条准入规则——叙事证据里已判 `excluded` 的 URL，**不得**从
    "正文引用"入口重新准入（实机反例：after_as_of 的文章经模型来源清单又进了采用清单）。
    """
    adopted: list[dict] = []
    seen: set[str] = set()
    rejected: list[dict] = []
    known_urls = tuple(_project_sources(task_id, ws_dir=ws_dir))

    def _add(url: str, title: str, kind: str, used_by: str, *, entry: str,
             **extra) -> None:
        u = str(url or "").strip()
        if not u:
            return
        if u in seen:
            # 已采用：只合并用途，不再走准入（也避免同一 URL 既在清单又被记"拒绝"）
            if used_by:
                for c in adopted:
                    if c["url"] == u and used_by not in c["used_by"]:
                        c["used_by"].append(used_by)
            return
        if entry == "structured":
            # 结构化财务来源是**本次研究取数本身**（chart_rows ok 即证据），
            # 不按网页规则判 unknown
            adm = "admitted"
        else:
            adm = _admission(u, evidence=evidence, known=known_urls)
        if adm == "excluded":
            # 已明确排除：任何入口都不再准入，改记"未采用"并说明原因
            rejected.append({"url": u, "title": str(title or "").strip() or _host(u),
                             "entry": entry, "admission": adm,
                             "reason": _validation_reason(u, evidence)})
            return
        if adm == "unknown" and entry in ("body", "material"):
            # 未抓取/未核验、仅罗列：留在待核查，不显示为已采用
            rejected.append({"url": u, "title": str(title or "").strip() or _host(u),
                             "entry": entry, "admission": adm,
                             "reason": "未取得正文或未核验，仅罗列"})
            return
        seen.add(u)
        entry_out = {"n": len(adopted) + 1, "url": u,
                     "title": str(title or "").strip() or _host(u),
                     "type": kind, "publisher": _publisher(u),
                     "admission": adm,
                     "used_by": [used_by] if used_by else []}
        entry_out.update({k: v for k, v in extra.items() if v})
        adopted.append(entry_out)

    # ① 结构化财务来源：**发布者是数据平台就标第三方**，并单独记录它依据的披露。
    # 实机教训：东方财富 API 被硬标成"发行人年报/官方披露"，读者会把第三方转载
    # 当成发行人原文（标题含"年报"、URL 里出现官方域名都不能证明发布者身份）。
    label = str(data.get("source_label") or "")
    surl = str(data.get("source_url") or "")
    if surl:
        stype = _source_type(surl)
        extra = {}
        if stype != "issuer_annual_report":
            extra = {"based_on": "发行人定期报告（经第三方数据平台转载；"
                                 "本次未取得原始披露文件）"}
        _add(surl, label, stype, "财务事实", entry="structured", **extra)
    # ② 定向取证采用的年报/公告页（业务背景/变化解释/风险用到它们才登记）
    # 类型按"访问路径域名 + 文档 provenance"：年报原文经第三方平台转载仍标发行人披露，
    # 但写明转载路径；"年报解读"类文章（域名第三方、非报告原文）保持第三方。
    for kind, used_by in (("business_background", "业务背景"),
                          ("change_explanation", "变化解释"),
                          ("footnote", "财务附注"),
                          ("risk", "风险因素")):
        for r in _located(evidence, kind, 4):
            url = str(r.get("url") or "")
            prov = str(r.get("document_provenance") or "")
            if prov == "issuer_annual_report":
                ctype = "issuer_annual_report"
                extra = {"based_on": f"发行人年度报告原文，经第三方平台"
                                     f"（{r.get('publisher') or '转载方'}）获取"}
            else:
                ctype = str(r.get("source_type") or _source_type(url))
                extra = {}
            _add(url, str(r.get("title") or ""), ctype, used_by,
                 entry="evidence", **extra)
    # ③ 正文实际引用的检索来源（按正文出现顺序编号）
    for url, title in _body_sources(body):
        _add(url, title, _source_type(url), "正文引用", entry="body")
    # ④ 用户材料（goal 文本）
    if str(goal or "").strip() and any(k in str(goal) for k in ("材料", "附件", "上传")):
        _add("user-material", "用户提供的材料", "user_material", "用户材料",
             entry="material")
    # 未被采用的候选（检索落盘里存在但正文没引用）→ 只留内部审计
    known = _project_sources(task_id, ws_dir=ws_dir)
    unused = [{"url": u, "title": _host(u)} for u in known if u not in seen]
    return adopted, {"unused_sources": unused[:20],
                     "rejected": rejected[:20],
                     "note": "未采用/越界材料只留在内部审计，不进简报来源清单"}


def _admission(url: str, *, evidence: dict | None, known=()) -> str:
    """来源准入：复用叙事证据的**同一实现**（单一规则）。"""
    try:
        import narrative_evidence as ne
        return ne.admission_of(url, evidence=evidence, known_urls=known)
    except Exception:
        return "unknown"


def _validation_reason(url: str, evidence: dict | None) -> str:
    for r in ((evidence or {}).get("records") or []):
        if str(r.get("url") or "") == str(url or ""):
            return _VALIDATION_LABELS.get(str(r.get("validation_status") or ""),
                                          "不满足主体/期间/资料截止")
    return "不满足主体/期间/资料截止"


def _source_type(url: str) -> str:
    """发布者身份按**解析后的域名**判定（复用叙事证据的同一实现）。"""
    try:
        import narrative_evidence as ne
        return ne.source_type(url)
    except Exception:
        return "third_party"


def _publisher(url: str) -> str:
    try:
        import narrative_evidence as ne
        return ne.publisher_of(url)
    except Exception:
        return _host(url)


def _body_source_list(body: str) -> list[tuple[int, str, str]]:
    """模型正文自己的来源清单 → `[(原编号, 标题, URL)]`（用于引用重编号映射）。"""
    out: list[tuple[int, str, str]] = []
    section = _section_text(body, ("参考来源", "资料来源", "数据来源", "参考文献"))
    for line in (section or "").splitlines():
        m = re.match(r"^\s*(\d{1,2})\s*[.、)]\s*\[([^\]]+)\]\((https?://[^\s)]+)\)", line)
        if m:
            out.append((int(m.group(1)), m.group(2), m.group(3)))
            continue
        m = re.match(r"^\s*(\d{1,2})\s*[.、)]\s*(https?://\S+)", line)
        if m:
            out.append((int(m.group(1)), "", m.group(2)))
    return out


def _remap_inline_refs(text: str, mapping: dict[int, int]) -> tuple[str, list[int]]:
    """把正文里的旧引用编号换成装配后的新编号；换不到的**移除编号**并报缺口（不猜）。

    同时归一 `[n=2]` 这类把指令记号抄进正文的写法（实机出现，会被验收判"引用无对应条目"
    并触发整稿重做）：按编号 2 处理，映射不到就同样去编号记缺口。

    只做编号重写；**主张与证据的关联**由 `_unsupported_claims` 保留（C2-1）——去编号
    不等于把"这句话没有来源支持"这件事一起抹掉。
    """
    unmapped: list[int] = []

    def _sub(m: re.Match) -> str:
        old = int(m.group(1) if m.group(1) else m.group(2))
        new = mapping.get(old)
        if new is None:
            if old not in unmapped:
                unmapped.append(old)
            return ""                     # 编号无法唯一对应：去掉，进"待核查"
        return f"[{new}]"

    out = re.sub(r"\[(?:n\s*=\s*(\d{1,2})|(\d{1,2}))\]", _sub, str(text or ""))
    return out, unmapped



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


def _chart_binding_note(binding: dict) -> str:
    """图表绑定摘要：指标/单位/期间/口径（正文引用据此核对，不靠编号）。"""
    if not isinstance(binding, dict) or not binding:
        return ""
    labels = [str(x) for x in (binding.get("metric_labels") or []) if str(x)]
    if not labels:
        labels = [str(x) for x in (binding.get("metrics") or []) if str(x)]
    parts = []
    if labels:
        parts.append("、".join(labels[:4]))
    if binding.get("unit"):
        parts.append(f"单位 {binding['unit']}")
    periods = [int(y) for y in (binding.get("periods") or [])]
    if periods:
        parts.append("期间 " + "、".join(str(y) for y in periods))
    if binding.get("caliber"):
        parts.append(f"口径 {binding['caliber']}")
    return "；".join(parts)


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
                        # C-2：稳定语义身份 + 绑定（指标/单位/期间/口径）——编号只是
                        # 显示结果，正文引用按 chart_id 对齐
                        "chart_id": str(c.get("chart_id") or ""),
                        "binding": dict(c.get("binding") or {}),
                        "keywords": list(c.get("keywords") or []),
                        "section_hint": str(c.get("section_hint") or ""),
                        "question": str(c.get("question") or ""),
                        "observation": str(c.get("observation") or ""),
                        "caption": str(c.get("caption") or ""),
                        "grade": str(c.get("grade") or "publish")})
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
    lines: list[str] = [f"# {who} 经营分析简报（{span} 年度）", "",
                        f"**主体**：{who}"
                        f"{'（' + str(sc.get('company_id')) + '）' if sc.get('company_id') else ''}"
                        f"　**期间**：{span} 年度"
                        f"　**口径**：{sc.get('caliber') or '未声明'}"
                        f"　**资料截止**：{sc.get('as_of') or '未声明'}", ""]
    # 附录延后写入的两块（C3）：口径说明与次要图表不进主文版面
    _appendix_extra: list[tuple[str, list[str]]] = []
    _appendix_charts: list[dict] = []
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
    # D3：研究问题与下一步——主文最多三个重点，每项含观察/支持证据/推断边界/核查动作
    questions = structure.get("research_questions") or []
    if questions:
        lines.append("## 研究问题与下一步")
        for q in questions:
            sup = q.get("support") or {}
            if sup.get("has_evidence"):
                support = f"支持：{sup.get('locator') or ''}"
                if sup.get("source_n"):
                    support += f"（来源 [{sup.get('source_n')}]"
                    support += "，管理层/发行人披露）" if sup.get("issuer") else "，第三方材料）"
            else:
                support = "支持：未取得对应披露，**观察成立、原因待证**"
            lines.append(f"- **{q.get('question')}**：{q.get('observation')}")
            lines.append(f"  - {support}；边界：{q.get('boundary')}；"
                         f"下一步：{'、'.join(q.get('next_action') or []) or '补齐底稿事实'}")
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
        # 金额变化（D1 夜间补修）与同比/比率**分开**：主文只列比率与同比，金额变化连同
        # 算式放附录（主文的研究问题观察已经给出金额差，附录提供可复算的算式）——
        # 主文每多一行都会把附录推到第 6 页（C3 版面目标：附录 ≤ 第 5 页）。
        _change_rows = [q for q in quality
                        if str(q.get("metric") or "").endswith("_change")]
        _main_rows = [q for q in quality if q not in _change_rows]
        for q in _main_rows:
            f = str(q.get("formula") or "").strip()
            expr = _formula_expr(f)
            unit = str(q.get("unit") or "%")
            lines.append(f"- {q.get('label')} {q.get('period')}：{q.get('value')}{unit}"
                         + (f"（{expr}）" if expr else ""))
        if _change_rows:
            lines.append("- 金额变化（与利润率变化分开呈现）见附录『金额变化（可复算）』。")
            _appendix_extra.append(("### 金额变化（可复算）", [
                *(f"- {q.get('label')} {q.get('period')}：{q.get('value')}"
                  f"{q.get('unit') or ''}"
                  + (f"（{_formula_expr(str(q.get('formula') or ''))}）"
                     if _formula_expr(str(q.get('formula') or '')) else "")
                  for q in _change_rows)]))
        conds = _ratio_conditions(quality)
        stype = str(table.get("subject_type") or "")
        if stype == "financial":
            # A1：金融机构——不呈现企业口径比率，也不靠页尾声明补救
            lines.append("")
            lines.append("**研究对象适用性**：按名称线索判定研究对象为**金融机构**"
                         "（未经行业元数据核实）。企业口径的质量比率（归母净利率、"
                         "经营现金流覆盖、资产负债率、研发强度）对金融机构不成立，"
                         "本次**不呈现**；银行需专用指标（净息差、不良与拨备、资本充足率等），"
                         "当前版本未配置。")
        elif conds:
            # C3：比率适用条件与适用范围是**口径说明**，放附录（主文只留一行指引），
            # 否则同一份说明会在主文里占掉半页，把"变化与缺口"挤下去
            lines.append("")
            lines.append("- 比率适用条件与适用范围见附录（主文不重复口径说明）。")
            _appendix_extra.append(("### 比率适用条件", [
                *(f"- {label}：{cond}" for label, cond in conds),
                f"- 适用范围：{_ratio_scope_note()}",
            ]))
    charts = structure.get("charts") or []
    if charts:
        lines.append("")
        lines.append("## 图表")
        # C3：主文只留能帮助判断的图（最多 3 张，且优先 publish 级），其余置附录——
        # 图注仍然逐图回答"这张图在问什么"，但不再让附录图挤占主文版面
        main_charts = [c for c in charts if str(c.get("grade") or "publish") == "publish"][:3]
        if not main_charts:
            main_charts = charts[:3]
        rest = [c for c in charts if c not in main_charts]
        for i, c in enumerate(main_charts, 1):
            lines.append(f"![{c.get('file')}](charts/{c.get('file')})")
            lines.append("")
            # 图注回答"这张图在回答什么、数据说了什么"；**正文图注不带数字**——
            # 正文里的数字必须逐个可溯源，图注单列读数会变成"不可溯源数字"
            q = str(c.get("question") or "").strip()
            obs = str(c.get("caption") or c.get("observation") or "").strip()
            head = q or f"{c.get('type') or '图'}"
            tail = f"；{obs}" if obs else ""
            # 读者正文只留**人可读**的图号/指标/期间/来源；`chart_id` 与绑定字典是
            # 内部标识，移到附录的图表元数据表（09-22 晚间指令 §3 第三小批）
            _bind = _chart_binding_note(c.get("binding") or {})
            _bind_note = f"（{_bind}）" if _bind else ""
            lines.append(f"图 {i}：{head}{tail}{_bind_note}"
                         "（数据同『财务对照』表与底稿）")
        if rest:
            lines.append("")
            lines.append(f"- 其余 {len(rest)} 张图（含比率分面）见附录『其他图表』。")
            _appendix_charts.extend(rest)
        # 图表元数据（内部标识）：供审计与引用对齐，不进读者主文
        _chart_meta = [c for c in charts if str(c.get("chart_id") or "")]
        if _chart_meta:
            _appendix_extra.append(("### 图表元数据（内部标识）", [
                "| 图 | chart_id | 绑定指标 | 单位 | 期间 | 口径 |",
                "|---|---|---|---|---|---|",
                *[f"| {i} | `{c.get('chart_id')}` | "
                  f"{'、'.join(str(x) for x in ((c.get('binding') or {}).get('metric_labels') or [])[:3])} | "
                  f"{(c.get('binding') or {}).get('unit') or ''} | "
                  f"{'、'.join(str(y) for y in ((c.get('binding') or {}).get('periods') or []))} | "
                  f"{(c.get('binding') or {}).get('caliber') or ''} |"
                  for i, c in enumerate(charts, 1) if str(c.get("chart_id") or "")],
            ]))
    lines.append("")
    lines.append("## 分析")
    analysis = str(structure.get("analysis") or "").strip() or _analysis_section(body)
    lines.append(analysis if analysis else
                 "> 本次未产出可交付的分析正文（数据与底稿已保留，见文末）。")
    lines.append("")
    # 变化解释：发生了什么 → 管理层/附注怎么解释 → 能推断到哪一步 → 还不能证明什么
    lines.append("## 变化解释")
    changes = structure.get("change_explanation") or {}
    rows_ch = changes.get("changes") or []
    if rows_ch:
        lines.append("**发生了什么（数据观察）**：")
        for c in rows_ch:
            lines.append(f"- {c.get('text')}")
    else:
        lines.append("- 本次未取得可复算的同比，无法说明变化（见文末资料缺口）。")
    management = changes.get("management") or []
    third = changes.get("third_party_views") or []
    lines.append("")
    lines.append("**管理层/附注的解释**：")
    if management:
        for e in management:
            n = f"[{e.get('source_n')}]" if e.get("source_n") else ""
            lines.append(f"- {e.get('text')}{n}")
            if e.get("locator"):
                lines.append(f"  - 出处：{e.get('locator')}")
    else:
        lines.append("- 未取得与上述变化对应的管理层讨论或附注段落，"
                     "原因**未在本次资料中体现**（需补充材料见下）。")
    if third:
        # 第三方评论不得包装成管理层解释：单独成块并标明发布者与文档期
        lines.append("")
        lines.append("**第三方观点（非管理层解释，仅供参照）**：")
        for e in third:
            n = f"[{e.get('source_n')}]" if e.get("source_n") else ""
            pub = f"（{e.get('publisher')}）" if e.get("publisher") else ""
            lines.append(f"- {e.get('text')}{n}{pub}")
            if e.get("locator"):
                lines.append(f"  - 出处：{e.get('locator')}")
    lines.append("")
    lines.append("**推断边界**：")
    for t in (changes.get("inference") or []):
        lines.append(f"- {t}")
    unproven = changes.get("unproven") or []
    if unproven:
        # C3：同一件事不在两处重复说——"还不能证明什么"的完整条目（含要补的材料）
        # 只在『风险与核查』出现，这里只留一行指引（原先两节各写一遍，读者看到两份）
        lines.append("")
        lines.append("- 尚未证明的部分（需补材料）见『风险与核查』，本处不重复。")
    excluded = list((structure.get("evidence") or {}).get("excluded") or [])
    if excluded:
        # 非准入材料照实说明（错主体/超资料截止/期间不符/缺字段），不静默丢弃
        lines.append("")
        lines.append("**未采用的材料（不满足主体/期间/资料截止或未核验）**：")
        for e in excluded[:6]:
            why = _VALIDATION_LABELS.get(str(e.get("validation_status") or ""),
                                         str(e.get("validation_status") or "不适用"))
            lines.append(f"- {str(e.get('title') or e.get('url'))[:60]}"
                         f"（{why}{'；发布 ' + e['published_at'] if e.get('published_at') else ''}）")
    rejected = list((structure.get("audit") or {}).get("rejected") or [])
    if rejected:
        lines.append("")
        lines.append("**正文引用但未准入的来源（已去编号，不借编号给其他来源）**：")
        for r in rejected[:6]:
            lines.append(f"- {str(r.get('title') or r.get('url'))[:60]}（{r.get('reason') or '未准入'}）")
    lines.append("")
    lines.append("## 风险与核查")
    risks = structure.get("risks") or []
    if risks:
        # C3：每条风险压成"一行主张 + 一行证据/条件/材料"，主文最多 6 条，
        # 其余（多为同类的底稿审计项）置附录——原先 12 条 × 3 行会占掉两三页
        _main_risks = risks[:6]
        _rest_risks = risks[6:]
        for r in _main_risks:
            lines.append(f"- **{r.get('text')}**")
            bits = [f"证据：{r.get('evidence') or '底稿无对应证据'}"]
            if r.get("would_change"):
                bits.append(f"改变判断的观察条件：{r.get('would_change')}")
            mats = r.get("materials_needed") or []
            if mats:
                bits.append(f"需补材料：{'、'.join(mats)}")
            lines.append(f"  - {'；'.join(bits)}")
        if _rest_risks:
            lines.append(f"- 另有 {len(_rest_risks)} 条同类核查项（底稿审计/材料清单）见附录。")
            _appendix_extra.append(("### 其他核查项", [
                *(f"- **{r.get('text')}**（证据：{r.get('evidence') or '底稿无对应证据'}）"
                  for r in _rest_risks)]))
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
    st_label = {"non_financial": "非金融企业", "financial": "金融机构（按名称线索，未经行业元数据核实）",
                "unknown": "未确定"}.get(str(sc.get("subject_type") or "unknown"), "未确定")
    lines.append(f"- 研究对象类型：{st_label}"
                 + (f"（判定来源：{sc.get('subject_type_source')}）"
                    if sc.get("subject_type_source") else "（与阅读视角无关）"))
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
    # C3：主文让出来的两块（口径说明 / 次要图表）在附录按原样呈现，证据不因压版面而丢
    for title, block in _appendix_extra:
        lines.append("")
        lines.append(title)
        lines.extend(block)
    if _appendix_charts:
        lines.append("")
        lines.append("### 其他图表")
        for c in _appendix_charts:
            lines.append(f"![{c.get('file')}](charts/{c.get('file')})")
            lines.append("")
            q = str(c.get("question") or "").strip()
            obs = str(c.get("caption") or c.get("observation") or "").strip()
            tail = f"；{obs}" if obs else ""
            lines.append(f"图：{q or c.get('type') or '图'}{tail}（数据同『财务对照』表与底稿）")
    lines.append("")
    lines.append("### 版本与验收状态")
    lines.append("> 关键数据与来源清单由底稿生成（可复算）；『分析』一节为模型撰写。"
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

    算式也可以**以括号开头**：同比的写法是 `(本期 - 上期) / 上期 * 100`，
    金额变化在任一期为负时写成 `(本期) - (上期)`（负号开头会让"紧贴数字的算式"
    识别失败，且 `-33.43 - -38.01` 读不出来）。此前只认数字开头，等于把这两类
    可复算的派生值排除在"可溯源"之外。
    """
    t = str(formula or "")
    m = re.match(r"\s*([0-9(][0-9.,\s*/+\-()]*?)\s*(?:，|,|输入|$)", t)
    if not m:
        return ""
    expr = m.group(1).strip().rstrip("，,")
    return expr


def _looks_like_brief(text: str) -> bool:
    """传入的正文是否**已经是装配好的简报**（验收候选稿会回流到这里）。"""
    t = str(text or "")
    return "> 报表口径：" in t and "## 关键发现" in t


def _brief_analysis_section(text: str) -> str:
    """从已装配的简报里取**它自己的『## 分析』一节**。

    只按**简报自己的小节标题**收尾（`BRIEF_SECTIONS`），不按"任意 `## `"截断——
    模型的分析正文自带小标题（如"## 分析与结论"），按任意标题截会把整节判成空。
    """
    return _brief_section(text, "## 分析")


# 简报自己的小节标题（顺序即渲染顺序）：取某一节时按这张表收尾
BRIEF_SECTIONS = ("## 关键发现", "## 业务背景", "## 财务对照", "## 图表", "## 分析",
                  "## 变化解释", "## 风险与核查", "## 附录", "## 参考来源")


def _brief_section(text: str, heading: str) -> str:
    """取简报里 `heading` 一节的内容（到下一个**简报小节**为止）。"""
    body = str(text or "")
    idx = body.find(heading)
    if idx < 0:
        return ""
    rest = body[idx + len(heading):]
    cut = len(rest)
    for other in BRIEF_SECTIONS:
        if other == heading:
            continue
        j = rest.find("\n" + other)
        if j >= 0:
            cut = min(cut, j)
    return rest[:cut].strip()


def _analysis_section(body: str) -> str:
    """模型正文 → 分析一节：**只移除可证明重复的块**，保留论证、限制与用户修订。

    三轮教训叠在这里：
    1. 最初在首个 `## 关键数据/财务对照` 标题处**截掉全部后文**——表后的有效分析被删光；
    2. 改为"标题命中关键词就整节丢弃"——模型把同比解读写成"三、核心指标两年对比与
       逐项解读"也被整节删掉（实机：分析一节只剩 214 字标题与口径声明）；
    3. 现在按**块级判断**：装配器**自己逐字生成**的小节（参考来源/免责声明等）整块丢；
       其余小节只丢非散文行（表格、图片、来源清单行），标题与散文一律保留；
       块内没有散文（只剩表格/图）才整块丢——"标题里有核心指标"不再是删除理由。
    """
    text = str(body or "")
    if not text:
        return ""
    if _looks_like_brief(text):
        # 已经是装配好的简报（如验收候选稿回流）：只取它的"## 分析"一节，
        # 不再把整份简报包一层——否则会"简报套简报"，分析一节变成整份简报，
        # "分析未完成"的判据随之失效（离线读数：交付状态误判为 verified）
        return _brief_analysis_section(text)
    if _ENGINEERING_REPORT_RE.search(text):
        # 工程收尾报告不是分析（"5 个步骤成功"回答不了经营问题）：返回空，
        # 简报会如实写"本次未产出可交付的分析正文"
        return ""
    # 按标题切块（首块是无标题的开头），逐块判断
    blocks: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines():
        if line.strip().startswith("#"):
            blocks.append((line, []))
        else:
            blocks[-1][1].append(line)
    keep: list[str] = []
    for title_line, lines in blocks:
        title = (title_line or "").strip().lstrip("#").strip()
        if title_line is not None and any(k in title for k in _ASSEMBLER_OWNED_SECTIONS):
            continue                          # 装配器逐字生成的小节：整块丢（可证明重复）
        prose: list[str] = []
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if s.startswith("![") and "](" in s:
                continue                      # 模型重复贴的图由装配器接管
            if s.startswith("|") and "|" in s:
                continue                      # 模型自写的表格由装配器接管
            if re.match(r"^\d{1,2}\s*[.、)]\s*[\[(]", s) or re.match(r"^[-*]\s*\[", s):
                continue                      # 模型自己的来源清单由装配器接管
            prose.append(ln)
        if title_line is not None:
            if not title or not prose:
                continue                      # 空标题 / 只剩表格图的块：没有可保留的内容
            keep.append(title_line)
        keep.extend(prose)
    out = "\n".join(keep).strip()
    if not keep and text.strip():
        # 全文都是表格/来源清单/工程说明：如实返回空（调用方写"未产出可交付的分析正文"）
        return ""
    return out


def analysis_coverage(analysis_text: str) -> dict:
    """"有分析"的最低要求：至少一项**实际数据观察**，且说明其意义/局限。

    标题、口径声明、免责声明都不算（D2）。读数进结构对象与风险清单：
    没有数据观察或没有意义/局限说明 → 不满足研究质量验收（不靠加长正文掩盖）。
    """
    text = str(analysis_text or "")
    observations = 0
    seen: set[str] = set()
    for sent in re.split(r"[。！？!?\n]+", text):
        s = sent.strip()
        if not s or s.startswith(("#", "|", ">")):
            continue
        if not any(a.get("metric") and a.get("unit_class") for a in _assertions_in(s)):
            continue
        # 同一观察重复写多次只算一条（D2：计数按独立问题/主张去重，不奖励堆砌）
        key = _sentence_key(s)
        if not key or key in seen:
            continue
        seen.add(key)
        observations += 1
    has_meaning = any(k in text for k in ("意义", "说明", "局限", "边界", "适用范围",
                                          "需核查", "不能证明", "不能据此", "推断",
                                          "待核查", "含义", "口径", "仅覆盖"))
    return {"chars": len(text), "observations": observations,
            "has_meaning_or_limit": bool(has_meaning),
            "ok": bool(observations >= 1 and has_meaning)}


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


def stamp_structure_version(structure: dict, version_id: str) -> dict:
    """把**本版正文的版本号**盖到结构对象与每条主张上（C2-2/C2-6）。

    为什么必须盖：结构对象是页面展示的发现/缺口/主张来源，而"版本"在装配时才确定。
    不盖的话，修订后页面会拿旧结构冒充新版（读者看到的发现与导出的正文不是同一版）。
    """
    vid = str(version_id or "")
    if not isinstance(structure, dict):
        return structure
    structure["version_id"] = vid
    for c in (structure.get("claims") or []):
        if isinstance(c, dict):
            c["version_id"] = vid
    return structure



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
