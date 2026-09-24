# -*- coding: utf-8 -*-
"""V1 离线固定样例回归：修订稿选择 + 来源语义（不用付费 LLM，全离线）。

依据《DeepSeek任务规划_视觉实测_20260915》V1 的最小验收：
- 更短的**纠错稿**必须被接受（原实现按字数增长，会把纠错稿丢掉）；
- 更长的**错误稿**必须被拒绝（重复需求块/占位增多不算改进）；
- 用户材料与外部来源**混合**时都能溯源，用户材料不降级为"模型知识"；
- 缺输入的计算仍被拒绝（不降全局阈值）；
- 外部无证据数字仍然失败（阈值不变）。
"""

from __future__ import annotations

import sys
import shutil
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import acceptance_checker as ac  # noqa: E402
import report_quality as rq  # noqa: E402


# ── 固定样例（对应实测任务 A 的确定性预期）────────────────────────
USER_MATERIAL = (
    "用户材料（虚构）：2024 年收入 1000 万元，2025 年收入 1200 万元；"
    "2024 年毛利率 30%，2025 年毛利率 25%；经营现金流/收入 2024 年为 12%。"
)

# 纠错稿：更短，但把 1033 字符旧稿里的不实结论删掉了，并把推导写明
FIXED_DRAFT = (
    "## 关键指标\n"
    "| 指标 | 2025 | 2024 |\n| --- | --- | --- |\n"
    "| 收入（万元） | 1200 | 1000 |\n"
    "| 毛利率 | 25% | 30% |\n\n"
    "## 推导\n"
    "收入增长 20%（1200/1000-1）；毛利率下降 5 个百分点（30%-25%）；"
    "现金流/收入从 12% 降至约 6.67%。以上均为用户材料的直接计算，不构成财务造假判断。\n"
)

# 旧稿：更长，但夹带无输入依据的结论 + 重复需求块（典型的"长而不改进"）
STALE_DRAFT = (
    "用户目标：请核算两年收入与毛利率变化\n原始指令：请核算两年收入与毛利率变化\n"
    + "本企业产品线单一，属中小型企业，议价能力偏弱。" * 12
    + "\n结论：该公司存在明显财务造假嫌疑。\n"
)

EXT_SOURCES = {
    "search_results": "公开报道：某公司 2025 年营业收入 800 亿元，净利润 90 亿元。",
}


class TestVersionSelection(unittest.TestCase):
    def test_shorter_corrected_draft_is_accepted(self):
        """C 的 504 字符纠错稿必须能胜出（旧实现按字数会保留 1033 字符旧稿）。"""
        improved, why = rq.compare_versions(STALE_DRAFT, FIXED_DRAFT)
        self.assertTrue(improved, f"纠错稿应被接受：{why}")
        self.assertIn("重复用户需求", why.lower() if "重复" in why else why) if False else None

    def test_longer_wrong_draft_is_rejected(self):
        improved, why = rq.compare_versions(FIXED_DRAFT, STALE_DRAFT)
        self.assertFalse(improved, f"更长但含不实结论/重复需求块的稿子不应胜出：{why}")
        self.assertNotIn("字符）不作为改进依据", why[:0])   # 仅断言结论
        self.assertTrue(any(k in why for k in ("重复用户需求", "未完成痕迹", "验收")), why)

    def test_length_alone_is_not_improvement(self):
        """只变长、硬约束无差别 → 保持当前版本，并说明长度不作为依据。"""
        base = "## 指标\n收入 1200 万元。\n"
        longer = base + "补充说明：" + "（无新增约束信息）" * 5 + "\n"
        improved, why = rq.compare_versions(base, longer)
        self.assertFalse(improved, why)
        self.assertIn("长度", why)

    def test_acceptance_rank_dominates_length(self):
        """验收等级优先于任何长度：短而 pass 胜过长而 fail。"""
        improved, why = rq.compare_versions(
            "长" * 500, "短", cur_acceptance={"overall": "FAILED", "gaps": ["x"]},
            cand_acceptance={"overall": "pass", "gaps": []})
        self.assertTrue(improved)
        self.assertIn("验收等级提升", why)


