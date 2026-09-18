# -*- coding: utf-8 -*-
"""报告版本与证据绑定：**单一权威记录**（M0-a）。

背景（架构复核 2026-09-16）：`best_report` 只有文本，从未与验收/来源/规则指纹绑定，
导致"两稿共用同一份最新验收"——纠正稿被旧稿顶掉，且最终交付内容与验收元数据可能错配。

设计要点（按路线文档）：

- **唯一写者**：每任务一份 `report_versions.json`（任务工作区内），只由 `VersionStore` 写；
  写盘用"临时文件 + 原子替换"，避免半截文件。
- **完整性用全量 SHA256**：`version_id` 是正文全文的 sha256（短 hash 仅供显示）。
- **验收绑到产出它的那版正文**：验收报告自身带 `report_sha256`（对 `report.md` 算的），
  回填时按该值找版本；找不到就保持"未知"，绝不借用其它版本的验收。
- **同正文不同证据 = 不同身份**：正文 hash 相同但来源快照/规则指纹变化时，身份不同
  （用 `identity_id` 区分：正文 hash + 来源指纹 + 规则指纹）。
- **原子切换选中版本**：`adopt()` 在锁内改 `selected`，迟到结果不得覆盖已选中/已取消版本。
- **导出 manifest**：PDF/HTML/Markdown **各有自己的文件 hash**（PDF 字节 hash ≠ 正文 hash），
  通过 manifest 绑定同一 `report_version_id` 与正文 hash，并记录渲染器/模板版本；
  字节完整性与语义一致性分开检查。
- **未验收草稿**：hash 不一致时可保留供排查，但不得显示已通过、不得进入成功沉淀。
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

VERSIONS_FILE = "report_versions.json"
RENDERER_VERSION = "report_pdf/v1"
TEMPLATE_VERSION = "default"


def body_hash(text: str) -> str:
    """正文**全量** SHA256（版本完整性依据；短 hash 只用于显示）。"""
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def file_hash(path: str | Path) -> str:
    """导出文件自身的字节 hash（PDF 与 Markdown 必然不同）。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ReportVersion:
    version_id: str = ""            # 正文全量 sha256
    body: str = ""
    root_task_id: str = ""
    parent_id: str = ""             # 父版本（纠错稿的来处）
    iteration: int = 0
    sources_fingerprint: str = ""   # 来源快照/事实集指纹
    rules_version: str = ""
    rules_fingerprint: str = ""
    acceptance: dict = field(default_factory=dict)   # {overall, gaps, report_sha256, ...}
    policy_version: str = ""
    created_at: float = 0.0
    adopted: bool = False
    draft_only: bool = False        # 交付正文与验收对象不一致时置位（未验收草稿）
    # 入库时的身份（版本库的键）。身份由"正文+来源+规则"算出，但 `bind_acceptance`
    # 会把 `rules_fingerprint` 覆盖成验收里的值——于是**重算**出来的身份与入库时的键
    # 不再相等（实测：键 `7faeb4d9…`、重算 `2033fae7…`），页面/导出清单里的
    # `report_version_id` 与版本库里那条记录对不上，审计无法对账。
    # 记录里存下来的身份才是权威（`record()` 就把它随记录一起写），这里只负责带上它。
    stored_identity: str = ""

    def identity_id(self) -> str:
        """身份 = 正文 + 来源 + 规则：同正文但证据变化时不是同一个"已验收版本"。

        已入库的记录返回**入库时算出的身份**（`stored_identity`）：身份一旦确定就不再
        随字段回填而漂移。只有尚未入库/旧记录（没有存下身份）才现算。
        """
        if self.stored_identity:
            return self.stored_identity
        raw = "|".join([self.version_id, self.sources_fingerprint, self.rules_fingerprint])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def identity_drifted(self) -> bool:
        """现算身份与入库身份是否已不一致（审计用：能显示"这条记录的身份被回填改过"）。"""
        if not self.stored_identity:
            return False
        raw = "|".join([self.version_id, self.sources_fingerprint, self.rules_fingerprint])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest() != self.stored_identity

    def acceptance_overall(self) -> str:
        return str((self.acceptance or {}).get("overall") or "")   # "" = 未知

    def acceptance_is_full_hash(self) -> bool:
        """验收是否带**全量**正文 hash（身份可证明）。"""
        got = str((self.acceptance or {}).get("report_sha256") or "")
        return len(got) >= 64

    def acceptance_needs_reverify(self) -> bool:
        """该版只有旧的短 hash 验收：按"待重验"处理，不算已验证（R0.2）。"""
        acc = self.acceptance or {}
        return bool(acc) and not self.acceptance_is_full_hash()

    def acceptance_for_this_body(self) -> bool:
        """验收是否确实是对**这版正文**做的（否则不得算已验证）。

        R0.2：只认**全量** hash 相等。短 hash（16 位）无法证明身份——旧记录按
        "待重验"处理（`acceptance_needs_reverify()`），不再用前缀匹配冒充完整性。
        """
        got = str((self.acceptance or {}).get("report_sha256") or "")
        if not got:
            return False
        if not self.acceptance_is_full_hash():
            return False
        return got == self.version_id


