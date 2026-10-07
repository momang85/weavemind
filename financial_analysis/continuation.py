# -*- coding: utf-8 -*-
"""X1（阶段X §4）：**可检验的研究续页**——上一期写下的持续性假说，这一期怎样了。

一页回答四件事（阶段X §4 的样式表）：

    | 上次持续性假说/依据 | 本期关键新读数（同口径） | 判断怎样改变 | 下一观察 |

纪律（与全文一致，写进代码而不是注释）：

- **历史观察不被改写**：新一期销量恢复不会推翻"上一期销量下降"这个历史事实，只削弱
  "销量压力持续"的假说；两者在输出里分开写（`historical_note`）。
- **同口径才比较**：量/库存取同一披露口径（如 `分产品:白酒` 的实物量），吨价与现金取同一
  算式与同一调节项定义；口径不可比或缺读数就写 `无法判断`，**不凑一个方向**。
- **只复用既有 ID 与读数**：假说沿用 `judgments` 的判断 ID 与它自己的观察条件（`watch`），
  不新建模型族、不新增数据库；本批允许的增量就是这份 JSON 与它渲染出的一页。
- **历史材料回放**：先冻结较早材料的假说、再用较新材料读数比较——这也是回放，不得包装成
  事前盲测或投资预测；将来真实时间前向更新才谈研究效用。
"""
from __future__ import annotations

# 复用同一包内的判断层（ID、读数取值、方向措辞都只有一处定义）
from . import judgments as _jd

STATUS_STRONGER = "加强"
STATUS_WEAKER = "削弱"
STATUS_UNKNOWN = "无法判断"

# 同比变化在 1 个百分点以内视为"基本不变"（不硬说方向）；分段离散度比值容差 20%
TOLERANCE_PP = 1.0
SPREAD_TOL = 0.2

# 判断 ID → 待检验的持续性假说与观察条件（假说由**较早材料**的判断触发，不按公司名硬编码）
HYPOTHESES: dict[str, dict] = {
    _jd.J_PROFIT_MECHANISM: {
        "title": "利润下降中的**税费缓冲**仍然存在（税费对利润变化的贡献为正）",
        "condition": ("下一期同口径的**税费贡献**（＝−Δ所得税及税金附加）是否仍为正、"
                      "而利润是否仍在下降（负贡献转为正贡献才叫结构改变）"),
    },
    _jd.J_VOLUME: {
        "title": "销量压力延续（数量收缩没有转正）",
        "condition": "下一期**同口径**销售量同比是否仍为负、降幅是否收窄（≥1pp 才算方向）",
    },
    _jd.J_PRICE: {
        "title": "单位收入（推算吨价）继续上升",
        "condition": "下一期同口径推算吨价同比方向（含结构混合，仍不称「提价」）",
    },
    _jd.J_INVENTORY: {
        "title": "企业成品库存继续累积（未被消化）",
        "condition": "下一期同口径库存量同比是否仍为正（企业口径，不代表渠道库存）",
    },
    _jd.J_CASH_WORKING_CAPITAL: {
        "title": "现金来源的方向延续（利润主导／营运资本主导）",
        "condition": ("下一期经营现金流变化的净利项与营运资本项：主导项是否同向延续；"
                      "营运资本内部**缓冲是否只来自应付项目**（应收转为释放即为结构改变）"),
    },
    _jd.J_STRUCTURE: {
        "title": "结构差异延续（同一口径不同切法的降幅差没有收敛）",
        "condition": "下一期同口径分段毛利变化的离散度是否收敛（粗判，跨切法仍不可相加）",
    },
}

# **三条业务身份命题**（10-07 第二次复核 §下一阶段）：按明确身份取页，不按"旧 judgments 前 N 条"
PROPOSITIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("①利润与税费缓冲", (_jd.J_PROFIT_MECHANISM,)),
    ("②销量与企业成品库存", (_jd.J_VOLUME, _jd.J_INVENTORY)),
    ("③现金构成（应付／应收／存货）", (_jd.J_CASH_WORKING_CAPITAL,)),
)


def proposition_ids() -> list[str]:
    """三条命题覆盖的判断 ID（顺序＝页内顺序）。"""
    return [jid for _name, ids in PROPOSITIONS for jid in ids]


def proposition_of(jid: str) -> str:
    for name, ids in PROPOSITIONS:
        if jid in ids:
            return name
    return ""

LIMITS = [
    "本页是**历史材料回放**：先用较早年报写假说、再拿较新年报读数比较；"
    "即使按时间顺序冻结，也**不构成事前盲测或投资预测成功**。",
    "假说只沿用既有判断 ID 与它自己的观察条件；本页不新建模型、不引入概率/显著性/准确率。",
    "读数缺同口径比较项时如实写「无法判断」——材料空缺不等于假说被反驳。",
    "企业口径（库存/销量）不冒充渠道库存或终端需求；分产品/分地区等切法覆盖同一笔收入，"
    "**不可相加**。",
]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v) -> str:
    n = _num(v)
    return "—" if n is None else f"{n:+.2f}%"


def _yi(v) -> str:
    return _jd._yi(v)


def _diag(run) -> dict:
    """运行的诊断块（阈值目标/模式等写在这里；取不到就空 dict）。"""
    for o in (_jd._attr(run, "outputs", ()) or ()):
        d = _jd._attr(o, "diagnostics", None)
        if d:
            return dict(d)
    return {}


# ------------------------------------------------------------------ 半年报读数（同期间口径）

def _page_of(doc: dict, pos: int):
    """字符位置 → 页码（用材料自己的 `page_offsets`，照实；取不到就 None）。"""
    po = (doc or {}).get("page_offsets")
    if not po or pos is None:
        return None
    try:
        if isinstance(po, list) and po and isinstance(po[0], (list, tuple)):
            last = int(po[-1][1])
            for off, pg in po:
                if int(off) > int(pos):
                    return max(1, int(pg) - 1)
            return max(1, last)
    except Exception:                                       # noqa: BLE001
        return None
    return None


def _label_re(label: str) -> str:
    """行标签可能被 PDF 换行切断（"归属于上市公司股东的净利\n润（元）"）⇒ 字符间允许空白。"""
    import re as _re
    return r"\s*".join(_re.escape(ch) for ch in str(label))


def _half_row(text: str, label: str, doc: dict) -> dict | None:
    """按行标签取「本期／上年同期／同比」三元组（半年报的主要会计数据表形状）。"""
    import re as _re
    pat = _re.compile(_label_re(label) + r"\s*（?元?）?\s*"
                      r"(?P<cur>[-+]?[\d,]+(?:\.\d+)?)\s+(?P<prev>[-+]?[\d,]+(?:\.\d+)?)"
                      r"(?:\s+(?P<yoy>[-+]?\d+(?:\.\d+)?)%)?")
    m = pat.search(text or "")
    if not m:
        return None
    cur, prev = _num(m.group("cur").replace(",", "")), _num(m.group("prev").replace(",", ""))
    if cur is None or prev is None:
        return None
    pg = _page_of(doc, m.start())
    return {"cur": cur, "prev": prev,
            "yoy": _num(m.group("yoy")) if m.group("yoy") is not None else
            ((cur - prev) / prev * 100 if prev else None),
            "disclosed_yoy": _num(m.group("yoy")),
            "locator": (f"第 {pg} 页 · 「{label}」" if pg else f"「{label}」（页码未取得）"),
            "text": (m.group(0) or "").replace("\n", " ")[:160]}


