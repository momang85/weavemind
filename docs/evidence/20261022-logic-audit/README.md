# 织光（WeaveMind）全面代码逻辑审查 — 2026-10-22

- **性质**：只读审计。未修改任何产品代码/测试/配置；未启动服务栈；未执行真实任务；未做任何 git 写操作；未从 `.env`/`config.json` 复制任何密钥值。
- **方法**：主审计会话直接通读关键模块（web_ui.py、orchestrator_v2.py、task_state.py、report_version.py、delivery_pipeline.py、worker_base.py、acceptance_checker.py、search_coverage.py、adapters/search_runner.py、admission.py、llm_client.py 关键区间），并用两个并行子审计分别覆盖（A）material_intake/admission 与（C）search_coverage/search_runner + llm_client + task_state；另对若干无法静态确证的机制在 `.tmp/audit_logic/` 下用只读探针脚本做了行为验证（未起服务、未发真实请求）。部分子代理报告以 `（子审计A/C 贡献）` 标注，其中结论均要求给出 file:line，主会话抽查复核过关键行号。
- **诚实性声明**：本审计**不**写"未发现漏洞"。每条结论附 `file:line` 与推理链；推测性判断一律标 `未验证`；未读过的代码不冒充已审（各文件末尾有"已读覆盖/未覆盖"清单）。`dist/weavemind-2026.09.27.1-win-x64/`、`.2` 内存在旧版重复副本（root `web_ui.py:1814` 的 `_safe_workspace_path` 在 dist 副本为 1622/1632 行），主审计只审根目录现行代码，dist 副本一致性列为独立发现（P2-13）。

---

## 一、本会话已实测的三处（对 Lead 实测结论的独立复核）

### ① 页面正文 ≠ 磁盘交付产物（同源断裂 + 渲染层段落剔除）

**同意该症状，但根因定性需修正两处。**

**独立定位的根因链（已验证）**：
1. **同源断裂**：`/task/<id>/report`（web_ui.py:5170 起）与分享页 `_get_share_page`（web_ui.py:5132 起）把 `data["report"]` 交给 `_share_page_structured`（web_ui.py:2307），后者调用 `_report_parse_sources`（web_ui.py:2132 起）与 `_report_parse_disclaimer`（web_ui.py:2181 起）后**剔除**"来源"与"免责声明"两节，仅渲染 `structured.get("body")`（web_ui.py:5201-5202 一带）。而磁盘交付产物由 `report_quality.write_report_files`（report_quality.py:449，调用点 delivery_pipeline.py:1482）+ `delivery_pipeline.repack_adopted`（delivery_pipeline.py:1438，页面调用点 web_ui.py:7703）从**采纳正文全文**生成。所以"页面正文=交付产物删去两节的版本"是**构造性**的：页面恒短于产物。
2. **接口层正文也未截断**：任务 payload（web_ui.py:5377-5411，`"report": data.get("final_report") or data.get("report","")` 5384）取**全量**正文，未发现按字符数截断的代码（全仓 grep `[:N]` 截断报告的写法无命中）。
3. **但存在另一处真实"变短"**：`_report_parse_sources` 在来源区块内**提取不到任何合法链接项**时返回 `raw_section`（web_ui.py:2175-2176），此时正文保留；而**能**提取到时会整段删除。因此"页面短了一截"的直接机制是**段落剔除**，不是字符截断。

**与 Lead 结论的分歧**：Lead 的表述"页面版本被截断"不精确——(a) 机制是**成段删除**（来源/免责声明两节），非字符级截断；(b) 断点不止"来源"一节，`_report_parse_disclaimer`（web_ui.py:2181）同样独立剔节；(c) 同源断裂不止出现在"页面 vs 磁盘"一处：**API payload（全量）与页面渲染（剔节后）本身就不同源**，同一个任务的正文在系统内至少有三个形态（DB 全量 / API 全量 / 页面剔节）。

### ② 验收数字误判：双向都会发生（假 FAIL 与假 PASS），共复现出 3 条独立机制

**同意 Lead 的判断，并补充：该误判不只一个方向。** 本会话用 `.tmp/audit_logic/falsepass_probe.py` 与 `falsepass_probe2.py`（只读、可重跑，直接调用 `acceptance_checker.extract_financial_numbers` + `check_number_traceability` 的纯函数）实测出 **3 条独立机制**：

**机制 A（假 FAIL：代码/示例数字进分母）**——样本"洋河股份（603288）"、"写作规范示例 2.88763"：

