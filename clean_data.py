# -*- coding: utf-8 -*-
"""数据清洗与结构化（对标用户建议：搜索 → 清洗/结构化 → 绘图）。

把 search_results.json 的杂乱文本清洗为 clean_chart_data.json（"广谱扫描"）：
- entity_frequency   ：仅统计【提到核心主题】的文档中的目标实体；
                       同一实体的中英文/别名合并到规范名（如 NVIDIA/NV → 英伟达）
- market_data        ：市场规模/销售额类（货币单位，含 推理/训练/边缘/逻辑芯片 等分项）
- market_share       ：市场份额/占比类（%，如 "GPU占有超过80%的市场份额"）
- macro_indicators   ：宏观指标类（非货币单位，如 "总调用量为46.7万亿Token"）
- market_trends      ：市场趋势类（同比/环比 ±%，如 "出货量预计同比下降7%"）
- notes              ：结构完整但数值缺失/被截断的事实（不绘图，仅保留线索）
- source_distribution：域名聚合计数（全 1 时由绘图层跳过）
- topic_terms        ：jieba 分词 + 词性/停用词/碎片过滤后的热词
                       （不使用滑动窗口，杜绝"辑芯片市""伟达和"式碎片）
"""

import json
import re
from collections import Counter
from pathlib import Path

try:
    import jieba
    import jieba.posseg as pseg
    HAVE_JIEBA = True
except Exception:  # 依赖缺失时走保守回退（jieba 已列入 requirements.txt）
    HAVE_JIEBA = False

# 实体规范名 → 别名（合并计数，避免 NVIDIA/NV 漏计或与"英伟达"分裂成两个条目）
ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "英伟达": ("英伟达", "NVIDIA", "Nvidia", "nvidia", "NV"),
    "AMD": ("AMD", "amd", "超威"),
    "英特尔": ("英特尔", "Intel", "intel", "因特尔"),
    "谷歌": ("谷歌", "Google", "google"),
    "华为": ("华为", "Huawei", "huawei"),
    "昇腾": ("昇腾", "Ascend", "ascend"),
    "高通": ("高通", "Qualcomm", "qualcomm"),
    "寒武纪": ("寒武纪", "Cambricon", "cambricon"),
    "海光": ("海光", "Hygon", "hygon"),
    "亚马逊": ("亚马逊", "Amazon", "amazon", "AWS", "aws"),
    "微软": ("微软", "Microsoft", "microsoft"),
    "Meta": ("Meta", "meta"),
    "台积电": ("台积电", "TSMC", "tsmc"),
    "赛灵思": ("赛灵思", "Xilinx", "xilinx"),
    "博通": ("博通", "Broadcom", "broadcom"),
    "三星": ("三星", "Samsung", "samsung"),
    "苹果": ("苹果", "Apple", "apple"),
    "Cerebras": ("Cerebras", "cerebras"),
    "Graphcore": ("Graphcore", "graphcore"),
    "美国": ("美国", "美方"),
    "中国": ("中国", "国内"),
    "欧洲": ("欧洲", "欧盟"),
    "日本": ("日本",),
    "亚太": ("亚太",),
}

THEME_ANCHOR = re.compile(
    r"芯片|AI|算力|推理|训练|NVIDIA|英伟达|GPU|ASIC|FPGA|加速卡|"
    r"半导体|Token|大模型|数据中心", re.I,
)

# 已知分项的精确市场数据规则：<分项>…<数字>亿元/亿美元（支持千分位逗号与"约"前缀）。
# 保留它们以稳定输出 推理/训练/边缘/逻辑芯片 等细分市场的柱状图。
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
SPECIFIC_MARKET_PATTERNS = [
    ("推理芯片", re.compile(r"推理芯片[^。；;]*?" + _NUM + r"\s*(?:亿|万)美元", re.I)),
    ("训练芯片", re.compile(r"训练芯片[^。；;]*?" + _NUM + r"\s*(?:亿|万)美元", re.I)),
    ("边缘AI芯片", re.compile(r"边缘(?:AI)?芯片[^。；;]*?" + _NUM + r"\s*(?:亿|万)美元", re.I)),
    ("逻辑芯片市场规模", re.compile(r"逻辑芯片市场规模[^。；;]*?" + _NUM + r"\s*亿元", re.I)),
]

