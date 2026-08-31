# -*- coding: utf-8 -*-
"""免费合规金融数据插件（MCP 插件系统：本地直连，无需任何账号授权）。

设计原则（对标竞品调研"数据源开关与透明性"）：
- 全部数据源为免费公开接口（腾讯行情 / 新浪排行 / 东方财富 / SEC EDGAR /
  FRED 宏观 / CoinGecko 加密），无需 API key、无个人账号；
- 本地直接调用 adapters 适配器，不经过 worker 派发（快、稳、可审计）；
- 每个工具带数据源与时效说明，可被 tool_contracts / mcp_lite 动态发现；
- 所有调用 try/except 静默降级，绝不影响任务主线。

工具清单（向 MCP 客户端/规划器暴露）：
- finance_quotes     : 实时行情（A股/港股/美股，批量 ≤50）
- finance_ranking    : 排行（A股成交额/涨幅等，全市场或前 N）
- finance_filings    : 美股 SEC 年报/财报（EDGAR，免费公开）
- finance_macro      : 宏观指标（FRED，免费公开）
- finance_crypto     : 加密行情（CoinGecko，免费公开）
- finance_news       : 财经新闻 RSS（免费公开）
"""

from __future__ import annotations

import json
import logging
import re
import time

logger = logging.getLogger(__name__)

PLUGIN_VERSION = "1.0"

# 工具目录（MCP tools/list 可发现；description 中文说明+来源标注）
FINANCE_TOOL_REGISTRY = [
    {
        "name": "finance_quotes",
        "description": (
            "实时行情批量查询（腾讯行情免费接口，无需账号）：支持 A股(sh600519/sz000001)、"
            "港股(00700.hk/hk00700)、美股(aapl.us/aapl)代码；一次最多 50 只；"
            "返回最新价/涨跌幅/成交额/成交量等。数据源：腾讯 qt.gtimg.cn，日终后为收盘快照。"
        ),
        "parameters": {"instruction": "股票代码列表，如 'sh600519, hk00700, aapl.us'"},
        "returns": "JSON：{\"status\": \"success|failed\", \"quotes\": [{code,name,price,change_pct,amount}], \"source\": \"tencent\"}",
        "required": ["status"],
    },
    {
        "name": "finance_ranking",
        "description": (
            "A股市场排行查询（新浪全市场/东财前N 免费公开接口，无需账号）：支持成交额(amount)/涨幅(change_pct)/"
            "成交量(volume)排行；top_n=0 表示全市场（新浪 5500+ 只，约 80s）；"
            "支持前5%统计口径（如 '前5%成交额占比'）。数据源：新浪行情中心/东方财富 push2。"
        ),
        "parameters": {"instruction": "排行要求，如 '成交额前20' 或 '前5%成交额占比'"},
        "returns": "JSON：{\"status\": \"success|failed\", \"rows\": [...], \"total\": 0, \"source\": \"sina_ranking|eastmoney\"}",
        "required": ["status"],
    },
    {
        "name": "finance_filings",
        "description": (
            "美股公司 SEC 年报/财报查询（EDGAR 免费公开 API，无需账号）：按公司名/代码解析 CIK，"
            "返回最近 N 年（默认 12 年）年报的营收/净利润/毛利/总资产/经营现金流等核心指标。"
            "数据源：SEC EDGAR (data.sec.gov)，公共数据无使用限制，需遵守 10 req/s 限速。"
        ),
        "parameters": {"instruction": "公司名或代码，如 'Apple' 或 'AAPL'，可附年份范围"},
        "returns": "JSON：{\"status\": \"success|failed\", \"company\": \"\", \"cik\": \"\", \"annuals\": [{year,revenue,net_income,...}]}",
        "required": ["status"],
    },
    {
        "name": "finance_macro",
        "description": (
            "宏观经济指标查询（FRED 免费公开 CSV 接口，无需账号）：支持 GDP/CPI/失业率/"
            "联邦基金利率/PMI 等常见指标，返回近 5 年月度/季度序列。数据源：FRED (fred.stlouisfed.org)。"
        ),
        "parameters": {"instruction": "指标名，如 '美国CPI同比' 或 '联邦基金利率'"},
        "returns": "JSON：{\"status\": \"success|failed\", \"indicator\": \"\", \"series\": [{date,value}], \"source\": \"FRED\"}",
        "required": ["status"],
    },
    {
        "name": "finance_crypto",
        "description": (
            "加密货币行情查询（CoinGecko 免费公开接口，无需账号）：返回币种当前价格/"
            "24h 涨跌幅/市值/成交量。数据源：CoinGecko API。"
        ),
        "parameters": {"instruction": "币种名，如 '比特币' 或 'BTC'"},
        "returns": "JSON：{\"status\": \"success|failed\", \"coin\": \"\", \"price_usd\": 0, \"change_24h_pct\": 0, \"source\": \"coingecko\"}",
        "required": ["status"],
    },
    {
        "name": "finance_news",
        "description": (
            "财经新闻检索（Google News RSS 免费公开接口，无需账号）：按关键词返回最新"
            "相关新闻标题+来源+链接+时间。数据源：Google News RSS。"
        ),
        "parameters": {"instruction": "新闻关键词，如 'A股 成交额'"},
        "returns": "JSON：{\"status\": \"success|failed\", \"items\": [{title,source,url,published}], \"source\": \"google_news_rss\"}",
        "required": ["status"],
    },
]