def _half_volume_rows(text: str, doc: dict) -> dict:
    """产销量表（半年报）：销售量／生产量／库存量 吨（同口径两期）。"""
    import re as _re
    out: dict = {}
    for label in ("销售量", "生产量", "库存量"):
        m = _re.search(label + r"\s*吨\s*(?P<cur>[\d,]+(?:\.\d+)?)\s+"
                       r"(?P<prev>[\d,]+(?:\.\d+)?)\s+(?P<yoy>[-+]?\d+(?:\.\d+)?)%",
                       text or "")
        if not m:
            continue
        cur, prev = _num(m.group("cur").replace(",", "")), _num(m.group("prev").replace(",", ""))
        if cur is None or prev is None:
            continue
        pg = _page_of(doc, m.start())
        out[label] = {"cur": cur, "prev": prev, "yoy": _num(m.group("yoy")),
                      "disclosed_yoy": _num(m.group("yoy")),
                      "locator": (f"第 {pg} 页 · 主要产品的生产量、销售量、库存量（吨）"
                                  if pg else "产销量表（页码未取得）"),
                      "text": m.group(0)[:120]}
    m = _re.search(r"成品酒库存量[^\n]{0,40}?([\d,]+(?:\.\d+)?)", text or "")
    if m:
        pg = _page_of(doc, m.start())
        out["成品酒库存量"] = {"cur": _num(m.group(1).replace(",", "")), "prev": None,
                               "yoy": None, "disclosed_yoy": None,
                               "locator": (f"第 {pg} 页 · 期末成品酒/半成品酒库存量（吨）"
                                           if pg else "期末成品酒库存量（页码未取得）"),
                               "text": m.group(0)[:120]}
    return out


def half_year_readings(document: dict, *, cur_label: str, prev_label: str) -> dict:
    """**同期半年**读数（2025H1 vs 2024H1），全部来自原件表格，逐项带页码定位。

    纪律：只从**同一张半年报表**的「本报告期／上年同期」两列取数（同期间口径），
    不拿半年与全年相减；缺行就缺（调用方写「无法判断」）。
    """
    text = str((document or {}).get("text") or "")
    rows = {k: _half_row(text, k, document) for k in
            ("营业收入", "归属于上市公司股东的净利润", "经营活动产生的现金流量净额",
             "所得税费用", "营业成本", "税金及附加")}
    return {"cur_label": cur_label, "prev_label": prev_label,
            "period_kind": "half",
            "rows": {k: v for k, v in rows.items() if v},
            "volume": _half_volume_rows(text, document)}


# ------------------------------------------------------------------ 一份材料 → 两期快照

def snapshot(document, *, company: str, company_id: str, code: str, periods,
             as_of: str, disclosed_at: str = "", label: str = "",
             period_kind: str = "annual", period_labels=(),
             limit: int = 3) -> dict:
    """一份年报 → 两期快照（复用**现有**算子/抽取/判断，不新开通道）。

    返回普通 dict：`{label, source, periods, dataset, runs, volume_price, records,
    judgments, direct_cash}`。年份取原件表头/期间（`periods` 由调用方按原件给出），
    不用请求年份重标。
    """
    import financial_analysis as fa
    import facts as F
    import narrative_evidence as ne
    from adapters import cashflow_supplement_tables as cst
    from adapters import operating_detail_tables as odt

    facts = F.facts_from_annual_tables(document, company=company,
                                       company_code=code, periods=tuple(periods),
                                       as_of=as_of, disclosed_at=disclosed_at)
    # 定向抽取（分产品/分地区收入与成本）+ 现金流量表补充资料，与正常链同源
    try:
        det = odt.extract_operating_detail(document, company=company,
                                           company_code=code, periods=tuple(periods))
    except Exception:                              # noqa: BLE001 - 抽取不到就按缺料处理
        det = {}
    try:
        sup = cst.extract_cashflow_supplement(document, company=company,
                                              company_code=code, periods=tuple(periods))
    except Exception:                              # noqa: BLE001
        sup = {}
    extra = list((det or {}).get("facts") or []) + list((sup or {}).get("facts") or [])
    # 合并要按**身份+值**去重：`facts_from_annual_tables` 内部**已经**追加了同一批
    # 定向抽取事实（facts.py 里的 odt/cst 两处），这里再抽一次是为了"内层被异常吞掉时
    # 还有一份"的兜底。原先写成 `[f for f in extra if f not in facts]`——拿 dict 和
    # `Fact` 对象比相等**永远不命中**，于是同一条身份出现两份（一份 Fact、一份 dict），
    # 两者的 `formula_version`/`period_type` 不同 ⇒ `observation_hash` 不同 ⇒
    # 数据集的冲突判定把它当成"同一(指标,期间,口径)多个不相容值"**全部丢弃**。
    # 实机后果（2025 年年度报告）：14 条同值身份被丢，其中含**上期分产品/分地区的
    # 收入与成本**——量价分解与分段毛利随之消失，续页会把"有数"写成"未取到"。
    rows = list(facts)
    _seen: set[tuple] = {(str(getattr(f, "metric", "") or ""),
                          str(getattr(f, "period", "") or ""),
                          str(getattr(f, "caliber", "") or ""),
                          str(getattr(f, "value", "") or "")) for f in rows}
    for f in extra:
        key = (str(f.get("metric") or ""), str(f.get("period") or ""),
               str(f.get("caliber") or ""), str(f.get("value") or ""))
        if key in _seen:
            continue
        _seen.add(key)
        rows.append(f)
    ds = fa.freeze_from_facts(rows, periods=tuple(periods), entity=company,
                              entity_id=company_id, as_of=as_of,
                              source_label=label or f"x1:{code}")
    runs = {mid: fa.run(mid, ds) for mid in ("operating_drivers", "cash_reconciliation")}
    vp = ne.extract_volume_price([document], periods=list(periods))
    records = [{"section": str(r.get("section") or ""),
                "snippet": str(r.get("snippet") or ""),
                "locator": str(r.get("locator") or ""),
                "kind": str(r.get("kind") or ""),
                "kind_label": str(r.get("kind_label") or "")}
               for r in (ne.extract_sections(document, periods=tuple(periods),
                                             company=company, company_id=company_id,
                                             as_of=as_of) or [])]
    js = _jd.research_judgments(list(runs.values()), volume_price=vp, records=records,
                                direct_cash=_jd.direct_cash_of(
                                    ds, locators={str(getattr(f, "fact_id", "")):
                                                  {"locator": str(getattr(
                                                      f, "source_locator", "") or "")}
                                                  for f in rows}),
                                limit=limit)
    labels = [str(x) for x in (period_labels or ())] or [str(p) for p in periods]
    _half = (half_year_readings(document, cur_label=labels[-1], prev_label=labels[0])
             if str(period_kind) == "half" else {})
    return {
        "label": label or f"{company} {periods[0]}–{periods[-1]}（{document.get('title')}）",
        "source": {"title": str(document.get("title") or ""),
                   "url": str(document.get("url") or ""),
                   "periods": [int(p) for p in periods],
                   "period_kind": str(period_kind),
                   "period_labels": labels,
                   "doc_type": str(document.get("doc_type") or
                                   ("半年度报告" if period_kind == "half" else "年度报告")),
                   "disclosure_date": str(disclosed_at or ""),
                   "text_sha256": str(document.get("text_sha256") or "")},
        "periods": [int(p) for p in periods],
        "period_kind": str(period_kind),
        "half": _half,
        "dataset": {"hash": str(ds.manifest.dataset_hash),
                    "observations": int(ds.manifest.observations),
                    "usable": int(getattr(ds.manifest, "usable", 0) or 0)},
        "runs": runs,
        "volume_price": vp,
        "records": records,
        "judgments": js[:max(0, int(limit))],
        # 供调用方做**同一数据集**的条件对照（如 fixed/detail 假设影响）；不进 JSON 输出
        "_ds": ds,
    }


