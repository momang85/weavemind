# -*- coding: utf-8 -*-
"""数据清洗与结构化（对标用户建议：搜索 → 清洗/结构化 → 绘图）。

把 search_results.json 的杂乱文本清洗为 clean_chart_data.json：
- entity_frequency   ：仅统计【提到核心主题】的文档中的目标实体；
                       同一实体的中英文/别名合并到规范名（如 NVIDIA/NV → 英伟达）
- market_data        ：用精确规则提取 推理/训练/边缘/逻辑芯片...亿元/亿美元 等结构化数值
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
    r"芯片|AI|算力|推理|训练|NVIDIA|英伟达|GPU|ASIC|FPGA|加速卡", re.I,
)

# 精确市场数据规则：<分项>…<数字>亿元/亿美元（支持千分位逗号与"约"前缀）
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
MARKET_PATTERNS = [
    ("推理芯片", re.compile(r"推理芯片[^。；;]*?" + _NUM + r"\s*(?:亿|万)美元", re.I)),
    ("训练芯片", re.compile(r"训练芯片[^。；;]*?" + _NUM + r"\s*(?:亿|万)美元", re.I)),
    ("边缘AI芯片", re.compile(r"边缘(?:AI)?芯片[^。；;]*?" + _NUM + r"\s*(?:亿|万)美元", re.I)),
    ("逻辑芯片市场规模", re.compile(r"逻辑芯片市场规模[^。；;]*?" + _NUM + r"\s*亿元", re.I)),
    ("市场规模", re.compile(r"市场规模[^。；;]*?" + _NUM + r"\s*(?:亿|万)美元", re.I)),
]

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
    """把原始检索结果清洗为结构化图表数据。"""
    docs = _docs(items)
    entity_freq = Counter()
    market = []
    for d in docs:
        text = _doc_text(d)
        if not THEME_ANCHOR.search(text):
            continue
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
        for label, pat in MARKET_PATTERNS:
            m = pat.search(text)
            if m:
                try:
                    val = float(m.group(1).replace(",", ""))
                    unit = "亿元" if "亿元" in m.group(0) else "亿美元"
                    market.append({
                        "label": label, "value": val, "unit": unit,
                        "source": _first_url(items, text),
                    })
                except (ValueError, TypeError):
                    pass
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
        "source_distribution": dict(domains.most_common(12)),
        "topic_terms": dict(topic_terms.most_common(12)),
    }


def _first_url(items: list[dict], text: str) -> str:
    for d in items:
        if isinstance(d, dict) and _doc_text(d) == text:
            return str(d.get("url") or "")
    return ""


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
