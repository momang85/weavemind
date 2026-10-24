# RUNTIME-1022 第四批（P1-D）：检索尝试与采纳证据分账 — 实际修复与验证记录

> 本批全部改动**只在工作区源码**，未 `git commit`/`push`，未重启在线实例，
> 未修改生产配置 / 凭据 / 系统 DNS / 代理。
> 结论先行：**新代码验证通过（含全部必回归），在线生效待受控重启**（在线实例
> PID 24572 自 2026-10-22 17:08 运行旧代码，本批不重启）。

## 1. 诊断问题 → 本批修复

| 诊断问题（20261022-runtime-diagnosis） | 修复 |
|---|---|
| 检索"试过什么"与"采纳了什么"混在一起，来源清单只写采纳结果，无法证明不是覆盖出来的 | 两本账分账：`search_attempts.json`（尝试轨迹，逐条阶段/状态/候选数/**准入数**/理由）与 `search_adopted.json`（采纳证据 + superseded 轨迹 + 缺口说明），都进交付包 `audit/` |
| 重试预算被拒返回空数组、重规划摘要代做、反思轮换查询后命中变差 → **覆盖既有已采纳来源**，来源不增反减 | `AdoptedStore.offer` 评分 `(准入数, 候选数, 状态==SUCCESS)`：不更优不覆盖，返回既有采纳结果（SUCCESS、来源原样）并标注 `search_evidence_retained / retained_admitted / retained_note`；摘要代做候选/准入数为 0，顶不掉检索证据 |
| "预算不能无限重置"：重试阶段吃光 6 次额度，反思补查注定被拒 | 阶段显式分配 `stage_plan(6) = {initial:4, retry:1, repair:1}`（3→{2,1,0}，≤2→全 initial）；`stage_reserve(plan,'retry')` 预留不被动用——余额只够预留时**不再派发**检索重试；环境变量 `WM_SEARCH_STAGE_PLAN="initial:4,retry:1,repair:1"` 可覆盖但**不能突破上限** |
| 检索无新增材料仍多轮重做（重复同一查询拿同一批候选） | 反思路径停损：跨轮采纳总数（准入口径）无新增 + 下一轮只有**已试过的查询** → 停止昂贵重做，把证据缺口（缺哪些来源、多少轮没新增）落 `search_adopted.json.gap` 并在控制台如实呈现；纯报告修复不走这条停损（交给验收与指纹规则） |
| 降级提示教模型"不要提及搜索或抓取失败"→ 隐瞒检索失败、模型知识伪装成检索结果 | `_generation_fallback_step` 提示改为如实披露："本轮**没有取得可核验的联网来源**（这是如实降级，不是检索成功）"，要求正文标注「未在本次检索中验证」+ 结尾列全缺口 + 不得自称完成联网检索 |

## 2. 修复明细（文件级）

| 文件 | 改动 |
|---|---|
| `search_evidence.py`（新，约 380 行） | 唯一实现：`stage_plan` / `environment_stage_plan`（上限内才用环境值）/ `stage_reserve`；`candidates_of` / `admitted_of(result, contract)`（**只统计通过准入的来源**——未准入候选不是有效证据；无契约字段时退回候选数）；`attempt_record`；`AdoptedStore`（offer/评分/保留/superseded 历史/`set_gap`/`to_dict`）；`save_attempts` / `load_adopted`；`stop_decision`（连续无新增 → 停损，新检索材料立即恢复） |
| `orchestrator_v2.py` | `_dispatch_step_safe`：首次派发与每次重试/重规划代做都记尝试轨迹；重试检查改用 `stage_reserve`（余额只够预留即停）；`_finalize_search_adoption` 收尾（更差→保留既有，`search_evidence_retained`；更优→`search_evidence_adopted`）；报告步派发前 `_persist_search_audit` 落盘两本账；`run` 反思循环：跨轮采纳总数（准入口径）无新增记 stale 轮，`_should_stop_search_redo` 命中即停损并落缺口；任务终态清理进程内副本（`_task_adopted_search` / `_task_search_attempts` / `_adopted_totals` / `_search_stale_rounds`） |
| `delivery_pipeline.py` | `_exclusion_audit_members` 增打包 `audit/search_attempts.json` 与 `audit/search_adopted.json`（两本账进交付包） |
| `test_orchestrator_v2.py` | `test_research_goal_never_falls_back_to_code` 断言改为**诚实披露**（"没有取得可核验的联网来源" + "未在本次检索中验证"，且不得含"不要提及搜索"）——原断言固化的是隐瞒降级的旧文案 |
| `.github/workflows/ci.yml` | 新步骤 `python test_search_evidence.py`（attempts vs adopted、阶段预算、停损） |
| `AGENTS.md` | 模块地图补 `search_evidence.py` 行；测试数量更新（97 个 test_*.py） |

