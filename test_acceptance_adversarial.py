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
        """**仍存在的缺口（记录现状，非通过标准）**：报告只在标题/文档级声明主体、
        数字本身所在单元没有局部归属时，主体仍不参与绑定。

        报告写"宁德时代2024年报"、表格单元只有"营业收入 | 1741亿元"，来源其实是
        "比亚迪营收1741亿元"——当前仍判可溯源。已绑定的部分见下一个用例
        （数字紧邻的子句里写明他司主体、且来源行主体不同 → 拒绝）。
        """
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
        # 记录现状：数字所在的表格单元没有局部主体声明 → 主体维度未参与绑定
        self.assertTrue(r["checks"]["number_traceability"]["pass"])
        # 阈值未被本批下调
        from acceptance_checker import _TRACEABILITY_THRESHOLDS
        self.assertEqual(_TRACEABILITY_THRESHOLDS.get("financial"), 0.7)

    def test_clause_subject_conflict_is_rejected(self):
        """已绑定部分：数字**紧邻的子句**里写明他司主体、来源行主体不同 → 不可溯源。"""
        from acceptance_checker import check_number_traceability
        report = (
            "# 行业对比\n\n比亚迪2024年营收 1741 亿元。\n\n"
            "## 数据时效\n\n数据截至 2024-12-31。\n\n" + _DISCLAIMER
        )
        tmp = _mk_env("adv-clause", [{
            "title": "宁德时代2024年营收1741亿元_新浪财经",
            "url": "https://finance.sina.com.cn/c/3",
            "snippet": "宁德时代2024年营收1741亿元。",
        }])
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ws_mod.configure_workspace_root(tmp)
        src = acceptance_checker_sources(ws_mod.task_workspace("adv-clause"))
        res = check_number_traceability(report, src, domain="financial",
                                        goal="分析宁德时代2024年报营收")
        raws = [str(t.get("raw")) for t in (res.get("traceable") or [])]
        self.assertNotIn("1741 亿元", raws,
                         f"子句主体与来源主体冲突不得算可溯源：{res.get('details')}")


def acceptance_checker_sources(ws):
    import acceptance_checker as ac
    return ac._collect_sources(ws)


