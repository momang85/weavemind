"""织光 (ZhiGuang) - 统一日志配置（按大小轮转，防止日志无限膨胀）。"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB
LOG_BACKUP_COUNT = 3

LOG_DIR_ENV = "WEAVEMIND_LOG_DIR"

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _console_stream():
    """控制台流：尽力切到 UTF-8（Windows 默认 GBK 下中文日志会乱码）。"""
    try:
        import cli_text
        return cli_text.make_stream_writer_utf8(sys.stderr)
    except Exception:
        return sys.stderr


def is_test_run() -> bool:
    """测试进程判定（保守双条件，P1-E 日志隔离）。

    两个条件**同时**成立才认定为测试进程：显式环境变量 `WEAVEMIND_TEST_MODE=1`，
    或（入口脚本是 test_*.py 且已导入 unittest）。生产入口（web_ui.py /
    orchestrator_v2.py / launcher.py / worker）永远不满足——绝不误伤在线日志。
    """
    if str(os.environ.get("WEAVEMIND_TEST_MODE", "") or "").strip().lower() \
            in ("1", "true", "yes"):
        return True
    try:
        entry = (sys.argv[0] or "").strip()
    except Exception:                                  # noqa: BLE001
        entry = ""
    return "unittest" in sys.modules and Path(entry).name.lower().startswith("test_")


def log_dir() -> Path:
    """日志落地目录：显式环境变量 > 数据根 > 测试临时目录 > 仓库 logs/。

    优先级与 `data_paths.under_data_root` 同构：`WEAVEMIND_LOG_DIR`（部署与测试
    各自可指）高于 `WEAVEMIND_DATA_DIR/logs`（容器部署统一数据根）高于测试进程
    的系统临时目录（**测试日志绝不混入生产 logs/**——20261022 诊断：模拟 Redis /
    模拟任务名的日志条目曾与真实故障结论混在同一目录），最后才是本地开发默认。
    """
    explicit = str(os.environ.get(LOG_DIR_ENV, "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    try:
        from data_paths import data_root
        root = data_root()
        if root is not None:
            return Path(root) / "logs"
    except Exception:                                  # noqa: BLE001
        pass
    if is_test_run():
        return Path(tempfile.gettempdir()) / "weavemind-test-logs"
    return LOG_DIR


def setup_logging(name: str = "weavemind", level: int = logging.INFO) -> Path:
    """配置根 logger：控制台 + 轮转文件（log_dir()/{name}.log）。

    返回日志文件路径。可重复调用：会替换根 logger 的处理器。
    文件始终 UTF-8；控制台尽力 UTF-8（失败则退回默认流，不影响功能）。
    """
    target = log_dir()
    target.mkdir(parents=True, exist_ok=True)
    log_path = target / f"{name}.log"

    root = logging.getLogger()
    root.setLevel(level)
    # 清空旧处理器，避免重复输出
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(_console_stream())
    stream_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(stream_handler)

    return log_path
