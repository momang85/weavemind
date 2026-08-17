# -*- coding: utf-8 -*-
"""数据清洗与结构化（对标用户建议：搜索 → 清洗/结构化 → 绘图）。

把 search_results.json 的杂乱文本清洗为 clean_chart_data.json：
- entity_frequency   ：仅统计【提到核心主题】的文档中的目标实体（避免"中国-1"错配）
- market_data        ：用精确规则提取 推理/训练/边缘芯片...亿美元 等结构化数值
- source_distribution：域名聚合计数（全 1 时由绘图层跳过）
- topic_terms        ：停用词 + 主题锚点过滤后的热词
"""

import json
import re
from collections import Counter
from pathlib import Path

TARGET_ENTITIES = [
    "英伟达", "NVIDIA", "AMD", "英特尔", "Intel", "谷歌", "Google",
    "华为", "昇腾", "高通", "Qualcomm", "寒武纪", "海光", "亚马逊",
    "AWS", "微软", "Microsoft", "Meta", "台积电", "Cerebras", "Graphcore",
    "美国", "中国", "欧洲", "亚太", "日本",
]

THEME_ANCHOR = re.compile(
    r"芯片|AI|算力|推理|训练|NVIDIA|英伟达|GPU|ASIC|FPGA|加速卡", re.I,
)

# 精确市场数据规则：<分项>…<数字>亿美元（支持千分位逗号与"约"前缀）
_NUM = r"(\d[\d,]*(?:\.\d+)?)"
MARKET_PATTERNS = [
    ("推理芯片", re.compile(r"推理芯片[^。；;]*?" + _NUM + r"\s*亿美元", re.I)),
    ("训练芯片", re.compile(r"训练芯片[^。；;]*?" + _NUM + r"\s*亿美元", re.I)),
    ("边缘AI芯片", re.compile(r"边缘(?:AI)?芯片[^。；;]*?" + _NUM + r"\s*亿美元", re.I)),
    ("市场规模", re.compile(r"市场规模[^。；;]*?" + _NUM + r"\s*亿美元", re.I)),
]

STOPWORDS = {
    "一个", "我们", "以及", "可以", "没有", "已经", "进行", "通过", "对于",
    "不是", "就是", "同时", "如果", "因为", "所以", "但是", "这些", "那些",
    "其中", "主要", "相关", "关于", "根据", "报告", "分析",
    "全球", "中国", "市场", "行业", "产业", "发展", "增长", "技术",
    "应用", "领域", "数据", "信息", "公司", "企业", "方面", "预计",
    "成为", "带来", "推动", "驱动", "规模", "目前", "未来", "有望",
    "万亿美元", "亿美元", "万亿元", "万亿", "存储", "存储芯片", "半导体",
}


def _docs(items: list[dict]) -> list[str]:
    return [
        str(d.get("title") or "") + " " + str(d.get("snippet") or "")
        for d in items if isinstance(d, dict)
    ]


def clean_and_structure(items: list[dict]) -> dict:
    """把原始检索结果清洗为结构化图表数据。"""
    docs = _docs(items)
    entity_freq = Counter()
    market = []
    for text in docs:
        if THEME_ANCHOR.search(text):
            for ent in TARGET_ENTITIES:
                if ent in text:
                    entity_freq[ent] += text.count(ent)
        for label, pat in MARKET_PATTERNS:
            m = pat.search(text)
            if m:
                try:
                    val = float(m.group(1).replace(",", ""))
                    market.append({"label": label, "value": val,
                                   "unit": "亿美元", "source": _first_url(items, text)})
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
    for text in docs:
        if not THEME_ANCHOR.search(text):
            continue
        for m in re.finditer(r"[\u4e00-\u9fff]{2,4}", text.lower()):
            w = m.group(0)
            if w in STOPWORDS:
                continue
            topic_terms[w] += 1
    return {
        "entity_frequency": dict(entity_freq.most_common(12)),
        "market_data": market,
        "source_distribution": dict(domains.most_common(12)),
        "topic_terms": dict(topic_terms.most_common(12)),
    }


def _first_url(items: list[dict], text: str) -> str:
    for d in items:
        if isinstance(d, dict) and str(d.get("title") or "") + " " + str(d.get("snippet") or "") == text:
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
