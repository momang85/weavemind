# -*- coding: utf-8 -*-
"""织光 - 图表规格规范与校验。

落地"工行杯图表质量评审标准"：
1. 每张图必须有明确调研问题与一句话结论（无结论 → skip）。
2. 数据必须带来源/时间/地域/单位/口径。
3. 图表类型必须匹配数据特征。
4. 视觉编码规范（柱状从 0、类别降序、色盲友好、禁 3D）。
5. 标注完整性（标题含指标+时间+地域+单位，轴标题+单位，来源，结论）。
"""

import re

CHART_TYPES = ("line", "bar", "horizontal_bar", "pie", "scatter")

# 关键字段缺失即视为无效图（跳过）
CRITICAL_FIELDS = (
    "title", "unit", "source", "x_axis_title",
    "y_axis_title", "conclusion",
)

# 强烈建议字段（缺失时在注释里提示，但不跳过）
RECOMMENDED_FIELDS = (
    "question", "time_range", "region",
    "sample_size", "annotation", "missing", "outliers",
)

# 色盲友好调色板（Okabe-Ito）
COLOR_BLIND_PALETTE = [
    "#0072B2", "#D55E00", "#009E73", "#CC79A7",
    "#F0E442", "#56B4E9", "#E69F00", "#000000",
]


def validate_spec(spec) -> list[str]:
    """校验单个图表规格，返回缺失/非法项列表；空列表 = 合法。"""
    issues: list[str] = []
    if not isinstance(spec, dict):
        return ["spec 不是对象"]
    if str(spec.get("type") or "") not in CHART_TYPES:
        issues.append(f"type 非法（应为 {'/'.join(CHART_TYPES)}）")
    for f in CRITICAL_FIELDS:
        if not str(spec.get(f) or "").strip():
            issues.append(f"缺少 {f}")
    data = spec.get("data") or []
    if not data:
        issues.append("data 为空")
    elif len(data) < 2:
        issues.append("data 少于 2 个数据点（单点图无结论，按规范跳过）")
    for it in data:
        if not isinstance(it, dict) or "label" not in it or "value" not in it:
            issues.append("data 行缺少 label/value")
            break
    return issues


def validate_specs(specs) -> tuple[list[dict], dict[int, list[str]]]:
    """批量校验：返回 (合法规格, {下标: 问题列表})。"""
    valid: list[dict] = []
    issues_map: dict[int, list[str]] = {}
    for i, s in enumerate(specs):
        issues = validate_spec(s)
        if issues:
            issues_map[i] = issues
        else:
            valid.append(s)
    return valid, issues_map


def pick_type(rows: list[dict]) -> str:
    """按数据特征推荐图表类型（规则见标准）。"""
    def _yr(r):
        v = r.get("year")
        if v is None:
            v = r.get("年份")
        return v

    years = {_yr(r) for r in rows if _yr(r) is not None}
    n = len(rows)
    if len(years) >= 2 and all(_yr(r) is not None for r in rows):
        return "line"  # 时间序列
    if n > 10:
        return "horizontal_bar"  # 类别多 → 水平条形
    if any(_yr(r) is not None for r in rows) and n <= 6:
        return "bar"
    return "bar"


_METRIC_STOP = ("预测", "预计", "测算", "展望", "统计", "口径", "对比")

_CORE_METRICS = ("市场规模", "市场销售额", "销售额", "份额", "占比", "营收", "出货量", "增速", "增长率")
_SEGMENT_WORDS = (
    "推理", "训练", "边缘", "数据中心", "云端", "端侧", "汽车", "消费",
    "工业", "医疗", "手机", "服务器", "存储", "通用", "专用", "中国",
    "美国", "欧洲", "日本", "车载", "云", "端",
)
_TOTAL_WORDS = ("全口径", "合计", "总计", "总体", "总共", "全部", "整体")


