# D1：主张真实性与证据计数（阶段 D 第一小批）

2026-09-21。基线 `faa9dba`。范围：`report_brief.py`、`narrative_evidence.py`、
`working_paper_export.py`（补 fact_id）、`orchestrator_v2.py`（证据注入过滤）、
面板与 payload 契约、测试与冻结用例。全程离线（不联网、不调用模型、不新增实机）。

## 1. 反例复现（修复前读数，留史）

`evals/claims/d1_counterexamples_before.json`：10 个用例 **0 通过**（旧代码全部失败）。
生成方式：`git worktree` 检出修复前提交，用 `scripts/claims_d1_cases.py before` 跑同一批用例。

| 用例 | 审查反例 | 修复前读数 |
|---|---|---|
| c1 跨期/跨指标/跨单位同名值 | 底稿只有"2024 收入 100 亿元"，"2023 净利润 100 万元"仍被支持 | `status=bound`，`fact_ids=[fact-rev-2024]` |
| c2 同句混入虚假数字 | "收入100亿元、利润999亿元"整句 bound | `status=bound`（999 无事实也照样整句通过） |
| c3 目标类判断 | "目标完成率100%并已达成"仅凭一个年报引用通过 | `status=bound`、`type=observation`、reason 为空 |
| c4 符号 | 底稿同比 −12.83%：正文"下降 12.83%"（真话）与"增长 12.83%"（假话）都不绑定 | 两句均 `needs_check`（负号被 `_NUM_RE` 丢掉，谁也绑不上） |
| c5 单位量级 | 20 万元被 20 亿元支持 | 0.01 亿元 → `needs_check`；20 万元 → **`bound`**（误绑） |
| c6a 真实年报数字正例 | 288.76 亿元 + −12.83% | 仅收入绑定；同比未绑（无断言概念） |
| c6b 真实分部行 | 年报真实分部行（中高档酒 243.17 亿元、−14.79%） | `needs_check`（未冒充总额，但也无逐条理由） |
| c6c 真实非核心指标句 | 前五名经销客户 235,177.00 万元 / 8.15% | `needs_check` 且 reason 为空 |
| e1 解释范围 | 只给收入解释，利润/现金流也标"解释已取得" | 三个指标 `has_explanation=True`（全局扩散） |
| n1 定位计数 | located=2 实际只有 1 条有正文定位 | `located=3`（含 1 条无定位摘要），且"风险因素"被摘要从缺口里抹掉 |

## 2. 修复（最小通用改动）

1. `working_paper_export.build_result`：`rows_detail`/`derived_detail` 补 `fact_id`
   （生产 `rows` 原本没有 fact_id → 行级事实永远绑不上，`_findings` 也拿不到 fact_ids）。
