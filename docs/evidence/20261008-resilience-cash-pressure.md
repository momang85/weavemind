# 10-08 经营韧性与现金压力：执行证据

批次：架构师 10-08 新阶段决定（[复核](./20261008-architect-next-stage/review.md)、[三域规划§14](../../三域金融研究架构规划_20261005.md)、[总指令](../../总架构师执行指令.md) 四条）。
交付：**唯一新增算子 `cash_pressure` ＋ 两张经营韧性卡**；只顺带收口两处身份接缝（旧版/新材料身份、运行事实身份）。
状态：**机器可复算、研究层待人工**；界面点击**未执行**（真实阻塞见下）。全部离线、0 API 调用。

## 一、新增能力（唯一新增算子）

`financial_analysis/operators/cash_pressure.py`（`cash_pressure_v1`，注册表白名单）：

```text
R1 = R0×(1+g)          # 与 scenario_sensitivity 同一处纯函数 scenario_revenue_gross
C1 = R1 − R1×(基期毛利率+m)
额外占用 = R1/365×Δ回款代理天数 + C1/365×Δ库存天数      # 首批付款不变
```

输出：`incremental_capital_occupation`（回款/库存两构成，相加＝合计）、
`collection_days_capital_per_day`、`inventory_days_capital_per_day`；
输入/假设/代理/边界随载荷（`assumptions`/`limits`/`diagnostics`）。
接入：registry → runner → store.run/selection → 现有参数表（`allowed_params`/`default_params` 自动进面板）。

计划位置：`RESEARCH_COMBINATION = (operating_drivers, cash_reconciliation,
scenario_sensitivity, cash_pressure)`；命「经营研究」的问题类型同源采用。
**按问题的选择照旧**：本批实测——旧任务 `ui-10ea37599d` 的问题是
「研究…营业收入、归母净利润、经营活动现金流净额」，计划采用 profit_bridge/cash_quality 等，
**不采用**情景/现金压力（拒绝原因在 `analysis/plan.json`）——不因新算子就多跑。

### 数学验收（§14.4）

| 检查 | 证据 | 结果 |
|---|---|---|
| 基准复现 | 天数 0/0 → 合计 0.00 元（洋河、三一各一次） | 通过 |
| 独立手算 | 洋河 288.7629699356/365×10＝7.9113 亿、77.5121835666/365×10＝2.1236 亿、合计 **10.0349 亿**；实现的默认运行给出 `1003493571.24 元` | 与设计算例逐位一致 |
| 分项相加＝合计 | `identity` 独立验证（`closure=0`）＋ `components_gold` 逐项 | 通过 |
| 单位/期间/假设同源 | 输出单位随输入（元/亿元）；`output_period` 取数据集期间；g/m 与情景同一求值器 | 通过 |
| 经济方向 | 天数 +20/+10 > +10/+10；−10/−10 < 0 | 通过 |
| 拒绝 | 收入非正→`not_computable`；毛利率假设把成本打到非正→`not_computable`；天数越界/`days_in_year=200`→`not_applicable` | 通过 |
| 两公司各自复算 | 洋河 10.03 亿 vs 三一 36.98 亿（各自冻结输入，无硬编码、不做跨行业排名） | 通过 |

定向测试：`test_financial_analysis.TestCashPressure`（11 项）、`TestResilienceCard`（3 项）。

## 二、两张经营韧性卡（同一入口，结论随本公司数据改变）

卡由**选定运行**现场装配（`narrative.resilience_lines`），随正文与同版附件
`analysis/resilience_card.md` 一起交付；利润门槛来自 `scenario_sensitivity` 的单因素反推，
天数冲击来自 `cash_pressure`，两项待核假设的依据只引用**已验证运行**的读数（不现算无背书比率）。

- 洋河 2024（[卡片](./resilience_card.md)，PDF 第 3–4 页）：
  目标归母净利 100.16 亿元（上一期 2023 年），基准 **66.73 亿元** → **恢复门槛**：
  收入侧需 **+15.8226%**（毛利率固定基期）或 毛利率侧需 **84.7325%**（较基期 +11.58pp）；
  Δ回款 15 天／Δ库存 10 天 → 11.87＋2.12＝**13.99 亿元**额外占用（每 1 天 0.79/0.21 亿元）。
- 三一 2024（[卡片](./sany_card.md)，同一生产入口离线跑）：
  目标 45.27 亿元、基准 **59.75 亿元** → **已在目标之上：可承受空间**
  收入可下降 **7.0440%** 或 毛利率可降到 **24.5694%**（较基期 −1.86pp）；
  Δ回款/库存各 10 天 → 21.31＋15.68＝**36.98 亿元**。
  与洋河**不是同行**，原始金额不作韧性排名。

卡片数字溯源：洋河 **13/13**、三一 **11/11**（结构化运行输出/构件/运行声明的目标/天数参数）。

