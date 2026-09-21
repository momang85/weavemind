# D5 退出：有界实机运行、账本逐条核对与三处缺口修复（附 D6 回归包 + 安全核对）

2026-09-21。承接 D5 第一批（`docs/evidence/d5_request_ledger_and_caps_20260921.md`）。
范围：一次**有界实机**（声明上限 40 次调用 / 1500 秒）、运行后账本逐条核对、核对暴露的
三处缺口修复与离线验证、D6 回归包补口、三处对外可达面的只读安全核对。

## Part A · 有界实机运行（一次，不重试、不扩预算）

- 启动声明上限：`WM_TASK_MAX_CALLS=40 WM_TASK_MAX_SECONDS=1500`（不改全局 `0/0/0`）。
  上限已入账：`budget_state.json` 的 `limits = {max_seconds: 1500, max_calls: 40, max_tokens: 0}`。
- 任务：`ui-750185076a`，洋河股份 `002304.SZ`，2023–2024 两年度三项指标，合并口径，
  资料截至 2025-04-30，`bank_corporate` 视角。
- 时间线（本地）：21:05:02 提交 → 21:05:06 结构化财务载入（2 个年度）→ 21:05:39 检索
  （10 条结果中 4 条死链被丢弃）→ 21:05:53 图表规格 6 张 → 21:06:11–21:07:26 装配/验收/反思
  （验收对象为代码装配候选稿，四次验收 `overall=pass`）→ 21:07:28 `status=verified` →
  21:07:29 任务终态 `SUCCESS`。**墙钟 2 分 27 秒**，远低于 25 分钟上限；**未触发任何预算拒绝**。
- 交付读数：验收 `overall=pass`（`number_traceability` 77%（164/212）≥70% 阈值、
  `entity_attribution`/`source_labeling`/`deliverable_completeness` 等全过、`gaps=[]`）；
  主张 40 条（`bound` 7 / `partially_supported` 7 / `needs_check` 26）；研究问题 4 条已渲染。
- 费用：**未知**（本次未取到供应商实际计费口径，账本只记调用次数与 token 上界）。

### A1 叙事证据：本次**没有取得任何带定位证据**（如实记缺口，未编内容）
`narrative_evidence.json`：`located=0`、四类（业务背景/经营变化解释/财务附注/风险因素）
全缺、`snippet_hints=0`；3 份抓取材料里：
- 东方财富财务接口 JSON（317 KB）→ 数字来源（结构化通道）；
- 新浪 2026-04-28《洋河股份 2025 年报解读》→ **按契约排除**（`after_as_of` 且
  `document_period=2025`，晚于资料截至 2025-04-30）；
- 上交所债券公告 **PDF**（`%PDF-1.5`）→ 以原始字节进快照（30 000 字符上限内），**未转文本**。

**发现（未在本批修复，需单独复现）**：该 PDF 由通用抓取路径取回，**没有**走 PDF 证据通道
（`orchestrator_v2._try_pdf_evidence`）——运行窗口内 `orchestrator.log` 既无
「PDF 证据通道：…」也无「PDF 证据通道未取得正文」，审计里也没有该主机的 `fetch_document`
记录。触发条件依赖**抓取步骤自身**的结果里带 PDF 地址或 `%PDF-` 头；材料由检索步骤取回时
不会被路由。因此这份 PDF 只贡献了乱码文本，四类缺口全开。处置建议（留给下一批）：
在快照新增项上按 `%PDF-` 头统一路由到解析通道，并补一条"检索步骤取回 PDF"的离线用例。

## Part B · 账本逐条核对（**对不上**，已定位三处缺口并修复）

核对口径：`budget_state.json`（阶段计数）↔ Redis 共享计数 ↔ `llm_calls.jsonl`（调用形状）。

| 来源 | 读数 |
|---|---|
| `budget_state.json` | `calls_reserved=8`、`calls_settled=8`，`stages` **只有 `step`**（8），`unsettled=0`，`rejected_transitions={}`，`limits` 已入账 |
| Redis `wm:budget:ui-750185076a:f32880846ab9:*` | `calls=8`、`seq=8`，**没有 `tokens` 键** |
| `llm_calls.jsonl` | 7 条，`end_reason` 全为 `ok`（6 条 `stage=""`、1 条 `stage="plan"`） |

对不上的三处：

