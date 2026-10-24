# -*- coding: utf-8 -*-
"""DATA-RC2-1024 · A：筛选页「数据准备」入口（材料投影 / 覆盖预览 / 受约束取数）。

目标（员工任务 §3）：用户输入公司/股票池后能明确知道「可直接用 / 当前条件可计算 /
缺材料 / 冲突待核验」，而不是以目录搜索代替取数。

三件事，都只作用于**当前账号作用域**：

1. `materials_projection(scope)` — 我已有什么材料：
   - 内置官方披露缓存（服务端配置的只读输入，带原文链接与事实 ID）；
   - 本作用域数据集（含来源身份与期间覆盖；跨作用域一律不可见）；
   - 本作用域研究任务已准入事实/底稿投影（`research_materials`，接通后用 `status=ok`）；
   - 本作用域研究清单（「想研究但还没有材料的对象」→ 缺口候选）。
   不扫描其他用户目录，不从生成报告文字反提数字。

2. `prepare_plan(scope, codes, periods)` — 覆盖与缺口预览：
   按**实际行**建立 `(主体, 报告期)` 覆盖，逐字段给出「可计算 / 缺什么」，
   区分「有材料」「当前条件可计算」「缺材料」「冲突待核验」；
   **不发起任何取数**；「在已有数据内搜索 ≠ 取数」写进响应。

3. `collect(scope, source_id, codes, periods)` — 受约束取数：
   - `official_cache`（本地，真实材料）：把缓存的相应行物化成作用域数据集；
   - `adapter:<name>`（网络）：复用现有适配器与出站保护（URL 由服务端按源模板
     构造，客户端不能提交任意 URL/路径）；**同主机一次固定策略拒绝即停**；
   - 期间只接受年报口径（年度 `2024` 或期末 `2024-12-31`）；半年度/季度、非法日期、
     非法代码**显式拒绝**，不切字符串改成年报、不静默丢（RC2-F2）；
   - 源响应主体必须与请求代码一致才入库，不以请求参数替换源身份（RC2-F3）；
   - 金额优先取 `amounts_yuan`（元，全精度），亿元展示值只作展示（RC2-F4）；
   - 部分成功：只物化成功公司的行，逐公司说明，不宣称全池完成。

成本口径（RC2-F6）：传输层没有连接事件，因此
`adapter_calls` = 适配器调用次数（公司级尝试），`network_denied` = 被策略拒绝未连接，
`network_requests` = **未知**（None，不拿 attempts-denied 冒充真实请求数）；
外部费用保持 unknown，不冒充免费。
"""
from __future__ import annotations

import datetime
import os
import re
import time

import screen_store as st
import stock_screen as ss

# ── 来源目录（许可/费用未知就写 unknown，不写 0） ──────────────────

_SOURCES: tuple[dict, ...] = (
    {
        "id": "official_cache", "kind": "local",
        "label": "内置官方披露缓存（整理后宽表，只读）",
        "requires_network": False, "licence": "unknown", "fee": "unknown",
        "fields": tuple(ss.FIELD_SPECS),
        "update": "随仓库/缓存文件更新；服务端按文件哈希判定身份",
        "note": "官方缓存是整理后的宽表：哈希只证明文件没变，不等于每行都回到原文核验过；"
                "原文链接见行内 source_url，人工复核状态未定前按「未独立核验」对待",
    },
    {
        "id": "user_upload", "kind": "upload",
        "label": "我自己的材料（CSV/底稿，上传后成作用域数据集）",
        "requires_network": False, "licence": "自有材料", "fee": "unknown",
        "fields": tuple(ss.FIELD_SPECS),
        "update": "由用户自行上传更新",
        "note": "上传永远是「用户提供」：不能声明官方；来源可用性由用户自己负责",
    },
    {
        "id": "adapter:eastmoney", "kind": "adapter",
        "label": "东方财富数据中心（年报口径财务指标）",
        "requires_network": True, "licence": "unknown", "fee": "unknown",
        "fields": ("revenue", "net_profit", "gross_profit", "gross_margin",
                   "operating_profit", "total_assets", "total_liabilities",
                   "operating_cashflow"),
        "update": "请求时抓取；限频与出站保护复用正式适配器",
        "constraint": "按公司代码+年度取数；URL 由服务端按源模板构造，不接受客户端地址",
        "note": "许可/费用未核实：只记录受限研究用途，不打包再分发数据",
    },
    {
        "id": "adapter:cninfo", "kind": "adapter",
        "label": "巨潮资讯（官方披露 PDF 原文）",
        "requires_network": True, "licence": "unknown", "fee": "unknown",
        "fields": (),
        "update": "请求时抓取；官方源开关默认关闭（探针未通过）",
        "constraint": "按公司代码+报告期取原文链接；不接受客户端地址",
        "note": "许可/费用未核实：原文仅用于证据定位，不入模型上下文",
    },
)

_ADAPTERS = {
    # source_id -> (module, fetch 函数名, 说明)；fetch(company, stock_code, year_range, period)
    "adapter:eastmoney": ("adapters.eastmoney", "fetch_ashare",
                          "A股年报口径财务指标（RPT_F10_FINANCE_MAINFINADATA）"),
}

