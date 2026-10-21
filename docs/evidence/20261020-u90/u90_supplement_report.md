# U90-R1→R4 补修报告（2026-10-21）

接续 `architect_review_20261020.md` 的裁决：**只补 R1–R4，不扩展新功能、不重复付费研究**。
本报告是补修的交审材料，供架构师复核；**不自行宣称"90 分达成"**。原交审报告
（`U90_交审报告_20261020.md`）与全部原下载证据保留不覆写。

## 1. 四项补修结论（逐项）

### R1 [P1] 断点复用补真校验 —— 完成

- **根因**：`orchestrator_v2._load_resumable_steps` 顶部守卫只检查"工作区目录存在"，随后仍按
  `step_id` 与历史 SUCCESS 取结果；架构师离线反例证实空目录仍复用、同 ID 换能力/指令/输入仍拿旧结果。
- **修法**（`orchestrator_v2.py`，不另建恢复引擎）：`_load_resumable_steps` 增加
  `_RESUME_STEP_ARTIFACTS`（每能力所需产物清单）＋ `_resume_step_identity_verdict`
  （同 ID 时核对 capability / instruction / 契约与输入指纹 / 所需产物，无法证明有效就不复用）；
  `_resume_artifact_verdict` 对产物缺失判不可复用；受影响后继步（depends_on 链）一并判不可复用；
  结果校验异常的 SUCCESS 不复用。恢复前经 `_restore_version_state`（L2513）恢复版本状态，
  版本丢失时按 `_project_restored_body`（L2559）如实在投影重建。
- **覆盖架构师要求的全部反例**：空目录、单个产物缺失、同 ID 换指令/能力/输入/契约、上游变更
  （后继步联动）；正例：身份与产物全同 → 真跳过。
- **测试**：`test_task26_checkpointer` 38→**55 OK**（R1 新增用例：身份不符不复用、产物缺失不复用、
  后继联动、合法复用真跳过）；与 `test_orchestrator_v2` 联跑 **156 OK**。
- **诚实边界**：真实运行的"跳过 N/M 步"仍属未验——565 个在库任务无一满足"身份+产物仍有效"
  的真实复用样本（多为 replanned / 缺 per-step contract）；证据为单元测试 + 隔离真码路径演练
  （`recovery_drill.md/.json`，pre-R1 对照 `recovery_drill_pre_r1.md/.json`）。

### R2 [P1] 当前交付全链同版 —— 完成

- **根因**：manifest 的 `report_version_id` 匹配 `store.adopted()` 只是必要条件——
  `analysis/selection.json` 的 `adopted_identity=d290ab01…` ≠ manifest `23b61bee…`（旧包）、
  洋河恢复包包内 MD ≠ 页面 MD，都是"一次导出没有一份快照"造成的分叉。
- **修法**（`delivery_pipeline.export_snapshot` L1032+ / `_freeze_payload` / `_manifest_from_frozen` /
  `repack_adopted`）：导出时把 ⓪ 正文 hash==采纳版、① 交付登记（版本/aligned/delivered_sha256）、
  ② 重渲染包含性、③ selection 身份四路证据一次冻结，清单按**写入字节**算 hash 并带
  `frozen`/`drift`/`selection_binding`；`repack_adopted` 发布前复核采纳身份未变（变了抛
  `RuntimeError("version changed")` 不静默混版）；旧包时间与标识**一律不改**。
  配套 `financial_analysis/store.py` 增 `rebind_selection_identity`（采纳绑定回滚时 selection
  同步指回）；`web_ui` 分析采纳路径（L6565/L6864）对"采纳未绑定"以 `code="adopt_not_bound"`
  回滚 selection，不让页面指向无验收的新版。
- **测试**：`test_delivery_chain` 增 `TestSelectionIdentitySameSnapshot`（同快照四路证据 +
  `rebind_selection_identity` 投影一致性）；**全套 422 OK**；恢复投影 2 项同步测试绿。
- **真码演练**（`.tmp/r2_repack_drill.py` → `r2_repack_drill.json`，临时隔离工作区）：**30/30 PASS**——
  临时副本新包 `deliverables_20261021_114831_f859f8.zip`：manifest 身份 `23b61beeb37a` == 包内
  selection == 采纳；22/22 成员哈希；PDF `bab6a95982d0`；旧 4 包字节逐一未变且页面口径如实判
  `historical`；PDF 第 1/2/3 页逐页打开目检（`r2_pdf_pages/page{1,2,3}.png`）。
