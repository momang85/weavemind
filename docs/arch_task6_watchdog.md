# 任务6 — stuck running 任务自动回收（watchdog）

## 背景
metrics_summary.json 显示有 1 个任务永久卡在 running 状态无法结算。
config.json 有 task_timeout: 300（秒），guardian_heartbeat: 20（秒），
但当前没有自动将超时 running 任务转为 cancelled 的机制。

## 目标
在守护进程 / orchestrator_v2.py 的心跳循环里增加 stuck 任务回收。

## 判定公式
is_stuck(task) = (now - task.started_at) > task_timeout × 1.5

×1.5 宽限倍数，防止临界任务被误杀。即 300×1.5 = 450 秒后视为 stuck。

## 实现步骤
1. 找到 guardian_heartbeat 心跳触发点（每 20 秒一次的循环）。
2. 在每次心跳里查询 SQLite 中状态为 running 且满足 is_stuck 条件的任务。
3. 将这些任务状态改为 cancelled，写 reason = "watchdog_timeout"，记 audit log。
4. 释放对应的 Redis 队列占位（如有）。
5. 不改任务的实际执行逻辑，只加状态回收。

## 验收
- 修改后手动将 metrics 里那 1 个 running 任务的 started_at 改早 > 450 秒，
  触发一次心跳，确认它被自动转为 cancelled。
- 运行现有测试，无回归。

## 约束
- 只改心跳/守护部分，不动任务执行主流程。
- 改动行数预计 < 40 行。
