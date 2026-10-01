# -*- coding: utf-8 -*-
"""U1/U2 成篇：把**已验证运行**装配成 4–6 页经营驱动分析正文（可复算）。

为什么单独一层：分析卡（`report_adapter`）是**按模型**给的一段话，读者拿到的是几张卡；
研究报告需要的是**一条论证线**——结论 → 金额分解 → 披露支持与定位 → 替代解释 →
现金与反向情景 → 待核查 → 口径限制。本节把同一批运行按这条线重排，数字仍然只来自
`validated` 运行（每个数字带 `run_id` / `output_id` 或事实身份，可回查）。

纪律：
- 只消费 `RunStatus.VALIDATED` 的运行；缺某个模型就如实写「本次未运行/缺输入」，不补数；
- 未取到的披露项留在「未解释差额」，既不摊派也不当零；
- 切法（分产品/分行业/分地区/分销售模式）**不可相加**；均价含结构，不得称「提价」；
- 反向阈值是**单因素反推**（给出「需要什么」），不表示可达、不是预测；
- 亿元只用于显示（两位小数），闭合与占比判定用运行里的元值。
"""
from __future__ import annotations

import re

from . import charts as _charts
from .contracts import RunStatus


def _yi(value) -> str:
    """亿元读数（带符号、两位小数）——与图表共用同一套格式（不两处各自四舍五入）。

    `None`/空值（例如"本期没有最大拖累项"）如实显示为 `—`，不抛异常、也不写成 0.00。
    """
    if value is None or value == "":
        return "—"
    return _charts._yi_s(value)


def _yi_plain(value) -> str:
    return _yi(value).lstrip("+")


def _attr(obj, name, default=""):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _pct(value) -> str:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "—"


def _pct4(value) -> str:
    """阈值用 4 位小数：+15.8226% 印成 +15.82% 就没法代回求值器核对同一目标（W0）。"""
    try:
        return f"{float(value):.4f}%"
    except (TypeError, ValueError):
        return "—"


# ------------------------------------------------------------------ 事实来源定位

def _clean_label(label, cut: str = "") -> str:
    """去掉「切法:口径」前缀（切法已在标题里说过，重复打印会挤掉真正的内容）。"""
    s = str(label or "")
    if cut and s.startswith(f"{cut}:"):
        s = s[len(cut) + 1:]
    elif ":" in s and s.split(":", 1)[0] in ("分产品", "分行业", "分地区", "分销售模式"):
        s = s.split(":", 1)[1]
    return s


def locator_text(loc) -> str:
    """来源定位 → 可读位置（结构化事实的 `source_locator` 有两种形状：dict 与字符串）。"""
    if isinstance(loc, str):
        return loc.strip()
    if not isinstance(loc, dict):
        return ""
    table = loc.get("table") if isinstance(loc.get("table"), dict) else {}
    parts: list[str] = []
    for key in ("table_name", "statement", "title"):
        if table.get(key) or loc.get(key):
            parts.append(str(table.get(key) or loc.get(key)))
            break
    if table.get("group"):
        parts.append(f"{table['group']}表")
    if table.get("row_label"):
        parts.append(f"行「{table['row_label']}」")
    for key in ("page", "pages"):
        v = str(loc.get(key) or "").strip()
        if v:
            # 只有**纯页码**才套「第 N 页」；定位串里已经写着「PDF 第 75 页」时照抄
            parts.append(f"第 {v} 页" if v.isdigit() else v)
            break
    if loc.get("locator"):
        parts.append(str(loc["locator"])[:70])
    return " · ".join(dict.fromkeys(p for p in parts if p))[:160]


def provenance_from_facts(facts, label_of=None) -> dict:
    """事实集合 → `{fact_id: {label, period, value, unit, locator}}`（正文来源表用）。

    `label_of`：可选的「指标 → 中文名」函数（事实层没写 `metric_label` 时补上，避免正文里
    出现 `operating_cost` 这类内部指标名）。
    """
    out: dict = {}
    for f in facts or ():
        fid = str(_attr(f, "fact_id") or "")
        if not fid:
            continue
        metric = str(_attr(f, "metric") or "")
        label = str(_attr(f, "metric_label") or "")
        if not label and label_of is not None:
            try:
                label = str(label_of(metric) or "")
            except Exception:                     # noqa: BLE001 - 取不到就退回指标名
                label = ""
        out[fid] = {
            "label": label or metric,
            "period": str(_attr(f, "period") or ""),
            "value": _attr(f, "value", None),
            "unit": str(_attr(f, "unit") or ""),
            "locator": locator_text(_attr(f, "source_locator")),
            "url": str(_attr(f, "source_url") or ""),
        }
    return out


def observations_from_inputs(blob) -> list:
    """从 `store.load_inputs()` 的返回里取观察（`dataset.json` 的嵌套有两层）。

    形状：`{"dataset": {"schema","kind","dataset": {"manifest","observations"}, …}}`；
    也兼容"观察直接在顶层"。取不到就返回空列表（调用方据此如实写"没有可复算输入记录"）。
    """
    if not isinstance(blob, dict):
        return []
    outer = blob.get("dataset") if isinstance(blob.get("dataset"), dict) else {}
    inner = outer.get("dataset") if isinstance(outer.get("dataset"), dict) else {}
    for cand in (inner.get("observations"), outer.get("observations"),
                 blob.get("observations")):
        if isinstance(cand, list) and cand:
            return cand
    return []


def provenance_from_observations(observations) -> dict:
    """**分析数据集里**的观察 → 同一张来源表（报告链只有这一层：观察不含页码定位）。

    事实层（`facts.py` 的 `Fact.source_locator`）才有页码/表名；报告链在任务工作区里只有
    `analysis/dataset.json`（`Observation` 无 `source_locator` 字段）。所以这里如实写
    **口径与血缘**，页码定位留给材料清单——不假装有页码，也不留空列。
    """
    out: dict = {}
    for o in observations or ():
        fid = str(_attr(o, "fact_id") or "")
        if not fid:
            continue
        derived = _attr(o, "derived_from", ()) or ()
        loc = "口径 " + str(_attr(o, "caliber") or "未声明")
        formula = str(_attr(o, "formula_version") or "")
        if derived:
            loc += "；推算自 " + "、".join(str(x) for x in derived)
        if formula:
            loc += f"（{formula}）"
        out[fid] = {
            "label": str(_attr(o, "metric_label") or _attr(o, "metric") or ""),
            "period": str(_attr(o, "period") or ""),
            "value": _attr(o, "value", None),
            "unit": str(_attr(o, "unit") or ""),
            "locator": loc,
            "url": str(_attr(o, "source_url") or ""),
        }
    return out


# ------------------------------------------------------------------ 取运行

