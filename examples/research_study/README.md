# 织光离线研究样例

这个入口让未来 Wind 数据在到达前也能验证开发流程：文件预览 → 财务情景/现金压力 → 事件与情绪候选 → 事件收益/内部回测账本 → 风险特征与标签准备。

**本目录所有公司名称、证券代码、财务、新闻、行情、交易日历与标签均为模拟。不能用于投资或证明模型效果。** 代码形似 Wind 证券代码，仅用于测试身份对齐。交易日历不是交易所提供的历史日历。

在项目根目录运行（使用项目已经配置的 Python，不安装新的依赖）：

```powershell
python scripts/run_research_study.py examples/research_study/study.json --preview
python scripts/run_research_study.py examples/research_study/study.json --output-dir tmp/research-studies
```

第一条只读文件并显示结果。第二条把内部研究产物保存到 `tmp/research-studies/<研究ID前16位>/`。相同研究不覆盖旧目录；参数或输入改变后生成另一版本。它不启动服务、不调用模型/API、不修改当前工作台任务或人工复核状态。

结果目录包含：

| 文件 | 内容 |
|---|---|
| `summary.md` | 数据性质、利润与资金占用、事件候选、历史反应及风险读数 |
| `previews.json` | 原字段、单位、缺口和逐行问题 |
| `financial.json` | 既有财务模型运行、冻结数据集、显式单位换算台账 |
| `events.json` | 三类候选事件、实体情绪、原文引文/字符位置、版本和排除记录 |
| `quant/` | 复用原事件收益/回测算子的结果及附件清单 |
| `risk.json` | 财务风险读数与异常核查线索，不包含评级或违约概率 |
| `label_readiness.json` | 信用/反欺诈特征与结果标签的成熟、授权和时间检查 |
| `inputs.json`、`study_manifest.json` | 冻结规范输入、来源/实现/参数hash与产物hash，供离线复算 |

用上条命令返回的**具体结果目录**核对和复算：

```powershell
python scripts/run_research_study.py --verify tmp/research-studies/<实际研究ID前16位>
python scripts/run_research_study.py --replay tmp/research-studies/<实际研究ID前16位>
```

`verify`检查产物字节；`replay`使用实际冻结输入重算，不需要原始 Wind 文件仍在。实现改变则明确拒绝给旧版背书。两者不认证来源真实性、预测能力或人工复核。

## Wind数据到达后

1. 复制 `study.json` 到一个新的输入目录；放入已获准使用的真实 CSV/JSON，填写相对文件名，不自动扫描个人目录。
2. 设置 `synthetic: false`，替换全部模拟材料；保留各真实文件的来源、单位、口径、披露时间及版本说明。混合样例与真实资料时必须继续显式标模拟。
3. 设置 `feature_cutoff`（用于财务/新闻/风险特征）和 `evaluation_as_of`（用于之后的真实价格结果/成熟标签）。不能把下载日写成历史公开日。
4. 中文宽表支持常见三表科目。没有年度跨度的季度材料需声明累计或单季度；原始单位保存，计算副本显式换算至元。`field_map`方向是“原表列名 → 项目正式字段”，例如 `{"经营活动净现金流":"operating_cashflow"}`。
5. `financial.scenario_params`只放共享的 `revenue_growth`、`gross_margin_delta`；模型专属参数放 `financial.params.<模型ID>`。利润与现金压力共享假设冲突会明确报错。
6. `event_windows: [1,5,20]`从披露后下一交易日算第1日，内部映射为既有算子的 t0 偏移 `0/4/19`。这是收盘反应观察，不是成交收益。真实数据默认不放行未复权收益；拿到可靠复权口径后在 `market.metadata.adj_basis`声明，不照抄模拟样例的 `allow_unadjusted: true`。
7. 缺少某域就删除对应输入项，不用假数据补齐。没有行情显示待数据；没有信用/欺诈标签显示待标签。公开财报不能替代借款表现、交易行为和调查结果。

`metadata`是显式默认值，不能拿来覆盖原行冲突。学校“允许查看”不自动等于商业使用、云端AI处理或对外分发；本入口输出始终为内部研究文件，不自动把原始数据放进现有 MD/PDF/ZIP 下载包。当前尚未接入工作台按钮/正常选版交付。

## 后续信用与反欺诈标签契约

每条记录至少给：`record_id`、`entity_id`（信用）或 `account_id`（反欺诈）、`decision_at`、`features`、`label`（严格0/1或待成熟空值）、`outcome_end`、`label_available_at`、`horizon_days`（正整数）、`label_definition`、`authorization`、`synthetic`。

每个特征给 `name/value`、同一主体ID、真实 `available_at`，有更正还给 `version_available_at`；不得晚于决策时点。负标签要等观察窗口结束才可知。`authorization`需明确 `internal/training` 和实际依据。本模块只核对输入声明，没有核验合同真实性；离线数据具备两类/多主体/多个日期也不等于已经训练或部署模型。下一步还需要按主体和时间拆分、真实样本充分性、概率校准/误报预算等验证。

当前只实现有意义的数据路径和小样，不安装 Qlib/新闻模型或新数据库，不输出自动授信、实盘交易或交易拦截。