# ------------------------------------------------------------------ 读数（同口径）

def readings_of(snap: dict) -> dict:
    """快照 → 同口径读数（量/库存/产量/吨价/现金方向/分段离散度）。取不到就 `None`。"""
    vp = snap.get("volume_price") or {}
    ok = bool(vp.get("ok"))
    vol = _jd._fact(vp, "销售量") if ok else {}
    prod = _jd._fact(vp, "生产量") if ok else {}
    inv = _jd._fact(vp, "库存量") if ok else {}
    price = _jd._vp_derived(vp, "吨价") if ok else {}
    if not price:
        # 披露侧没有"推算吨价"这一行时，用**数据集本身**算同一口径（收入÷销量）——
        # 与算子的量价分解同源；仍显式标为**推算**，不冒充披露价格。
        price = _implied_price(snap.get("_ds"), snap.get("periods") or [])
    runs = list((snap.get("runs") or {}).values())
    cash = _jd._pick(runs, "cash_reconciliation")
    chg = _jd._out(cash, "operating_cashflow_change") if cash is not None else None
    np_v = wc_v = total = None
    if chg is not None:
        total = _num(_jd._attr(chg, "value"))
        for c in (_jd._attr(chg, "components", ()) or ()):
            cid = str(_jd._attr(c, "component_id"))
            if "net_profit" in cid:
                np_v = _num(_jd._attr(c, "value"))
            elif "working_capital" in cid:
                wc_v = _num(_jd._attr(c, "value"))
    driver, _roles, direction = _jd.cash_direction_roles(total, np_v, wc_v)
    od = _jd._pick(runs, "operating_drivers")
    spreads: list[dict] = []
    if od is not None:
        for o in (_jd._attr(od, "outputs", ()) or ()):
            if str(_jd._attr(o, "metric")) != "gross_profit_change_by_segment":
                continue
            vals = [_num(_jd._attr(c, "value")) for c in (_jd._attr(o, "components", ()) or ())
                    if str(_jd._attr(c, "component_id")).startswith("segment:")]
            vals = [v for v in vals if v is not None]
            if vals:
                spreads.append({"cut": str(_jd._attr(o, "label")),
                                "spread": max(vals) - min(vals)})
    periods = [int(p) for p in (snap.get("periods") or [])]
    kind = str(snap.get("period_kind") or (snap.get("source") or {}).get("period_kind")
               or "annual")
    labels = [str(x) for x in ((snap.get("source") or {}).get("period_labels") or [])] \
        or [str(p) for p in periods]
    # ① 半年报：主要会计数据＋所得税＋产销量（同一张半年报表的"本期/上年同期"两列）
    half = snap.get("half") or {}
    hrows, hvol = (half.get("rows") or {}), (half.get("volume") or {})
    # ② 年报：税费/净利读数从**冻结数据集**取（同主体、同全年、同口径）
    ds = snap.get("_ds")
    np_row = hrows.get("归属于上市公司股东的净利润") or {}
    tax_row = hrows.get("所得税费用") or {}
    if not np_row or not tax_row:
        for metric, slot in (("net_profit", "np"), ("income_tax_expense", "tax")):
            if ds is None or len(periods) < 2:
                continue
            try:
                prev = ds.get(metric, f"{periods[0]}年", caliber="合并")
                cur = ds.get(metric, f"{periods[-1]}年", caliber="合并")
            except Exception:                              # noqa: BLE001
                continue
            if prev is None or cur is None:
                continue
            cv, pv = _num(getattr(cur, "value", None)), _num(getattr(prev, "value", None))
            if cv is None or pv is None:
                continue
            if slot == "np" and not np_row:
                np_row = {"cur": cv, "prev": pv, "yoy": (cv - pv) / pv * 100 if pv else None,
                          "locator": f"冻结底稿：归母净利润（{periods[0]}年→{periods[-1]}年）",
                          "text": f"{periods[0]}年 {pv:,.2f} / {periods[-1]}年 {cv:,.2f}"}
            elif slot == "tax" and not tax_row:
                tax_row = {"cur": cv, "prev": pv, "yoy": (cv - pv) / pv * 100 if pv else None,
                           "locator": f"冻结底稿：所得税费用（{periods[0]}年→{periods[-1]}年）",
                           "text": f"{periods[0]}年 {pv:,.2f} / {periods[-1]}年 {cv:,.2f}"}
    profit_change = ((np_row.get("cur") - np_row.get("prev"))
                     if np_row.get("cur") is not None and np_row.get("prev") is not None
                     else None)
    tax_change = ((tax_row.get("cur") - tax_row.get("prev"))
                  if tax_row.get("cur") is not None and tax_row.get("prev") is not None
                  else None)
    # 税费对**利润变化**的贡献＝−Δ所得税类（与 judgments 同一约定：税负上升＝负贡献）
    tax_contribution = (-tax_change) if tax_change is not None else None
    tax_role = (""
                if (tax_contribution is None or profit_change in (None, 0)) else
                ("缓冲" if (profit_change < 0 < tax_contribution) else
                 "拖累" if (profit_change > 0 > tax_contribution) else
                 "推动" if (profit_change > 0 and tax_contribution > 0) else
                 "加重下降" if (profit_change < 0 and tax_contribution < 0) else "—"))
    # ③ 现金：补充资料三行（应付/存货/应收）的**逐行变化**，与 judgments 同源
    wc_lines: dict = {}
    if ds is not None:
        try:
            wf = _jd.wc_flow_of(ds)
        except Exception:                                  # noqa: BLE001
            wf = {}
        for key, cn in (("payable", "经营性应付项目"), ("inventory", "存货"),
                        ("receivable", "经营性应收项目")):
            _r = (wf or {}).get(key) or {}
            if _r.get("delta") is not None:
                wc_lines[key] = {"label": cn, "delta": _num(_r.get("delta")),
                                 "locator": str(_r.get("locator") or "")}
    vol_row = hvol.get("销售量") or {}
    prod_row = hvol.get("生产量") or {}
    inv_row = hvol.get("库存量") or {}
    if kind == "half":
        vol = vol_row or vol
        prod = prod_row or prod
        inv = inv_row or inv
    # 半年报的 ΔOCF：数据集里没有现金流量表总额（半年报形状）时，用**同一张半年报**的
    # 「经营活动产生的现金流量净额」两列算，并把营运资本三行合计与净利变化一起给方向。
    if kind == "half" and total is None:
        _ocf = hrows.get("经营活动产生的现金流量净额") or {}
        if _ocf.get("cur") is not None and _ocf.get("prev") is not None:
            total = float(_ocf["cur"]) - float(_ocf["prev"])
            np_v = (float(np_row["cur"]) - float(np_row["prev"])
                    if np_row.get("cur") is not None and np_row.get("prev") is not None
                    else None)
            _wc_sum = sum(float(v["delta"]) for v in wc_lines.values()
                          if v.get("delta") is not None)
            wc_v = _wc_sum if wc_lines else None
            driver, _roles, direction = _jd.cash_direction_roles(total, np_v, wc_v)
    return {
        "periods": periods,
        "period_kind": kind,
        "period_labels": labels,
        "cur_period": labels[-1] if labels else (periods[-1] if periods else None),
        "prev_period": labels[0] if labels else (periods[0] if len(periods) >= 2 else None),
        "volume_yoy": _num(vol.get("yoy")), "volume_cur": _num(vol.get("cur")),
        "volume_prev": _num(vol.get("prev")),
        "volume_period": str(vol.get("cur_period") or (labels[-1] if labels else "")),
        "volume_locator": str(vol.get("locator") or ""), "volume_line": str(vol.get("line") or ""),
        "production_yoy": _num(prod.get("yoy")),
        "inventory_yoy": _num(inv.get("yoy")), "inventory_cur": _num(inv.get("cur")),
        "inventory_prev": _num(inv.get("prev")),
        "inventory_locator": str(inv.get("locator") or ""),
        "price_yoy": _num(price.get("yoy")), "price_cur": _num(price.get("cur")),
        "price_prev": _num(price.get("prev")),
        "price_formula": str(price.get("formula") or ""),
        "price_note": str(price.get("note") or ""),
        "ocf_change": total, "cash_np": np_v, "cash_wc": wc_v,
        "cash_driver": driver, "cash_direction": direction,
        # ① 利润/税费（同口径两期；半年报取半年表、年报取冻结数据集）
        "net_profit_cur": np_row.get("cur"), "net_profit_prev": np_row.get("prev"),
        "net_profit_yoy": _num(np_row.get("yoy")), "profit_change": profit_change,
        "profit_locator": str(np_row.get("locator") or ""),
        "tax_cur": tax_row.get("cur"), "tax_prev": tax_row.get("prev"),
        "tax_yoy": _num(tax_row.get("yoy")), "tax_change": tax_change,
        "tax_contribution": tax_contribution, "tax_role": tax_role,
        "tax_locator": str(tax_row.get("locator") or ""),
        # ③ 现金结构三行
        "wc_lines": wc_lines,
        "spreads": spreads,
    }


