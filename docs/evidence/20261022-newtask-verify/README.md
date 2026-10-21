# 20261022 新任务实机验证（织光后端 · 独立复核）

- **验证人**：backend verification 子代理（只读核验 + 仅一次真实提交）
- **实机墙钟**：2026-10-21 21:01–21:23（+0800）。目录名按派单要求使用 `20261022`，实际执行日期为 10-21。
- **栈状态**：沿用已启动的 16/16 服务（workbench http://127.0.0.1:8080，便携 Redis 6379），**未重启/停止任何服务，未改动任何产品源码或配置**。
- **本次唯一写操作**：`POST /task` 提交 1 个真实任务（无重试、无重跑）。

## 1. 任务概况

| 项 | 值 |
|---|---|
| 任务 ID | `ui-a0d82a26c4` |
| 会话 | `conv-3d4ae79a0f` |
| 提交时间 | 2026-10-21T21:01:41+08:00（DB 存 UTC `2026-10-21 13:01:41`） |
| 幂等键 | `verify-20261022-maotai`（`deduplicated` 为空 → 非去重回放） |
| 公司 | 贵州茅台 `600519.SH`（非金融、非海天味业） |
| 期间/口径 | 2023、2024 年度；合并；数据截至 2025-04-30；perspective=equity；subject_type=non_financial |
| 任务类型 | 两期经营研究（营业收入 / 归母净利润 / 经营活动现金流净额） |
| 终态 | `SUCCESS_WITH_ISSUES`，phase=`完成`，completed_at 21:22:56 |
| 耗时 | `elapsed_sec = 1275.0`（≈21.3 分钟，远低于 45 分钟上限） |
| LLM 调用 | `calls=20`，`failed=5`，失败原因**全部**为 `thinking_budget_exhausted`（end_reason：`empty_content`×4 + `thinking_budget_retry`×1；endpoint 全为 `primary`） |
| 数据获取 | 官方披露发现成功：巨潮 `1222993920.PDF`（2024 年报）`admitted`，835 小节 / 8 证据；`financials.json` 2 个年度；底稿 178 事实 / 16 同比 / 0 缺口 |
| 工作区 | `C:\Users\ding0\AppData\Local\Temp\dsh-win-bash-37684-7b69ad23\agent_workspace\tasks\projects\default\ui-a0d82a26c4\` |

**验收与交付**（`GET http://127.0.0.1:8080/task/ui-a0d82a26c4`，注意不带 `api/` 前缀）：
`overall=pass`、`gaps=[]`、`version_bound=true`、`report_version_id=6d5ec3b4…`；
但 check 级有 1 项未通过：`analysis_completeness` = “正文缺少结论段落”（`counted=false`，不翻转 overall）。
`search_coverage = {total:1, ok:0, degraded:1, zero_source:0, coverage_rate:0.0}`；
`delivery.status=verified`、`draft=false`；
`export.package = deliverables_20261021_212027.zip` **与磁盘唯一 zip 同名 ✓**，但 `package_stale=True`、`package_identity_ok=False`。
页面顶部研究状态横幅：**研究草稿／待补原始披露**（必答问题未完成 2/3，30 条肯定结论未证实）。

## 2. 七项逐条核验

### (a) 检索来源过滤一致性 —— **判定：未通过**

