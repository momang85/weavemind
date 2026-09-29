# Q2 第一批：现金质量 / 营运资本 / 条件情景三族模型（注册 + 独立验证 + 缺输入就拒绝）

- 基线：`447b04f`（Q1-②③ 之后）。
- 依据：`docs/金融分析与受控建模阶段Q_20260929.md` §5（第一批模型与金融口径）、§7 Q2。
- 未新增付费整跑、未改阈值/模型/权限/代理/模板、未碰真库；全部离线 + 真机冻结样本只读复算。
- **Q2 门槛（两个真实非金融公司）仍未满足**：第二个公司的材料本轮仍取不到
  （见 `q0_ci_and_second_company_20260929.md`）。本批交付的是**模型族**与"缺数据就拒绝"，
  不代表 Q2 已退出。

## 1. 三族模型（`financial_analysis/operators/`）

| 模型 | 算子 | 输入 | 输出 | 关键纪律（写进限制并由验证项守住） |
|---|---|---|---|---|
| `cash_quality` | `cash_quality_v1` | `operating_cashflow@0`、`net_profit@0` | 经营现金流−归母净利、覆盖率% | 覆盖率是**观察比率**不是调节恒等式；归母净利 ≤ 0 **不给覆盖率**（`cash_quality_sign`）；量纲不一致先**显式换算** |
| `working_capital` | `working_capital_v1` | 应收/存货/应付 ×2 期 + 收入 + 营业成本 | 占款变化（含三项分解）、应收/存货/应付周转天数 | 只有两期期末时用**期末口径**并写明（`posture_disclosed`）；应付天数分母是**营业成本近似**；分母非正不给天数 |
| `scenario_sensitivity` | `scenario_v1` | 基期 `revenue`/`gross_profit`/`net_profit` | 情景归母净利（基准/上行/下行）、单因素敏感度 | **基准必须复现基期**（`base_reproduction` 差额 0）；单因素方向独立检查（`scenario_direction`）；不显示概率/置信区间；参数越界 fail-closed |

注册表 `registry.specs()` 现为四族（利润桥 / 现金质量 / 营运资本 / 情景），算子白名单一一对应；
`compile_plan` 对**每个已注册模型**逐个给采用或拒绝原因（缺哪些指标），不再是一句"输入不齐备"。

## 2. 真机冻结样本（`ui-a06a005c9b`，只读）

```
adopted  ['profit_bridge', 'cash_quality', 'scenario_sensitivity']
rejected working_capital：缺输入 ['accounts_payable','accounts_receivable','inventory','operating_cost']
run profit_bridge        validated  net_profit_change -33.43 亿元 / gross_profit_change -38.01 亿元
run cash_quality         validated  cfo_minus_profit -20.44 亿元 / cashflow_coverage 69.37%
run scenario_sensitivity validated  上行情景 80.32 亿元 / 单因素敏感度 2.89 亿元（毛利率 +1pp）
run ratio:*              validated  净利率 23.11% / 现金覆盖 69.37% / 资产负债率 23.24% / 研发强度 0.37%
落盘 analysis_runs.json 7 条
```

- 现金质量的两个数与底稿同源：46.29 − 66.73 = **−20.44 亿元**；46.29/66.73 = **69.37%**。
- 情景的基准复现差额 **0**（参数全 0 时情景净利 = 基期 66.73 亿元），
  `direction_ok=True`（收入 +5% 升、毛利率 −1pp 降）。
- 归母净利改为 −5 亿元时，覆盖率**不再出现**，限制里写明"非正：分母没有可比含义"（有用例）。
- 营运资本在真实样本上**明确拒绝**并列出要补的四个指标——这正是"缺输入拒绝相应模型、不凑数"；
  用带占款字段的夹具验证了计算路径：占款变化 = Δ应收 3 + Δ存货 6 − Δ应付 1 = **8.0 亿元**、
  应收天数 = 13/288.76×365 = **16.43 天**，且写明"期末口径不是平均余额"。

## 3. 修掉的两个真实缺陷（都是本批新代码里的，夹具/真机抓出来的）

