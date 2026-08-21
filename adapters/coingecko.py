# -*- coding: utf-8 -*-
"""CoinGecko 加密货币行情适配器（F4）。

公开免费 API（无需 key）：
    GET https://api.coingecko.com/api/v3/simple/price
        ?ids=bitcoin&vs_currencies=usd
        &include_market_cap=true&include_24hr_vol=true
        &include_24hr_change=true&include_last_updated_at=true

返回（flat 结构，含 metadata，符合适配器约定）：
    {price, market_cap, volume_24h, change_24h, metadata}
网络/解析失败返回 None（调用方回退搜索链路）。
"""

from __future__ import annotations

import logging
import re
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"

# 常见中文/别名 → CoinGecko coin id
COIN_ALIASES = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "比特币": "bitcoin",
    "大饼": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "以太坊": "ethereum",
    "二饼": "ethereum",
    "sol": "solana",
    "solana": "solana",
    "索拉纳": "solana",
    "bnb": "binancecoin",
    "币安币": "binancecoin",
    "usdt": "tether",
    "usdc": "usd-coin",
    "xrp": "ripple",
    "瑞波": "ripple",
    "doge": "dogecoin",
    "狗狗币": "dogecoin",
}


def coin_id(coin: str) -> str:
    """把用户输入归一化为 CoinGecko coin id；无法识别时取首个小写 token。"""
    raw = str(coin or "").strip().lower()
    if raw in COIN_ALIASES:
        return COIN_ALIASES[raw]
    if raw:
        s = re.sub(r"[^a-z0-9-]", "", raw)
        return s or "bitcoin"
    return "bitcoin"


def parse_market(payload: dict, coin: str, vs_currency: str = "usd") -> dict | None:
    """解析 simple/price 响应；结构不符返回 None（供 canned 测试）。"""
    cid = coin_id(coin)
    entry = payload.get(cid) if isinstance(payload, dict) else None
    if not isinstance(entry, dict):
        return None
    return {
        "price": entry.get("usd"),
        "market_cap": entry.get("usd_market_cap"),
        "volume_24h": entry.get("usd_24h_vol"),
        "change_24h": entry.get("usd_24h_change"),
        "metadata": {
            "source": "coingecko",
            "coin": cid,
            "vs_currency": vs_currency,
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "api": f"{BASE_URL}/simple/price",
            "label": f"{cid} 实时行情",
        },
    }


def fetch_market(coin: str, vs_currency: str = "usd") -> dict | None:
    """请求 CoinGecko 实时行情；网络/解析失败返回 None。"""
    cid = coin_id(coin)
    try:
        resp = requests.get(
            f"{BASE_URL}/simple/price",
            params={
                "ids": cid,
                "vs_currencies": vs_currency,
                "include_market_cap": "true",
                "include_24hr_vol": "true",
                "include_24hr_change": "true",
                "include_last_updated_at": "true",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return parse_market(resp.json(), cid, vs_currency)
    except Exception as exc:
        logger.warning("CoinGecko fetch failed: %s", exc)
        return None
