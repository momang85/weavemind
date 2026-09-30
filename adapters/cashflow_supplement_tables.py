# -*- coding: utf-8 -*-
"""现金流量表**补充资料**（将净利润调节为经营活动现金流量）的定向抽取（U2，2026-10-01）。

为什么单列：这张表在**附注**里（"现金流量表补充资料"），不属于可识别报表，
`annual_financial_tables` 的纪律是"附注一律不取"，于是"利润为什么没变成现金"这个问题
此前只能靠观察比率（现金质量）回答。这里只取这一张表，且**必须**锚点与行标签都对得上：

- 锚点：出现「将净利润调节为经营活动现金流量」；
- 行：按**披露的标签**匹配（含折行：标签与金额常被排在不同行），金额取本期/上期两列；
- **符号照抄披露**（表里写"（增加以“－”号填列）"，披露值就是带符号的调节额），
  语义分组由算子声明，抽取器不改符号、不推断；
- `净利润` 与 `经营活动产生的现金流量净额` 两行不重复出事实（它们已由利润表/现金流量表
  进入数据集），只作**交叉核对**记录，避免同指标两条观察被判冲突。
"""
from __future__ import annotations

import hashlib
import re

from .annual_financial_tables import line_offset, norm_lines, page_of, parse_number
from .operating_detail_tables import unit_line_scale

ANCHOR = "将净利润调节为经营活动现金流量"
STOP_MARKERS = ("不涉及现金收支", "现金及现金等价物净变动", "现金的期末余额",
                "（2）", "(2)")

# 披露标签 → slug（按会计含义分组由算子声明，这里只做标签映射）
LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("asset_impairment_provision", ("资产减值准备",)),
    # 三一那版把这两项单列且用不同措辞：`信用减值损失`（调节表的**加回项**，与利润表的
    # `credit_impairment` 不是同一条事实）、`使用权资产摊销`（洋河写"折旧"）。
    # 不映射这两项时三一的对账差额恰好等于它们之和（实测 2024 差 9.99 亿、2023 差 13.51 亿）。
    ("credit_impairment_provision", ("信用减值损失",)),
    ("depreciation", ("固定资产折旧、油气资产折耗、生产性生物资产折旧",
                      "固定资产折旧、油气资产折耗、生产性生物资产折旧")),
    ("right_of_use_depreciation", ("使用权资产折旧", "使用权资产摊销")),
    ("intangible_amortization", ("无形资产摊销",)),
    ("long_term_prepaid_amortization", ("长期待摊费用摊销",)),
    ("disposal_long_asset_loss", ("处置固定资产、无形资产和其他长期资产的损失",)),
    ("fixed_asset_scrap_loss", ("固定资产报废损失",)),
    ("fair_value_change_loss", ("公允价值变动损失",)),
    ("finance_expense_adjust", ("财务费用",)),
    ("investment_loss", ("投资损失",)),
    ("deferred_tax_asset_decrease", ("递延所得税资产减少",)),
    ("deferred_tax_liability_increase", ("递延所得税负债增加",)),
    ("inventory_decrease", ("存货的减少",)),
    ("operating_receivable_decrease", ("经营性应收项目的减少",)),
    ("operating_payable_increase", ("经营性应付项目的增加",)),
    ("other_cashflow_adjustments", ("其他",)),
)
# 表内这两行已由其它报表进入数据集：只核对，不重复出事实
CROSS_CHECK_LABELS = (("net_profit_consolidated", ("净利润",)),
                      ("operating_cashflow", ("经营活动产生的现金流量净额",)))

_FORM_NOTE = re.compile(r"[（(][^）)]{0,30}号填列[）)][\s]*")
_NUM_TOK = re.compile(r"[（(]?-?\d[\d,，]*(?:\.\d+)?[）)]?")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")


def _strip(text: str) -> str:
    s = str(text or "").strip()
    s = re.sub(r"^(?:其中|加|减|其他|其它)\s*[:：]\s*", "", s)
    s = re.sub(r"^\d+[、.．]\s*", "", s)
    return s


def _nums(line: str) -> list:
    out = []
    for tok in _NUM_TOK.findall(str(line)):
        if tok.rstrip().endswith("%"):
            continue
        val = parse_number(tok)
        if val is not None:
            out.append(val)
    return out


def _match_label(text: str) -> tuple[str, str]:
    """→ `(slug, 剩余)`；最长别名优先，且**剥掉无数字的括号注**再匹配。"""
    s = _strip(text)
    best_slug, best_name = "", ""
    for slug, names in LABELS:
        for n in names:
            if s.startswith(n) and len(n) > len(best_name):
                best_slug, best_name = slug, n
    if not best_name:
        return "", s
    return best_slug, s[len(best_name):]


