# DeepSeek 执行状态（2026-09-23 第四轮更新）

**当前批次**：阶段 D · **09-23 实机复核四项**（统一证据判断 / 真导出快照 / 量价分析 / 证据连续性）。
项1：风险章节与逐问支持**同一条判据**（`match_kind`）——只有 explanation 才写"解释已取得"，
行业/市场背景与读数如实写"未取得该指标变化的解释"，且背景**不计入**必答分子；
『变化解释』每条披露带"归属"行。实机：必答 1/3（收入按量价/结构支持，利润/现金为缺口）。
项2：导出快照**冻结逐成员字节**（正文/PDF/底稿/图表/审计稿/引用证据），打包不再重读磁盘，
清单按写入字节算 hash 并带 `frozen`/`drift`，自检同时对照快照 hash；页面下载 MD/PDF 与包内**同字节**
（`3d357590…` / `678a196e…`），包内 MD 与任务库送达正文逐字节相同。
项3：新增『量价与结构（发行人披露）』——白酒销售量 -16.30%、分产品/分地区/分销售模式构成、
推算吨价 202,592 元/吨（+3.93%，带算式），收入问题按**量价/结构**支持；长段原文压成节选、
原始构成表不再重复贴出。验收 pass、溯源率 92%。
项4：片段跳号 = 缺口——合并文本在跳号处插标记，跨缺口记录带 `crosses_gap`/`missing_chunks`，
定位写明"非连续原文"；包内引用证据 8 条按坐标取回一致，`text_sha256` 可复算。
顺带修复：`_mark_unsupported` 丢换行导致"## 结论"与正文粘连（结论小节认不出）——改为按字符区间替换。
正常页面链：修订采纳 `6f2c004d1b52`（身份 `9b9ad5bc`，父 `1989fa2ec508`）；页面重新导出 →
`deliverables_20260923_195700_d4afd2.zip` 17 成员、自检全一致；包内 PDF 11 页**0 字符越界**、
标题与关键句（边界/引用/免责声明）逐条可找到。
证据：`docs/evidence/batch4_evidence_snapshot_volume_gaps_20260923.md`。
未验项：真实 Redis 多进程、导出期间真实并发修订、人工研究员评分、5 页主文软目标。
本轮无新增 HTTP 抓取、无付费模型生成。

**当前批次（上一轮，保留）**：阶段 D · **09-23 日间交付真实性复验**（指令 `docs/阶段D交付真实性复验与执行_20260923.md`）。
第一批：逐问题支持关系（背景/读数/原因分开，利润不再被同段收入解释误算支持 → 必答 1/3）、
比率方向按 `(1+gN)/(1+gD)`（同增/同降/反向/等速/跨零各定向例）、新候选改走**生产装配入口**
（当前采纳正文 + 8 条已准入资料；参考来源含洋河股份:2024年年度报告）。
第二批：PDF 逐行换页（首版第 1 页有 141 字符落在页外 → 修后 0 越界）、重包绑定**一次快照**
（期间修订 → 409 不混版；唯一包名 + 原子发布）、`rules_fingerprint` 入绑定并参与失效判定、
包内补**最小引用证据**（8 条 + 片段坐标映射）。
正常页面链：采纳 `7ae1f710` → `1989fa2e`，研究状态 `stale=false`、必答 1/3；
最终包 `deliverables_20260923_134459_289ccf.zip` 14 成员、自检全部一致。
证据：`docs/evidence/batch1_truthfulness_20260923.md`、`batch2_export_integrity_20260923.md`。

**推送状态（更正）**：`279dce4` 已于本日推送成功，`HEAD == origin/main`，CI run 35804433184 成功；
此前"未推送"的记录已过时，本轮不重复推送同一提交。

**当前批次（上一轮，保留）**：阶段 D · **09-23 正常交付闭环三小批**（指令 `docs/阶段D正常交付闭环执行指令_20260923.md`）。
第一批 `f7ba751`：财务语义三反例关闭（费用率空标签/无值两期/分母更快）、删 fetch_snapshot 准入捷径、
api_chunk 诚实定位（接口片段不冒认 PDF 页码）、账本锁恢复采纳身份。
第二批 `d0ae8ef`：契约同源写端、结构投影带来源正文/资料指纹/规则版本且失效原因逐项区分、
已保存公告文本经正常准入链进任务（8 条定位、收入解释只支持收入）、按当前版本重新导出新 ZIP
（清单 schema 2 区分采纳身份与文件 hash；页面新增按钮）。
第三批：主文收敛候选（15,701→8,334 字符，验收 pass）、负号/标点/标题去重/PDF 抬头渲染修复、
模型稿按内容 hash 版本化留档并入包；正常页面链复验：采纳 `1a3477c5`、`stale=false`、
新包 12 成员自检一致。
证据：`docs/evidence/batch1_verification_gaps_20260923.md`、`batch2_state_and_package_20260923.md`、
`batch3_readable_brief_20260923.md`。
**未追加付费实机、未换模型、未改全局 0/0/0、未改 templates/权限；推送按指令未重试（本地提交在案）。**

| 层 | 状态 |
|---|---|
| 第一批 | 三反例改前全误通过→改后按"值+算式+输入"与方向一致性裁决；未准入抓取文本不再支持归因；定位写 `api_chunk K（字符 a-b）`；锁 False→True 采纳同伴身份、旧 writers 保留 |
| 第二批 | 绑定完整（契约/资料/规则指纹非空）→ 修订后 `stale=false`；资料准入链 8 条定位、`missing_kinds=[]`、利润/现金仍缺口；重导出包 schema 2、页面"包内字节自检 全部一致"、旧包保留 |
| 第三批 | 主文 8,454 字符（原 36,917）、PDF 9 页（原 15 页）；任务指令/bank_corporate/chart_id 退出主文；"见 6.3 表"类失效引用清除；标题印两遍、负号断行、句号甩行首修复 |
| 未验项（如实） | ①主文 5 页未达 2–4 页目标（已去重、底稿与次要图入附录）②真实 Redis 多进程、公开原文获取正向全链、真人评分仍未验 ③visual-judge 子代理账号不可用，未做第二人视觉复核 ④并发修订混版未实测 ⑤推送未重试（三次失败后按指令停） |
| 历史批次 | 见下（09-22 晚间与 09-22 午后） |

## 历史（按日期，保留当时结论）

### 2026-09-22（晚间）：收口复验 + 浏览器正常入口测试

### 2026-09-22（午后）：阶段 D · A→B→C→D 四小批已实现并离线验证（指令
`docs/阶段D实机复核与下一批指令_20260922.md`，审查基线 `7ec2003`）。
A 批（验收两处豁免收窄 + 一请求一票 + 415 usage 归属 + 共享写失败 fail closed + 口径标注）、
B 批（执行契约穿过 Critic 修订到实际派发 + 已存真实材料整链闭合）、
C 批（归因/算术分开核验 + 图表 chart_id 绑定 + 就绪按契约必答问题逐项裁决 + 状态绑定采纳正文）、
D 批（候选修订保留历史 + 同版导出清单/PDF + 逐页目视 + 旧包陈旧）。
**未追加付费实机、未换模型、未改全局 0/0/0、未改 templates/权限。**

| 层 | 状态 |
|---|---|
| A-1 验收豁免收窄 | `_extract_source_claims` 豁免收窄到**声明片段本身**（"数据来源：X，需核查…"仍检查 X）；同值提升必须**同一事实身份**（`_promotion_subject` + `_subjects_conflict`）。实测：来源为空 + 尾加"需核查"→ 仍 fail（检查 1 条）；来源只有宁德时代营收100亿元、正文另称比亚迪/腾讯各100亿元 → **fail，溯源 33%（1/3）**；真"需核查……才能判断"句仍 pass |
| A-2 一请求一票 | `llm_client`：只有**真的流式→非流式回退**才另开票（`self._fell_back`）。实测：普通非流式 1 次发送 / 1 票 / 用量 5 一次 / 1 条记录；cap=1 首次放行、第二次发送前拒；415 回退 2 发送 2 票、成功 usage 只记一次、415 那次 `unknown_calls=1`、形状 2 条 |
| A-3 共享写失败 fail closed | `_save` 返回是否可信落盘；拿不到锁**不写**（不做无锁覆盖）；`persist_uncertain` 是账本级事实（落盘 + 断点继承）；有界多进程在"共享不可用 / 已标记不确定 / 预留未成功返回"三种情形**拒绝新付费请求**，并撤销该次预留（本地+共享计数回滚）。不配上限的任务照常（保留可用报告与只读工作台） |
| A-4 口径标注 | `BudgetLimits` 文档 + 快照新增 `count_basis`（`max_calls`=所有预留票据；`provider_requests`=llm+backup；`steps_dispatched`=step）与 `token_basis`（上界/实际/未知独立）。**未放宽任何上限** |
| B 执行契约 | 新增 `execution_contract.py`：主体/代码/期间/as_of/口径/文档类型/必需指标 → 指纹、按年短查询、期间冲突判定、幂等重建、不变量校验、历史提示"不适用"标注。修订稿先按契约重建**再复评**（PASS 绑在重建后的这一版）；派发前复核指纹，不一致则重建、重建失败/仍冲突则**拒绝派发**；SearchAgent 查询来源优先级 = 派发契约字段 > 指令 `[检索查询]` 行 > 整段指令，并剔除含契约外期间的变体 |
| B 原始字节复用 | `web_fetch_worker` 把 PDF 原始字节落任务工作区工件（`project/fetched/<sha16>.pdf` + sha256）；`_try_pdf_evidence` 解析**消费同一份字节**（校验 hash、路径必须落在任务工作区内），不再按 URL 重抓；工件缺失/校验不符按缺口处理 |
| C-1 归因与算术 | 新增 `check_attribution_support`（缺证归因不得因后接边界句豁免；明确假设/待查不扣分）与 `check_ratio_arithmetic`（绝对额减幅不能替代比率复算；给出两期比率或底稿派生关系则通过）。实机两处反例（§3.3 固定费用归因、§5.2 负债率算术）均被判 fail |
| C-2 图表绑定 | 规格带稳定 `chart_id` + 绑定块（指标/单位/期间/口径），渲染脚本写入清单，简报图注显示 `图 N［chart_id］…（绑定：…）`；新增 `check_chart_references`（图文错配只认正向证据）；同比全为负时措辞改为"降幅最小/降幅最大"；顺带修 chart_id 下划线被 Markdown 吞掉的渲染缺陷 |
| C-3/4/5 就绪与绑定 | `research_state` 重写：必答问题按契约（默认收入/利润/现金）**逐项**裁决；银行材料只作可选问题独列（不再出现 1/4 凑数）；肯定结论处于未证实状态即不就绪（语义角色判定，不豁免 boundary 字符串）；状态带 `binding`（采纳身份/正文 sha/结构版本/契约指纹与原文），结构不属于当前正文 → **待重验**；`export_manifest.json` 文件本体新增 `research_state` |
| D 同版交付 | 实机工作区离线候选修订（保留历史版本）：两处金融反例 + §2.4 同类绝对额解释 → 修订后**验收 pass / 交付 verified**（修订前 fail/draft）；图表按生产通道重算重渲染（6 张带 chart_id）；走 `/report.pdf` 同一路径实际导出，清单 `files.pdf.sha256` 与字节一致（`ea9321d2…`），清单与状态**同一 report_version_id**；15 页逐页目视（bundled `pypdfium2`）——图 1–3 清晰、图注与图一致、表列对齐；记录三处未修版面小缺陷 |
| 验证（本批） | `test_root_budget` 72（新增一请求一票 3 + 失败拒发 4 + 口径断言）；`test_delivery_chain` **315**（新增契约 5 + 已存材料链 3 + 归因/图表 4 + 就绪与绑定 3 + 同版链 2）；`test_acceptance_adversarial` 23（新增冻结反例 3）；`test_offline_delivery` 34；`test_r0_boundaries` 47；`test_report_quality` 32；`test_financial_chain` 34 全绿 |
| 未验项（如实） | ①真实 Redis 多进程未验（本批用内存假后端 + 无 Redis 端口）②公开原文获取未实测（离线批）③PDF 逐页目视由执行者完成——本机 visual-judge 子代理不可用，未做第二人复核 ④候选修订是离线产出，服务装载的仍是修订前代码 ⑤版面三处小缺陷已记录未修 ⑥研究状态仍 `research_draft`（必答三问 0/3 有定位依据）——本次确实没有取得原始年报，是如实结论 |

