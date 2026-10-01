# -*- coding: utf-8 -*-
"""U1/U2 底稿三图：把**已验证运行**翻译成可渲染的图表规格。

三图 = ①利润瀑布（经营驱动桥）②现金桥（补充资料调节桥）③情景比较（反向阈值）。
每条规格都满足 `chart_specs.validate_spec`（标题/单位/来源/轴标题/结论齐全），可直接交给
既有确定性渲染脚本（`chart_assembly.RENDER_CHART_SCRIPT`，含 `waterfall` 一类）出 PNG；
图与运行**同身份**（`run_id` + `dataset_hash` 写进来源与规格）。

纪律：
- 运行状态不是 VALIDATED → `available=False` + 原因，不画"大概"；
- 瀑布只画**能闭合**的桥：分项口径或显示口径任一不闭合就拒绝出图；
- 亿元只用于显示（两位小数），闭合判定用**元**的精确值；
- 结论句由读数拼出（带具体数字），不写"随年份变化"这类同义反复。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .contracts import RunStatus

YI = Decimal("100000000")
_TOL_YUAN = Decimal("1000000")        # 1e6 元：分项口径闭合容差
_TOL_SHOWN_YI = Decimal("0.03")       # 显示舍入容差（亿元）

# 调节项分组的中文名（图注用；与报告卡的 GROUP_NOTE 同一套分组）
_GROUP_CN = {
    "non_cash": "非现金项（折旧摊销/减值/递延税/公允价值等）",
    "working_capital": "营运资本项（存货与经营性应收应付的增减）",
    "other": "其他调节项",
}


def _d(value) -> Decimal:
    return Decimal(str(value))


def _yi(value) -> float:
    return float((_d(value) / YI).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _yi_s(value) -> str:
    """亿元读数（带符号，两位小数）——先算成字符串再进 f-string（3.11 兼容）。"""
    return f"{_yi(value):+,.2f}"


def _unavailable(run, reason: str) -> dict:
    return {"available": False, "reason": reason,
            "run_id": str(getattr(run, "run_id", "") or "")}


def _gate(run) -> str:
    if run is None:
        return "没有可用的运行"
    status = str(getattr(run, "status", "") or "")
    if status != str(RunStatus.VALIDATED):
        return f"运行未通过独立验证（{status}）：不出图"
    return ""


def _out(run, metric):
    return next((o for o in (getattr(run, "outputs", None) or ())
                 if o.metric == metric), None)


def _source(run, statements: str) -> str:
    """图注里的**简短来源**（V0：运行标识不再挤在图上，移到正文底稿索引与包内 analysis/）。

    规格里的 `run_id` / `dataset_hash` 字段仍然带着运行身份，供底稿与证据回查；
    只有**画在图上、写进正文图注**的这段文字保持简短。
    """
    return f"{statements}"


def _clip(text, limit: int = 12) -> str:
    """图注里的长标签截断（披露标签常有二三十字，图注一行放不下会挤掉后半句）。"""
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _diag(run) -> dict:
    for o in (getattr(run, "outputs", None) or ()):
        if getattr(o, "diagnostics", None):
            return dict(o.diagnostics)
    return {}


def _entity(run) -> str:
    diag = _diag(run)
    return str(diag.get("entity") or diag.get("entity_id") or "")


# ------------------------------------------------------------------ ① 利润瀑布

def profit_waterfall(run, dataset, *, top_n: int = 6,
                     question: str = "归母净利润同比变化由哪些金额构成？") -> dict:
    """经营驱动桥 → 利润瀑布：起点归母净利 → 逐项贡献 → 终点归母净利。

    `top_n` 之外的贡献**合并成一项**（合计值仍是精确和，不是估数），避免十几项挤成毛刺。
    """
    why = _gate(run)
    if why:
        return _unavailable(run, why)
    bridge = _out(run, "net_profit_change")
    detail = _out(run, "net_profit_change_detail")
    if bridge is None or detail is None:
        return _unavailable(run, "缺少 net_profit_change / net_profit_change_detail 输出")
    diag = _diag(run)
    periods = list(diag.get("periods") or ())
    if len(periods) < 2:
        return _unavailable(run, "运行诊断没有给出两期期间")
    prev_p, cur_p = str(periods[0]), str(periods[1])
    try:
        np_prev = _d(dataset.require("net_profit", prev_p).value)
        np_cur = _d(dataset.require("net_profit", cur_p).value)
    except Exception as exc:                                  # noqa: BLE001
        return _unavailable(run, f"取两期归母净利失败：{str(exc)[:80]}")
    gp = next((c for c in (bridge.components or ())
               if c.get("component_id") == "gross_profit_change"), None)
    if gp is None:
        return _unavailable(run, "桥里找不到毛利变化分项")

    items = [{"label": "毛利变化", "value": _d(gp.get("value"))}]
    rest = sorted((c for c in (detail.components or ())),
                  key=lambda c: -abs(_d(c.get("value"))))
    rest = [c for c in rest if _d(c.get("value")) != 0]
    for c in rest[:top_n]:
        items.append({"label": str(c.get("label") or c.get("component_id")),
                      "value": _d(c.get("value"))})
    tail = rest[top_n:]
    if tail:
        items.append({"label": f"其余 {len(tail)} 项合计（未单列）",
                      "value": sum((_d(c.get("value")) for c in tail), Decimal("0"))})

    total = sum((i["value"] for i in items), Decimal("0"))
    real = np_cur - np_prev
    if abs(total - real) > _TOL_YUAN:
        gap_s = _yi_s(total - real)
        return _unavailable(run, f"桥不闭合（分项与净利变化差 {gap_s} 亿元）：不画瀑布")

    rows = [{"label": f"{prev_p}归母净利", "value": _yi(np_prev),
             "unit": "亿元", "kind": "base"}]
    rows += [{"label": i["label"], "value": _yi(i["value"]),
              "unit": "亿元", "kind": "delta"} for i in items]
    rows.append({"label": f"{cur_p}归母净利", "value": _yi(np_cur),
                 "unit": "亿元", "kind": "total"})
    shown_in = sum((_d(r["value"]) for r in rows[1:-1]), Decimal("0"))
    shown_out = _d(rows[-1]["value"]) - _d(rows[0]["value"])
    if abs(shown_in - shown_out) > _TOL_SHOWN_YI:
        return _unavailable(run, "显示口径（亿元两位小数）下不闭合：不画瀑布")

    scale, margin = _out(run, "revenue_scale_effect"), _out(run, "gross_margin_effect")
    below = _out(run, "below_gross_line_change")
    start_s, end_s = f"{np_prev / YI:,.2f}", f"{np_cur / YI:,.2f}"
    delta_s, gp_s = _yi_s(real), _yi_s(gp.get("value"))
    # 结论控制在 ~140 字内：渲染脚本的图注会截断「结论」行（超过就看不清后半句），
    # 完整口径写在 annotation 与运行记录里。
    parts = [f"归母净利润 {start_s} → {end_s} 亿元（{delta_s}）",
             f"毛利变化 {gp_s} 亿元"]
    if scale is not None and margin is not None:
        parts.append(f"规模 {_yi_s(scale.value)}／毛利率 {_yi_s(margin.value)} 亿元")
    if below is not None:
        parts.append(f"毛利线以下 {_yi_s(below.value)} 亿元")
    resid = _d(diag.get("unexplained_residual_yuan") or 0)
    parts.append("未解释差额 0.00 亿元（披露项目齐全）" if resid == 0
                 else f"未解释差额 {_yi_s(resid)} 亿元（未取得的披露项目，不摊派）")
    missing = list(diag.get("line_items_missing") or ())
    rejected = list(diag.get("line_items_rejected") or ())

    return {
        "available": True,
        "type": "waterfall",
        "chart_id": f"{str(getattr(run, 'run_id', ''))[:12]}-profit-waterfall",
        "title": f"{_entity(run)} {prev_p}→{cur_p} 归母净利润瀑布（亿元）",
        "question": question,
        "conclusion": "；".join(parts),
        "unit": "亿元",
        "source": _source(run, "公司年报：合并利润表与经营分析（分产品/分地区/期间费用）"),
        "x_axis_title": "项目（起点→贡献→终点）",
        "y_axis_title": "金额（亿元）",
        "time_range": f"{prev_p}—{cur_p}",
        "region": "中国",
        "sample_size": len(rows),
        "missing": ("未取得项目：" + "、".join(map(str, missing))) if missing else "无",
        "outliers": ("被拒绝的输入：" + "、".join(map(str, rejected))) if rejected else "无",
        "annotation": ("亿元为四舍五入两位；闭合判定用元值。贡献项是会计分解，"
                       "业务原因需另找披露依据"),
        "data": rows,
        "run_id": str(getattr(run, "run_id", "") or ""),
        "dataset_hash": str(getattr(run, "dataset_hash", "") or ""),
    }


# ------------------------------------------------------------------ ② 现金桥

def cash_bridge_waterfall(run, *, which: str = "cur",
                          question: str = "合并净利润到经营现金流之间的调节桥在哪里？") -> dict:
    """补充资料调节桥 → 现金瀑布：合并净利润 → 三组调节项 → 经营现金流。"""
    why = _gate(run)
    if why:
        return _unavailable(run, why)
    metric = f"operating_cashflow_reconciliation_{which}"
    bridge = _out(run, metric)
    if bridge is None:
        return _unavailable(run, f"缺少 {metric} 输出")
    comps = list(bridge.components or ())
    start = next((c for c in comps if c.get("component_id") == "consolidated_net_profit"), None)
    if start is None:
        return _unavailable(run, "调节桥里找不到起点（合并净利润）")
    deltas = [c for c in comps if c is not start]
    deltas = [c for c in deltas if _d(c.get("value")) != 0]
    cash = _d(bridge.value)
    total = _d(start.get("value")) + sum((_d(c.get("value")) for c in deltas), Decimal("0"))
    if abs(total - cash) > _TOL_YUAN:
        gap_s = _yi_s(total - cash)
        return _unavailable(run, f"调节桥不闭合（与经营现金流差 {gap_s} 亿元）：不画瀑布")

    period = str(bridge.output_period or "")
    rows = [{"label": f"{period}合并净利润", "value": _yi(start.get("value")),
             "unit": "亿元", "kind": "base"}]
    for c in deltas:
        rows.append({"label": str(c.get("label") or c.get("component_id")),
                     "value": _yi(c.get("value")), "unit": "亿元", "kind": "delta"})
    rows.append({"label": f"{period}经营现金流", "value": _yi(cash),
                 "unit": "亿元", "kind": "total"})

    diag = _diag(run)
    rec = (diag.get("reconciliation") or {}).get(period) or {}
    groups = rec.get("groups") or {}
    short_cn = {"non_cash": "非现金项", "working_capital": "营运资本项", "other": "其他"}
    group_full = "、".join(f"{_GROUP_CN.get(k, k)} {_yi_s(v)}" for k, v in groups.items())
    sup = diag.get("largest_support") or {}
    drag = diag.get("largest_drag") or {}
    start_v = _d(start.get("value"))
    parts = [f"合并净利润 {start_v / YI:,.2f} 亿元 ＋ 调节项 {_yi_s(cash - start_v)} 亿元 "
             f"＝ 经营现金流 {cash / YI:,.2f} 亿元",
             f"未解释差额 {_yi_s(rec.get('residual_yuan') or 0)} 亿元"]
    if sup:
        parts.append(f"最大支撑 {_clip(sup.get('label'))} {_yi_s(sup.get('value') or 0)} 亿元")
    if drag:
        parts.append(f"最大拖累 {_clip(drag.get('label'))} {_yi_s(drag.get('value') or 0)} 亿元")
    # V1：图上也要能看出"现金为什么变了"——把同一次运行的 ΔOCF 桥最大构成写进图注
    chg = _out(run, "operating_cashflow_change")
    if chg is not None and (chg.components or ()):
        top = sorted((chg.components or ()),
                     key=lambda c: -abs(_d(c.get("value")))) [0]
        parts.append(f"经营现金流变化 {_yi_s(chg.value)} 亿元，最大构成 "
                     f"{_clip(top.get('label'), 16)} {_yi_s(top.get('value'))} 亿元")
    miss = (diag.get("items_missing") or {}).get(which) or []
    resid = _d(rec.get("residual_yuan") or 0)

    return {
        "available": True,
        "type": "waterfall",
        "chart_id": f"{str(getattr(run, 'run_id', ''))[:12]}-cash-bridge-{which}",
        "title": f"{_entity(run)} {period} 净利润→经营现金流桥（亿元）",
        "question": question,
        "conclusion": "；".join(parts),
        "unit": "亿元",
        "source": _source(run, "公司年报：现金流量表补充资料（将净利润调节为经营活动现金流量）"),
        "x_axis_title": "项目（起点→调节项→终点）",
        "y_axis_title": "金额（亿元）",
        "time_range": period,
        "region": "中国",
        "sample_size": len(rows),
        "missing": ("未取得调节项：" + "、".join(map(str, miss))) if miss else "无",
        "outliers": "无",
        "annotation": (("对账差额≈0 表示披露调节项与经营现金流自洽；差额非零时不得写"
                        "“完整调节”。调节项是会计加回/扣减，不表示经济原因")
                       + (f"。分组明细：{group_full}" if group_full else "")),
        "data": rows,
        "run_id": str(getattr(run, "run_id", "") or ""),
        "dataset_hash": str(getattr(run, "dataset_hash", "") or ""),
    }


# ------------------------------------------------------------ ③ 情景（反向阈值）比较

def scenario_outcome_bars(run, *,
                          question: str = "同一组假设下，基期、使用者情景与反向对照差多少？") -> dict:
    """**一次情景运行**的三个读数并排（基准／使用者情景／反向对照）——不额外跑参数。

    为什么需要它：正常任务里情景模型只跑一条（计划每个模型采一次），若第三张图必须"多档
    对比"就只能偷偷再跑参数——本函数改为把**同一次运行**里已验证的三个情景读数画出来；
    正文与图因此共用同一次运行（阶段V 的硬要求）。多档阈值比较仍由
    `scenario_threshold_comparison` 在**已选定多档运行**时使用。
    """
    why = _gate(run)
    if why:
        return _unavailable(run, why)
    out = _out(run, "scenario_net_profit")
    if out is None or not (out.components or ()):
        return _unavailable(run, "缺少 scenario_net_profit 的三情景读数")
    rows: list[dict] = []
    for c in (out.components or ()):
        rows.append({"label": _short_scenario_name(c.get("label")),
                     "value": _yi(c.get("value")), "unit": "亿元", "kind": "delta"})
    if len(rows) < 2:
        return _unavailable(run, "情景读数少于两个：不画比较图")
    base = next((r for r in rows if "基准" in r["label"]), None)
    user = next((r for r in rows if "使用者" in r["label"]), rows[1] if len(rows) > 1 else None)
    parts = []
    if base is not None:
        parts.append(f"基准复现 {base['value']:,.2f} 亿元")
    if user is not None:
        parts.append(f"使用者情景 {user['value']:,.2f} 亿元"
                     f"（较基准 {user['value'] - (base or user)['value']:+,.2f} 亿元）")
    parts.append("情景是**条件计算**（假设成立时才成立），不是预测、无概率")
    return {
        "available": True,
        "type": "bar",
        "chart_id": f"{str(getattr(run, 'run_id', ''))[:12]}-scenario-outcome",
        "title": f"{_entity(run)} {_scenario_period(run)} 情景比较（亿元）",
        "question": question,
        "conclusion": "；".join(parts),
        "unit": "亿元",
        "source": _source(run, "公司年报：营业收入/营业成本/归母净利润与毛利线以下净额"),
        "x_axis_title": "情景（同一组假设）",
        "y_axis_title": "归母净利润（亿元）",
        "time_range": str(getattr(out, "output_period", "") or ""),
        "region": "中国",
        "sample_size": len(rows),
        "missing": "无",
        "outliers": "无",
        "annotation": ("基准＝参数全 0 复现基期；反向对照＝同一收入假设下再降毛利率 1pp。"
                       "毛利线以下净额含费用/税项/投资收益/少数股东，不是纯费用"),
        "data": rows,
        "run_id": str(getattr(run, "run_id", "") or ""),
        "dataset_hash": str(getattr(run, "dataset_hash", "") or ""),
    }


def _short_scenario_name(label) -> str:
    """情景分项标签 → 短名（基准／使用者情景／反向对照＋假设摘要）。"""
    s = str(label or "").strip()
    head = s.split("（")[0].strip() or s
    if "（" in s and s.endswith("）"):
        inner = s[s.index("（") + 1:-1]
        if "（" in inner and inner.endswith("）"):
            inner = inner[inner.index("（") + 1:-1]
        inner = inner.split("，")[0]
        inner = inner.replace("／隐含块 +0.00%", "").replace("隐含块 +0.00%", "").strip("／ ")
        if inner and "参数 0" not in inner:
            head = f"{head}（{inner}）"
    return _clip(head, 22)


def _scenario_period(run) -> str:
    out = _out(run, "scenario_net_profit")
    period = str(getattr(out, "output_period", "") or "")
    return period.split("（")[0] or period


def scenario_threshold_comparison(variants, *,
                                  question: str = "收入假设变化时，要保住基期利润需要多高的毛利率？") -> dict:
    """反向情景比较：`variants=[(标签, 运行), …]` → 每个档位一对柱子（%）。

    两列都是**百分点口径**：基期毛利率与「维持基期归母净利所需毛利率」。
    基期毛利率由同一运行的两条已验证输出回推（所需毛利率 − 所需与基期之差），
    并与诊断里的 `thresholds.margin_base` 交叉核对，不一致就拒绝出图。
    """
    items = [(str(label), run) for label, run in (variants or ())]
    if len(items) < 2:
        return _unavailable(items[0][1] if items else None,
                            "情景比较至少需要两个假设档位")
    rows: list[dict] = []
    labels: list[str] = []
    notes: list[str] = []
    first = None
    bases: set[float] = set()
    for label, run in items:
        why = _gate(run)
        if why:
            return _unavailable(run, f"{label}：{why}")
        th = _out(run, "margin_threshold_to_hold_base_profit")
        gap = _out(run, "margin_gap_to_threshold_pp")
        if th is None or gap is None or th.value is None or gap.value is None:
            return _unavailable(run, f"{label}：缺少反向阈值输出（所需毛利率/与基期之差）")
        if "%" not in str(th.unit or ""):
            return _unavailable(run, f"{label}：所需毛利率单位不是百分比（{th.unit}）")
        base = _d(th.value) - _d(gap.value)                 # 由两条已验证输出回推
        diag_th = (_diag(run).get("thresholds") or {})
        mb = diag_th.get("margin_base")
        if mb is None:
            return _unavailable(run, f"{label}：运行诊断没给出基期毛利率，无法交叉核对")
        if abs(float(base) / 100.0 - float(mb)) > 0.0002:
            return _unavailable(run, f"{label}：回推的基期毛利率与诊断不一致")
        # 画出来的基期毛利率取诊断的**未取整值**（回推值受两条输出各自两位小数影响，
        # 同一基期会出现 73.15/73.16 的抖动）；两者已交叉核对到 0.02pp 以内。
        base = (Decimal(str(mb)) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        bases.add(float(base))
        rows.append({"label": label, "value": round(float(base), 2), "unit": "%",
                     "caliber": "基期毛利率"})
        rows.append({"label": label, "value": round(float(th.value), 2), "unit": "%",
                     "caliber": "维持基期净利所需毛利率"})
        labels.append(label)
        notes.append(f"{label} 需 {float(th.value):.2f}%（差 {float(gap.value):+.2f}pp）")
        first = first or run

    base_pairs = [(lab, r) for lab, r in items]
    if len({str(getattr(r, "dataset_hash", "") or "") for _, r in items}) > 1:
        return _unavailable(items[0][1], "各档位不是同一数据集：不可对比")
    if len(bases) > 1:
        return _unavailable(items[0][1], "各档位的基期毛利率不一致（基期不同）：不可对比")
    head = base_pairs[0][0]
    tail = base_pairs[-1][0]
    dir_s = (f"收入越低，维持同样利润所需毛利率越高（单因素反推：给出“需要什么”，"
             "不表示可达、也不是预测）")
    concl = "；".join(notes) + "。" + dir_s
    if len(concl) > 140:            # 图注会截断「结论」行，超长就只说方向
        concl = "；".join(notes) + "。收入越低所需毛利率越高"
    return {
        "available": True,
        "type": "grouped_bar",
        "chart_id": f"{str(getattr(first, 'run_id', ''))[:12]}-scenario-threshold",
        "title": f"{_entity(first)} 反向情景：维持基期归母净利所需毛利率（%）",
        "question": question,
        "conclusion": concl,
        "unit": "%",
        "source": "公司年报：营业收入/营业成本/归母净利润与毛利线以下净额（正常入口抽取）",
        "x_axis_title": "假设档位（收入）",
        "y_axis_title": "毛利率（%）",
        "time_range": str(getattr(_out(first, "margin_threshold_to_hold_base_profit"),
                                  "output_period", "") or ""),
        "region": "中国",
        "sample_size": len(rows),
        "missing": "无",
        "outliers": "无",
        "annotation": ("基期毛利率取运行诊断的未取整值，并与两条已验证输出的回推值交叉核对"
                       "（≤0.02pp）；两个口径同为百分比。"
                       "毛利线以下净额含费用/税项/投资收益/少数股东，不是纯费用"),
        "data": rows,
        "run_id": str(getattr(first, "run_id", "") or ""),
        "dataset_hash": str(getattr(first, "dataset_hash", "") or ""),
        "variants": [{"label": lab, "run_id": str(getattr(run, "run_id", "") or "")}
                     for lab, run in items],
    }
