# C 批（候选闭环）执行证据 · 2026-09-28 下午

基线：`b85e63e`（A+B 批）。本批只做 **C（候选闭环 ①..⑦）**，D 未做。
约束遵守：不新增付费整跑（本轮页面动作全是确定性装配/验收，未调用模型；无检索、无生成）、
不改阈值/门禁/模型/权限/代理/模板、不清理真库、不删日志与产物。

---

## 一、修前复现（全部在隔离环境复现，不碰真库）

脚本：`%TEMP%\wm_c_repro.py`（复用 `test_delivery_chain` 的装置与生产未绑定函数）、
`%TEMP%\wm_c6_repro.py`。

| 编号 | 反例 | 复现读数（修前） |
|---|---|---|
| **C-1a** | `FAILED` + 材料齐备 + 无活跃运行 → 生成候选 | `409 / status=None / error=任务已终态，不得再生成候选正文`；**不投递** |
| **C-1b** | `task_state.read_task` 抛错 | 入口 `except` 吞掉 → `owner=""`、状态为空 → **校验全部跳过，照投** `regenerate_candidate` |
| **C-1c** | `is_running` 抛错 | 同上（`except: pass` 注释写着"读不到状态就不挡"）→ 照投 |
| **C-2** | 生成候选后读 `candidate_identity_id` | `'<bound method ReportVersion.identity_id of ReportVersion(version_id='939a…', body="# 洋河股份 经营情况简报（2023…'`：**不是身份、长度≠64、夹带交付正文** |
| **C-3a** | 有效规则 `r1/rf1` → `r9/rf9` 后再生成 | `created=False`、`key` 不变（`9449c3f5d34f162d`）、候选版本没变 → **stale 复用** |
| **C-3b** | 采纳版换证据快照（正文相同 → `version_id` 相同、`identity` 不同） | `created=False`、`key` 相同 |
| **C-3c** | 在已并入材料**前面**多插一份已并入材料 | `created=False`、`key` 相同（旧键只取 `merged[-1]`） |
| **C-4** | 编排器不回执（超时） | `503 pending`，文案写死"**没有生成任何候选**（不假成功）"（对未知结果下结论）；无 `operation_id`、无查询入口、ack 键每次随机 |
| **C-6** | 任务失败在"缺原始披露（located=0）"（真实任务 `ui-29e8ca73b5` 形状）+ 健康列表首条是 `code_sandbox` 不可用 | `action=open_health`、`message=sandbox/code_sandbox 不可用…` → **让用户去装 Docker** |
| **C-7** | 冻结样本『财务对照』表头 `指标 \| 2023 \| 2024 \| 口径 \| 来源` | 分享页裸取"第 0/1 列" → 置顶卡片是 **2023**（较旧那期）且**不带年份**；而且分享页选的是**第一张表**（实物量/吨） |

---

## 二、修后读数（同一脚本、同一断言）

| 编号 | 修后 |
|---|---|
| C-1 | FAILED → `200 candidate_ready`、`created=True`；`prior_failure={status:FAILED, detail:…**研究状态：研究草稿／待补原始披露**…located=0, audit_preserved:True}`；任务行未被改写 |
| C-1 | `CANCELLED` → `409 cancelled`；`RUNNING` → `409 task_running`；读失败 → `503 state_unreadable`（**三者都不投递**） |
| C-2 | `candidate_identity_id = 1d9c035645782124…`（64 位 hex）；`test` 断言 `bound method`/`ReportVersion`/正文标记均不出现 |
| C-3 | 规则变化 / 采纳版换证据快照 / 材料集合变化 三种情况**都重建**（`created=True`，`key` 改变）；`basis={materials_merged:1, contract_fingerprint, adopted_identity_id, rules:{version,fingerprint}, key_sha256_32}` |
| C-4 | `503 pending` + `operation_id` + `query=/api/task/<id>/candidate?operation_id=…` + `result_unknown:true`；文案改为"**本次结果未知**（可能仍在装配，也可能已失败）"；投递用的 ack 键 `candidate_ack:<tid>:<op>`；带同一 `request_id` 重试 → **同一个 operation** |
| C-4 | 新增 `GET /api/task/<id>/candidate[?operation_id=]`（只读）：有回执 200；查不到 404 `unknown` 且**不说"没生成"**；监听器额外写 `candidate_ack:<tid>:latest` 稳定指针 |
| C-6 | `action=add_material`、`message=任务失败在**缺原始披露**上（不是环境问题）…`；健康原因仍如实列出（`causes` 里第一条仍是 `code_sandbox`）——**只是不再拿它当这次失败的原因**；沙箱只在任务**真的含** `code_execution` 步骤时才算原因 |
| C-7 | 分享页：`source=metrics`、`periods=['2024年','2023年']`、`unit=亿元`；卡片键 `营业收入 · 2024年` → `288.76亿元`（最新期在前，期间写进标题）；无『财务对照』表时退回首表并**如实标注**"未判定为结论"（退回首表时同样带期间标签） |

