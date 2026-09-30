# T0-b 执行许可与取消（不可变 attempt/epoch + 副作用前唯一闸门）

批次：阶段T `T0-b`（09-30 晚扩大审查 F03/F04）。基线 `8fff008`，前置 `d325cc4`（T0-a）。
**未改**用户模板/模型/代理/权限/密钥/人审；**未向真实运行任务注入故障**；
跨进程验收用真实 Redis 的**专属前缀**（`wm:permit:t0bcheck:`，结束时清理）。

## 1. 许可表（先画后改）

| 环节 | 谁核验 | 判据 | 不通过时 |
|---|---|---|---|
| 编排：进入 `run()` | 编排器 | 认领/续租租约 → 持有期代号 `gen` | 拒绝启动（既有） |
| 编排：任务开始（含恢复/接管） | `start_task_thread` | 新 `attempt_id` + 当前 `gen` → 写入可信记录 | 建不出记录 → 有日志，派发会被拒 |
| 编排：`_dispatch` **入口** | 编排器 | 租约绑定 `gen` 必须**等于**当前 `gen`（T0-b 由"只拦更新"收紧为"不等于就拦"） | FAILED，零副作用 |
| 编排：**入队前**（等 worker 之后） | `_send_gate` | 租约 + 许可（attempt/epoch/截止/取消）**再核一次** | FAILED，**零 lpush** |
| 编排：结果回来 | `_dispatch` | 结果必须属**当前尝试**（attempt/epoch/取消）；旧尝试结果丢弃 | FAILED + `stale_attempt`，不采纳 |
| 编排：终态落库 | `task_state.record_completion` | 写者 attempt == 可信记录当前 attempt | 拒绝写（库里保持原状） |
| worker：取到任务 | `_process_task` | 按**可信记录**核许可（不是内存守卫） | 不 execute，如实回传未执行 |
| worker：每次 LLM 发送前 / 切备用前 / 每次重试前 | `llm_client._cancelled()`（由 worker 装许可守卫） | 可信记录里的 cancelled/attempt/epoch | 抛 `LLMCancelledError`，**零新发送** |
| worker：发布结果前 | `_process_task` | 许可仍有效 | 不发布结果，如实回传 |

## 2. 反例（先复现，再修）

| 编号 | 主审反例（原话） | 复现读数 |
|---|---|---|
| F03-a | 等待 5s 失租仍 `lpush` | 进入时租约有效、等 worker 期间失租 → **旧：照样入队** |
| F03-b | gen1 续程在进程获 gen3 时又放行 | `_lease_superseded` 只拦 `start_gen > my_gen`；`start_gen=1, my_gen=3` **旧：放行** |
| F03-c | 终态无 attempt CAS | `record_completion` 无 attempt 概念 → 旧尝试的迟到结果**旧：直接覆盖** |
| F04-a | 取消守卫恒真 / worker 不继承编排器守卫 | worker 进程从不注册守卫；异步路径**（llm_client:2518）**发送前零检查 |
| F04-b | 排队后取消仍 execute | 旧：worker 取到任务照样跑完（付费调用照发），结果回来才被丢弃 |
| F04-c | 旧 attempt 结果不能发布 | 旧：等待期间换了尝试，迟到结果仍被当成功采纳 |

**失败→通过**（新增用例在 `test_cancel_semantics.py`，共 57→62 条）：
- 编排/终态部分不带修复：**3 失败 + 2 报错**；
- worker/async 部分不带修复：**2 失败 + 1 报错**；
- 修复后 **62 OK**。

## 3. 最小修复

新增 `execution_permit.py`（窄接缝，只依赖标准库 + 可选 Redis 客户端，不 import web_ui/orchestrator）：
`ExecutionPermit(root_task_id, attempt_id, epoch, deadline, owner, cancelled)` **不可变**；
`MemoryPermitStore` / `RedisPermitStore`（多进程真源）；`verify()` 的拒绝理由是稳定字符串
（`no_current_permit` / `attempt_mismatch` / `epoch_mismatch` / `cancelled` / `deadline_passed` /
`owner_mismatch` / `root_task_mismatch`），**没有可信记录 = 未知 = 拒绝**。

