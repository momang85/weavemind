# -*- coding: utf-8 -*-
"""Q1-v1 的**显式队列与对照评估**（§17.6/§17.7）：纯函数，不读文件、不联网。

这一层回答的是"规则到底错了几次"，而不是"故事讲得好不好"。它刻意做四件事：

1. **显式切分**：`split_at` 之前**结果已经公开**的案例才算探索期；`split_at` 之后才决策的
   才算留出；**decision 在切分前、结果在切分后**的案例单列成 `crossing`——既不当探索样本
   （会偷看结果），也不当留出样本（决策时点并不在切分之后）。
2. **成熟度状态位**：`mature / pending / missing / incomparable` 分开记，缺项**不填 0**，
   也不把"没有下一期"算成预测失败。
3. **共同覆盖上的公平对照**：方向错误率只在"规则与基线**都能评**、且实际符号非零"的同一批
   案例上比较；零预测记**弃权**、零实际**另列**，两者都不进正/负胜负分母；覆盖率分母保留
   初始公司池。
4. **负结果与退化如实写**：样本只有 1–2 例、两个窗口来自同一家公司（重叠）、探索期多数方向
   无法在留出外评估时，报告写"**无增量证据**"，不因为方向命中就宣布预测优势；反过来，
   方向为差时如实记 `disadvantage`（"当前样本上规则更差"），不把不利证据洗成"无证据"。

小样本统计：本模块**不做**显著性检验，也不给区间估计（§17.6.5：有足够公司与日期后再做）。
"""
from __future__ import annotations

import hashlib
import json

SCHEMA = "weavemind.q1_evaluation/1"
# q1_eval/1.1.0：_increment 改为方向感知（优/同/差 + 相同样本 + 池分类），
# 零 holdout 时不给优越性断言（只保留 no_evidence）。B2 批次起生效。
# q1_eval/1.2.0：_increment 优势判定再收紧——**只在留出（holdout）的共同有效样本上**
# 分方向；留出为 0 ⇒ 直接 no_evidence（探索/跨切分样本内表现不能当验证），
# 留出共同有效 0 例 ⇒ 同样 no_evidence；更差仍如实记 disadvantage，相同记无增量。
# B3 批次起生效。
EVAL_VERSION = "q1_eval/1.2.0"
VERDICTS = ("支持", "削弱", "未决")
BASELINES = ("profit_continuation", "exploration_majority")
BASELINE_LABELS = {"profit_continuation": "归母净利润变化方向延续",
                   "exploration_majority": "探索期多数方向（仅由探索期固定）",
                   "rule": "Q1 方向规则 sign(O_t) → sign(Y_op)"}


def _d(value):
    from decimal import Decimal, InvalidOperation
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def build_cases(readings: dict, *, known_at: dict | None = None,
                window: str = "") -> list[dict]:
    """读数 → 逐案例（含决策/结果公开时点）。事件时点由调用方**显式**给定。

    `known_at`：`{年度: ISO 时点}`，指"这一年的年度数字在哪份材料公开时可获得"
    （不是报告期末）。缺该年度时点的案例留空并标 `time_unknown`——不猜。
    """
    known_at = {str(k): str(v) for k, v in (known_at or {}).items()}
    cases: list[dict] = []
    for chain in readings.get("chains") or []:
        t = str(chain.get("t") or "")
        nxt = str(chain.get("next_year") or "")
        case = {
            "case_id": f"{readings.get('entity_id')}::t={t}",
            "entity_id": readings.get("entity_id"), "t": t, "next_year": nxt,
            "window": window,
            "decision_at": known_at.get(t, ""),
            "outcome_at": known_at.get(nxt, "") if nxt else "",
            "o_t": chain.get("o_t"), "sign_o": chain.get("sign_o"),
            "y_op": chain.get("y_op"), "sign_y": chain.get("sign_y"),
            "y_state": chain.get("y_state") or "",
            "baseline_profit_sign": (chain.get("baseline_profit") or {}).get("sign", "未知"),
            "rule_available": chain.get("o_t") is not None,
            "delta_e": (chain.get("e_next") or 0) - (chain.get("e_t") or 0)
            if chain.get("e_next") is not None and chain.get("e_t") is not None else None,
        }
        case["time_unknown"] = not (case["decision_at"] and
                                   (case["y_state"] != "mature" or case["outcome_at"]))
        cases.append(case)
    return cases


