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
import os
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


class TestEgressPolicy(unittest.TestCase):
    """S2：出口方式显式区分 + 代理失败不降级直连（专项 §6）。

    控制流全用替身，不发真实请求；代理通过环境变量构造。
    """

    def setUp(self):
        patcher = mock.patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                     "ALL_PROXY", "all_proxy", "WM_CONTENT_FETCH_MODE"):
            os.environ.pop(name, None)

    def test_mode_defaults_to_inherit_and_validates_values(self):
        self.assertEqual(net_policy.connection_mode(), "inherit")
        os.environ["WM_CONTENT_FETCH_MODE"] = "direct"
        self.assertEqual(net_policy.connection_mode(), "direct")
        os.environ["WM_CONTENT_FETCH_MODE"] = "something-else"
        self.assertEqual(net_policy.connection_mode(), "inherit",
                         "非法取值按 inherit，不猜用户想要直连")

    def test_proxy_settings_reports_host_without_credentials(self):
        os.environ["HTTPS_PROXY"] = "http://alice:s3cr3t@127.0.0.1:7897"
        got = net_policy.proxy_settings()
        self.assertTrue(got["configured"])
        self.assertEqual(got["hosts"], ["127.0.0.1:7897"])
        self.assertNotIn("s3cr3t", json.dumps(got))
        self.assertNotIn("alice", json.dumps(got))

    def test_proxy_failure_detection(self):
        import urllib.error
        self.assertTrue(net_policy.is_proxy_failure(_fake_http_error(407)),
                        "407 是代理层失败")
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"
        refused = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        self.assertTrue(net_policy.is_proxy_failure(refused),
                        "配了代理且连接阶段失败 → 按代理失败归类"
                        "（代理没起来，不是源站不可达）")
        os.environ.pop("HTTPS_PROXY", None)
        self.assertFalse(net_policy.is_proxy_failure(refused),
                         "没配代理时同样的失败不是代理问题")
        self.assertFalse(net_policy.is_proxy_failure(ValueError("bad payload")))

    def test_classify_network_error_marks_proxy(self):
        import urllib.error
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"
        exc = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        self.assertEqual(net_policy.classify_network_error(exc), "proxy_error")

    def test_proxy_required_mode_is_declared_unsupported(self):
        os.environ["WM_CONTENT_FETCH_MODE"] = "proxy_required"
        with mock.patch("net_policy._connect_pinned") as connect:
            with self.assertRaises(net_policy.NetworkPolicyError) as ctx:
                net_policy.fetch_document("https://93.184.216.34/a.pdf")
            connect.assert_not_called()
        self.assertIn("不支持", str(ctx.exception))

    def test_egress_is_reported_but_process_env_is_never_mutated(self):
        """内容下载不得清理整进程代理（指令 §4-C0.3）：只报告，不动环境。"""
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"
        report = net_policy.proxy_settings_report()
        self.assertEqual(report["mode"], "inherit")
        self.assertTrue(report["proxy_configured"])
        self.assertEqual(report["proxy_hosts"], ["127.0.0.1:7897"])
        self.assertIn("HTTPS_PROXY", os.environ, "不得为内容下载清掉进程代理")
        os.environ["WM_CONTENT_FETCH_MODE"] = "direct"
        self.assertEqual(net_policy.proxy_settings_report()["mode"], "direct")
        self.assertIn("HTTPS_PROXY", os.environ, "direct 模式也只改判定，不改环境")
        self.assertFalse(hasattr(net_policy, "apply_direct_mode_env"),
                         "清环境的旧接口已移除（它会连带改变其它服务出口）")

    def test_require_egress_ok_refuses_only_proxy_required(self):
        net_policy.require_egress_ok()                 # inherit：不拒绝
        os.environ["WM_CONTENT_FETCH_MODE"] = "direct"
        net_policy.require_egress_ok()                 # direct：不拒绝
        os.environ["WM_CONTENT_FETCH_MODE"] = "proxy_required"
        with self.assertRaises(net_policy.NetworkPolicyError):
            net_policy.require_egress_ok()

    def test_public_content_url_positive_case(self):
        """正例不受影响：公网 IP 字面量（无需 DNS）仍放行。"""
        decision = net_policy.validate_public_url("https://93.184.216.34/a.pdf")
        self.assertTrue(decision.ok, decision.reason)

    def test_tls_verification_is_never_disabled(self):
        """TLS 失败不改校验：源码里不得出现关闭校验的写法（专项 §6）。

        禁用写法按片段拼出来，免得这条断言自己变成"含禁用写法"的文件。
        """
        banned = ("verify" + "=False",
                  "CERT_" + "NONE",
                  "_create_unverified_" + "context",
                  "check_hostname" + " = False",
                  "check_hostname" + "=False")
        for rel in ("net_policy.py", "adapters/transport.py",
                    "adapters/news.py", "annual_report_pdf.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text, f"{rel} 出现关闭 TLS 校验的写法：{token}")


def _fake_http_error(code: int):
    """构造一个带状态码的 HTTPError（不联网）。"""
    import urllib.error
    return urllib.error.HTTPError("https://example.com/x", code, "err", {}, None)


class _FakeResp:
    """假的 urlopen 响应：够 `get_bytes_via_urllib` 用（状态/头/分段 read）。"""

    def __init__(self, body: bytes, *, status: int = 200, headers: dict | None = None):
        self._body = bytes(body)
        self._pos = 0
        self.status = status
        self.code = status
        self.headers = dict(headers or {})

    def read(self, n: int = -1):
        if n is None or n < 0:
            out = self._body[self._pos:]
            self._pos = len(self._body)
            return out
        # 模拟真实流：一次 read(n) 不一定返回 n 字节，但也**不做无谓的多次**
        out = self._body[self._pos:self._pos + n]
        self._pos += len(out)
        return out

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestByteChannelMetadata(unittest.TestCase):
    """P1-e：取字节的通道必须带回**状态/类型/字节数**，且二进制往返无损。

    没有这些元信息时，"985 字节取件"到底是 HTTP 403 的 HTML 错误页、Content-Type 不对、
    下载被截断，还是真扫描件，从日志上完全分不出来——处置方式却各不相同。
    """

    def setUp(self):
        from adapters import transport
        self.transport = transport

    def _call(self, resp, url="https://example.com/a.pdf", **kw):
        with mock.patch.object(self.transport, "_throttle_and_rewrite", lambda u: u), \
                mock.patch.object(self.transport, "_require_egress_ok", lambda: None), \
                mock.patch.object(self.transport, "_validate_public_url", return_value=True), \
                mock.patch.object(self.transport.urllib.request, "urlopen",
                                  return_value=resp):
            return self.transport.get_bytes_via_urllib(url, **kw)

    def test_binary_round_trip_and_metadata(self):
        """PDF 是二进制：0x80-0xFF 必须原样带回（此前走 latin-1 文本往返）。"""
        body = b"%PDF-1.7\n" + bytes(range(256)) + b"\n%%EOF\n"
        r = self._call(_FakeResp(body, headers={"Content-Type": "application/pdf"}))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"], body)
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["content_type"], "application/pdf")
        self.assertEqual(r["body_bytes"], len(body))
        self.assertFalse(r["over_limit"])

    def test_http_error_returns_status_and_body_size_without_raising(self):
        """403 的 985 字节 HTML 页：**不抛**，状态与体积都要报出来。"""
        import urllib.error
        err = urllib.error.HTTPError(
            "https://example.com/a.pdf", 403, "Forbidden",
            {"Content-Type": "text/html; charset=utf-8"}, None)
        # 恰好 985 字节的错误页（"985 字节取件"就是这种形状）
        _body = b"<html>" + b"x" * (985 - len(b"<html>") - len(b"</html>")) + b"</html>"
        self.assertEqual(len(_body), 985)
        err.read = lambda n=-1: _body
        r = self._call(err)
        self.assertFalse(r["ok"])
        self.assertEqual(r["status"], 403)
        self.assertEqual(r["error_kind"], "http_error")
        self.assertEqual(r["body_bytes"], 985)

    def test_redirect_is_not_followed(self):
        r = self._call(_FakeResp(b"", status=302, headers={"Location": "https://evil/x"}))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_kind"], "http_redirect")

    def test_byte_limit_truncates_the_read_and_flags_it(self):
        """上限必须**按上限读**（不多读进内存），并如实标记 over_limit。"""
        body = b"%PDF-1.7\n" + b"a" * 5000
        r = self._call(_FakeResp(body), max_bytes=1000)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_kind"], "too_large")
        self.assertTrue(r["over_limit"])
        self.assertEqual(len(r["data"]), 1000)

    def test_chunked_transfer_encoding_is_recorded(self):
        """chunked 由 http.client 透明解块；是否 chunked 记下来备查。"""
        r = self._call(_FakeResp(b"%PDF-1.7\nx\n%%EOF\n",
                                 headers={"Content-Type": "application/pdf",
                                          "Transfer-Encoding": "chunked"}))
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["transfer_encoding"], "chunked")

    def test_ssrf_guard_blocks_before_any_request(self):
        """校验紧邻请求点：被 SSRF 守卫拦下时**不得**发出任何请求。"""
        called = {"n": 0}

        def _fake_urlopen(*_a, **_k):
            called["n"] += 1
            return _FakeResp(b"")

        with mock.patch.object(self.transport, "_throttle_and_rewrite", lambda u: u), \
                mock.patch.object(self.transport, "_require_egress_ok", lambda: None), \
                mock.patch.object(self.transport, "_validate_public_url", return_value=False), \
                mock.patch.object(self.transport.urllib.request, "urlopen", _fake_urlopen):
            r = self.transport.get_bytes_via_urllib("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_kind"], "ssrf_blocked")
        self.assertEqual(called["n"], 0, "被守卫拦下后不得发请求")

    def test_proxy_error_is_reported_and_never_falls_back_direct(self):
        """代理失败单列 `proxy_error`；本函数**只有一条通道**，不存在直连降级。"""
        import urllib.error
        exc = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        with mock.patch.object(self.transport, "_throttle_and_rewrite", lambda u: u), \
                mock.patch.object(self.transport, "_require_egress_ok", lambda: None), \
                mock.patch.object(self.transport, "_validate_public_url", return_value=True), \
                mock.patch.dict("os.environ",
                                {"HTTPS_PROXY": "http://127.0.0.1:7897"}, clear=False), \
                mock.patch.object(self.transport.urllib.request, "urlopen", side_effect=exc), \
                mock.patch.object(self.transport, "get_via_socket") as sock:
            r = self.transport.get_bytes_via_urllib("https://example.com/a.pdf")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error_kind"], "proxy_error")
        self.assertFalse(sock.called, "字节通道不得降级到第二条通道")

    def test_egress_blocked_before_request(self):
        """egress 未放行（离线/受限）时同样不发请求，类别由 net_policy 给出。"""
        called = {"n": 0}
        with mock.patch.object(self.transport, "_throttle_and_rewrite", lambda u: u), \
                mock.patch.object(self.transport, "_require_egress_ok",
                                  side_effect=RuntimeError("离线测试不得联网")), \
                mock.patch.object(self.transport.urllib.request, "urlopen",
                                  side_effect=lambda *a, **k: called.__setitem__("n", 1)):
            r = self.transport.get_bytes_via_urllib("https://example.com/a.pdf")
        self.assertFalse(r["ok"])
        self.assertTrue(r["error_kind"])
        self.assertEqual(called["n"], 0)


