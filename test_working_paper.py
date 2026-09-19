# -*- coding: utf-8 -*-
"""S1-3 退出关卡：一公司两年度 × 三核心指标 × 可重算底稿。

基准输入**冻结**在本文件（合成夹具，非现实公司事实），断言按架构指令的退出关卡：
- 六个组合齐备、同比可从底稿重算（数值自己算，不采信报告）；
- 指定缺失项如实缺失（记缺口，不推断）；
- **错主体 / 错期 / 错币种 / 累计与单季混用不得被验证通过**；
- 缺证据（无来源位置）不得算已核验；
- **正确输入不得因无关的同值记录失败**（判定只看必需组合自身的行）。
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import facts as F  # noqa: E402
import workspace as ws_mod  # noqa: E402
import working_paper as W  # noqa: E402

# ── 冻结基准输入（合成；两年度 × 三核心指标）────────────────
FIXTURE_COMPANY = "示例制造股份有限公司"
FIXTURE_CODE = "000001.SZ"
BASELINE_PAYLOAD = {
    "financials": [
        {"year": 2023, "report_type": "年报", "caliber": "合并",
         "report_date": "2024-04-20",          # 披露日：截至日证据（A′4）
         "revenue": 1200.0, "net_profit": 150.0, "operating_cashflow": 210.0},
        {"year": 2024, "report_type": "年报", "caliber": "合并",
         "report_date": "2025-04-20",
         "revenue": 1380.0, "net_profit": 174.0, "operating_cashflow": 231.0},
    ],
    "metadata": {"source": "eastmoney", "company": FIXTURE_COMPANY,
                 "code": FIXTURE_CODE, "currency": "CNY", "unit": "亿元"},
    "raw": {"url": "https://example.invalid/financials/000001.json",
            "text": '{"data": [{"TOTALOPERATEREVE": 138000000000}]}'},
}


def _request(**over) -> F.ResearchRequest:
    req = F.parse_research_request(
        "研究示例制造股份有限公司 2023 与 2024 两个年度的营业收入、归母净利润、"
        "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
        company=FIXTURE_COMPANY, company_id=FIXTURE_CODE, market="cn",
        caliber="合并", as_of="2025-04-30")
    for k, v in over.items():
        setattr(req, k, v)
    req.gaps = [g for g in req.gaps]
    return req


def _paper(payload=None, **over) -> W.WorkingPaper:
    data = payload if payload is not None else BASELINE_PAYLOAD
    return W.build_working_paper(F.facts_from_financials(data), _request(**over))


class TestBaselineClosesLoop(unittest.TestCase):
    """基准输入：闭环成立——六个组合齐备、同比可重算、无缺口无问题。"""

    def setUp(self):
        self.paper = _paper()

    def test_six_combos_present(self):
        # 材料里标记"已核实"才无问题？不——基准输入也应保持 unverified（结构化源≠已核实），
        # 但那不是"问题"；这里断言的是组合齐备与无缺口。
        self.assertEqual(self.paper.completeness["present"], 6,
                         self.paper.completeness)
        self.assertEqual(self.paper.completeness["missing"], [])
        self.assertEqual([g for g in self.paper.gaps if g["kind"] == "fact"], [])

    def test_yoy_recomputable_from_paper(self):
        yoy = {d["metric"]: d for d in self.paper.derived}
        self.assertIn("revenue_yoy", yoy)
        d = yoy["revenue_yoy"]
        # 自己算一遍： (1380 - 1200) / 1200 * 100 = 15.0
        self.assertAlmostEqual(d["value"], 15.0, places=2)
        self.assertIn("1380", d["formula"])
        self.assertEqual(len(d["derived_from"]), 2, "同比必须带两个输入 fact_id")
        # 三条同比 + 该夹具能算出的同年比率（只有三核心指标 → 净利率、现金流覆盖）
        self.assertEqual(set(d["metric"] for d in self.paper.derived),
                         {"revenue_yoy", "net_profit_yoy", "operating_cashflow_yoy",
                          "net_margin", "cashflow_coverage"})

    def test_derived_values_match_hand_computation(self):
        """派生值必须能手算复现（同比 + 同年比率都算在内）。"""
        want = {
            ("revenue_yoy", "2024年同比"): (1380.0 - 1200.0) / 1200.0 * 100,
            ("net_profit_yoy", "2024年同比"): (174.0 - 150.0) / 150.0 * 100,
            ("operating_cashflow_yoy", "2024年同比"):
                (231.0 - 210.0) / 210.0 * 100,
            ("net_margin", "2023年"): 150.0 / 1200.0 * 100,
            ("net_margin", "2024年"): 174.0 / 1380.0 * 100,
            ("cashflow_coverage", "2023年"): 210.0 / 150.0 * 100,
            ("cashflow_coverage", "2024年"): 231.0 / 174.0 * 100,
        }
        seen = set()
        for d in self.paper.derived:
            key = (d["metric"], d["period"])
            if key not in want:
                self.fail(f"出现计划外的派生指标：{key}")
            self.assertAlmostEqual(d["value"], round(want[key], 2), places=2,
                                   msg=f"{key} 可从底稿重算")
            seen.add(key)
        self.assertEqual(seen, set(want), "该夹具应有的派生指标都要在")

    def test_rows_carry_source_position_and_currency(self):
        for r in self.paper.rows:
            self.assertEqual(r["currency"], "CNY")
            self.assertEqual(r["unit"], "亿元")
            self.assertTrue(r["source_url"])
            self.assertTrue(r["source_locator"], "每条事实要有来源位置")


class TestMissingStaysMissing(unittest.TestCase):
    def test_missing_metric_is_reported_not_inferred(self):
        payload = {"financials": [
            {"year": 2023, "report_type": "年报", "caliber": "合并",
             "revenue": 1200.0, "net_profit": 150.0},
            {"year": 2024, "report_type": "年报", "caliber": "合并",
             "revenue": 1380.0, "net_profit": 174.0},
        ], "metadata": BASELINE_PAYLOAD["metadata"], "raw": BASELINE_PAYLOAD["raw"]}
        paper = _paper(payload)
        self.assertFalse(paper.ok)
        missing = [g for g in paper.gaps if g["kind"] == "fact"]
        self.assertEqual(len(missing), 2, [g["detail"] for g in missing])
        self.assertTrue(all("经营现金流" in g["detail"] or "经营活动现金流" in g["detail"]
                            for g in missing), [g["detail"] for g in missing])
        self.assertFalse(any(d["metric"].startswith("operating_cashflow")
                             for d in paper.derived),
                         "缺一期就不许编同比")


class TestNeverVerifiedNegatives(unittest.TestCase):
    """错主体/错期/错币种/累计混用/无来源 —— 一律不得算已核验。"""

    def _with_row(self, **row_over) -> W.WorkingPaper:
        rows = [dict(r) for r in BASELINE_PAYLOAD["financials"]]
        rows[1].update(row_over)
        return _paper({"financials": rows,
                       "metadata": BASELINE_PAYLOAD["metadata"],
                       "raw": BASELINE_PAYLOAD["raw"]})

    def test_wrong_subject_fails(self):
        """事实主体与请求主体不是一家 → 不得通过；取错公司只能产生缺口。

        A 批起判定更精确：这类记录先被主体筛选**排除**（`PROBLEM_UNRELATED`，列出 fact_id），
        因此不会出现在必需组合里，也不会参与同比；旧断言只认 `PROBLEM_SUBJECT`。
        """
        rows = [dict(r) for r in BASELINE_PAYLOAD["financials"]]
        payload = {"financials": rows,
                   "metadata": dict(BASELINE_PAYLOAD["metadata"],
                                    company="另一家公司", code="000002.SZ"),
                   "raw": BASELINE_PAYLOAD["raw"]}
        paper = _paper(payload)
        self.assertTrue(any(a.get("kind") == W.PROBLEM_UNRELATED for a in paper.audit),
                        paper.audit)
        self.assertFalse(paper.ok)
        # 取错公司就是"必需事实没拿到"：缺口照记（不是把别家公司的数当自己的）
        missing = [g for g in paper.gaps if g["kind"] == "fact"]
        self.assertEqual(len(missing), 6, [g["detail"] for g in missing])
        self.assertFalse(paper.rows, "别家公司的事实不进本公司明细")
        self.assertFalse(paper.derived, "更不得参与任何计算")

    def test_mixed_currency_across_years_fails(self):
        """两期币种不同（行级声明）→ 不得直接算同比，也不得算已核验。"""
        rows = [dict(r) for r in BASELINE_PAYLOAD["financials"]]
        rows[1].update({"currency": "USD", "unit": "亿美元"})
        paper = _paper({"financials": rows,
                        "metadata": BASELINE_PAYLOAD["metadata"],
                        "raw": BASELINE_PAYLOAD["raw"]})
        kinds = {p.kind for p in paper.problems}
        self.assertIn(W.PROBLEM_CURRENCY, kinds, [p.detail for p in paper.problems])
        self.assertFalse(any(d["metric"] == "revenue_yoy" for d in paper.derived),
                         "币种不一致时不得给出同比")
        self.assertFalse(paper.ok)

    def test_unknown_unit_fails(self):
        """来源没声明单位 → 不得算已核验（口径不明）。"""
        payload = {"financials": [{"year": 2023, "report_type": "年报", "caliber": "合并",
                                   "revenue": 1200.0},
                                  {"year": 2024, "report_type": "年报", "caliber": "合并",
                                   "revenue": 1380.0}],
                   "metadata": {"company": FIXTURE_COMPANY, "code": FIXTURE_CODE,
                                "currency": "CNY"},
                   "raw": BASELINE_PAYLOAD["raw"]}
        paper = _paper(payload)
        self.assertIn(W.PROBLEM_CURRENCY, {p.kind for p in paper.problems},
                      [p.detail for p in paper.problems])

    def test_extra_quarterly_row_is_not_used_in_yoy(self):
        """同期多一条三季报（累计口径）时：它不参与年报同比，也不影响判定。"""
        rows = [dict(r) for r in BASELINE_PAYLOAD["financials"]]
        rows.append({"year": 2024, "report_type": "三季报", "caliber": "合并",
                     "revenue": 1000.0, "net_profit": 120.0})
        paper = _paper({"financials": rows,
                        "metadata": BASELINE_PAYLOAD["metadata"],
                        "raw": BASELINE_PAYLOAD["raw"]})
        self.assertFalse(any("三季报" in str(d["period"]) for d in paper.derived),
                         "季报不得混进年报同比")
        rev = [d for d in paper.derived if d["metric"] == "revenue_yoy"][0]
        inputs = set(rev.get("derived_from") or [])
        quarterly = [r["fact_id"] for r in paper.rows
                     if "三季报" in str(r.get("period"))]
        self.assertTrue(quarterly, "夹具里应有三季报那一行")
        self.assertFalse(inputs & set(quarterly),
                         "同比的输入必须是年报行，不能是季报行")

    def test_quarterly_only_cannot_satisfy_annual_requirement(self):
        """某年只有三季报、没有年报 → 不得当年度口径用（累计 vs 单季不可比）。"""
        rows = [r for r in BASELINE_PAYLOAD["financials"]
                if not (r["year"] == 2024)]           # 拿掉 2024 年报
        rows.append({"year": 2024, "report_type": "三季报", "caliber": "合并",
                     "revenue": 1000.0, "net_profit": 120.0,
                     "operating_cashflow": 180.0})
        paper = _paper({"financials": rows,
                        "metadata": BASELINE_PAYLOAD["metadata"],
                        "raw": BASELINE_PAYLOAD["raw"]})
        kinds = {p.kind for p in paper.problems}
        self.assertIn(W.PROBLEM_CUMULATIVE, kinds, [p.detail for p in paper.problems])
        self.assertFalse(paper.ok, "只有季报时不得算目标达成")
        self.assertFalse(any(str(d["period"]).startswith("2024")
                             for d in paper.derived),
                         "缺 2024 年报时不得算 2024 同比")

    def test_missing_source_url_is_flagged(self):
        paper = _paper({"financials": [{"year": 2024, "report_type": "年报",
                                        "caliber": "合并", "revenue": 1380.0}],
                        "metadata": BASELINE_PAYLOAD["metadata"], "raw": {}})
        self.assertIn(W.PROBLEM_UNVERIFIED, {p.kind for p in paper.problems})


class TestIrrelevantSameValueDoesNotBreakCorrectInput(unittest.TestCase):
    """正确输入不得因无关的同值记录失败（判定只看必需组合自身的行）。"""

    def test_unrelated_same_value_record_is_ignored(self):
        payload = dict(BASELINE_PAYLOAD)
        payload["raw"] = dict(BASELINE_PAYLOAD["raw"])
        # 在原文里塞一条**另一家公司**的同值记录（不影响结构化事实）
        payload["raw"]["text"] = (
            '{"data": [{"TOTALOPERATEREVE": 138000000000}],'
            ' "note": "另一家公司2024年营业收入 1380 亿元"}')
        paper = _paper(payload)
        self.assertEqual(paper.problems, [], [p.detail for p in paper.problems])
        self.assertTrue(paper.completeness["present"] == 6)


class TestPaperExport(unittest.TestCase):
    def test_csv_has_three_sections_and_is_parseable(self):
        text = W.paper_csv(_paper())
        rows = list(csv.reader(io.StringIO(text)))
        heads = [r[0] for r in rows if r]
        self.assertIn("# 明细（可重算底稿）", heads)
        self.assertIn("# 派生值（公式 + 输入）", heads)
        self.assertIn("# 缺口与问题", heads)
        body = [r for r in rows if r and r[0].startswith("fact-")]
        self.assertGreaterEqual(len(body), 6 + 3, "明细 6 行 + 同比 3 行")
        self.assertTrue(any("公式" in r[0] or "formula" in r[0] for r in rows if r),
                        "派生段应带公式列")


# ── 真实数据落盘快照（2026-09-17 东财公开接口实抓，冻结为夹具）──
# 这是**真实抓取的快照**（非合成），用于在 CI 里回归"真实数据形状"下的链路；
# 数值取自东财 datacenter 公开接口的当年年报，抓取时间与 URL 见
# docs/evidence/real_data_chain_20260917.md。真实网络抓取本身不进 CI（网络不可控）。
REAL_SNAPSHOT = {
    "financials": [
        {"year": 2023, "report_type": "年报", "report_date": "2024-04-02",
         "revenue": 1505.6, "net_profit": 747.34, "operating_cashflow": 665.93},
        {"year": 2024, "report_type": "年报", "report_date": "2025-04-02",
         "revenue": 1741.44, "net_profit": 862.28, "operating_cashflow": 924.64},
    ],
    "metadata": {"source": "eastmoney_ashare", "company": "贵州茅台",
                 "stock_code": "600519", "currency": "CNY", "unit": "亿元",
                 "period": "年报", "latest_report": "2025-12-31"},
    "raw": {"url": "https://datacenter-web.eastmoney.com/api/data/v1/get（实抓，见证据文件）",
            "text": "[real snapshot trimmed]"},
}


class TestRealSnapshotChain(unittest.TestCase):
    """真实抓取快照：链路在真实数据形状下同样成立，且数值可重算。"""

    def setUp(self):
        # 契约要用**快手**的公司（复用合成夹具的请求会被主体校验正确拦下）
        req = F.parse_research_request(
            "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            company="贵州茅台", company_id="600519", market="cn",
            caliber="合并", as_of="2025-04-30")
        self.facts = F.facts_from_financials(REAL_SNAPSHOT)
        self.paper = W.build_working_paper(self.facts, req)

    def test_subject_identifier_and_units_from_real_payload(self):
        self.assertTrue(self.facts)
        for f in self.facts:
            self.assertEqual(f.entity, "贵州茅台")
            self.assertEqual(f.entity_id, "600519", "稳定标识取自适配器 metadata")
            self.assertEqual(f.currency, "CNY")
            self.assertEqual(f.unit, "亿元")
            self.assertEqual(f.period_type, "年报")

    def test_unusable_caliber_rows_are_pending_not_certified(self):
        """口径未知的行进"待核验"：**只展示，不参与达标与同比**（A′2 政策）。

        真实快照的行没有声明口径 → 六条必需组合都拿不到认证输入 ⇒ 不达标；
        数值本身仍可从明细核对（可重算的链路在"行内声明口径"的正例里验证）。
        """
        self.assertEqual(self.paper.completeness["present"], 0, self.paper.completeness)
        self.assertFalse(self.paper.ok)
        self.assertFalse(self.paper.derived, "待核验输入不得生成同比")
        pending = self.paper.selection.get("pending") or []
        self.assertEqual(len(pending), 6, self.paper.selection)
        self.assertTrue(any("口径" in a.get("detail", "") for a in self.paper.audit),
                        self.paper.audit)

    def test_unknown_caliber_does_not_pass(self):
        """快照的行没有声明口径 → **不得达标**（A 批接受项，A′ 保持）。

        判定路径按 A′2 调整：口径不可用的行进 `selection.pending` + 审计提示，
        不达标来自"必需组合拿不到认证输入"，而不是把本公司记录当成"问题"。
        """
        self.assertFalse(self.paper.ok, "口径未知的必需事实不得算目标达成")
        self.assertTrue(self.paper.selection.get("pending"), self.paper.selection)


class TestBatchAContractAndCertification(unittest.TestCase):
    """A 批反例（架构复核 2026-09-18）：契约、选择与认证门槛。

    这些都是**当前生产函数**的内存反例，不是实机结论：
    - 未知口径不得达标；
    - 校验与计算必须共用同一份"已选择事实"，跨公司输入不得参与本公司计算；
    - 候选顺序不得决定结果；重复冲突候选要列出来；
    - 非相邻期不做同比、零分母显式不可算；
    - 同比单位是 %，输入保留金额单位。
    """

    # 另一家公司的记录（用于"跨公司不得污染"）：整条实体形如真实载荷
    BYD_ENTITY = {
        "metadata": {"source": "eastmoney_ashare", "company": "比亚迪",
                     "stock_code": "002594", "currency": "CNY", "unit": "亿元"},
        "financials": [{"year": 2024, "report_type": "年报", "caliber": "合并",
                        "revenue": 9999.0}],
        "raw": {"url": "https://example.invalid/financials/002594.json",
                "text": '{"data": [{"TOTALOPERATEREVE": 999900000000}]}'},
    }

    def _maotai_entity(self):
        # 与真实快照同形状，但**行内声明口径**（已知口径正例的来源）
        rows = [dict(r, caliber="合并")
                for r in REAL_SNAPSHOT["financials"]]
        return {"metadata": dict(REAL_SNAPSHOT["metadata"]), "financials": rows,
                "raw": dict(REAL_SNAPSHOT["raw"])}

    def _request(self, **over) -> F.ResearchRequest:
        req = F.parse_research_request(
            "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            company="贵州茅台", company_id="600519", market="cn",
            caliber="合并", as_of="2025-04-30")
        for k, v in over.items():
            setattr(req, k, v)
        return req

    def _paper(self, entities, **over) -> W.WorkingPaper:
        payload = {"companies": entities} if len(entities) > 1 else {
            "metadata": entities[0]["metadata"],
            "financials": entities[0]["financials"],
            "raw": entities[0]["raw"],
        }
        return W.build_working_paper(F.facts_from_financials(payload),
                                     self._request(**over))

    def test_known_caliber_positive_still_closes(self):
        """已知口径（行内声明 = 请求口径）的正例：六组合齐备、三个同比可复算、达标。"""
        paper = self._paper([self._maotai_entity()])
        self.assertEqual(paper.completeness["present"], 6, paper.completeness)
        self.assertEqual([g for g in paper.gaps if g["kind"] == "fact"], [])
        self.assertEqual(paper.problems, [], [p.detail for p in paper.problems])
        self.assertTrue(paper.ok)
        yoy = {d["metric"]: d["value"] for d in paper.derived}
        self.assertAlmostEqual(yoy["revenue_yoy"],
                               round((1741.44 - 1505.6) / 1505.6 * 100, 2), places=2)

    def test_cross_company_record_does_not_pollute_or_pass(self):
        """追加"比亚迪 2024 营收 9999 亿元"：不得参与本公司计算，也不得悄悄通过。

        复现（架构复核）：此前底稿仍通过，且**茅台同比变成 564.12%**
        （(9999-1505.6)/1505.6×100）——校验看第一条（茅台）、计算看最后一条（比亚迪）。
        """
        paper = self._paper([self._maotai_entity(), self.BYD_ENTITY])
        rev = {d["metric"]: d for d in paper.derived}
        self.assertIn("revenue_yoy", rev)
        self.assertAlmostEqual(rev["revenue_yoy"]["value"],
                               round((1741.44 - 1505.6) / 1505.6 * 100, 2), places=2,
                               msg="本公司同比不得被别家公司记录改写")
        self.assertNotAlmostEqual(rev["revenue_yoy"]["value"], 564.12, places=2)
        byd = [u for u in (paper.selection.get("unrelated") or [])
               if u.get("entity") == "比亚迪"]
        self.assertTrue(byd, "别家公司的记录要出现在 selection.unrelated 里（可审计）")
        self.assertFalse(any(r.get("entity") == "比亚迪" for r in paper.rows),
                         "别家公司的记录不进本公司明细")
        self.assertFalse(set(rev["revenue_yoy"]["derived_from"]) & {u["fact_id"] for u in byd},
                         "跨公司记录不得作为同比输入")
        # A′2：无关记录只作**审计提示**，不否决本公司的正确结果
        self.assertTrue(any(a.get("kind") == W.PROBLEM_UNRELATED
                            for a in paper.audit), paper.audit)
        self.assertEqual([p for p in paper.problems if p.kind == W.PROBLEM_UNRELATED], [],
                         "无关记录不得进 problems（那会让 paper.ok 从 true 变 false）")
        self.assertTrue(paper.ok, [p.detail for p in paper.problems])
        self.assertEqual(paper.completeness["present"], 6, paper.completeness)

    def test_candidate_order_does_not_change_outcome(self):
        """候选换序（别家公司在前/在后）结果必须一致——不得由列表先后决定。"""
        first = self._paper([self.BYD_ENTITY, self._maotai_entity()])
        last = self._paper([self._maotai_entity(), self.BYD_ENTITY])
        pick = lambda p: sorted(  # noqa: E731
            (d["metric"], d["value"]) for d in p.derived)
        self.assertEqual(pick(first), pick(last))
        self.assertEqual(first.completeness, last.completeness)
        self.assertEqual(sorted(p.kind for p in first.problems),
                         sorted(p.kind for p in last.problems))

    def test_conflicting_duplicate_candidates_are_listed(self):
        """同一（指标, 期间）两条同主体不同值 → 记冲突并**列出全部候选**，不先到先得。"""
        ent = self._maotai_entity()
        ent["financials"] = [dict(r) for r in ent["financials"]]
        ent["financials"].append({"year": 2024, "report_type": "年报", "caliber": "合并",
                                  "revenue": 1800.0})
        payload = {"financials": ent["financials"], "metadata": ent["metadata"],
                   "raw": ent["raw"]}
        facts = F.facts_from_financials(payload)
        cands = [f for f in facts if f.metric == "revenue" and f.period == "2024年"]
        self.assertEqual(len(cands), 2, "夹具里应有两条 2024 营收候选")
        self.assertEqual({str(f.value) for f in cands}, {"1741.44", "1800.0"})
        paper = W.build_working_paper(facts, self._request())
        kinds = {p.kind for p in paper.problems}
        self.assertIn(W.PROBLEM_CONFLICT, kinds, [p.detail for p in paper.problems])
        conflicts = paper.selection.get("conflicts") or []
        self.assertEqual(len(conflicts), 1, paper.selection)
        listed = {str(c.get("value")) for c in (conflicts[0].get("candidates") or [])}
        self.assertEqual(listed, {"1741.44", "1800.0"},
                         "冲突候选要连**值**一起列出（fact_id 与值无关，只给 id 分不出两条）")
        detail = " ".join(p.detail for p in paper.problems if p.kind == W.PROBLEM_CONFLICT)
        self.assertIn("1741.44", detail)
        self.assertIn("1800.0", detail)
        self.assertFalse(any(d["metric"] == "revenue_yoy" for d in paper.derived),
                         "输入冲突时不得给出同比")
        self.assertFalse(paper.ok)

    def test_non_adjacent_years_are_not_labelled_yoy(self):
        """请求 2022 与 2024：跨年不是同比，不得标成同比。"""
        ent = self._maotai_entity()
        ent["financials"] = [
            {"year": 2022, "report_type": "年报", "caliber": "合并",
             "revenue": 1275.5, "net_profit": 627.2, "operating_cashflow": 366.9},
            {"year": 2024, "report_type": "年报", "caliber": "合并",
             "revenue": 1741.44, "net_profit": 862.28, "operating_cashflow": 924.64},
        ]
        paper = self._paper([ent], periods=[2022, 2024])
        self.assertFalse(any("同比" in str(d.get("period")) for d in paper.derived),
                         "非相邻期不得产出同比行")
        self.assertIn(W.PROBLEM_PERIOD, {p.kind for p in paper.problems},
                      [p.detail for p in paper.problems])
        self.assertFalse(paper.ok)

    def test_zero_denominator_is_explicitly_not_computable(self):
        """分母为 0 → 明确记"不可算"，不是静默跳过。"""
        ent = self._maotai_entity()
        ent["financials"] = [
            {"year": 2023, "report_type": "年报", "caliber": "合并",
             "revenue": 0.0, "net_profit": 747.34, "operating_cashflow": 665.93},
            {"year": 2024, "report_type": "年报", "caliber": "合并",
             "revenue": 1741.44, "net_profit": 862.28, "operating_cashflow": 924.64},
        ]
        paper = self._paper([ent])
        self.assertFalse(any(d["metric"] == "revenue_yoy" for d in paper.derived))
        detail = " ".join(p.detail for p in paper.problems)
        self.assertIn("不可算", detail, [p.detail for p in paper.problems])
        self.assertFalse(paper.ok)

    def test_yoy_unit_is_percent_and_inputs_keep_amount_unit(self):
        """同比单位是 %（不是继承"亿元"）；输入行保留金额单位。"""
        paper = self._paper([self._maotai_entity()])
        yoy = [d for d in paper.derived if d["metric"] == "revenue_yoy"]
        self.assertEqual(yoy[0]["unit"], "%", yoy[0])
        self.assertIn("100", yoy[0]["formula"])
        amounts = [r for r in paper.rows if r["metric"] == "revenue"]
        self.assertTrue(amounts)
        for r in amounts:
            self.assertEqual(r["unit"], "亿元", "输入金额单位不得被派生值改写")

    def test_required_fact_without_source_location_fails(self):
        """必需事实没有来源位置（状态为 unknown 也一样）→ 不得算已核验。"""
        ent = self._maotai_entity()
        ent["raw"] = {}
        paper = self._paper([ent])
        self.assertIn(W.PROBLEM_UNVERIFIED, {p.kind for p in paper.problems},
                      [p.detail for p in paper.problems])
        self.assertFalse(paper.ok)


class TestBatchAPrimeMatrix(unittest.TestCase):
    """A′ 复核的四组反例矩阵（架构指令 2026-09-18 下午）。"""

    def _req(self, **over) -> F.ResearchRequest:
        base = dict(company="贵州茅台", company_id="600519.SH", market="cn",
                    caliber="合并", as_of="2025-04-30")
        base.update(over)
        return F.parse_research_request(
            "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            periods=[2023, 2024], identity_source="form", **base)

    def _maotai(self, **row_over):
        rows = [dict(r, caliber="合并", report_date="2024-04-02" if r["year"] == 2023
                     else "2025-04-02") for r in REAL_SNAPSHOT["financials"]]
        for r in rows:
            r.update(row_over)
        return {"metadata": dict(REAL_SNAPSHOT["metadata"]), "financials": rows,
                "raw": dict(REAL_SNAPSHOT["raw"])}

    # ── 1) 主体标识必须含市场/交易所 ──────────────────────────

    def test_same_digits_across_markets_is_not_the_same_subject(self):
        """`000001.SZ` 与 `00001.HK` 不是同一主体：跨市场同码不得当同一家。"""
        req = self._req(company="平安银行", company_id="000001.SZ", market="cn")
        ok, why = F.check_subject(req, "某港股公司", "00001.HK", "hk")
        self.assertFalse(ok, why)
        self.assertIn("市场", why)
        req_hk = self._req(company="腾讯控股", company_id="00700.HK", market="hk")
        self.assertTrue(F.check_subject(req_hk, "腾讯控股", "700.HK", "hk")[0],
                        "同市场内的前导零差异仍算同一家（有依据的别名归一）")

    def test_cross_market_candidate_does_not_complete_the_six(self):
        """跨市场候选混进来：本公司六项不得因此齐备。"""
        payload = {"companies": [
            {"metadata": {"company": "贵州茅台", "stock_code": "600519", "market": "cn",
                          "currency": "CNY", "unit": "亿元"},
             "financials": [], "raw": dict(REAL_SNAPSHOT["raw"])},
            {"metadata": {"company": "某港股公司", "stock_code": "00001.HK", "market": "hk",
                          "currency": "HKD", "unit": "亿元"},
             "financials": [dict(r, caliber="合并", report_date="2025-04-02")
                            for r in REAL_SNAPSHOT["financials"]],
             "raw": dict(REAL_SNAPSHOT["raw"])},
        ]}
        paper = W.build_working_paper(F.facts_from_financials(payload), self._req())
        self.assertEqual(paper.completeness["present"], 0, paper.completeness)
        self.assertFalse(paper.ok)
        self.assertFalse(paper.derived)

    def test_form_with_code_only_is_accepted(self):
        """表单只填 `600519.SH`（稳定标识留空）：契约要能识别出来，六项齐备。"""
        req = F.parse_research_request(
            "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            company="600519.SH", periods=[2023, 2024], caliber="合并",
            as_of="2025-04-30", identity_source="form")
        self.assertEqual(req.company_id, "600519.SH")
        self.assertEqual(req.market, "cn")
        self.assertFalse([g for g in req.gaps if "未识别到公司" in g], req.gaps)
        paper = W.build_working_paper(F.facts_from_financials(self._maotai()), req)
        self.assertEqual(paper.completeness["present"], 6, paper.completeness)
        self.assertTrue(paper.ok, [p.detail for p in paper.problems])

    def test_bare_ambiguous_code_asks_for_confirmation(self):
        """裸 1–5 位数字代码有市场歧义 → 留缺口请确认，不静默猜。"""
        req = F.parse_research_request("研究某公司 2023 与 2024", company="00700")
        self.assertTrue(any("裸代码" in g for g in req.gaps), req.gaps)

    # ── 2) 候选选择：顺序无关 + 无关记录只作审计 ──────────────

    def test_same_value_different_caliber_is_order_independent(self):
        """同值、不同口径（合并/母公司）两种排列结果一致；母公司不得进认证与同比。"""
        def paper_with(calibers):
            rows = []
            for year, d in ((2023, "2024-04-02"), (2024, "2025-04-02")):
                for c in calibers:
                    rows.append({"year": year, "report_type": "年报", "caliber": c,
                                 "report_date": d,
                                 "revenue": 1505.6 if year == 2023 else 1741.44,
                                 "net_profit": 747.34 if year == 2023 else 862.28,
                                 "operating_cashflow": 665.93 if year == 2023 else 924.64})
            payload = {"financials": rows, "metadata": dict(REAL_SNAPSHOT["metadata"]),
                       "raw": dict(REAL_SNAPSHOT["raw"])}
            return W.build_working_paper(F.facts_from_financials(payload), self._req())

        a = paper_with(["合并", "母公司"])
        b = paper_with(["母公司", "合并"])
        self.assertEqual(a.ok, b.ok, (a.ok, b.ok))
        self.assertEqual([(d["metric"], d["value"]) for d in a.derived],
                         [(d["metric"], d["value"]) for d in b.derived])
        self.assertTrue(a.ok, [p.detail for p in a.problems])
        pending = {p["fact_id"] for p in (a.selection.get("pending") or [])}
        self.assertTrue(pending, "母公司候选应进待核验")
        used = set().union(*[set(d["derived_from"]) for d in a.derived]) if a.derived else set()
        self.assertFalse(used & pending, "待核验候选不得作为同比输入")

    def test_same_caliber_different_value_conflicts_same_value_agrees(self):
        """同口径异值 → 冲突；同口径同值异来源 → 视为一致（来源全部记录在案）。"""
        rows = [dict(r, caliber="合并", report_date="2024-04-02" if r["year"] == 2023
                     else "2025-04-02") for r in REAL_SNAPSHOT["financials"]]
        conflict = {"financials": rows + [{"year": 2024, "report_type": "年报",
                                          "caliber": "合并", "report_date": "2025-04-02",
                                          "revenue": 9999.0}],
                    "metadata": dict(REAL_SNAPSHOT["metadata"]),
                    "raw": dict(REAL_SNAPSHOT["raw"])}
        paper = W.build_working_paper(F.facts_from_financials(conflict), self._req())
        self.assertIn(W.PROBLEM_CONFLICT, {p.kind for p in paper.problems})
        self.assertFalse(any(d["metric"] == "revenue_yoy" for d in paper.derived))

        agree_rows = [dict(r) for r in rows]
        agree_rows.append(dict(rows[1]))          # 同一值、第二个来源
        agree_rows[-1]["report_date"] = "2025-04-03"
        agree = {"financials": agree_rows, "metadata": dict(REAL_SNAPSHOT["metadata"]),
                 "raw": dict(REAL_SNAPSHOT["raw"])}
        paper2 = W.build_working_paper(F.facts_from_financials(agree), self._req())
        self.assertTrue(paper2.ok, [p.detail for p in paper2.problems])
        rev = [r for r in paper2.rows if r["metric"] == "revenue" and r["period"] == "2024年"]
        self.assertTrue(rev)
        self.assertGreaterEqual((rev[0].get("source_locator") or {}).get("agreeing_count", 0),
                                2, "一致来源要记在案（可审计）")

    def test_unrelated_extra_keeps_correct_six_ok(self):
        """追加已排除的无关公司：本公司六项与三个同比**不受影响**（A′2 明确要求）。"""
        payload = {"companies": [
            {"metadata": dict(REAL_SNAPSHOT["metadata"], company="贵州茅台", market="cn"),
             "financials": [dict(r, caliber="合并",
                                 report_date="2024-04-02" if r["year"] == 2023
                                 else "2025-04-02")
                            for r in REAL_SNAPSHOT["financials"]],
             "raw": dict(REAL_SNAPSHOT["raw"])},
            {"metadata": {"company": "比亚迪", "stock_code": "002594", "market": "cn",
                          "currency": "CNY", "unit": "亿元"},
             "financials": [{"year": 2024, "report_type": "年报", "caliber": "合并",
                             "report_date": "2025-04-02", "revenue": 9999.0}],
             "raw": {"url": "https://example.invalid/byd", "text": "{}"}},
        ]}
        paper = W.build_working_paper(F.facts_from_financials(payload), self._req())
        self.assertEqual(paper.completeness["present"], 6, paper.completeness)
        self.assertTrue(paper.ok, [p.detail for p in paper.problems])
        yoy = {d["metric"]: d["value"] for d in paper.derived}
        self.assertAlmostEqual(yoy["revenue_yoy"],
                               round((1741.44 - 1505.6) / 1505.6 * 100, 2), places=2)
        self.assertTrue(any(a.get("kind") == W.PROBLEM_UNRELATED for a in paper.audit))

    # ── 4) 截至日成为证据约束 ────────────────────────────────

    def test_as_of_before_period_end_fails(self):
        """2023/2024 的事实配截至 2022-01-01：该时点年报还没披露 → 不达标。"""
        paper = W.build_working_paper(F.facts_from_financials(self._maotai()),
                                      self._req(as_of="2022-01-01"))
        self.assertFalse(paper.ok)
        self.assertTrue(any("早于研究期末" in g["detail"] for g in paper.gaps), paper.gaps)

    def test_disclosure_after_as_of_fails(self):
        """披露日晚于截至日 → 时点不成立。"""
        paper = W.build_working_paper(F.facts_from_financials(self._maotai()),
                                      self._req(as_of="2025-03-01"))
        self.assertIn(W.PROBLEM_AS_OF, {p.kind for p in paper.problems})
        self.assertFalse(paper.ok)

    def test_missing_disclosure_date_is_unverified(self):
        """没有披露/可用日期 → 标"时点未核实"，不得宣称目标达成。"""
        payload = self._maotai()
        for r in payload["financials"]:
            r.pop("report_date", None)
        paper = W.build_working_paper(F.facts_from_financials(payload), self._req())
        self.assertIn(W.PROBLEM_AS_OF, {p.kind for p in paper.problems})
        self.assertFalse(paper.ok)

    def test_sourced_dates_make_as_of_satisfiable(self):
        """有出处的披露日期且不晚于截至日 → 截至日成立（正例）。"""
        paper = W.build_working_paper(F.facts_from_financials(self._maotai()), self._req())
        self.assertEqual([p for p in paper.problems if p.kind == W.PROBLEM_AS_OF], [])
        self.assertTrue(paper.ok, [p.detail for p in paper.problems])

    def test_invalid_as_of_is_a_contract_gap(self):
        req = F.parse_research_request("研究贵州茅台 2023 与 2024", company="600519.SH",
                                       as_of="昨天")
        self.assertTrue(any("不是有效日期" in g for g in req.gaps), req.gaps)


class TestDocumentSubjectScopeMatrix(unittest.TestCase):
    """A′3 的四条漏检场景（架构指令表格），逐条对号。"""

    def setUp(self):
        rows = [dict(r, caliber="合并", report_date="2024-04-02" if r["year"] == 2023
                     else "2025-04-02") for r in REAL_SNAPSHOT["financials"]]
        self.req = F.parse_research_request(
            "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            company="贵州茅台", company_id="600519.SH", market="cn",
            periods=[2023, 2024], caliber="合并", as_of="2025-04-30")
        payload = {"financials": rows, "metadata": dict(REAL_SNAPSHOT["metadata"]),
                   "raw": dict(REAL_SNAPSHOT["raw"])}
        self.paper = W.build_working_paper(F.facts_from_financials(payload), self.req)

    def _scope(self, text: str):
        return W.document_subject_scope(text, self.req, self.paper)

    def test_other_company_section_is_flagged_plain_number(self):
        problems = self._scope("# 贵州茅台\n\n## 比亚迪\n\n营业收入 1741.44 亿元\n")
        self.assertTrue(problems, "错公司小节的数值必须被拦")
        self.assertIn(W.PROBLEM_DOC_SCOPE, {p.kind for p in problems})

    def test_comma_format_same_conclusion(self):
        plain = self._scope("# 贵州茅台\n\n## 比亚迪\n\n营业收入 1741.44 亿元\n")
        comma = self._scope("# 贵州茅台\n\n## 比亚迪\n\n营业收入 1,741.44 亿元\n")
        self.assertEqual(bool(plain), bool(comma), (plain, comma))
        self.assertTrue(comma)

    def test_neutral_subheading_inherits_scope(self):
        self.assertEqual(self._scope("# 贵州茅台\n\n## 财务指标\n\n营业收入 1741.44 亿元\n"),
                         [], "中性子标题应继承公司作用域")

    def test_correct_occurrence_does_not_cancel_wrong_one(self):
        text = ("# 贵州茅台\n\n营业收入 1741.44 亿元\n\n"
                "## 比亚迪\n\n营业收入 1741.44 亿元\n")
        self.assertTrue(self._scope(text), "正确出现不能抵消错误出现")

    def test_non_core_metric_number_is_ignored(self):
        """别的指标的数字不该被算进本指标的判定（指标/期间/单位随事实绑定）。"""
        self.assertEqual(self._scope("# 贵州茅台\n\n## 比亚迪\n\n毛利率 41.2 %\n"), [],
                         "不属于核心三指标的数值不参与判定")


class TestDocumentSubjectScope(unittest.TestCase):
    """文档级主体作用域：标题/表格作用域必须绑定请求主体（本批只覆盖三项核心指标）。"""

    def setUp(self):
        rows = [dict(r, caliber="合并") for r in REAL_SNAPSHOT["financials"]]
        self.req = F.parse_research_request(
            "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            company="贵州茅台", company_id="600519", market="cn",
            caliber="合并", as_of="2025-04-30")
        payload = {"financials": rows,
                   "metadata": dict(REAL_SNAPSHOT["metadata"]),
                   "raw": dict(REAL_SNAPSHOT["raw"])}
        self.paper = W.build_working_paper(F.facts_from_financials(payload), self.req)

    def _scope(self, text: str):
        return W.document_subject_scope(text, self.req, self.paper)

    def test_scoped_report_passes(self):
        text = ("# 贵州茅台（600519）2023–2024 年度研究\n\n"
                "| 指标 | 2023 | 2024 |\n|---|---|---|\n"
                "| 营业收入（亿元） | 1505.6 | 1741.44 |\n")
        self.assertEqual(self._scope(text), [])

    def test_unscoped_row_under_other_company_title_fails(self):
        """A 公司标题 + 行内无公司名 + 同值来源属另一家公司 → 不得认证。"""
        text = ("# 比亚迪 2023–2024 年度研究\n\n"
                "| 指标 | 2023 | 2024 |\n|---|---|---|\n"
                "| 营业收入（亿元） | 1505.6 | 1741.44 |\n")
        problems = self._scope(text)
        self.assertTrue(problems, "标题是别家公司时不得算已绑定主体")
        self.assertIn(W.PROBLEM_DOC_SCOPE, {p.kind for p in problems})

    def test_missing_title_scope_is_reported(self):
        text = ("| 指标 | 2023 | 2024 |\n|---|---|---|\n"
                "| 营业收入（亿元） | 1505.6 | 1741.44 |\n")
        self.assertIn(W.PROBLEM_DOC_SCOPE, {p.kind for p in self._scope(text)})

    def test_multi_company_report_keeps_local_scope(self):
        """多公司报告：别的公司章节不算错，但本公司必需数值必须落在本公司作用域里。"""
        text = ("# 贵州茅台 2024 年度研究\n\n"
                "| 指标 | 2024 |\n|---|---|\n| 营业收入（亿元） | 1741.44 |\n\n"
                "# 五粮液 2024 年度研究\n\n"
                "| 指标 | 2024 |\n|---|---|\n| 营业收入（亿元） | 891.75 |\n")
        self.assertEqual(self._scope(text), [])



class TestWorkingPaperExport(unittest.TestCase):
    """底稿要真的落进任务产物，缺口要写进交付物（不是只留在内存里）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_wp_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", self._old)

    def _write_fin(self, tid: str, payload: dict) -> None:
        # 必须与 write_working_paper 用**同一个 project 作用域**定位目录，
        # 否则两边找的不是同一个地方（无 project 时回退旧版平铺路径）
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "financials.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_files_written_and_gap_note_reflects_missing(self):
        import working_paper_export as WPX
        tid = "wp-1"
        # 缺经营现金流 → 应产出缺口，并写进交付物可读段落
        payload = {
            "financials": [
                {"year": 2023, "report_type": "年报", "revenue": 1505.6,
                 "net_profit": 747.34},
                {"year": 2024, "report_type": "年报", "revenue": 1741.44,
                 "net_profit": 862.28},
            ],
            "metadata": REAL_SNAPSHOT["metadata"],
            "raw": {"url": "https://example.invalid/x", "text": "snapshot"},
        }
        self._write_fin(tid, payload)
        res = WPX.write_working_paper(
            tid, "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
                 "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            project="default")
        self.assertTrue(res["ok"], res)
        proj = ws_mod.task_project_dir(tid, "default")
        self.assertTrue((proj / WPX.PAPER_JSON).exists(), "底稿 JSON 必须落盘")
        self.assertTrue((proj / WPX.PAPER_CSV).exists(), "底稿 CSV 必须落盘")
        saved = json.loads((proj / WPX.PAPER_JSON).read_text(encoding="utf-8"))
        self.assertFalse(saved["ok"], "缺必需指标时底稿不得判达成")
        note = WPX.gaps_note(res)
        self.assertIn("底稿缺口与待核验项", note)
        self.assertIn("经营活动现金流", note, "缺口要具体到指标")

    def test_no_financials_means_no_paper(self):
        import working_paper_export as WPX
        res = WPX.write_working_paper("wp-2", "研究某公司", project="default")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("skipped"), "没有结构化财务时不产出空底稿冒充已复核")

    def test_scraped_company_does_not_overwrite_request(self):
        """抓取到别家公司时：请求主体保持用户写的那家，并把差异记成缺口。

        架构复核（2026-09-18）：此前 `write_working_paper` 把抓取到的 `metadata.company`
        当权威参数传给 `parse_research_request`，于是"用户要茅台、抓到比亚迪"会被悄悄
        改写成比亚迪；连带主体校验变成同义反复（两边都来自同一份元数据）。
        """
        import working_paper_export as WPX
        tid = "wp-scraped-wrong"
        payload = {
            "financials": [{"year": 2024, "report_type": "年报", "caliber": "合并",
                            "revenue": 9999.0, "net_profit": 300.0,
                            "operating_cashflow": 500.0}],
            "metadata": {"source": "eastmoney_ashare", "company": "比亚迪",
                         "stock_code": "002594", "currency": "CNY", "unit": "亿元"},
            "raw": {"url": "https://example.invalid/byd", "text": "{}"},
        }
        self._write_fin(tid, payload)
        # 说明：这里用**能解析出公司名**的目标写法（括号带代码）。表单拼的
        # "研究贵州茅台 2023 与 2024 两个年度…"在文本入口下会被弱抽取器读成别的词——
        # 所以正式路径是"提交时落库的契约"（下一条用例），文本入口只作兼容通道。
        res = WPX.write_working_paper(
            tid, "贵州茅台（600519.SH）2023 与 2024 年年度报告研究",
            project="default")
        self.assertTrue(res["ok"], res)
        proj = ws_mod.task_project_dir(tid, "default")
        saved = json.loads((proj / WPX.PAPER_JSON).read_text(encoding="utf-8"))
        self.assertEqual(saved["request"]["company"], "贵州茅台",
                         "抓取到的公司名不得改写用户请求")
        self.assertEqual(saved["request"]["identity_source"], "text")
        self.assertTrue(any(a.get("kind") == W.PROBLEM_UNRELATED
                            for a in saved["audit"]), saved["audit"])
        self.assertFalse(saved["ok"], "抓到别家公司的数据时不得判为达成（必需事实没拿到）")

    def test_persisted_contract_wins_over_goal_and_scraped(self):
        """提交时落库的契约优先：目标文本与抓取元数据都不能改它。"""
        import task_state
        import working_paper_export as WPX
        from facts import parse_research_request
        tid = "wp-stored-contract"
        payload = {
            "financials": [{"year": 2023, "report_type": "年报", "caliber": "母公司",
                            "revenue": 100.0, "net_profit": 10.0,
                            "operating_cashflow": 12.0},
                           {"year": 2024, "report_type": "年报", "caliber": "母公司",
                            "revenue": 120.0, "net_profit": 12.0,
                            "operating_cashflow": 15.0}],
            "metadata": {"source": "eastmoney_ashare", "company": "比亚迪",
                         "stock_code": "002594", "currency": "CNY", "unit": "亿元"},
            "raw": {"url": "https://example.invalid/x", "text": "{}"},
        }
        self._write_fin(tid, payload)
        contract = parse_research_request(
            "表单提交", company="贵州茅台", company_id="600519.SH", market="cn",
            periods=[2023, 2024], caliber="母公司",
            as_of="2025-04-30", identity_source="form")
        self.db = str(self.tmp / "contract.db")
        task_state.mark_queued(tid, goal="表单提交", research_request=contract.to_payload(),
                               db_path=self.db)
        original = task_state.DB_PATH
        task_state.DB_PATH = self.db
        self.addCleanup(setattr, task_state, "DB_PATH", original)
        res = WPX.write_working_paper(tid, "目标是另一家公司 2023 与 2024",
                                      project="default")
        saved = json.loads((ws_mod.task_project_dir(tid, "default")
                            / WPX.PAPER_JSON).read_text(encoding="utf-8"))
        self.assertEqual(res["request_source"], "stored")
        self.assertEqual(saved["request"]["company"], "贵州茅台")
        self.assertEqual(saved["request"]["caliber"], "母公司")
        self.assertEqual(saved["request"]["as_of"], "2025-04-30")
        self.assertEqual(saved["request"]["identity_source"], "form")


