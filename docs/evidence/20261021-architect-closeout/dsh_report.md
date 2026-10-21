# DSH 侧交付报告（F1–F3 收口）

日期：2026-10-21（用户客户端日期）。执行者：DSH。审阅：Codex（独立验收与选择性推送）。
任务书：`docs/DSH成果复核与推送收口_20261021.md`（本文件不改任务书）。

基线：HEAD `99d5480`，分支 `c5-market-comparison`（现场核得，与任务书一致）。
**本批未执行任何 git 写操作**（无 add / commit / push / 分支），未清理历史文件，
未动旧稿与旧包。推送由 Codex 复核后统一执行。

---

## 0. 接单回执（先于动手实测）

| 项 | 实测值 | 说明 |
|---|---|---|
| 服务实际端口 | **8099**（HTTP 200） | `launcher.py status` 的 `URL:` 仍打印默认 8080 但那端口无监听；**未按 8080 做事、未硬编码回 8080** |
| 服务数 | 16/16 alive | 由 Lead 的两个后台作业持栈，**本批未杀任何服务、未重启** |
| 在飞任务 | **0** | `task_history` 非终态行数 = 0（查询口径见 §5） |
| 并行子任务 | 4 个 teammate 全部 inactive；F2 由本批临时子智能体承担，写域 `llm_client.py` 等 | 无写域重叠：F1 落在 `orchestrator_v2.py` 的来源注入区 + `workers/report_generator_worker.py`；F3 落在 `orchestrator_v2.py` 的 `main()` 启动序；F2 落在 `llm_client.py` |
| 未触碰 | `web_ui.py`、人工复核展示（F4 归 Codex） | 全文未编辑这两个区域 |

---

## 1. F1：已剔除来源不得回流正文来源清单

### 1.1 根因（四段链条，逐段有现场证据）

用真实工单 `ui-b23642e5de`（研究伊利股份 600887）的**缓存副本**追根，未改写旧稿旧包：

1. **检索候选本身无关**：`project/search_results.json` 3 条全是
   SKF 深沟球轴承 6008 / Windows 事件日志 6008 / 汉语词典页。
   （注：假年份 `6008` 的产生路径是 `adapters/search_quality.py` 裸 `\d{4}` 切
   证券代码，已在上一批修复并有回归测试 `test_search_year_boundary`；本批不重复处理。）
2. **相关性过滤 fail-open 全保留**：`orchestrator_v2._filter_source_items` 的兜底是
   「一条都不剩时保留全部」。实测这 3 条对目标词命中 **0 个**，
   于是"该剔除"被兜底改写成"全保留"。
3. **双保险旁路不设防**：报告步骤注入来源时，还有一条"从本迭代 `completed` 的
   搜索结果里补 URL"的路径，原先**完全不经过滤**。
4. **报告生成器无条件追加**：`workers/report_generator_worker.py` 从指令的
   `[数据来源]` 块抽 URL，**无条件**追加 `## 数据来源`，且追加发生在
   `_strip_rejected_sources` 视野之外（模型正文只写"3 条均无关已整体剔除"这种
   **计数式声明**、不写域名/URL，那条正则取不到任何 URL，什么也删不掉）。

净效果：同一份交付里，正文说"已整体剔除、未纳入来源清单"，
`## 数据来源` 却逐字列出这 3 条 URL（交付主文实测 **6 处**命中无关域名）。

### 1.2 改法（候选 / 采用 / 剔除 三分，不按公司名或 URL 写死）

**`orchestrator_v2.py`**

- 新增任务级**剔除审计** `_task_excluded_sources`（初始化 / 惰性初始化 / 任务收尾清理
  三处都补，与既有 `_task_sources` 同生命周期）。
- 新增 `_subject_anchors(goal)`：**可证主体锚点** = 6 位证券代码（数字边界）
  + 带公司名后缀的中文段（`_SUBJECT_SUFFIXES` 是形态学后缀表，
  并剥掉"研究/分析/评估…"这类前导动词）。实测该目标得 `['600887', '伊利股份']`。
  刻意不用 `clean_data._strong_subject_tokens`——那是 4 字滑窗、噪声大且不含数字，
  拿它当剔除依据会误删合法来源。
