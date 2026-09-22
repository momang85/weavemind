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


class TestChangeDemotionTarget(unittest.TestCase):
    """C2-3 的降级目标按来源分（实机反例：21 财经业绩说明会报道被标"财务附注"）。

    "命中变化关键词但无因果语言"的段落不得当变化原因；降级到哪一类要看文档来源：
    发行人文件 → 财务附注（数字出处）；第三方新闻/解读 → 业务背景。一律叫"财务附注"
    会让读者把新闻报道当成报表附注。
    """

    NEWS_TEXT = ("两大苏酒同日举行业绩说明会：洋河、今世缘如何错位竞争？ 5月13日，"
                 "今世缘、洋河股份分别召开了2023年度业绩说明会。2023年，洋河股份营收"
                 "超过331亿元，归母净利润首次突破100亿元。")

    def test_third_party_news_passage_becomes_background(self):
        recs = ne._paragraph_records(
            {"title": "两大苏酒同日举行业绩说明会", "url": NEWS_URL, "text": self.NEWS_TEXT},
            periods=[2023, 2024], company="洋河股份", company_id="002304.SZ",
            as_of="2025-04-30")
        kinds = [r["kind"] for r in recs]
        self.assertIn(ne.KIND_BACKGROUND, kinds)
        self.assertNotIn(ne.KIND_NOTES, kinds, "新闻稿不得标成财务附注")

    def test_issuer_passage_still_demotes_to_footnote(self):
        recs = ne._paragraph_records(
            {"title": "洋河股份2024年年度报告", "url": ISSUER_URL,
             "text": "报告期内，公司营业收入同比增长 12%，归属于上市公司股东的净利润"
                     "同比增长 8%。具体口径见财务报表附注。"},
            periods=[2024], company="洋河股份", company_id="002304.SZ", as_of="2025-04-30")
        self.assertEqual([r["kind"] for r in recs], [ne.KIND_NOTES])

    def test_unknown_source_keeps_legacy_demotion(self):
        """不传 source（旧调用形态）沿用旧口径（附注）——改动不改变既有语义。"""
        text = "公司营业收入同比增长，详见附注。"
        self.assertEqual(ne.classify("经营情况讨论与分析", text), ne.KIND_NOTES)
        self.assertEqual(ne.classify("经营情况讨论与分析", text,
                                     source="third_party"), ne.KIND_BACKGROUND)


class TestRealMdnaCausality(unittest.TestCase):
    """D3：真实年报的"环境 + 应对"式解释必须被判为因果（否则真实经营讨论进不了解释）。

    实机材料（`evals/real/yanghe_ar2024_mdna_excerpt.json`，有界抓取冻结）：
    "白酒行业进入存量竞争阶段，市场竞争更加白热化……价位段承压较大……积极调整经营策略，
    应对外部环境的变化和发展中存在的问题，2024 年实现营业收入 288.76 亿元，同比下降 12.83%"。
    这句一个旧标记都不命中 → 被降级成"财务附注"，正向分支无真实材料可证。
    """

    REAL = ("报告期内，白酒行业进入存量竞争阶段，市场竞争更加白热化，公司主力产品集中的"
            "中端和次高端价位段承压较大，本着科学发展和可持续发展的原则，公司积极调整"
            "经营策略，应对外部环境的变化和发展中存在的问题，2024 年实现营业收入"
            " 288.76 亿元，同比下降 12.83%")

    def test_real_mdna_sentence_is_causal_and_classified_as_change(self):
        self.assertTrue(ne.is_causal(self.REAL), "真实经营讨论被判成非因果")
        self.assertFalse(ne.is_policy_text(self.REAL))
        self.assertEqual(
            ne.classify("经营情况讨论与分析", self.REAL, source="issuer_annual_report"),
            ne.KIND_CHANGE, "真实 MD&A 段落应归为经营变化解释")

    def test_policy_and_number_only_texts_stay_non_causal(self):
        policy = ("本公司自 2024 年 1 月 1 日起执行财政部修订后的《企业会计准则第 14 号》，"
                  "会计政策变更采用追溯调整法，比较期间数据已重述。")
        self.assertFalse(ne.is_causal(policy), policy)
        number_only = ("经营活动产生的现金流量净额本期为 46.29 亿元，上期为 61.30 亿元，"
                       "变动幅度为 -24.49%。")
        self.assertFalse(ne.is_causal(number_only), number_only)

    def test_real_excerpt_fixture_matches_the_rule(self):
        """冻结片段必须与生产判据一致（否则夹具会漂移成"合成正例"）。"""
        p = Path(__file__).resolve().parent / "evals" / "real" / "yanghe_ar2024_mdna_excerpt.json"
        fx = json.loads(p.read_text(encoding="utf-8"))
        self.assertTrue(fx["sections"], fx)
        for s in fx["sections"]:
            self.assertTrue(ne.is_causal(s["text"]), s["text"][:60])