- 工作区来源文件 `…\ui-a0d82a26c4\project\search_results.json` 存有 **5 条无关候选**：`https://www.guizhou.gov.cn/`、`https://www.guizhou.gov.cn/?%B9%F3…`（query 变体）、`https://news.qq.com/rain/a/20241006A06ZBY00`（贵州旅游）、`https://doubao-me.com/`、`https://apps.microsoft.com/detail/xp99jw18tlbk06…`；文件内**无任何“剔除/不相关”标记字段**。
- 日志侧**无任何剔除记录可对账**：本任务在 `logs/orchestrator.log` 中无 “剔除 / 不相关来源 / irrelevant / source-bucket relevance filter / 失效来源清单(非 0)” 行（dead-link 过滤 dropped 0）。
- 机制实证（在本目标任务上直接调用）：`orchestrator_v2._filter_source_items`（`orchestrator_v2.py:10592`）→ `kept=5 dropped=0`。原因是 5 条候选与 `_goal_search_tokens(goal)` 的 20 个 token **全不命中**，命中集为空 → 触发兜底 `if not kept: return items, 0`（**fail-open：一条都不剩时保留全部**），无关来源整体进入来源桶。
- 交付文本自相矛盾：磁盘/包内 `…\ui-a0d82a26c4\reports\report.md`（**8165 字符**，LF-sha256 `b276bc7fb7d9110e3e21e14277cc47f88537c7c982d1907ddd6222339a886fb1`）第 110 行 `### 附录 C：已剔除的无关来源` 写明“已**全部剔除**，未用于本报告任何数字”，第 119–123 行 `## 参考来源` 又把同 5 条逐条列出并各自标注“（已剔除：与主题无关）”，第 129 行起的 `## 数据来源` 同样把 `guizhou.gov.cn`（含 query 变体与一条畸形条目 `[https://www.guizhou.gov.cn/",](…)`）、`news.qq.com`、`doubao-me.com`、`apps.microsoft.com` 全部列入（该文件内 `guizhou.gov.cn` 共 **8** 次、“已剔除”字样 **6** 处）。
- 清洗函数失效实证：`workers/report_generator_worker._strip_rejected_sources`（`workers/report_generator_worker.py:336`）对该正文运行后**删除 0 行**（输出与输入逐字节相同，diff=0）。原因：`_REJECT_DECL_RE` 只吃“关键字之后的片段”，实际捕获到的是 `与主题无关）`、`的无关来源` 这类无域名片段；而附录三只列名称不写 URL，故 rejected 集合为空。
- 附件层面同样泄漏：交付包 `deliverables_20261021_212027.zip` 内 `reports/report.md` 与本文件同源（sha 相同，`PACKAGE_MANIFEST.json` 记录一致），`project/fetch_snapshot.json` 内含已抓取的 `https://www.guizhou.gov.cn/`（4097 字符）→ 无关来源进入了**可下载交付物**。
- **缓解事实（重要）**：最终**采纳稿**（`report_versions.json` 中 `adopted=true`，identity `6d5ec3b4…`，body `0487478906b198e8…`，9617 字符）是**代码装配的研究简报**，只采用 2 条来源（cninfo、eastmoney），无关 URL 出现 **0** 次、也无“剔除”声明；页面主文正是这一份。即：**用户主文干净，交付包内的深度稿与附件不干净**。

### (b) 数字溯源率是否误判格式示例数字 / 6 位股票代码 —— **判定：通过（附残余）**

- 验收结论：`number_traceability` pass，“数字溯源率 92%（165/179）；引用 110 / 计算 55 / 模型知识 0；财务金额溯源率 95%（124/130）；定位元数据 15 个不计入分母；不可溯源示例：88.55亿元、0.54亿元、258.70亿元、10.00、10.00”。
- 复核脚本在**采纳稿正文**（9617 字符）上实跑 `acceptance_checker.extract_financial_numbers`：提取 **179** 个数字，与验收分母 `total_count=179` 完全一致；`unverifiable` 14 条示例为 `88.55亿元 / 0.54亿元 / 258.70亿元 / 10.00 / 10.00 / 17.98% / 19.04% …`，**全部是真实读数**，无格式示例数字。
- 正文内 `例如`、`示例`、`格式`、`科学计数法` 出现次数均为 **0** → 本轮不存在“格式示例数字”误判样本。
- 股票代码情形：`600519` 出现在 pos 41 与 8898，上下文均为 `贵州茅台（600519.SH）`。它**仍被提取并计入溯源分母**（依据 `acceptance_checker.py:255` 跳过规则：`.SH` 不在后缀白名单 `HK|T|O|N|US|L|A|B|SS|SZ`，且 `t[pos-16:pos+8]` 内无“代码”二字）；但两次都被判为 **traceable**（`source=fetch_snapshot`）→ **未制造“不可溯源”假缺口**。
- 定向单测（仅此 1 个用例，未跑全量套件）：`python -m unittest -v test_p0.TestAcceptanceChecker.test_spec_examples_and_stock_code_not_financial_numbers` → **OK**（Ran 1 test, 0.021s）。
- **残余（未构成本轮失分）**：6 位股票代码仍进分母，靠“恰好可溯源”兜住；建议按主体标识排除而非依赖跳过后缀表。