def _from_raw(raw: dict) -> "ReportVersion":
    """从落盘记录重建（非字段键忽略，如 `adopt_reason`）。

    记录里存的 `identity_id` 是入库时的身份，带进 `stored_identity`——身份因此不会
    因为事后回填 `rules_fingerprint` 而漂移（页面/清单里的 `report_version_id` 与
    版本库的键保持同一个串）。
    """
    raw = dict(raw or {})
    valid = {f.name for f in dataclasses.fields(ReportVersion)}
    data = {k: v for k, v in raw.items() if k in valid}
    if not data.get("stored_identity"):
        stored = str(raw.get("identity_id") or "")
        if stored:
            data["stored_identity"] = stored
    return ReportVersion(**data)


class VersionStore:
    """每任务一个版本库（唯一写者 + 原子落盘）。"""

    _locks: dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, workspace: str | Path, root_task_id: str = ""):
        self.root_task_id = str(root_task_id or "")
        self.path = Path(workspace) / VERSIONS_FILE
        with VersionStore._locks_guard:
            self._lock = VersionStore._locks.setdefault(str(self.path), threading.Lock())

    # ── 读写 ────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        """原子写：临时文件 + replace，避免读者看到半截文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ── 记录 ────────────────────────────────────────────────
    @staticmethod
    def _key_for(versions: dict, version: ReportVersion) -> str:
        """定位条目键：**身份优先**，其次正文 hash（仅用于兼容旧键）。

        实测缺陷（R0.2）：先采纳 sourceA（键=身份A）后再采纳同正文的 sourceB，
        旧实现按正文 hash 找到了 sourceA 那条 → 采纳写错条目，选中还是 sourceA。
        因此这里先要身份精确相等，找不到才回退到"同正文的第一条"。
        """
        want_identity = version.identity_id()
        for key, raw in (versions or {}).items():
            if key == want_identity or str(raw.get("identity_id") or "") == want_identity:
                return key
        for key, raw in (versions or {}).items():
            if str(raw.get("version_id") or "") == version.version_id:
                return key
        return want_identity

    def record(self, body: str, *, sources_fingerprint: str = "", rules_version: str = "",
               rules_fingerprint: str = "", parent_id: str = "", iteration: int = 0,
               policy_version: str = "", acceptance: dict | None = None) -> ReportVersion:
        """登记一版正文。同正文同证据重复登记视为同一版本（幂等）。"""
        v = ReportVersion(
            version_id=body_hash(body), body=str(body or ""), root_task_id=self.root_task_id,
            parent_id=parent_id, iteration=int(iteration or 0),
            sources_fingerprint=str(sources_fingerprint or ""),
            rules_version=str(rules_version or ""), rules_fingerprint=str(rules_fingerprint or ""),
            acceptance=dict(acceptance or {}), policy_version=str(policy_version or ""),
            created_at=time.time(),
        )
        with self._lock:
            data = self._load()
            versions = data.setdefault("versions", {})
            key = v.identity_id()
            if key in versions:
                # 同身份（正文+来源+规则）已登记：补齐后来的证据，不新增条目
                raw = versions[key]
                if v.acceptance and not raw.get("acceptance"):
                    raw["acceptance"] = dict(v.acceptance)
                self._save(data)
                return _from_raw(raw)
            versions[key] = asdict(v)
            versions[key]["identity_id"] = key      # 身份随记录一起存（供选中查找）
            self._save(data)
            return _from_raw(versions[key])

    def bind_acceptance(self, acceptance: dict, *, sources_fingerprint: str = "",
                        rules_fingerprint: str = "") -> ReportVersion | None:
        """把一次验收结果绑到**产出它的那版正文**（R0.2：按完整身份精确绑定）。

        规则：
        - 只接受**全量** `report_sha256`（64 位十六进制）；短 hash 不能证明身份 →
          返回 None，该版保持"未知"（读取侧会标"待重验"）。
        - 调用方给出 `sources_fingerprint` 时，必须与该版记录的来源指纹一致，
          否则**不绑**（同正文不同来源是两版证据，不得互相借验收）。
        - 命中多条（历史分裂数据）时只更新**选中**那条，其余保持原样。
        """
        want = str((acceptance or {}).get("report_sha256") or "")
        if len(want) < 64:
            return None
        with self._lock:
            data = self._load()
            versions = data.get("versions") or {}
            hits = [(key, raw) for key, raw in versions.items()
                    if str(raw.get("version_id") or "") == want]
            if not hits:
                return None
            if sources_fingerprint:
                exact = [(k, r) for k, r in hits
                         if str(r.get("sources_fingerprint") or "") == sources_fingerprint]
                if not exact:
                    return None
                hits = exact
            if len(hits) > 1:
                sel = str(data.get("selected") or "")
                selected_hits = [(k, r) for k, r in hits if k == sel]
                hits = selected_hits or hits[:1]
            key, raw = hits[0]
            raw["acceptance"] = dict(acceptance or {})
            raw["rules_version"] = str((acceptance or {}).get("rules_version")
                                       or raw.get("rules_version") or "")
            raw["rules_fingerprint"] = str(
                (acceptance or {}).get("rules_fingerprint")
                or rules_fingerprint or raw.get("rules_fingerprint") or "")
            self._save(data)
            return _from_raw(raw)

    def get(self, version_id: str) -> ReportVersion | None:
        """按正文 hash 取版本；同正文多条时按"证据强度"排序取第一条（R0.2）。

        排序优先级：验收已**全量绑定** > 有验收（可能待重验） > 被选中 > 登记更晚。
        同正文不同来源是两版证据，调用方若需要精确某一条应改用 `find_identity()`。
        """
        with self._lock:
            data = self._load()
            sel = str(data.get("selected") or "")
            hits = [raw for raw in (data.get("versions") or {}).values()
                    if str(raw.get("version_id") or "") == str(version_id)]
            if not hits:
                return None

            def _rank(raw: dict) -> tuple:
                v = _from_raw(raw)
                return (
                    1 if v.acceptance_for_this_body() else 0,
                    1 if (raw.get("acceptance") or {}) else 0,
                    1 if (raw.get("identity_id") == sel or raw.get("version_id") == sel) else 0,
                    float(raw.get("created_at") or 0),
                )

            hits.sort(key=_rank, reverse=True)
            return _from_raw(hits[0])

    def find_identity(self, identity_id: str) -> ReportVersion | None:
        """按**完整身份键**取版本（R0.2 精确绑定用）。"""
        with self._lock:
            raw = (self._load().get("versions") or {}).get(str(identity_id))
            return _from_raw(raw) if isinstance(raw, dict) else None

    def find_by_body(self, body: str) -> ReportVersion | None:
        return self.get(body_hash(body))

    def adopted(self) -> ReportVersion | None:
        """当前选中版本（**只返回选中条目自身**）。

        R0.2：去掉"同正文兄弟条目借验收"的回退——那会让 sourceB 借到 sourceA 的
        PASS；选中条目没有自己的验收就是"未知"，由交付守卫按草稿处理。
        """
        with self._lock:
            data = self._load()
            sel = str(data.get("selected") or "")
            for raw in (data.get("versions") or {}).values():
                if raw.get("identity_id") == sel or raw.get("version_id") == sel:
                    return _from_raw(raw)
        return None

    def selected_needs_reverify(self) -> bool:
        """选中版本是否只有旧的短 hash 验收（需要重验）。"""
        v = self.adopted()
        return bool(v and v.acceptance_needs_reverify())

    # ── 采纳（原子切换）─────────────────────────────────────
    def adopt(self, version: ReportVersion, *, reason: str = "") -> bool:
        """切换选中版本。**已取消/已终态**的任务由调用方先行拦截，此处只做原子赋值。"""
        with self._lock:
            data = self._load()
            versions = data.setdefault("versions", {})
            key = self._key_for(versions, version)
            if key not in versions:
                versions[key] = asdict(version)
            versions[key]["identity_id"] = key
            for raw in versions.values():
                raw["adopted"] = False
            versions[key]["adopted"] = True
            versions[key]["adopt_reason"] = str(reason or "")
            data["selected"] = key
            data["selected_at"] = time.time()
            self._save(data)
        return True

    def reject_late(self, reason: str = "") -> None:
        """迟到结果：记录一次拒绝，不改选中版本（保证选中版本不被覆盖）。"""
        with self._lock:
            data = self._load()
            data.setdefault("late_rejected", []).append({"at": time.time(), "reason": str(reason or "")})
            self._save(data)

    def record_delivery(self, delivered_body: str, *, accepted_body: str = "",
                        ok: bool = False, reason: str = "") -> dict:
        """记录**最终交付正文**的 hash 与它对应的选中版本（M0-a）。

        交付正文 = 装配后的完整文档（交付说明 + 分隔 + 研究正文），与研究正文不是
        同一份字节，所以两者的 hash 都要留：验收对象是研究正文，交付物是前者。
        同一次投递重复记录只保留一条（按 delivered_sha256 去重）。
        """
        adopted = self.adopted()
        entry = {
            "delivered_sha256": body_hash(delivered_body),
            "accepted_body_sha256": body_hash(accepted_body) if accepted_body else "",
            "report_version_id": adopted.identity_id() if adopted else "",
            "aligned": bool(adopted) and adopted.version_id == body_hash(accepted_body or ""),
            "ok": bool(ok),
            "reason": str(reason or ""),
            "at": time.time(),
        }
        with self._lock:
            data = self._load()
            deliveries = data.setdefault("deliveries", [])
            deliveries[:] = [
                d for d in deliveries
                if str(d.get("delivered_sha256") or "") != entry["delivered_sha256"]
            ]
            deliveries.append(entry)
            self._save(data)
        return entry

    def deliveries(self) -> list[dict]:
        with self._lock:
            return list(self._load().get("deliveries") or [])


