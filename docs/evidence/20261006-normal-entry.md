# 10-06 正常入口复验（复核 §5-3）：一次选版导出 → 核同版正文/PDF/ZIP 与移出内容

> **入口**：真实工作台（`http://127.0.0.1:8080`，浏览器标签组「织光正常入口复验（10-06）」，**由用户本人登录**），
> 全程走**页面/HTTP 正常入口**，未新建付费任务、未联网取证、未调模型：
> ① `POST /api/task/ui-10ea37599d/analysis/adopt`（分析工作台「采纳这版」，确定性装配）→
> ② `POST /api/task/ui-10ea37599d/package`（「按当前版本重新导出」）→
> ③ `GET /api/task/ui-10ea37599d/report.md`、`GET /api/task/ui-10ea37599d/pdf`（报告/下载入口）。
> 旧原件保留：旧包 `deliverables_20261006_145425.zip` 与旧版本仍在工作区（新包是新时间戳文件）。

## 一、结果（`result: pass`，见 [20261006_normal_entry_verify.json](20261006_normal_entry_verify.json)）

| 核什么 | 结果 |
|---|---|
| 交付包 | `deliverables_20261006_220706_300c3c.zip`（5,827,119 字节，25 个清单成员 + 包内清单） |
| 同版（正文） | 包内 `reports/report.md` sha256 `2ee6405d…` ＝ 包内清单 `delivered_md_sha256` ＝ **下载入口 `/report.md` 实际字节** |
| 同版（PDF） | 包内 `reports/report.pdf` sha256 `dc2a2542…` ＝ 清单 `pdf_sha256`；**9 页**，第 1 页即 `## 关键判断与下一步`，全文无「三项主判断」 |
| 版本身份 | 包内清单 `report_version_id = ababd6f7…` ＝ 该次采纳正文的版本身份（不是"最新 zip"顶上） |
| 唯一首屏 | 正文 11,952 字符；二级标题顺序：关键判断与下一步 → 研究问题与下一步 → 财务对照 → 图表 → 变化解释 → 风险与核查 → 附录；`## 三项主判断`／`## 关键发现`／`## 分析`／`## 分析摘要` **均不在正文** |
| 移出内容保全 | 包内 `analysis/analysis_detail.md` 带主文收束块 **25,076 字节**（财务对照长尾明细表／量价与结构／业务背景／分析摘要／经营驱动分析正文／ID 详表），另有 `analysis/analysis_cards.md`、`evidence/citation_evidence.json` |
| 逐成员自检 | 25 个成员 hash 全部与包内清单一致（`verify` 全 `ok`），无清单外成员 |
| 机器验收 | `overall: pass`；`number_traceability` **79.9%（135/169）**，其中"计算（可复算）" 45 条（上一版 28 条） |

可视证据：`pdf_page_1.png`（导出包 PDF 第 1 页，即唯一首屏）、`pdf_page_4.png`、`browser_screenshot_1.jpg`
（工作台「项目结果」页；**其正文内容另经 DOM 文本核对**：页面含「利润下降的主因在毛利端」、
标题顺序以「关键判断与下一步」开头、无「三项主判断」与悬空『关键发现』指针——本机模型不能看图，
所以不把"我看过图"当证据）。导出包 PDF 原文另存 `deliverables_20261006_210410_5a297c.pdf`。

## 二、正常入口逼出来的四个真缺陷（都已修，附守卫用例）

1. **「采纳这版」在收束后的正文上恒失败**（`web_ui._post_task_analysis_adopt` 的绑定校验）：
   它只查 `run_id[:12] in 装配出的正文`，而 10-06 主文收束已把带运行标识的
   「分析摘要选择说明／经营驱动分析正文」移进 `analysis/analysis_detail.md`，于是恒判
   「采纳正文里找不到所选运行」并回滚（**本机实测原样复现**：`232b6c38335c` → 409）。
   修法：绑定校验改读**主文 ＋ 收束移出块**（与交付硬门槛同一实现
   `delivery_pipeline._gate_analysis_text`）——被移出的内容仍是**同一份交付**的一部分。
   守卫：`test_review_edit_api.TestAnalysisWorkbenchEndpoints.test_adopt_binding_reads_the_moved_argument_line_too`
   （同时验"没有收束块时按主文判"，不放宽）。
2. **正文里有 4 处悬空引用**：3 处「完整读数见『关键发现』」＋1 处「见开头『关键判断与下一步』与
   『关键发现』」，而 `## 关键发现` 已被唯一首屏取代（另有 1 处「比率适用条件…见附录」，
   该节也已移入附件）⇒ 读者被指到不存在的小节。
   修法：指针按**实际渲染出来的那一节**写（有唯一首屏就写『关键判断与下一步』，旧结构仍写
   『关键发现』；比率条件改为按标题指向并说明完整附件随包）。
   守卫：`test_financial_analysis.TestX0DeliveryClosure.test_no_pointer_to_a_moved_section`
   （新路径无悬空指针 ＋ 旧结构行为不变）。
