# -*- coding: utf-8 -*-
"""事件证据底座：测试实际金融语义与可得时间，不调用模型或联网。"""
from __future__ import annotations

import copy
import hashlib
import unittest

from financial_analysis import events


def record(text, **changes):
    out = {"code": "600031.SH", "company": "测试机械公司",
           "title": "研究材料", "text": text, "available_at": "2025-04-18",
           "ingested_at": "2026-10-08T10:00:00+08:00", "source": "合成测试公告",
           "source_kind": "official_disclosure", "material_id": "synthetic-1"}
    out.update(changes)
    return out


class FinancialCandidateTest(unittest.TestCase):
    def test_three_financial_classes_have_original_spans_and_caveats(self):
        text = "公司预计全年净利润同比增长20%。公司已签订设备订单。公司完成兑付到期债券。"
        result = events.extract([record(text)])
        self.assertEqual(result["status"], "ok")
        self.assertEqual({e["type"] for e in result["events"]}, set(events.EVENT_TYPES))
        for e in result["events"]:
            p = e["evidence"]
            self.assertEqual(text[p["start"]:p["end"]], p["quote"])
            self.assertEqual(e["sentiment"]["method"], "rule_candidate")
            self.assertIsNone(e["sentiment"]["confidence"])
            self.assertEqual(e["status"], "candidate")
        guidance = next(e for e in result["events"] if e["type"] == "earnings_guidance")
        self.assertEqual(guidance["modality"], "planned")
        order = next(e for e in result["events"] if e["type"] == "orders_demand")
        self.assertEqual(order["evidence"]["role"], "company_statement")
        self.assertTrue(any("不等于确认收入" in x for x in result["limits"]))

    def test_unexecuted_contract_not_presented_as_completed_order(self):
        unexecuted = events.extract([record("公司尚未签订该订单。")])["events"][0]
        self.assertEqual(unexecuted["modality"], "negated")
        self.assertEqual(unexecuted["sentiment"]["label"], "unknown")
        proposed = events.extract([record("公司拟签订设备采购合同。")])["events"][0]
        self.assertEqual(proposed["modality"], "planned")
        self.assertEqual(proposed["evidence"]["role"], "plan")

    def test_allegation_and_denied_default_are_not_certified(self):
        e = events.extract([record("报道称公司被指贷款违约。")])["events"][0]
        self.assertEqual(e["evidence"]["role"], "allegation")
        self.assertEqual(e["sentiment"]["label"], "unknown")
        e = events.extract([record("公司否认发生贷款违约。")])["events"][0]
        self.assertEqual(e["modality"], "negated")
        self.assertEqual(e["sentiment"]["label"], "unknown")

    def test_title_only_needs_original(self):
        r = events.extract([record("", title="重大订单中标公告")])
        self.assertEqual(r["status"], "needs_original")
        self.assertEqual(r["events"], [])

    def test_unknown_topic_not_filled_as_neutral_event(self):
        r = events.extract([record("公司举办员工培训活动。")])
        self.assertEqual(r["events"], [])
        self.assertEqual(r["excluded"][0]["reason"], "no_supported_event")


