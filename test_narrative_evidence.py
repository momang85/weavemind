# -*- coding: utf-8 -*-
"""F2 退出关卡：定向取证——抓到的年报正文 → **可定位的短证据**。

断言按架构指令的退出标准：
- 正文按小节切分并归类（业务背景 / 经营变化解释 / 财务附注 / 风险因素），每条带
  **小节路径 + 字符区间**（网页没有页码，这是诚实替代，不假装有页码）；
- 没抓到的类别记为缺口（`missing_kinds`），**不用模型知识补**；
- 检索摘要只有 snippet、没有正文定位 → `has_location=False`，**不计入已取得证据**；
- 提取全程不联网、不调用模型（同一输入必得同一输出）；
- 研究路径的两个抓取步骤按角色分流，**不把同一页抓两遍**；没有候选时第二个抓取
  步骤拿不到 URL（worker 明确失败），不得退化成"抓第一页"。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import narrative_evidence as ne  # noqa: E402
import workspace as ws_mod  # noqa: E402

ANNUAL_TEXT = """贵州茅台2024年年度报告

第三节 管理层讨论与分析

一、经营情况讨论与分析

报告期内，公司实现营业收入1741.44亿元，同比增长15.66%；归属于上市公司股东的净利润862.28亿元，同比增长15.38%。营业收入变动主要系本期销量增加及产品结构变化所致。

（一）主营业务情况

公司主营业务为茅台酒及系列酒的生产与销售，经营模式为以销定产，主要产品为茅台酒、系列酒，销售模式以直销与批发代理并行。

二、可能面对的风险

风险因素：宏观经济波动可能影响高端白酒消费需求；行业政策与税收政策变化存在不确定性。

第五节 财务报告

七、财务报表附注

