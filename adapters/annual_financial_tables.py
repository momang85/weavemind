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
    "revenue": ("营业收入",),
    # `营业总收入` 是利润表的**汇总行**，`营业收入` 往往是它的细项（金融/类金融业务另计
    # 利息收入等）。两者在三一年报里相差 0.78%（78,383,379 vs 77,773,391 千元），
    # 同挂一个 slug 会被"同表不同值→整项不入账"规则双双删掉（实机发生过）。
    # 因此分开记：模型用 `revenue`（营业收入），`total_revenue` 如实留档，
    # 跨来源对照时必须先看口径（东财 TOTALOPERATEREVE 是营业总收入口径）。
    "total_revenue": ("营业总收入",),
    "operating_cost": ("营业成本",),
    # `归属于母公司股东的净利润`（洋河/三一都这么写）与 `归属于母公司所有者的净利润`
    # 是同一行的两种写法，都要认；不能只认一种再靠"少一个数"去发现。
    "net_profit": ("归属于上市公司股东的净利润", "归属于母公司所有者的净利润",
                   "归属于母公司股东的净利润", "归属于母公司股东净利润", "归母净利润"),
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
NOT_A_STATEMENT_RE = re.compile(r"会计政策(变更|调整)|追溯调整|前期差错|分部|季度|半年度"
                                r"|首次执行|新收入准则|新租赁准则|年初财务报表")
# 表头层面的调整表标记（比标题更可靠）：`2019年12月31日 | 2020年01月01日 | 调整数`
_ADJUST_HEADER_RE = re.compile(r"调整数|新准则|原准则|追溯调整|重述")
# 年初/期初列（`2020年01月01日`）：它等于**上一年末**，不是"2020 年末"，不得占 2020 年这一格
_OPENING_LABEL_RE = re.compile(r"(?:01|1)\s*月\s*(?:01|1)\s*日|年初|期初")

# ── 单位/币种证据 ─────────────────────────────────────────────────────────
_UNIT_LINE_RE = re.compile(r"单位\s*[:：]\s*(元|万元|千元|百万元|美元|港元)")
# **币种是独立证据**（K0-b）：`单位：元 币种：美元` 里的"元"是美元的**基本单位**，
# 不能按单位推断成 CNY；两者分别取证，冲突时以**声明的币种**为准。
_CURRENCY_LINE_RE = re.compile(r"币种\s*[:：]\s*(人民币|美元|港元|CNY|USD|HKD)", re.I)
_CURRENCY_ALIAS = {"人民币": "CNY", "美元": "USD", "港元": "HKD",
                   "CNY": "CNY", "USD": "USD", "HKD": "HKD"}
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

# ── 报表行的**书写形态**（2026-09-29 A2 实机补：三种版面特征挡住过完整标签匹配）──
# 1. 行项目编号/层级前缀：`一、营业总收入`、`（一）`、`1.`、`(1)`
_LINE_PREFIX_RE = re.compile(
    r"^(?:[（(]?[一二三四五六七八九十]+[)）]?\s*[、.．]?\s*"
    r"|[（(]\d{1,2}[)）]\s*"
    r"|\d{1,2}\s*[、.．]\s*)+")
# 2. "其中/加/减"这类**行项目标记**：说明这是上一行的细项，不是另一个指标名
_LINE_MARKER_RE = re.compile(r"^(?:其中|加|减|其他|其它)\s*[:：]\s*")
# 3. **附注列引用**夹在标签与金额之间（`营业成本 七、54 57,216,959 54,051,053`）：
#    它不是金额，但旧逻辑"取第一个数字之前的部分当标签"会被它截断。
_NOTE_REF_RE = re.compile(r"(?:附注\s*)?[一二三四五六七八九十]+\s*[、.．]\s*\d{1,3}(?![\d,，.])")
# 4. **折行标签**：标签行末尾是没闭合的括号（`1.归属于母公司股东的净利润(净亏损`），
#    金额在下一行（`以“-”号填列) 5,975,451 4,527,451`）。
_OPEN_TAIL_RE = re.compile(r"^[\s:：]*[（(][^）)]*$")
# 折行金额行的形态：一段不含数字的短说明 + 右括号 + 紧跟金额
_WRAP_VALUE_RE = re.compile(r"^[^\d]{0,24}(?:填列|列)\s*[)）]\s*[（(]?-?\d")
# 期末/期初型列头（表头不写年份，年份在报表日期行上）
_END_START_RE = re.compile(r"(期末余额|期初余额|期末数|期初数|年末余额|年初余额"
                           r"|本期发生额|上期发生额)")


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
def _strip_line_form(line: str) -> str:
    """剥掉**书写形态**（行项目编号/其中·加·减标记/附注列引用），只留标签+金额。

    这些都不是指标名的一部分，却会让"完整标签匹配"失败——实机三种版面都踩到了：
    三一 `一、营业总收入 78,383,379 74,018,936`、`货币资金 七、1 20,383,175 ...`；
    洋河 `其中：营业收入 28,876,296,993.56 ...`。剥掉的是**格式**，标签本身仍要完整匹配。
    """
    s = str(line or "").strip()
    s = _LINE_PREFIX_RE.sub("", s)
    s = _LINE_MARKER_RE.sub("", s)
    return _NOTE_REF_RE.sub(" ", s)


