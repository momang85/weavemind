# -*- coding: utf-8 -*-
"""F5 任务完成通知测试：webhook / Server酱 / email 分发与失败隔离。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import notifications


def _cfg(**overrides):
    """构造全渠道配置；overrides 覆盖特定渠道。"""
    cfg = json.loads(json.dumps(notifications.DEFAULT_NOTIFICATIONS))
    for key, value in overrides.items():
        if key in cfg:
            cfg[key].update(value)
    return cfg


class TestWebhookNotification(unittest.TestCase):
    def test_webhook_payload_and_timeout(self):
        """webhook 载荷包含 task_id/goal/status/link/summary，超时 30s。"""
        cfg = _cfg(webhook={"enabled": True, "url": "https://hook.example/x"})
        captured = {}

        def fake_post(url, json=None, timeout=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return mock.Mock(raise_for_status=lambda: None)

        with mock.patch("requests.post", side_effect=fake_post):
            out = notifications.notify_task_done(
                "t-notify-1", "比特币最新价格", "SUCCESS",
                "/share/tok123", "比特币现价 67450 美元。", cfg=cfg,
            )
        self.assertEqual(out["sent"], ["webhook"])
        self.assertEqual(out["failed"], [])
        self.assertEqual(captured["url"], "https://hook.example/x")
        self.assertEqual(captured["timeout"], 30)
        self.assertEqual(captured["json"]["task_id"], "t-notify-1")
        self.assertEqual(captured["json"]["goal"], "比特币最新价格")
        self.assertEqual(captured["json"]["status"], "SUCCESS")
        self.assertEqual(captured["json"]["link"], "/share/tok123")
        self.assertEqual(captured["json"]["summary"], "比特币现价 67450 美元。")

    def test_webhook_failure_does_not_raise(self):
        """webhook 发送失败仅记录 failed，不向调用方抛异常。"""
        cfg = _cfg(webhook={"enabled": True, "url": "https://hook.example/x"})
        with mock.patch("requests.post", side_effect=RuntimeError("boom")):
            out = notifications.notify_task_done(
                "t-fail", "目标", "FAILED", "", "摘要", cfg=cfg,
            )
        self.assertEqual(out["failed"], ["webhook"])
        self.assertEqual(out["sent"], [])


class TestServerChanNotification(unittest.TestCase):
    def test_serverchan_url_title_desp(self):
        """Server酱：URL 含 sendkey，title=任务完成，desp 含摘要与链接。"""
        cfg = _cfg(serverchan={"enabled": True, "sendkey": "KEY123"})
        captured = {}

        def fake_post(url, data=None, timeout=None, **kwargs):
            captured["url"] = url
            captured["data"] = data
            captured["timeout"] = timeout
            return mock.Mock(raise_for_status=lambda: None)

        with mock.patch("requests.post", side_effect=fake_post):
            out = notifications.notify_task_done(
                "t-sc", "美国 CPI 宏观分析", "SUCCESS_WITH_ISSUES",
                "/share/abc", "CPI 同比 3.1%。", cfg=cfg,
            )
        self.assertEqual(out["sent"], ["serverchan"])
        self.assertEqual(
            captured["url"],
            "https://sctapi.ftqq.com/KEY123.send",
        )
        self.assertEqual(captured["timeout"], 30)
        self.assertIn("任务完成", captured["data"]["title"])
        self.assertIn("美国 CPI 宏观分析", captured["data"]["title"])
        self.assertIn("CPI 同比 3.1%。", captured["data"]["desp"])
        self.assertIn("/share/abc", captured["data"]["desp"])

    def test_serverchan_failure_does_not_raise(self):
        cfg = _cfg(serverchan={"enabled": True, "sendkey": "KEY"})
        with mock.patch("requests.post", side_effect=OSError("net down")):
            out = notifications.notify_task_done("t-sc2", "g", "FAILED", "", "s", cfg=cfg)
        self.assertEqual(out["failed"], ["serverchan"])


class TestEmailNotification(unittest.TestCase):
    def test_smtp_call_and_body(self):
        """email：SMTP_SSL 调用，正文含摘要与链接，收件人列表正确。"""
        import base64
        cfg = _cfg(email={
            "enabled": True,
            "host": "smtp.example.com",
            "port": 465,
            "user": "u@example.com",
            "password": "pwd",
            "from": "weavemind@example.com",
            "to": ["a@example.com", "b@example.com"],
        })
        smtp_inst = mock.Mock()
        smtp_inst.sendmail = mock.Mock()
        smtp_inst.quit = mock.Mock()
        captured = {}

        def fake_ssl(host, port, timeout=None):
            captured["host"] = host
            captured["port"] = port
            captured["timeout"] = timeout
            return smtp_inst

        with mock.patch("smtplib.SMTP_SSL", side_effect=fake_ssl):
            out = notifications.notify_task_done(
                "t-email", "每日宏观简报", "SUCCESS",
                "/share/xyz", "GDP 增速 2.1%。", cfg=cfg,
            )
        self.assertEqual(out["sent"], ["email"])
        self.assertEqual(captured["host"], "smtp.example.com")
        self.assertEqual(captured["port"], 465)
        self.assertEqual(captured["timeout"], 30)
        smtp_inst.login.assert_called_once_with("u@example.com", "pwd")
        smtp_inst.sendmail.assert_called_once()
        args = smtp_inst.sendmail.call_args
        self.assertEqual(args.args[0], "weavemind@example.com")
        self.assertEqual(args.args[1], ["a@example.com", "b@example.com"])
        msg_text = args.args[2]
        # MIMEText 默认对 UTF-8 正文做 base64 编码，先解码再断言
        raw_body = msg_text.split("\n\n", 1)[-1]
        body = base64.b64decode(raw_body).decode("utf-8")
        self.assertIn("GDP 增速 2.1%。", body)
        self.assertIn("/share/xyz", body)
        smtp_inst.quit.assert_called_once()

    def test_email_failure_does_not_raise(self):
        cfg = _cfg(email={
            "enabled": True,
            "host": "smtp.example.com",
            "port": 465,
            "user": "u",
            "password": "p",
            "from": "f@example.com",
            "to": ["t@example.com"],
        })
        with mock.patch(
            "smtplib.SMTP_SSL",
            side_effect=OSError("smtp refused"),
        ):
            out = notifications.notify_task_done("t-e2", "g", "FAILED", "", "s", cfg=cfg)
        self.assertEqual(out["failed"], ["email"])


class TestConfigAndHelpers(unittest.TestCase):
    def test_public_config_strips_secrets(self):
        """GET 回显配置必须剥离 email.password 与 serverchan.sendkey。"""
        cfg = _cfg(
            serverchan={"enabled": True, "sendkey": "TOP-SECRET"},
            email={
                "enabled": True,
                "host": "smtp.x",
                "port": 465,
                "user": "u",
                "password": "PWD",
                "from": "f",
                "to": ["t"],
            },
        )
        public = notifications.public_notifications_config(cfg)
        self.assertEqual(public["serverchan"]["sendkey"], "")
        self.assertEqual(public["email"]["password"], "")

    def test_save_and_load_roundtrip(self):
        """保存到临时 config.json 后可读回，未启用渠道保持默认。"""
        tmp = Path(tempfile.mkdtemp(prefix="notify_cfg_"))
        path = tmp / "config.json"
        path.write_text(json.dumps({"llm": {"model": "x"}}), encoding="utf-8")
        cfg = _cfg(
            webhook={"enabled": True, "url": "https://h/x"},
            serverchan={"enabled": True, "sendkey": "KEEP-ME"},
        )
        self.assertTrue(notifications.save_notifications_config(cfg, str(path)))
        loaded = notifications.load_notifications_config(str(path))
        self.assertTrue(loaded["webhook"]["enabled"])
        self.assertEqual(loaded["webhook"]["url"], "https://h/x")
        self.assertEqual(loaded["serverchan"]["sendkey"], "KEEP-ME")
        self.assertFalse(loaded["email"]["enabled"])
        # GET 回显剥离密钥后再次保存：空 sendkey 不得覆盖已有密钥
        redacted = notifications.public_notifications_config(loaded)
        self.assertEqual(redacted["serverchan"]["sendkey"], "")
        self.assertTrue(
            notifications.save_notifications_config(redacted, str(path)),
        )
        reloaded = notifications.load_notifications_config(str(path))
        self.assertEqual(reloaded["serverchan"]["sendkey"], "KEEP-ME")

    def test_make_summary_strips_markdown(self):
        summary = notifications.make_summary(
            "# 报告\n\n比特币现价 **67450** 美元。\n\n- 24h 涨跌 2.4%"
        )
        self.assertIn("比特币现价 67450 美元", summary)
        self.assertIn("24h 涨跌 2.4%", summary)

    def test_find_share_link_only_existing(self):
        """未分享的任务返回空链接；已分享返回 /share/<token>。"""
        tmp = Path(tempfile.mkdtemp(prefix="notify_share_"))
        share_file = tmp / "share_links.json"
        share_file.write_text(
            json.dumps({"tokA": {"task_id": "t-shared", "created_at": "x",
                                 "expires_at": "2999-01-01T00:00:00+00:00"}}),
            encoding="utf-8",
        )
        self.assertEqual(
            notifications.find_share_link("t-shared", str(share_file)),
            "/share/tokA",
        )
        self.assertEqual(notifications.find_share_link("t-other", str(share_file)), "")
        self.assertEqual(notifications.find_share_link("t-none", str(tmp / "no.json")), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