新增定向用例：
- `test_delivery_chain.TestCandidateEntryChain` **+9 条**（C-1 两条、C-2 一条、C-3 一条、C-4 两条、C-5 三条）；
- `test_actionable_state` **+4 条**（失败原因来自任务自身事实、沙箱只在本任务含代码步骤时算原因、模型未配置优先于健康列表、CANCELLED 与失败分开说）；
- `test_frontend_guards.TestReportPageWiring` **+1 条**（结论卡必须带期间/单位口径，不得裸取首数值列）。

按文件跑（本机约定）：
`test_delivery_chain` **397 OK**、`test_frontend_guards`+`test_actionable_state` **89 OK**、
`test_task_projection` 19 OK、`test_offline_delivery` 34 OK、
`test_task_persistence` 51 OK、`test_startup_readiness` 65 OK（A/B 批后未变）。

---

## 三、页面级闭环验收（C-5 退出条件：**不能只用 Python handler 当页面通过**）

### 夹具（真库**只新增**，可核可撤）

- 副本 task_id：**`ui-c5loop-1`**（goal 前缀 `【C-5 闭环副本 ui-c5loop-1】`，便于在列表里识别）
- 来源：真实 FAILED 任务 `ui-29e8ca73b5` 的工作区整份复制（含 1 份**已准入并入**材料
  `f42e747c73850b59`：证据 8 条、正文 177582 字、披露日 2025-04-29；另 1 份被拒材料
  `0e448b6e558cbb95`，原因"PDF 无可提取文本（扫描件或受保护）"）
- 工作区：`%TEMP%\agent_workspace\tasks\projects\default\ui-c5loop-1`
- 数据库：`task_history` 新增 1 行（列全部照抄原行，只改 `task_id`/`updated_at`，清空幂等键；
  `status` 保持 `FAILED` → 终态，永远不会被恢复扫描捡起、不会被执行）
- **未修改/未删除任何既有记录**；撤销方式：删该行 + 删该工作区目录

### 页面动作与证据（截图在 `C:\Users\ding0\.dsh\data\browser\snapshots\`）

| 步骤 | 页面读数（截图） |
|---|---|
| 打开副本（`/?conv=conv-03af7e1463`） | 顶部可行动提示：`[明确失败] 任务失败在**缺原始披露**上（不是环境问题）：补上原始材料后会按同一快照重做，然后可生成候选正文` + 按钮 `去补材料` ——**没有指向 Docker**（`shot-1790592641697-4bb7a759.png`、`shot-1790592056727-bf914465.png`） |
| 补材料 → 生成候选正文 | `候选 86216dc44d43… · 本候选验收：fail · 未采纳（需显式采纳）`；`影响步骤 report_generator；未重做 0 个`；`候选缺口：主体归属校验 456 处上下文，污染 3 处（占当期营业收入 = 10.0% 属 网络源证据指向其他公司）；归属模糊 429 处`；`原任务状态：FAILED（完成）——…` + `原失败记录未被修改；候选是新版本，不代表任务已成功。`（`c5_01_candidate_generated.png`） |
| 预览/比较候选 | `候选预览（只读）：身份 eb8b60fb54efc0b4… 未采纳`；`与当前稿比较：改动 117 行（+90/-27）；字节 18122 → 29552；人工分析节 原样保留`；证据缺口同上；下面给出统一 diff 与 `查看候选正文`（`c5_02_preview_panel.png` 为采纳后同一面板：`身份 …已是当前选中版本`、`改动 0 行`） |
| 采纳（两步确认） | 第一步只"上膛"：`确认采纳这一版作为交付正文？… 身份 eb8b60fb54efc0b43e0f… · 与当前稿改动 117 行 · 候选验收 fail` + `旧版与人工文字保留在版本库；人工复核（human_review）不继承、不改写；采纳**不等于**验收通过`；第二步按钮 `确认采纳（不可自动完成）` |
| 采纳结果 | `已采纳 86216dc44d43…（此前选中 5ff915ae06a0…）`；`对该身份重验：fail；缺口：主体归属校验…`；`人工复核文件未被改动；旧批准未继承；采纳本身不等于验证通过`；`下一步：POST /api/task/ui-c5loop-1/package`（`c5_03_adopted.png`、`c5_04_after_adopt_and_export_attempt.png`） |
| 关键数据速览（同一页） | `取自报告正文『财务对照』表（指标 × 期间：2024年、2023年 × 口径 × 来源）；表内单位 亿元`；卡片按最新期在前：`营业收入 · 2024年 288.76亿元`、`营业收入 · 2023年 331.26亿元`…（C-7 页面证据） |

