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
- **已补（同日第 7 节）**：编排器侧"重试必须产出结构化新查询"已落地——执行器跨轮
  `(查询,后端)` 去重 + 契约生成的结构化下一批 + 编排器重试指令携带新查询；
  `_replan_step` 的换词提示路径（失败后**换实现**那一支）仍未改，见第 7.2 节表末行说明。
- **未验**：以上均为离线单测（无网络、无模型调用）；**没有**再跑真机研究任务验证端到端
  （用户授权的一次付费任务已在上一批用掉）。包内环境复验已在第 7 节补上（2026.09.26.12 包）。
- `url_health` 现在用 GET（策略路径不支持 HEAD）做存活校验，比 HEAD 多下载少量字节
  （上限 256KB/URL，仅对待引用候选）；"不为验证重复下载已有完整文件"这条未做专门优化。

## 6. 命令与结果

```
python -m unittest test_p0                       # 408 OK
python -m unittest test_search_quality_unified   # 51 OK（S0 诊断 8 + S1 执行器 8 + 后端注册表 6
                                                 #        + 结构化重试 5 + 任务级台账 6 + 既有 18）
python -m unittest test_orchestrator_v2 test_delivery_chain   # 420 OK
```

## 7. 包内环境复验与两处修正（运行包 2026.09.26.12）

按专项 §4"必须在**真正执行搜索的同一个运行包/Worker 环境**做诊断"复验 S1；包内实测
暴露两个真问题，均已修正：

| 现象（包内实测） | 根因 | 修正 |
|---|---|---|
| ddgs 备后端仍选中包内**已停用**的 `yandex`，一次调用白等 32 秒后 ConnectError | ddgs 9.16 把可用后端做成自动发现的 `ddgs.engines.ENGINES` 注册表（按类属性 `disabled` 过滤），类上既没有 `get_available_backends` 也没有 `BACKENDS`——三处"可用后端"探测全部落空，回落策略清单首位 | 唯一入口 `adapters.search_quality.ddg_text_backends()` / `select_ddg_backend()`（返回 `(后端名, 依据)`，依据 ∈ registry / policy / none）；`worker_base`、`adapters/text_search`、`search_diag` 三处统一改用 |
| 包内 `backend=brave` 8 秒后 `No results found.` 被记成 `parse_error`（故障类） | ddgs 在"引擎跑完但零命中"时同样抛异常，被当成"没完成查询" | `search_diag.classify_error` 把 `No results found.` 归 `no_results`；`search_runner.is_empty_result_error()` 让调用方把这类异常当**完成但零命中**（返回空列表）：错误表不进、不熔断 |

**为什么这不是小事**：包内 `DDGS._get_engines` 对不在注册表里的后端名会**静默回落 auto**
（`if not instances: return self._get_engines(category, "auto")`）——正是专项 §5 明令禁止的
全引擎重扫。三处探测失效时，"显式单后端"只剩字面意义：名字无效，ddgs 替你扫全部引擎。

### 7.1 复验读数（包内解释器，`dist/weavemind-2026.09.26.13-win-x64`）

```
pkg ddgs     : 9.16.0
registry     : ('brave', 'duckduckgo', 'google', 'grokipedia', 'mojeek',
                'startpage', 'wikipedia', 'yahoo')      ← 9.16 已停用 bing/yandex
select       : ('brave', 'registry')
prod pick    : brave
retry plan   : ['洋河股份（002304.SZ） 2024年年度报告 经营情况讨论与分析',
                '洋河股份（002304.SZ） 2024年年度报告 主要财务指标']
```
（修正前同一命令的输出是 `ddgs 实际可用后端: []` → `策略清单首位: yandex` → `生产选中引擎: yandex`。）

