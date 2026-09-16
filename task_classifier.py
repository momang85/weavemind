# -*- coding: utf-8 -*-
"""任务分类器（task → domain + metadata）。

规则先行：识别 financial 任务，提取 公司名/年份范围/市场提示；
非 financial 任务返回 domain="general"，走原链路（搜索→抓取→清洗→报告）。
"""

import re

FINANCIAL_KEYWORDS = (
    "财报", "年报", "季报", "营收", "净利润", "财务", "业绩", "负债",
    "研发投入", "研发费用", "研发开支",
    "报表", "financial", "revenue", "earnings", "annual report", "income statement",
    # 与验收器 _FINANCIAL_MARKERS 对齐：这类纯财务措辞此前不在分类器词表，
    # 会被判 general/research——既不预载财务源，又享受非金融域的占位豁免
    "毛利率", "净利率", "市值", "现金流", "利润", "股价", "股票",
)

_MARKET_HINTS = {
    "HK": ("港股", "香港", "港交所", "hkex"),
    "US": ("美股", "纳斯达克", "nyse", "sec", "纽交所", "nasdaq"),
    "CN": ("a股", "沪深", "上交所", "深交所", "巨潮", "科创板"),
}

_GENERIC_WORDS = (
    r"搜索|总结|分析|调研|梳理|介绍|了解|盘点|回顾|关于|针对|研究|预测|"
    r"评估|生成|撰写|输出|做|写|并|与|和|及|的|之|最新|历年|年度|最近|当前|"
    r"分析一下|介绍一下|盘点一下|梳理一下"
)

# 对比类结构触发词：只有同时出现这些词才启用多实体拆分，
# 避免“与比亚迪合作”这类普通“与”字句被误拆。
_COMPARE_TRIGGER_RE = re.compile(
    r"(?:对比|比较|vs|分别|相比)", re.I,
)

# 报告期短语（整词）：断言/前瞻里必须先于"年报/季报"匹配，否则
# "比亚迪三季报"会被切成"比亚迪三" + "季报"（多实体拆分同款切分 bug）
_PERIOD_PHRASE_ALT = (
    r"年度报告|半年度报告|中期报告|一季报|三季报|半年报|中报|年度|年报|季报"
)

# 连接符后的公司名：名称后紧跟财务/对比语境词或句子结尾。
# 用前瞻限定“近三年营收”“的财报”等合法后缀语境，避免吞入后续普通动词。
_AFTER_CONNECTOR_RE = re.compile(
    r"(?:与|和|及|以及|、|跟|vs)\s*([\u4e00-\u9fff]{2,6}?)"
    rf"(?=(?:的)?(?:历年年度|历年|年度|最新|近三年|近五年|最近)?"
    rf"(?:{_PERIOD_PHRASE_ALT}|财报|财务|营收|净利润|净利|利润|收入|业绩|负债|报表|"
    r"趋势|情况|数据|表现|竞争格局|竞争|格局|技术路线|市场份额|"
    r"在|于|的市|的行|"
    r"相比|对比|比较|分别|vs)|$)",
    re.I,
)

# 连接符前的公司名（“宁德时代与比亚迪”“苹果和微软”）
_BEFORE_CONNECTOR_RE = re.compile(
    rf"(?:{_GENERIC_WORDS}|对比|比较|分别|相比)*"
    r"([\u4e00-\u9fff]{2,6}?)\s*(?:与|和|及|以及|、|跟|vs)",
    re.I,
)

# 财务语境前的公司名（“苹果的营收”“比亚迪近三年营收”），用于对比句首实体。
_BEFORE_FINANCIAL_RE = re.compile(
    rf"(?:{_GENERIC_WORDS}|对比|比较|分别|相比)*"
    r"([\u4e00-\u9fff]{2,6}?)"
    rf"(?:的)?(?:历年年度|历年|年度|最新|近三年|近五年|最近)?"
    rf"(?:{_PERIOD_PHRASE_ALT}|财报|财务|营收|净利润|净利|利润|收入|业绩|负债|报表|"
    r"趋势|情况|数据|表现)",
    re.I,
)

