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

# X0（阶段X §3）：**证据角色**——每条证据标明它是"发行人怎么解释的"、"披露的读数"、
# "我们算出来的"还是"背景/未来计划"。此前一律标「已准入段落」，于是三一第 29 页的
# **未来计划**被印成"已发生（读数）"（架构复核点名）。角色由披露记录自己的 `kind` 与
# 小节/段首措辞判定，不由调用方指定。
ROLE_COMPANY_CLAIM = "company_claim"
ROLE_OBSERVED = "observed"
ROLE_READING = "reading"
ROLE_DERIVED = "derived"
ROLE_ASSUMPTION = "assumption"
ROLE_BACKGROUND = "background"
ROLE_LABELS = {
    ROLE_COMPANY_CLAIM: "发行人归因（原句）",
    ROLE_OBSERVED: "披露原句",
    ROLE_READING: "运行读数",
    ROLE_DERIVED: "派生算式（计算依据，非披露原句）",
    ROLE_ASSUMPTION: "未来计划/假设（不作已发生解释）",
    ROLE_BACKGROUND: "背景",
}
# 只有这两类角色能当**支持**（"解释该指标变化"的披露原句 / 该指标的披露读数）
SUPPORT_ROLES = (ROLE_COMPANY_CLAIM, ROLE_OBSERVED)
# 未来/计划措辞：出现在**小节名或段首**时降为假设（不是已发生的经营解释）
_PLAN_SECTION = ("经营计划", "未来发展", "发展展望", "公司未来", "研发投入",
                 "募集资金", "拟达到", "项目目的", "发展战略", "未来展望")
_PLAN_HEAD = ("拟", "计划", "预计", "未来", "将于", "拟达到", "项目目的", "发展战略",
              "拟建设", "拟投入")


def record_role(rec) -> str:
    """披露记录 → 证据角色（计划/背景**不升级**为已发生经营解释）。"""
    if not isinstance(rec, dict):
        return ROLE_OBSERVED
    kind = str(rec.get("kind") or "")
    section = str(rec.get("section") or "")
    leaf = section.split(">")[-1].strip()
    snip = str(rec.get("snippet") or "")
    if any(t in leaf for t in _PLAN_SECTION) or any(t in snip[:40] for t in _PLAN_HEAD):
        return ROLE_ASSUMPTION
    if kind == "change_explanation":
        return ROLE_COMPANY_CLAIM
    if kind in ("business_background", "risk"):
        return ROLE_BACKGROUND
    return ROLE_OBSERVED


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


def _yi(value, unit: str = "元") -> str:
    """金额 → 亿元读数（按**单位**换算；默认元——单位已声明亿元时不再除 1e8）。"""
    n = _num(value)
    if n is None:
        return "—"
    yi = n * _unit_scale(unit) / 1e8
    return f"{yi:+,.2f}".replace("+-", "-")


def _unit_scale(unit) -> float:
    """金额单位 → 元倍数；不是金额单位（%/吨/空）按元（与 `charts.unit_to_yuan` 同表）。"""
    u = str(unit or "")
    if "%" in u or "％" in u or "/" in u:
        return 1.0
    for key, s in (("万亿", 1e12), ("千亿", 1e11), ("百亿", 1e10), ("亿", 1e8), ("万", 1e4)):
        if key in u:
            return s
    return 1.0


def _unit_of(run) -> str:
    """运行的金额单位（取第一个金额输出的 `unit`；取不到按元）——同一次运行同一量纲。"""
    for o in (_attr(run, "outputs", ()) or ()):
        unit = str(_attr(o, "unit") or "")
        if unit and "%" not in unit:
            return unit
    return "元"


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