### 2026-09-18：C 批（固定研究路径）与 CI 修复

**C 已完成**
- 研究任务走**代码内固定步骤**（契约 → 搜索/抓取 → 解释已选事实 → 出报告）：命中条件是
  表单落库契约 + `facts.research_shaped`（有主体、≥2 期间、已声明口径）；先于关键词匹配与
  那次无预算的 LLM 路由调用。闸门一条不绕（取消/评审/计划确认/根预算/准入/研究硬门槛）。
- 结构化预载前移到规划之前；失败分支也落可复核底稿；报告步骤注入"已选事实"块。
- `route_structured_for_request`：契约有市场 + 稳定代码就直接抓（跳过 resolve 公司名）；
  适配器只收**本地代码**（`600519.SH` → `600519`，实机预探针发现的缺陷）。
- 脱敏调用形状 `llm_calls`：阶段/次数/耗时/输入长度/输出上限/HTTP 或错误类别/结束原因，
  收尾落 `llm_calls.jsonl`；规划失败与重试的进度消息不再写异常原文。

**已验证范围（三类证据分开）**
- **离线矩阵**：`TestResearchFixedPathOffline` 11 项（规划超时/坏 JSON 仍进受控路径、
  素材缺失绝不判已验证、正文失败仍留可重算底稿、三种时点取消、根预算耗尽、诊断脱敏）。
- **全量回归**：CI backend 的全部 **45 个测试文件本地逐个跑通**（与 CI 同一命令，最终提交
  `c451491` 上重跑确认）。
- **CI**：`c451491` 四个 job 全部 success（backend / frontend / docker-image / clean-env-e2e）。
- **实机（一轮，未达终态）**：固定路径与契约抓取在真实服务上生效（执行步骤正是固定四步，
  规划器未被咨询；`financials.json.contract` 与东财两年数据齐全；11 次调用形状脱敏可读）。
  未达成"合格报告"：① 抓到的 18 条事实口径全 unknown（**没有适配器声明报表口径**，而 A′
  要求口径只认来源声明的）→ 6 项必需事实缺口 → draft；② LLM 端点持续 504/读超时，反思
  不可用 → 强制重做循环 → 超过预声明的 25 分钟墙钟上限，按纪律请求停止（11.6s 生效）；
  ③ 模型正文缺来源清单/免责声明且有 8 处占位 → 验收 fail。四面同版、一次修订、导出四件套
  **因未达终态未验证**。

**阻塞与下一步**
- **口径证据链已修（`docs/evidence/caliber_evidence_chain_20260918.md`）**：查清可达来源里
  **不存在**机器可读的口径字段（东财主指标 165 字段无、F10 忽略 type 参数、cninfo 未打通），
  因此改为让**适配器按来源自身的结构**声明口径并要求附依据：A 股行含 `PARENTNETPROFIT`、
  港股含 `HOLDER_PROFIT`（只在合并报表存在的科目）→ 声明合并；SEC 按 10-K 申报规则声明。
  缺该结构就不声明（fail closed），`_caliber_ok` 门槛一字未改。事实新增
  `caliber_source`/`caliber_evidence`，底稿行、读侧 detail 与报告的"已选事实"块都带口径与依据。
  顺带闭合 A′ 遗留：东财 `NOTICE_DATE` → 逐行披露日期（截至日证据）。
  离线端到端已证：生产形状的研究任务跑到 `status=verified`（6/6 必需事实 + 3 条同比），
  不声明口径的对偶仍判 draft。
- 仍待验证：修复后的**实机**验收（不重复跑同一条实机请求）；港股无公告日期（截至日证据偏弱）、
  美股口径依据是申报规则（不可由数据自证）、母公司口径在这些来源上取不到（会如实报缺口）。
- 环境：LLM 端点 `tokenrhythm.studio` 不稳定（504/读超时）；嵌入能力不可用（HTTP 400
  MODEL_CAPABILITY_NOT_SUPPORTED，已按降级处理）。
- 未做：前端修订入口（界面留到 D）、港股/美股契约抓取实机、D（试用与扩展决策）。

## 历史（按日期，保留当时结论）

### 2026-09-17：B 批（PDF 排版）与 CI 收尾

**B 批完成，并从 CI 日志里挖出两个 Linux 渲染真缺陷**

已完成（都有断言）：页脚「第 N 页 / 共 M 页」、长表格**跨页重画表头**、缺图**可见占位**、
封面标题与正文同名标题**去重**、三处画线几何（此前多一个操作数把线画成斜线）、
表头文字基线落在色块之内。

从 CI 日志挖出的两个真缺陷（都属于"只有真渲染才看得见"）：

1. **cmap 只保留一个子表** → Linux 字体（DroidSansFallbackFull.ttf）的 ASCII 与 CJK
   分处不同子表，取到的那份不含 ASCII → `glyph_id("1") == 0` → PDF 里「第 1 页」
   印成「第  页」、英文文件名整段消失（抽取文本是一串 `\x00`）。
   已改为合并全部 format 0/4/6/12 子表（按 (3,10) > (3,1) > (0,*) > 其它 覆盖）。
2. **字体缺字形时直接留白**。合并子表后 CI 仍然缺 ASCII（该字体的 Latin 不在我们
   解析得到的子表里），于是改成**让排版对缺字形免疫**：缺字形且落在单字节范围的字符
   改用 PDF 内置 Helvetica（/F2，base-14，无需嵌入）绘制，宽度口径同步；
   `_text_run` 切段 + 只在真用到时登记 /F2。
   测试用"把字体对 ASCII 的映射强制成 0"复现该环境，断言数字仍可见 —— 断言的是
   **"文档能渲染"**，而不是"字体恰好覆盖全"（后者取决于部署环境装了什么字体）。

- 顺带：排版断言改为**空白不敏感**——按字形切段绘制后，抽取工具会在两段接缝处多插
  空格（CI 抽出 `第 1 页 /  共 3 页`），空格数量是实现细节而非被测内容。
- **未验证**：逐页视觉验收（需要栅格化渲染器；本机 pypdfium2 无 3.14 wheel、
  无 poppler/mutool/ghostscript/ImageMagick）。当前只有几何/结构/文本断言。
- **未验证**：`b280cea`/`e7697a7` 的 CI 结果（本机代理当时不可达；`ec4f942` 之后
  的每次提交都只做本地全量复跑）。

**CI 从"从未观察过绿灯"到全绿：这轮修掉的七个真问题**

| # | 问题 | 性质 |
|---|---|---|
| 1 | `ci.yml` 步骤名里未加引号的冒号（`(R1: bound PASS…)`）→ 整份 YAML 解析失败 | **门禁自身坏了**：GitHub 仍创建 run，但**零作业**、结论 failure，本地全绿看不出来 |
| 2 | `launcher.py start` 拉起子进程后自己返回 → 容器 PID 1 退出、服务被收走 | 部署真缺陷：`docker compose up` 起来即退 |
| 3 | redis 无就绪探针 + `depends_on` 只保证启动顺序 → app 反复重启 | 部署真缺陷 |
| 4 | `web_ui` 默认 `BIND_HOST=127.0.0.1` → 容器只监听自己，`ports: 8080:8080` 空转 | 部署真缺陷（宿主机 readiness 探活 200s 全失败） |
| 5 | `test_windows_prefers_system_binary` 在 Linux 上必然失败（两端分支不同） | 用例机依赖（已按平台分叉 + 补 POSIX 用例） |
| 6 | `live_probe_embedding_classifies_quota` / `test_checkpointer` / `test_task_time_optimization` 依赖本机 config.json（CI 无配置、本机改用了真实 key 的余额） | 用例机依赖：CI 报"未配置"、本机报"余额不足"——都不是被测行为 |
| 7 | e2e 脚本 `_status` 查 `task_history` 时表未建好即抛异常 → 门禁一次轮询失败就整轮崩 | 门禁脆弱：同一份代码上一次绿、这一次红 |

- 另外补了守卫：`ci.yml` 必须能被 YAML 解析、每步恰有 `run`/`uses` 之一、名里不得有未加引号的 `: `；
  compose 的"容器必须常驻 + redis 就绪探针 + `BIND_HOST=0.0.0.0`"不变量。
- `test_task_time_optimization::test_convergence_continues_when_best_report_improves` 的状态断言
  在 R1/R2 **之前就是红的**（用 `d0e5a72` 的 worktree 复核过）：该用例全程打桩、没有版本绑定的
  验收，按"以最终验收判定"就该记 `SUCCESS_WITH_ISSUES`；已按可达且正确的期望改写并注明原因。
- **仍如实记两件事**：① `backend` 是 fail-fast，当前全绿说明所有 44 步都过了，但一旦早步骤失败，
  后面会显示 skipped（本轮就是这样才发现不了后面 37 步的问题）；② CI 日志/annotation 需要登录
  才能读（API 403），本轮起 `gh` 已登录（`momang85`，scope 含 `repo`）但**推送/读日志要记得给代理**
  （`HTTPS_PROXY=http://127.0.0.1:7897`，gh 不读 git 的 `http.proxy`）。

**B 批（PDF 排版）：线几何已修，其余项与视觉门禁未做**

- **三处画线全是同一个真缺陷**：`report_pdf.py` 的画线指令写成 `x y W 0 m x2 y l`，
  而 `m`/`l` 各自只吃两个操作数——PDF 取**最后两个**，于是线被画成从 `(W, 0)` 到
  `(x2, y)` 的斜线：位置、长度、方向全错，却仍然"生成成功"（所以只看"没报错"发现不了）。
  已修：分隔线（正文 `hr`）、表格行线、标题下划线三处，全部改成 `x y m x2 y l`。