```
count= 6
603288 | unit= ''
2.88763 | unit= ''
2.88763 | unit= ''
331.26 亿元 | unit= '亿元'
...
pass= False rate= 0.0 total= 6
调研报告数字覆盖率过低：0/6，疑似模型知识未标注；数字溯源率 0%（0/6）；
不可溯源示例：603288、2.88763、2.88763
```

机制链（acceptance_checker.py）：`_NUM_UNIT_RE`（:187 起）把任何数字串都当候选；兜底排除（:310-324）只覆盖 `.HK/.SZ` 后缀、`0\d{3,}` 前导零代码（603288 不匹配——A 股以 6/3 开头）、纯年份、无单位 <4 位短数、以及"6 位数字且 `t[pos-16 : m.end()+8]` 窗口内出现字面「代码」"（:319-322，P1-3 补丁）；`（603288）` 括号样式与不带「代码」二字的表述**均无排除规则**（实测：只有字面「代码」出现在数字前 16 至后 8 字符窗口内才被排除，`（603288）` 不算；A 股 6 位代码以 6/3 开头，也不匹配前导零规则）→ 6 个分母里 3 个是代码/示例 → 0/6 → `research` 域按 `total >= 5 and rate < threshold`（:4000-4004；阈值来自 `traceability_threshold_for`，:3625）判 FAIL → **驱动无效反思重做烧预算**。

**机制 B（假 PASS：裸数字借证）**——探针 CASE②：正文裸写 `603288`（无任何来源标注），而来源文本里含**同一数字串**的另一种写法 `603,288`：

```
{"raw":"603288","source":"search_results"} → traceable（借证命中）
```

机制链（acceptance_checker.py:3891-3899 一带）：溯源比对把**去分隔符后的数字串相等**当作"可溯源"，`603288` 与 `603,288` 归一化后相同 → 代码/任意裸数字只要在来源文本里出现过同数字串（年份、页码、另一公司的代码、百分比分子……）即被判可溯源 → **假 PASS**。这不是 Lead 原述的 `603288 ↔ 2,887.63`（该具体匹配本会话**未能复现**，见下），而是同数字串借证。

**机制 C（假 PASS：样本数 ≤4 直接放行）**——探针实测 `total=4` 与 `total=3`：

```
total=4 pass=True rate=0.0
total=3 pass=True rate=0.0
```

机制链（acceptance_checker.py:4000-4010 一带）：`research` 域的阈值判定是 `total >= 5 and rate < 0.2` 形态——**total ≤ 4 时整个谓词短路为通过**，哪怕 `rate=0.0`（一个数字都没溯源到）。短报告/少数字报告的验收因此形同虚设。

**与 Lead 结论的对账**：(a) Lead 报告的 `603288` 匹配 `2,887.63` 这一**具体配对**未能复现（两者数字串不同：603288 vs 288763）；若该结论来自某次实机验收，需要那个任务的 `acceptance_json`（`details` 里的"不可溯源示例"与 `checks.number_traceability`）来定位走的是哪条机制——请 Lead 提供任务 id 或验收 JSON。(b) 三个机制可以叠加：机制 A 抬高分母造成假 FAIL，机制 B/C 造成假 PASS；修法必须同时覆盖（代码/示例排除 + 借证需主体/指标/期间对齐 + 样本数下限不能短路成"通过"）。

### ③ 任务检索覆盖率在第二轮预算拒发后回落 0%

**同意（有条件），机制链已独立验证。**

- 台账 `search_ledger:{tid}`：`_reserve_search_calls`（worker_base.py:1226-1257）`HSETNX started`（:1245）只写一次、TTL 7200（:1191、:1246）；`left` 用**首次检索时刻**起算的墙钟（:1256）——第二轮派发时剩余秒/次数已为 0。
- 拒发路径：`granted <= 0 or left_secs <= 0` → 返回空数组并记 `refused_budget`（worker_base.py:1418-1427）。
- 覆盖率口径：`refused_budget` 属非 `ok`/`no_results` 状态 → 归 `incomplete` 桶（search_coverage.py:73-74）；`coverage_rate = ok/total`（search_coverage.py:110）。
- **同 step_id 覆盖**：反思重做 `completed_all[s["step_id"]] = result`（orchestrator_v2.py:4092）直接覆盖第一轮 SUCCESS 的 ok 结果；劣化保护（:4071-4074）只覆盖 `report_generator`/`content_summary`，**不含 web_search** → `1/1 = 100%` 变 `0/1 = 0%`。
- 仓库注释自证两次实机编号：worker_base.py:209-212（`ui-d59152ebb9 / sched-f6e5440b8b`）。

