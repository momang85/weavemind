# 前端布局与后端两处缺陷修复（2026-10-22）

> 角色：Lead（本机实机复验 + 修复）。
> 目标：`http://127.0.0.1:8080`（平台栈 16/16 健康）。
> 输入：`docs/evidence/20261022-pages-visual/README.md`（只读走查，P0×1 / P1×18 / P2×30）
> 与 `docs/evidence/20261022-newtask-verify/README.md`（新任务 `ui-a0d82a26c4` 七项判定）。
> 本文只记录 **Lead 亲自改并亲自复验** 的部分；错误展示与数字口径类（Skills / Settings /
> Memory / Metrics）由并行子智能体负责，见 `docs/evidence/20261022-pages-fix/README.md`。

---

## 0. 结论

| # | 缺陷 | 定级 | 状态 |
|---|---|---|---|
| L1 | 历史页 375 宽下列表项标题被挤成 `w=0`、操作按钮压盖时间戳 | **P0**（阻断移动端） | ✅ 已修，几何复测 |
| L2 | 智能体拓扑标签叠印成 `datalaanalymodel_train` 等不可读串 | P1 | ✅ 已修，截图复验 |
| L3 | 能力筛选行不可换行，桌面 3 枚 / 移动 10 枚按钮在视口外 | P1 | ✅ 已修，截图复验 |
| L4 | 展开详情把"无数据"渲染成 `0%` / `0` / `-s` | P1 | ✅ 已修 |
| L5 | 健康页漏报 `code_execution` 沙箱不可用，仍全绿 | P1 | ✅ 已修 |
| L6 | 健康页事件体原样直出且被截断 / 时间为裸 UTC / 全英文标签 | P1 | ✅ 已修（前一批） |
| L7 | 指标页把 UTC 当本地时间显示（早 8 小时） | P1 | ✅ 已修 |
| L8 | 历史页 50 条硬上限无任何说明 | P1 | ✅ 已修 |
| L9 | 审计页结果列英文枚举 `allow`/`deny` 直出 | P1 | ✅ 已修 |
| L10 | 审计页标题承诺的记录类型与实际内容不符、无条数上限说明 | P1 | ✅ 已修 |
| L11 | 审计页目标列截断且无 tooltip，无法得知审计对象 | P1 | ✅ 已修 |
| L12 | 审计页窄屏 6 列只剩 2 列可见且无横向滚动提示 | P1 | ✅ 已修 |
| L13 | 本地研究试验页 Markdown `**` 原样显示 | P1 | ✅ 已修 |
| L14 | 本地研究试验页窄屏区块说明被压成约 6 字宽逐字折行 | P1 | ✅ 已修 |
| L15 | 本地研究试验页英文枚举 `research_draft` / `unreviewed` | P2 | ✅ 已修 |
| L16 | 设置页标题/按钮英文（`Settings` / `Reset`） | P2 | ✅ 已修 |
| L17 | 智能体页 `Nm ago` / `Nh ago` 英文、负载 `0/5` 无标签、断连文案与实现不符 | P2 | ✅ 已修 |
| L18 | 健康页事件**体内**仍是裸 ISO UTC，与同行行头相差 8 小时 | P1 | ✅ 已修（展示层归一） |
| L19 | 智能体页能力筛选按钮显示英文枚举 `code_execution` 等 | P2 | ✅ 已修 |
| L20 | 拓扑中心「中枢」被辐条横穿，放大后几乎不可读 | P1 | ✅ 已修（绘制顺序 + 起点） |
| **B1** | **阶段事件时间戳用 `time.gmtime()` 输出裸 UTC，比本地早 8 小时** | **P1（后端）** | ✅ 已修 + 回归测试 |
| **B2** | **`_strong_subject_tokens` 字母序截断 `[:12]` 丢掉公司名，门控失效** | **P1（后端）** | ✅ 已修 + 回归测试 |
| B3 | 流式请求被拒 `http_404` 后回退非流式，每次多约 60s | P1（实机观察） | ⚠️ 未修（外部服务行为，见 §4） |
| B4 | `llm:stream_fallback` 路径丢失根任务归属 → **该次调用不记账** | **P1（实机观察）** | ⚠️ 未修（见 §4，需先定口径） |