def _label_of(line: str) -> tuple[str, str]:
    """行首标签 → `(slug, 后缀)`；不是指标行返回 `("", "")`。

    只认**最长**别名，且后缀里出现扣除/占比/账龄等语义词一律不算该指标
    （旧版按前缀匹配，把"营业收入扣除金额"当成营业收入，与真实收入冲突后把整个
    指标删掉——收入因此从抽取结果里消失）。

    `line` 允许带报表行的书写形态（编号/其中/附注引用），函数内部先按形态剥掉；
    **折行标签**（末尾是没闭合的括号）也认，金额由调用方从下一行取（见 `extract`）。
    """
    s = _strip_line_form(line)
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
    if _OPEN_TAIL_RE.match(label_part):
        # 折行：标签行没有金额，括号也没闭合。这里只认"标签 + 未闭合括号"，
        # 金额必须由下一行**按折行形态**给出，否则调用方拿不到数字、照样拒绝。
        return best_slug, tail.strip()
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
            if _near_not_a_statement(lines, j):
                return "", "", name
            if name in STATEMENT_TITLES:
                kind, caliber = STATEMENT_TITLES[name]
                return kind, caliber, name
            return "", "", name
        for name, (kind, caliber) in STATEMENT_TITLES.items():
            if name in line:
                # `合并利润表影响`（会计政策变更说明）里也有"合并利润表"四个字：标题附近
                # 出现"政策变更/追溯调整/分部/季度"就按**非报表**处理——否则政策调整表里的
                # 营业成本会被当成合并利润表的营业成本，与真表冲突后把整项删掉（实机发生）。
                if _near_not_a_statement(lines, j):
                    return "", "", name
                return kind, caliber, name
        # 普通语义词（追溯调整/分部/季度…）可能属于**别的节**的句子，不据此停；
        # 但"编号标题 + 非报表"说明已经走出了报表范围 → 停。
        if _NUM_TITLE_RE.match(line) and NOT_A_STATEMENT_RE.search(line):
            return "", "", line[:30]
    return "", "", ""


def _header_tokens(line: str) -> list[str]:
    """表头行里的**完整期间 token**（`2024年12月31日` / `2024年` / `2024-12-31`）。

    为什么要保留完整 token（K0-b）：只留 4 位年份会把 `2024年1月1日`（= 上年末的
    年初余额）读成"2024 年末"，把期初数记到年末格上。位置与日期一起带出去，
    由 `_OPENING_LABEL_RE` 判它是不是年初/期初列。
    """
    s = str(line or "")
    if _DECIMAL_RE.search(s):
        return []
    toks = re.findall(r"(?<!\d)((?:19|20)\d{2}(?:\s*年(?:\s*\d{1,2}\s*月)?"
                      r"(?:\s*\d{1,2}\s*日)?|\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2})?)(?!\d)", s)
    if not (2 <= len(toks) <= 6):
        return []
    return [] if _label_of(s)[0] else toks


def _year_of_token(tok: str) -> str:
    """完整期间 token → 4 位年份字符串（取不到返回空串）。"""
    m = re.search(r"(?:19|20)\d{2}", str(tok or ""))
    return m.group(0) if m else ""


def _header_years(line: str) -> list[str]:
    """表头行里的期间（年份）序列；不做分隔符假设。"""
    toks = _header_tokens(line)
    return [_year_of_token(t) for t in toks] if toks else []


# 完整日期（`2024-01-01` / `2024/1/1` / `2024年1月1日`）——**不能截年**（L0-a，复核 F2）
_FULL_DATE_RE = re.compile(r"((?:19|20)\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")


