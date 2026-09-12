# -*- coding: utf-8 -*-
"""任务意图判定：统一"这份目标是否需要外部行情/全市场数据"的唯一规则来源。

背景：该规则此前有两份实现（`adapters/router.py` 的 `_STATISTICAL_KEYWORDS`/
`_parse_scale` 与 `orchestrator_v2._is_statistical_goal`），都把"合计/汇总/分布"
这类**聚合动词**当作"需要全市场分母"的信号。于是
「用 Python 编写单文件脚本：内置近12个月的月度销售数据，计算合计/均值/最大值」
被判为 A股全市场排行，任务工作区里被塞进 `data/ranking.csv`（451KB 行情数据）、
`structured_data.json`，这些又经注入与打包进入提示词与交付包。

判定原则（按能力条件，而非裸关键词）：
1. 本地自造数据任务（内置/示例/模拟/硬编码…且未提市场）→ 不需要外部数据；
2. 需要"全市场"必须同时具备市场的**指标语义**（市场词或行情指标词）与
   **统计语义**（排行/占比/分布/百分位/前 N）；
3. 仅提及市场但无统计语义 → 按标的级行情处理（不需要全市场分母）。
"""

from __future__ import annotations

import re

# 市场词：出现即说明目标指向公开市场数据
_MARKET_WORDS = (
    "a股", "沪深", "两市", "全市场", "整个市场", "北交所", "创业板", "科创板",
    "上证", "深证", "港股", "美股", "纳斯达克", "纽交所", "交易所",
)

# 行情指标词：即使没写"市场"，出现这些也说明要的是行情数据
_QUOTE_METRIC_WORDS = (
    "成交额", "成交量", "涨幅", "跌幅", "涨停", "跌停", "换手率", "市盈率",
    "收盘价", "开盘价", "市值", "股价", "行情", "龙虎榜",
)

# 排行语义（信息性）：单独出现只说明"榜单类目标"，不等于需要全市场分母——
# "今日A股成交额排行"只取前 10（快路径），"前 250"或"前 5%"才需要全市场分页。
_RANKING_WORDS = (
    "排行", "排名", "榜单", "榜", "top", "前十", "前10", "榜首", "领涨", "领跌",
)

# 统计/分布语义（需要分母）
_STAT_WORDS = (
    "占比", "比例", "百分位", "集中度", "份额", "分布", "覆盖率",
)
# 显式要"整个市场"的表达：本身就是需要分母的强信号
_WHOLE_MARKET_WORDS = ("全市场", "整个市场", "两市", "沪深两市")

# 全市场规模的强表达：前 N%（任意 N）或前 N（N 超过快路径上限）
_FAST_PATH_MAX_TOP_N = 50

# 自造/本地数据任务的信号：不该去抓外部行情
_LOCAL_DATA_HINTS = (
    "内置", "示例", "模拟", "硬编码", "本地文件", "自造", "样例", "假设",
    "sample", "example", "demo", "mock",
)


def _hit(goal: str, words: tuple[str, ...]) -> bool:
    g = str(goal or "").lower()
    return any(w in g for w in words)


def _explicit_top_n(goal: str) -> bool:
    """是否含"前 N%"/"前 N 名|位|只|支"这类明确规模表达。"""
    g = str(goal or "").lower()
    return bool(re.search(r"前\s*\d+(?:\.\d+)?\s*(?:%|名|位|只|支|个)?", g))


def _needs_denominator(goal: str) -> bool:
    """是否需要全市场分母：显式"全市场"、占比/分布类语义，或前 N%/前 N>50。"""
    g = str(goal or "").lower()
    if _hit(g, _STAT_WORDS) or _hit(g, _WHOLE_MARKET_WORDS):
        return True
    if re.search(r"前\s*\d+(?:\.\d+)?\s*%", g):
        return True
    m = re.search(r"(?:前|top)\s*(\d{1,6})", g)
    return bool(m and int(m.group(1)) > _FAST_PATH_MAX_TOP_N)