def _verdict(predicted: str, actual: str) -> str:
    if predicted in ("正", "负") and actual in ("正", "负"):
        return "支持" if predicted == actual else "削弱"
    return "未决"


def classify(cases: list[dict], *, split_at: str) -> dict:
    """显式切分：探索 / 留出 / 跨切分 / 无结果。时点缺失的**不猜**，单列 `time_unknown`。"""
    split = str(split_at or "")
    buckets = {"exploration": [], "holdout": [], "crossing": [], "no_outcome": [],
               "time_unknown": []}
    for case in cases:
        if case.get("time_unknown"):
            buckets["time_unknown"].append(case)
            continue
        if case.get("y_state") != "mature":
            buckets["no_outcome"].append(case)
            continue
        if case["outcome_at"] and case["outcome_at"] <= split:
            buckets["exploration"].append(case)
        elif case["decision_at"] and case["decision_at"] > split:
            buckets["holdout"].append(case)
        else:
            buckets["crossing"].append(case)
    return buckets


def exploration_majority(cases: list[dict]) -> dict:
    """探索期多数方向：只由探索期的**实际**符号固定；平票/为空 → 弃权。"""
    signs = [c["sign_y"] for c in cases if c.get("sign_y") in ("正", "负")]
    pos, neg = signs.count("正"), signs.count("负")
    if not signs:
        return {"sign": "未知", "state": "no_exploration_sample", "positive": 0,
                "negative": 0, "note": "探索期没有可用的实际方向 ⇒ 该基线弃权"}
    if pos == neg:
        return {"sign": "未知", "state": "tie", "positive": pos, "negative": neg,
                "note": "探索期正负平票 ⇒ 该基线弃权"}
    return {"sign": "正" if pos > neg else "负", "state": "fixed",
            "positive": pos, "negative": neg,
            "degenerate": len(signs) < 2,
            "note": "探索期样本 <2 例 ⇒ 基线退化，只作记录，不据此宣称有效"
            if len(signs) < 2 else "由探索期实际方向固定"}


def _score(case: dict, predicted: str) -> str:
    """单案例单规则：hit / miss / abstain / zero_actual / unavailable。"""
    actual = case.get("sign_y")
    if predicted not in ("正", "负"):
        return "abstain"
    if actual not in ("正", "负"):
        return "zero_actual" if actual == "零" else "unavailable"
    return "hit" if predicted == actual else "miss"


def _tally(cases: list[dict], pick) -> dict:
    out = {"scored": 0, "hit": 0, "miss": 0, "abstain": 0, "zero_actual": 0,
           "unavailable": 0, "cases": []}
    for case in cases:
        predicted = pick(case)
        verdict = _score(case, predicted)
        out[verdict] += 1
        if verdict in ("hit", "miss"):
            out["scored"] += 1
        out["cases"].append({"case_id": case["case_id"], "predicted": predicted,
                             "actual": case.get("sign_y"), "verdict": verdict})
    out["error_rate"] = (out["miss"] / out["scored"]) if out["scored"] else None
    out["coverage"] = f"{out['scored']}/{len(cases)}" if cases else "0/0"
    return out