### 身份与 hash（磁盘核对，非页面自述）

```
采纳版 version_id = 86216dc44d43756fe7a282dc55f47506677da1cd25dcba956560864af100406f
采纳版 identity   = eb8b60fb54efc0b43e0f2cee38342c6003107d66c5fd7895b1942b6d4e87f578
本版验收 overall  = fail   | 绑到本版（acceptance_for_this_body）= True
                    report_sha256 = 86216dc44d43756f…（== version_id，**重验落在该身份上**）
human_review.json = 不存在（未被创建/改写）
candidate_state   = {key:43456cb0358e78f5, candidate_identity_id:eb8b60fb54efc0b4…, created:True,
                     prior_failure.status:FAILED,
                     basis:{materials_merged:1, contract_fingerprint:538668e8…,
                            adopted_identity_id:403757da…, rules:{2026.09.12, 2ddecc9a}}}
```

`verify_body_as_is=True` 的效果（对照修前）：采纳版正文 `e3d4d0a8bcbd…`（真实任务）经
`research_candidate_body` 重装配后变成 `429801667de0…`（"毛利额…：20920103385→24728464886"
被改写成"上期 247.28 亿元 → 本期 209.20 亿元（-38.08 亿元）"）——即**旧路径验收的不是要证明的那一版**。
候选生成与采纳后重验两条路径现在都按"正文就是验收对象"跑。

---

## 四、本批仍未过 / 新发现（**如实列出**）

1. **导出这一腿未过（C-5 的最后一环）**。在 FAILED 副本上点「按当前版本重新导出」/直接调
   `POST /api/task/<id>/package` → `409 {"error":"导出期间发生修订（交付正文与采纳版本不一致），未生成新包；请重试"}`。
   实测量化：采纳正文 13309 字（`86216dc4…`），而导出读到的交付正文 8038 字（`bf1a2993…`）——
   因为 `assemble_and_verify` 对研究任务会**再装配一次简报**（`build_structure`+`render_brief_markdown`），
   于是"交付正文"与"采纳版正文"天然不是同一份字节，`export_snapshot` 的版本一致性守卫因此拒绝。
   已做的两处最小修复（打包闸门按 CANCELLED/运行中/FAILED 分类；采纳后同步交付投影
   `assemble_and_verify` + `update_delivery_projection`）**不足以**解决它——这需要一个明确的装配语义决定：
   要么把"交付外壳（wrapper+body）"也登记成一个版本并采纳该身份，要么让 `export_snapshot`
   在**交付登记里 `accepted_body` 就是采纳版**时放行。**本轮不做**（会动到交付身份语义，需单独一批 + 反例）。
2. **`33.43亿元` 仍判"不可溯源"**：采纳后的 数字可信度 为 **99%（202/204）**、`计算 40`、不可溯源 2 条
   （`33.43亿元`、`378,003`）。同一族派生金额里 `-38.01亿元`（毛利润变化）、`+4.58亿元`（毛利线以下）、
   `69.37%`/`61.2%`（覆盖）都已进计算通道，**唯独 Δ归母净利润 33.43 没有**——同族不同命，需要单独定位
   （B 批的派生匹配对"关键判断行"与"附录金额变化行"的处理可能不同）。
