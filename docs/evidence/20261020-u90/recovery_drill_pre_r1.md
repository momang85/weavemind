# U90-D 隔离环境「中断—恢复」演练（2026-10-20）

脚本：`.tmp/u90_recovery_drill.py`（隔离运行，未触碰任何真实在飞任务或共享服务）
原始记录：`docs/evidence/20261020-u90/recovery_drill.json`

## 隔离边界
- 临时数据根 + 临时 `agents.db` + 临时工作区（脚本结束即删）；Fake Redis / Fake 消息总线。
- 真实组件：`task_state`（真 SQLite 库、真 CAS 断点恢复标记）、
  `OrchestratorV2._execute_steps(..., resume_completed=True)`（真恢复链路、真依赖传播）。
- 唯一替身：`_dispatch_step_safe`（记录派发序列并回 SUCCESS）——它代表"真花钱的那一步"，
  演练要证的正是**它被调用了几次**。**未杀任何真实任务、未重启共享服务。**

## 场景与结果
中断形态：计划 5 步（1 web_search、2 content_analysis、3 data_analyzer、
3a content_summary、4 report_generator）；落库 1/2 = SUCCESS，3 = FAILED，3a/4 未落
（相当于进程在 3 之后被杀）。

| 步骤 | 恢复前落库态 | 恢复后 | 是否重新执行（=是否再计费一次） |
|---|---|---|---|
| 1 web_search | SUCCESS | 复用（`resumed: true`） | 否 |
| 2 content_analysis | SUCCESS | 复用（`resumed: true`） | 否 |
| 3 data_analyzer | FAILED | 新执行 `fresh-3` | 是（本应重跑） |
| 3a content_summary | 未落 | 新执行 | 是（本应重跑） |
| 4 report_generator | 未落 | 新执行 | 是（本应重跑） |

- 恢复前 `task_state` = FAILED；`mark_resumed`（CAS 单赢）= True，状态 → RUNNING。✔
- 派发序列 = `['3', '3a', '4']`：**只跑未完成步骤，无重复派发**（无重复付费、无重复打包）。✔
- 依赖传播：3 依赖的 2 是复用步骤，未被判 "Blocked by" 连锁失败。✔
- 结果覆盖全 5 步、`has_failure = False`；审计消息：
  「断点续跑（step_resumed）：跳过 2/5 个已完成步骤（1, 2）」。✔

## 负例（U90-B.3 产物有效性）
同一任务、同样落库状态，**删掉工作区目录**（产物无从谈起）后再恢复：
派发序列 = `['1', '2', '3', '3a', '4']`，复用数 = 0 —— 不再"只凭 step_id + SUCCESS"跳过，
按全量重跑重新产出产物。✔

## 结论
- 有效已完成步骤不被重跑：**通过**（1、2 未再计费）。
- 产物/工作区无效时一步都不复用：**通过**。
- 演练全程隔离，未影响真实任务与共享服务：**通过**。
