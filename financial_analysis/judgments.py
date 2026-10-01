# -*- coding: utf-8 -*-
"""W2（阶段W）：**可检验的研究判断**——把已验证运行与已准入披露变成"能下判断、也知道什么会推翻它"。

每条判断固定七段（阶段W §5）：

    判断 → 数字与贡献 → 原句/原件位置 → 支持边界 → 本公司替代解释 → 后续指标与反转条件 → 缺口

纪律（与全文一致，写进代码而不是注释）：

- 判断**由读数驱动**：规则读已验证运行的输出与已准入的量价/结构事实，不按公司名硬编码结论；
  读数缺了就如实写缺口，不生成判断（宁可少一条，也不编一条）。
- 区分**已发生解释**（披露原句）、**发行人归因**、**未来计划/假设**与**背景**：本模块只把
  "解释该指标变化"的段落当支持；关键词命中只作候选，不作支持。
- 反面条件必须写出：一条不能被任何读数推翻的"判断"是口号，不是研究。
- 人工复核与机器验收分开：本模块只产出**研究判断**，不改任何状态、不代表真人通过。
"""
from __future__ import annotations

from decimal import Decimal

# 判断的稳定 id（正文/底稿/测试共用；文案可改，身份不变）
J_VOLUME = "volume_contraction"
J_PRICE = "unit_revenue_not_proof_of_pricing"
J_INVENTORY = "finished_goods_inventory_build"
J_CASH_WORKING_CAPITAL = "cash_from_working_capital"

KIND_LABELS = {"observed": "已发生（读数）", "company_claim": "发行人归因（原句）",
               "assumption": "假设（未发生）", "background": "背景"}


def _d(v) -> Decimal:
    return Decimal(str(v))


def _attr(obj, name, default=""):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _yi(value) -> str:
    n = _num(value)
    if n is None:
        return "—"
    yi = n / 1e8
    return f"{yi:+,.2f}".replace("+-", "-")


def _pct(value) -> str:
    n = _num(value)
    return "—" if n is None else f"{n:+.2f}%"


def _pp(value) -> str:
    n = _num(value)
    return "—" if n is None else f"{n:+.2f}pp"


def _out(run, metric: str):
    for o in (_attr(run, "outputs", ()) or ()):
        if str(_attr(o, "metric")) == metric:
            return o
    return None


def _pick(runs, model_id: str):
    for r in (runs or ()):
        if str(_attr(r, "model_id")) == model_id:
            return r
    return None


def _facts_of(volume_price: dict | None, group: str = "实物量") -> list[dict]:
    return [f for f in ((volume_price or {}).get("facts") or ())
            if isinstance(f, dict) and str(f.get("group") or "") == group]


def _fact(volume_price: dict | None, keyword: str) -> dict:
    return next((f for f in _facts_of(volume_price) if keyword in str(f.get("row_label") or "")),
                {})


def _vp_derived(volume_price: dict | None, keyword: str) -> dict:
    return next((d for d in ((volume_price or {}).get("derived") or ())
                 if isinstance(d, dict) and keyword in str(d.get("label") or "")), {})


def _snippet_for(records, terms, limit: int = 1) -> list[dict]:
    """已准入段落里与这些词相关的原句（严格打分：小节名 +3／段首 +2／段内 +1，<2 不绑）。"""
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


def _judgment(*, jid, title, numbers, evidence, boundary, alternatives, watch,
              kind="observed", gaps=()) -> dict:
    return {"judgment_id": jid, "title": title, "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "numbers": list(numbers), "evidence": list(evidence),
            "boundary": str(boundary), "alternatives": list(alternatives),
            "watch": list(watch), "gaps": list(gaps)}


