# -*- coding: utf-8 -*-
"""P1-E：测试日志与生产日志隔离（离线）。

验证（20261022 诊断：模拟 Redis / 模拟任务名的日志条目曾与真实故障结论混在
同一目录）：
1. `WEAVEMIND_LOG_DIR` 显式指定 > `WEAVEMIND_DATA_DIR/logs`（数据根）>
   测试进程的系统临时目录 > 仓库 `logs/`（生产默认）；
2. 测试进程判定：入口 test_*.py + 已导入 unittest，或显式 `WEAVEMIND_TEST_MODE`；
   生产入口（web_ui.py 等）永不误判；
3. 测试进程的 `setup_logging` 落在临时目录，**绝不写生产 logs/**；
4. 文件 UTF-8、可轮转配置不回归。
"""

import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import logging_setup


REPO = Path(__file__).resolve().parent
PROD_LOGS = REPO / "logs"


class _HandlerGuard:
    """setup_logging 会替换根 logger 处理器——测试后必须原样恢复。"""

    def __enter__(self):
        root = logging.getLogger()
        self._handlers = list(root.handlers)
        self._level = root.level
        return self

    def __exit__(self, *exc):
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in self._handlers:
            root.addHandler(h)
        root.setLevel(self._level)
        return False


class TestPriority(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="li-"))
        for k in ("WEAVEMIND_LOG_DIR", "WEAVEMIND_DATA_DIR", "WEAVEMIND_TEST_MODE"):
            self._saved = self._saved if hasattr(self, "_saved") else {
                k: os.environ.get(k) for k in
                ("WEAVEMIND_LOG_DIR", "WEAVEMIND_DATA_DIR", "WEAVEMIND_TEST_MODE")}
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in getattr(self, "_saved", {}).items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.base, ignore_errors=True)

    def test_explicit_log_dir_wins(self):
        with mock.patch.dict(os.environ,
                             {logging_setup.LOG_DIR_ENV: str(self.base / "explicit")},
                             clear=False):
            d = logging_setup.log_dir()
        self.assertEqual(d, self.base / "explicit")

    def test_data_root_second(self):
        with mock.patch.dict(os.environ,
                             {"WEAVEMIND_DATA_DIR": str(self.base / "data")},
                             clear=False):
            d = logging_setup.log_dir()
        self.assertEqual(d, self.base / "data" / "logs")

    def test_test_run_falls_back_to_tempdir(self):
        with mock.patch.object(sys, "argv", [str(REPO / "test_log_isolation.py")]):
            d = logging_setup.log_dir()
        self.assertTrue(str(d).startswith(tempfile.gettempdir()),
                        f"测试日志应落临时目录，实际 {d}")
        self.assertNotEqual(d, logging_setup.LOG_DIR)

    def test_test_mode_forces_tempdir_for_any_entry(self):
        with mock.patch.object(sys, "argv", [str(REPO / "web_ui.py")]), \
                mock.patch.dict(os.environ, {"WEAVEMIND_TEST_MODE": "1"}, clear=False):
            d = logging_setup.log_dir()
        self.assertTrue(str(d).startswith(tempfile.gettempdir()), f"实际 {d}")

    def test_production_default_unchanged(self):
        # 生产入口（web_ui.py）、unittest 已导入但入口不是 test_*、无环境变量
        # → 仍是仓库 logs/（不动用户机器上已有目录）。
        saved_unittest = sys.modules.pop("unittest", None)
        try:
            with mock.patch.object(sys, "argv", [str(REPO / "web_ui.py")]):
                d = logging_setup.log_dir()
        finally:
            if saved_unittest is not None:
                sys.modules["unittest"] = saved_unittest
        self.assertEqual(d, logging_setup.LOG_DIR)


class TestIsTestRun(unittest.TestCase):
    def test_test_entry_with_unittest(self):
        with mock.patch.object(sys, "argv", [str(REPO / "test_anything.py")]):
            self.assertTrue(logging_setup.is_test_run())

    def test_prod_entry_not_test_even_with_unittest_imported(self):
        with mock.patch.object(sys, "argv", [str(REPO / "orchestrator_v2.py")]):
            self.assertFalse(logging_setup.is_test_run())

    def test_test_mode_env(self):
        argv = [str(REPO / "launcher.py")]   # 非测试入口，不受 unittest 误判影响
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"WEAVEMIND_TEST_MODE": "1"}, clear=False):
            self.assertTrue(logging_setup.is_test_run())
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"WEAVEMIND_TEST_MODE": ""}, clear=False):
            self.assertFalse(logging_setup.is_test_run())


class TestSetupLoggingIsolation(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="li2-"))
        self._saved = {k: os.environ.get(k) for k in
                       ("WEAVEMIND_LOG_DIR", "WEAVEMIND_DATA_DIR",
                        "WEAVEMIND_TEST_MODE")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.base, ignore_errors=True)

    def test_test_process_writes_tempdir_not_prod_logs(self):
        with _HandlerGuard():
            with mock.patch.object(sys, "argv", [str(REPO / "test_log_isolation.py")]):
                p = logging_setup.setup_logging("t-isolated")
            logging.getLogger("t-isolated").info("模拟任务 t-wd 的日志条目")
            for h in logging.getLogger().handlers:
                h.flush()
        self.assertTrue(str(p).startswith(tempfile.gettempdir()), str(p))
        self.assertTrue(p.is_file())
        # 生产 logs/ 不新增同名文件（此名在生产目录本就不存在）
        self.assertFalse((PROD_LOGS / "t-isolated.log").exists())

    def test_explicit_dir_used_and_utf8(self):
        with _HandlerGuard():
            with mock.patch.dict(os.environ,
                                 {logging_setup.LOG_DIR_ENV: str(self.base / "log")},
                                 clear=False):
                p = logging_setup.setup_logging("t-utf8")
            logging.getLogger("t-utf8").info("中文日志 UTF-8 验证")
            for h in logging.getLogger().handlers:
                h.flush()
        self.assertEqual(p, self.base / "log" / "t-utf8.log")
        self.assertIn("中文日志 UTF-8 验证",
                      p.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
