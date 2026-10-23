"""P0-C 契约链：研究意图判定与交付状态（free text 也可公司研究）。

实机反例（2026-10-22 诊断快照）：自由文本提交的「研究伊利股份」因
`research_request_json` 缺失被归为 `not_applicable`（"非研究任务"），研究检查
整体跳过——文本请求一样可以是公司研究，缺的只是契约字段。

本文件钉住：
- 意图四分：`general` / `company_research` / `out_of_scope`，状态
  `not_applicable` / `needs_contract` / `in_scope` / `out_of_scope`；
- 缺字段 → 问题清单（不编造公司/期间/口径，不代替用户选择）；
- 首期边界显式：A 股非金融、两期、无交易/授信/目标价；
- 界外请求给解释与人工资料路径；历史无契约任务只读标注 + 明确修复建议
  （不静默回填）；
- `delivery_pipeline.research_state`：文本契约缺失 → `contract_missing`，
  一般任务维持 `not_applicable`。

测试只读本地夹具，无模型调用、无网络；DB 仅测试自建的临时库。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import research_intent as ri


class TestIntentAssessment(unittest.TestCase):
    """意图与状态四分；同一判据，没有公司关键词白名单。"""

    def test_free_text_company_research_is_not_not_applicable(self):
        a = ri.assess("研究伊利股份")
        self.assertEqual(a["intent"], "company_research")
        self.assertEqual(a["status"], "needs_contract",
                         "自由文本研究不因 research_request_json 缺失被归为非研究")

    def test_free_text_with_code_and_periods_is_needs_contract(self):
        a = ri.assess("研究伊利股份 600887 2023 与 2024 年度经营")
        self.assertEqual(a["intent"], "company_research")
        self.assertEqual(a["status"], "needs_contract")
        self.assertTrue(a["missing"], "缺契约字段要列出")
        self.assertTrue(a["questions"], "缺字段要给问题清单（不编答案）")

    def test_in_scope_request_is_in_scope(self):
        a = ri.assess("研究伊利股份 600887 2023 与 2024 年度经营",
                      stored_request={"company": "伊利股份", "company_id": "600887",
                                      "market": "A", "periods": [2023, 2024],
                                      "as_of": "2025-04-30", "caliber": "合并报表"})
        self.assertEqual(a["status"], "in_scope")
        self.assertEqual(a["intent"], "company_research")

    def test_hk_us_and_financial_industry_are_out_of_scope(self):
        for goal in ("研究腾讯控股 00700.HK 两期经营",
                     "研究苹果公司 美股 2023 与 2024 财年",
                     "研究招商银行 600036 2023 与 2024 年度"):
            a = ri.assess(goal)
            self.assertEqual(a["intent"], "out_of_scope", goal)
            self.assertEqual(a["status"], "out_of_scope", goal)
            self.assertTrue(a["reason"], goal)
            self.assertTrue(a["boundary"], goal)

    def test_unmarked_foreign_company_asks_instead_of_guessing(self):
        """没写市场标记的外国公司不猜：按公司研究请求补契约（问用户），不硬判界外。"""
        a = ri.assess("研究苹果公司 2023 与 2024 财年")
        self.assertEqual(a["intent"], "company_research")
        self.assertEqual(a["status"], "needs_contract")

    def test_three_periods_is_out_of_scope(self):
        a = ri.assess("研究伊利股份 600887 2022 2023 与 2024 三个年度",
                      stored_request={"company": "伊利股份", "company_id": "600887",
                                      "market": "A", "periods": [2022, 2023, 2024],
                                      "as_of": "2025-04-30", "caliber": "合并报表"})
        self.assertEqual(a["status"], "out_of_scope")
        self.assertIn("两期", a["boundary"] or "")

    def test_general_task_stays_not_applicable(self):
        a = ri.assess("总结一下这段文字的要点，列成条目")
        self.assertEqual(a["intent"], "general")
        self.assertEqual(a["status"], "not_applicable")

    def test_stored_request_source_is_recorded(self):
        a = ri.assess("研究伊利股份 600887 2023 与 2024 年度经营",
                      stored_request={"company": "伊利股份", "company_id": "600887",
                                      "market": "A", "periods": [2023, 2024],
                                      "as_of": "2025-04-30", "caliber": "合并报表"})
        self.assertEqual(a["source"], "stored")
        b = ri.assess("研究伊利股份 600887 2023 与 2024 年度经营")
        self.assertEqual(b["source"], "text")

    def test_no_fabricated_fields_in_questions(self):
        """问题清单不得替用户回答（不编公司/期间/口径）。"""
        a = ri.assess("研究一下这家公司")
        self.assertEqual(a["intent"], "company_research")
        for q in a["questions"]:
            self.assertIsInstance(q, str)
            self.assertTrue(q.strip())

    def test_backfill_is_never_silent(self):
        """历史无契约任务：只读标注 + 明确修复建议，不静默回填。"""
        a = ri.assess("研究伊利股份 600887 2023 与 2024 年度经营")
        self.assertEqual(a["backfill"], "not_attempted")
        self.assertTrue(a["repair"], "要给明确修复建议")


class TestDeliveryStateContract(unittest.TestCase):
    """`delivery_pipeline.research_state`：文本契约缺失 → contract_missing。

    注意两个键名：`research_state` 返回的主键是 `state`（历史口径），
    意图信息挂在 `intent` / `intent_status`。
    """

    def _state(self, goal, research_request=None):
        """`research_state` 从任务库读契约（`task_state.read_task(task_id)`）。

        测试隔离：临时库 + 直接改 `task_state.DB_PATH`（仓库既有测试同款做法，
        见 `test_task_persistence.py`），不碰真实 agents.db。
        """
        import delivery_pipeline as dp
        import task_state as ts
        old = ts.DB_PATH
        with tempfile.TemporaryDirectory() as td:
            ts.DB_PATH = str(Path(td) / "agents.db")
            try:
                ts.claim_receipt("t-intent", goal,
                                 research_request=research_request or None)
                return dp.research_state("t-intent", goal, None)
            finally:
                ts.DB_PATH = old

    def test_text_only_research_returns_contract_missing(self):
        st = self._state("研究伊利股份 600887 2023 与 2024 年度经营")
        self.assertEqual(st["state"], "contract_missing")
        self.assertEqual(st.get("intent_status"), "needs_contract")
        self.assertTrue(st.get("missing_fields"))
        self.assertTrue(st.get("questions"))
        self.assertEqual(st.get("backfill"), "not_attempted")

    def test_general_task_keeps_not_applicable(self):
        st = self._state("总结一下这段文字的要点")
        self.assertEqual(st["state"], "not_applicable")

    def test_full_contract_returns_in_scope(self):
        st = self._state("研究伊利股份 600887 2023 与 2024 年度经营",
                         research_request={"company": "伊利股份",
                                           "company_id": "600887", "market": "A",
                                           "periods": [2023, 2024],
                                           "as_of": "2025-04-30",
                                           "caliber": "合并报表"})
        self.assertEqual(st["state"], "in_scope_no_structure")
        self.assertEqual(st.get("intent_status"), "in_scope")


class TestHistoricalNoContractTask(unittest.TestCase):
    """历史无契约任务：读取侧只读标注（研究检查按文本要素降级），不静默回填契约。"""

    def test_read_only_annotation_with_repair_suggestion(self):
        a = ri.assess("研究伊利股份 600887 2023 与 2024 年度经营")
        self.assertEqual(a["backfill"], "not_attempted")
        blob = (a["repair"] or "") + (a["reason"] or "")
        self.assertTrue(
            ("补充契约" in blob) or ("表单" in blob) or ("重新提交" in blob),
            f"修复建议要说清下一步，实际：{blob!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
