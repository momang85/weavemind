# 前端真机视觉核查报告（2026-10-22）

> 角色：前端真机视觉核查（普通用户视角）。**只读核查**：未修改任何产品源码 / 配置 / 数据库，未启停任何服务。
> 目标：`http://127.0.0.1:8080`（平台栈已在运行，16/16 服务健康）。
> 范围：除「任务控制台」外的全部 10 个页面（任务控制台由 Lead 单独负责，本报告不覆盖）。
> 产物：截图与探针脚本在 `.tmp/shots-pages/`（临时目录，非产品资产）。

---

## 0. 核查方法，以及一次重要的并发修改

### 0.1 方法与两轮快照

| 项 | 做法 |
|---|---|
| 浏览器 | Chromium headless（Playwright），桌面 `1600×1000`、移动 `375×812` |
| 登录态 | 只读读取 `agents.db` 的 admin 会话 token 注入 Cookie + `localStorage.weavemind_user`；**token 未写入任何文件或回复** |
| 文本 | 每页 `innerText` 全量落盘，并在 +6s 再抓一次以识别"永远转圈/内容不收敛" |
| 截图 | 每页整屏 + 内部滚动容器 `main` 逐屏 0..4 屏 + 移动端 0..2 屏 |
| 网络/控制台 | console error/warning、`pageerror`、`requestfailed`、`status>=400` 全量记录 |
| 几何 | `overflow_probe.py`（越界元素清单）、`geom_probe.py`（行内元素 x/right/width，判定重叠与 0 宽）、`agents_filter_probe.py` / `scroll_reach.py`（筛选行可及性）、`memory_repro.py`（两次加载对照） |
| 数据 | `api_snapshot.py` 取 10 个只读 API 原始 JSON，与页面渲染值逐项对照 |

**两轮快照（关键）**：

| 轮次 | 时间（本地） | 说明 |
|---|---|---|
| **构建 A** | 21:0x–21:31 | 首轮全量核查，产物在 `.tmp/shots-pages/<page>/` |
| **构建 B** | 21:34:28 之后 | `frontend/dist` 于 **21:34:28 被重新构建**（`frontend/src/pages/Agents.tsx` 21:32:55、`frontend/src/pages/Health.tsx` 21:33:45 被改动）。我在 21:36–21:42 **对全部 10 个页面重新取文本+截图**，产物在 `.tmp/shots-pages/recheck/` |

**本报告的定级一律以构建 B（当前运行版本）为准**；构建 A 的发现若已被修掉，明确标注为「已修复」，避免 Lead 追已经不存在的问题。

### 0.2 复核对照表

| 首轮发现（构建 A） | 构建 B 状态 | 复核证据 |
|---|---|---|
| 健康页 4 张指标卡、事件时间线、健康检查全英文（`Uptime`/`Tasks Today`/`Survival Rate`/`LLM Tokens`/`Event Timeline`/`Health Checks`/`OK`） | ✅ **已修复**，全部中文化（运行时长/今日任务/存活率/模型 Token/事件时间线/实时/健康检查/正常） | `.tmp/shots-pages/recheck/health.txt:22-31,72-78` |
| 健康页事件体原样输出上游 JSON 且引号未闭合 | ✅ **已修复**，改为中文摘要并显式标注截断：`服务 worker_guardian · 时间 … · 跟踪 worker 数 11（事件体过长，后端已截断）` | `recheck/health.txt:65,70` |
| 健康页事件时间为 UTC ISO 微秒 | ✅ **已修复**，改为本地 `10-21 21:24:24` | `recheck/health.txt:32` |
| 智能体页 `Agent Topology` / `11 agents active` / `Search agents...` 英文 | ✅ **已修复**（智能体拓扑 / 11 个智能体在线 / 搜索智能体…） | `recheck/agents.png`、`recheck/agents.txt` |
| 记忆页命中率显示 `0.0%` 无任何解释 | ✅ **已修复**，同页与健康页顶部均给出降级说明与口径 | `recheck/memory.txt:24-27`、`recheck/health.txt:21` |
| 指标页「平均耗时 —」「P95 —」为 `—` | ✅ **已修复**（现有值），但 **引出新疑点**：平均值与 P95 完全同值，见 2.8 | `recheck/metrics.txt:37-39`、`api_snapshot.json` |
| 指标页/历史页时间口径 | ✅ 指标页已改为 `更新于 2026/10/21 21:36:03（本地时间）` | `recheck/metrics.txt:22` |
| 其余全部发现（见各页） | ❌ **仍存在**，已逐项在本轮复核 | 见 §2 |

### 0.3 复现命令

```bash
cd <repo>
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'   # 否则 /agents 会被 MSYS 改写成 Windows 路径
python .tmp/shots-pages/page_probe.py /agents      # 首轮全量：text/errors/reqs/shot/mobile
python .tmp/shots-pages/recheck.py                 # 复核轮：10 页文本+截图 → .tmp/shots-pages/recheck/
python .tmp/shots-pages/geom_probe.py /history     # 移动端行内几何（P0 证据）
python .tmp/shots-pages/agents_filter_probe.py     # 筛选行可及性（P1 证据）
```

---

## 1. 结论先行（构建 B 定级）

**整体可用**：10 个页面在两种视口下均 **零 console 报错、零 `pageerror`、零请求失败、零 `>=400`**（两轮结果一致）；无 `NaN` / `undefined` / `null` / `[object Object]` / `Infinity` / `\uXXXX` / 豆腐块；无永久骨架屏（`text.txt` 与 `text-late.txt` 除时间戳自然刷新外逐行一致）。

