# -*- coding: utf-8 -*-
"""MD&A 经营明细表的**定向**抽取（U1，2026-10-01）。

为什么单列一小块：`annual_financial_tables` 的纪律是"只认可识别**报表**，附注/季度摘要一律不取"，
而分产品/分地区/实物销量这三张表在**管理层讨论与分析**里（不是报表）。本模块只做这三张表，
且**必须**表头形状对得上才解析——形状不对就拒绝，绝不猜：

| 表 | 识别依据（表头） | 产出 |
|---|---|---|
| 主营业务分行业/产品/地区/销售模式 | 表头含「占营业收入比重」，行含 5 个数字（本期金额/占比/上期金额/占比/同比） | `revenue` × 两期（**两期都是披露绝对值**） |
| 占营业收入或营业利润 10% 以上 | 表头同时含「营业成本」与「毛利率」，行含 7 个数字 | `revenue`/`operating_cost` 本期绝对值；上期按**披露同比**反推并标 `derived_from` |
| 实物销售 | 表头含「销售量」「单位」，行是「销售量 吨 本期 上期 同比」 | `sales_volume` × 两期（两期都是披露绝对值） |

口径用现有 `caliber` 表达（`分产品:白酒`、`分地区:省外`…）：**不新增维度层**，冻结/算子/底稿
都不用改。上期成本没有绝对披露，只能由本期与同比反推——它是**推算值**，必须在事实里带上
`derived_from` 与 `formula_version`，报告才能如实标"含推算输入"。
"""
from __future__ import annotations

import hashlib
import re

from .annual_financial_tables import (line_offset, norm_lines, page_of,
                                       parse_number)

SECTIONS = ("分行业", "分产品", "分地区", "分销售模式")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# 表头特征
_HDR_PCT = "占营业收入比"          # 真实版面里"重"常被折到下一行，故用前缀
_HDR_COST_MARGIN = ("营业成本", "毛利率")
# 行首的百分比（用于校验"这一行确实是占比表"）
_PCT_RE = re.compile(r"^-?\d+(?:\.\d+)?%$")


def _numbers(line: str) -> list:
    out = []
    for tok in re.findall(r"[（(]?-?\d[\d,，]*(?:\.\d+)?[）)]?\s*%?", str(line)):
        is_pct = tok.rstrip().endswith("%")
        val = parse_number(tok.rstrip().rstrip("%"))
        if val is None:
            continue
        out.append((val, is_pct))
    return out


def _years_above(lines: list[str], idx: int, *, window: int = 40) -> list[str]:
    """在该表上方找**年份表头**；找不到或不是两个年份就返回空（不猜期间）。"""
    for j in range(idx, max(-1, idx - window), -1):
        ys = _YEAR_RE.findall(lines[j])
        if len(ys) >= 2:
            uniq = []
            for y in ys:
                if y not in uniq:
                    uniq.append(y)
            if len(uniq) == 2:
                return sorted(uniq)
            if len(uniq) > 2:
                return []
    return []


def _strip(text: str) -> str:
    """去掉小节编号/行项目标记（`(1). `、`1、`、`其中：`），只留标签本体。"""
    s = str(text or "").strip()
    s = re.sub(r"^[（(]?\d+[）)]?\s*[、.．]?\s*", "", s)
    return re.sub(r"^(?:其中|加|减|其他|其它)\s*[:：]\s*", "", s)


def unit_line_scale(lines: list[str], idx: int, *, window: int = 40) -> tuple[str, float]:
    """向上找最近的 `单位：X` 行 → `(单位原文, 缩放)`；找不到返回 `("", 0.0)`（不猜）。

    三一那版分段表给的是**本期绝对值 + 同比**，金额单位写在表头前的 `单位：千元 币种：人民币`，
    与报表同一套换算（千元→元）。单位取不到就不取数（宁可拒绝，也不把千元当元）。
    """
    for j in range(idx, max(-1, idx - window), -1):
        m = re.search(r"单位\s*[:：]\s*(元|万元|千元|百万元)", lines[j])
        if m:
            unit = m.group(1)
            return unit, _SCALE.get(unit, 0.0)
    return "", 0.0


