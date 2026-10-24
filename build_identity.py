# -*- coding: utf-8 -*-
"""实例身份与构建指纹（P1-E）：磁盘版本 / 启动时已加载版本 / 前端构建版本。

为什么需要它（20261022 运行时诊断）：在线实例 PID 24572 自 10-22 17:08 起运行，
源码之后有改动；但 `/api/health` 与 `/api/status` 都没有"我启动时加载的是哪份
字节、现在磁盘上又是哪份"的信息——于是"新代码验证通过"与"在线已在跑新代码"
只能靠人记。更糟的是，若用 `git rev-parse HEAD` 冒充版本，仓库历史会替进程
回答一个它并不知道的问题（HEAD 不说明已加载的字节）。

三条纪律：
1. 指纹一律来自**文件字节哈希**（sha256 前 12 位）；不用 git HEAD 冒充已加载代码；
2. 输出**不含绝对路径、环境变量值、密钥**——只有文件名、短哈希、字节数、计数；
3. "已加载"= 进程**导入本模块那一刻**的磁盘快照（长驻进程即启动时刻）。此后
   磁盘再变，`source.changed` 点名文件、`source.restart_required=true`——如实
   呈现"待受控重启"，不假装在线已在跑新代码。
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 指纹覆盖的关键模块：编排/API/成稿交付/检索证据/来源准入/契约/工作区/健康/日志。
# 只取与"研究闭环"直接相关的入口文件，避免每次健康探测都哈希几百个文件。
SOURCE_FILES = (
    "orchestrator_v2.py",
    "web_ui.py",
    "delivery_pipeline.py",
    "acceptance_checker.py",
    "report_brief.py",
    "workspace.py",
    "health_registry.py",
    "source_admission.py",
    "research_intent.py",
    "search_evidence.py",
    "net_diag.py",
    "logging_setup.py",
    "build_identity.py",
)

_IMPORTED_AT = time.time()
_IMPORTED_ISO = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(_IMPORTED_AT))


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts)))


def file_sha12(path: Path) -> str:
    """文件字节哈希（前 12 位）；读不到返回 `missing`，空文件返回 `empty`。"""
    try:
        p = Path(path)
        if not p.is_file():
            return "missing"
        h = hashlib.sha256()
        size = 0
        with p.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                h.update(chunk)
        return h.hexdigest()[:12] if size else "empty"
    except Exception:                                  # noqa: BLE001 - 指纹失败不阻断
        return "unreadable"


def source_fingerprint(base: str | os.PathLike | None = None) -> dict:
    """磁盘上关键源码的指纹（文件名 → 短哈希/字节数 + 合并哈希）。"""
    root = Path(base) if base else BASE_DIR
    files: dict[str, dict] = {}
    for name in SOURCE_FILES:
        p = root / name
        entry = {"sha": file_sha12(p)}
        try:
            entry["bytes"] = int(p.stat().st_size)
        except Exception:                              # noqa: BLE001
            entry["bytes"] = 0
        files[name] = entry
    joined = "|".join(f"{n}:{files[n]['sha']}" for n in SOURCE_FILES)
    return {"files": files,
            "sha": hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12],
            "at": _iso(time.time())}


# 启动时刻快照：长驻进程导入即记录，之后用于判断"磁盘是否已跑在会话之外"。
_LOADED = source_fingerprint()


def loaded_snapshot() -> dict:
    """进程启动时刻的源码指纹（深拷贝，调用方可安全修改）。"""
    return {"files": {k: dict(v) for k, v in _LOADED["files"].items()},
            "sha": _LOADED["sha"], "at": _LOADED["at"]}


def reset_loaded_for_test(snapshot: dict | None = None) -> None:
    """测试辅助：重置"启动时快照"（生产代码不得调用）。"""
    global _LOADED
    _LOADED = snapshot if snapshot is not None else source_fingerprint()


def changed_files(loaded: dict, disk: dict) -> list[str]:
    """哪些关键文件在启动之后变了（按文件名排序，不含路径）。"""
    lf = (loaded or {}).get("files") or {}
    df = (disk or {}).get("files") or {}
    out = [n for n in SOURCE_FILES
           if str((lf.get(n) or {}).get("sha")) != str((df.get(n) or {}).get("sha"))]
    return out


def frontend_fingerprint(base: str | os.PathLike | None = None) -> dict:
    """前端构建指纹：`frontend/dist/index.html` 与 assets 清单（名 + 字节数）。

    没有 dist 时返回 `state="dev"`（Vite 开发服务器或未构建）——**不假装有构建**。
    """
    root = Path(base) if base else BASE_DIR
    dist = root / "frontend" / "dist"
    index = dist / "index.html"
    if not index.is_file():
        return {"state": "dev", "index_sha": "missing", "assets": 0,
                "assets_sha": "missing"}
    assets = sorted(p.name for p in (dist / "assets").glob("*") if p.is_file()) \
        if (dist / "assets").is_dir() else []
    parts = []
    for name in assets:
        try:
            parts.append(f"{name}:{int((dist / 'assets' / name).stat().st_size)}")
        except Exception:                              # noqa: BLE001
            parts.append(f"{name}:0")
    assets_sha = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12] \
        if parts else "empty"
    return {"state": "built", "index_sha": file_sha12(index),
            "assets": len(assets), "assets_sha": assets_sha}


def _root_id(path_text: str) -> str:
    """根目录的**可识别但不泄露**的短指纹（8 位；不含路径本身）。"""
    if not path_text:
        return ""
    try:
        norm = os.path.normcase(os.path.abspath(str(path_text)))
        return hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:8]
    except Exception:                                  # noqa: BLE001
        return ""


def _workspace_ids() -> dict:
    out: dict = {"workspace_root_id": "", "data_root_id": "", "instance": ""}
    try:
        import workspace as _ws
        out["workspace_root_id"] = _root_id(str(getattr(_ws, "WORKSPACE_ROOT", "") or ""))
    except Exception:                                  # noqa: BLE001
        pass
    try:
        from data_paths import data_root
        root = data_root()
        out["data_root_id"] = _root_id(str(root)) if root else ""
    except Exception:                                  # noqa: BLE001
        pass
    try:
        import health_registry as _hr
        out["instance"] = _hr.instance_id()
    except Exception:                                  # noqa: BLE001
        pass
    return out


def identity() -> dict:
    """完整身份视图：实例 / 启动时刻 / 磁盘版本 / 已加载版本 / 前端构建 / 待重启。"""
    disk = source_fingerprint()
    loaded = _LOADED
    changed = changed_files(loaded, disk)
    fe_disk = frontend_fingerprint()
    fe_loaded = globals().get("_LOADED_FRONTEND") or fe_disk
    fe_changed = (str(fe_disk.get("index_sha")) != str(fe_loaded.get("index_sha"))
                  or int(fe_disk.get("assets") or 0) != int(fe_loaded.get("assets") or 0))
    ws_ids = _workspace_ids()
    return {
        "instance": ws_ids.get("instance") or "",
        "pid": os.getpid(),
        "started_at": _IMPORTED_ISO,
        "uptime_sec": int(max(0, time.time() - _IMPORTED_AT)),
        "python": f"{sys.version_info.major}.{sys.version_info.minor}."
                  f"{sys.version_info.micro}",
        "source": {
            "loaded_sha": str(loaded.get("sha") or ""),
            "loaded_at": str(loaded.get("at") or ""),
            "disk_sha": str(disk.get("sha") or ""),
            "changed": changed,
            "file_count": len(SOURCE_FILES),
            "restart_required": bool(changed),
        },
        "frontend": {
            "loaded_index_sha": str(fe_loaded.get("index_sha") or ""),
            "disk_index_sha": str(fe_disk.get("index_sha") or ""),
            "state": str(fe_disk.get("state") or ""),
            "assets": int(fe_disk.get("assets") or 0),
            "assets_sha": str(fe_disk.get("assets_sha") or ""),
            "changed": bool(fe_changed),
        },
        "workspace_root_id": ws_ids.get("workspace_root_id") or "",
        "data_root_id": ws_ids.get("data_root_id") or "",
        "restart_required": bool(changed or fe_changed),
        "note": ("源码/前端构建在进程启动后发生变化，当前实例仍在跑启动时加载的版本；"
                 "需按受控重启计划重启后才会生效"),
    }


# 启动时刻的前端构建快照（与源码快照同理：不用当前磁盘冒充"已加载"）。
_LOADED_FRONTEND = frontend_fingerprint()
