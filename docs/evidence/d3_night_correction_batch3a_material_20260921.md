# 第三小批（一）：真实材料闭环（检索查询、抓取前排除、PDF 接入）

2026-09-21 夜间纠偏（指令 §3 第三小批 item 1–4，基线 `3f59bd8`）。范围：
`orchestrator_v2.py`（契约短查询、候选抓取前排除、PDF 通道触发）、`worker_base.py`
（`[检索查询]` 优先）、`workers/web_fetch_worker.py`（PDF 识别 + 走统一文档接入契约）、
`test_delivery_chain.py`（4 项定向用例）。**离线；未新增实机。**

## 1 反例（实机 ui-750185076a 的资料侧）

| 现象 | 证据 |
|---|---|
| 检索查询是**整段任务要求** | `logs/worker-search.log`：引擎探测与变体里含"每个数字须能回溯…阅读重点：银行对公客户研究视角（bank_corporate）"；duckduckgo/google/grokipedia/mojeek/startpage/wikipedia/yahoo 全 `No results found` |
| 候选里没有年报正文 | 10 条结果 4 条死链被丢，剩 6 条：新浪 2025-05-02 新闻、联合资信 2024 跟踪评级 PDF、上交所债券公告 PDF、东财研报 PDF（2025-11-05）、新浪 **2026-04-28** 文章、乐居财经点评 |
| 抓回的材料两份都不适用 | 2026 文章（晚于资料截至）与**集团**评级 PDF（错主体）；两份都是**抓完**才被证据校验排除 |
| PDF 以乱码进快照 | `fetch_snapshot.json`：`%PDF-1.5` + UTF-8 替换符，截 30 000 字符；`_try_pdf_evidence` 未触发（无"PDF 证据通道"日志） |

## 2 改了什么

### 2.1 查询从结构化契约生成（item 1）
- `orchestrator_v2._research_steps` step 1 指令新增一行
  `[检索查询] {主体} {代码} {各年度}年年度报告 营业收入 归母净利润 经营活动现金流净额`
  ——只放主体/期间/文档类型/指标，不带任务要求样板。
- `worker_base.SearchAgent._query_variants`：指令里有 `[检索查询]` 时**以该行内容为准**
  构造变体，并把它**排在第一**（`execute` 用首个变体探测存活引擎，干净查询必须先试）。

### 2.2 元数据已证明不适用的候选**抓取前**排除（item 2）
- 新增 `orchestrator_v2._candidate_inadmissible(title, url, contract)`，与证据层**同一套判据**
  （复用 `narrative_evidence._subject_state / _published_at / _doc_period`）：
  - **错主体**：标题里出现别家主体形态名字且不含本主体/本代码（"江苏洋河集团有限公司"
    与"洋河股份"是两个主体）；
  - **晚于资料截至**：URL 里的发布日 > `as_of`（含 `20251105` 紧凑写法）；
  - **报告期不在契约期间**：标题里的"20XX年年度报告/年报"不在契约期间内。
- 元数据**缺失不排除**（标 unknown，允许受限抓取后核实）。
- `_inject_step_context` 的抓取候选选择把研究契约（主体/代码/期间/资料截至）传进去。

实测（7 例，含正例）：

| 候选 | 结果 |
|---|---|
| 江苏洋河集团有限公司 2025 跟踪评级报告 | 排除（错主体） |
| 洋河股份2025年报解读（URL 2026-04-28） | 排除（晚于截至） |
| 某券商研报：白酒行业 2024 年报综述（URL 20251105） | 排除（晚于截至） |
| 洋河股份2021年年度报告 | 排除（期间不在契约） |
| 贵州茅台酒股份有限公司2024年年度报告 | 排除（错主体） |
| **洋河股份2024年年度报告（cninfo）** | **保留** |
| 投资者关系活动记录（元数据缺失） | 保留（待抓取后核实） |

### 2.3 PDF 接入现有解析通道（item 3）
- `workers/web_fetch_worker`：抓取改走**统一文档接入契约** `net_policy.fetch_document`
  （协议/主机/解析后 IP 边界校验、已验 IP 连接、不跟随重定向、字节上限与审计都在那一层；
  本 worker 不再自己发裸请求）。按 **MIME + 魔数**识别 PDF：`text` 置空、返回
  `pdf: true` / `content_type` / `content_bytes` / `content_hash`（sha256）与说明——
  **不再把截断字节当正文**。
- `orchestrator_v2._try_pdf_evidence`：触发条件加上 worker 的 `pdf` 标记（此前只认
  `.pdf` 后缀或 `%PDF-` 字节头），继续走既有解析/页码定位通道（`annual_report_pdf`）。
- 说明：本次实机那份 PDF 是**集团评级报告**，即使解析成功也**不得**放行（错主体 +
  晚于资料截至）——`_candidate_inadmissible` 已在抓取前挡住这类候选。

## 3 定向验证（离线）

`test_delivery_chain.TestMaterialSideBatch3`（4 项）：
- 变体里不得出现任务要求样板词（银行对公/须能回溯/bank_corporate/如实标缺口/阅读重点），
  且干净契约查询排第一；
- 7 个候选的排除/保留逐一核对（含元数据缺失不排除）；
- PDF 字节不被当正文：`text==""`、`content_hash` 等于字节 sha256、序列化结果里不出现 `%PDF`；
- `pdf` 标记能触发解析通道（解析不出正文时按缺口处理）。

回归：`test_orchestrator_v2` 68、`test_delivery_chain` 290（含把既有 IRI 编码用例改到新
传输上：抓取已归一到 `net_policy.fetch_document`，该用例原来打桩 `urllib.request.urlopen`）、
CI 清单 46 文件本地全绿。

## 4 未验项

1. 真实检索的**候选质量**要等下一次有界实机才能看（本批只做了查询构造与候选筛选的
   离线验证；引擎侧不可控因素仍在）。
2. `net_policy.fetch_document` **不跟随重定向**：以前 urllib 会跟随。目标返回 3xx 时
   worker 现在如实报失败（策略要求"每一跳都要重新校验"）——该行为变化未在实机路径上验。
3. 第三小批余下两项（**独立研究状态** `research_state`、**固定路线接统一 Critic**）未做，
   见下一批。
