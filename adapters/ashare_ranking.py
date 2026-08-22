# -*- coding: utf-8 -*-
"""A股行情排行适配器（东方财富行情中心 clist 接口）。

接口（已勘探验证）：
    GET https://push2.eastmoney.com/api/qt/clist/get
        ?pn=1&pz=10&po=1&np=1&fltt=2&invt=2
        &fid=f6&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23
        &fields=f2,f3,f5,f6,f8,f12,f14,f20
    - fid=f6 按成交额降序；fid=f5 按成交量降序；
    - fs 覆盖沪深京 A 股；
    - f2 最新价 / f3 涨跌幅% / f5 成交量(手) / f6 成交额(元) /
      f8 换手率% / f12 代码 / f14 名称 / f20 总市值(元)。
无需 API Key，失败时由调用方回退通用搜索链路。
"""

import json
import time
import urllib.parse
import urllib.request

_BASE = "https://push2.eastmoney.com/api/qt/clist/get"
_FS_A_SHARE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
_FIELDS = "f2,f3,f5,f6,f8,f12,f14,f20"


def _get(url: str, timeout: int = 25) -> str:
    """GET 文本（与 eastmoney 财务适配器同款 UA，便于统一维护）。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _api_url(metric: str = "amount", top_n: int = 10) -> str:
    """构造排行接口 URL：metric=volume 按成交量，其余按成交额。"""
    fid = "f5" if metric == "volume" else "f6"
    qs = urllib.parse.urlencode({
        "pn": 1,
        "pz": max(1, min(int(top_n), 50)),
        "po": 1,          # 降序
        "np": 1,
        "fltt": 2,
        "invt": 2,
        "fid": fid,
        "fs": _FS_A_SHARE,
        "fields": _FIELDS,
    })
    return f"{_BASE}?{qs}"


def _num(raw, scale: float = 1.0) -> float | None:
    """原始值 → 数值；'-' 等缺失标记返回 None。"""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return round(v * scale, 2) if scale != 1.0 else v


def fetch_ranking(metric: str = "amount", top_n: int = 10) -> dict:
    """获取 A股 成交额/成交量排行前 N。

    Args:
        metric: "volume"（成交量）或 "amount"（成交额，默认）。
        top_n: 返回条数，默认 10。

    Returns:
        {rows, metric, top_n, source_url, retrieved_at}
        rows 元素：rank/code/name/price/change_pct/volume_hand/
        volume_wan_hand/amount_yuan/amount_yi/turnover_pct/market_cap_yi。
        失败抛异常（调用方回退搜索链路）。
    """
    url = _api_url(metric, top_n)
    text = _get(url)
    data = json.loads(text)
    rows_raw = ((data.get("data") or {}).get("diff")) or []
    if not rows_raw:
        raise RuntimeError("EastMoney 行情排行无数据")
    rows: list[dict] = []
    for i, r in enumerate(rows_raw[:top_n], 1):
        amount_yuan = _num(r.get("f6"))
        volume_hand = _num(r.get("f5"))
        rows.append({
            "rank": i,
            "code": str(r.get("f12") or ""),
            "name": str(r.get("f14") or ""),
            "price": _num(r.get("f2")),
            "change_pct": _num(r.get("f3")),
            "volume_hand": volume_hand,
            "volume_wan_hand": (
                round(volume_hand / 1e4, 2) if volume_hand is not None else None
            ),
            "amount_yuan": amount_yuan,
            "amount_yi": (
                round(amount_yuan / 1e8, 2) if amount_yuan is not None else None
            ),
            "turnover_pct": _num(r.get("f8")),
            "market_cap_yi": _num(r.get("f20"), 1e-8),
        })
    return {
        "rows": rows,
        "metric": metric,
        "top_n": len(rows),
        "source_url": url,
        "retrieved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


if __name__ == "__main__":
    import sys
    metric = sys.argv[1] if len(sys.argv) > 1 else "amount"
    result = fetch_ranking(metric, 10)
    print(json.dumps(result, ensure_ascii=False, indent=1))