def evaluate(cases: list[dict], *, pool: list[str], split_at: str,
             window_note: str = "", entity_windows: dict | None = None) -> dict:
    """一份显式队列 → 方向错误率、基线对照、成熟度、负结果表与"有没有增量"的结论。"""
    buckets = classify(cases, split_at=split_at)
    majority = exploration_majority(buckets["exploration"])
    # 多数方向基线只用于**非探索**案例（探索期自己不能给自己当基线）。
    exploration_ids = {c["case_id"] for c in buckets["exploration"]}
    for case in cases:
        if case["case_id"] in exploration_ids:
            case["baseline_majority_sign"] = "未知"
            case["baseline_majority_state"] = "not_scored_exploration"
        else:
            case["baseline_majority_sign"] = majority["sign"]
            case["baseline_majority_state"] = majority["state"]

    scored_all = buckets["exploration"] + buckets["holdout"] + buckets["crossing"]
    rule = _tally(scored_all, lambda c: c["sign_o"])
    profit = _tally(scored_all, lambda c: c["baseline_profit_sign"])
    majority_tally = _tally(scored_all, lambda c: c.get("baseline_majority_sign", "未知"))
    common = [c for c in scored_all
              if c["sign_o"] in ("正", "负") and c["baseline_profit_sign"] in ("正", "负")
              and c.get("sign_y") in ("正", "负")]
    common_rule = _tally(common, lambda c: c["sign_o"])
    common_profit = _tally(common, lambda c: c["baseline_profit_sign"])
    # 三向交集：规则/归母基线之外，多数方向只在**非探索**案例上有定义，
    # 只有三者都可评的同一交集才作同样本比较；否则分列，并在结论里注记。
    common3 = [c for c in common if c.get("baseline_majority_sign") in ("正", "负")]
    majority_common = _tally(common3, lambda c: c["baseline_majority_sign"])

    def _bucket_column(rows: list[dict]) -> dict:
        rt = _tally(rows, lambda c: c["sign_o"])
        pt = _tally(rows, lambda c: c["baseline_profit_sign"])
        mt = _tally(rows, lambda c: c.get("baseline_majority_sign", "未知"))
        return {"case_ids": [c["case_id"] for c in rows],
                "rule_scored": rt["scored"], "rule_hit": rt["hit"], "rule_miss": rt["miss"],
                "rule_abstain": rt["abstain"], "rule_error_rate": rt["error_rate"],
                "profit_scored": pt["scored"], "profit_hit": pt["hit"],
                "profit_miss": pt["miss"], "profit_abstain": pt["abstain"],
                "profit_error_rate": pt["error_rate"],
                "majority_scored": mt["scored"], "majority_miss": mt["miss"],
                "majority_abstain": mt["abstain"]}

    per_bucket = {name: _bucket_column(buckets[name])
                  for name in ("exploration", "crossing", "holdout", "no_outcome",
                               "time_unknown")}

    maturity = {
        "mature": len(buckets["exploration"]) + len(buckets["holdout"]) + len(buckets["crossing"]),
        "pending": len([c for c in buckets["no_outcome"] if c.get("y_state") == "pending"]),
        "missing": len([c for c in buckets["no_outcome"] if c.get("y_state") not in ("pending",)]),
        "incomparable": len([c for c in cases if not c.get("rule_available")
                             and c.get("y_state") == "mature"]),
        "time_unknown": len(buckets["time_unknown"]),
    }
    entity_counts: dict[str, int] = {}
    for case in cases:
        entity_counts[str(case.get("entity_id"))] = entity_counts.get(str(case.get("entity_id")), 0) + 1
    overlap = _overlap_note(entity_windows or {})
    increment = _increment(common_rule, common_profit, len(scored_all), overlap,
                           per_bucket=per_bucket, majority_common=majority_common,
                           three_way_comparable=bool(common) and len(common3) == len(common),
                           rule_all=rule, profit_all=profit, common_cases=common)

    return {
        "schema": SCHEMA, "eval_version": EVAL_VERSION,
        "split_at": str(split_at), "pool": list(pool),
        "coverage_denominator": {"cases": len(cases), "entities": len(pool),
                                 "cases_per_entity": entity_counts},
        "split": {k: [c["case_id"] for c in v] for k, v in buckets.items()},
        "split_note": ("探索=结果在切分前已公开；留出=决策在切分之后；"
                       "跨切分=决策在切分前而结果在切分后（两边都不算，单列）"),
        "maturity": maturity,
        "exploration_majority": majority,
        "tallies": {"rule": rule, "profit_continuation": profit,
                    "exploration_majority": majority_tally},
        "common_coverage": {"n": len(common), "cases": [c["case_id"] for c in common],
                            "rule": common_rule, "profit_continuation": common_profit,
                            "majority": majority_common,
                            "note": "只在规则与基线都能评、且实际符号非零的同一批案例上比较"
                            + ("；多数方向在同一交集上同样本比较"
                               if common and len(common3) == len(common)
                               else "；多数方向可评交集更小，分列见 increment.per_bucket，不作同样本合并")},
        "increment": increment,
        "window_note": window_note,
        "overlap": overlap,
        "negative_results": _negative_table(scored_all),
        "not_done": ["不做显著性检验/区间估计（样本远不足；有足够公司与日期后再做）",
                     "不把历史重建当样本外预测；已看过的结果不得继续叫未见样本",
                     "不从多个天数/阈值/权重里挑最好结果"],
        "case_rows": [_case_row(c) for c in cases],
    }