def _pick(runs, model_id: str):
    return next((r for r in (runs or ())
                 if str(_attr(r, "model_id")) == model_id), None)


def _spread(runs, model_id: str) -> list:
    return [r for r in (runs or ()) if str(_attr(r, "model_id")) == model_id]


def _out(run, metric: str):
    if run is None:
        return None
    return next((o for o in (_attr(run, "outputs") or ())
                 if str(_attr(o, "metric")) == metric), None)


def _outs(run, metric: str) -> list:
    """同一运行里**同名指标的多条输出**（分段分解每种切法一条）。"""
    if run is None:
        return []
    return [o for o in (_attr(run, "outputs") or ())
            if str(_attr(o, "metric")) == metric]


def _diag(run) -> dict:
    if run is None:
        return {}
    for o in (_attr(run, "outputs") or ()):
        d = _attr(o, "diagnostics", None)
        if d:
            return dict(d)
    return {}


def _rid(run) -> str:
    return str(_attr(run, "run_id") or "")[:12]


def _scenario_label(run, out) -> str:
    """情景档位短标签：取输出期间的括号内容，去掉「使用者情景（…）」外壳与隐含块 0.00%。

    例：`2024年（使用者情景（收入 +5.00%／毛利率 +1.00pp／隐含块 +0.00%，较基期高））`
    → `收入 +5.00%／毛利率 +1.00pp`。参数本身照抄输出期间，不由本函数改写数字。
    """
    period = str(_attr(out, "output_period") or "")
    text = period
    if "（" in period and period.endswith("）"):
        text = period[period.index("（") + 1:-1]
        if "（" in text and text.endswith("）"):
            text = text[text.index("（") + 1:-1]
    text = text.split("，")[0]
    text = text.replace("／隐含块 +0.00%", "").replace("隐含块 +0.00%", "").strip("／ ")
    return text or period or _rid(run)


def _cut_name(label: str) -> str:
    s = str(label or "")
    if "（切法：" in s and s.endswith("）"):
        return s[s.index("（切法：") + 4:-1]
    return s


def _cut_summary(seg, top: int = 2) -> str:
    cut = _cut_name(_attr(seg, "label"))
    comps = [c for c in (_attr(seg, "components") or ())]
    named = [c for c in comps if "unclassified" not in str(_attr(c, "component_id"))]
    named.sort(key=lambda c: -abs(float(_attr(c, "value") or 0)))
    shown = "、".join(f"{_clean_label(_attr(c, 'label'), cut)} {_yi(_attr(c, 'value'))} 亿元"
                      for c in named[:top])
    rest = named[top:]
    if rest:
        shown += ("；其余 " + str(len(rest)) + " 项 "
                  + _yi(sum(float(_attr(c, "value") or 0) for c in rest)) + " 亿元")
    unclass = [c for c in comps if "unclassified" in str(_attr(c, "component_id"))]
    if unclass:
        shown += f"；未分类差额 {_yi(_attr(unclass[0], 'value'))} 亿元"
    base = cut if cut.endswith("切法") else f"{cut}切法"
    return f"{base}：{shown}" if shown else str(_attr(seg, "label"))


# ------------------------------------------------------------------ 各段

def _conclusions(od, cash, scens) -> list[str]:
    items: list[str] = []
    if od is not None:
        bridge = _out(od, "net_profit_change")
        gp = next((c for c in (_attr(bridge, "components") or ())
                   if str(_attr(c, "component_id")) == "gross_profit_change"), None)
        scale, margin = _out(od, "revenue_scale_effect"), _out(od, "gross_margin_effect")
        below = _out(od, "below_gross_line_change")
        diag = _diag(od)
        periods = [str(p) for p in (diag.get("periods") or ())]
        span = f"{periods[0]}→{periods[1]} " if len(periods) >= 2 else ""
        txt = (f"**利润**：{span}归母净利润变化 {_yi(_attr(bridge, 'value'))} 亿元；"
               f"其中毛利变化 {_yi(_attr(gp, 'value'))} 亿元")
        if scale is not None and margin is not None:
            txt += (f"（收入规模 {_yi(_attr(scale, 'value'))}／毛利率 "
                    f"{_yi(_attr(margin, 'value'))} 亿元）")
        if below is not None:
            txt += f"，毛利线以下 {_yi(_attr(below, 'value'))} 亿元"
        gm = diag.get("gross_margin") or {}
        if gm:
            txt += (f"；毛利率 {_pct(float(gm.get('prev') or 0) * 100)} → "
                    f"{_pct(float(gm.get('cur') or 0) * 100)}"
                    f"（{float(gm.get('delta_pp') or 0):+.2f}pp）")
        items.append(txt)
    else:
        items.append("**利润**：本次未运行经营驱动分解（缺两期收入/成本/归母净利观察），"
                     "正文不给利润归因结论。")
    if od is not None:
        segs = _outs(od, "gross_profit_change_by_segment")
        vp = _out(od, "volume_price_decomposition")
        parts = [_cut_summary(seg) for seg in segs[:2]]
        if vp is not None:
            parts.append("量价分解（" + str(_attr(vp, "label")) + "）：" + "、".join(
                f"{_attr(c, 'label')} {_yi(_attr(c, 'value'))} 亿元"
                for c in (_attr(vp, "components") or ())))
        items.append("**结构**：" + ("；".join(parts) if parts else
                                    "本次未取到分段/量价所需口径的披露（缺成本或销量即如实跳过），"
                                    "见『待核查』。"))
    if cash is not None:
        cur = _out(cash, "operating_cashflow_reconciliation_cur")
        diag = _diag(cash)
        rec = diag.get("reconciliation") or {}
        period = str(_attr(cur, "output_period") or "")
        item = rec.get(period) or (list(rec.values())[0] if rec else {})
        sup = diag.get("largest_support") or {}
        drag = diag.get("largest_drag") or {}
        txt = (f"**现金**：{period} 合并净利润 {_yi_plain(item.get('net_profit_yuan'))} 亿元"
               f"经调节项 {_yi(item.get('adjustments_yuan'))} 亿元后为经营现金流 "
               f"{_yi_plain(item.get('cashflow_yuan'))} 亿元"
               f"（未解释差额 {_yi(item.get('residual_yuan') or 0)} 亿元）")
        chg = _out(cash, "operating_cashflow_change")
        if chg is not None:
            # 现金变化桥的**首要**分项（哪一项解释了现金变化），比"最大支撑/拖累"更贴题
            comps = sorted((_attr(chg, "components") or ()),
                           key=lambda c: -abs(float(_attr(c, "value") or 0)))
            if comps:
                txt += (f"；现金变化 {_yi(_attr(chg, 'value'))} 亿元，"
                        f"最大构成 {_attr(comps[0], 'label')} "
                        f"{_yi(_attr(comps[0], 'value'))} 亿元")
        if sup.get("label") or drag.get("label"):
            # 只有真的取到那一项才写：没有负向调节项时 `largest_drag` 为 None，
            # 不能印成“最大拖累 None — 亿元”。
            bits = []
            if sup.get("label"):
                bits.append(f"最大支撑 {sup['label']} {_yi(sup.get('value'))} 亿元")
            if drag.get("label"):
                bits.append(f"最大拖累 {drag['label']} {_yi(drag.get('value'))} 亿元")
            if bits:
                txt += "；" + "、".join(bits)
        items.append(txt)
    else:
        items.append("**现金**：本次未运行现金调节桥（缺现金流量表补充资料），"
                     "正文不给现金形成机制结论。")
    if scens:
        rows = []
        for r in scens:
            th = _out(r, "margin_threshold_to_hold_base_profit")
            gap = _out(r, "margin_gap_to_threshold_pp")
            rev_th = _out(r, "revenue_growth_to_hold_target")
            # W0：两把杠杆共用**同一目标**（默认上一期归母净利），目标写在读数前面——
            # 读者不会把"维持基期"与"恢复到上年"当成同一把杠杆（架构复核反例）。
            tgt = (_diag(r).get("thresholds") or {}).get("target_net_profit")
            tgt_s = f"（目标归母净利 {_yi_plain(tgt)} 亿元）" if tgt else ""
            bits = []
            if rev_th is not None and _attr(rev_th, "value") is not None:
                bits.append(f"收入侧需 {_pct4(_attr(rev_th, 'value'))}")
            if th is not None and _attr(th, "value") is not None:
                bits.append(f"毛利率侧需 {_pct4(_attr(th, 'value'))}"
                            + (f"（较基期 {float(_attr(gap, 'value') or 0):+.2f}pp）"
                               if gap is not None else ""))
            if bits:
                rows.append(tgt_s + "、".join(bits))
        if rows:
            items.append("**反向情景**（单因素反推，两把杠杆同一目标；不表示可达）："
                         + "；".join(rows))
    return items


