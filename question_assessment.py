# -*- coding: utf-8 -*-
"""逐问题评估：**一次评估**，问题区/风险区/研究状态/候选比较共用同一结果。

根因（09-23 晚间复核）：同一研究判断此前有多个独立计算者——`_research_questions`、
`_change_explanation`/`_risks`、`delivery_pipeline.research_state` 各自按关键词重判，
于是同一份正文里"利润原因待证"与"解释已取得"并存。

本模块只做一件事：给定某指标的候选披露片段，判定它**是哪种材料**，并保留判定理由与
证据区间。判定严禁凭单字"因/受/系"升级——必须命中因果短语，且排除否定、未来/假设、
不确定与明确"未披露"的表述。分类结果只有下面这些，展示层不得自行改写：

- `decomposition` 分解覆盖充分（定量把问题要求闭合）
- `management_cause` 管理层明确归因（**发行人说法**，不是独立证实）
- `observation` 观察可验证（只有读数）
- `background` 背景相关（行业/市场语境）
- `negation` 明确否认某原因（"并非由…导致"）
- `not_disclosed` 明确"未披露/未说明原因"
- `tentative` 不确定表述（可能/或许/有待）
- `hypothesis` 未来或假设（若/将/预计）
- `none` 没有相关材料
"""
from __future__ import annotations

import re

KIND_DECOMPOSITION = "decomposition"
KIND_RELATION = "relation"
KIND_MANAGEMENT = "management_cause"
KIND_OBSERVATION = "observation"
KIND_BACKGROUND = "background"
KIND_NEGATION = "negation"
KIND_NOT_DISCLOSED = "not_disclosed"
KIND_TENTATIVE = "tentative"
KIND_HYPOTHESIS = "hypothesis"
KIND_NONE = "none"

KIND_LABELS = {
    KIND_DECOMPOSITION: "分解覆盖充分",
    KIND_RELATION: "两期事实的关系可复算（计算自披露数值）",
    KIND_MANAGEMENT: "管理层明确归因（发行人说法）",
    KIND_OBSERVATION: "观察可验证（仅读数）",
    KIND_BACKGROUND: "背景相关（行业/市场语境）",
    KIND_NEGATION: "明确否认某原因",
    KIND_NOT_DISCLOSED: "明确未披露原因",
    KIND_TENTATIVE: "不确定表述",
    KIND_HYPOTHESIS: "未来/假设表述",
    KIND_NONE: "无相关材料",
}

