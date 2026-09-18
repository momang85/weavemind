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
        self.assertEqual(set(d["metric"] for d in self.paper.derived),
                         {"revenue_yoy", "net_profit_yoy", "operating_cashflow_yoy"})

    def test_derived_values_match_hand_computation(self):
        want = {"revenue_yoy": 15.0,
                "net_profit_yoy": (174.0 - 150.0) / 150.0 * 100,
                "operating_cashflow_yoy": (231.0 - 210.0) / 210.0 * 100}
        for d in self.paper.derived:
            self.assertAlmostEqual(d["value"], round(want[d["metric"]], 2), places=2,
                                   msg=f"{d['metric']} 可从底稿重算")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
