# -*- coding: utf-8 -*-
"""修订稿比较：按硬约束与验收结果选版本，**不以长短代替质量**。

背景（视觉实测 2026-09-15，V1）：反思循环里两处直接比较
`len(cand) > len(best_report)`，于是 C 的 504 字符**纠错稿**被 1033 字符旧稿替换，
"更简短、纠正错误、删除不实内容"反而被丢弃；A 的同来源缺口则反复重做。

判定顺序（先硬约束，后实质改进，末尾才看长度）：

1. **验收等级**：pass > issues > fail（高者胜）——这是硬门槛，不能被长度或文采覆盖。
2. **缺口数**：少的胜（同一等级下）。
3. **未溯源数字比例**：低的胜（数字覆盖是可信度硬指标）。
4. **占位/待补标记数**：少的胜（"待补充""TODO""占位"这类未完成痕迹）。
5. 全部相同 → **保持当前版本**（稳定，避免无实质改进的来回替换），并给出可解释原因。

长度只在**最终**作为同分时的极弱倾向（更完整的一稿），且必须两稿同上同下，
不单独构成"改进"。
"""

from __future__ import annotations

import re

# 未完成痕迹（占位/待补），越少越好
_PLACEHOLDER_PATTERNS = (
    r"待补充", r"待完善", r"待核实填充", r"占位", r"TODO", r"TBD",
    r"\[待", r"（待", r"\(待", r"XX+", r"约\s*XX",
)
# 把用户需求整段抄成正文（重复大标题）是常见"长但不改进"的膨胀来源
_ECHO_HINT = re.compile(r"(?:用户目标|原始指令|任务目标)[：:]\s*\S")


def _count_placeholders(text: str) -> int:
    total = 0
    for pat in _PLACEHOLDER_PATTERNS:
        total += len(re.findall(pat, text or ""))
    return total


def _count_echo(text: str) -> int:
    return len(_ECHO_HINT.findall(text or ""))


def quality_snapshot(text: str, acceptance: dict | None = None) -> dict:
    """把一稿的"硬约束可观测项"抽成可比较的快照。

    `acceptance` 为该稿对应的验收摘要（`{"overall": "pass|SUCCESS_WITH_ISSUES|FAILED", "gaps": [...]}`）；
    没有验收结果时按"未知"处理（与 fail 区分，不当作通过）。
    """
    t = str(text or "")
    overall = ""
    gaps = 0
    if isinstance(acceptance, dict):
        overall = str(acceptance.get("overall") or acceptance.get("status") or "")
        gap_list = acceptance.get("gaps")
        gaps = len(gap_list) if isinstance(gap_list, list) else int(acceptance.get("gaps_count") or 0)
    return {
        "accept_rank": accept_rank(overall),
        "gaps": gaps,
        "placeholders": _count_placeholders(t),
        "echo": _count_echo(t),
        "length": len(t),
        "overall": overall,
    }


def disclaimer_clause(has_external: bool, has_user_material: bool) -> str:
    """按**真实来源**生成免责声明（V1：固定写"数据来源于公开渠道"会与事实冲突）。

    - 只用用户材料与派生计算：不能说"公开渠道"，要说清材料真实性未经独立核实；
    - 引用外部检索：保留"公开渠道，可能存在延迟或误差"；
    - 两者都有：分别说明，别把用户材料混进"公开渠道"。
    """
    head = "本报告由织光 WeaveMind AI 自动生成，仅供参考，不构成任何投资建议；"
    tail = "据此操作风险自担。"
    if has_external and has_user_material:
        return (head + "数据同时来自用户提供的材料（其真实性未经独立核实）与公开渠道检索结果"
                      "（可能存在延迟或误差）；" + tail)
    if has_user_material:
        return (head + "数据来自用户提供的材料及据此进行的计算（材料真实性未经独立核实，"
                      "计算过程可复核）；" + tail)
    if has_external:
        return head + "数据来源于公开渠道，可能存在延迟或误差；" + tail
    return head + "本报告未引用外部数据源，结论仅为模型知识，未经检索验证；" + tail


