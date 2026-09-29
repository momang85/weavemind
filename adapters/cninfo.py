# -*- coding: utf-8 -*-
"""巨潮资讯网（cninfo）适配器：**两个端点、两套能力、分开记账**。

官方来源定位：www.cninfo.com.cn 是交易所指定的法定信息披露平台，A 股年报/公告的权威
原始出处（"数据源官方化"合规叙事的落点）。

端点能力（**2026-09-29 实机勘探更正**，证据 `docs/evidence/a2_official_discovery_20260929.md`）：

1. **公告查询** `POST /new/hisAnnouncement/query` —— **已经打通**。
   参数契约见 `PARAM_CONTRACT`，其中一条是关键：`stock` 必须是 `"{code},{orgId}"`。
   **只给裸代码时接口返回 `totalAnnouncement: 0`**（HTTP 200、结构完整、`announcements:null`），
   与"这家公司确实没有公告"长得一模一样——2026-09-07 的探针结论"恒返回 announcements=[]、
   疑似被挡"由此更正：**不是被挡，是参数缺 orgId**（与东财 `SECURITY_CODE` 必须裸代码
   是同一类错误：把参数问题误读成站点限制）。
   orgId 取自 `new/data/szse_stock.json`（实测含沪深全部 A 股 6258 条；
   `sse_stock.json` 不存在，HTTP 404）。
2. **财务指标行**（`webapi.cninfo.com.cn` 数据族）—— **仍未打通**（疑似 mcode token 门禁）。
   `_fetch_annual_rows()` 保持如实抛错，`fetch_cn_or_fallback()` 因此回退东财（行为不变）。

**两套能力分开记账**（专项 §4）：公告查询可用不代表财务行可用，反之亦然；健康/熔断
按**端点**取键（`cninfo_disclosure_query` / `cninfo_org_map` / `cninfo_annual_rows`），
一个端点的参数错不会封死整个供应商。注册信息见 `adapters/source_registry.py`。

取件一律走既有出域通道（`net_policy.fetch_document`：已验 IP 直连、不跟随跳转、
不带凭据、总截止 + 字节上限），本模块不自己拼 socket。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
from datetime import timedelta, timezone

from adapters import source_registry as registry

logger = logging.getLogger(__name__)

# ── 端点与参数契约（改契约 = 旧的"空结果"结论作废，必须重新勘探） ──
_SOURCE = "cninfo_annual"                       # 财务行（旧名，元数据里保持不变）
DISCOVERY_SOURCE = registry.SOURCES["cninfo_disclosure_query"]["health_source"]
ORG_MAP_SOURCE = registry.SOURCES["cninfo_org_map"]["health_source"]
UPSTREAM_FAMILY = "cninfo"

ORG_MAP_URL = "https://www.cninfo.com.cn/new/data/szse_stock.json"
QUERY_URL = "http://www.cninfo.com.cn/new/hisAnnouncement/query"
STATIC_BASE = "http://static.cninfo.com.cn/"
PARAM_CONTRACT = "cninfo-hisannouncement-v1"
QUERY_REFERER = "https://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice"
CATEGORY_ANNUAL = "category_ndbg_szsh"          # 年度报告（含摘要/更正后）
# 交易所本地时区（UTC+8）：披露日与"当年"都按它算，不随宿主时区变化
_EXCHANGE_TZ = timezone(timedelta(hours=8))


def _exchange_today() -> time.struct_time:
    """交易所本地时区的"今天"（用于窗口右端默认值）。"""
    from datetime import datetime
    return datetime.now(tz=_EXCHANGE_TZ).timetuple()
# orgId 映射的进程内缓存时长：映射变化很慢，没必要每次都下 0.6 MB
ORG_MAP_TTL = float(os.environ.get("WEAVEMIND_CNINFO_ORG_TTL", "") or 6 * 3600)
DEFAULT_TIMEOUT = 25
QUERY_MAX_BYTES = 2 * 1024 * 1024
ORG_MAP_MAX_BYTES = 8 * 1024 * 1024

_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Referer": QUERY_REFERER,
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.cninfo.com.cn",
}

# 标题里出现这些词 = 这是**更正/修订后**的版本（必须与原版并存，不得互相覆盖）
_CORRECTION_WORDS = ("更正", "修订", "重述", "补充公告")
_SUMMARY_WORDS = ("摘要", "正文摘要")
# 英文版/其他语言版：同一份披露的另一语言文本，选择时排除（内容判据是中文事实）
_ENGLISH_WORDS = ("英文版", "英文", "English", "(EN)", "（EN）")

_ORG_CACHE: dict[str, object] = {"at": 0.0, "by_code": {}, "reason": "", "code": ""}
_ORG_LOCK = threading.Lock()


class CninfoError(Exception):
    """巨潮接口调用失败（含无数据/参数不兼容）。"""

    def __init__(self, channel: str, reason: str, *, reason_code: str = ""):
        super().__init__(f"cninfo[{channel}]: {reason}")
        self.channel = channel
        self.reason = reason
        self.reason_code = reason_code or registry.UNKNOWN_CAUSE


class DiscoveryUnavailable(CninfoError):
    """公告发现暂时不可用：带**原因码**，供读侧区分"没实现/被挡/确实没有"。"""


def enabled() -> bool:
    """**财务行**链路是否启用（默认关闭：webapi 数据族未打通，见模块 docstring）。

    公告发现不走这个开关——它是另一套能力，见 `discovery_enabled()`：
    一个端点没打通不该把另一个已经打通的端点一起关掉。
    """
    return os.environ.get("WEAVEMIND_CNINFO_ENABLED", "0") == "1"


def discovery_enabled() -> bool:
    """公告发现是否启用（默认开启：已实机验证；`WEAVEMIND_CNINFO_DISCOVERY=0` 可关）。"""
    return os.environ.get("WEAVEMIND_CNINFO_DISCOVERY", "1") != "0"


# ── 取件（薄封装：所有出域纪律都在 net_policy 里，这里只决定参数） ──


def _default_fetch(url: str, *, method: str = "GET", body: str = "",
                   timeout: int = DEFAULT_TIMEOUT, max_bytes: int = QUERY_MAX_BYTES,
                   headers: dict | None = None) -> dict:
    import net_policy
    return net_policy.fetch_document(url, timeout=timeout, max_bytes=max_bytes,
                                     headers=dict(_BROWSER_HEADERS, **(headers or {})),
                                     method=method, body=body or None)


def _classify(exc: BaseException) -> str:
    """异常 → 原因码。**不把"我们自己的策略拒绝"记成站点故障**，也不把参数问题记成封禁。"""
    import net_policy
    if isinstance(exc, net_policy.NetworkPolicyError):
        return registry.POLICY_BLOCKED
    if isinstance(exc, net_policy.FetchError):
        text = str(exc)
        if "重定向" in text:
            return registry.PROTOCOL_ERROR
        if "超过上限" in text or "超过字节上限" in text:
            return registry.PROTOCOL_ERROR
        return registry.NETWORK_ERROR
    if isinstance(exc, CninfoError):
        return exc.reason_code or registry.UNKNOWN_CAUSE
    return registry.UNKNOWN_CAUSE


def _fetch_json(fetch, url: str, *, method: str = "GET", body: str = "",
                timeout: int = DEFAULT_TIMEOUT, max_bytes: int = QUERY_MAX_BYTES,
                what: str = "") -> dict:
    """取一次 JSON：非 2xx / 不是 JSON / 传输失败都**在这里**分类（不吞成"没有数据"）。

    分类放在这一层：调用方拿到的一定是带原因码的 `DiscoveryUnavailable`，
    不需要各自再判一次异常类型（否则每个调用点都可能把策略拒绝记成站点故障）。
    """
    try:
        got = (fetch or _default_fetch)(url, method=method, body=body, timeout=timeout,
                                        max_bytes=max_bytes)
    except Exception as exc:                       # noqa: BLE001 - 分类后重抛
        raise DiscoveryUnavailable(what or url, f"{type(exc).__name__}: {str(exc)[:160]}",
                                   reason_code=_classify(exc)) from exc
    status = int(got.get("status") or 0)
    raw = bytes(got.get("raw") or b"")
    if status in (401, 403):
        raise DiscoveryUnavailable(what or url, f"HTTP {status}", reason_code=registry.AUTH_REQUIRED)
    if status in (429, 456):
        raise DiscoveryUnavailable(what or url, f"HTTP {status}（限流/反爬）",
                                   reason_code=registry.RATE_LIMITED)
    if status != 200:
        raise DiscoveryUnavailable(what or url, f"HTTP {status}",
                                   reason_code=registry.PROTOCOL_ERROR)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:                       # noqa: BLE001
        raise DiscoveryUnavailable(
            what or url, f"响应不是 JSON：{type(exc).__name__}: {str(exc)[:80]}",
            reason_code=registry.PROTOCOL_ERROR)
    if not isinstance(data, dict):
        raise DiscoveryUnavailable(what or url, "响应 JSON 顶层不是对象",
                                   reason_code=registry.SCHEMA_CHANGED)
    data["_probe"] = {"url": url, "status": status, "bytes": len(raw)}
    return data


# ── orgId 映射（参数契约的一半：没有它，查询会**静默空**） ──


def org_map(*, fetch=None, refresh: bool = False) -> dict[str, dict]:
    """`{证券代码: {orgId, name, pinyin}}`；进程内按 TTL 缓存（失败不缓存）。"""
    now = time.time()
    with _ORG_LOCK:
        if (not refresh and _ORG_CACHE["by_code"]
                and now - float(_ORG_CACHE["at"] or 0) < ORG_MAP_TTL):
            return dict(_ORG_CACHE["by_code"])
    data = _fetch_json(fetch, ORG_MAP_URL, timeout=DEFAULT_TIMEOUT,
                       max_bytes=ORG_MAP_MAX_BYTES, what="orgId 映射")
    rows = data.get("stockList")
    if rows is None:
        raise DiscoveryUnavailable("orgId 映射", "响应里没有 stockList（上游改结构了）",
                                   reason_code=registry.SCHEMA_CHANGED)
    by_code: dict[str, dict] = {}
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        by_code[code] = {"orgId": str(row.get("orgId") or "").strip(),
                         "name": str(row.get("zwjc") or "").strip(),
                         "pinyin": str(row.get("pinyin") or "").strip(),
                         "category": str(row.get("category") or "").strip()}
    with _ORG_LOCK:
        _ORG_CACHE.update({"at": now, "by_code": by_code,
                           "reason": "", "code": "",
                           "probe": data.get("_probe") or {}})
    return dict(by_code)


def org_id(code: str, *, fetch=None) -> tuple[str, str]:
    """证券代码 → `(orgId, 简称)`；代码不在映射里时如实报"查不到"，不猜、不裸查。"""
    bare = _bare_code(code)
    if not bare:
        raise DiscoveryUnavailable("orgId 映射", f"证券代码不可识别：{code!r}",
                                   reason_code=registry.EMPTY_RESULT)
    table = org_map(fetch=fetch)
    hit = table.get(bare)
    if not hit or not hit.get("orgId"):
        raise DiscoveryUnavailable(
            "orgId 映射", f"{bare} 不在巨潮证券索引里（非 A 股/已退市/代码有误）",
            reason_code=registry.EMPTY_RESULT)
    return str(hit["orgId"]), str(hit.get("name") or "")


def reset_cache() -> None:
    """清空进程内缓存（测试隔离用）。"""
    with _ORG_LOCK:
        _ORG_CACHE.update({"at": 0.0, "by_code": {}, "reason": "", "code": ""})


def _bare_code(code: str) -> str:
    """`002304.SZ` / `sz002304` / `002304` → `002304`（映射表用裸代码）。"""
    text = str(code or "").strip().upper()
    for suffix in (".SZ", ".SH", ".BJ", ".SS"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    for prefix in ("SZ", "SH", "BJ"):
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            text = text[len(prefix):]
    return text if text.isdigit() else ""


# ── 公告查询（发现链的唯一请求点） ──


def build_query(code: str, org: str, *, years=(), category: str = CATEGORY_ANNUAL,
                page: int = 1, page_size: int = 30, until: str = "") -> dict:
    """构造查询参数（**参数契约 v1**，与站点表单一致）。

    `stock` 必须是 `"{code},{orgId}"`：这是 2026-09-07 探针判"接口死了"的真正原因，
    因此这里不接受空 orgId——宁可调用方拿到 `empty_result` 的明确拒绝，
    也不要发一次"必然静默空"的请求。

    `seDate` 过滤的是**披露日**，不是报告期（实机：京蓝 2020 年报的更正版是 2025-09-05
    才披露的，窗口只开到 2021 就看不到它）。所以默认窗口开到"现在"，
    要问"当时能看到什么"就显式传 `until`。
    """
    span = _year_span(years, until=until)
    return {
        "pageNum": str(int(page or 1)), "pageSize": str(int(page_size or 30)),
        "column": "szse", "tabName": "fulltext", "plate": "",
        "stock": f"{code},{org}", "searchkey": "", "secid": "",
        "category": str(category or CATEGORY_ANNUAL), "trade": "",
        "seDate": span, "sortName": "", "sortType": "", "isHLtitle": "true",
    }


def _year_span(years, *, until: str = "") -> str:
    """年份范围 → `seDate`（接口要 `YYYY-MM-DD~YYYY-MM-DD`）。

    **右端默认开到"现在"**：更正/重述稿在报告期之后很久才发布（京蓝 2020 年报的更正版
    发布于 2025-09-05）。窗口只开到报告期当年，就会把"更正版存在"这件事看漏，
    而"原版与更正版并存"是准入侧的硬要求。想限定"截至某日已知"时传 `until`。
    """
    got = sorted({int(y) for y in (years or ()) if str(y).strip().isdigit()})
    start = f"{got[0] - 1}-01-01" if got else f"{_exchange_today().tm_year - 2}-01-01"
    if str(until or "").strip():
        return f"{start}~{str(until).strip()[:10]}"
    latest = max([_exchange_today().tm_year + 1] + [y + 1 for y in got])
    return f"{start}~{latest}-12-31"


def query_announcements(code: str, *, years=(), org: str = "", category: str = CATEGORY_ANNUAL,
                        page: int = 1, page_size: int = 30, fetch=None,
                        timeout: int = DEFAULT_TIMEOUT, until: str = "") -> dict:
    """按证券查询公告列表 → `{items, total, reason_code, params, probe, contract}`。

    空结果**必须能自证**：`announcements` 为空时带上 `totalAnnouncement` 与本次参数摘要，
    读侧才能区分"这家公司在期间内确实没有该类公告"与"我们参数错了/被挡了"。
    """
    bare = _bare_code(code)
    if not bare:
        raise DiscoveryUnavailable("公告查询", f"证券代码不可识别：{code!r}",
                                   reason_code=registry.EMPTY_RESULT)
    org_id_value = str(org or "").strip()
    name = ""
    if not org_id_value:
        org_id_value, name = org_id(bare, fetch=fetch)
    params = build_query(bare, org_id_value, years=years, category=category,
                         page=page, page_size=page_size, until=until)
    body = urllib.parse.urlencode(params)
    data = _fetch_json(fetch, QUERY_URL, method="POST", body=body, timeout=timeout,
                       max_bytes=QUERY_MAX_BYTES, what="公告查询")
    if "announcements" not in data:
        raise DiscoveryUnavailable(
            "公告查询", f"响应里没有 announcements 字段（键：{sorted(data)[:8]}）",
            reason_code=registry.SCHEMA_CHANGED)
    rows = data.get("announcements") or []
    total = int(data.get("totalAnnouncement") or 0)
    items = [_normalize(row, bare) for row in rows if isinstance(row, dict)]
    reason_code = "" if items else registry.EMPTY_RESULT
    return {
        "source": DISCOVERY_SOURCE,
        "upstream_family": UPSTREAM_FAMILY,
        "access_method": "http_post_form",
        "authority": "official_disclosure",
        "contract": PARAM_CONTRACT,
        "code": bare, "org_id": org_id_value, "name": name,
        "params": params,                      # 参数摘要（无凭据，可入证据）
        "total": total, "items": items,
        "reason_code": reason_code,
        "reason": ("" if items else
                   f"查询成功但 {params['seDate']} 内没有该类公告（total={total}）"),
        "probe": data.get("_probe") or {},
    }


def _normalize(row: dict, code: str) -> dict:
    """接口行 → 统一候选：URL 绝对化、时间、版本（原版/更正后）、文种（正文/摘要）。"""
    adjunct = str(row.get("adjunctUrl") or "").strip().lstrip("/")
    title = str(row.get("announcementTitle") or "").strip()
    stamp = row.get("announcementTime")
    disclosed, precision = _date_from_ms(stamp)
    english = any(w in title for w in _ENGLISH_WORDS)
    return {
        "title": title,
        "url": (STATIC_BASE + adjunct) if adjunct else "",
        "adjunct_url": adjunct,
        "code": str(row.get("secCode") or code),
        "name": str(row.get("secName") or ""),
        "announcement_id": str(row.get("announcementId") or ""),
        "disclosed_at": disclosed,
        "disclosed_at_basis": "source_field:announcementTime" if disclosed else "",
        "disclosed_precision": precision,
        "size_kb": row.get("adjunctSize"),
        "version": ("corrected" if any(w in title for w in _CORRECTION_WORDS)
                    else "original"),
        "is_summary": any(w in title for w in _SUMMARY_WORDS),
        # 语言版本：英文版与中文版是**同一份披露**的两种语言，不是两个独立来源
        # （专项 §5：镜像不算独立确认）。把它标出来，选择策略才不会挑到英文版去抽中文事实。
        "language": "en" if english else "zh",
        "is_english": english,
        "source": DISCOVERY_SOURCE,
        "authority": "official_disclosure",
    }


def _date_from_ms(stamp) -> tuple[str, str]:
    """毫秒时间戳 → `(YYYY-MM-DD, 'day')`；缺字段/非法值 → `("", "")`（不猜日期）。

    **按交易所本地时区（UTC+8）换算，不用宿主时区**：接口给的是北京时间零点，
    在 UTC 机器上按本地时区算会整体退回一天（CI 实测 `2025-04-17 != 2025-04-18`），
    于是同一份披露在不同机器上得到不同披露日——足以让"截至日"判据翻面。
    """
    try:
        seconds = float(stamp) / 1000.0
    except (TypeError, ValueError):
        return "", ""
    if seconds <= 0:
        return "", ""
    try:
        from datetime import datetime
        return datetime.fromtimestamp(seconds, tz=_EXCHANGE_TZ).strftime("%Y-%m-%d"), "day"
    except (OverflowError, OSError, ValueError):
        return "", ""


def discover_annual_reports(code: str, *, years=(), include_summary: bool = True,
                            fetch=None, timeout: int = DEFAULT_TIMEOUT,
                            until: str = "") -> dict:
    """发现某公司某个期间范围的**年报类原始文件**（官方公告查询，独立于全文搜索）。

    返回 `query_announcements()` 的结果，外加：
    - `items`：只留年报类，按 (报告期倒序, 原版在前/更正版并列) 排序，**同一份披露的
      原版与更正版都保留**——更正稿是 2025 年发布的，不能覆盖 2021 年当时能看到的版本；
    - `matched` / `unmatched`：命中的与"取回了但不是我们要的"分开列，避免把
      "有返回但都不相关"说成"没有公告"（原因码 `irrelevant_result`）。

    `until` 限定"截至某日已知"（窗口右端），用于**历史时点**研究：不传就是"到今天为止"。
    """
    from narrative_evidence import _doc_period

    got = query_announcements(code, years=years, fetch=fetch, timeout=timeout,
                              until=until)
    matched: list[dict] = []
    unmatched: list[dict] = []
    for item in got.get("items") or ():
        period = ""
        try:
            period = str(_doc_period(item.get("title") or "", item.get("url") or "") or "")
        except Exception:                          # noqa: BLE001 - 取不到期间按不相关处理
            period = ""
        want = [str(y) for y in (years or ()) if str(y).strip()]
        item = dict(item, period=period)
        if period and (not want or period in want) \
                and (include_summary or not item.get("is_summary")):
            matched.append(item)
        else:
            unmatched.append(item)
    matched.sort(key=lambda it: (str(it.get("period") or ""), it.get("is_summary") is True,
                                 str(it.get("disclosed_at") or "")), reverse=True)
    if not matched and unmatched:
        got["reason_code"] = registry.IRRELEVANT_RESULT
        got["reason"] = (f"取回 {len(unmatched)} 条公告，但没有一条是"
                         f"{'/'.join(str(y) for y in (years or ())) or '目标期间'}的年报正文")
    got["items"] = matched
    got["unmatched"] = unmatched
    got["matched_count"] = len(matched)
    return got


# ── 财务指标行（仍未打通：如实抛错，不猜数） ──


def _to_yi(raw) -> float | None:
    """原始值（元）→ 亿元。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v / 1e8, 2)


