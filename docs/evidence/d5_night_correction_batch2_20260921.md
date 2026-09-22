# 第二小批：一请求一票与可靠账本（D5 补修）

2026-09-21 夜间纠偏（指令：`docs/阶段D夜间复核与纠偏_20260921.md` §3 第二小批，基线 `36f47b2`）。
范围：`root_budget.py`（跨进程落盘锁、账本身份初始化、有界任务 fail closed、实际用量与
上界分列、调度票据与请求分列）、`llm_client.py`（415 流式回退独立开票、预算拒发不重试
不切备用、按实际用量结算）、`test_root_budget.py`（禁网假供应商逐条对账）。
**未新增实机**；未连生产 Redis。

## 1 复现（协作审查给出的两个反例）

| 反例 | 现象 | 本轮复现方式 |
|---|---|---|
| P1-C 一票两发 | `max_calls=1` 时第一次流式发送收到 415，代码回退非流式**再发一次**；两次发送共用一张票（`reserved=settled=1`，调用形状仅一条） | 新增 `TestStreamFallbackTickets`：假供应商记录每次发送，断言"两次发送两张票"；cap=1 时断言第二次**在发送前**被拒 |
| P1-D 并发落盘丢账 | 两个进程读同一旧快照 → 各自合并 → 互相覆盖：成功预留两次，最终快照只剩一次、一个 writer | 新增 `TestCrossProcessSaveAtomicity`：两个真进程（`subprocess`）各预留/结算 40 次，断言最终快照 80 次、两个 writer。**修复前实测 40（复现）** |

## 2 改了什么

### 2.1 每次真实发送一张票（P1-C）
- `LLMClient._send_request`：流式被拒后的**非流式回退**单独 `_budget_open("llm", …)` /
  `_budget_close`（`usage="llm:stream_fallback"` 便于审计）。调用方那张票覆盖第一次
  流式发送，回退这张覆盖第二次——两条合起来"两次发送两张票"。
- `_async_chat_once`：同样的回退路径独立开票（`call_llm_async` 的票覆盖第一次发送）。
- **预算拒发不是端点故障**：新增 `_is_budget_refusal`，在 `call()` 的重试分支、
  `call_llm_stream` 的主端点→备用切换处都先判——拒发即停手（重试等于再发一次、
  切备用等于换端点再发一次，都会绕过上限）。
- 票据绑定的仍是"一次实际 provider HTTP 发送"：重试 = 新票，主备切换 = 新票
  （`backup` 阶段），回退 = 新票。

### 2.2 账本落盘跨进程原子（P1-D）
- `cross_process_file_lock`：锁文件与数据文件分离（`budget_state.json.lock`），
  Windows 用 `msvcrt.locking`、POSIX 用 `fcntl.flock`，进程退出即释放（不靠文件存在性
  加锁，崩溃不留死锁）；`_save` 的**整段读改写**（读 writers → 合并 → 写 tmp → replace）
  都在锁内，等待上限 `WM_BUDGET_SAVE_LOCK_SECONDS`（默认 5s，超时照写并告警）。
- **账本身份初始化竞态**：两个进程同时启动时，`ledger_id` 此前各自生成——`writers`
  合并按身份隔离，于是对方的增量被当成"上一轮"丢掉（这正是 40 vs 80 的成因）。
  现在身份在**锁内**确定：先拿锁的写，后拿锁的**采纳**已有身份与计数。
- 恢复与初始化一致：重开账本读回同一身份；未结算票据留在文件里（见 2.4）。

### 2.3 显式有界任务 fail closed
- `RootBudget(multiprocess=...)`：编排器与 Worker 两条生产路径都传
  `multiprocess_default()`（默认 True；`WM_SINGLE_PROCESS=1` 显式声明单进程，
  供离线单测/单进程脚本）。
