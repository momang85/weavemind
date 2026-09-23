# -*- coding: utf-8 -*-
"""交付装配与验收的**唯一实现**（B 批）。

为什么要有这个模块：编排器收尾与 web_ui 的人工修订如果各写一套"验收 → 装配 → 记录交付"，
两边产出的交付字节必然慢慢分叉——manifest 里"导出字节 = 记录交付"这句就永远对不上，
页面、验收详情、导出也会各说一套。这里把三件事收口成同一实现：

- `accept_for_body`：对**指定正文**跑确定性验收（含来源标注自动修复并复检）、
  写 `acceptance_report.json` 与审计事件、按完整身份绑到该版；
- `assemble_and_verify`：确保该版有自己的验收 → 重算底稿与研究硬门槛 → 按**固定顺序**
  装配交付正文 → 走唯一谓词 `verified_delivery` 判状态 → 记录交付 hash；
- `delivery_state`：页面/导出/manifest 共用的读取口径（选中版本 + 最后一次交付 +
  持久化评审事实 + 可重算的硬门槛），因此三者不可能再各说一套。

纪律（沿用既有契约，不另立一套）：
- 绑定验收 ≠ 通过验收；未通过/未知一律按草稿交付，且草稿理由要写进交付物本体；
- 人工修订是**新版本**：旧验收、旧评审 PASS、旧人工批准都不迁移；
- 任何中途失败保持草稿/未知，不用"HTTP 成功"掩盖部分更新。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path

from report_version import (DELIVERY_UNKNOWN, DELIVERY_VERIFIED, VersionStore,
                            body_hash, verified_delivery)
import workspace
from working_paper_export import gaps_note, write_working_paper

logger = logging.getLogger(__name__)

SEPARATOR = "\n\n---\n\n"
WRAPPER_FILE = "delivery_wrapper.md"      # 交付说明（分隔符之前的部分），供修订时逐字节复用
REVIEW_STATE_FILE = "review_state.json"
RESEARCH_STATE_FILE = "research_state.json"

# 研究状态（批次3b）：**与数字机器验收分开**的另一条轴。`verified` 只说"数字与格式
# 机器验收通过"；研究是否就绪要看关键问题有没有带定位的依据、重要推断可否复核、
# 有没有事实矛盾。两者可以同时出现（数字 pass + 研究草稿），不许互相冒充。
RESEARCH_READY = "research_ready"
RESEARCH_DRAFT = "research_draft"
RESEARCH_NOT_APPLICABLE = "not_applicable"

_RESEARCH_STATE_LABELS = {
    RESEARCH_READY: "研究就绪（关键问题有带定位依据）",
    RESEARCH_DRAFT: "研究草稿／待补原始披露",
    RESEARCH_NOT_APPLICABLE: "不适用（非研究任务或无需叙事）",
}


def _research_binding(structure: dict | None, version=None, *,
                      contract_wire: dict | None = None) -> dict:
    """研究状态的**绑定对象**：正文版本 + 结构版本 + 资料/规则/契约指纹。

    页面、正文、PDF、导出清单读到的必须是同一份绑定（C-5）：修订或验收改稿后，
    结构对象、资料集、规则任一变化都会让指纹不同——此时状态按"待重验"显示，
    而不是沿用旧计数。
    """
    st = structure or {}
    ev = st.get("evidence") or {}
    return {
        "report_version_id": (version.identity_id() if version is not None else ""),
        "body_sha256": str(getattr(version, "version_id", "") or ""),
        "structure_version_id": str(st.get("version_id") or ""),
        "evidence_fingerprint": str(ev.get("fingerprint") or ""),
        "rules_version": str(st.get("rules_version") or ""),
        # 09-23：规则**指纹**也要进绑定并参与比较——只比版本标签时，
        # 规则内容变了但版本号没动（rules_version 不变、fingerprint 变）仍会判 current
        "rules_fingerprint": str(st.get("rules_fingerprint") or ""),
        "contract_fingerprint": str((contract_wire or {}).get("fingerprint") or ""),
        # 契约原文（wire）：修订重装时按它重建同一份契约，页面也能直接展示
        # 主体/期间/口径——不留"只存指纹、重建时无从下手"的缺口
        "contract": (dict(contract_wire) if contract_wire else None),
    }


def state_is_current(state: dict, version=None, *, structure: dict | None = None,
                     contract_wire: dict | None = None,
                     require_binding: bool = False) -> bool:
    """已落盘的研究状态是否仍绑定在**当前**这一版（正文/结构/资料/规则/契约）。

    `require_binding=True`（研究任务）时，绑定信息缺失即视为**未知/待重验**：
    只加字段不校验等于没绑定（09-22 晚间复核 P1——实物 binding 里 `contract=null`、
    证据/规则指纹为空却 stale=false）。
    """
    if bool((state or {}).get("stale")):
        return False                     # 已判定待重验：不再当作当前状态
    b = (state or {}).get("binding") or {}
    if not b:
        return False
    if not str(b.get("report_version_id") or ""):
        return False
    if version is not None and str(b.get("report_version_id")) != version.identity_id():
        return False
    if structure is not None:
        if str(b.get("structure_version_id") or "") != str(structure.get("version_id") or ""):
            return False
        # 资料/规则任一变化 → 失效（同一正文配不同资料不是同一个研究结论）
        ev = (structure.get("evidence") or {})
        if str(b.get("evidence_fingerprint") or "") != str(ev.get("fingerprint") or ""):
            return False
        if str(b.get("rules_version") or "") != str(structure.get("rules_version") or ""):
            return False
        # 09-23：规则**指纹**也要比——版本标签没动、内容变了同样失效
        if str(b.get("rules_fingerprint") or "") != str(structure.get("rules_fingerprint") or ""):
            return False
    if contract_wire is not None:
        if str(b.get("contract_fingerprint") or "") != str(contract_wire.get("fingerprint") or ""):
            return False
    if require_binding:
        # 研究任务：契约指纹必须有值；结构与资料身份缺失同样按未知处理
        if not str(b.get("contract_fingerprint") or ""):
            return False
        if not str(b.get("structure_version_id") or ""):
            return False
        if not str(b.get("evidence_fingerprint") or ""):
            return False
    return True


# 契约未声明必需指标时的默认必答三问（收入/利润/现金）。**不把** `bank_materials`
# 这类"待查材料清单"算进必答问题：它只表示还要什么材料，不是已回答的问题。
DEFAULT_MANDATORY_METRICS = ("revenue", "net_profit", "operating_cashflow")
_MANDATORY_LABELS = {"revenue": "收入变化的量价与结构依据",
                     "net_profit": "利润变化的分解",
                     "operating_cashflow": "现金变化与利润覆盖的关系"}
# 明确写成假设/推断/核查问题的内容不按"未证实肯定结论"扣分（语义角色判定，
# 不看状态名，也不靠豁免某个 boundary 字符串）
_NON_ASSERTIVE_CLAIM_TYPES = ("boundary", "inference", "assumption")
_UNPROVEN_STATUSES = ("unsupported", "partially_supported", "needs_check")


def staleness_reason(state: dict, version=None, *, structure: dict | None = None,
                     contract_wire: dict | None = None) -> str:
    """待重验的**具体原因**：正文不同版 / 结构按旧正文重建 / 资料变化 / 规则变化 / 契约缺失。

    09-23：结构版本号与正文 hash 是不同对象，不能统一误报"结构≠正文"——按实际比对
    结果逐项说清是哪一类变化，读者才知道该重建什么。
    """
    b = (state or {}).get("binding") or {}
    if not b:
        return "研究状态没有绑定信息：待重验（需按当前采纳正文重建）"
    reasons: list[str] = []
    cur_id = version.identity_id() if version is not None else ""
    if cur_id and str(b.get("report_version_id") or "") != cur_id:
        reasons.append(f"研究状态绑定的是旧版正文（{str(b.get('report_version_id') or '')[:12]}），"
                       f"当前采纳正文是 {cur_id[:12]}")
    st = structure or {}
    body_v = str(getattr(version, "version_id", "") or "")
    if st:
        _src_body = str(st.get("source_body_sha256") or "")
        if _src_body:
            if body_v and _src_body != body_v:
                reasons.append(f"结构投影按另一版正文重建（来源正文 {_src_body[:12]} ≠ "
                               f"采纳正文 {body_v[:12]}）")
        elif str(st.get("version_id") or "") and body_v \
                and str(st.get("version_id")) != body_v:
            reasons.append("结构投影没有记录来源正文（旧格式），无法确认它属于当前采纳正文")
        ev_fp = str((st.get("evidence") or {}).get("fingerprint") or "")
        if str(b.get("evidence_fingerprint") or "") != ev_fp:
            reasons.append(f"资料/准入/定位变化（指纹 "
                           f"{str(b.get('evidence_fingerprint') or '空')[:12]} → "
                           f"{ev_fp[:12] or '空'}）")
        if str(b.get("rules_version") or "") != str(st.get("rules_version") or ""):
            reasons.append(f"验收规则版本变化（{str(b.get('rules_version') or '空')} → "
                           f"{str(st.get('rules_version') or '空')}）")
        elif str(b.get("rules_fingerprint") or "") != str(st.get("rules_fingerprint") or ""):
            # 版本标签没动、规则内容变了（09-23：此前只比标签，这种情形仍判 current）
            reasons.append(f"验收规则内容变化（指纹 "
                           f"{str(b.get('rules_fingerprint') or '空')[:12]} → "
                           f"{str(st.get('rules_fingerprint') or '空')[:12]}）")
        if not str(b.get("structure_version_id") or ""):
            reasons.append("绑定里缺结构版本")
    if not str(b.get("contract_fingerprint") or ""):
        reasons.append("绑定里缺契约指纹")
    if contract_wire is not None:
        cf = str(contract_wire.get("fingerprint") or "")
        if cf and str(b.get("contract_fingerprint") or "") != cf:
            reasons.append("执行契约变化")
    return "；".join(reasons) if reasons else "研究状态与当前正文/资料/规则不同版：待重验"


def research_state(task_id: str, goal: str, structure: dict | None, *,
                   ws_dir=None, contract_wire: dict | None = None,
                   version=None) -> dict:
    """按**契约必答问题**逐项裁决研究状态（与 `verified` 分开的一条轴）。

    判据（09-22 复核 §3-C-3/4/5）：
    - **必答问题**：契约声明的必需指标（缺省收入/利润/现金三问）逐项有可定位、
      可审的依据（`support.has_evidence` 且带定位）才算有依据；缺一项就不就绪。
      银行材料清单等只作**可选问题**独列，不参与分子分母，不用"1/3、1/4"宣称完成；
    - **肯定结论**：语义角色是观察/目标主张的主张，若 support_status 处于
      unsupported / partially_supported / needs_check，则不算就绪（数字正确不证明
      结论成立）；明确写成假设、核查问题、推断边界的内容不扣分；
    - **绑定**：状态绑定最终采纳正文 + 结构版本 + 契约指纹；结构不属于当前正文时
      返回"待重验"，不沿用旧计数。
    """
    st = structure or {}
    binding = _research_binding(st, version, contract_wire=contract_wire)
    # 投影不属于当前采纳正文（修订/重装/换契约后）→ 待重验：不拿旧结构的计数
    # 冒充当前正文的研究状态。09-23：按**来源正文**判断（结构记着它是按哪版正文建的），
    # 旧格式结构才退回版本号比对；原因用 `staleness_reason` 逐项说清。
    _struct_vid = str(st.get("version_id") or "")
    _body_vid = str(getattr(version, "version_id", "") or "")
    _src_body = str(st.get("source_body_sha256") or "")
    _projection_mismatch = False
    if st and _body_vid:
        if _src_body:
            _projection_mismatch = _src_body != _body_vid
        elif _struct_vid:
            _projection_mismatch = _struct_vid != _body_vid
    if _projection_mismatch:
        return {"state": RESEARCH_DRAFT,
                "label": _RESEARCH_STATE_LABELS[RESEARCH_DRAFT],
                "stale": True,
                "reason": (staleness_reason({"binding": binding}, version,
                                            structure=st,
                                            contract_wire=contract_wire)
                           + "：待重验，计数不作为结论"),
                "located": int(((st.get("evidence") or {}).get("located")) or 0),
                "missing_labels": list((st.get("evidence") or {}).get("missing_labels") or []),
                "unproven_assertions": 0, "unsupported_claims": 0,
                "mandatory_supported": 0, "mandatory_total": 0,
                "optional_questions": [], "requires_narrative": True,
                "binding": binding}
    if not st:
        return {"state": RESEARCH_NOT_APPLICABLE,
                "label": _RESEARCH_STATE_LABELS[RESEARCH_NOT_APPLICABLE],
                "reason": "非研究任务（无结构化研究契约）", "located": 0,
                "missing_labels": [], "unproven_assertions": 0,
                "mandatory_supported": 0, "mandatory_total": 0,
                "optional_questions": [], "requires_narrative": False,
                "binding": binding}
    scope = st.get("scope") or {}
    periods = list(scope.get("periods") or [])
    questions = list(st.get("research_questions") or [])
    ev = st.get("evidence") or {}
    located = int(ev.get("located") or 0)
    missing = list(ev.get("missing_labels") or [])
    claims = list(st.get("claims") or [])

    mandatory_metrics = _mandatory_metrics(contract_wire)
    by_metric = {str(q.get("metric") or ""): q for q in questions}
    # 分母来自**契约**（默认收入/利润/现金三问）：结构里缺条目是**缺口**，
    # 不能因为"输出里恰好只有一条"就把分母缩成 1（09-22 晚间复核 P1）
    mandatory = [by_metric.get(m) or {"metric": m, "question": _MANDATORY_LABELS.get(m, m),
                                      "support": {}, "missing_entry": True}
                 for m in mandatory_metrics]
    optional = [q for q in questions
                if str(q.get("metric") or "") not in mandatory_metrics]

    # R1：研究状态读**逐问题评估**（与问题区/风险区同源）。`coverage` 三档：
    # full（分解覆盖充分，才算"回答完成"）/ partial（有材料但未闭合）/ none。
    # 旧结构（无 question_assessments）按旧口径折算成 partial，不假装"完成"。
    assessments = {str(k): v for k, v in (st.get("question_assessments") or {}).items()
                   if isinstance(v, dict)}

    def _coverage(q: dict) -> str:
        if q.get("missing_entry"):
            return "none"                # 结构里根本没有这条必答问题 → 未覆盖
        a = assessments.get(str(q.get("metric") or "")) or {}
        if a:
            if a.get("answered"):
                return "full"
            return str(a.get("coverage") or "none")
        sup = q.get("support") or {}
        if sup.get("has_evidence") and str(sup.get("locator") or "").strip():
            return "partial"
        return "none"

    def _kind_label(q: dict) -> str:
        a = assessments.get(str(q.get("metric") or "")) or {}
        return str(a.get("kind_label") or (q.get("support") or {}).get("kind_label") or "")

    unsupported_mandatory = [q for q in mandatory if _coverage(q) != "full"]
    partial_mandatory = [q for q in mandatory if _coverage(q) == "partial"]
    unproven = [c for c in claims
                if str(c.get("claim_type") or "") not in _NON_ASSERTIVE_CLAIM_TYPES
                and str(c.get("support_status") or "") in _UNPROVEN_STATUSES]
    requires_narrative = len(periods) >= 2 or bool(questions)

    common = {
        "located": located, "missing_labels": missing,
        # `mandatory_supported` = **回答完成**（分解覆盖充分）；部分覆盖单列，
        # 不用"有材料"冒充完成（R1 退出条件：不沿用旧 1/3 宣传）。
        "mandatory_supported": len(mandatory) - len(unsupported_mandatory),
        "mandatory_partial": len(partial_mandatory),
        "mandatory_total": len(mandatory),
        "mandatory_questions": [{"metric": q.get("metric"), "question": q.get("question"),
                                 "supported": _coverage(q) == "full",
                                 "coverage": _coverage(q),
                                 "material_kind": _kind_label(q),
                                 "missing_entry": bool(q.get("missing_entry")),
                                 "locator": str((q.get("support") or {}).get("locator")
                                                or (assessments.get(str(q.get("metric") or ""))
                                                    or {}).get("locator") or "")}
                                for q in mandatory],
        "optional_questions": [{"metric": q.get("metric"), "question": q.get("question"),
                                "supported": _coverage(q) == "full",
                                "coverage": _coverage(q)} for q in optional],
        "unproven_assertions": len(unproven),
        "unsupported_claims": len(unproven),
        "requires_narrative": requires_narrative,
        "binding": binding,
    }
    if not requires_narrative:
        return {"state": RESEARCH_NOT_APPLICABLE,
                "label": _RESEARCH_STATE_LABELS[RESEARCH_NOT_APPLICABLE],
                "reason": "契约不要求经营解释（单期或纯数据核对）", **common}

    reasons: list[str] = []
    if located <= 0:
        reasons.append("没有一条带正文定位的原始披露（located=0）")
    if unsupported_mandatory:
        labels = "、".join(str(q.get("question") or q.get("metric"))
                          for q in unsupported_mandatory)
        _p = (f"（其中部分覆盖 {len(partial_mandatory)} 项："
              + "、".join(f"{q.get('question')}[{_kind_label(q) or '材料'}]"
                          for q in partial_mandatory) + "）") if partial_mandatory else ""
        reasons.append(f"必答问题未完成（{len(unsupported_mandatory)}/"
                       f"{len(mandatory)}）：{labels}{_p}")
    if any(q.get("missing_entry") for q in mandatory):
        miss = "、".join(str(q.get("question") or q.get("metric"))
                        for q in mandatory if q.get("missing_entry"))
        reasons.append(f"结构里没有这些必答问题的条目（按缺口记）：{miss}")
    if unproven:
        reasons.append(f"{len(unproven)} 条肯定结论处于未证实状态"
                       f"（未支持/部分支持/待核查）")
    if reasons:
        return {"state": RESEARCH_DRAFT,
                "label": _RESEARCH_STATE_LABELS[RESEARCH_DRAFT],
                "reason": "；".join(reasons)[:220], **common}
    return {"state": RESEARCH_READY,
            "label": _RESEARCH_STATE_LABELS[RESEARCH_READY],
            "reason": (f"带定位披露 {located} 条；必答问题 "
                       f"{len(mandatory)}/{len(mandatory)} 完成（分解覆盖充分）；"
                       f"无未证实肯定结论"), **common}


def _mandatory_metrics(contract_wire: dict | None) -> tuple[str, ...]:
    """契约声明的必答指标；未声明时用默认三问（收入/利润/现金）。"""
    wire = contract_wire or {}
    metrics = [str(m) for m in (wire.get("required_metrics") or [])]
    keep = tuple(m for m in metrics if m in DEFAULT_MANDATORY_METRICS)
    return keep or DEFAULT_MANDATORY_METRICS


def write_research_state(task_id: str, payload: dict, *, ws_dir=None) -> None:
    """落盘研究状态（页面/导出清单读它；缺文件即"未知"，不编）。"""
    try:
        import json as _json
        p = Path(_ws(task_id, ws_dir)) / RESEARCH_STATE_FILE
        p.write_text(_json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:                     # noqa: BLE001 - 写不进去不影响交付
        logger.warning("研究状态落盘失败（task=%s）：%s", task_id, str(exc)[:120])


def read_research_state(task_id: str, *, ws_dir=None) -> dict:
    try:
        import json as _json
        p = Path(_ws(task_id, ws_dir)) / RESEARCH_STATE_FILE
        if not p.exists():
            return {}
        return _json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def research_state_note(state: dict) -> str:
    """研究状态写进交付说明（Markdown 与 PDF 封面共用同一段文字）。"""
    st = str((state or {}).get("state") or "")
    if not st or st == RESEARCH_NOT_APPLICABLE:
        return ""
    label = str((state or {}).get("label") or st)
    reason = str((state or {}).get("reason") or "")
    extra = ("（数字与格式的机器验收不因此改变；本条说的是**研究**是否就绪）"
             if st == RESEARCH_DRAFT else "")
    return f"> **研究状态：{label}**——{reason}{extra}"

# 自动注记块（草稿/评审/硬门槛）：修订重装时必须先剥掉旧的，否则会把上一版失败说明
# 带进新稿——"不得继续拿旧失败说明判断新稿"。
_AUTO_NOTE_PREFIXES = (
    "> **未验收草稿：",
    "> **评审状态：",
    "> **研究交付硬门槛未通过**：",
    "> **本次运行因根任务预算耗尽而提前收尾：",
)


# ── 从编排器搬过来的纯函数（零实例依赖）────────────────────────


def read_acceptance_summary(task_id: str) -> dict | None:
    """读工作区的验收快照（文件口径）；没有就返回 None，不编。"""
    try:
        p = workspace.task_workspace(task_id) / "acceptance_report.json"
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return {
            "overall": data.get("overall") or "",
            "gaps": data.get("gaps") or [],
            "rules_version": data.get("rules_version") or "",
            "rules_fingerprint": data.get("rules_fingerprint") or "",
            "profile": data.get("profile") or "",
            "report_sha256": data.get("report_sha256") or "",
        }
    except Exception:
        return None


def rules_identity(task_id: str) -> tuple[str, str]:
    """本次验收的规则版本与指纹（供版本身份使用）。"""
    summary = read_acceptance_summary(task_id)
    if summary and (summary.get("rules_version") or summary.get("rules_fingerprint")):
        return str(summary.get("rules_version") or ""), str(summary.get("rules_fingerprint") or "")
    try:
        from acceptance_checker import ACCEPTANCE_RULES_VERSION, rules_fingerprint
        return str(ACCEPTANCE_RULES_VERSION), str(rules_fingerprint())
    except Exception:
        return "", ""


def sources_fingerprint(task_id: str, report_text: str = "") -> str:
    """本轮**来源/事实快照指纹**（版本身份的一部分）。

    只取真正的来源通道与报告里的来源清单——不含账簿类文件（`report_versions.json` 等），
    否则同一正文在不同时刻算出的指纹不同，身份会漂移。
    """
    try:
        from acceptance_checker import _collect_sources
        parts: list[str] = []
        try:
            src = _collect_sources(workspace.task_workspace(task_id)) or {}
        except Exception:
            src = {}
        for name in sorted(src):
            text = str(src.get(name) or "")
            parts.append(f"{name}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}")
        text = str(report_text or "")
        blocks = re.findall(
            r"(?m)^#+\s*(?:参考来源|来源清单|参考资料|数据来源|来源附录)\s*$([\s\S]{0,2000})",
            text)
        for b in blocks[:3]:
            parts.append("srclist:" + hashlib.sha256(
                b.strip().encode("utf-8")).hexdigest()[:16])
        if not parts:
            return ""
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def package_manifest(task_id: str, ws, files, *, pdf_name: str = "") -> dict:
    """包内清单（schema 2）：显式区分**采纳正文**与**导出文件**的身份。

    09-23：不再用"首个 MD 字节 hash"冒充采纳正文 hash（packaging_worker 旧实现），
    字段分开：
    - `report_version_id` / `research_body_sha256`：打包时刻的**采纳身份**（陈旧判定用它）；
    - `delivered_md_sha256`：包内交付 MD 的字节 hash（与页面下载同源的那份）；
    - `pdf_sha256`：包内 PDF 字节 hash（没有就是空）；
    - `files`：包内每个成员的字节 hash（含图表/底稿/清单自身之外的全部成员）。
    """
    import hashlib as _h
    from pathlib import Path as _P
    out: dict = {"packaged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "schema": "weavemind.package/2"}
    file_hashes: dict[str, str] = {}
    for abs_path, arc in (files or []):
        try:
            file_hashes[str(arc)] = _h.sha256(_P(abs_path).read_bytes()).hexdigest()
        except Exception:
            file_hashes[str(arc)] = ""
    out["files"] = file_hashes
    # 交付 MD：reports/report.md 优先，否则第一个 reports/*.md
    _md_candidates = [a for a in file_hashes if str(a).startswith("reports/")
                      and str(a).endswith(".md")]
    _md = "reports/report.md" if "reports/report.md" in file_hashes else (
        sorted(_md_candidates)[0] if _md_candidates else "")
    out["delivered_md"] = _md
    out["delivered_md_sha256"] = file_hashes.get(_md, "")
    _pdf = str(pdf_name or "")
    if not _pdf:
        _pdfs = sorted(a for a in file_hashes if str(a).lower().endswith(".pdf"))
        _pdf = _pdfs[0] if _pdfs else ""
    out["pdf"] = _pdf
    out["pdf_sha256"] = file_hashes.get(_pdf, "")
    out["charts"] = {a: h for a, h in file_hashes.items() if str(a).startswith("charts/")}
    # 采纳身份（打包时刻）：陈旧判定按它，不按时间戳
    try:
        from report_version import VersionStore
        store = VersionStore(ws, task_id)
        adopted = store.adopted()
        out["report_version_id"] = (adopted.identity_id() if adopted is not None else "")
        out["research_body_sha256"] = str(getattr(adopted, "version_id", "") or "")
    except Exception as exc:                     # noqa: BLE001 - 身份算不出就留空，不编
        logger.warning("包内清单：采纳身份读取失败（task=%s）：%s", task_id, str(exc)[:120])
        out["report_version_id"] = ""
        out["research_body_sha256"] = ""
    # 材料与规则指纹：与版本身份同源（复用既有实现，不另算一套）
    try:
        body_text = ""
        if _md:
            try:
                for abs_path, arc in (files or []):
                    if str(arc) == _md:
                        body_text = _P(abs_path).read_text(encoding="utf-8")
                        break
            except Exception:
                body_text = ""
        out["sources_fingerprint"] = sources_fingerprint(task_id, body_text)
        rv, rf = rules_identity(task_id)
        out["rules_version"], out["rules_fingerprint"] = rv, rf
    except Exception as exc:                     # noqa: BLE001 - 指纹算不出不阻断打包
        logger.warning("包内清单指纹计算失败：%s", str(exc)[:120])
    return out


def _freeze_payload(task_id: str, ws, *, md_bytes: bytes = b"",
                    pdf_bytes: bytes = b"") -> dict:
    """快照时刻把**要进包的成员按字节冻结**（正文/PDF/底稿/图表/审计稿/引用证据）。

    09-23（项2）：此前快照只记图表 hash，打包时按名字重读磁盘——版本检查通过
    （采纳身份没变）**不代表整包同版**：期间重渲染的图、改写的底稿、重跑的证据
    文件照样被装进去，清单还给它们记上"当前"hash。这里一次性读成字节，打包只用
    这些字节；磁盘后来变了只在 `drift` 里如实报告，不改包内内容。
    """
    from pathlib import Path as _P
    ws = _P(ws)
    payload: dict[str, bytes] = {}
    if md_bytes:
        payload["reports/report.md"] = bytes(md_bytes)
    if pdf_bytes:
        payload["reports/report.pdf"] = bytes(pdf_bytes)
    for name, arc in (("working_paper.json", "working_paper.json"),
                      ("working_paper.csv", "working_paper.csv")):
        for cand in (ws / "project" / name, ws / name):
            if cand.is_file():
                try:
                    payload[arc] = cand.read_bytes()
                except Exception as exc:         # noqa: BLE001 - 读不到就不进包，不编
                    logger.warning("快照：底稿读取失败（%s）：%s", arc, str(exc)[:100])
                break
    charts_dir = ws / "charts"
    if charts_dir.is_dir():
        for p in sorted(charts_dir.glob("*.png")):
            try:
                payload[f"charts/{p.name}"] = p.read_bytes()
            except Exception as exc:             # noqa: BLE001
                logger.warning("快照：图表读取失败（%s）：%s", p.name, str(exc)[:100])
    for cand_dir in (ws / "project", ws):
        if not cand_dir.is_dir():
            continue
        for p in sorted(cand_dir.glob("model_report_full_*.md")):
            try:
                payload[f"audit/{p.name}"] = p.read_bytes()
            except Exception as exc:             # noqa: BLE001
                logger.warning("快照：审计稿读取失败（%s）：%s", p.name, str(exc)[:100])
        break
    # 引用证据：用**快照时刻**读到的那份资料生成一次并冻结字节——打包不再重读，
    # 资料在导出期间被重跑也不会让包内证据与正文错版。
    try:
        import narrative_evidence as _ne
        material = _ne.read(task_id, ws_dir=ws) or {}
        ev = citation_evidence_payload(task_id, material=material)
        if ev is not None:
            payload["evidence/citation_evidence.json"] = json.dumps(
                ev, ensure_ascii=False, indent=1).encode("utf-8")
    except Exception as exc:                     # noqa: BLE001 - 证据生成不了不阻断导出
        logger.warning("快照：引用证据生成失败（task=%s）：%s", task_id, str(exc)[:120])
    return payload


def _disk_drift(ws, frozen: dict) -> dict:
    """快照之后磁盘上变过的成员（只报告，不改包内内容）。"""
    from pathlib import Path as _P
    ws = _P(ws)
    out: dict[str, str] = {}
    for arc, blob in (frozen or {}).items():
        arc = str(arc)
        if arc.startswith("reports/"):
            continue                     # 正文/PDF 由导出动作本身写出，不算漂移
        name = arc.split("/", 1)[1] if "/" in arc else arc
        cands: list = []
        if arc.startswith("charts/"):
            cands = [ws / "charts" / name]
        elif arc.startswith("audit/"):
            cands = [ws / "project" / name, ws / name]
        elif arc.startswith("evidence/"):
            cands = [ws / "project" / name, ws / name]
        else:
            cands = [ws / "project" / name, ws / name]
        p = next((c for c in cands if c.is_file()), None)
        if p is None:
            out[arc] = "missing_on_disk"
            continue
        try:
            if hashlib.sha256(p.read_bytes()).hexdigest() != \
                    hashlib.sha256(bytes(blob)).hexdigest():
                out[arc] = "changed_on_disk"
        except Exception as exc:                 # noqa: BLE001 - 读不到按未知处理
            out[arc] = f"unreadable:{str(exc)[:40]}"
    return out


def export_snapshot(task_id: str, *, ws_dir=None, delivered_text: str = "",
                    md_bytes: bytes = b"", pdf_bytes: bytes = b"") -> dict:
    """导出用**一次不可变快照**：采纳身份 + 正文 + 资料/规则指纹 + 逐成员字节。

    09-23：重包各环节此前各自重读"当前版本"——生产探针里 MD 取 A、修订切 B、PDF 取 B，
    仍返回 ok/verify_ok=true（文件自检只能证明写入字节未坏，证明不了语义同版）。
    这里把身份/正文/指纹连同**底稿、图表、审计稿、引用证据的字节**一次性捕获，
    MD/PDF/底稿/清单都由它生成；调用方在发布前再核对一次采纳身份，变了就拒绝发布（409）。
    """
    from pathlib import Path as _P
    ws = _P(ws_dir) if ws_dir else workspace.task_workspace(task_id)
    store = VersionStore(ws, task_id)
    adopted = store.adopted()
    if adopted is None:
        raise LookupError("该任务没有可导出的采纳版本")
    text = str(delivered_text or "")
    if not md_bytes and text:
        md_bytes = text.encode("utf-8")
    payload = _freeze_payload(task_id, ws, md_bytes=md_bytes, pdf_bytes=pdf_bytes)
    rv, rf = rules_identity(task_id)
    ev_fp = ""
    try:
        import report_brief as _rb
        st = _rb.read_structure(task_id, ws_dir=ws) or {}
        ev_fp = str((st.get("evidence") or {}).get("fingerprint") or "")
    except Exception:
        ev_fp = ""
    frozen = {arc: hashlib.sha256(blob).hexdigest() for arc, blob in payload.items()}
    # charts 与包内清单同口径：键是**包内成员路径**（charts/chart_1.png），不是文件名
    charts = {arc: h for arc, h in frozen.items() if arc.startswith("charts/")}
    return {
        "report_version_id": adopted.identity_id(),
        "body_sha256": str(getattr(adopted, "version_id", "") or ""),
        "delivered_text": text,
        "sources_fingerprint": sources_fingerprint(task_id, text),
        "rules_version": rv,
        "rules_fingerprint": rf,
        "evidence_fingerprint": ev_fp,
        "charts": charts,
        "payload": payload,
        "frozen": frozen,
        "captured_at": time.time(),
    }


def _manifest_from_frozen(frozen: dict, *, snap: dict, ws=None,
                          pdf_name: str = "") -> dict:
    """包内清单（schema 2）：**按实际写入的字节**算 hash，身份取自快照。

    快照里记的 `frozen` 与写入字节不一致 → 抛 `RuntimeError("snapshot drift")`：
    宁可不发布，也不把两版内容装进同一个包。
    """
    out: dict = {"packaged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "schema": "weavemind.package/2"}
    hashes = {str(arc): hashlib.sha256(bytes(blob)).hexdigest()
              for arc, blob in (frozen or {}).items()}
    want = {str(a): str(h) for a, h in (snap.get("frozen") or {}).items()}
    drift = {a: hashes.get(a) for a, h in want.items() if hashes.get(a) != h}
    if drift:
        raise RuntimeError("snapshot drift")
    out["files"] = hashes
    _md_candidates = [a for a in hashes if a.startswith("reports/")
                      and a.endswith(".md")]
    _md = ("reports/report.md" if "reports/report.md" in hashes else (
        sorted(_md_candidates)[0] if _md_candidates else ""))
    out["delivered_md"] = _md
    out["delivered_md_sha256"] = hashes.get(_md, "")
    _pdf = str(pdf_name or "")
    if not _pdf:
        _pdf = "reports/report.pdf" if "reports/report.pdf" in hashes else ""
    out["pdf"] = _pdf
    out["pdf_sha256"] = hashes.get(_pdf, "")
    out["charts"] = {a: h for a, h in hashes.items() if a.startswith("charts/")}
    out["report_version_id"] = str(snap.get("report_version_id") or "")
    out["research_body_sha256"] = str(snap.get("body_sha256") or "")
    out["sources_fingerprint"] = str(snap.get("sources_fingerprint") or "")
    out["rules_version"] = str(snap.get("rules_version") or "")
    out["rules_fingerprint"] = str(snap.get("rules_fingerprint") or "")
    out["evidence_fingerprint"] = str(snap.get("evidence_fingerprint") or "")
    out["snapshot_captured_at"] = float(snap.get("captured_at") or 0.0)
    out["frozen"] = dict(want)
    if ws is not None:
        out["drift"] = _disk_drift(ws, frozen)
    return out


def repack_adopted(task_id: str, *, md_bytes: bytes = b"", pdf_bytes: bytes = b"",
                   ws_dir=None, snapshot: dict | None = None) -> dict:
    """按**当前采纳版本**重新打包（无模型、确定性）：新 ZIP + 包内清单。

    - `snapshot`（`export_snapshot` 的结果）传入时：**整包只用快照里冻结的字节**
      （正文/PDF/底稿/图表/审计稿/引用证据），不重读磁盘；清单按写入字节算 hash，
      并带上 `frozen`（快照 hash）与 `drift`（快照后磁盘变过的成员，只报告）；
    - 无快照（旧调用）时按名字从工作区读数——`packaging_worker` 的既有入口不变；
    - **旧包不动**（名字带新时间戳 + 随机后缀；旧包时间与标识都不改）；
    - 发布前复核采纳身份未变，变了抛 `RuntimeError("version changed")`（不静默混版）；
    - 落盘走**临时文件 + 原子替换**，同秒两次重包不会互相覆盖。
    """
    import os as _os
    import uuid as _uuid
    import zipfile as _zf
    from pathlib import Path as _P
    ws = _P(ws_dir) if ws_dir else workspace.task_workspace(task_id)
    store = VersionStore(ws, task_id)
    adopted = store.adopted()
    if adopted is None:
        raise LookupError("该任务没有可打包的采纳版本")
    snap = dict(snapshot or {})
    reports = ws / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    if snap:
        if str(snap.get("report_version_id") or "") != adopted.identity_id():
            raise RuntimeError("version changed")
        frozen = {str(arc): bytes(blob)
                  for arc, blob in (snap.get("payload") or {}).items()}
        if not frozen.get("reports/report.md"):
            if not md_bytes:
                raise LookupError("快照里没有交付正文")
            frozen["reports/report.md"] = bytes(md_bytes)
        if pdf_bytes and not frozen.get("reports/report.pdf"):
            frozen["reports/report.pdf"] = bytes(pdf_bytes)
        # 工作区副本仍写出（页面下载读它），但**包内内容只认快照字节**
        (reports / "report.md").write_bytes(frozen["reports/report.md"])
        if frozen.get("reports/report.pdf"):
            (reports / "report.pdf").write_bytes(frozen["reports/report.pdf"])
        manifest = _manifest_from_frozen(frozen, snap=snap, ws=ws)
        members: list[tuple[str, bytes]] = list(frozen.items())
    else:
        mdb = bytes(md_bytes)
        (reports / "report.md").write_bytes(mdb)
        files: list[tuple[_P, str]] = [(reports / "report.md", "reports/report.md")]
        pdf_name = ""
        if pdf_bytes:
            (reports / "report.pdf").write_bytes(bytes(pdf_bytes))
            files.append((reports / "report.pdf", "reports/report.pdf"))
            pdf_name = "reports/report.pdf"
        for name, arc in (("working_paper.json", "working_paper.json"),
                          ("working_paper.csv", "working_paper.csv")):
            for cand in (ws / "project" / name, ws / name):
                if cand.is_file():
                    files.append((cand, arc))
                    break
        charts_dir = ws / "charts"
        if charts_dir.is_dir():
            for p in sorted(charts_dir.glob("*.png")):
                files.append((p, f"charts/{p.name}"))
        # 完整模型稿（审计留档，按内容 hash 命名）：存在的每一版都进包（不覆盖历史）
        for cand_dir in (ws / "project", ws):
            if not cand_dir.is_dir():
                continue
            for p in sorted(cand_dir.glob("model_report_full_*.md")):
                files.append((p, f"audit/{p.name}"))
            break
        manifest = package_manifest(task_id, ws, files, pdf_name=pdf_name)
        members = []
        for abs_path, arc in files:
            try:
                members.append((arc, _P(abs_path).read_bytes()))
            except Exception as exc:             # noqa: BLE001 - 读不到编不出，如实抛
                raise RuntimeError(f"打包读取失败：{arc}：{str(exc)[:80]}") from exc
    # 发布前最后复核：身份没变才发布（变了说明期间发生修订 → 不静默混版）
    if str(store.adopted().identity_id()) != str(manifest.get("report_version_id") or ""):
        raise RuntimeError("version changed")
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"deliverables_{ts}_{_uuid.uuid4().hex[:6]}.zip"
    zip_path = ws / name
    tmp_path = ws / f".{name}.tmp"
    with _zf.ZipFile(tmp_path, "w", _zf.ZIP_DEFLATED) as zf:
        for arc, blob in members:
            zf.writestr(arc, blob)
        zf.writestr("PACKAGE_MANIFEST.json",
                    json.dumps(manifest, ensure_ascii=False, indent=1))
    _os.replace(tmp_path, zip_path)          # 原子发布
    # 自检：包内字节 vs 清单 hash，**再对一次快照 hash**（两版内容不得混进同一包）
    verify: dict[str, str] = {}
    try:
        with _zf.ZipFile(zip_path) as zf:
            for arc, want in (manifest.get("files") or {}).items():
                if arc not in zf.namelist():
                    verify[arc] = "missing"
                    continue
                got = hashlib.sha256(zf.read(arc)).hexdigest()
                if got != want:
                    verify[arc] = "mismatch"
                    continue
                _fz = (snap.get("frozen") or {}).get(arc)
                verify[arc] = "ok" if (not _fz or _fz == got) else "snapshot_mismatch"
    except Exception as exc:                     # noqa: BLE001 - 自检失败如实报告
        verify["__error__"] = str(exc)[:120]
    return {"package": zip_path.name, "path": str(zip_path),
            "files": [a for a, _b in members], "manifest": manifest,
            "verify": verify,
            "bytes": zip_path.stat().st_size}


def citation_evidence_payload(task_id: str, *, material: dict | None = None) -> dict | None:
    """包内最小引用证据的**内容**：已准入、带定位的摘录 + 坐标口径 + 文本 hash。

    F：报告正文承诺了"定位说明"，包里却只有工作区路径——离线解包后应能凭
    source/locator/text hash 取回同一摘录。只写**实际采用**的记录，不搬整个快照。
    `material`（叙事证据载荷）传入时**按它生成**——导出快照据此把资料冻结成一份，
    打包不再重读工作区（项2）。
    """
    try:
        if material is None:
            import narrative_evidence as _ne
            material = _ne.read(task_id) or {}
        payload = material or {}
        recs = [r for r in (payload.get("records") or [])
                if isinstance(r, dict) and r.get("has_location")
                and str(r.get("admission") or "") in ("admitted", "comparison")]
        if not recs:
            return None
        out = {
            "schema": "weavemind.citation_evidence/1",
            "task_id": task_id,
            "location_kind": "api_chunk" if any(r.get("chunk") for r in recs) else "char_range",
            "chunk_offsets": payload.get("chunk_offsets") or [],
            # R1：逐文档映射——每条引用按**它自己那份文档**换算段内偏移，
            # 不共用"最长的那张表"（多文档时 B 文档会被按 A 的片段号换算而错位）。
            "chunk_offsets_by_doc": payload.get("chunk_offsets_by_doc")
            or _chunk_maps_from_records(recs, payload.get("chunk_offsets") or []),
            "chunk_gaps": payload.get("chunk_gaps") or [],
            "chunk_gaps_by_doc": payload.get("chunk_gaps_by_doc") or {},
            "note": ("定位口径：`locator` 里的字符区间是**合并文档偏移**（本任务为多段公告"
                     "文本拼接，每段 5000 字符，段起止见 `chunk_offsets_by_doc[url]`，"
                     "缺失片段见 `chunk_gaps_by_doc[url]`）；`chunk` 是该偏移落在的接口"
                     "片段号，`chunk_char_start/end` 为**段内**偏移（按该记录自己的文档"
                     "换算）。没有 PDF 页码映射时不写页码。`after_gap` 表示该摘录位于"
                     "缺口之后（与缺口前内容**不连续**，标题不继承）。离线解包后可用 "
                     "url + locator + text_sha256 取回同一条摘录。"),
            "records": [{
                "kind": r.get("kind"), "title": r.get("title"), "url": r.get("url"),
                "locator": r.get("locator"), "chunk": r.get("chunk"),
                "doc_char_start": r.get("char_start"), "doc_char_end": r.get("char_end"),
                "chunk_char_start": _chunk_local(r, _map_for(r, payload))[0],
                "chunk_char_end": _chunk_local(r, _map_for(r, payload))[1],
                "crosses_chunk": _chunk_local(r, _map_for(r, payload))[2],
                "crosses_gap": bool(r.get("crosses_gap")),
                "after_gap": bool(r.get("after_gap")),
                "missing_chunks": list(r.get("missing_chunks") or []),
                "segments": list(r.get("segments") or []),
                "text": str(r.get("snippet") or r.get("text") or ""),
                "text_sha256": hashlib.sha256(
                    str(r.get("snippet") or r.get("text") or "").encode("utf-8")).hexdigest(),
                "content_hash": r.get("content_hash"),
                "admission": r.get("admission"),
            } for r in recs],
        }
        return out
    except Exception as exc:                     # noqa: BLE001 - 证据生成不了不阻断
        logger.warning("引用证据生成失败（task=%s）：%s", task_id, str(exc)[:120])
        return None


def _write_citation_evidence(task_id: str, ws) -> "Path | None":
    """把引用证据写到工作区（旧入口；快照路径直接用 `citation_evidence_payload`）。"""
    import json as _json
    try:
        out = citation_evidence_payload(task_id)
        if out is None:
            return None
        p = Path(ws) / "project" / "citation_evidence.json"
        p.write_text(_json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        return p
    except Exception as exc:                     # noqa: BLE001 - 证据写不出不阻断打包
        logger.warning("引用证据生成失败（task=%s）：%s", task_id, str(exc)[:120])
        return None


def _map_for(rec: dict, payload: dict) -> list:
    """该记录**自己那份文档**的片段映射（缺则退回全局表，并如实如此）。"""
    by_doc = payload.get("chunk_offsets_by_doc") or {}
    url = str(rec.get("url") or "")
    if url and isinstance(by_doc.get(url), list) and by_doc[url]:
        return by_doc[url]
    return payload.get("chunk_offsets") or []


def _chunk_maps_from_records(recs, fallback_map) -> dict:
    """没有逐文档表时（旧载荷）：按记录的 url 分组复用全局表，避免静默错配。"""
    out: dict = {}
    for r in recs or []:
        url = str(r.get("url") or "")
        if url and url not in out:
            out[url] = list(fallback_map or [])
    return out


def _chunk_start(doc_offset, chunk_offsets) -> int | None:
    """文档偏移 → 所在片段的**段起点**（用于换算段内偏移）。"""
    start = None
    for pos, _no in (chunk_offsets or []):
        if int(doc_offset) >= int(pos):
            start = int(pos)
        else:
            break
    return start


def _chunk_local(rec: dict, chunk_offsets) -> tuple[int | None, int | None, bool]:
    """(段内起点, 段内终点, 是否跨片段)：两段偏移都相对**起点所在片段**。

    跨片段的区间不能只报一个段内终点（会读成"段内 3996-194"这种倒挂数字）——
    用 `crosses_chunk=True` 标明，终点按起点片段计（可能 > 段长，如实反映跨段）。
    """
    try:
        cs = int(rec.get("char_start"))
        ce = int(rec.get("char_end"))
    except Exception:
        return None, None, False
    start = _chunk_start(cs, chunk_offsets)
    if start is None:
        return None, None, False
    nxt = None
    for pos, _no in (chunk_offsets or []):
        if int(pos) > start:
            nxt = int(pos)
            break
    crosses = bool(nxt is not None and ce > nxt)
    return cs - start, ce - start, crosses


def with_draft_note(report: str, reason: str) -> str:
    """未验收草稿必须写在**交付物本体**上（页面/导出都看得到），且幂等。"""
    if not reason or "未验收草稿" in str(report or ""):
        return report
    note = (f"> **未验收草稿：{reason}**\n"
            "> 本次交付没有取得针对该版正文的验收通过，不得视为已通过，"
            "也不得直接用于对外发布或审批。\n\n")
    text = str(report or "")
    if SEPARATOR in text:
        head, _, tail = text.partition(SEPARATOR)
        return f"{note}{head}{SEPARATOR}{tail}"
    return note + text


def rewrite_report_links(report: str, task_id: str) -> str:
    r"""把报告 Markdown 链接目标里的任务工作区绝对路径改写成
    /files/<task_id>/ URL（图表/数据图片在浏览器里才能显示）；
    正文里的绝对路径（如"成果文件夹"）保持不变。

    工作区改写后，剩余的图片链接若仍为 Windows 盘符绝对路径
    （如 ![](C:\...\charts\xxx.png)），进一步重写为相对路径
    charts/xxx.png，避免交付报告残留本机绝对路径。
    """
    ws = str(_ws(task_id))
    ws_bs = ws.replace("/", "\\")
    seg = f"/files/{task_id}"

    # /files/ 只提供 reports/ 与 charts/ 两子目录（web_ui._files）：
    # 只有这两种目标改写为 /files/<tid>/ URL；其余子目录（data/project 等）
    # 改写为工作区相对路径，避免交付报告出现 404 死链。
    def _rewrite_to(url: str, old: str, new: str) -> str:
        rel = url.replace(old, new).replace("\\", "/").lstrip("/")
        if rel.startswith(("reports/", "charts/")):
            return f"{seg}/{rel}"
        return rel

    def _fix_target(m):
        t = m.group(2)
        if t.startswith(ws_bs):
            t = _rewrite_to(t, ws_bs, "")
        elif t.startswith(ws):
            t = _rewrite_to(t, ws, "")
        return m.group(1) + t.replace("\\", "/") + m.group(3)

    report = re.sub(r"(\]\()([^)\s]+)(\))", _fix_target, report)

    def _fix_windows_image(m):
        target = m.group(2)
        if not re.match(r"^[A-Za-z]:[\/]", target):
            return m.group(0)
        rel = re.sub(r"^[A-Za-z]:[\/]", "", target).replace("\\", "/")
        parts = rel.split("/")
        if len(parts) >= 2 and parts[-2] in ("charts", "data", "reports", "project"):
            rel = f"{parts[-2]}/{parts[-1]}"
        else:
            rel = parts[-1]
        return f"{m.group(1)}{rel}{m.group(3)}"

    return re.sub(r"(!\[[^\]]*\]\()([^)\s]+)(\))", _fix_windows_image, report)


def strip_auto_notes(text: str) -> str:
    """剥掉自动注记块（草稿/评审/硬门槛/预算），保留人工写的交付说明。

    只用于**旧任务**（没有 `delivery_wrapper.md`）：修订重装时不能把上一版的
    "未验收草稿：<旧理由>" 带进新稿。
    """
    out: list[str] = []
    in_note = False
    for line in str(text or "").splitlines():
        if any(line.startswith(p) for p in _AUTO_NOTE_PREFIXES):
            in_note = True
            continue
        if in_note:
            if line.startswith("> ") or line.startswith(">"):
                continue                      # 注记块的后续说明行
            if not line.strip():
                in_note = False               # 块后空行：结束
                continue
            in_note = False
        out.append(line)
    return "\n".join(out).strip("\n")


# ── 评审事实（持久化；web_ui 与编排器都能算）──────────────────


def review_required() -> bool:
    """评审是否"必需"：银行口径（`WEAVEMIND_IDENTITY_MODE=bank`）下必需。

    与编排器 `_review_is_required()` 同源（配置事实，不是实例状态），因此另一个进程
    （web_ui）也能算出同一个答案。
    """
    try:
        from orchestrator_v2 import OrchestratorV2
        return bool(OrchestratorV2._review_is_required(object.__new__(OrchestratorV2)))
    except Exception:
        try:
            from task_context import VALID_MODES, default_mode
            mode = str(default_mode() or "")
            return mode in VALID_MODES and mode == "bank"
        except Exception:
            return True          # 判不出来按"必需"（保守，不放行）


def read_review_facts(task_id: str, ws_dir=None) -> dict:
    """评审事实：工作区文件 + **当场从配置算**的"是否必需"。

    `required` 不读文件：它是配置事实（身份模式），文件里那份可能是别的模式/别的时刻
    写的——让旧文件决定"要不要评审"会让必需评审被静默绕过。
    """
    facts: dict = {"verdict": "NONE", "label": "", "degraded_reason": "",
                   "report_version_id": ""}
    try:
        p = _ws(task_id, ws_dir) / REVIEW_STATE_FILE
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                facts.update({k: data.get(k, facts[k]) for k in facts})
    except Exception:
        pass
    facts["required"] = review_required()
    return facts


def write_review_facts(task_id: str, facts: dict, ws_dir=None) -> None:
    """落盘评审事实（含**该 PASS 属于哪一版**）。写失败只记日志，不影响主线。"""
    try:
        payload = dict(facts or {})
        payload.setdefault("task_id", task_id)
        payload["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (_ws(task_id, ws_dir) / REVIEW_STATE_FILE).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        logger.warning("评审状态落盘失败（task=%s）：%s", task_id, str(exc)[:100])


def review_valid_for(version, facts: dict) -> tuple[bool, str]:
    """该版是否满足"必需评审"。

    **旧评审不迁移**：评审 PASS 只对当时那一版正文有效——正文被修订后就是新版本，
    必须重新评审（个人模式本来不要求评审，因此不受影响）。
    """
    required = bool((facts or {}).get("required"))
    verdict = str((facts or {}).get("verdict") or "NONE")
    rid = str((facts or {}).get("report_version_id") or "")
    if not required:
        return True, ""
    if version is None:
        return False, "没有选中版本，无法确认评审归属"
    if verdict == "PASS" and rid and rid == version.identity_id():
        return True, ""
    if verdict == "PASS" and not rid:
        return False, "评审 PASS 未记录所属版本：无法证明覆盖本版（需重新评审）"
    return False, "本版未取得评审 PASS（旧评审不迁移）"


# ── 研究硬门槛（可重算；返回原因而不写实例状态）────────────────


def _has_analysis_section(report_body: str) -> bool:
    """研究简报里是否有**实质**分析（`## 分析` 一节非占位、有内容）。

    只对带 `## 分析` 小节的代码装配简报判定；通用报告没有固定小节名，不在本门槛内。
    小节收尾按**简报自己的标题表**（模型的分析正文自带 `## ` 小标题，按任意标题截会误判为空）。
    """
    body = str(report_body or "")
    if "## 分析" not in body:
        return True
    try:
        import report_brief
        section = report_brief._brief_section(body, "## 分析")
    except Exception:
        idx = body.find("## 分析")
        section = body[idx + len("## 分析"):].strip()
    if _ANALYSIS_PLACEHOLDER in section:
        return False
    if _ENGINEERING_SUMMARY_RE.search(section):
        return False          # 工程收尾报告（步骤成功数）不是分析
    return len(section) >= 60


_ANALYSIS_PLACEHOLDER = "本次未产出可交付的分析正文"
_ENGINEERING_SUMMARY_RE = re.compile(
    r"^##\s*Task Report\s*$|^\s*Steps:\s*\d+\s*\(\d+\s*OK,\s*\d+\s*failed\)", re.M)


def apply_research_hard_gate(task_id: str, goal: str, wp: dict | None,
                             report_body: str = "") -> tuple[str, str]:
    """研究任务的交付硬门槛。返回 `(附加到交付物的说明, hard_fail 原因)`。

    判定只对**研究任务**生效：存在研究契约（落库的或目标里解析出的公司研究请求），
    或本次确实产出了底稿。任何校验异常按"证据未知"处理（fail closed）。
    """
    notes: list[str] = []
    hard_fail = ""
    try:
        import task_state as _ts
        from facts import (ResearchRequest, parse_research_request,
                           research_shaped, research_subject)
        raw = (_ts.read_task(task_id) or {}).get("research_request") or {}
        request = ResearchRequest.from_payload(raw)
        if request is None:
            fallback = parse_research_request(goal, identity_source="gate-fallback")
            # 自由文本兜底：说不清期间/口径的请求不据此判门槛（判据与固定路径同源）
            request = fallback if research_shaped(fallback) else None
    except Exception as exc:
        request = None
        hard_fail = f"研究契约读取异常：{str(exc)[:120]}"
        logger.warning("研究契约读取异常（task=%s）：%s", task_id, str(exc)[:120])
    # 门槛口径比路径选择宽：有明确主体即算（严格版 research_shaped 用于选固定路径），
    # 或者本次确实产出了底稿——"已有底稿"本身就是研究任务的独立证据
    has_contract = research_subject(request)
    paper_present = bool(wp and wp.get("ok"))
    if not (has_contract or paper_present):
        if hard_fail:
            return f"> **研究交付硬门槛未通过**：{hard_fail}", hard_fail
        return "", ""

    reasons: list[str] = []
    try:
        if wp is None or wp.get("skipped"):
            reasons.append("研究任务未取得结构化事实：底稿缺失，本次不得判为已验证")
        elif not paper_present:
            reasons.append(f"底稿产出失败：{str((wp or {}).get('reason') or '')[:120]}")
        else:
            if not wp.get("paper_ok"):
                problems = wp.get("problems") or []
                gaps = [g for g in (wp.get("gaps") or []) if g.get("kind") == "fact"]
                head = (problems or gaps)
                detail = "；".join(
                    str((p.get("detail") if isinstance(p, dict) else p) or "")[:80]
                    for p in head[:3])
                reasons.append(f"底稿未达标（{len(problems)} 项问题 / {len(gaps)} 项必需事实缺口）：{detail}")
            if request is not None and report_body:
                from working_paper import WorkingPaper, document_subject_scope
                paper = WorkingPaper(request=request)
                paper.rows = list(wp.get("rows_detail") or [])
                scope = document_subject_scope(report_body, request, paper)
                if scope:
                    reasons.append("文档主体作用域未绑定：" + scope[0].detail[:120])
            # 研究简报必须有**可交付的分析**：只有数据表与底稿时按草稿交付
            # （架构复核：正文失效时交付"数据表/底稿 + 分析未完成"，不以拼接日志冒充研报）
            if report_body and not _has_analysis_section(report_body):
                reasons.append("分析未完成：交付正文只有数据与底稿，"
                               "未产出可交付的分析结论（见文末资料缺口）")
    except Exception as exc:
        reasons.append(f"研究校验异常（按证据未知交付）：{str(exc)[:140]}")
        logger.warning("研究校验异常（task=%s）：%s", task_id, str(exc)[:160])
    if not reasons:
        return "", hard_fail
    hard_fail = reasons[0]
    logger.warning("研究交付硬门槛触发（task=%s）：%s", task_id, hard_fail[:160])
    notes.append("> **研究交付硬门槛未通过**：" + hard_fail)
    for extra in reasons[1:]:
        notes.append("> " + extra)
    notes.append("> 交付状态不得判为已验证；请按上述缺口补齐后重跑或人工修订。")
    return "\n".join(notes), hard_fail


# ── 验收（含自动修复、产物落盘、按身份绑定）──────────────────


def _ws(task_id: str, ws_dir=None):
    """工作区目录：调用方给了就用它（web_ui 传入自己解析的路径，便于替换与对账）。"""
    return Path(ws_dir) if ws_dir else workspace.task_workspace(task_id)


def research_candidate_body(task_id: str, goal: str, body: str, *,
                            project: str | None = None,
                            ws_dir=None) -> str | None:
    """研究任务：**先确定性装配最终候选稿**（关键数据/来源编号/声明由代码给出）。

    用途：报告步骤的验收要针对**同一版候选稿**，否则模型会被要求重做装配器本就会
    补齐的东西（实机读数：第一次验收 fail 是"缺少免责声明"，而装配器必定写
    `### 免责声明`），触发整稿重生成。这里只装配、不登记版本、不记录交付。
    """
    try:
        import report_brief
        if not report_brief.is_research_task(task_id, goal, ws_dir=ws_dir):
            return None
        structure = report_brief.build_structure(task_id, goal, body,
                                                project=project, ws_dir=ws_dir)
        if not structure:
            return None
        candidate = report_brief.render_brief_markdown(structure, body, task_id=task_id)
        return rewrite_report_links(candidate, task_id) or candidate
    except Exception as exc:                     # noqa: BLE001 - 装配失败退回原正文
        logger.warning("研究候选稿装配失败（task=%s，退回原正文）：%s",
                       task_id, str(exc)[:160])
        return None


def accept_for_body(task_id: str, goal: str, body: str = "", *,
                    trigger: str = "报告步骤", prefer_body: bool = True,
                    hooks: dict | None = None, ws_dir=None) -> dict | None:
    """对**指定正文**跑确定性验收，并把结论绑到该正文所属的版本。

    `hooks`（都可选，编排器用它保持既有行为）：
    - `cancelled()`：返回 True 则跳过（取消是终态，不再产生副作用）；
    - `on_summary(summary)`：把缺口摘要推给前端；
    - `iteration`：写进审计事件的轮次；
    - `on_failure(result, report)`：验收 fail 后的沉淀钩子（评测集生长）。
    """
    hooks = hooks or {}
    try:
        if hooks.get("cancelled") and hooks["cancelled"]():
            logger.info("任务已取消，跳过验收（task=%s, trigger=%s）", task_id, trigger)
            return None
        _t0 = time.time()
        _repaired = False
        from acceptance_checker import run_acceptance
        from workspace import task_reports_dir
        rpath = task_reports_dir(task_id) / "report.md"
        if prefer_body and str(body or "").strip():
            report = str(body)
        elif rpath.exists():
            report = rpath.read_text(encoding="utf-8")
        elif str(body or "").strip():
            report = str(body)
        else:
            return None
        # F2′-3：研究任务的**模型草稿**先装配最终候选稿，再对**同一版**验收——代码负责的
        # 声明/编号/链接不触发整稿模型重生成（否则验收会要求模型重做装配器的工作）。
        # 人工修订重验（trigger="人工修订重验"）不得替换用户正文：那会丢掉他的改动。
        if str(trigger or "") != "人工修订重验":
            candidate = research_candidate_body(task_id, goal, report,
                                                project=None, ws_dir=ws_dir)
            if candidate and candidate != report:
                logger.info("验收对象改为代码装配的候选稿（task=%s，%d→%d 字符）",
                            task_id, len(report), len(candidate))
                report = candidate
        result = run_acceptance(task_id, goal, report, _ws(task_id, ws_dir))
        # 虚假标注确定性修复：把"把叙述片段当来源"的句子降级为诚实披露后复检
        try:
            sl = (result.get("checks") or {}).get("source_labeling") or {}
            mis = list(sl.get("mislabeled") or [])
            if not sl.get("pass") and mis:
                from acceptance_checker import auto_repair_source_labels
                repaired = auto_repair_source_labels(report, mis)
                if repaired != report:
                    rpath.write_text(repaired, encoding="utf-8")
                    report = repaired
                    _repaired = True
                    result = run_acceptance(task_id, goal, repaired, _ws(task_id, ws_dir))
        except Exception as exc:
            logger.warning("来源标注自动修复失败（task=%s）：%s", task_id, str(exc)[:120])
        # 来源清单 URL 存活校验（仅提示，不改 overall）
        try:
            import os
            if os.environ.get("URL_HEALTH_CHECK", "1") != "0":
                from acceptance_checker import extract_source_list
                from adapters.url_health import check_urls
                src_urls = [e.get("url") for e in extract_source_list(report) if e.get("url")]
                if src_urls:
                    dead = [u for u, st in check_urls(src_urls).items() if st == "dead"]
                    if dead:
                        hint = f"来源链接失效: {len(dead)} 条"
                        result.setdefault("checks", {})["url_health"] = {
                            "pass": True, "hint": True,
                            "details": hint + "（仅提示，不影响验收结论）",
                            "dead_count": len(dead), "dead_urls": dead[:10],
                        }
                        if hint not in result.get("gaps", []):
                            result.setdefault("gaps", []).append(hint)
        except Exception:
            pass
        # 落盘：快照（覆盖写）+ 审计事件（追加），两者都在绑定之前完成
        try:
            (_ws(task_id, ws_dir) / "acceptance_report.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
            _store = VersionStore(_ws(task_id, ws_dir), task_id)
            _src_fp = sources_fingerprint(task_id, report)
            _acc = {
                "overall": result.get("overall"),
                "gaps": result.get("gaps") or [],
                "report_sha256": result.get("report_sha256") or "",
                "report_sha256_short": result.get("report_sha256_short") or "",
                "rules_version": result.get("rules_version") or "",
                "rules_fingerprint": result.get("rules_fingerprint") or "",
            }
            _bound = _store.bind_acceptance(
                _acc, sources_fingerprint=_src_fp,
                rules_fingerprint=_acc["rules_fingerprint"])
            if _bound is None and len(str(_acc["report_sha256"])) >= 64:
                # 验收发生在正文被采纳之前：先按**验收对象**登记该版（同一身份）再绑
                _rules_v, _rules_fp = rules_identity(task_id)
                _store.record(
                    report, sources_fingerprint=_src_fp,
                    rules_version=_acc["rules_version"] or _rules_v,
                    rules_fingerprint=_acc["rules_fingerprint"] or _rules_fp)
                _bound = _store.bind_acceptance(
                    _acc, sources_fingerprint=_src_fp,
                    rules_fingerprint=_acc["rules_fingerprint"])
            if _bound is None:
                logger.warning("验收无法绑到正文版本（task=%s, sha=%s）：该版按证据未知处理",
                               task_id, str(_acc["report_sha256"])[:16])
        except Exception as exc:
            logger.warning("验收产物落盘/绑定失败（task=%s）：%s", task_id, str(exc)[:120])
        try:
            from acceptance_checker import append_acceptance_event, build_acceptance_event
            _ev = build_acceptance_event(
                result, trigger=trigger,
                iteration=int(hooks.get("iteration") or 0),
                duration_ms=int((time.time() - _t0) * 1000))
            _ev["repaired"] = _repaired
            append_acceptance_event(task_id, _ev)
        except Exception:
            pass
        summary = "；".join(result.get("gaps") or []) or "验收通过"
        if hooks.get("on_summary"):
            try:
                hooks["on_summary"](summary)
            except Exception:
                pass
        logger.info("Acceptance(%s): overall=%s %s", task_id, result.get("overall"), summary)
        if result.get("overall") == "fail" and hooks.get("on_failure"):
            try:
                hooks["on_failure"](result, report)
            except Exception as exc:
                logger.warning("验收失败沉淀异常（已忽略）: %s", str(exc)[:100])
        # **被验收的那份正文**：自动修复会改写它，调用方据此让交付与验收对象同一份字节
        result["_accepted_body"] = report
        return result
    except Exception as exc:
        logger.warning("Acceptance check failed: %s", str(exc)[:150])
        return None


def ensure_body_accepted(task_id: str, goal: str, detail: str,
                         accept_fn=None, ws_dir=None) -> tuple[str, str]:
    """被采纳的交付正文要拿到**它自己**的验收（幂等）。

    返回 `(状态, 交付正文)`：`already`（本版已有）/`bound`（本次补验并绑定，
    正文可能是验收器修复过的版本）/`mismatch`（验收没能落到本版，按未知交付）/
    `skipped`（没有选中版本或正文为空）。
    """
    store = VersionStore(_ws(task_id, ws_dir), task_id)
    ver = store.adopted()
    if ver is None or not str(detail or "").strip():
        return "skipped", detail
    if ver.acceptance_for_this_body():
        return "already", detail
    accept = accept_fn or (lambda tid, goal, body: accept_for_body(
        tid, goal, body, trigger="最终装配", prefer_body=True, ws_dir=ws_dir))
    res = accept(task_id, goal, detail)
    accepted = str((res or {}).get("_accepted_body") or detail)
    after = store.adopted()
    if after is not None and after.acceptance_for_this_body():
        return "bound", accepted
    if accepted != detail:
        v_fix = store.find_by_body(accepted)
        if v_fix is not None and v_fix.acceptance_for_this_body():
            store.adopt(v_fix, reason="交付正文采用验收修正版")
            return "bound", accepted
    logger.warning("最终装配正文未取得本版验收（task=%s）：交付按证据未知处理", task_id)
    return "mismatch", accepted


# ── 装配 + 判定 + 记录交付（唯一顺序）────────────────────────


def assemble_and_verify(task_id: str, goal: str, body: str, *,
                        wrapper: str = "", project: str | None = None,
                        accept_fn=None, extra_notes=(),
                        paper: dict | None = None,
                        review_facts: dict | None = None, ws_dir=None,
                        contract: dict | None = None) -> dict:
    """按固定顺序装配交付正文、判定状态并记录交付 hash。

    顺序（编排器收尾与 web_ui 修订必须逐字节一致）：
    确保本版验收 → 底稿 + 研究硬门槛 → `交付说明 + 分隔符 + 正文` + 注记 →
    链接重写 → 唯一谓词判状态 → 未通过加草稿注记 → `record_delivery`。

    `review_facts`：编排器传它**本次运行**的评审结论（内存态），本函数在版本定下来之后
    把它连同 `report_version_id` 落盘——这样"PASS 属于哪一版"是事实而不是推断；
    不传则读工作区里已有的评审事实（人工修订走这条：没有针对新版的 PASS）。

    `contract`：执行契约（wire 或 `{"wire": ...}`）——研究状态按它的必答问题逐项裁决，
    并把契约指纹写进状态的绑定对象（C-5）。
    """
    store = VersionStore(_ws(task_id, ws_dir), task_id)
    # F1：研究任务改用**代码装配的研究简报**作为交付正文（关键发现/财务对照/图表/
    # 引用编号/资料范围/声明由代码输出，模型只贡献"分析"一节）；工程交付说明不再进
    # 研究简报正文。装配必须在验收之前——验收对象就是最终交付的那份正文。
    brief_note = ""
    try:
        import report_brief
        if report_brief.is_research_task(task_id, goal, ws_dir=ws_dir):
            structure = report_brief.build_structure(
                task_id, goal, body, project=project, ws_dir=ws_dir)
            if structure:
                brief_body = report_brief.render_brief_markdown(structure, body, task_id=task_id)
                # 链接重写必须在验收**之前**：验收对象就是最终交付的那份字节，
                # 装配后若再改写链接，记录的验收对象与送达字节就对不上了
                brief_body = rewrite_report_links(brief_body, task_id)
                # 简报是**新版本**：登记并采纳它（编排器先前采纳的是模型正文，
                # 不切换过来的话"选中版本"与交付字节会是两版 → 只能判草稿）
                _src_fp = sources_fingerprint(task_id, brief_body)
                _rules_v, _rules_fp = rules_identity(task_id)
                _v_brief = store.record(
                    brief_body, sources_fingerprint=_src_fp,
                    rules_version=_rules_v, rules_fingerprint=_rules_fp)
                if _v_brief is not None:
                    store.adopt(_v_brief, reason="研究简报（代码装配）")
                # 结构对象与每条主张**绑定本版正文**：修订后页面不会拿旧结构冒充新版
                report_brief.stamp_structure_version(
                    structure, str(getattr(_v_brief, "version_id", "") or ""))
                report_brief.write_structure(task_id, structure, ws_dir=ws_dir)
                body = brief_body
                wrapper = ""             # 工程说明移到任务详情，不进简报
                brief_note = ("研究简报由代码装配（关键数据/来源/声明）；"
                              f"采用来源 {len(structure.get('citations') or [])} 条")
                logger.info("研究简报装配（task=%s）：%s", task_id, brief_note)
    except Exception as exc:                     # noqa: BLE001 - 装配失败退回原正文
        logger.warning("研究简报装配失败（task=%s，退回原正文）：%s", task_id, str(exc)[:160])
    _st, body = ensure_body_accepted(task_id, goal, body, accept_fn=accept_fn,
                                     ws_dir=ws_dir)
    # 09-23：投影必须与**最终采纳正文**同源。`ensure_body_accepted` 可能换掉采纳正文
    # （人工修订路径：验收采纳的是用户修订正文，而结构是按装配候选盖的版本号）。
    # 处理：结构记着自己的来源正文 hash——来源就是当前采纳正文时**重盖版本号**；
    # 来源是别的正文时按采纳正文**重建投影**。不再用"结构≠正文"一句话糊过去。
    try:
        import report_brief as _rb
        _st_proj = _rb.read_structure(task_id, ws_dir=ws_dir)
        _adopted_now = store.adopted()
        _adv = str(getattr(_adopted_now, "version_id", "") or "")
        if _st_proj is not None and _adv:
            _src_body = str(_st_proj.get("source_body_sha256") or "")
            if _src_body and _src_body != _adv:
                _rebuilt = _rb.build_structure(task_id, goal, body,
                                               project=project, ws_dir=ws_dir)
                if _rebuilt:
                    _rb.stamp_structure_version(_rebuilt, _adv)
                    _rb.write_structure(task_id, _rebuilt, ws_dir=ws_dir)
                    logger.info("结构投影按采纳正文重建（task=%s，来源 %s→%s）",
                                task_id, _src_body[:12], _adv[:12])
            else:
                _rb.stamp_structure_version(_st_proj, _adv)
                _rb.write_structure(task_id, _st_proj, ws_dir=ws_dir)
    except Exception as exc:                     # noqa: BLE001 - 投影同步失败不阻断交付
        logger.warning("结构投影与采纳正文同步失败（task=%s）：%s", task_id, str(exc)[:140])

    wp = paper if paper is not None else write_working_paper(
        task_id, goal, project=project)
    facts = dict(review_facts) if review_facts is not None else read_review_facts(task_id)
    facts.setdefault("required", review_required())
    _v_for_review = store.adopted()
    if review_facts is not None:
        facts["report_version_id"] = (_v_for_review.identity_id()
                                      if _v_for_review is not None else "")
        write_review_facts(task_id, facts, ws_dir=ws_dir)

    notes: list[str] = []
    # 批次3b：研究状态（与数字机器验收分开的一条轴）——在验收/硬门槛之前算，注记进交付
    # 说明（MD 与 PDF 封面共用），文件落盘供页面与导出清单读
    #
    # C-5（09-22 复核）：状态必须绑定**最终采纳正文**。`ensure_body_accepted` 可能已经
    # 换掉采纳正文，而结构对象可能是上一版留下的（本次实机：结构与采纳正文确为不同
    # hash）——因此这里带上采纳版本与契约指纹，结构版本与采纳正文不一致时按"待重验"
    # 显示，不沿用旧计数。
    _structure_for_state = None
    try:
        import report_brief as _rb
        _structure_for_state = _rb.read_structure(task_id, ws_dir=ws_dir)
    except Exception:
        _structure_for_state = None
    _state_version = store.adopted()
    # 契约入参三种形状都要认：封装 `{"wire": {...}}`、`{"contract": {...}}`，以及
    # **直接给 wire**（扁平 dict）——此前只认前两种，传了扁平 wire 会静默丢契约。
    _contract_wire = None
    if isinstance(contract, dict):
        _contract_wire = contract.get("wire") or contract.get("contract")
        if not isinstance(_contract_wire, dict) or not _contract_wire:
            _looks_like_wire = any(
                k in contract for k in ("company", "company_id", "periods",
                                        "caliber", "doc_type", "required_metrics",
                                        "subject_type", "market", "as_of"))
            _contract_wire = contract if _looks_like_wire else None
    try:
        rstate = research_state(task_id, goal, _structure_for_state, ws_dir=ws_dir,
                                contract_wire=_contract_wire, version=_state_version)
        write_research_state(task_id, rstate, ws_dir=ws_dir)
        _rstate_note = research_state_note(rstate)
        if _rstate_note:
            notes.append(_rstate_note)
    except Exception as exc:                     # noqa: BLE001 - 研究状态算不出来不阻断交付
        rstate = {}
        logger.warning("研究状态判定失败（task=%s）：%s", task_id, str(exc)[:120])
    # C3：**装配说明不进正文**——"研究简报由代码装配（关键数据/来源/声明）"是工程说明，
    # 读者要的是结论与证据；它改为写进任务详情（structure 的 assembly_note）与日志。
    if brief_note:
        try:
            _st_now = report_brief.read_structure(task_id, ws_dir=ws_dir) or {}
            _st_now["assembly_note"] = brief_note
            report_brief.write_structure(task_id, _st_now, ws_dir=ws_dir)
        except Exception as exc:                     # noqa: BLE001 - 详情写不进去不影响交付
            logger.warning("装配说明写入任务详情失败（task=%s）：%s",
                           task_id, str(exc)[:120])
    wp_note = gaps_note(wp)
    if wp_note:
        notes.append(wp_note)
    gate_note, hard_fail = apply_research_hard_gate(task_id, goal, wp, body)
    if gate_note:
        notes.append(gate_note)
    review_note = review_note_text(facts)
    if review_note:
        notes.append(review_note)
    for extra in (extra_notes or ()):
        if str(extra or "").strip():
            notes.append(str(extra))

    head = str(wrapper or "").strip("\n")
    for note in notes:
        head = (head + "\n\n" + note) if head else note
    report = f"{head}{SEPARATOR}{body}" if head else str(body)
    report = rewrite_report_links(report, task_id)

    version = store.adopted()
    review_ok, review_why = review_valid_for(version, facts)
    status, why = verified_delivery(
        version, body,
        review_valid=review_ok,
        hard_ok=not bool(hard_fail),
        hard_reason=str(hard_fail or ""),
    )
    if status != DELIVERY_VERIFIED and not why:
        why = review_why or "未取得该版验收"
    if status != DELIVERY_VERIFIED:
        report = with_draft_note(report, why)
    entry = store.record_delivery(report, accepted_body=body,
                                  ok=(status == DELIVERY_VERIFIED), reason=why)
    logger.info("交付装配（task=%s）：status=%s version=%s", task_id, status,
                str(getattr(version, "version_id", ""))[:12])
    return {
        "report": report, "body": body, "status": status, "reason": why,
        "hard_fail": hard_fail, "review_valid": review_ok, "review_reason": review_why,
        "identity_id": (version.identity_id() if version is not None else ""),
        "version_id": (version.version_id if version is not None else ""),
        "acceptance_overall": (version.acceptance_overall() if version is not None else ""),
        "accepted_body_matches": bool(version is not None
                                      and version.acceptance_for_this_body()),
        "delivered_sha256": entry.get("delivered_sha256", ""),
        "research_state": rstate,
        "paper": wp,
    }


def review_note_text(facts: dict) -> str:
    """评审状态写进交付说明（PASS 不加噪声；降级/未评审要显式写明）。"""
    verdict = str((facts or {}).get("verdict") or "NONE")
    if verdict == "PASS":
        return ""
    reason = str((facts or {}).get("degraded_reason") or "未执行或未完成评审")
    label = ("未经评审（critic 关闭）" if "critic 已关闭" in reason
             else "评审未完成（降级）")
    return (f"> **评审状态：{label}** —— {reason}。\n"
            "> 本交付物未取得绑定计划版本的评审 PASS，须经人工复核后方可使用。")


def write_wrapper(task_id: str, wrapper: str) -> None:
    """把交付说明（分隔符之前的部分）落盘，供人工修订逐字节复用。"""
    try:
        (workspace.task_workspace(task_id) / WRAPPER_FILE).write_text(
            str(wrapper or ""), encoding="utf-8")
    except Exception as exc:
        logger.warning("交付说明落盘失败（task=%s）：%s", task_id, str(exc)[:100])


def read_wrapper(task_id: str, delivered: str = "", old_body: str = "") -> tuple[str, str]:
    """取交付说明：优先落盘文件；旧任务回退到"从交付正文里剥离旧正文与自动注记"。

    返回 `(wrapper, 来源说明)`；来源说明会写进交付物，便于人工判断这份修订是
    在什么基础上装配的。

    09-23：研究任务的读者主文不该顶着整段任务指令——落盘的 wrapper 若就是原始任务
    指令（长文且带"阅读重点/每个数字须能回溯"这类提示语），换成一句面向读者的抬头；
    任务指令仍在任务页与审计记录里可查，不减信息。
    """
    p = workspace.task_workspace(task_id) / WRAPPER_FILE
    wrapper = ""
    source = "derived"
    try:
        if p.exists():
            wrapper = p.read_text(encoding="utf-8").strip("\n")
            source = "stored"
    except Exception:
        pass
    if not wrapper:
        text = str(delivered or "")
        if old_body and old_body in text:
            text = text.replace(old_body, "", 1)
        elif SEPARATOR in text:
            text = text.split(SEPARATOR, 1)[0]
        wrapper = strip_auto_notes(text)
    if _looks_like_task_instruction(wrapper):
        return ("> 本文件为公司研究简报；完整任务指令、检索与工程细节见任务页与审计记录。\n"
                "> 每个数字的来源与算式见文末附录（可复算底稿）。", source)
    return wrapper, source


def _looks_like_task_instruction(text: str) -> bool:
    """交付说明是不是"整段任务指令"（研究任务把它移出读者主文）。"""
    t = str(text or "")
    if len(t) < 200:
        return False
    markers = ("阅读重点", "每个数字须能回溯", "bank_corporate", "研究契约",
               "缺证据的如实标缺口", "生成研究目标")
    return any(m in t for m in markers)


# ── 读取口径（页面 / 导出 / manifest 共用）──────────────────


def delivery_state(task_id: str, delivered_text: str = "", ws_dir=None) -> dict:
    """该任务的**交付状态**（唯一读取口径）。

    输入全部可重算或已持久化：选中版本、最后一次交付记录、评审事实（含所属版本）、
    研究硬门槛（读 `financials.json` + 契约重算，不写文件）。因此页面、导出清单、
    验收详情拿到的是同一个结论。
    """
    ws = _ws(task_id, ws_dir)
    store = VersionStore(ws, task_id)
    version = store.adopted()
    if version is None:
        return {"status": DELIVERY_UNKNOWN, "draft": True, "verified": False,
                "draft_reason": "无选中版本", "version_id": "", "identity_id": "",
                "acceptance_overall": "", "accepted_body_matches": False,
                "aligned": None, "delivered_sha256": "", "delivered_at": 0}
    facts = read_review_facts(task_id, ws_dir=ws_dir)
    review_ok, review_why = review_valid_for(version, facts)
    hard_fail = ""
    try:
        import task_state as _ts
        from working_paper_export import build_result
        goal = str((_ts.read_task(task_id) or {}).get("goal") or "")
        wp = build_result(task_id, goal)
        _note, hard_fail = apply_research_hard_gate(task_id, goal, wp, version.body)
    except Exception as exc:
        hard_fail = f"硬门槛重算异常：{str(exc)[:120]}"
    status, why = verified_delivery(
        version, version.body,
        review_valid=review_ok,
        hard_ok=not bool(hard_fail),
        hard_reason=str(hard_fail or ""),
    )
    if status != DELIVERY_VERIFIED and not why:
        why = review_why or "未取得该版验收"
    deliveries = store.deliveries()
    last = deliveries[-1] if deliveries else {}
    delivered = body_hash(delivered_text) if delivered_text else ""
    same_version = bool(last) and str(last.get("report_version_id") or "") == version.identity_id()
    aligned = None
    if not delivered:
        # 没有可核对的交付正文（空正文）：不得判已验证
        if status == DELIVERY_VERIFIED:
            status, why = "draft", "尚无交付正文（无法核对导出字节）"
    else:
        if not last:
            # 没有任何交付记录：**无法核对**导出字节是不是那份交付 → 不得判已验证
            if status == DELIVERY_VERIFIED:
                status, why = "draft", "尚无交付记录（无法核对导出字节）"
        else:
            aligned = bool(same_version
                           and str(last.get("delivered_sha256") or "") == delivered)
            if not aligned and status == DELIVERY_VERIFIED:
                status = "draft"
                why = ("导出字节与收尾记录的交付正文不一致（装配后未重验）"
                       if same_version else "交付记录属于其他版本（无法核对本版导出字节）")
            elif aligned and status == DELIVERY_VERIFIED and not last.get("ok"):
                status = "draft"
                why = str(last.get("reason") or "收尾时已判为未验收草稿")
    return {
        "status": status, "draft": status != DELIVERY_VERIFIED,
        "verified": status == DELIVERY_VERIFIED,
        "draft_reason": "" if status == DELIVERY_VERIFIED else str(why),
        "version_id": version.version_id,
        "identity_id": version.identity_id(),
        "acceptance_overall": version.acceptance_overall(),
        "accepted_body_matches": bool(version.acceptance_for_this_body()),
        "aligned": aligned,
        "hard_fail": hard_fail,
        "review_valid": review_ok,
        "delivered_sha256": str(last.get("delivered_sha256") or ""),
        "delivered_at": float(last.get("at") or 0),
    }