def _profit_table(od, limit: int = 6) -> list[str]:
    detail = _out(od, "net_profit_change_detail")
    bridge = _out(od, "net_profit_change")
    if detail is None or bridge is None:
        return []
    total = float(_attr(bridge, "value") or 0)
    items = list(_attr(detail, "components") or ())
    items.sort(key=lambda c: -abs(float(_attr(c, "value") or 0)))
    lines = ["| 项目 | 金额（亿元） | 占归母净利变化 |",
             "|---|---:|---:|"]
    for c in items[:limit]:
        v = float(_attr(c, "value") or 0)
        share = f"{v / total * 100:.1f}%" if total else "—"
        lines.append(f"| {_attr(c, 'label')} | {_yi(v)} | {share} |")
    rest = items[limit:]
    if rest:
        rest_v = sum(float(_attr(c, "value") or 0) for c in rest)
        share = f"{rest_v / total * 100:.1f}%" if total else "—"
        lines.append(f"| 其余 {len(rest)} 项合计（未单列） | {_yi(rest_v)} | {share} |")
    lines.append("")
    lines.append("- 占比 ＝ 该项目 ÷ 归母净利变化：变化为负时，**正贡献显示为负占比**"
                 "（符号是算术结果，不是方向判断）。")
    gp = next((c for c in (_attr(bridge, "components") or ())
               if str(_attr(c, "component_id")) == "gross_profit_change"), None)
    lines.append(
        f"- 对称分解（交互项均分，代入顺序无关）：毛利变化 {_yi(_attr(gp, 'value'))} 亿元 ＝ "
        f"规模效应 {_yi(_attr(_out(od, 'revenue_scale_effect'), 'value'))} ＋ "
        f"毛利率效应 {_yi(_attr(_out(od, 'gross_margin_effect'), 'value'))} 亿元")
    for seg in _outs(od, "gross_profit_change_by_segment")[:2]:
        lines.append(f"- {_cut_summary(seg, top=3)}；每种切法各自覆盖同一口径，"
                     "**不可跨切法相加**")
    vp = _out(od, "volume_price_decomposition")
    if vp is not None:
        lines.append("- 量价分解（" + str(_attr(vp, "label")) + "）：" + "、".join(
            f"{_attr(c, 'label')} {_yi(_attr(c, 'value'))} 亿元"
            for c in (_attr(vp, "components") or ()))
            + "；均价含产品结构混合，不得命名「提价效果」")
    return lines


def _locator_cell(item: dict) -> str:
    """来源位置单元格：有原文链接就做成**可点**（PDF 页码锚点），没有就写位置文字。"""
    loc = str(item.get("locator") or "").strip()
    url = str(item.get("url") or "").strip()
    if not loc:
        return "未取到页码定位（见底稿与材料清单）"
    if not url.startswith("http"):
        return loc
    m = re.search(r"第\s*(\d+)\s*页", loc)
    href = f"{url}#page={m.group(1)}" if m else url
    return f"[{loc}]({href})"


def _source_table(runs, provenance: dict, limit: int = 14) -> list[str]:
    ids: list[str] = []
    for r in (runs or ()):
        for o in (_attr(r, "outputs") or ()):
            for fid in (_attr(o, "inputs") or ()):
                if fid and str(fid) not in ids:
                    ids.append(str(fid))
    if not ids:
        return []
    lines = ["| 读数 | 期间 | 披露值 | 来源位置（原件） |", "|---|---|---:|---|"]
    for fid in ids[:limit]:
        p = provenance.get(fid) or {}
        value = p.get("value")
        shown = (f"{float(value):,.2f}{p.get('unit') or ''}"
                 if isinstance(value, (int, float)) else "—")
        lines.append(f"| {p.get('label') or '—'} | {p.get('period') or '—'} | {shown} | "
                     f"{_locator_cell(p)} |")
    if len(ids) > limit:
        lines.append(f"| 其余 {len(ids) - limit} 项输入 | — | — | 见分析底稿"
                     "（`analysis/analysis_runs.json` 与 `analysis/dataset.json`） |")
    lines.append("")
    lines.append("- 上表是**用于计算的关键读数**及其原件位置；完整事实身份清单"
                 "（fact_id → 表/页/行）见文末『附：底稿索引』与包内 `analysis/` 目录。")
    return lines


