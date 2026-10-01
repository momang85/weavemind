# -*- coding: utf-8 -*-
"""研究问题 → 问题类型：**规则化**的确定性映射（L2，2026-09-30 架构复核 M2）。

为什么需要它：此前 `runner.compile_plan` 只看"输入字段是否齐备"——于是
"**只研究现金转换**、不做情景预测"与"只分析营收变动的量价因素"选出来的模型**一模一样**
（都是 profit_bridge/cash_quality/scenario_sensitivity）。问题没有参与模型选择，
"数据齐备"被当成了"适用"。

本模块只做一件事：把**研究者写下的问题**按**显式规则**映射到问题类型，并说清每类问题
需要什么材料、允许哪些注册模型回答。纪律：

- **保留原问题**：不为了容易回答而改写它；分类结果里带上命中的关键词，读者能核对；
- **确定性**：纯字符串规则，不含 LLM、不联网；LLM 可以**提议**问题映射（在编排层），
  但公式执行、输入绑定、参数界限、验证与准入永远由确定性代码控制（架构 §4.4）；
- **数据齐备 ≠ 适用**：某模型输入齐备但与所问问题无关时，计划里必须写"与所问问题无关"
  并给出该模型回答的是哪一类问题，而不是把它塞进正文充数；
- **缺料就报缺什么**：问题类型声明它需要的材料（如"量价结构"需要销量/平均单价/
  分产品收入），资料里没有就如实列缺口，**不换跑无关模型**（架构 §4.5）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCHEMA_QUESTIONS = "weavemind.question_types/1"

# **否定**不能被当成"在研究这个问题"（2026-09-30 复核 M2 的第一条反例实测）：
# "只研究现金转换，**不做情景预测**" 若把"情景"算命中，计划就会去跑情景模型。
# 判据只看关键词**紧前面的**几个字，是显式的字符串规则，不做语义猜测。
_NEGATED_BEFORE_RE = re.compile(
    r"(不|非|无|没|勿|别|免|无需|不用)[^，。；、,;.！？!?\s]{0,4}\s*$")


@dataclass(frozen=True)
class QuestionType:
    """一类研究问题：命中词、需要的材料、能回答它的注册模型（按优先级）。"""

    qid: str
    label: str
    keywords: tuple[str, ...]
    needs_metrics: tuple[str, ...] = ()      # 回答它**需要**的观察（缺了要如实报缺口）
    needs_note: str = ""                     # 缺口怎么补（可行动）
    models: tuple[str, ...] = ()             # 允许回答该问题的注册模型（有序）
    # R3（2026-09-30 下午复核）：**问题意图**——历史分析 / 条件情景 / 统计预测。
    # 意图决定"能不能答"：预测类本版本一律未回答（`models=()`），且**同一子句里出现预测
    # 意图时，不得拿同子句的指标词去启动历史模型**（"预测明年的经营现金流"不是历史问题）。
    kind: str = "history"


# 子句切分（R3）：按标点与并列连词把请求切开，**逐子句**判意图。
# 为什么必须切：混合请求（"分析 2024 年现金流，并预测 2025 年"）里两半的答案状态不同；
# 不切就只能给整句一个结论，于是要么漏答历史、要么拿历史当预测的答案。
_CLAUSE_SPLIT_RE = re.compile(r"[，。；、,;.!?！？\n]+|并(?:且)?|同时|另外|以及|还要|再看")
_FORECAST_QID = "forecast_trend"
# V0（阶段V）：默认**经营研究组合**的问题类型 id——命中它时计划只采用组合里的三个模型
# （利润由何而来／现金为何变化／什么条件会改变判断），其余模型进探索入口，不重复堆叠。
COMBINATION_QID = "operating_research"


def clauses(question: str) -> list[str]:
    """把研究请求切成**子句**（保留每段的原文，用于说明命中了什么）。"""
    parts = [p.strip() for p in _CLAUSE_SPLIT_RE.split(str(question or ""))]
    return [p for p in parts if p]


def has_forecast_intent(text: str) -> bool:
    """该子句是否表达了**预测/未来**意图（命中词 + 否定语境规则，与 classify 同源）。"""
    hits = classify(text)
    return any(h["qid"] == _FORECAST_QID for h in hits)


def kind_of(qid: str) -> str:
    qt = _QT_BY_ID.get(str(qid))
    return str(qt.kind) if qt else ""


# ── 已规则化的问题类型（覆盖现有四族 + L3 的利润—现金链 + 尚未支持的量价）──────
QUESTION_TYPES: tuple[QuestionType, ...] = (
    # V0（阶段V）：**默认经营研究组合**——一次动作给一条论证线：利润由何而来、现金为何变化、
    # 什么条件会改变判断。三个模型共用同一份数据集，不重复堆叠旧模型。
    QuestionType(
        qid=COMBINATION_QID, label="经营研究（利润—现金—情景）",
        keywords=("经营研究", "经营情况", "经营分析", "经营业绩", "业绩变化", "经营质量",
                  "经营驱动", "业务驱动", "利润与现金", "为什么变化", "经营变化",
                  "经营研究组合"),
        needs_metrics=("revenue", "gross_profit", "net_profit", "operating_cashflow"),
        needs_note=("需要同主体同口径两期的收入/毛利/归母净利与经营现金流"
                    "（利润底稿＋现金调节桥都从这两期出发）"),
        models=("operating_drivers", "cash_reconciliation", "scenario_sensitivity"),
    ),
    QuestionType(
        qid="profit_attribution", label="利润变化归因",
        keywords=("利润桥", "利润变化", "净利润的变化", "归母净利润", "净利变化", "毛利变化",
                  "利润下滑", "利润下降", "利润增长的原因", "金额项构成", "利润归因",
                  "利润由什么构成", "分析利润"),
        needs_metrics=("net_profit", "gross_profit"),
        needs_note="需要同主体同口径的两期归母净利润与毛利（利润桥的两端）",
        models=("profit_bridge", "operating_drivers"),
    ),
    QuestionType(
        qid="cash_conversion", label="利润到现金的转化",
        keywords=("现金转化", "现金转换", "转成现金", "转化为现金", "现金含量", "现金质量",
                  "经营现金流", "现金流", "cfo", "回款", "收现", "现金创造",
                  "利润有没有转成现金", "利润增长是否转化"),
        needs_metrics=("operating_cashflow", "net_profit"),
        needs_note="需要经营现金流与归母净利润（同主体同期间）",
        models=("cash_reconciliation", "profit_to_cash", "cash_quality"),
    ),
    QuestionType(
        qid="working_capital", label="营运资本占用与周转",
        keywords=("占款", "营运资本", "营运资金", "周转", "账期", "应收账款", "存货", "应付账款",
                  "现金转换周期"),
        needs_metrics=("accounts_receivable", "inventory", "accounts_payable"),
        needs_note="需要应收/存货/应付的两期期末余额与收入/成本（周转分母）",
        models=("working_capital",),
    ),
    QuestionType(
        qid="scenario", label="条件情景与敏感性",
        keywords=("情景", "假设", "敏感性", "敏感度", "压力", "承压", "如果收入",
                  "若收入", "底线", "盈亏平衡"),
        needs_metrics=("revenue", "gross_profit", "net_profit"),
        needs_note="需要基期收入/毛利/归母净利润（情景从基期读数出发）",
        models=("scenario_sensitivity",),
        kind="scenario",
    ),
    QuestionType(
        qid="volume_price", label="量价结构分解",
        keywords=("量价", "销量", "单价", "价格变动", "产量", "分产品", "分地区", "产品结构",
                  "营收变动"),
        needs_metrics=("sales_volume", "average_price", "revenue_by_product"),
        needs_note=("需要可比产品集合的销量与平均单价（或分产品收入），且口径可比；"
                    "资料里没有就先补披露，**不拿无关模型充数**。"
                    "有同口径销量＋收入两期观察时由 operating_drivers 给量价分解（含结构混合）"),
        models=("operating_drivers",),    # U1：量价分解已有注册算子（口径可比为前提）
    ),
    # Q4（统计预测）：**本版本不开放**。它必须是一条**显式规则**而不是"没命中"——
    # 未命中会退回"按输入齐备性选模型"，于是"预测明年收入"会拿到利润桥/现金质量/营运资金
    # 一堆**不回答该问题**的输出（实测读数见 docs/统计预测门槛与禁用清单_20260930.md）。
    # 这里声明"这类问题没有任何注册模型"，编译计划就不再采用任何模型，并给出门槛与禁用清单。
    QuestionType(
        qid="forecast_trend", label="预测/趋势外推（本版本不开放）",
        keywords=("预测", "预计", "展望", "概率", "外推", "回归", "目标价", "估值", "明年",
                  "后年", "下一年", "未来", "走势", "增长趋势", "收入趋势", "业绩趋势",
                  "利润趋势", "趋势外推"),
        needs_metrics=(),                 # 门槛是"期数与样本"，不是某一列观察
        needs_note=("预测/趋势外推需要**不少于 5 个连续年度**的同主体同口径数据、"
                    "明确的分布与口径假设、以及样本外验证方案；本版本**不提供预测**，"
                    "门槛与禁用清单见 docs/统计预测门槛与禁用清单_20260930.md"),
        models=(),                        # 故意为空：不开放就不给任何模型
        kind="forecast",
    ),
)

_QT_BY_ID = {q.qid: q for q in QUESTION_TYPES}


def question_type(qid: str) -> QuestionType:
    return _QT_BY_ID[str(qid)]


def classify(question: str) -> list[dict]:
    """问题 → 命中的问题类型列表（按命中词数与声明顺序排序，**不做语义猜测**）。

    返回 `[{"qid", "label", "matched": [命中词, …], "excluded": [被否定掉的词, …]}]`；
    命中词一并给出去，读者可核对"为什么这样选模型"；被否定掉的词也列出来，
    免得"不做情景预测"这种写法让人以为问题没被读懂。没有任何命中 → 空列表。
    """
    text = str(question or "").lower()
    hits: list[tuple[int, int, dict]] = []
    for idx, qt in enumerate(QUESTION_TYPES):
        matched: list[str] = []
        excluded: list[str] = []
        for w in qt.keywords:
            if not w:
                continue
            low = w.lower()
            found = False
            start = 0
            while True:
                i = text.find(low, start)
                if i < 0:
                    break
                start = i + len(low)
                if _NEGATED_BEFORE_RE.search(text[max(0, i - 6):i]):
                    excluded.append(w)
                    continue
                found = True
            if found and w not in matched:
                matched.append(w)
            elif not found and w in excluded:
                continue
        if matched:
            hits.append((-len(matched), idx, {"qid": qt.qid, "label": qt.label,
                                              "matched": matched,
                                              "excluded": sorted(set(excluded))}))
    hits.sort(key=lambda x: (x[0], x[1]))
    return [h[2] for h in hits]


def models_for(qids) -> tuple[str, ...]:
    """这些类型允许的注册模型（按类型顺序去重；类型本身没有模型就什么都不给）。"""
    out: list[str] = []
    for qid in qids or ():
        qt = _QT_BY_ID.get(str(qid))
        if qt is None:
            continue
        for m in qt.models:
            if m not in out:
                out.append(m)
    return tuple(out)


def needs_for(qids) -> list[dict]:
    """这些类型需要的材料（给"缺什么、去哪补"的可行动缺口用）。"""
    out: list[dict] = []
    seen: set[str] = set()
    for qid in qids or ():
        qt = _QT_BY_ID.get(str(qid))
        if qt is None or qt.qid in seen:
            continue
        seen.add(qt.qid)
        out.append({"qid": qt.qid, "label": qt.label,
                    "metrics": list(qt.needs_metrics), "how": qt.needs_note})
    return out


def describe(qids) -> str:
    """一句话描述命中的问题类型（进计划说明/正文，读者据此核对模型选择）。"""
    labels = [str(_QT_BY_ID[q].label) for q in (qids or []) if q in _QT_BY_ID]
    return "、".join(labels) if labels else "未规则化的问题"
