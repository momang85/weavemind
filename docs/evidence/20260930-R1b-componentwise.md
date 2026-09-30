# 2026-09-30 下午 · R1-b 收口：逐项贡献验证（合计相等不是验证通过）

执行依据 `docs/代码逻辑收口与DSH下一批_20260930下午.md` §3 R1-b。**只做 R1-b**（R1-a 已在上批
收口）。小提交、失败→通过；未扩模型/训练、未全仓重构、未新增付费、未读或输出密钥。

## 1. 反例（复核原文，先复现）

`validation.py` 此前对分项只有两条：金样只核**主输出**，`identity` 只核"合计=总量"。
于是——利润桥两个分项 **+10/−10**、`profit_to_cash` 分项 **+10/−10**、情景 **基准 +10/使用者
情景 −10**：合计一模一样，`gold`/`closure`/`identity`/`unit` **全部 ok**，运行照样 `validated`。
情景的"基准复现"还只信载荷自报的 `base_reproduction_gap=0`/`direction_ok`。

## 2. 最小修复（四处，无新框架）

| 位置 | 修法 |
|---|---|
| `contracts.ModelSpec` | 新增 `component_ids: dict`——`{输出指标: (稳定 component_id, …)}` 的**契约声明** |
| 各算子 | 分项带 `component_id`；新增 `components_gold(dataset, params)`：与 `gold` 同一纪律的**独立计算路径**（Decimal、同一身份判据、不看载荷自报字段） |
| `validation.py` | 新增 `components_check` 并在 `validate_output` 里**恒定执行**（与 `binding`/`output_shape` 同级）；逐项比对数值与单位，集合必须完全一致 |
| `store.recompute_run` | `label_changes` 文案补上"或分项契约字段（component_id）不同"，不把契约字段新增说成纯措辞 |

判据（逐项）：**① id 集合完全一致**（缺项/多项/换 id/重复都拒绝）；**② 每个 id 的数值**与
独立计算在声明容差内一致（合法舍入照过：`_close` 是相对容差 + 绝对 0.01）；**③ 单位**一致。
合计校验（`identity`）保留为**附加**检查，不再是充分条件。情景的 `base` 分项直接对数据集
**真实基期读数**重算。

已接：`profit_bridge`（net_profit_change: gross_profit_change / below_gross_line_change）、
`profit_to_cash`（profit_change 同两项）、`scenario_sensitivity`（scenario_net_profit:
base/user/counter；scenario_sensitivity: revenue_p1pp/gross_margin_p1pp/expense_m1pp）。
`cash_quality`/`working_capital` 不产生分项，未声明。

## 3. 失败→通过（`test_financial_analysis.TestR1BComponentwiseValidation`，5 条）

| 反例 | 旧行为 | 修后 |
|---|---|---|
| 利润桥两分项 +10/−10（合计不变） | `ok` | `validation_failed`，`components` 指出 `gross_profit_change` |
| 情景基准 +10 / 使用者情景 −10 | `ok`（且只信自报 gap） | `validation_failed`，指出 `base` 与 `user` |
| 两分项**互换**值 | `ok` | 失败（每个 id 都错） |
| 缺 `component_id` / 重复 id / 少一项 | `ok` | 三种都失败 |
| 合法载荷（利润桥/情景） | — | 仍 `ok`（不靠收紧到什么都过不了） |
| 契约完整性 | — | 声明 `component_ids` 的模型必须有 `components_gold` 且 id 集合一致 |

**不带修复时**：3 失败 + 1 报错（`components_gold` 不存在）——反例真实、用例有效。
附带修掉两处**不该静默**的失败方向：分项值不是数时 `_close` 会抛 `InvalidOperation`（现改为
"分项值不是数"的明确失败）；`components_gold` 跑不动时判失败而不是当通过（fail-closed）。

## 4. K2 复算（未被误伤，如实记差异类别）

两包仍 **9/9 与 7/7 `ok`**、`mismatches=[]`（数值/单位/期间/输入一致）。`label_changes` 从
1→4（`ui-603f626cbe`）与 1→2（`ui-fa2cb73e59`）：**分项新增了稳定 `component_id`**，
旧包分项没有该字段——按"契约字段/标签差异"单列，不冒充数值差异，也不隐藏。

## 5. 定向验证读数

`test_financial_analysis` **129**（+5）、`annual_financial_tables` 38、`facts`、`working_paper`、
`delivery_chain` 408、`offline_delivery`、`financial_chain`、`report_quality`、`review_edit_api` 32、
`p0` 434、`orchestrator_v2` 86 全过；`py_compile` 通过。

## 6. 下一步

R1 ✅（a/b 两半）→ **R2**：资料与下载链真闭合（原稿/更正稿按披露版本裁决而不是目录顺序、
空/错误 `financials.json` 不得阻断官方年报、50ms 慢头及时终止、生产发现与准入共享同一总截止）。
随后 R3（问题计划按意图区分历史/情景/预测子问题）、R4（同包研究交接与 PDF 全页视觉）。
