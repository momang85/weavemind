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
import os
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

    def test_research_gate_keeps_unverified_paper_as_draft(self):
        """研究任务：底稿不达标（口径未知）→ 交付只能是草稿，终态 SUCCESS_WITH_ISSUES。

        对偶于上面那条 happy path：验收通过、评审也过，但**底稿侧认证不过**时交付不得
        判为已验证（A 批把底稿结论接进交付硬约束）。
        """
        self.o = self.run.orch(self.tid, critic=True)
        self.o._brpop_with_deadline = self.run.mixed_brpop(self.tid)
        payload = {
            "financials": [
                # 行内**不声明口径**（真实快照就是这样）→ 口径未知 → 不达标
                {"year": 2023, "report_type": "年报", "revenue": 1505.6,
                 "net_profit": 747.34, "operating_cashflow": 665.93},
                {"year": 2024, "report_type": "年报", "revenue": 1741.44,
                 "net_profit": 862.28, "operating_cashflow": 924.64},
            ],
            "metadata": {"source": "eastmoney_ashare", "company": "贵州茅台",
                         "stock_code": "600519", "currency": "CNY", "unit": "亿元"},
            "raw": {"url": "https://example.invalid/mt", "text": "{}"},
        }
        proj = ws_mod.task_project_dir(self.tid, "default")

        # `run()` 开头会重建任务工作区（预置文件会被清掉），所以结构化财务必须在
        # **执行阶段**落地——这正是生产里结构化预载做的事：把 `route_structured`
        # 换成"写夹具并返回载荷"，其余链路（底稿→门槛→交付）全走真实实现。
        def _fake_preload(task_id, goal, project=None, **_kw):
            proj.mkdir(parents=True, exist_ok=True)
            (proj / "financials.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return payload

        self.o = self.run.orch(self.tid, critic=True)
        self.o._brpop_with_deadline = self.run.mixed_brpop(self.tid)
        self.o._structured_data_preload = _fake_preload

        res = self._run()
        self.assertEqual(res["status"], "SUCCESS_WITH_ISSUES",
                         f"delivery={self.o._delivery(self.tid)!r}")
        self.assertEqual(self.o._delivery(self.tid).get("status"), "draft")
        self.assertTrue(self.o._delivery(self.tid).get("hard_fail"),
                        "底稿不达标必须写进交付硬约束")
        self.assertIn("研究交付硬门槛未通过", res["final_report"])

        from report_version import VersionStore
        store = VersionStore(ws_mod.task_workspace(self.tid), self.tid)
        deliveries = store.deliveries()
        self.assertTrue(deliveries)
        self.assertFalse(deliveries[-1]["ok"], "草稿交付不得记为 ok")

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
        # 端点健康/余额探测是**进程级缓存**的：本类里前一条用例故意让端点失败，
        # 缓存把"端点不可用"带到后一条用例上——后者的 run() 会在健康预检直接终止，
        # 于是"银行模式评审超时应拒绝"这类断言看到的失败原因完全不是被测行为
        # （实测：用例单独跑通过、整类跑失败）。这里按"端点可用"固定住探测结论，
        # 让每条用例只验证自己要验证的那一条故障路径。
        for target, value in (
            ("llm_client.endpoints_available", lambda: (True, "offline")),
            ("llm_client.get_task_llm_degradation", lambda tid: {}),
            ("llm_client.get_endpoint_warning", lambda: ""),
            ("llm_client.get_balance_status",
             lambda **kw: {"primary": {"ok": True, "reason": "ok"},
                           "backup": {"ok": True, "reason": "ok"}}),
        ):
            pt = mock.patch(target, value)
            pt.start()
            self.addCleanup(pt.stop)

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


RESEARCH_GOAL = (
    "研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润、经营活动现金流净额，"
    "合并报表口径，数据截至 2025-04-30。"
)


def _research_contract():
    """表单提交的契约（主体/市场/两年度/口径/截至日都来自表单，不靠文本解析）。"""
    from facts import parse_research_request
    return parse_research_request(
        RESEARCH_GOAL, company="贵州茅台", company_id="600519.SH", market="cn",
        periods=[2023, 2024], caliber="合并", as_of="2025-04-30",
        identity_source="form")


# 研究路径的报告正文夹具：数字必须与 financials.json 里的**逐字一致**
# （溯源按数值+单位比对，"1741亿元"与源里的"1741.44亿元"不算同一条事实）
RESEARCH_REPORT_BODY = (
    "# 贵州茅台 2023–2024 年度财务数据（合并报表口径）\n\n"
    "## 核心指标\n\n"
    "| 指标 | 2023 | 2024 | 来源 |\n|---|---|---|---|\n"
    "| 营业收入 | 1505.6亿元 | 1741.44亿元 | [1] |\n"
    "| 归母净利润 | 747.34亿元 | 862.28亿元 | [1] |\n"
    "| 经营活动现金流净额 | 665.93亿元 | 924.64亿元 | [1] |\n\n"
    "## 数据时效\n\n数据截至 2025-04-30，取自公司年度报告（合并报表口径），日终更新。\n\n"
    "## 参考来源\n\n1. [贵州茅台2024年报：营业收入1741亿元]"
    "(https://finance.sina.com.cn/a/1)\n\n"
    "## 免责声明\n\n" + DISCLAIMER
)


def _research_financials(*, caliber: str = "合并") -> dict:
    """两年 × 三项必需指标的财务夹具（行内声明口径，便于既有用例继续用）。"""
    rows = []
    for year, rev, np_, cf in ((2023, 1505.6, 747.34, 665.93),
                               (2024, 1741.44, 862.28, 924.64)):
        rows.append({"year": year, "report_type": "年报", "caliber": caliber,
                     "revenue": rev, "net_profit": np_, "operating_cashflow": cf,
                     # 公告日期（NOTICE_DATE）：截至日证据，与报告期末分开
                     "disclosure_date": f"{year + 1}-04-03",
                     "report_date": f"{year}-12-31"})
    return {
        "financials": rows,
        "metadata": {"source": "eastmoney_ashare", "company": "贵州茅台",
                     "stock_code": "600519", "currency": "CNY", "unit": "亿元",
                     "period": "annual", "latest_report": "2024-12-31"},
        "raw": {"url": "https://example.invalid/mt", "text": "{}"},
    }


def _adapter_shaped_financials(*, declare: bool = True) -> dict:
    """**生产形状**的夹具：行内不声明口径，由适配器在 metadata 里按来源结构声明。

    实机 `ui-96f5c363cc` 抓到的就是这种形状（行内无 caliber）；区别只在适配器现在
    会带上 `caliber` + `caliber_evidence`。
    """
    payload = _research_financials()
    for r in payload["financials"]:
        r.pop("caliber", None)
    if declare:
        payload["metadata"]["caliber"] = "合并"
        payload["metadata"]["caliber_evidence"] = (
            "来源行含 PARENTNETPROFIT（归属于母公司股东的净利润），"
            "该科目只存在于合并报表 → 合并报表口径")
    return payload


class _BrokenPlanner:
    """规划器替身：按指定方式坏掉（超时 / 坏 JSON）。

    固定研究路径的断言前提是"**规划器根本没被问到**"——所以这里的 `calls`
    计数必须为 0；真被问到就会抛错让用例失败。
    """

    def __init__(self, mode: str = "timeout"):
        self.mode = mode
        self.calls = 0

    def call(self, system, user, **kw):
        self.calls += 1
        from llm_client import LLMCallError, LLMJSONParseError
        if self.mode == "timeout":
            exc = LLMCallError("HTTP 504: gateway timeout（注入）")
            raise exc
        raise LLMJSONParseError("plan is not JSON（注入）")


class _RecordingRedis:
    """替身 Redis：把 `_record_llm_call` 真正**写下去**的载荷记下来。

    断言对象是落盘载荷（默认值已补齐），不是调用参数——否则测的是"传了什么"
    而不是"记了什么"。
    """

    def __init__(self):
        self.rows: list = []

    def __getattr__(self, name):
        """除 lrange 外一律当空实现。

        只通过 `pipeline()` 收集载荷：降级台账用的是直接 `rpush`（另一种记录），
        混进来会让断言比的是两套 schema。
        """
        def _noop(*a, **k):
            if name == "lrange":
                return list(self.rows)
            return None
        return _noop

    def pipeline(self):
        rows = self.rows

        def _rpush(*a, **k):
            if len(a) == 2:
                rows.append(a[1])

        class _Pipe:
            rpush = staticmethod(_rpush)
            ltrim = staticmethod(lambda *a, **k: None)
            expire = staticmethod(lambda *a, **k: None)
            execute = staticmethod(lambda *a, **k: None)

        return _Pipe()


class TestAsyncCallDiagnostics(unittest.TestCase):
    """异步调用路径（worker 步骤走的那条）也要记调用形状。

    此前只埋了同步 `LLMClient.call`：实机里 worker 的步骤调用全部走 async，
    于是 `llm_calls` 只在编排器侧有记录、步骤侧为空——诊断等于漏了最要紧的一段。
    """

    def test_async_records_shape_and_keeps_stage_label(self):
        import asyncio

        import httpx
        import llm_client

        fake = _RecordingRedis()
        req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
        resp504 = httpx.Response(504, request=req, text="<html>504</html>")

        class _Client:
            def __init__(self):
                self.calls = 0

            async def post(self, *a, **k):
                self.calls += 1
                return resp504

        client = _Client()
        llm_client.set_task_context("async-diag")
        self.addCleanup(llm_client.set_task_context, "")
        with mock.patch.object(llm_client, "_task_usage_client", fake), \
                mock.patch.object(llm_client, "_get_async_client", lambda: client), \
                mock.patch.object(llm_client, "_ensure_cfg_fresh", lambda: None), \
                mock.patch.object(llm_client, "_BACKUP_CFG", {}), \
                mock.patch.object(llm_client, "_task_budget_limits",
                                  lambda: {"max_calls": 0, "max_seconds": 0.0,
                                           "max_cost_usd": 0.0}):
            with self.assertRaises(llm_client.LLMCallError):
                asyncio.run(llm_client.call_llm_async(
                    "系统提示词", "用户提示词", expect_json=False,
                    usage="exec", max_attempts=2))
        self.assertEqual(client.calls, 2)
        recs = [json.loads(r) for r in fake.rows]
        self.assertEqual(len(recs), 2, recs)
        for rec in recs:
            # 阶段标签来自 usage：**不能**被响应里的 token 用量覆盖
            self.assertEqual(rec["stage"], "exec", rec)
            self.assertEqual(rec["http_status"], 504)
            self.assertEqual(rec["error_class"], "http_504")
            self.assertEqual(rec["input_chars"], len("系统提示词") + len("用户提示词"))
        self.assertEqual(recs[0]["end_reason"], "retry")
        self.assertEqual(recs[-1]["end_reason"], "exhausted")
        blob = json.dumps(recs, ensure_ascii=False)
        self.assertNotIn("系统提示词", blob)
        self.assertNotIn("用户提示词", blob)


class TestStreamTransport(unittest.TestCase):
    """流式传输：SSE 累积还原成**同形**响应体，网关不再按整段耗时判超时。

    实机背景：网关（响应体里的 alb）在 ~60s 处切断整段非流式响应，报告/总结这类
    长生成必 504；流式让字节持续到达。这里只验证"传输层替换、下游语义不变"。
    """

    def setUp(self):
        # 端点守卫默认拒绝本机/私网；这些用例测的是传输与解析，不是地址策略
        self._old = os.environ.get("WM_LLM_ALLOW_LOCAL")
        os.environ["WM_LLM_ALLOW_LOCAL"] = "1"
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        if self._old is None:
            os.environ.pop("WM_LLM_ALLOW_LOCAL", None)
        else:
            os.environ["WM_LLM_ALLOW_LOCAL"] = self._old

    @staticmethod
    def _sse_lines(text, *, with_usage=True, done=True):
        out = []
        for i in range(0, len(text), 5):
            chunk = {"choices": [{"index": 0, "delta": {"content": text[i:i + 5]}}]}
            out.append("data: " + json.dumps(chunk, ensure_ascii=False))
        final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        if with_usage:
            final["usage"] = {"prompt_tokens": 11, "completion_tokens": 22,
                              "total_tokens": 33}
        out.append("data: " + json.dumps(final, ensure_ascii=False))
        if done:
            out.append("data: [DONE]")
        return out

    def test_async_stream_reassembles_response(self):
        import asyncio

        import llm_client

        body = "报告正文：营业收入 1741.44 亿元。"
        seen: dict = {}

        class _Resp:
            status_code = 200

            async def aiter_lines(self):
                for line in TestStreamTransport._sse_lines(body):
                    yield line

        class _StreamCtx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *a):
                return False

        class _Client:
            # 注意：httpx 的 `client.stream(...)` 是**同步**方法，返回异步上下文管理器
            def stream(self, method, url, json=None, headers=None):
                seen["payload"] = json
                return _StreamCtx()

            async def post(self, *a, **k):        # 不该被调用
                raise AssertionError("流式成功时不得再发非流式请求")

        llm_client.set_task_context("stream-diag")
        self.addCleanup(llm_client.set_task_context, "")
        fake = _RecordingRedis()
        with mock.patch.object(llm_client, "_task_usage_client", fake), \
                mock.patch.object(llm_client, "_get_async_client", lambda: _Client()), \
                mock.patch.object(llm_client, "_ensure_cfg_fresh", lambda: None), \
                mock.patch.object(llm_client, "_task_budget_limits",
                                  lambda: {"max_calls": 0, "max_seconds": 0.0,
                                           "max_cost_usd": 0.0}):
            out = asyncio.run(llm_client.call_llm_async(
                "系统", "用户", expect_json=False, usage="exec",
                max_attempts=1, model_override="m"))
        self.assertEqual(out, body, "SSE 累积必须还原成完整正文")
        self.assertTrue(seen["payload"].get("stream"), seen["payload"])
        self.assertTrue(seen["payload"].get("stream_options", {}).get("include_usage"))
        # 用量在流式下也要记到（否则成本账目变空）
        rec = json.loads(fake.rows[-1])
        self.assertEqual(rec["end_reason"], "ok")
        self.assertEqual(rec["stage"], "exec")

    def test_async_stream_rejected_falls_back_to_plain(self):
        import asyncio

        import httpx
        import llm_client

        req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
        calls = {"stream": 0, "post": 0}

        class _Rejected:
            """流式被拒的响应（400 + 可读 body），走 raise_for_status 分支。"""
            status_code = 400

            async def aread(self):
                return b"stream not supported"

            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "400", request=req,
                    response=httpx.Response(400, request=req,
                                            text="stream not supported"))

        class _StreamCtx:
            async def __aenter__(self):
                return _Rejected()

            async def __aexit__(self, *a):
                return False

        class _Ok:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "ok"}}], "usage": {}}

        class _Client:
            def stream(self, *a, **k):
                calls["stream"] += 1
                return _StreamCtx()

            async def post(self, *a, **k):
                calls["post"] += 1
                return _Ok()

        llm_client.set_task_context("stream-fb")
        self.addCleanup(llm_client.set_task_context, "")
        fake = _RecordingRedis()
        with mock.patch.object(llm_client, "_task_usage_client", fake), \
                mock.patch.object(llm_client, "_get_async_client", lambda: _Client()), \
                mock.patch.object(llm_client, "_ensure_cfg_fresh", lambda: None), \
                mock.patch.object(llm_client, "_task_budget_limits",
                                  lambda: {"max_calls": 0, "max_seconds": 0.0,
                                           "max_cost_usd": 0.0}):
            out = asyncio.run(llm_client.call_llm_async(
                "s", "u", expect_json=False, usage="exec", max_attempts=1,
                model_override="m"))
        self.assertEqual(calls["stream"], 1, "先试流式")
        self.assertEqual(calls["post"], 1, "被拒后回退非流式")
        self.assertEqual(out, "ok")

    def test_sse_helpers_ignore_noise(self):
        """非 data 行、坏 chunk、[DONE] 都不该让整段失败。"""
        import llm_client
        acc = llm_client._new_stream_acc()
        for line in (": keep-alive", "", "event: ping", "data: {broken",
                     "data: " + json.dumps({"choices": [{"delta": {"content": "甲"}}]}),
                     "data: " + json.dumps({"choices": [{"delta": {"content": "乙"}}]}),
                     "data: [DONE]"):
            payload_line = llm_client._sse_payload(line)
            if not payload_line or payload_line == "[DONE]":
                continue
            try:
                llm_client._merge_stream_chunk(acc, json.loads(payload_line))
            except Exception:
                continue
        self.assertEqual(llm_client._stream_result(acc)["choices"][0]["message"]["content"],
                         "甲乙")

    def test_endpoint_guard_rejects_local_by_default(self):
        """默认拒绝本机/私网端点；显式开关或登记端点才放行。"""
        import llm_client
        llm_client._ENDPOINT_OK_CACHE.clear()
        os.environ.pop("WM_LLM_ALLOW_LOCAL", None)
        with self.assertRaises(llm_client.LLMCallError):
            llm_client._endpoint_guard("http://127.0.0.1:8799/v1/chat/completions")
        with self.assertRaises(llm_client.LLMCallError):
            llm_client._endpoint_guard("ftp://example.invalid/x")
        with self.assertRaises(llm_client.LLMCallError):
            llm_client._endpoint_guard("https://user:pw@example.invalid/x")
        os.environ["WM_LLM_ALLOW_LOCAL"] = "1"
        llm_client._ENDPOINT_OK_CACHE.clear()
        self.assertTrue(llm_client._endpoint_guard("http://127.0.0.1:8799/v1/x"))


    def test_sync_request_streams_and_skips_plain_request(self):
        """同步路径也走流式（复用既有实现）：长生成不再被网关 60s 切断。"""
        import llm_client

        client = llm_client.LLMClient(base_url="http://offline.invalid", api_key="k",
                                      model="m")
        with mock.patch.object(llm_client, "_call_llm_stream_once",
                               return_value="报告正文") as st, \
                mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("流式成功时不该再发非流式请求")):
            out = client._send_request("系统", "用户", 0.1, 100)
        self.assertEqual(out, "报告正文")
        self.assertFalse(st.call_args.kwargs.get("publish", True),
                         "非展示用途不得把片段推进步骤流")

    def test_sync_request_falls_back_when_stream_rejected(self):
        import llm_client

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "非流式正文"}}],
                                   "usage": {}}).encode("utf-8")

        client = llm_client.LLMClient(base_url="http://offline.invalid", api_key="k",
                                      model="m")
        with mock.patch.object(llm_client, "_call_llm_stream_once",
                               side_effect=llm_client.LLMCallError("HTTP 400: no stream")), \
                mock.patch("urllib.request.urlopen", return_value=_Resp()):
            out = client._send_request("s", "u", 0.1, 100)
        self.assertEqual(out, "非流式正文")

    def test_sync_request_reports_stream_failure_other_than_unsupported(self):
        """5xx/超时不算"不支持流式"：如实抛出，不静默回退。"""
        import llm_client

        client = llm_client.LLMClient(base_url="http://offline.invalid", api_key="k",
                                      model="m")
        with mock.patch.object(llm_client, "_call_llm_stream_once",
                               side_effect=llm_client.LLMCallError("HTTP 504: gateway")), \
                mock.patch("urllib.request.urlopen",
                           side_effect=AssertionError("不得回退")):
            with self.assertRaises(llm_client.LLMCallError):
                client._send_request("s", "u", 0.1, 100)


