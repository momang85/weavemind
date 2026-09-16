# -*- coding: utf-8 -*-
"""网络访问策略验收（依《架构决策_身份与网络边界_20260915》第三节的最小验收集合）。

控制流用替身（伪造解析器/连接），**不做真实外网访问**；真实出口隔离另记待验证。
覆盖：登记本地模型允许；同一地址经内容抓取拒绝；未登记私网拒绝；公网允许；
DNS 失败/公网+私网混合/地址映射/重定向进内网一律拒绝；跨源不携带凭据；
重绑定不能改变最终连接目标；调用方不得自报 trusted/allow_private。
"""

from __future__ import annotations

import ipaddress
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import net_policy  # noqa: E402


def _addrinfo(*ips):
    out = []
    for ip in ips:
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        out.append((fam, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0)))
    return out


class TestPublicUrlValidation(unittest.TestCase):
    def test_public_host_allowed(self):
        with mock.patch("net_policy._resolve_all", return_value=(["93.184.216.34"], "")):
            d = net_policy.validate_public_url("https://example.com/a?b=1")
        self.assertTrue(d.ok, d.reason)
        self.assertEqual(d.resolved, ("93.184.216.34",))

    def test_dns_failure_is_rejected(self):
        """旧实现"解析失败也放行"——必须改为拒绝。"""
        with mock.patch("net_policy._resolve_all", return_value=([], "DNS 解析失败：塌了")):
            d = net_policy.validate_public_url("https://nonexistent.invalid/x")
        self.assertFalse(d.ok)
        self.assertIn("DNS", d.reason)

    def test_mixed_public_and_private_resolution_rejected(self):
        with mock.patch("net_policy._resolve_all", return_value=(["93.184.216.34", "10.0.0.5"], "")):
            d = net_policy.validate_public_url("https://mixed.example/x")
        self.assertFalse(d.ok)
        self.assertIn("10.0.0.5", d.reason)

    def test_internal_and_reserved_addresses_rejected(self):
        for bad in ("127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254",
                    "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fc00::1", "fe80::1",
                    "::ffff:127.0.0.1", "::ffff:10.0.0.1"):
            d = net_policy.validate_public_url(f"http://[{bad}]/x" if ":" in bad else f"http://{bad}/x")
            self.assertFalse(d.ok, f"{bad} 应被拒绝")

    def test_localhost_variants_and_metadata_rejected(self):
        for host in ("localhost", "api.localhost", "metadata.google.internal"):
            self.assertFalse(net_policy.validate_public_url(f"http://{host}/x").ok, host)

    def test_userinfo_and_scheme_rejected(self):
        self.assertFalse(net_policy.validate_public_url("http://user:pw@example.com/").ok)
        for scheme in ("file", "ftp", "gopher", "data"):
            self.assertFalse(net_policy.validate_public_url(f"{scheme}://example.com/x").ok)

    def test_redacted_target_has_no_query_or_credentials(self):
        d = net_policy.validate_public_url("https://example.com/a/b?token=secret#frag")
        self.assertNotIn("secret", d.redacted)
        self.assertNotIn("?", d.redacted)
        self.assertEqual(d.redacted, "https://example.com/a/b")


