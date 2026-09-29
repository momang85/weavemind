# -*- coding: utf-8 -*-
"""官方披露发现（巨潮公告查询）离线验收：参数契约、空结果自证、失败分类、版本并存。

为什么必须有这一组：2026-09-07 的探针把"参数缺 orgId → 静默空"误读成"接口未连通/被挡"，
于是发现链整条被判死了一周多。这类错误**离线可防**——只要把参数契约、空结果自证和
失败分类各自钉住，就不会再出现"分不清是没数据还是我们问错了"。

全部用注入的取件函数（`fetch=`）驱动，**不发真实请求**；真实链的读数在
`docs/evidence/a2_official_discovery_20260929.md` 里另记。
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import adapters.cninfo as cninfo                                       # noqa: E402
from adapters import disclosure_ingest as di                           # noqa: E402
from adapters import source_registry as registry                       # noqa: E402

ORG_MAP = {
    "stockList": [
        {"code": "600031", "pinyin": "syzg", "category": "A股",
         "orgId": "gssh0600031", "zwjc": "三一重工"},
        {"code": "002304", "pinyin": "yhg", "category": "A股",
         "orgId": "gssz0002304", "zwjc": "洋河股份"},
        {"code": "000711", "pinyin": "jlkj", "category": "A股",
         "orgId": "gssz0000711", "zwjc": "*ST京蓝"},
    ]
}

# 一条公告行（结构照抄实机响应；时间戳 = 2025-04-18）
ANNOUNCEMENT = {
    "secCode": "600031", "secName": "三一重工", "orgId": "gssh0600031",
    "announcementId": "1223129214",
    "announcementTitle": "三一重工股份有限公司2024年年度报告",
    "announcementTime": 1744905600000,
    "adjunctUrl": "finalpage/2025-04-18/1223129214.PDF", "adjunctSize": 5253,
}


class FakeNet:
    """注入式取件替身：分别应答 orgId 映射与公告查询，并记下每一次调用。"""

    def __init__(self, *, org_map=None, org_status=200, org_raw=None,
                 query=None, query_status=200, query_raw=None):
        self.calls: list[dict] = []
        self._org = ORG_MAP if org_map is None else org_map
        self._org_status = org_status
        self._org_raw = org_raw
        self._query = {"announcements": [ANNOUNCEMENT], "totalAnnouncement": 1} \
            if query is None else query
        self._query_status = query_status
        self._query_raw = query_raw

    def __call__(self, url, *, method="GET", body="", timeout=25, max_bytes=0, headers=None):
        self.calls.append({"url": url, "method": method, "body": body, "headers": headers})
        if "szse_stock.json" in url:
            raw = self._org_raw if self._org_raw is not None else \
                json.dumps(self._org, ensure_ascii=False).encode("utf-8")
            return {"status": self._org_status, "raw": raw, "headers": {}}
        raw = self._query_raw if self._query_raw is not None else \
            json.dumps(self._query, ensure_ascii=False).encode("utf-8")
        return {"status": self._query_status, "raw": raw, "headers": {}}

    def query_calls(self) -> list[dict]:
        return [c for c in self.calls if "hisAnnouncement" in c["url"]]


class CninfoDiscoveryTestCase(unittest.TestCase):
    def setUp(self):
        cninfo.reset_cache()
        self.addCleanup(cninfo.reset_cache)


class TestParamContract(CninfoDiscoveryTestCase):
    """参数契约：`stock` 必须带 orgId；`seDate` 过滤的是**披露日**。"""

    def test_query_requires_code_and_org_id(self):
        params = cninfo.build_query("600031", "gssh0600031", years=(2024,))
        self.assertEqual(params["stock"], "600031,gssh0600031")
        self.assertEqual(params["category"], cninfo.CATEGORY_ANNUAL)
        self.assertEqual(params["isHLtitle"], "true")

    def test_missing_org_id_stops_before_the_request(self):
        """映射里没有这个代码 → 明确拒绝，**不发**那次"必然静默空"的查询。"""
        net = FakeNet(org_map={"stockList": []})
        with self.assertRaises(cninfo.DiscoveryUnavailable) as ctx:
            cninfo.query_announcements("600031.SH", years=(2024,), fetch=net)
        self.assertEqual(ctx.exception.reason_code, registry.EMPTY_RESULT)
        self.assertEqual(net.query_calls(), [], "不得发出没有 orgId 的查询")

    def test_bare_code_normalisation(self):
        self.assertEqual(cninfo._bare_code("002304.SZ"), "002304")
        self.assertEqual(cninfo._bare_code("sz000001"), "000001")
        self.assertEqual(cninfo._bare_code("600031"), "600031")
        self.assertEqual(cninfo._bare_code("00700.HK"), "")
        self.assertEqual(cninfo._bare_code(""), "")

    def test_span_keeps_the_right_end_open_for_restatements(self):
        span = cninfo.build_query("000711", "gssz0000711", years=(2020,))["seDate"]
        self.assertTrue(span.startswith("2019-01-01~"), span)
        right = int(span.split("~")[1][:4])
        self.assertGreaterEqual(right, 2025,
                                "右端必须开到更正稿发布之后，否则看不到更正版")

    def test_until_pins_the_as_of_window(self):
        span = cninfo.build_query("000711", "gssz0000711", years=(2020,),
                                  until="2021-12-31")["seDate"]
        self.assertEqual(span, "2019-01-01~2021-12-31")

    def test_org_map_is_cached_between_calls(self):
        net = FakeNet()
        first = cninfo.org_map(fetch=net)
        second = cninfo.org_map(fetch=net)
        self.assertEqual(first["600031"]["orgId"], "gssh0600031")
        self.assertEqual(second["600031"]["name"], "三一重工")
        self.assertEqual(len([c for c in net.calls if "szse_stock.json" in c["url"]]), 1,
                         "同一进程内映射只下一次")

    def test_org_map_schema_change_is_reported(self):
        net = FakeNet(org_raw=b'{"unexpected": true}')
        with self.assertRaises(cninfo.DiscoveryUnavailable) as ctx:
            cninfo.org_map(fetch=net, refresh=True)
        self.assertEqual(ctx.exception.reason_code, registry.SCHEMA_CHANGED)


class TestEmptyAndIrrelevant(CninfoDiscoveryTestCase):
    """空结果不是成功，也不必然是故障：要有前提、能自证。"""

    def _empty_net(self):
        return FakeNet(query={"announcements": None, "totalAnnouncement": 0,
                              "categoryList": None, "hasMore": False})

    def test_empty_result_carries_its_own_premise(self):
        got = cninfo.query_announcements("600031.SH", years=(2024,),
                                         fetch=self._empty_net())
        self.assertEqual(got["items"], [])
        self.assertEqual(got["reason_code"], registry.EMPTY_RESULT)
        self.assertEqual(got["params"]["stock"], "600031,gssh0600031")
        self.assertIn("seDate", got["params"])
        self.assertIn("total=0", got["reason"])
        self.assertEqual(got["contract"], "cninfo-hisannouncement-v1")

    def test_announcements_key_missing_is_schema_change_not_empty(self):
        net = FakeNet(query={"totalAnnouncement": 3})
        with self.assertRaises(cninfo.DiscoveryUnavailable) as ctx:
            cninfo.query_announcements("600031.SH", years=(2024,), fetch=net)
        self.assertEqual(ctx.exception.reason_code, registry.SCHEMA_CHANGED)

    def test_returned_but_irrelevant_is_not_empty(self):
        """有返回但没有一条是目标期间的年报 → `irrelevant_result`（不是"没有公告"）。"""
        rows = [dict(ANNOUNCEMENT, announcementTitle="三一重工股份有限公司2023年年度报告"),
                dict(ANNOUNCEMENT, announcementTitle="三一重工关于回购股份的公告")]
        net = FakeNet(query={"announcements": rows, "totalAnnouncement": 2})
        got = cninfo.discover_annual_reports("600031.SH", years=(2024,), fetch=net)
        self.assertEqual(got["items"], [])
        self.assertEqual(len(got["unmatched"]), 2)
        self.assertEqual(got["reason_code"], registry.IRRELEVANT_RESULT)
        self.assertIn("没有一条", got["reason"])


class TestFailureClassification(CninfoDiscoveryTestCase):
    """失败分类：同类错误不得跨入口误报（专项 §3）。"""

    def test_plain_text_response_is_protocol_error(self):
        net = FakeNet(query_raw=b"<html>503</html>")
        with self.assertRaises(cninfo.DiscoveryUnavailable) as ctx:
            cninfo.query_announcements("600031.SH", years=(2024,), fetch=net)
        self.assertEqual(ctx.exception.reason_code, registry.PROTOCOL_ERROR)

    def test_status_codes_map_to_distinct_reasons(self):
        for status, want in ((401, registry.AUTH_REQUIRED), (403, registry.AUTH_REQUIRED),
                             (429, registry.RATE_LIMITED), (456, registry.RATE_LIMITED),
                             (500, registry.PROTOCOL_ERROR),
                             (302, registry.PROTOCOL_ERROR)):
            net = FakeNet(query_status=status)
            with self.assertRaises(cninfo.DiscoveryUnavailable) as ctx:
                cninfo.query_announcements("600031.SH", years=(2024,), fetch=net)
            self.assertEqual(ctx.exception.reason_code, want, f"HTTP {status}")

    def test_our_own_policy_refusal_is_not_a_site_failure(self):
        """被**本项目**出域策略拒绝，不能记成"站点网络错误"。"""
        import net_policy
        exc = net_policy.NetworkPolicyError("目标不可达（策略）")

        def _boom(*a, **k):
            raise exc

        with self.assertRaises(cninfo.DiscoveryUnavailable) as ctx:
            cninfo.query_announcements("600031.SH", years=(2024,), fetch=_boom)
        self.assertEqual(ctx.exception.reason_code, registry.POLICY_BLOCKED)
        self.assertEqual(cninfo._classify(exc), registry.POLICY_BLOCKED)

    def test_transport_failure_is_network_error(self):
        import net_policy

        def _boom(*a, **k):
            raise net_policy.FetchError("读取超时（总截止 25s）")

        with self.assertRaises(cninfo.DiscoveryUnavailable) as ctx:
            cninfo.query_announcements("600031.SH", years=(2024,), fetch=_boom)
        self.assertEqual(ctx.exception.reason_code, registry.NETWORK_ERROR)
        self.assertEqual(cninfo._classify(net_policy.FetchError("读取超时")),
                         registry.NETWORK_ERROR)

    def test_every_classified_reason_is_in_the_vocabulary(self):
        import net_policy
        samples = [net_policy.NetworkPolicyError("x"), net_policy.FetchError("读取超时"),
                   net_policy.FetchError("目标返回重定向（302），按策略不自动跟随"),
                   cninfo.DiscoveryUnavailable("q", "x", reason_code=registry.RATE_LIMITED),
                   ValueError("whatever")]
        for exc in samples:
            self.assertIn(cninfo._classify(exc), registry.REASON_CODES, repr(exc))


class TestVersionsAndMaterialKind(CninfoDiscoveryTestCase):
    """原版与更正版并存；摘要与正文分开——都不许"取最新覆盖"。"""

    def _rows(self):
        return [
            dict(ANNOUNCEMENT, announcementTitle="京蓝科技股份有限公司2020年年度报告",
                 adjunctUrl="finalpage/2021-04-27/1209816825.PDF",
                 announcementTime=1619452800000, secCode="000711", secName="京蓝科技"),
            dict(ANNOUNCEMENT, announcementTitle="京蓝科技股份有限公司2020年年度报告（更正后）",
                 adjunctUrl="finalpage/2025-09-05/1224639904.PDF",
                 announcementTime=1757001600000, secCode="000711", secName="*ST京蓝"),
            dict(ANNOUNCEMENT, announcementTitle="京蓝科技股份有限公司2020年年度报告摘要",
                 adjunctUrl="finalpage/2021-04-27/1209816824.PDF",
                 announcementTime=1619452800000, secCode="000711", secName="京蓝科技"),
        ]

    def test_original_and_corrected_coexist(self):
        net = FakeNet(query={"announcements": self._rows(), "totalAnnouncement": 3})
        got = cninfo.discover_annual_reports("000711.SZ", years=(2020,), fetch=net)
        urls = {it["url"] for it in got["items"]}
        self.assertIn("http://static.cninfo.com.cn/finalpage/2021-04-27/1209816825.PDF", urls)
        self.assertIn("http://static.cninfo.com.cn/finalpage/2025-09-05/1224639904.PDF", urls)
        by_version = {it["version"]: it for it in got["items"]}
        self.assertEqual(by_version["corrected"]["disclosed_at"], "2025-09-05")
        self.assertEqual(by_version["original"]["disclosed_at"], "2021-04-27")
        self.assertEqual(by_version["corrected"]["disclosed_at_basis"],
                         "source_field:announcementTime")

    def test_summary_is_flagged_not_dropped(self):
        net = FakeNet(query={"announcements": self._rows(), "totalAnnouncement": 3})
        got = cninfo.discover_annual_reports("000711.SZ", years=(2020,), fetch=net)
        summaries = [it for it in got["items"] if it["is_summary"]]
        self.assertEqual(len(summaries), 1)
        only_body = cninfo.discover_annual_reports("000711.SZ", years=(2020,),
                                                   include_summary=False, fetch=net)
        self.assertTrue(all(not it["is_summary"] for it in only_body["items"]))

    def test_bad_timestamp_is_unknown_not_guessed(self):
        self.assertEqual(cninfo._date_from_ms(None), ("", ""))
        self.assertEqual(cninfo._date_from_ms("abc"), ("", ""))
        self.assertEqual(cninfo._date_from_ms(0), ("", ""))


class TestDiscoverContract(CninfoDiscoveryTestCase):
    """`disclosure_ingest.discover()`：候选 → 准入，且恢复入口是**可行动的**。"""

    def test_found_returns_actionable_candidates(self):
        net = FakeNet()
        out = di.discover("三一重工", "600031.SH", periods=(2024,), fetch=net)
        self.assertEqual(out["status"], "found")
        self.assertEqual(out["contract"], "cninfo-hisannouncement-v1")
        self.assertEqual(out["params"]["stock"], "600031,gssh0600031")
        cand = out["candidates"][0]
        self.assertIn("官方披露域", cand["why"])
        self.assertEqual(cand["authority"], "official_disclosure")
        self.assertEqual(cand["disclosed_at"], "2025-04-18")
        self.assertEqual(cand["subject_state"], "ok")

    def test_only_annual_reports_are_wired(self):
        out = di.discover("三一重工", "600031.SH", periods=(2024,), doc_type="季度报告",
                          fetch=FakeNet())
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(out["reason_code"], registry.NOT_IMPLEMENTED)
        self.assertTrue(out["next_steps"], "不可用时必须给恢复入口")

    def test_disabled_switch_is_not_implemented(self):
        with mock.patch.dict(os.environ, {"WEAVEMIND_CNINFO_DISCOVERY": "0"}):
            out = di.discover("三一重工", "600031.SH", periods=(2024,), fetch=FakeNet())
        self.assertEqual(out["reason_code"], registry.NOT_IMPLEMENTED)
        self.assertEqual(out["candidates"], [])

    def test_other_company_candidate_is_dropped(self):
        rows = [dict(ANNOUNCEMENT, announcementTitle="洋河股份2024年年度报告",
                     secCode="002304", secName="洋河股份")]
        out = di.discover("三一重工", "600031.SH", periods=(2024,),
                          fetch=FakeNet(query={"announcements": rows, "totalAnnouncement": 1}))
        self.assertEqual(out["status"], "no_candidates")
        self.assertEqual(out["candidates"], [])

    def test_unavailable_carries_reason_code_and_recovery(self):
        def _boom(*a, **k):
            raise cninfo.DiscoveryUnavailable("公告查询", "HTTP 503",
                                              reason_code=registry.PROTOCOL_ERROR)

        out = di.discover("三一重工", "600031.SH", periods=(2024,), fetch=_boom)
        self.assertEqual(out["status"], "unavailable")
        self.assertEqual(out["reason_code"], registry.PROTOCOL_ERROR)
        self.assertTrue(out["next_steps"])
        self.assertIn("参数摘要", " ".join(out["next_steps"]))

    def test_discovery_provenance_is_its_own_kind(self):
        """官方发现**不是**人工直链、也不是全文搜索，标签必须自己一类。"""
        self.assertIn(di.PROVENANCE_DISCOVERY, di.PROVENANCE_LABEL)
        self.assertNotEqual(di.PROVENANCE_DISCOVERY, di.PROVENANCE_MANUAL_URL)
        self.assertNotEqual(di.PROVENANCE_DISCOVERY, di.PROVENANCE_AUTO)
        self.assertIn("官方", di.PROVENANCE_LABEL[di.PROVENANCE_DISCOVERY])

    def test_request_carries_no_credentials(self):
        banned = ("authorization", "cookie", "x-api-key", "token")
        for key in cninfo._BROWSER_HEADERS:
            self.assertNotIn(str(key).lower(), banned, f"公告发现不得带凭据头：{key}")


class TestSourceRegistry(unittest.TestCase):
    """来源注册：端点粒度、字段齐全、授权范围如实写。"""

    REQUIRED = ("upstream_family", "access_method", "authority", "supported_materials",
                "license_scope", "capability", "budget", "health_source")

    def test_every_source_declares_the_required_fields(self):
        for sid, meta in registry.SOURCES.items():
            for key in self.REQUIRED:
                self.assertIn(key, meta, f"{sid} 缺 {key}")
            self.assertIn("verified", meta, f"{sid} 必须说明验过没有")

    def test_endpoints_of_one_family_are_accounted_separately(self):
        endpoints = registry.by_family("cninfo")
        self.assertGreaterEqual(len(endpoints), 3,
                                "巨潮至少三条端点：公告发现/orgId 映射/财务行")
        health = {registry.health_source(sid) for sid in endpoints}
        self.assertEqual(len(health), len(endpoints), "每条端点要有自己的健康键")

    def test_capability_and_health_are_separate_concepts(self):
        self.assertEqual(registry.capability("cninfo_disclosure_query"), "available")
        self.assertEqual(registry.capability("cninfo_annual_rows"), "not_implemented")
        for sid in registry.SOURCES:
            self.assertIn(registry.capability(sid),
                          ("available", "not_implemented", "unverified", "disabled"))

    def test_unknown_source_is_empty_not_invented(self):
        self.assertEqual(registry.describe("nope"), {})
        self.assertEqual(registry.capability("nope"), "unknown")
        self.assertEqual(registry.reason_text("nope"), registry.REASON_LABEL[registry.UNKNOWN_CAUSE])

    def test_eastmoney_endpoint_carries_the_parameter_contract_lesson(self):
        meta = registry.describe("eastmoney_ashare_annual")
        self.assertIn("裸代码", meta["params_contract"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