# ── 问题类型与判据表（C2）────────────────────────────────────────
#
# 完成条件由**问题真正询问的内容**决定，不由材料的自称覆盖决定。四类，每类的
# "需要什么/什么算完整/什么算部分/什么算没有"都写在这张表里，装配与页面读同一张表：
#
# - `numeric_change`      数值变化：可复算、可定位的同口径两期事实
# - `issuer_explanation`  发行人如何解释：原披露逐项对应，明确标为发行人说法
# - `quant_decomposition` 量化贡献/独立因果：问题要求的**每个组成部分**都有量化披露
# - `relation`            两个事实之间的关系：两条序列同口径且关系可复算
#
# 三条纪律：
# 1. `full` 由**已绑定、已准入、有定位**的证据对需求的覆盖算出（`components` 逐项状态），
#    材料载荷自报的 `coverage="full"` 不作数（见 `_coverage_by_rule`）。
# 2. 定性归因（管理层说法）**不能**冒充量化分解：它最多把覆盖抬到 partial。
# 3. 资料没披露时因果问题仍未完成（`none`），不得用背景/读数补齐。
QTYPE_NUMERIC_CHANGE = "numeric_change"
QTYPE_ISSUER_EXPLANATION = "issuer_explanation"
QTYPE_QUANT_DECOMPOSITION = "quant_decomposition"
QTYPE_RELATION = "relation"
QTYPE_NONE = "none"
QTYPE_LABELS = {
    QTYPE_NUMERIC_CHANGE: "数值变化",
    QTYPE_ISSUER_EXPLANATION: "发行人如何解释",
    QTYPE_QUANT_DECOMPOSITION: "量化贡献/分解",
    QTYPE_RELATION: "两个事实之间的关系",
    QTYPE_NONE: "未分类",
}
QUESTION_RULES: dict[str, dict] = {
    QTYPE_NUMERIC_CHANGE: {
        "need": "该指标两期同口径、可定位的事实（同一主体/报表口径/期间）",
        "nature": "计算自披露数值（非独立核实）",
        "full": "两期事实都已绑定且同口径，变化量与方向可复算",
        "partial": "只有一期事实，或口径/期间不一致",
        "none": "没有该指标的可用事实",
    },
    QTYPE_ISSUER_EXPLANATION: {
        "need": "发行人原披露里对**问题所列各项**的逐项说明（明确带因果表述）",
        "nature": "发行人说法，未独立验证",
        "full": "发行人说法逐项覆盖问题所列各项，且带定位",
        "partial": "只有笼统归因，或只覆盖部分项",
        "none": "原披露没有对该项的说明（含明确写未披露/否认）",
    },
    QTYPE_QUANT_DECOMPOSITION: {
        "need": "问题要求的**每个组成部分**都有发行人披露的量化项，且可核对闭合",
        "nature": "发行人口径的量化分解（未独立验证）",
        "full": "所需组成部分全部取得并逐项带定位",
        "partial": "只取得部分组成部分（缺口要写清是哪一项）",
        "none": "没有任何组成部分的量化披露",
    },
    QTYPE_RELATION: {
        "need": "待比较指标各自的同口径两期事实，且关系可复算",
        "nature": "计算自披露数值（非独立核实）",
        "full": "两条序列都已绑定且同口径，关系可复算",
        "partial": "缺其中一条序列，或口径不一致",
        "none": "没有可比较的可用事实",
    },
    QTYPE_NONE: {
        "need": "未分类的问题不判完成", "nature": "",
        "full": "—", "partial": "—", "none": "未分类即未完成",
    },
}

# 固定三问的类型与所需组成部分（问题真正问什么 → 完成条件）。
# 收入、利润问的是**组成部分的量化**；现金问的是**两条已绑定事实之间的关系**。
QUESTION_TYPE_BY_METRIC = {
    "revenue": QTYPE_QUANT_DECOMPOSITION,
    "net_profit": QTYPE_QUANT_DECOMPOSITION,
    "operating_cashflow": QTYPE_RELATION,
}
QUESTION_REQUIREMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    # (组成部分名, 该部分要求什么)
    "revenue": (("量", "销量/产量等实物量披露"), ("结构", "收入构成按组披露且与表内合计闭合"),
                ("价", "发行人披露的价格口径（推算吨价不算）")),
    "net_profit": (("毛利端", "营业收入、营业成本（或毛利率）可算出毛利额变化"),
                   ("毛利线以下", "期间费用/税项/非经常性损益/少数股东损益的明细")),
    "operating_cashflow": (("经营现金流净额（两期）", "两期同口径、可定位"),
                           ("归母净利润（两期）", "两期同口径、可定位")),
}


def question_type_of(metric: str) -> str:
    return QUESTION_TYPE_BY_METRIC.get(str(metric), QTYPE_NONE)


def question_set(metrics=(), *, perspective: str = "", subject_type: str = "") -> list[dict]:
    """任务创建时固定的问题集（**最多三问**，版本化保存）。

    问题的题目与边界来自报告侧的固定定义（`report_brief._RESEARCH_QUESTIONS`），
    本函数只补上"类型 + 所需证据"这两件装配与页面都要读的东西。
    阅读视角（equity / bank_corporate）与研究对象的**主体类型**分别记录：
    银行阅读视角**不改变**研究对象类型，因此不改变问题类型与要求。
    """
    import report_brief as _rb
    wanted = [str(m) for m in (metrics or ())] or [m for m, _q, _b in _rb._RESEARCH_QUESTIONS]
    out: list[dict] = []
    for metric, question, _boundary in _rb._RESEARCH_QUESTIONS:
        if metric not in wanted:
            continue
        qtype = question_type_of(metric)
        reqs = QUESTION_REQUIREMENTS.get(metric, ())
        out.append({
            "metric": metric,
            "question": question,
            "question_type": qtype,
            "question_type_label": QTYPE_LABELS.get(qtype, qtype),
            "rule": dict(QUESTION_RULES.get(qtype) or {}),
            "requires": [{"component": name, "what": what} for name, what in reqs],
            "perspective": str(perspective or ""),
            "subject_type": str(subject_type or ""),
        })
    return out[:3]


