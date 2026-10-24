"""F1（收口 20261021）：已剔除来源不得回流正文来源清单。

实机反例（任务 ui-b23642e5de，研究伊利股份 600887）：
  · 检索返回 3 条候选全是 SKF 深沟球轴承 6008 / Windows 事件日志 6008 / 汉语词典页；
  · `_filter_source_items` 的 fail-open「一条都没命中就全保留」把它们放进来源桶；
  · 报告生成器据此自动追加 `## 数据来源`，三条 URL 逐字写进交付主文；
  · 而同一份正文又声明「3 条结果均与标的无关，已整体剔除」——一份交付自相矛盾。

三分原则：检索候选 ≠ 正文采用；被判无关的进「剔除审计」，不进正文与来源清单；
没有采用依据时如实缺口，不伪造引用；合法采用来源不误删。

测试只读本地夹具与替身指令，无任何模型调用。
"""
import json
import unittest
from pathlib import Path

from orchestrator_v2 import OrchestratorV2
from workers.report_generator_worker import ReportGeneratorWorker as W


GOAL = ("研究某上市公司（600887）2023 与 2024 两个年度的营业收入、归母净利润，"
        "合并报表口径，数据截至 2025-04-30。每个数字须能回溯到来源位置并可重算；"
        "缺证据的如实标缺口。")

# 与标的毫无共同主题词的三条候选（用中性域名，避免按真实 URL 写死）
IRRELEVANT = [
    {"title": "6008 - 深沟球轴承", "url": "https://bearing.example.org/productid-6008",
     "snippet": "6008 深沟球轴承 Single row deep groove ball bearings"},
    {"title": "win10 eventlog 6008", "url": "https://answers.example.net/q/5525789",
     "snippet": "事件日志 6008 意外关机 排查"},
    {"title": "词典条目", "url": "https://dict.example.com/cidian/ci-6bb791f75",
     "snippet": "该词条的释义与例句"},
]


class TestSourcePartition(unittest.TestCase):
    """编排器侧：候选 / 采用 / 剔除 三分。"""

    def test_all_irrelevant_candidates_are_excluded_not_kept(self):
        kept, dropped = OrchestratorV2._partition_source_items(GOAL, IRRELEVANT)
        self.assertEqual(kept, [], "无关候选不得进入来源桶")
        self.assertEqual(len(dropped), 3)
        for it in dropped:
            self.assertTrue(str(it.get("_exclude_reason") or ""),
                            "每条剔除都要带理由（剔除审计可查）")

    def test_exclusion_reason_names_the_subject(self):
        _, dropped = OrchestratorV2._partition_source_items(GOAL, IRRELEVANT)
        reasons = " ".join(str(it.get("_exclude_reason")) for it in dropped)
        self.assertIn("600887", reasons)

    def test_relevant_candidate_is_kept(self):
        good = {"title": "伊利股份 2024 年年度报告",
                "url": "https://static.cninfo.com.cn/finalpage/2025-04-29/8.PDF",
                "snippet": "营业收入 归母净利润 经营活动现金流净额 600887"}
        kept, dropped = OrchestratorV2._partition_source_items(GOAL, [good] + IRRELEVANT)
        self.assertEqual([it["url"] for it in kept], [good["url"]])
        self.assertEqual(len(dropped), 3, "混在有效候选里的无关条目仍要剔除")
        self.assertTrue(all(str(it.get("_exclude_reason") or "") for it in dropped))

    def test_undated_relevant_candidate_is_pending_not_admitted(self):
        """主体/期间都对但**发布日不可核** → 待核验，不进来源清单、也不算"无关"。

        P0-C 词汇：候选 / 待核验 / 采用 / 剔除。判不准 ≠ 无关：不得把"待核验"
        写成"与本标的不含任何共同主题词"，也不得自动采用。
        """
        undated = {"title": "伊利股份 2024 年年度报告",
                   "url": "https://www.cninfo.com.cn/report.pdf",
                   "snippet": "营业收入 归母净利润 600887"}
        kept, dropped = OrchestratorV2._partition_source_items(GOAL, [undated])
        self.assertEqual(kept, [], "未核验时点前不得自动采用")
        self.assertEqual(len(dropped), 1)
        rec = dropped[0]
        self.assertEqual(str((rec.get("_admission") or {}).get("stage") or ""), "pending")
        self.assertIn("待核验", str(rec.get("_exclude_reason") or ""))
        self.assertNotIn("与本标的不含任何共同主题词",
                         str(rec.get("_exclude_reason") or ""),
                         "不得把'判不准'说成'无关'")

    def test_goal_without_strong_subject_fails_open(self):
        """无强主体词时启发式判不准：维持 fail-open，不误删来源。"""
        vague = "总结一下要点，列成条目。"
        kept, dropped = OrchestratorV2._partition_source_items(vague, IRRELEVANT)
        self.assertEqual(len(kept), 3)
        self.assertEqual(dropped, [])

    def test_empty_input(self):
        self.assertEqual(OrchestratorV2._partition_source_items(GOAL, []), ([], []))