# ── 导出 manifest ────────────────────────────────────────────────

def build_export_manifest(version: ReportVersion, files: dict[str, str | Path], *,
                          renderer_version: str = RENDERER_VERSION,
                          template_version: str = TEMPLATE_VERSION) -> dict:
    """导出清单：每个文件**自己的** hash + 绑定的 report_version_id 与正文 hash。

    PDF/HTML/Markdown 的**字节完整性**检查看各自 `file_sha256`；
    **语义一致性**检查看它们是否都引用同一 `report_version_id` / `body_sha256`。
    """
    entries = {}
    for kind, path in (files or {}).items():
        p = Path(path)
        entries[kind] = {
            "path": str(p),
            "file_sha256": file_hash(p) if p.exists() else "",
            "exists": p.exists(),
        }
    return {
        "report_version_id": version.identity_id(),
        "body_sha256": version.version_id,
        "root_task_id": version.root_task_id,
        "rules_version": version.rules_version,
        "rules_fingerprint": version.rules_fingerprint,
        "acceptance_overall": version.acceptance_overall(),
        "renderer_version": renderer_version,
        "template_version": template_version,
        "files": entries,
        "created_at": time.time(),
    }


def verify_delivery(version: ReportVersion, delivered_body: str) -> tuple[bool, str]:
    """交付前一致性校验：交付正文必须与该版本验收所指正文一致。

    返回 `(ok, reason)`。不一致时调用方只能给"未验收草稿"，不得显示通过、不得进成功沉淀。
    """
    if not version:
        return False, "无选中版本"
    if not version.acceptance_for_this_body():
        return False, "该版本没有对应它自身的验收（未知）"
    if body_hash(delivered_body) != version.version_id:
        return False, "交付正文与该版本的验收对象不一致（装配/链接重写后未重验）"
    return True, ""