def _coverage_by_rule(components) -> tuple[str, str]:
    """按组成部分的**绑定状态**算覆盖 → `(coverage, 说明)`。

    每个组成部分的 `state` 只认两值：`bound`（已绑定、已准入、有定位）/ `missing`。
    **材料载荷自报的 coverage 不参与**——否则"自报 full"就能把问题判成完成。
    """
    items = [c for c in (components or []) if isinstance(c, dict)]
    if not items:
        return "none", "没有组成部分证据"
    bound = [c for c in items if str(c.get("state")) == "bound"]
    missing = [str(c.get("component") or "?") for c in items
               if str(c.get("state")) != "bound"]
    if not bound:
        return "none", "组成部分均未取得：" + "、".join(missing)
    if not missing:
        return "full", f"{len(bound)} 个组成部分全部取得"
    return "partial", "已取得 " + "、".join(str(c.get("component") or "?") for c in bound) \
        + "；缺 " + "、".join(missing)


# 只有**因果短语/构式**才算归因：单字"因/受/系"不升级（复核实测漏判/误判的主因）。
# 构式（因X而Y / 受X影响 / 系X所致）是完整的因果表达，按模式匹配，不按单字。
_CAUSAL_PHRASES = ("主要由于", "主要是由于", "是由于", "由于", "因为", "所致", "导致",
                   "使得", "带动", "拖累", "源于", "来源于", "受益于", "系因")
_CAUSAL_RES = (
    re.compile(r"因.{1,24}(而|致)"),
    re.compile(r"受.{0,16}(影响|拖累|带动|提振)"),
    re.compile(r"系.{1,24}(所致|导致|造成)"),
)

# 否定：明确否认某原因（含"不因/未因/并非由…导致"）
_NEGATION_RES = (
    re.compile(r"(并非|并不是|不是|非|不)\s*(由于|因|因为|由)"),
    re.compile(r"(并不|不|未)\s*(是)?\s*(由|因|因为)?.{0,16}(导致|所致|造成)"),
    re.compile(r"(未|没有|不曾|不)\s*受.{0,16}(影响|拖累)"),
    re.compile(r"(原因)?\s*(尚|仍)?\s*(未|没有)\s*(披露|说明|解释|提及|给出)"),
    re.compile(r"(尚不|无法|难以)\s*(清楚|判断|确定|说明)"),
)
# 不确定：可能/或许/有待核实
_TENTATIVE_RES = (
    re.compile(r"(可能|或许|也许|大约|不排除|或与|也许还|有待|尚待|待核实|待确认)"),
)
# 未来/假设：若/如果/将/预计/未来
_HYPOTHESIS_RES = (
    re.compile(r"(若|如果|假如|假设|一旦|未来|将来|预计|有望|将会|或将|将继续)"),
    re.compile(r"将(下降|上升|增长|减少|增加|下滑|降低|提高|回落|承压|维持|保持)"),
)
# 背景：行业/市场语境
_BACKGROUND_MARKERS = ("行业", "市场", "竞争", "环境", "需求", "政策", "消费", "价位段",
                       "价格带", "景气", "宏观", "周期", "存量竞争")
# 定量分解线索（把问题要求闭合的说法）
_QUANT_MARKERS = ("同比", "占", "比重", "结构", "销量", "销售量", "吨", "单价", "吨价",
                  "分产品", "分地区", "分销售模式", "量价", "拆", "增减", "%", "％")
_CLAUSE_SPLIT_RE = re.compile(r"[。；;\n]")
_SUB_SPLIT_RE = re.compile(r"[，,、]")
_CHANGE_WORDS = ("下降", "上升", "增长", "减少", "增加", "下滑", "回落", "持平", "变化",
                 "同比", "降低", "提高")
_YEAR_RE = re.compile(r"(20\d{2})\s*年")


def clauses(text: str) -> list[str]:
    """句级切分（逗号保留在句内：真实因果常被逗号切开）。"""
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(str(text or "")) if c.strip()]