1. **单位换算方向反了**：把"值×（源量纲/目标量纲）"写成了"×（目标/源）"。
   夹具反例：`operating_cashflow` 46.29 亿元写成 `462900 万元` 时，差额算成 **4.63e9**（应 −20.44）。
   两处（`cash_quality`、`working_capital._to_unit`）都已改成与
   `working_paper` 既有比率换算同向，并留了会红的用例。
2. **验证项形状不对**：`unit` 检查原来要求"全模型只有一个单位"，把"差额（亿元）+ 覆盖率（%）"
   这种正常组合判成失败；改成"每个输出都要有单位 + 所有**金额**输出同量纲"。
   同时把 `identity`（合计=总量）对"没有分解项"的输出如实标为不适用，而不是判失败。

## 4. 定向验证

| 套件 | 结果 |
|---|---|
| `test_financial_analysis` | **57 OK**（本批 +12：三族各自的正例/反例、缺输入拒绝、越界 fail-closed、注册表一致性、计划采用+拒绝） |
| `test_delivery_chain` | 403 OK |
| `test_p0` | 434 OK |
| `test_report_quality` / `test_offline_delivery` / `test_deploy_manifest` / `test_financial_chain` | 39 / 34 / 39 / 36 OK |

## 5. 算子目录改名与**索引层守卫**（本批自查出的一个真缺陷）

现象：Q1/Q2 的三个提交（`3903945`/`447b04f`/`9bef7df`）里，`financial_analysis/models/` 下的
五个 .py **一个都没进 git**——`.gitignore:30` 有一条 `models/`（本意是别提交训练产物），
把源码目录整目录静默忽略。后果：被提交的树里 `registry.py` 仍 `from .models import …` →
干净检出与 CI 上 `import financial_analysis` 直接 ImportError，而**本机因为目录还在，全绿**。
这正是状态文档里点名过的"实现与用例分家"型事故，只是这次分家发生在 git 索引层。

处置（改前进）：

1. 目录改名 `financial_analysis/models/` → **`financial_analysis/operators/`**（不碰用户
   `.gitignore` 的既有规则；那里有用户未提交的改动，不去混写），`registry.py`/`runner.py`
   的 import 同步；
2. 新增守卫 `test_deploy_manifest.test_python_files_under_copied_dirs_are_tracked`：
   **凡 Dockerfile 拷进去的目录，其 .py 必须在 git 索引里**；
   失败→通过证据：`git rm --cached financial_analysis/operators/profit_bridge.py`
   → `AssertionError: Lists differ: ['financial_analysis/operators/profit_bridge.py'] != []`
   → `git add` 后 40 OK；
3. 干净检出等价验证：`git checkout-index -a --prefix=<tmp>` 后在该目录跑
   `python -m unittest test_financial_analysis` → **57 OK**（只含索引里的文件）。

**未修（如实）**：前三个提交本身仍缺这些文件（**不重写历史**）；HEAD 起是自洽的，
本证据文档与提交信息都写明了这一点。

## 6. 仍未做（如实）

1. **第二个真实非金融公司**：材料仍取不到（四条正常入口实测见 Q0-⑤ 证据）；因此 Q2 的
   "两个真实公司、不同经营特点"门槛**未满足**，不得宣称。
2. **占款口径的上游缺口**：现役事实层还没有 `accounts_receivable`/`inventory`/`accounts_payable`/
   `operating_cost` 这些 slug（抽取器目前只覆盖核心指标与经营维度），所以营运资本模型在真实
   样本上必然拒绝。补这些抽取器属"数据面"工作，未做。
3. **平均余额口径**：只有两期期末时可用的仍是期末口径；做平均余额需要三个期末余额
   （模型已按 `posture` 字段区分，缺第三期时不冒充平均）。
4. **可编辑假设的页面**：情景参数目前由调用方传入（worker/脚本），页面上的"改假设→复算→
   采纳→导出"未做（Q2 退出条件之一）。
5. **图仍是声明**：`chart_specs` 带 run_id/单位/期间/口径，未渲染 PNG。