# ── 广谱扫描规则（按句子逐条匹配，覆盖 市场规模/份额/宏观指标/趋势 四类）──
_MONEY_UNIT = r"(?:万亿美元|万亿元|亿美元|亿元|万美元|万元)"
_REGIONS = ("全球", "中国", "美国", "欧洲", "亚太", "日本", "国内", "国际", "中国大陆", "华东")
_SIZE_KEYWORDS = ("市场规模", "销售额", "销售规模", "营收", "收入")
_SHARE_PATTERNS = (
    re.compile(
        r"(?P<subject>[\w\u4e00-\u9fff]{1,12}?)(?:占据|占有|占)"
        r"(?:超过|约|接近|高达)?(?P<value>\d+(?:\.\d+)?)%(?:的)?(?:市场)?份额",
    ),
    re.compile(
        r"(?P<subject>[\w\u4e00-\u9fff]{1,12}?)(?:市场份额|市场占比|占比)"
        r"(?:约|约为|将达|达到)?(?P<value>\d+(?:\.\d+)?)%",
    ),
)
_MACRO = re.compile(
    r"(?P<theme>[\w\u4e00-\u9fff]{2,16}?(?:调用量|出货量|装机量|销量|产量|渗透率))"
    r"[^。；;]{0,12}?(?:为|达到|达|约|约为)?"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>万亿|亿|万)?"
    r"(?P<unit2>Token|token|TOKEN|台|辆|次|个|千瓦时|兆瓦|美元|元)?",
)
_TREND = re.compile(
    r"(?P<theme>[\w\u4e00-\u9fff]{2,16}?(?:出货量|销售额|收入|销量|营收|规模|增速|渗透率))"
    r"[^。；;]{0,14}?(?:同比|环比)?(?:预计|将|或)?"
    r"(?P<dir>下降|下滑|回落|减少|增长|上升|上涨|提升|走高)?(?P<num>\d+(?:\.\d+)?)%",
)
_TREND_PARTIAL = re.compile(r"(?:同比|环比|较上年|较上季|将|预计).{0,12}?(?:增长|下降|下滑|回落)")


def _norm_sentence(s: str) -> str:
    """去掉 CJK/字母数字之间的空格（PDF 摘要有"占 有 超 过80%"这类断词）。"""
    return re.sub(
        r"(?<=[\u4e00-\u9fffA-Za-z0-9%]) (?=[\u4e00-\u9fffA-Za-z0-9%])",
        "", s,
    )


def _sentences(text: str) -> list[str]:
    # 句点只在"前后不是数字"时视为句子边界，避免切断 5,757.5 的小数点或 v2.5 版本号
    parts = re.split(r"[。．；;\n]+|(?<![\d])\.(?![\d])", text)
    return [s.strip() for s in parts if s.strip()]


def _market_size_row(sent: str, url: str):
    """市场规模/销售额：年份+地域+主题+数值+货币单位；数值缺失时记 notes。"""
    kw = None
    for k in _SIZE_KEYWORDS:
        p = sent.find(k)
        if p >= 0 and (kw is None or p < kw):
            kw = p
    if kw is None:
        return None, None
    m = re.search(r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>" + _MONEY_UNIT + r")", sent)
    if not m:
        # 仅"市场规模/销售额"类缺失数值才算截断；"收入…两位数增长"由趋势规则记录
        if re.search(r"(市场规模|销售额|销售规模)", sent) and re.search(
            r"(约为|约|达|达到|有望|接近)", sent,
        ):
            return None, {
                "type": "market_size",
                "text": sent[:160],
                "reason": "结构完整但数值缺失/在摘要中被截断",
                "source": url,
            }
        return None, None
    year_m = re.search(r"(20\d{2})年?", sent[:kw])
    region = next((r for r in _REGIONS if r in sent[:kw]), None)
    pre = re.sub(r"20\d{2}年?", "", sent[:kw])
    if region:
        pre = pre.replace(region, "")
    pre = re.sub(r"(预计|预测|将|会|到|达|约|的|其|总|全|在|，|,)", "", pre)
    pre = re.sub(r"^\d+(?:\.|、)?\s*", "", pre)  # 去掉 "03. " 这类编号前缀
    theme = pre.strip()[-12:] or "市场规模"
    return {
        "type": "market_size",
        "label": theme,
        "value": float(m.group("num").replace(",", "")),
        "unit": m.group("unit"),
        "year": int(year_m.group(1)) if year_m else None,
        "region": region,
        "source": url,
    }, None


