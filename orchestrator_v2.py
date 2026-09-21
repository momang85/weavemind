"""Orchestrator V2 — clean, linear, push-progress-enabled.

Reuses all existing components: llm_client, common, async_worker_base, memory_manager,
critic_agent, ws_helpers. No complex state machine — just: plan → dispatch → collect → report.

This is the active orchestrator (legacy orchestrator.py was removed in the architecture cleanup).
"""

import csv
import errno
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
from dataclasses import dataclass
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

import db_paths
from common import AgentRegistry, MessagingClient, RedisAgentRegistry
import chart_assembly
from charts_pipeline import ChartPipelineMixin
from structured_pipeline import StructuredPipelineMixin
from llm_client import LLMClient, get_usage_stats
from checkpointer import (
    clear_checkpoint,
    goal_hash as _checkpoint_goal_hash,
    is_task_completed,
    load_checkpoint,
    save_checkpoint,
)

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
# L01：三层身份契约（根任务/步骤/派发）跨进程传递
from task_context import make_context
# V1：修订稿比较（硬约束 + 验收结果，不用字数）
from report_quality import compare_versions
from report_quality import disclaimer_instruction

# Redis 客户端统一关掉 redis-py 的内建重试：默认重试会把 socket_connect_timeout
# 叠成 26~48 秒才失败（实测 127.0.0.1 26s / localhost 48s），Redis 不在时
# 表现为"服务没崩但处处卡"。NoBackoff + 0 次重试 → 稳定 2 秒内失败并可被上层降级。
from redis.backoff import NoBackoff as _NoBackoff  # noqa: E402
from redis.retry import Retry as _Retry  # noqa: E402

_NO_REDIS_RETRY = _Retry(_NoBackoff(), 0)

logger = logging.getLogger(__name__)

# 来源纪律红线（#6 幻觉来源闭环）：追加到验收缺口文本后，让反思/重做指令
# 直接约束来源纪律。实测根因：报告编造 Gartner/TrendForce 等检索结果中
# 不存在的来源，参考来源清单用 [n] 引用标记代替真实 URL。
_SOURCE_DISCIPLINE_REDLINES = (
    "\n来源纪律红线（必须遵守）："
    "1) 只引用【上一步检索结果】中真实存在的来源 URL，"
    "检索结果中没有的来源（如 Gartner/IDC/TrendForce 原文）一律不得引用；"
    "2) 参考来源清单逐条给出真实 URL 与标题，不得用 [n] 引用标记代替链接；"
    "3) 检索无法核实的数据明确标注『基于模型知识，未经核实』，"
    "并在数据要点中剔除该行。"
)

# P0-2：派生/生成的 Python 以 subprocess 执行时，不得把含密钥的环境变量交给子进程。
# 与 code_sandbox.SECRET_PREFIXES 保持一致，并显式纳入 planner 密钥段。
_SECRET_ENV_PREFIXES = ("LLM_", "OPENAI_", "EMBEDDING_", "PLANNER_LLM_",
                        "API_KEY", "SERPAPI", "TOKEN", "SECRET")


class ReviewRequiredError(RuntimeError):
    """银行口径下"必需评审"未完成：按策略拒绝继续，不得当作评审通过。

    个人模式不抛此异常——改为按**根任务**记降级（`_review_state(task_id)` 的
    `degraded_reason`）并在交付物里注明需人工复核。身份模式配置非法/不可判定时同样
    抛此异常：配置坏了不能默认按个人模式放行。
    """


# 评审策略版本：PASS 与它绑定，策略变了旧 PASS 不再复用于新计划（M0-b）
REVIEW_POLICY_VERSION = "review-policy/v1"


class TaskCancelled(RuntimeError):
    """用户请求停止且**已放弃等待在飞调用**：由 run() 的取消路径收尾（M0-c）。

    `ticket_settled=True`（R2）表示"这次调用预留的那张预算票据**已经**由放弃等待的
    那一方转成了待对账"——调用方不得再对它结算，否则一张票据会被记两次
    （unsettled 一次、settled 一次），真实状态被假平衡掩盖。
    """

    def __init__(self, message: str = "", *, ticket_settled: bool = False):
        super().__init__(message)
        self.ticket_settled = bool(ticket_settled)


# 等待步骤结果的四类结果（M0-c）：此前取消/超时/协议错误都返回 None，
# 上层一律记成 "Step X timed out"——取消被写成超时、畸形回包被写成超时，
# 日志、诊断与状态三方对不上账。
WAIT_RESULT = "result"
WAIT_CANCEL = "cancel"
WAIT_TIMEOUT = "timeout"
WAIT_PROTOCOL = "protocol"


@dataclass
class WaitOutcome:
    """一次等待的分类结果：kind 决定上层怎么记（成功/取消/超时/协议错误）。"""

    kind: str
    result: dict | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == WAIT_RESULT

# 允许的评审裁决：只认这两个。其余（含缺失、拼写错误、别的进程塞进来的裁决）
# 一律按"评审未完成"处理——既不当通过，也不进付费修订分支。
REVIEW_VERDICTS = ("PASS", "FAIL")


def _sanitized_process_env(base: dict | None = None) -> dict:
    """返回一份剥离密钥变量的子进程环境（保留 PATH/PYTHON* 等必需运行时变量）。"""
    src = base if base is not None else os.environ
    return {k: v for k, v in src.items()
            if not any(p in k.upper() for p in _SECRET_ENV_PREFIXES)}


def plan_cache_key_for(goal: str, scope: str = "") -> str:
    """规划缓存键：目标哈希 + 作用域哈希。

    P1-3：只按目标文本哈希会让"不同项目/不同用户的同一句目标"互相命中
    （缓存串台）。作用域由调用方给出（project:user），空作用域也参与哈希，
    避免与旧键混用。
    """
    return (
        "plan:"
        + hashlib.sha256(str(goal).encode("utf-8")).hexdigest()
        + ":"
        + hashlib.sha256(str(scope or "").encode("utf-8")).hexdigest()[:12]
    )


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
    "cninfo_annual": "巨潮资讯网（A股年报）",
}

# 注：统计/排行类目标的判定统一在 task_intent.market_intent（唯一来源）。
# 此处曾有一份 _STATISTICAL_GOAL_KEYWORDS 与该模块重复，导致两处规则分叉
# （"合计/汇总"被当成全市场信号）——已移除。

# 已选事实注入块的展开上限（条数）：块本身有界，避免把报告步骤的输入撑大
_FACTS_BLOCK_MAX_ROWS = 40

# F2 定向取证：研究路径两个抓取步骤的**角色**（年报正文页 / 附注风险页）。
# 角色由 step_id 推出（不新增契约字段），供 `_pick_fetch_url` 分流候选。
_FETCH_ROLE_BY_STEP = {"2": "annual_report", "2b": "notes"}
_FETCH_ROLE_KW = {
    "annual_report": ("年报", "年度报告", "经营情况讨论", "管理层讨论", "经营回顾",
                      "经营情况", "annual report", "10-k", "20-f", "公告"),
    "notes": ("附注", "现金流量", "风险因素", "风险提示", "财务数据", "财务报表",
              "分部", "notes", "cash flow", "risk factor"),
}

# 叙事证据注入块上限（F2）：条数与单条字数都有界，避免把报告输入撑大
_NARRATIVE_MAX_RECORDS = 6
_NARRATIVE_SNIPPET_CHARS = 300

# 前序结果注入的结构化限长（F1）：JSON 按结构裁剪后**仍是合法 JSON**，
# 下游才能把它当数据识别；散文另有字符上限。
_STEP_JSON_MAX_ITEMS = 20
_STEP_JSON_MAX_FIELD = 400
_STEP_TEXT_MAX_CHARS = 2500


def _parse_json_text(text: str):
    """整串能解析成 dict/list 就返回对象，否则 None（不做"猜半截 JSON"）。"""
    t = str(text or "").strip()
    if not t or t[0] not in "[{":
        return None
    try:
        obj = json.loads(t)
    except Exception:
        return None
    return obj if isinstance(obj, (dict, list)) else None


def _trim_json_value(obj, *, max_items: int = _STEP_JSON_MAX_ITEMS,
                     max_field: int = _STEP_JSON_MAX_FIELD):
    """按结构限长：列表保留前 N 条、长字符串截断；返回 `(新对象, 是否有裁剪)`。"""
    changed = False

    def _walk(node):
        nonlocal changed
        if isinstance(node, list):
            if len(node) > max_items:
                changed = True
            return [_walk(x) for x in node[:max_items]]
        if isinstance(node, dict):
            return {str(k): _walk(v) for k, v in node.items()}
        if isinstance(node, str) and len(node) > max_field:
            changed = True
            return node[:max_field]
        return node

    return _walk(obj), changed


def _is_json_text(text: str) -> bool:
    """整串是否为结构化 JSON（数据非指令，注入扫描豁免）。"""
    return _parse_json_text(text) is not None


def _isolate_injection_lines(task_id: str, text: str, *, stage: str = "",
                             dep_step_id: str = "",
                             content_type: str = "markdown") -> str:
    """把命中注入签名的**那一行**替换成隔离标记，其余原文保留。

    为什么逐行：整段替换会把没问题的分析一起吃掉（架构复核 F1：含 `` `CNY` `` 的正常
    研报段落曾被整段替换成过滤标记，再被报告 worker 补进正文）。结构化 JSON 是数据
    而非指令，整体豁免（既有设计）。
    """
    t = str(text or "")
    if not t or _is_json_text(t):
        return t
    try:
        from security import scan_lines
        hits = scan_lines(t)
    except Exception:
        return t
    if not hits:
        return t
    lines = t.splitlines()
    for idx, label, _rule_id in hits:
        if 0 <= idx < len(lines):
            lines[idx] = f"[已隔离可疑内容：{label}]"
    out = "\n".join(lines)
    try:
        import content_filter_log as _cfl
        for _idx, label, rule_id in hits:
            _cfl.append_event(task_id, rule_id=rule_id, stage=stage,
                              dep_step_id=str(dep_step_id or ""),
                              content_type=content_type,
                              chars_before=len(t), chars_after=len(out),
                              isolation="line", reason=label)
    except Exception as exc:                     # noqa: BLE001 - 诊断不得拖垮主线
        logger.warning("内容过滤诊断落盘失败（task=%s）：%s", task_id, str(exc)[:120])
    return out


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
    + disclaimer_instruction() +
    "4. 合规红线：不得给出具体投资组合配比（如'30%某股+70%某资产'）、"
    "不得给出预期收益率/年化收益数值承诺；如涉及资产配置，只允许描述"
    "常见配置思路与风险框架，并强调'不构成投资建议'。\n"
    "5. 来源声明词汇表（硬约束）：任何'数据来源/来源'声明或表格来源列，"
    "只允许以下三种形式之一：① [n] 编号（n 对应 '## 参考来源' 清单序号）；"
    "② 参考来源清单中的媒体名/标题（逐字引用）；"
    "③ '基于模型知识，未在本次检索中验证'。"
    "禁止把正文短语、指标名、行业术语、观点片段当来源名"
    "（如'指出'、'主要挑战'、'能量密度提升空间有限'这类不是来源）；"
    "数据无对应检索来源时，必须写形式③，禁止编造任何来源名。"
)

# 报告**内容**要求（与上面的格式要求分开）：研究类报告不能只有"口径自证 + 来源清单"，
# 必须把已选事实讲成有经济含义的分析。实机教训（ui-2084c2c9cc）：18 条事实 + 3 条同比
# 全部注入过模型，但指令只说"给出结论"，报告最终只写了 2 个数字、没有任何分析段落。
# 这里给一份**可检查的骨架**：小节名固定、每项都要落到注入块里的数字。
_REPORT_ANALYSIS_REQUIREMENTS = (
    "\n\n[报告内容要求]（强制，研究/财务类报告必须遵守；数字只能来自上面的"
    "[已选事实]块，比率与同比已由系统算好，不得自行换算或补算）\n"
    "1. 关键数据一览表：正文必须有一张表，列为"
    "『指标 | 上一期 | 本期 | 同比 | 口径 | 来源』，逐行覆盖全部必需指标"
    "与系统给出的派生指标（同比、比率），每个数值都要与[已选事实]块一致。\n"
    "2. 逐项同比解读：对每个核心指标写一句『增长/下降多少、方向如何』，"
    "并说明本期与上期的差异来自哪里（能说清就说，说不清就写『原因未在本次资料中体现』）。\n"
    "3. 盈利质量：给出归母净利率（与毛利率，如有）的水平与变化，并说明含义；"
    "**变化用百分点表述**（如'上升 0.3 个百分点'），不要写成百分比变化。\n"
    "4. 现金流质量：给出经营活动现金流对归母净利润的覆盖（百分比），并说明含义。"
    "**只有当两者都为正**时，才可以说'当期利润有现金支撑'；任一为负（亏损或经营现金"
    "净流出）时必须分别描述为亏损/净流出，不得用'覆盖倍数'暗示质量良好。"
    "分母为 0 或系统标注'不可算'的，如实写'不可算'，不得硬算或省略不提。\n"
    "5. 结构与杠杆：给出资产负债率及其变化（如有总资产/总负债事实）。\n"
    "6. 风险与结论：基于上述事实写 2-4 条风险与一段结论；"
    "不得出现[已选事实]块之外的数字，不得给投资建议或收益承诺。"
    "**没有行业基准或同业数据时，不得写'健康/优秀/偏低/低风险'这类评级式判断**，"
    "只能陈述水平与变化本身，并写明需要什么资料才能判断。\n"
    "7. 口径纪律：比率与同比的**归属口径**必须写清（分子是归母净利润、经营现金流是"
    "合并口径；报表口径以块内声明为准）；跨口径比较要注明差异。\n"
    "8. 缺口纪律：块内标为缺口/不可算的项，必须在正文里如实写明，"
    "不得用模型知识补数、不得跳过不提。"
)


# 阅读视角（F2）：两种视角复用**同一底稿**，只是"要回答的问题"不同。
# 视角只认用户声明（契约字段），不按公司名或机构名自动套用——银行做权益投研时
# 仍用权益视角，不能因为机构名称就切成信用分析。
_PERSPECTIVE_REQUIREMENTS = {
    "equity": (
        "\n[阅读视角：投研] 在骨架之外还要回答：增长来源（量/价/结构，能说清就说）、"
        "盈利变化与盈利质量、现金流与利润的差异、关键假设与下一步要查的材料。"
        "没有同业与行情数据时不得给出估值或目标价。\n"
    ),
    "bank_corporate": (
        "\n[阅读视角：银行对公客户研究] 在骨架之外还要回答：经营现金的来源与波动、"
        "客户经营风险、需要向客户询问的事项（债务到期结构、受限资金、对外担保、"
        "授信与利息负担）。**偿债结论要等债务与利息材料齐备**，本次不得下偿债能力结论。\n"
    ),
}


def perspective_requirements(perspective: str) -> str:
    """视角 → 报告步骤的追加要求（未声明/未知视角返回空串，不硬套）。"""
    return _PERSPECTIVE_REQUIREMENTS.get(str(perspective or "").strip(), "")


# P0：产物文件注入白名单——仅数据类文本素材（.md/.txt/.csv/.json）读取正文注入；
# HTML/JS/CSS/PY/图片等源码或二进制一律跳过正文，只保留"文件存在 + 路径"提示，
# 防止报告"抄产物"把落盘的 index.html 等源码原文混入报告 prompt。
_ARTIFACT_TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json"}

# make_charts 语义图 → chart_manifest.json 回填时的文件名推断关键词
# （中文语义 + 英文 token；无 chart_data.json 规格，section_hint 保持为空）


def _loads_json_loose(text: str) -> dict:
    """先严格解析，失败后允许字符串内未转义控制字符（LLM 常在长指令中插入字面换行）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text, strict=False)


def filter_dead_search_results(parsed: list, timeout: float = 4) -> tuple[list, int]:
    """2c 死链治理：剔除 URL 明确 dead 的搜索结果项（探测内部自带 1 次
    重试防误杀），减少报告"来源链接失效"。返回 (过滤后列表, 剔除条数)。

    任何探测异常静默放行全部结果——检索可用性优先，不伤任务主线；
    URL_HEALTH_CHECK=0 时关闭过滤。"""
    if os.environ.get("URL_HEALTH_CHECK", "1") == "0":
        return parsed, 0
    try:
        src_urls = [
            str((it or {}).get("url") or "").strip()
            for it in parsed
            if str((it or {}).get("url") or "").strip().startswith("http")
        ]
        if not src_urls:
            return parsed, 0
        from adapters.url_health import check_urls
        health = check_urls(src_urls, timeout=timeout)
        kept = [
            it for it in parsed
            if health.get(
                str((it or {}).get("url") or "").strip(), "alive",
            ) != "dead"
        ]
        return kept, len(parsed) - len(kept)
    except Exception as exc:
        logger.warning("web_search dead-link filter failed: %s", exc)
        return parsed, 0

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
17. 单步耗时预算（LLM 慢模型/长文生成场景的硬约束）：
    - 长文生成步骤（content_summary / report_generator 输出完整报告）timeout 设 600-900；
    - 其余步骤 timeout 设 120-300，web_search/web_fetch 设 60-120；
    - 预计单次生成超过 5 分钟的内容必须拆成多个更小的步骤（例如先出要点框架，
      再分节撰写），禁止把全部写作压进一个步骤；
    - 不要给任何步骤写 timeout:60（那是示例占位，不是规范值）

Output ONLY this JSON with no extra text:
{"steps":[{"step_id":"1","capability":"web_search","instruction":"search for house price dataset","depends_on":[],"timeout":120}]}"""

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
    ("_critic_timeout", "critic_timeout", int, 120, lambda v: max(10, v)),
    ("_max_retry", "max_retry", int, 2, lambda v: max(0, v)),
    ("_replan_depth", "replan_depth", int, 2, lambda v: max(0, v)),
    ("_task_timeout", "task_timeout", int, 300, lambda v: max(1, v)),
    ("_stall_timeout", "stall_timeout", int, 60, lambda v: max(5, v)),
    ("_plan_confirm_timeout", "plan_confirm_timeout", int, 300, lambda v: max(30, v)),
)


class _BudgetScope:
    """一次计费调用的票据生命周期（R2）：预留 → 结算/转待对账，三选一。

    存在的理由：此前票据的结算散在调用方的 `finally` 里，而取消路径又在别处把
    **另一张虚构票据**标成待对账——结果是"取消后这次调用停没停"在账本里读不出来。
    把生命周期收成一个上下文管理器后，出口只有三条，且互斥：

    - 正常返回 → `settle(ok=True)`；
    - `TaskCancelled(ticket_settled=True)`（放弃等待方已把真票据转待对账）→ 不再动它；
    - 其它异常 → `settle(ok=False)`（失败也花了钱，不能当没发生）。
    """

    def __init__(self, orch, task_id: str, stage: str, detail: dict):
        self._orch = orch
        self.task_id = task_id
        self.stage = stage
        self.detail = dict(detail or {})
        self.ticket = ""

    def __enter__(self) -> str:
        self.ticket = self._orch._budget_reserve(self.task_id, self.stage,
                                                 detail=self.detail)
        return self.ticket

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self.ticket:
            return False
        if isinstance(exc, TaskCancelled) and getattr(exc, "ticket_settled", False):
            # 票据已转待对账（可能仍在计费）：不得再结算
            return False
        note = f"{self.stage}:{'failed' if exc_type else 'ok'}"
        self._orch._budget_settle(self.task_id, self.ticket, ok=exc_type is None,
                                  note=note)
        return False


