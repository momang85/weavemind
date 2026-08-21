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
    """请求 Google News RSS；网络/解析失败返回 None。"""
    try:
        resp = requests.get(
            GOOGLE_NEWS_RSS,
            params={"q": str(query or "头条新闻"), "hl": "zh-CN", "gl": "CN",
                    "ceid": "CN:zh-Hans"},
            timeout=10,
        )
        resp.raise_for_status()
        return parse_news_rss(resp.text, query)
    except Exception as exc:
        logger.warning("Google News RSS fetch failed: %s", exc)
        return None
