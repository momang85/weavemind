# 第一批证据：关闭本轮明确的验证缺口（2026-09-23）

指令：`docs/阶段D正常交付闭环执行指令_20260923.md` 第一批。审查基线 `80cabfe`。
边界：禁网内存反例 + 只读工作区产物；无模型调用、无付费实机；未改模型/权限/全局 0/0/0/模板。

## 1. 财务语义：可复算不等于原因已支持（三个生产函数反例）

### 1.1 改前基线（生产函数实测，全部误通过）

| 反例 | 改前 | 要求 |
|---|---|---|
| 归因：正文"净利率降幅大于毛利率降幅，主要来自收入规模下降对固定性费用摊薄的削弱"，底稿仅 `derived=[{metric:expense_ratio, metric_label:期间费用率}]`（无值/无算式/无输入） | `pass=True`（"归因主张 1 条；无对应材料支持 0 条"） | 标签行不构成支持 |
| 比率：正文"资产负债率下降，主要因为资产减少24.47亿元大于负债减少20.90亿元"，底稿仅 `debt_ratio` 两个 year、无 value/formula | `pass=True` | 无值无算式不得豁免 |
| 方向：正文"资产负债率下降，负债降幅11.78%、资产3.51%，分母降得更快，比率才下降" | `pass=True`，且 `claims=[]`（形状筛选没识别） | 方向与数值相反必须查出 |

### 1.2 改后（生产函数实测）

- 归因：`_derived_is_real` 要求**有效值 + 输入事实 + 算式**；机制类驱动（固定性费用/固定费用/固定成本/摊薄/规模效应）**派生读数一律不算支持**，只给待查提示。
  - 空标签 → `pass=False`；真派生（有值有算式）→ 仍 `pass=False`，`hints=['期间费用率 派生读数只作待查提示，不构成固定性费用机制的证明', …]`。
  - 有效正例（已准入披露摘录 + 定位）：`pass=True`，support=「原始披露（api_chunk 3（字符 120-180））谈到固定性费用/摊薄」。
- 比率：`_derived_is_real` 同样作用于派生路径；句内百分数按**该比率自己的分子/分母**归属（比率词自身区间不参与），并要求方向一致；新增"两期水平"通道（"由 25.42% 降至 23.24%"）。
  - 无值两期行 → `pass=False`；真派生两期 → `pass=True`。
  - 分母更快反例 → `pass=False`，gap=「句称分母降得更快，与所给百分数相反」；把"分母"改成"分子" → `pass=True`。
  - 两期水平方向相反（23.24%→25.42% 却写"下降"）→ `pass=False`。
  - 已采纳正文（`5d0116b6…`，15,721 字符）回归：比率 `pass=True`（4 条主张 0 缺口）、归因 `pass=True`（0 条主张），无新增误报。

### 1.3 删除 fetch_snapshot 准入捷径

- 改前 `_read_narrative_evidence` 把抓取快照的任意非空 `text` 合成为 `admission="admitted"`、`has_location=True`，locator 用 URL 或"第 N 页"（`page` 是接口片段号，见第 2 节）。
- 改后：只并入**记录自带** `admission ∈ {admitted, comparison}` 且 `has_location` 且 locator 非空的快照项；不合成、不用 URL 冒充位置。定向用例 `test_fetch_snapshot_does_not_manufacture_admission`：两条快照（一条无准入、一条自带准入）只并入 1 条；未准入抓取文本不构成归因支持。
- 同时修 `_driver_support` 读记录正文只认 `text` 的字段错误 → `text or snippet`（抽取记录存的是 `snippet`）。

## 2. 来源定位：接口片段不再冒认 PDF 页码

