# C1 补材料入口接入编排（2026-09-27）

目标（指令 §4-C1）：让 S3 的摄取/准入**通过正常入口**进入研究，补材料后能继续原任务，
且只重做依赖该材料的环节；先把现有洋河冻结 API 材料经正常入口贯穿（它仍是 `api_chunk`，
不写成已验 PDF）。

## 1. 交付面

| 环节 | 落点 | 说明 |
|---|---|---|
| 网页入口 | `POST /api/task/<id>/material`（`web_ui.py`）、`GET /api/task/<id>/materials` | 鉴权沿用 `do_POST` 的 admin 门；**新增任务归属校验**（任务记录有 `user` 时必须同一人）；运行中的任务拒收（抓取回灌正在写快照，避免互相覆盖） |
| 页面 | `frontend/src/components/console/MaterialPanel.tsx` + 任务页第四个标签「补材料」 | 直链/上传两种输入；显示状态、来源类别、披露日**依据**、逐指标状态、查阅范围、身份 hash、待办 |
| 摄取服务 | 新增 `material_intake.py` | 保存原件（内容寻址）、幂等键、类型/体积/页数体检、直链取件、解析、调 `disclosure_ingest.ingest`、并入快照、确定性重做 |
| 准入判据 | `adapters/disclosure_ingest.py`（扩） | 新增 `VERSION="adm-2"`、`admission_rules_version()`、按**问题**挑证据小节、逐指标状态、查阅范围；披露日新增 `operator_declared` 依据 |
| 编排器 | `OrchestratorV2.handle_add_material` / `resume_pending_materials` + `main()` 循环的 `add_material` 分支 | 取件→准入→并入 `project/fetch_snapshot.json`→清洗回灌→证据/结构/底稿重做→进度与收执键 |
| 局部恢复 | 启动时扫 `material_pending_tasks`（Redis 集合） | 进程退出时停在"已保存未摄取"的材料在启动后补做；原件已落盘的**不重复发请求** |
| 本地调试通道 | `scripts/admit_material_20260927.py`（`channel=local_file`） | 与网页上传在记录里分开记；投递与网页同一条消息，摄取仍由编排器做 |

## 2. 旧反例 → 现状（逐条）

| 反例 | 现在 |
|---|---|
| 「`disclosure_ingest` 没有正常生产调用，只有测试调 helper」 | 入口是 HTTPS 路由 → 编排器处理器（生产同名函数）→ 快照 → 证据；用例从 HTTP 入口走，**不调 helper** |
| 「自动发现不可用就无路可走」 | 直链/上传两条人工通道；`discover()` 的 `next_steps` 与页面一致 |
| 「证据只截前五节，等于三问都没定位」 | 按指标关键词挑**承载小节**（第 8 节也能选中），选不中时区分"已取得但未定位"/"在列明的材料范围内未披露"，并列出已解析/未解析字符区间 |
| 「任意参数可当披露日」 | `operator_declared` 是**单独一类依据**：来源字段/URL 格式优先，自填日期只在材料自身无日期时生效，且依据如实写 `operator_declared`（不冒充 `source_field`） |
| 「规则改变沿用旧 admitted」 | 材料记录 `rules_version=adm-2/f3a-1`；对不上就重跑准入判据（用例断言重跑发生） |
| 「错主体/错误页/压缩包进得来」 | 上传也要过正文主体、错误页标记、容器魔数、3 MiB/400 页上限（400/409 明说原因） |
| 「补材料顺手改交付正文」 | 重做只做确定性部分（证据→结构→底稿）；采纳版本逐字节不变（用例断言），模型重生成只挂待办 |
| 「无变化也报待重验」 | `identity_snapshot()` 比对摄取前后的**记录身份 + 文档身份**（含来源类别/准入规则/正文 hash）：没变就不产生待办并写明原因 |

## 3. 验证

### 3.1 离线（CI 文件 `test_delivery_chain.py`，新增 13 例）

- `TestMaterialEntryChain`：HTTP 入口 → 编排器 → 原始内容 → 同一证据快照，逐环核对
  （`material_id` → `raw_sha256` → `text_sha256` → `snapshot_sha256` 与磁盘复算一致；
  定位仍是 `api_chunk`；清洗回灌收到的正文与准入正文同一份）；同一材料重复提交幂等；
  归属不符 403 且不投递、不改快照；本地通道与网页上传分开；直链取件失败**不进**资料集。
- `TestMaterialRefreshKeepsHumanText`：补材料后结构重装配并绑定当前采纳正文，**交付正文不变**；
  规则版本变化强制重审；同一身份二次刷新**不产生假待办**。
- `TestMaterialAdmissionRules`：标题洋河/正文茅台 → `subject_mismatch`；错误页；压缩包/二进制；
  逐指标三态分离；承载小节排在第 8 节仍被选中。

### 3.2 实机（本机实例，16/16 服务，新代码已装载）

洋河任务 `ui-706c5ef4a5`（SUCCESS，已有底稿/结构/研究状态）：

1. 摄取前：快照 3 篇、`snapshot_sha256=09463fe5…`、证据 8 条（全部可定位）、结构按采纳版
   `b1ea5c59…` 盖戳。
2. `scripts/admit_material_20260927.py ui-706c5ef4a5`（本地通道，投递给**真实编排器**）：
   - 准入 `user_file`（人工取得的文件），披露日 `2025-04-29`（`source_field:published_at`），
     61 个小节、8 条证据；三个指标均"材料范围内已定位到承载小节"；
     查阅范围 30051 字、已解析 2 段、未解析 2 段。
   - 快照 3 篇（**替换**旧 `re_admitted_from` 条目而不是追加），文档身份齐备：
     `material_id/raw_sha256/text_sha256/parser_version=intake-1/admission_rules=adm-2/f3a-1/
     source_class=user_file/provenance=manual_file/disclosed_at/date_precision/date_basis`。
   - 证据 `snapshot_sha256=e0e0ce71…` 与磁盘复算一致；定位样例
     `api_chunk 2 · 小节：第三节 管理层讨论与分析 > 一、报告期内公司所处行业情况（字符 2744-3025）`
     ——仍是 `api_chunk`，**不是** PDF 页码。
   - 结构按同一采纳版重盖（`b1ea5c59…`），交付正文逐字节未变；首轮报"资料集身份已变化需重生成"
     待办。
