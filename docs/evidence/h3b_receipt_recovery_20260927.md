# H3b 收执先落库、执行权单一裁决与启动恢复（2026-09-27，Harness 接续）

批次：**H3b**（指令 §2-H3，承接 C3）。补齐 H3a 明确列出的两项未做：
「前端幂等键」与「重启后恢复消费」。C3 仍未整批退出（§5 列出剩余）。

## 1. 反例 → 修法

| # | 反例 | 修前 | 修后 |
|---|---|---|---|
| 1 | 网页每次提交都是新任务，双击/失败重试 → **第二次真实执行** | 无幂等键 | 前端 `submissionKey.ts` 按**提交意图**分配键（重复点击/重试复用；成功后或改题换新），随请求下发；服务端 H3a 已支持 |
| 2 | 编排器没起来（或在崩溃窗口里）时请求**什么都不剩** | 只有一条 120 秒 TTL 的 Redis 键；用户看到超时，查不到任何东西 | 提交方**先落收执再发布**：任务行以 `RECEIVED`（待消费）存在，含 received/published 时刻与实例；启动时被捡回来执行 |
| 3 | 两条路径（消息 / 启动恢复）可能把同一收执执行两次 | 无恢复路径 | `promote_received()` 用 `UPDATE … WHERE status='RECEIVED'` 做**唯一裁决点**：谁推进成功谁执行，另一方得到 `already` 并跳过 |
| 4 | 超时一律谎报"任务未被创建"，把用户引向重复提交 | 直接抛错 | 超时先查任务库：收执在 → 返回 `ack_pending_consume`（状态 `RECEIVED`），并说明可恢复 |
| 5 | 收执已存在时登记会主键冲突 → 登记失败 | `mark_queued` 无条件 INSERT | 收执行存在则**推进**为 QUEUED（不 REPLACE，保留时间线）；已是 RUNNING/终态则按幂等成功且**不改状态** |
| 6 | 收执落不了库仍照发请求 | 无此判定 | 落库失败**不发布**并抛错：不制造"界面成功、现实里什么都没有" |

## 2. 代码位置

- `task_state.py`：新状态 `RECEIVED`（待消费）；`mark_received()`（提交方写收执，仅此一个状态）、
  `promote_received()`（原子推进，返回 `promoted`/`already`/`absent`）、
  `list_received(older_than, limit)`；`mark_queued()` 改为"有收执行就推进、无则插入"。
- `web_ui.py`：`_publish_task` 在 publish **之前** `mark_received`（失败即抛错不发布），
  时间线的 received/published 由这里产生并随请求下发。
- `orchestrator_v2.py`：`accept_task_request` 先查幂等键、再 `promote_received` 裁决
  （`already` → `data["_skip_run"]=True`，收执回 accepted 但不执行）；消息循环尊重
  `_skip_run`；把 main() 内联的 `_run_task` 闭包提为模块级 **`run_and_finalize`**
  （消息路径与恢复路径共用同一收口点）；新增 `resume_received_tasks(older_than=10)`，
  在**订阅之前**执行（先恢复旧的，再接新的）。
- 前端：`frontend/src/lib/submissionKey.ts`（纯函数）+ `TaskConsole.submit` 使用它 +
  `frontend/tests/submissionKey.test.mjs`（6 例）；`frontend/dist` 干净重建。

`older_than` 默认 10 秒：避开"刚提交、消息正在飞"的那几秒（那条消息自己会推进），
因此恢复路径不会与正常路径抢同一条请求。

## 3. 被改动的既有断言（说明为什么不是放宽判据）

| 用例 | 原断言 | 现断言 | 理由 |
|---|---|---|---|
| `test_accepted_returns_queued_and_writes_nothing` → `test_accepted_writes_only_the_receipt_not_queued` | "任务库一行都没有" | "只有一行，且状态是 `RECEIVED`；不得出现 QUEUED" | 原断言锁定的是"编排器收到消息才登记"的旧设计。指令要求**先保存可追踪收执再触发工作**，因此提交方必须写收执；"webui 不替编排器写 QUEUED"这条不变式仍被断言 |
| `test_timeout_raises_and_writes_nothing` → `test_timeout_with_persisted_receipt_reports_pending_consume` | 必须抛 `编排器未在` + 无行 | 返回 `ack_pending_consume` + 恰好一行 `RECEIVED` | 指令要求"超时查询已有任务，不把重复点击当新付费任务"。收执是**可恢复**状态而非幽灵成功：没有 QUEUED/终态被伪造 |
| `test_wrapper_covers_early_return_and_exception_paths` | 按文本查找内联 `_run_task` | 查找模块级 `run_and_finalize`，并断言两条路径都 `target=run_and_finalize` | 该收口点被提取以便两条路径共用；守护的不变式（try/except 两条路径都兜底落库、且只有一处实现）未变 |

新增用例（`test_task_persistence.TestReceiptRecovery`，8 例）覆盖：收执写入状态与时间线、
推进只成功一次（`promoted` → `already`）、无收执返回 `absent`、收执存在时登记不冲突、
**不得把 RUNNING 改回 QUEUED**、`list_received` 排除已消费并按年龄过滤、
消息路径第二次 `_skip_run`、启动恢复只执行一次且太新的不抢。

## 4. 验证（本会话实测）