仍有 **1 项移动端阻断缺陷 + 18 项明显缺陷（P1） + 22 项轻微缺陷（P2）**，集中在四处：**英文/枚举残留**、**原始 JSON 与长文本硬截断直出**、**"无数据"被渲染成 0 或占位符**、**移动端布局重叠与信息丢失**。并发修改已修掉 6 项（含 3 项 P1），但**布局类问题一项未动**。

### 1.1 分级汇总（构建 B）

| 页面 | 路由 | P0 | P1 | P2 | 是否阻断推广 |
|---|---|---|---|---|---|
| 智能体 | `/agents` | 0 | 3 | 4 | 桌面端不阻断；移动端筛选基本不可用 |
| 历史 | `/history` | **1** | 1 | 2 | **阻断移动端**（桌面正常） |
| 健康 | `/health` | 0 | 1 | 3 | 不阻断；但漏报关键子系统故障 |
| 记忆与进化 | `/memory` | 0 | 1 | 3 | 不阻断（自述口径问题影响对外发布） |
| 评测 | `/evals` | 0 | 0 | 1 | 不阻断（本页最干净） |
| Skill | `/skills` | 0 | 1 | 2 | 不阻断 |
| 本地研究试验 | `/research-study` | 0 | 3 | 3 | 不阻断（桌面可读） |
| 指标 | `/metrics` | 0 | 1 | 3 | 不阻断 |
| 审计 | `/audit` | 0 | 3 | 3 | 不阻断；审计可用性明显受限 |
| 设置 | `/settings` | 0 | 2 | 4 | 不阻断；错误提示不可读 |
| 全局（跨页） | — | 0 | 2 | 2 | — |
| **合计** | | **1** | **18** | **30** | — |

**最严重三项**：
1. **P0｜历史页 375×812 下列表项标题被挤成 0 宽（完全不可见），「继续对话」按钮整体压盖时间戳**（两个页签都如此）。
2. **P1｜设置页把上游原始 JSON 硬截断 120 字后直接显示（引号不闭合、同一错误重复三遍、无修复指引）**；同类问题在智能体页/Skill 页也存在。
3. **P1｜智能体页拓扑图标签互相重叠粘连**，渲染出 `datalaanalymodel_train`、`webfetchwo`、`rengengene`、`search age` 等不可读字符串（桌面与移动端都出现）。

---

## 2. 逐页结果

### 2.1 智能体 `/agents`

- 首轮：`.tmp/shots-pages/agents/shot/0..4.png`、`mobile/0..2.png`、`text.txt`、`agents-detail/exp-click0.png`
- 复核（构建 B）：`.tmp/shots-pages/recheck/agents.png`、`agents.txt`、`recheck/agents-exp-click0.{png,txt}`

**问题 1（P1，仍存在）拓扑图标签互相重叠、粘连成不可读字符串**
复核文本逐字可见：`search age`、`reactagent`、`webfetchwo`、`fileiowork`、`codeexecwo`、`rengengene`（`report_generator` 被压叠）、`content su`、`datalaanalymodel_train`（`data_analyzer` 与 `model_trainer` 两个标签叠在同一个位置）、`packaging`（被截断）。
构图上每个标签都在刻意重叠的半径内，短标签尚可辨、长标签互相叠印。构建 A 与构建 B 均出现，未随本次中文化修复而改善；英文标签叠在中文页面上反差更明显。
证据：`recheck/agents.png`（拓扑区块）、`recheck/agents.txt` 第 40-46 行。
复现：打开 `/agents`，看「智能体拓扑」区块内圈标签。
阻断判定：不阻断，但页面主视觉是乱码，首次访问者会认为产品有 bug。

**问题 2（P1，仍存在）能力筛选行溢出容器，移动端 11 个筛选项只有 1 个可见，且需横向滚动整个内容区**
几何量测（`agents_filter_probe.py`，构建 A 与 B 结果一致）：

- 容器 `div.flex.gap-3`（`frontend/src/pages/Agents.tsx:319`）**没有 `flex-wrap`、也没有 `overflow-x-auto`**：`overflow-x: visible`，`scrollWidth 1505 > clientWidth 1152`。
- 桌面 `1600×1000`：`report_generator`、`web_fetch`、`web_search` 三枚按钮落在视口外——复核截图里可见 `report_generator` 在右边缘被切掉一半，后面两枚完全看不见。
- 移动 `375×812`：仅 `code_execution` 在视口内，**11 枚里 10 枚不可见**。
- 缓解事实（如实记录）：祖先 `main` 是 `overflow: auto` 且 `scrollWidth 1609 > clientWidth 1360`，用户**可以**横向拖动内容区把按钮拖出来（`main.scrollLeft=249` 后 `web_search` 勉强贴到右边缘）。所以这是"可达但极难用"，而非绝对不可达；但页面**没有任何横向可滚动的视觉提示**。
复现：`python .tmp/shots-pages/agents_filter_probe.py`（输出 11 个按钮的 `inView` 与坐标），或 375 宽下打开 `/agents` 并向右拖动内容。
阻断判定：桌面端不阻断（可达）；移动端视为筛选功能基本不可用。

**问题 3（P1，仍存在）展开详情把"无数据"渲染成 `-s` / `0%` / `0`**
复核文本（`recheck/agents-exp-click0.txt` 第 54-58 行，逐字）：`0%` / `成功率` / `0` / `任务数` / `-s` / `平均耗时`。
`-s` 是 `{agent.avgTime ?? '-'}s` 的拼接结果（`Agents.tsx:132`）：缺数据时输出字面量 `-` 再补单位 `s`，渲染成 `-s`。`successRate ?? 0` 与任务数同理，把"未知"显示成"0"，会让用户误判某个智能体从未成功过。
复现：打开 `/agents` 点任一智能体卡的「展开详情」。