def _overlap_note(entity_windows: dict) -> dict:
    shared = []
    for entity, windows in (entity_windows or {}).items():
        years = [set(w.get("years") or ()) for w in windows]
        for i in range(len(years)):
            for j in range(i + 1, len(years)):
                hit = sorted(years[i] & years[j])
                if hit:
                    shared.append({"entity_id": entity, "years": hit})
    return {"any": bool(shared), "detail": shared,
            "note": ("同一公司的多个窗口共享年度（重叠），**不是独立样本**；"
                     "本节不把它当独立有效性试验，也不挑获胜窗口" if shared else
                     "未发现窗口重叠")}


def _increment(common_rule: dict, common_profit: dict, total: int, overlap: dict,
               *, per_bucket: dict | None = None, majority_common: dict | None = None,
               three_way_comparable: bool = False, rule_all: dict | None = None,
               profit_all: dict | None = None, common_cases: list | None = None) -> dict:
    """增量结论（B3 再收紧，q1_eval/1.2.0）：**方向只在留出的共同有效样本上判**。

    - 共同有效 = 规则与基线**都能评**、且实际符号非零的同一批案例（`common`）；
      其中**落在留出（holdout）桶**的才算验证样本——探索是样本内描述、跨切分两边都不算，
      都不能当验证（q1_eval/1.2.0：留出共同有效 0 例 ⇒ 直接 no_evidence，不报方向）。
    - 分方向按误判次数：留出上规则更差 ⇒ 如实记 `disadvantage`；相同 ⇒ **没有增量**；
      严格更优也只在与全队列共同有效样本 ≥5 且无窗口重叠时才是优势候选。
    - 全队列共同有效样本的误判数保留为**描述性**字段（`rule_miss`/`profit_miss`），不进方向判定。
    - 三向（规则／归母基线／探索期多数方向）只有在**同一交集**上才作同样本比较，否则分列并注记。
    """
    per_bucket = per_bucket or {}
    holdout = per_bucket.get("holdout") or {}
    holdout_scored = int(holdout.get("rule_scored") or 0)
    common_n = int(common_rule.get("scored") or 0)
    # 弃权只在"全体可评案例"上有意义：共同覆盖按构造不含弃权。
    rule_abstain_all = int((rule_all or common_rule).get("abstain") or 0)
    profit_abstain_all = int((profit_all or common_profit).get("abstain") or 0)
    # 衔接点C（B3 首例）：留出 ∩ 共同有效 = 验证样本；由调用方传入共同有效案例明细。
    holdout_ids = set(holdout.get("case_ids") or [])
    holdout_valid = [c for c in (common_cases or []) if c.get("case_id") in holdout_ids]
    holdout_rule_c = _tally(holdout_valid, lambda c: c["sign_o"])
    holdout_profit_c = _tally(holdout_valid, lambda c: c["baseline_profit_sign"])
    holdout_n = len(holdout_valid)
    base = {
        "rule_error_rate": common_rule.get("error_rate"),
        "profit_error_rate": common_profit.get("error_rate"),
        "rule_scored": common_n, "profit_scored": int(common_profit.get("scored") or 0),
        "rule_miss": int(common_rule.get("miss") or 0),
        "profit_miss": int(common_profit.get("miss") or 0),
        "rule_abstain": rule_abstain_all,
        "profit_abstain": profit_abstain_all,
        "common_effective_cases": common_n, "holdout_scored": holdout_scored,
        "holdout_common": {"n": holdout_n,
                           "rule_miss": int(holdout_rule_c.get("miss") or 0),
                           "rule_scored": int(holdout_rule_c.get("scored") or 0),
                           "profit_miss": int(holdout_profit_c.get("miss") or 0),
                           "profit_scored": int(holdout_profit_c.get("scored") or 0)},
        "per_bucket": per_bucket,
        "three_way_comparable": bool(three_way_comparable),
        "majority_common": majority_common,
    }
    if not total:
        return dict(base, direction=None, verdict="no_evidence",
                    reason="没有任何可评案例（成熟且规则/基线可比为 0）⇒ 无证据，也无不利证据")
    if common_n == 0:
        return dict(base, direction=None, verdict="no_evidence",
                    reason="没有规则与归母基线**都能评**的共同覆盖案例（规则弃权 {} 例／基线弃权 {} 例）"
                           "⇒ 不存在同样本比较，不报方向、不报优劣".format(
                               base["rule_abstain"], base["profit_abstain"]))
    if holdout_n == 0:
        return dict(base, direction=None, verdict="no_evidence",
                    reason="共同有效样本 {} 例，但**留出共同有效 0 例**：方向只在留出上判"
                           "（q1_eval/1.2.0），探索/跨切分的样本内表现不能当验证 ⇒ "
                           "不报方向、不报优劣".format(common_n))
    holdout_denominator = ("留出共同有效样本 {} 例：规则误判 {}/{}，归母基线误判 {}/{}"
                           "（全队列共同有效 {} 例：规则 {}/{}、基线 {}/{}，仅描述；"
                           "规则另弃权 {} 例／基线另弃权 {} 例）".format(
                               holdout_n, base["holdout_common"]["rule_miss"], holdout_n,
                               base["holdout_common"]["profit_miss"],
                               base["holdout_common"]["profit_scored"],
                               common_n, base["rule_miss"], common_n,
                               base["profit_miss"], base["profit_scored"],
                               base["rule_abstain"], base["profit_abstain"]))
    if base["holdout_common"]["rule_miss"] > base["holdout_common"]["profit_miss"]:
        return dict(base, direction="差", verdict="disadvantage",
                    reason=holdout_denominator + " ⇒ 留出上**规则比基线更差**；"
                           "如实记为不利证据（样本仍小，不据此宣称规则无效）")
    if base["holdout_common"]["rule_miss"] == base["holdout_common"]["profit_miss"]:
        return dict(base, direction="同", verdict="no_evidence",
                    reason=holdout_denominator + " ⇒ 留出同样本上误判次数相同，**没有增量**；"
                           "保留为经营解释能力，不叫优势")
    direction = "优"
    if common_n < 5:
        return dict(base, direction=direction, verdict="no_evidence",
                    reason=holdout_denominator + " ⇒ 留出方向虽优，但有效样本不足 5 例，"
                                         "**不报优势**（小样本只作记录）")
    if overlap.get("any"):
        return dict(base, direction=direction, verdict="no_evidence",
                    reason=holdout_denominator + " ⇒ 留出方向虽优，但窗口来自同一家公司、年份重叠，"
                                         "**不是独立样本**，不报优势")
    return dict(base, direction=direction, verdict="directional",
                reason=holdout_denominator + "，且留出上规则误判少于基线 ⇒ 列为**优势候选**"
                                     "（仍需真实队列与人工复核，本批不作最终结论）")