class TestInstructionInjection(unittest.TestCase):
    """指令注入：没有可用来源时不给编号、只给缺口，并把剔除名单下发。"""

    def _instr(self, urls, excluded):
        # 复刻编排器注入的契约（不启动真编排器，只验文本契约）
        base = "任务目标：" + GOAL
        excl = {u for u in excluded}
        urls = [u for u in urls if u not in excl]
        if urls:
            base += ("\n\n[数据来源]\n以下为本次任务**通过相关性判定、可被正文引用**的"
                     "来源清单，编号即引用编号：\n"
                     + "\n".join(f"[{i+1}] {u}" for i, u in enumerate(urls[:12])))
        elif excl:
            base += ("\n\n[数据来源]\n本次检索到的候选来源均与标的无关、已按来源纪律"
                     "整体剔除，**没有可引用的外部来源**。")
        if excl:
            base += ("\n\n[已剔除来源]\n以下 URL 不得出现在正文与来源清单：\n"
                     + "\n".join(f"- {u}" for u in sorted(excl)))
        return base

    def test_excluded_urls_never_reach_auto_source_appendix(self):
        instr = self._instr([it["url"] for it in IRRELEVANT],
                            [it["url"] for it in IRRELEVANT])
        excluded = W._excluded_from_instruction(instr)
        self.assertEqual(len(excluded), 3)
        body = "# 某公司研究报告\n\n## 结论\n\n正文。\n"
        cleaned, n = W._drop_excluded_urls(body, excluded)
        self.assertEqual(n, 0)                       # 正文里本来就没有，不该误删
        for it in IRRELEVANT:
            self.assertNotIn(it["url"], cleaned)

    def test_excluded_entries_are_stripped_from_appendix(self):
        report = ("# 报告\n\n## 结论\n\n正文。\n\n## 数据来源\n\n"
                  + "\n".join(f"- [{it['url']}]({it['url']})" for it in IRRELEVANT)
                  + "\n- [https://www.cninfo.com.cn/ok.pdf](https://www.cninfo.com.cn/ok.pdf)\n")
        excluded = {it["url"] for it in IRRELEVANT}
        cleaned, n = W._drop_excluded_urls(report, excluded)
        self.assertEqual(n, 3)
        for it in IRRELEVANT:
            self.assertNotIn(it["url"], cleaned)
        self.assertIn("cninfo.com.cn/ok.pdf", cleaned, "合法采用来源不得误删")


    def test_prose_urls_are_unlinked_not_deleted(self):
        """正文散文里的被剔除 URL 也要清（验收口径：正文中无关 URL 为零），
        但**不删整句**——markdown 链接退化成文字，裸 URL 换成可核查说明。"""
        url = IRRELEVANT[0]["url"]
        report = ("# 报告\n\n## 结论\n\n"
                  f"参考了 [轴承规格页]({url}) 的说法，另见 {url} 。\n\n"
                  "## 数据来源\n\n"
                  f"- [{url}]({url})\n")
        cleaned, n = W._drop_excluded_urls(report, {url})
        self.assertNotIn(url, cleaned, "正文与清单里都不该再有该 URL")
        self.assertIn("轴承规格页", cleaned, "链接文字要保留，不整句删")
        self.assertIn("已按来源纪律剔除", cleaned)
        self.assertIn("参考了", cleaned)
        self.assertGreaterEqual(n, 2)          # 散文行 + 清单行各一次

    def test_legal_sources_survive_when_nothing_excluded(self):
        report = "# 报告\n\n## 数据来源\n\n- [https://a.example.com/1](https://a.example.com/1)\n"
        cleaned, n = W._drop_excluded_urls(report, set())
        self.assertEqual((cleaned, n), (report, 0))

    def test_instruction_without_exclusion_block_is_noop(self):
        self.assertEqual(W._excluded_from_instruction("任务目标：普通任务"), set())


