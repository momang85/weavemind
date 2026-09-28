# P1-c②：状态跨层丢失 + P1-c① 的两处收尾（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §4 第二条与第一条的收尾句

> **状态跨层丢失。** worker把预算拒绝/超时/无结果都降成 `[]`，外层记SUCCESS再由编排反复判
> 失败。研究路径传递结构化status/reason/retryable/attempts和实际查询，兼容数组只留在旧边界。
> 区分正常零命中和provider未完成；不把一次内部成功日志解释为真实搜索成功，也不凭四次
> dispatch推断四次外部请求。
>
> （第一条收尾）…持久保存规范计划…**无新查询/额度耗尽时明确停，不再自然回退到原查询。**
>
> **退出证据：** 空结果/错误原因跨层保真…

基线 `69ddc04`。**未联网、未跑付费整链**：全部离线、用替身与假结果验证。

## 一、缺口：`[]` 不是一种状态

`worker_base.SearchAgent.execute` **就是** `_execute_bounded`（`worker_base.py:1352`），
返回值直接进 `result["result"]`。而它有**四条出口都返回同一个 `json.dumps([])`**：

| 出口 | 真实含义 |
|---|---|
| 全部提供方在冷却期 | **我们没发请求**（不是提供方的错） |
| 任务级预算不可用 | **我们没发请求**（额度已尽） |
| 重试后仍空 | 未完成 / 零命中，混在一起 |
| 真零命中 | 查询**完成**，确实没有结果 |

四者在跨层后完全不可分：外层只看到"SUCCESS + 空数组"，再由编排反复判失败；
`outcome.attempts`（**真实发出的调用数**）、`outcome.queries_tried`（实际查询串）
在返回前就被丢掉——"不凭四次 dispatch 推断四次外部请求"因此**无从做到**。

第二条：重试计划用尽时，编排器只写了一句中文说明（"本轮没有可用的新查询"），
Worker 读不出来 → **自然回退**到 `contract.queries()`（上一批已证明打不出结果的查询）；
更糟的是 `_execute_bounded:1046` 的 `_query_variants(...) or [instruction[:120]]`：
一旦 `_query_variants` 返回空，就会**拿整段指令当查询**。

## 二、修法（三处，边界只留一处）

### 1. Worker：四条出口各自记真值（`_record_search_status`）

新增 `SearchAgent._record_search_status(status, reason, *, retryable, attempts, queries,
providers, elapsed, outcome, refused_calls)`，把本次检索的**真值**记成结构化状态：

- `attempts` / `queries` / `refused_calls` 全部取自 `SearchOutcome`（**真实发出的调用数**
  与实际查询串），不由 dispatch 次数或重试轮数推断；
- 四条出口分别记 `providers_cooling` / `refused_budget` / `stopped_no_new_queries` /
  `no_results` / `<provider 类别>`（timeout、rate_limited…），
  并统一在 `reason` 里写明"**查询完成但零命中**"还是"**检索未完成：X**"。

### 2. **明确停**：`[无新查询]` 标记，在发请求之前停

- `execution_contract.NO_NEW_QUERY_MARK = "[无新查询]"`；`_search_retry_instruction`
  在计划用尽时把它写进指令（连同原来的中文说明）；
- `worker_base` 在 `_execute_bounded` **开头**检查：有标记且**没有**重试批次 →
  记录 `stopped_no_new_queries` 并**直接返回**——不出网、不耗额度、
  **不回退契约查询**、也不拿整段指令当查询；
- 反向护栏：真的带 `[重试检索查询]` 批次时，标记不得把这一批也停掉（有用例）。

### 3. 跨层：状态作 `result` 的**兄弟字段**上行，数组只在旧边界

`_publish_result` 在消息里附 `search_status`（**`result` 仍是 JSON 数组**）——
现有 4 处消费者按数组解析 `result`，改形状会连带改掉它们，而这里只需要"说出来"。

编排器 `_normalize_result` 据此算出 `search_verdict`：

| 字段 | 含义 |
|---|---|
| `completed` | 查询是否真的跑完（`no_results` 算跑完；timeout/rate_limited… 不算） |
| `zero_hits` | **完成且零命中**——只有这一种才该继续走"无来源"的正常分支 |
| `attempted` | 是否真的发出过请求（预算不可用 / 提供方冷却 / 计划用尽**没有**发） |
| `attempts` / `queries` / `refused_calls` | 真值，不由 dispatch 次数推断 |

