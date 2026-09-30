# T0-c 共享账本原子票据（一次迁移、跨进程唯一裁决）

批次：阶段T `T0-c`（09-30 晚扩大审查 F05）。基线 `8fff008`，前置 `d325cc4`（T0-a）、`581dfe6`（T0-b）。
**未改**用户模板/模型/代理/权限/密钥/人审；**未发**外部网络请求；
跨进程验收用真实 Redis 的**专属键前缀**（`wm:budget:t0c-isolation*`，结束时清理）。

## 1. 反例（先复现，再修）

| 编号 | 主审反例（原话） | 复现读数（不带修复） |
|---|---|---|
| F05-a | cap100、A 预留 80/实际 80、结算间插 B 80 → **总 committed 160** | 新用例 `test_b_reserve_cannot_slip_into_settlement_window`：**>40s 不返回（挂住）**——结算被拆成"先退上界、再记实际"两次远程写，另一个进程的预留插进窗口；新实现一次 `eval` 完成迁移 |
| F05-b | 待对账不得当作没花 | `test_unsettled_keeps_upper_and_reconcile_settles`：**`0 != 80`**——旧实现经 `_take_open` 把上界退了，账面少记一档 |
| F05-c | 不同进程不得凭各自 open 副本独立释放 | `test_shared_ticket_state_machine_refuses_second_transition`：**AttributeError: 'RootBudget' object has no attribute '_ticket_key'`（旧实现根本没有共享票据状态机） |
| F05-d | 显式有 root 而建账失败必须拒绝新的付费发送 | `test_explicit_root_with_unbuildable_ledger_refuses`：旧 `_budget_open` 返回 `(None, "")` **继续放行**（既不记账也不受上限） |
| F05-e | 后台自迭代/反思线程显式继承 root/许可/账本 | `test_helper_binds_root_into_background_thread`、`test_prompt_refinery_thread_binds_root`：**FAILED**——旧代码在终态后 `threading.Thread(target=_refine_async)` 启线程，线程里 `get_task_context()` 为空 → 那次模型调用完全不记账 |

失败→通过：不带修复 **1 挂住 + 6 失败/报错**；修复后 `test_root_budget` **83 OK**。

## 2. 最小修复

`root_budget.py`——票据状态机成为**共享唯一裁决**（一次 Lua / 等效事务）：

- **键空间**：`wm:budget:{root}:{ledger_id}:tk:{ticket}` 存票据哈希
  （`state` OPEN/SETTLED/UNSETTLED/REFUNDED、`stage`、`upper_calls`、`upper_tokens`、`actual_tokens`）。
- **三个脚本**：`WM_OP settle`（OPEN→SETTLED，**一次** `tokens += actual − upper`）、
  `WM_OP refund`（OPEN→REFUNDED，调用次数与上界一起退）、
  `WM_OP unsettled`（OPEN→UNSETTLED，**不动计数**：可能仍在计费）。
- `reserve` 登记 OPEN 票据；**有界任务**在共享侧登记不出来（客户端不支持原子迁移/后端异常）时
  **拒发**（`_undo_reservation_locked` + BudgetExceeded），绝不退回两次远程写的旧路径。
- `settle`/`refund`/`mark_unsettled` 的共享计数改动**全部**交给脚本；本地只做镜像与拒绝判定；
  共享侧说"这张票不是 OPEN"时**拒绝**且把本地陈旧副本放回并留痕（`settle_not_open_remote`）。
- 新增 `reconcile(ticket, tokens=…)`：UNSETTLED→SETTLED 按实际用量入账；
  本地集合没有该票时按**共享票据**上的 `state=UNSETTLED` + `upper_tokens` 算
  （`_remote_ticket_info`）——跨进程/重启后的对账据此可行。
- `_take_open` 改为**只动本地**（去掉顺手退共享上界的那一步）。

`llm_client.py`——显式有 root 而建账失败 → **拒发**：

- `_root_budget_for_task()` 记下失败原因（`ledger_unavailable_reason`）并把日志升为 error；
- `_budget_open()`：有 task 上下文却建不出账本 → 抛带 `budget_exhausted` 的 `LLMCallError`
  （调用方据此不重试、不切备用），并记一条 `budget_unavailable` 调用形状；
- 完全没有根上下文的调用仍放行，但**计数留痕**（`no_root_call_count()` + warning），不静默。

`orchestrator_v2.py`——后台线程显式继承根任务：

- 新增 `_background_with_root(task_id, fn, …)`：创建边界 `copy_context()` + `set_task_context(task_id)`，
  异常在本线程内如实记录；
- 提示词自迭代 `_refine_async` 与进化竞技场 `_run_evo` 改走它（后台花费归原 root）。
  `add_material` / `regenerate_candidate` 两条后台路径按设计不调模型（候选是确定性装配），未改。

## 3. 退出项对照

| 退出项 | 读数 |
|---|---|
| cap1000 / A 预留 800 / B 800 必须拒绝 | 单进程：`refused` 两次（事务前 + 旧写窗口）；**双进程实测**：B 得 `根任务剩余 token 不足（剩 200，需要 800）` |
| 重复与跨实例结算无双扣双退 | 共享票据状态 `SETTLED`；另一进程再结算 → `verdict=SETTLED`（被拒）且共享计数**不变**（800） |
| 建账权限/存储异常发送 0 | 用例：建账失败 → `budget_exhausted` 拒发；有界任务共享票据登记不出来 → `reserve` 抛错、`calls_reserved` 不增 |
| 后台调用归原 root | `_background_with_root` 内 `get_task_context()` == 根任务；源码级断言：提示词自迭代不再用裸 `Thread` |
| 保留费用口径（应用记账/供应商结算）分别显示 | 未改：`tokens`（应用上界/实际）与 `tokens_actual`/`tokens_unknown_calls` 继续分开累计；未知用量不填 0 |
| 远端成功但回执丢失 → 冻结待对账，不自动退款重试 | `mark_unsettled` 保留上界（**双进程实测 900 = 800+100**），`reconcile` 才按实际入账（840）；`refund` 只对 OPEN 生效 |

## 4. 隔离双进程验收（13/13）

`scripts/t0c_ledger_isolation_check.py` → `docs/evidence/t0c_ledger_isolation.json`。
每个步骤都是**新起的子进程**，共享同一份临时账本目录 + 真实 Redis 专属键空间：

A 预留 800/1000 → B 的 800 被拒 → A 结算 800 → 共享 tokens=**800**（不是 1600）→
B 再要 800 仍被拒 → 共享票据 `SETTLED` → 另一进程再结算被状态机拒且计数不变 →
再预留 100 → 转待对账（计数 **900**，上界未释放）→ 对账 40 → 计数 **840**。

**这条验收当场抓到一处真缺口**：`reconcile` 依赖本地 `unsettled_tickets`，
而那是**本地文件状态**——另一个进程看不到它，跨进程对账会被判"未知票据"。
已补 `_remote_ticket_info`（按共享票据的 `state=UNSETTLED` + `upper_tokens` 对账）。

## 5. 定向回归（逐文件真实退出码）

`test_root_budget.py` **83 OK**、`test_cancel_semantics.py` 62 OK、`test_orchestrator_v2.py` OK、
`test_delivery_chain.py` 408 OK、`test_p0.py` OK、`test_deploy_manifest.py` OK、
`test_offline_delivery.py` OK、`test_startup_readiness.py` OK、`test_writer_consolidation.py` OK、
`test_task_persistence.py` OK、`test_review_edit_api.py` OK(skipped=3)、`test_frontend_guards.py` OK。

**测试替身补了 `hset/expire/eval`**（`_AtomicFakeRedis`）：生产走 Lua，替身按同一状态机语义
**一次性**应用；替身没有 `eval` 会被当成"共享侧无法原子迁移"（有界任务据此拒发）——
那正是要保证的语义，所以替身必须真的实现它，而不是让用例绕过去。

## 6. 未验 / 残留（不假装）

- **真实 Redis 压力/并发**：本验收只有两三个顺序子进程；未做并发压测与故障注入，
  也未对真实运行任务注入故障（架构要求）。
- **无根上下文调用的作用域账本**：现在只做到"计数留痕 + warning"，
  尚未给它们建一个显式作用域账本（架构要求"合法无界操作也记账，并有明确作用域"）。
- **应用记账与供应商结算**：口径仍分开显示，但"实际用量"只能来自响应里的 usage；
  供应商对账单接入不在本批。
- **Python 3.11**：本机 3.14 定向通过；发行环境 3.11 需同样复验（未做即不声称）。
