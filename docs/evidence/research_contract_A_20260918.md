# A 批证据：研究契约、事实选择与认证门槛（2026-09-18）

依据《阶段复核与下一步指令_20260918.md》§A。**全部为离线证据**：没有真实运行、没有联网、
没有改模型/权限/门禁。反例先以失败测试落库（下附当时的失败输出），实现后同一批测试通过。

## 一、反例 → 修复对照（每条都是当前生产函数的内存反例）

| # | 反例（架构复核原文） | 改前实测（失败测试输出） | 改后 |
|---|---|---|---|
| 1 | 真实快照六条必需事实口径未知，却 `paper.ok=True`、无缺口、同比单位继承"亿元" | `AttributeError: working_paper has no attribute 'PROBLEM_CALIBER'`（无此判据）；`'亿元' != '%'` | `REAL_SNAPSHOT` **转为反例**：口径未知 → `PROBLEM_CALIBER` → `ok=False`；同比单位 `%` |
| 2 | 正确茅台事实后追加"比亚迪 2024 营收 9999 亿元"，底稿仍通过、茅台同比变 564.12% | `564.12 != 15.66`（复现成功） | 跨公司记录被 `select_facts` 排除（记 `PROBLEM_UNRELATED` + `selection.unrelated`），同比仍 15.66% |
| 3 | 候选顺序决定结果 | 比亚迪在前 → 15.66%；在后 → **564.12%**（同夹具两种答案） | 两种顺序结果完全一致（同比、完备度、问题种类均相同） |
| 4 | `write_working_paper` 用抓取的公司名/代码反向填请求 | 契约公司 = 抓取到的公司 | 契约来自用户请求；抓取元数据只作候选，不一致记 `PROBLEM_UNRELATED` |
| 5 | 非相邻期被标成同比 / 基期为 0 静默跳过 / 缺来源位置在 `verify_state=unknown` 时不拦 | `True is not false`（2022→2024 产出"同比"行） | 非相邻期记 `PROBLEM_PERIOD` 且不出派生行；基期 0 记 `PROBLEM_NOT_COMPUTABLE`（"不可算"）；缺 `source_url` 一律记 `PROBLEM_UNVERIFIED` |
| 6 | 同一组合两条冲突候选靠列表先后取用 | 无此判据（拿不到冲突信息） | 记 `PROBLEM_CONFLICT`，并在 `selection.conflicts[].candidates` 里**连值一起列出**（`fact_id` 与值无关，只给 id 分不出两条） |
| 7 | 文档主体作用域未绑定（A 公司标题 + 行内无公司名 + 别公司同值来源） | `AttributeError: no attribute 'document_subject_scope'` | `document_subject_scope`（标题 + 承载必需数值的作用域）；标题是别家公司、或作用域缺失 → `PROBLEM_DOC_SCOPE` |
| 8 | 底稿缺口只当文本附注，`hard_fail` 全仓无人赋值 | `_delivery["hard_fail"]` 恒为空串（谓词那一分支不可达） | `_apply_research_hard_gate` 写入 `hard_fail` → `verified_delivery` 返回 `draft` → 终态 `SUCCESS_WITH_ISSUES` |

改前失败输出（节选，`python -m unittest test_working_paper`，11 红）：

```
FAIL: test_cross_company_record_does_not_pollute_or_pass
AssertionError: 564.12 != 15.66 within 2 places (548.46 difference) : 本公司同比不得被别家公司记录改写
FAIL: test_candidate_order_does_not_change_outcome
  [('revenue_yoy', 15.66)] != [('revenue_yoy', 564.12)]
FAIL: test_yoy_unit_is_percent_and_inputs_keep_amount_unit
AssertionError: '亿元' != '%'
FAIL: test_non_adjacent_years_are_not_labelled_yoy
AssertionError: True is not false : 非相邻期不得产出同比行
ERROR: test_unknown_caliber_does_not_pass  AttributeError: ... 'PROBLEM_CALIBER'
ERROR: test_scoped_report_passes           AttributeError: ... 'document_subject_scope'
```

## 二、A1 契约先落库（请求主体不被抓取覆盖）

- 表单送结构化字段（最小集）：公司、稳定标识（可选）、**市场（新增下拉）**、起止年、口径、
  截至日、参考资料；指标固定三项核心。`buildResearchGoal` 现在同时返回 `fields`，
  **市场未选不填默认值**（字段里没有这一项，不替你猜 A 股）。
- 提交链路：`TaskConsole.submit` → `POST /task`（`_sanitize_research_request` 白名单/枚举/限长）
  → `_publish_task`（随 Redis 下发）→ `accept_task_request` → `task_state.mark_queued`
  → 新列 `research_request_json`（走既有 `_add_missing_columns`，与 `acceptance_json` 同一模式）。
- `write_working_paper` → `resolve_request()`：**优先读落库契约**（`request_source="stored"`），
  没有才从目标解析（`"text"`）；抓取到的公司名/代码只作为 `candidates` 参与比对。
