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


if __name__ == "__main__":
    unittest.main(verbosity=2)