def disclaimer_instruction() -> str:
    """注入报告步骤的免责声明要求：给出三种措辞与选择规则，禁止一律写「公开渠道」。"""
    return (
        "3. 免责声明（按**真实来源**选择措辞，不得一律照抄）：报告结尾必须包含 "
        "'免责声明' 小节，并按下列对应关系选择：\n"
        f"   ① 只使用用户提供的材料与据此计算 → 「{disclaimer_clause(False, True)}」\n"
        f"   ② 引用了外部检索结果 → 「{disclaimer_clause(True, False)}」\n"
        f"   ③ 两者都有 → 「{disclaimer_clause(True, True)}」\n"
        "   没有外部检索结果时禁止写「数据来源于公开渠道」；用户材料必须写明"
        "「真实性未经独立核实」，不得改称模型知识，也不得伪造来源。\n"
    )


def accept_rank(overall: str) -> int:
    """验收等级：pass=2 / 未知=1（不当作通过）/ 有问题或失败=0。"""
    s = str(overall or "").upper()
    if not s:
        return 1
    if s == "PASS" or s == "SUCCESS":
        return 2
    return 0


# 质量向量里"分析观察"的计数上限：超过就不再算优势——避免靠堆砌观察刷分
_QUALITY_OBS_CAP = 10


def candidate_quality(tid: str, goal: str, body: str, *,
                      project=None) -> dict:
    """候选正文的**质量向量**（D2）：有证据的问题覆盖、未支持结论、分析遗漏与重复。

    口径与主张记录同源（确定性、离线）：读底稿事实 → 取分析一节 → 逐条断言判支持状态。
    计数按**独立句子**去重，观察数设上限（`_QUALITY_OBS_CAP`）——不奖励灌水、加图或加长。
    读不到底稿/正文时返回 `{}`（未知，不参与比较，也不当作好稿）。
    """
    try:
        import report_brief as rb
        from working_paper_export import chart_rows
        data = chart_rows(tid, goal, project=project)
        if not data.get("ok"):
            return {}
        rows = list(data.get("rows") or [])
        derived = list(data.get("derived") or [])
        analysis = rb._analysis_section(body)
        if not str(analysis or "").strip():
            return {"analysis_ok": False, "observations": 0, "claims": 0,
                    "bound": 0, "partial": 0, "unsupported": 0, "needs_check": 0,
                    "unsupported_or_unchecked": 0, "duplicate_blocks": 0}
        cov = rb.analysis_coverage(analysis)
        claims = rb._claims(analysis, rows, derived, [],
                            periods=list(data.get("periods") or []))
        counts = {"bound": 0, "partial": 0, "unsupported": 0, "needs_check": 0}
        for c in claims:
            st = str(c.get("status") or "")
            if st == "partially_supported":
                counts["partial"] += 1
            elif st in counts:
                counts[st] += 1
            else:
                counts["needs_check"] += 1
        # 重复块：分析一节里重复出现的行（规范化后相同且 ≥20 字）。
        # 注意按**行**而不是按空行分段：`_analysis_section` 的输出不含空行。
        seen: set[str] = set()
        dup = 0
        for ln in str(analysis).splitlines():
            key = re.sub(r"\s+", "", ln)
            if len(key) < 20:
                continue
            if key in seen:
                dup += 1
            else:
                seen.add(key)
        return {
            "analysis_ok": bool(cov.get("ok")),
            "observations": min(int(cov.get("observations") or 0), _QUALITY_OBS_CAP),
            "claims": len(claims),
            "bound": counts["bound"], "partial": counts["partial"],
            "unsupported": counts["unsupported"], "needs_check": counts["needs_check"],
            "unsupported_or_unchecked": counts["unsupported"] + counts["needs_check"],
            "duplicate_blocks": dup,
        }
    except Exception:                            # noqa: BLE001 - 向量算不出按未知处理
        return {}


