# -*- coding: utf-8 -*-
"""A-修复回归（2026-10-21）：检索重试不再吃光任务级预算，且已取得候选不被顶掉。

实测背景（`ui-d59152ebb9` / `sched-f6e5440b8b`）：**同一个** web_search 步被派发 3 次——
前两次都真取到 10 条候选（worker 记 `status=ok`）但输出契约不通过，第 3 次因任务级
6 次检索预算耗尽被拒（`refused_budget`，0 次真实请求）→ 步骤判 FAILED →
"重规划代做"给 content_summarizer → 该步结果数组被顶掉 → 下游 3 个 web_fetch 全部
以"无候选 URL"跳过（控制台显示为三个红色失败，`elapsed_sec=0.0`，一次网络都没发）。

候选侧最可能的触发点（代码可证）：bing 解析在 `<li class="b_algo">` 有 `<a href>` 却
没有 `<h2>` 时产出**空标题**条目（worker_base._search_bing），而 `_emit_payload` 只要求
url 非空 → `tool_contracts.validate_result` 对前 5 条逐条校验 → "列表元素缺少 title"。
本文件覆盖四条接缝（纯函数 / 假件，不发网络、不碰工作区）：

1. `SearchAgent._emit_payload` 只放行 title/url 都非空的候选（与工具契约同口径）；
2. `SearchAgent._search_bing` 在源头剔除空标题块；
3. `worker_base.search_ledger_remaining` 只读余量（读不到 → None，不改旧行为）；
4. `OrchestratorV2._dispatch_step_safe`：预算耗尽时不再派发注定被拒的检索重试、保留
   已取得候选（SUCCESS + `search_quality_untrusted` + 诊断记录 + 不重规划代做），
   并证明下游抓取步确实能从这份保留结果里取到 URL；真失败路径不受影响。
"""
import json
import threading
import time
import unittest
from unittest import mock

from test_orchestrator_v2 import make_orch

import orchestrator_v2 as ov
import tool_contracts
import worker_base as wb

GOAL = "研究洋河股份 2023 年报与 2024 年报的营业收入、归母净利润"
TASK = "t-a-keep"
TITLED = "洋河股份2024年年度报告-营业收入与归母净利润"
GOOD_URL = "https://www.cninfo.com.cn/a.pdf"
EMPTY_TITLE_ITEMS = [
    {"title": TITLED, "url": GOOD_URL, "snippet": "营业收入 归母净利润"},
    {"title": "", "url": "https://www.cninfo.com.cn/b.pdf", "snippet": "归母净利润"},
]
CLEAN_ITEMS = [
    {"title": TITLED, "url": GOOD_URL, "snippet": "营业收入 归母净利润"},
]


