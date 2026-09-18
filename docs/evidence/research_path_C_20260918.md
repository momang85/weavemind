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

**输入**：首页表单提交 —— 贵州茅台 `600519.SH`，2023 与 2024 两年度，合并报表口径，
截至 2025-04-30。

**验收清单**：六项必需事实（2 年 × 营业收入/归母净利润/经营活动现金流净额）齐全且能
指到来源位置；同比为 `%` 且期间相邻；关键结论不出现表外数字；做**一次**人工修订并确认
任务页 / 验收详情 / manifest / 导出头四面同版；Markdown/PDF/CSV/JSON + manifest 可下载
且口径一致。顺手取证东财返回的口径/披露日期字段。

**结果**：见第 4 节（运行后追加）。

## 4 实机结果

（运行后追加：脱敏时间线、六项事实与来源位置、同比、一次修订四面同版、四件套清单、
东财口径/披露日期字段、以及失败时的阶段级证据。）

## 5 未验证项与已知短板

- 港股/美股的**契约驱动**抓取未实机验证（`route_structured_for_request` 的 HK/US 分支
  只有单测覆盖；真实网络下东财港股接口与 SEC 的返回形状未在本批实测）。
- 前端仍无修订入口（B 批遗留）：一次修订只能用 API 触发。
- `report_versions.json` 仍是单进程锁 + 固定 `.tmp`，修订与编排器并发写不在本批范围。
- 自由文本研究请求的主体解析短板（"两个"被当主体）：影响门槛兜底口径，未在本批修。
- 诊断的 `stage` 来自调用方 `usage`：未传 `usage` 的调用点记为**空阶段**（已确认步骤/
  评审/规划都带标签）。
- Mimosa 预推送未取得完整扫描结论（`scanner_enobufs`）；本批不据此宣称项目安全。另有一次
  **误报**记录：对 `test_offline_delivery.py` 新增的替身 Redis 类报"高危 · SQL 注入
  （第 24 行）"，该处无任何 SQL 拼接。
