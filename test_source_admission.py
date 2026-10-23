"""P0-C：来源准入（候选 / 待核验 / 采用 / 剔除）与契约链的回归夹具。

实机反例（2026-10-22 诊断快照 `docs/evidence/20261022-runtime-diagnosis/`）：

1. **传奇影业**（task 研究传奇影业）：检索把「2025 年国民经济和社会发展统计公报」
   当成了公司材料，域名权威 + HTTP 200 + 正文非空，于是"看起来可用"。
2. **伊利股份 600887**（task ui-b23642e5de）：三条候选全是 SKF 6008 深沟球轴承、
   Windows 事件日志 6008、汉语词典页；`_filter_source_items` 的 fail-open 整批放行。
3. **同一条伊利任务的正确候选**（年报）被旧的主体启发式误杀（全称带"集团"后缀）。

本文件钉住四条准入判据（主体 / 期间 / 文档类型或用途 / 资料截止日）与两种不误伤：
待核验（元数据不足 → 有界验证）与"判不准 fail-open"（无主体锚点 → 不减损来源）。
判据里**没有**任何公司关键词白名单/站点黑名单：传奇影业与伊利走同一条规则。

测试只读本地夹具，无模型调用、无网络、无 DB/Redis。
"""
import json
import unittest

import source_admission as sa
from orchestrator_v2 import OrchestratorV2 as O

# ── 两份契约（都来自真实请求形态）────────────────────────────────────
LEGENDARY = {"company": "传奇影业", "company_id": "", "periods": [2023, 2024],
             "as_of": "2025-06-30", "market": "A", "caliber": "合并报表"}
YILI = {"company": "伊利股份", "company_id": "600887", "periods": [2023, 2024],
        "as_of": "2025-04-30", "market": "A", "caliber": "合并报表"}

NBS_BULLETIN = {
    "title": "2025 年国民经济和社会发展统计公报",
    "url": "https://www.stats.gov.cn/sj/zxfb/202602/t20260228_1.html",
    "snippet": "全年国内生产总值 134.9 万亿元，比上年增长 5.0%；居民消费价格指数上涨 0.2%",
}
SKF_BEARING = {
    "title": "6008 - 深沟球轴承 | SKF",
    "url": "https://www.skf.com.cn/products/bearings/6008",
    "snippet": "6008 深沟球轴承 产品参数 型号 下载 报价单",
}
YILI_ANNUAL = {
    "title": "内蒙古伊利实业集团股份有限公司 2023 年年度报告",
    "url": "https://static.cninfo.com.cn/finalpage/2024-04-30/1.PDF",
    "snippet": "营业收入 归母净利润 经营活动产生的现金流量净额",
}
YILI_UNDATED = {
    "title": "伊利股份 2024 年年度报告",
    "url": "https://www.cninfo.com.cn/new/disclosure/annual_report.pdf",
    "snippet": "营业收入 归母净利润 600887",
}

YILI_ANNUAL_BODY = ("内蒙古伊利实业集团股份有限公司 2023 年年度报告 第一节 重要提示 "
                    "营业收入 1257.58 亿元 归属于上市公司股东的净利润 104.29 亿元")
SKF_BODY = ("SKF 6008 深沟球轴承 Single row deep groove ball bearings 产品参数 型号 "
            "尺寸 内径 40mm 外径 68mm 下载 报价单 登录 首页 导航")
NBS_BODY = ("2025 年国民经济和社会发展统计公报 国家统计局 全年国内生产总值 1349084 亿元 "
            "第一产业增加值 第二产业增加值 居民消费价格 全国居民人均可支配收入")


class TestSubjectAndPurpose(unittest.TestCase):
    """判据一 + 判据三：主体与用途。宏观统计材料不得当公司财务证据（反例 1）。"""

    def test_macro_bulletin_is_rejected_for_company_research(self):
        """传奇影业的错误来源：权威域名 + HTTP 200 + 正文非空 ≠ 可用于本公司。"""
        res = sa.admit(NBS_BULLETIN, LEGENDARY, body=NBS_BODY)
        self.assertEqual(res["stage"], sa.STAGE_REJECTED)
        self.assertEqual(res["purpose"], sa.PURPOSE_MACRO)
        self.assertEqual(res["usable_as"], "background",
                         "宏观材料只能作背景，不能作公司财务证据")
        self.assertTrue(sa.reason_text(res))

    def test_macro_bulletin_without_body_is_pending_not_admitted(self):
        """未取正文时也不得直接采用：待核验（有界验证），不是"无关"。"""
        res = sa.admit(NBS_BULLETIN, LEGENDARY)
        self.assertEqual(res["stage"], sa.STAGE_PENDING)
        self.assertTrue(res["need_body_verify"])

    def test_bearing_product_page_is_rejected_after_body_check(self):
        """伊利 600887 的'6008'歧义：轴承页正文核验后仍无主体/代码/披露形态。"""
        res = sa.admit(SKF_BEARING, YILI, body=SKF_BODY)
        self.assertEqual(res["stage"], sa.STAGE_REJECTED)
        self.assertIn("非本公司披露", sa.reason_text(res))

    def test_full_legal_name_is_admitted(self):
        """正例：上市主体**全称**（带"集团"）是本公司披露，不得被形态启发式误杀。"""
        res = sa.admit(YILI_ANNUAL, YILI, body=YILI_ANNUAL_BODY)
        self.assertEqual(res["stage"], sa.STAGE_ADMITTED)
        self.assertEqual(res["purpose"], sa.PURPOSE_COMPANY_FINANCIAL)
        self.assertEqual(res["usable_as"], "evidence")

    def test_group_company_is_not_the_listed_subject(self):
        """母/集团公司名（无"股份有限公司"形态）不是本上市公司：错主体。"""
        cand = {"title": "江苏洋河集团有限公司 2025 年跟踪评级报告",
                "url": "https://static.sse.com.cn/disclosure/bond/x.pdf",
                "snippet": "评级"}
        res = sa.admit(cand, {"company": "洋河股份", "company_id": "002304",
                              "periods": [2023, 2024], "as_of": "2025-04-30"})
        self.assertEqual(res["stage"], sa.STAGE_REJECTED)
        self.assertEqual(res["subject_state"], "mismatch")