class TestAcceptanceBypassNarrowed(_AdversarialBase):
    """09-22 复核冻结反例：两处"新增豁免"必须收窄（原检查不得被绕过）。

    反例来自架构复核表（`docs/阶段D实机复核与下一批指令_20260922.md` P1）：
    1) 同值不得跨主体提升溯源——来源只有宁德时代营收100亿元，正文另称比亚迪、
       腾讯各100亿元，后两条必须仍判冲突（溯源 1/3，整体 fail）；
    2) 来源声明尾部追加"需核查"不能豁免已经声称的来源——"数据来源：X，需核查…"
       仍要检查 X，同时真正的"需核查……才能判断"句不得被误判。
    """

    _SRC_ONE = [{
        "title": "宁德时代2024年营业收入100亿元_新浪财经",
        "url": "https://finance.sina.com.cn/c/3",
        "snippet": "宁德时代2024年营业收入100亿元。",
    }]

    def test_same_value_is_not_promoted_across_subjects(self):
        report = (
            "# 同业营收对比\n\n## 营收规模\n\n"
            "宁德时代2024年营业收入为100亿元。比亚迪2024年营业收入为100亿元。"
            "腾讯2024年营业收入为100亿元。\n\n"
            "## 数据时效\n\n数据截至 2024-12-31 年度报告披露日，日终更新。\n\n"
            "## 参考来源\n\n1. [宁德时代2024年年度报告](https://finance.sina.com.cn/c/3)\n\n"
            + _DISCLAIMER
        )
        r = self._run("adv-promote", "分析宁德时代2024年报营收", report, self._SRC_ONE)
        tr = r["checks"]["number_traceability"]
        self.assertEqual(r["overall"], "fail")
        self.assertFalse(tr["pass"])
        # 只有"宁德时代"那一条可溯源（同值不因数值相等而提升到另外两家）
        self.assertEqual(len(tr.get("traceable") or []), 1)
        self.assertEqual(len(tr.get("untraceable") or []), 2)
        self.assertIn("33%", str(tr.get("details")), tr.get("details"))

    def test_check_suggestion_suffix_does_not_exempt_source_claim(self):
        """来源声明本身仍被检查：追加"需核查"（逗号句内、换行无句号两种写法）。"""
        from acceptance_checker import _extract_source_claims
        for tail in ("，需核查现金流明细。", "\n需核查现金流明细"):
            body = ("# 现金流结构\n\n## 结论\n\n经营活动现金流以销售回款为主。\n\n"
                    f"数据来源：洋河股份2024年年报{tail}\n\n"
                    "## 数据时效\n\n数据截至 2024-12-31。\n\n" + _DISCLAIMER)
            r = self._run("adv-srctail", "分析洋河股份2024年报现金流", body, None)
            self.assertEqual(_extract_source_claims(body), ["洋河股份2024年年报"])
            self.assertFalse(r["checks"]["source_labeling"]["pass"],
                             f"尾加核查建议不得豁免来源声明（tail={tail!r}）")

    def test_genuine_need_check_phrasing_is_not_flagged(self):
        """正例防误伤：真正的"需核查……才能判断"是判断句，不是来源声明。"""
        from acceptance_checker import _extract_source_claims
        body = ("# 现金流结构\n\n## 结论\n\n现金流结构需结合附注才能判断，"
                "来源是以销售回款为主还是以其他项目为主。\n\n"
                "## 数据时效\n\n数据截至 2024-12-31。\n\n" + _DISCLAIMER)
        self.assertEqual(_extract_source_claims(body), [])
        r = self._run("adv-needcheck", "分析洋河股份2024年报现金流", body, [{
            "title": "洋河股份2024年报_新浪财经",
            "url": "https://finance.sina.com.cn/d/4",
            "snippet": "洋河股份2024年报。",
        }])
        self.assertTrue(r["checks"]["source_labeling"]["pass"],
                        f"判断句被误判为来源声明：{r['checks']['source_labeling']}")


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
            # R0.2：`report_sha256` 给全量（身份可证明），短 hash 单列一个字段
            self.assertEqual(len(r["report_sha256"]), 64)
            self.assertEqual(len(r["report_sha256_short"]), 16)
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


    def test_code_task_traceability_not_applicable_for_local_data(self):
        """代码任务报告里的数字来自程序输出 → 不按"可溯源"判定。

        回归：销售脚本任务交付代码可运行、无污染，但报告里的样本汇总数字被判
        "数字溯源率 0%" 而 overall=fail。"""
        report = ("# 交付结果\n\n脚本已生成并运行：合计 1788.50 万元、"
                  "均值 149.04 万元、最大值 183.70 万元。\n")
        r = self._run("prof-nt", self.CODE_GOAL, report, [])
        self.assertEqual(r["profile"], "code")
        c = r["checks"]["number_traceability"]
        self.assertFalse(c.get("applicable"))
        self.assertFalse(c.get("counted"))
        self.assertTrue(c["pass"])
        self.assertIn("程序输出", str(c.get("details")))
        self.assertEqual(r["overall"], "pass")

    def test_code_task_traceability_applies_when_goal_needs_market_data(self):
        """目标本身要外部行情数据时，代码任务仍按溯源判定。"""
        report = "# 交付结果\n\nA股前5%成交额占比 43.21%，合计 1.2 万亿元。\n"
        r = self._run("prof-nt2", "用 Python 统计今日A股成交额排行前十并计算占比",
                      report, [])
        self.assertTrue(r["checks"]["number_traceability"].get("applicable"))
        self.assertEqual(r["overall"], "fail", "无来源的行情数字仍应判缺口")

    def test_financial_traceability_marked_applicable(self):
        report = "# 贵州茅台2025年三季报分析\n\n营收 1741 亿元，净利润 823 亿元。\n"
        r = self._run("prof-nt3", "梳理贵州茅台2025年三季报核心财务数据", report, [])
        self.assertTrue(r["checks"]["number_traceability"].get("applicable"))


if __name__ == "__main__":
    unittest.main()
