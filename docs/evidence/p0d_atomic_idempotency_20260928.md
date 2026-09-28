# P0-d：同作用域幂等键**原子**绑定唯一任务（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §2 第 1 条。
基线 `98e81e7`（P0-c）。

## 缺陷

旧路径是**两步**：`find_by_idempotency(key)` 先查，`mark_received` 再 `INSERT OR IGNORE`。
两步之间没有互斥，于是：

- 两个并发请求可以**各落一行 RECEIVED**（键相同、task_id 不同）；
- 随后消费 A 时 `find_by_idempotency` 命中 B → 映射到 B、跳过 B 的执行，
  但**库里留下了 A**；启动恢复扫到 A（RECEIVED）又执行一次 —— 同一份提交被执行两次。
- 而且查重**不带作用域**：键是客户端自造的（双击、超时重试、代理重放、时间戳），
  两个不同用户撞上同一个键时，A 的提交会被判成 B 的重复而丢弃。

## 修复

**`task_state.claim_receipt()`：查重与落收执合成一个 `BEGIN IMMEDIATE` 事务**（写锁
把"查 + 插"串行化），返回四种裁决：

| verdict | 含义 | 调用方义务 |
|---|---|---|
| `created` | 本次落了新收执 | **拥有**这次请求，继续发布/执行 |
| `duplicate` | 同作用域同键**且内容相同** | 复用既有任务（返回其 id），不重复执行 |
| `conflict` | 同作用域同键但**内容不同** | **明确冲突**（拒绝执行，如实回报） |
| `error` | 数据库异常 | 不落库、不执行 |

- **作用域** `receipt_scope()` = `可信用户 | 工作区(项目) | 操作`。
  `user` 来自**会话身份**（网页提交方已按 `admin["user"]` 取，不接受请求体自报）；
  操作默认 `task.submit`（补材料等其它入口可各自成域）。
- **内容指纹** `request_fingerprint()` = sha256(目标 + 项目 + 研究契约)，
  **不含** `conversation_id`/`parent_task_id`/`context` —— 那些是会话与提示，
  重试时本来就可能不同，算进去会把正常重试误判成冲突。
- 新增两列 `idem_scope` / `request_fingerprint`（`_add_missing_columns` 幂等补列）。
- **`mark_received` 改为走同一裁决**（`claim_receipt`），保留旧布尔契约：
  `created`/`duplicate` → True（收执在库）；**`conflict` → False**（这条请求没落库，
  不得假装成功）；`error` → False。
- **历史重复行不删**：新裁决只保证"今后"同一 (作用域, 键) 只落一行；
  既有重复行原样留在库里（审计），不做批量删除。

## 定向验证

新增 6 项（`test_task_persistence.TestAtomicIdempotencyScope`）：

| 用例 | 断言 |
|---|---|
| `test_create_then_same_payload_is_duplicate` | created → duplicate，且返回**既有**任务 id（不是新落的那条） |
| `test_same_key_different_payload_is_conflict` | created → **conflict**，指出既有任务，**不得落第二行** |
| `test_same_key_different_user_is_a_different_scope` | 换用户 = 换作用域 → 各自 created（不互相顶掉） |
| `test_same_key_different_project_is_a_different_scope` | 换工作区同理 |
| `test_concurrent_same_key_creates_exactly_one_row` | **两线程并发**（`Barrier` 同步起跑）→ 只有 1 方拿到 `created`、库里**只有 1 行**，其余只能是 `duplicate`/`error` |
| `test_mark_received_refuses_on_conflict` | 兼容层冲突时回 False |

`python test_task_persistence.py` → **45 项 OK**（39 + 6）。

## P0-d(2) 已闭合：发布侧改走同一裁决（同一提交只发一条消息）

上一版留下的最大缺口（"`web_ui._publish_task` 仍先查后插，端到端只发一条消息靠编排器
里那次不带作用域的兜底"）**已修**：

- `_publish_task` 删除"先 `find_by_idempotency` 查一次"的前置检查，改为**先 `claim_receipt`
  再决定是否发布**：
  - `created` → 用本进程生成的 `task_id` 发布（收执已在同一事务里落好）；
  - `duplicate` → **直接复用既有任务、不发布**（返回 `deduplicated: True`）；
  - `conflict` → 抛 `RuntimeError("同一幂等键的重复提交内容不一致（键被复用）；已拒绝，未派发")`；
  - `error` → 抛 `RuntimeError("任务收执无法落库…")`，不发布。
- **兼容规则**：`claim_receipt` 的按键匹配同时接受 `idem_scope` 为空的行（旧实现或
  `mark_queued` 等其它写入路径落的），因为那些行的历史语义就是**全局按键去重**；
  且这类行**指纹也为空**，内容判不出来时**保守判 `duplicate`**（复用、绝不重复执行），
  而不是判冲突把用户的正常重试挡掉。新落的行两者都有值 → 按作用域严格隔离。

新增/更新用例（`test_writer_consolidation.TestSubmitHandshake`）：
- `test_known_idempotency_key_does_not_publish_again`（既有）→ 仍绿：复用既有任务、
  `fake.publish.assert_not_called()`、库里仍只有 1 行；
- `test_conflicting_key_raises_and_does_not_publish`（新）→ 冲突抛错且**不发布**；
- `test_unwritable_receipt_raises_and_does_not_publish`（更新）→ 打桩从旧的
  `mark_received` 换成 `claim_receipt → ("error","")`，**场景与断言不变**
  （落不了收执就报错、不发布）。

`python test_writer_consolidation.py` → **58 项 OK**（57 + 1）。

## 仍然未验（如实留白）

- **编排器侧 `find_by_idempotency` 仍是全局的**（未带作用域）：提交侧已按作用域严格裁决，
  但编排器收到消息后的二次查重还是按键全局比对。在当前流程下这不会造成重复执行
  （同一次提交带的是同一个 `task_id`，走的是 `promote_received` 那条裁决），
  但两处语义尚未完全统一。
- 未做**跨进程**并发验证（同进程两线程已验）；未在真实 Redis 上做"双击/重放"演练；
  **本轮未跑任何付费整链**。
