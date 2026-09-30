# 2026-09-30 · 带登录会话的页面真实点击（含两个实机缺陷的修复）

本批目标：关掉「带登录会话的页面真实点击」这道此前无人能验的门。**只跑定向验证**；未改用户
模板/模型/代理/权限/人审；未追加付费调用；口令只用于登录，**不写入任何文件、日志或提交**。

## 1. 为什么会一直验不了：任务页没有入口

报告与研究工作台只认 `report.taskId`，而它此前只能由「本标签页提交的任务」或 `?conv=`（会话
最后一条消息）设置——**没有会话绑定的历史任务（脚本产出、K2 复算包）在页面上根本没有入口**，
只能用接口看。实测：50 个会话对应的 51 个任务里，带分析运行的只有 `ui-603f626cbe` /
`ui-fa2cb73e59`，而它们恰好都没有会话（脚本创建）。

修法（前端，已重建 `frontend/dist`）：控制台支持深链 `/?task=<id>`（比 `?conv=` 更具体，优先），
历史页每个任务展开后加「打开研究工作台」按钮。闸门 `test_frontend_guards.py` +2。

## 2. 真实点击链路与读数

| 步骤 | 真实操作 | 读数 |
|---|---|---|
| 登录 | 表单填写 + 点「登录」 | 进入控制台；会话走 HttpOnly Cookie（`document.cookie` 不可见） |
| 反例 | 错误口令登录 | `401`「用户名或密码错误」（不暴露用户是否存在） |
| 未登录 | 页面内请求 `/api/tasks`、`/api/task/<id>/analysis`、`/api/me` | 均 `401`「未登录或登录已过期」；`/api/health` `200` |
| 打开任务 | `/?task=ui-603f626cbe`（SUCCESS，当前包 b8b234） | 报告 + 工作台渲染出「分析工作台（选运行 → 改假设 → 复算 → 对比 → 采纳 → 导出）」 |
| 选运行 | 面板模型下拉 `profit_bridge → scenario_sensitivity` | 列出声明参数与默认值/范围：`revenue_growth [-0.5, 0.5] 默认 0.05`、`gross_margin_delta [-0.3, 0.3] 默认 0.01`、`expense_change_ratio [-0.5, 0.5] 默认 0` |
| 改假设 | `revenue_growth` 输入 `0.05 → 0.10` | 输入框接受（真键盘事件，React 受控值生效） |
| 复算 | 点「确定性复算」 | 新运行生成（**尚未采纳**，旧运行保留）；前后对比表：情景归母净利润 `959.12 → 1,038.55 亿元`（+79.43），单因素敏感度 `17.41 → 17.41`（0） |
| 采纳 | `/?task=ui-fa2cb73e59` 点「采纳这版」 | `POST /analysis/adopt` → `identity_id=cc0d1c19…`、`delivery_projected=true`；面板该运行标「**进正文**」 |
| 导出 | 点「导出当前包」 | 7 秒内产出新包 `deliverables_20260930_130634_a6a57c.zip`（5,623,363 B）并标 `current`；`GET /files/ui-fa2cb73e59/<包名>` → `200`、`application/x-zip-compressed`、PK 头 |

截图（DSH 快照目录，未入库）：`C:\Users\ding0\.dsh\data\browser\snapshots\weavemind-analysis-workbench-adopted.png`。

## 3. 实机发现的缺陷一：**采纳后导不出来**（恒 409「…请重试」）

**现象**：点「采纳这版」后，面板提示「没有任何包与当前采纳稿同版（需重新导出）」，随后点
「导出当前包」恒 `409`
`{"error": "导出期间发生修订（交付正文与采纳版本不一致），未生成新包；请重试", "retry": true}`，
而**重试永远不会好**（正文不会自己变），任务自此导不出包。

**根因**：`_post_task_analysis_adopt` 按所选运行重渲染正文并落成新版本，却**没有把这一版装配出的
正文投影成交付正文**；`export_snapshot` 拿任务记录里的旧正文与采纳版本比对 → 判"不一致"。
候选采纳（`_post_task_candidate_adopt`）与人工修订（`_post_task_review_edit`）都写了
`task_state.update_delivery_projection`，分析采纳漏了这一步。

