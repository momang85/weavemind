# -*- coding: utf-8 -*-
"""任务22 验收：报告精简——主文 1800 字约束 + 主文/附录拆分 + 交付包含主文。

覆盖 docs/arch_phase4_task22_report_compaction.md 的五步：
  ① 编排器指令：`_REPORT_ANALYSIS_REQUIREMENTS` 带篇幅硬约束（主文 ≤1800 字、
     附录分层）；`_REPORT_FORMAT_REQUIREMENTS` 注明参考来源清单移到 ## 附录；
  ② 报告落盘：`report.md`（完整版，不变）+ `report_body.md`/`report_appendix.md`
     拆分，主文超 3600 字符只 warning（不阻断），length_warning 随步骤结果带回；
  ③ 交付包：`reports/report_body.md` 随包（同一切分口径，从交付正文直接拆分，
     不读磁盘——正文修订期间包内主文不与完整版错版）；没有附录时不进包；
  ④ 验收证据：超字数时 `acceptance_evidence` 带 `length_warning`，overall 不变
     （任务不因此 FAIL）；未超限时键缺席；
  ⑤ PDF：优先用主文渲染（web_ui 导出路径）。
"""
import json
import logging
import os
import sqlite3
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent

import delivery_pipeline as dp  # noqa: E402
import report_quality as rq  # noqa: E402
import task_state as ts  # noqa: E402
from workers.report_generator_worker import ReportGeneratorWorker  # noqa: E402


