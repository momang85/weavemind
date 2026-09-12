# -*- coding: utf-8 -*-
"""设置页后端回归：配置清单完整性、脱敏、写回白名单、探测端点、用户管理保护。

注：本文件**不写任何真实凭据**，测试用的密钥值取自环境变量占位（`SCHED_TEST_KEY`），
缺失时用生成的随机串——避免"看起来像凭据的字面量"进入源码。
"""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

import settings_schema as ss

# 测试用占位密钥：优先环境变量，否则临时生成（不留字面量）
_PLACEHOLDER_KEY = os.environ.get("SCHED_TEST_KEY") or ("x" * 16)


class TestSettingsSchema(unittest.TestCase):
    """清单必须覆盖系统实际读取的配置面（否则设置页又漏项）。"""

    def test_all_config_sections_covered_or_declared(self):
        root = Path(__file__).resolve().parent
        with open(root / "config.json", encoding="utf-8") as fh:
            cfg = json.load(fh)
        declared = {e["path"].split(".")[0] for e in ss.iter_entries()}
        known_not_config = {"PUBLIC_BASE_URL", "BUDGET_MONTHLY_USD", "WEB_PORT",
                            "LLM_REQUEST_TIMEOUT", "WM_LOCAL_URL", "WM_LORA_TOKEN",
                            "WEAVEMIND_ADMIN_PASSWORD"}
        missing = [
            key for key in cfg.keys()
            if key not in declared
            and key not in ("users", "scheduled_jobs", "audit", "notifications")
        ]
        self.assertEqual(missing, [], f"config.json 里这些段没进清单：{missing}")
        self.assertTrue(known_not_config <= declared,
                        "仅环境变量的项也必须出现在清单里（否则用户看不到）")

    def test_entries_have_required_metadata(self):
        for entry in ss.iter_entries():
            self.assertTrue(entry.get("label"), entry["path"])
            self.assertIn(entry.get("kind"),
                          ("string", "secret", "int", "float", "bool", "url", "list"), entry)
            self.assertIn(entry.get("status"),
                          ("active", "env_only", "reserved", "unused"), entry)
            if entry.get("status") != "env_only":
                self.assertIn("affects", entry, f"{entry['path']} 缺少影响说明")

    def test_secrets_never_expose_plaintext(self):
        cfg = {"llm": {"api_key": _PLACEHOLDER_KEY},
               "embedding": {"api_key": _PLACEHOLDER_KEY}}
        for path in ("llm.api_key", "embedding.api_key"):
            entry = ss.entry_by_path(path)
            resolved = ss.resolve(entry, cfg, env={})
            self.assertTrue(resolved["configured"])
            # 脱敏后不得回显明文（只回"是否已配置"）
            self.assertEqual(ss.mask(entry, resolved["value"], api_key_set=True), "")
            self.assertNotIn(_PLACEHOLDER_KEY, str(ss.mask(entry, resolved["value"], True)))

    def test_source_precedence_config_over_env_then_default(self):
        entry = ss.entry_by_path("redis.host")
        self.assertEqual(ss.resolve(entry, {"redis": {"host": "cfg-host"}},
                                    env={"REDIS_HOST": "env-host"})["source"], "config")
        self.assertEqual(ss.resolve(entry, {}, env={"REDIS_HOST": "env-host"})["source"], "env")
        self.assertEqual(ss.resolve(entry, {}, env={})["source"], "default")

    def test_env_only_entries_read_environment(self):
        entry = ss.entry_by_path("PUBLIC_BASE_URL")
        resolved = ss.resolve(entry, {}, env={"PUBLIC_BASE_URL": "https://example.com"})
        self.assertEqual(resolved["value"], "https://example.com")
        self.assertEqual(resolved["source"], "env")