- 新增 `_partition_source_items(goal, results) -> (kept, dropped)`，三分判据：
  - 有主体锚点 **且** 所有候选对**任何**目标词零命中 ⇒ 可证无关，整批剔除，**每条带理由**；
  - 否则维持 fail-open（启发式判不准时不减损来源）。
  `_filter_source_items` 原签名与行为保留，既有调用点/测试不受影响。
- 检索桶落点改用三分结果：`kept` 进 `_task_sources`，`dropped` 进剔除审计（含 url+理由）。
- 新增模块级 `source_injection_text(urls, excluded)` 作为**唯一注入生成口**：
  - 有采用候选 → 编号清单（措辞改为"通过相关性判定、可被正文引用"）；
  - 候选全被剔除 → **不给编号、不给 URL**，只如实说"没有可引用的外部来源"，
    并要求"`## 数据来源` 如实写『本次无采用的外部来源』"；
  - 有任何剔除 → 追加 `[已剔除来源]` 块（逐条 URL + "不得出现在正文与来源清单"）。
- 双保险旁路补上同一份剔除名单（`_excluded_urls`），并在注入前再滤一遍。

**`workers/report_generator_worker.py`**

- 新增 `_excluded_from_instruction()`：解析 `[已剔除来源]` 块（确定性名单，
  不依赖模型是否写出域名）。
- 新增 `_drop_excluded_urls(text, excluded)`：从来源清单类附录（标题命中
  `数据来源|参考文献|来源附录|来源链接|参考资料|来源`）或列表/表格行里删掉被剔除 URL；
  正文散文里则**只摘 URL 不删句子**（`[文字](url)` 退化成 `文字`，裸 URL 换成
  "（该来源已按来源纪律剔除）"），返回 `(文本, 删除条数)`。
- **把生产收尾抽成两个可直调的确定性函数（架构师补令第 2 条）**：
  `compose_source_appendix(instruction)` 生成自动追加的 `## 数据来源` 文本；
  `finalize_source_hygiene(report, instruction)` 做「正文剔除声明 + 确定性剔除名单」
  两步收尾。`execute()` 的主路径与兜底路径**都只调用这两个函数**，
  测试也调同一份代码——不再是"测试里重写一遍推导"。
- 滤空且有剔除记录时改写为「本次无采用的外部来源（…剔除记录见交付包
  `audit/source_exclusions.json`）」，指引只指向**真实存在的工件**；
  同时删掉原先那句指向"任务详情与交付包来源审计"的**无依据指引**。

**剔除审计落盘（架构师补令第 1 条）**

- `orchestrator_v2._write_source_exclusions_file(task_id)`：报告步骤派发前把
  `_task_excluded_sources` 写成工作区 `project/source_exclusions.json`
  （`{task_id, generated_at, note, count, excluded:[{url, reason}]}`，
  只含 URL 与理由，不含提示词/响应体/密钥；每次整体覆写，重跑不带陈旧数据）。
  与既有 `_write_dead_sources_file` 同一条通道、同一时机。
- `delivery_pipeline._exclusion_audit_members(ws)` + `repack_adopted` 里一行
  `files.extend(...)`：文件存在才纳入，包内路径 `audit/source_exclusions.json`；
  没有剔除记录的任务行为零变化。**该成员与正文/清单同样过逐成员 sha256 自检。**
- **边界（如实）**：这是**工作区 + 交付包**两处可核验记录；
  **任务详情页（`web_ui.py`）未加展示入口**——本批不碰该文件（F4 归 Codex），
  所以报告措辞里也**不再声称"任务详情可查"**。

### 1.3 现场验证（真实工单缓存副本，零模型调用）

```
主体锚点: ['600887', '伊利股份']
真实工单：保留 0 / 剔除 3
  剔除: https://www.skf.com/...            | 与本标的不含任何共同主题词（目标主体：600887、伊利股份）
  剔除: https://learn.microsoft.com/...    | 与本标的不含任何共同主题词（目标主体：600887、伊利股份）
  剔除: https://www.hanyuguoxue.com/...    | 与本标的不含任何共同主题词（目标主体：600887、伊利股份）
模糊目标 fail-open: 3 条保留        ← 「总结一下要点，列成条目。」不误删
混入合法来源：保留 1 剔除 3 → ['https://www.cninfo.com.cn/x.pdf']
```

