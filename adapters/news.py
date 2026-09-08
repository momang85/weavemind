# -*- coding: utf-8 -*-
"""新闻适配器（F4）：Google News RSS（公开、无需 key）。

    https://news.google.com/rss/search?q=<query>&hl=zh-CN&gl=CN&ceid=CN:zh-Hans

返回：{source, query, items, metadata}；网络/解析失败返回 None。
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"


def parse_news_rss(xml_text: str, query: str = "") -> dict | None:
    """解析 Google News RSS XML → 标题/链接/时间列表。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    items: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = (item.findtext("pubDate") or "").strip()
        if not title and not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "published": published,
        })
        if len(items) >= 20:
            break
    if not items:
        return None
    return {
        "source": "google_news",
        "query": str(query or ""),
        "items": items,
        "metadata": {
            "source": "google_news",
            "url": f"{GOOGLE_NEWS_RSS}?q={quote(str(query or ''))}",
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "label": "Google News RSS 新闻列表",
        },
    }


def fetch_news(query: str) -> dict | None:
    """请求 Google News RSS；网络/解析失败返回 None。

    超时 4s：境内网络下不可达时快速失败，尽快落入 fetch_news_fallback，
    避免快答/行业预载路径白等 10s。"""
    try:
        resp = requests.get(
            GOOGLE_NEWS_RSS,
            params={"q": str(query or "头条新闻"), "hl": "zh-CN", "gl": "CN",
                    "ceid": "CN:zh-Hans"},
            timeout=4,
        )
        resp.raise_for_status()
        return parse_news_rss(resp.text, query)
    except Exception as exc:
        logger.warning("Google News RSS fetch failed: %s", exc)
        return None


def fetch_news_fallback(query: str, max_results: int = 6) -> dict | None:
    """通用文本检索兜底（境内网络下 Google News RSS 常不可达）。

    走 adapters.text_search.web_text_search：Bing HTML → ddgs 逐引擎探测
    （yandex 等境内可达引擎优先）。返回与 fetch_news 同构的
    {source, query, items[{title, link, published}], metadata}，source 如实
    标注实际引擎；失败返回 None（调用方诚实降级为 model_knowledge）。
    """
    try:
        from adapters.text_search import web_text_search
        raw = web_text_search(str(query or ""), max_results=max_results)
        if not raw:
            return None
        items = [
            {
                "title": (r.get("title") or "")[:120],
                "link": r.get("url") or "",
                "published": "",
            }
            for r in raw
            if (r.get("title") or "") and (r.get("url") or "").startswith("http")
        ][:max_results]
        if not items:
            return None
        engine = str((raw[0] or {}).get("engine") or "text_search")
        source = "bing" if engine == "bing" else "duckduckgo"
        return {
            "source": source,
            "query": str(query or ""),
            "items": items,
            "metadata": {
                "source": source,
                "url": "https://www.bing.com/search" if source == "bing"
                       else "https://duckduckgo.com",
                "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "label": "Bing 搜索结果" if source == "bing"
                         else "DuckDuckGo 搜索结果",
            },
        }
    except Exception as exc:
        logger.warning("text search fallback failed: %s", exc)
        return None
