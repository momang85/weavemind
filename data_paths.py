# -*- coding: utf-8 -*-
"""可变数据根（运行时写入、容器重建后必须还在的东西）。

背景（架构审查 P1-3）：compose 只挂了 `/data`，但任务产物默认落在系统临时目录
（`workspace.py` 用 `tempfile.gettempdir()`），分享记录与审计日志默认落在应用目录，
`prompts/overrides.json` 同样——容器重建后历史、附件与配置一起消失，而这些恰恰是
"可核验"要依赖的东西。

用法：部署时设 `WEAVEMIND_DATA_DIR=/data`（镜像已内置该变量），于是
工作区 / 分享记录 / 审计日志 / 提示词覆盖 / 任务库（`db_paths`）全部落在该根下。
本地开发不设该变量，各模块沿用既有位置——不动用户机器上已有的产物目录。

优先级：各模块自己的专用环境变量（`WEAVEMIND_WORKSPACE_ROOT` / `SHARE_FILE` /
`AUDIT_FILE` / `WEAVEMIND_PROMPTS_DIR` / `WEAVEMIND_DB`）高于本根，便于单独覆盖与测试。
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_ENV = "WEAVEMIND_DATA_DIR"


def data_root() -> Path | None:
    """显式配置的可变数据根；未配置返回 None（各模块沿用既有默认位置）。"""
    raw = str(os.environ.get(DATA_DIR_ENV) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def under_data_root(rel: str, default: str | os.PathLike) -> str:
    """把运行时可写路径挂到数据根下；未配置数据根时原样返回 default。"""
    root = data_root()
    if root is None:
        return str(default)
    return str(root / rel)