def _column_period(label: str, *, statement_kind: str) -> dict | None:
    """列头 token → `{period, period_start, period_end, period_kind, opening, why}`。

    为什么需要它（2026-09-30 复核 F2）：`2024-01-01 / 2023-12-31` 这种列头原先只截 4 位
    年份，于是"期初列"（= 上一年末）被记成 **2024 年末**，把 90/100 记反。这里解析**完整
    日期**，并按报表性质区分存量（stock，时点余额）与流量（flow，区间发生额）：

    - 存量表里 `X年1月1日` 的列头 = **上一年末**（`X-1-12-31`），不是 X 年末；
    - 流量表里 `X年1月1日…` 的列头是 X 年的区间起点，期间仍是 X 年；
    - 解析不出期间 → 返回 `None`（调用方拒绝该单元格，不猜）。
    """
    tok = str(label or "").strip()
    if not tok:
        return None
    stock_stmt = "资产负债表" in str(statement_kind or "")
    m = _FULL_DATE_RE.search(tok)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None
        opening = (mo == 1 and d == 1)
        if opening and stock_stmt:
            return {"period": f"{y - 1}年", "period_start": f"{y - 1}-12-31",
                    "period_end": f"{y - 1}-12-31", "period_kind": "stock",
                    "opening": True,
                    "why": f"列头「{tok}」是上年末（期初）时点，记 {y - 1} 年末"}
        iso = f"{y:04d}-{mo:02d}-{d:02d}"
        return {"period": f"{y}年", "period_start": iso, "period_end": iso,
                "period_kind": "stock" if stock_stmt else "flow", "opening": False,
                "why": f"列头「{tok}」→ {iso}"}
    m2 = re.search(r"(?:19|20)\d{2}", tok)
    if m2 and _OPENING_LABEL_RE.search(tok):
        # `年初余额` 这类文字列头：同样属于上一年末（增量：不再直接丢弃该列）
        return {"period": f"{int(m2.group(0)) - 1}年",
                "period_start": f"{int(m2.group(0)) - 1}-12-31",
                "period_end": f"{int(m2.group(0)) - 1}-12-31", "period_kind": "stock",
                "opening": True, "why": f"列头「{tok}」为年初/期初 → 上一年末"}
    if m2:
        return {"period": f"{m2.group(0)}年", "period_start": "", "period_end": "",
                "period_kind": "stock" if stock_stmt else "flow", "opening": False,
                "why": f"列头「{tok}」只给年度，期间按报表性质记 "
                       f"{'存量(stock)' if stock_stmt else '流量(flow)'}"}
    return None


def find_header(lines: list[str], row_idx: int, *, lookback: int = 14):
    """数据行**之前**最近的一张表头 → `(years, header_idx, has_note_col)`。

    **不跨报表标题**（K0-b）：新的一张表没有自己的年份/单位时，不能向前借用上一张表的
    表头（实机反例：母公司表缺年份，借到上一张合并表的"万元"表头）。遇到报表标题
    或明确不是报表的小节就停——短距离与远距离路径都要尊重表边界。
    """
    for i in range(row_idx - 1, max(-1, row_idx - lookback - 1), -1):
        line = lines[i]
        if _is_statement_title(line) or NOT_A_STATEMENT_RE.search(line):
            return [], -1, False
        ys = _header_years(line)
        if ys:
            return ys, i, bool(re.search(r"附注|注\s*[一二三四五六七八九十\d]", line))
    return [], -1, False


def _statement_date_year(lines: list[str], row_idx: int, *, window: int = 60) -> int:
    """报表日期行里的年份（`2024 年12 月 31 日`）——用于 `期末余额/期初余额` 型表头。

    只看**同一个报表标题之下**的日期：找不到就返回 0（调用方按无期间拒绝），
    绝不拿报告期年份兜底。
    """
    for i in range(row_idx - 1, max(-1, row_idx - window - 1), -1):
        line = lines[i]
        if _is_statement_title(line) or NOT_A_STATEMENT_RE.search(line):
            break
        m = re.search(r"((?:19|20)\d{2})\s*年", line)
        if m and _DATE_SPAN_RE.search(line):
            return int(m.group(1))
    return 0


def _is_statement_title(line: str) -> bool:
    """这一行是不是（任意一张）报表标题：`1、合并资产负债表` / `合并利润表`。"""
    m = _TITLE_RE.match(line)
    if m:
        return True
    return any(name in line for name in STATEMENT_TITLES)


