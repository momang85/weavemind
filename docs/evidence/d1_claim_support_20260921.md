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

## 5. 未验项

- 真实年报 MD&A 的**正向**经营变化解释（披露原文 → 解释已取得）仍未取得：现有真实摘录
  不含带因果语言的 MD&A 正文（D3 处理）。
- 面板/导出对**当前交付稿**的同版投影未做（D2）；本批只保证结构对象与新口径一致。
- 根任务调用账本与截止（D5）未做；本批无实机运行。
- 目标类判断的"目标发布时点"目前由"发行人披露原文支持"整体表达，未单独取披露日；
  真实正向样本要等 D3 的年报材料。