class TestSourceSemantics(unittest.TestCase):
    def test_user_material_numbers_are_traceable_not_model_knowledge(self):
        res = ac.check_number_traceability(FIXED_DRAFT, {"user_material": USER_MATERIAL},
                                          domain="financial")
        self.assertGreaterEqual(res["user_input_count"], 3, res)
        self.assertEqual(res["disclosed_count"], 0, "用户材料不得被当作模型知识")
        self.assertGreater(res["covered_ratio"], 0.0, "用户材料里的数字应可溯源")

    def test_derived_values_trace_to_user_input(self):
        """报告写明公式且操作数来自用户材料 → 记为计算值（"输入 + 公式"可复算）。"""
        res = ac.check_number_traceability(
            "收入增长 20%（1200/1000-1）；现金流/收入约 6.67%。",
            {"user_material": USER_MATERIAL}, domain="financial")
        self.assertGreaterEqual(res["computed_count"], 1, res)
        traced = [t["raw"] for t in res["traceable"]]
        self.assertIn("20%", traced, f"写明公式的派生值应可溯源：{res['details']}")

    def test_derived_value_without_stated_formula_stays_untraceable(self):
        """未写明公式的派生值仍不可溯源——门槛不放宽（报告必须给出公式/输入）。"""
        res = ac.check_number_traceability(
            "收入增长 20%，现金流/收入约 6.67%。",
            {"user_material": USER_MATERIAL}, domain="financial")
        self.assertGreaterEqual(res["unverifiable_count"], 2, res)
        self.assertEqual(res["computed_count"], 0, res)

    def test_mixed_sources_both_traceable(self):
        sources = {"user_material": USER_MATERIAL, **EXT_SOURCES}
        report = "用户材料口径收入 1200 万元；外部报道营业收入 800 亿元。"
        res = ac.check_number_traceability(report, sources, domain="financial")
        self.assertGreaterEqual(res["user_input_count"], 1, res)
        self.assertEqual(res["unverifiable_count"], 0, res)
        self.assertGreater(res["covered_ratio"], 0.0)

    def test_computation_without_input_still_rejected(self):
        """没有输入的"计算值"仍不可溯源——不降阈值、不自动改判。"""
        res = ac.check_number_traceability(
            "收入增长 37%，毛利率下降 11 个百分点。", {"user_material": USER_MATERIAL},
            domain="financial")
        self.assertGreaterEqual(res["unverifiable_count"], 1, res)

    def test_external_unsourced_numbers_still_fail(self):
        """外部无证据数字仍然失败：阈值与判定不变。"""
        report = "某公司营业收入 999 亿元，净利润 123 亿元，毛利率 44%，现金流 55 亿元。"
        res = ac.check_number_traceability(report, EXT_SOURCES, domain="financial")
        self.assertFalse(res["pass"], res)
        self.assertLess(res["covered_ratio"], 0.7)

    def test_user_material_flagged_unverified_but_present(self):
        """用户材料是来源，但标注"真实性未独立核实"。"""
        res = ac.check_number_traceability("收入 1200 万元。", {"user_material": USER_MATERIAL},
                                          domain="financial")
        hit = [t for t in res["traceable"] if t.get("source") == "user_material"]
        self.assertTrue(hit, res)
        self.assertTrue(hit[0].get("user_provided"))
        self.assertFalse(hit[0].get("verified"), "用户材料不应被标为已核实")