- **加了几何守卫**（`test_p0.TestReportPdfLineGeometry`，3 项）：解 PDF 内容流按算子取
  紧前操作数，断言 `m`/`l` 恰好两个操作数、线必须水平、跨度只能是正文宽或标题下划线的
  80pt、端点落在可打印区内。**已验证这个守卫能抓住原缺陷**（把旧写法改回去 → 测试报
  `4 != 2`，输出里能看到 `50.00 668.25 495.28 0 m ...` 那行）。
- **B 批其余项未做**（如实记账）：长表格跨页表头重复、图片缩放/失败占位、页脚页码、
  封面简洁任务名去重。
- **视觉门禁在本机不可用**：逐页出 PNG 需要渲染器，而本机 `pypdfium2`（Python 3.14 无
  wheel）、`fitz`/`pdf2image`（无 poppler）、`mutool`、`ghostscript`、ImageMagick 全部缺失
  （Windows 自带的 `convert.exe` 是文件系统工具，不是 ImageMagick）。装上可用渲染器之前，
  这一批只能做几何/结构断言，**不能宣称"逐页视觉验收通过"**。

**检索质量统一：已落地（两条路径收敛为一个实现 + 策略可配置）**

- **唯一实现**：`adapters/search_quality` 成为检索质量的唯一事实来源；
  `worker_base.SearchAgent` 的 `_clean_search_text` / `_extract_keywords` / `_query_variants` /
  `_filter_results` / `_is_garbage_result` 全部委派过去。此前两条路径各有一套近似的词表、
  计分与变体逻辑（一条有长连读要求与权威域加权、另一条没有），同一份结果在一处被过滤、
  在另一处被保留；已部署策略的 blocks/boosts 现在作为参数进入同一实现。
- **策略可配置**：新增 `SearchQualityPolicy`（`system.search_quality` 段 + 环境变量
  `WM_SEARCH_MIN_SCORE` / `WM_SEARCH_REQUIRE_HIT_RUN` / `WM_SEARCH_MAX_VARIANTS`）：
  min_score、长连读要求、变体上限、权威域四档权重、停用词、垃圾域名/路径/标题、
  机构定向白名单、以及 **ddgs 引擎清单**（原先写死在 worker_base）。默认值 = 迁移前行为，
  `config.example.json` 已附完整示例段。
- **迁移中发现并修掉两类假阴性**（都会让合法结果被整批误杀）：
  1. 长连读要求对**纯英文结果**也生效：查询里混着中文指令碎片（"搜索GitHub上完整的…
     开源项目"）时，合法的英文结果全军覆没。现在该规则只作用于含中文的结果；
     英文结果由英文词边界计分与 min_score 判定。
  2. 调用方**显式放宽** min_score（"严格过滤为空 → 放宽再试"的兜底路径）时不再套硬规则，
     否则"放宽"等于无效。
- **顺带修（实机复现的真缺陷）**：预算账本的跨进程计数原先每次读取都做一次 Redis 往返，
  Redis 不可用时每次付连接超时——看门狗的一次告警被拖到断言之后才发出（告警形同无效）。
  现在：**观测读取不为探测付代价**（只有写路径建立后端连接）、后端失败**按后实例永久降级**
  并有进程级健康缓存、账本客户端用 0.3s 连接 / 0.5s 读（消息总线的 5s 长连接超时另算）。
- **顺带修**：共享计数的键空间原先只按 `root_task_id`，同一 id 重新提交（重跑/测试复用）
  会继承上一轮留在 Redis 的花费，第一次预留即被拒。现在按**账本身份**
  （`ledger_id`，随账本文件持久化）隔离：同一次运行（含断点恢复）共用计数，
  重新开始的一次运行不继承上一轮。
- 回归：新增 `test_search_quality_unified.py`（17 项：两条路径同源、默认值不变、
  配置/环境变量生效、英文结果不被误杀、显式放宽生效）已接入 CI；
  `test_root_budget` 36→37、`test_offline_delivery` 8（并修掉 `TestOfflineFaultInjection`
  的**用例间端点健康缓存泄漏**——单跑通过、整类跑失败的假失败）；
  `test_p0` 391（含新增的 3 项 PDF 几何守卫）、`test_orchestrator_v2` 68、
  `test_delivery_chain` 184、`test_prompt_system` 39 全绿。
- **环境事实（如实记录）**：本批回归期间本机 Redis 曾停掉，套件耗时从 34s 涨到 209s 并出现
  8 处超时型假失败；用项目自己的 `dep_check.ensure_redis` 起回便携 Redis（pid 4604，
  绑定 127.0.0.1）后恢复（34.8s / 68 项全绿）。这些失败**与本批改动无关**，
  但说明这套测试对机器负载敏感，不能只看一次红灯就下结论。

**R2 请求级预算与取消：已落地**

- **票据状态互斥迁移**（去掉假平衡）：一张票据只能从 `open` 走到 `settled` **或**
  `unsettled` 之一，且只走一次。此前 `_call_llm_cancellable` 取消时写的是**凭空造的**
  `"{phase}-inflight"` 票据（账面上"取消已入账"），而真正预留的那张仍挂在 open 上，
  随后又被调用方的 `finally` 无条件 `settle(ok=True)`——同一张调用一次 unsettled、
  一次 settled，真实状态再也读不出来。现在：取消只动**真票据**（`mark_unsettled(ticket)`
  或 `mark_stage_unsettled(stage)` 只迁移当前 open 的），虚构票据一律拒绝并计入
  `rejected_transitions`；票据的生命周期收进 `_budget_scope` 上下文管理器（正常返回
  → settle ok；`TaskCancelled(ticket_settled=True)` → 不再结算；其它异常 → settle
  ok=False）。
- **token 也按上界预留**：修掉反例——`max_tokens=100` 时两次 `reserve(tokens=80)` 都会获准
  （此前只在结算时才扣）。现在 `remaining()["tokens"]` 用"已承诺量 = open 上界 + 已结算实际
  + 待对账上界"，第二次预留直接被拒；退回（发送前失败）会把上界还回去。
- **跨进程原子预留**：计数与票据号走 Redis `INCRBY`（先加后校验、超了回退），文件只是快照。
  修掉反例——两个实例各自 `reserve` 都获准、各自返回同一票据号（`step-1`）。现在票据号带
  序号 + 实例标记，`remaining()`/`snapshot()` 读共享计数，`snapshot()` 另给本地口径
  （`calls.local_reserved`）并标 `cross_process`。Redis 不可用时降级为单进程账本（如实说明）。
- **取消不可复位**：取消请求同时写提示键（可清）与**不可复位令牌**（无 TTL，`NX` 只写一次），
  并且 `_cancel_requested` 还认**已落库的 CANCELLED 终态**——Redis 被清/重启也不会
  "取消被抹掉"。`_clear_cancel` 只清提示键，令牌与终态保留。
- **取消后零新请求**：`llm_client` 新增取消守卫（编排器在 `run()` 注册 `_cancel_requested`），
  在**每次尝试前**与**切备用前**（含"健康路由直接走备用"那条分支）复查，命中即抛
  `LLMCancelledError` → `_call_llm_cancellable` 转成 `TaskCancelled`（取消不是失败，
  不走重试/降级）。此前取消只在派发边界可见，客户端仍会把重试与主备各跑一遍。
- **新线程在创建边界传 context**：`_call_llm_cancellable` 用 `contextvars.copy_context()`
  把调用方上下文带进工作线程并在拷贝里 `set_task_context`（`threading.Thread` 默认不继承
  contextvars；之前线程内台账/守卫都拿不到 task_id）。
- **根 deadline 收口等待**：`_wait_step_result` 的等待上限取 `min(步骤超时, 根任务剩余秒数)`，
  避免"根预算早已用尽还在按 300s 空等"。
- **两个时限分开记录**：`_cancel_latency` 分别给出取消 UI 响应时延（`WM_CANCEL_UI_DEADLINE_SECONDS`，
  默认 30s，超出如实标记）与供应商在飞结算窗口（`WM_INFLIGHT_SETTLE_SECONDS`，默认 900s，
  进 `unsettled` 票据的 `reconcile_by`）；拿不到请求时刻就报"未知"，不编数字。
- **0 值语义修正**：`system.budget` 全 0 = **真正不限**，不再回落 `system.task_timeout`
  变成 600 秒硬截止（配置写 0 与账本记的上限从此一致）。
- **顺带修（实机复现的真缺陷）**：`_execute_steps` 的 pipeline 串行判定与"占名额"不在同一次
  持锁里——两个 worker 都可能读到 `in_flight==0` 各取一步，pipeline 模式实测并发变成 2。
  现在串行判定与 `in_flight += 1` 在同一次持锁内完成。
- 回归：`test_root_budget` 19→36（token 上界、跨进程双实例、票据互斥、根 deadline、两个时限）、
  `test_cancel_semantics` 36→46（不可复位令牌、客户端重试/切备复查、线程上下文），
  `test_p0` 388、`test_orchestrator_v2` 68、`test_delivery_chain` 184、`test_offline_delivery` 8 全绿。
- **未验证**：跨进程原子预留只在替身 Redis（`INCRBY` 语义）上验证，未做真实多进程并发压测；
  真实供应商的计费窗口未观测（`WM_INFLIGHT_SETTLE_SECONDS` 是策略值，不是实测值）。

**R1 评审与待复核隔离：已落地（4 项 + 审批 API 并发保护）**

- **交付状态按根任务**：`_delivery_draft_reason` / `_delivery_status` / `_delivery_hard_fail`
  三个实例标量 → `_delivery(task_id)`（按根任务存放）。此前同一实例并发跑两个任务时，
  A 收尾写入的草稿理由会把 B 已判定的 verified 翻成 false，B 的交付与准入随之误判。
- **异版 PASS 不复用（评审绑定计划版本）**：`_review_bind_pass` 除指纹外再记
  `plan_version`；新增 `review_scope_ok()`（PASS + 策略版本 + 身份模式 + **绑定版本 == 当前版本**），
  交付守卫与准入判定都改读它，不再读裸 `verdict == "PASS"`。
  - 为什么用版本号而不是只比指纹：Critic 评审发生在规划返回时，之后编排器还要做机械加工
    （依赖连线、包步骤补齐、目标/Skills 注入、结构化裁剪），执行的那份 `steps` 与评审时
    看到的对象**本来就不同**——按对象相等判定会让每一次真实运行都被判成"没有绑定的 PASS"。
    因此机械加工**不**改版本（同一版计划的物化），**实质改写**才 +1：反思追加/替换步骤、
    计划确认阶段用户改了计划。
  - `_plan_fingerprint` 同时改成规范投影（只取 step_id/capability/instruction/depends_on/round，
    缺字段与空字段等价、依赖排序无关），执行期回填的 `iteration/status/result` 不再改变指纹。
  - 后果如实说明：反思改写过计划的运行，旧 PASS 不再覆盖它 → 个人模式仍可作经验
    （admitted）但**不计入已验证成功**（verified=false，模板固化因此不触发）；
    银行口径下连经验池都不进。这是"异版 PASS 不复用"的应有语义，不是回归。