**问题 4（P2，仍存在）智能体命名格式不统一**：同一份 `/api/status` 里既有 `search_agent`/`packaging_worker`/`content_summarizer`，也有 `dataloaderworker`/`reportgeneratorworker`/`modeltrainerworker`（无下划线），卡片小字直接展示这些内部 id（`recheck/agents.png`）。

**问题 5（P2，仍存在）负载数字无标签无单位**：卡片上的 `0/5`、`0/10`、`0/3`、`1/5` 没有说明分母是什么（并发上限？队列上限？），也没有单位。

**问题 6（P2，仍存在）** 断连提示写「后端断连 — 每2秒轮询」（`Agents.tsx:361`），而 `AppLayout` 实际每 **3000ms** 轮询（`frontend/src/components/AppLayout.tsx:57`）——文案与行为不符。

**问题 7（P2，源码可判、真机未复现）** `timeAgo`（`Agents.tsx:29-34`）小于 1 小时输出中文（`刚刚`/`N 秒前`），**大于等于 1 小时输出英文 `Nm ago` / `Nh ago`**。本次两轮截图时 11 个智能体心跳都很新，未观察到该分支（见 §5）。

**已修复**：`Agent Topology` / `11 agents active` / `Search agents...` 等英文已中文化（构建 B）。

### 2.2 历史 `/history` ← 唯一 P0

- 首轮：`.tmp/shots-pages/history/shot/0..4.png`、`mobile/0..2.png`、`history-tasks/tab2-click0.png`
- 复核：`python .tmp/shots-pages/geom_probe.py /history`（构建 B 复测，几何数值与首轮完全一致）

**问题 1（P0·移动端阻断，仍存在）375×812 下列表项标题被挤成 0 宽完全不可见，「继续对话」按钮整体压盖时间戳**
几何量测（viewport 375×812，行宽 343；`geom-_history-base.json` / `geom-_history-任务.json`）：

对话页签：
```
x=  93 right=  93 w=   0 SPAN   '研究伊利股份（600887）2023 与 2'   ← 标题宽度 0，永久不可见
x= 105 right= 152 w=  47 SPAN   '1 条消息'
x= 164 right= 218 w=  54 SPAN   '有缺口'
x= 230 right= 348 w= 118 SPAN   '2026/10/21 21:25:41'              ← 时间戳
x= 248 right= 338 w=  90 BUTTON '继续对话'                          ← 与时间戳重叠 90px
```
任务页签：
```
x=  89 right=  89 w=   0 SPAN   '研究伊利股份（600887）2023 与 202'  ← 标题同样是 0 宽
x= 101 right= 157 w=  56 SPAN   'default'
x= 235 right= 338 w= 103 SPAN   '2026/10/21 21:25:41'
```
视觉结果（`history/mobile/0.png`）：每行只剩「💬 1 条消息 + 状态徽章 + `2026/1▶继续对话…`」，**用户完全看不到这条记录是什么任务**，时间戳与按钮文字叠印。
复现：375×812 打开 `/history`（对话页签）与 `/history` → 「任务」页签；或运行上面的 `geom_probe.py` 命令。
阻断判定：**阻断移动端使用**（列表无法辨识）；桌面端（≥768px）布局正常，不阻断。

**问题 2（P1，仍存在）任务列表硬上限 50 条，无总数、无分页、无"仅显示最近 N 条"提示**
后端 `/tasks` = `_list_tasks(50)`、`/api/conversations` = `_list_conversations(50)`（`web_ui.py:5101-5104`），前端整表渲染，页面上没有任何条数说明。同期指标页显示「总任务数 570 / 终态 569」——用户会认为历史里只有这 50 条，或以为其余任务丢失。
复现：`/history` →「任务」页签滚到底（50 行），再打开 `/metrics` 看总任务数。

**问题 3（P2，仍存在）** 任务标签直接暴露内部值 `default`。

**问题 4（P2）** 时间为本地时间 `2026/10/21 21:25:41`，与审计/设置页的 UTC ISO 微秒不一致（见 G1）。

**正面**：两个页签空态都有中文说明（「暂无对话记录」/「暂无任务记录」），不是白屏也不是英文。

### 2.3 健康 `/health`

- 首轮：`.tmp/shots-pages/health/shot/0..4.png`、`mobile/0..2.png`、`text.txt`
- 复核（构建 B）：`.tmp/shots-pages/recheck/health.png`、`health.txt`

**问题 1（P1，仍存在）关键子系统 `code_execution` 沙箱不可用，本页完全不体现，仍全绿**
只读 API `/api/health` 原始值：
```json
"code_sandbox": {"mode":"docker","isolation_required":true,"isolation_ready":false,
 "isolation_reason":"docker 不可用（CLI 缺失或守护进程未响应）",
 "execution_available":false,
 "isolation_note":"要求容器隔离但当前不可用：docker 不可用（CLI 缺失或守护进程未响应）；代码执行会被拒绝（工作台与其余功能不受影响）"}
```
而复核后的健康检查区块仍只有三项：`编排器 / Worker 池 / 守护进程`，全部「正常」（`Health.tsx:172-184`）。页面**唯一**名为「沙箱」的区块是「进化沙箱」（策略进化竞技场，与 code_execution 无关）——术语撞车：用户看到"沙箱正常/空闲"会以为代码执行环境没问题。
复现：`python .tmp/shots-pages/api_snapshot.py` 比对 `/api/health.code_sandbox` 与 `/health` 页健康检查区块。
阻断判定：不阻断，但健康页在关键能力不可用时给出误导性全绿结论。

