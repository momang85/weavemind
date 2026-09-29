# Q0-③④ 检索总截止残余（chunked 慢分块头 / 诊断抬预算）+ 资料期间正式名与倒装年份

- 基线：`523d4c6`（Q0 前两个小提交之后）。
- 审查依据：`docs/金融分析与受控建模阶段Q_20260929.md` §7 Q0「检索总截止」「资料期间」、
  `docs/evidence/20260929-stage-review.md` 表格第 4–6 行。
- 范围：`adapters/search_runner.py`、`search_diag.py`、`execution_contract.py`、
  `test_search_quality_unified.py`、`test_delivery_chain.py`。
- 未新增付费整跑、未改阈值/模型/权限/代理/模板、未碰真库、未发真实外网请求
  （下面全部是**进程内 socketpair + 真实 `http.client.HTTPResponse`** 或纯函数）。

## 1. chunked 慢分块头（改前最多超预算 15.9 倍）

形状：进程内 socketpair 对端**逐字节**发一个合法 chunked 响应；客户端是**真实**
`http.client.HTTPResponse`；预算 0.02s。

| 形状 | 改前 | 改后 |
|---|---|---|
| 分块头 6 字节、每 5ms 一字节 | 0.0269s | **0.0218s** |
| 分块头 6 字节、每 20ms 一字节 | **0.1022s** | **0.0203s** |
| 分块头 60 字节、每 5ms 一字节 | **0.3182s** | **0.0217s** |
| 分块头 60 字节、每 2ms 一字节 | **0.3128s** | **0.0225s** |

根因：分块头不是"响应体数据"，是 `HTTPResponse._get_chunk_left() → fp.readline()` 读出来的，
而 `readline` 是**一次调用、内部连续 recv 直到换行**。原来的两道防线都不覆盖它：

- 外层循环"每次 `read1` 返回后再查钟" → 只能等 `readline` 自己读完；
- "把剩余时间设到 socket" → 每次 recv 都在超时内成功（一字节 5–20ms），照样继续读。

修法（`_DeadlineRaw` + `_wrap_response_deadline`）：把 `resp.fp` 换成
`io.BufferedReader(_DeadlineRaw(原 fp))`，`readinto` **每次只向原 fp 要一次数据**
（`read1`），读前设剩余 socket 超时、读后查钟——"连续 recv"因此被拆成
"每次 recv 之间都有一次时钟检查"。

两个刻意的设计选择（都有用例守）：

1. **从原 fp 读、不绕过它读 socket**：`begin()` 解析响应头时 `BufferedReader` 可能已把正文
   预读进自己的缓冲，绕过它会把已到的正文丢掉。用例
   `test_wrapping_the_response_stream_does_not_lose_prefetched_body`（同包到达的
   content-length 与 chunked 完整体都必须逐字节读全）。
2. **套不上不放宽**：没有 `fp` / 只读 / 已经是包装 → 返回 False，仍走原来的
   `read1`+时钟检查；`require_bounded=True` 时"既无 read1 又设不上超时"照旧
   `UnboundedReadError` 明确拒绝（既有用例不变）。

生产两处消费点（`adapters/text_search.py:63-64`、`worker_base.py:1412-1413`）都是
`urlopen(...)` → `read_with_deadline(resp, deadline)`，因此自动继承本修复。
**仍未覆盖**：`urlopen` 返回前的**响应头阶段**（connect + status line + headers）只受
请求时那个 socket timeout 约束（= `provider_timeout` = min(提供方上限, 剩余)），
即最坏情况可以把剩余预算用光但不会无限超出——本批未改这条，如实记账。

## 2. 诊断抬高剩余预算（`search_diag`）

| 项 | 改前 | 改后 |
|---|---|---|
| `probe_search_sdk` | `wait = max(3.0, min(剩余, 8.0))`：只剩 0.5s 也打一次最长 3s 的同步 SDK 调用 | 剩余 < ddgs 可行下限 → **一个请求都不发**（`refused_budget`，且**不消耗额度**：拒绝发生在 `budget.take()` 之前）；否则 `wait = min(剩余, 8)` |
| `probe_search_html` | `_fetch_bing_html(PUBLIC_SAMPLE)` 用默认上限，不看剩余 | 传 `provider_timeout("bing", 剩余)`；低于下限直接拒绝 |
| 事后记账 | `_budget_check` 只比 `MAX_SECONDS` | 增加 `allowance`：`elapsed > 允许 + 0.25s` 即改判 `timeout` 并记 `overran_budget`（同步 SDK 不能中途取消，"回来晚了"绝不算成功） |

实测（`python -c` 直调）：剩余 0.4s 时两条通道都返回 `refused_budget`、额度 `6→6`；
剩余 3.0s 时 `DDGS(timeout=3.00)`（不被抬到 3s 下限之上）。

## 3. 资料期间：正式名「半年度报告」与倒装年份

`execution_contract.conflicting_periods` 的两处漏口（均为审查复核实测）：

| 反例 | 改前 | 改后 |
|---|---|---|
| `洋河股份2024年半年度报告 全文` | 放行（年份在 `periods` 里就 `continue`；期间词表只有口语简称"半年报"，**没有正式名**） | 冲突：`半年度报告`、`半年度` |
| `洋河股份 年度报告2025公告` | 放行（只查年份**后面**的报告期词，倒装写法看不到） | 冲突：`年度报告2025` |
| `2024年度报告，2025年4月披露`（正例） | 放行 | **仍放行**（倒装判据要求紧邻；年份后是"年X月"= 披露时间） |
| `洋河股份2024年度报告2025年4月公告`（正例） | 放行 | **仍放行** |
| `query_in_contract("洋河股份 2024年半年度报告 全文")` | `(True, "")` | `(False, "含契约外期间 半年度报告、半年度")` |

**硬边界仍在结构化摄取**（文本判据只是辅助）：`disclosure_ingest` 用**文档自己的**报告期
（标题里的 `2024年年度报告`）判定，半年度/倒装标题取不到报告期 → `period_unknown` 拒绝
（fail-closed）；正例 `2024年年度报告 + 2025-04-28 披露 + as_of 2025-04-30` 照常通过。
新增用例把这条"辅助 vs 硬边界"的分工写死（`test_hard_boundary_is_structured_ingestion_not_the_text_hint`）。

## 4. 定向验证

| 套件 | 结果 |
|---|---|
| `test_search_quality_unified` | **80 OK**（+3：真实 HTTPResponse chunked 慢头有界；包装不丢预读正文；诊断不抬预算） |
| `test_delivery_chain` | **403 OK**（+3：正式半年度报告名判冲突；倒装年份判冲突；硬边界在结构化摄取） |
| `test_p0` | 434 OK |
| `test_orchestrator_v2` | 81 OK |
| `test_offline_delivery` | 34 OK |
| `test_r0_boundaries` / `test_narrative_evidence` / `test_financial_chain` | 47 / 68 / 36 OK |

## 5. 仍未验

1. **真实外网**未探测（本批全部进程内替身）：不能据此声称用户网络或金融站点已恢复，
   也不能声称真实 Bing/DDGS 路径的截止表现已验（"形状"已验，"链路"未验）。
2. DDGS 同步 SDK **卡住**仍只能事后记账 + 事前拒绝（低于下限不发）——它不可取消，
   本批没有把它塞进进程边界（架构上属更大改动）。
3. 响应头阶段的总截止未改（见 §1 末）。
4. `_promotion_subject` 散文前缀误判（Q0-① 已量化）仍未修。
