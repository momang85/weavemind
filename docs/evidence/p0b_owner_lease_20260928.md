# P0-b：实例归属改为真实租约（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §2 第 3 条「实例归属须有真实租约」。
基线 `951fcb7`（P0-a）。

## 缺陷（三个洞，都在旧 `claim_orchestrator_ownership`）

| 洞 | 旧代码 | 后果 |
|---|---|---|
| 心跳只写一次 | 认领时 `setex(OWNER_HB_KEY, 30, inst)`，之后**再没续过** | 30 秒后心跳过期，**A 还活着** B 就能"接管" → 两个编排器同时消费 `orchestrator:main` |
| 同名实例直接放行 | `if cur == inst: got = True` | 两个进程配成同名实例（同机两份拷贝/同配置跑两次）时，B 把 A 的**活租约**当成"我自己的旧租约"并放行 |
| 认领异常 fail-open | `except: return True, "归属未确认…按单实例继续"` | 证明不了唯一执行权仍照常派发 → 重复执行 |

另外续租若只用 `SETEX` 心跳键，**谁都能续**：被接管后旧持有者还能给自己"续命"。

## 修复

- **每进程唯一持有令牌** `_owner_token(instance)` = `实例名:pid:随机6字节`。
  同名实例不再是放行理由——令牌不同就必须走"租约是否过期"这一条。
- **认领/续期/拒绝三合一，Lua 原子**（`_OWNER_CLAIM_LUA`）：空则 `SET … PX` 认领；
  是本令牌则 `PEXPIRE` 续期；别人则 `0`。避免 `GET`→`SET` 之间两个进程同时判"没人持有"。
- **租约取代一次性心跳**：TTL 就是存活证明。`start_owner_lease_renewal()` 后台每 TTL/3
  续一次；失租即置 `held=False` 并尝试重新认领。`OWNER_HB_KEY` 仅为兼容保留。
- **续租必须带令牌比对**（`renew_orchestrator_ownership`）：只有租约仍是自己的才延长。
- **派发闸门 `ownership_held()`**：`accept_task_request` 与 `resume_received_tasks`
  在入口检查；未持有 → accept 回 `rejected:no_ownership`（返回 False，提交方看到失败），
  resume 直接返回 0（不捡任何任务）。**证明不了唯一执行权就不执行。**
- **认领异常语义**：仍**不阻断启动**（可见性问题），但状态记为**未持有** →
  闸门关闭 → 停止新派发（安全问题）。两条要求同时满足。
- **遗留键迁移（真实运维陷阱）**：旧实现 `SET owner inst` **不带 TTL**，那个键永远在、
  也没有存活信息。新实现会把它当"别人的活租约"→ 编排器**永久拒绝启动**。
  为此加 `_OWNER_LEGACY_TAKEOVER_LUA`：**仅当 `PTTL == -1`**（确实没有过期时间）时才
  原子覆盖；有租约的一律不动。
- `release_orchestrator_ownership()`：优雅退出时释放，且**只删自己的**租约。

## 定向验证

`python -m unittest test_startup_readiness.TestOrchestratorOwnership` → **9 项 OK**：

| 用例 | 断言 |
|---|---|
| `test_first_claim_wins` | 认领成功 + 闸门打开 + **租约里写的是"实例名:pid:随机"令牌** |
| `test_same_instance_name_in_another_process_is_refused` | **同名不同进程 → 拒绝**（指令 §2.3 的核心） |
| `test_second_live_instance_is_refused` | 别人活租约 → 拒绝，且**闸门关闭** |
| `test_dead_owner_is_taken_over` | 租约过期 → 允许接管，租约落到本进程令牌 |
| `test_redis_failure_does_not_block_startup_but_stops_dispatch` | 启动不阻断，但**闸门关闭** |
| `test_renew_requires_still_owning_the_lease` | 被接管后**续期不得成功** |
| `test_release_only_deletes_own_lease` | 不得误删他人租约 |
| `test_legacy_owner_key_without_ttl_is_migrated` | 无 TTL 遗留键 → 接管成功并注明"接管遗留键" |
| `test_dispatch_gate_blocks_accept_and_resume_when_not_held` | 未持有 → accept 拒绝、resume 返回 0 |

闸门波及面（显式声明，不是放宽）：`test_task_persistence` 的 3 个 `setUp` 与
`test_writer_consolidation.TestAcceptTaskRequest.setUp` 各加一行
`mock.patch("orchestrator_v2.ownership_held", return_value=True)` ——
**这些用例测的是收执/幂等/时间线，不测派发闸门**；闸门由上面 9 项专门覆盖。

## 真实 Redis 验证（用户重启服务后补做，`2026-09-28 10:23`）

16 个服务于 `10:20:14` 重启（加载本批代码）。裸 RESP 直读 Redis：

```
orchestrator:owner     = inst-5b43723245:17604:f2324b9b56ca     ← 新格式：实例名:pid:随机
orchestrator:owner     PTTL = 25399 / 29398 / 23397 / 27398 ms  （每 6 秒采样一次）
orchestrator:owner:hb  = (nil)   PTTL = -2                       ← 旧心跳键已不再使用
```

判定：
- **租约带 TTL** 且**续租真的在续**——4 次采样都在 23～29 秒高位震荡 = 每 10 秒
  （TTL/3）被重新写入；此刻距启动已 3.4 分钟，**旧实现下 TTL 早在 10:20:44 归零且永不恢复**。
- **每进程令牌**生效（`pid=17604` + 随机串），不再是裸实例名。
- **遗留无 TTL 键已被迁移**：该键现在装的是令牌而非旧实例名，`:hb` 键消失。

这直接钉死了本批要修的第一个洞（"心跳只写一次"）。

## 未验项

- **跨租期的两进程互斥仍未验**：本机是单实例；"A 活着时 B 不得接管 / A 崩后 B 可接管"
  目前只有单测（`TestOrchestratorOwnership` 9 项）覆盖，**未做真实双进程**。
  按指令"真实 Redis 验证只用隔离前缀/任务库、不杀用户服务"——单实例在跑，
  故只做了**只读**采样，未起第二个编排器去抢租约。
- 未接优雅退出的 `release` 调用点（`main()` 的 `listen()` 是死循环，加 try/finally 会
  大改缩进）；靠 TTL 过期接管兜底，属已知取舍。
- **P0-c（QUEUED 崩溃窗口恢复）/P0-d（同键并发原子绑定）/P0-e（时间线分段）未实施**，
  故"同键并发只留一个可执行收执"**仍未验证**。