**测试**：`python -m unittest test_p0` **438 OK**；`test_task24_dead_source_annotation` 19 OK；
`test_task_context` 43 OK；`test_writer_consolidation` 60 OK（1 skip）；新增
`test_ws_time_and_topic_gate.py` 7 OK。前端 `node --test tests/*.test.mjs` **178 pass / 0 fail**，
`npx tsc --noEmit` 对本文涉及文件无报错，`npm run build` 成功。

---

## 1. L1（唯一 P0）：历史页移动端不可用

**现象**（走查几何量测，375×812）：列表项标题 `SPAN` 宽度 **0**，永久不可见；
「继续对话」按钮区间 248–338 压盖时间戳 230–348，重叠 90px。

**根因**：行容器 `flex items-center gap-3`，标题是唯一的 `flex-1`，其余（消息数、状态徽章、
时间戳、按钮）全是 `shrink-0`。窄屏时固定项吃掉全部宽度 → 标题塌缩到 0，行内容溢出 → 叠印。

**修法**（`frontend/src/pages/History.tsx`，两个页签都改）：
- 行容器与展开按钮都改为 `flex flex-wrap items-center gap-x-3 gap-y-2`（允许折行）；
- 标题 `flex-1 min-w-[9rem] truncate`（保底宽度，窄屏时后面的元信息自动折到第二行）；
- 装饰性图标补 `shrink-0`。

**复验**（`python .tmp/shot_mobile.py http://127.0.0.1:8080/history out.png 375 812`）：

| 元素 | 改前 | 改后 |
|---|---|---|
| 标题 `SPAN` | `x=93 right=93 w=0` | `x=89 right=240 **w=151**` |
| 时间戳 | `230–348`（被压盖） | `x=33 right=151`（第二行） |
| 继续对话 | `248–338` | `252–342`（与时间戳**不再相交**） |

任务页签同样：标题 `w=0 → 185`（`x=89 right=274`）。截图：`.tmp/shots3/history-mobile.png`、
`.tmp/shots3/history-tasks-mobile.png`；两页 `console error 0`。

---

## 2. L2 + L3：智能体拓扑标签叠印、筛选行溢出

**L2 现象**：拓扑内圈渲染出 `datalaanalymodel_train`、`webfetchwo`、`fileiowork`、`codeexecwo`、
`rengengene`、`search age`、`content su`、`packaging` 等不可读字符串。

**根因三条**：① 标签取 `agent_id.replace('_',' ').slice(0, 10)`（10 个英文字符 ≈ 70px），
而周长上相邻节点仅约 51px 间距；② 一律水平居中画在节点正下方，圆环左右两侧沿切线方向互相压叠；
③ 完全没有碰撞检测。

**修法**：新增可测纯函数模块 `frontend/src/lib/topologyLayout.ts`：
- `shortAgentLabel()` 改用**能力中文短名**（与智能体卡片同口径，4 字 ≈ 48px）；
- 标签**沿半径向外**摆放（相邻标签自然发散），按方向决定 `textAlign` / `textBaseline`；
- 放置前做碰撞检测，撞了就沿半径推远（≤4 步），仍放不下则**隐藏并计数**
  （页面底部如实写「N 个标签画布放不下已省略，鼠标移到节点上可看名称」）——宁可少画，不留乱码；
- 画布高度 260 → 320（`TOPO_HEIGHT`），并修正命中检测的 CSS→画布缩放比。

**复验**（`.tmp/shots3/agents_topology.png`）：11 个标签全部可读、互不重叠——
`网络检索 / 数据加载 / 打包交付 / 报告生成 / 内容摘要 / 模型训练 / 数据分析 / 代码执行 /
文件读写 / 网页抓取 / ReAct 智能体`。

**测试**：11 个标签两两包围盒不相交（宽画布 + 375 宽）；极窄画布（170）转隐藏且计数正确；
断言旧乱码串不可能再现。

**L3 修法**：筛选行 `flex gap-3` → `flex flex-wrap gap-2 sm:gap-3`，搜索框 `flex-1 min-w-[12rem]`。
复验截图里 11 枚能力按钮**换行后全部可见**（改前桌面 3 枚、移动 10 枚在视口外）。

