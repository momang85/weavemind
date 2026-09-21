# -*- coding: utf-8 -*-
"""D1 冻结反例：主张支持 / 解释范围 / 证据计数（离线纯函数，可重复运行）。

为什么单独一个跑法：阶段 D 第一小批要求"先复现反例、再最小修复、旧/新读数各留一份"。
反例必须能**离线重放**（不联网、不调用模型），否则下次改动无法证明没把旧错误带回来。

用法：
    python scripts/claims_d1_cases.py before   # 修复前读数（留史证据，不作断言）
    python scripts/claims_d1_cases.py after    # 修复后读数（与期望逐条比对）
    python scripts/claims_d1_cases.py check    # 重放并与 after 文件里的期望比对

产物：`evals/claims/d1_counterexamples_<mode>.json`
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# `WM_CASE_ROOT`：指向另一棵树（如修复前的 git worktree）——用旧代码跑同一批用例，
# 生成可对照的 before 读数；默认用脚本所在仓库。
# `WM_CASE_OUT`：输出目录（默认仓库内 `evals/claims/`）——用工作树的旧代码跑、
# 把产物写回当前仓库时用它。
ROOT = Path(os.environ.get("WM_CASE_ROOT") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

import narrative_evidence as ne            # noqa: E402
import report_brief as rb                  # noqa: E402
import workspace as ws_mod                 # noqa: E402

OUT_DIR = Path(os.environ.get("WM_CASE_OUT") or (ROOT / "evals" / "claims"))

# 真实年报（洋河 2024，东方财富公告原文摘录）里的真实数字，与实机底稿一致：
# 营业收入 2024 = 288.76 亿元、2023 = 331.26 亿元，同比 -12.83%。
REAL_ROWS = [
    {"metric": "revenue", "metric_label": "营业收入", "period": "2023年", "year": 2023,
     "value": 331.26, "unit": "亿元", "caliber": "合并", "fact_id": "fact-rev-2023"},
    {"metric": "revenue", "metric_label": "营业收入", "period": "2024年", "year": 2024,
     "value": 288.76, "unit": "亿元", "caliber": "合并", "fact_id": "fact-rev-2024"},
    {"metric": "net_profit", "metric_label": "归母净利润", "period": "2024年", "year": 2024,
     "value": 66.73, "unit": "亿元", "caliber": "合并", "fact_id": "fact-np-2024"},
]
REAL_DERIVED = [
    {"metric": "revenue_yoy", "metric_label": "营业收入同比", "period": "2024年", "year": 2024,
     "value": -12.83, "unit": "%", "caliber": "合并",
     "derived_from": ["fact-rev-2023", "fact-rev-2024"]},
]
# 真实年报的比率两期水平（同一份年报的数字，用于百分点差的可重算复核）
REAL_RATIO_ROWS = [
    {"metric": "gross_margin", "metric_label": "毛利率", "period": "2023年", "year": 2023,
     "value": 75.25, "unit": "%", "caliber": "合并", "fact_id": "fact-gm-2023"},
    {"metric": "gross_margin", "metric_label": "毛利率", "period": "2024年", "year": 2024,
     "value": 73.16, "unit": "%", "caliber": "合并", "fact_id": "fact-gm-2024"},
    {"metric": "net_margin", "metric_label": "归母净利率", "period": "2023年", "year": 2023,
     "value": 30.24, "unit": "%", "caliber": "合并", "fact_id": "fact-nm-2023"},
    {"metric": "net_margin", "metric_label": "归母净利率", "period": "2024年", "year": 2024,
     "value": 23.11, "unit": "%", "caliber": "合并", "fact_id": "fact-nm-2024"},
    {"metric": "cashflow_coverage", "metric_label": "经营现金流对归母净利润的覆盖",
     "period": "2023年", "year": 2023, "value": 61.2, "unit": "%", "caliber": "合并",
     "fact_id": "fact-cov-2023"},
    {"metric": "cashflow_coverage", "metric_label": "经营现金流对归母净利润的覆盖",
     "period": "2024年", "year": 2024, "value": 69.37, "unit": "%", "caliber": "合并",
     "fact_id": "fact-cov-2024"},
    {"metric": "debt_ratio", "metric_label": "资产负债率", "period": "2023年", "year": 2023,
     "value": 25.42, "unit": "%", "caliber": "合并", "fact_id": "fact-dr-2023"},
    {"metric": "debt_ratio", "metric_label": "资产负债率", "period": "2024年", "year": 2024,
     "value": 23.24, "unit": "%", "caliber": "合并", "fact_id": "fact-dr-2024"},
]
# 年报原文（摘录页 2/3）里的真实句子/表格行
REAL_SEGMENT_LINE = ("各类产品收入情况如下： 单位：元 营业收入 产品类别 2024 年 同比增减 "
                     "中高档酒 24,317,191,550.05 -14.79% 普通酒 3,931,104,279.57 -0.49%")
REAL_TOP5_LINE = ("2024 年度,公司前五名经销客户的合计销售金额为 235,177.00 万元，"
                  "占本年度销售总额 8.15%")


CASES: list[dict] = [
    {
        "id": "c1_cross_period_metric_value",
        "note": "只有 2024 收入 100 亿元：'2023 净利润 100 万元'不得被它支持（跨期/跨指标/跨单位）",
        "kind": "claims",
        "body": "2023 年归母净利润 100 万元。",
        "rows": [{"metric": "revenue", "metric_label": "营业收入", "period": "2024年",
                  "year": 2024, "value": 100.0, "unit": "亿元", "caliber": "合并",
                  "fact_id": "fact-rev-2024"}],
        "derived": [],
        "citations": [],
        "expect": {"statuses": ["unsupported"], "assertion_statuses": [["unsupported"]],
                   "fact_ids": [[]]},
    },
    {
        "id": "c2_mixed_true_false_same_sentence",
        "note": "一句里正确收入 + 虚假利润 999：整句不得 bound（部分支持）",
        "kind": "claims",
        "body": "2024 年营业收入 100 亿元，归母净利润 999 亿元。",
        "rows": [{"metric": "revenue", "metric_label": "营业收入", "period": "2024年",
                  "year": 2024, "value": 100.0, "unit": "亿元", "caliber": "合并",
                  "fact_id": "fact-rev-2024"},
                 {"metric": "net_profit", "metric_label": "归母净利润", "period": "2024年",
                  "year": 2024, "value": 20.0, "unit": "亿元", "caliber": "合并",
                  "fact_id": "fact-np-2024"}],
        "derived": [],
        "citations": [],
        "expect": {"statuses": ["partially_supported"],
                   "assertion_statuses": [["supported", "unsupported"]],
                   "fact_ids": [["fact-rev-2024"]]},
    },
    {
        "id": "c3_target_claim_with_issuer_citation",
        "note": "有年报引用但缺目标值/实际口径：目标类判断必须点名缺口，不能因引用或数字命中通过",
        "kind": "claims",
        "body": "公司 2024 年经营目标完成率 100% 并已达成 [1]。",
        "rows": [{"metric": "revenue", "metric_label": "营业收入", "period": "2024年",
                  "year": 2024, "value": 100.0, "unit": "亿元", "caliber": "合并",
                  "fact_id": "fact-rev-2024"}],
        "derived": [],
        "citations": [{"n": 1, "type": "issuer_annual_report",
                       "title": "洋河股份2024年年度报告",
                       "url": "https://static.cninfo.com.cn/finalpage/2025-04-29/x.PDF"}],
        "expect": {"statuses": ["needs_check"], "types": ["target_claim"],
                   "reason_contains": [["目标值", "实际口径"]]},
    },
    {
        "id": "c4_sign_mismatch",
        "note": "底稿同比是 -12.83%：正文'下降 12.83%'必须绑定；'增长 12.83%'不得绑定（方向不符）",
        "kind": "claims",
        "body": "2024 年营业收入同比下降 12.83%。2024 年营业收入同比增长 12.83%。",
        "rows": REAL_ROWS,
        "derived": REAL_DERIVED,
        "citations": [],
        "expect": {"statuses": ["bound", "unsupported"],
                   "assertion_statuses": [["supported"], ["unsupported"]]},
    },
    {
        "id": "c5_unit_conversion",
        "note": "单位换算正例（0.01 亿元 = 100 万元）与量级不符反例（20 万元 ≠ 20 亿元）",
        "kind": "claims",
        "body": "2024 年营业收入 0.01 亿元。2024 年归母净利润 20 万元。",
        "rows": [{"metric": "revenue", "metric_label": "营业收入", "period": "2024年",
                  "year": 2024, "value": 100.0, "unit": "万元", "caliber": "合并",
                  "fact_id": "fact-rev-wan"},
                 {"metric": "net_profit", "metric_label": "归母净利润", "period": "2024年",
                  "year": 2024, "value": 20.0, "unit": "亿元", "caliber": "合并",
                  "fact_id": "fact-np-yi"}],
        "derived": [],
        "citations": [],
        "expect": {"statuses": ["bound", "unsupported"],
                   "assertion_statuses": [["supported"], ["unsupported"]]},
    },
    {
        "id": "c6a_real_disclosure_core_metric_positive",
        "note": "真实年报数字（288.76 亿元、同比 -12.83%）+ 同口径事实：必须绑定",
        "kind": "claims",
        "body": "2024 年营业收入 288.76 亿元，同比下降 12.83%。",
        "rows": REAL_ROWS,
        "derived": REAL_DERIVED,
        "citations": [],
        "expect": {"statuses": ["bound"], "assertion_statuses": [["supported", "supported"]]},
    },
    {
        "id": "c6b_real_disclosure_segment_row_not_total",
        "note": "年报真实分部行（中高档酒 243.17 亿元、-14.79%）：不得冒充营业收入总额事实",
        "kind": "claims",
        "body": REAL_SEGMENT_LINE,
        "rows": REAL_ROWS,
        "derived": REAL_DERIVED,
        "citations": [],
        "expect": {"statuses": ["unsupported"], "not_fact_ids": ["fact-rev-2024"]},
    },
    {
        "id": "c6c_real_disclosure_non_core_metric",
        "note": "年报真实附注句（前五名经销客户 235,177.00 万元 / 8.15%）：底稿无对应指标 → 待核查并写明原因",
        "kind": "claims",
        "body": REAL_TOP5_LINE,
        "rows": REAL_ROWS,
        "derived": REAL_DERIVED,
        "citations": [],
        "expect": {"statuses": ["needs_check"], "reason_contains": [["未提取到指标名"]]},
    },
    {
        "id": "c7_real_draft_paragraphs",
        "note": "实机模型草稿真实段落（洋河 2024）：覆盖率别名命中、百分点差由两期水平重算支持、跨句年度不借用",
        "kind": "claims",
        "body": ("盈利质量方面，毛利率由 2023 年的 75.25% 降至 2024 年的 73.16%，"
                 "下降 2.09 个百分点；归母净利率由 30.24% 降至 23.11%，下降 7.13 个百分点。"
                 "现金流质量方面，经营活动现金流净额对归母净利润的覆盖由 2023 年的 61.20% "
                 "升至 2024 年的 69.37%，上升 8.17 个百分点。"
                 "结构方面，资产负债率由 25.42% 降至 23.24%，下降 2.18 个百分点。"),
        "rows": REAL_RATIO_ROWS,
        "derived": [],
        "citations": [],
        "expect": {"statuses": ["partially_supported", "bound", "partially_supported"],
                   "assertion_statuses": [
                       ["supported", "supported", "supported",
                        "needs_check", "needs_check", "supported"],
                       ["supported", "supported", "supported"],
                       ["needs_check", "needs_check", "supported"]]},
    },
    {
        "id": "e1_explanation_scope",
        "note": "只取得收入解释：利润与现金流不得标'解释已取得'",
        "kind": "explanation",
        "rows": REAL_ROWS + [
            {"metric": "operating_cashflow", "metric_label": "经营活动现金流净额",
             "period": "2023年", "year": 2023, "value": 61.3, "unit": "亿元",
             "caliber": "合并", "fact_id": "fact-ocf-2023"},
            {"metric": "operating_cashflow", "metric_label": "经营活动现金流净额",
             "period": "2024年", "year": 2024, "value": 46.29, "unit": "亿元",
             "caliber": "合并", "fact_id": "fact-ocf-2024"},
            {"metric": "net_profit", "metric_label": "归母净利润", "period": "2023年",
             "year": 2023, "value": 100.16, "unit": "亿元", "caliber": "合并",
             "fact_id": "fact-np-2023"},
        ],
        "derived": REAL_DERIVED + [
            {"metric": "net_profit_yoy", "metric_label": "归母净利润同比", "period": "2024年",
             "year": 2024, "value": -33.38, "unit": "%",
             "derived_from": ["fact-np-2023", "fact-np-2024"]},
            {"metric": "operating_cashflow_yoy", "metric_label": "经营活动现金流净额同比",
             "period": "2024年", "year": 2024, "value": -24.49, "unit": "%",
             "derived_from": ["fact-ocf-2023", "fact-ocf-2024"]},
        ],
        "evidence": {"records": [{
            "kind": "change_explanation", "has_location": True, "admission": "admitted",
            "url": "https://static.cninfo.com.cn/finalpage/2025-04-29/x.PDF",
            "snippet": "报告期内营业收入同比下降，主要系公司主动调整产品结构与渠道库存所致。",
            "locator": "第 3 页 · 小节：经营情况讨论与分析（字符 0-40）",
            "source_type": "issuer_annual_report",
            "document_provenance": "issuer_annual_report",
            "content_hash": "ev-rev-1"}]},
        "expect": {"has_explanation": {"营业收入": True, "归母净利润": False,
                                       "经营活动现金流净额": False}},
    },
    {
        "id": "n1_located_counting",
        "note": "同一 URL 的检索摘要不重复计数：located 只数已准入且有正文位置的片段",
        "kind": "located",
        "docs": [{"title": "洋河股份2024年年度报告",
                  "url": "https://static.cninfo.com.cn/finalpage/2025-04-29/8.PDF",
                  "text": ("第三节 管理层讨论与分析\n\n一、经营情况讨论与分析\n\n"
                           "2024年度营业收入288.76亿元，同比下降12.83%。\n\n"
                           "七、财务报表附注\n\n现金流量表附注：经营活动现金流量净额46.29亿元。")}],
        "snips": [
            {"title": "洋河股份2024年年度报告", "url": "https://static.cninfo.com.cn/finalpage/2025-04-29/8.PDF",
             "snippet": "2024年度营业收入288.76亿元，同比下降12.83%"},
            {"title": "洋河股份风险因素提示", "url": "https://news.example/2025/04/20/a.html",
             "snippet": "洋河股份可能面对的风险因素包括行业竞争加剧与库存压力。"},
        ],
        "expect": {"located": 2, "snippet_hints": 1,
                   "missing_labels_contains": ["经营变化解释", "风险因素"]},
    },
]


def _claims_observation(case: dict) -> dict:
    kwargs = {"subject": "洋河股份", "periods": [2023, 2024]}
    try:                                     # D1 之后带 evidence（证据片段关联）
        claims = rb._claims(case.get("body") or "", case.get("rows") or [],
                            case.get("derived") or [], case.get("citations") or [],
                            evidence=case.get("evidence"), **kwargs)
    except TypeError:                        # 修复前的签名（before 读数用）
        claims = rb._claims(case.get("body") or "", case.get("rows") or [],
                            case.get("derived") or [], case.get("citations") or [],
                            **kwargs)
    return {
        "claims": [{"text": c.get("text"), "type": c.get("type"),
                    "status": c.get("status"),
                    "reason": c.get("reason"),
                    "fact_ids": list(c.get("fact_ids") or []),
                    "assertion_statuses": [a.get("support_status")
                                           for a in (c.get("assertions") or [])],
                    "assertions": [{"text": a.get("text"), "metric": a.get("metric"),
                                    "period": a.get("period"), "value": a.get("value"),
                                    "unit": a.get("unit"),
                                    "support_status": a.get("support_status"),
                                    "reason": a.get("reason")}
                                   for a in (c.get("assertions") or [])]}
                   for c in claims],
    }


def _explanation_observation(case: dict) -> dict:
    ch = rb._change_explanation(case.get("rows") or [], case.get("derived") or [],
                                [2023, 2024], [], case.get("evidence") or {}, [])
    return {
        "management": [{"text": m.get("text"), "source_n": m.get("source_n")}
                       for m in (ch.get("management") or [])],
        "unproven": [{"label": u.get("label"), "has_explanation": u.get("has_explanation"),
                      "matched": u.get("matched")} for u in (ch.get("unproven") or [])],
    }


def _located_observation(case: dict) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="wm_d1_"))
    old_root = ws_mod.WORKSPACE_ROOT
    try:
        ws_mod.configure_workspace_root(str(tmp))
        tid = "d1-located"
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "fetch_snapshot.json").write_text(
            json.dumps(case.get("docs") or [], ensure_ascii=False), encoding="utf-8")
        (proj / "search_results.json").write_text(
            json.dumps(case.get("snips") or [], ensure_ascii=False), encoding="utf-8")
        payload = ne.build(tid, periods=[2023, 2024], company="洋河股份",
                           company_id="002304.SZ", as_of="2025-04-30",
                           project="default", ws_dir=tmp / "ws")
        return {"located": payload.get("located"),
                "snippet_hints": payload.get("snippet_hints"),
                "missing_labels": payload.get("missing_labels"),
                "records": [{"kind": r.get("kind"), "has_location": r.get("has_location"),
                             "admission": r.get("admission"),
                             "locator": r.get("locator")}
                            for r in (payload.get("records") or [])]}
    finally:
        ws_mod.configure_workspace_root(str(old_root))
        shutil.rmtree(tmp, ignore_errors=True)


def _observe(case: dict) -> dict:
    kind = case.get("kind")
    if kind == "claims":
        return _claims_observation(case)
    if kind == "explanation":
        return _explanation_observation(case)
    if kind == "located":
        return _located_observation(case)
    raise SystemExit(f"未知用例类型：{kind}")


def _check_case(case: dict, obs: dict) -> list[str]:
    """返回不符合期望的条目（空 = 通过）。"""
    exp = case.get("expect") or {}
    bad: list[str] = []
    if case["kind"] == "claims":
        got = [c["status"] for c in obs["claims"]]
        if "statuses" in exp and got != exp["statuses"]:
            bad.append(f"statuses 期望 {exp['statuses']} 实得 {got}")
        if "types" in exp:
            got_types = [c["type"] for c in obs["claims"]]
            if got_types != exp["types"]:
                bad.append(f"types 期望 {exp['types']} 实得 {got_types}")
        if "assertion_statuses" in exp:
            got_as = [c["assertion_statuses"] for c in obs["claims"]]
            if got_as != exp["assertion_statuses"]:
                bad.append(f"assertion_statuses 期望 {exp['assertion_statuses']} 实得 {got_as}")
        if "fact_ids" in exp:
            got_f = [c["fact_ids"] for c in obs["claims"]]
            if got_f != exp["fact_ids"]:
                bad.append(f"fact_ids 期望 {exp['fact_ids']} 实得 {got_f}")
        if "not_fact_ids" in exp:
            for c in obs["claims"]:
                for fid in exp["not_fact_ids"]:
                    if fid in (c.get("fact_ids") or []):
                        bad.append(f"不得绑定 {fid}，实得 {c.get('fact_ids')}")
        if "reason_contains" in exp:
            for i, words in enumerate(exp["reason_contains"]):
                reason = str((obs["claims"][i] if i < len(obs["claims"]) else {}).get("reason") or "")
                for w in words:
                    if w not in reason:
                        bad.append(f"第 {i + 1} 句 reason 缺 {w}：{reason[:80]}")
    elif case["kind"] == "explanation":
        want = exp.get("has_explanation") or {}
        got = {u["label"]: u["has_explanation"] for u in obs["unproven"]}
        for label, expect_v in want.items():
            if label not in got:
                bad.append(f"unproven 缺 {label}")
            elif bool(got[label]) != bool(expect_v):
                bad.append(f"{label} has_explanation 期望 {expect_v} 实得 {got[label]}")
    elif case["kind"] == "located":
        for key in ("located", "snippet_hints"):
            if key in exp and obs.get(key) != exp[key]:
                bad.append(f"{key} 期望 {exp[key]} 实得 {obs.get(key)}")
        for label in (exp.get("missing_labels_contains") or []):
            if label not in (obs.get("missing_labels") or []):
                bad.append(f"missing_labels 缺 {label}：{obs.get('missing_labels')}")
    return bad


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    mode = (argv[0] if argv else "check").strip().lower()
    if mode not in ("before", "after", "check"):
        print("用法：python scripts/claims_d1_cases.py [before|after|check]", file=sys.stderr)
        return 2
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"mode": mode, "cases": []}
    failures: list[str] = []
    for case in CASES:
        obs = _observe(case)
        bad = _check_case(case, obs)
        payload["cases"].append({"id": case["id"], "note": case["note"],
                                 "expect": case.get("expect"), "observed": obs,
                                 "ok": not bad, "mismatch": bad})
        if bad:
            failures.extend(f"[{case['id']}] {b}" for b in bad)
    out = OUT_DIR / f"d1_counterexamples_{mode}.json"
    if mode != "check":                      # check 只重放比对，不落新文件
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"写入 {out}：{len(CASES)} 个用例，"
              f"{sum(1 for c in payload['cases'] if c['ok'])} 通过")
    else:
        print(f"重放 {len(CASES)} 个用例：{sum(1 for c in payload['cases'] if c['ok'])} 通过")
    if failures:
        for f in failures:
            print("  未达期望：" + f)
        return 1 if mode in ("after", "check") else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