生产收尾函数实测（同一份 `compose_source_appendix` / `finalize_source_hygiene`）：

```
全剔除 → ## 数据来源 / - 本次无采用的外部来源（…剔除记录见交付包 `audit/source_exclusions.json`）。
混合   → ## 数据来源 / - [https://www.cninfo.com.cn/ok.pdf](…)
```

**未采纳/旧包未改**：本批只改代码路径；`ui-b23642e5de` 的既有采纳版、
`deliverables_20261021_222233.zip` / `..._223018_3c00b8.zip` 一律未动。

---

## 2. F3：第二编排器须先认领，再处理共享结果

### 2.1 根因（启动序写反）

`orchestrator_v2.main()` 原先的顺序是：

```
1) r = _new_redis_sync()
2) r.keys("task_result:*") → r.delete(*stale)      ← 清理共享结果键
3) orch = OrchestratorV2()
4) claim_ownership_with_wait(r) → 失败则 return 2   ← 认领在最后
```

第二个编排器进程被拉起时，第 2 步会**先把活跃实例正在等待的结果键全部删掉**，
然后才去认领、认领失败退出。活跃实例 `_brpop_with_deadline(r, "task_result:*")`
从此等不到结果，在飞任务只能超时——即"认领失败仍有删除副作用"。

### 2.2 改法（最小调整启动顺序）

- 抽出 `_cleared_stale_results(r) -> int`：唯一的结果键清理入口，
  失败只记警告不挡启动（原实现是裸 `except: pass`，现在有日志可查）。
- `main()` 改成：`连接 → claim_ownership_with_wait → 失败 return 2 → 成功才清理
  → 构造 orch → 后续租约续租/恢复扫描不变`。
- 认领失败路径上，除 Redis 连接外**没有任何写/删**；`OrchestratorV2()` 都不构造。
- 同时删掉改动后重复出现的旧认领块（避免二次认领），并保留 `listening` 日志位置。

### 2.3 定向测试（假 Redis 替身，**未接触共享 Redis、未起第二套栈、未杀进程**）

`test_orchestrator_claim_order.py`，5 例全过：

| 用例 | 断言 |
|---|---|
| 认领失败时不清理 | `main()` 返回 2、`OrchestratorV2` 未构造、删除记录为空 |
| 认领成功后才清理 | 只删 `task_result:*`，非结果键 `keep` 原样保留 |
| 清理幂等且容错 | 空库返回 0；`keys()` 抛错时返回 0 不抛 |
| 启动序回归守卫 | 源码中 `claim_ownership_with_wait` 位置 **必须早于** `_cleared_stale_results`，且清理前不得出现裸 `keys("task_result:*")` |
| 第二实例全流程 | 认领被拒后共享结果键两条都健在 |

测试全程用 `_new_redis_sync` 替身注入，未连接真实 Redis。

---

## 3. F2：流式降级归属/记账与非 SSE 坏响应

改动文件：**`llm_client.py` 单文件**（+ 新增测试 `test_llm_stream_fallback_and_bad_response.py`）。
写域与 F1/F3 不相交，故并行进行；**未触碰 `web_ui.py`**。

### 3.1 根因（三处，均有现场证据）

1. **发出 `llm:stream_fallback` 警告的不是任务调用，是端点探测**。
   `_probe_endpoint_status`（`llm_client.py:969`）由两条**本来就没有任务上下文**的路径调用：
   `get_balance_status` 的 `ThreadPoolExecutor` 并行探测（线程池线程不继承 contextvars，`:1092`）
   与健康守护线程 `_health_monitor_loop(interval=60)`（`:1280`）。
   旧实现里探测复用"流式优先"路径：网关对 `stream:true` 回 404 → `_fell_back` →
   在 `:2387` 开 `usage="llm:stream_fallback"` 的回退票；探测线程 `get_task_context()` 为空 →
   `_budget_open` 记"没有根任务归属"、返回 `(None, "")`，请求照发却不进任何任务账本。
   **结论校正：这不是"任务丢了归属"，而是探测本来就没有、也不该有归属。**
   日志复核：`logs/orchestrator.log` 中「流式请求被拒」与「没有根任务归属
   （usage=llm:stream_fallback）」**420/420 成对**，且每条前一行都是 `LLMClient: model=…`
   （探测新建客户端），时间戳按 60s 整数倍递增（21:46:38→21:47:38→21:48:38…），
   与 `_health_monitor_loop(interval=60)` 吻合。
