# 批次 C 证据：固定公司研究路径 + 离线故障注入 + 一次有界实机

依据《阶段复核与下一步指令_20260918》§C。基线 HEAD `87c0755`（A/A′/B 已在 main）。
本文档在**实机之前**写下上限与判据，事后只追加结果，不改判据。

## 1 固定研究路径（改了什么）

| 位置 | 改动 |
|---|---|
| `orchestrator_v2.py` `_fixed_research_plan` / `_research_steps` | 契约驱动的研究任务用**代码内固定步骤**（搜索 → 抓取 → 解释已选事实 → 出报告），不经过通用规划器 |
| `orchestrator_v2.py` `_route_template` | 固定路径前置：命中即返回，先于 `direct_plan`、关键词匹配与那次无预算的 LLM 路由调用 |
| `facts.py` `research_subject` / `research_shaped` | "是不是研究任务"的**唯一定义**：有主体 + ≥2 期间 + 已声明口径 |
| `delivery_pipeline.py` `apply_research_hard_gate` | 改调上述判据（门槛口径 = 有主体，或已有底稿；与路径选择的严格版同源） |
| `orchestrator_v2.py` `run()` | 结构化预载前移到**规划之前**（取消检查之后、仍限 `resumed is None`）；计划为空/模型不可用的失败分支也落底稿 |
| `orchestrator_v2.py` `_selected_facts_block` | 报告/总结步骤注入"已选事实"（选中行 + 可重算同比 + 缺口 + 硬规则），模型只解释、不自行挑数 |
| `adapters/router.py` `route_structured_for_request` | 契约给了市场 + 稳定代码就直接抓（跳过 `classify_task` / `resolve_company`），代码后缀市场优先 |
| `llm_client.py` `_record_llm_call` / `get_task_llm_calls` / `describe_error` | 脱敏调用形状（阶段/次数/耗时/输入长度/输出上限/HTTP 或错误类别/结束原因），收尾落 `llm_calls.jsonl`，页面 payload 暴露 `llm_calls` |

**仍然生效的闸门**（固定路径没有绕开任何一条）：预规划取消检查、`_normalize_steps` +
`_ensure_report_step` + `_require_review_or_refuse`（路由模板分支）、计划确认、逐步派发的
根预算预留、收尾准入、研究交付硬门槛。固定计划不含 `code_execution`，`_is_simple_task`
保持 False，快速路径（跳过评审/反思）不会被误开。

**边界（有意不做）**：固定路径只认**表单落库契约**。实测 `_extract_company` 会把
"2023 与 2024 两个年度"里的"两个"当主体（`parse_research_request("…两个年度…")` →
`company='两个'`），据此选路径等于让路径依赖一个会认错主体的解析。自由文本入口仍走
通用规划器，并由 A 批的交付硬门槛兜底；该解析短板见第 5 节。

## 2 离线故障注入（C 的准入门）

`test_offline_delivery.TestResearchFixedPathOffline`，11 项全绿（`python -m unittest
test_offline_delivery`）：

| 用例 | 断言要点 |
|---|---|
| 规划超时 / 坏 JSON | `_BrokenPlanner.calls == 0`（根本没问规划器）、无 "Plan fallback: single content_summary step"、`financials.json` 与底稿存在、主体与期间来自契约、终态 SUCCESS |
| 素材缺失 | 交付 `status=draft` + `hard_fail` 非空 + 正文含"研究交付硬门槛未通过"、`deliveries()[-1].ok is False`、终态非 SUCCESS |
| 正文模型失败（报告步骤 FAILED） | 底稿 `working_paper.json` 存在，`build_result` 可重算且行数与落盘一致，交付非 verified |
| 失败分支兜底 | `_salvage_working_paper` 落 `working_paper.json/csv` |
| 取消（规划前 / 预载中 / 执行中） | 终态 CANCELLED，`step_by_key` 为空（一步都没派发） |
| 根预算耗尽 | 拒绝派发步骤、终态非 SUCCESS、底稿仍在（预载不发 LLM、不占预算票） |
| 诊断脱敏 | 载荷字段集恰为 10 项、类型正确；全量字符串不含目标文本与公司名；落盘 `llm_calls.jsonl` 与内存记录一致；带标签的调用 `stage=plan`、`input_chars=len(system)+len(user)`、`max_tokens` 为输出上限 |