def _negative_table(cases: list[dict]) -> dict:
    misses = [{"case_id": c["case_id"], "predicted": c["sign_o"], "actual": c["sign_y"]}
              for c in cases if _score(c, c["sign_o"]) == "miss"]
    baseline_misses = [{"case_id": c["case_id"], "predicted": c["baseline_profit_sign"],
                        "actual": c["sign_y"]}
                       for c in cases if _score(c, c["baseline_profit_sign"]) == "miss"]
    return {"rule_misses": misses, "profit_baseline_misses": baseline_misses,
            "note": "全部候选与负结果都写在同一试验账里，不隐藏、不事后换窗口"}


def _case_row(case: dict) -> dict:
    return {"case_id": case["case_id"], "window": case.get("window"),
            "t": case.get("t"), "next_year": case.get("next_year"),
            "decision_at": case.get("decision_at"), "outcome_at": case.get("outcome_at"),
            "o_t": case.get("o_t"), "sign_o": case.get("sign_o"),
            "y_op": case.get("y_op"), "sign_y": case.get("sign_y"),
            "y_state": case.get("y_state"),
            "rule_verdict": _score(case, case["sign_o"]),
            "profit_baseline_verdict": _score(case, case["baseline_profit_sign"]),
            "majority_baseline_verdict": _score(case, case.get("baseline_majority_sign", "未知")),
            "majority_baseline_state": case.get("baseline_majority_state", "")}