2. `report_brief.py`：
   - 新增纯函数：指标别名表（长别名优先）、单位类别与金额量级换算（复用 `facts.amount_scale`）、
     句内**原子断言**解析（带符号数字 + 单位 + **就近**指标 + 最近年份 + 方向词）。
   - `_claims`：逐条断言对事实（指标/期间/单位类别/换算后数值/**符号**），状态口径
     `bound` / `partially_supported` / `unsupported` / `needs_check`（缺必需语义）；
     主张增 `assertions[]`、`evidence_ids`（已定位证据片段）、`support_status`、`claim_type`、`reason`。
   - 百分比挂在核心指标名后（"营业收入…下降 12.83%"）先试同比派生指标（`revenue_yoy`）。
   - 无数字的目标/因果判断句进主张清单（`target_claim`/`inference`，needs_check + 原因），
     不再漏出支持清单。
   - 目标类判断逐项核对：目标年度、目标值或范围（"完成率/进度"不算目标值）、
     发行人披露原文支持、实际口径——缺哪项写哪项。
   - `_change_explanation`：解释按**指标 + 期间**匹配（词表 + 文档期间），
     `has_explanation` 逐条计算并带 `matched`（来源号/定位），废除全局 `bool(management)`；
     风险文案带上解释的来源定位。
3. `narrative_evidence.py`：`located` = 已准入 + 有正文位置 + 按 (url, 片段指纹) 去重；
   `missing_kinds/missing_labels` 同样要求正文位置；payload 增 `snippet_hints`（检索线索）；
   同一 URL 已有正文证据时不再登记它的检索摘要。
4. `orchestrator_v2._narrative_evidence_block`：注入模型的 `[已取证据]` 加 admission 过滤
   （排除项/身份未知不再冒充已取证据）。
5. 面板与 payload：`partially_supported` 进"修改与重验"待核查列表并单独标注
   （未采用来源 / 无底稿事实支持 / 部分支持 / 待核查）；证据行显示"另有 N 条检索线索"。

## 3. 修复后读数

`evals/claims/d1_counterexamples_after.json`：10 个用例 **10 通过**；`check` 模式可重放。

- 三个误绑定反例：c1 `unsupported`（fact_ids 空）、c2 `partially_supported`
  （收入 supported / 利润 unsupported）、c3 `target_claim` + `needs_check` 且 reason 点名
  "目标值或目标范围、实际口径"。
- 正例仍成立：c4 "下降 12.83%"→`bound`、"增长 12.83%"→`unsupported`；
  c5 0.01 亿元↔100 万元→`bound`、20 万元 vs 20 亿元→`unsupported`；
  c6a 真实年报数字→`bound`；c6b 真实分部行→`unsupported`（不冒充总额）；
  c6c 真实非核心指标句→`needs_check` 且 reason="未提取到指标名"。
- 解释范围：e1 收入 `has_explanation=True`、利润/现金流 `False`（带 matched 定位）。
- 定位计数：n1 `located=2`、`snippet_hints=1`、"风险因素"仍在缺口里。

## 4. 定向验证

- `scripts/claims_d1_cases.py check`：10/10（CI 内由 `TestClaimAssertionSupport` 重放）。
- `test_delivery_chain` 256、`test_narrative_evidence` 39、`test_frontend_guards` 45、
  `scenario_checks` 17、`test_offline_delivery` 32、`test_report_quality`+`test_report_version` 44 全绿。
- 三场景复跑：normal_growth `verified`（证据 4 / 缺口 0）、loss_mixed_units 与
  missing_footnote_asof 按契约 `draft`；`evidence_located_min=4` 等冻结期望全部通过
  （更严的计数下 normal_growth 仍是 4 条带定位小节）。
- 前端 `npm run build` 通过；面板守卫新增"部分支持/线索计数"断言。
- 改动的断言（2 处，均按新口径写实）：`test_explanation_present_is_not_reported_as_missing`
  （原断言"全部指标 has_explanation=True"）改为收入真、利润/现金流假；
  `test_target_claim_needs_issuer_support` 的 reason 断言从固定文案改为点名缺项。

## 5. 验收轮：用实机真实产物复核（只读，离线）

对实机任务 `ui-31305a2b28` 的工作区做只读复核（不写任何产物、不联网）：

- 底稿 18 行 + 11 派生行**全部带 `fact_id`**（前置缺陷已修；此前 rows 无 fact_id → 行级事实永远绑不上）。
- 采纳正文的分析节只有 214 字（标题 + 口径声明，无数字）→ 主张 0 条（如实：该节没有可验证读数）。
- 模型原始草稿（`eb4efbcb`，分析节 891 字、含真实数字）→ 5 条主张，逐条断言读数：
  - 核心指标整句（收入/利润/现金流 + 三项同比）**6 个断言全部 supported**（就近指标归属在真实多指标句上正确）；
  - 覆盖率句：`cashflow_coverage` 61.2 / 69.37 supported，8.17 个百分点由两期水平相减复核 supported；
  - 百分点差（2.09 / 7.13 / 2.18）全部由"两期水平相减"复核 supported；
  - 同句多年度且数值不紧跟年份的（归母净利率 30.24 / 23.11）→ needs_check（不借远处年度）；
  - 句中未标年度的（资产负债率 25.42 / 23.24）→ needs_check（未标期间）。

**本轮复核发现并修掉的 3 个 D1 实现缺陷 + 1 处语义门**（均已在 `b2bb55b` 之后的提交里修好）：

1. **百分点单位类别失效**：`_unit_class("个百分点")` 返回空（字面不含 %，又落到金额分支）→
   "下降 2.09 个百分点"被记成"未标单位"。改为先判"百分点"。
2. **跨句年份误归属**：期间回退取"窗口里最近年份"，把"归母净利率由 30.24% 降至 23.11%"的
   30.24 记成 2024 年。改为：只认紧邻数值（≤20 字）的年份；整句只有一个年度才允许回退；
   同句多年度且不紧邻 → 期间不明确（needs_check）。
3. **覆盖率别名漏匹配**：正文写"经营活动现金流净额对归母净利润的覆盖"，别名表只有
   "经营现金流对归母净利润的覆盖" → 落到内层"归母净利润"上。补齐两条别名；
   百分点断言的指标归属窗口放宽到整句前缀（8.17 个百分点离指标名较远）。
4. **语义门（更根本）**：必需语义不全（缺指标/单位/期间）时**不再尝试匹配**——
   此前期间不明确仍会按数值绑上某一期，这正是误绑机制；现在一律待核查并写明缺哪项。
5. **补强**：百分点差改由**同指标两期水平相减复核**支持（记录参与相减的两期 fact_id 与差值），
   差值或方向不符即 unsupported。

**发现的一处上游缺陷（D3 范围，未在本批修）**：底稿把"毛利率"的单位记成**亿元**
（`gross_margin 2024年 73.16 亿元`、2023年 75.25 亿元，见 `rows_detail`）。主张侧因此
正确地拒绝用 `%` 断言绑定金额单位事实（报"底稿中没有匹配的事实"）——**比率被记成金额**是
底稿/适配器的单位映射问题，报告表格里也会显示成"毛利率 73.16 亿元"。

冻结用例随之扩到 **11 个**：新增 `c7_real_draft_paragraphs`（实机真实草稿段落，钉住
覆盖率别名、百分点重算、跨句年度不借用），`before`（`faa9dba`）**0/11**、`after` **11/11**。

## 6. 定向验证（更新）

- `scripts/claims_d1_cases.py check`：11/11；`TestClaimAssertionSupport` 6 项（含冻结重放、
  三条审查反例、无数字判断进清单、语义门、百分点重算）。
- `test_delivery_chain` **258**、`test_narrative_evidence` 39、`test_frontend_guards` 45、
  `scenario_checks` 17、`test_offline_delivery` 32、`test_report_quality`+`test_report_version` 44 全绿；
  三场景复跑全部通过（normal_growth `verified`、证据 4）。

## 7. 未验项

- 真实年报 MD&A 的**正向**经营变化解释（披露原文 → 解释已取得）仍未取得：现有真实摘录
  不含带因果语言的 MD&A 正文（D3 处理）。
- 面板/导出对**当前交付稿**的同版投影未做（D2）；本批只保证结构对象与新口径一致。
- 根任务调用账本与截止（D5）未做；本批无实机运行。
- 底稿"毛利率单位记为亿元"的上游缺陷未修（D3）；在修好前，比率类主张遇到金额单位事实
  一律不支持（宁可待核查，不误绑）。
- 括号式同比（"降幅（-33.38%）"）未做专门识别：因句内无年度仍判待核查；已记录，不猜。
- 目标类判断的"目标发布时点"目前由"发行人披露原文支持"整体表达，未单独取披露日；
  真实正向样本要等 D3 的年报材料。