def _merged_spans(clause: str) -> list[tuple[str, tuple[str, ...]]]:
    """把"指标变化子句 + 逗号/相邻因果子句"合并成一个判定窗口（带子句组成）。

    反例（复核实测）："2024年净利润下降，主要由于原材料涨价。" 因果短语在逗号之后，
    只按逗号切会判成"仅读数"。带上子句组成后，因果只算在**它所在子句**的头上——
    "营业收入因销量下降而下降，净利润66.73亿元"里的因果不得给净利润当归因。
    """
    subs = [s.strip() for s in _SUB_SPLIT_RE.split(clause) if s.strip()]
    out: list[tuple[str, tuple[str, ...]]] = []
    for i, s in enumerate(subs):
        out.append((s, (s,)))
        if i + 1 < len(subs):
            out.append((s + "，" + subs[i + 1], (s, subs[i + 1])))
    return out or [(clause, (clause,))]


def _causal_in(clause: str) -> str:
    """子句内的因果表达（短语或构式）；单字"因/受/系"不算。"""
    for p in _CAUSAL_PHRASES:
        if p in clause:
            return f"「{p}」"
    for rx in _CAUSAL_RES:
        m = rx.search(clause)
        if m:
            return f"构式「{m.group(0)[:16]}」"
    return ""


_CAUSAL_LEADS = ("主要由于", "主要是由于", "由于", "是因为", "因为", "系", "受")


def _kind_of_span(span: str, metric_words: tuple[str, ...],
                  period=None, *, parts: tuple[str, ...] | None = None,
                  period_ok: bool = True) -> tuple[str, str]:
    """单个判定窗口 → (kind, reason)。只判这一件事，不做"有没有别的窗口"的兜底。

    `parts`：该窗口由哪些子句拼成（"指标子句" + "因果子句"）。因果**只能算在它所在的
    那个子句头上**——"营业收入因销量下降而下降，净利润66.73亿元"里，因果属于收入，
    不得给净利润当归因（复核实机：利润读数被同段收入因果误算支持）。
    """
    if not any(w in span for w in metric_words):
        return KIND_NONE, "窗口内没有该指标词"
    parts = tuple(parts or (span,))
    # 因果子句：含因果表达，且该子句自己带该指标词（或它是紧随指标子句的因果从句）
    _causal_owner: tuple[int, str] | None = None
    for i, p in enumerate(parts):
        c = _causal_in(p)
        if not c:
            continue
        same_metric = any(w in p for w in metric_words)
        follows_metric = (i > 0 and any(w in parts[i - 1] for w in metric_words)
                          and p.strip().startswith(_CAUSAL_LEADS))
        if same_metric or follows_metric:
            _causal_owner = (i, c)
            break
    if _causal_owner is None:
        for rx in _NEGATION_RES:
            if rx.search(span):
                _k = KIND_NOT_DISCLOSED if "披露" in rx.pattern else KIND_NEGATION
                return _k, f"命中否定/未披露表述：{rx.pattern[:24]}"
        for rx in _HYPOTHESIS_RES:
            if rx.search(span):
                return KIND_HYPOTHESIS, f"命中未来/假设表述：{rx.pattern[:24]}"
        for rx in _TENTATIVE_RES:
            if rx.search(span):
                return KIND_TENTATIVE, f"命中不确定表述：{rx.pattern[:24]}"
        if period is not None:
            years = {int(y) for y in _YEAR_RE.findall(span)}
            if years and int(period) not in years:
                return KIND_NONE, f"期间不符（该句写的是 {'、'.join(str(y) for y in sorted(years))} 年）"
        if any(k in span for k in _BACKGROUND_MARKERS):
            return KIND_BACKGROUND, "该指标子句邻近行业/市场语境词（不构成原因）"
        if any(w in span for w in _CHANGE_WORDS) or any(w in span for w in metric_words):
            return KIND_OBSERVATION, "只有读数/变化，没有因果表述"
        return KIND_NONE, "窗口内没有该指标的变化或读数"
    # 有因果归属：否定/未来/不确定/期间仍优先（"并非由…导致"不是归因）
    _owner_text = parts[_causal_owner[0]]
    for rx in _NEGATION_RES:
        if rx.search(span):
            _k = KIND_NOT_DISCLOSED if "披露" in rx.pattern else KIND_NEGATION
            return _k, f"命中否定/未披露表述：{rx.pattern[:24]}"
    for rx in _HYPOTHESIS_RES:
        if rx.search(_owner_text):
            return KIND_HYPOTHESIS, f"因果句本身是未来/假设：{rx.pattern[:24]}"
    for rx in _TENTATIVE_RES:
        if rx.search(_owner_text):
            return KIND_TENTATIVE, f"因果句本身不确定：{rx.pattern[:24]}"
    if period is not None and not period_ok:
        # 期间按**整个判定窗口**校验（不只因果子句）：实机反例是"2022 年净利润下降，
        # 主要由于毛利率下降"这类句子——年份在指标子句、因果在随后子句，只看因果子句
        # 会把它当成当期归因。注意**只否决归因**：否定/未来/不确定的分类照旧
        # （"预计2025年净利润将继续承压"仍要如实记成未来表述，不是"没有材料"）。
        years = {int(y) for y in _YEAR_RE.findall(span)}
        return KIND_NONE, (f"期间不符（该句写的是 "
                           f"{'、'.join(str(y) for y in sorted(years))} 年）"
                           if years else "期间不符")
    return KIND_MANAGEMENT, f"命中因果表达{_causal_owner[1]}且无否定/未来/不确定表述"