def _share_rows(sent: str, url: str):
    """市场份额/占比：主体+%+份额。"""
    rows = []
    for pat in _SHARE_PATTERNS:
        for m in pat.finditer(sent):
            subj = m.group("subject").strip()
            # 主体只取最后一个有信息量的片段：去掉 "在中国AI芯片市场GPU" 里的地域/市场前缀
            for sep in ("市场", "中国", "全球", "美国", "欧洲", "日本", "亚太"):
                parts = subj.split(sep)
                if len(parts) > 1:
                    subj = parts[-1]
            subj = subj.lstrip("与和及、")
            rows.append({
                "type": "market_share",
                "label": subj or m.group("subject").strip(),
                "value": float(m.group("value")),
                "unit": "%",
                "source": url,
            })
    return rows


def _macro_rows(sent: str, url: str):
    """宏观指标：调用量/出货量/渗透率 + 数值 + 非货币单位（Token 等）。"""
    rows = []
    for m in _MACRO.finditer(sent):
        unit = (m.group("unit") or "") + (m.group("unit2") or "")
        if not unit:
            continue
        rows.append({
            "type": "macro_indicator",
            "label": _clean_theme_label(m.group("theme")),
            "value": float(m.group("num").replace(",", "")),
            "unit": unit,
            "source": url,
        })
    return rows


def _trend_rows(sent: str, url: str, notes: list[dict]):
    """市场趋势：同比/环比 ±%。下降类取负值；无数值的趋势描述记 notes。"""
    rows = []
    spans: list[tuple[int, int]] = []
    for m in _TREND.finditer(sent):
        spans.append((m.start(), m.end()))
        v = float(m.group("num"))
        if m.group("dir") in ("下降", "下滑", "回落", "减少"):
            v = -v
        rows.append({
            "type": "market_trend",
            "label": _clean_theme_label(m.group("theme")),
            "value": v,
            "unit": "%",
            "source": url,
        })
    # 未覆盖的趋势描述（如"两位数增长"）即使本句已有数值趋势，也要单独记 note
    for pm in _TREND_PARTIAL.finditer(sent):
        if any(pm.start() >= s and pm.end() <= e for s, e in spans):
            continue
        notes.append({
            "type": "market_trend",
            "text": sent[max(0, pm.start() - 12):pm.end() + 20][:160],
            "reason": "趋势描述但无具体百分比（如'两位数增长'）",
            "source": url,
        })
    return rows


def _clean_theme_label(t: str) -> str:
    """去掉标签里的年份/地域/冗余虚字（"2026年全球手机芯片总"→"手机芯片"）。"""
    t = re.sub(r"20\d{2}年?", "", t)
    for r in _REGIONS:
        t = t.replace(r, "")
    t = re.sub(r"^(尽管|虽然|然而|但是|但|而|其中|预计|预测|将|或|且|并|也|还|仍)", "", t)
    t = t.rstrip("的总全")
    return t or "指标"