### (c) 时间戳是否为本地墙钟带时区偏移 —— **判定：未通过**

- `orchestrator_v2._now_iso`（`orchestrator_v2.py:1270`）实现正确：`datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")`，实机产出如 `…\ui-a0d82a26c4\step_failure.json:12`：`"timestamp": "2026-10-21T21:02:30+08:00"` ✓。
- 页面 `logs` 中 **68 条时间戳 65 条带 `+08:00`**，但 **3 条为裸 UTC**：`2026-10-21T13:09:52`、`…13:10:07`、`…13:10:16`，对应消息 `反思 开始`、`反思 进行中（已等待 15s）`、`反思 完成（耗时 24s）`。
- **根因定位**：`ws_helpers.py:74`（`_phase_event`）使用 `time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())`，未走 `_now_iso`。调用链为 `orchestrator_v2.py:3891 phase_begin(…,"反思")` 与 `3905/3907 phase_end` → `ws_helpers.py:68/100` → `_phase_event`。凡走阶段事件通道（phase begin/heartbeat/end）的时间戳一律为裸 UTC。
- 报告内唯一时间戳亦不带偏移：`reports/report.md` 的“数据截至：2026-10-21 21:01:45”来自 `adapters/eastmoney.py:197` 的 `retrieved_at = time.strftime("%Y-%m-%d %H:%M:%S")`（结构化财务数据的“数据获取时间”，经 `structured_pipeline/__init__.py:602` 注入报告）。是本地墙钟、**但无时区偏移**。
- 另见（不计入判定）：`project/content_filter_events.jsonl` 时间戳以 `Z` 结尾（UTC），属审计型记录。

### (d) “资料缺口：步骤 N” 措辞是否被非检索跳过误触发 —— **判定：无法判定（本轮无触发样本）**

- 本轮 7 个步骤全部 `SUCCESS`，**无任何 `gate_skipped`**：`step1 web_search`、`step2/2b web_fetch`、`step3 content_summary`、`step3a data_analyzer`、`step4 report_generator`、`package-7 package`。
- 全工作区文本检索 `资料缺口` = **0 次**（页面 report、`reports/report.md`、`reports/report_appendix.md`、`delivery_wrapper.md` 均无）。
- 相关但**未触发**该措辞的事件：日志仅有 `warning: 能力-执行者错配：web_search 步骤 1 由 content_summarizer 执行（重规划代做）；检索质量不可信，任务按有缺口交付`（`logs/orchestrator.log:25914`）与 `search_coverage.degraded_steps=1 / coverage_rate=0.0`；“资料缺口：步骤 N”与交付横幅只由 `_gap_steps`（`orchestrator_v2.py:8098-8130`）产出，而它只对通过 `_is_search_gap_skip`（`orchestrator_v2.py:8582`）判定的步生效，本轮无一命中。
- 结论：**无法用实机数据证明**“非检索跳过不会触发该措辞”（本轮没有非检索跳过发生）；源码判据（`cap` 非 `web_search/web_fetch` 时须显式 `gate_skipped` 才算缺口跳过）与日志行为不冲突。**建议下一轮专门构造 content_summary 被跳过的一次运行。**

### (e) `成果文件夹` 行是否为 `projects/default/<task_id>` 相对形态 —— **判定：通过**

- `…\ui-a0d82a26c4\delivery_wrapper.md:31`：
  `**成果文件夹**：`projects/default/ui-a0d82a26c4`（相对工作区根目录；每个任务独立目录，可整体移动/备份）`
- 既非裸 `ui-a0d82a26c4`，也非绝对临时路径；与 `orchestrator_v2._workspace_display_path`（`orchestrator_v2.py:6547`）实现一致，装配点 `orchestrator_v2.py:6446`。
- 补充：该字符串**只**落在 `delivery_wrapper.md`（`delivery_pipeline.WRAPPER_FILE`，由 `write_wrapper` 落盘），页面 `report` 字段与 `GET /task/<id>` 的其他字符串字段均不含此行。