1. **LLM 路径不接跨进程后端**。`llm_client._root_budget_for_task` 建账本时没有传
   `redis_factory`，而编排器传了（`orchestrator_v2._budget_redis_factory`）。后果：供应商
   请求的预留只落在**各进程本地**计数上，文件被最后一个保存的进程整份覆盖（本次只剩
   `step`），且"上限 40 次"实际是"每个进程各 40 次"。
2. **流式与异步路径没有票据**。`_budget_open` 只在 `LLMClient.call` 与 `_call_backup` 里调用；
   `call_llm_stream`（Worker 步骤执行的首选路径）与 `call_llm_async` 既不预留也不写调用记录。
   本次 7 条记录全部来自非流式路径，**真正跑步骤的流式请求一次都没进账、也没留记录**。
3. **文件是"后写覆盖"**。`_save()` 整份写本地视图，`stages` 明细随之丢失。

### B1 修复（离线验证，不新增实机）

- `root_budget.default_redis_factory()`：把编排器里的后端工厂提为模块级函数，
  `orchestrator_v2._budget_redis_factory` 与 `llm_client._root_budget_for_task` 共用
  （0.3s 连接 / 0.5s 读，失败降级单进程）。
- `root_budget._save()` 改为**多进程合并**：文件里新增 `writers`（逐进程增量快照，票据号
  自带实例标记），顶层计数 = 各进程增量之和、票据为并集、阶段明细按键相加；账本身份
  （`ledger_id`）不同则不并入。实例标记从"类级"改为"账本实例级"，否则同进程重开账本会
  覆盖彼此的增量。
- `call_llm_stream`：每次真实发送（主/备各一次）开票+结算，并补 `_record_llm_call`
  （阶段取 `usage`，失败记 `error_class`）。`call_llm_async`：把**一次发送**包在票据里
  （含 JSON 解析失败/空响应/异常等所有退出路径）。`_async_call_backup` 同样开票。
- `async_worker_base._call_llm`：`budget_exhausted` 不再被"流式失败→回退异步"吞掉，原样抛出。

离线证据（`test_root_budget.py`，48 项全过；CI 清单 46 个文件本地全绿）：

| 用例 | 断言 |
|---|---|
| `TestRequestLedger.test_stream_request_is_counted_and_recorded` | 流式 1 次请求 → 账本 `llm.settled=1` 且留一条 `ok` 记录（阶段 `exec`） |
| `TestRequestLedger.test_stream_cap_refuses_before_sending` | 上限 1 → 第二次流式请求被拒且**未发送** |
| `TestRequestLedger.test_two_workers_share_one_call_cap` | 同一份假 Redis 后端下，第二个"进程"的请求被拒（共享额度） |
| `TestWriterMerge.test_second_writer_keeps_first_writer_counts` | 两个实例先后写 → 文件里 2 次预留、`llm`/`step` 阶段都在、`writers` 2 条 |
| `TestWriterMerge.test_reopened_ledger_does_not_double_count` | 重开 3 次 + 首次 → 4 次预留，token 累计不重复 |
| `TestWriterMerge.test_writers_are_not_merged_across_ledger_identities` | 身份不同（重新开始的一次运行）→ 不并入上一轮计数 |

**未验项**：跨进程共享计数只在假后端上验证过；真实 Redis 下的并发压力（多 Worker 同时预留）
未做压力测试。修复后的实机账本核对需要下一次有界运行才能复验。

## Part C · D6 回归包补口（离线，五场景全过）

新增两个场景（`evals/scenarios/*.json`，跑真实装配/渲染/导出）：

- `all_decline`（④ 全下降）：三项同步下降、发行人对收入给出解释、覆盖率升到 110%。
  断言降幅措辞（`coverage_contains="高于"`）与两条护栏（`brief_contains=["不表示回款改善",
  "不等于"]`）、`## 研究问题与下一步` 小节、交付 `verified`、图表 ≥3。
- `wrong_subject_period`（⑤ 错主体 + 错期间）：别家公司年报写了**同一句话**（同样的读数），
  另有一份契约期间之前（2021）的年报。断言两条排除原因都在（`subject_mismatch` +
  `period_before_contract`）、`located` **恰好 1**（错主体那份不计入证据）、
  `brief_contains=["未采用的材料", "研究问题与下一步", "未取得对应披露"]`。