_CODE_RE = re.compile(r"^[0-9]{6}\.[A-Z]{2}$")
_YEAR_RE = re.compile(r"^\d{4}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_COLLECT_CODES = 50

# 规则字段 → 需要的行内科目：**不在这里另列一份**（G2）。
# 科目需求与可计算判据都来自 `stock_screen.FIELD_SPECS` + `judge_ratio` / `judge_growth`；
# 这里再抄一份"字段需要哪些列"就是第二套判据的来源（复核 G2 的根因）。


def parse_codes_strict(codes) -> tuple[list[str], list[str]]:
    """严格解析公司代码：非法代码**报错**，不静默丢弃、不猜测（RC2-A1）。"""
    out: list[str] = []
    errors: list[str] = []
    for raw in list(codes or []):
        code = str(raw or "").strip().upper()
        if not code:
            errors.append("公司代码为空值：请写形如 600519.SH / 002304.SZ 的 A 股代码")
            continue
        if not _CODE_RE.match(code):
            errors.append(f"公司代码「{raw}」不是 A 股代码（形如 600519.SH / 002304.SZ）："
                          "不猜测、不静默丢弃")
            continue
        if code not in out:
            out.append(code)
    return out, errors


def parse_periods_strict(periods) -> tuple[list[str], list[str]]:
    """严格解析期间：只接受年度或年报期末；半年度/季度/非法日期显式拒绝（RC2-F2）。

    返回 `(["2024-12-31", ...], 错误)`；不接受时返回空列表 + 明确原因，
    绝不把 `2024-06-30` 切成年报、也不自动补中间年。
    """
    out: list[str] = []
    errors: list[str] = []
    for raw in list(periods or []):
        text = str(raw or "").strip()
        if not text:
            errors.append("期间为空值：请写年度（如 2024）或年报期末（如 2024-12-31）")
            continue
        if _YEAR_RE.match(text):
            value = f"{text}-12-31"
        elif _DATE_RE.match(text):
            try:
                datetime.date.fromisoformat(text)
            except ValueError:
                errors.append(f"期间「{text}」不是合法日期")
                continue
            if not text.endswith("-12-31"):
                errors.append(f"期间「{text}」不是年报期末：当前只支持年报口径"
                              "（年度如 2024，或期末 2024-12-31）；"
                              "不会把半年度/季度当成年报")
                continue
            value = text
        else:
            errors.append(f"期间「{text}」格式不合法：请写年度（2024）或年报期末（2024-12-31）")
            continue
        if value not in out:
            out.append(value)
    return out, errors


def sources_catalog() -> list[dict]:
    """数据源选择表：可得字段/期间、更新方式、许可/费用是否核实；未知写 unknown。"""
    out = []
    for src in _SOURCES:
        item = dict(src)
        if src["id"] == "official_cache":
            summary = official_cache_summary()
            item["available"] = summary.get("row_count", 0) > 0
            item["periods"] = summary.get("periods") or []
        else:
            item["available"] = None            # 网络源不探测就不写「可用」
        item["available_note"] = None if item["available"] else \
            "本机缓存没有可用行" if src["id"] == "official_cache" else None
        out.append(item)
    return out


def official_cache_summary() -> dict:
    """内置官方缓存的身份摘要（只读；不复制数据内容进响应，只给行数/期间/链接数）。"""
    payload, errors, meta = st.load_source(None)
    if errors:
        return {"available": False, "errors": errors}
    rows = payload.get("rows") or []
    codes = sorted({str(r.get("code") or "") for r in rows if r.get("code")})
    periods = sorted({str(r.get("period") or "")[:10] for r in rows if r.get("period")})
    urls = sorted({str(r.get("source_url") or "").strip() for r in rows
                   if str(r.get("source_url") or "").strip()})
    fact_ids = sorted({str(f) for r in rows for f in (r.get("orig_fact_ids") or [])})
    return {
        "available": bool(rows), "label": meta.get("label"), "sha256": meta.get("sha256"),
        "row_count": len(rows), "codes": codes, "periods": periods,
        "official": bool(meta.get("official")), "synthetic": bool(meta.get("synthetic")),
        "source_url_count": len(urls), "fact_id_count": len(fact_ids),
        "note": ("原文链接与事实 ID 在行内；官方身份由服务端按配置路径判定，"
                 "人工复核状态未定前按「未独立核验」对待"),
    }


def _dataset_coverage(scope) -> dict:
    """本作用域**全部**数据集 → {code: [{dataset_id, name, source, period, row}]}（不跨作用域）。

    这是「我有哪些材料」的**库存覆盖**（materials_projection 用）；预览的可执行计算
    覆盖不从这里取（G3：预览绑定到唯一输入，见 `_resolve_preview_input`）——
    库存可以列全部数据集，但当前输入可算什么只看绑定到的那一份。"""
    norm = st.normalize_scope(scope)
    coverage: dict[str, list[dict]] = {}
    for meta in st.list_datasets(norm, limit=200):
        if not meta.get("usable", True):
            continue
        _m, payload, err = st.load_dataset(norm, meta["dataset_id"])
        if err:
            continue
        for row in payload.get("rows") or []:
            code = str(row.get("code") or "")
            if not code:
                continue
            coverage.setdefault(code, []).append({
                "dataset_id": meta["dataset_id"], "name": meta.get("name"),
                "period": str(row.get("period") or "")[:10],
                "entity": str(row.get("entity") or ""),
                "source": "official_cache" if meta.get("official") else "user_upload",
                "source_url": str(row.get("source_url") or ""),
                "row": row,
            })
    return coverage


def _required_metrics(spec) -> list[str]:
    """一个字段需要哪些行内科目（来自 `stock_screen.FIELD_SPECS`，不另列一份）。"""
    if spec.kind == "amount":
        return [spec.metric]
    if spec.needs_prev:
        return [spec.metric, f"{spec.metric}（上一自然年）"]
    return [spec.numerator, spec.denominator]


def _field_status(row: dict, prior_row: dict | None, *, period: str = "") -> dict:
    """单行 → 五个规则字段的「可计算 / 不可算（缺什么）」——**与执行同一条判据**（G2）。

    此前只看"字段非空"：资产总计=0 的行在这里是「可算」，执行却是资料不足——预览与
    执行各有一套判据正是复核 G2 的根因。现在直接调用 `stock_screen.judge_ratio` /
    `judge_growth`（执行读数用的同一入口），预览说「可算」当且仅当执行读数为 ok。
    上一期行必须是**相邻上一年度**且来自同一绑定输入（G1/G3），不跨数据集拼行。
    """
    period = str((row or {}).get("period") or "")[:10] or str(period or "")
    prior_period = ss.adjacent_prior_period(period)
    out: dict[str, dict] = {}
    for field_id, spec in ss.FIELD_SPECS.items():
        if spec.kind == "amount":
            value = ss.value_from_row(row, spec.metric)
            if value.usable:
                out[field_id] = {"status": "ok", "metric": spec.metric}
            else:
                out[field_id] = {"status": value.state, "metric": spec.metric,
                                 "missing": [spec.metric],
                                 "reason": ss.unusable_reason(value)}
            continue
        if spec.needs_prev:
            cur = ss.value_from_row(row, spec.metric)
            prior = (ss.value_from_row(prior_row, spec.metric) if prior_row else
                     ss.ComputableValue(metric=spec.metric, period=prior_period))
            status, reason = ss.judge_growth(cur, prior, prior_period=prior_period)
            info = {"status": status, "metric": spec.metric,
                    "prior_period_required": prior_period}
            if status == "ok" and prior_row is not None:
                info["previous_period"] = str(prior_row.get("period") or "")[:10]
            else:
                info["missing"] = ([spec.metric] if not cur.usable
                                   else [f"{spec.metric}（{prior_period or '上一自然年'}）"])
                info["reason"] = reason
            out[field_id] = info
            continue
        num = ss.value_from_row(row, spec.numerator)
        den = ss.value_from_row(row, spec.denominator)
        status, reason = ss.judge_ratio(num, den, label=spec.label)
        info = {"status": status, "metric": spec.numerator}
        if status == "ok":
            info["metric"] = f"{spec.numerator}/{spec.denominator}"
        else:
            missing = [m for m, v in ((spec.numerator, num), (spec.denominator, den))
                       if not v.usable]
            info["missing"] = missing or [spec.denominator]
            info["reason"] = reason
        out[field_id] = info
    return out


def materials_projection(scope) -> dict:
    """我已有什么材料：数据集 + 官方缓存 + 研究任务投影 + 清单缺口（只读本作用域）。"""
    norm = st.normalize_scope(scope)
    datasets = st.list_datasets(norm, limit=50)
    official = official_cache_summary()
    wl, _errs = st.load_watchlist(norm)
    coverage = _dataset_coverage(norm)
    wl_codes = [i["code"] for i in (wl.get("items") or [])]
    gaps = [c for c in wl_codes if c not in coverage]
    runs = st.list_runs(norm, limit=50)
    run_sources = sorted({str((r.get("source") or {}).get("label") or "")
                          for r in runs if (r.get("source") or {}).get("label")})
    # 研究任务/事实/底稿投影（RC2-A3）：接通后 status=ok；未接通或该作用域无任务时明确说明。
    research: dict = {"status": "unavailable", "tasks": [],
                      "note": "研究任务材料投影未接通"}
    try:
        import research_materials as rm
        research = rm.materials_from_research(norm)
    except Exception as exc:                          # noqa: BLE001 - 投影失败不影响其余材料
        research = {"status": "unavailable", "tasks": [],
                    "note": f"研究任务材料投影不可用：{exc}"}
    return {
        "status": "ok", "scope": dict(norm),
        "official_cache": official,
        "datasets": [{
            "dataset_id": d.get("dataset_id"), "name": d.get("name"),
            "row_count": d.get("row_count"), "codes": d.get("codes"),
            "periods": d.get("periods"), "created_at": d.get("created_at"),
            "source": "official_cache" if d.get("official") else "user_upload",
            "usable": d.get("usable", True),
            "note": "官方缓存物化" if d.get("official") else "我上传的材料（未独立核验）",
        } for d in datasets],
        "research": research,
        "watchlist": {"items": wl_codes, "missing_material": gaps},
        "runs": {"count": len(runs), "sources": run_sources},
        "reading": "清单是「想研究的对象」；没有材料覆盖的不等于已取数，缺口要显式准备",
    }


def _resolve_preview_input(scope, dataset_id: str = "") -> tuple[dict, list[str]]:
    """预览绑定的**唯一**输入（G3，2026-10-24 复核）：与执行 `_resolve_input` 同一取舍。

    此前预览把本作用域**所有**数据集 + 官方缓存按 `(code, period)` 拼成一张大表，于是
    "只选了 2024 数据集"的预览能借另一个数据集里 2023 的行算出同比，而执行只看得见
    2024 → 预览说可算、执行说资料不足（复核反例）。预览必须绑定到同一份输入：
    指定 `dataset_id` 就只读那一份（找不到照实报错，不静默换成别的数据）；未指定则
    用内置官方缓存——执行在同样不传 `dataset_id` 时用的也是它。
    """
    norm = st.normalize_scope(scope)
    want = str(dataset_id or "").strip()
    if want:
        meta, payload, error = st.load_dataset(norm, want)
        if error:
            return {}, [error]
        rows = payload.get("rows") or []
        return {
            "dataset_id": str(meta.get("dataset_id") or want),
            "name": str(meta.get("name") or ""),
            "source": "official_cache" if meta.get("official") else "user_upload",
            "official": bool(meta.get("official")),
            "user_provided": bool(meta.get("user_provided")),
            "provenance_kind": str((meta.get("provenance") or {}).get("kind") or ""),
            "synthetic": bool(meta.get("synthetic")),
            "periods": sorted({str(r.get("period") or "")[:10] for r in rows
                               if r.get("period")}),
            "rows": rows,
        }, []
    payload, errors, meta = st.load_source(None)
    if errors:
        return {}, [f"内置缓存不可读：{e}" for e in errors]
    rows = payload.get("rows") or []
    return {
        "dataset_id": "",
        "name": str(meta.get("label") or "内置官方披露缓存"),
        "source": "official_cache",
        "official": bool(meta.get("official")),
        "user_provided": False,
        "provenance_kind": "",
        "synthetic": bool(meta.get("synthetic")),
        "periods": sorted({str(r.get("period") or "")[:10] for r in rows if r.get("period")}),
        "rows": rows,
    }, []


def prepare_plan(scope, codes, periods=(), dataset_id=None, as_of=None) -> dict:
    """覆盖与缺口预览：逐主体×逐期间「哪些材料覆盖 / 哪些字段可计算」。**不取数**。

    覆盖与可计算性都**绑定到同一份输入**（G3）：`dataset_id` 指定的数据集，或未指定时的
    内置官方缓存——与执行同一取舍，所以"预览说可算"与"执行通过"是同一件事。
    `as_of` 可选：给了就按 `stock_screen.row_pit_state` 同一穿越判据把待核验/晚于截止日
    的行标出来（不给就只做材料与字段可计算性，不假装判过 PIT）。
    「我有哪些材料」是另一件事，见 `materials_projection`（库存覆盖，可列全部数据集）。
    """
    norm = st.normalize_scope(scope)
    code_list, code_errors = parse_codes_strict(codes)
    period_list, period_errors = parse_periods_strict(periods)
    bound, bind_errors = _resolve_preview_input(norm, dataset_id or "")
    errors = list(bind_errors) + code_errors + period_errors
    as_of_text = str(as_of or "").strip()
    if as_of_text and not ss.iso_date(as_of_text):
        errors.append(f"as_of 必须是 YYYY-MM-DD（资料截止日），得到「{as_of_text}」")
    if len(code_list) > _MAX_COLLECT_CODES:
        errors.append(f"一次最多 {_MAX_COLLECT_CODES} 家公司：请分批（目标 20–50 家）")
    official = official_cache_summary()
    bound_rows: dict[str, dict[str, dict]] = {}
    for row in (bound.get("rows") or []):
        code = str(row.get("code") or "")
        period = str(row.get("period") or "")[:10]
        if code and period:
            bound_rows.setdefault(code, {}).setdefault(period, row)
    items = []
    totals = {"covered": 0, "partial": 0, "material_only": 0, "missing": 0,
              "codes": len(code_list), "periods": len(period_list)}
    field_totals: dict[str, dict] = {f: {"ok": 0, "missing": 0}
                                     for f in ss.FIELD_SPECS}
    if bind_errors:
        return {
            "status": "invalid", "scope": dict(norm), "errors": errors,
            "items": [], "totals": totals, "field_totals": field_totals,
            "input_binding": {"dataset_id": str(dataset_id or ""), "source": "",
                              "note": "预览输入无法解析：不退回别的数据集（G3）"},
            "official_cache": {"available": official.get("available"),
                               "codes": len(official.get("codes") or []),
                               "periods": official.get("periods") or []},
            "reading": "预览输入不可用：没有拿别的数据集顶替（不跨数据集拼行）",
        }
    for code in code_list:
        own = bound_rows.get(code, {})
        # 未指定期间：只列**该输入**里已有材料的期间，不做无区间猜测
        periods_here = period_list or sorted(own)
        per_period = []
        for period in periods_here:
            row = own.get(period)
            if row is None:
                totals["missing"] += 1
                per_period.append({
                    "period": period, "status": "missing", "material": False,
                    "computable": False, "via": "",
                    "dataset_id": bound.get("dataset_id", ""),
                    "fields": {f: {"status": "missing", "missing": _required_metrics(spec)}
                               for f, spec in ss.FIELD_SPECS.items()},
                    "note": ("所选输入里没有这家/这个期间的行：需要准备（上传自己的材料"
                             "或明确许可的来源取数），不跨数据集拼行"),
                })
                for f in ss.FIELD_SPECS:
                    field_totals[f]["missing"] += 1
                continue
            prior_period = ss.adjacent_prior_period(period)
            prior_row = own.get(prior_period) if prior_period else None
            prior_pit = ""
            if as_of_text and prior_row is not None:
                # 执行在冻结前就按截止日过滤行：上一年度行待核验/晚于截止日 → 那一年
                # 在执行侧等于"没有观察"。预览必须同样不借它算同比（G1/G2 同一取舍）。
                p_state, p_reason, _pa = ss.row_pit_state(prior_row, as_of_text)
                if p_state != "ok":
                    prior_pit = f"上一年度 {prior_period}：{p_reason}"
                    prior_row = None
            fields = _field_status(row, prior_row, period=period)
            ok_count = sum(1 for v in fields.values() if v.get("status") == "ok")
            pit = {"state": "not_evaluated", "reason": "", "available_at": ""}
            if as_of_text:
                state, pit_reason, avail = ss.row_pit_state(row, as_of_text)
                pit = {"state": state, "reason": pit_reason, "available_at": avail}
                if state != "ok":
                    # 与执行同判据：待核验/晚于截止日的行执行时会被过滤，预览不冒充可算
                    pending_reason = pit_reason
                    for f in fields:
                        fields[f] = {"status": ("pending_pit" if state == "pending"
                                                else "excluded_pit"),
                                     "missing": _required_metrics(
                                         ss.FIELD_SPECS[f]),
                                     "reason": pending_reason}
                    ok_count = 0
            if ok_count == len(fields):
                status = "covered"          # 有材料且五字段全可算
            elif ok_count:
                status = "partial"          # 有材料、部分字段可算
            else:
                status = "material_only"    # 有材料但一个字段都算不出（缺科目/缺上期）
            totals[status] += 1
            for f, v in fields.items():
                field_totals[f]["ok" if v.get("status") == "ok" else "missing"] += 1
            per_period.append({
                "period": period, "status": status, "material": True,
                "computable": ok_count == len(fields),
                "via": "所选数据集" if bound.get("dataset_id") else "内置官方缓存",
                "dataset_id": bound.get("dataset_id", ""),
                "entity": str(row.get("entity") or ""),
                "source_url": str(row.get("source_url") or ""),
                "pit": pit,
                "fields": fields,
                "note": "" if status == "covered" else
                        "有材料但当前条件不可全算：缺的科目/原因见 fields",
            })
            if prior_pit:
                per_period[-1]["prior_year_pit"] = prior_pit
        if not period_list and not periods_here:
            items.append({"code": code, "entity": "", "periods": [], "missing_material": True,
                          "note": "所选输入里没有这家公司的任何行：没有材料覆盖（不拿别的数据集顶替）"})
            continue
        items.append({"code": code,
                      "entity": next((str(r.get("entity") or "") for r in own.values()
                                      if r.get("entity")), ""),
                      "periods": per_period})
    return {
        "status": "ok" if not errors else "invalid",
        "scope": dict(norm),
        "errors": errors,
        "items": items,
        "totals": totals,
        "field_totals": field_totals,
        "input_binding": {
            "dataset_id": bound.get("dataset_id", ""),
            "name": bound.get("name", ""),
            "source": bound.get("source", ""),
            "periods": bound.get("periods") or [],
            "official": bool(bound.get("official")),
            "as_of": as_of_text,
            "pit_note": ("" if as_of_text else
                         "未提供 as_of：本预览只判材料与字段可计算性，不判定 PIT"
                         "（执行会按资料截止日过滤待核验/晚于截止日的行）"),
            "note": ("未指定数据集：预览绑定内置官方缓存（与不传 dataset_id 的执行同一输入）"
                     if not bound.get("dataset_id") else
                     "预览绑定所选数据集：执行选同一份数据集时结论一致；"
                     "不跨数据集拼行、不借别的数据集的年度算同比（G3）"),
        },
        "official_cache": {"available": official.get("available"),
                           "codes": len(official.get("codes") or []),
                           "periods": official.get("periods") or []},
        "reading": "这是**当前输入**的可执行计算覆盖：在已有数据内搜索不等于取数；"
                   "覆盖按绑定输入的实际 (主体, 期间) 行计算，字段可计算性与执行同一判据；"
                   "缺的材料要用「我自己的材料」或明确许可的来源单独准备；"
                   "「我有哪些材料」看 materials_projection（可列全部数据集）",
    }


def _classify_error(exc: Exception) -> str:
    """取数失败分类：策略拒绝 / DNS / 连接 / HTTP / 结构 / 解析。"""
    text = str(exc or "")
    low = text.lower()
    if any(k in low for k in ("ssrf", "非公网", "策略", "reserved", "policy", "blocked url",
                              "fake", "198.18.")):
        return "policy_denied"
    if any(k in low for k in ("timeout", "timed out", "getaddrinfo", "name or service",
                              "name resolution", "dns")):
        return "dns_or_timeout"
    if any(k in low for k in ("connection", "connect", "refused", "reset", "unreachable")):
        return "connect_error"
    if any(k in low for k in ("http", "403", "404", "500", "502", "503")):
        return "http_error"
    if any(k in low for k in ("json", "结构", "参数错误", "empty", "0 行")):
        return "schema_error"
    return "other_error"


def _year_range(periods) -> tuple[int, int] | None:
    years = []
    for raw in list(periods or []):
        m = _YEAR_RE.match(str(raw or "").strip()[:4])
        if m:
            years.append(int(m.group(0)))
    if not years:
        return None
    return (min(years), max(years))


def _rows_from_adapter_financials(entry: dict) -> tuple[list[dict], list[str]]:
    """适配器 financials → 宽表行 + 行级拒绝原因。

    RC2-F3：源响应主体（SECUCODE/SECURITY_CODE）必须与请求代码一致才入库；
    缺失或冲突时拒绝并说明，不按请求代码给响应改姓。
    RC2-F4：金额优先取 `amounts_yuan`（元，全精度）；只有展示用亿元值时可回退，
    但会标 `precision=display_rounded_yuan`，不谎称全精度。
    """
    metadata = entry.get("metadata") or {}
    # 单位/币种/口径缺失时**不补默认**（A2）：观察层会按既有规则记为 unknown。
    currency = str(metadata.get("currency") or "")
    caliber = str(metadata.get("caliber") or "")
    url = str(((entry.get("raw") or {}).get("url")) or "")
    requested = str(entry.get("code") or "").strip().upper()
    rows: list[dict] = []
    errors: list[str] = []
    for f in entry.get("financials") or []:
        year = str(f.get("year") or "")
        if not _YEAR_RE.match(year):
            continue
        subject = f.get("source_subject") or {}
        source_code = str(subject.get("code") or "").strip().upper()
        if not source_code:
            errors.append(f"{year}：源响应缺证券代码，主体身份不明 → 拒绝入库（留待核验）")
            continue
        if requested and source_code != requested:
            errors.append(f"{year}：源响应主体是 {source_code}，与请求 {requested} 不一致"
                          " → 拒绝入库（不按请求代码给响应改姓）")
            continue
        period = f"{year}-12-31"
        row = {
            "entity": str(metadata.get("company") or entry.get("name") or ""),
            "code": requested,
            "period": period, "unit": "元", "currency": currency,
            "caliber": caliber, "period_type": "年报",
            "source_url": url,
            "source_subject_code": source_code,
            "source_subject_name": str(subject.get("name") or ""),
            "available_at": str(f.get("disclosure_date") or "")[:10],
            "disclosed_at": str(f.get("disclosure_date") or "")[:10],
            "retrieved_at": str(metadata.get("retrieved_at") or ""),
            "source_id": "adapter:eastmoney",
        }
        amounts = f.get("amounts_yuan") or {}
        filled = 0
        if isinstance(amounts, dict) and amounts:
            row["unit"] = "元"
            row["source_amount_unit"] = "元"
            row["normalization_version"] = str(metadata.get("normalization_version")
                                               or "amount_yuan/1")
            for key in ss.AMOUNT_METRICS:
                value = amounts.get(key)
                if value is None or value == "":
                    continue
                try:
                    row[key] = int(value) if float(value) == int(float(value)) else float(value)
                    filled += 1
                except (TypeError, ValueError):
                    continue
            if filled:
                row["precision"] = "source_exact"
        if not filled:                              # 旧适配器：只有亿元展示值 → 回退并标注
            row["source_amount_unit"] = "亿元"
            row["normalization_version"] = "display_yi_rounded/0"
            for key in ss.AMOUNT_METRICS:
                value = f.get(key)
                if value is None:
                    continue
                try:
                    row[key] = round(float(value) * 1e8, 2)
                    filled += 1
                except (TypeError, ValueError):
                    continue
            if filled:
                row["precision"] = "display_rounded_yuan"
                row["precision_note"] = ("源响应只给了亿元两位小数展示值：换算回元后"
                                         "精度只到 0.01 亿元，不等同原始元金额")
        if filled:
            rows.append(row)
        else:
            errors.append(f"{year}：没有可用金额科目（结构不符，不猜）")
    return rows, errors


def collect(*, scope, source_id: str, codes, periods=(),
            probe_only: bool = False) -> tuple[dict | None, list[str]]:
    """受约束取数：本地缓存物化 / 适配器逐公司抓取（策略拒绝一次即停本批）。"""
    norm = st.normalize_scope(scope)
    sid = str(source_id or "").strip()
    code_list, code_errors = parse_codes_strict(codes)
    period_list, period_errors = parse_periods_strict(periods)
    errors = code_errors + period_errors
    if errors:
        return None, errors
    if not code_list:
        return None, ["没有可取数的公司代码（形如 600519.SH / 002304.SZ）"]
    if len(code_list) > _MAX_COLLECT_CODES:
        return None, [f"一次最多 {_MAX_COLLECT_CODES} 家：请分批取数（目标 20–50 家，分批给实际数）"]
    years = sorted({int(p[:4]) for p in period_list})
    requested_years = set(years)

    if sid == "official_cache":
        payload, errors, meta = st.load_source(None)
        if errors:
            return None, [f"内置缓存不可读：{e}" for e in errors]
        want = set(code_list)
        want_periods = set(period_list) if period_list else None
        picked = [r for r in (payload.get("rows") or [])
                  if str(r.get("code") or "") in want
                  and (want_periods is None or str(r.get("period") or "")[:10] in want_periods)]
        found = sorted({str(r.get("code") or "") for r in picked})
        missing = [c for c in code_list if c not in found]
        if not picked:
            return None, ["内置缓存里没有这些公司/期间的行：请先准备材料（本缓存是只读输入，"
                          "不能把缺口编成数据）"]
        meta_extra = {
            "official": bool(meta.get("official")), "user_provided": False,
            "synthetic": bool(meta.get("synthetic")),
            "note": "由内置官方披露缓存物化的作用域数据集：来源身份沿用缓存（服务端判定）；"
                    "行带原文链接与事实 ID，人工复核未做前按「未独立核验」对待",
        }
        synthetic = bool(meta.get("synthetic"))
        provenance = {
            "kind": ("synthetic_demo" if synthetic else
                     ("official_cache_materialized" if meta.get("official")
                      else "local_cache_materialized")),
            "kind_label": ("合成演示（只用于界面走查，不进真实候选）" if synthetic
                           else "内置官方缓存物化"),
            # 合成标记必须随 provenance 一起落库：save_dataset_from_source 只认这里，
            # 否则合成数据会被写成「本地缓存物化」，界面上看不出是合成的。
            "synthetic": synthetic,
            "file_sha256": meta.get("sha256"), "origin_ref": meta.get("origin"),
            "official": bool(meta.get("official")),
            "note": "哈希只证明这份缓存文件没变，不等于每行回到原文核验过；原文链接见行内 source_url",
        }
        data, reused, ds_errors = st.save_dataset_from_source(
            scope=norm, name=f"官方缓存物化 {time.strftime('%Y-%m-%d %H:%M')}",
            rows=picked, provenance=provenance)
        if ds_errors:
            return None, ds_errors
        metering = st.record_cost(norm, prepare_requests=1)
        return {
            "status": "ok", "source": sid, "scope": dict(norm),
            "dataset": data, "reused": reused,
            "companies": [{"code": c, "status": "collected",
                           "rows": sum(1 for r in picked if str(r.get("code") or "") == c),
                           "periods": sorted({str(r.get("period") or "")[:10] for r in picked
                                              if str(r.get("code") or "") == c})}
                          for c in found]
                         + [{"code": c, "status": "missing",
                             "note": "缓存里没有这家/这些期间：不冒充已取数"} for c in missing],
            "counters": {"adapter_calls": 0, "network_attempts": 0, "network_denied": 0,
                         "network_requests": 0, "network_ok": 0, "network_failed": 0,
                         "network_requests_note": "本地来源：没有发起网络请求（已知 0）"},
            "metering": {"recorded": metering.get("recorded"),
                         "recording": metering.get("recording")},
            "note": "本地来源：没有发起任何网络请求；数据集已入本作用域，可继续筛选与原文定位",
        }, []

    adapter = _ADAPTERS.get(sid)
    if adapter is None:
        return None, [f"未知来源「{sid}」：请从数据源选择表里选（本地来源不联网、可先用）"]
    import importlib
    module = importlib.import_module(adapter[0])
    fetch_fn = getattr(module, adapter[1])

    companies: list[dict] = []
    collected_rows: list[dict] = []
    # RC2-F6：传输层没有连接事件 → 只报「适配器调用次数」，真实 HTTP 请求数标未知，
    # 不拿 attempts-denied 冒充真实请求数（旧实现把 adapter 调用数当 network_requests）。
    counters = {"adapter_calls": 0, "network_attempts": 0, "network_denied": 0,
                "network_ok": 0, "network_failed": 0, "pre_connect_failures": 0,
                "network_requests": None,
                "network_requests_note": ("适配器传输层不暴露连接事件：真实 HTTP 请求数未知；"
                                          "adapter_calls 是适配器调用次数，不等同真实请求数")}
    host_blocked = False
    block_reason = ""
    year_range = _year_range(years)
    for code in code_list:
        if host_blocked:
            companies.append({"code": code, "status": "skipped_policy_blocked",
                              "note": f"同一来源已被固定策略拒绝（{block_reason}），本批停止重试"})
            continue
        if probe_only and companies:
            companies.append({"code": code, "status": "not_attempted",
                              "note": "探测模式：只试第一家，其余未尝试"})
            continue
        counters["adapter_calls"] += 1
        counters["network_attempts"] += 1
        try:
            result = fetch_fn(code, code, year_range=year_range, period="annual")
            if not isinstance(result, dict):
                counters["network_failed"] += 1
                companies.append({"code": code, "status": "failed",
                                  "category": "schema_error",
                                  "detail": f"适配器返回了 {type(result).__name__}，不是财务载荷"})
                continue
            entry = {"name": str(result.get("company") or ""), "code": code, **result}
            entry.setdefault("metadata", {})
            entry["metadata"] = {**entry["metadata"],
                                 "company": str(entry["metadata"].get("company")
                                                or result.get("company") or code)}
            rows, row_errors = _rows_from_adapter_financials(entry)
            if requested_years:                   # 只保留用户明确选择的年度，不补中间年
                rows = [r for r in rows
                        if int(str(r.get("period") or "")[:4]) in requested_years]
            if not rows:
                counters["network_failed"] += 1
                category = ("subject_mismatch"
                            if any("主体" in e or "证券代码" in e for e in row_errors)
                            else "schema_error")
                companies.append({"code": code, "status": "failed",
                                  "category": category,
                                  "detail": "；".join(row_errors)[:300] or
                                            "响应里没有可用年度行（结构不符，不猜）",
                                  "rejected_rows": row_errors[:20]})
                continue
            counters["network_ok"] += 1
            collected_rows.extend(rows)
            item = {"code": code, "status": "collected", "rows": len(rows),
                    "periods": sorted({r["period"] for r in rows}),
                    "source_subject": rows[0].get("source_subject_code"),
                    "precision": rows[0].get("precision")}
            if row_errors:
                item["status"] = "collected_with_rejects"
                item["rejected_rows"] = row_errors[:20]
            companies.append(item)
        except Exception as exc:                       # noqa: BLE001 - 逐公司失败不中断本批
            category = _classify_error(exc)
            if category == "policy_denied":
                counters["network_denied"] += 1
                counters["pre_connect_failures"] += 1
                host_blocked = True
                block_reason = str(exc)[:160]
                companies.append({"code": code, "status": "denied_by_policy",
                                  "category": category, "detail": str(exc)[:200],
                                  "transport_started": False})
                continue
            counters["network_failed"] += 1
            if category in ("dns_or_timeout", "connect_error"):
                counters["pre_connect_failures"] += 1
            companies.append({"code": code, "status": "failed",
                              "category": category, "detail": str(exc)[:200],
                              "transport_started": "unknown" if category == "dns_or_timeout"
                                                   else True})

    dataset = None
    reused = False
    ds_errors: list[str] = []
    if collected_rows:
        provenance = {
            "kind": "adapter_collected", "kind_label": "适配器取数（未核验）",
            "source_id": sid, "official": False,
            "source_urls": sorted({str(r.get("source_url") or "") for r in collected_rows
                                   if str(r.get("source_url") or "")})[:50],
            "original_verified": False,
            "normalization_version": collected_rows[0].get("normalization_version"),
            "retrieved_at": collected_rows[0].get("retrieved_at"),
            "note": "适配器取数：请求指纹证明同一份数据，不等于原文核验；"
                    "许可/费用未核实，只作受限研究用途，不打包再分发",
        }
        dataset, reused, ds_errors = st.save_dataset_from_source(
            scope=norm, name=f"取数 {sid.split(':', 1)[-1]} {time.strftime('%Y-%m-%d %H:%M')}",
            rows=collected_rows, provenance=provenance)
        if ds_errors:
            return None, ds_errors
    metering = st.record_cost(norm, adapter_calls=counters["adapter_calls"],
                              network_denied=counters["network_denied"],
                              network_ok=counters["network_ok"],
                              network_failed=counters["network_failed"],
                              prepare_requests=1 if collected_rows else 0)
    denied = sum(1 for c in companies if c["status"] == "denied_by_policy")
    skipped = sum(1 for c in companies if c["status"] == "skipped_policy_blocked")
    summary_note = (
        f"成功 {sum(1 for c in companies if c['status'].startswith('collected'))} 家、"
        f"被策略拒绝 {denied} 家（未真正连接）、未尝试 {skipped} 家；"
        "部分成功只物化成功公司的行，不宣称全池完成")
    return {
        "status": "ok" if collected_rows else ("blocked" if host_blocked else "no_data"),
        "source": sid, "scope": dict(norm),
        "dataset": dataset, "reused": reused,
        "companies": companies, "counters": counters,
        "metering": {"recorded": metering.get("recorded"),
                     "recording": metering.get("recording")},
        "external_fee": "unknown",
        "note": summary_note + "；被拒的明细与适配器调用数分开记，"
                               "真实 HTTP 请求数未知，外部费用记 unknown",
    }, []
