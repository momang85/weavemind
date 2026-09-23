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
    KIND_MANAGEMENT: "管理层明确归因（发行人说法）",
    KIND_OBSERVATION: "观察可验证（仅读数）",
    KIND_BACKGROUND: "背景相关（行业/市场语境）",
    KIND_NEGATION: "明确否认某原因",
    KIND_NOT_DISCLOSED: "明确未披露原因",
    KIND_TENTATIVE: "不确定表述",
    KIND_HYPOTHESIS: "未来/假设表述",
    KIND_NONE: "无相关材料",
}

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
                  period=None, *, parts: tuple[str, ...] | None = None) -> tuple[str, str]:
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
    if period is not None:
        years = {int(y) for y in _YEAR_RE.findall(_owner_text)}
        if years and int(period) not in years:
            return KIND_NONE, f"因果句写的是 {'、'.join(str(y) for y in sorted(years))} 年"
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
        for span, parts in _merged_spans(cl):
            kind, reason = _kind_of_span(span, words, period, parts=parts)
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
                    has_observations: bool = False) -> dict:
    """逐问题评估：材料已取得 / 观察可验证 / 背景相关 / 管理层明确归因 / 分解覆盖。

    `items`：候选披露（每条含 `text`/`locator`/`source_n`/`issuer`/`document_period`）。
    `background_items`：业务背景类片段（可选）。
    `decomposition`：定量分解结果（如量价/结构事实），`{ok, coverage, note, locator}`。
    返回的对象是**唯一权威**：问题区、风险区、研究状态、候选比较都读它。
    """
    from report_brief import _EXPLANATION_METRIC_WORDS  # 延迟导入，避免循环依赖
    words = _EXPLANATION_METRIC_WORDS.get(metric) or (metric,)
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
    dec = dict(decomposition or {})
    dec_coverage = str(dec.get("coverage") or ("full" if dec.get("ok") else "none"))
    if dec_coverage != "none":
        cand = {"kind": KIND_DECOMPOSITION, "span": str(dec.get("note") or "")[:200],
                "reason": str(dec.get("reason") or "已取得定量分解材料"),
                "polarity": "affirm", "tense": "current", "certainty": "high",
                "locator": str(dec.get("locator") or ""), "source_n": "",
                "issuer": True, "text": str(dec.get("note") or "")[:200]}
        if best is None or _rank(cand["kind"]) > _rank(best["kind"]):
            best = cand
    if best is not None and best.get("kind") == KIND_DECOMPOSITION:
        flags[KIND_DECOMPOSITION] = dec_coverage == "full"
    out = best or {"kind": KIND_NONE, "span": "", "reason": "未取得该指标相关材料",
                   "polarity": "unknown", "tense": "current", "certainty": "high",
                   "locator": "", "source_n": "", "issuer": False, "text": ""}
    kind = out["kind"]
    # 覆盖三档：分解覆盖充分 = full（才算回答完成）；发行人定性归因或部分分解 = partial；
    # 背景/读数/否定/未披露/不确定/未来 = none（材料可能有用，但不构成对该问的覆盖）。
    if dec_coverage == "full":
        coverage = "full"
    elif dec_coverage == "partial":
        coverage = "partial"
    elif kind == KIND_MANAGEMENT:
        coverage = "partial"
    else:
        coverage = "none"
    out.update({
        "metric": metric,
        "kind_label": KIND_LABELS.get(kind, kind),
        "flags": dict(flags, **{KIND_DECOMPOSITION: dec_coverage == "full"}),
        "has_material": kind != KIND_NONE or dec_coverage != "none",
        "has_observation": bool(flags[KIND_OBSERVATION]) or has_observations
        or dec_coverage != "none",
        "has_background": bool(flags[KIND_BACKGROUND]),
        "has_management_cause": bool(flags[KIND_MANAGEMENT]),
        "has_decomposition": dec_coverage != "none",
        "has_negation": bool(flags[KIND_NEGATION] or flags[KIND_NOT_DISCLOSED]),
        "coverage": coverage,
        # 回答完成 = 分解覆盖充分；管理层归因只是"发行人说法"，不单独提升为完成
        "answered": coverage == "full",
        "decomposition": dec,
        "material_note": str(dec.get("note") or "") if dec_coverage != "none" else "",
    })
    return out


def _rank(kind: str) -> int:
    return {KIND_DECOMPOSITION: 6, KIND_MANAGEMENT: 5, KIND_BACKGROUND: 4,
            KIND_OBSERVATION: 3, KIND_NEGATION: 2, KIND_NOT_DISCLOSED: 2,
            KIND_TENTATIVE: 1, KIND_HYPOTHESIS: 1, KIND_NONE: 0}.get(kind, 0)
