# -*- coding: utf-8 -*-
"""RUNTIME-1022 P0-B 离线桩测试：出口诊断分类（20261022）。

验证四件事（全部离线，不出网）：
1. 策略**仍然**拒绝保留地址（198.18.0.0/15、2001:2::/48、私网、环回、链路本地、
   CGNAT、元数据服务、URL 凭据）——诊断新类别不得松动 SSRF 防线；
2. 允许的公网出口仍用**已验 IP** 连接并**校验证书**（SNI=主机名、CERT_REQUIRED、
   check_hostname）；
3. 凭据通道不混用：登记服务通道（fetch_service）带鉴权头，内容派生通道
   （fetch_document）剥掉全部凭据头；
4. 分类词表与 search_diag 一致，诊断/健康探针按证据给四态，不假装健康。
"""

from __future__ import annotations

import io
import ssl
import unittest
import urllib.parse
from pathlib import Path

import net_policy
import net_diag
import search_diag

REPO = Path(__file__).resolve().parent


def _fake_read(sock, *, url="", egress="", cap=0, budget=0.0):
    return {"status": 200, "url": url, "headers": {}, "bytes": 2,
            "raw": b"{}", "egress": egress, "text": "{}"}


class _FakeSock:
    def __init__(self):
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += bytes(data)

    def close(self):
        self.closed = True


class _StubbedConnect(unittest.TestCase):
    """把"连接"钉在桩上：记录连到哪个 IP、SNI 给了谁、证书校验参数是什么。"""

    def setUp(self):
        self.sock = _FakeSock()
        self.conn_targets: list = []
        self.ctxs: list = []
        self._saved = {
            "create_connection": net_policy.socket.create_connection,
            "mkctx": ssl.create_default_context,      # 注意：net_policy.ssl 就是全局 ssl
            "read": net_policy._read_http_response,
            "resolve": net_policy._resolve_all,
        }
        net_policy.socket.create_connection = self._fake_create_connection
        ssl.create_default_context = self._fake_mkctx
        net_policy._read_http_response = _fake_read
        net_policy.reset_registry_for_test()

    def tearDown(self):
        net_policy.socket.create_connection = self._saved["create_connection"]
        ssl.create_default_context = self._saved["mkctx"]
        net_policy._read_http_response = self._saved["read"]
        net_policy._resolve_all = self._saved["resolve"]
        net_policy.reset_registry_for_test()

    def _stub_resolve(self, ips):
        """钉住解析结果（域名 → 给定 IP），避免测试真实出网。"""
        net_policy._resolve_all = lambda host, port: (list(ips), "")

    def _fake_create_connection(self, address, timeout=None, source_address=None):
        self.conn_targets.append((str(address[0]), int(address[1])))
        return self.sock

    def _fake_mkctx(self):
        real = self._saved["mkctx"]()
        rec = {"verify_mode": real.verify_mode, "check_hostname": real.check_hostname}
        self.ctxs.append(rec)
        return _RecCtx(rec)


class _RecCtx:
    def __init__(self, rec: dict):
        self._rec = rec
        self.verify_mode = rec["verify_mode"]
        self.check_hostname = rec["check_hostname"]

    def wrap_socket(self, sock, server_hostname=None):
        self._rec["sni"] = server_hostname
        return sock