def _payload(items):
    return json.dumps(items, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 1 + 2. worker 侧：候选必须满足工具契约（空标题条目不得上行）
# ---------------------------------------------------------------------------

class TestCandidateContractClean(unittest.TestCase):
    class _Stub:
        """只借用 `_emit_payload` 的字段门槛与去重；相关性打分换恒等函数隔离。"""

        _emit_payload = wb.SearchAgent._emit_payload
        _search_bing = wb.SearchAgent._search_bing

        def _filter_results(self, instruction, items, min_score=None):
            return list(items)

    def test_empty_title_candidate_dropped_from_payload(self):
        out = self._Stub()._emit_payload("洋河股份 年报", list(EMPTY_TITLE_ITEMS))
        items = json.loads(out)
        self.assertEqual([it["url"] for it in items], [GOOD_URL],
                         "空标题条目必须被剔除，否则契约不通过")
        ok, issues = tool_contracts.validate_result("web_search", out)
        self.assertTrue(ok, f"上行载荷应通过工具契约：{issues}")

    def test_old_shape_really_violated_contract(self):
        """反向断言：修复前的原始候选列表确实过不了契约——这就是重试的触发点。"""
        ok, issues = tool_contracts.validate_result(
            "web_search", _payload(EMPTY_TITLE_ITEMS))
        self.assertFalse(ok, "空标题条目本应触发契约问题（重试根因）")
        self.assertTrue(any("title" in i for i in issues), issues)

    def test_all_empty_title_emits_nothing(self):
        only_empty = [{"title": "   ", "url": "https://x.example/1"}]
        self.assertIsNone(
            self._Stub()._emit_payload("洋河股份 年报", only_empty),
            "全无标题时不得回落到'随便发'：没有可交付候选就是没有")

    def test_bing_block_without_title_is_skipped(self):
        """无 `<h2>` 但有 `<a href>` 的块（旧实现产出空标题条目）不再算候选。"""
        html = (
            '<li class="b_algo"><h2><a href="https://good.example/1">洋河股份年报</a></h2>'
            '<p>营业收入</p></li>'
            '<li class="b_algo"><a href="https://notitle.example/2">x</a>'
            '<p>归母净利润</p></li>'
        ).encode("utf-8")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        import adapters.search_runner as sr
        with mock.patch.object(sr, "provider_timeout", lambda name, t=None: 5.0), \
                mock.patch.object(sr, "read_with_deadline",
                                  lambda resp, deadline: html), \
                mock.patch("urllib.request.urlopen", lambda req, timeout=None: _Resp()):
            rows = self._Stub()._search_bing("洋河股份 年报", 5)
        self.assertEqual([r["url"] for r in rows], ["https://good.example/1"],
                         "空标题块必须在解析处剔除（与 ddgs 分支同门槛）")


# ---------------------------------------------------------------------------
# 3. 任务级检索台账的只读余量
# ---------------------------------------------------------------------------

class TestSearchLedgerRemaining(unittest.TestCase):
    class _Redis:
        def __init__(self, raw=None, exc=None):
            self.raw, self.exc = raw, exc

        def hgetall(self, key):
            if self.exc:
                raise self.exc
            return self.raw or {}

    def test_used_up_reports_zero_calls(self):
        calls, secs = wb.search_ledger_remaining(
            self._Redis({"used": 6, "started": time.time()}), TASK)
        self.assertEqual(calls, 0, "额度用尽必须读成 0（编排器据此停止重试）")
        self.assertGreaterEqual(secs, 0.0)

    def test_bytes_fields_and_partial_use(self):
        total_calls, total_secs = wb.search_ledger_limits()
        raw = {b"used": b"2", b"started": str(time.time()).encode()}
        calls, secs = wb.search_ledger_remaining(self._Redis(raw), TASK)
        self.assertEqual(calls, max(0, total_calls - 2))
        self.assertLessEqual(secs, total_secs)

    def test_missing_ledger_is_full_budget(self):
        total_calls, total_secs = wb.search_ledger_limits()
        self.assertEqual(
            wb.search_ledger_remaining(self._Redis({}), TASK),
            (total_calls, total_secs),
            "从未检索过的任务：余量就是整份额度")

    def test_read_failure_returns_none_not_zero(self):
        """读不到 ≠ 没额度：返回 None 让调用方按旧行为重试（不许凭空判死）。"""
        self.assertIsNone(wb.search_ledger_remaining(
            self._Redis(exc=RuntimeError("redis down")), TASK))
        self.assertIsNone(wb.search_ledger_remaining(self._Redis({}), ""))


# ---------------------------------------------------------------------------
# 4. 编排器：预算耗尽时保留候选、不代做；真失败路径不变
# ---------------------------------------------------------------------------

class TestDispatchStepKeepsCandidates(unittest.TestCase):
    def _orch(self, retry=2, replan=1):
        o = make_orch(_max_retry=retry, _replan_depth=replan)
        o._now_iso = lambda: "t"
        o._cancel_requested = lambda tid: False
        o._task_goals = {TASK: GOAL}
        o._fetched_urls = lambda tid: ()
        o._replan_log = []
        o._replan_step = lambda *a, **k: (o._replan_log.append(a), None)[1]
        return o

    @staticmethod
    def _search_step():
        return {"step_id": "1", "capability": "web_search",
                "instruction": "检索洋河股份 2023/2024 年报", "timeout": 60}

    def _run(self, o, payloads):
        calls = {"n": 0}
        seq = list(payloads)

        def dispatch(step, _tid):
            calls["n"] += 1
            res = seq[min(calls["n"] - 1, len(seq) - 1)]
            return dict({"task_id": step.get("step_id")}, **res)

        o._dispatch = dispatch
        with mock.patch.object(ov.time, "sleep"), \
                mock.patch("step_diagnosis.write_step_failure") as wf:
            res = o._dispatch_step_safe(GOAL, self._search_step(), TASK,
                                        {"replan_used": 0})
        return res, calls, wf

    # -- 预算耗尽：停止重试 + 保留候选 -----------------------------------
    def test_budget_gone_stops_retry_and_keeps_candidates(self):
        o = self._orch()
        o._search_budget_left = lambda tid: (0, 30.0)
        res, calls, wf = self._run(o, [{
            "status": "SUCCESS", "result": _payload(EMPTY_TITLE_ITEMS),
            "search_status": {"status": "ok"}}])

        self.assertEqual(calls["n"], 1, "预算为 0：不再派发注定被拒的检索重试")
        self.assertEqual(res["status"], "SUCCESS", "不得把已取得候选的步骤判死")
        self.assertTrue(res.get("search_quality_untrusted"),
                        "保留候选必须如实标注'检索质量存疑'")
        self.assertEqual(res.get("kept_candidates"), 2)
        self.assertIn("title", str(res.get("contract_issue_note") or ""),
                      "契约问题原文要留在结果里（不许静默洗白）")
        self.assertEqual(res["search_status"]["status"], "ok",
                         "检索真值原样保留（覆盖率口径据此记账）")
        self.assertIn(GOOD_URL, [it["url"] for it in json.loads(res["result"])],
                      "结果数组仍是候选 JSON——下游抓取 URL 就取自这里")
        self.assertEqual(o._replan_log, [], "不得把检索步重规划给产不出 URL 的执行者")
        self.assertTrue(wf.called, "发生过契约失败仍要落盘结构化诊断")
        self.assertIn("title", str(wf.call_args[0][1]),
                      "诊断里要留下真实契约问题（first_issue，不是被拒空派发的空文本）")

    def test_fetch_step_gets_url_from_kept_candidates(self):
        """用户可见结果：抓取步从保留的候选里拿到 URL，不再'无候选 URL'跳过。"""
        o = self._orch()
        o._search_budget_left = lambda tid: (0, 30.0)
        res, _calls, _wf = self._run(o, [{
            "status": "SUCCESS", "result": _payload(EMPTY_TITLE_ITEMS),
            "search_status": {"status": "ok"}}])
        fetch = {"step_id": "2", "capability": "web_fetch",
                 "instruction": "抓取年报正文", "depends_on": ["1"], "timeout": 60}
        with mock.patch("working_paper_export.resolve_request",
                        side_effect=RuntimeError("unit test")):
            instr = o._inject_step_context(
                fetch, {"1": res}, threading.Lock(), TASK)
        self.assertIn(f"[URL: {GOOD_URL}]", instr,
                      "保留候选后抓取指令必须带上候选 URL")

    # -- 仍有余额：旧行为不变 -------------------------------------------
    def test_budget_left_still_retries(self):
        o = self._orch()
        o._search_budget_left = lambda tid: (3, 40.0)
        payload = {"status": "SUCCESS", "result": _payload(EMPTY_TITLE_ITEMS),
                   "search_status": {"status": "ok"}}
        res, calls, _wf = self._run(o, [payload])
        self.assertEqual(calls["n"], 1 + o._max_retry,
                         "仍有额度：照旧重试（本次修复不改这条路径）")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertTrue(res.get("search_quality_untrusted"))

    def test_clean_success_no_retry_no_flag(self):
        o = self._orch()
        o._search_budget_left = lambda tid: (0, 0.0)
        res, calls, wf = self._run(o, [{
            "status": "SUCCESS", "result": _payload(CLEAN_ITEMS),
            "search_status": {"status": "ok"}}])
        self.assertEqual(calls["n"], 1)
        self.assertEqual(res["status"], "SUCCESS")
        self.assertNotIn("search_quality_untrusted", res,
                         "契约通过的正常结果不得被标注存疑")
        self.assertFalse(wf.called)

    def test_no_candidates_keeps_failure_and_replans(self):
        """没有候选可保留时，如实失败并照旧走重规划（不许把失败洗成成功）。"""
        o = self._orch()
        o._search_budget_left = lambda tid: (3, 40.0)
        payload = {
            "status": "FAILED", "retryable": False,
            "result": "[]\n\n[NOT_RETRYABLE] 检索未获得可用结果（任务级检索预算不可用："
                      "已用尽（额度 0 次 / 剩余 36 秒））",
            "search_status": {"status": "refused_budget", "retryable": False},
        }
        res, calls, _wf = self._run(o, [payload])
        self.assertEqual(calls["n"], 1, "worker 判定不可重试：只派发一次")
        self.assertEqual(res["status"], "FAILED")
        self.assertFalse(res.get("search_quality_untrusted"))
        self.assertTrue(res.get("retry_skipped"))
        self.assertEqual(len(o._replan_log), 1, "无候选可保留：重规划路径不变")


if __name__ == "__main__":
    unittest.main()
