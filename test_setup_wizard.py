# -*- coding: utf-8 -*-
"""首次配置引导回归（setup_wizard）。

覆盖新手最容易卡住的几件事：能不能生成可用 config、填错有没有可行动提示、
回环/内网地址会不会被拿去发探测请求、已有配置会不会被乱改、非交互会不会挂住。
全部离线：不访问网络、不碰真实 config.json（一律走临时目录）。
夹具里的 key 都是明显的测试字面量，不是任何真实凭据。
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import setup_wizard as w

KEY_A = "unit-test-key-alpha"
KEY_B = "unit-test-key-bravo"
KEY_C = "unit-test-key-charlie"
KEY_LONG = "unit-test-key-for-mask-check"


def _template() -> dict:
    return {
        "llm": {"api_key": "YOUR_API_KEY", "base_url": "https://api.siliconflow.cn/v1",
                "model": "deepseek-ai/DeepSeek-V3", "model_roles": {"planner": "x"}},
        "embedding": {"api_key": "YOUR_EMBEDDING_API_KEY",
                      "base_url": "https://api.siliconflow.cn/v1",
                      "model": "BAAI/bge-large-zh-v1.5"},
        "planner": {"api_key": "YOUR_PLANNER_API_KEY", "base_url": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V3"},
        "backup": {"api_key": "YOUR_BACKUP_API_KEY", "base_url": "https://api.siliconflow.cn/v1",
                   "model": "deepseek-ai/DeepSeek-V3"},
        "redis": {"host": "localhost", "port": 6379},
        "system": {"task_timeout": 90, "_comment": "keep me"},
    }


class _Tmp(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_wiz_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.tmp = tmp
        self.cfg_path = tmp / "config.json"


class TestValidation(_Tmp):
    def test_base_url_scheme_and_host(self):
        self.assertTrue(w.validate_base_url("https://api.openai.com/v1")[0])
        self.assertTrue(w.validate_base_url("http://127.0.0.1:11434/v1")[0],
                        "本地 LLM 地址应允许保存（探测另按策略跳过）")
        self.assertFalse(w.validate_base_url("ftp://x/v1")[0])
        self.assertFalse(w.validate_base_url("https://")[0])
        self.assertFalse(w.validate_base_url("")[0])

    def test_placeholder_key_rejected(self):
        for bad in ("YOUR_API_KEY", "your_embedding_api_key", "", "  ", "changeme-123"):
            self.assertTrue(w._looks_placeholder(bad), f"{bad!r} 应判为未填")
        self.assertFalse(w._looks_placeholder(KEY_A))

    def test_mask_never_leaks(self):
        m = w.mask(KEY_LONG)
        self.assertNotIn(KEY_LONG, m)
        self.assertIn("…", m)
        self.assertEqual(w.mask(""), "<empty>")
        self.assertEqual(w.mask("short"), "*****")

    def test_probe_policy_blocks_private_and_loopback(self):
        for local in ("http://127.0.0.1:11434/v1", "http://localhost:8000/v1",
                      "http://192.168.1.10:8080/v1", "http://10.0.0.5/v1"):
            allowed, why = w.probe_allowed(local)
            self.assertFalse(allowed, f"{local} 不应被探测")
            self.assertIn("探测", why)
        self.assertTrue(w.probe_allowed("https://api.openai.com/v1")[0])


class TestConfigStatus(_Tmp):
    def test_missing_file(self):
        ok, why = w.config_status(self.cfg_path)
        self.assertFalse(ok)
        self.assertIn("不存在", why)

    def test_incomplete_and_placeholder_rejected(self):
        self.cfg_path.write_text(json.dumps({"llm": {"api_key": "YOUR_API_KEY",
                                                     "base_url": "https://x/v1",
                                                     "model": "m"}}), encoding="utf-8")
        ok, why = w.config_status(self.cfg_path)
        self.assertFalse(ok)
        self.assertIn("api_key", why)

    def test_complete_passes(self):
        self.cfg_path.write_text(json.dumps({"llm": {"api_key": KEY_A,
                                                     "base_url": "https://x/v1",
                                                     "model": "m"}}), encoding="utf-8")
        self.assertTrue(w.config_status(self.cfg_path)[0])

    def test_unparsable_reports_readably(self):
        self.cfg_path.write_text("{ not json", encoding="utf-8")
        ok, why = w.config_status(self.cfg_path)
        self.assertFalse(ok)
        self.assertIn("解析", why)


class TestBuildAndWrite(_Tmp):
    def test_build_fills_llm_and_keeps_other_sections(self):
        cfg = w.build_config(_template(), base_url="https://api.deepseek.com/v1",
                             api_key=KEY_A, model="deepseek-chat",
                             embedding={"base_url": "https://api.deepseek.com/v1",
                                        "api_key": KEY_A, "model": "bge-m3"})
        self.assertEqual(cfg["llm"]["api_key"], KEY_A)
        self.assertEqual(cfg["llm"]["model"], "deepseek-chat")
        self.assertEqual(cfg["llm"]["model_roles"]["planner"], "x", "其它字段必须保留")
        self.assertEqual(cfg["system"]["_comment"], "keep me")
        self.assertEqual(cfg["redis"]["port"], 6379)
        self.assertEqual(cfg["embedding"]["model"], "bge-m3")
        # 计划/备用端点跟随主端点：占位符 key 留着会让规划调用 401
        self.assertEqual(cfg["planner"]["api_key"], KEY_A)
        self.assertEqual(cfg["backup"]["api_key"], KEY_A)

    def test_template_not_mutated(self):
        tpl = _template()
        w.build_config(tpl, base_url="https://x/v1", api_key=KEY_A, model="m")
        self.assertEqual(tpl["llm"]["api_key"], "YOUR_API_KEY", "不得改动调用方对象")

    def test_written_config_passes_start_bat_gate(self):
        cfg = w.build_config(_template(), base_url="https://api.deepseek.com/v1",
                             api_key=KEY_B, model="deepseek-chat")
        p = w.write_config(cfg, self.cfg_path)
        self.assertTrue(p.exists())
        # 复刻 start.bat [2/6] 的检查语义
        llm = json.loads(p.read_text(encoding="utf-8"))["llm"]
        self.assertTrue(llm.get("api_key") and llm.get("base_url") and llm.get("model"))
        self.assertTrue(w.config_status(p)[0])

    def test_write_is_atomic_no_tmp_left(self):
        w.write_config(w.build_config(_template(), base_url="https://x/v1",
                                      api_key=KEY_C, model="m"), self.cfg_path)
        self.assertEqual(list(self.tmp.glob("*.tmp")), [], "临时文件必须被替换掉")


class TestInteractiveFlow(_Tmp):
    """用脚本化 stdin 跑完整问答（离线：探测被 mock）。"""

    def _run(self, answers: str, *, probe=None, probe_side_effect=None, force=False):
        probe_kwargs = ({"side_effect": probe_side_effect} if probe_side_effect is not None
                        else {"return_value": probe or (True, "连通正常")})
        with mock.patch.object(w, "CONFIG_PATH", self.cfg_path), \
                mock.patch.object(w, "_interactive", return_value=True), \
                mock.patch.object(sys.stdin, "isatty", return_value=False, create=True), \
                mock.patch.object(w, "probe_endpoint", **probe_kwargs) as pe, \
                mock.patch.object(w, "load_template", return_value=_template()), \
                mock.patch("sys.stdout", io.StringIO()), \
                mock.patch("builtins.input", side_effect=self._answers(answers)):
            code = w.run_interactive(force=force, path=self.cfg_path)
        return code, pe

    @staticmethod
    def _answers(text: str):
        buf = io.StringIO(text)

        def _next(prompt=""):
            line = buf.readline()
            if line == "":
                raise EOFError
            return line.rstrip("\n")
        return _next

    def test_guided_setup_creates_usable_config(self):
        # 序号 1（SiliconFlow）→ 回车采用预设 URL → key → 回车用推荐模型 → 1（复用 embedding）
        code, pe = self._run(f"1\n\n{KEY_A}\n\n1\n\n")
        self.assertEqual(code, 0)
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["llm"]["api_key"], KEY_A)
        self.assertEqual(cfg["llm"]["base_url"], "https://api.siliconflow.cn/v1")
        self.assertTrue(cfg["llm"]["model"])
        self.assertEqual(cfg["embedding"]["api_key"], KEY_A)
        pe.assert_called_once()

    def test_skip_embedding_keeps_it_unset(self):
        code, _ = self._run(f"1\n\n{KEY_B}\n\n2\n")
        self.assertEqual(code, 0)
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["embedding"]["api_key"], "YOUR_EMBEDDING_API_KEY",
                         "跳过时不应把 embedding 改成主 key")

    def test_existing_complete_config_is_left_alone(self):
        self.cfg_path.write_text(json.dumps({"llm": {"api_key": KEY_A,
                                                     "base_url": "https://x/v1",
                                                     "model": "m"}}), encoding="utf-8")
        before = self.cfg_path.read_text(encoding="utf-8")
        with mock.patch.object(w, "_interactive", return_value=True), \
                mock.patch("builtins.input", side_effect=AssertionError("不该提问")):
            code = w.run_interactive(path=self.cfg_path)
        self.assertEqual(code, 0)
        self.assertEqual(self.cfg_path.read_text(encoding="utf-8"), before)

    def test_force_overwrites(self):
        self.cfg_path.write_text(json.dumps({"llm": {"api_key": KEY_A,
                                                     "base_url": "https://x/v1",
                                                     "model": "m"}}), encoding="utf-8")
        code, _ = self._run(f"1\n\n{KEY_C}\n\n2\n", force=True)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.cfg_path.read_text(encoding="utf-8"))["llm"]["api_key"],
                         KEY_C)

    def test_private_endpoint_is_not_probed_but_still_saved(self):
        code, pe = self._run(f"4\nhttp://127.0.0.1:11434/v1\n{KEY_A}\nqwen2.5\n2\n")
        self.assertEqual(code, 0)
        pe.assert_not_called()
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["llm"]["base_url"], "http://127.0.0.1:11434/v1")

    def test_bad_placeholder_key_is_asked_again(self):
        code, _ = self._run(f"1\n\nYOUR_API_KEY\n{KEY_B}\n\n2\n")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.cfg_path.read_text(encoding="utf-8"))["llm"]["api_key"],
                         KEY_B)

    def test_invalid_url_is_asked_again(self):
        code, _ = self._run(f"4\nftp://bad/v1\nhttps://api.deepseek.com/v1\n{KEY_C}\n\n2\n")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.cfg_path.read_text(encoding="utf-8"))["llm"]["base_url"],
                         "https://api.deepseek.com/v1")

    def test_probe_failure_defaults_to_reenter(self):
        """探测失败时默认"重新填写"：回车不应把坏配置带进启动流程。

        实测动机：把 tokenrhythm 的 key 配到 siliconflow 地址上会被探测判为鉴权失败，
        若默认保存，新手就会带着一份必然 401 的配置去启动。
        """
        answers = ("1\n\n" + KEY_A + "\n\n2\n"    # 第 1 轮：SiliconFlow 预设
                   "\n"                            # 探测失败 → 回车 = 重新填写
                   "3\n\n" + KEY_B + "\n\n2\n")    # 第 2 轮：换 OpenAI 预设与新 key
        code, pe = self._run(answers, probe_side_effect=[
            (False, "鉴权失败（key 可能无效或已过期）"),
            (True, "连通正常"),
        ])
        self.assertEqual(code, 0)
        cfg = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["llm"]["base_url"], "https://api.openai.com/v1",
                         "回车后应采用第二轮填写的地址")
        self.assertEqual(cfg["llm"]["api_key"], KEY_B)
        self.assertEqual(pe.call_count, 2, "应探测两次（两轮各一次）")

    def test_probe_failure_can_still_save_on_explicit_n(self):
        code, pe = self._run("1\n\n" + KEY_C + "\n\n2\nn\n",
                             probe=(False, "鉴权失败"))
        self.assertEqual(code, 0, "显式输入 n 应允许保存")
        self.assertTrue(self.cfg_path.exists())
        self.assertEqual(pe.call_count, 1)


class TestNonInteractive(_Tmp):
    def test_noninteractive_prints_guidance_and_exits_nonzero(self):
        out = io.StringIO()
        with mock.patch.object(w, "_interactive", return_value=False), \
                mock.patch.object(w, "config_status", return_value=(False, "config.json 不存在")), \
                mock.patch("builtins.input", side_effect=AssertionError("不该提问")), \
                mock.patch("sys.stdout", out):
            code = w.main([])
        self.assertEqual(code, 1, "非交互必须非零退出，避免脚本误判成功")
        self.assertIn("config.example.json", out.getvalue())

    def test_check_mode_reports_and_exits(self):
        out = io.StringIO()
        with mock.patch.object(w, "config_status", return_value=(True, "")), \
                mock.patch("sys.stdout", out):
            self.assertEqual(w.main(["--check"]), 0)
        self.assertIn("OK", out.getvalue())
        with mock.patch.object(w, "config_status", return_value=(False, "config.json 不存在")), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(w.main(["--check"]), 1)

    def test_no_secret_in_any_output(self):
        """写盘提示只能出现掩码，绝不能回显明文 key。"""
        out = io.StringIO()
        with mock.patch.object(w, "load_template", return_value=_template()), \
                mock.patch.object(w, "_interactive", return_value=True), \
                mock.patch.object(w, "probe_endpoint", return_value=(True, "ok")), \
                mock.patch.object(w, "probe_allowed", return_value=(True, "")), \
                mock.patch.object(sys.stdin, "isatty", return_value=False, create=True), \
                mock.patch("builtins.input",
                           side_effect=["1", "", KEY_LONG, "", "2"]), \
                mock.patch("sys.stdout", out):
            w.run_interactive(path=self.cfg_path)
        self.assertNotIn(KEY_LONG, out.getvalue(), "终端输出不得含明文 key")


class TestStartupScriptWiring(unittest.TestCase):
    """启动脚本必须真的会调用引导，且不破坏既有 bat 约束。"""

    def test_start_bat_calls_wizard_on_failure(self):
        src = Path("start.bat").read_text(encoding="ascii")
        self.assertIn("setup_wizard.py", src)
        self.assertIn("call :check_config", src)
        self.assertIn("if defined WM_NONINTERACTIVE goto :config_fail", src,
                      "自动化场景不得进入交互问答")
        self.assertIn(":check_config", src)
        # 引导必须发生在启动服务之前（用启动步骤标记比较，launcher.py 字样在文件中多次出现）
        self.assertLess(src.index("setup_wizard.py"), src.index("Starting services"))

    def test_start_sh_has_config_gate(self):
        src = Path("start.sh").read_text(encoding="utf-8")
        self.assertIn("setup_wizard.py --check", src)
        self.assertIn("setup_wizard.py", src)
        self.assertIn("WM_NONINTERACTIVE", src)
        self.assertLess(src.index("setup_wizard.py"), src.index('"$PY" launcher.py'))


if __name__ == "__main__":
    unittest.main()
