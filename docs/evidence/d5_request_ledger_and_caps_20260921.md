# D5 第一批 + 存疑项验收（根任务账本贯通到每次真实请求）

2026-09-21。承接 D3（`6e54dac`）。范围：`root_budget.py`（记账/管制分离、环境上限、上限入账）、
`llm_client.py`（每次请求开票/结算）、`report_brief.py`（目标类披露时点与支持原文、
括号式同比）、`narrative_evidence.py`/`scripts/freeze_ar_mdna.py`（真实样本）、
测试与 CI 清单。离线为主 + 一次有界公开披露扫描。

## Part A · 存疑项验收

### A1 「利润/现金流解释不在所抓页内」——**证实**（带证据）
有界扫描第 17–71 页（55 页、固定主机、`net_policy.fetch_document`、1s 间隔、生产判据
`is_causal` + 核心指标筛句）：命中 **1 句**，且是**披露规则句**（第 71 页"非经常性损益项目
界定为经常性损益项目的情况说明…"），不是经营变化解释。加上此前的第 1–16 页，
**整份 71 页年报里核心指标的真实经营解释只有第 3 页那一句**（收入）。
处置：冻结脚本补 `is_policy_text` 守卫（规则句也会命中"影响"这类词），避免扩页时误收；
表述从"仅前 16 页"改为"全 71 页已扫，利润/现金流解释确实不存在"。

### A2 目标类判断的"披露时点"与"支持原文"——补齐并验证
- 冻结脚本新增**发行人目标句**采集：真实目标句已入样本（第 3 页
  "报告期初，公司规划 2024 年力争营业收入同比增长 5%-10%。"）。
- `_claims` 目标分支从四项扩到五项并**分别点名**：目标年度 / 目标值或范围 /
  发行人披露的**支持原文**（该句须匹配到已定位的发行人记录，只有引用编号不算）/
  **披露时点**（证据 `published_at` 存在，且报告期不得晚于目标年度）/ 实际口径。
  五项齐备 → `target_claim` + `bound`（理由写明"目标来自发行人披露原文"）；缺项 → `needs_check`。
- 既有反例仍成立：完成率式目标句缺"目标值/实际口径"；新闻来源的目标句缺"发行人披露原文"。

### A3 括号式同比——补规则
"归母净利润的降幅（-33.38%）"这类括号值按**最近指标的同比**解析并带方向。实机草稿复测：
三个断言现在分别记为 `net_profit_yoy / revenue_yoy / operating_cashflow_yoy`（此前落到基础
指标上）；因句内无年度，判定仍如实为 `needs_check`（未标期间）——不借远处年度。

### A4 候选验收修正版被采纳——**非桩**集成测试
真实 `_accept_candidate` + 真实 `accept_for_body`（临时工作区、无模型调用）：
候选拿到**它自己**的验收（`acceptance_for_this_body()` 为真）、被采纳的就是被验收的那一版
（研究任务候选会经装配，`out == adopted.body != raw`）、`report_quality.jsonl` 记到候选验收结论。

### A5 质量向量在真实样本上的分布
| 样本 | observations | analysis_ok | 未支持/待核查 | 重复块 |
|---|---|---|---|---|
| 实机草稿 `da62d9e4` | 4 | True | 1 | 0 |
| 实机草稿 `eb4efbcb` | 5 | True | 1 | 0 |
| 实机旧交付稿 `17ca7ebc` / `fde2f2e9` | 0 | **False** | 0 | 0 |
| 三场景产物 | — | — | — | — |

结论：上限 10 对真实草稿**不构成约束**（实测 4–5），保留；旧交付稿（分析节 214 字）
被如实标为"无分析"；场景夹具因工作区/契约解析不同（`scen-*` 任务不在标准库）返回**未知**，
不参与比较——已记入未验项。

## Part B · D5 第一批：账本贯通到每次真实请求