现金流量表附注：报告期内经营活动产生的现金流量净额924.64亿元，主要系销售商品收到的现金增加所致。营运资本变动情况见附注。
"""

ISSUER_URL = "https://static.cninfo.com.cn/finalpage/2025-04-03/1219999999.PDF"
NEWS_URL = "https://finance.sina.com.cn/a/1"


class TestSectionExtraction(unittest.TestCase):
    def test_sections_are_classified_with_locations(self):
        recs = ne.extract_sections(
            {"title": "贵州茅台2024年年度报告", "url": ISSUER_URL, "text": ANNUAL_TEXT},
            periods=[2023, 2024])
        kinds = {r["kind"] for r in recs}
        self.assertEqual(kinds, set(ne.KIND_ORDER),
                         "四类小节都要能被认出来（含附注与风险）")
        for r in recs:
            self.assertTrue(r["has_location"])
            self.assertIn("小节：", r["locator"])
            self.assertIn("字符 ", r["locator"])
            self.assertTrue(r["snippet"])
            self.assertLessEqual(len(r["snippet"]), ne.SNIPPET_CHARS)
            self.assertEqual(r["source_type"], "issuer_annual_report")
        # 位置是**原文里的真实区间**：能按区间切回同一段文字
        bg = next(r for r in recs if r["kind"] == ne.KIND_BACKGROUND)
        self.assertIn("主营业务为茅台酒", ANNUAL_TEXT[bg["char_start"]:bg["char_end"]])
        self.assertIn("主营业务", bg["section"])
        # 风险段落在"可能面对的风险"小节里，不是被别的关键词抢走
        risk = next(r for r in recs if r["kind"] == ne.KIND_RISK)
        self.assertIn("风险", risk["section"])
        self.assertIn("宏观经济波动", risk["snippet"])

    def test_third_party_source_is_not_labelled_issuer(self):
        recs = ne.extract_sections(
            {"title": "贵州茅台2024年业绩点评_新浪财经", "url": NEWS_URL,
             "text": "一、经营情况讨论与分析\n\n公司营收增长主要系销量增加。"},
            periods=[2024])
        self.assertTrue(recs)
        self.assertEqual({r["source_type"] for r in recs}, {"third_party"},
                         "第三方媒体的经营点评不得标成发行人年报")

    def test_per_kind_cap_and_dedupe(self):
        body = "\n\n".join(
            f"（{i}）主营业务情况\n\n公司主营业务为第 {i} 项产品的生产与销售。"
            for i in range(1, 8))
        recs = ne.extract_sections(
            {"title": "年报", "url": ISSUER_URL, "text": body}, periods=[2024])
        self.assertEqual(len(recs), ne.MAX_PER_KIND, "同一类别有条数上限")
        keys = [(r["kind"], r["section"]) for r in recs]
        self.assertEqual(len(keys), len(set(keys)), "同小节不重复登记")

    def test_empty_text_yields_nothing(self):
        self.assertEqual(ne.extract_sections({"url": ISSUER_URL, "text": ""}), [])
        self.assertEqual(ne.extract_sections({"url": ISSUER_URL}), [])


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="weavemind_narr_"))
        self._orig = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(self._restore)
        guard = mock.patch("socket.socket.connect",
                           side_effect=AssertionError("叙事证据提取不得联网"))
        guard.start()
        self.addCleanup(guard.stop)
        self.tid = "narr-1"

    def _restore(self):
        ws_mod.configure_workspace_root(str(self._orig))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _project(self) -> Path:
        return ws_mod.task_project_dir(self.tid, "default")

    def _write(self, name: str, payload) -> None:
        proj = self._project()
        (proj / name).write_text(json.dumps(payload, ensure_ascii=False),
                                 encoding="utf-8")

    def test_no_docs_reports_all_kinds_missing(self):
        data = ne.build(self.tid, periods=[2023, 2024], company="贵州茅台")
        self.assertFalse(data["ok"])
        self.assertEqual(list(data["missing_kinds"]), list(ne.KIND_ORDER))
        self.assertEqual(data["records"], [])
        # 落盘可读（页面/简报按同一份文件读）
        again = ne.read(self.tid)
        self.assertEqual(again["missing_kinds"], data["missing_kinds"])

    def test_snapshot_text_becomes_located_evidence(self):
        self._write("fetch_snapshot.json", [
            {"title": "贵州茅台2024年年度报告", "url": ISSUER_URL, "text": ANNUAL_TEXT},
        ])
        data = ne.build(self.tid, periods=[2023, 2024], company="贵州茅台")
        self.assertTrue(data["ok"])
        self.assertEqual(data["missing_kinds"], [],
                         "四类都从抓取正文里取得了带定位的证据")
        self.assertEqual(data["located"], len(data["records"]))
        self.assertEqual([s["url"] for s in data["sources"]], [ISSUER_URL])

    def test_search_snippet_alone_is_not_evidence(self):
        self._write("search_results.json", [
            {"title": "贵州茅台风险因素提示", "url": NEWS_URL,
             "snippet": "风险因素：白酒行业景气度波动。"},
        ])
        data = ne.build(self.tid, periods=[2024], company="贵州茅台")
        self.assertIn(ne.KIND_RISK, data["missing_kinds"],
                      "只有检索摘要、没有正文定位 → 仍算缺口")
        snippet_only = [r for r in data["records"] if not r["has_location"]]
        self.assertEqual(len(snippet_only), 1)
        self.assertEqual(snippet_only[0]["locator"], "检索摘要（未取得正文定位）")

    def test_extra_docs_are_merged_and_deduped(self):
        self._write("fetch_snapshot.json", [
            {"title": "贵州茅台2024年年度报告", "url": ISSUER_URL, "text": ANNUAL_TEXT},
        ])
        data = ne.build(self.tid, periods=[2024], company="贵州茅台",
                        extra_docs=[{"title": "同页重复", "url": ISSUER_URL,
                                     "text": ANNUAL_TEXT},
                                    {"title": "风险因素公告", "url": NEWS_URL,
                                     "text": "二、风险因素\n\n行业政策变化存在不确定性。"}])
        urls = [s["url"] for s in data["sources"]]
        self.assertEqual(sorted(urls), sorted({ISSUER_URL, NEWS_URL}),
                         "同一 URL 只登记一次")

    def test_build_is_deterministic(self):
        self._write("fetch_snapshot.json", [
            {"title": "贵州茅台2024年年度报告", "url": ISSUER_URL, "text": ANNUAL_TEXT},
        ])
        a = ne.build(self.tid, periods=[2023, 2024], company="贵州茅台")
        b = ne.build(self.tid, periods=[2023, 2024], company="贵州茅台")
        self.assertEqual(a["records"], b["records"], "同一输入必得同一输出")


class TestFetchRoleRouting(unittest.TestCase):
    """研究路径两个抓取步骤：各取所需、不抓重；无候选时明确失败。"""

    ITEMS = [
        {"title": "贵州茅台2024年年度报告（经营情况讨论与分析）", "url": ISSUER_URL,
         "snippet": "年报正文"},
        {"title": "贵州茅台2024年年报财务数据_数据中心", "url": "https://data.eastmoney.com/a/2",
         "snippet": "财务数据"},
        {"title": "贵州茅台现金流量表附注与风险因素", "url": "https://www.sse.com.cn/a/3",
         "snippet": "附注"},
    ]

    def _pick(self, **kw):
        from orchestrator_v2 import OrchestratorV2
        return OrchestratorV2._pick_fetch_url(self.ITEMS, "研究贵州茅台2024年财报", **kw)

    def test_roles_pick_different_pages(self):
        annual = self._pick(role="annual_report")
        notes = self._pick(role="notes")
        self.assertEqual(annual, ISSUER_URL)
        self.assertEqual(notes, "https://www.sse.com.cn/a/3")
        self.assertNotEqual(annual, notes, "两个抓取步骤不得抓同一页")

    def test_exclude_keeps_second_step_off_the_first_page(self):
        annual = self._pick(role="annual_report")
        second = self._pick(role="notes", exclude=(annual,))
        self.assertNotEqual(second, annual)
        # 排除全部候选 → 没有可抓的页（worker 会明确失败，不联网）
        self.assertIsNone(self._pick(role="notes", exclude=tuple(i["url"] for i in self.ITEMS)))

    def test_no_role_keeps_previous_behaviour(self):
        self.assertIsNotNone(self._pick(), "非研究路径的抓取选择不变")

    def test_research_plan_has_two_fetch_steps_with_roles(self):
        from facts import parse_research_request
        from orchestrator_v2 import OrchestratorV2
        req = parse_research_request(
            "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、"
            "经营活动现金流净额，合并报表口径，数据截至 2025-04-30",
            company="贵州茅台", company_id="600519.SH", market="cn",
            caliber="合并", as_of="2025-04-30", identity_source="form")
        steps = OrchestratorV2._research_steps(req)
        caps = [s["capability"] for s in steps]
        self.assertEqual(caps, ["web_search", "web_fetch", "web_fetch",
                                "content_summary", "report_generator"])
        fetch_ids = [s["step_id"] for s in steps if s["capability"] == "web_fetch"]
        self.assertEqual(fetch_ids, ["2", "2b"])
        self.assertIn("年报", steps[1]["instruction"])
        self.assertIn("附注", steps[2]["instruction"])
        self.assertIn("不得编造 URL", steps[2]["instruction"])


class TestEvidenceBlock(unittest.TestCase):
    """注入块：只给证据字段（小节定位 + 短证据 + URL），不把正文整段塞进提示词。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="weavemind_narr2_"))
        self._orig = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(self._restore)
        self.tid = "narr-2"

    def _restore(self):
        ws_mod.configure_workspace_root(str(self._orig))
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_block_carries_locators_and_missing_kinds(self):
        from orchestrator_v2 import OrchestratorV2
        proj = ws_mod.task_project_dir(self.tid, "default")
        (proj / "fetch_snapshot.json").write_text(json.dumps(
            [{"title": "贵州茅台2024年年度报告", "url": ISSUER_URL, "text": ANNUAL_TEXT}],
            ensure_ascii=False), encoding="utf-8")
        block = OrchestratorV2._narrative_evidence_block(self.tid, "研究贵州茅台财报")
        self.assertIn("[已取证据]", block)
        self.assertIn("小节：", block)
        self.assertIn(ISSUER_URL, block)
        self.assertIn("不得编造因果", block)
        # 单条证据有字数上限：不整段搬运正文
        longest = max(len(line) for line in block.splitlines())
        self.assertLess(longest, 700)

    def test_block_without_evidence_lists_gaps_not_guesses(self):
        from orchestrator_v2 import OrchestratorV2
        block = OrchestratorV2._narrative_evidence_block(self.tid, "研究贵州茅台财报")
        self.assertIn("未取得正文定位的类别", block)
        self.assertIn("业务背景", block)
        self.assertIn("不得用模型知识补原因", block)


if __name__ == "__main__":
    unittest.main()