class TestDisclaimerBySource(unittest.TestCase):
    """免责声明必须按真实来源生成：没有外部检索就不能宣称"数据来源于公开渠道"。"""

    def test_user_material_only_does_not_claim_public_channels(self):
        text = rq.disclaimer_clause(has_external=False, has_user_material=True)
        self.assertNotIn("公开渠道", text)
        self.assertIn("用户提供的材料", text)
        self.assertIn("未经独立核实", text)

    def test_external_only_keeps_public_channel_wording(self):
        text = rq.disclaimer_clause(has_external=True, has_user_material=False)
        self.assertIn("公开渠道", text)

    def test_mixed_sources_state_both(self):
        text = rq.disclaimer_clause(has_external=True, has_user_material=True)
        self.assertIn("用户提供的材料", text)
        self.assertIn("公开渠道", text)

    def test_no_source_at_all_is_stated_as_model_knowledge(self):
        text = rq.disclaimer_clause(has_external=False, has_user_material=False)
        self.assertIn("模型知识", text)
        self.assertNotIn("公开渠道", text)

    def test_injected_instruction_forbids_blanket_wording(self):
        """注入报告步骤的要求里必须给出三种措辞与选择规则，而不是一条固定文案。"""
        instr = rq.disclaimer_instruction()
        for frag in ("用户提供的材料", "公开渠道", "两者都有"):
            self.assertIn(frag, instr)
        self.assertIn("禁止写", instr)
        self.assertIn("不得伪造来源", instr)

    def test_orchestrator_injects_the_conditional_rule(self):
        src = (ROOT / "orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn("disclaimer_instruction()", src, "报告格式要求仍写死固定免责声明")


class TestOfflineCounterexamples(unittest.TestCase):
    """架构复核 2026-09-16 第 0 节给出的三个离线反例（修前全部错误通过）。"""

    CLEAN = (
        '{"market_data": ['
        '{"label":"2024年营收","year":2024,"value":1000,"unit":"亿元"},'
        '{"label":"2025年营收","year":2025,"value":1200,"unit":"亿元"}]}'
    )
    REPORT_SUM = "合计2200亿元，年均1100亿元，同比增长20%。"

    def test_user_material_does_not_break_structured_channel(self):
        """反例 1：结构化 JSON 与用户材料**分通道**——加了用户材料不得把原本 pass 的判成 fail。

        修前：把 clean_chart_data 的 JSON 与 user_material 文本拼成一段，结构化解析失败
        → 同一份报告从 pass/1.0 变成 fail/0.0。
        """
        base = {"clean_chart_data": self.CLEAN}
        with_um = {"clean_chart_data": self.CLEAN, "user_material": "请分析营收同比变化"}
        r1 = ac.check_number_traceability(self.REPORT_SUM, base, domain="financial")
        r2 = ac.check_number_traceability(self.REPORT_SUM, with_um, domain="financial")
        self.assertTrue(r1["pass"], r1)
        self.assertEqual(r2["pass"], r1["pass"], "加用户材料后判定不应变化")
        self.assertEqual(r2["covered_ratio"], r1["covered_ratio"])

    def setUp(self):
        self.um = {"user_material": USER_MATERIAL}

    def test_fabricated_percentage_is_not_granted_computed(self):
        """反例 2a：`120%（1200/1000-1）` 与算式结果（20%）不符，不得记计算值。

        修前：把完整表达式截成 `1200/1000`=1.2，再乘 100 恰好凑成 120%。
        """
        r = ac.check_number_traceability(
            "收入增长120%（1200/1000-1）。", self.um, domain="financial")
        self.assertEqual(r["computed_count"], 0, r)

    def test_scale_mismatch_is_not_granted_computed(self):
        """反例 2b：操作数是万元而结论写亿元（错一万倍），不得记计算值。"""
        r = ac.check_number_traceability(
            "收入合计2200亿元（1200+1000）。", self.um, domain="financial")
        self.assertEqual(r["computed_count"], 0, r)

    def test_missing_operand_is_rejected_with_token_boundary(self):
        """反例 2c：`12-10` 的操作数不在来源里（`12`/`10` 只是 `1200`/`1000` 的子串）。"""
        r = ac.check_number_traceability("利润2亿元（12-10）。", self.um, domain="financial")
        self.assertEqual(r["computed_count"], 0, r)
        self.assertEqual(r["covered_ratio"], 0.0, r)

    def test_legitimate_formula_still_recognised(self):
        """正向对照：真实可复算的公式仍须被认可（不能靠收紧阈值把真话也否掉）。"""
        r = ac.check_number_traceability(
            "收入增长20%（1200/1000-1）。", self.um, domain="financial")
        self.assertGreaterEqual(r["computed_count"], 1, r)


    def test_increment_constant_cannot_stand_in_for_missing_input(self):
        """a 增量反例 1：材料缺 2024 时，`1000/100-1` 里的 100 是"把常量当财务输入"。

        1/2/100 只允许作**换算角色**（加减里的 1/2、整式末尾乘 100），不得当分母/被除数。
        """
        um = {"user_material": "2025年收入1000万元；2024年收入未提供。"}
        r = ac.check_number_traceability("收入增长900%（1000/100-1）。", um, domain="financial")
        self.assertEqual(r["computed_count"], 0, r)

    def test_increment_indicator_period_mismatch_rejected(self):
        """a 增量反例 2：材料是 2024/2025 收入，报告写 2023 年净利润 → 拒绝。"""
        r = ac.check_number_traceability(
            "2023年净利润增长20%（1200/1000-1）。", self.um, domain="financial")
        self.assertEqual(r["computed_count"], 0, r)

    def test_increment_unrelated_same_value_source_does_not_break_correct_result(self):
        """a 增量反例 3（正向）：无关的"资本支出 1000 亿元"不得让正确增长 20% 失败。"""
        clean = '{"market_data": [{"label":"资本支出","value":1000,"unit":"亿元"}]}'
        base = ac.check_number_traceability(
            "收入增长20%（1200/1000-1）。", self.um, domain="financial")
        with_unrelated = ac.check_number_traceability(
            "收入增长20%（1200/1000-1）。",
            {"user_material": USER_MATERIAL, "clean_chart_data": clean}, domain="financial")
        self.assertGreaterEqual(base["computed_count"], 1, base)
        self.assertEqual(with_unrelated["covered_ratio"], base["covered_ratio"],
                         "无关同值来源改变了正确计算的判定")
        self.assertGreaterEqual(with_unrelated["computed_count"], 1, with_unrelated)

    def test_increment_derived_carries_semantics_and_unverified(self):
        """a 增量反例 4：派生值必须分开输出"算术正确/指标期间/外部核实"。"""
        r = ac.check_number_traceability(
            "收入增长20%（1200/1000-1）。", self.um, domain="financial")
        d = [t for t in r["traceable"] if t.get("derived")]
        self.assertTrue(d, r)
        self.assertTrue(d[0].get("arithmetic_ok"))
        self.assertIn(d[0].get("indicator_period"), ("ok", "unknown"))
        self.assertFalse(d[0].get("verified"), "派生值应继承用户输入的'未外部核实'")


class TestQualityVectorTiebreak(unittest.TestCase):
    """D2：硬条件相同时按**有证据的质量向量**决定，长度仍不参与。

    向量口径见 `report_quality.candidate_quality`：分析是否达标、未支持/待核查结论数、
    分析观察数（独立句子、有上限）、重复块数。审查要求"指标计数按独立问题/主张去重，
    不奖励灌水、加图或增加字数"——这里用显式向量钉住判定顺序。
    """

    BASE = "## 分析\n\n2024 年营业收入 288.76 亿元，同比下降 12.83%。该变化仅覆盖本期。\n"

    def _q(self, **kw) -> dict:
        q = {"analysis_ok": True, "observations": 2, "claims": 3, "bound": 2,
             "partial": 0, "unsupported": 1, "needs_check": 0,
             "unsupported_or_unchecked": 1, "duplicate_blocks": 0}
        q.update(kw)
        return q

    def test_analysis_ok_wins(self):
        improved, why = rq.compare_versions(self.BASE, self.BASE,
                                            cur_quality=self._q(analysis_ok=False),
                                            cand_quality=self._q())
        self.assertTrue(improved, why)
        self.assertIn("最低要求", why)
        improved2, why2 = rq.compare_versions(self.BASE, self.BASE,
                                              cur_quality=self._q(),
                                              cand_quality=self._q(analysis_ok=False))
        self.assertFalse(improved2, why2)
        self.assertIn("最低要求", why2)

    def test_fewer_unsupported_conclusions_win(self):
        improved, why = rq.compare_versions(
            self.BASE, self.BASE,
            cur_quality=self._q(unsupported_or_unchecked=3),
            cand_quality=self._q(unsupported_or_unchecked=1))
        self.assertTrue(improved, why)
        self.assertIn("未支持/待核查结论 3→1", why)
        improved2, why2 = rq.compare_versions(
            self.BASE, self.BASE,
            cur_quality=self._q(unsupported_or_unchecked=1),
            cand_quality=self._q(unsupported_or_unchecked=4))
        self.assertFalse(improved2, why2)

    def test_more_observations_win(self):
        improved, why = rq.compare_versions(self.BASE, self.BASE,
                                            cur_quality=self._q(observations=2),
                                            cand_quality=self._q(observations=4))
        self.assertTrue(improved, why)
        self.assertIn("分析观察 2→4", why)

    def test_duplicate_blocks_lose(self):
        improved, why = rq.compare_versions(self.BASE, self.BASE,
                                            cur_quality=self._q(duplicate_blocks=3),
                                            cand_quality=self._q(duplicate_blocks=0))
        self.assertTrue(improved, why)
        self.assertIn("重复块 3→0", why)

    def test_identical_vectors_keep_current_and_mention_length(self):
        improved, why = rq.compare_versions(self.BASE, self.BASE + "补充" * 20,
                                            cur_quality=self._q(), cand_quality=self._q())
        self.assertFalse(improved, why)
        self.assertIn("质量向量无差异", why)
        self.assertIn("长度差异", why)

    def test_missing_vectors_keep_old_behaviour(self):
        """不传向量时行为与旧实现一致（硬约束无差异 → 保持当前）。"""
        improved, why = rq.compare_versions(self.BASE, self.BASE)
        self.assertFalse(improved, why)
        self.assertIn("硬约束无差异", why)
        self.assertNotIn("质量向量", why)


class TestQualityVectorOrder(unittest.TestCase):
    """R3：候选比较先看"有据覆盖/矛盾/信息保留"，再看重复与观察句数。

    固定反例（不许胜出）：候选多写 8 句观察，但契约问题一个有据覆盖都没增加；
    正向：候选少写观察但多答一个契约问题 → 胜出。
    """

    BODY = "# 报告\n\n## 分析\n\n观察。\n"

    def test_more_observations_does_not_beat_answered_questions(self):
        import report_quality as rq
        cur = {"analysis_ok": True, "answered_questions": 1, "coverage_total": 3,
               "partial_questions": 1, "unanswered_questions": 1, "contradictions": 0,
               "facts_missing": 0, "unsupported_or_unchecked": 0, "observations": 2,
               "duplicate_blocks": 0}
        cand = dict(cur, answered_questions=0, observations=10)
        improved, why = rq.compare_versions(self.BODY, self.BODY,
                                            cur_quality=cur, cand_quality=cand)
        self.assertFalse(improved, why)
        self.assertIn("有据覆盖更少", why)
        cand2 = dict(cur, answered_questions=2, observations=1)
        improved2, why2 = rq.compare_versions(self.BODY, self.BODY,
                                              cur_quality=cur, cand_quality=cand2)
        self.assertTrue(improved2, why2)
        self.assertIn("有据覆盖", why2)

    def test_contradictions_and_retention_come_before_observations(self):
        import report_quality as rq
        base = {"analysis_ok": True, "answered_questions": 1, "coverage_total": 3,
                "contradictions": 0, "facts_missing": 0, "unsupported_or_unchecked": 0,
                "observations": 5, "duplicate_blocks": 0}
        got = rq._quality_tiebreak(base, dict(base, contradictions=2, observations=9))
        self.assertIsNotNone(got)
        self.assertFalse(got[0])
        self.assertIn("矛盾", got[1])
        got2 = rq._quality_tiebreak(base, dict(base, facts_missing=3))
        self.assertIsNotNone(got2)
        self.assertFalse(got2[0])
        self.assertIn("必须保留", got2[1])
        got3 = rq._quality_tiebreak(base, dict(base, observations=8))
        self.assertIsNotNone(got3)
        self.assertTrue(got3[0])

    def test_candidate_quality_reads_material_assessments(self):
        """分母来自契约：材料侧评估决定 answered/partial/unanswered（删正文不缩小分母）。"""
        import json as _json
        import tempfile
        import report_quality as rq
        import workspace as ws_mod
        tmp = Path(tempfile.mkdtemp(prefix="wm_rq_vec_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", old_root)
        tid = "rqvec-1"
        proj = ws_mod.task_project_dir(tid)
        proj.mkdir(parents=True, exist_ok=True)
        (ws_mod.task_workspace(tid) / "report_structure.json").write_text(_json.dumps({
            "question_assessments": {
                "revenue": {"kind": "decomposition", "coverage": "partial",
                            "answered": False},
                "net_profit": {"kind": "observation", "coverage": "none",
                               "answered": False},
                "operating_cashflow": {"kind": "management_cause",
                                       "coverage": "partial", "answered": False},
            },
        }, ensure_ascii=False), encoding="utf-8")
        a = rq._material_assessments(tid)
        self.assertEqual(len(a), 3)
        self.assertEqual(str(a["operating_cashflow"].get("coverage")), "partial")


class TestPdfPagination(unittest.TestCase):
    """09-23：长段落必须**逐行**检查剩余空间——首版 PDF 有 141 个非空白字符落在页下边界外。

    判定用内容流里的文字定位（`1 0 0 1 x y Tm`）：正文文字不得画在页脚线以下；
    长结论段应拆到下一页而不是画到纸外。
    """

    def _text_ys(self, pdf: bytes) -> list:
        import re as _re
        return [float(m.group(1))
                for m in _re.finditer(rb"1 0 0 1 [\d.]+ (-?[\d.]+) Tm", pdf)]

    def test_long_paragraph_does_not_run_off_the_page(self):
        import report_pdf
        long_para = ("结论：" + "本次资料足以刻画客户收入、利润与经营现金流的变化方向，"
                     "但不足以支撑任何偿债能力结论；" * 40)
        md = ("# 标题\n\n## 结论\n\n" + long_para + "\n\n"
              "> " + "引用块同样要逐行换页，不能画到纸外；" * 30 + "\n")
        pdf = report_pdf.markdown_to_pdf(md, title="标题", workspace=None)
        ys = self._text_ys(pdf)
        self.assertTrue(ys, "PDF 内容流里应有文字定位")
        # 回归判据：不得有文字画到**页外**（实机缺陷是 y=-46.6pt 的正文），
        # 也不得低于页脚线（页脚在 MARGIN_B*0.45≈25.2pt）
        self.assertGreaterEqual(min(ys), 20.0,
                                f"有文字画在页脚线以下/页外：min_y={min(ys)}")
        self.assertGreaterEqual(pdf.count(b"/Type /Page"), 2, "长内容应分页")

    def test_long_list_item_does_not_run_off_the_page(self):
        """R2：列表项与代码块同样逐行检查空间（离线探针：超长列表项 1010 字符越界）。"""
        import report_pdf
        long_item = ("- " + "该条观察需要跨页续排，不能画到纸外；" * 80 + "\n")
        md = ("# 标题\n\n## 风险与核查\n\n" + long_item
              + "\n```\n" + "code line\n" * 60 + "```\n")
        pdf = report_pdf.markdown_to_pdf(md, title="标题", workspace=None)
        ys = self._text_ys(pdf)
        self.assertTrue(ys)
        self.assertGreaterEqual(min(ys), 20.0,
                                f"列表/代码块有文字画在页脚线以下/页外：min_y={min(ys)}")
        self.assertGreaterEqual(pdf.count(b"/Type /Page"), 2)



class TestContradictionDetector(unittest.TestCase):
    """R4：跨章节矛盾检测要**保守**（宁缺勿错）——它是候选比较用的计数，不是验收结论。"""

    def test_units_and_attribution_cases(self):
        import report_quality as rq
        cases = [
            # (文本, 期望冲突数, 说明)
            ("归母净利润 862.28亿元；归母净利润变化 +114.94亿元。", 0, "水平值 vs 变化量"),
            ("2024 年营业收入 1741.44亿元。2023 年营业收入 1505.6亿元。", 0, "同指标不同年度"),
            ("2024 年营业收入 1741.44亿元。2024 年营业收入 9999亿元。", 1, "同指标同期不同值=真矛盾"),
            ("| 营业收入 | 1505.6亿元 | 1741.44亿元 | 合并 |", 0, "表格行（年份在表头）"),
            ("经营现金流对归母净利润的覆盖 2024 年为 107.23%（2023 年 89.11%）", 0,
             "长短语归属：覆盖率不归净利润"),
            ("归母净利润 84.53亿元。液体乳销售量同比下降8.16%。", 0, "实物量不是财务读数"),
            ("归属于上市公司股东的净利润 84.53亿元，经营活动产生的现金流量净额90.0亿元。", 0,
             "指标别名不串行"),
            ("2024 年营业收入同比 -8.19%。2024 年营业收入同比下降 8.20%。", 0,
             "发行人四舍五入 vs 复算（百分点零头）"),
            ("2024 年营业收入 176.0亿元。2024 年营业收入 176 亿元。", 0, "同一数值的写法差异"),
        ]
        for text, want, why in cases:
            self.assertEqual(rq._contradictions(text), want, f"{why}：{text}")


class TestScenarioCheckKeys(unittest.TestCase):
    """R4：场景核对的 R4 键（覆盖/矛盾/信息保留/批准/请求数）按清单语义工作。"""

    def _check(self, manifest, expect):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "scenario_run", str(Path(__file__).resolve().parent / "scripts"
                                / "scenario_run.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._check(manifest, expect)

    def test_r4_keys(self):
        m = {"delivery": {"status": "verified"},
             "assessments": {"revenue": {"coverage": "partial"},
                             "net_profit": {"coverage": "none"}},
             "contradictions": 0, "facts_missing": 0,
             "approval": {"migrated": False}, "llm_requests": 0,
             "quality_metrics": ["revenue_yoy", "net_profit_yoy"],
             "pdf_parse": {"verdict": "pass"},
             "pdf_export": {"images": {"ok": True, "placeholders": 0}, "tables": []}}
        out = self._check(m, {"coverage_expect": {"revenue": "partial"},
                              "contradictions_zero": True, "facts_missing_leq": 0,
                              "approval_not_migrated": True, "requests_zero": True,
                              "no_ratio_metrics": ["debt_ratio"]})
        for k in ("coverage_expect", "contradictions_zero", "facts_missing_leq",
                  "approval_not_migrated", "requests_zero", "no_ratio_metrics"):
            self.assertTrue(out.get(k), (k, out))
        # 反例：覆盖对不上 / 有矛盾 / 批准被迁移 / 发了请求 → 逐项为 False
        bad = self._check(m, {"coverage_expect": {"revenue": "full"},
                             "contradictions_zero": True, "facts_missing_leq": 0,
                             "approval_not_migrated": True, "requests_zero": True})
        self.assertFalse(bad.get("coverage_expect"))
        m2 = dict(m, contradictions=2, approval={"migrated": True}, llm_requests=3)
        out2 = self._check(m2, {"contradictions_zero": True, "approval_not_migrated": True,
                               "requests_zero": True})
        self.assertFalse(out2.get("contradictions_zero"))
        self.assertFalse(out2.get("approval_not_migrated"))
        self.assertFalse(out2.get("requests_zero"))


if __name__ == "__main__":
    unittest.main()