class TestPolicyStillRefusesReserved(_StubbedConnect):
    """诊断新类别是"说得更清楚"，不是"放行更宽"——SSRF 防线语义不变。"""

    def test_reserved_v4_and_v6_are_refused_with_category(self):
        for ip in ("198.18.0.88", "2001:2::53"):
            host = ip if ":" not in ip else f"[{ip}]"
            self._stub_resolve([ip])
            d = net_policy.validate_public_url(f"https://{host}/x")
            self.assertFalse(d.ok)
            self.assertEqual(d.category, "reserved_address", ip)
            self.assertIn(ip, d.reason)
        # 内容派生通道同样拒绝（不是只挡字面 IP）
        self._stub_resolve(["198.18.0.88"])
        with self.assertRaises(net_policy.NetworkPolicyError) as cm:
            net_policy.fetch_document("https://www.cninfo.com.cn/new/data/szse_stock.json")
        self.assertEqual(cm.exception.category, "reserved_address")
        self.assertEqual(self.conn_targets, [], "被拒绝的请求不得建立连接")

    def test_internal_and_metadata_still_refused(self):
        cases = [
            ("https://127.0.0.1/x", "reserved_address"),
            ("https://10.1.2.3/x", "reserved_address"),
            ("https://169.254.169.254/latest/meta-data/", "reserved_address"),
            ("https://100.64.0.1/x", "reserved_address"),
            ("https://[::1]/x", "reserved_address"),
            ("https://[fe80::1]/x", "reserved_address"),
            ("https://metadata.google.internal/x", "policy_blocked"),
            ("https://user:pass@example.com/x", "policy_blocked"),
            ("ftp://example.com/x", "policy_blocked"),
        ]
        for url, want in cases:
            d = net_policy.validate_public_url(url)
            self.assertFalse(d.ok, url)
            self.assertEqual(d.category, want, url)

    def test_unregistered_endpoint_and_self_escalation_refused(self):
        d = net_policy.request_service("data_source:none")
        self.assertEqual(d.category, "policy_blocked")
        d = net_policy.request_service("data_source:none", trusted=True)
        self.assertFalse(d.ok, "调用方不得自报 trusted")


class TestPublicEgressStillValidated(_StubbedConnect):
    """放行的公网出口：已验 IP 连接 + 系统级证书校验 + SNI=主机名。"""

    def test_uses_validated_ip_and_verifies_certificate(self):
        validated = "93.184.216.34"
        self._stub_resolve([validated])
        got = net_policy.fetch_document("https://example.com/a", timeout=5)
        self.assertEqual(self.conn_targets, [(validated, 443)],
                         "必须连已验 IP，不得二次解析")
        self.assertEqual(len(self.ctxs), 1)
        self.assertEqual(self.ctxs[0]["verify_mode"], ssl.CERT_REQUIRED,
                         "证书校验不得关闭")
        self.assertTrue(self.ctxs[0]["check_hostname"])
        self.assertEqual(self.ctxs[0].get("sni"), "example.com", "SNI 必须是主机名")
        self.assertEqual(got["egress"], "direct_pinned")

    def test_registered_service_channel_uses_same_pinned_ip(self):
        from net_policy import Endpoint
        net_policy.register_endpoint(Endpoint(
            endpoint_id="data_source:svc", purpose="datasource", scheme="https",
            host="93.184.216.34", port=443, operations=("GET",)))
        net_policy.fetch_service("data_source:svc", path="/v1/q")
        self.assertEqual(self.conn_targets, [("93.184.216.34", 443)])
        self.assertEqual(self.ctxs[0]["verify_mode"], ssl.CERT_REQUIRED)


class TestCredentialChannelsDoNotMix(_StubbedConnect):
    """登记服务通道的鉴权头是它的用途；内容派生通道一个凭据头都不能带。"""

    def test_document_channel_strips_credentials(self):
        self._stub_resolve(["93.184.216.34"])
        net_policy.fetch_document(
            "https://example.com/a",
            headers={"Authorization": "Bearer secret", "Cookie": "sid=1",
                     "X-Api-Key": "k", "X-Trace": "t"})
        sent = self.sock.sent.decode("utf-8", "replace")
        for banned in ("Bearer secret", "sid=1", "X-Api-Key: k"):
            self.assertNotIn(banned, sent, f"内容通道不得携带：{banned}")
        self.assertIn("X-Trace: t", sent, "非凭据头原样透传")

    def test_service_channel_carries_its_own_credentials(self):
        from net_policy import Endpoint
        net_policy.register_endpoint(Endpoint(
            endpoint_id="data_source:svc", purpose="datasource", scheme="https",
            host="93.184.216.34", port=443, operations=("GET",)))
        net_policy.fetch_service("data_source:svc", path="/v1/q",
                                 headers={"Authorization": "Bearer svc-key"})
        sent = self.sock.sent.decode("utf-8", "replace")
        self.assertIn("Authorization: Bearer svc-key", sent,
                      "登记服务通道的鉴权头正是它的用途")
        self.assertNotIn("svc-key", str(self.sock.closed))