def _normalize_series(metric: str, caliber: str = "") -> str:
    """把分项指标归一为同一系列（如"推理芯片市场规模"→"市场规模"），
    使同一指标的分项（训练/推理/边缘）能合成一张对比图；
    "全口径/合计/总计"等总量行保持独立，避免与分项混图。"""
    if any(w in str(caliber) for w in _TOTAL_WORDS):
        return str(metric)
    core = next((c for c in _CORE_METRICS if c in str(metric)), None)
    if not core:
        return str(metric)
    rest = str(metric).replace(core, "")
    stripped = re.sub(r"^(全球|中国|美国|欧洲|日本|其中|AI|芯片|半导体)+", "", rest)
    if not stripped:
        return str(metric)  # 无领域前缀 → 普通/总指标行，不合并
    if any(w in stripped for w in _SEGMENT_WORDS):
        return core
    return str(metric)


def _metric_key(title: str) -> str:
    """归一化标题为指标键：去掉括号（年份/单位）、修饰词与标点，
    用于识别"同一指标的不同年份"单点图。"""
    t = re.sub(r"[（(][^）)]*[）)]", "", str(title or ""))
    for w in _METRIC_STOP:
        t = t.replace(w, "")
    t = re.sub(r"[年月日：:，,。．\-·\s]+", "", t)
    return t.lower()


def merge_year_series(specs: list[dict]) -> list[dict]:
    """把同一指标、不同年份的独立单点规格合并为一张时间序列折线图。
    例如 2025 年 890 亿美元 + 2026 年 1120 亿美元 → 一张折线。
    仅合并：type=bar、单数据行、带数值年份、指标键相同的规格。"""
    specs = [s for s in specs if isinstance(s, dict)]
    groups: dict[tuple, list[int]] = {}
    for idx, s in enumerate(specs):
        rows = [r for r in (s.get("data") or []) if isinstance(r, dict)]
        if s.get("type") != "bar" or len(rows) != 1:
            continue
        yr = rows[0].get("year")
        if yr is None:
            continue
        key = (str(s.get("unit") or ""), _metric_key(str(s.get("title") or "")))
        groups.setdefault(key, []).append(idx)

    consumed: set[int] = set()
    merged_specs: list[dict] = []
    for key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        items = [(specs[i], specs[i]["data"][0].get("year")) for i in idxs]
        years = sorted({y for _, y in items})
        if len(years) < 2:
            continue
        consumed.update(idxs)
        items = sorted(items, key=lambda t: t[1])
        rows: list[dict] = []
        srcs: list[str] = []
        for s, _ in items:
            for r in s.get("data") or []:
                rows.append(r)
                u = str(r.get("source") or "").strip()
                if u and u not in srcs:
                    srcs.append(u)
        base = items[0][0]
        unit = str(base.get("unit") or "")
        title = re.sub(r"[（(][^）)]*[）)]", "", str(base.get("title") or "")).strip()

        def _fmt(v):
            try:
                return f"{float(v):g}"
            except (TypeError, ValueError):
                return str(v)

        merged_specs.append({
            "question": f"{title}在 {years[0]}-{years[-1]} 年间的变化趋势？",
            "conclusion": (
                f"{title}从 {years[0]} 年 {_fmt(rows[0].get('value'))}{unit} "
                f"变化到 {years[-1]} 年 {_fmt(rows[-1].get('value'))}{unit}。"
            ),
            "type": "line",
            "title": f"{title}（{years[0]}-{years[-1]}年，单位：{unit}）",
            "x_axis_title": "年份",
            "y_axis_title": f"{title}（{unit}）",
            "unit": unit,
            "time_range": f"{years[0]}-{years[-1]}年",
            "region": str(base.get("region") or "未标注"),
            "source": "；".join(srcs) or str(base.get("source") or ""),
            "sample_size": str(len(rows)),
            "annotation": "同指标不同年份数据合并为时间序列；口径差异见各数据行。",
            "missing": str(base.get("missing") or "无"),
            "outliers": str(base.get("outliers") or "无"),
            "data": rows,
        })

    out = [s for i, s in enumerate(specs) if i not in consumed]
    return merged_specs + out