# 公司名候选禁用片段：财报语境词/连接符/通用动词等不能作为公司名主体。
_COMPANY_STOPWORDS = (
    "的", "与", "和", "及", "等", "之", "并", "跟", "vs",
    "对比", "比较", "分别", "相比", "一下", "一个", "这家", "该公司",
    "财报", "年报", "季报", "财务", "营收", "净利润", "净利", "利润",
    "收入", "业绩", "负债", "报表", "趋势", "情况", "数据", "表现",
    "发展", "合作", "关系", "现状", "历程", "规模",
    "分析", "总结", "调研", "梳理", "介绍", "了解", "盘点", "回顾",
    "关于", "针对", "研究", "预测", "评估", "生成", "撰写", "输出",
    "搜索", "最新", "历年", "年度", "最近", "当前", "近三年", "近五年",
    "做", "写", "请", "帮", "要", "想",
    "给出", "提供", "列出", "包括", "包含", "以及", "来源",
    # 泛化语境片段（常被连接符/财务词误捕获为"公司名"的普通名词）：
    # 观察例：'经营基本面'（…的经营基本面与…）、'所有关键'（所有关键数据）、
    # '新能源'（LG新能源被截出后半段）。这些词不可能作为上市公司名主体。
    "基本面", "关键", "格局", "图表", "因素", "水平", "差异",
    "影响", "程度", "份额", "建议", "内容", "信息", "风险",
    "环境", "范围", "方向", "进展", "前景", "空间", "策略",
    "方案", "路径", "挑战", "机遇", "逻辑", "价值", "领域",
)

# 显式股票代码模式：'宁德时代（CATL, 300750.SZ）' / '腾讯(00700.HK)' /
# 'Apple (AAPL)' / '宁德时代（300750.SZ）' —— 括号内的代码是权威锚点，
# 其前的中文名直接可信（动词前缀由 _GENERIC_WORDS 剥离，如"调研宁德时代"）。
_STOCK_CODE_RE = re.compile(
    rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,10}}?)\s*[（(]\s*"
    r"[A-Za-z]{0,8}(?:\s*,\s*[A-Za-z]{0,4}\s*)?\s*\d{4,6}\.?"
    r"(?:SZ|SH|HK|US)?\s*[）)]"
)
# 反向写法：'600519（贵州茅台）' —— 代码在前、括号内是名称（同样是权威锚点）
_STOCK_CODE_FIRST_RE = re.compile(
    r"\d{4,6}\.?(?:SZ|SH|HK|US)?\s*[（(]\s*([\u4e00-\u9fff]{2,10})\s*[）)]",
    re.I,
)

# ── 英文公司名 / 美股代码（MKT-P0-3）──────────────────────────────────────────
# 此前公司名只认中文（`_looks_like_company` 要求 2-6 个汉字），于是英文/代码驱动的
# 目标（"分析 WBD 的财报"、"AAPL 营收"）提取不到主体 → `route_structured` 返回 None
# → **SEC EDGAR 链路永远不触发**（工作区里也就没有 financials.json）。
_EN_NAME_ANCHOR_RE = re.compile(
    r"(?<![A-Za-z])([A-Z][A-Za-z&.\-]{1,30}(?:\s+[A-Z][A-Za-z&.\-]{1,20}){0,3})\s*[（(]\s*"
    r"(?:NASDAQ|NYSE|AMEX|NASDAQGS)?[:\s]*([A-Za-z]{1,5})\s*[）)]"
)
_EN_TICKER_COLON_RE = re.compile(
    r"(?<![A-Za-z])([A-Z]{1,5})\s*[:：]\s*(?:US|NASDAQ|NYSE|AMEX)\b")