---

## 3. L5：健康页漏报代码执行沙箱不可用

**现象**：只读 `/api/health` 明确返回
`code_sandbox: {mode:"docker", isolation_ready:false, execution_available:false, isolation_reason:"docker 不可用（CLI 缺失或守护进程未响应）", isolation_note:"…代码执行会被拒绝…"}`，
而健康页「健康检查」只有 编排器 / Worker 池 / 守护进程 三项且全绿。页面上唯一叫「沙箱」的区块
是「**进化**沙箱」（策略进化竞技场）——术语撞车，用户会以为代码执行环境正常。

**修法**（`frontend/src/pages/Health.tsx`）：新增只读 `GET /api/health` 拉取（公开路径），
在健康检查区块增加第 4 行「代码执行沙箱」：可用→正常；不可用→**不可用** + 中文原因；
拿不到→「未取得」（不编造）。同时修掉同页两处：
- 「进化沙箱 · 上次运行」原为 `String(timestamp).slice(0,19)`（UTC 原文，且空值显示全角破折号
  像一条下划线）→ 改为 `formatEventTime()` 本地时间，空值写「从未运行」；
- 按钮下静态文案写「需要管理员确认」，而当前登录用户就是管理员 → 按 `getAuthUser().role`
  区分「仅管理员可触发；会真实消耗模型额度」/「需要管理员权限」。

---

## 4. B3 / B4：实机观察到的两条运行时问题（未修）

运行中的任务 `ui-0cfe9652ed` 期间 `logs/orchestrator.log` 反复出现：

```
21:48:38 [WARNING] llm_client: 流式请求被拒（http_404(HTTP 404)），回退非流式
21:48:38 [WARNING] llm_client: 模型调用没有根任务归属（stage=llm usage=llm:stream_fallback）：本次不记账；后台线程必须显式继承 root（累计 22 次）
21:49:38 [WARNING] （同上，累计 23 次）
```

- **B3**：`/v1` 的流式端点被上游 404 拒绝，客户端回退非流式。行为本身是合理的降级，
  但每次调用因此多花约 60 秒（21:48:38 → 21:49:38），本任务 21:25 提交、21:48 已写完验收，
  之后仍在跑。**未修理由**：这是外部服务能力，需要先确认是模型/网关侧配置还是我们的请求形态，
  属于"先定口径再改"，不在本轮视觉/逻辑修复范围内。
- **B4**：`llm:stream_fallback` 路径**丢失根任务归属，该次调用不记账**，累计 23 次。
  也就是说**用户看到的 token/成本是低估的**（真实调用次数多于账本）。
  这是用户可感知的账目问题，但修它要动 `llm_client` 的记账口径（谁承担这次调用的成本、
  是否回填），风险与影响面都超出本轮，**留给用户裁决**，本文只如实记录。

---

## 5. B1 / B2：后端两处缺陷（已修）

### 5.1 L18：事件**体内**的时间戳（本轮第三处同源缺陷）

修完 B1 后复验健康页，事件时间线里仍有：

```
行头   10-21 22:01:49  守护进程
事件体 服务 worker_guardian · 时间 2026-10-21T14:01:49.686895+00:00 · 跟踪 worker 数 11
```

**同一行**两个时间差 8 小时。行头走 `formatEventTime`（本地），事件体是写入方自己格式化的原文。
全仓排查后确认两类写入方：约 20 处 `datetime.now(timezone.utc).isoformat()`（带 `+00:00`，明确 UTC），
另有 `time.strftime(...)`（裸串，本仓约定即本地墙钟，例如 `annual_report_pdf.py:199`、
`delivery_pipeline.py:772`）。

**修法**：**在展示层归一**（`frontend/src/lib/healthEvents.ts` 的 `localizeTimestamps`），
理由是不逐处改写入方——写入方多且会新增，逐个改既漏又危险；且**不动存量数据**，
下游仍按原样解析 ISO 串。规则：带 `Z`/偏移的用 `Date` 换算成本地墙钟；
裸串只改格式不换算（本仓裸串即本地，不做时区猜测）；**不合法的时间原样保留**。

**复验**：同一行现在读作 `时间 10-21 22:01:49`，与行头一致。

