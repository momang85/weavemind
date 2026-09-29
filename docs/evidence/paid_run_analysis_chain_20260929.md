# 付费整跑：分析链进交付（2026-09-29 夜间）

> 提交方式与前端一致：`scripts/research_acceptance_run.py --submit`（走 `POST /api/tasks`
> 鉴权之后的同一个 `web_ui._publish_task`），目标：贵州茅台 600519.SH 2023 与 2024 年度
> 营业收入/归母净利润/经营活动现金流净额，合并口径，数据截至 2025-04-30。
> 本轮**两次**整跑都在本文件留下原始读数（预算按"修完再验"用）。

## 0. 结论

| 项 | 第一次 `ui-fa2cb73e59` | 第二次 `ui-603f626cbe` |
|---|---|---|
| 任务终态 | **FAILED** | **SUCCESS** |
| 交付状态 | verified（`hard_fail` 空、`aligned` 真） | verified（同左） |
| 机器验收 | pass（绑定本版正文） | pass（绑定本版正文） |
| 分析运行 | 3 模型 + 4 比率 validated | 3 模型 + 4 比率 validated（`analysis_runs.json` 26,974 B） |
| 必需事实 | 6/6 带来源定位 | 6/6 带来源定位；同比 11 条；缺口 0、问题 0 |
| 失败原因 | **第二轮反思新增的抓取 `i2-r2` 无候选 URL → 连锁标死 `i2-r3`/打包** | 无（检索/抓取失败已被当作缺口，不阻塞） |

即：**交付这一层第一次就修好了**（正文有分析结论、验收 pass、清单与正文字节对齐）；
第一次的 FAILED 是**反思轮新增步骤**仍在硬失败链上，第二次修完即通过。

## 1. 第一次：`ui-fa2cb73e59`（13:26:10Z → 13:41:59Z，949s）

固定研究路径（主链）**全部走通**：

```
1  web_search      SUCCESS（实为重规划后的 alt-1 content_summary）
2  web_fetch       SUCCESS（alt-2）
2b web_fetch       FAILED  {"status":"failed"}          ← 计划里 optional，已不阻塞
3  content_summary SUCCESS
3a data_analyzer   SUCCESS                              ← 本轮修复的断点
4  report_generator SUCCESS
package-7          SUCCESS
```

交付读数：`status=verified`、`hard_fail=""`、`acceptance_overall=pass`、
`acceptance_for_this_body=true`、`review_state.verdict=PASS`（绑定同一
`report_version_id=e5ae7ff6…`）、正文 `9840d2ad…`、交付记录
`delivered_sha256=e69f6eef…` 与正文一致；导出 MD 33,979 B / PDF 5,587,233 B /
CSV 12,192 B / JSON 44,670 B。

**终态 FAILED 来自第二次反思轮新增的补洞步骤**：

```
i1-1  web_search       SUCCESS（alt）      i2-r1 web_search  SUCCESS（alt）
i1-2  content_summary  SUCCESS             i2-r2 web_fetch   FAILED 无候选 URL（检索未产出可用来源）
i1-3  report_generator SUCCESS             i2-r3 report_generator FAILED Blocked by ['i2-r2']
package-4             FAILED（首轮）        package-4         FAILED Blocked by ['i2-r2','i2-r3']
```

`i2-r2` 没有 URL（检索为空），命中"无候选 URL 就不派发抓取"的前置判定 → 判失败 →
**连锁**把报告与打包标死 → `has_failure=True` → 整单 FAILED，而交付本身是好的。
这就是"计划里 optional 的语义没有覆盖到反思轮新增步骤"。

## 2. 第二次：`ui-603f626cbe`（13:55Z → 14:17:57Z，1320s）

```
1  web_search      SUCCESS（alt-1）  2 web_fetch  FAILED 无候选 URL  2b web_fetch FAILED 无候选 URL
3  content_summary SUCCESS           3a data_analyzer SUCCESS        4 report_generator SUCCESS
package-7 SUCCESS   i1-1/2/3 SUCCESS   package-4 SUCCESS
```

- 非可选步骤全部 SUCCESS → `has_failure=False` → 终态 **SUCCESS**。
- 交付：`verified`、`draft=false`、`hard_fail=""`、`review_valid=true`、`aligned=true`、
  `acceptance_overall=pass`（`acceptance_for_this_body=true`）；版本
  `f585edbf…`，正文 `17960ed5…`，交付记录 `delivered_sha256=ed55fb94…` 与 MD 字节一致。
- 导出：MD 27,346 B / PDF 5,567,593 B / CSV 12,192 B / JSON 44,670 B。
- 必需事实 6/6 带来源定位（eastmoney 结构化源），同比 11 条（营收 +15.66%、
  归母净利 +15.38%、经营现金流 +38.85%），缺口 0、问题 0、`paper_ok=true`。
- 分析运行 `analysis_runs.json`（26,974 B）：
  `profit_bridge`/`cash_quality`/`scenario_sensitivity` = **validated**，
  另有 4 条比率 validated；正文"分析卡"块可渲染（1,113 字符）。
