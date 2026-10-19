# -*- coding: utf-8 -*-
"""任务24（🔴 合规线）验收：死链来源必须在**报告里**标出来。

根因：`orchestrator_v2._dead_fetch_urls` 是纯内存集合——任务终态/编排器重启后
即丢失，而 `## 参考来源` 里死链与活链混在一起，复核的人看不出哪些链接其实
抓不到。四条 prescribed case ＋ 编排器落盘接线：
  ① 报告步骤派发前，编排器把本轮死链（含原因）写进工作区 `dead_sources.json`
     （每次整体覆写，含空数组——重跑不带上一轮的陈旧死链）；
  ② 报告生成器读同一份文件，在 `## 参考来源`（及来源清单/参考资料）命中行尾
     追加「⚠️ 链接已失效」；没有该章节就什么都不做，绝不新建章节；
  ③ 验收把失效清单并入证据：`url_health.dead_source_count` >0 → **弱缺口**
     （不翻转 overall，但必须可见——`build_acceptance_event` 的 checks_failed
     也不把它计成失败项）；
  ④ `sources_fingerprint`（PACKAGE_MANIFEST 同源）计算前剔除死链 URL——
     标注动作本身不得改变版本身份（否则标注前/标注后指纹漂移）。
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import acceptance_checker as ac  # noqa: E402
import delivery_pipeline as dp  # noqa: E402
import orchestrator_v2 as ov  # noqa: E402
from workers.report_generator_worker import ReportGeneratorWorker  # noqa: E402

DEAD_A = "https://static.sse.com.cn/dead-annual-2024.pdf"
DEAD_B = "https://www.cninfo.com.cn/dead-summary-2024.pdf"
LIVE = "https://data.eastmoney.com/live-indicator.html"
MARK = ac.DEAD_SOURCE_MARK  # "⚠️ 链接已失效"

# 与格式要求同构的样本：免责声明 + 参考来源（2 死 1 活）+ 附录
REPORT = (
    "# 某公司两期经营研究\n\n"
    "## 执行摘要\n\n营收增长 8%，经营平稳。\n\n"
    "## 免责声明\n\n本报告由 AI 生成，不构成任何投资建议。\n\n"
    "## 参考来源\n\n"
    f"1. [2024 年年度报告]({DEAD_A})\n"
    f"2. [2024 年年报摘要]({DEAD_B})\n"
    f"3. [行业景气数据]({LIVE})\n"
    "\n## 附录\n\n底稿明细见 working_paper.csv。\n"
)


def _mk_ws(records=None, *, with_project=False):
    """临时工作区；`records` 非 None 时写入 `dead_sources.json`（编排器同形）。"""
    tmp = Path(tempfile.mkdtemp(prefix="wm_t24_ws_"))
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    if with_project:
        (tmp / "project").mkdir()
    if records is not None:
        (tmp / ac.DEAD_SOURCES_FILE).write_bytes(
            json.dumps(records, ensure_ascii=False).encode("utf-8"))
    return tmp


class TestDeadSourcesFileWritten(unittest.TestCase):
    """① 编排器：报告步骤派发前把死链清单（含原因）落盘为 dead_sources.json。"""

    def _orch(self, task_id="t24"):
        from test_orchestrator_v2 import FakeMessaging, make_orch
        o = make_orch()
        o._messaging = FakeMessaging()
        o._now_iso = lambda: "t"
        o._task_goals = {task_id: "目标"}
        return o

    def test_records_carry_url_and_reason(self):
        o = self._orch()
        self.assertTrue(o._mark_dead_fetch_url("t24", DEAD_A, "策略拒绝"))
        self.assertTrue(o._mark_dead_fetch_url("t24", DEAD_B, "HTTP 404"))
        recs = o.dead_sources_records("t24")
        self.assertEqual(len(recs), 2)
        by_url = {r["url"]: r["reason"] for r in recs}
        self.assertEqual(by_url[DEAD_A], "策略拒绝",
                         "落盘必须带原因（此前 reason 被丢弃）")
        self.assertEqual(by_url[DEAD_B], "HTTP 404")

    def test_write_file_writes_sorted_json_records(self):
        o = self._orch()
        o._mark_dead_fetch_url("t24", DEAD_B, "策略拒绝")
        o._mark_dead_fetch_url("t24", DEAD_A, "HTTP 404")
        with mock.patch.object(ov, "task_workspace", return_value=_mk_ws()):
            path = o._write_dead_sources_file("t24")
        self.assertTrue(path)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual([r["url"] for r in data],
                         sorted([DEAD_A, DEAD_B]), "按 URL 排序，落盘稳定可对账")
        self.assertTrue(all("reason" in r for r in data))

    def test_write_file_always_overwrites_including_empty(self):
        """重跑任务不带上一轮陈旧死链：没有死链也整体覆写为空数组。"""
        o = self._orch()
        ws = _mk_ws()
        with mock.patch.object(ov, "task_workspace", return_value=ws):
            o._mark_dead_fetch_url("t24", DEAD_A)
            o._write_dead_sources_file("t24")
            self.assertEqual(
                len(json.loads((ws / ac.DEAD_SOURCES_FILE)
                               .read_text(encoding="utf-8"))), 1)
            o._forget_dead_urls("t24")
            o._write_dead_sources_file("t24")
        self.assertEqual(
            json.loads((ws / ac.DEAD_SOURCES_FILE).read_text(encoding="utf-8")),
            [], "终态清空后落盘必须是空数组，不是陈旧清单")

    def test_report_step_dispatch_writes_file_before_dispatch(self):
        """接线守卫：报告步骤派发**前**文件已就位（报告生成时按它标注）。"""
        o = self._orch()
        o._mark_dead_fetch_url("t24", DEAD_A, "策略拒绝")
        # 用真实 execute_step 路径验证：monkeypatch workspace 根到临时目录
        tmp = Path(tempfile.mkdtemp(prefix="wm_t24_dispatch_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        import workspace as ws_mod
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(tmp))
        self.addCleanup(lambda: setattr(ws_mod, "WORKSPACE_ROOT", old_root))
        step = {"step_id": "5", "capability": "report_generator",
                "instruction": "写报告", "depends_on": [], "mode": "pipeline"}
        dispatched = []

        def _dispatch_step_safe(_goal, _step, _tid, _state):
            dispatched.append(_step["step_id"])
            exists = (tmp / "t24" / ac.DEAD_SOURCES_FILE).exists()
            self.assertTrue(
                exists, "报告步骤派发前 dead_sources.json 必须已写入工作区")
            return {"task_id": _step["step_id"], "status": "SUCCESS", "result": "ok"}

        o._dispatch_step_safe = _dispatch_step_safe
        with mock.patch.object(ov, "push_progress"):
            o._execute_steps([step], "t24", "目标")
        self.assertEqual(dispatched, ["5"])


class TestAnnotationInReport(unittest.TestCase):
    """② 报告生成器：参考来源命中行尾追加「⚠️ 链接已失效」（prescribed case 1）。"""

    def _write(self, report: str, ws_dir):
        reports = ws_dir / "reports"
        reports.mkdir(exist_ok=True)
        info = ReportGeneratorWorker._write_report_files(reports, report,
                                                         ws_dir=ws_dir)
        return (reports / "report.md").read_text(encoding="utf-8"), info

    def test_two_dead_links_annotated_live_untouched(self):
        ws = _mk_ws([{"url": DEAD_A, "reason": "策略拒绝"},
                     {"url": DEAD_B, "reason": "HTTP 404"}])
        out, _info = self._write(REPORT, ws)
        lines = out.splitlines()
        dead_lines = [ln for ln in lines if DEAD_A in ln or DEAD_B in ln]
        self.assertEqual(len(dead_lines), 2)
        for ln in dead_lines:
            self.assertIn(MARK, ln,
                          f"失效来源行必须带标注：{ln}")
        live_lines = [ln for ln in lines if LIVE in ln]
        self.assertEqual(len(live_lines), 1)
        self.assertNotIn(MARK, live_lines[0], "活链不得被误标")
        self.assertEqual(out.count(MARK), 2, "标注数必须等于失效链接数")

    def test_body_lines_outside_reference_section_untouched(self):
        """标注只落在来源清单章节内；正文行里的同一 URL 不动。"""
        ws = _mk_ws([{"url": DEAD_A, "reason": "策略拒绝"}])
        report = (
            "# 某公司两期经营研究\n\n"
            f"## 执行摘要\n\n年报见 {DEAD_A}（正文引用）。\n\n"
            "## 免责声明\n\n本报告由 AI 生成。\n\n"
            f"## 参考来源\n\n1. [年报]({DEAD_A})\n"
        )
        out, _info = self._write(report, ws)
        body_line = [ln for ln in out.splitlines()
                     if DEAD_A in ln and "正文引用" in ln][0]
        self.assertNotIn(MARK, body_line, "正文行不得被标注")
        list_line = [ln for ln in out.splitlines()
                     if DEAD_A in ln and ln.startswith("1.")][0]
        self.assertIn(MARK, list_line, "来源清单行必须标注")

    def test_no_reference_section_appends_nothing(self):
        """没有来源清单章节 → 什么都不做，绝不新建章节。"""
        ws = _mk_ws([{"url": DEAD_A, "reason": "策略拒绝"}])
        report = "# 报告\n\n正文。\n\n## 附录\n\n明细\n"
        out, _info = self._write(report, ws)
        self.assertNotIn(MARK, out)
        self.assertNotIn("参考来源", out, "不得新建参考来源章节")
        self.assertIn("## 附录", out)

    def test_annotation_is_idempotent(self):
        ws = _mk_ws([{"url": DEAD_A, "reason": "策略拒绝"}])
        once, _info = self._write(REPORT, ws)
        self.assertEqual(once.count(MARK), 1)
        twice, _info2 = self._write(once, ws)
        self.assertEqual(twice.count(MARK), 1, "重复标注不得叠加")

    def test_source_list_heads_cover_all_three(self):
        """`## 来源清单`/`## 参考资料` 同样是标注范围（格式要求同形）。"""
        for head in ("来源清单", "参考资料"):
            ws = _mk_ws([{"url": DEAD_A, "reason": "策略拒绝"}])
            report = f"# 报告\n\n## {head}\n\n1. [年报]({DEAD_A})\n"
            out, _info = self._write(report, ws)
            self.assertEqual(out.count(MARK), 1, head)


class TestMissingDeadSourcesFile(unittest.TestCase):
    """② prescribed case 2：`dead_sources.json` 缺失/损坏 → 报告照常生成。"""

    def test_missing_file_report_unchanged(self):
        ws = _mk_ws()  # 目录存在，文件缺失
        reports = ws / "reports"
        reports.mkdir()
        info = ReportGeneratorWorker._write_report_files(reports, REPORT,
                                                         ws_dir=ws)
        out = (reports / "report.md").read_text(encoding="utf-8")
        self.assertEqual(out, REPORT, "无死链清单 → 原文返回，一个字不改")
        self.assertNotIn(MARK, out)
        self.assertTrue((reports / "report_body.md").exists(),
                        "报告照常落盘（主文/附录拆分不受影响）")

    def test_corrupt_json_is_tolerated(self):
        ws = _mk_ws()
        (ws / ac.DEAD_SOURCES_FILE).write_bytes(b"{not-json")
        out = ReportGeneratorWorker._annotate_dead_sources(REPORT, ws)
        self.assertEqual(out, REPORT, "JSON 损坏 → 容错返回原文，不抛异常")

    def test_ws_dir_none_degrades_to_noop(self):
        """无工作区（task 无 workspace）→ 不标注（静默降级，不影响成稿）。"""
        out = ReportGeneratorWorker._annotate_dead_sources(REPORT, None)
        self.assertEqual(out, REPORT)

    def test_empty_records_noop(self):
        ws = _mk_ws([])
        out = ReportGeneratorWorker._annotate_dead_sources(REPORT, ws)
        self.assertEqual(out, REPORT)


class TestAcceptanceDeadSourceEvidence(unittest.TestCase):
    """③ 验收：`url_health.dead_source_count` 进证据，>0 → 弱缺口（不翻 overall）。"""

    def _run(self, ws_dir, report=REPORT):
        with mock.patch.dict("os.environ", {"URL_HEALTH_CHECK": "0"}):
            return ac.run_acceptance(
                "t24-acc", "研究某公司两期经营", report, str(ws_dir),
                profile="research", report_type="research_brief")

    def test_dead_source_count_in_evidence(self):
        ws = _mk_ws([{"url": DEAD_A, "reason": "策略拒绝"},
                     {"url": DEAD_B, "reason": "HTTP 404"}], with_project=True)
        result = self._run(ws)
        health = result["checks"].get("url_health") or {}
        self.assertEqual(health.get("dead_source_count"), 2,
                         "失效来源数必须进验收证据")
        self.assertEqual(health.get("dead_count"), 2)
        self.assertTrue(health.get("pass"), "证据壳必须 pass=True（弱缺口不阻断）")
        self.assertFalse(health.get("counted"), "不得计入 overall 缺口")
        self.assertTrue(any("失效链接" in g for g in result["gaps"]),
                         f"缺口必须可见：{result['gaps']}")
        self.assertIn(DEAD_A, str(health.get("dead_source_urls") or []),
                      "证据里要能看到具体是哪些 URL")

    def test_weak_gap_does_not_flip_overall(self):
        """同一份报告：有/无死链清单 overall 一致（不阻断，但缺口可见）。"""
        clean = _mk_ws(with_project=True)
        dead = _mk_ws([{"url": DEAD_A, "reason": "策略拒绝"}], with_project=True)
        r_clean = self._run(clean)
        r_dead = self._run(dead)
        self.assertEqual(r_clean["overall"], r_dead["overall"],
                         "弱缺口不得翻转 overall")
        event = ac.build_acceptance_event(r_dead, trigger="测试")
        self.assertEqual(event["url_health_dead"], 1,
                         "审计事件必须带 url_health_dead")
        self.assertNotIn("url_health", event["checks_failed"],
                         "弱缺口不得计成失败项")

    def test_no_dead_sources_no_evidence_shell_gap(self):
        """没有死链清单：不建证据壳、不加缺口（不编数）。"""
        ws = _mk_ws(with_project=True)
        result = self._run(ws)
        health = result["checks"].get("url_health")
        self.assertTrue(health is None or not health.get("dead_source_count"),
                        "无死链不得编 dead_source_count")
        self.assertFalse(any("失效链接" in g for g in result["gaps"]))


class TestFingerprintExcludesDeadUrls(unittest.TestCase):
    """④ `sources_fingerprint`（PACKAGE_MANIFEST 同源）剔除失效来源 URL。"""

    def _ws_with_sources(self, records, *, include_dead_in_sources=True):
        """workspace：project/search_results.json（含死链+活链）＋ dead_sources.json。"""
        ws = _mk_ws(records, with_project=True)
        items = [{"title": "2024 年年度报告", "url": DEAD_A, "snippet": "年报正文"},
                 {"title": "行业数据", "url": LIVE, "snippet": "景气指数"}]
        if not include_dead_in_sources:
            items = items[1:]
        (ws / "project" / "search_results.json").write_bytes(
            json.dumps(items, ensure_ascii=False).encode("utf-8"))
        return ws

    def test_dead_url_absent_from_fingerprint(self):
        """prescribed case 4：有失效标注的指纹 == 只含活链的指纹。"""
        ws_marked = self._ws_with_sources(
            [{"url": DEAD_A, "reason": "策略拒绝"}])
        ws_live_only = self._ws_with_sources(None, include_dead_in_sources=False)
        with mock.patch("workspace.task_workspace", return_value=ws_marked):
            fp_marked = dp.sources_fingerprint("t24-fp", REPORT)
        with mock.patch("workspace.task_workspace", return_value=ws_live_only):
            fp_live = dp.sources_fingerprint(
                "t24-fp", REPORT.replace(f"1. [2024 年年度报告]({DEAD_A})\n", ""))
        self.assertNotEqual(fp_marked, "", "指纹必须算得出")
        self.assertEqual(fp_marked, fp_live,
                         "失效来源不得参与指纹（剔除后与只含活链一致）")

    def test_control_without_dead_sources_fingerprint_differs(self):
        """对照组：同样的报告，没有 dead_sources.json → 死链参与指纹（两者不同）。

        证明上面的相等确实来自"按死链剔除"，而不是指纹本来就不含来源。
        """
        ws_plain = self._ws_with_sources(None)  # search_results 含死链，无清单
        ws_marked = self._ws_with_sources([{"url": DEAD_A, "reason": "策略拒绝"}])
        with mock.patch("workspace.task_workspace", return_value=ws_plain):
            fp_plain = dp.sources_fingerprint("t24-fp", REPORT)
        with mock.patch("workspace.task_workspace", return_value=ws_marked):
            fp_marked = dp.sources_fingerprint("t24-fp", REPORT)
        self.assertNotEqual(fp_plain, fp_marked,
                            "无失效清单时死链参与指纹（对照组必须不同）")

    def test_no_dead_sources_fingerprint_unchanged_semantics(self):
        """无死链清单 → 指纹与既有实现同语义（来源通道 + srclist 块）。"""
        ws = self._ws_with_sources(None)
        with mock.patch("workspace.task_workspace", return_value=ws):
            fp = dp.sources_fingerprint("t24-fp", REPORT)
            fp_again = dp.sources_fingerprint("t24-fp", REPORT)
        self.assertEqual(fp, fp_again, "同一输入指纹必须稳定")


if __name__ == "__main__":
    unittest.main(verbosity=2)