**问题 2（P2，仍存在）「存活率 100%」无口径说明**：不与成功率字面撞车了（已中文化），但同一天指标页显示「成功率 53.6% / 失败率 27.2%」，两者并列仍易被理解为同一件事；卡片无 tooltip、无副标题。

**问题 3（P2，仍存在）**「进化沙箱 上次运行 —」：全角破折号单独作值，视觉上像一条下划线。

**问题 4（P2，仍存在）**按钮下方静态文案「需要管理员确认」，而当前登录用户就是管理员（右上角 `momang · 管理员`）——文案与身份不符。

**问题 5（P2，降低严重度）**事件体里的时间戳仍是原始 UTC（`时间 2026-10-21T12:54:02.138020+00:00`），但已被中文摘要包裹并标注「（事件体过长，后端已截断）」，属可接受的原文引用。

**已修复（构建 B）**：4 张指标卡与区块标题全部中文化；事件体改为中文摘要并标注截断；事件时间改为本地 `10-21 21:24:24`；新增 Embedding 降级顶部预警（`Embedding 接口连续失败 8 次：…记忆命中率会显示为 0%…经验复用失效`），这条预警同时也解释了记忆页的 0% 命中率来源，写得清楚。

### 2.4 记忆与进化 `/memory`

- 首轮：`.tmp/shots-pages/memory/shot/0..4.png`、`memory-repro-r0.png`、`memory-repro-r1.png`、`memory_repro.json`
- 复核（构建 B）：`.tmp/shots-pages/recheck/memory.png`、`memory.txt`

**问题 1（P1，仍存在）「系统自述」把 464 条对话写成「464 项任务」，与同页卡片和指标页口径互相矛盾**
复核原文（`recheck/memory.txt:40`，逐字）：
```
我是织光——一支运行在你本地的 AI 团队。我已完成了 464 项任务、沉淀了 269 条成功策略。我可以帮你调研、分析数据、写报告，还会在任务后自我复盘与进化。
```
同一页面：卡片把 464 标注为「对话条数」、269 标注为「策略条数」；`/metrics` 显示「总任务数 570 / 终态 569 / 成功 305」。即「464 项任务」既不等于对话数（464 是对话），也不等于总任务数 570。另一次加载返回的缓存自述措辞不同（`我已积累464条对话、269条成功策略`），说明**同一数字的措辞由模型生成、不稳定**。
风险放大：该区块旁明确写着自述「可直接用于发布到社交媒体」——把 464 条对话当 464 项任务对外发布属对外口径错误。
复现：`python .tmp/shots-pages/memory_repro.py`（连续加载两次，落盘 `memory_repro.json` + 两张截图），或打开 `/memory` 对比顶部「系统自述」与下方「记忆库」数字。
阻断判定：页面不阻断；若按页面自述用于对外发布，建议按阻断处理。

**问题 2（P2，仍存在）进化锦标赛回放未本地化**：`演化完成: 胜者=baseline-v1-gen1-A, 稳定=True, 已部署=True`（`recheck/memory.txt:404,409,414,419`），`key=value` + 英文布尔值；同屏 4 条里还有 `已部署=False` 与 `True` 混排。

**问题 3（P2，仍存在）记忆库混入测试残留数据**：可见 `ping`、`Say hello`、`EVOLUTION_TRIGGER`、`Summary: find 3 AI trends in 2024`，其中 `ping` 还沉淀出一条 10 步"成功策略"（本次只读观察，未清理）。

**问题 4（P2，仍存在）时间格式不统一**：记忆页条目显示 `2026-07-23T07:56`（ISO 无时区），与历史页 `2026/10/21 21:25:41` 本地格式、审计页 UTC 微秒格式三者并存（见 G1）。

**已修复（构建 B）**：命中率卡片现在带完整口径说明——`向量检索不可用（Embedding 欠费/故障）：当前按关键词兜底检索，命中率分母是兜底检索次数`、`关键词命中率（命中 0）`，并说明`另有 1 条新经验排队待补录，接口恢复后会自动写入`。

**核查更正（避免误导 Lead）**：首轮目视截图时我曾把 464 归到「策略条数」，本轮用 API + 复现脚本逐项对照后确认——**统计卡与记忆库 chip 本身完全一致**：卡片「269 策略条数 / 464 对话条数」= chip「269 策略 / 464 对话」= API `{"conversations":464,"strategies":269}`。**不存在"卡片数字互换"的缺陷**，首轮那句目视判断作废，以本节为准。

### 2.5 评测 `/evals`

- 首轮：`.tmp/shots-pages/evals/shot/0..4.png`、`mobile/0..2.png`、`text.txt`；复核：`recheck/evals.png`、`evals.txt`

**本页是最干净的一页：无 P0/P1。**
- 可见内容：`Judge 校准（黄金测试集）` → `MAE（误差）0.281` / `通过失败一致率 75%` / `样本数 4` / `达标阈值 0.7`；空态区 `最近任务评测分数（评测驱动反思）` + `暂无评测记录（任务匹配到评测案例后自动产生）`——**空态带原因说明，写法正确**。
- **问题 1（P2）** 标题中 `Judge`、`MAE` 保留英文（行业通用术语，可接受，但与全站中文化风格不齐）。
- 说明：校准「样本数 4」与「暂无评测记录」不矛盾（黄金集校准与任务评测记录是两套数据），未记为缺陷。

### 2.6 Skill `/skills`