def _pick_records(records, terms, limit: int = 1, roles=SUPPORT_ROLES):
    """已准入段落里与这些词相关的原句 → `(采用, 被降级的角色)`。

    打分（严格：小节**叶名** +3／段首 +2／段内 +1，<2 不绑）：小节名只认**叶子**
    （`第三节 管理层讨论与分析 > 四、主营业务分析 > 4、研发投入` 的叶子是"研发投入"）——
    此前拿整条路径打分，凡是"主营业务分析"下的段落都加 3 分，于是**研发微生态**
    被绑成"产品/区域结构"的支持（架构复核点名）。

    角色过滤（X0）：只有 `roles` 里的角色能当支持；背景/未来计划被排除并**计数返回**，
    调用方据此如实写"候选段落属背景/计划"，而不是把它印成已发生解释。
    """
    hits = []
    skipped: list[str] = []
    for r in (records or ()):
        if not isinstance(r, dict):
            continue
        role = record_role(r)
        leaf = str(r.get("section") or "").split(">")[-1].strip()
        snip = str(r.get("snippet") or "")
        head = snip[:60]
        score = 0
        for t in terms:
            if not t:
                continue
            if t in leaf:
                score += 3
            if t in head:
                score += 2
            elif t in snip:
                score += 1
        if score < 2:
            continue
        if role not in roles:
            if role not in skipped:
                skipped.append(role)
            continue
        hits.append((score, r, role))
    hits.sort(key=lambda x: -x[0])
    return [{"record": r, "role": role} for _s, r, role in hits[:limit]], skipped


def _snippet_for(records, terms, limit: int = 1) -> list[dict]:
    """兼容入口：只取**能当支持**的段落（角色过滤见 `_pick_records`）。"""
    kept, _skipped = _pick_records(records, terms, limit=limit)
    return [k["record"] for k in kept]


def _record_evidence(records, terms, *, limit: int = 1) -> tuple[list[dict], list[str]]:
    """支持证据（带角色）＋"被降级角色"清单（背景/未来计划不作为经营解释）。"""
    kept, skipped = _pick_records(records, terms, limit=limit)
    ev: list[dict] = []
    for k in kept:
        r, role = k["record"], k["role"]
        ev.append({"type": ROLE_LABELS.get(role, "披露原句"),
                   "text": str(r.get("snippet") or "")[:160],
                   "locator": str(r.get("locator") or ""), "kind": role,
                   "role": role})
    return ev, skipped


def _entity_of(runs) -> str:
    """选定运行里的**主体名**（用于公司边界：本公司缺口不串到别家公司）。"""
    for r in (runs or ()):
        for o in (_attr(r, "outputs", ()) or ()):
            entity = str(_attr(o, "entity") or _attr(o, "entity_id") or "")
            if entity:
                return entity
    return ""


def _facts_evidence(vp, *, groups=("分产品", "分地区")) -> list[dict]:
    """量价/结构事实 → 披露原句证据（每个分组各一条；带角色）。"""
    ev: list[dict] = []
    for group in groups:
        for f in _facts_of(vp, group)[:1]:
            if f.get("locator"):
                ev.append({"type": "披露原句", "text": str(f.get("line") or ""),
                           "locator": str(f.get("locator") or ""),
                           "kind": ROLE_OBSERVED, "role": ROLE_OBSERVED})
    return ev


def _judgment(*, jid, title, numbers, evidence, boundary, alternatives, watch,
              kind="observed", gaps=(), roles=()) -> dict:
    return {"judgment_id": jid, "title": title, "kind": kind,
            "kind_label": KIND_LABELS.get(kind, kind),
            "numbers": list(numbers), "evidence": list(evidence),
            "boundary": str(boundary), "alternatives": list(alternatives),
            "watch": list(watch), "gaps": list(gaps),
            # 贡献方向（X0）：{"item": 名称, "value": 亿元, "role": 推动/拖累/缓冲/主导}
            "roles": [dict(r) for r in roles]}