def _cash_sustainability(cash) -> list[str]:
    """V1：现金改善**能不能持续**——按组拆出单项变动，并给出后续观察指标。

    证据层能确定的是：现金变化由哪些披露调节项构成（会计口径）。"能不能持续"取决于这些
    项目是**时点/占用**还是经营改善；本层不替读者下结论，而是把单项拆开、写明可观察指标，
    并给出反证方向（结算节奏、票据/预收、备货、减值口径）。
    """
    if cash is None:
        return []
    items = _diag(cash).get("cash_change_items") or {}
    chg = _out(cash, "operating_cashflow_change")
    if chg is None or not items:
        return []
    total = float(_attr(chg, "value") or 0)
    lines: list[str] = []
    for group, title, note, watch in (
        ("working_capital", "营运资本项（存货/经营性应收/经营性应付）",
         "这三项是**占用与时点**口径：应付增加可能只是结算节奏或票据，不等于账期延长；"
         "应收/存货的减少也可能是备货或确认节奏",
         "下期存货、应收账款、应付账款的绝对额与周转天数；现金流量表附注里的票据与预收变动"),
        ("non_cash", "非现金项（折旧摊销/减值/递延税等）",
         "非现金项是**会计加回**：减值计提与转回、递延税确认都会让这一组变大或变小，"
         "不直接代表现金改善",
         "下期折旧摊销与减值明细、递延所得税附注；资产减值准备余额变化"),
    ):
        group_rows = [r for r in (items.get(group) or ()) if r.get("delta_yuan")]
        if not group_rows:
            continue
        group_total = sum(float(r["delta_yuan"]) for r in group_rows)
        # 现金**下降**时"占现金变化 x%"会把符号读反（洋河：ΔOCF −15.02、营运资本 +11.04
        # 会显示成 −73.6%）。按方向写成"抵消/加重现金下降"，读数才与直觉一致。
        if not total:
            share = ""
        elif total > 0:
            share = f"（占现金变化 {group_total / total * 100:.1f}%）"
        else:
            share = (f"（抵消现金下降 {abs(group_total / total) * 100:.1f}%）"
                     if group_total > 0 else
                     f"（加重现金下降 {abs(group_total / total) * 100:.1f}%）")
        top = "、".join(f"{r['label']} {_yi(r['delta_yuan'])} 亿元"
                        for r in group_rows[:3])
        lines.append(f"- **{title}**：合计 {_yi(group_total)} 亿元{share}；主要单项：{top}")
        lines.append(f"  - 读法：{note}")
        lines.append(f"  - 后续观察指标：{watch}")
    return lines


def _alternatives(od) -> list[str]:
    if od is None:
        return []
    alt = _diag(od).get("alternative_explanations") or {}
    if not alt:
        return ["- 本次未产出可计算的替代解释（缺所得税/公允价值等明细）："
                "因此本节的金额分解**不构成业务原因**，只说明金额构成。"]
    lines: list[str] = []
    rate, cf = alt.get("effective_tax_rate") or {}, alt.get("tax_at_prior_rate") or {}
    if rate and cf:
        effect = float(cf.get("rate_effect_yuan") or 0)     # 正 = 税率变化多吃掉
        verb = "多吃掉" if effect > 0 else "少吃掉"
        delta_pp = float(rate.get("delta_pp") or 0)
        move = "上升" if delta_pp > 0 else "下降"
        lines.append(
            f"- **实际税率**：{float(rate.get('prev') or 0):.2%} → "
            f"{float(rate.get('cur') or 0):.2%}（{delta_pp:+.2f}pp）。本年所得税 "
            f"{_yi_plain(cf.get('actual_yuan'))} 亿元；按上年实际税率折算为 "
            f"{_yi_plain(cf.get('at_prior_rate_yuan'))} 亿元——税率{move}本身让税负"
            f"{verb} {_yi_plain(abs(effect))} 亿元。这是**被动结果**（利润变化会改变税基），"
            "既不表示经营改善，也不能直接当成业务原因")
    for _key, item in (alt.get("non_operating_items") or {}).items():
        lines.append(f"- **{item.get('label')}** "
                     f"{_yi(item.get('contribution_yuan'))} 亿元：{item.get('note')}")
    if alt.get("structure_vs_price"):
        lines.append(f"- **结构 vs 价格**：{alt['structure_vs_price']}")
    lines.append("- **口径提醒**：以上都是会计分解；要说成「业务原因」必须另有披露依据"
                 "（管理层讨论、分部附注、销量与结算条款），否则只能写"
                 "「观察成立、原因待证」。")
    return lines


# ── V1：主要贡献 → 披露支持 / 反证 / 观察指标（确定性绑定，不代拟业务解释）──────
# 每个指标的关键词（用于在已准入材料的段落里找**可能相关**的披露）与"该看什么指标"。
_DRIVER_TERMS: dict = {
    "selling_expense": ("销售费用", "促销", "广告", "市场推广", "渠道", "销售人员"),
    "admin_expense": ("管理费用", "职工薪酬", "股份支付", "管理人员"),
    "rd_expense": ("研发费用", "研发投入", "研发人员"),
    "finance_expense": ("财务费用", "利息收入", "利息支出", "汇兑"),
    "taxes_and_surcharges": ("税金及附加", "消费税", "城建税", "教育费附加"),
    "income_tax_expense": ("所得税", "税率", "递延所得税"),
    "credit_impairment": ("信用减值", "坏账", "应收账款"),
    "asset_impairment": ("资产减值", "存货跌价", "减值准备"),
    "fair_value_change": ("公允价值", "交易性金融资产"),
    "investment_income": ("投资收益", "联营", "合营", "理财", "股权"),
    "minority_interest": ("少数股东", "少数股东损益"),
    "operating_cost": ("营业成本", "成本", "原材料"),
    "revenue": ("营业收入", "销量", "销售", "市场份额"),
    "non_operating_income": ("营业外收入", "政府补助"),
    "non_operating_expense": ("营业外支出", "捐赠", "罚款"),
    # 现金侧
    "change_in_working_capital": ("经营性应付", "经营性应收", "应付账款", "应收账款",
                                  "存货", "票据", "预收", "结算"),
    "change_in_non_cash": ("折旧", "摊销", "减值", "递延所得税"),
}
# 每条金额贡献的**反证方向**与**后续观察指标**（确定性、可检验；不写业务因果）
_DRIVER_WATCH: dict = {
    "selling_expense": ("费用下降也可能来自投放节奏后移或口径调整",
                        "下期销售费用率、广告与促销费明细、经销商政策披露"),
    "admin_expense": ("管理费用含一次性项目（股份支付/重组），单期变化不代表常态化",
                      "下期管理费用率、职工薪酬与股份支付明细"),
    "rd_expense": ("研发费用波动常与项目阶段有关，未必是投入收缩",
                   "下期研发投入强度、资本化比例与在研项目披露"),
    "finance_expense": ("利息收支受货币资金与利率影响，非经营改善",
                        "下期货币资金余额、有息负债与利率环境"),
    "taxes_and_surcharges": ("消费税/附加税随收入与结构变化，属被动项",
                             "下期税金及附加占收入比、消费税计税依据"),
    "income_tax_expense": ("税率变化只是对照，不是税率变化的原因；原因见税率调节附注",
                           "下期实际税率与税率调节表、非经常性损益的税务影响"),
    "credit_impairment": ("减值计提与转回有主观性，单期变化不等于资产质量改善",
                          "下期应收账款账龄、迁徙率与坏账准备余额"),
    "asset_impairment": ("存货跌价与资产减值的计提/转回会双向影响利润",
                         "下期存货跌价准备余额、存货周转天数"),
    "fair_value_change": ("公允价值变动未实现，不构成经营改善",
                          "下期交易性金融资产/负债余额与持仓披露"),
    "investment_income": ("是否经常性要看公司非经常性损益披露与业务实质，不按指标名剔除",
                          "下期投资收益构成（联营/合营/理财）、非经常性损益与扣非归母净利"),
    "operating_cost": ("成本变化含原材料价格与结构，未必是效率改善",
                       "下期毛利率、单位成本、主要原材料价格"),
    "revenue": ("收入含并表范围与结构变化（均价≠提价）",
                "下期分产品/分地区收入、销量与均价、并表范围变化"),
    "change_in_working_capital": ("占用与时点口径：应付增加可能只是结算节奏或票据",
                                  "下期应付/应收/存货绝对额与周转天数、票据与预收变动"),
    "change_in_non_cash": ("非现金项是会计加回（计提/转回/递延税）",
                           "下期折旧摊销与减值明细、递延所得税附注"),
}