**与 Lead 分歧/补充**：(a) 0% 只在"该任务是唯一检索步"时成立；反思若走**追加/替换步骤**（step_id 加 `i{iteration}-` 前缀，orchestrator_v2.py:7937-7940，并同步 `_bump_plan_version` 使旧评审 PASS 失效，:7942）或本就有多个检索步，会表现为 50%/33% 而非 0%；(b) `_dispatch_step_safe` 内部确有两道保留机制（orchestrator_v2.py:10316-10326 预算耗尽即**停止派发检索重试**、10423-10432 `best_search` **保留候选**并把最后一次失败原文留在 `contract_issue_note`），**单次派发内**不会自我覆盖——塌陷发生在**跨派发**的新一轮 `_dispatch_step_safe`（`best_search` 在函数入口重置为 None，orchestrator_v2.py:10284）；(c) 需分清两个口径：单步检索的"已取得候选"保留（已有）与**任务级**覆盖率（没有保留——covered 覆盖发生在 result 级，重做后仍以最后结果为准）。

---

## 二、最严重的 10 条 + 4 条补充（P0/P1 优先，行号均为现行工作树）

| # | 严重度 | file:line | 一句话 |
|---|---|---|---|
| 1 | P1 | web_ui.py:1995-2012 + orchestrator_v2.py:2581 | webui 内存 `_task_results` 只被 pubsub 写、跨进程采纳/恢复只写 DB → 导出恒 409 / MD-PDF"身份头=采纳版、字节=旧稿"（实机 ui-433f4cf9e2 自证） |
| 2 | P1 | acceptance_checker.py:310-324 | 数字抽取兜底排除不含"A股 6/3 开头股票代码与示例数字" → 溯源率 0% 误判、驱动无效重做（实测复现）；另有两方向假 PASS（借证 :3891-3899、`total≤4` 短路 :4000-4004，见实测②） |
| 3 | P1 | orchestrator_v2.py:4092 + worker_base.py:1245,1256,1418-1427 | 反思重做覆盖同 step_id 已成功检索结果 + 任务级检索台账跨轮共享 → 覆盖率 100%→0%、下游 web_fetch 全跳过 |
| 4 | P1 | common.py:450 BRPOP + worker_base.py:561-653 | 队列 at-most-once 且无 visibility timeout/requeue：worker 弹出后崩溃任务即永久丢失；编排器 WAIT_TIMEOUT 重派（orchestrator_v2.py:6171-6176）→ 同任务并发双跑/双计费 |
| 5 | P1 | report_version.py:182-189 + 192-204 | `VersionStore` 锁是**进程内** threading.Lock + 整文件读-改-写 + 固定临时名：orchestrator 与 webui 两进程并发 adopt/record 互相丢版本记录，`os.replace` 还可能因占用抛 PermissionError |
| 6 | P1 | web_ui.py:3992-3996 | do_POST 无 Content-Length 上限全量缓冲（ThreadingHTTPServer 无读超时/线程上限），单请求即可内存放大 OOM（admin 权限内） |
| 7 | P1 | admission.py:47-52 + memory_manager.py:770 | 准入谓词三个安全参数默认全在宽松侧（hard_ok=True/version_bound=True/review_bound=None），consolidate_memory 回退分支硬编码 SUCCESS → 谓词可被静默降级 |
| 8 | P1 | task_state.py:1417 + 1430-1434 | `merge_projection` 快照优先整体忽略更新的 overlay（白名单含 steps/logs 但**不含 report**）：页面 steps/logs 用过期快照、正文用 DB → 同一响应两个真值源 |
| 9 | P1 | web_ui.py:2132-2205 + 2307 + 5132/5170-5202 | 页面渲染构造性剔除"来源/免责声明"节，与磁盘交付产物/ API payload 不同源 → 用户看到的正文 ≠ 验收过的正文 |
| 10 | P1 | llm_client.py:2493-2494 | 流式非 SSE 回退分支把**原始响应体**（错误页/坏 JSON）当模型回答返回并进缓存 → 错误页可进交付正文 |

P0 判定说明：本次审计未发现"越权+资损+数据损坏"级组合漏洞——web 层鉴权 fail-closed（Bearer 优先于 Cookie，web_ui.py:3651-3683）、`/files/` 公开前缀仅限 `reports/`/`charts/` 且需有效分享授权（web_ui.py:3685-3706、4493-4507）、admission 谓词无 except 放行链。最接近 P0 的是 #4（并发双跑含真实计费）与 #1（导出恒 409 属可用性死锁），按"错误结论/状态不一致"定为 P1 顶格。

### 补充：末段（专题 09-13）新增的 P1