def assess_text(text: str, metric: str, *, period=None,
                metric_words: tuple[str, ...] = ()) -> dict:
    """一段文本对该指标的判定（保留命中窗口、理由与极性/时态/确定性/flags）。

    多个窗口各判一次：`kind` 取最强，`flags` 逐类如实置位（同一段可以既是背景又含读数）。
    否定/未披露/不确定/未来类**不因存在更强的窗口而被抹掉**——它们本身就是结论。
    """
    words = tuple(metric_words) or (metric,)
    flags = {k: False for k in (KIND_DECOMPOSITION, KIND_MANAGEMENT, KIND_OBSERVATION,
                                KIND_BACKGROUND, KIND_NEGATION, KIND_NOT_DISCLOSED,
                                KIND_TENTATIVE, KIND_HYPOTHESIS)}
    best = {"kind": KIND_NONE, "span": "", "reason": "无该指标的窗口",
            "polarity": "unknown", "tense": "current", "certainty": "high"}
    for cl in clauses(text):
        # 期间是**子句级**属性：子句里出现年份、且都不是请求期间时，该子句的**归因**
        # 不作数（否定/未来/不确定的分类照旧）。
        _years = {int(y) for y in _YEAR_RE.findall(cl)}
        _period_ok = not (period is not None and _years and int(period) not in _years)
        for span, parts in _merged_spans(cl):
            kind, reason = _kind_of_span(span, words, period, parts=parts,
                                         period_ok=_period_ok)
            if kind == KIND_NONE:
                continue
            flags[kind] = True
            if _rank(kind) > _rank(best["kind"]):
                tense = ("future" if kind == KIND_HYPOTHESIS else
                         "past" if period is not None and _YEAR_RE.search(span)
                         and int(period) not in {int(y) for y in _YEAR_RE.findall(span)}
                         else "current")
                best = {"kind": kind, "span": span[:200], "reason": reason,
                        "polarity": "negate" if kind in (KIND_NEGATION, KIND_NOT_DISCLOSED)
                        else "affirm",
                        "tense": tense,
                        "certainty": "low" if kind == KIND_TENTATIVE else "high"}
    best["flags"] = flags
    return best


def assess_background(text: str) -> dict:
    """业务背景片段判定：有行业/市场语境即可算"背景相关"（不要求它提到该指标词）。

    背景是"读者需要的语境"，不是"该指标的原因"；它不参与回答完成度，也不因此
    获得任何原因地位（复核实机：收入段的行业语境曾被算进必答已支持）。
    """
    t = str(text or "")
    if any(k in t for k in _BACKGROUND_MARKERS):
        return {"kind": KIND_BACKGROUND, "span": t[:200], "reason": "行业/市场语境",
                "polarity": "affirm", "tense": "current", "certainty": "high",
                "flags": {KIND_BACKGROUND: True}}
    return {"kind": KIND_NONE, "span": "", "reason": "没有行业/市场语境词",
            "polarity": "unknown", "tense": "current", "certainty": "high",
            "flags": {}}