class TestContractApplicability(unittest.TestCase):
    """F2′-1：有字符位置只证明"在某段文本里找到"，不证明主体/期间/资料截止成立。"""

    GOAL_AS_OF = "2025-04-30"
    PERIODS = [2023, 2024]

    def _recs(self, title, url, text, **kw):
        return ne.extract_sections(
            {"title": title, "url": url, "text": text},
            periods=kw.pop("periods", self.PERIODS), company="贵州茅台",
            company_id="600519.SH", as_of=kw.pop("as_of", self.GOAL_AS_OF), **kw)

    def test_other_subject_or_later_document_is_excluded(self):
        """离线反例：五粮液 2026 年报与 2027 年发布的第三方段落都不得当证据。"""
        wuliangye = self._recs(
            "五粮液2026年年度报告",
            "https://static.cninfo.com.cn/finalpage/2027-04-01/999.PDF",
            "一、经营情况讨论与分析\n\n本公司2026年度营业收入891.75亿元，主要系系列酒销量增加所致。\n")
        third_2027 = self._recs(
            "白酒行业2027年展望_某媒体", "https://news.example/2027/outlook",
            "一、经营情况讨论与分析\n\n贵州茅台2027年经营情况讨论：预计营业收入将保持增长。\n")
        for recs in (wuliangye, third_2027):
            self.assertTrue(recs)
            self.assertTrue(all(r["excluded"] for r in recs), recs)
            self.assertTrue(all(r["has_location"] for r in recs),
                            "位置仍然记录（用于说明为什么没采用）")
        self.assertEqual({r["validation_status"] for r in wuliangye}, {"after_as_of"})
        self.assertEqual({r["validation_status"] for r in third_2027}, {"after_as_of"})

    def test_real_annual_report_within_as_of_is_applicable(self):
        recs = self._recs(
            "贵州茅台2024年年度报告",
            "https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF",
            "一、经营情况讨论与分析\n\n2024年度营业收入1741.44亿元，主要系销量增加所致。\n")
        self.assertTrue(recs)
        self.assertEqual({r["validation_status"] for r in recs}, {"applicable"})
        self.assertFalse(any(r["excluded"] for r in recs))

    def test_comparison_disclosure_with_full_date_is_admitted(self):
        """允许匹配两期的真实比较披露（发布日**完整**且在资料截止前）。"""
        recs = self._recs(
            "茅台2022年报", "https://www.sse.com.cn/a/2023-04-08/2022.PDF",
            "一、经营情况讨论与分析\n\n2022年营业收入1275.5亿元，2023年营业收入1505.6亿元。\n")
        self.assertTrue(recs)
        self.assertEqual({r["validation_status"] for r in recs}, {"comparison"})
        self.assertEqual({r["admission"] for r in recs}, {"comparison"})
        self.assertFalse(any(r["excluded"] for r in recs))

    def test_month_or_year_precision_dates_are_not_admitted(self):
        """A3：仅知年月/年份不能断言"未晚于资料截止"——不得虚构月初/年初据此通过。"""
        month = self._recs(
            "贵州茅台2024年报解读_某媒体", "https://news.example/2025-03/review",
            "一、经营情况讨论与分析\n\n贵州茅台2024年营业收入1741.44亿元。\n")
        year = self._recs(
            "贵州茅台2024年报解读_某媒体", "https://news.example/2025/review",
            "一、经营情况讨论与分析\n\n贵州茅台2024年营业收入1741.44亿元。\n")
        for recs in (month, year):
            self.assertTrue(recs)
            self.assertTrue(all(r["admission"] == "unknown" for r in recs), recs)
            self.assertTrue(all(r["published_precision"] in ("month", "year") for r in recs))
        # 月份精度参与排序，但**精度另记**，不冒充已核实日期
        self.assertEqual(month[0]["published_at"], "2025-03-01")
        self.assertEqual(month[0]["published_precision"], "month")

    def test_full_date_after_the_cutoff_is_excluded(self):
        """A3 反例：资料截止 2025-04-15，文档发布 2025-04-20 → 必须排除。"""
        recs = self._recs(
            "贵州茅台2024年年度报告",
            "https://www.cninfo.com.cn/a/2025-04-20/x.PDF",
            "一、经营情况讨论与分析\n\n2024年度营业收入288.76亿元。\n",
            as_of="2025-04-15")
        self.assertTrue(recs)
        self.assertEqual({r["validation_status"] for r in recs}, {"after_as_of"})
        self.assertTrue(all(r["admission"] == "excluded" for r in recs))

    def test_paragraph_fallback_when_page_has_no_headings(self):
        """整页无小节标题（新闻/解读类）→ 段落窗口回退，仍可定位、可准入。

        实机读数：21 财经 2024-05-14 的行业观察（主体命中、发布日在截止内）因整页
        没有标题行而产出 0 条证据，简报只能写"未取得"。
        """
        text = ("两大苏酒同日举行业绩说明会：洋河、今世缘如何错位竞争？\n\n"
                "洋河股份 2024 年营业收入同比下滑，公司称主要系产品结构主动调整与"
                "渠道去库存所致，省内市场竞争加剧是共同背景。\n\n"
                "公司同时披露了经销商库存与合同负债的变化情况，但未给出下一阶段指引。\n")
        recs = ne.extract_sections(
            {"title": "两大苏酒同日举行业绩说明会：洋河、今世缘如何错位竞争？",
             "url": "https://m.21jingji.com/article/20240514/herald/x.html", "text": text},
            periods=[2023, 2024], company="洋河股份", company_id="002304.SZ",
            as_of="2025-04-30")
        self.assertTrue(recs, "无标题页面应走段落回退")
        self.assertEqual({r["extraction"] for r in recs}, {"paragraph"})
        rec = recs[0]
        self.assertEqual(rec["admission"], "admitted")
        self.assertEqual(rec["validation_status"], "period_unstated",
                         "新闻页标题没有报告期 → 记未标注，不据此排除")
        self.assertIn("段落 ", rec["locator"])
        self.assertIn("字符 ", rec["locator"])
        # 段落窗口在原文里的位置是真实的：按区间能取回同一段
        seg = text[rec["char_start"]:rec["char_end"]]
        self.assertIn(rec["snippet"][:20], " ".join(seg.split()))

    def test_heading_mode_still_wins_when_headings_exist(self):
        """有可分类小节标题时仍走标题模式（段落回退只在整页无标题时启用）。"""
        recs = self._recs(
            "贵州茅台2024年年度报告",
            "https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF",
            "一、经营情况讨论与分析\n\n2024年度营业收入1741.44亿元。\n")
        self.assertTrue(recs)
        self.assertEqual({r.get("extraction") for r in recs}, {None})

    def test_report_period_year_is_not_a_publication_date(self):
        """A3 反例：只有"2024年年度报告"、无发布日 → 缺项记 unknown，不当 applicable。"""
        recs = self._recs(
            "2024年年度报告", "https://news.example/report/2024",
            "一、经营情况讨论与分析\n\n本公司2024年度营业收入同比增长。\n")
        self.assertTrue(recs)
        self.assertEqual({r["subject_state"] for r in recs}, {"unknown"})
        self.assertEqual({r["subject"] for r in recs}, {""},
                         "subject 不得从请求回填")
        self.assertTrue(all(r["admission"] == "unknown" for r in recs), recs)
        # _located 只收明确准入的证据：unknown 不得进"管理层/附注解释"
        import report_brief
        self.assertEqual(report_brief._located(
            {"records": [dict(x, kind="change_explanation") for x in recs]},
            "change_explanation"), [])

    def test_third_party_with_full_date_within_as_of_is_usable(self):
        """合法第三方引用正例：完整发布日 + 命中主体 → admitted（类型仍是第三方）。"""
        recs = self._recs(
            "贵州茅台2024年报解读_某媒体", "https://news.example/2025-03-12/review",
            "一、经营情况讨论与分析\n\n贵州茅台2024年营业收入1741.44亿元，较2023年增长15.66%。\n")
        self.assertTrue(recs)
        self.assertEqual({r["source_type"] for r in recs}, {"third_party"})
        self.assertEqual({r["admission"] for r in recs}, {"admitted"})
        self.assertFalse(any(r["excluded"] for r in recs))

    def test_publisher_identity_ignores_title_and_query_params(self):
        """发布者身份只看主机名：标题含"年报"、查询参数里带官方域名都不算发行人披露。"""
        self.assertEqual(ne.source_type("https://static.cninfo.com.cn/x.PDF"),
                         "issuer_annual_report")
        self.assertEqual(
            ne.source_type("https://datacenter-web.eastmoney.com/api?u=sse.com.cn"),
            "third_party", "查询参数里的域名不是发布者")
        self.assertEqual(ne.source_type("https://news.example/贵州茅台2024年年度报告"),
                         "third_party", "标题/路径里的『年报』字样不能证明发布者")

    def test_record_carries_the_required_provenance_fields(self):
        recs = self._recs(
            "贵州茅台2024年年度报告",
            "https://static.cninfo.com.cn/finalpage/2025-04-03/1.PDF",
            "一、经营情况讨论与分析\n\n2024年度营业收入1741.44亿元。\n")
        rec = recs[0]
        for field in ("subject", "document_period", "published_at", "fetched_at",
                      "publisher", "source_type", "content_hash", "locator",
                      "validation_status"):
            self.assertIn(field, rec, field)
        self.assertEqual(rec["publisher"], "static.cninfo.com.cn")
        self.assertEqual(rec["document_period"], "2024")
        self.assertEqual(rec["published_at"], "2025-04-03")
        self.assertTrue(rec["content_hash"])