_PLUGIN_NAMES = {t["name"] for t in FINANCE_TOOL_REGISTRY}


def is_finance_tool(name: str) -> bool:
    return str(name or "") in _PLUGIN_NAMES


def finance_tool_catalog() -> str:
    """规划器可用的插件工具目录文本。"""
    return "\n".join(
        f"- {t['name']}: {t['description']}" for t in FINANCE_TOOL_REGISTRY
    )


# ---------------------------------------------------------------------------
# 各工具本地执行（直接调用适配器，静默降级）
# ---------------------------------------------------------------------------

def _ok(payload: dict) -> dict:
    return {"status": "SUCCESS", "result": json.dumps(payload, ensure_ascii=False)}


def _fail(err: str) -> dict:
    return {"status": "FAILED", "result": f"金融插件调用失败: {err}"[:300]}


def _normalize_code(raw: str) -> str | None:
    """股票代码归一化为腾讯格式：
    - aapl.us / AAPL → usAAPL；00700.hk / hk00700 → hk00700；
    - sh600519 / sz000001 / 600519 / 000001 → 原样或补前缀；
    - 其他无法识别返回 None。"""
    c = str(raw or "").strip().lower()
    if not c:
        return None
    # 已带前缀：sh/sz/bj/hk/us + 数字（us 后是字母）
    if re.match(r"^(sh|sz|bj)\d{6}$", c) or re.match(r"^hk\d{5}$", c) or re.match(r"^us[a-z]{1,5}$", c):
        return c
    # 后缀格式：aapl.us / 00700.hk / 600519.sh
    m = re.match(r"^([a-z0-9]{1,6})\.(us|hk|sh|sz)$", c)
    if m:
        body, market = m.group(1), m.group(2)
        if market == "us":
            return f"us{body.upper()}"
        if market == "hk":
            return f"hk{body.zfill(5)}"
        return f"{market}{body.zfill(6)}"
    # 裸 A 股代码（6 位数字）
    if re.match(r"^\d{6}$", c):
        if c.startswith(("6", "9")):
            return f"sh{c}"
        if c.startswith(("0", "2", "3")):
            return f"sz{c}"
        if c.startswith(("4", "8")):
            return f"bj{c}"
        return None
    # 纯美股字母 ticker
    if re.match(r"^[a-z]{1,5}$", c):
        return f"us{c.upper()}"
    # 港股 5 位数字
    if re.match(r"^\d{5}$", c):
        return f"hk{c}"
    return None