- **产物更正**：`evals/real/yanghe_ar2024_pages_20260922.json` 由 `page` 改为 `api_chunk`（`api_param=page_index`），补 `text_sha256`（每段正文独立 hash）、`raw_retained=false`、`raw_sha256_unverifiable`（旧 raw hash 不可复验，不再冒充校验值）；顶层 `location_kind=api_chunk`、`pdf_page_mapping=null`、披露日取自返回元数据（`notice_date=2025-04-29`）。
- **解析/定位**：`narrative_evidence` 新增 `chunk_offsets` 支持——无页码映射时 locator 写 `api_chunk K · 小节：…（字符 a-b）`，`page` 保持空；真 PDF 页码映射存在时仍写页码（回归用例保留）。
- **验证脚本**：`scripts/verify_material_chain_20260922.py` 改 `chunk_offsets`、去掉 `page_offsets`、修 `text`→`snippet` 字段错误，并新增两个分开的口径。实测：
  - 6 段 `text_sha256` 全部一致；7 条已准入定位记录，样例 `api_chunk 2 · 小节：第三节 管理层讨论与分析 > 一、报告期内公司所处行业情况（字符 2744-3025）`；断言"定位里不得出现页码形状"通过。
  - **收入解释核对（修字段后）**：已分类/已定位记录里收入因果句 **0** 条；原文片段全量里有 **1** 条——`api_chunk 3`（字符 9607-10820，"四、主营业务分析 > 1、概述"）："…中端和次高端价位段承压较大…积极调整经营策略…2024 年实现营业收入 288.76 亿元，同比下降 12.83%"。该小节当前被分类器判为 `kind=None`，**未进入已分类记录**（归因链拿不到，属第二批待接通项）。
  - 利润/现金：同段仍只有读数、无原因表述 → 保持缺口。
- **抓取脚本**：`scripts/fetch_yanghe_ar2024_bounded.py` 改为输出上述诚实字段；披露日缺失时写 `unknown`（不再用硬编码 `2025-04-29` 补齐）。本轮**未新增 HTTP**（沿用已有缓存完成接线）。
- **orchestrator 旧分支**：`orchestrator_v2._try_pdf_evidence` 无 `pdf` 标记的旧路径里，工件在但读不出/校验失败时**保留缺口**，不再退成 `data=None` 走 URL 重抓。用例 `test_old_branch_artifact_failure_does_not_refetch_by_url`（断言 `doc_from_url` 未被调用）。
- 证据文档 `docs/evidence/night_closure_batch1_2_20260922.md` 的"第 3 页/真实页码"改为 `api_chunk 3` 并注明定位口径更正。

## 3. 账本锁恢复分支（False→True）

- 场景：初始化拿锁 `False`（不写、空身份、标不确定）→ 期间另一进程建立账本身份 → 本进程后续 `_save` 拿锁 `True`。改前会把**空身份**写进账本并与磁盘身份不匹配，旧 `writers`（别人的计数）被丢。
- 改后 `_save_locked`：空身份时先在锁内重读，**采纳磁盘身份或建立新身份**（并重置增量基线），再合并 `writers`；空身份一律不普通保存。
- 用例 `test_lock_recovery_adopts_peer_identity_and_keeps_writers`：flaky 锁（第一次 False、第二次 True），第二次拿锁前写入 peer 账本（`ledger_id=peer-run`，`writers.peer-tag`，`calls_reserved=3`）。断言：落盘 `ledger_id=peer-run`、`peer-tag` 保留、`calls_reserved>=3`；再存一次不翻倍。

## 4. 本轮定向测试

| 文件 | 结果 |
|---|---|
| `test_delivery_chain` + `test_acceptance_adversarial` + `test_narrative_evidence` | 389 通过 |
| `test_narrative_evidence`（含新增 api_chunk 2 例） | 45 通过 |
| `test_root_budget`（含新增锁恢复 1 例） | 76 通过 |
| `test_financial_chain` | 34 通过 |
| `test_report_quality` + `test_offline_delivery` + `test_r0_boundaries` | 113 通过 |

替换的错误正例：归因的"空标签费用率放行"与比率的"无值两期行放行"改为**真派生/带定位披露**正例；新增 `test_ratio_direction_statement_contradicting_numbers_is_caught`、`test_fetch_snapshot_does_not_manufacture_admission`、`test_old_branch_artifact_failure_does_not_refetch_by_url`、`test_lock_recovery_adopts_peer_identity_and_keeps_writers`、`TestApiChunkLocation`（2 例）。

## 5. 未验项（如实）

- 未新增 HTTP：api_chunk 定位用**已有缓存**完成；响应字节仍未落盘（raw hash 标不可复验，正文用 text_sha256 核对）。
- "四、主营业务分析 > 1、概述"未分类导致收入解释进不了归因链——本轮只如实记录，未改分类器（第二批接通资料准入链时处理）。
- 未跑全套 46 文件；只跑受影响模块（见上表）。真实 Redis 多进程、正常页面重包、付费实机仍未验（按指令边界）。