- LLM 调用 14 次 / 400,889 ms（1 次失败），与"计算核心零模型调用"一致。

### 2.1 交付物**自己写明的**保留意见（不隐藏）

- `research_state.json`：`state=research_draft`（"研究底稿：仅原始披露"），
  `located=0`（**没有取到任何带定位的原始披露**），强制问题 2/3 未获支持
  （变化驱动/结构性数据、变化分解），缺 业务背景/经营变化解释/财报附注/风险因素。
- `review_state.verdict=DEGRADED`，`degraded_reason="评审超时（30s）"`：
  金融类**计划评审**超时，个人模式按"未完成评审"继续执行。
- 交付正文开头逐字写着：

  > **研究状态：研究底稿（仅原始披露）**（没有任何带定位的原始披露，located=0 …）
  > **评审状态：评审未完成，已降级**（耗时超时 30s）；本次评审未取得绑定计划版本的
  > 评审 PASS，请经人工复核后方可使用。

即三态分离照常：**机器验收通过**、**研究状态如实为"底稿"**、**人工复核未被代劳**；
"检索/抓取取不到"这件事以缺口形式留在交付物里，而不是靠阻塞整单来"表达"。

## 3. 本轮为通过整跑改了什么（都在动因与判定点上写明理由）

1. **分析步拿到数据集**：派发 `data_analyzer` 前先落底稿（`write_working_paper`，
   幂等）；worker 侧在底稿缺失时按预载载荷 `financials.json` 冻结数据集，并在运行记录里
   记 `dataset_source`（底稿 / 预载载荷）。用**真实失败工作区**离线预演：3 模型 + 4 比率
   validated，`analysis_runs.json` 落盘。
2. **可选步骤失败不再连锁**：阻塞传播与"步骤失败⇒任务失败"两处漏了 `optional` 判定
   （`deps_failed` 一直认），`2b` 失败曾把解释/分析/报告逐步标死。
3. **财务任务的检索/抓取是补充证据**：预载结构化财务存在时，这两类步骤按 `optional`
   语义处理（反思轮新增的抓取步骤同样适用），取不到=缺口。
4. **分析步失败不得降级为文字概括**：金融 `data_analyzer` 失败**不**重规划成
   `content_summary`（那是"整段跳过注册模型"），失败如实保留。

## 4. 回归证据（每条都先在旧代码上复现失败）

| 用例 | 旧代码读数 | 现读数 |
|---|---|---|
| `test_financial_path_falls_back_to_preloaded_financials` | `{"status":"failed","error":"No fresh CSV found in workspace"}`（=实机原话） | `mode=financial`、`cash_quality` validated、运行记录落盘 |
| `test_analysis_step_materializes_paper_before_dispatch` | 派发时无底稿 | 派发前底稿已在 |
| `test_optional_dependency_failure_does_not_block_dependents` | 下游 `Blocked by failed dependency: ['2b']` | 下游 SUCCESS |
| `test_optional_second_source_failure_does_not_kill_the_delivery` | 交付 `draft` + `hard_fail="分析未完成…"`（与三连实机逐字一致） | `verified`、`hard_fail=""`、`analysis_runs.json` 存在 |
| `test_financial_preload_keeps_delivery_alive_when_sources_fail` | 同上「分析未完成」 | 报告照跑、终态非 FAILED、缺口可见 |
| `test_financial_task_fetch_without_url_does_not_fail_dependents` | 报告被 `Blocked by failed dependency` 标死 | 报告 SUCCESS、`has_failure=False` |

定向全绿（每文件一进程）：`test_delivery_chain` 408、`test_p0` 434、
`test_orchestrator_v2` 83、`test_offline_delivery` 36、`test_financial_analysis` 68、
`test_working_paper` 61、`test_financial_chain` 36、`test_us_chain` 9、
`test_root_budget` 76、`test_cancel_semantics` 46、`test_writer_consolidation` 60（跳过 1）、
`test_checkpointer` 9。

## 5. 仍未解决（不在本轮范围内，也不假装已解决）

- **公开检索在本机取不到可用来源**：三次实机 + 本轮两次，`web_search` 的候选列表都
  没有可用的年报正文 URL（抓取步骤因此"无候选 URL"）。本轮把它变成**缺口**而不是
  阻塞，但"研究状态=底稿、located=0"的根因是**来源可得性**，需要检索侧专项（引擎可用性/
  候选排序/官方披露直取）。
- 金融类**计划评审 30s 超时**（`DEGRADED`）：评审判定与交付状态的关系
  （"计划评审降级 ⇒ 交付能否 verified"）是架构决策，本轮**未改**，只如实带出。
- 真人 F3 五项复核（≥8/10）——**必须真人**，代理不代评。
- 页面「改假设→复算→采纳→导出」（Q2/Q3 UI）、`report_version`/交付包携带
  `derived_from`、AGENTS.md 优化积压 10 项：未动。