class TestPdfEvidence(unittest.TestCase):
    """F2′-2：年报 PDF 的文本提取与**页码定位**（解析通道不依赖网页 worker）。"""

    def test_looks_like_pdf(self):
        import annual_report_pdf as pdf
        self.assertTrue(pdf.looks_like_pdf("https://x.com/a.PDF"))
        self.assertTrue(pdf.looks_like_pdf("https://x.com/a.pdf?t=1"))
        self.assertTrue(pdf.looks_like_pdf("https://x.com/download", b"%PDF-1.7"))
        self.assertFalse(pdf.looks_like_pdf("https://x.com/a.html"))

    def test_pages_map_to_page_numbers_in_locators(self):
        """每页文本 → 页码偏移 → 小节 locator 写"第 N 页"（可回溯到原页）。"""
        import annual_report_pdf as pdf
        pages = [
            "第一节 释义\n\n本报告书指贵州茅台酒股份有限公司年度报告。",
            "第三节 管理层讨论与分析\n\n一、经营情况讨论与分析\n\n2024年度营业收入1741.44亿元，"
            "主要系销量增加及产品结构变化所致。",
            "第八节 财务报告\n\n七、财务报表附注\n\n现金流量表附注：经营活动现金流量净额924.64亿元。",
        ]
        doc = pdf.doc_from_pages("贵州茅台2024年年度报告", "https://x.com/a.pdf", pages)
        recs = ne.extract_sections(doc, periods=[2023, 2024], company="贵州茅台",
                                   company_id="600519.SH", as_of="2025-04-30")
        by_kind = {r["kind"]: r for r in recs}
        self.assertEqual(by_kind["change_explanation"]["page"], 2)
        self.assertEqual(by_kind["footnote"]["page"], 3)
        self.assertIn("第 2 页", by_kind["change_explanation"]["locator"])
        self.assertIn("第 3 页", by_kind["footnote"]["locator"])
        # 页码与正文能对上：按页码取回该页，能找到定位到的那段
        self.assertIn("营业收入1741.44亿元", pages[1])

    def test_garbage_or_scanned_pdf_yields_no_doc(self):
        """反例：不是 PDF 的字节、或提取不出文本 → 返回 None（缺口），不硬编。"""
        import annual_report_pdf as pdf
        self.assertIsNone(pdf.doc_from_url("https://x.com/a.pdf", data=b"<script>waf</script>"))
        self.assertIsNone(pdf.doc_from_url("https://x.com/a.pdf", data=b"%PDF-1.4\n\n"))
        self.assertEqual(pdf.pages_text(b"not a pdf"), [])

    def test_url_from_instruction_reads_the_url_hint(self):
        import annual_report_pdf as pdf
        instr = "抓取年报 [URL: https://www.sse.com.cn/a/2024.pdf] 保留正文"
        self.assertEqual(pdf.url_from_instruction(instr), "https://www.sse.com.cn/a/2024.pdf")