def cash_direction_roles(total, np_v, wc_v, *, np_label="合并净利润项",
                         wc_label="营运资本项", rest_v=None,
                         rest_label="其他调节项", unit: str = "元") -> tuple[str, list[dict], str]:
    """现金桥按**方向**说贡献（X0，架构复核点名）：返回 `(主导项, 角色表, 方向句)`。

    纪律：不以"绝对占比"代替方向。`total`（ΔOCF）为负时，同号项是**拖累/主导**、
    反号项是**缓冲**；`total` 为正时，同号项是**推动**、反号项是**拖累**。
    洋河实测：ΔOCF −15.02、净利项 −33.54（主导下降）、营运资本项 +11.04（缓冲）；
    三一实测：ΔOCF +91.06、营运资本项 +79.77（主要构成）。
    """
    total = None if total is None else float(total)
    rows: list[dict] = []
    for label, value in ((np_label, np_v), (wc_label, wc_v), (rest_label, rest_v)):
        if value is None:
            continue
        rows.append({"item": label, "value": float(value),
                     "same_direction": bool(total is not None and total != 0
                                            and float(value) * total > 0)})
    if total is None or total == 0 or not rows:
        return "", rows, ""
    down = total < 0
    for r in rows:
        if r["same_direction"]:
            r["role"] = "拖累（主导下降）" if down else "推动"
        else:
            r["role"] = "缓冲" if down else "拖累"
    same = [r for r in rows if r["same_direction"]]
    driver = max(same, key=lambda r: abs(r["value"])) if same else None
    driver_key = ""
    if driver is not None:
        driver_key = ("net_profit" if driver["item"] == np_label
                      else "working_capital" if driver["item"] == wc_label else "other")
    bits = []
    for r in rows:
        if r is driver or r["value"] == 0:
            continue
        if r["role"] == "缓冲":
            bits.append(f"{r['item']} {_yi(r['value'], unit)} 亿元是**缓冲**"
                        f"（抵消降幅的 {abs(r['value'] / total) * 100:.1f}%）")
        elif r["role"] == "拖累（主导下降）":
            bits.append(f"{r['item']} {_yi(r['value'], unit)} 亿元**加重下降**")
        elif r["role"] == "拖累":
            bits.append(f"{r['item']} {_yi(r['value'], unit)} 亿元是**拖累**")
        else:
            bits.append(f"{r['item']} {_yi(r['value'], unit)} 亿元**同向推动**")
    lead = ""
    if driver is not None:
        what = "下降" if down else "改善"
        if driver_key == "net_profit":
            tail = "由利润下降主导" if down else "由利润增长推动"
        elif driver_key == "working_capital":
            tail = "主要由营运资本（占用与时点）构成"
        else:
            tail = f"主要由{driver['item']}构成"
        lead = f"现金{what}**{tail}**（{driver['item']} {_yi(driver['value'], unit)} 亿元）"
    direction = lead + ("；" + "；".join(bits) if bits else "")
    return driver_key, rows, direction


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
            row[f"{tag}_unit"] = str(getattr(obs, "unit", "") or "")
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
    entity = _entity_of(runs)
    # 本公司专属缺口（只对**它自己**列）：洋河第 10 页的"中高档酒/普通酒"产品类别表是
    # 单期+同比形状、现抽取层未取到。这条缺口此前写死在结构判断里，于是**三一**的
    # 结构判断也挂着洋河的酒类缺口（架构复核点名"证据角色和公司边界混用"）。
    _is_yanghe = ("洋河" in entity) or ("002304" in entity)

    vpe = _out(od, "volume_price_decomposition") if od is not None else None
    if vpe is None and od is not None:
        vpe = next((o for o in (_attr(od, "outputs", ()) or ())
                    if "volume_price" in str(_attr(o, "metric"))), None)
    vol_eff = price_eff = None
    _ou = _unit_of(od)                    # 经营驱动运行的金额单位（元/亿元…）
    _cu = _unit_of(cash)                  # 现金桥运行的金额单位
    _vpu = str(_attr(vpe, "unit") or "") or _ou
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
        numbers = [f"量价分解：销量效应 {_yi(vol_eff, _vpu)} 亿元"
                   + (f"、单位价格效应 {_yi(price_eff, _vpu)} 亿元（含结构混合）"
                      if price_eff is not None else "")]
        if sales:
            numbers.append(f"披露销售量 {_num(sales.get('cur')):,.2f} 吨，"
                           f"同比 {_pct(sales.get('yoy'))}"
                           + (f"（{_num(sales.get('prev')):,.2f} 吨）"
                              if _num(sales.get("prev")) is not None else ""))
        evidence = []
        gaps_v: list[str] = []
        if sales.get("locator"):
            evidence.append({"type": "披露原句", "text": str(sales.get("line") or ""),
                             "locator": str(sales.get("locator") or ""),
                             "kind": ROLE_OBSERVED, "role": ROLE_OBSERVED})
        _rec_ev, _skipped = _record_evidence(records, ("销售量", "销量", "产销量"))
        evidence.extend(_rec_ev)
        if _skipped:
            gaps_v.append("相关但属%s的段落不作为已发生解释"
                          % "、".join(ROLE_LABELS.get(s, s) for s in _skipped))
        out.append(_judgment(
            jid=J_VOLUME, title="销量收缩是收入下降的重要「数量」观察（不是需求结论）",
            numbers=numbers, evidence=evidence,
            boundary=("销量与收入都来自发行人披露，只能说明**数量在收缩**；"
                      "需求量、渠道库存与终端动销不在本次材料里，不能由销量推断"),
            alternatives=["发货与确认节奏调整（时点）", "产品结构与统计口径变化",
                          "公司主动控货/去库存"],
            watch=["下一期同口径销售量（与生产量对照）",
                   "收入降幅是否小于销量降幅（结构/价格是否在托底）"],
            gaps=["渠道库存与终端动销材料未取得（机制缺口，不臆造）"] + gaps_v))
    elif vp_ok or vpe is not None:
        out.append(_judgment(
            jid=J_VOLUME, title="销量与收入的数量关系：本次不足以下判断",
            numbers=[("量价分解未取到销量/单位收入效应"
                      if vol_eff is None else f"销量效应 {_yi(vol_eff, _vpu)} 亿元不为负")],
            evidence=[], boundary="读数不足：不生成「销量收缩」结论，先补量价与产销量表",
            alternatives=[], watch=["补齐分产品产销量与收入构成后重算"],
            gaps=["量价分解或产销量披露缺失"]))

    # ② 价：单位收入上升 ≠ 提价能力
    if price_eff is not None and price_eff > 0:
        numbers = [f"单位价格效应 {_yi(price_eff, _vpu)} 亿元（量价分解，含产品结构混合）"]
        if ton_price:
            numbers.append(f"推算吨价 {_num(ton_price.get('cur')):,.0f} 元/吨，"
                           f"同比 {_pct(ton_price.get('yoy'))}"
                           "（发行人未直接披露吨价，为推算口径）")
        evidence = ([{"type": ROLE_LABELS[ROLE_DERIVED],
                      "text": f"吨价（推算）＝ {str(ton_price.get('formula') or '')}",
                      "locator": "收入构成与产销量表（**推算口径**，不是发行人披露价格）",
                      "kind": ROLE_DERIVED, "role": ROLE_DERIVED}]
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
                     "locator": str(inv.get("locator") or ""),
                     "kind": ROLE_OBSERVED, "role": ROLE_OBSERVED}]
        _rec_ev, _skipped = _record_evidence(records, ("库存", "存货", "产销"))
        evidence.extend(_rec_ev)
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
        rest_c = next((c for c in comps
                       if str(_attr(c, "component_id")) not in
                       ("change_in_net_profit", "change_in_working_capital")), None)
        rest_v = _num(_attr(rest_c, "value")) if rest_c is not None else None
        if np_v is not None or wc_v is not None:
            # X0：**方向**决定措辞（不以绝对占比代替方向）——先算主导项/缓冲项，再写标题。
            driver, roles, direction = cash_direction_roles(total, np_v, wc_v,
                                                            rest_v=rest_v, unit=_cu)
            comp_txt = "；".join(
                f"{r['item']} {_yi(r['value'], _cu)} 亿元（{r['role']}）" for r in roles)
            numbers = [
                f"经营现金流变化（**间接法**：净利润＋调节项，独立对账）{_yi(total, _cu)} 亿元"
                f"：{comp_txt}",
                f"方向：{direction}",
            ]
            evidence = [{"type": "运行读数", "text": "ΔOCF 分项（经营驱动/现金桥同一次运行）",
                         "locator": "底稿 `analysis/analysis_runs.json`",
                         "kind": ROLE_READING, "role": ROLE_READING}]
            _rec_ev, _skipped = _record_evidence(
                records, ("应付", "应收", "货款", "结算", "回款", "采购"), limit=2)
            evidence.extend(_rec_ev)
            if driver == "working_capital":
                title = ("现金变化**主要由营运资本（占用与时点）构成**，"
                         "其持续性是关键")
                gaps = ["结算条款与账龄明细未取得时不推断账期"]
            elif driver == "net_profit":
                title = ("现金变化**由利润变化主导**，营运资本是缓冲/拖累"
                         "——缓冲能不能持续才是关键")
                gaps = ["结算条款与账龄明细未取得时不推断账期"]
            else:
                title = "现金变化的构成：以营运资本与净利润项之外的部分为主"
                gaps = ["结算条款与账龄明细未取得时不推断账期"]
            if _skipped:
                gaps.append("相关但属%s的段落不作为已发生解释"
                            % "、".join(ROLE_LABELS.get(s, s) for s in _skipped))
            out.append(_judgment(
                jid=J_CASH_WORKING_CAPITAL, title=title,
                numbers=numbers, evidence=evidence, roles=roles,
                boundary=("营运资本项变化可能只是结算节奏、票据或时点；"
                          "**不直接证明**账期延长，也不等同于经营改善；"
                          "**方向**说明谁在推动、谁在缓冲，占比只是量级参考"),
                alternatives=["客户回款节奏与票据结算变化", "备货/采购节奏（时点）",
                              "收入规模变化带来的自然占用变化"],
                watch=["下一期经营/投资活动现金流与应收/应付/存货绝对额",
                       "票据、账龄与结算政策披露",
                       "采购付现是否反弹、销售收现是否继续改善"],
                gaps=gaps))
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
                    f"{name} {_yi(val, _ou)} 亿元" for name, val in parts))
            struct_lines = []
            for f in _facts_of(vp, "分产品")[:3]:
                struct_lines.append(f"分产品收入：{f.get('row_label')} "
                                    f"{_pct(f.get('yoy'))}")
            for f in _facts_of(vp, "分地区")[:3]:
                struct_lines.append(f"分地区收入：{f.get('row_label')} "
                                    f"{_pct(f.get('yoy'))}")
            numbers += struct_lines
            if numbers:
                evidence = _facts_evidence(vp)
                _rec_ev, _skipped = _record_evidence(
                    records, ("分产品", "分地区", "分销售模式", "收入构成"), limit=1)
                evidence.extend(_rec_ev)
                _gaps = ["渠道库存与终端动销未取得时不判断「结构性需求」"]
                if _skipped:
                    _gaps.append("相关但属%s的段落不作为已发生解释"
                                 "（研发/未来计划段落不能当结构支持）"
                                 % "、".join(ROLE_LABELS.get(s, s) for s in _skipped))
                if _is_yanghe:
                    _gaps.append(
                        "另有按出厂价分的中高档酒/普通酒「产品类别」表（第 10 页，"
                        "架构文件给出 −14.79%／−0.49%）：该表是**单期+同比**形状，"
                        "本次抽取层未取到 → 只作缺口列出，不并入上面的切法")
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
                    gaps=_gaps))

    # ⑤ 直接法收支：现金跃升**有没有真实收支支撑**、主要是不是利润带来的（三一式）
    rec, paid = (direct_cash or {}).get("received") or {}, (direct_cash or {}).get("paid") or {}
    if (chg is not None and rec.get("delta") is not None and paid.get("delta") is not None
            and total is not None and total > 0):
        rec_d, paid_d = float(rec["delta"]), float(paid["delta"])
        # 直接法两行的**现金净贡献** = 收现变化 − 付现变化（付现减少即正贡献）
        net_support = rec_d - paid_d
        # 直接法的"其余经营活动收支" = ΔOCF − 两行净贡献（三一实测 −27.06 亿元）。
        # X0（架构复核点名）：它**不是**利润口径的项，更不是"净利 +14.86 亿元的其中"——
        # 净利项属于**间接法**桥，两种切法分开讲、不相加、不嵌套。
        other_total = total - net_support
        wc_rest = None
        if np_v is not None or wc_v is not None:
            wc_rest = total - (np_v or 0.0) - (wc_v or 0.0)
        # 直接法两行来自**数据集观察**，它自己的单位才是这份读数的单位（可能是元或亿元）
        _du = str(rec.get("cur_unit") or paid.get("cur_unit") or _cu or "元")
        numbers = [
            f"**直接法**（收付实现）：销售收现变化 {_yi(rec_d, _du)} － 采购付现变化 "
            f"{_yi(paid_d, _du)} ＝ 两行净贡献 {_yi(net_support, _du)} 亿元；其余经营活动收支 "
            f"{_yi(other_total, _cu)} 亿元（＝ΔOCF −两行净贡献，含税费/薪酬/其他经营收支，"
            "**不含**利润口径的净利项）",
            f"**间接法**（净利润＋调节项，独立对账、**不与直接法相加**）：ΔOCF "
            f"{_yi(total, _cu)} 亿元 ＝ 合并净利润项 {_yi(np_v, _cu)} 亿元 ＋ 营运资本项 "
            f"{_yi(wc_v, _cu)} 亿元"
            + (f" ＋ 其他调节项 {_yi(wc_rest, _cu)} 亿元" if wc_rest is not None else "")
            + "；现金跃升主要不是利润带来的",
            f"两行水平：销售收现 {rec.get('prev_period','')} {_yi(rec.get('prev'), _du)} → "
            f"{rec.get('cur_period','')} {_yi(rec.get('cur'), _du)} 亿元；"
            f"采购付现 {paid.get('prev_period','')} {_yi(paid.get('prev'), _du)} → "
            f"{paid.get('cur_period','')} {_yi(paid.get('cur'), _du)} 亿元"
            "（付现减少＝现金正贡献；这是收付实现口径，不是利润口径）"]
        evidence = [{"type": "披露原句", "text": "合并现金流量表·销售商品、提供劳务收到的现金",
                     "locator": str(rec.get("locator") or ""),
                     "kind": ROLE_OBSERVED, "role": ROLE_OBSERVED},
                    {"type": "披露原句", "text": "合并现金流量表·购买商品、接受劳务支付的现金",
                     "locator": str(paid.get("locator") or ""),
                     "kind": ROLE_OBSERVED, "role": ROLE_OBSERVED}]
        _rec_ev, _skipped = _record_evidence(
            records, ("回款", "采购", "结算", "应付", "收现",
                      # 三一的原句在「…5、现金流量表…」小节里：
                      # 「主要系本期销售回款增加、采购付款减少影响。」（第 19 页）
                      # 小节名带"现金"、段内含回款/采购 → 算作**发行人归因**，位置照抄。
                      "现金流量", "现金"), limit=4)
        # 先给**段内真的写到回款/采购**的那一条（否则会被"科目变动分析表"顶掉）
        _rec_ev.sort(key=lambda e: 0 if any(
            t in str(e.get("text") or "") for t in ("回款", "采购")) else 1)
        evidence.extend(_rec_ev[:2])
        gaps = []
        if not _rec_ev:
            gaps.append("公司对回款/采购变化的披露原句未准入：只有两行读数与调节项，"
                        "不代拟原因")
        if _skipped:
            gaps.append("相关但属%s的段落不作为已发生解释"
                        % "、".join(ROLE_LABELS.get(s, s) for s in _skipped))
        out.append(_judgment(
            jid=J_CASH_DIRECT_SUPPORT,
            title="现金跃升有**真实收支**支撑，但主要不来自利润增长",
            numbers=numbers, evidence=evidence,
            boundary=("直接法（收付实现：收现/付现）与间接法（净利润＋调节项）是同一变化的"
                      "**不同切法**，**不可相加**：直接法的「其余收支」**不是**净利项的分项，"
                      "间接法的净利项另列；应付/采购变化也不直接证明账期延长"),
            alternatives=["采购与备货节奏变化（时点）", "票据与结算政策变化",
                          "收入确认与回款节奏"],
            watch=["下一期采购付现是否反弹（反弹**且**销售收现增量不足抵消，"
                   "才削弱现金改善的持续性；单独反弹不足以作必然结论）",
                   "销售收现是否继续改善",
                   "应付调节项是否反转、票据/账龄/采购规模披露"],
            gaps=gaps))
    return out[:max(0, int(limit))]


