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

from report_version import (DELIVERY_DRAFT, DELIVERY_UNKNOWN, DELIVERY_VERIFIED,
                            VersionStore,
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
        # R2：判定逻辑（逐问题评估/规则表/验收规则源码）的指纹——改了逻辑就不该继续判"当前"
        "logic_fingerprint": str(st.get("logic_fingerprint") or ""),
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
        # R2：判定逻辑源码变化同样失效（新逻辑可能改变同一个问题的结论）
        if str(b.get("logic_fingerprint") or "") != str(structure.get("logic_fingerprint") or ""):
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
        _lf, _lf_cur = (str(b.get("logic_fingerprint") or ""),
                        str(st.get("logic_fingerprint") or ""))
        if _lf != _lf_cur:
            reasons.append(f"判定逻辑变化（指纹 {_lf[:12] or '空'} → {_lf_cur[:12] or '空'}）")
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
        # 括号里写**覆盖程度**，不写材料类别标签——"分解覆盖充分"这类类名跟在"部分覆盖"
        # 后面自相矛盾（C2：完成度与材料类别分开说，页面上两处也各说各的）。
        _p = (f"（其中部分覆盖 {len(partial_mandatory)} 项："
              + "、".join(f"{q.get('question')}[已取得部分构成]"
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


def read_acceptance_summary(task_id: str, *, ws_dir=None,
                            with_authority: bool = False) -> dict | None:
    """读工作区的验收快照（文件口径）；没有就返回 None，不编。

    P0-2b（验收只允许一个权威来源）：`with_authority=True` 时在**读取时**判定这份快照
    是不是当前采纳/交付那一版的验收，并给出唯一的权威结论：
    - `authoritative=True`：这份就是采纳版的验收，`effective_overall`/`effective_gaps` 即它；
    - `authoritative=False`：`effective_*` 换成**采纳版自己那份**验收（`authoritative_*`），
      并给出 `acceptance_conflict`（两套数字各是什么、页面该认哪一套），同时留 WARNING。
    默认 `False` 保持旧行为（`rules_identity` 每次验收都读它，不该因此刷日志）。
    """
    try:
        p = (Path(ws_dir) if ws_dir else workspace.task_workspace(task_id)) \
            / "acceptance_report.json"
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        out = {
            "overall": data.get("overall") or "",
            "gaps": data.get("gaps") or [],
            "rules_version": data.get("rules_version") or "",
            "rules_fingerprint": data.get("rules_fingerprint") or "",
            "profile": data.get("profile") or "",
            "report_sha256": data.get("report_sha256") or "",
            # 快照自述（谁在什么时候验收了哪一版）
            "task_id": data.get("task_id") or "",
            "trigger": data.get("trigger") or "",
            "body_chars": int(data.get("body_chars") or 0),
            "is_adopted_version": data.get("is_adopted_version"),
            "evaluated_at": data.get("evaluated_at") or "",
        }
        if not with_authority:
            out["effective_overall"] = out["overall"]
            out["effective_gaps"] = list(out["gaps"])
            return out
        _adopted = VersionStore(Path(ws_dir) if ws_dir else
                                workspace.task_workspace(task_id), task_id).adopted()
        _sha = out["report_sha256"]
        _adopted_sha = str(getattr(_adopted, "version_id", "") or "")
        _same = bool(_sha and _adopted_sha and _sha == _adopted_sha)
        out["adopted_body_sha256"] = _adopted_sha
        out["authoritative"] = bool(_same or not _adopted_sha)
        if out["authoritative"]:
            out["effective_overall"] = out["overall"]
            out["effective_gaps"] = list(out["gaps"])
            return out
        _own = dict(getattr(_adopted, "acceptance", {}) or {})
        out["authoritative_overall"] = str(_own.get("overall") or "")
        out["authoritative_gaps"] = list(_own.get("gaps") or [])
        out["effective_overall"] = out["authoritative_overall"]
        out["effective_gaps"] = list(out["authoritative_gaps"])
        _chars_txt = (f"{out['body_chars']} 字符" if out["body_chars"]
                      else "字符数未记录")
        out["acceptance_conflict"] = (
            f"工作区验收快照绑的是正文 {_sha[:12]}（{_chars_txt}，"
            f"overall={out['overall']}，缺口 {len(out['gaps'])} 个，来自「{out['trigger']}」）；"
            f"当前采纳/交付的那版是 {_adopted_sha[:12]}（{len(str(getattr(_adopted, 'body', '')))}"
            f" 字符，overall={out['authoritative_overall']}，缺口 "
            f"{len(out['authoritative_gaps'])} 个）。页面/导出/下载只认采纳版那套"
            f"（`effective_*`），另一套只是候选稿的验收留痕。")
        logger.warning("验收快照与采纳版不是同一版（task=%s）：%s",
                       task_id, out["acceptance_conflict"][:260])
        return out
    except Exception:
        return None


def rules_identity(task_id: str) -> tuple[str, str]:
    """本次验收的规则版本与指纹（供版本身份使用）。

    R2：返回的指纹里并入**判定逻辑身份**（逐问题评估模块与验收规则的源码指纹）——
    只认"未更新的版本标签"会让改了语义规则的代码继续用旧绑定（复核反例：
    rules_version 不变、rules_fingerprint 变了仍判 current）。
    """
    summary = read_acceptance_summary(task_id)
    if summary and (summary.get("rules_version") or summary.get("rules_fingerprint")):
        return str(summary.get("rules_version") or ""), str(summary.get("rules_fingerprint") or "")
    try:
        from acceptance_checker import ACCEPTANCE_RULES_VERSION, rules_fingerprint
        return str(ACCEPTANCE_RULES_VERSION), str(rules_fingerprint())
    except Exception:
        return "", ""


def logic_fingerprint() -> str:
    """判定/渲染逻辑的源码指纹：这些文件改了，任何绑定都不该继续判"当前"。

    覆盖：逐问题评估（`question_assessment.py`）、规则表与装配（`report_brief.py`）、
    验收规则（`acceptance_checker.py`）。文件名+内容一起哈希（改名也算变化）。
    """
    import hashlib as _h
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent
    h = _h.sha256()
    for name in ("question_assessment.py", "report_brief.py", "acceptance_checker.py"):
        p = root / name
        try:
            h.update(name.encode("utf-8"))
            h.update(p.read_bytes())
        except Exception:                        # noqa: BLE001 - 读不到就跳过（不编内容）
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def sources_fingerprint(task_id: str, report_text: str = "") -> str:
    """本轮**来源/事实快照指纹**（版本身份的一部分）。

    只取真正的来源通道与报告里的来源清单——不含账簿类文件（`report_versions.json` 等），
    否则同一正文在不同时刻算出的指纹不同，身份会漂移。

    任务24：**失效来源不参与指纹**。工作区 `dead_sources.json` 列出的 URL 在计算前
    从各来源通道与来源清单块里剔除——报告生成器会在这些行尾追加"⚠️ 链接已失效"，
    标注动作本身不得改变版本身份（否则同一份稿子"标注前/标注后"指纹漂移，
    采纳绑定与交付包身份判定全乱）。
    """
    try:
        from acceptance_checker import _collect_sources, dead_source_urls
        parts: list[str] = []
        try:
            _ws = workspace.task_workspace(task_id)
        except Exception:
            _ws = None
        try:
            _dead_urls = [u for u in dead_source_urls(_ws) if u] if _ws else []
        except Exception:
            _dead_urls = []

        def _drop_dead(text: str) -> str:
            """剔除命中失效 URL 的整行（标注只加在该行，去掉行即去掉标注）。"""
            if not _dead_urls:
                return text
            kept = [ln for ln in str(text or "").splitlines()
                    if not any(u in ln for u in _dead_urls)]
            return "\n".join(kept)

        try:
            src = _collect_sources(_ws) or {} if _ws else {}
        except Exception:
            src = {}
        for name in sorted(src):
            text = _drop_dead(str(src.get(name) or ""))
            parts.append(f"{name}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}")
        text = str(report_text or "")
        blocks = re.findall(
            r"(?m)^#+\s*(?:参考来源|来源清单|参考资料|数据来源|来源附录)\s*$([\s\S]{0,2000})",
            text)
        for b in blocks[:3]:
            parts.append("srclist:" + hashlib.sha256(
                _drop_dead(b).strip().encode("utf-8")).hexdigest()[:16])
        if not parts:
            return ""
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return ""


def _norm_text_sha(text: str) -> str:
    """**文本** hash（CRLF/CR 先归一成 LF）。工作区/包内文件常是 CRLF，而交付登记与
    验收按 LF 文本算 hash；不归一就会把"同一份正文"算成两份（实测 `ed8cf52b` vs
    `94b594a2`）。"""
    t = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def delivery_projection(task_id: str, *, ws_dir=None) -> dict:
    """**本版正文身份**（唯一取处）：优先交付登记，其次结构对象里盖的章。

    P0-2b 的落点：装配时把身份写进 `report_versions.json.deliveries[].projection` 与
    `report_structure.json.delivery_projection`，页面 / 下载 / 包内清单 / 状态判定都从
    这一个函数取，不再各自数各自的字节。取不到返回 `{}`（不编）。
    """
    try:
        dels = VersionStore(_ws(task_id, ws_dir), task_id).deliveries()
        if dels:
            proj = (dels[-1] or {}).get("projection")
            if isinstance(proj, dict) and proj:
                return dict(proj)
    except Exception:                            # noqa: BLE001 - 读不到再看结构
        pass
    try:
        import report_brief
        return report_brief.read_delivery_projection(task_id, ws_dir=ws_dir)
    except Exception:
        return {}


def projection_vs_package(task_id: str, *, ws_dir=None, package: str = "",
                          md_text: str = "", md_sha256: str = "") -> dict:
    """包内/工作区主文与**本版交付身份**是否同一份（可核对谓词，不是提示语）。

    `match` 的三种成立方式（都要身份里记着的那两个 hash 之一）：
    ① 文件**字节** hash == 登记的 `delivered_sha256`（同一份交付字节）；
    ② 文件**文本** hash（CRLF 归一）== `delivered_sha256`；
    ③ 文件文本 hash == `body_sha256`（主文就是这份研究正文本身，未加交付外壳）。
    拿不到身份或拿不到文件 → `unknown`（不判死，也不冒充一致）。
    """
    out: dict = {"verdict": "unknown", "reason": "", "package": str(package or ""),
                 "md_sha256": str(md_sha256 or ""), "projection": {}}
    proj = delivery_projection(task_id, ws_dir=ws_dir)
    out["projection"] = proj
    if not proj:
        out["reason"] = "没有交付身份（装配未登记 projection）：无法核对包内主文与页面同版"
        return out
    txt = str(md_text or "")
    if not txt.strip() and not md_sha256:
        out["reason"] = "没有可比对的包内主文"
        return out
    _deliv = str(proj.get("delivered_sha256") or "")
    _body = str(proj.get("body_sha256") or "")
    _txt_sha = _norm_text_sha(txt) if txt else ""
    out.update({
        "md_text_sha256": _txt_sha, "md_chars": len(txt),
        "delivered_sha256": _deliv, "delivered_chars": int(proj.get("delivered_chars") or 0),
        "body_sha256": _body, "body_chars": int(proj.get("body_chars") or 0),
    })
    if (md_sha256 and _deliv and md_sha256 == _deliv) \
            or (_txt_sha and _txt_sha in (_deliv, _body)):
        out["verdict"] = "match"
        return out
    _seen = md_sha256 or _txt_sha
    out["verdict"] = "mismatch"
    out["reason"] = (
        f"包内 {out['package'] or 'reports/report.md'}（{_seen[:12]}，{out['md_chars']} 字符）"
        f"不等于收尾交付正文（{_deliv[:12]}，{out['delivered_chars']} 字符），也不等于本版"
        f"研究正文（{_body[:12]}，{out['body_chars']} 字符）：屏幕版/下载版与包内版不是同一份，"
        f"该包不得当当前稿下载（重新导出即生成同版包）")
    return out


def package_statuses(task_id: str, *, ws_dir=None) -> dict:
    """工作区里的交付包**逐个**判身份：`current`（= 当前采纳稿）/ `historical` / `unknown`。

    为什么必须逐个判（K2，2026-09-29 夜验收）：此前页面只取"最新的那个 zip"，再拿**最新的
    export_manifest** 去解释它——实机 `ui-603f626cbe` 采纳身份 `f585edbf…`，唯一 ZIP 却是
    `81047fdf…`（内文 4751 字节，非当前稿）：清单说 verified，包却是旧稿，等于用清单给旧包
    背书。这里只认**包内 `PACKAGE_MANIFEST.json` 的身份**与当前采纳版对比，时间戳不参与判定
    （时间只用于排序）。
    """
    import zipfile as _zf
    from pathlib import Path as _P
    ws = _P(ws_dir) if ws_dir else workspace.task_workspace(task_id)
    adopted_id = ""
    try:
        adopted = VersionStore(ws, task_id).adopted()
        adopted_id = adopted.identity_id() if adopted is not None else ""
    except Exception as exc:                     # noqa: BLE001 - 身份读不到按 unknown
        logger.warning("读取采纳身份失败（task=%s）：%s", task_id, str(exc)[:120])
    out: list[dict] = []
    for p in sorted(ws.glob("deliverables_*.zip")):
        item = {"name": p.name, "bytes": 0, "mtime": 0.0, "identity": "",
                "body_sha256": "", "status": "unknown", "reason": ""}
        man: dict = {}                           # 本轮包内清单（缺清单就是空，不借上一个包）
        try:
            item["bytes"] = int(p.stat().st_size)
            item["mtime"] = float(p.stat().st_mtime)
        except Exception:                        # noqa: BLE001
            pass
        try:
            with _zf.ZipFile(p) as zf:
                if "PACKAGE_MANIFEST.json" in zf.namelist():
                    man = json.loads(zf.read("PACKAGE_MANIFEST.json").decode("utf-8")) or {}
                    item["identity"] = str(man.get("report_version_id") or "")
                    item["body_sha256"] = str(man.get("research_body_sha256")
                                              or man.get("body_sha256") or "")
                    item["inputs_recomputable"] = bool(
                        (man.get("analysis_inputs") or {}).get("recomputable"))
                else:
                    item["reason"] = "包内没有 PACKAGE_MANIFEST.json（旧包，无法核对身份）"
                # P0-1/P0-4：包内主文必须**就是**收尾交付的那份字节（或本版研究正文本身）。
                # 自动打包步在装配**之前**跑，装的是当时磁盘上的 `reports/report.md`——
                # 屏幕版/下载版与包内版因此可能各是一份（实测 ui-8b28c95ab1：页面 5825 字、
                # 包内 23493 字节、工作区又是第三份）。这里按交付登记的身份**当场核对**。
                if "reports/report.md" in zf.namelist():
                    try:
                        _raw = zf.read("reports/report.md")
                        item["md_sha256"] = hashlib.sha256(_raw).hexdigest()
                        item["md_chars"] = len(_raw.decode("utf-8", errors="replace"))
                        _pv = projection_vs_package(
                            task_id, ws_dir=ws, package="reports/report.md",
                            md_text=_raw.decode("utf-8", errors="replace"),
                            md_sha256=item["md_sha256"])
                        item["projection_verdict"] = str(_pv.get("verdict") or "")
                        if _pv.get("reason"):
                            item["projection_reason"] = str(_pv.get("reason"))
                    except Exception as exc:     # noqa: BLE001 - 读不出主文按未知处理
                        item["projection_verdict"] = "unknown"
                        item["projection_reason"] = f"包内主文不可读：{str(exc)[:60]}"
                # R2：清单身份对得上还**不够**——同一次导出里包内冻结的选择记录也必须
                # 是这一版（实测三一 `375af7`：manifest 23b61bee… 而包内
                # `analysis/selection.json.adopted_identity=d290ab01…`）。旧包字节不动，
                # 但**不得再当"当前包"下载**：身份不一致即历史包。
                if "analysis/selection.json" in zf.namelist():
                    try:
                        _sel = json.loads(
                            zf.read("analysis/selection.json").decode("utf-8")) or {}
                        item["selection_identity"] = str(
                            _sel.get("adopted_identity") or "")
                    except Exception as exc:         # noqa: BLE001 - 解析不了如实标记
                        item["selection_identity"] = ""
                        item["reason"] = item["reason"] or f"包内选择记录不可读：{str(exc)[:60]}"
        except Exception as exc:                 # noqa: BLE001 - 坏包如实标
            item["reason"] = f"包不可读：{str(exc)[:80]}"
        _sel_id = str(item.get("selection_identity") or "")
        _sel_conflict = bool(adopted_id and _sel_id and _sel_id != adopted_id)
        # P0-1/P0-4：包内主文与本版交付身份对不上 → 屏幕版/下载版与包内版不是同一份，
        # 该包不得当"当前稿"下载（结论在读取时按交付登记算，老包同样受约束）。
        _proj_conflict = str(item.get("projection_verdict") or "") == "mismatch"
        if not adopted_id:
            item["status"] = "unknown"
            item["reason"] = item["reason"] or "该任务没有采纳版本，无法判定同版"
        elif _sel_conflict:
            item["status"] = "historical"
            item["reason"] = (f"包内选择记录 {_sel_id[:12]} != 当前采纳 "
                              f"{adopted_id[:12]}：这一版导出与选择记录不同源，"
                              f"旧包保留但不得当当前稿下载（重新导出即生成同版包）")
        elif _proj_conflict:
            item["status"] = "historical"
            item["reason"] = str(item.get("projection_reason")
                                 or "包内主文与收尾交付正文不是同一份")
        elif item["identity"] and item["identity"] == adopted_id:
            item["status"] = "current"
            item["reason"] = ""
        else:
            item["status"] = "historical"
            item["reason"] = (item["reason"] or
                              f"包内身份 {item['identity'][:12] or '缺失'} != 当前采纳 "
                              f"{adopted_id[:12]}：历史/过期包，不得当当前稿下载")
        out.append(item)
    cur = None
    # 与采纳稿同版的包**可能不止一个**（每次重新导出都会新增一份）：取**最新**的那份
    # （时间戳排序只用于在同版包之间选最新，不参与身份判定）。
    _current = [i for i in out if i["status"] == "current"]
    if _current:
        cur = max(_current, key=lambda i: (float(i.get("mtime") or 0), i["name"]))
    return {"adopted_identity": adopted_id, "packages": out,
            "current": cur["name"] if cur else "",
            "has_current": bool(cur),
            "note": ("" if cur else "没有任何包与当前采纳稿同版（需重新导出）")}


def current_package(task_id: str, *, ws_dir=None):
    """当前采纳稿对应的包路径（没有则 `None`）——**下载路径的单一真源**。"""
    st = package_statuses(task_id, ws_dir=ws_dir)
    if not st.get("current"):
        return None
    ws = Path(ws_dir) if ws_dir else workspace.task_workspace(task_id)
    p = ws / str(st["current"])
    return p if p.is_file() else None


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
    # P0-2b：包内清单必须带上**本版正文身份**（交付登记 projection）与"包内主文是否
    # 就是那一份"的可核对结论。此前清单只写"打包时刻的采纳身份"，于是同一个包里
    # "采纳身份=半截稿 / 主文字节=另一版完整稿"两件事并存却看不出矛盾（实测 ui-8b28c95ab1）。
    try:
        _proj = delivery_projection(task_id, ws_dir=ws)
        out["delivery_projection"] = _proj
        _md_text = ""
        if _md:
            for abs_path, arc in (files or []):
                if str(arc) == _md:
                    try:
                        _md_text = _P(abs_path).read_text(encoding="utf-8")
                    except Exception:            # noqa: BLE001 - 读不出就按 hash 判
                        _md_text = ""
                    break
        _pv = projection_vs_package(task_id, ws_dir=ws, package=_md,
                                    md_text=_md_text,
                                    md_sha256=str(file_hashes.get(_md, "")))
        out["delivery_projection_verdict"] = str(_pv.get("verdict") or "unknown")
        if _pv.get("reason"):
            out["delivery_projection_reason"] = str(_pv.get("reason"))
        if _proj:
            out["delivery_status"] = str(_proj.get("status") or "")
            out["draft_reason"] = str(_proj.get("draft_reason") or "")
    except Exception as exc:                     # noqa: BLE001 - 身份核对失败不阻断打包
        logger.warning("包内清单：交付身份核对失败（task=%s）：%s", task_id, str(exc)[:120])
    _pdf = str(pdf_name or "")
    if not _pdf:
        # P0-f：这里**不能**取包内任意 PDF。实测样本 ui-a06a005c9b 的清单里
        # `pdf_sha256` 就是原始年报 `fetched/436928c6acaac793.pdf` —— 因为旧写法
        # `sorted(任意 .pdf)[0]` 下，`fetched/…` 按字母序排在 `reports/report.pdf` 前面，
        # 于是**原始披露 PDF 冒充了研究报告 PDF** 被交付出去。
        # 研究报告 PDF 的位置只认 `reports/report.pdf`；没有就如实留空（不拿别的顶上）。
        # 与 schema 2 构造器（本文件 `_pdf = "reports/report.pdf" if … else ""`）同一口径。
        _pdf = "reports/report.pdf" if "reports/report.pdf" in file_hashes else ""
    out["pdf"] = _pdf
    out["pdf_sha256"] = file_hashes.get(_pdf, "")
    if not _pdf:
        # 说明为什么空：调用方/读者据此区分"没有报告 PDF"与"忘了放"
        out["pdf_missing_reason"] = (
            "包内没有研究报告 PDF（reports/report.pdf）；"
            "原始披露 PDF 不占该位置")
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
    # 分析运行身份（Q1）：与快照路径同一构造器，两路清单口径一致
    out["analysis_runs"] = analysis_binding(ws)
    # 分析**输入**身份（K2）：可复算输入是否在包里、数据集 hash 与来源/契约是什么。
    # 只有 run 摘要 + dataset hash 时读者无法复算；这里把"输入在哪"一并写进清单。
    try:
        from financial_analysis import store as _fa_in
        _inputs = _fa_in.input_payload_bytes(ws) or {}
        _blob = _fa_in.load_inputs(ws)
        _ctx = dict(_blob.get("context") or {})
        # 分析**选择**（L0-b）：正文里用的是哪一条运行是用户选择，不是"最早/最新"。
        # 清单同时带上选择与逐条是否仍然可用，读者能核对"正文这一段是这一次运行"。
        _sel = _fa_in.selection_status(
            ws, dataset_hash=str(((_blob.get("dataset") or {}).get("dataset_hash")) or ""),
            rules_version=_rules_version())
        out["analysis_inputs"] = {
            "schema": _fa_in.SCHEMA_INPUTS,
            "present": sorted(_inputs),
            "missing": [arc for arc in _fa_in.ARC_INPUTS.values() if arc not in _inputs],
            "dataset_hash": str(((_blob.get("dataset") or {}).get("dataset_hash")) or ""),
            "dataset_source": _ctx.get("dataset_source") or {},
            "contract": _ctx.get("contract") or {},
            "recomputable": bool(_fa_in.ARC_INPUTS["dataset"] in _inputs),
            "selection": {
                "schema": _fa_in.SCHEMA_SELECTION,
                "entries": _sel.get("entries") or [],
                "ok": bool(_sel.get("ok")),
                "adopted_identity": str(_sel.get("adopted_identity") or ""),
                "note": ("有选择记录：正文分析卡按所选运行装配" if _sel.get("entries")
                         else "无选择记录：正文按默认（每模型第一条已验证运行）装配"),
            },
        }
    except Exception as exc:                     # noqa: BLE001 - 输入身份算不出不阻断打包
        logger.warning("包内清单：分析输入身份读取失败：%s", str(exc)[:120])
        out["analysis_inputs"] = {"schema": "weavemind.analysis_inputs/1",
                                 "present": [], "missing": [], "recomputable": False}
    return out


def _delivered_body_md(md_bytes: bytes) -> bytes | None:
    """交付正文 → 主文字节（任务22）；没有附录（完整版即主文）时返回 `None`。

    直接从**同一份交付正文**切分，不读工作区磁盘：打包/快照期间正文若发生修订，
    包内主文不会与 `reports/report.md` 错版（宁可不放，也不混两版）。
    """
    try:
        from report_quality import split_report_appendix
        body, appendix = split_report_appendix(bytes(md_bytes).decode("utf-8"))
    except Exception as exc:                     # noqa: BLE001 - 切不出就不放，不阻断打包
        logger.warning("交付主文拆分失败（按无附录处理）：%s", str(exc)[:120])
        return None
    if not appendix.strip() or not body.strip():
        return None
    return body.encode("utf-8")


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
        _body = _delivered_body_md(bytes(md_bytes))
        if _body is not None:
            payload["reports/report_body.md"] = _body
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
    # 分析运行（Q1）：把"正文/图引用的是哪一次运行"按**字节**冻结进包。
    # 没有运行记录就不放这个文件（不制造空文件、不改既有包的内容清单）。
    _analysis = analysis_payload_bytes(ws)
    if _analysis:
        payload["analysis/analysis_runs.json"] = _analysis
    # 可复算输入（K2）：dataset/plan/context —— 有 run 摘要没有输入，包就不可复算
    try:
        from financial_analysis import store as _fa_in
        for arc, blob in (_fa_in.input_payload_bytes(ws) or {}).items():
            payload[arc] = blob
        # 分析**选择**（L0-b）：包内留下"正文用的是哪条运行"的用户选择，离线可核对
        _sel_blob = _fa_in.selection_payload_bytes(ws)
        if _sel_blob:
            payload[_fa_in.ARC_SELECTION] = _sel_blob
    except Exception as exc:                     # noqa: BLE001 - 读不到就不进包
        logger.warning("快照：分析输入读取失败：%s", str(exc)[:120])
    # X0（10-02 实测）：**正文实际引用的分析底稿文本**也要进包。正文会写
    # 「完整分析卡……见 `analysis/analysis_cards.md`」，而快照路径此前只冻结
    # dataset/plan/context/runs/selection —— 包里因此出现过"正文指了一个包内不存在的
    # 路径"（12 页 PDF 引用 analysis_cards.md，包内没有该文件）。同一处把
    # `analysis_detail.md`/`continuation.md` 一并冻结（有才进包，不制造空文件）。
    try:
        _ana_dir = ws / "analysis"
        if _ana_dir.is_dir():
            for p in sorted(_ana_dir.glob("*.md")):
                try:
                    payload[f"analysis/{p.name}"] = p.read_bytes()
                except Exception as exc:         # noqa: BLE001 - 单个读不到不影响其余
                    logger.warning("快照：分析底稿读取失败（%s）：%s", p.name, str(exc)[:100])
    except Exception as exc:                     # noqa: BLE001
        logger.warning("快照：分析底稿目录读取失败：%s", str(exc)[:120])
    # Y2 量化读数（Y2b 接入）：以**附件**随包（`quant/*`），**不改写主文**（规划 §11.2）。
    # 同一套"有才进包、不制造空文件"契约 ⇒ 没有量化读数的工作区，包内成员一字不变。
    # 许可门结论写在 `quant/quant_manifest.json` 的 `external_delivery` 里，随包可离线核对。
    try:
        from quant_research import publish as _qpub
        _qmembers = _qpub.payload_bytes(ws) or {}
        for _arc, _blob in _qmembers.items():
            payload[_arc] = _blob
        if _qmembers:
            logger.info("快照：量化读数入包 %d 个成员（task=%s）", len(_qmembers), task_id)
    except Exception as exc:                     # noqa: BLE001 - 量化读数不可用不阻断导出
        logger.warning("快照：量化读数读取失败：%s", str(exc)[:120])
    return payload


# ---------------------------------------------------------------- §3D 外发投影
# 10-06 复核：内部快照留下无权外发的量化附件**可以成立**，但交付包是**对外交付面**——
# 没有外发许可的 `quant/*` 不得随包出门，同时**财报正文仍可交付**。判据一律复用既有
# 许可门（`quant_research.publish.external_delivery_gate`），不另写一套许可解释，
# 也不因为"我们内部看过了"就放行；衍生结论同样按用途许可判（不天然放行）。
EXTERNAL_NOTE_ARC = "quant/EXTERNAL_DELIVERY_RESTRICTION.md"


def _quant_arcs(members) -> list[str]:
    """包内的量化成员（不含限制说明本身）。"""
    return sorted(str(a) for a in (members or {})
                  if str(a).startswith("quant/") and str(a) != EXTERNAL_NOTE_ARC)


def _readings_from_members(members: dict) -> dict:
    """按**包内字节**还原读数（不重读磁盘：打包后磁盘变了不算，与整包同版口径一致）。"""
    from pathlib import PurePosixPath
    out: dict = {}
    for arc in _quant_arcs(members):
        if not arc.endswith(".json") or arc.endswith("/quant_manifest.json"):
            continue
        try:
            blob = json.loads(bytes(members[arc]).decode("utf-8"))
        except Exception:                        # noqa: BLE001 - 坏文件按"读不到"处理
            out.setdefault("_unreadable", []).append(arc)
            continue
        if isinstance(blob, dict):
            out[PurePosixPath(arc).stem] = blob
    return out


def _external_note(gate: dict, withheld: dict) -> str:
    lines = [
        "# 外发限制说明：受限读数附件已按许可门移出本包",
        "",
        "本交付包**不包含**受限读数附件（`quant/*`，以及带市场对照读数的 `analysis/*` 实验成员）。",
        "判据是既有的许可门 `quant_research.publish.external_delivery_gate()`（按每条读数自带的 "
        "`delivery_eligible`），市场对照读数按其 `market_comparison.delivery_eligible`（同一家族："
        "`event_returns` 按数据源许可算），不是代理侧自行放行。",
        "",
        f"- 结论：**不允许对外**。{gate.get('reason') or ''}",
        "- 被移出的成员（sha256 供内部核对，**字节不随包**）：",
    ]
    for arc, h in sorted((withheld or {}).items()):
        lines.append(f"  - `{arc}` `{h}`")
    lines += [
        "- **内部保存不受影响**：原件仍在工作区 `quant/` 与内部导出快照里，逐字节可核对。",
        "- **可交付部分不受影响**：财报正文 `reports/report.md`（及 PDF／底稿／图表）仍在本包内。",
        "- 对外交付量化结论须取得机构授权，或只交付取得授权的衍生结论；"
        "原始行情／事件／日历数据不得随包外发。",
        "",
    ]
    return "\n".join(lines)


def _market_delivery_blocked(members: dict) -> list[str]:
    """带**市场对照读数**的 `analysis/` 实验成员（C5-4）：按内容判，无 market 段零影响。

    判据＝每块 `market_comparison` 自带的 `delivery_eligible`（与 quant 许可门同一家族：
    `event_returns` 按数据源许可算），不是代理侧自行放行；这一位判不出/缺失 ⇒ 受限
    （fail closed）——受限数不得从 `analysis/`、实验 JSON 绕许可门外发，也不改许可口径。
    """
    blocked: list[str] = []
    for arc in sorted(str(a) for a in (members or {})):
        if not (str(arc).startswith("analysis/") and str(arc).endswith(".json")):
            continue
        try:
            blob = json.loads(bytes(members[arc]).decode("utf-8"))
        except Exception:                        # noqa: BLE001 - 坏文件按"没有市场段"处理
            continue
        if not (isinstance(blob, dict) and isinstance(blob.get("market_comparison"), dict)):
            continue
        if (blob.get("market_comparison") or {}).get("delivery_eligible") is not True:
            blocked.append(arc)
    return blocked


def external_delivery_projection(members: dict) -> tuple[dict, dict | None]:
    """**外发投影**：按现有许可判据决定量化附件能不能随交付包出门（复核 §3D）。

    - 没有量化附件、也没有带市场对照读数的 analysis 成员 ⇒ 原样返回、**不产任何结论**
      （`None`）：没有受限读数的工作区，包内成员一字不变（零存量影响契约不变）；
    - 判据复用 `quant_research.publish.external_delivery_gate()`（不另写一套许可解释）；
      C5-4：带 `market_comparison` 的 analysis/experiment 成员按每块自带的
      `delivery_eligible` 判（同一家族），`False`/判不出 ⇒ 同样移出本包；
    - **不允许对外**（含"判不出来"）⇒ 受限成员移出本包，换成一份限制说明
      （列出被移出成员与 sha256），其余成员原样保留；
    - 允许对外 ⇒ 原样保留，只在清单里记一条结论。
    """
    q = _quant_arcs(members)
    ma = _market_delivery_blocked(members)
    if not q and not ma:
        return dict(members or {}), None
    if q:
        try:
            from quant_research import publish as _qpub
            gate = _qpub.external_delivery_gate(_readings_from_members(members))
        except Exception as exc:                 # noqa: BLE001 - 判不出就 fail closed
            gate = {"allowed": False, "blocked_by": q,
                    "reason": f"许可门不可用（{str(exc)[:80]}）⇒ 不默认放行，量化附件不随包外发"}
    else:
        gate = {"allowed": False, "blocked_by": ma,
                "reason": ("带市场对照读数的 analysis 成员 `delivery_eligible=False`"
                           "（数据源许可仅内部试验）⇒ 不随包外发")}
    info: dict = {"schema": "weavemind.external_delivery/1", "applied": True,
                  "allowed": bool(gate.get("allowed")), "checked": q or ma,
                  "basis": ("quant_research.publish.external_delivery_gate" if q else
                            "market_comparison.delivery_eligible（event_returns 按数据源许可算）"),
                  "reason": str(gate.get("reason") or "")}
    out = dict(members or {})
    if gate.get("allowed") and not ma:
        return out, info
    withheld = {arc: hashlib.sha256(bytes(out[arc])).hexdigest()
                for arc in list(q) + list(ma) if arc in out}
    for arc in list(q) + list(ma):
        out.pop(arc, None)
    out[EXTERNAL_NOTE_ARC] = _external_note(gate, withheld).encode("utf-8")
    info["withheld"] = withheld
    info["note"] = EXTERNAL_NOTE_ARC
    return out, info


def analysis_payload_bytes(ws) -> bytes | None:
    """工作区里的分析运行记录（`financial_analysis.store`）→ 进包的字节。

    读不到/没装这个包都返回 `None`：交付链不因为"分析包不可用"而失败。
    """
    try:
        from financial_analysis import store as _fa_store
    except Exception:                            # noqa: BLE001
        return None
    try:
        return _fa_store.payload_bytes(Path(ws))
    except Exception as exc:                     # noqa: BLE001
        logger.warning("快照：分析运行记录读取失败：%s", str(exc)[:100])
        return None


def _rules_version() -> str:
    """当前独立验证规则集版本（取不到就空串 = 不比较，不假装匹配）。"""
    try:
        from financial_analysis.validation import RULES_VERSION
        return str(RULES_VERSION)
    except Exception:                            # noqa: BLE001
        return ""


def analysis_binding(ws) -> dict:
    """交付清单里的分析身份块（无运行时返回空块，键仍然存在，便于核对"没有"与"没读"）。"""
    try:
        from financial_analysis import store as _fa_store
        runs = _fa_store.load_runs(Path(ws))
        return _fa_store.binding_summary(runs)
    except Exception as exc:                     # noqa: BLE001
        return {"schema": "weavemind.analysis_runs/1", "count": 0, "validated": 0,
                "runs": [], "error": str(exc)[:120]}


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
        # 工作区里的自然位置优先（analysis/plan.json 这类二级目录成员此前被误报
        # missing_on_disk——文件明明在，只是映射只认 project/ 平铺两种布局）。
        cands = [ws / arc]
        if arc.startswith("charts/"):
            cands += [ws / "charts" / name]
        elif arc.startswith("audit/"):
            cands += [ws / "project" / name, ws / name]
        elif arc.startswith("evidence/"):
            cands += [ws / "project" / name, ws / name]
        else:
            cands += [ws / "project" / name, ws / name]
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


def _exclusion_audit_members(ws) -> list:
    """F1 剔除审计成员：工作区 `project/source_exclusions.json` → 包内
    `audit/source_exclusions.json`（按名存在才进包，没有就返回空列表）。

    单独成函数是为了能被测试直接调用并核对"确有纳入"，而不是只在长函数里
    一行 `files.append(...)`、无法验证。
    """
    try:
        p = Path(ws) / "project" / "source_exclusions.json"
        if p.is_file() and p.stat().st_size > 0:
            return [(p, "audit/source_exclusions.json")]
    except Exception as exc:                     # noqa: BLE001 - 不进包不影响主链
        logger.warning("打包：剔除审计读取失败：%s", str(exc)[:100])
    return []


def export_snapshot(task_id: str, *, ws_dir=None, delivered_text: str = "",
                    md_bytes: bytes = b"", pdf_bytes: bytes = b"",
                    goal: str = "") -> dict:
    """导出用**一次不可变快照**：采纳身份 + 正文 + 资料/规则指纹 + 逐成员字节。

    09-23：重包各环节此前各自重读"当前版本"——生产探针里 MD 取 A、修订切 B、PDF 取 B，
    仍返回 ok/verify_ok=true（文件自检只能证明写入字节未坏，证明不了语义同版）。
    R2：再加一条**受保护读取**——正文必须能证明是由**这一版**正文渲染出来的：
    交付正文取自页面/任务库时可能与版本库不同步（复核探针：包内正文 A + 身份 B 仍自检绿），
    这里用生产渲染入口把采纳正文重渲一次，交付正文必须与它一致（外壳可包住它）；
    对不上就抛 `RuntimeError("version changed")`，由调用方 409 重试，不静默混版。
    """
    from pathlib import Path as _P
    ws = _P(ws_dir) if ws_dir else workspace.task_workspace(task_id)
    store = VersionStore(ws, task_id)
    adopted = store.adopted()
    if adopted is None:
        raise LookupError("该任务没有可导出的采纳版本")
    body_text = str(getattr(adopted, "body", "") or "")
    text = str(delivered_text or "")
    # R2（受保护读取）：交付正文必须能证明属于**这一版**。三条证据任一成立即可：
    #   ⓪ 交付正文就是这一版采纳正文本身（恢复/采纳后页面交付的就是它）；
    #   ① 版本库的交付登记（装配时记下的 delivered_sha256 + 对应当前身份 + aligned）；
    #   ② 生产渲染入口能把采纳正文渲成交付正文（外壳可包住它）。
    # 都对不上 → version changed（调用方 409 重试），不把旧正文贴上新身份。
    binding_verified = False
    binding_evidence = ""
    if text:
        # 证据⓪（R2 同版的最强形式）：交付正文**就是**这一版采纳正文本身。
        # 恢复/采纳把投影换成采纳正文后（`_restore_version_state` → 投影），页面
        # 交付的正是它——直接判定同版，不再退到"重渲包含"或交付登记。
        try:
            if str(body_hash(text)) == str(getattr(adopted, "version_id", "") or ""):
                binding_verified = True
                binding_evidence = "same_as_adopted_body"
        except Exception as exc:                 # noqa: BLE001 - 哈希失败退到别的证据
            logger.warning("导出快照：交付正文哈希失败（task=%s）：%s", task_id, str(exc)[:80])
    if text and not binding_verified:
        try:
            for d in reversed(store.deliveries()):
                if str(d.get("report_version_id") or "") != adopted.identity_id():
                    continue
                if not d.get("aligned"):
                    continue
                if str(d.get("delivered_sha256") or "") == body_hash(text):
                    binding_verified = True
                    binding_evidence = "delivery_registry"
                else:
                    raise RuntimeError("version changed")
                break
        except RuntimeError:
            raise
        except Exception as exc:                 # noqa: BLE001 - 登记读不到就退到渲染核对
            logger.warning("导出快照：交付登记读取失败（task=%s）：%s", task_id, str(exc)[:120])
    _rendered = ""
    try:
        if goal:
            _rendered = research_candidate_body(task_id, goal, body_text, ws_dir=ws) or ""
    except Exception as exc:                     # noqa: BLE001 - 渲染失败不阻断，但记日志
        logger.warning("导出快照：按采纳正文重渲染失败（task=%s）：%s", task_id, str(exc)[:120])
    if _rendered:
        _n = lambda s: "".join(str(s).split())   # noqa: E731 - 只用于包含判断
        _same = (_n(_rendered) in _n(text)) or (_n(text) in _n(_rendered)) if text else False
        if text and not _same and not binding_verified:
            # 交付正文不是这一版正文渲染出来的（期间有修订或读到了旧投影）
            raise RuntimeError("version changed")
        if _same and not binding_verified:
            binding_verified = True
            binding_evidence = "rendered_containment"
        if not text:
            text = _rendered
            binding_verified = True
    if text and not binding_verified:
        # 两条证据都不成立：**fail closed**——宁可不导出，也不把无法证明归属的正文
        # 贴上当前身份（复核探针的缺陷正是"包内正文 A + 身份 B"仍自检绿）。
        raise RuntimeError("binding unverified")
    # R2（采纳→selection→冻结 同版）：选择记录的 `adopted_identity` 只在「分析采纳」
    # 入口写入，人工修订/重装配/检查点恢复换掉采纳版本后就落后——同一份包里 manifest
    # 的 `report_version_id` 是新身份、`analysis/selection.json` 还写着旧身份
    # （实测三一：manifest 23b61bee… vs 包内 selection d290ab01…）。这里在**冻结之前**
    # 回绑：正文仍绑定所选运行（run_id 出现在分析卡里，与采纳入口同一判据）才改，
    # 证明不了就如实记录不一致进快照（清单可见，不隐藏、不假成功）。
    _sel_binding: dict = {}
    try:
        from financial_analysis.store import rebind_selection_identity as _rbs
        _sel_binding = _rbs(ws, adopted.identity_id(),
                            body_text=text or body_text,
                            reason="导出快照按当前采纳版本回绑选择身份") or {}
        if _sel_binding.get("changed"):
            logger.info("导出快照：选择身份回绑 %s→%s（task=%s，凭 %s）",
                        str(_sel_binding.get("from") or "")[:12],
                        str(_sel_binding.get("to") or "")[:12],
                        task_id, str(_sel_binding.get("verified_by") or ""))
        elif not _sel_binding.get("consistent"):
            logger.warning("导出快照：选择身份未回绑（task=%s）：%s",
                           task_id, str(_sel_binding.get("reason") or "")[:140])
    except Exception as exc:                     # noqa: BLE001 - 回绑失败不阻断导出，但要可见
        logger.warning("导出快照：选择身份回绑失败（task=%s）：%s", task_id, str(exc)[:120])
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
        "body_text_sha256": body_hash(body_text),
        "delivered_sha256": body_hash(text) if text else "",
        # 未知依赖不伪装成已验证：三条绑定证据都没有时如实标 False（页面/清单可见）
        "binding_verified": bool(binding_verified),
        "binding_evidence": binding_evidence,
        "delivered_text": text,
        # R2：选择身份回绑记录（changed/consistent/reason）——同版证据链的一部分，
        # 页面与清单都能看到"选择记录是否与这一版同源"
        "selection_binding": dict(_sel_binding),
        "sources_fingerprint": sources_fingerprint(task_id, text),
        "rules_version": rv,
        "rules_fingerprint": rf,
        "logic_fingerprint": logic_fingerprint(),
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
    out["logic_fingerprint"] = str(snap.get("logic_fingerprint") or "")
    out["evidence_fingerprint"] = str(snap.get("evidence_fingerprint") or "")
    out["snapshot_captured_at"] = float(snap.get("captured_at") or 0.0)
    out["binding_verified"] = bool(snap.get("binding_verified"))
    out["delivered_sha256"] = str(snap.get("delivered_sha256") or "")
    # R2：选择身份回绑记录（若这次快照发生过回绑或发现不一致，如实写进清单）
    out["selection_binding"] = dict(snap.get("selection_binding") or {})
    out["frozen"] = dict(want)
    out["analysis_runs"] = analysis_binding(ws) if ws is not None else {
        "schema": "weavemind.analysis_runs/1", "count": 0, "validated": 0, "runs": []}
    # 分析**输入**身份（K2）：与普通路径同一构造器 —— 快照整包只认冻结字节，
    # 而"输入在不在包里"要按**冻结成员**判（磁盘上有没有是另一回事，不能当证据）
    if "analysis/dataset.json" in hashes or any(
            a.startswith("analysis/") for a in hashes):
        try:
            _ctx_blob = {}
            if "analysis/context.json" in (frozen or {}):
                _ctx_blob = json.loads(bytes(frozen["analysis/context.json"])
                                       .decode("utf-8")) or {}
            _ds_blob = {}
            if "analysis/dataset.json" in (frozen or {}):
                _ds_blob = json.loads(bytes(frozen["analysis/dataset.json"])
                                      .decode("utf-8")) or {}
            _present = sorted(a for a in hashes
                              if a in ("analysis/dataset.json", "analysis/plan.json",
                                       "analysis/context.json"))
            # 分析**选择**（L0-b）：从**冻结字节**里读，和输入同一口径（磁盘变了不算）
            _sel_blob = {}
            if "analysis/selection.json" in (frozen or {}):
                try:
                    _sel_blob = json.loads(bytes(frozen["analysis/selection.json"])
                                           .decode("utf-8")) or {}
                except Exception:                    # noqa: BLE001 - 坏文件按无选择
                    _sel_blob = {}
            out["analysis_inputs"] = {
                "schema": "weavemind.analysis_inputs/1",
                "present": _present,
                "missing": [a for a in ("analysis/dataset.json", "analysis/plan.json",
                                        "analysis/context.json") if a not in hashes],
                "dataset_hash": str(_ds_blob.get("dataset_hash") or ""),
                "dataset_source": _ctx_blob.get("dataset_source") or {},
                "contract": _ctx_blob.get("contract") or {},
                "recomputable": "analysis/dataset.json" in hashes,
                "selection": {
                    "schema": "weavemind.analysis_selection/1",
                    "entries": list(_sel_blob.get("entries") or []),
                    "adopted_identity": str(_sel_blob.get("adopted_identity") or ""),
                    "in_package": "analysis/selection.json" in hashes,
                    "note": ("包内带选择记录：正文分析卡按所选运行装配"
                             if _sel_blob.get("entries")
                             else "包内无选择记录：正文按默认（每模型第一条已验证运行）装配"),
                },
            }
        except Exception as exc:                     # noqa: BLE001 - 判不出按缺输入
            logger.warning("包内清单：分析输入身份读取失败（冻结路径）：%s", str(exc)[:120])
            out["analysis_inputs"] = {"schema": "weavemind.analysis_inputs/1",
                                     "present": [], "missing": [], "recomputable": False}
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
        if str(snap.get("body_sha256") or "") != str(
                getattr(adopted, "version_id", "") or ""):
            raise RuntimeError("version changed")
        frozen = {str(arc): bytes(blob)
                  for arc, blob in (snap.get("payload") or {}).items()}
        if not frozen.get("reports/report.md"):
            if not md_bytes:
                raise LookupError("快照里没有交付正文")
            frozen["reports/report.md"] = bytes(md_bytes)
        if pdf_bytes and not frozen.get("reports/report.pdf"):
            frozen["reports/report.pdf"] = bytes(pdf_bytes)
        # 工作区副本仍写出（页面下载读它），但**包内内容只认快照字节**。
        # 缺陷B（真机 E2E 复现）：此前只回写 report.md，工作区 report_body.md/
        # report_appendix.md 仍是成稿时的旧版 → 拆分件与完整版错版（16:17 稿
        # vs 16:56 重打包）。三件都由 write_report_files 从**同一份冻结正文**写出。
        try:
            from report_quality import write_report_files
            write_report_files(reports,
                               frozen["reports/report.md"].decode("utf-8"))
        except Exception as exc:                 # noqa: BLE001 - 拆不出就只留完整版
            logger.warning("快照：主文/附录拆分落盘失败：%s", str(exc)[:120])
            (reports / "report.md").write_bytes(frozen["reports/report.md"])
        if frozen.get("reports/report.pdf"):
            (reports / "report.pdf").write_bytes(frozen["reports/report.pdf"])
        manifest = _manifest_from_frozen(frozen, snap=snap, ws=ws)
        members: list[tuple[str, bytes]] = list(frozen.items())
    else:
        mdb = bytes(md_bytes)
        files: list[tuple[_P, str]] = [(reports / "report.md", "reports/report.md")]
        # 任务22 / 缺陷B（真机 E2E 复现）：report.md 与主文/附录拆分件由**同一份交付
        # 正文**经 report_quality.write_report_files 一次写出，不再各写一份（口径分叉）。
        # 没有附录级标题（= 完整版本身就是主文）时拆分件不写也不进包，
        # 避免同一份内容两个成员。
        try:
            from report_quality import write_report_files
            _split = write_report_files(reports, mdb.decode("utf-8"))
        except Exception as exc:                 # noqa: BLE001 - 拆不出就保完整版
            logger.warning("交付主文拆分落盘失败（按无附录处理）：%s", str(exc)[:120])
            (reports / "report.md").write_bytes(mdb)
            _split = {}
        if _split.get("appendix_chars") and _split.get("body_chars"):
            files.append((reports / "report_body.md", "reports/report_body.md"))
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
        # 分析运行记录 + **可复算输入**（K2）：有才进包（与快照路径同一组路径名，便于两路核对）
        _analysis = analysis_payload_bytes(ws)
        if _analysis:
            _ana_path = ws / "analysis" / "analysis_runs.json"
            try:
                _ana_path.parent.mkdir(parents=True, exist_ok=True)
                _ana_path.write_bytes(_analysis)
                files.append((_ana_path, "analysis/analysis_runs.json"))
            except Exception as exc:             # noqa: BLE001 - 写不出就不进包
                logger.warning("打包：分析运行记录落盘失败：%s", str(exc)[:100])
        try:
            from financial_analysis import store as _fa_in
            for arc, blob in (_fa_in.input_payload_bytes(ws) or {}).items():
                p = ws / arc
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(blob)
                files.append((p, arc))
            _sel_blob = _fa_in.selection_payload_bytes(ws)
            if _sel_blob:
                p = ws / _fa_in.ARC_SELECTION
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(_sel_blob)
                files.append((p, _fa_in.ARC_SELECTION))
            # V0（阶段V）：**分析底稿文本**（完整卡＝run/output/component_id 与规则）随包，
            # 正文只留三条摘要；底稿是"可展开"的那一层，缺了它就只剩摘要无处可查。
            for p in sorted((ws / "analysis").glob("*.md")):
                files.append((p, f"analysis/{p.name}"))
            # 冻结研究输入（B4 研究直连）：research_inputs.json 原名随包。
            # 按名存在才进包：旧任务没有这个文件名，行为零变化；manifest 同步收录。
            _fi = ws / "analysis" / "research_inputs.json"
            if _fi.exists():
                files.append((_fi, "analysis/research_inputs.json"))
            # C4 研究实验（现金回撤/机制卡/披露更新）：**完整实验 JSON 同版随包**——
            # `company_view.realization.c3_experiment.exact_values_in` 指向的就是它，
            # 此前只进了渲染后的 .md、JSON 成员缺失（C4-3 接缝修）。
            # 按名存在才进包：旧任务没有这个文件名，行为零变化。
            for _c3 in sorted((ws / "analysis").glob("c3_experiment_*.json")):
                files.append((_c3, f"analysis/{_c3.name}"))
        except Exception as exc:                 # noqa: BLE001 - 输入读不到就不进包
            logger.warning("打包：分析输入读取失败：%s", str(exc)[:100])
        # F1 剔除审计（`project/source_exclusions.json`）：报告缺口行会写
        # "剔除记录见交付包 audit/source_exclusions.json"，所以必须真有这个成员。
        # 按名存在才进包：没有剔除记录的任务行为零变化。
        files.extend(_exclusion_audit_members(ws))
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
    # §3D（10-06 复核）**外发投影**：交付包是对外交付面 —— 按**现有许可判据**把无权外发的
    # 量化附件移出本包（换成一份限制说明），财报正文与其他成员原样保留；内部快照与工作区
    # `quant/` 原件不动（内部保存成立），被移出成员的 sha256 记进包内清单。两路（快照/直读）
    # 都过同一判据，不新增第二条交付链。
    _projected, _ext = external_delivery_projection(dict(members))
    if _ext is not None:
        members = list(_projected.items())
        manifest["files"] = {arc: hashlib.sha256(blob).hexdigest() for arc, blob in members}
        manifest["external_delivery"] = _ext
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
    # R2：**先校验再发布**——在临时包上复算字节 hash（并对快照 hash），失败就删掉临时包
    # 报错；校验通过的包才原子替换上线。"先发布后自检"会把坏包暴露在下载列表里。
    verify: dict[str, str] = {}
    try:
        with _zf.ZipFile(tmp_path) as zf:
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
    if any(v != "ok" for v in verify.values()):
        try:
            tmp_path.unlink()
        except Exception:                        # noqa: BLE001 - 清理失败不影响报错
            pass
        return {"package": "", "path": "", "files": [a for a, _b in members],
                "manifest": manifest, "verify": verify, "bytes": 0,
                "error": "发布前校验未通过，未上线新包（旧包保持可用）"}
    _os.replace(tmp_path, zip_path)          # 原子发布（校验已通过）
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

    V0（阶段V）：`分析摘要` 与 `经营驱动分析正文` 是装配器从**同一组已验证运行**现场生成的
    论证线（利润由何而来／现金为何变化／什么条件会改变判断），内容属于『分析』——
    判定"有没有实质分析"时必须连它们一起看：模型散文缺位时它们**不是**"只有数据与底稿"。
    但两种情况下它们不算数：① 取模型散文（回流再装配）时按 `_brief_analysis_section` 收尾，
    不把它们当模型内容；② `分析摘要` 只写"本次已验证运行不属于经营研究组合"的空位声明
    （没有组合运行）时，那句话本身不构成分析。
    """
    body = str(report_body or "")
    if "## 分析" not in body:
        return True
    generated = ""
    try:
        import report_brief
        section = report_brief._brief_section(body, "## 分析")
        parts = []
        card = report_brief._brief_section(body, "## 分析摘要")
        if card and report_brief.ANALYSIS_EMPTY_SUMMARY_MARK not in card:
            parts.append(card)
        note = report_brief._brief_section(body, "## 经营驱动分析正文")
        if note:
            parts.append(note)
        generated = "\n".join(parts)
    except Exception:
        idx = body.find("## 分析")
        section = body[idx + len("## 分析"):].strip()
    if _ANALYSIS_PLACEHOLDER in section or _ENGINEERING_SUMMARY_RE.search(section):
        # 模型散文缺位/是工程收尾报告：只有装配器生成的论证线够长，才算有分析
        return len(generated) >= 60
    return len(section) + len(generated) >= 60


_ANALYSIS_PLACEHOLDER = "本次未产出可交付的分析正文"
_ENGINEERING_SUMMARY_RE = re.compile(
    r"^##\s*Task Report\s*$|^\s*Steps:\s*\d+\s*\(\d+\s*OK,\s*\d+\s*failed\)", re.M)


def _gate_analysis_text(task_id: str, report_body: str) -> str:
    """硬门槛判"这份交付有没有分析"时该读的文本：**主文 ＋ 收束移出的另附底稿**。

    规划 §11.2 的主文收束把装配器生成的论证线（`## 经营驱动分析正文`）移进
    `analysis/analysis_detail.md`——那是**随包交付的同一份交付物**，不是"没有分析"。
    只看主文会把好报告判成"只有数据与底稿"（降级为草稿）。
    **凡被移走的内容，读它的每一处都要跟着改**——这里是硬门槛这一处。

    读不到/没有收束块就按主文判（退回旧口径，不制造新缺口、不误判通过）。
    """
    body = str(report_body or "")
    try:
        import workspace as _ws
        p = _ws.task_workspace(task_id) / "analysis" / "analysis_detail.md"
        if not p.is_file():
            return body
        txt = p.read_text(encoding="utf-8")
        import report_brief as _rb
        if _rb.COMPACT_BEGIN not in txt or _rb.COMPACT_END not in txt:
            return body
        block = txt.split(_rb.COMPACT_BEGIN, 1)[1].split(_rb.COMPACT_END, 1)[0]
        return f"{body}\n\n{block}" if body else block
    except Exception as exc:                     # noqa: BLE001 - 读不到就按主文判
        logger.warning("收束另附底稿读取失败（task=%s，按主文判）：%s",
                       task_id, str(exc)[:120])
        return body


def _structure_analysis_state(task_id: str, *, ws_dir=None) -> dict | None:
    """从**交付结构对象**读"有没有可交付分析"所需的信号（10-06 复核：迁移标题哨兵）。

    `report_structure.json` 里本来就有这三样东西，不必再从正文标题去猜：

    - `analysis`：模型散文（占位句 `本次未产出可交付的分析正文` 在它里面）；
    - `analysis_card`：分析摘要（只有空位声明时不算分析）；
    - `analysis_note`：装配器生成的论证线；
    - `reader_judgments`：本次选版快照产出的**读者判断**（10-06 起）。

    读不到结构对象就返回 `None`（调用方退回正文文本判据，旧行为不变）。
    """
    try:
        import report_brief as _rb
        st = _rb.read_structure(task_id, ws_dir=ws_dir)
        if not isinstance(st, dict) or not st:
            return None
        prose = str(st.get("analysis") or "")
        note = str(st.get("analysis_note") or "")
        card = str(st.get("analysis_card") or "")
        generated = ""
        if card and _rb.ANALYSIS_EMPTY_SUMMARY_MARK not in card:
            generated += card
        if note:
            generated += "\n" + note
        _prose = prose.strip()
        # **空的模型散文也要算"缺位"**：报告步骤失败时 `analysis` 常常是空串（不是占位句），
        # 只看占位句会把它当成"有散文" ⇒ 失败的稿子被判 verified（实测过）。
        placeholder = (not _prose) or (_ANALYSIS_PLACEHOLDER in prose) \
            or bool(_ENGINEERING_SUMMARY_RE.search(prose))
        return {"placeholder": placeholder,
                "prose_len": len(_prose),
                "generated_len": len(generated.strip()),
                # 装配时算好的**判决**（首选）：它是在"未收束正文＋生成的论证线"都在手时
                # 算的，因此不受主文收束影响（10-06 复核 §5-1 的哨兵迁移）。
                "gate_ok": st.get("analysis_gate_ok"),
                "reader_judgments": len(st.get("reader_judgments") or [])}
    except Exception as exc:                     # noqa: BLE001 - 读不到就退回文本判据
        logger.warning("结构对象分析状态读取失败（task=%s）：%s", task_id, str(exc)[:120])
        return None


def _structure_from_dict(structure: dict | None) -> dict | None:
    """把**调用方手上的结构对象**变成判据所需的信号（不需要再读磁盘）。

    10-06 复核 §5-1 的哨兵迁移要点：判决要在"未收束正文＋生成的论证线"都在手时算，
    并且**跟着本次交付对象走**——按任务名去磁盘读结构，在"同名任务多工作区"（重跑、测试
    夹具）时会读到不同源的旧结构，让"报告步骤失败不得 verified"随读取顺序漂移（实测）。
    """
    if not isinstance(structure, dict) or not structure:
        return None
    import report_brief as _rb
    gate_ok = structure.get("analysis_gate_ok")
    if gate_ok is None:
        return None
    return {"gate_ok": bool(gate_ok),
            "reader_judgments": len(structure.get("reader_judgments") or []),
            "placeholder": False, "prose_len": 0, "generated_len": 0}


def _has_deliverable_analysis(task_id: str, report_body: str, *, ws_dir=None,
                              structure: dict | None = None) -> bool:
    """交付里有没有**可交付的分析**。

    结构判据优先（10-06 复核 §5-1：把标题哨兵依赖迁移到既有结构判据）：
    有读者判断、或装配器论证线够长、或模型散文不是占位句 ⇒ 有分析。
    结构对象读不到时**退回**正文文本判据 `_has_analysis_section`（旧行为不变）。

    为什么必须迁移：主文收束把 `## 分析` 移出正文后，`_has_analysis_section` 会撞上
    "正文里没有 `## 分析` ⇒ 不是研究简报、不判"的免伤分支，于是**报告步骤失败的稿子
    被判成 verified**（实测 `test_body_model_failure_keeps_recomputable_paper`）。
    结构判据不看正文标题，所以"报告步骤失败不得 verified"照旧成立。
    """
    st = (_structure_from_dict(structure) if structure is not None
          else _structure_analysis_state(task_id, ws_dir=ws_dir))
    if st is not None:
        # ① 首选装配时算好的判决（不受主文收束影响）；② 新交付契约下，结构里有读者判断
        #    也算有分析；③ 结构里既没有判决也没有读者判断，才退回下面按结构信号自算。
        if st.get("gate_ok") is not None:
            return bool(st["gate_ok"]) or bool(st["reader_judgments"])
        # 判据与旧文本判据同形：散文缺位时要有**装配器生成的论证线**（或读者判断）才算；
        # 散文在时，则要求（散文＋论证线）达到最低长度，避免一句"待写"就算有分析。
        if st["placeholder"]:
            return bool(st["reader_judgments"]) or st["generated_len"] >= 60
        return (st["prose_len"] + st["generated_len"]) >= 60
    return _has_analysis_section(_gate_analysis_text(task_id, report_body))


def _rb_gate_structure(task_id: str, body: str, *, ws_dir=None) -> dict | None:
    """给硬门槛用的结构对象：**优先用装配时算好的判决**（`analysis_gate_ok`）。

    为什么不能就地按当前正文重算：走到这里时正文可能**已经收束**（`## 分析` 已被移进
    附件），按它重算会撞上"正文里没有 `## 分析` ⇒ 不是研究简报、不判"的免伤分支，
    于是一份"报告步骤失败"的稿子被判成有分析（实测 `off-res-1`：body 6,256 字符、
    无 `## 分析` ⇒ 重算 True ⇒ verified）。所以这里：
      ① 结构对象里有判决 ⇒ 直接用（装配时用**未收束的模型正文**算的）；
      ② 没有判决 ⇒ 退回**旧的正文文本判据**（连收束块一起看）；
      ③ 结构读不到 ⇒ 也退回旧文本判据。
    """
    import report_brief as _rb
    txt = str(body or "")
    # ① 正文**还没收束**（里面就有 `## 分析`）：按旧判据就地算 —— 读顺序无关、最可靠。
    #    装配链在收束之前调用本函数，所以这是常规路径。
    if "## 分析" in txt:
        st = None
        try:
            st = _rb.read_structure(task_id, ws_dir=ws_dir)
        except Exception:                        # noqa: BLE001 - 结构读不到也能算
            st = None
        return {"analysis_gate_ok": _rb._analysis_gate_verdict(
            txt, card=str((st or {}).get("analysis_card") or ""),
            note=str((st or {}).get("analysis_note") or ""))}
    # ② 已收束：用装配时算好的结构判决（那时用的是**未收束的模型正文**）。
    #    已知边界（登记，未在本批解决）：结构按**任务名**解析工作区，因此在"同名任务
    #    多工作区"（重跑、测试夹具复用同一个 task_id）时可能拿到同任务的旧判决——
    #    离线套件里表现为"报告步骤失败那条**单独跑通过、混跑偶发失败**"。
    #    生产里任务名唯一，不触发；彻底修法是把判决随正文传递（本批试过标记版，
    #    因重渲染轮次让判决漂移而回退，见证据文档）。
    try:
        st = _rb.read_structure(task_id, ws_dir=ws_dir)
        if isinstance(st, dict) and st and st.get("analysis_gate_ok") is not None:
            return st
    except Exception as exc:                     # noqa: BLE001 - 读不到就退回文本判据
        logger.warning("结构对象读取失败（task=%s，退回文本判据）：%s", task_id, str(exc)[:120])
    # ③ 都没有：退回**旧的正文文本判据**（连收束块一起看）
    return {"analysis_gate_ok": _has_analysis_section(_gate_analysis_text(task_id, body))}


def _snippet_reports_success(snippet: str) -> bool:
    """失败记录里的**原始结果**自己说成功 ⇒ 这是"纠正步骤没达成目标"，不是"没产出正文"。

    实机 `ui-10ea37599d` 的 `i1-acc-redo` 记录就是这种形状：`error_snippet` 里带着
    `{"status": "success", "report_path": …reports/report.md, "chars": 10588}`，任务终态 SUCCESS、
    交付已 verified。把它当"报告步骤失败"会把已交付的好稿子**误判成草稿**——那条记录要说的是
    "这次纠正重做没让验收过关"，交付状态该由验收决定，不该被它一票否决。
    """
    s = str(snippet or "").strip()
    if not s.startswith("{"):
        return False
    try:
        import json as _json
        d = _json.loads(s)
    except Exception:                                # noqa: BLE001 - 不是结构化结果
        return False
    if not isinstance(d, dict):
        return False
    return str(d.get("status") or "").strip().lower() in ("success", "succeeded", "ok")


def _replacement_recovered(d) -> tuple[bool, str]:
    """替代步骤是否**真的成功**接管了这次失败 → `(已恢复, 说明)`。

    10-07 复核 §随样品最小收口-3：此前**只看 `replacement_step_id` 是否存在**，
    于是 `replacement_step_id=<某步> + replacement_outcome=FAILED` 也被当成"有恢复路径"
    （反例：只有替代 ID 就解除失败门）。**有 ID 不等于恢复成功**：
    只有替代结果明确成功才解除；失败/未执行/未知一律**不能**证明恢复（fail-closed）。
    """
    rid = str(getattr(d, "replacement_step_id", "") or "").strip()
    if not rid:
        return False, ""
    outcome = str(getattr(d, "replacement_outcome", "") or "").strip()
    if outcome.lower() in ("success", "succeeded", "ok", "passed", "completed"):
        return True, f"{rid}（outcome={outcome}）"
    return False, f"{rid}（outcome={outcome or '未记录'}）"


def _report_step_failure(task_id: str, *, ws_dir=None) -> str:
    """本任务是否有**未恢复、且确实没产出正文的报告步骤失败**（交付硬门槛用）→ 原因或 `""`。

    为什么门槛必须读这个**事实记录**：报告步骤失败时，交给装配的是失败文本
    （没有 `## 分析`，也没有可交付的正文），而内容判据看不见"这一步失败了"——
    `_analysis_gate_verdict` 对"正文没有 `## 分析`"走免伤分支返回 True，
    `_has_deliverable_analysis` 又接受"有确定性读者判断就算有分析"，于是失败稿也判 `verified`
    （10-06 复核离线用例 `off-res-1` 实测，基线 `92c359f` 起就成立）。
    步骤失败是**记录在案的事实**（`step_failure.json`，capability=`report_generator`），
    不能靠正文标题去猜。

    三个判据缺一不可：① capability 是 `report_generator`；② 没有**成功接管**的替代步骤
    （`replacement_step_id` 有值但 `replacement_outcome` 是 FAILED/未执行/未知 ⇒ 不算恢复）；
    ③ 记录里的原始结果**不是**成功载荷（见 `_snippet_reports_success`）。

    读不到记录（文件不存在）＝没有信号；文件存在但读不出来＝**不静默放行**，仍报原因。
    """
    try:
        p = _ws(task_id, ws_dir) / "step_failure.json"
        if not p.is_file():
            return ""
    except Exception as exc:                         # noqa: BLE001 - 路径都算不出＝没有信号
        logger.warning("步骤失败记录定位失败（task=%s）：%s", task_id, str(exc)[:120])
        return ""
    try:
        import json as _json
        from step_diagnosis import StepDiagnosis
        # 自己读+解析（不用 `read_step_failures`）：后者把"文件坏掉"吞成 `[]`，
        # 于是"有失败记录却读不出来"会被当成"没有失败"——那正是**静默放行**。
        raw = p.read_text(encoding="utf-8")
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("step_failure.json 不是列表")
        for item in data:
            if not isinstance(item, dict):
                continue
            d = StepDiagnosis.from_dict(item)
            if str(getattr(d, "capability", "") or "") != "report_generator":
                continue
            _rec, _rec_why = _replacement_recovered(d)
            if _rec:
                continue                             # 替代步骤**确实成功** ⇒ 有恢复路径
            snippet = str(getattr(d, "error_snippet", "") or "")
            if _snippet_reports_success(snippet):
                continue                             # 原始结果说成功了 → 不是"没产出正文"
            return ("报告步骤失败（report_generator，未恢复且未产出正文）"
                    + (f"，替代步骤未成功：{_rec_why}" if _rec_why else "")
                    + (f"：{snippet[:80]}" if snippet else ""))
        return ""
    except Exception as exc:                         # noqa: BLE001 - 有失败记录却读不出来
        logger.warning("步骤失败记录读取失败（task=%s）：%s", task_id, str(exc)[:120])
        return f"步骤失败记录读取失败（按证据未知处理）：{str(exc)[:100]}"


def apply_research_hard_gate(task_id: str, goal: str, wp: dict | None,
                             report_body: str = "", *, ws_dir=None,
                             structure: dict | None = None) -> tuple[str, str]:
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
    # 报告步骤失败是**事实记录**（不是内容判据）：失败稿不得判 verified。
    # 10-06 复核 §5-1 的明确保证——"迁移标题哨兵到结构判据时保持'报告步骤失败不得 verified'"。
    _step_fail = _report_step_failure(task_id, ws_dir=ws_dir)
    if _step_fail:
        reasons.append(_step_fail + "：本次交付不得判为已验证（内容判据看不见步骤失败）")
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
            # （架构复核：正文失效时交付"数据表/底稿 + 分析未完成"，不以拼接日志冒充研报）。
            # 判据优先读**结构对象**（10-06 复核：迁移标题哨兵），读不到才退回正文文本。
            if report_body and not _has_deliverable_analysis(
                    task_id, report_body, ws_dir=ws_dir, structure=structure):
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
        # 规划 §11.2 / 阶段X §7：**主文收束**放在这唯一一处渲染出口——验收
        # （`accept_for_body`）、导出复核（`export_snapshot` 的"受保护读取"）与最终装配
        # 都按**本函数**的产物比对字节，收束只在别处做的话会被它们重新渲染成未收束的
        # 正文（实测：装配处收束后又被验收路径换回全文，页数不降）。
        # 口径（§8.3 最小落地）：**只移**模型子报告与附录 ID/底稿详表，
        # **必要结论与边界（`## 研究问题与下一步` 等）留在主文**——上一轮把边界一起移走
        # （6 条冻结短语消失、CI 判死）即为此处翻车，勿再扩大移动范围。
        try:
            from pathlib import Path as _Pth
            _ws_c = _Pth(ws_dir) if ws_dir else workspace.task_workspace(task_id)
            candidate, _moved = report_brief.compact_main_body(
                candidate, detail_path=_ws_c / "analysis" / "analysis_detail.md")
            if _moved.get("moved"):
                logger.info("主文收束（task=%s）：移出 %s（%d 字符）",
                            task_id, "、".join(_moved["moved"]), _moved["chars"])
        except Exception as exc:                 # noqa: BLE001 - 收束失败就保留全文
            logger.warning("主文收束失败（task=%s，保留全文）：%s", task_id, str(exc)[:140])
        return rewrite_report_links(candidate, task_id) or candidate
    except Exception as exc:                     # noqa: BLE001 - 装配失败退回原正文
        logger.warning("研究候选稿装配失败（task=%s，退回原正文）：%s",
                       task_id, str(exc)[:160])
        return None


def accept_task_meta(task_id: str) -> dict:
    """验收体裁（report_type）要读的**任务元数据**：任务行 + 解析后的 run_options。

    实测：当前链路没有任何"报告体裁"字段——`research_request` 只有公司/期间/口径
    （`facts.parse_research_request`），`run_options` 只有 auto_run/template_steps，
    `task_history` 也没有 report_type 列。所以这里如实把"可能声明体裁的地方"整包交给
    `acceptance_checker.resolve_report_type`：将来任一字段声明了体裁就优先采信，
    没声明则按目标文本判定。读不到元数据时返回空 dict，验收照常进行。
    """
    try:
        import task_state as _ts
        row = _ts.read_task(task_id) or {}
    except Exception as exc:                     # noqa: BLE001 - 元数据读不到不影响验收
        logger.debug("读取任务元数据失败（task=%s）：%s", task_id, str(exc)[:120])
        return {}
    if not isinstance(row, dict):
        return {}
    meta = dict(row)
    _raw = row.get("run_options_json")
    if isinstance(_raw, str) and _raw.strip():
        try:
            _parsed = json.loads(_raw)
            if isinstance(_parsed, dict):
                meta["run_options"] = _parsed
        except Exception:                        # noqa: BLE001 - 坏 JSON 当没配
            pass
    return meta


# 单条检查项入库前保留的最大字节数（任务18：见 `_compact_acceptance_checks`）
_EVIDENCE_CHECK_MAX_CHARS = 2000


def _compact_acceptance_checks(checks: dict) -> dict:
    """逐项结论**瘦身**后入库：过长的单条只留判定与短说明。

    为什么瘦身：`number_traceability` 一类检查会带全量数字轨迹（实测单条 25KB，
    整份 `checks` 40~80KB），而 `acceptance_json` 这一列每次任务页/投影读取都要解析，
    不该为可见性把任务行撑到几十 KB。瘦身后保留的是"这条规则过没过、为什么"，
    丢掉的是逐笔明细——明细仍在 `acceptance_report.json` 与验收事件流里。
    """
    out: dict = {}
    for key, val in (checks or {}).items():
        if not isinstance(val, dict):
            out[key] = val
            continue
        if len(json.dumps(val, ensure_ascii=False)) <= _EVIDENCE_CHECK_MAX_CHARS:
            out[key] = val
            continue
        out[key] = {
            "pass": val.get("pass"),
            "applicable": val.get("applicable"),
            "status": val.get("status"),
            "details": str(val.get("details") or "")[:400],
            "gaps": list(val.get("gaps") or [])[:10],
            "trimmed": True,
        }
    return out


def _acceptance_evidence(result: dict, report_type: str = "") -> dict:
    """一次验收 → 入库证据字典（任务18）。空值不写（键缺席 = 没有这项事实，不编 0）。"""
    ev = {
        "overall": result.get("overall"),
        # 验收器目前不产出分数：如实为 None（被下面的过滤去掉），不假装有分
        "score": result.get("score"),
        "gaps": list(result.get("gaps") or []),
        "checks": _compact_acceptance_checks(result.get("checks") or {}),
        "report_type": str(result.get("report_type") or report_type or ""),
        "profile": str(result.get("profile") or ""),
        "rules_version": str(result.get("rules_version") or ""),
        "rules_fingerprint": str(result.get("rules_fingerprint") or ""),
        "report_sha256": str(result.get("report_sha256") or ""),
        "evaluated_at": str(result.get("evaluated_at") or ""),
        # 任务22：主文超字数（只提示，不影响 overall）；未超限时键缺席
        "length_warning": result.get("length_warning"),
    }
    if not ev["evaluated_at"]:
        # UTC + Z。设计文档写的是 `datetime.utcnow()`：3.12 起已弃用，且本模块
        # 没有 datetime 导入——`time.gmtime` 输出同样口径，不为此新增导入。
        ev["evaluated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {k: v for k, v in ev.items() if v not in (None, "", [], {})}


def accept_for_body(task_id: str, goal: str, body: str = "", *,
                    trigger: str = "报告步骤", prefer_body: bool = True,
                    hooks: dict | None = None, ws_dir=None,
                    verify_body_as_is: bool = False) -> dict | None:
    """对**指定正文**跑确定性验收，并把结论绑到该正文所属的版本。

    `verify_body_as_is`：**认这份正文就是验收对象，不再用装配器重写一遍**。
    默认 False（沿用旧行为：先装配最终候选稿再验收）。凡"要证明的就是**这一个身份**"
    的场合（采纳后重验、对候选正文自己验收）必须置 True——否则验收跑在装配器重写后的
    另一份正文上，结论与身份对不上（实测：采纳版正文 `e3d4d0a8…` 重装配后变成
    `429801667d…`，"毛利额…：20920103385→24728464886"被改写成
    "上期 247.28 亿元 → 本期 209.20 亿元（-38.08 亿元）"，两版数字不同、验收对象不同）。

    `hooks`（都可选，编排器用它保持既有行为）：
    - `cancelled()`：返回 True 则跳过（取消是终态，不再产生副作用）；
    - `on_summary(summary)`：把缺口摘要推给前端；
    - `iteration`：写进审计事件的轮次；
    - `on_failure(result, report)`：验收 fail 后的沉淀钩子（评测集生长）。

    验收体裁（`report_type`）由 `accept_task_meta(task_id)` + goal 判定后**透传**给
    `run_acceptance`：同一份正文按不同体裁取不同阈值，结论里回写生效体裁，便于对账。
    """
    hooks = hooks or {}
    try:
        if hooks.get("cancelled") and hooks["cancelled"]():
            logger.info("任务已取消，跳过验收（task=%s, trigger=%s）", task_id, trigger)
            return None
        _t0 = time.time()
        _repaired = False
        from acceptance_checker import resolve_report_type, run_acceptance
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
        # 同理，"验证的就是这一个身份"（`verify_body_as_is`）时不得替换。
        if not verify_body_as_is and str(trigger or "") != "人工修订重验":
            candidate = research_candidate_body(task_id, goal, report,
                                                project=None, ws_dir=ws_dir)
            if candidate and candidate != report:
                logger.info("验收对象改为代码装配的候选稿（task=%s，%d→%d 字符）",
                            task_id, len(report), len(candidate))
                report = candidate
        # 报告体裁（report_type）：任务元数据显式声明 > 目标文本判定 > default_profile；
        # 验收阈值/时效豁免按体裁档位取（acceptance.profiles），判定规则不变。
        _report_type = resolve_report_type(goal, accept_task_meta(task_id))
        result = run_acceptance(task_id, goal, report, _ws(task_id, ws_dir),
                                report_type=_report_type)
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
                    result = run_acceptance(task_id, goal, repaired,
                                            _ws(task_id, ws_dir),
                                            report_type=_report_type)
        except Exception as exc:
            logger.warning("来源标注自动修复失败（task=%s）：%s", task_id, str(exc)[:120])
        # 来源清单 URL 存活校验（仅提示，不改 overall）
        try:
            import os
            if os.environ.get("URL_HEALTH_CHECK", "1") != "0":
                from acceptance_checker import extract_source_list
                from adapters.url_health import check_urls, is_definitely_gone
                src_urls = [e.get("url") for e in extract_source_list(report) if e.get("url")]
                if src_urls:
                    states = check_urls(src_urls)
                    # 只有 404/410 算"链接失效"；403/429/超时/DNS/策略拒绝属于"当前访问不到"，
                    # 如实分列，不写成失效（专项 §5：暂时不可访问保留原因，不算已取得证据）。
                    gone = [u for u, st in states.items() if is_definitely_gone(st)]
                    unverified = [u for u, st in states.items()
                                  if str(st) in ("inaccessible", "unknown", "policy_blocked")]
                    if gone or unverified:
                        hint = f"来源链接失效: {len(gone)} 条" if gone else ""
                        parts = []
                        if gone:
                            parts.append(f"{hint}（404/410）")
                        if unverified:
                            parts.append(f"当前访问不到（保留为未验证来源）: {len(unverified)} 条")
                        # 任务24：**合并**而非整体覆盖——run_acceptance 已把
                        # `dead_source_count`/`dead_source_urls`（dead_sources.json
                        # 通道的证据）写进 url_health，这里覆盖写会把它们抹掉。
                        _uh = dict(result.get("checks", {}).get("url_health") or {})
                        _uh.update({
                            "pass": True, "hint": bool(gone),
                            "details": "；".join(parts) + "（仅提示，不影响验收结论）",
                            "dead_count": len(gone), "dead_urls": gone[:10],
                            "unverified_count": len(unverified),
                        })
                        result.setdefault("checks", {})["url_health"] = _uh
                        if gone and hint not in result.get("gaps", []):
                            result.setdefault("gaps", []).append(hint)
        except Exception:
            pass
        # 任务22：主文字数（附录前）超限只记警告，不阻断交付、不改 overall。
        # 放进 result 后由 `_acceptance_evidence` 随证据入库（键缺席=未超限）。
        try:
            from report_quality import body_length_warning, split_report_appendix
            _body, _appendix = split_report_appendix(report)
            _lw = body_length_warning(_body)
            if _lw:
                result["length_warning"] = _lw
                logger.warning("主文超字数限制：%d 字符（目标≤%d）",
                               _lw["chars"], _lw["limit"])
        except Exception:                        # noqa: BLE001 - 量不出字数不阻断验收
            pass
        # 落盘：快照（覆盖写）+ 审计事件（追加），两者都在绑定之前完成
        #
        # P0-2b（验收只允许一个权威来源）：快照**自己说清绑的是哪一版**；当它不是当前
        # 采纳/交付那一版时，把采纳版自己的结论一并写成"权威那套"，并留 WARNING。
        # 反例（实测 ui-8b28c95ab1）：页面横幅读任务行的 acceptance_json（采纳版
        # 8fdfc0d5，4569 字，2 个缺口），工作区 acceptance_report.json 却被最后一次
        # 验收（完整稿 94b594a2，3 个缺口）覆盖——两套数字并排显示，谁也说不清哪套算数。
        try:
            _wsx = _ws(task_id, ws_dir)
            _snap = dict(result or {})
            _this_sha = str(_snap.get("report_sha256") or "")
            _adopted_now = VersionStore(_wsx, task_id).adopted()
            _adopted_sha = str(getattr(_adopted_now, "version_id", "") or "")
            _is_adopted = bool(_adopted_sha and _adopted_sha == _this_sha)
            _snap.update({
                "task_id": task_id,
                "trigger": str(trigger or ""),
                "body_sha256": _this_sha,
                "body_chars": len(str(report or "")),
                "is_adopted_version": bool(_is_adopted),
                "adopted_body_sha256": _adopted_sha,
                "authoritative": bool(_is_adopted or not _adopted_sha),
            })
            if (_adopted_now is not None and not _is_adopted
                    and _adopted_now.acceptance_for_this_body()):
                # 采纳/交付那一版**自己**有绑定的验收，而这份快照是给另一版写的：
                # 两套数字从此并存（页面横幅读采纳版那套，本文件是另一套）。
                _own = dict(_adopted_now.acceptance or {})
                _snap["authoritative_overall"] = str(_own.get("overall") or "")
                _snap["authoritative_gaps"] = list(_own.get("gaps") or [])
                _snap["authoritative_report_sha256"] = _adopted_sha
                _snap["authoritative_body_chars"] = len(str(_adopted_now.body or ""))
                _chars_txt = (f"{len(str(report or ''))} 字符"
                              if str(report or "") else "字符数未记录")
                _snap["acceptance_conflict"] = (
                    f"本文件是「{trigger or '未知来源'}」对正文 {_this_sha[:12]}"
                    f"（{_chars_txt}）的验收：overall={_snap.get('overall')}、"
                    f"缺口 {len(_snap.get('gaps') or [])} 个；当前采纳/交付的那版是 "
                    f"{_adopted_sha[:12]}（{len(str(_adopted_now.body or ''))} 字符），其权威结论 "
                    f"overall={_snap['authoritative_overall']}、缺口 "
                    f"{len(_snap['authoritative_gaps'])} 个。页面与交付物只认权威那套。")
                logger.warning("验收快照不是采纳版验收（task=%s）：%s",
                               task_id, _snap["acceptance_conflict"][:260])
            (_wsx / "acceptance_report.json").write_text(
                json.dumps(_snap, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:                 # noqa: BLE001 - 快照身份写入失败不阻断验收
            logger.warning("验收快照身份标注失败（task=%s）：%s", task_id, str(exc)[:120])
        # 绑定验收：**独立** try，不与上面的快照落盘共用。
        # 20261021 回归（Lead 复核发现）：绑定代码一度被写进上面那个 `except` 的
        # 分支体里 → 只有快照落盘**抛异常**时才会执行绑定，于是所有交付都退回
        # "该版本没有对应它自身的验收（未知）"，页面/下载恒为未验收草稿。
        # 参考用例 test_orchestrator_v2.test_iteration_accumulates_steps_and_uses_best_report
        # 直接抓住了这个改动（delivery_pipeline.py 单文件还原即通过）。两件事分开 try：
        # 快照写失败不得让验收绑不上，绑定失败也不得让快照丢失。
        try:
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
        # 任务18：验收**证据**入库（这一行就是最后一次 run_acceptance 的结论，
        # 含虚假标注自动修复后的复检）。此前逐项 checks 只活在快照文件与审计事件里，
        # 进程死了/文件被换版覆盖，任务行就答不出"当时哪条规则没过、用的哪版规则"。
        # 失败只 warning：可见性改动不得影响交付（设计文档硬约束）。
        try:
            import task_state as _ts
            _ts.save_acceptance_evidence(
                task_id, _acceptance_evidence(result, _report_type))
        except Exception as exc:                 # noqa: BLE001
            logger.warning("验收证据入库失败（task=%s）：%s", task_id, str(exc)[:160])
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


def _task_steps(task_id: str) -> list:
    """任务的步骤真值（DB 投影口径）。读不到就返回空列表——摘要块静默跳过。"""
    try:
        import task_state as _ts
        return list((_ts.task_projection(task_id) or {}).get("steps") or [])
    except Exception as exc:                     # noqa: BLE001 - 步骤读不到不阻断交付
        logger.warning("检索覆盖率：步骤读取失败（task=%s）：%s", task_id, str(exc)[:140])
        return []


def _attach_search_coverage(report: str, task_id: str, steps=None) -> str:
    """任务19：在交付物**末尾**附"数据来源摘要"（只追加，不改正文一个字）。

    - 口径与任务详情 API 的 `search_coverage` 同源（`search_coverage.summarize_search_coverage`）；
    - `steps`：编排器收尾时传**本次运行**的步骤真值（那一刻 DB 里还没有这轮的行，
      读 DB 会漏或读到上一轮）；人工修订等路径不传，按 DB 投影读（那些任务早已落库）；
    - 幂等：先剥掉上次装配留下的摘要块，再按当前步骤重算——修订重装不会叠加、也不会
      把上一版的覆盖率当成这一版的；
    - 没有检索步（纯代码/离线任务）→ 不追加，并且把可能残留的旧摘要块剥掉；
    - 步骤解析不了 → 原样返回（`_task_steps` 已吞异常并告警）。
    """
    try:
        import search_coverage as _sc
        summary = _sc.summarize_search_coverage(
            steps if steps is not None else _task_steps(task_id))
        text = _sc.strip_coverage_block(report)
        block = _sc.render_coverage_block(summary, rules_version=_rules_version())
        if not block:
            return text
        return (text.rstrip("\n") + "\n\n" + block) if text.strip() else block
    except Exception as exc:                     # noqa: BLE001 - 摘要算不出来不阻断交付
        logger.warning("检索覆盖率摘要追加失败（task=%s）：%s", task_id, str(exc)[:140])
        return report


def assemble_and_verify(task_id: str, goal: str, body: str, *,
                        wrapper: str = "", project: str | None = None,
                        accept_fn=None, extra_notes=(),
                        paper: dict | None = None,
                        review_facts: dict | None = None, ws_dir=None,
                        contract: dict | None = None,
                        assemble_body_as_is: bool = False,
                        steps: list | None = None) -> dict:
    """按固定顺序装配交付正文、判定状态并记录交付 hash。

    顺序（编排器收尾与 web_ui 修订必须逐字节一致）：
    确保本版验收 → 底稿 + 研究硬门槛 → `交付说明 + 分隔符 + 正文` + 注记 →
    链接重写 → 唯一谓词判状态 → 未通过加草稿注记 → `record_delivery`。

    `review_facts`：编排器传它**本次运行**的评审结论（内存态），本函数在版本定下来之后
    把它连同 `report_version_id` 落盘——这样"PASS 属于哪一版"是事实而不是推断；
    不传则读工作区里已有的评审事实（人工修订走这条：没有针对新版的 PASS）。

    `contract`：执行契约（wire 或 `{"wire": ...}`）——研究状态按它的必答问题逐项裁决，
    并把契约指纹写进状态的绑定对象（C-5）。

    `assemble_body_as_is`：**认这份正文就是交付正文，不再按工作区重装配一遍**。
    默认 False（编排器收尾/人工修订沿用旧行为：先代码装配简报再判状态）。候选采纳走 True：
    候选本来就是装配器（`build_structure`+`render_brief_markdown`）的产物，再来一次会把它
    重写成另一份字节（实测：采纳版 13309 字 vs 重装配 8038 字），于是
    `record_delivery(accepted_body=…)` 记的不是采纳版 → 导出守卫判"version changed"，
    用户会看到"采纳了却导不出来"。

    `steps`：本次运行的步骤真值（编排器收尾传内存态——那一刻 DB 里还没有这一轮的行；
    人工修订等路径不传，按 DB 投影读）。只用于报告末尾的"数据来源摘要"（任务19），
    不参与验收、不参与交付状态判定。
    """
    store = VersionStore(_ws(task_id, ws_dir), task_id)
    # F1：研究任务改用**代码装配的研究简报**作为交付正文（关键发现/财务对照/图表/
    # 引用编号/资料范围/声明由代码输出，模型只贡献"分析"一节）；工程交付说明不再进
    # 研究简报正文。装配必须在验收之前——验收对象就是最终交付的那份正文。
    brief_note = ""
    try:
        import report_brief
        if (not assemble_body_as_is) and report_brief.is_research_task(
                task_id, goal, ws_dir=ws_dir):
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

    # R2（采纳→selection→冻结 同版）：装配把采纳版本换掉之后，选择记录的
    # `adopted_identity` 必须跟着**这一版**——否则一次导出里 manifest 的身份与分析
    # 选择的身份会是两版（实测三一包：23b61bee… vs d290ab01…）。同一判据：正文里
    # 找得到所选运行才回绑；找不到就不改，由导出快照把不一致如实记进清单。
    try:
        from financial_analysis.store import rebind_selection_identity as _rbs
        _adopted_final = store.adopted()
        _rb_out = _rbs(_ws(task_id, ws_dir), (
            _adopted_final.identity_id() if _adopted_final is not None else ""),
            body_text=body, reason="装配后按最终采纳版本回绑选择身份")
        if _rb_out.get("changed"):
            logger.info("交付装配：选择身份回绑 %s→%s（task=%s）",
                        str(_rb_out.get("from") or "")[:12],
                        str(_rb_out.get("to") or "")[:12], task_id)
        elif not _rb_out.get("consistent"):
            logger.warning("交付装配：选择身份未回绑（task=%s）：%s",
                           task_id, str(_rb_out.get("reason") or "")[:140])
    except Exception as exc:                     # noqa: BLE001 - 回绑失败不阻断交付
        logger.warning("交付装配：选择身份回绑失败（task=%s）：%s", task_id, str(exc)[:120])

    wp = paper if paper is not None else write_working_paper(
        task_id, goal, project=project)
    facts = dict(review_facts) if review_facts is not None else read_review_facts(task_id)
    facts.setdefault("required", review_required())
    _v_for_review = store.adopted()
    if review_facts is not None:
        facts["report_version_id"] = (_v_for_review.identity_id()
                                      if _v_for_review is not None else "")
        write_review_facts(task_id, facts, ws_dir=ws_dir)

    # P0-1：**正文完整性闸门**。截断的正文不得静默当作可交付：
    # ① 页面与交付物上显式标注「正文在第 X 节截断，内容不完整」（引用块，读者第一眼可见）；
    # ② 状态降级为未验收草稿，理由写进交付登记的结构化字段 `draft_reason`；
    # ③ 留 WARNING 日志（`assertLogs` 可断言）。
    # 判定对象是**研究正文**（`body`），不是加了外壳的交付字节——外壳不影响正文完整性。
    trunc_info: dict = {}
    try:
        import report_brief as _rb_trunc
        trunc_info = _rb_trunc.detect_body_truncation(body)
    except Exception as exc:                     # noqa: BLE001 - 判据失败不阻断交付
        logger.warning("交付装配：截断判据执行失败（task=%s）：%s", task_id, str(exc)[:120])

    notes: list[str] = []
    if trunc_info.get("truncated"):
        _trunc_sentence = str(trunc_info.get("reason") or "正文被截断，内容不完整")
        logger.warning("交付正文**截断**（task=%s，%d 字符，节=%s）：%s", task_id,
                       len(str(body or "")), str(trunc_info.get("section") or ""),
                       _trunc_sentence[:200])
        _tnote = ""
        try:
            import report_brief as _rb_tnote
            _tnote = _rb_tnote.truncation_note(trunc_info)
        except Exception:                        # noqa: BLE001
            _tnote = f"> ⚠️ **{_trunc_sentence}**。"
        if _tnote:
            notes.append(_tnote)
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
    gate_note, hard_fail = apply_research_hard_gate(
        task_id, goal, wp, body, ws_dir=ws_dir,
        structure=_rb_gate_structure(task_id, body, ws_dir=ws_dir))
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
    # 任务19：报告末尾附"数据来源摘要"（检索步覆盖率），必须在 `record_delivery` 之前——
    # 交付 hash 要覆盖读者真正看到的这份字节；正文 `body` 不带此块（验收对象不变）。
    report = _attach_search_coverage(report, task_id, steps=steps)

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
    # P0-1：截断稿必须**显式**判为草稿，理由就是那句标注（页面/下载/包内读同一字段）
    trunc_draft_reason = ""
    if trunc_info.get("truncated"):
        trunc_draft_reason = str(trunc_info.get("reason") or "正文被截断，内容不完整")
        status = DELIVERY_DRAFT
        why = f"{trunc_draft_reason}：截断正文只能作未验收草稿交付，需重跑报告生成或人工补全后重新验收"
    if status != DELIVERY_VERIFIED and not why:
        why = review_why or "未取得该版验收"
    if status != DELIVERY_VERIFIED:
        report = with_draft_note(report, why)
    projection = {
        "schema": "weavemind.delivery_projection/1",
        "body_sha256": body_hash(body),          # 本次被交付的**研究正文**字节
        "body_chars": len(str(body or "")),
        "delivered_sha256": body_hash(report),   # 页面投影 / 下载 / 包内主文同一份字节
        "delivered_chars": len(report),
        "version_id": (version.version_id if version is not None else ""),
        "version_body_sha256": (version.version_id if version is not None else ""),
        "identity_id": (version.identity_id() if version is not None else ""),
        "md_relpath": "reports/report.md",
        "truncated": bool(trunc_info.get("truncated")),
        "draft_reason": ("" if status == DELIVERY_VERIFIED
                         else (trunc_draft_reason or why)),
        "status": status,
    }
    entry = store.record_delivery(report, accepted_body=body,
                                  ok=(status == DELIVERY_VERIFIED), reason=why,
                                  draft_reason=(trunc_draft_reason or
                                                ("" if status == DELIVERY_VERIFIED else why)),
                                  truncated=bool(trunc_info.get("truncated")),
                                  truncation=trunc_info, projection=projection)
    # P0-2b：本版正文身份也盖进结构对象（页面详情/清单与正文同源，不再各读一处）
    try:
        import report_brief as _rb_proj
        _st_proj_final = _rb_proj.read_structure(task_id, ws_dir=ws_dir)
        if _st_proj_final is not None:
            _rb_proj.stamp_delivery_projection(_st_proj_final,
                                               entry.get("projection") or projection)
            _rb_proj.write_structure(task_id, _st_proj_final, ws_dir=ws_dir)
    except Exception as exc:                     # noqa: BLE001 - 身份写入失败不阻断交付
        logger.warning("交付身份写入结构对象失败（task=%s）：%s", task_id, str(exc)[:120])
    logger.info("交付装配（task=%s）：status=%s version=%s", task_id, status,
                str(getattr(version, "version_id", ""))[:12])
    return {
        "report": report, "body": body, "status": status,
        "delivery_status": status, "reason": why,
        "draft_reason": ("" if status == DELIVERY_VERIFIED else (trunc_draft_reason or why)),
        "truncated": bool(trunc_info.get("truncated")),
        "truncation": trunc_info,
        "projection": entry.get("projection") or projection,
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

    P0-1/P0-2b：返回里带**本版正文身份**（`projection`：交付字节 sha256/字符数 + 研究正文
    sha256/字符数 + 选中版本身份 + 主文相对路径）、`truncated`/`truncation`（截断闸门）
    与 `draft_reason`（结构化未交付原因）。页面、导出、包内清单、下载守卫都读这一份，
    不再各自拼条件（"只加一句提示"不算修）。
    """
    ws = _ws(task_id, ws_dir)
    store = VersionStore(ws, task_id)
    version = store.adopted()
    proj = delivery_projection(task_id, ws_dir=ws)
    if version is None:
        return {"status": DELIVERY_UNKNOWN, "draft": True, "verified": False,
                "draft_reason": "无选中版本", "version_id": "", "identity_id": "",
                "acceptance_overall": "", "accepted_body_matches": False,
                "aligned": None, "delivered_sha256": "", "delivered_at": 0,
                "truncated": False, "truncation": {}, "projection": proj,
                "md_file": {}}
    trunc_info = version.truncation_info()
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
    if trunc_info.get("truncated") and status == DELIVERY_VERIFIED:
        status = "draft"
        why = (version.truncation_reason()
               + "：截断正文不得当作完整交付（需重跑报告生成或人工补全）")
    # 工作区主文（包内 `reports/report.md` 的来源）与本版交付身份核对：结果如实返回
    # （`projection_vs_package` 的 mismatch 由 `package_statuses` 落成"历史包"）。
    md_file: dict = {}
    try:
        _mdp = ws / "reports" / "report.md"
        if _mdp.is_file():
            _md_bytes = _mdp.read_bytes()
            md_file = projection_vs_package(
                task_id, ws_dir=ws, package="reports/report.md",
                md_text=_md_bytes.decode("utf-8", errors="replace"),
                md_sha256=hashlib.sha256(_md_bytes).hexdigest())
    except Exception as exc:                     # noqa: BLE001 - 核对失败不影响状态
        md_file = {"verdict": "unknown", "reason": f"工作区主文核对失败：{str(exc)[:80]}"}
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
        # P0-1/P0-2b：截断闸门 + 本版正文身份（读取侧唯一取处）
        "truncated": bool(trunc_info.get("truncated")),
        "truncation": trunc_info,
        "projection": (proj or (dict(last.get("projection") or {})
                                if isinstance(last.get("projection"), dict) else {})),
        "md_file": md_file,
    }