def assess_question(metric: str, *, items=None, background_items=None,
                    decomposition=None, period=None,
                    has_observations: bool = False, question_type: str = "",
                    components=(), issuer_items=None) -> dict:
    """逐问题评估：按**问题类型**的判据算覆盖（材料性质仍逐类保留）。

    `items`：候选披露（每条含 `text`/`locator`/`source_n`/`issuer`/`document_period`）。
    `background_items`：业务背景类片段（可选）。
    `components`：问题要求的组成部分及其**绑定状态**（`[{component, state, locator, note}]`）。
    覆盖由 `_coverage_by_rule` 按 `QUESTION_RULES[question_type]` 算出——材料载荷自报的
    `decomposition.coverage` **不作数**（专项 C2：不许透传自报 full）。
    `decomposition`：定量分解的展示信息（note/locator），不再决定覆盖。
    返回的对象是**唯一权威**：问题区、风险区、研究状态、候选比较都读它。
    """
    from report_brief import _EXPLANATION_METRIC_WORDS  # 延迟导入，避免循环依赖
    words = _EXPLANATION_METRIC_WORDS.get(metric) or (metric,)
    qtype = str(question_type or question_type_of(metric))
    best = None
    flags = {k: False for k in (KIND_MANAGEMENT, KIND_OBSERVATION, KIND_BACKGROUND,
                                KIND_NEGATION, KIND_NOT_DISCLOSED, KIND_TENTATIVE,
                                KIND_HYPOTHESIS)}
    for it in (items or []):
        text = str(it.get("text") or "")
        res = assess_text(text, metric,
                          period=period if it.get("document_period") else None,
                          metric_words=words)
        for k in flags:
            flags[k] = flags[k] or bool((res.get("flags") or {}).get(k))
        if res["kind"] == KIND_NONE:
            continue
        cand = dict(res)
        cand.update({"locator": str(it.get("locator") or ""),
                     "source_n": str(it.get("source_n") or ""),
                     "issuer": bool(it.get("issuer")),
                     "text": text[:200]})
        if best is None or _rank(cand["kind"]) > _rank(best["kind"]):
            best = cand
    for it in (background_items or []):
        _bt = str(it.get("text") or "")
        # 背景只挂在**它确实谈到的**问题上：纯行业语境（不含该指标词）留在业务背景区，
        # 不当成某个问题的"已取材料"（否则利润/现金问题会借行业段拿到背景标签）。
        if not any(w in _bt for w in words):
            continue
        res = assess_background(_bt)
        flags[KIND_BACKGROUND] = flags[KIND_BACKGROUND] or bool(res["flags"].get(KIND_BACKGROUND))
        if res["kind"] != KIND_BACKGROUND:
            continue
        cand = dict(res, kind=KIND_BACKGROUND, reason=res["reason"])
        cand.update({"locator": str(it.get("locator") or ""),
                     "source_n": str(it.get("source_n") or ""), "issuer": True,
                     "text": str(it.get("text") or "")[:200]})
        if best is None or _rank(cand["kind"]) > _rank(best["kind"]):
            best = cand
    # 分解的展示信息：只在**组成部分确实绑定了**（按判据表 full 或 partial）时才算材料，
    # 自报的 coverage 文本不参与判定。
    dec = dict(decomposition or {})
    comps = [c for c in (components or []) if isinstance(c, dict)]
    dec_coverage, dec_note = _coverage_by_rule(comps)
    if comps and dec_coverage != "none":
        # 材料类别按**性质**记：量化分解类记 decomposition，关系类（两条序列）记 relation；
        # 覆盖程度由 coverage 与 flags 如实区分（部分分解不等于"覆盖充分"）。
        cand = {"kind": KIND_RELATION if qtype == QTYPE_RELATION else KIND_DECOMPOSITION,
                "span": str(dec.get("note") or dec_note)[:200],
                "reason": str(dec.get("reason") or f"按判据表：{dec_note}"),
                "polarity": "affirm", "tense": "current", "certainty": "high",
                "locator": str(dec.get("locator") or ""), "source_n": "",
                "issuer": True, "text": str(dec.get("note") or dec_note)[:200]}
        if best is None or _rank(cand["kind"]) > _rank(best["kind"]):
            best = cand
    out = best or {"kind": KIND_NONE, "span": "", "reason": "未取得该指标相关材料",
                   "polarity": "unknown", "tense": "current", "certainty": "high",
                   "locator": "", "source_n": "", "issuer": False, "text": ""}
    kind = out["kind"]
    # 覆盖三档由**判据表**给出；定性归因（管理层说法）最多把覆盖抬到 partial——
    # 它不能冒充量化分解（专项 C2），发行人解释类问题另有自己的判据。
    coverage = dec_coverage
    if qtype == QTYPE_ISSUER_EXPLANATION:
        coverage = _issuer_coverage(issuer_items, items, period=period, words=words)
    elif coverage == "none" and kind == KIND_MANAGEMENT:
        coverage = "partial"
    out.update({
        "metric": metric,
        "question_type": qtype,
        "question_type_label": QTYPE_LABELS.get(qtype, qtype),
        "rule": dict(QUESTION_RULES.get(qtype) or {}),
        "components": comps,
        "coverage_reason": dec_note if comps else "",
        "kind_label": KIND_LABELS.get(kind, kind),
        "flags": dict(flags, **{KIND_DECOMPOSITION: (qtype == QTYPE_QUANT_DECOMPOSITION
                                                     and coverage == "full")}),
        "has_material": kind != KIND_NONE or coverage != "none",
        "has_observation": bool(flags[KIND_OBSERVATION]) or has_observations
        or coverage != "none",
        "has_background": bool(flags[KIND_BACKGROUND]),
        "has_management_cause": bool(flags[KIND_MANAGEMENT]),
        # "有分解材料"只对**量化分解类**问题为真：关系类问题（现金覆盖）拿到的
        # 是两条序列，不是分解——不让它借 has_decomposition 冒充分解覆盖。
        "has_decomposition": (qtype == QTYPE_QUANT_DECOMPOSITION and coverage != "none"
                              and bool(comps)),
        "has_negation": bool(flags[KIND_NEGATION] or flags[KIND_NOT_DISCLOSED]),
        "coverage": coverage,
        # 回答完成 = 判据表 full；发行人说法与观察读数都不单独提升为完成
        "answered": coverage == "full",
        "decomposition": dec,
        "material_note": str(dec.get("note") or "") if coverage != "none" else "",
    })
    return out


