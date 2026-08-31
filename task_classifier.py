# -*- coding: utf-8 -*-
"""任务分类器（task → domain + metadata）。

规则先行：识别 financial 任务，提取 公司名/年份范围/市场提示；
非 financial 任务返回 domain="general"，走原链路（搜索→抓取→清洗→报告）。
"""

import re

FINANCIAL_KEYWORDS = (
    "财报", "年报", "季报", "营收", "净利润", "财务", "业绩", "负债",
    "报表", "financial", "revenue", "earnings", "annual report", "income statement",
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

# 连接符后的公司名：名称后紧跟财务/对比语境词或句子结尾。
# 用前瞻限定“近三年营收”“的财报”等合法后缀语境，避免吞入后续普通动词。
_AFTER_CONNECTOR_RE = re.compile(
    r"(?:与|和|及|以及|、|跟|vs)\s*([\u4e00-\u9fff]{2,6}?)"
    r"(?=(?:的)?(?:历年年度|历年|年度|最新|近三年|近五年|最近)?"
    r"(?:财报|年报|季报|财务|营收|净利润|净利|利润|收入|业绩|负债|报表|"
    r"趋势|情况|数据|表现|相比|对比|比较|分别|vs)|$)",
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
    r"(?:的)?(?:历年年度|历年|年度|最新|近三年|近五年|最近)?"
    r"(?:财报|年报|季报|财务|营收|净利润|净利|利润|收入|业绩|负债|报表|"
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
)


def _extract_company(g: str) -> str:
    """提取公司名：优先 'X集团/控股'，其次 'X公司'，再次财报前词。"""
    for pat in (
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}})(?:集团|控股)",
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}})公司",
        rf"(?:{_GENERIC_WORDS})*([\u4e00-\u9fff]{{2,6}}?)(?:的)?"
        r"(?:历年年度|历年|年度|最新|近三年|近五年|最近)?"
        r"(?:财报|年报|季报|财务)",
    ):
        m = re.search(pat, g)
        if not m:
            continue
        c = m.group(1)
        if len(c) >= 2:
            return c
    return ""


def _is_valid_company_name(name: str) -> bool:
    """公司名候选校验：2-6 个中文字、无连接符/语境词，
    末尾带 '公司/集团/控股' 等合法后缀，或可被现有单实体提取规则认可。"""
    name = str(name or "")
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,6}", name):
        return False
    if any(w in name for w in _COMPANY_STOPWORDS):
        return False
    if re.search(r"(?:与|和|及|以及|、|跟|vs)", name, re.I):
        return False
    if name in ("公司", "集团", "控股"):
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
    names: list[str] = []
    seen: set[str] = set()
    for pattern in (
        _BEFORE_CONNECTOR_RE, _AFTER_CONNECTOR_RE, _BEFORE_FINANCIAL_RE,
    ):
        for m in pattern.finditer(g):
            name = m.group(1)
            if name in seen or not _is_valid_company_name(name):
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