class TestTransportEgress(unittest.TestCase):
    """传输层出口纪律：代理失败不得降级直连（专项 §6 最小验收）。"""

    def setUp(self):
        from adapters import transport
        self.transport = transport

    def _run(self, exc, *, proxy=True):
        calls = {"socket": 0}
        env = {"HTTPS_PROXY": "http://127.0.0.1:7897"} if proxy else {}

        def _fake_socket(*_a, **_k):
            calls["socket"] += 1
            return "body"

        with mock.patch.dict("os.environ", env, clear=False), \
                mock.patch.object(self.transport, "get_via_urllib", side_effect=exc), \
                mock.patch.object(self.transport, "get_via_socket",
                                  side_effect=_fake_socket):
            try:
                return self.transport.dual_channel_get("https://example.com/a",
                                                       source="test"), calls, None
            except Exception as err:                 # noqa: BLE001 - 断言用
                return None, calls, err

    def test_proxy_failure_never_falls_back_to_direct(self):
        import urllib.error
        exc = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        out, calls, err = self._run(exc, proxy=True)
        self.assertIsNone(out)
        self.assertEqual(calls["socket"], 0, "代理失败后直连次数必须为 0")
        self.assertIsInstance(err, self.transport.ProxyEgressError)
        self.assertEqual(err.category, "proxy_error")

    def test_origin_failure_without_proxy_still_uses_the_second_channel(self):
        """没有代理参与时，源站侧失败仍走第二通道（既有反爬降级行为不变）。"""
        import http.client
        out, calls, err = self._run(http.client.RemoteDisconnected("closed"),
                                    proxy=False)
        self.assertEqual(out, "body")
        self.assertEqual(calls["socket"], 1)
        self.assertIsNone(err)

    def test_proxy_in_effect_blocks_fallback_even_after_connect(self):
        """反例（§3.3）：配了代理时 `RemoteDisconnected` 也不得触发 socket 直连。"""
        import http.client
        out, calls, err = self._run(http.client.RemoteDisconnected("closed"),
                                    proxy=True)
        self.assertIsNone(out)
        self.assertEqual(calls["socket"], 0, "代理在生效时不得落直连")
        self.assertIsInstance(err, self.transport.ProxyEgressError)

    def test_proxy_required_refuses_before_any_request(self):
        """反例（§3.3）：必须代理但无代理时，PDF/HTML 入口一次请求都不许发。"""
        import urllib.error
        calls = {"n": 0}

        def _urlopen(*_a, **_k):
            calls["n"] += 1
            raise urllib.error.URLError("should not be reached")

        with mock.patch.dict("os.environ",
                             {"WM_CONTENT_FETCH_MODE": "proxy_required"}),                 mock.patch("urllib.request.urlopen", side_effect=_urlopen):
            with self.assertRaises(Exception):
                self.transport.get_via_urllib("https://example.com/a")
            with self.assertRaises(Exception):
                self.transport.dual_channel_get("https://example.com/a")
        self.assertEqual(calls["n"], 0, "拒绝必须发生在发请求之前")

    def test_byte_channel_also_refuses_before_any_request_when_proxy_required(self):
        """同一道出口纪律也管**字节通道**：`proxy_required` 下一次请求都不许发。

        该通道把失败**类别化返回**（不抛），所以"没发请求"这件事必须单独断言——
        否则一个把异常吞掉的实现会看起来"很稳"，实际已经绕过了出口约束。
        """
        calls = {"n": 0}

        def _urlopen(*_a, **_k):
            calls["n"] += 1
            raise RuntimeError("should not be reached")

        with mock.patch.dict("os.environ",
                             {"WM_CONTENT_FETCH_MODE": "proxy_required"}), \
                mock.patch("urllib.request.urlopen", side_effect=_urlopen):
            r = self.transport.get_bytes_via_urllib(
                "https://proxy-required-probe.invalid/a.pdf",
                headers={"User-Agent": "wm-test"})
        self.assertFalse(r["ok"])
        self.assertEqual(calls["n"], 0, "拒绝必须发生在发请求之前")
        self.assertTrue(r["error_kind"], r)

    def test_proxy_error_classifies_for_adapters(self):
        import urllib.error
        exc = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        _out, _calls, err = self._run(exc, proxy=True)
        self.assertEqual(self.transport.classify_error(err), "proxy_error")


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