def _report(body_chars: int = 900, appendix: bool = True) -> str:
    """与格式要求同构的报告样本：主文（≤1800 字）+ 免责声明 + 附录（来源清单）。

    任务25 后落盘报告必须带 `## 免责声明`（缺字样会被 `_ensure_disclaimer`
    自动注入），fixture 与生产形态同构：声明放在参考来源之前。
    """
    body = (
        "# 测试公司两期经营研究\n\n"
        "## 数据时效\n\n行情数据截至 2026-08-30 15:00 收盘。\n\n"
        "## 执行摘要\n\n" + ("经营平稳。" * (body_chars // 5)) + "\n\n"
        "## 风险与结论\n\n结论：仅陈述水平与变化，不构成投资建议。\n\n"
        "## 免责声明\n\n本报告由 AI 生成，不构成任何投资建议；"
        "数据来源于公开披露，可能存在时效性差异和获取误差。\n"
    )
    if not appendix:
        return body
    return body + (
        "\n## 参考来源\n\n"
        "1. [示例标题](https://example.com/a)\n"
        "2. [示例标题二](https://example.com/b)\n"
    )


class TestOrchestratorLengthInstruction(unittest.TestCase):
    """① 编排器给报告步骤的指令带篇幅约束与主文/附录分层。"""

    def test_analysis_requirements_carry_body_cap(self):
        import orchestrator_v2
        reqs = orchestrator_v2._REPORT_ANALYSIS_REQUIREMENTS
        self.assertIn("9. 篇幅约束", reqs)
        self.assertIn("不超过 1800 字", reqs)
        self.assertIn("## 附录", reqs, "附录分层指令必须点名附录标题")
        self.assertIn("关键数字一览表", reqs, "关键数字表必须留在主文")

    def test_format_requirements_note_appendix_refs(self):
        import orchestrator_v2
        reqs = orchestrator_v2._REPORT_FORMAT_REQUIREMENTS
        self.assertIn("参考来源清单移到", reqs)
        self.assertIn("[n] 上标", reqs, "主文正文只保留编号上标，不展开 URL")


class TestSplitReportAppendix(unittest.TestCase):
    """② 唯一切分口径：`split_report_appendix`，两段拼回等于原文。"""

    def test_split_at_reference_heading(self):
        text = _report(appendix=True)
        body, appendix = rq.split_report_appendix(text)
        self.assertTrue(body.startswith("# 测试公司两期经营研究"))
        self.assertIn("## 风险与结论", body)
        self.assertNotIn("参考来源", body)
        self.assertTrue(appendix.startswith("## 参考来源"))
        self.assertIn("[示例标题](https://example.com/a)", appendix)
        # 切分不改写内容：两段合起来保住原文的全部小节
        self.assertIn("## 执行摘要", body + appendix)
        self.assertIn("## 风险与结论", body + appendix)

    def test_no_appendix_returns_full_text(self):
        text = _report(appendix=False)
        body, appendix = rq.split_report_appendix(text)
        self.assertEqual(body, text)
        self.assertEqual(appendix, "")

    def test_appendix_headings_cover_doc_cases(self):
        """分割点认 ## 附录 / ## 参考来源（设计文档点名）与数据来源/核查细节。"""
        for heading in ("## 附录", "## 参考来源", "## 数据来源", "## 核查细节",
                        "## 附录：底稿明细"):
            body, appendix = rq.split_report_appendix("# T\n\n主文。\n\n" + heading + "\n\n明细\n")
            self.assertNotIn("明细", body, heading)
            self.assertTrue(appendix.startswith(heading.split("：")[0]))

    def test_h3_and_bold_headings_do_not_split(self):
        """只有二级标题是分割点；三级/加粗行不切。"""
        text = "# T\n\n主文。\n\n### 附录\n\nxxx\n"
        body, appendix = rq.split_report_appendix(text)
        self.assertEqual(appendix, "")


class TestBodyLengthWarning(unittest.TestCase):
    """② 字数校验：超限给告警字典，未超限 None（键缺席，不编 0）。"""

    def test_under_limit_is_none(self):
        self.assertIsNone(rq.body_length_warning("短主文"))
        self.assertIsNone(rq.body_length_warning("x" * rq.BODY_CHAR_LIMIT))

    def test_over_limit_carries_chars_and_note(self):
        warn = rq.body_length_warning("x" * (rq.BODY_CHAR_LIMIT + 1))
        self.assertEqual(warn["chars"], rq.BODY_CHAR_LIMIT + 1)
        self.assertEqual(warn["limit"], rq.BODY_CHAR_LIMIT)
        self.assertIn("不影响", warn["note"], "只提示，不改验收结论")


class _ListHandler(logging.Handler):
    """记录日志消息列表（合规路径可能一条日志都没有，assertLogs 会误报）。"""

    def __init__(self):
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class TestWorkerWritesBodyAndAppendix(unittest.TestCase):
    """② 报告落盘：三文件 + 字数告警随步骤结果带回。"""

    def _write(self, report: str):
        tmp = Path(tempfile.mkdtemp(prefix="wm_t22_worker_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        handler = _ListHandler()
        # 缺陷B 后落盘收敛到 report_quality.write_report_files（唯一权威写入口），
        # 因此字数告警从 report_quality 记录器发出；两个记录器都挂，断言不依赖实现位置。
        loggers = [logging.getLogger("workers.report_generator_worker"),
                   logging.getLogger("report_quality")]
        for lg in loggers:
            lg.addHandler(handler)
        try:
            info = ReportGeneratorWorker._write_report_files(tmp, report)
        finally:
            for lg in loggers:
                lg.removeHandler(handler)
        return tmp, info, handler.messages

    def test_three_files_written_and_appendix_not_in_body(self):
        report = _report(appendix=True)
        tmp, info, _cm = self._write(report)
        full = (tmp / "report.md").read_text(encoding="utf-8")
        body = (tmp / "report_body.md").read_text(encoding="utf-8")
        appendix = (tmp / "report_appendix.md").read_text(encoding="utf-8")
        self.assertEqual(full, report, "report.md 完整版保留，向下兼容")
        self.assertIn("参考来源", full, "附录内容在 report.md 里存在")
        self.assertNotIn("参考来源", body, "附录内容不在 report_body.md 里")
        self.assertIn("风险与结论", body, "风险结论必须留在主文")
        self.assertTrue(appendix.startswith("## 参考来源"))
        self.assertEqual(info["body_chars"], len(body))
        self.assertEqual(info["appendix_chars"], len(appendix))
        self.assertEqual(info["chars"], len(report))
        self.assertNotIn("length_warning", info, "合规主文不产生告警")

    def test_compliant_body_stays_under_cap(self):
        """①/② 合规输出：主文 ≤3600 字符（1800 中文字 × 2 的估算上限）。"""
        tmp, info, _cm = self._write(_report(body_chars=900, appendix=True))
        self.assertLessEqual(info["body_chars"], rq.BODY_CHAR_LIMIT)
        self.assertTrue((tmp / "report_body.md").exists())

    def test_no_appendix_writes_no_duplicate_body(self):
        """无来源清单（完整版即主文）时不写重复副本。"""
        tmp, info, _cm = self._write(_report(appendix=False))
        self.assertFalse((tmp / "report_body.md").exists())
        self.assertFalse((tmp / "report_appendix.md").exists())
        self.assertEqual(info["body_chars"], info["chars"])

    def test_oversized_body_logs_warning_and_returns_length_warning(self):
        report = _report(body_chars=5000, appendix=True)
        tmp, info, cm = self._write(report)
        self.assertIn("length_warning", info)
        self.assertGreater(info["body_chars"], rq.BODY_CHAR_LIMIT)
        self.assertTrue(any("主文超字数限制" in line for line in cm),
                        "超字数必须 logger.warning（只警告不阻断）")
        self.assertTrue((tmp / "report.md").exists(), "超字数不阻断交付")


class TestReportFilesInvariant(unittest.TestCase):
    """③ 缺陷B（真机 E2E 复现）：三件必须同源——body + appendix == report.md。

    真机交付包（deliverables_20261019_163107.zip）里 report.md 是 16:56 重写版，
    report_body.md/report_appendix.md 还是 16:17 成稿版，一份交付两个版本。
    收敛到 report_quality.write_report_files 唯一写入口后逐字节恒等。
    """

    def _tmp(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_t22_invariant_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def test_worker_output_body_plus_appendix_equals_full(self):
        """ prescribed invariant：report_body + report_appendix == report.md。"""
        for appendix in (True, False):
            tmp = self._tmp()
            report = _report(body_chars=900, appendix=appendix)
            ReportGeneratorWorker._write_report_files(tmp, report)
            full = (tmp / "report.md").read_text(encoding="utf-8")
            if appendix:
                body = (tmp / "report_body.md").read_text(encoding="utf-8")
                app = (tmp / "report_appendix.md").read_text(encoding="utf-8")
                self.assertEqual(body + app, full)
            else:
                self.assertFalse((tmp / "report_body.md").exists())
                self.assertFalse((tmp / "report_appendix.md").exists())

    def test_shared_writer_roundtrip_bytes_equal(self):
        """权威写入口本身：切分零改写，两段拼回与完整版逐字节相等。"""
        tmp = self._tmp()
        for text in (_report(appendix=True), _report(appendix=False),
                     "# T\n\n主文结尾无换行\n## 附录：明细\n\n行1\n"):
            rq.write_report_files(tmp, text)
            full = (tmp / "report.md").read_text(encoding="utf-8")
            self.assertEqual(full, text)
            body_p, app_p = tmp / "report_body.md", tmp / "report_appendix.md"
            if rq.APPENDIX_HEADING_RE.search(text):
                self.assertEqual(
                    body_p.read_text(encoding="utf-8")
                    + app_p.read_text(encoding="utf-8"), full)
            else:
                self.assertFalse(body_p.exists())
                self.assertFalse(app_p.exists())

    def test_rewrite_without_appendix_removes_stale_split_files(self):
        """回归：报告改写后不再带附录 → 上次的拆分件必须被清掉。"""
        tmp = self._tmp()
        rq.write_report_files(tmp, _report(appendix=True))   # 第一版：带附录
        self.assertTrue((tmp / "report_body.md").exists())
        rq.write_report_files(tmp, _report(appendix=False))  # 改写：无附录
        self.assertFalse((tmp / "report_body.md").exists(),
                         "陈旧 report_body.md 不得残留")
        self.assertFalse((tmp / "report_appendix.md").exists(),
                         "陈旧 report_appendix.md 不得残留")

    def test_rewrite_replaces_split_files_with_new_version(self):
        """回归（真机错版形态）：report.md 被重写后，拆分件跟到新版。"""
        tmp = self._tmp()
        rq.write_report_files(tmp, _report(body_chars=900, appendix=True))
        old_body = (tmp / "report_body.md").read_text(encoding="utf-8")
        new_report = _report(body_chars=1800, appendix=True)
        rq.write_report_files(tmp, new_report)
        full = (tmp / "report.md").read_text(encoding="utf-8")
        body = (tmp / "report_body.md").read_text(encoding="utf-8")
        app = (tmp / "report_appendix.md").read_text(encoding="utf-8")
        self.assertEqual(body + app, full)
        self.assertNotEqual(body, old_body, "拆分件必须跟到最新版")

    def test_split_is_lossless_for_every_doc_case(self):
        """切分不改写：任何输入下 body + appendix == 原文（此前主文被 rstrip 改写）。"""
        cases = [
            "# T\n\n主文\n\n## 参考来源\n\n1. [a](https://e.com)\n",
            "# T\n\n主文结尾无换行\n## 数据来源\n\n明细\n",
            "纯文本无标题",
            "",
        ]
        for text in cases:
            body, appendix = rq.split_report_appendix(text)
            self.assertEqual(body + appendix, text)

    def test_zip_never_pairs_stale_split_with_report(self):
        """回归（真机 zip 形态）：重打包前拆分件按当前 report.md 重算，包内同源。

        真机 deliverables_20261019_163107.zip：report.md 是 16:56 重写版（无附录），
        report_body.md/report_appendix.md 还是 16:17 成稿版——三个文件都"新鲜"，
        时间戳判不出来。现在入包前按当前 report.md 重算（write_report_files 唯一口径）。
        """
        import time as _t
        from workers.packaging_worker import PackagingWorker
        ws = self._tmp()
        (ws / "project").mkdir(parents=True, exist_ok=True)
        (ws / "project" / "analysis.py").write_text("print(1)\n", encoding="utf-8")
        reports = ws / "reports"
        old = _report(body_chars=900, appendix=True)
        rq.write_report_files(reports, old)                       # 16:17 成稿：三件同源
        stale_body = (reports / "report_body.md").read_text(encoding="utf-8")
        new = _report(body_chars=1800, appendix=True)             # 16:56 重写完整版
        (reports / "report.md").write_text(new, encoding="utf-8")  # 只换完整版（真机形态）
        now = _t.time()
        for p in reports.glob("*.md"):
            os.utime(p, (now, now))                               # 都新鲜，时间戳判不出
        w = PackagingWorker.__new__(PackagingWorker)
        task = {"task_id": "t-22-zip", "workspace": str(ws),
                "task_start_ts": now - 60, "goal": "研究交付"}
        w._package(ws / "project", task)
        zips = sorted(ws.glob("deliverables_*.zip"))
        self.assertEqual(len(zips), 1, zips)
        with zipfile.ZipFile(zips[0]) as zf:
            names = zf.namelist()
            self.assertIn("reports/report.md", names)
            md = zf.read("reports/report.md").decode("utf-8")
            self.assertEqual(md, new, "包内完整版必须是重写后的正文")
            self.assertIn("reports/report_body.md", names)
            body = zf.read("reports/report_body.md").decode("utf-8")
            app = (zf.read("reports/report_appendix.md").decode("utf-8")
                   if "reports/report_appendix.md" in names else "")
            self.assertEqual(body + app, md,
                             "包内主文/附录必须与完整版同源（缺陷B）")
            self.assertNotEqual(body, stale_body, "不得把成稿旧版主文装进包里")
            manifest = json.loads(zf.read("PACKAGE_MANIFEST.json").decode("utf-8"))
            self.assertEqual(manifest.get("delivered_md"), "reports/report.md")

    def test_repack_snapshot_heals_workspace_split_files(self):
        """回归（快照重打包路，真机 16:56 现场）：工作区拆分件跟到冻结版。"""
        import hashlib as _h
        from report_version import VersionStore
        ws = self._tmp()
        tid = "t-22-snap-heal"
        store = VersionStore(ws, tid)
        old = _report(body_chars=900, appendix=True)
        ver = store.record(old, sources_fingerprint="fp-old",
                           rules_version="2026.09.10")
        store.adopt(ver, reason="成稿")
        delivered = _report(body_chars=1800, appendix=True)   # 交付版（与成稿不同版）
        payload = {
            "reports/report.md": delivered.encode("utf-8"),
            "reports/report_body.md": dp._delivered_body_md(delivered.encode("utf-8")),
        }
        snap = {
            "report_version_id": store.adopted().identity_id(),
            "body_sha256": str(getattr(store.adopted(), "version_id", "") or ""),
            "payload": payload,
            "frozen": {a: _h.sha256(b).hexdigest() for a, b in payload.items()},
        }
        res = dp.repack_adopted(tid, ws_dir=ws, snapshot=snap)
        self.assertTrue(res.get("package"), res)
        reports = ws / "reports"
        full = (reports / "report.md").read_text(encoding="utf-8")
        self.assertEqual(full, delivered, "工作区 report.md 与包内同源")
        body = (reports / "report_body.md").read_text(encoding="utf-8")
        app = (reports / "report_appendix.md").read_text(encoding="utf-8")
        self.assertEqual(body + app, full, "拆分件必须跟到冻结版（缺陷B）")
        self.assertNotEqual(body, rq.split_report_appendix(old)[0],
                            "不得残留成稿版的旧主文")


class TestDeliveryPackageCarriesBody(unittest.TestCase):
    """③ 交付包：report_body.md 随包（快照与直读两路同一口径）。"""

    def _tmp_ws(self):
        tmp = Path(tempfile.mkdtemp(prefix="wm_t22_ws_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return tmp

    def test_delivered_body_md_derives_from_same_bytes(self):
        md = _report(appendix=True).encode("utf-8")
        body = dp._delivered_body_md(md)
        self.assertIsNotNone(body)
        self.assertEqual(body.decode("utf-8"), rq.split_report_appendix(
            md.decode("utf-8"))[0])
        self.assertIsNone(dp._delivered_body_md("# 无附录\n\n主文。\n".encode("utf-8")))

    def test_freeze_payload_includes_body_when_appendix_exists(self):
        ws = self._tmp_ws()
        md = _report(appendix=True).encode("utf-8")
        payload = dp._freeze_payload("t-22", ws, md_bytes=md)
        self.assertIn("reports/report.md", payload)
        self.assertIn("reports/report_body.md", payload)
        self.assertEqual(payload["reports/report_body.md"],
                         dp._delivered_body_md(md))
        self.assertNotIn("reports/report_appendix.md", payload,
                         "附录随完整版 report.md 交付，包内不重复收录")

    def test_freeze_payload_without_appendix_unchanged(self):
        """没有附录（完整版即主文）时零影响：包内成员集合不变。"""
        ws = self._tmp_ws()
        md = _report(appendix=False).encode("utf-8")
        payload = dp._freeze_payload("t-22b", ws, md_bytes=md)
        self.assertNotIn("reports/report_body.md", payload)
        self.assertIn("reports/report.md", payload)

    def test_repack_packages_body_and_verifies(self):
        """真实打包链路（非快照直读路）：包内带主文，字节自检一致。"""
        from report_version import VersionStore
        ws = self._tmp_ws()
        tid = "t-22-repack"
        store = VersionStore(ws, tid)
        body = _report(appendix=True)
        ver = store.record(body, sources_fingerprint="fp-t22",
                           rules_version="2026.09.10")
        store.adopt(ver, reason="测试：任务22 打包验收")
        res = dp.repack_adopted(tid, md_bytes=body.encode("utf-8"),
                                pdf_bytes=b"%PDF-1.4 t22", ws_dir=ws)
        self.assertTrue(res.get("package"), res)
        self.assertTrue(all(v == "ok" for v in (res.get("verify") or {}).values()),
                        f"包内字节自检必须全部一致：{res.get('verify')}")
        with zipfile.ZipFile(ws / res["package"]) as zf:
            names = zf.namelist()
            self.assertIn("reports/report.md", names)
            self.assertIn("reports/report_body.md", names)
            self.assertEqual(zf.read("reports/report.md"), body.encode("utf-8"))
            pkg_body = zf.read("reports/report_body.md").decode("utf-8")
            self.assertNotIn("参考来源", pkg_body)
            manifest = json.loads(zf.read("PACKAGE_MANIFEST.json").decode("utf-8"))
            self.assertIn("reports/report_body.md", manifest.get("files") or {})

    def test_repack_without_appendix_has_no_body_member(self):
        from report_version import VersionStore
        ws = self._tmp_ws()
        tid = "t-22-repack-b"
        store = VersionStore(ws, tid)
        body = _report(appendix=False)
        ver = store.record(body, sources_fingerprint="fp-t22b",
                           rules_version="2026.09.10")
        store.adopt(ver, reason="测试：无附录零影响")
        res = dp.repack_adopted(tid, md_bytes=body.encode("utf-8"),
                                pdf_bytes=b"", ws_dir=ws)
        self.assertTrue(res.get("package"), res)
        with zipfile.ZipFile(ws / res["package"]) as zf:
            self.assertNotIn("reports/report_body.md", zf.namelist())


class _TempDbCase(unittest.TestCase):
    """临时任务库 + 临时工作区（与 test_task18 同构），DB_PATH 指过去。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_t22_db_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = str(self.tmp / "agents.db")
        self.ws = self.tmp / "ws"
        self.ws.mkdir(parents=True, exist_ok=True)
        ts.ensure_schema(self.db)
        self._patches = []

        def _p(target, **kwargs):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self._patches.append(patcher)

        _p("task_state.DB_PATH", new=self.db)
        _p("workspace.task_reports_dir", return_value=self.ws / "reports")
        _p("acceptance_checker.append_acceptance_event", return_value=None)
        env = mock.patch.dict(os.environ, {"URL_HEALTH_CHECK": "0"}, clear=False)
        env.start()
        self._patches.append(env)
        for p in self._patches:
            self.addCleanup(p.stop)

    def _row(self, task_id: str) -> sqlite3.Row:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(
                "SELECT * FROM task_history WHERE task_id=?", (task_id,)).fetchone()
        finally:
            con.close()

    def _queued(self, task_id: str = "t-22") -> str:
        ts.mark_queued(task_id, "研究某公司两期经营", db_path=self.db)
        return task_id

    def run_accept(self, task_id: str, result: dict, body: str) -> dict | None:
        """跑一次真实链路（`accept_for_body`），只把 `run_acceptance` 换成假的。"""
        with mock.patch("acceptance_checker.run_acceptance", return_value=result):
            return dp.accept_for_body(task_id, "研究某公司两期经营", body,
                                      trigger="测试", verify_body_as_is=True,
                                      ws_dir=str(self.ws))


def _fake_result(overall: str = "pass", sha: str = "b" * 64) -> dict:
    return {
        "checks": {
            "disclaimer": {"pass": True, "details": "有", "clause": "…"},
            "source_labeling": {"pass": True, "details": "标注可信", "mislabeled": []},
        },
        "overall": overall,
        "profile": "research",
        "report_type": "research_brief",
        "gaps": [],
        "suggestions": [],
        "rules_version": "2026.09.10",
        "rules_fingerprint": "fp-task22",
        "report_sha256": sha,
        "report_sha256_short": sha[:16],
        "evaluated_at": "2026-10-19T00:00:00+00:00",
    }


class TestAcceptanceEvidenceLengthWarning(_TempDbCase):
    """④ 超字数 → acceptance_evidence 带 length_warning，任务不 FAIL。"""

    def test_oversized_body_records_warning_without_fail(self):
        tid = self._queued()
        result = self.run_accept(tid, _fake_result(), _report(body_chars=5000))
        self.assertIsNotNone(result)
        self.assertEqual(result.get("overall"), "pass",
                         "超字数只警告，不得改 overall / 不得让任务 FAIL")
        self.assertIn("length_warning", result)
        stored = json.loads(self._row(tid)["acceptance_json"] or "{}")
        self.assertEqual(stored.get("overall"), "pass")
        lw = stored.get("length_warning")
        self.assertIsInstance(lw, dict, "验收证据入库 length_warning")
        self.assertGreater(lw["chars"], lw["limit"])
        # 投影/API 透传（task18 同一入口）
        proj = ts.task_projection(tid, self.db)
        self.assertIsNotNone(proj["acceptance_evidence"])
        self.assertIn("length_warning", proj["acceptance_evidence"])

    def test_compliant_body_has_no_length_warning_key(self):
        """未超限：键缺席（空值不写，与任务18 的"不编 0"同一口径）。"""
        tid = self._queued("t-22-ok")
        self.run_accept(tid, _fake_result(), _report(body_chars=600))
        stored = json.loads(self._row(tid)["acceptance_json"] or "{}")
        self.assertNotIn("length_warning", stored)
        self.assertEqual(stored.get("overall"), "pass")

    def test_fail_result_keeps_overall_and_carries_warning(self):
        """本来就该 FAIL 的验收不因字数合规被救回；超字数也不加重结论。"""
        tid = self._queued("t-22-fail")
        result = self.run_accept(tid, _fake_result(overall="fail"),
                                 _report(body_chars=5000))
        self.assertEqual(result.get("overall"), "fail")
        self.assertIn("length_warning", result)


class TestPdfPrefersBody(unittest.TestCase):
    """⑤ PDF：导出路径优先用主文渲染（web_ui 导出处理）。"""

    def test_web_ui_renders_pdf_from_body_part(self):
        src = (ROOT / "web_ui.py").read_text(encoding="utf-8")
        self.assertIn("split_report_appendix", src)
        self.assertIn("markdown_to_pdf(pdf_source", src,
                      "PDF 渲染用主文（附录前），不再用完整正文")
        # 渲染源仍来自同一份交付正文（快照口径，不重读磁盘）
        self.assertIn("pdf_source, _appendix = split_report_appendix(delivered)", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