def _implied_price(ds, periods, *, caliber: str = "分产品:白酒") -> dict:
    """数据集口径的推算吨价（收入 ÷ 销售量，两期）——披露侧没有该行时的同口径替代。

    与算子里的量价分解**同源**（同一口径、同一两期）；仍标 `note` 说明是推算，不是披露价格。
    """
    if ds is None or len(periods) < 2:
        return {}
    try:
        prev, cur = str(periods[0]) + "年", str(periods[-1]) + "年"
        rows = {}
        for tag, period in (("prev", prev), ("cur", cur)):
            rev = ds.get("revenue", period, caliber=caliber)
            vol = ds.get("sales_volume", period, caliber=caliber)
            if rev is None or vol is None:
                return {}
            rv, vv = _num(getattr(rev, "value", None)), _num(getattr(vol, "value", None))
            if not vv:
                return {}
            rows[tag] = rv / vv
        yoy = ((rows["cur"] - rows["prev"]) / rows["prev"] * 100
               if rows.get("prev") else None)
        return {"cur": rows["cur"], "prev": rows["prev"], "yoy": yoy,
                "formula": (f"({rows['cur'] * 1.0:.2f})（{cur} 收入÷销量）"),
                "note": f"由数据集按{caliber}口径推算（非披露价格）"}
    except Exception:                              # noqa: BLE001 - 取不到就当没有
        return {}



