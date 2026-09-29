# Q1-② 同版绑定（正文/清单/ZIP 同一次运行）+ Q1-③ data_analyzer 金融路径

- 基线：`b6127e8`（Q1 第一片之后）。
- 依据：`docs/金融分析与受控建模阶段Q_20260929.md` §6（报告只消费验证输出、图表声明带 run ID、
  冻结包加入模型/运行/验证结果）、§7 Q1 退出（页面、图、底稿、正文、ZIP 绑同一 run）、
  以及 "workers/data_analyzer_worker.py 取消猜最新 CSV/目标列的金融路径"。
- 未新增付费整跑、未改阈值/模型/权限/代理/模板、未碰真库；全部离线与本地文件。

## 1. 新增：运行记录落盘 + 正文↔运行 的同一性核对（`financial_analysis/store.py`）

| 能力 | 说明 |
|---|---|
| `save_run / model_runs` | `ModelRun`（含每个 `output_hash`）原子落到工作区 `analysis_runs.json`；同 `run_id` **覆盖更新**，不产生第二份 |
| `binding_summary` | 交付清单用的身份块：run_id / 模型与实现版本 / dataset_hash / params_hash / 状态 / 每个输出的 `output_hash`（**不放正文**） |
| `payload_bytes` | 进冻结包的字节；没有运行记录时返回 `None`（不制造空文件、不改既有包内容清单） |
| `render_card_block` | 严格格式的分析卡区块：`run=<12位>` + 每行读数带 `output <output_id>`（可被下游反解核对） |
| `verify_body_binding` | 从正文分析卡反解 run/output，逐条与落盘记录核对：**数值不符 / 运行不存在 / 非同一 run / 运行未通过验证**都必须报出来；没有分析卡时如实返回"无卡可核"（既有交付不被改判） |

## 2. 接线（三处，都是"有运行记录才出现"）

1. `report_brief.build_structure` → `analysis_card`：读该任务工作区的**已验证**运行并按严格格式
   渲染；`render_brief_markdown` 把它插在 `## 分析` 之后（`BRIEF_SECTIONS` 同步加 `## 分析卡`）；
   没有运行记录 → 空串 → **正文一字不变**（既有样本不受影响）。未通过验证的运行不渲染。
2. `delivery_pipeline`：两条打包路径（快照路径 `_freeze_payload`、无快照路径 `repack_adopted`）
   都把 `analysis/analysis_runs.json` 按**字节**放进包；两个清单构造器
   （`_manifest_from_frozen` / `package_manifest`）都写 `analysis_runs` 身份块。
   包内字节 hash 由既有 `files` 机制覆盖（R2 先校验后发布那条路径照旧复算）。
3. `Dockerfile`：加 `COPY financial_analysis/ ./financial_analysis/` ——
   `test_deploy_manifest` 的"运行入口导入的本地包必须在镜像里"守卫**先报了红**
   （`['financial_analysis'] != []`），补上后 39 OK。这条守卫正好证明了它的价值。

## 3. 端到端证据（`test_financial_analysis.py` 45 OK，其中 17 项本批新增）

| 用例 | 断言 |
|---|---|
| `test_zip_manifest_and_body_share_one_run` | 真建临时工作区：`VersionStore.record+adopt` → `repack_adopted` → ZIP 内有 `analysis/analysis_runs.json`；清单 `files[...]` 与包内字节 sha256 **逐字节相符**；`analysis_runs.runs[0].run_id` == 运行；包内交付正文含 `run=<12位>`；对包内正文再跑 `verify_body_binding` → **ok** |
| `test_report_body_carries_a_verifiable_card` | 正文 `## 分析卡` 带 output 标识与 run → 与落盘运行一致 |
| `test_brief_structure_picks_up_the_card_from_the_workspace` | `build_structure` 的接缝：有已验证运行→有卡；只有未通过验证的运行→**空串** |
| `test_report_without_runs_has_no_card_section` | 没有运行记录时正文不含 `## 分析卡` |
| `test_card_without_any_output_tag_is_reported` | 卡里没有 output 标识 → 报"无法核对" |
| `test_body_referencing_an_unknown_run_is_reported` | 正文引用了不存在的 run → 报错 |
| `test_body_with_a_different_number_is_reported` | 卡里把 -33.43 写成 -30.00 → 报"正文与运行不是同一版" |
| `test_unvalidated_run_must_not_reach_the_body` | 未通过验证的运行进正文 → 报错 |
| `test_body_without_a_card_is_not_failed` | 没有卡 → ok 且注明"无分析卡区块" |
| `test_round_trip_preserves_identity_and_outputs` | 落盘再读回：run_id/dataset_hash/状态/元组字段/output_hash 全部一致 |
| `test_same_run_id_is_updated_not_duplicated` | 同一运行落两次只有一份 |
| `test_payload_is_none_when_there_is_nothing_to_bind` | 没有运行 → 不写空文件 |
| `test_binding_summary_carries_hashes_not_prose` | 身份块只放身份与 hash |

