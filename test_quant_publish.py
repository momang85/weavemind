# -*- coding: utf-8 -*-
"""Y2 接入选版/交付包：量化读数以**附件**随包，凭**许可门**决定能否对外。

断言按规划 §11.2 与 §12.4 的接入要求：
- 读数是**附件**（`quant/*`），**不改写主文** ⇒ 没有量化读数的工作区，包成员一字不变；
- 每条读数带 `input_kind` 绑定 + `input_fingerprint`/`spec_hash` + `reading_hash`（离线可复算）；
- **许可门**：免费源/个人非商业一律 `allowed=False` 并写原因；**没有读数也不默认放行**；
  任一读数不可对外 ⇒ 整体不可对外（不是"多数通过就行"）；
- 底稿 `quant/quant_detail.md` **只放数字**（正文只放结论），且写明"不是因果/alpha"；
- 坏文件跳过并记录，**不让一个坏文件毁掉整包导出**。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from quant_research import publish as pub  # noqa: E402

RED = "免费源·仅内部试验"


def _er(lic=RED, eligible=False, hash_="h-er"):
    return {"operator": "event_returns_v1", "impl_version": "event_returns/1.0.0",
            "status": "ok", "input_fingerprint": "fp-1", "reading_hash": hash_,
            "delivery_eligible": eligible, "limits": ["不是因果结论：没有识别策略"],
            "input_kind": {"input_kind": "market_history", "dataset_id": "ds-1",
                           "license": lic, "adj_basis": "前复权(qfq)"},
            "readings": [{"label": "定期报告｜2024年年度报告", "event_date": "2025-04-29",
                          "affected_window": ["2025-04-29", "2025-06-03"],
                          "points": [{"offset": -1, "state": "anchor", "excess_return": None},
                                     {"offset": 0, "state": "observed", "excess_return": -0.0022},
                                     {"offset": 1, "state": "observed", "excess_return": -0.0094},
                                     {"offset": 5, "state": "pending", "excess_return": None},
                                     {"offset": 20, "state": "observed",
                                      "excess_return": -0.0599}]}]}


def _bt(lic=RED, eligible=False):
    return {"operator": "backtest_v1", "impl_version": "backtest/1.0.0", "status": "ok",
            "license": lic, "delivery_eligible": eligible, "spec_hash": "sh-1",
            "reading_hash": "h-bt", "audit": {"ok": True}, "input_fingerprint": "fp-bt",
            "input_kind": {"input_kind": "market_history", "dataset_id": "ds-1",
                           "license": lic, "adj_basis": "前复权(qfq)"},
            "metrics": {"start": "2023-01-03", "end": "2026-09-30", "days": 908, "orders": 66,
                        "total_return": -0.383571, "nocost_total_return": -0.381641,
                        "cost_drag": 0.00193, "annualized_return": -0.125649,
                        "max_drawdown": -0.48449, "avg_exposure": 0.983258,
                        "turnover_ratio": 1.197004,
                        "benchmark": {"return": 0.120815, "excess_return": -0.504386}},
            "limits": ["不是策略有效性结论", "不宣称 alpha"]}


class StoreAndPayloadTest(unittest.TestCase):
    def test_no_readings_means_empty_payload(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(pub.payload_bytes(Path(td)), {},
                             "没有量化读数时不得制造任何包成员（零存量影响）")

    def test_unregistered_name_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            r = pub.store_reading(Path(td), "../evil", {})
            self.assertFalse(r["ok"])
            self.assertIn("未登记", r["reason"])

    def test_store_then_payload_contains_manifest_and_detail(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self.assertTrue(pub.store_reading(ws, "event_returns", _er())["ok"])
            self.assertTrue(pub.store_reading(ws, "backtest", _bt())["ok"])
            members = pub.payload_bytes(ws)
            self.assertIn("quant/quant_manifest.json", members)
            self.assertIn("quant/quant_detail.md", members)
            self.assertIn("quant/event_returns.json", members)
            self.assertIn("quant/backtest.json", members)
            man = json.loads(members["quant/quant_manifest.json"].decode("utf-8"))
            self.assertEqual(man["count"], 2)
            self.assertFalse(man["external_delivery"]["allowed"])

    def test_broken_file_is_skipped_and_recorded(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            pub.store_reading(ws, "backtest", _bt())
            (ws / "quant" / "event_returns.json").write_text("{not json", encoding="utf-8")
            members = pub.payload_bytes(ws)
            man = json.loads(members["quant/quant_manifest.json"].decode("utf-8"))
            self.assertEqual(man["count"], 1)
            self.assertIn("quant/event_returns.json", man["unreadable"])


class LicenseGateTest(unittest.TestCase):
    def test_free_trial_blocks_external_delivery(self):
        g = pub.external_delivery_gate({"event_returns": _er(), "backtest": _bt()})
        self.assertFalse(g["allowed"])
        self.assertEqual(sorted(g["blocked_by"]), ["backtest", "event_returns"])
        self.assertIn("不得随包外发", g["reason"])

    def test_no_readings_is_not_auto_allowed(self):
        g = pub.external_delivery_gate({})
        self.assertFalse(g["allowed"])
        self.assertIn("不默认放行", g["reason"])

    def test_one_blocked_reading_blocks_the_whole_package(self):
        g = pub.external_delivery_gate({"event_returns": _er(eligible=True),
                                        "backtest": _bt(eligible=False)})
        self.assertFalse(g["allowed"], "多数通过不等于可以对外")
        self.assertEqual(g["blocked_by"], ["backtest"])

    def test_all_eligible_allows(self):
        g = pub.external_delivery_gate({"event_returns": _er(lic="机构授权", eligible=True),
                                        "backtest": _bt(lic="机构授权", eligible=True)})
        self.assertTrue(g["allowed"])

    def test_gate_reads_license_from_input_kind_or_top_level(self):
        a = pub.external_delivery_gate({"event_returns": _er(eligible=False)})
        b = pub.external_delivery_gate({"backtest": _bt(eligible=False)})
        self.assertFalse(a["allowed"])
        self.assertFalse(b["allowed"])


class DetailMarkdownTest(unittest.TestCase):
    def test_detail_carries_numbers_and_binding(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            pub.store_reading(ws, "event_returns", _er())
            pub.store_reading(ws, "backtest", _bt())
            md = pub.detail_markdown(ws)
            self.assertIn("fp-1", md)
            self.assertIn("h-bt", md)
            self.assertIn("-0.383571", md)       # 数字进底稿
            self.assertIn("待成熟", md)          # pending 不当成 0
            self.assertIn("不是因果结论", md)
            self.assertIn("对外分发", md)
            self.assertNotIn("dataset `None…`", md, "输入绑定缺失时不得渲染成 None")

    def test_detail_empty_without_readings(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(pub.detail_markdown(Path(td)), "")

    def test_detail_marks_failed_audit_as_unusable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            bad = _bt()
            bad["audit"] = {"ok": False}
            pub.store_reading(ws, "backtest", bad)
            md = pub.detail_markdown(ws)
            self.assertIn("结果不可用", md)


class DeliveryChainWiringTest(unittest.TestCase):
    """真接进交付快照 `_freeze_payload`：**没有读数不改包，有读数才多成员**。"""

    def test_snapshot_payload_unchanged_without_quant(self):
        import tempfile
        import delivery_pipeline as dp
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "analysis").mkdir()
            base = dp._freeze_payload("t-quant-none", ws)      # noqa: SLF001
            self.assertFalse([k for k in base if k.startswith("quant/")],
                             "没有量化读数时包成员里不得出现 quant/*")

    def test_snapshot_payload_includes_quant_when_present(self):
        import tempfile
        import delivery_pipeline as dp
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "analysis").mkdir()
            pub.store_reading(ws, "event_returns", _er())
            got = dp._freeze_payload("t-quant-yes", ws)        # noqa: SLF001
            self.assertIn("quant/quant_manifest.json", got)
            self.assertIn("quant/quant_detail.md", got)
            man = json.loads(got["quant/quant_manifest.json"].decode("utf-8"))
            self.assertFalse(man["external_delivery"]["allowed"],
                             "免费源读数不得被判为可对外")


if __name__ == "__main__":
    unittest.main()
