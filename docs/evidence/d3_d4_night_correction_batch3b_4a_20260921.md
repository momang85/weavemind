# 第三小批（二）+ 第四小批（一）：独立研究状态、固定路线 Critic、同版交付收口

2026-09-21 夜间纠偏（指令 §3 第三小批 item 5–7、第四小批 item 1–2，基线 `6ae23b5`）。
范围：`delivery_pipeline.py`（研究状态）、`web_ui.py`（页面/导出清单同源）、
`orchestrator_v2.py`（确定性计划接统一 Critic）、`chart_specs.py`（图注按数据方向）、
`workers/packaging_worker.py`（包内 manifest）、前端面板与类型、测试。
**离线；实机在本批之后单独一次（见下一份证据）。**

## 1 独立研究状态（`research_state`，与数字机器验收分开）

- 新轴：`delivery_pipeline.research_state(task_id, goal, structure)` →
  `research_ready` / `research_draft` / `not_applicable`，附原因与读数
  （`located`、缺哪几类披露、未支持主张数、缺依据的关键问题数）。
- 判据：**要求经营解释**（期间 ≥2 或有关键问题）时——
  - `located == 0` → **研究草稿／待补原始披露**（"没有一条带正文定位的原始披露"）；
  - 关键问题全部只有读数、没有可定位依据 → 研究草稿；
  - 存在**已断言但未支持**的主张（`claim_type != boundary`）→ 研究草稿；
  - 其余 → 研究就绪（原因里给出"带定位披露 N 条；关键问题 x/y 有依据"）。
- `not_applicable`：非研究任务或契约不要求叙事（单期/纯数据核对）——**纯数据核对任务
  不因不需要叙事而被判失败**。
- 显示面（同一份文字，不各写一套）：`research_state.json` 落盘 → 交付说明注记
  （Markdown 与 **PDF 封面**共用）→ 页面 `research` 负载 → 导出清单 `research_state`
  → 前端面板一块（草稿态琥珀色，并写明"本条说的是研究是否就绪，不改变数字与格式的
  机器验收结论"）。`verified` 语义不变，仍只表示"数字与格式机器验收通过"。
- 实测（六场景）：`missing_footnote_asof`（located=0）→ 研究草稿；其余有定位披露的场景
  → 研究就绪，原因里带比例（如"关键问题 1/3 有依据"）。

## 2 固定研究路线接统一 Critic（批次3c）

- `_review_deterministic_plan(task_id, goal, steps, reason=…)`：确定性计划（模板 / 路由模板 /
  **固定研究步骤**）在 `system.critic=true` 时进**同一入口** `_review_plan`（FAIL 修订一次
  并复评；超时/异常按"评审未完成"处置）。此前固定研究路线直接跳过 Critic，只记一句降级。
- 不补造评审：Critic 关闭 → 如实记降级（个人模式继续 / 银行拒绝）；`_review_plan` 未给
  PASS → 仍按既有策略记降级，**绝不写 PASS**。银行口径的执行前 PASS 硬条件不变
  （`_review_is_required`）。
- Critic 是独立进程，它的 LLM 调用早已带根任务上下文（`plan_review` 的 `context`），
  因此同样进根任务预算。

## 3 D4 同版交付收口（批次4 的两项必要项）

- **图注/结论按数据方向**（`chart_specs`）：两期核心指标"三项均下降 / 均上升 / 有升有降"
  由 yoy 读数判定，图注与结论同一来源。实测 `all_decline` → "三项均下降"、
  `normal_growth` → "三项均上升"（此前全降也写"有升有降"）。
- **包内真实 manifest**（`packaging_worker`）：交付 zip 里写入 `PACKAGE_MANIFEST.json`
  ——正文（包内 `reports/*.md` 的 sha256）、图表（包内 `charts/*.png` 的 sha256）、
  材料指纹（`sources_fingerprint`）、规则版本与指纹、打包时间。
- `web_ui._export_payload`：**包内清单优先**——`package_body_version_id` 取包内正文 sha；
  陈旧判定先用"包内正文 sha ≠ 当前采纳正文"（内容标识），时间戳只作辅助；清单里同时给出
  `package_sources_fingerprint` / `package_rules_fingerprint` / `package_charts`。

## 4 定向验证（离线）

| 用例 | 断言 |
|---|---|
| `TestResearchStateAndDeterministicCritic.test_research_draft_when_no_located_disclosure` | located=0 → 研究草稿；注记写明"不因此改变"数字验收 |
| `…test_research_ready_keeps_numeric_acceptance_separate` | 有定位披露且无未支持主张 → 就绪；有未支持主张 → 草稿；**边界句不算未支持** |
| `…test_not_applicable_for_non_research_task` | 非研究任务/单期 → 不适用（不误判失败） |
| `…test_deterministic_plan_goes_through_critic_when_enabled` | `critic=true` 时确定性计划**真的调用** `_review_plan` |
| `…test_deterministic_plan_degrades_when_critic_disabled` | critic 关闭 → 如实记降级原因，不写 PASS |
| `TestPackageManifestInsideZip.test_zip_carries_manifest_with_body_and_chart_hashes` | 包内含 `PACKAGE_MANIFEST.json`；正文 sha 与**包内字节**一致；图表 hash 在册 |

回归：`test_delivery_chain` 296、`test_orchestrator_v2` 68、`test_frontend_guards` 46、
`scenario_checks` 17、六场景全过、`claims_d1_cases check` 11/11、前端 `npm run build` 通过；
CI 清单 46 文件本地全绿。

## 5 未验项

1. 研究状态的**阈值校准**（"关键问题 x/y 有依据"要多少才算就绪）目前是"至少一条有定位
   依据且无未支持主张"，原因里如实给出比例；更严的门槛（如要求收入问题必须有依据）留给
   架构师裁决。
2. 包内 manifest 的**下载/陈旧链路**要等实机产物复验（本批只验了打包侧与 payload 侧）。
3. 图注方向只改了"两期对比"那张；其余图注（同比图、比率图）本来就是数据算的。
