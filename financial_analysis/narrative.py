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
    """亿元读数（带符号、两位小数）——与图表共用同一套格式（不两处各自四舍五入）。"""
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
        if sup or drag:
            txt += (f"；最大支撑 {sup.get('label')} {_yi(sup.get('value'))} 亿元、"
                    f"最大拖累 {drag.get('label')} {_yi(drag.get('value'))} 亿元")
        items.append(txt)
    else:
        items.append("**现金**：本次未运行现金调节桥（缺现金流量表补充资料），"
                     "正文不给现金形成机制结论。")
    if scens:
        rows = []
        for r in scens:
            th = _out(r, "margin_threshold_to_hold_base_profit")
            gap = _out(r, "margin_gap_to_threshold_pp")
            if th is None or gap is None:
                continue
            rows.append(f"{_scenario_label(r, th)} 需 {_pct(_attr(th, 'value'))}"
                        f"（{float(_attr(gap, 'value') or 0):+.2f}pp）")
        if rows:
            items.append("**反向情景**（单因素反推，不表示可达）：" + "；".join(rows))
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

def research_note(runs, *, provenance=None, charts=None, label_of=None) -> str:
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
        lines.append("")
    if cash is not None or scens:
        lines.append("### 五、现金形成与反向情景")
        if cash is not None:
            for o in (_attr(cash, "outputs") or ()):
                if str(_attr(o, "metric")).startswith("operating_cashflow_reconciliation"):
                    comps = "、".join(f"{_attr(c, 'label')} {_yi(_attr(c, 'value'))} 亿元"
                                      for c in (_attr(o, "components") or ()))
                    lines.append(f"- **{_attr(o, 'output_period')}**：{comps}")
            gap = _out(cash, "cash_gap_change")
            if gap is not None:
                comps = "、".join(f"{_attr(c, 'label')} {_yi(_attr(c, 'value'))} 亿元"
                                  for c in (_attr(gap, "components") or ()))
                lines.append(f"- **现金缺口变化**：{_yi(_attr(gap, 'value'))} 亿元 ＝ {comps}；"
                             "缺口＝经营现金流−合并净利润，缺口缩小不等于现金变好")
            note = _diag(cash).get("closure_note")
            if note:
                lines.append(f"- {note}")
        for r in scens:
            th = _out(r, "margin_threshold_to_hold_base_profit")
            gap = _out(r, "margin_gap_to_threshold_pp")
            if th is None:
                continue
            extra = (f"，与基期之差 {float(_attr(gap, 'value') or 0):+.2f}pp"
                     if gap is not None else "")
            lines.append(f"- **反向情景（{_scenario_label(r, th)}）**：维持基期归母净利"
                         f"所需毛利率 {_pct(_attr(th, 'value'))}{extra}")
        lines.append("")
    lines.extend(_todo(runs, label_of))
    lines.append("")
    lines.extend(_limits())
    lines.append("")
    lines.extend(_figures(charts))
    lines.append("")
    lines.extend(_engineering_index(runs, charts))
    return "\n".join(lines).rstrip() + "\n"