- `reserve`：`limited and multiprocess` 时先探共享账本（`_shared_available` 直接 GET
  计数键，失败即标记后端降级），**不可用就抛 BudgetExceeded**：
  "显式有界任务要求跨进程共享计数，但共享账本（Redis）不可用：拒绝新付费请求
  （不得按每进程一份上限继续）"。
- 不配上限的任务不受影响（离线检查、工作台读取照常）；单进程语义不受影响。
- 拒发原因进入既有收尾路径：`_budget_exhausted[task_id]` → 交付说明顶部
  "本次运行因根任务预算耗尽而提前收尾：<原因>"，底稿与已产出交付物照常保存。

### 2.4 调度票据 vs 供应商请求、上界 vs 实际用量
- `snapshot()` 新增两列：`provider_requests`（仅 `llm`/`backup` 阶段）与
  `steps_dispatched`（`step` 阶段）——调度票据**不算**请求证明，核对上限看前者。
- `settle(..., usage_known=)`：`tokens_actual`（供应商实际用量）与
  `tokens_unknown_calls`（没取到用量的调用数，那部分记的是**预留上界**）分开累计；
  未知用量**不填 0**。`_budget_close` 接受 `actual_tokens`：`call()`/流式 `_attempt`/
  异步 `call_llm_async`/异步回退 四处都从响应体的 `usage` 取实际值（取不到就记上界）。

## 3 定向验证（禁网假供应商，全部离线）

`test_root_budget` **63 项全过**（新增 15 项）：

| 用例 | 断言 |
|---|---|
| `TestStreamFallbackTickets.test_sync_stream_fallback_opens_its_own_ticket` | 流式 415 → 非流式回退：两次发送、两张票（`llm.reserved/settled=2`） |
| `…test_cap_one_blocks_the_fallback_before_sending` | cap=1：第二次**发送前**被拒、`budget_exhausted` 可识别、只结算 1 次、不留未结票据 |
| `…test_async_stream_fallback_opens_its_own_ticket` / `…test_async_chain_counts_two_tickets_for_two_sends` | 异步回退单独开票；整链两次发送两张票 |
| `TestCrossProcessSaveAtomicity.test_two_processes_keep_both_writers_increments` | 两进程各 40 次 → 快照 80 次、2 个 writer（**修复前 40**） |
| `…test_lock_file_is_separate_and_released` | 锁文件与数据文件分离；进程退出后锁可立即获得 |
| `TestProviderRequestReconciliation.*`（5 项） | 重试/主备/415 回退各自"供应商实收 == 票据数"；调度票据不计入请求；回退那次记**实际用量**、415 那次记上界并计 `unknown_calls`；未结算票据重开后可见且可结算、重复收尾被拒 |
| `TestBoundedFailClosed.*`（4 项） | 多进程+无共享账本 → 第一次请求即拒（原因含"共享账本"/"拒绝新付费请求"）；有可用共享后端则放行；单进程语义不受约束；不限额度任务不受影响 |

回归：`scenario_checks` 17、`test_offline_delivery` 34、`test_delivery_chain` 286、
`test_root_budget` 63、`claims_d1_cases check` 11/11；**CI 清单 46 文件本地全绿**。

## 4 未验项 / 说明

1. **真实 Redis 未连**：多进程验证走的是真进程 + 本地文件锁；"共享计数跨进程原子"
   仍只在假后端（`_AtomicFakeRedis`）上验证过。指令允许"没有该环境如实标未验"。
2. 修复后的**实机**账本核对（供应商请求 vs 票据）需下一次有界实机复验——按指令
   "当前不新增有费实机"，留给第四小批。
3. `WM_SINGLE_PROCESS=1` 是给离线单测/单进程脚本的显式声明；生产不设该变量即按
   多进程语义（有界任务在共享账本不可用时拒发）。
4. 锁等待超时后"照写并告警"：账本是审计视图，共享计数才是额度真源；该分支未在
   真实高并发下压测。