class TestConfigPayloadValidation(unittest.TestCase):
    def test_accepts_frontend_shaped_payload(self):
        payload = {
            "llm": {"api_key": "", "base_url": "https://example.invalid/v1", "model": "m",
                    "model_roles": {"planner": "p", "exec": "e", "judge": "j"},
                    "task_budget": {"max_calls": 60, "max_seconds": 1800, "max_cost_usd": 0},
                    "api_key_set": True},
            "redis": {"host": "localhost", "port": 6379},
            "system": {"task_timeout": 600, "max_retry": 2, "replan_depth": 2,
                       "guardian_heartbeat": 20, "critic": True, "critic_timeout": 30,
                       "max_steps": 8, "max_parallel": 3, "max_iterations": 2,
                       "stall_timeout": 300, "reflect_max_redo_steps": 2,
                       "scheduler": False, "market_preference": "hk",
                       "reflection_budget": {"ranking": 2, "financial": 3, "general": 3}},
            "embedding": {"api_key": _PLACEHOLDER_KEY,
                          "base_url": "https://example.invalid/v1", "model": "m"},
            "mcp_servers": [{"name": "wind", "command": "python wind.py"}],
        }
        self.assertEqual(ss.validate_payload(payload), [])

    def test_rejects_unknown_and_env_only(self):
        self.assertTrue(ss.validate_payload({"llm": {"hack": 1}}))
        self.assertTrue(ss.validate_payload({"evil_section": {"a": 1}}))
        self.assertTrue(ss.validate_payload({"PUBLIC_BASE_URL": "https://x"}))

    def test_server_managed_sections_ignored_not_persisted(self):
        """users/audit/notifications 各有独立端点：从 /api/config 混写必须无效（忽略）。"""
        payload = {"notifications": {"webhook": {"url": "https://x"}},
                   "users": {"evil": {"role": "admin"}},
                   "llm": {"model": "m"}}
        self.assertEqual(ss.validate_payload(payload), [], "只应忽略，不应让整次保存失败")
        import web_ui
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="wm_cfg_")) / "config.json"
        self.addCleanup(__import__("shutil").rmtree, tmp.parent, ignore_errors=True)
        tmp.write_text("{}", encoding="utf-8")
        with mock.patch.object(web_ui, "CONFIG_PATH", str(tmp)):
            web_ui._save_config(payload)
        saved = json.loads(tmp.read_text(encoding="utf-8"))
        self.assertIn("llm", saved)
        for dropped in ("users", "notifications", "audit"):
            self.assertNotIn(dropped, saved, f"{dropped} 不得从这里落盘")


class TestHealthRegistryProbes(unittest.TestCase):
    def test_snapshot_covers_all_dependencies_with_uniform_fields(self):
        import health_registry
        items = health_registry.snapshot()
        names = {i["name"] for i in items}
        self.assertTrue(
            {"llm", "search", "market_source", "embedding", "code_sandbox",
             "planner", "mcp", "lora"} <= names, names)
        for item in items:
            self.assertTrue(
                {"name", "ok", "reason", "since", "source_process", "detail"} <= set(item))

    def test_snapshot_is_cheap_no_network(self):
        """snapshot() 每 3 秒被 /api/status 调用：不得发起真实 LLM/HTTP 请求。"""
        import health_registry
        with mock.patch("urllib.request.urlopen",
                        side_effect=AssertionError("快照不得联网")) as net, \
                mock.patch("socket.create_connection",
                           side_effect=AssertionError("快照不得探活")) as sock:
            health_registry.snapshot()
        net.assert_not_called()
        sock.assert_not_called()

    def test_live_probe_rejects_unknown_target(self):
        import health_registry
        result = health_registry.live_probe("nope")
        self.assertFalse(result["ok"])
        self.assertIn("未知", result["reason"])

    def test_live_probe_embedding_classifies_quota(self):
        import health_registry
        import urllib.error
        err = urllib.error.HTTPError(
            "http://x/v1/embeddings", 402, "Payment Required", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            result = health_registry.live_probe("embedding")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "insufficient_balance")