2. **"每次回退多耗 60 秒"不成立（架构师要求不凭两条日志断言）**：60s 是**两轮探测的间隔**，
   不是单次调用的额外耗时。回退路径代码内没有任何 sleep，唯一的重试等待是整次尝试失败后的
   `_RETRY_BASE*attempt=0.3s`。**有证据的重复是：每次探测白打一个被拒的 stream 请求。**
3. **非 SSE 坏响应被当成正文并进成功缓存**：`_call_llm_stream_once` 旧 else 分支
   `except: chunks.append(body_text)`，把 HTML / 坏 JSON / `{"error": …}` 原样当正文返回，
   `call(expect_json=False)` 随后 `_cache_set` 写入**成功缓存**（同 cache_key 后续直接命中垃圾）；
   异步回退 `_async_chat_once` 旧实现是**先把回退票结算 ok、再取 choices**，
   于是供应商错误对象被记成 `stream_fallback_ok` 之后才炸 KeyError。
4. **未归属调用静默丢弃**：`_record_llm_call` 在 `task_id` 为空时直接 `return`，
   这次调用在账本与诊断里**都不存在**；也没有任何"费用未知"的口径。

### 3.2 改法

- **探测显式非流式**（`:997`）：探测请求固定 `stream=False`，不再因网关不支持流式而多打一次
  被拒请求，也不再污染任务级 `llm:stream_fallback` 告警；探测的未归属按
  `_note_no_root_call("probe", "llm:probe", tokens=…, warn=False)` 记入诊断（可见、可计数、不刷告警）。
  `no_root_call_count()` 仍只统计"本应有根却丢了"的调用，不被探测污染。
- **坏响应显式拒绝**：新增 `LLMBadResponseError`（`:1669`）与三个校验入口
  `_validate_chat_response()`（`:1697`）/ `_parse_model_response_body()`（`:1725`）/
  `_parse_async_model_response()`（`:1741`）——只接受
  `{"choices":[{"message":{"content": str|null}}]}`：
  - `Content-Type: text/html` → 拒绝（不等 JSON 解析猜）；
  - 响应体不是合法 JSON → 拒绝；
  - `{"error": …}` 供应商错误对象 → 拒绝；
  - 缺 choices / 缺 message / content 非字符串 → 拒绝；
  - **合法非 SSE JSON 照常成功**（仍取 `choices[0].message.content`，既有测试与部署依赖此行为）。
- **坏响应走失败路径**：回退那张票按失败结算并记 `llm:nonstream_failed` + `bad_response` 形状；
  `_error_shape()` / `_degradation_reason()` 增加 `bad_response` 分类（重试次数 0，
  避免被当成"端点瞬时故障"反复重试）；**不发布成功片段、不写成功缓存**。
- **先校验再结算**：`_send_request`（`:2275`）新增 `stream` 覆盖参数；非流式回退改为
  **先校验响应体再结算回退票**。异步 `_async_chat_once`（`:3158`）/ `_async_call_backup`（`:3229`）
  同样先校验再按成功结算；`call_llm_async` 新增 `except LLMBadResponseError`（`:3021`）
  记形状并按尝试次数重试、不缓存。
- **非 SSE usage 不再漏记**：`_call_llm_stream_once` 非 SSE 分支（`:2656`）补记 usage，
  只发布真实 content（空正文不发布）。
- **未归属不再静默**：未归属调用进 `_no_root_calls` / `get_unattributed_diagnostics()`（`:563`），
  `cost_usd=None`、`cost_known=False`、取不到实际用量的 token 记 `None`——
  **没有可靠归属就不写任何任务账本，也不补造历史费用**，费用口径一律"未知"而不是 0。

### 3.3 定向测试（替身响应，零真实模型调用）

`test_llm_stream_fallback_and_bad_response.py` —— **12 例全过**：