def _end_start_years(line: str, year: int) -> tuple[list[str], list[str]]:
    """`期末余额 / 期初余额` 型列头 → `(年份清单, 存量/流量清单)`，**按列顺序**（K0-b）。

    实机反例：`期初余额 期末余额`（先期初后期末）时固定映射成 `[本年, 上年]`，于是
    90/100 被倒着记（期初数当成本期数）。这里按 token 在行内的先后分别指派：
    期末/年末 → 本年；期初/年初 → 上年（= 本年 − 1）。本期/上期发生额同理。
    每一列的**存量/流量**也一并带出（余额=stock，发生额=flow），供事实层如实标注。
    """
    years: list[str] = []
    kinds: list[str] = []
    for m in re.finditer(r"期末余额|期初余额|期末数|期初数|年末余额|年初余额"
                         r"|本期发生额|上期发生额|本期数|上期数", str(line or "")):
        tok = m.group(0)
        cur = tok.startswith(("期末", "年末", "本期"))
        years.append(f"{year}年" if cur else f"{year - 1}年")
        kinds.append("stock" if ("余额" in tok or "数" in tok and "发生额" not in tok)
                     else "flow")
    return years, kinds


def find_header_ex(lines: list[str], row_idx: int, *, lookback: int = 14,
                   table_window: int = 90) -> dict:
    """表头（**含跨行/期末期初型**）→ `{years, labels, top, has_note, source}`。

    优先用原来的"上一行以内找年份"，保持一致；找不到再走**表内向上找**（`source="table"`）：

    - 一张报表的表头可能**跨行**（`项目` / `2024 年 12 月 31 日` / `2023 年 12 月 31 日`
      各占一行），也会在每页重复；数据行离表头可以很远（三一利润表：表头在 4990，
      归母净利润行在 5022）。所以放宽到"同一张报表内向上找最近一张表头"，遇到
      **报表标题**或**明确不是报表的小节**（会计政策调整/分部/季度）就停；
    - `期末余额/期初余额`（洋河合并资产负债表）这种**不写年份**的表头：年份来自报表
      日期行，**按列顺序**映射为"本年/上年"（先期初就先把上年放前面），来源记 `end_start`。
    """
    years, hidx, has_note = find_header(lines, row_idx, lookback=lookback)
    if years:
        note = bool(re.search(r"附注|注\s*[一二三四五六七八九十\d]", lines[hidx]))
        toks = _header_tokens(lines[hidx]) or list(years)
        return {"years": years, "labels": toks, "top": hidx, "has_note": note,
                "source": "above", "end_start": False}
    for i in range(row_idx - 1, max(-1, row_idx - table_window - 1), -1):
        line = lines[i]
        if _is_statement_title(line) or NOT_A_STATEMENT_RE.search(line):
            break
        ys = _header_years(line)
        if ys:
            toks = _header_tokens(line) or list(ys)
            return {"years": ys, "labels": toks, "top": i,
                    "has_note": bool(re.search(r"附注|注\s*[一二三四五六七八九十\d]", line)),
                    "source": "table", "end_start": False}
        if _END_START_RE.search(line):
            # 期末/期初型列头：**按列顺序**取年份（本期/上期两组），年份由报表日期行给出
            year = _statement_date_year(lines, i + 1)
            if year:
                ordered, kinds = _end_start_years(line, year)
                if not ordered:
                    ordered, kinds = [f"{year}年", f"{year - 1}年"], ["stock", "stock"]
                return {"years": ordered, "labels": ordered, "kinds": kinds, "top": i,
                        "has_note": False, "source": "end_start", "end_start": True}
    return {"years": [], "labels": [], "kinds": [], "top": -1, "has_note": False,
            "source": "", "end_start": False}


def _near_not_a_statement(lines: list[str], title_idx: int, *, back: int = 10) -> bool:
    """标题**附近**（标题及其上方若干行）是否出现"政策变更/追溯调整/分部/季度"字样。

    用途：`合并利润表影响` 这种**会计政策变更说明表**的标题里也有"合并利润表"，
    只看标题会把政策调整数当成报表数。
    """
    lo = max(0, title_idx - back)
    return any(NOT_A_STATEMENT_RE.search(lines[j]) for j in range(lo, title_idx + 1))


def _statement_scan(lines: list[str], row_idx: int, *, window: int = 150):
    """在**同一张报表内**向上走：返回 `(停止处, 走过的行)`，遇到报表标题/非报表就停。

    报表可能跨页，页眉会在每页重复，单位标注只在报表开头出现一次——所以"报表内向上看"
    比"行上方 N 行"更接近事实。停下来的地方是**报表标题**或**明确不是报表的小节**。
    """
    walked: list[int] = []
    for i in range(row_idx - 1, max(-1, row_idx - window - 1), -1):
        line = lines[i]
        if _is_statement_title(line) or NOT_A_STATEMENT_RE.search(line):
            return i, walked
        walked.append(i)
    return -1, walked