- **恢复不复用旧 PASS**：`_restore_review_state` 增加计划版本比较（策略版本/身份模式已有），
  版本不符 → 按"未完成评审"处置；**旧 checkpoint 没有 `plan_version` 字段 → 视为无法证明，
  一律不复用**（拦下而不是放行）。checkpoint 新增 `plan_version`（与 `review.plan_version`
  分开记：两者不同即表示落盘时计划已被改写）。
- **待复核内容的隔离闭环**：
  - `add_prompt_refinement(..., status=...)`：默认 **`pending_review`**（默认拒绝），
    调用方必须显式声明"本次运行已验证成功"才写 `active`；状态同时进 metadata 与文档正文。
  - `query_prompt_refinements`：向量路径与**字面兜底路径**都只返回 `status == "active"` 的条目；
    **缺 status 的历史记录一律按未生效处理**（它们写入时正是"反思产出直接生效"的年代，
    状态无从证明）。此前注册表按 M0-d 拦住了 pending_review，但 RAG 那条没有拦——
    同一次运行写下的改进仍然会被下一个任务检索并注入。
  - `count_pending_refinements()` + `memory_health.refinements_held`：隔离**可见**，
    不是静默丢弃；`set_prompt_refinement_status(ids, "active")` 是放行入口（只改状态，
    **不动**任何样本的核实标记——人工 approve ≠ 数据已核实）。
  - 反思写入方（`_record_reflection_refinement`）与自迭代写入方（`prompt_refinery`）都改成
    按准入结论传状态：注册表与 RAG 两条路径用**同一个**状态。
- **审批 API 并发保护**（`/api/evolution/approve`）：认领改为原子
  （`lrem(count=1)` 的返回值当凭据，拿不到 → 409），修掉"两个并发审批都读到同一条待审策略、
  都删成功、都去部署（同一策略被批准两次、后一次覆盖前一次的 rollout）"；另加版本保护——
  已部署的另一版策略不再被静默覆盖（需显式 `force=true`，冲突时返回 409 且**不**把待审策略
  认领掉，否则它会凭空消失）。**未做**：CSRF（后端同时接受 `Cookie: session=`，
  跨站表单可借会话 cookie 发起写操作）与 A5 管理界面——按计划留到 D 批。
- 回归：新增 `test_review_isolation.py`（10 项，含"生成→待审→下一任务检索/注入为零→放行后可用"
  链路）已接入 CI；`test_review_protocol` 24→28、`test_admission` 29→31（新增异版 PASS
  的本地/银行两条判定）、`test_memory_degradation` 30 项。

**MKT-P0-4 README 区间披露 + 定位改写：已落地（仅仓库内）**

- 删除单点最优值：`README.md` 原第 37 行"A 股活跃度日报数字可溯源率 **85%**"已替换为
  **8%–85% 区间表**，四类任务分列（A 股结构化链路 85% / 美股链路 8%、金额 0% /
  财务任务结构化未命中 46%、通用调研 0%），每行带独立实测来源链接，并明写
  **"能否拿到报告期结构化财务数据是 85% 与 8% 的分水岭，而不是模型能力差异"**。
- 定位改写：首屏加"**主打场景：可复核的上市公司研究工作台**……每个数字都要能回溯到底稿并重算，
  算不出来的必须显式标成'模型知识'"，并声明"披露完整区间而不是只报最好那一次"。
- 美股链路状态如实标注：根因已修 + 英文/代码主体识别 + 单位口径 + 子句级主体绑定，
  但**验证范围只到合成夹具（金额溯源 0.833）**，真实 `data.sec.gov` 抓取未跑通，
  故 8% 仍作为可引用实测值保留。
- `README.md`「已知限制」原写"不绑定数值-主体归属"（已过时）→ 改为**子句级已绑定 +
  文档级仍缺**（指向 `test_acceptance_adversarial.py::test_known_gap_number_subject_mismatch`），
  并补美股链路验证范围、结构化链路覆盖面两条。`README.en.md` 补同口径三条，两份不冲突。
- 首屏 `docs/hero-demo.gif` 引用**已存在**（README.md:17），无需新增素材。
- **未做（按你已定的边界）**：Show HN / 掘金 / 公众号发布、定价与询价——由你本人执行，
  我不登录外部平台。

**MKT-P0-1 干净环境端到端门禁：已落地并本机演练通过**

- `scripts/e2e_clean_check.py`（临时目录 clone → 依赖自检 → 起 stub → 起服务 →
  提交任务 → 等终态 → 断言产出报告；独立端口、结果写 `docs/evidence/e2e_clean_last.json`）
  ＋ `scripts/stub_llm.py`（OpenAI 兼容替身，不烧额度；凭据运行时随机占位；
  请求限回环白名单）。
- 本机演练 EXIT=0：commit `c1a7559`、deps 退出码 0、16/16 服务存活、任务 `ui-842ff25fe6`
  以 **SUCCESS** 结束、任务库 report 1648 字符；本机实例未受影响。
- 已进 CI：`clean-env-e2e` 作业（Redis service 容器 + 仓库内 stub + 独立端口 + 证据 artifact）。
- **未验证**：CI 作业尚未观察到绿灯；克隆源是本机 `.git`（证明"提交内容足够"），
  不等于远端 clone 的同一性；该演练不证明报告质量（stub 返回固定文本）。

**MKT-P0-3 美股链路：已接进 target（数字-主体硬绑定仍待做）**

- **真断点已修**：US 的允许清单/注入/`fetch_sec` 其实都在，断在**公司名提取只认中文**
  （`_looks_like_company` 要求 2-6 个汉字）——英文/代码目标（"分析 WBD 的财报"、"AAPL 营收"）
  提取不到主体 → `route_structured` 返回 None → 工作区从来没有 `financials.json`。
  现在英文名/美股代码都能认（`(?<![A-Za-z])` 边界，中文紧邻也成立），并补了
  `中文名（代码）` 锚点（"微软（MSFT）"）与请求语剥离（"请给微软"不再留下"给微软"）。
  同时修掉三类把噪声当公司名的提取（动词前缀 "Analyze Apple"、期间残片 "一期"、
  泛称 "美股科技"），中文既有行为**不变**（`test_financial_chain` 31 项全绿）。
- **单位诚实性**：`_collect_sources` 原来对所有市场把金额写成"亿元"；现在优先用源声明的
  `metadata.unit`（美股为"亿美元"），不再靠短单位兜底偶然命中。
- **固定夹具 + 判据**：新增 `test_us_chain.py`（9 项 EXIT=0）——合成 10-K 夹具（**非现实公司
  事实**，明确标注）走真实 `route_structured` → SEC 分支 → `financials.json` → 注入 →
  验收，实测 **金额溯源率 0.833（5/6）**、阈值仍 0.7、overall=pass；夹具里没有的数字不被算作可溯源。
- **数字-主体绑定：子句级已绑定，文档级仍是缺口（本批如实划分）**
  - 已绑定：数字**紧邻的子句**里写明了他司主体、且命中来源行主体不同 → 该数字不进可溯源
    （新用例 `test_clause_subject_conflict_is_rejected` 断言）；比较报告/多实体报告**不误伤**
    （收窄过程中修掉三类假阴性：拿任务目标当兜底主体、`_norm` 压掉换行导致行内主体取成"第一家公司"、
    "口径/材料"这类泛称被当公司；并给归属检查内部调用加了 `subject_check=False`，两套判断不再互相污染）。
  - 仍缺：报告只在**标题/文档级**声明主体、数字所在单元没有局部归属时主体不参与绑定
    （架构给的那条 1741 亿用例仍判可溯源，已在测试里**记录为已知缺口**并标明阈值未下调）。
    要补它需要在"文档级主体 + 多实体识别"上做设计，避免重新引入上面的假阴性。

**MKT-P0-2 便携 Redis 国内可用性：已落地（多源 + 预算 + 离线复用 + 中文指引）**

- `dep_check.py`：**系统已装 Redis 优先**（Windows 先探 PATH / 已注册服务 / 常见安装目录）→
  **离线复用**（`.weavemind/downloads/redis-windows.zip` 合法即用，不联网）→ **多源下载**
  （`WM_REDIS_ZIP_URL` → `WM_REDIS_MIRRORS` 前缀 → 内置 ghproxy/gh-proxy/ghfast → 官方 release），
  默认总预算 60s、每源 20s，到点即停；可选 `WM_REDIS_ZIP_SHA256` 强校验摘要；镜像主机已进下载白名单，
  但仍走 https + 重定向逐跳复校 + 体积上限。失败指引改为**可执行步骤**（系统安装/Docker/镜像/
  手动放包/固定摘要/换 host）。
- **实测（真实网络）**：用户镜像不可用时自动落到 `ghproxy.net`，**24.6s** 取到 zip
  （13,933,009 字节，sha256 `4e8f2f956ed92fea…`），且与更早一次从官方源获取的同一文件
  **逐字节一致**（两条独立路径互证）；预算纪律 ≤30s 达标。
- 回归：新增 `test_redis_acquisition` **14 项 EXIT=0**（源顺序/前缀语义/摘要拒绝后继续试/
  非 zip 拒绝/预算到点停/离线复用不联网/系统 Redis 优先/指引可执行/默认预算 60s），已接入 CI。
- 顺带修：`test_startup_readiness` 的全仓守卫抓到我这轮三个驱动脚本没关 Redis 内建重试
  （Redis 不可达会挂几十秒），已统一传 `_NO_REDIS_RETRY`。
- **未验证**：镜像可达性只在**本机网络**验证过一次；"无代理 60 秒就绪"这条判据在你的真实网络里
  需要再跑一次确认（清掉 `.weavemind/redis/portable` 后 `python launcher.py deps --fix` 即可复现）。

**R0b/R0c 进度（逐项给证据）**

- ✅ **R0.2 版本身份真实接线**：验收输出**全量** `report_sha256`（短 hash 单列 `report_sha256_short`，
  只作显示）；`bind_acceptance` 改为**按完整身份精确绑定**（短 hash / 来源指纹不符一律不绑，只更新
  目标条目）；`adopted()` 去掉"同正文兄弟条目借验收"的回退；`adopt()` 改为**身份优先**定位（修掉
  "同正文换来源时采纳写错条目"）；终态判定新增 `_acceptance_summary_for_status`——读**选中版本自身**
  验收并带 `version_bound`/`needs_reverify`，未绑定由 `derive_status` 统一按有缺口处理。
- ✅ **R0.3 统一"已验证交付"谓词**：`report_version.verified_delivery()` 成为唯一实现（身份/正文/交付
  一致 + 验收 **pass** + 属该版 + 硬约束 + **必需**评审；个人模式评审降级不算草稿，银行口径缺 PASS
  仍是草稿）；编排器收尾守卫、导出清单 `status/draft/draft_reason`、`/pdf` 与 `/report.md` 响应头
  共用它。清单写入失败**可见**（`manifest_write_error` + 日志）；导出路由不再把"导出失败"混成 404。