3. **首屏"亿元"读数判不成"可复算"**（`acceptance_checker._match_derived_fact`）：该函数一边按
   单位缩放比数值（`abs(v)*scale vs abs(fv)*f_scale`），一边却要求**缩放系数完全相等**
   （`abs(f_scale - scale) > 1e-9 → 跳过`）⇒ 正文按读者口径写"33.43亿元"、底稿以"元"记派生事实时
   **永远配不上**。10-06 实测：唯一首屏的 33.43／38.01 亿元全部落在"不可溯源"示例里，
   整表移出正文时溯源率跌到 **67% 判死**。
   修法：改判**量纲**（金额 vs 百分比不得互认；同一量纲内 元/万元/亿元 可换算，与 `_num_forms`
   的既有等价约定一致）。效果：`number_traceability` 78.8%(204/259) → **79.9%(135/169)**，
   "计算"命中 28 → **45**，而分母更小（正文更短）。
   守卫：`test_p0` 新增 `test_derived_amount_matches_across_amount_units_but_not_percent`
   （元/万元/亿元互认；百分比事实不得给金额读数背书，反之亦然）。
4. **财务对照 61 行占 4 页**：正文只留**被引用/被算式用到**的指标行
   （`report_brief._split_table_rows`：读者指标登记表 ∪ 派生算式操作数 ∪ 正文已出现的 label），
   长尾明细移附件『完整指标表（其余指标）』并留一行指针（原始值仍在包内 `working_paper.csv`）。
   效果：正文 13,838 → **11,952 字符**，PDF **11 → 9 页**，验收仍 `pass`。
   守卫：本轮以**副本装配 + 正常入口两次实测**为准（不改验收阈值、不删读数）。

## 三、照实登记的未验项

- **人工复核未做**：本轮只走机器验收（`acceptance_overall: pass`）与正常下载入口；
  `human_review.json` 未动，研究状态仍是 `research_draft`（正文自带研究草稿说明）。
  三条状态轴（机器验收／研究状态／人工复核）保持独立，未互相背书。
- 研究状态绑定里**缺执行契约指纹** ⇒ 导出清单如实标 `research_state.stale: true`、
  「计数不作为结论」；这是状态本身的记录，不改机器验收结论。
- **主文 9 页（目标 4–6 页，本轮从 11 页降到 9 页）**：可动的三个大头都被既有约束封住——
  `## 研究问题与下一步` 在 `COMPACT_MOVE_TITLES` 的**禁移**名单里；61 行事实表整表移出后
  **溯源率实测跌到 67% 判死**（本轮量化确认：分母 259→169、可溯源 204→114，删掉的
  全是可溯源的原始行），所以改成"只留被引用/被算式用到的行"（配合缺陷 3 的修法后
  溯源率反而升到 79.9%）；图表已限 3 张。再往下压需要架构侧决定"哪些必要边界可以离开正文"
  （或把溯源判据改成"同版附件可计"），不适合由代理单方面删。
- **目标兑现判断仍缺**：缓存 2024 年报的「2025 年经营计划」是定性表述、无可量化目标，
  不硬凑成一条判断（登记为缺口）。
- **三一复制是离线结构复制**（见 [20261006-sany-replication.md](20261006-sany-replication.md)），
  不是第二份已验证交付，也未走正常入口。

## 四、本轮另记一处**基线就存在**的开放缺陷（不是本轮引入，未擅自改语义）

`test_offline_delivery.test_body_model_failure_keeps_recomputable_paper`（"报告步骤失败的任务
**不得** verified"）**在基线 `92c359f` 上单独跑就是 FAIL**（用 `git worktree` 在 92c359f 复现：
`AssertionError: 'verified' == 'verified'`），本轮仍未修，机制已定位：

- 报告步骤失败时，交给装配的**模型正文**是失败文本（里面**没有** `## 分析`）；
- `report_brief._analysis_gate_verdict` 对"正文没有 `## 分析`"走**免伤分支直接返回 True**
  （该分支有 `test_r0_boundaries` 的用例保护：老式/简版研究正文不该因此被判死）；
- 于是结构对象里 `analysis_gate_ok=True`，而 `delivery_pipeline._has_deliverable_analysis`
  又接受"有读者判断就算有分析"（`reader_judgments` 由**确定性**装配产出，报告步骤失败也在）
  ⇒ 判成有可交付分析 ⇒ 交付 `verified`。
- **结论**：内容判据看不见"模型正文这一步失败了"这件事；要恢复该保证，必须把**步骤失败信号**
  显式传进装配/门槛（例如 `assemble_and_verify(..., report_step_failed=True)` →
  `apply_research_hard_gate`），不能靠正文标题或结构内容去猜。
  本轮不改：它牵涉装配 API 与 `test_r0_boundaries` 的免伤语义，属需要架构侧定的产品决策。