### (f) 主题词闸门是否挡住明显无关来源 —— **判定：未通过（现象面通过，机制面失效）**

- 现象面（好）：落盘 `clean_chart_data.json` 的 `topic_terms` 无任何无关词（旅游/景区/豆包/下载/规划/知识产权均不出现），5 条无关候选**确实**未进入主题词。
- 机制面（坏）：在目标任务上复算 `clean_data._strong_subject_tokens`（`clean_data.py:527`），完整滑窗集合共 **42** 个 4 字窗，其中 **「贵州茅台」排第 39 位**，被 `tuple(sorted(out))[:12]` 按 Unicode 码点排序截断**丢弃**。实际生效集合为：
  `('两个年度','个年度的','个数字须','位置并可','到来源位','动现金流','合并报表','回溯到来','如实标缺','字须能回','实标缺口','年度的营')`
  → **公司名级门控完全失效**，只剩通用指令片段；无关来源是被这些片段“碰巧”挡住的（它们恰好不含“合并报表/现金流”类词），而非被主体识别挡住。
- 锚点闸门（2 字 bigram）放行了全部 5 条：豆包页含“阅读/研究”，旅游页含“贵州”，因此 `entity_frequency`（`clean_data.py:589-592` 只用锚点、不加强闸门）被无关来源污染——在 `project/search_results.json` 范围内复算 `贵州` 计入 **8** 次，全部来自 5 条无关文档（落盘 `entity_frequency` 因混入两份大来源文件而显示为 `贵州981 / 茅台958`）。
- `source_distribution` 不受闸门保护，直接统计全部文档 → 图表“数据来源分布”含 `guizhou.gov.cn:3`、`news.qq.com:1`、`doubao-me.com:1`、`apps.microsoft.com:1`。
- 结论：本轮热词未被污染是**侥幸**，不是闸门生效；一旦无关页面命中“合并报表/经营现金流”等通用段，即可穿透。

### (g) 截断闸门 + 三处正文哈希一致性 —— **判定：通过（截断分支）／同项另有未通过（哈希一致性）**

- 触发前提**不成立**：采纳稿（`04874789…`，9617 字符）结尾完整（`…见同版附件（analysis/analysis_detail.md）。` + 免责声明“据此操作风险自担。”），`finish_reason=""`（非 `length`），无中途断句。
- `report_versions.json`：6 个版本 `truncated` **全为 False**；采纳版 `truncation={truncated:false, signals:["分隔符不配对：（）+2"], evidence:{body_chars:9617, main_chars:8258, finish_reason:""}}`。
- 交付落地正确：`delivery.status=verified`、`draft=false`、无 `draft_reason`（未截断→不应落草稿，判断正确）。
- **三处哈希（如实给出）**：

| 位置 | 字符数 | LF-sha256 | 与采纳稿同源 |
|---|---|---|---|
| `acceptance_report.json.report_sha256`（=采纳稿 body=`version_id`） | 9617 | `0487478906b198e8d5b4350f7cf15d91f06491d65886270a6ff662d6b70d0baa` | ✓ |
| 页面主文（`GET /task/<id>` 的 `report`，含研究状态横幅 + `---` + 采纳稿全文 + 数据来源摘要卡） | 9957 | `674b72a45971094d463cbffc94021a8f53800edcc4bb0e19cf15168f6b5137e4` | ✓（`lf(采纳稿) in lf(页面)` = True，已逐字包含验证） |
| 磁盘 `reports/report.md` = 包内 `reports/report.md` | 8165 | `b276bc7fb7d9110e3e21e14277cc47f88537c7c982d1907ddd6222339a886fb1` | ✗（另一版深度稿） |

  → **验收对象与页面主文一致，磁盘/包内正文不一致**（第三份文本 = 未采纳版本）。
