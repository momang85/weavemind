"""Orchestrator V2 — clean, linear, push-progress-enabled.

Reuses all existing components: llm_client, common, async_worker_base, memory_manager,
critic_agent, ws_helpers. No complex state machine — just: plan → dispatch → collect → report.

This is the active orchestrator (legacy orchestrator.py was removed in the architecture cleanup).
"""

import json, logging, os, re, threading, time, uuid

from workspace import (
    ensure_task_workspace,
    task_charts_dir,
    task_data_dir,
    task_project_dir,
    task_reports_dir,
    task_workspace,
)

from common import AgentRegistry, MessagingClient, RedisAgentRegistry
from llm_client import LLMClient, get_usage_stats

# 贯通测试用：canvas 像素指纹（采样哈希），用于判断游戏画面是否仍在变化
_FINGERPRINT_JS = """() => {
    const cv = document.querySelector('canvas');
    if (!cv || !cv.width || !cv.height) return 'no-canvas';
    try {
        const ctx = cv.getContext('2d');
        const img = ctx.getImageData(0, 0, cv.width, cv.height).data;
        let h = 7;
        for (let i = 0; i < img.length; i += 977) {
            h = ((h << 5) - h + img[i] * 3 + (img[i + 1] || 0) * 5 + (img[i + 2] || 0) * 7) | 0;
        }
        return 'h' + h;
    } catch (e) { return 'err'; }
}"""
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
10. 当任务涉及"报告/分析/研报/调研"时：必须保留所有搜索结果的原始 URL，并把 URL 列表传给 report_generator；
    报告步骤的指令必须包含"将图表嵌入报告"和"在报告末尾标注每条数据的来源链接"
11. 若目标明确要求"图表/可视化/趋势图/plot/chart"，计划必须包含 data_analyzer 或 code_execution 图表生成步骤

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
    # 通用词：出现在几乎任何计划指令里，不能作为"对题"证据
    "文件", "游戏", "页面", "程序", "应用", "系统", "内容", "结果", "报告",
    "html", "HTML", "功能", "实现", "进行", "生成", "编写",
}

