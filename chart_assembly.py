# -*- coding: utf-8 -*-
"""图表装配模块（orchestrator 图表家族深化拆分）。

职责：把 orchestrator_v2 的图表链路（约 900 行）收敛为深模块：
- 纯函数层：主题判断、[CHART_DATA] 提取、表格兜底解析、主题过滤、规格清洗、
  chart_manifest 回填 —— 无 I/O，可独立测试；
- 渲染脚本：确定性渲染（chart_N.png）与探索性基线图（make_charts），
  以子进程执行，脚本常量集中在此，便于与 orchestrator 解耦。

orchestrator_v2 仅保留薄委托方法，本模块不依赖 orchestrator。
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


# 排行数据源（与 orchestrator_v2._RANKING_SOURCES 同源；本模块不反向依赖 orchestrator）
_RANKING_SOURCES = (
    "eastmoney_ranking", "tencent_ranking",
    "tencent_us_ranking", "sina_ranking",
)


_SEMANTIC_CHART_KEYWORDS = {
    "entity_frequency.png": ["主体提及频率", "entity"],
    "financial_trends.png": ["财务指标趋势", "financial"],
    "market_trends.png": ["市场趋势", "market"],
    "source_distribution.png": ["数据来源分布", "source"],
    "topic_terms.png": ["主题热词", "topic"],
    "market_data.png": ["市场规模", "market"],
    "market_share.png": ["市场份额", "share"],
    "macro_indicators.png": ["宏观指标", "macro"],
}

def _wants_visualization(goal: str) -> bool:
    """目标是否明确要求可视化/图表（只有此时才生成检索数据图表）。
    P1-3：加密/宏观类目标（币价/行情/走势/宏观/利率/涨跌幅/市值/竞争格局/排名）
    自动配图，避免"评估比特币短期趋势与风险"不命中可视化而 0 张图。"""
    g = str(goal or "").lower()
    return any(k in g for k in (
        "可视化", "图表", "趋势图", "柱状", "饼图", "折线", "调研",
        "plot", "chart", "graph",
        # 金融类目标也自动配图（财报要点 → 指标表 → 图表）
        "财报", "营收", "净利润", "财务", "季报", "年报", "业绩",
        # 市场数据类目标（规模/份额/占比/销量/渗透率/增长率）
        "市场规模", "市场份额", "占比", "销售", "销量", "出货",
        "渗透率", "增长率", "cagr", "预测", "规模",
        # P1-3：加密/宏观类目标（行情/走势/趋势与风险/宏观指标/排名格局）
        "币价", "比特币", "以太坊", "加密货币", "行情", "走势", "趋势",
        "宏观", "通胀", "利率", "涨跌幅", "市值", "份额", "竞争格局",
        "排名", "排行", "前十", "榜单", "成交量", "成交额", "涨停",
        "跌幅", "美联储", "cpi", "gdp",
    ))

def _extract_chart_data(text: str) -> list[dict]:
    """从 content_summary 结果中解析 [CHART_DATA] JSON 图表规格；
    兼容旧格式扁平 data 行（自动打包为规格）。"""
    if not text:
        return []
    idx = text.find("[CHART_DATA]")
    if idx < 0:
        return []
    seg = text[idx + len("[CHART_DATA]"):]
    i = seg.find("{")
    if i < 0:
        return []
    depth = 0
    for j in range(i, len(seg)):
        if seg[j] == "{":
            depth += 1
        elif seg[j] == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(seg[i:j + 1])
                    specs = data.get("charts")
                    if specs is None:
                        rows = data.get("data") or []
                        from chart_specs import wrap_rows_to_specs
                        specs = wrap_rows_to_specs([r for r in rows if isinstance(r, dict)])
                    return [s for s in specs if isinstance(s, dict)] if specs else []
                except Exception:
                    break
    return []

def _extract_chart_rows_from_table(text: str) -> list[dict]:
    """兜底：LLM 未输出 [CHART_DATA] 时，从摘要中的 Markdown 表格解析图表数据行。
    按列语义提取：数值列必须有单位；"指标"列逐行取值；年份/口径/来源按表头定位；
    无具体数值（如"数千亿"）、无单位的行直接丢弃。"""
    if not text:
        return []
    rows: list[dict] = []
    for tbl_no, tbl in enumerate(re.findall(r"(\|.+\|(?:\n\|.+\|)+)", text)):
        lines = tbl.strip().split("\n")
        if len(lines) < 3:
            continue
        headers = [c.strip() for c in lines[0].strip("|").split("|")]
        hmap = {h: i for i, h in enumerate(headers)}
        # 数值列定位：精确表头优先，其次按含 数值/规模/份额/增速/金额/营收 的表头
        val_idx = next(
            (hmap[k] for k in ("数值", "规模", "金额", "份额", "增速", "营收")
             if k in hmap),
            None,
        )
        if val_idx is None:
            for i, h in enumerate(headers):
                if any(k in h for k in ("数值", "规模", "份额", "增速", "金额", "营收")):
                    val_idx = i
                    break
        if val_idx is None:
            continue  # 无数值列 → 时间线/政策类表格不画图
        metric_idx = next(
            (hmap[k] for k in ("指标", "指标/年份", "指标名称") if k in hmap),
            None,
        )
        year_idx = next(
            (hmap[k] for k in ("年份", "时间", "年度") if k in hmap),
            None,
        )
        src_idx = next(
            (hmap[k] for k in ("来源", "来源链接", "出处", "链接") if k in hmap),
            None,
        )
        cal_idx = next(
            (hmap[k] for k in ("口径", "口径说明", "口径/年份", "统计口径", "口径范围", "备注")
             if k in hmap),
            None,
        )
        # 单位可能在表头括号里（如"市场规模（亿美元）"）
        header_unit = ""
        if val_idx is not None and val_idx < len(headers):
            mu = re.search(r"[（(]([^）)]*)[）)]", headers[val_idx])
            if mu:
                cand = mu.group(1).strip()
                if any(k in cand for k in (
                    "美元", "亿元", "万亿", "亿元", "%", "万辆", "万台", "元", "欧元", "人民币"
                )):
                    header_unit = cand
        # 占比/份额列 → 额外生成市场份额行（适合饼图）
        share_idx = next(
            (i for i, h in enumerate(headers)
             if i != val_idx and any(k in h for k in ("占比", "份额"))),
            None,
        )
        for line in lines[2:]:
            if "---" in line:
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if val_idx >= len(cells):
                continue
            joined = " ".join(cells)
            val_cell_clean = cells[val_idx].replace(",", "").replace("，", "")
            # 只从数值单元格解析"数字+单位"，禁止从整行拼接文本里抓其他数字
            # （如占比列的 52%），否则 labels 与 values 会错位。单元格可能只含
            # 数字（单位在表头括号里），此时回退 header_unit。
            num_m = re.search(
                r"(\d[\d]*(?:\.\d+)?)\s*(万亿|千亿|百亿|亿|万)?\s*(亿美元|亿元|%|万辆|万台|美元)?",
                val_cell_clean,
            )
            if not num_m:
                continue
            # "数千亿/数百亿" 这类无具体数字的值不画图
            if not num_m.group(1):
                continue
            cell_unit = (
                (num_m.group(2) or "") + (num_m.group(3) or "")
                if num_m.group(2) and num_m.group(3) in ("美元", "元", "人民币")
                else (num_m.group(3) or "")
            )
            year = None
            if year_idx is not None and year_idx < len(cells):
                ym = re.search(r"(20\d{2})", cells[year_idx])
                if ym:
                    year = int(ym.group(1))
            if year is None:
                ym = re.search(r"(20\d{2})", joined)
                if ym:
                    year = int(ym.group(1))
            src = ""
            if src_idx is not None and src_idx < len(cells):
                u = re.search(r"https?://[^\s\)\]]+", cells[src_idx])
                src = u.group(0) if u else cells[src_idx][:40]
            if not src:
                for c in cells:
                    u = re.search(r"https?://[^\s\)\]]+", c)
                    if u:
                        src = u.group(0)
                        break
            unit = cell_unit
            if not unit and header_unit:
                unit = header_unit
            if not unit:
                continue  # 无单位 → 规范要求必须有单位，不画图
            metric = "指标"
            if metric_idx is not None and metric_idx < len(cells):
                m = cells[metric_idx].strip()
                if m and m not in ("—", "-"):
                    metric = m[:30]
            else:
                metric = headers[val_idx] if val_idx < len(headers) else "指标"
                if any(k in metric for k in ("规模", "市场")):
                    metric = "市场规模"
                elif any(k in metric for k in ("份额", "占比")):
                    metric = "市场份额"
                elif any(k in metric for k in ("增速", "增长", "复合")):
                    metric = "增速"
            caliber = ""
            if cal_idx is not None and cal_idx < len(cells):
                caliber = cells[cal_idx][:30]
            if not caliber:
                for c in cells:
                    if not c or c in ("—", "-") or c == metric or c == cells[val_idx]:
                        continue
                    if re.fullmatch(r"[\d.]+(?:万亿|千亿|百亿|亿|万)?(?:美元|元|%|辆|台)?", c):
                        continue
                    if src and c == src:
                        continue
                    caliber = c[:30]
                    break
            rows.append({
                "指标": metric,
                "年份": year,
                "数值": float(num_m.group(1)),
                "单位": unit,
                "口径": caliber or "表格",
                "来源": src,
                "_tbl": f"t{tbl_no}",
            })
            # 占比/份额列：提取为市场份额行（单位 %，适合饼图）
            if share_idx is not None and share_idx < len(cells):
                sm = re.search(r"(\d+(?:\.\d+)?)\s*(%)?", cells[share_idx])
                if sm:
                    rows.append({
                        "指标": "市场份额",
                        "年份": year,
                        "数值": float(sm.group(1)),
                        "单位": "%",
                        "口径": (cells[0] if cells and cells[0] and cells[0] not in headers
                                 else (metric[:20] or "占比")),
                        "来源": src,
                        "_tbl": f"t{tbl_no}",
                    })
    return rows

def _filter_chart_rows(rows: list[dict], goal: str) -> list[dict]:
    """主题过滤：剔除与核心主题无关的数值（人形机器人/SoC/投资等），
    并要求指标/口径与目标核心对象相关（如目标含"芯片"则须含芯片/AI/算力等）。"""
    if not rows:
        return rows
    excluded = (
        "人形机器人", "机器人", "soc", "汽车", "手机", "白宫",
        "dram", "pcb", "oled", "投资", "财报", "具身智能", "蓝牙",
        "显示器", "面板",
    )
    kept = []
    for r in rows:
        text = (
            str(r.get("指标", "")) + " " + str(r.get("口径", ""))
            + " " + str(r.get("来源", ""))
        ).lower()
        if any(k in text for k in excluded):
            continue
        kept.append(r)
    return kept

def _excluded_for(goal: str) -> tuple[str, ...]:
    """与目标主题无关的领域词；若目标本身就在讨论该领域（如"新能源汽车"、
    "特斯拉财报"），则对应词不排除，避免误杀。"""
    excluded = (
        "人形机器人", "机器人", "soc", "汽车", "手机", "白宫",
        "dram", "pcb", "oled", "投资", "财报", "具身智能", "蓝牙",
        "显示器", "面板",
    )
    g = str(goal or "").lower()
    return tuple(k for k in excluded if k not in g)

def _goal_core(goal: str) -> list[str]:
    """从目标中提取核心主题词（2 字中文双字组/英文技术词），用于正向
    相关性校验：规格文本（标题/问题/结论/数据行）至少命中一个核心词。"""
    g = str(goal or "").lower()
    generic = (
        "市场", "报告", "调研", "分析", "全球", "中国", "国内", "国际",
        "可视化", "生成", "最新", "现状", "趋势", "规模", "份额", "数据",
        "行业", "情况", "请分", "进行", "梳理", "汇总", "总结", "要点",
        "评估", "方案", "项目", "产品", "技术", "领域", "相关", "以及",
        "我们", "可以", "需要", "完成", "输出", "一份", "文档", "内容",
        "差异", "明确", "要求", "必须", "时间", "方面", "主要", "官方",
        "权威", "机构", "经济", "整理", "以及", "以及", "请调", "研并",
        "并总", "年至", "年间", "球主", "要经", "济体", "体在", "工智",
        "能算", "力基", "础设", "施方", "资规", "心技", "术路", "线差",
        "异及", "及相", "关的", "策法", "求数", "据必", "须附", "附带",
        "带明", "确的", "的官", "方或", "或权", "威机", "构出", "并按",
        "按时", "间线", "线整", "整理", "2025", "2026", "20", "25", "26",
    )
    cands: set[str] = set()
    for m in re.findall(r"[\u4e00-\u9fff]{2,4}", g):
        for i in range(len(m) - 1):
            bg = m[i:i + 2]
            if bg not in generic:
                cands.add(bg)
    for m in re.findall(r"[a-z]{2,8}", g):
        if m in ("ai", "gpu", "soc", "ev", "llm", "iot", "saas", "b2b", "b2c", "erp", "crm", "cpu"):
            cands.add(m)
    excluded = set(_excluded_for(goal))
    # 排序保证跨进程确定性（集合迭代顺序受 hash 随机化影响）；
    # 全量返回而非截断——真正的主题词可能被截掉导致整图误删。
    # 噪声双字组（如"请分"）不会命中规格文本，保留无害。
    core = sorted(
        (c for c in cands if c not in excluded and not any(e in c for e in excluded)),
        key=lambda c: (-len(c), c),
    )
    return core

def _filter_chart_specs(specs: list[dict], goal: str) -> list[dict]:
    """主题过滤（图表规格版）：剔除与核心主题无关的图；
    规格中混入无关领域的数据行（人形机器人/SoC/投资等）逐行剔除，
    整图无关（标题/问题/结论即偏离主题）或数据行被清空则整图丢弃。"""
    if not specs:
        return specs
    excluded = _excluded_for(goal)
    core = _goal_core(goal)
    kept: list[dict] = []
    for s in specs:
        if not isinstance(s, dict):
            continue
        title_q = (
            str(s.get("title", "")) + " " + str(s.get("question", ""))
            + " " + str(s.get("conclusion", ""))
        ).lower()
        if any(k in title_q for k in excluded):
            continue
        rows = [r for r in s.get("data") or [] if isinstance(r, dict)]
        rows_kept = []
        for r in rows:
            text = (
                str(r.get("label", "")) + " " + str(r.get("caliber", ""))
                + " " + str(r.get("source", ""))
            ).lower()
            if any(k in text for k in excluded):
                continue
            rows_kept.append(r)
        if not rows_kept:
            continue
        core_text = (
            str(s.get("title", "")) + " " + str(s.get("question", ""))
            + " " + " ".join(
            str(r.get("label", "")) + " " + str(r.get("caliber", ""))
            for r in rows_kept
            )
        ).lower()
        # 结论字段不参与主题匹配：兜底规格的结论模板词（如"差异显著"）
        # 可能误中目标里的通用词（如"技术路线差异"），导致离题图被放行
        if core and not any(k in core_text for k in core):
            continue
        s = dict(s)
        s["data"] = rows_kept
        kept.append(s)
    return kept

def _clean_rows_to_specs(clean: dict) -> list[dict]:
    """数据驱动兜底：把 clean_chart_data 的扁平行（英文键）转换为
    wrap_rows_to_specs 可消费行；crypto 行情点（价格/市值/24h量/涨跌幅）
    归入同一指标组，≥2 个可作图数据点即可成图。"""
    from chart_specs import wrap_rows_to_specs

    type_metric = {
        "market_size": "市场规模",
        "market_share": "市场份额",
        "market_trends": "市场趋势",
        "macro_indicators": "宏观指标",
        "entity_frequency": "主体提及频率",
        "source_distribution": "来源分布",
    }
    # P1-3：FRED series → 中文指标名，保证"利率/CPI/失业率"目标能命中主题过滤
    fred_metric = {
        "GDP": "美国GDP",
        "CPIAUCSL": "美国CPI",
        "UNRATE": "美国失业率",
        "DFF": "联邦基金利率",
    }
    rows: list[dict] = []
    for key in ("market_data", "market_trends", "macro_indicators", "market_share"):
        for r in clean.get(key) or []:
            if not isinstance(r, dict):
                continue
            try:
                value = float(r.get("value"))
            except (TypeError, ValueError):
                continue
            label = str(r.get("label") or "?")
            source = str(r.get("source") or "")
            series = str(r.get("series") or "")
            if series:
                metric = series
            elif source == "coingecko":
                metric = "加密货币行情"
            elif source == "fred":
                tok = label.split(" ", 1)[0] if " " in label else label
                metric = fred_metric.get(tok, tok)
            else:
                metric = type_metric.get(str(r.get("type") or ""), label)
            year = r.get("year")
            if year is None:
                m = re.search(r"(20\d{2})", label)
                if m:
                    year = int(m.group(1))
            rows.append({
                "指标": metric,
                "数值": value,
                "单位": str(r.get("unit") or ""),
                "年份": year,
                "口径": label,
                "来源": source or "检索资料",
            })
    specs = wrap_rows_to_specs(rows)
    # 排行量价散点：最新价(x) × 成交量/成交额(y)，让"排行前十"任务
    # 除了 top10 条形图还有量价关系图
    specs.extend(_ranking_volume_price_scatter(clean))
    return specs

def _ranking_volume_price_scatter(clean: dict) -> list[dict]:
    """排行来源量价散点规格（≥5 只股票才成图，兼容 eastmoney/tencent）。

    从 market_data 行按 label 还原同一股票的 最新价/成交量/成交额：
    成交量口径取 万手，成交额口径取 亿元；x=最新价，y=量或额。
    返回 [] 表示数据不足或非排行来源。
    """
    try:
        points: dict[str, dict] = {}
        series = ""
        has_volume = False
        has_amount = False
        src_label = "东方财富行情中心实时排行"
        region = "A股"
        for r in clean.get("market_data") or []:
            row_source = str(r.get("source") or "")
            if row_source not in _RANKING_SOURCES:
                continue
            if row_source == "tencent_us_ranking":
                src_label = "腾讯行情实时排行（美股）"
                region = "美股"
            elif row_source == "tencent_ranking":
                src_label = "腾讯行情实时排行"
            elif row_source == "sina_ranking":
                src_label = "新浪行情中心实时排行（全市场）"
            if "美股" in str(r.get("series") or ""):
                region = "美股"
            label = str(r.get("label") or "")
            unit = str(r.get("unit") or "")
            if not series:
                series = str(r.get("series") or "")
            if unit == "元" and label.endswith("最新价"):
                name = label[:-3]
                points.setdefault(name, {})["price"] = r.get("value")
            elif unit == "万手":
                points.setdefault(label, {})["volume"] = r.get("value")
                has_volume = True
            elif unit == "亿元":
                points.setdefault(label, {})["amount"] = r.get("value")
                has_amount = True
        use_amount = has_amount and not has_volume
        y_key = "amount" if use_amount else "volume"
        y_label = "成交额（亿元）" if use_amount else "成交量（万手）"
        pts = [
            {"name": name, "price": p.get("price"), "y": p.get(y_key)}
            for name, p in points.items()
            if p.get("price") is not None and p.get(y_key) is not None
        ]
        if len(pts) < 5:
            return []
        title_series = str(series or f"{region}行情排行")
        for suffix in ("最新价", "涨跌幅"):
            title_series = title_series.replace(suffix, "")
        return [{
            "question": f"{title_series}中最新价与成交量/成交额的关系如何？",
            "conclusion": (
                f"{len(pts)} 只个股的量价分布见散点，极端值已在图内保留"
            ),
            "type": "scatter",
            "title": f"{title_series}量价散点（{y_label} vs 最新价）",
            "x_axis_title": "最新价（元）",
            "y_axis_title": y_label,
            "unit": "元/万手" if not use_amount else "元/亿元",
            "time_range": "实时",
            "region": region,
            "source": src_label,
            "sample_size": str(len(pts)),
            "annotation": f"数据来自{src_label}，口径见各数据行",
            "missing": "无",
            "outliers": "极端值已在图内保留",
            "data": [
                {
                    "label": p["name"],
                    "value": p["y"],
                    "year": p["price"],  # render_scatter 用 year 作为数值 x 轴
                    "caliber": p["name"],
                    "source": src_label,
                }
                for p in pts
            ],
        }]
    except Exception as exc:
        logger.warning("ranking scatter spec failed: %s", str(exc)[:120])
        return []

def _backfill_chart_manifest(project) -> None:
    """P2-4：图表渲染后回填 chart_manifest.json。

    只补充缺失条目（file+keywords+section_hint），不覆盖渲染器已写出的
    正确条目；manifest 与磁盘上已渲染 PNG 一一对应。
    chart_N.png 沿用 chart_data.json 规格映射 + curated 关键词逻辑；
    make_charts 语义图按文件名推断关键词；不认识的 PNG 跳过（防误纳）。
    """
    mf = project / "chart_manifest.json"
    try:
        charts = (
            json.loads(mf.read_text(encoding="utf-8")).get("charts") or []
            if mf.exists() else []
        )
        charts = [c for c in charts if isinstance(c, dict)]
    except Exception:
        charts = []
    existing = {str(c.get("file") or "") for c in charts}
    pngs = sorted(p.name for p in project.glob("*.png"))
    missing = [n for n in pngs if n not in existing]
    if not missing:
        return
    # 按渲染脚本的校验顺序重建 chart_N.png → 规格映射
    try:
        payload = json.loads(
            (project / "chart_data.json").read_text(encoding="utf-8")
        )
        specs = [s for s in (payload.get("charts") or []) if isinstance(s, dict)]
    except Exception:
        specs = []
    try:
        from chart_specs import validate_spec
    except Exception:
        validate_spec = None
    seq: dict[str, dict] = {}
    idx = 0
    for spec in specs:
        if validate_spec is not None and validate_spec(spec):
            continue
        rows = [r for r in (spec.get("data") or []) if isinstance(r, dict)]
        if len(rows) < 2:
            continue
        ctype = str(spec.get("type") or "bar")
        if ctype == "pie":
            vals = []
            for r in rows:
                try:
                    vals.append(float(r.get("value")))
                except (TypeError, ValueError):
                    pass
            unit = str(spec.get("unit") or "")
            is_pct = unit == "%" or (bool(vals) and all(v <= 100 for v in vals))
            if vals and is_pct and not (95 <= sum(vals) <= 105):
                continue
        idx += 1
        seq[f"chart_{idx}.png"] = spec
    curated = (
        "规模", "趋势", "份额", "占比", "营收", "收入", "增速", "增长",
        "成本", "价格", "预测", "玩家", "厂商", "格局", "对比", "分布",
        "技术", "市场", "出货", "渗透", "渗透率", "出货量",
    )
    added = False
    for name in missing:
        spec = seq.get(name)
        if spec:
            text = " ".join(
                str(spec.get(k) or "")
                for k in ("title", "question", "conclusion")
            )
            keywords = list(dict.fromkeys(k for k in curated if k in text))[:14]
            if not keywords:
                continue
            charts.append({
                "file": name,
                "keywords": keywords,
                "section_hint": str(spec.get("section_hint") or ""),
            })
        else:
            semantic_keywords = _SEMANTIC_CHART_KEYWORDS.get(name)
            if not semantic_keywords:
                # 非图表 PNG（旧文件/临时图/不认识的命名）不纳入 manifest
                continue
            charts.append({
                "file": name,
                "keywords": list(semantic_keywords),
                "section_hint": "",
            })
        added = True
    if added:
        try:
            mf.write_text(
                json.dumps({"charts": charts}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            logger.info(
                "chart_manifest backfilled: %d missing entries added for %s",
                len(missing), project,
            )
        except Exception as exc:
            logger.warning("chart_manifest backfill failed: %s", str(exc)[:120])

_FINANCIAL_ENTITY_PREFIX_RE = re.compile(r"^([\u4e00-\u9fff]{2,8}?)(?=\d{4}年)")

_FINANCIAL_YEAR_PREFIX_RE = re.compile(r"^\d{4}年")

def _normalize_financial_metric(label: str) -> str:
    """从财务标签提取指标名，供图表按指标分组。

    多实体标签形如"宁德时代2025年营收"：先剥离实体前缀（仅当后面紧跟
    "YYYY年"时剥离），再剥离"YYYY年"；普通中文标签（无年份无实体）原样返回。
    """
    s = str(label or "")
    s = _FINANCIAL_ENTITY_PREFIX_RE.sub("", s)
    s = _FINANCIAL_YEAR_PREFIX_RE.sub("", s)
    return s

def _group_financial_rows(rows) -> dict:
    """把 clean_chart_data 的 market_data 行按 (实体, 指标) 分组。

    返回 {指标名: {实体名: [(年份, 值), ...]}}；实体名从标签前缀提取，
    单实体/无实体标签实体名为空字符串；各序列按年份排序并去重 (年份, 值)。
    """
    groups: dict[str, dict[str, list[tuple[int, float]]]] = {}
    for m in rows or []:
        yr = m.get("year")
        label = str(m.get("label") or "")
        metric = _normalize_financial_metric(label)
        if not yr or not metric or len(metric) < 2:
            continue
        em = _FINANCIAL_ENTITY_PREFIX_RE.match(label)
        entity = em.group(1) if em else ""
        pts = groups.setdefault(metric, {}).setdefault(entity, [])
        pt = (int(yr), float(m.get("value") or 0))
        if pt not in pts:
            pts.append(pt)
    return {
        metric: {ent: sorted(pts) for ent, pts in by_entity.items()}
        for metric, by_entity in groups.items()
    }

RENDER_CHART_SCRIPT = '# -*- coding: utf-8 -*-\n"""确定性渲染：读取 chart_data.json（{"charts": [规格]}），按 5 类图表规范绘制。\n语义（问题/结论/口径）由 LLM 负责；数字、标注、视觉编码由本脚本保证。\n无效规格跳过并打印原因；有效图输出 chart_N.png 并写 chart_manifest.json。"""\nimport json\nimport re\nimport sys\n\nsys.path.insert(0, r"__REPO_ROOT__")\nfrom chart_specs import COLOR_BLIND_PALETTE, validate_spec, wrap_rows_to_specs\n\nimport matplotlib\nmatplotlib.use("Agg")\nimport matplotlib.pyplot as plt\nimport numpy as np\n\nfrom chart_fonts import configure_zh_font\nconfigure_zh_font()\n\nCURATED = (\n    "规模", "趋势", "份额", "占比", "营收", "收入", "增速", "增长",\n    "成本", "价格", "预测", "玩家", "厂商", "格局", "对比", "分布",\n    "技术", "市场", "出货", "渗透", "渗透率", "出货量",\n)\n\ndef norm(v):\n    try:\n        return float(v)\n    except (TypeError, ValueError):\n        return None\n\n\ndef is_natural_order(rows):\n    """类别标签全部为年份/纯数字 → 保持自然顺序，不做降序。"""\n    labels = [str(r.get("label") or "").strip() for r in rows]\n    if not labels:\n        return False\n    return all(re.fullmatch(r"\\d{4}|\\d+(\\.\\d+)?", l) for l in labels)\n\n\ndef series_key(r, i):\n    return str(r.get("caliber") or r.get("source") or f"系列{i + 1}")[:20]\n\n\ndef short_label(r):\n    """类别标签清洗：LLM 有时把指标描述/整句当作 label（如\n    "AI芯片占全球芯片市场11%，全球芯片市场5760亿美元"），导致 x 轴标签错乱。\n    优先取 口径 或 label 中首个短片段（机构/年份/地区），否则取来源域名。"""\n    label = str(r.get("label") or "").strip() or "?"\n    if len(label) <= 12:\n        return label\n    for cand in (str(r.get("caliber") or "").strip(), label):\n        for sep in ("，", ",", "；", ";", "：", ":", "（", "("):\n            head = cand.split(sep)[0].strip()\n            if head and len(head) <= 12:\n                return head\n        if len(cand) <= 12 and cand:\n            return cand\n    src = str(r.get("source") or "")\n    m = re.search(r"https?://([^/]+)", src)\n    if m:\n        return m.group(1).replace("www.", "")[:12]\n    return label[:12]\n\n\ndef footer_lines(spec, rows, conclusion):\n    source = str(spec.get("source") or "").strip()\n    if not source:\n        srcs = [str(r.get("source") or "") for r in rows if r.get("source")]\n        source = "；".join(dict.fromkeys(srcs))[:260]\n    tr = str(spec.get("time_range") or "时间未标注")\n    rg = str(spec.get("region") or "地域未标注")\n    n = str(spec.get("sample_size") or len(rows))\n    miss = str(spec.get("missing") or "无").strip() or "无"\n    out = str(spec.get("outliers") or "无").strip() or "无"\n    lines = []\n    if conclusion:\n        lines.append("结论：" + conclusion[:140])\n    lines.append(f"数据来源：{source or \'未标注\'}\u3000时间：{tr}\u3000地域：{rg}\u3000样本量：n={n}")\n    if miss not in ("无", ""):\n        lines.append("缺失：" + miss[:80])\n    if out not in ("无", ""):\n        lines.append("异常：" + out[:80])\n    lines.append("数据未经审计，仅供参考；不同口径数据未合并。")\n    return "\\n".join(lines)\n\n\ndef add_footer(fig, spec, rows, conclusion):\n    fig.text(0.01, 0.004, footer_lines(spec, rows, conclusion),\n             fontsize=7.5, color="#444444", va="bottom", ha="left",\n             wrap=True)\n\n\ndef keywords_for(spec):\n    text = " ".join(str(spec.get(k) or "") for k in ("title", "question", "conclusion"))\n    kw = [k for k in CURATED if k in text]\n    # 2-gram 滑动切片已移除（曾产生 "年全/片市/场规" 式碎片关键词）\n    return list(dict.fromkeys(kw))[:14]\n\n\ndef render_bar(ax, spec, rows, horizontal):\n    items = [(short_label(r), norm(r.get("value"))) for r in rows]\n    items = [(l, v) for l, v in items if v is not None]\n    if not items:\n        return False\n    if not is_natural_order(rows):\n        items.sort(key=lambda t: t[1], reverse=True)\n    labels = [l for l, _ in items]\n    vals = [v for _, v in items]\n    colors = [COLOR_BLIND_PALETTE[i % len(COLOR_BLIND_PALETTE)] for i in range(len(labels))]\n    if horizontal:\n        ax.barh(labels, vals, color=colors, edgecolor="white")\n        ax.set_xlim(left=0)\n    else:\n        ax.bar(labels, vals, color=colors, edgecolor="white")\n        ax.set_ylim(bottom=0)\n    ax.set_xlabel(str(spec.get("x_axis_title") or "类别"))\n    ax.set_ylabel(str(spec.get("y_axis_title") or "数值"))\n    top5 = set(sorted(vals, reverse=True)[:5]) if len(vals) > 12 else set()\n    for i, v in enumerate(vals):\n        if len(vals) <= 12 or v in top5:\n            if horizontal:\n                ax.text(v, i, f"{v:g}", va="center", ha="left", fontsize=8)\n            else:\n                ax.text(i, v, f"{v:g}", ha="center", va="bottom", fontsize=8)\n    ax.tick_params(axis="x", rotation=28 if not horizontal else 0)\n    return True\n\n\ndef render_line(ax, spec, rows):\n    years = [norm(r.get("year")) for r in rows]\n    has_years = (len(years) >= 2 and all(y is not None for y in years)\n                 and len({y for y in years}) >= 2)\n    if has_years:\n        by = {}\n        for i, r in enumerate(rows):\n            by.setdefault(series_key(r, i), []).append((norm(r.get("year")), norm(r.get("value"))))\n        plotted = 0\n        for ci, (name, pts) in enumerate(by.items()):\n            pts = sorted(p for p in pts if p[1] is not None)\n            if not pts:\n                continue\n            xs = [p[0] for p in pts]\n            ys = [p[1] for p in pts]\n            ax.plot(xs, ys, marker="o", linewidth=2,\n                    color=COLOR_BLIND_PALETTE[ci % len(COLOR_BLIND_PALETTE)], label=name)\n            if len(ys) <= 12:\n                for x, y in zip(xs, ys):\n                    ax.text(x, y, f"{y:g}", ha="center", va="bottom", fontsize=7.5)\n            plotted += 1\n        if plotted > 1:\n            ax.legend(fontsize=8, frameon=False)\n        ax.set_xlabel(str(spec.get("x_axis_title") or "年份"))\n    else:\n        labels = [short_label(r) for r in rows]\n        vals = [norm(r.get("value")) for r in rows]\n        xs = list(range(len(vals)))\n        ax.plot(xs, vals, marker="o", linewidth=2, color=COLOR_BLIND_PALETTE[0])\n        ax.set_xticks(xs)\n        ax.set_xticklabels(labels, rotation=25, fontsize=8)\n        ax.set_xlabel(str(spec.get("x_axis_title") or "类别/年份"))\n    ax.set_ylabel(str(spec.get("y_axis_title") or "数值"))\n    ax.grid(alpha=0.25)\n    return True\n\n\ndef render_pie(ax, spec, rows):\n    items = [(short_label(r), norm(r.get("value"))) for r in rows]\n    items = [(l, v) for l, v in items if v is not None]\n    if not items or len(items) > 5:\n        return False\n    labels = [l for l, _ in items]\n    vals = [v for _, v in items]\n    total = sum(vals)\n    if total <= 0:\n        return False\n    unit = str(spec.get("unit") or "")\n    is_pct = unit == "%" or all(v <= 100 for v in vals)\n    if is_pct and not (95 <= total <= 105):\n        print(f"SKIP pie: 占比数据加和 {total:.1f}% 不为 100%，饼图会误导", flush=True)\n        return False\n    wedges, _ = ax.pie(\n        vals, labels=labels, colors=COLOR_BLIND_PALETTE[:len(labels)],\n        startangle=90, counterclock=False,\n        wedgeprops={"edgecolor": "white", "linewidth": 1},\n    )\n    ax.axis("equal")\n    # 数据标注：直接用原值（占比数据即原百分比，禁止重算占比导致图与数据不符）\n    for w, v in zip(wedges, vals):\n        ang = (w.theta2 - w.theta1) / 2.0 + w.theta1\n        x = 0.70 * np.cos(np.deg2rad(ang))\n        y = 0.70 * np.sin(np.deg2rad(ang))\n        if is_pct:\n            ax.text(x, y, f"{v:g}%", ha="center", va="center",\n                    fontsize=9, color="white", weight="bold")\n        else:\n            share = 100.0 * v / total\n            ax.text(x, y, f"{v:g}{unit}\\n{share:.1f}%", ha="center", va="center",\n                    fontsize=8, color="white", weight="bold")\n    return True\n\n\ndef render_scatter(ax, spec, rows):\n    xs = [norm(r.get("year")) for r in rows]\n    ys = [norm(r.get("value")) for r in rows]\n    if not any(x is not None and y is not None for x, y in zip(xs, ys)):\n        # 无年份 → 以序号为横轴，标签做刻度\n        labels = [short_label(r) for r in rows]\n        xs = list(range(len(rows)))\n        ax.scatter(xs, ys, s=48, color=COLOR_BLIND_PALETTE[0], edgecolor="white")\n        ax.set_xticks(xs)\n        ax.set_xticklabels(labels, rotation=25, fontsize=8)\n        ax.set_xlabel(str(spec.get("x_axis_title") or "类别/序号"))\n    else:\n        ax.scatter(xs, ys, s=48, color=COLOR_BLIND_PALETTE[0], edgecolor="white")\n        ax.set_xlabel(str(spec.get("x_axis_title") or "年份/序号"))\n    ax.set_ylabel(str(spec.get("y_axis_title") or "数值"))\n    ax.grid(alpha=0.25)\n    return True\n\n\ndef main():\n    with open("chart_data.json", encoding="utf-8") as f:\n        payload = json.load(f)\n    specs = payload.get("charts")\n    if specs is None:\n        rows = payload.get("data") or []\n        specs = wrap_rows_to_specs([r for r in rows if isinstance(r, dict)])\n    specs = [s for s in specs if isinstance(s, dict)]\n    manifest = []\n    idx = 0\n    for i, spec in enumerate(specs):\n        issues = validate_spec(spec)\n        if issues:\n            print(f"SKIP chart#{i}: {\'; \'.join(issues)}", flush=True)\n            continue\n        rows = [r for r in spec.get("data") or [] if isinstance(r, dict)]\n        ctype = str(spec.get("type") or "bar")\n        conclusion = str(spec.get("conclusion") or "").strip()\n        title = str(spec.get("title") or "").strip()\n        if len(rows) < 2:\n            print(f"SKIP chart#{i}: 数据点少于 2 个，单点图无结论（{title}）", flush=True)\n            continue\n        fig, ax = plt.subplots(figsize=(9.5, 5.6))\n        fig.subplots_adjust(bottom=0.34)\n        ok = False\n        if ctype == "pie":\n            ok = render_pie(ax, spec, rows)\n        elif ctype == "horizontal_bar":\n            ok = render_bar(ax, spec, rows, horizontal=True)\n        elif ctype == "line":\n            ok = render_line(ax, spec, rows)\n        elif ctype == "scatter":\n            ok = render_scatter(ax, spec, rows)\n        else:\n            ok = render_bar(ax, spec, rows, horizontal=False)\n        if not ok:\n            print(f"SKIP chart#{i}: 数据无法支撑 {ctype} 图（{title}）", flush=True)\n            plt.close(fig)\n            continue\n        ax.set_title(title or "图表", fontsize=12, pad=10)\n        add_footer(fig, spec, rows, conclusion)\n        idx += 1\n        fname = f"chart_{idx}.png"\n        # 图表视觉质量 QA + 自动修复（无视觉 LLM 的确定性替代）：\n        # 标签重叠/图例遮挡/字号过小 → 自动调整并重渲染\n        from chart_qa import render_with_qa\n        residual = render_with_qa(fig, ax, fname, dpi=120)\n        if residual:\n            print(f"QA issue {fname}: {\'; \'.join(r[\'detail\'] for r in residual[:3])}", flush=True)\n        plt.close(fig)\n        manifest.append({\n            "file": fname,\n            "keywords": keywords_for(spec),\n            "section_hint": str(spec.get("section_hint") or ""),\n        })\n        print(f"RENDERED {fname}: {title}", flush=True)\n    # 合并写入：保留既有条目（含 make_charts 语义图回填），仅更新/追加 chart_N\n    try:\n        with open("chart_manifest.json", "r", encoding="utf-8") as f:\n            prev = json.load(f).get("charts") or []\n    except Exception:\n        prev = []\n    prev = [c for c in prev if isinstance(c, dict)]\n    prev_files = {str(c.get("file") or "") for c in prev}\n    for c in manifest:\n        if str(c.get("file") or "") in prev_files:\n            prev = [c if str(x.get("file") or "") == str(c.get("file") or "") else x for x in prev]\n        else:\n            prev.append(c)\n    with open("chart_manifest.json", "w", encoding="utf-8") as f:\n        json.dump({"charts": prev}, f, ensure_ascii=False, indent=1)\n    print(f"total={len(manifest)} skipped={len(specs) - len(manifest)}", flush=True)\n\n\nif __name__ == "__main__":\n    main()\n'

MAKE_CHARTS_SCRIPT = '# -*- coding: utf-8 -*-\n"""从清洗后的 clean_chart_data.json 绘制探索性图表（搜索→清洗→绘图）。\n绘图只接收结构化数据，不直接解析 search_results.json 原始文本。\n被跳过的图会打印原因（SKIP），便于用户/日志了解发生了什么。"""\nimport json\nimport re\nimport sys\nfrom collections import Counter\nimport matplotlib\nmatplotlib.use("Agg")\nimport matplotlib.pyplot as plt\n\nsys.path.insert(0, r"__REPO_ROOT__")\nfrom chart_fonts import configure_zh_font\nfrom chart_assembly import _group_financial_rows\nconfigure_zh_font()\n\nclean = json.load(open("clean_chart_data.json", encoding="utf-8"))\n\n\ndef short_label(s, n=14):\n    s = str(s)\n    return s if len(s) <= n else s[: n - 1] + "…"\n\n\ndef save_qa(fig, ax, name):\n    """渲染 + 视觉质量 QA + 自动修复（标签重叠/图例遮挡/字号过小）。"""\n    from chart_qa import render_with_qa\n    residual = render_with_qa(fig, ax, name, dpi=110)\n    if residual:\n        print(f"QA {name}: {\'; \'.join(r[\'detail\'] for r in residual[:2])}", flush=True)\n\n\nNOISE_LABELS = ("·", "报告", "摘要", "分析", "统计及", " -", "—", "–")\nSUBJECT_HINTS = ("芯片", "GPU", "TPU", "ASIC", "半导体", "出货量",\n                 "收入", "规模", "增速", "侧", "端", "市场")\n\n\ndef is_noise_label(s):\n    return any(p in str(s) for p in NOISE_LABELS)\n\n\ndef clean_rows(rows, require_type=None):\n    """过滤噪音 label；market_data 只保留 type=market_size（排除 AI 整体市场）。"""\n    out = []\n    for r in rows or []:\n        if is_noise_label(r.get("label")):\n            continue\n        if require_type and r.get("type") != require_type:\n            continue\n        out.append(r)\n    return out\n\n\n# 1) 主要主体提及频率（信息量大且稳定：只要有检索结果即可画）\nentity_freq = Counter({k: v for k, v in clean.get("entity_frequency", {}).items()})\ntop_e = entity_freq.most_common(10)\nif len(top_e) >= 3 and len(set(c for _, c in top_e)) > 1:\n    fig, ax = plt.subplots(figsize=(10, 5))\n    pairs_e = top_e[::-1]  # 条形与标签共用同一反转顺序，防止索引错位\n    ax.barh([short_label(e) for e, _ in pairs_e], [c for _, c in pairs_e],\n            color="#0ea5e9", edgecolor="white")\n    ax.set_xlabel("提及次数")\n    ax.set_title("检索资料中的主要主体提及频率（厂商/区域）")\n    for i, (_, c) in enumerate(pairs_e):\n        ax.text(c + 0.1, i, str(c), va="center", fontsize=9)\n    save_qa(fig, ax, "entity_frequency.png")\n    plt.close()\nelse:\n    print(f"SKIP 主体提及频率图：有效实体不足或无区分度（{len(top_e)} 条）")\n\n# 2) 数据来源分布（X=来源域名, Y=结果数）\ndomains = Counter({k: v for k, v in clean.get("source_distribution", {}).items()})\ntop = domains.most_common(8)\n# 若所有来源计数相同（如每源仅 1 条）→ 无信息增量，跳过该图\nif top and len(set(c for _, c in top)) > 1:\n    fig, ax = plt.subplots(figsize=(8, 4.5))\n    ax.barh([d for d, _ in top][::-1], [c for _, c in top][::-1],\n            color="#8b5cf6", edgecolor="white")\n    ax.set_xlabel("结果数")\n    ax.set_title("数据来源分布（检索结果）")\n    save_qa(fig, ax, "source_distribution.png")\n    plt.close()\nelse:\n    print(f"SKIP 数据来源分布图：来源计数全部相同或为空（{len(top)} 个来源），无信息增量。")\n\n# 3) 主题热词（X=热词, Y=出现次数）\nwords = Counter({k: v for k, v in clean.get("topic_terms", {}).items()})\ntop_w = words.most_common(12)\nif top_w and len(set(c for _, c in top_w)) > 1:\n    fig, ax = plt.subplots(figsize=(10, 5))\n    pairs_w = top_w[::-1]  # 条形与标签共用同一反转顺序\n    ax.barh([short_label(w) for w, _ in pairs_w], [c for _, c in pairs_w],\n            color="#06b6d4", edgecolor="white")\n    for i, (_, c) in enumerate(pairs_w):\n        ax.text(c + 0.1, i, str(c), va="center", fontsize=9)\n    ax.set_xlabel("出现次数")\n    ax.set_title("检索资料主题热词")\n    save_qa(fig, ax, "topic_terms.png")\n    plt.close()\nelse:\n    print(f"SKIP 主题热词图：热词不足或无区分度（{len(top_w)} 条）")\n\n# 4) 财务/市场规模（结构化源）：优先"年份 × 指标"面板数据 → 每指标一张折线子图，\n#    避免把 12 年 × 6-8 个指标塞进单张柱状图（70+ 柱子不可读）。\n#    财务行（营收/净利…）可能被标为 ai_overall，按"带年份"识别，不再按 type 过滤。\n#    多实体标签（"宁德时代2024年营收"）先归一化出指标名，再按 (实体, 指标) 分组，\n#    同指标不同实体各画一条折线，避免双实体同年份互相覆盖。\nmarket = clean_rows(clean.get("market_data"))\nmetric_groups = {\n    mname: by_entity\n    for mname, by_entity in _group_financial_rows(market).items()\n    if len({p[0] for pts in by_entity.values() for p in pts}) >= 3\n}\nif len(metric_groups) >= 2:\n    names = sorted(\n        metric_groups,\n        key=lambda k: -sum(len(pts) for pts in metric_groups[k].values()),\n    )\n    fig, axes = plt.subplots(len(names), 1, figsize=(11, 3.0 * len(names)))\n    if hasattr(axes, "ravel"):\n        axes = axes.ravel()\n    elif not isinstance(axes, (list, tuple)):\n        axes = [axes]\n    for ax, mname in zip(axes, names):\n        by_entity = metric_groups[mname]\n        colors = ("#0ea5e9", "#f59e0b", "#10b981", "#8b5cf6", "#ef4444")\n        for idx, (ent, pts) in enumerate(sorted(by_entity.items())):\n            xs = [p[0] for p in pts]\n            ys = [p[1] for p in pts]\n            ax.plot(\n                xs, ys, marker="o", linewidth=2,\n                color=colors[idx % len(colors)], label=ent or None,\n            )\n            for x, y in zip(xs, ys):\n                ax.text(x, y, f"{y:g}", ha="center", va="bottom", fontsize=7)\n        ax.set_title(f"{mname}（亿元）", fontsize=10)\n        ax.set_xlabel("年份")\n        ax.grid(alpha=0.3)\n        if len(by_entity) >= 2:\n            ax.legend()\n    fig.suptitle("财务指标趋势（结构化数据，亿元）", fontsize=12)\n    save_qa(fig, axes[0], "financial_trends.png")\n    plt.close()\nelif len(market) >= 2:\n    # 非年份类市场规模数据（AI 芯片市场等）→ 按单位分组柱状图；排除 AI 整体市场\n    bar_market = [m for m in market if m.get("type") == "market_size"]\n    by_unit = {}\n    for m in bar_market:\n        by_unit.setdefault(str(m.get("unit") or "?"), []).append(m)\n    best = max(by_unit.values(), key=len)\n    if len(best) >= 2:\n        items_m = best\n        # 与 bar 标签共用同一顺序，杜绝错位\n        labels = [short_label(m.get("label") or "?") for m in items_m]\n        vals = [float(m.get("value") or 0) for m in items_m]\n        fig, ax = plt.subplots(figsize=(9, 5))\n        ax.bar(labels, vals, color="#f59e0b", edgecolor="white")\n        ax.set_ylim(bottom=0)\n        ax.set_xlabel("细分市场/区域")\n        ax.set_ylabel(f"规模（{best[0].get(\'unit\')}）")\n        ax.set_title("市场规模（清洗后结构化数据）")\n        for i, v in enumerate(vals):\n            ax.text(i, v, f"{v:g}", ha="center", va="bottom", fontsize=9)\n        save_qa(fig, ax, "market_data.png")\n        plt.close()\n    else:\n        print(f"SKIP 市场规模图：按单位分组后最多的仅 {len(best)} 条（{len(market)} 条总数据）")\nelse:\n    print(f"SKIP 市场规模图：有效结构化数据不足（当前 {len(market)} 条），无法生成对比图。")\n\n# 5) 市场份额（%）\nshares = clean_rows(clean.get("market_share"))\nif len(shares) >= 2 and len(set(c.get("value") for c in shares)) > 1:\n    fig, ax = plt.subplots(figsize=(9, 5))\n    pairs_s = sorted(shares, key=lambda x: x.get("value", 0), reverse=True)\n    labels_s = [short_label(x.get("label") or "?") for x in pairs_s]\n    vals_s = [float(x.get("value") or 0) for x in pairs_s]\n    ax.bar(labels_s, vals_s, color="#10b981", edgecolor="white")\n    ax.set_ylim(bottom=0)\n    ax.set_xlabel("主体")\n    ax.set_ylabel("份额（%）")\n    ax.set_title("市场份额/占比（清洗后结构化数据）")\n    for i, v in enumerate(vals_s):\n        ax.text(i, v, f"{v:g}%", ha="center", va="bottom", fontsize=9)\n    save_qa(fig, ax, "market_share.png")\n    plt.close()\nelse:\n    print(f"SKIP 市场份额图：有效数据不足或无区分度（{len(shares)} 条）")\n\n# 6) 宏观指标（Token/台/辆/次 等非货币单位）\nmacros = clean.get("macro_indicators") or []\nif len(macros) >= 2 and len(set(c.get("value") for c in macros)) > 1:\n    fig, ax = plt.subplots(figsize=(9, 5))\n    pairs_m = sorted(macros, key=lambda x: x.get("value", 0), reverse=True)\n    labels_m = [f"{short_label(x.get(\'label\') or \'?\')}（{x.get(\'unit\')}）" for x in pairs_m]\n    vals_m = [float(x.get("value") or 0) for x in pairs_m]\n    ax.bar(labels_m, vals_m, color="#6366f1", edgecolor="white")\n    ax.set_ylim(bottom=0)\n    ax.set_ylabel("数值")\n    ax.set_title("宏观指标（调用量/出货量/渗透率等）")\n    for i, v in enumerate(vals_m):\n        ax.text(i, v, f"{v:g}", ha="center", va="bottom", fontsize=9)\n    save_qa(fig, ax, "macro_indicators.png")\n    plt.close()\nelse:\n    print(f"SKIP 宏观指标图：有效数据不足或无区分度（{len(macros)} 条）")\n\n# 7) 市场趋势（同比/环比 ±%，下降为负）；label 过短且无主体词的条目跳过\ntrends = [\n    t for t in clean_rows(clean.get("market_trends"))\n    if len(str(t.get("label") or "")) > 4 or any(h in str(t.get("label")) for h in SUBJECT_HINTS)\n]\nif len(trends) >= 2 and len(set(c.get("value") for c in trends)) > 1:\n    fig, ax = plt.subplots(figsize=(9, 5))\n    pairs_t = sorted(trends, key=lambda x: x.get("value", 0), reverse=True)\n    labels_t = [short_label(x.get("label") or "?") for x in pairs_t]\n    vals_t = [float(x.get("value") or 0) for x in pairs_t]\n    colors_t = ["#ef4444" if v < 0 else "#10b981" for v in vals_t]\n    ax.bar(labels_t, vals_t, color=colors_t, edgecolor="white")\n    ax.axhline(0, color="#888888", linewidth=0.8)\n    ax.set_ylabel("同比/环比变化（%）")\n    ax.set_title("市场趋势（同比增长/下降）")\n    for i, v in enumerate(vals_t):\n        ax.text(i, v, f"{v:g}%", ha="center", va="bottom" if v >= 0 else "top", fontsize=9)\n    save_qa(fig, ax, "market_trends.png")\n    plt.close()\nelse:\n    print(f"SKIP 市场趋势图：有效数据不足或无区分度（{len(trends)} 条）")\n\nprint("charts generated")\n'
