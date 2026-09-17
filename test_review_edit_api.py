# -*- coding: utf-8 -*-
"""S2：底稿接口与"复核改版重验"接口的回归。

两条不变量：
- `GET /api/task/<id>/working_paper` 只读任务工作区的底稿；没有底稿就 404（不编空底稿）。
- `POST /api/task/<id>/review/edit` 的修订版是**新版本**（parent 指向原版），
  **旧验收与旧批准不迁移**：新版本默认没有对应自身的验收，交付状态只能是"未验收草稿"，
  必须重验通过；终态任务（CANCELLED/FAILED）不允许改写正文。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import workspace as ws_mod  # noqa: E402


class _Handler:
    """最小 handler 替身：只实现 `_json`（收集响应）与 `_client_ip`。"""

    def __init__(self, path: str):
        self.path = path
        self.responses: list[tuple] = []

    def _json(self, payload, status=200):
        self.responses.append((payload, status))
        return payload

    def _client_ip(self):
        return "127.0.0.1"

    @property
    def last(self):
        return self.responses[-1] if self.responses else (None, None)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_s2_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        self.addCleanup(setattr, ws_mod, "WORKSPACE_ROOT", self._old)


class TestWorkingPaperEndpoint(_Base):
    def test_404_without_paper(self):
        import web_ui
        h = _Handler("/api/task/no-paper/working_paper")
        web_ui._get_task_working_paper(h, h.path)
        payload, status = h.last
        self.assertEqual(status, 404, payload)

    def test_returns_saved_paper(self):
        import web_ui
        tid = "s2-wp"
        # 端点按 `task_project_dir(tid)`（无 project）定位：写到同一个地方
        proj = ws_mod.task_project_dir(tid)
        proj.mkdir(parents=True, exist_ok=True)
        paper = {"rows": [{"fact_id": "fact-1", "entity": "示例公司"}],
                 "derived": [], "gaps": [], "problems": [], "ok": True}
        (proj / "working_paper.json").write_text(
            json.dumps(paper, ensure_ascii=False), encoding="utf-8")
        h = _Handler(f"/api/task/{tid}/working_paper")
        web_ui._get_task_working_paper(h, h.path)
        payload, status = h.last
        self.assertEqual(status, 200)
        self.assertEqual(payload["rows"][0]["fact_id"], "fact-1")


class TestReviewEditEndpoint(_Base):
    def setUp(self):
        super().setUp()
        # 端点入口先查任务是否存在；测试环境没有任务台账 → 打桩放行，
        # 这样测的是端点自身逻辑（版本/验收迁移、终态拦截）。
        import web_ui
        pat = mock.patch.object(web_ui, "_task_exists", lambda tid: True)
        pat.start()
        self.addCleanup(pat.stop)

    def _seed(self, tid: str, body: str = "原正文：贵州茅台2024年营收 1741 亿元。"):
        from report_version import VersionStore
        ws = ws_mod.task_workspace(tid, "default")
        ws.mkdir(parents=True, exist_ok=True)
        store = VersionStore(ws, tid)
        v = store.record(body)
        store.bind_acceptance({"overall": "pass", "gaps": [],
                               "report_sha256": v.version_id})
        store.adopt(store.get(v.version_id) or v, reason="初版")
        return store, v

    def test_edit_creates_new_version_without_migrating_acceptance(self):
        import task_state
        import web_ui
        tid = "s2-edit"
        store, old = self._seed(tid)
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": "研究贵州茅台2024年营收"}):
            h = _Handler(f"/api/task/{tid}/review/edit")
            web_ui._post_task_review_edit(
                h, h.path,
                {"body": "修订正文：贵州茅台2024年营收 1741 亿元（人工核对来源后修订）。"},
                {"user": "admin", "role": "admin"})
        payload, status = h.last
        self.assertEqual(status, 200, payload)
        self.assertNotEqual(payload["version_id"], old.version_id, "必须落成新版本")
        self.assertEqual(payload["parent_version_id"], old.version_id)
        # 旧验收不迁移：新版本对外状态是"未验收草稿"（needs_reverify）
        store2 = store.__class__(ws_mod.task_workspace(tid, "default"), tid)
        new = store2.adopted()
        self.assertEqual(new.version_id, payload["version_id"])
        self.assertTrue(payload.get("needs_reverify"),
                        "新版本未取得针对自身的验收通过 → 必须标需重验")

    def test_find_replace_requires_match(self):
        import task_state
        import web_ui
        tid = "s2-edit-nomatch"
        self._seed(tid)
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": "目标"}):
            h = _Handler(f"/api/task/{tid}/review/edit")
            web_ui._post_task_review_edit(h, h.path,
                                         {"find": "不存在的片段", "replace": "x"},
                                         {"user": "admin", "role": "admin"})
        _, status = h.last
        self.assertEqual(status, 400, "find 不命中时不得改动任何东西")

    def test_identical_body_is_rejected(self):
        import task_state
        import web_ui
        tid = "s2-edit-same"
        store, old = self._seed(tid)
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": "目标"}):
            h = _Handler(f"/api/task/{tid}/review/edit")
            web_ui._post_task_review_edit(h, h.path, {"body": old.body},
                                         {"user": "admin", "role": "admin"})
        _, status = h.last
        self.assertEqual(status, 400, "与当前版本一致就不该新建版本")

    def test_terminal_task_cannot_be_edited(self):
        import task_state
        import web_ui
        tid = "s2-edit-cancelled"
        self._seed(tid)
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "CANCELLED", "goal": "目标"}):
            h = _Handler(f"/api/task/{tid}/review/edit")
            web_ui._post_task_review_edit(h, h.path, {"body": "新正文"},
                                         {"user": "admin", "role": "admin"})
        _, status = h.last
        self.assertEqual(status, 409, "终态任务不得改写正文")

    def test_revision_reaches_delivered_report(self):
        """改版必须同步进**交付正文**（页面/导出/PDF 读的就是它）。

        实机反例（任务 ui-01efce721a）：接口返回 ok、版本库也有新版本且绑了新验收，
        但导出的 Markdown 字节与改版前完全一样——修订只落在版本库里，
        用户改完看到的还是旧文。
        """
        import task_state
        import web_ui
        tid = "s2-edit-delivery"
        store, old = self._seed(tid)
        delivered = "## 交付说明\n\n步骤 5/5\n\n---\n\n" + old.body
        seen = {}

        def fake_update(t, report, db_path=None):
            seen["tid"], seen["report"] = t, report
            return True

        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": "目标"}), \
                mock.patch.object(task_state, "update_report", side_effect=fake_update), \
                mock.patch.object(web_ui, "_get_task_report_data",
                                  return_value={"report": delivered}):
            h = _Handler(f"/api/task/{tid}/review/edit")
            web_ui._post_task_review_edit(
                h, h.path, {"find": "1741 亿元", "replace": "1741.44 亿元"},
                {"user": "admin", "role": "admin"})
        payload, status = h.last
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("delivery_updated"),
                        "修订没进交付正文 → 用户看到的仍是旧文")
        self.assertEqual(seen.get("tid"), tid)
        self.assertIn("1741.44 亿元", seen["report"])
        self.assertIn("## 交付说明", seen["report"], "交付说明不能被改版丢掉")
        self.assertNotIn("1741 亿元。", seen["report"], "旧数字不得留在交付正文里")

    def test_unmappable_revision_does_not_touch_delivery(self):
        """交付正文里找不到被修订的那版正文时**不猜**：如实返回未同步。"""
        import task_state
        import web_ui
        tid = "s2-edit-unmappable"
        self._seed(tid)
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": "目标"}), \
                mock.patch.object(task_state, "update_report") as m, \
                mock.patch.object(web_ui, "_get_task_report_data",
                                  return_value={"report": "另一份完全无关的交付正文"}):
            h = _Handler(f"/api/task/{tid}/review/edit")
            web_ui._post_task_review_edit(h, h.path, {"body": "修订后的正文"},
                                         {"user": "admin", "role": "admin"})
        payload, status = h.last
        self.assertEqual(status, 200, payload)
        self.assertFalse(payload.get("delivery_updated"))
        m.assert_not_called()


class TestWorkingPaperExportAndDownload(_Base):
    """底稿要出现在导出清单里，并且能下载（缺则 404，不编空底稿）。"""

    def _seed_paper(self, tid: str, *, ok: bool = False) -> Path:
        proj = ws_mod.task_project_dir(tid)
        proj.mkdir(parents=True, exist_ok=True)
        paper = {
            "ok": ok,
            "request": {"company": "示例公司", "as_of": "2025-04-30"},
            "rows": [{"fact_id": "fact-1", "metric": "revenue", "value": 1380.0,
                      "unit": "亿元", "entity": "示例公司"}],
            "derived": [{"fact_id": "fact-d1", "metric": "revenue_yoy", "value": 15.0,
                         "formula": "(1380 - 1200) / 1200 * 100",
                         "derived_from": ["fact-1"]}],
            "gaps": [{"kind": "fact", "detail": "缺少必需事实：经营活动现金流净额 2023年"}],
            "problems": [],
        }
        (proj / "working_paper.json").write_text(
            json.dumps(paper, ensure_ascii=False), encoding="utf-8")
        (proj / "working_paper.csv").write_text(
            "fact_id,指标\nfact-1,营业收入\n", encoding="utf-8")
        return proj

    def test_manifest_registers_paper_with_own_hashes(self):
        import hashlib
        import web_ui
        tid = "s2-dl"
        proj = self._seed_paper(tid)
        manifest = web_ui._write_export_manifest(tid, "研究正文", b"%PDF-1.4 stub")
        files = manifest["files"]
        self.assertIn("working_paper_json", files)
        self.assertIn("working_paper_csv", files)
        want = hashlib.sha256((proj / "working_paper.json").read_bytes()).hexdigest()
        self.assertEqual(files["working_paper_json"]["sha256"], want,
                         "底稿要按自己的字节登记 hash")
        meta = manifest["working_paper"]
        self.assertEqual(meta["goal_met"], False)
        self.assertEqual(meta["rows"], 1)
        self.assertEqual(meta["gaps"], 1)

    def test_download_serves_csv_and_404_without_paper(self):
        import web_ui

        class _H(_Handler):
            def __init__(self, path):
                super().__init__(path)
                self.headers_out: list[tuple] = []
                self.written = b""
                self.status = None

            def send_response(self, code):
                self.status = code

            def send_header(self, k, v):
                self.headers_out.append((k, v))

            def end_headers(self):
                pass

            @property
            def wfile(self):
                outer = self

                class _W:
                    def write(self, data):
                        outer.written += data
                return _W()

        tid = "s2-dl-2"
        self._seed_paper(tid)
        h = _H(f"/api/task/{tid}/working_paper.csv")
        web_ui._get_task_working_paper_file(h, h.path)
        self.assertEqual(h.status, 200)
        self.assertIn(b"fact-1", h.written)
        self.assertTrue(any(k == "Content-Type" for k, _ in h.headers_out))

        h2 = _H("/api/task/no-paper-here/working_paper.json")
        web_ui._get_task_working_paper_file(h2, h2.path)
        _, status = h2.last
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