## 4. `data_analyzer` 的金融路径（取消"最新 CSV + 末列当目标"）

规则：**工作区里有本次任务自己的金融底稿**（`project/working_paper.json` 或
`working_paper.json`）→ 走金融路径；否则原 EDA 路径一字不变（不误伤通用数据任务）。

金融路径：`freeze_from_working_paper` → `compile_plan` → 逐个 `run` 注册模型 →
`save_run` 落盘 → 返回结构化结果（`mode=financial`、dataset 摘要与缺口、计划的采用/拒绝、
每个 run 状态与输出、分析卡、图声明）。同年比率（净利率/现金覆盖/资产负债率/研发强度）
复用既有口径走注册算子；零分母记 `not_computable`、不产出数字。

真机冻结样本（`ui-a06a005c9b` 底稿，只读）：

```
mode=financial  entity 洋河股份/002304.SZ  periods ['2023年','2024年']  usable 62/62
plan adopted ['profit_bridge']
run profit_bridge validated   net_profit_change -33.43 亿元
run ratio:net_margin 23.11%  ratio:cashflow_coverage 69.37%
run ratio:debt_ratio 23.24%  ratio:rd_intensity 0.37%
analysis_runs.json 落盘 5 条；结果里**没有** target 键（末列猜测已不适用于金融任务）
```

状态分档（不看"顺带算出的比率"）：请求的模型一个都用不上 → `failed`；
过了一部分 → `partial`；全过 → `success`。缺输入的模型进计划的**拒绝清单**并写明
缺哪个指标（`compile_plan` 对每个已注册模型逐个给原因，不再是一句"输入不齐备"）。

用例：`test_financial_task_runs_registered_models_and_stores_runs`、
`test_missing_input_still_takes_the_financial_path`（缺 gross_profit → `failed` +
拒绝清单 `missing: ['gross_profit']`，且**不产出**该 run）、
`test_non_financial_workspace_keeps_the_generic_eda_path`（无底稿 → 原路径，
且**不写**运行记录）。

## 5. 回归

`test_financial_analysis` 45、`test_delivery_chain` 403、`test_report_quality` 39、
`test_offline_delivery` 34、`test_task_projection` 19、`test_p0` 434、
`test_acceptance_adversarial` 23、`test_frontend_guards` 54、`test_actionable_state` 35、
`test_writer_consolidation` 60、`test_deploy_manifest` 39 —— 逐文件 OK。

## 6. 仍未做（如实）

1. **图仍是"声明"不是"像素"**：`chart_specs` 带 run_id/单位/期间/口径，但没有渲染 PNG
   （中文标注需要 CJK 字体与既有的 PDF 渲染口径，留给图表批次；不先做半成品图）。
2. **编排器把金融任务派给 data_analyzer** 的调用点未改：worker 侧已就绪，派发侧仍按既有
   能力路由；换句话说"金融任务一定走这条路"只在直接调用 worker 时成立。
3. 报告卡目前只渲染**主模型**（比率运行不进卡）；"修改可编辑假设并重算"属 Q2 情景模型。
4. 第二个真实公司仍未取得（见 `q0_ci_and_second_company_20260929.md`）。