- 首轮：`.tmp/shots-pages/skills/shot/0..4.png`、`mobile/0..2.png`、`text.txt`；复核：`recheck/skills.png`、`skills.txt`

**问题 1（P1，仍存在）「自动沉淀教训」正文残缺、句子断裂，用户读不懂**
复核原文（逐字，`recheck/skills.txt:31`）：
```
来源声明检查 17 条，虚假标注 17 条（与换算日期、数值逐字一致、中属于其他主体的数值、的分级判定标准、」列填写对应 [n] 编号：）；建议补录域名媒体映射
```
括号内是多个互不相干的片段被硬拼在一起。根因：`frontend/src/pages/Skills.tsx:57` 用 `String(l.issue || '').slice(0, 80)` **硬截断 80 字且不加省略号**。
**问题 2（P2，仍存在）同一条教训文本在多张卡下逐字重复**：`recheck/skills.txt:31` 与 `:52` 完全相同（分别属于不同 Skill 卡片），疑似串味或重复沉淀。
**问题 3（P2，仍存在）** Skill 名为英文 slug（`data-pipeline` 等）与中文 owner 混排；页面无搜索/筛选（当前 4 张卡尚可）。

### 2.7 本地研究试验 `/research-study`

- 首轮：`.tmp/shots-pages/research/shot/0..4.png`、`mobile/0..2.png`、`text.txt`；复核：`recheck/research-study.png`、`research-study.txt`

**问题 1（P1，仍存在）Markdown 粗体标记 `**` 原样显示**
复核文本仍可见：`预览后需逐个勾选确认的**文件内主体**`（2 处 `**`），首轮截图中还有 `标签**全部为模拟**`、`输出是**内部研究草稿**`。渲染层未解析 Markdown，用户直接看到星号。

**问题 2（P1，仍存在）⑦「已保存的内部研究产物」的「主体」列 20 行全部为「—」**
复核文本（`recheck/research-study.txt:66-80`）：表头 `选定 / 研究ID / 主体 / 特征截止 / 模拟 / 状态 / 人工 / 附件`，其后每一行「主体」都是 `—`；同时研究 ID 重复（`c99068abe588e002…` 连出现两次），多行的特征截止/模拟/状态完全相同。这张表最关键的定位信息整体缺失。

**问题 3（P1，仍存在）375×812 下区块标题与右侧说明挤成一行，说明被压成约 6 字宽逐字折行**
`research/mobile/1.png`：⑤ Q1 读数、⑥ 同一入口改参 两个区块的标题（左）与灰字说明（右）共用一行，说明列被压到约 50px，渲染成「数值句从 / ModelRun 读 / 数装配； / 本节只读回 / 显，不在渲 / 染期改参数 / 或重算」——几乎不可读。

**问题 4（P2，仍存在）英文枚举未本地化**：状态列 `research_draft`、人工列 `unreviewed`（同表「模拟/真实」列却是中文，反差明显）。

**问题 5（P2，仍存在）表头挤压**：① 文件表「字节」与「sha256」两列无间距，渲染成 `字节sha256`；375 宽下表内文字互相挤压。

**问题 6（P2，仍存在）** 375 宽下 h1「本地研究试\n验」折行，且与顶栏已有的「本地研究试验」标题重复。

**正面**：多处空态写明原因（例：「这份产物没有 Q1 网格 / 只有带 `q1.joint_grid` 块的研究才有 3×3 读数；旧产物保持原样，不回填。」）；表格容器使用 `overflow-x-auto`，横向溢出属设计内可滚动。

### 2.8 指标 `/metrics`

- 首轮：`.tmp/shots-pages/metrics/shot/0..4.png`、`mobile/0..2.png`、`text.txt`；复核：`recheck/metrics.png`、`metrics.txt`、`api_snapshot.json`

**问题 1（P1·新发现）「平均耗时」与「P95」完全同值，568+ 终态任务下不可能**
复核页面（`recheck/metrics.txt:37-39`）：`平均耗时` → `21 分 14 秒`，副标签 `P95 21 分 14 秒`。
同一时刻 API：`"avg_latency_sec": 1274.4, "p95_latency_sec": 1274.4, "terminal": 569`（1274.4 秒 = 21 分 14 秒）。
569 个终态任务的平均耗时与 P95 **精确到 0.1 秒完全相同**，统计上不可能，几乎可以断定两个字段取自同一来源（或 P95 直接回落到均值）。用户在指标页同时看到这两个数会误判"耗时分布非常集中"。
复现：`python .tmp/shots-pages/api_snapshot.py`（比对 `avg_latency_sec` / `p95_latency_sec`）+ 打开 `/metrics`。
说明：首轮的「平均耗时 —（无解释）」已修复为有值，本条是修复后暴露出的新问题。

**问题 2（P2）「平均耗时」的旧形态已消失**，但若耗时再次缺失，卡片会退回裸「—」（大号字体下像一条下划线，375 宽尤其明显）——建议保留"无终态任务"式的解释分支。

**问题 3（P2，仍存在）`?? 0` 兜底把"字段缺失"显示成"确实是 0"**：`Critic 评审 0 通过 / 0 未通过`、`重规划 0 / 0 成功`（`frontend/src/pages/MetricsPage.tsx:114-115`）。当前 API 真的是 `{pass:2,fail:0}`、`{total:0,success:0}`，渲染值正确；但字段一旦缺失用户无法察觉。

**问题 4（P2，仍存在）成本口径未注明**：页面 `累计成本` 与设置页 `本月已用 $… / $10` 两个数都叫"成本/已用"、量级不同，页面未说明「累计」与「本月」的区别（复核时 API `cost_usd_total: 0.7303`）。

