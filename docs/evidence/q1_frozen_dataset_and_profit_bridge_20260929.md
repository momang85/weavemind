# Q1 起步：冻结数据集 + 第一条利润分析链（`financial_analysis/`）

- 基线：`d4d3946`（Q0 五个提交之后）。
- 依据：`docs/金融分析与受控建模阶段Q_20260929.md` §4（最小契约与代码组织）、
  §5（第一批模型与金融口径）、§7 Q1（冻结数据集 + 一条完整利润分析链）。
- 本批只做**包内**（不堆进编排/HTTP 巨文件）：`contracts / dataset / registry / runner /
  validation / models.profit_bridge / report_adapter`，共 10 个文件。
- **零模型调用**：包内不 import `llm_client` / `web_ui` / `orchestrator_v2` / `task_state`，
  也不 import `requests`/`urllib.request`/`socket`——有源码级用例守着。

## 1. 契约（`contracts.py`）

| 对象 | 关键字段 | 为什么 |
|---|---|---|
| `Observation` | `fact_id` + `observation_hash` + 主体/市场/指标/**维度**/期间/期间类型/币种/单位/合并或母公司/重述批次/来源坐标/**状态** | 观察不是"一个数"；`fact_id` 刻意不含数值（既有约定），数值身份进 `observation_hash` |
| `State` | `ok / zero / missing / unknown / conflicting / invalid` | **真实零与缺失分开**；未知/冲突/无效一律"不可用"，不参与计算也不充当别人的输入 |
| `AnalysisDataset` | 观察元组 + `DatasetManifest` + `period_at(offset)` | 不可变：一变就是新数据集、新 hash；期间取用**按清单顺序**（0=最新期），不猜字符串 |
| `DatasetManifest` | `dataset_hash` / 截止日 / 期间 / 覆盖 / 缺口 / 冲突 / 可用模型 / 使用限制 | "当时我知道什么"；不含 `available_models` 以外的派生结论，避免"换注册表"看起来像"数据变了" |
| `ModelSpec` | 适用问题 + 输入槽位（期间关系 + 必须同维度）+ 输出（量纲/类型）+ **算子白名单名** + 参数界限 + 容差 + 验证项 + 预算 | 没有注册就没有算子；不做任意生成 Python / eval / AutoML |
| `ModelRun` | `run_id` = hash(model+实现版本+**dataset_hash**+params+as_of) + 起止/预算/环境 + 状态 + `expired(dataset)` | 数据集、参数、实现任一改变 → 新身份；旧结果**保留但立即作废** |
| `ValidatedOutput` | `output_id` / `run_id` / 值+单位 / 贡献项 / 残差 / 公式 / 输入 fact_id / 假设 / 限制 / `output_hash` | 正文与图只消费它，逐项可追溯，不按数值相等反猜 |

状态与既有三态分离一致：`validated / validation_failed / not_applicable / missing_input /
not_computable / failed` —— 与任务 SUCCESS、研究 ready、人工批准**各自记录**。

## 2. 真实链（真机冻结样本，只读复算）

在 `ui-a06a005c9b`（洋河股份 002304.SZ，2023→2024）的 `working_paper.json` 上：

```
dataset_hash 56c2c9dd…  entity 洋河股份/002304.SZ  as_of 2025-04-30
periods ('2023年','2024年')  observations 62  usable 62  conflicts 0
plan adopted ['profit_bridge']
run validated  run_id b39fbe24…  validation ok=True failed=[]
  net_profit_change      -33.43 亿元  2024年较2023年   residual 0.0  贡献项 2
  gross_profit_change    -38.01 亿元
  below_gross_line_change +4.58 亿元（未解释段：费用/税项/投资收益/少数股东等）
card: judgement「归母净利润变化 -33.43亿元；其中 毛利变化 -38.01亿元、毛利线以下净额变化 +4.58亿元」
      unexplained.value 4.58 亿元，note 写明"缺明细表只能说观察成立、原因待证"
      basis.unit 亿元 / formula 带四次输入 / observation_hashes 逐期
chart: waterfall，run_id 与 card 一致，unit/period/caliber 一并带走
```

这三个数与底稿/正文里的**同一批事实**逐字一致（此前 C 批为它们做过溯源修复），
并且**恒等式闭合差精确为 0**（十进制算，`Decimal("0.00")`）。

## 3. 退出条件里本批已验的部分（`test_financial_analysis.py`，28 项 OK）

| 退出条件 | 用例 | 结果 |
|---|---|---|
| 金样手算一致 | `test_gold_matches_and_closure_holds` | ✅ 十进制独立路径；-33.43/-38.01/+4.58 |
| 零分母 | `test_zero_or_negative_denominator_gives_not_computable` | ✅ `not_computable`，**不产出数字** |
| 负分母 | 同上 | ✅ 可算但带限制（不提"增长率"） |
| 单位换算 | `test_unit_conversion_is_explicit_when_scales_differ` | ✅ 万元/亿元换算进公式，不静默放大 1e4 |
| 错公司/错期/错币种/错口径 | `test_immaterial_company_or_period_or_currency_or_caliber_rejects` | ✅ 各自 `not_applicable`/`missing_input` + 原因 |
| 同值不同指标 | `test_same_value_different_metric_is_not_borrowed` | ✅ 四个输入各自独立，不互相顶替 |
| 数据重述 | `test_restatement_becomes_a_new_dataset` | ✅ 重述批次进身份 → 新 hash |
| 输入缺失 | `test_missing_input_stops_the_run` | ✅ `missing_input`，不拿别的数顶上 |
| 输出篡改 | `test_output_tampering_is_detectable` | ✅ 改值/改贡献项 → `output_hash` 变 |
| **改值保留 fact_id 必须让旧 run 过期** | `test_value_change_with_same_fact_id_changes_the_hash`、`test_run_identity_binds_dataset_model_params` | ✅ `expired` |
| 页面/图/底稿绑定同一 run | `test_card_and_chart_share_the_same_run` | ✅ 卡与图共用 `run_id`/`output_id`；`delivery_binding()` 供交付链引用 |
| 计算核心零模型调用 | `TestCoreHasNoModelCalls` | ✅ 源码级守卫 |

另加：冲突值不择一（`conflicting`）、非年度期间不组年度对、参数白名单外 fail-closed、
未注册模型拒绝、跨进程冻结同 hash（重启身份稳定）。
CI 同步加了一步（48 → **49 步**，`test_deploy_manifest.py` 守卫 39 OK），
本地 `python test_common.py`、`test_p0` 434 亦 OK（`test_common` 在本机需
`PYTHONIOENCODING=utf-8`，否则 GBK 控制台打不出 ✓，与 CI 无关）。

## 4. 仍未做（明确，不含糊）

1. **接进现役交付链**：本批只到"卡/图/绑定块"。`report_brief` / `delivery_pipeline` /
   冻结包尚未消费 `delivery_binding()`，因此"页面、图、底稿、正文、ZIP 绑定同一 run"
   只在本包内成立，**端到端同版绑定未做**。
2. **主动写入/编排接入**：`data_analyzer_worker` 尚未按显式数据集/分析计划调用本包
   （架构要求"取消猜最新 CSV/目标列的金融路径"，属于下一步改造）。
3. **Q2 的三类分析**（现金质量、营运资本、情景）与第二个真实公司（见
   `q0_ci_and_second_company_20260929.md`：本环境四条正常入口都取不到）。
4. 统计模型（Q4）仍只设计：本包没有任何统计/预测算子，`net_profit` 两期不构成时序。
