# 受控重启计划（P1-E 准备件）— 只准备，不执行

> 状态：**待用户授权后执行**。本文件只给出步骤与判据；本批次不重启在线服务、
> 不触发真实 LLM/付费数据调用、不修改生产配置与凭据。
> 在线实例（截至 2026-10-23）：端口 8080，PID 24572，启动于 10-22 17:08:16，
> 源码其后已多次改动 → `/api/health` 的 `build.restart_required` 应为 `true`
> （重启前可用它二次确认"确实在跑旧代码"）。

## 0. 重启要解决的问题

1. 本会话修复（P0-A/B/C、P1-D、P1-E：稳定制品寻址、出口诊断、来源准入、
   契约闭环、检索证据分账、停损、构建指纹、日志隔离）**全部只在新代码里**；
   在线实例仍是旧代码。
2. 重启后必须能证明"在线已在跑新代码"：`/api/health` →
   `build.source.restart_required == false` 且 `build.started_at` 更新。

## 1. 前置检查（全部满足才动）

| # | 检查 | 命令/判据 | 不满足时 |
|---|---|---|---|
| 1 | 无运行中任务 | `sqlite3`（经 task_state.DB_PATH）：`SELECT status,COUNT(*) FROM task_history GROUP BY status` 中不得出现 `RUNNING/QUEUED/PENDING`；`/api/status` 的 agents 无 `running` | 有任务 → 等它终态，或用户明确放弃该任务 |
| 2 | 队列已清空 | `python launcher.py queues` 全 0 | 有积压 → 消化完或明确丢弃 |
| 3 | 工作区可读 | 任选 3 个历史任务 `/api/task/<id>` 只读打开，产物/报告/来源在 | 有读不到的 → 先修寻址（P0-A 已修，重启前复验） |
| 4 | 备份完成 | 见 §2 | 备份失败 → 不重启 |

## 2. 备份与回退

**备份（只读拷贝，不动原文件）：**

```
# 会话开始时的 git 状态清单已存档（本目录 evidence）
git status --porcelain  > docs/evidence/20261022-runtime-repair/pre-restart-git-status.txt
git diff                >> docs/evidence/20261022-runtime-repair/pre-restart-git-status.txt
# agents.db 只读备份（SQLite 在线拷贝）
python -c "import sqlite3,task_state,shutil,os; \
src=task_state.DB_PATH; dst='docs/evidence/20261022-runtime-repair/agents-db-backup/db.bak'; \
os.makedirs(os.path.dirname(dst),exist_ok=True); \
con=sqlite3.connect(src); con.backup(sqlite3.connect(dst)); con.close(); con.close(); print('backed up', dst)"
```

**回退路径（任一失败即走）：**

| 故障 | 回退 |
|---|---|
| 新代码起不来（导入错/端口占用） | `git stash push`（保留 stash，不丢工作）→ `python launcher.py up` → `/api/health` 确认旧版本恢复（`build.source.disk_sha` 回到 10-22 的指纹） |
| 起来了但任务全挂 | 同上；并保留新代码 stash 供修复后再来 |
| 数据库损坏 | 用 §2 的 `db.bak` 恢复到 `task_state.DB_PATH`（先停服务） |
| 日志被污染 | 新日志隔离生效后生产日志在原 `logs/`；测试日志在系统临时目录——无需回退 |

## 3. 服务与数据根（本次重启涉及什么、不涉及什么）

| 项 | 值 | 说明 |
|---|---|---|
| 服务进程 | `python launcher.py stop` → `python launcher.py up`（16 服务：11 worker、critic、orchestrator、webui、guardian、metrics + supervisor） | 宿主机直跑，非容器 |
| Web 端口 | 8080（`WEB_PORT`） | 不改 |
| Redis | 便携 Redis（`REDIS_HOST/REDIS_PORT`） | 随 stop/up 一并停起；队列清空后重启无丢任务 |
| 数据库 | `task_state.DB_PATH`（本机 `agents.db`） | **不移动、不改位置**；只备份 |
| 工作区根 | `workspace.WORKSPACE_ROOT`（P0-A 已稳定寻址） | 不动历史任务产物 |
| 日志 | 仓库 `logs/`（生产）；测试 → 系统临时目录 | P1-E 起隔离 |
| 不动 | `config.json` / `credentials.json` / `.env` / 系统 DNS / 代理 | 红线 |

## 4. 执行步骤（授权后）

```
python launcher.py stop                # 等全部进程退出（status 确认）
python launcher.py up                  # 依赖自检 + 拉起 16 服务
python launcher.py status              # 16/16 alive
curl -s http://127.0.0.1:8080/api/health   # build.restart_required == false；started_at 更新
```

## 5. 重启后验收（按序，任何一步失败即停）

1. **实例身份**：`/api/health` → `build.instance` 与重启前一致（工作区根+端口指纹不变）、
   `started_at` 为新时刻、`source.restart_required == false`、
   `frontend.state == "built"` 且 `frontend.changed == false`。
2. **历史只读复验**：抽查 3 个历史任务（含 10-22 的SUCCESS 任务）——任务页能打开、
   报告/来源/底稿能看、交付包哈希未变（只读，不重跑、不重打包）。
3. **一轮范围内研究（唯一付费预算）**：目标"研究伊利股份（600887.SH）2023/2024
   年报收入、利润与现金流"（结构完整，不触发契约提问）。
   - 预算：检索台账默认 6 次/60s（`worker_base.search_ledger_limits`），阶段分配
     initial 4 / retry 1 / repair 1；**不调大、不重跑第二轮研究**。
   - 看点：① 来源准入——无关网页（轴承页/总局公报）不进公司证据；
     ② `workspace/<task>/search_attempts.json` 与 `search_adopted.json` 两本账；
     ③ 报告末尾来源摘要与采纳证据一致；
     ④ 若检索被网络阻断（本机 DNS fake-IP 问题未解），**必须出现如实降级披露**
     （"没有取得可核验的联网来源"）而不是编造来源；
     ⑤ 交付包 `audit/` 含 `source_exclusions.json`、`source_pending.json`、
     `search_attempts.json`、`search_adopted.json`。
4. **日志隔离**：重启期间跑一次任一 `python test_xxx.py`，确认测试日志落在
   系统临时目录 `weavemind-test-logs/`，生产 `logs/` 无新增同名文件。

> **验收执行结果（2026-10-23）**：§5 四项全部通过，实测任务 `ui-2bcfcbc06d`
> （momang 账号，SUCCESS_WITH_ISSUES，验收 fail 如实呈现）。逐项证据见
> `P1-F-e2e.md`；本轮还修复了采纳证据 note 与 pending 语义的矛盾
> （`search_evidence.py` / `orchestrator_v2.py`，22+13 测试锁定，待下次重启生效，
> build 指纹已如实标记 `restart_required=true`）。

## 6. 已知待办（不阻塞重启，如实保留）

- 本机 DNS 对外网域名返回 fake-IP（198.18.x）——在线检索仍会失败并**如实降级**；
  修复属环境治理，不在本任务边界内。
- `config.json` 的 `backup.model = "DeepSeek-V4.1-Flash"`（404）与 embedding 指向
  `tokenrhythm.studio/v1` + `deepseek-flash` ——等用户决定后再动，本任务不改生产配置。
- `orchestrator_v2.py:1027` 数据源工具发现、嵌入与聊天端点解耦 ——延后项。