配套改动：
- `scripts/scenario_run.py` 补 `clean_chart_data.json` 写入（调用**生产同一函数**
  `StructuredPipelineMixin._merge_structured_financials`）。此前场景不写该文件，验收的结构化
  溯源通道为空，正文里的同比/比率只能靠"文档原文里恰好也有同一个百分数"才算可溯源——
  同一份正文在场景里 67%、在实机里 100%（`ui-31305a2b28` 为 100%）。补上后 `all_decline`
  由 `draft`（溯源 67%）转为 `verified`，且清单新增 `acceptance.untraceable`（不可溯源数字
  逐条可见，跨修订比对不必再猜是不是同一批）。
- `scenario_run._check` 新增 `brief_contains` / `brief_absent` / `excluded_reasons` /
  `evidence_located_max` 四个核对键（`evidence_located_max` 用来证明"不适用材料**没有**
  被算成证据"，只有下界时多算一条测不出来）。
- `narrative_evidence.build`：未采用材料按**文档**去重（此前同一份 PDF 的多条记录各列一次，
  简报把一份材料写成两份）。`test_narrative_evidence.TestBuild.
  test_wrong_subject_document_listed_once_with_reason` 钉住该行为。
- 场景清单由三份扩到五份，`scenario_checks` 与 `test_offline_delivery.TestFrozenOfflineScenarios`
  同步更新（新增两条不变量：全下降的护栏措辞、错主体/错期不计入证据）。

**与计划的偏差（照实记）**：计划里 `wrong_subject_period` 期望"交付草稿"，实测交付为
`verified`。原因是交付状态由**数字层**门槛决定（底稿必需事实/披露时点/分析小节），
叙事证据缺口记在简报与验收 `gaps` 里、不进交付状态。本批不改这条语义（改它等于改交付
口径，超出批次范围）；若架构师要求"零可定位证据不得判 verified"，需要单独立项。

## Part D · 安全核对（有界、只读；**不宣称项目安全**）

按对外可达面排序核对，逐条给证据；未做渗透测试，未改任何安全设置。

| 面 | 结论 | 证据 |
|---|---|---|
| 研究入口 `POST /task` | 无新发现 | 所有 POST 走 `_require_admin`（`web_ui.py:3252`）；目标长度上限 + 注入检测 + 限流 + 清洗（`web_ui.py:4823-4846`）。残留：回环地址不限流（本地开发豁免，已在代码注释说明） |
| 任务读取 `GET /api/task/<id>/*` | 无新发现 | `_is_public_get` 不放行 `/api/*`、`/task/*`（`web_ui.py:2984-2988`），需会话；viewer 只读、config/audit 仅 admin（`web_ui.py:2991-3005`） |
| 文件导出 `/files/*` | 残留（低） | 匿名仅 `reports/`、`charts/` 与 `data/ranking.csv`，且必须持有分享令牌（`web_ui.py:3687`、`2976-2985`）；路径穿越由 `_safe_workspace_path` 前缀校验兜底（`web_ui.py:1586`）。残留：用 `abspath` 而非 `realpath`，工作区内**符号链接**可指向区外——需先能在工作区建链（本机写权限或沙箱逃逸），本批不改 |
| 交付 zip | 无新发现 | 归档名取 `relative_to(root)`（`workers/packaging_worker.py:185`），无 `..`；`project/**` + `reports/*.md` + `charts/*.png` 白名单（同文件 `95-100`）。残留同上（符号链接） |
| 抓取边界 `net_policy` | 无新发现 | 仅 http/https、无凭据、全部解析地址须公网（含保留/CGNAT/未指定/映射地址，`net_policy.py:152`）、连接使用**已验 IP**（`net_policy.py:283`）、不跟随重定向（`net_policy.py:358`）、剥离 Authorization/Cookie（`net_policy.py:325`）、字节上限与审计 |

Mimosa：本批改动后仍需按流程重跑扫描；**扫描器报 `scanner_enobufs` 时不得据此宣称安全**
（历史六笔提交均为此状态）。

## 未验项 / 留给下一批

1. 修复后的**实机**账本核对（需要下一次有界运行；本批只做离线验证）。
2. 真实 Redis 下多 Worker 并发预留的压力验证。
3. 检索步骤取回的 PDF 未路由到解析通道（Part A1）——需单独复现 + 离线用例。
4. "零可定位证据仍判 `verified`"是否为可接受口径（Part C 偏差）——待架构师裁决。
5. token 维度：账本记的是**上界**，与供应商实际用量仍未对账（沿用 D5 第一批未验项）。
