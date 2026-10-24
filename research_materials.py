# -*- coding: utf-8 -*-
"""RC2-1024 · A3：把「本人研究任务」的准入事实 / 底稿 / 来源准入投影进数据准备。

为什么单独一个模块（`docs/evidence/20261024-data-rc2/architect_review.json` →
`additional_source_review[0]`）：`screen_prepare.materials_projection` 只列举筛选数据集、
官方缓存、清单与运行记录，**没有接通**既有公司研究任务的准入事实与底稿投影——用户在自己
的研究任务里已经复核过的材料，在「数据准备」这一屏看不见、也用不上。这里补上这一段，
并且只做两件事：

1. `materials_from_research(scope)` — **只读投影**：本账号已授权的研究任务里有什么材料：
   任务身份与状态、主体与期间、准入事实（事实 ID + 字段 + 值 + 期间 + 来源定位）、
   底稿（路径 / 哈希 / 条目数）、来源准入结论（采用 / 待核验 / 剔除）。不可用时给出
   显式原因（任务不存在 / 无权限 / 底稿缺失 / 无准入事实 / 事实被剔除），不当"没有材料"。
2. `collect_from_research(scope, *, task_id, fact_ids, periods)` — 用户**显式选定**事实后，
   把这些事实物化成**本作用域内的新数据集**（走 `screen_store.save_dataset_from_source`，
   服务端判定 `official=False`，provenance 带 `parent_task_id` / `parent_task_version` /
   `fact_ids` / `working_paper_ref`，于是结果永远能回到"哪一份原版"）。

复用了哪些既有实现（不另造权限、不另造事实层）：
- 任务存储与列定义：`task_state`（`task_history`）；DB 路径每次调用现取（`db_paths`），
  测试可用环境变量隔离到自己的临时库；
- 任务可见性：`user` 列等值（与结果缓存隔离 `web_ui._find_cached_task`、收执作用域
  `receipt_scope` 同一语义）。**不因为调用方是研究员就放宽**，也**不把管理员/调度器
  名下或没有提交人身份（历史/系统）的任务自动开放**；
- 任务工作区定位：`workspace.existing_task_project_dir`（只读，绝不建目录）；
- 事实与准入：`project/working_paper.json` 的 `rows` + `selection`（`working_paper.Selection`
  的 selected/pending/unrelated/conflicts 四分）；没有底稿时退回 `project/financials.json`
  → `facts.facts_from_financials` + `working_paper.select_facts`（有契约才判准入，
  没契约只作待核验，不冒充已认证）；
- 金额单位换算：`facts.to_yuan`（不可换算 = 不物化，不猜量级）；
- 来源准入留痕：`project/materials/index.json`（`material_intake` 的 admitted/rejected/
  pending_intake/fetch_failed）、`project/source_pending.json`、`project/source_exclusions.json`；
- 数据集写入与内容定址/复用/损坏判定：全部交给 `screen_store`，本模块不复制其逻辑。

红线（与任务书一致）：
- **不**从生成报告正文（`reports/*.md`）反抽数字——数字只来自事实层（底稿/结构化载荷）；
- **不**扫描整个 workspace 或其他账号的数据目录——只按 task_id 走既有任务定位；
- **不**猜：期间/字段/单位/披露日缺失逐项说明，不补零、不回填；
- **不**替用户签人工复核：这里只投影"有哪些材料"和"选定后物化成什么"，
  `review` 状态一概不写、不改。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from typing import Any

import db_paths as _db_paths
import facts as _facts
import screen_store as _st
import stock_screen as _ss
import task_state as _ts
import workspace as _ws

MODULE_VERSION = "t1-1"

# ── 来源身份契约（T1.2 / G5）：一个稳定枚举 + 四个独立布尔，贯穿投影/事实/数据集 ──
# 复核反例（`docs/evidence/20261024-trial-ready/architect_review.json` G5）：研究来源声明
# `user_provided=true`，存储元信息却固定写 `false`、来源类别恒为 `user_provided`，前端按
# `research_material` 判标签永远不命中。所以来源身份**不能靠改文案对齐**，而要有一套
# 显式契约：`kind` 说明"从哪来"，四个布尔分别回答四个不同的问题——
#   user_provided          是否用户提供的材料（任务事实投影 = 是）
#   official               是否服务端认定的官方数据（任务事实投影 = 否）
#   synthetic              是否合成样例（真实任务事实 = 否）
#   independent_verification 是否经过独立核验（投影不冒充核验 = 否）
RESEARCH_ORIGIN = "research_material"
ORIGIN_KINDS = {
    "research_material": "本人研究材料（任务事实投影）",
    "user_upload": "用户上传",
    "official_cache_materialized": "内置官方缓存物化（未核验原件）",
    "adapter_normalized": "适配器整理的宽表",
    "synthetic_demo": "合成演示",
    "user_provided": "用户提供（旧标识：读旧数据时按是否带 parent_task 归一）",
    "unknown": "来源未标注",
}
# 旧数据集把研究材料记成 `user_provided`：读的时候归一，写的时候不再产生这个歧义
_LEGACY_RESEARCH_KINDS = ("user_provided",)
_ORIGIN_FLAGS = ("user_provided", "official", "synthetic", "independent_verification")


def _research_origin(*, parent_task_id: str = "", parent_task_version: str = "",
                     working_paper_ref: dict | None = None,
                     fact_ids=()) -> dict:
    """研究材料的标准来源身份（新写入的数据集/事实/任务行都用它，避免三处各写一套）。"""
    return {
        "kind": RESEARCH_ORIGIN,
        "kind_label": ORIGIN_KINDS[RESEARCH_ORIGIN],
        "origin": "研究任务底稿（project/working_paper.json 或 project/financials.json）",
        "acquisition": "task_fact_projection",
        "user_provided": True,
        "official": False,
        "synthetic": False,
        "independent_verification": False,
        "parent_task_id": str(parent_task_id or ""),
        "parent_task_version": str(parent_task_version or ""),
        "working_paper_ref": dict(working_paper_ref or {}) or None,
        "fact_ids": [str(f) for f in (fact_ids or ()) if str(f).strip()][:200],
    }


def origin_identity(*, meta: dict | None = None, provenance: dict | None = None) -> dict:
    """把（数据集元信息, provenance）归一到**同一套**来源身份：稳定枚举 + 四个独立布尔。

    - 规范值：研究材料一律 `kind="research_material"`、`user_provided=True`、
      `official=False`、`synthetic=False`、`independent_verification=False`；
    - 兼容旧数据：旧数据集 `kind="user_provided"` 且带 `parent_task_id` → 归一为
      `research_material` 并标 `legacy=True`；**只读归一，不重写已冻结的数据集内容**；
    - 冲突可见：存储元信息与 provenance 不一致时 `consistent=False` + `mismatch` 列表，
      绝不静默挑一个当真（G5 的修法：先让不一致可见，再由存储层落地修正）。
    """
    prov = dict(provenance or {})
    mt = dict(meta or {})
    raw_kind = str(prov.get("kind") or mt.get("provenance_kind") or "").strip()
    parent_task_id = str(prov.get("parent_task_id") or mt.get("parent_task_id") or "").strip()
    looks_research = bool(parent_task_id) and (raw_kind in _LEGACY_RESEARCH_KINDS
                                              or raw_kind == RESEARCH_ORIGIN)
    kind = RESEARCH_ORIGIN if (raw_kind == RESEARCH_ORIGIN or looks_research) else raw_kind
    if kind not in ORIGIN_KINDS:
        kind = kind if kind else "unknown"
    if kind not in ORIGIN_KINDS:
        kind = "unknown"
    declared = prov.get("user_provided")
    synthetic = bool(prov.get("synthetic") or mt.get("synthetic") is True)
    if declared is None:
        user_provided = kind in (RESEARCH_ORIGIN, "user_upload", "synthetic_demo")
    else:
        user_provided = bool(declared)
    official = bool(prov.get("official")) and not user_provided and not synthetic
    label = str(prov.get("kind_label") or ORIGIN_KINDS.get(kind) or "")
    identity = {
        "kind": kind,
        "kind_label": label or ORIGIN_KINDS.get(kind, ""),
        "origin": str(prov.get("origin") or mt.get("origin") or ""),
        "acquisition": str(prov.get("acquisition") or ""),
        "user_provided": user_provided,
        "official": official,
        "synthetic": synthetic,
        "independent_verification": bool(prov.get("independent_verification")),
        "label": str(prov.get("label") or ""),
        "legacy": bool(looks_research and raw_kind in _LEGACY_RESEARCH_KINDS),
        "legacy_kind": raw_kind if raw_kind and raw_kind != kind else "",
        "parent_task_id": parent_task_id,
        "parent_task_version": str(prov.get("parent_task_version") or ""),
        "working_paper_ref": prov.get("working_paper_ref") or None,
    }
    mismatch: list[dict] = []
    if "user_provided" in mt and bool(mt.get("user_provided")) != user_provided:
        mismatch.append({"field": "meta.user_provided", "stored": bool(mt.get("user_provided")),
                         "canonical": user_provided})
    store_kind = str(mt.get("provenance_kind") or "")
    if store_kind and store_kind != kind and not (
            store_kind in _LEGACY_RESEARCH_KINDS and looks_research):
        mismatch.append({"field": "meta.provenance_kind", "stored": store_kind, "canonical": kind})
    identity["consistent"] = not mismatch
    identity["mismatch"] = mismatch
    identity["store_fix_required"] = (
        "screen_store.save_dataset_from_source 仍固定写 user_provided=False："
        "需改为按 provenance 落 user_provided/official/synthetic（清单见 "
        "docs/evidence/20261024-trial-ready/T1-research-flow.md §G5）" if mismatch else "")
    return identity


# ── 事实准入结论：与 working_paper.Selection 的四分对齐，不另造一套 ──────────────
ADMITTED = "admitted"        # 底稿认证选中：可用于物化
PENDING = "pending"          # 待核验：口径未声明/与请求不符，只展示不认证
EXCLUDED = "excluded"        # 剔除：主体是别的公司（只作审计提示）
CONFLICT = "conflict"        # 剔除：同一组合多条互不相容，悬而未决，不替用户选
UNJUDGED = "unjudged"        # 没有落库契约、无法判定（不冒充已准入）

ADMISSION_LABEL = {
    ADMITTED: "采用",
    PENDING: "待核验",
    EXCLUDED: "剔除（主体不符）",
    CONFLICT: "剔除（同组合冲突未决）",
    UNJUDGED: "未判定（缺研究契约）",
}

# 不可用原因：显式列出，界面与报告都能直接回答"为什么不能用"
# T1.4：无底稿 / 无准入事实 / **不可计算** / **读取损坏** / 权限不足 / 工作区缺失
# 必须分别给原因与行动建议——尤其不能把"损坏"当成"没有材料"。
UNAVAILABLE_REASONS = {
    "task_not_found": "任务不存在：任务库里查不到这个 task_id",
    "no_permission": "无权限：该任务不在本账号名下（管理员/调度器名下或无提交人身份的"
                     "历史任务不自动对研究员开放）",
    "workspace_missing": "工作区缺失：该任务在服务端没有可读的项目目录"
                         "（可能已清理或未同步到本机）",
    "working_paper_missing": "底稿缺失：任务工作区没有 working_paper.json，也没有 "
                             "financials.json（结构化事实层为空时无物化依据）",
    "working_paper_corrupt": "底稿损坏：working_paper.json 存在但无法解析（不是「没有底稿」），"
                             "需要重新导出或人工检查该文件",
    "financials_corrupt": "结构化载荷损坏：financials.json 存在但无法解析（不是「没有数据」）",
    "no_admitted_facts": "无准入事实：底稿里没有通过认证选择的记录（全是待核验/剔除）",
    "not_computable": "不可计算：有准入事实，但没有一条能落成筛选宽表字段"
                      "（字段不在白名单 / 单位不可换算 / 期间判不出 / 缺披露日）",
    "nothing_selected": "未显式选择：本入口不把任务里的全部事实自动带入，请给出 fact_ids",
}

# 宽表白名单：只有落进 `stock_screen.AMOUNT_METRICS` 的字段才能成为数据集列
_AMOUNT_METRICS = frozenset(_ss.AMOUNT_METRICS)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PERIOD_QUERY_RE = re.compile(r"^(?:19|20)\d{2}(?:([-/]\d{2}([-/]\d{2}))?|年)?$")
# 年报口径：与 working_paper._annual_year 同判据（季报/中报是累计口径，不参与年度期末）
_ANNUAL_PERIOD_TYPES = ("", "10-K", "年报", "annual", "Annual", "ANNUAL")

_MAX_FACTS_PER_TASK = 200          # 投影里的逐事实上限（超出只计数，不截断计数）
_MAX_TASKS = 200                   # 单次投影的任务上限（本账号最新 N 条）


# ── 只读：任务行与作用域 ────────────────────────────────────────────────────────

def _norm_scope(scope) -> dict:
    """作用域归一（复用 store 的判据）：user/project 都是服务端派生的，不接受声明。"""
    return dict(_st.normalize_scope(scope))


def _db_path() -> str:
    """任务库路径**每次现取**：测试用环境变量隔离到自己的临时库，不碰生产 agents.db。"""
    return str(_db_paths.resolve_db_path() or _ts.DB_PATH)


def _query(sql: str, params: tuple = ()) -> tuple[list[dict], str]:
    """只读查询（PRAGMA query_only）：库不存在/没有表 → 返回空 + 原因，不创建文件。"""
    path = _db_path()
    if not path or not os.path.isfile(path):
        return [], f"任务库不存在：{os.path.basename(path) or path}"
    con = None
    try:
        con = sqlite3.connect(path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows], ""
    except sqlite3.Error as exc:
        return [], f"任务库读取失败：{exc}"
    finally:
        if con is not None:
            try:
                con.close()
            except sqlite3.Error:
                pass


def task_owner(row: dict | None) -> str:
    """任务提交人（与 `web_ui._find_cached_task` 的 `COALESCE(user,'')` 同一列）。"""
    if not isinstance(row, dict):
        return ""
    return str(row.get("user") or row.get("user_id") or "").strip()


def _task_visible(row: dict, norm: dict) -> tuple[bool, str]:
    """既有可见性判断：提交人等值。**没有**"研究员看全部"这种放宽。"""
    owner = task_owner(row)
    if not owner:
        return False, "no_permission"
    if owner != str(norm.get("user") or ""):
        return False, "no_permission"
    return True, ""


def _read_task(task_id: str) -> dict | None:
    """按 task_id 读一行（只读，不建库、不建表）。"""
    tid = str(task_id or "").strip()
    if not tid:
        return None
    rows, _err = _query(
        "SELECT task_id, goal, status, created_at, updated_at, user, project, "
        "research_request_json FROM task_history WHERE task_id = ?", (tid,))
    return rows[0] if rows else None


def _scoped_task_rows(norm: dict, limit: int, offset: int = 0) -> tuple[list[dict], str, dict]:
    """本账号名下最新 N 条任务行：**先在查询层按服务端身份限定，再排序/分页**。

    G6（`architect_review.json`）：旧实现先 `ORDER BY created_at DESC LIMIT 800` 扫全局
    最近 800 条、再按提交人过滤——库里本人旧任务只有 1 条、最近 801 条都是别人的任务时，
    本人任务被**别人的新任务挤没**（`observed.returned_owned_tasks=0`）。修法不是把窗口
    调大（仍是"别人多写几条就挤掉"），而是把权限条件放进 SQL：

        WHERE COALESCE(user,'') = <本作用域用户> ORDER BY created_at DESC LIMIT/OFFSET

    于是"本人任务会不会出现"只取决于本人的数据，与别人新增多少任务无关。管理员/调度器
    名下、别人的任务、以及没有提交人身份的历史/系统任务都不进这个结果（沿用既有权限，
    没有"研究员看全部"的放宽）；`user` 为空的作用域一律返回空。
    """
    user = str(norm.get("user") or "").strip()
    limit = int(max(limit, 1))
    offset = int(max(offset, 0))
    counts = {"scanned": 0, "owned_total": 0, "other_owner": 0, "unattributed": 0,
              "scope_filter": "owner_equality(sql)", "limit": limit, "offset": offset,
              "has_more": False}
    if not user:
        return [], "", counts
    rows, err = _query(
        "SELECT task_id, goal, status, created_at, updated_at, user, project, "
        "research_request_json FROM task_history "
        "WHERE COALESCE(user, '') = ? "
        "ORDER BY COALESCE(created_at, '') DESC, task_id DESC LIMIT ? OFFSET ?",
        (user, limit, offset))
    counts["scanned"] = len(rows)
    if err:
        return [], err, counts
    # 透明计数：只数条数，不带任何他人任务身份（G6 之后不再需要"扫描窗口"）
    agg, _agg_err = _query(
        "SELECT COALESCE(user, '') AS owner, COUNT(*) AS n FROM task_history GROUP BY owner")
    for rec in agg:
        owner = str(rec.get("owner") or "")
        n = int(rec.get("n") or 0)
        if not owner:
            counts["unattributed"] += n
        elif owner == user:
            counts["owned_total"] = n
        else:
            counts["other_owner"] += n
    counts["has_more"] = (offset + len(rows)) < counts["owned_total"]
    return rows, "", counts


# ── 只读：任务工作区里的底稿 / 事实 / 来源准入 ──────────────────────────────────

def _project_dir_of(task_id: str, project: str):
    """只读定位任务的 project/ 目录（绝不 mkdir；打不开就是打不开）。"""
    try:
        return _ws.existing_task_project_dir(task_id, str(project or "").strip() or None)
    except Exception:                                        # noqa: BLE001 - 定位失败要显式化
        return None


def _read_json(path):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None, ""
    try:
        return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, hashlib.sha256(raw).hexdigest()


def _paper_summary(task_id: str, proj_dir) -> dict:
    """底稿摘要：路径 + 内容哈希 + 条目数（供"回到哪份原版"与复核对照）。

    T1.4：`present=False`（没有文件）与 `corrupt=True`（文件在但读不了）必须分开——
    把损坏报成"没有材料"会让用户以为再跑一遍任务就行，实际是那份文件坏了。
    """
    if proj_dir is None:
        return {"present": False, "corrupt": False,
                "reason": "任务工作区不存在（未投影任何内容）"}
    path = os.path.join(str(proj_dir), "working_paper.json")
    if not os.path.isfile(path):
        return {"present": False, "corrupt": False, "path": path.replace("\\", "/"),
                "reason": "没有 working_paper.json（该任务没有落库底稿）"}
    paper, sha = _read_json(path)
    if not isinstance(paper, dict):
        return {"present": True, "readable": False, "corrupt": True,
                "path": path.replace("\\", "/"), "sha256": sha,
                "short_sha256": sha[:12], "reason": "working_paper.json 不可解析"}
    rows = paper.get("rows") if isinstance(paper.get("rows"), list) else []
    problems = [str(p.get("detail") or p.get("kind") or p) if isinstance(p, dict) else str(p)
                for p in (paper.get("problems") or [])]
    return {
        "present": True, "readable": True, "corrupt": False,
        "path": path.replace("\\", "/"),
        "sha256": sha, "short_sha256": sha[:12], "bytes": len(rows), "rows": len(rows),
        "derived": len(paper.get("derived") or []),
        "gaps": len(paper.get("gaps") or []),
        "problems": problems[:20],
        "paper_ok": bool(paper.get("ok")) if "ok" in paper else None,
        "selection_counts": {k: len(v) for k, v in (paper.get("selection") or {}).items()
                             if isinstance(v, list)},
    }


def _admission_of(paper: dict) -> dict[str, tuple[str, str]]:
    """底稿 selection → 每条事实的准入结论与**可直接展示的原因**（沿用既有四分）。"""
    sel = paper.get("selection") if isinstance(paper.get("selection"), dict) else {}
    out: dict[str, tuple[str, str]] = {}
    for fid in sel.get("selected") or []:
        out[str(fid)] = (ADMITTED, "底稿认证选中：可用于物化")
    for rec in sel.get("pending") or []:
        if isinstance(rec, dict):
            why = str(rec.get("reason") or "口径未知或与请求不符：不用于认证")
            out[str(rec.get("fact_id") or "")] = (PENDING, f"待核验：{why}")
    for rec in sel.get("unrelated") or []:
        if isinstance(rec, dict):
            out[str(rec.get("fact_id") or "")] = (
                EXCLUDED, "剔除：主体与本任务标的不符（只作审计提示，不物化）")
    for rec in sel.get("conflicts") or []:
        if isinstance(rec, dict):
            why = str(rec.get("reason") or rec.get("detail") or "")
            for cand in rec.get("candidates") or []:
                fid = str((cand or {}).get("fact_id") or "") if isinstance(cand, dict) else ""
                if fid:
                    out[fid] = (CONFLICT,
                                f"剔除：同一（字段,期间）存在冲突候选（互不相容），不替用户选"
                                f"{('：' + why) if why else ''}")
    return out


def _view_fact(f: dict, admission: dict[str, tuple[str, str]]) -> dict:
    """一条事实的投影视图：ID + 字段 + 值 + 期间 + 来源定位 + 准入结论。"""
    fid = str(f.get("fact_id") or "")
    stage, why = admission.get(fid, (UNJUDGED, ADMISSION_LABEL[UNJUDGED]))
    loc = f.get("source_locator")
    if not isinstance(loc, dict):
        loc = {"raw": str(loc or "")}
    return {
        "fact_id": fid,
        "metric": str(f.get("metric") or ""),
        "metric_label": str(f.get("metric_label") or _facts.metric_label(str(f.get("metric") or ""))),
        "period": str(f.get("period") or ""),
        "period_end": str(f.get("period_end") or ""),
        "period_type": str(f.get("period_type") or ""),
        "value": f.get("value"),
        "raw_value": f.get("raw_value"),
        "unit": str(f.get("unit") or ""),
        "currency": str(f.get("currency") or ""),
        "caliber": str(f.get("caliber") or ""),
        "entity": str(f.get("entity") or ""),
        "entity_id": str(f.get("entity_id") or ""),
        "market": str(f.get("market") or ""),
        "disclosed_at": str(f.get("disclosed_at") or ""),
        "retrieved_at": str(f.get("retrieved_at") or ""),
        "source_url": str(f.get("source_url") or ""),
        "source_hash": str(f.get("source_hash") or ""),
        "locator": {k: v for k, v in loc.items()
                    if k in ("page", "sheet", "row", "col", "field", "quote", "anchor",
                             "table", "line", "xpath", "label", "section", "block")
                    or str(v or "").strip()} if loc else {},
        "verify_state": str(f.get("verify_state") or ""),
        "derived": bool(f.get("derived")),
        "formula": str(f.get("formula") or ""),
        "admission": stage,
        "admission_label": ADMISSION_LABEL.get(stage, stage),
        "admission_reason": why,
        # T1.2/G5：事实也带稳定来源身份（不靠调用方自己拼标签）
        "origin": _research_origin(parent_task_id=str(f.get("task_id") or "")),
        "materializable": stage == ADMITTED and str(f.get("metric") or "") in _AMOUNT_METRICS,
    }


def _facts_from_paper(proj_dir) -> tuple[list[dict], dict]:
    """底稿事实（首选来源）：project/working_paper.json 的 rows + selection。"""
    paper, _sha = _read_json(os.path.join(str(proj_dir), "working_paper.json"))
    if not isinstance(paper, dict):
        return [], {}
    admission = _admission_of(paper)
    rows = paper.get("rows") if isinstance(paper.get("rows"), list) else []
    return [_view_fact(f, admission) for f in rows if isinstance(f, dict)], paper


def _facts_from_financials(task_id: str, proj_dir, row: dict) -> tuple[list[dict], dict]:
    """退路：没有底稿时读 project/financials.json → 事实 →（有契约才）认证选择。

    契约来自 `task_history.research_request_json`（既有落库列）。没有契约时**不猜**：
    全部事实记 `unjudged`（未判定），因此不可物化，只能作为"待核验"呈现。
    """
    fin, _sha = _read_json(os.path.join(str(proj_dir), "financials.json"))
    if fin is None:
        # `_read_json` 用 sha 是否为空区分"文件不存在"与"文件在但读不了"（T1.4：损坏≠缺失）
        return [], {"financials": "unreadable" if _sha else "missing"}
    if not isinstance(fin, (dict, list)):
        return [], {"financials": "unreadable"}
    req_raw = None
    try:
        req_raw = json.loads(row.get("research_request_json") or "")
    except (TypeError, json.JSONDecodeError):
        req_raw = None
    req = _facts.ResearchRequest.from_payload(req_raw if isinstance(req_raw, dict) else None)
    try:
        objs = _facts.facts_from_financials(fin if isinstance(fin, dict) else {"items": fin},
                                            source_kind="financials_json")
    except Exception:                                        # noqa: BLE001 - 载荷形状不受控
        return [], {"financials": "unreadable"}
    dicts = [o.as_dict() for o in objs]
    if req is None:
        return [_view_fact(d, {}) for d in dicts], {"financials": "no_contract"}
    try:
        import working_paper as _wp
        sel = _wp.select_facts(objs, req)
        admission = {_str_fid(f): (ADMITTED, ADMISSION_LABEL[ADMITTED]) for f in sel.selected}
        for f in sel.pending:
            admission[_str_fid(f)] = (PENDING, ADMISSION_LABEL[PENDING])
        for f in sel.unrelated:
            admission[_str_fid(f)] = (EXCLUDED, ADMISSION_LABEL[EXCLUDED])
        for c in sel.conflicts:
            for cand in getattr(c, "candidates", []) or []:
                fid = _str_fid(cand)
                if fid:
                    admission[fid] = (CONFLICT, ADMISSION_LABEL[CONFLICT])
        return [_view_fact(d, admission) for d in dicts], {"financials": "selected"}
    except Exception as exc:                                 # noqa: BLE001 - 认证失败要显式
        return [_view_fact(d, {}) for d in dicts], {"financials": f"select_failed:{exc}"}


def _str_fid(f) -> str:
    """Fact 对象或 dict → fact_id（两处形状都可能：Selection 给 Fact、conflict 给 dict）。"""
    if isinstance(f, dict):
        return str(f.get("fact_id") or "")
    return str(getattr(f, "fact_id", "") or "")


def _admitted_sources(task_id: str, proj_dir) -> dict:
    """来源准入结论（若记录存在）：材料摄取分诊 + 检索采用/待核验/剔除三份留痕。

    只读既有落盘产物；没有记录就说"没有记录"，不把"缺失"当成"已核验通过"。
    """
    out: dict[str, Any] = {"present": False, "adopted": [], "pending": [], "excluded": [],
                           "counts": {}, "notes": []}
    if proj_dir is None:
        return out
    # 1) 用户上传/直链材料：material_intake 的 index.json（admitted/pending/rejected/fetch_failed）
    idx_path = os.path.join(str(proj_dir), "materials", "index.json")
    if os.path.isfile(idx_path):
        items, _sha = _read_json(idx_path)
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                stt = str(it.get("status") or "")
                rec = {"url": str(it.get("url") or ""), "title": str(it.get("title") or ""),
                       "material_id": str(it.get("material_id") or ""),
                       "status": stt, "reason": str(it.get("reason") or ""),
                       "provenance_label": str(it.get("provenance_label") or ""),
                       "source_class": str(it.get("source_class") or ""),
                       "period": str(it.get("period") or ""),
                       "doc_type": str(it.get("doc_type") or ""),
                       "disclosure_date": str(it.get("disclosure_date") or ""),
                       "text_sha256": str(it.get("text_sha256") or ""),
                       "from": "material_intake"}
                if stt == "admitted":
                    out["adopted"].append(rec)
                elif stt in ("pending_intake",):
                    out["pending"].append(rec)
                else:
                    out["excluded"].append(rec)
            out["present"] = True
    # 2) 检索候选：采用 / 待核验 / 剔除（search_evidence + orchestrator 的三份留痕）
    adopted, _sha = _read_json(os.path.join(str(proj_dir), "search_adopted.json"))
    if isinstance(adopted, dict):
        out["present"] = True
        totals = adopted.get("totals") if isinstance(adopted.get("totals"), dict) else {}
        out["adopted_total"] = {"steps": totals.get("steps"), "admitted": totals.get("admitted"),
                                "candidates": totals.get("candidates")}
        for step_id, rec in sorted((adopted.get("steps") or {}).items()):
            if isinstance(rec, dict):
                out["adopted"].append({"step_id": str(step_id),
                                       "status": str(rec.get("status") or ""),
                                       "candidates": rec.get("candidates"),
                                       "admitted": rec.get("admitted"),
                                       "stage": str(rec.get("stage") or ""),
                                       "at": str(rec.get("at") or ""),
                                       "from": "search_adopted"})
    for key, fname in (("pending", "source_pending.json"), ("excluded", "source_exclusions.json")):
        doc, _sha = _read_json(os.path.join(str(proj_dir), fname))
        if isinstance(doc, dict):
            out["present"] = True
            for rec in doc.get(key) or []:
                if isinstance(rec, dict):
                    out[key].append({"url": str(rec.get("url") or ""),
                                     "title": str(rec.get("title") or ""),
                                     "reason": str(rec.get("reason") or ""),
                                     "from": fname})
    out["counts"] = {"adopted": len(out["adopted"]), "pending": len(out["pending"]),
                     "excluded": len(out["excluded"])}
    if not out["present"]:
        out["notes"].append("该任务没有来源准入留痕（materials/index.json、search_adopted.json、"
                            "source_pending.json、source_exclusions.json 都不存在）")
    for lst in ("adopted", "pending", "excluded"):
        out[lst] = out[lst][:50]
    return out


# ── 期间工具（不猜：判不出就空，逐项说明） ──────────────────────────────────────

def _annual_year(f: dict) -> int | None:
    """事实所属**年度**：只认年报/未标类型（与 working_paper._annual_year 同判据）。"""
    ptype = str(f.get("period_type") or "")
    if ptype and ptype not in _ANNUAL_PERIOD_TYPES:
        return None
    m = str(f.get("period") or "").strip()
    if m.endswith("年") and m[:-1].isdigit():
        return int(m[:-1])
    if m.isdigit() and len(m) == 4:
        return int(m)
    return None


def _row_period(f: dict) -> tuple[str, str]:
    """(期末日 YYYY-MM-DD, 依据)；判不出返回 ("", 原因)。"""
    pend = str(f.get("period_end") or "").strip()
    if _DATE_RE.match(pend):
        return pend, "事实自带期末日"
    year = _annual_year(f)
    if year:
        return f"{year}-12-31", "年报惯例期末 12-31（事实未带 period_end，非来源声明日期）"
    return "", (f"期间无法确定：period={f.get('period') or '空'}、"
                f"period_type={f.get('period_type') or '空'}（不猜期末日）")


def _norm_period_query(values) -> tuple[list[str], list[str]]:
    """用户声明的期间 → 年度/期末日过滤条件；不认识的写法进错误，不静默丢弃。"""
    years: list[str] = []
    errs: list[str] = []
    for v in values or ():
        s = str(v).strip()
        if not s:
            continue
        if not _PERIOD_QUERY_RE.match(s):
            errs.append(f"期间写法不认识：{s}（请用 2024 / 2024年 / 2024-12-31）")
            continue
        y = s[:4]
        if y not in years:
            years.append(y)
    return years, errs


def _period_selected(f: dict, years: list[str]) -> bool:
    if not years:
        return True
    year = _annual_year(f)
    if year:
        return str(year) in years
    pend = str(f.get("period_end") or "")
    return bool(pend[:4]) and pend[:4] in years


# ── 对外 API 1：只读投影 ─────────────────────────────────────────────────────────

def research_material(scope, task_id: str) -> dict:
    """单个任务的材料投影（含显式 unavailable 原因）。"""
    norm = _norm_scope(scope)
    row = _read_task(task_id)
    if row is None:
        return _unavailable_item(norm, str(task_id or ""), "task_not_found")
    ok, code = _task_visible(row, norm)
    if not ok:
        item = _unavailable_item(norm, str(row.get("task_id") or task_id), code)
        item["title"] = "（不展示他人任务标题）"
        item["owner"] = task_owner(row) or ""
        return item
    return _item_of(norm, row)


def materials_from_research(scope, *, limit: int = _MAX_TASKS, offset: int = 0,
                            task_ids=()) -> dict:
    """本账号已授权的现有研究任务材料（**只读投影**，供 `screen_prepare` 与研究界面）。

    返回 `{status, scope, tasks, unavailable, totals, note}`；`status` 只有
    `ok`（有可物化材料）与 `unavailable`（没有/读不到）两种，两种都带逐任务说明。
    分页参数 `limit`/`offset` 只作用在**本账号任务**上（G6：权限先于分页）。
    """
    try:
        norm = _norm_scope(scope)
    except ValueError as exc:
        return {"status": "unavailable", "scope": {}, "tasks": [], "unavailable": [],
                "totals": {}, "note": f"作用域不合法：{exc}"}
    err = ""
    counts = {"scanned": 0, "owned_total": 0, "other_owner": 0, "unattributed": 0,
              "has_more": False, "limit": int(limit), "offset": int(offset)}
    if task_ids:
        items = [research_material(norm, t) for t in task_ids if str(t or "").strip()]
    else:
        rows, err, counts = _scoped_task_rows(norm, limit, offset)
        items = [_item_of(norm, r) for r in rows]
    usable = [i for i in items if i.get("usable")]
    blocked = [i for i in items if not i.get("usable")]
    out = {
        "status": "ok" if usable else "unavailable",
        "scope": dict(norm),
        "version": MODULE_VERSION,
        "tasks": usable + blocked,
        "unavailable": [{"task_id": i["task_id"], "code": (i.get("unavailable") or {}).get("code"),
                         "reason": (i.get("unavailable") or {}).get("reason"),
                         "hint": (i.get("unavailable") or {}).get("hint"),
                         "entities": i.get("entities") or [], "periods": i.get("periods") or []}
                        for i in blocked],
        "totals": {
            "tasks_total": len(items),
            "usable": len(usable),
            "facts_admitted": sum(int(i.get("facts_admitted") or 0) for i in items),
            "facts_pending": sum(int(i.get("facts_pending") or 0) for i in items),
            "facts_excluded": sum(int(i.get("facts_excluded") or 0) for i in items),
            "scanned_rows": counts["scanned"],
            "owned_total": counts.get("owned_total", 0),
            "has_more": bool(counts.get("has_more")),
            "excluded_other_owner": counts["other_owner"],
            "excluded_unattributed": counts["unattributed"],
        },
        "paging": {"limit": int(limit), "offset": int(offset),
                   "owned_total": counts.get("owned_total", 0),
                   "has_more": bool(counts.get("has_more")),
                   "filter": "先按提交人等值限定，再排序/分页（G6）"},
        "note": ("只列本账号名下（task_history.user == 本作用域用户）的研究任务，"
                 "**权限条件在查询层**（先限定再排序分页：别人新增任务不会把本人旧任务挤掉）；"
                 "事实来自底稿（working_paper.json 的 rows+selection）或结构化载荷"
                 "（financials.json），**没有**从报告正文反抽数字，**没有**扫描其他账号目录；"
                 "不可用原因逐项给出（无底稿/底稿损坏/无准入事实/不可计算/无权限分开说）。"
                 "本投影不写任何文件、不改任何状态。"),
    }
    if err:
        out["error"] = err
        out["note"] += f"（任务库读取提示：{err}）"
    if counts["unattributed"]:
        out["note"] += (f"；另有 {counts['unattributed']} 条任务没有提交人身份"
                        "（历史/系统任务），未纳入投影")
    return out


def _unavailable_item(norm: dict, task_id: str, code: str) -> dict:
    reason = UNAVAILABLE_REASONS.get(code, code)
    return {
        "task_id": task_id, "title": "", "status": "", "created_at": "", "updated_at": "",
        "owner": "", "project": "", "entities": [], "periods": [], "facts": [],
        "facts_total": 0, "facts_admitted": 0, "facts_pending": 0, "facts_excluded": 0,
        "facts_source": "", "fields": {}, "usable_fields": [],
        "working_paper": {"present": False}, "working_paper_version": {},
        "admitted_sources": None, "warnings": [],
        "origin": _research_origin(parent_task_id=task_id),
        "usable": False, "unavailable": {"code": code, "reason": reason,
                                         "hint": _hint(code)},
    }


def _hint(code: str) -> str:
    return {
        "task_not_found": "确认 task_id 是否输错，或该任务是否已被清理",
        "no_permission": "需要该任务提交人本人登录才能看到其材料；本模块不代授权",
        "workspace_missing": "该任务的工作区不在本机数据根下：检查数据目录是否被清理/未同步",
        "working_paper_missing": "先让研究任务产出底稿（working_paper_export），或改用上传 CSV 入口",
        "working_paper_corrupt": "底稿文件存在但读不了：重新导出底稿，或人工检查该 JSON 后再投影",
        "financials_corrupt": "结构化载荷存在但读不了：重新预载财务数据，或改用上传 CSV 入口",
        "no_admitted_facts": "底稿只有待核验/剔除记录：先补来源与口径声明，再回来物化",
        "not_computable": "这些事实算不出当前 5 个筛选字段：可先看事实，或补齐单位/期间/披露日后重试",
        "nothing_selected": "在界面上勾选要用的事实（fact_ids），本入口不自动全带",
    }.get(code, "")


def _field_support(facts_view: list[dict]) -> dict:
    """按 `stock_screen.FIELD_SPECS`（与筛选**同一份**判据）判"哪些当前字段能算"。

    T1.3：前端要能回答「这条材料的哪些事实能用于当前字段」。字段→所需指标直接读
    `stock_screen.FIELD_SPECS`，不在这里另写一套映射（否则筛选侧改了字段，这里会漂）。
    """
    by_metric: dict[str, set[str]] = {}
    for f in facts_view:
        if f.get("admission") != ADMITTED or not f.get("metric"):
            continue
        year = _annual_year(f) or str(f.get("period_end") or "")[:4]
        if year:
            by_metric.setdefault(str(f["metric"]), set()).add(str(year))
    out: dict[str, dict] = {}
    for field_id, spec in _ss.FIELD_SPECS.items():
        if spec.numerator:
            need = [m for m in (spec.numerator, spec.denominator) if m]
        else:
            need = [spec.metric] if spec.metric else []
        missing = [m for m in need if not by_metric.get(m)]
        years: set[str] = set()
        for metric in need:
            if by_metric.get(metric):
                years = (years & by_metric[metric]) if years else set(by_metric[metric])
        ok = bool(need) and not missing and bool(years)
        if spec.needs_prev and ok and need:
            if not any(str(int(y) - 1) in by_metric.get(need[0], set())
                       for y in years if y.isdigit()):
                ok = False
                missing = missing + ["previous_year"]
        rec = {"label": spec.label, "status": "ok" if ok else ("partial" if not ok and missing
                                                              and len(missing) < len(need) else "missing"),
               "unit": spec.unit, "need": need, "missing": missing,
               "periods": sorted(years)}
        if spec.note:
            rec["note"] = spec.note
        out[field_id] = rec
    return out


def _item_of(norm: dict, row: dict) -> dict:
    """把一个任务行投影成材料条目（只读）。"""
    task_id = str(row.get("task_id") or "")
    project = str(row.get("project") or "")
    proj_dir = _project_dir_of(task_id, project)
    facts_view: list[dict] = []
    facts_source = ""
    fin_note: dict = {}
    paper = _paper_summary(task_id, proj_dir)
    if proj_dir is not None and paper.get("readable"):
        facts_view, paper_doc = _facts_from_paper(proj_dir)
        if not paper_doc:
            paper_doc = {}
        facts_source = "working_paper.json"
        if not facts_view:
            facts_view, fin_note = _facts_from_financials(task_id, proj_dir, row)
            if facts_view:
                facts_source = "financials.json（没有底稿行，按结构化载荷投影）"
    elif proj_dir is not None:
        facts_view, fin_note = _facts_from_financials(task_id, proj_dir, row)
        if facts_view:
            facts_source = "financials.json（没有可读底稿）"

    admitted = [f for f in facts_view if f["admission"] == ADMITTED]
    pending = [f for f in facts_view if f["admission"] == PENDING]
    excluded = [f for f in facts_view if f["admission"] in (EXCLUDED, CONFLICT, UNJUDGED)]
    # 事实的来源身份带上是哪个任务的投影（T1.2：贯穿到事实层）
    for f in facts_view:
        origin = f.get("origin") or {}
        if isinstance(origin, dict):
            origin["parent_task_id"] = task_id
            origin["parent_task_version"] = str(row.get("updated_at") or "")
            f["origin"] = origin
    entities: dict[str, dict] = {}
    for f in facts_view:
        key = f["entity_id"] or f["entity"]
        if not key:
            continue
        ent = entities.setdefault(key, {"entity": f["entity"], "entity_id": f["entity_id"],
                                        "code": f["entity_id"], "market": f["market"],
                                        "facts": 0, "admitted_facts": 0})
        ent["facts"] += 1
        if f["admission"] == ADMITTED:
            ent["admitted_facts"] += 1
        ent["market"] = ent["market"] or f["market"]
    periods = sorted({str(f.get("period") or "") for f in facts_view if f.get("period")})
    row_periods = sorted({p for p, _b in (_row_period(f) for f in admitted) if p})
    fields = _field_support(facts_view)

    item = {
        "task_id": task_id,
        "title": str(row.get("goal") or "")[:120],
        "status": str(row.get("status") or ""),
        "created_at": str(row.get("created_at") or ""),
        "updated_at": str(row.get("updated_at") or ""),
        "owner": task_owner(row),
        "project": project,
        "entities": list(entities.values())[:20],
        "periods": periods[:20],
        "row_periods": row_periods[:20],
        "facts": [{k: v for k, v in f.items() if k != "materializable"}
                  for f in (admitted + pending + excluded)[:_MAX_FACTS_PER_TASK]],
        "facts_total": len(facts_view),
        "facts_admitted": len(admitted),
        "facts_pending": len(pending),
        "facts_excluded": len(excluded),
        "facts_source": facts_source or "无事实来源",
        "facts_truncated": max(0, len(facts_view) - _MAX_FACTS_PER_TASK),
        "fields": fields,
        "usable_fields": [k for k, v in fields.items() if v["status"] == "ok"],
        "working_paper": paper,
        "working_paper_version": {
            "source": facts_source or "",
            "sha256": str(paper.get("sha256") or ""),
            "short": str(paper.get("short_sha256") or ""),
            "rows": int(paper.get("rows") or 0),
            "task_updated_at": str(row.get("updated_at") or ""),
        },
        "admitted_sources": _admitted_sources(task_id, proj_dir),
        "origin": _research_origin(
            parent_task_id=task_id, parent_task_version=str(row.get("updated_at") or ""),
            working_paper_ref=({"path": paper.get("path"), "sha256": paper.get("sha256"),
                                "rows": paper.get("rows")} if paper.get("path") else None),
            fact_ids=[f["fact_id"] for f in admitted]),
        "usable": False,
        "unavailable": None,
        "warnings": [],
    }
    if paper.get("corrupt"):
        item["warnings"].append("底稿损坏：working_paper.json 不可解析"
                                + ("；本次投影来自 financials.json" if facts_view else ""))
    if str(fin_note.get("financials") or "") == "unreadable":
        item["warnings"].append("结构化载荷损坏：financials.json 存在但读不了"
                                "（损坏 ≠ 没有材料，重新预载后可再投影）")
    if facts_source.startswith("financials") and fin_note.get("financials") == "no_contract":
        item["warnings"].append("结构化载荷没有研究契约（research_request_json）："
                                "事实只能标「未判定」，不能物化")
    if proj_dir is None:
        item["unavailable"] = {"code": "workspace_missing",
                               "reason": UNAVAILABLE_REASONS["workspace_missing"],
                               "hint": _hint("workspace_missing"),
                               "detail": paper.get("reason") or ""}
    elif not item["facts_total"]:
        if paper.get("corrupt"):
            code = "working_paper_corrupt"
        elif str(fin_note.get("financials") or "") == "unreadable":
            code = "financials_corrupt"
        else:
            code = "working_paper_missing"
        item["unavailable"] = {"code": code, "reason": UNAVAILABLE_REASONS[code],
                               "hint": _hint(code),
                               "detail": paper.get("reason") or fin_note or ""}
    elif not admitted:
        item["unavailable"] = {"code": "no_admitted_facts",
                               "reason": UNAVAILABLE_REASONS["no_admitted_facts"],
                               "hint": _hint("no_admitted_facts"),
                               "detail": f"待核验 {len(pending)} 条、剔除/未判定 {len(excluded)} 条"}
    elif not any(f["materializable"] for f in admitted):
        item["unavailable"] = {
            "code": "not_computable", "reason": UNAVAILABLE_REASONS["not_computable"],
            "hint": _hint("not_computable"),
            "detail": ("准入事实的字段都不在筛选宽表白名单（" +
                       "、".join(sorted(_AMOUNT_METRICS)[:5]) + "…）" if not any(
                           f["metric"] in _AMOUNT_METRICS for f in admitted) else
                       "准入事实缺可换算金额/可判期间/披露日，逐条见 facts[].admission_reason"),
        }
    else:
        item["usable"] = True
        if not item["usable_fields"]:
            item["note"] = ("有准入事实，但当前筛选字段（5 个）一个都算不出："
                            "只能看事实，不能直接生成该字段的筛选输入")
    if not item["usable"] and item["unavailable"]:
        item["note"] = (item.get("note") + "；" if item.get("note") else "") + \
            item["unavailable"]["reason"]
    return item


# ── 对外 API 2：显式选定后的物化 ─────────────────────────────────────────────────

def collect_from_research(scope, *, task_id: str, fact_ids=(), periods=(),
                          name: str = "", max_rows: int | None = None,
                          expect_paper_sha256: str = "") -> tuple[dict | None, list[str]]:
    """把用户**显式选定**的任务事实物化成本作用域的新数据集。

    返回 `(data, errors)`（与 `screen_prepare.collect` 同形状）：
    - `errors` 非空 → 没有写入任何数据集，逐条说清为什么；
    - `StoreWriteError` 原样抛出（调用方按"存储不可用"处理，绝不报成成功复用）；
    - `data.dataset` 的 provenance 带 `parent_task_id` / `parent_task_version` /
      `fact_ids` / `working_paper_ref`，`data.origin` 回答"能回到哪份原版"。

    T1.5：`expect_paper_sha256` 是**选择时**看到的底稿内容哈希。底稿在选择与提交之间被
    更正（哈希变了）时拒绝按旧选择物化，返回"请重新确认"，而不是把用户没看过的值写进数据集。
    """
    try:
        norm = _norm_scope(scope)
    except ValueError as exc:
        return None, [f"作用域不合法：{exc}"]

    selected_ids = [str(x).strip() for x in (fact_ids or ()) if str(x).strip()]
    want_periods, period_errs = _norm_period_query(periods)
    if period_errs:
        return None, period_errs
    if not str(task_id or "").strip():
        return None, ["没有指定 task_id：本入口不接受整批投影，请指明要投影哪一个研究任务"]
    if not selected_ids:
        return None, [UNAVAILABLE_REASONS["nothing_selected"]]

    item = research_material(norm, task_id)
    if item.get("unavailable"):
        un = item["unavailable"]
        return None, [f"{un['reason']}（任务 {task_id}）" +
                      (f"；{un['hint']}" if un.get("hint") else "")]

    # T1.5：选择绑定底稿内容哈希——底稿在"看"与"选"之间被更正时不按旧选择物化
    paper_now = item.get("working_paper") or {}
    current_sha = str(paper_now.get("sha256") or "")
    expected_sha = str(expect_paper_sha256 or "").strip().lower()
    if expected_sha:
        if not current_sha:
            return None, [f"无法确认底稿版本：任务 {task_id} 现在没有可读底稿"
                          "（选择时看到的那份已不在了）；请重新查看材料后再选"]
        if expected_sha != current_sha:
            return None, [f"底稿已变更：现在是 {current_sha[:12]}…，选择时是 "
                          f"{expected_sha[:12]}…；请重新查看该任务的事实并再次确认"
                          "（不按旧选择物化，避免把没看过的值写进数据集）"]

    by_id = {f["fact_id"]: f for f in item["facts"] if f.get("fact_id")}
    materialized: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for fid in selected_ids:
        if fid in seen:
            continue
        seen.add(fid)
        f = by_id.get(fid)
        if f is None:
            skipped.append({"fact_id": fid, "reason":
                            f"事实不属于该任务（或被投影上限截断，共 {item['facts_total']} 条，"
                            f"上限 {os.environ.get('WEAVEMIND_RM_MAX_FACTS', _MAX_FACTS_PER_TASK)}）"})
            continue
        if f["admission"] != ADMITTED:
            skipped.append({"fact_id": fid, "metric": f["metric"], "period": f["period"],
                            "reason": f["admission_reason"]})
            continue
        # 可选事实的其余门槛逐项检查并**全部列出**（不取第一个挡路的当唯一原因）：
        metric = str(f["metric"] or "")
        blockers: list[str] = []
        if metric not in _AMOUNT_METRICS:
            blockers.append("字段不在筛选宽表白名单里：不进数据集（不另造列）")
        if bool(f.get("derived")):
            blockers.append(f"派生值（公式 {f.get('formula') or '未记'}）："
                            "底稿派生列由筛选侧按同行数据复算，不重复物化")
        pend, basis = _row_period(f)
        if not pend:
            blockers.append(basis)
        elif not _period_selected(f, want_periods):
            blockers.append(f"未选期间（只接受 {', '.join(want_periods)}）")
        yuan = _facts.to_yuan(f.get("value"), f.get("unit"))
        if yuan is None:
            blockers.append(f"值或单位不可换算（value={f.get('value')!r}、"
                            f"unit={f.get('unit') or '空'}）：不猜量级、不补零")
        code = str(f.get("entity_id") or "").strip()
        entity = str(f.get("entity") or "").strip()
        if not code or not entity:
            blockers.append(f"缺主体标识（entity={entity or '空'}、"
                            f"code={code or '空'}）：不猜代码")
        disclosed = str(f.get("disclosed_at") or "")
        if not _DATE_RE.match(disclosed):
            blockers.append(f"缺来源披露日（disclosed_at={disclosed or '空'}）："
                            "无法证明该事实在时点截止前可得（PIT），不补日期")
        if blockers:
            skipped.append({"fact_id": fid, "metric": metric, "period": f["period"],
                            "row_period": pend, "reason": "；".join(blockers)})
            continue
        materialized.append({"fact_id": fid, "metric": metric, "metric_label": f["metric_label"],
                             "entity": entity, "code": code, "period": f["period"],
                             "row_period": pend, "period_basis": basis,
                             "value_yuan": yuan, "raw_value": f.get("raw_value"),
                             "unit": f["unit"], "currency": f["currency"],
                             "caliber": f["caliber"], "disclosed_at": disclosed,
                             "source_url": f["source_url"], "locator": f["locator"]})

    if want_periods:
        have = {m["row_period"][:4] for m in materialized}
        for y in want_periods:
            if y not in have:
                skipped.append({"fact_id": "", "period": y,
                                "reason": f"该期间在任务 {task_id} 里没有可物化的准入事实"})

    if not materialized:
        return None, [f"选定的 {len(seen)} 条事实都不能物化（逐项原因见 skipped）：" +
                      "；".join(sorted({str(s['reason']) for s in skipped}))[:400]]

    rows, group_skips = _rows_from_materialized(materialized)
    skipped.extend(group_skips)
    if not rows:
        return None, ["选定的事实经过同组冲突检查后没有可用行：" +
                      "；".join(sorted({str(g['reason']) for g in group_skips}))[:400]]

    used_ids = sorted({f for f in _row_fact_ids(rows) if f in seen})
    paper = item.get("working_paper") or {}
    # T1.2/G5：来源身份用**稳定枚举 + 四个独立布尔**（不再写歧义的 user_provided）
    prov = {
        "kind": RESEARCH_ORIGIN,
        "kind_label": ORIGIN_KINDS[RESEARCH_ORIGIN],
        "origin": "研究任务底稿（project/working_paper.json 或 project/financials.json）",
        "acquisition": "task_fact_projection",
        "user_provided": True,
        "official": False,
        "synthetic": False,
        "independent_verification": False,
        "label": f"本人研究材料（任务 {task_id} 事实投影）",
        "parent_task_id": task_id,
        "parent_task_version": str(item.get("updated_at") or ""),
        "parent_task_owner": str(item.get("owner") or ""),
        "parent_task_project": str(item.get("project") or ""),
        "fact_ids": used_ids,
        "working_paper_ref": ({"path": paper.get("path"), "sha256": paper.get("sha256"),
                               "rows": paper.get("rows")} if paper.get("path") else None),
        "paper_sha256": current_sha,
        "facts_source": str(item.get("facts_source") or ""),
        "source_urls": sorted({str(m.get("source_url") or "").strip()
                               for m in materialized if str(m.get("source_url") or "").strip()})[:50],
        "module_version": MODULE_VERSION,
        "note": ("行来自本账号研究任务的准入事实（底稿 rows+selection），不是官方数据，"
                 "也没有从报告正文反抽数字；人工复核状态一概不写。"),
    }
    title = str(name or "").strip() or f"研究材料投影 {task_id}"
    kwargs = {"scope": norm, "name": title[:80], "rows": rows, "provenance": prov}
    if max_rows:
        kwargs["max_rows"] = int(max_rows)
    meta, reused, errs = _st.save_dataset_from_source(**kwargs)
    if errs:
        return None, [f"数据集写入失败：{e}" for e in errs]

    identity = origin_identity(meta=meta or {}, provenance=prov)
    data = {
        "status": "ok", "scope": dict(norm), "source": f"research_task:{task_id}",
        "task": {"task_id": task_id, "title": item.get("title"), "status": item.get("status"),
                 "owner": item.get("owner"), "project": item.get("project"),
                 "version": item.get("updated_at"), "facts_total": item.get("facts_total"),
                 "facts_admitted": item.get("facts_admitted")},
        "dataset": meta, "reused": bool(reused),
        "row_count": (meta or {}).get("row_count"), "rows": rows,
        "materialized": [m for m in materialized if m["fact_id"] in set(used_ids)],
        "skipped": skipped,
        "requested_fact_ids": selected_ids, "requested_periods": want_periods,
        # G5：数据集来源身份的**规范答案**（含与存储元信息不一致时的可见说明）
        "dataset_origin": identity,
        "origin": {
            "parent_task_id": task_id,
            "parent_task_version": prov["parent_task_version"],
            "fact_ids": used_ids,
            "working_paper_ref": prov["working_paper_ref"],
            "paper_sha256": current_sha,
            "identity": identity,
            "note": "回到原版：按 parent_task_id + parent_task_version 取该任务底稿，"
                    "再按 fact_ids 逐条对照（working_paper_ref.sha256 证明文件未变）。",
        },
        "counters": {"network_attempts": 0, "network_requests": 0, "network_denied": 0,
                     "adapter_calls": 0, "pre_connect_failures": 0,
                     "note": "本入口只读本账号任务产物并写本作用域数据集：没有调用任何"
                             "取数适配器，也没有发起传输层连接"},
        "note": ("已把显式选定的准入事实物化成本作用域的新数据集"
                 "（provenance.kind=research_material、user_provided=True、official=False、"
                 "synthetic=False）。逐事实的未物化原因见 skipped；"
                 "缺失的期间/字段没有补零，也没有从报告正文反抽。"),
    }
    if reused:
        data["note"] += "；同内容重复提交复用同一 dataset_id（未新增副本）"
    if not identity["consistent"]:
        data["note"] += ("；存储元信息与来源身份不一致（见 dataset_origin.mismatch）："
                         "消费方应读 dataset_origin 的规范值，修法在 screen_store 侧")
    return data, []


def _row_fact_ids(rows: list[dict]) -> set[str]:
    out: set[str] = set()
    for r in rows:
        for fid in (r.get("orig_fact_ids") or []):
            out.add(str(fid))
    return out


def _rows_from_materialized(materialized: list[dict]) -> tuple[list[dict], list[dict]]:
    """事实 → 筛选宽表行：按 (主体, 期末日, 币种, 口径) 分组，同组冲突不替用户选。

    - 值统一换算成**元**（`facts.to_yuan`），避免 亿元/万元 混列造成静默量级错；
    - 同一 (字段, 期间) 出现不同值/币种/口径 → 该字段整条跳过并说明，不取第一条；
    - 行带 `orig_fact_ids` / `fact_refs` / `source_url`，能一路回到事实与来源。
    """
    groups: dict[tuple, list[dict]] = {}
    for m in materialized:
        key = (m["code"], m["row_period"], m["currency"] or "unknown", m["caliber"] or "unknown")
        groups.setdefault(key, []).append(m)

    rows: list[dict] = []
    skips: list[dict] = []
    for (code, period, currency, caliber), items in sorted(groups.items()):
        by_metric: dict[str, list[dict]] = {}
        for m in items:
            by_metric.setdefault(m["metric"], []).append(m)
        # 同一行的披露日/可得日取**最晚**的一条：这一行要全部字段都可得才算可得，
        # 取最早会让没披露完的字段静默通过 PIT 闸。
        latest = max((m["disclosed_at"] for m in items if m["disclosed_at"]), default="")
        row: dict[str, Any] = {
            "entity": items[0]["entity"], "code": code, "period": period,
            "unit": "元", "currency": currency, "caliber": caliber,
            "period_type": "年报", "source_url": items[0]["source_url"],
            "disclosed_at": latest,
            "available_at": latest,
            "source_kind": "research_task_fact", "orig_fact_ids": [], "fact_refs": [],
        }
        for metric, ms in sorted(by_metric.items()):
            values = {repr(m["value_yuan"]) for m in ms}
            if len(values) > 1:
                skips.append({"fact_id": ",".join(m["fact_id"] for m in ms),
                              "metric": metric, "period": period,
                              "reason": f"同组多值冲突（{len(values)} 个不同值）："
                                        "不替用户选一条，该字段不物化"})
                continue
            if len({(m["currency"], m["caliber"]) for m in ms}) > 1:
                skips.append({"fact_id": ",".join(m["fact_id"] for m in ms),
                              "metric": metric, "period": period,
                              "reason": "币种或口径不一致：不合并成同一列"})
                continue
            row[metric] = ms[0]["value_yuan"]
            row["orig_fact_ids"].append(ms[0]["fact_id"])
            row["fact_refs"].append({"fact_id": ms[0]["fact_id"], "metric": metric,
                                     "period": ms[0]["period"], "unit": ms[0]["unit"],
                                     "raw_value": ms[0]["raw_value"],
                                     "value_yuan": ms[0]["value_yuan"],
                                     "period_basis": ms[0]["period_basis"],
                                     "source_url": ms[0]["source_url"],
                                     "locator": ms[0]["locator"],
                                     "disclosed_at": ms[0]["disclosed_at"]})
            if ms[0]["period_basis"] != "事实自带期末日":
                row["period_basis"] = ms[0]["period_basis"]
        for col in ("entity", "code", "period", "unit", "currency", "caliber",
                    "period_type", "source_url", "disclosed_at", "available_at",
                    "source_kind", "orig_fact_ids", "fact_refs"):
            if col not in row:
                row[col] = "" if col.endswith(("url", "at", "kind")) else []
        if not any(c in _AMOUNT_METRICS for c in row):
            skips.append({"fact_id": ",".join(row["orig_fact_ids"]), "period": period,
                          "reason": "该组没有任何白名单字段可写：不生成空行"})
            continue
        rows.append(row)
    return rows, skips