def _is_label_continuation(line: str) -> bool:
    """折行标签与金额之间的"续行"是不是**非数字**的标签/单位碎片（`(元)`、`以“-”号填列)`）。

    必须是**不含数字**的短碎片：实机反例是"数字被排版截断"——
    `应收账款` / `1,279,570,42` / `9.23 1,966,154,875.23`，若把 `9.23` 当初下一行的金额，
    就会把 12.79 亿记成 9.23 元（抽错一个数比少一个数危险，宁可拒绝）。
    """
    s = str(line or "").strip()
    if not s or len(s) > 24:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    return "。" not in s


def _unit_evidence(lines: list[str], row_idx: int, header_idx: int, *, top: int = -1,
                   last_idx: int = -1):
    """单位与币种证据 → `(unit, scale, currency, unit_src, cur_src)`；无证据 → `(None, …)`。

    两段：先看**表头附近**（含跨行表头的最上面一行，`top`）；不够就沿**同一张报表**
    继续向上找最近的单位标注——报表跨页时页眉重复、单位标注只在报表开头有一次
    （三一合并现金流量表：单位在 5122，期末那行在 5156）。找不到就是找不到（不默认"元"）。

    `last_idx`：折行时金额在其下若干行，行内单位标注（`(元)`）可能落在两行之间，
    所以行内检查覆盖 `[row_idx, last_idx]` 这一段。

    **币种单独取证**（K0-b）：`单位：元 币种：美元` 的"元"是美元的**基本单位**，
    按单位推断会得到 CNY（实机反例）。声明了币种就用声明的，并分别记录两条证据。
    **不跨报表标题**借用上一张表的单位标注（新表缺单位时按无证据拒绝）。
    """
    def _cur_line(i: int) -> tuple[str, str]:
        """该行里的**显式币种**标注 → `(币种, 来源)`；没有 → `("", "")`。"""
        line = lines[i]
        m = _CURRENCY_LINE_RE.search(line)
        if not m:
            return "", ""
        cur = _CURRENCY_ALIAS.get(m.group(1).upper() if m.group(1).isascii()
                                  else m.group(1), "")
        return (cur, f"同行币种标注「{m.group(0)}」") if cur else ("", "")

    def _boundary(i: int) -> bool:
        return (_is_statement_title(lines[i]) or NOT_A_STATEMENT_RE.search(lines[i])
                or _ADJUST_HEADER_RE.search(lines[i]))

    anchor = top if top >= 0 else (header_idx if header_idx >= 0 else row_idx)
    # **单位与币种分别取证**（L0-a，复核 F3）：`单位：元` 与下一行 `币种：美元` 是两条
    # 独立证据。此前只在**带单位的那一行**里找币种，找不到就按"元→CNY"推断，于是
    # 美元表被记成 CNY。现在两者各走各的搜索，显式币种优先，且都受同一报表边界限制。
    found_unit = found_cur = ""
    unit_src = cur_src = ""
    for j in range(row_idx, max(0, anchor - 6) - 1, -1):
        if j != row_idx and _boundary(j):
            break
        if not found_cur:
            cur, cs = _cur_line(j)
            if cur:
                found_cur, cur_src = cur, cs
        if not found_unit:
            m = _UNIT_LINE_RE.search(lines[j])
            if m:
                found_unit = m.group(1)
                unit_src = f"表头单位标注「单位：{found_unit}」"
        if found_unit and found_cur:
            break
    if not (found_unit and found_cur):
        _stop, walked = _statement_scan(lines, min(row_idx, anchor))
        for j in walked:
            if not found_cur:
                cur, cs = _cur_line(j)
                if cur:
                    found_cur, cur_src = cur, cs
            if not found_unit:
                m = _UNIT_LINE_RE.search(lines[j])
                if m:
                    found_unit = m.group(1)
                    unit_src = f"同表向上找到单位标注「单位：{found_unit}」"
            if found_unit and found_cur:
                break
    if not found_unit:
        end = max(row_idx, last_idx)
        for j in range(row_idx, end + 1):
            m2 = re.search(r"[（(](元|万元|千元|百万元|美元|港元)[)）]", lines[j])
            if m2:
                found_unit = m2.group(1)
                unit_src = f"行内单位标注「({found_unit})」"
                break
    if not found_unit:
        return None, None, "", "", ""
    if not found_cur:
        # 没有显式币种标注 → 才按单位推断（并写明是推断出来的）
        found_cur, cur_src = _CURRENCY_BY_UNIT.get(found_unit, ""), \
            f"由单位「{found_unit}」判定"
    return found_unit, _UNIT_SCALE.get(found_unit, Decimal(1)), found_cur, unit_src, cur_src


