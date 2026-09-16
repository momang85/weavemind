# -*- coding: utf-8 -*-
"""R0 边界回归（《M0完成复核与下一步修订_20260916》§2）。

覆盖两件事，都走**真实验收入口**（`run_acceptance`），不是断言 helper 返回值：
- R0.4 计算反例：常量角色按**操作数位置**判定；每个财务输入必须绑定 指标/期间/币种/单位；
- R0.1（本文件先落"目标达成 vs 诚实披露"的判定面，后续批次继续扩）。

反例来源是架构复核给出的最小复现；正向对照必须一起保留，避免"收紧阈值把真话否掉"。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import acceptance_checker as ac  # noqa: E402
import workspace as ws_mod  # noqa: E402

DISCLAIMER = (
    "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，不构成任何投资建议；"
    "数据来源于公开渠道，可能存在延迟或误差；据此操作风险自担。\n"
)


def _run(tid: str, goal: str, report: str) -> dict:
    """建临时工作区跑真实验收。

    用户材料的通道就是 **goal 文本**（`run_acceptance` 把 goal 注入为
    `user_material` 来源）——夹具必须按同一接线走，否则材料根本没进判定。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wm_r0_"))
    old_root = ws_mod.WORKSPACE_ROOT
    ws_mod.configure_workspace_root(str(tmp))
    try:
        ws = ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        return ac.run_acceptance(tid, goal, report, ws, capabilities=["web_search"])
    finally:
        ws_mod.WORKSPACE_ROOT = old_root
        shutil.rmtree(tmp, ignore_errors=True)


def _goal_with_material(base: str, material: str) -> str:
    """把材料并进目标文本（= 生产的 user_material 通道）。"""
    return f"{base}（材料：{material}）" if material else base


def _num(report: str, tid: str, goal: str, user_material: str = "") -> dict:
    """取验收里数字可信度检查的原始结果（含每条数字的四类结论）。"""
    res = _run(tid, _goal_with_material(goal, user_material), report)
    return (res.get("checks") or {}).get("number_traceability") or {}


class TestR04ConstantRoles(unittest.TestCase):
    """常量豁免按位置判定：分母位置的 1 不是换算常量。"""

    def test_operand_roles_flags_denominator_one_as_input(self):
        roles = ac._operand_roles("1000/1-1")
        self.assertEqual(roles, [("1000", "input"), ("1", "input"), ("1", "conversion")],
                         "分母的 1 必须按输入处理，只有末尾 -1 是换算角色")

    def test_operand_roles_allows_growth_and_percent_conversion(self):
        self.assertEqual(ac._operand_roles("1200/1000-1")[-1], ("1", "conversion"))
        self.assertEqual(ac._operand_roles("1200/1000*100")[-1], ("100", "conversion"))

    def test_missing_base_period_cannot_use_one_as_denominator(self):
        """反例 1：材料只有 2025 年收入时，`1000/1-1` 不得认证为增长 99900%。"""
        goal = "分析公司2025年经营表现"
        user_material = "2025年营业收入1000万元"
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 营业收入增长 | 99900%（1000/1-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c1", goal, user_material)
        bad = [u for u in (nt.get("untraceable") or []) if "99900" in str(u.get("raw"))]
        ok_rows = [t for t in (nt.get("traceable") or [])
                   if "99900" in str(t.get("raw")) and t.get("indicator_period") == "ok"]
        self.assertEqual(ok_rows, [], f"缺基期的伪增长不得认证：{ok_rows}")
        self.assertTrue(bad or nt.get("covered_ratio", 1) < 1.0,
                        "该伪增长应计入不可溯源/未绑定，而不是通过")

    def test_correct_growth_with_bound_base_period_still_passes(self):
        """正向对照：材料给出两期收入时，20% 必须仍然通过。"""
        goal = "分析公司2025年经营表现"
        user_material = "2024年营业收入1000万元；2025年营业收入1200万元"
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年营业收入增长 | 20%（1200/1000-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c1p", goal, user_material)
        rows = [t for t in (nt.get("traceable") or []) if "20" in str(t.get("raw"))]
        self.assertTrue(rows, f"正确计算必须可溯源：{nt.get('untraceable')}")
        self.assertEqual(rows[0].get("indicator_period"), "ok",
                         f"指标/期间应绑定为 ok：{rows[0]}")
        self.assertTrue(rows[0].get("arithmetic_ok"))


