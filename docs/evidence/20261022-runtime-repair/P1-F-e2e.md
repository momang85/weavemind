# P1-F：momang 账号端到端实测（RUNTIME-1022 收口验证）

- 执行时间：2026-10-23 00:19–00:46（本机）
- 执行者：Team Lead（lead），goal `goal-aae0ccea-5414-416f-a154-3a8bba89ce5d`
- 前置：受控重启已完成（见 `P1-E-restart-plan.md`）；重启后 `/api/health` 已验 `build` 字段与 `loaded_sha == disk_sha`（`inst-cf0d8d8dd0`，pid 33840，started 2026-10-23T00:13:40+0800）。

## 1. 测试入口（走真实账号面）

1. `POST /api/login`（momang / 密码从任务书）→ `status=ok`，role=`admin`；会话仅经 HttpOnly Cookie（响应体不带 token）。
2. `POST /task` 提交范围内研究任务，带结构化契约：
   - goal：研究伊利股份（600887.SH）2023/2024 年报：营业收入、归母净利润、经营活动现金流净额，给出可复算底稿与来源定位
   - `research_request`：company=伊利股份 / company_id=600887.SH / market=cn / caliber=合并 / as_of=2025-04-30 / periods 2023–2024
   - 响应当场回 `research_intent`：`intent=company_research, status=in_scope`（P0-C 契约链生效）
   - task_id：`ui-2bcfcbc06d`（project=`runtime-repair-e2e`）

## 2. 运行中观察（P0/P1 机制逐项核验）

| 机制 | 实机证据 |
|---|---|
| 任务级检索预算 | Redis `search_ledger:ui-2bcfcbc06d`：`used=6, started=1792685988, ttl≈7200`（6 次/60s 上限） |
| 检索真值记账 | 日志 3 条「检索真值」：initial 2 次各 3 真实请求取 10 候选；后续 `refused_budget`（0 次请求，如实记"未发出"） |
| 阶段预算（P1-D） | `search_attempts.json` 6 条：r0 initial×2（web_search 真检索）、retry×1（无关来源换查询）、repair×3（重规划代做，标注"非检索结果不计入有效证据"） |
| 采纳证据不被空结果顶掉 | 第 5 次派发返回 `[]`，`search_adopted.json` 仍保留首轮 5 候选那份（`steps.1` 未被覆盖） |
| 来源四分（P0-C） | `source_pending.json` 5 条待核验（bing 重定向页，正文未核验）；`source_exclusions.json` 剔除 0；`dead_sources.json` 1 条（抓取失败链接，报告内标注"⚠️ 链接已失效"） |
| 降级诚实披露 | 报告正文前置声明：5 条检索结果全部与标的无关（日文年历/维基年份页），已整体剔除；核心数值全部标注「基于模型知识，未在本次检索中验证」 |
| 反思停损 | 00:45:32 `Reflection early-stop: 输入/正文/验收缺口与上一轮完全相同，重做无收益` |
| 机器验收 | `overall=fail`（数字溯源率 0%（0/10），财务域低于阈值；引用 0/计算 0）——验收**没有**被洗白为 pass |
| 人工复核分离 | `accepted_by=inst-cf0d8d8dd0` 是实例归属（登记列），非人工签字；`review_state.json` verdict=PASS 为 critic 关闭时的"降级放行"记录（required=false），无人工签字字段 |
| 交付冻结快照 | `deliverables_20261023_004504.zip`：9 成员，PACKAGE_MANIFEST.json 8 文件哈希逐一复算全部一致 |
| 最终状态 | `SUCCESS_WITH_ISSUES`（completed 2026-10-22 16:45:33 本地记录）；研究状态 `in_scope_no_structure`（范围内、结构未建，未冒充"研究就绪"） |

## 3. 本轮实测暴露并当场修复的缺陷（诚实边界增强）

**现象**：搜索引擎被 DNS fake-IP 劫持返回无关重定向页时，首轮结果 `admitted=0`
（全部 pending/无关），但 `search_adopted.json` note 写「只含已通过准入的来源」，
与实际（admitted=0）自相矛盾。

**修复**（本轮完成，未重启实例——在线验证的是"旧逻辑下的台账行为"，新逻辑已由测试锁定）：

- `search_evidence.py`：`AdoptedStore.to_dict` note 改为如实表述——step_results 保留步级
  最优结果（candidates 留给下游抓取），admitted 为通过准入数，pending 需抓正文核验才转正。
- `orchestrator_v2.py::_finalize_search_adoption`：采纳时回填 `adopted_admitted` 计数；
  admitted==0 时附 `adopted_note`（"尚无可直接引用的已准入来源，报告引用需以正文核验为准"），
  不把 pending 冒充证据，也不把候选判死（下游抓取仍可转正）。
- `test_search_evidence.py` 新增 2 例（22 tests 全绿）：
  `test_zero_admitted_search_is_pending_not_rejected`、`test_zero_admitted_flow_is_marked_pending_not_flagged`。

**回归**：`test_search_evidence.py` 22 OK；`test_search_candidate_kept.py` 13 OK；
`test_task19_search_coverage.py` 26 OK；`test_source_admission.py` 19 OK。
（曾尝试"admitted==0 就不盖采纳章"的更严方案，回归暴露它与 source_admission 的
pending 语义冲突——搜索候选未抓正文前 admitted 必然为 0，会误杀正常待核验流程，
故改为"如实计数 + note 披露"，语义与四分模型一致。）

