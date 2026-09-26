# S1 有界搜索与真实结果协议（09-26 夜）

日期：2026-09-26。批次：搜索网络与金融资料专项 **S1**（专项 §5）。承接 S0 的两环境事实表
（`s0_worker_env_diag_20260926.md`：包内首个引擎 32 秒失败、轻量路径 9 引擎阶梯实测一次调用
90 秒）。

## 1. 改了什么（三个执行入口收敛成一个）

| 位置 | 之前 | 现在 |
|---|---|---|
| `adapters/search_runner.py`（新） | 无 | **唯一检索执行器**：一个预算（次数 + 单调时钟截止）、显式单后端（从不 auto）、(查询,后端) 去重、结果协议 `status/items/attempts/elapsed/reason/retryable/provider/backend/errors`，并给旧调用方 `to_legacy_items()` 数组兼容层 |
| `worker_base.SearchAgent` | 逐引擎探测 + 全失败回落 `alive=None → auto` + 二轮重试（实机 9 分钟 / 31 次尝试） | `execute = _execute_bounded`：Bing 主 + ddgs **一个**备后端；引擎取"策略清单 ∩ ddgs 实际可用"；至多一次有界重试且受同一截止线约束；全部提供方冷却时不发请求 |
| `adapters/text_search.web_text_search` | 自带 Bing→ddgs 阶梯、吞异常返回 `[]` | 同一个 `run_search` 执行器（Bing 主 + ddgs 单备后端），`_search_ddg` 改为**单后端单次** |

默认预算（专项 §5 的新人口径）：`WM_SEARCH_DEADLINE_SECONDS=60`、`WM_SEARCH_MAX_CALLS=6`，
两个维度由 `SearchBudget` 共用一条截止线；底层 HTTP 次数在 ddgs 侧未知时如实标注（S0 同一纪律）。

## 2. 候选与步骤状态（专项 §5 的"URL 状态 + 过滤后重判"）

| 位置 | 之前 | 现在 |
|---|---|---|
| `adapters/url_health.py` | 403/429/超时/代理错误一律 `dead`，编排器据此删候选 | 五态：`reachable / not_found / inaccessible / unknown / policy_blocked`；只 404/410 可剔候选；请求走 `net_policy.fetch_document`（策略校验 + 已验 IP + 不跟随重定向） |
| `orchestrator.filter_dead_search_results` | 删 `dead` | 只删 `not_found`；`inaccessible/unknown/policy_blocked` **保留候选并记原因**（日志） |
| `orchestrator` 搜索步收尾 | 过滤后可能留 `SUCCESS` 但结果是 `[]` | 过滤后为空 → 该步判 **FAILED** 并写明"候选全部 404/410"，`has_failure` 置位 |
| `orchestrator` 抓取步派发前 | 无候选也派发 → worker 回 `No URL found` → 下游 `Blocked by failed dependency` | **无候选 URL 不派发**（本轮抓取调用数 0），直接判失败并写明"需补资料或换检索词" |
| `delivery_pipeline` 来源清单校验 | 把 `dead` 一律写成"来源链接失效" | 只有 404/410 写"失效"；`inaccessible/unknown/policy_blocked` 另列"当前访问不到（保留为未验证来源）"，不进 gaps |

## 3. 最小验收逐条（专项 §5）

| 验收项 | 证据 |
|---|---|
| 连接全失败不走 auto | `TestBoundedSearchRunner.test_never_falls_back_to_auto_backend`：断言每次调用的 backend 非空 |
| 真零结果不熔断 | `test_zero_results_is_not_a_backend_failure`：`status=no_results`、`retryable=False` |
| 单变体异常不标健康 | `worker_base._execute_bounded` 按 `outcome.errors` 标记（异常即不健康），不再是"added or not err" |
| 短 deadline 可中止整个执行 | `test_short_deadline_can_stop_the_whole_run`（到点后不再发新请求） |
| 调用次数上限是硬闸 | `test_call_budget_caps_attempts`（3 变体 × 2 提供方只能打 2 次） |
| 403/429 保留未验证候选 | `test_url_health_403_and_429_are_inaccessible_not_gone` |
| 超时不判失效 | `test_url_health_timeout_is_unknown_not_dead` |
| 404/410 明确失效 | `test_url_health_404_is_the_only_definitely_gone` |
| 策略拒绝仍禁止抓取 | `test_url_health_policy_rejection_blocks_fetch`（`policy_blocked` 不重试、不改直连） |
| 过滤后空结果不保持成功 | `orchestrator` 搜索步收尾分支（`_raw_count and not parsed → FAILED`） |
| 无候选抓取调用数 0 | `orchestrator` 派发前检查（`re.findall` 无 URL → 不派发） |
| 无新模型调用 | 本轮改动不含任何 LLM 调用 |
| 旧数组契约有兼容层 | `test_partial_and_legacy_compat_layer`（`to_legacy_items()` 输出 JSON 数组） |
| 契约查询重试去重 | `test_dedupes_query_provider_pairs`：同 (查询, 后端) 不重复提交 |

## 4. 被调整的既有断言（专项要求"修复必须调整行为预期"）

- `test_p0.TestNewDataAdapters`：旧的 `alive/dead` + "失败重试 1 次" + "HEAD 405 降级 GET"
  断言按新语义重写为五态；新增 403/429/超时/策略拒绝四例。
- `test_orchestrator_v2`：`test_filter_dead_search_results_drops_dead` →
  `..._keeps_unverifiable_candidates`（只剔 404/410）；来源清单校验加"访问不到 ≠ 失效"断言。
- 两处 fixture 的 `web_fetch` 步骤原本**没有 URL**（真实环境只会回 `No URL found in instruction`），
  按新语义补上候选 URL：`test_delivery_chain`（修订流）与 `test_p0`（human_in_loop 自动放行）。

## 5. 未做 / 未验（照实）

- **未做**：S2 的网络通道统一（代理路径与直连路径的显式区分、`transport` 的 raw socket 回落、
  各适配器接入同一诊断类别）——按指令顺序留给 S2；S3 的 A 股披露发现闭环未动。
- **未做**：编排器侧"重规划必须产出结构化新查询"的完整实现——本轮只做了执行器的
  (查询,后端) 去重；`_replan_step` 的换词提示路径未改（留给 S1 的后续或 S3）。
- **未验**：以上均为离线单测（无网络、无模型调用）；**没有**再跑真机研究任务验证端到端
  （用户授权的一次付费任务已在上一批用掉）。包内环境复验需要重建运行包后跑 `search_diag.py`
  与一次 smoke（S4 的验收矩阵要求）。
- `url_health` 现在用 GET（策略路径不支持 HEAD）做存活校验，比 HEAD 多下载少量字节
  （上限 256KB/URL，仅对待引用候选）；"不为验证重复下载已有完整文件"这条未做专门优化。

## 6. 命令与结果

```
python -m unittest test_p0                       # 408 OK
python -m unittest test_search_quality_unified   # 31 OK（含 S1 执行器 7 例 + S0 诊断 8 例）
python -m unittest test_orchestrator_v2 test_delivery_chain   # 420 OK
```