_EN_TICKER_RE = re.compile(r"(?<![A-Za-z])([A-Z]{1,5})(?![A-Za-z])")
# 不是代码的大写词（机构/指标/期间/常见词），命中即排除
_EN_TICKER_STOPWORDS = {
    "AI", "CEO", "CFO", "CTO", "IPO", "ETF", "GDP", "CPI", "PPI", "PMI", "USD",
    "CNY", "HKD", "EPS", "PE", "PB", "ROE", "ROA", "ROI", "YOY", "QOQ", "ESG",
    "US", "USA", "UK", "HK", "CN", "SEC", "FDA", "FED", "IRS", "NYSE", "NASDAQ",
    "AMEX", "GAAP", "IFRS", "YTD", "TTM", "EBIT", "EBITDA", "FCF", "DCF", "WACC",
    "Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY", "A", "I", "AND", "OR", "THE", "FOR",
    "WITH", "VS", "REPORT", "ANALYSIS", "STOCK", "SHARE", "MARKET", "REVENUE",
}
# 金融语境词：裸代码/英文公司名必须出现在这些词的邻域内才认（否则 "AI"/"CEO" 之类会误判）
_EN_FIN_CONTEXT = (
    "财报", "年报", "季报", "营收", "收入", "净利润", "利润", "业绩", "毛利率",
    "现金流", "每股收益", "财务", "股价", "市值", "美股", "revenue", "earnings",
    "financial", "financials", "10-k", "10-q", "income", "profit", "margin",
    "cash flow", "eps", "stock", "share price", "market cap", "fiscal",
)
_EN_NAME_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "annual", "quarterly", "report",
    "analysis", "stock", "share", "market", "revenue", "earnings", "financial",
    "q1", "q2", "q3", "q4", "fiscal", "year", "latest", "sec", "edgar", "us",
}
# 英文请求动词/前缀：'Analyze Apple's revenue' 里的 Analyze 不是公司名的一部分
_EN_LEADING_VERBS = {
    "analyze", "analyse", "review", "research", "study", "summarize", "summarise",
    "compare", "evaluate", "assess", "check", "explain", "tell", "give", "show",
    "please", "about", "on", "for", "read", "look", "find", "get", "write",
}
# 中文名 + 纯字母代码：'微软（MSFT）' / '苹果(AAPL)' —— 与数字代码锚点同等权威
_CN_NAME_TICKER_RE = re.compile(
    r"(?:%s)*([\u4e00-\u9fff]{2,6})\s*[（(]\s*[A-Za-z]{1,5}\s*[）)]" % _GENERIC_WORDS
)
# 期间与泛称残片：这些串能通过"2-6 个汉字"的字面检查，却是提取噪声
_NON_COMPANY_FRAGMENTS = (
    "一期", "二期", "三期", "本期", "上期", "当期", "各期", "期间", "年内",
    "季度", "全年", "上半年", "下半年", "股市", "个股", "股权",
    "美股", "港股", "A股", "中概", "行业", "板块", "赛道", "概念", "指数",
    "市场", "标的", "头部", "同行", "竞品", "科技", "医药", "消费",
    "新能源", "半导体", "互联网", "地产", "汽车", "白酒",
)


def _has_en_fin_context(text: str, start: int, end: int, window: int = 40) -> bool:
    """英文候选是否处在金融语境里（前/后 window 字内出现语境词）。"""
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    blob = text[lo:hi].lower()
    return any(w.lower() in blob for w in _EN_FIN_CONTEXT)


def _extract_en_subject(g: str) -> str:
    """英文公司名或美股代码提取（只在中文路径无果时兜底）。

    `resolve_company` 对两者都能解析（美股代码本身就是 ticker）。
    只在金融语境内认，避免把 'AI'/'CEO'/'IPO' 之类当成公司。
    """
    text = str(g or "")
    if not text:
        return ""
    m = _EN_NAME_ANCHOR_RE.search(text)
    if m:
        name = (m.group(1) or "").strip()
        if name:
            return name
        ticker = (m.group(2) or "").strip().upper()
        if ticker and ticker not in _EN_TICKER_STOPWORDS:
            return ticker
    m = _EN_TICKER_COLON_RE.search(text)
    if m:
        ticker = m.group(1).upper()
        if ticker not in _EN_TICKER_STOPWORDS:
            return ticker
    for m in _EN_TICKER_RE.finditer(text):
        tok = m.group(1).upper()
        if tok in _EN_TICKER_STOPWORDS:
            continue
        if not _has_en_fin_context(text, m.start(1), m.end(1)):
            continue
        return tok
    for m in re.finditer(
            r"(?<![A-Za-z])([A-Z][A-Za-z&.\-]{1,20}(?:\s+[A-Z][A-Za-z&.\-]{1,20}){0,3})\b",
            text):
        # 去掉前导请求动词（"Analyze Apple" → "Apple"）
        words = [w for w in m.group(1).strip().split()
                 if w.lower() not in _EN_LEADING_VERBS]
        name = " ".join(words)
        words = name.split()
        if not words or all(w.lower() in _EN_NAME_STOPWORDS for w in words):
            continue
        if len(words) == 1 and words[0].upper() in _EN_TICKER_STOPWORDS:
            continue
        if not _has_en_fin_context(text, m.start(1), m.end(1)):
            continue
        return name
    return ""


