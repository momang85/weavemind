# 新用户真实闭环报告（2026-10-21）

工单：新用户闭环（全新工单）。本报告只引用历史批次结论，不自评达标分数。
所有页面操作均来自真实浏览器（Kimi WebBridge，会话 `zhiguang-newuser`），登录账号 `u90new21`（本工单新建，页内提为管理员）；密码仅存会话内存，未写入任何文件/日志/证据。

## 0. 事件披露（必须先读）
**本工单早前发生过一次自伤性事故并已清理**：在验证服务存活时误跑 `python launcher.py up`，因该会话进程探活被拒，launcher 误判「0/16 服务存活」而拉起了**第二套完整栈**（webui 溢出 8081、pids.json 被改写、supervisor 崩溃重启循环、guardian/critic/metrics 重复）。清理过程 `launcher.stop_services()` 杀不掉任何进程（`taskkill` 全部 `ERROR: Access denied`）且**删除了 `.weavemind/pids.json`**；最终用 `Stop-Process -Id <pid> -Force` 逐个结束第二栈全部本会话子进程。**原栈未被波及**：编排器租约拒绝所有重复实例（日志 14:23:54–14:24:48），8080 webui 正常，Redis 存活，11 个智能体心跳连续。遗留副作用：`.weavemind/pids.json` 丢失（守护按设计自行退出）、`runtime_ports.json` 被改写为 `{redis:6379, web:8081, preferred:8080}`。此事故不影响本报告数据，但如实记录。

## 1. 新用户开局（A 项）
| 步骤 | 页面状态（逐字/截图） |
|---|---|
| 1 | `momang` 登录进入任务控制台（截图 01） |
| 2 | 设置页新建用户 `u90new21`（截图 02） |
| 3 | 退出 → 登录页（截图 03/07） |
| 4 | `u90new21` 首登为**只读 viewer**：控制台提交被挡（viewer 无提交权限，截图 04/04b/05） |
| 5 | 页内角色入口（设置→用户管理）把 `u90new21` 提为**管理员**（截图 06/06b）——这是新用户必须知道的内部步骤（blocker：新账号默认 viewer，无法提交任务） |
| 6 | 控制台提交表单填写：公司 洋河股份 / 代码 `002304.SZ`（按输入记录）/ A股 / 2023–2024 / 合并报表 / 投研视角 / 资料截至 2025-04-30 / 参考资料留空 / 先确认计划未勾选；目标文本自动生成进 textarea（截图 08） |
| 7 | 提交成功，任务 `ui-d59152ebb9` 进入 RUNNING（截图 09，06:41:57 UTC） |
| 8 | 运行至终态 `SUCCESS_WITH_ISSUES`（06:56:20 UTC，截图 10）；活动日志：「检索缺口（无候选 URL）按资料缺口处理，不判任务失败」「能力-执行者错配：web_search 步骤 1 由 content_summarizer 执行（重规划代做）；检索质量不可信，任务按有缺口交付」 |

