# -*- coding: utf-8 -*-
"""任务25（🔴 合规红线）验收：免责声明必须**强制进报告**。

根因：实机 ag-049 的验收缺口就是"报告缺少'免责声明'与'不构成投资建议'
（AI 生成标识）"——编排器格式要求里从没写过这条红线，报告生成器也没有兜底，
模型没写就直接交付出去。四步收口：
  ① 生成侧指令：`_REPORT_FORMAT_REQUIREMENTS` 第 6 条写明强制要求与基线文案
     （与注入模板逐字一致，避免"指令说的"和"系统补的"两个版本）；
  ② 落盘兜底：报告生成器在写文件前，报告完全没有"免责声明"字样时自动补
     `## 免责声明`（插在 `## 附录` **之前**，无附录则追加文末），
     已有内容一律不动、不重复注入；
  ③ 验收侧：`check_disclaimer` 增加 **AI 生成标识**检测（AI 生成/AI自动生成/
     人工智能生成），基础要素齐但缺 AI 标识 → 检查判 fail + **弱缺口**
     （`counted=False`，不翻转 overall：存量合规措辞的报告不因新增要素翻红，
     但缺口必须可见）；基础要素本身缺失仍按原有路径判负；
  ④ 交付链路（`report_brief` 汇编 / `acceptance_checker` 复核）都按 ② 的
     关键词判定，注入幂等。
"""
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import acceptance_checker as ac  # noqa: E402
import orchestrator_v2 as ov  # noqa: E402
import report_quality as rq  # noqa: E402
from workers.report_generator_worker import ReportGeneratorWorker  # noqa: E402

# 标准文案（注入模板与格式要求必须同一条，逐字比对）。
# U90-R4：基线不再咬定"数据来源于公开披露"——声明必须按实际采用的来源措辞，
# 基线只用于"完全没有任何外部来源"的兜底（与 workers/report_generator_worker
# 的 STANDARD_DISCLAIMER、orchestrator_v2._REPORT_FORMAT_REQUIREMENTS 逐字一致）。
BASE_SENTENCE = ("本报告由 AI 生成，仅供参考，不构成任何投资建议；数据与结论以报告内"
                 "来源清单与可复算底稿为准，请注意其时效性与适用范围")

FULL_DISCLAIMER = f"## 免责声明\n\n{BASE_SENTENCE}。\n"

BODY = (
    "# 某公司两期经营研究\n\n"
    "## 数据时效\n\n行情数据截至 2026-08-30 15:00 收盘。\n\n"
    "## 执行摘要\n\n营收增长 8%，经营平稳。\n\n"
    "## 风险与结论\n\n结论：仅陈述水平与变化。\n"
)

REFS = "## 参考来源\n\n1. [2024 年年度报告](https://example.com/a)\n"


def _report(disclaimer: str = "full", *, appendix: bool = True) -> str:
    """拼装样本：full=三要素齐 / none=完全没有 / no_ai=有声明但无 AI 标识。"""
    text = BODY
    if disclaimer == "full":
        text += "\n" + FULL_DISCLAIMER
    elif disclaimer == "no_ai":
        text += ("\n## 免责声明\n\n本报告不构成任何投资建议；数据来源于公开披露"
                 "，可能存在时效性差异和获取误差。\n")
    elif disclaimer == "legacy_auto":
        text += ("\n## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
                 "不构成任何投资建议。\n")
    if appendix:
        text += "\n## 附录\n\n" + REFS
    return text


class TestFormatRequirementMandate(unittest.TestCase):
    """① 生成侧：编排器给报告步骤的格式要求里写死这条红线。"""

    def test_requirements_carry_disclaimer_mandate(self):
        reqs = ov._REPORT_FORMAT_REQUIREMENTS
        self.assertIn("6. 免责声明", reqs)
        self.assertIn("合规红线", reqs)
        self.assertIn("## 免责声明", reqs)
        self.assertIn("本报告由 AI 生成", reqs, "AI 生成标识必须在指令里点名")
        self.assertIn("不构成任何投资建议", reqs)

    def test_mandate_sentence_equals_injected_template(self):
        """指令里的基线文案与系统兜底注入的模板逐字一致（不能两个版本）。"""
        reqs = ov._REPORT_FORMAT_REQUIREMENTS
        template = ReportGeneratorWorker.STANDARD_DISCLAIMER
        self.assertIn(BASE_SENTENCE, template, "注入模板必须含基线文案")
        self.assertIn(BASE_SENTENCE, reqs, "格式要求必须含同一条基线文案")
        self.assertIn("免责声明", reqs)

    def test_dynamic_wording_instruction_not_clobbered(self):
        """既有动态措辞入口不被本次静态红线顶掉（`test_report_quality` 同一条守卫）：
        要求里必须同时保留「按真实来源选择措辞」的动态指令与其结果文本。"""
        reqs = ov._REPORT_FORMAT_REQUIREMENTS
        dynamic = rq.disclaimer_instruction()
        self.assertIn("按**真实来源**选择措辞", reqs)
        self.assertIn("真实性未经独立核实", dynamic)
        self.assertIn("真实性未经独立核实", reqs,
                      "静态红线不得把动态措辞指令挤掉")