def _decide(jid: str, e: dict, l: dict, *, tol: float,
            same_kind: bool = True) -> tuple[str, str]:
    """确定性判定：`(状态, 原因)`——三种状态，缺口径/基本不变都算「无法判断」。

    `same_kind=False`（上期年度、本期半年）时**不做差值比较**（复核禁止半年与全年硬比）：
    改为用**本期同口径读数**对假说本身判方向，并在原因里写明依据与期间。
    """
    if jid == _jd.J_PROFIT_MECHANISM:
        role = str(l.get("tax_role") or "")
        pc, tc = l.get("profit_change"), l.get("tax_contribution")
        if not role or pc is None or tc is None:
            return STATUS_UNKNOWN, ("本期没有同口径的归母净利润/所得税两项："
                                    "税费贡献算不出来，不比较")
        if pc < 0 and tc > 0:
            return (STATUS_STRONGER,
                    f"税费贡献仍为正 {_yi(tc)} 亿元（缓冲），而利润仍在下降"
                    f"（{_yi(pc)} 亿元）——下跌年里的税费缓冲结构延续")
        if pc < 0 and tc < 0:
            return (STATUS_WEAKER,
                    f"税费转为**拖累**（贡献 {_yi(tc)} 亿元）：利润下降不再由税费缓冲")
        if pc > 0 and tc < 0:
            return (STATUS_STRONGER,
                    f"税费为拖累 {_yi(tc)} 亿元、利润转增（{_yi(pc)} 亿元）："
                    "「下降年的税费缓冲」已不复存在，但方向判断转为改善")
        return (STATUS_UNKNOWN,
                f"税费贡献方向不足以判定（利润变化 {_yi(pc)}、税费贡献 {_yi(tc)} 亿元）")
    if jid == _jd.J_VOLUME:
        a, b = e.get("volume_yoy"), l.get("volume_yoy")
        if b is None:
            return STATUS_UNKNOWN, "本期没有同口径销售量同比：不比较"
        if not same_kind:
            # 上期是**全年**同比、本期是**半年**同比：只对假说本身判方向，不做差值
            if b < 0:
                return (STATUS_STRONGER,
                        f"本期半年销量同比仍为负（{_pct(b)}，{l.get('cur_period')} 对 "
                        f"{l.get('prev_period')}）——与上期全年 {_pct(a)} 同为收缩，"
                        "压力未转正（**不作半年与全年差值**）")
            return (STATUS_WEAKER,
                    f"本期半年销量同比转正（{_pct(b)}）——上期全年的收缩压力未延续"
                    f"（上期 {_pct(a)}）")
        if a is None:
            return STATUS_UNKNOWN, "上期没有同口径销售量同比：不比较"
        if a < 0:                                  # 上一期是"销量压力"
            if b > 0:
                return STATUS_WEAKER, f"上一期 {_pct(a)} → 本期 {_pct(b)}（转正），压力未延续"
            if b >= a + tol:
                return (STATUS_WEAKER,
                        f"降幅收窄 {abs(b - a):.2f}pp（{_pct(a)} → {_pct(b)}）")
            if b <= a - tol:
                return (STATUS_STRONGER,
                        f"降幅扩大 {abs(b - a):.2f}pp（{_pct(a)} → {_pct(b)}）")
        else:
            if b < 0:
                return STATUS_STRONGER, f"上一期 {_pct(a)} → 本期 {_pct(b)}（转负）"
            if b <= a - tol:
                return STATUS_WEAKER, f"增速回落 {abs(b - a):.2f}pp"
            if b >= a + tol:
                return STATUS_STRONGER, f"增速继续 {abs(b - a):.2f}pp"
        return STATUS_UNKNOWN, f"降幅基本不变（{_pct(a)} → {_pct(b)}，阈值 {tol:.2f}pp）"
    if jid == _jd.J_PRICE:
        a, b = e.get("price_yoy"), l.get("price_yoy")
        if a is None or b is None:
            return STATUS_UNKNOWN, "两期里至少有一期没有同口径推算吨价：不比较"
        if a > 0 and b > 0:
            # 假说问的是"单位收入是否**继续上升**"（水平方向）；增幅变化要写出来，
            # 但不能把"仍在上升但放缓"写成反向（那会把水平与动量混为一谈）。
            if b <= a - tol:
                return (STATUS_STRONGER,
                        f"推算吨价同比仍为正，但增幅收窄 {abs(b - a):.2f}pp"
                        f"（{_pct(a)} → {_pct(b)}）")
            if b >= a + tol:
                return (STATUS_STRONGER,
                        f"推算吨价同比仍为正且增幅扩大 {abs(b - a):.2f}pp"
                        f"（{_pct(a)} → {_pct(b)}）")
            return STATUS_STRONGER, f"推算吨价同比仍为正（{_pct(a)} → {_pct(b)}）"
        if a > 0 >= b:
            return STATUS_WEAKER, f"推算吨价同比转负（{_pct(a)} → {_pct(b)}）"
        if a <= 0 < b:
            return STATUS_WEAKER, f"推算吨价同比转正（{_pct(a)} → {_pct(b)}）"
        return STATUS_UNKNOWN, f"方向不足以判定（{_pct(a)} → {_pct(b)}）"
    if jid == _jd.J_INVENTORY:
        a, b = e.get("inventory_yoy"), l.get("inventory_yoy")
        if b is None:
            return STATUS_UNKNOWN, "本期没有同口径库存量同比：不比较"
        if not same_kind:
            if b > 0:
                return (STATUS_STRONGER,
                        f"本期半年库存量同比仍为正（{_pct(b)}）——累积未转向消化"
                        f"（上期全年 {_pct(a)}，**不作半年与全年差值**）")
            return (STATUS_WEAKER,
                    f"本期半年库存量同比转负（{_pct(b)}）——由上期全年的累积转为下降，"
                    "消化出现（企业口径，不代表渠道库存）")
        if a is None:
            return STATUS_UNKNOWN, "上期没有同口径库存量同比：不比较"
        if a > 0 and b > 0:
            return (STATUS_STRONGER,
                    f"库存量同比仍为正（{_pct(a)} → {_pct(b)}），累积未转向消化")
        if a > 0 >= b:
            return STATUS_WEAKER, f"库存量由累积转为下降（{_pct(a)} → {_pct(b)}）"
        if a <= 0 < b:
            return STATUS_WEAKER, f"库存量由下降转为累积（{_pct(a)} → {_pct(b)}）"
        return STATUS_STRONGER, f"库存量继续下降（{_pct(a)} → {_pct(b)}），消化延续"
    if jid == _jd.J_CASH_WORKING_CAPITAL:
        lines = l.get("wc_lines") or {}
        pay, rec = lines.get("payable") or {}, lines.get("receivable") or {}
        has_lines = pay.get("delta") is not None and rec.get("delta") is not None
        a, b = e.get("cash_driver"), l.get("cash_driver")
        if same_kind and a and b and a != b:
            # 同期同口径下主导项换了：X1 的既有判定（结构规则的补充，不是替代）
            return (STATUS_WEAKER,
                    f"主导项换了（{a} → {b}）：ΔOCF {_yi(e.get('ocf_change'))} → "
                    f"{_yi(l.get('ocf_change'))} 亿元")
        if not has_lines:
            if same_kind and a and b and a == b:
                return (STATUS_STRONGER,
                        f"主导项同向延续（{a} → {b}）：ΔOCF {_yi(e.get('ocf_change'))} → "
                        f"{_yi(l.get('ocf_change'))} 亿元")
            return (STATUS_UNKNOWN,
                    "本期没有同口径的补充资料三行（应付/应收）：缓冲结构不比较")
        pay_d, rec_d = _num(pay.get("delta")), _num(rec.get("delta"))
        # 缓冲**只来自应付**的读法：应收由拖累转为释放即结构改变（与期间长度无关）
        if rec_d is not None and rec_d > 0 and pay_d is not None and pay_d > 0:
            _prev_rec = _num(((e.get("wc_lines") or {}).get("receivable") or {}).get("delta"))
            _extra = (f"（上期应收 {_yi(_prev_rec)} 亿元为拖累）"
                      if _prev_rec is not None and _prev_rec < 0 else "")
            return (STATUS_WEAKER,
                    f"缓冲不再只来自应付：本期应收项转为**释放** {_yi(rec_d)} 亿元"
                    f"{_extra}，应付项 {_yi(pay_d)} 亿元仍是主要支撑")
        if pay_d is not None and pay_d > 0 and (rec_d is None or rec_d <= 0):
            return (STATUS_STRONGER,
                    f"缓冲仍主要来自应付项 {_yi(pay_d)} 亿元"
                    + (f"，应收项 {_yi(rec_d)} 亿元仍是拖累" if rec_d is not None else ""))
        return (STATUS_UNKNOWN,
                f"补充资料三行方向不足以判定（应付 {_yi(pay_d)}、应收 {_yi(rec_d)} 亿元）")
    if jid == _jd.J_STRUCTURE:
        a = (e.get("spreads") or [{}])[0].get("spread")
        b = (l.get("spreads") or [{}])[0].get("spread")
        if a is None or b is None or not a:
            return STATUS_UNKNOWN, "两期里至少有一期没有同口径分段毛利：不比较"
        ratio = abs(b) / abs(a)
        if ratio <= 1 - SPREAD_TOL:
            return STATUS_WEAKER, f"结构离散度收敛（{_yi(a)} → {_yi(b)} 亿元）"
        if ratio >= 1 + SPREAD_TOL:
            return STATUS_STRONGER, f"结构离散度扩大（{_yi(a)} → {_yi(b)} 亿元）"
        return STATUS_UNKNOWN, f"结构离散度基本不变（比值 {ratio:.2f}）"
    return STATUS_UNKNOWN, "没有该假说的判定规则"