class OrchestratorV2(ChartPipelineMixin, StructuredPipelineMixin):
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
        # B1：反思轮数预算按链路分级（排行 2 / 金融 3 / 通用 3），
        # config.json system.reflection_budget 可覆盖
        self._reflection_budget = {"ranking": 2, "financial": 3, "general": 3}
        # B3：单轮反思重做步数上限（默认 2，可用 REFLECT_MAX_REDO_STEPS 或
        # config.json system.reflect_max_redo_steps 覆盖）
        self._max_redo_steps = max(
            1, int(os.environ.get("REFLECT_MAX_REDO_STEPS", "2") or 2)
        )
        self._plan_confirm_timeout = 300
        # _stall_timeout 的默认值唯一来源是热重载表（stall_timeout=60）；
        # 此处 300 的死赋值删除——同一旋钮两处默认值属时间轴 bug 类
        self._stall_timeout = 60
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
            # B1：反思预算热更新（dict 段，逐键校验）
            _rb = _sys.get('reflection_budget')
            if isinstance(_rb, dict):
                for _k in ("ranking", "financial", "general"):
                    _v = _rb.get(_k)
                    if isinstance(_v, int) and _v >= 1:
                        self._reflection_budget[_k] = _v
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
        self._sqlite_reg = AgentRegistry(db_paths.resolve_db_path())
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
            # 重试必须显式关掉，否则 redis-py 的默认重试会把 5s 连接超时
            # 叠成分钟级才失败（见 _NO_REDIS_RETRY）
            retry=_NO_REDIS_RETRY,
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
            direct_steps = self._normalize_steps(direct)
            # M0-b：确定性直出计划同样受评审策略约束（跳过 Critic 不等于免评审）
            self._require_review_or_refuse(
                task_id, "确定性直出计划未经过 Critic 评审", plan=direct_steps)
            return direct_steps
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
                from ws_helpers import call_with_heartbeat, phase_begin, phase_end
                # B2：同目标重复规划直接命中缓存（LLM_CACHE_TTL 开启时生效），
                # 键含目标哈希，同一会话追问重复提交不会重复花规划 token。
                # P1-3：键必须带作用域——只按目标文本哈希会让"不同项目/不同用户
                # 的同一句目标"互相命中（缓存串台），项目与用户各加一段。
                _scope = "{}:{}".format(
                    str((getattr(self, "_task_projects", {}) or {}).get(task_id) or ""),
                    str((getattr(self, "_task_user_ids", {}) or {}).get(task_id) or ""),
                )
                plan_cache_key = plan_cache_key_for(goal, _scope)
                phase_begin(self._messaging, task_id, "规划", attempt=attempt + 1)
                try:
                    # 规划调用可能阻塞数分钟：放进工作线程并按时心跳，
                    # 控制台因此显示"规划进行中（已等待 Ns）"而不是静默；
                    # deadline 给出调用级时间预算，预算耗尽不再无谓重试/切备用
                    _plan_budget = float(os.environ.get("WM_PLAN_BUDGET_SECONDS", "180") or 180)
                    _plan_deadline = time.time() + _plan_budget
                    # M0-e：规划调用也要在根任务预算里预留（重试/降级共享同一份剩余额度）
                    # R2：预留与结算走同一个 scope——取消时票据由 helper 转待对账，
                    # 出口**不**再结算（此前 finally 无条件结算成 ok=True，
                    # 与虚构的 inflight 票据凑成假平衡）
                    with self._budget_scope(
                            task_id, "plan",
                            detail={"stage": "规划", "attempt": attempt + 1}) as _ticket:
                        # M0-c：规划调用在可取消的等待里跑——端点退化时"停止"也要能立刻生效
                        raw = self._call_llm_cancellable(
                            task_id, "规划", call_with_heartbeat,
                            self._messaging, task_id, "规划",
                            self._planner_llm.call,
                            get_prompt("planner", PLANNER_SYSTEM, goal=goal),
                            attempt_prompt, expect_json=True, max_tokens=8192,
                            # B1：规划/反思/评审统一走 planner 用途模型
                            usage="plan", cache_key=plan_cache_key,
                            deadline=_plan_deadline,
                            ticket=_ticket,
                        )
                    phase_end(self._messaging, task_id, "规划", ok=True,
                              detail=f"规划完成（第 {attempt + 1} 次尝试）")
                except Exception as exc:
                    phase_end(self._messaging, task_id, "规划", ok=False,
                              detail=f"规划第 {attempt + 1} 次尝试失败")
                    if getattr(exc, "budget_exhausted", False):
                        push_progress(self._messaging, task_id, "log", {
                            "type": "warning", "agent": "orchestrator",
                            "message": (f"规划时间预算（{_plan_budget:.0f}s）已耗尽，"
                                        "转入确定性降级规划"),
                            "timestamp": self._now_iso(),
                        })
                        break
                    raise
                plan_data = self._parse_plan_response(raw)
                break
            except TaskCancelled:
                # 取消不是"规划失败"：不重试、不降级，直接交给 run() 的取消收尾
                raise
            except Exception as e:
                last_error = e
                try:
                    from llm_client import describe_error
                    _err_cls = describe_error(e)
                except Exception:
                    _err_cls = type(e).__name__
                logger.warning("Plan attempt %d failed（%s / %s）",
                               attempt + 1, _err_cls, type(e).__name__)
                push_progress(self._messaging, task_id, "log",
                              {"type": "error", "agent": "orchestrator",
                               "message": f"规划第 {attempt + 1} 次尝试失败：{_err_cls}，重试中",
                               "timestamp": self._now_iso()})
                if attempt == 0:
                    time.sleep(3)  # 瞬断退避：给双端点恢复留出窗口
        if plan_data is None:
            # 进度流会随 logs_json 落库并显示给用户：只写**脱敏类别**，不写异常原文
            # （LLMCallError 可能带上游响应片段）
            try:
                from llm_client import describe_error
                _err_cls = describe_error(last_error)
            except Exception:
                _err_cls = type(last_error).__name__
            logger.error("Plan failed（%s / %s）", _err_cls, type(last_error).__name__)
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": "orchestrator",
                           "message": f"Plan failed: {_err_cls}", "timestamp": self._now_iso()})
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
        if steps:
            if self._critic_enabled:
                try:
                    steps = self._review_plan(goal, steps, task_id)
                except ReviewRequiredError as exc:
                    # V2-2：银行口径下必需评审未完成 → 拒绝继续（不把超时当通过）
                    push_progress(self._messaging, task_id, "log",
                                  {"type": "error", "agent": "critic",
                                   "message": f"必需评审未完成，按策略拒绝继续：{exc}",
                                   "timestamp": self._now_iso()})
                    raise
            else:
                # M0-b：critic 关掉也要走同一套策略——银行口径没有任何绑定 PASS，
                # 不能靠"配置里关了评审"就把必需评审变成可选项
                self._require_review_or_refuse(
                    task_id, "critic 已关闭（system.critic=false），本计划没有评审", plan=steps)
            _degraded = str(self._review_state(task_id).get("degraded_reason") or "")
            if _degraded:
                # 个人模式：评审未完成/降级**如实标注**，不假装通过；银行模式在
                # _review_plan 内直接拒绝（抛 ReviewRequiredError）
                push_progress(self._messaging, task_id, "log",
                              {"type": "review", "agent": "critic",
                               "message": f"未完成评审（降级）：{_degraded}——"
                                          "计划按原始草案继续，交付物需人工复核",
                               "timestamp": self._now_iso()})
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

    def _fixed_research_plan(self, task_id: str, goal: str) -> list[dict] | None:
        """公司研究的**固定步骤序列**（代码内模板，不改 templates.json）。

        命中条件：**表单落库的研究契约**（`research_request`）且 `facts.research_shaped`
        ——有主体、≥2 个期间、已声明口径。只认落库契约不认自由文本解析：实测
        `_extract_company` 会把"2023 与 2024 两个年度"里的"两个"当主体，据此选路径
        等于让路径依赖一个会认错主体的解析（A 批"身份不猜"的延伸）。自由文本仍走
        通用规划器，并由 A 批的交付硬门槛兜底。

        命中后：取来源与事实（结构化预载 + 搜索）→ 解释已选事实 → 出报告；
        不经过 LLM 规划，因此也不花那次无预算的路由调用。返回 None 表示不适用。
        """
        try:
            from facts import research_shaped
            from working_paper_export import resolve_request
            request, _candidates, source = resolve_request(task_id, goal, {}, None)
        except Exception as exc:
            logger.warning("固定研究路径判据读取失败（task=%s）：%s", task_id, str(exc)[:140])
            return None
        if source != "stored" or not research_shaped(request):
            return None
        return self._research_steps(request)

    @staticmethod
    def _research_steps(request) -> list[dict]:
        """固定研究步骤（能力复用既有 worker，不新造执行体）。

        超时按**流式下的真实生成时长**给（实机实测）：报告类步骤一次生成可到 5 分钟
        （8192 tokens 中文），而 `_dispatch` 的等待下限是 300s——给 180/300 会让步骤在
        生成中途被判超时、随后进入重做循环。这里显式放宽，让长生成能跑完。
        """
        from facts import metric_label
        years = sorted({int(y) for y in (request.periods or [])})
        span = "、".join(str(y) for y in years)
        who = str(request.company or request.company_id or "目标公司")
        code = f"（{request.company_id}）" if request.company_id else ""
        metrics = "、".join(metric_label(m) for m in (request.required_metrics or []))
        perspective = str(getattr(request, "perspective", "") or "")
        perspective_note = perspective_requirements(perspective)
        contract_note = (
            f"研究契约（以此为准，不得替换主体）：公司 {who}{code}；"
            f"期间 {span}；报表口径 {request.caliber}；"
            f"资料截至 {request.as_of or '未声明'}；必需指标 {metrics}。"
        )
        return [
            {
                "step_id": "1",
                "capability": "web_search",
                "instruction": (
                    f"检索 {who}{code} 的年报与财务数据的权威来源（优先公司公告/交易所/"
                    f"官方年报）；其中至少一条要指向 {span} 的**发行人年报/公告**"
                    f"（经营情况讨论与分析、管理层讨论、财务附注、风险因素），"
                    f"返回含原始 URL 的结果列表。{contract_note}"
                ),
                "timeout": 180,
            },
            {
                "step_id": "2",
                "capability": "web_fetch",
                "instruction": (
                    f"从步骤1结果中选取与 {span} 匹配的**发行人年报/公告正文页**"
                    f"（经营情况讨论与分析、管理层讨论、经营回顾），抓取完整正文并保留"
                    f"小节标题、原始 URL 与全部数字（年份、金额、币种、单位、报表口径）；"
                    f"主链接失败则换备用链接。{contract_note}"
                ),
                "timeout": 300,
            },
            {
                # F2 定向取证：第二个抓取步骤，取与步骤 2 **不同**的一页（附注/风险/数据）。
                # 有界：不新增搜索步骤、不循环；步骤1没有合适候选时 worker 直接返回
                # failed（指令里没有 URL，不联网），缺口照实记，不编 URL。
                # `optional`：第二个来源取不到**不算任务失败**（缺资料列待核查即可），
                # 不得阻塞分析/报告/打包步骤。
                "step_id": "2b",
                "capability": "web_fetch",
                "instruction": (
                    f"抓取与步骤2**不同**的一个链接：优先 {span} 的财务附注、"
                    f"现金流量表附注、风险因素或财务数据页，保留小节标题与原始 URL；"
                    f"若步骤1结果中没有可用的第二来源，直接返回 failed 并说明，"
                    f"不得编造 URL。{contract_note}"
                ),
                "timeout": 300,
                "optional": True,
            },
            {
                "step_id": "3",
                "capability": "content_summary",
                "instruction": (
                    "只解释本次**已选定的事实**：按注入的[已选事实]块逐项说明指标含义、"
                    "期间变化与同比，并给出盈利质量（净利率）与现金流质量（经营现金流对"
                    "净利润的覆盖）的读数。块内列出的缺口要如实写出来源不足的部分，"
                    "不得引入块外数字，不得自行换算或补齐缺失年份。"
                ),
                "timeout": 900,
            },
            {
                "step_id": "4",
                "capability": "report_generator",
                "instruction": (
                    f"生成 {who} 研究报告，按[报告内容要求]的骨架写全："
                    f"①关键数据一览表（指标×期间×同比×口径×来源，覆盖全部必需指标与"
                    f"系统给出的派生指标）；②逐项同比解读；③盈利质量（净利率/毛利率）；"
                    f"④现金流质量（经营现金流对净利润的覆盖）；⑤结构与杠杆（资产负债率）；"
                    f"⑥风险与结论。每个财务数字标注来源位置，只能用[已选事实]块里的数字"
                    f"（含块内的同比与比率），缺口如实写明。{contract_note}"
                    f"{perspective_note}"
                ),
                "timeout": 1200,
            },
        ]

    @staticmethod
    def _dump_llm_calls(task_id: str) -> None:
        """把任务的 LLM 调用形状（阶段/次数/耗时/输入长度/输出上限/错误类别/结束原因）
        写成工作区 `llm_calls.jsonl`（Redis 里是累计的，这里按快照整体重写，
        避免失败路径与收尾各写一次造成重复）。**只落形状**：提示词与响应体不进这里。"""
        try:
            import json as _json
            from llm_client import get_task_llm_calls
            data = get_task_llm_calls(task_id)
            events = list(data.get("events") or [])
            if not events:
                return
            out = Path(task_workspace(task_id)) / "llm_calls.jsonl"
            out.write_text("".join(_json.dumps(ev, ensure_ascii=False) + "\n"
                                   for ev in events), encoding="utf-8")
        except Exception as exc:                 # noqa: BLE001 - 诊断不得拖垮收尾
            logger.warning("调用形状落盘失败（task=%s）：%s", task_id, str(exc)[:140])

    def _salvage_working_paper(self, task_id: str, goal: str,
                               project: str | None = None) -> dict:
        """失败路径专用：把已取到的结构化事实落成可复核底稿（不抛异常）。

        计划为空或正文模型不可用时，工作区里的 financials.json 仍在——底稿是"事实
        可复核"的证据，不该随任务失败一起消失（任务状态仍是 FAILED）。
        """
        try:
            from working_paper_export import write_working_paper
            wp = write_working_paper(task_id, goal, project=project)
        except Exception as exc:                 # noqa: BLE001 - 兜底不得再抛
            logger.warning("失败路径底稿落盘异常（task=%s）：%s", task_id, str(exc)[:140])
            return {"ok": False, "reason": str(exc)[:160]}
        if wp.get("ok"):
            push_progress(self._messaging, task_id, "log",
                          {"type": "info", "agent": "orchestrator",
                           "message": (f"已保留可复核底稿：{wp.get('rows', 0)} 条事实、"
                                       f"{wp.get('derived', 0)} 条同比、"
                                       f"{len(wp.get('gaps') or [])} 项缺口"),
                           "timestamp": self._now_iso()})
        return wp

    @staticmethod
    def _try_pdf_evidence(task_id: str, step: dict, result: dict) -> bool:
        """抓取目标是 PDF 时：解析成带页码正文并入快照（F2′-2）。

        只在**目标 URL 是 .pdf**（或返回字节头是 %PDF）时触发；解析不出文本
        （扫描件/受保护/下载受限）就什么都不写——缺口由证据提取如实报告。
        """
        try:
            import annual_report_pdf as pdf
        except Exception:
            return False
        url = pdf.url_from_instruction(str(step.get("instruction") or ""))
        head = b""
        try:
            parsed = json.loads(str(result.get("result") or ""))
            if isinstance(parsed, dict):
                url = str(parsed.get("url") or url)
                head = str(parsed.get("text") or "")[:8].encode("utf-8", "replace")
        except Exception:
            pass
        if not url or not pdf.looks_like_pdf(url, head):
            return False
        doc = pdf.doc_from_url(url)
        if not doc:
            logger.info("PDF 证据通道未取得正文（按缺口处理，task=%s）：%s",
                        task_id, url[:100])
            return False
        ok = pdf.append_snapshot(task_id, doc)
        if ok:
            logger.info("PDF 证据通道：%s（%d 页，%d 字符）", url[:80],
                        len(doc.get("page_offsets") or []), len(doc.get("text") or ""))
        return ok

    @staticmethod
    def _narrative_evidence_block(task_id: str, goal: str = "") -> str:
        """把**已抓取的年报/公告正文证据**（带小节定位）注入报告/总结步骤（F2）。

        与 `_selected_facts_block`（数字）互补：这里给的是解释"为什么变"的短证据。
        没有证据时返回空串——缺口由报告正文如实写明，不在这里编原因。
        """
        try:
            import narrative_evidence as ne
            data = ne.read(task_id)
            if data is None:
                data = ne.build(task_id, goal=goal)
        except Exception as exc:                 # noqa: BLE001 - 注入失败不拖垮步骤
            logger.warning("叙事证据读取失败（task=%s）：%s", task_id, str(exc)[:140])
            return ""
        # D1：注入给模型的"已取证据"必须是**已准入 + 有正文位置**的记录——排除项
        # （错主体/超截止/期间不符）与身份未知的记录不得冒充已取证据（此前只过滤
        # has_location，excluded 记录会以"已取证据"进入提示词）。
        records = [r for r in (data.get("records") or [])
                   if r.get("has_location")
                   and str(r.get("admission") or "") in ("admitted", "comparison")]
        missing = list(data.get("missing_labels") or [])
        if not records and not missing:
            return ""
        lines = ["\n\n[已取证据]（发行人年报/公告正文的**小节定位**；只能用它解释变化，"
                 "块内没有的原因不得自行推断）"]
        for r in records[:_NARRATIVE_MAX_RECORDS]:
            snip = str(r.get("snippet") or "")[:_NARRATIVE_SNIPPET_CHARS]
            lines.append(f"- {r.get('kind_label') or r.get('kind')}｜{r.get('locator')}｜"
                         f"{r.get('url')}\n  {snip}")
        if missing:
            lines.append("未取得正文定位的类别：" + "、".join(missing)
                         + "。报告中必须写明『需核查……』，不得用模型知识补原因。")
        lines.append("硬规则：解释经营变化只能引用本块证据并写明其小节位置；"
                     "证据不足时如实写『原因未在本次资料中体现』，不得编造因果。")
        return "\n".join(lines)

    @staticmethod
    def _selected_facts_block(task_id: str, goal: str) -> str:
        """把**已选定的事实**注入报告/总结步骤（C 批）。

        与 `_structured_injection`（整张原始财务表）的分工：这里给的是按研究契约
        完整身份选出来、并算过同比与缺口的那一组事实，模型只解释它，不自行挑选、
        换算或补齐缺失年份。没有契约/没有事实时返回空串（不编）。
        """
        try:
            from working_paper_export import build_result
            res = build_result(task_id, goal)
        except Exception as exc:                 # noqa: BLE001 - 注入失败不拖垮步骤
            logger.warning("已选事实注入失败（task=%s）：%s", task_id, str(exc)[:140])
            return ""
        if not res.get("ok"):
            return ""
        rows = list(res.get("rows_detail") or [])
        derived = list(res.get("derived_detail") or [])
        if not rows and not derived:
            return ""
        lines = ["\n\n[已选事实]（按研究契约的完整身份选定；只解释本块内的事实）",
                 "| 指标 | 期间 | 值 | 单位 |", "|---|---|---|---|"]
        for r in rows[:_FACTS_BLOCK_MAX_ROWS]:
            lines.append(f"| {r.get('metric_label') or r.get('metric')} | "
                         f"{r.get('period')} | {r.get('value')} | |")
        for r in derived[:_FACTS_BLOCK_MAX_ROWS]:
            lines.append(f"| {r.get('metric_label') or r.get('metric')} | "
                         f"{r.get('period')} | {r.get('value')} | {r.get('unit') or ''} |")
        hidden = (len(rows) + len(derived)) - min(len(rows), _FACTS_BLOCK_MAX_ROWS) \
            - min(len(derived), _FACTS_BLOCK_MAX_ROWS)
        if hidden > 0:
            lines.append(f"| …（另有 {hidden} 条未展开） | | | |")
        lines.append("说明：同比与比率均由系统按已选事实重算（带公式与输入 fact_id），"
                     "单位 %；报告须逐个给出这些读数。")
        # 口径证据链：口径"是谁声明的、依据是什么"要一并给到模型与报告读者——
        # 口径不能只活在底稿内部，报告里必须能被人工复核
        cal_ev = ""
        for r in rows + derived:
            _cal = str(r.get("caliber") or "")
            _ev = str(r.get("caliber_evidence") or "")
            if _cal and _ev:
                cal_ev = f"{_cal}（{_ev}）"
                break
        if cal_ev:
            lines.append(f"报表口径：{cal_ev}。报告中须注明口径及该依据。")
        else:
            lines.append("报表口径：**未声明**——报告不得声称已知口径，须如实写明这一缺口。")
        gaps = [str(g.get("detail") or g) if isinstance(g, dict) else str(g)
                for g in (res.get("gaps") or [])]
        problems = [str(p.get("detail") or "") if isinstance(p, dict) else str(p)
                    for p in (res.get("problems") or [])]
        if gaps or problems:
            lines.append("\n必须如实写出的缺口：")
            for g in (gaps + problems)[:8]:
                lines.append(f"- {g[:120]}")
        else:
            lines.append("\n本块无缺口记录。")
        lines.append("硬规则：不得引入本块之外的财务数字，不得自行换算或补齐缺失年份；"
                     "缺口按上面列出的内容原样说明。")
        return "\n".join(lines)

    def _step_context_snippet(self, task_id: str, dep_id: str, raw: str,
                              *, max_chars: int = _STEP_TEXT_MAX_CHARS) -> str:
        """前序结果 → 注入片段：**先按内容类型处理，再限长**（架构复核 F1）。

        - 结构化 JSON（dict/list）：按结构裁剪（前 N 条 / 单字段限长），结果**仍是合法
          JSON**——下游 `_format_search_json` 才能把它当数据识别；
        - 长散文：滚动摘要（失败兜底为截断），随后逐行做注入隔离；
        - 任何裁剪都记一条脱敏诊断（`isolation=truncated`，只记长度不记正文）。
        """
        text = str(raw or "")
        if not text:
            return ""
        obj = _parse_json_text(text)
        if obj is not None:
            trimmed, changed = _trim_json_value(obj)
            out = json.dumps(trimmed, ensure_ascii=False)
            if changed or len(out) != len(text):
                self._log_snippet_event(task_id, dep_id, text, out, content_type="json")
            return out                       # JSON 是数据不是指令，不扫描
        if len(text) > 6000 and os.environ.get("ROLLING_SUMMARY", "1") != "0":
            snippet = self._rolling_summarize(text)
        else:
            snippet = text[:max_chars]
        if len(snippet) != len(text):
            self._log_snippet_event(task_id, dep_id, text, snippet,
                                    content_type="markdown")
        return _isolate_injection_lines(task_id, snippet, stage="inject_step_context",
                                        dep_step_id=dep_id, content_type="markdown")

    @staticmethod
    def _log_snippet_event(task_id: str, dep_id: str, before: str, after: str, *,
                           content_type: str, isolation: str = "truncated") -> None:
        """脱敏诊断：只记来源步骤/内容类型/前后长度/隔离状态，不记正文。"""
        try:
            import content_filter_log as _cfl
            _cfl.append_event(task_id, rule_id="", stage="inject_step_context",
                              dep_step_id=str(dep_id or ""), content_type=content_type,
                              chars_before=len(before), chars_after=len(after),
                              isolation=isolation)
        except Exception as exc:                 # noqa: BLE001 - 诊断不得拖垮主线
            logger.warning("内容过滤诊断落盘失败（task=%s）：%s", task_id, str(exc)[:120])

    def _route_template(self, goal: str, task_id: str = "") -> list[dict] | None:
        """T10b：委托 templates_pipeline 模块实现（依赖注入，原主体已搬迁）。

        C 批：研究任务先走固定步骤（契约 → 来源与事实 → 解释已选事实），
        不依赖通用规划器成功；命中即返回。
        """
        fixed = self._fixed_research_plan(task_id, goal)
        if fixed:
            return fixed
        from templates_pipeline import route_template
        return route_template(
            goal, task_id,
            load_templates=self._load_templates,
            direct_plan=self._direct_deliverable_plan,
            keyword_match=self._template_keyword_match,
            messaging=self._messaging,
            now_iso=self._now_iso,
            plan_llm=lambda: self._plan_llm,
        )

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
        """T10b：委托 templates_pipeline 模块实现（依赖注入，原主体已搬迁）。"""
        from templates_pipeline import consolidate_template
        return consolidate_template(
            goal, all_steps, tpl_path, task_id,
            consolidation_key=self._consolidation_key,
            acceptance_passed=self._acceptance_passed,
            count_verified_chain=self._count_verified_chain,
            threshold=self._consolidate_threshold(),
        )

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
        """任务验收是否通过（无验收报告返回 None）。

        M0-d：优先取**选中版本自身的验收**（验收与正文版本绑定）；版本库里没有该版
        证据时退回 acceptance_report.json，两者都没有就是未知（None，不算通过）。
        """
        try:
            from report_version import VersionStore
            from workspace import task_workspace
            ws = task_workspace(task_id)
            adopted = VersionStore(ws, task_id).adopted()
            if adopted is not None:
                acc = adopted.acceptance or {}
                if acc:
                    return str(acc.get("overall") or "") == "pass"
            acc_path = ws / "acceptance_report.json"
            if not acc_path.exists():
                return None
            acc = json.loads(acc_path.read_text(encoding="utf-8"))
            return acc.get("overall") == "pass"
        except Exception:
            return None

    def _sources_fingerprint(self, task_id: str, report_text: str = "") -> str:
        """本轮来源/事实快照指纹（B 批起唯一实现在 `delivery_pipeline`）。"""
        from delivery_pipeline import sources_fingerprint
        return sources_fingerprint(task_id, report_text)

    def _rules_identity(self, task_id: str) -> tuple[str, str]:
        """本次验收的规则版本与指纹（B 批起唯一实现在 `delivery_pipeline`）。"""
        from delivery_pipeline import rules_identity
        return rules_identity(task_id)

    def _acceptance_summary_for_status(self, task_id: str) -> dict | None:
        """**终态判定**用的验收摘要（R0.2）：以选中版本自身的验收为准。

        保留验收记录里的 `overall/gaps/rules_*`（供记忆准入等按原文判断），
        另加 `version_bound`（该验收能否证明属于本正文）与 `needs_reverify`
        （只有旧短 hash）。**未绑定**由 `derive_status` 统一按"有缺口"处理，
        这里不擅自改写 `overall`，避免把"fail"抹成未知。
        """
        try:
            adopted = self._version_store(task_id).adopted()
        except Exception:
            adopted = None
        if adopted is not None:
            acc = dict(adopted.acceptance or {})
            bound = adopted.acceptance_for_this_body()
            if not acc:
                # 选中版本没有自己的验收：仍把文件里的结论带给上层（记忆准入需要知道
                # "验收 fail"），但显式标 version_bound=False —— 状态派生会按有缺口处理
                summary = self._read_acceptance_summary(task_id)
                if summary is None:
                    return {"overall": "", "gaps": ["选中版本没有对应它自身的验收（未知）"],
                            "rules_version": "", "rules_fingerprint": "",
                            "report_sha256": adopted.version_id, "version_bound": False}
                summary = dict(summary)
                summary["version_bound"] = False
                summary["unverified_source"] = "file"
                summary.setdefault("gaps", []).append(
                    "选中版本没有对应它自身的验收：按未知处理（文件结论仅供参照）")
                return summary
            out = {
                "overall": str(acc.get("overall") or ""),
                "gaps": list(acc.get("gaps") or []),
                "rules_version": str(acc.get("rules_version") or ""),
                "rules_fingerprint": str(acc.get("rules_fingerprint") or ""),
                "report_sha256": str(acc.get("report_sha256") or ""),
                "version_bound": bool(bound),
            }
            if not bound:
                out["needs_reverify"] = adopted.acceptance_needs_reverify()
                out.setdefault("gaps", []).append(
                    "该版验收无法证明属于本正文（短 hash 或身份不符）：按未知处理")
            return out
        summary = self._read_acceptance_summary(task_id)
        if summary is not None:
            summary = dict(summary)
            summary["unverified_source"] = "file"
            summary["version_bound"] = False
        return summary

    def _admission_decision(self, task_id: str, status: str,
                            acceptance: dict | None) -> "AdmissionDecision":
        """本次运行的准入结论（M0-d）：经验池与模板固化都以它为准。"""
        from admission import admit_success
        from report_version import VersionStore, body_hash
        from workspace import task_workspace
        hard_ok, hard_reasons = True, []
        version_bound = False
        try:
            ws = task_workspace(task_id)
            store = VersionStore(ws, task_id)
            adopted = store.adopted()
            if adopted is not None:
                # 验收绑在选中版本；交付守卫若已判为草稿，硬约束不成立
                version_bound = adopted.acceptance_for_this_body()
                if not version_bound:
                    hard_reasons.append("验收未绑定选中版本正文")
            deliveries = store.deliveries()
            if deliveries and not any(d.get("ok") for d in deliveries):
                hard_ok = False
                hard_reasons.append("交付一致性守卫未通过（按未验收草稿交付）")
            _draft_why = str(self._delivery(task_id).get("reason") or "")
            if _draft_why:
                hard_ok = False
                hard_reasons.append(f"交付按草稿处理：{_draft_why}")
        except Exception as exc:
            hard_ok = False
            hard_reasons.append(f"版本/交付证据不可读：{str(exc)[:80]}")
        # R1：准入看的不是裸 `verdict == "PASS"`，而是"该 PASS 是否覆盖交付所依据的
        # 计划版本"。异版 PASS 一律按"未完成评审"处理（银行不入池 / 个人只算 admitted），
        # 不把"另一个计划版本被评审过"当成"这一版已验证成功"。
        review = dict(self._review_state(task_id))
        try:
            review_bound = self.review_scope_ok(task_id)
        except Exception as exc:
            review_bound = False
            logger.error("评审绑定不可判定，按未绑定处理：%s", str(exc)[:120])
        if str(review.get("verdict") or "") == "PASS" and not review_bound:
            review["verdict"] = "UNBOUND"
            review["degraded_reason"] = (
                f"持有的 PASS 绑定计划版本 v{int(review.get('plan_version') or 0)}，"
                f"当前为 v{self._plan_version(task_id)}")
        decision = admit_success(
            status=status, acceptance=acceptance, hard_ok=hard_ok,
            hard_reasons=hard_reasons, review=review,
            review_bound=review_bound,
            mode=self._identity_mode(), version_bound=version_bound,
        )
        self._task_admission[task_id] = decision
        if not decision.admitted or not decision.verified:
            logger.info("准入结论（task=%s）：admitted=%s verified=%s 原因=%s",
                        task_id, decision.admitted, decision.verified, decision.reasons)
        return decision

    def _notify_done_async(
        self, task_id: str, goal: str, status: str, report: str = "",
    ) -> None:
        """F5：任务终态外部通知（后台线程，不阻塞完成流程）。

        分工与去重（跨进程台账 `notify_claim:{kind}:{tid}`，见 notifications.claim_notify）：
        - daily-report 任务交给 webui——只有它会在终态生成分享链接并补发带链接的通知，
          编排器先发会让用户收到一条没有链接的通知；
        - 其它任务由编排器发（webui 不为普通任务发通知），按状态抢占 done/failed；
        - 抢占失败说明另一个进程/另一条路径已经发过，直接跳过，避免重复通知。
        """
        do_notify = {"ok": False}

        def _claim_and_mark() -> None:
            try:
                project = str((getattr(self, "_task_projects", {}) or {}).get(task_id) or "")
                if project == "daily-report":
                    return                      # webui 负责（带分享链接）
                from notifications import claim_notify
                kind = "failed" if str(status).upper() == "FAILED" else "done"
                do_notify["ok"] = claim_notify(kind, task_id)
            except Exception:
                do_notify["ok"] = True          # 抢占机制不可用时保持既有行为

        _claim_and_mark()
        if not do_notify["ok"]:
            logger.info("Skip duplicate notify for %s (%s)", task_id, status)
            return

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

    def _version_store(self, task_id: str):
        """每任务一份权威版本记录（M0-a），落在任务工作区的 `report_versions.json`。"""
        from report_version import VersionStore
        stores = getattr(self, "_version_stores", None)
        if stores is None:
            stores = {}
            self._version_stores = stores
        st = stores.get(task_id)
        if st is None:
            st = VersionStore(task_workspace(task_id), task_id)
            stores[task_id] = st
        return st

    def _restore_version_state(self, task_id: str, best_report: str) -> str:
        """恢复时校验版本归属与 hash（M0-a），返回应用作 `best_report` 的正文。

        - 版本库里已有**选中**版本时以它为准：检查点里的正文可能是旧稿或另一版，
          不能只凭检查点就把未验收正文当成当前版本；
        - 检查点正文在版本库里查不到（旧格式检查点 / 工作区被清）→ 重新登记，
          但**不补**验收：证据未知，交付守卫会据此判为未验收草稿；
        - 查得到但该版自身没有验收 → 同样保持未知。
        """
        body = str(best_report or "")
        if not body.strip():
            return body
        from report_version import body_hash
        store = self._version_store(task_id)
        adopted = store.adopted()
        if adopted is not None and adopted.body:
            if body_hash(adopted.body) != body_hash(body):
                logger.warning(
                    "恢复：检查点正文与已选中版本不一致（task=%s），以已选中版本为准",
                    task_id,
                )
            return adopted.body
        v = store.find_by_body(body)
        if v is None:
            # R0.2：恢复登记同样带完整身份（来源快照 + 规则 + 策略版本）
            _rules_v, _rules_fp = self._rules_identity(task_id)
            v = store.record(
                body, sources_fingerprint=self._sources_fingerprint(task_id, body),
                rules_version=_rules_v, rules_fingerprint=_rules_fp,
                policy_version=REVIEW_POLICY_VERSION)
            logger.warning(
                "恢复：检查点正文在版本库无记录（task=%s），按证据未知登记", task_id,
            )
        if v.acceptance_needs_reverify():
            logger.warning(
                "恢复：选中版本只有旧的短 hash 验收（task=%s），按待重验处理", task_id)
        store.adopt(v, reason="恢复自检查点")
        return body

    def _adopt_candidate(self, task_id: str, cur_text: str, cand: str, *,
                         iteration: int = 0, cancelled: bool = False) -> str:
        """**唯一采纳点**：两个反思/重做分支都走这里（M0-a）。

        候选稿先登记成版本；比较时取**两版各自**的验收（缺失即未知，不借用最新那份）；
        采纳用 `adopt()` 原子切换；任务已取消/终态时只记迟到拒绝，不覆盖已选中版本。
        返回采纳后应作为 `best_report` 的正文。
        """
        store = self._version_store(task_id)
        if cancelled:
            store.reject_late("任务已取消/终态：迟到候选稿不采用")
            return cur_text
        # R0.2：登记一律带完整身份（来源/事实快照指纹 + 规则版本与指纹 + 策略版本）
        _src_fp = self._sources_fingerprint(task_id, cand)
        _rules_v, _rules_fp = self._rules_identity(task_id)
        cur_v = store.find_by_body(cur_text) if str(cur_text or "").strip() else None
        if cur_v is None and str(cur_text or "").strip():
            cur_v = store.record(
                cur_text, sources_fingerprint=self._sources_fingerprint(task_id, cur_text),
                rules_version=_rules_v, rules_fingerprint=_rules_fp,
                policy_version=REVIEW_POLICY_VERSION)
        cand_v = store.record(cand, parent_id=(cur_v.version_id if cur_v else ""),
                              iteration=iteration, sources_fingerprint=_src_fp,
                              rules_version=_rules_v, rules_fingerprint=_rules_fp,
                              policy_version=REVIEW_POLICY_VERSION)
        improved, why = compare_versions(
            cur_text, cand,
            cur_acceptance=(cur_v.acceptance if cur_v else None),
            cand_acceptance=cand_v.acceptance)
        if improved:
            store.adopt(cand_v, reason=why)
            logger.info("版本比较：采用候选稿（%s）", why)
            return cand
        logger.info("版本比较：保留当前稿（%s）", why)
        return cur_text

    @staticmethod
    def _read_acceptance_summary(task_id: str) -> dict | None:
        """读工作区验收快照（B 批起唯一实现在 `delivery_pipeline`）。"""
        from delivery_pipeline import read_acceptance_summary
        return read_acceptance_summary(task_id)

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
        draft_reason: str = "",
    ) -> str:
        """任务最终状态（A1：以最终验收报告判定，而非反思是否执行）：
        - 有步骤失败 → FAILED；
        - 最终验收 fail（无论反思是否执行/重做）→ SUCCESS_WITH_ISSUES；
        - LLM 双端点均失败 → SUCCESS_WITH_ISSUES；
        - 其余成功 → SUCCESS。
        reflection_unavailable 保留入参仅用于调用方日志追溯，不再参与判定：
        只要最终 acceptance_report.json 仍为 fail，就如实降级，避免
        "反思执行过但重做后仍失败"被误报为 SUCCESS。

        派生规则**唯一实现**已收归 task_state.derive_status（状态真源收口），
        这里只做入参适配，避免两处规则分叉。"""
        try:
            import task_state
            return task_state.derive_status(
                step_statuses=["FAILED"] if has_failure else ["SUCCESS"],
                acceptance=acceptance_summary or {},
                llm_degraded=llm_degraded or {},
                # M0-a：交付按未验收草稿处理时不得显示"通过"
                draft_delivery=draft_reason,
            )
        except Exception:
            # 投影器不可用时的兜底：保持与收口前一致的判定
            if has_failure:
                return "FAILED"
            accept_fail = bool(
                acceptance_summary and acceptance_summary.get("overall") != "pass"
            )
            both_failed = bool(llm_degraded and llm_degraded.get("both_failed"))
            if accept_fail or both_failed or str(draft_reason or "").strip():
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

    # 探索-固化统计文件：项目目录下的固定文件名（跨任务共享的计数依据）。
    # 不做环境变量覆盖——计数依据的载体若能由环境指向任意位置，"已验证成功次数"
    # 就能被外部改写；测试隔离改这个类属性（或打桩 _consolidation_stats_path）。
    CONSOLIDATION_STATS_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "consolidation_stats.json")

    @classmethod
    def _consolidation_stats_path(cls) -> str:
        """统计文件位置（唯一解析点）。"""
        return cls.CONSOLIDATION_STATS_FILE

    def _record_consolidation_stat(self, task_id: str, goal: str, all_steps: list,
                                   admission=None) -> None:
        """任务完成时记录 domain×能力链 与准入结论，供探索-固化阈值判定。

        M0-d：①只记准入谓词的 `verified`，不再用"最新验收"；②同任务同版本只计一次
        （按 task_id + 正文版本去重），重复收尾不会把已验证计数刷上去。
        """
        try:
            path = self._consolidation_stats_path()
            stats = []
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        stats = json.loads(f.read())
                except Exception:
                    stats = []
            domain, chain = self._consolidation_key(goal, all_steps)
            version_id = ""
            try:
                from report_version import VersionStore
                from workspace import task_workspace
                adopted = VersionStore(task_workspace(task_id), task_id).adopted()
                version_id = adopted.version_id if adopted else ""
            except Exception:
                version_id = ""
            key = (str(task_id), version_id)
            stats = [
                s for s in stats
                if (str(s.get("task_id")), str(s.get("report_version_id") or "")) != key
            ]
            stats.append({
                "task_id": str(task_id),
                "report_version_id": version_id,
                "domain": domain,
                "chain": list(chain),
                "acceptance": self._acceptance_passed(task_id),
                # verified 才是"计入阈值"的字段；旧条目没有该键 → 不计（历史样例隔离）
                "verified": bool(getattr(admission, "verified", False)),
                "admitted": bool(getattr(admission, "admitted", False)),
                "reasons": list(getattr(admission, "reasons", []) or []),
                "ts": self._now_iso(),
            })
            with open(path, "w", encoding="utf-8") as f:
                json.dump(stats[-300:], f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _count_verified_chain(self, domain: str, chain: tuple, current_task: str = "") -> int:
        """统计历史上同 domain×能力链 且**已准入为已验证成功**的任务数（不含当前任务）。

        M0-d：只认 `verified is True`。旧条目（只有 acceptance、没有 verified）在重建
        证据前不再计入——历史样例原样留档，既不批量删也不继续抬高固化计数。
        """
        try:
            path = self._consolidation_stats_path()
            if not os.path.exists(path):
                return 0
            with open(path, encoding="utf-8") as f:
                stats = json.loads(f.read())
            count = 0
            seen: set[str] = set()
            # 同一任务只算一次：倒序扫描（文件按写入顺序追加），每个任务取**最新**那条，
            # 否则同一任务的两个版本会把这个任务的成功算成两次
            for s in reversed(stats):
                tid = str(s.get("task_id") or "")
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                if tid == str(current_task):
                    continue
                if s.get("domain") != domain:
                    continue
                if tuple(s.get("chain") or []) != chain:
                    continue
                if s.get("verified") is True:
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
    def _pick_fetch_url(items: list, goal: str = "", *, role: str = "",
                        exclude: tuple = ()) -> str | None:
        """从搜索结果中挑选财务相关度最高的 URL（搜索根因缩小版）。

        评分维度：
        1) 标题-目标相关性：标题真正关于目标公司财报/研报/IR 才高分；
           仅"提到"目标（合作新闻）或标题是其他公司（Line/Lululemon）→ 强降权；
        2) 来源分级：官方 IR/交易所披露 > 权威财经 > 内容社区/杂页；
        3) 财务关键词：标题命中 > URL 命中；
        4) **抓取角色**（F2 研究路径）：`annual_report` 优先年报/公告正文页，
           `notes` 优先附注/现金流/风险页——两个抓取步骤各取所需，且排除已抓过的
           URL（`exclude`），不把同一页抓两遍。"""
        finance_kw = (
            "财报", "年报", "季报", "营收", "净利润", "业绩", "财务", "公告",
            "研报", "复盘", "深度", "投资者关系",
            "financial", "earnings", "annual", "revenue", "results",
            "quarterly", "investor",
        )
        # 来源分级常量收敛到 adapters.search_quality（text_search 结果
        # 计分复用同一份权威域分级，保证快答/行业预载与财务抓取同口径）
        from adapters.search_quality import (
            AUTHORITY_GOOD as good_domains,
            AUTHORITY_JUNK as junk_domains,
            AUTHORITY_OFFICIAL as official_domains,
        )
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
            if role and url in tuple(exclude or ()):
                continue          # 已抓过的页面不再选（研究路径的第二个抓取步骤）
            low_t = title.lower()
            low_u = url.lower()
            score = 0
            is_official = any(o in low_u for o in official_domains)
            # 抓取角色：年报正文页 / 附注风险页各取所需
            for k in _FETCH_ROLE_KW.get(role, ()):
                if k in low_t:
                    score += 5
                elif k in low_u:
                    score += 3
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

    @staticmethod
    def _fetched_urls(task_id: str, project=None) -> tuple:
        """本任务已抓取过的 URL（`fetch_snapshot.json`）：第二个抓取步骤据此避重。"""
        try:
            proj = task_project_dir(task_id, project) if project \
                else task_project_dir(task_id)
            p = Path(proj) / "fetch_snapshot.json"
            if not p.exists():
                return ()
            items = json.loads(p.read_text(encoding="utf-8")) or []
        except Exception:
            return ()
        out: list[str] = []
        for it in (items if isinstance(items, list) else []):
            if isinstance(it, dict):
                u = str(it.get("url") or "").strip()
                if u.startswith("http") and u not in out:
                    out.append(u)
        return tuple(out)



    def _structured_financial_preload(
        self, task_id: str, goal: str,
    ) -> dict | None:
        """兼容旧名：F4 后统一走 _structured_data_preload（financial 走原通道）。"""
        return self._structured_data_preload(task_id, goal)


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
        """B 方案：已预载结构化数据（排行/crypto/macro/news/财务）的任务，
        把 data_analyzer 步骤替换为 content_summary 直接消费预载文件
        （排行类 structured_data.json / 财务类 financials.json），
        避免 EDA 步骤只认 CSV 造成断链；图表仍由数据驱动兜底渲染提供。
        保持 step_id 与依赖不变，报告/打包步骤无需重连。"""
        if not steps or not data:
            return steps
        source = str(data.get("source") or "")
        financial_sources = (
            "eastmoney_datacenter", "eastmoney_ashare", "sec_edgar",
            "cninfo_annual", "multi_entity",
        )
        other_sources = (
            "eastmoney_ranking", "tencent_ranking", "tencent_us_ranking",
            "coingecko", "macro", "news",
        )
        is_financial = source in financial_sources or (
            not source and isinstance(data.get("financials"), list)
        )
        if source not in other_sources and not is_financial:
            return steps
        label = {
            "eastmoney_ranking": "A股行情排行",
            "tencent_ranking": "A股行情排行（腾讯）",
            "tencent_us_ranking": "美股行情排行（腾讯）",
            "sina_ranking": "行情排行（新浪全市场）",
            "coingecko": "加密货币行情",
            "macro": "宏观指标",
            "news": "新闻列表",
            "eastmoney_datacenter": "港交所财报",
            "eastmoney_ashare": "A股财报",
            "sec_edgar": "美股 10-K 年报",
            "cninfo_annual": "巨潮 A 股年报",
            "multi_entity": "多公司财报对比",
        }.get(source, source)
        out: list[dict] = []
        changed = False
        for s in steps:
            if str(s.get("capability")) == "data_analyzer":
                changed = True
                if is_financial:
                    instruction = (
                        f"基于已预载的 financials.json（{label}，公司年报结构化"
                        "财务数据，含营收/净利润/研发投入等科目）直接输出财务"
                        "指标要点与趋势结论；无需寻找 CSV，无需重新抓取数据；"
                        "财务数字必须与 financials.json 一致并标注数据来源与"
                        "数据获取时间。"
                        f"原始指令：{s.get('instruction', '')}"
                    )
                else:
                    instruction = (
                        f"基于已预载的 structured_data.json（{label}）"
                        "直接输出结构化要点与结论；无需寻找 CSV，无需重新抓取数据；"
                        "如目标需要图表，请按 [CHART_DATA] 规格输出图表数据"
                        "或引用工作区已生成的图表。"
                        f"原始指令：{s.get('instruction', '')}"
                    )
                out.append({
                    "step_id": s.get("step_id"),
                    "capability": "content_summary",
                    "instruction": instruction,
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
            # 打包必须等所有步骤（含摘要/报告）完成，否则工作区还没有新文件可打包；
            # 可选步骤（如定向取证的第二个来源）不进依赖：取不到不该拖垮交付包
            "depends_on": [s.get("step_id") for s in steps if not s.get("optional")],
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
        """T10：委托 templates_pipeline 模块实现（原主体已搬迁）。"""
        from templates_pipeline import normalize_steps
        return normalize_steps(steps, self._max_steps)

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
        """T10：委托 templates_pipeline 模块实现（原主体已搬迁）。"""
        from templates_pipeline import topic_tokens
        return topic_tokens(text)

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
                    _acc_gap_text = "\n".join(f"- {g}" for g in acc["gaps"])
                    # 防幻觉来源指引（#6）：实测重做后仍复现"编造 Gartner/
                    # TrendForce""引用标记被当来源名"——追加确定性红线约束
                    _acc_gap_text += _SOURCE_DISCIPLINE_REDLINES
                    ctx_parts.append(
                        "验收器缺口报告（确定性检查结果；请优先修复以下缺口，"
                        "只重做能补齐缺口的步骤，不要整轮重来）：\n"
                        + _acc_gap_text
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
            from ws_helpers import call_with_heartbeat, phase_begin, phase_end
            phase_begin(self._messaging, task_id, "反思")
            try:
                # 反思同样受调用级预算约束：耗尽即跳过本轮（宁可少一轮反思，
                # 也不要拖到任务级超时把整个交付拖垮）
                _reflect_budget = float(
                    os.environ.get("WM_REFLECT_BUDGET_SECONDS", "120") or 120)
                raw = call_with_heartbeat(
                    self._messaging, task_id, "反思",
                    self._planner_llm.call,
                    get_prompt("reflect", ITERATOR_SYSTEM, goal=goal),
                    prompt, expect_json=True, max_tokens=8192,
                    usage="plan",
                    deadline=time.time() + _reflect_budget,
                )
                phase_end(self._messaging, task_id, "反思", ok=True)
            except Exception:
                phase_end(self._messaging, task_id, "反思", ok=False)
                raise
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
                # M0-a：回退只恢复**内存结果**；磁盘 report.md 可能仍是重做版 →
                # 明确标为"需重验的草稿"，不让交付路径把它当已验收版本。
                self._delivery(task_id)["reason"] = (
                    f"步骤 {step_id} 重做劣化已回退：磁盘报告可能仍为重做版本，交付需重验")
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
                # 闭环修复：报告步骤重做成功后必须复跑确定性验收——
                # 此前重做路径从不重跑验收，_read_acceptance_summary 永远
                # 引用过期快照（"重做后仍失败"实为旧文件未被复检）
                if target.get("capability") in ("report_generator", "content_summary"):
                    try:
                        self._run_acceptance_check(task_id, goal, trigger="反思重做")
                    except Exception as exc:
                        logger.warning(
                            "Redo acceptance recheck failed for %s: %s",
                            task_id, str(exc)[:120],
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

    def _ensure_final_body_accepted(self, task_id: str, goal: str,
                                    detail: str) -> tuple[str, str]:
        """被采纳的交付正文要拿到**它自己**的验收（B 批起唯一实现在 `delivery_pipeline`）。

        返回 `(状态, 交付正文)`；验收器修复过正文时，修复版就是交付正文。
        """
        from delivery_pipeline import ensure_body_accepted
        return ensure_body_accepted(task_id, goal, detail,
                                    accept_fn=self._accept_fn_for(task_id, goal))

    def _apply_research_hard_gate(self, task_id: str, goal: str, wp: dict | None,
                                  report_body: str = "") -> str:
        """研究任务交付硬门槛（B 批起唯一实现在 `delivery_pipeline`）。

        这里保留实例语义：把门槛原因写进 `_delivery(task_id)["hard_fail"]`，
        供准入判定与终态派生使用；返回要附加到交付物的一段说明。
        """
        from delivery_pipeline import apply_research_hard_gate
        note, hard_fail = apply_research_hard_gate(task_id, goal, wp, report_body)
        if hard_fail:
            self._delivery(task_id)["hard_fail"] = hard_fail
        return note

    def _accept_fn_for(self, task_id: str, goal: str):
        """给共享装配器用的验收回调：仍走本实例的验收（取消检查、前端推送、评测集沉淀）。

        共享模块只负责"对哪份正文验收、产物落盘、按身份绑定"，实例相关的副作用留在
        编排器这一侧——两条入口（收尾/人工修订）因此共用同一套验收语义。
        """
        def _accept(tid: str, goal_text: str, body: str):
            return self._run_acceptance_check(
                tid, goal_text or goal, trigger="最终装配",
                report_body=body, prefer_body=True)
        return _accept

    def _run_acceptance_check(self, task_id: str, goal: str,
                              trigger: str = "报告步骤",
                              report_body: str = "",
                              prefer_body: bool = False) -> dict | None:
        """报告生成后跑确定性验收器：数字溯源等 checklist → 缺口报告。

        B 批起核心（修复 → 产物落盘 → 审计事件 → 按身份绑定）搬进
        `delivery_pipeline.accept_for_body`，与 web_ui 的人工修订共用同一实现；
        这里只挂本实例的副作用：取消检查、前端推送、评测集沉淀。
        """
        from delivery_pipeline import accept_for_body
        return accept_for_body(
            task_id, goal, report_body, trigger=trigger, prefer_body=prefer_body,
            hooks={
                "cancelled": lambda: self._cancel_requested(task_id),
                "iteration": int(getattr(self, "_accept_iteration", 0) or 0),
                "on_summary": lambda summary: push_progress(
                    self._messaging, task_id, "acceptance",
                    {"message": summary, "timestamp": self._now_iso()}),
                "on_failure": self._harvest_acceptance_failure(task_id, goal),
            })

    def _harvest_acceptance_failure(self, task_id: str, goal: str):
        """验收 fail 的真实任务沉淀为评测案例（静默，不干扰主线）。"""
        def _hook(result: dict, report: str) -> None:
            try:
                from evals.auto_grow import harvest_failure
                harvest_failure(task_id, goal, result, report)
            except Exception as exc:
                logger.warning("评测集自动沉淀异常（已忽略）: %s", str(exc)[:100])
        return _hook

    def _record_reflection_refinement(
        self, goal: str, task_id: str, key: str, issue: str, fix_prompt: str,
    ) -> None:
        """把反思对提示词的改动沉淀进进化系统 RAG。失败不影响任务主线。

        R1：只有**本次运行已取得已验证成功**时才写 `active`（可被后续任务注入）；
        否则写 `pending_review` 只留档——反思发生在任务尚在迭代的中途，
        那时它自己的结论还没被任何验收/评审确认过。
        """
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
            from memory_manager import REFINEMENT_ACTIVE, REFINEMENT_PENDING
        except Exception:
            REFINEMENT_ACTIVE, REFINEMENT_PENDING = "active", "pending_review"
        _verified = False
        try:
            _adm = (getattr(self, "_task_admission", {}) or {}).get(task_id)
            _verified = bool(getattr(_adm, "verified", False))
        except Exception:
            _verified = False
        try:
            rec(
                goal=goal, key=key,
                issue=str(issue)[:300],
                fix_prompt=str(fix_prompt)[:800],
                rationale="反思轮发现缺陷后对步骤提示词的修改",
                task_id=task_id, version=1, outcome="reflection",
                status=(REFINEMENT_ACTIVE if _verified else REFINEMENT_PENDING),
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
        timeout = min(self._plan_confirm_timeout, 600)
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
                # M0-e：全量状态里带阶段观测（剩余预算 + 各阶段等待对象/进展）
                "budget": self.budget_snapshot(task_id),
            })
        except Exception as exc:
            logger.warning("Full state push failed: %s", str(exc)[:120])

    # ------------------------------------------------------------------
    # V1.2 checkpointer：断点续跑（对标 LangGraph checkpointer）
    # ------------------------------------------------------------------
    def _mark_task_running(self, task_id: str) -> None:
        """Redis 进行中标记（含 pid，供崩溃后判断旧标记是否存活）。"""
        try:
            self._redis.set(
                f"task_running:{task_id}",
                json.dumps(
                    {"pid": os.getpid(), "started": self._now_iso()},
                    ensure_ascii=False,
                ),
                ex=86400,
            )
        except Exception:
            pass

    def _clear_task_running(self, task_id: str) -> None:
        try:
            self._redis.delete(f"task_running:{task_id}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 用户取消（协作式）：webui 写 task_cancel:{tid}，编排器在派发边界检查
    # ------------------------------------------------------------------

    def _cancel_key(self, task_id: str) -> str:
        return f"task_cancel:{task_id}"

    def _cancel_token_key(self, task_id: str) -> str:
        """不可复位的取消令牌（R2）。

        Redis 里的 `task_cancel:{tid}` 是**可被删除/过期**的提示（webui 写、编排器
        收尾时清），单靠它判断"用户是否请求过停止"会漏：键被清掉、过期、或 Redis
        重启之后，同一个任务再被恢复/重跑就"看不到取消"，于是一个用户明确停掉的
        任务可能继续派发新请求。因此取消信号落成两个不可复位的东西：
        令牌（set 后不再清除，值里带时间与来源）与任务终态 CANCELLED。
        """
        return f"task_cancel_token:{task_id}"

    def request_cancel(self, task_id: str, reason: str = "用户请求停止") -> None:
        """记录一次取消请求（webui 入口）：同时写提示键与**不可复位令牌**。"""
        payload = json.dumps({"at": self._now_iso(), "at_ts": time.time(),
                              "reason": str(reason or "")[:120]},
                             ensure_ascii=False)
        try:
            self._redis.set(self._cancel_key(task_id), payload)
        except Exception as exc:
            logger.warning("取消提示键写入失败（task=%s）：%s", task_id, str(exc)[:100])
        try:
            # 只在第一次写入（NX）：令牌不可复位，重复取消不改写既有时间与来源
            self._redis.set(self._cancel_token_key(task_id), payload, nx=True)
        except TypeError:
            try:
                self._redis.set(self._cancel_token_key(task_id), payload)
            except Exception as exc:
                logger.warning("取消令牌写入失败（task=%s）：%s", task_id, str(exc)[:100])
        except Exception as exc:
            logger.warning("取消令牌写入失败（task=%s）：%s", task_id, str(exc)[:100])

    def _cancel_requested(self, task_id: str) -> bool:
        """用户是否请求停止该任务。

        用"标志位 + 派发边界检查"而不是抛异常：step worker 的 `except Exception`
        会把深层异常转成"步骤失败"继续跑，而 run() 又没有兜底 except——抛异常
        既停不下来也收尾不干净。

        R2：三个来源取**或**——提示键（快）、不可复位令牌（慢但不会丢）、
        已落库的 CANCELLED 终态（最慢但持久）。任何一个说"停过"，就不再发起新请求。
        """
        try:
            if self._redis.get(self._cancel_key(task_id)):
                return True
            if self._redis.get(self._cancel_token_key(task_id)):
                return True
        except Exception:
            pass
        # 持久终态：CANCELLED 一旦落库就不可复位（Redis 被清也不影响）
        try:
            import task_state as _ts
            st = str((_ts.read_task(task_id) or {}).get("status") or "").upper()
            if st == _ts.CANCELLED:
                return True
        except Exception:
            pass
        return False

    def _clear_cancel(self, task_id: str) -> None:
        """清掉**提示键**；不可复位令牌与 CANCELLED 终态都保留。

        保留令牌是刻意的：这个任务已经被用户停过一次，恢复/重跑时不得因为
        "提示键被清了"就当没发生过。
        """
        try:
            self._redis.delete(self._cancel_key(task_id))
        except Exception:
            pass

    def _cancel_latency(self, task_id: str) -> dict:
        """取消的两个时限（R2）：UI 响应时延与供应商在飞结算窗口。

        - UI 响应：从取消请求落盘的时间戳到**现在**（收尾时刻）。用户在意的就是这个；
          预期上限 `WM_CANCEL_UI_DEADLINE_SECONDS`（默认 30s），超出如实标记。
        - 供应商结算：被放弃等待的调用在供应商侧多久内仍可能计费
          （`root_budget.INFLIGHT_SETTLE_SECONDS`）。它是账目窗口，不是用户等待时间，
          两者混成"取消花了 N 秒"会把账目不确定性写成性能问题。
        """
        requested_at = 0.0
        for key in (self._cancel_token_key(task_id), self._cancel_key(task_id)):
            try:
                raw = self._redis.get(key)
            except Exception:
                raw = None
            if not raw:
                continue
            try:
                requested_at = float((json.loads(raw) or {}).get("at_ts") or 0.0) \
                    if str(raw).startswith("{") else 0.0
            except Exception:
                requested_at = 0.0
            if requested_at:
                break
        if not requested_at:
            return {}
        sec = max(0.0, time.time() - requested_at)
        try:
            deadline = float(os.environ.get("WM_CANCEL_UI_DEADLINE_SECONDS", "30") or 30)
        except (TypeError, ValueError):
            deadline = 30.0
        try:
            from root_budget import INFLIGHT_SETTLE_SECONDS as _win
        except Exception:
            _win = 0.0
        return {"sec": sec, "deadline": deadline, "within": sec <= deadline,
                "settle_window": float(_win or 0.0),
                "requested_at": requested_at}

    def _finish_cancelled(self, task_id: str, goal: str,
                          steps: list | None = None) -> dict:
        """取消收尾：通告终态 → 清运行标记 → 落库/通知，返回 CANCELLED 结果。

        V2-1：终态是 **CANCELLED**（不再写成 FAILED）。此前用户主动停止与真实失败
        在历史、SSE、指标里无法区分——前端 statusMeta 早就映射了"已取消"，但后端
        从不产生该状态，属于死代码。
        """
        note = "任务被用户取消（运行中被停止）"
        import task_state as _ts
        status = _ts.CANCELLED
        # R2：两个时限**分别**记录，不混成一个数：
        # ① 取消 UI 响应时限——从用户点"停止"到编排器真正收尾（用户在意的时延）；
        # ② 供应商在飞结算时限——被放弃等待的调用多久后才可能真正结清（账目问题）。
        _lat = self._cancel_latency(task_id)
        if _lat:
            note += (f"（自请求停止起 {_lat['sec']:.1f}s 生效"
                     f"{'，' if _lat['within'] else '，超出 UI 预期，'}"
                     f"{'在时限内' if _lat['within'] else '需关注'}）")
        push_progress(self._messaging, task_id, "log",
                      {"type": "log", "agent": "orchestrator", "message": note,
                       "timestamp": self._now_iso()})
        if _lat:
            push_progress(self._messaging, task_id, "log",
                          {"type": "review", "agent": "orchestrator",
                           "message": (f"取消时限：UI 响应 {_lat['sec']:.1f}s"
                                       f"（预期 ≤{_lat['deadline']:.0f}s，"
                                       f"{'达标' if _lat['within'] else '超时'}）；"
                                       f"在飞调用的供应商结算窗口 "
                                       f"{_lat['settle_window']:.0f}s（可能仍在计费，"
                                       f"按待对账记账）"),
                           "timestamp": self._now_iso()})
        push_progress(self._messaging, task_id, "task_complete",
                      {"status": status, "summary": note})
        self._clear_task_running(task_id)
        self._finalize_task(task_id, goal, status, report=note,
                            steps=steps or [])
        self._notify_done_async(task_id, goal, status, note)
        self._clear_cancel(task_id)
        return {"task_id": task_id, "status": status, "steps": steps or [],
                "report": note}

    def _task_is_running(self, task_id: str) -> bool:
        """进行中标记存在且其 pid 仍存活 → True；否则 False（允许恢复）。

        探活判据与 webui 看护线程共用同一条实现（`task_state.pid_same_process`）：
        此处此前把 os.kill 的 WinError 87 当"进程已不存在"，而实测该错误对**存活
        进程**也会出现——会把检查点恢复到正在跑的任务上。判据不足（None）一律视为
        运行中，宁可少恢复一次。
        """
        try:
            raw = self._redis.get(f"task_running:{task_id}")
        except Exception:
            return False
        if not raw:
            return False
        try:
            payload = json.loads(raw) or {}
        except Exception:
            return True  # 标记在但解析不了：不敢当作已死
        try:
            import task_state as _ts
            holder = _ts.pid_same_process(payload.get("pid"), payload.get("started"))
        except Exception:
            return True
        return holder is not False

    def _checkpoint_payload(
        self, task_id: str, goal: str, project: str,
        all_steps: list[dict], completed_all: dict,
        current_steps: list[dict] | None = None,
        pending_steps: list[dict] | None = None,
        phase: str = "executing", iteration: int = 0,
        has_failure: bool = False, redo_rounds: int = 0,
        best_report: str = "", gate_checked: bool = False,
        simple: bool = False, used_template: bool = False,
    ) -> dict:
        """组装 checkpoint 内容（steps 为全部步骤，含 result/status）。"""
        completed_all = completed_all or {}

        def _with_result(step: dict) -> dict:
            s = dict(step)
            r = dict(completed_all.get(s.get("step_id"), {}) or {})
            s["result"] = r
            s["status"] = r.get("status", "")
            return s

        return {
            "task_id": task_id,
            "goal": goal,
            "goal_hash": _checkpoint_goal_hash(goal),
            "project": project,
            "steps": [_with_result(s) for s in (all_steps or [])],
            "completed_all": {
                str(k): dict(v) for k, v in completed_all.items()
            },
            "current_steps": [_with_result(s) for s in (current_steps or [])],
            "pending_steps": [_with_result(s) for s in (pending_steps or [])],
            "phase": phase,
            "iteration": int(iteration or 0),
            "has_failure": bool(has_failure),
            "redo_rounds": int(redo_rounds or 0),
            "best_report": str(best_report or ""),
            "gate_checked": bool(gate_checked),
            "simple": bool(simple),
            "used_template": bool(used_template),
            # M0-b：评审状态随检查点走，恢复时据此判断能否复用 PASS
            "review": dict(self._review_state(task_id)),
            # R1：当前**计划版本号**。与 review.plan_version 分开记：两者不同即表示
            # 落盘时计划已被改写而 PASS 还是旧版本——恢复时按"异版 PASS 不复用"处理。
            "plan_version": self._plan_version(task_id),
            "status": "RUNNING",
            "saved_at": self._now_iso(),
        }

    def _save_checkpoint(self, task_id: str, payload: dict) -> None:
        """保存 checkpoint（内部已静默降级；此处再兜一层）。"""
        try:
            save_checkpoint(task_id, payload)
        except Exception as exc:
            logger.debug("checkpoint save failed for %s: %s", task_id, str(exc)[:100])

    def _resume_from_checkpoint(
        self, task_id: str, goal: str, project: str | None = None,
    ) -> dict | None:
        """从 checkpoint 恢复初始状态；不适用（无 checkpoint/已完成/目标不一致）返回 None。

        返回状态：steps=待执行步骤（SUCCESS 直接复用，失败/未完成保留重试），
        all_steps=已累积步骤（待重试步骤先摘出，执行后以新结果追加，避免重复），
        completed_all/phase/iteration 等供 run() 直接初始化。
        """
        try:
            cp = load_checkpoint(task_id)
        except Exception as exc:
            logger.warning("checkpoint load failed for %s: %s", task_id, str(exc)[:100])
            return None
        if not isinstance(cp, dict):
            return None
        # 目标一致性：goal 哈希不同 → 忽略（防复用旧任务计划）
        cp_hash = cp.get("goal_hash")
        if cp_hash:
            if str(cp_hash) != _checkpoint_goal_hash(goal):
                logger.warning(
                    "Checkpoint goal mismatch for %s, ignoring", task_id,
                )
                return None
        elif str(cp.get("goal") or "") != str(goal or ""):
            logger.warning(
                "Checkpoint goal mismatch for %s, ignoring", task_id,
            )
            return None
        # 已完成任务不恢复
        if is_task_completed(task_id, cp):
            logger.info("Checkpoint for %s already completed, ignoring", task_id)
            return None

        phase = str(cp.get("phase") or "executing")
        if phase not in ("executing", "reflecting", "finalizing"):
            phase = "executing"

        def _pending(candidate) -> list[dict]:
            if not isinstance(candidate, list):
                return []
            return [
                s for s in candidate
                if (s.get("result") or {}).get("status") != "SUCCESS"
            ]

        raw_pending = cp.get("pending_steps")
        if isinstance(raw_pending, list):
            # 显式 pending_steps（可为空）以 checkpoint 为准
            pending = _pending(raw_pending)
        else:
            # 兜底：旧格式 checkpoint 无 pending_steps 时，过滤当前轮（或全部）步骤
            active = cp.get("current_steps")
            if not isinstance(active, list) or not active:
                active = cp.get("steps") or []
            pending = _pending(active)

        all_steps = cp.get("steps")
        if not isinstance(all_steps, list):
            all_steps = []
        completed_all = cp.get("completed_all")
        if not isinstance(completed_all, dict):
            completed_all = {
                str(s.get("step_id")): dict(s.get("result") or {})
                for s in all_steps if s.get("step_id")
            }
        pending_ids = {
            str(s.get("step_id")) for s in pending if s.get("step_id")
        }
        # 待重试步骤从累积列表摘出：执行后以新结果追加，避免 all_steps 重复
        prior = [
            s for s in all_steps
            if str(s.get("step_id")) not in pending_ids
        ]
        # M0-a：恢复的正文要过版本归属/hash 校验，缺证据保持未知
        restored_best = self._restore_version_state(
            task_id, str(cp.get("best_report") or ""))
        return {
            "steps": pending,
            "all_steps": prior,
            "completed_all": completed_all,
            "phase": phase,
            "iteration": int(cp.get("iteration") or 0),
            "has_failure": bool(cp.get("has_failure") or False),
            "redo_rounds": int(cp.get("redo_rounds") or 0),
            "best_report": restored_best,
            "review": dict(cp.get("review") or {}),
            # R1：计划版本号随 checkpoint 恢复（与 review.plan_version 相比即知
            # "落盘时计划是否已被改写、PASS 是否还是旧版本"）
            "plan_version": int(cp.get("plan_version") or 0),
            "gate_checked": bool(cp.get("gate_checked") or False),
            "simple": bool(cp.get("simple") or False),
            "used_template": bool(cp.get("used_template") or False),
            "skip_execute": not pending,
        }

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
        """把计划草案交给 Critic 评审；FAIL 修订一次，修订稿要**再评一次**。

        V2-2 / M0-b：超时、异常、ERROR 一律"评审未完成"：
        - 个人模式：按**根任务**记降级（`_review_state`），交付物需人工复核；
        - 银行模式：抛 `ReviewRequiredError` 拒绝继续（修订稿没有拿到绑定的 PASS 也一样）。
        裁决只认 PASS/FAIL：别的一律按"评审未完成"处置，既不通过也不进付费修订。
        """
        plan_id = f"plan-{task_id}-{int(time.time())}"
        st = self._review_state(task_id)
        st["degraded_reason"] = ""
        st["verdict"] = ""
        st["rounds"] = 0
        bank = self._review_is_required()
        max_rounds = 2                      # 初评 + 修订后复评，最多一次付费修订
        plan = list(steps or [])
        for round_no in range(1, max_rounds + 1):
            # 每轮用各自的 plan_id：回复列表键随之不同，上一轮的迟到回包不会
            # 被当成本轮裁决（同一 plan_id 复用会让复评读到初评的结果）
            round_plan_id = f"{plan_id}-r{round_no}-{os.urandom(4).hex()}"
            review = self._request_plan_review(
                goal, plan, task_id, round_plan_id, round_no, bank)
            if review is None:                       # 已被 _review_unavailable 处置
                return plan
            verdict = str(review.get("verdict", "")).upper()
            if verdict == "PASS":
                self._review_bind_pass(task_id, plan)
                push_progress(self._messaging, task_id, "log",
                              {"type": "review", "agent": "critic",
                               "message": f"Review PASSED {review.get('scores', {})}"
                                          f"（绑定第 {round_no} 轮计划）",
                               "timestamp": self._now_iso()})
                return plan
            # 只有 FAIL 才进修订分支；其余裁决在 _request_plan_review 里按协议错误处理
            suggestions = review.get("suggestions") or []
            push_progress(self._messaging, task_id, "log",
                          {"type": "review", "agent": "critic",
                           "message": f"Review FAILED, revising ({len(suggestions)} suggestions)",
                           "timestamp": self._now_iso()})
            if round_no == max_rounds:
                return self._review_unavailable(
                    task_id, f"修订后仍未通过评审（{max_rounds} 轮）", plan, bank)
            revised = self._revise_plan(goal, plan, suggestions, task_id)
            if not revised:
                return self._review_unavailable(task_id, "评审 FAIL 且修订未产出计划", plan, bank)
            plan = list(revised)
            st["rounds"] = round_no
        return plan

    def _request_plan_review(self, goal: str, steps: list[dict], task_id: str,
                             plan_id: str, round_no: int, bank: bool) -> dict | None:
        """发起一轮评审；返回评审结果，或已按"评审未完成"处置时返回 None。

        每次评审带**步骤指纹**：PASS 只对这份计划有效，换计划要重新拿 PASS。
        """
        try:
            r = self._new_redis_sync()
            self._messaging.publish("orchestrator:plan_draft", {
                "plan_id": plan_id,
                "goal": goal,
                "steps": steps,
                "round": round_no,
                "plan_fingerprint": self._plan_fingerprint(steps),
                # L01：Critic 是独立进程，它的 LLM 调用也要能归属到根任务
                "context": make_context(task_id, step_id="plan_review",
                                        dispatch_id=plan_id).to_wire(),
            })
            push_progress(self._messaging, task_id, "log",
                          {"type": "review", "agent": "critic",
                           "message": f"Plan submitted for review (round {round_no})",
                           "timestamp": self._now_iso()})
            # redis-py 8 的单次 brpop(timeout) 不可靠：用分片轮询到 deadline
            msg = self._brpop_with_deadline(
                r, f"plan_review:{plan_id}",
                deadline=time.time() + self._critic_timeout,
            )
            if not msg:
                self._review_unavailable(task_id, f"评审超时（{self._critic_timeout}s）",
                                         steps, bank)
                return None
            review = json.loads(msg[1])
            if not isinstance(review, dict):
                # 空/非字典回包属于协议错误：不是"没有建议"，更不能当通过
                self._review_unavailable(task_id, f"评审回包非字典：{type(review).__name__}",
                                         steps, bank)
                return None
            verdict = str(review.get("verdict", "")).upper()
            if verdict in ("ERROR", "DEGRADED") or verdict not in REVIEW_VERDICTS:
                reason = (f"评审返回 {verdict or '（缺失）'}："
                          f"{str(review.get('error') or review.get('summary') or '')[:120]}")
                self._review_unavailable(task_id, reason, steps, bank)
                return None
            return review
        except ReviewRequiredError:
            raise
        except Exception as exc:
            self._review_unavailable(task_id, f"评审异常：{str(exc)[:150]}", steps, bank)
            return None

    def _review_is_required(self) -> bool:
        """评审是否"必需"：银行口径（`WEAVEMIND_IDENTITY_MODE=bank`）下必需。

        个人模式允许降级交付，但必须在报告/状态里如实标注（不能当作通过）。
        """
        return self._identity_mode() == "bank"

    def _identity_mode(self) -> str:
        """身份模式判定；配置非法/不可判定 → 抛错，**不**按宽松模式放行。

        合法取值只认 `task_context.VALID_MODES`（local / bank），不另立词表；
        此前 `default_mode()` 抛错被吞成 False，等于"配置写错就按个人模式放行"，
        必需评审整套失效。
        """
        try:
            from task_context import VALID_MODES, default_mode
        except Exception as exc:
            raise ReviewRequiredError(f"身份模块不可用，无法判定评审要求：{exc}") from exc
        try:
            mode = str(default_mode() or "")
        except Exception as exc:
            raise ReviewRequiredError(
                f"身份模式配置非法（不得按个人模式放行）：{exc}") from exc
        if mode not in VALID_MODES:
            raise ReviewRequiredError(f"未知身份模式 {mode!r}：拒绝按宽松模式放行")
        return mode

    def _review_state(self, task_id: str) -> dict:
        """评审状态**按根任务**归属（M0-b）。

        此前是实例级 `_review_degraded` 标量：同一实例上跑另一个任务时会被清空，
        于是"这个任务的降级"可能在收尾前消失，变成看起来已通过。
        """
        states = getattr(self, "_task_review", None)
        if states is None:
            states = {}
            self._task_review = states
        st = states.get(task_id)
        if st is None:
            st = {
                "plan_fingerprint": "",
                "verdict": "",
                "policy_version": REVIEW_POLICY_VERSION,
                "mode": "",
                "degraded_reason": "",
                "rounds": 0,
                "at": 0.0,
            }
            states[task_id] = st
        return st

    def _plan_version(self, task_id: str) -> int:
        """本任务当前**计划版本号**（R1）。

        为什么需要版本号而不是只比指纹：Critic 评审发生在规划返回时，之后编排器还要做
        机械加工（依赖连线、包步骤补齐、目标/Skills 注入、结构化裁剪）与计划确认编辑，
        执行的那份 `steps` 与评审时看到的对象**本来就不同**——若按对象相等判定，
        每一次真实运行都会被判成"没有绑定的 PASS"，这个判定就废了。

        因此把"评审覆盖哪一版计划"记成版本号：机械加工不改变版本（同一版计划的物化），
        而**实质改写**（反思追加/替换计划、用户确认阶段的编辑）会 +1，于是
        "异版 PASS" 不再被复用。
        """
        versions = getattr(self, "_task_plan_versions", None)
        if versions is None:
            versions = {}
            self._task_plan_versions = versions
        return int(versions.get(task_id) or 0)

    def _bump_plan_version(self, task_id: str, why: str) -> int:
        """计划被**实质改写** → 版本 +1（此前绑定的 PASS 随之失效）。"""
        versions = getattr(self, "_task_plan_versions", None)
        if versions is None:
            versions = {}
            self._task_plan_versions = versions
        nv = int(versions.get(task_id) or 0) + 1
        versions[task_id] = nv
        st = self._review_state(task_id)
        if (str(st.get("verdict") or "") == "PASS"
                and int(st.get("plan_version") or 0) != nv
                and getattr(self, "_messaging", None) is not None):
            push_progress(self._messaging, task_id, "log",
                          {"type": "review", "agent": "critic",
                           "message": (f"计划已改写（{why}）→ 计划版本 v{nv}："
                                       "此前绑定的评审 PASS 不再覆盖该版本"),
                           "timestamp": self._now_iso()})
        return nv

    def review_scope_ok(self, task_id: str) -> bool:
        """当前持有的 PASS 是否覆盖**本任务当前这一版计划**（R1 准入/交付用）。

        与 `review_passed_for` 的分工：后者按指纹比对（调用方手里有评审时那一版计划，
        用于测试与"这版计划是否被评审过"的精确判断）；本方法按版本号判断，
        用于编排器内部——那里的 `steps` 已经过机械加工，指纹天然不同。
        """
        st = self._review_state(task_id)
        if str(st.get("verdict") or "") != "PASS":
            return False
        if str(st.get("policy_version") or "") != REVIEW_POLICY_VERSION:
            return False
        try:
            if str(st.get("mode") or "") != self._identity_mode():
                return False
        except Exception:
            return False
        bound = int(st.get("plan_version") or 0)
        return bound > 0 and bound == self._plan_version(task_id)

    def _delivery(self, task_id: str) -> dict:
        """交付状态**按根任务**归属（R1）。

        此前是三个实例标量（`_delivery_draft_reason` / `_delivery_status` /
        `_delivery_hard_fail`）：同一实例并发跑两个任务时，任务 A 收尾写入的草稿理由
        会把任务 B 已判定的 verified 翻成 false（准入判定随之误判）。按根任务存放后，
        每个任务的交付结论只受自己的证据影响。
        """
        states = getattr(self, "_task_delivery", None)
        if states is None:
            states = {}
            self._task_delivery = states
        st = states.get(task_id)
        if st is None:
            st = {"status": "", "reason": "", "hard_fail": ""}
            states[task_id] = st
        return st

    def _budget(self, task_id: str):
        """根任务预算（M0-e）：一次任务一份账，落盘在任务工作区，跨进程/恢复共用。"""
        budgets = getattr(self, "_task_budgets", None)
        if budgets is None:
            budgets = {}
            self._task_budgets = budgets
        b = budgets.get(task_id)
        if b is None:
            from root_budget import RootBudget, limits_from_config
            try:
                cfg = self._load_cfg_snapshot()
            except Exception:
                cfg = {}
            limits = limits_from_config(cfg)
            # R2：0 就是**真正不限**。此前全 0 时会拿 `system.task_timeout` 兜底成
            # 时间上限——于是"我把预算都设成 0（不限）"实际得到的是"600 秒硬截止"，
            # 账本里记的上限和配置写的不是一回事。
            b = RootBudget(task_id, task_workspace(task_id), limits,
                           redis_factory=self._budget_redis_factory)
            budgets[task_id] = b
        return b

    def _budget_redis_factory(self):
        """预算账本的跨进程后端。

        预留必须是**跨进程原子**的（INCRBY 先加后校验）：主进程、Worker、评审进程
        各有一份内存账本时，"两个进程各自预留都获准"就会把同一份额度花两次。
        Redis 不可用时退回单进程语义（文件仍是快照，但并发写是后写覆盖）。

        **短超时**是刻意的：账本在预留、收尾、看门狗等热路径上被读，而消息总线用的
        客户端连接超时是 5 秒（为长连接 pubsub 保留）。Redis 不在时那 5 秒会把一次
        告警、一次预留拖到秒级（实测：看门狗告警被拖到断言之后才发出）。这里只要
        0.3s 连接 / 0.5s 读，失败立刻降级为本地账本。
        """
        try:
            import redis as _redis
            try:
                from common import _NO_REDIS_RETRY as _noretry
            except Exception:
                from redis.backoff import NoBackoff as _NoBackoff
                from redis.retry import Retry as _Retry
                _noretry = _Retry(_NoBackoff(), 0)
            return _redis.Redis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_timeout=0.5,
                socket_connect_timeout=0.3,
                retry=_noretry,
            )
        except Exception as exc:
            logger.warning("预算跨进程后端不可用（按单进程记账）：%s", str(exc)[:100])
            return None

    def _load_cfg_snapshot(self) -> dict:
        """读取 config.json（`system.budget` 等）。

        与 `system` 段热重载同源：用编排器自己的配置路径。此前尝试的
        `llm_client._load_config` 并不存在 → 静默回落成 {} → 配置里的预算上限
        不生效（实测账本记的是 `task_timeout` 兜底值，而不是配置值）。
        """
        path = str(getattr(self, "_system_cfg_path", "") or "")
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}

    def _budget_reserve(self, task_id: str, stage: str, **kw) -> str:
        """发送前预留；预算不足抛 `BudgetExceeded`（调用方决定如何收尾）。"""
        return self._budget(task_id).reserve(stage, **kw)

    def _budget_settle(self, task_id: str, ticket: str, **kw) -> None:
        try:
            self._budget(task_id).settle(ticket, **kw)
        except Exception:
            pass

    def _budget_note(self, task_id: str, stage: str, **detail) -> None:
        """记一次有效进展（心跳不算进展）。"""
        try:
            self._budget(task_id).note_progress(stage, detail)
        except Exception:
            pass

    def budget_snapshot(self, task_id: str) -> dict:
        """阶段观测快照（含剩余预算与各阶段等待对象/最近有效进展）。"""
        try:
            return self._budget(task_id).snapshot()
        except Exception as exc:
            return {"error": str(exc)[:120]}

    def _call_llm_cancellable(self, task_id: str, phase: str, fn, *args,
                              ticket: str = "", **kwargs):
        """在**可取消的等待**里跑一次 LLM 调用（M0-c/M0-e）。

        单次模型调用本身不可中断（它跑在 `llm_client` 里，含自身重试与主备切换），
        但**等待**可以中断：调用放到工作线程，主线程按 1 秒分片轮询取消标志。
        取消命中就放弃等待、抛 `TaskCancelled` 让 run() 走取消收尾。

        R2：取消时把**真实票据**（`ticket`，即发送前预留的那张）转"待对账"——
        该调用可能仍在计费，不冒充已停止。此前这里写的是凭空造的
        `f"{phase}-inflight"` 票据：账面多一张不存在的票据，而真正预留的那张仍挂在
        `open` 上，随后又被调用方的 `finally` 结算成"成功"，于是"取消后到底停没停"
        在账本里再也读不出来。票据状态改为互斥迁移后，这里只动真票据。
        """
        box: dict = {}

        # R2：新线程默认**不继承** contextvars（`threading.Thread` 拿到的是空上下文）。
        # 之前在 `_run` 里丢掉任务上下文 → worker/客户端按任务归属的台账记不上，
        # 取消守卫也拿不到 task_id（于是取消后仍会重试、切备用）。这里在**创建边界**
        # 显式把上下文拷进线程，并补一次 set_task_context（编排器知道根任务 id）。
        import contextvars
        _ctx = contextvars.copy_context()

        def _run() -> None:
            try:
                from llm_client import set_task_context
                # 在**拷贝出来的上下文里**设置：set 只影响当前上下文，若在拷贝之外
                # 设置，`_ctx.run(fn)` 里读到的仍是空值（实测踩过）
                _ctx.run(set_task_context, task_id)
            except Exception:
                pass
            try:
                box["value"] = _ctx.run(fn, *args, **kwargs)
            except BaseException as exc:       # 原样带回主线程（含 KeyboardInterrupt）
                box["error"] = exc

        th = threading.Thread(target=_run, daemon=True,
                              name=f"llm-{phase}-{task_id}")
        th.start()
        while th.is_alive():
            if self._cancel_requested(task_id):
                logger.info("取消命中：放弃等待 %s 阶段的模型调用（task=%s）",
                            phase, task_id)
                moved = 0
                try:
                    b = self._budget(task_id)
                    if ticket:
                        moved = 1 if b.mark_unsettled(
                            ticket, reason="取消后不再等待本次模型调用") else 0
                    else:
                        # 该阶段的每次调用各自预留票据（调用方未透传时的兜底）：
                        # 只把当前 open 的那些转待对账，不造票据
                        moved = b.mark_stage_unsettled(
                            phase, reason="取消后不再等待本次模型调用")
                except Exception as exc:
                    logger.warning("取消时票据转待对账失败：%s", str(exc)[:100])
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": (f"已取消：放弃等待{phase}阶段的模型调用"
                                           f"（{moved} 次调用可能仍在计费，已转待对账）"),
                               "timestamp": self._now_iso()})
                # 票据已转待对账 → 告诉调用方不要再去结算它（互斥迁移）
                raise TaskCancelled(
                    f"取消：放弃等待{phase}阶段模型调用", ticket_settled=True)
            th.join(1.0)
        if "error" in box:
            err = box["error"]
            # R2：客户端在重试/切备前看到取消后抛的是 LLMCancelledError——那是"取消"，
            # 不是"调用失败"，交给 run() 的取消收尾（当失败处理会触发重试/降级）
            try:
                from llm_client import LLMCancelledError
            except Exception:
                LLMCancelledError = ()          # type: ignore[assignment]
            if LLMCancelledError and isinstance(err, LLMCancelledError):
                raise TaskCancelled(f"取消：{phase}阶段的模型调用不再重试/切换端点")
            raise err
        return box.get("value")

    def _budget_scope(self, task_id: str, stage: str, **detail):
        """一次计费调用的**预留—结算**上下文：唯一正确的票据生命周期。

        用法：
            with self._budget_scope(task_id, "plan", attempt=1) as ticket:
                raw = self._call_llm_cancellable(task_id, "规划", fn, ticket=ticket, ...)

        出口规则（R2）：
        - 正常返回 → `settle(ok=True)`；
        - 抛 `TaskCancelled(ticket_settled=True)`（helper 已把票据转待对账）→ **不**结算；
        - 其它异常 → `settle(ok=False)`（失败也花了钱，不能当没发生）。
        """
        return _BudgetScope(self, task_id, stage, detail)

    # 计划指纹只取**定义这一版计划**的字段；执行期回填的字段不进指纹。
    # 否则同一版计划在执行前（评审时）与执行后（`iteration`/`status` 被回填）会算出
    # 两个指纹，"PASS 绑定在这版计划上"就永远无法在交付口被验证。
    _PLAN_FP_STR_FIELDS = ("step_id", "capability", "instruction")
    _PLAN_FP_LIST_FIELDS = ("depends_on",)

    def _plan_fingerprint(self, steps: list[dict]) -> str:
        """计划指纹：PASS 绑定的对象是**这一版计划**，不是"某个计划"。

        按 `_PLAN_FP_*_FIELDS` 做规范投影——缺字段与空字段等价（依赖连线等加工
        会补上 `depends_on`，不该因此算作另一版计划），依赖顺序无关（排序后入指纹），
        步骤顺序敏感（顺序即并行拓扑的输入）。
        """
        projection = []
        for s in (steps or []):
            if not isinstance(s, dict):
                projection.append({"raw": str(s)})
                continue
            item = {f: str(s.get(f) or "") for f in self._PLAN_FP_STR_FIELDS}
            for f in self._PLAN_FP_LIST_FIELDS:
                raw = s.get(f) or []
                if isinstance(raw, (list, tuple)):
                    item[f] = sorted(str(x) for x in raw)
                else:
                    item[f] = [str(raw)]
            item["round"] = s.get("round")
            projection.append(item)
        payload = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _review_bind_pass(self, task_id: str, steps: list[dict]) -> dict:
        st = self._review_state(task_id)
        st.update({
            "plan_fingerprint": self._plan_fingerprint(steps),
            # R1：PASS 覆盖的是**这一版**计划；版本号与指纹一起记，恢复/准入据此判断
            "plan_version": self._plan_version(task_id),
            "verdict": "PASS",
            "policy_version": REVIEW_POLICY_VERSION,
            "mode": self._identity_mode(),
            "degraded_reason": "",
            "at": time.time(),
        })
        return st

    def _review_mark_degraded(self, task_id: str, reason: str,
                              *, plan: list[dict] | None = None) -> None:
        """记一次"评审未完成/降级"——只在个人模式用；银行模式走拒绝路径。"""
        st = self._review_state(task_id)
        st.update({
            "verdict": "DEGRADED",
            "policy_version": REVIEW_POLICY_VERSION,
            "mode": self._identity_mode(),
            "degraded_reason": str(reason or ""),
            "at": time.time(),
        })
        if plan is not None:
            st["plan_fingerprint"] = self._plan_fingerprint(plan)

    def review_passed_for(self, task_id: str, steps: list[dict]) -> bool:
        """该任务当前的 PASS 是否**绑定**在这版计划与当前策略版本上。"""
        st = self._review_state(task_id)
        return bool(
            st.get("verdict") == "PASS"
            and st.get("plan_fingerprint") == self._plan_fingerprint(steps)
            and st.get("policy_version") == REVIEW_POLICY_VERSION
        )

    @staticmethod
    def _with_draft_note(report: str, reason: str) -> str:
        """未验收草稿注记（B 批起唯一实现在 `delivery_pipeline`）。"""
        from delivery_pipeline import with_draft_note
        return with_draft_note(report, reason)

    def _with_review_note(self, task_id: str, delivery: str) -> str:
        """把评审状态写进交付说明并落盘（M0-b；B 批起注记文本与落盘走共享实现）。

        B1b：落盘时**记下这个 PASS 属于哪一版**（`report_version_id`）与"是否必需"
        （`required`）——人工修订产生新版本后，旧 PASS 不再适用（不迁移），
        而个人模式本来不要求评审，因此不受影响。两个事实都持久化，另一个进程
        （web_ui）也能算出同一个结论。
        """
        from delivery_pipeline import (read_review_facts, review_note_text,
                                       write_review_facts)
        st = self._review_state(task_id)
        verdict = str(st.get("verdict") or "NONE")
        facts = {
            "verdict": verdict,
            # 注记标签是**当时**记下的（如"未经评审（critic 关闭）"）：不能按 verdict
            # 反推，否则 critic 关闭与评审降级会被说成同一件事
            "label": str(st.get("label") or ("PASS" if verdict == "PASS" else "")),
            "degraded_reason": str(st.get("degraded_reason") or ""),
            "required": bool(self._review_is_required()),
        }
        # 工作区走本模块名（可替换），并把同一路径显式传给共享实现——否则两边各算
        # 一次工作区，注记与落盘文件可能落在不同位置
        ws = task_workspace(task_id)
        write_review_facts(task_id, facts, ws_dir=ws)
        # 注记以**内存事实**为准（写盘失败也不能让交付物变成"未执行评审"的通用文案）；
        # 读回仅用于与文件对账，取到非空值才覆盖
        merged = dict(facts)
        for k, v in (read_review_facts(task_id, ws_dir=ws) or {}).items():
            if v not in ("", None) and k in merged:
                merged[k] = v
        merged["required"] = facts["required"]
        note = review_note_text(merged)
        return (delivery + "\n\n" + note) if note else delivery

    def _restore_review_state(self, task_id: str, saved: dict,
                              plan: list[dict] | None = None) -> bool:
        """恢复 checkpoint 里的评审状态；返回是否复用了仍有效的 PASS。

        复用条件：裁决 PASS、同一策略版本、同一身份模式、**同一计划版本**。
        计划版本这一条是 R1 补的：checkpoint 里的 PASS 绑定在它当时评审的那一版计划上，
        若落盘之后计划又被改写（反思追加步骤等），恢复时不能把旧 PASS 当成覆盖新版计划。
        旧 checkpoint 没有 `plan_version` 字段 → 视为**无法证明**，不复用（拦下而不是放行）。
        任一不符 → 按"未完成评审"处置（银行拒绝 / 个人记降级），也不默认已通过。
        """
        cur_mode = self._identity_mode()
        saved = saved if isinstance(saved, dict) else {}
        try:
            saved_pv = int(saved.get("plan_version") or 0)
        except (TypeError, ValueError):
            saved_pv = 0
        cur_pv = self._plan_version(task_id)
        ok = (str(saved.get("verdict") or "") == "PASS"
              and str(saved.get("policy_version") or "") == REVIEW_POLICY_VERSION
              and str(saved.get("mode") or "") == cur_mode
              and saved_pv > 0 and saved_pv == cur_pv)
        st = self._review_state(task_id)
        if ok:
            st.update({
                "plan_fingerprint": str(saved.get("plan_fingerprint") or ""),
                "plan_version": saved_pv,
                "verdict": "PASS",
                "policy_version": REVIEW_POLICY_VERSION,
                "mode": cur_mode,
                "degraded_reason": "",
                "rounds": int(saved.get("rounds") or 0),
                "at": float(saved.get("at") or 0.0),
            })
            push_progress(self._messaging, task_id, "log",
                          {"type": "review", "agent": "critic",
                           "message": (f"恢复：复用仍有效的评审 PASS"
                                       f"（同策略版本/同身份模式/同计划版本 v{saved_pv}）"),
                           "timestamp": self._now_iso()})
            return True
        if str(saved.get("verdict") or "") == "PASS" and saved_pv != cur_pv:
            logger.warning("恢复：checkpoint 的 PASS 绑定计划v%s，当前为 v%s，不复用"
                           "（task=%s）", saved_pv or "未知", cur_pv, task_id)
        self._require_review_or_refuse(
            task_id, "恢复的计划没有仍有效的评审 PASS", plan=plan)
        return False

    def _require_review_or_refuse(self, task_id: str, reason: str,
                                  *, plan: list[dict] | None = None) -> None:
        """按身份模式统一处置"必需评审未完成"：银行拒绝，个人记降级。"""
        if self._review_is_required():
            logger.error("必需评审未完成（银行口径），拒绝继续：%s", reason)
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": "critic",
                           "message": f"必需评审未完成，按策略拒绝继续：{reason}",
                           "timestamp": self._now_iso()})
            raise ReviewRequiredError(reason)
        logger.warning("评审未完成（降级，个人模式继续）：%s", reason)
        self._review_mark_degraded(task_id, reason, plan=plan)
        push_progress(self._messaging, task_id, "log",
                      {"type": "review", "agent": "critic",
                       "message": f"未完成评审（降级）：{reason}——继续执行，交付物需人工复核",
                       "timestamp": self._now_iso()})

    def _review_unavailable(self, task_id: str, reason: str,
                            steps: list[dict], bank: bool) -> list[dict]:
        """评审不可用（超时/异常/ERROR/裁决非法）时的统一处置。"""
        if bank:
            logger.error("必需评审未完成（银行口径），拒绝继续：%s", reason)
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": "critic",
                           "message": f"必需评审未完成，按策略拒绝继续：{reason}",
                           "timestamp": self._now_iso()})
            raise ReviewRequiredError(reason)
        self._review_mark_degraded(task_id, reason, plan=steps)
        push_progress(self._messaging, task_id, "log",
                      {"type": "review", "agent": "critic",
                       "message": f"未完成评审（降级）：{reason}——继续执行，交付物需人工复核",
                       "timestamp": self._now_iso()})
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
        # 默认超时取 system.task_timeout（C3 热重载后对下一任务生效）。
        # 步骤显式 timeout 优先（规划器按规则 17 设定），无显式值时用 config 兜底。
        task_timeout = int(getattr(self, "_task_timeout", 300) or 300)
        _step_t = step.get("timeout")
        timeout = max(int(_step_t or task_timeout), 300)
        if capability == "code_execution":
            timeout = max(timeout, 600)

        agent_id = self._find_agent(capability)
        if not agent_id:
            # Worker 可能瞬时掉线（如 Redis 超时重连）或服务栈冷启动尚未
            # 注册完成（实测重启后首任务 15s 内查不到 web_search worker）：
            # 等待重查后再判失败（6×5s=30s 冷启动窗口）
            for _ in range(6):
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
        # V2-1 取消闸门：**所有**派发都经过这里，取消后不再发起新步骤与新付费调用
        # （此前首次派发无取消检查，重做/修复/降级重派等路径也能在取消后继续派发）
        if self._cancel_requested(task_id):
            logger.info("已取消：跳过派发（step=%s, capability=%s）", step_id, capability)
            push_progress(self._messaging, task_id, "log",
                          {"type": "info", "agent": "orchestrator",
                           "message": f"已取消，跳过步骤 {step_id}（{capability}）的派发",
                           "timestamp": self._now_iso()})
            return {"task_id": step_id, "status": "CANCELLED",
                    "result": "任务已取消，该步骤未派发"}
        # 唯一派发 ID：避免 task_result:{step_id} 与其它任务/历史残留键碰撞
        # （步骤 ID 如 "1"/"2" 在所有任务中通用，曾导致跨任务误取结果）
        dispatch_id = f"{step_id}-{uuid.uuid4().hex[:8]}"
        with self._task_starts_lock:
            task_start_ts = self._task_starts.get(task_id, time.time())
        # L01：派发载荷带版本化身份上下文。`task_id` 仍是派发 id（结果通道
        # `task_result:{dispatch_id}` 与旧 Worker 都靠它），根任务身份放在 context 里
        # ——此前 Worker 只拿到派发 id，LLM 台账记在派发键上，根任务台账恒为空。
        # M0-e：先预留再发送（预算不足**拒绝发送**，不是"先花再算"）
        try:
            _ticket = self._budget_reserve(
                task_id, "step",
                detail={"step_id": step_id, "capability": capability,
                        "dispatch_id": dispatch_id})
        except Exception as exc:
            logger.error("根任务预算不足，拒绝派发步骤 %s：%s", step_id, str(exc)[:150])
            # 预算耗尽必须**停止本任务的后续尝试**：只拒绝这一次派发的话，
            # 重试/重做循环会每 2 秒再试一次，任务永远收不了尾（实机观测）
            try:
                self._budget_exhausted[task_id] = str(exc)[:200]
            except Exception:
                pass
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": capability,
                           "message": f"预算不足，未派发步骤 {step_id}：{str(exc)[:120]}",
                           "timestamp": self._now_iso()})
            return {"task_id": step_id, "status": "FAILED", "budget_exceeded": True,
                    "result": f"根任务预算不足，未派发：{str(exc)[:150]}"}

        ctx = make_context(root_task_id=task_id, step_id=step_id, dispatch_id=dispatch_id)
        r.lpush(f"task_queue:{agent_id}", json.dumps({
            "task_id": dispatch_id,
            "context": ctx.to_wire(),
            "instruction": instruction,
            "task_start_ts": task_start_ts,
            "step_deadline": time.time() + timeout,
            "workspace": str(task_workspace(task_id)),
            "simple": bool(self._task_simple.get(task_id, False)),
            # 目标文本随派发下发：打包步骤据此判断"预载数据是否为交付物"
            # （预载的行情/结构化数据默认不进交付包，除非目标明确要数据文件）
            "goal": str(
                (getattr(self, "_task_goals", {}) or {}).get(task_id, "") or ""
            )[:400],
        }, ensure_ascii=False))

        # Wait for result（M0-c：等待结果分四类，取消不再被写成"超时"）
        self._track_inflight(task_id, dispatch_id, time.time())
        try:
            out = self._wait_step_result(dispatch_id, timeout, cancel_task_id=task_id)
        except Exception as exc:
            out = WaitOutcome(WAIT_PROTOCOL, reason=f"等待步骤结果异常：{str(exc)[:120]}")
        if out.kind == WAIT_CANCEL:
            # 取消时**不**销账：worker 可能仍在跑、仍在计费，留痕供对账
            self._mark_inflight_unsettled(task_id, dispatch_id)
            self._budget(task_id).mark_unsettled(_ticket, reason=out.reason)
            self._budget(task_id).note_progress("step", {
                "step_id": step_id, "capability": capability,
                "dispatch_id": dispatch_id, "cancelled": True})
        else:
            self._untrack_inflight(task_id, dispatch_id)
            self._budget_settle(
                task_id, _ticket, ok=(out.kind == WAIT_RESULT),
                note=f"{capability}:{out.kind}")
            self._budget_note(task_id, "step", step_id=step_id,
                              capability=capability, outcome=out.kind)
        if out.kind == WAIT_CANCEL:
            push_progress(self._messaging, task_id, "log",
                          {"type": "info", "agent": capability,
                           "message": f"已取消，停止等待步骤 {step_id}（{out.reason}）",
                           "timestamp": self._now_iso()})
            return {"task_id": step_id, "status": "CANCELLED", "cancelled": True,
                    "result": f"任务已取消：{out.reason}"}
        if out.kind == WAIT_TIMEOUT:
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": capability,
                           "message": f"Step {step_id} timed out ({timeout}s)",
                           "timestamp": self._now_iso()})
            return {"task_id": step_id, "status": "FAILED", "result": f"Timeout after {timeout}s"}
        if out.kind == WAIT_PROTOCOL:
            # 协议错误不是超时：日志与结果都要说清是哪一类，别混成"没等到"
            push_progress(self._messaging, task_id, "log",
                          {"type": "error", "agent": capability,
                           "message": f"Step {step_id} 结果不合协议：{out.reason}",
                           "timestamp": self._now_iso()})
            return {"task_id": step_id, "status": "FAILED", "protocol_error": True,
                    "result": f"结果协议错误：{out.reason}"}
        result = out.result
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
                           "message": f"Step {step_id} empty result", "timestamp": self._now_iso()})
            result = {"task_id": step_id, "status": "FAILED", "result": "Empty result"}

        return result

    def _inflight_map(self) -> dict:
        table = getattr(self, "_inflight", None)
        if table is None:
            table = {}
            self._inflight = table
        return table

    def _track_inflight(self, task_id: str, dispatch_id: str, started: float) -> None:
        """登记在飞派发：等待提前返回**不代表** worker 停止（M0-c/M0-e 对账依据）。"""
        lock = getattr(self, "_inflight_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._inflight_lock = lock
        with lock:
            self._inflight_map().setdefault(task_id, {})[dispatch_id] = started

    def _untrack_inflight(self, task_id: str, dispatch_id: str) -> None:
        lock = getattr(self, "_inflight_lock", None)
        if lock is None:
            return
        with lock:
            self._inflight_map().get(task_id, {}).pop(dispatch_id, None)

    def _mark_inflight_unsettled(self, task_id: str, dispatch_id: str) -> None:
        """取消/放弃等待时把在飞调用转入"待对账"：可能已计费但结果不再读取。"""
        lock = getattr(self, "_inflight_lock", None)
        started = 0.0
        if lock is not None:
            with lock:
                started = float(
                    self._inflight_map().get(task_id, {}).get(dispatch_id, 0.0) or 0.0)
        record = {"dispatch_id": dispatch_id, "started_at": started,
                  "marked_at": time.time(), "task_id": task_id,
                  "reason": "取消后不再读取结果，调用可能仍在计费，需对账"}
        try:
            path = task_workspace(task_id) / "inflight_unsettled.json"
            data = []
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                data = raw if isinstance(raw, list) else []
            data = [d for d in data if str(d.get("dispatch_id")) != dispatch_id] + [record]
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.warning("在飞未结算记录写入失败（task=%s）：%s", task_id, str(exc)[:100])
        logger.warning("在飞调用转入待对账（task=%s, dispatch=%s）", task_id, dispatch_id)

    def _wait_step_result(self, task_id: str, timeout: int,
                          cancel_task_id: str = "") -> WaitOutcome:
        """等待步骤结果，返回**分类**结果：result / cancel / timeout / protocol。

        M0-c：取消、超时、协议错误必须能区分开——上层据此决定是否记超时日志、
        是否写 step_failure、是否按取消收尾。空字典、非字典、JSON 解析失败、
        status 与 result 均缺失，一律算**协议错误**（不是成功，也不是"没消息"）。

        R2：等待上限还要受**根任务 deadline** 约束——步骤超时（常是 300s 下限）
        比根任务剩余时间还长时，等到根预算早就用尽了才返回，属于"没额度的空等"。
        """
        root_left = None
        try:
            root_left = self._budget(cancel_task_id).remaining().get("seconds") \
                if cancel_task_id else None
        except Exception:
            root_left = None
        if root_left is not None:
            timeout = min(float(timeout), max(1.0, float(root_left)))
        deadline = time.time() + max(timeout, 5)
        while True:
            slice_end = min(deadline, time.time() + 1.0)
            try:
                r = self._new_redis_sync()
                msg = self._brpop_with_deadline(r, f"task_result:{task_id}", slice_end)
            except Exception as exc:
                return WaitOutcome(WAIT_PROTOCOL, reason=f"读取结果通道失败：{str(exc)[:100]}")
            if msg:
                try:
                    payload = json.loads(msg[1])
                except Exception as exc:
                    logger.warning("Result parse error for %s: %s", task_id, exc)
                    return WaitOutcome(WAIT_PROTOCOL, reason=f"结果不是合法 JSON：{str(exc)[:80]}")
                if not isinstance(payload, dict):
                    return WaitOutcome(
                        WAIT_PROTOCOL, reason=f"结果不是字典：{type(payload).__name__}")
                if not payload:
                    return WaitOutcome(WAIT_PROTOCOL, reason="结果为空字典")
                if payload.get("status") is None and payload.get("result") is None:
                    return WaitOutcome(
                        WAIT_PROTOCOL, reason="结果缺关键字段（status 与 result 均缺失）")
                return WaitOutcome(WAIT_RESULT, result=payload)
            if cancel_task_id and self._cancel_requested(cancel_task_id):
                logger.info("收到取消请求：停止等待步骤结果（dispatch=%s）", task_id)
                return WaitOutcome(WAIT_CANCEL, reason="用户取消：停止等待该步骤结果")
            if time.time() >= deadline:
                return WaitOutcome(WAIT_TIMEOUT, reason=f"等待步骤结果超时（{timeout}s）")

    def _wait_for_result(self, task_id: str, timeout: int,
                         cancel_task_id: str = "") -> dict | None:
        """兼容包装：只返回结果本体，取消/超时/协议错误都是 None。

        新调用点请用 `_wait_step_result`——None 无法区分"取消"与"超时"。
        """
        return self._wait_step_result(
            task_id, timeout, cancel_task_id).result

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

        report = f"## Task Report\n\nGoal: {goal}\nStatus: {status}\nSteps: {len(steps)} ({ok} OK, {fail} failed)\n\n"
        for s, r in zip(steps, results):
            report += f"- [{r.get('status', '?')}] {s.get('instruction', '?')[:60]}"
            if r.get("result"):
                report += f"\n  Result: {str(r['result'])[:200]}"
            report += "\n"
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
                 f"**步骤**：{ok}/{len(results)} 成功" if ok == len(results)
                 else f"**步骤**：{ok}/{len(results)} 成功（其余失败或未完成）", ""]
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

    def _is_research_task(self, task_id: str) -> bool:
        """是否有**落库研究契约**（表单提交的研究任务）。

        用于图表分流：研究任务的图必须回答"公司怎么样"（两期对比/同比/质量比率），
        而不是"我搜了多少"（词频/域名分布）。
        """
        try:
            from working_paper_export import resolve_request
            req, _cands, source = resolve_request(task_id, "", {}, None)
            return source == "stored" and req is not None
        except Exception as exc:
            logger.warning("研究任务判定失败（task=%s）：%s", task_id, str(exc)[:120])
            return False

    def _financial_chart_specs(self, task_id: str, goal: str) -> list[dict]:
        """研究任务的财务分析图规格（确定性：底稿 → 两期对比 / 同比 / 质量三张）。

        数据来自 `working_paper_export.chart_rows`（只保留契约期间、含同比与比率），
        不经过 LLM，也不走"把财务行归一成市场规模"那条老路。非研究任务返回 []。
        """
        try:
            from chart_specs import financial_research_specs
            from working_paper_export import chart_rows
            data = chart_rows(task_id, goal)
            if not data.get("ok"):
                return []
            req = data.get("request") or {}
            # A1：研究对象类型决定哪些图适用（金融机构不出企业口径比率图）
            try:
                from facts import subject_type_of
                stype, _src = subject_type_of(
                    declared=str(req.get("subject_type") or ""),
                    company=str(req.get("company") or ""),
                    company_id=str(req.get("company_id") or ""))
            except Exception:
                stype = "unknown"
            return financial_research_specs(
                data.get("rows") or [], data.get("derived") or [],
                unit=str(data.get("unit") or ""),
                source=str(data.get("source_label") or ""),
                company=str(req.get("company") or req.get("company_id") or ""),
                caliber=str(req.get("caliber") or ""),
                periods=list(data.get("periods") or []),
                # 对比图只画契约点名的必需指标：底稿里还有总资产/毛利率这类
                # 支撑事实，混进同一张金额图会把小项压成看不见（实机踩过）
                core_metrics=list(req.get("required_metrics") or []),
                subject_type=stype,
            )
        except Exception as exc:
            logger.warning("财务图规格生成失败（task=%s）：%s", task_id, str(exc)[:140])
            return []

    @staticmethod
    def _wants_visualization(goal: str) -> bool:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._wants_visualization(goal)

    @staticmethod
    def _extract_chart_data(text: str) -> list[dict]:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._extract_chart_data(text)

    @staticmethod
    def _extract_chart_rows_from_table(text: str) -> list[dict]:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._extract_chart_rows_from_table(text)

    @staticmethod
    def _filter_chart_rows(rows: list[dict], goal: str) -> list[dict]:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._filter_chart_rows(rows, goal)

    @staticmethod
    def _excluded_for(goal: str) -> tuple[str, ...]:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._excluded_for(goal)

    @staticmethod
    def _goal_core(goal: str) -> list[str]:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._goal_core(goal)

    @classmethod
    def _filter_chart_specs(cls, specs: list[dict], goal: str) -> list[dict]:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._filter_chart_specs(specs, goal)

    @staticmethod
    def _clean_rows_to_specs(clean: dict) -> list[dict]:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._clean_rows_to_specs(clean)

    @staticmethod
    def _ranking_volume_price_scatter(clean: dict) -> list[dict]:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._ranking_volume_price_scatter(clean)



    @staticmethod
    def _backfill_chart_manifest(project) -> None:
        """图表装配逻辑已迁移至 chart_assembly（深化拆分），此处为薄委托。"""
        return chart_assembly._backfill_chart_manifest(project)

    @staticmethod
    def _is_game_goal(goal: str) -> bool:
        """T9：委托 e2e_verify 模块实现。"""
        from e2e_verify import is_game_goal
        return is_game_goal(goal)

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
        """T9：委托 e2e_verify 模块实现。"""
        from e2e_verify import sweep_workspace_artifacts
        sweep_workspace_artifacts(task_id)

    @staticmethod
    def _rewrite_report_links(report: str, task_id: str) -> str:
        """工作区绝对路径 → 前端可访问 URL（B 批起唯一实现在 `delivery_pipeline`）。"""
        from delivery_pipeline import rewrite_report_links
        return rewrite_report_links(report, task_id)

    def _run_e2e_verification(
        self, files: list[dict], project_dir: str, game_goal: bool = True,
    ) -> list[dict]:
        """T9：委托 e2e_verify 模块实现（原主体已搬迁）。"""
        from e2e_verify import run_e2e_verification
        return run_e2e_verification(files, project_dir, game_goal)

    def _playwright_verify(
        self, project_dir: str, rel_name: str, fp: str,
        require_game: bool = True,
    ) -> tuple[bool, str, str, bool]:
        """T9：委托 e2e_verify 模块实现（原主体已搬迁）。
        第 4 个返回值为降级标志：True=Playwright 不可用可静态兜底。"""
        from e2e_verify import playwright_verify
        return playwright_verify(project_dir, rel_name, fp, require_game)

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


    # ── Main Loop ──
    def _start_phase_monitor(self, task_id: str,
                             stop_evt: "threading.Event") -> "threading.Thread":
        """阶段看门狗（分级第一步：告警，不终止）。

        判定依据是 ws_helpers 的统一阶段心跳：某阶段超过 `_stall_timeout`
        没有心跳 → 发一条 warning 事件（含已等待时长与阶段名），让"卡在 LLM 调用"
        在控制台/日志里可见。真正的重试与超时由各阶段既有逻辑处理。"""
        try:
            threshold = int(
                os.environ.get("WM_PHASE_STALL_SECONDS")
                or max(30, int(getattr(self, "_stall_timeout", 60) or 60))
            )
        except Exception:
            threshold = max(30, int(getattr(self, "_stall_timeout", 60) or 60))
        threshold = max(1, threshold)
        interval = 10.0
        try:
            # 轮询间隔（默认 10s）；下限 0.1s 只影响轮询频率，便于测试与调优
            interval = max(0.1, float(os.environ.get("WM_PHASE_WATCH_INTERVAL", "10") or 10))
        except Exception:
            interval = 10.0

        def _loop():
            last_alert = 0.0
            while not stop_evt.wait(interval):
                # 自终止：任务已不在运行（正常完成/失败/被清理）即退出，
                # 不必在每个 return 路径上穿线设置 stop 事件
                try:
                    if not self._task_is_running(task_id):
                        return
                except Exception:
                    pass
                try:
                    from ws_helpers import phase_state
                    state = phase_state(task_id)
                    if not state:
                        continue
                    age = time.time() - float(state.get("last_beat") or 0)
                    if age < threshold or time.time() - last_alert < threshold:
                        continue
                    last_alert = time.time()
                    # M0-e 阶段观测：卡住时把"等待对象/最近有效进展/剩余预算/取消状态"
                    # 一起报出来——只报"卡了多久"没法判断是等模型、等 worker 还是没钱了
                    _budget = self.budget_snapshot(task_id)
                    _stage = str(state.get("phase") or "")
                    _detail = ((_budget.get("stages") or {}).get(_stage) or {})
                    push_progress(self._messaging, task_id, "log", {
                        "type": "warning",
                        "agent": "orchestrator",
                        "message": (
                            f"{state.get('phase') or '当前'}阶段已 {age:.0f}s 无进展"
                            f"（疑似 LLM 调用阻塞，阈值 {threshold}s）；仍在等待，"
                            "超时后会重试或降级"
                        ),
                        "phase": _stage,
                        "elapsed_seconds": round(age, 1),
                        "waiting_on": _detail.get("waiting_on") or {},
                        "last_progress": _detail.get("last_progress") or {},
                        "since_progress_sec": _detail.get("since_progress_sec"),
                        "budget_remaining": _budget.get("remaining") or {},
                        "cancelled": self._cancel_requested(task_id),
                        "timestamp": self._now_iso(),
                    })
                    logger.warning(
                        "Phase watchdog: %s stuck for %.0fs (task %s)",
                        state.get("phase"), age, task_id,
                    )
                except Exception as exc:
                    # 不能静默：看门狗自身出错会表现为"永远不告警"
                    logger.warning("Phase watchdog error for %s: %s",
                                   task_id, str(exc)[:150])
                    continue

        thread = threading.Thread(target=_loop, daemon=True,
                                 name=f"phase-watch-{task_id}")
        thread.start()
        return thread

    def _finalize_task(self, task_id: str, goal: str, status: str,
                       report: str = "", steps: list | None = None,
                       logs: list | None = None,
                       acceptance: dict | None = None) -> None:
        """终态唯一落库点（状态写者收口的一部分）。

        编排器是 task_history 的**唯一写者**：终态（含 6 个提前 return 路径）都经此
        写库并记录已终态，避免出现"进程内认为结束、DB 里永远 RUNNING"的空洞。
        """
        try:
            if not hasattr(self, "_finalized_tasks"):
                self._finalized_tasks = set()
            if task_id in self._finalized_tasks:
                return
            import task_state as _ts
            # 本地 SQLite 的失败多半是瞬时争用（库忙/锁），小幅重试即可；重试后仍失败
            # 就**不**算已终结：库里会停在 RUNNING，由 stale 兜底与后续重试处理，
            # 并在 Redis 留一个可查标记（此前只写一行 warning，等于没人知道）。
            ok, err = False, ""
            for attempt in range(3):
                try:
                    ok = bool(_ts.record_completion(
                        task_id, goal=goal, status=status, report=report or "",
                        steps=steps or [], logs=logs or [], acceptance=acceptance or {},
                    ))
                except Exception as exc:
                    ok, err = False, str(exc)
                if ok:
                    break
                if attempt < 2:
                    time.sleep(0.2)
            if ok:
                self._finalized_tasks.add(task_id)
            else:
                logger.error("任务 %s 终态未落库（已尝试 3 次），不标记为已终结：%s",
                             task_id, err[:150] or "任务库不可写")
                try:
                    _ts.mark_persist_failed(task_id, err or "任务库不可写")
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("Finalize task %s failed: %s", task_id, str(exc)[:150])

    def run(self, task_id: str, goal: str, context: str = "",
            auto_run: bool = True, template_steps: list | None = None,
            user_id: str = "", project: str | None = None,
            report_confirm: bool = False) -> dict:
        """Execute a full task lifecycle. Returns final status dict."""
        try:
            from llm_client import set_task_context, set_cancel_guard
            set_task_context(task_id)
            # R2：把"这个任务是否已请求停止"交给 LLM 客户端——它在**每次尝试前**与
            # **切备用前**复查，取消后不再重试、不再换端点（此前只有派发边界看得见取消）
            set_cancel_guard(self._cancel_requested)
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
        # V2-2 / M0-b：评审状态按**根任务**归属（个人模式评审未完成时如实标注，
        # 交付物注明需人工复核）。任务开始时清掉本任务这一条，不动别的任务。
        self._review_state(task_id).update({
            "verdict": "", "degraded_reason": "", "plan_fingerprint": "",
            "plan_version": 0,
            "policy_version": REVIEW_POLICY_VERSION, "rounds": 0, "at": 0.0,
        })
        # R1：计划版本号从 v1 起；恢复路径会把 checkpoint 里的版本号带回来
        self._bump_plan_version(task_id, "任务起始规划")
        # M0-d：本次运行的准入结论（收尾时计算并落库/注入模板与经验沉淀）
        # R1：交付状态按**根任务**归属（此前是实例标量：另一个任务写草稿会把本任务
        # 已判定的 verified 翻成 false，反之亦然）
        self._delivery(task_id).update({"status": "", "reason": "", "hard_fail": ""})
        # M0-e：根任务预算耗尽时，本任务不再尝试新调用（见 _dispatch 的拒绝分支）
        if not hasattr(self, "_budget_exhausted"):
            self._budget_exhausted = {}
        self._budget_exhausted.pop(task_id, None)
        if not hasattr(self, "_task_admission"):
            self._task_admission = {}
        self._task_admission.pop(task_id, None)
        # V1.2 checkpointer：进程崩溃后从最后完成步骤续跑。
        # 仅当无进行中标记（或旧标记 pid 已死）且 goal 哈希/终态校验通过时恢复；
        # 恢复时跳过工作区清理，保护已有成果文件。
        resumed = None
        try:
            if not self._task_is_running(task_id):
                resumed = self._resume_from_checkpoint(task_id, goal, project)
        except Exception as exc:
            logger.warning(
                "Checkpoint resume check failed for %s: %s",
                task_id, str(exc)[:120],
            )
            resumed = None
        self._mark_task_running(task_id)
        # 新一轮任务开始前清掉可能残留的取消标志（同 id 复用/重跑时不被旧标志秒杀）
        self._clear_cancel(task_id)
        # 状态真源：进入执行写 RUNNING（提交时是 QUEUED），
        # 让历史/状态接口能区分"排队中"与"运行中"
        try:
            import task_state
            task_state.mark_running(task_id, phase="规划")
        except Exception:
            pass
        # 阶段看门狗：规划/评审/反思这类阻塞调用此前完全在进度模型之外，
        # 卡住时控制台没有任何信号（实测静默 6 分钟）。这里按统一阶段心跳判定，
        # 超阈值先告警（分级第一步；重试/终止仍由各阶段的既有逻辑负责）。
        _phase_stop = threading.Event()
        _phase_thread = self._start_phase_monitor(task_id, _phase_stop)
        if resumed is None:
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
        else:
            # 恢复语义（显式化，避免"为什么这次没清工作区"成为疑问）：
            # 从 checkpoint 恢复时**保留**已有产物——已完成的步骤结果就靠这些文件，
            # 清掉会让恢复变成"从零重跑"。清理只发生在全新任务（resumed is None）。
            logger.info(
                "Task %s resumed from checkpoint: phase=%s pending_steps=%d"
                "（保留工作区已有产物，不做清理）",
                task_id, resumed.get("phase"), len(resumed.get("steps") or []),
            )

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
            # M0-c：健康探测是网络等待，取消要能立刻打断（不等探测超时）
            _llm_ok, _llm_msg = self._call_llm_cancellable(
                task_id, "健康预检", endpoints_available)
            if not _llm_ok:
                push_progress(self._messaging, task_id, "warning",
                              {"type": "llm", "agent": "orchestrator",
                               "message": _llm_msg, "timestamp": self._now_iso()})
                push_progress(self._messaging, task_id, "task_complete",
                              {"status": "FAILED", "summary": _llm_msg})
                logger.error("Task %s aborted: %s", task_id, _llm_msg)
                self._clear_task_running(task_id)
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
            # 单端点不足照常运行并在 llm_degraded 预置余额警告。
            # M0-c：预检本身也是网络等待，同样要能被"停止"打断（实测：端点退化时
            # 健康/余额探测会各占数十秒，取消要等它们跑完才生效 → 停止延迟 58s）
            try:
                _balance_ok, _balance_msg = self._call_llm_cancellable(
                    task_id, "余额预检", self._precheck_llm_balance, task_id)
            except TaskCancelled:
                _phase_stop.set()
                return self._finish_cancelled(task_id, goal, None)
            if not _balance_ok:
                self._clear_task_running(task_id)
                self._notify_done_async(task_id, goal, "FAILED", _balance_msg)
                return {"task_id": task_id, "status": "FAILED",
                        "steps": [], "report": _balance_msg, "reason": _balance_msg}
        except TaskCancelled:
            _phase_stop.set()
            return self._finish_cancelled(task_id, goal, None)
        except Exception:
            pass

        # 状态真源：阶段名落库（规划 → 执行 → 反思），供历史页展示当前阶段
        try:
            import task_state
            task_state.set_phase(task_id, "规划")
        except Exception:
            pass
        # 取消可能在提交后立刻到达（用户在规划期就点了停止）：规划前先看一眼，
        # 免得为一次已经不要的任务再花一次规划调用
        if self._cancel_requested(task_id):
            _phase_stop.set()
            return self._finish_cancelled(task_id, goal, None)
        # 结构化数据源预载：financial → 东方财富/SEC；crypto/macro/news → 对应适配器。
        # C 批把它提到**规划之前**：研究任务的主路径不得因为规划失败/超时就丢掉来源与
        # 事实（预载不发 LLM、不占预算票，取消检查已在上面做过）。
        preloaded = None
        if resumed is None:
            preloaded = self._structured_data_preload(task_id, goal, project)
        # 1. Plan（模板步骤直接采用，否则 LLM 规划）——恢复路径跳过规划
        used_template = False
        if resumed is None:
            if template_steps:
                steps = self._normalize_steps(template_steps)
                steps = self._ensure_report_step(steps, task_id)
                used_template = True
                # M0-b：模板计划同样受评审策略约束
                self._require_review_or_refuse(
                    task_id, "模板计划未经过 Critic 评审", plan=steps)
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
                    # M0-b：路由到模板同样是"没有 Critic 评审"的计划
                    self._require_review_or_refuse(
                        task_id, "路由模板计划未经过 Critic 评审", plan=steps)
                else:
                    try:
                        from llm_client import LLMUnavailableError
                        steps = self._plan(goal, task_id, context, memory_context)
                    except TaskCancelled:
                        # 规划期间用户点了停止：放弃在飞调用并立即收尾（不再等模型超时）
                        _phase_stop.set()
                        return self._finish_cancelled(task_id, goal, None)
                    except LLMUnavailableError as exc:
                        push_progress(self._messaging, task_id, "warning",
                                      {"type": "llm", "agent": "orchestrator",
                                       "message": str(exc), "timestamp": self._now_iso()})
                        # 正文模型不可用也要把已取到的事实落成可复核底稿（失败状态不变）
                        self._salvage_working_paper(task_id, goal, project)
                        push_progress(self._messaging, task_id, "task_complete",
                                      {"status": "FAILED", "summary": str(exc)})
                        logger.error("Task %s aborted: %s", task_id, exc)
                        self._clear_task_running(task_id)
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
                # 计划产不出来也要保住已取到的事实（失败状态不变）
                self._salvage_working_paper(task_id, goal, project)
                push_progress(self._messaging, task_id, "task_complete",
                              {"status": "FAILED", "summary": "Planning failed"})
                self._clear_task_running(task_id)
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
                _cf: dict = {}
                confirmed = self._wait_plan_confirm(task_id, steps, _cf)
                if confirmed is None:
                    if _cf.get("kind") == WAIT_CANCEL:
                        # 用户在计划确认期间取消 → 终态是 CANCELLED（不是 FAILED）
                        _phase_stop.set()
                        return self._finish_cancelled(task_id, goal, None)
                    push_progress(self._messaging, task_id, "task_complete",
                                  {"status": "FAILED", "summary": "Plan not confirmed, task cancelled"})
                    self._clear_task_running(task_id)
                    self._notify_done_async(
                        task_id, goal, "FAILED", "Plan not confirmed, task cancelled",
                    )
                    return {"task_id": task_id, "status": "FAILED", "steps": [],
                            "report": "Plan not confirmed"}
                # R1：用户在确认阶段**改了计划**（不是原样确认）→ 这是评审看到的那一版
                # 之外的另一个版本，此前绑定的 PASS 不覆盖它；原样确认不升级版本号。
                if self._plan_fingerprint(confirmed) != self._plan_fingerprint(steps):
                    self._bump_plan_version(task_id, "计划确认阶段被编辑")
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
                    self._clear_task_running(task_id)
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

            # 结构化数据源预载已在规划前完成（见上方 preloaded），此处只按预载结果
            # 做步骤替换：data_analyzer → content_summary，让下游直接消费结构化数据
            steps = self._reduce_steps_for_structured(task_id, steps, preloaded)
        else:
            # 恢复路径：直接用 checkpoint 中的待执行步骤与历史标记
            steps = list(resumed.get("steps") or [])
            used_template = bool(resumed.get("used_template"))
            simple = bool(resumed.get("simple"))
            # M0-b：恢复的评审状态要么仍有效（同策略版本、同身份模式、同计划版本），
            # 要么按"未完成评审"处置——不复用过期/异版 PASS，也不默认已通过
            # R1：先把 checkpoint 里的计划版本号带回来，再判 PASS 是否覆盖该版本
            # （旧 checkpoint 无该字段 → 回到任务起始的 v1；此时若自带 PASS，
            # 其 plan_version 为 0，判定必然不复用——无法证明就不放行）
            self._task_plan_versions[task_id] = int(resumed.get("plan_version") or 1)
            self._restore_review_state(task_id, resumed.get("review") or {}, steps)
            with self._task_starts_lock:
                self._task_simple[task_id] = simple

        # 2..N. 执行 + 自主迭代（执行 → 验收评审 → 追加步骤，直到通过或达到上限）
        if resumed is not None:
            all_steps = list(resumed.get("all_steps") or [])
            completed_all = dict(resumed.get("completed_all") or {})
            has_failure = bool(resumed.get("has_failure"))
            iteration = int(resumed.get("iteration") or 0)
            redo_rounds = int(resumed.get("redo_rounds") or 0)
            skip_execute = bool(resumed.get("skip_execute"))
            gate_checked = bool(resumed.get("gate_checked"))
            best_report = str(resumed.get("best_report") or "")
            last_steps = []
            last_results: list[dict] = []
            # 恢复后把全量状态推给前端（web_ui 内存态在进程重启后已丢失）
            try:
                self._publish_full_state(task_id, goal, all_steps, completed_all)
            except Exception:
                pass
        else:
            all_steps: list[dict] = []
            completed_all: dict = {}
            has_failure = False
            iteration = 0
            redo_rounds = 0
            skip_execute = False
            gate_checked = False
            last_steps = steps
            last_results: list[dict] = []
            best_report = ""
            # 反思早期收敛：记录上一轮验收 gaps 签名，重做后 gaps 无变化
            # → 不再空转重做（实测两轮重做后缺口仍相同，浪费 6+ 分钟）
            last_gap_signature = ""
        # P0-1：反思 LLM 不可用标记（每次任务重置）
        self._reflection_llm_unavailable = ""

        while True:
            if resumed is not None and resumed.get("phase") == "finalizing":
                break
            # 每轮（执行→反思→重做）开始前检查取消：这是覆盖面最广的检查点
            if self._cancel_requested(task_id):
                _phase_stop.set()
                return self._finish_cancelled(task_id, goal, last_steps)
            if not skip_execute:
                iter_results, iter_failed = self._execute_steps(steps, task_id, goal)
                # 执行途中被取消：立刻收尾，不再进入反思/重做（否则会继续花额度）
                if self._cancel_requested(task_id):
                    _phase_stop.set()
                    return self._finish_cancelled(task_id, goal, last_steps or steps)
                has_failure = has_failure or iter_failed
                last_steps = steps
                last_results = iter_results
                for s, r in zip(steps, iter_results):
                    completed_all[s["step_id"]] = r
                    s.setdefault("iteration", iteration)
                all_steps.extend(steps)

                cand = self._best_deliverable(goal, last_steps, last_results)
                # V1/M0-a：按**两版各自的验收**比较，并原子采纳（唯一采纳点）
                _before_best = best_report
                best_report = self._adopt_candidate(
                    task_id, best_report, cand, iteration=iteration,
                    cancelled=self._cancel_requested(task_id))
                _cand_improved = best_report != _before_best
                self._publish_full_state(task_id, goal, all_steps, completed_all)
                # V1.2 checkpoint：每轮执行完成后保存（含全部步骤/结果/迭代轮次）
                self._save_checkpoint(task_id, self._checkpoint_payload(
                    task_id, goal, project, all_steps, completed_all,
                    current_steps=steps,
                    pending_steps=[
                        s for s in steps
                        if completed_all.get(s["step_id"], {}).get("status") != "SUCCESS"
                    ],
                    phase="executing", iteration=iteration,
                    has_failure=has_failure, redo_rounds=redo_rounds,
                    best_report=best_report, gate_checked=gate_checked,
                    simple=simple, used_template=used_template,
                ))
                # 反思收敛优化：反思产出的本轮执行后 best_report 与上一轮相同
                # 或更短 → 提前终止循环，避免无意义多轮；验收 fail 时除外
                # （验收为准，仍强制重做，长度不能作为放行依据）。
                if (
                    best_report
                    and iteration > 0
                    and not _cand_improved
                ):
                    _acc_now = self._read_acceptance_summary(task_id)
                    if not (_acc_now and _acc_now.get("overall") != "pass"):
                        logger.info(
                            "Reflection convergence: best_report 未改善"
                            "（上一轮 %d -> 本轮 %d 字符），提前终止反思（task=%s）",
                            len(best_report), len(cand), task_id,
                        )
                        push_progress(self._messaging, task_id, "log",
                                      {"type": "iteration", "agent": "orchestrator",
                                       "message": (
                                           "Reflection: best_report 未改善"
                                           f"（{len(best_report)} -> {len(cand)} 字符），"
                                           "提前终止反思"
                                       ),
                                       "timestamp": self._now_iso()})
                        break
            skip_execute = False

            if has_failure or self._max_iterations <= 0 or iteration >= self._max_iterations:
                break
            # M0-e：预算已耗尽 → 不再进入新一轮（新一轮的每次派发都会被拒绝，
            # 只会把"每 2 秒重试一次"的循环拖到天荒地老）
            if (getattr(self, "_budget_exhausted", {}) or {}).get(task_id):
                logger.warning("根任务预算已耗尽，停止后续迭代（task=%s）", task_id)
                has_failure = True
                push_progress(self._messaging, task_id, "log",
                              {"type": "error", "agent": "orchestrator",
                               "message": ("根任务预算已耗尽：不再进入新的迭代，"
                                           "按现有结果如实收尾"),
                               "timestamp": self._now_iso()})
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
            # B1：反思预算按链路分级——预算耗尽直接收口，缺口随最终结果
            # 如实披露为 SUCCESS_WITH_ISSUES（避免排行类任务无节制重做）
            from acceptance_checker import traceability_domain
            _gl = str(goal or "").lower()
            _dom = (
                "ranking"
                if any(k in _gl for k in ("排行", "排名", "top10", "前十"))
                else (traceability_domain(goal) or "general")
            )
            _rb = getattr(self, "_reflection_budget", None) or {
                "ranking": 2, "financial": 3, "general": 3,
            }
            _budget = int(_rb.get(_dom, _rb.get("general", 3)) or 3)
            if iteration >= _budget:
                logger.warning(
                    "Reflection budget exhausted (%s, budget=%d, iteration=%d, "
                    "task=%s)，收口不再重做",
                    _dom, _budget, iteration, task_id,
                )
                push_progress(self._messaging, task_id, "log",
                              {"type": "iteration", "agent": "orchestrator",
                               "message": (
                                   f"反思预算耗尽（{_dom} 链路，预算 {_budget} 轮），"
                                   "按当前最优报告收口"
                               ),
                               "timestamp": self._now_iso()})
                break
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
                _gap_text += _SOURCE_DISCIPLINE_REDLINES
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
            # 反思早期收敛：验收 gaps 与上一轮完全相同 → 重做无改善，
            # 继续迭代只会重复耗时（实测两轮重做后缺口不变仍继续）。
            # iteration 每次 while 循环末尾递增，iteration>0 表示已过首轮。
            _cur_sig = "|".join(
                str(g).strip() for g in ((_acc_summary or {}).get("gaps") or [])
                if str(g).strip()
            )
            if (
                iteration > 0
                and _cur_sig
                and _cur_sig == last_gap_signature
            ):
                logger.warning(
                    "Reflection early-stop: 验收 gaps 与上一轮相同，重做无改善"
                    "（task=%s），提前终止反思",
                    task_id,
                )
                push_progress(self._messaging, task_id, "log",
                              {"type": "iteration", "agent": "orchestrator",
                               "message": "验收缺口与上一轮相同，重做无改善，提前终止反思",
                               "timestamp": self._now_iso()})
                break
            last_gap_signature = _cur_sig
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
                        _gap_text += _SOURCE_DISCIPLINE_REDLINES
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
                        # V1/M0-a：重做后同样走唯一采纳点（各自验收 + 原子切换；取消/终态不覆盖）
                        _before = best_report
                        best_report = self._adopt_candidate(
                            task_id, best_report, cand, iteration=iteration,
                            cancelled=self._cancel_requested(task_id))
                        _cand_improved = best_report != _before
                        _why = "采纳候选稿" if _cand_improved else "保留当前稿（各自验收比较）"
                        logger.info("重做后版本比较：%s", _why)
                        self._publish_full_state(task_id, goal, all_steps, completed_all)
                        # V1.2 checkpoint：反思单步重做后保存（继续反思轮）
                        self._save_checkpoint(task_id, self._checkpoint_payload(
                            task_id, goal, project, all_steps, completed_all,
                            current_steps=[], pending_steps=[], phase="reflecting",
                            iteration=iteration, has_failure=has_failure,
                            redo_rounds=redo_rounds, best_report=best_report,
                            gate_checked=gate_checked, simple=simple,
                            used_template=used_template,
                        ))
                        # 反思收敛优化：单步重做后 best_report 无改善（相同或
                        # 更短）→ 提前终止反思；验收 fail 时仍继续重做。
                        if best_report and not _cand_improved and not _acc_fail:
                            logger.info(
                                "Reflection convergence: 重做后 best_report 未改善"
                                "（上一轮 %d -> 本轮 %d 字符），提前终止反思（task=%s）",
                                len(best_report), len(cand), task_id,
                            )
                            push_progress(self._messaging, task_id, "log",
                                          {"type": "iteration", "agent": "orchestrator",
                                           "message": (
                                               "Reflection: 重做后 best_report 未改善"
                                               f"（{len(best_report)} -> {len(cand)} 字符），"
                                               "提前终止反思"
                                           ),
                                           "timestamp": self._now_iso()})
                            break
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
            # R1：反思追加/替换步骤 = **另一版计划**，此前绑定的评审 PASS 不覆盖它
            self._bump_plan_version(task_id, "反思追加/替换步骤")
            steps = self._wire_report_deps(steps)
            steps = self._wire_search_fetch_deps(steps)
            steps = self._ensure_package_step(steps)
            steps = self._break_cycles(steps)
            steps = self._inject_goal_into_steps(steps, goal)
            steps = self._inject_skills(steps, goal)
            # V1.2 checkpoint：反思产出下一轮步骤后保存（崩溃后从新轮开始）
            self._save_checkpoint(task_id, self._checkpoint_payload(
                task_id, goal, project, all_steps, completed_all,
                current_steps=steps, pending_steps=steps, phase="reflecting",
                iteration=iteration, has_failure=has_failure,
                redo_rounds=redo_rounds, best_report=best_report,
                gate_checked=gate_checked, simple=simple,
                used_template=used_template,
            ))
            push_progress(self._messaging, task_id, "log",
                          {"type": "iteration", "agent": "orchestrator",
                           "message": f"Iteration {iteration}: closing {len(gaps)} gaps with {len(steps)} steps",
                           "timestamp": self._now_iso()})
            push_progress(self._messaging, task_id, "plan_update", {"steps": steps})

        # V1.2 checkpoint：收尾前保存 finalizing 阶段（崩溃后直接跳到交付）
        self._save_checkpoint(task_id, self._checkpoint_payload(
            task_id, goal, project, all_steps, completed_all,
            current_steps=[], pending_steps=[], phase="finalizing",
            iteration=iteration, has_failure=has_failure,
            redo_rounds=redo_rounds, best_report=best_report,
            gate_checked=gate_checked, simple=simple,
            used_template=used_template,
        ))
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
        # M0-a：快速路径/单步任务不走反思循环，也就不会经过"唯一采纳点"——
        # 收尾时补齐选中版本，否则这类任务永远没有选中版本，交付恒被判"未验收草稿"
        # （实机：验收 pass 却按草稿交付、经验也被准入拒绝）。
        try:
            _store0 = self._version_store(task_id)
            if _store0.adopted() is None and str(detail or "").strip():
                _rules_v, _rules_fp = self._rules_identity(task_id)
                _store0.record(
                    detail, sources_fingerprint=self._sources_fingerprint(task_id, detail),
                    rules_version=_rules_v, rules_fingerprint=_rules_fp,
                    policy_version=REVIEW_POLICY_VERSION)
                _v0 = _store0.find_by_body(detail)
                if _v0 is not None:
                    _store0.adopt(_v0, reason="收尾补齐：快速路径/单步任务未经采纳")
        except Exception as exc:
            logger.warning("收尾补齐选中版本失败（task=%s）：%s", task_id, str(exc)[:120])
        # 最终装配正文自己也要有验收：上面的验收绑的是**报告步骤的中间正文**，
        # 而交付采纳的是收尾装配后的 `detail`——两者字节不同，于是每次真实运行
        # 都以"该版本没有对应它自身的验收（未知）"交付，用户看不到本该显示的
        # "验收未通过 + 缺口"。这里按**同一正文**补一次确定性验收（幂等：已有则跳过）；
        # 验收器若把正文修成诚实披露版，就**以那一版交付**（交付与验收对象同一份字节）。
        try:
            _st, _final = self._ensure_final_body_accepted(task_id, goal, detail)
            if _final and _final != detail:
                # helper 已把修复版采纳为交付版本；这里只切换交付正文
                detail = _final
                logger.info("交付正文采用验收器修正版（task=%s，%s）", task_id, _st)
        except Exception as exc:
            logger.warning("最终装配验收失败（task=%s）：%s", task_id, str(exc)[:120])
        # S1 + B：底稿与交付装配走**唯一实现**（与人工修订同一条路径，
        # 交付字节与记录的 delivered_sha256 才对得上；此前两边各写一套必然分叉）。
        _budget_stop = str((getattr(self, "_budget_exhausted", {}) or {}).get(task_id) or "")
        if _budget_stop:
            has_failure = True
            delivery = (f"> **本次运行因根任务预算耗尽而提前收尾：{_budget_stop}**\n"
                        "> 结果可能不完整，需人工确认后再使用。\n\n" + delivery)
        try:
            from delivery_pipeline import assemble_and_verify, write_wrapper
            from report_version import DELIVERY_DRAFT, DELIVERY_VERIFIED
            from working_paper_export import write_working_paper
            _wp = write_working_paper(task_id, goal, project=project)
            self._working_paper = _wp
            # C 批：把脱敏的 LLM 调用形状落到工作区，供离线取证（无正文/密钥）
            self._dump_llm_calls(task_id)
            # 评审结论是**本次运行**的内存态：连同"属于哪一版"一起交给装配器落盘
            _rv = self._review_state(task_id)
            _review_facts = {
                "verdict": str(_rv.get("verdict") or "NONE"),
                "label": str(_rv.get("label") or ""),
                "degraded_reason": str(_rv.get("degraded_reason") or ""),
                "required": bool(self._review_is_required()),
            }
            _asm = assemble_and_verify(
                task_id, goal, detail, wrapper=delivery, project=project,
                paper=_wp, accept_fn=self._accept_fn_for(task_id, goal),
                review_facts=_review_facts)
            report = _asm["report"]
            write_wrapper(task_id, delivery)
            self._delivery(task_id)["hard_fail"] = str(_asm.get("hard_fail") or "")
            _status, _why = _asm["status"], _asm["reason"]
            self._delivery(task_id)["status"] = _status
            self._delivery(task_id)["reason"] = "" if _status == DELIVERY_VERIFIED else str(_why)
            if _status != DELIVERY_VERIFIED:
                logger.warning("交付状态=%s：%s", _status, _why)
                push_progress(self._messaging, task_id, "log",
                              {"type": "review", "agent": "orchestrator",
                               "message": f"交付状态：{'未验收草稿' if _status == DELIVERY_DRAFT else '证据未知'}"
                                          f"（{_why}）：不得视为已通过",
                               "timestamp": self._now_iso()})
        except Exception as exc:
            logger.warning("交付装配失败（按未验收草稿）：%s", str(exc)[:160])
            self._delivery(task_id).update({"reason": "装配异常", "status": "unknown"})
            try:
                from delivery_pipeline import with_draft_note
                report = with_draft_note(
                    delivery + "\n\n---\n\n" + detail, f"装配异常：{str(exc)[:120]}")
            except Exception:
                report = delivery + "\n\n---\n\n" + detail
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

        # P0-1/P0-2 + A1：以最终验收判定——验收 fail 无论反思是否执行/重做后仍 fail
        # 都如实降级为 SUCCESS_WITH_ISSUES；反思重做后验收 pass 则 SUCCESS；
        # LLM 双端点均失败同样降级。
        # R0.2：终态判定读**选中版本自身**的验收（而不是"最新那份 acceptance 文件"），
        # 否则"正文换成 sourceB、验收文件还是 sourceA 的 pass"会被当成功。
        acceptance_summary = self._acceptance_summary_for_status(task_id)
        llm_degraded = self._read_llm_degraded(task_id)
        overall = self._resolve_final_status(
            has_failure, acceptance_summary,
            getattr(self, "_reflection_llm_unavailable", ""), llm_degraded,
            draft_reason=str(self._delivery(task_id).get("reason") or ""),
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

        # V1.2 关键节点 HITL：报告终稿审批（report_confirm=True 时，在最终交付前
        # 等待用户确认）。只在此处（反思循环退出、报告最终确定后）触发一次；
        # 超时自动放行（与单步确认语义一致），用户取消则任务标记为 FAILED，
        # 并在报告中注明"用户取消终稿审批"。
        if report_confirm and overall != "FAILED":
            _rf: dict = {}
            approved = self._wait_report_confirm(task_id, goal, report, _rf)
            if not approved:
                if _rf.get("kind") == WAIT_CANCEL:
                    # 用户在终稿审批期间点了停止 → CANCELLED 终态，不写成"失败"
                    _phase_stop.set()
                    return self._finish_cancelled(task_id, goal, all_steps)
                overall = "FAILED"
                has_failure = True
                report = str(report or "") + "\n\n---\n\n> 用户取消终稿审批"
                push_progress(self._messaging, task_id, "log",
                              {"type": "error", "agent": "orchestrator",
                               "message": "终稿审批被用户取消，任务标记为失败",
                               "timestamp": self._now_iso()})

        # M0-d：准入结论（经验池/模板固化/自迭代都据此；无证据一律拒绝）
        admission = self._admission_decision(task_id, overall, acceptance_summary)
        self._record_consolidation_stat(task_id, goal, all_steps, admission=admission)

        # 4. Memory（P0 验收准入：验收 fail 只沉淀对话，不沉淀策略；
        #    M0-d：准入判据统一在 admission.admit_success，未知/取消/有缺口都不进经验池）
        if not has_failure:
            # M0-c：**已取消的任务不再产生新沉淀**——收尾期间到达的取消同样有效，
            # 否则"停止"之后还会往记忆/模板池里写入这次运行的经验
            if self._cancel_requested(task_id):
                logger.info("任务已取消，跳过记忆与模板沉淀（task=%s）", task_id)
            else:
                with self._memory_lock:
                    self._memory.consolidate_memory(
                        goal, all_steps, report,
                        acceptance_summary=acceptance_summary,
                        task_id=task_id,
                        admission=admission.as_dict(),
                    )
                if admission.admitted:
                    push_progress(self._messaging, task_id, "log",
                                  {"type": "memory", "agent": "orchestrator",
                                   "message": ("Strategy memory consolidated"
                                               + ("（已验证成功）" if admission.verified
                                                  else "（经验参考，未计入已验证成功）")),
                                   "timestamp": self._now_iso()})
                else:
                    logger.warning(
                        "Task %s 未通过经验准入，跳过策略沉淀：%s",
                        task_id, "; ".join(admission.reasons) or "未知原因")
                    push_progress(self._messaging, task_id, "log",
                                  {"type": "memory", "agent": "orchestrator",
                                   "message": "未通过经验准入（" + "；".join(admission.reasons)
                                              + "），仅记录对话，跳过策略沉淀",
                                   "timestamp": self._now_iso()})
                # 进化沉淀：复杂任务（未走模板）且**已验证成功**才提炼为确定性模板；
                # 未验证的经验（评审降级/缺证据）不得固化成"以后都这么做"
                if not used_template and admission.verified:
                    self._consolidate_template(goal, all_steps, task_id=task_id)
                elif not used_template:
                    logger.info("模板固化跳过（未达已验证成功）：%s", goal[:40])

        # 5. Complete
        # V1.2 checkpointer：最终交付前清理断点与进行中标记，
        # 避免已完成任务被启动扫描误恢复
        clear_checkpoint(task_id)
        self._clear_task_running(task_id)
        ok_count = sum(1 for r in completed_all.values() if r.get("status") == "SUCCESS")
        # T7：终态附带任务耗时（metrics 延迟统计的数据源对齐；run 入口已记 _task_starts）
        _start_ts = (getattr(self, "_task_starts", {}) or {}).get(task_id)
        _elapsed = round(time.time() - _start_ts, 1) if _start_ts else None
        push_progress(self._messaging, task_id, "task_complete",
                      {"status": overall,
                       "summary": f"{overall}: {ok_count}/{len(all_steps)} steps, {iteration} iterations",
                       "report": report,
                       "acceptance": acceptance_summary,
                       "llm_degraded": llm_degraded,
                       "elapsed_sec": _elapsed})
        # 状态写者收口：终态由编排器直接落库（不再依赖 webui 监听器转写；
        # webui 重启或消息丢失都不会让 DB 停在 RUNNING）
        try:
            _snap = {}
            try:
                import task_state as _ts
                _snap = _ts.read_snapshot(task_id) or {}
            except Exception:
                _snap = {}
            _steps_out = _snap.get("steps") or [
                {"step_id": s["step_id"], "capability": s["capability"],
                 "instruction": s["instruction"],
                 "result": completed_all.get(s["step_id"], {})}
                for s in all_steps
            ]
            self._finalize_task(
                task_id, goal, overall, report=report,
                steps=_steps_out, logs=_snap.get("logs") or [],
                acceptance=acceptance_summary or {},
            )
        except Exception as exc:
            logger.warning("finalize on main terminal failed: %s", str(exc)[:150])

        # 6. 提示词自迭代（后台线程，不阻塞交付）：LLM 分析本次输出与预期的差距，
        #    总结问题并产出改进版提示词写入注册表，下一轮任务自动生效
        def _refine_async() -> None:
            try:
                # M0-c：后台自迭代也要认取消——线程排在终态之后启，
                # 用户点了停止就不该再花一次模型调用去改提示词
                if self._cancel_requested(task_id):
                    logger.info("任务已取消，跳过后台提示词自迭代（task=%s）", task_id)
                    return None
                from prompt_refinery import maybe_refine
                maybe_refine(
                    self._messaging, task_id, goal, all_steps, completed_all, report,
                    {"has_failure": has_failure, "reflection_used": iteration > 0,
                     "iterations": iteration, "memory": getattr(self, "_memory", None),
                     # M0-d：自迭代产出是否可直接生效，取决于本次运行的准入结论
                     "admission": admission.as_dict()},
                )
            except Exception as exc:
                logger.warning("prompt refinery async failed: %s", str(exc)[:150])
            return None

        threading.Thread(target=_refine_async, daemon=True).start()

        # F5：任务完成外部通知（后台线程，不阻塞完成流程）
        self._notify_done_async(task_id, goal, overall, report)

        # 快速路径标志仅任务运行期间需要，用完即清，避免字典无限增长
        # （含 goals/projects/user_ids/prompt_hints/market_resolution/starts
        # 等全部按任务键存储的辅助字典，常驻进程不清会无界增长）
        self._task_simple.pop(task_id, None)
        self._task_sources.pop(task_id, None)
        for d in (
            "_task_goals", "_task_projects", "_task_user_ids",
            "_task_prompt_hints", "_task_market_resolution", "_task_starts",
            "_task_structured_data",
        ):
            try:
                getattr(self, d).pop(task_id, None)
            except AttributeError:
                pass
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

    def _confirm_wait(self, task_id: str, key: str, timeout: float) -> WaitOutcome:
        """带取消感知的人工确认等待（M0-c）。

        此前确认等待是一次到底的 brpop：用户点了停止，编排器仍要等确认超时
        （最长 180s）才收尾。现在按 1 秒分片轮询，取消立即返回 cancel。
        """
        deadline = time.time() + max(timeout, 1)
        while True:
            slice_end = min(deadline, time.time() + 1.0)
            msg = self._brpop_with_deadline(self._redis, key, slice_end)
            if msg:
                return WaitOutcome(WAIT_RESULT, result={"raw": msg[1]})
            if self._cancel_requested(task_id):
                return WaitOutcome(WAIT_CANCEL, reason="用户已取消：停止等待人工确认")
            if time.time() >= deadline:
                return WaitOutcome(WAIT_TIMEOUT, reason="人工确认超时")

    def _wait_plan_confirm(self, task_id: str, original_steps: list[dict],
                           outcome: dict | None = None) -> list[dict] | None:
        """等待用户确认/编辑计划；返回确认后的步骤，取消或超时返回 None。

        `outcome`（可选）回填本次等待的分类（`kind` = cancel/timeout/result），
        调用方据此区分"用户取消"与"没人确认"——两者后续处置不同。
        """
        try:
            wait = self._confirm_wait(
                task_id, f"plan_confirm:{task_id}", self._plan_confirm_timeout)
            if outcome is not None:
                outcome["kind"] = wait.kind
                outcome["reason"] = wait.reason
            if wait.kind == WAIT_CANCEL:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": "计划确认期间收到取消请求，停止等待",
                               "timestamp": self._now_iso()})
                return None
            if wait.kind != WAIT_RESULT:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": f"Plan confirm timeout ({self._plan_confirm_timeout}s), cancelling",
                               "timestamp": self._now_iso()})
                return None
            msg = wait.result["raw"]
            data = json.loads(msg if isinstance(msg, str) else msg.decode())
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
            # Redis 不可用/通道异常 → 按"没等到确认"记录（可观测），仍用原计划继续
            logger.warning("Plan confirm error for %s: %s", task_id, str(exc)[:120])
            if outcome is not None:
                outcome["kind"] = WAIT_PROTOCOL
                outcome["reason"] = f"确认通道异常：{str(exc)[:100]}"
            return original_steps

    def _wait_step_confirm(self, task_id: str, step: dict) -> bool:
        """人工确认单步（mode=human_in_loop）：确认放行，取消拒绝，超时自动放行。"""
        try:
            key = f"step_confirm:{task_id}:{step.get('step_id')}"
            push_progress(self._messaging, task_id, "log",
                          {"type": "step_confirm", "agent": step.get("capability", "?"),
                           "message": f"等待人工确认步骤 {step.get('step_id')}（{step.get('capability')}）",
                           "timestamp": self._now_iso()})
            wait = self._confirm_wait(task_id, key, self._plan_confirm_timeout)
            if wait.kind == WAIT_CANCEL:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": f"步骤 {step.get('step_id')} 等待确认期间被取消",
                               "timestamp": self._now_iso()})
                return False
            if wait.kind != WAIT_RESULT:
                push_progress(self._messaging, task_id, "log",
                              {"type": "info", "agent": "orchestrator",
                               "message": f"步骤 {step.get('step_id')} 确认超时，自动继续",
                               "timestamp": self._now_iso()})
                return True
            msg = wait.result["raw"]
            data = json.loads(msg if isinstance(msg, str) else msg.decode())
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

    def _wait_report_confirm(self, task_id: str, goal: str, report: str,
                             outcome: dict | None = None) -> bool:
        """报告终稿审批（关键节点 HITL）：确认放行，取消拒绝，超时自动放行。

        复用 plan_confirm 通道：发布 AWAITING_CONFIRM 状态（stage='final_report'
        并附报告前 3000 字符预览），等待 plan_confirm:{task_id} 上的确认/取消；
        超时（_plan_confirm_timeout 封顶 180s）自动放行返回 True。
        `outcome`（可选）回填等待分类，供调用方区分"用户取消"与"没人审批"。
        """
        preview = str(report or "")[:3000]
        # 状态消息：直接发布到 orchestrator:response，web_ui 会合并进
        # _task_results，前端 useTaskPoller 据此弹出 AWAITING_CONFIRM 确认框
        try:
            self._messaging.publish("orchestrator:response", {
                "task_id": task_id,
                "status": "AWAITING_CONFIRM",
                "revision": False,
                "stage": "final_report",
                "report_preview": preview,
                "goal": goal,
            })
        except Exception as exc:
            logger.warning(
                "Final report confirm publish failed for %s: %s",
                task_id, str(exc)[:120],
            )
            return True
        # 进度消息：push_progress 类型 'plan'（前端已处理）；不带 steps 键，
        # 避免覆盖 web_ui 已合并的计划树
        push_progress(self._messaging, task_id, "plan",
                      {"status": "AWAITING_CONFIRM", "stage": "final_report",
                       "report_preview": preview,
                       "message": "报告终稿已生成，等待用户审批"})
        push_progress(self._messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator",
                       "message": "报告终稿已生成，等待用户审批（超时自动放行）",
                       "timestamp": self._now_iso()})
        timeout = min(self._plan_confirm_timeout, 600)
        try:
            wait = self._confirm_wait(task_id, f"plan_confirm:{task_id}", timeout)
        except Exception as exc:
            logger.warning(
                "Final report confirm wait failed for %s: %s",
                task_id, str(exc)[:120],
            )
            return True
        if outcome is not None:
            outcome["kind"] = wait.kind
            outcome["reason"] = wait.reason
        if wait.kind == WAIT_CANCEL:
            push_progress(self._messaging, task_id, "log",
                          {"type": "info", "agent": "orchestrator",
                           "message": "终稿审批期间收到取消请求，停止等待",
                           "timestamp": self._now_iso()})
            return False
        if wait.kind != WAIT_RESULT:
            push_progress(self._messaging, task_id, "log",
                          {"type": "info", "agent": "orchestrator",
                           "message": f"终稿审批 {timeout}s 超时，自动放行",
                           "timestamp": self._now_iso()})
            return True
        msg = wait.result["raw"]
        try:
            data = json.loads(
                msg if isinstance(msg, str) else msg.decode()
            )
        except Exception:
            return True
        if data.get("action") == "cancel":
            push_progress(self._messaging, task_id, "log",
                          {"type": "info", "agent": "orchestrator",
                           "message": "终稿审批被用户取消",
                           "timestamp": self._now_iso()})
            return False
        push_progress(self._messaging, task_id, "log",
                      {"type": "plan", "agent": "orchestrator",
                       "message": "终稿审批通过，交付",
                       "timestamp": self._now_iso()})
        return True

    def _execute_steps(self, steps: list[dict], task_id: str, goal: str) -> tuple[list[dict], bool]:
        """并行 DAG 执行一轮步骤，返回（按步骤顺序的结果列表, 是否有失败）。"""
        # 惰性初始化（兼容直接 __new__ 构造的实例/测试）
        if not hasattr(self, "_task_sources"):
            self._task_sources = {}
            self._task_sources_lock = threading.Lock()
        # 状态真源：阶段名落库（进入步骤执行）
        try:
            import task_state
            task_state.set_phase(task_id, "执行")
        except Exception:
            pass
        completed: dict = {}
        has_failure = False
        state = {"replan_used": 0}
        step_ids = {s.get("step_id") for s in steps}
        lock = threading.Lock()

        def deps_ok(step):
            return all(d in completed for d in step.get("depends_on", []))

        def deps_failed(step):
            # `optional` 依赖失败不阻塞下游：定向取证的第二个来源取不到是**正常缺口**
            # （缺资料列待核查即可），不该让分析/报告/打包步骤一起失败
            optional_ids = {s.get("step_id") for s in steps if s.get("optional")}
            return [d for d in step.get("depends_on", [])
                    if d in completed and completed[d].get("status") == "FAILED"
                    and d not in optional_ids]

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
                    # 显式编号来源清单：模型在正文/表格里直接引用 [1]/[2]，
                    # 与 '## 参考来源' 清单编号一致——避免模型自造来源名
                    base_instr += (
                        "\n\n[数据来源]\n"
                        "以下为本次任务检索到的来源清单，编号即引用编号：\n"
                        + "\n".join(
                            f"[{i+1}] {u}" for i, u in enumerate(urls[:12])
                        )
                        + "\n正文/表格引用时使用上述编号；'## 参考来源' 清单"
                        "按上述编号与顺序原样列出（可追加额外条目，但编号必须"
                        "与引用一致）。"
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
                # F2′-2：目标是**年报 PDF** 时走解析通道（抓取 worker 只会把它按
                # UTF-8 解码成乱码）；拿到带页码的正文后并入快照，证据里就能写页码
                self._try_pdf_evidence(task_id, step, result)
                # F2：抓到的年报/公告正文切成带定位的叙事证据，供报告步骤解释变化
                try:
                    import narrative_evidence as _ne
                    try:
                        _parsed = json.loads(str(result.get("result") or ""))
                    except Exception:
                        _parsed = None
                    _ne.build(task_id, goal=goal,
                              extra_docs=[_parsed] if isinstance(_parsed, dict) else None)
                except Exception as exc:         # noqa: BLE001 - 证据提取不拖垮主线
                    logger.warning("叙事证据构建失败（task=%s）：%s", task_id, str(exc)[:140])
            elif step.get("capability") == "web_fetch" and result.get("status") != "SUCCESS":
                # 抓取失败也可能是"这份材料是 PDF"：照样试一次解析通道，
                # 拿不到就按缺口处理（不编内容）
                self._try_pdf_evidence(task_id, step, result)
            if step.get("capability") == "report_generator" and result.get("status") == "SUCCESS":
                # 确定性验收器：数字溯源等 checklist → 缺口报告（供反思/前端/人工）
                self._run_acceptance_check(task_id, goal, trigger="报告步骤")
            elif (
                step.get("capability") == "content_summary"
                and result.get("status") == "SUCCESS"
                and not any(str(x.get("capability") or "") == "report_generator"
                            for x in steps)
                and steps and step is steps[-1]
            ):
                # 计划里没有独立报告步骤：最后一步的输出就是交付正文，
                # 同样要过验收（实机：研究类任务被规划成单个 content_summary，
                # 于是从来没有验收报告 → 交付恒为"未验收草稿"）
                self._run_acceptance_check(
                    task_id, goal, trigger="内容摘要步骤",
                    report_body=str(result.get("result") or ""))
            if step.get("capability") == "web_search" and result.get("status") == "SUCCESS":
                # 搜索结果 URL 累计到任务级，供后续（含反射轮）报告步骤引用来源
                try:
                    parsed = json.loads(str(result.get("result") or ""))
                except Exception:
                    parsed = None
                if isinstance(parsed, list):
                    # 2c 死链治理：累计引用/落盘前剔除明确 dead 的链接，
                    # 减少报告"来源链接失效"（探测异常静默放行，不伤任务主线）
                    parsed, _dropped = filter_dead_search_results(parsed)
                    if _dropped:
                        logger.info(
                            "web_search dead-link filter for %s: dropped %d/%d results",
                            task_id, _dropped, _dropped + len(parsed),
                        )
                        # 过滤结果回写 step 结果：下游 _inject_step_context
                        # 会把 result 原文喂给 LLM，不回写则正文引用仍是
                        # 含死链的旧列表（报告来源与落盘清单两套 URL）
                        result["result"] = json.dumps(
                            parsed, ensure_ascii=False,
                        )
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
                            # 研究任务：**不出检索统计图**（词频/域名分布回答的是"搜了多少"，
                            # 不是"公司怎么样"）；财务图由底稿确定性生成（见 content_summary
                            # 分支）。顺带堵住越界数据进图：那类图的来源是检索清洗结果，
                            # 会把契约期间之外的数字（如 2025 半年报）画进交付。
                            if not self._is_research_task(task_id):
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
                # 研究任务：图由**底稿确定性生成**（两期核心指标对比 / 同比增速 /
                # 盈利与现金流质量），不走 LLM 规格与"市场规模"兜底——实机里那条路
                # 把 16 个会计科目塞进一张同轴图，读者拿不到任何结论。
                _fin_specs = self._financial_chart_specs(task_id, goal)
                if _fin_specs:
                    chart_specs = _fin_specs
                    logger.info("chart pipeline %s: financial_research_specs=%d",
                                task_id, len(chart_specs))
                else:
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
                    # P1-3：LLM 未产出图表规格时，数据驱动兜底（≥2 个可作图点即渲染）。
                    # 研究任务不走这条：它的图必须来自底稿（否则又会画出"市场规模"那类
                    # 同轴混装图）；底稿没有可用事实时宁可不出图。
                    if not self._is_research_task(task_id):
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
                counted_in_flight = False
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
                                # 串行判定与"占用名额"必须在**同一次持锁**里完成：
                                # 否则两个 worker 都可能读到 in_flight==0（各自持锁），
                                # 各自取走一个步骤，pipeline 模式的串行约束就失效了
                                # （实测并发 2）。名额在这里占，下面按标记跳过重复 +1。
                                if serial:
                                    in_flight += 1
                                    counted_in_flight = True
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
                # 取消：把刚取出的步骤放回、退出调度，不再派发新步骤
                # （已在飞的步骤让它自然结束，强杀会留下半成品产物）
                if self._cancel_requested(task_id):
                    with lock:
                        pending[k] = step
                    return
                # 预算耗尽：同理停止派发（每步都会被拒，继续循环只是空转）
                if (getattr(self, "_budget_exhausted", {}) or {}).get(task_id):
                    with lock:
                        pending[k] = step
                    push_progress(self._messaging, task_id, "log",
                                  {"type": "error", "agent": "orchestrator",
                                   "message": ("根任务预算已耗尽：停止派发剩余步骤，"
                                               "按现有结果收尾"),
                                   "timestamp": self._now_iso()})
                    return
                with lock:
                    if not counted_in_flight:
                        in_flight += 1
                try:
                    result = execute_step(step)
                except Exception as exc:
                    logger.error("Step %s crashed: %s", k, str(exc)[:200])
                    result = {"task_id": k, "status": "FAILED", "result": f"Step crashed: {exc}"}
                finally:
                    with lock:
                        in_flight -= 1
                # code_execution 降级必须在结果落盘时完成：若等全部线程 join
                # 后再转换，依赖检查早已把下游步骤标记 Blocked（降级为 SUCCESS
                # 也救不回 report/package 被连锁阻塞）
                if (
                    step.get("capability") == "code_execution"
                    and result.get("status") == "FAILED"
                    and "No valid code" in str(result.get("result") or "")
                ):
                    result["status"] = "SUCCESS"
                    result["degraded_codegen"] = True
                    result["result"] = (
                        "（代码执行降级）代码生成-校验-修复循环未产出可运行代码，"
                        "本步骤已跳过；相关数值请以结构化数据与其他步骤产物为准。"
                    )
                    logger.warning(
                        "code_execution degraded (task=%s, step=%s)，跳过而非任务失败",
                        task_id, k,
                    )
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
                            _snapshot = None
                            with lock:
                                if not revision_done:
                                    revision_done = True
                                    # pending 是 {step_id: step} 字典，浅拷贝即可
                                    # （_build_search_revision 只读）
                                    _snapshot = dict(pending)
                            # LLM 复盘在锁外执行：_build_search_revision 内含
                            # 同步 LLM 调用（超时+重试可分钟级），持 DAG 锁会
                            # 把其余步骤写入与 stall 看门狗全部冻结
                            if _snapshot is not None:
                                revision = self._build_search_revision(_snapshot, goal)
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

        # L01：contextvars 不跨线程。步骤线程里也会发起 LLM 调用（滚动摘要、上下文注入），
        # 不绑定的话这些调用既无任务归属、也不进预算台账；这里把根任务身份显式带进线程。
        from llm_client import clear_task_context, set_task_context

        def _bind_task_ctx(fn, tid):
            def _bound():
                set_task_context(tid)
                try:
                    fn()
                finally:
                    clear_task_context()
            return _bound

        threads = [threading.Thread(target=_bind_task_ctx(worker, task_id), daemon=True)
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
        # 兜底：执行期降级（上方 worker 内转换）已覆盖并行路径；
        # 这里仅防御串行/异常路径漏网的 "No valid code" 失败
        for _s, _r in zip(steps, results):
            if (
                _s.get("capability") == "code_execution"
                and _r.get("status") == "FAILED"
                and "No valid code" in str(_r.get("result") or "")
            ):
                _r["status"] = "SUCCESS"
                _r["degraded_codegen"] = True
                _r["result"] = (
                    "（代码执行降级）代码生成-校验-修复循环未产出可运行代码，"
                    "本步骤已跳过；相关数值请以结构化数据和其他步骤产物为准。"
                )
                logger.warning(
                    "code_execution degraded post-join (task=%s, step=%s)",
                    task_id, _s.get("step_id"),
                )
        # 以最终 results 为准（含 code_execution 降级后的状态），
        # 不再沿用循环内的即时失败标记——降级为 SUCCESS 的步骤不得拖垮任务
        has_failure = any(r.get("status") != "SUCCESS" for r in results)
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
            """上一步结果可能来自外部网页，进指令前做注入检测（对标 C4-4.4）。

            F1 起改为**逐行隔离**：命中签名的行替换成隔离标记，其余原文保留——整段替换
            会把没问题的分析一起吃掉（实机：含 `` `CNY` `` 的正常研报段落被整段替换成
            过滤标记，随后又被报告 worker 补进正文）。结构化 JSON 是数据而非指令，
            整体豁免（既有设计）。
            """
            return _isolate_injection_lines(
                task_id, str(text or ""), stage="inject_step_context")

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
            # F2：研究路径的两个抓取步骤按角色分流（年报正文页 / 附注风险页）。
            # 角色只在固定研究计划的 step_id 上生效，模板任务的抓取行为不变。
            fetch_role = _FETCH_ROLE_BY_STEP.get(str(step.get("step_id") or ""), "") \
                if finance_fetch else ""
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
                            role=fetch_role,
                            exclude=self._fetched_urls(task_id),
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
                # 角色步骤**不追加兜底 URL**：没有合适候选时让 worker 明确失败（不联网），
                # 否则会退化成"抓第一页"，第二个抓取步骤就和第一个抓重了
                if not fetch_role:
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
                # 结构优先 + 逐行隔离（同报告分支）：file_io 的产物也可能是 JSON
                snippet = self._step_context_snippet(task_id, dep_id, str(prev_res or ""),
                                                     max_chars=12000)
                if snippet:
                    instr += f"\n[上一步结果 {dep_id}]:\n{_filter_role(snippet)}"

        if cap == 'code_execution':
            # 数据清洗提示：走统一的相关性判定（与 worker 侧同一规则）——只在
            # 本任务确实需要行情数据、或本步要作图且文件含相应键时注入。
            # 此前只要文件存在就注入（并罗列 market_data 等键），无关任务
            # （如内置销售数据的脚本）也会被告知"这是一份市场/图表数据"，
            # 实测把交付代码带偏成金融绘图。
            try:
                from workspace import task_project_dir as _tpd
                from task_intent import is_relevant
                _cd = _tpd(task_id) / "clean_chart_data.json"
                if _cd.exists():
                    _goal = str(
                        (getattr(self, "_task_goals", {}) or {}).get(task_id, "")
                        or ""
                    )
                    try:
                        _head = _cd.read_text(encoding="utf-8", errors="replace")[:600]
                    except Exception:
                        _head = ""
                    if is_relevant(_goal or instr, _cd.name, _head):
                        try:
                            _keys = sorted(
                                json.loads(_cd.read_text(encoding="utf-8")).keys()
                            )
                        except Exception:
                            _keys = []
                        if _keys:
                            instr += (
                                "\n[数据] 工作区已提供清洗后的结构化图表数据"
                                f" clean_chart_data.json（含 {' / '.join(_keys)}）。"
                                "如任务需要作图，请优先读取该文件并按其中的 "
                                "Label-Value 结构绘图，不要直接解析原始文本。"
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
            except Exception:                pass
            # 结构化财务数据【内容】注入（不只文件提及）：让报告/总结 LLM 真正看到
            # 权威年报序列，否则模型只会用搜索片段（如 IT之家）并宣称历史年份缺失；
            # 新数据源（crypto/macro/news）经 structured_data.json 走同一通道。
            try:
                block = self._structured_injection(task_id)
                if block:
                    instr += block
                # C 批：把**已选定的事实**（含同比与缺口）给到模型——研究路径下模型
                # 只解释这组事实，不自行从原始表里挑数或换算
                selected = self._selected_facts_block(
                    task_id, str((getattr(self, "_task_goals", {}) or {}).get(task_id, "")))
                if selected:
                    instr += selected
                    # E 批：研究类报告的内容骨架（关键数据表 + 同比解读 + 盈利/现金流
                    # 质量 + 结构杠杆 + 风险结论）。只在**有已选事实块**时注入：通用
                    # 任务没有这块，硬套会要求模型写它拿不到的数字。
                    instr += _REPORT_ANALYSIS_REQUIREMENTS
                # F2：已抓取的年报/公告正文证据（带小节定位）——解释变化只能引它。
                # 只对**落库研究契约**的任务注入：通用报告的素材不是年报附注，
                # 硬塞"未取得财务附注"这类缺口只会干扰它。
                if self._is_research_task(task_id):
                    evidence = self._narrative_evidence_block(
                        task_id, str((getattr(self, "_task_goals", {}) or {}).get(task_id, "")))
                    if evidence:
                        instr += evidence
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
                # 结构优先（F1）：**先判内容类型再限长**。此前"先截 2500 字再交给 _safe"，
                # 而 _safe 的 JSON 豁免要求整串可解析——长 JSON 被切坏后既失去豁免、
                # 又只剩半截，最终整段被替换成过滤标记并混进报告正文。
                snippet = self._step_context_snippet(task_id, dep_id, str(prev_res or ""))
                if snippet:
                    instr += f"\n[上一步结果 {dep_id}]:\n{_filter_role(snippet)}"
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
        if result.get("budget_exceeded"):
            # 预算已耗尽：重试/重规划都只会被同样拒绝，直接返回如实失败
            return result
        attempt = 0
        # 输出契约校验（对标 3.1 引导-校验-重试）：契约不通过视为失败，
        # 并把校验错误喂回指令重试，而不是盲目重发
        # M0-c：取消导致的空/短结果不是"输出不合契约"，不能被改写成步骤失败
        issue = ("" if str(result.get("status")) == "CANCELLED"
                 else self._contract_issue(goal, step, result))
        tried: list[str] = []
        while (result.get("status") == "FAILED" or issue) and attempt < self._max_retry:
            # 取消：不再重试/重规划（失败重试循环是实测最爱烧额度的地方）
            if self._cancel_requested(task_id):
                break
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
        if (result.get("status") == "FAILED"
                and state["replan_used"] < self._replan_depth
                and not self._cancel_requested(task_id)):
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
        # P1-4：发生过失败（重试或重规划）就落盘结构化诊断。
        # 搜索相关性失败（_contract_issue 判"结果与目标无关"）即使无重试/换步
        # 也要落盘——否则反思只看到"报告溯源率低"却不知根因在搜索质量（#6 传导）。
        if attempt > 0 or alt is not None or (
            issue and step.get("capability") == "web_search"
            and ("相关" in issue or "无关" in issue or "未命中" in issue)
        ):
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
        """是否统计/排行类目标（需要全市场分母）。

        判定统一走 task_intent.market_intent（唯一规则来源）：必须同时具备
        行情指标语义与统计语义——此前用"合计/汇总"等聚合动词判定，把
        「月度销售数据计算合计/均值」这类本地自造数据任务也判成统计类，
        于是往 code_execution 步骤注入全市场 ranking.csv 指令。"""
        from task_intent import is_statistical_goal
        return is_statistical_goal(goal)

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
            # 搜索结果与目标无关（_contract_issue 的相关性校验失败）→
            # 归为 SEARCH_IRRELEVANT 而非笼统 CONTRACT_VIOLATION，让反思
            # 能识别"搜索质量差"这一根因并指导换查询词（#6 根因传导）。
            if capability == "web_search" and ("相关" in issue or "无关" in issue or "未命中" in issue):
                return "SEARCH_IRRELEVANT"
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
        if error_type == "SEARCH_IRRELEVANT":
            return (
                "搜索结果与目标无关：必须更换查询词组合并追加权威机构定向"
                "（Gartner/IDC/TrendForce 等 site: 域名或公司财报 IR 页），"
                "禁止沿用上次查询词；后续步骤只采用与目标直接相关的结果"
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
# 财务图表归一化/分组纯函数（薄委托）
# 实现已迁移至 chart_assembly（深化拆分）；此处保留导出兼容，
# 供回归测试与历史调用方 from orchestrator_v2 import ... 使用。
# ─────────────────────────────────────────────

_FINANCIAL_ENTITY_PREFIX_RE = chart_assembly._FINANCIAL_ENTITY_PREFIX_RE
_FINANCIAL_YEAR_PREFIX_RE = chart_assembly._FINANCIAL_YEAR_PREFIX_RE


def _normalize_financial_metric(label: str) -> str:
    """薄委托：实现见 chart_assembly._normalize_financial_metric。"""
    return chart_assembly._normalize_financial_metric(label)


def _group_financial_rows(rows) -> dict:
    """薄委托：实现见 chart_assembly._group_financial_rows。"""
    return chart_assembly._group_financial_rows(rows)


# ─────────────────────────────────────────────
# Standalone listener (drop-in replacement)
# ─────────────────────────────────────────────

def accept_task_request(orch, data: dict) -> tuple[bool, str]:
    """接收一条任务请求：登记 QUEUED（唯一写者）+ 写收执键。

    返回 (是否接收, 原因)。抽出成独立函数是为了可测：收执是"提交是否成功"的
    唯一依据，不能让它的判定逻辑埋在 main() 的循环里。

    判定依据是 `task_state.mark_queued()` 的**返回值**，不是异常：该函数此前
    内部吞掉一切异常并正常返回，靠 except 区分 accepted/rejected 的分支永不触发，
    于是任务库不可写时提交方仍拿到 accepted（界面显示成功、现实里没有任务）。
    """
    task_id = str(data.get("task_id") or "")
    goal = str(data.get("goal") or "")
    if not task_id:
        return False, "缺少 task_id"
    ok, reason = True, ""
    try:
        import task_state as _ts
        wrote = _ts.mark_queued(
            task_id, goal,
            project=str(data.get("project") or "default"),
            conversation_id=str(data.get("conversation_id") or ""),
            parent_task_id=str(data.get("parent_task_id") or ""),
            context=str(data.get("context") or ""),
            user=str(data.get("user_id") or ""),
            # 研究契约在**提交时**落库：底稿只读它，抓取元数据不得反向决定研究对象
            research_request=(data.get("research_request")
                              if isinstance(data.get("research_request"), dict) else None),
        )
        if not wrote:
            ok, reason = False, "登记失败：任务库不可写（详见编排器日志）"
    except Exception as exc:
        ok, reason = False, f"登记失败：{str(exc)[:120]}"
    try:
        orch._redis.setex(
            f"task_ack:{task_id}", 120,
            "accepted" if ok else f"rejected:{reason}")
    except Exception as exc:
        logger.error("任务 %s 收执键写入失败：%s（提交方会超时判为失败）",
                     task_id, str(exc)[:150])
    if ok:
        logger.info("Task %s accepted (queued)", task_id)
    else:
        logger.error("Task %s rejected: %s", task_id, reason)
    return ok, reason


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
            report_confirm = bool(data.get("report_confirm", False))
            project = data.get("project") or "default"

            # 状态写者收口：QUEUED 由**编排器**登记（webui 不再写 task_history），
            # 并写收执键让提交方知道请求已被接收（pub/sub 本身无回执；
            # 编排器不在时消息静默丢失，收执键能把这个事实变成"提交失败"）
            if goal and goal != "EVOLUTION_TRIGGER":
                accept_task_request(orch, data)

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
            def _run_task(tid, g, ctx, ar, tpl_steps, uid, proj, rc):
                try:
                    result = orch.run(
                        tid, g, ctx, auto_run=ar, template_steps=tpl_steps,
                        user_id=uid, project=proj, report_confirm=rc,
                    )
                    result["project"] = proj
                    # 兜底落库：run() 内有 6 个提前 return（余额不足/端点不可用/
                    # 规划失败等），它们各自 push 了 task_complete 但未必走到主终态；
                    # 这里是唯一能覆盖全部返回路径与异常路径的单点。
                    orch._finalize_task(
                        tid, g, str(result.get("status") or "FAILED"),
                        report=str(result.get("report") or ""),
                        steps=result.get("steps") or [],
                        acceptance=result.get("acceptance") or {},
                    )
                    orch._messaging.publish("orchestrator:response", result)
                except Exception as e:
                    logger.error("Task %s failed: %s", tid, e)
                    orch._finalize_task(tid, g, "FAILED", report=str(e))
                    orch._messaging.publish("orchestrator:response",
                                            {"task_id": tid, "status": "FAILED",
                                             "report": str(e), "project": proj})
            threading.Thread(
                target=_run_task,
                args=(
                    task_id, goal, context, auto_run,
                    data.get("template_steps"), data.get("user_id", ""),
                    project, report_confirm,
                ),
                daemon=True,
            ).start()

        except Exception as e:
            logger.error("Message error: %s", e)


if __name__ == "__main__":
    main()