def _issuer_coverage(issuer_items, items, *, period, words) -> str:
    """发行人解释类问题：原披露逐项对应才算 full，笼统归因只能 partial。

    `issuer_items`：该问要求的项（字符串列表）；`items`：候选披露片段。
    逐项都要有一条**带因果表述的发行人句子**覆盖，否则按缺项降到 partial。
    判定仍走 `assess_text`（否定/未来/不确定/错期间都在那里被排除）。
    """
    want = [str(x) for x in (issuer_items or ())]
    if not want:
        return "none"
    pool = list(items or [])
    hit: set[str] = set()
    vague = False
    for it in pool:
        text = str(it.get("text") or "")
        if not any(w in text for w in words):
            continue
        res = assess_text(text, "issuer", period=period if it.get("document_period") else None,
                          metric_words=words)
        if res.get("kind") != KIND_MANAGEMENT:
            continue
        _hit_here = [name for name in want if name and name in text]
        if _hit_here:
            hit.update(_hit_here)
        else:
            vague = True                            # 有归因，但没提到任何所列项
    if hit and len(hit) == len(want):
        return "full"
    # 笼统归因（或只覆盖部分项）：是发行人说法，但没逐项对应 → 只能 partial
    return "partial" if (hit or vague) else "none"



def _rank(kind: str) -> int:
    return {KIND_DECOMPOSITION: 6, KIND_RELATION: 6, KIND_MANAGEMENT: 5,
            KIND_BACKGROUND: 4, KIND_OBSERVATION: 3, KIND_NEGATION: 2,
            KIND_NOT_DISCLOSED: 2, KIND_TENTATIVE: 1, KIND_HYPOTHESIS: 1,
            KIND_NONE: 0}.get(kind, 0)