def _label_and_nums(line: str, want: int) -> tuple[str, list]:
    parts = str(line).split()
    if len(parts) < 2:
        return "", []
    nums = _numbers(" ".join(parts[1:]))
    if len(nums) != want:
        return "", []
    return parts[0], nums


_UNIT_TOKENS = ("吨", "千升", "万元", "元")
# 金额单位 → 换算到元（与 annual_financial_tables 同一套口径）
_SCALE = {"元": 1.0, "千元": 1000.0, "万元": 10000.0, "百万元": 1000000.0}
# 「分行业 营业收入 营业成本 毛利率」这种**小节名与表头同行**的写法（三一/CSCR 常见）
_SECTION_HEADER_RE = re.compile(
    r"^(分行业|分产品|分地区|分销售模式)\s+营业收入\s+营业成本\s+毛利率")


def _unit_of(tokens: list[str]) -> str:
    """`销售量 吨 139,076.05 …` / `销售量(吨) 139,076.05 …` 两种写法都认。"""
    for tok in tokens[1:3]:
        t = str(tok).strip("()（）")
        if t in _UNIT_TOKENS:
            return t
    return "吨"


def _fact(*, doc: dict, text_hash: str, entity: str, entity_id: str, metric: str,
          period: str, value: float, unit: str, caliber: str, idx: int,
          line: str, period_kind: str = "flow", derived_from=(),
          formula_version: str = "", label: str = "", page: int = 0) -> dict:
    return {
        "fact_id": f"opdetail-{metric}-{period}-{caliber}-{idx}",
        "entity": entity, "entity_id": entity_id,
        "metric": metric, "metric_label": label or metric,
        "period": period, "period_kind": period_kind,
        "period_label": f"{period}（MD&A 经营明细表）",
        "currency": "CNY", "unit": unit, "caliber": caliber,
        "value": value,
        "page": page,
        "line": idx, "source_line": str(line)[:120], "quote": str(line)[:120],
        "source_hash": text_hash,
        "verify_state": "verified",
        "unit_source": f"MD&A 经营明细表表头（{unit}）",
        "caliber_source": f"MD&A 经营明细表分段标签（{caliber}）",
        "derived_from": tuple(derived_from),
        "formula_version": formula_version,
        "source_locator": f"年报 MD&A 经营明细表 第 {page} 页 第 {idx} 行：{str(line)[:60]}",
    }


