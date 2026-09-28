# P0-e：时间线区分「发布意图」与「确实发布」（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §2 第 5 条
「时间线区分 publish_intent、实际 published、consumed、started；发布前不能记'已经发布成功'」。
基线 `b94c181`（P0-d 2/2）。

## 缺陷

`web_ui._publish_task` 在**调用 `r.publish` 之前**就把两段事件写进收执：

```python
_events = [
    {"event": "received", ...},
    {"event": "published", "detail": "已发布到 orchestrator:main，等待收执"},  # ← 还没发就写了
]
...claim_receipt(..., submit_events=_events)
r.publish("orchestrator:main", ...)
```

于是 `r.publish` 抛异常（Redis 掉线）或进程在两步之间死掉时，**时间线已经在说
"已发布成功"**——事后对账无法区分"没发出去"和"发出去了但编排器没消费"，
排障会被这条假记录带偏。

## 修复

- 事件词表（`task_state.SUBMIT_EVENTS`）新增 **`publish_intent`**，
  与 `published` 分成两个语义：
  - `publish_intent`：**打算**发布（落收执时写，与收执同一时刻）；
  - `published`：**确实**发布成功（`r.publish` 返回之后才写）。
- `_publish_task` 落收执时只带 `received` + `publish_intent`（下发给编排器的
  `submit_events` 也是这两段）。
- `r.publish` 包上 try/except：失败 → **不记 published**、记 error 日志、
  抛 `RuntimeError("任务发布失败（收执已保留，将自动重试）：…")`。
  收执仍在库里（RECEIVED），由启动/周期恢复捡起（P0-c 已有）。
- 发布成功后由 web_ui 补记真实 `published`（`record_submit_event`）。

对账口径现在是四段可分：`publish_intent` → `published` → `consumed` → `started`。

## 定向验证

`test_writer_consolidation` 新增/更新 3 项：

| 用例 | 断言 |
|---|---|
| `test_payload_carries_key_and_submit_timeline`（更新） | 下发给编排器的 `submit_events` 恰为 **`["received","publish_intent"]`**（不再预写 published） |
| `test_publish_failure_records_intent_but_not_published`（新） | `r.publish` 抛错 → 抛 RuntimeError；库里时间线**有** `publish_intent`、**没有** `published` |
| `test_published_is_recorded_only_after_real_publish`（新） | 成功路径：`publish` 被调用一次；时间线同时有二者且 `publish_intent` **排在** `published` 之前 |

结果：`test_writer_consolidation` **60 项 OK**（58 + 2）。

## 未验项

- **未在真实 Redis 上制造发布失败**（如中途停 Redis）来观察时间线与恢复的联动；
  失败路径是单测覆盖（`publish` 替身抛错）。
- 时间线写入是"读-改-写"（`record_submit_event`），web_ui 补记 `published` 与编排器
  并发写 `consumed` 时**理论上有丢更新的窗口**（本批未改并发写协议，如实记录）。
- 编排器侧 `find_by_idempotency` 仍为**全局**比对（未带作用域），与提交侧语义尚未统一。
- 未做跨进程并发验证；**本轮未跑任何付费整链**。
