# -*- coding: utf-8 -*-
"""抓取侧（**跑在独立可选环境**）：akshare（免费、无 token）／tushare 免费档 → 本地原始文件。

为什么要独立环境：架构约束"基础研究启动不被量化环境拖累"——主环境只读本脚本产出的文件
（`adapters/market_history.import_file`），不 import 任何数据客户端。

用法（在数据环境里）：
    <数据venv>\\Scripts\\python.exe scripts/fetch_market_history.py --codes 002304,600031 --benchmark 000300

许可与权利（写进产出，不用时也留着）：
- akshare：无正式许可 → 标 `免费源·仅内部试验`，**不得**进对外下载包。
- tushare 免费档（120 积分）：仅**非复权日线**，个人非商业；要复权/分钟/实时须购买对应档位。
- `available_at` 按"EOD 收盘后可得"约定写 `<date>T15:00:00+08:00`——**这是约定，不是验证过的公告级时间**。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

OUT_DIR = Path(os.environ.get("WEAVEMIND_MARKET_RAW")
               or (Path(os.environ.get("TEMP") or ".") / "wm_market_raw"))
COLUMNS = ["code", "date", "open", "high", "low", "close", "volume", "amount",
           "adj_factor", "available_at", "source", "role", "adj_basis"]


def _six(code: str) -> str:
    s = "".join(ch for ch in str(code) if ch.isdigit())
    return s[-6:].zfill(6) if s else ""


def _avail(date_str: str) -> str:
    return f"{date_str}T15:00:00+08:00"


def _row(code: str, date_str: str, o, h, l, c, vol, amt, adj, source: str, role: str,
         basis: str) -> dict:
    return {"code": code, "date": date_str, "open": o, "high": h, "low": l, "close": c,
            "volume": vol, "amount": amt, "adj_factor": adj, "available_at": _avail(date_str),
            "source": source, "role": role, "adj_basis": basis}


def _prefix(code: str) -> str:
    c = _six(code)
    return ("sh" if c[0] in "569" else "bj" if c[0] in "48" else "sz") + c


def fetch_akshare_stock(code: str, start: str, end: str, role: str) -> tuple[list[dict], str]:
    """A 股日线：**优先新浪**（`stock_zh_a_daily`），失败再退东财（`stock_zh_a_hist`）。

    实机（2026-10-05）：本机到东财 push2his 的连接被对端直接断开（RemoteDisconnected），
    新浪可用（洋河 908 行／沪深300 6003 行）；两条都失败才报错，不静默。
    """
    import akshare as ak
    errors = []
    try:
        df = ak.stock_zh_a_daily(symbol=_prefix(code), start_date=start, end_date=end,
                                 adjust="qfq")
        rows = [_row(code, str(r.get("date"))[:10], r.get("open"), r.get("high"),
                     r.get("low"), r.get("close"), r.get("volume"), r.get("amount"),
                     None, "akshare.stock_zh_a_daily(sina)", role, "前复权(qfq)")
                for _, r in df.iterrows()]
        return rows, f"akshare/新浪 前复权 {len(rows)} 行"
    except Exception as exc:                             # noqa: BLE001
        errors.append(f"sina: {type(exc).__name__}")
    df = ak.stock_zh_a_hist(symbol=_six(code), period="daily", start_date=start,
                            end_date=end, adjust="qfq")
    rows = [_row(code, str(r.get("日期"))[:10], r.get("开盘"), r.get("最高"), r.get("最低"),
                 r.get("收盘"), r.get("成交量"), r.get("成交额"), None,
                 "akshare.stock_zh_a_hist(eastmoney)", role, "前复权(qfq)")
            for _, r in df.iterrows()]
    return rows, f"akshare/东财 前复权 {len(rows)} 行（{'/'.join(errors)} 先失败）"


def fetch_akshare_index(code: str, start: str, end: str, role: str) -> tuple[list[dict], str]:
    """指数日线：**优先新浪** `stock_zh_index_daily`，失败再退东财 `index_zh_a_hist`。"""
    import akshare as ak
    errors = []
    # 指数代码前缀与个股不同名空间：000xxx 是中证/上证系列（sh），399xxx 是深证系列（sz）
    _pref = ("sh" if _six(code).startswith("000") else
             "sz" if _six(code).startswith("399") else _prefix(code))
    try:
        df = ak.stock_zh_index_daily(symbol=_pref + _six(code))
        df = df[(df["date"].astype(str) >= f"{start[:4]}-{start[4:6]}-{start[6:8]}")
                & (df["date"].astype(str) <= f"{end[:4]}-{end[4:6]}-{end[6:8]}")]
        rows = [_row(code, str(r.get("date"))[:10], r.get("open"), r.get("high"),
                     r.get("low"), r.get("close"), r.get("volume"), None, None,
                     "akshare.stock_zh_index_daily(sina)", role, "指数（不复权）")
                for _, r in df.iterrows()]
        return rows, f"akshare/新浪 指数 {len(rows)} 行"
    except Exception as exc:                             # noqa: BLE001
        errors.append(f"sina: {type(exc).__name__}")
    df = ak.index_zh_a_hist(symbol=_six(code), period="daily", start_date=start, end_date=end)
    rows = [_row(code, str(r.get("日期"))[:10], r.get("开盘"), r.get("最高"), r.get("最低"),
                 r.get("收盘"), r.get("成交量"), r.get("成交额"), None,
                 "akshare.index_zh_a_hist(eastmoney)", role, "指数（不复权）")
            for _, r in df.iterrows()]
    return rows, f"akshare/东财 指数 {len(rows)} 行（{'/'.join(errors)} 先失败）"


def fetch_tushare_stock(code: str, start: str, end: str, role: str) -> tuple[list[dict], str]:
    """tushare 免费档：**非复权日线**（120 积分可用）；需环境变量 `TUSHARE_TOKEN`。"""
    import tushare as ts
    token = str(os.environ.get("TUSHARE_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("缺 TUSHARE_TOKEN（免费档也要注册取 token）")
    ts.set_token(token)
    df = ts.pro_bar(ts_code=f"{_six(code)}.SZ" if _six(code).startswith(("0", "3"))
                    else f"{_six(code)}.SH", start_date=start, end_date=end, adj=None)
    rows = []
    for _, r in df.iterrows():
        d = str(r.get("trade_date"))
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        rows.append(_row(code, d, r.get("open"), r.get("high"), r.get("low"), r.get("close"),
                         r.get("vol"), r.get("amount"), None, "tushare.pro_bar(adj=None)",
                         role, "不复权"))
    return rows, f"tushare 不复权 {len(rows)} 行"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="002304,600031", help="标的主体（逗号分隔）")
    ap.add_argument("--benchmark", default="000300", help="基准指数代码（如 000300）")
    ap.add_argument("--start", default="20230101")
    ap.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--source", default="akshare", choices=["akshare", "tushare", "both"])
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    notes: list[str] = []
    errors: list[str] = []
    for code in [c.strip() for c in str(args.codes).split(",") if c.strip()]:
        try:
            r, n = (fetch_akshare_stock(code, args.start, args.end, "subject")
                    if args.source in ("akshare", "both") else
                    fetch_tushare_stock(code, args.start, args.end, "subject"))
            rows += r
            notes.append(f"{code}: {n}")
        except Exception as exc:                          # noqa: BLE001
            errors.append(f"{code} 抓取失败：{type(exc).__name__}: {str(exc)[:120]}")
    if args.benchmark:
        try:
            r, n = fetch_akshare_index(args.benchmark, args.start, args.end, "benchmark")
            rows += r
            notes.append(f"{args.benchmark}(基准): {n}")
        except Exception as exc:                          # noqa: BLE001
            errors.append(f"基准 {args.benchmark} 抓取失败：{type(exc).__name__}: {str(exc)[:120]}")

    if not rows:
        print(json.dumps({"ok": False, "notes": notes, "errors": errors},
                         ensure_ascii=False, indent=1))
        return 1
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw = OUT_DIR / f"market_{stamp}.csv"
    with raw.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    side = raw.with_suffix(".meta.json")
    side.write_text(json.dumps({
        "source": str(args.source), "license": "免费源·仅内部试验",
        "adj_basis": "akshare=前复权(qfq)；tushare 免费档=不复权",
        "available_at_basis": "EOD 收盘后可得（约定 <date>T15:00:00+08:00，非公告级验证时间）",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rows": len(rows), "notes": notes, "errors": errors,
        "usage_warning": "免费源与个人非商业许可不得进对外下载包；对外交付须机构授权或只交付衍生结论",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "raw": str(raw), "sidecar": str(side),
                      "rows": len(rows), "notes": notes, "errors": errors},
                     ensure_ascii=False, indent=1))
    print("下一步（主环境）：python -c \"from adapters import market_history as m;"
          f"print(m.import_file(r'{raw}', source='{args.source}',"
          "license=m.LICENSE_FREE_TRIAL, adj_basis='前复权(qfq)'))\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
