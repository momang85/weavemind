# -*- coding: utf-8 -*-
"""抓取侧（跑在**独立数据环境**）：交易日历 → 本地 CSV。

为什么单独取：缺独立日历时，"标的当天没有行情"分不清**停牌**还是**休市**，
`event_returns` 只能报"疑似停牌"、`backtest` 只能用基准序列当代理。日历一次取全、
长期不变，属于**基础数据**。

用法（数据环境）：
    <数据venv>\\Scripts\\python.exe scripts/fetch_trading_calendar.py

来源与边界：
- 新浪交易日历（akshare `tool_trade_date_hist_sina`）：**无正式许可** ⇒ 标 `免费源·仅内部试验`，
  不得进对外下载包。要对外交付请用授权源重取（契约不变，换来源即可）。
- 只落 `trade_date` 一列：日历不含停牌信息（停牌是**标的级**事实，由"该交易日无行情行"推出）。
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

OUT_DIR = Path(os.environ.get("WEAVEMIND_CALENDAR_RAW")
               or (Path(os.environ.get("TEMP") or ".") / "wm_calendar_raw"))


def main() -> int:
    try:
        import akshare as ak
    except Exception as exc:                                  # noqa: BLE001
        print(json.dumps({"ok": False, "reason": f"数据环境缺 akshare：{exc}"},
                         ensure_ascii=False))
        return 1
    try:
        df = ak.tool_trade_date_hist_sina()
    except Exception as exc:                                  # noqa: BLE001
        print(json.dumps({"ok": False, "reason": f"日历抓取失败：{type(exc).__name__}: "
                                                 f"{str(exc)[:120]}"}, ensure_ascii=False))
        return 1
    col = "trade_date" if "trade_date" in df.columns else df.columns[0]
    dates = sorted({str(v)[:10] for v in df[col].tolist() if str(v).strip()})
    if not dates:
        print(json.dumps({"ok": False, "reason": "日历为空"}, ensure_ascii=False))
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw = OUT_DIR / f"calendar_{stamp}.csv"
    with raw.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "session"])
        for d in dates:
            w.writerow([d, 1])
    side = raw.with_suffix(".meta.json")
    side.write_text(json.dumps({
        "source": "akshare.tool_trade_date_hist_sina(sina)", "market": "CN",
        "license": "免费源·仅内部试验",
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "rows": len(dates), "range": [dates[0], dates[-1]],
        "usage_warning": "无正式许可的来源，仅内部试验，不得进对外下载包",
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "raw": str(raw), "sidecar": str(side),
                      "rows": len(dates), "range": [dates[0], dates[-1]]},
                     ensure_ascii=False, indent=1))
    print("下一步（主环境）：python -c \"from adapters import trading_calendar as tc;"
          f"print(tc.import_file(r'{raw}', source='akshare/sina'))\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