def _extract_company(g: str) -> str:
    """提取公司名：优先显式股票代码（'X（CODE）' 锚点）、'X集团/控股'、
    'X公司'，其次"公司名紧接报告期短语"，最后退回"财报/营收"等前词。

    报告期短语单独成一条模式的原因（实测 bug）：旧模式的关键词里含"季报/年报"，
    于是"2025年三季报"被切成 `年三` + `季报`（"研究宁德时代三季报"则得到
    `宁德时代三`）。公司名提取错 → `resolve_company` 失败 → `route_structured`
    返回 None → **结构化财务链路从不命中**，报告只能靠搜索拼口径。
    """
    m = _STOCK_CODE_RE.search(g)
    if m:
        return m.group(1)
    m = _CN_NAME_TICKER_RE.search(g)
    if m:
        cand = _trim_context(m.group(1))
        if cand and _looks_like_company(cand):
            return cand
    m = _STOCK_CODE_FIRST_RE.search(g)
    if m:
        return m.group(1)
    for pat in (
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}})(?:集团|控股)",
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}})公司",
    ):
        # 全部匹配逐个过公司名校验：语境词前的垃圾片段（如"评估其财务"
        # 的"评估其"）跳过继续找，真公司名（"贵州茅台近三年营收"）命中即返回。
        # 捕获串同样要过 _trim_context：请求语不在 _GENERIC_WORDS 里时
        # （"说说腾讯控股的年报"）会原样留下"说说腾讯"。
        for m in re.finditer(pat, g):
            c = _trim_context(m.group(1))
            if len(c) < 2 or not _looks_like_company(c):
                continue
            # 报告期残片守卫：捕获串含报告期词一律不认（防"年三""宁德时代三"）
            if any(w in c for w in _REPORT_PERIOD_WORDS):
                continue
            return c
    c = _company_before_period(g)
    if c:
        return c
    pat = (
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}}?)(?:的)?"
        r"(?:历年年度|历年|年度|最新|近三年|近五年|最近"
        r"|20\d{2}(?:\s*[-—至]\s*20\d{2})?\s*年?)?"
        r"(?:财报|年报|季报|财务|营收|净利润|净利|利润|收入|业绩|负债|研发投入)"
    )
    for m in re.finditer(pat, g):
        c = _trim_context(m.group(1))
        if len(c) < 2 or not _looks_like_company(c):
            continue
        if any(w in c for w in _REPORT_PERIOD_WORDS):
            continue
        return c
    # 中文路径全部无果时，兜底认英文公司名/美股代码（MKT-P0-3；中文行为不变）
    return _extract_en_subject(g)


# 报告期/财报语境短语：允许带年份（"2025年三季报"），用于定位紧邻其前的公司名。
# 含"财报/财务"这类非报告期词：它们同样是"公司名紧跟其后"的语境（"…茅台的财报"）。
_PERIOD_PHRASE_RE = re.compile(
    r"(?:20\d{2}\s*年?)?\s*"
    r"(?:年度报告|半年度报告|中期报告|一季报|三季报|半年报|中报|年度|年报|季报"
    r"|财报|财务)"
)

# 公司名左侧可能粘着的语境词（剥离用）。取"通用语境词 + 公司名停用词 + 常见请求语"，
# 长词优先匹配，保证"梳理/分析"整体剥掉而不是留下单字伪名。
_CTX_TOKENS = tuple(sorted(
    set(_GENERIC_WORDS.split("|")) | set(_COMPANY_STOPWORDS)
    | {"看看", "看", "我要", "我想", "想要", "给我", "帮我", "帮我看看", "麻烦",
       "帮忙", "请", "阅读", "查看", "查一下", "看一下", "讲一下", "说说",
       "能否", "可以", "读一下", "浏览",
       # 请求语补充（"请给微软（MSFT）…" 曾留下"给微软"）
       "给", "请给", "请给我", "请帮我", "请帮忙", "帮我查", "帮我查一下",
       "帮我分析", "帮我梳理", "帮我看看", "麻烦帮我", "麻烦给"},
    key=len, reverse=True,
))

