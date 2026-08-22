# -*- coding: utf-8 -*-
"""宏观指标适配器（F4）：FRED 公开 CSV。

    GET https://fred.stlouisfed.org/graph/fredgraph.csv?id=GDP
    支持：GDP（国内生产总值）、CPIAUCSL（CPI 通胀）、UNRATE（失业率）。

返回：{indicator, series, points, metadata}；网络/解析失败返回 None。
"""

from __future__ import annotations

import csv
import io
import logging
import time

import requests

logger = logging.getLogger(__name__)

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# 用户常用说法 → FRED series id
INDICATOR_ALIASES = {
    "gdp": "GDP",
    "国内生产总值": "GDP",
    "生产总值": "GDP",
    "cpi": "CPIAUCSL",
    "通胀": "CPIAUCSL",
    "通货膨胀": "CPIAUCSL",
    "消费者物价指数": "CPIAUCSL",
    "unrate": "UNRATE",
    "失业率": "UNRATE",
    "失业": "UNRATE",
    # P1-1：利率/降息/美联储类目标路由到联邦基金有效利率
    "dff": "DFF",
    "利率": "DFF",
    "降息": "DFF",
    "加息": "DFF",
    "美联储": "DFF",
    "联邦基金利率": "DFF",
}


def series_id(indicator: str) -> str:
    """把用户输入归一化为 FRED series id；未知默认 GDP。"""
    key = str(indicator or "").strip().lower()
    if key in INDICATOR_ALIASES:
        return INDICATOR_ALIASES[key]
    if str(indicator or "").strip().upper() in ("GDP", "CPIAUCSL", "UNRATE"):
        return str(indicator).strip().upper()
    return "GDP"


def parse_macro_csv(text: str, indicator: str = "GDP") -> dict | None:
    """解析 FRED CSV：DATE,<series>[,...]；返回最近至多 240 个点。"""
    sid = series_id(indicator)
    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception:
        return None
    if not rows:
        return None
    points: list[dict] = []
    for r in rows:
        date = str(r.get("DATE") or "").strip()
        raw = str(r.get(sid) or "").strip()
        if not date or not raw or raw in (".", "NA", ""):
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        points.append({"date": date, "value": value})
    if not points:
        return None
    points.sort(key=lambda p: p["date"])
    return {
        "indicator": sid,
        "series": sid,
        "points": points[-240:],
        "metadata": {
            "source": "fred",
            "url": f"{FRED_CSV_URL}?id={sid}",
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "label": {
                "GDP": "美国国内生产总值 GDP",
                "CPIAUCSL": "美国 CPI 消费者物价指数",
                "UNRATE": "美国失业率",
                "DFF": "美国联邦基金有效利率 DFF",
            }.get(sid, sid),
        },
    }


def fetch_macro(indicator: str) -> dict | None:
    """请求 FRED CSV；网络/解析失败返回 None。"""
    sid = series_id(indicator)
    try:
        resp = requests.get(FRED_CSV_URL, params={"id": sid}, timeout=10)
        resp.raise_for_status()
        return parse_macro_csv(resp.text, sid)
    except Exception as exc:
        logger.warning("FRED fetch failed: %s", exc)
        return None