3. **预览面板文案竞态**：采纳成功后同一次刷新里，预览卡片仍显示"未采纳"（`is_adopted` 取到的是切换前的值），
   而比较结果已按新版计算（改动 0 行）。下次预览即正确。属显示竞态，不影响状态与身份。
4. **未验**：跨进程真实并发、真实崩溃恢复、真实 Redis 上的租约转移（环境不具备）；
   第二家公司真实原文；`clean_env_verified`；远端 CI；真人 F3 五项。
5. **D（期间口径 + 真实截止中断）整批未做**。

---

## 五、改动清单（按文件）

| 文件 | 改动 |
|---|---|
| `orchestrator_v2.py` | 新增 `_material_fingerprint()` / `_version_identity_fields()`（**调用** `identity_id()` 而不是 `str()` 方法对象）/ `_prior_failure()`（跳过标题、优先取失败语义词那一行）；幂等键改为**全集依据**（全部已并入材料 + 契约 + 采纳版 identity + 有效规则），并把 `basis` 回给页面；候选验收改 `verify_body_as_is=True`（结论必须是对这份候选的）；监听器补写 `candidate_ack:<tid>:latest` |
| `web_ui.py` | 候选入口闸门重做（严格读 `read_task_checked`；CANCELLED/RUNNING 分开；读失败 503 不投递；FAILED 放行）；稳定 `operation_id` + 诚实 pending 文案 + `GET /api/task/<id>/candidate`；新增 `GET …/candidate/preview`（正文 + 结构化比较 + 缺口，只读）与 `POST …/candidate/adopt`（`confirm:true`、按身份精确采纳、重验绑定该身份、人工复核不改写、旧批准不继承）；打包闸门按 CANCELLED/运行中/FAILED 分类；分享页选表与期间/单位（`_period_of_header`/`_parse_md_tables`/`_report_unit_of`/`_pick_period_table`/`_top_stats_of_table`） |
| `actionable_state.py` | 失败行动改由**任务自身持久化的失败事实**决定（phase/report/失败步骤 error），健康原因降为兜底且**只认本任务真的用到的依赖**；CANCELLED 与 FAILED 分开说 |
| `delivery_pipeline.py` | `accept_for_body(..., verify_body_as_is=False)`：True 时**不重写**验收对象（"要证明的就是这一个身份"） |
| `frontend/src/components/ReportViewer.tsx` | 结论卡带期间与单位；最新期间在前；`periodOfHeader`/`reportUnitOf`/`topStatsOfTable`；分享口径同步 |
| `frontend/src/components/console/MaterialPanel.tsx` | 新增 `预览/比较候选`、`查回上次操作`、两步确认的 `采纳这一版…`；展示候选身份/缺口/比较/diff/正文，与采纳后的重验结论、`human_review` 是否被动过 |
| `frontend/src/pages/TaskConsole.tsx` | 从会话打开**已结束**任务时绑定 `currentTaskId`（此前从不设置 → 可行动提示与补材料面板拿不到任务）；可行动取数去掉 `revision` 依赖并改为"只认最后一次请求" |
| 测试 | `test_delivery_chain` +9、`test_actionable_state` +4、`test_frontend_guards` +1 |

## 六、页面层顺带修掉的两处（截图看到、单测抓不到）

1. 可行动提示**在页面上根本不显示**：从会话打开已结束任务时 `currentTaskId` 从不设置，
   `/api/task/<id>/actionable` 接口明明返回 `{state:failed, action:add_material}`，页面却拿不到——
   这是"缺原始材料却指向无关 Docker"之外的第二层缺陷（接口对了、页面没显示）。
2. 采纳用 `window.confirm` 既让用户"盲确认"，也让自动化验收点不到（脚本环境 confirm 默认被拒）——
   改成面板内两步确认（第一次上膛、第二次 `确认采纳（不可自动完成）`），身份/改动行数/验收结论都在按钮上方。
