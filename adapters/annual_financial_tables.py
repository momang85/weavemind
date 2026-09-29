# -*- coding: utf-8 -*-
"""年报 PDF 正文里的**财务报表行** → 候选观察（语义准入 A0 修正版）。

设计原则（09-29 架构纠偏 A0）：**候选观察必须带证据**，拿不到证据就拒绝。
上一版把"能抽出数"当成功，结果制造了四类真实污染：

1. **跨口径补齐**：2020 应收账款取母公司 18,600,000 却写"合并"（2019 是合并 1,966,154,875.23）；
2. **表性质丢失**：2020 存货 680,900,264.45 取自 2020-01-01 **会计政策调整表**，被当成 2020 年末；
3. **前缀误吞**：`营业收入扣除金额/扣除后金额` 被当前缀"营业收入"，与真实收入冲突后整项删除；
4. **补造元数据**：单位默认"元"、币种硬写 CNY、口径硬写"合并"、`fact_id` 全空。

现在每一条候选观察都必须能回答：**哪张表（表名/表性质）、哪一列（期间）、什么单位（表头标注）、
什么币种（单位证据）、什么口径（表名里的合并/母公司）**；任一项拿不出证据 → 该行拒绝，
拒绝原因带**源定位**（PDF 页码 + 表名 + 原标签 + 原单元格文本）。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# ── 指标词表：**完整标签**匹配（前缀相同但语义不同的行必须排除）────────────────
LABELS: dict[str, tuple[str, ...]] = {
    "revenue": ("营业收入", "营业总收入"),
    "operating_cost": ("营业成本",),
    "net_profit": ("归属于上市公司股东的净利润", "归属于母公司所有者的净利润",
                   "归属于母公司股东净利润", "归母净利润"),
    "operating_cashflow": ("经营活动产生的现金流量净额", "经营活动现金流量净额"),
    "total_assets": ("资产总计", "总资产"),
    "total_liabilities": ("负债合计", "总负债"),
    "accounts_receivable": ("应收账款",),
    "inventory": ("存货",),
    "accounts_payable": ("应付账款",),
}
# 标签后缀出现这些词 → 不是我们要的那个指标（扣除项/占比/账龄/减值/其中…）
LABEL_REJECT_WORDS = (
    "扣除", "扣除后", "占比", "比例", "账龄", "坏账", "跌价", "减值", "其中", "明细",
    "前五名", "合计的", "账面价值", "账面余额", "周转", "天数", "变动", "增减", "同比",
    "本期增加", "本期减少", "期初", "分季度", "第一季度", "第二季度",
    "第三季度", "第四季度", "上年同期", "行业", "地区", "产品", "客户", "供应商",
)
# 可以出现在标签后缀里的中性内容（单位/括号/冒号/空白）
_LABEL_TAIL_OK = re.compile(r"^[\s:：()（）\[\]【】、,，.。]*(?:元|万元|千元|百万元|美元|港元|"
                            r"人民币|股|/股|附注|注|注释)?[\s:：()（）\[\]【】、,，.。]*$")

# ── 表名 → (表性质, 口径) ──────────────────────────────────────────────────
STATEMENT_TITLES: dict[str, tuple[str, str]] = {
    "主要会计数据和财务指标": ("主要会计数据", "合并"),
    "主要会计数据": ("主要会计数据", "合并"),
    "合并资产负债表": ("合并资产负债表", "合并"),
    "合并利润表": ("合并利润表", "合并"),
    "合并现金流量表": ("合并现金流量表", "合并"),
    "母公司资产负债表": ("母公司资产负债表", "母公司"),
    "母公司利润表": ("母公司利润表", "母公司"),
    "母公司现金流量表": ("母公司现金流量表", "母公司"),
}
_NUM_TITLE_RE = re.compile(r"^\d+[、.．]\s*\S+")
_TITLE_RE = re.compile(r"^(\d+)[、.．]\s*([^\s，。]{2,20}(?:资产负债表|利润表|现金流量表))")
# 明确**不是**报表的表（政策调整/追溯调整/分部/季度摘要），行宁愿不取
NOT_A_STATEMENT_RE = re.compile(r"会计政策(变更|调整)|追溯调整|前期差错|分部|季度|半年度")

# ── 单位/币种证据 ─────────────────────────────────────────────────────────
_UNIT_LINE_RE = re.compile(r"单位\s*[:：]\s*(元|万元|千元|百万元|美元|港元)")
_CURRENCY_BY_UNIT = {"元": "CNY", "万元": "CNY", "千元": "CNY", "百万元": "CNY",
                     "美元": "USD", "港元": "HKD"}
_UNIT_SCALE = {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000),
               "百万元": Decimal(1000000), "美元": Decimal(1), "港元": Decimal(1)}

_NUM_RE = re.compile(r"[（(]?-?\d[\d,，]*(?:\.\d+)?[)）]?")
_DECIMAL_RE = re.compile(r"\d[\d,，]*\.\d")
_DASHES = ("—", "-", "－", "不适用", "无", "")
_FULLWIDTH = str.maketrans("０１２３４５６７８９．－，（）", "0123456789.-,()")
_PAGE_NOISE = re.compile(
    r"^(京蓝科技股份有限公司|[^\n]{0,30}股份有限公司)?\s*\d{4}\s*年年度报告全文$|^\d{1,3}$")
_DATE_SPAN_RE = re.compile(r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?"
                           r"|[（(]?\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[)）]?")


def norm_lines(text: str) -> list[str]:
    """正文 → 逻辑行（去页眉页脚/纯页码；统一全角；压掉多余空白）。"""
    out: list[str] = []
    for raw in str(text or "").splitlines():
        s = str(raw).translate(_FULLWIDTH).replace("\u3000", " ").strip()
        if not s or _PAGE_NOISE.match(s):
            continue
        out.append(re.sub(r"[ \t]+", " ", s))
    return out


def page_of(doc: dict, offset: int) -> int:
    """字符偏移 → **PDF 页码**（用 `page_offsets` 反查；查不到返回 0）。

    清洗后的"正文第 N 行"不能冒充页码：定位必须能回到原件。
    """
    page = 0
    for pos, pno in (doc.get("page_offsets") or []):
        if int(pos) <= int(offset):
            page = int(pno)
        else:
            break
    return page


def line_offset(doc: dict, lines: list[str], idx: int) -> int:
    """逻辑行 → 原文字符偏移（用于反查页码）。"""
    text = str(doc.get("text") or "")
    probe = lines[idx][:40]
    if not probe:
        return 0
    found = text.find(probe)
    if found >= 0:
        return found
    return sum(len(x) + 1 for x in lines[:idx])


def parse_number(tok: str):
    """数字 token → `Decimal`；`—`/`-`/空 → `None`（表示"无"，**不是 0**）。"""
    s = str(tok or "").translate(_FULLWIDTH).strip()
    if s in _DASHES:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = s.replace(",", "").replace("，", "")
    if s.startswith("-"):
        neg, s = True, s[1:]
    try:
        v = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    return -v if neg else v


def numbers_in(line: str) -> list[Decimal]:
    """行里的**金额**数字（排除百分比列与日期里的数字）。"""
    out: list[Decimal] = []
    spans = [m.span() for m in _DATE_SPAN_RE.finditer(line)]
    for m in _NUM_RE.finditer(line):
        if any(a <= m.start() < b for a, b in spans):
            continue
        if line[m.end():m.end() + 1] == "%":
            continue
        tok = m.group(0)
        head = line[max(0, m.start() - 1):m.start()]
        tail = line[m.end():m.end() + 1]
        if re.fullmatch(r"(19|20)\d{2}", tok) and (tail in ("年", "-", "/", ".") or head == "年"):
            continue
        v = parse_number(tok)
        if v is not None:
            out.append(v)
    return out


# ── 标签识别（完整语义）────────────────────────────────────────────────────
def _label_of(line: str) -> tuple[str, str]:
    """行首标签 → `(slug, 后缀)`；不是指标行返回 `("", "")`。

    只认**最长**别名，且后缀里出现扣除/占比/账龄等语义词一律不算该指标
    （旧版按前缀匹配，把"营业收入扣除金额"当成营业收入，与真实收入冲突后把整个
    指标删掉——收入因此从抽取结果里消失）。
    """
    s = str(line or "").strip()
    best_slug, best_name = "", ""
    for slug, names in LABELS.items():
        for n in names:
            if s.startswith(n) and len(n) > len(best_name):
                best_slug, best_name = slug, n
    if not best_name:
        return "", ""
    tail = s[len(best_name):]
    # 行文里跟的是数值（`营业收入(元) 995,410,...`）：只对**第一个数字之前**的标签部分
    # 做语义判断，否则"数值本身"会把合法行判掉。
    label_part = re.split(r"\d", tail, maxsplit=1)[0]
    if any(w in label_part for w in LABEL_REJECT_WORDS):
        return "", ""
    if not _LABEL_TAIL_OK.match(label_part):
        return "", ""
    return best_slug, tail.strip()


def _table_at(lines: list[str], idx: int, *, window: int = 200) -> tuple[str, str, str]:
    """该行所属**报表** → `(表性质, 口径, 表名)`；不是可识别报表返回 `("", "", name)`。

    向上找最近的小节标题（`1、合并资产负债表` 这类编号标题，或"主要会计数据和财务指标"
    这种节名）。找不到或落在政策调整/附注表里 → 不认（旧版此时落到泛化的"报表"，
    于是**政策调整表**里的存货被当成 2020 年末）。
    """
    for j in range(idx, max(-1, idx - window), -1):
        line = lines[j]
        m = _TITLE_RE.match(line)
        if m:
            name = m.group(2)
            if name in STATEMENT_TITLES:
                kind, caliber = STATEMENT_TITLES[name]
                return kind, caliber, name
            return "", "", name
        for name, (kind, caliber) in STATEMENT_TITLES.items():
            if name in line:
                return kind, caliber, name
        # 普通语义词（追溯调整/分部/季度…）可能属于**别的节**的句子，不据此停；
        # 但"编号标题 + 非报表"说明已经走出了报表范围 → 停。
        if _NUM_TITLE_RE.match(line) and NOT_A_STATEMENT_RE.search(line):
            return "", "", line[:30]
    return "", "", ""


def _header_years(line: str) -> list[str]:
    """表头行里的期间（年份）序列；不做分隔符假设。"""
    s = str(line or "")
    if _DECIMAL_RE.search(s):
        return []
    ys = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)\s*年?", s)
    if not (2 <= len(ys) <= 6):
        return []
    return [] if _label_of(s)[0] else ys


def find_header(lines: list[str], row_idx: int, *, lookback: int = 14):
    """数据行**之前**最近的一张表头 → `(years, header_idx, has_note_col)`。"""
    for i in range(row_idx - 1, max(-1, row_idx - lookback - 1), -1):
        ys = _header_years(lines[i])
        if ys:
            return ys, i, bool(re.search(r"附注|注\s*[一二三四五六七八九十\d]", lines[i]))
    return [], -1, False


def _unit_evidence(lines: list[str], row_idx: int, header_idx: int):
    """单位证据 → `(unit, scale, currency)`；无证据 → `(None, None, "")`。"""
    lo = max(0, (header_idx if header_idx >= 0 else row_idx) - 4)
    for j in range(row_idx, lo - 1, -1):
        m = _UNIT_LINE_RE.search(lines[j])
        if m:
            u = m.group(1)
            return u, _UNIT_SCALE.get(u, Decimal(1)), _CURRENCY_BY_UNIT.get(u, "")
    m2 = re.search(r"[（(](元|万元|千元|百万元|美元|港元)[)）]", lines[row_idx])
    if m2:
        u = m2.group(1)
        return u, _UNIT_SCALE.get(u, Decimal(1)), _CURRENCY_BY_UNIT.get(u, "")
    return None, None, ""


def extract(doc: dict, *, company: str = "", company_code: str = "",
            periods=(), verify_entity: bool = True) -> dict:
    """→ `{ok, facts, rejected, tables, text_hash, entity_state}`。"""
    import hashlib

    import facts as _fx

    lines = norm_lines(doc.get("text"))
    want_periods = [str(p) for p in (periods or ())]
    title_head = f"{doc.get('title') or ''} {str(doc.get('text') or '')[:2000]}"
    _co = re.sub(r"(股份有限公司|有限公司|集团|股份|公司)$", "", str(company or "")).strip()
    entity_state = "verified" if (_co and _co in title_head) else (
        "no_company_given" if not _co else "unverified")
    text_hash = hashlib.sha256(str(doc.get("text") or "").encode("utf-8")).hexdigest()
    if verify_entity and entity_state == "unverified":
        return {"ok": False, "facts": [], "tables": [],
                "rejected": [{"metric": "*", "reason": "entity_unverified",
                              "page": 0, "label": str(doc.get("title"))[:60],
                              "detail": f"材料里找不到预期主体「{company}」："
                                        f"标题={str(doc.get('title'))[:60]}"}],
                "text_hash": text_hash, "entity_state": entity_state, "lines": len(lines)}

    facts: list[dict] = []
    rejected: list[dict] = []
    tables: list[dict] = []

    def _reject(slug: str, reason: str, idx: int, detail: str) -> None:
        rejected.append({"metric": slug or "*", "reason": reason,
                         "page": page_of(doc, line_offset(doc, lines, idx)),
                         "line": idx, "label": lines[idx][:60], "detail": detail})

    i = 0
    while i < len(lines):
        slug, tail = _label_of(lines[i])
        if not slug:
            i += 1
            continue
        kind, caliber, table_name = _table_at(lines, i)
        if not kind:
            _reject(slug, "table_unrecognized", i,
                    f"该行不属于可识别报表（最近标题：{table_name or '无'}）："
                    f"附注表/政策调整表/季度摘要一律不取")
            i += 1
            continue
        years, hidx, has_note = find_header(lines, i)
        if not years:
            _reject(slug, "no_periods", i, "表头里没有两个及以上年份")
            i += 1
            continue
        unit, scale, currency = _unit_evidence(lines, i, hidx)
        if not unit or not currency:
            _reject(slug, "no_unit_evidence", i,
                    "表头/行内没有单位标注（默认“元”并写“表头单位标注”属补造）")
            i += 1
            continue
        vals = numbers_in(tail)
        if has_note and len(vals) == len(years) + 1 and vals[0] == vals[0].to_integral_value() \
                and abs(vals[0]) < 1000:
            vals = vals[1:]                      # 表头**声明了**附注列，且首数是小额整数
        if len(vals) < len(years):
            _reject(slug, "split_number" if len(vals) and re.search(r"[\d,]$", lines[i])
                    else "column_mismatch", i,
                    f"表头 {years}（{len(years)} 列）但解析到 {len(vals)} 个金额：{lines[i][:60]}")
            i += 1
            continue
        if len(vals) > len(years):
            _reject(slug, "column_mismatch", i,
                    f"金额多于表头列数（{len(vals)} > {len(years)}）：不截断，按拒绝处理")
            i += 1
            continue
        page = page_of(doc, line_offset(doc, lines, i))
        for label, raw_v in zip(years, vals):
            m = re.search(r"(19|20)\d{2}", label)
            period = f"{m.group(0)}年" if m else label
            if want_periods and not any(period.startswith(p[:4]) for p in want_periods):
                continue
            facts.append({
                "metric": slug, "period": period,
                "value": float(raw_v * scale), "unit": "元",
                "currency": currency, "caliber": caliber,
                "unit_source": f"表头单位标注「单位：{unit}」",
                "currency_source": f"由单位「{unit}」判定",
                "caliber_source": f"表名「{table_name}」",
                "entity": company, "entity_id": company_code, "market": "cn",
                "entity_state": entity_state,
                "period_type": "年报", "table": kind, "table_name": table_name,
                "source_url": str(doc.get("url") or ""), "source_hash": text_hash,
                "extracted_by": "annual_financial_tables",
                "locator": f"PDF 第 {page} 页 · {table_name} · 行「{lines[i][:40]}」",
                "quote": lines[i][:200],
                "fact_id": _fx.make_fact_id(company_code, company, slug, period, caliber),
            })
        tables.append({"line": i, "header": years, "unit": unit, "metric": slug,
                       "table": kind, "page": page})
        i += 1

    # 同一 (指标, 期间, 口径, 表) 两个不同值 → 该项整体不入账（不按先后择一）
    seen: dict[tuple, Decimal] = {}
    kept: list[dict] = []
    dropped: set = set()
    for f in facts:
        k = (f["metric"], f["period"], f["caliber"], f["table"])
        if k in dropped:
            continue
        prev = seen.get(k)
        if prev is None:
            seen[k] = Decimal(str(f["value"]))
            kept.append(f)
        elif prev != Decimal(str(f["value"])):
            dropped.add(k)
            kept = [x for x in kept if (x["metric"], x["period"], x["caliber"],
                                        x["table"]) != k]
            rejected.append({"metric": f["metric"], "reason": "conflicting",
                             "page": 0, "label": f["locator"],
                             "detail": f"{f['table']} {f['period']}：同表内已有 {prev}，"
                                       f"又见 {f['value']}——不按先后择一，该项整体不入账"})
    return {"ok": bool(kept), "facts": kept, "rejected": rejected, "tables": tables,
            "text_hash": text_hash, "entity_state": entity_state, "lines": len(lines)}


def derive_gross_profit(facts: list[dict]) -> list[dict]:
    """毛利 = 营业收入 − 营业成本：只在**同主体/同期间/同口径/同币种/同单位**时派生。

    旧版不检查主体与币种（A 公司 CNY 元收入减 B 公司 USD 万元成本也照样出毛利），
    也不带稳定 `fact_id` 与血缘。现在输入必须是已准入事实，且携带 `derived_from`。
    """
    idx = {(f["metric"], f["period"], f.get("caliber") or ""): f for f in facts}
    out: list[dict] = []
    for (metric, period, caliber), rev in list(idx.items()):
        if metric != "revenue":
            continue
        cost = idx.get(("operating_cost", period, caliber))
        if not cost:
            continue
        if any(str(rev.get(k) or "") != str(cost.get(k) or "")
               for k in ("entity_id", "currency", "unit")):
            continue
        if not (rev.get("fact_id") and cost.get("fact_id")):
            continue
        out.append({
            "metric": "gross_profit", "period": period,
            "value": float(rev["value"]) - float(cost["value"]),
            "unit": rev.get("unit") or "元", "currency": rev.get("currency") or "",
            "caliber": caliber, "entity": rev.get("entity") or "",
            "entity_id": rev.get("entity_id") or "", "market": "cn",
            "entity_state": rev.get("entity_state") or "",
            "period_type": "年报", "table": "算式派生",
            "source_url": rev.get("source_url") or "",
            "source_hash": rev.get("source_hash") or "",
            "unit_source": rev.get("unit_source") or "",
            "currency_source": rev.get("currency_source") or "",
            "caliber_source": rev.get("caliber_source") or "",
            "extracted_by": "derived", "formula_version": "gross_profit_v1",
            "formula": f"{rev['value']} - {cost['value']}",
            "derived_from": [rev["fact_id"], cost["fact_id"]],
            "locator": f"{period} {caliber}：营业收入 − 营业成本",
            "quote": f"{rev['value']} - {cost['value']}",
            "fact_id": f"fact-gp-{rev['fact_id'][-8:]}-{cost['fact_id'][-8:]}",
        })
    return out


def to_dataset(doc: dict, *, company: str, company_code: str, periods=(),
               as_of: str = "", restatement: str = "", verify_entity: bool = True):
    """抽取 → 候选观察 → `financial_analysis.AnalysisDataset`（含毛利派生与血缘）。"""
    import financial_analysis as fa

    raw = extract(doc, company=company, company_code=company_code, periods=periods,
                  verify_entity=verify_entity)
    rows = list(raw["facts"]) + derive_gross_profit(raw["facts"])
    ds = fa.freeze_from_facts(
        rows, entity=company, entity_id=company_code, market="cn", periods=periods,
        as_of=as_of, source_label=f"annual_financial_tables:{raw['text_hash'][:12]}",
        restatement=restatement,
        required_metrics=("revenue", "net_profit", "gross_profit", "operating_cashflow"))
    return ds, raw