def _call_quotes(instruction: str) -> dict:
    """finance_quotes：解析代码列表 → 腾讯批量行情。"""
    try:
        from adapters.tencent_quotes import fetch_quotes
        raw_codes = [c.strip() for c in re.split(r"[,\s，、;；]+", str(instruction)) if c.strip()]
        codes: list[str] = []
        skipped: list[str] = []
        for rc in raw_codes:
            n = _normalize_code(rc)
            if n:
                codes.append(n)
            else:
                skipped.append(rc)
        if not codes:
            return _fail("未能解析出股票代码（示例：600519, 00700.hk, aapl.us）")
        codes = codes[:50]
        data = fetch_quotes(codes)
        # 腾讯返回 {请求代码: {name, price, change_pct, volume, amount, ...}}（dict 非 list）
        if isinstance(data, dict):
            rows = []
            for orig, q in data.items():
                if isinstance(q, dict) and q.get("name"):
                    rows.append({
                        "code": q.get("code") or orig,
                        "name": q.get("name"),
                        "price": q.get("price"),
                        "change_pct": q.get("change_pct"),
                        "amount": q.get("amount"),
                        "volume": q.get("volume"),
                        "turnover_pct": q.get("turnover_pct"),
                        "market_cap_yi": q.get("market_cap_yi"),
                    })
        else:
            rows = data.get("quotes") or data.get("data") or [] if isinstance(data, dict) else []
        payload = {"status": "success", "quotes": rows, "requested": codes,
                   "source": "tencent", "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if skipped:
            payload["skipped"] = skipped
        if not rows:
            payload["note"] = "接口返回空（代码可能无效或停牌）"
        return _ok(payload)
    except Exception as exc:
        return _fail(str(exc)[:200])


def _call_ranking(instruction: str) -> dict:
    """finance_ranking：按规模路由新浪全市场 / 东财前 N。"""
    try:
        from adapters.router import route_structured
        data = route_structured(instruction)
        if not data:
            return _fail("排行路由未命中（候选源冷却或指令无法解析）")
        return _ok(data)
    except Exception as exc:
        return _fail(str(exc)[:200])


def _call_filings(instruction: str) -> dict:
    """finance_filings：SEC EDGAR 年报。"""
    try:
        import adapters.sec_edgar as sec
        text = str(instruction or "").strip()
        # 提取纯 ticker：大写字母代码（AAPL/MSFT/NVDA），排除常见英文单词
        m = re.search(r"\b([A-Z]{2,5})\b", text)
        ticker = ""
        if m and m.group(1).upper() not in ("AI", "AN", "THE", "FOR", "AND", "INC", "LTD", "CO", "US"):
            ticker = m.group(1)
        year_m = re.search(r"(20\d{2})\s*[-~至]\s*(20\d{2})", text)
        year_range = (int(year_m.group(1)), int(year_m.group(2))) if year_m else None
        # company：取中文名或完整文本
        company = text[:80] if not ticker or re.search(r"[\u4e00-\u9fff]", text) else text[:80]
        data = sec.fetch(company=company, ticker=ticker, year_range=year_range)
        if not data:
            return _fail(f"SEC 未返回数据（company={company}, ticker={ticker}）")
        financials = data.get("financials") or []
        if not financials:
            return _fail(f"SEC 未返回年报（company={company}, ticker={ticker}）")
        # 统一字段：financials 为年报列表（年份+核心指标），补 metadata
        payload = {
            "status": "success",
            "company": (data.get("metadata") or {}).get("company", company),
            "cik": (data.get("metadata") or {}).get("cik", ""),
            "annuals": financials,
            "unit": (data.get("metadata") or {}).get("unit", ""),
            "source": "sec_edgar",
            "retrieved_at": (data.get("metadata") or {}).get("retrieved_at", ""),
        }
        return _ok(payload)
    except Exception as exc:
        return _fail(str(exc)[:200])


def _call_macro(instruction: str) -> dict:
    """finance_macro：FRED 宏观指标。"""
    try:
        from adapters.macro import fetch_macro
        data = fetch_macro(str(instruction))
        if not data or not data.get("points"):
            return _fail("FRED 未返回指标序列（请用中文常用名，如 '美国CPI同比'、'失业率'、'联邦基金利率'）")
        return _ok(data)
    except Exception as exc:
        return _fail(str(exc)[:200])


def _call_crypto(instruction: str) -> dict:
    """finance_crypto：CoinGecko 加密行情。"""
    try:
        from adapters.coingecko import fetch_market
        data = fetch_market(str(instruction))
        if not data:
            return _fail("CoinGecko 未返回行情")
        return _ok(data)
    except Exception as exc:
        return _fail(str(exc)[:200])


def _call_news(instruction: str) -> dict:
    """finance_news：Google News RSS。"""
    try:
        from adapters.news import fetch_news
        data = fetch_news(str(instruction))
        items = data if isinstance(data, list) else data.get("items") if isinstance(data, dict) else []
        if not items:
            return _fail("新闻 RSS 未返回结果")
        return _ok({"status": "success", "items": items[:20], "keyword": str(instruction)[:60],
                    "source": "google_news_rss",
                    "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    except Exception as exc:
        return _fail(str(exc)[:200])


_HANDLERS = {
    "finance_quotes": _call_quotes,
    "finance_ranking": _call_ranking,
    "finance_filings": _call_filings,
    "finance_macro": _call_macro,
    "finance_crypto": _call_crypto,
    "finance_news": _call_news,
}


def call_finance_tool(name: str, instruction: str, timeout: float = 120) -> dict | None:
    """插件工具调用入口；未注册工具返回 None（交回主派发链）。"""
    handler = _HANDLERS.get(str(name or ""))
    if not handler:
        return None
    try:
        # 超时保护：插件工具统一在独立线程执行，超时返回失败
        import threading
        box: dict = {"result": None}
        def _run():
            box["result"] = handler(str(instruction or ""))
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=max(10, float(timeout)))
        if t.is_alive():
            return _fail(f"插件工具超时（>{int(timeout)}s）")
        return box["result"]
    except Exception as exc:
        return _fail(str(exc)[:200])


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        r = call_finance_tool(sys.argv[1], sys.argv[2])
        print(json.dumps(r, ensure_ascii=False, indent=1) if r else "未知工具")
    else:
        print(f"金融数据插件 v{PLUGIN_VERSION}，工具：")
        for t in FINANCE_TOOL_REGISTRY:
            print(f"- {t['name']}: {t['description'][:60]}...")