def render_judgments(judgments, *, heading: str = "### 研究判断（可检验）",
                     evidence_limit: int = 4) -> list[str]:
    """七段式排版：判断 → 数字与贡献 → 原句/原件位置 → 支持边界 → 替代解释 → 观察与反转 → 缺口。

    X0：证据按**角色**排序后再展示（发行人归因/披露原句在前，运行读数在后），
    最多 `evidence_limit` 条——此前只印前两条，把公司在第 19 页的归因（第 3 条）
    藏掉了（架构复核点名）。
    """
    if not judgments:
        return []
    lines = [heading,
             "> 每条判断都由**已验证运行**与**已准入披露**驱动；"
             "「反转条件」写的是什么读数会削弱它——写不出反转条件的不是判断。"]
    _prio = {ROLE_COMPANY_CLAIM: 0, ROLE_OBSERVED: 1, ROLE_READING: 2,
             ROLE_DERIVED: 3, ROLE_ASSUMPTION: 4, ROLE_BACKGROUND: 5}
    for i, j in enumerate(judgments, 1):
        # 标题自带重点标记（`**…**`），这里不再套一层，避免出现 `****` 这种断掉的强调
        lines.append(f"{i}. {j.get('title')}（{j.get('kind_label')}）")
        for n in j.get("numbers") or ():
            lines.append(f"   - 数字与贡献：{n}")
        ev = sorted((e for e in (j.get("evidence") or ()) if isinstance(e, dict)),
                    key=lambda e: _prio.get(str(e.get("role") or ""), 9))
        for e in ev[:max(1, int(evidence_limit))]:
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