- ✅ **前端导出边界**：401/403 分别提示"需登录/无权限"且**不自动下载**；服务端不可用退本地缓存稿时
  文件名与提示写明"**本地未验证副本**"；打印页页脚始终标注版本或用"版本未知（本地未验证副本）"。
- 🐞 **顺带修掉一个真实缺陷**：`web_ui.py` 没有模块级 `logger`——此前四处导出/清单日志都会抛
  `NameError` 被吞（"清单写不进去也没有任何可见记录"），现已定义并实测（失败日志正常打印）。

**R0 进度（首批，逐项给证据）**

- ✅ **R0.4 计算反例**（`acceptance_checker.py`）：常量豁免改为**按操作数位置**判定
  （`1000/1-1` 的分母 1 不再被末尾 `-1` 连带豁免）；新增逐操作数语义联合判定
  （`_combo_semantics`：每个必需输入都要绑定指标/期间/币种，未绑定 → unknown 而非 ok）；
  材料解析新增币种，并修掉两处被它照出来的真实缺陷——指标取"±20 字窗口首个词"（会把
  `2025 年收入 1200 万元` 标成"毛利率"）、期间取"子句首个年份"（会把 2024 的数标成 2025），
  现在都改为**取数字之前最近**的词/年；顺带补上 `万美元/万港元` 的单位解析。
  回归：`test_r0_boundaries`（R0.4 部分 7 项）+ `test_report_quality` 26 + `test_acceptance_adversarial` 19 全绿。
- ✅ **R0.1 目标达成与诚实披露分开**：新增 `derive_requirements`（目标里要求数据吗？要来源吗？
  点名了哪些指标/期间？）与 `check_requirement_coverage`（四态 `executed / honest_disclosure /
  goal_met / evidence`），并把"目标未达成"做成**独立门槛**：缺必需数值或来源 → 不得 `overall=pass`；
  缺失但已显式披露 → `partial`；未披露 → `fail`；正文为空 → `unknown`；定性要求（"定性即可、
  不需要数字"）不被迫造数字。缺口现在直接给可操作文案。
  最小复现（架构给的）**已转绿**：要求数据与官方来源、正文全"未披露"、来源为空 → 由 `pass` 变为非 pass 并给出缺口。
- ⏳ **R0.2 版本身份真实接线**、**R0.3 已验证交付谓词**：未开始，见下节"实机缺口 ④/⑤"。

**四分类（按《修订》§5 要求）**

- **已实现**：版本/证据记录（`report_version.py`）、导出清单、根预算（`root_budget.py`）、
  评审协议（`review-policy/v1` + 根任务状态）、准入谓词（`admission.py`）、取消四分类与
  可取消的模型等待、阶段观测、按端点额度冷却；前端 ReportViewer/Console 的中文与门户化改动**在源码里**。
- **独立定向通过**（他人可在禁止真实连接下复现）：`test_offline_delivery.TestOfflineFullDelivery`
  3 项、增量验收 4 反例、`test_review_protocol` + `test_admission` 53 项；本机另跑
  `test_report_version` 17 / `test_root_budget` 19 / `test_cancel_semantics` 36 / `test_offline_delivery` 8
  均 EXIT=0（**未跑全套**，不以单测总数替代关卡）。
- **实机缺口**：① `ui-2554df5f9f` 是 SUCCESS_WITH_ISSUES，但两个目标收入数值都未取得、
  来源 URL=0、验收多处占位却 `overall=pass` → **不代表研究目标达成**；② 该任务账本
  `plan=2/step=5`、`tokens_settled=0` → worker 内部请求与费用**未对齐**；③ 该样例当时
  未生成 `export_manifest.json`；④ 配置 0（不限）实际回落 600s（任务①因此提前收尾）；
  ⑤ 取消时票据 `1/1/1` 是"真票据已结算 + 虚构 inflight 未结算"的**假平衡**；
  ⑥ 检索通道对茅台类查询返回无关结果（本轮两个任务失败的直接原因）。
- **未验证**：① **浏览器/UI 未验收**（`dist` 已重建为 `index-yUwXXuD1.js` 并被 8080 提供，
  但未在浏览器逐项验收）；② pending_review 是否经 RAG 泄漏**未被证明**——注册表侧已隔离，
  RAG 侧今天 0 条记录是因为嵌入欠费写入失败，不是隔离生效；③ 主/备同 host，"主备容灾"未做跨供应商验证。

**P0 联调结论（本轮，已记录改动）**

- LLM 检测：主/备探测 ok（1.9s）；三段模型真实最小调用均返回（1.2/2.1/2.2s）；JSON 规划路径可用。
  生效模型：plan/reflect/review → `qwen3.8-max`，exec/judge → `deepseek-flash`；
  `planner.model=qwen3.7-max` 对规划调用不生效（**只记录，不改模型权限**）。
- `system.budget` 已改 `{0,0,0}`（按你的选择）；`frontend/dist` 已重建（新指纹）。
- 三个任务：① 含来源研究 FAILED（验收 fail，数字溯源 0/48）；② 定性研究 FAILED（检索无关结果，0/6 步）；
  ③ 运行中取消 CANCELLED（**停止延迟 1.0s**，账本假平衡见上）。
- 任务①②的"失败"**不是**目标达成证据；每份交付物都带"预算耗尽/评审降级/未验收草稿"注记，
  状态如实降级，未进成功沉淀。

**M0-f 三块（分开做）**

- ① **离线完整交付**（`test_offline_delivery.py`，CI 已挂）：固定官方文档夹具 +
  真实编排链路（模型回包与 worker 结果在请求边界替身）跑完整交付，核对
  页面正文 hash == 记录的交付 hash、Markdown/PDF 各自 hash 且绑定同一 `report_version_id`、
  服务端导出路由送达字节 == 清单登记 hash、验收规则指纹与版本记录一致。
  同文件覆盖"离线不得联网"（`socket.connect` 直接失败）。
- ② **离线故障注入**（同文件）：取消（终态 CANCELLED、在飞转待对账）、评审超时（个人记降级并
  写进交付物 / 银行拒绝且不留已验证交付）、协议错误回包（不得判成功）、迟到结果
  （不覆盖已选中版本、留痕）。
- ③ **实机少量真实任务**（已重启服务加载新代码）：4 次公开研究任务 + 3 次取消验收，
  结果与暴露缺陷见 `docs/实机运行记录_M0f_20260916.md`。

**实机暴露并已修的 6 个缺陷**（每个都带离线回归）：取消无法打断模型调用/健康探测的等待；
预算耗尽只拒绝单次派发导致循环空转；配置里的预算上限不生效；研究类任务从不产生验收；
快速路径/单步任务没有选中版本；未验收草稿只在日志里说（现在写进交付物并如实降级状态）。

**验证**（真实退出码）

- 离线（全部 EXIT=0）：`test_offline_delivery` 8 项、`test_root_budget` 19 项、
  `test_cancel_semantics` 36 项、`test_report_version` 17 项、`test_admission` 29 项、
  `test_review_protocol` 24 项、`test_task_state` 8 项；`test_orchestrator_v2` 68 项、
  `test_delivery_chain` 184 项、`test_p0`、`test_deploy_manifest` 10 项均通过。
- 实机：`ui-2554df5f9f` 验收 pass + 选中版本带自身验收 + 交付 `ok=True` + 5/5 步、
  报告 12.3KB；取消 13s 进 CANCELLED 且被放弃的调用记为 `unsettled`；
  预算账本 `limits` 来自配置且 `reserved==settled`。

**未验证 / 已知缺口**

- **UI 鉴权与浏览器渲染未验证**：本机无有效会话、我没有凭据，也没有新建旁路凭据；
  任务提交走 webui 同一条内部通道，页面只核对数据一致性。
- 模型端点本轮持续退化（504/读超时/推理预算耗尽）：happy path 仅第 4 次跑通，
  不据此宣称稳定；应用内定时任务与实跑并发争用同一端点，耗时有干扰。
- 取消延迟 13s（>预期 3s）：取消信号还未传进 `llm_client` 的每次重试间隙。
- Embedding 欠费（402）：待补录队列在用，策略沉淀本轮因准入拒绝而未发生。
- 实跑为有界写入了 `system.budget={900s,60 次,0}`；是否保留由你决定（默认 0/0/0＝不限）。

**M0-a…e 摘要（已完成）**

- **a 版本与证据绑定**：单一权威版本记录（正文全量 SHA256 + 来源/规则指纹 + 该版自身验收）、
  唯一采纳点、恢复校验归属与 hash、收尾交付一致性与最终交付 hash、导出清单逐格式各自 hash。
- **b 评审协议**：非法身份模式不降级、裁决白名单（仅 PASS/FAIL）、修订稿需复评取得绑定 PASS、
  评审状态按根任务隔离、critic 关闭/直出/模板/恢复共用策略、降级写进交付物与 `review_state.json`。
- **c 取消/超时/协议**：等待四分类（result/cancel/timeout/protocol）、协议错误不冒充超时、
  取消覆盖验收前与三处人工确认、取消为持久终态、在飞转待对账。
- **d 经验与模板准入**：`admission.admit_success` 显式谓词（默认拒绝）三处共用；
  未验证策略标 `needs_review`、自迭代产出写 `pending_review` 不生效；统计只认 `verified`、同任务只计一次。
- **e 根任务预算与阶段观测**：一次任务一份账（原子落盘、恢复不重置）、先预留后发送、失败也结算、
  取消转待对账；规划与派发在发送前卡预算；阶段观测带等待对象/进展/剩余预算；
  额度冷却按端点生效（健康主端点不被欠费备用冻住）。

# 历史批次（V1 反例批次，2026-09-16 上半场）

**当时问题**：按《架构复核与纠偏_20260916》第 0 节修 V1 的三个实际回归（不放宽阈值）。

**本批修改**（工作区，**未提交**）

V1 三个反例（第 0 节）：

- **0.2 分来源通道**（`acceptance_checker`）：`clean_chart_data` 的结构化 JSON 与 `user_material` 文本不再拼成一段；
  派生判定改用"结构化文本 / 用户材料文本"各自通道，公式核验改用**结构化记录 + 用户材料里带单位的数字**
  组成的记录列表。修前：仅加入 `user_material` 文本就把原本 pass/1.0 的同比计算判成 fail/0.0。
- **0.3 完整公式 + 单位 + token 边界**（`_formula_derived_in_report` + `_unit_profile` + `_collect_source_records`）：
  表达式必须是紧邻数字的**完整括号算式**（删掉"截取局部二元式"的兜底）、操作数必须**按值**命中来源记录
  （不再用子串，`12` 不能命中 `1200`）、操作数之间量纲一致且与结果量纲/缩放相容（挡住 万元 与 亿元 混用）；
  换算常量（1/2/100）放行，其余缺输入即拒绝。指标/期间语义**尚未绑定**，未实现部分保持"未核实"。

**验证**（真实退出码，全部离线）