class TestCaliberEvidenceChain(unittest.TestCase):
    """口径证据链（实机复现的 bug）：适配器按来源结构声明口径后，研究底稿才能达标。

    修前：东财/SEC/巨潮都不声明口径 → 18 条事实全进 `pending` → 6 项必需事实全缺 →
    硬门槛判 draft（实机 `ui-96f5c363cc` 就是这个终态）。修后：来源用**自身结构**
    （含"归母净利润"类科目）声明合并口径，事实带 `caliber_source`/`caliber_evidence`
    参与达标；来源没有该结构时仍按未知处理（fail closed，见 `test_facts`）。
    """

    def _payload(self, *, declare: bool):
        md = {"source": "eastmoney_ashare", "company": "示例制造",
              "stock_code": "000001", "currency": "CNY", "unit": "亿元"}
        if declare:
            md["caliber"] = "合并"
            md["caliber_evidence"] = (
                "来源行含 PARENTNETPROFIT（归属于母公司股东的净利润），"
                "该科目只存在于合并报表 → 合并报表口径")
        rows = []
        for year, rev, np_, cf in ((2023, 1200.0, 150.0, 210.0),
                                   (2024, 1380.0, 174.0, 231.0)):
            rows.append({"year": year, "report_type": "年报",
                         "report_date": f"{year + 1}-04-20",
                         "disclosure_date": f"{year + 1}-04-03",
                         "revenue": rev, "net_profit": np_,
                         "operating_cashflow": cf})
        return {"financials": rows, "metadata": md,
                "raw": {"url": "https://example.invalid/financials/000001.json",
                        "text": '{"data": []}'}}

    def _request(self):
        return F.parse_research_request(
            "研究示例制造 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            company="示例制造", company_id="000001.SZ", market="cn",
            caliber="合并", as_of="2025-04-30", identity_source="form")

    def _paper(self, payload):
        return W.build_working_paper(F.facts_from_financials(payload), self._request())

    def test_declared_caliber_reaches_the_bar_with_yoy(self):
        paper = self._paper(self._payload(declare=True))
        self.assertTrue(paper.ok, [p.as_dict() for p in paper.problems])
        self.assertEqual(paper.completeness["present"], 6)
        self.assertEqual(paper.completeness["missing"], [])
        yoy = [d for d in paper.derived
               if str(d.get("metric") or "").endswith(F.YOY_SUFFIX)]
        self.assertEqual(len(yoy), 3, "三核心指标各一条同比")
        for d in yoy:
            self.assertEqual(d.get("unit"), "%")
            self.assertIn("同比", str(d.get("period") or ""))

    def test_undeclared_caliber_still_fails_closed(self):
        """对偶：来源不声明口径 → 事实只作展示，6 项必需事实仍缺、不得达标。"""
        paper = self._paper(self._payload(declare=False))
        self.assertFalse(paper.ok)
        self.assertEqual(paper.completeness["present"], 0)
        self.assertTrue(any(a.get("kind") == "caliber_mismatch" for a in paper.audit),
                        paper.audit)
        self.assertEqual(paper.derived, [], "未达标的事实不得参与同比")

    def test_row_carries_caliber_evidence_for_review(self):
        """底稿行要带口径与依据，审核人才能复核这条口径从哪来。"""
        paper = self._paper(self._payload(declare=True))
        row = paper.rows[0]
        self.assertEqual(row.get("caliber"), "合并")
        self.assertIn("PARENTNETPROFIT", row.get("caliber_evidence") or "")
        self.assertEqual({r.get("disclosed_at") for r in paper.rows},
                         {"2024-04-03", "2025-04-03"},
                         "披露日取公告日期（不是报告期末）")

    def test_read_side_detail_exposes_caliber_evidence(self):
        """读侧（`build_result`）也要带出口径与依据：报告步骤的"已选事实"块靠它。"""
        import tempfile
        import task_state
        import working_paper_export as WPX
        tid = "wp-caliber-detail"
        tmp = tempfile.mkdtemp(prefix="wm_cal_")
        old = ws_mod.WORKSPACE_ROOT
        old_db = task_state.DB_PATH
        ws_mod.configure_workspace_root(tmp)
        task_state.DB_PATH = str(Path(tmp) / "cal.db")
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old)
        self.addCleanup(setattr, task_state, "DB_PATH", old_db)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        # 契约落库：读侧按完整身份选事实，主体必须与夹具一致
        task_state.mark_queued(tid, goal="表单提交",
                               research_request=self._request().to_payload(),
                               db_path=task_state.DB_PATH)
        proj = ws_mod.task_project_dir(tid, "default")
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "financials.json").write_text(
            json.dumps(self._payload(declare=True), ensure_ascii=False),
            encoding="utf-8")
        res = WPX.build_result(tid, "表单提交", project="default")
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["rows_detail"][0]["caliber"], "合并")
        self.assertIn("PARENTNETPROFIT",
                      res["rows_detail"][0]["caliber_evidence"] or "")
        self.assertTrue(res["derived_detail"])
        self.assertEqual(res["derived_detail"][0]["caliber"], "合并")