class TestRegisteredEndpoints(unittest.TestCase):
    """可信端点：私网例外只来自登记项，调用方不能自报。"""

    def setUp(self):
        net_policy.reset_registry_for_test()
        net_policy.register_endpoint(net_policy.Endpoint(
            endpoint_id="local-ollama", purpose="model", scheme="http",
            host="127.0.0.1", port=11434, loopback_ok=True))
        net_policy.register_endpoint(net_policy.Endpoint(
            endpoint_id="cloud-llm", purpose="model", scheme="https",
            host="api.example.com", port=443, operations=("chat",)))

    def tearDown(self):
        net_policy.reset_registry_for_test()

    def test_registered_local_model_allowed(self):
        d = net_policy.request_service("local-ollama", "chat")
        self.assertTrue(d.ok, d.reason)
        self.assertEqual(d.url, "http://127.0.0.1:11434")
        self.assertEqual(d.kind, "registered")

    def test_same_address_via_content_fetch_is_rejected(self):
        """同一个环回地址：登记端点允许，内容抓取通道拒绝。"""
        self.assertTrue(net_policy.request_service("local-ollama").ok)
        self.assertFalse(net_policy.validate_public_url("http://127.0.0.1:11434/v1").ok)

    def test_unregistered_private_service_rejected(self):
        d = net_policy.request_service("not-registered")
        self.assertFalse(d.ok)
        self.assertIn("未登记", d.reason)
        intern = net_policy.Endpoint(endpoint_id="intern", purpose="mcp", scheme="http",
                                     host="10.0.0.9", port=8080, loopback_ok=False)
        net_policy.register_endpoint(intern)
        d2 = net_policy.request_service("intern")
        self.assertFalse(d2.ok, "指向内网但未标记为本地服务的端点必须拒绝")
        self.assertIn("内部地址", d2.reason)

    def test_caller_cannot_self_assert_trust(self):
        for kw in ("trusted", "allow_private", "allow_internal"):
            d = net_policy.request_service("cloud-llm", **{kw: True})
            self.assertFalse(d.ok, f"{kw} 不应被接受")
            self.assertIn("不得自报", d.reason)

    def test_operation_whitelist_enforced(self):
        with mock.patch("net_policy._resolve_all", return_value=(["93.184.216.34"], "")):
            self.assertTrue(net_policy.request_service("cloud-llm", "chat").ok)
            self.assertFalse(net_policy.request_service("cloud-llm", "delete").ok)

    def test_public_registered_endpoint_resolves(self):
        with mock.patch("net_policy._resolve_all", return_value=(["93.184.216.34"], "")):
            d = net_policy.request_service("cloud-llm", "chat")
        self.assertTrue(d.ok, d.reason)
        self.assertEqual(d.resolved, ("93.184.216.34",))

    def test_registry_reads_config_network_endpoints(self):
        import json
        import tempfile
        cfg = Path(tempfile.mkdtemp(prefix="wm_net_")) / "config.json"
        cfg.write_text(json.dumps({"network": {"endpoints": [
            {"id": "ep-1", "purpose": "webhook", "url": "https://hooks.example.com/x", "operations": ["post"]},
        ]}}), encoding="utf-8")
        net_policy.reset_registry_for_test()
        with mock.patch.object(net_policy, "_CONFIG_PATH", str(cfg)):
            reg = net_policy.load_registry(force=True)
        self.assertIn("ep-1", reg)
        self.assertEqual(reg["ep-1"].purpose, "webhook")


class TestFetchDocumentTransport(unittest.TestCase):
    """抓取通道：用已验 IP 连接、不跟随重定向、不带凭据、超限即停。"""

    def _run_fetch(self, url, connect_result, *, payload=b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nhi",
                   max_bytes=None):
        calls = {"connect": []}

        class _Sock:
            def __init__(self, data):
                self._data = data
                self.sent = b""

            def sendall(self, data):
                self.sent += data

            def recv(self, n):
                chunk, self._data = self._data[:n], self._data[n:]
                return chunk

            def close(self):
                pass

        def _fake_connect(ip, port, host, scheme, timeout):
            calls["connect"].append((ip, port, host, scheme))
            if isinstance(connect_result, Exception):
                raise connect_result
            return _Sock(payload)

        with mock.patch("net_policy._resolve_all", return_value=(["93.184.216.34"], "")), \
                mock.patch("net_policy._connect_pinned", side_effect=_fake_connect):
            out = net_policy.fetch_document(url, max_bytes=max_bytes)
        return out, calls

    def test_fetch_connects_to_validated_ip_with_host_header(self):
        out, calls = self._run_fetch("https://example.com/a", None)
        self.assertEqual(out["status"], 200)
        self.assertEqual(calls["connect"][0][0], "93.184.216.34", "必须连到已验 IP（防重绑定）")
        self.assertEqual(calls["connect"][0][2], "example.com", "TLS/SNI 与 Host 仍是原主机")

    def test_redirect_is_not_followed(self):
        with self.assertRaises(net_policy.FetchError) as ctx:
            self._run_fetch("https://example.com/a", None,
                            payload=b"HTTP/1.1 302 Found\r\nLocation: http://10.0.0.1/\r\n\r\n")
        self.assertIn("重定向", str(ctx.exception))

    def test_request_carries_no_credentials(self):
        calls = {"connect": []}

        class _Sock:
            def __init__(self):
                self.sent = b""

            def sendall(self, data):
                self.sent += data

            def recv(self, n):
                return b"HTTP/1.1 200 OK\r\n\r\nok" if not self.sent else b""

            def close(self):
                pass

        captured = {}

        def _fake_connect(ip, port, host, scheme, timeout):
            sock = _Sock()
            captured["sock"] = sock
            return sock

        with mock.patch("net_policy._resolve_all", return_value=(["93.184.216.34"], "")), \
                mock.patch("net_policy._connect_pinned", side_effect=_fake_connect):
            net_policy.fetch_document("https://example.com/a",
                                      headers={"Authorization": "Bearer sekret", "X-Trace": "1"})
        sent = captured["sock"].sent.decode("latin-1")
        self.assertNotIn("sekret", sent, "内容抓取不得携带调用方凭据")
        self.assertIn("X-Trace: 1", sent)

    def test_body_cap_enforced(self):
        big = b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 5000
        with self.assertRaises(net_policy.FetchError):
            self._run_fetch("https://example.com/a", None, payload=big, max_bytes=1024)
        out, _ = self._run_fetch("https://example.com/a", None, payload=big[:800], max_bytes=4096)
        self.assertEqual(out["status"], 200)

    def test_private_target_never_reaches_transport(self):
        with mock.patch("net_policy._connect_pinned") as conn:
            with self.assertRaises(net_policy.NetworkPolicyError):
                net_policy.fetch_document("http://10.0.0.1/steal")
        conn.assert_not_called()