- 新增 `facts.check_subject`：稳定标识优先（`600519`/`600519.SH`/`00700`↔`700` 归一），
  其次名称去后缀互为包含；任一侧缺主体 → 不一致（缺主体不得算已核验）。
- 文本入口的**已知弱项（如实记录）**：`task_classifier._extract_company` 对
  "研究贵州茅台 2023 与 2024 年营业收入"给不出公司，对全称还会截成"台酒股份有限"；
  "贵州茅台（600519.SH）…"能正确取到。所以文本入口识别不出时**留缺口**（不猜），
  正式路径是表单的结构化契约。本批不改抽取器。

## 三、A2/A3 选择与认证

- 新增 `working_paper.select_facts(facts, request) -> Selection{selected, unrelated, conflicts}`，
  **校验（`by_combo`）与计算（`by_metric_year`）共用同一份结果**（此前一边先到先得、一边后到覆盖）。
- 口径：`request.caliber` 已知时事实必须声明且相符；未知或不同 → `PROBLEM_CALIBER`。
  字段语义修正：`structured_pipeline` 此前把**报告期描述**（`_caliber_tag` → "年报口径（…）"）
  写进 `row["caliber"]`，现改到 `row["period_label"]`；`caliber` 只保留来源真声明
  （行级 > 实体级 > unknown，不默认合并）。适配器目前**没有任何一家**声明合并/母公司——
  因此 A 批的已知口径正例来自"行内声明 caliber"的合成夹具；东财口径是否可声明留到 C 批取证。
- 来源要求：必需组合没有 `source_url` 一律 `PROBLEM_UNVERIFIED`（`verify_state=unknown` 也不例外；
  结构化定位符只说明"取自哪一格"，不算来源位置）。
- 同比：仅相邻年度、同年报、同币种同单位；分母 0 明确"不可算"；`derived_fact` 支持显式
  `unit`/`unit_source`，同比落 `%`（输入行仍保留"亿元"）；`paper_csv` 派生段补主体与单位列。

## 四、A4 交付硬门槛

- 位置：`orchestrator_v2` 收尾 `write_working_paper` 之后、`report = delivery + … + detail` **之前**。
- 触发（仅研究任务）：存在落库契约，或本次确实产出了底稿。以下即写入
  `_delivery(tid)["hard_fail"]`：底稿被跳过（有契约却无结构化事实）、底稿产出失败、
  `paper_ok=False`（含口径/主体/期间/单位/来源/冲突/跨公司问题）、文档主体作用域未绑定。
  普通非财务任务（无契约且无底稿）**不进门槛**。
- 效果链复用既有语义：`verified_delivery(hard_ok=False)` → `draft`；`_delivery["reason"]`
  → 页面/`derive_status` → 终态 `SUCCESS_WITH_ISSUES`；交付物里带
  "> **研究交付硬门槛未通过**：…"。
- 边界（未做）：契约缺失且底稿也缺失时，无法判定"这是不是研究任务"，因此不触发"底稿缺失"这一条
  （不猜任务类型）；文档主体作用域只覆盖三项核心指标，不做通用自然语言证明。

## 五、定向测试结果（未跑全仓作为本批前置）

```
python -m unittest test_r0_boundaries test_working_paper test_facts test_fact_fidelity \
                   test_review_edit_api test_task_state test_task_projection test_report_version
→ Ran 159 tests ... OK

python -m unittest test_offline_delivery        → 9 tests OK（含新增负向对偶）
python -m unittest test_delivery_chain test_orchestrator_v2 test_p0                    test_acceptance_adversarial test_report_quality test_task_time_optimization
                                                → Ran 708 tests ... OK
node --experimental-strip-types --test tests/*.test.mjs → 36 tests pass（含新增 4 条字段用例）
python test_frontend_guards.py                  → 36 tests OK
npm run build                                   → 干净重建通过
```

正例（合成夹具，行内声明口径）：六个组合齐备、三个同比可独立复算、`paper.ok=True`、
同比单位 `%`、输入保留 `亿元`；**没有把 unknown 硬改成合并来变绿**。

## 六、未验证项（不要按已完成记账）

- **未做真实运行**：本批全部为离线反例与测试；真实抓取/真实报告质量仍未闭环（C 批）。
- **东财（A 股）接口的报表口径仍未声明**：需要取证（与年报原文对齐）后才能在适配器里声明
  `caliber=合并`；在那之前真实快照的底稿只能是"口径未知 ⇒ 不达标"。
- 文本入口的公司抽取弱（见 §二），首发正式路径以表单结构化契约为准。
- 观察（未修，非本批范围）：离线 `run()` 默认 `project="default"`，而 `task_workspace(tid)`
  是平铺路径——两处作用域不一致会让夹具/产物落在不同目录（本批只按真实读取路径写夹具）。