class TestCaptureFailureFixtures(unittest.TestCase):
    """F2′-2 回放夹具：实机四类失败产物都不得产出可用证据。

    四类分别是：反爬 WAF 载荷、付费墙页（"会员可见"）、无关页（Statista 首页）、
    以及一条**URL 里没有可核实发布日**的短新闻（不透明文章 ID）——最后一条按 A3
    "未知披露时点不能当已验证支持"同样不准入。段落回退能读无标题页面的正向能力
    由 `test_paragraph_fallback_when_page_has_no_headings`（带日期 URL）覆盖。
    """

    FIXTURE = ROOT / "evals" / "fixtures" / "f2p_capture_failures.json"

    def test_real_capture_failures_yield_no_usable_evidence(self):
        import json as _json
        data = _json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        docs = {k: v for k, v in data.items() if not k.startswith("_")}
        self.assertGreaterEqual(len(docs), 4)
        for key, doc in docs.items():
            usable = [r for r in ne.extract_sections(
                doc, periods=[2023, 2024], company="贵州茅台",
                company_id="600519.SH", as_of="2025-04-30")
                if r.get("admission") in ("admitted", "comparison")]
            self.assertEqual(usable, [], f"{key} 不得产出可用证据")


class TestSourceAdmission(unittest.TestCase):
    """A2：来源准入只有一条规则，全入口共用；正文引用不能绕过。"""

    EV = {"records": [
        {"url": "https://news.example/2026/q1", "title": "洋河2026Q1点评",
         "kind": "change_explanation", "source_type": "third_party",
         "has_location": True, "admission": "excluded",
         "validation_status": "after_as_of"},
        {"url": "https://static.cninfo.com.cn/2025-04-03/annual.PDF",
         "title": "洋河股份2024年年度报告", "kind": "business_background",
         "source_type": "issuer_annual_report", "has_location": True,
         "admission": "admitted", "validation_status": "applicable"},
    ]}

    def test_excluded_url_is_rejected_at_every_entry(self):
        """已排除 URL 从叙事证据、正文引用、检索候选任何入口都不再准入。"""
        from narrative_evidence import admission_of, excluded_urls, source_registry
        self.assertEqual(admission_of("https://news.example/2026/q1", evidence=self.EV),
                         "excluded")
        reg = source_registry(["https://news.example/2026/q1"], evidence=self.EV,
                              known_urls=("https://news.example/2026/q1",))
        self.assertEqual(reg["https://news.example/2026/q1"], "excluded")
        self.assertIn("https://news.example/2026/q1", excluded_urls(self.EV))

    def test_unknown_urls_stay_unknown_not_adopted(self):
        """未抓取/仅罗列的 URL：unknown（待核查），不显示为已采用。"""
        from narrative_evidence import admission_of
        self.assertEqual(admission_of("https://never-fetched.example/a"), "unknown")
        self.assertEqual(
            admission_of("https://news.example/2026/q1",
                         fetched_urls=("https://news.example/2026/q1",)),
            "unknown", "抓到了但没有定位/校验结果，仍是 unknown")

    def test_body_citation_of_excluded_url_is_rejected(self):
        """实机反例：excluded URL 出现在模型来源清单时，不得以"正文引用"重新准入。"""
        import report_brief
        body = ("# 报告\n\n洋河 2026 年一季度收入下滑[1]。\n\n## 参考来源\n\n"
                "1. [洋河2026Q1点评](https://news.example/2026/q1)\n")
        data = {"source_label": "东方财富数据中心（A股）",
                "source_url": "https://datacenter-web.eastmoney.com/api?x=1"}
        cits, audit = report_brief._collect_citations(
            "no-such-task", "研究洋河股份", body, data, evidence=self.EV)
        urls = [c["url"] for c in cits]
        self.assertNotIn("https://news.example/2026/q1", urls,
                         "已排除 URL 不得进入采用清单")
        self.assertTrue(any(r["url"] == "https://news.example/2026/q1"
                            for r in audit.get("rejected") or []),
                        "拒绝原因要记录（晚于资料截止日）")

    def test_legit_third_party_body_citation_is_adopted(self):
        """合法第三方引用正例：正文引用 + 已抓取 + 准入通过 → 进采用清单。"""
        import report_brief
        ev = {"records": [
            {"url": "https://news.example/2025-03-12/review", "title": "洋河2024年报解读",
             "kind": "change_explanation", "source_type": "third_party",
             "has_location": True, "admission": "admitted",
             "validation_status": "applicable"},
        ]}
        body = ("# 报告\n\n洋河 2024 年收入下滑[1]。\n\n## 参考来源\n\n"
                "1. [洋河2024年报解读](https://news.example/2025-03-12/review)\n")
        data = {"source_label": "东方财富数据中心（A股）",
                "source_url": "https://datacenter-web.eastmoney.com/api?x=1"}
        cits, _audit = report_brief._collect_citations(
            "no-such-task", "研究洋河股份", body, data, evidence=ev)
        by_url = {c["url"]: c for c in cits}
        self.assertIn("https://news.example/2025-03-12/review", by_url)
        self.assertEqual(by_url["https://news.example/2025-03-12/review"]["type"],
                         "third_party")
        self.assertEqual(by_url["https://news.example/2025-03-12/review"]["admission"],
                         "admitted")


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
        # D1：located 只数"已准入 + 有正文位置"的片段（本夹具四类各一条，无摘要记录）
        self.assertEqual(data["located"], 4)
        self.assertEqual(data["snippet_hints"], 0)
        self.assertEqual(
            data["located"],
            len([r for r in data["records"]
                 if r.get("has_location")
                 and r.get("admission") in ("admitted", "comparison")]),
            "located 必须能由逐条记录重算")
        self.assertEqual([s["url"] for s in data["sources"]], [ISSUER_URL])

    def test_search_snippet_alone_is_not_evidence(self):
        self._write("search_results.json", [
            {"title": "贵州茅台风险因素提示", "url": NEWS_URL,
             "snippet": "风险因素：白酒行业景气度波动。"},
        ])
        data = ne.build(self.tid, periods=[2024], company="贵州茅台")
        self.assertIn(ne.KIND_RISK, data["missing_kinds"],
                      "只有检索摘要、没有正文定位 → 仍算缺口")
        # D1：摘要不进 located（哪怕它被判为可准入），只计 snippet_hints
        self.assertEqual(data["located"], 0)
        self.assertEqual(data["snippet_hints"], 1)
        snippet_only = [r for r in data["records"] if not r["has_location"]]
        self.assertEqual(len(snippet_only), 1)
        self.assertEqual(snippet_only[0]["locator"], "检索摘要（未取得正文定位）")

    def test_same_url_snippet_does_not_double_count(self):
        """同一 URL 已有正文证据时，它的检索摘要不再登记——一个 URL 不能靠摘要复制加覆盖。"""
        doc_text = ("第三节 管理层讨论与分析\n\n一、经营情况讨论与分析\n\n"
                    "报告期内营业收入同比增长15.66%，主要系销量增加所致。\n\n"
                    "（一）主营业务情况\n\n公司主营业务为茅台酒及系列酒的生产与销售。\n\n"
                    "七、财务报表附注\n\n现金流量表附注：经营活动产生的现金流量净额同比增长。")
        self._write("fetch_snapshot.json", [
            {"title": "贵州茅台2024年年度报告", "url": ISSUER_URL, "text": doc_text},
        ])
        self._write("search_results.json", [
            {"title": "贵州茅台2024年年度报告", "url": ISSUER_URL,
             "snippet": "报告期内营业收入1741.44亿元，同比增长15.66%"},
            {"title": "贵州茅台风险因素提示", "url": NEWS_URL,
             "snippet": "风险因素：白酒行业景气度波动。"},
        ])
        data = ne.build(self.tid, periods=[2023, 2024], company="贵州茅台")
        self.assertEqual(data["located"], 3, "三条带定位的小节证据（背景/变化解释/附注）")
        self.assertEqual(data["snippet_hints"], 1, "只剩那条没有正文的新闻摘要")
        self.assertEqual(
            [s["url"] for s in data["sources"] if s["url"] == ISSUER_URL], [ISSUER_URL],
            "来源按 URL 去重后仍是一条")
        self.assertIn(ne.KIND_RISK, data["missing_kinds"],
                      "新闻摘要不得把'风险因素'从缺口里抹掉")

    def test_wrong_subject_document_listed_once_with_reason(self):
        """错主体材料按**文档**记一次（同页多条记录不重复列出），并写明不适用原因。"""
        self._write("fetch_snapshot.json", [
            {"title": "贵州茅台酒股份有限公司2024年年度报告",
             "url": "https://static.cninfo.com.cn/finalpage/2025-04-25/9.PDF",
             "text": "第三节 管理层讨论与分析\n\n2024年度营业收入288.76亿元，"
                     "同比下降12.83%；归母净利润66.73亿元，同比下降33.38%。\n\n"
                     "七、财务报表附注\n\n现金流量表附注：经营活动现金流量净额46.29亿元。"},
        ])
        data = ne.build(self.tid, periods=[2023, 2024], company="洋河股份",
                        company_id="002304.SZ")
        excluded = data["excluded"]
        self.assertEqual(len(excluded), 1,
                         "同一份材料的多条记录只登记一次（否则简报会把一份写成两份）")
        self.assertEqual(excluded[0]["validation_status"], "subject_mismatch")
        self.assertEqual(data["located"], 0, "错主体材料不进证据计数")

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


