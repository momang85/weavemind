# C2 问题/证据需求/报告表达共用契约（2026-09-27，第一批）

目标（指令 §4-C2）：问题、证据需求与报告表达读**同一份契约**；完成条件按"问题真正问什么"
算；补利润/现金的正常装配；主体类型适用性四用例；方向与单位取同一事实对象；旧批准不继承。

## 1. 判据表与问题类型（`question_assessment`）

- 新增四类问题与**判据表** `QUESTION_RULES`（每类写清"需要什么/什么算完整/部分/没有/证据性质"）：
  `numeric_change`（可复算的两期同口径事实）、`issuer_explanation`（原披露逐项对应）、
  `quant_decomposition`（问题要求的**每个组成部分**都有量化披露）、`relation`（两条序列 + 可复算关系）。
- 固定三问的类型：收入=量化分解（量/结构/价）、利润=量化分解（毛利端/期间费用/税项与非经常性）、
  现金=`relation`（经营现金流净额与归母净利润**两期同口径事实**）。`question_set()`
  生成 ≤3 问并带 `requires`（逐项证据要求）与规则原文。
- **覆盖由判据表算**：`_coverage_by_rule(components)` 只看每个组成部分的 `state`；
  材料载荷自报的 `coverage="full"` 不再参与判定（新用例 `test_declared_full_is_not_trusted`）。
- 定性归因只能到 partial；发行人解释类问题逐项对应才 full（笼统归因 partial）。

## 2. 装配（`report_brief`）

- 收入：`volume_price.components`（量/结构**已绑定**，价**未取得**——发行人未披露价格口径）。
- 利润：新增 `narrative_evidence.extract_profit_decomposition`（真实年报的毛利率表 + 费用明细行，
  按行解析、按行给定位）：毛利额与期间费用合计是**推导量**（上期由披露同比反推，公式随事实记录）；
  毛利端**只取一组切法**（各分组是同一笔收入的不同切法，跨组相加会重复计入——冻结样本反例抓到过）。
- 现金：两条序列的同口径判定（`_series_state`：同来源或同一已知口径；缺一期/口径不同即 missing）。
- 实机读数（洋河任务 `ui-706c5ef4a5`，真实材料）：
  - 收入 `partial`「已取得 量、结构；缺 价」；利润 `partial`「已取得 毛利端、期间费用；缺 税项与非经常性损益」；
  - 现金 `full`（relation，两条序列两期事实已绑定）；必答 **1/3 完成、2/3 部分**，研究状态仍 `research_draft`。

## 3. 方向、单位与表达

- 方向措辞取**同一事实对象**（`_metric_direction`：两期值决定上升/下降/持平；负基数不给百分比方向）
  → 意义/资料计划两处固定文案改为 `{change}` 模板（实机平安反例：三项都增长却写"降幅/收入下降"）。
- 修复 `research_state` 理由串里"部分覆盖…[分解覆盖充分]"的自相矛盾表述（改为"已取得部分构成"）。
- 证据性质进**结构对象**（`question_assessments[*].rule.nature`，页面/导出清单读它），主文不加行
  （原因见 §5）。

## 4. 主体类型适用性（四用例）

- 新增 `facts.subject_type_state`：**只有用户声明**才算确认；名称线索是建议；其余未确认。
- 类型未确认时：企业口径比率（净利率/资产负债率/研发强度）仍给出但**不标"适用"**
  （`applicable=None` + 原因），补材料建议**先要类型确认**；金融主体（声明或名称线索）
  不生成企业口径比率，建议清单换成"财务报表及附注原文/口径说明/管理层讨论原文"。
- 四个固定用例（`test_subject_type_applicability_four_fixed_cases`）：中国平安、仅代码 601318.SH、
  声明金融的 601318.SH、银行对公视角研究**非金融**主体——逐项检查派生指标、比率适用标注、
  下一步建议与"确认类型"提示。
- 覆盖关系（现金对利润的覆盖倍数）**不受**类型确认影响：它是两个已绑定事实之间的关系，
  质量判断仍由边界句挡住（改回后 D6 冻结场景的三个场景全部恢复通过）。

## 5. 未验 / 未做（如实留痕）

- **负基数问题**：同比不可算时不单独出问（底稿已把"基期为负、可比性不成立"记成底稿问题）。
  曾经尝试"照样出问并给绝对额"，会把 PDF 分页推到多一页近似空白页（冻结场景 `pdf_parseable` 反例），
  本批按"不改判据"处理：**不加问**且如实记录该取舍；方向措辞与绝对额表示已在底稿/缺口里给出。
- 主文**未加**"证据性质"行（同上分页原因），该信息在结构对象与页面数据里可用。
- 未做：首屏"四处重复警示"合并（前端）、两次装配稳定性的结构级断言（当前只有评估级）、
  "编辑→重验→下载同版"的实机复跑（机制已有，回归在 `test_review_edit_api`）。

## 6. 验证与文件

- 判据表/类型/契约问题集/确定性：新增 `test_question_assessment.py`（20 例；本地按 CI 口径单跑通过）。
  当时工作区 `ci.yml` 里已存在一步指向该文件的**既有未提交改动**——作者未核实其来源，按既有改动
  记录，不归为"架构师改动"；本批未接管其提交。H0 已把该步骤精确纳入 `8a44f85`（此前已提交的
  CI 不含它：HEAD 46 步 → 纳入后 47 步；仅暂存本批 hunk，未使用 `git add .`）。
- 装配与表达：`test_delivery_chain`（四用例、方向措辞、负基数、证据性质、覆盖计数）、
  `test_narrative_evidence`（判据表驱动的分解 full/partial、自报 full 不作数）。
- 冻结场景：`test_offline_delivery` 全场景通过（含 `pdf_parseable`）。
- 回归：`test_offline_delivery`+`test_delivery_chain`+`test_narrative_evidence`+
  `test_question_assessment`+`test_working_paper`+`test_facts`+`test_report_quality`
  共 **636 例 OK**。
- 文件：`question_assessment.py`、`narrative_evidence.py`、`report_brief.py`、
  `delivery_pipeline.py`、`execution_contract.py`、`facts.py`、`test_question_assessment.py`、
  `test_delivery_chain.py`、`test_narrative_evidence.py`、本文件。