夹具修正记录（都是夹具问题，不是产品行为）：报告步骤在固定计划里是**第 4 步**（原夹具
按 2 步计划回包）；研究报告的数字必须与 `financials.json` **逐字一致**（溯源按数值+单位
比对，"1741亿元"与源里的"1741.44亿元"不算同一条事实；happy path 之所以 100% 是因为它的
目标文本把数字作为用户材料带进去了）。

## 3 一次有界实机（上限在运行前声明）

**上限（运行前声明，事后不改）**：单任务墙钟 ≤ 25 分钟；LLM 调用 ≤ 40 次；单任务费用
≤ ¥5；全局预算偏好保持 0/0/0（不限）；失败即停，**不重试同一条实机**。
（实际：LLM 调用 11 次、累计调用耗时 429.7s，均在限内；墙钟 ≈ 37 分钟**超出**上限 →
按纪律请求停止，未重试。配置里的 `system.task_timeout=600` 未能在这条路径上封住墙钟。）

**输入**：首页表单提交 —— 贵州茅台 `600519.SH`，2023 与 2024 两年度，合并报表口径，
截至 2025-04-30。

**验收清单**：六项必需事实（2 年 × 营业收入/归母净利润/经营活动现金流净额）齐全且能
指到来源位置；同比为 `%` 且期间相邻；关键结论不出现表外数字；做**一次**人工修订并确认
任务页 / 验收详情 / manifest / 导出头四面同版；Markdown/PDF/CSV/JSON + manifest 可下载
且口径一致。顺手取证东财返回的口径/披露日期字段。

**结果**：见第 4 节（运行后追加）。

## 4 实机结果

任务 `ui-96f5c363cc`（2026-09-18 21:26 提交 → 22:0x 请求停止，墙钟 ≈ 37 分钟）。
**结论：C 的管线在实机上是通的；"合格报告"这一条没达成，卡在三个可定位的点上。**
按预声明纪律：超上限即停，不重试同一条实机。

提交通道说明：本机 UI 需要登录会话，运行侧没有可用凭据（也不应复制会话 token），
因此提交走 `POST /api/tasks` 在鉴权之后调用的**同一个函数** `web_ui._publish_task`，
并送出前端 `buildResearchGoal` 会生成的同一份 `research_request`（先过
`_sanitize_research_request`）。未覆盖的只有"表单字段 → payload"这一层（由前端守卫与
此前的渲染检查覆盖）。脚本：`scripts/research_acceptance_run.py`。

### 4.1 通的部分（实机证据）

| 项 | 证据 |
|---|---|
| 固定研究路径生效 | DB `steps_json` = `[1 web_search, 2 web_fetch, 3 content_summary, 4 report_generator, package-5 package]`——正是代码内固定序列；通用规划器**未被咨询**（若走了 `_plan`，失败会落到单步 `content_summary`，步骤集不可能长这样） |
| 契约驱动抓取生效 | `financials.json.contract = {company 贵州茅台, company_id 600519.SH, market cn, periods [2023,2024], caliber 合并}`；`metadata` = eastmoney_ashare / 600519 / CNY / 亿元 / annual / annual_count 2；行含 `report_date` 2023-12-31、2024-12-31 |
| 脱敏调用形状 | Redis `llm_calls:ui-96f5c363cc`：11 次调用、4 次失败、by_stage `{'':9,'plan':2}`、累计耗时 429.7s；504 → `http_status=504, error_class=http_504, end_reason=exhausted`，读超时 → `error_class=timeout`；`max_tokens` 记录到思考耗尽后的 4096→8192 放大；记录内**不含**目标文本与公司名 |
| 失败分支兜底 | 计划/正文失败路径都会 `_salvage_working_paper`（离线用例覆盖；实机本次未触发） |

实机预探针发现并修掉的缺陷：适配器只认**本地代码**——契约里的 `600519.SH` 直接传给
东财 A 股接口会报"无数据"，已加 `facts.bare_code`（`600519.SH` → `600519`，美股
`BRK.B` 原样）并有单测。**这是离线夹具（替身适配器）覆盖不到、只有真探针能发现的一类。**

### 4.2 没达成的部分与各自的原因