# 紧邻报告期短语的中文窗口（比公司名上限 6 宽，留出语境词的空间）与尾部虚词
_PERIOD_TAIL_RE = re.compile(r"[\u4e00-\u9fff]{2,12}$")
_TRAIL_PARTICLE_RE = re.compile(r"[的了吗呢啊呀吧]+$")
# 叠字请求语（"瞧瞧/说说/瞅瞅"）：语境词表不可能穷举，用叠字规律兜底
_REDUP_PREFIX_RE = re.compile(r"^([\u4e00-\u9fff])\1")


def _company_before_period(g: str) -> str:
    """取紧贴报告期短语之前的公司名。

    不能用 `([\\u4e00-\u9fff]{2,6})(?=报告期短语)` 这种"从头捕获"的写法：
    它会在最早位置吞掉整段（"我要贵州茅台"），校验失败后 `finditer` 已越过
    正确起点，正确公司名再也匹配不到——实测"我要贵州茅台2025年三季报…"
    返回空、整条结构化财务链路失效。

    也不能"从长到短试后缀"：截短会切出"理贵州茅台"（"梳理"的尾巴）这类
    看似合法的片段，而校验只看整词存在与否，反而比真名更容易通过。

    最终做法：短语前取一段中文窗口 → 剥尾部虚词（"茅台的三季报"的"的"）
    → 剥/裁左侧语境词 → 仍超长则取尾段 → 校验。
    """
    for m in _PERIOD_PHRASE_RE.finditer(g):
        window = _TRAIL_PARTICLE_RE.sub("", g[max(0, m.start() - 12):m.start()])
        tail = _PERIOD_TAIL_RE.search(window)
        if not tail:
            continue
        cand = _trim_context(tail.group(0))
        if cand:
            return cand
    return ""


def _trim_context(seg: str) -> str:
    """把中文窗口裁成公司名：剥左侧语境词、裁到语境词之后、超长取尾段。

    刻意不做"逐字左滑"：窗口若以虚词结尾（"看看茅台的"），逐字削会一路把真名
    啃到只剩单字并返回空——实测"看看茅台的三季报""帮我看看茅台的财报"
    "给我茅台的三季报营收"都因此取不到公司名（尾部虚词改为先行剥除）。
    """
    changed = True
    while changed and len(seg) > 1:
        changed = False
        for tok in _CTX_TOKENS:
            if seg.startswith(tok) and len(seg) > len(tok):
                seg = seg[len(tok):]
                changed = True
                break
        if changed:
            continue
        m = _REDUP_PREFIX_RE.match(seg)
        if m and len(seg) > 2:
            seg = seg[2:]
            continue
        # 语境词不在开头（"我看看茅台"）：整体裁到它之后，而不是逐字左滑
        pos, hit = min(
            ((seg.find(t), t) for t in _CTX_TOKENS if seg.find(t) > 0),
            default=(-1, ""),
        )
        if pos > 0:
            seg = seg[pos + len(hit):]
            changed = True
    if len(seg) < 2:
        return ""
    if len(seg) > 6:
        # 未知前缀挤占窗口：中文公司名不超过 6 字，取尾段（前缀会由
        # resolve_company 证伪，最多退化为搜索兜底，不会产生错误数据）
        seg = seg[-6:]
    if any(t in seg for t in _CTX_TOKENS):
        return ""
    if any(w in seg for w in _REPORT_PERIOD_WORDS):
        return ""
    return seg if _looks_like_company(seg) else ""


# 报告期词：出现在"公司名候选"里说明切分切错了（如"年三"/"宁德时代三"）

_REPORT_PERIOD_WORDS = (
    "季报", "年报", "半年报", "中报", "年度", "半年", "年三", "年半", "年一",
    "一季", "三季", "二季", "四季度", "单季",
)