# 停用词：通用虚词 + 主题级噪音（芯片/市场/AI 几乎每篇都有，信息量低）
# + 网页/文档标题垃圾词（"PowerPoint 演示文稿"这类标题不该成为热词）
STOPWORDS = {
    "一个", "我们", "以及", "可以", "没有", "已经", "进行", "通过", "对于",
    "不是", "就是", "同时", "如果", "因为", "所以", "但是", "这些", "那些",
    "其中", "主要", "相关", "关于", "根据", "报告", "分析", "本次", "该",
    "全球", "中国", "市场", "行业", "产业", "发展", "增长", "技术",
    "应用", "领域", "数据", "信息", "公司", "企业", "方面", "预计",
    "成为", "带来", "推动", "驱动", "规模", "目前", "未来", "有望",
    "万亿美元", "亿美元", "万亿元", "万亿", "亿元", "存储", "存储芯片", "半导体",
    # 主题级噪音：几乎每篇必出现，无区分度
    "芯片", "ai", "gpu", "cpu", "算力", "大模型", "人工智能", "智能",
    "模型", "训练", "推理", "加速", "厂商", "产品", "设备", "方案",
    # 网页/文档标题垃圾
    "演示文稿", "演示", "文稿", "powerpoint", "ppt", "pdf", "下载", "全文", "免费",
    "官网", "首页", "网页", "搜索", "搜索引擎", "注册", "登录", "上传",
    "文档", "查看", "更多", "来源", "返回", "欢迎", "版权", "声明",
    "免责", "隐私", "条款", "快讯", "日报", "专栏", "专题", "频道",
}

# 会出现在碎片里的单字功能词（"市场被英""伟达和"等碎片含这些字 → 丢弃）
_FRAGMENT_CHARS = set(
    "和被由的吗了是在与及或等为对把将给让这那它其之以而于就都也又再只"
    "从到向跟叫使令个中上里后时年月份日天每各该某此何如若则却且但"
)


def _count_alias(text: str, alias: str) -> int:
    """统计别名出现次数：纯英文别名按词边界匹配（避免 NV 命中 INVEST）。"""
    if re.fullmatch(r"[A-Za-z]{2,}", alias):
        return len(re.findall(
            r"(?<![A-Za-z0-9])" + re.escape(alias) + r"(?![A-Za-z0-9])",
            text, re.I,
        ))
    return text.count(alias)


def _tokenize(text: str) -> list[str]:
    """jieba 分词 + 词性过滤；缺依赖时走保守 bigram 回退。"""
    if HAVE_JIEBA:
        out = []
        for w, flag in pseg.cut(text):
            w = w.strip()
            if not w:
                continue
            if not _pos_ok(flag):
                continue
            if _valid_token(w):
                out.append(w)
        return out
    # 回退：2 字 bigram + 功能字/停用词过滤（无滑动窗口 4 字碎片）
    out = []
    for m in re.finditer(r"[\u4e00-\u9fff]{2}", text):
        w = m.group(0)
        if _valid_token(w) and not any(c in _FRAGMENT_CHARS for c in w):
            out.append(w)
    return out


def _pos_ok(flag: str) -> bool:
    """只保留名词类/动名词/英文词/简称，过滤动词、形容词、虚词等干扰项。"""
    return flag.startswith(("n", "vn", "eng", "j", "l", "nz", "nr", "ns", "nt"))


def _valid_token(w: str) -> bool:
    """热词合法性：非纯数字、长度达标、不在停用词表。
    碎片功能字过滤只用于回退 bigram 路径（jieba 词典分词无滑动窗口碎片）。"""
    if not w:
        return False
    wl = w.lower()
    if wl in STOPWORDS:
        return False
    if re.fullmatch(r"[\d.,%]+", w):
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", w))
    ascii_len = len(re.findall(r"[A-Za-z0-9]", w))
    if cjk and len(w) < 2:
        return False
    if ascii_len and ascii_len < 2 and not cjk:
        return False
    return True


def _docs(items: list[dict]) -> list[dict]:
    return [d for d in items if isinstance(d, dict)]


def _doc_text(d: dict) -> str:
    return str(d.get("title") or "") + " " + str(d.get("snippet") or "")


