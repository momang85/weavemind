# 2026-10-22 页面级缺陷修复（S1–S6）— 前端修复与真机复验

上游依据：`docs/evidence/20261022-pages-visual/README.md`（真机视觉审计，缺陷 S1–S6、G1/G2）。
本目录是其修复后的复验证据。**只做前端**：后端 `.py` 一行未改（仅只读排查、只读定位根因）。

- 改动范围：`frontend/src/pages/{Skills,Settings,Memory,MetricsPage}.tsx` + 新增 `frontend/src/lib/*.ts` 纯函数模块 + `frontend/tests/*.test.mjs`。
- 验证（三绿，全部实跑）：
  - `cd frontend && npx tsc --noEmit` → exit 0（`strict` + `noUnusedLocals`）。
  - `node --test tests/*.test.mjs` → **178 pass / 0 fail**（修复前基线 126，本次新增 46 条断言测试，另有他人在同一分支新增的用例）。
  - `npm run build` → 成功（`✓ built in 9.17s`）。
- 真机复验：`http://127.0.0.1:8099/{skills,settings,memory,metrics}`，文本快照见 `recheck/*.txt`，截图见 `shots/*-after.png`。
  - ⚠ 环境变化：审计当时 webui 在 **8080**；本次复验时 **8080 已被 `GRW.exe`（Ghost Recon Wildlands）占用**，织光 webui 由 Lead 重启后改在 **8099**（页面引用 `assets/index-*.js` 与本次 build 产物哈希一致，确认是新构建）。因此本轮复验用 8099，未停/未起任何服务。
- 修复前的原文对照取自审计留存的 `.tmp/shots-pages/recheck/*.txt`。

---

## S1 Skill「自动沉淀教训」正文被硬截断成断句

**现象（审计）**：`frontend/src/pages/Skills.tsx:57` `String(l.issue || '').slice(0, 80)`，80 字一刀切且**不加省略号**，用户读到的是被切断的半句话。

**根因**：前端硬截断（`slice(0, 80)`）。另有两点实机事实必须一起处理：
1. 同一条教训的 `skill` 字段为空串，会同时挂在多张 Skill 卡片下（真机同一段文字出现在 data-pipeline / game-delivery / research-report 三张卡）；
2. 后端落库时另有一层截断：`skill_registry.py:147` `"issue": str(issue)[:300]`，**同样不加省略号**。真机上就存在正好 300 字、结尾停在数字「202」的教训——这部分前端永远拿不到，不能假装完整。

**改法**：
- 新增 `frontend/src/lib/skillLessons.ts`：`splitLessonSegments()`（按 `；;\n` 拆并列问题，段内不改写）、`skillLessonView()`（默认 `maxChars=0` **不截断**；确需限长时截断必带 `…` 并置 `truncated`）、`lessonTailCapped()`+`tailNote`（达 300 字且结尾非句末标点 → 标注「原文即止于此：教训落库时被后端按 300 字截断」，`LESSON_STORAGE_CAP=300`）、`scopeNote`（`skill` 为空 → 「未绑定具体 Skill…同一条教训会出现在多张 Skill 卡片下」）。
- `Skills.tsx`：整段完整显示、按分号渲染为多行列表、`break-words`；页脚 `共 N 字`；时间改 `formatLocalTimeLabel()`；`owner:` → `负责方：`。

**真机前后（逐字）**：

| | 文本 |
|---|---|
| 前（`recheck/skills.txt:31`） | `来源声明检查 17 条，虚假标注 17 条（与换算日期、数值逐字一致、中属于其他主体的数值、的分级判定标准、」列填写对应 [n] 编号：）；建议补录域名媒体映射` ← 到此为止，括号未闭合 |
| 后（`recheck/skills.txt:32-36`） | 同一段拆成 4 行完整显示：「…[n] 编号：）」/「建议补录域名媒体映射：主流财经」/「建议补录域名媒体映射：媒体的转载报」/「目标要求引用官方/可核实来源，报告未给出任何来源链接或来源清单条目」+ `共 137 字 · 记于 2026/9/17 15:57:32（本地时间）` |
| 新增标注（`recheck/skills.txt:55-56`） | `（原文即止于此：教训落库时被后端按 300 字截断且未加省略号，前端无法补全后半句）` / `共 300 字 · 记于 2026/10/21 14:14:36（本地时间）` |