- **重启后页面口径实测**（`r2_live_page_state.json`，`launcher.py restart` 后只读 in-process，
  页面/导出/清单共用读取口径）：三一 3 个旧包全部 `historical` 并带原因行
  （`包内选择记录 d290ab012e54 != 当前采纳 23b61beeb37a…`、`包内身份 b77dbdc24145 != 当前采纳…`）；
  洋河 1 个旧包同样 `historical`；**两个任务 `current=""` + `has_current=false`**，页面不再给任何
  旧包"当前包"背书（`note: 没有任何包与当前采纳稿同版（需重新导出）`）。旧包不删、仍可按精确
  URL 下载（历史版本标注，不冒充当前）。
- **诚实边界**：真机重导出未做——三一工作区已无可复算底稿（`financials.json` 缺失），当前
  `delivery_state=verified` 的正文与交付字节仍在，重导出需先补底稿（涉及真实材料/模型，超出本批）。

### R3 [P2] 恢复失败后的重试与挂起 —— 完成

- **根因**：页面（History.tsx / ConsoleSideTabs.tsx）把 resumeNote 同时当"失败说明"和"禁止再请求"
  条件，失败后必须重开页面；`lib/resume.ts` 的 POST 无超时，挂起时在途锁无有限释放。
- **修法**：前端 `lib/resume.ts` 重写为**状态层**——`ResumePhase = pending|ok|failed|timeout`、
  `ResumeResult{ok,phase,message,retryable,resumed?,taskStatus?}`；`noteFromResult`（失败保留原因但
  `retryable=true` 允许重试）、`beginResumeNote`、`applyResumeResult`、`resumeButtonState`（pending 禁用、
  failed 可点）、`fetchTaskStatus`（超时后先核任务真实状态，RUNNING/QUEUED/PENDING/QRUNNING 视为
  已接管，防重复 resume）；POST 带 12s `AbortController`。三个页面统一走状态层。
  后端 `web_ui._post_task_resume`：409/503/500/CAS 竞争失败/publish 失败的响应体统一带
  `status`+`retryable`（+`resumed`,`note`）；publish 失败回写 `task_state.mark_resume_undone`
  （CAS `RUNNING/恢复执行` → `FAILED/恢复未生效`，不改其他行）。
- **测试**：`frontend/tests/resume.test.mjs` **12 pass**（503→重试可再成功、挂起超时、双击去重、
  超时后按任务真实状态防重复接管——覆盖页面级状态层，不只测请求助手）；`test_frontend_guards` 69 OK；
  `test_task26_checkpointer` 含后端 409/503/CAS 用例。
- **真机**：`r3_resume_ui_live.json` + `r3_ui_retry_after_503.png`（真实浏览器、已登录 `momang`）——
  腿 A：注入 503 后历史页点击得到"失败+原因+可重试"，再次点击**同页**重试成功（449,096 B 截图）；
  腿 B：合成 `ui-r3-live` POST 恢复 200 → DB `RUNNING/恢复执行` → `mark_resume_undone` 回滚 →
  `FAILED/恢复未生效` 后行删除（不残留假状态行）；腿 C：自动化挂起超时/双击去重。
  **诚实边界**：腿 A 截图页面对的是合成/注入故障（真实 Redis 故障窗口未复现）；R3 的真实 UI 验证
  未重开标签页（同一页内重试）。

### R4 [P2] 来源声明与实际采用证据一致 —— 完成

- **根因**：成稿器免责声明按 `any(c.type == 'third_party')` 判"是否有外部来源"——发行人年报/公告
  引用（`issuer_annual_report`）与已准入材料不计入，于是"只引用发行人披露"被写成
  「未引用外部数据源，结论仅为模型知识」（真机反例：三一冻结结构 1 条 `issuer_annual_report`
  引用，URL `static.cninfo.com.cn/finalpage/2025-04-18/1223129214.`）；检索降级/零命中完全不进声明。