## 3. 验证（真实退出码，无 tail 掩码）

| 验证 | 结果 | 退出码 |
|---|---|---|
| `python test_search_evidence.py`（新 20 用例，首跑全绿） | OK | 0 |
| `python test_orchestrator_v2.py`（106 用例） | OK | 0 |
| `python test_search_candidate_kept.py`（13 用例） | OK | 0 |
| `python test_task19_search_coverage.py`（26 用例） | OK | 0 |
| `python test_task24_dead_source_annotation.py`（19 用例） | OK | 0 |
| `python test_source_admission.py`（19 用例） | OK | 0 |
| `python test_source_exclusion_registry.py`（22 用例） | OK | 0 |
| `python test_p0.py`（438 用例） | 434 OK + 4 FAIL（pip 镜像，环境问题，见 §4） | 1 |
| `python test_delivery_chain.py`（425 用例） | 424 OK + 1 ERROR（行情用例被 fake-IP DNS 拦住，与 §4 同源，非回归） | 1 |
| `python -m py_compile orchestrator_v2.py delivery_pipeline.py search_evidence.py` | 无输出 | 0 |

### 新用例覆盖（`test_search_evidence.py`，20 用例）

- **阶段预算**：总和不超过上限；`stage_reserve` 反向预留（4/1/1 → initial 预留 2）；环境覆盖越界回退（不无限重置）、界内生效。
- **准入口径**：候选 3 条（年报+公报+轴承页）→ 公司契约 admitted=1（未准入不算证据）；市场契约退回候选数；FAILED/坏 JSON → 0。
- **跨轮保留**（编排级，真实 `_dispatch_step_safe`）：第二轮空数组 → 仍返回首轮来源（SUCCESS、`search_evidence_retained`、`retained_admitted=1`），两本账落盘（attempts 2 条 / adopted 1 条）；预算只够预留时**一次派发都不多发**；`retry_skipped` 失败分类进轨迹（status/candidates/admitted/reason）。
- **停损**：重复已试查询 + 上一轮无新增 → 停；新查询 / web_fetch 步骤 → 恢复重做；纯报告轮不受此规则拦截；`stop_decision` 纯函数边界（stale=1 停、stale=0 不停、有新材料不停）。
- **交付包**：两本账进 `audit/`。
- **降级披露**：兜底提示不再含"不要提及搜索"，必须含"没有取得可核验的联网来源"。

## 4. 环境问题（非本批引入，不改 DNS/代理）

`test_p0.py` 的 4 个失败全部在 `TestPipMirrorPolicy`：本机 DNS 又对
`pypi.tuna.tsinghua.edu.cn` 返回 fake-IP（`198.18.0.35`，与
`egress-current-20261023.json` 里 5 个内容主机同源）。`dep_check._pip_mirror()`
经 `adapters.transport._validate_public_url` 做 SSRF 防护，**正确拒绝**保留地址段
→ 镜像被禁用 → 断言默认镜像的 4 个用例失败。离线复验（mock 放行公网 URL）下
其中 3 个立即转绿（第 4 个仅在 mock 恒真时失败，属复验脚本的预期行为）。
`dep_check.py` / `adapters/transport.py` 本会话零改动——这是**环境问题，不是代码回归**。

## 5. 边界自检

- 未 `git commit` / `git push` / 新建分支 / `git add -A`；
- 未修改生产配置 / 凭据 / 系统 DNS / 代理；未重启在线服务（8080 仍 PID 24572 旧代码）；
- 未删除或移动旧制品；未签署人工复核；
- 测试工作区全部临时目录隔离（`se-round-*` / `se-pkg-*`，tearDown 清理）；
- "新代码验证通过"与"在线实例已在跑新代码"严格区分——在线生效待受控重启（P1-E）。

## 6. 本批后仍待办（→ P1-E）

- 实例身份 / 启动时间 / 构建指纹经健康机制区分"磁盘版本 / 已加载版本 / 前端构建"；
- 受控重启计划（备份/回滚、确认无运行中任务、明确服务与数据根、历史只读复验、
  一个范围内研究预算跑端到端）；
- 测试/生产日志隔离；
- 已知环境项：本机 DNS fake-IP（见 §4，5 个内容主机 + pypi 镜像均受影响）。
