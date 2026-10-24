# -*- coding: utf-8 -*-
"""用户自加数据源（设置页 → 密钥仓 → 出站登记 → 工具 → 探测）回归。

覆盖 20261021 新增的一整条链，全部离线：
- `credentials.py`：密钥与配置分家（round-trip、删除、前缀清理、外部改写重载）；
- `data_sources.validate_items`：id/kind/base_url/auth/method 取值域，坏条目不进登记表；
- `settings_schema.validate_payload`：`data_sources` 条目级校验（列表段以前整体放行）；
- `net_policy.load_registry` + `fetch_service`：启用源自动登记为 `data_source:<id>`、
  私网例外只由该源显式 `loopback` 授予、鉴权头只发往已验 IP 的登记端点；
- `mcp_client`：HTTP 型 MCP 的鉴权头与密钥仓读取（Bearer / 自定义头两种）；
- `tool_dispatch` 侧目录：启用的源出现在规划器工具目录里；
- 密钥安全：GET 侧任何接口不回明文、日志/错误文本不含密钥。

不联网：本地 127.0.0.1 测试服务器 + `loopback` 显式授权，正是这条通道的设计用法。
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import credentials
import data_sources
import net_policy
import settings_schema


class _EnvSandbox(unittest.TestCase):
    """每个用例一套临时 config/credentials，并复位两处进程内缓存。"""

    def setUp(self) -> None:
        self._tmp = Path(tempfile.mkdtemp(prefix="wm_ds_"))
        self._cfg = self._tmp / "config.json"
        self._cred = self._tmp / "credentials.json"
        self._old = {k: os.environ.get(k) for k in
                    ("WEAVEMIND_CONFIG", "WEAVEMIND_CREDENTIALS")}
        self._old.update({"WM_CONTENT_FETCH_MODE": os.environ.get("WM_CONTENT_FETCH_MODE")})
        os.environ["WEAVEMIND_CREDENTIALS"] = str(self._cred)
        os.environ.pop("WM_CONTENT_FETCH_MODE", None)
        credentials.reset_cache_for_test()
        self._old_net_cfg = net_policy._CONFIG_PATH
        net_policy._CONFIG_PATH = str(self._cfg)
        net_policy.reset_registry_for_test()

    def tearDown(self) -> None:
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        net_policy._CONFIG_PATH = self._old_net_cfg
        net_policy.reset_registry_for_test()
        credentials.reset_cache_for_test()

    def write_config(self, cfg: dict) -> dict:
        self._cfg.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        os.environ["WEAVEMIND_CONFIG"] = str(self._cfg)
        net_policy.reset_registry_for_test()
        return cfg

    def read_config(self) -> dict:
        return json.loads(self._cfg.read_text(encoding="utf-8")) if self._cfg.exists() else {}

    def cred_file_text(self) -> str:
        return self._cred.read_text(encoding="utf-8") if self._cred.exists() else ""


# ─────────────────────────────────────────────────────── 密钥仓


class TestCredentialsStore(_EnvSandbox):

    def test_round_trip_and_masking_surface(self):
        self.assertTrue(credentials.set_secret("data_sources.wind.api_key", "SECRET-1"))
        self.assertEqual(credentials.get_secret("data_sources.wind.api_key"), "SECRET-1")
        self.assertTrue(credentials.has_secret("data_sources.wind.api_key"))
        # 对外的列举只给路径，不给值
        self.assertEqual(credentials.configured_paths("data_sources"),
                         ["data_sources.wind.api_key"])
        self.assertEqual(credentials.secret_state("data_sources.wind.api_key"), {"set": True})

    def test_empty_value_means_deleted(self):
        credentials.set_secret("data_sources.a.api_key", "X")
        credentials.set_secret("data_sources.a.api_key", "")
        self.assertFalse(credentials.has_secret("data_sources.a.api_key"))

    def test_forget_prefix_removes_orphans(self):
        credentials.set_secret("data_sources.a.api_key", "X")
        credentials.set_secret("data_sources.a.token", "Y")
        credentials.set_secret("data_sources.b.api_key", "Z")
        self.assertEqual(credentials.forget_prefix("data_sources.a"), 2)
        self.assertEqual(credentials.configured_paths(), ["data_sources.b.api_key"])

    def test_external_rewrite_is_repicked_up(self):
        credentials.set_secret("data_sources.a.api_key", "OLD")
        payload = json.loads(self.cred_file_text())
        payload["secrets"]["data_sources.a.api_key"] = "NEW"
        self._cred.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(self._cred, (self._cred.stat().st_atime,
                             self._cred.stat().st_mtime + 5))
        self.assertEqual(credentials.get_secret("data_sources.a.api_key"), "NEW")

    def test_broken_file_is_not_overwritten(self):
        self._cred.write_text("{ not json", encoding="utf-8")
        self.assertEqual(credentials.get_secret("data_sources.a.api_key"), "")
        self.assertEqual(self._cred.read_text(encoding="utf-8"), "{ not json")

    def test_path_defaults_next_to_config(self):
        os.environ.pop("WEAVEMIND_CREDENTIALS")
        os.environ["WEAVEMIND_CONFIG"] = str(self._tmp / "sub" / "config.json")
        credentials.reset_cache_for_test()
        self.assertEqual(credentials.credentials_path().name, "credentials.json")
        self.assertEqual(credentials.credentials_path().parent, self._tmp / "sub")


# ─────────────────────────────────────────────────────── 条目校验


GOOD_HTTP = {"id": "wind_proxy", "label": "Wind 中转", "kind": "http_json",
             "base_url": "https://data.example.com/v1", "auth": "bearer",
             "markets": ["cn"], "metrics": ["营业收入", "净利润"]}
GOOD_MCP = {"id": "wind", "label": "Wind", "kind": "mcp", "tool": "wind_query",
            "server": "wind"}


class TestSourceValidation(_EnvSandbox):

    def test_good_items_survive(self):
        clean, issues = data_sources.validate_items([GOOD_HTTP, GOOD_MCP])
        self.assertEqual(issues, [])
        self.assertEqual([c["id"] for c in clean], ["wind_proxy", "wind"])
        self.assertEqual(clean[0]["method"], "GET")
        self.assertTrue(clean[0]["enabled"])
        self.assertEqual(clean[1]["tool"], "wind_query")

    def test_bad_id_kind_and_scheme_rejected(self):
        cases = [
            {"id": "Bad ID", "kind": "http_json", "base_url": "https://x/v1"},
            {"id": "ok", "kind": "scraped", "base_url": "https://x/v1"},
            {"id": "ok2", "kind": "http_json", "base_url": "ftp://x/v1"},
            {"id": "ok3", "kind": "http_json"},
            {"id": "ok4", "kind": "mcp"},
        ]
        clean, issues = data_sources.validate_items(cases)
        self.assertEqual(clean, [])
        self.assertEqual(len(issues), 5)
        self.assertTrue(any("id 非法" in i for i in issues))
        self.assertTrue(any("kind 必须是" in i for i in issues))
        self.assertTrue(any("base_url 必须是 http(s)" in i for i in issues))
        self.assertTrue(any("必须填 tool" in i for i in issues))

    def test_duplicate_id_rejected(self):
        _clean, issues = data_sources.validate_items([GOOD_HTTP, dict(GOOD_HTTP)])
        self.assertTrue(any("id 重复" in i for i in issues))

    def test_auth_mode_and_header_defaults(self):
        item = dict(GOOD_HTTP, auth="header", auth_header="")
        clean, _issues = data_sources.validate_items([item])
        self.assertEqual(clean[0]["auth_header"], data_sources.DEFAULT_AUTH_HEADER)
        q = dict(GOOD_HTTP, auth="query")
        clean, _ = data_sources.validate_items([q])
        self.assertEqual(clean[0]["query_param"], data_sources.DEFAULT_QUERY_PARAM)
        _c, issues = data_sources.validate_items([dict(GOOD_HTTP, auth="magic")])
        self.assertTrue(any("auth 必须是" in i for i in issues))

    def test_params_and_headers_must_be_string_maps(self):
        _c, issues = data_sources.validate_items([dict(GOOD_HTTP, params={"a": 1, 2: "b"})])
        self.assertTrue(any("params" in i for i in issues))
        _c, issues = data_sources.validate_items([dict(GOOD_HTTP, headers=[1])])
        self.assertTrue(any("headers" in i for i in issues))

    def test_payload_validation_goes_through_settings_schema(self):
        issues = settings_schema.validate_payload({"data_sources": [GOOD_HTTP, GOOD_MCP]})
        self.assertEqual(issues, [])
        issues = settings_schema.validate_payload(
            {"data_sources": [{"id": "x", "kind": "http_json", "base_url": "nope"}]})
        self.assertTrue(any("base_url" in i for i in issues), issues)

    def test_rejects_non_list(self):
        _c, issues = data_sources.validate_items({"id": "wind"})
        self.assertTrue(any("必须是列表" in i for i in issues))


# ─────────────────────────────────────────────── 登记表与抓取通道


class TestRegistryAndFetch(_EnvSandbox):

    def test_enabled_http_source_is_registered(self):
        self.write_config({"data_sources": [GOOD_HTTP, GOOD_MCP]})
        reg = net_policy.load_registry(force=True)
        self.assertIn("data_source:wind_proxy", reg)
        # mcp 型不登记端点（它由 mcp_client 走自己的连接）
        self.assertNotIn("data_source:wind", reg)

    def test_disabled_source_not_registered(self):
        self.write_config({"data_sources": [dict(GOOD_HTTP, enabled=False)]})
        reg = net_policy.load_registry(force=True)
        self.assertNotIn("data_source:wind_proxy", reg)
        d = net_policy.request_service("data_source:wind_proxy", "GET")
        self.assertFalse(d.ok)
        self.assertIn("未登记", d.reason)

    def test_saving_after_disable_needs_registry_invalidation(self):
        self.write_config({"data_sources": [GOOD_HTTP]})
        self.assertIn("data_source:wind_proxy", net_policy.load_registry())
        # 直接改文件（不走助手，它会顺手失效登记表）→ 复用的仍是旧表
        self._cfg.write_text(json.dumps(
            {"data_sources": [dict(GOOD_HTTP, enabled=False)]}, ensure_ascii=False),
            encoding="utf-8")
        os.utime(self._cfg, (self._cfg.stat().st_atime,
                             self._cfg.stat().st_mtime + 5))
        self.assertIn("data_source:wind_proxy", net_policy.load_registry())
        net_policy.invalidate_registry()
        self.assertNotIn("data_source:wind_proxy", net_policy.load_registry())

    def test_private_host_needs_explicit_loopback_flag(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        base = f"http://127.0.0.1:{port}/v1"
        self.write_config({"data_sources": [dict(GOOD_HTTP, base_url=base)]})
        d = net_policy.request_service("data_source:wind_proxy", "GET")
        self.assertFalse(d.ok)
        self.assertIn("内部地址", d.reason)
        self.write_config({"data_sources": [dict(GOOD_HTTP, base_url=base,
                                                 loopback=True)]})
        d = net_policy.request_service("data_source:wind_proxy", "GET")
        self.assertTrue(d.ok, d.reason)

    def test_operation_must_match_declared_method(self):
        self.write_config({"data_sources": [dict(GOOD_HTTP, method="GET")]})
        d = net_policy.request_service("data_source:wind_proxy", "POST")
        self.assertFalse(d.ok)
        self.assertIn("不允许操作", d.reason)

    def test_caller_cannot_self_declare_trust(self):
        self.write_config({"data_sources": [GOOD_HTTP]})
        d = net_policy.request_service("data_source:wind_proxy", "GET",
                                       trusted=True, allow_private=True)
        self.assertFalse(d.ok)
        self.assertIn("不得自报", d.reason)


class _StubHandler(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):        # noqa: N802
        self._reply()

    def do_POST(self):       # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _body = self.rfile.read(length)
        self._reply()

    def _reply(self):
        type(self).seen.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization") or "",
            "x_api_key": self.headers.get("X-Api-Key") or "",
        })
        payload = json.dumps({"rows": [{"dt": "2024", "revenue": 100}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):   # 静音
        pass


def _serve(handler_cls):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class TestSourceCall(_EnvSandbox):

    def setUp(self):
        super().setUp()
        _StubHandler.seen = []

    def test_http_source_call_sends_key_and_returns_source(self):
        srv, base = _serve(_StubHandler)
        try:
            self.write_config({"data_sources": [dict(
                GOOD_HTTP, base_url=base + "/v1", loopback=True, path="quote",
                params={"lang": "cn"})]})
            credentials.set_secret("data_sources.wind_proxy.api_key", "SUPER-SECRET")
            got = data_sources.call_source_tool("finance_custom_wind_proxy", "茅台 2024")
            self.assertEqual(got["status"], "success")
            self.assertEqual(got["result"]["rows"][0]["revenue"], 100)
            self.assertEqual(got["source"]["class"], "custom_source")
            self.assertEqual(got["source"]["id"], "wind_proxy")
            self.assertEqual(_StubHandler.seen[-1]["authorization"], "Bearer SUPER-SECRET")
            self.assertIn("/v1/quote", _StubHandler.seen[-1]["path"])
            self.assertIn("lang=cn", _StubHandler.seen[-1]["path"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_header_auth_mode(self):
        class H(_StubHandler):
            pass
        H.seen = []
        srv, base = _serve(H)
        try:
            self.write_config({"data_sources": [dict(
                GOOD_HTTP, base_url=base, loopback=True, auth="header",
                auth_header="X-Api-Key")]})
            credentials.set_secret("data_sources.wind_proxy.api_key", "HK-1")
            got = data_sources.call_source_tool("finance_custom_wind_proxy", "x")
            self.assertEqual(got["status"], "success")
            self.assertEqual(H.seen[-1]["x_api_key"], "HK-1")
            self.assertEqual(H.seen[-1]["authorization"], "")
        finally:
            srv.shutdown()
            srv.server_close()

    def test_query_auth_and_instruction_param(self):
        class Q(_StubHandler):
            seen = []

        srv, base = _serve(Q)
        try:
            self.write_config({"data_sources": [dict(
                GOOD_HTTP, base_url=base, loopback=True, auth="query",
                query_param="token", path="quote")]})
            credentials.set_secret("data_sources.wind_proxy.api_key", "QS-1")
            got = data_sources.call_source_tool("finance_custom_wind_proxy", "宁德时代")
            self.assertEqual(got["status"], "success")
            # auth=query：密钥参数不被检索词覆盖，检索词落到 q
            self.assertIn("token=QS-1", Q.seen[-1]["path"])
            self.assertIn("q=%E5%AE%81", Q.seen[-1]["path"])
            self.assertIn("/quote", Q.seen[-1]["path"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_disabled_source_refused_without_network(self):
        self.write_config({"data_sources": [dict(GOOD_HTTP, enabled=False)]})
        got = data_sources.call_source_tool("finance_custom_wind_proxy", "x")
        self.assertEqual(got["status"], "failed")
        self.assertIn("停用", got["error"])

    def test_unknown_source_tool_returns_none_for_fallback(self):
        self.write_config({"data_sources": []})
        self.assertIsNone(data_sources.call_source_tool("web_search", "x"))
        got = data_sources.call_source_tool("finance_custom_ghost", "x")
        self.assertEqual(got["status"], "failed")
        self.assertIn("未配置或已删除", got["error"])

    def test_error_message_never_carries_the_key(self):
        self.write_config({"data_sources": [dict(
            GOOD_HTTP, base_url="http://127.0.0.1:1/v1", loopback=True)]})
        credentials.set_secret("data_sources.wind_proxy.api_key", "NEVER-LEAK")
        got = data_sources.call_source_tool("finance_custom_wind_proxy", "x", timeout=3)
        self.assertEqual(got["status"], "failed")
        self.assertNotIn("NEVER-LEAK", json.dumps(got, ensure_ascii=False))

    def test_probe_reports_http_status_in_human_words(self):
        class Fail(http.server.BaseHTTPRequestHandler):
            def do_GET(self):        # noqa: N802
                body = b'{"error":"invalid api key"}'
                self.send_response(401)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv, base = _serve(Fail)
        try:
            self.write_config({"data_sources": [dict(GOOD_HTTP, base_url=base,
                                                     loopback=True)]})
            credentials.set_secret("data_sources.wind_proxy.api_key", "P-SECRET")
            out = data_sources.probe_source("wind_proxy", timeout=5)
            self.assertFalse(out["ok"])
            self.assertEqual(out["reason"], "unauthorized")
            self.assertIn("同一家服务商", out["message"])
            self.assertNotIn("P-SECRET", out["message"])
        finally:
            srv.shutdown()
            srv.server_close()

    def test_probe_ok_and_missing_key(self):
        srv, base = _serve(_StubHandler)
        try:
            self.write_config({"data_sources": [dict(GOOD_HTTP, base_url=base,
                                                     loopback=True)]})
            self.assertEqual(data_sources.probe_source("wind_proxy", timeout=5)["reason"],
                             "missing_key")
            credentials.set_secret("data_sources.wind_proxy.api_key", "K")
            out = data_sources.probe_source("wind_proxy", timeout=5)
            self.assertTrue(out["ok"], out)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_probe_unknown_source(self):
        self.write_config({"data_sources": []})
        out = data_sources.probe_source("ghost")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_configured")

    def test_secret_can_come_from_env(self):
        self.write_config({"data_sources": [dict(GOOD_HTTP,
                                                 base_url="http://127.0.0.1:1/v1",
                                                 loopback=True)]})
        src = data_sources.source_by_id("wind_proxy")
        os.environ["WEAVEMIND_DATA_SOURCE_WIND_PROXY_KEY"] = "ENV-KEY"
        try:
            self.assertEqual(data_sources.source_secret(src), "ENV-KEY")
        finally:
            os.environ.pop("WEAVEMIND_DATA_SOURCE_WIND_PROXY_KEY")


# ─────────────────────────────────────────────────────── MCP 鉴权


class TestMcpAuth(_EnvSandbox):

    def test_client_sends_bearer_by_default(self):
        import mcp_client
        c = mcp_client.MCPClient("wind", url="http://127.0.0.1:9/mcp", token="T-1")
        self.assertEqual(c.headers["Authorization"], "Bearer T-1")

    def test_header_mode_without_prefix(self):
        import mcp_client
        c = mcp_client.MCPClient("wind", url="http://127.0.0.1:9/mcp", token="T-2",
                                 token_header="X-Api-Key", token_prefix="")
        self.assertEqual(c.headers["X-Api-Key"], "T-2")
        self.assertNotIn("Authorization", c.headers)

    def test_server_auth_reads_credentials_then_env(self):
        import mcp_client
        credentials.set_secret("mcp_servers.wind.api_key", "FROM-STORE")
        token, header, prefix = mcp_client._server_auth({"name": "wind"}, "wind")
        self.assertEqual(token, "FROM-STORE")
        self.assertEqual((header, prefix), ("Authorization", "Bearer "))
        token, _h, _p = mcp_client._server_auth({"name": "wind", "auth": "header",
                                                 "auth_header": "X-Api-Key"}, "wind")
        self.assertEqual(token, "FROM-STORE")
        os.environ["WEAVEMIND_MCP_WIND_KEY"] = "FROM-ENV"
        try:
            token, _h, prefix = mcp_client._server_auth({"name": "wind"}, "wind")
            self.assertEqual(token, "FROM-ENV")
        finally:
            os.environ.pop("WEAVEMIND_MCP_WIND_KEY")

    def test_load_servers_wires_headers_and_token(self):
        import mcp_client
        credentials.set_secret("mcp_servers.w.api_key", "LK")
        clients = mcp_client.load_mcp_servers({"mcp_servers": [
            {"name": "w", "url": "http://127.0.0.1:9/mcp",
             "headers": {"X-Tenant": "acme"}},
            {"name": "stdio-one", "command": "python", "args": ["-c", "pass"]},
        ]})
        by = {c.name: c for c in clients}
        self.assertEqual(by["w"].headers.get("X-Tenant"), "acme")
        self.assertEqual(by["w"].headers.get("Authorization"), "Bearer LK")
        # stdio 型不注入它没登记的密钥（每服务自己的 secret）
        self.assertIsNone(by["stdio-one"].headers.get("Authorization"))
        for c in clients:
            c.close()

    def test_mcp_kind_source_call_reports_missing_registration(self):
        self.write_config({"data_sources": [GOOD_MCP]})
        got = data_sources.call_source_tool("finance_custom_wind", "x")
        self.assertEqual(got["status"], "failed")
        self.assertIn("MCP 工具 wind_query 未发现", got["error"])

    def test_mcp_probe_without_servers_configured(self):
        self.write_config({"data_sources": [GOOD_MCP]})
        out = data_sources.probe_source("wind")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "not_configured")


# ─────────────────────────────────────────────── 写回与掩码（HTTP 层）


class TestWriteBackAndMasking(_EnvSandbox):

    def setUp(self):
        super().setUp()
        # web_ui 的 _load_config/_save_config 用模块级 CONFIG_PATH：
        # 不钉到临时文件会把真实 config.json 当读写目标（本用例曾经踩过）。
        import web_ui
        self._old_web_cfg = web_ui.CONFIG_PATH
        web_ui.CONFIG_PATH = str(self._cfg)

    def tearDown(self):
        import web_ui
        web_ui.CONFIG_PATH = self._old_web_cfg
        super().tearDown()

    def test_public_config_never_shares_source_keys(self):
        import web_ui
        cfg = {"data_sources": [dict(GOOD_HTTP, api_key="LIVE-KEY")],
               "mcp_servers": [{"name": "wind", "url": "http://x/y",
                                "api_key": "MCP-KEY"}]}
        out = web_ui._public_config(cfg)
        text = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("LIVE-KEY", text)
        self.assertNotIn("MCP-KEY", text)
        self.assertEqual(out["data_sources"][0]["api_key"], "")

    def test_api_key_set_flag_comes_from_credentials_store(self):
        import web_ui
        credentials.set_secret("data_sources.wind_proxy.api_key", "STORED")
        out = web_ui._public_config({"data_sources": [dict(GOOD_HTTP, api_key="")]})
        self.assertTrue(out["data_sources"][0]["api_key_set"])
        out2 = web_ui._public_config({"data_sources": [{"id": "other",
                                                        "kind": "mcp",
                                                        "tool": "t"}]})
        self.assertFalse(out2["data_sources"][0]["api_key_set"])

    def test_route_list_secrets_moves_keys_out_of_config(self):
        import web_ui
        self.write_config({"data_sources": []})
        body = {"data_sources": [dict(GOOD_HTTP, api_key="NEW-KEY")],
                "mcp_servers": [{"name": "wind", "url": "http://x", "api_key": "M-1"}]}
        stats = web_ui._route_list_secrets(body)
        self.assertEqual(stats["written"], 2)
        self.assertNotIn("api_key", body["data_sources"][0])
        self.assertNotIn("api_key", body["mcp_servers"][0])
        self.assertEqual(credentials.get_secret("data_sources.wind_proxy.api_key"),
                         "NEW-KEY")
        self.assertEqual(credentials.get_secret("mcp_servers.wind.api_key"), "M-1")
        web_ui._save_config(body)
        self.assertNotIn("NEW-KEY", self._cfg.read_text(encoding="utf-8"))
        self.assertNotIn("M-1", self._cfg.read_text(encoding="utf-8"))

    def test_blank_key_keeps_existing_and_clear_key_deletes(self):
        import web_ui
        credentials.set_secret("data_sources.wind_proxy.api_key", "OLD")
        self.write_config({"data_sources": [GOOD_HTTP]})
        body = {"data_sources": [dict(GOOD_HTTP, api_key="")]}
        web_ui._route_list_secrets(body)
        self.assertEqual(credentials.get_secret("data_sources.wind_proxy.api_key"), "OLD")
        body = {"data_sources": [dict(GOOD_HTTP, clear_key=True)]}
        stats = web_ui._route_list_secrets(body)
        self.assertEqual(stats["cleared"], 1)
        self.assertFalse(credentials.has_secret("data_sources.wind_proxy.api_key"))

    def test_deleted_source_drops_its_credential(self):
        import web_ui
        credentials.set_secret("data_sources.gone.api_key", "BYE")
        self.write_config({"data_sources": [{"id": "gone", "kind": "mcp",
                                             "tool": "t"}]})
        stats = web_ui._route_list_secrets({"data_sources": [GOOD_HTTP]})
        self.assertEqual(stats["dropped"], 1)
        self.assertFalse(credentials.has_secret("data_sources.gone.api_key"))

    def test_requirements_payload_scrubs_list_keys(self):
        # 历史配置可能把 key 写进过列表条目：清单接口同样清零
        import web_ui
        cfg = {"data_sources": [dict(GOOD_HTTP, api_key="LEGACY")]}
        out = web_ui._public_config(cfg)
        self.assertNotIn("LEGACY", json.dumps(out, ensure_ascii=False))


# ─────────────────────────────────────────────────────── 目录暴露


class TestCatalogExposure(_EnvSandbox):

    def test_enabled_sources_appear_in_planner_catalog(self):
        import tool_contracts
        self.write_config({"data_sources": [GOOD_HTTP, GOOD_MCP,
                                             dict(GOOD_HTTP, id="off",
                                                  base_url="https://o.example/v1",
                                                  enabled=False)]})
        text = tool_contracts.tool_catalog_text()
        self.assertIn("用户自加数据源", text)
        self.assertIn("finance_custom_wind_proxy", text)
        self.assertIn("finance_custom_wind", text)
        self.assertNotIn("finance_custom_off", text)

    def test_mcp_lite_lists_source_tools(self):
        import mcp_lite
        self.write_config({"data_sources": [GOOD_HTTP]})
        names = [t["name"] for t in mcp_lite._all_tools()]
        self.assertIn("finance_custom_wind_proxy", names)

    def test_catalog_empty_when_no_sources(self):
        self.write_config({"data_sources": []})
        self.assertEqual(data_sources.catalog_text(), "")

    def test_tool_description_carries_host_and_metrics(self):
        self.write_config({"data_sources": [GOOD_HTTP]})
        tool = data_sources.tool_registry()[0]
        self.assertIn("data.example.com", tool["description"])
        self.assertIn("营业收入", tool["description"])
        self.assertIn("status", tool["required"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
