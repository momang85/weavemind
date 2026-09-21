# -*- coding: utf-8 -*-
"""F3-B 退出关卡：五类**固定离线场景**跑真实装配/渲染/导出路径，逐项确定性核对。

为什么冻结成场景：单测各自钉一处行为，但"整条链路一起改"时没人保证组合起来还对。
五份输入（`evals/scenarios/*.json`）覆盖：①正常增长且证据齐全 ②亏损/现金净流出+跨期
单位不一致 ③缺附注且资料截止不满足 ④三项同步下降（护栏措辞）⑤错主体/错期间材料被排除
且不计入证据；跑法见 `scripts/scenario_run.py`，产物落运行期目录
（`.weavemind/scenarios/`），跨修订可直接比对 manifest。

为什么是模块而不是 `test_*.py`：仓库守卫要求每个 `test_*.py` 都必须出现在 `ci.yml` 里，
而工作流文件需要 `workflow` scope 才能推送；把场景检查做成模块、由 CI 清单内的
`test_offline_delivery.py` 调用，既进了门禁又不新增工作流条目（`run_all()` 供其调用，
本模块也可直接 `python -m unittest scenario_checks` 单独跑）。
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
sys.path.insert(0, str(ROOT / "scripts"))

import scenario_run  # noqa: E402


def run_all(*, out_root: Path | None = None) -> dict:
    """跑全部冻结场景，返回 `{name: manifest}`（产物写 `out_root`，默认运行期目录）。"""
    root = Path(out_root) if out_root else scenario_run.OUT_ROOT
    out: dict = {}
    for f in sorted(scenario_run.SCENARIO_DIR.glob("*.json")):
        spec = json.loads(f.read_text(encoding="utf-8"))
        out[str(spec["name"])] = scenario_run.run_scenario(spec, out_root=root)
    return out


class TestOfflineScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="wm_scen_"))
        cls.manifests = run_all(out_root=cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_scenarios_exist(self):
        self.assertEqual(sorted(self.manifests),
                         ["all_decline", "loss_mixed_units", "missing_footnote_asof",
                          "normal_growth", "wrong_subject_period"])

    def test_each_scenario_meets_its_frozen_expectations(self):
        for name, m in self.manifests.items():
            checks = {k: v for k, v in (m.get("checks") or {}).items() if k != "all_passed"}
            self.assertTrue(checks, f"{name} 没有核对项")
            failed = [k for k, v in checks.items() if not v]
            self.assertEqual(failed, [], f"{name} 未通过：{failed}；交付={m['delivery']}")

    def test_normal_growth_is_verified_with_full_evidence(self):
        m = self.manifests["normal_growth"]
        self.assertEqual(m["delivery"]["status"], "verified", m["delivery"])
        self.assertEqual(m["evidence"]["missing_labels"], [])
        self.assertGreaterEqual(m["evidence"]["located"], 4)
        self.assertEqual(m["paper"]["problems"], 0)
        self.assertEqual(m["paper"]["gaps"], 0)
        self.assertIn("高于", m["coverage_finding"])
        self.assertEqual(m["acceptance"]["overall"], "pass", m["acceptance"])

    def test_loss_scenario_keeps_honest_gaps_and_no_yoy_on_negative_base(self):
        m = self.manifests["loss_mixed_units"]
        self.assertIn("不表示利润有现金支撑", m["coverage_finding"])
        # 跨期单位不一致 + 基期为负 → 底稿记问题、不硬算同比
        self.assertTrue(any("币种/单位不一致" in p for p in m["paper_problems"]),
                        m["paper_problems"])
        self.assertTrue(any("基期为负" in p for p in m["paper_problems"]),
                        m["paper_problems"])
        self.assertNotIn("revenue_yoy", m["quality_metrics"])
        self.assertNotIn("net_profit_yoy", m["quality_metrics"])
        self.assertEqual(m["delivery"]["status"], "draft")

    def test_missing_footnote_scenario_reports_exclusions_with_reasons(self):
        m = self.manifests["missing_footnote_asof"]
        self.assertGreaterEqual(len(m["evidence"]["missing_labels"]), 1)
        self.assertTrue(any(e.get("validation_status") == "after_as_of"
                            for e in (m["evidence"]["excluded"] or [])),
                        m["evidence"]["excluded"])
        self.assertEqual(m["delivery"]["status"], "draft")
        self.assertIn("低于", m["coverage_finding"])

    def test_artifacts_are_listed_with_hashes_for_diffing(self):
        """每个场景都要有可跨修订比对的产物清单（含 sha256 与版本号）。"""
        for name, m in self.manifests.items():
            arts = m.get("artifacts") or {}
            for required in ("report.md", "report_structure.json",
                             "narrative_evidence.json", "working_paper.json"):
                self.assertIn(required, arts, f"{name} 缺少 {required}")
            self.assertTrue(any(p.startswith("charts/chart_") for p in arts),
                            f"{name} 没有图表产物")
            for path, meta in arts.items():
                self.assertEqual(len(meta["sha256"]), 64, path)
                self.assertGreater(meta["bytes"], 0, path)
            self.assertTrue(m["delivery"]["version_id"], name)

    def test_chart_captions_repeat_no_derived_readings(self):
        """正文图注不得重复派生读数（同比/百分点）：图注单列读数会变成"不可溯源数字"。"""
        for name, m in self.manifests.items():
            report = (self.tmp / name / "report.md").read_text(encoding="utf-8")
            captions = [ln for ln in report.splitlines() if ln.startswith("图 ")]
            self.assertTrue(captions, f"{name} 没有图注")
            for ln in captions:
                self.assertNotIn("%", ln, f"{name} 图注含百分比读数：{ln[:80]}")
                self.assertNotIn("个百分点", ln, f"{name} 图注含百分点读数：{ln[:80]}")

    # ── C3：成稿形态（主文给研究员，工程说明与口径细节进附录）────────────

    def test_no_engineering_notes_in_the_delivered_body(self):
        """工程说明（"由代码装配""内部规则"）不得进产品主文，改放任务详情。"""
        for name in self.manifests:
            report = (self.tmp / name / "report.md").read_text(encoding="utf-8")
            for bad in ("装配说明", "由代码装配", "内部规则"):
                self.assertNotIn(bad, report, f"{name} 正文仍含工程说明：{bad}")

    def test_main_body_stays_within_page_target(self):
        """主文（附录之前）以 2–4 页为设计目标：附录标题必须落在第 5 页以内。"""
        import io
        from pypdf import PdfReader
        for name in self.manifests:
            pdf = (self.tmp / name / "report.pdf")
            if not pdf.exists():
                continue
            pages = list(PdfReader(str(pdf)).pages)
            hit = next((i for i, pg in enumerate(pages, 1)
                        if any(l.strip() == "附录" for l in
                               (pg.extract_text() or "").splitlines())), None)
            self.assertIsNotNone(hit, f"{name} 找不到附录标题")
            self.assertLessEqual(hit, 5, f"{name} 主文到第 {hit} 页才开始附录")

    def test_evidence_moved_to_appendix_is_still_present(self):
        """让出主文版面的证据（口径条件/次要图表）必须在附录里仍在，不得为压页数删除。"""
        for name, m in self.manifests.items():
            report = (self.tmp / name / "report.md").read_text(encoding="utf-8")
            head, _, tail = report.partition("## 附录")
            self.assertTrue(tail, f"{name} 没有附录")
            self.assertIn("比率适用条件", tail, f"{name} 附录缺口径条件")
            self.assertIn("参考来源", tail, f"{name} 附录缺来源清单")
            extra = len(m.get("charts") or []) - 3
            if extra > 0:
                self.assertIn("其他图表", tail,
                              f"{name} 有 {extra} 张图不在主文，附录里应列出")

    # ── C1：导出可用性（图必须真进 PDF、表头必须与数据列对齐）──────────

    def test_referenced_charts_are_embedded_not_placeholders(self):
        """正文引用的每张图都要真进 PDF。

        原缺陷（三个场景各缺 5/2/4 张）：场景跑法只把 PNG 拷到**场景输出目录**，
        而导出按 `<工作区>/charts/` 解析图片 → 每张图渲染成"[图片未能嵌入]"占位，
        页数/字符检查却照样 pass。
        """
        for name, m in self.manifests.items():
            export = m["pdf_export"]
            images = export["images"]
            expected = len(m["charts"])
            self.assertGreater(expected, 0, f"{name} 没有图表")
            self.assertEqual(images["expected"], expected, name)
            self.assertEqual(images["images"], expected,
                             f"{name} 嵌入图数 {images['images']} ≠ 引用图数 {expected}")
            self.assertEqual(images["placeholders"], 0,
                             f"{name} 仍有缺图占位：{images['issues']}")
            self.assertEqual(export["verdict"], "pass", export["issues"])

    def test_table_headers_share_the_column_grid(self):
        """表头与数据列共用列网格（原缺陷：表头拼成一行画在左上角）。"""
        for name, m in self.manifests.items():
            tables = m["pdf_export"]["tables"]
            self.assertTrue(tables, f"{name} 没有表")
            for g in tables:
                self.assertTrue(g["aligned"],
                                f"{name} 第 {g['page']} 页表头未对齐：{g}")

    def test_pdf_verdicts_are_named_honestly(self):
        """"可解析"与"导出完整"分开：页数/文字量不得冒充视觉通过。"""
        for name, m in self.manifests.items():
            self.assertIn("pdf_parse", m, name)
            self.assertIn("pdf_export", m, name)
            self.assertNotIn("visual_verdict", m, f"{name} 仍用旧名冒充视觉判定")
            self.assertEqual(m["pdf_parse"]["verdict"], "pass", m["pdf_parse"]["issues"])

    def test_chart_canvases_are_sane_size(self):
        """图表 PNG 画布尺寸必须正常（高宽比 ≤ 上限）。

        原缺陷：全是负值的同比图被 `set_ylim(bottom=0)` 弄成奇异坐标变换，数值标签
        被甩到画布外 → `bbox_inches="tight"` 撑出 1038×121366 的空白巨图（场景
        loss/missing 各一张）。这种图在报告里几乎全白，必须由退出关卡拦住。
        """
        import struct
        import chart_qa
        for name in self.manifests:
            pngs = sorted((self.tmp / name / "charts").glob("chart_*.png"))
            self.assertTrue(pngs, f"{name} 没有图表 PNG")
            for p in pngs:
                head = p.read_bytes()[:24]
                w, h = struct.unpack(">II", head[16:24])
                self.assertLessEqual(
                    h / w, chart_qa.MAX_CANVAS_ASPECT,
                    f"{name}/{p.name} 画布异常：{w}×{h}")
                self.assertIsNone(chart_qa.canvas_aspect_issue(p), p.name)

    def test_canvas_aspect_check_has_teeth(self):
        """自证有齿：把正常 PNG 的 IHDR 高度改成 10 万 → 必须报画布异常。"""
        import struct
        import chart_qa
        src = sorted((self.tmp / "normal_growth" / "charts").glob("chart_1.png"))[0]
        raw = bytearray(src.read_bytes())
        w = struct.unpack(">II", bytes(raw[16:24]))[0]
        raw[20:24] = struct.pack(">I", 100000)
        bad = self.tmp / "_bad_canvas.png"
        bad.write_bytes(bytes(raw))
        issue = chart_qa.canvas_aspect_issue(bad)
        self.assertIsNotNone(issue, f"异常画布未被识别（{w}×100000）")
        self.assertEqual(issue["type"], "canvas_aspect")
        self.assertIsNone(chart_qa.canvas_aspect_issue(src), "正常图被误判")

    def test_oversized_image_is_a_placeholder_not_a_silent_offpage_draw(self):
        """单页放不下的图按缺图处理：可见占位 + 导出完整性失败（不得算"已嵌入"）。

        反例来源：场景里被 tight bbox 撑爆的 1038×121366 PNG——当时它被"成功嵌入"
        （资源里有 Image 对象）却画在页面可视区之外，页面上什么都看不到，
        导出完整性判定也照样 pass。这里用一张**数据一致**的过高 PNG 复现。
        """
        import report_pdf
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        # 1038×2000：高宽比正常，但按可用宽度缩放后高度仍超过单页可用高度
        tall = self.tmp / "chart_tall.png"
        Image.new("RGB", (1038, 2000), "white").save(tall)
        md = f"# 图\n\n![chart_tall.png]({tall.as_posix()})\n\n正文。\n"
        pdf = report_pdf.markdown_to_pdf(md, title="过高图", workspace=None)
        img = report_pdf.pdf_image_report(pdf)
        self.assertEqual(img["placeholders"], 1, img)
        self.assertEqual(img["drawn_on_page"], 0, img)
        self.assertFalse(img["ok"], "过高图未被判为导出不完整")
        self.assertTrue(img["oversized_placeholder"],
                        f"占位未说明尺寸异常：{img['issues']}")

    def test_table_header_alignment_check_has_teeth(self):
        """自证有齿：把表头改回"拼成一行画在左上角"，对齐检查必须报红。

        直接渲染一份表（长中文标题/负数/单位/年份列）→ 断言表头落在数据列位上；
        再把 `_draw_table_header` 换回旧写法（各列名拼成一段、画在左边距 +4），
        断言同一检查判为不对齐。
        """
        import report_pdf
        md = ("| 指标 | 2023年 | 2024年 | 口径 | 来源 |\n"
              "| --- | --- | --- | --- | --- |\n"
              "| 营业收入（亿元） | 1476.94 | 1708.99 | 合并 | 年报 |\n"
              "| 归母净利润（亿元） | 747.34 | 862.28 | 合并 | 年报 |\n"
              "| 经营活动现金流净额（亿元） | -46.29 | 66.73 | 合并 | 年报 |\n")
        grids = report_pdf.pdf_table_grids(report_pdf.markdown_to_pdf(md, title="表头对齐自证"))
        self.assertTrue(grids, "没有解析到表")
        self.assertTrue(all(g["aligned"] for g in grids), grids)
        self.assertTrue(all(g["cols"] == 5 for g in grids), grids)

        def _old_header(self, top_y, header_lines, col_w, header_h):
            self.page_content += (
                f"q 0.16 0.20 0.34 rg {report_pdf.MARGIN_L:.2f} "
                f"{top_y - header_h:.2f} {report_pdf.USABLE_W:.2f} "
                f"{header_h:.2f} re f Q\n"
            ).encode()
            self.cursor_y = top_y - 5 - report_pdf.TABLE_SIZE
            self._draw_text(report_pdf.MARGIN_L + 4,
                            "  ".join("".join(ln) for ln in header_lines)[:20],
                            report_pdf.TABLE_SIZE, (1, 1, 1))

        original = report_pdf._PDFBuilder._draw_table_header
        report_pdf._PDFBuilder._draw_table_header = _old_header
        try:
            bad = report_pdf.markdown_to_pdf(md, title="表头对齐自证")
        finally:
            report_pdf._PDFBuilder._draw_table_header = original
        bad_grids = report_pdf.pdf_table_grids(bad)
        self.assertTrue(any(not g["aligned"] for g in bad_grids),
                        f"拼接式表头未被判为不对齐：{bad_grids}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
