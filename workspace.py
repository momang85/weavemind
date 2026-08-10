# -*- coding: utf-8 -*-
"""织光 - 任务工作区管理。

每个任务一个独立成果文件夹，方便按任务查看/移动：
    <WORKSPACE_ROOT>/<task_id>/
        project/   代码执行 / 文件读写产出（含 screenshots/）
        reports/   报告生成器产出
        data/      data_loader / model_trainer 数据
        charts/    data_analyzer 图表
        *.zip      最终交付包

可通过环境变量 WEAVEMIND_WORKSPACE_ROOT 覆盖根目录（测试/多实例部署用）。
"""

import os
import re
import tempfile
from pathlib import Path

WORKSPACE_ROOT = Path(
    os.environ.get(
        "WEAVEMIND_WORKSPACE_ROOT",
        str(Path(tempfile.gettempdir()) / "agent_workspace" / "tasks"),
    )
)


def configure_workspace_root(root: str | os.PathLike) -> Path:
    """覆盖工作区根目录（部署/测试用）。"""
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = Path(root)
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    return WORKSPACE_ROOT


def _safe_task_id(task_id: str) -> str:
    """把任务 ID 变成安全的目录名，防止路径穿越。"""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", str(task_id)[:80]).strip("._")
    return s or "task"


def task_workspace(task_id: str) -> Path:
    """任务成果根目录（不存在时不会自动创建，由调用方 mkdir）。"""
    return WORKSPACE_ROOT / _safe_task_id(task_id)


def ensure_task_workspace(task_id: str) -> Path:
    """创建任务成果根目录及标准子目录。"""
    ws = task_workspace(task_id)
    ws.mkdir(parents=True, exist_ok=True)
    for sub in ("project", "reports", "data", "charts"):
        (ws / sub).mkdir(exist_ok=True)
    return ws


def task_project_dir(task_id: str) -> Path:
    p = task_workspace(task_id) / "project"
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_reports_dir(task_id: str) -> Path:
    p = task_workspace(task_id) / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_data_dir(task_id: str) -> Path:
    p = task_workspace(task_id) / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_charts_dir(task_id: str) -> Path:
    p = task_workspace(task_id) / "charts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def worker_dir(task: dict | None, sub: str, fallback: Path) -> Path:
    """worker 用：优先取任务工作区子目录，否则回退到旧共享目录。"""
    if task and task.get("workspace"):
        p = Path(str(task["workspace"])) / sub
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = fallback
    p.mkdir(parents=True, exist_ok=True)
    return p
