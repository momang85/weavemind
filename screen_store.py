# -*- coding: utf-8 -*-
"""可解释选股：结果/预设持久化、身份哈希、作用域隔离与证据绑定（R1-NEXT-1023 R1.1）。

R1.1 关键变化（2026-10-23 架构师复核 `docs/evidence/20261023-screen-next/architect_review.json`）：

- **作用域隔离**：结果、预设、（R1.2 的）上传数据集都落在 `scope` 命名空间下；
  scope 由服务端从会话推导（`web_ui._screen_scope`），**不信任客户端自报 owner/project**。
  跨作用域读/写/列举一律 404/400，不返回别人的记录。
- **legacy 不迁移**：R1 原型的实例级目录（`workspace/screen-results`、`workspace/screen-presets`
  或环境变量覆盖）只读保留，标记 `legacy/owner=None`，仅管理员可诊断查看；归属建议给出，
  实际迁移待授权。
- **排序是展示态**：`sort` 不进 `run_id`/`result_hash`，只在读取时施加；非法 sort 明确报错。
- **完整 ID 精确匹配**：目录名必须等于请求 id；短 id 只接受唯一前缀（歧义报错），
  32 位完整 id 永不做前缀匹配（架构师探针 ③：`…2222` 命中 `…1111` 的回归）。
- **损坏结果不得复用/覆盖**：清单缺失、文件哈希不一致、身份不一致的目录 → 执行时 409
  `frozen_corrupt`，**绝不自动重写**；恢复走显式隔离流程（`quarantine`，只允许隔离损坏目录）。
- **并发一致**：先写临时目录（run.json + manifest.json 都 fsync）再整体 `os.rename`；
  竞争失败方复验已有目录，不存在"半份结果"。
- **身份契约**：`run_id = sha256(输入快照 + 规则 + 期间政策 + 实现/字段版本 + 作用域)`；
  `result_hash` 只含经济内容（快照/规则/期间政策/行），不含作用域与展示排序——
  同一内容跨作用域可比，但各自的 run 目录互不可见。
- **目录安全**：目录名 = run_id（8–64 位小写十六进制），只认持久根下的**直系子目录**；
  路径分隔符、`..`、绝对路径一律拒绝。
"""
from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time

import stock_screen as ss

RUN_SCHEMA = "weavemind.stock_screen.run/2"
MANIFEST_SCHEMA = "weavemind.stock_screen.manifest/2"
PRESET_SCHEMA = "weavemind.stock_screen.preset/2"
EVIDENCE_SCHEMA = "weavemind.stock_screen.evidence/1"
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
_PRESET_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")
_REPO = os.path.dirname(os.path.abspath(__file__))
_MAX_PRESET_NAME = 80
_MAX_POOL_CODES = 200
_MAX_PRESET_RULES = 20

DATASET_SCHEMA = "weavemind.stock_screen.dataset/1"
DATASET_MAX_ROWS = 5000
DATASET_MAX_BYTES = 2 * 1024 * 1024
_DATASET_ID_RE = re.compile(r"^[0-9a-f]{8,64}$")


class StoreWriteError(RuntimeError):
    """结果写入失败（IO/占用等瞬时错误，已重试）：不写入任何结果，也不假装成功。"""


class FrozenCorruptError(RuntimeError):
    """同一身份的历史冻结结果已损坏：不得复用，也不得自动覆盖。"""

    def __init__(self, run_id: str, integrity: str, detail: str = ""):
        self.run_id = run_id
        self.integrity = integrity or "unreadable"
        self.detail = detail
        super().__init__(
            f"同一身份的历史结果已损坏（integrity={self.integrity}）："
            "不会复用，也不会覆盖原文件")


class VersionConflict(RuntimeError):
    """并发修改同一份数据：本次写入被拒绝（不覆盖别人刚保存的内容）。"""

    def __init__(self, message: str, current: dict | None = None):
        self.current = dict(current or {})
        super().__init__(message)


# ============================================================
# 读写事务保护
#
# web_ui 用 ThreadingHTTPServer：同一进程里多个请求天然并发。原子替换只能防「半写」，
# 防不住「两个请求都先读旧值、再各自写回」——后者会静默丢掉一次更新。所以：
#   1) 进程内：按文件/目录路径取锁，包住「读 → 校验 → 改 → 写」整段，而不是只护住单次字节读写；
#   2) 跨进程：screen 基根下 `.writer.lock` 独占声明。本服务**不支持多进程共享同一目录**，
#      第二个写者必须在启动时被可靠拒绝，不能靠注释约定。
# ============================================================

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_WRITER_LOCK_FH = None
_WRITER_INFO: dict = {}


def _lock_for(path: str) -> threading.RLock:
    key = os.path.abspath(path)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def writer_info_path(base: str | None = None) -> str:
    return os.path.join(os.path.abspath(base or screen_base()), ".writer.json")


def claim_writer(base: str | None = None) -> tuple[bool, dict]:
    """跨进程独占写者声明。同进程重复调用返回已持有的信息。

    返回 `(ok, info)`；失败时 info 里有当前持有者（若可读）与原因，调用方应拒绝启动写服务。
    """
    global _WRITER_LOCK_FH, _WRITER_INFO
    if _WRITER_LOCK_FH is not None:
        return True, dict(_WRITER_INFO)
    root = os.path.abspath(base or screen_base())
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        return False, {"error": f"筛选存储目录不可用：{exc}", "path": root}
    lock_path = os.path.join(root, ".writer.lock")
    try:
        fh = open(lock_path, "a+b")
    except OSError as exc:
        return False, {"error": f"写者锁文件打不开：{exc}", "path": lock_path}
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        holder = {}
        try:
            with open(writer_info_path(root), "r", encoding="utf-8") as other:
                holder = json.load(other) or {}
        except (OSError, ValueError):
            holder = {}
        return False, {"error": "同一筛选目录已有写入进程：本服务不支持多进程共享同一目录，已拒绝第二写者",
                       "holder": holder, "path": lock_path}
    info = {"pid": os.getpid(), "started_at": _now(),
            "host": socket.gethostname(), "screen_dir": root}
    try:
        with open(writer_info_path(root), "w", encoding="utf-8") as meta:
            json.dump(info, meta, ensure_ascii=False, indent=2)
    except OSError:
        pass
    _WRITER_LOCK_FH = fh
    _WRITER_INFO = info
    return True, dict(info)


def writer_status(base: str | None = None) -> dict:
    """只读查看写者声明（诊断用：不拿锁、不改文件）。"""
    root = os.path.abspath(base or screen_base())
    info: dict = {}
    try:
        with open(writer_info_path(root), "r", encoding="utf-8") as fh:
            info = json.load(fh) or {}
    except (OSError, ValueError):
        info = {}
    return {"screen_dir": root, "declared": info,
            "claimed_by_this_process": _WRITER_LOCK_FH is not None}


# ============================================================
# 路径：作用域布局 + legacy 只读根
# ============================================================

def screen_base() -> str:
    """存储基根：环境变量覆盖，默认仓库下 `workspace/screen`（非 TEMP）。"""
    raw = str(os.environ.get("WEAVEMIND_SCREEN_DIR") or "").strip()
    if raw:
        return os.path.abspath(raw)
    return os.path.join(_REPO, "workspace", "screen")


def default_cache_path() -> str:
    """官方披露缓存路径：环境变量覆盖，默认仓库下 `data/screen_cache/official_disclosure_cache.json`。

    首次启动（试用包内没有官方缓存）时回退到**包内合成样例**
    `data/samples/synthetic_5_companies.json`：让默认路径可用，且 provenance 仍是
    「合成演示、非官方」（`load_source` 的 official 判定要求非 synthetic，样例自带
    synthetic=true，不会被标成官方数据）。
    """
    raw = str(os.environ.get("WEAVEMIND_SCREEN_CACHE") or "").strip()
    if raw:
        return os.path.abspath(raw)
    official = os.path.join(_REPO, "data", "screen_cache", "official_disclosure_cache.json")
    if os.path.isfile(official):
        return official
    sample = os.path.join(_REPO, "data", "samples", "synthetic_5_companies.json")
    if os.path.isfile(sample):
        return sample
    return official


def normalize_scope(scope) -> dict:
    """规范化作用域：服务端会话推导后传入；user 必填，project 缺省 personal。"""
    if not isinstance(scope, dict) or not str(scope.get("user") or "").strip():
        raise ValueError("缺 scope.user：结果/预设必须落在服务端推导的用户作用域内")
    user = str(scope["user"]).strip()
    project = str(scope.get("project") or "personal").strip() or "personal"
    if any(ch in user + project for ch in "/\\:*?\"<>|") or "\x00" in user + project:
        raise ValueError("scope 含非法字符")
    return {"user": user, "project": project}


def scope_key(scope) -> str:
    """作用域 → 不透明目录名（稳定、不泄漏明文用户名；scope.json 留管理员可读的映射）。"""
    norm = normalize_scope(scope)
    return (f"u-{hashlib.sha256(norm['user'].encode()).hexdigest()[:16]}"
            f"-p-{hashlib.sha256(norm['project'].encode()).hexdigest()[:12]}")


def _scoped_dir(scope, kind: str) -> str:
    root = os.path.join(screen_base(), "scopes", scope_key(scope), kind)
    return root


def runs_root(scope) -> str:
    return _scoped_dir(scope, "runs")


def presets_root(scope) -> str:
    return _scoped_dir(scope, "presets")


def datasets_root(scope) -> str:
    """（R1.2 预留）用户上传数据集的作用域根。"""
    return _scoped_dir(scope, "datasets")


def legacy_runs_root() -> str:
    raw = str(os.environ.get("WEAVEMIND_SCREEN_LEGACY_RUNS") or "").strip()
    if raw:
        return os.path.abspath(raw)
    return os.path.join(_REPO, "workspace", "screen-results")


def legacy_presets_root() -> str:
    raw = str(os.environ.get("WEAVEMIND_SCREEN_LEGACY_PRESETS") or "").strip()
    if raw:
        return os.path.abspath(raw)
    return os.path.join(_REPO, "workspace", "screen-presets")


def _now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


# ============================================================
# 身份
# ============================================================

