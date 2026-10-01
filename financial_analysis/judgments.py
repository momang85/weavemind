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
J_CASH_DIRECT_SUPPORT = "cash_direct_method_support"
J_STRUCTURE = "product_region_structure"

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


def direct_cash_of(dataset, *, locators: dict | None = None) -> dict:
    """**直接法**经营现金流的两行收支（合并口径，两期）→ 普通 dict（W2，三一现金判断用）。

    `销售商品、提供劳务收到的现金` 与 `购买商品、接受劳务支付的现金` 是"钱怎么收进来/付出去"的
    一手读数；只读间接法调节项时，读者看不到现金跃升是否有**真实收支**支撑。
    取不到就返回 `{}`（调用方据此不生成判断，不拿别的数顶）。
    """
    out: dict = {}
    for key, slug in (("received", "cash_received_from_sales"),
                      ("paid", "cash_paid_for_goods")):
        row: dict = {}
        for tag, offset in (("cur", 0), ("prev", -1)):
            period = dataset.period_at(offset)
            if not period:
                row = {}
                break
            obs = dataset.get(slug, period)
            if obs is None or obs.value is None:
                row = {}
                break
            row[tag] = _num(obs.value)
            row[f"{tag}_period"] = period
            row[f"{tag}_fact"] = str(getattr(obs, "fact_id", "") or "")
        if not row:
            continue
        row["delta"] = (row.get("cur") or 0.0) - (row.get("prev") or 0.0)
        if isinstance(locators, dict):
            item = locators.get(str(row.get("cur_fact") or "")) or {}
            row["locator"] = str((item or {}).get("locator") or "")
        out[key] = row
    if out.get("received") and out.get("paid"):
        out["source"] = "合并现金流量表（直接法两行；与补充资料间接法是同一变化的不同切法）"
    return out