- 复核给的三条反例现在**全部拒绝**：`120%（1200/1000-1）` computed=0（值不符）、`2200亿元（1200+1000）` computed=0
  （量纲错一万倍）、`利润2亿元（12-10）` coverage=0.0 computed=0（缺输入+子串命中已被边界挡住）；
  正向对照 `20%（1200/1000-1）` 仍 **coverage 1.0 / computed 1**（没有靠收紧阈值把真话否掉）。
- 结构化通道不再被文本破坏：同一份报告在"仅 clean"与"clean+user_material"两种输入下判定一致（都 pass/1.0）。
- `test_report_quality` **22 项 EXIT=0**（新增 5 项反例/对照）；`test_acceptance_adversarial` EXIT=0、
  `test_deploy_manifest` EXIT=0。

**尚未修（按复核顺序，下一步）**

- **0.1 版本绑定**：`orchestrator_v2` 仍把同一份最新 acceptance 传给旧稿与候选稿（3746-3748、4048-4050），
  4209 用旧 best_report、4232 读最新验收 → 交付内容与元数据可能错配。要做的是把正文/来源/验收/指纹
  绑成**不可混用的版本记录**，修订比较与导出引用同一版本（不能只改 `compare_versions` 排序）。
- **第 1 节**：未知/缺失/非法评审裁决在银行口径下仍被放行（只有 ERROR/DEGRADED 被拦）；
  `_review_is_required` 吞掉 `default_mode()` 的配置错误 → 非法 mode 默认按个人模式；
  取消导致的等待返回被上层当作超时；`system.critic` 关闭/模板/恢复入口未覆盖。
- **缺验收/有缺口不得计成功经验或 verified 模板**（记忆与模板沉淀需按验收结果设闸）。
- B/C/D/E/F 连续实跑按复核要求**暂缓**；先离线定向回归、再一次有界端到端。

**事实更正**：B `ui-4cf6e482e9` 已于 **01:05:36 以 SUCCESS_WITH_ISSUES 结束（1697 秒，acceptance_json 为空）**，
当前 RUNNING=0；我此前"仍在卡死"的判断已过时，不再据此重启或断言死锁。它只作为"有缺口/失败样例"，
其财务数字不得当作事实引用。

**保护项**：门禁原样（命中留证、走复核）；模型权限未动；`templates.json` 指纹未变；配置中只写入了你提供的新 Key
（`llm`/`planner`/`backup` 三处，`config.json` 已在版本库忽略，未写入任何源码/示例/测试）。

# 历史批次（V2-1/V2-2/V1.1/V1.2/L01 网络通道）

**当前问题**：V2-2 评审退化（超时/异常不得当通过）；V2-1 取消语义与端到端验证待续。

**本批修改**（工作区，**未提交**）

V2-2 评审退化（本轮）：

- `_review_plan`：超时/异常/`ERROR`/`DEGRADED` 四种"没评上"的情形统一走 `_review_unavailable`——
  **个人模式**标记 `_review_degraded`（如实标注"未完成评审（降级）"，计划按草案继续，交付物注明需人工复核）；
  **银行口径**（`WEAVEMIND_IDENTITY_MODE=bank`）抛 `ReviewRequiredError` **拒绝继续**，不再把超时当通过。
- `verdict=ERROR`（Critic 身份/协议拒绝）与 `DEGRADED` 不再掉进"修订"分支（那里会多花一次 LLM 并掩盖"没评上"）。
- `critic_agent._fallback_review`：不再返回 `PASS`，改为 `DEGRADED` + 原因（"评审系统不可用，未完成评审——不得视为通过"）。

**执行过的测试与结果**（真实退出码）

- 新增 `test_review_degradation` **10 项 EXIT=0**：个人模式超时/异常标降级且不修订、银行模式超时/异常拒绝、
  ERROR 与 DEGRADED 不进修订分支、PASS/FAIL 行为不变（FAIL 仍触发一次修订）、回退评审非 PASS
- `test_deploy_manifest` EXIT=0、`test_prompt_system` 72s EXIT=0
- ⚠️ **`test_orchestrator_v2` 首次复跑超时：EXIT=124（900s 上限，此前 ~217s）**——属**未查明**的回归信号。
  已做一次防御性修正（`_plan` 里改为 `getattr(self, "_review_degraded", "")`，避免测试用 `__new__`
  构造的实例触发 AttributeError 在重试循环里拖死），并已重跑该套件，结果见下条；**在套件通过前不宣称 V2-2 已验证**。

**尚未验证**：8080 未重启，取消语义与评审退化的端到端行为（真实停止、真实 Critic 超时）需一次实机操作；
`_review_plan` 之外（反思 `3809`、评测闸门 `3712`、收尾阶段 `4175/4198/4241`）仍无取消前置检查。

**本批修改**（工作区，**未提交**）

V2-1 取消语义（本轮）：

- **独立终态**：`task_state.CANCELLED` 加入终态词表（`TERMINAL` 四值）；`_finish_cancelled` 改写 `CANCELLED`
  （此前硬编码 `FAILED`），SSE `task_complete`、落库、通知一致；`web_ui` 取消接口的终态集合补 `CANCELLED`。
  前端 `statusMeta` 早已映射「已取消」，此前是**死代码**。
- **派发取消闸门**：`_dispatch` 在入队（`lpush`）**之前**统一判定取消，覆盖所有派发路径（首次派发、重做链、
  任务级修复、ReAct 降级重派、重试、重规划后派发）——取消后不再发起新步骤与新付费调用。
- **等待提前返回**：`_wait_for_result` 增加 `cancel_task_id`，按 1 秒切片检查取消并立即返回——这正是实测
  "停止后仍等 ~92 秒"的那条路径（此前只在派发前挡新步骤）。

**执行过的测试与结果**（真实退出码）

- 新增 `test_cancel_semantics` **11 项 EXIT=0**：取消是独立终态、`_finish_cancelled` 写 CANCELLED 且清标志、
  SSE 事件携带 CANCELLED、**闸门位于入队之前**、`_cancel_requested` 读标志、等待循环**取消后 <5s 返回**
  （对照：无取消时正常拿结果、不传新参数行为不变）、取消接口终态集合含 CANCELLED、指标可计数、前端映射存在
- 受影响用例按新语义更新并注明原因：`test_writer_consolidation.test_finish_cancelled_finalizes_and_clears`
  由断言 `FAILED` 改为 `CANCELLED`（EXIT=0）
- 相关回归：`test_deploy_manifest` EXIT=0、`test_task_state` EXIT=0；新测试已接进 CI

**V2-1 尚未完成**（下一轮）：收尾阶段（验收 `4533`、Memory 固化 `4175`、模板沉淀 `4198`、后台提示词自迭代
线程 `4241`）与反思/评测闸门（`3809`/`3712`）仍无取消前置检查；无超时 `t.join()`（`4819-4820`）未显式加界
（当前靠等待提前返回来收敛）；人工确认等待（`4323/4380`，上限 600s）取消后不会立即结束。
**端到端未验证**：8080 服务按要求未重启，取消语义需一次真实"运行中停止"确认卡片显示"已取消"与收尾耗时——单测不能替代。

## 历史批次（V1 / L01 / 门禁）

**本批修改**（工作区，**未提交**）

V1.1 修订稿选择（不再以长短代替质量）：

- 新增 `report_quality.py`：`compare_versions()` 按**硬约束→验收→实质改进**排序——
  验收等级（pass>未知>fail）→ 验收缺口数 → 占位/未完成痕迹 → 是否重复用户需求块；
  全部相同则**保持当前版本**，并在原因里写明"长度差异不作为改进依据"。
- `orchestrator_v2.py` 两处（初稿轮次 3649、反思重做后 3947）都改为调用它，替换
  `len(cand) > len(best_report)`；日志改为输出**可解释的比较原因**，不再只说字符数。

V1.2 来源语义（用户材料是来源，但真实性未核实）：

- `acceptance_checker.py`：`run_acceptance` 把**用户材料**（任务目标/指令）注为独立来源通道；
  数字命中它记为 `source=user_material` + `user_provided=True` + `verified=False`（**不**算模型知识、
  **不**算不可溯源），并新增 `user_input_count` 计数；派生判定改用"清洗数据 + 用户材料"，
  再对**报告内写明的公式**做核验（`_eval_arith_expression` 用 AST 白名单求值，
  **不使用动态执行入口**——报告文本是不可信输入），要求公式的所有操作数都能在来源中找到。
- 阈值与判定强度**未放宽**：外部无证据数字仍失败、无输入的计算仍不可溯源。

**执行过的测试与结果**（真实退出码）

- 新增 `test_report_quality`（离线固定样例，不用付费 LLM）**11 项 EXIT=0**：更短纠错稿被接受、
  更长错误稿被拒绝、仅变长不算改进、验收等级压过长度、用户材料可溯源且标"未核实"、
  混合来源都可溯源、写明公式的派生值可溯源、未写公式的仍不可溯源、缺输入的计算仍被拒、
  外部无证据数字仍失败
- `test_deploy_manifest` / `test_acceptance_adversarial` / `test_p0`：本轮相关回归（见下条追加）

**门禁**：本轮写时扫描拦下一次**真问题**——我最初用动态求值算报告里的算式，被"代码注入"规则拦下，
判定正确，已改为 AST 白名单求值。另：先前那次 `execute` 命名与注释字面量误报仍记录在
`docs/门禁命中证据与复核请求.md` 第三节。未停用钩子、未换终端绕过。

V1.2b 免责声明按来源生成（本轮新增）：

- `report_quality.py` 新增 `disclaimer_clause(has_external, has_user_material)` 与 `disclaimer_instruction()`：
  三种措辞（只用用户材料 / 只用外部检索 / 两者都有）+ 选择规则，并明确"没有外部检索禁止写
  『数据来源于公开渠道』""用户材料必须写明真实性未经独立核实、不得改称模型知识、不得伪造来源"。
- `orchestrator_v2._REPORT_FORMAT_REQUIREMENTS` 里那条**写死的**"数据来源于公开渠道"已替换为
  `disclaimer_instruction()` 注入（A/C 这类以用户材料为来源的任务不再被强行宣称公开渠道）。

**执行过的测试与结果**（真实退出码）

- `test_report_quality` **17 项 EXIT=0**（11 → 17）：新增免责声明 6 项——只用用户材料不得出现"公开渠道"、
  只有外部来源保留公开渠道措辞、混合来源分别说明、无来源时说明是模型知识、注入要求含三种措辞与禁止条款、
  编排器不得再写死固定免责声明；以及修订稿选择 4 项与来源语义 7 项
- `test_deploy_manifest` EXIT=0、`test_acceptance_adversarial` EXIT=0、`test_p0` EXIT=0

**尚未做**（V1 余项，位置已定位，留给下一批）：

- **跨任务经验相关性/约束优先级过滤**：教训经 `orchestrator_v2._inject_memory_context(goal)`
  → `memory_manager.inject_context(goal)` 进入规划提示词（`:641` 以 "Relevant past experience" 拼入）。
  实测 B 被注入 A 的"不联网、虚构数据"约束，与 B 的官方检索目标冲突。做法：按当前目标过滤
  "约束类教训"（联网/虚构/离线/不执行代码 等），不相关的不注入或降级为"历史做法、未必适用"，
  且不得覆盖本次任务的显式约束；需要独立测试（相关/不相关/显式约束优先）。
