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

Output ONLY this JSON with no extra text:
{"steps":[{"step_id":"1","capability":"web_search","instruction":"search for house price dataset","depends_on":[],"timeout":60}]}"""

ITERATOR_SYSTEM = """你是严格的交付验收评审。根据用户目标评估交付物是否达标。
输出严格JSON：{"accepted": true|false, "gaps": ["缺口1","缺口2"], "next_steps":[{"step_id":"1","capability":"...","instruction":"...","timeout":120}]}
规则：
1. accepted=false 只用于明确缺失用户要求的内容；不要吹毛求疵
2. next_steps 每次最多3个，必须具体可执行，指令用中文并严格围绕目标主题
3. 可用能力：web_search, web_fetch, data_loader, data_analyzer, model_trainer, report_generator, content_summary, code_execution, file_io, package
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
        except Exception:
            pass
        import redis as sync_redis
        self._redis = sync_redis.Redis(host="localhost", port=6379, decode_responses=True)
        self._redis_reg = RedisAgentRegistry(self._redis)
        self._sqlite_reg = AgentRegistry("agents.db")
        self._messaging = MessagingClient("localhost", 6379)
        # MessagingClient auto-connects
        self._memory = MemoryManager(os.environ.get("MEMORY_DIR", "./chroma_memory"))
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
                if isinstance(raw, str):
                    clean = raw.strip()
                    if clean.startswith('```json'):
                        clean = clean[7:]
                    if clean.startswith('```'):
                        clean = clean[3:]
                    if clean.endswith('```'):
                        clean = clean[:-3]
                    plan_data = json.loads(clean.strip())
                elif isinstance(raw, dict):
                    plan_data = raw
                else:
                    plan_data = json.loads(str(raw))
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
            s.setdefault("capability", "content_summary")
            s.setdefault("timeout", 120)
            out.append(s)
        if len(out) > self._max_steps:
            logger.warning("Plan normalized from %d to %d steps (max_steps=%d)",
                           len(out), self._max_steps, self._max_steps)
            out = out[:self._max_steps]
        return out

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
            return json.loads(clean.strip())
        except Exception as exc:
            logger.warning("Reflection failed, stopping iteration: %s", str(exc)[:150])
            return None

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
        prompt = (
            f"Goal: {goal}\n\n"
            f"Failed step: [{step.get('capability')}] {step.get('instruction')}\n"
            f"Error: {str(error)[:300]}\n\n"
            "Propose ONE alternative step that avoids this failure. "
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
                plan_data = json.loads(clean.strip())
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
            import redis as _redis
            r = _redis.Redis(host="localhost", port=6379, decode_responses=True)
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
                plan_data = _json.loads(clean.strip())
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
        import redis
        r = redis.Redis(host="localhost", port=6379, decode_responses=True)
        r.lpush(f"task_queue:{agent_id}", json.dumps({"task_id": step_id, "instruction": instruction}))

        # Wait for result
        result = self._wait_for_result(step_id, timeout)
        if result:
            result = self._normalize_result(result)
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
        import redis
        r = redis.Redis(host="localhost", port=6379, decode_responses=True)
        try:
            msg = r.brpop([f"task_result:{task_id}"], timeout)
            if msg:
                _, payload = msg
                return json.loads(payload)
        except Exception as e:
            logger.warning("BRPOP error for %s: %s", task_id, e)
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

    def _best_deliverable(self, steps: list[dict], results: list[dict]) -> str:
        """从步骤结果中挑选最实质的交付内容作为最终报告。

        优先 content_summary / report_generator / code_execution 的长文本产出；
        report_generator 只返回文件路径时读取落盘内容；都没有则返回空串（走汇总兜底）。
        """
        candidates: list[str] = []
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
                            candidates.append(content)
                    except Exception:
                        pass
                continue
            if isinstance(text, str):
                stripped = text.strip()
                if len(stripped) < 200:
                    continue
                # 排除 JSON 输出（如 web_search / code 的序列化结果）
                if stripped.startswith("{") or stripped.startswith("["):
                    continue
                candidates.append(stripped)
        if not candidates:
            return ""
        best = max(candidates, key=len)
        logger.info("Final report: deliverable from step content (%d chars)", len(best))
        return best

    # ── Main Loop ──
    def run(self, task_id: str, goal: str, context: str = "", auto_run: bool = True) -> dict:
        """Execute a full task lifecycle. Returns final status dict."""
        started = time.time()
        logger.info("Task %s: %s", task_id, goal[:80])

        # 1. Plan
        steps = self._plan(goal, task_id, context)
        if not steps:
            push_progress(self._messaging, task_id, "task_complete",
                          {"status": "FAILED", "summary": "Planning failed"})
            return {"task_id": task_id, "status": "FAILED", "steps": [], "report": "No plan generated"}

        push_progress(self._messaging, task_id, "plan_update",
                      {"steps": steps})

        # 计划确认阶段（auto_run=False 时等待用户编辑/确认）
        if not auto_run:
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
            push_progress(self._messaging, task_id, "plan_update", {"steps": steps})

        # 2..N. 执行 + 自主迭代（执行 → 验收评审 → 追加步骤，直到通过或达到上限）
        all_steps: list[dict] = []
        completed_all: dict = {}
        has_failure = False
        iteration = 0
        last_steps = steps
        last_results: list[dict] = []

        while True:
            iter_results, iter_failed = self._execute_steps(steps, task_id, goal)
            has_failure = has_failure or iter_failed
            last_steps = steps
            last_results = iter_results
            for s, r in zip(steps, iter_results):
                completed_all[s["step_id"]] = r
                s.setdefault("iteration", iteration)
            all_steps.extend(steps)

            report = self._best_deliverable(last_steps, last_results) or self._finalize(goal, last_steps, last_results)
            self._publish_full_state(task_id, goal, all_steps, completed_all)

            if has_failure or self._max_iterations <= 0 or iteration >= self._max_iterations:
                break
            verdict = self._reflect(goal, report, task_id)
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
            push_progress(self._messaging, task_id, "log",
                          {"type": "iteration", "agent": "orchestrator",
                           "message": f"Iteration {iteration}: closing {len(gaps)} gaps with {len(steps)} steps",
                           "timestamp": self._now_iso()})
            push_progress(self._messaging, task_id, "plan_update", {"steps": steps})

        # 3. Report
        push_progress(self._messaging, task_id, "log",
                      {"type": "info", "agent": "orchestrator",
                       "message": f"Generating report ({len(all_steps)} steps, {iteration} iterations, {time.time()-started:.0f}s)",
                       "timestamp": self._now_iso()})

        overall = "FAILED" if has_failure else "SUCCESS"
        self._publish_usage()

        # 4. Memory（仅沉淀成功计划，避免污染 successful_strategies）
        if not has_failure:
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
            msg = self._redis.brpop([f"plan_confirm:{task_id}"], timeout=self._plan_confirm_timeout)
            if not msg:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": f"Plan confirm timeout ({self._plan_confirm_timeout}s), cancelling",
                               "timestamp": self._now_iso()})
                return None
            data = json.loads(msg[1])
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

        def worker():
            nonlocal last_progress, has_failure, in_flight
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
                self._push_realtime_state(task_id, goal, steps, completed)

        def watchdog():
            nonlocal has_failure
            while True:
                with lock:
                    if not pending:
                        return
                    stalled = in_flight == 0 and time.time() - last_progress > 60
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

        if cap == 'data_loader':
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

        if cap in ('content_summary', 'report_generator'):
            for dep_id in deps:
                prev_res = _prev(dep_id)
                snippet = str(prev_res)[:2500] if prev_res else ''
                if snippet:
                    instr += f"\n[上一步结果 {dep_id}]:\n{snippet}"
        return instr

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
    import redis as sync_redis
    r = sync_redis.Redis(host="localhost", port=6379, decode_responses=True)
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
            def _run_task(tid, g, ctx, ar):
                try:
                    result = orch.run(tid, g, ctx, auto_run=ar)
                    orch._messaging.publish("orchestrator:response", result)
                except Exception as e:
                    logger.error("Task %s failed: %s", tid, e)
                    orch._messaging.publish("orchestrator:response",
                                            {"task_id": tid, "status": "FAILED", "report": str(e)})
            threading.Thread(target=_run_task, args=(task_id, goal, context, auto_run), daemon=True).start()

        except Exception as e:
            logger.error("Message error: %s", e)


if __name__ == "__main__":
    main()