def research_judgments(runs, *, volume_price: dict | None = None,
                       records=None, limit: int = 3) -> list[dict]:
    """已验证运行＋已准入量价/结构事实＋已准入段落 → 最多 `limit` 条**可检验判断**。

    规则（洋河式"销量/单位收入/库存"与现金两条主线，都由读数触发）：

    - 量：销量同比下降、且量价分解里销量效应为负、且其绝对值不小于价格效应
      → 「销量收缩是收入下降的重要数量观察」（不是"需求崩塌"：那是机制判断，材料不够）；
    - 价：单位价格效应为正 → 「单位收入上升不足以证明提价能力」（均价含结构混合、价无披露口径）；
    - 库存：销量下降而**企业成品库存量**上升 → 「库存积累把销量恢复与库存消化变成优先观察事项」
      （企业出货/库存 ≠ 终端消费或渠道库存，机制缺口如实列出）；
    - 现金：ΔOCF 的主要构成来自营运资本 → 「现金改善的持续性取决于占用与时点是否反转」。
    """
    out: list[dict] = []
    od = _pick(runs, "operating_drivers")
    cash = _pick(runs, "cash_reconciliation")
    vp = volume_price or {}
    vp_ok = bool(vp.get("ok"))

    vpe = _out(od, "volume_price_decomposition") if od is not None else None
    if vpe is None and od is not None:
        vpe = next((o for o in (_attr(od, "outputs", ()) or ())
                    if "volume_price" in str(_attr(o, "metric"))), None)
    vol_eff = price_eff = None
    if vpe is not None:
        for c in (_attr(vpe, "components", ()) or ()):
            cid = str(_attr(c, "component_id") or "")
            if "volume" in cid or "销量" in str(_attr(c, "label")):
                vol_eff = _num(_attr(c, "value"))
            elif "price" in cid or "价" in str(_attr(c, "label")):
                price_eff = _num(_attr(c, "value"))

    sales = _fact(vp, "销售量") if vp_ok else {}
    prod = _fact(vp, "生产量") if vp_ok else {}
    inv = _fact(vp, "库存量") if vp_ok else {}
    ton_price = _vp_derived(vp, "吨价") if vp_ok else {}

    # ① 量：销量收缩是不是收入下降的主要数量观察
    if vol_eff is not None and vol_eff < 0 and (
            price_eff is None or abs(vol_eff) >= abs(price_eff)):
        numbers = [f"量价分解：销量效应 {_yi(vol_eff)} 亿元"
                   + (f"、单位价格效应 {_yi(price_eff)} 亿元（含结构混合）"
                      if price_eff is not None else "")]
        if sales:
            numbers.append(f"披露销售量 {_num(sales.get('cur')):,.2f} 吨，"
                           f"同比 {_pct(sales.get('yoy'))}"
                           + (f"（{_num(sales.get('prev')):,.2f} 吨）"
                              if _num(sales.get("prev")) is not None else ""))
        evidence = []
        if sales.get("locator"):
            evidence.append({"type": "披露原句", "text": str(sales.get("line") or ""),
                             "locator": str(sales.get("locator") or ""),
                             "kind": "observed"})
        for r in _snippet_for(records, ("销售量", "销量", "产销量")):
            evidence.append({"type": "已准入段落", "text": str(r.get("snippet") or "")[:160],
                             "locator": str(r.get("locator") or ""), "kind": "observed"})
        out.append(_judgment(
            jid=J_VOLUME, title="销量收缩是收入下降的重要「数量」观察（不是需求结论）",
            numbers=numbers, evidence=evidence,
            boundary=("销量与收入都来自发行人披露，只能说明**数量在收缩**；"
                      "需求量、渠道库存与终端动销不在本次材料里，不能由销量推断"),
            alternatives=["发货与确认节奏调整（时点）", "产品结构与统计口径变化",
                          "公司主动控货/去库存"],
            watch=["下一期同口径销售量（与生产量对照）",
                   "收入降幅是否小于销量降幅（结构/价格是否在托底）"],
            gaps=["渠道库存与终端动销材料未取得（机制缺口，不臆造）"]))
    elif vp_ok or vpe is not None:
        out.append(_judgment(
            jid=J_VOLUME, title="销量与收入的数量关系：本次不足以下判断",
            numbers=[("量价分解未取到销量/单位收入效应"
                      if vol_eff is None else f"销量效应 {_yi(vol_eff)} 亿元不为负")],
            evidence=[], boundary="读数不足：不生成「销量收缩」结论，先补量价与产销量表",
            alternatives=[], watch=["补齐分产品产销量与收入构成后重算"],
            gaps=["量价分解或产销量披露缺失"]))

    # ② 价：单位收入上升 ≠ 提价能力
    if price_eff is not None and price_eff > 0:
        numbers = [f"单位价格效应 {_yi(price_eff)} 亿元（量价分解，含产品结构混合）"]
        if ton_price:
            numbers.append(f"推算吨价 {_num(ton_price.get('cur')):,.0f} 元/吨，"
                           f"同比 {_pct(ton_price.get('yoy'))}"
                           "（发行人未直接披露吨价，为推算口径）")
        evidence = ([{"type": "披露原句", "text": str(ton_price.get("formula") or ""),
                      "locator": "收入构成与产销量表（推算，非披露价格）", "kind": "observed"}]
                    if ton_price else [])
        out.append(_judgment(
            jid=J_PRICE, title="单位收入上升不足以证明「提价能力」",
            numbers=numbers, evidence=evidence,
            boundary=("均价由「该口径收入 ÷ 销量」算出，含产品结构混合；"
                      "价无披露口径时不得命名「提价效果」，也不能把结构上移读成提价"),
            alternatives=["产品结构上移（高档占比提高）", "渠道/销售模式结构变化",
                          "并表范围或口径变化"],
            watch=["分产品收入与销量（同口径）", "公司对价格调整的披露原文（若有）"],
            gaps=([] if ton_price else ["吨价推算所需的同口径收入/销量未取全"])))
    elif vp_ok:
        out.append(_judgment(
            jid=J_PRICE, title="单位收入与提价能力：本次无从判断",
            numbers=["量价分解里的单位价格效应未取到"],
            evidence=[], boundary="没有同口径的量价分解就不评价价格，不用毛利率差替代",
            alternatives=[], watch=["补齐分产品收入与销量后重算"], gaps=["量价分解缺失"]))

    # ③ 库存：企业成品库存积累 ⇒ 销量恢复/库存消化优先观察
    if inv and _num(inv.get("yoy")) is not None and _num(inv.get("yoy")) > 0:
        numbers = [f"企业**成品**库存量 {_num(inv.get('cur')):,.2f} 吨，"
                   f"同比 {_pct(inv.get('yoy'))}"
                   + (f"（上期 {_num(inv.get('prev')):,.2f} 吨）"
                      if _num(inv.get("prev")) is not None else "")]
        if prod:
            numbers.append(f"生产量同比 {_pct(prod.get('yoy'))}"
                           "（产量降幅小于销量降幅时，库存被动积累）")
        evidence = [{"type": "披露原句", "text": str(inv.get("line") or ""),
                     "locator": str(inv.get("locator") or ""), "kind": "observed"}]
        for r in _snippet_for(records, ("库存", "存货", "产销")):
            evidence.append({"type": "已准入段落", "text": str(r.get("snippet") or "")[:160],
                             "locator": str(r.get("locator") or ""), "kind": "observed"})
        out.append(_judgment(
            jid=J_INVENTORY,
            title="企业成品库存积累 → **销量恢复与库存消化**是优先观察事项",
            numbers=numbers, evidence=evidence,
            boundary=("这是**企业口径**的成品库存（资产负债表/产销量表），"
                      "**不等于**终端消费或渠道库存；库存上升也可能来自发货与确认节奏"),
            alternatives=["为旺季/新品备货（主动）", "渠道回款与发货节奏（时点）",
                          "产品结构变化导致的结构性库存"],
            watch=["下一期同口径库存量与销售量（库存/销量比）",
                   "存货跌价准备与库龄披露（若有）",
                   "下一期销量是否恢复且库存压力下降"],
            gaps=["渠道库存与终端动销未取得：机制判断留待补料"]))

    # ④ 现金：改善来自营运资本 ⇒ 持续性取决于占用是否反转
    chg = _out(cash, "operating_cashflow_change") if cash is not None else None
    if chg is not None and _attr(chg, "value") is not None:
        comps = list(_attr(chg, "components", ()) or ())
        wc = next((c for c in comps
                   if "working_capital" in str(_attr(c, "component_id"))), None)
        np_c = next((c for c in comps
                     if "net_profit" in str(_attr(c, "component_id"))), None)
        wc_v, np_v = (_num(_attr(wc, "value")) if wc is not None else None,
                      _num(_attr(np_c, "value")) if np_c is not None else None)
        total = _num(_attr(chg, "value"))
        if wc_v is not None and total not in (None, 0) and abs(wc_v) >= abs(total) * 0.5:
            share = abs(wc_v) / abs(total) * 100 if total else 0.0
            numbers = [f"经营现金流变化 {_yi(total)} 亿元：营运资本项 {_yi(wc_v)} 亿元"
                       f"（占 {share:.1f}%）、合并净利润项 {_yi(np_v)} 亿元"]
            evidence = [{"type": "运行读数", "text": "ΔOCF 分项（经营驱动/现金桥同一次运行）",
                         "locator": "底稿 `analysis/analysis_runs.json`", "kind": "observed"}]
            for r in _snippet_for(records, ("应付", "应收", "货款", "结算", "回款")):
                evidence.append({"type": "已准入段落", "text": str(r.get("snippet") or "")[:160],
                                 "locator": str(r.get("locator") or ""), "kind": "observed"})
            out.append(_judgment(
                jid=J_CASH_WORKING_CAPITAL,
                title="现金变化主要由**营运资本（占用与时点）**解释，其持续性是关键",                numbers=numbers, evidence=evidence,
                boundary=("营运资本项变化可能只是结算节奏、票据或时点；"
                          "**不直接证明**账期延长，也不等同于经营改善"),
                alternatives=["客户回款节奏与票据结算变化", "备货/采购节奏（时点）",
                              "收入规模变化带来的自然占用变化"],
                watch=["下一期经营/投资活动现金流与应收/应付/存货绝对额",
                       "票据、账龄与结算政策披露",
                       "采购付现是否反弹、销售收现是否继续改善"],
                gaps=["结算条款与账龄明细未取得时不推断账期"]))
    return out[:max(0, int(limit))]