ITERATOR_SYSTEM = """你是严格的交付验收评审。根据用户目标评估交付物是否达标。
输出严格JSON：{"accepted": true|false, "score": 0-10, "gaps": ["缺口1","缺口2"], "next_steps":[{"step_id":"1","capability":"...","instruction":"...","timeout":120}]}
规则：
1. score 是交付物与目标的吻合度（0-10）。只有 score < 6 且存在明确缺失时才 accepted=false；
   核心内容已完成、只是缺少可选的润色项时，score 应给 6 分以上并 accepted=true
2. accepted=false 只用于明确缺失用户要求的内容；不要吹毛求疵，不要"锦上添花"
3. next_steps 每次最多3个，必须具体可执行，指令用中文并严格围绕目标主题
4. 每个 next_step 的 capability 只能是下列之一，且每步只能一个：
   web_search, web_fetch, data_loader, data_analyzer, model_trainer, report_generator, content_summary, code_execution, file_io, package
5. 若交付物缺失的原因是外部资料获取失败（搜索无结果、网页无内容、URL 无效），next_steps 必须改为直接生成（content_summary / code_execution），禁止再安排 web_search 或 web_fetch
6. 若目标明确要求"可视化/图表/趋势图"且交付物没有生成任何图表（无 PNG），
   next_steps 必须包含一个 code_execution 图表生成步骤（用 matplotlib 基于已有检索数据/摘要作图），禁止只追加报告步骤
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
        self._max_reflection_steps = 3
        self._reflection_accept_score = 6.0
        self._plan_confirm_timeout = 300
        self._stall_timeout = 300
        self._max_offtopic_regenerations = 2
        self._planner_model = None
        self._task_starts: dict[str, float] = {}
        self._task_simple: dict[str, bool] = {}
        self._task_sources: dict[str, list[str]] = {}
        self._task_sources_lock = threading.Lock()
        self._task_starts_lock = threading.Lock()
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
            self._max_reflection_steps = max(1, int(_sys.get('max_reflection_steps', 3)))
            self._reflection_accept_score = max(0.0, float(_sys.get('reflection_accept_score', 6.0)))
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
            # pubsub 长连接不能有读超时：默认 5s 会导致阻塞读偶发超时重连，
            # 消息处理被延迟数分钟；连接超时保留 5s 快速失败
            socket_timeout=None,
            socket_connect_timeout=5,
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
    def _inject_memory_context(self, goal: str, task_id: str) -> str:
        """查历史经验并推送 memory 日志（无经验时也明确提示），
        模板路径与 LLM 规划路径统一从这里拿上下文。"""
        with self._memory_lock:
            memory_context = self._memory.inject_context(goal)
        if memory_context:
            push_progress(self._messaging, task_id, "log",
                          {"type": "memory", "agent": "orchestrator",
                           "message": f"Memory: 注入历史经验（{len(memory_context)} chars）",
                           "timestamp": self._now_iso()})
        else:
            push_progress(self._messaging, task_id, "log",
                          {"type": "memory", "agent": "orchestrator",
                           "message": "Memory: 未找到相关历史经验",
                           "timestamp": self._now_iso()})
        return memory_context

    def _plan(self, goal: str, task_id: str, context: str = "",
              memory_context: str = "") -> list[dict]:
        """Ask LLM to decompose goal into steps."""
        direct = self._direct_deliverable_plan(goal)
        if direct:
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": "Plan: deterministic direct-delivery template (LLM planning skipped)",
                           "timestamp": self._now_iso()})
            return self._normalize_steps(direct)
        push_progress(self._messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator", "message": f"Planning: {goal[:60]}", "timestamp": self._now_iso()})
        if context:
            logger.info("Task %s using conversation context (%d chars)", task_id, len(context))
            push_progress(self._messaging, task_id, "log",
                          {"type": "context", "agent": "orchestrator",
                           "message": f"Conversation context: {len(context)} chars",
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
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": f"正在规划（LLM 第 {attempt + 1}/2 次尝试）...",
                           "timestamp": self._now_iso()})
            try:
                raw = self._planner_llm.call(PLANNER_SYSTEM, attempt_prompt, expect_json=True)
                plan_data = self._parse_plan_response(raw)
                break
            except Exception as e:
                last_error = e
                logger.warning("Plan attempt %d failed: %s", attempt + 1, str(e)[:200])
                push_progress(self._messaging, task_id, "log",
                              {"type": "error", "agent": "orchestrator",
                               "message": f"规划第 {attempt + 1} 次尝试失败：{str(e)[:80]}，重试中",
                               "timestamp": self._now_iso()})
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
        # 规划自检：计划主题与目标明显不符时，用强约束重生成（最多 N 次）
        _topic_ok = self._plan_topic_ok(goal, steps) if steps else True
        logger.info("Topic guard: goal_esc=%s tokens=%s ok=%s steps=%d",
                    goal.encode("unicode_escape")[:120], sorted(self._topic_tokens(goal))[:10],
                    _topic_ok, len(steps))
        if steps and not _topic_ok:
            logger.warning("Plan appears off-topic for goal: %s", goal[:50])
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": "Plan off-topic, regenerating with strict topic prompt",
                           "timestamp": self._now_iso()})
            for _regen in range(self._max_offtopic_regenerations):
                try:
                    strict_prompt = (
                        "严格围绕用户目标主题重写计划，禁止偏离、泛化或替换到其他主题；"
                        "计划步骤必须明确包含目标的核心对象（如游戏类型、题材等）。"
                        f"用户目标：\n{goal}\n\n重新输出严格JSON计划。"
                    )
                    raw2 = self._planner_llm.call(PLANNER_SYSTEM, strict_prompt, expect_json=True)
                    steps2 = self._normalize_steps(self._parse_plan_response(raw2))
                    steps2 = self._ensure_report_step(steps2, task_id)
                    if steps2 and self._plan_topic_ok(goal, steps2):
                        steps = steps2
                        break
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

    def _direct_deliverable_plan(self, goal: str) -> list[dict] | None:
        """明确产物型任务：跳过 LLM 规划，用确定性模板（生成→报告→打包），
        消除规划漂移并减少延迟。需要外部信息（调研/数据/分析）的任务不走此路径。"""
        g = goal.lower()
        deliver_markers = (
            "游戏", "脚本", "工具", "单文件", "html", ".py", "页面",
            "小应用", "贪吃蛇", "愤怒的小鸟", "计算器", "待办", "打砖块",
            "俄罗斯方块",
        )
        research_markers = (
            "调研", "市场", "分析", "数据", "roi", "对比", "现状",
            "预测", "方案", "报告", "趋势", "评估",
        )
        if any(m in g for m in deliver_markers) and not any(m in g for m in research_markers):
            return [
                {
                    "step_id": "1",
                    "capability": "code_execution",
                    "instruction": (
                        f"根据目标生成完整可运行的自包含交付物（单文件 HTML 或 Python 脚本），"
                        f"确保能直接在浏览器/命令行运行并验证通过：{goal[:500]}"
                    ),
                    "timeout": 300,
                    "depends_on": [],
                },
                {
                    "step_id": "2",
                    "capability": "report_generator",
                    "instruction": (
                        f"汇总交付结果，生成面向用户的最终交付报告（Markdown，"
                        f"包含功能清单与运行方式）：{goal[:300]}"
                    ),
                    "timeout": 120,
                    "depends_on": ["1"],
                },
                {
                    "step_id": "3",
                    "capability": "package",
                    "instruction": (
                        "将本次任务产出的所有文件打包为一个 ZIP 交付包"
                        "（包含代码、资源、报告等），并返回下载链接。"
                    ),
                    "timeout": 120,
                    "depends_on": ["1", "2"],
                },
            ]
        return None

    def _load_templates(self) -> list[dict]:
        """读取确定性模板库（含手动模板与进化沉淀的 auto 模板）。"""
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates.json")
            with open(path, "r", encoding="utf-8") as f:
                return (json.load(f) or {}).get("templates", [])
        except Exception:
            return []

    def _route_template(self, goal: str, task_id: str = "") -> list[dict] | None:
        """由 LLM 判断目标是否适合确定性模板（含直接交付模板）；
        失败时回退到关键词判断。返回模板 steps 或 None（走完整规划）。"""
        templates = self._load_templates()
        # 关键词快速路由：明确命中的任务直接走确定性模板，省掉 LLM 路由调用
        direct = self._direct_deliverable_plan(goal)
        if direct:
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": "模板路由：直接交付（关键词命中，跳过 LLM）",
                           "timestamp": self._now_iso()})
            return direct
        tpl = self._template_keyword_match(goal, templates)
        if tpl:
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": f"模板命中（关键词）：{tpl.get('name')}",
                           "timestamp": self._now_iso()})
            return tpl.get("steps") or None
        if not templates:
            return self._direct_deliverable_plan(goal)
        push_progress(self._messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator",
                       "message": f"模板路由（LLM）：{len(templates)} 个模板待匹配",
                       "timestamp": self._now_iso()})
        try:
            tpl_list = "\n".join(
                f"- {t.get('name')}: {str(t.get('goal'))[:120]}" for t in templates
            )
            prompt = (
                "判断以下用户目标是否适合使用现成的确定性执行模板。\n"
                f"可用模板：\n{tpl_list or '（无）'}\n\n"
                "规则：\n"
                '1. 仅当目标与某个模板的【核心任务】基本一致时才选择该模板'
                '（例如目标是"房价预测/数据科学流水线"才能选"数据分析流水线"；'
                '目标是"用户评论情感分析"而模板是房价预测 → 不匹配）→ {"template": "模板名"}\n'
                '2. 若目标适合"直接交付"（单产物如游戏/脚本/工具/单文件页面，'
                '无需外部调研或多技能协作）→ {"template": "direct_deliverable"}\n'
                '3. 否则（需要规划拆解/多技能协作/外部资料）→ {"template": null}\n'
                f"目标：{goal[:400]}\n只输出JSON。"
            )
            raw = self._plan_llm.call(
                "你是任务路由专家，判断任务类型并选择模板，只输出JSON。",
                prompt,
                expect_json=True,
            )
            if isinstance(raw, dict):
                name = raw.get("template")
            else:
                clean = str(raw).strip()
                if clean.startswith("```"):
                    clean = re.sub(r"^```[a-zA-Z]*\s*", "", clean).rstrip("`").strip()
                name = json.loads(clean).get("template")
            if name == "direct_deliverable":
                direct = self._direct_deliverable_plan(goal)
                if direct:
                    return direct
            elif name:
                for t in templates:
                    if t.get("name") == name:
                        return t.get("steps") or None
        except Exception as exc:
            logger.info("Template routing via LLM skipped: %s", exc)
        push_progress(self._messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator",
                       "message": "模板路由结束：未命中模板，进入 LLM 规划",
                       "timestamp": self._now_iso()})
        # 关键词回退
        return self._direct_deliverable_plan(goal)

    @staticmethod
    def _template_keyword_match(goal: str, templates: list[dict]) -> dict | None:
        """模板关键词快速匹配（保守：只在强信号时命中，避免误路由）。"""
        g = str(goal or "").lower()
        conds = {
            "数据分析流水线": (
                ("房价",), ("数据集",), ("数据科学",), ("回归",),
                ("模型训练",), ("机器学习",), ("eda",),
            ),
            "行业调研报告": (
                ("调研", "行业"), ("调研", "市场"), ("市场规模",), ("行业现状",),
            ),
            "董事会汇报": (
                ("可行性", "方案"), ("roi",), ("董事会",), ("可行性", "评估"),
            ),
        }
        for t in templates:
            groups = conds.get(str(t.get("name") or ""))
            if not groups:
                continue
            for grp in groups:
                if all(k in g for k in grp):
                    return t
        return None

    def _consolidate_template(
        self, goal: str, all_steps: list[dict], tpl_path: str | None = None,
    ) -> None:
        """成功的复杂任务沉淀为确定性模板：提炼能力序列与指令骨架，
        供后续 LLM 路由选择。"""
        try:
            if not all_steps:
                return
            steps: list[dict] = []
            for s in all_steps[:8]:
                cap = s.get("capability")
                if cap in ("report_generator", "package"):
                    continue  # 收尾打包类步骤不进模板，保留核心能力链（含内容摘要）
                ins = str(s.get("instruction") or "")
                if "用户目标：" in ins:
                    ins = ins.split("用户目标：", 1)[-1]
                if "任务目标：" in ins:
                    ins = ins.split("任务目标：", 1)[-1]
                if "原始指令：" in ins:
                    ins = ins.split("原始指令：", 1)[-1]
                if not ins.strip():
                    continue
                steps.append({
                    "step_id": str(len(steps) + 1),
                    "capability": cap,
                    "instruction": ins.strip()[:120],
                    "timeout": 180,
                })
            if len(steps) < 2:
                return
            # 主题一致性校验：步骤指令必须包含目标主题词，否则可能是跑偏任务（不沉淀）
            hay = " ".join(str(s.get("instruction", "")) for s in steps)
            tokens = self._topic_tokens(goal)
            if not tokens:
                # 目标无法提取主题词（可能中文损坏/过短）：保守不沉淀，避免污染模板库
                logger.info("Template not consolidated (no topic tokens): %s", goal[:40])
                return
            if not any(t in hay for t in tokens):
                logger.info("Template not consolidated (off-topic execution): %s", goal[:40])
                return
            name = f"auto-{goal[:10]}"
            path = tpl_path or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "templates.json",
            )
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tpls = data.get("templates", [])
            tpls = [t for t in tpls if t.get("name") != name]
            tpls.append({"name": name, "goal": goal[:200], "steps": steps})
            data["templates"] = tpls[-30:]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Template consolidated: %s (%d steps)", name, len(steps))
        except Exception as exc:
            logger.warning("Template consolidation failed: %s", exc)

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
            # 打包必须等所有步骤（含摘要/报告）完成，否则工作区还没有新文件可打包
            "depends_on": [s.get("step_id") for s in steps],
            "timeout": 180,
        }]

    def _wire_report_deps(self, steps: list[dict]) -> list[dict]:
        """报告/摘要/打包步骤若无依赖，按信息流向接线：
        摘要依赖信息源步骤；报告依赖信息源+摘要；打包依赖所有产物步骤。
        避免"互相依赖所有步骤"形成环，被 break_cycles 清空后变成全并行。"""
        ids = [s.get("step_id") for s in steps]
        cap = {s.get("step_id"): s.get("capability") for s in steps}
        for s in steps:
            c = s.get("capability")
            if c == "content_summary" and not s.get("depends_on"):
                s["depends_on"] = [
                    i for i in ids
                    if i != s.get("step_id") and cap.get(i) not in (
                        "content_summary", "report_generator", "package",
                    )
                ]
            elif c == "report_generator" and not s.get("depends_on"):
                s["depends_on"] = [
                    i for i in ids
                    if i != s.get("step_id") and cap.get(i) not in (
                        "report_generator", "package",
                    )
                ]
            elif c == "package" and not s.get("depends_on"):
                s["depends_on"] = [
                    i for i in ids if i != s.get("step_id") and cap.get(i) != "package"
                ]
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
            s.setdefault("timeout", 300)
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
            # 生成-验证-审查循环耗尽（如 HTML 反复截断）也必须回到代码生成，
            # 不得被降级成 content_summary 之类只产出文本的步骤
            "No valid code", "generation/verify/review", "HTML incomplete",
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
        # LLM 生成/运行较慢：普通步骤下限 300s，code_execution（含生成-修复循环）下限 600s
        timeout = max(int(step.get("timeout", 300) or 300), 300)
        if capability == "code_execution":
            timeout = max(timeout, 600)

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
        with self._task_starts_lock:
            task_start_ts = self._task_starts.get(task_id, time.time())
        r.lpush(f"task_queue:{agent_id}", json.dumps({
            "task_id": dispatch_id,
            "instruction": instruction,
            "task_start_ts": task_start_ts,
            "workspace": str(task_workspace(task_id)),
            "simple": bool(self._task_simple.get(task_id, False)),
        }, ensure_ascii=False))

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
        self, task_id: str, goal: str, all_steps: list[dict], completed_all: dict,
    ) -> tuple[str, list[dict]]:
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
                    zip_path = m.group(1).strip()  # 取最后一个（修复轮的最终交付包）
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
            lines.append(f"**成果文件夹**：`{task_workspace(task_id)}`（每个任务独立目录，可整体移动/备份）")
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
        e2e: list[dict] = []
        if files:
            import tempfile as _tempfile
            project_dir = str(task_project_dir(task_id))
            e2e = self._run_e2e_verification(
                files, project_dir, game_goal=self._is_game_goal(goal),
            )
            if e2e:
                lines.append("## 贯通测试（整体可运行性）")
                passed = sum(1 for r in e2e if r.get("ok"))
                lines.append(f"**结果**：{passed}/{len(e2e)} 项通过")
                for r in e2e:
                    mark = "✅" if r.get("ok") else "❌"
                    line = f"- {mark} `{r['name']}`（{r['type']}）：{r.get('detail', '')}"
                    if r.get("screenshot") and os.path.exists(str(r["screenshot"])):
                        rel_shot = os.path.relpath(r["screenshot"], project_dir).replace("\\", "/")
                        line += f"（[可玩性截图](/files/{task_id}/{rel_shot})）"
                    lines.append(line)
                lines.append("")

        # 3) 如何启动
        htmls = [f for f in files if f["kind"] == "html"]
        pys = [f for f in files if f["kind"] == "py"]
        if htmls or pys:
            lines.append("## 如何启动")
            if htmls:
                lines.append(
                    f"- 网页版：在任务控制台交付文件区点击「打开」按钮，"
                    f"或访问 `/files/{task_id}/{htmls[0]['name']}` 在浏览器中游玩"
                )
            if pys:
                lines.append(f"- 脚本版：在交付文件区点击「运行」按钮执行 `{pys[0]['name']}`")
            lines.append("")
        lines.append("> 以下为任务执行过程中的详细内容（设计文档 / 过程记录）。")
        return "\n".join(lines), e2e

    def _cleanup_project_workspace(self, task_id: str) -> None:
        """任务开始时清空本任务的成果目录（project/reports/data/charts），
        保证"一次运行 = 一个干净文件夹"；每任务独立，不影响其他任务。"""
        import shutil
        for sub in ("project", "reports", "data", "charts"):
            d = task_workspace(task_id) / sub
            if not d.is_dir():
                continue
            try:
                for name in os.listdir(d):
                    fp = os.path.join(str(d), name)
                    if os.path.isfile(fp) or os.path.islink(fp):
                        os.remove(fp)
                    elif os.path.isdir(fp):
                        shutil.rmtree(fp, ignore_errors=True)
            except Exception as exc:
                logger.warning("Workspace cleanup failed for %s/%s: %s", task_id, sub, exc)
        logger.info("Task workspace cleaned for %s", task_id)

    @staticmethod
    def _supersede_key(name: str) -> str:
        """归一化交付物名：index_1786354743.html -> index（去掉时间戳后缀）。"""
        stem = os.path.splitext(os.path.basename(str(name)))[0]
        m = re.match(r"^(.*)_\d{9,11}$", stem)
        return m.group(1) if m else stem

    @staticmethod
    def _wants_visualization(goal: str) -> bool:
        """目标是否明确要求可视化/图表（只有此时才生成检索数据图表）。"""
        g = str(goal or "").lower()
        return any(k in g for k in (
            "可视化", "图表", "趋势图", "柱状", "饼图", "折线",
            "plot", "chart", "graph",
        ))

    @staticmethod
    def _is_game_goal(goal: str) -> bool:
        """判断目标是否"可玩"类（游戏/交互），决定贯通测试走哪种验证。"""
        g = str(goal or "").lower()
        return any(k in g for k in (
            "游戏", "玩", "playable", "game", "canvas", "pygame",
            "贪吃蛇", "打砖块", "弹弓", "小鸟", "棋盘", "2048", "扫雷",
            "五子棋", "射击", "闯关", "体感", "可玩",
        ))

    def _prune_superseded_files(
        self, task_id: str, all_steps: list[dict],
        completed_all: dict, e2e_results: list[dict],
    ) -> bool:
        """同基础名的失败交付物若有已通过的兄弟版本（迭代补洞产生的双份文件），
        删除失败版（磁盘 + 交付 zip），保证交付包只含可用产物。"""
        if not e2e_results:
            return False
        passed_keys = {
            self._supersede_key(r["name"])
            for r in e2e_results if r.get("ok") and r.get("name")
        }
        pruned = [
            r["name"] for r in e2e_results
            if not r.get("ok") and r.get("name")
            and self._supersede_key(r["name"]) in passed_keys
        ]
        if not pruned:
            return False
        import tempfile as _tf
        project_dir = os.path.abspath(str(task_project_dir(task_id)))
        removed: list[str] = []
        for name in pruned:
            fp = os.path.abspath(os.path.join(project_dir, name))
            if not fp.startswith(project_dir + os.sep) or not os.path.isfile(fp):
                continue  # 路径穿越防护 / 文件已不存在
            try:
                os.remove(fp)
                removed.append(name)
                logger.info("Pruned superseded broken deliverable: %s", name)
            except Exception as exc:
                logger.warning("Prune failed for %s: %s", name, exc)
        if not removed:
            return False
        # 同步从最后一个交付 zip 中剔除，保持前端交付列表一致
        try:
            zip_path = None
            for s in all_steps:
                r = completed_all.get(s["step_id"], {})
                if s.get("capability") != "package":
                    continue
                text = str(r.get("result") or "")
                m = re.search(r"Download: file://([^\s]+)", text)
                if m and os.path.exists(m.group(1).strip()):
                    zip_path = m.group(1).strip()
            if zip_path:
                _fd, _tmp = _tf.mkstemp(
                    suffix=".zip", dir=os.path.dirname(zip_path)
                )
                os.close(_fd)
                import zipfile
                with zipfile.ZipFile(zip_path) as zin, \
                        zipfile.ZipFile(_tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                    for info in zin.infolist():
                        if info.is_dir() or info.filename in removed:
                            continue
                        zout.writestr(info, zin.read(info.filename))
                os.replace(_tmp, zip_path)
        except Exception as exc:
            logger.warning("Delivery zip prune failed: %s", exc)
        push_progress(self._messaging, task_id, "log",
                      {"type": "info", "agent": "orchestrator",
                       "message": f"清理 {len(removed)} 个被新版替换的失败交付物",
                       "timestamp": self._now_iso()})
        return True

    def _sweep_workspace_artifacts(self, task_id: str) -> None:
        """收尾清扫：删除 __pycache__ 与临时校验文件，只保留最新交付包，
        让成果文件夹干净可移动。"""
        import shutil
        ws = task_workspace(task_id)
        try:
            for p in ws.rglob("*"):
                try:
                    if p.name == "__pycache__" and p.is_dir():
                        shutil.rmtree(p, ignore_errors=True)
                    elif p.is_file() and (
                        p.name.startswith("_check_")
                        or p.name.startswith(".test_")
                        or p.suffix in (".pyc", ".pyo")
                    ):
                        p.unlink(missing_ok=True)
                except Exception:
                    continue
            # 多轮迭代会产生多个 zip（每轮打包一次）：只保留最新一份
            zips = sorted(
                (p for p in ws.glob("*.zip") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            for z in zips[:-1]:
                try:
                    z.unlink(missing_ok=True)
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("Workspace sweep failed for %s: %s", task_id, exc)

    @staticmethod
    def _rewrite_report_links(report: str, task_id: str) -> str:
        """把报告 Markdown 链接目标里的任务工作区绝对路径改写成
        /files/<task_id>/ URL（图表/数据图片在浏览器里才能显示）；
        正文里的绝对路径（如"成果文件夹"）保持不变。"""
        ws = str(task_workspace(task_id))
        ws_bs = ws.replace("/", "\\")
        seg = f"/files/{task_id}"

        def _fix_target(m):
            t = m.group(2).replace(ws_bs, seg).replace(ws, seg).replace("\\", "/")
            return m.group(1) + t + m.group(3)

        return re.sub(r"(\]\()([^)\s]+)(\))", _fix_target, report)

    def _run_e2e_verification(
        self, files: list[dict], project_dir: str, game_goal: bool = True,
    ) -> list[dict]:
        """对最终交付物做贯通验证（确定性，不依赖 LLM）：
        HTML → 文档结构 + 内联 JS 语法（node --check）+ 本地 HTTP 可访问；
        PY → 编译 + 无头冒烟运行（超时视为启动成功）。"""
        import http.server
        import socketserver
        import subprocess
        import sys
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
            # 优先浏览器级"可玩"验证（Playwright 缺失时自动安装）
            try:
                pw_ok, pw_detail, shot = self._playwright_verify(
                    project_dir, f["name"], fp, require_game=game_goal,
                )
            except Exception as exc:
                pw_ok, pw_detail, shot = False, f"Playwright 验证异常: {exc}", ""
            if pw_ok:
                results.append({
                    "name": f["name"], "type": "html", "ok": True,
                    "detail": pw_detail, "screenshot": shot,
                })
                continue
            if "降级" not in pw_detail and "不可用" not in pw_detail:
                results.append({
                    "name": f["name"], "type": "html", "ok": False,
                    "detail": pw_detail, "screenshot": shot,
                })
                continue
            # Playwright 不可用 → 降级为静态检查
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
            if content.lower().count("<script") != content.lower().count("</script>"):
                ok = False
                notes.append("<script>/</script> 标签不配平（JS 不会执行）")
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

    def _playwright_verify(
        self, project_dir: str, rel_name: str, fp: str,
        require_game: bool = True,
    ) -> tuple[bool, str, str]:
        """用无头 Chromium 真实打开页面验证：
        require_game=True → 模拟拖拽/键盘交互（"能玩"级，canvas 有绘制）；
        require_game=False → 普通页面正常渲染（有内容、无 JS 错误）。
        返回 (是否通过, 详情, 截图路径)；Playwright 缺失时自动安装。"""
        import http.server
        import socketserver
        import urllib.request

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            from env_setup import ensure_playwright
            ok, msg = ensure_playwright(install_browser=True)
            if not ok:
                return False, f"Playwright 不可用（{msg}），降级为静态检查", ""
            from playwright.sync_api import sync_playwright

        screenshot_dir = os.path.join(project_dir, "screenshots")
        os.makedirs(screenshot_dir, exist_ok=True)
        shot = os.path.join(screenshot_dir, rel_name.replace("/", "_").replace(".html", ".png"))
        srv = None
        try:
            class _H(http.server.SimpleHTTPRequestHandler):
                def __init__(self, *a, **k):
                    super().__init__(*a, directory=project_dir, **k)

                def log_message(self, *a):
                    pass

            srv = socketserver.TCPServer(("127.0.0.1", 0), _H)
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{port}/{rel_name}"
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 960, "height": 640})
                js_errors: list[str] = []
                game_over_seen = {"v": False}
                page.on("dialog", lambda d: (game_over_seen.__setitem__("v", True), d.dismiss()))
                page.on("pageerror", lambda e: js_errors.append(str(e)))
                page.on("console", lambda m: js_errors.append(m.text) if m.type == "error" else None)
                page.goto(url, timeout=15000)
                page.wait_for_timeout(800)
                # 编码检查：页面必须是 UTF-8（否则中文界面/得分会显示为乱码）
                try:
                    enc = str(page.evaluate("document.characterSet") or "")
                except Exception:
                    enc = ""
                if enc and "utf-8" not in enc.lower():
                    browser.close()
                    return False, f"页面编码 {enc} 非 UTF-8（中文会显示为乱码）", shot
                if not require_game:
                    # 普通页面：不要求 canvas，只需内容可见、无 JS 错误
                    visible = page.evaluate(
                        """() => {
                            const t = (document.body && document.body.innerText || '').trim();
                            const hasMedia = !!document.querySelector('img,canvas,video,iframe');
                            return { len: t.length, hasMedia, text: t.slice(0, 80) };
                        }"""
                    )
                    try:
                        page.screenshot(path=shot)
                    except Exception:
                        pass
                    if js_errors:
                        browser.close()
                        return False, "JS 错误: " + " | ".join(js_errors[:2]), shot
                    if not visible["len"] and not visible["hasMedia"]:
                        browser.close()
                        return False, "页面内容为空（没有可见文字或媒体元素）", shot
                    browser.close()
                    return True, (
                        f"浏览器加载 OK，页面有内容（{visible['len']} 字符，无 JS 错误）"
                    ), shot
                canvas = page.query_selector("canvas")
                if not canvas:
                    browser.close()
                    return False, "页面无 <canvas>（不是可视化游戏）", shot
                box = canvas.bounding_box()
                if not box or box["width"] < 50 or box["height"] < 50:
                    browser.close()
                    return False, f"canvas 尺寸异常 {box}", shot
                # 模拟拖拽（弹弓类）与键盘方向键（贪吃蛇类）交互
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                page.mouse.move(cx * 0.75, cy * 0.9)
                page.mouse.down()
                page.mouse.move(cx * 0.35, cy * 0.4, steps=8)
                page.mouse.up()
                for _k in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
                    page.keyboard.press(_k)
                    page.wait_for_timeout(120)
                page.wait_for_timeout(800)
                state = page.evaluate(
                    """() => {
                        const s = document.querySelector(
                            '#score, .score, [class*="score"], [id*="score"], .ui-text'
                        );
                        const cv = document.querySelector('canvas');
                        let nonBlank = false;
                        if (cv && cv.width > 0 && cv.height > 0) {
                            try {
                                const ctx = cv.getContext('2d');
                                const img = ctx.getImageData(0, 0, cv.width, cv.height).data;
                                const r0 = img[0], g0 = img[1], b0 = img[2];
                                let varied = 0;
                                for (let i = 0; i < img.length; i += 4) {
                                    if (Math.abs(img[i]-r0) > 12 || Math.abs(img[i+1]-g0) > 12 || Math.abs(img[i+2]-b0) > 12) {
                                        varied++;
                                        if (varied > 300) break;
                                    }
                                }
                                nonBlank = varied > 300;
                            } catch (e) { nonBlank = false; }
                        }
                        return {
                            scoreText: s ? (s.textContent || '') : '',
                            canvasDataLen: cv ? cv.toDataURL().length : 0,
                            nonBlank,
                        };
                    }"""
                )
                try:
                    page.screenshot(path=shot)
                except Exception:
                    pass
                # 撞墙重启验证：持续朝一个方向走直到出界/失败，随后尝试
                # 换方向键、Enter/Space、点击画布，确认游戏循环仍在运行。
                # "第一次撞墙就永久卡死"的伪可玩游戏在这里被拦下。
                for _ in range(45):
                    page.keyboard.press("ArrowUp")
                    page.wait_for_timeout(130)
                restart_ok = False
                if game_over_seen["v"]:
                    # 出现游戏结束弹窗才算"撞墙失败"；无该机制的拖拽型游戏
                    # （弹弓类，结束后画面静止属正常）自动跳过此项断言
                    for _ in range(2):
                        fp_wall = page.evaluate(_FINGERPRINT_JS)
                        page.keyboard.press("ArrowRight")
                        page.wait_for_timeout(700)
                        if page.evaluate(_FINGERPRINT_JS) != fp_wall:
                            restart_ok = True
                            break
                        for extra in ("Enter", "Space"):
                            page.keyboard.press(extra)
                            page.wait_for_timeout(400)
                            if page.evaluate(_FINGERPRINT_JS) != fp_wall:
                                restart_ok = True
                                break
                        if restart_ok:
                            break
                        page.mouse.click(cx, cy)
                        page.wait_for_timeout(400)
                        if page.evaluate(_FINGERPRINT_JS) != fp_wall:
                            restart_ok = True
                            break
                    if not restart_ok:
                        browser.close()
                        return False, "撞墙/失败后游戏未重启（游戏循环卡死，不可玩）", shot
                browser.close()
            if js_errors:
                return False, "JS 错误: " + " | ".join(js_errors[:2]), shot
            if not state.get("nonBlank"):
                return False, "canvas 渲染为空白（游戏没有实际绘制内容）", shot
            score = str(state.get("scoreText") or "")[:30]
            detail = "浏览器加载 + 拖拽/方向键模拟 OK，canvas 有渲染内容（无 JS 错误）"
            if score:
                detail += f"；分数/状态='{score}'"
            return True, detail, shot
        except Exception as exc:
            return False, f"浏览器验证异常: {exc}", ""
        finally:
            if srv is not None:
                try:
                    srv.shutdown()
                    srv.server_close()
                except Exception:
                    pass

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

    @staticmethod
    def _delivery_has_code_files(all_steps: list[dict], completed_all: dict) -> bool:
        """检查最终交付包里是否有可运行的代码文件（HTML/PY/JS）。"""
        import zipfile
        for s in all_steps:
            if s.get("capability") != "package":
                continue
            text = str(completed_all.get(s["step_id"], {}).get("result") or "")
            m = re.search(r"Download: file://([^\s]+)", text)
            if not m or not os.path.exists(m.group(1).strip()):
                continue
            try:
                with zipfile.ZipFile(m.group(1).strip()) as zf:
                    for info in zf.infolist():
                        if info.filename.endswith((".html", ".py", ".js")):
                            return True
            except Exception:
                continue
        return False

    def _generate_search_charts(self, task_id: str, goal: str) -> None:
        """确定性图表生成：仅当目标明确要求可视化时，基于真实检索结果
        （search_results.json）绘制多张直观图表（来源分布、关键数值点、
        主题热词）。不调用 LLM，端点波动也不影响。"""
        import subprocess
        import sys
        if not self._wants_visualization(goal):
            return
        project = task_project_dir(task_id)
        src = project / "search_results.json"
        if not src.exists():
            return
        script = r'''# -*- coding: utf-8 -*-
import json, re
from collections import Counter
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

data = json.load(open("search_results.json", encoding="utf-8"))
items = [d for d in data if isinstance(d, dict)]

# 1) 数据来源分布
domains = Counter()
for d in items:
    m = re.match(r"https?://([^/]+)", str(d.get("url") or ""))
    if m:
        domains[m.group(1).replace("www.", "")] += 1
top = domains.most_common(8)
if top:
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.barh([d for d, _ in top][::-1], [c for _, c in top][::-1],
            color="#3b82f6", edgecolor="white")
    ax.set_title("数据来源分布（检索结果）")
    ax.set_xlabel("结果数")
    for i, (_, c) in enumerate(top):
        ax.text(c + 0.05, i, str(c), va="center", fontsize=9)
    plt.tight_layout()
    plt.savefig("source_distribution.png", dpi=110)
    plt.close()

# 2) 检索摘要中的关键数值点（真实数据）
pat = re.compile(
    r"(\d+(?:\.\d+)?)\s*(万亿|千亿|百亿|亿|万)?\s*(美元|元|人民币|万辆|万台|TOPS|Gbps|%|亿美元)"
)
seen, picked = set(), []
for d in items:
    text = str(d.get("title") or "") + " " + str(d.get("snippet") or "")
    for m in pat.finditer(text):
        key = (m.group(1), m.group(2) or "", m.group(3) or "")
        if key in seen:
            continue
        seen.add(key)
        picked.append((float(m.group(1)), m.group(2) or "", m.group(3) or ""))
        if len(picked) >= 8:
            break
    if len(picked) >= 8:
        break
if picked:
    fig, ax = plt.subplots(figsize=(10, 5))
    labels = [f"{v:g}{u}{unit}" for v, u, unit in picked]
    ax.bar(labels, [v for v, _, _ in picked], color="#10b981", edgecolor="white")
    ax.set_title("检索资料中的关键数值点（真实数据，来源：搜索结果摘要）")
    ax.tick_params(axis="x", rotation=30)
    for i, v in enumerate([v for v, _, _ in picked]):
        ax.text(i, v, f"{v:g}", ha="center", va="bottom", fontsize=9)
    plt.tight_layout()
    plt.savefig("key_numbers.png", dpi=110)
    plt.close()

# 3) 主题热词分布（检索结果高频词，反映讨论焦点）
words = Counter()
stop = {
    "一个", "我们", "以及", "可以", "没有", "已经", "进行", "通过", "对于",
    "不是", "就是", "同时", "如果", "因为", "所以", "但是", "这些", "那些",
    "其中", "以及", "主要", "相关", "关于", "根据", "报告", "分析",
}
for d in items:
    text = (str(d.get("title") or "") + " " + str(d.get("snippet") or "")).lower()
    for m in re.finditer(r"[\u4e00-\u9fff]{2,4}", text):
        w = m.group(0)
        if w in stop:
            continue
        words[w] += 1
top_w = words.most_common(10)
if top_w:
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.barh([w for w, _ in top_w][::-1], [c for _, c in top_w][::-1],
            color="#f59e0b", edgecolor="white")
    ax.set_title("检索资料主题热词（高频词）")
    ax.set_xlabel("出现次数")
    plt.tight_layout()
    plt.savefig("topic_terms.png", dpi=110)
    plt.close()
print("charts generated")
'''
        script_path = project / "make_charts.py"
        try:
            script_path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=str(project), capture_output=True, timeout=120,
            )
            if proc.returncode != 0:
                logger.warning("make_charts failed: %s", proc.stderr.decode("utf-8", errors="replace")[:200])
        except Exception as exc:
            logger.warning("make_charts error: %s", exc)

    # ── Main Loop ──
    def run(self, task_id: str, goal: str, context: str = "",
            auto_run: bool = True, template_steps: list | None = None) -> dict:
        """Execute a full task lifecycle. Returns final status dict."""
        started = time.time()
        with self._task_starts_lock:
            self._task_starts[task_id] = started
        # 每任务独立成果文件夹：清空本项目目录与旧交付包，保证只含本次产物
        ensure_task_workspace(task_id)
        self._cleanup_project_workspace(task_id)
        ws_dir = task_workspace(task_id)
        try:
            for p in ws_dir.glob("*.zip"):
                p.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Old zip cleanup failed for %s: %s", task_id, exc)
        logger.info("Task %s: %s", task_id, goal[:80])

        # 记忆注入：模板路由与 LLM 规划都先查历史经验（观众可见 memory 日志）
        memory_context = self._inject_memory_context(goal, task_id)

        # 1. Plan（模板步骤直接采用，否则 LLM 规划）
        used_template = False
        if template_steps:
            steps = self._normalize_steps(template_steps)
            steps = self._ensure_report_step(steps, task_id)
            used_template = True
        else:
            routed = self._route_template(goal, task_id)
            if routed:
                push_progress(self._messaging, task_id, "log",
                              {"type": "plan", "agent": "orchestrator",
                               "message": "Plan: routed to deterministic template (LLM routing)",
                               "timestamp": self._now_iso()})
                steps = self._normalize_steps(routed)
                steps = self._ensure_report_step(steps, task_id)
                used_template = True
            else:
                steps = self._plan(goal, task_id, context, memory_context)
        # 模板路径：把历史经验作为额外上下文注入首步骤，让框架可复用
        if memory_context and used_template and steps:
            steps[0]["instruction"] = (
                f"历史经验（来自相似任务，可复用框架/数据/结论）：\n"
                f"{memory_context[:1000]}\n\n原始指令：{steps[0]['instruction']}"
            )
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
                "revision": False,
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
            # 确认后立即把状态从 AWAITING_CONFIRM 切到 RUNNING：
            # 否则前端会一直读到 AWAITING_CONFIRM，确认模块反复弹出
            self._messaging.publish("orchestrator:response", {
                "task_id": task_id,
                "status": "RUNNING",
                "steps": steps,
                "goal": goal,
            })
            push_progress(self._messaging, task_id, "plan_update", {"steps": steps})

        # 简单任务判定：只含"生成+报告+打包"的直达型任务启用快速路径，
        # 复杂任务（搜索/数据管道/多轮代码等）保持原逻辑不变。
        simple = self._is_simple_task(steps)
        with self._task_starts_lock:
            self._task_simple[task_id] = simple
        if simple:
            push_progress(self._messaging, task_id, "log",
                          {"type": "info", "agent": "orchestrator",
                           "message": "Simple task: fast path enabled (skip TDD/review/reflection, early LLM failover)",
                           "timestamp": self._now_iso()})

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
            if simple:
                # 简单任务：一轮执行即交付，由贯通测试守门，不做反射式追加迭代
                break
            # 报告/调研类任务：核心管道已产出报告（图表+来源已确定性嵌入）后直接交付，
            # 反射轮只会追加"锦上添花"步骤拖慢任务（演示目标：时间可控）
            _goal_low = str(goal or "").lower()
            _research_hint = any(k in _goal_low for k in ("报告", "调研", "研报"))
            _report_done = any(
                s.get("capability") == "report_generator"
                and completed_all.get(s["step_id"], {}).get("status") == "SUCCESS"
                for s in all_steps
            )
            if _research_hint and _report_done:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": "Reflection: 报告类任务核心交付已完成，跳过反射轮",
                               "timestamp": self._now_iso()})
                break
            verdict = self._reflect(goal, best_report, task_id)
            if not verdict:
                break
            # 评分门控：score ≥ 阈值直接接受；LLM 未给 score 时回退到 accepted 判断
            score_raw = verdict.get("score")
            if score_raw is None:
                score = 5.0 if not verdict.get("accepted") else 10.0
            else:
                try:
                    score = float(score_raw)
                except (TypeError, ValueError):
                    score = 5.0 if not verdict.get("accepted") else 10.0
            if verdict.get("accepted") or score >= self._reflection_accept_score:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": f"Reflection: deliverable accepted (score={score:.1f})",
                               "timestamp": self._now_iso()})
                break
            gaps = verdict.get("gaps") or []
            # 反思预算：每次最多追加 max_reflection_steps 个步骤，防止任务膨胀
            next_steps = self._normalize_steps(
                (verdict.get("next_steps") or [])[: self._max_reflection_steps]
            )
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

        delivery, e2e_results = self._build_delivery_summary(task_id, goal, all_steps, completed_all)
        # 代码交付守门：任务要求生成代码，但最终交付包没有 HTML/PY/JS 文件
        # （例如步骤被降级成文本摘要）→ 视为贯通测试失败，进入修复轮；
        # 修复仍无代码交付物时任务如实标记失败，避免"只剩报告"的假成功。
        has_code_steps = any(
            s.get("capability") == "code_execution" for s in all_steps
        )
        if has_code_steps and not self._delivery_has_code_files(all_steps, completed_all):
            e2e_results = [{
                "name": "(无代码交付物)", "type": "file", "ok": False,
                "detail": "任务要求生成代码，但交付包中没有 HTML/PY/JS 文件",
            }]
        # 任务级失败修复循环：交付物全部未通过可运行性验证时，带失败原因自动重做（最多 2 轮）
        _max_repair = 2
        _repair = 0
        while e2e_results and not any(r.get("ok") for r in e2e_results) and _repair < _max_repair:
            _repair += 1
            failures = [
                f"{r.get('name')}（{r.get('type')}）：{r.get('detail', '')}"
                for r in e2e_results if not r.get("ok")
            ]
            push_progress(self._messaging, task_id, "log",
                          {"type": "iteration", "agent": "orchestrator",
                           "message": f"贯通测试失败，进入修复轮 {_repair}/{_max_repair}",
                           "timestamp": self._now_iso()})
            repair_step = {
                "step_id": f"fix-{_repair}",
                "capability": "code_execution",
                "instruction": (
                    f"任务目标：{goal[:300]}\n\n"
                    "以下交付物未通过可运行性验证，请针对失败原因修复并重新生成完整文件"
                    "（保持单文件、自包含、可直接运行）：\n" + "\n".join(failures)
                ),
                "timeout": 300,
            }
            fix_result = self._dispatch_step_safe(goal, repair_step, task_id, {"replan_used": 0})
            completed_all[repair_step["step_id"]] = fix_result
            all_steps.append(repair_step)
            if fix_result.get("status") == "SUCCESS":
                # 修复成功后才删除旧的未通过文件，并重新打包（交付包只含可用产物）
                _project_dir = str(task_project_dir(task_id))
                for r in e2e_results:
                    if r.get("ok") or not r.get("name"):
                        continue
                    _bad = os.path.abspath(os.path.join(_project_dir, r["name"]))
                    if _bad.startswith(os.path.abspath(_project_dir) + os.sep) and os.path.isfile(_bad):
                        try:
                            os.remove(_bad)
                        except Exception:
                            pass
                pkg_step = {
                    "step_id": f"fix-pkg-{_repair}",
                    "capability": "package",
                    "instruction": "将本次任务产出的所有文件打包为一个 ZIP 交付包，并返回下载链接。",
                    "timeout": 120,
                }
                pkg_result = self._dispatch_step_safe(goal, pkg_step, task_id, {"replan_used": 0})
                completed_all[pkg_step["step_id"]] = pkg_result
                all_steps.append(pkg_step)
            delivery, e2e_results = self._build_delivery_summary(task_id, goal, all_steps, completed_all)
        # 迭代补洞残留清理：同一目标文件可能同时存在旧失败版与带时间戳的新版
        # （如 index.html 空白 + index_<ts>.html 可玩）。当失败文件存在同基础名
        # 且已通过的兄弟文件时，把它从磁盘与交付包中移除，保证交付只含可用产物。
        if self._prune_superseded_files(task_id, all_steps, completed_all, e2e_results):
            delivery, e2e_results = self._build_delivery_summary(task_id, goal, all_steps, completed_all)
        # 收尾清扫：移除 __pycache__ 与临时校验文件，成果文件夹保持干净
        self._sweep_workspace_artifacts(task_id)
        detail = best_report or self._finalize(goal, all_steps, [
            completed_all.get(s["step_id"], {}) for s in all_steps
        ])
        report = delivery + "\n\n---\n\n" + detail
        # 报告内任务工作区绝对路径 → 前端可访问 URL（图表/数据图片链接可显示）
        report = self._rewrite_report_links(report, task_id)
        # 贯通测试守门：修复后仍全部未通过 → 如实标记失败
        if e2e_results and not any(r.get("ok") for r in e2e_results):
            has_failure = True
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": "orchestrator",
                           "message": "贯通测试：修复后仍全部未通过可运行性验证，任务标记为失败",
                           "timestamp": self._now_iso()})

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
            # 进化沉淀：复杂任务（未走模板）成功后提炼为确定性模板，供后续 LLM 路由选择
            if not used_template:
                self._consolidate_template(goal, all_steps)

        # 5. Complete
        ok_count = sum(1 for r in completed_all.values() if r.get("status") == "SUCCESS")
        push_progress(self._messaging, task_id, "task_complete",
                      {"status": overall,
                       "summary": f"{overall}: {ok_count}/{len(all_steps)} steps, {iteration} iterations",
                       "report": report})

        # 快速路径标志仅任务运行期间需要，用完即清，避免字典无限增长
        self._task_simple.pop(task_id, None)
        self._task_sources.pop(task_id, None)
        return {
            "task_id": task_id,
            "status": overall,
            "steps": [{"step_id": s["step_id"], "capability": s["capability"],
                        "instruction": s["instruction"], "iteration": s.get("iteration", 0),
                        "depends_on": s.get("depends_on", []),
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
        # 惰性初始化（兼容直接 __new__ 构造的实例/测试）
        if not hasattr(self, "_task_sources"):
            self._task_sources = {}
            self._task_sources_lock = threading.Lock()
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
            base_instr = self._inject_step_context(step, completed, lock)
            if step.get("capability") in ("report_generator", "content_summary"):
                # 注入前序搜索的数据来源 URL（任务级累计，跨迭代生效），
                # 报告末尾自动生成"数据来源"附录
                with self._task_sources_lock:
                    urls: list[str] = list(self._task_sources.get(task_id, []))
                # 补充本迭代 completed 中的搜索结果（双保险）
                with lock:
                    for k, r in completed.items():
                        if r.get("status") != "SUCCESS":
                            continue
                        try:
                            parsed = json.loads(str(r.get("result") or ""))
                        except Exception:
                            continue
                        if not isinstance(parsed, list):
                            continue
                        for it in parsed:
                            u = str((it or {}).get("url") or "").strip()
                            if u.startswith("http") and u not in urls:
                                urls.append(u)
                if urls:
                    base_instr += (
                        "\n\n[数据来源]\n"
                        + "\n".join(f"- {u}" for u in urls[:12])
                    )
            # 注入全局任务目标，让 Worker 知道自己正在为哪个目标工作（Codex 式上下文感知）
            if goal and "任务目标" not in base_instr[:60]:
                step["instruction"] = f"任务目标：{goal[:300]}\n\n{base_instr}"
            else:
                step["instruction"] = base_instr
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
            if step.get("capability") == "web_search" and result.get("status") == "SUCCESS":
                # 搜索结果 URL 累计到任务级，供后续（含反射轮）报告步骤引用来源
                try:
                    parsed = json.loads(str(result.get("result") or ""))
                except Exception:
                    parsed = None
                if isinstance(parsed, list):
                    with self._task_sources_lock:
                        bucket = self._task_sources.setdefault(task_id, [])
                        for it in parsed:
                            u = str((it or {}).get("url") or "").strip()
                            if u.startswith("http") and u not in bucket:
                                bucket.append(u)
                    # 仅当目标明确要求可视化时才持久化检索结果并生成图表，
                    # 避免"总结要点"类任务交付一堆无意义的图/脚本
                    if self._wants_visualization(goal):
                        try:
                            _json_path = task_project_dir(task_id) / "search_results.json"
                            _json_path.write_text(
                                json.dumps(parsed, ensure_ascii=False, indent=1),
                                encoding="utf-8",
                            )
                            self._generate_search_charts(task_id, goal)
                        except Exception as exc:
                            logger.warning("search_results.json write failed: %s", exc)
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
        """把用户目标注入所有步骤（尤其模板步骤），防止模板指令与目标跑偏。"""
        if not goal:
            return steps
        for s in steps:
            ins = str(s.get("instruction", ""))
            if "用户目标：" not in ins[:80]:
                s["instruction"] = (
                    f"用户目标：{goal[:300]}\n"
                    f"原始指令：{ins}"
                )
        return steps

    @staticmethod
    def _is_simple_task(steps: list[dict]) -> bool:
        """判定是否为"简单任务"（启用快速路径）：
        只含 code_execution / report_generator / package / content_summary，
        且至多一次代码生成；不含搜索、抓取、数据管道、模型训练等复杂编排。"""
        caps = [str(s.get("capability", "")) for s in steps]
        allowed = {"code_execution", "report_generator", "package", "content_summary"}
        return (
            bool(caps)
            and all(c in allowed for c in caps)
            and caps.count("code_execution") <= 1
            and "code_execution" in caps
        )

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