class TestPeriodAndAsOf(unittest.TestCase):
    """判据二 + 判据四：期间与资料截止日。"""

    def test_report_period_outside_contract_is_rejected(self):
        cand = dict(YILI_ANNUAL)
        cand["url"] = "https://static.cninfo.com.cn/finalpage/2021-04-28/6.PDF"
        cand["title"] = "伊利股份 2020 年年度报告"
        res = sa.admit(cand, YILI, body="伊利股份 2020 年年度报告 营业收入")
        self.assertEqual(res["stage"], sa.STAGE_REJECTED)
        self.assertIn("报告期早于契约期间", sa.reason_text(res))

    def test_published_after_as_of_is_rejected(self):
        cand = {"title": "伊利股份 2025 年半年度报告",
                "url": "https://static.cninfo.com.cn/finalpage/2026-08-29/9.PDF",
                "snippet": "半年度报告"}
        res = sa.admit(cand, YILI)
        self.assertEqual(res["stage"], sa.STAGE_REJECTED)


class TestPipelinePartition(unittest.TestCase):
    """把准入接到来源桶：采用 / 待核验 / 剔除三分，下游上下文同步。"""

    def test_mixed_bucket_keeps_only_admitted(self):
        kept, dropped = O._partition_source_items(
            "研究伊利股份 600887 2023 与 2024 年度经营",
            [NBS_BULLETIN, SKF_BEARING, YILI_ANNUAL], contract=YILI)
        self.assertEqual([it["url"] for it in kept], [YILI_ANNUAL["url"]])
        self.assertEqual(len(dropped), 2)
        for it in dropped:
            self.assertTrue(str(it.get("_exclude_reason") or ""), "每条剔除都要带理由")

    def test_all_rejected_bucket_is_empty_not_fail_open(self):
        """全部不可用 → 空清单（如实"无合格资料"），不再整批兜底。"""
        kept, dropped = O._partition_source_items(
            "研究传奇影业 两期经营", [NBS_BULLETIN], contract=LEGENDARY)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)

    def test_pending_material_does_not_enter_bucket_but_stays_verifiable(self):
        kept, dropped = O._partition_source_items(
            "研究伊利股份 600887 2023 与 2024 年度经营", [YILI_UNDATED],
            contract=YILI)
        self.assertEqual(kept, [], "未核验时点前不采用")
        self.assertEqual(len(dropped), 1)
        self.assertEqual(str((dropped[0].get("_admission") or {}).get("stage")), "pending")
        # 有界验证：抓取选择仍可以选它（不是"无关"，不封锁）
        picked = O._pick_fetch_url([YILI_UNDATED],
                                   "研究伊利股份 600887 2023 与 2024 年度经营",
                                   role="annual_report", contract=YILI)
        self.assertEqual(picked, YILI_UNDATED["url"], "待核验材料必须还能被抓来核验")

    def test_fetch_selection_never_picks_macro_bulletin(self):
        """宏观统计公报不是公司财务证据：即便"待核验"也不在这条通道抓。"""
        picked = O._pick_fetch_url([NBS_BULLETIN],
                                   "研究伊利股份 600887 2023 与 2024 年度经营",
                                   role="annual_report", contract=YILI)
        self.assertIsNone(picked)
        # 轴承产品页元数据判不出主体 → 允许**有界验证**（抓一次正文复核），
        # 但正文核验后仍会被拒（见 TestSubjectAndPurpose）。
        picked2 = O._pick_fetch_url([SKF_BEARING],
                                    "研究伊利股份 600887 2023 与 2024 年度经营",
                                    role="annual_report", contract=YILI)
        self.assertIn(picked2, (None, SKF_BEARING["url"]))

    def test_fetch_selection_prefers_admitted(self):
        picked = O._pick_fetch_url([NBS_BULLETIN, SKF_BEARING, YILI_ANNUAL],
                                   "研究伊利股份 600887 2023 与 2024 年度经营",
                                   role="annual_report", contract=YILI)
        self.assertEqual(picked, YILI_ANNUAL["url"])

    def test_vague_goal_still_fails_open(self):
        """判不准（无主体锚点/无契约）时维持 fail-open：不误删来源。"""
        kept, dropped = O._partition_source_items("总结一下要点，列成条目。",
                                                  [SKF_BEARING, NBS_BULLETIN])
        self.assertEqual(len(kept), 2)
        self.assertEqual(dropped, [])