class TestApiChunkLocation(unittest.TestCase):
    """09-23：公告文本 API 的 `page_index` 是**接口片段**，不是 PDF 实体页。

    没有页码映射时定位只能写 `api_chunk K（字符 a-b）`，不得写"第 N 页"。
    """

    def test_chunk_offsets_produce_api_chunk_locator(self):
        import annual_report_pdf as pdf
        text = ("第三节 管理层讨论与分析\n\n一、经营情况讨论与分析\n\n"
                "2024年度营业收入1741.44亿元，主要系销量增加及产品结构变化所致。\n")
        doc = pdf.doc_from_pages("贵州茅台:2024年年度报告", ISSUER_URL, [text])
        doc["text"] = text
        doc["chunk_offsets"] = [(0, 3)]
        doc.pop("page_offsets", None)
        recs = ne.extract_sections(doc, periods=[2024], company="贵州茅台",
                                   company_id="600519.SH", as_of="2025-04-30")
        self.assertTrue(recs, "夹具应产出记录")
        loc = str(recs[0].get("locator") or "")
        self.assertIn("api_chunk 3", loc)
        self.assertNotIn("第 ", loc, "没有页码映射时不得写页码")
        self.assertEqual(recs[0].get("page"), None)
        self.assertEqual(recs[0].get("chunk"), 3)

    def test_page_offsets_still_win_when_real_pages_exist(self):
        """真 PDF 页码映射仍在时照旧写页码（不因本改动回退）。"""
        import annual_report_pdf as pdf
        text = ("第三节 管理层讨论与分析\n\n一、经营情况讨论与分析\n\n"
                "2024年度营业收入1741.44亿元，主要系销量增加及产品结构变化所致。\n")
        doc = pdf.doc_from_pages("贵州茅台:2024年年度报告", ISSUER_URL, [text, text])
        doc["text"] = text + "\n" + text
        doc["page_offsets"] = [(0, 7), (len(text) + 1, 8)]
        recs = ne.extract_sections(doc, periods=[2024], company="贵州茅台",
                                   company_id="600519.SH", as_of="2025-04-30")
        self.assertTrue(recs)
        self.assertIn("第 7 页", str(recs[0].get("locator") or ""))


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