3. 同一材料再投一次（身份未变）：`refresh.stale=false`、`pending=[]`，并写明
   "资料与证据身份未变（同一份正文、同一来源类别）：本次只刷新了记录，交付正文无需重验"。

### 3.3 回归

- 逐文件跑（CI 口径）：本批触及的文件全部 OK——`test_delivery_chain` / `test_prompt_system` /
  `test_setup_wizard` / `test_auth_audit` / `test_offline_delivery` / `test_p0` /
  `test_startup_readiness` / `test_task_persistence` / `test_net_policy` /
  `test_search_quality_unified` / `test_facts` / `test_working_paper` /
  `test_narrative_evidence` / `test_frontend_guards`。
- 单进程全量拼接跑 46 个 CI 文件：**2096 例，1 例失败**（`test_setup_wizard` 的
  "占位符不算已配置"）。已定位到 §5 第 3 类（真机 `config.json` 经 `_apply_cfg_to_env()`
  写进测试进程环境），**不是本批引入**，也不在 CI 口径内复现。

## 4. 未验 / 未做

- **未验（受阻于登录）**：浏览器里**已登录**状态下点「补材料」的实测。本机 `sessions` 表为空，
  无可用登录态；不伪造会话。已用等价路径覆盖：路由已注册（未登录返回 401 而非 404）、
  HTTP 入口离线端到端（真实处理器）、实机脚本经真实编排器全链、构建产物含新面板
  （`frontend/dist/assets/TaskConsole-*.js`）。
- **未做**：第二家公司（三一重工 600031）的**真实原文**。本地无原始文件（`evals/real/` 只有洋河
  三份）；本轮一次受限检索（`web_text_search`，8 秒、单查询）候选 0，
  `pick_official_candidates` 自然为 0 → 如实留待取得，不换词硬找、不拿别家替代。
- **未验**：上传 PDF 的 400 页上限与页数统计（本批用例覆盖了容器/体积/二进制，页数上限走的是
  同一 `validate_content`，但缺真 PDF 夹具）；多进程并发补材料（同一任务同时投两份）未做。

## 5. 顺带修掉的既有跨文件污染（逐条留痕，含一次归因更正）

单进程串跑 46 个文件时才暴露（CI 逐文件跑不受影响）。核实后分三类，**不是**同一类问题：

1. **在库缺陷，本批修**：`test_delivery_chain.py` 两处借用外部实例（`TestUnsupportedClaimLinkage`）
   做夹具，只写了 `self.addCleanup(lambda: None)`，从没跑被借用实例的清理 → 工作区根与
   `task_state.DB_PATH` 泄漏到后续文件（`test_task_persistence` 的"web 与状态写者同一库"断言
   因此失败）。改为 `self.addCleanup(t.doCleanups)`。
   （先用逐用例探针定位到具体两个用例，再改；改后探针重跑无泄漏。）

2. **只在本地工作副本存在，不在库里**：`test_prompt_system.py` 带 `assume-unchanged`
   索引标志，工作副本里有一个**未提交**的测试类 `TestShippedConfigDefaults`；它的
   `_apply_cfg_to_env()` 用例只还原了配置路径与 planner 变量，没还原 `LLM_API_KEY`/
   `LLM_BASE_URL`/`LLM_MODEL`，串跑时会污染后续"配置是否填过"的判定。本批**不接管该文件的
   提交**（里面有别人的未提交工作），只把环境还原补在该工作副本里，并在文档里记名。
   CI 跑的是库里那一版（无此类），因此这个失败**不会**出现在 CI。

3. **生产行为的副作用，未修，记录在案**：`test_offline_delivery` 跑完会把**真实端点凭据**
   留在进程环境里（来源：`llm_client._apply_cfg_to_env()` 按设计把配置写进环境 + 本机存在
   真实 `config.json`）。值不记录。影响限于"同进程后续用例看到已配置"，本机才有（CI 无真实
   配置）；**建议**统一加一个进程级 `LLM_*` 还原夹具，而不是逐文件补。

因此单进程全量拼接的那 1 例失败已定位到第 3 类；CI 口径（逐文件）全绿。上一稿把三类混写成
"既有跨文件污染已修"，此处更正。

## 6. 边界遵守

不改模型/代理/权限/模板；不新增付费任务（本批无模型调用，实机只有一次脚本投递 + 编排器
本地处理）；不绕过安全门禁（上传不接受服务器路径、不执行压缩内容、保持公网校验与出口 mode）；
未清理用户既有脏文件。

## 7. 本批文件

- 新增：`material_intake.py`、`scripts/admit_material_20260927.py`、
  `frontend/src/components/console/MaterialPanel.tsx`、本文件
- 修改：`adapters/disclosure_ingest.py`、`web_ui.py`、`orchestrator_v2.py`、
  `frontend/src/components/console/ConsoleSideTabs.tsx`、`frontend/src/pages/TaskConsole.tsx`、
  `frontend/dist/*`（干净重建）、`test_delivery_chain.py`
- **未入库**（本地工作副本，见 §5-2）：`test_prompt_system.py` 里那处环境还原改动——
  该文件带 `assume-unchanged` 索引标志且含未提交的测试类，本批不接管它的提交。