- 系统对此**有自标**（并非静默）：`export.package_stale=True`、`export.package_identity_ok=False`、`export.package_body_version_id=8e029cde…`（≠ 采纳稿 `04874789…`）、`package_report_version_id=f442b4a7…`；日志 `logs/orchestrator.log:25931` 明写“验收快照不是采纳版验收……当前采纳/交付的那版是 8e029cde33cc（9789 字符）”；`25940`“Reflection convergence: 重做后 best_report 未改善（上一轮 9789 → 本轮 8165 字符），提前终止反思”。
- 包内哈希自校验**通过**（`PACKAGE_MANIFEST.json` 用原始字节哈希：`reports/report.md` 声明 `b276bc7f…` = 实际；`model_report_full_ef4f379914e28410.md` 声明 `fdf1c32c…` = 实际原始字节哈希，其 LF 归一哈希为 `ef4f3799…`，非缺陷）。

## 3. 顺带记录

- 装配链时间线（`logs/orchestrator.log`）：21:07:05/15 深度稿验收通过（zip v1 21:07:19）→ 21:12:06 检索错配告警 → 21:20:42 另一版 8165→9784 → 21:22:03 “研究简报装配：由代码装配（关键数据/来源/声明）；采用来源 2 条” → 21:22:22 `27084→9617` → 21:22:35 验收通过 → 21:22:56 终态。
- 交付包内正文图片链接使用**绝对本机路径**：`![chart_3](C:\Users\ding0\AppData\Local\Temp\dsh-win-bash-37684-7b69ad23\agent_workspace\tasks\projects\default\ui-a0d82a26c4\charts\chart_3.png)`（`reports/report.md:100`、`:115`、`:139` 行），换机/打包外发即失效。
- 深度稿的 `## 数据来源` 只列 5 条无关 URL + cninfo（该文件内 `eastmoney` 出现 **0** 次），**遗漏**已实际采用的东方财富来源——而采纳稿的 `## 参考来源` 第 1 条正是 `东方财富数据中心（A股）`；同一文件里还出现畸形链接 `[https://www.guizhou.gov.cn/",](https://www.guizhou.gov.cn/",)`。
- `content_filter`：`count=14`，全部 `by_isolation.truncated`（上下文注入裁剪，`rule_id` 空），非内容拦截。
- 工作区未发现 `资料缺口提示` 横幅落盘痕迹（无该措辞）。

## 4. 未验证 / 不确定

1. **(d) 无触发样本**：本轮无任何跳过步，无法实证“非检索跳过不触发缺口措辞”，需构造 content_summary 跳过场景复验。
2. **(b)** 只跑了 1 个定向单测（`test_p0.py::TestAcceptanceChecker::test_spec_examples_and_stock_code_not_financial_numbers`），**未**跑 `acceptance_checker` 相关全量用例；`trimmed: true` 出现在验收 details 中，其裁剪逻辑未逐行核对。
3. **(g)** 三处文本差异的**根因**未追到底（只定位到“验收对象改为代码装配的候选稿”“主文收束”两条路径交替覆盖，以及 21:20 的反思终止）；未验证 `reports/report_body.md`（12.5 KB）与 `model_report_full_*.md` 各自对应哪一版。
4. **(c)** `content_filter_events.jsonl` 的 `Z` 时间戳是否属“审计轨迹豁免”未下结论；`_phase_event` 是否还有其他调用方（评审/规划阶段）本轮未逐一取样。
5. 检索错配（web_search 由 content_summarizer 代做）对最终数字的影响未深挖；本轮关键数字由底稿确定性计算（`working_paper` 178 事实/16 同比/0 缺口）与 cninfo 年报来源支撑。
6. 未做任何产品代码修改、未重启服务、未跑全量测试、未提交/推送任何内容。

## 5. 复现用脚本（只读，`repo/.tmp/`）

`nv_submit_maotai.py`（提交）、`nv_watch.py`（轮询）、`nv_ws.py`（工作区 ls/srch/filter/versions/accept/files/hashes）、`nv_num_probe.py`（(b) 数字提取复算）、`nv_gate_probe.py`（(d) 缺口判据）、`nv_topic_probe.py`（(f) 主题词闸门）、`nv_src_hygiene_probe.py` / `nv_bucket_probe.py`（(a) 来源卫生与桶过滤）、`nv_snap.py`、`nv_final.py`、`nv_final2.py`。
所有脚本仅从 `agents.db` 读取管理员 token 并以 Cookie 头使用，**不落盘、不打印** token。