**修法**：采纳后按同一纪律投影（`report=asm["report"]`、验收摘要取采纳版本、`allow_terminal=True`
以便失败任务上「补材料后显式采纳」也能写正文，**不写状态**），并在响应里新增 `delivery_projected`；
未投影时 note 明说「交付正文未同步到本版，导出会被判不一致」。

**先复现再修**：新增用例
`test_review_edit_api.TestL0BSelectedRunEntersTheReport.test_adopt_projects_the_body_so_export_is_not_stuck`；
**不带修复**时它失败于 `delivery_projected is None`（旧行为），带修复后断言
`export_snapshot(delivered_text=任务记录正文)` 的 `report_version_id == 采纳版本身份` 通过。
真机复验：修复前恒 409 → 修复后 `delivery_projected=true` 且导出成功出包。

## 4. 实机发现的缺陷二：`launcher.py restart` 后编排器**拒绝启动**

**现象**：重启后 `launcher.py status` 显示 `15/16 services alive`、
「研究能力：未就绪——编排器未存活」；编排器日志：
「拒绝启动：已有编排器实例 inst-… 在运行（租约 30s 内）；同一 Redis 上不允许两个编排器
消费同一条任务通道」，退出码 2。**再重启一次才恢复**——一键重启等于不可靠。

**根因**：单实例归属租约（服务端 TTL 30s）在旧进程被杀后仍然有效，新进程一认领失败就退出。

**修法**：新增 `claim_ownership_with_wait()`——**有界等待**让位（上限 `OWNER_HB_TTL + 6s`，
可用 `WM_OWNER_WAIT_SECONDS` 覆盖），等满仍拒绝启动。等待不削弱单实例保证：活着的持有者会持续
续租，等多久都不会让位。用例钉住两条（等到过期后接管成功 / 活实例仍拒绝）。

**真机复验**：单次 `python launcher.py restart` → `16/16 services alive`、
`[OK] 研究能力：就绪（Redis 8、编排器与 5 项必需 Worker 心跳新鲜）`。

## 5. K2 两包：复算仍成立，但**其中一包的「当前」指向变了**（如实记）

- 两包复算**仍是 7/7**：`ui-603f626cbe` 与 `ui-fa2cb73e59` 各 7 条运行全部 `ok`，
  `mismatches=[]`（值/单位/期间/输入一致），各 1 条 `label_changes`（情景显示文本按参数生成，
  L0-c 已记）；`dataset_hash` 仍 `f34114d9…`。证据 `k2_audit_all.json`（本批重跑）。
- **但**：点击链在 `ui-fa2cb73e59` 上点了「采纳这版」，采纳稿因此推进了一版 →
  原 K2 包 `deliverables_20260930_004010_092fdb.zip`（identity `e5ae7ff6…`）状态变为
  `historical`（**字节未变、仍在盘上、仍可复算**），新当前包是
  `deliverables_20260930_130634_a6a57c.zip`（identity `cc0d1c19…`）。
  `ui-603f626cbe` 的当前包未变（`deliverables_20260930_003858_b8b234.zip`，identity `f585edbf…`）。
- 若要把旧稿恢复成当前，需要人工回退采纳版本（未做，等指令）。

## 6. 本批定向验证读数

`test_review_edit_api` 28 全过（+1）、`test_startup_readiness` 78 全过（+2）、
`test_orchestrator_v2` 86 全过、`test_financial_analysis` 119 全过（+4，Q4 预测闸门）、
`test_frontend_guards` 全过（+2）、前端 `tsc --noEmit` 0 错误、`npm run build` 通过、
node 行为测试 60/60、`check_secrets` 干净。

## 7. 仍未解决（不假装已解决）

- 真人 F3 五项 ≥8/10 与严重问题否决、复核耗时：**必须真人**；
- 干净 Windows 一键启动与完整交付验收（`clean-env-e2e` 是 ubuntu + 固定模型替身，不可替代）；
- K1 付费 API 端到端实网整跑：已获批准但**额度上限未给**，仍未跑；
- Q4 统计预测：**未开放**（门槛与禁用清单见 `docs/统计预测门槛与禁用清单_20260930.md`）；
- PDF 全页视觉、当前 HEAD 的远端 CI 重查。
