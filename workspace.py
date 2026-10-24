# -*- coding: utf-8 -*-
"""织光 - 任务工作区管理。

每个任务一个独立成果文件夹，方便按项目分组/查看/移动：
    <WORKSPACE_ROOT>/projects/<project>/<task_id>/
        project/   代码执行 / 文件读写产出（含 screenshots/）
        reports/   报告生成器产出
        data/      data_loader / model_trainer 数据
        charts/    data_analyzer 图表
        *.zip      最终交付包

稳定性（RUNTIME-1022 P0-A）：默认根不再依赖 `tempfile.gettempdir()`——DSH 会话
注入的 TEMP/TMP 每个会话都不同，此前默认根随之漂移，任务在库、产物却落在旧会话
临时目录里打不开。现在的解析顺序：

1. 显式 `WEAVEMIND_WORKSPACE_ROOT`（测试/多实例部署，优先级不变）；
2. `WEAVEMIND_DATA_DIR`（容器部署）→ <数据根>/workspace；
3. 稳定默认：模块目录旁的 `workspace/`（与任务库 `agents.db` 同一存储身份，
   不随 TEMP/TMP 与 CWD 漂移）。

历史回查（只读）：旧产物仍在旧根时，定位器按登记根回查——显式登记
（`WEAVEMIND_WORKSPACE_LEGACY_ROOTS` / <稳定根>/roots.json）+ 内建已知形态
（系统真实临时目录下的 `agent_workspace/tasks` 与 `dsh-*/agent_workspace/tasks`）。
定位到历史产物后把「任务身份 → 真实产物根」持久绑定到 <稳定根>/task_roots.json，
后续读取不再依赖临时目录扫描；绑定指向的目录消失时自动回退重扫。

只读与建目录严格区分：
- `locate_task_workspace` / `existing_task_workspace` 纯只读定位（可 `bind=False`）；
- `task_workspace` 保持既有语义：返回"既定或定位到的"路径，不创建目录；
- `ensure_task_workspace` 才创建（先定位沿用真实根，找不到才建新并绑定）。

兼容性：
- 旧版平铺路径 <ROOT>/<task_id>/ 仍可定位；
- 打开历史任务不会创建空目录，也不会改写产物目录里的任何内容。
"""

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path

import data_paths

_DEFAULT_PROJECT = "default"

# 判定"真产物"的标记：路径相对任务目录（目录或文件），命中越多分越高；
# 空壳（如仅 2 个文件的 official_discovery 残留）分数必然低于完整产物。
_ARTIFACT_MARKERS: tuple[str, ...] = (
    "working_paper.json",
    "financials.json",
    "analysis/dataset.json",
    "acceptance_report.json",
    "research_state.json",
    "reports",
    "project",
)
_MARKER_SCAN_CAP = 2000  # 单目录文件计数上限，防止异常大目录拖垮定位

# 进程内 task_id -> project 索引：本进程先 ensure 过即可免扫描定位；
# 其他进程（如 web_ui 与 orchestrator 分开跑）通过扫描 projects/ 目录兜底。
_task_project_index: dict[str, str] = {}
_index_lock = threading.Lock()
# 进程内定位缓存：只缓存"定位命中"（确定性的），未命中每次重扫（几次 stat，便宜）
_locate_cache: dict[str, Path] = {}


def system_temp_dir() -> Path:
    """系统真实临时目录（不受 DSH 会话注入的 TEMP/TMP 影响）。

    Windows 取 %LOCALAPPDATA%\\Temp（历史产物所在），POSIX 取 /tmp；
    都取不到再回退 tempfile.gettempdir()。
    """
    if os.name == "nt":
        base = str(os.environ.get("LOCALAPPDATA") or "").strip()
        cand = Path(base) / "Temp" if base else None
        if cand and cand.is_dir():
            return cand
    else:
        cand = Path("/tmp")
        if cand.is_dir():
            return cand
    return Path(tempfile.gettempdir())