def render_judgments(judgments, *, heading: str = "### 研究判断（可检验，先看这三条）") -> list[str]:
    """七段式排版：判断 → 数字与贡献 → 原句/原件位置 → 支持边界 → 替代解释 → 观察与反转 → 缺口。"""
    if not judgments:
        return []
    lines = [heading,
             "> 每条判断都由**已验证运行**与**已准入披露**驱动；"
             "「反转条件」写的是什么读数会削弱它——写不出反转条件的不是判断。"]
    for i, j in enumerate(judgments, 1):
        # 标题自带重点标记（`**…**`），这里不再套一层，避免出现 `****` 这种断掉的强调
        lines.append(f"{i}. {j.get('title')}（{j.get('kind_label')}）")
        for n in j.get("numbers") or ():
            lines.append(f"   - 数字与贡献：{n}")
        for e in (j.get("evidence") or ())[:2]:
            lines.append(f"   - {e.get('type')}：{str(e.get('text') or '')[:160]}")
            if e.get("locator"):
                lines.append(f"     - 位置：{e.get('locator')}")
        if j.get("boundary"):
            lines.append(f"   - 支持边界：{j['boundary']}")
        if j.get("alternatives"):
            lines.append("   - 本公司替代解释：" + "、".join(j["alternatives"]))
        if j.get("watch"):
            lines.append("   - 后续指标与反转条件：" + "；".join(j["watch"]))
        if j.get("gaps"):
            lines.append("   - 缺口（不臆造）：" + "；".join(j["gaps"]))
    lines.append("")
    return lines