## 2. B 全链（新任务 `ui-d59152ebb9`，真实页面操作）
| # | 步骤 | 页面可见状态（逐字） | 证据 |
|---|---|---|---|
| 1 | 材料准入 | 自动官方发现命中：`official_discovery.json` status=admitted；准入 1 份材料 `f42e747c73850b59`「2024年年度报告」（URL `http://static.cninfo.com.cn/finalpage/2025-04-29/1223370519.PDF`，`raw_sha256=436928c6…79a2`，4,699,879 字节，status=**admitted**，provenance=官方公告发现（自动）；raw.bin 复算哈希一致）。页面头部来源行：「来源：正文引用 **2** 条（编号见『参考来源』）；另有 0 条候选材料未采用（不编号、不进正文，只留在内部审计）。」 | 截图 08/09、`per-item/r4_declaration_vs_sources.json` |
| 2 | 确定性复算 | 工作台按钮「确定性复算」点击 → 新运行生成：页面文案「**复算完成：这是一条\*\*新运行\*\*，尚未采纳（旧运行仍保留）**」 | 截图 11→12、`per-item/workbench_before_recompute.txt` |
| 3 | 生成候选 | 新运行并入运行菜单（前端 `AnalysisWorkbenchPanel`：recompute 返回 `run_id` 并选中），运行芯片「519c5db160f8｜validated」→ 复算后出现新运行（/analysis runs 列表 8 条 validated） | `final_page_state.json`、API 快照 |
| 4 | 预览比较 | 页面表格（逐字）：「前后对比（新运行 vs 所选基准运行）：归母净利润变化 -3,342,541,438.15元 → 同（0元）；毛利变化 -3,800,953,659.19元 → 同（0元）；毛利线以下净额变化（未解释段）458,412,221.04元 → 同（0元）」 | `per-item/recompute_compare_verbatim.txt`、截图 12 |
| 5a | 采纳（第一次，**失败**） | 「采纳未成立：采纳正文里找不到所选运行 519c5db160f8（选了新运行，正文却还是旧稿）。已回滚选择并保留旧的有效稿」——服务端 409 `adopt_not_bound`，selection 回滚为空 | `per-item/adopt_fail_then_retry.txt` |
| 5b | 采纳（第二次，成功） | 换选正文绑定运行 `f478645b6722`（芯片「f478645b6722｜validated｜**进正文**｜2026-10-21T06:43:09」）→「**已采纳：报告身份 9751ef20b489；可导出当前包**」；`analysis/selection.json.adopted_identity=9751ef20b489…`，entries=1，selected_at=2026-10-21T15:19:45 | 截图 13 |
| 6 | 导出当前包 | 「导出当前包」→ 页面：「**当前包：deliverables_20261021_152053_9ab4b5.zip（与采纳稿同版才可下载）**」；`current_package_ok=true`、`package_identity_ok=true`；期间 llm_calls 维持 38（打包零 LLM 调用） | 截图 17、`final_page_state.json` |
| 7 | 页面下载 MD/PDF/ZIP | 页面按钮「下载Markdown」「下载PDF」各点击一次（`a.download=<tid>.md/.pdf`）；包经「导出当前包」→ `/files/ui-d59152ebb9/deliverables_20261021_152053_9ab4b5.zip`。浏览器落盘位置在本会话不可观测（沙箱），以**服务字节哈希 + 字节副本**留证：md `034112ed…59bc`（== manifest.delivered_md_sha256）、pdf `83a93ec4…9931`（即时重生成版，见 §3 注）、zip `62d0c6fb…2d53`（== 磁盘包） | `downloads/` 三件套 |

## 3. R2 同版核验（全部通过）
采纳身份 = `9751ef20b489546be76da463f86fdad0e151a6fb04ffeb2d89a40d6deea2bb13`
| 处 | 值 | 一致 |
|---|---|---|
| `PACKAGE_MANIFEST.json.report_version_id`（包内） | `9751ef20b489…bb13` | ✓ |
| 采纳身份（`/analysis adopted_identity` = 页面「已采纳：报告身份 9751ef20b489」） | `9751ef20b489…bb13` | ✓ |
| `analysis/selection.json.adopted_identity`（工作区 + 包内副本，entries=1，selection_binding: present=true, changed=false, consistent=true, verified_by=already_bound） | `9751ef20b489…bb13` | ✓ |
| 页面「当前包」指向 | `deliverables_20261021_152053_9ab4b5.zip`（current，identity=9751ef20b489…）；两个旧包 `145534`/`151250` 均 **historical**（identity 885673ec…/ea96713e…） | ✓ 非历史包 |

**包内 25 个冻结成员逐一 sha256 复算：全部匹配**（`per-item/r2_same_version_check.txt`，`ALL_HASHES_MATCH: True`）。

**PDF 实开检查**：通过 `http://localhost:8080/files/ui-d59152ebb9/reports/report.pdf` 在 Chrome 内建查看器真实打开（`embed[type=application/pdf]`），逐页导航截图 3 页（哈希互异）：`14_pdf_open_page1.png`、`15_pdf_open_page2.png`、`16_pdf_open_page3.png`；共 7 页，第 1–3 页文本与正文一致（`per-item/pdf_text_check.json`）。
注（如实记录的实机观察）：页面「下载PDF」服务的是**即时重生成**的 PDF（sha `83a93ec4…`，5,526,922 字节），与包内成员 `reports/report.pdf`（`35c66f80…`，5,517,433 字节）字节不同——响应头 `X-Report-Version-Id: 9751ef20b489…`（=采纳身份）、`X-Report-Draft: 0` 证明同版；差异来自按需生成嵌入时间戳（web_ui L4921 起：导出时清单按同一正文与 PDF 字节重写）。包内成员哈希仍以 manifest 冻结值为准，两者均绑定同一采纳版本。