def default_workspace_root() -> Path:
    """稳定默认根：显式环境变量 > 数据根 > 模块目录旁 workspace/。

    不使用 tempfile.gettempdir()——那是 P0-A 漂移的根因。
    """
    explicit = str(os.environ.get("WEAVEMIND_WORKSPACE_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    under_data = data_paths.under_data_root("workspace", "")
    if under_data:
        return Path(under_data)
    return Path(os.path.dirname(os.path.abspath(__file__))) / "workspace"


WORKSPACE_ROOT = default_workspace_root()


def configure_workspace_root(root: str | os.PathLike) -> Path:
    """覆盖工作区根目录（部署/测试用）。"""
    global WORKSPACE_ROOT
    WORKSPACE_ROOT = Path(root)
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    with _index_lock:
        _locate_cache.clear()
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


# ---- 历史根登记与内建已知形态（只读回查的候选根） ----

_LEGACY_ROOTS_ENV = "WEAVEMIND_WORKSPACE_LEGACY_ROOTS"
_ROOTS_FILE = "roots.json"
_BINDINGS_FILE = "task_roots.json"
_MAX_LEGACY_ROOTS = 20


def _valid_root(raw: str) -> Path | None:
    """登记根校验：绝对路径、真实存在的目录、且不是盘符/文件系统根。"""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        p = Path(s).expanduser().resolve()
    except Exception:
        return None
    if not p.is_absolute() or not p.is_dir():
        return None
    if len(p.parts) <= 2:      # 拒绝 C:\、/ 这类根，防止把整个盘当工作区扫
        return None
    return p


def _registered_legacy_roots() -> list[Path]:
    """显式登记的历史根：环境变量（os.pathsep 分隔）+ <稳定根>/roots.json。"""
    out: list[Path] = []
    raw_env = str(os.environ.get(_LEGACY_ROOTS_ENV) or "")
    for part in raw_env.split(os.pathsep):
        p = _valid_root(part)
        if p:
            out.append(p)
    try:
        f = WORKSPACE_ROOT / _ROOTS_FILE
        if f.is_file():
            data = json.loads(f.read_text(encoding="utf-8"))
            for raw in (data.get("roots") or []):
                p = _valid_root(raw)
                if p:
                    out.append(p)
    except Exception:
        pass
    return out


def _builtin_legacy_roots() -> list[Path]:
    """内建已知历史形态（P0-A 诊断证实的两类根，限定在系统临时目录内）。

    **默认不参与 ambient 扫描**（RUNTIME-1022 第一批复查教训）：测试任务 id（如
    `t-1`）会撞上历史会话残留的同名目录，把写入引去旧根——聚合用例两次跑出 count=1
    的真 bug。跨根恢复因此只走**显式登记**：`WEAVEMIND_WORKSPACE_LEGACY_ROOTS`
    或 `register_legacy_root()`（重启 runbook 里有现成命令）。本函数仅在显式登记
    值包含 `builtin` 哨兵时才展开内建清单，供运维自助排查，不进默认路径。
    """
    out: list[Path] = []
    env_raw = str(os.environ.get(_LEGACY_ROOTS_ENV) or "")
    if "builtin" not in [s.strip().lower() for s in env_raw.split(os.pathsep)]:
        return out
    bases: list[Path] = []
    sys_tmp = system_temp_dir()
    if sys_tmp not in bases:
        bases.append(sys_tmp)
    try:
        cur = Path(tempfile.gettempdir()).resolve()
    except Exception:
        cur = None
    if cur and cur not in bases:
        bases.append(cur)
    for base in bases:
        cand = base / "agent_workspace" / "tasks"
        if cand.is_dir():
            out.append(cand)
        try:
            for entry in sorted(base.iterdir()):
                if not entry.is_dir() or not entry.name.startswith("dsh-"):
                    continue
                cand = entry / "agent_workspace" / "tasks"
                if cand.is_dir():
                    out.append(cand)
        except Exception:
            pass
    return out


def workspace_roots() -> list[Path]:
    """定位用根清单（去重、有界）：显式根 > 数据根 > 登记历史根（含 `builtin` 哨兵展开）。

    模块目录旁的稳定默认根**只有它自己就是当前根时**参与扫描（显式根/数据根
    覆盖后不再隐式回扫）——否则测试与多实例里同名的残留任务目录会把定位引去
    旧根（RUNTIME-1022 教训：test_prune_superseded 被 `workspace/t1` 残留劫持）。
    覆盖根后的历史回查一律走显式登记（`WEAVEMIND_WORKSPACE_LEGACY_ROOTS` /
    <根>/roots.json / register_legacy_root）。
    """
    order: list[Path] = [WORKSPACE_ROOT]
    under_data = data_paths.under_data_root("workspace", "")
    if under_data:
        order.append(Path(under_data))
    order.extend(_registered_legacy_roots())
    order.extend(_builtin_legacy_roots())
    seen: set[str] = set()
    out: list[Path] = []
    for p in order:
        try:
            key = os.path.realpath(str(p)).lower() if os.name == "nt" else os.path.realpath(str(p))
        except Exception:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out[: 3 + _MAX_LEGACY_ROOTS]


def register_legacy_root(root: str | os.PathLike) -> bool:
    """把一个历史根显式登记进 <稳定根>/roots.json（幂等；不移动、不删除任何文件）。"""
    p = _valid_root(str(root))
    if not p:
        return False
    try:
        WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        f = WORKSPACE_ROOT / _ROOTS_FILE
        data: dict = {"version": 1, "roots": []}
        if f.is_file():
            try:
                loaded = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = {**data, **loaded}
                    if not isinstance(data.get("roots"), list):
                        data["roots"] = []
            except Exception:
                pass
        roots = [str(r) for r in data.get("roots", [])]
        if str(p) not in roots:
            roots.append(str(p))
        data["roots"] = roots
        data.setdefault("_comment", "只读回查的历史工作区根登记；不迁移不删除，可随时人工编辑")
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)
        with _index_lock:
            _locate_cache.clear()
        return True
    except Exception:
        return False


# ---- 持久绑定：任务身份 → 真实产物根 ----
#
# 绑定索引与任务库（agents.db）放在一起：绑定是"任务身份 → 产物根"的账本元数据，
# 与任务记录同一存储身份；因此测试隔离了 WEAVEMIND_DB 就同时隔离了绑定，
# 不会把测试 fixture 写进在线实例的工作区。

def _bindings_file() -> Path:
    """绑定索引位置：**当前工作区根自带一份** `<根>/task_roots.json`。

    为什么跟着根走而不是全局一份：绑定是"这个工作区里，任务身份 → 真实产物根"的
    定位索引。根一变（测试把工作区指到临时目录、部署换盘），索引必须跟着变——否则
    沙箱测试写进去的绑定会留在共享位置，被另一个根下的定位读到（实测过：聚合用例
    命中上一次跑留下的旧绑定，把两个任务的失败诊断算成了一个）。目录消失时绑定
    自动失效重扫，所以旧索引残留不会造成误读。
    """
    return WORKSPACE_ROOT / _BINDINGS_FILE


def _load_bindings() -> dict:
    try:
        f = _bindings_file()
        if f.is_file():
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("bindings"), dict):
                return data
    except Exception:
        pass
    return {"version": 1, "bindings": {}}


