# H3a 持久化收执、幂等提交与实例归属（2026-09-27，Harness 接续）

批次：**H3a**（指令 `docs/Harness接续执行指令_20260927.md` §2-H3，承接
`docs/真实研究闭环与阶段D收口_20260927.md` §4-C3）。本文件记录**已落地的部分**与
**照实未做的部分**；C3 尚未整批退出，不把部分实现写成阶段通过。

## 1. 反例（原指令 §3.4）→ 修法

| # | 反例 | 修前 | 修后 |
|---|---|---|---|
| 1 | 重复提交（双击/超时重试/重放）变成**第二次真实执行** | `_publish_task` 每次生成新 UUID；编排器不看身份 | 幂等键两处生效：web_ui **发布前**查一次（命中就复用、连发布都不做）；编排器 `accept_task_request` **再查一次**（竞争时先落库者赢），命中则**不登记、不执行**，收执 `accepted:dedup:<已有 id>` |
| 2 | 收执只有一条 120 秒 TTL 的 Redis 键，事后无法回答"到没到、谁收的、卡在哪段" | 无持久收执、无时间线 | 任务行新增 `idempotency_key` / `submit_timeline_json` / `accepted_by`；时间线逐段记 **received / published / persisted / consumed / started**，每条带时刻、实例标识、代码版本 |
| 3 | 两个编排器同 Redis 订阅同一通道 → 同一条请求**执行两次** | 无归属校验，默默重复消费 | 启动时认领 `orchestrator:owner` + 心跳；已有**活**实例则**明确拒绝启动**（返回非零），心跳过期才允许接管 |
| 4 | 收执超时一律说"任务未被创建" | 直接把用户引向"再点一次" | 超时先查任务库：收执已落库则如实返回 **`ack_pending_consume`**（已收执待消费），不再谎报不存在 |

## 2. 代码位置

- `task_state.py`：新增列（`idempotency_key` / `submit_timeline_json` / `accepted_by`，
  与既有列补丁同一套幂等 DDL）；`record_submit_event()` / `read_submit_timeline()` /
  `find_by_idempotency()`；`mark_queued()` 增参 `idempotency_key`、`instance`、
  `code_version`、`submit_events`，一次写入即落 `accepted_by` 与时间线。
- `orchestrator_v2.py`：`_set_task_ack()`、`_instance_identity()`（复用
  `health_registry.instance_id()`，代码版本可选）、`claim_orchestrator_ownership()`、
  `OWNER_KEY`/`OWNER_HB_KEY`/`OWNER_HB_TTL=30`；`accept_task_request()` 去重 + 时间线；
  `main()` 启动闸门；消息循环按 `data["_effective_task_id"]` **跳过重复执行**，
  登记失败也**不再启动执行**（反向幽灵任务）；`_run_task` 记 `started`。
- `web_ui.py`：`_publish_task(..., idempotency_key="")` 前置去重、请求内置幂等键与
  received/published 时刻、解析 `accepted:dedup:<id>`、超时改查收执；
  HTTP 处理器接受请求体 `idempotency_key` 或标准 `Idempotency-Key` 头。

**没有键时行为与之前完全一致**（不去重、不查库、payload 键为空）——这是刻意保留的兼容边界。

## 3. 定向验证（本会话实测）

| 套件 | 结果 |
|---|---|
| `test_task_persistence.TestSubmitIdempotencyAndTimeline`（新增 6 例） | **6 OK** |
| `test_startup_readiness.TestOrchestratorOwnership`（新增 5 例） | **5 OK** |
| `test_task_persistence` + `test_writer_consolidation`（含新增 4 例） | **92 OK**（skipped=1） |
| `test_startup_readiness` + `test_cancel_semantics` + `test_review_isolation` + `test_review_protocol` + `test_review_degradation` + `test_root_budget` + 上两文件 | 307 例中除下列环境项外全部通过 |

新增断言要点：
- 同幂等键提交两次 → 库里**只有一行**、第二次 `_effective_task_id` 指向已有任务、
  收执为 `accepted:dedup:ui-a1`；
