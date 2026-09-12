# -*- coding: utf-8 -*-
"""验收器对抗测试集（adversarial suite）。

与既有"已知误报回归"测试不同，本文件的用例**主动构造逼真但造假的报告**，
检验确定性验收器能否识破；同时配"诚实报告必须 pass"的反例，防止规则收紧
时误伤（两类方向都覆盖，规则改动跑一遍即可回归）。

另含：
- 规则版本指纹守卫（规则变更必须 bump 版本并更新基线）；
- 验收事件流的追加/回放（可对账字段）。
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import workspace as ws_mod


def _safe_project_dir(root: Path, task_id: str) -> Path:
    """测试辅助：task_id → root 下的单层目录（规范化 + 边界校验）。

    拒绝路径分隔符与 ..；解析后必须仍位于 root 内，否则拒绝。"""
    tid = str(task_id).replace("\\", "/").split("/")[-1].strip()
    if not tid or tid in (".", ".."):
        raise ValueError("invalid task_id")
    root_abs = root.resolve()
    proj = (root_abs / tid).resolve()
    if proj != root_abs and not proj.is_relative_to(root_abs):  # 边界校验
        raise ValueError("task dir escapes workspace root")
    return proj


def _mk_env(task_id: str, sources: list[dict] | None = None,
            extra_files: dict | None = None) -> str:
    """建临时任务工作区：来源写进 <ws>/project/（_collect_sources 的读取位置）。"""
    tmp = Path(tempfile.mkdtemp(prefix="wm_adv_"))
    proj = _safe_project_dir(tmp, task_id)
    (proj / "project").mkdir(parents=True, exist_ok=True)
    base = proj / "project"
    if sources is not None:
        (base / "search_results.json").write_text(
            json.dumps(sources, ensure_ascii=False), encoding="utf-8")
    for name, content in (extra_files or {}).items():
        fname = Path(str(name)).name  # 仅取单层文件名，丢弃任何目录成分
        if not fname or fname in (".", ".."):
            continue
        (base / fname).write_text(content, encoding="utf-8")
    return str(tmp)


# 一份真实媒体来源（中研网），供"伪造他媒体名"用例做对照
_SRC_ZHONGYAN = [{
    "title": "2026休闲食品行业现状调研_中研网",
    "url": "https://www.chinairn.com/hyzx/x.shtml",
    "snippet": "",
}]
# 一份真实财经来源（新浪财经），供诚实反例做数字溯源
_SRC_SINA = [{
    "title": "贵州茅台2024年报：营收1741亿元_新浪财经",
    "url": "https://finance.sina.com.cn/a/1",
    "snippet": "贵州茅台2024年营收1741亿元，净利润862亿元。",
}]


_DISCLAIMER = (
    "## 免责声明\n\n本报告由织光 WeaveMind AI 自动生成，仅供参考，"
    "不构成任何投资建议；数据来源于公开渠道，可能存在延迟或误差；"
    "据此操作风险自担。\n"
)


class _AdversarialBase(unittest.TestCase):
    """公共夹具：临时工作区 + 恢复全局 WORKSPACE_ROOT。"""

    def setUp(self):
        self._old_root = ws_mod.WORKSPACE_ROOT

    def tearDown(self):
        ws_mod.WORKSPACE_ROOT = self._old_root

    def _run(self, task_id, goal, report, sources):
        from acceptance_checker import run_acceptance
        tmp = _mk_env(task_id, sources)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ws_mod.configure_workspace_root(tmp)
        ws = ws_mod.task_workspace(task_id)
        return run_acceptance(task_id, goal, report, ws)


class TestAcceptanceAdversarial(_AdversarialBase):
    """对抗：造假报告必须被拦住。"""

    def test_fabricated_media_name_is_caught(self):
        """伪造媒体名：源里只有中研网，报告却声明"高工锂电"→ 判虚假标注。"""
        r = self._run(
            "adv-media", "固态电池行业调研",
            "2025年出货量 100GWh（数据来源：高工锂电）。\n\n" + _DISCLAIMER,
            _SRC_ZHONGYAN,
        )
        sl = r["checks"]["source_labeling"]
        self.assertFalse(sl["pass"], sl["details"])
        self.assertTrue(any("高工锂电" in m for m in sl.get("mislabeled") or []))

    def test_fabricated_authority_doc_is_caught(self):
        """伪造权威文档：声明"腾讯官方年报"但检索无年报类文档 → 判虚假标注。"""
        r = self._run(
            "adv-doc", "分析腾讯2023年财报",
            "2023年净利润 1152 亿元（数据来源：腾讯官方年报）。\n\n" + _DISCLAIMER,
            _SRC_ZHONGYAN,
        )
        self.assertFalse(r["checks"]["source_labeling"]["pass"])
        self.assertEqual(r["overall"], "fail")

    def test_placeholder_disguised_as_disclosure_fails_financial(self):
        """占位伪装：financial 域把"未披露/未获取/待补充"包装成诚实披露，
        仍须判交付物不完整（数字域不允许以占位充数）。"""
        report = (
            "# 贵州茅台2025年报分析\n\n核心指标：营收未披露，净利润未获取，"
            "毛利率待补充。\n\n数据来源：基于模型知识，未在本次检索中验证。\n\n"
            + _DISCLAIMER
        )
        r = self._run("adv-ph", "分析贵州茅台2025年报营收与净利润", report, _SRC_SINA)
        comp = r["checks"]["deliverable_completeness"]
        self.assertFalse(comp["pass"], comp["details"])
        self.assertGreaterEqual(comp["placeholder_count"], 3)
        self.assertEqual(r["overall"], "fail")

    def test_source_list_vs_inline_refs_mismatch_fails(self):
        """来源清单与正文引用不一致（financial 域，清单完整性计入 gaps）：
        正文引用 [1][2][3]，清单只有 1 条 → 判 fail。"""
        report = (
            "# 贵州茅台2024年报分析\n\n营业收入[1]，净利润[2]，毛利率[3]。\n\n"
            "## 数据时效\n\n数据截至 2024-12-31 年报，日终更新。\n\n"
            "## 参考来源\n\n1. [中研网](https://www.chinairn.com/a)\n\n" + _DISCLAIMER
        )
        r = self._run("adv-list", "分析贵州茅台2024年报营收与净利润", report, _SRC_ZHONGYAN)
        slc = r["checks"]["source_list_completeness"]
        self.assertFalse(slc["pass"], slc["details"])
        self.assertEqual(r["overall"], "fail")

    def test_combined_evasion_still_fails(self):
        """组合规避：真实媒体名 + 诚实占位词 + 缺时效块/免责声明 → 仍须 fail。"""
        report = (
            "# 贵州茅台2025年报分析\n\n营收未披露；净利润未获取；毛利率待补充。\n\n"
            "部分数据来自中研网公开资料。\n"
        )
        r = self._run("adv-combo", "分析贵州茅台2025年报营收", report, _SRC_ZHONGYAN)
        self.assertEqual(r["overall"], "fail")
        self.assertGreaterEqual(len(r.get("gaps") or []), 2)

    def test_known_gap_number_subject_mismatch(self):
        """已知盲区（记录现状，非通过标准）：数字溯源只比对数值、不绑定主体——
        报告写"宁德时代营收1741亿元"而来源是"比亚迪营收1741亿元"时，
        当前判为可溯源。将来实现"数字-主体绑定"校验后，本用例应改为
        断言 number_traceability 判 fail。"""
        report = (
            "# 宁德时代2024年报\n\n## 核心指标\n\n| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 营业收入 | 1741亿元 | [1] |\n\n## 数据时效\n\n数据截至 2024-12-31 年报，日终更新。\n\n"
            "## 参考来源\n\n1. [比亚迪2024年营收1741亿元](https://finance.sina.com.cn/b/2)\n\n"
            + _DISCLAIMER
        )
        r = self._run(
            "adv-gap", "分析宁德时代2024年报营收", report, [{
                "title": "比亚迪2024年营收1741亿元_新浪财经",
                "url": "https://finance.sina.com.cn/b/2",
                "snippet": "比亚迪2024年营收1741亿元。",
            }],
        )
        # 记录现状：值命中即算可溯源（主体未绑定）
        self.assertTrue(r["checks"]["number_traceability"]["pass"])


class TestAcceptanceHonestBaseline(_AdversarialBase):
    """反例防误伤：合规报告必须通过（规则收紧时的回归网）。"""

    def test_honest_financial_report_passes(self):
        """诚实财报报告（数字可溯源 + 编号引用 + 清单 + 时效 + 免责）→ pass。"""
        report = (
            "# 贵州茅台2024年报核心财务数据\n\n## 核心指标\n\n"
            "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
            "| 营业收入 | 1741亿元 | [1] |\n| 净利润 | 862亿元 | [1] |\n\n"
            "## 数据时效\n\n数据截至 2024-12-31 年度报告披露日，来源为公开财经报道，日终更新。\n\n"
            "## 参考来源\n\n1. [贵州茅台2024年报：营收1741亿元](https://finance.sina.com.cn/a/1)\n\n"
            + _DISCLAIMER
        )
        r = self._run("honest-fin", "贵州茅台2024年报核心财务数据", report, _SRC_SINA)
        self.assertEqual(r["overall"], "pass", r.get("gaps"))

    def test_generic_public_source_phrasing_not_flagged(self):
        """"来源为公开财经报道/来源于公开渠道"是泛化诚实表述，不得判虚假标注
        （历史误报：引导字"为"混入声明主体）。"""
        from acceptance_checker import check_source_labeling
        sources = {"search_results": "title: 行业数据 https://www.chinairn.com/a"}
        for text in ("来源为公开财经报道。", "数据来源于公开渠道。",
                     "来源为公开市场数据。"):
            r = check_source_labeling(text, sources)
            self.assertTrue(r["pass"], f"{text} → {r['details']}")


class TestAcceptanceRulesVersioning(unittest.TestCase):
    """规则版本化与验收事件流。"""

    # 规则指纹基线：任何规则表/阈值/正则/分档表变更都会改变指纹，
    # 本断言随之失败 —— 强制"改规则 → bump ACCEPTANCE_RULES_VERSION
    # → 更新此基线"的流程，保证历史结果可反查判定规则版本。
    _FINGERPRINT_BASELINE = "2ddecc9a"

    def test_acceptance_rules_fingerprint_stable(self):
        import acceptance_checker as ac
        self.assertTrue(ac.ACCEPTANCE_RULES_VERSION)
        self.assertEqual(ac.rules_fingerprint(), self._FINGERPRINT_BASELINE,
                         "规则已变更：请 bump ACCEPTANCE_RULES_VERSION 并更新指纹基线")

    def test_run_acceptance_includes_rule_metadata(self):
        from acceptance_checker import run_acceptance
        tmp = _mk_env("ver-1", _SRC_SINA)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws = ws_mod.task_workspace("ver-1")
            r = run_acceptance("ver-1", "分析贵州茅台年报", "营收 1741 亿元。", ws)
            import acceptance_checker as ac
            self.assertEqual(r["rules_version"], ac.ACCEPTANCE_RULES_VERSION)
            self.assertEqual(r["rules_fingerprint"], ac.rules_fingerprint())
            self.assertEqual(len(r["report_sha256"]), 16)
            self.assertTrue(r["evaluated_at"])
            self.assertTrue(r["profile"], "验收结果应带档位（profile）")
            # 报告文本变化 → 指纹变化（对账可区分两次验收）
            r2 = run_acceptance("ver-1", "分析贵州茅台年报", "营收 1741 亿元。新增一句。", ws)
            self.assertNotEqual(r["report_sha256"], r2["report_sha256"])
        finally:
            ws_mod.WORKSPACE_ROOT = old

    def test_acceptance_events_append_and_replay(self):
        from acceptance_checker import (
            append_acceptance_event, build_acceptance_event, read_acceptance_events,
        )
        tmp = _mk_env("evt-1", _SRC_SINA)
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        old = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            result = {
                "report_id": "evt-1", "overall": "fail",
                "gaps": ["数字溯源率过低"], "checks": {
                    "number_traceability": {"pass": False},
                    "url_health": {"dead_count": 2},
                },
                "rules_version": "2026.09.10", "rules_fingerprint": "abcdef12",
                "report_sha256": "0123456789abcdef",
            }
            append_acceptance_event("evt-1", build_acceptance_event(
                result, trigger="报告步骤", iteration=0, duration_ms=42))
            append_acceptance_event("evt-1", build_acceptance_event(
                dict(result, overall="pass", gaps=[]), trigger="反思重做",
                iteration=1, duration_ms=17))
            events = read_acceptance_events("evt-1")
            self.assertEqual(len(events), 2)
            self.assertEqual([e["seq"] for e in events], [1, 2])
            self.assertEqual([e["trigger"] for e in events], ["报告步骤", "反思重做"])
            self.assertEqual(events[0]["checks_failed"], ["number_traceability"])
            self.assertEqual(events[0]["url_health_dead"], 2)
            self.assertEqual(events[0]["rules_version"], "2026.09.10")
            self.assertEqual(events[1]["overall"], "pass")
        finally:
            ws_mod.WORKSPACE_ROOT = old


class TestAcceptanceProfileGating(_AdversarialBase):
    """验收按任务类型分档：报告格式类检查对代码/数据任务不适用。

    回归：代码任务的验收报告里 source_list_completeness=false，读起来像交付缺陷
    （"写个脚本却没来源清单"）；分档后该类检查标记 N/A 并说明原因。
    """

    CODE_GOAL = ("用 Python 编写单文件脚本 sales_report.py：内置近12个月的月度销售数据，"
                 "计算合计/均值/最大值并打印 ASCII 柱状图")
    RESEARCH_GOAL = "调研2026年国内固态电池产业化进展：主要厂商量产时间节点"

    def test_profile_resolution(self):
        from acceptance_checker import resolve_profile
        self.assertEqual(resolve_profile(self.CODE_GOAL), "code")
        self.assertEqual(resolve_profile(
            "梳理贵州茅台2025年三季报核心财务数据（营收/净利润/毛利率/现金流）"), "financial")
        self.assertEqual(resolve_profile(self.RESEARCH_GOAL), "research")
        self.assertEqual(resolve_profile("读取 csv 数据集做 EDA 并训练回归模型"), "data")
        # 步骤能力优先于目标线索
        self.assertEqual(resolve_profile(
            "分析茅台财报", capabilities=["code_execution", "package"]), "code")

    def test_code_task_report_checks_marked_not_applicable(self):
        r = self._run("prof-code", self.CODE_GOAL,
                      "# 交付结果\n\n脚本已生成并运行通过。\n", [])
        self.assertEqual(r["profile"], "code")
        for key in ("source_list_completeness", "freshness_block", "disclaimer"):
            check = r["checks"][key]
            self.assertFalse(check.get("applicable"), f"{key} 对代码任务应不适用")
            self.assertFalse(check.get("counted"))
            self.assertTrue(check["pass"], "N/A 检查不得计为失败")
            self.assertIn("不适用于", str(check.get("details")))
        self.assertEqual(r["overall"], "pass")

    def test_financial_profile_still_counts_report_checks(self):
        """financial 档不受分档影响：来源清单缺失仍计入缺口。"""
        report = "# 贵州茅台2025年三季报分析\n\n营收 1741 亿元，净利润 823 亿元。\n"
        r = self._run("prof-fin", "梳理贵州茅台2025年三季报核心财务数据", report, [])
        self.assertEqual(r["profile"], "financial")
        for key in ("source_list_completeness", "freshness_block", "disclaimer"):
            self.assertTrue(r["checks"][key].get("applicable"))
            self.assertTrue(r["checks"][key].get("counted"))

    def test_research_profile_not_counted_but_applicable(self):
        """research 档沿用既有宽容语义：检查适用但不计入缺口（不改变既有判定）。"""
        report = "# 固态电池产业化进展调研\n\n2026年多家厂商进入中试阶段。\n"
        r = self._run("prof-res", self.RESEARCH_GOAL, report, [])
        self.assertEqual(r["profile"], "research")
        for key in ("source_list_completeness", "freshness_block", "disclaimer"):
            check = r["checks"][key]
            self.assertTrue(check.get("applicable"))
            self.assertFalse(check.get("counted"))

    def test_event_carries_profile(self):
        from acceptance_checker import build_acceptance_event
        r = self._run("prof-evt", self.CODE_GOAL, "# 交付\n脚本已生成。\n", [])
        event = build_acceptance_event(r, trigger="报告步骤")
        self.assertEqual(event["profile"], "code")


if __name__ == "__main__":
    unittest.main()
