# -*- coding: utf-8 -*-
"""lora_serve 多实例管理器回归测试。

覆盖：
- 配置加载：多 server 配置正确解析（base + servers 映射）；
- adapter 池映射：端口 → adapter name / server name / system prompt；
- 单实例兼容：无配置文件时退回默认端口 8765；
- lora_client：server_url(name) 按配置查端口、set_server 切换；
- 未知 server 名回退默认 URL。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

import lora_client as lc
import lora_serve as ls


def _write_cfg(tmp, servers, base="models/base"):
    p = os.path.join(tmp, "lora_servers.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"base_model": base, "servers": servers}, f, ensure_ascii=False)
    return p


class TestLoraMultiInstanceConfig(unittest.TestCase):
    def test_load_config_two_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_cfg(tmp, {
                "content_summary": {"port": 8765, "lora_path": "models/a", "enabled": True},
                "ranking": {"port": 8766, "lora_path": "models/b", "enabled": True},
            })
            old = ls.CONFIG_FILE
            ls.CONFIG_FILE = p
            try:
                cfg = ls._load_config()
                servers = ls._config_servers(cfg)
                self.assertEqual(len(servers), 2)
                ports = {s["port"] for s in servers}
                self.assertEqual(ports, {8765, 8766})
                names = {s["name"] for s in servers}
                self.assertEqual(names, {"content_summary", "ranking"})
            finally:
                ls.CONFIG_FILE = old

    def test_disabled_server_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_cfg(tmp, {
                "a": {"port": 8765, "lora_path": "models/a", "enabled": True},
                "b": {"port": 8766, "lora_path": "models/b", "enabled": False},
            })
            old = ls.CONFIG_FILE
            ls.CONFIG_FILE = p
            try:
                servers = ls._config_servers(ls._load_config())
                self.assertEqual(len(servers), 1)
                self.assertEqual(servers[0]["name"], "a")
            finally:
                ls.CONFIG_FILE = old

    def test_single_instance_fallback(self):
        """无配置文件 → 退回单实例（默认端口 8765 + 环境变量 LoRA）。"""
        old_cfg, old_lora, old_port = ls.CONFIG_FILE, os.environ.get("WM_LOCAL_LORA"), os.environ.get("WM_LOCAL_PORT")
        try:
            ls.CONFIG_FILE = os.path.join(tempfile.gettempdir(), "no_such_lora_cfg.json")
            os.environ.pop("WM_LOCAL_LORA", None)
            os.environ.pop("WM_LOCAL_PORT", None)
            servers = ls._config_servers({})
            self.assertEqual(len(servers), 1)
            self.assertEqual(servers[0]["port"], 8765)
            self.assertEqual(servers[0]["name"], "default")
        finally:
            ls.CONFIG_FILE = old_cfg
            if old_lora:
                os.environ["WM_LOCAL_LORA"] = old_lora
            if old_port:
                os.environ["WM_LOCAL_PORT"] = old_port


class TestLoraClientMultiServer(unittest.TestCase):
    def test_server_url_from_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_cfg(tmp, {
                "content_summary": {"port": 8765, "enabled": True},
                "ranking": {"port": 8766, "enabled": True},
            })
            old = lc.CONFIG_FILE
            lc.CONFIG_FILE = p
            try:
                self.assertEqual(lc.server_url("content_summary"), "http://127.0.0.1:8765")
                self.assertEqual(lc.server_url("ranking"), "http://127.0.0.1:8766")
                # 未知名回退默认
                self.assertEqual(lc.server_url("unknown"), "http://127.0.0.1:8765")
            finally:
                lc.CONFIG_FILE = old

    def test_set_server_switches_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = _write_cfg(tmp, {
                "a": {"port": 8801, "enabled": True},
                "b": {"port": 8802, "enabled": True},
            })
            old_cfg, old_url = lc.CONFIG_FILE, lc._cache["url"]
            lc.CONFIG_FILE = p
            try:
                lc.set_server("a")
                self.assertEqual(lc._cache["url"], "http://127.0.0.1:8801")
                lc.set_server("b")
                self.assertEqual(lc._cache["url"], "http://127.0.0.1:8802")
            finally:
                lc.CONFIG_FILE = old_cfg
                lc._cache["url"] = old_url

    def test_disabled_flag(self):
        """WM_USE_LOCAL_LORA=0 → 探活返回 False（回退云端）。"""
        old_env = os.environ.get("WM_USE_LOCAL_LORA")
        old_enabled = lc._ENABLED
        try:
            os.environ["WM_USE_LOCAL_LORA"] = "0"
            import importlib
            importlib.reload(lc)
            self.assertFalse(lc._service_alive())
        finally:
            # 恢复环境变量 + 模块状态（reload 后 _ENABLED 需显式恢复，避免污染后续测试）
            if old_env is None:
                os.environ.pop("WM_USE_LOCAL_LORA", None)
            else:
                os.environ["WM_USE_LOCAL_LORA"] = old_env
            import importlib
            importlib.reload(lc)
            lc._ENABLED = old_enabled


if __name__ == "__main__":
    unittest.main(verbosity=2)