## 三、两处身份接缝（随同一选版输入收口，不扩通用审核层）

1. **更新必须有旧版本与已准入新材料身份**（`continuation.build_for_task`）：
   较早侧＝由已采纳旧版本（`report_versions.json`）与契约裁定的**同主体、按旧截止准入**材料；
   较新侧＝报告期严格更晚、主体 `ok`、按**新截止**（声明优先，未声明按材料自身发布日并如实记录来源）
   准入的材料；更新**另存新版**（旧版原样归档 `analysis/continuation_versions/`，内容未变则幂等不重写），
   没有已采纳旧版本时标 `historical_rebuild`。
   反例记录（[identity_version_cases.txt](./identity_version_cases.txt)）：错主体更晚材料 → 不进快照并返回不适用；
   发布日晚于声明新截止 → 同样不适用；变更截止 → 新版本 id、`supersedes` 指向旧版、旧版归档字节不变。
   `financial_analysis` 仍不 import `task_state`/HTTP 层（计算核心零模型调用这条守恒）。
2. **运行事实必须认识指标与期间**（`acceptance_checker._run_output_facts`）：
   期间取运行输入期间（`input_periods`/诊断 `periods`），指标必须在注册模型声明的输出清单里、
   构件必须是声明的构件/科目 slug，**未知不计入结构化可溯源**；复合指标按构件身份参与相容判定。
   同年错指标反例（[identity_runfact_cases.txt](./identity_runfact_cases.txt)）：
   `net_profit_change_detail:income_tax_expense` 的 7.2044 亿元——「2024 年**所得税费用** 7.20 亿元」命中、
   「2024 年**营业收入** 7.20 亿元」与「**2020** 年所得税费用 7.20 亿元」都被拒。

## 四、同版交付（洋河，离线生产链）

[run_log](./yanghe_run_log.txt)：正常入口（经营研究问题）→ 计划采用四模型 → 默认运行
（10/10 天，10.0349 亿）→ **改假设**（Δ回款 15 天）→ 新 run（13.99 亿，旧 run 保留）
→ 显式采纳（`accepted_identity`）→ 同版导出。

- 包 `deliverables_20261008_122426_f55e11.zip`：成员 34、清单 33、**hash 不符 0**；
  正文 sha `60ade368…`、PDF sha `ce1104dd…` 与清单一致；包内正文＝工作区正文。
- 包内 `analysis/resilience_card.md`（1,035 字符）与 `analysis/continuation.md`（3,427 字符）
  随包；`analysis/analysis_runs.json` 含**两条**现金压力运行（默认与采纳的
  `906182b37c2d…`），参数与身份随包可复算。
- 验收 `pass`；数字溯源 **91%（158/173）**；未溯源的 15 个仍是半年报续页/首屏里的旧数
  （17.83、22.83、11.64 亿元…），与 10-08 复核点名的同一批，**不是**本批新卡的数字
  （卡片溯源 100%）。
- PDF 10 页；首次出现页：关键判断与下一步 1、追踪卡/研究续页 2、**经营韧性卡 3**、
  研究问题与下一步 4、财务对照 5、图表 6、风险与核查 8。

## 五、界面（真实阻塞，不冒充点击）

- 应用**可用**：`launcher.py up` → 16/16 服务存活、`http://localhost:8080` HTTP 200。
- 但工作台**需要管理员登录**（页面快照：用户名 admin、密码空、登录按钮）；
  README 明确「没有固定默认管理员密码」⇒ 口令是**用户凭据/密钥类信息**：
  代理侧不读 `config.json`/`.env`、不改用户、不绕过登录。
- 因此本批**没有**界面点击证据：打开工作台、改天数/利润假设、复算、采纳、导出/下载
  四步都停在登录墙前（[ui_blocker.json](./ui_blocker.json)、[截图](./ui_login_blocked.png)）。
- 恢复条件：用户在本机浏览器登录一次（或提供已登录会话）→ 同一标签组内按
  「任务页 → 分析工作台 → 改 `collection_days_delta` → 复算 → 采纳这版 → 导出」即可；
  上述每一步的生产函数路径与预期产物本轮已在离线链上验证。

## 六、诚实缺口与未测项

- **研究状态与人工复核未动**：`research_draft` 仍是草稿；机器 pass ≠ 研究完成 ≠ 人工复核。
- **界面点击未执行**（原因见上）；离线链**不称**为正常界面通过。
- 五分钟读者任务：本批由代理按 PDF 自查（见 [reader_task.md](./reader_task.md)），
  **真实读者评价未做**。
- 费用：本轮为本地确定性计算，**0 API 调用**；真实付费/等待/人工活跃时间未测。
- 边界照旧：不是 CFO 预测/融资缺口/授信/目标价；缺授权行情不接；两家公司不作排名。