class TestDisclaimerAutoInjection(unittest.TestCase):
    """② 落盘兜底：缺"免责声明"字样时自动补标准声明（prescribed case 2）。"""

    def _tmp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_t25_ws_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def test_missing_disclaimer_is_injected_with_three_elements(self):
        reports = self._tmp()
        ReportGeneratorWorker._write_report_files(reports, _report("none"))
        out = (reports / "report.md").read_text(encoding="utf-8")
        self.assertIn("## 免责声明", out, "必须有免责声明章节")
        self.assertIn("不构成", out)
        self.assertIn("投资建议", out)
        self.assertIn("AI 生成", out, "AI 生成标识必须在注入文案里")
        self.assertIn(BASE_SENTENCE, out, "注入文案必须逐字等于基线模板")
        # 注入不得吃掉正文
        self.assertIn("## 风险与结论", out)
        self.assertIn("## 参考来源", out)

    def test_injection_lands_before_appendix(self):
        """有 `## 附录` 时插在它**之前**（声明不能被推到附录之后）。"""
        reports = self._tmp()
        ReportGeneratorWorker._write_report_files(reports, _report("none"))
        out = (reports / "report.md").read_text(encoding="utf-8")
        self.assertLess(out.index("## 免责声明"), out.index("## 附录"),
                        "免责声明必须在附录之前")

    def test_append_to_end_without_appendix(self):
        text = ReportGeneratorWorker._ensure_disclaimer(BODY)
        self.assertTrue(text.rstrip().startswith("# 某公司两期经营研究"))
        self.assertIn("## 免责声明", text)
        self.assertLess(text.index("## 免责声明"), len(text))

    def test_complete_disclaimer_not_duplicated(self):
        """prescribed case 4：已有完整免责声明 → 一个字不改、不重复注入。"""
        report = _report("full")
        self.assertEqual(ReportGeneratorWorker._ensure_disclaimer(report), report)
        reports = self._tmp()
        ReportGeneratorWorker._write_report_files(reports, report)
        out = (reports / "report.md").read_text(encoding="utf-8")
        self.assertEqual(out.count("免责声明"), report.count("免责声明"),
                         "不得重复注入免责声明章节")
        self.assertEqual(out, report)

    def test_existing_disclaimer_without_ai_is_never_overwritten(self):
        """已有免责声明但缺 AI 标识：注入侧**不动**（由验收侧弱缺口提示，
        不由系统改写用户/模型已写的声明内容）。"""
        report = _report("no_ai")
        out = ReportGeneratorWorker._ensure_disclaimer(report)
        self.assertEqual(out, report)
        self.assertIn("不构成任何投资建议", out)

    def test_injection_is_idempotent(self):
        once = ReportGeneratorWorker._ensure_disclaimer(_report("none"))
        twice = ReportGeneratorWorker._ensure_disclaimer(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count("## 免责声明"), 1)

    def test_empty_report_gets_disclaimer(self):
        """空正文也不能漏（兜底路径与主路径同规则）。"""
        out = ReportGeneratorWorker._ensure_disclaimer("")
        self.assertIn("## 免责声明", out)
        self.assertIn(BASE_SENTENCE, out)


class TestAiMarkerDetection(unittest.TestCase):
    """③ 验收侧：AI 生成标识检测（prescribed case 1 与 case 3）。"""

    def test_complete_disclaimer_passes(self):
        res = ac.check_disclaimer(_report("full"))
        self.assertTrue(res["pass"])
        self.assertTrue(res["has_disclaimer"])
        self.assertTrue(res["has_advice_note"])
        self.assertTrue(res["has_ai_marker"])
        self.assertFalse(res["ai_marker_missing"])
        self.assertEqual(res["gaps"], [])

    def test_disclaimer_without_ai_marker_fails(self):
        res = ac.check_disclaimer(_report("no_ai"))
        self.assertFalse(res["pass"], "缺 AI 生成标识 → 检查项判负")
        self.assertTrue(res["has_disclaimer"])
        self.assertTrue(res["has_advice_note"])
        self.assertFalse(res["has_ai_marker"])
        self.assertTrue(res["ai_marker_missing"])
        self.assertTrue(any("缺少 AI 生成标识" in g for g in res["gaps"]),
                        f"缺口必须点名 AI 生成标识：{res['gaps']}")

    def test_production_auto_generated_wording_counts(self):
        """生产措辞「AI 自动生成」（`report_quality.disclaimer_clause`）同样合格。"""
        clause = rq.disclaimer_clause(has_external=True, has_user_material=False)
        self.assertIn("AI 自动生成", clause)
        res = ac.check_disclaimer("## 免责声明\n\n" + clause + "\n")
        self.assertTrue(res["has_ai_marker"], res["details"])
        self.assertTrue(res["pass"])

    def test_chinese_ai_wording_counts(self):
        res = ac.check_disclaimer(
            "## 免责声明\n\n本报告由人工智能生成，不构成任何投资建议。\n")
        self.assertTrue(res["has_ai_marker"])
        self.assertTrue(res["pass"])

    def test_missing_base_elements_keeps_legacy_gap(self):
        """基础要素缺失：维持原判负路径（缺口文案含'免责声明'与'不构成投资建议'）。"""
        res = ac.check_disclaimer("# 报告\n\n只有正文，没有任何声明。\n")
        self.assertFalse(res["pass"])
        self.assertFalse(res["ai_marker_missing"])
        self.assertTrue(any("免责声明" in g for g in res["gaps"]), res["gaps"])


