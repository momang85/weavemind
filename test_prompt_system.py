# -*- coding: utf-8 -*-
"""提示词体系测试：步骤信封、注册表覆盖、反思/自迭代闭环。"""
import json
import os
import tempfile
import unittest
from unittest import mock


class _FakeMessaging:
    def __init__(self):
        self.published = []

    def publish(self, channel, msg):
        self.published.append((channel, msg))


class TestStepEnvelope(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("WEAVEMIND_PROMPTS_DIR")
        self._tmp = tempfile.mkdtemp(prefix="wmenv_")
        os.environ["WEAVEMIND_PROMPTS_DIR"] = self._tmp

    def tearDown(self):
        if self._old:
            os.environ["WEAVEMIND_PROMPTS_DIR"] = self._old
        else:
            os.environ.pop("WEAVEMIND_PROMPTS_DIR", None)

    def test_envelope_contains_six_elements(self):
        from step_envelope import build_envelope

        for cap in ("content_summary", "code_execution", "report_generator", "web_search"):
            env = build_envelope(cap, "请分析2025年AI芯片市场并生成可视化报告")
            self.assertIn("【角色】", env, cap)
            self.assertIn("【受众】", env, cap)
            self.assertIn("【输出要求】", env, cap)
            self.assertIn("【质量标准】", env, cap)

    def test_unknown_capability_gets_minimal_envelope(self):
        from step_envelope import build_envelope

        env = build_envelope("unknown_cap", "目标")
        self.assertIn("【任务目标】", env)
        self.assertIn("目标", env)


class TestPromptRegistry(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("WEAVEMIND_PROMPTS_DIR")
        self._tmp = tempfile.mkdtemp(prefix="wmprompt_")
        os.environ["WEAVEMIND_PROMPTS_DIR"] = self._tmp

    def tearDown(self):
        if self._old:
            os.environ["WEAVEMIND_PROMPTS_DIR"] = self._old
        else:
            os.environ.pop("WEAVEMIND_PROMPTS_DIR", None)

    def test_fallback_returns_default(self):
        from prompt_registry import get_prompt

        self.assertEqual(get_prompt("planner", "DEFAULT"), "DEFAULT")

    def test_record_and_read_override(self):
        from prompt_registry import get_prompt, record_override

        ok, issues = record_override(
            "planner",
            "你是资深规划专家。【受众】下游编排引擎（机器可读）。"
            "【输出要求】严格JSON，包含 steps 数组。"
            "【质量标准】每步指令必须自带角色、受众、输出要求与验收标准，"
            "把用户目标拆成可执行的步骤。",
            "测试理由",
            "t-1",
        )
        self.assertTrue(ok, issues)
        self.assertIn("规划专家", get_prompt("planner", "DEFAULT"))
        self.assertEqual(get_prompt("planner", "DEFAULT").count("规划专家"), 1)

    def test_override_appends_not_replaces(self):
        from prompt_registry import get_prompt, record_override

        fix = ("补充要求：每个步骤必须标注受众（按目标推断）、输出格式示例"
               "与可验证的验收标准；缺失以上要素视为不合格，需重写该步骤指令。")
        ok, issues = record_override("planner", fix, "追加式改进", "t-1")
        self.assertTrue(ok, issues)
        got = get_prompt("planner", "DEFAULT_PLANNER")
        self.assertIn("DEFAULT_PLANNER", got, "默认提示词必须保留")
        self.assertIn("【自迭代改进】", got)
        self.assertIn("必须标注受众", got)

    def test_invalid_override_rejected(self):
        from prompt_registry import record_override

        ok, issues = record_override("planner", "太短", "", "t-2")
        self.assertFalse(ok)
        self.assertTrue(issues)
        # 危险操作示例也被拒绝
        ok2, issues2 = record_override(
            "planner", "你是X。输出要求：删除文件示例 rm -rf /tmp。其他要求：完整提示词内容。", "x", "t-3"
        )
        self.assertFalse(ok2, issues2)

    def test_version_increments(self):
        from prompt_registry import load_overrides, record_override

        fix = ("你是资深规划专家。【受众】下游编排引擎（机器可读）。"
               "【输出要求】严格JSON，包含 steps 数组。"
               "【质量标准】每步指令必须自带角色、受众、输出要求与验收标准。")
        record_override("planner", fix, "v1", "t-1")
        record_override("planner", fix, "v2", "t-2")
        # 基线源码 v1 → 第一次覆盖 v2 → 第二次覆盖 v3
        self.assertEqual(load_overrides()["planner"]["version"], 3)


class TestReflectionUsesRegistry(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("WEAVEMIND_PROMPTS_DIR")
        self._tmp = tempfile.mkdtemp(prefix="wmreflect_")
        os.environ["WEAVEMIND_PROMPTS_DIR"] = self._tmp

    def tearDown(self):
        if self._old:
            os.environ["WEAVEMIND_PROMPTS_DIR"] = self._old
        else:
            os.environ.pop("WEAVEMIND_PROMPTS_DIR", None)

    def test_reflect_uses_registry_override(self):
        from orchestrator_v2 import ITERATOR_SYSTEM, OrchestratorV2
        from prompt_registry import record_override

        record_override(
            "reflect",
            "你是测试评审员。输出严格JSON：{\"score\":10,\"verdict\":\"accept\",\"gaps\":[],\"next_steps\":[]}",
            "测试",
            "t-1",
        )
        o = OrchestratorV2.__new__(OrchestratorV2)
        captured = {}

        class FakeLLM:
            def call(self, system, prompt, expect_json=True, **kwargs):
                captured["system"] = system
                return {"score": 10, "verdict": "accept", "gaps": [], "next_steps": []}

        o._planner_llm = FakeLLM()
        o._messaging = _FakeMessaging()
        o._reflect("目标", "报告", "t1", [], {}, "")
        self.assertIn("测试评审员", captured["system"])
        self.assertNotEqual(captured["system"], ITERATOR_SYSTEM)


class TestPromptRefinery(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("WEAVEMIND_PROMPTS_DIR")
        self._tmp = tempfile.mkdtemp(prefix="wmrefine_")
        os.environ["WEAVEMIND_PROMPTS_DIR"] = self._tmp

    def tearDown(self):
        if self._old:
            os.environ["WEAVEMIND_PROMPTS_DIR"] = self._old
        else:
            os.environ.pop("WEAVEMIND_PROMPTS_DIR", None)

    def test_refinery_applies_override(self):
        import llm_client
        from prompt_registry import load_overrides
        from prompt_refinery import refine_after_task

        orig = llm_client.call_llm

        def fake_call(system, user, expect_json=True):
            return {"content": json.dumps({
                "findings": [{
                    "target": "step:code_execution",
                    "issue": "缺少受众与验收标准",
                    "evidence": "指令只有目标",
                    "fix_prompt": (
                        "【角色】资深工程师。【受众】最终用户（需可玩）。"
                        "【输出要求】可运行的代码文件。【质量标准】必须可直接运行。"
                    ),
                    "rationale": "补齐四要素，提升可运行性",
                }],
                "apply": True,
            }, ensure_ascii=False)}

        llm_client.call_llm = fake_call
        try:
            res = refine_after_task(_FakeMessaging(), "t1", "目标", [], {}, "报告", {})
            self.assertTrue(res["ran"])
            self.assertEqual(res["applied"], 1)
            data = load_overrides()
            self.assertIn("step:code_execution", data)
            self.assertEqual(data["step:code_execution"]["version"], 2)
        finally:
            llm_client.call_llm = orig

    def test_refinery_no_apply_when_empty_findings(self):
        import llm_client
        from prompt_registry import load_overrides
        from prompt_refinery import refine_after_task

        orig = llm_client.call_llm
        llm_client.call_llm = lambda system, user, expect_json=True: {
            "content": json.dumps({"findings": [], "apply": False})
        }
        try:
            res = refine_after_task(_FakeMessaging(), "t1", "目标", [], {}, "报告", {})
            self.assertTrue(res["ran"])
            self.assertEqual(res["applied"], 0)
            self.assertEqual(load_overrides(), {})
        finally:
            llm_client.call_llm = orig

    def test_refinery_skips_non_ui_task(self):
        from prompt_refinery import _maybe_run

        res = _maybe_run(
            _FakeMessaging(), "t-cap-1", "目标", [], {}, "报告",
            {"has_failure": True},
        )
        self.assertFalse(res["ran"])
        self.assertEqual(res["reason"], "not_ui_task")


class TestSearchKeywordsIgnoreEnvelope(unittest.TestCase):
    def test_envelope_words_not_used_as_keywords(self):
        from worker_base import SearchAgent

        sa = SearchAgent.__new__(SearchAgent)
        text = (
            "用户目标：请分析2025年全球AI芯片市场并生成可视化报告。\n"
            "原始指令：搜索全球AI芯片市场规模数据。\n\n"
            "【角色】信息检索专家。【受众】下游分析引擎。"
            "【输出要求】严格JSON数组。【质量标准】必须带原始URL。"
        )
        kw = sa._extract_keywords(text)
        words = set(kw.split())
        self.assertNotIn("角色", words)
        self.assertNotIn("受众", words)
        self.assertNotIn("质量标准", words)
        self.assertTrue(any("芯片" in w for w in words), f"关键词应含主题词: {words}")


class _FakeMemory:
    """带提示词 RAG 方法的假记忆，记录调用。"""

    def __init__(self):
        self.queries = []
        self.records = []

    def query_prompt_refinements(self, goal, n=3, threshold=None):
        self.queries.append(goal)
        return ["[step:code_execution] 提示词进化记录（step:code_execution v2） 改进：必须可运行"]

    def add_prompt_refinement(self, **kwargs):
        self.records.append(kwargs)


class TestPromptRagWiring(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("WEAVEMIND_PROMPTS_DIR")
        self._tmp = tempfile.mkdtemp(prefix="wmrag_")
        os.environ["WEAVEMIND_PROMPTS_DIR"] = self._tmp

    def tearDown(self):
        if self._old:
            os.environ["WEAVEMIND_PROMPTS_DIR"] = self._old
        else:
            os.environ.pop("WEAVEMIND_PROMPTS_DIR", None)

    def test_query_prompt_hints_uses_memory(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        mem = _FakeMemory()
        o._memory = mem
        o._messaging = _FakeMessaging()
        hints = o._query_prompt_hints("请分析AI芯片市场", "t1")
        self.assertEqual(len(hints), 1)
        self.assertIn("step:code_execution", hints[0])
        self.assertEqual(mem.queries, ["请分析AI芯片市场"])

    def test_record_reflection_refinement(self):
        from orchestrator_v2 import OrchestratorV2

        o = OrchestratorV2.__new__(OrchestratorV2)
        mem = _FakeMemory()
        o._memory = mem
        o._record_reflection_refinement(
            "目标", "t1", "step:code_execution",
            "代码不可运行", "优化后指令…",
        )
        self.assertEqual(len(mem.records), 1)
        self.assertEqual(mem.records[0]["key"], "step:code_execution")
        self.assertEqual(mem.records[0]["outcome"], "reflection")

    def test_envelope_carries_rag_hints(self):
        from step_envelope import build_envelope

        hints = [
            "[step:code_execution] 提示词进化记录（step:code_execution v2） 改进：必须可直接运行",
            "[reflect] 提示词进化记录（reflect v2） 改进：指令要带验收标准",
        ]
        env = build_envelope("code_execution", "目标", hints)
        self.assertIn("【角色】", env)
        self.assertIn("【历史改进经验（RAG）】", env)
        self.assertIn("必须可直接运行", env)
        # 无关能力的提示不进本步骤信封
        env2 = build_envelope("web_search", "目标", hints)
        self.assertNotIn("必须可直接运行", env2)

    def test_refinery_records_into_rag_when_memory_given(self):
        import json
        import llm_client
        from prompt_refinery import refine_after_task

        mem = _FakeMemory()
        orig = llm_client.call_llm

        def fake_call(system, user, expect_json=True):
            return {"content": json.dumps({
                "findings": [{
                    "target": "step:web_search",
                    "issue": "关键词被信封词污染",
                    "evidence": "查询含角色/受众",
                    "fix_prompt": (
                        "补充要求：搜索关键词必须只从用户目标中提取，"
                        "剔除角色、受众、质量标准、输出要求等信封词，"
                        "避免查询被指令包装污染。"
                    ),
                    "rationale": "避免查询污染",
                }],
                "apply": True,
            }, ensure_ascii=False)}

        llm_client.call_llm = fake_call
        try:
            res = refine_after_task(
                _FakeMessaging(), "ui-t1", "目标", [], {}, "报告",
                {}, memory=mem,
            )
            self.assertEqual(res["applied"], 1)
            self.assertEqual(len(mem.records), 1)
            self.assertEqual(mem.records[0]["key"], "step:web_search")
        finally:
            llm_client.call_llm = orig


class TestLLMHealthDegradation(unittest.TestCase):
    """P0-2 LLM 端点降级可见化：健康原因记录 + 任务级降级汇总。"""

    def test_degradation_reason_classification(self):
        import llm_client

        self.assertEqual(llm_client._degradation_reason(
            Exception("Empty content in LLM response")), "empty_content")
        self.assertEqual(llm_client._degradation_reason(
            Exception("HTTP 401: Insufficient balance")), "HTTP_401")
        self.assertEqual(llm_client._degradation_reason(
            Exception("HTTP 402: Payment Required")), "HTTP_402")
        self.assertEqual(llm_client._degradation_reason(
            Exception("HTTP 403: Forbidden")), "HTTP_403")
        self.assertEqual(llm_client._degradation_reason(
            Exception("Network error: timed out")), "timeout")
        self.assertEqual(llm_client._degradation_reason(
            Exception("Network error: refused")), "network_error")
        self.assertEqual(llm_client._degradation_reason(
            Exception("JSON parse error")), "generic")

    def test_mark_endpoint_records_last_reason(self):
        import llm_client

        with llm_client._endpoint_health_lock:
            old = {k: dict(v) for k, v in llm_client._endpoint_health.items()}
        try:
            llm_client._mark_endpoint("primary", False, "HTTP_401")
            h = llm_client.get_endpoint_health()
            self.assertEqual(h["primary"]["last_degradation_reason"], "HTTP_401")
            self.assertGreater(h["primary"]["last_degradation_ts"], 0)
            # 恢复健康后保留最近一次降级原因（"最近一次"语义）
            llm_client._mark_endpoint("primary", True)
            h2 = llm_client.get_endpoint_health()
            self.assertEqual(h2["primary"]["last_degradation_reason"], "HTTP_401")
        finally:
            with llm_client._endpoint_health_lock:
                llm_client._endpoint_health.clear()
                llm_client._endpoint_health.update(old)

    def test_task_degradation_summary(self):
        import fakeredis
        import llm_client

        fake = fakeredis.FakeStrictRedis(decode_responses=True)
        old_client = llm_client._task_usage_client
        llm_client._task_usage_client = fake
        try:
            llm_client._record_task_degradation("t-dg", "HTTP_401", both_failed=False)
            llm_client._record_task_degradation("t-dg", "switch_to_backup", both_failed=False)
            llm_client._record_task_degradation("t-dg", "HTTP_402", both_failed=True)
            s = llm_client.get_task_llm_degradation("t-dg")
            self.assertEqual(s["switches"], 1)
            self.assertIn("HTTP_401", s["reasons"])
            self.assertIn("HTTP_402", s["reasons"])
            self.assertTrue(s["both_failed"])
            self.assertEqual(len(s["events"]), 3)
            empty = llm_client.get_task_llm_degradation("t-none")
            self.assertEqual(empty["switches"], 0)
            self.assertFalse(empty["both_failed"])
        finally:
            llm_client._task_usage_client = old_client

    def test_task_degradation_inherited_root_cause(self):
        """P2-3：主端点由他处标记不健康（无具体失败原因）时切换 →
        reasons 回填 inherited_unhealthy；补记根因事件不丢 switch 事件。"""
        import fakeredis
        import llm_client

        fake = fakeredis.FakeStrictRedis(decode_responses=True)
        old_client = llm_client._task_usage_client
        old_backup = dict(llm_client._BACKUP_CFG)
        llm_client._task_usage_client = fake
        with llm_client._endpoint_health_lock:
            old_health = {k: dict(v) for k, v in llm_client._endpoint_health.items()}
        llm_client.set_task_context("")
        try:
            # 模拟"主端点由他处标记不健康"：探测标记但未留具体失败原因
            llm_client._endpoint_health.clear()
            llm_client._mark_endpoint("primary", False)
            self.assertEqual(
                llm_client._primary_degradation_root(), "inherited_unhealthy",
            )
            llm_client.set_task_context("t-inh-1")
            llm_client._BACKUP_CFG = {
                "base_url": "http://backup.example/v1",
                "api_key": "k",
                "model": "m",
            }
            client = llm_client.LLMClient.__new__(llm_client.LLMClient)
            client._backup_cfg = dict(llm_client._BACKUP_CFG)
            with mock.patch.object(
                llm_client.LLMClient, "_send_request", return_value="ok",
            ):
                res = client._call_backup("s", "u", 0.1, 100, False)
            self.assertEqual(res, {"content": "ok"})
            s = llm_client.get_task_llm_degradation("t-inh-1")
            self.assertIn("inherited_unhealthy", s["reasons"])
            self.assertEqual(s["switches"], 1)
            switch_events = [
                e for e in s["events"] if e.get("reason") == "switch_to_backup"
            ]
            self.assertEqual(len(switch_events), 1, "switch 事件不应被根因事件挤出")
            self.assertGreaterEqual(len(s["events"]), 2)
            # 多次切换：events 截断上限与 switches 对齐（补记事件不丢）
            for _ in range(12):
                llm_client._record_task_degradation(
                    "t-inh-1", "inherited_unhealthy", both_failed=False,
                )
                llm_client._record_task_degradation(
                    "t-inh-1", "switch_to_backup", both_failed=False,
                )
            s2 = llm_client.get_task_llm_degradation("t-inh-1")
            self.assertEqual(s2["switches"], 13)
            switch_events2 = [
                e for e in s2["events"] if e.get("reason") == "switch_to_backup"
            ]
            self.assertEqual(
                len(switch_events2), 13,
                "events 上限应对齐 switches 计数，所有 switch 事件必须保留",
            )
        finally:
            llm_client.clear_task_context()
            llm_client._task_usage_client = old_client
            llm_client._BACKUP_CFG.clear()
            llm_client._BACKUP_CFG.update(old_backup)
            with llm_client._endpoint_health_lock:
                llm_client._endpoint_health.clear()
                llm_client._endpoint_health.update(old_health)


class TestModelRoleRouting(unittest.TestCase):
    """B1：llm.model_roles 模型分级路由（planner/exec/judge，缺省回退默认模型）。"""

    def setUp(self):
        import llm_client
        self.llm = llm_client
        self._old_roles = llm_client._MODEL_ROLES_CFG
        self._old_model = os.environ.get("LLM_MODEL")
        os.environ["LLM_MODEL"] = "default-model"
        llm_client._MODEL_ROLES_CFG = {
            "planner": "deepseek-chat",
            "exec": "deepseek-v4-flash",
            "judge": "deepseek-chat",
        }
        self._patchers = [
            mock.patch.object(llm_client, "_ensure_cfg_fresh", lambda: None),
            mock.patch.object(llm_client, "_primary_healthy", lambda: True),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.llm._MODEL_ROLES_CFG = self._old_roles
        if self._old_model is None:
            os.environ.pop("LLM_MODEL", None)
        else:
            os.environ["LLM_MODEL"] = self._old_model

    def test_usage_mapping_table(self):
        from llm_client import MODEL_ROLES, get_model_for_usage
        self.assertEqual(MODEL_ROLES["plan"], "planner")
        self.assertEqual(MODEL_ROLES["reflect"], "planner")
        self.assertEqual(MODEL_ROLES["review"], "planner")
        self.assertEqual(MODEL_ROLES["exec"], "exec")
        self.assertEqual(MODEL_ROLES["judge"], "judge")
        self.assertEqual(get_model_for_usage("plan"), "deepseek-chat")
        self.assertEqual(get_model_for_usage("exec"), "deepseek-v4-flash")
        self.assertEqual(get_model_for_usage("judge"), "deepseek-chat")

    def test_fallback_to_default_model(self):
        from llm_client import get_model_for_usage
        self.assertEqual(get_model_for_usage("unknown-usage"), "default-model")

    def test_client_call_selects_role_model(self):
        from llm_client import LLMClient
        client = LLMClient(model="default-model")
        captured = {}

        def fake_send(system, user, temperature, max_tokens, model=None):
            captured["model"] = model
            return '{"steps": []}'

        client._send_request = fake_send
        client.call("s", "u", usage="plan")
        self.assertEqual(captured["model"], "deepseek-chat")
        client.call("s", "u", usage="exec")
        self.assertEqual(captured["model"], "deepseek-v4-flash")

    def test_exec_falls_back_to_default_when_role_unset(self):
        from llm_client import LLMClient
        self.llm._MODEL_ROLES_CFG = {"planner": "deepseek-chat"}
        client = LLMClient(model="default-model")
        captured = {}
        client._send_request = (
            lambda system, user, temperature, max_tokens, model=None:
            captured.update(model=model) or '{"ok": true}'
        )
        client.call("s", "u", usage="exec")
        self.assertEqual(captured["model"], "default-model")

    def test_model_override_wins(self):
        from llm_client import LLMClient
        client = LLMClient(model="default-model")
        captured = {}
        client._send_request = (
            lambda system, user, temperature, max_tokens, model=None:
            captured.update(model=model) or '{"ok": true}'
        )
        client.call("s", "u", usage="plan", model_override="custom-model")
        self.assertEqual(captured["model"], "custom-model")

    def test_async_call_selects_role_model(self):
        import asyncio
        from llm_client import call_llm_async

        posts = []

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }

        class FakeHTTPX:
            async def post(self, url, json=None, headers=None):
                posts.append((url, dict(json or {})))
                return FakeResp()

        with mock.patch.object(self.llm, "_get_async_client", lambda: FakeHTTPX()), \
             mock.patch.object(self.llm, "_publish_usage_snapshot", lambda: None):
            asyncio.run(call_llm_async("s", "u", usage="exec"))
        self.assertEqual(posts[0][1]["model"], "deepseek-v4-flash")


class TestLLMCache(unittest.TestCase):
    """B2：LLM_CACHE_TTL 开启后同键调用命中缓存，不触发网络，不计 token 成本。"""

    class _FakeRedis:
        def __init__(self, now=0.0):
            self.store = {}
            self.expires = {}
            self.now = now
            self.ex_values = []

        def get(self, key):
            exp = self.expires.get(key)
            if exp is not None and self.now >= exp:
                return None
            return self.store.get(key)

        def set(self, key, value, ex=None):
            self.store[key] = value
            self.expires[key] = self.now + (ex or 0)
            self.ex_values.append(ex)

    def setUp(self):
        import llm_client
        self.llm = llm_client
        self._old_ttl = os.environ.get("LLM_CACHE_TTL")
        self._old_client = llm_client._llm_cache_client
        self._old_roles = llm_client._MODEL_ROLES_CFG
        self._old_model = os.environ.get("LLM_MODEL")
        llm_client._MODEL_ROLES_CFG = {}
        os.environ["LLM_CACHE_TTL"] = "60"
        # call() 每次会用 LLM_MODEL 刷新客户端默认模型（热重载设计），
        # 测试里让环境变量与构造模型保持一致
        os.environ["LLM_MODEL"] = "m"
        self.fake = self._FakeRedis()
        llm_client._llm_cache_client = self.fake
        self._patchers = [
            mock.patch.object(llm_client, "_ensure_cfg_fresh", lambda: None),
            mock.patch.object(llm_client, "_primary_healthy", lambda: True),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.llm._llm_cache_client = self._old_client
        self.llm._MODEL_ROLES_CFG = self._old_roles
        if self._old_model is None:
            os.environ.pop("LLM_MODEL", None)
        else:
            os.environ["LLM_MODEL"] = self._old_model
        if self._old_ttl is None:
            os.environ.pop("LLM_CACHE_TTL", None)
        else:
            os.environ["LLM_CACHE_TTL"] = self._old_ttl

    def test_hit_returns_same_without_network(self):
        from llm_client import LLMClient
        client = LLMClient(model="m")
        calls = {"n": 0}

        def fake_send(system, user, temperature, max_tokens, model=None):
            calls["n"] += 1
            return '{"ok": true, "n": %d}' % calls["n"]

        client._send_request = fake_send
        r1 = client.call("s", "u", cache_key="plan:g1")
        r2 = client.call("s", "u", cache_key="plan:g1")
        self.assertEqual(r1, {"ok": True, "n": 1})
        self.assertEqual(r2, r1)
        self.assertEqual(calls["n"], 1, "第二次调用应命中缓存，不触发网络")

    def test_hit_marks_cached_usage_without_tokens(self):
        from llm_client import LLMClient
        client = LLMClient(model="m")
        client._send_request = lambda *a, **k: '{"ok": true}'
        client.call("s", "u", cache_key="k1")
        with mock.patch.object(self.llm, "_record_usage") as rec:
            client.call("s", "u", cache_key="k1")
        rec.assert_called_once_with(0, 0, model="m", cached=True)

    def test_ttl_expiry_refetches(self):
        from llm_client import LLMClient
        client = LLMClient(model="m")
        calls = {"n": 0}

        def fake_send(system, user, temperature, max_tokens, model=None):
            calls["n"] += 1
            return '{"ok": true, "n": %d}' % calls["n"]

        client._send_request = fake_send
        client.call("s", "u", cache_key="k")
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.fake.ex_values, [60])
        self.fake.now += 61  # TTL 过期
        client.call("s", "u", cache_key="k")
        self.assertEqual(calls["n"], 2, "TTL 过期后应重新走网络")

    def test_disabled_no_cache(self):
        os.environ["LLM_CACHE_TTL"] = "0"
        from llm_client import LLMClient
        client = LLMClient(model="m")
        calls = {"n": 0}

        def fake_send(system, user, temperature, max_tokens, model=None):
            calls["n"] += 1
            return '{"ok": true}'

        client._send_request = fake_send
        client.call("s", "u", cache_key="k")
        client.call("s", "u", cache_key="k")
        self.assertEqual(calls["n"], 2, "缓存关闭时行为保持不变")
        self.assertEqual(self.fake.store, {})

    def test_different_prompt_not_hit(self):
        from llm_client import LLMClient
        client = LLMClient(model="m")
        calls = {"n": 0}

        def fake_send(system, user, temperature, max_tokens, model=None):
            calls["n"] += 1
            return '{"ok": true}'

        client._send_request = fake_send
        client.call("s", "u1", cache_key="k")
        client.call("s", "u2", cache_key="k")
        self.assertEqual(calls["n"], 2, "prompt 不同不应命中缓存")

    def test_async_cache_hit_skips_network(self):
        import asyncio
        from llm_client import call_llm_async

        posts = {"n": 0}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "choices": [{"message": {"content": "cached-async"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                }

        class FakeHTTPX:
            async def post(self, url, json=None, headers=None):
                posts["n"] += 1
                return FakeResp()

        with mock.patch.object(self.llm, "_get_async_client", lambda: FakeHTTPX()), \
             mock.patch.object(self.llm, "_publish_usage_snapshot", lambda: None):
            r1 = asyncio.run(
                call_llm_async("s", "u", expect_json=False, cache_key="ak")
            )
            r2 = asyncio.run(
                call_llm_async("s", "u", expect_json=False, cache_key="ak")
            )
        self.assertEqual(r1, "cached-async")
        self.assertEqual(r2, r1)
        self.assertEqual(posts["n"], 1, "异步缓存命中也不应触发网络")


if __name__ == "__main__":
    unittest.main()