**测试**（`frontend/tests/skillsLessons.test.mjs`，9 条）：`旧实现的 80 字硬截断确实是断句且无省略号（回归反例，固定证据）`、`新实现不截断：整段完整、括号闭合、被吞掉的尾部回来了`、`教训按分号拆成多条并列问题，逐条可读`、`长教训正文一字不丢`、`未绑定 Skill 的教训如实标注`、`确需限长时：加省略号并给出"已截断"说明`、`原文被后端落库截断时不冒充完整内容`、`正常结尾的教训不被误标为"原文被截断"`、`空教训不编内容`。

**存疑/未验证**：源数据本身有拼接瑕疵（如「（与换算日期、数值逐字一致、中属于其他主体的数值…）」是后端写入时就长这样），前端只保证**不截断、不伪装完整**，不替后端修数据。后端 300 字截断属后端问题（未改）。

---

## S2 设置页直出上游 400 原始 JSON（断口、重复、无结论）

**现象（审计）**：`需要处理：3 项` 下三条同款告警，每条都是被 `slice(0,120)` 切断的原始报文（引号/花括号不闭合），既无中文结论也无处理建议，同一错误重复 3 遍。

**根因**：两层截断叠加——前端旧代码 `slice(0, 120)`；后端 `health_registry.py:317` `str(health.get("last_error") or "降级")[:120]` 已经先截到 120 字（只读实测：live 探测返回的 `health_reason` 长度正好 120，末尾是 `,`）。所以**前端拿不到完整报文**，只能如实标注。

**改法**：
- 新增 `frontend/src/lib/depHealth.ts`：`healthReasonView()` 解析 `HTTP (\d{3})`、`"code"`、`"message"`、`"capability"` → 中文结论 + `adviceFor()` 建议（`MODEL_CAPABILITY_NOT_SUPPORTED` → 换成支持该能力的模型/端点，保存后点『测试连通性』；并说明修好前向量检索降级为关键词兜底）+ `rawNote`（不完整报文标注「服务端已截断，不补全、不猜测」）；`groupDegradedRequirements()` 把**同一原因**的多条降级项合并为一组并列出全部受影响字段；`requirementFieldLabel()` 统一 `标签（path）` 写法。
- `Settings.tsx`：渲染「需要处理：N 项配置（由 M 个原因引起）」→ 结论 / 建议 / 受影响字段清单 / `<details>查看服务端原始报文（原样，不做截断）`（`<pre class="whitespace-pre-wrap break-words">`，**不再由前端截断**）。

**真机前后（逐字）**：

| | 文本 |
|---|---|
| 前（`recheck/settings.txt:23-27`） | `需要处理：3 项` / `· API Key（embedding.api_key）：Embedding API HTTP 400: {"code":"MODEL_CAPABILITY_NOT_SUPPORTED","message":"当前模型不支持该能力：embeddings","data":{"capability":` ← 断口 JSON ×3 |
| 后（`recheck/settings.txt:24-28`） | `需要处理：3 项配置（由 1 个原因引起）` / `结论：上游返回 HTTP 400：当前模型不支持该能力：embeddings（错误码 MODEL_CAPABILITY_NOT_SUPPORTED）` / `建议：当前的模型/端点不提供 embeddings 能力。请在下方对应区块换成支持 embeddings 的模型（或改用支持该能力的端点），保存后点『测试连通性』复验。修好之前向量检索不可用：记忆命中率会退化为关键词兜底统计（见「记忆与进化」页顶部说明）。` / `受影响 3 项配置：API Key（embedding.api_key）、Base URL（embedding.base_url）、模型（embedding.model）` / `查看服务端原始报文（原样，不做截断）` |
| 同页 S6 附带（`:29`） | `余额：主端点 正常 · 备用 不可达 · 本月已用 $0.167 / $10（口径：主/备为 LLM 端点预检结果——能否连通、鉴权是否通过、额度是否够用，不是金额读数；「本月已用」取 Redis llm_usage_month 月度台账，与指标页「本月累计成本」同本账，不是历史总额）` ← 旧文本为 `余额：主端点 ok · 备用 unreachable` |

