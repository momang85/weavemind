"""Orchestrator V2 — clean, linear, push-progress-enabled.

Reuses all existing components: llm_client, common, async_worker_base, memory_manager,
critic_agent, ws_helpers. No complex state machine — just: plan → dispatch → collect → report.

This is the active orchestrator (legacy orchestrator.py was removed in the architecture cleanup).
"""

import json, logging, os, re, threading, time, uuid

from common import AgentRegistry, MessagingClient, RedisAgentRegistry
from llm_client import LLMClient, get_usage_stats
from memory_manager import MemoryManager
from ws_helpers import push_progress

logger = logging.getLogger(__name__)


def _loads_json_loose(text: str) -> dict:
    """先严格解析，失败后允许字符串内未转义控制字符（LLM 常在长指令中插入字面换行）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text, strict=False)

# ─────────────────────────────────────────────
# Planner prompt
# ─────────────────────────────────────────────
PLANNER_SYSTEM = """Break user goals into sequential execution steps.

Available: web_search, web_fetch, data_loader, data_analyzer, model_trainer, report_generator, content_summary, code_execution, file_io, package

Rules:
1. Each step uses EXACTLY one capability from the list above
2. instruction = what the worker should do, in plain language
3. depends_on = list of step_ids this step needs to complete first
4. For data pipelines: use data_loader (loads sklearn datasets automatically), then data_analyzer, then model_trainer, then report_generator
5. All steps MUST strictly follow the user's goal topic; never generalize to other domains
6. Search is NOT a mandatory information source: code, documents, summaries and reports can be produced directly by content_summary / code_execution from model knowledge. Use web_search ONLY when the goal explicitly requires up-to-date external facts (market data, news, current prices, real repos)
7. NEVER chain repeated searches: at most one web_search/web_fetch pair per plan; if external info is unavailable, later steps must fall back to direct generation instead of searching again
8. code_execution ONLY generates and runs Python scripts (or a single self-contained HTML file). JavaScript / multi-file frontend projects must be restructured into a single Python script or single HTML file; do NOT plan separate .js modules
9. depends_on must NOT form cycles; each step may only depend on steps that come before it in execution order

Output ONLY this JSON with no extra text:
{"steps":[{"step_id":"1","capability":"web_search","instruction":"search for house price dataset","depends_on":[],"timeout":60}]}"""

KNOWN_CAPABILITIES = {
    "web_search", "web_fetch", "data_loader", "data_analyzer", "model_trainer",
    "report_generator", "content_summary", "code_execution", "file_io", "package",
}

_TOPIC_STOPWORDS = {
    "一个", "我们", "你们", "他们", "它们", "完成", "输出", "生成", "要求",
    "进行", "需要", "可以", "是否", "如何", "什么", "请", "帮", "并", "与",
    "和", "在", "用", "把", "将", "给", "让", "这", "那", "为", "对", "其",
    "及", "或", "等", "做", "写", "的", "了", "是", "我", "你", "他", "她", "它",
}

ITERATOR_SYSTEM = """你是严格的交付验收评审。根据用户目标评估交付物是否达标。
输出严格JSON：{"accepted": true|false, "gaps": ["缺口1","缺口2"], "next_steps":[{"step_id":"1","capability":"...","instruction":"...","timeout":120}]}
规则：
1. accepted=false 只用于明确缺失用户要求的内容；不要吹毛求疵
2. next_steps 每次最多3个，必须具体可执行，指令用中文并严格围绕目标主题
3. 每个 next_step 的 capability 只能是下列之一，且每步只能一个：
   web_search, web_fetch, data_loader, data_analyzer, model_trainer, report_generator, content_summary, code_execution, file_io, package
