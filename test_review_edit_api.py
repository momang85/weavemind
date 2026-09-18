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
        # B 批起修订会同步"交付投影"（正文 + 验收摘要 + 状态）。这些用例考的是端点
        # 自身的版本/验收语义，投影写入在专门的矩阵用例里单独断言 → 这里默认打桩成功。
        import task_state
        self.projected: list = []
        pat2 = mock.patch.object(
            task_state, "update_delivery_projection",
            side_effect=lambda tid, **kw: (self.projected.append((tid, kw)), True)[1])
        pat2.start()
        self.addCleanup(pat2.stop)

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
        # B 批：响应要带齐"同版"所需的标识与交付状态，调用方不必再取
        self.assertEqual(payload["report_version_id"], new.identity_id())
        self.assertIn("delivery", payload)
        self.assertIn(payload["delivery"]["status"], ("draft", "unknown"),
                      payload["delivery"])
        self.assertFalse(payload["delivery"]["verified"])

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

        B 批起不再"手工拼回旧交付"，而是走**共享装配路径**重装交付正文：
        交付说明保留、正文换成新版、状态由唯一谓词判定。
        """
        import task_state
        import web_ui
        tid = "s2-edit-delivery"
        store, old = self._seed(tid)
        delivered = "## 交付说明\n\n步骤 5/5\n\n---\n\n" + old.body

        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": "目标"}), \
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
        self.assertEqual(len(self.projected), 1, "必须同步交付投影（正文+验收+状态）")
        _tid, kw = self.projected[0]
        self.assertEqual(_tid, tid)
        self.assertIn("1741.44 亿元", kw["report"])
        self.assertIn("## 交付说明", kw["report"], "交付说明不能被改版丢掉")
        self.assertNotIn("1741 亿元。", kw["report"], "旧数字不得留在交付正文里")
        # 交付正文与记录的交付 hash 必须一致（manifest 的 aligned 就靠它）
        from report_version import body_hash
        self.assertEqual(body_hash(kw["report"]),
                         store.deliveries()[-1]["delivered_sha256"])

    def test_unmappable_wrapper_is_derived_and_flagged(self):
        """交付正文里找不到被修订的那版正文时**不猜**：交付说明按推导并显式标注。

        B 批语义变化：修订**必须**成为新交付（否则页面/导出仍是旧文），所以不再
        "不动交付"；但推导出来的交付说明要写明来源，供人工确认。
        """
        import task_state
        import web_ui
        tid = "s2-edit-unmappable"
        self._seed(tid)
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": "目标"}), \
                mock.patch.object(web_ui, "_get_task_report_data",
                                  return_value={"report": "另一份完全无关的交付正文"}):
            h = _Handler(f"/api/task/{tid}/review/edit")
            web_ui._post_task_review_edit(h, h.path, {"body": "修订后的正文"},
                                         {"user": "admin", "role": "admin"})
        payload, status = h.last
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload.get("delivery_updated"))
        self.assertEqual(payload.get("wrapper_source"), "derived")
        _tid, kw = self.projected[0]
        self.assertIn("修订后的正文", kw["report"])
        self.assertIn("交付说明由旧交付正文推导", kw["report"],
                      "推导出来的交付说明要显式标注，不能冒充原始交付说明")


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


class TestRevisionSameVersionMatrix(_Base):
    """B 批：修订后**任务页 / 验收详情 / manifest / 导出**必须指向同一选定版本。

    四个用例（架构指令的最低矩阵）：旧 fail→新 pass、旧 pass→新 fail、验收异常、交付更新失败。
    每个用例都核对面：`_get_task_page` 的 delivery/acceptance、`_get_task_acceptance`、
    `_write_export_manifest`、`/report.md` 的版本与草稿响应头。
    """

    def setUp(self):
        super().setUp()
        import web_ui
        pat = mock.patch.object(web_ui, "_task_exists", lambda tid: True)
        pat.start()
        self.addCleanup(pat.stop)
        import task_state
        self.projected: list = []
        pat2 = mock.patch.object(
            task_state, "update_delivery_projection",
            side_effect=lambda tid, **kw: (self.projected.append((tid, kw)), True)[1])
        pat2.start()
        self.addCleanup(pat2.stop)

    # ── 夹具 ────────────────────────────────────────────────

    def _seed(self, tid: str, overall: str, body: str = "原正文：营收 1741 亿元。") -> str:
        """播一个**已交付**的任务：版本 + 绑定验收 + 选中 + 交付记录 + 评审事实。"""
        from report_version import VersionStore, body_hash
        ws = ws_mod.task_workspace(tid, "default")
        ws.mkdir(parents=True, exist_ok=True)
        store = VersionStore(ws, tid)
        v = store.record(body, sources_fingerprint="src-A")
        store.bind_acceptance({"overall": overall, "gaps": [],
                               "report_sha256": v.version_id,
                               "rules_version": "2026.09.12",
                               "rules_fingerprint": "fp"}, sources_fingerprint="src-A")
        adopted = store.get(v.version_id)
        store.adopt(adopted, reason="初版")
        delivered = "## 交付说明\n\n步骤 5/5\n\n---\n\n" + body
        store.record_delivery(delivered, accepted_body=body,
                              ok=(overall == "pass"), reason="")
        (ws / "delivery_wrapper.md").write_text("## 交付说明\n\n步骤 5/5",
                                                encoding="utf-8")
        (ws / "review_state.json").write_text(json.dumps(
            {"verdict": "PASS", "label": "PASS", "report_version_id": adopted.identity_id(),
             "required": False}), encoding="utf-8")
        return delivered

    def _revise(self, tid: str, delivered: str, verdict, *, goal="研究贵州茅台2024年营收",
                accept_exc=None):
        """走真实端点做一次修订；返回 (payload, status)。"""
        import acceptance_checker
        import task_state
        import web_ui

        def _fake_accept(*a, **kw):
            if accept_exc:
                raise accept_exc
            body = a[2] if len(a) > 2 else kw.get("report_text") or kw.get("body")
            out = dict(verdict or {})
            if out.get("overall"):
                from report_version import body_hash
                out.setdefault("report_sha256", body_hash(str(body)))
            return out

        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": goal}), \
                mock.patch.object(web_ui, "_get_task_report_data",
                                  return_value={"report": delivered, "goal": goal}), \
                mock.patch.object(acceptance_checker, "run_acceptance",
                                  side_effect=_fake_accept):
            h = _Handler(f"/api/task/{tid}/review/edit")
            web_ui._post_task_review_edit(
                h, h.path, {"find": "1741 亿元", "replace": "1741.44 亿元"},
                {"user": "admin", "role": "admin"})
        return h.last

    def _surfaces(self, tid: str, delivered: str):
        """四个面各取一次读数（任务页 / 验收详情 / manifest / 导出头）。

        `delivered`：页面当前送达的交付正文（投影成功时是新版，投影失败时仍是旧版）。
        """
        import task_state
        import web_ui
        from test_report_version import _FakeHandler

        row = {"status": "SUCCESS", "report": delivered, "goal": "目标",
               "steps": [], "logs": [], "acceptance": {}}
        # 两种绑定都要替换：web_ui 顶层是 `from workspace import task_workspace`（模块属性），
        # 而页面/端点内部还有调用时的 `from workspace import ...`（读 workspace 模块属性）。
        _real_ws = ws_mod.task_workspace           # 先抓住原函数，避免打桩后自调用
        ws_patch = lambda t, project=None: _real_ws(t, project or "default")  # noqa: E731
        page_h = _Handler(f"/api/task/{tid}")
        with mock.patch.object(task_state, "merge_projection",
                               lambda t, **kw: dict(row)),                 mock.patch.object(web_ui, "task_workspace", ws_patch),                 mock.patch.object(ws_mod, "task_workspace", ws_patch):
            web_ui._get_task_page(page_h, f"/task/{tid}")
        page = (page_h.last[0] or {})

        acc_h = _Handler(f"/api/task/{tid}/acceptance")
        with mock.patch.object(web_ui, "task_workspace", ws_patch),                 mock.patch.object(ws_mod, "task_workspace", ws_patch):
            web_ui._get_task_acceptance(acc_h, acc_h.path)
        acc = (acc_h.last[0] or {})

        with mock.patch.object(web_ui, "task_workspace", ws_patch),                 mock.patch.object(ws_mod, "task_workspace", ws_patch):
            manifest = web_ui._write_export_manifest(tid, delivered, b"%PDF-1.4 x")
            md_h = _FakeHandler()
            with mock.patch.object(web_ui, "_get_task_report_data",
                                   lambda t: {"report": delivered, "goal": "目标"}):
                web_ui._get_task_markdown(md_h, f"/api/task/{tid}/report.md")
        headers = dict(md_h.headers)
        return page, acc, manifest, headers

    def _assert_same_version(self, page, acc, manifest, headers, *, status,
                             reason_kw="", acc_bound=True):
        ver = manifest["report_version_id"]
        self.assertTrue(ver, manifest)
        self.assertEqual(str(page.get("delivery", {}).get("version_id") or ""), ver,
                         "任务页的交付版本必须与 manifest 同一版")
        if acc_bound:
            self.assertEqual(str(acc.get("report_version_id") or ""), ver,
                             "验收详情必须绑定同一版")
        else:
            # 该版本没有验收（如验收器异常）：验收详情必须是明确的"没有"，
            # 不得拿旧版本的结论冒充（旧结论属于别的正文 hash）。
            self.assertFalse(acc.get("report_version_id"),
                             f"不得冒充其他版本的验收：{acc}")
            self.assertIn("error", acc, acc)
        self.assertEqual(headers.get("X-Report-Version-Id"), ver,
                         "导出头必须绑定同一版")
        self.assertEqual(str(page.get("delivery", {}).get("status") or ""), status,
                         f"任务页 delivery={page.get('delivery')!r} status={page.get('status')!r}")
        self.assertEqual(str(manifest["status"]), status, manifest.get("draft_reason"))
        self.assertEqual(headers.get("X-Report-Draft"),
                         "1" if status != "verified" else "0")
        if reason_kw:
            self.assertIn(reason_kw, str(manifest.get("draft_reason") or ""),
                          manifest.get("draft_reason"))

    # ── 四例 ────────────────────────────────────────────────

    def test_fail_to_pass_points_everywhere_at_new_version(self):
        """旧 fail → 新 pass：四面都指向新版本；个人模式（评审非必需）且硬约束满足 → verified。"""
        tid = "b-f2p"
        delivered = self._seed(tid, overall="fail")
        payload, http = self._revise(tid, delivered, {"overall": "pass", "gaps": []})
        self.assertEqual(http, 200, payload)
        self.assertEqual(payload["delivery"]["status"], "verified", payload["delivery"])
        page, acc, manifest, headers = self._surfaces(tid, self.projected[-1][1]["report"])
        self._assert_same_version(page, acc, manifest, headers, status="verified")
        self.assertEqual(acc.get("overall"), "pass")
        self.assertFalse(manifest["draft"])
        self.assertTrue(manifest["aligned"])

    def test_pass_to_fail_points_everywhere_at_draft(self):
        """旧 pass → 新 fail：四面都变 draft + 新失败理由，任何一面不得残留旧 pass。"""
        tid = "b-p2f"
        delivered = self._seed(tid, overall="pass")
        payload, http = self._revise(tid, delivered,
                                     {"overall": "fail", "gaps": ["缺来源"]})
        self.assertEqual(http, 200, payload)
        self.assertIn(payload["delivery"]["status"], ("draft", "unknown"))
        page, acc, manifest, headers = self._surfaces(tid, self.projected[-1][1]["report"])
        self._assert_same_version(page, acc, manifest, headers,
                                  status=payload["delivery"]["status"])
        self.assertEqual(acc.get("overall"), "fail", acc)
        self.assertTrue(manifest["draft"])
        self.assertIn("验收未通过", str(manifest.get("draft_reason") or ""))
        self.assertIn("验收未通过", str(page.get("delivery", {}).get("draft_reason") or ""))
        self.assertNotEqual(str(page.get("delivery", {}).get("version_id") or ""), "",
                            "必须已经切到新版本")
        self.assertFalse(page.get("delivery", {}).get("verified"))

    def test_acceptance_exception_is_draft_everywhere(self):
        """验收异常：200 但状态 draft/未知，四面一致，绝不 verified。"""
        tid = "b-exc"
        delivered = self._seed(tid, overall="pass")
        payload, http = self._revise(tid, delivered, None,
                                     accept_exc=RuntimeError("注入：验收器不可用"))
        self.assertEqual(http, 200, payload)
        self.assertFalse(payload["delivery"]["verified"])
        page, acc, manifest, headers = self._surfaces(tid, self.projected[-1][1]["report"])
        self._assert_same_version(page, acc, manifest, headers,
                                  status=payload["delivery"]["status"], acc_bound=False)
        self.assertFalse(page.get("delivery", {}).get("verified"))
        self.assertFalse(manifest["draft"] is False)
        self.assertEqual(acc.get("overall") or "", "", acc)

    def test_old_review_pass_does_not_migrate_to_the_new_version(self):
        """旧评审 PASS 不迁移：银行口径下修订版必须先重新评审（个人模式不受影响）。

        B 批把"PASS 属于哪一版"落成事实（`review_state.json` 记 `report_version_id`），
        因此正文一改，旧 PASS 就不再覆盖本版——这正是"旧批准不得自动复制"。
        """
        import delivery_pipeline
        import task_state
        import web_ui
        tid = "b-bank"
        delivered = self._seed(tid, overall="pass")
        # "银行口径"是**配置事实**：修订与四个面的读数都要在同一个口径下取，
        # 否则等于换了个模式去读别人的结论。
        with mock.patch.object(delivery_pipeline, "review_required", lambda: True):
            payload, http = self._revise(tid, delivered, {"overall": "pass", "gaps": []})
            self.assertEqual(http, 200, payload)
            self.assertFalse(payload["delivery"]["verified"],
                             "银行口径下修订版没有本版 PASS → 不得 verified")
            self.assertIn("评审", str(payload["delivery"]["draft_reason"]),
                          payload["delivery"])
            page, acc, manifest, headers = self._surfaces(
                tid, self.projected[-1][1]["report"])
            self._assert_same_version(page, acc, manifest, headers, status="draft",
                                      reason_kw="评审")
            self.assertFalse(page.get("delivery", {}).get("review_valid"))

    def test_projection_write_failure_is_not_masked(self):
        """交付更新失败：5xx + 明确原因，且不得声称成功（不用 HTTP 成功掩盖部分更新）。"""
        import task_state
        import web_ui
        tid = "b-proj"
        delivered = self._seed(tid, overall="pass")
        with mock.patch.object(task_state, "update_delivery_projection",
                               return_value=False):
            payload, http = self._revise(tid, delivered, {"overall": "pass", "gaps": []})
        self.assertEqual(http, 500, payload)
        self.assertIn("投影写入失败", payload.get("error") or "")
        self.assertFalse(payload.get("delivery_updated"))
        # 版本库里已经是新版本，但页面/导出仍是**旧正文** → 核对不一致 → 如实为 draft
        page, acc, manifest, headers = self._surfaces(tid, delivered)
        self.assertFalse(page.get("delivery", {}).get("verified"))
        self.assertTrue(manifest["draft"], manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
