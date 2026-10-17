# 10-06 定向纯计算反例

复核代码 HEAD `733ec89`，由只读量化审查子代理执行并向主代理回传。无联网、无模型调用、无服务操作。以下保留输入构造及观察输出；本文件是审查证据，不是实现或新增测试。未反复重跑。其后尝试临时目录的发布验证遭权限拒绝，未绕过、未取得发布路径实测结论。

## 评价日未挡住未来结果

```python
import json
from quant_research import event_returns as er

p = {
    "schema": "market_history/0", "dataset_id": "fixture",
    "source": "synthetic", "license": "免费源·仅内部试验",
    "adj_basis": "前复权(qfq)", "rows": 4,
    "instruments": ["A"], "date_range": ["2025-01-02", "2025-01-07"],
    "data": [
        {"code": "A", "date": d, "close": c, "open": c,
         "available_at": "2026-01-01"}
        for d, c in [("2025-01-02", 100), ("2025-01-03", 110),
                     ("2025-01-06", 120), ("2025-01-07", 130)]
    ],
}
r = er.compute(p, [{"code": "A", "date": "2025-01-02", "label": "fixture event"}],
               offsets=(0, 1), evaluation_as_of="2025-01-02")
print(json.dumps({"evaluation_as_of": r["readings"][0]["evaluation_as_of"],
                  "points": r["readings"][0]["points"],
                  "available_at_upper_bound": r["available_at_upper_bound"]},
                 ensure_ascii=False))
```

关键观察输出：

```json
{
  "evaluation_as_of": "2025-01-02",
  "points": [
    {"offset": -1, "date": "2025-01-02", "close": 100.0, "return": null, "state": "anchor"},
    {"offset": 0, "date": "2025-01-03", "close": 110.0, "return": 0.1, "state": "observed"},
    {"offset": 1, "date": "2025-01-06", "close": 120.0, "return": 0.2, "state": "observed"}
  ],
  "available_at_upper_bound": "2026-01-01"
}
```

这验证了无日历分支；有日历分支存在相同“有行即 observed”代码，本轮未另跑有日历反例。不能以日期字段已记录替代时点生效。

## 等权组合的列表顺序改变收益

```python
import json
from quant_research import backtest as bt

rows = []
for d, a, b in [("2025-01-02", 10, 10), ("2025-01-31", 5, 15),
                ("2025-02-03", 5, 15), ("2025-02-04", 10, 15)]:
    for c, v in [("A", a), ("B", b)]:
        rows.append({"date": d, "code": c, "open": v, "close": v,
                     "available_at": d})
p = {
    "schema": "market_history/0", "dataset_id": "fixture",
    "source": "synthetic", "license": "unknown", "adj_basis": "qfq",
    "rows": len(rows), "instruments": ["A", "B"],
    "date_range": ["2025-01-02", "2025-02-04"], "data": rows,
}
for u in [["A", "B"], ["B", "A"]]:
    r = bt.run(p, {"universe": u, "initial_cash": 1000, "lot_size": 1},
               costs={k: 0 for k in bt.DEFAULT_COSTS})
    print(json.dumps({
        "universe": u, "total_return": r["metrics"]["total_return"],
        "final": r["nav"][-1], "audit": r["audit"]["ok"],
        "feb_orders": [{k: o[k] for k in ["code", "side", "qty", "cash_after"]}
                       for o in r["orders"] if o["date"] == "2025-02-03"],
    }, ensure_ascii=True))
```

未传 calendar / benchmark / dividends，其他默认参数 monthly/equal/next_open、cash_buffer=0。

| universe | total_return | final.cash | final.total | 最后持仓 | audit.ok | 2 月再平衡 |
|---|---:|---:|---:|---|---|---|
| A,B | 0.25 | 240 | 1250 | A=50, B=34 | true | B 卖16，现金240 |
| B,A | 0.49 | 0 | 1490 | B=34, A=98 | true | B 卖16，再 A 买48 |

单一合成反例足以拒绝“顺序无关的等权结果已经验证”，不用于估计真实策略的修正后收益。