- **修法**（五文件）：`report_quality.disclaimer_clause` 增 `external_desc`/`search_degraded`——
  外部分支按**实际来源类别**措辞（发行人公开披露（年报/公告原文）/第三方平台/媒体公开信息/公开渠道），
  降级检索作为事实追加；`report_brief` 增 `_materials_state`（读 `material_intake.status`，取**准入状态**
  而非目标关键词）、`_external_desc`、`_search_degraded_reason`（`search_coverage` 对任务投影步骤，
  任一检索步 ok 即空）；来源清单登记已准入材料（URL 或 `material://<mid>`，`user_material`，
  `admission="admitted"`）；`acceptance_checker.bad_urls` 放行 `material://` 占位；
  `orchestrator_v2._REPORT_FORMAT_REQUIREMENTS` 第 6 条改写（禁止无外部来源时写"数据来源于公开披露"、
  禁止把用户材料改称模型知识）＋新基准句；`workers/report_generator_worker.STANDARD_DISCLAIMER`
  同步（兜底与编排器逐字一致）。旧验收**不迁移**：新措辞只进新装配/新采纳。
- **测试**：`test_report_quality` 45→**49 OK**（新增 `TestU90R4SourceStatement` 4 例：全来源/
  零来源/材料直供+检索降级/材料状态取准入而非目标关键词）；`test_task25_disclaimer_enforcement`
  **20 OK**（基准句三处一致）；`test_task22_report_compaction` 28、`test_r0_boundaries` 48、
  `test_delivery_chain` 422、`test_offline_delivery` 41、`test_task26_checkpointer` 55、
  `test_frontend_guards` 69 **全绿**。
- **声明层演练**（`.tmp/r4_statement_drill.py` → `r4_source_statement.json`）：**12/12 PASS**——
  A 腿（真机三一冻结结构副本）：改前「未引用外部数据源，结论仅为模型知识」→ 改后
  「数据来自发行人公开披露（年报/公告原文），可能存在延迟或误差；本次外部检索降级运行（检索步由非检索执行者代做）…」；
  B 腿（合成材料任务，`material_intake.store` 准入 + 检索零命中）：「数据来自用户提供的材料及据此进行的计算
  （材料真实性未经独立核实，计算过程可复核）；本次外部检索降级运行（检索零命中）…」；
  C 腿（零来源）：措辞不变（如实"未引用外部数据源"）；D 腿：4 个旧包哈希未变。
- **流程腿（新候选按现有链产出新包）**：`.tmp/r4_flow_package.py` **12/12 PASS**——按生产
  「人工复核修订重装」同序（`store.record → store.adopt → accept_for_body(trigger='人工修订重验') →
  assemble_and_verify → export_snapshot → repack_adopted`，`web_ui._post_task_revise` 同一代码路径）：
  A 腿新包 `deliverables_20261021_123221_c92525.zip`：`status=verified`（验收 pass）→ 新包内
  `reports/report.md` 声明 =「数据来自第三方平台/媒体公开信息…」；manifest `report_version_id` ==
  采纳身份 `c48958a74a56…`，7 成员哈希全对（`PACKAGE_MANIFEST.json` 自身不计）；绑定证据
  `same_as_adopted_body`。B 腿（零来源例）：声明正确，且因正文缺来源位置被硬门槛如实判 `draft`
  （与声明无关的独立判据，非"零来源必然草稿"）。
- **诚实边界**：①页面侧本批未见新声明——新措辞只出现在新装配交付物；页面渲染的是已采纳正文，
  旧交付按纪律不追改（改写历史需重验，涉及模型）；②真机 565 个在库任务全部 0 条已准入材料且无
  可用底稿 → 真机无法重跑成稿器产出新交付；流程腿用仓库夹具（契约/财务/正文）复现同一条代码路径；
  ③中间演练 `.tmp/r4_candidate_flow.py`（5/10）为诊断记录，已被 12/12 流程腿取代（保留作过程证据，
  不作结论）。

## 2. 代码加载身份

- 前端：R3 四文件（`resume.ts`、`History.tsx`、`ConsoleSideTabs.tsx`、`TaskConsole.tsx`）改后
  `npm run build`（2026-10-21 11:58，`frontend/dist/index.html` sha256
  `4864fe6df40b714ba2a92e0dbbd00b7426877313abf23fe3aa4a847f69bfdf6f`）。
