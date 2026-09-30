# 2026-09-30 下午 · R1-a 收口：期间链贯通（材料事实 → 底稿 → 冻结观察 → 身份）

执行依据 `docs/代码逻辑收口与DSH下一批_20260930下午.md` §3 R1-a。**只做 R1-a**（R1-b 逐项贡献验证
留下一批）。小提交、失败→通过；未扩模型/训练、未全仓重构、未新增付费、未读或输出密钥。

## 1. 反例（复核原文，先复现）

- `dataset.py` 的 `_observation_of` **丢 `period_kind`/`period_label`**（抽取器产出了、底稿也写了）：
  冻结后"毛利半年当全年""flow↔stock 互换"完全看不出来 → 利润桥照样 `validated`；
- `contracts.full_identity_ok` 不核实际区间与存量/流量；
- `dataset._is_annual` 只看"标签以年结尾"：`2024年` 这个标签可以掩盖半年度或期初。

## 2. 最小修复（四个文件）

| 位置 | 修法 |
|---|---|
| `adapters/annual_financial_tables.py` | **派生事实继承父事实的期间身份**（两个父一致才继承：`period_kind/label/start/end`）；不一致就留空（不猜） |
| `working_paper_export.py` | 底稿行补 `period_label` + `period_start`（此前只有 `period_kind`/`period_end`——"只在抽取器补字段、底稿再丢一次"） |
| `financial_analysis/dataset.py` | `_observation_of` **保留**四个期间字段；`_is_annual` 按**真实区间**判年度（flow 声明了 0<天数<300 → 不是年度期间；stock 期末非 12-31 → 不是年末余额；**只给期末日期（0 天）→ 区间未知，按标签判但不补全年**）；新增 `_period_limitations` 把"受限/未知"如实写进 `gaps` |
| `financial_analysis/contracts.py` | `Observation.observation_hash` 纳入 `period_label`（kind/start/end 已在）；新增 `METRIC_PERIOD_ROLE`（flow/stock 角色表）、`period_role_of`、`_period_span_days`、`period_identity_ok`，并在 `full_identity_ok` 里**与主体/币种/口径同级**执行 |

`period_identity_ok` 的判据（只判输入自己声明出来的东西）：角色↔kind 不符 → 拒绝；flow 区间
0<天<300 → 拒绝；stock 期末非 12-31 → 拒绝；同一期间出现不同区间（半年度与全年混排）→ 拒绝；
日期缺失（0 天或空）→ 不下断言，但由 `gaps` 记"区间未知，未按全年补"。

## 3. 失败→通过（`test_financial_analysis.TestR1APeriodChain`，5 条）

| 反例 | 旧行为 | 修后 |
|---|---|---|
| 冻结带期间性质与列头 | 字段被丢（观察里为空） | `period_kind=flow`、`period_label=2023年度`、起止齐备 |
| 只改列头原文 | 身份不变 | `observation_hash` 与 `dataset_hash` **都变**（改 label 即换观察） |
| 两期毛利一条 flow 一条 stock | `validated` | `not_applicable`（期间身份不自洽） |
| 净利全年 + 毛利半年（同一 "2024年"） | `validated` | `not_applicable`：同一期间出现不同区间 |
| 半年流量冒年报 | 计为年度期间 | 不进年度期间清单 + `gaps` 记"不按年度期间使用"；桥只剩一期 → 不 `validated` |
| 合法全年 + 年末存量（含期初→上期末） | — | 利润桥/营运资本仍 `validated`（不得误伤） |

**不带修复时**：该 5 条中 3 条失败、2 条因缺 `period_role_of` 报错（既证明反例真实，也证明用例有效）。
另在 `TestL1OfficialMaterialFeedsFacts` 增补**端到端贯通断言**：官方原文 → 事实（含派生毛利，
带 period_kind/label）→ 底稿行（带 label）→ 冻结观察（带 kind/label）。

## 4. 兼容性（K2 证据未被误伤）

冻结包内的数据集身份是 `DatasetManifest.dataset_hash` 这一**落盘字段**（`AnalysisDataset.dataset_hash`
直接返回它），本次加严作用于**新冻结**；旧包复算仍以"输出/数值指纹"为准：
`ui-603f626cbe` **9/9 一致**、`ui-fa2cb73e59` **7/7 一致**，`mismatches=[]`（仅旧默认情景 1 条措辞差异）。
即"身份版本差异"与"数值一致"两件事分开，不互相冒充。

## 5. 定向验证读数

`test_financial_analysis` **124**（+5）、`test_annual_financial_tables` 38、`test_facts`、`test_fact_fidelity`、
`test_working_paper`、`test_delivery_chain` 408、`test_offline_delivery`、`test_financial_chain`、
`test_p0` 434、`test_review_edit_api` 32 全过；`py_compile` 通过。

## 6. 未做（下一批）

- **R1-b 逐项贡献验证**：`validation.py` 的 `identity` 仍只核"合计=总量"，分项 +10/−10 抵消、
  情景基准 +10/用户情景 −10 仍会通过——需要模型声明稳定 `component_id` 与输入绑定、独立逐项
  计算并比对（比值/量纲/期间/参数/结构/公式），合计校验降为附加检查。本批**未动**；
- R2（版本裁决、空 `financials.json` 不阻断官方年报、50ms 慢头及时终止与发现/准入共享截止）、
  R3（问题计划意图）、R4（同包交接与 PDF 视觉）按序在后。
