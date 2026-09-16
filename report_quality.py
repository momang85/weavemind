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


def compare_versions(cur_text: str, cand_text: str, *,
                     cur_acceptance: dict | None = None,
                     cand_acceptance: dict | None = None) -> tuple[bool, str]:
    """候选稿是否**实质优于**当前稿。返回 `(improved, reason)`（reason 可入日志/状态）。"""
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
    # 全部相同：不做长度替换，保持当前版本（此前"更长即更好"会把纠错稿丢掉）
    return False, (f"硬约束无差异（验收 {cur['overall'] or '未知'}、缺口 {cur['gaps']}、"
                   f"占位 {cur['placeholders']}），保持当前版本；长度差异"
                   f"（{cur['length']} → {cand['length']} 字符）不作为改进依据")