**测试**（`frontend/tests/depHealth.test.mjs`，7 条）：`上游 400 报文解析出中文结论与错误码（真机原文）`、`复核对不上：截断后仍能解析（带前缀的旧形态同样可用）`、`原文不完整要显式说明"服务端已截断"，且前端不自行截断、不加省略号`、`相同原因合并为一条，并列出全部受影响字段（真机：3 字段 1 原因）`、`不同原因不合并；字段写法统一`、`按错误类型给可操作建议（401/402/404/5xx/超时）`、`非 JSON 原因不编造：中文原样、空值说未取得`。

**存疑/未验证**：展开 `<details>` 后的原文样式只在源码层确认（文本快照抓的是收起态，故 `recheck/settings.txt` 里看不到 120 字断口原文）；截图见 `shots/settings-after.png`。120 字截断在后端（`health_registry.py:317`），未改。

---

## S3 记忆页「系统自述」数字口径与同页/指标页矛盾

**现象（审计）**：自述称「我已完成了 464 项任务」，而同页统计卡是 `464 对话条数 / 269 策略条数`，指标页总任务数为 570/571——464 是**对话条数**，被模型写成了任务数；自述尾部还写「可直接用于发布到社交媒体」。

**根因**：自述正文由 LLM 生成、措辞不稳定（两次加载分别返回「464 项任务」与「我已积累464条对话」），`/api/memory/summary` 不做数字核对；页面把「可直接用于发布」当成事实陈述。**不修改自述正文**（那是模型输出与来源，前端改写等于篡改 provenance）→ 改为在自述下方做核对与口径声明。

**改法**：新增 `frontend/src/lib/memorySummary.ts`：`auditMemorySummary(summary, counts)` 抽取自述里的数字主张并逐条与本页实取口径比对（`memoryAuthorityNote()` 只写本页权威值：对话/策略条数 + 任务数「不在本页统计范围（见『指标看板』）」），命中矛盾 → 告警；`publishNote` 在存在未核对数字时明说「不可直接用于对外发布」。`Memory.tsx` 统计卡补充口径副标题（「记忆库对话集合条数；不是任务数——任务总数见『指标看板』」）。

**真机前后（逐字）**：

| | 文本 |
|---|---|
| 前（`recheck/memory.txt:40` 与 `:265`） | `我是织光——一支运行在你本地的 AI 团队。我已完成了 464 项任务、沉淀了 269 条成功策略。…` + `基于真实记忆数据生成，可直接用于发布到社交媒体。` |
| 后（`recheck/memory.txt:41-48`） | 自述原文保留；下方新增：`自述数字核对：发现 1 处对不上（自述由 LLM 生成，数字不被本页背书）` / `· 自述把「464」称为「464 项任务」，但本页口径里 464 是「对话条数」（记忆库对话集合的条数），不是任务数；该句由模型生成、未经核对，请勿按此口径对外引用。` / `本页权威口径（取自 /api/memory 记忆库条数，与上方统计卡同源）：对话 464 条、策略 269 条；「任务」总数不在本页统计范围（见「指标看板」），本页不推断。` / `该自述由 LLM 生成，含未经核对的数字（见上方提示），不可直接用于对外发布；对外引用前请以本页统计卡与「指标看板」核对。` |

**测试**（`frontend/tests/memorySummary.test.mjs`，8 条）：`实机反例：把 464 条对话写成「464 项任务」→ 明确告警`、`告警里不出现本页没取到的数字（不编造指标页的 570）`、`权威口径行只写本页实取的两个数`、`对外发布口径改写：数字对不上时明说不可直接对外发布`、`措辞正确的缓存自述：核对一致并给出确认`、`数字真的对不上（999 条对话）→ 以本页口径为准`、`本页口径未取到（接口失败）时：权威口径写未取得，任务数仍标记为无法核对`、`空自述不产生任何主张`。

