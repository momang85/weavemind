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
    m = _STOCK_CODE_FIRST_RE.search(g)
    if m:
        return m.group(1)
    for pat in (
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}})(?:集团|控股)",
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}})公司",
    ):
        # 全部匹配逐个过公司名校验：语境词前的垃圾片段（如"评估其财务"
        # 的"评估其"）跳过继续找，真公司名（"贵州茅台近三年营收"）命中即返回
        for m in re.finditer(pat, g):
            c = m.group(1)
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
        c = m.group(1)
        if len(c) < 2 or not _looks_like_company(c):
            continue
        if any(w in c for w in _REPORT_PERIOD_WORDS):
            continue
        return c
    return ""


# 报告期短语：允许带年份（"2025年三季报"），用于定位紧邻其前的公司名
_PERIOD_PHRASE_RE = re.compile(
    r"(?:20\d{2}\s*年?)?\s*"
    r"(?:年度报告|半年度报告|年度|年报|三季报|一季报|中期报告|半年报|中报|季报)"
)

# 公司名左侧可能粘着的语境词（剥离用）。取"通用语境词 + 公司名停用词 + 常见请求语"，
# 长词优先匹配，保证"梳理/分析"整体剥掉而不是留下单字伪名。
_CTX_TOKENS = tuple(sorted(
    set(_GENERIC_WORDS.split("|")) | set(_COMPANY_STOPWORDS)
    | {"看看", "看", "我要", "我想", "给我", "帮我", "麻烦", "帮忙", "请"},
    key=len, reverse=True,
))


def _company_before_period(g: str) -> str:
    """取紧贴报告期短语之前的公司名。

    不能用 `([\\u4e00-\\u9fff]{2,6})(?=报告期短语)` 这种"从头捕获"的写法：
    它会在最早位置吞掉整段（"我要贵州茅台"），校验失败后 `finditer` 已越过
    正确起点，正确公司名再也匹配不到——实测"我要贵州茅台2025年三季报…"
    返回空、整条结构化财务链路失效。

    也不能"从长到短试后缀"：截短会切出"理贵州茅台"（"梳理"的尾巴）这类
    看似合法的片段，而校验只看整词存在与否，反而比真名更容易通过。
    正确做法是先剥掉左侧语境词，再对剩余整段做校验。
    """
    for m in _PERIOD_PHRASE_RE.finditer(g):
        window = g[max(0, m.start() - 6):m.start()]
        tail = re.search(r"[\u4e00-\u9fff]{2,6}$", window)
        if not tail:
            continue
        cand = _peel_context_prefix(tail.group(0))
        if cand:
            return cand
    return ""


def _peel_context_prefix(seg: str) -> str:
    """剥掉公司名左侧的语境词（"我要贵州茅台"→"贵州茅台"）。

    逐字左滑而非只看开头：语境词可能不在首位（"我要…"的"要"在第 2 字）。
    滑动时优先整词剥离，避免把"梳理"剥成"理"留下"理贵州茅台"这种伪合法名。
    """
    while len(seg) > 1:
        hit = next((t for t in _CTX_TOKENS if seg.startswith(t)), "")
        if hit:
            seg = seg[len(hit):]
            continue
        if any(t in seg for t in _CTX_TOKENS):
            seg = seg[1:]
            continue
        break
    if len(seg) < 2 or any(t in seg for t in _CTX_TOKENS):
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