包内 `search_diag.py` 有界诊断（≤6 次调用、≤60 秒、无重试、无模型）：
```
[ok            ] search_html    www.bing.com items=10 0.48s
[no_results    ] search_sdk     ddgs backend=brave 8.05s（HTTP次数未知）  原因：No results found.
[not_configured] disclosure_api cninfo 0.0s（默认关闭；A 股财务当前走东财聚合源）
[ok            ] structured_api eastmoney items=1 0.28s
[ok            ] document       data.eastmoney.com 0.16s
                （取回的不是 PDF 直链，是公告查看页；PDF 原文本轮未取得）
预算：用 4/6 次，剩余 51.0s
小结：可用 ['search_html', 'structured_api', 'document']；零结果 ['search_sdk']；
      失败 {'disclosure_api': 'not_configured'}
```
对照：**同一探测在修正前（包 `2026.09.26.12`）读数是 `[parse_error] search_sdk … 8.05s
（backend=brave, No results found.）`，小结把它归进 `failed`**；修正后归 `零结果`。
读法：**Bing 主通道可用**（0.48s/10 条）；**ddgs 备后端不再打失效名字**（8.05s 结束、记
`no_results`）；结构化源与公告查看页可达，但**未取得 PDF 原文**。`brave` 这次零命中只说明
"这条查询在这个引擎上没结果"，不据此断言 brave 永久不可用，也不据此宣称 A 股披露路径已打通。

### 7.2 S1 补查重试的真正输入（专项 §5）

| 位置 | 现在 |
|---|---|
| `execution_contract.retry_queries()/retry_query_line()`（新） | 按契约字段生成下一批：主体/期间/文档类型/截止**不变**，只换资料面（MD&A 正文 / 指标底稿 / 公告 / 全文）；`tried` 里出现过的字符串不再产出，计划用尽返回空 |
| `search_runner.run_search(exclude_keys=…)` | 任务内重试不再提交同一 `(提供方, 后端, 查询)` 组合；`SearchOutcome.tried_keys` 回传已打出的组合（`submitted` 计数进日志） |
| `worker_base._retry_variants()` / `_execute_bounded` | 第二轮改打**契约生成的新查询**（带 `exclude_keys`）；无契约时保留旧行为（只有暂时性失败才退避一次），不凭空换词 |
| `worker_base._query_variants()` | 指令里带 `[重试检索查询]` 行时**只**打这批（不再回落到上一批已证明无结果的常规契约查询）；期间外变体仍被剔除 |
| `orchestrator._search_retry_instruction()`（新） | 编排器重试不再只追加"请换查询词"：按契约给出新查询块；拿不出新查询时如实写"本轮没有可用的新查询"，且不许换契约外主体/期间 |
| `worker_base._search_allowance()/_search_ledger()/_record_search_ledger()`（新） | **任务级预算**：Redis 台账按根任务累计"已用次数 + 首次检索时刻"，重试/重做派发只拿余额；余额为 0 时一次请求都不发（旧行为 = 每次派发各给 6 次，重试即翻倍）。台账读不到按"不知道"处理：回落默认额度并记日志，不伪造结论 |
| `_replan_step` 的"换实现"分支 | **未改**：该分支处理的是搜索/抓取失败后改用结构化数据或模型知识生成，不重发检索；若后续要它也能产出新查询，需另立批次 |

对应测试（`test_search_quality_unified.TestDdgBackendRegistry` / `TestStructuredRetryQueries` /
`TestTaskScopedSearchBudget`、`test_orchestrator_v2.TestDispatchContractRetry`）覆盖：注册表三态、
零结果不熔断、跨轮去重、契约计划推进与用尽、重试指令携带新查询、无契约时不假装有方案、
任务级台账（首次全额/重试只拿余额/用尽不发请求/读不到回落默认/不跨任务串）。

**未验**：任务级台账只记"次数 + 首次时刻"，多次派发并行时的计数可能低估（读取在派发前、
记帐在派发后，两条并行检索各读到同一余额）；代价是可能多发一轮 6 次，不会无限放大。
台账用 Redis 哈希键，未做跨实例共享校验（本项目一实例一台 Redis）。