**根因**（D5 前置审查 P1 + 实机证据）：`root_budget` 在**不限额度时所有方法都是空操作**
（`reserve` 直接返回空串）→ 实机 `budget_state.json` 恒为 0/0/0，而 `llm_calls.jsonl`
只有编排器侧 5 条 → "供应商实际收到多少次请求"无从核对。

1. **记账与管制分离**（`root_budget`）：不限额度也**如实计数**（开票、结算、落盘），
   上限只决定"是否拒绝"；`settle/refund/mark_unsettled/mark_stage_unsettled` 同样不再因
   "不限"而空操作。0 仍然是不限：不拒绝、不拿别的配置兜底（R2 语义保留）。
   两处旧断言按新语义更新（`test_unlimited_budget_counts_without_refusing`、
   `TestZeroMeansTrulyUnlimited`），并注明改动原因。
2. **每次请求开票**（`llm_client`）：`_budget_open` 在**发送前**用任务上下文的 accounting id
   建账并预留（`stage=llm/backup`，`tokens=max_tokens` 作上界）；成功/失败/JSON 解析失败/
   思考耗尽重试各结算一次；**预算不足时抛带 `budget_exhausted` 的错误且请求不发出**；
   备端点（`_call_backup`）同样入账。开票刻意放在 `try` 之外——放进去会被本地重试逻辑
   吞成一次"端点失败"。
3. **每任务上限声明**（不改全局 0/0/0）：`limits_from_config` 支持
   `WM_TASK_MAX_SECONDS / WM_TASK_MAX_CALLS / WM_TASK_MAX_TOKENS` 覆盖；
   **声明上限写进 `budget_state.json`**（`limits` 字段）供审计"这次跑的上限是多少"。
   实测：`WM_TASK_MAX_CALLS=40` + `WM_TASK_MAX_SECONDS=1500` → 账本入账
   `{'max_seconds': 1500.0, 'max_calls': 40, 'max_tokens': 0}`、`limited=True`。
4. **模拟供应商验收**（`test_llm_request_ledger.py`，5 项，不联网）：
   - 不限额度下两次调用 → 账本 `calls_settled == 供应商实收 2`、无未结票据；
   - `WM_TASK_MAX_CALLS=2` → 第三次**发送前**被拒（`budget_exhausted`）、供应商实收仍为 2；
   - 首次失败后重试 → 供应商实收 2、账本记 2（`stages.llm.settled == 2`）；
   - 备端点请求 → `stages.backup.settled == 1`（切流也入账）；
   - 发送前取消 → 不开票、无在飞票据。
   token 记账为**上界**（本客户端不解析供应商实际用量），结算注记写明。

**连带修复**：`test_deploy_manifest` 的 CI 覆盖守卫抓到新测试文件未进清单（已加），
并抓到步骤名里未加引号的冒号会让 YAML 解析失败（已加引号）——两条守卫都按设计生效。

## 验证

- `test_root_budget` 37、`test_llm_request_ledger` 5、`test_cancel_semantics`+`test_prompt_system`
  85、`test_deploy_manifest` 16、`scenario_checks` 17 全绿；三场景复跑全过。
- 本地按 `ci.yml` **全量后端清单逐个跑：47 个文件、0 失败（356s）**。

## 未验项

- **实机读数**：账本与上限在真实运行中的读数要等下一批有界实机（D5 退出标准：
  "测试供应商实际收到的请求数与根账本可逐条一致"已在模拟供应商上成立；真实供应商的
  逐条一致需要一次带 `WM_TASK_MAX_*` 的有界运行）。
- token 记的是上界而非实际用量（客户端未解析 usage）；若要精确 token 账，需要先接用量解析。
- 跨进程原子性依赖 Redis：本批的模拟测试是单进程；Redis 路径由 `test_root_budget` 的
  假 Redis 用例覆盖，未在真实 Redis 上做并发压测。
- 场景夹具（`scen-*`）下质量向量返回未知——需要场景运行器把契约/底稿暴露成标准任务形状
  才能参与比较（D6 回归包可一并处理）。