## 4. R4 声明核验（通过）
交付正文声明（逐字，来源 = 实际采纳版本 reports/report.md）：
> 免责声明：本报告由织光 WeaveMind AI 自动生成，仅供参考，不构成任何投资建议；数据同时来自用户提供的材料（其真实性未经独立核实）与发行人公开披露（年报/公告原文）与第三方平台/媒体公开信息（可能存在延迟或误差）；**本次外部检索降级运行（检索步由非检索执行者代做），报告来源以实际采用的来源清单为准**，公开信息可能不完整；据此操作风险自担。
> 数据来源摘要：检索步总数 1｜有效覆盖率 0%｜降级执行步 1（由非检索模型完成，来源可能为空）｜零来源步 0｜❌ 检索质量严重不足，报告结论需谨慎参考

实际来源清单（并排）：
| # | 来源 | 类型 | 与准入状态对应 |
|---|---|---|---|
| 1 | 东方财富数据中心（A股）`datacenter-web.eastmoney.com/api/data/v1/get?...SECURITY_CODE="002304"` | 第三方数据/媒体（结构化财务数据，用于财务事实、正文引用） | 检索步 0 有效（降级）下仍获结构化数据源 |
| 2 | 2024年年度报告 `static.cninfo.com.cn/finalpage/2025-04-29/1223370519.PDF` | 发行人年报/官方披露（业务背景、变化解释、财务附注、风险因素） | 材料准入 admitted（官方公告发现，raw_sha256 复算一致） |

- 三分法成立：**检索降级**（web_search 0 候选 → 步骤 2/2b/2c gate_skipped，不计失败）、**材料直供**（官方年报 admitted → 来源 [2]）、**无来源**（报告未虚构来源；缺口如实标注）。
- 「搜索 0 结果」≠「未引用材料」：搜索步 0 有效结果，但正文引用 2 条实际来源（结构化数据源 + 已准入官方原件），声明与来源一一对应。
- 证据：`per-item/r4_declaration_vs_sources.json`（声明逐字 + materials/index.json + official_discovery.json + meta 摘要）。

## 5. R1 断点复用正例 —— 未验 + 原因
本批唯一新任务 `ui-d59152ebb9` 全部步骤跑完（终态 SUCCESS_WITH_ISSUES，恢复入口不可用），**未发生 FAILED/CRASHED，故无页面恢复入口、无「继续执行（跳过 N/M 步）」可测**。制造真实失败的尝试与不可行原因（逐字级）：
1. **进程注入不可行**：本会话令牌无法观察或终止原栈进程——`Stop-Process -Id 5160` →「找不到进程标识符为 5160 的进程」；`taskkill /F /PID 5160`、`taskkill /F /T /PID 5160` →「ERROR: Access denied」；而 netstat（8080 LISTENING pid 37880）+ Redis 租约 `orchestrator:owner=inst-c7b0d3e07c:5160:5fffc833e75a`（TTL 持续刷新）证明编排器存活。10-18 批次的「杀便携 Redis → 崩溃兜底翻转」机制因此无法复现；杀共享 Redis 会同时打断 webui/workers 且本会话无安全重启路径（`launcher.py up` 已实证会拉起第二套栈——见 §0 事故），属「破坏环境」，按工单口径不得采用。
2. **页面无中断入口**：编排器/web_ui 无 PAUSED/pause 语义（Pause 仅停活动滚动）；`POST /api/task/<id>/cancel` → CANCELLED（明确无恢复入口）。
3. **缺口吸收是设计**：检索/抓取缺口走 `gate_skipped`+`optional_ids`（「按资料缺口处理，不判任务失败」），用户侧无法让必需步骤失败。
4. **先确认计划超时**：会产生 FAILED 但完成步数 0（M=0，不构成正例），且消耗一次真实运行。
5. **预算**：授权真实付费运行 ≤2 次（含续跑）；本批已用 1 次（`ui-d59152ebb9` 提交）。「制造失败 + 续跑」需 2 次，剩余 1 次不足。
6. **为何不能用桩/探针替代**：桩不经过 `_load_resumable_steps` 的身份+工件三重校验，也产生不了 `web_ui._resume_view` 的真实页面标签；伪造 M 值违反「不许伪造数字」。
页面侧真实观察到的守卫（非正例，如实记录）：`/task` API `resume.available=false`，reason=「任务已完成：结论是不可重跑的终态；要修正内容走报告页的修订/复核入口，或补材料后在分析工作台复算。」（`final_page_state.json`）。