**存疑/未验证**：自述措辞每次生成都可能不同，「1 处对不上」的具体条目随模型输出变化；页面逻辑对任意措辞都成立（含「未取得口径时不下结论」分支），但**无法保证模型以后不再写出别的错误表述**——那需要后端在生成端约束（未改）。

---

## S4 进化锦标赛回放未本地化（`key=value` + 英文布尔）

**现象（审计）**：`演化完成: 胜者=baseline-v1-gen1-A, 稳定=True, 已部署=True`，同一列表里还混着 `已部署=False`，英文/中文/等号三种写法混排。

**根因**：`summary` 字段由后端进化模块直接拼出英文布尔与 `key=value`，前端 `Memory.tsx:408` 原样渲染（`{r.summary || '进化轮次'}`）；`rankings[].scores` 用 `JSON.stringify` 直出花括号。

**改法**：新增 `frontend/src/lib/evolutionText.ts`：`localizeEvolutionSummary()`（`胜者=X` → `胜者 X`、`稳定=True` → `稳定：是`、`已部署=False` → `已部署：否`，未识别的键值原样保留不硬翻）、`boolishValueText()`（`None` → 未记录）、`scoreMapText()`（评分映射 → `A 8.5、B 6`，超 6 项时写「等 N 项」）。`Memory.tsx` 回放区改为卡片式：任务完整换行、排名、各方案得分、评审理由，时间 `formatLocalTimeLabel()`。

**真机前后（逐字）**：

| | 文本 |
|---|---|
| 前（`recheck/memory.txt:404,409,414,419`） | `演化完成: 胜者=baseline-v1-gen1-A, 稳定=True, 已部署=True` / `…C, 稳定=True, 已部署=True` / `…A, 稳定=True, 已部署=False` |
| 后（`recheck/memory.txt:410,415,420,425,430`） | `演化完成：胜者 baseline-v1-gen1-A；稳定：是；已部署：是` / `…C；稳定：是；已部署：是` / `…C；稳定：是；已部署：否` / `…A；稳定：否；已部署：否`，时间 `2026/9/7 23:34:42（本地时间）` |

**测试**（`frontend/tests/evolutionText.test.mjs`，6 条）：`真机四条摘要全部本地化：无英文布尔、无 key=value`、`逐字对照：True→是、False→否、胜者名原样保留`、`不认识的键值不翻译也不丢（不编造中文名）`、`摘要缺失时返回空串，由页面显示占位而不是编造"进化完成"`、`评分映射不再直出 JSON 花括号与引号`、`评分项过多时如实说明总数（不静默丢弃）`。

**存疑/未验证**：`rankings[].reason`（评审理由）在实机当前 6 轮里为空，未取得真实长文本样例；渲染分支按「有则显示、无则隐藏」实现，未做实机对照。后端 `summary` 生成处仍产出英文（未改），前端做展示层本地化。

---

## S5 「平均耗时」与「P95」精确同值 —— 根因结论

**现象（审计）**：`平均耗时 21 分 14 秒` / `P95 21 分 14 秒`，两行完全相同。

**只读排查过程与结论（一句话）**：**P95 等于均值，是因为本 metrics 进程当时只有 1 个耗时样本**，不是前端取错字段，也不是后端把 P95 显式回落成均值——属**后端统计口径/样本范围**问题（同一响应里把两个不同分母的数据并排展示）。

证据链（只读）：
1. 实机 `/api/metrics`：`avg_latency_sec: 1274.4`、`p95_latency_sec: 1274.4`、**`latency_sample_size: 1`**，而同一响应 `tasks.terminal: 569`（来自 `task_history` 全表）。
2. `metrics_collector.py:582`：`p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]`。`len == 1` 时下标为 `int(0.95) - 1 = -1` → 取到的就是那唯一样本本身，故与均值同值。
3. 后端自述口径原文（页面已展示）：`仅统计本 metrics 进程启动后收到 elapsed_sec 的主终态任务，重启即清零…历史任务不能直接取均值：task_history.completed_at 会被后续落库重盖（task_state ON CONFLICT DO UPDATE），需要历史值请取分位数`。