class TestR04InputRoleBinding(unittest.TestCase):
    """每个必需输入都要绑定 指标/期间/币种——分子对不等于整式对。"""

    def test_indicator_period_mismatch_on_denominator_is_rejected(self):
        """反例 2：2024 收入 + 2025 净利润，写"2025 净利润增长20%（1200/1000-1）"。"""
        goal = "分析公司2025年经营表现"
        user_material = "2024年营业收入1000万元；2025年净利润1200万元"
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年净利润增长 | 20%（1200/1000-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c2", goal, user_material)
        ok_rows = [t for t in (nt.get("traceable") or [])
                   if "20" in str(t.get("raw")) and t.get("indicator_period") == "ok"]
        self.assertEqual(ok_rows, [], f"指标/期间错配不得判 ok：{ok_rows}")

    def test_unrelated_same_value_source_does_not_break_correct_math(self):
        """无关同值来源不得破坏正确计算（联合匹配 + 顺序不敏感）。"""
        goal = "分析公司2025年经营表现"
        user_material = (
            "2024年营业收入1000万元；2025年营业收入1200万元；"
            "2025年资本支出1000亿元"
        )
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年营业收入增长 | 20%（1200/1000-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c2p", goal, user_material)
        rows = [t for t in (nt.get("traceable") or []) if "20" in str(t.get("raw"))]
        self.assertTrue(rows, f"无关同值来源不得破坏正确计算：{nt.get('untraceable')}")
        self.assertEqual(rows[0].get("indicator_period"), "ok")

    def test_currency_mixing_is_rejected(self):
        """币种混用：万元 与 万美元 相除不是增速。"""
        goal = "分析公司2025年经营表现"
        user_material = "2024年营业收入1000万元；2025年营业收入1200万美元"
        report = (
            "# 公司2025年经营分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年营业收入增长 | 20%（1200/1000-1） | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-c3", goal, user_material)
        ok_rows = [t for t in (nt.get("traceable") or [])
                   if "20" in str(t.get("raw")) and t.get("indicator_period") == "ok"]
        self.assertEqual(ok_rows, [], f"币种混用不得判 ok：{ok_rows}")

    def test_currency_helper(self):
        self.assertEqual(ac._currency_of("万元"), "CNY")
        self.assertEqual(ac._currency_of("万美元"), "USD")
        self.assertEqual(ac._currency_of("亿港元"), "HKD")
        self.assertEqual(ac._currency_of("%"), "")


class TestR04NoRegressionOnExistingFixtures(unittest.TestCase):
    """既有 V1 反例仍然成立（本轮只收紧"常量位置"和"逐操作数绑定"）。"""

    def test_full_formula_with_unit_scale_mismatch_still_rejected(self):
        goal = "分析公司2025年经营表现"
        user_material = "2024年营业收入1000万元；2025年营业收入1200万元"
        report = (
            "# 公司2025年经营分析\n\n营业收入合计 2200亿元（1200+1000），"
            "同比增长 20%（1200/1000-1）。\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为用户提供材料。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-reg1", goal, user_material)
        bad = [t for t in (nt.get("traceable") or []) if "2200" in str(t.get("raw"))]
        self.assertEqual(bad, [], "万元相加结果写成亿元不得通过（量纲错一万倍）")

    def test_substring_boundary_still_enforced(self):
        goal = "分析公司2025年经营表现"
        user_material = "2025年营业收入1200万元"
        report = (
            "# 公司2025年经营分析\n\n| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 收入 | 12亿元 | 用户材料 |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31。\n\n" + DISCLAIMER
        )
        nt = _num(report, "r0-reg2", goal, user_material)
        hit = [t for t in (nt.get("traceable") or []) if str(t.get("raw")).startswith("12")]
        self.assertEqual(hit, [], "12 不得命中 1200（按值匹配，不是子串）")