def follow_up(case: dict | None, *, verify_by: str = "") -> dict:
    """旧卡 → 实际 → 支持/削弱/未决（§17.5 第 4 组；缺结果不写读数）。"""
    if not case or case.get("y_state") != "mature":
        return {"verify_by": verify_by, "status": "待数据", "actual_reading": None,
                "difference": None, "mechanism_verdict": "未决",
                "maturity": "pending", "note": "下一同口径年度尚未取得：不写实际读数"}
    verdict = _verdict(case.get("sign_o"), case.get("sign_y"))
    return {
        "verify_by": verify_by or case.get("outcome_at") or "",
        "status": "已核验",
        "maturity": "mature",
        "actual_reading": {"t": case.get("t"), "next_year": case.get("next_year"),
                           "y_op": case.get("y_op"), "sign_y": case.get("sign_y")},
        "difference": {"predicted_sign": case.get("sign_o"), "actual_sign": case.get("sign_y"),
                       "hit": verdict == "支持"},
        "mechanism_verdict": verdict,
        "note": ("历史重建下的核验：用**已公开**的下一同口径年度结果比对原卡方向；"
                 "不构成样本外预测证明"),
    }


def markdown(evaluation: dict, *, title: str = "Q1-v1 方向规则对照（真实两公司）") -> str:
    t = evaluation["tallies"]
    c = evaluation["common_coverage"]
    inc = evaluation["increment"]
    mat = evaluation["maturity"]
    lines = [f"# {title}", "",
             f"- 公司池：{'、'.join(evaluation['pool'])}（{evaluation['coverage_denominator']['entities']} 家）；"
             f"案例 {evaluation['coverage_denominator']['cases']} 例（同一公司多窗口会重叠）",
             f"- 切分时点：`{evaluation['split_at']}`；{evaluation['split_note']}",
             f"- 成熟度：成熟 {mat['mature']}／待数据 {mat['pending']}／缺失 {mat['missing']}／"
             f"不可比 {mat['incomparable']}／时点未知 {mat['time_unknown']}",
             f"- 探索期多数方向：{evaluation['exploration_majority']['sign']}"
             f"（{evaluation['exploration_majority']['note']}）",
             "", "## 方向错误率（各自覆盖）", "",
             "| 规则 | 可评 | 命中 | 误判 | 弃权 | 零实际 | 错误率 |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for key, label in (("rule", BASELINE_LABELS["rule"]),
                       ("profit_continuation", BASELINE_LABELS["profit_continuation"]),
                       ("exploration_majority", BASELINE_LABELS["exploration_majority"])):
        row = t[key]
        rate = "—" if row["error_rate"] is None else f"{row['error_rate'] * 100:.1f}%"
        lines.append(f"| {label} | {row['scored']} | {row['hit']} | {row['miss']} | "
                     f"{row['abstain']} | {row['zero_actual']} | {rate} |")
    lines += ["", f"共同覆盖（{c['n']} 例：{('、'.join(c['cases']) or '无')}）："
                  f"规则 {c['rule']['miss']}/{c['rule']['scored']} 次误判，"
                  f"归母基线 {c['profit_continuation']['miss']}/{c['profit_continuation']['scored']} 次误判。",
              "", "## 逐案例", "",
              "| 案例 | 决策时点 | 结果公开 | O_t | 实际 Y_op | 规则 | 归母基线 | 多数方向 |",
              "|---|---|---|---:|---:|---|---|---|"]
    for row in evaluation["case_rows"]:
        lines.append(f"| {row['case_id']} | {row['decision_at'] or '—'} "
                     f"| {row['outcome_at'] or '—'} | {_fmt(row['o_t'])} | {_fmt(row['y_op'])} "
                     f"| {row['rule_verdict']} | {row['profit_baseline_verdict']} "
                     f"| {row['majority_baseline_verdict']}"
                     f"{'' if row['majority_baseline_state'] == 'fixed' else '（' + row['majority_baseline_state'] + '）'} |")
    inc = evaluation["increment"]
    bucket_labels = {"exploration": "探索（样本内，不当验证）",
                     "crossing": "跨切分（决策在前、结果在后，两边都不算）",
                     "holdout": "留出（唯一可支持优势主张）",
                     "no_outcome": "无结果", "time_unknown": "时点未知"}
    bucket_lines = []
    for name, label in bucket_labels.items():
        b = (inc.get("per_bucket") or {}).get(name) or {}
        if not b.get("case_ids"):
            continue
        bucket_lines.append(
            f"- {label}：{len(b['case_ids'])} 例（{'、'.join(b['case_ids'])}）；"
            f"规则误判 {b['rule_miss']}/{b['rule_scored']}（弃权 {b['rule_abstain']}）；"
            f"归母基线误判 {b['profit_miss']}/{b['profit_scored']}（弃权 {b['profit_abstain']}）")
    hc = inc.get("holdout_common") or {}
    lines += ["", "## 增量结论（如实）", "",
              f"- 方向（**只在留出共同有效样本 {hc.get('n', 0)} 例上判**：规则误判 "
              f"{hc.get('rule_miss', 0)}/{hc.get('rule_scored', 0)}，"
              f"归母基线误判 {hc.get('profit_miss', 0)}/{hc.get('profit_scored', 0)}）："
              f"**{inc.get('direction') or '不可比'}**",
              f"- 全队列共同有效 {inc.get('common_effective_cases', 0)} 例（描述性，不进方向判定）："
              f"规则误判 {inc.get('rule_miss', 0)}/{inc.get('rule_scored', 0)}，"
              f"归母基线误判 {inc.get('profit_miss', 0)}/{inc.get('profit_scored', 0)}"
              f"（规则另弃权 {inc.get('rule_abstain', 0)}／基线另弃权 {inc.get('profit_abstain', 0)}）",
              f"- 判定：**{inc['verdict']}**——{inc['reason']}",
              f"- 留出可评 {inc.get('holdout_scored', 0)} 例"
              + ("（**零留出** ⇒ 本批不报任何预测优势）" if not inc.get("holdout_scored") else ""),
              f"- 规则错误率 {_rate(inc['rule_error_rate'])}；归母基线错误率 {_rate(inc['profit_error_rate'])}"]
    if bucket_lines:
        lines += ["- 分桶（同样本比较只在各桶内做；探索与跨切分都不计入留出主张）："] + bucket_lines
    if not inc.get("three_way_comparable", False):
        lines.append("- 三向对照：多数方向只对非探索案例有定义，与两向共同覆盖不在同一交集"
                     " ⇒ **分列呈现**（见上表各桶的多数方向列），不做跨样本拼接")
    lines += [f"- 窗口重叠：{evaluation['overlap']['note']}",
              "", "## 负结果账", "",
              f"- 规则误判：{evaluation['negative_results']['rule_misses'] or '无'}",
              f"- 归母基线误判：{evaluation['negative_results']['profit_baseline_misses'] or '无'}",
              "- 本层未做：" + "；".join(evaluation["not_done"]), ""]
    return "\n".join(lines)


def _fmt(value) -> str:
    d = _d(value)
    return "—" if d is None else f"{d * 100:+.2f}%"


def _rate(value) -> str:
    d = _d(value)
    return "不可评" if d is None else f"{d * 100:.1f}%"


def evaluation_hash(evaluation: dict) -> str:
    payload = {k: v for k, v in evaluation.items() if k != "generated_at"}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     default=str).encode("utf-8")).hexdigest()
