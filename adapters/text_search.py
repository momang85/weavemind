# -*- coding: utf-8 -*-
"""通用文本检索（境内可达）：Bing HTML 解析 → ddgs 逐引擎探测。

Google News RSS 在境内网络不可达，ddgs 的 auto 后端又会串行尝试全部引擎
（yahoo/wikipedia 等常超时，单个查询白等数十秒）。本模块提供轻量检索链：

1. Bing HTML（无 API Key，实测境内可达且快）；
2. ddgs 按引擎顺序探测（yandex 等境内可达引擎优先），首个出结果的引擎
   即收敛，不白等已知死引擎；
3. 全部失败返回空列表（调用方诚实降级为 model_knowledge）。

与 worker_base.SearchAgent 的检索链同源思路，但独立轻量实现——不引入
worker_base 的 Redis/messaging 重依赖，供 news 适配器等轻调用方复用。
"""

from __future__ import annotations

import base64
import logging
import re
import urllib.parse
import urllib.request

from adapters.search_quality import (
    build_query_variants,
    score_results,
)

logger = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
_BING_BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
# 检索仅允许固定外网主机（Bing 搜索页），查询词只作为参数编码进 URL，
# 杜绝用户输入影响请求目标主机（SSRF 防御：协议 + 主机双白名单）。
_ALLOWED_FETCH_HOSTS = ("www.bing.com",)
# 引擎探测顺序：境内可达（yandex）优先，其余按通用可用性排序
_DDG_ENGINES = (
    "yandex", "brave", "duckduckgo", "mojeek", "startpage",
    "yahoo", "google", "wikipedia", "grokipedia",
)


def _fetch_bing_html(query: str) -> str:
    """抓取 Bing 搜索页 HTML。目标主机白名单固定，仅查询参数动态。"""
    url = (
        "https://www.bing.com/search?q="
        + urllib.parse.quote(str(query or ""))
        + "&setlang=zh-hans"
    )
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_FETCH_HOSTS:
        raise ValueError(f"disallowed fetch host: {parsed.hostname!r}")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _search_bing(query: str, max_results: int) -> list[dict]:
    """Bing HTML 结果解析（无 API Key）。返回 [{title,url,snippet,engine}]。"""
    html = _fetch_bing_html(query)
    out: list[dict] = []
    for block in _BING_BLOCK_RE.findall(html)[: max_results * 2]:
        m_title = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        m_url = re.search(r'<h2[^>]*>.*?<a[^>]+href="(https?://[^"]+)"', block, re.S)
        if not m_title or not m_url:
            m_url = re.search(r'<a[^>]+href="(https?://[^"]+)"', block)
            if not m_url:
                continue
        m_snip = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        strip = lambda s: re.sub(r"<[^>]+>", "", s or "").strip()
        target = m_url.group(1)
        if "bing.com/ck/a" in target:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(target).query)
            u = params.get("u", [""])[0]
            if u:
                try:
                    # Bing 跳转负载为无 padding 的 urlsafe base64，补足后再解
                    payload = u[2:]
                    payload += "=" * (-len(payload) % 4)
                    u = base64.urlsafe_b64decode(
                        payload.encode(),
                    ).decode("utf-8", errors="replace")
                except Exception:
                    u = urllib.parse.unquote(u)
            if u.startswith("http"):
                target = u
        title = strip(m_title.group(1) if m_title else "")
        if title and target.startswith("http"):
            out.append({
                "title": title,
                "url": target,
                "snippet": strip(m_snip.group(1) if m_snip else ""),
                "engine": "bing",
            })
        if len(out) >= max_results:
            break
    return out


def _first_available_engine() -> str:
    """策略清单 ∩ ddgs 实际可用后端的第一个（读库不发请求）。

    包内 ddgs 9.16 已不含 yandex，而清单首位就是它——照清单打会先白等几十秒（S0 实测）。
    """
    advertised: tuple = ()
    try:
        from ddgs import DDGS
        for attr in ("get_available_backends", "available_backends"):
            fn = getattr(DDGS, attr, None)
            if callable(fn):
                got = fn()
                if got:
                    advertised = tuple(str(x) for x in got)
                    break
        if not advertised:
            val = getattr(DDGS, "BACKENDS", None)
            advertised = tuple(str(x) for x in val) if val else ()
    except Exception:
        advertised = ()
    wanted = tuple(_DDG_ENGINES)
    if not advertised:
        return wanted[0] if wanted else ""
    for eng in wanted:
        if eng in advertised:
            return eng
    return ""


def _search_ddg(query: str, max_results: int, timeout: float) -> list[dict]:
    """ddgs **单后端单次**调用（S1）：不再逐引擎阶梯，也不再回落 auto。"""
    from ddgs import DDGS

    engine = _first_available_engine()
    out: list[dict] = []
    with DDGS(timeout=max(3.0, float(timeout))) as ddgs:
        rows = list(ddgs.text(str(query or ""), backend=engine,
                              max_results=max_results))
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()
        href = str(r.get("href") or "").strip()
        if title and href.startswith("http"):
            out.append({
                "title": title,
                "url": href,
                "snippet": str(r.get("body") or ""),
                "engine": f"duckduckgo:{engine}",
            })
    return out


def web_text_search(query: str, max_results: int = 6, timeout: float = 3) -> list[dict]:
    """轻量文本检索：收敛到**同一个有界执行器**（S1），Bing 主 + ddgs 单备后端。

    与 worker 同一条纪律：一个预算（次数 + 截止）、不 auto 全扫、真零结果不触发熔断、
    同一 (查询, 后端) 不重复提交。查询先经 search_quality 预处理，结果统一计分排序；
    全部失败返回空列表（不抛），并如实标注原因由调用方决定。
    """
    import os as _os

    from adapters.search_runner import SearchBudget, run_search

    q = str(query or "").strip()
    if not q:
        return []
    variants = build_query_variants(q) or [q]
    try:
        calls = int(_os.environ.get("WM_SEARCH_MAX_CALLS", "") or 6)
    except Exception:
        calls = 6
    budget = SearchBudget(max_calls=calls,
                          deadline_seconds=float(timeout or 3.0) * 2)
    specs = [{"provider": "bing", "backend": "www.bing.com"}]
    engine = _first_available_engine()
    if engine:
        specs.append({"provider": "ddgs", "backend": engine})

    def _call(provider: str, backend: str, text: str, wait: float) -> list[dict]:
        if provider == "bing":
            return _search_bing(text, max_results * 2)
        return _search_ddg(text, max_results * 2, min(float(wait), 8.0))

    outcome = run_search(variants, call_provider=_call, providers=specs,
                         budget=budget, max_results=max_results)
    ranked = score_results(q, outcome.to_legacy_items())
    return ranked[:max_results]