## 4. P1-E 重启后验收项（§5）核验结果

1. **build identity** ✅（重启当轮已验；本轮结束时因上述源码修复变为
   `changed=['orchestrator_v2.py','search_evidence.py'], restart_required=True` ——
   指纹机制如实上报了"修复后的磁盘 ≠ 在线"，这本身就是 P1-E 验收点在工作）。
2. **历史任务只读复核** ✅：3 个历史任务（`ui-631efe6983` 10-22 / `ui-b23642e5de` /
   `ui-d59152ebb9` 10-21）报告均只读可读（8523/4124/9869 字符，内容完整含未验收
   草稿标注与降级说明）；旧任务工作区目录已不存在（历史布局），deliverable 以
   task_history 报告与冻结 zip 为准，未发生哈希变化动作（只读，无写入）。
3. **范围内研究实测** ✅：见 §2（契约链、预算、四分、两本检索账、诚实降级、
   审计四件套 `audit/` 成员 search_attempts/search_adopted/source_exclusions/
   source_pending 全部进包）。
4. **日志隔离抽检** ✅：生产 `logs/` 92 个文件无 test_/unittest/pytest 前缀；
   测试日志落系统临时目录 `weavemind-test-logs/`。

## 5. 实测暴露的环境与产品问题（不在本轮修复范围，如实上报）

- **DNS fake-IP 劫持**：`bing.com` 等域名在本机被解析为 198.18.x（代理 fake-IP 段），
  搜索返回日文年历等无关重定向页 → 这是"检索质量差"的根因，也是准入正确把它们
  剔除/标 pending 的原因。SSRF 防护按设计拦截，未放行。
- **LLM 端点**：备份端点 `DeepSeek-V4.1-Flash` 404；embedding 指向
  `tokenrhythm.studio/v1` 的 deepseek-flash（400 MODEL_CAPABILITY_NOT_SUPPORTED:
  embeddings）→ memory_manager 告警（不影响主流程）。
- **报告超字数**：主文 8328 字符 > 3600 目标（已有 WARNING，未阻断交付）。
- **能力-执行者错配**：反思轮多个步骤由 content_summarizer 代做（重规划代做，
  已如实记 WARNING 与"非检索结果不计入有效证据"标注）。
- **登录页明文密码提示**（10-21 已报）：登录页明文展示账号密码（密码值不写入
  证据文件，见任务书）——本轮按用户授权使用该密码登录，未改动该页面。

## 6. 结论

- 在线实例在重启后以 momang 真实账号面完成全链路研究任务：提交 → 契约 → 计划 →
  预算受控检索 → 准入四分 → 诚实降级报告 → 机器验收 fail 如实呈现 → 冻结交付包
  （哈希可复算）→ SUCCESS_WITH_ISSUES。三种状态分离无越界（无人工复核签字）。
- P0-A/B/C、P1-D、P1-E 的机制在实机全部有对应证据；本轮新发现的采纳语义歧义
  已修复并有测试锁定（尚未上线，需下次重启生效——build 指纹已如实标记）。


## 7. 修复上线后的第二次实测（e2e-2，2026-10-23 08:11–08:36）

**前置**：机器在 00:46–08:10 之间休眠，服务全停（`launcher.py status` 1/16）。执行
`stop` → `up` 受控重启，16/16 服务存活、Redis 可达、研究能力就绪（EXIT 0）。
`/api/health.build`：`loaded_sha == disk_sha == de24c0b55aba`、`restart_required=false`、
`changed=[]` —— 修复（`search_evidence.py` note + `orchestrator_v2.py` adopted_note）
已确认**在线生效**。

**实测**：momang 登录 → 提交同一范围内研究任务 `ui-7581c1a827`（契约完整，
`research_intent=in_scope`）→ 约 24 分钟 → **SUCCESS_WITH_ISSUES**，验收仍
`overall=fail`（数字溯源率 0%（0/39），缺口如实列出"报告缺少数据时效区块"等），
研究状态 `in_scope_no_structure`，critic `review_state` PASS 属机器评审、`required=false`。

**新语义在线证据**：
- `search_adopted.json` note 已是修复后的如实表述（"pending 需抓正文核验才转正，
  不在此处冒充证据"），steps[1].admitted=0 / candidates=5；
- `search_attempts.json` 5 条（initial 1 + repair 3 + 收尾 initial 空数组 1），
  末条 repair 如实记 FAILED；空数组未顶掉已采纳候选；
- 交付包 9 成员、清单 8 文件哈希复算 **0 不一致**；两本检索账 + 剔除/待核验清单全在包；
- 报告仍前置披露"未在本次检索中验证 / 基于模型知识"，未编造来源；
- 运行结束后 `restart_required` 仍为 false（运行未改源码，指纹无漂移）。

**回归**（修复上线前离线跑）：`test_search_evidence` 22、`test_search_candidate_kept` 13、
`test_task19_search_coverage` 26、`test_source_admission` 19、
`test_source_exclusion_registry` 22、`test_build_identity` 10、`test_log_isolation` 10、
`test_workspace_addressing` 17、`test_research_intent` 14、`test_net_diag` 15、
`test_orchestrator_v2` 106、`test_deploy_manifest` 41 全 OK；
`test_delivery_chain` 425 例 424 OK + 1 例已知 fake-IP DNS 环境错误（非回归）。