def verify_specs_against_text(specs: list[dict], text: str) -> tuple[list[dict], int]:
    """数据溯源校验：规格中每个数据行的数值必须能在来源文本中找到对应表述
    （防 LLM 编造/转写错误，如 1059.8 误写成 1060）。
    找不到数值的行丢弃；行数不足 2 的整图丢弃。
    返回 (保留规格, 丢弃行数)。"""
    nt = re.sub(r"[\s,，。；;：:·\u3000]+", "", text or "")
    kept_specs: list[dict] = []
    dropped_rows = 0
    for s in specs:
        if not isinstance(s, dict):
            continue
        rows_kept: list[dict] = []
        for r in s.get("data") or []:
            if not isinstance(r, dict):
                continue
            v = r.get("value")
            cands: set[str] = set()
            if isinstance(v, (int, float)):
                f = float(v)
                cands.add(f"{f:g}")
                cands.add(str(f))
                if f == int(f):
                    cands.add(str(int(f)))
            else:
                cands.add(str(v))
            if any(c and c in nt for c in cands):
                rows_kept.append(r)
            else:
                dropped_rows += 1
        if len(rows_kept) >= 2:
            s = dict(s)
            s["data"] = rows_kept
            s["sample_size"] = str(len(rows_kept))
            kept_specs.append(s)
        elif rows_kept and len(rows_kept) < 2:
            dropped_rows += 0  # 行数已计入
    return kept_specs, dropped_rows


def wrap_rows_to_specs(rows: list[dict]) -> list[dict]:
    """兜底：把扁平数据行（指标/年份/数值/单位/口径/来源）打包成图表规格。
    结论由数据形态推导（对比/差异），供无 LLM 规格时使用。
    单一数据点无法支撑对比/趋势结论 → 跳过（图表规范：无结论不画图）。"""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        metric = str(r.get("指标") or "指标")
        unit = str(r.get("单位") or "")
        # 同指标不同单位（% vs 万亿美元）不得合并，按规范分开
        # 不同表格的同名指标（机构规模 vs 区域规模）不得合并：表作用域参与分组
        key = (_normalize_series(metric, str(r.get("口径") or "")), unit,
               str(r.get("_tbl") or ""))
        groups.setdefault(key, []).append(r)
    specs = []
    for (metric, unit, _tbl), group in groups.items():
        if len(group) < 2:
            continue  # 单点图无意义，按规范跳过
        years = sorted({r.get("年份") for r in group if r.get("年份") is not None})
        if len(years) >= 2 and all(r.get("年份") is not None for r in group):
            ctype = "line"
            x = "年份"
            conclusion = f"{metric}随年份变化（{years[0]}→{years[-1]}），见数据标注"
        else:
            ctype = pick_type(group)
            # 占比/份额数据（单位 % 或指标含份额/占比）→ ≤5 类用饼图
            if unit == "%" or any(k in metric for k in ("份额", "占比")):
                ctype = "pie" if len(group) <= 5 else "bar"
            x = "口径/来源"
            vals = [r.get("数值") for r in group]
            hi = max(vals) if vals else 0
            lo = min(vals) if vals else 0
            if hi == lo:
                conclusion = f"{metric}各口径数值一致（{hi:g}{unit}）"
            else:
                conclusion = f"{metric}在各口径/来源间差异显著（{lo:g}~{hi:g}{unit}）"
        spec = {
            "question": f"{metric}对比：{x}如何影响数值？",
            "conclusion": conclusion,
            "type": ctype,
            "title": f"{metric}对比（{unit}）",
            "x_axis_title": x,
            "y_axis_title": f"{metric}（{unit}）",
            "unit": unit,
            "time_range": f"{years[0]}-{years[-1]}" if len(years) >= 2 else str(years[0] if years else "未知"),
            "region": "未标注",
            "source": "；".join(str(r.get("来源") or "") for r in group if r.get("来源"))[:200] or "检索资料",
            "sample_size": str(len(group)),
            "annotation": "数值来自检索资料，口径见各数据行；缺失或异常已按来源标注。",
            "missing": "无",
            "outliers": "极端值已在图内保留并可在口径中核对",
            "data": [
                {
                    "label": str(r.get("口径") or r.get("来源") or "?"),
                    "value": r.get("数值"),
                    "year": r.get("年份"),
                    "caliber": str(r.get("口径") or ""),
                    "source": str(r.get("来源") or ""),
                }
                for r in group
            ],
        }
        specs.append(spec)
    return specs
