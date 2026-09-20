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

CHART_TYPES = ("line", "bar", "horizontal_bar", "pie", "scatter", "grouped_bar")

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
    # 单位一致性（BUG-5）：同图数据必须同量纲，否则柱子/刻度语义错乱
    # （实测例：指数点位 3450 与市盈率 11.8/8.5 同轴 → 后两者不可见）。
    # 散点图例外：x/y 双轴按设计携带不同量纲（如量价散点 最新价×成交量）
    if str(spec.get("type") or "") != "scatter":
        spec_unit = str(spec.get("unit") or "").strip()
        if re.search(r"[／/、]", spec_unit):
            issues.append(f"unit 混合单位（{spec_unit}），同图禁止混装量纲")
        row_units = {
            str(it.get("unit") or "").strip()
            for it in data if isinstance(it, dict) and str(it.get("unit") or "").strip()
        }
        if len(row_units) > 1:
            issues.append(
                "data 行单位不一致（" + "/".join(sorted(row_units)) + "），同图禁止混装量纲"
            )
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
                if f < 0:
                    # 亏损/负值在正文常以正数表述（"亏损6862亿元"），补绝对值候选，
                    # 避免 2021 年 -6862 这类极端值被溯源校验误删
                    cands.add(f"{abs(f):g}")
                    cands.add(str(int(abs(f))))
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