> 单测先写出来时抓到过自己一个缺陷：`2026-13-45T99:99:99Z` 因"先按偏移解析失败、再落到裸串旁路"
> 被粉饰成 `13-45 99:99:99`。改成单遍扫描 + 显式范围校验后，坏数据原样暴露。

### 5.2 L19 / L20：智能体页两处刺点

- **L19**：能力筛选按钮直接渲染 `code_execution` / `data_analyzer` 等英文枚举，整页中文里格外扎眼
  → 改用与卡片标题同源的 `CAPABILITY_ZH`，原始 id 放 `title` 供排查。
- **L20**：中心节点画在连线**之前**，11 条辐条从圆心出发横穿「中枢」二字。
  1600 宽截图上已难以辨认，放大 5 倍后确认是"被连线盖住"而非竖排。
  → 辐条改从中心圆**边缘**起画，且中心节点挪到连线之后绘制（与标签同层）。

### B1 阶段事件时间戳裸 UTC（`ws_helpers.py`）

`_phase_event` 用 `time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())`：输出**裸 UTC 无偏移**，
而控制台其余日志走 `OrchestratorV2._now_iso`（本地墙钟 + 偏移）。于是同一屏的反思
「开始/进行中/完成」三条比其它条目早 8 小时，且不带偏移、前端无法识别成 UTC。

**修法**：新增 `ws_helpers._now_local_iso()`，与 `_now_iso` 同口径
（`datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")`），
`_phase_event` 改用它。实测输出 `2026-10-21T21:49:10+08:00`。

### B2 强主题门控被码点排序截断打断（`clean_data.py`）

`_strong_subject_tokens` 原为 `tuple(sorted(out))[:12]`——**字母序**排序后截 12 个。
实机反例：目标「研究贵州茅台 2023 与 2024 两个年度的营业收入、归母净利润…」产出 42 个 4 字窗，
字母序里「贵州茅台」排第 **39**，被 `[:12]` 截掉 → 公司名级门控形同失效，
无关来源（贵州省政府网等）只能靠更弱的通用片段碰巧挡住。

**修法**：改为**首次出现顺序**（目标通常以「研究<公司名>…」开头，实体名必在前几个窗），
去重后上限放宽到 24。实测：`贵州茅台 / 伊利股份 / 海天味业 / 洋河股份` 均进入集合。

**回归测试** `test_ws_time_and_topic_gate.py`（7 用例）：时间戳必须带偏移且是本地墙钟、
`_phase_event` 载荷可被解析；四个真实目标的公司名必须存活；顺序必须是首次出现序而非字母序；
上限仍生效且无重复；短目标仍返回空以便调用方退回锚点门控。

---

## 6. 测试与复现

```bash
# 后端（根目录平铺测试，按文件跑）
python -m unittest test_p0                              # 438 OK
python -m unittest test_task24_dead_source_annotation    # 19 OK
python -m unittest test_task_context                     # 43 OK
python -m unittest test_writer_consolidation             # 60 OK (skipped=1)
python -m unittest test_ws_time_and_topic_gate           # 7 OK（本轮新增）

# 前端
cd frontend && npx tsc --noEmit && node --test tests/*.test.mjs && npm run build
# 178 pass / 0 fail

# 真机几何与截图
export MSYS_NO_PATHCONV=1
python .tmp/shot_mobile.py http://127.0.0.1:8080/history .tmp/shots3/history-mobile.png 375 812
python .tmp/shot_ui.py http://127.0.0.1:8080/agents .tmp/shots3/agents_topology.png 8
python .tmp/dump_text.py http://127.0.0.1:8080/health .tmp/shots3/health.txt 8
```

**环境坑（记下来避免误判）**：本机 MSYS bash 里若 `HOME` 是 MSYS 风格（`/c/Users/...`），
Python 3.14 下 `pathlib.Path.home()` 会抛 `RuntimeError: Could not determine home directory`，
导致 `import chromadb` 失败、`python -m unittest` 报错。这与代码无关；
`export HOME="C:\\Users\\ding0"` 后全部正常（`test_p0` 438 OK 即在此状态跑出）。
另外 `python -m unittest` 那两次失败与 `python -c "import <同一模块>"` 成功并存，
也由该环境差异解释。

