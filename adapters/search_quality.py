# -*- coding: utf-8 -*-
"""检索质量共享模块：查询预处理、查询变体、相关性计分、垃圾过滤、权威域分级。

**单一事实来源**：快答/行业预载的轻量检索（`adapters.text_search`）与
`worker_base.SearchAgent` 此前各自维护一套近似的词表、计分与变体逻辑——同一份
结果在两条路径上判定不同（一条有长连读要求与权威加权，另一条没有），阈值与
关键词还都写死在代码里。本模块收敛为唯一实现，并把可调项收进 `SearchQualityPolicy`
（config.json 的 `system.search_quality` + 环境变量覆盖）：

- `min_score`：相关性最低分；
- `require_hit_min_run`：查询含 ≥3 字连续词段时要求命中的连读长度
  （0 = 关闭该要求）；
- 权威域四档权重（官方/行业研究/优质/降权）；
- 词表与域名黑名单（垃圾站、低权威文库、垃圾路径、垃圾标题、停用词）；
- `max_variants` 与机构白名单、财经/A 股定向模板开关；
- `engines`：ddgs 引擎清单与健康冷却（引擎清单本身不再写死在 worker_base）。

设计要点（对应"Bing 分词错配/权威信息少"缺陷）：
- `extract_keywords`：按停用词切出完整词段（防 2-4 字滑窗把"固态电池"
  拆碎），保留年份与英文词——查询不再原样直塞搜索引擎；
- `score_results`：中文按 2/3/4 字滑窗、英文按词边界计分，min_score 过滤
  与主题无关的结果（"固态硬盘"只与"固态电池"共享"固态"二字，2 字滑窗
  仅 +1，4 字滑窗命中为零 → 被滤除）；权威域加权排序并打 authoritative
  标记，报告优先引用。
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# ── 停用词（对齐 worker_base.SearchAgent._ZH_STOP/_EN_STOP 语义）───────
_ZH_STOP = {
    "一个", "我们", "你们", "他们", "完成", "输出", "生成", "要求",
    "进行", "需要", "可以", "是否", "如何", "什么", "请", "帮", "并",
    "与", "和", "在", "用", "把", "将", "给", "让", "这", "那", "为",
    "对", "其", "及", "或", "等", "做", "写", "的", "了", "是", "我",
    "你", "他", "她", "它", "搜索", "获取", "项目", "文件", "内容",
    "结果", "报告", "选择", "优先", "记录", "完整", "基于", "上一步",
    "编写", "实现", "包含", "以及", "使用", "提供", "相关", "信息",
    "资料", "地址", "链接", "来源", "开源", "同时", "需要", "并且",
    "如果", "找到", "查看", "说明", "运行", "方式", "明确", "给出",
    "任务", "目标", "原始", "指令", "调研", "现状", "国内", "国外",
    "关于", "针对", "请根据", "确保", "然后", "随后", "接下来",
    "角色", "受众", "质量标准", "输出要求", "任务目标", "用户目标",
    "自迭代改进", "一份", "董事会", "汇报", "注明", "预测", "建议",
    "需包含", "风险分析", "对比", "数据", "图表", "指标",
}
_EN_STOP = {
    "the", "and", "with", "from", "for", "that", "this", "not",
    "are", "was", "were", "output", "only", "json", "using", "your",
}

# ── 垃圾过滤（对齐 worker_base 语义）────────────────────────────────
_SPAM_DOMAINS = (
    "susmeat.com", "aydvjch.cc", "example.com", "imty-web.com",
    "zhxsg.com", "mmzx2.cn", "ng28gaming.com", "online-28quan.com",
    "28quan.com", "365qp", "88qp", "h888", "ky777", "lywl",
)
_LOW_AUTHORITY_DOMAINS = (
    "wenku.baidu.com", "book118.com", "max.book118.com", "doc88.com",
    "docin.com", "jz.docin.com", "mbd.baidu.com", "wenku.so.com",
)
_JUNK_URL_PATTERNS = (
    r"/works/\d+\.html",
    r"/tiyu-toutiao/",
    r"/login|/register|/agent",
)
_GAMBLING_KEYWORDS = (
    "博彩", "六合彩", "彩票", "投注", "下注", "返水", "棋牌", "电玩",
    "真人视讯", "娱乐城", "时时彩", "开户送", "注册送", "秒到账",
    "提现", "抢庄", "龙虎", "牛牛", "百家乐", "老虎机", "赌场",
    "casino", "lottery", "bet365", "betting", "gambling",
)
_JUNK_TITLES = ("google", "bing", "microsoft", "登录", "403", "404")

# ── 权威域分级 ─────────────────────────────────────────────────────
# 官方 IR/交易所（对齐 orchestrator._pick_fetch_url 的 official_domains）
AUTHORITY_OFFICIAL = (
    "ir.", "investor.", "hkex", "eastmoney", "10jqka",
    "cninfo", "sse.com.cn", "szse.cn",
)
# 权威财经媒体（对齐 _pick_fetch_url 的 good_domains）
AUTHORITY_GOOD = (
    "sina", "163.com", "21jingji", "yicai", "cls.cn", "finance",
    "stock", "xueqiu", "snowball", "pedaily",
)
# 内容社区/杂页（对齐 _pick_fetch_url 的 junk_domains）
AUTHORITY_JUNK = (
    "blog.csdn", "zhihu.com", "zhengxianling", "cp.baidu",
    "toutiao", "csdn", "alishui", "sgpjbg",
)
# 中文行业研究/官方统计站点（新增：行业调研任务权威信息少的主因是
# 检索未对这些站点加权，报告只能退化为"基于模型知识"）
AUTHORITY_INDUSTRY = (
    "gg-lb.com",        # 高工锂电
    "chinairn.com",     # 中研网
    "chyxx.com",        # 中研普华（智研咨询）
    "askci.com",        # 中商产业研究院
    "qianzhan.com",     # 前瞻产业研究院
    "iresearch.com.cn", # 艾瑞咨询
    "stcn.com",         # 证券时报
    "thepaper.cn",      # 澎湃新闻
    "gov.cn",           # 中国政府网
    "miit.gov.cn",      # 工信部
    "stats.gov.cn",     # 国家统计局
    "gartner.com", "idc.com", "trendforce.com", "statista.com",
    "counterpointresearch.com", "canalys.com", "macrotrends.net",
)

# ddgs text 引擎全集（backend 参数按名过滤）。auto 模式每次查询都会尝试全部引擎，
# 而环境内 wikipedia/google 等可能 100% 超时，每次白等 5s×N——引擎清单因此可配。
DDG_ENGINES = (
    "brave", "duckduckgo", "google", "grokipedia", "mojeek",
    "startpage", "wikipedia", "yahoo", "yandex",
)

# 调研类目标追加的机构定向域名（原先写死在 worker_base 的查询变体里）
RESEARCH_DOMAINS = (
    "gartner.com", "idc.com", "trendforce.com", "statista.com",
    "counterpointresearch.com", "canalys.com", "macrotrends.net",
)


@dataclass
class SearchQualityPolicy:
    """检索质量的可调项（默认值 = 迁移前的既有行为）。

    为什么要收成一个对象：这些阈值与词表原先分散在 worker_base 与
    adapters/search_quality **两处**且都写死——两条路径判定不一致（一条有长连读
    要求与权威加权、另一条没有），改一处忘另一处就会出现"同一个结果在一处被过滤、
    在另一处被保留"。现在只有一份，且能按部署环境调整（config.json 的
    `system.search_quality` 段，环境变量可覆盖标量项）。
    """

    min_score: int = 2
    # 查询含 ≥N 字连续词段时，结果必须命中该长度的连读（0 = 关闭）
    require_hit_min_run: int = 3
    max_variants: int = 10
    # 权威域四档权重
    authority_official: int = 4
    authority_industry: int = 3
    authority_good: int = 2
    authority_junk: int = -2
    # 命中模型
    zh_token_weight: tuple = (1, 2)      # (2 字权重, ≥3 字权重)
    en_token_weight: int = 2
    strip_token_weight: int = 1          # 命中"策略加权域名"的加分
    engines: tuple = DDG_ENGINES
    research_domains: tuple = RESEARCH_DOMAINS
    # 关键词表与黑名单（可整体替换）
    zh_stop: frozenset = field(default_factory=lambda: frozenset(_ZH_STOP))
    en_stop: frozenset = field(default_factory=lambda: frozenset(_EN_STOP))
    spam_domains: tuple = _SPAM_DOMAINS
    low_authority_domains: tuple = _LOW_AUTHORITY_DOMAINS
    junk_url_patterns: tuple = _JUNK_URL_PATTERNS
    gambling_keywords: tuple = _GAMBLING_KEYWORDS
    junk_titles: tuple = _JUNK_TITLES
    authority_official_domains: tuple = AUTHORITY_OFFICIAL
    authority_industry_domains: tuple = AUTHORITY_INDUSTRY
    authority_good_domains: tuple = AUTHORITY_GOOD
    authority_junk_domains: tuple = AUTHORITY_JUNK
    # 定向变体开关（关掉可减少查询数，代价是权威命中率）
    finance_hints: bool = True
    research_hints: bool = True
    ashare_hints: bool = True


_DEFAULT_POLICY = SearchQualityPolicy()
_policy_lock = threading.Lock()
_policy_cache: dict = {"mtime": None, "policy": None, "path": ""}


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


def _config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config.json"


def load_policy(cfg: dict | None = None) -> SearchQualityPolicy:
    """从 `system.search_quality` 段构造策略；缺省项沿用默认（既有行为）。"""
    section = {}
    if isinstance(cfg, dict):
        sys_cfg = cfg.get("system") or {}
        if isinstance(sys_cfg, dict):
            section = sys_cfg.get("search_quality") or {}
    if not isinstance(section, dict):
        section = {}

    def _int(key: str, default: int) -> int:
        try:
            return int(section.get(key, default))
        except (TypeError, ValueError):
            return default

    def _tuple(key: str, default: tuple) -> tuple:
        raw = section.get(key)
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(x) for x in raw)
        return default

    def _flag(key: str, default: bool) -> bool:
        raw = section.get(key)
        return default if raw is None else bool(raw)

    return SearchQualityPolicy(
        min_score=_env_int("WM_SEARCH_MIN_SCORE", _int("min_score", 2)),
        require_hit_min_run=_env_int(
            "WM_SEARCH_REQUIRE_HIT_RUN", _int("require_hit_min_run", 3)),
        max_variants=_env_int("WM_SEARCH_MAX_VARIANTS", _int("max_variants", 10)),
        authority_official=_int("authority_official", 4),
        authority_industry=_int("authority_industry", 3),
        authority_good=_int("authority_good", 2),
        authority_junk=_int("authority_junk", -2),
        engines=_tuple("engines", DDG_ENGINES),
        research_domains=_tuple("research_domains", RESEARCH_DOMAINS),
        zh_stop=frozenset(section.get("zh_stop") or _ZH_STOP),
        en_stop=frozenset(section.get("en_stop") or _EN_STOP),
        spam_domains=_tuple("spam_domains", _SPAM_DOMAINS),
        low_authority_domains=_tuple("low_authority_domains", _LOW_AUTHORITY_DOMAINS),
        junk_url_patterns=_tuple("junk_url_patterns", _JUNK_URL_PATTERNS),
        gambling_keywords=_tuple("gambling_keywords", _GAMBLING_KEYWORDS),
        junk_titles=_tuple("junk_titles", _JUNK_TITLES),
        authority_official_domains=_tuple("authority_official_domains", AUTHORITY_OFFICIAL),
        authority_industry_domains=_tuple("authority_industry_domains", AUTHORITY_INDUSTRY),
        authority_good_domains=_tuple("authority_good_domains", AUTHORITY_GOOD),
        authority_junk_domains=_tuple("authority_junk_domains", AUTHORITY_JUNK),
        finance_hints=_flag("finance_hints", True),
        research_hints=_flag("research_hints", True),
        ashare_hints=_flag("ashare_hints", True),
    )


def current_policy() -> SearchQualityPolicy:
    """当前生效策略：config.json 变更后自动重载（按 mtime，低频）。

    读不到配置时用默认策略（不抛错：检索路径不能因为配置问题整体失效）。
    """
    try:
        path = _config_path()
        mtime = path.stat().st_mtime if path.exists() else 0.0
    except Exception:
        return _DEFAULT_POLICY
    with _policy_lock:
        if _policy_cache["policy"] is not None and _policy_cache["mtime"] == mtime \
                and _policy_cache["path"] == str(path):
            return _policy_cache["policy"]
    cfg = {}
    if mtime:
        try:
            import json
            cfg = json.loads(path.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}
    policy = load_policy(cfg)
    with _policy_lock:
        _policy_cache.update({"mtime": mtime, "policy": policy, "path": str(path)})
    return policy


def clean_search_text(text: str) -> str:
    """去掉指令包装，取"用户目标"作为查询基础（"任务目标/用户目标/原始指令"
    等包装词混进查询会污染相关性）。"""
    m = re.search(r"用户目标：([^\n]+)", str(text))
    if m:
        return m.group(1).strip()
    t = re.sub(r"^(任务目标|原始指令|用户目标)[：:]\s*", "", str(text).strip())
    idx = t.find("\n【角色】")
    if idx > 0:
        t = t[:idx]
    return t.strip()


def extract_keywords(text: str, policy: SearchQualityPolicy | None = None) -> str:
    """核心搜索词：去指令包装与停用词、保留年份，按停用词切出完整词段
    （避免 2-4 字滑窗把"新能源汽车/固态电池"拆成碎片）。"""
    pol = policy or _DEFAULT_POLICY
    t = clean_search_text(text)
    t = re.sub(r"(\d{4})年", r"\1 ", t)
    for w in pol.zh_stop:
        t = t.replace(w, " ")
    zh = [
        s for s in re.split(r"[\s\u3000，。、；：！？（）()【】《》\"'“”‘’,.…]+", t)
        if re.search(r"[\u4e00-\u9fff]", s) and len(s) >= 2
    ]
    en = [
        w.lower()
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", t)
        if w.lower() not in pol.en_stop
    ]
    years = re.findall(r"\d{4}", t)
    merged = list(dict.fromkeys(years + zh + en))
    return " ".join(merged)[:150]


# 指令性标记：整句变体在这里截断（"生成…汇报/需包含/请"之后的部分对搜索无意义）
_IMPERATIVE_CUT_RE = re.compile(
    r"(生成|撰写|输出|需包含|请给出|列出|注明|要求|必须|包含[^，。]{0,10}图表|汇报[：:])"
)
_FINANCE_HINTS = (
    "财报", "年报", "季报", "营收", "净利润", "负债", "财务", "业绩",
    "financial", "revenue", "earnings",
)
_RESEARCH_HINTS = (
    "调研", "市场规模", "竞争格局", "预测", "趋势", "行业报告",
    "市场份额", "占比", "研报", "analysis", "forecast", "market size",
)
_ASHARE_HINTS = (
    "成交量排行", "成交额排行", "成交量前十", "成交额前十",
    "涨停", "跌幅榜", "a股今日", "今日a股", "前十股", "排名榜",
    "股票排行", "a股排行", "股票排名",
)


def build_query_variants(query: str, max_variants: int | None = None,
                         policy: SearchQualityPolicy | None = None,
                         rich: bool = False) -> list[str]:
    """查询变体：关键词组合优先 + 中文短语带引号精确变体。

    引号变体解决 Bing 中文分词错配（"固态电池"被拆成"固态"+"电池"
    返回固态硬盘 SSD 结果）：变体 2 把关键词组合整体加引号强制
    短语匹配。无 CJK 关键词时不生成引号变体（英文引号搜索语义不同）。

    `rich=True`（SearchAgent 的长链检索用）：再追加整句变体、时效性年份、
    财经/A 股定向模板与机构域名定向——这些模板原先写死在 `worker_base`，
    与轻量检索共用同一份实现后不再有两套变体逻辑。
    变体上限缺省时按模式取：轻量检索 3 条（现状），长链检索取策略的
    `max_variants`（默认 10：关键词 + 整句 + 机构定向 + 公司 IR 定向，
    旧上限 6 会挤掉新定向）。
    """
    pol = policy or current_policy()
    if max_variants is None:
        max_variants = int(pol.max_variants) if rich else 3
    kws = [k for k in extract_keywords(query, pol).split() if k]
    variants: list[str] = []
    if kws:
        variants.append(" ".join(kws)[:120])
        zh_kws = [k for k in kws if re.search(r"[\u4e00-\u9fff]", k)]
        if len(zh_kws) >= 1:
            quoted = '"' + " ".join(zh_kws)[:80] + '"'
            if quoted not in variants:
                variants.append(quoted)
    clean = clean_search_text(query)
    if clean and len(clean) >= 4 and clean not in variants:
        variants.append(clean[:60])
    if not rich:
        return variants[:max_variants]
    return _rich_variants(query, variants, kws, max_variants, pol)


def _rich_variants(query: str, variants: list[str], kws: list[str],
                   max_variants: int, pol: SearchQualityPolicy) -> list[str]:
    """整句/时效/财经/A股/机构定向与 site: 变体（SearchAgent 长链检索用）。"""
    goal = clean_search_text(query)
    if goal and len(goal) >= 4:
        cut = _IMPERATIVE_CUT_RE.split(goal, maxsplit=1)[0].strip()
        trimmed = (cut or goal)[:60]
        if trimmed not in variants:
            variants.append(trimmed)
        # 时效性（修复"最新财报"返回旧年份）：目标要求最新时补当前年份
        if any(k in query for k in ("最新", "最近", "latest", "current")):
            variants.append(f"{trimmed[:50]} {time.localtime().tm_year}")
        # 财报类目标：引导结果页含具体数字（营收/净利润/亿元），
        # 否则 snippet 常只有叙事没有数值，清洗层无数据可洗
        if pol.finance_hints and any(k in query for k in _FINANCE_HINTS):
            variants.append(f"{trimmed[:50]} 年报 营收 净利润 亿元")
            variants.append(f"{trimmed[:50]} 财务数据 亿元")
        # 调研类目标：追加权威机构定向查询。实测报告大量引用权威机构但搜索结果
        # 从未命中——源头是查询未定向，LLM 只能编造来源。
        if pol.research_hints and any(k in query for k in _RESEARCH_HINTS):
            for dom in pol.research_domains:
                variants.append(f"{trimmed[:40]} site:{dom}")
            if any(k in str(query).lower() for k in (
                "英伟达", "nvidia", "amd", "英特尔", "intel",
                "台积电", "tsmc", "苹果", "apple", "微软", "microsoft",
            )):
                variants.append(
                    f"{trimmed[:40]} 财报 营收 净利润 "
                    "site:ir.nvidia.com site:investor.amd.com")
        # A股行情排行类目标：追加财经站点定向查询模板并排除无关平台
        if pol.ashare_hints and any(k in query for k in _ASHARE_HINTS):
            metric = ("成交额"
                      if "成交额" in query and "成交量" not in query else "成交量")
            variants.append(
                f"今日 A股 {metric} 排行 前十 东方财富 "
                "-site:youtube.com -site:baike.baidu.com")
            variants.append(f"{trimmed[:50]} {metric} 排行 site:eastmoney.com")
            variants.append(f"{trimmed[:50]} 东方财富 同花顺 新浪财经 雪球")
    # 域名定向（ReAct 兜底）：指令含 site:xxx 时追加定向查询变体
    for m in re.finditer(r"site:\s*([a-zA-Z0-9.\-]+)", str(query)):
        dom = m.group(1).strip()
        variants.append(f"{goal[:110]} site:{dom}")
        if kws:
            variants.append(f"{' '.join(kws[:3])} site:{dom}")
    if kws:
        for i in range(1, min(len(kws), 5)):
            sub = " ".join(kws[: i + 1])
            if sub:
                variants.append(sub[:120])
    en = [
        w.lower()
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", query)
        if w.lower() not in pol.en_stop
    ]
    if en and kws:
        variants.append(" ".join(kws[:2] + en[:2])[:120])
    seen: set[str] = set()
    out: list[str] = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out[:max_variants]



def _query_tokens(query: str) -> tuple[set[str], set[str], set[str]]:
    """查询 → (中文 2/3/4 字滑窗 token, 年份, 英文词)。"""
    clean = clean_search_text(query)
    tokens: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", clean):
        for size in (4, 3, 2):
            if len(run) >= size:
                tokens.update(run[i:i + size] for i in range(len(run) - size + 1))
    year_tokens = {y for y in re.findall(r"\d{4}", clean)}
    en_tokens = {w.lower() for w in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", clean)}
    return tokens, year_tokens, en_tokens


def _is_garbage_result(title: str, url: str, snip: str = "",
                       policy: SearchQualityPolicy | None = None) -> bool:
    """私有别名（保留旧调用点）：实际判定在 `is_garbage_result`。"""
    return is_garbage_result(title, url, snip, policy)


def is_garbage_result(title: str, url: str, snip: str = "",
                      policy: SearchQualityPolicy | None = None) -> bool:
    """通用垃圾识别（公开入口）：低权威文库/博彩/下载站/假页。

    词表来自策略——两条检索路径共用同一份判定，不再各自维护一份近似词表。
    """
    pol = policy or _DEFAULT_POLICY
    u = str(url or "").lower()
    t = str(title or "")
    s = str(snip or "")
    if not u.startswith("http"):
        return True
    for d in pol.low_authority_domains:
        if d in u:
            return True
    if pol.junk_url_patterns and re.search("|".join(pol.junk_url_patterns), u):
        return True
    if any(d in u for d in pol.spam_domains):
        return True
    hay = t + " " + s
    if any(k in hay for k in pol.gambling_keywords):
        return True
    if any(m in hay for m in ("404 not found", "page not found",
                              "无法访问该页面", "页面不存在", "您访问的页面不存在")):
        return True
    if t.lower().strip() in pol.junk_titles:
        return True
    return False


def _domain_hits(url: str, needle: str) -> bool:
    u = str(url or "").lower()
    return needle.lower() in u


def score_results(query: str, results: list[dict], min_score: int | None = None,
                  policy: SearchQualityPolicy | None = None,
                  blocks: tuple | list = (), boosts: tuple | list = ()) -> list[dict]:
    """按主题相关性过滤并排序：中文滑窗计分 + 权威域加权。

    返回排序后的列表，权威命中者带 authoritative=True；每条带 `score`。
    query 提取不到任何 token（纯英文短词/单字）时不过滤（无从判断相关性）。

    `min_score`/`policy`：阈值与词表来自策略（默认取当前配置）。
    `blocks`/`boosts`：**已部署策略**的个性化域名（黑名单直接剔除、白名单 +N 分）
    ——它们此前只作用于 SearchAgent 那条路径，现在作为参数进入唯一实现，
    轻量检索也能复用同一套变体/计分。
    """
    pol = policy or current_policy()
    if min_score is None:
        min_score = pol.min_score
    blocks_l = tuple(str(b) for b in (blocks or ()) if str(b))
    boosts_l = tuple(str(b) for b in (boosts or ()) if str(b))
    tokens, year_tokens, en_tokens = _query_tokens(query)
    if not tokens and not year_tokens and not en_tokens:
        return list(results)
    # 查询含 ≥3 字的连续词段时，结果必须命中足够长的连读才算主题相关：
    # 词段 ≥4 字要求命中 4 字连读（"固态电池"）；词段 =3 字要求命中完整
    # 词段。仅靠"固态"+"电池"等散落 2 字词或"固态电"（来自"固态电子
    # 存储芯片"）凑分的是主题不符条目——固态硬盘 SSD 百科恰是这种误中。
    # 查询只有短词（"苹果 财报"）时不做此要求，避免误杀正常结果。
    _runs = re.findall(r"[\u4e00-\u9fff]+", clean_search_text(query))
    _max_run = max((len(r) for r in _runs), default=0)
    _require_hit = None
    if pol.require_hit_min_run:
        if _max_run >= 4:
            _require_hit = max(4, int(pol.require_hit_min_run))
        elif _max_run == 3:
            _require_hit = min(3, int(pol.require_hit_min_run))
    _w2, _wbig = pol.zh_token_weight
    kept: list[tuple[int, dict]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "")
        url = str(r.get("url") or "")
        snip = str(r.get("snippet") or "")
        if not title or not url.startswith("http"):
            continue
        if is_garbage_result(title, url, snip, pol):
            continue
        url_title = (url + " " + title).lower()
        if any(b.lower() in url_title for b in blocks_l):
            continue
        hay = title + " " + snip
        hay_lower = hay.lower()
        score = 0
        for tok in tokens:
            if tok in hay:
                score += _w2 if len(tok) == 2 else _wbig
        for y in year_tokens:
            if y in hay:
                score += 1
        for tok in en_tokens:
            if len(tok) >= 3 and re.search(
                rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])", hay_lower,
            ):
                score += pol.en_token_weight
        # 权威域加权：官方/行业研究站优先，内容社区降权
        authoritative = False
        if any(_domain_hits(url, d) for d in pol.authority_official_domains):
            score += pol.authority_official
            authoritative = True
        if any(_domain_hits(url, d) for d in pol.authority_industry_domains):
            score += pol.authority_industry
            authoritative = True
        if any(_domain_hits(url, d) for d in pol.authority_good_domains):
            score += pol.authority_good
        if any(_domain_hits(url, d) for d in pol.authority_junk_domains):
            score += pol.authority_junk
        if boosts_l and any(b.lower() in url_title for b in boosts_l):
            score += pol.strip_token_weight + 2
        # 长连读要求只对**中文结果**生效：这条规则是为了杀掉"同语种但主题不符"
        # 的条目（"固态电池"查询命中"固态硬盘"）。查询里常混着中文指令碎片
        # （"搜索GitHub上完整的…开源项目"），若对纯英文结果也要求中文 4 字连读，
        # 合法的英文结果会被整批误杀（实测：GitHub 项目检索全军覆没）。
        # 英文结果的判定交给英文词边界计分与 min_score。
        #
        # 调用方**显式放宽** min_score（低于策略默认）时不套这条硬规则：那正是
        # "严格过滤为空 → 放宽再试"的兜底路径，再硬性要求连读等于放宽无效。
        _loosened = min_score < pol.min_score if min_score is not None else False
        if _require_hit is not None and not _loosened \
                and re.search(r"[\u4e00-\u9fff]", hay):
            if not any(len(t) == _require_hit and t in hay for t in tokens):
                continue
        if score < min_score:
            continue
        r2 = dict(r)
        r2["score"] = score
        r2["authoritative"] = authoritative
        kept.append((score, r2))
    kept.sort(key=lambda x: -x[0])
    return [r for _, r in kept]

    if not tokens and not year_tokens and not en_tokens:
        return list(results)
    # 查询含 ≥3 字的连续词段时，结果必须命中足够长的连读才算主题相关：
    # 词段 ≥4 字要求命中 4 字连读（"固态电池"）；词段 =3 字要求命中完整
    # 词段。仅靠"固态"+"电池"等散落 2 字词或"固态电"（来自"固态电子
    # 存储芯片"）凑分的是主题不符条目——固态硬盘 SSD 百科恰是这种误中。
    # 查询只有短词（"苹果 财报"）时不做此要求，避免误杀正常结果。
    _runs = re.findall(r"[\u4e00-\u9fff]+", clean_search_text(query))
    _max_run = max((len(r) for r in _runs), default=0)
    _require_hit = None
    if _max_run >= 4:
        _require_hit = 4
    elif _max_run == 3:
        _require_hit = 3
    kept: list[tuple[int, dict]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "")
        url = str(r.get("url") or "")
        snip = str(r.get("snippet") or "")
        if not title or not url.startswith("http"):
            continue
        if _is_garbage_result(title, url, snip):
            continue
        hay = title + " " + snip
        hay_lower = hay.lower()
        score = 0
        for tok in tokens:
            if tok in hay:
                score += 1 if len(tok) == 2 else 2
        for y in year_tokens:
            if y in hay:
                score += 1
        for tok in en_tokens:
            if len(tok) >= 3 and re.search(
                rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])", hay_lower,
            ):
                score += 2
        # 权威域加权：官方/行业研究站优先，内容社区降权
        authoritative = False
        if any(_domain_hits(url, d) for d in AUTHORITY_OFFICIAL):
            score += 4
            authoritative = True
        if any(_domain_hits(url, d) for d in AUTHORITY_INDUSTRY):
            score += 3
            authoritative = True
        if any(_domain_hits(url, d) for d in AUTHORITY_GOOD):
            score += 2
        if any(_domain_hits(url, d) for d in AUTHORITY_JUNK):
            score -= 2
        if _require_hit is not None and not any(
            len(t) == _require_hit and t in hay for t in tokens
        ):
            continue
        if score < min_score:
            continue
        r2 = dict(r)
        r2["score"] = score
        r2["authoritative"] = authoritative
        kept.append((score, r2))
    kept.sort(key=lambda x: -x[0])
    return [r for _, r in kept]