class TestAutoAppendixContract(unittest.TestCase):
    """自动来源附录：**直接调用生产实现**（不放测试里重写推导/硬编码句子）。

    `ReportGeneratorWorker.compose_source_appendix()` 与
    `finalize_source_hygiene()` 就是 `execute()` 成稿收尾调用的同一份代码，
    所以这里验的是生产行为，而不是"测试自己算了一遍"。
    """

    def _instr(self, urls, excluded):
        from orchestrator_v2 import source_injection_text
        return "任务目标：" + GOAL + source_injection_text(
            list(urls), [{"url": u, "reason": "与本标的不含任何共同主题词"}
                         for u in excluded])

    def test_mixed_adopted_and_excluded_keeps_legal_only(self):
        """采用 + 剔除混合：正常 URL 保留，剔除 URL 不回流。"""
        good = "https://www.cninfo.com.cn/ok.pdf"
        bad = [it["url"] for it in IRRELEVANT]
        instr = self._instr([good] + bad, bad)

        appendix = W.compose_source_appendix(instr)
        self.assertIn(good, appendix, "合法采用来源必须保留")
        for u in bad:
            self.assertNotIn(u, appendix, f"剔除 URL 不得进入自动附录：{u}")
        # 附录里不含任何"已剔除"指引（有可用来源时不该提剔除）
        self.assertNotIn("无采用的外部来源", appendix)

    def test_all_excluded_yields_honest_gap_and_no_false_pointer(self):
        """全部剔除：附录如实写缺口，且**不指向未实现的入口**。"""
        bad = [it["url"] for it in IRRELEVANT]
        instr = self._instr(bad, bad)

        appendix = W.compose_source_appendix(instr)
        self.assertIn("本次无采用的外部来源", appendix)
        for u in bad:
            self.assertNotIn(u, appendix)
        # 真实工件指路：只允许指向交付包里真实存在的 audit/source_exclusions.json
        self.assertIn("audit/source_exclusions.json", appendix)
        self.assertNotIn("任务详情", appendix, "不得指向未实现的任务详情入口")
        self.assertNotIn("包来源审计", appendix)

    def test_finalize_path_removes_excluded_from_full_report(self):
        """成稿收尾（生产同一函数）：正文散文 + 附录里的剔除 URL 都被摘掉。"""
        bad = [it["url"] for it in IRRELEVANT]
        good = "https://www.cninfo.com.cn/ok.pdf"
        instr = self._instr([good] + bad, bad)
        report = ("# 报告\n\n## 结论\n\n"
                  f"参考了 [轴承规格页]({bad[0]}) 的说法。\n\n"
                  "## 数据来源\n\n"
                  + "\n".join(f"- [{u}]({u})" for u in bad + [good]) + "\n")
        cleaned, n = W.finalize_source_hygiene(report, instr)
        for u in bad:
            self.assertNotIn(u, cleaned, f"剔除 URL 仍残留：{u}")
        self.assertIn(good, cleaned, "合法来源不得被误删")
        self.assertIn("轴承规格页", cleaned, "散文只摘 URL，不删句子")
        self.assertGreaterEqual(n, 2)

    def test_no_sources_no_exclusion_appends_nothing(self):
        self.assertEqual(W.compose_source_appendix("任务目标：普通任务"), "")