1. **研究硬门槛：6 项必需事实缺口（口径未知）——设计问题，待架构决策。**
   实机抓到的 18 条事实 `caliber` 全为 `unknown`（东财/SEC/巨潮**都不声明报表口径**），
   按 A′ 规则"口径只认来源声明的"它们进 `audit`（"已列在明细中供核对，不用于达标与
   同比"），于是 6 项必需事实全缺、`paper.ok=False`、同比 0 条 → 硬门槛 →
   交付 `draft`。这一结论在**运行前**用真实抓取载荷离线预演过，实机复现一致。
   即：**在当前规则下，任何只用这些数据源的研究任务都到不了 verified**，
   缺的是"口径证据链"，不是管线。
2. **未达终态：端点 504 拖垮反思，进入强制重做循环。**
   `tokenrhythm.studio` 持续返回 `HTTP 504` 与读超时（21:28 / 21:52-53 两轮）；
   21:53:23 日志："反思 LLM 失败，停止迭代（节省成本）" → "验收 fail 且反思不可用，
   按验收缺口强制进入重做" → 重做的报告步骤再次撞上 504。累计超过预声明的 25 分钟
   墙钟上限，遂请求取消（取消在 11.6s 内生效，DB 正文变为取消说明）。
3. **正文质量：验收 `fail` 的具体缺口（21:48:44）**——8 处数据缺失占位
   （未披露/未获取/待补充/数据缺失）、`[1..5]` 引用在文末来源清单中无对应条目、
   缺"免责声明"与"不构成投资建议"。这是模型输出与报告格式的问题，不是取数问题。

### 4.3 本次未验证（因未达终态）

- 四面同版（任务页 / 验收详情 / manifest / 导出头）与一次人工修订：**未跑到**——
  没有终态交付就没有可比对的交付字节；B 批的四例×四面矩阵仍在离线覆盖。
- 导出四件套与 manifest：未生成（`_task_pdf_bytes` 报 "report not found"，因为取消时
  还没有可导出的终态正文）。
- `llm_calls.jsonl` 落盘：收尾未执行，故文件未生成；本节 4.1 用的是 Redis 里的记录。
- 东财**披露日期**字段：payload 只有 `report_date`（报告期末），没有披露日期字段；
  `metadata.latest_report` 是 `2026-06-30 中报`（与本次两年度无关）。A′ 遗留的
  "东财口径/披露日期取证"因此仍未闭合。

### 4.4 环境侧的旁证

- 端点 `https://tokenrhythm.studio/v1`：504 与读超时（本轮 4 次失败调用）。
- 嵌入能力：`Embedding API HTTP 400 {"code":"MODEL_CAPABILITY_NOT_SUPPORTED",
  "message":"当前模型不支持该能力：embeddings"}`——记忆提炼按降级处理，未影响交付。
- 诊断标签缺口：`_reflect` 用的是既有 `usage="plan"`，所以反思调用在 `llm_calls` 里
  记为 `stage=plan`。**本次刻意不改**：`usage` 同时决定模型角色（`plan` → qwen3.8-max），
  改成 `reflect` 会顺带换掉反思用的模型（属于"不切模型"的红线）。
- CI 抖动一次：纯文档提交 `1bba827` 的 `clean-env-e2e` 报"60s 内没收到任务收执
  （orchestrator 未消费队列）"，而**同一 job 在代码提交 `c451491` 上通过**；按失败 job
  重跑后 success。判为 CI 侧冷启动/排队抖动，与本批改动无关（该 smoke 的 goal 无研究契约，
  固定路径不会命中）。

## 4.5 第二轮实机（口径修复后，走首页表单）

任务 `ui-b0c70016d5`：浏览器首页表单提交（公司 贵州茅台 / 稳定标识 600519.SH / 市场 A股 /
2023–2024 / 合并报表 / 截至日 2025-04-30），契约由**表单**落库。

**通的部分（实机确认）**

| 项 | 证据 |
|---|---|
| 表单 → 契约 | DB `research_request`：company 贵州茅台、company_id 600519.SH、market cn、periods [2023,2024]、caliber 合并、as_of 2025-04-30 |
| 固定研究路径 | 执行步骤 = `1 web_search / 2 web_fetch / 3 content_summary / 4 report_generator / package-5 package`——正是代码内固定序列，通用规划器未被咨询 |
| 口径证据链（本轮修复） | `financials.json.metadata.caliber=合并` + `caliber_evidence`（"来源行含 PARENTNETPROFIT…"）；两行年报 1741.44/862.28/924.64 与 1505.6/747.34/665.93，逐行公告日 2025-04-03 / 2024-04-03 |
| 结构化预载 | 图表由结构化数据生成（`Snapshot recycle: 10 docs, market_data=12`），预载在规划前完成 |