- `orchestrator_v2.py`：`_task_permit()`（旧持有期的许可**不重签**，直接拒）、
  `_send_gate()`（入队前再核租约+许可）、`_gate_refuse()`（拒绝统一可见）、
  等待结果后按当前尝试判"采不采纳"、`request_cancel()` 把取消**落到尝试记录**（跨进程可见）、
  `start_task_thread` 为每次运行**创建新尝试**、`run()` 装配记录（Redis 优先；退回进程内会打日志）。
- `worker_base.py`：`_task_permit/_permit_gate/_install_permit_guard/_publish_refused`；
  **执行前**与**发布前**各核一次许可；`_task_loop` 装配记录（Redis）；结果回显尝试身份；
  守卫按任务装、按任务清。
- `llm_client.py`：异步路径补上**发送前**取消检查（健康路由与每次尝试之前各一处）。
- `task_state.py`：`record_completion(attempt_id=…)` + `_attempt_is_current()`——旧尝试的终态被拒；
  没带代号的老调用点放行但**留痕**（旁路可见，不是静默）。

## 4. 隔离双进程验收（10/10）

`scripts/t0b_permit_isolation_check.py` → `docs/evidence/t0b_permit_isolation.json`。
每个步骤都是**新起的子进程**，记录写在真实 Redis 的专属前缀下：

| 场景 | 读数 |
|---|---|
| A 建立尝试（gen=1）→ B 进程核验 | `ok=True`（跨进程共享真源） |
| C 接管并换代号（gen=3，新尝试） | 新 attempt ≠ 旧 attempt |
| B 进程核验**旧**许可 | `ok=False, reason=attempt_mismatch`（不得借 gen3 复活） |
| 取消（写在 C 的当前尝试上）→ B 核验 | `reason=cancelled` |
| worker 子进程收到被取消的许可 | `executed=[]`、回传 `CANCELLED` |
| worker 子进程收到旧尝试的许可 | `executed=[]`、回传 `FAILED` |

**这条验收当场抓到一处真缺陷**：worker 从不装配 Redis 许可记录 →
`_permit_gate` 读到的是**本进程空记录** → 回传 `no_current_permit`。
也就是说"worker 不继承编排器守卫"不只是测试问题，而是**装配缺失**；
已在 `worker_base._task_loop` 补 `_install_permit_store()`（Redis 优先，退回进程内会打日志）。

## 5. 定向回归（逐文件真实退出码）

`test_cancel_semantics.py` 62 OK、`test_orchestrator_v2.py` OK、`test_root_budget.py` OK、
`test_offline_delivery.py` OK、`test_p0.py` OK、`test_deploy_manifest.py` OK、
`test_delivery_chain.py` 408 OK。

## 6. 未验 / 残留（不假装）

- **旧 worker 写 attempt 私有暂存产物、正文/图/记忆/模板发布走当前 attempt 的 CAS**：
  本批只做到"结果与终态不得跨尝试发布"，**暂存目录私有化与交付物 CAS 未做**，
  与 T2-a（采纳事务/提交）同批更合适，现如实登记为未完成。
- **供应商在飞**：取消后我方零新发送已验；已发出的远端请求仍可能计费，
  路径上如实记 "在飞、待对账"（`_mark_inflight_unsettled`），**不宣称取消已撤销远端费用**。
- **真实多进程/Redis 压力**：本验收只用本机便携 Redis 的专属前缀与两个子进程，
  未做压力与崩溃注入；不对真实运行任务注入故障（架构要求）。
- **Python 3.11**：扩展审查在 3.14 下跑的复现，本批定向回归在本机 3.14 通过；
  发行环境 3.11 需同样定向复验（未做即不声称）。