class TestDownstreamContext(unittest.TestCase):
    """下游一致：被拒来源不得进入模型上下文（不只是来源清单）。"""

    def _split(self, cands, contract, goal, bodies=None):
        import source_admission as _sa
        batch = _sa.admit_many(cands, contract, bodies=bodies or {})
        kept, dropped = O._partition_source_items(goal, cands, contract=contract)
        return kept, dropped, batch

    def test_rejected_urls_never_reach_step_context(self):
        goal = "研究伊利股份 600887 2023 与 2024 年度经营"
        bodies = {NBS_BULLETIN["url"]: NBS_BODY, SKF_BEARING["url"]: SKF_BODY,
                  YILI_ANNUAL["url"]: YILI_ANNUAL_BODY}
        batch = sa.admit_many([NBS_BULLETIN, SKF_BEARING, YILI_ANNUAL], YILI,
                              bodies=bodies)
        ctx, all_rej = O._step_context_after_admission(
            [NBS_BULLETIN, SKF_BEARING, YILI_ANNUAL],
            batch["admitted"], batch["pending"] + batch["rejected"])
        self.assertFalse(all_rej)
        urls = [it["url"] for it in ctx]
        self.assertEqual(urls, [YILI_ANNUAL["url"]])
        blob = json.dumps(ctx, ensure_ascii=False)
        self.assertNotIn(NBS_BULLETIN["url"], blob)
        self.assertNotIn(SKF_BEARING["url"], blob)

    def test_all_rejected_marks_step_failed(self):
        ctx, all_rej = O._step_context_after_admission(
            [NBS_BULLETIN, SKF_BEARING], [],
            [dict(NBS_BULLETIN, _admission={"stage": "rejected"}),
             dict(SKF_BEARING, _admission={"stage": "rejected"})])
        self.assertIsNone(ctx)
        self.assertTrue(all_rej, "全部不适用要如实判失败，不给'检索成功'的外观")

    def test_no_excluded_items_means_no_rewrite(self):
        ctx, all_rej = O._step_context_after_admission(
            [YILI_ANNUAL], [dict(YILI_ANNUAL, _admission={"stage": "admitted"})], [])
        self.assertIsNone(ctx)
        self.assertFalse(all_rej)


class TestReasonVocabulary(unittest.TestCase):
    """四种状态与理由必须可区分：候选 / 待核验 / 采用 / 剔除。"""

    def test_stages_and_reasons_are_distinct(self):
        admitted = sa.admit(YILI_ANNUAL, YILI, body=YILI_ANNUAL_BODY)
        pending = sa.admit(YILI_UNDATED, YILI)
        rejected = sa.admit(SKF_BEARING, YILI, body=SKF_BODY)
        self.assertEqual({admitted["stage"], pending["stage"], rejected["stage"]},
                         {sa.STAGE_ADMITTED, sa.STAGE_PENDING, sa.STAGE_REJECTED})
        texts = {sa.reason_text(x) for x in (admitted, pending, rejected)}
        self.assertEqual(len(texts), 3, "三种结论的理由文本不得混同")

    def test_contract_accepts_request_object_and_dict(self):
        """`contract_fields` 接受 `ResearchRequest` 对象或 dict，同一形状出同一判据。"""
        from facts import ResearchRequest
        req = ResearchRequest(goal="研究伊利股份 600887 2023 与 2024 年度经营",
                              company="伊利股份", company_id="600887",
                              market="A", periods=[2023, 2024],
                              as_of="2025-04-30", caliber="合并报表")
        a = sa.contract_fields(req)
        b = sa.contract_fields(YILI)
        self.assertEqual(a["company_id"], b["company_id"])
        self.assertEqual(a["periods"], b["periods"])
        self.assertEqual(a["company"], b["company"])

    def test_admission_is_stable_across_repeat_runs(self):
        first = sa.admit(YILI_ANNUAL, YILI, body=YILI_ANNUAL_BODY)
        second = sa.admit(YILI_ANNUAL, YILI, body=YILI_ANNUAL_BODY)
        self.assertEqual(first["stage"], second["stage"])
        self.assertEqual(first["reasons"], second["reasons"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