def clean_and_structure(items: list[dict]) -> dict:
    """把原始检索结果清洗为结构化图表数据（广谱扫描四类规则 + 截断记录）。"""
    docs = _docs(items)
    entity_freq = Counter()
    market: list[dict] = []
    shares: list[dict] = []
    macros: list[dict] = []
    trends: list[dict] = []
    notes: list[dict] = []
    for d in docs:
        text = _doc_text(d)
        if not THEME_ANCHOR.search(text):
            continue
        url = str(d.get("url") or "")
        for ent, aliases in ENTITY_ALIASES.items():
            # 别名按小写去重：避免 "AMD"+"amd" 在同一条文本里被计两次
            seen: set[str] = set()
            n = 0
            for a in aliases:
                key = a.lower()
                if key in seen:
                    continue
                seen.add(key)
                n += _count_alias(text, a)
            if n:
                entity_freq[ent] += n
        # 已知分项的精确规则（推理/训练/边缘/逻辑芯片…亿美元/亿元）
        for label, pat in SPECIFIC_MARKET_PATTERNS:
            m = pat.search(text)
            if m:
                try:
                    val = float(m.group(1).replace(",", ""))
                    unit = "亿元" if "亿元" in m.group(0) else "亿美元"
                    market.append({
                        "type": "market_size", "label": label, "value": val,
                        "unit": unit, "source": url,
                    })
                except (ValueError, TypeError):
                    pass
        # 广谱扫描：按句子逐条规则
        for sent in _sentences(text):
            ns = _norm_sentence(sent)
            if not THEME_ANCHOR.search(ns):
                continue
            row, note = _market_size_row(ns, url)
            if row:
                market.append(row)
            if note:
                notes.append(note)
            shares.extend(_share_rows(ns, url))
            macros.extend(_macro_rows(ns, url))
            trends.extend(_trend_rows(ns, url, notes))
    market = _dedupe_rows(market)
    shares = _dedupe_rows(shares)
    macros = _dedupe_rows(macros)
    trends = _dedupe_rows(trends)
    notes = _dedupe_notes(notes)
    domains = Counter()
    for d in items:
        if not isinstance(d, dict):
            continue
        m = re.match(r"https?://([^/]+)", str(d.get("url") or ""))
        if m:
            domains[m.group(1).replace("www.", "")] += 1
    topic_terms = Counter()
    for d in docs:
        text = _doc_text(d)
        if not THEME_ANCHOR.search(text):
            continue
        for w in _tokenize(text):
            topic_terms[w] += 1
    return {
        "entity_frequency": dict(entity_freq.most_common(12)),
        "market_data": market,
        "market_share": shares,
        "macro_indicators": macros,
        "market_trends": trends,
        "notes": notes,
        "source_distribution": dict(domains.most_common(12)),
        "topic_terms": dict(topic_terms.most_common(12)),
    }


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    """精确去重 + 标签包含去重：同值同单位同源、标签互相包含时保留更具体（带年份）的一条。"""
    out: list[dict] = []
    for r in rows:
        dup = False
        for o in out:
            same = (
                o.get("value") == r.get("value")
                and o.get("unit") == r.get("unit")
                and o.get("source") == r.get("source")
                and (o.get("year") == r.get("year")
                     or o.get("year") is None or r.get("year") is None)
            )
            if not same:
                continue
            ol, rl = str(o.get("label") or ""), str(r.get("label") or "")
            if ol and rl and (ol in rl or rl in ol):
                # 保留更长的标签，同时补齐年份/地域
                if len(rl) > len(ol):
                    o["label"] = rl
                if r.get("year") and not o.get("year"):
                    o["year"] = r.get("year")
                if r.get("region") and not o.get("region"):
                    o["region"] = r.get("region")
                dup = True
                break
        if not dup:
            out.append(r)
    return out


def _dedupe_notes(notes: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for n in notes:
        key = str(n.get("text"))[:80]
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def clean_file(search_path, out_path=None) -> Path:
    """读取 search_results.json，写出 clean_chart_data.json，返回输出路径。"""
    sp = Path(search_path)
    op = Path(out_path) if out_path else sp.parent / "clean_chart_data.json"
    try:
        items = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        items = []
    op.write_text(
        json.dumps(clean_and_structure(items if isinstance(items, list) else []),
                    ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return op


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else "search_results.json"
    out = clean_file(src)
    print(f"已生成 {out}")