def render_judgment_summary(judgments, *, limit: int = 3,
                            heading: str = "## 三项主判断（先看这里）") -> list[str]:
    """**前两页**的三项主判断（X0）：一条判断＝判断句＋关键读数＋依据位置＋反转条件。

    选条规则：优先**有实质读数**的判断（三一没有量价时，前两条会是"不足以下判断"的
    空位判断——首屏把三个位置占满空位，读者就看不到现金与收支的主线）；空位判断只在
    实质判断不足三条时补位。完整七段式（全部原句、位置、替代解释与缺口）另附
    `analysis/analysis_detail.md`——主文先让读者在一分钟内看到"经营故事怎样了、
    什么会推翻它"。
    """
    if not judgments:
        return []
    items = list(judgments)
    _empty = ("不足以下判断", "无从判断")
    picked = [j for j in items if not any(t in str(j.get("title") or "") for t in _empty)]
    for j in items:
        if len(picked) >= limit:
            break
        if j not in picked:
            picked.append(j)
    lines = [heading, "",
             "> 以下三条由**已验证运行**与**已准入披露**驱动；每条都写明了"
             "「什么读数会削弱它」。完整七段式（原句/位置/替代解释/缺口）见"
             "`analysis/analysis_detail.md`。", ""]
    for i, j in enumerate(picked[:max(1, int(limit))], 1):
        lines.append(f"{i}. {j.get('title')}（{j.get('kind_label')}）")
        for n in (j.get("numbers") or ())[:2]:
            lines.append(f"   - 关键读数：{n}")
        ev = sorted((e for e in (j.get("evidence") or ()) if isinstance(e, dict)),
                    key=lambda e: 0 if str(e.get("role") or "") in
                    (ROLE_COMPANY_CLAIM, ROLE_OBSERVED, ROLE_DERIVED) else 1)
        for e in ev[:1]:
            if e.get("locator"):
                lines.append(f"   - 依据：{e.get('type')}·{e.get('locator')}")
            elif e.get("text"):
                lines.append(f"   - 依据：{e.get('type')}·{str(e.get('text'))[:80]}")
        if j.get("watch"):
            lines.append(f"   - 会削弱它的读数：{j['watch'][0]}")
    lines.append("")
    return lines
