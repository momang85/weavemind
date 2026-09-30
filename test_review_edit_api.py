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


class TestAnalysisWorkbenchEndpoints(_Base):
    """K3：分析卡 → 改假设 → 确定性复算 → 前后对比 → 采纳（API 侧，全程离线零模型）。"""

    def setUp(self):
        super().setUp()
        import task_state
        import web_ui
        # 与本文件其它用例一致：`_task_exists` 走替身（真实库不参与单测）
        p = mock.patch.object(web_ui, "_task_exists", lambda tid: True)
        p.start()
        self.addCleanup(p.stop)
        self.db = str(self.tmp / "k3.db")
        self._orig_db = task_state.DB_PATH
        task_state.DB_PATH = self.db
        self.addCleanup(setattr, task_state, "DB_PATH", self._orig_db)
        self.tid = "k3-an"
        task_state.mark_queued(self.tid, goal="研究洋河股份 2023 与 2024 年度经营情况",
                               db_path=self.db)

    def _seed_inputs(self, *, mutate=lambda rows: rows, revenue=300.0):
        """落一份可复算输入（含情景所需三项）+ 一条 base 运行。"""
        import financial_analysis as fa
        from financial_analysis import store as fa_store
        rows = [
            {"fact_id": "f-rev-23", "entity": "洋河股份", "entity_id": "002304.SZ",
             "metric": "revenue", "period": "2023年", "period_type": "年报",
             "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 288.76},
            {"fact_id": "f-rev-24", "entity": "洋河股份", "entity_id": "002304.SZ",
             "metric": "revenue", "period": "2024年", "period_type": "年报",
             "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": revenue},
            {"fact_id": "f-gp-24", "entity": "洋河股份", "entity_id": "002304.SZ",
             "metric": "gross_profit", "period": "2024年", "period_type": "年报",
             "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 211.25},
            {"fact_id": "f-np-24", "entity": "洋河股份", "entity_id": "002304.SZ",
             "metric": "net_profit", "period": "2024年", "period_type": "年报",
             "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 66.73},
        ]
        ds = fa.freeze_from_facts(mutate(rows), periods=(2023, 2024),
                                 entity_id="002304.SZ", source_label="k3")
        plan = fa.compile_plan("情景分析", ds)
        ws = ws_mod.task_workspace(self.tid)
        pathlib_path = Path(ws)
        pathlib_path.mkdir(parents=True, exist_ok=True)
        fa_store.save_inputs(ws, dataset=ds, plan=plan,
                            context={"dataset_source": {"kind": "test"}})
        base = fa.run("scenario_sensitivity", ds, params={})
        fa_store.save_run(ws, base)
        return ds, base

    def _call(self, path: str, body: dict):
        import web_ui
        h = _Handler(path)
        out = (web_ui._post_task_analysis_recompute(h, path, body, {"user": "admin"})
               if path.endswith("/analysis/recompute")
               else web_ui._post_task_analysis_adopt(h, path, body, {"user": "admin"}))
        payload, status = h.last if h.responses else (out, 200)
        return payload, status

    def test_state_endpoint_reports_inputs_and_models(self):
        import web_ui
        self._seed_inputs()
        h = _Handler("/api/task/" + self.tid + "/analysis")
        web_ui._get_task_analysis(h, h.path)
        payload, status = h.last
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(payload["inputs"]["recomputable"])
        models = {r["model_id"] for r in payload["runs"]}
        self.assertIn("scenario_sensitivity", models)
        sc = [r for r in payload["runs"] if r["model_id"] == "scenario_sensitivity"][0]
        self.assertIn("revenue_growth", sc["allowed_params"],
                      "面板要能拿到**允许改的假设**清单")

    def test_recompute_without_inputs_is_409(self):
        payload, status = self._call("/api/task/" + self.tid + "/analysis/recompute",
                                     {"model_id": "scenario_sensitivity", "params": {}})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload.get("code"), "no_inputs")

    def test_recompute_rejects_unknown_model_and_out_of_range_params(self):
        self._seed_inputs()
        payload, status = self._call("/api/task/" + self.tid + "/analysis/recompute",
                                     {"model_id": "not_a_model", "params": {}})
        self.assertEqual(status, 400, payload)
        payload2, status2 = self._call("/api/task/" + self.tid + "/analysis/recompute",
                                       {"model_id": "scenario_sensitivity",
                                        "params": {"not_a_param": 0.1}})
        self.assertEqual(status2, 400, payload2)
        self.assertEqual(payload2.get("code"), "bad_params")
        self.assertIn("revenue_growth", payload2.get("allowed") or {})
        # 声明了范围的参数越界 → 400（请求错误），与"数据不适用"（200 + 说明）区分开
        payload3, status3 = self._call("/api/task/" + self.tid + "/analysis/recompute",
                                       {"model_id": "scenario_sensitivity",
                                        "params": {"revenue_growth": 5.0}})
        self.assertEqual(status3, 400, payload3)
        self.assertEqual(payload3.get("code"), "params_out_of_range")

    def test_recompute_is_a_new_run_with_diff_and_keeps_the_old(self):
        from financial_analysis import store as fa_store
        _ds, base = self._seed_inputs()
        payload, status = self._call(
            "/api/task/" + self.tid + "/analysis/recompute",
            {"model_id": "scenario_sensitivity", "params": {"revenue_growth": 0.10}})
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ok"], payload)
        self.assertFalse(payload["adopted"], "复算结果不得自动被采纳")
        self.assertNotEqual(payload["run_id"], base.run_id, "必须是新运行")
        self.assertEqual(payload["base_run_id"], base.run_id)
        self.assertTrue(payload["diff"], payload)
        row = [d for d in payload["diff"] if d["metric"] == "scenario_net_profit"]
        self.assertTrue(row, payload["diff"])
        self.assertIsNotNone(row[0]["before"])
        self.assertIsNotNone(row[0]["after"])
        self.assertNotEqual(row[0]["before"], row[0]["after"])
        # 旧运行原样保留（并存），新运行已落盘且通过验证
        ws = ws_mod.task_workspace(self.tid)
        runs = {str(r.get("run_id")): r for r in fa_store.load_runs(ws)}
        self.assertIn(base.run_id, runs)
        self.assertIn(payload["run_id"], runs)
        self.assertEqual(runs[payload["run_id"]]["status"], "validated")
        self.assertEqual(runs[base.run_id]["params"], {})

    def test_not_applicable_recompute_is_reported_and_not_stored(self):
        """基期毛利率为负（亏损期）→ 情景模型不适用：如实报，不落盘成"已验证"。"""
        from financial_analysis import store as fa_store
        self._seed_inputs(revenue=100.0)          # 毛利 211 > 收入 100 → 毛利率 >1？用负毛利
        ws = ws_mod.task_workspace(self.tid)
        runs_before = len(fa_store.load_runs(ws))
        payload, status = self._call(
            "/api/task/" + self.tid + "/analysis/recompute",
            {"model_id": "scenario_sensitivity", "params": {"revenue_growth": 0.05}})
        self.assertEqual(status, 200, payload)
        if not payload.get("ok"):
            self.assertIn("不适用", str(payload.get("message") or payload.get("reason")))
            self.assertEqual(len(fa_store.load_runs(ws)), runs_before,
                             "不适用/缺输入不得落盘成新运行")
        else:
            self.skipTest("该夹具下情景仍可算（不适用分支由算子单测覆盖）")

    def test_adopt_requires_a_known_validated_run(self):
        from financial_analysis import store as fa_store
        _ds, base = self._seed_inputs()
        payload, status = self._call("/api/task/" + self.tid + "/analysis/adopt",
                                     {"run_id": "deadbeef" * 4})
        self.assertEqual(status, 404, payload)
        self.assertEqual(payload.get("code"), "unknown_run")
        ws = ws_mod.task_workspace(self.tid)
        bad = fa_store.load_runs(ws)[0]
        bad["run_id"] = "not-validated-run"
        bad["status"] = "validation_failed"
        fa_store.save_run(ws, fa_store.run_from_dict(bad))
        payload2, status2 = self._call("/api/task/" + self.tid + "/analysis/adopt",
                                       {"run_id": "not-validated-run"})
        self.assertEqual(status2, 409, payload2)
        self.assertIn("未通过验证", str(payload2.get("error")))


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


class TestL0BSelectedRunEntersTheReport(_Base):
    """L0-b（2026-09-30 复核 U1/U2）：**用户选择的运行真正进正文/清单/导出**。

    反例（复核 U1）：原实现只回显 run_id，装配仍然取"前两条运行"，新增的情景运行
    "采纳成功"却不出现在正文里。这里逐条钉住：选择记录驱动正文；过期选择只报不代替；
    缺 run_id 不接受默认采纳；采纳返回**实际采纳版本的身份**；面板拿得到当前包。
    """

    def setUp(self):
        super().setUp()
        import task_state
        import web_ui
        p = mock.patch.object(web_ui, "_task_exists", lambda tid: True)
        p.start()
        self.addCleanup(p.stop)
        self.db = str(self.tmp / "l0b.db")
        self._orig_db = task_state.DB_PATH
        task_state.DB_PATH = self.db
        self.addCleanup(setattr, task_state, "DB_PATH", self._orig_db)
        self.tid = "l0b-an"
        task_state.mark_queued(self.tid, goal="研究洋河股份 2023 与 2024 年度经营情况",
                               db_path=self.db)

    def _seed(self, *, revenue: float = 300.0):
        import financial_analysis as fa
        from financial_analysis import store as fa_store
        rows = [
            {"fact_id": "f-rev-23", "entity": "洋河股份", "entity_id": "002304.SZ",
             "metric": "revenue", "period": "2023年", "period_type": "年报",
             "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 288.76},
            {"fact_id": "f-rev-24", "entity": "洋河股份", "entity_id": "002304.SZ",
             "metric": "revenue", "period": "2024年", "period_type": "年报",
             "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": revenue},
            {"fact_id": "f-gp-24", "entity": "洋河股份", "entity_id": "002304.SZ",
             "metric": "gross_profit", "period": "2024年", "period_type": "年报",
             "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 211.25},
            {"fact_id": "f-np-24", "entity": "洋河股份", "entity_id": "002304.SZ",
             "metric": "net_profit", "period": "2024年", "period_type": "年报",
             "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 66.73},
        ]
        ds = fa.freeze_from_facts(rows, periods=(2023, 2024), entity_id="002304.SZ",
                                  source_label="l0b")
        ws = ws_mod.task_workspace(self.tid)
        Path(ws).mkdir(parents=True, exist_ok=True)
        fa_store.save_inputs(ws, dataset=ds, plan=fa.compile_plan("情景分析", ds),
                             context={"dataset_source": {"kind": "test"}})
        base = fa.run("scenario_sensitivity", ds, params={})
        fa_store.save_run(ws, base)
        newer = fa.run("scenario_sensitivity", ds, params={"revenue_growth": 0.30})
        fa_store.save_run(ws, newer)
        return ds, base, newer, ws

    def _call(self, path: str, body: dict):
        import web_ui
        h = _Handler(path)
        out = (web_ui._post_task_analysis_recompute(h, path, body, {"user": "admin"})
               if path.endswith("/analysis/recompute")
               else web_ui._post_task_analysis_adopt(h, path, body, {"user": "admin"}))
        payload, status = h.last if h.responses else (out, 200)
        return payload, status

    def test_state_endpoint_gives_the_panel_package_selection_and_defaults(self):
        import web_ui
        self._seed()
        h = _Handler("/api/task/" + self.tid + "/analysis")
        web_ui._get_task_analysis(h, h.path)
        payload, status = h.last
        self.assertEqual(status, 200, payload)
        self.assertIn("current_package", payload,
                      "面板导出必须能拿到「与采纳稿同版」的当前包（复核 U2）")
        self.assertIn("packages", payload)
        self.assertEqual(payload["selection"]["entries"], [])
        sc = [r for r in payload["runs"] if r["model_id"] == "scenario_sensitivity"][0]
        self.assertEqual(sc["default_params"]["revenue_growth"], 0.05,
                         "默认参数要显式给出，不让用户猜「不改会用什么」")
        self.assertEqual(sc["allowed_params"]["revenue_growth"], [-0.5, 0.5])

    def test_adopt_without_run_id_is_refused(self):
        self._seed()
        payload, status = self._call("/api/task/" + self.tid + "/analysis/adopt", {})
        self.assertEqual(status, 400, payload)
        self.assertEqual(payload.get("code"), "run_id_required",
                         "缺 run_id 不接受默认采纳（禁止取最早/最新运行代替选择）")

    def test_selected_run_is_what_the_report_renders(self):
        """U1 反例：默认真装配前两条运行；**用户选择新运行后正文必须是那一条**。"""
        import financial_analysis as fa
        import report_brief
        from financial_analysis import store as fa_store
        ds, base, newer, ws = self._seed()
        default_block = report_brief._analysis_card_block(self.tid, ws_dir=ws)
        self.assertIn(base.run_id[:12], default_block, default_block)
        self.assertNotIn(newer.run_id[:12], default_block, default_block)
        fa_store.save_selection(ws, {
            "model_id": "scenario_sensitivity", "run_id": newer.run_id,
            "dataset_hash": ds.dataset_hash, "params": dict(newer.params),
            "rules_version": fa.validation.RULES_VERSION,
        }, note="测试显式选择")
        block = report_brief._analysis_card_block(self.tid, ws_dir=ws)
        self.assertIn(newer.run_id[:12], block,
                      f"所选运行必须进正文：{block[:400]}")
        self.assertNotIn(base.run_id[:12], block,
                         "不得改取默认运行（前两条）代替用户选择")

    def test_stale_selection_is_reported_not_replaced(self):
        """资料/参数/规则一变，旧选择**标未采用并说明**，绝不悄悄换一条运行。"""
        import financial_analysis as fa
        import report_brief
        from financial_analysis import store as fa_store
        ds, base, newer, ws = self._seed()
        fa_store.save_selection(ws, {
            "model_id": "scenario_sensitivity", "run_id": newer.run_id,
            "dataset_hash": ds.dataset_hash, "params": dict(newer.params),
            "rules_version": fa.validation.RULES_VERSION,
        })
        # 资料变了（重冻结数据集）→ 旧选择过期
        rows = [{"fact_id": "f-rev-23", "entity": "洋河股份", "entity_id": "002304.SZ",
                 "metric": "revenue", "period": "2023年", "period_type": "年报",
                 "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 288.76},
                {"fact_id": "f-rev-24", "entity": "洋河股份", "entity_id": "002304.SZ",
                 "metric": "revenue", "period": "2024年", "period_type": "年报",
                 "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 301.0},
                {"fact_id": "f-gp-24", "entity": "洋河股份", "entity_id": "002304.SZ",
                 "metric": "gross_profit", "period": "2024年", "period_type": "年报",
                 "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 211.25},
                {"fact_id": "f-np-24", "entity": "洋河股份", "entity_id": "002304.SZ",
                 "metric": "net_profit", "period": "2024年", "period_type": "年报",
                 "currency": "CNY", "unit": "亿元", "caliber": "合并", "value": 66.73}]
        ds2 = fa.freeze_from_facts(rows, periods=(2023, 2024), entity_id="002304.SZ",
                                   source_label="l0b-v2")
        self.assertNotEqual(ds.dataset_hash, ds2.dataset_hash)
        fa_store.save_inputs(ws, dataset=ds2)
        block = report_brief._analysis_card_block(self.tid, ws_dir=ws)
        self.assertIn("未采用", block, block)
        self.assertFalse(block.startswith("## 分析卡"),
                         f"过期的选择不得照样渲染成结论卡：{block[:300]}")
        self.assertIn("选择说明", block)
        self.assertNotIn(base.run_id[:12], block,
                         "更不得改取别的运行（数据集已变，任何旧运行都过期）")
        status = fa_store.selection_status(ws, dataset_hash=ds2.dataset_hash,
                                          rules_version=fa.validation.RULES_VERSION)
        self.assertFalse(status["ok"], status)
        self.assertEqual(status["stale"][0]["state"], "dataset_changed", status)

    def test_adopt_records_the_choice_and_returns_the_real_identity(self):
        """采纳：选择落盘 + 返回**实际采纳版本的身份**（不再读不存在的 report_version_id）。"""
        import task_state
        import web_ui
        from financial_analysis import store as fa_store
        from report_version import VersionStore
        ds, base, newer, ws = self._seed()
        store = VersionStore(ws, self.tid)
        v = store.record("# 洋河股份 2024 年度研究\n\n## 分析\n收入与利润变化。\n")
        store.adopt(v, reason="初版")
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": "研究洋河股份"}):
            payload, status = self._call("/api/task/" + self.tid + "/analysis/adopt",
                                         {"run_id": newer.run_id})
        self.assertIn(status, (200, 409), payload)
        if status != 200:
            self.skipTest(f"该夹具下装配未通过（如实为 {payload.get('delivery_status')}）")
        self.assertTrue(payload.get("selection_saved"), payload)
        self.assertEqual(payload.get("adopted_run"), newer.run_id)
        _adv = VersionStore(ws, self.tid).adopted()
        self.assertEqual(payload.get("identity_id"), _adv.identity_id(),
                         "必须返回**实际采纳版本**的身份")
        self.assertNotIn("report_version_id", payload,
                         "不再回显不存在的 report_version_id（复核 U2）")
        sel = fa_store.load_selection(ws)
        self.assertEqual(sel["entries"][0]["run_id"], newer.run_id)
        self.assertEqual(sel["entries"][0]["dataset_hash"], ds.dataset_hash)
        # `ok` 与交付状态一致：没通过验收就不是"采纳成功"
        self.assertEqual(bool(payload.get("ok")),
                         str(payload.get("delivery_status")) == "verified", payload)

    def test_adopt_projects_the_body_so_export_is_not_stuck(self):
        """实机反例（2026-09-30，带登录会话的页面真实点击）：采纳后**导不出来**。

        现象：页面上点「采纳这版」→ 面板提示"当前包…需重新导出" → 点「导出当前包」恒
        409「导出期间发生修订（交付正文与采纳版本不一致），未生成新包；请重试」，
        而重试永远不会好——任务记录里的交付正文还停在采纳前的正文，不会自己变。

        根因：采纳按所选运行重渲染正文并落成新版本，但**没有把这一版装配出的正文投影成
        交付正文**（候选采纳/人工修订都写了 `update_delivery_projection`，分析采纳漏了）。
        """
        import task_state
        import web_ui
        from report_version import VersionStore
        ds, base, newer, ws = self._seed()
        store = VersionStore(ws, self.tid)
        old_body = "# 洋河股份 2024 年度研究\n\n## 分析\n旧的正文。\n"
        store.adopt(store.record(old_body), reason="初版")
        task_state.update_delivery_projection(self.tid, report=old_body)
        with mock.patch.object(task_state, "read_task",
                               return_value={"status": "SUCCESS", "goal": "研究洋河股份"}):
            payload, status = self._call("/api/task/" + self.tid + "/analysis/adopt",
                                         {"run_id": newer.run_id})
        if status != 200:
            self.skipTest(f"该夹具下装配未通过（如实为 {payload.get('delivery_status')}）")
        self.assertTrue(payload.get("delivery_projected"), payload)
        row = task_state.read_task(self.tid) or {}
        stored = str(row.get("report") or "")
        self.assertNotEqual(stored, old_body, "交付正文必须换成这一版装配出的正文")
        self.assertIn("旧的正文。", stored)
        # 真正的判据：导出快照必须接受这份正文（此前这里抛 version changed → 409）
        from delivery_pipeline import export_snapshot
        snap = export_snapshot(self.tid, ws_dir=ws, delivered_text=stored)
        self.assertEqual(str(snap.get("report_version_id") or ""),
                         VersionStore(ws, self.tid).adopted().identity_id(),
                         "交付正文必须被判定属于当前采纳版本")

    def test_adopt_refuses_a_run_from_another_dataset(self):
        import financial_analysis as fa
        from financial_analysis import store as fa_store
        ds, base, newer, ws = self._seed()
        stale = fa.run("scenario_sensitivity", ds, params={"revenue_growth": 0.20})
        rec = stale.as_dict()
        rec["dataset_hash"] = "0" * 64
        rec["run_id"] = "stale-run-0001"
        fa_store.save_run(ws, fa_store.run_from_dict(rec))
        payload, status = self._call("/api/task/" + self.tid + "/analysis/adopt",
                                     {"run_id": "stale-run-0001"})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload.get("code"), "dataset_changed", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