def compare(earlier: dict, later: dict, *, limit: int = 3,
            tolerance_pp: float = TOLERANCE_PP,
            select_ids: list[str] | None = None) -> dict:
    """较早快照的假说 × 较新快照的同口径读数 → 一页（普通 dict）。

    10-07 第二次复核：按**明确业务身份**取页（默认＝三条命题，见 `PROPOSITIONS`），
    不再拿"旧 judgments 前 N 条"凑；期间长度不同（年度 vs 半年）时不做差值比较。
    """
    e_read, l_read = readings_of(earlier), readings_of(later)
    same_kind = str(e_read.get("period_kind")) == str(l_read.get("period_kind"))
    if select_ids is None:
        # **既有行为保留**（X1 年度→年度：量／吨价／库存／现金，按 judgments 顺序取前 N 条）；
        # 新样品（2024 判断 → 2025H1）用**明确业务身份**传 `select_ids=proposition_ids()`。
        want = [str(j.get("judgment_id") or "")
                for j in (earlier.get("judgments") or [])[:max(1, int(limit))]]
    else:
        want = [str(x) for x in select_ids]
    _by_id = {str(j.get("judgment_id") or ""): j for j in (earlier.get("judgments") or [])}
    rows: list[dict] = []
    for jid in want:
        j = _by_id.get(jid)
        if j is None:
            continue
        spec = HYPOTHESES.get(jid)
        if not spec:                                # 目录里没有的 ID 不硬编
            continue
        status, reason = _decide(jid, e_read, l_read, tol=tolerance_pp,
                                 same_kind=same_kind)
        rows.append({
            "judgment_id": jid,
            "proposition": proposition_of(jid),
            "hypothesis": spec["title"],
            "basis": str(j.get("title") or ""),
            "basis_readings": list(j.get("numbers") or ())[:2],
            "observation_condition": spec["condition"],
            "basis_source": dict(earlier.get("source") or {}),
            "new_readings": _new_readings(jid, l_read),
            "new_locators": _new_locators(jid, l_read),
            "status": status,
            "status_reason": reason,
            "decision_basis": ("同期同口径相减" if same_kind else
                               "期间长度不同：只按本期同口径读数对假说判方向，不相减"),
            "next_watch": list(j.get("watch") or ())[:3],
        })
    return {
        "schema": "weavemind.research_continuation/1",
        "propositions": [{"name": n, "judgment_ids": list(ids)} for n, ids in PROPOSITIONS],
        "period_kind": {"earlier": e_read.get("period_kind"),
                        "later": l_read.get("period_kind"), "same": same_kind},
        "earlier": {"label": earlier.get("label"), "source": earlier.get("source"),
                    "dataset": earlier.get("dataset"),
                    "runs": {k: str(_jd._attr(v, "run_id"))
                             for k, v in (earlier.get("runs") or {}).items()}},
        "later": {"label": later.get("label"), "source": later.get("source"),
                  "dataset": later.get("dataset"),
                  "runs": {k: str(_jd._attr(v, "run_id"))
                           for k, v in (later.get("runs") or {}).items()}},
        "hypotheses": rows,
        "overlap_check": overlap_check(earlier, later),
        "readings": {"earlier": e_read, "later": l_read},
        "historical_note": ("历史观察与持续性假说分开：本期读数只改变"
                            "「上一期的压力/结构是否延续」，**不推翻**上一期已经发生的事实；"
                            "上期年度值与本期半年值**不相减**（期间长度不同）。"),
        "limits": list(LIMITS),
        "next_watch": [w for r in rows for w in (r.get("next_watch") or [])][:5],
    }


def _new_locators(jid: str, l: dict) -> list[str]:
    """本期新读数的**原件定位**（页码/小节；取不到就空）——复核要求一页里带定位。"""
    out: list[str] = []
    if jid == _jd.J_PROFIT_MECHANISM:
        for k in ("profit_locator", "tax_locator"):
            if l.get(k):
                out.append(str(l[k]))
    elif jid == _jd.J_VOLUME:
        if l.get("volume_locator"):
            out.append(str(l["volume_locator"]))
    elif jid == _jd.J_INVENTORY:
        if l.get("inventory_locator"):
            out.append(str(l["inventory_locator"]))
    elif jid == _jd.J_CASH_WORKING_CAPITAL:
        for _k, row in (l.get("wc_lines") or {}).items():
            if row.get("locator"):
                out.append(f"{row.get('label')}：{row['locator']}")
    return out


def _new_readings(jid: str, l: dict) -> list[str]:
    """本期关键新读数（同口径）——只写取到的；取不到就写缺口，不凑方向。"""
    out: list[str] = []
    if jid == _jd.J_VOLUME:
        if l.get("volume_yoy") is not None:
            out.append(f"销售量（{l.get('volume_period')}）{_pct(l['volume_yoy'])}"
                       + (f"，生产量 {_pct(l['production_yoy'])}"
                          if l.get("production_yoy") is not None else ""))
        if l.get("volume_locator"):
            out.append("位置：" + str(l["volume_locator"])[:120])
    elif jid == _jd.J_INVENTORY:
        if l.get("inventory_yoy") is not None:
            out.append(f"库存量 {_pct(l['inventory_yoy'])}"
                       + (f"（{l['inventory_cur']:,.2f} 吨）"
                          if l.get("inventory_cur") is not None else ""))
        if l.get("inventory_locator"):
            out.append("位置：" + str(l["inventory_locator"])[:120])
    elif jid == _jd.J_PROFIT_MECHANISM:
        if l.get("net_profit_yoy") is not None:
            out.append(f"归母净利润 {_pct(l['net_profit_yoy'])}"
                       + (f"（{l['net_profit_cur']:,.2f}）"
                          if l.get("net_profit_cur") is not None else "")
                       + (f"；利润变化 {_yi(l.get('profit_change'))} 亿元"
                          if l.get("profit_change") is not None else ""))
        if l.get("tax_yoy") is not None:
            out.append(f"所得税费用 {_pct(l['tax_yoy'])}"
                       + (f"；税费对利润变化的贡献 {_yi(l.get('tax_contribution'))} 亿元"
                          f"（{l.get('tax_role')}）"
                          if l.get("tax_contribution") is not None else ""))
        if l.get("wc_lines"):
            _l3 = "、".join(f"{v.get('label')} {_yi(v.get('delta'))} 亿元"
                            for v in (l.get("wc_lines") or {}).values())
            out.append("补充资料三行（同口径两期变化）：" + _l3)
    elif jid == _jd.J_PRICE:
        if l.get("price_yoy") is not None:
            out.append(f"推算吨价 {_pct(l['price_yoy'])}"
                       + (f"（{l['price_cur']:,.0f} 元/吨）"
                          if l.get("price_cur") is not None else ""))
        out.append("口径：吨价为推算（非披露价格），含产品结构混合")
    elif jid == _jd.J_CASH_WORKING_CAPITAL:
        if l.get("ocf_change") is not None:
            out.append(f"ΔOCF {_yi(l['ocf_change'])} 亿元：合并净利润项 "
                       f"{_yi(l.get('cash_np'))} 亿元、营运资本项 {_yi(l.get('cash_wc'))} 亿元")
        if l.get("wc_lines"):
            out.append("补充资料三行（同口径两期变化）："
                       + "、".join(f"{v.get('label')} {_yi(v.get('delta'))} 亿元"
                                   for v in (l.get("wc_lines") or {}).values()))
        if l.get("cash_direction"):
            out.append("方向：" + str(l["cash_direction"])[:160])
    elif jid == _jd.J_STRUCTURE:
        for s in (l.get("spreads") or [])[:2]:
            out.append(f"{s.get('cut')}：离散度 {_yi(s.get('spread'))} 亿元")
    return out or ["本期没有同口径读数（材料未取得或不适用）"]


