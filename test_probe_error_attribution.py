"""端点探测失败的**归因**：一句话必须指向真正的成因，而不是笼统的"密钥无效/不可达"。

实测动机（2026-10-22 的新人实例）：新检出没有 config.json，配置页回退到随包模板
（当时 llm.base_url 预填的是 SiliconFlow），用户把别家的 key 粘进这个预填表单，
端点回 401，界面显示「密钥无效或无权限」——于是先去换 key（key 本身有效，错在
端点与密钥不同源）；随后把端点改对、模型名却仍是别家的（或把 bge 嵌入模型发给
只有对话能力的中转网关），失败落进 `unreachable` 枚举，界面又说「不可达」，于是
去查网络与代理。两轮都被指到错误方向。

本文件锁三件事：
1. 一句话里必须带**端点域名**与**状态码含义**，并覆盖三类互斥成因
   （端点不认这把密钥 / 接入点不认这个模型名 / 真的连不上）；
2. `reason` 机器枚举**不许变**（路由与降级判定只认它，改枚举会动行为）；
3. `config.example.json` 不再预填别家端点与别家模型名（默认值即陷阱）。
"""
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

import llm_client as lc

ROOT = Path(__file__).resolve().parent


def _msg(exc, base_url="https://api.siliconflow.cn/v1", model="tierflow_pro"):
    return lc._probe_failure_message(exc, base_url, model)


class TestFailureMessageSaysWhatActuallyHappened(unittest.TestCase):
    """三类成因互斥，文案不得互相串味。"""

    def test_wrong_vendor_key_names_the_endpoint_not_the_key(self):
        """401 = 这个接入点不认这把密钥（端点与密钥不同源），不是"密钥无效"。"""
        msg = _msg(RuntimeError("HTTP 401: invalid api_key"))
        self.assertIn("api.siliconflow.cn", msg, "必须点明是哪个端点在拒绝")
        self.assertIn("同一家服务商", msg, "必须给出「端点与密钥须同源」这条判断")
        self.assertIn("401", msg)

    def test_forbidden_says_permission_scope(self):
        msg = _msg(RuntimeError("HTTP 403: forbidden"))
        self.assertIn("403", msg)
        self.assertIn("权限", msg)

    def test_model_not_supported_names_the_model(self):
        """400/422 = 接入点不认这个模型名。这一类此前被说成"不可达"，人就去查代理了。"""
        msg = _msg(RuntimeError("HTTP 400: model not found"))
        self.assertIn("模型名", msg)
        self.assertIn("tierflow_pro", msg, "要把当前模型名写出来，否则用户不知道改哪个")
        self.assertNotIn("连不上", msg, "模型名错不是网络问题，不得混说")

    def test_bad_path_hints_v1_suffix(self):
        """404 = 路径不对（最常见是 base_url 少了/多了 /v1）。"""
        msg = _msg(RuntimeError("HTTP 404: not found"))
        self.assertIn("/v1", msg)

    def test_billing_says_quota(self):
        for raw in ("HTTP 402: payment required",
                    "HTTP 403: Insufficient balance",
                    "HTTP 401: credits exceeded"):
            msg = _msg(RuntimeError(raw))
            self.assertTrue("余额" in msg or "额度" in msg, raw)

    def test_rate_limit_says_throttled(self):
        self.assertIn("限流", _msg(RuntimeError("HTTP 429: too many requests")))

    def test_transport_failure_stays_a_connection_problem(self):
        """真的连不上时才说连不上——这条不能被修成"所有失败都像配错"。"""
        for exc in (TimeoutError("timed out"),
                    OSError("Could not resolve host: api.x.com")):
            msg = _msg(exc, base_url="https://api.x.com/v1")
            self.assertIn("连不上", msg)
            self.assertIn("api.x.com", msg)
            self.assertIn("网络", msg)

    def test_unknown_host_degrades_honestly(self):
        """解析不出域名时不许崩，也不许编一个域名。"""
        msg = _msg(RuntimeError("HTTP 500: boom"), base_url="")
        self.assertIn("端点", msg)
        self.assertIn("500", msg)