```
python -m unittest test_task_persistence test_writer_consolidation \
    test_startup_readiness.TestOrchestratorOwnership
# Ran 94 tests OK（skipped=1）
```

按**项目约定与 CI 口径逐文件跑**（`AGENTS.md`：47 个 test_*.py「按文件运行，别全量跑」；
`ci.yml` 也是每个文件一个进程）：

| 文件 | 结果 |
|---|---|
| `test_startup_readiness` | **57 OK** |
| `test_delivery_chain` | **373 OK**（沙箱放开后原先 8 例环境失败全部转为通过） |
| `test_p0` | **412 OK** |
| `test_task_persistence` / `test_writer_consolidation` | OK（含本批新增） |

前端：

```
cd frontend
node --experimental-strip-types --test tests/*.test.mjs   # 51 例全过（原 45 + 新增 6）
npx tsc -b                                                # 通过
python test_frontend_guards.py                             # 53 例 OK
npm run build（先删 dist 后干净重建）                        # 产物含 idempotency_key
```

### 4.1 单进程全量跑的 4 个失败：**与基线一致，非本批回归**

为确认"不是自己改坏的"，另开一个 **`2519d9a`（本会话开始前）的 git worktree**，
用**完全相同**的单进程全量命令跑了一遍：

| | 本会话（含 H0–H3b） | 基线 `2519d9a` |
|---|---|---|
| 用例数 | 1101 | 1076 |
| 失败/错误 | 2 failures + 2 errors | **完全相同的 2 failures + 2 errors** |

即这 4 项在**我做任何改动之前就存在**：

1. `test_p0.TestReportRouteAndMetricsConsistency.test_metrics_totals_match_database`（FAIL）
2. `test_p0.TestReportRouteAndMetricsConsistency.test_metrics_totals_degrade_to_unknown_shape`（FAIL）
3. `test_delivery_chain.TestFinancialResearchCharts.test_render_script_draws_grouped_bar`（ERROR）
4. `test_task_persistence.TestDbPathResolution.test_state_writer_reads_the_same_file_in_fresh_process`（ERROR）

**性质**：都是"**跨文件、同进程**"的测试隔离问题，不是产品缺陷，也不影响 CI——
CI 与项目约定都是**一个文件一个进程**，按该口径逐文件跑时上述 4 项**全部通过**
（见上表）。已定位到触发者：`test_startup_readiness` 与指标两例同进程时会复现
（`python -m unittest test_startup_readiness test_p0.TestReportRouteAndMetricsConsistency`
→ 两个 metrics 用例失败 + 2 条 `The specified module could not be found.`），
单独跑则通过；**根因未定位**（`db_paths.resolve_db_path()` 实测**不缓存**，
所以"路径被缓存"这一假设已排除）。留给架构师：这是否值得立项修
（例如给会改全局状态的用例加 `os.environ` 还原夹具），本批不顺手改。

### 4.2 事故记录：本批测试曾把 7 行收执写进**真库**

- 第一版 H3b 测试里有若干 `_publish_task` 用例只重定向了 `web_ui.DB_PATH`，
  没重定向 `task_state.DB_PATH`；新加的"先落收执"于是写进了真实的 `agents.db`
  （7 条 `RECEIVED`，13:59:43–14:00:01，两条带测试幂等键 `k-ev`/`k-race`）。
  危害：污染用户任务历史，并让上面第 1、2 项在**全量组合**下失败（单独跑通过）。
- **处置**：按**精确 task_id** 删除这 7 行（不用状态/时间通配），真库 `526 → 519` 行、
  `RECEIVED` 归零；并给全部 8 处 `_publish_task` 调用点补上 `task_state.DB_PATH` 重定向。
  备份/日志/**数据库文件本身**未删除、未移动。清理脚本一次性放在 `.tmp/`（已 gitignore，不进库）。

**沙箱说明**：本会话早前禁止以管道捕获子进程输出，vite 的 esbuild 因此 `spawn EPERM`，
前端 dist 无法重建；经一次性放宽沙箱完成构建（**未改任何项目配置**），
随后本会话的文件策略已改为不限制。构建前误删的 `frontend/dist` 已用 `git checkout` 完整还原，
未丢失任何已入库产物。

## 5. 未做（C3 剩余，逐条不含糊）

1. **新人可行动状态统一**：C3 要求复用 `health_registry`，把状态分成
   "未接收 / 待消费 / 执行中 / 待材料 / 明确失败"并统一旧 `source_health` 与 `dependencies`。
   本批已具备 `RECEIVED`（待消费）与时间线数据，但**页面尚未把它合并成一张状态表**。
2. **"两实例同 Redis 不重复处理"仍只有替身单测**：启动闸门与 `promote_received` 的
   原子裁决都有测试，但**未**用真实双进程 + 真实 Redis 复验（本机 Redis 处于 MISCONF 拒写，
   见 `h1_deadline_propagation_20260927.md` §4/§5；沙箱内起不了私有 Redis）。
3. **真实端到端**：没有在真实 UI 上做"同键重复提交只跑一次""提交后杀编排器再启动自动恢复"
   的实机演练（本会话不登录、不伪造会话）。
4. **C4** 全部未开始：只重建一次便携包、同包真实材料/同版交付、当前版本评分交接、
   第二家公司原文、真人每份 ≥8/10。