def _save_bindings(data: dict) -> bool:
    try:
        f = _bindings_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        data["version"] = 1
        data.setdefault("_comment", "任务身份→真实产物根的持久绑定（定位缓存；产物目录本身只读不动）")
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)
        return True
    except Exception:
        return False


def bind_task_root(task_id: str, root: Path, project: str | None = None) -> bool:
    """把任务的真实产物根持久绑定（幂等；只写绑定索引，不碰产物目录）。"""
    tid = _safe_task_id(task_id)
    try:
        rp = os.path.realpath(str(root))
    except Exception:
        rp = str(root)
    data = _load_bindings()
    bindings = data["bindings"]
    prev = bindings.get(tid)
    layout, _ = _layout_of(Path(rp))
    entry = {
        "root": rp,
        "project": _safe_project(project),
        "layout": layout,
        "bound_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if prev and isinstance(prev, dict):
        entry["first_bound_at"] = prev.get("first_bound_at") or prev.get("bound_at")
    bindings[tid] = entry
    # 绑定数量有界：只保留最近 500 个任务的绑定
    if len(bindings) > 500:
        for k in sorted(bindings, key=lambda k: bindings[k].get("bound_at", ""))[: len(bindings) - 500]:
            bindings.pop(k, None)
    ok = _save_bindings(data)
    if ok:
        with _index_lock:
            _locate_cache[tid] = Path(root)
    return ok


def _binding_for(task_id: str) -> Path | None:
    data = _load_bindings()
    entry = data["bindings"].get(_safe_task_id(task_id))
    if not isinstance(entry, dict):
        return None
    root = str(entry.get("root") or "")
    if not root:
        return None
    p = Path(root)
    return p if p.is_dir() else None   # 绑定目录已消失（临时根被清理）→ 回退重扫


# ---- 候选评分：真产物 vs 空壳 ----

def _marker_count(ws: Path) -> tuple[int, int]:
    """(命中标记数, 文件总数)；异常按 0 计。"""
    markers = 0
    try:
        for rel in _ARTIFACT_MARKERS:
            if (ws / rel).exists():
                markers += 1
    except Exception:
        markers = 0
    files = 0
    try:
        for _, _, names in os.walk(ws):
            files += len(names)
            if files >= _MARKER_SCAN_CAP:
                break
    except Exception:
        pass
    return markers, min(files, _MARKER_SCAN_CAP)


def _inside_root(candidate: Path, root: Path) -> bool:
    """候选必须真的位于该根内（双保险；task_id/project 已先做净化）。"""
    try:
        c = os.path.realpath(str(candidate))
        r = os.path.realpath(str(root))
        return os.path.commonpath([c, r]) == r
    except Exception:
        return False


def _discover_candidates(task_id: str, project: str | None) -> list[Path]:
    """在全部登记根里收集同名任务目录（只读；显式 project 不跨项目）。"""
    tid = _safe_task_id(task_id)
    out: list[Path] = []
    for root in workspace_roots():
        if project is not None:
            cands = [project_workspace(project, task_id) if root == WORKSPACE_ROOT
                     else root / "projects" / _safe_project(project) / tid,
                     root / tid]
        else:
            cands = [root / tid]
            proot = root / "projects"
            try:
                if proot.is_dir():
                    for pdir in sorted(proot.iterdir()):
                        if pdir.is_dir():
                            cands.append(pdir / tid)
            except Exception:
                pass
        for c in cands:
            try:
                if c.is_dir() and _inside_root(c, root) and c not in out:
                    out.append(c)
            except Exception:
                continue
    return out


def _layout_of(path: Path) -> tuple[str, Path]:
    """任务目录的布局与所属根：projects/<p>/<tid> 或平铺 <root>/<tid>。"""
    if path.parent.parent.name == "projects":
        return "projects", path.parent.parent.parent
    return "flat", path.parent


def locate_task_workspace(task_id: str, project: str | None = None,
                          *, bind: bool = True) -> dict:
    """只读定位任务的真实产物根（不创建目录、不改写产物）。

    返回 {task_id, path, found, project, layout, root, source,
          candidates, conflict, empty}：
    - source: binding（持久绑定）/ scan（本次扫描）/ none；
    - candidates: 全部同名候选（含评分），供冲突显式呈现；
    - conflict: 前两名评分相同（同名、同分、不同内容），选择是确定性平局裁定，
      调用方应向用户显式提示存在多份；
    - empty: 命中目录缺少任何产物标记（空壳）。
    """
    tid = _safe_task_id(task_id)
    # 1) 显式 project 且既定新路径已存在 → 直接用（新任务/常规写路径，不变）
    if project is not None:
        intended = project_workspace(project, task_id)
        if intended.is_dir():
            layout, root_dir = _layout_of(intended)
            return {"task_id": tid, "path": intended, "found": True,
                    "project": _safe_project(project), "layout": layout,
                    "root": str(root_dir), "source": "intended",
                    "candidates": [], "conflict": False, "empty": _marker_count(intended)[0] == 0}
    # 2) 进程内缓存 → 3) 持久绑定
    with _index_lock:
        cached = _locate_cache.get(tid)
    if cached is not None and cached.is_dir():
        layout, root_dir = _layout_of(cached)
        return {"task_id": tid, "path": cached, "found": True,
                "project": _safe_project(project), "layout": layout,
                "root": str(root_dir),
                "source": "cache", "candidates": [], "conflict": False,
                "empty": _marker_count(cached)[0] == 0}
    bound = _binding_for(tid)
    sources: list[tuple[str, Path | None]] = []
    if bound is not None:
        sources.append(("binding", bound))
    # 4) 扫描全部根
    cands = _discover_candidates(task_id, project)
    for c in cands:
        if c != bound:
            sources.append(("scan", c))
    if not sources:
        return {"task_id": tid, "path": None, "found": False,
                "project": _safe_project(project), "layout": None, "root": None,
                "source": "none", "candidates": [], "conflict": False, "empty": False}
    scored = sorted(
        ((src, p, _marker_count(p)) for src, p in sources),
        key=lambda t: (t[2][0], t[2][1]),
        reverse=True,
    )
    top_src, top, top_score = scored[0]
    conflict = len(scored) > 1 and scored[1][2] == top_score and scored[1][1] != top
    markers = top_score[0]
    layout, root_dir = _layout_of(top)
    info = {
        "task_id": tid, "path": top, "found": True,
        "project": _safe_project(project) if project else (
            top.parent.name if layout == "projects" else _DEFAULT_PROJECT),
        "layout": layout,
        "root": str(root_dir),
        "source": top_src, "conflict": conflict, "empty": markers == 0,
        "candidates": [
            {"path": str(p), "source": s, "markers": m, "files": n}
            for s, p, (m, n) in scored
        ],
    }
    if bind and not conflict:
        _, top_root = _layout_of(top)
        try:
            same_as_current = (
                os.path.realpath(str(top_root))
                == os.path.realpath(str(WORKSPACE_ROOT)))
        except Exception:
            same_as_current = False
        # 只在**跨根发现**（历史根/登记根）时写绑定：当前稳定根内的任务由扫描
        # 即可确定性定位，不必每读一次就落一次盘（也避免高频读放大写放大）。
        if not same_as_current:
            bind_task_root(tid, top, info.get("project"))
    return info


def existing_task_workspace(task_id: str, project: str | None = None) -> Path | None:
    """只读定位已存在的任务目录；找不到返回 None（绝不创建）。"""
    info = locate_task_workspace(task_id, project)
    p = info.get("path")
    return p if info.get("found") and p and p.is_dir() else None


def existing_task_project_dir(task_id: str, project: str | None = None) -> Path | None:
    """只读定位已存在任务的 project/ 子目录；任务或子目录不存在返回 None。

    读链（底稿/图表清单/导出登记）必须用本函数，不得用会建目录的
    task_project_dir —— 打开历史任务不得产生空目录。
    """
    ws = existing_task_workspace(task_id, project)
    if ws is None:
        return None
    p = ws / "project"
    return p if p.is_dir() else None


def _remember_project(task_id: str, project: str) -> None:
    """把 task_id → project 记入进程内索引，后续免目录扫描。"""
    with _index_lock:
        _task_project_index[_safe_task_id(task_id)] = _safe_project(project)


def _scan_project_for_task(task_id: str) -> Path | None:
    """扫描 projects/ 下所有项目，按目录名精确匹配任务（兼容旧调用）。"""
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
    1. 显式 project 且既定新路径存在 → 新路径 projects/<project>/<task_id>/；
    2. 进程内索引（本进程创建过）→ 新路径；
    3. 持久绑定 / 扫描全部登记根（含历史根，跨进程与跨会话定位）；
    4. 回退旧版平铺路径 <ROOT>/<task_id>/。
    """
    info = locate_task_workspace(task_id, project)
    p = info.get("path")
    if info.get("found") and p is not None:
        with _index_lock:
            _task_project_index[_safe_task_id(task_id)] = _safe_task_id(info.get("project") or _DEFAULT_PROJECT)
        return p
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
    """创建任务成果根目录及标准子目录。

    先只读定位：任务已有真实产物根（含历史根）→ 沿用并在那里补齐子目录，
    保证一个任务的产物始终在同一处；全新任务才在稳定默认根建新目录。
    """
    located = locate_task_workspace(task_id, project)
    p = located.get("path")
    if located.get("found") and p is not None:
        ws = p
    else:
        ws = task_workspace(task_id, project)
    _remember_project(task_id, _safe_project(project))
    ws.mkdir(parents=True, exist_ok=True)
    for sub in ("project", "reports", "data", "charts"):
        (ws / sub).mkdir(exist_ok=True)
    bind_task_root(task_id, ws, project)
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