# 交付状态：唯一枚举（页面/MD/PDF/打印件/manifest/准入共用）
DELIVERY_VERIFIED = "verified"
DELIVERY_DRAFT = "draft"
DELIVERY_UNKNOWN = "unknown"


def verified_delivery(version: ReportVersion | None, delivered_body: str, *,
                      review_valid: bool = True,
                      hard_ok: bool = True,
                      hard_reason: str = "") -> tuple[str, str]:
    """**已验证交付**谓词（R0.3 唯一实现）：返回 `(status, reason)`。

    `status ∈ {verified, draft, unknown}`，判断顺序即优先级：

    1. `unknown`：没有选中版本，或该版验收无法证明属于本正文（短 hash / 身份不符）；
    2. `draft`：验收**不是 pass**（`overall != "pass"`）、交付正文与该版不一致（装配后变了）、
       任务硬约束未满足、或必需评审无效；
    3. `verified`：以上全部成立。

    "hash 相同"不能替代质量通过——这是 R0.3 的核心：绑定验收 ≠ 通过验收。
    调用方（页面/导出/manifest/准入）必须共用本函数，不得各自拼条件。
    """
    if version is None:
        return DELIVERY_UNKNOWN, "无选中版本"
    acc = version.acceptance or {}
    if not acc:
        return DELIVERY_UNKNOWN, "该版本没有对应它自身的验收（未知）"
    if not version.acceptance_for_this_body():
        why = ("该版只有短 hash 验收，需重验" if version.acceptance_needs_reverify()
               else "验收身份与本正文不符（未知）")
        return DELIVERY_UNKNOWN, why
    overall = str(acc.get("overall") or "")
    if overall != "pass":
        return DELIVERY_DRAFT, f"验收未通过（overall={overall or '未知'}）"
    if body_hash(delivered_body) != version.version_id:
        return DELIVERY_DRAFT, "交付正文与该版本的验收对象不一致（装配/链接重写后未重验）"
    if not hard_ok:
        return DELIVERY_DRAFT, hard_reason or "任务硬约束未满足"
    if not review_valid:
        return DELIVERY_DRAFT, "必需评审未取得绑定的 PASS"
    return DELIVERY_VERIFIED, ""
