"""Orchestrator V2 — clean, linear, push-progress-enabled.

Reuses all existing components: llm_client, common, async_worker_base, memory_manager,
critic_agent, ws_helpers. No complex state machine — just: plan → dispatch → collect → report.

This is the active orchestrator (legacy orchestrator.py was removed in the architecture cleanup).
"""

import csv
import hashlib
import json
import logging
import math
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from workspace import (
    ensure_task_workspace,
    task_charts_dir,
    task_data_dir,
    task_project_dir,
    task_reports_dir,
    task_workspace,
    _safe_project,
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

# 行情类目标关键词：命中后 web_search 指令追加财经行情站点限定，
# 并允许搜索代理把"今日 A股 成交量 排行 前十 东方财富"加入查询变体。
_MARKET_SEARCH_KEYWORDS = (
    "成交量排行", "成交额排行", "成交量前十", "成交额前十",
    "涨停", "跌幅榜", "a股今日", "今日a股", "前十股", "排名榜",
    "股票排行", "a股排行", "股票排名", "成交量榜", "成交额榜",
)

_MARKET_SEARCH_SUFFIX = (
    "\n[行情数据源限定] 本任务为 A股 行情排行类目标："
    "仅检索东方财富、同花顺、新浪财经、雪球等财经行情站点；"
    "禁止 YouTube、直播/短视频平台、百度百科及美股平台来源。"
    "查询词模板：今日 A股 成交量 排行 前十 东方财富"
    "（成交额口径用：今日 A股 成交额 排行 前十 东方财富）。"
)

# 排行类结构化来源：东方财富（原）+ 腾讯 A股/美股候选池（韧性降级新增）
# + 新浪分页全市场（统计类/前 N% 任务，P0-2）。
_RANKING_SOURCES = (
    "eastmoney_ranking", "tencent_ranking",
    "tencent_us_ranking", "sina_ranking",
)

# 结构化财务数据源显示名（单实体与 multi_entity 快照/报告块共用）
_STRUCTURED_SOURCE_LABELS = {
    "eastmoney_datacenter": "东方财富数据中心（港股）",
    "eastmoney_ashare": "东方财富数据中心（A股）",
    "sec_edgar": "SEC EDGAR（10-K 年报）",
}

# 统计类目标关键词：命中后在 code_execution 步骤注入全市场占比计算指令
_STATISTICAL_GOAL_KEYWORDS = (
    "占比", "比例", "百分位", "分布", "合计", "汇总",
    "份额", "集中度", "全市场",
)

# V1.2 竞品启示：报告格式强制要求（三级溯源链 / 数据时效 / 免责声明）。
# 注入 content_summary / report_generator 步骤指令；验收器负责硬检查。
_REPORT_FORMAT_REQUIREMENTS = (
    "\n\n[报告格式要求]（强制，报告/总结步骤必须遵守）\n"
    "1. 三级溯源链：报告正文引用外部信息（网页/数据源内容）处，"
    "必须紧跟 [n] 编号上标引用（n 从 1 递增，可并列如 [1][2]）；"
    "文末必须提供 '## 参考来源' 清单，逐条列出全部编号对应的 URL 与标题"
    "（格式：1. [标题](URL)）。没有引用任何外部信息时可不编号，"
    "但一旦引用必须编号并全部登记到清单。\n"
    "2. 数据时效：报告开头（摘要之前）必须包含 '数据时效' 小节，写明"
    "数据截止时间/行情快照时间（如 '行情数据截至 2026-08-30 15:00 收盘'）、"
    "数据源与更新频次（如 '腾讯行情接口，日终刷新'）；"
    "排行类任务必须标注 '盘中/盘后/日终' 状态。\n"
    "3. 免责声明：报告结尾必须包含 '免责声明' 小节："
    "'本报告由织光 WeaveMind AI 自动生成，仅供参考，不构成任何投资建议；"
    "数据来源于公开渠道，可能存在延迟或误差；据此操作风险自担。'"
)

# P0：产物文件注入白名单——仅数据类文本素材（.md/.txt/.csv/.json）读取正文注入；
# HTML/JS/CSS/PY/图片等源码或二进制一律跳过正文，只保留"文件存在 + 路径"提示，
# 防止报告"抄产物"把落盘的 index.html 等源码原文混入报告 prompt。
_ARTIFACT_TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json"}


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

Available: web_search, web_fetch, data_loader, data_analyzer, model_trainer, report_generator, content_summary, code_execution, file_io, package, react_agent

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
12. 每个步骤的 instruction 必须以"验收：..."结尾，写明可验证的完成标准
    （如"验收：生成 main.py 且能运行并输出结果"），禁止无验收点的空泛指令
13. 步骤可带 "mode": "pipeline"|"parallel"|"human_in_loop"：
    - pipeline：该步骤必须串行执行（与其他步骤不并发；用于强顺序/成本控制）
    - parallel（默认）：无依赖的步骤并行执行
    - human_in_loop：执行前需用户确认（用于高风险/不可逆操作，如删除、发布、付费调用）
14. 需要"根据中间结果反复搜索/迭代工具"的任务（多轮调研、需要多次抓取核对）：
    使用 react_agent（运行时 ReAct：决策→调用工具→观察→再决策），而不是堆叠多个搜索步骤
15. 财报/研报/调研/金融/行业分析类任务：禁止 code_execution 去抓取/爬取/解析网页或
    在线财报数据。网页数据获取只能用 web_fetch，内容整理用 content_summary，
    code_execution 只用于本地数据处理/计算（如对已抓取的 CSV 做分析）
16. 只能引用工作区实际存在的文件：data_loader/model_trainer 需要数据集时，
    先确认"工作区文件清单"里有对应文件；没有对应文件时禁止规划读取本地数据集，
    应改用 web_search/web_fetch 获取外部数据

Output ONLY this JSON with no extra text:
{"steps":[{"step_id":"1","capability":"web_search","instruction":"search for house price dataset","depends_on":[],"timeout":60}]}"""

KNOWN_CAPABILITIES = {
    "web_search", "web_fetch", "data_loader", "data_analyzer", "model_trainer",
    "report_generator", "content_summary", "code_execution", "file_io", "package",
    "react_agent",
}

# P1-2：仅外部检索/报告等步骤允许 human_in_loop（执行前需用户确认）；
# package/file_io/data_loader 等低风险内部步骤若被规划器误标，强制改为 pipeline，
# 避免"打包被误标 human_in_loop → 300 秒确认超时"这类问题。
_HUMAN_IN_LOOP_ALLOWED = {
    "web_search", "web_fetch", "report_generator", "react_agent",
}

_TOPIC_STOPWORDS = {
    "一个", "我们", "你们", "他们", "它们", "完成", "输出", "生成", "要求",
    "进行", "需要", "可以", "是否", "如何", "什么", "请", "帮", "并", "与",
    "和", "在", "用", "把", "将", "给", "让", "这", "那", "为", "对", "其",
    "及", "或", "等", "做", "写", "的", "了", "是", "我", "你", "他", "她", "它",
    # 通用词：出现在几乎任何计划指令里，不能作为"对题"证据
    "文件", "游戏", "页面", "程序", "应用", "系统", "内容", "结果", "报告",
    "html", "HTML", "功能", "实现", "进行", "生成", "编写",
    # 占位/空泛词：损坏的短期目标（如"目标"）不得作为沉淀模板的主题证据
    "目标", "任务", "调研", "分析", "搜索", "查询", "工作",
}

ITERATOR_SYSTEM = """你是严格的交付验收评审。你会获得【完整上下文】：用户目标、任务的全部步骤及结果摘要、交付文件、贯通测试结果、当前报告。请基于完整上下文判断交付物是否达标，而不是只看报告文本。
受众：你的评审结论将直接生成下一轮步骤指令，必须具体到可执行，禁止空泛意见。
输出严格JSON：
{"score": 0-10, "verdict": "accept"|"stop"|"retry_step"|"add_steps",
 "retry_step_id": "步骤ID", "retry_reason": "缺陷原因与修复要求",
 "gaps": ["缺口"], "next_steps": [{"step_id":"1","capability":"...","instruction":"...","timeout":120}],
 "memory_ops": [{"action": "remember"|"forget", "summary": "一句话经验", "key": "经验标识/目标"}]}
next_steps 示例（instruction 必须自带 角色/受众/输出要求/验收标准 四要素）：
{"step_id":"3","capability":"code_execution",
 "instruction":"【角色】资深全栈工程师。【受众】最终用户（需可玩）。【输出要求】自包含单文件 HTML，内联 CSS/JS。【质量标准】浏览器直接可玩、键盘可操控、有得分显示。实现：xxx",
 "timeout":180}
规则：
1. score 是交付物与目标的吻合度（0-10）。score>=6 → verdict="accept"
2. 可随时 verdict="stop" 停止反思（当前交付已足够好，或继续修改边际收益很低）
3. 明确不达标时优先 verdict="retry_step"：某一步骤结果有明显缺陷（搜索与主题无关、内容过时/不足、代码运行失败、报告遗漏关键内容），
   指定 retry_step_id 并给出 retry_reason（具体修复要求）；只重试该步骤及其下游，不要整轮任务重来
4. 若缺陷无法归因于某一步骤，用 verdict="add_steps"，next_steps 最多3个，具体可执行，指令中文且严格围绕主题
5. 若判断已有知识过时/不足、需要更新信息，可在 next_steps 或重试步骤中安排一次 web_search（整轮反思最多补1次检索）
6. next_step 的 capability 只能是下列之一，且每步只能一个：
   web_search, web_fetch, data_loader, data_analyzer, model_trainer, report_generator, content_summary, code_execution, file_io, package
7. retry_reason 与 next_steps 的 instruction 必须包含：角色、受众（按目标推断）、输出要求（结构化格式）、质量标准（可验证的验收点）
8. memory_ops（主动记忆）：发现值得长期记住的有效方法/用户偏好时输出 remember（summary 一句话）；
   发现已过时/错误经验时输出 forget（key 填该经验的标识或目标）。无则省略。
9. 不要吹毛求疵、不要"锦上添花"；只有明确缺失用户要求的内容才继续反思
10. 财报/研报/调研类任务禁止追加 code_execution 抓网页/解析 URL 的步骤；
    需要补数据时用 web_fetch/web_search，整理用 content_summary
11. 追加 data_loader/model_trainer 步骤前，确认工作区文件清单里存在对应数据集；
    不存在时不要追加，改用 web_search/web_fetch
只输出JSON。"""

# ─────────────────────────────────────────────
# Orchestrator V2
# ─────────────────────────────────────────────

# C3：system 段热重载字段表（实例属性, 配置键, 类型转换, 缺省值, 取值钳制）
# 与 __init__ 的解析逻辑保持一致；只覆盖本次要求的旋钮（含 task_timeout）。
_SYSTEM_HOT_RELOAD_FIELDS = (
    ("_max_steps", "max_steps", int, 8, lambda v: max(1, v)),
    ("_max_parallel", "max_parallel", int, 3, lambda v: max(1, v)),
    ("_max_iterations", "max_iterations", int, 2, lambda v: max(0, v)),
    ("_critic_enabled", "critic", bool, False, lambda v: bool(v)),
    ("_critic_timeout", "critic_timeout", int, 30, lambda v: max(10, v)),
    ("_max_retry", "max_retry", int, 2, lambda v: max(0, v)),
    ("_replan_depth", "replan_depth", int, 2, lambda v: max(0, v)),
    ("_task_timeout", "task_timeout", int, 300, lambda v: max(1, v)),
    ("_stall_timeout", "stall_timeout", int, 60, lambda v: max(5, v)),
    ("_plan_confirm_timeout", "plan_confirm_timeout", int, 300, lambda v: max(30, v)),
)


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
        # P0-1：反思 LLM 不可用标记（每次任务运行前重置）
        self._reflection_llm_unavailable = ""
        self._max_redo_rounds = 2
        # B3：单轮反思重做步数上限（默认 2，可用 REFLECT_MAX_REDO_STEPS 或
        # config.json system.reflect_max_redo_steps 覆盖）
        self._max_redo_steps = max(
            1, int(os.environ.get("REFLECT_MAX_REDO_STEPS", "2") or 2)
        )
        self._plan_confirm_timeout = 300
        self._stall_timeout = 300
        self._task_timeout = 300
        self._max_offtopic_regenerations = 2
        self._planner_model = None
        try:
            from llm_client import start_health_monitor
            start_health_monitor(interval=float(
                os.environ.get("LLM_HEALTH_INTERVAL", "60") or 60
            ))
        except Exception:
            pass
        # MCP 第三方工具发现（config.json mcp_servers；空配置则跳过）
        try:
            from mcp_client import discover_external_tools
            _ext = discover_external_tools()
            if _ext:
                logger.info("MCP external tools discovered: %s",
                            ", ".join(t["name"] for t in _ext))
        except Exception:
            pass
        self._task_starts: dict[str, float] = {}
        self._task_simple: dict[str, bool] = {}
        self._task_sources: dict[str, list[str]] = {}
        self._task_goals: dict[str, str] = {}
        # P2-5 结构化预载命中时记录 resolver 的 market/name/code 与候选列表，
        # 报告生成时在 [结构化财务数据] 段标注数据源选择依据
        self._task_market_resolution: dict[str, dict] = {}
        self._task_prompt_hints: dict[str, list[str]] = {}
        self._task_user_ids: dict[str, str] = {}
        self._task_sources_lock = threading.Lock()
        self._task_starts_lock = threading.Lock()
        # C3：system 段热重载的 mtime 缓存（机制与 llm_client 一致）
        self._system_cfg_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.json"
        )
        self._system_cfg_mtime: float | None = None
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
            self._max_redo_rounds = max(1, int(_sys.get('max_redo_rounds', 2)))
            self._max_redo_steps = max(
                1, int(_sys.get('reflect_max_redo_steps', self._max_redo_steps))
            )
            # 环境变量优先于配置文件
            self._max_redo_steps = max(
                1, int(os.environ.get("REFLECT_MAX_REDO_STEPS", self._max_redo_steps))
            )
            self._task_timeout = max(1, int(_sys.get('task_timeout', 300)))
            self._plan_confirm_timeout = max(30, int(_sys.get('plan_confirm_timeout', 300)))
            self._stall_timeout = max(5, int(_sys.get('stall_timeout', 60)))
        except Exception:
            pass
        # 首次加载即缓存 mtime，后续仅 mtime 变化时热重载
        self._reload_system_config()
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
                is_planner=True,
            )
            self._planner_model = _planner_cfg.get("model")
            logger.info("Planner LLM: %s", self._planner_model)
        else:
            self._planner_llm = self._plan_llm
        logger.info("OrchestratorV2 initialized")

    def _reload_system_config(self) -> list[str]:
        """system 段配置热重载（mtime 检测，机制与 llm_client 一致）。

        仅在 mtime 变化时读取并逐个赋值；返回本次变化的配置键。
        边界：执行中的任务继续用已读取的属性，热重载对下一任务生效。
        """
        path = getattr(self, "_system_cfg_path", None)
        if not path:
            return []
        try:
            m = os.path.getmtime(path)
        except Exception:
            return []
        old_mtime = getattr(self, "_system_cfg_mtime", None)
        if old_mtime is not None and m == old_mtime:
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            _sys = cfg.get("system") or {}
        except Exception:
            return []
        changed: list[str] = []
        for attr, key, caster, default, clamp in _SYSTEM_HOT_RELOAD_FIELDS:
            try:
                value = clamp(caster(_sys.get(key, default)))
            except Exception:
                continue
            if getattr(self, attr, None) != value:
                setattr(self, attr, value)
                changed.append(key)
        self._system_cfg_mtime = m
        if changed and old_mtime is not None:
            logger.info("system 配置热重载：%s", ", ".join(changed))
        return changed

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

    def _query_prompt_hints(self, goal: str, task_id: str) -> list[str]:
        """检索进化系统 RAG 中与本目标相关的提示词改进经验，
        用于改写/丰富本次任务的规划与步骤提示词。"""
        q = getattr(self._memory, "query_prompt_refinements", None)
        if not callable(q):
            return []
        try:
            hints = q(goal)
            push_progress(self._messaging, task_id, "log",
                          {"type": "memory", "agent": "orchestrator",
                           "message": f"Prompt RAG: 注入提示词改进经验 {len(hints)} 条",
                           "timestamp": self._now_iso()})
            return hints
        except Exception as exc:
            logger.warning("Prompt RAG query failed: %s", str(exc)[:120])
            return []

    def _plan(self, goal: str, task_id: str, context: str = "",
              memory_context: str = "") -> list[dict]:
        """Ask LLM to decompose goal into steps."""
        # C3：计划生成前热重载 system 段配置（低频 mtime 检测，无变化直接返回；
        # 执行中任务不受影响，下一任务生效）
        self._reload_system_config()
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
        # 工作区文件清单：让规划器知道实际有哪些文件，避免规划 data_loader
        # 加载不存在的 CSV（"No fresh CSV found in workspace" 之类失败）
        ws_inv = self._workspace_inventory(task_id)
        if ws_inv:
            prompt = (
                f"{prompt}\n\n工作区现有文件（规划时只能引用这些文件；"
                "若任务所需数据文件不存在，禁止规划 data_loader/model_trainer "
                "读取本地数据集，应改用 web_search/web_fetch 获取外部数据）：\n"
                f"{ws_inv}"
            )
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
        # 工具目录（对标 3.1 FC 工具定义）：让规划器基于能力描述选择工具
        try:
            from tool_contracts import tool_catalog_text
            prompt = f"{prompt}\n\n{tool_catalog_text()}"
        except Exception:
            pass

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
                from prompt_registry import get_prompt
                # B2：同目标重复规划直接命中缓存（LLM_CACHE_TTL 开启时生效），
                # 键含目标哈希，同一会话追问重复提交不会重复花规划 token
                plan_cache_key = (
                    "plan:"
                    + hashlib.sha256(str(goal).encode("utf-8")).hexdigest()
                )
                raw = self._planner_llm.call(
                    get_prompt("planner", PLANNER_SYSTEM, goal=goal),
                    attempt_prompt, expect_json=True, max_tokens=8192,
                    # B1：规划/反思/评审统一走 planner 用途模型
                    usage="plan", cache_key=plan_cache_key,
                )
                plan_data = self._parse_plan_response(raw)
                break
            except Exception as e:
                last_error = e
                logger.warning("Plan attempt %d failed: %s", attempt + 1, str(e)[:200])
                push_progress(self._messaging, task_id, "log",
                              {"type": "error", "agent": "orchestrator",
                               "message": f"规划第 {attempt + 1} 次尝试失败：{str(e)[:80]}，重试中",
                               "timestamp": self._now_iso()})
                if attempt == 0:
                    time.sleep(3)  # 瞬断退避：给双端点恢复留出窗口
        if plan_data is None:
            logger.error("Plan failed: %s", str(last_error)[:300])
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": "orchestrator",
                           "message": f"Plan failed: {last_error}", "timestamp": self._now_iso()})
            # P0-1：双端点均不可用（余额不足/无响应）→ 抛异常让任务终止并弹警告，
            # 不再回退到 content_summary 单步（那个 worker 同样会因 LLM 死掉而失败）
            try:
                from llm_client import endpoints_available, LLMUnavailableError
                _ok, _msg = endpoints_available()
                if not _ok:
                    raise LLMUnavailableError(_msg)
            except LLMUnavailableError:
                raise
            except Exception:
                pass
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
                    raw2 = self._planner_llm.call(
                        get_prompt("planner", PLANNER_SYSTEM, goal=goal),
                        strict_prompt, expect_json=True, max_tokens=8192,
                        usage="plan",
                    )
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
        react_markers = ("多轮", "反复搜索", "迭代核对", "多次搜索", "需要反复", "react")
        if any(m in g for m in react_markers):
            # 多轮/反复搜索类任务 → 确定性路由到 react_agent（运行时 ReAct）
            return [
                {
                    "step_id": "1",
                    "capability": "react_agent",
                    "instruction": (
                        "多轮 ReAct 调研：根据中间结果反复调用工具（搜索/抓取/总结）"
                        "核对不同来源，最终输出对比总结。目标："
                        f"{goal[:500]}"
                    ),
                    "timeout": 600,
                    "depends_on": [],
                },
                {
                    "step_id": "2",
                    "capability": "report_generator",
                    "instruction": (
                        f"汇总 ReAct 调研结果，生成最终交付报告（Markdown）。{goal[:300]}"
                    ),
                    "timeout": 180,
                    "depends_on": ["1"],
                },
            ]
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

    def _load_templates(self, path: str | None = None) -> list[dict]:
        """读取确定性模板库（手工模板在前，auto-* 沉淀模板在后）。"""
        try:
            path = path or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "templates.json",
            )
            with open(path, "r", encoding="utf-8") as f:
                tpls = (json.load(f) or {}).get("templates", [])
            # P2-6 模板优先级：手工模板（不以 auto- 开头）在前，auto-* 排后
            return sorted(
                tpls,
                key=lambda t: str(t.get("name") or "").startswith("auto-"),
            )
        except Exception:
            return []

    def _route_template(self, goal: str, task_id: str = "") -> list[dict] | None:
        """由 LLM 判断目标是否适合确定性模板（含直接交付模板）；
        失败时回退到关键词判断。返回模板 steps 或 None（走完整规划）。"""
        # P2-6 手工模板优先于 auto-*：再排一次，防御 _load_templates 被覆盖/顺序变化
        templates = sorted(
            self._load_templates(),
            key=lambda t: str(t.get("name") or "").startswith("auto-"),
        )
        # 关键词快速路由：明确命中的任务直接走确定性模板，省掉 LLM 路由调用
        direct = self._direct_deliverable_plan(goal)
        if direct:
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": "模板路由：直接交付（关键词命中，跳过 LLM；模板来源：manual）",
                           "timestamp": self._now_iso()})
            return direct
        tpl = self._template_keyword_match(goal, templates)
        if tpl:
            src = "auto" if str(tpl.get("name") or "").startswith("auto-") else "manual"
            push_progress(self._messaging, task_id, "log",
                          {"type": "plan", "agent": "orchestrator",
                           "message": f"模板命中（关键词）：{tpl.get('name')}（模板来源：{src}）",
                           "timestamp": self._now_iso()})
            return tpl.get("steps") or None
        if not templates:
            return self._direct_deliverable_plan(goal)
        push_progress(self._messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator",
                       "message": f"模板路由（LLM）：{len(templates)} 个模板待匹配（手工模板优先）",
                       "timestamp": self._now_iso()})
        # P2-6 auto-* 模板描述增强：goal 中的"目标公司/集团"等占位符
        # 用具体公司名替换后再展示，便于 LLM 判断是否匹配
        company = ""
        try:
            from task_classifier import _extract_company
            company = _extract_company(goal)
        except Exception:
            pass

        def _display_goal(t: dict) -> str:
            """展示模板描述；auto-* 模板含公司占位符时替换为具体公司名。"""
            desc = str(t.get("goal") or "")[:120]
            if company and str(t.get("name") or "").startswith("auto-"):
                for pat in ("目标公司/集团", "目标公司官网", "目标公司", "公司/集团"):
                    desc = desc.replace(pat, company)
            return desc

        try:
            tpl_list = "\n".join(
                f"- {t.get('name')}: {_display_goal(t)}" for t in templates
            )
            prompt = (
                "判断以下用户目标是否适合使用现成的确定性执行模板。\n"
                f"可用模板：\n{tpl_list or '（无）'}\n\n"
                "规则：\n"
                '1. 仅当目标与某个模板的【核心任务】基本一致时才选择该模板'
                '（例如目标是"房价预测/数据科学流水线"才能选"数据分析流水线"；'
                '目标是"用户评论情感分析"而模板是房价预测 → 不匹配）→ {"template": "模板名"}\n'
                '1a. 特别注意"公司调研与财报分析"模板：只有目标包含公司/集团的'
                '发展历程、业务现状或公司调研要求时才可选它；'
                '目标仅涉及财报/财务指标分析（营收、净利润、毛利率、现金流、'
                '财务健康度等，不含发展历程/现状调研）→ 不选该模板 → {"template": null}\n'
                '2. 若目标适合"直接交付"（单产物如游戏/脚本/工具/单文件页面，'
                '无需外部调研或多技能协作）→ {"template": "direct_deliverable"}\n'
                '2a. auto- 前缀模板仅在无手工模板匹配时选用；'
                '手工模板（名称不以 auto- 开头）优先\n'
                '3. 否则（需要规划拆解/多技能协作/外部资料）→ {"template": null}\n'
                f"目标：{goal[:400]}\n只输出JSON。"
            )
            raw = self._plan_llm.call(
                "你是任务路由专家，判断任务类型并选择模板，只输出JSON。",
                prompt,
                expect_json=True,
                usage="plan",
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
                tpl = next((t for t in templates if t.get("name") == name), None)
                if tpl:
                    is_auto = str(name).startswith("auto-")
                    has_manual = any(
                        not str(t.get("name") or "").startswith("auto-")
                        for t in templates
                    )
                    if is_auto and has_manual:
                        # 手工模板优先：库中存在手工模板时 auto-* 不可选，
                        # 直接交给完整规划，避免 auto-financial 抢占手工模板
                        push_progress(
                            self._messaging, task_id, "log",
                            {"type": "plan", "agent": "orchestrator",
                             "message": (
                                 f"模板命中被拒（LLM 选了 auto-* {name}，"
                                 "但库中存在手工模板，auto-* 仅在无手工模板时选用）"
                             ),
                             "timestamp": self._now_iso()},
                        )
                    else:
                        src = "auto" if is_auto else "manual"
                        push_progress(
                            self._messaging, task_id, "log",
                            {"type": "plan", "agent": "orchestrator",
                             "message": f"模板命中（LLM）：{name}（模板来源：{src}）",
                             "timestamp": self._now_iso()},
                        )
                        return tpl.get("steps") or None
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
            "公司调研与财报分析": (
                ("集团", "发展", "财报"), ("公司", "发展", "财报"),
                ("集团", "现状", "财报"), ("公司", "现状", "财报"),
                ("集团", "发展", "年报"), ("公司", "发展", "年报"),
                ("集团", "财报", "分析"), ("公司", "财报", "分析"),
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

    _CONSOLIDATE_THRESHOLD = 3  # 同 domain×能力链 且验收通过 ≥ N 次才固化

    def _consolidate_threshold(self) -> int:
        try:
            return max(1, int(os.environ.get("WEAVEMIND_CONSOLIDATE_THRESHOLD", "3") or 3))
        except (TypeError, ValueError):
            return 3

    def _consolidate_template(
        self, goal: str, all_steps: list[dict], tpl_path: str | None = None,
        task_id: str = "",
    ) -> None:
        """成功的复杂任务沉淀为确定性模板（探索→固化）：
        按 domain×能力链 分组，要求本次验收 pass 且历史同链 pass ≥ 阈值；
        模板使用类型级 goal（不含具体公司名），避免模板库爆炸。"""
        try:
            if not all_steps:
                return
            steps: list[dict] = []
            # 目标主题词：用于单步指令清洗后的主题一致性校验
            tokens = self._topic_tokens(goal)
            if not tokens:
                # 目标无法提取主题词（可能中文损坏/过短）：保守不沉淀，避免污染模板库
                logger.info("Template not consolidated (no topic tokens): %s", goal[:40])
                return
            for s in all_steps[:8]:
                cap = s.get("capability")
                if cap in ("report_generator", "package"):
                    continue  # 收尾打包类步骤不进模板，保留核心能力链（含内容摘要）
                ins = str(s.get("instruction") or "")
                # 剥离追加的反思反馈与自动沉淀的历史教训块
                # （"【历史教训（自动沉淀）】反思要求重做：…"、"【反思要求重做】…"），
                # 保留原可执行指令；经反思重做的任务同样可固化
                for marker in ("【历史教训", "【反思要求重做】", "反思要求重做"):
                    if marker in ins:
                        ins = ins.split(marker, 1)[0].strip()
                        break
                # 纯记忆注入（指令整体是历史经验上下文）才跳过
                if ins.startswith("历史经验"):
                    continue
                if "用户目标：" in ins:
                    ins = ins.split("用户目标：", 1)[-1]
                if "任务目标：" in ins:
                    ins = ins.split("任务目标：", 1)[-1]
                if "原始指令：" in ins:
                    ins = ins.split("原始指令：", 1)[-1]
                ins = ins.strip()
                if not ins:
                    continue
                # 单步主体一致性：步骤指令混入其他公司且不含目标公司
                # （如跨公司教训串味后的"搜索美团；…"）→ 记忆残渣，丢弃
                try:
                    from acceptance_checker import _OTHER_ENTITIES
                    from task_classifier import _extract_company
                    _tgt_comp = _extract_company(goal)
                    if _tgt_comp and _tgt_comp not in ins and any(
                        e != _tgt_comp and e in ins for e in _OTHER_ENTITIES
                    ):
                        continue
                except Exception:
                    pass
                # 截断时尽量在句子边界，避免沉淀出半句指令
                if len(ins) > 200:
                    head = ins[:200]
                    cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"))
                    ins = head if cut <= 120 else head[:cut + 1]
                steps.append({
                    "step_id": str(len(steps) + 1),
                    "capability": cap,
                    "instruction": ins,
                    "timeout": 180,
                })
            if len(steps) < 2:
                return
            # 主题一致性校验：步骤指令必须包含目标主题词，否则可能是跑偏任务（不沉淀）
            hay = " ".join(str(s.get("instruction", "")) for s in steps)
            if not any(t in hay for t in tokens):
                logger.info("Template not consolidated (off-topic execution): %s", goal[:40])
                return
            # ── 探索→固化：domain×能力链 验收驱动 ──
            domain, chain = self._consolidation_key(goal, all_steps)
            # 本次任务验收：存在验收报告时必须 pass
            if task_id:
                acc = self._acceptance_passed(task_id)
                if acc is False:
                    logger.info("Template not consolidated (acceptance fail): %s", goal[:40])
                    return
            # 历史同链验收 pass 计数（不含本次）
            verified = self._count_verified_chain(domain, chain, task_id)
            if verified + 1 < self._consolidate_threshold():
                logger.info(
                    "Template not consolidated yet (%s/%d verified): %s",
                    verified + 1, self._consolidate_threshold(), goal[:40],
                )
                return
            type_goal = {
                "financial": "公司/集团的发展历程与现状，并分析历年财报",
                "code": "生成可运行的代码/程序并验证交付",
                "general": "搜索并总结目标主题的现状，输出结构化报告",
            }.get(domain, "通用任务调研与报告")
            name = f"auto-{domain}-{'-'.join(chain[:3]) or 'pipeline'}"
            path = tpl_path or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "templates.json",
            )
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tpls = data.get("templates", [])
            tpls = [t for t in tpls if t.get("name") != name]
            # 模板必须公司无关：把具体公司名替换为"目标公司"，避免固化出
            # "搜索腾讯控股…"这类只对单一公司有效的指令（goal 已是类型级）
            try:
                from task_classifier import _extract_company
                company = _extract_company(goal)
                if company:
                    for s in steps:
                        ins = str(s.get("instruction") or "")
                        # 英文公司名括号（如"（Tencent Holdings Ltd）"）也剥离，
                        # 避免模板残留单一公司英文名
                        try:
                            import re as _re
                            ins = _re.sub(
                                r"[（(][A-Za-z][A-Za-z0-9 .&'-]*(?:Ltd|Inc|Corp|Holdings|Group|Limited|Co)[^）)]*[）)]",
                                "", ins,
                            )
                        except Exception:
                            pass
                        for pat, rep in (
                            (company + "控股", "目标公司/集团"),
                            (company + "集团", "目标公司/集团"),
                            (company + "官网", "目标公司官网"),
                            (company, "目标公司"),
                        ):
                            ins = ins.replace(pat, rep)
                        s["instruction"] = ins
            except Exception:
                pass
            tpls.append({"name": name, "goal": type_goal, "steps": steps})
            data["templates"] = tpls[-30:]
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Template consolidated: %s (%d steps, domain=%s)", name, len(steps), domain)
        except Exception as exc:
            logger.warning("Template consolidation failed: %s", exc)

    @staticmethod
    def _consolidation_key(goal: str, all_steps: list) -> tuple[str, tuple]:
        """固化分组的 key：domain × 归一化能力序列（去掉收尾步骤与重复）。"""
        try:
            from task_classifier import classify_task
            domain = classify_task(goal).get("domain", "general")
        except Exception:
            domain = "general"
        chain: list[str] = []
        for s in all_steps or []:
            cap = str(s.get("capability") or "")
            if cap in ("report_generator", "package", "file_io"):
                continue
            if not chain or chain[-1] != cap:
                chain.append(cap)
        return domain, tuple(chain[:5])

    @staticmethod
    def _acceptance_passed(task_id: str) -> bool | None:
        """任务验收是否通过（无验收报告返回 None）。"""
        try:
            from workspace import task_workspace
            acc_path = task_workspace(task_id) / "acceptance_report.json"
            if not acc_path.exists():
                return None
            acc = json.loads(acc_path.read_text(encoding="utf-8"))
            return acc.get("overall") == "pass"
        except Exception:
            return None

    def _notify_done_async(
        self, task_id: str, goal: str, status: str, report: str = "",
    ) -> None:
        """F5：任务终态外部通知（后台线程，不阻塞完成流程）。
        链接复用已生成的分享路径（share_links.json）；任务未分享则不附链接，只附摘要。"""
        def _notify_done() -> None:
            try:
                from notifications import (
                    find_share_link,
                    make_summary,
                    notify_task_done,
                )
                notify_task_done(
                    task_id,
                    goal=goal,
                    status=status,
                    report_link=find_share_link(task_id),
                    summary=make_summary(report),
                )
            except Exception as exc:
                logger.warning("notify task done failed: %s", str(exc)[:150])

        threading.Thread(target=_notify_done, daemon=True).start()

    @staticmethod
    def _read_acceptance_summary(task_id: str) -> dict | None:
        """读取验收摘要：{overall, gaps}（无验收报告返回 None）。"""
        try:
            from workspace import task_workspace
            acc_path = task_workspace(task_id) / "acceptance_report.json"
            if not acc_path.exists():
                return None
            acc = json.loads(acc_path.read_text(encoding="utf-8"))
            return {"overall": acc.get("overall"), "gaps": acc.get("gaps") or []}
        except Exception:
            return None

    @staticmethod
    def _read_llm_degraded(task_id: str) -> dict:
        """读取任务级 LLM 降级汇总（Redis 写入，跨 worker/编排器进程）。"""
        try:
            from llm_client import get_task_llm_degradation
            return get_task_llm_degradation(task_id)
        except Exception:
            return {}

    @staticmethod
    def _resolve_final_status(
        has_failure: bool,
        acceptance_summary: dict | None,
        reflection_unavailable: str,
        llm_degraded: dict,
    ) -> str:
        """任务最终状态（A1：以最终验收报告判定，而非反思是否执行）：
        - 有步骤失败 → FAILED；
        - 最终验收 fail（无论反思是否执行/重做）→ SUCCESS_WITH_ISSUES；
        - LLM 双端点均失败 → SUCCESS_WITH_ISSUES；
        - 其余成功 → SUCCESS。
        reflection_unavailable 保留入参仅用于调用方日志追溯，不再参与判定：
        只要最终 acceptance_report.json 仍为 fail，就如实降级，避免
        "反思执行过但重做后仍失败"被误报为 SUCCESS。"""
        if has_failure:
            return "FAILED"
        # A1：读最终验收报告——overall != pass 即视为验收未通过，
        # 不区分"反思未执行/反思执行后仍失败"两条路径
        accept_fail = bool(
            acceptance_summary and acceptance_summary.get("overall") != "pass"
        )
        both_failed = bool(llm_degraded and llm_degraded.get("both_failed"))
        if accept_fail or both_failed:
            return "SUCCESS_WITH_ISSUES"
        return "SUCCESS"

    def _precheck_llm_balance(self, task_id: str) -> tuple[bool, str]:
        """A3：LLM 端点余额预检（任务开始前调用）。
        - 主/备均 insufficient_balance → 拒绝任务（FAILED，reason=余额不足），前端可见；
        - 单个不足 → 照常走降级逻辑，并在 llm_degraded 预置余额警告。
        返回 (是否继续, 拒绝原因/空串)。"""
        try:
            from llm_client import get_balance_status, _record_task_degradation
            balance = get_balance_status()
            reasons = [str(v.get("reason") or "") for v in balance.values()]
            if reasons and all(r == "insufficient_balance" for r in reasons):
                msg = "LLM 端点余额不足，请充值后重试"
                push_progress(self._messaging, task_id, "warning",
                              {"type": "llm", "agent": "orchestrator",
                               "message": msg, "timestamp": self._now_iso()})
                push_progress(self._messaging, task_id, "task_complete",
                              {"status": "FAILED", "summary": msg})
                logger.error("Task %s rejected: %s", task_id, msg)
                return False, msg
            if "insufficient_balance" in reasons:
                # 单端点不足：写入任务级降级（完成阶段 llm_degraded 带出余额警告），
                # 健康路由切到可用端点，任务照常运行
                _record_task_degradation(
                    task_id, "insufficient_balance", both_failed=False
                )
                push_progress(self._messaging, task_id, "warning",
                              {"type": "llm", "agent": "orchestrator",
                               "message": "LLM 单端点余额不足，已自动降级；"
                                          "请充值后重试以恢复完整质量",
                               "timestamp": self._now_iso()})
        except Exception:
            pass
        return True, ""

    def _record_consolidation_stat(self, task_id: str, goal: str, all_steps: list) -> None:
        """任务完成时记录 domain×能力链 与验收结果，供探索-固化阈值判定。"""
        try:
            path = os.environ.get("WEAVEMIND_CONSOLIDATION_STATS") or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "consolidation_stats.json",
            )
            stats = []
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        stats = json.loads(f.read())
                except Exception:
                    stats = []
            domain, chain = self._consolidation_key(goal, all_steps)
            stats = [s for s in stats if s.get("task_id") != task_id]
            stats.append({
                "task_id": str(task_id),
                "domain": domain,
                "chain": list(chain),
                "acceptance": self._acceptance_passed(task_id),
                "ts": self._now_iso(),
            })
            with open(path, "w", encoding="utf-8") as f:
                json.dump(stats[-300:], f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _count_verified_chain(self, domain: str, chain: tuple, current_task: str = "") -> int:
        """统计历史上同 domain×能力链 且验收 pass 的任务数（不含当前任务）。"""
        try:
            path = os.environ.get("WEAVEMIND_CONSOLIDATION_STATS") or os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "consolidation_stats.json",
            )
            if not os.path.exists(path):
                return 0
            with open(path, encoding="utf-8") as f:
                stats = json.loads(f.read())
            count = 0
            for s in stats:
                if str(s.get("task_id")) == str(current_task):
                    continue
                if s.get("domain") != domain:
                    continue
                if tuple(s.get("chain") or []) != chain:
                    continue
                if s.get("acceptance") is True:
                    count += 1
            return count
        except Exception:
            return 0

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

    def _workspace_inventory(self, task_id: str) -> str:
        """列出任务工作区现有文件（≤30 个，含大小），供规划器决策。"""
        try:
            from workspace import task_workspace
            ws = task_workspace(task_id)
            if not ws.exists():
                return ""
            files: list[str] = []
            for p in sorted(ws.rglob("*")):
                if not p.is_file():
                    continue
                if "screenshots" in p.parts:
                    continue
                if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".zip"):
                    continue
                try:
                    files.append(f"- {p.relative_to(ws)} ({p.stat().st_size} B)")
                except Exception:
                    continue
                if len(files) >= 30:
                    break
            return "\n".join(files) if files else "（工作区暂无文件）"
        except Exception:
            return ""

    @staticmethod
    def _wants_financial_data(goal: str) -> bool:
        """目标是否要求财务/财报类数据（触发快照页抓取与回灌清洗）。"""
        g = str(goal or "").lower()
        return any(k in g for k in (
            "财报", "年报", "季报", "营收", "净利润", "负债", "财务", "业绩",
            "financial", "revenue", "earnings", "annual report",
        ))

    @staticmethod
    def _pick_fetch_url(items: list, goal: str = "") -> str | None:
        """从搜索结果中挑选财务相关度最高的 URL（搜索根因缩小版）。

        评分维度：
        1) 标题-目标相关性：标题真正关于目标公司财报/研报/IR 才高分；
           仅"提到"目标（合作新闻）或标题是其他公司（Line/Lululemon）→ 强降权；
        2) 来源分级：官方 IR/交易所披露 > 权威财经 > 内容社区/杂页；
        3) 财务关键词：标题命中 > URL 命中。"""
        finance_kw = (
            "财报", "年报", "季报", "营收", "净利润", "业绩", "财务", "公告",
            "研报", "复盘", "深度", "投资者关系",
            "financial", "earnings", "annual", "revenue", "results",
            "quarterly", "investor",
        )
        official_domains = (
            "ir.", "investor.", "hkex", "eastmoney", "10jqka",
            "cninfo", "sse.com.cn", "szse.cn",
        )
        good_domains = (
            "sina", "163.com", "21jingji", "yicai", "cls.cn",
            "finance", "stock", "xueqiu", "snowball", "pedaily",
        )
        junk_domains = ("blog.csdn", "zhihu.com", "zhengxianling",
                        "cp.baidu", "toutiao", "csdn", "alishui", "sgpjbg")
        target = ""
        try:
            from task_classifier import _extract_company
            target = _extract_company(goal)
        except Exception:
            pass
        other_entities: tuple = ()
        try:
            from acceptance_checker import _OTHER_ENTITIES
            other_entities = tuple(_OTHER_ENTITIES)
        except Exception:
            pass
        best: str | None = None
        best_score = -10**9
        best_is_official = False
        for it in items or []:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or "")
            url = str(it.get("url") or it.get("href") or "")
            if not url.startswith("http"):
                continue
            low_t = title.lower()
            low_u = url.lower()
            score = 0
            is_official = any(o in low_u for o in official_domains)
            # 财务关键词：标题命中 +2，URL 命中 +1
            for k in finance_kw:
                if k in low_t:
                    score += 2
                elif k in low_u:
                    score += 1
            # 来源分级：官方 > 权威财经 > 杂页
            if is_official:
                score += 4
            elif any(g in low_u for g in good_domains):
                score += 2
            for j in junk_domains:
                if j in low_u:
                    score -= 2
            # 标题-目标相关性
            if target:
                target_in_title = target in title
                target_in_url = target in url
                has_fin_topic = any(k in title for k in finance_kw)
                others_in_title = [
                    e for e in other_entities
                    if e and e != target and e in title
                ]
                if target_in_title:
                    score += 3
                    if has_fin_topic:
                        score += 2
                    # 标题同时出现他司且无财务主题
                    # （"官宣与腾讯合作，Line 用户流失"类）→ 合作/八卦新闻降权
                    if others_in_title and not has_fin_topic:
                        score -= 6
                elif target_in_url:
                    score += 1
                    if has_fin_topic and not others_in_title:
                        score += 1
                elif re.search(r"[\u4e00-\u9fff]", title):
                    # 中文标题不含目标：他司财报（Lululemon）强降权，
                    # 无他司的泛内容降权；英文标题不判定（避免误伤
                    # Apple 等英文官方 IR 页）
                    if others_in_title:
                        score -= 8
                    else:
                        score -= 4
            # 平局时官方源优先（如标题关键词得分相同，港交所/IR 页胜出）
            if score > best_score or (
                score == best_score and is_official and not best_is_official
            ):
                best_score = score
                best = url
                best_is_official = is_official
        return best

    def _recycle_fetch_into_clean(self, task_id: str, goal: str, result: dict) -> None:
        """快照页回灌清洗闭环：把 web_fetch 抓到的完整正文并入清洗输入，
        重新生成 clean_chart_data.json 并重跑探索图，让财务数字进入图表与摘要。"""
        if not self._wants_financial_data(goal):
            return
        try:
            parsed = json.loads(str(result.get("result") or ""))
        except Exception:
            parsed = None
        if not isinstance(parsed, dict):
            return
        text = str(parsed.get("text") or "").strip()
        if len(text) < 300:
            logger.info("Snapshot fetch too short, skip recycle (%d chars)", len(text))
            return
        try:
            proj = task_project_dir(task_id)
            snap = proj / "fetch_snapshot.json"
            snaps: list[dict] = []
            if snap.exists():
                try:
                    snaps = json.loads(snap.read_text(encoding="utf-8"))
                except Exception:
                    snaps = []
            entry = {
                "title": str(parsed.get("title") or ""),
                "url": str(parsed.get("url") or ""),
                "text": text,
            }
            if not any(s.get("url") == entry["url"] for s in snaps):
                snaps.append(entry)
            snap.write_text(
                json.dumps(snaps, ensure_ascii=False, indent=1), encoding="utf-8",
            )
            src = proj / "search_results.json"
            items = json.loads(src.read_text(encoding="utf-8")) if src.exists() else []
            docs = list(items) + [
                {
                    "title": s.get("title") or "",
                    "url": s.get("url") or "",
                    "snippet": s.get("text") or "",
                }
                for s in snaps
            ]
            from clean_data import clean_and_structure
            clean = clean_and_structure(docs, goal=goal)
            (proj / "clean_chart_data.json").write_text(
                json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8",
            )
            # 结构化源（东方财富）数据重新并入，避免被搜索清洗覆盖
            self._remerge_structured_financials(task_id)
            # P1-3：crypto/macro 结构化行情行同样重新并入（供数据驱动图表兜底）
            self._remerge_structured_points(task_id)
            logger.info(
                "Snapshot recycle: %d docs, market_data=%d, shares=%d, trends=%d",
                len(docs),
                len(clean.get("market_data") or []),
                len(clean.get("market_share") or []),
                len(clean.get("market_trends") or []),
            )
            self._generate_search_charts(task_id, goal)
            self._render_clean_chart_data(task_id, goal)
        except Exception as exc:
            logger.warning("snapshot recycle failed: %s", str(exc)[:150])

    def _structured_data_preload(
        self, task_id: str, goal: str, project: str | None = None,
    ) -> dict | None:
        """结构化数据源预载：按目标路由到 financial（东方财富/SEC）或
        新增的 crypto/macro/news 适配器；命中后写入工作区并合并到
        clean_chart_data.json，供搜索清洗、图表与报告引用。失败静默回退搜索链路。"""
        try:
            from adapters.router import route_structured
            data = route_structured(goal)
            if not data:
                # P2-6 预载失败可见化：首次未命中（如瞬时接口异常）等待 2s
                # 重试一次，第二次仍返回 None 才放弃并告警，避免静默占位
                logger.warning(
                    "Structured preload miss for %s, retry once after 2s",
                    task_id,
                )
                time.sleep(2)
                data = route_structured(goal)
                if not data:
                    logger.warning(
                        "Structured preload retry failed for %s: "
                        "route_structured returned None", task_id,
                    )
                    return None
            source = str(data.get("source") or "")
            metadata = data.get("metadata") or {}
            is_financial = source in (
                "eastmoney_datacenter", "eastmoney_ashare", "sec_edgar",
            ) or (not source and isinstance(data.get("financials"), list))
            is_financial = is_financial or source == "multi_entity"
            proj = task_project_dir(task_id, project)
            if not hasattr(self, "_task_structured_data"):
                self._task_structured_data = {}
            self._task_structured_data[task_id] = data
            # 财务数据：沿用既有 financials.json 通道（报告注入/失败回退/验收溯源）
            if is_financial:
                # P2-5 把 resolver 返回的 market/name/code 与候选列表写入任务上下文，
                # 报告生成时在 [结构化财务数据] 段标注数据源选择依据
                prefs = getattr(self, "_task_market_resolution", None)
                if prefs is None:
                    prefs = {}
                    self._task_market_resolution = prefs
                try:
                    from adapters.resolver import market_preference
                    pref = market_preference()
                except Exception:
                    pref = "hk"
                if source == "multi_entity":
                    prefs[task_id] = {
                        "entities": [
                            {
                                "name": str(
                                    (ent.get("resolution") or {}).get("name")
                                    or ent.get("name") or ""
                                ),
                                "market": str(ent.get("market") or ""),
                                "code": str(
                                    ent.get("code")
                                    or ent.get("stock_code") or ""
                                ),
                                "preference": pref,
                                "alternatives": (
                                    (ent.get("resolution") or {})
                                    .get("resolved_alternatives") or []
                                ),
                            }
                            for ent in data.get("companies") or []
                        ],
                    }
                else:
                    resolution = data.get("resolution") or {}
                    if resolution:
                        prefs[task_id] = {
                            "name": str(resolution.get("name") or ""),
                            "market": str(resolution.get("market") or ""),
                            "code": str(resolution.get("stock_code") or ""),
                            "preference": pref,
                            "alternatives": resolution.get(
                                "resolved_alternatives"
                            ) or [],
                        }
                (proj / "financials.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8",
                )
                snap = proj / "fetch_snapshot.json"
                snaps: list[dict] = []
                if snap.exists():
                    try:
                        snaps = json.loads(snap.read_text(encoding="utf-8"))
                    except Exception:
                        snaps = []
                entries: list[dict] = []
                if source == "multi_entity":
                    for ent in data.get("companies") or []:
                        ent_source = str(
                            (ent.get("metadata") or {}).get("source") or ""
                        )
                        source_label = _STRUCTURED_SOURCE_LABELS.get(
                            ent_source, ent_source or "结构化数据源",
                        )
                        entries.append({
                            "title": (
                                f"{ent.get('name')} 主要财务指标"
                                f"（{source_label}）"
                            ),
                            "url": (ent.get("raw") or {}).get("url", ""),
                            "text": (ent.get("raw") or {}).get("text", ""),
                        })
                else:
                    entries.append({
                        "title": (
                            f"{metadata.get('company')} 主要财务指标"
                            "（东方财富数据中心）"
                        ),
                        "url": (data.get("raw") or {}).get("url", ""),
                        "text": (data.get("raw") or {}).get("text", ""),
                    })
                snapshot_changed = False
                for entry in entries:
                    if entry["url"] and not any(
                        s.get("url") == entry["url"] for s in snaps
                    ):
                        snaps.append(entry)
                        snapshot_changed = True
                if snapshot_changed:
                    snap.write_text(
                        json.dumps(snaps, ensure_ascii=False, indent=1),
                        encoding="utf-8",
                    )
                clean_path = proj / "clean_chart_data.json"
                clean = {}
                if clean_path.exists():
                    try:
                        clean = json.loads(clean_path.read_text(encoding="utf-8"))
                    except Exception:
                        clean = {}
                if source == "multi_entity":
                    for ent in data.get("companies") or []:
                        clean = self._merge_structured_financials(
                            clean, ent.get("financials") or [],
                            (ent.get("raw") or {}).get("url", ""),
                            entity=ent.get("name"),
                        )
                else:
                    clean = self._merge_structured_financials(
                        clean, data.get("financials") or [],
                        (data.get("raw") or {}).get("url", ""),
                    )
                clean_path.write_text(
                    json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8",
                )
                if source == "multi_entity":
                    hit_msg = (
                        f"结构化数据源命中：{metadata.get('entities')} 个实体"
                        "（多实体对比）"
                    )
                    log_msg = (
                        "Structured multi-entity financials loaded for %s: "
                        "%s entities"
                    )
                    log_args = (task_id, metadata.get("entities"))
                else:
                    hit_msg = (
                        f"结构化数据源命中：{metadata.get('company')} "
                        f"{metadata.get('annual_count')} 份年报"
                    )
                    log_msg = "Structured financials loaded for %s: %s annuals"
                    log_args = (task_id, metadata.get("annual_count"))
                push_progress(self._messaging, task_id, "log",
                              {"type": "data", "agent": "orchestrator",
                               "message": hit_msg,
                               "timestamp": self._now_iso()})
                logger.info(log_msg, *log_args)
                return data
            # 新数据源（crypto/macro/news）：统一写 structured_data.json，
            # 可数值化的点序列并入 clean_chart_data 的 market_trends 供绘图
            (proj / "structured_data.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8",
            )
            # A 方案：排行数据转写为 data/ranking.csv，让 data_analyzer
            # （只认 CSV/JSON）无需再找"新 CSV"即可直接消费
            self._export_ranking_csv(task_id, data, project)
            self._merge_structured_points(task_id, data, project)
            # P1-3：预载即尝试数据驱动图表（crypto/macro 行情点 ≥2 即可渲染），
            # 保证只有"预载 → 报告"两步的加密任务也能出图
            self._render_clean_chart_data(task_id, goal)
            label = metadata.get("label") or source
            push_progress(self._messaging, task_id, "log",
                          {"type": "data", "agent": "orchestrator",
                           "message": f"结构化数据源命中：{label}",
                           "timestamp": self._now_iso()})
            logger.info("Structured data loaded for %s: %s", task_id, source)
            return data
        except Exception as exc:
            logger.warning("Structured data preload failed: %s", str(exc)[:150])
            return None

    def _structured_financial_preload(
        self, task_id: str, goal: str,
    ) -> dict | None:
        """兼容旧名：F4 后统一走 _structured_data_preload（financial 走原通道）。"""
        return self._structured_data_preload(task_id, goal)

    @staticmethod
    def _export_ranking_csv(
        task_id: str, data: dict, project: str | None = None,
    ) -> Path | None:
        """把排行来源（eastmoney/tencent/sina）的 rows 转写为 data/ranking.csv。

        列与适配器 payload 保持一致（rank/code/name/price/change_pct/
        volume_hand/volume_wan_hand/amount_yuan/amount_yi/turnover_pct/
        market_cap_yi），data_analyzer/code_execution 可直接 pd.read_csv 消费；
        sina_ranking 全市场数据（fetched_count 可 >50）整表导出，不做截断，
        供统计类任务计算前 N% 成交额占比（分母=全市场合计）；
        非排行来源返回 None（不产生无关文件）。
        """
        try:
            source = str(data.get("source") or "")
            if source not in _RANKING_SOURCES:
                return None
            rows = (data.get("data") or {}).get("rows") or []
            if not rows:
                return None
            fetched_count = int(
                (data.get("data") or {}).get("fetched_count") or len(rows)
            )
            csv_path = task_data_dir(task_id, project) / "ranking.csv"
            columns = [
                "rank", "code", "name", "price", "change_pct",
                "volume_hand", "volume_wan_hand",
                "amount_yuan", "amount_yi",
                "turnover_pct", "market_cap_yi",
            ]
            with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(columns)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    writer.writerow([row.get(c, "") for c in columns])
            logger.info(
                "ranking.csv exported for %s: %d rows (fetched %d)",
                task_id, len(rows), fetched_count,
            )
            return csv_path
        except Exception as exc:
            logger.warning("ranking csv export failed: %s", str(exc)[:120])
            return None

    @staticmethod
    def _statistical_ranking_instruction(
        task_id: str, goal: str, csv_path,
    ) -> str:
        """统计类排行任务：为 code_execution 生成全市场占比计算指令。

        明确告诉代码步骤读取 data/ranking.csv（已按成交额/成交量降序、
        含全市场数据），计算前 X%（前 target_top_n 只）的合计与占全市场
        比例并输出 JSON，禁止只取前 10 只。"""
        try:
            target = 0
            total = 0
            metric_label = "成交额"
            sd_path = task_project_dir(task_id) / "structured_data.json"
            if sd_path.exists():
                sd = json.loads(sd_path.read_text(encoding="utf-8"))
                payload = sd.get("data") or {}
                target = int(payload.get("target_top_n") or 0)
                total = int(payload.get("total") or 0) or len(
                    payload.get("rows") or []
                )
                if str(payload.get("metric") or "amount") == "volume":
                    metric_label = "成交量"
                partial = bool(payload.get("partial"))
            if not target:
                # 兜底：从目标里的百分比现场计算前 N
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", str(goal or ""))
                if m and total:
                    target = max(
                        1,
                        int(math.ceil(total * float(m.group(1)) / 100.0)),
                    )
            scope = (
                f"（前 {target} 只，全市场约 {total} 只）" if target else ""
            )
            partial_note = ""
            if partial:
                fetched = payload.get("fetched_count") or len(
                    payload.get("rows") or []
                )
                partial_note = (
                    f"，部分数据：仅获取 {fetched} 只"
                    f"（全市场总数 {payload.get('total') or '未知'}，"
                    "统计结果须标注覆盖率）"
                )
            return (
                f"\n[Data: {csv_path}]"
                f"\n[统计任务] 工作区已预载全市场行情数据 {csv_path}"
                f"（已按{metric_label}降序，含全市场{metric_label}）"
                f"{scope}{partial_note}。"
                f"请读取该 CSV，计算前 {target or 'X%'} 的{metric_label}合计"
                f"与占全市场{metric_label}的比例，输出 JSON："
                '{"top_n": ..., "top_amount": ..., "total_amount": ..., '
                '"share_pct": ...}；禁止只取前 10 只，分母必须为全市场合计。'
            )
        except Exception as exc:
            logger.warning(
                "statistical ranking instruction failed: %s", str(exc)[:120],
            )
            return ""

    def _reduce_steps_for_structured(
        self, task_id: str, steps: list[dict], data: dict | None,
    ) -> list[dict]:
        """B 方案：已预载结构化行情数据（排行/crypto/macro/news）的任务，
        把 data_analyzer 步骤替换为 content_summary 直接消费 structured_data.json，
        避免 EDA 步骤只认 CSV 造成断链；图表仍由数据驱动兜底渲染提供。
        保持 step_id 与依赖不变，报告/打包步骤无需重连。"""
        if not steps or not data:
            return steps
        source = str(data.get("source") or "")
        if source not in (
            "eastmoney_ranking", "tencent_ranking", "tencent_us_ranking",
            "coingecko", "macro", "news",
        ):
            return steps
        label = {
            "eastmoney_ranking": "A股行情排行",
            "tencent_ranking": "A股行情排行（腾讯）",
            "tencent_us_ranking": "美股行情排行（腾讯）",
            "sina_ranking": "行情排行（新浪全市场）",
            "coingecko": "加密货币行情",
            "macro": "宏观指标",
            "news": "新闻列表",
        }.get(source, source)
        out: list[dict] = []
        changed = False
        for s in steps:
            if str(s.get("capability")) == "data_analyzer":
                changed = True
                out.append({
                    "step_id": s.get("step_id"),
                    "capability": "content_summary",
                    "instruction": (
                        f"基于已预载的 structured_data.json（{label}）"
                        "直接输出结构化要点与结论；无需寻找 CSV，无需重新抓取数据；"
                        "如目标需要图表，请按 [CHART_DATA] 规格输出图表数据"
                        "或引用工作区已生成的图表。"
                        f"原始指令：{s.get('instruction', '')}"
                    ),
                    "depends_on": list(s.get("depends_on") or []),
                    "timeout": 120,
                })
                push_progress(self._messaging, task_id, "log",
                              {"type": "plan", "agent": "orchestrator",
                               "message": (
                                   "Plan B: 结构化数据已预载，data_analyzer "
                                   "替换为 content_summary 直接消费"
                               ),
                               "timestamp": self._now_iso()})
            else:
                out.append(s)
        if changed:
            logger.info("Task %s: data_analyzer -> content_summary (%s)", task_id, source)
        return out

    @staticmethod
    def _merge_structured_points(
        task_id: str, data: dict, project: str | None = None,
    ) -> None:
        """把 crypto/macro 的点序列并入 clean_chart_data 的 market_trends，
        让新适配器的数据能走既有"清洗 → 绘图 → 报告"通道。"""
        try:
            source = str(data.get("source") or "")
            payload = data.get("data") or {}
            meta = data.get("metadata") or {}
            rows: list[dict] = []
            if source == "coingecko":
                try:
                    from adapters.coingecko import coin_zh
                    series = f"{coin_zh(meta.get('coin'))}行情"
                except Exception:
                    series = "加密货币行情"
                for key, label, unit in (
                    ("price", "当前价格", "USD"),
                    ("market_cap", "市值", "USD"),
                    ("volume_24h", "24小时成交量", "USD"),
                    ("change_24h", "24小时涨跌幅", "%"),
                ):
                    if payload.get(key) is not None:
                        rows.append({
                            "type": "market_size",
                            "label": label,
                            "value": float(payload[key]),
                            "unit": unit,
                            "source": "coingecko",
                            "caliber": "CoinGecko 实时行情",
                            "series": series,
                        })
            elif source == "macro":
                series = str(payload.get("series") or "")
                for pt in (payload.get("points") or [])[-40:]:
                    try:
                        rows.append({
                            "type": "market_trends",
                            "label": f"{series} {pt.get('date')}",
                            "value": float(pt.get("value")),
                            "unit": "",
                            "year": None,
                            "source": "fred",
                            "series": series,
                        })
                    except (TypeError, ValueError):
                        continue
            elif source in _RANKING_SOURCES:
                metric = str(payload.get("metric") or "amount")
                market_label = str(payload.get("market") or "")
                if not market_label:
                    market_label = (
                        "美股" if source == "tencent_us_ranking" else "A股"
                    )
                series = (
                    f"{market_label}成交额排行"
                    if metric != "volume" else f"{market_label}成交量排行"
                )
                caliber = (
                    "新浪行情中心全市场排行"
                    if source == "sina_ranking"
                    else (
                        "腾讯行情实时排行"
                        if source.startswith("tencent")
                        else "东方财富行情中心实时排行"
                    )
                )
                for row in (payload.get("rows") or [])[:20]:
                    label = f"{row.get('rank')}.{row.get('name')}"
                    if metric == "volume":
                        v = row.get("volume_wan_hand")
                        unit = "万手"
                    else:
                        v = row.get("amount_yi")
                        unit = "亿元"
                    if v is not None:
                        rows.append({
                            "type": "market_size",
                            "label": label,
                            "value": float(v),
                            "unit": unit,
                            "source": source,
                            "caliber": caliber,
                            "series": series,
                        })
                    if row.get("change_pct") is not None:
                        rows.append({
                            "type": "market_size",
                            "label": f"{label}涨跌幅",
                            "value": float(row["change_pct"]),
                            "unit": "%",
                            "source": source,
                            "caliber": caliber,
                            "series": f"{series}涨跌幅",
                        })
                    # 最新价行：供"量价散点"（价格 × 成交量/成交额）成图
                    if row.get("price") is not None:
                        rows.append({
                            "type": "market_size",
                            "label": f"{label}最新价",
                            "value": float(row["price"]),
                            "unit": "元",
                            "source": source,
                            "caliber": caliber,
                            "series": f"{series}最新价",
                        })
            if rows:
                proj = task_project_dir(task_id, project)
                clean_path = proj / "clean_chart_data.json"
                clean = {}
                if clean_path.exists():
                    try:
                        clean = json.loads(clean_path.read_text(encoding="utf-8"))
                    except Exception:
                        clean = {}
                md = list(clean.get("market_data") or [])
                seen = {(r.get("label"), r.get("value"), r.get("unit")) for r in md}
                for r in rows:
                    k = (r.get("label"), r.get("value"), r.get("unit"))
                    if k not in seen:
                        seen.add(k)
                        md.append(r)
                clean["market_data"] = md
                clean_path.write_text(
                    json.dumps(clean, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
        except Exception as exc:
            logger.warning("merge structured points failed: %s", str(exc)[:120])

    def _remerge_structured_points(self, task_id: str) -> None:
        """搜索清洗/快照回灌后，把 crypto/macro 结构化行重新并入
        clean_chart_data.json，避免被 clean_file 重写覆盖（数据驱动兜底依赖）。"""
        try:
            proj = task_project_dir(task_id)
            sd_path = proj / "structured_data.json"
            if not sd_path.exists():
                return
            sd = json.loads(sd_path.read_text(encoding="utf-8"))
            self._merge_structured_points(task_id, sd)
            self._export_ranking_csv(task_id, sd)
        except Exception as exc:
            logger.warning("remerge structured points failed: %s", str(exc)[:120])

    @staticmethod
    def _structured_injection(task_id: str) -> str:
        """构造报告/总结步骤的 [结构化数据] 内容块：
        财务 → financials.json（沿用既有表格）；crypto/macro/news/行情排行 →
        structured_data.json（新通道）。返回空串表示无结构化数据。"""
        try:
            proj = task_project_dir(task_id)
            fin_path = proj / "financials.json"
            if fin_path.exists():
                fin = json.loads(fin_path.read_text(encoding="utf-8"))
                if str(fin.get("source") or "") == "multi_entity":
                    blocks: list[str] = []
                    for ent in fin.get("companies") or []:
                        fs = (ent.get("financials") or [])[-12:]
                        m = ent.get("metadata") or {}
                        if not fs:
                            continue
                        unit = m.get("unit") or "亿元"
                        currency = str(m.get("currency") or "").strip()
                        currency_note = ""
                        if currency:
                            currency_note = f"，币种：{currency}"
                            low_c = currency.lower()
                            if not any(
                                k in low_c
                                for k in ("cny", "rmb", "人民币", "元")
                            ):
                                currency_note += (
                                    "（非人民币口径：若报告以人民币呈现，须换算并"
                                    "注明换算说明，或明确标注原币种与口径差异）"
                                )
                        retrieved_at = str(m.get("retrieved_at") or "")
                        src_name = _STRUCTURED_SOURCE_LABELS.get(
                            str(m.get("source") or ""),
                            str(m.get("source") or "结构化数据源"),
                        )
                        rows = [
                            "| 年份 | 营收 | 归母净利润 | 毛利率% | 总负债 | 经营现金流 |",
                            "|---|---|---|---|---|---|",
                        ]
                        for f in fs:
                            rows.append(
                                f"| {f.get('year')} | {f.get('revenue')} | "
                                f"{f.get('net_profit')} | "
                                f"{f.get('gross_margin')} | "
                                f"{f.get('total_liabilities')} | "
                                f"{f.get('operating_cashflow')} |"
                            )
                        blocks.append(
                            "\n[结构化财务数据]（"
                            f"{ent.get('name')}，来自 {src_name}，单位：{unit}"
                            + currency_note
                            + (
                                f"，数据获取时间 {retrieved_at}"
                                if retrieved_at else ""
                            )
                            + "，权威数据源，优先引用）\n"
                            + "\n".join(rows)
                        )
                    if blocks:
                        return (
                            "\n\n" + "\n\n".join(blocks)
                            + "\n规则：报告/总结中的财务数字优先引用以上各实体表格，"
                            "并在来源处标注实体与数据源；对比结论必须基于表中数值；"
                            "本表未覆盖的数字若无溯源，标注"
                            "'基于模型知识，未在本次检索中验证'；禁止编造年份；"
                            "报告的数据截至/报告日期必须引用各表标注的数据获取时间，"
                            "禁止使用模型回忆的日期。"
                        )
                fs = (fin.get("financials") or [])[-12:]
                m = fin.get("metadata") or {}
                if fs:
                    unit = m.get("unit") or "亿元"
                    # P2-4：metadata.currency 存在时标注币种；非人民币口径要求
                    # 报告换算或注明口径差异，避免 HKD 数值被直接当人民币陈述
                    currency = str(m.get("currency") or "").strip()
                    currency_note = ""
                    if currency:
                        currency_note = f"，币种：{currency}"
                        low_c = currency.lower()
                        if not any(
                            k in low_c for k in ("cny", "rmb", "人民币", "元")
                        ):
                            currency_note += (
                                "（非人民币口径：若报告以人民币呈现，须换算并"
                                "注明换算说明，或明确标注原币种与口径差异）"
                            )
                    retrieved_at = str(m.get("retrieved_at") or "")
                    src_name = {
                        "eastmoney_datacenter": "东方财富数据中心（港交所披露）",
                        "eastmoney_ashare": "东方财富数据中心（A股财报）",
                        "sec_edgar": "SEC EDGAR（10-K 年报）",
                    }.get(str(m.get("source")), str(m.get("source")))
                    rows = [
                        "| 年份 | 营收 | 归母净利润 | 毛利率% | 总负债 | 经营现金流 |",
                        "|---|---|---|---|---|---|",
                    ]
                    for f in fs:
                        rows.append(
                            f"| {f.get('year')} | {f.get('revenue')} | "
                            f"{f.get('net_profit')} | {f.get('gross_margin')} | "
                            f"{f.get('total_liabilities')} | "
                            f"{f.get('operating_cashflow')} |"
                        )
                    return (
                        "\n\n[结构化财务数据]（来自 " + src_name + "，单位：" + unit
                        + currency_note
                        + (f"，数据获取时间 {retrieved_at}" if retrieved_at else "")
                        + "，权威数据源，优先引用）\n" + "\n".join(rows)
                        + "\n规则：报告/总结中的财务数字优先引用本表，并在来源处标注"
                        + src_name + "；本表未覆盖的数字若无溯源，标注"
                        "'基于模型知识，未在本次检索中验证'；禁止编造年份；"
                        "报告的数据截至/报告日期必须引用本表标注的数据获取时间，"
                        "禁止使用模型回忆的日期。"
                        + (
                            "币种口径：本表币种为 " + currency + "，报告必须注明币种；"
                            "以人民币口径呈现时必须换算并注明换算说明，或明确标注"
                            "原币种与口径差异，禁止把本表数值直接当作人民币陈述。"
                            if currency else ""
                        )
                    )
            sd_path = proj / "structured_data.json"
            if sd_path.exists():
                sd = json.loads(sd_path.read_text(encoding="utf-8"))
                source = str(sd.get("source") or "")
                payload = sd.get("data") or {}
                meta = sd.get("metadata") or {}
                retrieved_at = str(meta.get("retrieved_at") or "")
                time_hint = f"，数据获取时间 {retrieved_at}" if retrieved_at else ""
                lines: list[str] = []
                if source == "coingecko":
                    try:
                        from adapters.coingecko import coin_zh
                        name = coin_zh(meta.get("coin")) or "加密货币"
                    except Exception:
                        name = meta.get("coin") or "加密货币"
                    vs = str(meta.get("vs_currency") or "usd").upper()
                    lines = [
                        "| 指标 | 数值 |",
                        "|---|---|",
                        f"| 当前价格 | {payload.get('price')} {vs} |",
                        f"| 市值 | {payload.get('market_cap')} {vs} |",
                        f"| 24小时成交量 | {payload.get('volume_24h')} {vs} |",
                        f"| 24小时涨跌幅 | {payload.get('change_24h')}% |",
                    ]
                    block_title = (
                        f"[结构化数据]（{name} 实时行情，CoinGecko{time_hint}）"
                    )
                elif source == "macro":
                    series = str(payload.get("series") or "")
                    label = str(payload.get("indicator") or series)
                    lines = ["| 日期 | 数值 |", "|---|---|"]
                    for pt in (payload.get("points") or [])[-24:]:
                        lines.append(f"| {pt.get('date')} | {pt.get('value')} |")
                    block_title = (
                        f"[结构化数据]（{label}，FRED 宏观指标{time_hint}）"
                    )
                elif source == "news":
                    query = str(payload.get("query") or "")
                    lines = ["| 标题 | 来源时间 | 链接 |", "|---|---|---|"]
                    for it in (payload.get("items") or [])[:15]:
                        title = str(it.get("title") or "").replace("|", "｜")
                        pub = str(it.get("published") or "")
                        link = str(it.get("link") or "")
                        lines.append(f"| {title} | {pub} | {link} |")
                    block_title = (
                        f"[结构化数据]（新闻列表，Google News RSS，"
                        f"查询：{query or '默认'}{time_hint}）"
                    )
                elif source in _RANKING_SOURCES:
                    metric = str(payload.get("metric") or "amount")
                    market_label = str(payload.get("market") or "")
                    if not market_label:
                        market_label = (
                            "美股" if source == "tencent_us_ranking" else "A股"
                        )
                    if source == "sina_ranking":
                        # 新浪全市场：报告块给前 20 行摘要即可，全量在 ranking.csv
                        source_label = "新浪行情中心"
                        row_limit = 20
                        title = (
                            f"{market_label}成交量排行（全市场）"
                            if metric == "volume"
                            else f"{market_label}成交额排行（全市场）"
                        )
                    else:
                        source_label = (
                            "腾讯行情"
                            if source.startswith("tencent")
                            else "东方财富行情中心"
                        )
                        row_limit = 10
                        title = (
                            f"{market_label}成交量排行（前十）"
                            if metric == "volume"
                            else f"{market_label}成交额排行（前十）"
                        )
                    lines = [
                        "| 排名 | 代码 | 名称 | 最新价 | 涨跌幅% | "
                        "成交量(万手) | 成交额(亿元) | 换手率% |",
                        "|---|---|---|---|---|---|---|---|",
                    ]
                    for row in (payload.get("rows") or [])[:row_limit]:
                        lines.append(
                            f"| {row.get('rank')} | {row.get('code')} | "
                            f"{row.get('name')} | {row.get('price')} | "
                            f"{row.get('change_pct')} | "
                            f"{row.get('volume_wan_hand')} | "
                            f"{row.get('amount_yi')} | {row.get('turnover_pct')} |"
                        )
                    # 部分数据降级：报告块必须标注覆盖率（fetched_count/total），
                    # 避免把不完整全市场数据当作全量口径陈述
                    coverage_note = ""
                    if payload.get("partial"):
                        fetched = payload.get("fetched_count")
                        total = payload.get("total")
                        if total:
                            coverage_note = (
                                f"，部分数据：已获取 {fetched}/{total} 只"
                            )
                        else:
                            coverage_note = (
                                f"，部分数据：已获取 {fetched} 只"
                                "（全市场总数未知）"
                            )
                    block_title = (
                        f"[结构化数据]（{title}，{source_label}"
                        f"{coverage_note}{time_hint}）"
                    )
                if lines:
                    return (
                        f"\n\n{block_title}\n" + "\n".join(lines)
                        + "\n规则：报告/总结优先引用本表内容，并在来源处标注数据源；"
                        "表中没有的信息若无溯源，标注'基于模型知识，未在本次检索中验证'；"
                        "报告的数据截至/报告日期必须引用本表标注的数据获取时间"
                        "（retrieved_at），禁止使用模型回忆的日期；"
                        "若上下文中没有任何结构化数据块，须在报告末尾标注"
                        "'数据截至日期：未获取（本次任务未提供结构化数据）'。"
                    )
        except Exception:
            pass
        return ""

    @staticmethod
    def _merge_structured_financials(
        clean: dict, financials: list, source_url: str,
        entity: str | None = None,
    ) -> dict:
        """把适配器结构化财务行并入 clean_chart_data 的 market_data（去重）。

        entity 非空时标签带实体前缀（多实体对比，如“宁德时代2024年营收”），
        避免不同公司同年指标互相覆盖。
        """
        clean = dict(clean or {})
        md = list(clean.get("market_data") or [])
        seen = {(r.get("label"), r.get("value"), r.get("unit")) for r in md}
        for f in financials or []:
            if not isinstance(f, dict):
                continue
            for key, label, unit in (
                ("revenue", "营收", "亿元"), ("net_profit", "归母净利润", "亿元"),
                ("gross_profit", "毛利润", "亿元"), ("gross_margin", "毛利率", "%"),
                ("operating_profit", "经营利润", "亿元"),
                ("total_assets", "总资产", "亿元"), ("total_liabilities", "总负债", "亿元"),
                ("operating_cashflow", "经营现金流", "亿元"),
            ):
                v = f.get(key)
                if v is None:
                    continue
                label = f"{entity}{f.get('year')}年{label}" if entity else (
                    f"{f.get('year')}年{label}"
                )
                row = {
                    "type": "market_size",
                    "label": label,
                    "value": v, "unit": unit,
                    "year": f.get("year"),
                    "source": source_url,
                    "caliber": f"年报口径（{f.get('report_type') or ''}）",
                }
                k = (row["label"], row["value"], row["unit"])
                if k not in seen:
                    seen.add(k)
                    md.append(row)
        clean["market_data"] = md
        return clean

    def _remerge_structured_financials(self, task_id: str) -> None:
        """搜索清洗/快照回灌后，把结构化源数据重新并入 clean_chart_data.json。"""
        try:
            proj = task_project_dir(task_id)
            fin_path = proj / "financials.json"
            clean_path = proj / "clean_chart_data.json"
            if not fin_path.exists() or not clean_path.exists():
                return
            fin = json.loads(fin_path.read_text(encoding="utf-8"))
            clean = json.loads(clean_path.read_text(encoding="utf-8"))
            if str(fin.get("source") or "") == "multi_entity":
                for ent in fin.get("companies") or []:
                    clean = self._merge_structured_financials(
                        clean, ent.get("financials") or [],
                        (ent.get("raw") or {}).get("url", ""),
                        entity=ent.get("name"),
                    )
            else:
                clean = self._merge_structured_financials(
                    clean, fin.get("financials") or [],
                    (fin.get("raw") or {}).get("url", ""),
                )
            clean_path.write_text(
                json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("remerge structured financials failed: %s", str(exc)[:120])

    def _enforce_no_web_scrape_code(
        self, steps: list[dict], goal: str, task_id: str,
    ) -> list[dict]:
        """财报/研报/调研/金融类任务：禁止 code_execution 抓网页/解析在线数据。
        命中后改写为 web_fetch（含 URL）或 content_summary，防止"代码抓财报"连环失败。"""
        g = str(goal or "").lower()
        if not any(k in g for k in (
            "财报", "研报", "调研", "报告", "金融", "行情", "股票", "上市公司",
            "industry", "report", "financial",
        )):
            return steps
        changed = False
        for s in steps:
            if s.get("capability") != "code_execution":
                continue
            ins = str(s.get("instruction") or "")
            if not re.search(
                r"(https?://|抓取|爬取|爬虫|解析网页|网页内容|网页数据|financial|财报|财报数据)",
                ins, re.I,
            ):
                continue
            urls = re.findall(r"https?://[^\s\]\)]+", ins)
            if urls:
                s["capability"] = "web_fetch"
                s["instruction"] = (
                    "抓取以下网页的完整正文内容并保留原始URL，输出可读文本：\n"
                    + "\n".join(urls[:5])
                )
                s["timeout"] = 180
            else:
                s["capability"] = "content_summary"
                s["instruction"] = (
                    "基于本任务已有的搜索/抓取结果，用中文输出结构化的内容总结"
                    "（含关键数据、时间与来源）；数据不足时如实列出缺失项，禁止编造。"
                )
                s["timeout"] = 120
            s["_rewritten"] = True
            changed = True
        if changed:
            push_progress(self._messaging, task_id, "log",
                          {"type": "replan", "agent": "orchestrator",
                           "message": "规划器约束：财报/研报类任务禁用 code_execution 抓网页，"
                                      "已改写为 web_fetch/content_summary",
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
            # 验收点兜底（对标标准 3.2 Plan & Execute）：planner 漏写时自动补
            if "验收：" not in instruction:
                instruction = instruction + "\n验收：步骤完成后输出可验证的结果（文件/数据/文本均可）。"
            s["instruction"] = instruction
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
            mode = str(s.get("mode") or "parallel").strip().lower()
            if mode not in ("pipeline", "parallel", "human_in_loop"):
                mode = "parallel"
            # P1-2：低风险内部能力步骤不允许 human_in_loop，强制串行 pipeline，
            # 避免执行前等人工确认导致整轮超时（如 package 被规划器误标）。
            if mode == "human_in_loop" and cap not in _HUMAN_IN_LOOP_ALLOWED:
                logger.info("低风险步骤强制 pipeline：%s（step %s）", cap, sid)
                mode = "pipeline"
            s["mode"] = mode
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

    def _reflect(
        self, goal: str, report: str, task_id: str,
        all_steps: list[dict], completed_all: dict,
        memory_context: str = "", validator_summary: str = "",
        eval_scores: str = "",
    ) -> dict | None:
        """验收评审：基于【完整上下文】判断交付物是否达标。
        上下文含用户目标、检索到的私有知识、全部步骤及结果摘要、当前报告；
        LLM 可返回 accept / stop / retry_step（单步重做）/ add_steps。"""
        push_progress(self._messaging, task_id, "log",
                      {"type": "iteration", "agent": "orchestrator",
                       "message": "Reflecting: reviewing deliverable against goal",
                       "timestamp": self._now_iso()})
        ctx_parts = [f"Goal:\n{goal}"]
        if memory_context:
            ctx_parts.append(f"Retrieved private knowledge:\n{memory_context[:2000]}")
        if all_steps:
            brief = []
            for s in all_steps[-15:]:
                r = completed_all.get(s["step_id"], {})
                st = r.get("status") if isinstance(r, dict) else "?"
                res = str(r.get("result") or "")[:180] if isinstance(r, dict) else ""
                brief.append(f"- [{st}] {s['step_id']} ({s.get('capability')}): {res[:150]}")
            ctx_parts.append("Execution steps & results:\n" + "\n".join(brief))
        ctx_parts.append(f"Deliverable (report):\n{str(report)[:3000]}")
        if validator_summary:
            ctx_parts.append(validator_summary)
        if eval_scores:
            ctx_parts.append(f"自动评测分数（供参考）：\n{eval_scores}")
        # 确定性验收器缺口报告：让反思基于硬检查精准补缺口，而不是"感觉不够好"
        try:
            from workspace import task_workspace
            acc_path = task_workspace(task_id) / "acceptance_report.json"
            if acc_path.exists():
                acc = json.loads(acc_path.read_text(encoding="utf-8"))
                if acc.get("gaps"):
                    ctx_parts.append(
                        "验收器缺口报告（确定性检查结果；请优先修复以下缺口，"
                        "只重做能补齐缺口的步骤，不要整轮重来）：\n"
                        + "\n".join(f"- {g}" for g in acc["gaps"])
                    )
        except Exception:
            pass
        # P1-4：结构化失败诊断进入反思 prompt（不包含原始错误文本/自然语言反馈）
        try:
            from step_diagnosis import read_step_failures
            _diags = read_step_failures(task_id)
            if _diags:
                _lines = []
                for _d in _diags[-10:]:
                    _lines.append(
                        f"- step_id={_d.step_id} capability={_d.capability} "
                        f"error_type={_d.error_type} tried={_d.tried_alternatives} "
                        f"suggestion={_d.suggestion} replacement={_d.replacement_step_id} "
                        f"outcome={_d.replacement_outcome}"
                    )
                ctx_parts.append(
                    "步骤失败诊断（结构化；只重做能修复失败/缺口的步骤，"
                    "优先采用 suggestion）：\n" + "\n".join(_lines)
                )
        except Exception:
            pass
        # Roadmap 余项③：错误模式库（跨任务聚合的修复模板）注入反思
        try:
            from error_patterns import build_reflection_context
            _ptn = build_reflection_context(limit=5)
            if _ptn:
                ctx_parts.append(_ptn)
        except Exception:
            pass
        prompt = "\n\n".join(ctx_parts)
        try:
            from prompt_registry import get_prompt
            raw = self._planner_llm.call(
                get_prompt("reflect", ITERATOR_SYSTEM, goal=goal),
                prompt, expect_json=True, max_tokens=8192,
                usage="plan",
            )
            if isinstance(raw, dict):
                return raw
            clean = str(raw).strip()
            if clean.startswith("```"):
                clean = clean.strip("`")
                if clean.startswith("json"):
                    clean = clean[4:]
            return _loads_json_loose(clean.strip())
        except Exception as exc:
            # P0-1：反思 LLM 不可用（空内容/401/402/403/超时/网络）时记录降级，
            # 完成阶段据此把状态降为 SUCCESS_WITH_ISSUES，避免"验收失败仍报 SUCCESS"
            try:
                from llm_client import _degradation_reason, _record_task_degradation
                _reason = _degradation_reason(exc)
                if _reason not in ("generic", ""):
                    self._reflection_llm_unavailable = _reason
                    _record_task_degradation(task_id, _reason, both_failed=False)
                    logger.warning(
                        "反思 LLM 失败（%s），停止迭代（节省成本）: %s",
                        _reason, str(exc)[:150],
                    )
                    return None
            except Exception:
                pass
            logger.warning(
                "反思 LLM 失败，停止迭代（节省成本）: %s", str(exc)[:150],
            )
            return None

    def _redo_step_and_dependents(
        self, task_id: str, goal: str,
        all_steps: list[dict], completed_all: dict,
        step_id: str, feedback: str,
    ) -> bool:
        """反思要求"单步重做"：重做指定步骤及其传递依赖它的下游步骤
        （报告/摘要会基于重做后的结果重新生成），不整轮任务重来。"""
        target = next((s for s in all_steps if s.get("step_id") == step_id), None)
        if not target:
            return False
        # 保存原结果：若重做后质量明显劣化则恢复并停止（防止 fallback 覆盖好报告）
        old_result = completed_all.get(step_id, {})
        old_text = str(old_result.get("result") or "")
        dependents: set[str] = set()
        changed = True
        while changed:
            changed = False
            for s in all_steps:
                if s["step_id"] in dependents or s["step_id"] == step_id:
                    continue
                if any(d in dependents or d == step_id for d in s.get("depends_on", [])):
                    dependents.add(s["step_id"])
                    changed = True
        order = [
            s for s in all_steps
            if s["step_id"] == step_id or s["step_id"] in dependents
        ]
        # B3：单轮反思重做最多 N 步（默认 2），避免缺口多时把整条依赖链全部重做。
        # 排序保证目标步骤优先，其余按依赖距离（依赖越少越靠前）截断。
        order.sort(key=lambda s: (
            s["step_id"] != step_id,
            len(s.get("depends_on", []) or []),
        ))
        max_redo_steps = max(
            1, int(getattr(self, "_max_redo_steps", 2) or 2)
        )
        if len(order) > max_redo_steps:
            logger.warning(
                "单轮反思重做步数上限 %d，本次从 %d 步中仅重做前 %d 步",
                max_redo_steps, len(order), max_redo_steps,
            )
            push_progress(self._messaging, task_id, "log",
                          {"type": "iteration", "agent": "orchestrator",
                           "message": (
                               f"单轮反思重做步数上限 {max_redo_steps}，"
                               f"仅重做前 {max_redo_steps} 步"
                           ),
                           "timestamp": self._now_iso()})
            order = order[:max_redo_steps]
        for s in order:
            s2 = dict(s)
            s2["depends_on"] = []  # 依赖步骤已完成，单步独立重做
            if s["step_id"] == step_id:
                extra = ""
                if s.get("capability") == "web_search":
                    extra = (
                        "；本次重做必须更换查询词/增加限定条件"
                        "（site: 官方域名、年份、具体指标词），"
                        "禁止原样重复上次查询，否则只会得到相同结果"
                    )
                # P1-4：优先使用结构化失败诊断（suggestion 为修复方向），
                # 不再把自然语言反馈文本拼进 worker 指令（防"报告抄反馈"泄漏）
                diag = self._diagnosis_for_step(task_id, step_id)
                if diag and diag.suggestion:
                    s2["instruction"] = (
                        f"{s['instruction']}\n\n"
                        f"【失败诊断】capability={diag.capability} "
                        f"error_type={diag.error_type} 已尝试={diag.tried_alternatives} "
                        f"建议修复={diag.suggestion}{extra}"
                    )
                elif feedback:
                    s2["instruction"] = (
                        f"{s['instruction']}\n\n【反思要求重做】{feedback}{extra}"
                    )
            orig_instr = s2.get("instruction", "")
            result = self._dispatch_step_safe(goal, s2, task_id, {"replan_used": 0})
            if (
                s["step_id"] == step_id
                and target.get("capability") in ("report_generator", "content_summary")
                and self._redo_result_worse(old_text, result.get("result", ""))
            ):
                # 重做劣化（fallback 空壳 / 长度大幅缩水）→ 恢复原结果并停止重做
                logger.warning(
                    "Redo of step %s made result worse (%d -> %d chars); "
                    "keeping original and stopping",
                    step_id, len(old_text), len(str(result.get("result") or "")),
                )
                completed_all[step_id] = old_result
                push_progress(self._messaging, task_id, "log",
                              {"type": "iteration", "agent": "orchestrator",
                               "message": "反思重做结果劣化，保留原结果并停止该步骤重做",
                               "timestamp": self._now_iso()})
                return False
            completed_all[s["step_id"]] = result
            if s["step_id"] == step_id and result.get("status") == "SUCCESS":
                # 反思改变提示词并成功 → 沉淀进进化系统 RAG，供后续任务检索
                self._record_reflection_refinement(
                    goal, task_id,
                    key=f"step:{s.get('capability')}",
                    issue=f"反思要求重做：{feedback}",
                    fix_prompt=f"优化前：{orig_instr[:250]}\n优化后：{s2.get('instruction', '')[:250]}",
                )
            push_progress(self._messaging, task_id, "log",
                          {"type": "iteration", "agent": "orchestrator",
                           "message": f"反思单步重做: step {s['step_id']} ({s.get('capability')}) -> {result.get('status')}",
                           "timestamp": self._now_iso()})
        return True

    @staticmethod
    def _redo_result_worse(old: str, new: str) -> bool:
        """重做结果是否明显劣化：新结果是 fallback 标记，或长度不足原结果 40%。"""
        o, n = str(old or ""), str(new or "")
        if len(o) < 300:
            return False
        if "fallback" in n.lower() and "fallback" not in o.lower():
            return True
        if len(n) < len(o) * 0.4:
            return True
        return False

    def _run_acceptance_check(self, task_id: str, goal: str) -> dict | None:
        """报告生成后跑确定性验收器：数字溯源等 checklist → 缺口报告。
        结果写入任务工作区 acceptance_report.json 并推前端，供反思精准补缺口。"""
        try:
            from acceptance_checker import run_acceptance
            from workspace import task_reports_dir, task_workspace
            rpath = task_reports_dir(task_id) / "report.md"
            if not rpath.exists():
                return None
            report = rpath.read_text(encoding="utf-8")
            result = run_acceptance(task_id, goal, report, task_workspace(task_id))
            # V1.2 竞品启示：文末来源清单 URL 存活校验（仅提示，不判 fail）。
            # 支持 URL_HEALTH_CHECK=0 关闭，避免网络抖动误伤验收结果。
            try:
                if os.environ.get("URL_HEALTH_CHECK", "1") != "0":
                    from acceptance_checker import extract_source_list
                    from adapters.url_health import check_urls
                    src_urls = [
                        e.get("url") for e in extract_source_list(report)
                        if e.get("url")
                    ]
                    if src_urls:
                        health = check_urls(src_urls)
                        dead = [
                            u for u, st in health.items() if st == "dead"
                        ]
                        if dead:
                            hint = f"来源链接失效: {len(dead)} 条"
                            result.setdefault("checks", {})["url_health"] = {
                                "pass": True,  # 仅提示：不改变 overall
                                "hint": True,
                                "details": hint + "（仅提示，不影响验收结论）",
                                "dead_count": len(dead),
                                "dead_urls": dead[:10],
                            }
                            if hint not in result.get("gaps", []):
                                result.setdefault("gaps", []).append(hint)
            except Exception:
                pass
            try:
                (task_workspace(task_id) / "acceptance_report.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            except Exception:
                pass
            summary = "；".join(result.get("gaps") or []) or "验收通过"
            push_progress(self._messaging, task_id, "acceptance",
                          {"message": summary, "timestamp": self._now_iso()})
            logger.info("Acceptance(%s): overall=%s %s",
                        task_id, result.get("overall"), summary)
            # 评测集自动生长：验收 fail 的真实任务沉淀为新评测案例（静默，不干扰主线）
            try:
                if result.get("overall") == "fail":
                    from evals.auto_grow import harvest_failure
                    harvest_failure(task_id, goal, result, report)
            except Exception as _exc:
                logger.warning("评测集自动沉淀异常（已忽略）: %s", str(_exc)[:100])
            return result
        except Exception as exc:
            logger.warning("Acceptance check failed: %s", str(exc)[:150])
            return None

    def _record_reflection_refinement(
        self, goal: str, task_id: str, key: str, issue: str, fix_prompt: str,
    ) -> None:
        """把反思对提示词的改动沉淀进进化系统 RAG。失败不影响任务主线。"""
        # 失败教训写回 Skill（对标标准 3.8）：自动沉淀，供后续任务注入
        if str(task_id or "").startswith("ui-"):
            try:
                from skill_registry import match_skills, record_lesson
                _hits = match_skills(goal, key.replace("step:", ""))
                record_lesson(
                    task_id=task_id, goal=goal,
                    capability=key.replace("step:", ""),
                    issue=issue, fix=fix_prompt,
                    skill_name=_hits[0]["name"] if _hits else "",
                )
            except Exception:
                pass
        rec = getattr(self._memory, "add_prompt_refinement", None)
        if not callable(rec):
            return
        try:
            rec(
                goal=goal, key=key,
                issue=str(issue)[:300],
                fix_prompt=str(fix_prompt)[:800],
                rationale="反思轮发现缺陷后对步骤提示词的修改",
                task_id=task_id, version=1, outcome="reflection",
            )
        except Exception as exc:
            logger.warning("Reflection refinement RAG record failed: %s", str(exc)[:120])

    def _generation_fallback_step(self, goal: str, step: dict, structured_hint: str = "") -> dict:
        """搜索/抓取无果时的降级步骤：优先用结构化数据，其次模型知识（须标注）。"""
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
                "外部检索/抓取失败。"
                + (f"优先使用任务上下文中已提供的结构化财务数据{structured_hint}；"
                   if structured_hint else "")
                + "其余内容基于已有知识直接完成，数值若无法溯源标注"
                "'基于模型知识，未在本次检索中验证'，不要提及搜索或抓取失败："
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
        """步骤失败后，让 LLM 提出一个替代步骤（方案 A：同目标换实现）。
        失败诊断 + 结构化源优先：金融任务搜索/抓取失败 → 先转向已注入的
        [结构化财务数据]，而不是直接退化为模型知识（避免 IT之家/幻觉来源）。"""
        failed_cap = step.get("capability", "")
        # 失败诊断：结构化数据源是否可用
        structured_hint = ""
        try:
            fin_path = task_project_dir(task_id) / "financials.json"
            if fin_path.exists():
                fin = json.loads(fin_path.read_text(encoding="utf-8"))
                m = fin.get("metadata") or {}
                if str(fin.get("source") or "") == "multi_entity":
                    n_entities = m.get("entities") or len(
                        fin.get("companies") or []
                    )
                    structured_hint = (
                        "（工作区已有结构化财务数据：multi_entity，"
                        f"{n_entities} 个实体）"
                    )
                elif m.get("annual_count"):
                    structured_hint = (
                        f"（工作区已有结构化财务数据：{m.get('source')}，"
                        f"{m.get('annual_count')} 份年报，单位 {m.get('unit')}）"
                    )
            else:
                sd_path = task_project_dir(task_id) / "structured_data.json"
                if sd_path.exists():
                    sd = json.loads(sd_path.read_text(encoding="utf-8"))
                    m = sd.get("metadata") or {}
                    structured_hint = (
                        "（工作区已有结构化数据："
                        f"{m.get('label') or sd.get('source')}）"
                    )
        except Exception:
            pass
        # 金融任务：搜索/抓取失败 → 优先用结构化数据完成分析（不是模型知识）
        if failed_cap in ("web_search", "web_fetch") and structured_hint:
            alt = {
                "capability": "content_summary",
                "instruction": (
                    "本步骤的外部检索/抓取失败。改用任务上下文中已提供的结构化财务数据"
                    f"{structured_hint} 完成分析：优先引用 [结构化财务数据] 表格中的数值"
                    "（来源标注为该结构化源）；表中没有的数据若无法溯源，标注"
                    "'基于模型知识，未在本次检索中验证'，禁止编造。"
                    f"目标：{goal[:300]}"
                ),
                "timeout": 120,
            }
            alt["step_id"] = f"alt-{step.get('step_id', '?')}-{int(time.time())}"
            push_progress(self._messaging, task_id, "log",
                          {"type": "replan", "agent": "orchestrator",
                           "message": "Replan (structured-source fallback): 外部检索失败，"
                                      "改用结构化财务数据",
                           "timestamp": self._now_iso()})
            return alt
        # 搜索/抓取类失败降级为 LLM 生产（保留，但内容优先用结构化数据）
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
            alt = self._generation_fallback_step(goal, step, structured_hint)
            alt["step_id"] = f"alt-{step.get('step_id', '?')}-{int(time.time())}"
            push_progress(self._messaging, task_id, "log",
                          {"type": "replan", "agent": "orchestrator",
                           "message": f"Replan (search-fallback): {alt.get('capability')}: {str(alt.get('instruction'))[:60]}",
                           "timestamp": self._now_iso()})
            return alt
        prompt = (
            f"Goal: {goal}\n\n"
            f"Failed step: [{step.get('capability')}] {step.get('instruction')}\n"
            f"Error: {str(error)[:300]}\n"
            f"Available structured data: {structured_hint or '（无）'}\n\n"
            "Propose ONE replacement step that avoids this failure. Priority: "
            "① 若存在结构化数据源，优先 content_summary 引用 [结构化财务数据]；"
            "② 换 URL/换查询词/换实现（仅当失败属瞬时可恢复）；"
            "③ 最后才从模型知识直接生成，且指令必须注明'基于模型知识，未在本次检索中验证'。"
            'Output ONLY this JSON: {"steps":[{"step_id":"alt","capability":"...","instruction":"...","timeout":120}]}'
        )
        try:
            from prompt_registry import get_prompt
            raw = self._plan_llm.call(
                get_prompt("planner", PLANNER_SYSTEM, goal=goal),
                prompt, expect_json=True, max_tokens=8192,
                usage="plan",
            )
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
            from prompt_registry import get_prompt
            raw = self._plan_llm.call(
                get_prompt("planner", PLANNER_SYSTEM, goal=goal),
                prompt, expect_json=True, max_tokens=8192,
                usage="plan",
            )
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
        # 步骤信封：为每个 Worker 补齐 角色/受众/输出要求/质量标准
        # （反思重做、重规划步骤同样经过本单点，保证提示词一致性）
        try:
            from step_envelope import build_envelope
            instruction = str(instruction) + build_envelope(
                capability,
                (getattr(self, "_task_goals", {}) or {}).get(task_id, ""),
                (getattr(self, "_task_prompt_hints", {}) or {}).get(task_id),
            )
        except Exception as exc:
            logger.warning("step envelope failed: %s", str(exc)[:100])
        step_id = step.get("step_id", uuid.uuid4().hex[:8])
        # LLM 生成/运行较慢：普通步骤下限 300s，code_execution（含生成-修复循环）下限 600s；
        # 默认超时取 system.task_timeout（C3 热重载后对下一任务生效）
        task_timeout = int(getattr(self, "_task_timeout", 300) or 300)
        timeout = max(int(step.get("timeout", task_timeout) or task_timeout), 300)
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

    def _cleanup_project_workspace(self, task_id: str, project: str | None = None) -> None:
        """任务开始时清空本任务的成果目录（project/reports/data/charts），
        保证"一次运行 = 一个干净文件夹"；每任务独立，不影响其他任务。"""
        import shutil
        for sub in ("project", "reports", "data", "charts"):
            d = task_workspace(task_id, project) / sub
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
        """目标是否明确要求可视化/图表（只有此时才生成检索数据图表）。
        P1-3：加密/宏观类目标（币价/行情/走势/宏观/利率/涨跌幅/市值/竞争格局/排名）
        自动配图，避免"评估比特币短期趋势与风险"不命中可视化而 0 张图。"""
        g = str(goal or "").lower()
        return any(k in g for k in (
            "可视化", "图表", "趋势图", "柱状", "饼图", "折线", "调研",
            "plot", "chart", "graph",
            # 金融类目标也自动配图（财报要点 → 指标表 → 图表）
            "财报", "营收", "净利润", "财务", "季报", "年报", "业绩",
            # 市场数据类目标（规模/份额/占比/销量/渗透率/增长率）
            "市场规模", "市场份额", "占比", "销售", "销量", "出货",
            "渗透率", "增长率", "cagr", "预测", "规模",
            # P1-3：加密/宏观类目标（行情/走势/趋势与风险/宏观指标/排名格局）
            "币价", "比特币", "以太坊", "加密货币", "行情", "走势", "趋势",
            "宏观", "通胀", "利率", "涨跌幅", "市值", "份额", "竞争格局",
            "排名", "排行", "前十", "榜单", "成交量", "成交额", "涨停",
            "跌幅", "美联储", "cpi", "gdp",
        ))

    @staticmethod
    def _extract_chart_data(text: str) -> list[dict]:
        """从 content_summary 结果中解析 [CHART_DATA] JSON 图表规格；
        兼容旧格式扁平 data 行（自动打包为规格）。"""
        if not text:
            return []
        idx = text.find("[CHART_DATA]")
        if idx < 0:
            return []
        seg = text[idx + len("[CHART_DATA]"):]
        i = seg.find("{")
        if i < 0:
            return []
        depth = 0
        for j in range(i, len(seg)):
            if seg[j] == "{":
                depth += 1
            elif seg[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(seg[i:j + 1])
                        specs = data.get("charts")
                        if specs is None:
                            rows = data.get("data") or []
                            from chart_specs import wrap_rows_to_specs
                            specs = wrap_rows_to_specs([r for r in rows if isinstance(r, dict)])
                        return [s for s in specs if isinstance(s, dict)] if specs else []
                    except Exception:
                        break
        return []

    @staticmethod
    def _extract_chart_rows_from_table(text: str) -> list[dict]:
        """兜底：LLM 未输出 [CHART_DATA] 时，从摘要中的 Markdown 表格解析图表数据行。
        按列语义提取：数值列必须有单位；"指标"列逐行取值；年份/口径/来源按表头定位；
        无具体数值（如"数千亿"）、无单位的行直接丢弃。"""
        if not text:
            return []
        rows: list[dict] = []
        for tbl_no, tbl in enumerate(re.findall(r"(\|.+\|(?:\n\|.+\|)+)", text)):
            lines = tbl.strip().split("\n")
            if len(lines) < 3:
                continue
            headers = [c.strip() for c in lines[0].strip("|").split("|")]
            hmap = {h: i for i, h in enumerate(headers)}
            # 数值列定位：精确表头优先，其次按含 数值/规模/份额/增速/金额/营收 的表头
            val_idx = next(
                (hmap[k] for k in ("数值", "规模", "金额", "份额", "增速", "营收")
                 if k in hmap),
                None,
            )
            if val_idx is None:
                for i, h in enumerate(headers):
                    if any(k in h for k in ("数值", "规模", "份额", "增速", "金额", "营收")):
                        val_idx = i
                        break
            if val_idx is None:
                continue  # 无数值列 → 时间线/政策类表格不画图
            metric_idx = next(
                (hmap[k] for k in ("指标", "指标/年份", "指标名称") if k in hmap),
                None,
            )
            year_idx = next(
                (hmap[k] for k in ("年份", "时间", "年度") if k in hmap),
                None,
            )
            src_idx = next(
                (hmap[k] for k in ("来源", "来源链接", "出处", "链接") if k in hmap),
                None,
            )
            cal_idx = next(
                (hmap[k] for k in ("口径", "口径说明", "口径/年份", "统计口径", "口径范围", "备注")
                 if k in hmap),
                None,
            )
            # 单位可能在表头括号里（如"市场规模（亿美元）"）
            header_unit = ""
            if val_idx is not None and val_idx < len(headers):
                mu = re.search(r"[（(]([^）)]*)[）)]", headers[val_idx])
                if mu:
                    cand = mu.group(1).strip()
                    if any(k in cand for k in (
                        "美元", "亿元", "万亿", "亿元", "%", "万辆", "万台", "元", "欧元", "人民币"
                    )):
                        header_unit = cand
            # 占比/份额列 → 额外生成市场份额行（适合饼图）
            share_idx = next(
                (i for i, h in enumerate(headers)
                 if i != val_idx and any(k in h for k in ("占比", "份额"))),
                None,
            )
            for line in lines[2:]:
                if "---" in line:
                    continue
                cells = [c.strip() for c in line.strip("|").split("|")]
                if val_idx >= len(cells):
                    continue
                joined = " ".join(cells)
                val_cell_clean = cells[val_idx].replace(",", "").replace("，", "")
                # 只从数值单元格解析"数字+单位"，禁止从整行拼接文本里抓其他数字
                # （如占比列的 52%），否则 labels 与 values 会错位。单元格可能只含
                # 数字（单位在表头括号里），此时回退 header_unit。
                num_m = re.search(
                    r"(\d[\d]*(?:\.\d+)?)\s*(万亿|千亿|百亿|亿|万)?\s*(亿美元|亿元|%|万辆|万台|美元)?",
                    val_cell_clean,
                )
                if not num_m:
                    continue
                # "数千亿/数百亿" 这类无具体数字的值不画图
                if not num_m.group(1):
                    continue
                cell_unit = (
                    (num_m.group(2) or "") + (num_m.group(3) or "")
                    if num_m.group(2) and num_m.group(3) in ("美元", "元", "人民币")
                    else (num_m.group(3) or "")
                )
                year = None
                if year_idx is not None and year_idx < len(cells):
                    ym = re.search(r"(20\d{2})", cells[year_idx])
                    if ym:
                        year = int(ym.group(1))
                if year is None:
                    ym = re.search(r"(20\d{2})", joined)
                    if ym:
                        year = int(ym.group(1))
                src = ""
                if src_idx is not None and src_idx < len(cells):
                    u = re.search(r"https?://[^\s\)\]]+", cells[src_idx])
                    src = u.group(0) if u else cells[src_idx][:40]
                if not src:
                    for c in cells:
                        u = re.search(r"https?://[^\s\)\]]+", c)
                        if u:
                            src = u.group(0)
                            break
                unit = cell_unit
                if not unit and header_unit:
                    unit = header_unit
                if not unit:
                    continue  # 无单位 → 规范要求必须有单位，不画图
                metric = "指标"
                if metric_idx is not None and metric_idx < len(cells):
                    m = cells[metric_idx].strip()
                    if m and m not in ("—", "-"):
                        metric = m[:30]
                else:
                    metric = headers[val_idx] if val_idx < len(headers) else "指标"
                    if any(k in metric for k in ("规模", "市场")):
                        metric = "市场规模"
                    elif any(k in metric for k in ("份额", "占比")):
                        metric = "市场份额"
                    elif any(k in metric for k in ("增速", "增长", "复合")):
                        metric = "增速"
                caliber = ""
                if cal_idx is not None and cal_idx < len(cells):
                    caliber = cells[cal_idx][:30]
                if not caliber:
                    for c in cells:
                        if not c or c in ("—", "-") or c == metric or c == cells[val_idx]:
                            continue
                        if re.fullmatch(r"[\d.]+(?:万亿|千亿|百亿|亿|万)?(?:美元|元|%|辆|台)?", c):
                            continue
                        if src and c == src:
                            continue
                        caliber = c[:30]
                        break
                rows.append({
                    "指标": metric,
                    "年份": year,
                    "数值": float(num_m.group(1)),
                    "单位": unit,
                    "口径": caliber or "表格",
                    "来源": src,
                    "_tbl": f"t{tbl_no}",
                })
                # 占比/份额列：提取为市场份额行（单位 %，适合饼图）
                if share_idx is not None and share_idx < len(cells):
                    sm = re.search(r"(\d+(?:\.\d+)?)\s*(%)?", cells[share_idx])
                    if sm:
                        rows.append({
                            "指标": "市场份额",
                            "年份": year,
                            "数值": float(sm.group(1)),
                            "单位": "%",
                            "口径": (cells[0] if cells and cells[0] and cells[0] not in headers
                                     else (metric[:20] or "占比")),
                            "来源": src,
                            "_tbl": f"t{tbl_no}",
                        })
        return rows

    @staticmethod
    def _filter_chart_rows(rows: list[dict], goal: str) -> list[dict]:
        """主题过滤：剔除与核心主题无关的数值（人形机器人/SoC/投资等），
        并要求指标/口径与目标核心对象相关（如目标含"芯片"则须含芯片/AI/算力等）。"""
        if not rows:
            return rows
        excluded = (
            "人形机器人", "机器人", "soc", "汽车", "手机", "白宫",
            "dram", "pcb", "oled", "投资", "财报", "具身智能", "蓝牙",
            "显示器", "面板",
        )
        kept = []
        for r in rows:
            text = (
                str(r.get("指标", "")) + " " + str(r.get("口径", ""))
                + " " + str(r.get("来源", ""))
            ).lower()
            if any(k in text for k in excluded):
                continue
            kept.append(r)
        return kept

    @staticmethod
    def _excluded_for(goal: str) -> tuple[str, ...]:
        """与目标主题无关的领域词；若目标本身就在讨论该领域（如"新能源汽车"、
        "特斯拉财报"），则对应词不排除，避免误杀。"""
        excluded = (
            "人形机器人", "机器人", "soc", "汽车", "手机", "白宫",
            "dram", "pcb", "oled", "投资", "财报", "具身智能", "蓝牙",
            "显示器", "面板",
        )
        g = str(goal or "").lower()
        return tuple(k for k in excluded if k not in g)

    @staticmethod
    def _goal_core(goal: str) -> list[str]:
        """从目标中提取核心主题词（2 字中文双字组/英文技术词），用于正向
        相关性校验：规格文本（标题/问题/结论/数据行）至少命中一个核心词。"""
        g = str(goal or "").lower()
        generic = (
            "市场", "报告", "调研", "分析", "全球", "中国", "国内", "国际",
            "可视化", "生成", "最新", "现状", "趋势", "规模", "份额", "数据",
            "行业", "情况", "请分", "进行", "梳理", "汇总", "总结", "要点",
            "评估", "方案", "项目", "产品", "技术", "领域", "相关", "以及",
            "我们", "可以", "需要", "完成", "输出", "一份", "文档", "内容",
            "差异", "明确", "要求", "必须", "时间", "方面", "主要", "官方",
            "权威", "机构", "经济", "整理", "以及", "以及", "请调", "研并",
            "并总", "年至", "年间", "球主", "要经", "济体", "体在", "工智",
            "能算", "力基", "础设", "施方", "资规", "心技", "术路", "线差",
            "异及", "及相", "关的", "策法", "求数", "据必", "须附", "附带",
            "带明", "确的", "的官", "方或", "或权", "威机", "构出", "并按",
            "按时", "间线", "线整", "整理", "2025", "2026", "20", "25", "26",
        )
        cands: set[str] = set()
        for m in re.findall(r"[\u4e00-\u9fff]{2,4}", g):
            for i in range(len(m) - 1):
                bg = m[i:i + 2]
                if bg not in generic:
                    cands.add(bg)
        for m in re.findall(r"[a-z]{2,8}", g):
            if m in ("ai", "gpu", "soc", "ev", "llm", "iot", "saas", "b2b", "b2c", "erp", "crm", "cpu"):
                cands.add(m)
        excluded = set(OrchestratorV2._excluded_for(goal))
        # 排序保证跨进程确定性（集合迭代顺序受 hash 随机化影响）；
        # 全量返回而非截断——真正的主题词可能被截掉导致整图误删。
        # 噪声双字组（如"请分"）不会命中规格文本，保留无害。
        core = sorted(
            (c for c in cands if c not in excluded and not any(e in c for e in excluded)),
            key=lambda c: (-len(c), c),
        )
        return core

    @classmethod
    def _filter_chart_specs(cls, specs: list[dict], goal: str) -> list[dict]:
        """主题过滤（图表规格版）：剔除与核心主题无关的图；
        规格中混入无关领域的数据行（人形机器人/SoC/投资等）逐行剔除，
        整图无关（标题/问题/结论即偏离主题）或数据行被清空则整图丢弃。"""
        if not specs:
            return specs
        excluded = cls._excluded_for(goal)
        core = cls._goal_core(goal)
        kept: list[dict] = []
        for s in specs:
            if not isinstance(s, dict):
                continue
            title_q = (
                str(s.get("title", "")) + " " + str(s.get("question", ""))
                + " " + str(s.get("conclusion", ""))
            ).lower()
            if any(k in title_q for k in excluded):
                continue
            rows = [r for r in s.get("data") or [] if isinstance(r, dict)]
            rows_kept = []
            for r in rows:
                text = (
                    str(r.get("label", "")) + " " + str(r.get("caliber", ""))
                    + " " + str(r.get("source", ""))
                ).lower()
                if any(k in text for k in excluded):
                    continue
                rows_kept.append(r)
            if not rows_kept:
                continue
            core_text = (
                str(s.get("title", "")) + " " + str(s.get("question", ""))
                + " " + " ".join(
                str(r.get("label", "")) + " " + str(r.get("caliber", ""))
                for r in rows_kept
                )
            ).lower()
            # 结论字段不参与主题匹配：兜底规格的结论模板词（如"差异显著"）
            # 可能误中目标里的通用词（如"技术路线差异"），导致离题图被放行
            if core and not any(k in core_text for k in core):
                continue
            s = dict(s)
            s["data"] = rows_kept
            kept.append(s)
        return kept

    @staticmethod
    def _clean_rows_to_specs(clean: dict) -> list[dict]:
        """数据驱动兜底：把 clean_chart_data 的扁平行（英文键）转换为
        wrap_rows_to_specs 可消费行；crypto 行情点（价格/市值/24h量/涨跌幅）
        归入同一指标组，≥2 个可作图数据点即可成图。"""
        from chart_specs import wrap_rows_to_specs

        type_metric = {
            "market_size": "市场规模",
            "market_share": "市场份额",
            "market_trends": "市场趋势",
            "macro_indicators": "宏观指标",
            "entity_frequency": "主体提及频率",
            "source_distribution": "来源分布",
        }
        # P1-3：FRED series → 中文指标名，保证"利率/CPI/失业率"目标能命中主题过滤
        fred_metric = {
            "GDP": "美国GDP",
            "CPIAUCSL": "美国CPI",
            "UNRATE": "美国失业率",
            "DFF": "联邦基金利率",
        }
        rows: list[dict] = []
        for key in ("market_data", "market_trends", "macro_indicators", "market_share"):
            for r in clean.get(key) or []:
                if not isinstance(r, dict):
                    continue
                try:
                    value = float(r.get("value"))
                except (TypeError, ValueError):
                    continue
                label = str(r.get("label") or "?")
                source = str(r.get("source") or "")
                series = str(r.get("series") or "")
                if series:
                    metric = series
                elif source == "coingecko":
                    metric = "加密货币行情"
                elif source == "fred":
                    tok = label.split(" ", 1)[0] if " " in label else label
                    metric = fred_metric.get(tok, tok)
                else:
                    metric = type_metric.get(str(r.get("type") or ""), label)
                year = r.get("year")
                if year is None:
                    m = re.search(r"(20\d{2})", label)
                    if m:
                        year = int(m.group(1))
                rows.append({
                    "指标": metric,
                    "数值": value,
                    "单位": str(r.get("unit") or ""),
                    "年份": year,
                    "口径": label,
                    "来源": source or "检索资料",
                })
        specs = wrap_rows_to_specs(rows)
        # 排行量价散点：最新价(x) × 成交量/成交额(y)，让"排行前十"任务
        # 除了 top10 条形图还有量价关系图
        specs.extend(OrchestratorV2._ranking_volume_price_scatter(clean))
        return specs

    @staticmethod
    def _ranking_volume_price_scatter(clean: dict) -> list[dict]:
        """排行来源量价散点规格（≥5 只股票才成图，兼容 eastmoney/tencent）。

        从 market_data 行按 label 还原同一股票的 最新价/成交量/成交额：
        成交量口径取 万手，成交额口径取 亿元；x=最新价，y=量或额。
        返回 [] 表示数据不足或非排行来源。
        """
        try:
            points: dict[str, dict] = {}
            series = ""
            has_volume = False
            has_amount = False
            src_label = "东方财富行情中心实时排行"
            region = "A股"
            for r in clean.get("market_data") or []:
                row_source = str(r.get("source") or "")
                if row_source not in _RANKING_SOURCES:
                    continue
                if row_source == "tencent_us_ranking":
                    src_label = "腾讯行情实时排行（美股）"
                    region = "美股"
                elif row_source == "tencent_ranking":
                    src_label = "腾讯行情实时排行"
                elif row_source == "sina_ranking":
                    src_label = "新浪行情中心实时排行（全市场）"
                if "美股" in str(r.get("series") or ""):
                    region = "美股"
                label = str(r.get("label") or "")
                unit = str(r.get("unit") or "")
                if not series:
                    series = str(r.get("series") or "")
                if unit == "元" and label.endswith("最新价"):
                    name = label[:-3]
                    points.setdefault(name, {})["price"] = r.get("value")
                elif unit == "万手":
                    points.setdefault(label, {})["volume"] = r.get("value")
                    has_volume = True
                elif unit == "亿元":
                    points.setdefault(label, {})["amount"] = r.get("value")
                    has_amount = True
            use_amount = has_amount and not has_volume
            y_key = "amount" if use_amount else "volume"
            y_label = "成交额（亿元）" if use_amount else "成交量（万手）"
            pts = [
                {"name": name, "price": p.get("price"), "y": p.get(y_key)}
                for name, p in points.items()
                if p.get("price") is not None and p.get(y_key) is not None
            ]
            if len(pts) < 5:
                return []
            title_series = str(series or f"{region}行情排行")
            for suffix in ("最新价", "涨跌幅"):
                title_series = title_series.replace(suffix, "")
            return [{
                "question": f"{title_series}中最新价与成交量/成交额的关系如何？",
                "conclusion": (
                    f"{len(pts)} 只个股的量价分布见散点，极端值已在图内保留"
                ),
                "type": "scatter",
                "title": f"{title_series}量价散点（{y_label} vs 最新价）",
                "x_axis_title": "最新价（元）",
                "y_axis_title": y_label,
                "unit": "元/万手" if not use_amount else "元/亿元",
                "time_range": "实时",
                "region": region,
                "source": src_label,
                "sample_size": str(len(pts)),
                "annotation": f"数据来自{src_label}，口径见各数据行",
                "missing": "无",
                "outliers": "极端值已在图内保留",
                "data": [
                    {
                        "label": p["name"],
                        "value": p["y"],
                        "year": p["price"],  # render_scatter 用 year 作为数值 x 轴
                        "caliber": p["name"],
                        "source": src_label,
                    }
                    for p in pts
                ],
            }]
        except Exception as exc:
            logger.warning("ranking scatter spec failed: %s", str(exc)[:120])
            return []

    def _render_clean_chart_data(self, task_id: str, goal: str) -> None:
        """数据驱动兜底（P1-3）：clean_chart_data 中 ≥2 个可作图数据点即渲染；
        若 market_data 行不足 2 条，先从 structured_data.json 补齐 crypto/macro
        行情点再转可作图行。LLM 规格缺失时保证加密/宏观任务仍有图表。"""
        if not self._wants_visualization(goal):
            return
        project = task_project_dir(task_id)
        clean_path = project / "clean_chart_data.json"
        if not clean_path.exists():
            return
        try:
            clean = json.loads(clean_path.read_text(encoding="utf-8"))
        except Exception:
            return
        row_count = sum(
            len(clean.get(key) or []) for key in (
                "market_data", "market_trends", "macro_indicators", "market_share",
            )
        )
        if row_count < 2:
            # 清洗/回灌可能覆盖了结构化行 → 从 structured_data.json 补齐
            self._remerge_structured_points(task_id)
            try:
                clean = json.loads(clean_path.read_text(encoding="utf-8"))
            except Exception:
                return
        specs = self._filter_chart_specs(self._clean_rows_to_specs(clean), goal)
        if not specs:
            return
        try:
            from chart_specs import validate_specs
            valid, _issues = validate_specs(specs)
        except Exception:
            return
        if not valid:
            return
        chart_path = project / "chart_data.json"
        existing: list[dict] = []
        if chart_path.exists():
            try:
                existing = json.loads(
                    chart_path.read_text(encoding="utf-8")
                ).get("charts") or []
            except Exception:
                existing = []
        seen_titles = {str(s.get("title") or "") for s in existing}
        merged = list(existing) + [
            s for s in valid if str(s.get("title") or "") not in seen_titles
        ]
        chart_path.write_text(
            json.dumps({"charts": merged}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        self._render_chart_data(task_id, goal)

    def _render_chart_data(self, task_id: str, goal: str) -> None:
        """确定性渲染 LLM 结构化图表规格（chart_data.json → {"charts": [...]}）：
        语义（问题/结论/口径）由 LLM 负责；数字、标注、视觉编码由脚本保证。
        无效规格跳过并记录原因；有效图输出 chart_N.png + chart_manifest.json。"""
        import subprocess
        import sys
        if not self._wants_visualization(goal):
            return
        project = task_project_dir(task_id)
        src = project / "chart_data.json"
        if not src.exists():
            return
        repo_root = os.path.dirname(os.path.abspath(__file__))
        script = r'''# -*- coding: utf-8 -*-
"""确定性渲染：读取 chart_data.json（{"charts": [规格]}），按 5 类图表规范绘制。
语义（问题/结论/口径）由 LLM 负责；数字、标注、视觉编码由本脚本保证。
无效规格跳过并打印原因；有效图输出 chart_N.png 并写 chart_manifest.json。"""
import json
import re
import sys

sys.path.insert(0, r"__REPO_ROOT__")
from chart_specs import COLOR_BLIND_PALETTE, validate_spec, wrap_rows_to_specs

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from chart_fonts import configure_zh_font
configure_zh_font()

CURATED = (
    "规模", "趋势", "份额", "占比", "营收", "收入", "增速", "增长",
    "成本", "价格", "预测", "玩家", "厂商", "格局", "对比", "分布",
    "技术", "市场", "出货", "渗透", "渗透率", "出货量",
)


def norm(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_natural_order(rows):
    """类别标签全部为年份/纯数字 → 保持自然顺序，不做降序。"""
    labels = [str(r.get("label") or "").strip() for r in rows]
    if not labels:
        return False
    return all(re.fullmatch(r"\d{4}|\d+(\.\d+)?", l) for l in labels)


def series_key(r, i):
    return str(r.get("caliber") or r.get("source") or f"系列{i + 1}")[:20]


def short_label(r):
    """类别标签清洗：LLM 有时把指标描述/整句当作 label（如
    "AI芯片占全球芯片市场11%，全球芯片市场5760亿美元"），导致 x 轴标签错乱。
    优先取 口径 或 label 中首个短片段（机构/年份/地区），否则取来源域名。"""
    label = str(r.get("label") or "").strip() or "?"
    if len(label) <= 12:
        return label
    for cand in (str(r.get("caliber") or "").strip(), label):
        for sep in ("，", ",", "；", ";", "：", ":", "（", "("):
            head = cand.split(sep)[0].strip()
            if head and len(head) <= 12:
                return head
        if len(cand) <= 12 and cand:
            return cand
    src = str(r.get("source") or "")
    m = re.search(r"https?://([^/]+)", src)
    if m:
        return m.group(1).replace("www.", "")[:12]
    return label[:12]


def footer_lines(spec, rows, conclusion):
    source = str(spec.get("source") or "").strip()
    if not source:
        srcs = [str(r.get("source") or "") for r in rows if r.get("source")]
        source = "；".join(dict.fromkeys(srcs))[:260]
    tr = str(spec.get("time_range") or "时间未标注")
    rg = str(spec.get("region") or "地域未标注")
    n = str(spec.get("sample_size") or len(rows))
    miss = str(spec.get("missing") or "无").strip() or "无"
    out = str(spec.get("outliers") or "无").strip() or "无"
    lines = []
    if conclusion:
        lines.append("结论：" + conclusion[:140])
    lines.append(f"数据来源：{source or '未标注'}　时间：{tr}　地域：{rg}　样本量：n={n}")
    if miss not in ("无", ""):
        lines.append("缺失：" + miss[:80])
    if out not in ("无", ""):
        lines.append("异常：" + out[:80])
    lines.append("数据未经审计，仅供参考；不同口径数据未合并。")
    return "\n".join(lines)


def add_footer(fig, spec, rows, conclusion):
    fig.text(0.01, 0.004, footer_lines(spec, rows, conclusion),
             fontsize=7.5, color="#444444", va="bottom", ha="left",
             wrap=True)


def keywords_for(spec):
    text = " ".join(str(spec.get(k) or "") for k in ("title", "question", "conclusion"))
    kw = [k for k in CURATED if k in text]
    # 2-gram 滑动切片已移除（曾产生 "年全/片市/场规" 式碎片关键词）
    return list(dict.fromkeys(kw))[:14]


def render_bar(ax, spec, rows, horizontal):
    items = [(short_label(r), norm(r.get("value"))) for r in rows]
    items = [(l, v) for l, v in items if v is not None]
    if not items:
        return False
    if not is_natural_order(rows):
        items.sort(key=lambda t: t[1], reverse=True)
    labels = [l for l, _ in items]
    vals = [v for _, v in items]
    colors = [COLOR_BLIND_PALETTE[i % len(COLOR_BLIND_PALETTE)] for i in range(len(labels))]
    if horizontal:
        ax.barh(labels, vals, color=colors, edgecolor="white")
        ax.set_xlim(left=0)
    else:
        ax.bar(labels, vals, color=colors, edgecolor="white")
        ax.set_ylim(bottom=0)
    ax.set_xlabel(str(spec.get("x_axis_title") or "类别"))
    ax.set_ylabel(str(spec.get("y_axis_title") or "数值"))
    top5 = set(sorted(vals, reverse=True)[:5]) if len(vals) > 12 else set()
    for i, v in enumerate(vals):
        if len(vals) <= 12 or v in top5:
            if horizontal:
                ax.text(v, i, f"{v:g}", va="center", ha="left", fontsize=8)
            else:
                ax.text(i, v, f"{v:g}", ha="center", va="bottom", fontsize=8)
    ax.tick_params(axis="x", rotation=28 if not horizontal else 0)
    return True


def render_line(ax, spec, rows):
    years = [norm(r.get("year")) for r in rows]
    has_years = (len(years) >= 2 and all(y is not None for y in years)
                 and len({y for y in years}) >= 2)
    if has_years:
        by = {}
        for i, r in enumerate(rows):
            by.setdefault(series_key(r, i), []).append((norm(r.get("year")), norm(r.get("value"))))
        plotted = 0
        for ci, (name, pts) in enumerate(by.items()):
            pts = sorted(p for p in pts if p[1] is not None)
            if not pts:
                continue
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            ax.plot(xs, ys, marker="o", linewidth=2,
                    color=COLOR_BLIND_PALETTE[ci % len(COLOR_BLIND_PALETTE)], label=name)
            if len(ys) <= 12:
                for x, y in zip(xs, ys):
                    ax.text(x, y, f"{y:g}", ha="center", va="bottom", fontsize=7.5)
            plotted += 1
        if plotted > 1:
            ax.legend(fontsize=8, frameon=False)
        ax.set_xlabel(str(spec.get("x_axis_title") or "年份"))
    else:
        labels = [short_label(r) for r in rows]
        vals = [norm(r.get("value")) for r in rows]
        xs = list(range(len(vals)))
        ax.plot(xs, vals, marker="o", linewidth=2, color=COLOR_BLIND_PALETTE[0])
        ax.set_xticks(xs)
        ax.set_xticklabels(labels, rotation=25, fontsize=8)
        ax.set_xlabel(str(spec.get("x_axis_title") or "类别/年份"))
    ax.set_ylabel(str(spec.get("y_axis_title") or "数值"))
    ax.grid(alpha=0.25)
    return True


def render_pie(ax, spec, rows):
    items = [(short_label(r), norm(r.get("value"))) for r in rows]
    items = [(l, v) for l, v in items if v is not None]
    if not items or len(items) > 5:
        return False
    labels = [l for l, _ in items]
    vals = [v for _, v in items]
    total = sum(vals)
    if total <= 0:
        return False
    unit = str(spec.get("unit") or "")
    is_pct = unit == "%" or all(v <= 100 for v in vals)
    if is_pct and not (95 <= total <= 105):
        print(f"SKIP pie: 占比数据加和 {total:.1f}% 不为 100%，饼图会误导", flush=True)
        return False
    wedges, _ = ax.pie(
        vals, labels=labels, colors=COLOR_BLIND_PALETTE[:len(labels)],
        startangle=90, counterclock=False,
        wedgeprops={"edgecolor": "white", "linewidth": 1},
    )
    ax.axis("equal")
    # 数据标注：直接用原值（占比数据即原百分比，禁止重算占比导致图与数据不符）
    for w, v in zip(wedges, vals):
        ang = (w.theta2 - w.theta1) / 2.0 + w.theta1
        x = 0.70 * np.cos(np.deg2rad(ang))
        y = 0.70 * np.sin(np.deg2rad(ang))
        if is_pct:
            ax.text(x, y, f"{v:g}%", ha="center", va="center",
                    fontsize=9, color="white", weight="bold")
        else:
            share = 100.0 * v / total
            ax.text(x, y, f"{v:g}{unit}\n{share:.1f}%", ha="center", va="center",
                    fontsize=8, color="white", weight="bold")
    return True


def render_scatter(ax, spec, rows):
    xs = [norm(r.get("year")) for r in rows]
    ys = [norm(r.get("value")) for r in rows]
    if not any(x is not None and y is not None for x, y in zip(xs, ys)):
        # 无年份 → 以序号为横轴，标签做刻度
        labels = [short_label(r) for r in rows]
        xs = list(range(len(rows)))
        ax.scatter(xs, ys, s=48, color=COLOR_BLIND_PALETTE[0], edgecolor="white")
        ax.set_xticks(xs)
        ax.set_xticklabels(labels, rotation=25, fontsize=8)
        ax.set_xlabel(str(spec.get("x_axis_title") or "类别/序号"))
    else:
        ax.scatter(xs, ys, s=48, color=COLOR_BLIND_PALETTE[0], edgecolor="white")
        ax.set_xlabel(str(spec.get("x_axis_title") or "年份/序号"))
    ax.set_ylabel(str(spec.get("y_axis_title") or "数值"))
    ax.grid(alpha=0.25)
    return True


def main():
    with open("chart_data.json", encoding="utf-8") as f:
        payload = json.load(f)
    specs = payload.get("charts")
    if specs is None:
        rows = payload.get("data") or []
        specs = wrap_rows_to_specs([r for r in rows if isinstance(r, dict)])
    specs = [s for s in specs if isinstance(s, dict)]
    manifest = []
    idx = 0
    for i, spec in enumerate(specs):
        issues = validate_spec(spec)
        if issues:
            print(f"SKIP chart#{i}: {'; '.join(issues)}", flush=True)
            continue
        rows = [r for r in spec.get("data") or [] if isinstance(r, dict)]
        ctype = str(spec.get("type") or "bar")
        conclusion = str(spec.get("conclusion") or "").strip()
        title = str(spec.get("title") or "").strip()
        if len(rows) < 2:
            print(f"SKIP chart#{i}: 数据点少于 2 个，单点图无结论（{title}）", flush=True)
            continue
        fig, ax = plt.subplots(figsize=(9.5, 5.6))
        fig.subplots_adjust(bottom=0.34)
        ok = False
        if ctype == "pie":
            ok = render_pie(ax, spec, rows)
        elif ctype == "horizontal_bar":
            ok = render_bar(ax, spec, rows, horizontal=True)
        elif ctype == "line":
            ok = render_line(ax, spec, rows)
        elif ctype == "scatter":
            ok = render_scatter(ax, spec, rows)
        else:
            ok = render_bar(ax, spec, rows, horizontal=False)
        if not ok:
            print(f"SKIP chart#{i}: 数据无法支撑 {ctype} 图（{title}）", flush=True)
            plt.close(fig)
            continue
        ax.set_title(title or "图表", fontsize=12, pad=10)
        add_footer(fig, spec, rows, conclusion)
        idx += 1
        fname = f"chart_{idx}.png"
        # 图表视觉质量 QA + 自动修复（无视觉 LLM 的确定性替代）：
        # 标签重叠/图例遮挡/字号过小 → 自动调整并重渲染
        from chart_qa import render_with_qa
        residual = render_with_qa(fig, ax, fname, dpi=120)
        if residual:
            print(f"QA issue {fname}: {'; '.join(r['detail'] for r in residual[:3])}", flush=True)
        plt.close(fig)
        manifest.append({
            "file": fname,
            "keywords": keywords_for(spec),
            "section_hint": str(spec.get("section_hint") or ""),
        })
        print(f"RENDERED {fname}: {title}", flush=True)
    with open("chart_manifest.json", "w", encoding="utf-8") as f:
        json.dump({"charts": manifest}, f, ensure_ascii=False, indent=1)
    print(f"total={len(manifest)} skipped={len(specs) - len(manifest)}", flush=True)


if __name__ == "__main__":
    main()
'''
        script = script.replace("__REPO_ROOT__", repo_root)
        script_path = project / "render_charts.py"
        try:
            script_path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=str(project), capture_output=True, timeout=180,
            )
            out = proc.stdout.decode("utf-8", errors="replace")
            err = proc.stderr.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                logger.warning("render_charts failed: %s", (out + "\n" + err)[:400])
            else:
                for line in out.splitlines():
                    if line.startswith("SKIP "):
                        logger.info("chart skipped: %s", line)
            # P2-4：渲染后回填 manifest——保证每张已渲染 PNG 都有 file+keywords
            # 条目（修复"实际 4 图但 chart_manifest.json 空数组"的交付缺口）
            self._backfill_chart_manifest(project)
            # 图表同步到 workspace/charts/（report_generator 从该目录发现图表并嵌入报告）
            try:
                from workspace import task_charts_dir
                cdir = task_charts_dir(task_id)
                for png in project.glob("*.png"):
                    shutil.copy2(png, cdir / png.name)
                mf = project / "chart_manifest.json"
                if mf.exists():
                    shutil.copy2(mf, cdir / mf.name)
            except Exception as exc:
                logger.warning("chart sync failed: %s", str(exc)[:120])
        except Exception as exc:
            logger.warning("render_charts error: %s", exc)

    @staticmethod
    def _backfill_chart_manifest(project) -> None:
        """P2-4：图表渲染后回填 chart_manifest.json。

        只补充缺失条目（file+keywords+section_hint），不覆盖渲染器已写出的
        正确条目；manifest 与磁盘上已渲染 PNG 一一对应。
        """
        mf = project / "chart_manifest.json"
        try:
            charts = (
                json.loads(mf.read_text(encoding="utf-8")).get("charts") or []
                if mf.exists() else []
            )
            charts = [c for c in charts if isinstance(c, dict)]
        except Exception:
            charts = []
        existing = {str(c.get("file") or "") for c in charts}
        pngs = sorted(p.name for p in project.glob("chart_*.png"))
        missing = [n for n in pngs if n not in existing]
        if not missing:
            return
        # 按渲染脚本的校验顺序重建 chart_N.png → 规格映射
        try:
            payload = json.loads(
                (project / "chart_data.json").read_text(encoding="utf-8")
            )
            specs = [s for s in (payload.get("charts") or []) if isinstance(s, dict)]
        except Exception:
            specs = []
        try:
            from chart_specs import validate_spec
        except Exception:
            validate_spec = None
        seq: dict[str, dict] = {}
        idx = 0
        for spec in specs:
            if validate_spec is not None and validate_spec(spec):
                continue
            rows = [r for r in (spec.get("data") or []) if isinstance(r, dict)]
            if len(rows) < 2:
                continue
            ctype = str(spec.get("type") or "bar")
            if ctype == "pie":
                vals = []
                for r in rows:
                    try:
                        vals.append(float(r.get("value")))
                    except (TypeError, ValueError):
                        pass
                unit = str(spec.get("unit") or "")
                is_pct = unit == "%" or (bool(vals) and all(v <= 100 for v in vals))
                if vals and is_pct and not (95 <= sum(vals) <= 105):
                    continue
            idx += 1
            seq[f"chart_{idx}.png"] = spec
        curated = (
            "规模", "趋势", "份额", "占比", "营收", "收入", "增速", "增长",
            "成本", "价格", "预测", "玩家", "厂商", "格局", "对比", "分布",
            "技术", "市场", "出货", "渗透", "渗透率", "出货量",
        )
        added = False
        for name in missing:
            spec = seq.get(name)
            if not spec:
                continue
            text = " ".join(
                str(spec.get(k) or "") for k in ("title", "question", "conclusion")
            )
            keywords = list(dict.fromkeys(k for k in curated if k in text))[:14]
            if not keywords:
                continue
            charts.append({
                "file": name,
                "keywords": keywords,
                "section_hint": str(spec.get("section_hint") or ""),
            })
            added = True
        if added:
            try:
                mf.write_text(
                    json.dumps({"charts": charts}, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
                logger.info(
                    "chart_manifest backfilled: %d missing entries added for %s",
                    len(missing), project,
                )
            except Exception as exc:
                logger.warning("chart_manifest backfill failed: %s", str(exc)[:120])

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
        正文里的绝对路径（如"成果文件夹"）保持不变。

        P2-4：工作区改写后，剩余的图片链接若仍为 Windows 盘符绝对路径
        （如 ![](C:\\...\\charts\\xxx.png)），进一步重写为相对路径
        charts/xxx.png，避免交付报告残留本机绝对路径。"""
        ws = str(task_workspace(task_id))
        ws_bs = ws.replace("/", "\\")
        seg = f"/files/{task_id}"

        def _fix_target(m):
            t = m.group(2).replace(ws_bs, seg).replace(ws, seg).replace("\\", "/")
            return m.group(1) + t + m.group(3)

        report = re.sub(r"(\]\()([^)\s]+)(\))", _fix_target, report)

        def _fix_windows_image(m):
            target = m.group(2)
            if not re.match(r"^[A-Za-z]:[\\/]", target):
                return m.group(0)
            rel = re.sub(r"^[A-Za-z]:[\\/]", "", target).replace("\\", "/")
            parts = rel.split("/")
            if len(parts) >= 2 and parts[-2] in ("charts", "data", "reports", "project"):
                rel = f"{parts[-2]}/{parts[-1]}"
            else:
                rel = parts[-1]
            return f"{m.group(1)}{rel}{m.group(3)}"

        return re.sub(r"(!\[[^\]]*\]\()([^)\s]+)(\))", _fix_windows_image, report)

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
                # 剥离 LLM 常见的整体 Markdown 围栏（前端结构化显示前置处理）
                try:
                    from common import strip_outer_markdown_fence
                    cleaned = strip_outer_markdown_fence(best)
                    if cleaned != best:
                        logger.info("Report outer markdown fence stripped (%d -> %d chars)",
                                    len(best), len(cleaned))
                    best = cleaned
                except Exception:
                    pass
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
        """确定性基线图表：来源分布、主要主体提及频率、主题热词。
        语义类图表（趋势/份额/指标）由 LLM 结构化数据渲染（_render_chart_data）。"""
        import subprocess
        import sys
        import os
        if not self._wants_visualization(goal):
            return
        project = task_project_dir(task_id)
        repo_root = os.path.dirname(os.path.abspath(__file__))
        src = project / "search_results.json"
        clean_src = project / "clean_chart_data.json"
        if not src.exists():
            return
        # 数据清洗兜底：绘图只读清洗后数据（搜索→清洗→绘图）
        if not clean_src.exists():
            try:
                from clean_data import clean_file
                clean_file(src, clean_src, goal=goal)
            except Exception as exc:
                logger.warning("clean_data failed: %s", str(exc)[:100])
                return
        script = r'''# -*- coding: utf-8 -*-
"""从清洗后的 clean_chart_data.json 绘制探索性图表（搜索→清洗→绘图）。
绘图只接收结构化数据，不直接解析 search_results.json 原始文本。
被跳过的图会打印原因（SKIP），便于用户/日志了解发生了什么。"""
import json
import re
import sys
from collections import Counter
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, r"__REPO_ROOT__")
from chart_fonts import configure_zh_font
from orchestrator_v2 import _group_financial_rows
configure_zh_font()

clean = json.load(open("clean_chart_data.json", encoding="utf-8"))


def short_label(s, n=14):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def save_qa(fig, ax, name):
    """渲染 + 视觉质量 QA + 自动修复（标签重叠/图例遮挡/字号过小）。"""
    from chart_qa import render_with_qa
    residual = render_with_qa(fig, ax, name, dpi=110)
    if residual:
        print(f"QA {name}: {'; '.join(r['detail'] for r in residual[:2])}", flush=True)


NOISE_LABELS = ("·", "报告", "摘要", "分析", "统计及", " -", "—", "–")
SUBJECT_HINTS = ("芯片", "GPU", "TPU", "ASIC", "半导体", "出货量",
                 "收入", "规模", "增速", "侧", "端", "市场")


def is_noise_label(s):
    return any(p in str(s) for p in NOISE_LABELS)


def clean_rows(rows, require_type=None):
    """过滤噪音 label；market_data 只保留 type=market_size（排除 AI 整体市场）。"""
    out = []
    for r in rows or []:
        if is_noise_label(r.get("label")):
            continue
        if require_type and r.get("type") != require_type:
            continue
        out.append(r)
    return out


# 1) 主要主体提及频率（信息量大且稳定：只要有检索结果即可画）
entity_freq = Counter({k: v for k, v in clean.get("entity_frequency", {}).items()})
top_e = entity_freq.most_common(10)
if len(top_e) >= 3 and len(set(c for _, c in top_e)) > 1:
    fig, ax = plt.subplots(figsize=(10, 5))
    pairs_e = top_e[::-1]  # 条形与标签共用同一反转顺序，防止索引错位
    ax.barh([short_label(e) for e, _ in pairs_e], [c for _, c in pairs_e],
            color="#0ea5e9", edgecolor="white")
    ax.set_xlabel("提及次数")
    ax.set_title("检索资料中的主要主体提及频率（厂商/区域）")
    for i, (_, c) in enumerate(pairs_e):
        ax.text(c + 0.1, i, str(c), va="center", fontsize=9)
    save_qa(fig, ax, "entity_frequency.png")
    plt.close()
else:
    print(f"SKIP 主体提及频率图：有效实体不足或无区分度（{len(top_e)} 条）")

# 2) 数据来源分布（X=来源域名, Y=结果数）
domains = Counter({k: v for k, v in clean.get("source_distribution", {}).items()})
top = domains.most_common(8)
# 若所有来源计数相同（如每源仅 1 条）→ 无信息增量，跳过该图
if top and len(set(c for _, c in top)) > 1:
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.barh([d for d, _ in top][::-1], [c for _, c in top][::-1],
            color="#8b5cf6", edgecolor="white")
    ax.set_xlabel("结果数")
    ax.set_title("数据来源分布（检索结果）")
    save_qa(fig, ax, "source_distribution.png")
    plt.close()
else:
    print(f"SKIP 数据来源分布图：来源计数全部相同或为空（{len(top)} 个来源），无信息增量。")

# 3) 主题热词（X=热词, Y=出现次数）
words = Counter({k: v for k, v in clean.get("topic_terms", {}).items()})
top_w = words.most_common(12)
if top_w and len(set(c for _, c in top_w)) > 1:
    fig, ax = plt.subplots(figsize=(10, 5))
    pairs_w = top_w[::-1]  # 条形与标签共用同一反转顺序
    ax.barh([short_label(w) for w, _ in pairs_w], [c for _, c in pairs_w],
            color="#06b6d4", edgecolor="white")
    for i, (_, c) in enumerate(pairs_w):
        ax.text(c + 0.1, i, str(c), va="center", fontsize=9)
    ax.set_xlabel("出现次数")
    ax.set_title("检索资料主题热词")
    save_qa(fig, ax, "topic_terms.png")
    plt.close()
else:
    print(f"SKIP 主题热词图：热词不足或无区分度（{len(top_w)} 条）")

# 4) 财务/市场规模（结构化源）：优先"年份 × 指标"面板数据 → 每指标一张折线子图，
#    避免把 12 年 × 6-8 个指标塞进单张柱状图（70+ 柱子不可读）。
#    财务行（营收/净利…）可能被标为 ai_overall，按"带年份"识别，不再按 type 过滤。
#    多实体标签（"宁德时代2024年营收"）先归一化出指标名，再按 (实体, 指标) 分组，
#    同指标不同实体各画一条折线，避免双实体同年份互相覆盖。
market = clean_rows(clean.get("market_data"))
metric_groups = {
    mname: by_entity
    for mname, by_entity in _group_financial_rows(market).items()
    if len({p[0] for pts in by_entity.values() for p in pts}) >= 3
}
if len(metric_groups) >= 2:
    names = sorted(
        metric_groups,
        key=lambda k: -sum(len(pts) for pts in metric_groups[k].values()),
    )
    fig, axes = plt.subplots(len(names), 1, figsize=(11, 3.0 * len(names)))
    if hasattr(axes, "ravel"):
        axes = axes.ravel()
    elif not isinstance(axes, (list, tuple)):
        axes = [axes]
    for ax, mname in zip(axes, names):
        by_entity = metric_groups[mname]
        colors = ("#0ea5e9", "#f59e0b", "#10b981", "#8b5cf6", "#ef4444")
        for idx, (ent, pts) in enumerate(sorted(by_entity.items())):
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            ax.plot(
                xs, ys, marker="o", linewidth=2,
                color=colors[idx % len(colors)], label=ent or None,
            )
            for x, y in zip(xs, ys):
                ax.text(x, y, f"{y:g}", ha="center", va="bottom", fontsize=7)
        ax.set_title(f"{mname}（亿元）", fontsize=10)
        ax.set_xlabel("年份")
        ax.grid(alpha=0.3)
        if len(by_entity) >= 2:
            ax.legend()
    fig.suptitle("财务指标趋势（结构化数据，亿元）", fontsize=12)
    save_qa(fig, axes[0], "financial_trends.png")
    plt.close()
elif len(market) >= 2:
    # 非年份类市场规模数据（AI 芯片市场等）→ 按单位分组柱状图；排除 AI 整体市场
    bar_market = [m for m in market if m.get("type") == "market_size"]
    by_unit = {}
    for m in bar_market:
        by_unit.setdefault(str(m.get("unit") or "?"), []).append(m)
    best = max(by_unit.values(), key=len)
    if len(best) >= 2:
        items_m = best
        # 与 bar 标签共用同一顺序，杜绝错位
        labels = [short_label(m.get("label") or "?") for m in items_m]
        vals = [float(m.get("value") or 0) for m in items_m]
        fig, ax = plt.subplots(figsize=(9, 5))
        ax.bar(labels, vals, color="#f59e0b", edgecolor="white")
        ax.set_ylim(bottom=0)
        ax.set_xlabel("细分市场/区域")
        ax.set_ylabel(f"规模（{best[0].get('unit')}）")
        ax.set_title("市场规模（清洗后结构化数据）")
        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:g}", ha="center", va="bottom", fontsize=9)
        save_qa(fig, ax, "market_data.png")
        plt.close()
    else:
        print(f"SKIP 市场规模图：按单位分组后最多的仅 {len(best)} 条（{len(market)} 条总数据）")
else:
    print(f"SKIP 市场规模图：有效结构化数据不足（当前 {len(market)} 条），无法生成对比图。")

# 5) 市场份额（%）
shares = clean_rows(clean.get("market_share"))
if len(shares) >= 2 and len(set(c.get("value") for c in shares)) > 1:
    fig, ax = plt.subplots(figsize=(9, 5))
    pairs_s = sorted(shares, key=lambda x: x.get("value", 0), reverse=True)
    labels_s = [short_label(x.get("label") or "?") for x in pairs_s]
    vals_s = [float(x.get("value") or 0) for x in pairs_s]
    ax.bar(labels_s, vals_s, color="#10b981", edgecolor="white")
    ax.set_ylim(bottom=0)
    ax.set_xlabel("主体")
    ax.set_ylabel("份额（%）")
    ax.set_title("市场份额/占比（清洗后结构化数据）")
    for i, v in enumerate(vals_s):
        ax.text(i, v, f"{v:g}%", ha="center", va="bottom", fontsize=9)
    save_qa(fig, ax, "market_share.png")
    plt.close()
else:
    print(f"SKIP 市场份额图：有效数据不足或无区分度（{len(shares)} 条）")

# 6) 宏观指标（Token/台/辆/次 等非货币单位）
macros = clean.get("macro_indicators") or []
if len(macros) >= 2 and len(set(c.get("value") for c in macros)) > 1:
    fig, ax = plt.subplots(figsize=(9, 5))
    pairs_m = sorted(macros, key=lambda x: x.get("value", 0), reverse=True)
    labels_m = [f"{short_label(x.get('label') or '?')}（{x.get('unit')}）" for x in pairs_m]
    vals_m = [float(x.get("value") or 0) for x in pairs_m]
    ax.bar(labels_m, vals_m, color="#6366f1", edgecolor="white")
    ax.set_ylim(bottom=0)
    ax.set_ylabel("数值")
    ax.set_title("宏观指标（调用量/出货量/渗透率等）")
    for i, v in enumerate(vals_m):
        ax.text(i, v, f"{v:g}", ha="center", va="bottom", fontsize=9)
    save_qa(fig, ax, "macro_indicators.png")
    plt.close()
else:
    print(f"SKIP 宏观指标图：有效数据不足或无区分度（{len(macros)} 条）")

# 7) 市场趋势（同比/环比 ±%，下降为负）；label 过短且无主体词的条目跳过
trends = [
    t for t in clean_rows(clean.get("market_trends"))
    if len(str(t.get("label") or "")) > 4 or any(h in str(t.get("label")) for h in SUBJECT_HINTS)
]
if len(trends) >= 2 and len(set(c.get("value") for c in trends)) > 1:
    fig, ax = plt.subplots(figsize=(9, 5))
    pairs_t = sorted(trends, key=lambda x: x.get("value", 0), reverse=True)
    labels_t = [short_label(x.get("label") or "?") for x in pairs_t]
    vals_t = [float(x.get("value") or 0) for x in pairs_t]
    colors_t = ["#ef4444" if v < 0 else "#10b981" for v in vals_t]
    ax.bar(labels_t, vals_t, color=colors_t, edgecolor="white")
    ax.axhline(0, color="#888888", linewidth=0.8)
    ax.set_ylabel("同比/环比变化（%）")
    ax.set_title("市场趋势（同比增长/下降）")
    for i, v in enumerate(vals_t):
        ax.text(i, v, f"{v:g}%", ha="center", va="bottom" if v >= 0 else "top", fontsize=9)
    save_qa(fig, ax, "market_trends.png")
    plt.close()
else:
    print(f"SKIP 市场趋势图：有效数据不足或无区分度（{len(trends)} 条）")

print("charts generated")
'''
        script = script.replace("__REPO_ROOT__", repo_root.replace("\\", "/"))
        script_path = project / "make_charts.py"
        try:
            script_path.write_text(script, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(script_path), str(goal or "")],
                cwd=str(project), capture_output=True, timeout=120,
            )
            if proc.stdout:
                out = proc.stdout.decode("utf-8", errors="replace").strip()
                if out:
                    logger.info("make_charts(%s): %s", task_id, out[:500])
            if proc.returncode != 0:
                logger.warning("make_charts failed: %s", proc.stderr.decode("utf-8", errors="replace")[:200])
            else:
                # 探索性图表同步到 workspace/charts/，供报告内联嵌入
                try:
                    from workspace import task_charts_dir
                    cdir = task_charts_dir(task_id)
                    for png in project.glob("*.png"):
                        shutil.copy2(png, cdir / png.name)
                except Exception as exc:
                    logger.warning("search-chart sync failed: %s", str(exc)[:120])
        except Exception as exc:
            logger.warning("make_charts error: %s", exc)

    # ── Main Loop ──
    def run(self, task_id: str, goal: str, context: str = "",
            auto_run: bool = True, template_steps: list | None = None,
            user_id: str = "", project: str | None = None) -> dict:
        """Execute a full task lifecycle. Returns final status dict."""
        try:
            from llm_client import set_task_context
            set_task_context(task_id)
        except Exception:
            pass
        project = _safe_project(project)
        if not hasattr(self, "_task_projects"):
            self._task_projects = {}
        self._task_projects[task_id] = project
        # C3：任务开始前热重载 system 段配置（下一任务生效；执行中任务不受影响）
        self._reload_system_config()
        started = time.time()
        with self._task_starts_lock:
            self._task_starts[task_id] = started
        if not hasattr(self, "_task_goals"):
            self._task_goals = {}
        self._task_goals[task_id] = goal
        if not hasattr(self, "_task_user_ids"):
            self._task_user_ids = {}
        self._task_user_ids[task_id] = str(user_id or "")
        # 每任务独立成果文件夹：清空本项目目录与旧交付包，保证只含本次产物
        ensure_task_workspace(task_id, project)
        self._cleanup_project_workspace(task_id, project)
        ws_dir = task_workspace(task_id, project)
        try:
            for p in ws_dir.glob("*.zip"):
                p.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Old zip cleanup failed for %s: %s", task_id, exc)
        logger.info("Task %s: %s", task_id, goal[:80])

        # 记忆注入：模板路由与 LLM 规划都先查历史经验（观众可见 memory 日志）
        memory_context = self._inject_memory_context(goal, task_id)
        # 提示词改进经验（进化系统 RAG）：检索相关反思/自迭代记录，
        # 注入规划上下文并用于改写步骤提示词（历史经验反哺）
        prompt_hints = self._query_prompt_hints(goal, task_id)
        if prompt_hints:
            memory_context = (
                f"{memory_context}\n\n"
                "## 历史提示词改进经验（RAG，来自反思/自迭代）\n"
                + "\n".join(f"- {h[:400]}" for h in prompt_hints)
            ).strip()
        if not hasattr(self, "_task_prompt_hints"):
            self._task_prompt_hints = {}
        self._task_prompt_hints[task_id] = prompt_hints

        # LLM 健康预检（P0-1）：主/备用端点均不可用 → 立即终止并向前端弹警告，
        # 避免带着死端点空转 30 分钟（余额不足/密钥失效/无响应）。
        try:
            from llm_client import endpoints_available
            _llm_ok, _llm_msg = endpoints_available()
            if not _llm_ok:
                push_progress(self._messaging, task_id, "warning",
                              {"type": "llm", "agent": "orchestrator",
                               "message": _llm_msg, "timestamp": self._now_iso()})
                push_progress(self._messaging, task_id, "task_complete",
                              {"status": "FAILED", "summary": _llm_msg})
                logger.error("Task %s aborted: %s", task_id, _llm_msg)
                self._notify_done_async(task_id, goal, "FAILED", _llm_msg)
                return {"task_id": task_id, "status": "FAILED",
                        "steps": [], "report": _llm_msg}
            # 主端点近期有鉴权/余额错误（已切备用）→ 弹警告，让用户知道质量下降原因
            from llm_client import get_endpoint_warning
            _llm_warn = get_endpoint_warning()
            if _llm_warn:
                push_progress(self._messaging, task_id, "warning",
                              {"type": "llm", "agent": "orchestrator",
                               "message": _llm_warn
                               + "（已自动切换备用端点；若任务质量下降，请检查前端 API 设置）",
                               "timestamp": self._now_iso()})
            # A3：LLM 端点余额预检——主/备均余额不足直接拒绝任务；
            # 单端点不足照常运行并在 llm_degraded 预置余额警告
            _balance_ok, _balance_msg = self._precheck_llm_balance(task_id)
            if not _balance_ok:
                self._notify_done_async(task_id, goal, "FAILED", _balance_msg)
                return {"task_id": task_id, "status": "FAILED",
                        "steps": [], "report": _balance_msg, "reason": _balance_msg}
        except Exception:
            pass

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
                try:
                    from llm_client import LLMUnavailableError
                    steps = self._plan(goal, task_id, context, memory_context)
                except LLMUnavailableError as exc:
                    push_progress(self._messaging, task_id, "warning",
                                  {"type": "llm", "agent": "orchestrator",
                                   "message": str(exc), "timestamp": self._now_iso()})
                    push_progress(self._messaging, task_id, "task_complete",
                                  {"status": "FAILED", "summary": str(exc)})
                    logger.error("Task %s aborted: %s", task_id, exc)
                    self._notify_done_async(task_id, goal, "FAILED", str(exc))
                    return {"task_id": task_id, "status": "FAILED",
                            "steps": [], "report": str(exc)}
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
        steps = self._inject_skills(steps, goal)
        steps = self._enforce_no_web_scrape_code(steps, goal, task_id)
        if not steps:
            push_progress(self._messaging, task_id, "task_complete",
                          {"status": "FAILED", "summary": "Planning failed"})
            self._notify_done_async(task_id, goal, "FAILED", "Planning failed")
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
                self._notify_done_async(
                    task_id, goal, "FAILED", "Plan not confirmed, task cancelled",
                )
                return {"task_id": task_id, "status": "FAILED", "steps": [],
                        "report": "Plan not confirmed"}
            steps = confirmed
            steps = self._wire_report_deps(steps)
            steps = self._wire_search_fetch_deps(steps)
            steps = self._ensure_package_step(steps)
            steps = self._break_cycles(steps)
            steps = self._inject_goal_into_steps(steps, goal)
            steps = self._inject_skills(steps, goal)
            steps = self._enforce_no_web_scrape_code(steps, goal, task_id)
            if not steps:
                push_progress(self._messaging, task_id, "task_complete",
                              {"status": "FAILED", "summary": "Empty plan confirmed, task cancelled"})
                self._notify_done_async(
                    task_id, goal, "FAILED", "Empty plan confirmed, task cancelled",
                )
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

        # 结构化数据源预载：financial → 东方财富/SEC；crypto/macro/news →
        # 对应新适配器（若命中，搜索仅作补充）
        preloaded = self._structured_data_preload(task_id, goal, project)
        # B 方案：已预载结构化行情数据时，data_analyzer 替换为 content_summary，
        # 让下游步骤直接消费 structured_data.json（图表仍由数据驱动兜底渲染）
        steps = self._reduce_steps_for_structured(task_id, steps, preloaded)

        # 2..N. 执行 + 自主迭代（执行 → 验收评审 → 追加步骤，直到通过或达到上限）
        all_steps: list[dict] = []
        completed_all: dict = {}
        has_failure = False
        iteration = 0
        redo_rounds = 0
        skip_execute = False
        gate_checked = False
        # P0-1：反思 LLM 不可用标记（每次任务重置）
        self._reflection_llm_unavailable = ""
        last_steps = steps
        last_results: list[dict] = []
        best_report = ""

        while True:
            if not skip_execute:
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
            skip_execute = False

            if has_failure or self._max_iterations <= 0 or iteration >= self._max_iterations:
                break
            if simple:
                # 简单任务：一轮执行即交付，由贯通测试守门，不做反射式追加迭代
                break
            # 评测驱动反思（对标标准 3.6）：先过评测闸门（每任务一次），
            # 达标直接交付；未达标则覆盖"研究类跳过反思"，继续修正。
            # B3：gate_checked 标志保证 judge 调用只在评测闸门命中时执行一次
            # （evals/drive.py 内部才调用 evals.judge），每轮反思不会重复触发。
            _eval_scores = ""
            _gate_failed = False
            if not gate_checked:
                gate_checked = True
                try:
                    from evals.drive import eval_gate, gate_passed
                    _matched, _scores = eval_gate(
                        task_id, goal, best_report, completed_all
                    )
                    if _matched and _scores:
                        # 持久化评测分数，供前端评测看板（O-24）
                        try:
                            r = self._new_redis_sync()
                            r.set(
                                f"eval_score:{task_id}",
                                json.dumps(_scores, ensure_ascii=False),
                                ex=86400,
                            )
                        except Exception:
                            pass
                        _eval_scores = "；".join(
                            f"{k}={v:.2f}" for k, v in _scores.items()
                        )
                        if gate_passed(_scores):
                            _avg = sum(_scores.values()) / max(1, len(_scores))
                            push_progress(self._messaging, task_id, "log",
                                          {"type": "iteration", "agent": "orchestrator",
                                           "message": f"评测达标（avg={_avg:.2f}），直接交付",
                                           "timestamp": self._now_iso()})
                            break
                        _gate_failed = True
                except Exception as exc:
                    logger.info("Eval gate skipped: %s", str(exc)[:120])
            # 验证器（含时效性审查）先于"跳过反思"决策计算：
            # 时效性未通过 → 强制进入反思重检索，避免"最新财报返回旧年份"
            try:
                from validators.registry import run_for_task, summary_text
                _caps = [str(s.get("capability")) for s in all_steps]
                _vres = run_for_task(task_id, goal, _caps)
                _vsum = summary_text(_vres)
                if ("recency_check" in _vsum and "未通过" in _vsum) or (
                    "completeness_check" in _vsum and "未通过" in _vsum
                ):
                    # 时效性或完整性审查失败 → 强制进入反思重检索（ReAct 模式）
                    _gate_failed = True
            except Exception:
                _vsum = ""
            # 报告/调研类任务：核心管道已产出报告（图表+来源已确定性嵌入）后直接交付，
            # 反射轮只会追加"锦上添花"步骤拖慢任务；评测未达标时例外
            _goal_low = str(goal or "").lower()
            _research_hint = any(k in _goal_low for k in ("报告", "调研", "研报"))
            _has_search = any(
                s.get("capability") == "web_search" for s in all_steps
            )
            _report_done = any(
                s.get("capability") == "report_generator"
                and completed_all.get(s["step_id"], {}).get("status") == "SUCCESS"
                for s in all_steps
            )
            # P3：确定性验收 fail 时不得跳过反射轮（验收为准，不因"核心交付
            # 已完成"放行）；无验收报告（None）保持原有跳过行为
            _acceptance_ok = self._acceptance_passed(task_id)
            if (
                _research_hint and _report_done and _has_search
                and not _gate_failed and _acceptance_ok is not False
            ):
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": "Reflection: 报告类任务核心交付已完成，跳过反射轮",
                               "timestamp": self._now_iso()})
                break
            if _research_hint and _report_done and _has_search and _acceptance_ok is False:
                push_progress(self._messaging, task_id, "log",
                              {"type": "iteration", "agent": "orchestrator",
                               "message": "确定性验收未通过，覆盖'报告类跳过反射轮'，进入修复迭代",
                               "timestamp": self._now_iso()})
            verdict = self._reflect(
                goal, best_report, task_id, all_steps, completed_all,
                memory_context, _vsum, _eval_scores,
            )
            # P3：验收 fail 存在时，反思判定以验收为准（accept/高评分不得放行）
            _acc_summary = self._read_acceptance_summary(task_id)
            _acc_fail = bool(_acc_summary and _acc_summary.get("overall") != "pass")
            if not verdict and _acc_fail:
                # 反思 LLM 不可用同样不放行：按验收缺口合成重做步骤
                logger.warning(
                    "验收 fail 且反思不可用，按验收缺口强制进入重做（task=%s）",
                    task_id,
                )
                _gap_text = "\n".join(
                    f"- {g}" for g in (_acc_summary.get("gaps") or [])
                ) or "确定性验收未通过，请重新生成报告并修复全部缺口"
                verdict = {
                    "score": 0.0,
                    "verdict": "add_steps",
                    "gaps": _acc_summary.get("gaps") or [],
                    "next_steps": [{
                        "step_id": "acc-redo",
                        "capability": "report_generator",
                        "instruction": (
                            "按确定性验收缺口重做报告：\n" + _gap_text
                        ),
                        "timeout": 180,
                    }],
                }
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
            action = str(
                verdict.get("verdict")
                or ("accept" if verdict.get("accepted") else "add_steps")
            ).lower()
            # 主动记忆（对标标准 3.4 agent_control）：执行反思输出的记/忘操作
            for _op in (verdict.get("memory_ops") or [])[:3]:
                try:
                    _act = str(_op.get("action") or "")
                    _mem = getattr(self, "_memory", None)
                    if _act == "remember" and _op.get("summary") and _mem is not None:
                        if hasattr(_mem, "add_note"):
                            _mem.add_note(
                                goal, str(_op["summary"])[:400],
                                str(_op.get("key") or "")[:80],
                            )
                    elif _act == "forget" and _op.get("key") and _mem is not None:
                        if hasattr(_mem, "delete_where") and hasattr(_mem, "_conversations"):
                            _mem.delete_where(
                                _mem._conversations, {"goal": str(_op["key"])[:200]}
                            )
                except Exception:
                    pass
            score_gate = (
                action in ("accept", "stop")
                or score >= self._reflection_accept_score
            )
            if _acc_fail and score_gate:
                # P3：确定性验收 fail 覆盖反思评分——accept/高评分必须转为重做
                logger.warning(
                    "验收 fail 覆盖反思评分（action=%s score=%.1f，task=%s），"
                    "强制进入重做",
                    action, score, task_id,
                )
                push_progress(self._messaging, task_id, "log",
                              {"type": "iteration", "agent": "orchestrator",
                               "message": (
                                   f"确定性验收未通过，覆盖反思评分 {action}"
                                   f"（score={score:.1f}），强制进入重做"
                               ),
                               "timestamp": self._now_iso()})
                if not (action == "retry_step" and verdict.get("retry_step_id")):
                    if not verdict.get("next_steps"):
                        _gap_text = "\n".join(
                            f"- {g}"
                            for g in (_acc_summary.get("gaps")
                                      or verdict.get("gaps") or [])
                        ) or "确定性验收未通过，请重新生成报告并修复全部缺口"
                        verdict = dict(verdict)
                        verdict["next_steps"] = [{
                            "step_id": "acc-redo",
                            "capability": "report_generator",
                            "instruction": (
                                "按确定性验收缺口重做报告：\n" + _gap_text
                            ),
                            "timeout": 180,
                        }]
                        verdict["verdict"] = "add_steps"
                    action = "add_steps"
            elif score_gate:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": f"Reflection: {action}（score={score:.1f}）",
                               "timestamp": self._now_iso()})
                break
            if action == "retry_step" and verdict.get("retry_step_id"):
                if redo_rounds < self._max_redo_rounds:
                    redo_rounds += 1
                    ok = self._redo_step_and_dependents(
                        task_id, goal, all_steps, completed_all,
                        str(verdict.get("retry_step_id")),
                        str(verdict.get("retry_reason") or ""),
                    )
                    if ok:
                        # 重做后基于新结果重建 best_report，继续反思确认
                        cand = self._best_deliverable(
                            goal, all_steps,
                            [completed_all.get(s["step_id"], {}) for s in all_steps],
                        )
                        if len(cand) > len(best_report):
                            best_report = cand
                        self._publish_full_state(task_id, goal, all_steps, completed_all)
                        skip_execute = True  # 跳过整轮重跑，直接进入下一轮反思
                        continue
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": "单步重做预算耗尽或步骤不存在，停止反思",
                               "timestamp": self._now_iso()})
                break
            gaps = verdict.get("gaps") or []
            # 反思预算：每次最多追加 max_reflection_steps 个步骤，防止任务膨胀
            next_steps = self._normalize_steps(
                (verdict.get("next_steps") or [])[: self._max_reflection_steps]
            )
            if not next_steps:
                break
            # 反思新增步骤（即对反思提示词的落地改动）→ 沉淀进进化系统 RAG
            self._record_reflection_refinement(
                goal, task_id,
                key="reflect",
                issue="；".join(str(x) for x in (gaps or []))[:300],
                fix_prompt="\n".join(
                    f"- [{s.get('capability')}] {str(s.get('instruction') or '')[:200]}"
                    for s in next_steps
                )[:800],
            )
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
            steps = self._inject_skills(steps, goal)
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

        # P0-1/P0-2 + A1：以最终 acceptance_report.json 判定——验收 fail
        # 无论反思是否执行/重做后仍 fail 都如实降级为 SUCCESS_WITH_ISSUES；
        # 反思重做后验收 pass 则 SUCCESS；LLM 双端点均失败同样降级
        acceptance_summary = self._read_acceptance_summary(task_id)
        llm_degraded = self._read_llm_degraded(task_id)
        overall = self._resolve_final_status(
            has_failure, acceptance_summary,
            getattr(self, "_reflection_llm_unavailable", ""), llm_degraded,
        )
        if overall == "SUCCESS_WITH_ISSUES":
            logger.error(
                "Task %s completed with issues: acceptance=%s "
                "reflection_unavailable=%s llm_degraded=%s",
                task_id,
                (acceptance_summary or {}).get("overall"),
                getattr(self, "_reflection_llm_unavailable", ""),
                json.dumps(llm_degraded, ensure_ascii=False),
            )
        self._publish_usage()

        # 4. Memory（P0 验收准入：验收 fail 只沉淀对话，不沉淀策略；
        #    判定逻辑在 MemoryManager.consolidate_memory 内，这里传入验收摘要）
        if not has_failure:
            # 记录探索-固化统计（domain×能力链 × 验收），供模板固化阈值判定
            self._record_consolidation_stat(task_id, goal, all_steps)
            with self._memory_lock:
                self._memory.consolidate_memory(
                    goal, all_steps, report,
                    acceptance_summary=acceptance_summary,
                    task_id=task_id,
                )
            if acceptance_summary is not None and (
                acceptance_summary.get("overall") or ""
            ) != "pass":
                logger.warning(
                    "Task %s acceptance failed, skipped strategy consolidation",
                    task_id,
                )
                push_progress(self._messaging, task_id, "log",
                              {"type": "memory", "agent": "orchestrator",
                               "message": "验收未通过，仅记录对话，跳过策略沉淀",
                               "timestamp": self._now_iso()})
            else:
                push_progress(self._messaging, task_id, "log",
                              {"type": "memory", "agent": "orchestrator",
                               "message": "Strategy memory consolidated",
                               "timestamp": self._now_iso()})
            # 进化沉淀：复杂任务（未走模板）成功后提炼为确定性模板，供后续 LLM 路由选择
            if not used_template:
                self._consolidate_template(goal, all_steps, task_id=task_id)

        # 5. Complete
        ok_count = sum(1 for r in completed_all.values() if r.get("status") == "SUCCESS")
        push_progress(self._messaging, task_id, "task_complete",
                      {"status": overall,
                       "summary": f"{overall}: {ok_count}/{len(all_steps)} steps, {iteration} iterations",
                       "report": report,
                       "acceptance": acceptance_summary,
                       "llm_degraded": llm_degraded})

        # 6. 提示词自迭代（后台线程，不阻塞交付）：LLM 分析本次输出与预期的差距，
        #    总结问题并产出改进版提示词写入注册表，下一轮任务自动生效
        def _refine_async() -> None:
            try:
                from prompt_refinery import maybe_refine
                maybe_refine(
                    self._messaging, task_id, goal, all_steps, completed_all, report,
                    {"has_failure": has_failure, "reflection_used": iteration > 0,
                     "iterations": iteration, "memory": getattr(self, "_memory", None)},
                )
            except Exception as exc:
                logger.warning("prompt refinery async failed: %s", str(exc)[:150])

        threading.Thread(target=_refine_async, daemon=True).start()

        # F5：任务完成外部通知（后台线程，不阻塞完成流程）
        self._notify_done_async(task_id, goal, overall, report)

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

    def _wait_step_confirm(self, task_id: str, step: dict) -> bool:
        """人工确认单步（mode=human_in_loop）：确认放行，取消拒绝，超时自动放行。"""
        try:
            key = f"step_confirm:{task_id}:{step.get('step_id')}"
            push_progress(self._messaging, task_id, "log",
                          {"type": "step_confirm", "agent": step.get("capability", "?"),
                           "message": f"等待人工确认步骤 {step.get('step_id')}（{step.get('capability')}）",
                           "timestamp": self._now_iso()})
            msg = self._brpop_with_deadline(
                self._redis, key, time.time() + self._plan_confirm_timeout,
            )
            if not msg:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": f"步骤 {step.get('step_id')} 确认超时，自动继续",
                               "timestamp": self._now_iso()})
                return True
            data = json.loads(msg[1] if isinstance(msg[1], str) else msg[1].decode())
            if data.get("action") == "cancel":
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": f"步骤 {step.get('step_id')} 被用户取消",
                               "timestamp": self._now_iso()})
                return False
            return True
        except Exception as exc:
            # Redis 不可用/测试环境 → 自动放行，不阻塞任务
            logger.info("Step confirm skipped (auto-proceed): %s", str(exc)[:100])
            return True

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
            base_instr = self._inject_step_context(step, completed, lock, task_id)
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
            # 人机协作（对标标准 3.2 human_in_loop）：高风险步骤执行前等人工确认
            if str(step.get("mode")) == "human_in_loop":
                if not self._wait_step_confirm(task_id, step):
                    return {"task_id": step["step_id"], "status": "FAILED",
                            "result": "步骤被用户取消",
                            "elapsed_sec": round(time.time() - step_start, 1)}
            result = self._dispatch_step_safe(goal, step, task_id, state)
            # P1-1：react_agent 未收敛/失败 → 自动降级为 content_summary，
            # 不把"ReAct 达到最大轮数仍未收敛"这类过程文本传给报告
            if (
                step.get("capability") == "react_agent"
                and (
                    result.get("status") == "FAILED"
                    or "未收敛" in str(result.get("result") or "")
                    or "最大轮数" in str(result.get("result") or "")
                )
            ):
                push_progress(self._messaging, task_id, "log",
                              {"type": "replan", "agent": "orchestrator",
                               "message": "react_agent 未收敛，自动降级为 content_summary",
                               "timestamp": self._now_iso()})
                fb_step = {
                    "step_id": step["step_id"],
                    "capability": "content_summary",
                    "instruction": (
                        "基于本任务已有的搜索结果/抓取内容，用中文输出结构化要点总结；"
                        "若上游数据不足，如实列出缺失项，禁止编造。"
                    ),
                    "timeout": 120,
                }
                fb_instr = self._inject_step_context(fb_step, completed, lock, task_id)
                fb_step["instruction"] = f"任务目标：{goal[:300]}\n\n{fb_instr}"
                fb_result = self._dispatch_step_safe(goal, fb_step, task_id, state)
                if fb_result.get("status") == "SUCCESS":
                    fb_result["degraded_from_react"] = True
                    result = fb_result
            if step.get("capability") == "web_fetch" and result.get("status") == "SUCCESS":
                # 快照页回灌清洗：抓到的正文并入清洗输入，财务数字进入图表/摘要
                self._recycle_fetch_into_clean(task_id, goal, result)
            if step.get("capability") == "report_generator" and result.get("status") == "SUCCESS":
                # 确定性验收器：数字溯源等 checklist → 缺口报告（供反思/前端/人工）
                self._run_acceptance_check(task_id, goal)
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
                            # 数据清洗/结构化（搜索 → 清洗 → 绘图）：
                            # 先生成 clean_chart_data.json，绘图只读清洗后数据
                            from clean_data import clean_file
                            clean_file(_json_path, goal=goal)
                            self._remerge_structured_financials(task_id)
                            self._remerge_structured_points(task_id)
                            self._generate_search_charts(task_id, goal)
                            self._render_clean_chart_data(task_id, goal)
                        except Exception as exc:
                            logger.warning("search_results.json write failed: %s", exc)
            if (step.get("capability") == "content_summary"
                    and result.get("status") == "SUCCESS"):
                # LLM 结构化图表规格 → 主题过滤 → 确定性渲染（数据驱动：
                # 只要总结含 ≥2 个可作图数据点就渲染，不依赖目标措辞）
                # （语义/结论由 LLM 负责，数字与标注由脚本保证）
                from chart_specs import (
                    merge_year_series, verify_specs_against_text, wrap_rows_to_specs,
                )
                _summary_text = str(result.get("result") or "")
                _llm_specs = self._extract_chart_data(_summary_text)
                _table_rows = self._extract_chart_rows_from_table(_summary_text)
                # LLM 规格 + 表格兜底合并（不再二选一），保证结果更可能有图
                chart_specs = list(_llm_specs) + wrap_rows_to_specs(_table_rows)
                # 去重：同标题优先保留 LLM 版
                _seen_titles = {}
                for _s in chart_specs:
                    _t = str(_s.get("title") or "")
                    if _t not in _seen_titles:
                        _seen_titles[_t] = _s
                chart_specs = list(_seen_titles.values())
                # 同指标跨年份的单点图合并为时间序列（防 2025/2026 拆成两张单点图）
                chart_specs = merge_year_series(chart_specs)
                # 数据溯源：数值必须能在摘要文本中找到（防 LLM 编造/转写错误）
                chart_specs, _dropped_rows = verify_specs_against_text(
                    chart_specs, _summary_text
                )
                chart_specs = self._filter_chart_specs(chart_specs, goal)
                logger.info(
                    "chart pipeline %s: llm_specs=%d table_rows=%d dropped_rows=%d kept=%d",
                    task_id,
                    len(_llm_specs),
                    len(_table_rows),
                    _dropped_rows,
                    len(chart_specs),
                )
                if chart_specs:
                    try:
                        _cd_path = task_project_dir(task_id) / "chart_data.json"
                        _cd_path.write_text(
                            json.dumps({"charts": chart_specs}, ensure_ascii=False, indent=1),
                            encoding="utf-8",
                        )
                        self._render_chart_data(task_id, goal)
                    except Exception as exc:
                        logger.warning("chart_data render failed: %s", exc)
                else:
                    # P1-3：LLM 未产出图表规格时，数据驱动兜底（≥2 个可作图点即渲染）
                    self._render_clean_chart_data(task_id, goal)
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
        # 工作流模式（对标标准 3.2）：任一 pipeline 步骤 → 整轮串行执行
        serial = any(str(s.get("mode")) == "pipeline" for s in steps)
        last_progress = time.time()
        in_flight = 0
        revision_done = False

        def worker():
            nonlocal last_progress, has_failure, in_flight, revision_done
            while True:
                wait_serial = False
                ready = None
                with lock:
                    if not pending:
                        return
                    if serial and in_flight > 0:
                        # 串行模式：等当前步骤完成再取下一个
                        wait_serial = True
                    else:
                        for k, s in pending.items():
                            if deps_ok(s) and not deps_failed(s):
                                ready = (k, pending.pop(k))
                                break
                if wait_serial:
                    time.sleep(0.5)
                    continue
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

    def _inject_step_context(
        self, step: dict, completed: dict, lock: threading.Lock, task_id: str = "",
    ) -> str:
        """按能力类型把前序步骤的输出注入指令（URL/路径/结果摘要）。"""
        instr = step.get('instruction', '')
        cap = step.get('capability', '')
        deps = step.get('depends_on', [])

        def _prev(dep_id):
            with lock:
                prev = completed.get(dep_id)
            return prev.get('result', '') if isinstance(prev, dict) else ''

        def _safe(text: str) -> str:
            """上一步结果可能来自外部网页，进指令前做注入检测（对标 C4-4.4）。"""
            try:
                from security import detect_injection
                bad, reason = detect_injection(text)
                if bad:
                    return f"[已过滤可疑内容：{reason}]"
            except Exception:
                pass
            return text

        def _filter_role(text: str) -> str:
            """按任务用户职位过滤注入片段（仅过滤带 [kb:...] 标记的受控内容）。"""
            uid = (getattr(self, "_task_user_ids", {}) or {}).get(task_id, "")
            if not uid:
                return text
            try:
                if not hasattr(self, "_kb_ctrl"):
                    from kb_access_control import KbAccessControl
                    self._kb_ctrl = KbAccessControl()
                kept = self._kb_ctrl.filter_contents(uid, [text])
                return kept[0] if kept else "[无权限访问该知识片段，已过滤]"
            except Exception:
                return text

        if cap in ('data_loader', 'web_fetch'):
            goal_low = str((getattr(self, "_task_goals", {}) or {}).get(task_id, "") or "").lower()
            finance_fetch = (
                cap == "web_fetch"
                and any(k in goal_low for k in (
                    "财报", "年报", "季报", "营收", "净利润", "负债", "财务", "业绩",
                    "financial", "revenue", "earnings",
                ))
            )
            for dep_id in deps:
                prev_res = _prev(dep_id)
                try:
                    prev_json = json.loads(prev_res) if isinstance(prev_res, str) else prev_res
                except Exception:
                    prev_json = prev_res
                if isinstance(prev_json, list):
                    if finance_fetch:
                        best = self._pick_fetch_url(
                            prev_json,
                            str((getattr(self, "_task_goals", {}) or {}).get(task_id, "") or ""),
                        )
                        if best:
                            instr = f"[URL: {best}] " + instr
                    else:
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
            if finance_fetch:
                instr += (
                    " 优先抓取与财报/财务数据直接相关的页面，"
                    "抓取完整正文（保留所有数字、年份、口径），不要只抓摘要。"
                )

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
            # 断链修复：工作区已预载结构化数据（eastmoney_ranking 等非财务类）时，
            # 直接指向 data/ranking.csv / structured_data.json，无需再找"新 CSV"
            try:
                _rk_csv = task_data_dir(task_id) / "ranking.csv"
                _sd_json = task_project_dir(task_id) / "structured_data.json"
                if _rk_csv.exists():
                    instr += f' [Data: {_rk_csv}]'
                    instr += (
                        "\n[数据] 工作区已预载行情排行数据 data/ranking.csv"
                        "（列：rank/code/name/price/change_pct/volume_wan_hand/"
                        "amount_yi/turnover_pct），请优先读取该文件做 EDA/图表，"
                        "无需再找 CSV。"
                    )
                elif _sd_json.exists():
                    try:
                        _sd = json.loads(_sd_json.read_text(encoding="utf-8"))
                        _src = str(_sd.get("source") or "")
                    except Exception:
                        _src = ""
                    if _src in (
                        "eastmoney_ranking", "tencent_ranking",
                        "tencent_us_ranking", "sina_ranking", "macro",
                    ):
                        # 排行/宏观 JSON 含 rows/points 数组，worker 可直接转 DataFrame
                        instr += f' [Data: {_sd_json}]'
                    instr += (
                        "\n[数据] 工作区已预载结构化数据 structured_data.json"
                        f"（来源：{_src or '未知'}），请优先读取该文件，无需再找 CSV。"
                    )
            except Exception:
                pass

        if cap == 'file_io':
            for dep_id in deps:
                prev_res = _prev(dep_id)
                snippet = str(prev_res)[:12000] if prev_res else ''
                if snippet:
                    instr += f"\n[上一步结果 {dep_id}]:\n{_filter_role(_safe(snippet))}"

        if cap == 'code_execution':
            # 数据清洗提示：作图任务优先读取结构化清洗数据，避免直接解析原始文本
            try:
                from workspace import task_project_dir as _tpd
                _cd = _tpd(task_id) / "clean_chart_data.json"
                if _cd.exists():
                    instr += (
                        "\n[数据] 工作区已提供清洗后的结构化图表数据 clean_chart_data.json"
                        "（含 entity_frequency / market_data / source_distribution / topic_terms）。"
                        "如任务需要作图，请优先读取该文件并按其中的 Label-Value 结构绘图，"
                        "不要直接解析 search_results.json 的原始文本。"
                    )
            except Exception:
                pass
            # 统计类排行（前 N% 占比等）：明确指向全市场 ranking.csv，
            # 防止代码步骤只取前十（历史 code_execution 反复失败的根因）
            try:
                _rk_csv = task_data_dir(task_id) / "ranking.csv"
                if _rk_csv.exists():
                    _goal = str(
                        (getattr(self, "_task_goals", {}) or {}).get(task_id, "")
                        or ""
                    )
                    if self._is_statistical_goal(_goal):
                        block = self._statistical_ranking_instruction(
                            task_id, _goal, _rk_csv,
                        )
                        if block:
                            instr += block
            except Exception:
                pass

        if cap in ('content_summary', 'report_generator'):
            # V1.2 竞品启示：注入三级溯源链/数据时效/免责声明强制要求
            try:
                instr += _REPORT_FORMAT_REQUIREMENTS
            except Exception:
                pass
            # 结构化财务数据【内容】注入（不只文件提及）：让报告/总结 LLM 真正看到
            # 权威年报序列，否则模型只会用搜索片段（如 IT之家）并宣称历史年份缺失；
            # 新数据源（crypto/macro/news）经 structured_data.json 走同一通道。
            try:
                block = self._structured_injection(task_id)
                if block:
                    instr += block
                # P2-5 追加数据源选择依据：市场偏好与候选列表（前 3）
                prefs = (
                    getattr(self, "_task_market_resolution", {}) or {}
                ).get(task_id)
                if prefs and prefs.get("name") and prefs.get("market"):
                    alts = "、".join(
                        f"{a.get('market')}:{a.get('name')}"
                        for a in (prefs.get("alternatives") or [])[:3]
                        if a.get("market") and a.get("name")
                    )
                    instr += (
                        f"\n数据源：{prefs['name']}"
                        f"（{prefs['market']} 市场 {prefs['code']}），"
                        f"系统按市场偏好 {prefs['preference']} 选择；"
                        f"候选：{alts}。"
                    )
            except Exception:
                pass
            for dep_id in deps:
                prev_res = _prev(dep_id)
                _raw = str(prev_res or "")
                if len(_raw) > 6000 and os.environ.get("ROLLING_SUMMARY", "1") != "0":
                    # 滚动摘要（对标标准 3.4）：长前序先压缩成要点，防中间迷失
                    snippet = self._rolling_summarize(_raw)
                else:
                    snippet = _raw[:2500]
                if snippet:
                    instr += f"\n[上一步结果 {dep_id}]:\n{_filter_role(_safe(snippet))}"
                # 读取产物文件（如 code_execution 落盘的 HTML/代码），给报告真实素材；
                # 仅白名单数据类文本注入正文，源码/二进制（HTML/JS/CSS/PY/图片等）
                # 只提示文件存在与路径，禁止把文件原文抄进报告指令
                try:
                    parsed = json.loads(prev_res) if isinstance(prev_res, str) else prev_res
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    fpath = parsed.get("path") or parsed.get("report_path") or parsed.get("data_path") or ""
                    if fpath and os.path.exists(fpath):
                        try:
                            fname = os.path.basename(fpath)
                            if os.path.splitext(fpath)[1].lower() in _ARTIFACT_TEXT_EXTENSIONS:
                                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                                    content = f.read(4000)
                                if content.strip():
                                    instr += f"\n[产物文件 {dep_id} ({fname})]:\n{content[:3000]}"
                            else:
                                instr += (
                                    f"\n[产物文件 {dep_id} ({fname})]"
                                    "（源码/二进制，跳过正文注入，仅保留路径）"
                                )
                        except Exception:
                            pass
            instr += ("\n[指令] 仅使用与任务目标主题直接相关的信息；"
                      "若上一步结果中的来源与主题无关（如无关政策新闻、其他领域文档、垃圾站点或空结果），"
                      "一律不要纳入输出，并注明已剔除无关内容。")
        return instr

    def _rolling_summarize(self, text: str) -> str:
        """滚动摘要：LLM 压缩长上下文；失败回退硬截断。"""
        try:
            from llm_client import call_llm
            raw = call_llm(
                "你是上下文压缩器。把长文本压缩为不超过 800 字的要点列表，"
                "必须保留关键事实、数值、机构与来源信息，不得编造。"
                "只输出 Markdown 要点。",
                str(text)[:12000],
                expect_json=False,
            )
            out = str((raw or {}).get("content") or "") if isinstance(raw, dict) else str(raw or "")
            if len(out.strip()) >= 100:
                return out.strip()
        except Exception:
            pass
        return str(text)[:2500]

    def _inject_goal_into_steps(self, steps: list[dict], goal: str) -> list[dict]:
        """把用户目标注入所有步骤（尤其模板步骤），防止模板指令与目标跑偏。"""
        if not goal:
            return steps
        market_suffix = _MARKET_SEARCH_SUFFIX if self._is_market_goal(goal) else ""
        for s in steps:
            ins = str(s.get("instruction", ""))
            if "用户目标：" not in ins[:80]:
                s["instruction"] = (
                    f"用户目标：{goal[:300]}\n"
                    f"原始指令：{ins}"
                )
            if (
                s.get("capability") == "web_search"
                and market_suffix
                and "行情数据源限定" not in s["instruction"]
            ):
                s["instruction"] += market_suffix
        return steps

    def _inject_skills(self, steps: list[dict], goal: str) -> list[dict]:
        """Skill 渐进式披露：按目标/能力命中 skill，注入 description+质量标准+反模式
        （对标标准 3.5，只给标准不给全文工作流）。"""
        try:
            from skill_registry import (
                get_lessons, get_skill_standards, match_skills, skill_applies,
            )
        except Exception:
            return steps
        for s in steps:
            hits = match_skills(goal, s.get("capability"))
            if not hits:
                continue
            # 能力门控：skill 只注入其适用范围内的步骤
            hit = next(
                (h for h in hits if skill_applies(h["name"], s.get("capability"))),
                None,
            )
            if not hit:
                continue
            std = get_skill_standards(hit["name"])
            if not std:
                continue
            block = f"[Skill: {std['name']}] {std['description']}"
            if std.get("standards"):
                block += f"\n【质量标准】{std['standards']}"
            if std.get("antipatterns"):
                block += f"\n【反模式】{std['antipatterns']}"
            lessons = get_lessons(std["name"], limit=2)
            if lessons:
                block += "\n【历史教训（自动沉淀）】" + "；".join(
                    f"{x.get('issue', '')}→{x.get('fix', '')[:120]}"
                    for x in lessons
                )
            s["instruction"] = f"{s['instruction']}\n\n{block}"
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
        """派发单步：失败自动重试，重试仍失败则尝试单步重规划。
        P1-4：重试/重规划结束后写结构化失败诊断（step_failure.json），
        替换步骤结果已知后再落盘，供反思精准补缺口。"""
        result = self._dispatch(step, task_id)
        attempt = 0
        # 输出契约校验（对标 3.1 引导-校验-重试）：契约不通过视为失败，
        # 并把校验错误喂回指令重试，而不是盲目重发
        issue = self._contract_issue(goal, step, result)
        tried: list[str] = []
        while (result.get("status") == "FAILED" or issue) and attempt < self._max_retry:
            attempt += 1
            tried.append(
                f"重试#{attempt}"
                + ("（输出契约校验失败）" if issue else "")
            )
            push_progress(self._messaging, task_id, "log",
                          {"type": "retry", "agent": step.get("capability", "?"),
                           "message": f"Step {step.get('step_id')} retry {attempt}/{self._max_retry}",
                           "timestamp": self._now_iso()})
            time.sleep(2)
            amended = dict(step)
            if step.get("capability") == "web_search":
                # 搜索重试必须更换策略：同一关键词重复搜只会得到同样结果
                amended["instruction"] = (
                    f"{step.get('instruction', '')}\n\n"
                    f"【搜索重试 {attempt}】上次查询未获得有效结果；"
                    "本次必须更换查询词组合/增加限定条件"
                    "（如 site: 官方域名、具体年份、具体指标词），"
                    "禁止原样重复上次查询。"
                    + (
                        f"\n【行情目标换词】{_MARKET_SEARCH_SUFFIX.strip()}"
                        if self._is_market_goal(goal) else ""
                    )
                    + (
                        f"\n【输出契约校验失败】{issue}，请修正输出格式后重新执行。"
                        if issue else ""
                    )
                )
            elif issue:
                amended["instruction"] = (
                    f"{step.get('instruction', '')}\n\n"
                    f"【输出契约校验失败】{issue}，请修正输出格式后重新执行。"
                )
            result = self._dispatch(amended, task_id)
            issue = self._contract_issue(goal, amended, result)
        # 契约在重试耗尽后仍不通过：不得把 SUCCESS 空转当成功，
        # 标记为失败让下方重规划路径接管（或如实返回 FAILED）。
        if issue and result.get("status") != "FAILED":
            result = dict(result)
            result["status"] = "FAILED"
            result["contract_violation"] = True
            result["result"] = (
                str(result.get("result") or "") + f"\n\n[CONTRACT_VIOLATION] {issue}"
            ).strip()
        alt = None
        alt_result = None
        fail_result = None
        if result.get("status") == "FAILED" and state["replan_used"] < self._replan_depth:
            fail_result = result
            alt = self._replan_step(goal, step, result.get("result", ""), task_id)
            if alt:
                state["replan_used"] += 1
                tried.append(f"重规划为 {alt.get('capability')}")
                alt_result = self._dispatch(alt, task_id)
                if alt_result.get("status") == "SUCCESS":
                    alt_result["replanned"] = True
                    alt_result["replan_instruction"] = alt.get("instruction", "")
                result = alt_result
        # P1-4：发生过失败（重试或重规划）就落盘结构化诊断
        if attempt > 0 or alt is not None:
            try:
                from step_diagnosis import write_step_failure
                diag = self._build_step_diagnosis(
                    step, fail_result or result, issue, tried, alt, alt_result, task_id,
                )
                write_step_failure(task_id, diag)
            except Exception as exc:
                logger.warning("step failure diagnosis write failed: %s", str(exc)[:120])
        return result

    def _contract_issue(self, goal: str, step: dict, result: dict) -> str:
        """按能力返回契约校验步骤结果；返回问题文本（空串=通过）。
        web_search 额外做相关性校验：返回结果全部与目标无关时也判为失败，
        触发换查询词重试（复用 _dispatch_step_safe 的搜索重试分支）。"""
        try:
            from tool_contracts import validate_result
            ok, issues = validate_result(
                step.get("capability"), result.get("result")
            )
            if not ok:
                return "；".join(issues)
            if step.get("capability") == "web_search":
                try:
                    data = json.loads(str(result.get("result") or ""))
                except Exception:
                    data = None
                if isinstance(data, list):
                    irr = self._search_results_irrelevant(goal, data)
                    if irr:
                        return irr
            return ""
        except Exception:
            return ""

    @staticmethod
    def _is_market_goal(goal: str) -> bool:
        """是否行情排行类目标：命中专用关键词，或"排行/前十/榜单"与
        A股/股票/沪深/今日 语境同时出现。"""
        g = str(goal or "").lower()
        if any(k in g for k in _MARKET_SEARCH_KEYWORDS):
            return True
        # 统计类排行（如"前5%成交额占比"）没有"排行/前十"字样，
        # 但含成交额/成交量 + 市场语境，仍按行情目标处理搜索补充
        if any(k in g for k in ("成交额", "成交量")) and any(
            k in g for k in ("a股", "股票", "沪深", "股市", "今日")
        ):
            return True
        if any(k in g for k in ("排行", "排名", "前十", "榜单")):
            return any(
                k in g for k in ("a股", "股票", "沪深", "股市", "今日")
            )
        return False

    @staticmethod
    def _is_statistical_goal(goal: str) -> bool:
        """是否统计类排行目标：占比/比例/百分位/前N%/分布/合计/汇总等。

        这类任务必须拿到全市场数据（含分母），不能只取前十。"""
        g = str(goal or "").lower()
        if any(k in g for k in _STATISTICAL_GOAL_KEYWORDS):
            return True
        return bool(re.search(r"前\s*\d+(?:\.\d+)?\s*%", g))

    @staticmethod
    def _goal_search_tokens(goal: str) -> list[str]:
        """从目标提取通用相关性 token：中文 3/4 字滑窗 + 英文词（≥4 字符），
        剔除包装词片段，用于"搜索返回全部无关来源"判定。"""
        g = str(goal or "").lower()
        out: set[str] = set()
        for run in re.findall(r"[\u4e00-\u9fff]+", g):
            for size in (4, 3):
                for i in range(len(run) - size + 1):
                    tok = run[i:i + size]
                    if not any(w in tok for w in (
                        "任务目标", "用户目标", "原始指令", "验收", "输出",
                        "生成", "完成", "要求",
                    )):
                        out.add(tok)
        out.update(
            w.lower()
            for w in re.findall(r"[a-z][a-z0-9-]{3,}", g)
            if w.lower() not in ("the", "and", "with", "from", "that")
        )
        return sorted(out)[:20]

    @staticmethod
    def _search_results_irrelevant(goal: str, results: list) -> str:
        """搜索结果相关性判定：返回问题文本（空串=有有效结果）。
        行情类目标要求标题/摘要命中 A股/成交/排行 等关键词；
        通用目标要求至少一个结果命中目标关键词片段。"""
        if not results:
            return ""
        hay = " ".join(
            f"{it.get('title') or ''} {it.get('snippet') or ''} {it.get('url') or ''}"
            for it in results if isinstance(it, dict)
        ).lower()
        if OrchestratorV2._is_market_goal(goal):
            hits = [
                k for k in ("a股", "成交", "排行", "排名", "涨停", "跌幅", "前十", "股票")
                if k in hay
            ]
            if hits:
                return ""
            return (
                "搜索结果均为无关来源（标题/摘要未命中 A股/成交/排行 等行情关键词），"
                "请更换查询词并限定财经行情站点（东方财富/同花顺/新浪财经/雪球）"
            )
        tokens = OrchestratorV2._goal_search_tokens(goal)
        if not tokens:
            return ""
        if any(t in hay for t in tokens):
            return ""
        return (
            "搜索结果均为无关来源（标题/摘要未命中目标关键词），"
            "请更换查询词组合/增加限定条件"
        )

    @staticmethod
    def _classify_step_error(capability: str, result: dict, issue: str = "") -> str:
        """把步骤失败归类为稳定 error_type（供结构化诊断/反思消费）。"""
        text = str(result.get("result") or "")
        low = text.lower()
        if issue:
            return "CONTRACT_VIOLATION"
        if "timeout" in low or "超时" in text:
            return "TIMEOUT"
        m = re.search(r"HTTP[ _-]?(\d{3})", text, re.I)
        if m:
            return f"HTTP_{m.group(1)}"
        if any(sig in text for sig in (
            "No URL", "no url", "empty", "filtered", "无结果", "没有找到",
            "未找到", "No relevant", "not found",
        )):
            return "SEARCH_EMPTY"
        if capability == "code_execution" and any(sig in text for sig in (
            "SyntaxError", "ModuleNotFoundError", "Traceback", "No module named",
            "Script exited with code", "No valid code", "HTML incomplete",
        )):
            return "CODE_RUNTIME_ERROR"
        if capability == "report_generator" and "too short" in low:
            return "OUTPUT_TOO_SHORT"
        return "GENERIC_FAILURE"

    @staticmethod
    def _failure_suggestion(capability: str, error_type: str, alt: dict | None) -> str:
        """确定性修复建议：反思重做时作为优先修复方向（suggestion 字段）。"""
        if alt is not None:
            hint = (
                "（优先结构化数据源）"
                if "结构化" in str(alt.get("instruction") or "")
                else ""
            )
            return f"改用 {alt.get('capability')} 替代 {capability}{hint}"
        if error_type.startswith("HTTP_"):
            return (
                "检查 URL 可达性，换备用 URL/镜像源；"
                "金融数据优先改用结构化数据源（东方财富/SEC/巨潮）"
            )
        if error_type == "SEARCH_EMPTY":
            return (
                "更换查询词并增加限定（site: 官方域名、年份、具体指标词）；"
                "禁止原样重复上次查询"
            )
        if error_type == "CODE_RUNTIME_ERROR":
            return "修复语法/依赖错误，简化实现，避免不可用的外部库"
        if error_type == "OUTPUT_TOO_SHORT":
            return "裁剪输入提示词后重试（保留摘要，去掉原文引用），max_tokens 不缩小"
        if error_type == "CONTRACT_VIOLATION":
            return "按输出契约格式重新生成结果"
        if error_type == "TIMEOUT":
            return "减少单步工作量或拆分步骤，避免超时"
        return "按失败原因修正实现；若无法恢复，改用已有结构化数据源或模型知识（标注来源）"

    def _build_step_diagnosis(
        self, step: dict, result: dict, issue: str,
        tried: list[str], alt: dict | None, alt_result: dict | None,
        task_id: str,
    ):
        """组装 StepDiagnosis：替换结果已知后才落盘。"""
        from step_diagnosis import StepDiagnosis
        capability = str(step.get("capability") or "")
        error_type = self._classify_step_error(capability, result, issue)
        return StepDiagnosis(
            step_id=str(step.get("step_id") or "?"),
            capability=capability,
            error_type=error_type,
            tried_alternatives=tried or ["重试"],
            suggestion=self._failure_suggestion(capability, error_type, alt),
            timestamp=self._now_iso(),
            replacement_step_id=str(alt.get("step_id") or "") if alt else "",
            replacement_outcome=str((alt_result or {}).get("status") or "") if alt else "",
            error_snippet=str(result.get("result") or "")[:200],
        )

    @staticmethod
    def _diagnosis_for_step(task_id: str, step_id: str):
        """取某步骤最新失败诊断（无则 None）。"""
        try:
            from step_diagnosis import read_step_failures
            diags = read_step_failures(task_id)
            for d in reversed(diags):
                if d.step_id == step_id:
                    return d
        except Exception:
            pass
        return None

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
# 财务图表归一化/分组纯函数
# make_charts 模板脚本经 __REPO_ROOT__ sys.path 引入；回归测试直接调用。
# ─────────────────────────────────────────────