| 组 | 覆盖 |
|---|---|
| 根任务归属 | 有 root 时回退调用正常入账（票数与记录数一致） |
| 未归属诊断 | 无 root 时调用**可见**、`tokens=None`、`cost_usd=None`、不生成任务账本 |
| 非 SSE 坏响应 | HTML → 失败且**不发布**；HTML → **不进成功缓存**且记 `bad_response`；坏 JSON → 失败不发布；供应商 error 对象 → 失败且回退票按失败结算 |
| 合法非 SSE | 纯 JSON 正文照常返回；且 `cache_key` 命中时**正常写缓存** |
| 请求/重试路径 | 流式 404 → 回退**只多一次**请求、无重试 sleep；探测**显式非流式且未归属** |

Lead 侧独立复核（不走子任务结论，直接调真实函数）：

```
HTML 响应          → 拒绝：LLMBadResponseError: 非模型响应：Content-Type 是 HTML
坏 JSON            → 拒绝：LLMBadResponseError: 非模型响应：响应体不是合法 JSON
供应商 error 对象   → 拒绝：LLMBadResponseError: 供应商返回错误对象（error），不是模型响应
合法非 SSE         → 通过，正文='报告正文'
未归属诊断： {'calls': 0, 'tokens': None, 'cost_usd': None, 'cost_known': False}
```

### 3.4 未验边界

1. **60 秒这个数字未采信**：本批只证明"探测每次白打一个被拒的 stream 请求"与
   "回退路径内无 sleep、唯一重试等待 0.3s"，**没有**把"每次多耗 60 秒"写进代码或报告；
   实测 60s 是 `_health_monitor_loop(interval=60)` 的两轮探测间隔。
2. 真实网关在探测去掉 `stream:true` 后是否立即 200、以及真实任务调用是否仍遇 404，
   **未实机验证**（零真实调用、未重启服务、未跑 8099 端到端）。
3. 真实任务调用仍"每次先试流式"；**未加端点级"流式不支持"记忆**——证据只支持
   "探测重复请求"，不支持"固定 60s 重复等待"，故未按后者改。
4. 多模态/工具调用响应（`content` 为数组等）会被判 `bad_response`；本仓当前文本用途
   未覆盖，未测。
5. 异步非流式坏响应的形状记录/重试是新增行为，已用替身测试覆盖，未在真实 worker 进程跑。
6. 未归属诊断是**进程内计数**（`get_unattributed_diagnostics()`），重启清零、未落库；
   是否持久化属产品/架构判断，本批不改存储形态。
7. 环境既有失败（与本轮代码路径无关，未修、未算作回归）：`test_setup_wizard` 4 例
   （本机 DNS 把公网域名解析到 198.18.0.0/15，被端点安全策略拒绝）、
   `test_task20_redis_degradation` 1 例（Redis 可用时 `degraded_mode=False`）。

---

## 4. 回归与验证纪律

| 命令 | 结果 |
|---|---|
| `python -m unittest test_source_exclusion_registry` | **20 通过 / 0 失败** |
| `python -m unittest test_orchestrator_claim_order` | **5 通过** |
| `python -m unittest test_llm_stream_fallback_and_bad_response` | **12 通过** |
| `python -m unittest test_orchestrator_v2` | 106 通过 |
| `python -m unittest test_writer_consolidation` | 60 通过 |
| `python -m unittest test_report_quality` | 49 通过 |
| `python -m unittest test_task24_dead_source_annotation` | 19 通过 |
| `python -m unittest test_search_quality_unified` | 80 通过 |
| `python -m unittest test_task22_report_compaction` | 28 通过 |
| `python -m unittest test_task25_disclaimer_enforcement` | 20 通过 |
| `python -m unittest test_llm_thinking_escape` + `test_task23_llm_failover` | 11 + 64 通过 |
| `python -m unittest test_root_budget` | 83 通过 |
| 语法核验 | `ast.parse` 通过（`orchestrator_v2.py`、`workers/report_generator_worker.py`、`llm_client.py`） |

> 环境注记：`test_root_budget` 一次报 `RuntimeError: Could not determine home directory.`，
> 定位为**本会话 shell 的 HOME/USERPROFILE 未导出**导致 chromadb 在导入期 `Path.home()` 失败，
> 与本次改动无关；导出 `HOME`/`USERPROFILE` 后 **83 例全过**。

- **零新增项目付费模型调用**：所有验证走缓存副本、夹具与替身；未提交任何真实任务。
- **费用口径**：本批无模型调用，费用 **0（无调用可计）**；既有任务的费用仍按
  `config.json` 无价格表 → **未知**，不写 0。