**正面**：`成功率 53.6%` 与 `失败率 27.2%` 内部自洽；`进行中 1` 与历史页「运行中 1」一致；`更新于 2026/10/21 21:36:03（本地时间）` 显式标注了时区口径（构建 B 改进）。

### 2.9 审计 `/audit`

- 首轮：`.tmp/shots-pages/audit/shot/0..4.png`、`mobile/0..2.png`、`text.txt`；复核：`recheck/audit.png`、`audit.txt`

**问题 1（P1，仍存在）「结果」列原样显示英文枚举 `allow` / `deny`；新增的「详情」列显示英文 `public`**
复核文本逐字：`2026-10-21T12:22:10.177861+00:00	system	fetch_document	https://np-cnotice-stock.eastmoney.com/api/content/ann	allow	public`。
`frontend/src/lib/statusMeta.ts:107-113` 的 `resultLabel` 只映射 `ok/success/fail/failed/error/denied`，未覆盖审计使用的 `allow`/`deny`，英文原值直出；同一产品其它页面用「成功/失败/拒绝」。

**问题 2（P1，仍存在）标题承诺的记录类型与实际内容不符，且无筛选/搜索/分页**
- 可见文案：`登录/任务提交/删除/分享等操作留痕（200 条）`。
- 实际：200 条**全部** `用户 = system`、动作 `fetch_document`（复核文本通篇如此）；首屏看不到任何登录、任务提交、删除、分享记录。
- 页面没有筛选、没有搜索、没有分页、没有"共 N 条"；`/api/audit` 后端硬上限 200 条。
复现：打开 `/audit` 全选文本，可见 200 行 `system / fetch_document`。

**问题 3（P1，仍存在）「目标」列固定 `max-w-[160px]` + `truncate`，无 `title`/tooltip**
`frontend/src/pages/AuditPage.tsx:61`。复核可见值 `https://np-cnotice-stock.eastmoney.com/api/content/ann`（被截在路径中段），用户无法得知审计对象到底是哪个地址，也无法复制完整值。

**问题 4（P1，仍存在）375×812 下只剩「时间」「用户」两列可见**
`audit/mobile/0.png`：首屏只有时间戳与 `system`，「动作 / 目标 / 结果 / 详情」全在屏外（表格宽 945px，视口 375px，`overflow-x-auto` 无视觉提示）；标题「审计日志」也折成两行。

**问题 5（P2，仍存在）** 详情列出现前导竖线与原始 errno 英文：`| DNS 解析失败：[Errno 11001] getaddrinfo failed`。

**问题 6（P2，仍存在）测试/样例数据进入真实审计库**：可见目标 `https://news.example/2025-03-12/a`（`example` 保留域名，DNS 必然失败）被记为 `deny`。

**问题 7（P2，仍存在）** 时间为 `2026-10-21T12:22:04.533333+00:00`（UTC + 6 位微秒），未本地化（见 G1）。

### 2.10 设置 `/settings`

- 首轮：`.tmp/shots-pages/settings/shot/0..4.png`、`mobile/0..2.png`、`text.txt`；复核：`recheck/settings.png`、`settings.txt`

**问题 1（P1，仍存在）「需要处理：3 项」把上游原始 JSON 硬截断 120 字后直接显示，同一根因重复三遍，无修复指引**
复核原文（逐字，红色告警区，`recheck/settings.txt`）：
```
· API Key（embedding.api_key）：Embedding API HTTP 400: {"code":"MODEL_CAPABILITY_NOT_SUPPORTED","message":"当前模型不支持该能力：embeddings","data":{"capability":
· Base URL（embedding.base_url）：Embedding API HTTP 400: {"code":"MODEL_CAPABILITY_NOT_SUPPORTED","message":"当前模型不支持该能力：embeddings","data":{"capability":
· 模型（embedding.model）：Embedding API HTTP 400: {"code":"MODEL_CAPABILITY_NOT_SUPPORTED","message":"当前模型不支持该能力：embeddings","data":{"capability":
```
问题：① JSON 在 `"capability":` 处被切断，**引号与大括号都不闭合**，也没有 `…`；② 同一条 400 因三个字段共享同一端点而重复三遍，用户以为是三个不同错误；③ 没有一句中文结论（如"当前 embedding 模型不支持 embeddings 能力，请更换模型或端点"）或"查看详情"入口。根因：`frontend/src/pages/Settings.tsx:396` `entry?.health_reason?.slice(0, 120)`。
复现：打开 `/settings` 首屏红框区。**这是本次核查中最典型的"原始 JSON 直出"缺陷。**

**问题 2（P1，仍存在）「记忆治理参数」4 个字段可编辑、可保存，但徽章明说「模板字段·代码未读取」**
`策略去重阈值 0.95`、`策略过期天数 90`、`对话上限 2000`、`对话去重窗口(小时) 24` 均有输入框与当前值，同时带琥珀色徽章 `模板字段·代码未读取`（复核文本共 4 处），影响说明为「记忆治理；改 config.json 不生效，需设对应环境变量」。对普通用户而言这就是"改了、点了保存、什么都没发生"。
复现：`/settings` 滚到「记忆治理参数」区块；计数命令 `grep -c '模板字段·代码未读取' .tmp/shots-pages/recheck/settings.txt` → 4。

**问题 3（P2，仍存在）英文残留**：页面 h1 `Settings`（顶栏已是「设置」）、按钮 `Reset`、`余额：主端点 ok · 备用 unreachable`（`Settings.tsx:404` 直接渲染后端英文 reason）、角色 `admin`/`viewer`、默认体裁 `annual_report`。

