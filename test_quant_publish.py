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

    def test_unreadable_member_blocks_even_when_others_are_eligible(self):
        """10-07 复核 §随样品最小收口-2 反例：**授权读数 + 坏文件**曾被判 allowed=True。

        坏的/未登记的量化成员**判不出许可** ⇒ 不默认放行（fail-closed）：
        内部快照保留原件、财报正文照常交付，只阻止量化附件外发。
        """
        g = pub.external_delivery_gate(
            {"event_returns": _er(lic="机构授权", eligible=True),
             "_unreadable": ["quant/backtest.json"]})
        self.assertFalse(g["allowed"], "一个坏成员不得被'其它读数都合格'掩盖")
        self.assertIn("quant/backtest.json", g["blocked_by"])
        self.assertIn("判不出许可", g["reason"])
        self.assertIn("内部快照保留原件", g["reason"])

    def test_unknown_member_key_blocks(self):
        """未登记的成员键（不是已知读数）同样 fail-closed。"""
        g = pub.external_delivery_gate(
            {"event_returns": _er(lic="机构授权", eligible=True),
             "_mystery": {"whatever": 1}})
        self.assertFalse(g["allowed"])
        self.assertIn("_mystery", g["blocked_by"])


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


class ExternalDeliveryProjectionTest(unittest.TestCase):
    """复核 §3D：**外发许可拒绝时，无权外发的量化附件不进入交付包，财报正文仍可交付**。

    内部快照/工作区 `quant/` 原件保留（内部保存成立）；交付包（对外交付面）里只留一份
    限制说明 + 被移出成员的 sha256。判据复用既有许可门，不新写一套许可解释。
    """

    MD = b"# \xe4\xba\xa4\xe4\xbb\x98\xe6\xad\xa3\xe6\x96\x87\n"

    def _members(self, ws):
        import delivery_pipeline as dp
        pub.store_reading(ws, "event_returns", _er())
        pub.store_reading(ws, "backtest", _bt())
        return dp, dp._freeze_payload("t-ext", ws, md_bytes=self.MD)      # noqa: SLF001

    def test_restricted_quant_is_removed_but_report_stays(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "analysis").mkdir()
            dp, members = self._members(ws)
            self.assertIn("quant/backtest.json", members)   # 内部快照仍带附件
            proj, info = dp.external_delivery_projection(members)
            self.assertFalse(info["allowed"])
            self.assertEqual(info["schema"], "weavemind.external_delivery/1")
            self.assertNotIn("quant/backtest.json", proj)
            self.assertNotIn("quant/event_returns.json", proj)
            self.assertNotIn("quant/quant_detail.md", proj, "衍生底稿同样按用途许可判，不天然放行")
            self.assertNotIn("quant/quant_manifest.json", proj)
            self.assertEqual(proj["reports/report.md"], self.MD, "财报正文必须仍可交付")
            self.assertEqual(sorted(info["withheld"]),
                             ["quant/backtest.json", "quant/event_returns.json",
                              "quant/quant_detail.md", "quant/quant_manifest.json"])
            self.assertTrue(all(len(h) == 64 for h in info["withheld"].values()))
            note = proj[dp.EXTERNAL_NOTE_ARC].decode("utf-8")
            self.assertIn("不允许对外", note)
            self.assertIn("内部保存不受影响", note)
            self.assertIn("可交付部分不受影响", note)
            # 幂等：再投一次不再变化（重复打包不会越删越多）
            again, info2 = dp.external_delivery_projection(proj)
            self.assertEqual(again, proj)
            self.assertIsNone(info2)

    def test_without_quant_nothing_changes(self):
        import tempfile
        import delivery_pipeline as dp
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "analysis").mkdir()
            members = dp._freeze_payload("t-ext-none", ws, md_bytes=self.MD)  # noqa: SLF001
            proj, info = dp.external_delivery_projection(members)
            self.assertIsNone(info, "没有量化附件就不产对外许可结论")
            self.assertEqual(proj, members)

    def test_institutional_license_keeps_quant_in_package(self):
        import tempfile
        import delivery_pipeline as dp
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "analysis").mkdir()
            pub.store_reading(ws, "event_returns", _er(lic="机构授权", eligible=True))
            pub.store_reading(ws, "backtest", _bt(lic="机构授权", eligible=True))
            members = dp._freeze_payload("t-ext-ok", ws, md_bytes=self.MD)    # noqa: SLF001
            proj, info = dp.external_delivery_projection(members)
            self.assertTrue(info["allowed"])
            self.assertEqual(proj, members, "允许对外时包内成员一字不变")

    def test_unreadable_readings_fail_closed(self):
        """读数读不出来 ⇒ 按**不可对外**处理（不默认放行）。"""
        import delivery_pipeline as dp
        members = {"reports/report.md": self.MD, "quant/backtest.json": b"{not json"}
        proj, info = dp.external_delivery_projection(members)
        self.assertFalse(info["allowed"])
        self.assertNotIn("quant/backtest.json", proj)
        self.assertIn(dp.EXTERNAL_NOTE_ARC, proj)
        self.assertIn("reports/report.md", proj)

    def test_authorized_reading_plus_broken_member_still_fails_closed(self):
        """10-07 复核原反例：**授权 event_returns ＋ 坏 backtest.json** 曾被判 allowed=true。

        修后：坏成员一起被移出、只留限制说明，财报正文照常交付。
        """
        import delivery_pipeline as dp
        members = {
            "reports/report.md": self.MD,
            "quant/event_returns.json": json.dumps(
                _er(lic="机构授权", eligible=True)).encode("utf-8"),
            "quant/backtest.json": b"{broken",
        }
        proj, info = dp.external_delivery_projection(members)
        self.assertFalse(info["allowed"], "坏成员不得被授权读数掩盖")
        self.assertNotIn("quant/backtest.json", proj)
        self.assertNotIn("quant/event_returns.json", proj,
                         "量化外发是整体的：有成员判不出许可就不放行任何量化附件")
        self.assertEqual(proj["reports/report.md"], self.MD, "财报正文仍可交付")
        note = proj[dp.EXTERNAL_NOTE_ARC].decode("utf-8")
        self.assertIn("不允许对外", note)
        self.assertIn("内部保存不受影响", note)


if __name__ == "__main__":
    unittest.main()