## 6. R3 失败重试与挂起（恢复路径）—— 未验 + 原因；采纳流实机类比已记录
**恢复请求四项（①同页重试成功 ②挂起有界收敛 ③双击去重 ④按任务真实状态防重复接管）均为 resume.ts 专层，恢复入口不存在（无 FAILED 任务）→ 未验**，原因同 §5（无失败可复用；杀共享 Redis 属破坏环境；预算不足）。
**不冒充 R3、但同页族的真实实机观察**（`per-item/adopt_fail_then_retry.txt`）：
- ①类比：第 1 次「采纳这版」失败（页面逐字「采纳未成立：…已回滚选择并保留旧的有效稿」，服务端 409 `adopt_not_bound`）→ 同页第 2 次修正后重试成功（「已采纳：报告身份 9751ef20b489；可导出当前包」）。
- ④类比：失败后服务端按真实状态回滚（restore_selection + 重新采纳旧有效稿 ea96713e…），「导出当前包」在回滚态重新生成 145534→151250→152053 三个包并正确标注 historical/current，未出现错版接管。
- ②③未测：本次未对采纳/导出做挂起注入与双击去重实验（避免引入不必要的状态扰动），如实记未测。

## 7. 旧包与历史任务前后指纹比对（通过）
快照：`fingerprint_before.json`（2026-10-21 13:32:13，zip 17 / pdf 34 / 任务行 565）→ `fingerprint_after.json`（15:29:36，zip 18 / pdf 35 / 任务行 566）。
| 对象 | 前后 |
|---|---|
| 17 个旧 zip（含受保护清单 ui-cc853cf86b ×3、ui-91677c7767 ×1 等全部工作区 zip） | **逐文件 sha256 全部一致**（diff：new=0, gone=0, changed=0） |
| 34 个旧 PDF（含 docs/evidence 全部） | **逐文件 sha256 全部一致** |
| 新增文件 | 仅本批证据副本 2 个：`downloads/ui-d59152ebb9.zip`、`downloads/ui-d59152ebb9.pdf` |
| task_history | 565→566 行：+1 行即本批新任务 `ui-d59152ebb9`（updated_at 07:20:09 UTC）；其余全部行 updated_at ≤ 04:39:10 UTC，早于前快照 13:32:13（本地）——历史任务行零触碰 |
| 新任务自身工作区 | 位于 `…\dsh-win-bash-37684-38250d7e\agent_workspace\tasks\projects\default\ui-d59152ebb9\`（前快照不存在该目录，无「前」可比；其 3 个 zip 中 2 个已按包清单标注 historical） |
详见 `per-item/fingerprint_diff.json`。受保护旧件字节与时间戳均未改写。

## 8. 费用与调用数（如实）
| 项 | 值 |
|---|---|
| LLM 调用 | **38 次**（failed 9，主要为 thinking_budget_exhausted 重试），事件明细含端点/耗时/字符数（`final_page_state.json` / `api_task_ui-d59152ebb9.json`） |
| 费用 | **未测**——`config.json` 无价格表，无计费接口 |
| 授权真实运行 | **1/2 次**：仅 `ui-d59152ebb9` 提交（2026-10-21 06:41:57 UTC）；续跑 0 次；采纳/复算/导出/下载全程 0 次 LLM 调用（llm_calls 恒 38） |
| 运行前后 `u90_inflight_check.py` | 前：已记录；后：`{"inflight": 0, rows: []}` |
| 边界声明 | 零新增密钥/模型/权限/代理/安全或来源许可阈值；未新增服务/worker/DB/DSL；未做全仓测试（仅触碰受影响文件）；未改 20 项量表与阈值；未 commit/未 push/未建分支 |

## 9. 剩余项（如实）
1. **R1 恢复正例未验**（§5 六条原因）；如需补验：需人工授权 ≥2 次真实运行与可对编排器进程发信号的会话，或一个真实的编排器崩溃场景。
2. **R3 恢复路径四项未验**（§6）；补验依赖同上。
3. 采纳第一次被拒暴露的产品面事实：分析卡「未进正文」时，所选运行（519c5db160f8）永远过不了绑定校验——页面已如实提示并回滚，属守卫行为而非数据损坏；是否要在工作台对「未进正文」的运行预禁用「采纳这版」，留给产品判断（本工单不改动代码）。
4. 页面「下载PDF」为按需重生成字节（同版、不同字节序列）——是否需要把下载件与包内成员字节对齐，留给产品判断。
5. `code_sandbox`（docker 不可用）与 embedding（HTTP 400 MODEL_CAPABILITY_NOT_SUPPORTED）为既有环境限制（健康页可见），本工单未处理。
6. 历史遗留已知缺陷未复测：`report_brief` DatasetManifest 参数缺失、`/deliverables` current_* 空字段（旧批次记录）。