def _continuation_kind(line: str) -> str:
    """折行续行的**形态**：`"value"`（只含金额/百分号）｜`"unit"`（单位/括号碎片）｜
    `"label"`（带科目文字 → **新行边界**，不得被上一行借走）。

    实机反例（K0-b）：`存货` 行没有金额，下面紧跟 `在建工程 200.00 180.00`——旧逻辑
    只看"下一行有没有已知指标标签"，而 `在建工程` 不在 LABELS 里，于是被当成存货的
    折行金额取走。未知行标签同样是**新的一行**。
    """
    s = str(line or "").strip()
    if not s:
        return "unit"
    if _WRAP_VALUE_RE.match(s):
        return "value"
    # 去掉单位/括号/百分号/数字后还剩什么：没有汉字就是纯金额行；剩下的是单位碎片则算 unit
    rest = re.sub(r"[（(][^）)]{0,8}[)）]", " ", s)
    rest = re.sub(r"[0-9,，.．%％\-—－\s/]+", " ", rest)
    rest = re.sub(r"(元|万元|千元|百万元|美元|港元|人民币|股|填列|列)", " ", rest)
    rest = re.sub(r"[\s:：、,。.]+", "", rest)
    if not rest:
        return "value" if any(ch.isdigit() for ch in s) else "unit"
    return "label"


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
        header = find_header_ex(lines, i)
        years, hidx, has_note = header["years"], header["top"], header["has_note"]
        labels = list(header.get("labels") or years)
        if not years:
            _reject(slug, "no_periods", i, "表头里没有两个及以上年份")
            i += 1
            continue
        if hidx >= 0 and _ADJUST_HEADER_RE.search(lines[hidx]):
            # 实机反例（京蓝）：`首次执行新收入准则调整年初财务报表` 那张表也叫"合并资产负债表"，
            # 表头是 `2019年12月31日 | 2020年01月01日 | 调整数`。它的两列**不是年度报表数**
            # （一列是调整前、一列是调整后的年初数），照年度口径记账会让"2020年"出现两个值。
            _reject(slug, "table_unrecognized", i,
                    f"这是调整/重述对比表（表头：{lines[hidx][:50]}），不是年度报表")
            i += 1
            continue
        vals = numbers_in(tail)
        value_line = i
        if not vals:
            # **折行**：标签行没有金额，金额在下面一两行。两种实机形态：
            #   三一：`1.归属于母公司股东的净利润(净亏损` + `以“-”号填列) 5,975,451 4,527,451`
            #   京蓝：`归属于上市公司股东的净利润` + `(元)` + `-2,354,850,607.11 -1,036,745,832.56 …`
            # 放宽到两行仍然只认"**没有另一条指标标签**、且真的能读出金额"的续行；
            # 下一行只要出现指标标签就停（那是另一行数据，不是本行的折行）。
            for step in (1, 2):
                j = i + step
                if j >= len(lines) or _label_of(lines[j])[0]:
                    break
                # 中间只允许**单位/括号碎片**（京蓝的 `(元)`）；夹着科目文字
                # （`在建工程 200.00`）或数字碎片（`1,279,570,42`）都不是本行折行。
                if any(_continuation_kind(lines[k]) != "unit"
                       for k in range(i + 1, j)):
                    break
                if _continuation_kind(lines[j]) != "value":
                    continue                  # 本行只是单位碎片 → 继续看下一行
                cand = numbers_in(lines[j])
                if len(cand) >= 2:
                    vals, value_line = cand, j
                    break
        # 单位证据要看**标签行到金额行**这一整段（京蓝的 `(元)` 就夹在两行之间）
        unit, scale, currency, unit_src, cur_src = _unit_evidence(
            lines, i, hidx, top=hidx, last_idx=value_line)
        if not unit or not currency:
            _reject(slug, "no_unit_evidence", i,
                    "表头/行内没有单位标注（默认“元”并写“表头单位标注”属补造）")
            i += 1
            continue
        if has_note and len(vals) == len(years) + 1 and vals[0] == vals[0].to_integral_value() \
                and abs(vals[0]) < 1000:
            vals = vals[1:]                      # 表头**声明了**附注列，且首数是小额整数
        if len(vals) < len(years):
            _reject(slug, "split_number" if len(vals) and re.search(r"[\d,]$", lines[value_line])
                    else "column_mismatch", i,
                    f"表头 {years}（{len(years)} 列）但解析到 {len(vals)} 个金额："
                    f"{lines[value_line][:60]}")
            i += 1
            continue
        if len(vals) > len(years):
            # 列比年份多，两种常见成因分开报（都是**拒绝**，但读者要知道该去补什么）：
            # ① 同一年的"调整后/调整前"两列 + 一个变动率列（主要会计数据表，三一实测：
            #    2024/2023/2022 三个年份对 4 个金额 + 1 个变动率）；
            # ② 其它列映射问题（表头跨行没拼全等）。
            # 说明：这两类都**不做猜测性列映射**——同一指标同期两个值宁可不要，
            # 也不要挑一个；同样的四个指标都能从审计过的三张报表里取到（三一/洋河/京蓝实测）。
            _ratio_or_variant = bool(
                re.search(r"调整[前后]|新准则|原准则", " ".join(
                    lines[j] for j in range(max(0, (hidx if hidx >= 0 else i) - 6), i + 1)))
                or re.search(r"增减|变动|\(%\)|（%）", " ".join(
                    lines[j] for j in range(max(0, (hidx if hidx >= 0 else i) - 6), i + 1))))
            _reject(slug, "adjustment_variants_or_ratio_column" if _ratio_or_variant
                    else "column_mismatch", i,
                    f"金额多于表头列数（{len(vals)} > {len(years)}）：不截断、不猜列归属，"
                    f"按拒绝处理（表头 {years}）")
            i += 1
            continue
        page = page_of(doc, line_offset(doc, lines, i))
        for col, (label, raw_v) in enumerate(zip(labels, vals)):
            # 列头 → 完整期间（L0-a，复核 F2）：期初列（`2024-01-01`/`年初余额`）属于
            # **上一年末**，不能占本年这一格；解析不出期间就拒绝该单元格，不截年、不猜。
            colp = _column_period(str(label), statement_kind=kind)
            if colp is None:
                _reject(slug, "column_period_unparsed", i,
                        f"列头「{label}」解析不出完整期间：拒绝该单元格（不截年）")
                continue
            period = str(colp["period"])
            if want_periods and not any(period.startswith(p[:4]) for p in want_periods):
                continue
            facts.append({
                "metric": slug, "period": period,
                "value": float(raw_v * scale), "unit": "元",
                "currency": currency, "caliber": caliber,
                "unit_source": unit_src,
                "currency_source": cur_src,
                "caliber_source": f"表名「{table_name}」",
                "period_source": (f"列头「{label}」" if not header["end_start"] else
                                  f"列头「期末/期初」+ 报表日期行 → {label}"),
                # 存量/流量与完整期间一起进事实层（K0-b / L0-a）：表头写的是余额还是
                # 发生额、是期末还是期初，读者要能看见；完整日期不截成年份。
                "period_kind": str(colp.get("period_kind") or (
                    (header.get("kinds") or [])[col]
                    if col < len(header.get("kinds") or [])
                    else ("stock" if re.search(r"余额|年末|年初|期末|期初", str(label))
                          else "flow"))),
                "period_start": str(colp.get("period_start") or ""),
                "period_end": str(colp.get("period_end") or ""),
                "period_label": str(label),
                "period_opening": bool(colp.get("opening")),
                "period_evidence": str(colp.get("why") or ""),
                "header_source": header["source"],
                "entity": company, "entity_id": company_code, "market": "cn",
                "entity_state": entity_state,
                "period_type": "年报", "table": kind, "table_name": table_name,
                "source_url": str(doc.get("url") or ""), "source_hash": text_hash,
                "extracted_by": "annual_financial_tables",
                "locator": (f"PDF 第 {page} 页 · {table_name} · 行「{lines[i][:40]}」"
                            + ("（标签折行，金额在下一行）" if value_line != i else "")),
                "quote": f"{lines[i][:160]}" + (f" ⏎ {lines[value_line][:80]}"
                                                if value_line != i else ""),
                "fact_id": _fx.make_fact_id(company_code, company, slug, period, caliber),
            })
        tables.append({"line": i, "header": years, "unit": unit, "metric": slug,
                       "table": kind, "page": page, "header_source": header["source"],
                       "value_line": value_line})
        i += 1

    # 同一 (指标, 期间, 口径, 表) 两个不同值 → 该项整体不入账（不按先后择一）
    # **身份不等价 ≠ 重复**（L0-a，复核 F4）：仅"数值相同"不能当同一条观察——
    # 币种/单位/主体不同的两行是两条不同观察，必须一起判冲突，不能静默丢掉后一条。
    seen: dict[tuple, tuple] = {}
    kept: list[dict] = []
    dropped: set = set()
    for f in facts:
        k = (f["metric"], f["period"], f["caliber"], f["table"])
        ident = (Decimal(str(f["value"])), str(f.get("unit") or ""),
                 str(f.get("currency") or ""), str(f.get("entity_id") or ""))
        if k in dropped:
            continue
        prev = seen.get(k)
        if prev is None:
            seen[k] = ident
            kept.append(f)
        elif prev != ident:
            dropped.add(k)
            kept = [x for x in kept if (x["metric"], x["period"], x["caliber"],
                                        x["table"]) != k]
            rejected.append({"metric": f["metric"], "reason": "conflicting",
                             "page": 0, "label": f["locator"],
                             "detail": f"{f['table']} {f['period']}：同表内已有 "
                                       f"{prev[0]}{prev[1]}（{prev[2]}/{prev[3]}），"
                                       f"又见 {ident[0]}{ident[1]}（{ident[2]}/{ident[3]}）"
                                       "——身份不等价即冲突，不按先后择一，该项整体不入账"})
        # ident 相同 = 真正的重复行，静默合并（保留第一条）
    return {"ok": bool(kept), "facts": kept, "rejected": rejected, "tables": tables,
            "text_hash": text_hash, "entity_state": entity_state, "lines": len(lines)}


