# -*- coding: utf-8 -*-
"""任务库路径的唯一解析口径（架构审查 P1-2）。

历史问题：`task_history` 的写者与读者各读各的环境变量——web_ui / checkpointer /
workers / 调度与守护进程读 `REGISTRY_DB`（默认相对 CWD 的 `agents.db`），
task_state / metrics_collector 读 `AGENTS_DB`（默认模块目录下的绝对路径）。
Dockerfile 只设了 `REGISTRY_DB=/data/agents.db`，于是编排器把状态写进
`/app/agents.db`（表不存在，且不在挂载卷里），web_ui 在 `/data/agents.db` 建表——
表现为"提交被判 accepted、历史里查不到、状态永远缺失"。

本模块是唯一入口：任何需要任务库路径的模块都必须调用 `resolve_db_path()`，
不得各自 `os.environ.get(...)`（`test_task_persistence.py` 会逐文件检查）。
默认值取模块所在目录（绝对路径），不再依赖 CWD。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# 优先级：新入口 > 既有主用变量 > 历史遗留变量
_ENV_ORDER = ("WEAVEMIND_DB", "REGISTRY_DB", "AGENTS_DB")

_warned = False


def default_db_path() -> str:
    """未设任何环境变量时的库路径：模块目录下的 agents.db（绝对路径）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents.db")


def resolve_db_path() -> str:
    """任务库绝对路径（每次调用都读环境变量，运行时切换与测试替身都能生效）。"""
    global _warned
    chosen = ""
    seen: dict[str, str] = {}
    for var in _ENV_ORDER:
        val = str(os.environ.get(var) or "").strip()
        if not val:
            continue
        path = os.path.abspath(val)
        seen[var] = path
        if not chosen:
            chosen = path
    if not chosen:
        return default_db_path()
    if not _warned and len(set(seen.values())) > 1:
        _warned = True
        logger.warning(
            "任务库环境变量指向不同文件 %s，已统一取 %s；建议只保留一个（推荐 WEAVEMIND_DB）",
            seen, chosen)
    return chosen
