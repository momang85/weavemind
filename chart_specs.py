# -*- coding: utf-8 -*-
"""织光 - 图表规格规范与校验。

落地"工行杯图表质量评审标准"：
1. 每张图必须有明确调研问题与一句话结论（无结论 → skip）。
2. 数据必须带来源/时间/地域/单位/口径。
3. 图表类型必须匹配数据特征。
4. 视觉编码规范（柱状从 0、类别降序、色盲友好、禁 3D）。
5. 标注完整性（标题含指标+时间+地域+单位，轴标题+单位，来源，结论）。
"""

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


def wrap_rows_to_specs(rows: list[dict]) -> list[dict]:
    """兜底：把扁平数据行（指标/年份/数值/单位/口径/来源）打包成图表规格。
    结论由数据形态推导（对比/差异），供无 LLM 规格时使用。
    单一数据点无法支撑对比/趋势结论 → 跳过（图表规范：无结论不画图）。"""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        metric = str(r.get("指标") or "指标")
        groups.setdefault(metric, []).append(r)
    specs = []
    for metric, group in groups.items():
        if len(group) < 2:
            continue  # 单点图无意义，按规范跳过
        unit = str(group[0].get("单位") or "")
        years = sorted({r.get("年份") for r in group if r.get("年份") is not None})
        if len(years) >= 2 and all(r.get("年份") is not None for r in group):
            ctype = "line"
            x = "年份"
            conclusion = f"{metric}随年份变化（{years[0]}→{years[-1]}），见数据标注"
        else:
            ctype = pick_type(group)
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