def derive_gross_profit(facts: list[dict], rejected: list | None = None) -> list[dict]:
    """毛利 = 营业收入 − 营业成本：只在**父观察唯一、可用、范围相容**时派生。

    旧版不检查主体与币种（A 公司 CNY 元收入减 B 公司 USD 万元成本也照样出毛利），
    也不带稳定 `fact_id` 与血缘；更危险的是**冲突向派生不传播**（K0-b 反例）：
    两张表各写收入 100/110、成本 60 时，字典取末值算出毛利 50 并留成 ok，读者看到的是
    一个"算出来的数"，而它的父值本身还没消歧。现在：

    - 父（营业收入 / 营业成本）必须**同键唯一或值一致**——同一 (指标, 期间, 口径) 出现
      互不相容的值就**不派生**，并把原因记进 `rejected`（冲突向派生传播）；
    - 父必须带 `fact_id`，且主体/币种/单位一致（范围相容）。
    """
    groups: dict[tuple, list[dict]] = {}
    for f in facts:
        groups.setdefault((f["metric"], f["period"], f.get("caliber") or ""), []).append(f)

    def _parent(metric: str, period: str, caliber: str) -> dict | None:
        items = groups.get((metric, period, caliber)) or []
        if not items:
            return None
        if len(items) > 1:
            # 冲突判据是**完整身份**（值+单位+币种+主体），不是数值（L0-a/复核 F4）：
            # "收入 100 CNY" 与 "收入 100 USD" 数值相同却是两条不同观察 → 冲突，
            # 不许挑一条当父（此前只比数值 → 挑中 CNY，派生出 ok 的毛利 40）。
            idents = {(Decimal(str(x["value"])), str(x.get("unit") or ""),
                       str(x.get("currency") or ""), str(x.get("entity_id") or ""))
                      for x in items}
            if len(idents) > 1:
                return None                     # 冲突：不择一、不择末，直接不派生
        return items[0]

    out: list[dict] = []
    for (metric, period, caliber) in list(groups):
        if metric != "revenue":
            continue
        rev = _parent("revenue", period, caliber)
        cost = _parent("operating_cost", period, caliber)
        if not rev or not cost:
            if rev is not None or cost is not None:
                _miss = "operating_cost" if rev is not None else "revenue"
                if rejected is not None:
                    rejected.append({
                        "metric": "gross_profit", "reason": "derivation_input_conflict",
                        "page": 0, "label": f"{period} {caliber}",
                        "detail": (f"不派生毛利：{_miss} 在该 (期间,口径) 下没有可用观察，"
                                   "或同一键出现互不相容的值（冲突不向派生放行）")})
            continue
        if any(str(rev.get(k) or "") != str(cost.get(k) or "")
               for k in ("entity_id", "currency", "unit")):
            if rejected is not None:
                rejected.append({
                    "metric": "gross_profit", "reason": "derivation_scope_mismatch",
                    "page": 0, "label": f"{period} {caliber}",
                    "detail": "不派生毛利：营业收入与营业成本的主体/币种/单位不一致"})
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
    # 派生要**看见**拒绝原因（父冲突/范围不符）：把 rejections 传进去一起记录
    rows = list(raw["facts"]) + derive_gross_profit(raw["facts"], raw["rejected"])
    ds = fa.freeze_from_facts(
        rows, entity=company, entity_id=company_code, market="cn", periods=periods,
        as_of=as_of, source_label=f"annual_financial_tables:{raw['text_hash'][:12]}",
        restatement=restatement,
        required_metrics=("revenue", "net_profit", "gross_profit", "operating_cashflow"))
    return ds, raw