4. 若交付物缺失的原因是外部资料获取失败（搜索无结果、网页无内容、URL 无效），next_steps 必须改为直接生成（content_summary / code_execution），禁止再安排 web_search 或 web_fetch
只输出JSON。"""

# ─────────────────────────────────────────────
# Orchestrator V2
# ─────────────────────────────────────────────

class OrchestratorV2:
    def __init__(self):
        # Load config.json for LLM settings (if env not set)
        import os as _os
        _cfg_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'config.json')
        self._max_retry = 2
        self._replan_depth = 2
        self._critic_enabled = False
        self._critic_timeout = 30
        self._max_steps = 8
        self._max_parallel = 3
        self._max_iterations = 2
        self._plan_confirm_timeout = 300
        self._stall_timeout = 300
        self._max_offtopic_regenerations = 1
        self._planner_model = None
        _cfg = {}
        try:
            with open(_cfg_path, 'r') as _f:
                _cfg = json.loads(_f.read())
            _llm = _cfg.get('llm', {})
            for _k in ('api_key', 'base_url', 'model'):
                _env_key = f'LLM_{_k.upper()}'
                if not _os.environ.get(_env_key) and _llm.get(_k):
                    _os.environ[_env_key] = _llm[_k]
            _sys = _cfg.get('system', {})
            self._max_retry = max(0, int(_sys.get('max_retry', 2)))
            self._replan_depth = max(0, int(_sys.get('replan_depth', 2)))
            self._critic_enabled = bool(_sys.get('critic', False))
            self._critic_timeout = max(10, int(_sys.get('critic_timeout', 30)))
            self._max_steps = max(1, int(_sys.get('max_steps', 8)))
            self._max_parallel = max(1, int(_sys.get('max_parallel', 3)))
            self._max_iterations = max(0, int(_sys.get('max_iterations', 2)))
            self._plan_confirm_timeout = max(30, int(_sys.get('plan_confirm_timeout', 300)))
            self._stall_timeout = max(5, int(_sys.get('stall_timeout', 60)))
        except Exception:
            pass
        self._redis = self._new_redis_sync()
        self._redis_reg = RedisAgentRegistry(self._redis)
        self._sqlite_reg = AgentRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
        self._messaging = MessagingClient(
            os.environ.get("REDIS_HOST", "localhost"),
            int(os.environ.get("REDIS_PORT", "6379")),
        )
        # MessagingClient auto-connects
        self._memory = MemoryManager(os.environ.get("MEMORY_DIR", "./chroma_memory"))
        self._memory_lock = threading.Lock()
        self._plan_llm = LLMClient()
        # 可选：专用规划模型（更稳的模型负责拆解，执行仍用默认模型）
        _planner_cfg = (_cfg.get("planner") or {}) if isinstance(_cfg, dict) else {}
        if _planner_cfg.get("model"):
            _llm_cfg = _cfg.get("llm", {}) if isinstance(_cfg, dict) else {}
            self._planner_llm = LLMClient(
                model=_planner_cfg.get("model"),
                base_url=_planner_cfg.get("base_url") or _llm_cfg.get("base_url"),
                api_key=_planner_cfg.get("api_key") or _llm_cfg.get("api_key"),
            )
            self._planner_model = _planner_cfg.get("model")
            logger.info("Planner LLM: %s", self._planner_model)
        else:
            self._planner_llm = self._plan_llm
        logger.info("OrchestratorV2 initialized")

    def _find_agent(self, capability: str) -> str | None:
        """Try Redis first, fall back to SQLite."""
        agent = self._redis_reg.find_capable_agent(capability)
        if agent:
            return agent
        return self._sqlite_reg.find_capable_agent(capability)

    def _now_iso(self):
        import datetime
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    @staticmethod
    def _new_redis_sync():
        """环境变量感知的同步 Redis 客户端（Docker 中 REDIS_HOST=redis）。"""
        import redis as _redis
        return _redis.Redis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            decode_responses=True,
        )

    @staticmethod
    def _brpop_with_deadline(r, key: str, deadline: float):
        """redis-py 8 的 brpop 大超时行为不可靠（超时抛异常/时间膨胀），
        统一改用 2 秒小超时轮询直到总截止时间。返回 (key, payload) 或 None。"""
        import redis as _redis
        while time.time() < deadline:
            try:
                msg = r.brpop([key], timeout=2)
                if msg:
                    return msg
            except _redis.exceptions.TimeoutError:
                continue
            except Exception as e:
                logger.warning("BRPOP error for %s: %s", key, e)
                return None
        return None

    # ── Plan ──
    def _plan(self, goal: str, task_id: str, context: str = "") -> list[dict]:
        """Ask LLM to decompose goal into steps."""
        push_progress(self._messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator", "message": f"Planning: {goal[:60]}", "timestamp": self._now_iso()})
        if context:
            logger.info("Task %s using conversation context (%d chars)", task_id, len(context))
            push_progress(self._messaging, task_id, "log",
                          {"type": "context", "agent": "orchestrator",
                           "message": f"Conversation context: {len(context)} chars",
                           "timestamp": self._now_iso()})

        with self._memory_lock:
            memory_context = self._memory.inject_context(goal)
        if memory_context:
            push_progress(self._messaging, task_id, "log",
                          {"type": "memory", "agent": "orchestrator",
                           "message": f"Memory: {len(memory_context)} chars of relevant experience",
                           "timestamp": self._now_iso()})

        topic_hint = (
            f"用户目标主题：{goal[:80].strip()}。"
            "所有步骤必须严格围绕该主题，禁止泛化、替换或扩大到其他领域。"
        )
        prompt = f"{topic_hint}\n\n{goal}"
        if context:
            prompt = (
                "Conversation context (previous requests and results):\n"
                f"{context}\n\nCurrent goal:\n{goal}\n\n"
                "Note: this is a follow-up in an ongoing conversation. Prefer lightweight steps "
                "(web_search, content_summary, file_io, code_execution); only use "
                "data_loader/data_analyzer/model_trainer/report_generator when the user "
                "explicitly asks for data processing.\n"
                f"{topic_hint}"
            )
        if memory_context:
            prompt = f"{prompt}\n\nRelevant past experience:\n{memory_context}\n\nGenerate a plan."

        plan_data = None
        last_error = None
        for attempt in range(2):
            attempt_prompt = prompt if attempt == 0 else (
                "STRICT JSON ONLY. Output ONLY a JSON object {\"steps\":[...]}, no markdown, "
                "no explanation, and no line breaks inside instruction strings.\nGoal: " + goal
            )
            try:
                raw = self._planner_llm.call(PLANNER_SYSTEM, attempt_prompt, expect_json=True)
                plan_data = self._parse_plan_response(raw)
                break
            except Exception as e:
                last_error = e
                logger.warning("Plan attempt %d failed: %s", attempt + 1, str(e)[:200])
        if plan_data is None:
            logger.error("Plan failed: %s", str(last_error)[:300])
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": "orchestrator",
                           "message": f"Plan failed: {last_error}", "timestamp": self._now_iso()})
            # 兜底：目标无法拆解时，作为单个 content_summary 步骤交给 LLM Worker
            fallback = [{
                "step_id": "1",
                "capability": "content_summary",
                "instruction": f"完成以下目标并输出结果：{goal[:500]}",
                "timeout": 120,
            }]
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": "Plan fallback: single content_summary step", "timestamp": self._now_iso()})
            return fallback

        steps = self._normalize_steps(plan_data.get("steps", []))
        steps = self._ensure_report_step(steps, task_id)
        # 规划自检：计划主题与目标明显不符时，用强约束重生成一次
        if steps and not self._plan_topic_ok(goal, steps):
            logger.warning("Plan appears off-topic for goal: %s", goal[:50])
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": "Plan off-topic, regenerating with strict topic prompt",
                           "timestamp": self._now_iso()})
            try:
                strict_prompt = (
                    "严格围绕用户目标主题重写计划，禁止偏离、泛化或替换到其他主题。"
                    f"用户目标：\n{goal}\n\n重新输出严格JSON计划。"
                )
                raw2 = self._planner_llm.call(PLANNER_SYSTEM, strict_prompt, expect_json=True)
                steps2 = self._normalize_steps(self._parse_plan_response(raw2))
                steps2 = self._ensure_report_step(steps2, task_id)
                if steps2 and self._plan_topic_ok(goal, steps2):
                    steps = steps2
            except Exception as exc:
                logger.warning("Off-topic regeneration failed: %s", str(exc)[:150])
        if not steps:
            steps = [{
                "step_id": "1",
                "capability": "content_summary",
                "instruction": f"完成以下目标并输出结果：{goal[:500]}",
                "timeout": 120,
            }]
        # Critic 评审（可选，config system.critic）
        if self._critic_enabled and steps:
            steps = self._review_plan(goal, steps, task_id)
        push_progress(self._messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator",
                       "message": f"Plan ready: {len(steps)} steps", "timestamp": self._now_iso()})
        return steps

    def _ensure_report_step(self, steps: list[dict], task_id: str) -> list[dict]:
        """规划自检：计划中缺少报告/总结步骤时，自动补一步 report_generator（报告兜底）。"""
        if not steps:
            return steps
        has_report = any(s.get("capability") in ("content_summary", "report_generator") for s in steps)
        if not has_report:
            all_ids = [s.get("step_id") for s in steps]
            steps = steps + [{
                "step_id": f"report-{len(steps) + 1}",
                "capability": "report_generator",
                "instruction": "汇总以上所有步骤的结果，生成面向用户的最终交付报告（Markdown，含必要的表格、图表说明与结论）。",
                "depends_on": all_ids,
                "timeout": 120,
            }]
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": "Plan self-check: auto-added report step",
                           "timestamp": self._now_iso()})
        return steps

    _FILE_CAPABILITIES = (
        "code_execution", "file_io", "web_fetch", "data_loader",
        "data_analyzer", "model_trainer", "report_generator",
    )

    def _ensure_package_step(self, steps: list[dict]) -> list[dict]:
        """交付兜底：计划包含文件类产物但没有 package 步骤时，自动补一步打包，
        保证每个任务都能产出可下载的 ZIP 交付包。"""
        if not steps:
            return steps
        if any(s.get("capability") == "package" for s in steps):
            return steps
        producers = [
            s.get("step_id") for s in steps
            if s.get("capability") in self._FILE_CAPABILITIES
        ]
        if not producers:
            return steps
        return steps + [{
            "step_id": f"package-{len(steps) + 1}",
            "capability": "package",
            "instruction": "将本次任务产出的所有文件打包为一个 ZIP 交付包（包含代码、资源、报告等），并返回下载链接。",
            "depends_on": producers,
            "timeout": 180,
        }]

    def _wire_report_deps(self, steps: list[dict]) -> list[dict]:
        """报告/总结步骤若无依赖，则自动依赖所有其它步骤（保证执行时带全量上下文）。"""
        ids = [s.get("step_id") for s in steps]
        for s in steps:
            if s.get("capability") in ("content_summary", "report_generator") and not s.get("depends_on"):
                s["depends_on"] = [i for i in ids if i != s.get("step_id")]
        return steps

    def _wire_search_fetch_deps(self, steps: list[dict]) -> list[dict]:
        """链式兜底：web_fetch 步骤若无依赖且计划中存在 web_search 步骤，
        自动接上前置搜索步骤，保证抓取时有 URL 可用。"""
        search_id = next(
            (s.get("step_id") for s in steps if s.get("capability") == "web_search"),
            None,
        )
        if not search_id:
            return steps
        for s in steps:
            if s.get("capability") == "web_fetch" and not s.get("depends_on"):
                s["depends_on"] = [search_id]
        return steps

    def _break_cycles(self, steps: list[dict]) -> list[dict]:
        """检测并打破步骤依赖环：环上节点降级为并行（清空 depends_on），
        避免 DAG 执行卡死被看门狗误判为 stalled。"""
        ids = [s.get("step_id") for s in steps]
        id_set = set(ids)
        deps = {
            s.get("step_id"): [d for d in s.get("depends_on", []) if d in id_set]
            for s in steps
        }
        indeg = {i: len(deps[i]) for i in ids}
        children = {i: [] for i in ids}
        for i in ids:
            for d in deps[i]:
                children[d].append(i)
        queue = [i for i in ids if indeg[i] == 0]
        topo: list[str] = []
        while queue:
            i = queue.pop()
            topo.append(i)
            for c in children[i]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    queue.append(c)
        in_cycle = set(ids) - set(topo)
        if not in_cycle:
            return steps
        for s in steps:
            if s.get("step_id") in in_cycle:
                s["depends_on"] = []
                logger.warning("Cycle detected, breaking deps of step %s", s.get("step_id"))
        return steps

    def _normalize_steps(self, steps: list) -> list[dict]:
        """规范化计划步骤：去重 ID、剔除空指令、补齐默认字段、限制步数。"""
        out: list[dict] = []
        seen: set[str] = set()
        for i, s in enumerate(steps, 1):
            if not isinstance(s, dict):
                continue
            sid = str(s.get("step_id") or i)
            if sid in seen:
                sid = f"{sid}-{i}"
            seen.add(sid)
            instruction = str(s.get("instruction") or "").strip()
            if not instruction:
                continue
            s["step_id"] = sid
            # 校验能力字段：非法/多值拼接时回退到 content_summary
            cap = str(s.get("capability") or "content_summary").strip()
            if "," in cap:
                cap = next((c.strip() for c in cap.split(",") if c.strip() in KNOWN_CAPABILITIES), "content_summary")
            elif cap not in KNOWN_CAPABILITIES:
                cap = "content_summary"
            # 能力纠偏：安装依赖的"package"步骤实为环境准备，改派 code_execution 执行 pip
            if cap == "package" and any(
                k in instruction for k in ("安装", "install", "依赖", "pip")
            ):
                cap = "code_execution"
                s["instruction"] = (
                    "使用 pip 安装所需依赖（已安装则跳过），并验证 import 成功："
                    f"{instruction}"
                )
                instruction = s["instruction"]
            s["capability"] = cap
            s.setdefault("timeout", 120)
            out.append(s)
        if len(out) > self._max_steps:
            logger.warning("Plan normalized from %d to %d steps (max_steps=%d)",
                           len(out), self._max_steps, self._max_steps)
            out = out[:self._max_steps]
        return out

    def _parse_plan_response(self, raw) -> dict:
        """把规划器返回（dict 或带代码围栏的 JSON 字符串）解析为 dict。"""
        if isinstance(raw, dict):
            return raw
        clean = str(raw).strip()
        if clean.startswith("```json"):
            clean = clean[7:]
        if clean.startswith("```"):
            clean = clean[3:]
        if clean.endswith("```"):
            clean = clean[:-3]
        return _loads_json_loose(clean.strip())

    def _topic_tokens(self, text: str) -> set[str]:
        """从目标中提取主题词（中文 2-4 字片段 + 英文词，剔除停用词）。"""
        import re as _re

        tokens = set()
        for w in _re.findall(r"[\u4e00-\u9fff]{2,4}", text):
            if w not in _TOPIC_STOPWORDS:
                tokens.add(w)
        tokens |= {w.lower() for w in _re.findall(r"[a-z][a-z0-9-]{2,}", text)}
        return tokens

    def _plan_topic_ok(self, goal: str, steps: list[dict]) -> bool:
        """计划是否明显围绕目标主题：至少一个步骤指令命中一个主题词。"""
        tokens = self._topic_tokens(goal)
        if not tokens:
            return True
        hay = " ".join(str(s.get("instruction", "")) for s in steps).lower()
        return any(t in hay for t in tokens)

    def _reflect(self, goal: str, report: str, task_id: str) -> dict | None:
        """验收评审：判断交付物是否达标，不达标则给出缺口与下一步。"""
        push_progress(self._messaging, task_id, "log",
                      {"type": "iteration", "agent": "orchestrator",
                       "message": "Reflecting: reviewing deliverable against goal",
                       "timestamp": self._now_iso()})
        prompt = (
            f"Goal:\n{goal}\n\nDeliverable produced:\n{str(report)[:4000]}\n\n"
            "Assess acceptance and propose next steps if needed."
        )
        try:
            raw = self._planner_llm.call(ITERATOR_SYSTEM, prompt, expect_json=True)
            if isinstance(raw, dict):
                return raw
            clean = str(raw).strip()
            if clean.startswith("```"):
                clean = clean.strip("`")
                if clean.startswith("json"):
                    clean = clean[4:]
            return _loads_json_loose(clean.strip())
        except Exception as exc:
            logger.warning("Reflection failed, stopping iteration: %s", str(exc)[:150])
            return None

    def _generation_fallback_step(self, goal: str, step: dict) -> dict:
        """搜索/抓取无果时的降级步骤：由 LLM 直接生产，不依赖外部资料。"""
        ins = str(step.get("instruction") or "")
        if any(k in ins.lower() for k in ("html", "网页", "web", "webpage")):
            return {
                "capability": "code_execution",
                "instruction": (
                    "不依赖任何外部资料，直接生成一个自包含的单文件 HTML 页面/游戏"
                    "（内联 CSS/JS，保存为 index.html，不要用 Python 运行），实现："
                    f"{ins[:500]}"
                ),
                "timeout": 180,
            }
        if any(k in ins for k in (
            "代码", "实现", "生成", "编写", "开发", "脚本", "main.py", ".py", "游戏",
        )):
            return {
                "capability": "code_execution",
                "instruction": (
                    "不依赖任何外部资料，直接编写可运行的 Python 代码实现以下要求："
                    f"{ins[:500]}"
                ),
                "timeout": 180,
            }
        return {
            "capability": "content_summary",
            "instruction": (
                "不依赖任何外部资料，基于已有知识直接完成并输出以下目标，"
                "内容要具体、围绕主题，不要提及搜索或抓取失败："
                f"{goal[:300]}"
            ),
            "timeout": 120,
        }

    def _build_search_revision(self, pending: dict, goal: str) -> list[dict]:
        """把仍待执行的 web_fetch 步骤替换为 LLM 直接生产步骤（保留 step_id 与依赖）。"""
        revision = []
        for k, s in pending.items():
            if s.get("capability") == "web_fetch":
                alt = self._generation_fallback_step(goal, s)
                alt["step_id"] = k
                alt["depends_on"] = s.get("depends_on", [])
                revision.append(alt)
        return revision

    def _confirm_revision(
        self, task_id: str, goal: str, steps: list[dict],
        completed: dict, revision: list[dict],
    ) -> list[dict] | None:
        """搜索无果时把降级计划推给前端确认/编辑。
        超时自动采用修订；用户取消则保持原计划；用户编辑则采用编辑后的步骤。"""
        rev_map = {r.get("step_id"): r for r in revision}
        view = []
        for s in steps:
            c = dict(s)
            c["result"] = completed.get(s.get("step_id"), {})
            r = rev_map.get(s.get("step_id"))
            if r:
                c["capability"] = r.get("capability", c.get("capability"))
                c["instruction"] = r.get("instruction", c.get("instruction"))
                c["timeout"] = r.get("timeout", c.get("timeout"))
            view.append(c)
        push_progress(
            self._messaging, task_id, "log",
            {"type": "info", "agent": "orchestrator",
             "message": f"Search yielded no usable results; proposing {len(revision)} direct-generation step(s), awaiting confirmation",
             "timestamp": self._now_iso()},
        )
        try:
            self._messaging.publish("orchestrator:response", {
                "task_id": task_id, "status": "AWAITING_CONFIRM",
                "goal": goal, "steps": view, "revision": True,
            })
        except Exception as exc:
            logger.warning("Revision confirm publish failed: %s", str(exc)[:120])
            return revision
        timeout = min(self._plan_confirm_timeout, 180)
        try:
            msg = self._brpop_with_deadline(
                self._redis,
                f"plan_confirm:{task_id}",
                time.time() + timeout,
            )
        except Exception as exc:
            logger.warning("Revision confirm wait failed: %s", str(exc)[:120])
            return revision
        if not msg:
            push_progress(
                self._messaging, task_id, "log",
                {"type": "info", "agent": "orchestrator",
                 "message": f"No confirmation within {timeout}s, auto-applying revised plan",
                 "timestamp": self._now_iso()},
            )
            return revision
        try:
            data = json.loads(msg[1] if isinstance(msg[1], str) else msg[1].decode())
        except Exception:
            return revision
        if data.get("action") == "cancel":
            push_progress(
                self._messaging, task_id, "log",
                {"type": "info", "agent": "orchestrator",
                 "message": "Revision declined by user; continuing with original plan",
                 "timestamp": self._now_iso()},
            )
            return None
        new_steps = data.get("steps")
        if not new_steps:
            return revision
        normalized = self._normalize_steps(new_steps)
        push_progress(
            self._messaging, task_id, "log",
            {"type": "plan", "agent": "orchestrator",
             "message": f"Revised plan confirmed with {len(normalized)} steps",
             "timestamp": self._now_iso()},
        )
        return normalized

    def _apply_revision(
        self, steps: list[dict], pending: dict, completed: dict,
        confirmed: list[dict] | None,
    ) -> None:
        """把确认后的修订计划写回待执行集合；取消则保持原计划。"""
        if confirmed is None:
            return
        by_id = {s.get("step_id"): s for s in confirmed}
        for k in list(pending):
            if k not in by_id:
                del pending[k]
        for s in confirmed:
            sid = s.get("step_id")
            if sid and sid not in completed:
                pending[sid] = s
        # 同步到 steps 列表，保证前端树与结果 zip 使用修订后的步骤
        for i, st in enumerate(steps):
            if st.get("step_id") in by_id:
                steps[i] = by_id[st.get("step_id")]

    def _publish_full_state(self, task_id: str, goal: str, all_steps: list[dict], completed_all: dict) -> None:
        """跨迭代推送全量步骤状态（前端按轮次展示）。"""
        current = []
        for s in all_steps:
            c = dict(s)
            c["result"] = completed_all.get(s["step_id"], {})
            current.append(c)
        try:
            self._messaging.publish("orchestrator:response", {
                "task_id": task_id,
                "status": "RUNNING",
                "steps": current,
                "goal": goal,
            })
        except Exception as exc:
            logger.warning("Full state push failed: %s", str(exc)[:120])

    def _replan_step(self, goal: str, step: dict, error: str, task_id: str) -> dict | None:
        """步骤失败后，让 LLM 提出一个替代步骤（保留原步骤 ID 的依赖关系）。"""
        # 搜索/抓取类失败直接降级为 LLM 生产，不再重复尝试外部获取
        failed_cap = step.get("capability", "")
        _SEARCH_FAILURE_SIGNALS = (
            "No URL", "no url", "empty", "filtered", "无结果", "没有找到",
            "未找到", "No relevant", "not found",
        )
        _CODE_FAILURE_SIGNALS = (
            "No code generated", "Empty content", "代码生成失败",
            "SyntaxError", "ModuleNotFoundError", "Script exited with code",
            "Traceback", "No module named",
        )
        if (
            failed_cap in ("web_search", "web_fetch")
            or any(sig in str(error) for sig in _SEARCH_FAILURE_SIGNALS)
            or (failed_cap == "code_execution" and any(sig in str(error) for sig in _CODE_FAILURE_SIGNALS))
        ):
            alt = self._generation_fallback_step(goal, step)
            alt["step_id"] = f"alt-{step.get('step_id', '?')}-{int(time.time())}"
            push_progress(self._messaging, task_id, "log",
                          {"type": "replan", "agent": "orchestrator",
                           "message": f"Replan (search-fallback): {alt.get('capability')}: {str(alt.get('instruction'))[:60]}",
                           "timestamp": self._now_iso()})
            return alt
        prompt = (
            f"Goal: {goal}\n\n"
            f"Failed step: [{step.get('capability')}] {step.get('instruction')}\n"
            f"Error: {str(error)[:300]}\n\n"
            "Propose ONE alternative step that avoids this failure. "
            "If the failure is caused by missing external information, the alternative MUST be "
            "content_summary or code_execution that generates the deliverable directly from model knowledge; "
            "do NOT propose web_search or web_fetch again. "
            'Output ONLY this JSON: {"steps":[{"step_id":"alt","capability":"...","instruction":"...","timeout":120}]}'
        )
        try:
            raw = self._plan_llm.call(PLANNER_SYSTEM, prompt, expect_json=True)
            if isinstance(raw, dict):
                plan_data = raw
            else:
                clean = str(raw).strip()
                if clean.startswith("```json"):
                    clean = clean[7:]
                if clean.startswith("```"):
                    clean = clean[3:]
                if clean.endswith("```"):
                    clean = clean[:-3]
                    plan_data = _loads_json_loose(clean.strip())
            steps = plan_data.get("steps", [])
            if steps:
                alt = steps[0]
                alt["step_id"] = f"alt-{step.get('step_id', '?')}-{int(time.time())}"
                push_progress(self._messaging, task_id, "log",
                              {"type": "replan", "agent": "orchestrator",
                               "message": f"Replan: {alt.get('capability')}: {str(alt.get('instruction'))[:60]}",
                               "timestamp": self._now_iso()})
                return alt
        except Exception as exc:
            logger.error("Replan failed for step %s: %s", step.get("step_id"), str(exc)[:200])
        return None

    def _review_plan(self, goal: str, steps: list[dict], task_id: str) -> list[dict]:
        """把计划草案交给 Critic 评审；FAIL 则修订一次；超时/异常兜底放行。"""
        plan_id = f"plan-{task_id}-{int(time.time())}"
        try:
            r = self._new_redis_sync()
            self._messaging.publish("orchestrator:plan_draft", {
                "plan_id": plan_id,
                "goal": goal,
                "steps": steps,
            })
            push_progress(self._messaging, task_id, "log",
                          {"type": "review", "agent": "critic",
                           "message": "Plan submitted for review", "timestamp": self._now_iso()})
            msg = r.brpop([f"plan_review:{plan_id}"], timeout=self._critic_timeout)
            if not msg:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "critic",
                               "message": f"Review timeout ({self._critic_timeout}s), proceeding",
                               "timestamp": self._now_iso()})
                return steps
            review = json.loads(msg[1])
            verdict = str(review.get("verdict", "PASS")).upper()
            if verdict == "PASS":
                scores = review.get("scores", {})
                push_progress(self._messaging, task_id, "log",
                              {"type": "review", "agent": "critic",
                               "message": f"Review PASSED {scores}", "timestamp": self._now_iso()})
                return steps
            suggestions = review.get("suggestions") or []
            push_progress(self._messaging, task_id, "log",
                          {"type": "review", "agent": "critic",
                           "message": f"Review FAILED, revising ({len(suggestions)} suggestions)",
                           "timestamp": self._now_iso()})
            revised = self._revise_plan(goal, steps, suggestions, task_id)
            return revised if revised else steps
        except Exception as exc:
            logger.warning("Critic review failed, proceeding: %s", str(exc)[:200])
            return steps

    def _revise_plan(self, goal: str, steps: list[dict], suggestions: list[str], task_id: str) -> list[dict] | None:
        """按 Critic 建议让 LLM 修订计划。"""
        import json as _json
        prompt = (
            f"Goal: {goal}\n\n"
            f"Original plan: {_json.dumps(steps, ensure_ascii=False)}\n\n"
            f"Critic suggestions: {_json.dumps(suggestions, ensure_ascii=False)}\n\n"
            "Revise the plan to address ALL suggestions. "
            'Output ONLY this JSON: {"steps":[{"step_id":"1","capability":"...","instruction":"...","depends_on":[],"timeout":120}]}'
        )
        try:
            raw = self._plan_llm.call(PLANNER_SYSTEM, prompt, expect_json=True)
            if isinstance(raw, dict):
                plan_data = raw
            else:
                clean = str(raw).strip()
                if clean.startswith("```json"):
                    clean = clean[7:]
                if clean.startswith("```"):
                    clean = clean[3:]
                if clean.endswith("```"):
                    clean = clean[:-3]
                plan_data = _loads_json_loose(clean.strip())
            revised = plan_data.get("steps") or []
            if revised:
                for i, s in enumerate(revised):
                    s.setdefault("step_id", str(i + 1))
                    s.setdefault("timeout", 120)
            return revised
        except Exception as exc:
            logger.error("Plan revision failed: %s", str(exc)[:200])
            return None

    # ── Dispatch ──
    def _dispatch(self, step: dict, task_id: str) -> dict:
        """Send one step to a worker and wait for result."""
        capability = step.get("capability", "")
        instruction = step.get("instruction", "")
        step_id = step.get("step_id", uuid.uuid4().hex[:8])
        timeout = max(step.get("timeout", 300), 90)

        agent_id = self._find_agent(capability)
        if not agent_id:
            # Worker 可能瞬时掉线（如 Redis 超时重连），等待重查后再判失败
            for _ in range(3):
                time.sleep(5)
                agent_id = self._find_agent(capability)
                if agent_id:
                    break
        if not agent_id:
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": "orchestrator",
                           "message": f"No agent for {capability}", "timestamp": self._now_iso()})
            return {"task_id": step_id, "status": "FAILED", "result": f"No worker for {capability}"}

        push_progress(self._messaging, task_id, "log",
                      {"type": "dispatch", "agent": capability,
                       "message": f"Dispatching to {agent_id}: {instruction[:60]}", "timestamp": self._now_iso()})
        push_progress(self._messaging, task_id, "agent_status",
                      {"agent_id": agent_id, "status": "busy"})

        # Push task to worker queue
        r = self._new_redis_sync()
        # 唯一派发 ID：避免 task_result:{step_id} 与其它任务/历史残留键碰撞
        # （步骤 ID 如 "1"/"2" 在所有任务中通用，曾导致跨任务误取结果）
        dispatch_id = f"{step_id}-{uuid.uuid4().hex[:8]}"
        r.lpush(f"task_queue:{agent_id}", json.dumps({"task_id": dispatch_id, "instruction": instruction}))

        # Wait for result
        result = self._wait_for_result(dispatch_id, timeout)
        if result:
            result = self._normalize_result(result)
            # 展示层保留原步骤 ID（任务结果仅用于完成状态与内容）
            result["task_id"] = step_id
            push_progress(self._messaging, task_id, "agent_status",
                          {"agent_id": agent_id, "status": "idle"})
            push_progress(self._messaging, task_id, "log",
                          {"type": "success" if result.get("status") == "SUCCESS" else "error",
                           "agent": capability, "message": f"Step {step_id}: {result.get('status', '?')}",
                           "timestamp": self._now_iso()})
        else:
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": capability,
                           "message": f"Step {step_id} timed out ({timeout}s)", "timestamp": self._now_iso()})
            result = {"task_id": step_id, "status": "FAILED", "result": f"Timeout after {timeout}s"}

        return result

    def _wait_for_result(self, task_id: str, timeout: int) -> dict | None:
        """Block until worker result arrives via Redis BRPOP."""
        r = self._new_redis_sync()
        deadline = time.time() + max(timeout, 5)
        msg = self._brpop_with_deadline(r, f"task_result:{task_id}", deadline)
        if not msg:
            return None
        try:
            return json.loads(msg[1])
        except Exception as e:
            logger.warning("Result parse error for %s: %s", task_id, e)
            return None

    def _normalize_result(self, result: dict) -> dict:
        """识别 Worker 返回中的显式失败标记，避免"假成功"污染结果。

        覆盖两类情况：
        - 顶层 status 已是 FAILED/ERROR
        - 结果体是结构化 JSON 且 status 标记为 failed/error
          （data_loader / data_analyzer / model_trainer / report_generator 的错误返回）
        """
        status = str(result.get("status", "SUCCESS")).upper()
        if status in ("FAILED", "ERROR"):
            return result
        payload = result.get("result")
        if isinstance(payload, dict):
            inner = str(payload.get("status", "")).lower()
            if inner in ("failed", "failure", "error"):
                logger.info(
                    "Step %s worker-reported failure detected: %s",
                    result.get("task_id", "?"),
                    str(payload.get("error", ""))[:120],
                )
                result["status"] = "FAILED"
        return result

    # ── Finalize ──
    def _finalize(self, goal: str, steps: list[dict], results: list[dict]) -> str:
        """Generate final report."""
        ok = sum(1 for r in results if r.get("status") == "SUCCESS")
        fail = len(results) - ok
        status = "SUCCESS" if fail == 0 else "PARTIAL" if ok > 0 else "FAILED"

        report = f"## Task Report\\n\\nGoal: {goal}\\nStatus: {status}\\nSteps: {len(steps)} ({ok} OK, {fail} failed)\\n\\n"
        for s, r in zip(steps, results):
            report += f"- [{r.get('status', '?')}] {s.get('instruction', '?')[:60]}"
            if r.get("result"):
                report += f"\\n  Result: {str(r['result'])[:200]}"
            report += "\\n"
        return report

    def _build_delivery_summary(
        self, goal: str, all_steps: list[dict], completed_all: dict,
    ) -> str:
        """任务收尾：用代码生成"交付结果说明"，回答"项目结果如何"——
        交付了哪些文件、运行验证是否成功、如何启动。不依赖 LLM，保证一定包含。"""
        import tempfile, zipfile

        results = [completed_all.get(s["step_id"], {}) for s in all_steps]
        ok = sum(1 for r in results if r.get("status") == "SUCCESS")
        fail = len(results) - ok
        status = "SUCCESS" if fail == 0 else "PARTIAL" if ok > 0 else "FAILED"

        # 1) 交付文件：从 package 步骤结果解析 zip 条目
        files: list[dict] = []
        zip_path = None
        for s, r in zip(all_steps, results):
            if s.get("capability") == "package":
                text = str(r.get("result") or "")
                m = re.search(r"Download: file://([^\s]+)", text)
                if m and os.path.exists(m.group(1).strip()):
                    zip_path = m.group(1).strip()
                    break
        if zip_path:
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    for info in zf.infolist():
                        if info.is_dir() or "_check_" in info.filename or info.filename.startswith("__pycache__"):
                            continue
                        ext = os.path.splitext(info.filename)[1].lower().lstrip(".")
                        kind = (
                            "html" if ext == "html"
                            else "py" if ext == "py"
                            else "md" if ext in ("md", "markdown")
                            else ext or "file"
                        )
                        files.append({"name": info.filename, "size": info.file_size, "kind": kind})
            except Exception:
                pass
        files.sort(key=lambda x: x["name"])

        lines = ["# 项目交付结果", "", f"**目标**：{goal[:200]}",
                 f"**状态**：{status}（{ok}/{len(results)} 个步骤成功）", ""]
        if files:
            lines.append("## 交付文件")
            for f in files:
                size_kb = (f["size"] or 0) / 1024
                lines.append(f"- `{f['name']}`（{f['kind']}，{size_kb:.1f} KB）")
            lines.append("")

        # 2) 运行验证：code_execution 步骤的真实运行结果
        run_lines = []
        for s, r in zip(all_steps, results):
            if s.get("capability") == "code_execution" and r.get("status") == "SUCCESS":
                try:
                    parsed = json.loads(str(r.get("result") or ""))
                    out = str(parsed.get("output") or "")[:160]
                    rc = parsed.get("returncode")
                except Exception:
                    out, rc = "", None
                run_lines.append(
                    f"- {str(s.get('instruction'))[:50]}：运行{'成功' if rc == 0 or rc is None else '异常'}"
                    + (f"（{out}）" if out else "")
                )
        if run_lines:
            lines.append("## 运行验证")
            lines.extend(run_lines)
            lines.append("")

        # 2.5) 贯通测试：把最终交付物当作整体验证能否运行
        if files:
            import tempfile as _tempfile
            project_dir = os.path.join(_tempfile.gettempdir(), "agent_workspace", "project")
            e2e = self._run_e2e_verification(files, project_dir)
            if e2e:
                lines.append("## 贯通测试（整体可运行性）")
                passed = sum(1 for r in e2e if r.get("ok"))
                lines.append(f"**结果**：{passed}/{len(e2e)} 项通过")
                for r in e2e:
                    mark = "✅" if r.get("ok") else "❌"
                    lines.append(f"- {mark} `{r['name']}`（{r['type']}）：{r.get('detail', '')}")
                lines.append("")

        # 3) 如何启动
        htmls = [f for f in files if f["kind"] == "html"]
        pys = [f for f in files if f["kind"] == "py"]
        if htmls or pys:
            lines.append("## 如何启动")
            if htmls:
                lines.append(f"- 网页版：在任务控制台交付文件区点击「打开」按钮，或访问 `/files/{htmls[0]['name']}` 在浏览器中游玩")
            if pys:
                lines.append(f"- 脚本版：在交付文件区点击「运行」按钮执行 `{pys[0]['name']}`")
            lines.append("")
        lines.append("> 以下为任务执行过程中的详细内容（设计文档 / 过程记录）。")
        return "\n".join(lines)

    def _run_e2e_verification(
        self, files: list[dict], project_dir: str,
    ) -> list[dict]:
        """对最终交付物做贯通验证（确定性，不依赖 LLM）：
        HTML → 文档结构 + 内联 JS 语法（node --check）+ 本地 HTTP 可访问；
        PY → 编译 + 无头冒烟运行（超时视为启动成功）。"""
        import http.server
        import socketserver
        import subprocess
        import tempfile
        import urllib.request

        results: list[dict] = []
        htmls = [f for f in files if f["kind"] == "html"]
        pys = [f for f in files if f["kind"] == "py"]

        # Node 是否可用（用于 JS 语法校验）
        js_checker = None
        try:
            p = subprocess.run(["node", "--version"], capture_output=True, timeout=10)
            if p.returncode == 0:
                js_checker = "node"
        except Exception:
            js_checker = None

        for f in htmls:
            fp = os.path.join(project_dir, f["name"])
            notes: list[str] = []
            ok = True
            if not os.path.isfile(fp):
                results.append({"name": f["name"], "type": "html", "ok": False, "detail": "文件不存在"})
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except Exception as exc:
                results.append({"name": f["name"], "type": "html", "ok": False, "detail": f"读取失败: {exc}"})
                continue
            if "<!doctype html" not in content.lower() and "<html" not in content.lower():
                ok = False
                notes.append("缺少 HTML 文档结构")
            if "<canvas" not in content.lower():
                notes.append("无 <canvas>")
            if "<script" not in content.lower():
                ok = False
                notes.append("无 <script>（页面没有交互逻辑）")
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", content, re.S)
            if js_checker and scripts:
                tmp_js = ""
                try:
                    with tempfile.NamedTemporaryFile(
                        "w", suffix=".js", delete=False, encoding="utf-8",
                    ) as tf:
                        tf.write("\n".join(scripts))
                        tmp_js = tf.name
                    p = subprocess.run(
                        [js_checker, "--check", tmp_js],
                        capture_output=True, timeout=15,
                    )
                    if p.returncode != 0:
                        ok = False
                        notes.append(
                            "JS 语法错误: " + p.stderr.decode("utf-8", errors="replace")[:120]
                        )
                except Exception as exc:
                    notes.append(f"JS 校验异常: {exc}")
                finally:
                    try:
                        if tmp_js:
                            os.unlink(tmp_js)
                    except Exception:
                        pass
            # 本地 HTTP 可访问性（模拟在浏览器中打开）
            try:
                class _H(http.server.SimpleHTTPRequestHandler):
                    def __init__(self, *a, **k):
                        super().__init__(*a, directory=project_dir, **k)

                    def log_message(self, *a):
                        pass

                srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
                port = srv.server_address[1]
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/{f['name']}", timeout=10,
                    ) as resp:
                        body = resp.read(256)
                        if resp.status != 200 or not body:
                            ok = False
                            notes.append("HTTP 无法访问")
                        else:
                            notes.append("HTTP 200 可访问")
                finally:
                    srv.shutdown()
            except Exception as exc:
                ok = False
                notes.append(f"HTTP 失败: {exc}")
            results.append({
                "name": f["name"], "type": "html",
                "ok": ok, "detail": "；".join(notes) or "通过",
            })

        for f in pys:
            fp = os.path.join(project_dir, f["name"])
            if not os.path.isfile(fp):
                results.append({"name": f["name"], "type": "py", "ok": False, "detail": "文件不存在"})
                continue
            try:
                p = subprocess.run(
                    [sys.executable, "-m", "py_compile", fp],
                    capture_output=True, timeout=20,
                )
                if p.returncode != 0:
                    results.append({
                        "name": f["name"], "type": "py", "ok": False,
                        "detail": "编译失败: " + p.stderr.decode("utf-8", errors="replace")[:150],
                    })
                    continue
            except Exception as exc:
                results.append({"name": f["name"], "type": "py", "ok": False, "detail": f"编译异常: {exc}"})
                continue
            env = dict(os.environ)
            env["SDL_VIDEODRIVER"] = "dummy"
            try:
                proc = subprocess.Popen(
                    [sys.executable, fp],
                    cwd=os.path.dirname(fp),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
                try:
                    out, _ = proc.communicate(timeout=15)
                    rc = proc.returncode
                    ok = rc == 0
                    detail = out.decode("utf-8", errors="replace")[:120] if ok else f"退出码 {rc}"
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    ok = True
                    detail = "启动成功（15s 超时未崩溃）"
                results.append({
                    "name": f["name"], "type": "py",
                    "ok": ok, "detail": detail,
                })
            except Exception as exc:
                results.append({"name": f["name"], "type": "py", "ok": False, "detail": f"运行异常: {exc}"})
        return results

    def _best_deliverable(self, goal: str, steps: list[dict], results: list[dict]) -> str:
        """从步骤结果中挑选最实质的交付内容作为最终报告。

        优先 content_summary / report_generator 的 Markdown 文档（含文件读取）；
        code_execution 的长文本仅作兜底；命中目标主题的候选优先，
        避免跑偏内容（如房价报告出现在游戏任务中）胜出；都没有则返回空串（走汇总兜底）。
        """
        report_files: list[str] = []
        summary_docs: list[str] = []
        code_texts: list[str] = []
        for s, r in zip(steps, results):
            if r.get("status") != "SUCCESS":
                continue
            cap = s.get("capability", "")
            if cap not in ("content_summary", "report_generator", "code_execution"):
                continue
            text = r.get("result", "")
            if isinstance(text, dict):
                path = text.get("report_path") or text.get("path")
                if path and os.path.exists(path):
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            content = f.read()
                        if content.strip():
                            (report_files if cap == "report_generator" else summary_docs).append(content)
                    except Exception:
                        pass
                continue
            if isinstance(text, str):
                # report_generator 返回的是 JSON 字符串，需要解析出 report_path
                if cap == "report_generator":
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            path = parsed.get("report_path") or parsed.get("path")
                            if path and os.path.exists(path):
                                with open(path, "r", encoding="utf-8") as f:
                                    content = f.read()
                                if content.strip():
                                    report_files.append(content)
                                    continue
                    except Exception:
                        pass
                stripped = text.strip()
                if len(stripped) < 200:
                    continue
                if stripped.startswith("{") or stripped.startswith("["):
                    continue
                if cap in ("content_summary", "report_generator"):
                    summary_docs.append(stripped)
                else:
                    code_texts.append(stripped)

        def _pick(pool: list[str]) -> str:
            if not pool:
                return ""
            goal_tokens = self._topic_tokens(goal)
            if goal_tokens:
                on_topic = [
                    c for c in pool
                    if any(t in c.lower() for t in goal_tokens)
                ]
                pool = on_topic or pool
            md = [c for c in pool if ("#" in c[:200] or "|" in c[:500] or "```" in c)]
            return max(md or pool, key=len)

        # 优先级：report_generator 落盘正式文档 > 内容摘要 > 代码文本
        for pool in (report_files, summary_docs, code_texts):
            best = _pick(pool)
            if best:
                logger.info("Final report: deliverable from step content (%d chars)", len(best))
                return best
        return ""

    # ── Main Loop ──
    def run(self, task_id: str, goal: str, context: str = "",
            auto_run: bool = True, template_steps: list | None = None) -> dict:
        """Execute a full task lifecycle. Returns final status dict."""
        started = time.time()
        logger.info("Task %s: %s", task_id, goal[:80])

        # 1. Plan（模板步骤直接采用，否则 LLM 规划）
        if template_steps:
            steps = self._normalize_steps(template_steps)
            steps = self._ensure_report_step(steps, task_id)
        else:
            steps = self._plan(goal, task_id, context)
        steps = self._wire_report_deps(steps)
        steps = self._wire_search_fetch_deps(steps)
        steps = self._ensure_package_step(steps)
        steps = self._break_cycles(steps)
        steps = self._inject_goal_into_steps(steps, goal)
        if not steps:
            push_progress(self._messaging, task_id, "task_complete",
                          {"status": "FAILED", "summary": "Planning failed"})
            return {"task_id": task_id, "status": "FAILED", "steps": [], "report": "No plan generated"}

        push_progress(self._messaging, task_id, "plan_update",
                      {"steps": steps})

        # 计划确认阶段（auto_run=False 时等待用户编辑/确认）
        if not auto_run and not template_steps:
            self._messaging.publish("orchestrator:response", {
                "task_id": task_id,
                "status": "AWAITING_CONFIRM",
                "steps": steps,
                "goal": goal,
            })
            confirmed = self._wait_plan_confirm(task_id, steps)
            if confirmed is None:
                push_progress(self._messaging, task_id, "task_complete",
                              {"status": "FAILED", "summary": "Plan not confirmed, task cancelled"})
                return {"task_id": task_id, "status": "FAILED", "steps": [],
                        "report": "Plan not confirmed"}
            steps = confirmed
            steps = self._wire_report_deps(steps)
            steps = self._wire_search_fetch_deps(steps)
            steps = self._ensure_package_step(steps)
            steps = self._break_cycles(steps)
            steps = self._inject_goal_into_steps(steps, goal)
            if not steps:
                push_progress(self._messaging, task_id, "task_complete",
                              {"status": "FAILED", "summary": "Empty plan confirmed, task cancelled"})
                return {"task_id": task_id, "status": "FAILED", "steps": [],
                        "report": "Empty plan confirmed"}
            push_progress(self._messaging, task_id, "plan_update", {"steps": steps})

        # 2..N. 执行 + 自主迭代（执行 → 验收评审 → 追加步骤，直到通过或达到上限）
        all_steps: list[dict] = []
        completed_all: dict = {}
        has_failure = False
        iteration = 0
        last_steps = steps
        last_results: list[dict] = []
        best_report = ""

        while True:
            iter_results, iter_failed = self._execute_steps(steps, task_id, goal)
            has_failure = has_failure or iter_failed
            last_steps = steps
            last_results = iter_results
            for s, r in zip(steps, iter_results):
                completed_all[s["step_id"]] = r
                s.setdefault("iteration", iteration)
            all_steps.extend(steps)

            cand = self._best_deliverable(goal, last_steps, last_results)
            if len(cand) > len(best_report):
                best_report = cand
            self._publish_full_state(task_id, goal, all_steps, completed_all)

            if has_failure or self._max_iterations <= 0 or iteration >= self._max_iterations:
                break
            verdict = self._reflect(goal, best_report, task_id)
            if not verdict or verdict.get("accepted"):
                if verdict and verdict.get("accepted"):
                    push_progress(self._messaging, task_id, "log",
                                  {"type": "info", "agent": "orchestrator",
                                   "message": "Reflection: deliverable accepted",
                                   "timestamp": self._now_iso()})
                break
            gaps = verdict.get("gaps") or []
            next_steps = self._normalize_steps(verdict.get("next_steps") or [])
            if not next_steps:
                break
            iteration += 1
            for s in next_steps:
                s["iteration"] = iteration
                s["depends_on"] = []
                s["step_id"] = f"i{iteration}-{s['step_id']}"
            steps = next_steps
            steps = self._wire_report_deps(steps)
            steps = self._wire_search_fetch_deps(steps)
            steps = self._ensure_package_step(steps)
            steps = self._break_cycles(steps)
            steps = self._inject_goal_into_steps(steps, goal)
            push_progress(self._messaging, task_id, "log",
                          {"type": "iteration", "agent": "orchestrator",
                           "message": f"Iteration {iteration}: closing {len(gaps)} gaps with {len(steps)} steps",
                           "timestamp": self._now_iso()})
            push_progress(self._messaging, task_id, "plan_update", {"steps": steps})

        delivery = self._build_delivery_summary(goal, all_steps, completed_all)
        detail = best_report or self._finalize(goal, all_steps, [
            completed_all.get(s["step_id"], {}) for s in all_steps
        ])
        report = delivery + "\n\n---\n\n" + detail

        # 3. Report
        push_progress(self._messaging, task_id, "log",
                      {"type": "info", "agent": "orchestrator",
                       "message": f"Generating report ({len(all_steps)} steps, {iteration} iterations, {time.time()-started:.0f}s)",
                       "timestamp": self._now_iso()})

        overall = "FAILED" if has_failure else "SUCCESS"
        self._publish_usage()

        # 4. Memory（仅沉淀成功计划，避免污染 successful_strategies）
        if not has_failure:
            with self._memory_lock:
                self._memory.consolidate_memory(goal, all_steps, report)
            push_progress(self._messaging, task_id, "log",
                          {"type": "memory", "agent": "orchestrator",
                           "message": "Strategy memory consolidated", "timestamp": self._now_iso()})

        # 5. Complete
        ok_count = sum(1 for r in completed_all.values() if r.get("status") == "SUCCESS")
        push_progress(self._messaging, task_id, "task_complete",
                      {"status": overall,
                       "summary": f"{overall}: {ok_count}/{len(all_steps)} steps, {iteration} iterations",
                       "report": report})

        return {
            "task_id": task_id,
            "status": overall,
            "steps": [{"step_id": s["step_id"], "capability": s["capability"],
                        "instruction": s["instruction"], "iteration": s.get("iteration", 0),
                        "result": completed_all.get(s["step_id"], {})}
                      for s in all_steps],
            "final_report": report,
        }

    def _wait_plan_confirm(self, task_id: str, original_steps: list[dict]) -> list[dict] | None:
        """等待用户确认/编辑计划；返回确认后的步骤，取消或超时返回 None。"""
        try:
            msg = self._brpop_with_deadline(
                self._redis,
                f"plan_confirm:{task_id}",
                time.time() + self._plan_confirm_timeout,
            )
            if not msg:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": f"Plan confirm timeout ({self._plan_confirm_timeout}s), cancelling",
                               "timestamp": self._now_iso()})
                return None
            data = json.loads(msg[1] if isinstance(msg[1], str) else msg[1].decode())
            if data.get("action") == "cancel":
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": "Plan cancelled by user", "timestamp": self._now_iso()})
                return None
            new_steps = data.get("steps")
            if not new_steps:
                return original_steps
            normalized = self._normalize_steps(new_steps)
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": f"Plan confirmed with {len(normalized)} steps",
                           "timestamp": self._now_iso()})
            return normalized
        except Exception as exc:
            logger.warning("Plan confirm error for %s: %s", task_id, str(exc)[:120])
            return original_steps

    def _execute_steps(self, steps: list[dict], task_id: str, goal: str) -> tuple[list[dict], bool]:
        """并行 DAG 执行一轮步骤，返回（按步骤顺序的结果列表, 是否有失败）。"""
        completed: dict = {}
        has_failure = False
        state = {"replan_used": 0}
        step_ids = {s.get("step_id") for s in steps}
        lock = threading.Lock()

        def deps_ok(step):
            return all(d in completed for d in step.get("depends_on", []))

        def deps_failed(step):
            return [d for d in step.get("depends_on", [])
                    if d in completed and completed[d].get("status") == "FAILED"]

        def execute_step(step):
            step_start = time.time()
            step["instruction"] = self._inject_step_context(step, completed, lock)
            blocked = deps_failed(step)
            if blocked:
                push_progress(self._messaging, task_id, "log",
                              {"type": "replan", "agent": "orchestrator",
                               "message": f"Step {step['step_id']} blocked by failure: {blocked}",
                               "timestamp": self._now_iso()})
                return {"task_id": step["step_id"], "status": "FAILED",
                        "result": f"Blocked by: {blocked}",
                        "elapsed_sec": round(time.time() - step_start, 1)}
            result = self._dispatch_step_safe(goal, step, task_id, state)
            result["elapsed_sec"] = round(time.time() - step_start, 1)
            return result

        for s in steps:
            dangling = [d for d in s.get("depends_on", []) if d not in step_ids]
            if dangling:
                completed[s["step_id"]] = {
                    "task_id": s["step_id"], "status": "FAILED",
                    "result": f"Dangling dependency: {dangling}",
                }
                has_failure = True

        pending = {s["step_id"]: s for s in steps if s["step_id"] not in completed}
        last_progress = time.time()
        in_flight = 0
        revision_done = False

        def worker():
            nonlocal last_progress, has_failure, in_flight, revision_done
            while True:
                with lock:
                    if not pending:
                        return
                    ready = None
                    for k, s in pending.items():
                        if deps_ok(s) and not deps_failed(s):
                            ready = (k, pending.pop(k))
                            break
                if ready is None:
                    with lock:
                        # 传递式阻塞传播：任何步骤一旦依赖失败/已阻塞步骤，
                        # 立即标记为 Blocked，避免“报告依赖全部步骤”等链条卡死
                        changed = True
                        while changed:
                            changed = False
                            for k in list(pending):
                                s = pending[k]
                                failed_deps = [
                                    d for d in s.get("depends_on", [])
                                    if d in completed and completed[d].get("status") == "FAILED"
                                ]
                                if failed_deps:
                                    completed[k] = {
                                        "task_id": k, "status": "FAILED",
                                        "result": f"Blocked by failed dependency: {failed_deps}",
                                    }
                                    del pending[k]
                                    has_failure = True
                                    changed = True
                        if not pending:
                            return
                        if pending and all(deps_failed(s) for s in pending.values()):
                            for k in list(pending):
                                completed[k] = {
                                    "task_id": k, "status": "FAILED",
                                    "result": "Blocked by failed dependency",
                                }
                            pending.clear()
                            has_failure = True
                            return
                    time.sleep(0.5)
                    continue
                k, step = ready
                with lock:
                    in_flight += 1
                try:
                    result = execute_step(step)
                except Exception as exc:
                    logger.error("Step %s crashed: %s", k, str(exc)[:200])
                    result = {"task_id": k, "status": "FAILED", "result": f"Step crashed: {exc}"}
                finally:
                    with lock:
                        in_flight -= 1
                with lock:
                    completed[k] = result
                    last_progress = time.time()
                    if result.get("status") != "SUCCESS":
                        has_failure = True
                if step.get("capability") == "web_search":
                    res_raw = result.get("result", "")
                    try:
                        parsed = json.loads(res_raw) if isinstance(res_raw, str) else res_raw
                        if isinstance(parsed, list) and not parsed and not revision_done:
                            with lock:
                                revision = self._build_search_revision(pending, goal)
                                revision_done = True
                            if revision:
                                confirmed = self._confirm_revision(task_id, goal, steps, completed, revision)
                                with lock:
                                    self._apply_revision(steps, pending, completed, confirmed)
                                    last_progress = time.time()
                                self._push_realtime_state(task_id, goal, steps, completed)
                            else:
                                push_progress(
                                    self._messaging, task_id, "log",
                                    {"type": "info", "agent": "orchestrator",
                                     "message": "Search returned no relevant results; no dependent fetch steps to revise",
                                     "timestamp": self._now_iso()},
                                )
                        elif isinstance(parsed, list) and not parsed:
                            push_progress(
                                self._messaging, task_id, "log",
                                {"type": "info", "agent": "orchestrator",
                                 "message": "Search returned no relevant results; continuing with direct generation",
                                 "timestamp": self._now_iso()},
                            )
                    except Exception:
                        pass
                self._push_realtime_state(task_id, goal, steps, completed)

        def watchdog():
            nonlocal has_failure
            while True:
                with lock:
                    if not pending:
                        return
                    stalled = in_flight == 0 and time.time() - last_progress > self._stall_timeout
                if stalled:
                    with lock:
                        for k in list(pending):
                            completed[k] = {
                                "task_id": k, "status": "FAILED",
                                "result": "Stalled: dependency never satisfied (cycle?)",
                            }
                        pending.clear()
                        has_failure = True
                    return
                time.sleep(5)

        threads = [threading.Thread(target=worker, daemon=True)
                   for _ in range(max(1, self._max_parallel))]
        for t in threads:
            t.start()
        wd = threading.Thread(target=watchdog, daemon=True)
        wd.start()
        for t in threads:
            t.join()
        wd.join(timeout=10)

        results = [
            completed.get(s["step_id"], {
                "task_id": s["step_id"], "status": "FAILED", "result": "Not executed",
            })
            for s in steps
        ]
        has_failure = has_failure or any(r.get("status") != "SUCCESS" for r in results)
        return results, has_failure

    def _inject_step_context(self, step: dict, completed: dict, lock: threading.Lock) -> str:
        """按能力类型把前序步骤的输出注入指令（URL/路径/结果摘要）。"""
        instr = step.get('instruction', '')
        cap = step.get('capability', '')
        deps = step.get('depends_on', [])

        def _prev(dep_id):
            with lock:
                prev = completed.get(dep_id)
            return prev.get('result', '') if isinstance(prev, dict) else ''

        if cap in ('data_loader', 'web_fetch'):
            for dep_id in deps:
                prev_res = _prev(dep_id)
                try:
                    prev_json = json.loads(prev_res) if isinstance(prev_res, str) else prev_res
                except Exception:
                    prev_json = prev_res
                if isinstance(prev_json, list):
                    for item in prev_json:
                        url = item.get('url') or item.get('href') or ''
                        if url and url.startswith('http'):
                            instr += f' [URL: {url}]'
                            break
                elif isinstance(prev_json, dict):
                    url = prev_json.get('url') or prev_json.get('href') or ''
                    if url:
                        instr += f' [URL: {url}]'
                urls = re.findall(r'https?://\S+', prev_res if isinstance(prev_res, str) else '')
                if urls:
                    instr += f' [URL: {urls[0]}]'

        if cap in ('data_analyzer', 'model_trainer'):
            for dep_id in deps:
                prev_res = _prev(dep_id)
                try:
                    prev_json = json.loads(prev_res) if isinstance(prev_res, str) else prev_res
                except Exception:
                    prev_json = prev_res
                if isinstance(prev_json, dict):
                    path = prev_json.get('path') or prev_json.get('data_path') or prev_json.get('report_path') or ''
                    if path:
                        instr += f' [Data: {path}]'
                tmps = re.findall(r'/tmp/\S+', prev_res if isinstance(prev_res, str) else '')
                if tmps:
                    instr += f' [Path: {tmps[0]}]'

        if cap == 'file_io':
            for dep_id in deps:
                prev_res = _prev(dep_id)
                snippet = str(prev_res)[:12000] if prev_res else ''
                if snippet:
                    instr += f"\n[上一步结果 {dep_id}]:\n{snippet}"

        if cap in ('content_summary', 'report_generator'):
            for dep_id in deps:
                prev_res = _prev(dep_id)
                snippet = str(prev_res)[:2500] if prev_res else ''
                if snippet:
                    instr += f"\n[上一步结果 {dep_id}]:\n{snippet}"
                # 读取产物文件（如 code_execution 落盘的 HTML/代码），给报告真实素材
                try:
                    parsed = json.loads(prev_res) if isinstance(prev_res, str) else prev_res
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    fpath = parsed.get("path") or parsed.get("report_path") or parsed.get("data_path") or ""
                    if fpath and os.path.exists(fpath):
                        try:
                            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                                content = f.read(4000)
                            if content.strip():
                                instr += f"\n[产物文件 {dep_id} ({os.path.basename(fpath)})]:\n{content[:3000]}"
                        except Exception:
                            pass
            instr += ("\n[指令] 仅使用与任务目标主题直接相关的信息；"
                      "若上一步结果中的来源与主题无关（如无关政策新闻、其他领域文档、垃圾站点或空结果），"
                      "一律不要纳入输出，并注明已剔除无关内容。")
        return instr

    def _inject_goal_into_steps(self, steps: list[dict], goal: str) -> list[dict]:
        """把用户目标注入报告/摘要步骤，避免报告生成器无主题可写。"""
        if not goal:
            return steps
        for s in steps:
            if s.get("capability") in ("content_summary", "report_generator"):
                s["instruction"] = (
                    f"用户目标：{goal[:300]}\n"
                    f"原始指令：{s.get('instruction', '')}"
                )
        return steps

    def _dispatch_step_safe(self, goal: str, step: dict, task_id: str, state: dict) -> dict:
        """派发单步：失败自动重试，重试仍失败则尝试单步重规划。"""
        result = self._dispatch(step, task_id)
        attempt = 0
        while result.get("status") == "FAILED" and attempt < self._max_retry:
            attempt += 1
            push_progress(self._messaging, task_id, "log",
                          {"type": "retry", "agent": step.get("capability", "?"),
                           "message": f"Step {step.get('step_id')} retry {attempt}/{self._max_retry}",
                           "timestamp": self._now_iso()})
            time.sleep(2)
            result = self._dispatch(step, task_id)
        if result.get("status") == "FAILED" and state["replan_used"] < self._replan_depth:
            alt = self._replan_step(goal, step, result.get("result", ""), task_id)
            if alt:
                state["replan_used"] += 1
                alt_result = self._dispatch(alt, task_id)
                if alt_result.get("status") == "SUCCESS":
                    alt_result["replanned"] = True
                    alt_result["replan_instruction"] = alt.get("instruction", "")
                    result = alt_result
        return result

    def _push_realtime_state(self, task_id: str, goal: str, steps: list[dict], completed: dict) -> None:
        """步骤完成后推送当前全量状态（前端实时展示）。"""
        self._publish_usage()
        current_steps = []
        for s in steps:
            s_copy = dict(s)
            s_copy["result"] = completed.get(s["step_id"], {})
            current_steps.append(s_copy)
        try:
            self._messaging.publish("orchestrator:response", {
                "task_id": task_id,
                "status": "RUNNING",
                "steps": current_steps,
                "goal": goal,
            })
        except Exception as exc:
            logger.warning("Realtime push failed: %s", str(exc)[:120])

    def _publish_usage(self) -> None:
        """把编排器进程的 LLM 用量累计快照写入 Redis，供 web_ui 跨进程读取。"""
        try:
            self._redis.set("llm_usage", json.dumps(get_usage_stats()), ex=3600)
        except Exception:
            pass

    def shutdown(self):
        self._messaging.close()


# ─────────────────────────────────────────────
# Standalone listener (drop-in replacement)
# ─────────────────────────────────────────────

def main():
    from logging_setup import setup_logging
    setup_logging("orchestrator")
    r = OrchestratorV2._new_redis_sync()
    # 清理历史残留的结果键，避免与本次运行的新派发 ID 混淆（防御性清理）
    try:
        stale = r.keys("task_result:*")
        if stale:
            r.delete(*stale)
            logger.info("Cleaned %d stale task_result keys", len(stale))
    except Exception:
        pass
    orch = OrchestratorV2()

    logger.info("OrchestratorV2 listening on orchestrator:main")
    ps = r.pubsub()
    ps.subscribe("orchestrator:main")

    for msg in ps.listen():
        if msg["type"] != "message":
            continue
        try:
            data = json.loads(msg["data"])
            task_id = data.get("task_id", "")
            goal = data.get("goal", "")
            context = data.get("context", "")
            auto_run = data.get("auto_run", True)

            if goal == "EVOLUTION_TRIGGER":
                push_progress(orch._messaging, task_id, "log",
                              {"type": "info", "message": "Evolution trigger received"})
                # Run evolution sandbox in background thread
                def _run_evo():
                    try:
                        from evolution_sandbox import EvolutionSandbox
                        sandbox = EvolutionSandbox(orch._messaging, orch._sqlite_reg)
                        result = sandbox.evolve("search_agent")
                        push_progress(orch._messaging, task_id, "task_complete",
                                      {"status": "SUCCESS", "summary": result.get("summary", "Evolution complete")})
                        orch._messaging.publish("orchestrator:evolution_result", result)
                    except Exception as e:
                        logger.error("Evolution error: %s", e)
                        push_progress(orch._messaging, task_id, "task_complete",
                                      {"status": "FAILED", "summary": f"Evolution error: {e}"})
                threading.Thread(target=_run_evo, daemon=True).start()
                continue

            # Run task in background thread
            def _run_task(tid, g, ctx, ar, tpl_steps):
                try:
                    result = orch.run(tid, g, ctx, auto_run=ar, template_steps=tpl_steps)
                    orch._messaging.publish("orchestrator:response", result)
                except Exception as e:
                    logger.error("Task %s failed: %s", tid, e)
                    orch._messaging.publish("orchestrator:response",
                                            {"task_id": tid, "status": "FAILED", "report": str(e)})
            threading.Thread(
                target=_run_task,
                args=(task_id, goal, context, auto_run, data.get("template_steps")),
                daemon=True,
            ).start()

        except Exception as e:
            logger.error("Message error: %s", e)


if __name__ == "__main__":
    main()