class EvidenceIdentityTest(unittest.TestCase):
    def test_explicit_candidate_requires_exact_original_quote(self):
        text = "  公司已签订设备订单。\n"
        candidate = {"type": "orders_demand", "quote": "公司已签订设备订单。",
                     "start": 2, "end": len(text) - 1}
        good = events.extract([record(text, candidates=[candidate])])
        self.assertEqual(len(good["events"]), 1)
        wrong = dict(candidate, end=candidate["end"] - 1)
        bad = events.extract([record(text, candidates=[wrong])])
        self.assertEqual(bad["excluded"][0]["reason"], "quote_span_mismatch")

    def test_changed_text_breaks_old_hash_and_run_identity(self):
        text = "公司已签订设备订单。"
        r = record(text, source_hash=hashlib.sha256(text.encode("utf-8")).hexdigest())
        first = events.extract([r])
        changed = dict(r, text="公司已取消设备订单。")
        rejected = events.extract([changed])
        self.assertEqual(rejected["excluded"][0]["reason"], "source_hash_mismatch")
        changed.pop("source_hash")
        second = events.extract([changed])
        self.assertNotEqual(first["input_hash"], second["input_hash"])
        self.assertNotEqual(first["events"][0]["version_id"], second["events"][0]["version_id"])

    def test_missing_or_changed_source_subject_is_not_task_relabelled(self):
        missing = events.extract([record("公司已签订订单。", code="")])
        self.assertEqual(missing["excluded"][0]["reason"], "missing_source_subject")
        c = {"type": "orders_demand", "quote": "公司已签订订单。", "code": "002304.SZ"}
        bad = events.extract([record(c["quote"], candidates=[c])])
        self.assertEqual(bad["excluded"][0]["reason"], "candidate_subject_mismatch")

    def test_explicit_reference_must_bind_same_body_and_material(self):
        c = {"type": "orders_demand", "quote": "公司订单增加。", "source_hash": "0" * 64}
        bad = events.extract([record(c["quote"], candidates=[c])])
        self.assertEqual(bad["excluded"][0]["reason"], "candidate_source_hash_mismatch")
        c.pop("source_hash")
        c["material_id"] = "some-other-notice"
        bad = events.extract([record(c["quote"], candidates=[c])])
        self.assertEqual(bad["excluded"][0]["reason"], "candidate_material_mismatch")

    def test_reprints_dedup_but_company_and_corrections_keep_identity(self):
        original = record("公司已签订设备订单。", origin_id="notice-1", version_id="v1")
        reprint = dict(original, source="转载媒体", material_id="copy-1")
        correction = dict(original, version_id="v2", correction_of="v1",
                          available_at="2025-04-19")
        next_correction = dict(correction, version_id="v3", available_at="2025-04-20")
        other = dict(original, code="002304.SZ", company="测试酒业公司")
        r = events.extract([original, reprint, correction, next_correction, other])
        self.assertEqual(len(r["events"]), 4)
        self.assertEqual([e["reason"] for e in r["excluded"]], ["duplicate_reprint"])
        self.assertEqual({e["version_id"] for e in r["events"]}, {"v1", "v2", "v3"})
        self.assertNotEqual(r["events"][0]["event_id"], r["events"][-1]["event_id"])

    def test_ambiguous_quote_without_span_is_rejected_and_input_untouched(self):
        text = "公司订单增加。公司订单增加。"
        r = record(text, candidates=[{"type": "orders_demand", "quote": "公司订单增加。"}])
        before = copy.deepcopy(r)
        batch = events.extract([r])
        self.assertEqual(batch["excluded"][0]["reason"], "ambiguous_quote_span")
        self.assertEqual(r, before)


class PublicTimeTest(unittest.TestCase):
    def test_day_precision_material_unavailable_before_that_day_ends(self):
        r = record("公司订单增加。")
        before = events.extract([r], as_of="2025-04-18T10:00:00+08:00")
        self.assertEqual(before["events"], [])
        self.assertEqual(before["excluded"][0]["reason"], "not_yet_public")
        after = events.extract([r], as_of="2025-04-18")
        self.assertEqual(len(after["events"]), 1)
        self.assertEqual(after["events"][0]["times"]["public"]["precision"], "day")
        self.assertTrue(after["events"][0]["historical_reconstruction"])

    def test_correction_public_after_cutoff_is_not_backfilled(self):
        old = record("公司订单增加。", version_id="old")
        revised = dict(old, text="公司订单减少。", available_at="2025-04-20",
                       version_id="revised", correction_of="old")
        r = events.extract([old, revised], as_of="2025-04-18")
        self.assertEqual([e["version_id"] for e in r["events"]], ["old"])
        self.assertEqual(r["excluded"][0]["reason"], "not_yet_public")

    def test_missing_timezone_timestamp_is_not_assumed_as_public_time(self):
        r = events.extract([record("公司订单增加。", available_at="2025-04-18T10:00:00")])
        self.assertEqual(r["excluded"][0]["reason"], "unverifiable_public_time")
        invalid = events.extract([record("公司订单增加。")], as_of="2025")
        self.assertEqual(invalid["excluded"][0]["reason"], "invalid_as_of")

    def test_bridge_uses_disclosure_day_not_occurrence_or_ingestion(self):
        r = events.extract([record("公司订单增加。", event_at="2025-03-01")])
        row = events.to_event_list(r)[0]
        self.assertEqual(row["code"], "600031.SH")
        self.assertEqual(row["date"], "2025-04-18")
        self.assertEqual(row["timing_rule"], "next_trading_day_after_public_date")
        self.assertTrue(row["candidate"])
        self.assertEqual(row["kind"], "event_reaction_observation")

    def test_one_announcement_yields_one_price_observation_not_three_samples(self):
        r = events.extract([record("公司预计净利润增长。公司订单增加。公司完成兑付债券。")])
        self.assertEqual(len(r["events"]), 3)
        rows = events.to_event_list(r)
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]["types"]), set(events.EVENT_TYPES))
        self.assertEqual(len(rows[0]["event_ids"]), 3)

    def test_cross_timezone_timestamp_uses_chinese_market_public_day(self):
        r = events.extract([record("公司订单增加。", available_at="2025-04-18T20:30:00-04:00")])
        self.assertEqual(events.to_event_list(r)[0]["date"], "2025-04-19")


if __name__ == "__main__":
    unittest.main()