---

## 7. 未验证 / 未做（如实列出）

1. **本轮后端改动尚未生效**：`ws_helpers.py`、`clean_data.py` 的修改需要重启服务才加载，
   而重启会打断运行中的任务 `ui-0cfe9652ed`，故**待任务终态后**再重启并复验。
   同理，上一批 `web_ui.py` 的正文截断透出（`delivery.truncated` / `truncation`）也尚未生效。
2. **B3 / B4 未修**，只记录（§4）。
2b. **指标页「各能力成功率」仍显示英文能力枚举**（`code_execution` / `data_summary`…）——
   与 L19 同类，但该文件在并行子智能体的写入范围内，为避免并发覆盖**留待其交付后**再改。
   → **第二轮已修，见 §9.1 的 L23。**
3. **以下走查项本轮未动**（属子智能体范围或本轮未及）：
   - 记忆页「系统自述」把 464 条对话写成「464 项任务」的对外口径矛盾 → 子智能体负责；
   - 指标页「平均耗时」与「P95」精确同值 → 子智能体负责给结论；
   - 审计页无筛选/搜索/分页（后端硬上限 200 已如实标注，但功能仍缺）；
   - 任务标签直接暴露内部值 `default`（P2，未修）；
   - Skill 名为英文 slug、Skill 卡下同一条教训逐字重复（P2）。
4. **未做**：全量测试、任何 git 写操作、服务启停（除了待任务终态后的必要重启）、
   任何密钥/模型/权限/代理阈值改动。
5. 本机系统日期为 **2026-10-21**，目录名按派单写 `20261022`。


---

## 8. 运行环境变更（本轮）

交付过程中平台栈曾**整体停止**：`launcher.py up` 拉起的服务是当时那个 shell 的子进程，
而该 shell 异常退出（会话记录里两次「persistent bash shell exited without reporting a status」），
进程组被一并带走。已停止运行中的任务 `ui-0cfe9652ed` 因此被编排器判为 `FAILED / 崩溃`。

经用户裁决：**改用 8099 端口重启**（8080 被用户正在运行的游戏 `GRW.exe` 占用，
`Get-NetTCPConnection` 确认），并允许该任务重跑。实际处置：

- 以 `WEB_PORT=8099` + `WEAVEMIND_WORKSPACE_ROOT=<原临时根>` 后台起栈 → **16/16 服务存活**、
  工作台 HTTP 200、研究能力就绪；工作区根沿用原值，历史任务页面照常可见。
- 残留任务不会自动重跑（已结算为失败），按用户授权**重新提交同一目标**：`ui-b23642e5de`。
- 用户日常入口仍是 `http://127.0.0.1:8080`；要让 8080 恢复，需先释放该端口再 `stop` + `up`。

**教训**：长跑任务期间不要用会随会话结束而死的 shell 拉起服务；`launcher.py up` 应在
独立后台进程里执行（本轮重启即如此）。


---

## 9. 第二轮：任务终态后的复验与新增修复

任务 `ui-b23642e5de`（伊利股份 600887，用户授权重跑的那次）于 22:22:50 结算
`SUCCESS_WITH_ISSUES / 完成`（准入跳过：终态不是成功）。随后在 8099 上做了全页视觉复验
与一次后端重启（加载 §5 的 `ws_helpers` / `clean_data` / `web_ui` 改动）。

### 9.1 本轮新增修复