**阻塞：网关 ~60s 上游超时 × 我们的长生成**

`content_summary` 步骤被反复打回（10:30 / 10:35 / 10:40 / 10:47 各一次），
每次 `HTTP 504` 于 60s 整（客户端 60s 超时点）。**直接探针复现**（不经过任务链路）：

| 请求 | 结果 |
|---|---|
| 极小（16 tokens）| HTTP 200，2.3s |
| 中等（512 tokens）| HTTP 200，7.4s |
| 长生成 4096 tokens，deepseek-flash | HTTP 200，**38.3s** |
| 长生成 4096 tokens，qwen3.8-max | **HTTP 504，60.6s** |
| 步骤规模（2.3k 输入 + 8192 tokens）| **两个模型都 504，60.6 / 60.8s** |

即：网关（响应体里的 `alb`）在 ~60s 切断上游，而报告/总结步骤的**输出上限是 4096–8192**，
中文长生成普遍超过这个窗口。另外客户端在"思考预算耗尽"时会**把 max_tokens 翻倍到 8192**
（日志 `thinking budget exhausted, retry with max_tokens=8192`），这一步恰好把生成推过 60s。

**处置**：按预声明纪律（超 25 分钟墙钟即停、不重试同一条实机）在 1100s（≈18 分钟）请求取消，
取消在派发边界生效，终态 CANCELLED，未重试。

**本轮未验证**：六项必需事实与来源位置的**交付级**核对、同比、一次人工修订的四面同版、
导出四件套与 manifest——都没有终态交付可比对。另：更早那次表单提交 `ui-122e9962d7`
是我重启服务时打断的（"编排器进程已退出"），属无效验收，不计入。

**旁证**：本次实机还暴露一个诊断漏项——worker 步骤走 `call_llm_async`，而 C 批只给同步路径
埋了调用形状，故步骤侧 `llm_calls` 为空；已修（提交 `3edb1a1`），并顺带让 `_error_shape`
识别 httpx 形状（此前 httpx 的 504 会落成 `generic`）。



- **口径证据链（本批最关键的开放项）**：东财/SEC/巨潮都不声明报表口径，而 A′ 规则要求
  "口径只认来源声明的"——两者相加使研究交付**到不了 verified**（实机已复现：18 条事实
  全 unknown → 6 项必需事实缺口 → draft）。可选的下一步（需架构决策，不宜自行放宽门槛）：
  ① 从被引用的**年报正文**（`web_fetch` 抓到的页面）里提取"合并财务报表/母公司"声明，
  以来源位置为证据绑定到事实；② 引入声明口径的第二数据源；③ 明确"按请求口径采信并标注
  为人工声明"的第三种证据级别。三者语义不同，须由架构侧定。
- 港股/美股的**契约驱动**抓取未实机验证（`route_structured_for_request` 的 HK/US 分支
  只有单测覆盖）。
- 前端仍无修订入口（B 批遗留）：一次修订只能用 API 触发；本次实机也因未达终态没跑到。
- `report_versions.json` 仍是单进程锁 + 固定 `.tmp`，修订与编排器并发写不在本批范围。
- 自由文本研究请求的主体解析短板（"两个"被当主体）：影响门槛兜底口径，未在本批修。
- 诊断的 `stage` 来自调用方 `usage`：未传 `usage` 的调用点记为**空阶段**；反思调用因既有
  `usage="plan"` 而显示为 `plan` 阶段（改标签会顺带换模型，见 4.4）。
- 实机未达终态 → 四面同版、一次修订、导出四件套、`llm_calls.jsonl` 落盘本次均未验证。
- Mimosa 预推送未取得完整扫描结论（`scanner_enobufs`）；本批不据此宣称项目安全。另有一次
  **误报**记录：对 `test_offline_delivery.py` 新增的替身 Redis 类报"高危 · SQL 注入
  （第 24 行）"，该处无任何 SQL 拼接。
