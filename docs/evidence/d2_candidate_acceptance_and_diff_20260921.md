# D2（第二批）：候选先验收再比较 + 可解释差异记录

2026-09-21。承接 D2 第一批（`4eb3b36`）。范围：`report_quality.py`（质量向量与比较分支）、
`orchestrator_v2.py`（唯一采纳点）、测试。全程离线（不新增实机、不调用模型）。

## 1. 问题（阶段 D 前置审查 P1）

`_adopt_candidate` 只把候选登记成版本（`acceptance` 为空）就交给 `compare_versions`：
两稿按"未知 vs 未知"并列，**空验收被当成"两稿同分"的证据**；比较只有硬约束四级
（验收等级→缺口→占位→回抄），硬条件相同时没有任何"内容质量"依据。

## 2. 实现

### 2.1 候选先完成自身验收（`orchestrator_v2._accept_candidate`）
- 采纳前用 `_accept_fn_for(task_id, goal)`（trigger=最终装配、`prefer_body=True`）对
  **候选正文**跑一次确定性验收；验收器可能把它修成诚实披露版（研究任务还会装配成简报），
  那就**以那一版作为候选**——验收对象 = 比较对象，采纳与返回都用这一版。
- 验收不可用/异常 → 记日志并按"未知"继续比较（不阻断反思，也不把未知当通过）。
- 两个调用点（整轮反思 / 单步重做）都传 `goal`。

### 2.2 质量向量（`report_quality.candidate_quality`）
同一套确定性口径、与主张记录同源：读底稿事实（`chart_rows`）→ 取分析一节 →
逐条断言判支持状态。字段：
`analysis_ok`（≥1 数据观察 + 意义/局限）、`observations`（**独立句子**去重、上限 10）、
`claims`、`bound/partial/unsupported/needs_check`、`unsupported_or_unchecked`、
`duplicate_blocks`（规范化后重复且 ≥20 字的行数）。读不到底稿/正文 → `{}`（未知，不参与）。

### 2.3 比较分支（`compare_versions(..., cur_quality, cand_quality)`）
硬约束四级之后、最终"保持当前"之前，按**可解释顺序**比较向量：
① `analysis_ok` 满足者胜；② 未支持/待核查结论更少者胜；③ 分析观察更多者胜；
④ 重复块更少者胜；⑤ 全同 → 保持当前，理由同时列出向量读数与"长度不作为依据"。
不传向量时行为与旧实现完全一致；**长度始终不参与判定**。

### 2.4 差异记录（`orchestrator_v2._record_quality_decision`）
每次选稿把 `{at, task_id, cur_version, cand_version, cur_acceptance, cand_acceptance,
reason, adopted, cur_vector, cand_vector}` 追加到工作区 `report_quality.jsonl`
（只含版本号/验收结论/理由/向量，不含正文与提示词），可离线追溯"为什么这一版被采纳/保留"。

## 3. 验证读数

- `test_report_quality` 32 项：向量五个分支各一条（`analysis_ok` 反转、未支持 3→1、
  观察 2→4、重复块 3→0、全同保持并提长度）+ 不传向量时旧行为不变。
- `test_delivery_chain.TestCandidateAdoption` 3 项：
  ① 候选先验收 → 采纳的是**验收修正版**、选中版本即被比较的那一版、`report_quality.jsonl`
  写入一条含向量的采纳记录；
  ② 验收抛异常 → 不阻断，按未知继续采纳；
  ③ 硬条件相同 → **重复块更少**的一稿胜出，理由含"质量向量更优"，且"同一观察写两遍"
  在向量里只算一条（`observations` 相等）——计数按独立主张去重。
- 全量：`test_delivery_chain` 271、`test_task_time_optimization` 11、`test_report_quality` 32、
  `test_report_version` 18、`test_narrative_evidence` 39、`test_frontend_guards` 46、
  `scenario_checks` 17、`test_offline_delivery` 32 全绿；三场景复跑全过。

## 4. 连带行为变化（如实记录）

采纳点现在会写验收快照（`acceptance_report.json`），于是**反思收敛判据读到的验收结论
是候选自己的**（此前读的是文件里可能过期的结论）。`test_task_time_optimization` 里那条
"验收 fail 时不得因长度不变提前收敛"的用例原先靠**预置一个 fail 文件**，会被新流程覆盖；
已改为把验收桩设成 `fail`（不变量不变，测的仍是"验收未通过必须继续重做"）。
语义上这是改进：收敛与否跟着**当前稿的验收结论**走，而不是跟着一个可能属于旧稿的文件。

## 5. 未验项

- 真实反思循环里"候选经验收修正后被采纳"的端到端读数：本批只有桩测试；真实读数要等
  D5（根任务账本与截止）通过后的有界实机。
- 向量口径：`observations` 上限 10、重复块按"行"判定、未支持/待核查未按问题类型加权——
  阈值未经真实分布校准；D6 的固定回归包会给分布。
- 向量只覆盖"分析一节"的观察与重复；跨章节（关键发现 vs 分析）的同义复述仍未度量。
- 验收在采纳点的开销（每候选一次确定性验收，秒级）未做预算/超时约束。