- 未跑全量测试、未重启服务、未改配置/密钥/权限/阈值、未加服务/worker/DB/DSL。

---

## 5. 未验边界（如实列出，不冒充完成）

1. **F1 尚未经真实 LLM 成稿验证**：本批用真实工单的检索缓存副本 + 替身指令验证，
   推理链与文本契约都覆盖了，但"模型在新注入措辞下实际怎么写 `## 数据来源`"
   需要一次真实跑批才能看到（本批零模型调用，未跑）。
2. **F1 的正文清理只针对被剔除 URL 本身**：散文句里 `[文字](url)` 会退化成 `文字`、
   裸 URL 会替换成"（该来源已按来源纪律剔除）"，**不删整句**；若模型改写/缩短了 URL
   字符串，按 URL 精确匹配不到的仍可能残留（已知边界）。
3. **F3 未做双进程真机演练**：替身测试覆盖认领失败零副作用与启动序，但
   "真起第二个编排器进程、看它是否被拒且活跃实例无感"未做（任务书禁止起第二套栈、
   禁止杀共享服务）。
4. `_brpop_with_deadline` 在结果键被外部删除时的超时行为未改（属 F3 的根因链上游，
   本批只堵住"我方删自己键"这一条）。
5. **F2 未在真实网关上验证**非 2xx + HTML 正文组合（零真实调用）；
   未归属诊断是进程内计数、重启清零，未落库。
6. 上一批已记录的未修项（B3 流式 404 非 SSE 回退损耗、B4 回退调用不入账）与
   审计 14 项**不在本批范围**，未动。其中 B4「回退调用不入账」在本批已由
   "未归属诊断 + 逐次记录"部分收敛，但**没有**为历史调用补账（不补造）。

## 6. 改动文件清单（供 Codex 选择性提交）

| 文件 | 类型 | 说明 |
|---|---|---|
| `orchestrator_v2.py` | 修改 | F1：剔除审计注册表、`_subject_anchors`、`_partition_source_items`、`source_injection_text`、双保险旁路过滤、检索桶落点；F3：`_cleared_stale_results` + `main()` 启动序 |
| `workers/report_generator_worker.py` | 修改 | F1：`_excluded_from_instruction`、`_drop_excluded_urls`、自动附录过滤与缺口改写、两条收尾路径挂接 |
| `delivery_pipeline.py` | 修改 | F1：`_exclusion_audit_members` + `repack_adopted` 纳入剔除审计成员 |
| `llm_client.py` | 修改 | F2：`LLMBadResponseError` 与三个校验入口、探测显式非流式、回退先校验再结算、未归属诊断（费用未知） |
| `test_source_exclusion_registry.py` | 新增 | F1 定向测试 20 例（含真实注入↔解析端到端、**生产收尾函数直调**的全剔除/混合两类、剔除审计落盘与打包纳入） |
| `test_orchestrator_claim_order.py` | 新增 | F3 定向测试 5 例（含启动序回归守卫） |
| `test_llm_stream_fallback_and_bad_response.py` | 新增 | F2 定向测试 12 例（替身响应，零真实调用） |
| `docs/evidence/20261021-architect-closeout/dsh_report.md` | 新增 | 本文件 |
| `docs/DeepSeek执行状态.md` | 修改 | 顶部当前账纠正（§7） |

F2 的其余验证还跑过 `test_cancel_semantics` 62、`test_offline_delivery` 流式子集 8、
`test_prompt_system` 39、`test_task_context` 43、`test_task_time_optimization` 11、
`test_startup_readiness` 77、`test_p0.TestLLMDeadlineBudget` 3（均通过）。

> 逐文件清单以本表为准：**新增必需源码/测试**如上；**未纳入**任何密钥、数据库、
> 大安装包（`tools/offline_bundle/out/**` 208MB）、日志、临时截图与 `.tmp/**`。
>
> 另需说明：`web_ui.py` 在工作树里**有**未提交改动，但**不是本批（F1–F3）产生的**——
> 一部分是上一批的正文截断透出（`delivery.truncated` / `truncation`），
> 一部分是 Codex 正在进行的 F4。本批自始至终未编辑该文件，是否提交由 Codex 判定。
