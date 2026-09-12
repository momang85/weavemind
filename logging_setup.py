"""织光 (ZhiGuang) - 统一日志配置（按大小轮转，防止日志无限膨胀）。"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5MB
LOG_BACKUP_COUNT = 3

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _console_stream():
    """控制台流：尽力切到 UTF-8（Windows 默认 GBK 下中文日志会乱码）。"""
    try:
        import cli_text
        return cli_text.make_stream_writer_utf8(sys.stderr)
    except Exception:
        return sys.stderr


def setup_logging(name: str = "weavemind", level: int = logging.INFO) -> Path:
    """配置根 logger：控制台 + 轮转文件（logs/{name}.log）。

    返回日志文件路径。可重复调用：会替换根 logger 的处理器。
    文件始终 UTF-8；控制台尽力 UTF-8（失败则退回默认流，不影响功能）。
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"

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
