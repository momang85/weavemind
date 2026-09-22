# 批次 A：验收与预算可信（09-22 复核 P1 定向修复）

2026-09-22。指令 `docs/阶段D实机复核与下一批指令_20260922.md` §3-A（审查基线 `7ec2003`）。
本批全部**禁网**离线验证：只打桩传输层/内存假后端，不联网、不调用模型、不连实际 Redis
（`REDIS_PORT=6399`，与 CI 等价）。**未追加实机、未换模型、未改全局 0/0/0。**

## 1 两处验收豁免收窄（A-1）

`acceptance_checker.py`。复核表三行逐条复现（生产函数，`run_acceptance` 全链）：

| 反例 | 修复前 | 修复后（本批实测） |
|---|---|---|
| 来源为空 + "数据来源：洋河股份2024年年报。" | fail，检查 1 条 | **fail，检查 1 条**（保持） |
| 同上仅追加"，需核查现金流明细。" | pass，检查 0 条 | **fail，检查 1 条** |
| 同上换行追加"需核查现金流明细"（无句号） | pass，检查 0 条 | **fail，检查 1 条** |
| 来源只有宁德时代营收100亿元；正文另称比亚迪、腾讯各100亿元 | pass，溯源 100%（同值提升） | **fail，溯源 33%（1/3）**；另两条仍判冲突 |

正例（防误伤）：`现金流结构需结合附注才能判断，来源是以销售回款为主还是以其他项目为主。`
→ `source_labeling` 仍 pass（判断句不是来源声明）。

实现要点：
- 来源声明的豁免收窄到**声明片段本身**（`_extract_source_claims` 逐片段判定"像来源名/像判断句"），
  不再因整句任意位置出现"需核查"而豁免；
- 同值提升必须**同一事实身份**：`_promotion_subject` 抽主体（含媒体前缀剥离、停用词），
  报告子句与来源行主体冲突时不得提升（`_subjects_conflict`）。

冻结用例：`test_acceptance_adversarial.TestAcceptanceBypassNarrowed`（3 例，含正例）。

## 2 一请求一票、usage 归属（A-2）

`llm_client.py`：只有**真的发生"流式被拒→非流式回退"**才另开票（`self._fell_back`），
普通非流式这一次发送由调用方那张票覆盖；回退那张票按**它自己响应**的 usage 结算，
调用方那张票覆盖的流式发送没有 usage → 保持"未知"。

| 形态（复核表） | 修复前 | 修复后（本批实测） |
|---|---|---|
| 普通非流式 usage=5 | 1 次发送 / 2 票 / 用量记 10 / 1 条记录 | **1 次发送 / 1 票 / 用量 5 一次 / 1 条记录** |
| 普通非流式 cap=1 | 0 次发送，却留已结算票 | **第一次允许发出；第二次发送前拒（`budget_exhausted`），不留票** |
| 流式 415 → 非流式成功 | 2 次发送 / 2 票，成功 usage 记两遍、未知记 0、形状 1 条 | **2 次发送 / 2 票，usage 记一次，415 那次 `unknown_calls=1`，形状 2 条** |

冻结用例：`test_root_budget.TestOneSendOneTicket`（3 例，**不替换 `_send_request`**，回退逻辑真跑）。

## 3 共享账本"读成功、写失败"必须拒发（A-3）

`root_budget.py`：
- `_save()` 返回**是否可信落盘**；拿不到落盘锁时**不写**（不做无锁覆盖），标记不确定；
- `_persist_uncertain` 是**账本级**事实：写进 `budget_state.json`，任一进程报过就不被后来的成功写入洗掉，
  断点恢复（同一账本）继承；
- 有界多进程任务（`limited` + `multiprocess`）在下列任一路径**拒绝新付费请求**：
  1. 共享账本不可用（原有一条，保留）；
  2. 已标记落盘不确定（锁超时 / 写失败 / 序列化失败）；
  3. 共享预留未成功返回（探测可用、预留却失败）。
- 预留后落盘失败 → **撤销该次预留**（本地计数回退 + 共享计数按"请求没发生"改回），
  不留"没发出却占额度"的票；不配上限的任务不受影响（保留可用报告与只读工作台），
  但快照 `persist_uncertain=True` 如实标记。

| 反例 | 修复前 | 修复后（本批实测） |
|---|---|---|
| 假后端 GET 成功、INCRBY 后落盘异常 | 返回票据（local_reserved=1，转本地成功） | **`BudgetExceeded`（"拒绝新付费请求…待对账"），共享计数回到 1，本地 0 票** |
| 落盘锁等待超时 | 照写（无锁覆盖） | **不写（文件字节不变），拒发** |
| 断点恢复 | 不确定状态丢失 | **继承 `persist_uncertain`，继续拒发** |

冻结用例：`test_root_budget.TestBoundedFailClosed`（新增 4 例，合计 8 例）。

## 4 口径标注（A-4）

`BudgetLimits` 文档 + 快照新增机器可读口径：
`count_basis = {max_calls: all_reserved_tickets, provider_requests: "stages:llm+backup",
steps_dispatched: "stages:step"}`，`token_basis = {open_upper, actual, unknown_calls}`。
即：`max_calls` 扣**所有预留票据**（含步骤调度），不等于供应商请求数；token 预留上界、
实际 usage、未知用量三项独立，不互相折算。**未放宽任何上限、未新增可用额度。**

冻结用例：`test_root_budget.TestProviderRequestReconciliation.test_step_dispatch_is_not_a_provider_request`
（补断言口径字段）。

## 5 未验项（如实）

- **真实 Redis 多进程**：本批全部用内存假后端 + `REDIS_PORT=6399`（无 Redis），
  "跨进程共享写失败"只在假后端上复现；真实 Redis 的落盘锁超时/网络分区仍未验。
- **实机 415**：回退路径由打桩传输层复现，真实网关 415 响应未再触发（不追加实机）。
- **同值提升的表格形态**：表格单元没有局部主体时（主体只在标题/文档级声明），
  同值仍判可溯源——这是既有已记录缺口（`test_known_gap_number_subject_mismatch`），
  本批未扩大范围去改文档级主体绑定。
- 浏览器/PDF 视觉验收属批次 C/D，本批未做。