- 正文长度策略显式化；页面/Markdown/PDF 同版本的端到端断言。
- `llm_client`/`lora_client`/`mcp_client`/embedding/webhook 仍未接入登记端点（L01 未完成项）。

**下一步**：V2 预算/评审退化/取消 → V3 PDF 排版 → V4 中文进度；银行认证未接入，**不宣称银行可用**。

**本批修改**（工作区，**未提交**）

L01 协议入口收口（依 17:38 复核补充，不只针对例子打补丁）：

1. **模式校验统一**：`resolve_mode(mode)` 让**显式传参与环境变量走同一条规则**——`admit_dispatch(..., mode="bnak")`
   与环境变量写成 `bnak` 得到同样拒绝（`IdentityConfigError`），不再因为"显式传参"跳过校验。
2. **只有缺 `context` 键才算旧消息**：显式 `context: null` 属协议非法，直接拒绝；旧消息走独立的本地兼容策略。
3. **v1 版本号仅接受 JSON 整数**：布尔（`True`/`False` 是 int 子类）、小数 `1.9`、字符串 `"1"`、`null`、数组、对象
   一律拒绝，不做 `int()` 转换；缺失版本键也拒绝；支持版本忽略新增可选字段。
4. **兼容缺口可观测**：`admit_dispatch` 返回 `(ctx, gaps, refuse_reason)`，三个接收点（两个 Worker + Critic）
   都记 warning 日志，Worker 还把 `context_gaps` 随结果回传——不再像以前那样在内部丢掉。

L01 此前四项（上一批，已接受）：Critic 先校验后评审（银行缺身份零调用 + 明确 ERROR）、清理进 `finally`、
心跳子线程 `copy_context`、Worker/Critic 共用 `admit_dispatch` 边界。

**执行过的测试与结果**（真实退出码）

- `test_task_context` **43 项 EXIT=0**（30 → 43）：新增边界矩阵 9 项（显式模式校验、`context: null`、
  版本类型矩阵、缺版本键、银行拒绝、兼容缺口返回）+ 真实 Worker 接收边界 4 项
  （调用基类真实 `_handle`：银行拒绝 → **零执行** + FAILED 回传 + 不残留身份；坏版本 → 零执行；
  `context: null` → 零执行；本地旧消息照常执行且回传 `context_gaps`）
- 相关回归：`test_deploy_manifest` EXIT=0、`test_p0` 128s **EXIT=0**、`test_orchestrator_v2` 202s **EXIT=0**

**门禁**：写时扫描第二次 `execute` 误报（测试替身里的协议方法名）已在
`docs/门禁命中证据与复核请求.md` 第三节如实记录（含取舍说明）；未改产品代码、未停用钩子、未换终端绕过。

**本批修改**（工作区，**未提交**）

网络通道（依《身份与网络边界》决策，新增 `net_policy.py` + `test_net_policy.py`）：