def extract_cashflow_supplement(doc: dict, *, company: str = "",
                                company_code: str = "", periods=()) -> dict:
    """→ `{ok, facts, cross_checks, rejected, text_hash}`。"""
    lines = norm_lines(doc.get("text"))
    text_hash = hashlib.sha256(str(doc.get("text") or "").encode("utf-8")).hexdigest()
    want_years = {y for p in (periods or ()) for y in _YEAR_RE.findall(str(p))}
    facts: list[dict] = []
    cross: list[dict] = []
    rejected: list[dict] = []

    start = next((i for i, ln in enumerate(lines) if ANCHOR in ln), -1)
    if start < 0:
        return {"ok": False, "facts": [], "cross_checks": [],
                "rejected": [{"metric": "*", "reason": "no_supplement_table",
                              "detail": "材料里没有『将净利润调节为经营活动现金流量』"}],
                "text_hash": text_hash}

    def _pair(idx: int) -> tuple[tuple[str, str], str] | None:
        """期间对照：优先读**表头年份**；表头只写"本期/上期"时用请求期间
        （最新=本期、上一期=上期），并把这条映射的来源如实带回。"""
        for j in range(idx, max(-1, idx - 60), -1):
            ys = _YEAR_RE.findall(lines[j])
            uniq = []
            for y in ys:
                if y not in uniq:
                    uniq.append(y)
            if len(uniq) == 2:
                pair = tuple(sorted(uniq))
                if want_years and not set(pair) <= want_years:
                    return None
                return pair, f"表头年份（第 {j} 行）"
        for j in range(idx, max(-1, idx - 25), -1):
            ln = lines[j]
            if "本期" in ln and "上期" in ln:
                years = sorted({y for p in (periods or ())
                                for y in _YEAR_RE.findall(str(p))})
                if len(years) == 2:
                    return ((years[0], years[1]),
                            f"表头「本期/上期」（第 {j} 行）+ 请求期间映射：本期={years[1]}年")
                return None
        return None

    def _page(idx: int) -> int:
        try:
            return page_of(doc, line_offset(doc, lines, idx))
        except Exception:                       # noqa: BLE001
            return 0

    _pair_res = _pair(start)
    pair, period_source = (_pair_res if _pair_res else (None, ""))
    # 金额单位：三一那版整份报告用"千元"（表头前一行写 `单位：千元 币种：人民币`）。
    # 不读单位就会把千元当元（实测：调节项比经营现金流小三个数量级，对账差额 87 亿）。
    unit_src, scale = unit_line_scale(lines, start)
    if not scale:
        return {"ok": False, "facts": [], "cross_checks": [], "periods": pair,
                "rejected": [{"metric": "*", "reason": "unit_unknown",
                              "detail": "补充资料表上方找不到金额单位（元/千元/万元）：不取"}],
                "text_hash": text_hash}
    i = start + 1
    pending: list[str] = []            # 折行标签的前半段（如"固定资产折旧、油气资产折"）
    while i < len(lines):
        raw = lines[i]
        stripped = _strip(raw)
        if any(m in stripped for m in STOP_MARKERS):
            break
        slug, rest = _match_label(raw)
        if not slug and not _nums(stripped):
            # 这一行既不是指标行、又没有金额：可能是**折行标签**的前半段，先攒着
            pending.append(raw)
            i += 1
            continue
        if not slug and pending:
            # 与前半段拼起来再认（真实版面：'固定资产折旧、油气资产折' + '耗、… 586,592,227.18 …'）
            joined = "".join(pending + [raw])   # 折行发生在词中间：不能插空格
            slug, rest = _match_label(joined)
            if slug:
                raw = joined
                stripped = _strip(raw)
        if not slug:
            # 交叉核对行（净利润 / 经营现金流净额）：已由别的表进数据集，这里只记录
            for metric, names in CROSS_CHECK_LABELS:
                if stripped.startswith(names[0]):
                    nums = _nums(stripped[len(names[0]):])
                    if len(nums) >= 2:
                        cross.append({"metric": metric, "cur": float(nums[0]),
                                      "prev": float(nums[1]), "line": i,
                                      "source_line": stripped[:90]})
                    break
            pending = []
            i += 1
            continue
        # 折行：标签与金额不在同一行时，向后最多 3 行找金额
        nums = _nums(rest)
        j = i
        while len(nums) < 2 and j + 1 < len(lines) and (j - i) < 3:
            nxt = lines[j + 1]
            if any(m in _strip(nxt) for m in STOP_MARKERS):
                break
            extra = _nums(_FORM_NOTE.sub("", nxt))
            if not extra:
                break
            nums = nums + extra
            j += 1
        if len(nums) < 2 or pair is None:
            pending = []
            rejected.append({"metric": slug, "reason": "no_amounts_or_periods",
                             "line": i, "label": stripped[:50],
                             "detail": f"该行取到 {len(nums)} 个金额、期间表头 {pair}"})
            i += 1
            continue
        cur_v, prev_v = float(nums[0]), float(nums[1])
        src_line = " ".join(x.strip() for x in lines[i:j + 1])[:140]
        for per, val in ((f"{pair[1]}年", cur_v), (f"{pair[0]}年", prev_v)):
            facts.append({
                "fact_id": f"cfsupp-{slug}-{per}",
                "entity": company, "entity_id": company_code,
                "metric": slug, "metric_label": stripped[:30],
                "period": per, "period_kind": "flow",
                "period_label": f"{per}（现金流量表补充资料）",
                "currency": "CNY", "unit": "元", "caliber": "合并",
                "value": float(val) * scale, "raw_value": float(val),
                "unit_raw": unit_src, "page": _page(i), "line": i, "source_line": src_line,
                "quote": src_line, "source_hash": text_hash,
                "verify_state": "verified",
                "unit_source": f"现金流量表补充资料表头：{unit_src}（已换算为元）",
                "caliber_source": "合并现金流量表附注（补充资料）",
                "sign_convention": "照抄披露符号（表内已注明“增加/减少以“－”号填列”）",
                "period_source": period_source,
                "source_locator": f"年报现金流量表补充资料 第 {_page(i)} 页 第 {i} 行：{src_line[:60]}",
            })
        pending = []
        i = j + 1
    ok = bool(facts)
    if not ok and not rejected:
        rejected.append({"metric": "*", "reason": "no_supplement_rows",
                         "detail": "锚点找到了，但一行也没取到（标签/金额形状不符）"})
    return {"ok": ok, "facts": facts, "cross_checks": cross,
            "rejected": rejected, "text_hash": text_hash, "periods": pair}