def overlap_check(earlier: dict, later: dict, *, tol: float = 0.01) -> dict:
    """两份材料**共同比较期**的读数交叉核对（重述/口径变化要注明用哪一组）。

    例：较早材料（本期 2023／上期 2022）与较新材料（本期 2024／上期 2023）都在
    `2023` 有读数——必须拿**同一期**比：较早材料的**本期**对较新材料的**上期**。
    （第一版曾拿两侧的"本期"直接比，于是 2023 对上了 2024，读数必然不同——这是错的。）
    两者不一致说明重述或口径变化，本页如实记录并采用**较新材料**的比较组。
    """
    e, l = [int(p) for p in (earlier.get("periods") or [])], \
        [int(p) for p in (later.get("periods") or [])]
    shared = sorted(set(e) & set(l))
    er, lr = readings_of(earlier), readings_of(later)
    # 10-07 第二次复核：**期间长度不同的两份材料不能"同一比较期"互核**——
    # 较早材料的 2024 是**全年**、较新材料的 2024 是**上半年**，数值本来就不同，
    # 说成"重述/口径变化"是误导。这种情况只登记口径差异，不判不一致。
    if str(er.get("period_kind")) != str(lr.get("period_kind")):
        return {"shared_periods": shared, "items": [],
                "verdict": ("期间长度不同（较早 "
                            f"{er.get('period_kind')}／较新 {lr.get('period_kind')}）："
                            "同名年份不是同一比较期，**不作一致性判断**、不相减；"
                            "本期读数只用较新材料自己的同期两列")}

    def _at(readings: dict, periods: list, shared_period: int, stem: str):
        if shared_period == (periods[-1] if periods else None):
            return readings.get(f"{stem}_cur")
        if shared_period == (periods[0] if len(periods) >= 2 else None):
            return readings.get(f"{stem}_prev")
        return None

    items: list[dict] = []
    for metric, stem in (("白酒销售量", "volume"), ("白酒库存量", "inventory"),
                         ("推算吨价", "price")):
        a = _at(er, e, shared[-1], stem) if shared else None
        b = _at(lr, l, shared[-1], stem) if shared else None
        if a is None or b is None:
            items.append({"metric": metric, "shared_period": shared[-1] if shared else None,
                          "earlier_material": a, "later_material": b,
                          "same": None, "note": "至少一侧未取得，无法交叉核对"})
            continue
        same = abs(a - b) <= max(abs(a), abs(b)) * tol
        items.append({"metric": metric, "shared_period": shared[-1] if shared else None,
                      "earlier_material": a, "later_material": b, "same": bool(same),
                      "note": ("两份材料在同一比较期一致" if same else
                               "两份材料在同一比较期不一致：可能是重述或口径变化，"
                               "本页采用较新材料的比较组")})
    return {"shared_periods": shared, "items": items,
            "verdict": ("共同比较期读数一致" if items and all(i["same"] for i in items)
                        else "存在不一致或未取得：见各项说明")}


def assumption_impact(ds, *, target_yuan=None, revenue_growth: float = 0.0,
                      gross_margin_delta: float = 0.0) -> dict:
    """同一目标下 **fixed／detail** 两种假设规则的条件对照（解释"假设为什么重要"）。

    阶段X §4 要求：不让用户只看到一个未说明假设的阈值。这里用**现有求值器**跑两次
    （fixed＝毛利线以下按一个整块；detail＝费用随收入、税率与少数股东按基期比例），
    把同一目标下的收入侧/毛利率侧条件并排给出，并说明差异来自**规则**而不是材料。
    """
    import financial_analysis as fa

    out: dict = {"target_net_profit": None, "modes": {},
                 "note": ("两种模式的目标是**同一个**（默认上一期归母净利）：差异来自"
                          "毛利线以下的费用/税率/少数股东规则，不是材料或目标变了")}
    for mode in ("fixed", "detail"):
        params = {"revenue_growth": float(revenue_growth),
                  "gross_margin_delta": float(gross_margin_delta),
                  "below_gross_mode": mode}
        if target_yuan is not None:
            params["target_net_profit"] = float(target_yuan)
        run = fa.run("scenario_sensitivity", ds, params=params)
        th = _jd._out(run, "margin_threshold_to_hold_base_profit")
        rev = _jd._out(run, "revenue_growth_to_hold_target")
        diag = dict(_diag(run).get("thresholds") or {})
        out["modes"][mode] = {
            "status": str(run.status),
            "revenue_growth_to_hold_target": (_num(_jd._attr(rev, "value"))
                                              if rev is not None else None),
            "margin_threshold": (_num(_jd._attr(th, "value")) if th is not None else None),
            "target_net_profit": diag.get("target_net_profit"),
        }
        if out["target_net_profit"] is None and diag.get("target_net_profit") is not None:
            out["target_net_profit"] = diag.get("target_net_profit")
    f, d = out["modes"].get("fixed") or {}, out["modes"].get("detail") or {}
    if f.get("revenue_growth_to_hold_target") is not None \
            and d.get("revenue_growth_to_hold_target") is not None:
        # 两个读数都是**百分数**（15.8226 = +15.8226%），差额直接相减就是百分点
        out["revenue_gap_pp"] = (float(d["revenue_growth_to_hold_target"])
                                 - float(f["revenue_growth_to_hold_target"]))
    return out