def _quality_tiebreak(cur_q: dict, cand_q: dict) -> tuple[bool, str] | None:
    """硬条件相同时的质量向量比较；返回 None 表示"向量不可用/无差异"。

    顺序（逐项可解释）：有分析 → 未支持/待核查更少 → 分析观察更多（独立句子、有上限）
    → 重复块更少。长度始终不参与。
    """
    if not (isinstance(cur_q, dict) and isinstance(cand_q, dict) and cur_q and cand_q):
        return None
    if bool(cand_q.get("analysis_ok")) != bool(cur_q.get("analysis_ok")):
        if cand_q.get("analysis_ok"):
            return True, ("候选稿的分析满足最低要求（数据观察 + 意义/局限），"
                          "当前稿不满足")
        return False, ("候选稿的分析不满足最低要求（缺数据观察或意义/局限），"
                       "保留当前稿")
    cu = int(cur_q.get("unsupported_or_unchecked") or 0)
    ca = int(cand_q.get("unsupported_or_unchecked") or 0)
    if ca != cu:
        diff = f"未支持/待核查结论 {cu}→{ca}"
        return (True, f"质量向量更优：{diff}") if ca < cu else (
            False, f"候选稿的未支持/待核查结论更多（{diff}），保留当前稿")
    co = int(cur_q.get("observations") or 0)
    cno = int(cand_q.get("observations") or 0)
    if cno != co:
        diff = f"分析观察 {co}→{cno}"
        return (True, f"质量向量更优：{diff}") if cno > co else (
            False, f"候选稿的分析观察更少（{diff}），保留当前稿")
    cd = int(cur_q.get("duplicate_blocks") or 0)
    cnd = int(cand_q.get("duplicate_blocks") or 0)
    if cnd != cd:
        diff = f"重复块 {cd}→{cnd}"
        return (True, f"质量向量更优：{diff}") if cnd < cd else (
            False, f"候选稿的重复块更多（{diff}），保留当前稿")
    return None


def compare_versions(cur_text: str, cand_text: str, *,
                     cur_acceptance: dict | None = None,
                     cand_acceptance: dict | None = None,
                     cur_quality: dict | None = None,
                     cand_quality: dict | None = None) -> tuple[bool, str]:
    """候选稿是否**实质优于**当前稿。返回 `(improved, reason)`（reason 可入日志/状态）。

    `cur_quality`/`cand_quality`：两稿的**质量向量**（`candidate_quality` 的产出）。
    硬条件（验收等级/缺口/占位/回抄）相同且两稿都有向量时，按"有分析 → 未支持结论更少
    → 分析观察更多 → 重复块更少"决定，并把差异写进理由；向量缺失按未知处理，不参与。
    长度始终不作为改进依据。
    """
    cur = quality_snapshot(cur_text, cur_acceptance)
    cand = quality_snapshot(cand_text, cand_acceptance)

    # 当前无稿（首次产出）→ 必须采用候选稿，否则首稿永远落不了地
    if not str(cur_text or "").strip() and str(cand_text or "").strip():
        return True, "当前无稿，采用候选稿"
    if str(cur_text or "").strip() and not str(cand_text or "").strip():
        return False, "候选稿为空，保留当前版本"

    if cand["accept_rank"] > cur["accept_rank"]:
        return True, f"验收等级提升（{cur['overall'] or '未知'} → {cand['overall'] or '未知'}）"
    if cand["accept_rank"] < cur["accept_rank"]:
        return False, (f"候选验收等级更低（{cand['overall'] or '未知'} < {cur['overall'] or '未知'}），"
                       "长度不作为理由保留")
    if cand["gaps"] < cur["gaps"]:
        return True, f"验收缺口减少（{cur['gaps']} → {cand['gaps']}）"
    if cand["gaps"] > cur["gaps"]:
        return False, f"验收缺口增加（{cur['gaps']} → {cand['gaps']}）"
    if cand["placeholders"] < cur["placeholders"]:
        return True, f"未完成痕迹减少（占位 {cur['placeholders']} → {cand['placeholders']}）"
    if cand["placeholders"] > cur["placeholders"]:
        return False, f"未完成痕迹增加（占位 {cur['placeholders']} → {cand['placeholders']}）"
    if cand["echo"] < cur["echo"]:
        return True, "删除了重复需求块（正文更聚焦）"
    if cand["echo"] > cur["echo"]:
        return False, "候选稿重复用户需求块，属篇幅膨胀而非实质改进"
    # D2：硬条件相同 → 比有证据的质量向量（问题覆盖/未支持结论/分析遗漏与重复）
    verdict = _quality_tiebreak(cur_quality or {}, cand_quality or {})
    if verdict is not None:
        return verdict
    # 全部相同：不做长度替换，保持当前版本（此前"更长即更好"会把纠错稿丢掉）
    _vec = ""
    if cur_quality and cand_quality:
        _vec = (f"、质量向量无差异（观察 {cur_quality.get('observations')}、"
                f"未支持/待核查 {cur_quality.get('unsupported_or_unchecked')}、"
                f"重复块 {cur_quality.get('duplicate_blocks')}）")
    return False, (f"硬约束无差异（验收 {cur['overall'] or '未知'}、缺口 {cur['gaps']}、"
                   f"占位 {cur['placeholders']}）{_vec}，保持当前版本；长度差异"
                   f"（{cur['length']} → {cand['length']} 字符）不作为改进依据")