def _fetch_annual_rows(stock_code: str, year_range=None) -> list[dict]:
    """拉取年报财务行（**未打通**，恒抛错触发回退）。

    为什么不做"顺手用 PDF 顶替"：财务行要的是**结构化口径**，而本模块的公告发现
    只负责把原件找出来；原文解析在 `adapters/annual_financial_tables.py`，两者不可互相冒充。
    """
    raise CninfoError("probe", "巨潮 webapi 财务数据族未打通（疑似 mcode 门禁），"
                               "详见模块 docstring 与 source_registry",
                      reason_code=registry.AUTH_REQUIRED)


def fetch_annual_report(company: str, stock_code: str, year_range=None,
                        max_years: int = 12) -> dict:
    """抓取 A 股年报财务指标（营收/净利/研发投入等），输出契约同 eastmoney.fetch_ashare。"""
    from adapters.source_health import ensure_available, mark_failure, mark_success

    ensure_available(_SOURCE)
    try:
        rows = _fetch_annual_rows(stock_code, year_range)
        if not rows:
            raise CninfoError("data", f"巨潮 A股 无数据: {stock_code}")
        annuals = [r for r in rows if "年报" in str(r.get("REPORT_TYPE") or "")]
        annuals.sort(key=lambda r: str(r.get("REPORT_DATE") or ""), reverse=True)
        if year_range:
            start, end = year_range
            annuals = [
                r for r in annuals
                if start <= int(str(r.get("REPORT_DATE") or "")[:4]) <= end
            ]
        annuals = annuals[:max_years]
        mark_success(_SOURCE)
    except Exception as exc:
        mark_failure(_SOURCE, str(exc))
        raise

    financials = []
    for r in annuals:
        financials.append({
            "year": int(str(r.get("REPORT_DATE") or "")[:4]),
            "report_type": str(r.get("REPORT_TYPE") or ""),
            "revenue": _to_yi(r.get("TOTALOPERATEREVE")),
            "net_profit": _to_yi(r.get("PARENTNETPROFIT")),
            "gross_profit": _to_yi(r.get("MLR")),
            "gross_margin": round(float(r["XSMLL"]), 2)
                            if r.get("XSMLL") is not None else None,
            "operating_profit": _to_yi(r.get("OPERATE_PROFIT_PK")),
            "total_assets": _to_yi(r.get("TOTAL_ASSETS_PK")),
            "total_liabilities": _to_yi(r.get("LIABILITY")),
            "operating_cashflow": _to_yi(r.get("NETCASH_OPERATE_PK")),
            "rd_expense": _to_yi(r.get("RDEXPEND")),
        })
    metadata = {
        "source": _SOURCE,
        "company": str(company or ""),
        "stock_code": str(stock_code),
        "currency": "CNY",
        "unit": "亿元",
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "annual_count": len(annuals),
        "latest_report": f"{str(annuals[0].get('REPORT_DATE') or '')[:10]} 年报" if annuals else "",
    }
    return {
        "financials": financials,
        "metadata": metadata,
        "raw": {
            "url": QUERY_URL,
            "text": json.dumps({"stock_code": stock_code, "rows": len(annuals)}, ensure_ascii=False),
        },
    }


def fetch_cn_or_fallback(company: str, stock_code: str, year_range=None,
                         max_years: int = 12, period: str = "annual") -> dict:
    """A 股财务抓取统一入口：巨潮优先（启用且可用时），东财兜底。

    路由层调用本函数即可获得"官方源优先 + 降级"语义；
    巨潮关闭（默认）时直接走东财，行为与现状完全一致。
    period 透传给东财（annual/quarter/all）；巨潮通道目前只做年报，
    period != annual 时直接走东财（它才带季报/中报行）。

    **公告发现不在此路径上**：`discover_annual_reports()` 是另一套已打通的能力，
    不受 `WEAVEMIND_CNINFO_ENABLED` 影响（端点分开记账）。
    """
    if enabled() and str(period or "annual").lower() == "annual":
        try:
            return fetch_annual_report(company, stock_code, year_range, max_years)
        except Exception as exc:
            logger.info("cninfo unavailable, fallback to eastmoney_ashare: %s", str(exc)[:120])
    from adapters.eastmoney import fetch_ashare
    return fetch_ashare(company, stock_code, year_range, max_years, period=period)
