# -*- coding: utf-8 -*-
"""织光 - 任务工作区管理。

每个任务一个独立成果文件夹，方便按项目分组/查看/移动：
    <WORKSPACE_ROOT>/projects/<project>/<task_id>/
        project/   代码执行 / 文件读写产出（含 screenshots/）
        reports/   报告生成器产出
        data/      data_loader / model_trainer 数据
        charts/    data_analyzer 图表
        *.zip      最终交付包

兼容性：
- 旧版平铺路径 <WORKSPACE_ROOT>/<task_id>/ 仍可通过 task_workspace() 回退定位；
- task_workspace(task_id) 不带 project 时，先按新路径（内存索引 → 扫描
  projects/ 目录）查找，找不到再回退旧路径；
- 可通过环境变量 WEAVEMIND_WORKSPACE_ROOT 覆盖根目录（测试/多实例部署用）。
"""

import os
import re
import tempfile
import threading
from pathlib import Path

WORKSPACE_ROOT = Path(
    os.environ.get(
        "WEAVEMIND_WORKSPACE_ROOT",
        str(Path(tempfile.gettempdir()) / "agent_workspace" / "tasks"),
    )
)

_DEFAULT_PROJECT = "default"

# 进程内 task_id -> project 索引：本进程先 ensure 过即可免扫描定位；
# 其他进程（如 web_ui 与 orchestrator 分开跑）通过扫描 projects/ 目录兜底。
_task_project_index: dict[str, str] = {}
_index_lock = threading.Lock()


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


def _safe_project(project: str | None) -> str:
    """把项目名变成安全的目录名，防止路径穿越（保留中文/字母/数字/._-）。"""
    if not project:
        return _DEFAULT_PROJECT
    s = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]", "_", str(project)[:60]).strip("._")
    return s or _DEFAULT_PROJECT


def projects_root() -> Path:
    """项目分组根目录：<WORKSPACE_ROOT>/projects。"""
    return WORKSPACE_ROOT / "projects"


def project_workspace(project: str | None, task_id: str) -> Path:
    """新路径：<ROOT>/projects/<project>/<task_id>/。"""
    return (
        projects_root()
        / _safe_project(project)
        / _safe_task_id(task_id)
    )


def _remember_project(task_id: str, project: str) -> None:
    """把 task_id → project 记入进程内索引，后续免目录扫描。"""
    with _index_lock:
        _task_project_index[_safe_task_id(task_id)] = _safe_project(project)


def _scan_project_for_task(task_id: str) -> Path | None:
    """扫描 projects/ 下所有项目，按目录名精确匹配任务。"""
    tid = _safe_task_id(task_id)
    proot = projects_root()
    if not proot.is_dir():
        return None
    try:
        for pdir in sorted(proot.iterdir()):
            if pdir.is_dir() and (pdir / tid).is_dir():
                return pdir / tid
    except Exception:
        pass
    return None


def task_workspace(task_id: str, project: str | None = None) -> Path:
    """任务成果根目录（不存在时不会自动创建，由调用方 mkdir）。

    定位顺序：
    1. 显式 project → 新路径 projects/<project>/<task_id>/；
    2. 进程内索引（本进程创建过）→ 新路径；
    3. 扫描 projects/ 目录（跨进程定位，如 web_ui 找 orchestrator 建的任务）；
    4. 回退旧版平铺路径 <ROOT>/<task_id>/。
    """
    if project is not None:
        return project_workspace(project, task_id)
    with _index_lock:
        known = _task_project_index.get(_safe_task_id(task_id))
    if known:
        return project_workspace(known, task_id)
    found = _scan_project_for_task(task_id)
    if found is not None:
        return found
    return WORKSPACE_ROOT / _safe_task_id(task_id)


def ensure_task_workspace(task_id: str, project: str | None = None) -> Path:
    """创建任务成果根目录及标准子目录。"""
    ws = task_workspace(task_id, project)
    _remember_project(task_id, _safe_project(project))
    ws.mkdir(parents=True, exist_ok=True)
    for sub in ("project", "reports", "data", "charts"):
        (ws / sub).mkdir(exist_ok=True)
    return ws


def task_project_dir(task_id: str, project: str | None = None) -> Path:
    p = task_workspace(task_id, project) / "project"
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_reports_dir(task_id: str, project: str | None = None) -> Path:
    p = task_workspace(task_id, project) / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_data_dir(task_id: str, project: str | None = None) -> Path:
    p = task_workspace(task_id, project) / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_charts_dir(task_id: str, project: str | None = None) -> Path:
    p = task_workspace(task_id, project) / "charts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_projects() -> list[dict]:
    """扫描项目目录，返回项目列表（含任务数）。
    旧版平铺任务目录归入 legacy 项目，保证迁移后仍可见。"""
    out: list[dict] = []
    proot = projects_root()
    if proot.is_dir():
        try:
            for pdir in sorted(proot.iterdir()):
                if not pdir.is_dir():
                    continue
                try:
                    task_count = sum(1 for d in pdir.iterdir() if d.is_dir())
                except Exception:
                    task_count = 0
                out.append({
                    "name": pdir.name,
                    "task_count": task_count,
                    "path": str(pdir),
                })
        except Exception:
            pass
    # 旧版平铺任务（直接位于根目录下的目录）归为 legacy
    try:
        legacy = [
            d for d in WORKSPACE_ROOT.iterdir()
            if d.is_dir() and d.name != "projects"
        ]
        if legacy:
            out.insert(0, {
                "name": "legacy",
                "task_count": len(legacy),
                "path": str(WORKSPACE_ROOT),
                "legacy": True,
            })
    except Exception:
        pass
    return out


def worker_dir(task: dict | None, sub: str, fallback: Path) -> Path:
    """worker 用：优先取任务工作区子目录，否则回退到旧共享目录。"""
    if task and task.get("workspace"):
        p = Path(str(task["workspace"])) / sub
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = fallback
    p.mkdir(parents=True, exist_ok=True)
    return p