**改法（遵守"只做前端如实标注，不改后端算法"）**：新增 `frontend/src/lib/metricsLatency.ts`：`latencyCardView(m, terminal)` → 主值仍为均值，副标签在样本 < 2（`MIN_P95_SAMPLES`）或 `p95 === avg` 时明确写 `P95 未独立计算（样本 1 个）`；`hint` 写清「与上方终态任务数不是同一范围」「样本过少不代表耗时分布集中」「样本为空 = 不是耗时 0 秒」并附后端口径原文。`MetricsPage.tsx` 接入该视图，另给 Critic/重规划/告警卡补上后端的进程内计数口径，`累计成本` → `本月累计成本`（口径为 Redis 月度台账，避免被读成历史总额）。`MetricsPage.tsx` 中 `更新于 …formatLocalTime(m.timestamp)` 一行是 Lead 的改动，**保持原样未动**。

**真机复验**：本次复验时栈已重启，耗时样本清零 → 页面显示 `平均耗时 未取得` / `P95 未取得 · 样本 0 个` 与完整口径说明（`recheck/metrics.txt:38-41`）。**退化分支（样本 1 个、P95 == 均值）本轮未能在实机复现**，因为它要求 metrics 进程里恰好结算过 1 个带 `elapsed_sec` 的主终态任务；该分支由测试用审计时留存的真实响应（1274.4/1274.4/1）覆盖。

**测试**（`frontend/tests/metricsLatency.test.mjs`，8 条）：`实机反例：avg == P95 == 1274.4 且只有 1 个样本 → 标注"P95 未独立计算"`、`口径写清分母：与上方终态任务数不是同一范围，且不给用户"同分布"的错觉`、`样本充足且 P95 独立于均值 → 正常展示样本数，不再告警`、`样本数为 2 但 P95 与均值仍完全同值 → 也判为未独立计算`、`没有耗时样本时说"未取得"，绝不显示 0 秒`、`P95 缺失但均值有值时如实说未取得`、`样本数未回传时不假装知道样本量`、`空指标不崩、不给数字`。

**建议（留给 Lead 决策，未实施）**：后端要么在 `len(latencies) < 2` 时不下发 `p95`（或下发 `null`），要么把 `latency_sample_size`/口径写进卡片主文案；若要展示历史耗时分布，应改用分位数而非 `task_history.completed_at` 均值（后者会被重盖，见口径原文）。

---

## S6 / G2 三种时间格式与英文枚举混排

**现象（审计 G1）**：历史页 `2026/10/21 21:25:41`（本地、无标注）、审计/设置页 UTC ISO 微秒、记忆页 `2026-07-23T07:56`（ISO、无时区）三种格式并存；另有 `admin/viewer`、`ok/unreachable`、`submitted` 等英文枚举直出。

**根因**：各页各自 `slice`/`toLocaleString`，无统一出口；枚举映射散落或缺项（共用 `resultLabel` 不认识 `submitted`）。

**改法**：新增 `frontend/src/lib/timeText.ts`（`formatLocalTimeLabel()` → 本地时间 + `（本地时间）`；`formatLocalTimeShort()` → `YYYY-MM-DD HH:MM（本地时间）`，用 `Date` 本地 getter 构造而非解析 `toLocaleString` 字符串，避免 locale 顺序差异；非法值 → `时间未知`）与 `frontend/src/lib/settingsText.ts`（`roleLabel()`、`balanceStatusText()`、`scheduledResultLabel()`）。四页全部走这两个出口。

**真机前后（逐字）**：

