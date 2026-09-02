"""任务耗时架构优化回归测试。

覆盖：
- content_summary 合并调用：一次 LLM 调用同时产出 {summary, charts}；
  格式不兼容/调用失败时回退旧的两次调用路径。
- 反思循环收敛：best_report 无改善提前终止；有改善继续；
  验收 fail 仍强制重做。
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import workspace as ws_mod


def _valid_chart_specs() -> list[dict]:
    return [{
        "question": "营收变化趋势？",
        "conclusion": "营收从6602.57亿元增长到7517.66亿元。",
        "type": "line",
        "title": "腾讯营收（2024-2025年，单位：亿元）",
        "x_axis_title": "年份",
        "y_axis_title": "营收（亿元）",
        "unit": "亿元",
        "section_hint": "财务分析",
        "time_range": "2024-2025年",
        "region": "全球",
        "source": "财报",
        "sample_size": "2",
        "annotation": "数据来自财报",
        "missing": "无",
        "outliers": "无",
        "data": [
            {"label": "2024", "value": 6602.57, "year": 2024, "source": "财报"},
            {"label": "2025", "value": 7517.66, "year": 2025, "source": "财报"},
        ],
    }]


SUMMARY_TEXT = (
    "# 腾讯财报总结\n\n"
    "2024年营收6602.57亿元，2025年营收7517.66亿元。"
)


class TestContentSummaryMergedCall(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # 禁用本地 LoRA 优先路径：worker 测试走 mock 的云端调用链。
        # 同时设环境变量与模块属性（防 lora_client 被其他测试 reload 后失效）
        self._old_lora = os.environ.get("WM_USE_LOCAL_LORA")
        os.environ["WM_USE_LOCAL_LORA"] = "0"
        try:
            import lora_client as _lc
            self._old_lora_enabled = _lc._ENABLED
            _lc._ENABLED = False
        except Exception:
            self._old_lora_enabled = None
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old_lora is None:
            os.environ.pop("WM_USE_LOCAL_LORA", None)
        else:
            os.environ["WM_USE_LOCAL_LORA"] = self._old_lora
        try:
            import lora_client as _lc
            if self._old_lora_enabled is not None:
                _lc._ENABLED = self._old_lora_enabled
        except Exception:
            pass

    async def _run_worker(self, instruction="分析腾讯财报并生成总结"):
        from workers.content_summary_worker import ContentSummaryWorker
        w = ContentSummaryWorker.__new__(ContentSummaryWorker)
        return await w.execute(instruction)

    async def test_merged_call_completes_in_one_llm_call(self):
        """合法 {summary, charts} 输出 → 仅一次 LLM 调用完成，复用图表管线。"""
        calls = {"stream": 0, "llm": 0}

        def fake_stream(system, user, **kwargs):
            calls["stream"] += 1
            return json.dumps({
                "summary": SUMMARY_TEXT,
                "charts": _valid_chart_specs(),
            }, ensure_ascii=False)

        def fake_llm(*args, **kwargs):
            calls["llm"] += 1
            raise AssertionError("合并调用成功时不应再调用 call_llm")

        with mock.patch("llm_client.call_llm_stream", fake_stream), \
             mock.patch("llm_client.call_llm", fake_llm):
            out = await self._run_worker()

        self.assertEqual(calls["stream"], 1, "合并调用应只触发一次 LLM 调用")
        self.assertEqual(calls["llm"], 0)
        self.assertIn(SUMMARY_TEXT, out)
        self.assertIn("[CHART_DATA]", out)
        self.assertIn("7517.66", out)

    async def test_incompatible_format_falls_back_to_two_calls(self):
        """合并输出缺 charts（格式不兼容）→ 回退旧路径：总结 + 提取两次调用。"""
        calls = {"stream": 0, "llm": 0}

        def fake_stream(system, user, **kwargs):
            calls["stream"] += 1
            if calls["stream"] == 1:
                # 合并调用：缺少 charts 键 → 格式不兼容
                return json.dumps({"summary": "不兼容输出"}, ensure_ascii=False)
            return SUMMARY_TEXT  # 回退路径第一次调用（总结）

        def fake_llm(system, user, expect_json=True, **kwargs):
            calls["llm"] += 1
            return {"content": json.dumps(
                {"charts": _valid_chart_specs()}, ensure_ascii=False
            )}

        with mock.patch("llm_client.call_llm_stream", fake_stream), \
             mock.patch("llm_client.call_llm", fake_llm):
            out = await self._run_worker()

        self.assertEqual(calls["stream"], 2, "回退路径：合并 1 次 + 旧总结 1 次")
        self.assertEqual(calls["llm"], 1, "回退路径：图表提取 1 次")
        self.assertIn(SUMMARY_TEXT, out)
        self.assertIn("[CHART_DATA]", out)

    async def test_merged_call_exception_falls_back(self):
        """合并调用抛异常 → 回退旧两次调用路径（总结 + 提取）。"""
        calls = {"stream": 0, "llm": 0}

        def fake_stream(system, user, **kwargs):
            calls["stream"] += 1
            if calls["stream"] == 1:
                raise RuntimeError("merged LLM unavailable")
            return SUMMARY_TEXT

        def fake_llm(system, user, expect_json=True, **kwargs):
            calls["llm"] += 1
            if calls["llm"] == 1:
                # 合并路径的流式回退也会调用 call_llm（仍失败/返回空由格式判定）
                return {"content": "not json"}
            return {"content": json.dumps(
                {"charts": _valid_chart_specs()}, ensure_ascii=False
            )}

        with mock.patch("llm_client.call_llm_stream", fake_stream), \
             mock.patch("llm_client.call_llm", fake_llm):
            out = await self._run_worker()

        self.assertEqual(calls["stream"], 2)
        self.assertEqual(calls["llm"], 2)
        self.assertIn(SUMMARY_TEXT, out)
        self.assertIn("[CHART_DATA]", out)


class TestReflectionConvergence(unittest.TestCase):
    """反思循环收敛：best_report 无改善提前终止；有改善继续；验收 fail 仍重做。"""

    def _orch(self, **overrides):
        from test_orchestrator_v2 import make_orch
        kwargs = dict(_max_iterations=5)
        kwargs.update(overrides)
        o = make_orch(**kwargs)
        o._plan = lambda goal, task_id, context="", memory_context="": [
            {"step_id": "1", "capability": "content_summary",
             "instruction": "x", "timeout": 120}
        ]
        o._now_iso = lambda: "t"
        return o

    def test_convergence_stops_when_best_report_unchanged(self):
        o = self._orch()
        o._execute_steps = lambda steps, task_id, goal: (
            [{"task_id": s["step_id"], "status": "SUCCESS",
              "result": "# 报告" + "A" * 300} for s in steps],
            False,
        )
        reflected = {"n": 0}

        def fake_reflect(goal, report, task_id, all_steps=None, completed_all=None,
                         memory_context="", validator_summary="", eval_scores=""):
            reflected["n"] += 1
            return {"accepted": False, "score": 4.0, "gaps": ["缺图"],
                    "next_steps": [{"step_id": "x", "capability": "content_summary",
                                    "instruction": "补", "timeout": 120}]}

        o._reflect = fake_reflect
        res = o.run("t-conv-stop", "目标", auto_run=True)

        self.assertEqual(reflected["n"], 1, "best_report 不变应提前终止反思")
        self.assertEqual(len(res["steps"]), 2, "初始 1 步 + 反思追加 1 步")
        msgs = [m.get("payload", {}).get("message", "")
                for _, m in o._messaging.published if isinstance(m, dict)]
        self.assertTrue(
            any("best_report 未改善" in s for s in msgs),
            "提前终止时应记录收敛原因",
        )

    def test_convergence_continues_when_best_report_improves(self):
        o = self._orch()
        rounds = [
            [{"task_id": "1", "status": "SUCCESS",
              "result": "# 第一轮" + "A" * 300}],
            [{"task_id": "i1-1", "status": "SUCCESS",
              "result": "# 第二轮更完整" + "B" * 400}],
        ]
        calls = {"n": 0}

        def fake_execute(steps, task_id, goal):
            i = calls["n"]
            calls["n"] += 1
            return (rounds[i] if i < len(rounds) else []), False

        o._execute_steps = fake_execute
        reflected = {"n": 0}

        def fake_reflect(goal, report, task_id, all_steps=None, completed_all=None,
                         memory_context="", validator_summary="", eval_scores=""):
            reflected["n"] += 1
            if reflected["n"] == 1:
                return {"accepted": False, "score": 4.0, "gaps": ["补充"],
                        "next_steps": [{"step_id": "x", "capability": "content_summary",
                                        "instruction": "补充", "timeout": 120}]}
            return {"accepted": True, "score": 9.0, "verdict": "accept"}

        o._reflect = fake_reflect
        res = o.run("t-conv-go", "目标", auto_run=True)

        self.assertEqual(reflected["n"], 2, "best_report 有改善应继续反思")
        self.assertEqual(res["status"], "SUCCESS")
        self.assertEqual(len(res["steps"]), 2)

    def test_acceptance_fail_still_redoes_despite_unchanged_report(self):
        """验收 fail 覆盖反思评分：即使 best_report 长度不变也必须继续重做。"""
        o = self._orch(_max_iterations=4)
        o._execute_steps = lambda steps, task_id, goal: (
            [{"task_id": s["step_id"], "status": "SUCCESS",
              "result": "# 报告" + "A" * 300} for s in steps],
            False,
        )
        reflected = {"n": 0}

        def fake_reflect(goal, report, task_id, all_steps=None, completed_all=None,
                         memory_context="", validator_summary="", eval_scores=""):
            reflected["n"] += 1
            return {"accepted": True, "score": 9.0, "verdict": "accept"}

        o._reflect = fake_reflect
        tmp = tempfile.mkdtemp(prefix="wm_conv_accfail_")
        old_root = ws_mod.WORKSPACE_ROOT
        ws_mod.configure_workspace_root(tmp)
        try:
            ws = ws_mod.task_workspace("t-conv-accfail", "default")
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "acceptance_report.json").write_text(json.dumps({
                "overall": "fail",
                "gaps": ["数字溯源率不足"],
            }, ensure_ascii=False), encoding="utf-8")
            res = o.run("t-conv-accfail", "目标", auto_run=True)
            self.assertGreaterEqual(
                reflected["n"], 2,
                "验收 fail 时不得因 best_report 长度不变而提前终止",
            )
            self.assertGreaterEqual(len(res["steps"]), 2, "验收 fail 必须产出重做步骤")
        finally:
            ws_mod.WORKSPACE_ROOT = old_root
            shutil.rmtree(tmp, ignore_errors=True)


class TestLocalLoraClient(unittest.TestCase):
    """本地 QLoRA 推理客户端：探活/生成/回退语义。"""

    def test_disabled_returns_none(self):
        import lora_client as lc
        old = lc._ENABLED
        try:
            lc._ENABLED = False
            self.assertFalse(lc._service_alive())
            self.assertIsNone(lc.local_generate("测试", timeout=1))
        finally:
            lc._ENABLED = old

    def test_alive_check_short_circuit(self):
        """服务不可达 → 快速返回 None（不等待 HTTP 超时）。"""
        import lora_client as lc
        old_url, old_en = lc._cache["url"], lc._ENABLED
        try:
            lc._cache["url"] = "http://127.0.0.1:1"  # 必然不可达
            lc._ENABLED = True
            self.assertIsNone(lc.local_generate("测试", timeout=1))
        finally:
            lc._cache["url"], lc._ENABLED = old_url, old_en

    def test_generate_mock_success(self):
        import lora_client as lc
        from unittest import mock

        with mock.patch.object(lc, "_service_alive", return_value=True), \
                mock.patch.object(lc.urllib.request, "urlopen") as m_urlopen:
            class FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({
                        "status": "success",
                        "summary": "# 测试总结\n\n这是一段足够长的正文内容，用于通过最短长度校验。",
                        "charts": [{"type": "bar"}],
                    }, ensure_ascii=False).encode("utf-8")
            m_urlopen.return_value = FakeResp()
            r = lc.local_generate("测试指令", timeout=5)
            self.assertIsNotNone(r)
            self.assertIn("测试总结", r["summary"])
            self.assertEqual(len(r["charts"]), 1)

    def test_generate_mock_failure_falls_back(self):
        """服务异常 → 返回 None（调用方回退云端）。"""
        import lora_client as lc
        from unittest import mock

        with mock.patch.object(lc, "_service_alive", return_value=True), \
                mock.patch.object(lc.urllib.request, "urlopen", side_effect=OSError("conn refused")):
            self.assertIsNone(lc.local_generate("测试", timeout=1))

    def test_short_summary_rejected(self):
        """输出过短（<20 字符）→ 拒绝并回退云端。"""
        import lora_client as lc
        from unittest import mock

        with mock.patch.object(lc, "_service_alive", return_value=True), \
                mock.patch.object(lc.urllib.request, "urlopen") as m_urlopen:
            class FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({
                        "status": "success",
                        "summary": "太短",
                        "charts": [],
                    }, ensure_ascii=False).encode("utf-8")
            m_urlopen.return_value = FakeResp()
            self.assertIsNone(lc.local_generate("测试", timeout=5))


if __name__ == "__main__":
    unittest.main()
