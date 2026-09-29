# 架构任务收口：跨进程归属 / 租约接管 / 启动恢复（真实进程演练）· 2026-09-29

此前这三项一直挂在"未验"里，理由是**没有隔离环境**（同 Redis 上跑第二个编排器会抢
`orchestrator:main` 的消息）。本轮把隔离环境搭起来了，所以它们从"未验"变成**已验**。

### 隔离环境（不碰正在运行的实例）

| 项 | 值 |
|---|---|
| Redis | 便携版 `redis-server.exe`，**独立端口 6390 / 6391**（真实例在 6379） |
| 任务库 | `%TEMP%\wm_owner_drill_*` 下的独立 `drill.db`（`WEAVEMIND_DB`）（真库 `agents.db` 不动） |
| 进程 | `python orchestrator_v2.py`，环境变量只改 `REDIS_HOST/REDIS_PORT/WEAVEMIND_DB` |
| 日志 | 每个实例一份独立日志文件，保留在 `%TEMP%\wm_owner_drill_*` |

脚本：`%TEMP%\wm_owner_drill.py`（归属/接管）、`%TEMP%\wm_recovery_drill.py`（启动恢复）。

---

## 一、真实跨进程并发：第二个编排器**明确拒绝启动**

```
[A 认领] 编排器归属：本实例 inst-84a341f826 持有租约（令牌 …76b688，TTL 30s，gen=1）
         编排器实例归属：本实例 inst-84a341f826 持有编排器归属
[B 启动] ERROR 拒绝启动：已有编排器实例 inst-84a341f826 在运行（租约 30s 内）；
         同一 Redis 上不允许两个编排器消费同一条任务通道
         退出码 = 2
```

- 两个真进程、同一 Redis、同一任务通道；B **没有**消费任何消息（拒绝发生在订阅之前）；
- 这正是 A-2 "同 QUEUED 两次 mark_running" 的跨进程对照面：pub/sub 是广播语义，
  单靠"每条消息各自裁决"挡不住重复消费，实例归属这道闸门才是真闸门。

## 二、真实租约接管：持有者崩溃后允许接管，且**代次递增**

```
[A 被强杀（模拟崩溃）] 等心跳 TTL（30s）过期
[B 启动] 编排器归属：本实例 inst-84a341f826 持有租约（令牌 …c35fcd，TTL 30s，gen=2）
         编排器实例归属：本实例 inst-84a341f826 持有编排器归属
（第三次启动 gen=3，令牌 …ab149e）
```

- 心跳新鲜期内**拒绝**（第一条），心跳过期后**接管**（本条）——两侧都是真进程实测；
- `gen` 单调递增：旧持有者复活后写的落库会被 `_lease_superseded(task_id)` /
  `ownership_lost()` 挡住（A 批的守卫），跨进程这一面现在有实测支撑。

## 三、真实恢复演练：未被消费的收执被捡回来执行

```
[写入] claim_receipt(...) -> verdict=created，status=RECEIVED（真实接口，非 SQL 手插）
[启动] WARNING 收执恢复：任务 drill-recv-… 收执超过 10s 未被消费，重新执行（goal=drill 恢复演练…）
       INFO Task drill-recv-…: drill 恢复演练…
       INFO Injected context for goal …
[结果] 行状态 RECEIVED → RUNNING（收执确实被消费）
```

`claim_receipt` 的 `created` 裁决 + 启动恢复扫描 + 置 RUNNING，三者都在隔离库里真实发生。

---

## 四、演练里查出的**真缺陷**（已修）

**编排器的退出码没有传播**：`claim_orchestrator_ownership` 拒绝启动时 `main()` 返回 `2`，
但文件末尾写的是裸调 `main()`——进程退出码留在 **0**（实测：日志写着"拒绝启动"、`exit code=0`）。
launcher / guardian 因此**分不清**"明确拒绝启动（同一 Redis 上已有活着的编排器）"与
"正常退出"，重启拉起策略会跟着错（把"拒绝"当成"跑完了"）。

修法：`if __name__ == "__main__": raise SystemExit(main())`。复跑后实测 **退出码 = 2**。

---

## 五、仍未验（不是没做，是环境/真人）

- **第二家公司真实原文**：需要一条新的真实披露材料。用应用的材料入口抓取是**允许**的
  （确定性、不调用模型），但那要起一套隔离的 webui+worker 并等准入/并入完成，
  本轮时间与上下文不足以稳妥做完；**留待下一批**（不得用上一家的材料冒充第二家）。
- **干净环境同包完整链**（`clean_env_verified`）：需要一台没有 Python/Node/Docker 的机器
  （仓库里的路径是 `scripts/e2e_clean_check.py`——它自己会 clone + 起隔离栈，
  本机跑的是"本机环境"，不等于干净环境）。
- **远端 CI**：本仓库没有配置远端流水线。
- **真人 F3 五项 ≥8/10**：必须真人评，代理不得代评。

## 六、本批改动

- `orchestrator_v2.py`：`__main__` 退出码传播（`raise SystemExit(main())`）。
- 证据：本文件。
- 演练脚本落在 `%TEMP%`（一次性工具，不入库；结论与读数已固化在本文件）。