- 两类接口互不重叠：`request_service(endpoint_id, operation)` 读 `config.json` 的 `network.endpoints`
  登记表（用途/scheme/host/port/允许操作/`loopback` 标记），**调用方自报 `trusted`/`allow_private` 一律拒绝**；
  `fetch_document(url)` 走内容派生通道——仅 http/https 公网、拒绝环回/私网/链路本地/共享(`100.64.0.0/10`)/
  保留/组播/未指定/**IPv4+IPv6 映射地址**、拒绝 URL 用户凭据、**解析失败即拒绝**。
- 校验与连接同源：抓取用**已验 IP** 连接、TLS 保留 SNI 与系统 CA 校验、**不跟随重定向**、不带调用方凭据、
  有超时与响应体上限；审计只记脱敏目标与策略判定（`audit_logger`）。
- `adapters/transport._validate_public_url` 收敛为委托共享策略，修掉旧实现"DNS 解析失败也放行"、
  缺 `100.64/8`、缺映射地址判定三处（现有抓取点因此立即变为严格）。
- 真实缺陷：`workers/data_loader_worker.py` 的下载改走 `fetch_document`（内容派生 SSRF），
  文件名改为 `Path(name).name` + 落盘前 `resolve()` 边界校验（Windows 反斜杠穿越），失败如实回报。
- 配置面：`config.example.json` 新增 `network.endpoints` 示例（含登记的本机模型），`settings_schema` 同步。

L01 协议入口（上一批，已接受）：模式校验统一（显式 mode 与环境同规则）、只有缺 `context` 键才算旧消息、
v1 只接受非布尔整数、兼容缺口可由调用方记录与回传。

**执行过的测试与结果**（真实退出码）

- `test_net_policy` **22 项 EXIT=0**：决策的最小验收集合全覆盖——登记本地模型允许、同一地址经内容抓取拒绝、
  未登记私网拒绝、公网允许、DNS 失败/公网+私网混合/映射地址/`100.64.0.1`/元数据主机/用户凭据/协议 全部拒绝、
  重定向不跟随、跨源不携带凭据、**连接使用已验 IP**（防重绑定）、体量上限、未登记/私网目标不触达传输层、
  旧校验器委托后行为一致；另含 data_loader 接线 2 项（内网地址拒绝且不落盘、穿越文件名被夹在工作区内）
- `test_task_context` **43 项 EXIT=0**；相关回归：`test_settings_requirements` 26 项 EXIT=0（新配置段已进设置清单）、
  `test_deploy_manifest` EXIT=0、`test_p0` 126s **EXIT=0**、`test_delivery_chain` 132s **EXIT=0**

**门禁**：写时扫描第三次误报（注释里出现"关闭证书校验"的字面量被当作真的关闭校验）已记入
`docs/门禁命中证据与复核请求.md` 第三节；实现用的是系统 CA 校验。未改产品语义、未停用钩子、未换终端绕过。

**下一步**：把 `llm_client`/`lora_client`/`mcp_client`/embedding/webhook 的地址解析接到登记端点
（当前它们仍直接用配置里的 base_url，未走 `request_service`）；随后 L02 全链预算、事实链。
银行认证与可信身份仍未接入，**不宣称银行可用**。

**尚未验证**（按决策显式保留）：编排器仍未从可信认证/会话链填 `tenant/workspace/actor` → **银行能力未就绪**，
不从提示词/前端自报/默认公共租户补值；`deadline` 与 `step_deadline` 的同步规则未定；预算强制执行属 L02。

**下一步**：按决策实现网络通道（`request_service` 登记端点 / `fetch_document` 内容抓取；修
`_validate_public_url` 的 DNS 失败放行与 `100.64.0.0/10`、校验与连接同源解析），再做 L02 与事实链。
门禁保持原样，不换终端绕过、不为减命中改写代码。

# 历史记录（2026-09-15 之前）

**该批修改**（提交 `d6505ef` / `c600943` / `de2a825` / `8cbee18` / `ee99dd0` / `ebfad59` / `e72378a` / `eea6f0f` / `ab740b5`）

## 一、CI 红灯定位与修复（提交 `fix(CI)`）

1. **`docker-image` 作业 "Build image" 失败**
   - 根因：`Dockerfile` 有 `COPY prompts/ ./prompts/`，但 `prompts/` 在版本库里**零文件**（只装 gitignore 的
     `overrides.json` / `*.bak`）。本地目录还在，所以怎么跑都看不出问题；CI 干净检出后该目录不存在，
     `COPY` 直接以 `not found` 失败。本地反复构建失败于 Docker Hub 不可达（`auth.docker.io` 超时），
     一直没能走到这一步。
   - 修复：删除该行。override 属运行时可写状态——容器里落 `/data/prompts`（`WEAVEMIND_DATA_DIR`），
     `prompt_registry` 写入时 `mkdir(parents=True, exist_ok=True)`，缺失即空覆盖，镜像不需要这份空目录。
   - 守卫：`test_deploy_manifest.py` 新增 `test_copy_sources_exist_in_clean_checkout`（每个 COPY 源必须在
     git 索引里）；并用历史那一行自证有齿（喂 `COPY prompts/` → 报红）。同时把 `prompts` 从
     `REQUIRED_RESOURCES` 移除——它此前把该 bug 固化成了"必须拷进镜像"。
2. **`backend` 作业 "Settings requirements tests" 失败**
   - 根因：`test_all_config_sections_covered_or_declared` 直接打开本机 `config.json`（已 gitignore）→
     CI 无此文件，抛 `FileNotFoundError`。
   - 修复：优先 `config.json`、回退 `config.example.json`，两者都没有才 fail。已按"无 config.json"的
     CI 条件本地模拟验证通过。

## 二、同家族缺陷收口（提交 `fix(Redis)`）

冷启动竞态修的是 `common.MessagingClient`，但**同一写法还有 8 处**：`adapters/quote_cache`、
`adapters/source_health`、`lora_client`、`metrics_collector`、`orchestrator_v2._new_redis_sync`、
`tool_dispatch`、`smoke_test`、`verification_suite` 构造 `redis.Redis(...)` 时未关内建重试（后两处连超时都没有）。

- 实测代价：`/api/config/requirements` → `health_registry._redis_get` 在 Redis 不可达时单次要 26~48 秒才失败，
  设置页/健康页表现为长时间卡死；本地跑 `test_settings_requirements` 的 `TestConfigEndpoints` /
  `TestHealthRegistryProbes` 直接挂住（现在分别 26s / 42s 跑完，EXIT=0）。
- 修复：8 处全部显式传 `retry`（复用 `common._NO_REDIS_RETRY` 或文件内同款常量）；`smoke_test` /
  `verification_suite` 补 2 秒连接与读超时。
- 守卫：`test_startup_readiness.py` 新增 `TestNoUnretriedRedisClients`——AST 扫全仓，任何未传 `retry=` 的
  同步客户端（`aioredis` 除外）报红，并带检测器自证。
- 口径更正：`MessagingClient` 连不通实测 **7.6 秒**抛出（redis-py 内建重试已关，耗时来自
  `MessagingClient._connect` 自己的 3 次退避——那是应用层策略，不是 redis-py 的重试风暴）。

## 三、执行过的测试（本地 Windows / Python 3.14，真实退出码）

| 测试文件 | 结果 |
| --- | --- |
| `test_deploy_manifest` | 10 项 EXIT=0（含新增检出等价性守卫） |
| `test_startup_readiness` | 7 项 EXIT=0（含新增全仓重试守卫） |
| `test_settings_requirements` | EXIT=0（修复前 `TestConfigEndpoints`/`TestHealthRegistryProbes` 挂死） |
| `test_metrics_scope` | EXIT=0 |
| `test_delivery_chain` | EXIT=0 |
| `test_lora_manager` / `test_isolation_scope` | EXIT=0 |

## 四、F3a 报告页完整升级（已落地并实测）

六项全部接线（`frontend/src/components/ReportViewer.tsx`，不动后端契约）：

| 项 | 内容 |
| --- | --- |
| 结论卡（置顶） | 正文首表前 4 行，列与截断口径与分享页 `_share_page_structured` 一致；无表时降级显示验收器统计并标注"非报告结论" |
| 数字可信度卡 | 四档计数 + 数字溯源率 + **金额溯源率单列** + 不可溯源清单（最多 10 条，与后端截断一致）+ 口径/阈值/域 + 达阈值判定 |
| 图表 | 图注（title/alt）、点击放大（Esc 与遮罩关闭）、单图下载、"草稿级"标签（"补充图表（未达发布标准）"章节下） |
| 证据指纹 | 规则版本 / 规则指纹 / 报告 SHA256 / 评估时间，附"为何留指纹"说明 |
| 验收时间线 | `/api/task/<id>/acceptance/timeline`：每轮 trigger / overall / 缺口数 / 时间 / 报告指纹前 8 位 |
| 导出区收敛 | 分"报告文件""复核与分享"两组；交付包 zip 标注未提供的原因（`/files` 只放行 reports\|charts\|data） |

**浏览器实测**（新实例真跑一个财务任务 `ui-02ef5f30f1`，SUCCESS）：

- 可信度卡 **与 `acceptance_report.json` 逐项一致**：共 27 个 / 引用 25 / 计算 0 / 模型知识 2 / 不可溯源 0、
  数字溯源率 93%（25/27）、金额溯源率 100%（25/25）、阈值 70%、域=财务、判定"达阈值"
- 指纹卡与后端一致：`2026.09.12` / `2ddecc9a` / `b40ff4b1e89ee23e` / `2026-09-15T03:11:55+00:00`；
  时间线 1 条（报告步骤 / pass / 缺口 0 / 同一指纹）
- 图表：3 张渲染，`src` 为后端改写后的 `/files/ui-02ef5f30f1/charts/*.png`；放大浮层弹出、**真实按键 Esc 关闭**；
  单图下载取数 `200 image/png 26122 bytes`
- 实例 `index.html` 与本地产物哈希一致（确认实测的就是本批构建）

**顺带修掉一个既有缺陷（非本批引入）**：报告容器带 `animate-fade-in`（`will-change: transform`）会为
`fixed` 后代新建包含块 → 分享对话框浮层被拉到报告全高（实测 **6388px、top=-5693**，用户根本看不到，
无法设密码/生成分享链接）。新增的图表浮层同样会中招。两处改为 `createPortal(document.body)`；
修后浮层高度=视口高度、两个按钮均落在视口内（实测）。

**未触发的条件渲染**（本样本无对应数据，待有样本再验）：未溯源清单（该任务 `unverifiable_count=0`）、
"草稿级"标签（该报告无"补充图表（未达发布标准）"章节）。

## 五、F3b 交互与移动端（第一批已落地，实测通过）

已落地：

- **窄屏底栏**：10 项平铺改为 4 个高频页 + 「更多」抽屉；抽屉内含其余 6 个页面、演示开关、退出登录。
  此前 <640px 时演示开关与退出登录全部不可达（两者都带 `hidden sm:flex` / `hidden md:flex`）。
- **StepInspector**：字段中文化（步骤详情/能力/执行体/指令/执行结果/决策轨迹）；结果改为白名单字段 + 中文名，
  对象/数组只进折叠区；**本机路径脱敏**（保留末两段，仍可定位文件）；原始 JSON 默认折叠；小屏全屏（`w-full sm:w-96`）。
- **重跑二次确认**：说明"重新规划并消耗额度"，给「确认重跑 / 改为修改目标 / 取消」三个出口
  （"复用原计划"需后端支持，对话框内如实说明；「改为修改目标」把目标填回提交框并滚回提交区）。
- **演示模式置灰扫尾**：Agents 直发（输入框 + 发送）与终止、Settings 通知保存、定时任务启停/删除/新增、
  用户角色/改密/删除/新增——全部 `disabled` + 原因 title，不再"点了才被 403 拦"。
- **实时通道降级提示**：SSE 断开回退轮询时顶部提示"实时通道已降级为轮询（每 2 秒拉一次）"，并说明任务不受影响。
- **响应式**：报告页统计条、Agents 统计、Health 五列、Evals 四列改为断点网格（窄屏 2 列）。

实测（375×812 与 1280×720，实例真机）：

- 底栏 5 项、`scrollWidth - innerWidth = 0`（无横向溢出）；「更多」抽屉在视口内（挂 body、面板高 303px），
  含演示开关与退出登录；**手机端开启演示成功**（横幅出现、底栏标签变「更多 · 演示」），关闭后恢复
- 测试手段说明：`onToggleDemo` 用**同步** `window.confirm` 确认，自动化点击会被原生弹窗阻塞（实测 32s 超时），
  验证时先在页面里桩掉 `confirm`——只影响测试会话，未改产品代码

**第二批（时长提示，已落地）**：

- 提交区显示"近 N 次任务平均 X 分钟"（取 `/api/status` 里已完成任务的 created/completed 差值，
  样本为 0 时明说"预计时长未知（暂无已完成任务样本）"，不给假预期）；运行中显示"已运行 X 分钟"（每 30s 走一次）
- 实测：实例上该文案为"近 1 次任务平均 23 分钟"，与该任务实际耗时（03:08 → 03:31）一致

**仍未完成（留作 F3b 第三批）**：

- 三态分离：约 29 处静默 `catch {}` 尚未逐页改为"错误态 + 重试"（优先 History / TaskConsole / Memory /
  ReportViewer / useTaskLive）。逐页都要补错误态 UI，批量改容易引入回归，单独一批做

## 六、F4 守卫固化（已落地）

`test_frontend_guards.py` 由 13 项扩到 **27 项**（该文件已在 CI 内，由 `test_deploy_manifest` 的
"每个 test 文件都进 CI"断言兜底），新增守卫：

- **浮层必须走 `createPortal`**：并断言"`fixed inset-0` 数量 ≤ `createPortal(` 数量"——正是 F3a 实测到的
  6388px 浮层那个坑。已用合成反例自证有齿（未挂 portal / 未引入 createPortal 两种写法都报红）
- **报告页接线**：`covered_ratio`、`amount_rate`、`untraceable`、`unverifiable_count`、四项指纹字段、
  `/acceptance/timeline`、结论卡口径（键 24 / 值 32 截断）、图表三件套（草稿级 / Esc / 下载图片）
- **窄屏入口**：底栏 4+更多 收敛标记、抽屉含「演示模式」「退出登录」
- **步骤详情**：`redactPaths` 不仅定义还要在实际渲染路径上用（≥4 处）、原始 JSON 默认不展开、
  中文标签齐全、不得残留英文标签
- **重跑必须确认**：且不得退回 `onClick={() => onSubmit(m.goal)}` 这种直接提交

未纳入（依赖 F3b 第二批）："错误态 ≠ 空态"的断言。

## 七、阻塞与待验证

- **`docker-image` 作业本轮无本地复现**：本机到 `auth.docker.io` 超时（Docker Hub 不可达），镜像构建
  走不到 COPY 那步；修复依据是"COPY 源与干净检出不等价"这一确凿事实，最终确认只能靠 CI 复跑。
- **推送需要一次性凭据（已定位，不是代理问题）**：代理 `http://localhost:7897` 正常——`curl` 走代理访问
  `github.com` 首页与 `git-receive-pack` 端点均在 0.7~1.4 秒内返回（未带凭据的 401）；`git ls-remote` 正常。
  阻塞点在凭据：`git credential fill`（配 `GCM_INTERACTIVE=never`）返回"无缓存凭据"，而仓库内
  `credential.helper=manager`（GCM）在无 TTY 环境下转为等待交互式登录 → push 挂住直到超时。
  本机也没有 `gh` CLI、没有 `~/.git-credentials`、环境变量里没有令牌。
  补齐一次 GitHub 凭据（或由本机人工执行一次 push）即可；本地现有 4 个提交待推：
  `8cbee18`（冷启动竞态）、`ee99dd0`（CI 两条红灯）、`ebfad59`（Redis 重试收口）、`6e3799e`（门禁记录）。
- **CI 尚未复跑**：上述 4 个提交未推送，`docker-image` 作业的修复只能等推送后有 CI 结果才能确认。
- 未修（已排序）：交付包内 `index.html` 编码缺陷已检出未拦截、图表分级未覆盖自动生成图、
  美股结构化财务链路未生效（金额溯源 0%）、便携 Redis 默认源为 GitHub（无代理会卡住）。

---

# 历史记录（2026-09-14）

**当时问题**：代码沙箱安全默认值——未配置 / 取值非法 / 隔离不可用都必须拒绝执行，不得回退宿主。

**该批修改**（提交 `d6505ef`）

- `code_sandbox.py`：默认（未设置）即按 docker 策略；删除 FALLBACK 开关；非法取值抛 `SandboxConfigError` 并拒绝；`sandbox_status()` 区分 `isolation_ready`（restricted/none 恒 false）与 `execution_available`；拒绝提示只给恢复隔离的出路，不诱导关闭隔离
- `workers/code_execution_worker.py`：`_run_smoke` / `execute` 遇设施错误原样上抛并保留根因，不进入模型重生成循环（新增 `code_sandbox.is_facility_error`）
- `health_registry.py` / `web_ui.py` / `launcher.py`：健康项以隔离为判据、同时报"能否执行"；启动日志说明隔离状态但不阻断启动
- `docker-compose.yml` / `docs/部署指南.md` / `README(.en).md`：删除"挂 socket 即真隔离"的未验证承诺；容器内代码执行不可用、工作台其余功能照常
- 测试：新增 `test_sandbox_isolation.py`（24 项）；`test_p0` 沙箱类按新语义更新；`test_delivery_chain` 执行流水线用例显式选开发模式（未删用例、未全局设值）

**当时执行过的测试**：`test_p0` 387 项、`test_sandbox_isolation` 24 项、`test_delivery_chain` 171 项（排除网络类）及其余 10 个文件均 EXIT=0；mock 故障分支一律拒绝、宿主进程创建次数 0。

**新环境实测补充（`docs/新手实测报告_20260914.md`）**

- 执行隔离**无直接证据**（原表述已更正）：`grade=publish` 是图表**质检等级**，`charts_pipeline` 内置渲染走宿主 `subprocess.run`，不能据此推断容器执行
- **P0 前端缺陷已修（批 F1）**：登录/创建管理员/退出白屏（`React #300/#310`）→ `App.tsx` 去掉早退后的 `useMemo` + `ErrorBoundary` 覆盖两条分支；同批演示模式不再发真实请求、首屏状态改"连接中"、指标页区分进行中/已完成
- **F1 补修（架构审查四项）**：① 指标改为后端同范围同快照聚合（`test_metrics_scope.py` 7 项）；② 演示模式改为 fetch 层统一拦截写/付费操作；③ `grade=publish` 不能证明容器隔离，报告已更正为待验证；④ 门禁命中与恢复证据见 `docs/门禁处理记录.md`
- **冷启动竞态（P0，已修）**：新实例首次启动 15/16、编排器静默死掉；faulthandler 抓到栈落在 `redis/retry.py call_with_retry`。修复=关内建重试 + 启动器拉服务前等一次成功 PING，细则见报告 §2.5