> 行号口径：本次审计期间工作树相对 HEAD 有未提交改动（`git status --short` 显示 `M acceptance_checker.py`、`M orchestrator_v2.py`、`M web_ui.py` 等，故本文件早期引用的行号曾整体偏移 20 余行）。**全文行号以审计当日工作树为准**，每次引用前均以 grep 复核。

| # | 严重度 | file:line | 一句话 |
|---|---|---|---|
| 11 | P1 | acceptance_checker.py:1555-1607 + orchestrator_v2.py:9210-9230 | 来源收集 6 处 `except: pass`（含 `search_results.json`）+ 写入方非原子直写 → 来源文件"存在但解析失败"时整类来源**静默消失** → 溯源率塌到 0% / `overall=fail`，且验收结论里没有"来源读不到"的任何原因（假 FAIL，无诊断） |
| 12 | P1 | web_ui.py:5655-5664（对照 6768-6769 承诺） | `human_review.json` 的 `version_id` **不与当前采纳身份比对**：走 `/api/task/<id>/review/edit` 出新版后，旧版人工批准仍显示"已复核"（前端只在括号里给哈希，无"非本版"措辞），而该路由 docstring 明确承诺"旧人工批准不迁移"——承诺只兑现了机器验收与计划评审两项 |
| 13 | P1 | web_ui.py:6994-7001 + 3992-3996 | 材料上传**先 base64 解码再查大小**、且 `do_POST` 无 Content-Length 上限：超大 body 在 413 之前已把 2 份超大对象读进内存 → 单请求 OOM（与 #6 同一根因的两条入口） |
| 14 | P1 | orchestrator_v2.py:12039-12044（先于 12063 认领） | 第二个编排器进程启动时**先** `r.keys("task_result:*")` + `delete` **再**做实例认领：认领失败退出前已把**在活编排器手里**的结果通道删光 → 其全部在途步骤等到 WAIT_TIMEOUT 全部 FAILED（防御性清理的误伤面是"别人的在途数据"） |

---

## 三、分主题明细（每条：`严重度 | file:line | 现象 | 触发路径/输入 | 影响 | 建议修法 | 证据 | 是否已验证`）

1. [web_ui 路由 / 鉴权 / 路径穿越](01-web-ui-routing-auth.md)
2. [orchestrator 派发 / 重试 / 幂等](02-orchestrator-dispatch-idempotency.md)
3. [task_state 投影 / 缓存一致性](03-task-state-projection-cache.md)
4. [交付管线 / 版本绑定](04-delivery-version-binding.md)
5. [llm_client 计费 / 重试 / 四处 _NO_REDIS_RETRY](05-llm-client-accounting-retry.md)
6. [worker 队列语义 / ack / 重复交付](06-worker-queue-ack.md)
7. [acceptance_checker 数字与谓词](07-acceptance-numbers.md)
8. [search_coverage / search_runner 预算](08-search-coverage-budget.md)
9. [material_intake 上传边界](09-material-intake-bounds.md)
10. [admission 准入谓词](10-admission-predicates.md)
11. [并发与资源（sqlite / 句柄 / 锁 / 临时目录）](11-concurrency-resources.md)
12. [三态分离（机器验收 / 研究状态 / 人工复核）](12-three-state-separation.md)
13. [except-pass 静默吞错热点](13-except-pass-hotspots.md)

---

## 四、审计边界与未覆盖

- 未启动服务栈、未发真实 HTTP 请求、未连 Redis/SQLite 生产实例——所有"运行时行为"结论要么来自只读探针脚本（纯函数级），要么标注"静态/未验证"。
- 本次审计期间**工作树相对 HEAD 有未提交改动**（`git status --short`：`M acceptance_checker.py`、`M orchestrator_v2.py`、`M web_ui.py`、`M delivery_pipeline.py`、`M report_version.py`、`M test_p0.py` 等），行号随改动整体偏移过 20 余行——本文件与各专题文件的行号均以**审计当日工作树**为准、引用前逐条 grep 复核；换基线（提交/回滚）后需重新对行。
- 只审根目录现行代码；`dist/` 旧副本、`.tmp/` 历史探针、`*_trial_*.py` 调试脚本未审。
- 专题 10 的 `templates_pipeline` / `prompt_refinery` 旧"默认放行"路径、专题 12 的 `research_state` 写入方状态机、专题 09 的 `adapters/disclosure_ingest` 判据，均只做了引用层 grep，未逐行审（各专题文件末尾有清单）。
- `test_*` 平铺测试文件（82 个）只做抽样交叉引用，未逐个审读。
- 前端 `frontend/src` 只审 ReportViewer 等关键组件的相关行为，未全面审计。
- dist/ 目录内旧副本只做行号对照，未全面审计其与现行代码的差异全集。