| 位置 | 前 | 后 |
|---|---|---|
| 设置页用户（`recheck/settings.txt:393-408`） | `创建于 -`、角色徽章 `admin` | `创建于 2026/8/21 13:38:35（本地时间）`；无记录者 `创建于 未知（服务端未记录创建时间）`；徽章/下拉 `管理员`/`只读用户` |
| 设置页定时任务（`:422`） | `上次：… · submitted` | `上次：2026/10/21 16:00:16（本地时间） · 已提交入队 · sched-f6e5440b8b` |
| 设置页余额（`:29`） | `主端点 ok · 备用 unreachable` | `主端点 正常 · 备用 不可达` |
| 记忆页条目（`recheck/memory.txt:56`） | `2026-07-23T07:56` | `2026-07-23 15:56（本地时间）` |
| 记忆页进化回放（`:411`） | `2026/9/7 23:34:42`（无时区说明） | `2026/9/7 23:34:42（本地时间）` |
| Skill 教训时间（`recheck/skills.txt:36`） | 无时间 | `记于 2026/9/17 15:57:32（本地时间）` |
| 指标页更新于 | （Lead 已改）`formatLocalTime(m.timestamp)` | 未动 |

**测试**（`frontend/tests/timeText.test.mjs`，8 条）：`UTC 微秒串不再直出：换算本地时间并标注"（本地时间）"`（非 UTC 机器上另断言 UTC 墙钟 05:38 不得被当本地时间直出）、`记忆页 ISO 无时区串：不再 slice(0,16)，改为本地时间`、`带偏移的串在紧凑格式里同样换算本地（不误读成 UTC）`、`取不到时间说"时间未知"，不再用 - / — 顶数`、`后端已给 HH:MM:SS 的本地时钟串：保留原样并注明本地时间，不谎报未知`、`角色枚举不再英文直出（真机：admin / viewer）`、`定时任务「上次」结果不再直出 submitted`、`端点状态不再直出 ok / unreachable`。

**存疑/未验证**：
- 时间格式统一覆盖到本次改动的四个页面；`History.tsx`/`Agents.tsx`/`Health.tsx` 由 Lead 并行处理，本目录不主张其状态。
- 记忆页「最近对话」的时间来自 `metadata.timestamp`，实机为**无时区** ISO；按本地时间解释（与 `Date` 对无时区 ISO 的规范行为一致）。若后端未来改为存 UTC 而不带偏移，该解释会偏一个时区——需后端补偏移量，前端无法自行判断（未改后端）。
- 设置页仍保留配置字段名原文（`llm.api_key`、`API Key`、`Base URL`、`Host`、`annual_report` 等），因为它们是用户需要在 `config.json` 里对照的键名，不属"英文枚举直出"。
- 未直连验证 `/api/users`、`/api/scheduled-jobs` 的原始响应（管理员受限路由，直连返回 404）；上表后值取自页面真实渲染文本。

---

## 本次新增/改动文件清单

新增纯函数模块（页面只渲染）：
`frontend/src/lib/skillLessons.ts`、`timeText.ts`、`depHealth.ts`、`settingsText.ts`、`memorySummary.ts`、`evolutionText.ts`、`metricsLatency.ts`
（`skillsLessons`/`metricsLatency` 内部按仓库既有约定用 `./format.ts` 显式扩展名引入，保证 `node --test` 可直接加载 `.ts`。）

新增测试：
`frontend/tests/skillsLessons.test.mjs`（9）、`depHealth.test.mjs`（7）、`memorySummary.test.mjs`（8）、`evolutionText.test.mjs`（6）、`metricsLatency.test.mjs`（8）、`timeText.test.mjs`（8）——共 46 条，逐条使用审计留存的实机反例原文作为测试数据。

改动的页面：`Skills.tsx`、`Settings.tsx`、`Memory.tsx`、`MetricsPage.tsx`（其中 `更新于 …` 一行保留 Lead 的原改动）。
**未做**：未改任何后端 `.py`，未改 `History.tsx`/`Agents.tsx`/`AgentTopology.tsx`/`Health.tsx`，未动 git，未停/未起任何服务，未新增依赖。