class TestClassification(unittest.TestCase):
    def setUp(self):
        net_policy.reset_registry_for_test()
        net_diag.reset_cache_for_test()

    def tearDown(self):
        net_diag.reset_cache_for_test()

    def test_vocabulary_is_single_source(self):
        """net_diag 词表 = search_diag.ERROR_CLASSES ∪ APP_ONLY，不出现第二套名字。"""
        extra = set(net_diag.CATEGORIES) - set(search_diag.ERROR_CLASSES)
        self.assertEqual(extra, set(net_diag.APP_ONLY))
        self.assertIn("reserved_address", search_diag.ERROR_CLASSES)
        self.assertIn("connect_error", search_diag.ERROR_CLASSES)
        self.assertIn("reserved_address", net_diag.CATEGORIES)

    def test_error_mapping_distinguishes_new_causes(self):
        self.assertEqual(search_diag.classify_error(ConnectionRefusedError("[Errno 111]")),
                         "connect_error")
        exc = net_policy.NetworkPolicyError("解析到非公网地址：198.18.0.88",
                                            category="reserved_address")
        self.assertEqual(search_diag.classify_error(exc), "reserved_address")
        self.assertEqual(net_policy.classify_network_error(exc), "reserved_address")
        import socket as _s
        self.assertEqual(search_diag.classify_error(_s.gaierror("getaddrinfo failed")),
                         "dns_error")
        f = net_policy.FetchError("读取超时", category="timeout")
        self.assertEqual(net_policy.classify_network_error(f), "timeout")

    def test_address_class_table(self):
        cases = {
            "198.18.0.88": "benchmark_198_18",
            "198.19.255.255": "benchmark_198_18",
            "198.20.0.1": "public",
            "2001:2::53": "benchmark_2001_2",
            "127.0.0.1": "loopback",
            "10.0.0.5": "private",
            "169.254.1.2": "link_local",
            "100.64.0.9": "cgnat",
            "224.0.0.1": "multicast",
            "::ffff:127.0.0.1": "ipv4_mapped:loopback",
            "93.184.216.34": "public",
            "not-an-ip": "unparsable",
        }
        for ip, want in cases.items():
            self.assertEqual(net_diag.address_class(ip), want, ip)
            self.assertTrue(net_diag.address_label(net_diag.address_class(ip)))

    def test_classify_host_with_stubbed_resolver(self):
        saved = net_diag._resolve_bounded
        try:
            net_diag._resolve_bounded = lambda h, p=0, timeout=2.0: {
                "ips": ["198.18.0.88", "2001:2::53"], "error": "", "timed_out": False}
            r = net_diag.classify_host("www.cninfo.com.cn")
            self.assertEqual(r["category"], "reserved_address")
            self.assertEqual([a["ip"] for a in r["addresses"]],
                             ["198.18.0.88", "2001:2::53"])
            self.assertTrue(r["advice"], "保留段要给出可操作建议")
            net_diag._resolve_bounded = lambda h, p=0, timeout=2.0: {
                "ips": [], "error": "gaierror: name unknown", "timed_out": False}
            r = net_diag.classify_host("no.such.host")
            self.assertEqual(r["category"], "dns_error")
            net_diag._resolve_bounded = lambda h, p=0, timeout=2.0: {
                "ips": ["93.184.216.34"], "error": "", "timed_out": False}
            self.assertEqual(net_diag.classify_host("example.com")["category"], "ok")
        finally:
            net_diag._resolve_bounded = saved
        # 字面 IP 不经 DNS
        r = net_diag.classify_host("198.18.0.88")
        self.assertEqual(r["category"], "reserved_address")
        self.assertEqual(r["detail"], "字面 IP（不经 DNS）")

    def test_egress_report_states_follow_evidence(self):
        saved_hosts, saved_cls = net_diag.configured_public_hosts, net_diag.classify_host
        net_diag.configured_public_hosts = lambda limit=4: []
        try:
            net_diag.KNOWN_CONTENT_HOSTS = ()            # 无目标 → unknown，不假装健康
            r = net_diag.egress_report(cache_ttl=0)
            self.assertEqual(r["state"], "unknown")
            self.assertEqual(r["probed"], 0)

            def fake(host, port=0, timeout=2.0):
                cat = ("reserved_address" if host.startswith("www.cninfo")
                       else "ok")
                return {"host": host, "category": cat, "addresses": [],
                        "detail": "", "advice": []}

            net_diag.KNOWN_CONTENT_HOSTS = ("www.cninfo.com.cn", "www.bing.com")
            net_diag.classify_host = fake
            r = net_diag.egress_report(cache_ttl=0)
            self.assertEqual(r["state"], "degraded", "部分目标可用不得夸大成全断")
            self.assertEqual(r["counts"], {"ok": 1, "reserved_address": 1})

            net_diag.classify_host = lambda h, port=0, timeout=2.0: {
                "host": h, "category": "reserved_address", "addresses": [],
                "detail": "", "advice": ["fake-IP 特征"]}
            r = net_diag.egress_report(cache_ttl=0)
            self.assertEqual(r["state"], "unavailable")
            self.assertIn("不是密钥或数据源问题", r["summary"])

            net_diag.classify_host = lambda h, port=0, timeout=2.0: {
                "host": h, "category": "ok", "addresses": [], "detail": "", "advice": []}
            r = net_diag.egress_report(cache_ttl=0)
            self.assertEqual(r["state"], "ok")
        finally:
            net_diag.configured_public_hosts = saved_hosts
            net_diag.classify_host = saved_cls

    def test_report_never_contains_secrets(self):
        saved = net_diag.classify_host
        net_diag.classify_host = lambda h, port=0, timeout=2.0: {
            "host": h, "category": "reserved_address", "addresses": [],
            "detail": "", "advice": []}
        try:
            blob = io.StringIO()
            import json as _json
            blob.write(_json.dumps(net_diag.egress_report(cache_ttl=0), default=str))
            text = blob.getvalue().lower()
            for marker in ("api_key", "apikey", "authorization", "bearer ", "secret"):
                self.assertNotIn(marker, text)
        finally:
            net_diag.classify_host = saved

    def test_health_probe_maps_report_to_states(self):
        import health_registry
        saved = net_diag.egress_report
        try:
            net_diag.egress_report = lambda **kw: {
                "state": "unavailable", "summary": "全部被保留段挡住", "probed": 3,
                "counts": {"reserved_address": 3}, "checked_at": 1.0, "targets": []}
            e = health_registry.probe_network_egress()
            self.assertEqual(e["state"], "unavailable")
            self.assertFalse(e["ok"])
            self.assertEqual(e["name"], "network_egress")
            net_diag.egress_report = lambda **kw: {
                "state": "unknown", "summary": "无可诊断目标", "probed": 0,
                "counts": {}, "checked_at": 1.0, "targets": []}
            e = health_registry.probe_network_egress()
            self.assertEqual(e["state"], "unknown", "没证据不显示为可用")
            net_diag.egress_report = lambda **kw: {
                "state": "degraded", "summary": "部分被挡", "probed": 3,
                "counts": {"ok": 1, "reserved_address": 2}, "checked_at": 1.0,
                "targets": []}
            e = health_registry.probe_network_egress()
            self.assertEqual(e["state"], "degraded")
            self.assertTrue(e["ok"])
        finally:
            net_diag.egress_report = saved

    def test_known_hosts_are_the_adapters_hosts(self):
        """诊断清单不许漂移成第二套事实：每个主机必须出现在 adapters 源码里。"""
        sources = ""
        for name in ("cninfo.py", "eastmoney.py", "text_search.py",
                     "ashare_ranking.py", "news.py"):
            sources += (REPO / "adapters" / name).read_text(encoding="utf-8")
        for host in net_diag.KNOWN_CONTENT_HOSTS:
            self.assertIn(host, sources, f"{host} 不在任何适配器里——诊断清单漂移了")


if __name__ == "__main__":
    unittest.main()