**问题 4（P2，仍存在）用户区数据展示粗糙**：`创建于 -`（半角连字符占位，`revieweradmin` 账号无创建时间）；`创建于 2026-08-21T05:38:35.970506+00:00`（UTC 微秒未本地化）；`创建于 2026-10-21T05:46:48.438140+00:00`。证据 `recheck/settings.txt:393,405`。

**问题 5（P2，仍存在）** 375 宽下长 JSON token 溢出卡片被裁切（`settings/mobile/0.png`）；`daily-report` 标签 chip 在 375 宽下超出容器 13px（`overflow.json`）。

**正面**：多处配置项写清影响面并诚实标注未接线，例：`CoinGecko Key … 预留未接线 … 未配置 … 影响：预留，当前不影响任何功能`；`数据源密钥（收费）… 该字段为未来付费额度预留。` 这类写法值得保留。

---

## 3. 全局问题（跨页）

### G1（P1，部分修复）时间口径在同一应用内不统一

| 页面 | 复核后渲染样例 | 口径 | 状态 |
|---|---|---|---|
| 历史 | `2026/10/21 21:25:41` | 本地 | ✅ 正常 |
| 指标 | `更新于 2026/10/21 21:36:03（本地时间）` | 本地（显式标注） | ✅ 已修 |
| 健康 | `10-21 21:24:24` | 本地 | ✅ 已修 |
| 记忆与进化 | `2026-07-23T07:56` | ISO 无时区 | ⚠️ 不统一 |
| 审计 | `2026-10-21T12:22:10.177861+00:00` | UTC + 6 位微秒 | ❌ |
| 设置·用户 | `创建于 2026-08-21T05:38:35.970506+00:00` | UTC + 6 位微秒 | ❌ |

核查时刻本机墙钟本地 `Wed Oct 21 21:05:55 2026` = UTC `13:05:55`，即审计/设置页显示的时间比用户本地时间早 8 小时且精度到微秒。同一产品内三种时间形态并存，用户无法判断"任务到底什么时候跑的"。

### G2（P1，审计/设置/研究/记忆仍存在）英文与内部枚举残留

健康页与智能体页已中文化；**仍残留**：

| 页面 | 残留原文（逐字） | 位置 |
|---|---|---|
| 设置 | h1 `Settings`、按钮 `Reset`、`主端点 ok · 备用 unreachable`、角色 `admin`/`viewer`、`annual_report` | `Settings.tsx:367,371,404` |
| 审计 | 结果列 `allow`/`deny`、详情列 `public` | `lib/statusMeta.ts:107-113` 未覆盖 |
| 研究试验 | 状态 `research_draft`、人工 `unreviewed` | `recheck/research-study.txt` |
| 记忆与进化 | `胜者=baseline-v1-gen1-A, 稳定=True, 已部署=False` | `recheck/memory.txt:404-419` |
| 智能体 | `dataloaderworker`/`reportgeneratorworker`/`modeltrainerworker` 与 `search_agent`/`packaging_worker` 混排；断连文案「每2秒轮询」（实为 3s） | `Agents.tsx:361` |

### G3（P2）空值占位符不统一
`—`（研究试验「主体」列 20 处、健康「上次运行 —」）、`-`（设置 `创建于 -`）、`-s`（智能体「平均耗时」）。

### G4（正面）控制台与网络干净
10 个页面 × 两轮，`console.error==0`、`pageerror==0`、`requestfailed==0`、`status>=400==0`。详见 §4。

### G5（P2·观察项）轮询密度
`/api/status` 在所有页面每 3s 轮询（`AppLayout.tsx:57`）；`/api/events` 5s（`Health.tsx:117`）；`/api/evals` 10s（`Evals.tsx:26`）；`/api/skills` 15s（`Skills.tsx:22`）；`/api/metrics` 30s（`MetricsPage.tsx:62`，每次都对 `task_history` 全表重算）。未见重复轮询或无限重试，但多标签页会线性放大全表重算成本。

---

## 4. 控制台报错与异常请求（需求第 5 项）

**结论：两轮 × 10 页 × 两种视口，全部干净。**

| 页面 | console.error/warning | pageerror | requestfailed | status ≥ 400 |
|---|---|---|---|---|
| `/agents` `/history` `/health` `/memory` `/evals` `/skills` `/research-study` `/metrics` `/audit` `/settings` | 0 | 0 | 0 | 0 |

- 证据：首轮各页 `.tmp/shots-pages/<page>/errors.txt`（空文件）、`reqs.tsv`；复核轮各页 `.tmp/shots-pages/recheck/*.err.txt`（空文件，10/10 页 `errs=0`）。
- 未发现：调用不存在的 API、重复轮询同一端点、失败后无限重试。
- 说明：`tab_probe.py` 打印的 `issues: 1` 是脚本自己记录的"已点击"信息行，不是页面报错（对应 `errors.txt` 为空）。

---

## 5. 跨页数据一致性交叉核对（需求第 6 项）