class TestLogLinesDoNotEchoTheKey(unittest.TestCase):
    """401/402/403 的**日志行**同样会打在新生屏幕上（首启引导），必须脱敏。

    实测截图：`LLM auth/balance error 401 endpoint=primary body={"error":{"message":
    "Authentication Fails. Your api key: ****zthD is invalid ...`——DeepSeek 自己把 key
    打了码，但多数中转站会把整串回显，这一行会原样进控制台与会话日志。
    """

    def test_auth_error_log_scrubs_the_body(self):
        body = ('{"error":{"message":"bad key sk-live-9f8e7d6c5b4a3210",'
                '"type":"authentication_error"}}')
        with self.assertLogs("llm_client", level="WARNING") as cm:
            lc._mark_auth_error(401, body, "primary")
        out = "\n".join(cm.output)
        self.assertNotIn("sk-live-9f8e7d6c5b4a3210", out)
        self.assertIn("authentication_error", out, "诊断信息要保留")

    def test_auth_error_message_for_users_carries_no_body(self):
        lc._mark_auth_error(401, '{"message":"your key sk-live-9f8e7d6c5b4a3210"}', "primary")
        msg = str(lc._last_auth_error.get("message") or "")
        self.assertNotIn("sk-live", msg)
        self.assertIn("401", msg)


class TestFirstRunIsNotAnError(unittest.TestCase):
    """全新克隆没有 config.json 是**正常态**：引导会先生成它。

    实测：引导探测时先 import llm_client，模块级加载打两条 WARNING
    （`LLM 配置加载失败: [Errno 2] No such file or directory: ...\\config.json`），
    新人在等待探测结果时先看到满屏红字，把它当成"又出错了"。
    """

    def _load(self, fn):
        with mock.patch("builtins.open", side_effect=FileNotFoundError(
                2, "No such file or directory", "config.json")):
            with self.assertLogs("llm_client", level="DEBUG") as cm:
                return fn(), cm.output

    def test_missing_config_is_not_a_warning(self):
        got, out = self._load(lc._load_llm_config)
        self.assertEqual(got, {}, "缺失时按空配置降级")
        warnings = [l for l in out if l.startswith("WARNING")]
        self.assertEqual(warnings, [], f"缺失 config.json 不该报 WARNING：{warnings}")

    def test_missing_backup_config_is_silent(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError(
                2, "No such file or directory", "config.json")):
            with self.assertNoLogs("llm_client", level="WARNING"):
                got = lc._load_backup_config()
        self.assertEqual(got, {}, "没有 config.json 时「无备援」是正常态，不该告警")

    def test_corrupt_config_still_warns(self):
        """真正的读失败（编码错/JSON 坏）仍要留痕，别把"没配置"和"配置坏了"混为一谈。"""
        with mock.patch("builtins.open", side_effect=UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "invalid start byte")):
            with self.assertLogs("llm_client", level="WARNING") as cm:
                lc._load_llm_config()
        self.assertTrue(any("配置加载失败" in line for line in cm.output))


class TestSetupWizardCarriesTheSentence(unittest.TestCase):
    """首启引导是新人唯一会读的那块屏幕：它不能只翻枚举。"""

    def _probe(self, res):
        import setup_wizard
        with mock.patch.object(lc, "_probe_endpoint_status", return_value=res):
            return setup_wizard.probe_endpoint(
                "https://api.deepseek.com/v1", "sk-test", "deepseek-chat")

    def test_wizard_shows_host_and_the_way_out(self):
        ok, reason = self._probe({"ok": False, "reason": "unauthorized",
                                  "message": "api.deepseek.com 返回 401：该接入点不认这把密钥"
                                             "——密钥与接入点必须是同一家服务商"})
        self.assertFalse(ok)
        self.assertIn("api.deepseek.com", reason)
        self.assertIn("自定义 base_url", reason)


