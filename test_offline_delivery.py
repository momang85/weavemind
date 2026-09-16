# -*- coding: utf-8 -*-
"""M0-f ①离线完整交付 + ②离线故障注入（不联网、不调模型、不改生产配置）。

① 完整交付：材料（用户材料 + 固定搜索来源 + 报告正文）走**真实**编排链路，
   核对"页面 / 两种导出 / 导出清单"是否都指向同一个选中版本，验收与规则指纹是否一致。
② 故障注入：取消、评审不可用（个人/银行）、超时、迟到结果——都在**请求边界**打桩
   （只替身结果回包与进度上报），不替换编排逻辑；核对终态、版本归属与沉淀副作用。

打桩位置说明：worker 结果通过 `_brpop_with_deadline` 回包（替身 redis 管道），
报告落盘由替身回包时的副作用完成——与真实 worker 的落盘位置一致
（`task_reports_dir()/report.md`）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import workspace as ws_mod  # noqa: E402

# 固定免责声明：用编排器注入的**同一份**规范条款（含 AI 生成标识），
# 避免夹具自己写一份近似文本、把"声明缺失"测成"文案不同"
import report_quality as _rq  # noqa: E402

DISCLAIMER = _rq.disclaimer_clause(True, True)

# 固定来源夹具（官方/财经口径），写在 <ws>/project/ 供 _collect_sources 读取
SRC_SINA = [{
    "title": "贵州茅台2024年报：营业收入1741亿元_新浪财经",
    "url": "https://finance.sina.com.cn/a/1",
    "snippet": "贵州茅台2024年营业收入1741亿元，净利润862亿元。",
}]

# 评审替身回包（PASS：绑定计划版本；个人/银行两种模式都用它）
PASS_JSON = {"verdict": "PASS", "scores": {"goal": 9}, "suggestions": [],
             "summary": "ok"}

GOAL = (
    "根据材料分析贵州茅台2024年报核心财务数据（营收/净利润），"
    "材料给出：2024 年营业收入 1741 亿元、净利润 862 亿元。"
)

REPORT_BODY = (
    "# 贵州茅台2024年报核心财务数据\n\n"
    "## 核心指标\n\n"
    "| 指标 | 数值 | 来源 |\n|---|---|---|\n"
    "| 营业收入 | 1741亿元 | [1] |\n"
    "| 净利润 | 862亿元 | [1] |\n\n"
    "## 数据时效\n\n数据截至 2024-12-31 年度报告披露日，来源为公开财经报道，日终更新。\n\n"
    "## 参考来源\n\n1. [贵州茅台2024年报：营业收入1741亿元](https://finance.sina.com.cn/a/1)\n\n"
    # 验收器要求报告同时含"免责声明"字样与"不构成投资建议"（AI 生成标识）
    "## 免责声明\n\n" + DISCLAIMER
)


class _FakePlanner:
    """规划器替身：只替身"模型回包"这一处，`_plan` 本体（含评审、话题校验）照常跑。"""

    def __init__(self, plan_steps):
        self.plan = list(plan_steps)
        self.calls = 0

    def call(self, system, user, **kw):
        self.calls += 1
        return json.dumps({"steps": self.plan}, ensure_ascii=False)


class _FakeRedis:
    """替身 redis：lpush 记录派发映射（派发 ID → 步骤 ID），brpop 由测试分流处理。"""

    def __init__(self, run=None):
        self.run = run

    def lpush(self, key, value):
        if self.run is not None:
            self.run.record_push(value)

    def brpop(self, *a, **k):
        return None


class _OfflineRun:
    """离线跑一次真实 run()：模型回包与结果回包都在**请求边界**替身。"""

    def __init__(self, test: unittest.TestCase, *, report_body: str = REPORT_BODY,
                 steps=None):
        self.test = test
        self.tmp = Path(tempfile.mkdtemp(prefix="wm_offline_"))
        test.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self._old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(str(self.tmp))
        test.addCleanup(self._restore)
        self.report_body = report_body
        self.steps = steps or [
            {"step_id": "1", "capability": "web_search", "instruction": "查茅台年报营收数据",
             "timeout": 5},
            # 报告步骤必须是 report_generator：验收器只在报告步骤成功后触发
            {"step_id": "2", "capability": "report_generator",
             "instruction": "生成贵州茅台年报财务数据报告", "timeout": 5},
        ]
        self.step_by_key: dict[str, str] = {}
        self.redis = _FakeRedis(self)

    def _restore(self):
        ws_mod.WORKSPACE_ROOT = self._old_root

    def record_push(self, value) -> None:
        try:
            data = json.loads(value)
        except Exception:
            return
        dispatch = str(data.get("task_id") or "")
        step_id = str((data.get("context") or {}).get("step_id") or "")
        if dispatch:
            self.step_by_key[f"task_result:{dispatch}"] = step_id

    def orch(self, task_id: str, *, critic: bool = False, **overrides):
        from checkpointer import clear_checkpoint
        from test_orchestrator_v2 import make_orch

        clear_checkpoint(task_id)
        o = make_orch(**overrides)
        o._critic_enabled = critic
        o._critic_timeout = 5
        o._now_iso = lambda: "2026-09-16T00:00:00Z"
        o._find_agent = lambda cap: "fake-agent"
        o._redis = self.redis
        o._new_redis_sync = lambda: self.redis
        # 规划器只替身模型回包；模板/直出/结构化预载关掉，保证走 LLM 规划 + 评审
        o._planner_llm = _FakePlanner(self.steps)
        # 反思也是一次 LLM 调用：离线用例必须在**请求边界**替身，否则会真的出网付费
        o._reflect = lambda *a, **k: {
            "accepted": True, "score": 9.0, "verdict": "accept",
            "gaps": [], "next_steps": [],
        }
        o._route_template = lambda goal, task_id: None
        o._structured_data_preload = lambda *a, **k: None
        o._direct_deliverable_plan = lambda goal: None
        o._ensure_package_step = lambda steps: steps
        # 真实 `_plan` 需要的编排参数（make_orch 默认不设，多数测试直接替身 _plan）
        o._max_offtopic_regenerations = 0
        o._task_timeout = 300
        o._build_delivery_summary = lambda tid, g, steps, done: (
            "# 交付结果说明（离线替身）：报告已生成，文件见交付包", [])
        o._prune_superseded_files = lambda *a, **k: False
        o._sweep_workspace_artifacts = lambda tid: None
        o._notify_done_async = lambda *a, **k: None
        o._publish_usage = lambda: None
        o._record_reflection_refinement = lambda *a, **k: None
        o._memory = _RecordingMemory()
        proj = ws_mod.task_project_dir(task_id)
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "search_results.json").write_text(
            json.dumps(SRC_SINA, ensure_ascii=False), encoding="utf-8")
        return o

    def step_brpop(self, task_id: str, *, report_on: str = "2", fail_steps=()):
        """步骤结果回包替身（按派发映射识别步骤；报告步骤同时落盘报告正文）。"""

        def _pop(r, key, deadline):
            k = str(key)
            if not k.startswith("task_result:"):
                return None
            step_id = self.step_by_key.get(k, "")
            return ("k", json.dumps(self.payload(task_id, step_id, report_on, fail_steps)))

        return _pop

    def mixed_brpop(self, task_id: str, *, review_reply=PASS_JSON,
                    report_on: str = "2", fail_steps=(), broken_reply=None):
        """按 key 分流：plan_review:* 回评审结论；task_result:* 回步骤结果。

        步骤结果必须**能等到**：`_dispatch` 的超时下限是 300 秒，替身不回包
        测试就会真等满 5 分钟。
        """

        def _pop(r, key, deadline):
            k = str(key)
            if k.startswith("plan_review:"):
                if review_reply is None:
                    return None
                return ("k", json.dumps(review_reply))
            if broken_reply is not None:
                return ("k", broken_reply)
            if not k.startswith("task_result:"):
                return None
            step_id = self.step_by_key.get(k, "")
            return ("k", json.dumps(self.payload(task_id, step_id, report_on, fail_steps)))

        return _pop

    def payload(self, task_id: str, step_id: str, report_on: str, fail_steps) -> dict:
        if step_id in fail_steps:
            return {"task_id": step_id, "status": "FAILED", "result": "注入的失败"}
        if step_id == report_on:
            rpath = ws_mod.task_reports_dir(task_id) / "report.md"
            rpath.parent.mkdir(parents=True, exist_ok=True)
            rpath.write_text(self.report_body, encoding="utf-8")
            return {"task_id": step_id, "status": "SUCCESS",
                    "result": self.report_body}
        return {"task_id": step_id, "status": "SUCCESS",
                "result": ("搜索结果：贵州茅台2024年营业收入1741亿元，净利润862亿元。"
                           "来源：https://finance.sina.com.cn/a/1")}


class _RecordingMemory:
    """记录沉淀调用：用于核对"哪些运行允许沉淀"。"""

    def __init__(self):
        self.consolidated: list = []

    def consolidate_memory(self, goal, steps, report, **kw):
        self.consolidated.append({"goal": goal, "admission": kw.get("admission")})

    def inject_context(self, goal):
        return ""


class TestOfflineFullDelivery(unittest.TestCase):
    """① 离线完整交付：版本/来源/规则/导出四处一致。"""

    def setUp(self):
        self.run = _OfflineRun(self)
        self.tid = "off-1"
        self.o = self.run.orch(self.tid)
        self.o._brpop_with_deadline = self.run.mixed_brpop(self.tid)
        # 离线纪律：任何真实出网立即失败（含模型/检索/嵌入）
        guard = mock.patch("socket.socket.connect",
                           side_effect=AssertionError("离线交付测试不得联网"))
        guard.start()
        self.addCleanup(guard.stop)
        # 模型健康/余额预检也在请求边界替身：否则它会真的去探测端点，
        # 被上面的护栏打断后任务会被判"端点不可用"直接失败（环境事实，不是交付缺陷）
        for target, value in (
            ("llm_client.endpoints_available", lambda: (True, "offline")),
            # 进程级的降级台账会把别的用例/真实探测的结论带进来（顺序相关）：
            # 本类验证的是交付与版本语义，台账在此按"无降级"固定
            ("llm_client.get_task_llm_degradation", lambda tid: {}),
            ("llm_client.get_endpoint_warning", lambda: ""),
            ("llm_client.get_balance_status",
             lambda **kw: {"primary": {"ok": True, "reason": "ok"},
                           "backup": {"ok": True, "reason": "ok"}}),
        ):
            pt = mock.patch(target, value)
            pt.start()
            self.addCleanup(pt.stop)

    def _run(self):
        with mock.patch("orchestrator_v2.push_progress"):
            res = self.o.run(self.tid, GOAL, auto_run=True)
        return res

    def test_full_delivery_is_consistent_across_page_exports_and_manifest(self):
        # 打开评审：只有拿到绑定 PASS 的运行才算"已验证成功"（M0-d 准入的第二道条件）
        self.o = self.run.orch(self.tid, critic=True)
        self.o._brpop_with_deadline = self.run.mixed_brpop(self.tid)
        res = self._run()
        self.assertEqual(res["status"], "SUCCESS",
                         f"delivery={self.o._delivery(self.tid)!r} "
                         + res.get("report", "")[:200])

        from report_version import VersionStore, body_hash
        from web_ui import _write_export_manifest

        ws = ws_mod.task_workspace(self.tid)
        store = VersionStore(ws, self.tid)
        adopted = store.adopted()
        self.assertIsNotNone(adopted, "交付后必须有选中版本")
        self.assertTrue(adopted.acceptance_for_this_body(),
                        "选中版本必须带**该版自身**的验收")
        acc = json.loads((ws / "acceptance_report.json").read_text(encoding="utf-8"))
        self.assertEqual(acc.get("overall"), "pass", acc.get("gaps"))
        self.assertTrue(acc.get("rules_version"), "验收必须记规则版本")
        self.assertTrue(acc.get("rules_fingerprint"), "验收必须记规则指纹")
        self.assertEqual(adopted.rules_fingerprint, acc.get("rules_fingerprint"),
                         "版本记录的规则指纹必须与验收一致")

        deliveries = store.deliveries()
        self.assertTrue(deliveries, "收尾必须记录最终交付文档")
        self.assertTrue(deliveries[-1]["ok"], deliveries[-1].get("reason"))
        self.assertEqual(self.o._delivery(self.tid).get("reason"), "",
                         "通过验收的交付不得被标草稿")
        self.assertEqual(self.o._delivery(self.tid).get("status"), "verified")
        final_report = res["final_report"]
        self.assertIn(REPORT_BODY.splitlines()[0], final_report)
        self.assertEqual(deliveries[-1]["delivered_sha256"], body_hash(final_report),
                         "页面正文的 hash 必须等于记录的交付正文 hash")

        manifest = _write_export_manifest(self.tid, final_report, b"%PDF-1.4 offline")
        self.assertEqual(manifest["report_version_id"], adopted.identity_id())
        self.assertEqual(manifest["body_sha256"], adopted.version_id)
        self.assertTrue(manifest["aligned"])
        self.assertFalse(manifest["draft"], "验收通过且送达一致 → 不是草稿")
        self.assertTrue(manifest["final_content_matches"],
                        "导出字节必须就是记录的最终交付正文")
        md_sha = manifest["files"]["markdown"]["sha256"]
        self.assertNotEqual(md_sha, manifest["files"]["pdf"]["sha256"])
        self.assertEqual(md_sha, body_hash(final_report))

        from test_report_version import _FakeHandler

        h = _FakeHandler()
        with mock.patch("web_ui.task_workspace", lambda tid: ws),                 mock.patch("web_ui._get_task_report_data",
                           lambda tid: {"report": final_report, "goal": GOAL}):
            from web_ui import _get_task_markdown
            _get_task_markdown(h, f"/api/task/{self.tid}/report.md")
        self.assertEqual(dict(h.headers)["X-Report-Version-Id"], adopted.identity_id())
        import hashlib
        self.assertEqual(hashlib.sha256(h.body).hexdigest(), md_sha,
                         "路由送达的字节必须与清单登记的 markdown 一致")

    def test_admitted_run_consolidates_experience(self):
        """通过验收 + 评审 PASS 的运行才允许沉淀经验（准入谓词的真实接线）。"""
        self.o = self.run.orch(self.tid, critic=True)
        self.o._brpop_with_deadline = self.run.mixed_brpop(self.tid)
        res = self._run()
        self.assertTrue(self.o._review_state(self.tid)["verdict"] == "PASS",
                        "本轮必须拿到绑定的评审 PASS")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(self.o._memory.consolidated, "已验证成功应沉淀经验")
        adm = self.o._memory.consolidated[0]["admission"]
        self.assertTrue(adm["admitted"])
        self.assertTrue(adm["verified"], adm["reasons"])

    def test_offline_run_uses_no_network(self):
        """离线纪律：整段跑完不出现真实出网连接（请求边界已替身）。"""
        with mock.patch("socket.socket.connect",
                        side_effect=AssertionError("离线测试不得联网")):
            res = self._run()
        self.assertEqual(res["status"], "SUCCESS")


class TestOfflineFaultInjection(unittest.TestCase):
    """② 故障注入：取消 / 评审不可用（个人+银行）/ 超时 / 迟到结果。"""

    def setUp(self):
        self.run = _OfflineRun(self)

    def _mixed_brpop(self, tid: str, *, review_reply=None):
        """按 key 分流：task_result:* 回步骤结果（含报告落盘），plan_review:* 按参数回包。

        关键：步骤结果必须**能等到**——`_dispatch` 的超时下限是 300 秒，
        替身若不回包，测试就会真等满 5 分钟（这正是下面每条用例都要显式回包的原因）。
        """
        step_pop = self.run.step_brpop(tid)

        def _pop(r, key, deadline):
            if str(key).startswith("plan_review:"):
                if review_reply is None:
                    return None
                return ("k", json.dumps(review_reply))
            return step_pop(r, key, deadline)

        return _pop

    def _base_orch(self, tid: str, **kw):
        o = self.run.orch(tid, **kw)
        o._find_agent = lambda cap: "fake-agent"
        o._brpop_with_deadline = self._mixed_brpop(tid)
        return o

    def test_cancel_stops_run_and_keeps_cancelled_terminal(self):
        tid = "flt-cancel"
        o = self._base_orch(tid)
        cancelled = {"v": False}

        def _wait(dispatch_id, timeout, cancel_task_id=""):
            from orchestrator_v2 import WaitOutcome, WAIT_CANCEL
            cancelled["v"] = True
            return WaitOutcome(WAIT_CANCEL, reason="用户取消")

        o._wait_step_result = _wait
        o._cancel_requested = lambda task_id: cancelled["v"]
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(tid, GOAL, auto_run=True)
        self.assertEqual(res["status"], "CANCELLED")
        # 取消后不再沉淀经验/模板，且在飞调用留痕待对账
        self.assertFalse((ws_mod.task_workspace(tid) / "consolidation").exists())
        snapshot = o.budget_snapshot(tid)
        self.assertIn("stages", snapshot)

    def test_review_timeout_marks_degraded_in_personal_mode(self):
        tid = "flt-review-local"
        o = self._base_orch(tid, critic=True)
        o._identity_mode = lambda: "local"
        o._critic_timeout = 1
        o._brpop_with_deadline = self._mixed_brpop(tid, review_reply=None)
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(tid, GOAL, auto_run=True)
        state = o._review_state(tid)
        self.assertEqual(state["verdict"], "DEGRADED")
        self.assertIn("超时", state["degraded_reason"])
        review_file = ws_mod.task_workspace(tid) / "review_state.json"
        self.assertTrue(review_file.exists(), "评审状态必须落盘")
        self.assertIn("评审未完成", res["final_report"],
                      "降级必须写进交付物本体，不能只留日志")

    def test_review_timeout_refused_in_bank_mode(self):
        tid = "flt-review-bank"
        o = self._base_orch(tid, critic=True)
        o._identity_mode = lambda: "bank"
        o._critic_timeout = 1
        o._brpop_with_deadline = self._mixed_brpop(tid, review_reply=None)
        with mock.patch("orchestrator_v2.push_progress"):
            with self.assertRaises(Exception) as ctx:
                o.run(tid, GOAL, auto_run=True)
        self.assertIn("评审", str(ctx.exception))
        # 银行口径拒绝的任务不得留下"已交付"的版本记录
        from report_version import VersionStore
        store = VersionStore(ws_mod.task_workspace(tid), tid)
        deliveries = [d for d in store.deliveries() if d.get("ok")]
        self.assertEqual(deliveries, [], "未完成必需评审不得产出已验证交付")

    def test_protocol_error_result_is_not_success(self):
        tid = "flt-proto"
        o = self._base_orch(tid)
        o._brpop_with_deadline = lambda r, key, deadline: (
            None if str(key).startswith("plan_review:") else ("k", json.dumps({})))
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(tid, GOAL, auto_run=True)
        self.assertNotEqual(res["status"], "SUCCESS")
        acc = ws_mod.task_workspace(tid) / "acceptance_report.json"
        if acc.exists():
            self.assertNotEqual(json.loads(acc.read_text(encoding="utf-8")).get("overall"),
                                "pass")

    def test_late_result_after_cancel_does_not_overwrite(self):
        tid = "flt-late"
        o = self._base_orch(tid)
        from report_version import VersionStore

        ws = ws_mod.task_workspace(tid)
        ws.mkdir(parents=True, exist_ok=True)
        store = VersionStore(ws, tid)
        v = store.record("已选中的稿子")
        store.bind_acceptance({"overall": "pass", "gaps": [],
                               "report_sha256": v.version_id})
        store.adopt(store.get(v.version_id), reason="交付")
        # 迟到结果：只登记拒绝，不改选中版本
        store.reject_late("迟到 worker 结果")
        self.assertEqual(store.adopted().body, "已选中的稿子")
        self.assertTrue(store.deliveries() == [])
        data = json.loads((ws / "report_versions.json").read_text(encoding="utf-8"))
        self.assertTrue(data.get("late_rejected"), "迟到结果必须留痕")


if __name__ == "__main__":
    unittest.main()
