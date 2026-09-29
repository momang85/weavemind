# -*- coding: utf-8 -*-
"""年报 PDF 正文里的**财务表**抽取：主要会计数据 / 三张报表 → 可入账的事实行。

为什么要有它：检索/取件拿到的是**形态各不相同**的年报正文（排版、换行、单位标注、
列顺序都随公司与年度变），而分析链要的是"同一主体、同一期间、同一口径"的结构化事实。
这里只做一件事：把**能确定的**读出来，确定不了就**明确拒绝**（不猜、不取近似）。

能容忍的形态（都用真实年报片段做过用例）：
- 标签跨行（`归属于上市公司股东的净利润` + 下一行 `（元）`）、数字另起一行；
- 千分位、负号前置、**括号负数** `(1,234.56)`、全角数字、破折号 `—`/`-` 表示"无"；
- 单位标注 `单位：元 / 万元 / 千元`，并按标注**显式换算**（不是默认元）；
- 表头 `本年比上年增减 / 增减 / 备注 / 同比` 这类**非数值列**可出现在任意位置；
- 指标别名（`营业收入/营业总收入`、`归属于上市公司股东的净利润/归属于母公司所有者的净利润`…）。

已知边界（本批实测，**默认关闭**）：`期末余额/期初余额` 式表头的**远距离**锚定会把附注表
误认成报表（京蓝 2020 应付账款差 ~2680 倍）→ `allow_anchor` 默认 False，需显式开启并自担风险。

明确拒绝（宁可没有）：
- 表头解析不出**两个及以上**期间 → `no_periods`；
- 行内数字个数与表头数值列不匹配 → `column_mismatch`；
- 同一指标在**多处**出现且数值不同 → `conflicting`（不选"看起来更对"的那个）；
- 数字出现在**注释/说明句**里而不是表格行 → 不认（要求行首是指标名）。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# ── 指标词表：别名 → 内部 slug（同一 slug 的多个别名取"第一次出现的可解析行"）──
ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": ("营业收入", "营业总收入"),
    "operating_cost": ("营业成本", "营业总成本"),
    "net_profit": ("归属于上市公司股东的净利润", "归属于母公司所有者的净利润",
                   "归属于母公司股东净利润", "归母净利润"),
    "operating_cashflow": ("经营活动产生的现金流量净额", "经营活动现金流量净额"),
    "total_assets": ("资产总计", "总资产"),
    "total_liabilities": ("负债合计", "总负债"),
    "accounts_receivable": ("应收账款",),
    "inventory": ("存货",),
    "accounts_payable": ("应付账款",),
}
# 只用**主要会计数据**表取的头条指标（三张报表里也会出现同名行，值应一致；不一致时报冲突）
HEADLINE = ("revenue", "net_profit", "operating_cashflow")
# 表优先级：同一 (指标, 期间) 在多张表里出现时，**只用优先表**的值，并把分歧记进
# `cross_table_conflicts`（可见，不静默择大/平均）。主要会计数据是发行人自己的口径声明表。
TABLE_PRIORITY = {"主要会计数据": 0, "合并利润表": 1, "合并资产负债表": 1,
                  "合并现金流量表": 1, "母公司利润表": 3, "母公司资产负债表": 3,
                  "母公司现金流量表": 3, "报表": 2}
_DEFAULT_PRIORITY = 2

# 非数值列标记（表头里出现即视为"不是期间列"）
_NON_VALUE_HEADERS = ("增减", "同比", "变动", "备注", "说明", "比例", "%")
_NUM_RE = re.compile(r"[（(]?-?\d[\d,，]*(?:\.\d+)?[)）]?")
_DECIMAL_RE = re.compile(r"\d[\d,，]*\.\d")   # 数据行特征：带小数的金额
_UNIT_LINE_RE = re.compile(r"单位\s*[:：]\s*(元|万元|千元|百万元)")
_FULLWIDTH = str.maketrans("０１２３４５６７８９．－，（）", "0123456789.-,()")
_TABLE_TITLE_RE = re.compile(r"^\d+[、.．]\s*\S*(资产负债表|利润表|现金流量表|所有者权益变动表)")
_CLOSING_OPENING_RE = re.compile(r"期末余额?\s+期初余额?|期初余额?\s+期末余额?|年末余额?\s+年初余额?")
_PAGE_NOISE = re.compile(
    r"^(京蓝科技股份有限公司|[^\n]{0,30}股份有限公司)?\s*\d{4}\s*年年度报告全文$|^\d{1,3}$")


def norm_lines(text: str) -> list[str]:
    """正文 → 逻辑行（去页眉页脚/纯页码；统一全角；压掉多余空白）。"""
    out: list[str] = []
    for raw in str(text or "").splitlines():
        s = str(raw).translate(_FULLWIDTH).replace("\u3000", " ").strip()
        if not s or _PAGE_NOISE.match(s):
            continue
        out.append(re.sub(r"[ \t]+", " ", s))
    return out


def parse_number(tok: str):
    """数字 token → `Decimal`；`—`/`-`/空 → `None`（表示"无"，**不是 0**）。"""
    s = str(tok or "").translate(_FULLWIDTH).strip()
    if s in ("", "—", "-", "－", "不适用", "无"):
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


_NUM_FRAGMENT_RE = re.compile(r"^[\d.,]+(?:\s|$)")
_DATE_SPAN_RE = re.compile(r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?"
                           r"|[（(]?\d{4}[-/.]\d{1,2}[-/.]\d{1,2}[)）]?")


def _date_spans(line: str) -> list[tuple[int, int]]:
    return [m.span() for m in _DATE_SPAN_RE.finditer(line)]


def numbers_in(line: str) -> list[Decimal]:
    """行里的**金额**数字（排除百分比列与日期里的数字）。

    为什么必须排除百分比：主要会计数据表的列序是
    `2020 年 | 2019 年 | 本年比上年增减 | 2018 年`，增减列是 `-47.65%`。
    把它当金额会**整列错位**（2018 拿到 -47.65）——而错位的数看不出来，只能靠排除。
    日期（`2019 年 2 月 25 日`）同理：那是说明文字里的时间，不是本期金额。
    """
    out: list[Decimal] = []
    spans = _date_spans(line)
    for m in _NUM_RE.finditer(line):
        if any(a <= m.start() < b for a, b in spans):
            continue                                  # 日期整段里的数字
        tail = line[m.end():m.end() + 1]
        if tail == "%":
            continue                                  # 比率列
        head = line[max(0, m.start() - 1):m.start()]
        tok = m.group(0)
        if re.fullmatch(r"(19|20)\d{2}", tok) and (tail in ("年", "-", "/", ".") or head == "年"):
            continue                                  # 年份/日期片段
        v = parse_number(tok)
        if v is not None:
            out.append(v)
    return out


def _is_value_header(cell: str) -> bool:
    c = str(cell or "")
    if not c or any(t in c for t in _NON_VALUE_HEADERS):
        return False
    return bool(re.search(r"(19|20)\d{2}", c)) or c in ("本期", "上期", "期末", "期初")


def _header_years(line: str) -> list[str]:
    """表头行里的期间（年份）序列；**不做分隔符假设**——真实年报里表头常是单空格分隔。"""
    s = str(line or "")
    if _DECIMAL_RE.search(s):
        return []                                     # 有小数金额 → 是数据行，不是表头
    ys = re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)\s*年?", s)
    if not (2 <= len(ys) <= 6):
        return []
    # 表头行不该以指标别名开头（那是数据行）
    return [] if _alias_of(s) else ys


def find_table(lines: list[str], row_idx: int, *, lookback: int = 14,
               anchor_year: int | None = None, allow_anchor: bool = False):
    """在数据行**之前**找最近的一张表头 → `(periods, header_idx, unit)`。

    只往回找：年报的表头在数据行上方；往前找会把下一张表的表头错配到本行。

    两种表头形态都认（"适应不同返回值"）：
    1. **年份式**：`2020 年 2019 年 本年比上年增减 2018 年`（主要会计数据/利润表常见）；
    2. **期末/期初式**：`项目 期末余额 期初余额`（资产负债表/现金流量表常见，表头里
       **没有年份**）。后者按会计惯例映射成 `(报告期, 报告期−1)`，报告期由调用方给的
       `anchor_year` 锚定（= 文档自身报告期）；**锚不到年份就继续拒绝**（不猜）。
    """
    for i in range(row_idx - 1, max(-1, row_idx - lookback - 1), -1):
        ys = _header_years(lines[i])
        if not ys and allow_anchor and anchor_year and _CLOSING_OPENING_RE.search(lines[i]) \
                and not _DECIMAL_RE.search(lines[i]):
            ys = [f"{int(anchor_year)}", f"{int(anchor_year) - 1}"]
            _basis = "closing_opening_anchor"
        else:
            _basis = "year_header"
        if not ys:
            continue
        unit = "元"
        for j in range(max(0, i - 3), i + 1):
            m = _UNIT_LINE_RE.search(lines[j])
            if m:
                unit = m.group(1)
                break
        if not any(_UNIT_LINE_RE.search(lines[j]) for j in range(max(0, i - 3), i + 1)):
            # 表头附近没有单位标注：再看数据行本身有没有 `（元）` 这类后缀
            for j in range(row_idx, min(len(lines), row_idx + 2)):
                m2 = re.search(r"[（(](元|万元|千元|百万元)[)）]", lines[j])
                if m2:
                    unit = m2.group(1)
                    break
        return ys, i, unit, _basis
    # 第二趟：报表主体很长（合并资产负债表从标题到"应付账款"可能隔上百行），
    # 年份式表头在近处找不到时，允许**远距离**找"期末余额/期初余额"式表头；
    # 但跨过另一张表的标题（`2、母公司资产负债表`）就停——不能拿别张表的表头。
    if allow_anchor and anchor_year:
        for i in range(row_idx - lookback - 1, max(-1, row_idx - 160), -1):
            line = lines[i]
            if _TABLE_TITLE_RE.match(line):
                break
            if _DECIMAL_RE.search(line) or not _CLOSING_OPENING_RE.search(line):
                continue
            unit = "元"
            for j in range(max(0, i - 3), i + 1):
                m = _UNIT_LINE_RE.search(lines[j])
                if m:
                    unit = m.group(1)
                    break
            return ([f"{int(anchor_year)}", f"{int(anchor_year) - 1}"], i, unit,
                    "closing_opening_anchor")
    return [], -1, "元", "none"


def _unit_scale(unit: str) -> Decimal:
    return {"元": Decimal(1), "千元": Decimal(1000), "万元": Decimal(10000),
            "百万元": Decimal(1000000)}.get(str(unit or "元"), Decimal(1))


def _alias_of(line: str) -> str:
    """该行是否以某个指标别名开头 → slug（取**最长**别名，避免"营业收入"吃掉"营业总收入"）。"""
    s = str(line or "").strip()
    best = ""
    for slug, names in ALIASES.items():
        for n in names:
            if s.startswith(n) and len(n) > len(best):
                best = n
    if not best:
        return ""
    for slug, names in ALIASES.items():
        if best in names:
            return slug
    return ""


def _strip_alias(line: str) -> str:
    s = str(line or "").strip()
    for names in ALIASES.values():
        for n in names:
            if s.startswith(n):
                return s[len(n):].strip()
    return s


def _row_values(lines: list[str], idx: int, want: int) -> tuple[list[Decimal], str]:
    """取该行的数值：本行不够就去**下一行**补（标签与数字常分行）。

    返回 `(值列表, 用到的原文)`；个数与 `want` 不符时由调用方判 mismatch。
    """
    text = lines[idx]
    # 数字被拆行（`1,279,570,42` 换行后接 `9.23`）：拼起来会得到一个**看不出来的错数**
    # （127,957,042 而不是 1,279,570,429.23）。宁可拒绝。
    _nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
    if re.search(r"\d,\s*$|\d$", text) and _NUM_FRAGMENT_RE.match(_nxt) \
            and not _alias_of(_nxt) and not _TABLE_TITLE_RE.match(_nxt):
        return [], f"SPLIT:{text[:60]} | {_nxt[:40]}"
    vals = numbers_in(_strip_alias(text))
    used = [text]
    j = idx
    while len(vals) < want and j + 1 < len(lines) and j - idx < 2:
        nxt = lines[j + 1]
        if _alias_of(nxt):
            break
        # 上一行以数字/逗号收尾、下一行以数字开头 → 这是**同一个数被拆行**
        if re.search(r"[\d,]$", lines[j]) and _NUM_FRAGMENT_RE.match(nxt) \
                and not _TABLE_TITLE_RE.match(nxt):
            return [], f"SPLIT:{lines[j][:60]} | {nxt[:40]}"
        vals += numbers_in(nxt)
        used.append(nxt)
        j += 1
    return vals, " | ".join(used)


def _table_of(lines: list[str], idx: int, *, window: int = 60) -> str:
    """该行属于哪张表（往上找最近的表名标题；找不到归"报表"）。"""
    for j in range(idx, max(-1, idx - window), -1):
        for name in ("主要会计数据和财务指标", "主要会计数据", "合并利润表", "合并资产负债表",
                     "合并现金流量表", "母公司利润表", "母公司资产负债表", "母公司现金流量表"):
            if name in lines[j]:
                key = "主要会计数据" if name.startswith("主要会计数据") else name
                return key
    return "报表"


def extract(doc: dict, *, company: str = "", company_code: str = "",
            periods=(), anchor_year: int | None = None,
            allow_anchor: bool = False) -> dict:
    """→ `{ok, unit, facts, rejected:[{metric, reason, detail}], tables, text_hash}`。

    `facts` 每项：`{metric, period, value, unit, currency, caliber, entity, entity_id,
    source_url, source_hash, locator, quote, table}`（可直接喂
    `financial_analysis.freeze_from_facts`）。
    """
    import hashlib

    lines = norm_lines(doc.get("text"))
    want_periods = [str(p) for p in (periods or ())]
    facts: list[dict] = []
    seen: dict[tuple[str, str, str], Decimal] = {}
    dropped: set = set()
    cross: list[dict] = []
    rejected: list[dict] = []
    tables: list[dict] = []
    src_hash = hashlib.sha256(str(doc.get("text") or "").encode("utf-8")).hexdigest()

    i = 0
    while i < len(lines):
        slug = _alias_of(lines[i])
        if not slug:
            i += 1
            continue
        years, hidx, unit, basis = find_table(lines, i, anchor_year=anchor_year,
                                              allow_anchor=allow_anchor)
        if not years:
            rejected.append({"metric": slug, "reason": "no_periods",
                             "detail": f"行 {i} 附近找不到含两个及以上期间的表头"})
            i += 1
            continue
        vals, used = _row_values(lines, i, len(years))
        if used.startswith("SPLIT:"):
            rejected.append({"metric": slug, "reason": "split_number",
                             "detail": used[6:]})
            i += 1
            continue
        if len(vals) < len(years):
            rejected.append({"metric": slug, "reason": "column_mismatch",
                             "detail": f"表头 {years}（{len(years)} 列）但只解析到 "
                                       f"{len(vals)} 个数值：{used[:80]}"})
            i += 1
            continue
        vals = vals[:len(years)]
        scale = _unit_scale(unit)
        for label, raw_v in zip(years, vals):
            m = re.search(r"(19|20)\d{2}", label)
            period = f"{m.group(0)}年" if m else label
            if want_periods and not any(period.startswith(p[:4]) for p in want_periods):
                continue
            key = (slug, period)
            value = raw_v * scale                     # 按标注单位**显式换算**到元
            table = _table_of(lines, i)
            tkey = (slug, period, table)
            if tkey in seen:
                prev_v = seen[tkey]
                if prev_v == value:
                    continue
                # **同一张表内**两个不同值 = 该表这一项的冲突：只废掉这一项，不牵连别的表
                rejected.append({"metric": slug, "reason": "conflicting",
                                 "detail": f"{table} {period}: 已有 {prev_v}，又见 {value}"
                                           f"（同表内不一致：**按先后顺序择一是错的**，"
                                           f"该项整体不入账）"})
                dropped.add(tkey)
                facts = [f for f in facts
                         if not (f["metric"] == slug and f["period"] == period
                                 and f.get("table") == table)]
                continue
                # 同一张表里两个不同值 = 真冲突（整条指标作废）；
                # 跨表分歧 = 按表优先级取一个，**并记下来**（不静默）
                if prev_table == table:
                    rejected.append({"metric": slug, "reason": "conflicting",
                                     "detail": f"{table} {period}: 已有 {prev_v}，又见 {value}"
                                               f"（同表内不一致，按冲突处理）"})
                    continue
                keep_new = TABLE_PRIORITY.get(table, _DEFAULT_PRIORITY) < \
                    TABLE_PRIORITY.get(prev_table, _DEFAULT_PRIORITY)
                cross.append({"metric": slug, "period": period,
                              "kept": table if keep_new else prev_table,
                              "dropped": prev_table if keep_new else table,
                              "kept_value": float(value if keep_new else prev_v),
                              "dropped_value": float(prev_v if keep_new else value)})
                if keep_new:
                    facts = [f for f in facts
                             if not (f["metric"] == slug and f["period"] == period)]
                else:
                    continue
            if tkey in dropped:
                continue
            seen[tkey] = value
            facts.append({
                "metric": slug, "period": period, "value": float(value), "unit": "元",
                "currency": "CNY", "caliber": "合并", "entity": company,
                "entity_id": company_code, "market": "cn",
                "period_type": "年报", "source_url": str(doc.get("url") or ""),
                "source_hash": src_hash, "extracted_by": "annual_financial_tables",
                "locator": f"正文第 {i} 行（{unit}）",
                "quote": used[:200], "table": _table_of(lines, i),
                "period_basis": basis,
                "unit_source": f"表头单位标注：{unit}",
            })
        tables.append({"line": i, "header": years, "unit": unit, "metric": slug})
        i += 1

    # 按 (指标, 期间) 归并：取**表优先级最高**的那张表的值；同优先级多表不一致 → 记 cross
    best: dict[tuple[str, str], tuple[int, dict]] = {}
    for f in facts:
        k = (f["metric"], f["period"])
        pr = TABLE_PRIORITY.get(str(f.get("table") or ""), _DEFAULT_PRIORITY)
        cur = best.get(k)
        if cur is None or pr < cur[0]:
            if cur is not None and cur[1]["value"] != f["value"]:
                cross.append({"metric": f["metric"], "period": f["period"],
                              "kept": f.get("table"), "dropped": cur[1].get("table"),
                              "kept_value": f["value"], "dropped_value": cur[1]["value"]})
            best[k] = (pr, f)
        elif cur[1]["value"] != f["value"]:
            cross.append({"metric": f["metric"], "period": f["period"],
                          "kept": cur[1].get("table"), "dropped": f.get("table"),
                          "kept_value": cur[1]["value"], "dropped_value": f["value"]})
    facts = [v[1] for v in best.values()]
    return {"ok": bool(facts), "unit": "元", "facts": facts, "rejected": rejected,
            "tables": tables, "cross_table_conflicts": cross,
            "text_hash": src_hash, "lines": len(lines)}


def derive_gross_profit(facts: list[dict]) -> list[dict]:
    """毛利 = 营业收入 − 营业成本（**声明过的算式**，带 inputs，不猜）。"""
    import facts as _fx

    idx = {(f["metric"], f["period"]): f for f in facts}
    out: list[dict] = []
    for (metric, period), rev in list(idx.items()):
        if metric != "revenue":
            continue
        cost = idx.get(("operating_cost", period))
        if not cost:
            continue
        gp = float(rev["value"]) - float(cost["value"])
        out.append({
            "metric": "gross_profit", "period": period, "value": gp, "unit": "元",
            "currency": "CNY", "caliber": rev.get("caliber") or "合并",
            "entity": rev.get("entity") or "", "entity_id": rev.get("entity_id") or "",
            "market": "cn", "period_type": "年报",
            "source_url": rev.get("source_url") or "", "source_hash": "",
            "extracted_by": "derived", "formula": f"{rev['value']} - {cost['value']}",
            "derived_from": [_fx.make_fact_id(rev.get("entity_id") or "",
                                              rev.get("entity") or "", "revenue", period,
                                              rev.get("caliber") or "合并"),
                             _fx.make_fact_id(cost.get("entity_id") or "",
                                              cost.get("entity") or "", "operating_cost",
                                              period, cost.get("caliber") or "合并")],
            "locator": f"{period}：营业收入 − 营业成本",
            "quote": f"{rev['value']} - {cost['value']}", "table": "算式派生",
        })
    return out


def to_dataset(doc: dict, *, company: str, company_code: str, periods=(),
               as_of: str = "", restatement: str = ""):
    """抽取 → 事实行 → `financial_analysis.AnalysisDataset`（含毛利派生）。"""
    import financial_analysis as fa

    from narrative_evidence import _doc_period
    _dp = _doc_period(str(doc.get("title") or ""), str(doc.get("url") or ""))
    # `allow_anchor=False`（默认）：真实报告实测，"期末余额/期初余额"式表头**远距离**锚定
    # 会把附注表错认成报表（京蓝 2020：应付账款取到 650,625 元，而合并资产负债表是
    # 1,743,811,151.80 元，差 ~2680 倍）——抽错一个数比少一个数危险得多，故默认关闭。
    raw = extract(doc, company=company, company_code=company_code, periods=periods,
                  anchor_year=(int(_dp) if _dp else None), allow_anchor=False)
    rows = list(raw["facts"]) + derive_gross_profit(raw["facts"])
    ds = fa.freeze_from_facts(
        rows, entity=company, entity_id=company_code, market="cn", periods=periods,
        as_of=as_of, source_label=f"annual_financial_tables:{raw['text_hash'][:12]}",
        restatement=restatement,
        required_metrics=("revenue", "net_profit", "gross_profit", "operating_cashflow"))
    return ds, raw