| 编号 | 问题 | 级别 | 状态 |
|---|---|---|---|
| L21 | 全站中文界面却声明 `<html lang="en">`：影响屏幕阅读器发音、浏览器翻译提示与 CJK 字体回退选型；标题 `WeaveMind` 无中文名 | P2 | ✅ 已修（`lang="zh-CN"` + 标题「织光 WeaveMind — 上市公司研究工作台」） |
| L22 | 任务页「修改与重验 → 未采用来源」把正文 Markdown 直接渲染成纯文本：`**1157.80 亿元**` 原样显示，且 `slice(0,60)` 把 `**8.24%**` 切成 `**8.24%*`（留下半个标记） | P1 | ✅ 已修（新增 `lib/quoteText.ts`：先剥行内标记再按可见字符截断） |
| B5 | `metrics_collector` P95 用 `sorted(latencies)[int(n*0.95)-1]`：Nearest-rank 索引差 1，n=2 时把 p95 算成中位数 | P1 | ✅ 已修（`percentile_nearest_rank`，10 用例） |
| B6 | **`adapters/search_quality.py` 用裸 `\d{4}` 取年份，把证券代码 `600887` 切成假年份 `6008`**（详见 §10.2） | P0 | ✅ 已修（`_YEAR_RE = (?<!\d)(?:19\|20)\d{2}(?!\d)`，两处调用点） |
| L23 | 指标页「各能力成功率」卡片仍显示英文能力枚举（`code_execution` / `web_search`…）——§7 第 2b 条因写域冲突搁置的那处 | P2 | ✅ 已修（复用 `CAPABILITY_ZH`，保留 `title="能力标识：…"`；页面实测显示「代码执行」「网络检索」） |

### 9.2 L22 的实机证据与做法

修复前（`?task=ui-b23642e5de` 页面文本，第 362 行）：

```
· 未采用来源：2024 年营业收入 **1157.80 亿元**，较 2023 年 **1261.79 亿元**下降 **8.24%*——8.24%：…
```

三个问题叠在一行：`**` 原样显示、末尾 `**8.24%*` 少了半个标记（`slice(0,60)` 切在标记中间）、
截断点切进数值。修复后同一行：

```
· 未采用来源：2024 年营业收入 1157.80 亿元，较 2023 年 1261.79 亿元下降 8.24%（金额差…——8.24%：…
```

`quoteForPanel` 的三条约束（都有用例）：剥掉行内标记后**不留孤立 `*`**；截断补 `…` 且优先收在
标点处（不把数值切半）；短句不加省略号。全页 `**` 残留数：**2 → 0**。

### 9.3 本轮测试

| 命令 | 结果 |
|---|---|
| `node --test tests/*.test.mjs` | **186 通过 / 0 失败**（新增 `quoteText.test.mjs` 8 例） |
| `npx tsc --noEmit` | 无输出（通过） |
| `npm run build` | 成功 |
| `python -m unittest test_search_year_boundary` | **5 通过** |
| `python -m unittest test_search_quality_unified` | **80 通过**（无回归） |
| `python -m unittest test_metrics_p95_nearest_rank` | **10 通过** |
| `python -m unittest test_metrics_scope` / `test_task13_metrics_split` / `test_task17_routing_metrics` | 通过 |

视觉复验（8099，含 375 宽）：agents / health / metrics / memory / settings / skills / audit /
research-study / history 全部 `issues: 0`、控制台 0 报错。


---

## 10. 交付闭环实机验证（P0-5 结案）与检索质量缺陷

### 10.1 交付三处哈希不一致：**不是"显示错"，是"包本身过期"，且系统能识别**

`ui-b23642e5de` 上按顺序核到的事实：

| 读法 | `reports/report.md` | 字符 | 机器验收 |
|---|---|---|---|
| 自动打包的 `deliverables_20261021_222233.zip` | `9fa13f429521916f` | 8870 | 非采纳稿 |
| 页面/接口登记的**收尾交付正文** `delivered_sha256` | `4e0628a0b71cc5e6` | 4124 | — |
| 本版**采纳研究正文** `research_body_sha256` | `31aee6ada6e2891e` | 2632 | `pass`，0 缺口 |

- `delivery_pipeline.package_statuses('ui-b23642e5de')` → 该包 `status = historical`，
  理由逐字为「包内 reports/report.md（9fa13f429521，8870 字符）不等于收尾交付正文
  （4e0628a0b71c，4124 字符），也不等于本版研究正文（31aee6ada6e2，2632 字符）：
  屏幕版/下载版与包内版不是同一份，该包不得当当前稿下载（重新导出即生成同版包）」。
- 读取期复算 `projection_vs_package(...)` → **`verdict = mismatch`**；
  `current_package()` → `None`（下载路径的单一真源，不会把过期包当当前稿给出去）。
- 旧包的 `PACKAGE_MANIFEST.json` 里 `delivery_projection_verdict = "unknown"`、
  `delivery_projection = {}`（**写时**没登记投影）；**读取期**重算补上了这个判定，
  所以"清单字段为空"不等于"无人把关"。