def render_page(page: dict, *, heading: str = "## 研究续页（可检验）",
                compact: bool = False) -> str:
    """一页（markdown）：上次假说 → 本期关键新读数 → 判断怎样改变 → 下一观察。

    `compact=True`：**读者主文**的一页版（10-07 第二次复核 §阅读层"续页1页"）——
    每格只留结论与关键读音，逐行重复的判定口径提成一行脚注，旧判断的读数明细与
    出处定位不再占版面（完整页仍由 `compact=False` 渲染并写进同版附件
    `analysis/continuation.md`，主文与附件同源、不丢信息）。
    """
    lines = [heading, ""]
    if compact:
        lines.append("> 由 `financial_analysis.continuation` 装配：假说沿用既有判断 ID 与观察"
                     "条件，读数取同口径披露。**历史材料回放**，不是事前预测。")
    else:
        lines.append("> 本页由 `financial_analysis.continuation` 从**两份已验证材料**装配："
                     "假说沿用既有判断 ID 与它自己的观察条件，读数取同口径披露/运行输出；"
                     "缺同口径读数就写「无法判断」。**历史材料回放**，不是事前预测。")
    lines.append("")
    e = (page.get("earlier") or {}).get("source") or {}
    l = (page.get("later") or {}).get("source") or {}
    lines.append(f"- 较早材料：{e.get('title') or '—'}（{e.get('disclosure_date') or '—'}，"
                 f"比较期 {'/'.join(str(p) for p in (e.get('periods') or []))}）")
    lines.append(f"- 较新材料：{l.get('title') or '—'}（{l.get('disclosure_date') or '—'}，"
                 f"比较期 {'/'.join(str(p) for p in (l.get('periods') or []))}）")
    lines.append("")
    lines.append("| 上次持续性假说／依据 | 本期关键新读数（同口径） | 判断怎样改变 | 下一观察 |")
    lines.append("|---|---|---|---|")
    _bases: list[str] = []
    for r in (page.get("hypotheses") or []):
        # "待检验假设："不是套话：这一列本来就是**假说**，写上它才不会让
        # 归因核验把续页的假说当成"肯定性归因"（复核：续页曾因此被判缺证归因）。
        _hyp = str(r.get("hypothesis") or "")
        _cond = str(r.get("observation_condition") or "")
        if compact:
            # 一页版：假说＋它自己的观察条件（复核要求两者都在），旧判断的读数明细
            # 留给同版附件——主文这里只放"这条假说要检验什么"。
            basis = "待检验假设：" + _hyp
            if _cond:
                basis += f"（原观察条件：{_cell(_cond, 46)}）"
        else:
            basis = "；".join(["待检验假设：" + _hyp]
                              + list(r.get("basis_readings") or []))
            if _cond:
                basis += "；原观察条件：" + _cond
        new = "；".join(str(x) for x in (r.get("new_readings") or []))
        status = f"**{r.get('status')}**——{r.get('status_reason')}"
        if r.get("decision_basis"):
            _bases.append(str(r["decision_basis"]))
            if not compact:
                status += f"（判定口径：{r['decision_basis']}）"
        watch = "；".join(str(x) for x in (r.get("next_watch") or [])) or "—"
        if compact:
            lines.append(f"| {_cell(basis, 100)} | {_cell(new, 90)} "
                         f"| {_cell(status, 65)} | {_cell(watch, 50)} |")
        else:
            lines.append(f"| {_cell(basis)} | {_cell(new)} | {_cell(status)} | {_cell(watch)} |")
    if compact and _bases:
        # 逐行重复的判定口径**提成一行脚注**（四行各印一遍曾是续页跨两页的主因之一）
        _uniq = list(dict.fromkeys(_bases))
        lines.append("")
        lines.append("- **判定口径**（各行相同）：" + _cell("；".join(_uniq), 45))
    _locs = [(r.get("proposition") or "", x) for r in (page.get("hypotheses") or [])
             for x in (r.get("new_locators") or [])]
    if _locs:
        lines.append("")
        if compact:
            # 同一页/同一小节被多条命题引用时会重复印（实机：第 11 页那张表印了两遍）
            _seen: list[str] = []
            for _p, x in _locs:
                x = _cell(x, 60)
                if x not in _seen:
                    _seen.append(x)
            lines.append("- **本期读数的原件定位**：" + "；".join(_seen[:6]))
        else:
            lines.append("- **本期读数的原件定位**：")
            for _p, x in _locs[:8]:
                lines.append(f"  - {_cell(x, 160)}")
    lines.append("")
    oc = page.get("overlap_check") or {}
    if oc.get("shared_periods"):
        # 交叉核对的判定句**不截断**：它是"半年不与全年硬比"的护栏，
        # 截到"不作一致性…"正好把那半句护栏砍掉（实机压页时出现过）。
        lines.append(f"- **共同比较期交叉核对**（{'、'.join(str(p) for p in oc['shared_periods'])}）："
                     f"{oc.get('verdict')}")
        for it in (oc.get("items") or []):
            if it.get("same") is False:
                if compact:
                    # 一页版：同一比较期不一致的**事实**留着（它挡"半年硬比全年"），
                    # 逐项两期数值不重复印——附件的完整页里有。
                    lines.append(f"  - {it.get('metric')}：{it.get('note')}")
                    continue
                lines.append(f"  - {it.get('metric')}：较早材料 "
                             f"{_fmt(it.get('earlier_material'))} vs 较新材料 "
                             f"{_fmt(it.get('later_material'))}——{it.get('note')}")
    ai = page.get("assumption_impact") or {}
    if ai.get("modes"):
        f, d = ai["modes"].get("fixed") or {}, ai["modes"].get("detail") or {}
        tgt = ai.get("target_net_profit")
        _fr, _dr = (f.get("revenue_growth_to_hold_target"),
                    d.get("revenue_growth_to_hold_target"))
        if _fr is None or _dr is None:
            # 一侧没有读数就**不并排比较**：detail 模式下毛利线以下随收入变化，
            # "维持目标所需收入变化"这个单因素反解在本材料里没有解（不是零）。
            lines.append(f"- **假设为什么重要**（同一目标归母净利 {_fmt(tgt)} 元）："
                         f"fixed 模式取到 {_pct2(_fr)}、detail 模式**未取到**"
                         "（毛利线以下随收入变化的规则下，单因素反解无解）——"
                         "**不并排比较、不据此说差异来自规则**；要看规则差异得换"
                         "同一目标下两种模式的**情景净利**，本页不给该结论")
        else:
            lines.append(f"- **假设为什么重要**（同一目标归母净利 {_fmt(tgt)} 元）："
                         f"fixed 模式需收入 {_pct2(_fr)}、"
                         f"detail 模式需 {_pct2(_dr)}"
                         + (f"（差 {ai['revenue_gap_pp']:+.2f}pp）"
                            if ai.get("revenue_gap_pp") is not None else "")
                         + "——差异来自毛利线以下的费用/税率/少数股东**规则**，不是材料变化")
    _hist = str(page.get("historical_note") or "")
    # 同理不截断：这句是"历史事实不被新材料重写、半年与全年不相减"的护栏本身。
    lines.append(f"- **历史观察与假说分开**：{_hist}")
    lines.append("- **本页限制**：")
    _limits = list(page.get("limits") or [])
    if compact:
        # 一页版：限制**照样写**（边界不因压版面消失），只是每条压到一句
        # （复核 §阅读层：不靠删边界压页）。`历史材料回放`/`无法判断`两条始终保留。
        _keep = [t for t in _limits if "无法判断" in t]
        _limits = _keep or _limits[:1]
        for t in _limits:
            lines.append(f"  - {_cell(t, 60)}")
    else:
        for t in _limits:
            lines.append(f"  - {t}")
    lines.append("")
    return "\n".join(lines)


def _pct2(v) -> str:
    """阈值已经是**百分数**（15.8226 = +15.8226%）——不再乘 100（第一版乘了，印成 +1582%）。"""
    n = _num(v)
    return "—" if n is None else f"{n:+.4f}%"


def _fmt(v) -> str:
    n = _num(v)
    return "—" if n is None else f"{n:,.4f}".rstrip("0").rstrip(".")


def _cell(text: str, limit: int = 240) -> str:
    s = str(text or "").replace("|", "／").replace("\n", " ").strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "…"