class TestDerivedRatios(unittest.TestCase):
    """同年比率（净利率/现金流覆盖/资产负债率/研发强度）：报告要有经济含义，
    不能只有绝对数。比率必须可从已选事实复算，且不得因算不出来就把交付判成不达标。
    """

    def _payload(self, rows):
        return {"financials": rows,
                "metadata": {"source": "eastmoney_ashare", "company": "示例制造",
                             "currency": "CNY", "unit": "亿元", "caliber": "合并",
                             "caliber_evidence": "含 PARENTNETPROFIT"},
                "raw": {"url": "https://example.invalid/a", "text": "{}"}}

    def _rows(self, **over):
        base = [
            {"year": 2023, "report_type": "年报", "revenue": 1200.0, "net_profit": 150.0,
             "operating_cashflow": 210.0, "total_assets": 900.0,
             "total_liabilities": 300.0, "rd_expense": 12.0,
             "disclosure_date": "2024-04-03"},
            {"year": 2024, "report_type": "年报", "revenue": 1380.0, "net_profit": 174.0,
             "operating_cashflow": 231.0, "total_assets": 1000.0,
             "total_liabilities": 350.0, "rd_expense": 15.0,
             "disclosure_date": "2025-04-03"},
        ]
        for r in base:
            r.update(over)
        return base

    def _paper(self, rows):
        req = F.parse_research_request(
            "研究示例制造 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            company="示例制造", company_id="000001.SZ", market="cn",
            caliber="合并", as_of="2025-04-30", identity_source="form")
        return W.build_working_paper(F.facts_from_financials(self._payload(rows)), req)

    def test_ratios_are_recomputable_with_formula_and_inputs(self):
        paper = self._paper(self._rows())
        by = {(d["metric"], d["period"]): d for d in paper.derived}
        self.assertAlmostEqual(by[("net_margin", "2024年")]["value"],
                               round(174.0 / 1380.0 * 100, 2), places=2)
        self.assertAlmostEqual(by[("cashflow_coverage", "2024年")]["value"],
                               round(231.0 / 174.0 * 100, 2), places=2)
        self.assertAlmostEqual(by[("debt_ratio", "2024年")]["value"],
                               round(350.0 / 1000.0 * 100, 2), places=2)
        self.assertAlmostEqual(by[("rd_intensity", "2024年")]["value"],
                               round(15.0 / 1380.0 * 100, 2), places=2)
        for key in (("net_margin", "2024年"), ("debt_ratio", "2024年")):
            d = by[key]
            self.assertEqual(d["unit"], "%")
            self.assertIn("/", d["formula"], "公式要能复核")
            self.assertEqual(len(d["derived_from"]), 2, "两个输入 fact_id")
            self.assertEqual(d["caliber"], "合并", "口径继承输入")
        self.assertTrue(paper.ok, [p.detail for p in paper.problems])

    def test_zero_denominator_is_audited_not_fatal(self):
        """分母为 0：如实记"不可算"，但不把整份交付判成不达标。"""
        paper = self._paper(self._rows(total_assets=0.0))
        self.assertFalse(any(d["metric"] == "debt_ratio" for d in paper.derived))
        self.assertTrue(paper.ok, [p.detail for p in paper.problems])
        self.assertTrue(any("资产负债率" in str(a.get("detail") or "")
                            for a in paper.audit), paper.audit)

    def test_missing_input_is_not_a_gap(self):
        """契约没要求总资产/总负债 → 不算比率、也不记缺口（支撑事实不是交付要求）。"""
        rows = [{k: v for k, v in r.items()
                 if k not in ("total_assets", "total_liabilities", "rd_expense")}
                for r in self._rows()]
        paper = self._paper(rows)
        metrics = {d["metric"] for d in paper.derived}
        self.assertNotIn("debt_ratio", metrics)
        self.assertNotIn("rd_intensity", metrics)
        self.assertIn("net_margin", metrics)
        self.assertTrue(paper.ok, [p.detail for p in paper.problems])
        self.assertEqual(paper.audit, [])

    def test_ratio_normalizes_units_before_dividing(self):
        """架构复核 P1：分子/分母单位不同时必须先归一再相除（否则静默放大 1e4 倍）。

        反例形状：同一年的净利润来自"万元"行、营业收入来自"亿元"行——此前只比币种与
        口径，直接相除得到 125000%（而非 12.5%），且 paper.ok 仍为 True、无任何提示。
        """
        rows = [
            {"year": 2023, "report_type": "年报", "revenue": 1200.0,
             "disclosure_date": "2024-04-03"},
            {"year": 2023, "report_type": "年报", "unit": "万元",
             "net_profit": 1500000.0, "disclosure_date": "2024-04-03"},
            {"year": 2024, "report_type": "年报", "revenue": 1380.0,
             "disclosure_date": "2025-04-03"},
            {"year": 2024, "report_type": "年报", "unit": "万元",
             "net_profit": 1740000.0, "disclosure_date": "2025-04-03"},
        ]
        paper = self._paper(rows)
        by = {(d["metric"], d["period"]): d for d in paper.derived}
        self.assertAlmostEqual(by[("net_margin", "2023年")]["value"], 12.5, places=2)
        self.assertAlmostEqual(by[("net_margin", "2024年")]["value"], 12.61, places=2)
        # 换算过程必须留在公式里，读者才能复核
        self.assertIn("万元", by[("net_margin", "2024年")]["formula"])
        self.assertIn("换算", by[("net_margin", "2024年")]["formula"])

    def test_ratio_not_produced_when_units_not_convertible(self):
        """单位不可换算（百分比/未知）→ 不生成比率，只记审计提示，不硬算。"""
        rows = [
            {"year": 2023, "report_type": "年报", "revenue": 1200.0,
             "disclosure_date": "2024-04-03"},
            {"year": 2023, "report_type": "年报", "unit": "%",
             "net_profit": 12.5, "disclosure_date": "2024-04-03"},
        ]
        paper = self._paper(rows)
        self.assertFalse(any(d["metric"] == "net_margin" for d in paper.derived))
        self.assertTrue(any("不可换算" in str(a.get("detail") or "")
                            for a in paper.audit), paper.audit)

    def test_negative_base_is_not_a_yoy(self):
        """负基期：同比不具可比含义 → 记为不可算（应分别描述亏损/转正），不硬算。"""
        rows = [
            {"year": 2023, "report_type": "年报", "revenue": 1200.0, "net_profit": -50.0,
             "operating_cashflow": 210.0, "disclosure_date": "2024-04-03"},
            {"year": 2024, "report_type": "年报", "revenue": 1380.0, "net_profit": 174.0,
             "operating_cashflow": 231.0, "disclosure_date": "2025-04-03"},
        ]
        paper = self._paper(rows)
        self.assertFalse(any(d["metric"] == "net_profit_yoy" for d in paper.derived),
                         "负基期不得产出同比")
        self.assertTrue(any("基期为负" in p.detail for p in paper.problems),
                        [p.detail for p in paper.problems])

    def test_ratio_labels_state_attribution(self):
        """命名要写清归属口径：分子是归母净利润、现金流是合并口径。"""
        self.assertEqual(F.metric_label("net_margin"), "归母净利率")
        self.assertEqual(F.metric_label("cashflow_coverage"),
                         "经营现金流对归母净利润的覆盖")

    def test_labels_are_chinese_for_injection(self):
        """注入块与报告都直接读标签：英文 slug 对报告读者没有意义。"""
        self.assertEqual(F.metric_label("net_margin"), "归母净利率")
        self.assertEqual(F.metric_label("cashflow_coverage"),
                         "经营现金流对归母净利润的覆盖")
        self.assertEqual(F.metric_label("debt_ratio"), "资产负债率")
        self.assertEqual(F.metric_label("revenue_yoy"), "营业收入同比")


if __name__ == "__main__":
    unittest.main(verbosity=2)