| 指标 | 指标页 | 历史页 | 健康页 | 记忆页 | 其它 | 判定 |
|---|---|---|---|---|---|---|
| 任务总数 | 总任务数 **570**（终态 569 / 进行中 1） | 任务页签最多 **50** 行，无总数 | 只显示 `今日任务 5` | 自述说「**464 项任务**」 | — | **矛盾**：570（全表）vs 50（截断展示）vs 464（把对话数说成任务数）→ §2.2 / §2.4 |
| 成功率 / 失败率 | 53.6% / 27.2%（305、155 / 569） | 状态徽章混合，无法汇总 | `存活率 100%` | — | — | **易误读**：100% 是进程存活率，与 53.6% 任务成功率口径不同且无解释 → §2.3 |
| 进行中 | **1** | 运行中 **1** | — | — | — | 一致 ✅ |
| 平均耗时 vs P95 | `21 分 14 秒` vs `P95 21 分 14 秒`（API 均为 1274.4） | — | — | — | — | **不可信**：569 样本下均值 = P95 精确同值 → §2.8 |
| 策略 / 对话数 | — | — | — | 卡片 269 策略 / 464 对话；chip 269/464；API `{conversations:464,strategies:269}` | 自述 269 成功策略 | 卡片/chip/API 一致 ✅；仅自述把 464 说成「项任务」 |
| 成本 | `累计成本`（复核时 API `cost_usd_total 0.7303`） | — | — | — | 设置页 `本月已用 $… / $10` | **口径未注明**：累计 vs 本月 → §2.8 |
| 审计条目数 | — | — | — | — | 审计页 `（200 条）`，后端上限 200 | 无"共 N 条"，无法判断是否被截断 |
| Embedding 状态 | — | — | 顶部预警 `连续失败 8 次…已降级` | `向量检索不可用…命中率分母是兜底检索次数` | 设置页 `需要处理：3 项`（同一根因） | 三处**一致** ✅（本次改进的亮点） |

---

## 6. 未验证 / 不确定

1. **所有会产生副作用的按钮均未点击**（严格遵守只读约束）。因此"点得动但没反应 / 文案与行为不符"仅确认了 **设置页「模板字段·代码未读取」4 个字段**（静态可判）。未验证：设置页「保存」「Reset」「测试」「新增用户/改密/删除」、健康页「触发一轮进化」、智能体页「终止」「单智能体直发」、记忆页「重新生成」「触发新一轮进化」「审批」、研究试验页「运行并保存」「文件预览」「选定」。需专门一轮受控点击核查。
2. **智能体页 `timeAgo` 的英文分支（`Nm ago`/`Nh ago`）源码可判、真机未复现**（截图时 11 个智能体心跳均 <1h）。
3. **平均耗时 = P95 的根因未定位**：本次只确认"接口返回两个相同值、页面如实渲染"，未追到后端是把 P95 回落成均值，还是样本本身只有一条有效耗时。
4. **`code_execution` 沙箱不可用的影响面未验证**：只确认 `/api/health` 自报 `execution_available: false` 且健康页不体现；未验证实际提交依赖代码执行的任务时的用户可见表现（属任务控制台范畴，由 Lead 覆盖）。
5. **审计"登录/任务提交/删除/分享"记录是否真的缺失未验证**：只观察到 200 条返回全是 `system / fetch_document`，未核对 `/api/audit` 的排序与上限实现是否把其它类型挤掉。
6. **深色主题对比度未量化**：未用对比度工具逐项测量。目视可疑处：`text-slate-600` 的时间戳/空态文案在 `bg-slate-900` 上偏暗（`health/mobile/0.png` 时间戳、设置页底部说明）。
7. **键盘可达性 / 焦点顺序 / 屏幕阅读器标签**未检查。
8. **"无永久骨架屏"结论基于 9s + 15s 两次采样**，未做 >10 分钟长时浸泡，也未覆盖慢网络节流场景。
9. **移动端只覆盖 375×812 竖屏**：未测横屏、768px 平板、iOS Safari 实机（本次为 Chromium headless 模拟视口，非真机）。
10. **未覆盖「任务控制台」**（`/`）——按分工由 Lead 负责，本报告不构成对首页的结论。
11. **并发修改仍在进行**：`Agents.tsx`(21:32)、`Health.tsx`(21:33) 与 `frontend/dist`(21:34) 在我核查期间被改动，我的复核快照时间为 **21:36–21:42**。若此后又有改动，本报告对应条目需再次复核。

---

## 7. 证据索引

```
.tmp/shots-pages/
├── 探针脚本（只读）：page_probe.py  tab_probe.py  overflow_probe.py  api_snapshot.py
│                     memory_repro.py  geom_probe.py  agents_filter_probe.py
│                     scroll_reach.py  recheck.py
├── api_snapshot.json                 # 10 个只读 API 原始 JSON（一致性交叉核对依据）
├── overflow.json                     # 375 宽下所有越界元素清单
├── memory_repro.json                 # 记忆页两次加载的 API+渲染对照
├── geom-_history-base.json / geom-_history-任务.json   # 「标题 w=0、按钮压时间戳」的几何证据
├── agents-filter-1600.png / agents-filter-375.png / agents-filter-scrolled.png
├── memory-repro-r0.png / memory-repro-r1.png
├── batch1.log / batch2.log / overflow.log
├── 【构建 A 首轮】agents/ history/ history-tasks/ health/ memory/ evals/ skills/
│                  research/ metrics/ audit/ settings/ agents-detail/
│                  每目录含 text.txt, text-late.txt, reqs.tsv, errors.txt, shot/0..4.png,
│                  shot/top-late.png, mobile/0..2.png
└── 【构建 B 复核】recheck/  10 页各 {<name>.txt, <name>.err.txt, <name>.png}
                           + agents-exp-click0.{txt,png}, agents-exp-s0..3.png
```

只读引用的源码位置（未修改）：`frontend/src/pages/{Agents,History,Health,Memory,MetricsPage,AuditPage,Evals,Skills,Settings}.tsx`、`frontend/src/components/AppLayout.tsx`、`frontend/src/lib/statusMeta.ts`、`frontend/src/lib/useVisibleInterval.ts`、`web_ui.py:5101-5104`。
