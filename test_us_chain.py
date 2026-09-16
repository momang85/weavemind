# -*- coding: utf-8 -*-
"""MKT-P0-3：美股（SEC EDGAR）链路接进 target + 数字-主体判定 + 固定夹具（离线，不联网）。

背景（市场评估的实测根因）：SEC 适配器本身可用、允许清单/注入/验收也都已支持 US，
但**公司名提取只认中文**——英文/代码驱动的目标（"分析 WBD 的财报"）提取不到主体，
`route_structured` 直接返回 None，于是工作区里从来没有 `financials.json`。

本文件用**固定 XBRL 夹具**（合成数据，不是任何现实公司的真实财报）验证：
1. 英文/代码目标能提取主体，`route_structured` 真的走到 SEC 分支；
2. 预载把 `financials.json` 写进任务工作区（含 USD/亿美元 口径）；
3. 报告里的金额能从该夹具溯源，金额溯源率 **> 70%**（与 A 股同级口径，不降阈值）。
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

import workspace as ws_mod  # noqa: E402
from task_classifier import _extract_company, classify_task  # noqa: E402

# ── 固定夹具：合成的 10-K 年度数据（**非现实公司事实**，仅用于链路与判定回归）──
FIXTURE_COMPANY = "Contoso Semiconductors Inc."
FIXTURE_TICKER = "CSTX"
FIXTURE_ROWS = [
    {"year": 2021, "report_type": "10-K", "revenue": 210.5, "net_profit": 31.2,
     "gross_profit": 105.2, "gross_margin": 49.98, "operating_profit": 42.1,
     "total_assets": 320.4, "total_liabilities": 121.9,
     "operating_cashflow": 55.3, "basic_eps": 2.1},
    {"year": 2022, "report_type": "10-K", "revenue": 245.8, "net_profit": 36.9,
     "gross_profit": 121.4, "gross_margin": 49.39, "operating_profit": 48.7,
     "total_assets": 351.2, "total_liabilities": 130.5,
     "operating_cashflow": 61.8, "basic_eps": 2.4},
    {"year": 2023, "report_type": "10-K", "revenue": 268.1, "net_profit": 40.2,
     "gross_profit": 131.9, "gross_margin": 49.2, "operating_profit": 52.4,
     "total_assets": 372.9, "total_liabilities": 133.1,
     "operating_cashflow": 66.2, "basic_eps": 2.6},
]

FIXTURE_PAYLOAD = {
    "financials": FIXTURE_ROWS,
    "metadata": {"source": "sec_edgar", "company": FIXTURE_COMPANY,
                 "ticker": FIXTURE_TICKER, "cik": "0000000001",
                 "currency": "USD", "unit": "亿美元",
                 "retrieved_at": "2026-09-17 00:00:00", "annual_count": 3},
    "raw": {"url": "https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json",
            "text": "（夹具：合成的 XBRL companyfacts 片段）"},
}


class TestSubjectExtraction(unittest.TestCase):
    """英文名/代码要能被认成主体；中文行为不变；噪声不得当公司名。"""

    def test_english_ticker_and_name(self):
        self.assertEqual(_extract_company("分析 WBD 的财报与营收"), "WBD")
        self.assertEqual(_extract_company("AAPL 营收与净利润分析"), "AAPL")
        self.assertEqual(_extract_company("Analyze Apple's revenue and net income"),
                         "Apple")
        self.assertEqual(_extract_company("请给微软（MSFT）最近一期财报做财务分析"), "微软")
        self.assertEqual(_extract_company("苹果(AAPL) 最近三年营收"), "苹果")
        self.assertEqual(_extract_company(f"梳理{FIXTURE_COMPANY}近三年营收"),
                         FIXTURE_COMPANY)

    def test_chinese_behavior_unchanged(self):
        self.assertEqual(
            _extract_company("梳理贵州茅台2025年三季报核心财务数据（营收/净利润/毛利率/现金流）"),
            "贵州茅台")
        self.assertEqual(_extract_company("分析贵州茅台近三年营收"), "贵州茅台")

    def test_noise_is_not_a_company(self):
        for text in ("美股科技公司近三年营收对比", "生成一个贪吃蛇游戏",
                     "帮我看看这个季度的行业趋势"):
            got = _extract_company(text)
            self.assertNotIn(got, ("一期", "三年", "美股科技", "这个季度"), text)

    def test_classify_reports_us_company(self):
        cls = classify_task("分析 AAPL 近三年的营收与净利润")
        self.assertEqual(cls.get("company"), "AAPL")


class TestRouterReachesSecBranch(unittest.TestCase):
    """英文/代码目标必须真的走到 SEC 分支（这是"接进 target"的直接证据）。"""

    def test_route_structured_calls_fetch_sec_for_ticker_goal(self):
        import adapters.router as router

        calls: dict = {}

        def _resolve(name):
            calls["resolve"] = name
            return {"market": "US", "stock_code": FIXTURE_TICKER,
                    "name": FIXTURE_COMPANY, "quote_id": FIXTURE_TICKER}

        def _fetch_sec(company, ticker, *a, **k):
            calls["fetch"] = (company, ticker)
            return dict(FIXTURE_PAYLOAD)

        with mock.patch.object(router, "resolve_company", _resolve), \
                mock.patch.object(router, "fetch_sec", _fetch_sec), \
                mock.patch.object(router, "_market_source_available", lambda *a, **k: True,
                                  create=True):
            data = router.route_structured("分析 AAPL 近三年的营收与净利润", scope="e2e")

        self.assertIsNotNone(data, "英文/代码目标必须能命中结构化链路")
        # 真实 router 的返回形状：适配器载荷 + classification/resolution（顶层无 source，
        # 下游靠 `financials` 列表判定为财务；这与 A 股链路一致，不要按臆想的字段断言）
        self.assertEqual(len(data.get("financials") or []), len(FIXTURE_ROWS))
        self.assertEqual(str((data.get("metadata") or {}).get("source")), "sec_edgar")
        self.assertEqual(str((data.get("metadata") or {}).get("currency")), "USD")
        self.assertEqual(calls.get("fetch", ("", ""))[1], FIXTURE_TICKER,
                         "US 分支必须把 ticker 交给 SEC 适配器")


class TestFixturePreloadWritesFinancials(unittest.TestCase):
    """预载把夹具写进任务工作区（= 生产里 `financials.json` 的来源）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_us_"))
        self.old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(self._restore)

    def _restore(self):
        ws_mod.WORKSPACE_ROOT = self.old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preload_writes_financials_with_us_unit(self):
        from orchestrator_v2 import OrchestratorV2
        import adapters.router as router

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._task_structured_data = {}
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        with mock.patch.object(router, "route_structured",
                               lambda goal, scope=None: dict(FIXTURE_PAYLOAD)):
            preloaded = o._structured_data_preload("t-us-1", "分析 AAPL 近三年营收", "default")
        self.assertTrue(preloaded, "预载应返回结构化数据")
        fin = ws_mod.task_project_dir("t-us-1", "default") / "financials.json"
        self.assertTrue(fin.exists(), "预载必须写出 financials.json")
        payload = json.loads(fin.read_text(encoding="utf-8"))
        self.assertEqual(str(payload["metadata"]["unit"]), "亿美元")
        self.assertEqual(len(payload["financials"]), len(FIXTURE_ROWS))

    def test_injection_block_is_built_from_fixture(self):
        from orchestrator_v2 import OrchestratorV2
        import adapters.router as router

        o = OrchestratorV2.__new__(OrchestratorV2)
        o._task_structured_data = {}
        o._messaging = mock.MagicMock()
        o._now_iso = lambda: "T"
        with mock.patch.object(router, "route_structured",
                               lambda goal, scope=None: dict(FIXTURE_PAYLOAD)):
            o._structured_data_preload("t-us-2", "分析 AAPL 近三年营收", "default")
            block = o._structured_injection("t-us-2")
        self.assertIn("结构化财务数据", block)
        self.assertIn("268.1", block, block[:400])