class TestMessageNeverLeaksSecrets(unittest.TestCase):
    """这句话会进 /api/status、设置页与会话日志，回显的凭据必须先脱敏。"""

    def test_key_in_upstream_body_is_scrubbed(self):
        msg = _msg(RuntimeError(
            "HTTP 500: upstream said Authorization: Bearer sk-live-abcdef123456 bad"))
        self.assertNotIn("sk-live-abcdef123456", msg)
        self.assertNotIn("abcdef123456", msg)

    def test_scrub_keeps_the_diagnostic_part(self):
        msg = _msg(RuntimeError("HTTP 500: sk-live-abcdef123456 db timeout"))
        self.assertIn("db timeout", msg, "脱敏只抹凭据，不能把有效信息一起抹掉")


class TestReasonEnumIsNotChangedByThisFix(unittest.TestCase):
    """枚举是机器契约：路由/降级/终态冷却都只认它，本次修的是"怎么解释"。"""

    def test_enum_mapping_is_stable(self):
        cases = {
            "HTTP 401: invalid": "unauthorized",
            "HTTP 403: forbidden": "unauthorized",
            "HTTP 402: payment": "insufficient_balance",
            "HTTP 403: Insufficient balance": "insufficient_balance",
            "HTTP 400: model not found": "unreachable",
            "HTTP 404: nope": "unreachable",
            "HTTP 429: slow down": "unreachable",
            "timed out": "unreachable",
        }
        for raw, expected in cases.items():
            self.assertEqual(lc._classify_probe_error(RuntimeError(raw)), expected, raw)