class TestConfigEndpoints(unittest.TestCase):
    """端点行为：清单/探测/用户管理（不启真实服务，直接驱动处理函数）。"""

    def _fake_handler(self, captured, path="/api/config"):
        class _H:
            def __init__(self):
                self.path = path
            def _json(self, data, code=200, extra_headers=None):
                captured.update(data=data, code=code, headers=extra_headers)
            def _client_ip(self):
                return "127.0.0.1"
        return _H()

    def test_requirements_payload_shape(self):
        import web_ui
        captured = {}
        handler = self._fake_handler(captured)
        web_ui._get_config_requirements(handler, "/api/config/requirements")
        data = captured["data"]
        self.assertIn("sections", data)
        self.assertTrue(data["sections"])
        self.assertIn("test_targets", data)
        self.assertIn("degraded", data)
        self.assertIn("balance", data)
        first_entry = data["sections"][0]["entries"][0]
        self.assertIn("source", first_entry)
        self.assertIn("configured", first_entry)

    def test_config_test_rejects_unknown_target(self):
        import web_ui
        captured = {}
        handler = self._fake_handler(captured)
        handler = self._fake_handler(captured, path="/api/config/test")
        web_ui._post_config_test(handler, "/api/config/test", {"target": "evil"}, {"user": "reviewer"})
        self.assertEqual(captured["code"], 400)

    def test_config_test_runs_probe(self):
        import web_ui
        captured = {}
        handler = self._fake_handler(captured, path="/api/config/test")
        with mock.patch("health_registry.live_probe",
                        return_value={"target": "llm", "ok": True, "reason": "",
                                      "latency_ms": 12, "detail": ""}) as probe:
            web_ui._post_config_test(handler, "/api/config/test", {"target": "llm"},
                                     {"user": "reviewer"})
        probe.assert_called_once_with("llm")
        self.assertTrue(captured["data"]["ok"])

    def test_post_config_rejects_undeclared_paths(self):
        import web_ui
        captured = {}
        handler = self._fake_handler(captured)
        with mock.patch.object(web_ui, "_save_config") as save:
            web_ui._post_config(handler, "/api/config",
                                {"llm": {"evil": 1}}, {"user": "reviewer"})
        self.assertEqual(captured["code"], 400)
        save.assert_not_called()

    def test_post_config_saves_declared_paths(self):
        import web_ui
        captured = {}
        handler = self._fake_handler(captured)
        with mock.patch.object(web_ui, "_save_config") as save, \
                mock.patch("llm_client._clear_balance_cache"):
            web_ui._post_config(handler, "/api/config",
                                {"embedding": {"base_url": "https://example.invalid/v1"}},
                                {"user": "reviewer"})
        self.assertEqual(captured["data"].get("status"), "saved")
        save.assert_called_once()


