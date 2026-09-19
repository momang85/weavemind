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
                        review_facts: dict | None = None, ws_dir=None) -> dict:
    """按固定顺序装配交付正文、判定状态并记录交付 hash。

    顺序（编排器收尾与 web_ui 修订必须逐字节一致）：
    确保本版验收 → 底稿 + 研究硬门槛 → `交付说明 + 分隔符 + 正文` + 注记 →
    链接重写 → 唯一谓词判状态 → 未通过加草稿注记 → `record_delivery`。

    `review_facts`：编排器传它**本次运行**的评审结论（内存态），本函数在版本定下来之后
    把它连同 `report_version_id` 落盘——这样"PASS 属于哪一版"是事实而不是推断；
    不传则读工作区里已有的评审事实（人工修订走这条：没有针对新版的 PASS）。
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
                brief_body = report_brief.render_brief_markdown(structure, body)
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
    if brief_note:
        notes.append(f"> **装配说明**：{brief_note}。")
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
    """
    p = workspace.task_workspace(task_id) / WRAPPER_FILE
    try:
        if p.exists():
            return p.read_text(encoding="utf-8").strip("\n"), "stored"
    except Exception:
        pass
    text = str(delivered or "")
    if old_body and old_body in text:
        text = text.replace(old_body, "", 1)
    elif SEPARATOR in text:
        text = text.split(SEPARATOR, 1)[0]
    return strip_auto_notes(text), "derived"


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