class TestProbeResultCarriesTheSentence(unittest.TestCase):
    """`_probe_endpoint_status` 的返回要带 message，且 ok 时为空字符串。"""

    def _probe(self, exc):
        with mock.patch.object(lc.LLMClient, "_send_request", side_effect=exc):
            return lc._probe_endpoint_status(
                "https://tierflow.cn/v1", "sk-test", "tierflow_pro")

    def test_failure_carries_reason_and_message(self):
        out = self._probe(RuntimeError("HTTP 400: model not found"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "unreachable")
        self.assertIn("模型名", out["message"])
        self.assertIn("tierflow.cn", out["message"])

    def test_success_has_empty_message(self):
        with mock.patch.object(lc.LLMClient, "_send_request", return_value={}):
            out = lc._probe_endpoint_status("https://tierflow.cn/v1", "sk-test", "tierflow")
        self.assertTrue(out["ok"])
        self.assertEqual(out["message"], "")

    def test_key_never_reaches_the_message(self):
        out = self._probe(RuntimeError("HTTP 500: bad key sk-secret-abc123456"))
        self.assertNotIn("sk-secret-abc123456", out["message"])


class TestSentenceSurvivesTheStatusPath(unittest.TestCase):
    """message 要能一路走到 /api/status 与设置页（缓存、终态冷却都不能丢字段）。"""

    def setUp(self):
        lc._clear_balance_cache()

    def tearDown(self):
        lc._clear_balance_cache()

    def test_balance_status_propagates_message(self):
        fake = {"primary": {"ok": False, "reason": "unauthorized",
                            "message": "api.siliconflow.cn 返回 401：端点不认这把密钥"}}
        env = {"LLM_BASE_URL": "https://api.siliconflow.cn/v1",
               "LLM_API_KEY": "sk-test", "LLM_MODEL": "deepseek-ai/DeepSeek-V3"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(lc, "_probe_endpoint_status",
                                  return_value=fake["primary"]):
            out = lc.get_balance_status(use_cache=False)
        self.assertEqual(out["primary"]["reason"], "unauthorized")
        self.assertIn("api.siliconflow.cn", out["primary"]["message"],
                      "设置页只能看到 reason 就会退回「密钥无效或无权限」")

    def test_terminal_freeze_keeps_the_message(self):
        """终态冷却期复用上次结论时也不能只剩枚举（否则冷却期内文案又退化了）。"""
        now = time.time()
        lc._balance_cache["data"] = {
            "primary": {"ok": False, "reason": "unauthorized",
                        "message": "api.x.com 返回 401：端点不认这把密钥"},
        }
        lc._balance_cache["ep_ts"] = {"primary": now}
        frozen = lc._frozen_terminal_endpoints(now)
        self.assertIn("primary", frozen)
        self.assertIn("api.x.com", frozen["primary"]["message"])
        self.assertTrue(frozen["primary"].get("frozen"))


class TestHealthProbeDetailCarriesTheSentence(unittest.TestCase):
    """「测试连通性」面板的 detail 必须放这句话（此前只有 base/model 回显）。"""

    def test_llm_probe_detail(self):
        import health_registry as hr
        env = {"LLM_BASE_URL": "https://tierflow.cn/v1",
               "LLM_API_KEY": "sk-test", "LLM_MODEL": "tierflow"}
        fake = {"ok": False, "reason": "unreachable",
                "message": "tierflow.cn 返回 400：请求被拒，模型名（当前模型名：tierflow）"
                           "可能不被该接入点支持"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(lc, "_probe_endpoint_status", return_value=fake):
            out = hr._live_probe_llm("llm")
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "unreachable")
        self.assertIn("模型名", out["detail"])
        self.assertIn("tierflow.cn", out["detail"])
        self.assertIn("base=", out["detail"], "配置回显要保留，便于核对填的是哪个端点")

    def test_embedding_probe_detail(self):
        """嵌入探测同样要能说出"接入点不认这个模型名"（bge 发给只有对话能力的网关）。"""
        import health_registry as hr
        env = {"EMBEDDING_BASE_URL": "https://tierflow.cn/v1",
               "EMBEDDING_API_KEY": "sk-test",
               "EMBEDDING_MODEL": "BAAI/bge-large-zh-v1.5"}
        fake = {"ok": False, "reason": "unreachable",
                "message": "tierflow.cn 返回 404：模型名（当前模型名：BAAI/bge-large-zh-v1.5）"
                           "可能不被该接入点支持"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(lc, "_probe_endpoint_status", return_value=fake):
            out = hr._live_probe_embedding()
        self.assertFalse(out["ok"])
        self.assertIn("模型名", out["detail"])


class TestTemplateDoesNotPrefillAnotherVendor(unittest.TestCase):
    """默认值即陷阱：模板里不许再预填别家的端点/模型名。

    config.json 不入库（.gitignore），新检出只能回退这个模板——模板预填别家端点，
    使用者粘自家 key 就必然 401，而界面又把 401 说成"密钥无效"。留空才会出现
    首启引导，让使用者显式选服务商（setup_wizard 有 SiliconFlow 预设与自定义 base_url）。
    """

    def setUp(self):
        with open(ROOT / "config.example.json", encoding="utf-8") as fh:
            self.cfg = json.load(fh)

    def test_llm_endpoint_and_model_are_blank(self):
        llm = self.cfg["llm"]
        self.assertEqual(llm["base_url"], "", "模板不得预填别家端点（默认值即陷阱）")
        self.assertEqual(llm["model"], "", "模板不得预填别家模型名")
        self.assertTrue(str(llm.get("api_key", "")).strip(),
                        "api_key 仍需占位符：占位符才会被判为「未配置」并触发首启引导")

    def test_embedding_is_blank(self):
        emb = self.cfg["embedding"]
        for field in ("base_url", "model", "api_key"):
            self.assertEqual(emb[field], "", f"embedding.{field} 不得预填（中转网关多半无嵌入能力）")

    def test_backup_has_no_orphan_model_name(self):
        """备用端点 model 不许留一个没有 base_url 的模型名（failover 时照样 404）。"""
        backup = self.cfg["backup"]
        self.assertEqual(backup["base_url"], "")
        self.assertEqual(backup["model"], "", "上一版模板残留了 tierflow_pro 却没有 base_url")

    def test_no_vendor_host_left_in_endpoint_sections(self):
        blob = json.dumps({k: self.cfg[k] for k in ("llm", "embedding", "planner", "backup")},
                          ensure_ascii=False)
        for host in ("siliconflow.cn", "tierflow.cn", "deepseek.com", "moonshot.cn",
                     "bigmodel.cn", "openai.com"):
            self.assertNotIn(host, blob, f"模板端点段里不该再出现 {host}")

    def test_template_explains_why_it_is_blank(self):
        """留空的理由要写在模板里，否则很快又被"好心"填回去。"""
        comment = str(self.cfg["llm"].get("_comment") or "")
        self.assertIn("留空", comment)
        self.assertIn("同一家服务商", comment)


if __name__ == "__main__":
    unittest.main(verbosity=2)
