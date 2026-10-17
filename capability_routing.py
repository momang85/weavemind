"""能力 → 执行者路由表（任务17）：orchestrator 与 metrics 共享的唯一定义。

此前 `CAPABILITY_WORKER_MAP` 只长在 orchestrator_v2.py 里，metrics_collector
要判"能力×执行者错配"就得各自硬编码一份白名单，两处必然漂移。任务17 起
搬到这里，orchestrator_v2 仍以同名导入暴露（旧引用不破坏），metrics/
guardian 用同一份定义做交叉表与错配统计。

背景（任务12 复盘 `agents.db` 822 步 capability×executor 交叉表）：web_search
有 19 步由 content_summarizer、5 步由 codeexecworker 执行；web_fetch 有 8 步
由 content_summarizer 执行——这些"由别人代做"的步骤被记到原能力名下，把
by_capability 的成功率与真实能力健康度混在一起，且检索质量不可信却静默成功。

不在键里的能力（content_summary / package 等）**不设限**：按现有实现自由执行，
不视为错配。执行者名单以 worker 注册值为准（workers/*_worker.py 的
capabilities / agent_id）。
"""

from __future__ import annotations

# 能力 → **该能力的合法执行者**（worker 注册的 agent_id）。
CAPABILITY_WORKER_MAP: dict[str, set[str]] = {
    "web_search": {"search_agent"},
    "web_fetch": {"webfetchworker"},
    "code_execution": {"codeexecworker"},
    "react_agent": {"reactagentworker"},
    "report_generator": {"reportgeneratorworker"},
    "data_analyzer": {"dataanalyzerworker"},
}


def expected_executors(capability: str) -> set[str] | None:
    """该能力的合法执行者集合；返回 None 表示该能力**不设限**。

    调用方必须把 None 与空集区分开：None = 任何执行者都合法（不算错配），
    空集理论上不该出现（等价于"谁都不许做"，仅作防御性兜底）。
    """
    return CAPABILITY_WORKER_MAP.get(str(capability or "").strip()) or None
