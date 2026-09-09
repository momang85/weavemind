# -*- coding: utf-8 -*-
"""检索质量共享模块：查询预处理、相关性计分、垃圾过滤、权威域分级。

快答/行业预载的轻量检索（adapters.text_search）与 worker_base.SearchAgent
曾各自维护近似的词表与计分逻辑；本模块收敛为单一事实来源（stdlib only，
不引入 worker_base 的 Redis/messaging 依赖）。worker_base 本轮不动，
后续可按相同语义逐步迁移到本模块。

设计要点（对应"Bing 分词错配/权威信息少"缺陷）：
- extract_keywords：按停用词切出完整词段（防 2-4 字滑窗把"固态电池"
  拆碎），保留年份与英文词——查询不再原样直塞搜索引擎；
- score_results：中文按 2/3/4 字滑窗、英文按词边界计分，min_score 过滤
  与主题无关的结果（"固态硬盘"只与"固态电池"共享"固态"二字，2 字滑窗
  仅 +1，4 字滑窗命中为零 → 被滤除）；权威域加权排序并打 authoritative
  标记，报告优先引用。
"""

from __future__ import annotations

import re

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


def extract_keywords(text: str) -> str:
    """核心搜索词：去指令包装与停用词、保留年份，按停用词切出完整词段
    （避免 2-4 字滑窗把"新能源汽车/固态电池"拆成碎片）。"""
    t = clean_search_text(text)
    t = re.sub(r"(\d{4})年", r"\1 ", t)
    for w in _ZH_STOP:
        t = t.replace(w, " ")
    zh = [
        s for s in re.split(r"[\s\u3000，。、；：！？（）()【】《》\"'“”‘’,.…]+", t)
        if re.search(r"[\u4e00-\u9fff]", s) and len(s) >= 2
    ]
    en = [
        w.lower()
        for w in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}", t)
        if w.lower() not in _EN_STOP
    ]
    years = re.findall(r"\d{4}", t)
    merged = list(dict.fromkeys(years + zh + en))
    return " ".join(merged)[:150]


def build_query_variants(query: str, max_variants: int = 3) -> list[str]:
    """查询变体：关键词组合优先 + 中文短语带引号精确变体。

    引号变体解决 Bing 中文分词错配（"固态电池"被拆成"固态"+"电池"
    返回固态硬盘 SSD 结果）：变体 2 把关键词组合整体加引号强制
    短语匹配。无 CJK 关键词时不生成引号变体（英文引号搜索语义不同）。"""
    kws = [k for k in extract_keywords(query).split() if k]
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
    return variants[:max_variants]


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


def _is_garbage_result(title: str, url: str, snip: str = "") -> bool:
    """通用垃圾识别：低权威文库/博彩/下载站/假页。"""
    u = str(url or "").lower()
    t = str(title or "")
    s = str(snip or "")
    if not u.startswith("http"):
        return True
    for d in _LOW_AUTHORITY_DOMAINS:
        if d in u:
            return True
    if re.search("|".join(_JUNK_URL_PATTERNS), u):
        return True
    if any(d in u for d in _SPAM_DOMAINS):
        return True
    hay = t + " " + s
    if any(k in hay for k in _GAMBLING_KEYWORDS):
        return True
    if any(m in hay for m in ("404 not found", "page not found",
                              "无法访问该页面", "页面不存在", "您访问的页面不存在")):
        return True
    if t.lower().strip() in _JUNK_TITLES:
        return True
    return False


def _domain_hits(url: str, needle: str) -> bool:
    u = str(url or "").lower()
    return needle.lower() in u


def score_results(query: str, results: list[dict], min_score: int = 2) -> list[dict]:
    """按主题相关性过滤并排序：中文滑窗计分 + 权威域加权。

    返回排序后的列表，权威命中者带 authoritative=True。query 提取不到
    任何 token（纯英文短词/单字）时不过滤（无从判断相关性）。"""
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