**闭环验证**：点产品自带的「导出当前包」（`POST /api/task/<id>/package`）后生成
`deliverables_20261021_223018_3c00b8.zip`（5 955 118 B），此时

- `status = current`、`has_current = True`、`current_package()` 返回它；
- 包内 `reports/report.md` = **4124 字符 / `4e0628a0b71cc5e6`**，与页面登记的
  `delivered_sha256` / `delivered_chars` **逐字节相同**；
- 清单 `report_version_id=d58bc7b0…`（采纳版）、`research_body_sha256=31aee6ad…`、
  `delivered_md_sha256=4e0628a0…` 三者自洽；读取期复算 → **`verdict = match`**。

**结论**：交付身份是三重可核的（研究正文 / 收尾交付正文 / 包内主文），过期包会被
标 `historical` 且不能当当前稿下载，重新导出即得同版包。**遗留的工作流毛刺**（未修）：
自动打包发生在装配之前，所以任务刚结束时那次自动包**几乎必然是 `historical`**，
用户要多点一次「导出当前包」才拿到可下载的同版包（`note` 已如实提示「没有任何包与当前
采纳稿同版（需重新导出）」）。

### 10.2 检索质量：P0 级缺陷 —— 证券代码被当成年份（已修）

同一个任务的检索侧证据（`project/search_results.json` 3 条全部与标的无关）：

```
title: 6008 - 深沟球轴承 | SKF
title: win10-eventlog-6008 …
title: 汉典「…」ci-6bb791f75
```

`logs/worker-search.log` 的 `queries_tried` 显示实际发出的查询：

1. `任务目标：研究伊利股份（600887）2023 与 2024 两个年度的营业收入、…`（整段目标原文）
2. 同上（重复）
3. `6008 2023 2024 2025 研究伊利股份 两个年度 营业收入 归母净利润 …`

第 3 条就是垃圾结果的来源：`adapters/search_quality.py:374` 与 `:508` 用裸 `\d{4}` 取年份，
把 `600887` 切出前 4 位 `6008`。后果有两层：**关键词查询里混进一个不存在的"年份"**，
以及 `_query_tokens` 的年份集合把 `6008` 当查询年份、给含 6008 的垃圾结果加分。

修复：`_YEAR_RE = (?<!\d)(?:19|20)\d{2}(?!\d)` + `_year_tokens()`，两处调用点统一。
实机复算（重启后同一目标）：

```
变体1: 2023 2024 2025 研究伊利股份 两个年度 营业收入 归母净利润 报表口径 截至   ← 6008 已消失
变体2: "研究伊利股份 两个年度 营业收入 归母净利润 报表口径 截至"
变体3: 研究伊利股份（600887）2023 与 2024 两个年度的营业收入、归母净利润，…
```

### 10.3 未修（如实列出）

1. **正文自相矛盾**：采纳版正文第 8 行写「本次外部检索返回的 3 条结果均与标的公司及其
   年度财务数据无关，已按来源纪律整体剔除，**未纳入正文、表格与来源清单**」，而同一份
   正文的「## 数据来源」小节**恰好列出这三条 URL**。剔除决定做对了，装配时没把它落到
   「数据来源」小节。属报告装配逻辑，未改。
2. **检索质量不可信时的口径是可信的**：编排器打了「能力-执行者错配：web_search 步骤 1 由
   content_summarizer 执行（重规划代做）；检索质量不可信，任务按有缺口交付」，任务以
   `SUCCESS_WITH_ISSUES` 结算，页面标「未验收草稿…不得视为已通过」。**平台没有把垃圾
   检索包装成可信交付**，这点是达标项而不是缺陷项。
3. `core_search_terms()`（`adapters/search_quality.py:357`）**没有调用点**（死代码）；
   整段目标原文仍会作为「整句变体」发出（`_rich_variants`），是否收敛到关键词变体属设计取舍，
   未擅自改。
4. 长查询直发搜索引擎这件事只在"变体 1 已被修复"的范围内缓解：`extract_keywords` 的输出
   仍有 `两个年度` / `报表口径` / `截至` 这类非检索词，**未做停用词表扩充**（怕误伤）。