class TestLegacyValidatorDelegation(unittest.TestCase):
    """旧校验器必须委托到共享策略（修掉 DNS 失败放行、补 CGNAT/映射地址）。"""

    def test_transport_validator_delegates(self):
        from adapters import transport
        with mock.patch("net_policy._resolve_all", return_value=([], "DNS 解析失败：塌了")):
            self.assertFalse(transport._validate_public_url("https://x.invalid/"))
        self.assertFalse(transport._validate_public_url("http://100.64.0.1/"))
        self.assertFalse(transport._validate_public_url("http://[::ffff:127.0.0.1]/"))
        with mock.patch("net_policy._resolve_all", return_value=(["93.184.216.34"], "")):
            self.assertTrue(transport._validate_public_url("https://example.com/"))


class TestDataLoaderWorkerUsesPolicy(unittest.TestCase):
    """内容派生下载必须走策略通道：内网地址拒绝、文件名不得越出工作区。"""

    def _run(self, instruction, ws: Path, fetch_impl):
        import asyncio
        from workers import data_loader_worker as dlw

        worker = dlw.DataLoaderWorker.__new__(dlw.DataLoaderWorker)
        with mock.patch("net_policy.fetch_document", side_effect=fetch_impl):
            return asyncio.run(worker.execute(instruction, {"workspace": str(ws)}))

    def test_private_url_is_refused_by_policy(self):
        import tempfile
        ws = Path(tempfile.mkdtemp(prefix="wm_dl_"))

        def _refuse(url, **k):
            raise net_policy.NetworkPolicyError("解析到非公网地址：10.0.0.5")

        out = json.loads(self._run("请加载数据集 http://10.0.0.5/secret.csv", ws, _refuse))
        self.assertEqual(out["status"], "failed")
        self.assertIn("网络策略拒绝", out["reason"])
        self.assertEqual([p for p in ws.rglob("*") if p.is_file()], [],
                         "被拒绝的下载不得落盘")

    def test_traversal_filename_stays_inside_workspace(self):
        import tempfile
        ws = Path(tempfile.mkdtemp(prefix="wm_dl_"))

        def _ok(url, **k):
            return {"status": 200, "raw": b"a,b\n1,2\n", "text": "a,b\n1,2\n", "headers": {}, "bytes": 8}

        # Windows 风格 URL：末段带反斜杠与上跳，旧实现会写到工作区之外
        out = json.loads(self._run("加载数据集 http://93.184.216.34/data\\..\\..\\evil.csv", ws, _ok))
        self.assertEqual(out["status"], "downloaded")
        written = Path(out["path"]).resolve()
        self.assertIn(ws.resolve(), written.parents, f"落盘越出工作区：{written}")
        self.assertEqual(written.name, "evil.csv")


if __name__ == "__main__":
    unittest.main()
