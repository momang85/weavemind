# -*- coding: utf-8 -*-
"""M0-a 版本与证据绑定回归（离线，不联网不调模型）。

覆盖路线文档要求的最小回归：各自验收的版本比较、原子切换与迟到结果、
恢复/缺证据标未知、同正文不同证据不同身份、导出 manifest 的字节与语义检查分离、
未验收草稿判定。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import report_quality as rq  # noqa: E402
import report_version as rv  # noqa: E402


def _accept(overall: str, body: str, gaps=None) -> dict:
    return {"overall": overall, "gaps": gaps or [],
            "report_sha256": rv.body_hash(body),   # R0.2：绑定只认全量 hash
            "rules_version": "2026.09.12", "rules_fingerprint": "abc12345"}


class TestPerVersionAcceptance(unittest.TestCase):
    """两稿必须用**各自**的验收比较——这正是"纠错稿被旧稿顶掉"的根因。"""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="wm_ver_"))
        self.store = rv.VersionStore(self.ws, "task-1")

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_numeric_only_correction_wins_with_own_acceptance(self):
        old_text = "营业收入999亿元，同比增长20%。"
        new_text = "营业收入800亿元，同比增长20%。"      # 只改数字（纠错）
        old_v = self.store.record(old_text)
        self.store.bind_acceptance(_accept("fail", old_text, ["数字与来源不符"]))
        new_v = self.store.record(new_text, parent_id=old_v.version_id)
        self.store.bind_acceptance(_accept("pass", new_text))

        cur = self.store.get(old_v.version_id)
        cand = self.store.get(new_v.version_id)
        self.assertEqual(cur.acceptance_overall(), "fail")
        self.assertEqual(cand.acceptance_overall(), "pass")
        improved, why = rq.compare_versions(
            old_text, new_text, cur_acceptance=cur.acceptance, cand_acceptance=cand.acceptance)
        self.assertTrue(improved, f"纠错稿应胜出：{why}")

    def test_missing_acceptance_is_unknown_not_pass(self):
        """候选稿没有自己的验收 → 未知；不得借用旧稿那份 pass。"""
        old = self.store.record("旧稿正文" * 20)
        self.store.bind_acceptance(_accept("pass", "旧稿正文" * 20))
        new = self.store.record("新稿正文" * 5)          # 未验收
        cur, cand = self.store.get(old.version_id), self.store.get(new.version_id)
        self.assertEqual(cand.acceptance_overall(), "", "缺验收必须表现为未知")
        self.assertFalse(cand.acceptance_for_this_body())
        improved, why = rq.compare_versions(
            "旧稿正文" * 20, "新稿正文" * 5,
            cur_acceptance=cur.acceptance, cand_acceptance=cand.acceptance)
        self.assertFalse(improved, f"未验收的候选稿不应胜出：{why}")

    def test_same_body_different_evidence_is_different_identity(self):
        body = "同一份正文。"
        a = self.store.record(body, sources_fingerprint="src-A", rules_fingerprint="r1")
        b = self.store.record(body, sources_fingerprint="src-B", rules_fingerprint="r1")
        self.assertEqual(a.version_id, b.version_id, "正文相同 → 正文 hash 相同")
        self.assertNotEqual(a.identity_id(), b.identity_id(), "来源变化 → 身份应不同")


class TestAdoptionAndLateResults(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="wm_ver_"))
        self.store = rv.VersionStore(self.ws, "task-2")

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_adopt_is_single_and_atomic(self):
        v1 = self.store.record("第一版")
        v2 = self.store.record("第二版")
        self.store.adopt(v1, reason="首次产出")
        self.assertEqual(self.store.adopted().body, "第一版")
        self.store.adopt(v2, reason="纠错稿更优")
        self.assertEqual(self.store.adopted().body, "第二版", "选中版本应被原子替换")
        self.assertFalse(self.store.get(v1.version_id).adopted)

    def test_late_result_does_not_overwrite_selected(self):
        v1 = self.store.record("已交付的稿")
        self.store.adopt(v1, reason="已交付")
        self.store.reject_late("迟到 worker 结果")       # 迟到结果只记录，不改选中
        self.assertEqual(self.store.adopted().body, "已交付的稿")
        data = (self.ws / rv.VERSIONS_FILE).read_text(encoding="utf-8")
        self.assertIn("迟到 worker 结果", data, "迟到结果应留痕")


class TestDeliveryGuard(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="wm_ver_"))
        self.store = rv.VersionStore(self.ws, "task-3")

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_assembled_body_mismatch_becomes_draft(self):
        """装配/链接重写后正文变了的，只能给"未验收草稿"。"""
        body = "研究正文（验收对象）"
        v = self.store.record(body)
        self.store.bind_acceptance(_accept("pass", body))
        v = self.store.get(v.version_id)
        ok, _ = rv.verify_delivery(v, body)
        self.assertTrue(ok, "同一正文应通过交付校验")
        ok2, why = rv.verify_delivery(v, body + "\n\n---\n\n交付说明（装配后）")
        self.assertFalse(ok2, "装配后正文与验收对象不一致 → 必须拒绝正式交付")
        self.assertIn("不一致", why)

    def test_version_without_matching_acceptance_is_unknown(self):
        v = self.store.record("没有任何验收的正文")
        ok, why = rv.verify_delivery(v, "没有任何验收的正文")
        self.assertFalse(ok)
        self.assertIn("未知", why)


class TestExportManifest(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="wm_ver_"))
        self.store = rv.VersionStore(self.ws, "task-4")

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_each_export_has_own_hash_bound_to_same_version(self):
        body = "报告正文"
        v = self.store.record(body)
        self.store.bind_acceptance(_accept("pass", body))
        v = self.store.get(v.version_id)
        md = self.ws / "report.md"
        pdf = self.ws / "report.pdf"
        md.write_text(body, encoding="utf-8")
        pdf.write_bytes(b"%PDF-1.4 fake bytes")          # 字节必然不同于正文
        m = rv.build_export_manifest(v, {"markdown": md, "pdf": pdf})
        self.assertEqual(m["report_version_id"], v.identity_id())
        self.assertEqual(m["body_sha256"], v.version_id)
        self.assertNotEqual(m["files"]["pdf"]["file_sha256"], m["body_sha256"],
                            "PDF 字节 hash 不应等于正文 hash")
        self.assertNotEqual(m["files"]["markdown"]["file_sha256"],
                            m["files"]["pdf"]["file_sha256"])
        self.assertTrue(m["files"]["pdf"]["exists"])
        self.assertEqual(m["renderer_version"], rv.RENDERER_VERSION)
        self.assertTrue(m["template_version"], "模板版本应记录")


class _FakeHandler:
    """最小的 Handler 替身：只记录状态码/响应头/响应体，不开 socket。"""

    def __init__(self):
        self.status = None
        self.headers: list = []
        self.body = b""
        self.json = None
        self.wfile = self

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.headers.append((k, v))

    def end_headers(self):
        pass

    def write(self, data):
        self.body += data

    def _json(self, obj, code=200):
        self.status = code
        self.json = obj
        return None


class TestExportManifestWiring(unittest.TestCase):
    """导出清单走到**真实路由**：逐格式累加、绑定同一选中版本、草稿判定。"""

    def setUp(self):
        import web_ui

        self.web_ui = web_ui
        self.ws = Path(tempfile.mkdtemp(prefix="wm_exp_"))
        self.tid = "task-exp"
        self.body = "# 报告\n\n正文内容\n"
        self.store = rv.VersionStore(self.ws, self.tid)
        v = self.store.record(self.body)
        self.store.bind_acceptance(_accept("pass", self.body))
        v = self.store.get(v.version_id)
        self.store.adopt(v, reason="产出版本被选中")
        self.version_id = v.identity_id()
        # 正常任务在收尾/修订时会记录交付正文；清单的 aligned/同版绑定就靠这条记录。
        # （"没有交付记录"是另一条用例专门覆盖的边界。）
        self.store.record_delivery(self.body, accepted_body=self.body,
                                   ok=True, reason="")
        self._orig_ws = web_ui.task_workspace
        self._orig_data = web_ui._get_task_report_data
        web_ui.task_workspace = lambda tid: self.ws
        web_ui._get_task_report_data = lambda tid: {
            "report": self.body, "goal": "导出测试"}

    def tearDown(self):
        self.web_ui.task_workspace = self._orig_ws
        self.web_ui._get_task_report_data = self._orig_data
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_manifest_accumulates_formats_under_one_version(self):
        pdf = b"%PDF-1.4 not the body"
        m1 = self.web_ui._write_export_manifest(self.tid, self.body, pdf)
        self.assertEqual(m1["report_version_id"], self.version_id)
        self.assertIn("pdf", m1["files"])
        self.assertEqual(m1["files"]["markdown"]["sha256"], rv.body_hash(self.body))
        self.assertNotEqual(m1["files"]["pdf"]["sha256"], m1["files"]["markdown"]["sha256"])
        # 再导出 Markdown：不能丢掉已登记的 PDF 条目，且仍绑定同一版本
        m2 = self.web_ui._write_export_manifest(self.tid, self.body, b"")
        self.assertEqual(m2["report_version_id"], self.version_id)
        self.assertIn("pdf", m2["files"], "后一次导出不得覆盖已登记格式")
        self.assertEqual(m2["files"]["pdf"]["sha256"], m1["files"]["pdf"]["sha256"])
        self.assertEqual(m2["files"]["markdown"]["sha256"], m1["files"]["markdown"]["sha256"])
        self.assertFalse(m2["draft"])
        self.assertTrue(m2["aligned"])

    def test_no_delivery_record_means_draft(self):
        """没有交付记录时**无法核对导出字节**：不得判为已验证（B 批）。

        此前那种情况会落到"比较交付文档 hash 与研究正文 hash"的兜底分支——两者本就不是
        同一份字节，判定结构性错误。现在如实记 draft 并说明原因。
        """
        data = self.store._load()
        data["deliveries"] = []          # 只清交付记录，保留版本与选中
        self.store._save(data)
        m = self.web_ui._write_export_manifest(self.tid, self.body, b"")
        self.assertIsNone(m["aligned"], m)
        self.assertTrue(m["draft"])
        self.assertIn("交付记录", m["draft_reason"])

    def test_markdown_route_serves_bytes_with_version_headers(self):
        h = _FakeHandler()
        self.web_ui._get_task_markdown(h, f"/api/task/{self.tid}/report.md")
        self.assertEqual(h.status, 200)
        self.assertEqual(h.body, self.body.encode("utf-8"), "导出字节=选中版本正文")
        got = dict(h.headers)
        self.assertEqual(got["X-Report-Version-Id"], self.version_id)
        self.assertEqual(got["X-Report-Body-Sha256"], rv.body_hash(self.body))
        self.assertEqual(got["X-Report-Draft"], "0")
        import hashlib
        self.assertEqual(hashlib.sha256(h.body).hexdigest(),
                         got["X-Report-Body-Sha256"],
                         "响应头声明的 hash 必须等于实际送达字节的 hash")

    def test_route_registered(self):
        src = (ROOT / "web_ui.py").read_text(encoding="utf-8")
        self.assertIn('endswith("/report.md")', src, "Markdown 导出路由未注册")
        self.assertIn("_get_task_markdown", src)

    def test_unaccepted_body_exports_as_draft(self):
        other = self.ws / "other"
        other.mkdir(exist_ok=True)
        old_ws = self.web_ui.task_workspace
        self.web_ui.task_workspace = lambda tid: other
        try:
            m = self.web_ui._write_export_manifest("task-draft", "# 未验收稿\n")
            st = rv.VersionStore(other, "task-draft")
            self.assertIsNotNone(st.find_by_body("# 未验收稿\n"),
                                 "导出也要把正文登记成可追溯版本")
            self.assertIsNone(st.adopted(), "未经采纳/验收的正文不算选中版本")
        finally:
            self.web_ui.task_workspace = old_ws
        self.assertTrue(m["draft"], "无验收的正文只能作未验收草稿导出")
        self.assertFalse(m["aligned"])

    def test_manifest_of_other_version_is_not_merged(self):
        self.web_ui._write_export_manifest(self.tid, self.body, b"%PDF-1.4 one")
        # 装配后正文变了（版本仍是已选中那版，但送达字节不同）：不得继承旧 PDF 条目
        self.body = "# 报告\n\n改写后的正文\n"
        m = self.web_ui._write_export_manifest(self.tid, self.body, b"")
        self.assertNotIn("pdf", m["files"], "送达正文变了不得沿用旧格式条目")
        self.assertTrue(m["draft"], "送达正文与选中版本不一致 → 草稿")
        self.assertFalse(m["aligned"])

    def test_manifest_binds_final_delivery_content_hash(self):
        """收尾记录的最终交付正文 hash 要与导出字节对照（研究正文≠交付文档）。"""
        delivered = self.body + "\n\n---\n\n交付说明（装配后）"
        self.store.record_delivery(delivered, accepted_body=self.body, ok=True)
        m = self.web_ui._write_export_manifest(self.tid, delivered, b"%PDF-1.4 x")
        self.assertEqual(m["final_content_sha256"], rv.body_hash(delivered))
        self.assertTrue(m["final_content_matches"], "导出字节就是那份交付")
        self.assertEqual(m["delivered_body_sha256"], m["final_content_sha256"])
        # 导出的是另一份字节 → 语义检查必须报不一致，而不是只比正文 hash
        m2 = self.web_ui._write_export_manifest(self.tid, self.body, b"")
        self.assertFalse(m2["final_content_matches"])


class TestDeliveryRecord(unittest.TestCase):
    """最终交付正文的记录：与研究正文分开、按字节去重。"""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="wm_del_"))
        self.store = rv.VersionStore(self.ws, "task-del")

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_delivery_hash_is_not_the_research_body_hash(self):
        body = "研究正文"
        v = self.store.record(body)
        self.store.adopt(v, reason="交付选中")
        assembled = body + "\n\n---\n\n交付说明"
        e = self.store.record_delivery(assembled, accepted_body=body, ok=True)
        self.assertEqual(e["delivered_sha256"], rv.body_hash(assembled))
        self.assertEqual(e["accepted_body_sha256"], rv.body_hash(body))
        self.assertNotEqual(e["delivered_sha256"], e["accepted_body_sha256"])
        self.assertTrue(e["aligned"])
        self.assertEqual(e["report_version_id"], v.identity_id())

    def test_same_delivery_recorded_once(self):
        v = self.store.record("正文")
        self.store.adopt(v, reason="选中")
        for _ in range(3):
            self.store.record_delivery("正文+说明", accepted_body="正文", ok=True)
        self.assertEqual(len(self.store.deliveries()), 1, "同一份交付不应重复计数")

    def test_mismatched_delivery_is_flagged_not_aligned(self):
        v = self.store.record("验收过的正文")
        self.store.bind_acceptance(_accept("pass", "验收过的正文"))
        self.store.adopt(self.store.get(v.version_id), reason="选中")
        e = self.store.record_delivery("另一个正文", accepted_body="验收过的正文")
        self.assertFalse(e["ok"])
        self.assertTrue(e["aligned"], "aligned 只说明交付正文含的是选中版本的研究正文")


if __name__ == "__main__":
    unittest.main()