class TestUsAmountTraceability(unittest.TestCase):
    """判据：报告金额能从 SEC 夹具溯源，金额溯源率 **> 70%**（不降阈值）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_us2_"))
        self.old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(self._restore)
        proj = ws_mod.task_project_dir("t-us-3", "default")
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "financials.json").write_text(
            json.dumps(FIXTURE_PAYLOAD, ensure_ascii=False), encoding="utf-8")

    def _restore(self):
        ws_mod.WORKSPACE_ROOT = self.old_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _accept(self, report: str) -> dict:
        import acceptance_checker as ac

        ws = ws_mod.task_workspace("t-us-3", "default")
        return ac.run_acceptance("t-us-3", "分析 AAPL 近三年的营收与净利润", report, ws)

    def test_amount_rate_above_70_with_fixture_numbers(self):
        report = (
            "# Contoso 半导体 2021-2023 财年核心数据（夹具）\n\n"
            "## 核心指标\n\n| 指标 | 数值 | 口径/年份 | 来源 |\n|---|---|---|---|\n"
            "| 营业收入 | 268.1 亿美元 | 2023 财年 | [1] |\n"
            "| 营业收入 | 245.8 亿美元 | 2022 财年 | [1] |\n"
            "| 营业收入 | 210.5 亿美元 | 2021 财年 | [1] |\n"
            "| 净利润 | 40.2 亿美元 | 2023 财年 | [1] |\n"
            "| 净利润 | 36.9 亿美元 | 2022 财年 | [1] |\n"
            "| 经营现金流 | 66.2 亿美元 | 2023 财年 | [1] |\n"
            "| 总资产 | 372.9 亿美元 | 2023 财年 | [1] |\n"
            "| 总负债 | 133.1 亿美元 | 2023 财年 | [1] |\n"
            "| 现金储备 | 999.9 亿美元 | 2023 财年 | [1] |\n\n"
            "## 数据时效\n\n数据截至 2023-12-31 财年 10-K 披露日，日终更新。\n\n"
            "## 参考来源\n\n1. [SEC EDGAR 10-K（夹具）](https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json)\n\n"
            "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，不构成任何投资建议；"
            "数据来源于公开渠道（夹具为合成数据），可能存在延迟或误差；据此操作风险自担。\n"
        )
        r = self._accept(report)
        nt = (r.get("checks") or {}).get("number_traceability") or {}
        rate = float(nt.get("amount_rate") or 0.0)
        self.assertGreater(rate, 0.70,
                           f"金额溯源率应 >70%（实测 {rate:.2f}，总数 {nt.get('amount_total')}）："
                           f"{nt.get('amount_traceable')}/{nt.get('amount_total')}")
        # 阈值没有被本批下调
        self.assertEqual(ac_threshold(), 0.7)

    def test_wrong_number_is_not_traceable(self):
        report = (
            "# Contoso 半导体 2023 财年\n\n营业收入 1234.5 亿美元。\n\n"
            "## 数据时效\n\n数据截至 2023-12-31。\n\n"
            "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
            "不构成任何投资建议；数据来源于公开渠道。\n"
        )
        nt = (self._accept(report).get("checks") or {}).get("number_traceability") or {}
        self.assertEqual(int(nt.get("amount_traceable") or 0), 0,
                         "夹具里没有的数字不得算可溯源")


def ac_threshold() -> float:
    import acceptance_checker as ac

    return float(ac._TRACEABILITY_THRESHOLDS.get("financial"))


if __name__ == "__main__":
    unittest.main()