def market_intent(goal: str) -> dict:
    """判定目标的行情数据意图。

    返回 {"needs_market_data": bool, "scope": "full_market"|"symbol"|"local"|"none",
          "reason": str, "has_market": bool, "statistical": bool}
    """
    g = str(goal or "").lower()
    has_market = _hit(g, _MARKET_WORDS)
    has_quote = _hit(g, _QUOTE_METRIC_WORDS)
    local = _hit(g, _LOCAL_DATA_HINTS)
    ranking = _hit(g, _RANKING_WORDS) or _explicit_top_n(g)
    needs_denom = _needs_denominator(g)

    if local and not has_market:
        return {
            "needs_market_data": False, "scope": "local",
            "reason": "本地自造/内置数据任务，不取外部行情",
            "has_market": False, "statistical": needs_denom, "ranking": ranking,
        }
    if (has_market or has_quote) and needs_denom:
        return {
            "needs_market_data": True, "scope": "full_market",
            "reason": "行情指标 + 占比/分布语义或大规模前 N，需要全市场分母",
            "has_market": has_market, "statistical": True, "ranking": ranking,
        }
    if has_market or has_quote:
        return {
            "needs_market_data": True, "scope": "symbol",
            "reason": "指向行情数据但只需标的级/小规模榜单",
            "has_market": has_market, "statistical": False, "ranking": ranking,
        }
    return {
        "needs_market_data": False, "scope": "none",
        "reason": "无行情语义", "has_market": False,
        "statistical": needs_denom, "ranking": ranking,
    }


def needs_full_market(goal: str) -> bool:
    """是否需要全市场数据（含分母）。"""
    return market_intent(goal)["scope"] == "full_market"


def is_statistical_goal(goal: str) -> bool:
    """兼容旧调用名：是否统计/排行类需要全市场数据的目标。"""
    return needs_full_market(goal)


# 作图意图：出现这些词说明本步/本任务要出图（决定是否注入图表数据提示）
_CHART_WORDS = (
    "图", "图表", "柱状图", "折线图", "饼图", "散点图", "热力图", "可视化",
    "chart", "plot", "histogram", "bar", "ascii 柱状", "绘图",
)


def has_chart_intent(text: str) -> bool:
    """文本是否要求作图（用于按需注入图表数据，而非"工作区有就注入"）。"""
    return _hit(text, _CHART_WORDS)


def is_relevant(text: str, name: str = "", content_head: str = "") -> bool:
    """某工作区文件是否与本步相关。

    - 已知数据文件按**意图**判定：ranking.csv / structured_data.json 只在
      目标确实需要行情数据时相关；clean_chart_data.json 只在作图步骤相关，
      且非市场任务还要看它是否含研究类键（entity_frequency 等）；
    - 其他文件按主题词相交判定（文件名或内容开头）。

    背景：工作区里有什么就往提示词里说什么，会把别的步骤/预载的产物塞进当前
    步骤，实测把本地销售脚本任务的交付代码带偏成金融绘图。
    """
    low = str(name or "").lower()
    if "ranking" in low or "structured_data" in low:
        return market_intent(text)["needs_market_data"]
    if "clean_chart_data" in low:
        if not has_chart_intent(text):
            return False
        if market_intent(text)["needs_market_data"]:
            return True
        try:
            import json
            keys = set(json.loads(str(content_head or "{}")).keys())
        except Exception:
            keys = set()
        return bool(keys - _MARKET_ONLY_KEYS)
    probe = f"{name} {str(content_head or '')[:400]}"
    return _topic_overlap(text, probe)


# clean_chart_data.json 里只对行情任务有意义的键
_MARKET_ONLY_KEYS = {
    "market_data", "market_share", "macro_indicators", "market_trends", "notes",
}


def _topic_overlap(text: str, other: str) -> bool:
    """共享主题词判定：取两侧中文 2-4 字片段与英文词，剔除泛词后求交集。"""
    from prompt_registry import _tokens  # 复用统一分词，避免再造一套

    inter = _tokens(text) & _tokens(other)
    inter -= _ABILITY_WORDS
    return bool(inter)


_ABILITY_WORDS = {
    "代码", "脚本", "python", "文件", "程序", "实现", "运行", "步骤", "结果",
    "图表", "绘图", "统计", "汇总", "合计", "要求", "标准", "格式", "来源",
    "输出", "生成", "数据", "报告", "分析", "搜索", "内容", "数值", "指标",
}