class TestExclusionAuditArtifact(unittest.TestCase):
    """剔除审计必须落成**真实可核验记录**，并被打包路径纳入。"""

    def test_workspace_writer_creates_real_file(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from orchestrator_v2 import OrchestratorV2

        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "project"
            with patch.object(ov_mod := __import__("orchestrator_v2"),
                              "task_project_dir", lambda tid: proj):
                orch = OrchestratorV2.__new__(OrchestratorV2)
                orch._task_excluded_sources = {
                    "t-audit": [{"url": IRRELEVANT[0]["url"],
                                 "reason": "与本标的不含任何共同主题词"}],
                }
                orch._now_iso = lambda: "2026-10-21T23:59:00+08:00"
                out = orch._write_source_exclusions_file("t-audit")
            self.assertTrue(out, "必须真的写出文件路径")
            data = json.loads((proj / "source_exclusions.json").read_text(encoding="utf-8"))
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["excluded"][0]["url"], IRRELEVANT[0]["url"])
            self.assertTrue(data["excluded"][0]["reason"])
            self.assertIn("候选 ≠ 正文采用", data["note"])

    def test_package_members_include_the_audit_when_present(self):
        import json
        import tempfile
        from pathlib import Path
        import delivery_pipeline as dp

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            self.assertEqual(dp._exclusion_audit_members(ws), [],
                             "没有记录时不进包")
            (ws / "project").mkdir(parents=True, exist_ok=True)
            (ws / "project" / "source_exclusions.json").write_text(
                json.dumps({"count": 0}, ensure_ascii=False), encoding="utf-8")
            members = dp._exclusion_audit_members(ws)
            self.assertEqual([arc for _p, arc in members],
                             ["audit/source_exclusions.json"])

    def test_pending_ledger_is_split_from_rejections_and_packaged(self):
        """P0-C：pending / rejected 分两本账，两份都进交付包 audit/。"""
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import delivery_pipeline as dp
        from orchestrator_v2 import OrchestratorV2

        rejected = dict(IRRELEVANT[0], _admission={"stage": "rejected"})
        pending = dict(IRRELEVANT[1],
                       _exclude_reason="待核验：主体/期间/时点证据不足",
                       _admission={"stage": "pending"})
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "project"
            with patch.object(__import__("orchestrator_v2"),
                              "task_project_dir", lambda tid: proj):
                orch = OrchestratorV2.__new__(OrchestratorV2)
                orch._task_sources_lock = __import__("threading").Lock()
                orch._now_iso = lambda: "2026-10-22T12:00:00+08:00"
                orch._record_source_exclusion("t-split", [rejected, pending])
                out_r = orch._write_source_exclusions_file("t-split")
                out_p = orch._write_source_pending_file("t-split")
            self.assertTrue(out_r and out_p)
            rej = json.loads((proj / "source_exclusions.json")
                             .read_text(encoding="utf-8"))
            pend = json.loads((proj / "source_pending.json")
                              .read_text(encoding="utf-8"))
            self.assertEqual([e["url"] for e in rej["excluded"]],
                             [IRRELEVANT[0]["url"]], "rejected 只进剔除账")
            self.assertEqual([e["url"] for e in pend["pending"]],
                             [IRRELEVANT[1]["url"]], "pending 只进待核验账")
            self.assertNotIn("待核验", rej["excluded"][0]["reason"])
            ws = Path(td)
            members = dp._exclusion_audit_members(ws)
            self.assertEqual(sorted(arc for _p, arc in members),
                             ["audit/source_exclusions.json",
                              "audit/source_pending.json"],
                             "交付包 audit/ 要同时装下两本账")

    def test_packaging_call_site_is_wired(self):
        """回归守卫：打包路径必须真的调用纳入函数（防有人删掉那一行）。"""
        import inspect
        import delivery_pipeline as dp
        src = inspect.getsource(dp.repack_adopted)
        self.assertIn("_exclusion_audit_members(ws)", src)


class TestInjectionContractEndToEnd(unittest.TestCase):
    """编排器**真实**注入文本 ↔ 报告生成器**真实**解析器：契约端到端。"""

    def test_worker_parser_reads_orchestrator_block(self):
        from orchestrator_v2 import source_injection_text
        excluded = [{"url": it["url"], "reason": "与本标的不含任何共同主题词"}
                    for it in IRRELEVANT]
        instr = source_injection_text([], excluded)
        # 编排器不给编号、不给 URL 清单
        self.assertIn("没有可引用的外部来源", instr)
        self.assertNotIn("[1]", instr)
        # 工人能逐条解析出剔除名单
        parsed = W._excluded_from_instruction(instr)
        self.assertEqual(parsed, {it["url"] for it in IRRELEVANT})

    def test_adopted_sources_still_get_numbered_list(self):
        from orchestrator_v2 import source_injection_text
        good = "https://www.cninfo.com.cn/ok.pdf"
        instr = source_injection_text([good], [])
        self.assertIn("[1] " + good, instr)
        self.assertIn("通过相关性判定", instr)
        self.assertEqual(W._excluded_from_instruction(instr), set())

    def test_no_sources_no_exclusions_injects_nothing(self):
        from orchestrator_v2 import source_injection_text
        self.assertEqual(source_injection_text([], []), "")


if __name__ == "__main__":
    unittest.main()