- 不同键 → 两个任务（不把用户主动再跑一次当重复）；
- 无键 → 行为不变；
- 时间线含 received/published/persisted/consumed 且**实例非空**、请求自带时刻原样保留；
- 重启后（重解库路径）幂等键、收执行、时间线**仍可查**；
- 库不可写 → 收执必须是 `rejected:`，不留"已消费"假时间线；
- web_ui 侧：命中已有键时 `publish` **未被调用**、任务数仍为 1；`accepted:dedup:` 回原任务；
  payload 带键与 received/published；无键时键为空串；
- 归属：首个认领成功；同实例重启可续；**别的活实例被拒绝**（说明里点出对方实例）；
  心跳过期允许接管；Redis 不可写**不阻断启动**（只记日志）。

## 4. 环境受限未跑的用例（照实，非本批回归）

本会话沙箱禁止"通过管道捕获另一个程序的输出"（`subprocess` + `PIPE` → 拒绝访问），
因此两条依赖 `subprocess` 管道、或依赖 `tasklist` 探活 pid 的既有用例失败：

- `test_task_persistence.TestDbPathResolution.test_state_writer_reads_the_same_file_in_fresh_process`
  （用 `subprocess.run(..., capture_output=True)` 起子进程）；
- `test_startup_readiness.TestStartupController.test_instance_lock_second_holder_is_reported`
  （`launcher._is_alive()` 走 `subprocess.check_output(["tasklist", ...])`，
  沙箱下 `_is_alive(os.getpid())` 实测返回 **False** → 同进程第二次获取锁被当成"陈旧锁接管"）。

两者都**与本批改动无关**（前者属库路径解析、后者属 launcher 单实例锁），
是沙箱边界；按纪律不扩权、不改判据，记录为环境受限。CI（ubuntu）无此限制。

本地跑测仍用一次性脚手架 `.tmp/run_tests.py`（不进库，`.tmp/` 已 gitignore），
只在运行期把 `mkdtemp` 换成默认 mode 的等价实现。

## 5. 未做（C3 尚未整批退出，逐条列出，不含糊）

1. **前端没有生成幂等键**：HTTP 侧已接受 `idempotency_key` 字段与 `Idempotency-Key` 头，
   但页面提交尚未带上"本次提交尝试"的键，所以**真实双击/自动重试目前还不走去重**。
   需要前端在用户发起提交时生成一次键、网络失败重试时复用同一键（并重建 dist）。
2. **重启后的"恢复消费"未做**：收执行与时间线已持久化、可查（"可查到收执"已达成），
   但"启动时把 `QUEUED` 且从未 `consumed` 的任务重新投递执行"没做——
   目前只有 C1 的 `material_pending_tasks` 局部恢复。
3. **新人可行动状态统一未做**：C3 要求复用 `health_registry` 统一旧 `source_health` 与
   `dependencies`、把状态分成"未接收/待消费/执行中/待材料/明确失败"。本批只在**收执**层面
   区分了"已收执待消费"（`ack_pending_consume`）；页面四态统一仍是 S2 已完成的部分，
   与收执状态尚未合成一张表。
4. **"两实例同 Redis 不会重复处理"只验到启动闸门**：拒绝第二个实例有单测（替身 Redis），
   **未**用真实双进程 + 真实 Redis 复验（本机 Redis 处于 MISCONF 拒写、沙箱内起不了私有
   Redis，见 `h1_deadline_propagation_20260927.md` §4）。
5. **真实端到端**：没有在真实 UI 上做"同键重复提交只跑一次"的实机复验（本会话不登录、
   不伪造会话）。

## 6. 文档更正（H3 明确要求）

`docs/DeepSeek执行状态.md` 的"闸门观察"把 clean-env-e2e 偶发收执超时写成
"判定为 runner 侧抖动，**不是本次改动引入的回归**"——该结论超出证据。
已更正为 **"间歇性收执失败，根因未确定"**，并写明用本批落地的提交时间线定位
（received/published/persisted/consumed/started 五段 + 实例 + 代码版本），
不放宽 60 秒收执判据，也不靠"反复重跑到绿"。
