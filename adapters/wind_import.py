# -*- coding: utf-8 -*-
"""Wind 离线 CSV/JSON 导出预览（标准库，纯读，不连接终端、不落库）。

``preview_file(path, kind="financial"|"market", metadata={}, field_map={})``
接受规范英文或明确列出的中文表头；``field_map`` 的方向是原列名 → 规范字段。
财务宽表展开为现役 ``financial_analysis.dataset.freeze`` 可读的观察字典。
价格、金额、复权因子均保持导出值，不转换单位、不补披露日、不猜合并口径。

预览的 ``ready`` 只表示输入身份/数值就绪，不表示 Wind 许可或人工复核通过。
``analysis_eligible`` 才是调用方的输入筛选信号；缺元数据的候选记录仍保留供补齐。
尤其要保留披露日和重述版本：今天下载的历史值不能自证为当时可见的版本。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = "weavemind.wind_import/1"

# 不做模糊/前缀匹配：总收入、归母净利与合并净利各自保留身份。
FINANCIAL_FIELDS = {
    "revenue": ("营业收入",),
    "total_revenue": ("营业总收入",),
    "operating_cost": ("营业成本",),
    "gross_profit": ("毛利润", "毛利"),
    "operating_profit": ("营业利润",),
    "profit_before_tax": ("利润总额",),
    "net_profit": ("归母净利润", "归属于母公司股东的净利润",
                   "归属于母公司所有者的净利润", "归属于上市公司股东的净利润"),
    "net_profit_consolidated": ("净利润", "合并净利润", "净利润（合并）"),
    "operating_cashflow": ("经营活动产生的现金流量净额", "经营活动现金流量净额",
                           "经营活动现金流净额"),
    "investing_cashflow": ("投资活动产生的现金流量净额",),
    "financing_cashflow": ("筹资活动产生的现金流量净额",),
    "capital_expenditure": ("购建固定资产、无形资产和其他长期资产支付的现金",),
    "cash_received_from_sales": ("销售商品、提供劳务收到的现金",),
    "cash_paid_for_goods": ("购买商品、接受劳务支付的现金",),
    "selling_expense": ("销售费用",),
    "admin_expense": ("管理费用",),
    "finance_expense": ("财务费用",),
    "rd_expense": ("研发费用",),
    "taxes_and_surcharges": ("税金及附加",),
    "income_tax_expense": ("所得税费用",),
    "minority_interest": ("少数股东损益",),
    "other_income": ("其他收益",),
    "investment_income": ("投资收益",),
    "fair_value_change": ("公允价值变动收益",),
    "credit_impairment": ("信用减值损失",),
    "asset_impairment": ("资产减值损失",),
    "asset_disposal_income": ("资产处置收益",),
    "non_operating_income": ("营业外收入",),
    "non_operating_expense": ("营业外支出",),
    "monetary_funds": ("货币资金",),
    "cash_and_equivalents": ("现金及现金等价物",),
    "available_cash": ("可用现金",),
    "interest_bearing_debt_due_within_one_year": ("一年内到期的有息债务",),
    "accounts_receivable": ("应收账款",),
    "notes_receivable": ("应收票据",),
    "inventory": ("存货",),
    "accounts_payable": ("应付账款",),
    "notes_payable": ("应付票据",),
    "contract_liabilities": ("合同负债",),
    "total_assets": ("资产总计", "总资产"),
    "total_liabilities": ("负债合计", "总负债"),
    "equity": ("所有者权益合计", "净资产"),
    "short_term_debt": ("短期借款",),
    "long_term_debt": ("长期借款",),
    "current_portion_of_debt": ("一年内到期的非流动负债",),
    "depreciation": ("固定资产折旧",),
    "intangible_amortization": ("无形资产摊销",),
}
# R3 经营维度行（C2c，2026-10-11）：年报"产量/销量/库存"与"营业收入构成"表的**分产品**行。
# 与三表科目分开列，理由有三：①单位不同（吨 / 元）而本预览器的 `unit` 是**行级**的，
# 混在一行会把吨当元；②分产品是**同一总体的另一种切法**，不是"其他科目"，不能与三表科目
# 混在一个科目集合里；③`dimension`（维度范围）只在分维度行上有意义——公司合计行留空，
# 于是"公司收入 ÷ 白酒销量"这类**范围错配**在数据集层就能被识别，而不是靠标签说明。
# 只登记 C2c 需要且原表确有的行（白酒销量/产量/库存、分产品白酒/红酒/其他、表内合计）；
# 不登记分地区/分销售模式/分行业（那是另两种切法，本批不引入第三个结构项）。
OPERATING_FIELDS = {
    "sales_volume_baijiu": ("白酒销售量（吨）",),
    "production_volume_baijiu": ("白酒生产量（吨）",),
    "inventory_volume_baijiu": ("白酒库存量（吨）",),
    "revenue_product_baijiu": ("分产品·白酒收入",),
    "revenue_product_hongjiu": ("分产品·红酒收入",),
    "revenue_product_other": ("分产品·其他收入",),
    "revenue_total_reported": ("营业收入合计（表内）",),
}
# C2d（2026-10-11，§18.15 接缝①）：现金流量表**补充资料**的调节项行。与三表科目分开列，
# 理由：①它们是**调节表专用口径**（"信用减值损失（调节项）"≠ 利润表的信用减值损失；
# "财务费用（调节项）"≠ 利润表财务费用），与三表同名科目混在一个字段集合里会撞名；
# ②它们全部是**流量**（本期发生额，表头"本期/上期"两列），一行两期，与三表"一行一期"不同。
# 符号照抄披露（表内已注明"增加/减少以'－'号填列"）；这里只登记洋河披露确有的 16 项
# ＋算子要求的 17 项里的另一项（信用减值调节项——洋河本表没有该行，登记是为别的公司留位，
# 投影时缺失就缺行，不造数）。金额单位一律"元"（补充资料表头），unit 行级声明。
CASH_SUPPLEMENT_FIELDS = {
    "asset_impairment_provision": ("补充资料·资产减值准备",),
    "credit_impairment_provision": ("补充资料·信用减值损失（调节项）",),
    # 注：`depreciation`、`intangible_amortization` **不在此重复登记**——它们在
    # `FINANCIAL_FIELDS` 里已有中文表头映射（"固定资产折旧"/"无形资产摊销"），
    # 在此重登记会用补充资料长标签盖掉原映射，破坏既有 Wind 导入。抽取器输出的
    # 就是这两个 slug，所以投影无需重登记。
    "right_of_use_depreciation": ("补充资料·使用权资产折旧/摊销",),
    "long_term_prepaid_amortization": ("补充资料·长期待摊费用摊销",),
    "disposal_long_asset_loss": ("补充资料·处置长期资产的损失",),
    "fixed_asset_scrap_loss": ("补充资料·固定资产报废损失",),
    "fair_value_change_loss": ("补充资料·公允价值变动损失",),
    "finance_expense_adjust": ("补充资料·财务费用（调节项）",),
    "investment_loss": ("补充资料·投资损失（调节项）",),
    "deferred_tax_asset_decrease": ("补充资料·递延所得税资产减少",),
    "deferred_tax_liability_increase": ("补充资料·递延所得税负债增加",),
    "inventory_decrease": ("补充资料·存货的减少（增加以'－'号填列）",),
    "operating_receivable_decrease": ("补充资料·经营性应收项目的减少（增加以'－'号填列）",),
    "operating_payable_increase": ("补充资料·经营性应付项目的增加（减少以'－'号填列）",),
    "other_cashflow_adjustments": ("补充资料·其他（调节项）",),
}
_FINANCIAL_METRICS = {**FINANCIAL_FIELDS, **OPERATING_FIELDS, **CASH_SUPPLEMENT_FIELDS}
STOCK_FIELDS = {
    "monetary_funds", "cash_and_equivalents", "available_cash",
    "interest_bearing_debt_due_within_one_year", "accounts_receivable", "notes_receivable", "inventory",
    "accounts_payable", "notes_payable", "contract_liabilities", "total_assets",
    "total_liabilities", "equity", "short_term_debt", "long_term_debt",
    "current_portion_of_debt",
    # 期末**实物量**库存：时点量，不是流量（C2c：年报"库存量"行；销量/产量仍是流量）。
    "inventory_volume_baijiu",
    # 补充资料调节项**全部是流量**（本期发生额），不进 STOCK_FIELDS。
}
IDENTITY_FIELDS = {
    "code": ("证券代码", "公司代码", "Wind代码", "wind_code", "ts_code"),
    "entity": ("公司名称", "公司名", "证券简称", "名称"),
    "period": ("报告期", "报告期末", "报告期末日期"),
    "disclosed_at": ("披露日期", "公告日期", "公告时间"),
    "available_at": ("可用时间", "可得时间"),
    "retrieved_at": ("下载日期", "下载时间", "采集时间"),
    "version_available_at": ("版本可用时间", "重述版本披露日期"),
    "unit": ("单位", "金额单位"),
    "currency": ("币种", "货币"),
    "caliber": ("报表口径", "合并口径", "报表范围"),
    # C2c：分维度行的**范围**（如 `分产品:白酒` / `公司合计`）。只在分维度行上写；
    # 三表科目行留空，因此不会改变已有行的 fact_id / 观察哈希。
    "dimension": ("维度", "维度范围", "分项范围"),
    "period_type": ("报告类型", "报告期间类型"),
    "period_basis": ("累计/单季类型", "累计或单季", "累计/单季", "期间口径"),
    "period_start": ("期间开始", "期间起始日"),
    "period_end": ("期间结束", "期间截止日"),
    "restatement": ("重述版本", "数据版本", "重述批次"),
    "source": ("数据来源", "来源"),
    "source_url": ("来源链接", "原文链接"),
    "adj_basis": ("复权口径", "复权方式"),
    "volume_unit": ("成交量单位",),
    "amount_unit": ("成交额单位",),
    "suspended": ("停牌", "停牌状态"),
}
MARKET_FIELDS = {
    "date": ("日期", "交易日期", "交易日", "trade_date"),
    "open": ("开盘", "开盘价"),
    "high": ("最高", "最高价"),
    "low": ("最低", "最低价"),
    "close": ("收盘", "收盘价"),
    "volume": ("成交量", "vol"),
    "amount": ("成交额", "成交金额"),
    "adj_factor": ("复权因子",),
    "role": ("标的角色",),
}
_EMPTY = {"", "--", "—", "-", "na", "n/a", "null", "none"}


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _key(value) -> str:
    return _text(value).lstrip("\ufeff").casefold()


def _issue(issues: list, code: str, message: str, *, row=0, field="", severity="warning"):
    issues.append({"code": code, "message": message, "row": row,
                   "field": field, "severity": severity})


def _numeric(value):
    if value is None or isinstance(value, bool):
        return None
    text = _text(value).replace(",", "").replace("，", "")
    if text.casefold() in _EMPTY:
        return None
    # 只识别确切数字；不剥离单位、%、注脚，也不把空白当零。
    try:
        number = Decimal(text)
        out = float(number)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None
    return out if number.is_finite() and math.isfinite(out) else None


def _unit_scale(value):
    # 仅供调用方显式换算，原 value/unit 始终保持不变；不接受模糊包含匹配。
    # C2c：R3 实物量行（吨）也要过同一道元数据完整度闸门，否则"单位无换算系数"会被
    # 记成缺元数据、`analysis_eligible=False`，销量就永远进不了数据集。吨是这里的基准
    # 单位（系数 1），不做任何跨单位换算。
    return {"元": 1, "千元": 1000, "万元": 10000, "百万元": 1000000,
            "亿元": 100000000, "万亿元": 1000000000000,
            "吨": 1, "千吨": 1000, "万吨": 10000}.get(_text(value))


def _date(value, *, timestamp=False) -> str:
    text = _text(value)
    if not text or isinstance(value, bool):
        return ""
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    text = re.sub(r"^(\d{4})年(\d{1,2})月(\d{1,2})日", r"\1-\2-\3", text)
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(.*)$", text)
    if not m:
        return ""
    try:
        day = date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        tail = m[4]
        if tail:
            if not tail.startswith(("T", " ")):
                return ""
            parsed = datetime.fromisoformat(day + tail.replace("Z", "+00:00"))
            return parsed.isoformat() if timestamp else parsed.date().isoformat()
        return day
    except ValueError:
        return ""


def _code(value) -> str:
    code = _text(value).upper()
    # 没有市场后缀时保留未知，不根据号码猜交易所。
    return code if re.fullmatch(r"\d{6}\.(?:SH|SZ|BJ)", code) else ""


def _is_before(first: str, second: str) -> bool:
    """比较时间；两端有时区时按真实时间，否则只判明确早于的日期。"""
    a, b = datetime.fromisoformat(first), datetime.fromisoformat(second)
    if a.tzinfo is not None and b.tzinfo is not None:
        return a < b
    if a.tzinfo is None and b.tzinfo is None:
        return a < b
    return a.date() < b.date()


def _aliases(kind: str) -> dict:
    groups = {**IDENTITY_FIELDS,
              **(_FINANCIAL_METRICS if kind == "financial" else MARKET_FIELDS)}
    return {_key(name): canonical for canonical, names in groups.items()
            for name in (canonical, *names)}


def _canonical_value(field: str, value):
    if field == "code":
        return _code(value)
    if field in ("disclosed_at", "available_at", "retrieved_at", "version_available_at"):
        return _date(value, timestamp=True)
    if field in ("period_start", "period_end", "date"):
        return _date(value)
    if field in _FINANCIAL_METRICS or field in MARKET_FIELDS and field not in ("date", "role"):
        return _numeric(value)
    text = _text(value)
    if field == "caliber":
        return {"合并报表": "合并", "合并": "合并", "consolidated": "合并",
                "母公司报表": "母公司", "母公司": "母公司", "parent": "母公司"}.get(text, "")
    if field == "currency":
        return {"人民币": "CNY", "人民币元": "CNY", "美元": "USD", "港币": "HKD",
                "港元": "HKD"}.get(text, text.upper() if re.fullmatch("[A-Za-z]{3}", text) else "")
    return text


def _mapped_row(raw: dict, aliases: dict, field_map: dict, defaults: dict,
                issues: list, row_no: int) -> tuple[dict, dict, set]:
    columns: dict[str, list] = {}
    extra = {}
    conflicts = set()
    for key, value in raw.items():
        canonical = field_map.get(_key(key)) or aliases.get(_key(key))
        if canonical:
            columns.setdefault(canonical, []).append((str(key), value))
        else:
            extra[str(key)] = value
    out = {}
    for field, pairs in columns.items():
        present = [(name, value) for name, value in pairs
                   if value is not None and _text(value).casefold() not in _EMPTY]
        normalized = [_canonical_value(field, value) for _, value in present]
        if len(present) > 1 and any(value != normalized[0] for value in normalized[1:]):
            conflicts.add(field)
            out[field] = None
            _issue(issues, "conflicting_columns", "多个列映射到同一字段且值不一致："
                   + "、".join(name for name, _ in present), row=row_no, field=field, severity="error")
        elif present:
            out[field] = present[0][1]
        elif field in defaults:
            out[field] = defaults[field]
        else:
            out[field] = None
    # 元数据是显式的行外默认值；文件中已有的非空值优先，不跨公司硬套。
    for field, value in defaults.items():
        if field not in out:
            out[field] = value
    return out, extra, conflicts


def _read_file(blob: bytes, suffix: str, encoding: str, issues: list) -> tuple[list, dict]:
    if suffix not in (".csv", ".json"):
        _issue(issues, "unsupported_format", "本轮仅接受 CSV/JSON；XLSX 请另存为仅值 CSV。", severity="error")
        return [], {}
    try:
        text = blob.decode(encoding or "utf-8-sig")
    except (UnicodeError, LookupError):
        _issue(issues, "encoding_error", "无法按指定编码读取；可显式 metadata.encoding='gb18030'。", severity="error")
        return [], {}
    try:
        if suffix == ".json":
            payload = json.loads(text)
            embedded = payload.get("metadata", {}) if isinstance(payload, dict) else {}
            rows = payload.get("rows", payload.get("records", payload.get("data"))) if isinstance(payload, dict) else payload
            if not isinstance(rows, list) or not isinstance(embedded, dict):
                raise ValueError("JSON 应为行数组，或带 rows/records/data 数组的对象")
            result = []
            for index, row in enumerate(rows, 1):
                if isinstance(row, dict):
                    result.append(row)
                else:
                    _issue(issues, "invalid_row", "JSON 行必须为对象。", row=index, severity="error")
            return result, embedded
        reader = csv.reader(io.StringIO(text, newline=""))
        header = next(reader, [])
        if not header:
            return [], {}
        if len({_key(x) for x in header}) != len(header):
            _issue(issues, "duplicate_header", "CSV 有重复列名；请明确区分后再导入，避免覆盖值。", severity="error")
            return [], {}
        result = []
        for index, values in enumerate(reader, 1):
            if not values or all(not v.strip() for v in values):
                continue
            if len(values) != len(header):
                _issue(issues, "row_width", "数据行列数与表头不一致。", row=index, severity="error")
                continue
            result.append(dict(zip(header, values)))
        return result, {}
    except (ValueError, csv.Error) as exc:
        _issue(issues, "parse_error", str(exc)[:160], severity="error")
        return [], {}


def _period_identity(row: dict, issues: list, row_no: int) -> dict:
    label = _text(row.get("period"))
    type_text = _text(row.get("period_type"))
    basis_text = _text(row.get("period_basis"))
    period_type = {"年报": "annual", "年度": "annual", "annual": "annual",
                   "半年报": "half_year", "中报": "half_year", "半年度": "half_year",
                   "half_year": "half_year", "季报": "quarterly", "季度": "quarterly",
                   "quarterly": "quarterly"}.get(type_text, "")
    basis = {"累计": "accumulated", "年度累计": "accumulated", "年初至今": "accumulated",
             "accumulated": "accumulated", "单季": "single_quarter", "单季度": "single_quarter",
             "single_quarter": "single_quarter", "时点": "point_in_time",
             "point_in_time": "point_in_time"}.get(basis_text, "")
    start = _date(row.get("period_start"))
    end = _date(row.get("period_end"))
    for key, parsed in (("period_start", start), ("period_end", end)):
        if _text(row.get(key)) and not parsed:
            _issue(issues, "invalid_date", "期间日期无效。", row=row_no, field=key, severity="error")
    raw_end = _date(label)
    half_year_label = re.fullmatch(r"(\d{4})年(?:上半年|半年度)", label)
    if half_year_label and period_type == "half_year" and basis == "accumulated":
        raw_end = f"{half_year_label[1]}-06-30"
    if raw_end and end and raw_end != end:
        _issue(issues, "period_conflict", "报告期末与期间截止日不一致。", row=row_no, field="period", severity="error")
        return {"period": label, "period_label": label, "period_type": "", "period_basis": basis,
                "period_start": "", "period_end": ""}
    end = end or raw_end
    # 首期 A 股日历年度报表：明确“年度/年报/annual”且期末 12/31，
    # 其流量区间就是本年 1/1—12/31，不要求导出者再重复填写累计类型。
    # 季度没有这个兜底：仍需明确累计还是单季，避免把 Q4 当全年。
    if period_type == "annual" and not basis_text and end.endswith("12-31"):
        basis = "accumulated"
        start = start or f"{end[:4]}-01-01"
    year_label = re.fullmatch(r"(\d{4})(?:年)?", label)
    if year_label and period_type == "annual" and basis == "accumulated":
        year = int(year_label[1])
        if not 1900 <= year <= 2199 or end and end[:4] != str(year):
            _issue(issues, "period_conflict", "年度标签无效或与期间截止日不一致。", row=row_no, field="period", severity="error")
            return {"period": label, "period_label": label, "period_type": "", "period_basis": basis,
                    "period_start": "", "period_end": ""}
        end = end or f"{year:04d}-12-31"
        start = start or f"{year:04d}-01-01"
    if end and basis == "accumulated":
        start = start or f"{end[:4]}-01-01"
        if end.endswith("12-31"):
            inferred_type, output_label = "annual", f"{end[:4]}年"
        elif end.endswith("06-30"):
            inferred_type, output_label = "half_year", f"{end[:4]}年上半年"
        else:
            inferred_type, output_label = "quarterly", f"{end}累计"
        if period_type == "quarterly":
            label = f"{end}累计"
        elif period_type and period_type != inferred_type:
            _issue(issues, "period_conflict", "报告类型与累计期间截止日不一致。", row=row_no, field="period_type", severity="error")
            period_type = ""
        else:
            period_type = period_type or inferred_type
            label = output_label
    elif end and basis == "single_quarter":
        quarter_ends = {"03-31": ("01-01", 1), "06-30": ("04-01", 2),
                        "09-30": ("07-01", 3), "12-31": ("10-01", 4)}
        if end[5:] in quarter_ends:
            month_day, quarter = quarter_ends[end[5:]]
            start = start or f"{end[:4]}-{month_day}"
            period_type = "quarterly"
            label = f"{end[:4]}年Q{quarter}（单季）"
        else:
            _issue(issues, "period_conflict", "单季度报告期末不是日历季度末。", row=row_no, field="period", severity="error")
    if start and end and start > end:
        _issue(issues, "period_conflict", "期间开始日晚于截止日。", row=row_no, field="period_start", severity="error")
        start = ""
    return {"period": label, "period_label": _text(row.get("period")), "period_type": period_type,
            "period_basis": basis, "period_start": start, "period_end": end}


def _identity(row: dict, raw_hash: str, issues: list, row_no: int) -> dict:
    code = _code(row.get("code"))
    if not code:
        _issue(issues, "invalid_code", "需完整 Wind 证券代码（如 002304.SZ）；不猜交易所。", row=row_no, field="code", severity="error")
    dates = {}
    for field in ("disclosed_at", "available_at", "retrieved_at", "version_available_at"):
        dates[field] = _date(row.get(field), timestamp=True)
        if _text(row.get(field)) and not dates[field]:
            _issue(issues, "invalid_date", "日期/时间无效。", row=row_no, field=field, severity="error")
    # 已明确披露日即最早公开可用日；不是把报告期末/下载日当披露日。
    if not dates["available_at"] and dates["disclosed_at"] and not _text(row.get("available_at")):
        dates["available_at"] = dates["disclosed_at"]
    if dates["available_at"] and dates["disclosed_at"] and _is_before(dates["available_at"], dates["disclosed_at"]):
        _issue(issues, "availability_before_disclosure", "可用时间早于披露时间，不能用于当时信息集。", row=row_no, field="available_at", severity="error")
        dates["available_at"] = ""
    return {"entity_id": code, "entity": _text(row.get("entity")), "market": code.rsplit(".", 1)[-1] if code else "",
            "currency": _canonical_value("currency", row.get("currency")),
            "unit": _text(row.get("unit")), "unit_scale": _unit_scale(row.get("unit")),
            "caliber": _canonical_value("caliber", row.get("caliber")),
            "dimension": _text(row.get("dimension")),
            "restatement": _text(row.get("restatement")), "source_hash": raw_hash,
            "source": _text(row.get("source")) or "Wind离线导出",
            "source_url": _text(row.get("source_url")), **dates}


def _financial_records(row, raw, extra, raw_hash, issues, row_no):
    identity = _identity(row, raw_hash, issues, row_no)
    period = _period_identity(row, issues, row_no)
    missing = [field for field in ("entity_id", "currency", "unit", "caliber", "disclosed_at", "available_at")
               if not identity[field]]
    missing.extend(field for field in ("period", "period_type", "period_end") if not period[field])
    if identity["unit"] and identity["unit_scale"] is None:
        missing.append("unit_scale")
    if missing:
        _issue(issues, "missing_identity", "缺失明确身份：" + "、".join(missing), row=row_no, field=",".join(missing))
    if not identity["retrieved_at"]:
        _issue(issues, "missing_retrieved_at", "下载/采集时间未知；请保留导出记录。", row=row_no, field="retrieved_at")
    records = []
    for metric in _FINANCIAL_METRICS:
        if metric not in row:
            continue
        value = _numeric(row.get(metric))
        if value is None:
            _issue(issues, "missing_or_invalid_number", "数值缺失或无效；保留候选，不充零。", row=row_no, field=metric)
        stock = metric in STOCK_FIELDS
        metric_period = {**period, "period_start": "" if stock else period["period_start"]}
        metric_missing = [] if stock or metric_period["period_start"] else ["period_start"]
        if metric_missing:
            _issue(issues, "unknown_flow_span", "流量期间起始日未知，不能按全年分析。", row=row_no, field=metric)
        seed = {"entity_id": identity["entity_id"], "metric": metric, "period": metric_period,
                "caliber": identity["caliber"], "unit": identity["unit"], "currency": identity["currency"],
                "restatement": identity["restatement"]}
        # 只在**真的**带维度时才进种子：三表科目行的 fact_id 与加维度前逐位一致。
        if identity.get("dimension"):
            seed["dimension"] = identity["dimension"]
        fact_id = "wind:" + hashlib.sha256(json.dumps(seed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        records.append({"metric": metric, "metric_label": _FINANCIAL_METRICS[metric][0], "fact_id": fact_id,
                        "value": value, "period_kind": "stock" if stock else "flow", **identity,
                        **metric_period, "as_of": identity["available_at"], "verify_state": "unverified",
                        "analysis_eligible": value is not None and not missing and not metric_missing,
                        "source_row": row_no, "sidecar": {"raw_fields": raw, "unmapped_fields": extra}})
    # 唯一派生项：明确同一行同口径的营业收入减营业成本；原毛利存在但缺值时不覆盖它。
    by_metric = {record["metric"]: record for record in records}
    if "gross_profit" not in row and "revenue" in by_metric and "operating_cost" in by_metric:
        revenue, cost = by_metric["revenue"], by_metric["operating_cost"]
        if revenue["analysis_eligible"] and cost["analysis_eligible"]:
            seed = {"entity_id": identity["entity_id"], "metric": "gross_profit", "period": period,
                    "caliber": identity["caliber"], "unit": identity["unit"], "currency": identity["currency"],
                    "restatement": identity["restatement"]}
            gross_value = _numeric(Decimal(str(revenue["value"])) - Decimal(str(cost["value"])))
            if gross_value is None:
                _issue(issues, "invalid_derived_number", "收入−成本结果超出可用数值范围。", row=row_no, field="gross_profit", severity="error")
            records.append({**revenue, "metric": "gross_profit", "metric_label": "毛利润（收入−成本）",
                            "fact_id": "wind:" + hashlib.sha256(json.dumps(seed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24],
                            "value": gross_value, "analysis_eligible": gross_value is not None,
                            "derived_from": [revenue["fact_id"], cost["fact_id"]],
                            "formula_version": "wind_gross_profit_v1", "formula": "revenue - operating_cost"})
    if not records:
        _issue(issues, "no_financial_fields", "未识别财务科目；请使用明确列名或 field_map。", row=row_no, severity="error")
    return records


def _market_record(row, raw, extra, raw_hash, issues, row_no):
    code = _code(row.get("code"))
    day = _date(row.get("date"))
    available = _date(row.get("available_at"), timestamp=True)
    out = {"code": code, "date": day, "available_at": available,
           "source": _text(row.get("source")) or "Wind离线导出", "source_hash": raw_hash,
           "adj_basis": _text(row.get("adj_basis")), "unit": _text(row.get("unit")),
           "currency": _canonical_value("currency", row.get("currency")),
           "volume_unit": _text(row.get("volume_unit")), "amount_unit": _text(row.get("amount_unit")),
           "retrieved_at": _date(row.get("retrieved_at"), timestamp=True),
           "suspended": row.get("suspended"), "role": _text(row.get("role")),
           "source_row": row_no, "sidecar": {"raw_fields": raw, "unmapped_fields": extra}}
    for field in ("open", "high", "low", "close", "volume", "amount", "adj_factor"):
        out[field] = _numeric(row.get(field))
        if field in row and out[field] is None:
            _issue(issues, "missing_or_invalid_number", "行情数值缺失或无效；不充零。", row=row_no, field=field)
    required = [field for field in ("code", "date", "available_at", "adj_basis", "unit", "currency") if not out[field]]
    if out["close"] is None or out["close"] <= 0:
        required.append("close")
    if required:
        _issue(issues, "missing_identity", "行情缺失/无效字段：" + "、".join(required), row=row_no, field=",".join(required))
    if available and day and available[:10] < day:
        _issue(issues, "availability_before_trade", "行情可用时间早于交易日期。", row=row_no, field="available_at", severity="error")
        required.append("available_at")
    bad_optional = any(field in row and out[field] is None for field in ("open", "high", "low", "volume", "amount", "adj_factor"))
    if any(out[field] is not None and out[field] < 0 for field in ("volume", "amount")):
        _issue(issues, "invalid_market_number", "成交量/成交额不得为负。", row=row_no, severity="error")
        bad_optional = True
    if out["adj_factor"] is not None and out["adj_factor"] <= 0:
        _issue(issues, "invalid_adj_factor", "复权因子应为正值。", row=row_no, field="adj_factor", severity="error")
        bad_optional = True
    prices = [out[field] for field in ("open", "high", "low", "close") if out[field] is not None]
    if any(price <= 0 for price in prices) or (out["high"] is not None and prices and out["high"] < max(prices)) or (out["low"] is not None and prices and out["low"] > min(prices)):
        _issue(issues, "invalid_ohlc", "价格应为正，最高/最低须包住同一行价格。", row=row_no, severity="error")
        bad_optional = True
    out["analysis_eligible"] = not required and not bad_optional
    return out


def preview_file(path, *, kind, metadata=None, field_map=None) -> dict:
    """只读原始文件并归一化，返回候选记录、逐行缺口和原始字节 hash。

    ``metadata`` 给显式行外默认值（单位/币种/披露日等）；行内值优先。
    ``field_map`` 只允许映射至已知规范字段。返回的 ``sidecar.raw_fields``
    保存原列，``metadata.unknown_columns`` 列出未识别字段，供人工决定映射。
    """
    issues = []
    result = {"schema": SCHEMA, "status": "unavailable", "kind": kind, "raw_sha256": "",
              "records": [], "issues": issues, "metadata": {
                  "license": "unknown", "license_status": "unverified", "export_allowed": False,
                  "human_review": "unreviewed", "no_network": True}}
    if kind not in ("financial", "market"):
        _issue(issues, "invalid_kind", "kind 只支持 financial/market。", severity="error")
        return result
    if metadata is not None and not isinstance(metadata, dict) or field_map is not None and not isinstance(field_map, dict):
        _issue(issues, "invalid_options", "metadata/field_map 必须为字典。", severity="error")
        return result
    metadata = dict(metadata or {})
    try:
        p = Path(path)
        blob = p.read_bytes()
    except (OSError, TypeError, ValueError) as exc:
        _issue(issues, "file_unavailable", "无法读取文件：" + str(exc)[:160], severity="error")
        return result
    raw_hash = hashlib.sha256(blob).hexdigest()
    result["raw_sha256"] = raw_hash
    rows, embedded = _read_file(blob, p.suffix.lower(), _text(metadata.get("encoding")), issues)
    combined = {**embedded, **metadata}
    aliases = _aliases(kind)
    canonical_fields = set(aliases.values())
    mapping = {}
    for column, field in (field_map or {}).items():
        if field not in canonical_fields:
            _issue(issues, "invalid_field_mapping", "field_map 目标必须是已知规范字段：" + _text(field), field=_text(column), severity="error")
        else:
            mapping[_key(column)] = field
    if any(issue["code"] == "invalid_field_mapping" for issue in issues):
        return result
    defaults = {canonical: value for key, value in combined.items()
                if (canonical := aliases.get(_key(key))) in canonical_fields}
    unknown = set()
    records = []
    for row_no, raw in enumerate(rows, 1):
        row_issues_start = len(issues)
        row, extra, conflicts = _mapped_row(raw, aliases, mapping, defaults, issues, row_no)
        unknown.update(extra)
        created = (_financial_records(row, raw, extra, raw_hash, issues, row_no)
                   if kind == "financial" else [_market_record(row, raw, extra, raw_hash, issues, row_no)])
        row_errors = any(issue["severity"] == "error" for issue in issues[row_issues_start:])
        # 明确冲突/无效日期必须使候选不可用于模型，即使剩余数值看起来齐全。
        if row_errors or conflicts:
            for record in created:
                record["analysis_eligible"] = False
        records.extend(created)
    if unknown:
        _issue(issues, "unmapped_columns", "未映射字段已保留在 sidecar：" + "、".join(sorted(unknown)), severity="info")
    if not rows:
        _issue(issues, "empty_input", "没有可读取的数据行。", severity="error")
    result["records"] = records
    result["metadata"] = {
        "raw_file": p.name, "raw_bytes": len(blob),
        # 来源标签**反映实际声明的来源**；没有声明就写"未标注"，不因为模块名叫 wind_import
        # 就把官方披露缓存等其它来源冒称成 Wind 导出（B1 接缝②）。
        "source": _text(defaults.get("source")) or "来源未标注（未指定数据来源）",
        "declared": combined, "unknown_columns": sorted(unknown), "field_map": dict(field_map or {}),
        "license": "unknown", "license_claim": combined.get("license", combined.get("许可", "")),
        "license_status": "unverified", "usage_scope": "学校研究/内部开发；实际授权范围待核实",
        "export_allowed": False, "human_review": "unreviewed", "no_network": True,
        "eligible_records": sum(bool(row.get("analysis_eligible")) for row in records),
        "instruments": sorted({row.get("entity_id", row.get("code", "")) for row in records
                               if row.get("entity_id", row.get("code", ""))}),
        "historical_version": "下载时版本；披露日期本身不能证明该值在历史时点未被重述",
    }
    values_present = (any(row.get("value") is not None for row in records) if kind == "financial"
                      else any(row.get("close") is not None and row["close"] > 0 for row in records))
    if values_present:
        ready = all(row.get("analysis_eligible") for row in records)
        incomplete_read = any(issue["severity"] in ("error", "warning") for issue in issues)
        result["status"] = "ready" if ready and not incomplete_read else "partial"
    return result