class TestResearchFixedPathOffline(unittest.TestCase):
    """③ 固定研究路径的离线故障注入（C 批）。

    契约驱动的研究任务不经过通用规划器：把规划器打成"坏"，研究路径仍要
    取到事实、建出底稿、交付可核对的结论（或明确失败），而不是掉进
    单步 content_summary 兜底、也不是无源长文冒充已验证交付。
    """

    def setUp(self):
        self.run = _OfflineRun(self)
        self.tid = "off-res-1"
        # 契约落库用临时库（不碰真实任务库）
        import task_state
        self.db = str(self.run.tmp / "research.db")
        self._orig_db = task_state.DB_PATH
        task_state.DB_PATH = self.db
        self.addCleanup(setattr, task_state, "DB_PATH", self._orig_db)
        for target, value in (
            ("llm_client.endpoints_available", lambda: (True, "offline")),
            ("llm_client.get_task_llm_degradation", lambda tid: {}),
            ("llm_client.get_endpoint_warning", lambda: ""),
            ("llm_client.get_balance_status",
             lambda **kw: {"primary": {"ok": True, "reason": "ok"},
                           "backup": {"ok": True, "reason": "ok"}}),
        ):
            pt = mock.patch(target, value)
            pt.start()
            self.addCleanup(pt.stop)
        guard = mock.patch("socket.socket.connect",
                           side_effect=AssertionError("离线测试不得联网"))
        guard.start()
        self.addCleanup(guard.stop)

    def _seed_contract(self, **overrides):
        import task_state
        payload = _research_contract().to_payload()
        payload.update(overrides)
        task_state.mark_queued(self.tid, goal=RESEARCH_GOAL,
                               research_request=payload, db_path=self.db)

    def _brpop(self, *, fail_report: bool = False):
        """步骤回包替身：按固定研究计划的步骤语义回包。

        1 搜索 → 结果列表（含原始 URL）；2 抓取 → 真实 worker 的 `{title,url,text}`
        JSON 形态；3 解释 → 文本；4 报告 → 落盘正文（与真实 worker 的落盘位置一致）。
        """
        tid = self.tid

        def _pop(r, key, deadline):
            k = str(key)
            if k.startswith("plan_review:"):
                return ("k", json.dumps(PASS_JSON))
            if not k.startswith("task_result:"):
                return None
            step_id = self.run.step_by_key.get(k, "")
            if step_id == "2":
                return ("k", json.dumps({
                    "task_id": step_id, "status": "SUCCESS",
                    "result": json.dumps({
                        "title": "贵州茅台2024年报：营业收入1741亿元_新浪财经",
                        "url": "https://finance.sina.com.cn/a/1",
                        "text": ("贵州茅台2023年年报（合并报表口径）：营业收入1505.6亿元，"
                                 "归母净利润747.34亿元，经营活动现金流净额665.93亿元。"
                                 "2024年年报（合并报表口径）：营业收入1741.44亿元，"
                                 "归母净利润862.28亿元，经营活动现金流净额924.64亿元。"
                                 "以上数据取自公司年度报告，单位人民币亿元。"),
                    }, ensure_ascii=False)}))
            if step_id == "4":
                if fail_report:
                    return ("k", json.dumps({"task_id": step_id, "status": "FAILED",
                                             "result": "注入的正文模型失败"}))
                rpath = ws_mod.task_reports_dir(tid) / "report.md"
                rpath.parent.mkdir(parents=True, exist_ok=True)
                rpath.write_text(RESEARCH_REPORT_BODY, encoding="utf-8")
                return ("k", json.dumps({"task_id": step_id, "status": "SUCCESS",
                                         "result": RESEARCH_REPORT_BODY}))
            return ("k", json.dumps({
                "task_id": step_id, "status": "SUCCESS",
                "result": ("搜索结果：贵州茅台2024年营业收入1741.44亿元，"
                           "归母净利润862.28亿元。"
                           "来源：https://finance.sina.com.cn/a/1")}))

        return _pop

    def _orch(self, planner_mode: str = "timeout", *, critic: bool = False,
              fail_report: bool = False, **overrides):
        import orchestrator_v2 as ov
        o = self.run.orch(self.tid, critic=critic, **overrides)
        # 恢复**真实**模板路由：固定研究路径就在它里面（其余替身照旧）
        o._route_template = (
            lambda goal, task_id="": ov.OrchestratorV2._route_template(o, goal, task_id)
        )
        o._planner_llm = _BrokenPlanner(planner_mode)
        # 固定研究计划的报告步骤是第 4 步（1 搜索 / 2 抓取 / 3 解释 / 4 报告）
        o._brpop_with_deadline = self._brpop(fail_report=fail_report)
        return o

    @staticmethod
    def _fake_preload(task_id: str, goal: str, project=None, **kw):
        """生产里预载会写 financials.json；这里写夹具并返回载荷。"""
        if kw.get("payload") is None:
            return None
        proj = ws_mod.task_project_dir(task_id, project or "default")
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "financials.json").write_text(
            json.dumps(kw["payload"], ensure_ascii=False), encoding="utf-8")
        return kw["payload"]

    def _preload_writer(self, payload):
        def _pre(task_id, goal, project=None, **_kw):
            proj = ws_mod.task_project_dir(task_id, project or "default")
            proj.mkdir(parents=True, exist_ok=True)
            (proj / "financials.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return payload
        return _pre

    def _plan_messages(self, o):
        out = []
        for _chan, msg in (o._messaging.published or []):
            if isinstance(msg, dict) and msg.get("type") == "plan":
                out.append(str(msg.get("message") or ""))
        return out

    # ── 规划器坏掉：仍进受控研究路径 ─────────────────────────

    def _assert_fixed_path(self, o, res, planner):
        self.assertEqual(planner.calls, 0,
                         "固定研究路径不得先问通用规划器（它是坏的）")
        self.assertNotIn("Plan fallback: single content_summary step",
                         " ".join(self._plan_messages(o)),
                         "研究任务不得掉进单步兜底")
        proj = ws_mod.task_project_dir(self.tid, "default")
        self.assertTrue((proj / "financials.json").exists(),
                        "研究路径必须取到事实（结构化预载）")
        paper = json.loads((proj / "working_paper.json").read_text(encoding="utf-8"))
        self.assertEqual(paper["request"]["company"], "贵州茅台",
                         "底稿主体必须来自契约（不得被解析/抓取改写）")
        self.assertEqual(paper["request"]["periods"], [2023, 2024])
        return paper

    def test_planner_timeout_still_enters_controlled_research_path(self):
        self._seed_contract()
        o = self._orch("timeout")
        o._structured_data_preload = self._preload_writer(_research_financials())
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        paper = self._assert_fixed_path(o, res, o._planner_llm)
        self.assertTrue(paper["ok"], paper.get("problems"))
        self.assertEqual(res["status"], "SUCCESS",
                         f"delivery={o._delivery(self.tid)!r}")

    def test_planner_bad_json_still_enters_controlled_research_path(self):
        self._seed_contract()
        o = self._orch("badjson")
        o._structured_data_preload = self._preload_writer(_research_financials())
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        self._assert_fixed_path(o, res, o._planner_llm)
        self.assertIn(res["status"], ("SUCCESS", "SUCCESS_WITH_ISSUES"))

    # ── 素材缺失：绝不判已验证 ───────────────────────────────

    def test_missing_material_never_certifies(self):
        """契约在、事实拿不到（预载未命中 + 搜索无结果）→ 只能是草稿 + 明确缺口。"""
        self._seed_contract()
        o = self._orch("timeout", critic=True)
        o._structured_data_preload = lambda *a, **k: None
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        delivery = o._delivery(self.tid)
        self.assertEqual(delivery.get("status"), "draft",
                         f"没有事实来源不得判已验证：{delivery!r}")
        self.assertTrue(delivery.get("hard_fail"), delivery)
        self.assertIn("研究交付硬门槛未通过", res["final_report"],
                      "缺口必须写进交付物本体，而不是静默通过")
        self.assertNotEqual(res["status"], "SUCCESS")
        from report_version import VersionStore
        store = VersionStore(ws_mod.task_workspace(self.tid), self.tid)
        self.assertTrue(store.deliveries())
        self.assertFalse(store.deliveries()[-1]["ok"], "草稿交付不得记为 ok")

    def test_adapter_declared_caliber_reaches_verified_delivery(self):
        """口径证据链修好的判据：生产形状（行内无口径、适配器按来源结构声明）
        要能一路走到 **verified** 交付，并带出三核心指标的同比。"""
        self._seed_contract()
        o = self._orch("timeout", critic=True)
        o._structured_data_preload = self._preload_writer(_adapter_shaped_financials())
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        delivery = o._delivery(self.tid)
        self.assertEqual(delivery.get("status"), "verified",
                         f"delivery={delivery!r}")
        self.assertEqual(delivery.get("hard_fail"), "")
        self.assertEqual(res["status"], "SUCCESS")
        proj = ws_mod.task_project_dir(self.tid, "default")
        paper = json.loads((proj / "working_paper.json").read_text(encoding="utf-8"))
        self.assertTrue(paper["ok"], paper.get("problems"))
        self.assertEqual(paper["completeness"]["present"], 6)
        yoy = [d for d in paper["derived"] if str(d.get("unit") or "") == "%"]
        self.assertEqual(len(yoy), 3, "三核心指标各一条同比（单位 %）")
        row = paper["rows"][0]
        self.assertEqual(row.get("caliber"), "合并")
        self.assertIn("PARENTNETPROFIT", row.get("caliber_evidence") or "")

    def test_undeclared_caliber_delivery_stays_draft(self):
        """对偶：来源形状变了（没有归母净利类字段）→ 不声明口径 → 只能草稿。"""
        self._seed_contract()
        o = self._orch("timeout", critic=True)
        o._structured_data_preload = self._preload_writer(
            _adapter_shaped_financials(declare=False))
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        delivery = o._delivery(self.tid)
        self.assertEqual(delivery.get("status"), "draft", delivery)
        self.assertTrue(delivery.get("hard_fail"), delivery)
        self.assertIn("研究交付硬门槛未通过", res["final_report"])

    # ── 正文模型失败：底稿仍可复核 ───────────────────────────

    def test_body_model_failure_keeps_recomputable_paper(self):
        """报告步骤失败：任务不得算成功，但已取到的事实要留下可复核底稿。"""
        self._seed_contract()
        o = self._orch("timeout", fail_report=True)
        o._structured_data_preload = self._preload_writer(_research_financials())
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        proj = ws_mod.task_project_dir(self.tid, "default")
        paper_path = proj / "working_paper.json"
        self.assertTrue(paper_path.exists(), "正文失败也要留底稿")
        saved = json.loads(paper_path.read_text(encoding="utf-8"))
        # 可重算：读侧口径（build_result）与落盘底稿的行数一致
        import working_paper_export as WPX
        recomputed = WPX.build_result(self.tid, RESEARCH_GOAL, project="default")
        self.assertTrue(recomputed.get("ok"), recomputed)
        self.assertEqual(len(recomputed["rows_detail"]), len(saved["rows"]))
        self.assertNotEqual(o._delivery(self.tid).get("status"), "verified")

    def test_salvage_paper_on_failure_branch(self):
        """计划为空/模型不可用的失败分支：兜底也要落底稿（直接覆盖该分支的实现）。"""
        self._seed_contract()
        o = self._orch("timeout")
        o._structured_data_preload = self._preload_writer(_research_financials())
        o._structured_data_preload(self.tid, RESEARCH_GOAL, "default")
        wp = o._salvage_working_paper(self.tid, RESEARCH_GOAL, "default")
        self.assertTrue(wp.get("ok"), wp)
        proj = ws_mod.task_project_dir(self.tid, "default")
        self.assertTrue((proj / "working_paper.json").exists())
        self.assertTrue((proj / "working_paper.csv").exists())

    # ── 取消 ─────────────────────────────────────────────────

    def test_cancel_before_plan_stops_run(self):
        self._seed_contract()
        o = self._orch("timeout")
        o._cancel_requested = lambda tid: True
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        self.assertEqual(res["status"], "CANCELLED")
        self.assertEqual(len(self.run.step_by_key), 0, "取消后不得派发步骤")

    def test_cancel_during_preload_stops_before_dispatch(self):
        """取消在预载期间到达：事实照取（预载不占预算），但一步都不许派发。"""
        self._seed_contract()
        o = self._orch("timeout")
        state = {"cancelled": False}
        writer = self._preload_writer(_research_financials())

        def _pre(task_id, goal, project=None, **_kw):
            out = writer(task_id, goal, project, **_kw)
            state["cancelled"] = True          # 预载过程中用户点了停止
            return out

        o._structured_data_preload = _pre
        o._cancel_requested = lambda tid: state["cancelled"]
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        self.assertEqual(res["status"], "CANCELLED")
        self.assertEqual(len(self.run.step_by_key), 0, "取消后不得派发步骤")

    def test_cancel_during_execution_stops_run(self):
        from orchestrator_v2 import WAIT_CANCEL
        self._seed_contract()
        o = self._orch("timeout")
        o._structured_data_preload = self._preload_writer(_research_financials())
        o._wait_step_result = lambda *a, **k: WAIT_CANCEL
        o._cancel_requested = lambda tid: True
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        self.assertEqual(res["status"], "CANCELLED")

    # ── 根预算共享 ───────────────────────────────────────────

    def test_root_budget_refuses_dispatch_but_keeps_facts(self):
        """根预算耗尽：不得再派发步骤；预载（不发 LLM）不占预算，事实仍留底稿。"""
        import root_budget as rb
        self._seed_contract()
        o = self._orch("timeout")
        o._structured_data_preload = self._preload_writer(_research_financials())
        budget = o._budget(self.tid)
        budget.limits = rb.BudgetLimits(max_calls=1)
        budget.reserve("pre-consume")          # 把根预算的额度先用掉
        with mock.patch("orchestrator_v2.push_progress"):
            res = o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        self.assertEqual(len(self.run.step_by_key), 0,
                         "预算耗尽后不得派发新步骤")
        self.assertNotEqual(res["status"], "SUCCESS")
        proj = ws_mod.task_project_dir(self.tid, "default")
        self.assertTrue((proj / "working_paper.json").exists(),
                        "预算耗尽也要保住已取到的事实")

    # ── 诊断脱敏 ─────────────────────────────────────────────

    def test_diagnostics_are_sanitized_shapes_only(self):
        """调用形状落盘：字段齐全，且**不含**目标文本、提示词或凭据。"""
        import llm_client
        self._seed_contract()
        fake = _RecordingRedis()
        o = self._orch("timeout")
        o._structured_data_preload = self._preload_writer(_research_financials())
        with mock.patch.object(llm_client, "_task_usage_client", fake):
            with mock.patch("orchestrator_v2.push_progress"):
                o.run(self.tid, RESEARCH_GOAL, auto_run=True)
        self.assertTrue(fake.rows, "研究路径至少要记下调用形状")
        allowed = {"ts", "stage", "attempt", "elapsed_ms", "input_chars",
                   "max_tokens", "http_status", "error_class", "end_reason",
                   "endpoint"}
        records = [json.loads(r) for r in fake.rows]
        for rec in records:
            self.assertEqual(set(rec), allowed, f"字段集不符：{set(rec) ^ allowed}")
            self.assertIsInstance(rec["input_chars"], int)
            self.assertIsInstance(rec["elapsed_ms"], int)
            self.assertIsInstance(rec["http_status"], int)
        # 阶段标签来自调用方的 usage：本用例的步骤 worker 在**请求边界**替身，
        # 所以这里只断言 schema 与脱敏；带标签的记录另有一条直接用例
        self.assertTrue(all(isinstance(r.get("stage"), str) for r in records))
        # 记录里不得出现目标文本/公司名等正文（只有形状）
        blob = json.dumps(records, ensure_ascii=False)
        self.assertNotIn("贵州茅台", blob)
        self.assertNotIn(RESEARCH_GOAL[:20], blob)
        # 同一批形状要落成工作区文件（离线取证用），且与内存记录一致
        lc = ws_mod.task_workspace(self.tid) / "llm_calls.jsonl"
        self.assertTrue(lc.exists(), "调用形状必须落盘")
        saved = [json.loads(x) for x in lc.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(saved), len(records))
        self.assertEqual(set(saved[0]), allowed)

    def test_call_record_carries_stage_and_shape(self):
        """带阶段标签的调用：记录里是"长度/上限/类别"，不是提示词本身。"""
        import llm_client
        fake = _RecordingRedis()
        client = llm_client.LLMClient(base_url="http://offline.invalid", api_key="k",
                                      model="m")
        client._send_request = lambda *a, **k: '{"ok": true}'
        llm_client.set_task_context(self.tid)
        self.addCleanup(llm_client.set_task_context, "")
        with mock.patch.object(llm_client, "_task_usage_client", fake):
            out = client.call("系统提示词", "用户提示词", usage="plan", expect_json=True)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(fake.rows), 1, fake.rows)
        rec = json.loads(fake.rows[0])
        self.assertEqual(rec["stage"], "plan")
        self.assertEqual(rec["end_reason"], "ok")
        self.assertEqual(rec["attempt"], 1)
        self.assertEqual(rec["input_chars"], len("系统提示词") + len("用户提示词"))
        self.assertEqual(rec["max_tokens"], client.max_tokens)
        self.assertEqual(rec["http_status"], 0)
        self.assertEqual(rec["error_class"], "")
        self.assertEqual(rec["endpoint"], "primary")
        blob = json.dumps(rec, ensure_ascii=False)
        self.assertNotIn("系统提示词", blob)
        self.assertNotIn("用户提示词", blob)


if __name__ == "__main__":
    unittest.main()