def run_identity(snapshot_id: str, rules_hash: str, period_policy: dict,
                 scope: str) -> str:
    """一次执行的身份：快照 + 规则 + 期间政策 + 实现/字段版本 + **作用域**。

    - 作用域进身份：防止把别人作用域里的结果目录整目录搬过来冒充自己的历史；
    - 排序不在身份里（展示态）；as_of/输入哈希在 `snapshot_id` 里。
    """
    blob = json.dumps({
        "schema": RUN_SCHEMA, "impl": ss.IMPL_VERSION,
        "field_map_version": ss.FIELD_MAP_VERSION,
        "snapshot_id": snapshot_id, "rules_hash": rules_hash,
        "period_policy": dict(period_policy or {}),
        "scope": scope,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ============================================================
# 输入来源：官方缓存（R1.2 起另有用户上传数据集）
# ============================================================

_PROVENANCE_KINDS = {
    "official_original": "官方原文",
    "official_cache_materialized": "内置官方缓存物化（未核验原件）",
    "adapter_collected": "适配器取数（未核验）",
    "adapter_normalized": "适配器整理的宽表",
    "user_provided": "用户提供",
    "research_material": "本人研究材料（任务事实投影）",
    "synthetic_demo": "合成演示",
    "unknown": "来源未标注",
}


def _provenance(*, kind: str, file_sha256: str, origin_ref: str = "",
                source_urls=(), note: str = "") -> dict:
    """来源关系与核验状态——**性质、文件哈希、人工复核三件事分开**。

    - `kind` 说明这份数据从哪来（原文 / 适配器整理 / 用户提供 / 合成演示）；
    - `file_sha256` 只证明「这份文件没被换过」，**不是原文已核验**，也不是签名；
    - `human_review` 只能由人在复核流程里写，本模块任何路径都不会把它置为已复核。
    """
    kind = str(kind or "unknown")
    if kind not in _PROVENANCE_KINDS:
        kind = "unknown"
    return {
        "kind": kind, "kind_label": _PROVENANCE_KINDS[kind],
        "file_sha256": str(file_sha256 or ""),
        "origin_ref": str(origin_ref or ""),
        "source_urls": sorted({str(u) for u in (source_urls or ()) if str(u).strip()})[:50],
        "original_verified": False,
        "human_review": {"status": "not_reviewed", "by": None, "at": None},
        "note": note or "文件哈希只证明这份文件未变；来源性质与人工复核状态是两件事。",
    }


def load_source(path: str | None = None) -> tuple[dict, list[str], dict]:
    """读输入源 → `(载荷, 错误, 来源身份)`；来源身份记录**文件字节哈希**（进快照身份）。"""
    p = str(path or default_cache_path())
    payload, errors = ss.load_payload_file(p)
    meta_raw = (payload.get("metadata") or {}) if isinstance(payload, dict) else {}
    official = (os.path.abspath(p) == os.path.abspath(default_cache_path())
                and not meta_raw.get("synthetic")
                and not meta_raw.get("user_provided"))
    urls = sorted({str(r.get("source_url") or "").strip()
                   for r in (payload.get("rows") or []) if str(r.get("source_url") or "").strip()})
    meta = {
        "label": str(meta_raw.get("source") or os.path.basename(p) or "screen_input"),
        "sha256": ss.payload_sha256(p),
        "row_count": len(payload.get("rows") or []) if isinstance(payload, dict) else 0,
        "synthetic": bool(meta_raw.get("synthetic")),
        "user_provided": bool(meta_raw.get("user_provided")),
        "origin": str(meta_raw.get("provenance", {}).get("origin_file") or ""),
        # 官方身份**只由服务端判定**（不读文件里的自我声明）：
        # 必须是服务端配置的官方缓存路径，且不是合成夹具、不是用户上传。
        "official": official,
        "provenance": _provenance(
            kind=("synthetic_demo" if meta_raw.get("synthetic")
                  else "user_provided" if meta_raw.get("user_provided")
                  else "adapter_normalized" if official else "unknown"),
            file_sha256=ss.payload_sha256(p),
            origin_ref=str(meta_raw.get("provenance", {}).get("origin_file") or os.path.basename(p)),
            source_urls=urls,
            note=("官方缓存是**整理后的宽表**：文件哈希只证明这份文件没变，"
                  "不等于每一行都回到原文核验过。原文链接见行的 source_url。"
                  if official else ""),
        ),
    }
    if errors:
        return {}, errors, meta
    return payload, [], meta


def available_pool(payload: dict) -> list[dict]:
    """输入源里**真实可用**的主体（表单候选来自数据本身，不预置公司清单）。"""
    from facts import market_of_code
    groups: dict[str, list[dict]] = {}
    for row in payload.get("rows") or []:
        code = str(row.get("code") or row.get("entity_id") or "").strip().upper()
        if not code:
            continue
        market, ambiguous = market_of_code(code)
        if ambiguous or market != "cn":
            continue                      # 界外主体不进候选池（与快照规则一致）
        groups.setdefault(code, []).append(row)
    out: list[dict] = []
    for code in sorted(groups):
        rows = groups[code]
        periods = sorted({str(r.get("period") or "")[:10] for r in rows if r.get("period")})
        avails = sorted({str(r.get("available_at") or r.get("disclosed_at") or "")[:10]
                         for r in rows if r.get("available_at") or r.get("disclosed_at")})
        out.append({
            "code": code, "entity": str(rows[0].get("entity") or code),
            "rows": len(rows), "periods": periods,
            "first_available": avails[0] if avails else "",
            "last_available": avails[-1] if avails else "",
            "pending_rows": sum(1 for r in rows
                                if not (r.get("available_at") or r.get("disclosed_at"))),
        })
    return out


def pool_catalog(source_path: str | None = None) -> tuple[dict, list[str]]:
    """选股表单的股票池目录：来源身份 + 输入里真实可用的主体。"""
    payload, errors, meta = load_source(source_path)
    if errors:
        return {}, errors
    return {"status": "ok", "source": meta,
            "available_pool": available_pool(payload)}, []


# ============================================================
# R1.2 数据入口：模板下载 / 用户上传数据集（三类来源可区分）
# ============================================================
#
# 设计约束（派单 R1.2 + 产品原则 §2 数据一体化体验）：
# - 普通用户从 Excel/CSV 起步，不要求先懂 JSON 或内部快照结构；
# - 上传不经过任何联网取样、不调用模型、不产生付费调用；
# - **上传永远不是官方数据**：`official` 只由服务端按「配置的官方缓存路径 + 非合成」
#   判定，上传侧没有任何字段可以声明官方；CSV 里也没有这种列（结构上不可自封）；
# - 与官方缓存共用同一 wide 行契约（`ss.validate_payload`），进快照时同样做 PIT 判定。

CSV_COLUMNS = (
    "entity", "code", "period", "unit", "currency", "caliber", "period_type",
    "source_url", "available_at", "disclosed_at", *ss.AMOUNT_METRICS,
)
_CSV_REQUIRED = ("entity", "code", "period")
_TEMPLATE_ROWS = (
    {"entity": "示例公司甲（示例行，请删除）", "code": "000000.SZ", "period": "2024-12-31",
     "unit": "元", "currency": "CNY", "caliber": "合并", "period_type": "年报",
     "source_url": "https://example.com/示例年报.pdf",
     "available_at": "2025-04-20", "disclosed_at": "2025-04-20",
     "revenue": 10000000000, "gross_profit": 3000000000, "net_profit": 800000000,
     "net_profit_consolidated": 850000000, "total_assets": 20000000000,
     "total_liabilities": 9000000000, "operating_cashflow": 1200000000},
    {"entity": "示例公司乙（示例行，请删除）", "code": "000000.SH", "period": "2023-12-31",
     "unit": "元", "currency": "CNY", "caliber": "合并", "period_type": "年报",
     "source_url": "https://example.com/示例年报-2023.pdf",
     "available_at": "2024-04-19", "disclosed_at": "2024-04-19",
     "revenue": 8000000000, "gross_profit": 2000000000, "net_profit": 500000000,
     "net_profit_consolidated": 520000000, "total_assets": 15000000000,
     "total_liabilities": 7000000000, "operating_cashflow": 900000000},
)


def template_csv() -> str:
    """模板 CSV 文本：表头 + 两行**示例**（示例代码不存在，不会被当成真实主体做结论）。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
    writer.writeheader()
    for row in _TEMPLATE_ROWS:
        writer.writerow(row)
    return buf.getvalue()


def parse_dataset_csv(text: str, *, filename: str = "") -> tuple[dict, list[str]]:
    """上传 CSV → `(载荷, 错误)`。错误按**文件行号**报，不静默跳过、不猜单位/口径。"""
    if not isinstance(text, str) or not text.strip():
        return {}, ["上传内容为空：请先下载模板，至少填写一行数据后重新上传"]
    raw = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(raw))
    header = [str(h or "").strip() for h in (reader.fieldnames or [])]
    if not header or not any(header):
        return {}, ["没有表头：第一行必须是字段名（可直接从模板复制）"]
    missing = [c for c in _CSV_REQUIRED if c not in header]
    if missing:
        return {}, [f"表头缺必填列：{', '.join(missing)}（必填 entity/code/period）"]
    unknown = [h for h in header if h and h not in CSV_COLUMNS]
    if unknown:
        return {}, [f"未知列：{', '.join(unknown)}"
                    "（不静默忽略：这些列会改变筛选含义，请删除或改用模板表头）"]
    rows: list[dict] = []
    errors: list[str] = []
    for lineno, raw_row in enumerate(reader, start=2):
        row = {str(k).strip(): ("" if v is None else str(v).strip())
               for k, v in raw_row.items() if k is not None}
        if not any(row.values()):
            continue
        label = f"第 {lineno} 行"
        for key, hint in (("entity", "主体名"), ("code", "股票代码"), ("period", "期末日 YYYY-MM-DD")):
            if not row.get(key):
                errors.append(f"{label} 缺 {key}（{hint}）")
        for metric in ss.AMOUNT_METRICS:
            value = row.get(metric, "")
            if value == "":
                continue
            cleaned = value.replace(",", "").replace("，", "").replace("　", "").strip()
            try:
                row[metric] = float(cleaned)
            except ValueError:
                errors.append(f"{label} {metric} 不是数：「{value}」")
        rows.append(row)
    if errors:
        return {}, errors
    if not rows:
        return {}, ["没有数据行：至少需要一行真实数据（模板里的示例行不算）"]
    # 内容身份只含数据本身：文件名/命名不进哈希（同一份数据换个文件名不算新数据集），
    # 原始文件名记在数据集 meta 里供人工核对。
    payload = {
        "metadata": {
            "source": "user_upload", "synthetic": False, "user_provided": True,
            "provenance": {
                "columns": header,
                "note": "用户上传：与官方披露缓存区分，不可标记为官方数据",
            },
        },
        "rows": rows,
    }
    validated, verrors = ss.validate_payload(payload)
    if verrors:
        return {}, verrors
    payload["rows"] = validated
    return payload, []


def _dataset_meta_path(directory: str) -> str:
    return os.path.join(directory, "meta.json")


def _dataset_rows_path(directory: str) -> str:
    return os.path.join(directory, "rows.json")


def _persist_dataset(*, scope, title: str, filename: str, payload: dict,
                     extra_meta: dict | None = None) -> tuple[dict, bool]:
    """把**已校验**的载荷写成内容定址数据集目录（同内容复用；不覆盖被改动的目录）。"""
    norm = normalize_scope(scope)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    did = digest[:32]
    rows = payload.get("rows") or []
    meta = {
        "schema": DATASET_SCHEMA, "dataset_id": did, "name": title,
        "created_at": _now(), "scope": dict(norm),
        "filename": str(filename or "")[:120],
        "sha256": digest, "bytes": len(body), "row_count": len(rows),
        "codes": sorted({str(r.get("code") or "") for r in rows if r.get("code")}),
        "periods": sorted({str(r.get("period") or "")[:10] for r in rows if r.get("period")}),
        "columns": list(CSV_COLUMNS),
        "source_urls": sorted({str(r.get("source_url") or "").strip()
                               for r in rows if str(r.get("source_url") or "").strip()})[:50],
        "official": False, "user_provided": True, "synthetic": False,
    }
    if extra_meta:
        meta.update(extra_meta)
    root = datasets_root(norm)
    target = os.path.join(root, did)
    last_err: Exception | None = None
    lock = _lock_for(root)                       # 同内容并发写入：串行化，避免同 ID 目录竞争
    with lock:
        for attempt in range(6):
            if os.path.isdir(target):
                old_meta = _read_dataset_meta(target)
                if not old_meta:
                    raise StoreWriteError(
                        "同内容数据集目录已存在但不可读：请修复或移走该目录后重试（不会覆盖）")
                if old_meta.get("sha256") != digest:
                    raise StoreWriteError(
                        "同 ID 数据集内容不一致（目录被改动）：不会覆盖；请确认来源后再上传")
                # RC2-F5：复用前复验**实际** rows.json 内容、scope 与 schema——
                # 只看 meta.sha256 会被「meta 没变、文件被改」骗过（架构师探针 F5）。
                rows_path = _dataset_rows_path(target)
                try:
                    with open(rows_path, "rb") as fh:
                        actual = fh.read()
                except OSError as exc:
                    raise StoreWriteError(
                        f"同 ID 数据集 rows.json 不可读（{exc}）：不当作成功复用，"
                        "也不重写原文件；请显式修复或移走该目录后重试")
                if hashlib.sha256(actual).hexdigest() != digest or len(actual) != len(body):
                    raise StoreWriteError(
                        "同 ID 数据集实际内容与元信息不一致（rows.json 哈希不符，可能损坏）："
                        "损坏数据不当作成功复用，也不重写原文件；请显式修复或删除后重试")
                if old_meta.get("scope") != dict(norm) or old_meta.get("schema") != DATASET_SCHEMA:
                    raise StoreWriteError(
                        "同 ID 数据集元信息 scope/schema 与本次不符：不跨作用域复用，也不覆盖")
                return old_meta, True
            tmp = os.path.join(root, f".tmp-dataset-{did}-{os.getpid()}-{attempt}")
            try:
                os.makedirs(tmp, exist_ok=False)
                with open(_dataset_rows_path(tmp), "wb") as fh:
                    fh.write(body)
                    fh.flush()
                    os.fsync(fh.fileno())
                with open(_dataset_meta_path(tmp), "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, ensure_ascii=False, indent=2, sort_keys=True)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.rename(tmp, target)
                return meta, False
            except OSError as exc:                    # 竞争 / 占用：退避重试，不静默失败
                last_err = exc
                shutil.rmtree(tmp, ignore_errors=True)
                time.sleep(0.05 * (attempt + 1))
    raise StoreWriteError(f"数据集写入失败（目录仍未被创建，未写入任何数据集）：{last_err}")


def save_dataset(*, scope, name, filename, text,
                 max_rows: int = DATASET_MAX_ROWS) -> tuple[dict | None, bool, list[str]]:
    """保存一份用户上传数据集 → `(meta, reused, 错误)`；同内容复用同一 `dataset_id`。"""
    try:
        norm = normalize_scope(scope)
    except ValueError as exc:
        return None, False, [str(exc)]
    title = str(name or "").strip() or str(filename or "").strip() or "未命名数据集"
    if len(title) > _MAX_PRESET_NAME:
        return None, False, [f"数据集名称最多 {_MAX_PRESET_NAME} 字"]
    blob_in = str(text or "")
    if len(blob_in.encode("utf-8")) > DATASET_MAX_BYTES:
        return None, False, [f"上传内容超过 {DATASET_MAX_BYTES // (1024 * 1024)} MB：请拆成多份数据集"]
    payload, errors = parse_dataset_csv(blob_in, filename=filename)
    if errors:
        return None, False, errors
    rows = payload["rows"]
    if len(rows) > max_rows:
        return None, False, [f"行数 {len(rows)} 超过上限 {max_rows}：请按股票池或年度拆分上传（不静默截断）"]
    payload["metadata"] = {**dict(payload.get("metadata") or {}),
                           "note": "用户上传：与官方披露缓存区分，不可标记为官方数据"}
    try:
        meta, reused = _persist_dataset(
            scope=norm, title=title, filename=filename, payload=payload,
            extra_meta={"official": False, "user_provided": True, "synthetic": False,
                        "note": "用户上传数据集：只在本作用域可见；不是官方披露缓存，"
                                "不能用于宣称官方数据"})
    except StoreWriteError as exc:
        # 上传路径保持「返回错误」语义（不抛异常），与既有 API 行为一致
        text = str(exc)
        if "已存在但不可读" in text:
            return None, False, [
                "同 ID 数据集目录已存在但不可读：数据集按**内容**定 ID，换名称不会得到新 ID；"
                "请修复该目录（meta.json 损坏）或联系管理员把它移走后重试"]
        if "内容不一致" in text:
            return None, False, [
                "同 ID 数据集内容不一致（目录被改动）：不会覆盖，换名称也仍是同一 ID；"
                "请联系管理员把被改动的目录移走，或确认改动来源后再上传"]
        return None, False, [text]
    return meta, reused, []


def save_dataset_from_source(*, scope, name, rows, provenance: dict,
                             max_rows: int = DATASET_MAX_ROWS) -> tuple[dict | None, bool, list[str]]:
    """把**服务端已核验来源**的行写成作用域数据集（内置官方缓存 / 适配器取数）。

    与 `save_dataset` 的区别：这里的 `official`/`user_provided` 由服务端按来源判定，
    用户不可声明；写入仍是内容定址 + 原子重命名，失败不静默。
    """
    try:
        norm = normalize_scope(scope)
    except ValueError as exc:
        return None, False, [str(exc)]
    title = str(name or "").strip() or "来源数据集"
    # G5：四个布尔由**服务端来源判定**写进存储元信息，不再固定 user_provided=False
    # （研究材料投影 user_provided=True/official=False；官方缓存 official=True）。
    prov = dict(provenance or {})
    user_provided = bool(prov.get("user_provided"))
    official = bool(prov.get("official")) and not user_provided
    synthetic = bool(prov.get("synthetic"))
    payload = {"metadata": {"source": prov.get("label") or "screen_source",
                            "synthetic": synthetic,
                            "user_provided": user_provided,
                            "provenance": prov},
               "rows": list(rows or [])}
    validated, errors = ss.validate_payload(payload)
    if errors:
        return None, False, [f"来源行不符合宽表契约：{e}" for e in errors]
    if not validated:
        return None, False, ["来源没有可用行：不生成空数据集"]
    if len(validated) > max_rows:
        return None, False, [f"行数 {len(validated)} 超过上限 {max_rows}：请缩小公司/期间范围"]
    payload["rows"] = validated
    try:
        meta, reused = _persist_dataset(
            scope=norm, title=title[:80], filename="", payload=payload,
            extra_meta={"official": official,
                        "user_provided": user_provided,
                        "synthetic": synthetic,
                        "independent_verification": bool(prov.get("independent_verification")),
                        "provenance_kind": str(prov.get("kind") or "unknown"),
                        "provenance_label": str(prov.get("kind_label") or ""),
                        "acquisition": str(prov.get("acquisition") or "")[:200],
                        "origin": str(prov.get("origin") or "")[:200],
                        "parent_task_id": str(prov.get("parent_task_id") or "")[:120],
                        "parent_task_version": str(prov.get("parent_task_version") or "")[:120],
                        "working_paper_ref": prov.get("working_paper_ref") or None,
                        "paper_sha256": str(prov.get("paper_sha256") or ""),
                        "provenance": prov,
                        "note": str(prov.get("note") or "")[:400]})
    except StoreWriteError:
        raise
    return meta, reused, []


def _origin_identity(meta: dict) -> dict:
    """数据集元信息 → research_materials.origin_identity 归一（懒导入避免模块环）。

    旧数据（kind=user_provided + parent_task_id）只读归一为 research_material，
    **不重写冻结内容**；不一致（consistent=False）原样透出，不静默挑一个当真。
    """
    try:
        import research_materials as _rm
    except Exception:
        return {}
    prov = dict(meta.get("provenance") or {})
    # 旧数据集可能只存了布尔（或什么都没存）：先用存储元信息补齐，再交给统一归一，
    # 免得老上传被当成 unknown（G5 兼容读，不重写冻结内容）。
    for key in ("user_provided", "official", "synthetic", "independent_verification"):
        if key not in prov and key in meta:
            prov[key] = bool(meta.get(key))
    if not str(prov.get("kind") or meta.get("provenance_kind") or ""):
        prov["kind"] = ("official_cache_materialized" if prov.get("official")
                        else "research_material" if str(meta.get("parent_task_id") or "")
                        else "user_provided")
    try:
        return _rm.origin_identity(meta=meta, provenance=prov)
    except Exception:
        return {}


def _read_dataset_meta(directory: str) -> dict:
    try:
        with open(_dataset_meta_path(directory), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return meta if isinstance(meta, dict) else {}


def list_datasets(scope, limit: int = 50) -> list[dict]:
    """列出本作用域的数据集摘要（新到旧）；损坏条目带 `usable=False`，不隐藏。"""
    root = datasets_root(normalize_scope(scope))
    if not os.path.isdir(root):
        return []
    out: list[dict] = []
    for rid in sorted(os.listdir(root), reverse=True):
        if not _DATASET_ID_RE.match(rid):
            continue
        directory = os.path.join(root, rid)
        if not os.path.isdir(directory):
            continue
        meta = _read_dataset_meta(directory)
        if not meta:
            out.append({"dataset_id": rid, "name": "（不可读）", "usable": False,
                        "integrity": "meta_unreadable"})
            continue
        out.append({
            "dataset_id": meta.get("dataset_id") or rid, "name": meta.get("name") or rid,
            "created_at": meta.get("created_at"), "row_count": meta.get("row_count"),
            "codes": meta.get("codes") or [], "periods": meta.get("periods") or [],
            "filename": meta.get("filename") or "",
            "official": bool(meta.get("official")) and not bool(meta.get("user_provided")),
            "user_provided": bool(meta.get("user_provided")),
            "synthetic": bool(meta.get("synthetic")),
            "independent_verification": bool(meta.get("independent_verification")),
            "parent_task_id": str(meta.get("parent_task_id") or ""),
            "provenance_kind": str(meta.get("provenance_kind") or ""),
            "source_urls": meta.get("source_urls") or [],
            "usable": True,
        })
        if len(out) >= limit:
            break
    return out


def load_dataset(scope, token: str) -> tuple[dict, dict, str]:
    """按 `dataset_id`（完整或唯一短前缀）读本作用域数据集 → `(meta, 载荷, 错误文本)`。"""
    root = datasets_root(normalize_scope(scope))
    tid = str(token or "").strip().lower()
    if not _DATASET_ID_RE.match(tid):
        return {}, {}, "数据集 ID 不合法（应为 8–64 位小写十六进制）"
    rid = tid
    if not os.path.isdir(os.path.join(root, rid)):
        if len(tid) >= 32:
            return {}, {}, "数据集不存在（完整 ID 精确匹配：不做前缀命中）"
        names = [n for n in (os.listdir(root) if os.path.isdir(root) else [])
                 if _DATASET_ID_RE.match(n) and n.startswith(tid)]
        if not names:
            return {}, {}, "数据集不存在"
        if len(names) > 1:
            return {}, {}, f"数据集 ID 前缀有 {len(names)} 个匹配：请提供更长的 ID"
        rid = names[0]
    directory = os.path.join(root, rid)
    meta = _read_dataset_meta(directory)
    if not meta:
        return {}, {}, "数据集元信息不可读：该数据集已损坏，不会用于筛选"
    norm = normalize_scope(scope)
    if dict(meta.get("scope") or {}) != norm:
        return {}, {}, "数据集不存在"                      # 跨作用域一律 404，不泄露存在性
    try:
        with open(_dataset_rows_path(directory), "rb") as fh:
            body = fh.read()
    except OSError:
        return {}, {}, "数据集内容不可读：该数据集已损坏，不会用于筛选"
    if hashlib.sha256(body).hexdigest() != meta.get("sha256"):
        return {}, {}, "数据集内容与登记哈希不一致（文件被改动）：不会用于筛选"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}, {}, "数据集内容不是有效 JSON：不会用于筛选"
    rows, errors = ss.validate_payload(payload)
    if errors:
        return {}, {}, "；".join(errors)
    payload["rows"] = rows
    return meta, payload, ""


def dataset_source_meta(meta: dict) -> dict:
    """数据集 → 快照来源身份。

    `official` 只认**服务端物化**写进清单的标记（`save_dataset_from_source`）；
    上传路径 `save_dataset` 恒定写 `official=False`，用户无法声明官方身份。
    """
    identity = _origin_identity(meta)
    official = bool(identity.get("official")) if identity else (
        bool(meta.get("official")) and not bool(meta.get("user_provided")))
    user_provided = bool(identity.get("user_provided")) if identity else not official
    kind = str(identity.get("kind") or meta.get("provenance_kind") or
               ("official_cache_materialized" if official else "user_provided"))
    if kind not in _PROVENANCE_KINDS:
        kind = "official_cache_materialized" if official else "unknown"
    if official:
        label = f"官方缓存数据集：{meta.get('name') or meta.get('dataset_id')}"
        note = ("由服务端从内置官方披露缓存物化的数据集：行内带原文链接；"
                "内容指纹只证明这份数据集没变，不等于每行回到原文核验过（人工复核未做）。")
    elif kind == "research_material":
        label = f"本人研究材料数据集：{meta.get('name') or meta.get('dataset_id')}"
        note = ("由本人研究任务的事实投影物化：绑定任务/底稿身份；"
                "不是官方数据，也不表示已独立核验（人工复核未做）。")
    else:
        label = f"用户数据集：{meta.get('name') or meta.get('dataset_id')}"
        note = "用户上传：内容指纹只证明这份文件没变；不构成原文核验或官方数据。"
    return {
        "label": label,
        "sha256": str(meta.get("sha256") or ""), "row_count": int(meta.get("row_count") or 0),
        "synthetic": bool(meta.get("synthetic")), "user_provided": user_provided,
        "official": official,
        "origin_identity": identity or None,
        "dataset_id": meta.get("dataset_id") or "", "name": meta.get("name") or "",
        "origin": str(meta.get("filename") or ""), "origin_file": str(meta.get("filename") or ""),
        "provenance": _provenance(
            kind=kind, file_sha256=str(meta.get("sha256") or ""),
            origin_ref=str(meta.get("filename") or ""),
            source_urls=meta.get("source_urls") or (),
            note=note,
        ),
    }

def _resolve_input(scope, dataset_id, source_path: str | None = None) -> tuple[dict, list[str], dict]:
    """筛选输入：给了 `dataset_id` 用本作用域数据集，否则用服务端配置的官方缓存。"""
    did = str(dataset_id or "").strip()
    if not did:
        return load_source(source_path)
    meta, payload, error = load_dataset(scope, did)
    if error:
        return {}, [error], {}
    return payload, [], dataset_source_meta(meta)


# ============================================================
# 预览 / 执行
# ============================================================

def preview(*, scope, pool, as_of: str, rules_raw=None, period_policy_raw=None,
            source_path: str | None = None,
            dataset_id: str | None = None) -> tuple[dict, list[str]]:
    """输入预览：可用性、PIT 排除/待核验、规则与期间政策校验；**不执行、不落结果**。"""
    payload, errors, meta = _resolve_input(scope, dataset_id, source_path)
    if errors:
        return {}, errors
    policy, policy_errors = ss.validate_period_policy(period_policy_raw)
    if policy_errors:
        return {}, policy_errors
    rules, rule_errors = (), []
    if rules_raw is not None:
        rules, rule_errors = ss.parse_rules(rules_raw)
        if rule_errors:
            return {}, rule_errors
    snap, snap_errors = ss.build_snapshot(
        payload, pool=pool, as_of=as_of, source_label=meta["label"],
        source_sha256=meta["sha256"], synthetic=meta["synthetic"],
        user_provided=meta["user_provided"])
    if snap_errors:
        return {}, snap_errors
    in_cache = {row["code"] for row in available_pool(payload)}
    wanted = [str(c or "").strip().upper() for c in (pool or []) if str(c or "").strip()]
    missing = [c for c in sorted(set(wanted)) if c not in in_cache]
    ready = [e for e in snap.entities if e["dataset"].manifest.periods]
    common = ss.common_periods(snap)
    per_entity = {e["entity_id"]: ss.annual_periods_of(e["dataset"])
                  for e in snap.entities}
    desc = ss.describe_snapshot(snap)
    desc.update({
        "status": "ok",
        "scope": dict(normalize_scope(scope)),
        "source": meta,
        "available_pool": available_pool(payload),
        "missing_pool_codes": missing,
        "entities_ready": len(ready),
        "entities_without_periods": len(snap.entities) - len(ready),
        "rules": [r.as_dict() for r in rules],
        "rules_errors": rule_errors,
        "period_policy": policy,
        "common_periods": common,
        # 与 evaluate 的 uniform 同一算法：池内最新可用年度（≥ 共同年度），
        # 让预览里的「默认统一报告期」和实际执行的统一报告期永远一致。
        "suggested_report_period": max((pers[-1] for pers in per_entity.values() if pers),
                                       default=""),
        "per_entity_periods": per_entity,
        "field_map_version": ss.FIELD_MAP_VERSION,
        "impl_version": ss.IMPL_VERSION,
        "schema": ss.SCHEMA_VERSION,
    })
    return desc, []


def execute(*, scope, pool, as_of: str, rules_raw, period_policy_raw=None,
            sort_raw=None, source_path: str | None = None,
            dataset_id: str | None = None,
            store: bool = True) -> tuple[dict | None, list[str]]:
    """执行一次筛选并落**不可变结果**；同一身份重复执行复用（损坏则 409，不覆盖）。

    - `sort_raw` 是展示态：只影响返回行序，不进身份、不改哈希；
    - `period_policy_raw` 进身份与哈希；
    - 损坏目录 → `FrozenCorruptError`（HTTP 层转 409，绝不自动重写）。
    """
    try:
        norm_scope = normalize_scope(scope)
    except ValueError as exc:
        return None, [str(exc)]
    sort, sort_errors = ss.validate_sort(sort_raw)
    if sort_errors:
        return None, sort_errors
    payload, errors, meta = _resolve_input(scope, dataset_id, source_path)
    if errors:
        return None, errors
    rules, rule_errors = ss.parse_rules(rules_raw or [])
    if rule_errors:
        return None, rule_errors
    policy, policy_errors = ss.validate_period_policy(period_policy_raw)
    if policy_errors:
        return None, policy_errors
    snap, snap_errors = ss.build_snapshot(
        payload, pool=pool, as_of=as_of, source_label=meta["label"],
        source_sha256=meta["sha256"], synthetic=meta["synthetic"],
        user_provided=meta["user_provided"])
    if snap_errors:
        return None, snap_errors
    try:
        result = ss.evaluate(snap, rules, period_policy=policy)
    except ss.PeriodPolicyError as exc:
        return None, exc.errors
    run_id = run_identity(snap.snapshot_id, result.rules_hash, policy, scope_key(norm_scope))
    run = {
        "schema": RUN_SCHEMA, "impl_version": ss.IMPL_VERSION,
        "field_map_version": ss.FIELD_MAP_VERSION,
        "run_id": run_id, "created_at": _now(),
        "scope": dict(norm_scope),
        "as_of": snap.as_of, "pool": list(snap.pool),
        "rules": [r.as_dict() for r in result.rules],
        "rules_hash": result.rules_hash, "snapshot_id": snap.snapshot_id,
        "result_hash": result.result_hash,
        "period_policy": dict(policy), "report_period": result.report_period,
        "comparable": result.comparable,
        "counts": result.counts, "notes": list(result.notes),
        "source": dict(meta),
        "entities": _entities_payload(snap),
        "excluded": [dict(x) for x in snap.excluded],
        # 行存**规范序**（按代码）；展示排序在读取时施加
        "rows": {"pass": [r.as_dict() for r in result.pass_rows],
                 "fail": [r.as_dict() for r in result.fail_rows],
                 "insufficient": [r.as_dict() for r in result.insufficient_rows]},
    }
    if not store:
        return apply_view(run, sort, reused=False), []
    stored, reused = save_run(run, norm_scope)
    stored = dict(stored)
    stored["reused"] = reused
    return apply_view(stored, sort, reused), []


def apply_view(run: dict, sort: dict | None, reused: bool | None = None) -> dict:
    """施加展示排序并标注 `view`（不改 run.json 的任何冻结字段）。"""
    out = dict(run)
    rows = out.get("rows") or {}
    ordered = {k: ss.sort_rows(rows.get(k) or [], sort) for k in ("pass", "fail", "insufficient")}
    out["rows"] = ordered
    out["view"] = {"sort": dict(sort) if sort else None,
                   "applied": bool(sort),
                   "note": "排序只影响展示顺序，不改变结果身份与哈希"}
    if reused is not None:
        out["reused"] = reused
    return out


def _entities_payload(snapshot) -> list[dict]:
    """快照 → 结果文件里的主体证据段（观察全量，读数靠 fact_id 在这里解析）。"""
    out: list[dict] = []
    for ent in snapshot.entities:
        ds = ent["dataset"]
        out.append({
            "entity": ent["entity"], "entity_id": ent["entity_id"],
            "dataset_hash": ds.dataset_hash,
            "periods": list(ds.manifest.periods),
            "caliber": ds.manifest.caliber,
            "gaps": list(ds.manifest.gaps),
            "conflicts": list(ds.manifest.conflicts),
            "excluded": [dict(x) for x in ent["excluded"]],
            "pending_pit": [dict(x) for x in ent["pending_pit"]],
            "observations": [o.as_dict() for o in ds.observations],
        })
    return out


# ============================================================
# 定位：完整 ID 精确匹配 + 唯一前缀（歧义报错）
# ============================================================

def resolve_run_id(scope, token: str, *, root: str | None = None) -> tuple[str | None, dict | None]:
    """把请求 id 解析到**本作用域内**的结果目录名。

    - 32 位完整 id：只做**整名精确匹配**（架构师探针 ③ 的修复：绝不截断再前缀匹配）；
    - 短 id（8–31 位）：唯一前缀才接受，多个匹配 → `ambiguous`（列出数量，不猜）；
    - 非法字符 → `invalid_id`。
    """
    rid = str(token or "").strip().lower()
    if not _RUN_ID_RE.match(rid):
        return None, {"code": "invalid_id",
                      "error": "run_id 非法：只接受 8–64 位小写十六进制"}
    base = root or runs_root(scope)
    try:
        names = [n for n in os.listdir(base)
                 if _RUN_ID_RE.match(n) and os.path.isdir(os.path.join(base, n))]
    except OSError:
        names = []
    if rid in names:
        return rid, None
    if len(rid) >= 32:
        # 完整 id 不存在就是不存在：不做任何前缀/截断匹配
        return None, {"code": "not_found", "error": "结果不存在或不可读",
                      "run_id": rid}
    matches = sorted(n for n in names if n.startswith(rid))
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, {"code": "ambiguous",
                      "error": f"前缀「{rid}」匹配到 {len(matches)} 份结果：请给出更长的 id "
                               f"（如 {matches[0][:20]}…）",
                      "matches": len(matches)}
    return None, {"code": "not_found", "error": "结果不存在或不可读", "run_id": rid}


# ============================================================
# 读：完整性分层（ok / manifest_missing / hash_mismatch / identity_mismatch / unreadable）
# ============================================================

def _read_run_dir(target: str, rid: str) -> tuple[dict | None, dict | None, str, str]:
    """读一个结果目录 → `(run, manifest, integrity, detail)`；run=None 表示内容不可读。"""
    try:
        with open(os.path.join(target, "run.json"), encoding="utf-8") as fh:
            raw = fh.read()
        run = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, None, "unreadable", f"run.json 不可读：{exc}"
    if not isinstance(run, dict):
        return None, None, "unreadable", "run.json 不是 JSON 对象"
    manifest: dict = {}
    try:
        with open(os.path.join(target, "manifest.json"), encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            manifest = loaded
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        manifest = {}
    detail = ""
    integrity = "ok"
    want = ""
    for item in (manifest.get("files") or []):
        if item.get("name") == "run.json":
            want = str(item.get("sha256") or "")
    got = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if not want:
        integrity, detail = "manifest_missing", "manifest.json 缺失或没有 run.json 条目"
    elif want != got:
        integrity, detail = "hash_mismatch", "run.json 与 manifest 记录的哈希不一致"
    if integrity == "ok":
        checks = (
            ("run_id", run.get("run_id"), rid),
            ("manifest.run_id", manifest.get("run_id"), rid),
            ("manifest.result_hash", manifest.get("result_hash"), run.get("result_hash")),
            ("manifest.snapshot_id", manifest.get("snapshot_id"), run.get("snapshot_id")),
            ("manifest.rules_hash", manifest.get("rules_hash"), run.get("rules_hash")),
        )
        for label, a, b in checks:
            if str(a or "") != str(b or ""):
                integrity = "identity_mismatch"
                detail = f"{label} 与目录/文件内身份不一致（目录={rid}）"
                break
    return run, manifest, integrity, detail


def load_run(scope, token: str) -> tuple[dict | None, dict | None, dict | None]:
    """读一份结果 → `(run, manifest, error)`。

    - 目录不存在/id 非法/歧义 → `(None, None, error)`（HTTP 层 404/400）；
    - 目录在但内容不可读 → 伪 run（`integrity="unreadable"`，`usable=False`）供诊断；
    - 清单缺失/哈希不一致/身份不一致 → 照样返回内容但标 `integrity`+`usable=False`
      （诊断查看允许，**复用与比较必须看 usable**）。
    """
    rid, err = resolve_run_id(scope, token)
    if err:
        return None, None, err
    target = os.path.join(runs_root(scope), rid)
    run, manifest, integrity, detail = _read_run_dir(target, rid)
    if run is None:
        return ({"run_id": rid, "integrity": integrity, "usable": False,
                 "detail": detail, "schema": RUN_SCHEMA},
                None, None)
    run["integrity"] = integrity
    run["usable"] = integrity == "ok"
    if detail:
        run["integrity_detail"] = detail
    return run, manifest, None


def _identity_matches(stored: dict, fresh: dict) -> bool:
    """复用前的身份核对：stored 必须与本次执行完全同身份（防串目录/防串内容）。"""
    keys = ("run_id", "snapshot_id", "rules_hash", "result_hash",
            "impl_version", "field_map_version", "period_policy",
            "report_period", "comparable")
    for key in keys:
        if json.dumps(stored.get(key), ensure_ascii=False, sort_keys=True, default=str) != \
           json.dumps(fresh.get(key), ensure_ascii=False, sort_keys=True, default=str):
            return False
    a = normalize_scope(stored.get("scope") or {})
    b = normalize_scope(fresh.get("scope") or {})
    return (a["user"], a["project"]) == (b["user"], b["project"])


# ============================================================
# 写：临时目录 + 整体 rename；同身份复用；损坏绝不覆盖
# ============================================================

def save_run(run: dict, scope) -> tuple[dict, bool]:
    """落盘 → `(读回的 run, 是否复用)`；同身份已存在则校验后复用，损坏则抛错。"""
    norm_scope = normalize_scope(scope)
    rid = str(run.get("run_id") or "")
    if not _RUN_ID_RE.match(rid):
        raise ValueError(f"run_id 非法：{rid!r}")
    root = runs_root(norm_scope)
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, rid)
    if os.path.isdir(target):
        stored, _manifest, integrity, detail = _read_run_dir(target, rid)
        if stored is not None and integrity == "ok" and _identity_matches(stored, run):
            return stored, True
        raise FrozenCorruptError(rid, integrity, detail)
    tmp = os.path.join(root, f".tmp-{rid}-{os.getpid()}-{os.urandom(4).hex()}")
    os.makedirs(tmp, exist_ok=False)
    try:
        try:
            _write_one(tmp, "run.json", run)
            _write_one(tmp, "manifest.json", _manifest_of(run, tmp))
        except OSError as exc:
            # 磁盘满/权限/中断：不留下半个结果，也不把 IO 错误混成「历史损坏」
            raise StoreWriteError(
                f"结果写入失败（未写入任何结果；临时目录已清理）：{exc}") from None
        # rename 的两种失败必须分开处理（Windows 上偶发 sharing violation 不能当成"历史已损坏"）：
        #  a) 目标已存在 → 并发竞争，复验身份后按复用处理；
        #  b) 目标不存在 → 瞬时 IO 错误，退避重试；最终失败 → StoreWriteError（不写任何结果）。
        last_err: OSError | None = None
        for attempt in range(6):
            try:
                os.rename(tmp, target)
                last_err = None
                break
            except OSError as exc:
                last_err = exc
                if os.path.isdir(target):
                    stored, _manifest, integrity, detail = _read_run_dir(target, rid)
                    if (stored is not None and integrity == "ok"
                            and _identity_matches(stored, run)):
                        return stored, True
                    raise FrozenCorruptError(rid, integrity, detail) from None
                time.sleep(0.05 * (attempt + 1))
        if last_err is not None:
            raise StoreWriteError(
                f"结果写入失败（目录仍未被创建，未写入任何结果）：{last_err}")
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    return run, False


def quarantine_run(scope, token: str) -> tuple[dict | None, dict | None]:
    """**显式**隔离一个损坏结果目录（管理员操作）：原样移走，绝不重写、绝不删除内容。

    只允许隔离 `integrity != ok` 的目录；正常结果不允许移动（防误删研究记录）。
    返回 `(详情, 错误)`。
    """
    rid, err = resolve_run_id(scope, token)
    if err:
        return None, err
    target = os.path.join(runs_root(scope), rid)
    _run, _manifest, integrity, detail = _read_run_dir(target, rid)
    if integrity == "ok":
        return None, {"code": "not_damaged",
                      "error": "该结果完整性正常（integrity=ok）：不允许隔离/移动，"
                               "如确有需要请在授权流程中处理"}
    base = screen_base()
    dest_root = os.path.join(base, "quarantine", scope_key(scope))
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    dest = os.path.join(dest_root, f"{rid}.{stamp}")
    os.makedirs(dest_root, exist_ok=True)
    try:
        os.rename(target, dest)
    except OSError as exc:
        return None, {"code": "move_failed", "error": f"隔离失败：{exc}"}
    return {"run_id": rid, "integrity": integrity, "detail": detail,
            "moved_to": os.path.relpath(dest, base),
            "note": "损坏目录已原样移入 quarantine/（保留原始字节供诊断）；"
                    "同身份重新执行会创建全新目录"}, None


def _write_one(target: str, name: str, obj) -> None:
    """原子写入单个 JSON；Windows 上文件被占用（杀软/索引/另一句柄）时退避重试。

    重试耗尽 → `StoreWriteError`（明确「未写入」），不把占用错误当成内容问题。
    """
    blob = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    last: OSError | None = None
    for attempt in range(4):
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=target)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, os.path.join(target, name))
            return
        except OSError as exc:
            last = exc
            time.sleep(0.03 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    raise StoreWriteError(f"{name} 写入失败（文件被占用或原子替换失败，已重试 4 次，未写入）：{last}")


def _manifest_of(run: dict, target: str) -> dict:
    """清单（在临时目录里、紧随 run.json 写入）：文件哈希 + 身份字段（供读回时交叉核对）。"""
    entries = []
    for name in ("run.json",):
        try:
            with open(os.path.join(target, name), "rb") as fh:
                raw = fh.read()
        except OSError:
            raw = b""
        entries.append({"name": name, "bytes": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest()})
    return {
        "schema": MANIFEST_SCHEMA,
        "run_id": run.get("run_id"), "created_at": run.get("created_at"),
        "impl_version": run.get("impl_version"),
        "field_map_version": run.get("field_map_version"),
        "as_of": run.get("as_of"), "pool": list(run.get("pool") or ()),
        "rules_hash": run.get("rules_hash"), "snapshot_id": run.get("snapshot_id"),
        "result_hash": run.get("result_hash"),
        "period_policy": dict(run.get("period_policy") or {}),
        "report_period": run.get("report_period"),
        "comparable": run.get("comparable"),
        "scope": dict(run.get("scope") or {}),
        "counts": dict(run.get("counts") or {}),
        "source": dict(run.get("source") or {}),
        "files": entries,
    }


def list_runs(scope, limit: int = 50) -> list[dict]:
    """**本作用域**的历史结果摘要（新到旧）；坏目录标出（integrity/usable），不静默丢。"""
    root = runs_root(scope)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out: list[dict] = []
    for name in names:
        d = os.path.join(root, name)
        if not os.path.isdir(d) or not _RUN_ID_RE.match(name):
            continue
        run, _manifest, integrity, detail = _read_run_dir(d, name)
        if run is None:
            out.append({"run_id": name, "integrity": "unreadable", "usable": False,
                        "detail": detail, "scope": "self"})
            continue
        out.append({
            "run_id": run.get("run_id"), "created_at": run.get("created_at"),
            "as_of": run.get("as_of"), "pool": list(run.get("pool") or ()),
            "counts": dict(run.get("counts") or {}),
            "result_hash": run.get("result_hash"),
            "rules_hash": run.get("rules_hash"), "snapshot_id": run.get("snapshot_id"),
            "report_period": run.get("report_period"),
            "period_policy": dict(run.get("period_policy") or {}),
            "comparable": run.get("comparable"),
            "source": dict(run.get("source") or {}),
            "integrity": integrity, "usable": integrity == "ok",
        })
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    try:
        capped = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        capped = 50
    return out[:capped]


# ============================================================
# legacy：R1 原型的实例级目录（只读、owner=None、仅管理员诊断）
# ============================================================

def _legacy_summary(root: str) -> list[dict]:
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out: list[dict] = []
    for name in names:
        d = os.path.join(root, name)
        if not os.path.isdir(d) or not _RUN_ID_RE.match(name):
            continue
        run, _manifest, integrity, detail = _read_run_dir(d, name)
        if run is None:
            out.append({"run_id": name, "integrity": "unreadable", "usable": False,
                        "legacy": True, "owner": None, "detail": detail})
            continue
        out.append({
            "run_id": run.get("run_id"), "created_at": run.get("created_at"),
            "as_of": run.get("as_of"), "pool": list(run.get("pool") or ()),
            "counts": dict(run.get("counts") or {}),
            "result_hash": run.get("result_hash"),
            "source": dict(run.get("source") or {}),
            "integrity": integrity, "usable": False,
            "legacy": True, "owner": None,
            "detail": detail,
        })
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return out


def legacy_overview() -> dict:
    """管理员诊断：实例级旧记录（无 owner）清单 + 归属建议；**只读、不迁移、不公开**。"""
    runs = _legacy_summary(legacy_runs_root())
    presets: list[dict] = []
    root = legacy_presets_root()
    try:
        names = os.listdir(root)
    except OSError:
        names = []
    for name in sorted(names):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        presets.append({"preset_id": data.get("preset_id") or name[:-5],
                        "name": data.get("name"), "pool": list(data.get("pool") or []),
                        "as_of": data.get("as_of", ""),
                        "rules": len(data.get("rules") or []),
                        "created_at": data.get("created_at"),
                        "legacy": True, "owner": None, "usable": False})
    return {
        "status": "ok",
        "runs": runs, "presets": presets,
        "note": "R1 原型的实例级记录：无 owner、不默认公开、不迁移；"
                "归属建议：按 created_at/来源与使用记录人工核对后，由管理员在授权流程中显式迁移",
        "suggestion": "如需归属：先在授权流程确认使用人 → 管理员显式迁移 → 迁移记录进审计",
    }


def legacy_load_run(token: str) -> tuple[dict | None, dict | None, dict | None]:
    """管理员诊断读一份 legacy 结果（标 `usable=False`：不可采纳、不得复用/比较）。"""
    rid, err = resolve_run_id(None, token, root=legacy_runs_root())
    if err:
        return None, None, err
    target = os.path.join(legacy_runs_root(), rid)
    run, manifest, integrity, detail = _read_run_dir(target, rid)
    if run is None:
        return ({"run_id": rid, "integrity": integrity, "usable": False,
                 "detail": detail}, None, None)
    run["integrity"] = integrity
    run["usable"] = False
    run["legacy"] = True
    run["owner"] = None
    run["note"] = "legacy 实例级原型记录：不可采纳为个人研究记录，不得复用/比较；仅诊断查看"
    if detail:
        run["integrity_detail"] = detail
    return run, manifest, None


# ============================================================
# 证据绑定：run + entity → 可核对清单
# ============================================================

def evidence(run: dict, entity_id: str) -> dict | None:
    """某一份结果里某家公司的证据（绑 run_id + entity_id，不绑「最近任务」）。"""
    eid = str(entity_id or "").strip().upper()
    if not eid:
        return None
    ent = next((e for e in (run.get("entities") or [])
                if str(e.get("entity_id") or "").upper() == eid), None)
    row = None
    for group in ("pass", "fail", "insufficient"):
        for cand in (run.get("rows") or {}).get(group) or []:
            if str(cand.get("entity_id") or "").upper() == eid:
                row = cand
                break
        if row is not None:
            break
    if ent is None and row is None:
        return None
    obs = {str(o.get("fact_id") or ""): o for o in (ent or {}).get("observations") or []}
    readings: list[dict] = []
    for item in (row or {}).get("readings") or []:
        inputs = [str(x) for x in item.get("inputs") or []]
        detail = []
        for fid in inputs:
            o = obs.get(fid)
            if not o:
                continue
            detail.append({"fact_id": fid, "metric": o.get("metric"),
                           "metric_label": o.get("metric_label"),
                           "period": o.get("period"), "value": o.get("value"),
                           "unit": o.get("unit"), "currency": o.get("currency"),
                           "caliber": o.get("caliber"),
                           "period_kind": o.get("period_kind"),
                           "period_start": o.get("period_start"),
                           "period_end": o.get("period_end"),
                           "as_of": o.get("as_of"), "state": o.get("state"),
                           "source_url": o.get("source_url"),
                           "source_hash": o.get("source_hash"),
                           "verify_state": o.get("verify_state")})
        readings.append({
            "field": item.get("field"), "label": item.get("label"),
            "status": item.get("status"), "value": item.get("value"),
            "unit": item.get("unit"), "display": item.get("display"),
            "formula": item.get("formula"), "reason": item.get("reason"),
            "inputs": detail,
            "unresolved_inputs": [f for f in inputs if f not in obs],
        })
    urls = sorted({d["source_url"] for r in readings for d in r["inputs"]
                   if d.get("source_url")})
    return {
        "schema": EVIDENCE_SCHEMA,
        "run_id": run.get("run_id"), "result_hash": run.get("result_hash"),
        "snapshot_id": run.get("snapshot_id"), "as_of": run.get("as_of"),
        "created_at": run.get("created_at"),
        "scope": dict(run.get("scope") or {}),
        "report_period": run.get("report_period"),
        "comparable": run.get("comparable"),
        "source": dict(run.get("source") or {}),
        "entity": (ent or {}).get("entity") or (row or {}).get("entity"),
        "entity_id": eid,
        "verdict": (row or {}).get("verdict"),
        "eval_period": (row or {}).get("eval_period"),
        "dataset_hash": (row or {}).get("dataset_hash"),
        "reasons": list((row or {}).get("reasons") or ()),
        "readings": readings,
        "periods": list((ent or {}).get("periods") or ()),
        "gaps": list((ent or {}).get("gaps") or ()),
        "conflicts": list((ent or {}).get("conflicts") or ()),
        "excluded": [dict(x) for x in (ent or {}).get("excluded") or ()],
        "pending_pit": [dict(x) for x in (ent or {}).get("pending_pit") or ()],
        "source_urls": urls,
        "integrity": run.get("integrity", "ok"),
        "usable": run.get("usable", True),
    }


# ============================================================
# 预设：股票池 + 规则（可变偏好；作用域内保存/读取/删除）
# ============================================================
#
# R1.1：预设按作用域分仓（服务端从会话推导），跨作用域不可见；文件内也写 scope，
# 读回时二次核对（防止手工搬文件跨作用域冒充）。

def _preset_path(scope, preset_id: str) -> str | None:
    pid = str(preset_id or "").strip()
    if not _PRESET_ID_RE.match(pid):
        return None
    return os.path.join(presets_root(scope), f"{pid}.json")


def _clean_pool(pool) -> tuple[list[str], list[str]]:
    if isinstance(pool, str):
        pool = [p for p in re.split(r"[,，\s]+", pool) if p]
    if not isinstance(pool, (list, tuple)):
        return [], ["股票池必须是代码数组"]
    out: list[str] = []
    for raw in pool:
        code = str(raw or "").strip().upper()
        if not code:
            continue
        if len(code) > 16 or any(ch in code for ch in "/\\:"):
            return [], [f"代码「{code}」不合法（不接受路径/前缀，只接受市场代码）"]
        if code not in out:
            out.append(code)
    if not out:
        return [], ["股票池不能为空"]
    if len(out) > _MAX_POOL_CODES:
        return [], [f"股票池最多 {_MAX_POOL_CODES} 个代码"]
    return out, []


def _clean_name(name) -> tuple[str, list[str]]:
    text = str(name or "").strip()
    if not text:
        return "", ["预设名称不能为空"]
    if len(text) > _MAX_PRESET_NAME:
        return "", [f"预设名称最长 {_MAX_PRESET_NAME} 字"]
    if any(ord(ch) < 32 for ch in text):
        return "", ["预设名称不能包含控制字符"]
    return text, []


def save_preset(*, scope, name, pool, rules_raw, as_of: str = "", note: str = "",
                preset_id: str = "", dataset_id: str = "") -> tuple[dict | None, list[str]]:
    """新增或更新一份预设 → `(预设, 错误)`；有错就一个字段都不落盘。"""
    norm_scope = normalize_scope(scope)
    errors: list[str] = []
    if len(list(rules_raw or [])) > _MAX_PRESET_RULES:
        return None, [f"规则最多 {_MAX_PRESET_RULES} 条（不静默截断：少一条都可能改变筛选含义）"]
    clean_name, name_errors = _clean_name(name)
    errors.extend(name_errors)
    codes, pool_errors = _clean_pool(pool)
    errors.extend(pool_errors)
    rules, rule_errors = ss.parse_rules(list(rules_raw or [])[:_MAX_PRESET_RULES])
    errors.extend(rule_errors)
    asof = ""
    raw_asof = str(as_of or "").strip()
    if raw_asof:
        asof = ss._iso_date(raw_asof)
        if not asof:
            errors.append(f"截止日「{raw_asof}」不是 YYYY-MM-DD 日期")
    # 数据来源也进预设：只记池与规则、不记来源，重开时会在用户没注意的情况下换用另一份数据，
    # 「同一份预设」看起来一样、算出来的候选却不同 —— 那是假一致，比缺功能更糟。
    did = str(dataset_id or "").strip()
    if did:
        if not _DATASET_ID_RE.match(did):
            errors.append(f"数据来源 id 非法：{did}")
        elif not load_dataset(norm_scope, did)[0]:
            errors.append(f"数据来源数据集不存在或已删除：{did}"
                          "（不保存指向空来源的预设；请重新选择数据来源）")
    if errors:
        return None, errors

    # 更新同一预设 id 时先读后写：整段放进锁里，避免两个请求同时保存同一预设互相覆盖
    with _lock_for(presets_root(norm_scope)):
        old: dict | None = None
        pid = str(preset_id or "").strip()
        if pid:
            path = _preset_path(norm_scope, pid)
            if path is None:
                return None, ["预设 id 非法"]
            old = load_preset(norm_scope, pid)
            if old is None:
                return None, [f"预设不存在：{pid}"]
        else:
            pid = hashlib.sha256(os.urandom(16)).hexdigest()[:16]

        now = _now()
        preset = {
            "schema": PRESET_SCHEMA,
            "preset_id": pid,
            "scope": dict(norm_scope),
            "name": clean_name,
            "note": str(note or "").strip()[:200],
            "pool": codes,
            "dataset_id": did,
            "as_of": asof,
            "rules": [r.as_dict() for r in rules],
            "rules_hash": ss.rules_hash(rules),
            "created_at": (old or {}).get("created_at") or now,
            "updated_at": now,
        }
        root = presets_root(norm_scope)
        os.makedirs(root, exist_ok=True)
        target = os.path.join(root, f"{pid}.json")
        blob = json.dumps(preset, ensure_ascii=False, indent=2).encode("utf-8")
        fd, tmp = tempfile.mkstemp(prefix=".preset.", dir=root)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return preset, []


def load_preset(scope, preset_id: str) -> dict | None:
    """读一份预设（本作用域内）；不存在/坏文件/契约不符/作用域不符都返回 None。"""
    path = _preset_path(scope, preset_id)
    if path is None or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != PRESET_SCHEMA:
        return None
    if not _PRESET_ID_RE.match(str(data.get("preset_id") or "")):
        return None
    try:
        norm_scope = normalize_scope(scope)
    except ValueError:
        return None
    stored = data.get("scope") or {}
    if str(stored.get("user") or "") != norm_scope["user"] or \
       str(stored.get("project") or "personal") != norm_scope["project"]:
        return None
    return data


def list_presets(scope, limit: int = 50) -> list[dict]:
    """**本作用域**预设摘要（最近更新在前）；坏文件跳过。"""
    root = presets_root(scope)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    items: list[dict] = []
    for name in sorted(names):
        if not name.endswith(".json") or name.startswith("."):
            continue
        pid = name[:-5]
        preset = load_preset(scope, pid)
        if preset is None:
            continue
        items.append({
            "preset_id": preset.get("preset_id"), "name": preset.get("name"),
            "note": preset.get("note", ""), "pool": list(preset.get("pool") or []),
            "as_of": preset.get("as_of", ""), "rules": list(preset.get("rules") or []),
            "dataset_id": preset.get("dataset_id", ""),
            "rules_hash": preset.get("rules_hash", ""),
            "created_at": preset.get("created_at"), "updated_at": preset.get("updated_at"),
        })
    items.sort(key=lambda x: (str(x.get("updated_at") or ""), str(x.get("preset_id"))),
               reverse=True)
    try:
        capped = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        capped = 50
    return items[:capped]


def delete_preset(scope, preset_id: str) -> bool:
    """删除本作用域内的一份预设（只删预设，永不触碰结果快照）。"""
    path = _preset_path(scope, preset_id)
    if path is None or not os.path.isfile(path):
        return False
    # 只删本作用域的文件；跨作用域 id 在这里自然落空
    with _lock_for(presets_root(scope)):
        if load_preset(scope, preset_id) is None:
            return False
        try:
            os.remove(path)
        except OSError:
            return False
    return True


# ============================================================
# R1.3 人工复筛对比：两份**已冻结**结果之间，谁进、谁出、谁读数变了
# ============================================================

COMPARE_SCHEMA = "weavemind.stock_screen.compare/1"
# 字段白名单先行：只有能筛的 5 个字段参与读数对比。
# 结果文件里的其它数字（哈希、计数、底稿内部量）一律不当成「读数变化」来报，
# 否则对比会冒出用户无法核对、也无法执行的差异。
COMPARE_FIELDS: tuple[str, ...] = tuple(ss.FIELD_SPECS)
_VERDICT_LABELS = {"pass": "通过", "fail": "未通过", "insufficient": "资料不足"}
_CONDITION_ORDER = (
    ("rules", "筛选规则"), ("pool", "股票池"), ("period_policy", "期间口径"),
    ("eval_periods", "实际评估期"), ("impl_version", "计算实现版本"),
    ("field_map_version", "字段口径版本"), ("as_of", "资料截止日"),
    ("source", "数据来源"),
)
# 这几类变化属于「口径/实现」，一旦不同就不能把差异当成数据或条件的效果。
_NON_ATTRIBUTABLE_KEYS = {"rules", "pool", "period_policy", "eval_periods",
                          "impl_version", "field_map_version"}
# 版本号在旧结果里可能没有记录：**未知不等于相同**，按「不同」处理。
_VERSION_KEYS = {"impl_version", "field_map_version"}


def _run_summary(run: dict) -> dict:
    """一份结果的「对比用摘要」（只取身份相关字段，不搬证据全量）。"""
    policy = run.get("period_policy") or {}
    src = run.get("source") or {}
    return {
        "run_id": run.get("run_id"), "created_at": run.get("created_at"),
        "as_of": run.get("as_of"), "pool": list(run.get("pool") or []),
        "rules": list(run.get("rules") or []), "rules_hash": run.get("rules_hash"),
        "period_policy": {"mode": policy.get("mode"), "report_period": policy.get("report_period")},
        "report_period": run.get("report_period"), "comparable": run.get("comparable"),
        "counts": dict(run.get("counts") or {}), "snapshot_id": run.get("snapshot_id"),
        "result_hash": run.get("result_hash"),
        "source": {
            "label": src.get("label"), "sha256": src.get("sha256"),
            "dataset_id": src.get("dataset_id") or "",
            "official": bool(src.get("official")), "user_provided": bool(src.get("user_provided")),
            "synthetic": bool(src.get("synthetic")), "row_count": src.get("row_count"),
            "provenance": dict(src.get("provenance") or {}),
        },
        "impl_version": str(run.get("impl_version") or ""),
        "field_map_version": str(run.get("field_map_version") or ""),
        # 实际评估期：按行统计（latest_each / 缺期时各主体可能不同），不是只看配置口径
        "eval_periods": _eval_period_counts(run),
    }


def _eval_period_counts(run: dict) -> dict:
    out: dict[str, int] = {}
    for bucket in ("pass", "fail", "insufficient"):
        for row in ((run.get("rows") or {}).get(bucket) or []):
            period = str(row.get("eval_period") or "").strip()
            if period:
                out[period] = out.get(period, 0) + 1
    return dict(sorted(out.items()))


def _conditions(base: dict, other: dict) -> list[dict]:
    """口径逐项比对：哪一项变了，是「不可直接归因」的唯一依据。"""
    out: list[dict] = []
    for key, label in _CONDITION_ORDER:
        if key == "pool":
            same = sorted(base["pool"]) == sorted(other["pool"])
        elif key == "source":
            same = (str(base["source"].get("sha256") or "") ==
                    str(other["source"].get("sha256") or "")
                    and base["source"].get("label") == other["source"].get("label"))
        elif key in _VERSION_KEYS:
            left_raw = str(base.get(key) or "")
            right_raw = str(other.get(key) or "")
            same = bool(left_raw) and bool(right_raw) and left_raw == right_raw
        else:
            same = base.get(key) == other.get(key)
        if key == "rules":
            left = "；".join(_rule_text(r) for r in base["rules"]) or "（无规则）"
            right = "；".join(_rule_text(r) for r in other["rules"]) or "（无规则）"
        elif key == "pool":
            left = "、".join(base["pool"]) or "（空池）"
            right = "、".join(other["pool"]) or "（空池）"
        elif key == "period_policy":
            left = _period_text(base)
            right = _period_text(other)
        elif key == "source":
            left = _source_text(base["source"])
            right = _source_text(other["source"])
        elif key == "eval_periods":
            left, right = _periods_text(base["eval_periods"]), _periods_text(other["eval_periods"])
        elif key in _VERSION_KEYS:
            left = str(base.get(key) or "未记录")
            right = str(other.get(key) or "未记录")
        else:
            left, right = str(base.get(key) or ""), str(other.get(key) or "")
        out.append({"key": key, "label": label, "same": same,
                    "base": left, "other": right})
    return out


def _rule_text(rule: dict) -> str:
    field = str(rule.get("field") or "")
    spec = ss.FIELD_SPECS.get(field)
    name = spec.label if spec else field
    unit = str(rule.get("unit") or (spec.unit if spec else ""))
    value = rule.get("value")
    try:
        shown = f"{float(value):g}"
    except (TypeError, ValueError):
        shown = str(value)
    return f"{name} {rule.get('op')} {shown}{unit}"


def _period_text(summary: dict) -> str:
    """期间**口径**本身（模式 + 要求年度）；实际落到哪一年由「实际评估期」单独比。"""
    policy = summary.get("period_policy") or {}
    mode = policy.get("mode") or ""
    label = ss.PERIOD_MODE_LABELS.get(mode, mode or "（未记录）")
    pinned = str(policy.get("report_period") or "").strip()
    return f"{label}｜要求 {pinned}" if pinned else label


def _periods_text(counts: dict) -> str:
    if not counts:
        return "（未记录评估期）"
    return "、".join(f"{p}×{n}" for p, n in sorted(counts.items()))


def _source_text(src: dict) -> str:
    label = src.get("label") or "（未记录）"
    prov = src.get("provenance") or {}
    if prov.get("kind_label"):
        kind = prov["kind_label"]
    elif src.get("synthetic"):
        kind = "合成演示数据"
    elif src.get("user_provided"):
        kind = "用户提供（未独立核验）"
    elif src.get("official"):
        kind = "官方披露缓存（整理后的宽表）"
    else:
        kind = "来源未标注"
    sha = str(src.get("sha256") or "")[:12]
    return f"{label}（{kind}，{src.get('row_count') or '?'} 行，文件指纹 {sha}…）"


def _reading_map(row: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in (row.get("readings") or []):
        field = str(item.get("field") or "")
        if field:
            out[field] = item
    return out


def _same_reading(a: dict | None, b: dict | None) -> bool:
    """读数是否同一：状态相同，且（有值时可复算值到 1e-6 相等）。"""
    if not a or not b:
        return a is b
    if str(a.get("status")) != str(b.get("status")):
        return False
    if str(a.get("status")) != "ok":
        return True                                   # 都是「不适用/缺失」：不算读数变化
    try:
        return abs(float(a.get("value")) - float(b.get("value"))) <= 1e-6
    except (TypeError, ValueError):
        return a.get("display") == b.get("display")


def _row_index(run: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for bucket in ("pass", "fail", "insufficient"):
        for row in ((run.get("rows") or {}).get(bucket) or []):
            code = str(row.get("entity_id") or "")
            if code:
                out[code] = {**row, "bucket": bucket}
    return out


def compare_runs(scope, base_token: str, other_token: str) -> tuple[dict, list[str]]:
    """对比两份**本作用域内**已冻结结果 → `(对比结果, 错误)`。

    - 只读：不重算、不写盘、不改任何一份结果的身份或哈希；
    - 读数对比只走字段白名单（`COMPARE_FIELDS`），其它数字不当读数；
    - 规则/池/期间口径只要有一项不同，就标「不可直接归因」。
    """
    errors: list[str] = []
    loaded: dict[str, dict] = {}
    for key, token, name in (("base", base_token, "基准结果"), ("other", other_token, "对照结果")):
        rid, err = resolve_run_id(scope, token)
        if err:
            errors.append(f"{name}：{err.get('error')}")
            continue
        run, _manifest, integrity, detail = _read_run_dir(
            os.path.join(runs_root(scope), rid), rid)
        if run is None or integrity != "ok":
            errors.append(f"{name}不可用于对比（integrity={integrity}）：{detail}")
            continue
        loaded[key] = run
    if errors:
        return {}, errors
    if loaded["base"].get("run_id") == loaded["other"].get("run_id"):
        return {}, ["同一份结果不能和自己对比：请选择两份不同的冻结结果"]

    base, other = _run_summary(loaded["base"]), _run_summary(loaded["other"])
    conditions = _conditions(base, other)
    changed = [c for c in conditions if not c["same"]]
    condition_keys = {c["key"] for c in changed}

    # 归因口径：口径/实现变了 → 不可直接归因；只有输入/截止日变 → 差异由数据变化解释，也不是「筛选效果」。
    if condition_keys & _NON_ATTRIBUTABLE_KEYS:
        reasons = [f"{c['label']}变了：{c['base']} → {c['other']}" for c in changed
                   if c["key"] in _NON_ATTRIBUTABLE_KEYS]
        if condition_keys & _VERSION_KEYS:
            reasons.append("计算实现或字段口径变了：同一条规则在不同版本下可能给出不同读数，"
                           "不能当作数据或经营变化")
        if "eval_periods" in condition_keys:
            reasons.append("实际评估期不同（即使模式相同，分子分母对应的年度不同）：读数不可横比")
        attribution = {
            "code": "not_attributable", "attributable": False,
            "label": "不可直接归因",
            "reasons": reasons,
            "note": "两份结果的筛选条件、期间/实际评估期、计算实现或字段口径有不同，"
                    "候选差异不能当作「条件产生了什么效果」，也不能跨这两份结果做连续比较；"
                    "要先统一口径并重新执行再比。",
        }
    elif changed:
        attribution = {
            "code": "input_changed", "attributable": False,
            "label": "条件未变，输入变了",
            "reasons": [f"{c['label']}变了：{c['base']} → {c['other']}" for c in changed],
            "note": "规则、股票池、期间口径都相同，差异来自输入数据或资料截止日变化；"
                    "这解释的是「数据更新带来的变化」，不是「筛选条件的效果」。",
        }
    else:
        attribution = {
            "code": "identical_conditions", "attributable": False,
            "label": "条件完全相同",
            "reasons": [],
            "note": "条件与输入都相同（正常不会出现两份不同结果）；如出现请检查是否同一份结果。",
        }

    base_rows, other_rows = _row_index(loaded["base"]), _row_index(loaded["other"])
    pool_base, pool_other = set(base["pool"]), set(other["pool"])
    pool_added, pool_removed = pool_other - pool_base, pool_base - pool_other
    entered, exited, stayed = [], [], []
    for code in sorted(set(base_rows) | set(other_rows)):
        a, b = base_rows.get(code), other_rows.get(code)
        if a is None and b is not None:
            # 「结果里新出现」的原因要分开说：池里加进来的 ≠ 条件变化后新满足的 ≠ 数据补齐后才可评的
            if code in pool_added:
                why = "股票池这次新增了这家公司（不是条件变化导致的入选）"
            elif "rules" in condition_keys:
                why = "筛选规则变了，这一份里这家才进入结果"
            elif condition_keys & {"impl_version", "field_map_version", "eval_periods"}:
                why = "口径/实现变了，这一份里这家才可评估并进入结果"
            else:
                why = "条件未变：来自输入数据变化（例如新披露或资料补齐）"
            entered.append({
                "code": code, "entity": b.get("entity"), "verdict": b.get("bucket"),
                "verdict_label": _VERDICT_LABELS.get(b.get("bucket"), b.get("bucket")),
                "eval_period": b.get("eval_period"),
                "reason_kind": ("pool_added" if code in pool_added else
                                "rules_changed" if "rules" in condition_keys else
                                "input_changed"),
                "explain": why,
            })
            continue
        if b is None and a is not None:
            if code in pool_removed:
                why = "股票池这次移除了这家公司（不是条件变化导致的落选）"
            elif "rules" in condition_keys:
                why = "筛选规则变了，这一份里这家不再进入结果（也仍可能标为资料不足）"
            elif condition_keys & {"impl_version", "field_map_version", "eval_periods"}:
                why = "口径/实现变了，这一份里这家不再出现在结果里"
            else:
                why = "条件未变：来自输入数据变化（例如数据被撤下或过期）"
            exited.append({
                "code": code, "entity": a.get("entity"), "verdict": a.get("bucket"),
                "verdict_label": _VERDICT_LABELS.get(a.get("bucket"), a.get("bucket")),
                "eval_period": a.get("eval_period"),
                "reason_kind": ("pool_removed" if code in pool_removed else
                                "rules_changed" if "rules" in condition_keys else
                                "input_changed"),
                "explain": why,
            })
            continue
        # 两份都在：比读数（字段白名单内）与期间
        ra, rb = _reading_map(a), _reading_map(b)
        fields = [f for f in COMPARE_FIELDS if f in ra or f in rb]
        field_changes = []
        for field in fields:
            left, right = ra.get(field), rb.get(field)
            if _same_reading(left, right):
                continue
            spec = ss.FIELD_SPECS.get(field)
            field_changes.append({
                "field": field, "label": spec.label if spec else field,
                "base": {"status": left.get("status") if left else None,
                         "display": left.get("display") if left else "（该份未评估此字段）"},
                "other": {"status": right.get("status") if right else None,
                          "display": right.get("display") if right else "（该份未评估此字段）"},
                "kind": "value" if (left and right and str(left.get("status")) == "ok"
                                    and str(right.get("status")) == "ok") else "state",
            })
        period_same = str(a.get("eval_period") or "") == str(b.get("eval_period") or "")
        stayed.append({
            "code": code, "entity": b.get("entity") or a.get("entity"),
            "base_verdict": a.get("bucket"), "other_verdict": b.get("bucket"),
            "base_verdict_label": _VERDICT_LABELS.get(a.get("bucket"), a.get("bucket")),
            "other_verdict_label": _VERDICT_LABELS.get(b.get("bucket"), b.get("bucket")),
            "verdict_changed": a.get("bucket") != b.get("bucket"),
            "period": {"base": a.get("eval_period"), "other": b.get("eval_period"),
                       "same": period_same},
            "comparable": period_same,
            "field_changes": field_changes,
            "note": "" if period_same else "两份的评估期不同：读数不可直接比较（口径变了）",
        })

    changed_rows = [r for r in stayed if r["field_changes"]]

    def _pair(base_v: str, other_v: str) -> int:
        return sum(1 for r in stayed
                   if r["base_verdict"] == base_v and r["other_verdict"] == other_v)

    summary = {
        # A. 池成员变化：来自两份的股票池清单，和「筛选结论变化」无关
        "pool_added": len(pool_added), "pool_removed": len(pool_removed),
        # B. 结果行进出：实际出现在结果里的公司数变化（可能由池/规则/数据任一引起）
        "entered": len(entered), "exited": len(exited),
        "stayed": len(stayed), "stayed_changed": len(changed_rows),
        "stayed_unchanged": len(stayed) - len(changed_rows),
        # C. 筛选结论变化：只在「两份都在」的公司上算，且不再把资料不足进出算成经营变化
        "verdict_flipped": sum(1 for r in stayed if r["verdict_changed"]),
        "became_pass": _pair("fail", "pass"),
        "left_pass": _pair("pass", "fail"),
        "insufficient_new": sum(1 for r in stayed
                                if r["other_verdict"] == "insufficient"
                                and r["base_verdict"] != "insufficient"),
        "insufficient_resolved": sum(1 for r in stayed
                                     if r["base_verdict"] == "insufficient"
                                     and r["other_verdict"] != "insufficient"),
        "pass_from_insufficient": _pair("insufficient", "pass"),
        "fail_from_insufficient": _pair("insufficient", "fail"),
        "not_comparable": sum(1 for r in stayed if not r["comparable"]),
    }
    return {
        "status": "ok", "schema": COMPARE_SCHEMA,
        "scope": dict(normalize_scope(scope)),
        "base": base, "other": other,
        "conditions": conditions, "changed_conditions": changed,
        "attribution": attribution,
        "summary": summary,
        "entered": entered, "exited": exited, "stayed": stayed,
        "field_whitelist": [
            {"field": f, "label": ss.FIELD_SPECS[f].label, "unit": ss.FIELD_SPECS[f].unit}
            for f in COMPARE_FIELDS],
        "reading": "差值只按字段白名单逐项给出，不做综合评分、不做因果推断、不预测。",
        "note": "对比只读：两份冻结结果都不会被改动；要改条件请重新执行筛选，另生成一份新结果。",
    }, []

# ============================================================
# B3-1 个人研究清单（观察对象，不是持仓/组合）+ 简短备注
# ============================================================

WATCHLIST_SCHEMA = "weavemind.stock_screen.watchlist/1"
RULES_TEMPLATE_SCHEMA = "weavemind.stock_screen.rules/1"
COST_SCHEMA = "weavemind.stock_screen.cost/1"

_MAX_WATCH_ITEMS = 200
_MAX_WATCH_NOTE = 500
_WATCH_CODE_RE = re.compile(r"^[0-9]{6}\.[A-Z]{2}$")


def watchlist_root(scope) -> str:
    """研究清单根：`<screen>/scopes/u-<hash>-p-<hash>/watchlist`（按作用域隔离）。"""
    return _scoped_dir(scope, "watchlist")


def _watchlist_path(scope) -> str:
    return os.path.join(watchlist_root(scope), "watchlist.json")


def _clean_watch_code(code) -> tuple[str, list[str]]:
    text = str(code or "").strip().upper()
    if not text:
        return "", ["代码不能为空"]
    if not _WATCH_CODE_RE.match(text):
        return "", [f"代码「{text}」不合法：应形如 600519.SH 或 002304.SZ"]
    return text, []


def _clean_watch_note(note) -> tuple[str, list[str]]:
    text = str(note or "").replace("\r\n", "\n").strip()
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        return "", ["备注不能包含控制字符"]
    if len(text) > _MAX_WATCH_NOTE:
        return "", [f"备注最长 {_MAX_WATCH_NOTE} 字（当前 {len(text)} 字）"]
    return text, []


def _watchlist_rev(items: list[dict], baseline_run: str = "") -> str:
    """清单 rev：条目 + **跟踪基准**一起进哈希。

    只哈希条目会出现「两个标签用同一个旧 rev 各设一次基准，后写覆盖前者」；
    把 baseline 纳入 rev 后，设基准也受同一个乐观并发条件保护。
    """
    body = json.dumps({"items": items, "baseline_run": str(baseline_run or "")},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def load_watchlist(scope) -> tuple[dict, list[str]]:
    """读本作用域的研究清单；目录不存在 → 空清单（不是错误）。

    文件损坏时**不覆盖、不清空**：返回 `integrity=corrupt` + 原文件路径 + 警告，调用方不得据此写回。
    """
    norm = normalize_scope(scope)
    path = _watchlist_path(norm)
    base = {"schema": WATCHLIST_SCHEMA, "items": [], "updated_at": "", "rev": _watchlist_rev([]),
            "scope": dict(norm), "integrity": "empty", "path": path,
            "baseline_run": "", "baseline_set_at": "",
            "limits": {"max_items": _MAX_WATCH_ITEMS, "max_note": _MAX_WATCH_NOTE}}
    if not os.path.exists(path):
        return base, []
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        size = 0
        try:
            size = os.path.getsize(path)
        except OSError:
            pass
        return ({**base, "integrity": "corrupt", "corrupt_bytes": size,
                 "note": "原文件已保留；修复或重建需独立显式操作，不会自动覆盖"},
                [f"研究清单读取失败（{path}）：{exc}"])
    if not isinstance(data, dict):
        return ({**base, "integrity": "corrupt", "corrupt_bytes": 0,
                 "note": "原文件已保留；修复或重建需独立显式操作，不会自动覆盖"},
                ["研究清单格式不合法（应为对象）"])
    items = []
    skipped = 0
    for raw_item in data.get("items") or []:
        if not isinstance(raw_item, dict):
            skipped += 1
            continue
        code, errs = _clean_watch_code(raw_item.get("code"))
        if errs:
            skipped += 1
            continue
        note, _ = _clean_watch_note(raw_item.get("note"))
        try:
            version = max(1, int(raw_item.get("version") or 1))
        except (TypeError, ValueError):
            version = 1
        items.append({"code": code, "name": str(raw_item.get("name") or "")[:40],
                      "note": note, "added_at": str(raw_item.get("added_at") or ""),
                      "updated_at": str(raw_item.get("updated_at") or ""),
                      "version": version})
    baseline_run = str(data.get("baseline_run") or "")
    payload = {"schema": WATCHLIST_SCHEMA, "items": items,
               "updated_at": str(data.get("updated_at") or ""),
               "rev": _watchlist_rev(items, baseline_run), "scope": dict(norm),
               "baseline_run": baseline_run,
               "baseline_set_at": str(data.get("baseline_set_at") or ""),
               "integrity": "ok_with_skipped" if skipped else "ok", "path": path,
               "limits": {"max_items": _MAX_WATCH_ITEMS, "max_note": _MAX_WATCH_NOTE}}
    if skipped:
        return payload, [f"清单里有 {skipped} 条记录无法解析，已跳过（原文件未改动）"]
    return payload, []


def _save_watchlist(scope, items: list[dict], *, baseline_run: str | None = None,
                    baseline_set_at: str | None = None) -> None:
    norm = normalize_scope(scope)
    root = watchlist_root(norm)
    os.makedirs(root, exist_ok=True)
    if baseline_run is None:
        old, _ = load_watchlist(norm)
        baseline_run = str(old.get("baseline_run") or "")
        baseline_set_at = baseline_set_at if baseline_set_at is not None else str(
            old.get("baseline_set_at") or "")
    _write_one(root, "watchlist.json",
               {"schema": WATCHLIST_SCHEMA, "scope": dict(norm), "items": items,
                "rev": _watchlist_rev(items, baseline_run), "updated_at": _now(),
                "baseline_run": str(baseline_run or ""),
                "baseline_set_at": str(baseline_set_at or "")})


def watchlist_mutate(*, scope, action: str, code, name: str = "",
                     note: str = "", if_version: int | str | None = None,
                     if_updated_at: str = "") -> tuple[dict | None, list[str]]:
    """增/删/改备注：整段「读→校验→改→写」在锁内完成，返回更新后的整份清单。

    乐观并发校验用**单调递增的 `version`**（不是秒级时间戳：同一秒内两次写会撞在一起）：
      - 传 `if_version`（客户端上次看到的该条 version），或兼容传 `if_updated_at`；
      - 不匹配 → 抛 `VersionConflict`（HTTP 409），不悄悄覆盖对方内容。
    """
    norm = normalize_scope(scope)
    code_clean, errs = _clean_watch_code(code)
    if errs:
        return None, errs
    note_clean, note_errs = _clean_watch_note(note)
    if note_errs:
        return None, note_errs
    act = str(action or "add").strip().lower()
    if act not in ("add", "remove", "note"):
        return None, [f"未知动作「{act}」：只支持 add / remove / note"]
    with _lock_for(watchlist_root(norm)):
        current, load_errs = load_watchlist(norm)
        if current.get("integrity") in ("corrupt", "ok_with_skipped"):
            detail = "文件损坏" if current.get("integrity") == "corrupt" else "部分记录无法解析"
            return None, [f"研究清单{detail}（{current.get('path')}）：原字节已保留，本次未写入。"
                          "请先备份该文件、修复或显式重建后重试（不会自动覆盖/丢行）"]
        if load_errs and not (current.get("items")):
            return None, load_errs
        items = [dict(it) for it in (current.get("items") or [])]
        now = _now()
        idx = next((i for i, it in enumerate(items) if it["code"] == code_clean), -1)
        expect_version: int | None = None
        if if_version not in (None, ""):
            try:
                expect_version = int(if_version)
            except (TypeError, ValueError):
                return None, [f"if_version 不是整数：「{if_version}」"]
        expect_at = str(if_updated_at or "").strip()
        if idx >= 0 and expect_version is not None \
                and int(items[idx].get("version") or 1) != expect_version:
            raise VersionConflict(
                f"{code_clean} 的清单内容已被另一个页面修改（版本 {expect_version}"
                f" → {items[idx].get('version')}）：本次没有写入，请刷新后重试",
                current={"code": code_clean, "note": items[idx].get("note", ""),
                         "updated_at": items[idx].get("updated_at", ""),
                         "version": items[idx].get("version"),
                         "rev": current.get("rev", "")})
        if idx >= 0 and expect_version is None and expect_at \
                and str(items[idx].get("updated_at") or "") != expect_at:
            raise VersionConflict(
                f"{code_clean} 的清单内容已被另一个页面修改：本次没有写入，请刷新后重试",
                current={"code": code_clean, "note": items[idx].get("note", ""),
                         "updated_at": items[idx].get("updated_at", ""),
                         "version": items[idx].get("version"),
                         "rev": current.get("rev", "")})
        if act == "add":
            if idx >= 0:
                items[idx] = {**items[idx], "name": str(name or items[idx]["name"])[:40],
                              "note": note_clean or items[idx]["note"], "updated_at": now,
                              "version": int(items[idx].get("version") or 1) + 1}
            else:
                if len(items) >= _MAX_WATCH_ITEMS:
                    return None, [f"研究清单最多 {_MAX_WATCH_ITEMS} 只（请先移出一些再添加）"]
                items.append({"code": code_clean, "name": str(name or "")[:40],
                              "note": note_clean, "added_at": now, "updated_at": now,
                              "version": 1})
        elif act == "remove":
            if idx < 0:
                return None, [f"研究清单里没有 {code_clean}"]
            items.pop(idx)
        else:  # note
            if idx < 0:
                return None, [f"研究清单里没有 {code_clean}：先加入清单再写备注"]
            items[idx] = {**items[idx], "note": note_clean, "updated_at": now,
                          "version": int(items[idx].get("version") or 1) + 1}
        try:
            _save_watchlist(norm, items)
        except StoreWriteError as exc:
            return None, [f"清单未写入：{exc}"]
        data, _ = load_watchlist(norm)
        return data, []


def watchlist_set_baseline(*, scope, run_token: str = "", if_rev=None,
                           clear: bool = False) -> tuple[dict | None, list[str]]:
    """把某份**本人冻结结果**设为跟踪基准（简报的 base）；或显式清除。

    - 只接受本作用域内可读、完整性 `ok` 的结果（`resolve_run_id` + `_read_run_dir`）；
    - `if_rev`（清单 rev）用于乐观并发：期间被别人改过 → `VersionConflict`（409），
      不覆盖对方内容；查看简报本身**不**自动推进基准。
    """
    norm = normalize_scope(scope)
    with _lock_for(watchlist_root(norm)):
        current, load_errs = load_watchlist(norm)
        if current.get("integrity") in ("corrupt", "ok_with_skipped"):
            return None, [f"研究清单不可用于设置基准（integrity={current.get('integrity')}）："
                          "原字节已保留，本次未写入"]
        if if_rev not in (None, "") and str(if_rev) != str(current.get("rev") or ""):
            raise VersionConflict(
                f"研究清单已被另一个页面修改（rev {if_rev} → {current.get('rev')}）："
                "本次没有写入，请刷新后重试",
                current={"rev": current.get("rev"), "items": current.get("items") or []})
        rid = ""
        if not clear:
            rid_resolved, err = resolve_run_id(norm, run_token)
            if err:
                return None, [f"基准记录不可用：{err.get('error')}"]
            run, _m, integrity, detail = _read_run_dir(
                os.path.join(runs_root(norm), rid_resolved), rid_resolved)
            if run is None or integrity != "ok":
                return None, [f"基准记录不可用作基准（integrity={integrity}）：{detail}"]
            rid = str(run.get("run_id") or rid_resolved)
        try:
            _save_watchlist(norm, [dict(it) for it in (current.get("items") or [])],
                            baseline_run=rid, baseline_set_at=_now() if rid else "")
        except StoreWriteError as exc:
            return None, [f"基准未写入：{exc}"]
        data, _ = load_watchlist(norm)
        return data, []


# ============================================================
# B3-3 规则模板：只导规则/期间策略/版本/说明；不含池、备注、结果、身份
# ============================================================

_RULES_TEMPLATE_KEYS = {"schema", "created_at", "impl_version", "field_map_version",
                        "period_policy", "rules", "notes", "disclaimer"}
_RULES_TEMPLATE_PRIVATE_KEYS = ("pool", "pool_codes", "watchlist", "notes_list",
                                "results", "run_id", "dataset_id", "evidence",
                                "scope", "user", "token", "api_key", "password")


def export_rules_template(*, scope, rules_raw, period_policy_raw=None,
                          note: str = "") -> tuple[dict | None, list[str]]:
    """导出可分享的规则模板（纯文本 JSON）；内容不含私人股票池/备注/结果/身份。"""
    norm = normalize_scope(scope)
    rules, rule_errors = ss.parse_rules(list(rules_raw or []))
    if rule_errors:
        return None, rule_errors
    if not rules:
        return None, ["没有可导出的条件：至少需要一条完整规则"]
    if len(rules) > _MAX_PRESET_RULES:
        return None, [f"规则最多 {_MAX_PRESET_RULES} 条"]
    policy, policy_errors = ss.validate_period_policy(period_policy_raw)
    if policy_errors:
        return None, policy_errors
    text = str(note or "").strip()
    if len(text) > 200:
        return None, ["说明最长 200 字"]
    template = {
        "schema": RULES_TEMPLATE_SCHEMA,
        "created_at": _now(),
        "impl_version": ss.IMPL_VERSION,
        "field_map_version": ss.FIELD_MAP_VERSION,
        "period_policy": {"mode": policy.get("mode") or "uniform",
                          "report_period": policy.get("report_period") or ""},
        "rules": [r.as_dict() for r in rules],
        "notes": text,
        "disclaimer": ("本模板只含筛选条件与期间策略，不含股票池、研究备注、结果或任何账号信息；"
                       "导入后需你确认才会应用到表单，且不会自动执行筛选。"),
    }
    return {"status": "ok", "scope": dict(norm), "template": template,
            "note": "模板可直接分享；导入侧只做校验与预览，不执行筛选"}, []


def import_rules_template(scope, template) -> tuple[dict | None, list[str]]:
    """校验并规范化一份模板（**不执行、不落盘**）；可疑/私人内容一律拒绝。"""
    norm = normalize_scope(scope)
    if not isinstance(template, dict):
        return None, ["模板格式不合法：需要 JSON 对象"]
    bad = [k for k in _RULES_TEMPLATE_PRIVATE_KEYS if k in template]
    if bad:
        return None, [f"模板含私人或不可移植内容（{'、'.join(bad)}）：拒绝导入。"
                      "请只导出规则/期间策略/版本与说明"]
    unknown = sorted(set(template) - _RULES_TEMPLATE_KEYS)
    if unknown:
        return None, [f"模板含未知键（{'、'.join(unknown)}）：请用本产品导出的模板"]
    if str(template.get("schema") or "") != RULES_TEMPLATE_SCHEMA:
        return None, [f"模板 schema 不匹配：需要 {RULES_TEMPLATE_SCHEMA}，"
                      f"实际「{template.get('schema') or '缺失'}」"]
    warnings: list[str] = []
    for key in ("impl_version", "field_map_version"):
        if not str(template.get(key) or "").strip():
            warnings.append(f"模板没写 {key}：先按当前版本校验，是否可比由你在执行后自行判断")
    if str(template.get("impl_version") or "") not in ("", ss.IMPL_VERSION):
        warnings.append(f"模板来自实现版本「{template.get('impl_version')}」，"
                        f"当前是「{ss.IMPL_VERSION}」：口径可能不同，执行后不要直接和旧结果比高低")
    if str(template.get("field_map_version") or "") not in ("", ss.FIELD_MAP_VERSION):
        warnings.append(f"模板字段口径版本「{template.get('field_map_version')}」"
                        f"与当前「{ss.FIELD_MAP_VERSION}」不同：导入前请核对字段含义")
    raw_rules = template.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        return None, ["模板缺少 rules 数组（至少一条规则）"]
    if len(raw_rules) > _MAX_PRESET_RULES:
        return None, [f"规则最多 {_MAX_PRESET_RULES} 条（模板给了 {len(raw_rules)} 条）"]
    rules, rule_errors = ss.parse_rules(raw_rules)
    if rule_errors:
        return None, [f"模板规则不合法：{e}" for e in rule_errors]
    raw_policy = template.get("period_policy")
    if raw_policy is not None and not isinstance(raw_policy, dict):
        return None, ["模板 period_policy 必须是对象"]
    policy_raw = None
    if isinstance(raw_policy, dict):
        allowed = {"mode", "report_period"}
        extra = sorted(set(raw_policy) - allowed)
        if extra:
            return None, [f"period_policy 含未知键（{'、'.join(extra)}）"]
        policy_raw = {"mode": raw_policy.get("mode") or "uniform",
                      "report_period": raw_policy.get("report_period") or ""}
    policy, policy_errors = ss.validate_period_policy(policy_raw)
    if policy_errors:
        return None, [f"期间策略不合法：{e}" for e in policy_errors]
    return {"status": "ok", "scope": dict(norm),
            "preview": {"rules": [r.as_dict() for r in rules],
                        "rules_count": len(rules),
                        "period_policy": policy,
                        "notes": str(template.get("notes") or "")[:200],
                        "impl_version": ss.IMPL_VERSION,
                        "field_map_version": ss.FIELD_MAP_VERSION},
            "warnings": warnings,
            "note": "只做校验与预览：没有执行筛选、没有写入任何结果；点「应用这些条件」才会填进表单"}, []


# ============================================================
# B3-4 成本分账：数据准备请求 / 缓存命中 / 确定性计算耗时 / 模型调用
# ============================================================

def cost_root(scope) -> str:
    return _scoped_dir(scope, "cost")


def _cost_path(scope) -> str:
    return os.path.join(cost_root(scope), "ledger.json")


_EMPTY_COST = {
    "prepare_requests": 0,      # 数据准备请求（上传/解析 CSV、登记数据集）
    "previews": 0,              # 输入预览次数（不执行、不落结果）
    "compute_runs": 0,          # 真正做确定性计算的执行次数
    "compute_ms": 0,            # 确定性计算累计耗时（毫秒，进程内实测）
    "cache_hits": 0,            # 同一身份复用已冻结结果（不重算）
    "runs_total": 0,            # 执行请求总数
    "model_calls": 0,           # 实测模型调用次数（本功能当前恒为 0）
    "network_attempts": 0,      # 计划发起的出站请求数（含被策略拒绝的）
    "adapter_calls": 0,         # RC2-F6：适配器调用次数（公司级尝试；传输层无连接事件）
    "network_denied": 0,        # 被出站策略拒绝、**没有真正连接**（与抓取失败分开）
    "network_requests": 0,      # 已确认进入传输层的 HTTP 请求数；传输层不暴露连接事件时
                                # **不累加**（真实请求数未知，视图返回 None，不用调用数冒充）
    "network_ok": 0,            # 真正拿到响应的出站请求数
    "network_failed": 0,        # 真正连接后失败（超时/HTTP/结构不符）
    "external_fee": "unknown",  # 外部费用：没有报价就写 unknown，不写 0 冒充免费
}

_COST_INTS = tuple(k for k in _EMPTY_COST if k != "external_fee")


def _cost_view(data: dict, norm: dict, *, integrity: str, updated_at: str = "",
               path: str = "", warnings: list[str] | None = None,
               corrupt_bytes: int = 0) -> dict:
    view = {k: data.get(k, 0) for k in _COST_INTS}
    view["external_fee"] = str(data.get("external_fee") or "unknown")
    view.update({"schema": COST_SCHEMA, "scope": dict(norm),
                 "updated_at": updated_at, "integrity": integrity, "path": path,
                 "note": ("本地 API 额度为零不等于运营成本为零：模型调用按实测记；"
                          "被出站策略拒绝的请求记在 network_denied，不冒充已抓取；"
                          "外部数据费用没有报价就记 unknown，不填 0")})
    if corrupt_bytes:
        view["corrupt_bytes"] = corrupt_bytes
    if warnings:
        view["warnings"] = list(warnings)
    return view


def load_cost(scope) -> tuple[dict, list[str]]:
    """读成本账本；文件损坏时返回 integrity=corrupt + 警告，**不覆盖、不清零**。"""
    norm = normalize_scope(scope)
    path = _cost_path(norm)
    data = dict(_EMPTY_COST)
    if not os.path.exists(path):
        return _cost_view(data, norm, integrity="empty", path=path), []
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        warning = f"成本账本读取失败（{path}）：{exc}；原字节已保留，计量状态记为「未记录」"
        return (_cost_view(data, norm, integrity="unreadable", path=path,
                           warnings=[warning]), [warning])
    try:
        stored = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        warning = (f"成本账本损坏（{path}）：{exc}；原字节已保留，计量状态记为「未记录」，"
                   "修复或重建需显式操作（不会自动覆盖）")
        return (_cost_view(data, norm, integrity="corrupt", path=path,
                           warnings=[warning], corrupt_bytes=len(raw)), [warning])
    if not isinstance(stored, dict):
        warning = "成本账本格式不合法（应为对象）：原字节已保留，计量状态记为「未记录」"
        return (_cost_view(data, norm, integrity="corrupt", path=path,
                           warnings=[warning], corrupt_bytes=len(raw)), [warning])
    for key in _COST_INTS:
        value = stored.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            data[key] = value
    data["external_fee"] = str(stored.get("external_fee") or "unknown")
    return _cost_view(data, norm, integrity="ok", path=path,
                      updated_at=str(stored.get("updated_at") or "")), []


def record_cost(scope, **deltas) -> dict:
    """在锁内累加成本账本，返回含 `recorded`/`recording` 的账本视图。

    主业务可以成功而计量失败；但失败必须**显式**（`recorded=False` + 原因），
    并且损坏账本时保留原字节、不把坏文件覆盖成一份"看起来完整"的新累计。
    """
    norm = normalize_scope(scope)
    root = cost_root(norm)
    path = _cost_path(norm)
    with _lock_for(root):
        data, load_errors = load_cost(norm)
        integrity = data.get("integrity")
        # RC2-F7：corrupt / unreadable / 任何读取异常都**阻止旧账本被覆盖**——
        # 读取失败得到的零默认值只能用于「未知」视图，不能当可信累计继续写。
        if integrity in ("corrupt", "unreadable") or load_errors:
            reason = "ledger_corrupt" if integrity == "corrupt" else "ledger_unreadable"
            detail = ("成本账本损坏：原字节已保留，本次计量未记录"
                      if integrity == "corrupt" else
                      "成本账本不可读：原字节已保留，本次计量未记录；"
                      "业务结果已保存但计量失败，需显式修复账本后才能继续记账")
            out = dict(data)
            out["recorded"] = False
            out["recording"] = {"ok": False, "reason": reason, "detail": detail}
            return out
        acc = {k: int(data.get(k) or 0) for k in _COST_INTS}
        for key, value in deltas.items():
            if key not in _COST_INTS:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            acc[key] = acc[key] + int(value)
        acc["external_fee"] = str(data.get("external_fee") or "unknown")
        payload = dict(acc)
        payload.update({"schema": COST_SCHEMA, "scope": dict(norm),
                        "updated_at": _now()})
        try:
            os.makedirs(root, exist_ok=True)
            _write_one(root, "ledger.json", payload)
        except (OSError, StoreWriteError) as exc:
            out = _cost_view(acc, norm, integrity=data.get("integrity", "empty"),
                             path=path, updated_at=str(data.get("updated_at") or ""))
            out["recorded"] = False
            out["recording"] = {"ok": False, "reason": "write_failed", "detail": str(exc)}
            return out
        out = _cost_view(acc, norm, integrity="ok", path=path,
                         updated_at=payload["updated_at"])
        out["recorded"] = True
        out["recording"] = {"ok": True}
        return out


def rebuild_cost(scope, *, confirm: bool = False) -> tuple[dict | None, list[str]]:
    """显式重建损坏账本：把坏字节原样归档后再开新账；不做隐式自愈。"""
    norm = normalize_scope(scope)
    path = _cost_path(norm)
    if not confirm:
        return None, ["重建账本需要显式确认（confirm=true）：会归档当前坏文件并开启新账本"]
    if not os.path.exists(path):
        return None, ["没有损坏的账本需要重建"]
    with _lock_for(cost_root(norm)):
        data, load_errors = load_cost(norm)
        if data.get("integrity") not in ("corrupt", "unreadable") and not load_errors:
            return None, ["账本当前可读：无需重建（拒绝无谓改写）"]
        stamp = time.strftime("%Y%m%dT%H%M%S")
        archive = os.path.join(cost_root(norm), f"ledger.corrupt-{stamp}.json")
        try:
            shutil.copy2(path, archive)
            os.remove(path)
        except OSError as exc:
            return None, [f"归档/移除坏账本失败（原文件保留）：{exc}"]
        fresh = dict(_EMPTY_COST)
        fresh.update({"schema": COST_SCHEMA, "scope": dict(norm), "updated_at": _now()})
        try:
            _write_one(cost_root(norm), "ledger.json", fresh)
        except (OSError, StoreWriteError) as exc:
            return None, [f"新账本写入失败（坏文件已归档到 {archive}）：{exc}"]
        view, _ = load_cost(norm)
        return {"status": "ok", "scope": dict(norm), "archived": archive, "cost": view}, []