class TestR01GoalRequirementBoundary(unittest.TestCase):
    """R0.1：目标达成与诚实披露分开，缺必需数据/来源不得整体 pass。

    反例来自架构复核：目标"研究公司2025年经营表现，提供数据并引用官方来源"，
    正文写"营业收入未披露。归母收益未披露。经营净流入未披露。"、来源为空时，
    此前 `run_acceptance` 仍给 `overall=pass`（零数字/无来源反而更容易过）。
    """

    GOAL = "研究公司2025年经营表现，提供数据并引用官方来源"
    BODY = (
        "# 公司2025年经营表现\n\n营业收入未披露。归母收益未披露。经营净流入未披露。\n\n"
        "## 免责声明\n\n本文基于定性判断，不构成任何投资建议。\n"
    )

    def _cov(self, goal: str, body: str) -> dict:
        res = _run("r0-01", goal, body)
        return {"overall": res.get("overall"), "gaps": res.get("gaps") or [],
                "cov": (res.get("checks") or {}).get("requirement_coverage") or {}}

    def test_repro_no_longer_passes(self):
        out = self._cov(self.GOAL, self.BODY)
        self.assertNotEqual(out["overall"], "pass", "缺必需数据与来源不得整体 pass")
        self.assertIn(out["overall"], ("partial", "unknown", "fail"))
        self.assertTrue(out["cov"].get("needs_data"))
        self.assertTrue(out["cov"].get("needs_sources"))
        self.assertTrue(out["cov"].get("honest_disclosure"), "写了'未披露'应识别为诚实披露")
        self.assertTrue(out["gaps"], "必须给出可操作缺口")
        self.assertIn("来源", " ".join(out["gaps"]))

    def test_undisclosed_missing_data_is_fail(self):
        """缺数据且**不披露** → fail（不是 partial）。"""
        body = (
            "# 公司2025年经营表现\n\n公司经营稳健，渠道结构持续优化，"
            "产品结构向高端集中，品牌势能延续。\n\n"
            "## 免责声明\n\n本文基于定性判断，不构成任何投资建议。\n"
        )
        out = self._cov(self.GOAL, body)
        self.assertEqual(out["overall"], "fail", out)
        self.assertFalse(out["cov"].get("honest_disclosure"))

    def test_qualitative_goal_is_not_forced_to_produce_numbers(self):
        """正向对照：明确"定性即可、不需要数字"的研究不得被要求造数字。"""
        goal = "梳理公司渠道与产品结构的主要变化方向（定性分析即可，不需要具体财务数字）"
        body = (
            "# 渠道与产品结构变化\n\n## 渠道\n\n直营与经销比重持续调整，"
            "线上自营平台占比提升。\n\n## 产品结构\n\n高端产品比重上升，系列酒结构优化。\n\n"
            "## 数据时效\n\n数据截至 2025-12-31，来源为公司公开披露与主流财经报道。\n\n"
            "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
            "不构成任何投资建议；数据来源于公开渠道，可能存在延迟或误差。\n"
        )
        out = self._cov(goal, body)
        self.assertFalse(out["cov"].get("needs_data"),
                         "定性要求不应被判定为'必须给数字'")
        self.assertNotIn("目标要求给出", " ".join(out["gaps"]))

    def test_requirement_met_passes(self):
        """正向对照：目标要数据与来源，报告给足可溯源数据与来源清单 → pass。"""
        goal = ("分析公司2025年营业收入，给出数据与来源链接"
                "（材料：2025年营业收入1200亿元）")
        body = (
            "# 公司2025年营业收入分析\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 2025年营业收入 | 1200亿元 | [1] |\n\n"
            "## 数据时效\n\n数据截至 2025-12-31 年报披露日，日终更新。\n\n"
            "## 参考来源\n\n1. [公司2025年年报](https://www.example.com/annual-2025)\n\n"
            "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
            "不构成任何投资建议；数据来源于公开渠道，可能存在延迟或误差。\n"
        )
        out = self._cov(goal, body)
        self.assertTrue(out["cov"].get("goal_met"), out)
        self.assertEqual(out["overall"], "pass", out)


if __name__ == "__main__":
    unittest.main()