def _looks_like_company(name: str) -> bool:
    """公司名候选的非递归校验（供 _extract_company 使用）：
    2-6 个中文字、无连接符/语境词/量词短语。不含"_extract_company 兜底"
    递归分支——那会与调用方形成互递归。"""
    name = str(name or "")
    if not re.fullmatch(r"[一-鿿]{2,6}", name):
        return False
    if any(w in name for w in _COMPANY_STOPWORDS):
        return False
    # 期间/泛称残片（"一期"、"美股科技"）不是公司名：它们能过"2-6 个汉字"的字面检查
    if any(w in name for w in _NON_COMPANY_FRAGMENTS):
        return False
    if re.search(r"(?:与|和|及|以及|、|跟|vs)", name, re.I):
        return False
    if name in ("公司", "集团", "控股"):
        return False
    # 量词+泛指后缀不是公司名（"两家公司/三家集团"）
    if re.fullmatch(r"[两三四五六七八九十几\d一二]+(?:家|个)?(?:公司|集团|控股|企业|厂商|巨头|主体)", name):
        return False
    return True


def _is_valid_company_name(name: str) -> bool:
    """公司名候选校验：非递归基础校验 + 末尾带 '公司/集团/控股' 等合法
    后缀，或可被现有单实体提取规则认可（递归分支仅此一处，有终结）。"""
    name = str(name or "")
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,6}", name):
        return False
    if any(w in name for w in _COMPANY_STOPWORDS):
        return False
    if re.search(r"(?:与|和|及|以及|、|跟|vs)", name, re.I):
        return False
    if name in ("公司", "集团", "控股"):
        return False
    # 量词+泛指后缀不是公司名（"两家公司/三家集团"来自"对比两家公司近三年营收"）
    if re.fullmatch(r"[两三四五六七八九十几\d一二]+(?:家|个)?(?:公司|集团|控股|企业|厂商|巨头|主体)", name):
        return False
    if name.endswith(("公司", "集团", "控股")):
        return True
    # 复用现有单实体提取规则兜底（“X的财报”应恰好还原 X）
    return _extract_company(f"{name}的财报") == name


def _extract_companies(g: str) -> list[str]:
    """多实体对比目标提取：仅当出现对比触发词（对比/比较/vs/分别/相比）
    时才按连接符拆分，拆分出的每个名称过 _is_valid_company_name 校验。
    返回按出现顺序去重后的公司名列表；非对比目标返回 []。"""
    g = str(g or "")
    if not _COMPARE_TRIGGER_RE.search(g):
        return []
    # 显式股票代码是权威锚点：命中即只信它（如 '与LG新能源…对比' 这类
    # 子串歧义句，代码模式把目标钉死在 '宁德时代'，不再拆出垃圾片段）
    coded = [
        m.group(1) for m in _STOCK_CODE_RE.finditer(g)
        if _is_valid_company_name(m.group(1))
    ]
    if coded:
        return list(dict.fromkeys(coded))
    names: list[str] = []
    seen: set[str] = set()
    for pattern in (
        _BEFORE_CONNECTOR_RE, _AFTER_CONNECTOR_RE, _BEFORE_FINANCIAL_RE,
    ):
        for m in pattern.finditer(g):
            name = m.group(1)
            if name in seen or not _is_valid_company_name(name):
                continue
            # 报告期残片守卫（与 _extract_company 同款）：'比亚迪三' 不得当公司名
            if any(word in name for word in _REPORT_PERIOD_WORDS):
                continue
            seen.add(name)
            names.append(name)
    return names


def classify_task(goal: str) -> dict:
    """分类任务：返回 {domain, company, year_range, market, companies}。

    companies 为多实体对比目标拆分出的公司名列表（纯提取，不做解析）；
    单实体目标为空列表或仅含原 company，行为保持不变。
    """
    g = str(goal or "")
    gl = g.lower()
    domain = "financial" if any(k in gl for k in FINANCIAL_KEYWORDS) else "general"
    company = _extract_company(g)
    years = [int(y) for y in re.findall(r"(20\d{2})", g)]
    year_range = (min(years), max(years)) if years else None
    market = next(
        (mk for mk, kws in _MARKET_HINTS.items() if any(k in gl for k in kws)),
        None,
    )
    return {
        "domain": domain,
        "company": company,
        "year_range": year_range,
        "market": market,
        "companies": _extract_companies(g),
    }
