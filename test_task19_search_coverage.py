"""任务19：检索覆盖率可见化（报告末尾"数据来源摘要" + 任务详情 API `search_coverage`）。

覆盖设计文档 step 4 的四条：
1. 有 `search_degraded` 步的任务 → 摘要块里降级步 > 0（并给 ❌ 告警）；
2. 全部正常检索步 → 覆盖率 1.0、无告警；
3. 覆盖率 < 0.5 → 出现 ❌ 告警；
4. 无 web_search 步（纯代码分析）→ 不追加摘要块。

外加现实校对：文档给的口径假设步字段是**平的**（`s.get("search_degraded")`），而
`steps_json` 里真实形状是 `{"step_id","capability","instruction","result":{...}}`
（标记在 `result` 里）——两种形状都必须认；`timeout` 这类"没跑成"既不是"有来源"也不是
"零命中"，单独归一类，否则覆盖率会把没查成的步算成有来源。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import search_coverage as sc


def _ok(step_id: str = "1", agent: str = "search_agent") -> dict:
    """有来源的检索步（真实形状：标记在 `result` 里）。"""
    return {"step_id": step_id, "capability": "web_search",
            "result": {"status": "SUCCESS", "agent_id": agent,
                       "search_verdict": {"status": "ok", "completed": True,
                                          "zero_hits": False}}}


def _zero(step_id: str = "2") -> dict:
    """查询完成但零命中（`no_results`）——没来源。"""
    return {"step_id": step_id, "capability": "web_search",
            "result": {"status": "SUCCESS", "agent_id": "search_agent",
                       "search_status": {"status": "no_results"},
                       "search_verdict": {"status": "no_results", "zero_hits": True}}}


def _silent_success(step_id: str = "3") -> dict:
    """SUCCESS 但**没有任何检索真值**：就是"0 链接 Markdown"那类步。"""
    return {"step_id": step_id, "capability": "web_search",
            "result": {"status": "SUCCESS", "agent_id": "content_summarizer"}}


def _degraded(step_id: str = "4") -> dict:
    return {"step_id": step_id, "capability": "web_search",
            "result": {"status": "SUCCESS", "agent_id": "content_summarizer",
                       "worker_mismatch": True, "search_degraded": True}}


def _incomplete(step_id: str = "5") -> dict:
    return {"step_id": step_id, "capability": "web_search",
            "result": {"status": "FAILED", "agent_id": "search_agent",
                       "search_status": {"status": "timeout", "retryable": True},
                       "search_verdict": {"status": "timeout", "completed": False}}}


class TestSummarizeSearchCoverage(unittest.TestCase):
    def test_all_ok_is_full_coverage_without_alert(self):
        """文档用例 2：全部正常检索步 → 1.0，无告警。"""
        s = sc.summarize_search_coverage([_ok("1"), _ok("2")])
        self.assertEqual(s["total_search_steps"], 2)
        self.assertEqual(s["ok_steps"], 2)
        self.assertEqual(s["coverage_rate"], 1.0)
        self.assertEqual(sc.coverage_alert(s), "")
        block = sc.render_coverage_block(s, rules_version="v9")
        self.assertIn("| 有效覆盖率 | 100% |", block)
        self.assertNotIn("⚠️", block)
        self.assertNotIn("❌", block)

    def test_degraded_step_lowers_coverage_and_warns_hard(self):
        """文档用例 1：有降级步 → degraded > 0，且降级一律 ❌（不管理论覆盖率多高）。"""
        s = sc.summarize_search_coverage([_ok("1"), _ok("2"), _degraded("3")])
        self.assertEqual(s["degraded_steps"], 1)
        self.assertEqual(s["coverage_rate"], 0.67)
        self.assertIn("❌", sc.coverage_alert(s))
        block = sc.render_coverage_block(s)
        self.assertIn("| 降级执行步 | 1（由非检索模型完成，来源可能为空）|", block)
        self.assertIn("> ❌ 检索质量严重不足，报告结论需谨慎参考", block)

    def test_low_coverage_warns_hard(self):
        """文档用例 3：覆盖率 < 0.5 → ❌。"""
        s = sc.summarize_search_coverage([_ok("1"), _zero("2"), _zero("3"), _zero("4")])
        self.assertEqual(s["coverage_rate"], 0.25)
        self.assertIn("❌", sc.coverage_alert(s))

    def test_mid_coverage_warns_soft(self):
        """0.5–0.9：⚠️（不是 ❌）。"""
        s = sc.summarize_search_coverage([_ok("1"), _ok("2"), _ok("3"), _zero("4")])
        self.assertEqual(s["coverage_rate"], 0.75)
        self.assertIn("⚠️", sc.coverage_alert(s))
        self.assertNotIn("❌", sc.coverage_alert(s))

    def test_no_search_steps_gives_none_and_no_block(self):
        """文档用例 4：纯代码/离线任务 → 不追加摘要块。"""
        s = sc.summarize_search_coverage([
            {"step_id": "1", "capability": "code_execution", "result": {"status": "SUCCESS"}},
            {"step_id": "2", "capability": "report_generator", "result": {"status": "SUCCESS"}},
        ])
        self.assertEqual(s["total_search_steps"], 0)
        self.assertIsNone(s["coverage_rate"])
        self.assertEqual(sc.render_coverage_block(s), "")

    def test_web_fetch_is_not_a_search_step(self):
        """`web_fetch` 是按已知 URL 取正文，不参与检索覆盖率。"""
        s = sc.summarize_search_coverage([
            {"step_id": "1", "capability": "web_fetch", "result": {"status": "SUCCESS"}}])
        self.assertEqual(s["total_search_steps"], 0)

    def test_flat_shape_is_also_read(self):
        """文档里的**平铺**形状（标记在步顶层）同样要认，且与真实形状同解。"""
        flat = [{"step_id": "1", "capability": "web_search",
                 "status": "SUCCESS", "search_degraded": True}]
        nested = [_degraded("1")]
        self.assertEqual(sc.summarize_search_coverage(flat),
                         sc.summarize_search_coverage(nested))
        self.assertEqual(sc.summarize_search_coverage(flat)["degraded_steps"], 1)

    def test_incomplete_is_not_counted_as_source(self):
        """`timeout` 这类"根本没查成"不算来源（文档公式会把它算成有效，这里不跟随）。"""
        s = sc.summarize_search_coverage([_ok("1"), _incomplete("2")])
        self.assertEqual(s["incomplete_steps"], 1)
        self.assertEqual(s["coverage_rate"], 0.5)
        block = sc.render_coverage_block(s)
        self.assertIn("| 未完成检索步 | 1（超时/限流/预算未发请求等）|", block)

    def test_buckets_are_mutually_exclusive(self):
        """既降级又没来源的步只记一次——否则覆盖率会被扣两次、算出负数。"""
        step = _degraded("1")
        step["result"]["search_verdict"] = {"status": "no_results"}
        s = sc.summarize_search_coverage([step])
        self.assertEqual(s["degraded_steps"], 1)
        self.assertEqual(s["zero_source_steps"], 0)
        self.assertEqual(s["total_search_steps"], 1)
        self.assertEqual(s["coverage_rate"], 0.0)
        self.assertGreaterEqual(s["coverage_rate"], 0.0)

    def test_silent_success_is_zero_source(self):
        """SUCCESS 且无检索真值 = 无来源事实（不假装有来源）。"""
        s = sc.summarize_search_coverage([_silent_success("1")])
        self.assertEqual(s["zero_source_steps"], 1)
        self.assertEqual(s["coverage_rate"], 0.0)

    def test_junk_input_does_not_raise(self):
        for junk in (None, {}, "steps", 0, [None, 1, "x", {}]):
            with self.subTest(junk=junk):
                s = sc.summarize_search_coverage(junk)
                self.assertEqual(s["total_search_steps"], 0)
                self.assertIsNone(s["coverage_rate"])

    def test_footer_carries_rules_version_and_signature(self):
        block = sc.render_coverage_block(sc.summarize_search_coverage([_ok("1")]),
                                        rules_version="v1.2.3")
        self.assertIn(sc.FOOTER_SIGNATURE, block)
        self.assertIn("验收版本 v1.2.3", block)
        self.assertTrue(block.lstrip().startswith("---"))
        self.assertIn("## 数据来源摘要", block)
        self.assertIn("| 检索步总数 | 1 |", block)


class TestReportBlockInjection(unittest.TestCase):
    """`delivery_pipeline._attach_search_coverage`：只追加、幂等、读不到就原样返回。"""

    BODY = "# 研究简报\n\n正文一段。\n"

    def _attach(self, report: str, steps=None, projection=None, exc=None):
        import delivery_pipeline as dp
        if exc is not None:
            patcher = mock.patch.object(
                __import__("task_state"), "task_projection",
                side_effect=exc)
        else:
            patcher = mock.patch.object(
                __import__("task_state"), "task_projection",
                return_value=projection if projection is not None else {"steps": steps or []})
        with patcher:
            return dp._attach_search_coverage(report, "t-cov", steps=steps)

    def test_footer_appended_after_body(self):
        out = self._attach(self.BODY, steps=[_ok("1"), _degraded("2")])
        self.assertTrue(out.startswith(self.BODY.rstrip("\n")))
        self.assertIn("## 数据来源摘要", out)
        self.assertIn("| 检索步总数 | 2 |", out)
        self.assertIn("| 有效覆盖率 | 50% |", out)
        self.assertIn("❌ 检索质量严重不足", out)
        self.assertLess(out.index("正文一段。"), out.index("## 数据来源摘要"))

    def test_no_search_steps_leaves_report_untouched(self):
        """纯代码分析任务：报告逐字节不变。"""
        out = self._attach(self.BODY, steps=[{"step_id": "1",
                                              "capability": "code_execution",
                                              "result": {"status": "SUCCESS"}}])
        self.assertEqual(out, self.BODY)

    def test_projection_failure_is_silent(self):
        """步骤读不到（DB 挂了/没有这行）→ 原样返回，不阻断交付。"""
        out = self._attach(self.BODY, exc=RuntimeError("db down"))
        self.assertEqual(out, self.BODY)

    def test_attach_is_idempotent(self):
        first = self._attach(self.BODY, steps=[_ok("1")])
        second = self._attach(first, steps=[_ok("1")])
        self.assertEqual(second, first)
        self.assertEqual(second.count("## 数据来源摘要"), 1)

    def test_stale_block_is_replaced_by_current_numbers(self):
        stale = self._attach(self.BODY, steps=[_degraded("1"), _zero("2")])
        self.assertIn("| 有效覆盖率 | 0% |", stale)
        fresh = self._attach(stale, steps=[_ok("1"), _ok("2")])
        self.assertIn("| 有效覆盖率 | 100% |", fresh)
        self.assertNotIn("❌", fresh)
        self.assertEqual(fresh.count("## 数据来源摘要"), 1)

    def test_stale_block_removed_when_no_search_steps(self):
        """上一版有检索、这一版没有：旧摘要块不得留在新稿里。"""
        stale = self._attach(self.BODY, steps=[_ok("1")])
        out = self._attach(stale, steps=[{"step_id": "1", "capability": "package",
                                          "result": {"status": "SUCCESS"}}])
        self.assertNotIn("数据来源摘要", out)
        self.assertIn("正文一段。", out)

    def test_explicit_steps_win_over_db(self):
        """编排器收尾路径：显式传本次运行步骤，DB 读不到也要出摘要块。"""
        out = self._attach(self.BODY, steps=[_ok("1")], exc=RuntimeError("no row yet"))
        self.assertIn("## 数据来源摘要", out)


class TestTaskDetailPayload(unittest.TestCase):
    """走真实 `_get_task_page`（`GET /task/<id>`）：payload 必须带 `search_coverage`。"""

    def _handler(self, captured):
        class _H:
            def _json(self, data, code=200, extra_headers=None):
                captured.update(data=data, code=code)
        return _H()

    def _call(self, steps):
        import web_ui
        captured = {}
        task = {
            "task_id": "t-cov", "status": "SUCCESS_WITH_ISSUES", "goal": "目标",
            "steps": steps, "report": "正文", "logs": [], "project": "default",
            "revision": False, "acceptance": None, "llm_degraded": None,
        }
        with mock.patch.object(web_ui, "_task_results", {}), \
                mock.patch("task_state.merge_projection", return_value=task):
            web_ui._get_task_page(self._handler(captured), "/task/t-cov")
        return captured["data"]

    def test_payload_carries_summary(self):
        data = self._call([_ok("1"), _degraded("2"), _zero("3")])
        cov = data["search_coverage"]
        self.assertEqual(cov["total_search_steps"], 3)
        self.assertEqual(cov["ok_steps"], 1)
        self.assertEqual(cov["degraded_steps"], 1)
        self.assertEqual(cov["zero_source_steps"], 1)
        self.assertEqual(cov["coverage_rate"], 0.33)

    def test_no_search_steps_gives_null(self):
        """没有检索步 → null（前端不显示该块），不用 0% 冒充"检索质量差"。"""
        data = self._call([{"step_id": "1", "capability": "code_execution",
                            "result": {"status": "SUCCESS"}}])
        self.assertIsNone(data["search_coverage"])

    def test_summary_failure_does_not_break_the_page(self):
        data = self._call([_ok("1")])
        self.assertIsNotNone(data)          # 页面照常返回
        self.assertIn("search_coverage", data)


class TestWiring(unittest.TestCase):
    def test_assembly_choke_point_attaches_footer(self):
        src = Path("delivery_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("report = _attach_search_coverage(report, task_id, steps=steps)", src)
        self.assertIn("steps: list | None = None", src)
        self.assertIn("def _attach_search_coverage(report: str, task_id: str, steps=None)",
                      src)

    def test_orchestrator_passes_run_steps_and_logs(self):
        src = Path("orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertIn("steps=_cov_steps)", src)
        self.assertIn("_summarize_search_coverage", src)
        self.assertIn("检索覆盖率（task=%s）", src)

    def test_task_detail_payload_exposes_key(self):
        src = Path("web_ui.py").read_text(encoding="utf-8")
        self.assertIn('"search_coverage": search_coverage,', src)
        self.assertIn('_sc.summarize_search_coverage(data.get("steps") or [])', src)

    def test_summary_is_not_written_into_steps_json(self):
        """摘要不落库：伪步骤会被前端当一步渲染，也会污染步级指标聚合。"""
        src = Path("orchestrator_v2.py").read_text(encoding="utf-8")
        self.assertNotIn("__search_summary", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