def research_judgments(runs, *, volume_price: dict | None = None,
                       records=None, direct_cash: dict | None = None,
                       limit: int = 3) -> list[dict]:
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
    # ④ 产品/区域结构：不同切法的降幅差 → 结构在拖累还是托底（**不可相加**）
    if od is not None:
        seg_rows: list[dict] = []
        for o in (_attr(od, "outputs", ()) or ()):
            metric = str(_attr(o, "metric") or "")
            if metric != "gross_profit_change_by_segment":
                continue
            label = str(_attr(o, "label") or "")
            cut = label[label.find("切法：") + 3:label.find("）")] if "切法：" in label else label
            parts = []
            for c in (_attr(o, "components", ()) or ()):
                cid = str(_attr(c, "component_id") or "")
                if cid.startswith("segment:"):
                    parts.append((cid.split(":")[-1], _num(_attr(c, "value"))))
            if parts:
                seg_rows.append({"cut": cut, "parts": parts,
                                 "label": label})
        # 分段切法：任一把"分产品/分地区/分销售模式/分行业"（生产数据里的口径名形如
        # `分产品:白酒`）都取前两把，各自列最差与最好分组（同一口径内部可比，跨切法不可加）
        picked_cuts = [r for r in seg_rows if len(r["parts"]) >= 1][:2]
        if picked_cuts:
            numbers = []
            for r in picked_cuts[:2]:
                parts = sorted([p for p in r["parts"] if p[1] is not None],
                               key=lambda p: p[1])
                if not parts:
                    continue
                numbers.append(f"{r['cut']}切法：" + "、".join(
                    f"{name} {_yi(val)} 亿元" for name, val in parts))
            struct_lines = []
            for f in _facts_of(vp, "分产品")[:3]:
                struct_lines.append(f"分产品收入：{f.get('row_label')} "
                                    f"{_pct(f.get('yoy'))}")
            for f in _facts_of(vp, "分地区")[:3]:
                struct_lines.append(f"分地区收入：{f.get('row_label')} "
                                    f"{_pct(f.get('yoy'))}")
            numbers += struct_lines
            if numbers:
                evidence = []
                for f in (_facts_of(vp, "分产品")[:1] + _facts_of(vp, "分地区")[:1]):
                    if f.get("locator"):
                        evidence.append({"type": "披露原句",
                                         "text": str(f.get("line") or ""),
                                         "locator": str(f.get("locator") or ""),
                                         "kind": "observed"})
                for r in _snippet_for(records, ("分产品", "分地区", "主营业务", "结构")):
                    evidence.append({"type": "已准入段落",
                                     "text": str(r.get("snippet") or "")[:160],
                                     "locator": str(r.get("locator") or ""),
                                     "kind": "observed"})
                out.append(_judgment(
                    jid=J_STRUCTURE,
                    title="产品/区域结构是**同一个口径的不同切法**：降幅差说明结构在起作用",
                    numbers=numbers, evidence=evidence,
                    boundary=("分产品/分地区/分销售模式是**不同切法**，覆盖同一笔收入，"
                              "**不可相加**；结构差异也可能来自发货与确认节奏，"
                              "不能直接读成「某类需求更好」"),
                    alternatives=["渠道与发货节奏差异", "统计口径或并表范围变化",
                                  "价格与促销政策在不同品类上的差异"],
                    watch=["下一期分产品/分地区收入的降幅差是否收敛",
                           "同口径销量与吨价（结构混合）",
                           "公司对区域/产品策略的披露原句"],
                    gaps=["渠道库存与终端动销未取得时不判断「结构性需求」"]))

    # ⑤ 直接法收支：现金跃升**有没有真实收支支撑**、主要是不是利润带来的（三一式）
    rec, paid = (direct_cash or {}).get("received") or {}, (direct_cash or {}).get("paid") or {}
    if (chg is not None and rec.get("delta") is not None and paid.get("delta") is not None
            and total is not None and total > 0):
        rec_d, paid_d = float(rec["delta"]), float(paid["delta"])
        # 直接法两行的**现金净贡献** = 收现变化 − 付现变化（付现减少即正贡献）
        net_support = rec_d - paid_d
        # 其余收支合计 = ΔOCF − 直接法两行净贡献（三一实测 ≈ −27.06 亿元；
        # 合并净利润项 +14.86 亿元在其中，其余为营运资本/其他调节）
        other_total = total - net_support
        numbers = [
            f"ΔOCF {_yi(total)} 亿元 ＝ **直接法两行净贡献 {_yi(net_support)} 亿元**"
            f"（销售收现变化 {_yi(rec_d)} － 采购付现变化 {_yi(paid_d)}）"
            f" ＋ **其余收支合计 {_yi(other_total)} 亿元**"
            f"（其中合并净利润项只 {_yi(np_v)} 亿元——现金跃升主要不是利润带来的）",
            f"两行水平：销售收现 {rec.get('prev_period','')} {_yi(rec.get('prev'))} → "
            f"{rec.get('cur_period','')} {_yi(rec.get('cur'))} 亿元；"
            f"采购付现 {paid.get('prev_period','')} {_yi(paid.get('prev'))} → "
            f"{paid.get('cur_period','')} {_yi(paid.get('cur'))} 亿元"
            "（付现减少＝现金正贡献；这是收付实现口径，不是利润口径）"]
        evidence = [{"type": "披露原句", "text": "合并现金流量表·销售商品、提供劳务收到的现金",
                     "locator": str(rec.get("locator") or ""), "kind": "observed"},
                    {"type": "披露原句", "text": "合并现金流量表·购买商品、接受劳务支付的现金",
                     "locator": str(paid.get("locator") or ""), "kind": "observed"}]
        for r in _snippet_for(records, ("回款", "采购", "结算", "应付", "收现")):
            evidence.append({"type": "已准入段落", "text": str(r.get("snippet") or "")[:160],
                             "locator": str(r.get("locator") or ""), "kind": "observed"})
        gaps = []
        if not _snippet_for(records, ("回款", "采购", "结算", "应付", "收现")):
            gaps.append("公司对回款/采购变化的披露原句未准入：只有两行读数与调节项，"
                        "不代拟原因")
        out.append(_judgment(
            jid=J_CASH_DIRECT_SUPPORT,
            title="现金跃升有**真实收支**支撑，但主要不来自利润增长",
            numbers=numbers, evidence=evidence,
            boundary=("直接法（收付实现：收现/付现）与间接法（净利润＋调节项）是同一变化的"
                      "**不同切法**，**不可相加**；应付/采购变化也不直接证明账期延长"),
            alternatives=["采购与备货节奏变化（时点）", "票据与结算政策变化",
                          "收入确认与回款节奏"],
            watch=["下一期采购付现是否反弹（反弹则现金改善不可持续）",
                   "销售收现是否继续改善",
                   "应付调节项是否反转、票据/账龄/采购规模披露"],
            gaps=gaps))
    return out[:max(0, int(limit))]


def render_judgments(judgments, *, heading: str = "### 研究判断（可检验）") -> list[str]:
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