- 后端：`inflight=0` 确认后 `python launcher.py restart`（2026-10-21 12:40:30，16/16 服务存活、
  工作台 HTTP 200 @8080；容器隔离不可用为既有环境状态——docker 缺失，未动隔离门槛）。
- 改动源文件 sha256 见 `r4_source_statement.json` 的 `code_changes`（7 文件，R4）及
  `r1_resume_validity.json`/`r1_instruction_diff.json`（R1）。

## 3. 补修后重评（原 20 项量表，待架构师复核）

**这不是审定成绩**；按裁决口径：可复核证据才记 5，失败/未证明记 0 并单列。

| 项 | 内容 | 补修后自评 | 依据 |
|---|---|---:|---|
| 15 | 有效断点可复用、过期产物不误用 | 5 | R1：反例全覆盖＋合法复用真跳过（55+156 OK，隔离演练）；**真机正例未出现，单列未验** |
| 16 | 恢复无重复副作用 | 5 | 既有（一次接管/一次出包/409）＋ R3 后端 CAS `mark_resume_undone` 回滚实测 |
| 18 | 所选版本同源一致 | 5 | R2：30/30 演练＋重启后页面口径实测（旧包全 historical、无当前包背书、旧包未动） |
| 19 | AI 声明/死链/缺口可见**且准确** | 5 | R4：声明层 12/12＋流程腿 12/12（新包内声明=实际来源）＋单元 49/20；**页面旧交付未追改，单列** |
| 其余 16 项 | 同初版自评 | 5×16 | 初版 `U90_交审报告_20261020.md` §评分（架构师尚未独立验收这 16 项） |
| **合计** | | **自评 100**（=原自评 95 中被退回的第 15/18 项与本批修复的第 19 项各记 5） | **仅作复核输入：本批只补了 4 项能力，其余 16 项沿用初版自评与初版证据；不宣称"目标达成/硬门槛全过"** |

硬门槛逐条（供复核）：无关键数字/主体/期间/口径错配（第 10 项抽样回查仍是抽样，非全量复算——如实）；
无新旧版本混包（R2 后：新包身份链同版，旧包标注 historical）✓；无越权/密钥泄漏（本批零密钥接触）✓；
无代理代签人工复核（人工复核栏本批零操作）✓；主路径无阻断（真机页面操作沿用初版证据）；
真实界面操作与恢复复验不以探针替代（R3 腿 A 为真实浏览器同页操作 ✓，但故障为注入——如实）。

## 4. 费用与调用

**本批（R1–R4）零新增付费模型调用**：全部证据来自缓存任务副本、仓库夹具与确定性代码路径；
未启动编排器任务与任何 worker。初批账本沿用 `usage_cost.json`（三一费用未测）。

## 5. 剩余未验/尚存项（如实，不豁免硬门槛）

1. 真实运行的"跳过 N/M 步"复用仍未验（无真实样本；机制经单元+隔离演练证明）。
2. 真机重导出同版新包未做（三一缺可复算底稿；需补底稿后走真实材料/模型链）。
3. 过期/旧交付包仍可按精确 URL 下载（无 staleness 头）——页面口径已如实标 historical，
   文件端点本身仍不拦（初版已列，未在本批范围）。
4. `/api/task/<id>/deliverables` 判决块 `current_*` 字段在真机返回为空（`package_body_version_id`
   有值）——页面口径以 `package_statuses` 为准（本批实测正常）；该字段形状未在本批范围。
5. `report_brief` 直接法补充资料读取 `DatasetManifest.__init__() missing 2 required positional
   arguments`（既有缺陷，本批复现 5×，未修——非 R1–R4 范围）。
6. 流程腿候选主文超字数（3857/3787 > 3600，夹具正文较长；压缩口径既有，未在本批动）。
7. 三一运行费用未测（账本过期；`config.json` 无单价表）。

## 6. 停在交审点

R1–R4 已全部完成并停此等架构师复核。未 commit / 未 push / 未新建分支；旧包、旧任务、
用户既有修改全部未动；本批无新增服务/worker/数据库/DSL。
