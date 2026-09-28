# P0-c：恢复覆盖「已 QUEUED 但从未开始」的崩溃窗口（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §2 第 4 条
「恢复覆盖提交到执行间的崩溃窗口」。基线 `ff95290`（P0-b）。

## 缺陷

旧恢复只做两件事：启动时扫一次 `RECEIVED`（`resume_received_tasks`）。
两个洞：

1. **窗口漏掉**：`promote_received` 把 RECEIVED→QUEUED 之后、执行线程真正起来之前
   进程崩了 —— 这行的状态是 **QUEUED**，而恢复只扫 RECEIVED，于是**永久漏掉**。
   任务卡在"排队中"：既没人执行、也不会失败，用户永远等不到结果，也看不到错误。
2. **一次性扫描**：崩溃可能发生在**启动之后**（运行期线程崩、被 guardian 重启），
   只在启动扫一次覆盖不到；而"无限量扫描"又会在积压时一次拉起几百个任务把端点打爆。
   旧实现另有 `limit=20` 与"跳过 10 秒内新收执"，但没有**周期**，也没有覆盖 QUEUED。

## 修复

- **`task_state.list_unstarted_queued(older_than=120, limit=20)`**：
  取 `status=QUEUED` 且**时间线里没有 `started` 事件**的行。
  用时间线做判据而不是猜：`run_and_finalize` 一进线程就写 `started`，
  所以"没有 started 的 QUEUED"= 确实没跑过；正在跑的行是 RUNNING，天然不在候选里。
  `older_than` 默认 120 秒，给正常路径（消息驱动）足够时间把行推进到 RUNNING。
- **`orchestrator_v2.resume_unstarted_queued()`**：执行权**复用 `mark_running` 的原子
  `UPDATE … WHERE status IN (QUEUED,'PENDING')`** —— 只有把 QUEUED 改成 RUNNING 的
  那一方执行，所以"周期扫描"与"消息恰好也在路上"不会重复跑同一条请求。
  与 `accept`/`resume_received` 共用同一条**派发闸门**（未持有归属 → 直接返回 0，
  且不得把行改成 RUNNING）。
- **`start_recovery_loop(interval=60, limit=20)`**：有界周期恢复，每轮
  `resume_received_tasks(10s, 20)` + `resume_unstarted_queued(120s, 20)`。
  周期覆盖"启动之后才崩"，上限覆盖"积压不炸"。
- `main()` 启动时除原有收执恢复外，**再扫一次未开始的 QUEUED**，然后启动周期循环。

**外部请求不重复收费**：恢复走的是正常执行路径（`run_and_finalize`），
证据/快照按既有幂等规则复用；本函数**不新建**第二次付费调用，也不改任何门禁。

## 定向验证

新增 3 项（`test_task_persistence.TestReceiptRecovery`）：

| 用例 | 断言 |
|---|---|
| `test_unstarted_queued_crash_window_is_recovered_once` | 先证明该行**不在** `list_received`（旧恢复看不见它）→ 阈值 600s 时不抢 → 0s 时捡回 1 条 → 第二次 0 条（已被认领）；只起 1 个线程；行变 **RUNNING** |
| `test_unstarted_queued_skips_rows_that_already_started` | 写过 `started` 的 QUEUED 不在候选（可能正在跑，不能抢） |
| `test_unstarted_queued_requires_ownership` | 未持有归属 → 返回 0、**不起任何线程**、**不把行改成 RUNNING** |

`python test_task_persistence.py` → **39 项 OK**（36 + 3）。

## 未验项

- **保留原请求的 `auto_run` / `template_steps` / `report_confirm` 未做**：
  这三个字段**当前没有落库**（`task_history` 里没有对应列），所以恢复出来的任务
  只能按默认值执行。这是本条的**剩余部分**（P0-c 第 2 小批）：要加一列
  `run_options_json`，从 `web_ui._publish_task` 一路写到 `mark_received`，
  再由恢复路径读回。研究契约（`research_request_json`）本身已落库、不受影响。
- **未做真实崩溃演练**：本机是单实例在跑，未制造"promote 后杀进程"的真机事故；
  窗口覆盖只有单测证明（含"旧路径看不见它"的反例断言）。
- 未做真实 Redis 上的多轮周期扫描观察（循环周期 60 秒，需长时间运行取样）。
- **P0-d（同键并发原子绑定）/P0-e（时间线分段）未实施**，故
  "同键并发只留一个可执行收执"**仍未验证**。