并打一条日志把真值写清楚：`status / completed / zero_hits / attempted / 实际请求 N 次（未发出 M 次）`。

**刻意不改顶层 status**：`refused_budget` / `providers_cooling` / `stopped_no_new_queries`
是**我们自己没发请求**，按提供方故障处置会误触发熔断与重规划；`timeout` 这类未完成也不在
这里改判——把真值**说出来**，由既有重试/反思路径按原规则处理。这是一处**明确的口径边界**，
不是遗漏。

### 4. P1-c① 收尾：规范计划**写回调用方持有的那个对象**

`_dispatch` 此前只做 `step = _fixed[0]`（局部变量）：本次派发用的是重建后的指令，
但计划里那一份仍是旧的——`_publish_full_state` 推给页面的、checkpointer 落盘的、
下一次派发读到的都是旧指令，于是**每次派发都要重建一遍**。
改为原地更新（`step.clear(); step.update(_fixed[0])`），派发路径与展示路径从此同源。

## 三、测试（全部离线）

`test_search_quality_unified`：

| 用例 | 断言 |
|---|---|
| `test_zero_hits_and_incomplete_are_distinguishable_cross_layer` | 同一循环里跑 `no_results` 与 `timeout`：`result` 仍是数组；兄弟状态里 `status/attempts/queries/retryable` 正确，理由分别是"零命中"/"未完成" |
| `test_status_is_carried_as_sibling_field_on_the_result_message` | 上行消息 `result == "[]"` **不变形状**，`search_status` 另开字段 |
| `test_no_new_queries_marker_stops_before_any_request` | 有标记无批次 → **一次请求都不发**，`attempts=0`，状态 `stopped_no_new_queries` |
| `test_no_new_queries_marker_does_not_block_a_real_retry_batch` | 反向护栏：真有重试批次时照常检索 |
| `test_orchestrator_marks_verdict_without_forcing_failure` | 四种状态各判一次 `(completed, zero_hits, attempted)`；**顶层 status 不被改判**；`attempts` 取真值；无 `search_status` 时不凭空造 verdict |

`test_orchestrator_v2`：

| 用例 | 断言 |
|---|---|
| `test_dispatch_rebuild_writes_the_canonical_plan_back` | 走**真实** `_dispatch`：第一次重建并**写回传入的 step 对象**（契约指纹与指令都变）；第二次派发**不再重建**、指令不变 |
| `test_search_retry_exhaustion_emits_explicit_stop_marker` | 计划用尽 → 指令带 `[无新查询]` 且无重试行；还有新查询时**不带**标记 |

回归见提交信息（`test_orchestrator_v2` 81 OK、`test_search_quality_unified` 75 OK 等）。

## 四、本批**未**覆盖（如实列出）

1. **`result` 的形状没有改成结构化对象**。指令里"研究路径传递结构化 status…兼容数组只留在
   旧边界"的**严格**读法是"内层结构化、边界转数组"；本批做的是"数组照旧 + 结构化状态作
   兄弟字段"。理由：`result` 有 4 处按数组解析的消费者（`orchestrator_v2` 两处、
   `structured_pipeline` 一处、`_try_pdf_evidence` 一处），改形状要连带改它们并逐一回归；
   兄弟字段已经让"零命中 vs 未完成"跨层可分，**没有**声称做了那个更大的重构。
2. **未新增"实际停止/清理时间分账"字段**（§4 第三条收尾句）。既有任务级预算预占/退回
   （`_finish_search` → `_release_search_calls`）本批未改。
3. **checkpointer 落盘的计划**：本批让**调用方持有的 step 对象**成为规范计划（它正是
   `_publish_full_state` 推给页面、`all_steps` 里被持久化的那一份），但**没有**新增
   "重启后把检查点里的指令读回来与派发时逐字比对"的用例——那需要一次真实恢复演练。
4. `ddgs` 这类同步 SDK 仍只做到"低于可行下限拒发"，没有发出后的受控隔离终止（同 P1-c③ 记录）。

## 五、口径声明

全部验证离线完成（替身 + 假 `SearchOutcome` + 假 worker），**未对任何真实站点发起请求**、
未消耗搜索额度、未跑付费整链；未改门禁/阈值/模型/权限/模板；未删除任何日志、备份或产物。
