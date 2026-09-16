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

    def identity_id(self) -> str:
        """身份 = 正文 + 来源 + 规则：同正文但证据变化时不是同一个"已验收版本"。"""
        raw = "|".join([self.version_id, self.sources_fingerprint, self.rules_fingerprint])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def acceptance_overall(self) -> str:
        return str((self.acceptance or {}).get("overall") or "")   # "" = 未知

    def acceptance_for_this_body(self) -> bool:
        """验收是否确实是对**这版正文**做的（否则不得算已验证）。"""
        got = str((self.acceptance or {}).get("report_sha256") or "")
        if not got:
            return False
        # 验收侧用的是短 hash（前 16 位）；两边都按前缀比较
        return self.version_id.startswith(got) or got.startswith(self.version_id[:16])


def _from_raw(raw: dict) -> "ReportVersion":
    """从落盘记录重建（忽略非字段键，如存放的 `identity_id`/`adopt_reason`）。"""
    valid = {f.name for f in dataclasses.fields(ReportVersion)}
    return ReportVersion(**{k: v for k, v in dict(raw or {}).items() if k in valid})


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
        """定位条目键：**正文 hash 是锚点**，身份键会随验收规则变化而变。

        实测缺陷（M0-f 离线完整交付）：先采纳（身份键 A）后绑验收（规则指纹进入身份
        → 键 B），于是同一个正文出现两条记录，选中条目上没有验收，交付守卫判"未知"。
        """
        for key, raw in (versions or {}).items():
            if str(raw.get("version_id") or "") == version.version_id:
                return key
        return version.identity_id()

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

    def bind_acceptance(self, acceptance: dict) -> ReportVersion | None:
        """把一次验收结果绑到**产出它的那版正文**（按验收的 report_sha256 找版本）。

        找不到匹配版本时返回 None —— 调用方必须按"未知"处理，**不得**把这份验收
        借给别的版本。绑定**就地更新**（键不变），避免同一正文出现"有验收/无验收"两条。
        """
        want = str((acceptance or {}).get("report_sha256") or "")
        if not want:
            return None
        with self._lock:
            data = self._load()
            versions = data.get("versions") or {}
            hits = [
                (key, raw) for key, raw in versions.items()
                if str(raw.get("version_id") or "").startswith(want) or want.startswith(
                    str(raw.get("version_id") or "")[:16])
            ]
            if not hits:
                return None
            # 多条同正文时全部绑定（历史数据可能已分裂），选中那条优先返回
            sel = str(data.get("selected") or "")
            chosen = None
            for key, raw in hits:
                raw["acceptance"] = dict(acceptance or {})
                raw["rules_version"] = str((acceptance or {}).get("rules_version")
                                           or raw.get("rules_version") or "")
                raw["rules_fingerprint"] = str((acceptance or {}).get("rules_fingerprint")
                                               or raw.get("rules_fingerprint") or "")
                if key == sel or chosen is None:
                    chosen = raw
            self._save(data)
            return _from_raw(chosen)

    def get(self, version_id: str) -> ReportVersion | None:
        """按正文 hash 取版本；同正文多条时优先带验收的那条（证据更全）。"""
        with self._lock:
            data = self._load()
            hits = [raw for raw in (data.get("versions") or {}).values()
                    if str(raw.get("version_id") or "") == str(version_id)]
            if not hits:
                return None
            hits.sort(key=lambda r: 1 if (r.get("acceptance") or {}) else 0, reverse=True)
            return _from_raw(hits[0])

    def find_by_body(self, body: str) -> ReportVersion | None:
        return self.get(body_hash(body))

    def adopted(self) -> ReportVersion | None:
        """当前选中版本；选中条目缺验收时，回退到同正文带验收的那条（历史分裂数据）。"""
        with self._lock:
            data = self._load()
            sel = str(data.get("selected") or "")
            versions = data.get("versions") or {}
            chosen = None
            for raw in versions.values():
                if raw.get("identity_id") == sel or raw.get("version_id") == sel:
                    chosen = raw
                    break
            if chosen is None:
                return None
            if not (chosen.get("acceptance") or {}):
                for raw in versions.values():
                    if (str(raw.get("version_id") or "") == str(chosen.get("version_id") or "")
                            and (raw.get("acceptance") or {})):
                        return _from_raw(raw)
            return _from_raw(chosen)

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