def extract_operating_detail(doc: dict, *, company: str = "", company_code: str = "",
                             periods=()) -> dict:
    """→ `{ok, facts, rejected, text_hash}`；只有表头形状对得上才产出事实。"""
    lines = norm_lines(doc.get("text"))
    text_hash = hashlib.sha256(str(doc.get("text") or "").encode("utf-8")).hexdigest()
    want = [str(p) for p in (periods or ())]
    # 期间参数可能写成 `2023` 或 `2023年`（契约里两种都有）：统一按**年份数字**比。
    want_years = {y for p in want for y in _YEAR_RE.findall(p)}
    facts: list[dict] = []
    pct_facts: list[dict] = []          # 占比表：**分摊全部营业收入**，没有成本
    cost_facts: list[dict] = []         # 10% 以上表：**主营业务**收入+成本（同源成对）
    rejected: list[dict] = []
    seen_value: dict[int, dict] = {}    # 桶 → {(metric, period, caliber): (值, 是否推算, 位置)}
    duplicates: list[str] = []

    def _push(fact: dict, *, bucket: list | None = None) -> None:
        """同一 (指标, 期间, 口径) 的去重与**证据优先级**：

        - 同值重复只留一条（实机：实物销售表在两处披露同一数字；两份都进数据集会让
          "同口径多条观察"被判成口径不明，量价分解就拿不到数）；
        - **披露绝对值优先于推算值**：10% 以上表用同比反推的上期收入与占比表披露的
          绝对值会差几十万到几百万（同比经过四舍五入），两条都留会被判冲突而整项不可用；
        - 值不同的两条披露绝对值照旧都留，让冲突显式暴露（不任选）。"""
        key = (fact["metric"], fact["period"], fact["caliber"])
        is_derived = bool(fact.get("derived_from"))
        target = bucket if bucket is not None else facts
        # 去重与优先级**按桶**判：占比表与 10% 以上表是两套口径，跨桶比较会把
        # "10% 表里与成本同源的那条上期收入"误判成"推算值不许覆盖绝对值"而丢掉，
        # 结果收入取占比表、成本取 10% 表（跨口径配对，实测省内含差额 +2.8 亿）。
        seen = seen_value.setdefault(id(target), {})
        prev = seen.get(key)
        if prev is not None:
            old_value, old_derived, pos = prev
            if float(old_value) == float(fact["value"]):
                duplicates.append(f"{key[0]} {key[1]} {key[2]}（同一披露重复，已去重）")
                return
            if is_derived and not old_derived:
                duplicates.append(f"{key[0]} {key[1]} {key[2]}（推算值不覆盖披露绝对值）")
                return
            if old_derived and not is_derived:
                target[pos] = fact          # 换成披露绝对值，血缘不再标推算
                seen[key] = (float(fact["value"]), False, pos)
                duplicates.append(f"{key[0]} {key[1]} {key[2]}（披露绝对值替换了推算值）")
                return
        seen[key] = (float(fact["value"]), is_derived, len(target))
        target.append(fact)

    def _page(idx: int) -> int:
        try:
            return page_of(doc, line_offset(doc, lines, idx))
        except Exception:                             # noqa: BLE001 - 页码取不到就记 0
            return 0

    def _rej(reason: str, idx: int, detail: str) -> None:
        rejected.append({"metric": "*", "reason": reason, "line": idx,
                         "label": lines[idx][:60] if 0 <= idx < len(lines) else "",
                         "detail": detail})

    def _period_pair(idx: int) -> tuple[str, str] | None:
        ys = _years_above(lines, idx)
        if len(ys) == 2:
            pair = (f"{ys[0]}年", f"{ys[1]}年")
            if want_years and not {ys[0], ys[1]} <= want_years:
                _rej("period_not_requested", idx,
                     f"表头年份 {pair} 不在请求期间 {want}（不穿越历史截止）")
                return None
            return pair
        # 表头只写"比上年增减"（三一/部分 CSCR 版面：列说明可能在表头**下方**几行）
        # → 用请求期间映射：本期=最新一期
        for j in list(range(idx, max(-1, idx - 20), -1)) + list(
                range(idx + 1, min(len(lines), idx + 12))):
            ln = lines[j]
            if "比上年增减" in ln or "比上年同期" in ln or (
                    "本期" in ln and "上期" in ln):
                yrs = sorted({y for p in want for y in _YEAR_RE.findall(p)})
                if len(yrs) == 2:
                    return (f"{yrs[0]}年", f"{yrs[1]}年")
                break
        _rej("no_year_header", idx, "该表上方找不到两个年份（不猜期间）")
        return None

    section_of: dict[str, str] = {}
    seen_pct_table: set[str] = set()
    seen_cost_table: set[str] = set()
    volume_product = ""

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        # ① 占比表（两期收入绝对值）
        if _HDR_PCT in line:
            pair = _period_pair(i)
            if pair:
                prev_p, cur_p = pair
                j, section = i + 1, ""
                while j < len(lines):
                    ln = lines[j].strip()
                    if not ln:
                        j += 1
                        continue
                    if ln in SECTIONS:
                        section = ln
                        seen_pct_table.add(f"{section}@{prev_p}")
                        j += 1
                        continue
                    if _HDR_PCT in ln or (_HDR_COST_MARGIN[0] in ln and _HDR_COST_MARGIN[1] in ln):
                        break
                    if "（2）" in ln or "(2)" in ln:
                        break
                    label, nums = _label_and_nums(ln, 5)
                    if label and section and label != "营业收入合计":
                        r_cur, pct_cur, r_prev, pct_prev, _yoy = nums
                        if pct_cur[1] and pct_prev[1]:
                            for metric, per, val in (("revenue", cur_p, r_cur[0]),
                                                     ("revenue", prev_p, r_prev[0])):
                                _push(_fact(
                                    doc=doc, text_hash=text_hash, entity=company,
                                    entity_id=company_code, metric=metric, period=per,
                                    value=float(val), unit="元",
                                    caliber=f"{section}:{label}", idx=j, line=ln,
                                    label="主营业务收入", page=_page(j)),
                                          bucket=pct_facts)
                    j += 1
                i = j
                continue
        # ② 10% 以上表（本期收入/成本绝对值 + 同比）。**排除**"小节名与表头同行"的写法：
        #    三一那版 `分产品 营业收入 营业成本 毛利率` 会先命中这里，但它的百分比列**不带 %**
        #    （`26.63` 而不是 `26.63%`），按本分支的百分比校验会被整行跳过 → 交给 ④ 处理。
        if (all(k in line for k in _HDR_COST_MARGIN) and "营业收入" in line
                and not _SECTION_HEADER_RE.match(_strip(line))):
            pair = _period_pair(i)
            if pair:
                cur_p = pair[1]
                j, section = i + 1, ""
                while j < len(lines):
                    ln = lines[j].strip()
                    if not ln:
                        j += 1
                        continue
                    if ln in SECTIONS:
                        section = ln
                        j += 1
                        continue
                    if _HDR_COST_MARGIN[0] in ln and _HDR_COST_MARGIN[1] in ln:
                        break
                    if "（3）" in ln or "(3)" in ln:
                        break
                    label, nums = _label_and_nums(ln, 6)   # 收入/成本/毛利率/收入同比/成本同比/毛利率同比
                    if label and section:
                        r_cur, c_cur, m_cur, yoy_r, yoy_c, _yoy_m = nums
                        if m_cur[1] and yoy_r[1] and yoy_c[1]:
                            cal = f"{section}:{label}"
                            if f"{section}@{cur_p}" not in seen_cost_table:
                                seen_cost_table.add(f"{section}@{cur_p}")
                            cur_ids = []
                            for metric, val in (("revenue", r_cur[0]),
                                                ("operating_cost", c_cur[0])):
                                f_ = _fact(doc=doc, text_hash=text_hash, entity=company,
                                           entity_id=company_code, metric=metric,
                                           period=cur_p, value=float(val), unit="元",
                                           caliber=cal, idx=j, line=ln, label="主营业务",
                                           page=_page(j))
                                _push(f_, bucket=cost_facts)
                                cur_ids.append(f_["fact_id"])
                            # 上期：按披露同比反推（推算值，必须带血缘与公式版本）
                            for metric, val_cur, yoy in (("revenue", r_cur[0], yoy_r[0]),
                                                         ("operating_cost", c_cur[0], yoy_c[0])):
                                base = 1 + float(yoy) / 100.0
                                if base <= 0:
                                    _rej("yoy_not_invertible", j,
                                         f"{metric} 同比 {yoy}% 无法反推上期值")
                                    continue
                                _push(_fact(
                                    doc=doc, text_hash=text_hash, entity=company,
                                    entity_id=company_code, metric=metric,
                                    period=pair[0], value=float(val_cur) / base,
                                    unit="元", caliber=cal, idx=j, line=ln, label="主营业务（上期由同比推算）",
                                    page=_page(j),
                                    derived_from=tuple(cur_ids),
                                    formula_version="yoy_inverse/1.0"),
                                      bucket=cost_facts)
                    j += 1
                i = j
                continue
        # ④ 「小节名+表头同行」型：`分产品 营业收入 营业成本 毛利率` + 「本期绝对值 + 同比」行
        #    （三一/CSCR 常见；金额单位写在表头前的 `单位：千元`）。上期按披露同比反推。
        m_hdr = _SECTION_HEADER_RE.match(_strip(line))
        if m_hdr:
            section = m_hdr.group(1)
            unit_src, scale = unit_line_scale(lines, i)
            if not scale:
                _rej("unit_unknown", i, "该分段表上方找不到金额单位（千元/万元/元）：不取")
                i += 1
                continue
            pair = _period_pair(i)
            if not pair:
                i += 1
                continue
            prev_p, cur_p = pair
            j = i + 1
            while j < len(lines):
                ln = lines[j].strip()
                if not ln or _SECTION_HEADER_RE.match(_strip(ln)):
                    if ln and _SECTION_HEADER_RE.match(_strip(ln)):
                        break
                    j += 1
                    continue
                if (ln.startswith("主营业务") or ln.startswith("产销量")
                        or re.match(r"^[（(]\d+[）)]", ln)):
                    break
                if unit_line_scale(lines, j, window=3)[1]:
                    j += 1
                    continue
                nums = _numbers(ln)
                label = ln.split()[0] if ln.split() else ""
                if label and len(nums) >= 5 and nums[0][0] is not None:
                    r_cur, c_cur, m_cur, yoy_r, yoy_c = (nums[0][0], nums[1][0],
                                                         nums[2][0], nums[3][0],
                                                         nums[4][0])
                    if not (m_cur and yoy_r and yoy_c and nums[0][1] is False):
                        j += 1
                        continue
                    cal = f"{section}:{label}"
                    cur_ids = []
                    for metric, val in (("revenue", r_cur), ("operating_cost", c_cur)):
                        f_ = _fact(doc=doc, text_hash=text_hash, entity=company,
                                   entity_id=company_code, metric=metric,
                                   period=cur_p, value=float(val) * scale, unit="元",
                                   caliber=cal, idx=j, line=ln, label="主营业务",
                                   page=_page(j))
                        f_["unit_source"] = f"分段表表头单位：{unit_src}（已换算为元）"
                        _push(f_, bucket=cost_facts)
                        cur_ids.append(f_["fact_id"])
                    for metric, val_cur, yoy in (("revenue", r_cur, yoy_r),
                                                 ("operating_cost", c_cur, yoy_c)):
                        if not yoy:
                            continue
                        base = 1 + float(yoy) / 100.0
                        if base <= 0:
                            _rej("yoy_not_invertible", j,
                                 f"{metric} 同比 {yoy}% 无法反推上期值")
                            continue
                        d = _fact(doc=doc, text_hash=text_hash, entity=company,
                                  entity_id=company_code, metric=metric,
                                  period=prev_p, value=float(val_cur) / base * scale,
                                  unit="元", caliber=cal, idx=j, line=ln,
                                  label="主营业务（上期由同比推算）",
                                  page=_page(j), derived_from=tuple(cur_ids),
                                  formula_version="yoy_inverse/1.0")
                        d["unit_source"] = f"分段表表头单位：{unit_src}（已换算为元）"
                        _push(d, bucket=cost_facts)
                j += 1
            i = j
            continue
        # ③ 实物销售表：产品名行 + 「销售量 吨 本期 上期 同比」
        if line in ("白酒", "红酒", "啤酒", "其他酒类"):
            volume_product = line
        if line.startswith("销售量") and volume_product:
            nums = _numbers(line)
            pair = _period_pair(i)
            toks = str(line).split()
            unit = _unit_of(toks)
            if pair and len(nums) >= 2 and nums[0][0] is not None:
                for per, n in ((pair[1], nums[0][0]), (pair[0], nums[1][0])):
                    _push(_fact(
                        doc=doc, text_hash=text_hash, entity=company,
                        entity_id=company_code, metric="sales_volume", period=per,
                        value=float(n), unit=unit, caliber=f"分产品:{volume_product}",
                        idx=i, line=line, label=f"{volume_product}销售量",
                        page=_page(i)))
        i += 1

    # **同一分段的两种披露口径**（占比表 = 分摊全部营业收入；10% 以上表 = 主营业务收入+成本）
    # 数值不同、都属披露绝对值、都真实：取**有成本的那张表**，保证"同一分段收入—成本同源"
    # （否则数据集判冲突 → 该分段整块不可用，实测 6 处冲突全在这里）；
    # 占比表只补它没有的口径（红酒/其他业务等，那些本来就拿不到成本，算子会如实跳过）。
    _cost_keys = {(f["metric"], f["period"], f["caliber"]) for f in cost_facts}
    _kept_pct = []
    for f in pct_facts:
        key = (f["metric"], f["period"], f["caliber"])
        if key in _cost_keys:
            duplicates.append(f"{key[0]} {key[1]} {key[2]}"
                              "（同分段另有含成本的主营业务口径，取后者以保证同源）")
            continue
        _kept_pct.append(f)
    facts = facts + cost_facts + _kept_pct

    ok = bool(facts)
    if not ok:
        rejected.append({"metric": "*", "reason": "no_operating_detail_found", "line": 0,
                         "label": "", "detail": "MD&A 里没有识别出分产品/分地区/实物销售表"
                                                "（表头形状不符即不取）"})
    return {"ok": ok, "facts": facts, "rejected": rejected, "text_hash": text_hash,
            "lines": len(lines), "duplicates": duplicates}