_FINANCIAL_ENTITY_PREFIX_RE = re.compile(r"^([\u4e00-\u9fff]{2,8}?)(?=\d{4}年)")
_FINANCIAL_YEAR_PREFIX_RE = re.compile(r"^\d{4}年")


def _normalize_financial_metric(label: str) -> str:
    """从财务标签提取指标名，供图表按指标分组。

    多实体标签形如"宁德时代2025年营收"：先剥离实体前缀（仅当后面紧跟
    "YYYY年"时剥离），再剥离"YYYY年"；普通中文标签（无年份无实体）原样返回。
    """
    s = str(label or "")
    s = _FINANCIAL_ENTITY_PREFIX_RE.sub("", s)
    s = _FINANCIAL_YEAR_PREFIX_RE.sub("", s)
    return s


def _group_financial_rows(rows) -> dict:
    """把 clean_chart_data 的 market_data 行按 (实体, 指标) 分组。

    返回 {指标名: {实体名: [(年份, 值), ...]}}；实体名从标签前缀提取，
    单实体/无实体标签实体名为空字符串；各序列按年份排序并去重 (年份, 值)。
    """
    groups: dict[str, dict[str, list[tuple[int, float]]]] = {}
    for m in rows or []:
        yr = m.get("year")
        label = str(m.get("label") or "")
        metric = _normalize_financial_metric(label)
        if not yr or not metric or len(metric) < 2:
            continue
        em = _FINANCIAL_ENTITY_PREFIX_RE.match(label)
        entity = em.group(1) if em else ""
        pts = groups.setdefault(metric, {}).setdefault(entity, [])
        pt = (int(yr), float(m.get("value") or 0))
        if pt not in pts:
            pts.append(pt)
    return {
        metric: {ent: sorted(pts) for ent, pts in by_entity.items()}
        for metric, by_entity in groups.items()
    }


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
            project = data.get("project") or "default"

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
            def _run_task(tid, g, ctx, ar, tpl_steps, uid, proj):
                try:
                    result = orch.run(
                        tid, g, ctx, auto_run=ar, template_steps=tpl_steps,
                        user_id=uid, project=proj,
                    )
                    result["project"] = proj
                    orch._messaging.publish("orchestrator:response", result)
                except Exception as e:
                    logger.error("Task %s failed: %s", tid, e)
                    orch._messaging.publish("orchestrator:response",
                                            {"task_id": tid, "status": "FAILED",
                                             "report": str(e), "project": proj})
            threading.Thread(
                target=_run_task,
                args=(
                    task_id, goal, context, auto_run,
                    data.get("template_steps"), data.get("user_id", ""),
                    project,
                ),
                daemon=True,
            ).start()

        except Exception as e:
            logger.error("Message error: %s", e)


if __name__ == "__main__":
    main()
