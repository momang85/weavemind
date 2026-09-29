# Q0-② 旧 owner 失租闸门与本地有效期（失败→通过证据）

- 基线：`5fe791e`（本批在其上做 Q0 的第二个小提交；与 Q0-① 互不依赖）。
- 审查依据：`docs/金融分析与受控建模阶段Q_20260929.md` §7 Q0「执行权」、
  `docs/evidence/20260929-stage-review.md` 表格第 1 行。
- 范围：`orchestrator_v2.py`（租约有效期、任务绑定、派发/落库闸门、时间线代号）、
  `task_state.py`（`record_submit_event` 记持有期代号）、`test_startup_readiness.py`。
- 未改：TTL 数值（仍 30s）、租约键名、认领 Lua、`ownership_held()` 的调用点语义；
  未动模型/权限/代理/模板/阈值；未对真实库做故障注入（演练全程用进程内替身）。

## 1. 复现（改前实测）

```
claim: True | held = True | fp = 35b57d1e5a6a00f2
失租后 held = False | lost = True
反例① _lease_superseded(同指纹, 已失租) -> ''
  距上次续租 25.0s（服务端 TTL=30s）-> held=True
  距上次续租 31.0s（服务端 TTL=30s）-> held=True      ← 服务端租约已过期、对端已可合法接管
  距上次续租 35.0s（服务端 TTL=30s）-> held=True
  本地有效期上限 = 40.0 s，服务端 TTL = 30 s          ← 两处方向都反了
```

两条独立缺陷：

1. **同 owner 指纹直接放行**：`_lease_superseded` 的第一条就是
   `start_owner == _owner_fingerprint() → return ""`。于是"旧 owner 被挂起 → 租约过期 →
   新 owner 接管 → 旧 owner 恢复"这一串里，旧持有者**指纹没变**，它继续派发、继续落终态，
   与新 owner 的进度互相覆盖。判据必须改成"**先证明执行权，再看任务绑定**"。
2. **本地有效期 = `TTL + 宽限 10s`**，比服务端租约**长 10 秒**：`SET … PX 30000` 一到期
   对端即可合法认领，而本端到 40 秒才判失租——中间那 10 秒两个 owner 并存。
   方向必须反过来，且必须**只减不加**。

## 2. 修法与改后读数

- `OWNER_LEASE_SKEW = 1.0`、`OWNER_LEASE_GRACE = 0.0`（兼容位，**不得**再加到有效期上）；
  `_OWNER_STATE` 新增 `expires_mono` / `lease_ttl`。
- 新增 `_owner_remaining_ttl(r)`（读服务端 `PTTL`，读不到退回满 TTL）与
  `_owner_effective_ttl(r)`（**服务端剩余 − 保守余量**，永不超过 TTL）；认领/续租成功
  一律由 `_owner_note(..., lease_ttl=…)` 记同一个单调钟截止。
- `ownership_held()`：布尔量 **且** 本地有效截止未过；没有可证明的截止即判未持有。
- `_task_start_binding(task_id)`：读 `started` 事件里的 `(owner 指纹, 持有期代号, 实例)`；
  `_task_start_owner()` 保留为薄包装。`run_and_finalize` 落 `started` 时一并写
  `owner_gen`（`task_state.record_submit_event` 新增该字段）。
- `_lease_superseded()` 新判据（顺序即优先级）：
  1. 现在**确实持有**活租约 → 放行（代次比本进程更新属异常，拦）；
  2. **曾持有、现已失去** → 一律拦（不看指纹）——这条就是反例① 的正解；
  3. 从未持有 + 任务**没有任何绑定** → 放行（旧行/离线单机，**唯一旁路**，
     且每个任务 id 打一条 warning 使其可见）；
  4. 从未持有 + 绑定的是**本进程**指纹 → 拦（令牌还在、租约已判失效）；
  5. 从未持有 + 绑定的是**别人**指纹 → 拦（消息是广播的、启动恢复也扫库，
     证明不了执行权却去跑别人的任务 = 重复消费；旧实现即如此，本批只收紧）。

改后实测（同一炮制脚本）：

```
effective local validity = 29.0s (server TTL = 30s, skew = 1.0)
  t=+  0.0s held = True        t=+29.1s held = False
  t=+ 25.0s held = True        t=+30.0s held = False
  t=+ 28.9s held = True        t=+35.0s/40.0s held = False
no-PTTL fallback effective local validity = 29.0s
同指纹已失租 -> superseded: '任务启动时是本进程持有（指纹相同）但本进程**已失去有效租约**（迟到结果不得覆盖新 owner；停止派发/落库）'
```

## 3. 定向用例（`test_startup_readiness.TestOrchestratorOwnership` 16 项 OK）

| 用例 | 断言 |
|---|---|
| `test_same_owner_fingerprint_is_refused_once_the_lease_is_lost` | 持活租约+同指纹→放行；**本地截止一过，同指纹也必须拦**；从未持有+别人绑定→拦；无绑定→放行 |
| `test_local_validity_never_exceeds_the_server_lease` | `expires_in <= ttl`；`grace == 0`；到期前后逐点核对闸门 |
| `test_resumed_old_owner_cannot_dispatch_or_finalize_after_takeover` | 进程内两套本地视图 + 一个共享替身租约：A 认领(gen1) → 任务在 A 名下 started → A 挂起(截止过期) → **B 接管(gen2)** → 切回 A 视图：`_lease_superseded` 与 `_finalize_task` **都被拒**；B 自己的任务照常放行；替身上的令牌仍是 B 的 |
| `test_stale_lease_is_not_held_after_the_local_validity_window`（更新） | 一小时前的截止 → 失租、`ownership_lost()` 为真、`expires_in < 0` |
| `test_dispatch_is_refused_when_the_task_start_lease_was_superseded`（更新） | 派发闸门仍拦被取代者；绑定了代次；无绑定旧行不拦 |

替身 `_R` 补 `incr`/`pttl`（此前缺 `incr` → 所有用例的 `gen` 恒为 0，等于"代次检查"从未被测到）。

## 4. 真实链范围与仍未验

- 真实链：**本轮未起真进程**。上一批（`5fe791e`）已在隔离栈（便携 Redis 6390 +
  临时 DB + 真 `orchestrator_v2.py` 进程）验过"第二实例拒绝启动 / TTL 过期后接管 gen 递增 /
  收执恢复"，本批只改**进程内**判据与有效期算法，因此用进程内替身 + 两套本地视图覆盖。
- **仍未验（如实列出，不含糊）**：
  1. "旧 owner 真进程挂起 → 新 owner 真进程接管 → 旧进程恢复"的**跨真进程**演练
     （需要能对真进程发 SIGSTOP/恢复；本轮用进程内两视图替代，属**降级证据**）；
  2. 离线/单机的**显式模式**尚未实现：目前"任务无 `started` 绑定"仍按离线放行
     （已加 warning 使其可见，但架构要求的"显式模式"是独立工作项）；
  3. `OWNER_LEASE_SKEW=1.0` 对高延迟网络的适用性未实测（只做了方向性论证：
     少信一秒 = 提前判失租 = fail-closed）。