def financial_research_specs(rows: list[dict], derived: list[dict], *,
                             unit: str = "", source: str = "",
                             company: str = "", caliber: str = "",
                             periods: list[int] | None = None,
                             core_metrics: list[str] | None = None,
                             subject_type: str = "") -> list[dict]:
    """公司研究任务的**三张财务分析图**（确定性规格，不经 LLM）。

    为什么单独一条：实机 `ui-2084c2c9cc` 的图是"市场规模对比（亿元）"——16 个会计科目
    （含流量与存量）塞进同一坐标轴、每个"序列"只有一个点，退化成 16 个孤立圆点；财务
    读者要的是**两期对比 + 同比 + 质量比率**这三张。数据一律来自底稿（含同比/比率），
    越界期间已在上游 `chart_rows` 过滤。

    两条硬约束（都是实机踩出来的）：
    - 对比图**只画契约点名的必需指标**（`core_metrics`），且**同图不得混装量纲**——
      否则毛利率（%）会与营收（亿元）同轴，小项完全不可见（实机 `ui-ecb93e57a1`）；
    - 只在数据够画时才产出（单点图无结论，按规范跳过）；结论由数据算出，不写空话。

    A1：研究对象为**金融机构**时不生成第三张（企业口径质量比率图）——净利率/现金
    覆盖/资产负债率对银行不成立，画出来就是误导；保留两期对比与同比两张。
    """
    try:
        from facts import metric_label
    except Exception:                     # 生成脚本环境缺依赖时退回英文键
        def metric_label(key):
            return str(key)

    years = sorted({int(r.get("year")) for r in rows if r.get("year") is not None})
    periods = sorted(int(y) for y in (periods or years)) or years
    src = source or "结构化财务数据源"
    span = (f"{periods[0]}-{periods[-1]}" if len(periods) >= 2
            else str(periods[0] if periods else "未知"))
    who = f"{company} " if company else ""
    specs: list[dict] = []

    # ① 两期核心指标对比（分组柱：x=指标，series=年度）
    core_rows = [r for r in rows if r.get("year") in set(periods)]
    if core_metrics:
        _want = [str(m) for m in core_metrics]
        core_rows = [r for r in core_rows if str(r.get("metric") or "") in set(_want)]
        core_rows.sort(key=lambda r: (_want.index(str(r.get("metric") or ""))
                                      if str(r.get("metric") or "") in _want else 99,
                                      r.get("year") or 0))
    else:
        core_rows.sort(key=lambda r: (str(r.get("metric") or ""), r.get("year") or 0))
    if core_rows:
        # 同图一个量纲：只保留占多数的单位（金额类指标），百分比指标不混进来
        _units = [str(r.get("unit") or "") for r in core_rows]
        _main_unit = max(set(_units), key=_units.count) if _units else ""
        if _main_unit:
            core_rows = [r for r in core_rows
                         if str(r.get("unit") or "") == _main_unit]
            unit = unit or _main_unit
    if len(years) >= 2 and len(core_rows) >= 2:
        yoy_by_metric = {str(d.get("metric") or ""): d.get("value")
                         for d in derived
                         if str(d.get("metric") or "").endswith("_yoy")}
        ups = [v for v in yoy_by_metric.values() if isinstance(v, (int, float))]
        if ups and all(v > 0 for v in ups):
            top = max(yoy_by_metric.items(), key=lambda kv: kv[1])
            conclusion = (f"两期核心指标均上升，{metric_label(top[0])}增幅最大"
                          f"（{top[1]:g}%）；分项同比见同比增速图")
        elif ups:
            conclusion = "两期核心指标有升有降，分项同比见同比增速图"
        else:
            conclusion = "两期核心指标规模对比，数值见图注"
        specs.append({
            "question": f"{who}{periods[0]} 与 {periods[-1]} 三个核心指标的规模对比如何？",
            "conclusion": conclusion,
            # 图注（报告正文用）**不带数字**：正文里的数字必须可溯源，图注里的
            # 同比/百分点若单列一处就成"不可溯源数字"（实测把溯源率从 ~100% 拉到 69%）
            "caption": "两期核心指标规模对比（升/降方向见图中标注）",
            "type": "grouped_bar",
            "title": f"{who}{periods[0]} vs {periods[-1]} 核心指标对比（{unit or '原值'}）",
            "x_axis_title": "核心指标",
            "y_axis_title": f"金额（{unit}）" if unit else "数值",
            "unit": unit,
            "time_range": span,
            "region": "未标注",
            "sample_size": str(len(core_rows)),
            "source": src,
            "section_hint": "关键数据",
            "data": [
                {"label": str(r.get("metric_label") or metric_label(r.get("metric"))),
                 "value": r.get("value"), "unit": r.get("unit") or unit,
                 "year": r.get("year"), "caliber": f"{r.get('year')}年",
                 "source": src}
                for r in core_rows
            ],
        })

    # ② 同比增速（柱：每个核心指标的同比 %）
    yoy_rows = [d for d in derived
                if str(d.get("metric") or "").endswith("_yoy")
                and isinstance(d.get("value"), (int, float))]
    if len(yoy_rows) >= 2:
        best = max(yoy_rows, key=lambda d: d["value"])
        worst = min(yoy_rows, key=lambda d: d["value"])
        last_year = periods[-1] if periods else ""
        specs.append({
            "question": f"{who}{last_year} 年各核心指标同比增速是多少？",
            "conclusion": (f"{metric_label(best.get('metric'))} 增幅最大"
                           f"（{best['value']:g}%），"
                           f"{metric_label(worst.get('metric'))} 最低（{worst['value']:g}%）"),
            "type": "bar",
            "caption": "各核心指标同比增速（升/降方向见图中标注）",
            "title": f"{who}{last_year} 年核心指标同比增速（%）",
            "x_axis_title": "核心指标",
            "y_axis_title": "同比（%）",
            "unit": "%",
            "time_range": span,
            "region": "未标注",
            "sample_size": str(len(yoy_rows)),
            "source": src,
            "section_hint": "同比",
            "data": [
                {"label": str(d.get("metric_label") or metric_label(d.get("metric"))),
                 "value": d.get("value"), "unit": "%", "source": src}
                for d in yoy_rows
            ],
        })

    # ③ 质量比率：**按指标分面**（每个比率一张两期对比图）
    #    为什么不再挤一张：归母净利率（利润率）、经营现金流覆盖（倍数，可 >100%）、
    #    资产负债率（杠杆）、研发投入强度（投入强度）经济含义与量级都不同，同轴会把
    #    小项压成看不见；且长标签（覆盖 14 字）会被类别清洗退化成"2024年"，
    #    x 轴混入年份、分组与系列配对错位（实机 chart_3）。
    #    A1：金融机构不生成（企业口径比率对银行不成立，画出来就是误导）。
    ratio_rows = [d for d in derived
                  if str(d.get("metric") or "") in _QUALITY_RATIOS
                  and isinstance(d.get("value"), (int, float))
                  and subject_type != "financial"]
    by_metric: dict[str, list[dict]] = {}
    for d in ratio_rows:
        by_metric.setdefault(str(d.get("metric") or ""), []).append(d)
    last_year = periods[-1] if periods else None
    for metric in _QUALITY_RATIOS:
        series = sorted(by_metric.get(metric) or [], key=lambda d: d.get("year") or 0)
        if len(series) < 2:
            continue                      # 单点不画（无对比就没有观察）
        label = str(series[0].get("metric_label") or metric_label(metric))
        vals = [d.get("value") for d in series]
        # 观察由数据算出：变化用**百分点**表述（比率型指标的规范写法）
        delta = vals[-1] - vals[0]
        direction = "上升" if delta > 0 else "下降" if delta < 0 else "持平"
        observation = (f"{label} {vals[0]:g}% → {vals[-1]:g}%，"
                       f"{direction} {abs(delta):.2f} 个百分点")
        if metric == "cashflow_coverage":
            _np_val = next((r.get("value") for r in rows
                            if r.get("metric") == "net_profit"
                            and r.get("year") == last_year), None)
            _cf_val = next((r.get("value") for r in rows
                            if r.get("metric") == "operating_cashflow"
                            and r.get("year") == last_year), None)
            if isinstance(_np_val, (int, float)) and _np_val < 0:
                observation += f"；最新一期归母净利润为负（{_np_val:g}），该倍数不表示利润有现金支撑"
            elif isinstance(_cf_val, (int, float)) and _cf_val < 0:
                observation += "；最新一期经营现金流为净流出，该倍数不表示利润有现金支撑"
            elif vals[-1] > 100:
                observation += "；当期经营现金流高于归母净利润（>100%）"
            else:
                observation += "；当期经营现金流低于归母净利润（<100%）"
        specs.append({
            "question": f"{who}{label} {periods[0]} 与 {periods[-1]} 两期变化如何？",
            "conclusion": observation,
            "caption": f"{label}两期{'上升' if delta > 0 else '下降' if delta < 0 else '持平'}"
                       "（百分点变化见『同比与比率』块）",
            "type": "bar",
            "title": f"{who}{label}两期对比（%）",
            "x_axis_title": "期间",
            "y_axis_title": f"{label}（%）",
            "unit": "%",
            "time_range": span,
            "region": "未标注",
            "sample_size": str(len(series)),
            "source": src,
            "section_hint": "盈利质量",
            "data": [
                {"label": f"{d.get('year')}年", "value": d.get("value"), "unit": "%",
                 "year": d.get("year"), "caliber": f"{d.get('year')}年",
                 "source": src}
                for d in series
            ],
        })
    return specs


# 质量类比率（与 working_paper._RATIO_SPECS 的派生指标同名）
_QUALITY_RATIOS = ("net_margin", "cashflow_coverage", "debt_ratio", "rd_intensity")

_PERIOD_LABEL_RE = re.compile(r"^\d{4}|\d+(\.\d+)?$")


def is_period_labels(labels) -> bool:
    """类别标签是否全是"期间/序号"（`2023年`、`2024`、`1`）——是则保持给定顺序，不按值排序。

    实机反例：只认纯数字标签时，"2023年"匹配不上 → 比率分面图的两期被按数值降序排成
    "2024年/2023年"，颜色与年份的对应也跟着反了。带"年/月/季度"后缀的一并认作期间标签。
    """
    items = [str(x or "").strip() for x in (labels or [])]
    if not items:
        return False
    norm = [re.sub(r"[年月日季度]+$", "", x) for x in items]
    return all(re.fullmatch(r"\d{4}|\d+(\.\d+)?", x) for x in norm)


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
