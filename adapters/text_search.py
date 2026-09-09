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


def _search_ddg(query: str, max_results: int, timeout: float) -> list[dict]:
    """ddgs 逐引擎探测：首个出结果的引擎收敛，只走已存活引擎。"""
    from ddgs import DDGS

    out: list[dict] = []
    with DDGS(timeout=timeout) as ddgs:
        for eng in _DDG_ENGINES:
            try:
                results = list(
                    ddgs.text(
                        str(query or ""), backend=eng, max_results=max_results,
                    ),
                )
            except Exception as exc:
                logger.debug(
                    "text_search ddg engine %s failed: %s",
                    eng, str(exc)[:80],
                )
                continue
            if not results:
                continue
            for r in results:
                if not isinstance(r, dict):
                    continue
                title = str(r.get("title") or "").strip()
                href = str(r.get("href") or "").strip()
                if title and href.startswith("http"):
                    out.append({
                        "title": title,
                        "url": href,
                        "snippet": str(r.get("body") or ""),
                        "engine": f"duckduckgo:{eng}",
                    })
            break  # 本引擎出结果即收敛，不再试后续引擎
    return out


def web_text_search(query: str, max_results: int = 6, timeout: float = 3) -> list[dict]:
    """轻量文本检索：Bing（含引号精确变体）→ ddg 引擎探测合并。

    查询先经 search_quality 预处理（去指令包装/提取关键词），中文
    长目标不再原样直塞引擎；结果统一相关性计分 + 权威域加权排序，
    与主题无关的条目（如"固态硬盘"之于"固态电池"）被滤除。
    全部失败返回空列表（不抛）。"""
    q = str(query or "").strip()
    if not q:
        return []
    variants = build_query_variants(q) or [q]
    collected: list[dict] = []
    seen: set[str] = set()

    def _add(items) -> None:
        for it in items or []:
            if not isinstance(it, dict):
                continue
            u = str(it.get("url") or "")
            if u and u not in seen:
                seen.add(u)
                collected.append(it)

    # Bing 主变体；相关结果不足 3 条时追加带引号变体（每变体 ≤1 次请求）
    for v in variants[:2]:
        try:
            _add(_search_bing(v, max_results * 2))
        except Exception as exc:
            logger.warning("text_search bing failed: %s", exc)
        if len(score_results(q, collected)) >= 3 or len(variants) <= 1:
            break
    # ddg 引擎探测合并：Bing 相关结果不足时补充（yandex 等境内可达
    # 引擎）；门槛按"计分后相关条数"判定——Bing 常灌入大量主题不符
    # 的条目（如固态硬盘），原始条数充足不代表相关条数充足
    if len(score_results(q, collected)) < max_results:
        try:
            _add(_search_ddg(q, max_results * 2, timeout))
        except Exception as exc:
            logger.warning("text_search ddg failed: %s", exc)
    ranked = score_results(q, collected)
    return ranked[:max_results]