class TestWeakGapDoesNotBlockOverall(unittest.TestCase):
    """③ `run_acceptance`：AI 标识缺失 = 弱缺口（可见，但不翻转 overall）。

    目标含"上市公司"→ 溯源域 financial（免责声明按 `counted = applicable and
    domain == "financial"` 才计入缺口——非金融域本来就只展示不计数）。
    """

    GOAL = "研究某上市公司两期经营情况"

    def _run(self, report: str) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix="wm_t25_acc_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.dict("os.environ", {"URL_HEALTH_CHECK": "0"}):
            return ac.run_acceptance(
                "t25-acc", self.GOAL, report, str(tmp),
                profile="financial", report_type="research_brief")

    def test_ai_marker_gap_is_visible_but_not_blocking(self):
        without = self._run(_report("no_ai"))
        with_marker = self._run(_report("full"))
        self.assertEqual(without["overall"], with_marker["overall"],
                         "只缺 AI 标识不得翻转 overall")
        check = without["checks"]["disclaimer"]
        self.assertTrue(check["pass"], "降级为弱缺口：检查项不判负")
        self.assertFalse(check["counted"], "不计入 overall 缺口")
        self.assertTrue(check["ai_marker_missing"], "证据必须留下 ai_marker_missing")
        self.assertTrue(any("缺少 AI 生成标识" in g for g in without["gaps"]),
                        f"缺口必须可见：{without['gaps']}")
        self.assertFalse(any("缺少 AI 生成标识" in g for g in with_marker["gaps"]),
                         "带标识的报告不得有这条缺口")

    def test_event_does_not_count_it_as_failed_check(self):
        res = self._run(_report("no_ai"))
        event = ac.build_acceptance_event(res, trigger="测试")
        self.assertNotIn("disclaimer", event["checks_failed"],
                         "弱缺口不得计成失败项")

    def test_missing_disclaimer_entirely_still_blocks(self):
        """基础要素缺失（ag-049 真实形态）：overall 必须按缺口处理。"""
        tmp = Path(tempfile.mkdtemp(prefix="wm_t25_acc2_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bare = "# 报告\n\n## 风险与结论\n\n结论。\n"
        with mock.patch.dict("os.environ", {"URL_HEALTH_CHECK": "0"}):
            res = ac.run_acceptance("t25-bare", self.GOAL, bare, str(tmp),
                                    profile="financial",
                                    report_type="research_brief")
        self.assertFalse(res["checks"]["disclaimer"]["pass"])
        self.assertTrue(res["checks"]["disclaimer"]["counted"],
                        "financial 域免责声明缺失必须计入缺口")
        self.assertNotEqual(res["overall"], "pass",
                            "完全没有免责声明仍是真缺口")
        self.assertTrue(any("免责声明" in g for g in res["gaps"]), res["gaps"])


class TestExistingFixturesStayGreen(unittest.TestCase):
    """兼容守卫：存量报告措辞（含「AI 自动生成」）不得被新检查翻红。"""

    def test_report_quality_disclaimer_clause_passes(self):
        for has_external in (True, False):
            for has_user in (True, False):
                clause = rq.disclaimer_clause(has_external=has_external,
                                              has_user_material=has_user)
                res = ac.check_disclaimer("## 免责声明\n\n" + clause)
                self.assertTrue(res["pass"], (has_external, has_user, res["details"]))

    def test_six_hundred_source_wording_is_not_treated_as_ai_marker(self):
        """防误判：正文里出现"AI"字样不等于 AI 生成标识（必须与"生成"搭配）。"""
        res = ac.check_disclaimer(
            "## 免责声明\n\n本报告不构成任何投资建议。公司正在布局 AI 业务。\n")
        self.assertFalse(res["has_ai_marker"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
