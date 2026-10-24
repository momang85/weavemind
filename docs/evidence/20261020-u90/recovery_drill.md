# U90-R1 断点身份与产物校验（2026-10-20 补修）

> 初版（U90-B 的"工作区目录存在"轻量守卫）记录保留在 `recovery_drill_pre_r1.md|json`。
> 本轮按架构师裁决重做判据并重跑演练。

## 根因（架构师独立复现 + 本批确认）

`_load_resumable_steps` 此前只检查**工作区目录存在**，随后按 `step_id` + `status==SUCCESS`
复用，并把落库的 `result` 文本当作已完成结果注入下游。架构师离线复现的反例在真机上成立：
空目录仍复用；同 `step_id` 换 capability/instruction/input_hash/contract 仍拿 `old result`。
交审报告里"计划/契约/输入指纹不一致或产物缺失即拒绝复用"的描述与代码不符（如实退回）。

## 补修（`orchestrator_v2.py`，不新建恢复引擎）

复用前必须证明三件事，证明不了就重做（`_load_resumable_steps`）：

1. **工作区在**（既有检查）。
2. **身份一致**（`_resume_step_identity_verdict`）：
   - 执行契约：`contract.matches(stored["contract"])`——与派发路径同一判定；落库步骤没有
     契约身份也算"证明不了"；
   - capability / depends_on 与当前计划一致；
   - 指令：当前计划的核心指令**相同、或被落库指令完整包含**（派发时运行期会在核心指令上
     追加运行上下文——实测落库 3000~11000 字 vs 核心 120~570 字；核心被改写/换查询面时
     既不相同也不包含）；
   - 上次执行若由**重规划代做**（`result.replanned` / `replan_instruction` /
     `worker_mismatch`：能力-执行者错配后换了指令）→ 不复用。
3. **产物在**（`_resume_artifact_verdict`）：
   - 步骤结果里**自己记录**的产物路径（file_io 的 `path`/`files[].path`、report 的
     `report_path`、package 的 `zip` 等）必须全部存在（只认工作区内路径）；
   - 能力必需产物表 `_RESUME_STEP_ARTIFACTS`（每条都注明既有消费者）：
     web_search/web_fetch → `project/fetch_snapshot.json` 或 `narrative_evidence.json`；
     content_summary → `narrative_evidence.json`；data_analyzer → `project/working_paper.json`；
     report_generator → `reports/report.md`；package → `export_manifest.json`；
     未列出的能力（file_io/data_loader/code_execution/model_trainer/react_agent）没有固定
     产物：一条记录路径都没有就是"无法证明"。
4. **后继失效**：任何直接/间接依赖被拒步骤的当前计划步骤一并重做（上游输入已变）。
5. 审计：`push_progress` 输出"断点复用裁决：可复用 x/y（…）；按需重做 n 步：id（原因）…"。

## 定向反例/正例（离线，真函数）

`r1_resume_validity.json`（只读探针：真实 agents.db + 真实任务工作区 + 真
`_load_resumable_steps`；反例在临时库/临时工作区上跑，不碰服务与在飞任务）：

| 用例 | 结果 |
|---|---|
| 正例：身份一致 + 产物在 | 复用 `["1"]`（跳过） |
| 反例①：空目录（目录在、产物不在） | 复用 `[]` |
| 反例②：单个必需产物缺失 | 复用 `[]` |
| 反例③a：同 ID 换能力 | 复用 `[]` |
| 反例③b：同 ID 换指令 | 复用 `[]` |
| 反例③c：同 ID 换依赖 | 复用 `[]` |
| 反例④：执行契约指纹变化 | 复用 `[]` |
| 正例2：核心被落库指令包含（运行期追加上下文） | 复用 `["1"]` |

单元测试：`test_task26_checkpointer` 50 用例（含 12 项 R1 反例/正例）全绿；
`test_checkpointer` + `test_orchestrator_v2` 115 用例全绿。

## 隔离演练（`recovery_drill.json`；本轮按 R1 规则重跑）

真 `task_state` + 真 `_execute_steps(resume_completed=True)`，只桩掉真正花钱的派发：

| 用例 | 派发序列 | 结果 |
|---|---|---|
| 正例（身份+产物有效，1/2 已 SUCCESS） | `['3','3a','4']` | 跳过 2/5 步、无重复派发、审计事件在 ✔ |
| 反例①工作区被删 | 全部 5 步 | 全量重跑 ✔ |
| 反例②空目录 | 全部 5 步 | 全量重跑 ✔ |
| 反例③缺 `narrative_evidence.json` | `['2','3','3a','4']` | 该步 + 后继重做，步骤 1 仍复用 ✔ |
| 反例④同 ID 换指令 | 全部 5 步 | 全量重跑 ✔ |
| 反例⑤步骤 1 由重规划代做 | 全部 5 步 | 全量重跑 ✔ |

`findings.PASS = true`（11 项断言全过，含 `no_duplicate_dispatch`、`step_resumed_audit`）。

## 真实任务上的裁决（如实）

`r1_resume_validity.json` / `r1_reasons.json`（两个真实任务的逐步骤原因）：

| 任务 | 落库 SUCCESS 步骤 | 本轮复用 | 原因 |
|---|---|---|---|
| `ui-cc853cf86b`（三一） | 1/2/3/3a/4/package-7 | **0** | 1、3a = 上次由重规划代做；2 = 落库无契约身份；3、4 自身身份+产物都通过，但上游 1 被拒 → 连带重做；package-7 不在当前计划 |
| `ui-91677c7767`（洋河） | 1/2/4/5/package-6 | **0** | 落库记录早于契约身份落盘（`contract` 为空）→ 全部证明不了；file_io 也未记录工作区内产物路径 |

**诚实结论**：判据是可区分的（三一 step 3/4 的"自身有效"确实通过了身份与产物两道检查，
只是被上游连带规则拦下——不是一律拒绝），但本机现有真实记录上**没有一步满足"全链可证明"**：
`docker` 不可用导致能力-执行者错配（重规划代做）普遍存在，10-20 之前的记录又没有契约身份。
因此真实样本复用数为 0、恢复按全量重跑走——这是"证明不了就不跳过"的正确一侧，
代价是省不下这部分成本（如实列出，不粉饰）。可复核的跳过证据是上面的离线正例与隔离演练。

## 已知连带影响

`_execute_steps` 派发时会**就地把运行上下文注入 `step["instruction"]`**（P1-c① 的规范写回）：
调用方持有的计划对象会被改写。R1 的"核心被包含"判据正是为此设计；单测/演练里必须给每个
用例独立的计划副本，否则上一用例的注入会污染下一用例的身份比对（演练首轮即因此误判，
已修正为 `copy.deepcopy`）。