def _terms_for(component_id: str, label: str) -> tuple:
    key = str(component_id or "")
    if key in _DRIVER_TERMS:
        return _DRIVER_TERMS[key]
    base = key.split(":")[-1]
    return _DRIVER_TERMS.get(base) or (str(label or "")[:6],)


def _watch_for(component_id: str) -> tuple:
    key = str(component_id or "")
    base = key.split(":")[-1]
    return _DRIVER_WATCH.get(key) or _DRIVER_WATCH.get(base) or (
        "该金额是会计分解，业务原因需要对应披露",
        "下期同一项目的金额与披露说明")


def _match_records(records, terms, limit: int = 1) -> list:
    """在已准入段落里找与这项贡献**相关**的披露（严格打分，避免"句尾顺带提到"就绑定）。

    打分：小节名命中 +3、段首 60 字命中 +2、段内其它位置命中 +1；**总分 < 2 不绑定**
    （即至少要出现在小节名或段首），否则如实报告"没有相关段落"。
    """
    hits = []
    for r in (records or ()):
        if not isinstance(r, dict):
            continue
        section = str(r.get("section") or "")
        snip = str(r.get("snippet") or "")
        head = snip[:60]
        score = 0
        for t in terms:
            if not t:
                continue
            if t in section:
                score += 3
            if t in head:
                score += 2
            elif t in snip:
                score += 1
        if score >= 2:
            hits.append((score, r))
    hits.sort(key=lambda x: -x[0])
    return [r for _s, r in hits[:limit]]


