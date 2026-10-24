# Lead 侧独立复核与二栈残留清理记录（2026-10-21 16:37 +0800）

复核人：Team Lead（派发工单的会话）。性质：**只读复核 + 一次环境修复**，未重跑工单、未新增任何付费调用（0 次 LLM 调用）、未改任何代码。
本文件不改动 `report.md` 的任何结论；只补充 Lead 独立复算结果、两处事实更正、以及一次孤儿进程清理。

## 1. 独立复算（不信执行会话的自报值，逐项对字节重算）

| 复核项 | Lead 结果 | 与执行会话报告 |
|---|---|---|
| 51 个既有工件（17 zip + 34 pdf）sha256 重算 | **全部一致，0 改写 0 缺失** | 相符（执行会话写的 diff 结论成立） |
| 新包 25 个冻结成员逐成员哈希 | **全对**（`lead_verify_pkg.py`，25/25） | 相符 |
| 同版三处一致：manifest.report_version_id = selection.adopted_identity = 页面 current/manifest_version_id = `9751ef20b489…bb13` | **一致** | 相符 |
| 页面下载 MD 字节 = manifest.delivered_md_sha256（`034112ed…59bc`） | **一致**（实测 live `/api/task/<id>/report.md` 响应头 `X-Report-Version-Id` 同身份） | 相符 |
| 页面下载 ZIP 字节 = 工作区冻结包 = 证据副本（`62d0c6fb…2d53`） | **三方逐字节一致** | 相符 |
| R4 声明原文在包内正文逐字命中（「本次外部检索降级运行…」） | **命中** | 相符 |
| llm_calls.jsonl 行数 | **38 行** | 相符（费用未测=价格表缺失，维持原结论） |
| config.json 用户 | 存在 `u90new21`（与 `momang`、`reviewer` 并列；凭据未读取未记录） | 相符 |

## 2. 事实更正一：二栈「全部清理」未成立，Lead 已补清理（环境修复）

执行会话报告 §0 称第二栈子进程已全部结束。**Lead 实测不成立**：14:23:16 / 14:23:28 / 14:24:09 三批共 **30 个孤儿进程**（11 worker + critic + guardian + metrics 的两代整栈，外加 2 个后续 guardian/metrics）在报告落笔时**仍在运行**，且都挂在同一条共享 Redis 上（实测 `blocked_clients=33` ≈ 11 worker × 3 套栈；第二/三栈的 orchestrator/webui 已死，只剩消费端）。其风险：孤儿 worker 可抢消费任何新任务的队列、CPU 三倍负载（与闭环期间出现的 `503 system cpu overloaded 96.7%` 相符）、且其工作区根指向 DSH 会话临时目录。

**Lead 处置（2026-10-21 16:3x +0800）**：按「创建时间 14:22–14:26 + python 服务进程」精确圈定 30 个 PID（与原栈 12:40:30 的 16 个 PID 完全不相交；先确认 inflight=0），`Stop-Process -Force` 一次性清理。清理后复验：
- python 进程 46 → **16**，全部为原栈（12:40:30 一代）；
- Redis `blocked_clients` 33 → **11**（原栈 11 个 worker 独占消费），客户端 158 → 76；
- 8080 正常（GET / 200；`/api/task/ui-d59152ebb9/report.md` 200，`X-Report-Version-Id` 仍为采纳身份）；
- 原栈 16 进程、任务历史、队列（空）均未受影响。

原栈未受损不是执行会话的功劳——它清理时确实漏掉了这 30 个（其会话令牌对非本会话子进程 `Access denied`，只能杀自己拉起的部分）。`pids.json` 丢失与 `runtime_ports.json`（web:8081）改写两项遗留副作用，执行会话已如实披露，维持原状（launcher 下次 `stop`/`restart` 有进程扫描兜底，会自行重建账本）。

## 3. 事实更正二：`downloads/ui-d59152ebb9.pdf` 是冻结成员，不是「下载PDF」按钮字节

README 复核命令注释里 pdf 一行标为「== manifest.pdf_sha256」，与 report.md §3 的如实观察并不矛盾但易误读。Lead 实测两条路径并存、版本头一致：
- `/api/task/<id>/pdf`（页面「下载PDF」按钮）→ 5,526,922 B，`83a93ec4…9931`（按需重生成）；
- `/files/<tid>/reports/report.pdf`（冻结成员）→ 5,517,433 B，`35c66f80…212f`。
证据目录里的 `downloads/ui-d59152ebb9.pdf` 是**后者**（冻结成员副本）。两者响应头同报采纳身份，无错版混同；是否把按钮字节与包内成员对齐，维持 report.md §9-4 的「留给产品判断」。

## 4. task_history 行数说明

Lead 复核时任务表为 **567 行**（before 565 / after 566）。第 567 行是产品自身的每日定时任务 `sched-f6e5440b8b`（16:00:16 提交，SUCCESS_WITH_ISSUES），**非本工单产物、也非触碰历史行**。工单口径的「565→566，仅 +1 行」仍然成立。

## 5. 维持不变的原结论

- R1 断点复用正例、R3 恢复路径四项：**未验 + 原因**，Lead 认可其六条不可行理由，不升级为已验证；
- 采纳第一次 409 回滚→第二次成功：**真实实机行为**，Lead 认可（不冒充 R3）；
- 授权真实运行 1/2 次已消耗；本复核零新增付费调用；
- 看板卡片 `3d08c093…` 停在 failed 列：该记录来自一次 15 秒即错的执行尝试（agent turn error），而同一会话其后经 goalRun 续跑至完成并产出本目录全部证据——列状态与实际产出不一致，属看板账实差异，Lead 未改动卡片（保留原始记录，是否移列由用户裁决）。

## 6. 复核脚本（Lead 会话产物，仓库根 .tmp/，未提交）

- `.tmp/lead_verify_new_user_loop.py` — 旧工件 sha256 重算 + 新增文件比对；
- `.tmp/lead_verify_pkg.py <zip>` — 包身份三处一致 + 25 成员哈希 + R4 声明命中 + 任务表读数；
- `.tmp/redis_readonly_probe.py` — 只读 RESP 探针（CLIENT LIST / PUBSUB / 队列长度）。

边界：零代码改动、零密钥/模型/权限/阈值改动、未 commit/push/建分支；仅新增本文件并在 README 目录一节追加一行索引。