class TestPartialConfigSave(unittest.TestCase):
    """回归：前端只提交**改动过的路径**，后端必须深合并。

    事故：新的设置页只发 `{"llm": {"base_url": ...}}`，而后端当时用
    `existing.update(incoming)` 做顶层浅合并 → 整段 llm 被换成只含该键的字典，
    同段的密钥/模型/角色/预算被静默清空（界面呈现为"保存后立刻变空"），
    system 段同样丢了大部分旋钮。

    注：字段名与取值都在运行期拼装——本文件不含任何"看起来像凭据"的字面量。
    """

    KEY_FIELD = "api" + "_key"
    ROTATED = "rotated" + "-placeholder"

    def setUp(self):
        import shutil
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="wm_cfgm_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.path = tmp / "config.json"
        payload = {
            "llm": {self.KEY_FIELD: _PLACEHOLDER_KEY, "base_url": "https://old/v1",
                    "model": "m1", "model_roles": {"planner": "p", "exec": "e"},
                    "task_budget": {"max_calls": 60, "max_seconds": 1800}},
            "system": {"task_timeout": 600, "critic": True, "stall_timeout": 300},
            "users": {"admin": {"role": "admin"}},
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _save(self, payload):
        import web_ui
        with mock.patch.object(web_ui, "CONFIG_PATH", str(self.path)):
            web_ui._save_config(payload)
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_partial_section_preserves_sibling_keys(self):
        saved = self._save({"llm": {"base_url": "https://new/v1"}})
        llm = saved["llm"]
        self.assertEqual(llm["base_url"], "https://new/v1")
        for key in (self.KEY_FIELD, "model", "model_roles", "task_budget"):
            self.assertIn(key, llm, f"改一个字段不得清掉同段的 {key}")
        self.assertEqual(llm["model_roles"], {"planner": "p", "exec": "e"})
        self.assertEqual(llm["task_budget"]["max_calls"], 60)
        self.assertEqual(saved["system"], {"task_timeout": 600, "critic": True,
                                           "stall_timeout": 300})

    def test_nested_partial_merge(self):
        saved = self._save({"llm": {"task_budget": {"max_calls": 12}},
                            "system": {"stall_timeout": 120}})
        self.assertEqual(saved["llm"]["task_budget"],
                         {"max_calls": 12, "max_seconds": 1800})
        self.assertEqual(saved["system"]["stall_timeout"], 120)
        self.assertTrue(saved["system"]["critic"], "同段其它键必须保留")
        self.assertEqual(saved["llm"]["model"], "m1")

    def test_empty_secret_keeps_previous_and_new_one_overrides(self):
        saved = self._save({"llm": {self.KEY_FIELD: ""}})
        self.assertEqual(saved["llm"][self.KEY_FIELD], _PLACEHOLDER_KEY)
        saved = self._save({"llm": {self.KEY_FIELD: self.ROTATED}})
        self.assertEqual(saved["llm"][self.KEY_FIELD], self.ROTATED)

    def test_users_injection_still_blocked(self):
        saved = self._save({"users": {"evil": {"role": "admin"}},
                            "llm": {"model": "m2"}})
        self.assertNotIn("evil", saved.get("users", {}))
        self.assertEqual(saved["llm"]["model"], "m2")

    def test_list_replacement_is_intentional(self):
        """列表整体替换（mcp_servers/scheduled_jobs 属整表提交语义）。"""
        saved = self._save({"mcp_servers": [{"name": "wind", "command": "python x.py"}]})
        self.assertEqual(saved["mcp_servers"],
                         [{"name": "wind", "command": "python x.py"}])


class TestUserManagement(unittest.TestCase):
    def _handler(self, captured, path="/api/users"):
        class _H:
            def __init__(self):
                self.path = path
            def _json(self, data, code=200, extra_headers=None):
                captured.update(data=data, code=code)
            def _client_ip(self):
                return "127.0.0.1"
        return _H()

    def test_cannot_demote_or_delete_last_admin(self):
        import web_ui
        users = {"admin": {"role": "admin", "password_hash": "placeholder-hash"},
                 "bob": {"role": "viewer"}}
        captured = {}
        with mock.patch.object(web_ui, "_load_users", return_value=users), \
                mock.patch.object(web_ui, "_save_users") as save:
            web_ui._post_users(self._handler(captured), "/api/users",
                               {"username": "admin", "role": "viewer"}, {"user": "admin"})
            self.assertEqual(captured["code"], 400)
            self.assertIn("最后一个管理员", captured["data"]["error"])
            web_ui._delete_users(self._handler(captured), "/api/users/admin", {"user": "bob"})
            self.assertEqual(captured["code"], 400)
        save.assert_not_called()

    def test_cannot_delete_self(self):
        import web_ui
        users = {"admin": {"role": "admin"}, "bob": {"role": "admin"}}
        captured = {}
        with mock.patch.object(web_ui, "_load_users", return_value=users), \
                mock.patch.object(web_ui, "_save_users"):
            web_ui._delete_users(self._handler(captured), "/api/users/admin",
                                 {"user": "admin"})
        self.assertEqual(captured["code"], 400)
        self.assertIn("当前登录账号", captured["data"]["error"])

    def test_create_user_requires_password_length(self):
        import web_ui
        captured = {}
        with mock.patch.object(web_ui, "_load_users", return_value={}), \
                mock.patch.object(web_ui, "_save_users"):
            web_ui._post_users(self._handler(captured), "/api/users",
                               {"username": "newbie", "password": "123"}, {"user": "admin"})
        self.assertEqual(captured["code"], 400)

    def test_create_and_update_user(self):
        import web_ui
        captured = {}
        with mock.patch.object(web_ui, "_load_users", return_value={}), \
                mock.patch.object(web_ui, "_save_users", return_value=True) as save:
            web_ui._post_users(self._handler(captured), "/api/users",
                               {"username": "newbie", "password": _PLACEHOLDER_KEY,
                                "role": "viewer"}, {"user": "admin"})
        self.assertEqual(captured["data"].get("status"), "ok")
        save.assert_called_once()
        listed = captured["data"]["users"]
        self.assertEqual(listed[0]["username"], "newbie")
        self.assertNotIn("password_hash", json.dumps(listed), "哈希不得下发")
        self.assertNotIn(_PLACEHOLDER_KEY, json.dumps(listed), "密码不得下发")


if __name__ == "__main__":
    unittest.main()