def _doc_keyword_hit(doc, terms, *, window: int = 100) -> dict | None:
    """兜底取证：在**原件全文**里按关键词找一处命中（带页码），并标明是关键词命中。

    为什么需要：`narrative_evidence` 每类只留若干条（避免堆砌），像"投资收益 643,008 …
    主要系处置远期外汇合约收益增加"这种句子可能落在别的类别里而被漏掉。兜底只做
    **关键词窗口 + 页码**，不冒充"管理层讨论段落"——locator 里写清楚。
    """
    text = str((doc or {}).get("text") or "")
    if not text:
        return None
    for t in terms:
        if not t:
            continue
        i = text.find(t)
        if i < 0:
            continue
        page = 0
        for pos, pno in (doc.get("page_offsets") or []):
            if int(pos) <= i:
                page = int(pno)
            else:
                break
        snippet = text[max(0, i - window // 2):i + window].replace("\n", " ")
        locator = (f"原件第 {page} 页（关键词「{t}」命中，未归类到管理层讨论小节）"
                   if page else f"原件（关键词「{t}」命中，未取到页码）")
        return {"snippet": snippet, "page": page, "locator": locator,
                "kind_label": "关键词命中"}
    return None


def _nonrecurring_note(records, doc=None) -> list[str]:
    """V1：用公司**非经常性损益**披露做对照（不按指标名统一剔除投资收益）。

    只给"去哪儿对照"与边界，不替读者重算"正常化利润"（那会变成新的黑箱）。
    """
    terms = ("非经常性损益", "扣除非经常性损益", "扣非")
    hits = _match_records(records, terms, limit=1)
    if not hits and doc is not None:
        fallback = _doc_keyword_hit(doc, terms)
        if fallback:
            hits = [fallback]
    lines = ["- **非经常性损益对照**：判断投资收益/公允价值这类项目是否经常性，"
             "以公司披露的**非经常性损益明细与扣非归母净利**为准（分类取决于事项经济性质、"
             "行业与业务模式，并考虑持续性）；本报告**不按指标名统一剔除**，"
             "也不据此构造「正常化利润」。"]
    if hits:
        rec = hits[0]
        where = str(rec.get("locator") or rec.get("section") or "")
        lines.append(f"  - 本期披露位置：{where[:120]}")
        snip = str(rec.get("snippet") or "").replace("\n", " ")[:120]
        if snip:
            lines.append(f"  - 原文摘录：{snip}")
    else:
        lines.append("  - 本次材料里未取到非经常性损益明细/扣非归母净利："
                     "该对照留待补料后再做")
    return lines


def driver_evidence(runs, records, *, limit: int = 3, doc=None) -> list[str]:
    """把主要金额贡献绑到**已准入材料的披露原句**，并给反证与观察指标（V1）。

    只做绑定，不代拟业务解释：披露说了什么就引用什么，并写明"这条披露支持到哪里"。
    找不到对应披露的贡献**如实写缺**（不编解释、不拿别的段落顶替）。
    """
    od = _pick(runs, "operating_drivers")
    cash = _pick(runs, "cash_reconciliation")
    items: list[dict] = []
    if od is not None:
        detail = _out(od, "net_profit_change_detail")
        for c in (_attr(detail, "components") or ()) if detail is not None else ():
            if str(_attr(c, "component_id")) == "unexplained_residual":
                continue
            items.append({"component_id": str(_attr(c, "component_id")),
                          "label": str(_attr(c, "label")),
                          "value": float(_attr(c, "value") or 0)})
    if cash is not None:
        chg = _out(cash, "operating_cashflow_change")
        for c in (_attr(chg, "components") or ()) if chg is not None else ():
            cid = str(_attr(c, "component_id"))
            if cid in ("change_in_net_profit", "change_in_residual"):
                continue          # 起点与差额不是"可解释的驱动项"
            items.append({"component_id": cid, "label": str(_attr(c, "label")),
                          "value": float(_attr(c, "value") or 0)})
    items.sort(key=lambda x: -abs(x["value"]))
    lines: list[str] = []
    for item in items[:limit]:
        cid, label, value = item["component_id"], item["label"], item["value"]
        terms = _terms_for(cid, label)
        hits = _match_records(records, terms)
        if not hits and doc is not None:
            fallback = _doc_keyword_hit(doc, terms)
            if fallback:
                hits = [fallback]
        contra, watch = _watch_for(cid)
        lines.append(f"- **{label} {_yi(value)} 亿元**（会计分解）")
        if hits:
            rec = hits[0]
            where = str(rec.get("locator") or rec.get("section") or "")
            if rec.get("page") and "第" not in where:
                where = f"{where}（PDF 第 {rec.get('page')} 页）".strip("（）")
            snip = str(rec.get("snippet") or "").replace("\n", " ")[:140]
            lines.append(f"  - 披露原句：{snip}")
            lines.append(f"  - 出处：{where[:120]}")
            lines.append("  - 支持到哪里：该披露与这项金额**方向一致或同期出现**；"
                         "金额来自调节表、段落来自管理层讨论，**不构成因果已证明**")
        else:
            lines.append(f"  - 披露支持：本次材料里**没有**与「{'/'.join(terms[:3])}」"
                         "相关的段落（未取到或未准入）：这一项只作金额分解")
        lines.append(f"  - 反证/替代解释：{contra}")
        lines.append(f"  - 后续观察指标：{watch}")
    if not lines:
        return []
    return ["- **主要贡献的披露支持（原句＋边界＋反证＋观察指标）**"] + lines


def _scenario_detail_note(scens) -> list[str]:
    """W0：明细模式下，把毛利线以下逐项规则与**假设如何进入最终利润**写进正文。

    纪律：只描述规则与读数（固定金额／随收入变化／单独假设／冻结残差），
    不做完整预测；逐项按**披露符号**计入（正号增利、负号减利）。
    """
    out: list[str] = []
    for r in (scens or ()):
        det = _out(r, "scenario_below_gross_detail")
        diag = (_diag(r).get("below_gross") or {})
        if det is None or str(diag.get("mode")) != "detail":
            continue
        items = list(_attr(det, "components") or ())
        rules = {"fixed": "固定金额", "revenue_linked": "随收入变化",
                 "explicit": "单独假设", "base_rate": "基期有效税率",
                 "base_share": "基期占比", "residual": "冻结残差",
                 "missing": "缺披露（并入残差）"}
        shown = "、".join(
            f"{_attr(c, 'label')} {_yi(_attr(c, 'value'))}"
            f"（{rules.get(str(_attr(c, 'rule')), str(_attr(c, 'rule')))}）"
            for c in items)
        out.append(f"- **情景明细模式（{_attr(det, 'output_period')}）**：毛利线以下净额对"
                   f"归母净利的影响 {_yi(_attr(det, 'value'))} 亿元 ＝ {shown}")
        out.append("  - 各项按**披露符号**计入（正号为增利、负号为减利）；"
                   "负号不是「少花了钱」，是这一项在减少利润")
        pretax = diag.get("pretax_profit_yuan")
        if pretax is not None:
            out.append(f"  - 情景税前利润 {_yi_plain(pretax)} 亿元"
                       f"（基期 {_yi_plain(diag.get('pretax_profit_base_yuan'))} 亿元）；"
                       f"所得税 {_yi_plain(diag.get('income_tax_yuan'))} 亿元"
                       f"（{diag.get('tax_rate_source')}）→ 合并净利 "
                       f"{_yi_plain(diag.get('consolidated_net_yuan'))} 亿元")
        if diag.get("minority_interest_yuan") is not None:
            out.append(f"  - 少数股东损益 {_yi_plain(diag.get('minority_interest_yuan'))} 亿元"
                       f"（{diag.get('minority_source')}）→ 情景归母净利 "
                       f"{_yi_plain(diag.get('scenario_net_profit_yuan'))} 亿元")
        out.append(f"  - 残差 {_yi_plain(diag.get('residual_yuan'))} 亿元："
                   "未取得明细的部分**只算一次并冻结**，不随税率/费用/少数股东假设变化，"
                   "**不摊派、不当零、也不构成完整预测**")
        missing = list(diag.get("items_missing") or ())
        if missing:
            out.append("  - 未取到明细：" + "、".join(str(m) for m in missing[:6]))
    return out


def _slug_text(slug, label_of=None) -> str:
    """内部指标名 → 可读写法：`信用减值损失（`credit_impairment_provision`）`。

    给的是**可选**映射（`facts.metric_label` 那种）；没有映射就照写 slug——不含糊其辞地
    编一个中文名。
    """
    s = str(slug or "")
    if label_of is None or not s:
        return f"`{s}`" if s else "（未记名）"
    try:
        label = str(label_of(s) or "")
    except Exception:                                  # noqa: BLE001
        label = ""
    return f"{label}（`{s}`）" if label and label != s else f"`{s}`"


def _todo(runs, label_of=None) -> list[str]:
    od = _pick(runs, "operating_drivers")
    cash = _pick(runs, "cash_reconciliation")
    todo: list[str] = []
    if od is not None:
        diag = _diag(od)
        for skipped in (diag.get("segments_skipped") or ())[:3]:
            todo.append(f"分段缺成本或收入：{skipped}")
        for skipped in (diag.get("volume_price_skipped") or ())[:2]:
            todo.append(f"量价缺销量或收入：{skipped}")
        for miss in (diag.get("line_items_missing") or ())[:4]:
            todo.append("利润表明细未取到：" + _slug_text(miss, label_of))
        ex = _out(od, "net_profit_change_detail")
        resid = float(_attr(ex, "residual") or 0) if ex is not None else 0.0
        if resid:
            todo.append(f"未解释差额 {_yi(resid)} 亿元：先补对应披露项目，"
                        "不得摊到已列项目上")
    if cash is not None:
        diag = _diag(cash)
        for miss in ((diag.get("items_missing") or {}).get("cur") or [])[:3]:
            todo.append("现金调节项未取到：" + _slug_text(miss, label_of))
        for it in ((diag.get("largest_support") or {}), (diag.get("largest_drag") or {})):
            if it.get("label"):
                todo.append(f"对「{str(it['label'])[:24]}」找一条披露依据"
                            "（明细表/附注/结算条款）做旁证")
    seen: list[str] = []
    for t in todo:
        if t not in seen:
            seen.append(t)
    lines = ["### 六、待核查问题（按影响排序）"]
    if seen:
        for i, t in enumerate(seen[:5], 1):
            lines.append(f"{i}. {t}")
    else:
        lines.append("1. 两期披露项齐全，仍未解决的是**业务原因**：把最大的三项贡献各对到"
                     "一条管理层讨论或分部附注的原文。")
    return lines


def _limits() -> list[str]:
    return [
        "### 七、口径与限制",
        "- 亿元为显示换算（两位小数）；闭合判定与占比用运行里的**元**值。",
        "- 逐项贡献是**会计分解**：说明金额构成，不等于业务原因。",
        "- 分产品/分行业/分地区/分销售模式是同一口径的**不同切法**，不可相加；"
        "每种切法的未分类差额单独列出。",
        "- 均价由「该口径收入/销量」算出，含产品结构混合，不得命名「提价效果」。",
        "- 现金桥起点是**合并净利润**（不是归母净利）；调节项是会计加回/扣减，"
        "不表示经济原因；资产负债表期末差只作旁证、不用于补桥。",
        "- 反向阈值是**单因素反推**：给出「需要什么」，不表示可达、不是预测、无概率。",
        "- 本节数字全部来自 `validated` 运行；某模型未通过验证时，本节不出现它的结论。",
    ]


def _figures(charts) -> list[str]:
    figs = [c for c in (charts or ()) if isinstance(c, dict) and c.get("available")]
    if not figs:
        return []
    lines = ["### 附：可复算底稿图（与正文同一次运行）"]
    for i, c in enumerate(figs, 1):
        lines.append(f"- 图 {i}：{c.get('title') or ''}")
        if c.get("conclusion"):
            lines.append(f"  - 图注（由读数算出）：{c['conclusion']}")
    lines.append("- 图与正文共用同一次运行；文件名与运行标识见文末『附：底稿索引』。")
    return lines


def summary_lines(runs, limit: int = 3) -> list[str]:
    """正文**开头三条摘要**（无工程标识）：利润 / 结构 / 现金（+反向情景）。

    与 `research_note` 的『一、结论』同源（同一批运行、同一套算式），只是去掉 run/output
    标识与长解释——卡片位置只留这三条。
    """
    runs = [r for r in (runs or ())
            if str(_attr(r, "status")) == str(RunStatus.VALIDATED)]
    od = _pick(runs, "operating_drivers")
    cash = _pick(runs, "cash_reconciliation")
    scens = _spread(runs, "scenario_sensitivity")
    if od is None and cash is None:
        return []
    out = []
    for txt in _conclusions(od, cash, scens):
        out.append(txt.split("　")[0])
        if len(out) >= limit:
            break
    return out


def _engineering_index(runs, charts) -> list[str]:
    """底稿索引：run / dataset / output / component_id / 事实身份 / 图文件名（正文之外）。"""
    runs = [r for r in (runs or ())]
    if not runs and not charts:
        return []
    lines = ["### 附：底稿索引（run / output / component_id）",
             "> 正文与图的所有数字都能在这里回查；包内 `analysis/` 目录含数据集、计划、"
             "契约与全部运行记录。"]
    for r in runs:
        lines.append(f"- `{_attr(r, 'model_id')}`：run `{_attr(r, 'run_id')}`"
                     f"（数据集 `{str(_attr(r, 'dataset_hash'))[:12]}`，"
                     f"规则 `{_attr(r, 'rules_version') or '—'}`）")
        for o in (_attr(r, "outputs") or ()):
            comps = "、".join(str(_attr(c, "component_id"))
                              for c in (_attr(o, "components") or ()))
            lines.append(f"  - {_attr(o, 'metric')} → `{_attr(o, 'output_id')}`"
                         + (f"；分项 {comps}" if comps else ""))
    for c in (charts or ()):
        if isinstance(c, dict) and c.get("available"):
            lines.append(f"- 图 `{c.get('chart_id')}`：{c.get('title')}")
    return lines


# ------------------------------------------------------------------ 主入口

def research_note(runs, *, provenance=None, charts=None, label_of=None,
                  records=None, doc=None) -> str:
    """已验证运行 → 4–6 页正文（markdown）。缺哪个模型就如实写缺，不补数。

    **未通过独立验证的运行在这里被挡掉**：即使调用方把 `validation_failed` 的运行传进来，
    正文也不会引用它的数字（该模型一律按「本次未运行」写）。
    """
    runs = [r for r in (runs or ())
            if str(_attr(r, "status")) == str(RunStatus.VALIDATED)]
    od = _pick(runs, "operating_drivers")
    cash = _pick(runs, "cash_reconciliation")
    scens = _spread(runs, "scenario_sensitivity")
    if od is None and cash is None:
        return ""
    entity = ""
    for r in (od, cash):
        if r is None:
            continue
        for o in (_attr(r, "outputs") or ()):
            entity = str(_attr(o, "entity") or _attr(o, "entity_id") or "")
            break
        if entity:
            break
    periods: list[str] = []
    for r in (od, cash):
        periods = [str(p) for p in (_diag(r).get("periods") or ())]
        if periods:
            break
    span = "→".join(periods) if len(periods) >= 2 else ""
    head = f"## 经营驱动分析正文（{entity}{' ' + span if span else ''}）"
    lines: list[str] = [head, "",
                        "> 本节由 `financial_analysis` 从**已验证运行**装配：每个数字都能回查到"
                        "运行与输出标识（见文末『附：底稿索引』），图、卡、底稿共用同一次运行。"
                        "未取到的披露项留在「未解释差额」，既不摊派也不当零。", ""]
    lines.append("### 一、结论（先看这三条）")
    for i, txt in enumerate(_conclusions(od, cash, scens), 1):
        lines.append(f"{i}. {txt}")
    lines.append("")
    if od is not None:
        lines.append("### 二、利润变化：金额分解")
        lines.extend(_profit_table(od))
        lines.append("")
    prov_map = dict(provenance or {})
    if prov_map:
        table = _source_table(runs, prov_map)
        if table:
            lines.append("### 三、披露支持与来源定位")
            lines.extend(table)
            lines.append("")
    else:
        # 没有可复算输入记录时不摆一张全是「—」的表：如实说清楚去哪儿查（运行记录里有
        # `inputs` 的 fact_id 清单），而不是让读者以为"查不到"。
        lines.append("### 三、披露支持与来源定位")
        lines.append("- 本次工作区没有可复算输入记录（`analysis/dataset.json` 缺失）："
                     "每个输出消费的**输入事实身份**见运行记录 `analysis_runs.json` 的 "
                     "`inputs`；表名/页码定位见材料清单与底稿。")
        lines.append("")
    if od is not None:
        lines.append("### 四、替代解释（会改变判断）")
        lines.extend(_alternatives(od))
        lines.extend(_nonrecurring_note(records, doc))
        lines.append("")
        hits = driver_evidence(runs, records, doc=doc) if (records or doc) else []
        if hits:
            lines.append("### 四之二、主要贡献的披露支持（原句＋边界＋反证＋观察指标）")
            lines.extend(hits[1:])          # 首行是标题行，已在上一行给出
            lines.append("")
        elif od is not None:
            lines.append("### 四之二、主要贡献的披露支持")
            lines.append("- 本次没有可绑定的已准入披露段落（材料未准入或未取到管理层讨论）："
                         "主要贡献只作金额分解，**不代拟业务解释**；补齐材料后按"
                         "「金额 → 披露原句 → 支持到哪里 → 反证 → 观察指标」逐项绑定。")
            lines.append("")
    if cash is not None or scens:
        lines.append("### 五、现金形成与反向情景")
        if cash is not None:
            # V1：**先给现金变化桥**（ΔOCF = Δ合并净利＋Δ非现金＋Δ营运资本＋Δ其他＋Δ差额），
            # 再给两期调节表与辅助观察；这样"现金为什么变了"才是正文的主角。
            chg = _out(cash, "operating_cashflow_change")
            if chg is not None:
                parts = "、".join(f"{_attr(c, 'label')} {_yi(_attr(c, 'value'))} 亿元"
                                  for c in (_attr(chg, "components") or ()))
                lines.append(f"- **经营现金流变化**：{_yi(_attr(chg, 'value'))} 亿元 ＝ {parts}")
                lines.append("  - 这是**会计构成**：营运资本项变化可能只是时点与资金占用，"
                             "能不能持续须看应付/应收/存货明细与结算条款；"
                             "投资收益等是否非经常要看公司非经常性损益披露，不按指标名剔除")
            for o in (_attr(cash, "outputs") or ()):
                if str(_attr(o, "metric")).startswith("operating_cashflow_reconciliation"):
                    comps = "、".join(f"{_attr(c, 'label')} {_yi(_attr(c, 'value'))} 亿元"
                                      for c in (_attr(o, "components") or ()))
                    lines.append(f"- **{_attr(o, 'output_period')}调节表**：{comps}")
            gap = _out(cash, "cash_gap_change")
            if gap is not None:
                comps = "、".join(f"{_attr(c, 'label')} {_yi(_attr(c, 'value'))} 亿元"
                                  for c in (_attr(gap, "components") or ()))
                lines.append(f"- **辅助观察：现金缺口变化**（经营现金流−合并净利润）："
                             f"{_yi(_attr(gap, 'value'))} 亿元 ＝ {comps}；"
                             "缺口缩小不等于现金变好")
            # V1：现金改善能不能持续——单项拆解 + 观察指标（结论要落到可检验的东西上）
            sustain = _cash_sustainability(cash)
            if sustain:
                lines.append("- **现金改善的可持续性（按组拆到单项）**")
                lines.extend(sustain)
            note = _diag(cash).get("closure_note")
            if note:
                lines.append(f"- {note}")
        for r in scens:
            th = _out(r, "margin_threshold_to_hold_base_profit")
            gap = _out(r, "margin_gap_to_threshold_pp")
            # V2：先给**该假设下的情景归母净利**（读者要看到"改了假设以后是多少"），
            # 再给两个方向的反推阈值。三处都来自同一次运行。
            scr = _out(r, "scenario_net_profit")
            if scr is not None:
                user = next((c for c in (_attr(scr, "components") or ())
                             if str(_attr(c, "component_id")) == "user"), None)
                base_c = next((c for c in (_attr(scr, "components") or ())
                               if str(_attr(c, "component_id")) == "base"), None)
                if user is not None:
                    line = (f"- **情景归母净利（{_scenario_label(r, scr)}）**："
                            f"{_yi(_attr(user, 'value'))} 亿元")
                    if base_c is not None:
                        line += (f"；基准复现 {_yi(_attr(base_c, 'value'))} 亿元"
                                 f"（差 {_yi(float(_attr(user, 'value') or 0) - float(_attr(base_c, 'value') or 0))} 亿元）")
                    lines.append(line)
            # W0：两把杠杆**共用同一目标归母净利**（默认上一期），各自单因素；
            # 目标与来源写在读数前面，读者不会把"维持基期"与"恢复到上年"混为一谈。
            tdiag = dict(_diag(r).get("thresholds") or {})
            tgt = tdiag.get("target_net_profit")
            tgt_src = str(tdiag.get("target_source") or "")
            tgt_s = (f"目标归母净利 {_yi_plain(tgt)} 亿元"
                     + (f"（{tgt_src}）" if tgt_src else "") if tgt is not None else "")
            if th is not None and _attr(th, "value") is not None:
                extra = (f"，与基期之差 {float(_attr(gap, 'value') or 0):+.2f}pp"
                         if gap is not None else "")
                lines.append(f"- **毛利率侧反推**（收入固定基期；{tgt_s}）："
                             f"所需毛利率 {_pct4(_attr(th, 'value'))}{extra}")
            else:
                reason = str((tdiag.get("threshold_notes") or {}).get("margin") or "")
                lines.append(f"- **毛利率侧反推**（{tgt_s}）：本次**不可达**"
                             + (f"——{reason}" if reason else "")
                             + "；不硬报一个数")
            # V2：**收入侧**反推（把利润拉回目标水平需要多少收入变化；同一目标/模式/假设）
            rev_th = _out(r, "revenue_growth_to_hold_target")
            if rev_th is not None and _attr(rev_th, "value") is not None:
                lines.append(f"  - 收入侧反推（毛利率固定基期）：收入需变化 "
                             f"{_pct4(_attr(rev_th, 'value'))} 才能达到同一目标"
                             "（单因素反推，不表示可达）")
            elif tdiag:
                reason = str((tdiag.get("threshold_notes") or {}).get("revenue") or "")
                lines.append("  - 收入侧反推：本次**不可达**"
                             + (f"——{reason}" if reason else "")
                             + "；不硬报一个数")
            cond = tdiag.get("conditional_margin_threshold")
            cond_gap = tdiag.get("conditional_margin_gap_pp")
            if cond is not None and _attr(th, "value") is not None:
                # 两因素条件值：先给定该档收入，再反推毛利率（与单因素阈值不是一回事）
                lines.append(f"  - 若先接受该档收入假设，则所需毛利率为 "
                             f"{_pct4(cond)}"
                             + (f"（较基期 {float(cond_gap):+.2f}pp）"
                                if cond_gap is not None else "")
                             + "——这是**两因素条件计算**，不是同一把杠杆")
        lines.append("")
    lines.extend(_scenario_detail_note(scens))
    if scens:
        lines.append("")
    lines.extend(_todo(runs, label_of))
    lines.append("")
    lines.extend(_limits())
    lines.append("")
    lines.extend(_figures(charts))
    lines.append("")
    lines.extend(_engineering_index(runs, charts))
    return "\n".join(lines).rstrip() + "\n"
