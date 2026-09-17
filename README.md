<div align="center">

# 织光 ZhiGuang · WeaveMind

**可复核的上市公司研究工作台：每个数字都能回溯到底稿与来源位置并重算。**

[![CI](https://github.com/momang85/weavemind/actions/workflows/ci.yml/badge.svg)](https://github.com/momang85/weavemind/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](docker-compose.yml)
[![Open Source](https://img.shields.io/badge/Open%20Source-Yes-brightgreen)](LICENSE)

🌏 [English](README.en.md) · 中文

</div>

![织光 hero 演示](docs/hero-demo.gif)

> 演示素材录制于早期版本，展示的是通用多智能体车间；当前首屏承诺已收敛为下述"可复核的
> 上市公司研究工作台"，素材待重录。

**主打：可复核的上市公司研究工作台。** 输入公司与期间，拿到一份公司研究稿、可重算的底稿
与缺口清单——**报告里每个数字都要能回溯到底稿与来源位置并重算**，算不出来的必须显式标成
"未核验/来源不足"，不能含糊过去。

多智能体、记忆、策略演进是实现机制（计划的每一步可编辑、可回放），不再作为首屏主承诺。

溯源率随任务类型差异极大，我们**按样例逐条披露**（下表），不合成"稳定区间"或平均值：

| 样例 | 数字溯源率 | 金额溯源率 | 说明与来源 |
|---|---|---|---|
| A 股活跃度日报（本仓库自述） | 85%（206/241） | — | 结构化财务链命中、报告期数据落盘 `financials.json`，数字可直接重算 |
| WBD 美股任务（独立实测 2026-09-14） | 8%（3/37），18 处模型知识 | 0%（0/17） | 根因是结构化财务数据当时未接到该 target；见 [新手实测报告](docs/新手实测报告_20260914.md) |
| 传奇影业任务（独立实测 2026-09-14） | 0 个数字可核，15 处数据缺失占位 | — | 同上 |
| 财务任务（试运行 2026-09-12） | 46% | 53%（12 处占位） | 同一目标两次复现；见 [试运行报告](docs/试运行报告_20260912.md) |

**这些样例的时期、规则与目标各不相同，不能合成一个"稳定质量区间"**。同样地，
"修复后的**合成夹具**"（金额溯源 0.833 > 0.70 阈值）与"**真实任务**"是两回事：
前者证明链路与判据接通，不代表真实抓取与真实报告的质量；后者以独立实测为准。

**核心差异一句话**：别的 Agent 用完就忘，织光 **看得见、记得住、会进化**。

> ⭐ 如果织光对你有帮助，欢迎点个 **Star**，让更多人看见这支 AI 团队。

---

## 🛡 可信交付（Trustworthy by Design）

金融研究最大的敌人是幻觉。织光把"可验证"做进了管线，而不是靠提示词祈祷：

- **全链溯源**：报告中每个数字分三档——`引用值`（可溯源到来源）/ `计算值`（可由来源数据算术验证：均值/求和/占比，容差 0.5%）/ `模型知识`（强制明示标注）；文末自动附「参考来源」清单与「数据时效」卡
- **确定性验收器**：数字溯源率、来源声明真实性、死链检测、交付完整性——不达标自动触发反思重做，直到达标或如实标记 `SUCCESS_WITH_ISSUES`
- **溯源体检 API**：`POST /api/verify` 把任意研报丢进来，30 秒返回"多少数字可验证、哪些是模型猜的"——可独立作为内容团队的防幻觉闸门
- **合规护栏**：内置红线（禁止具体投资配比与预期收益率承诺）、强制免责声明、公开分享页的匿名访问控制

### 溯源率的读法（逐条样例见首屏）

- **分水岭是"能否拿到报告期结构化财务数据"**，不是模型能力差异：结构化链路命中时数字可回溯
  到底稿并重算（85%），未命中时跌到 0%–46%。
- **不合成区间/平均值**：各样例的时期、规则与目标不同，合成出来的数没有意义。
- **合成夹具 ≠ 真实任务**：夹具（金额溯源 0.833 > 0.70 阈值）只证明链路与判据接通。
- 同一次任务内溯源率随反思迭代逐轮爬升（6% → 23% → 55% → 85%，A 股链路实测）。

> **美股链路的当前状态**：根因（结构化数据未接入 target）已修复，并补齐了英文/代码主体识别、
> 单位口径（不再把美股金额一律写成"亿元"）与子句级主体绑定，但修复目前只在**合成夹具**上验证
> （夹具显式标注"非现实公司事实"）；真实 `data.sec.gov` 抓取尚未跑通（依赖境外网络）。
> 上表的 8% 仍是可引用的独立实测值，修复后的真实成绩待补。

---

## 📰 近期里程碑

- **单任务取消**：运行中可一键停止，编排器在派发边界协作式收尾（不再只能干等"失败→重试→改计划"循环烧额度）
- **状态真源收口**：编排器是任务状态的唯一写者 + 提交收执——编排器不在时提交会明确失败，不再出现"界面显示已提交、现实里任务从未存在"
- **记忆降级可用**：Embedding 欠费/故障时自动改用关键词兜底检索，历史经验照常复用；欠费期的写入排队，恢复后自动回填
- **财务链路解锁**：按报告期抓取（年报/中报/季报），A股与港股都能拿到毛利率、经营性现金流等口径，数字可直接溯源到落盘的 `financials.json`
- **分享范围收口**：分享链接只暴露报告与其引用的图表，不再连带整个任务工作区（原始检索结果、抓取快照、交付包）
- **端点探测与 CI 加固**：连通性探测不再把"推理模型吃光 token 预算"误判为端点不可达；CI 测试不再随外网端点状态漂移

---

## ✨ 为什么是织光

市面上的 Agent 框架让你自己组装；织光给你**一支现成的 AI 团队**：

- 🧑‍💼 **开箱即用**：克隆 → 填一个 API Key → 开始干活（Windows / macOS / Linux / Docker 均可）
- 👀 **看得见**：实时进度、任务树、Agent 拓扑、"它记得什么"、进化锦标赛回放，全部可视化
- 🧠 **会记忆**：ChromaDB 长期记忆，新任务自动复用历史成功策略
- 💬 **能对话**：同一会话连续追问，历史会话随时切换继续
- ✏️ **听指挥**：生成计划后可上移/下移/删除/添加步骤，确认后执行
- 🔁 **会自我完善**：任务后自动验收评审，不达标就追加步骤继续迭代
- 🧬 **会自我进化**：策略变异 → 锦标赛 → 安全红线 → 人工审批部署

## 🆚 与常见方案对比

| 能力 | **织光 WeaveMind** | 单 Agent 对话 | AutoGPT / Manus | 自建框架（CrewAI 等） |
|---|---|---|---|---|
| 多 Worker 并行 DAG 执行 | ✅ 内置 | ❌ 单线程 | ⚠️ 部分 | 要自己写 |
| 全程可视化（任务树/拓扑/实时） | ✅ 内置 | ❌ | ❌ | 要自己写 |
| 长期记忆 + 成功策略复用 | ✅ ChromaDB | ❌ 会话内 | ⚠️ 弱 | 自己接 |
| 计划可编辑、确认后执行 | ✅ 内置 | ❌ | ❌ | 自己写 |
| 自主迭代（验收评审 → 追加步骤） | ✅ 内置 | ❌ | ⚠️ 部分 | 自己写 |
| **策略自我进化（锦标赛）** | ✅ 独有 | ❌ | ❌ | ❌ |
| 一键启动（Windows/macOS/Linux/Docker） | ✅ | — | ⚠️ | ❌ |
| 本地运行、数据私有 | ✅ | ❌ 云端 | ❌ 云端 | ✅ |
| 代码执行沙箱（密钥剥离/超时/隔离） | ✅ 内置 | ❌ | ⚠️ | 自己写 |

## 🚀 30 秒快速体验

**前置**：Python 3.10+（无需 Node —— 前端构建产物已随仓库提供；首次启动会自动下载便携 Redis，Windows 上开箱可用）。你只需要自备一个 OpenAI 兼容的 LLM API Key。

```bash
# 1. 克隆
git clone https://github.com/momang85/weavemind.git && cd weavemind

# 2. 配密钥（首次启动会自动进入中文引导；也可以启动后在「设置」页里填）
#    引导会问：供应商（SiliconFlow/DeepSeek/OpenAI/自定义）→ API Key → 模型 → 是否配 Embedding，
#    并当场测一次连通性。跳过引导手动配置也行：
cp config.example.json config.json   # 填入你的 LLM_API_KEY

# 3. 安装依赖并启动（任选一种，见下方"安装与运行"）
pip install -r requirements.txt
bash start.sh                        # Windows 用 start.bat；或 docker compose up --build -d

# 4. 打开 http://localhost:8080，输入：
#    "调研 2026 年全球工业 AI 视觉市场并写一份董事会汇报，含架构图与 ROI 测算"
#    （5173 是 `npm run dev` 开发服务器端口，普通使用无需 Node）
```

然后你会看到：计划树实时生成 → 多个 Worker 并行干活 → 每步逐个变绿 →
评审发现缺口自动进入下一轮迭代 → 最终交付完整报告。全程无需写代码。

## 🎯 核心演示

### 自主迭代模式

![自主迭代模式](docs/demo-iteration.svg)

任务进入 **执行 → 验收评审 → 追加步骤** 的循环，直到通过或达到 `max_iterations`：

1. 规划器生成计划并执行（无依赖步骤并行）；
2. LLM 对照用户目标做验收评审；
3. 不达标时输出缺口与下一步骤，自动进入第 N+1 轮；
4. 通过评审后输出最终报告，前端为每轮步骤显示 **「第 N 轮」** 徽标。

实测示例（要求"介绍必须包含表格 + Mermaid 架构图 + Python 代码块"）：

| 轮次 | 步骤 | 结果 |
|---|---|---|
| 第 0 轮 | 6 步（搜索/摘要/报告/代码） | 全成功，评审发现缺口 |
| 第 1 轮 | 2 步（报告 + 代码） | 全成功，仍缺要素 |
| 第 2 轮 | 3 步（报告 + 代码） | 全成功，评审通过 |

### 计划可编辑

提交时勾选 **「先确认计划」**：规划器暂停等待你编辑——每步可上移/下移/删除，
底部可添加新步骤（选能力 + 填指令），「确认并执行」后按你的计划运行；
「放弃」或超时（`plan_confirm_timeout`，默认 300 秒）则取消。

### 记忆与进化可视化

「记忆与进化」页面把系统看不见的积累变成看得见的资产：

- **记忆库**：最近对话（它做过什么）+ 成功策略（它学到了什么，可展开）
- **系统自述**：让 LLM 基于真实记忆写一段可直接发布的自述，一键复制
- **进化锦标赛回放**：每轮进化的胜者、稳定性、得分板、每个测试任务的排名与评审分数；
  一键「触发新一轮进化」，数分钟后刷新即可回放

## 功能特性

- LLM 规划 → 多 Worker **并行 DAG** 执行 → 自动挑选最实质产出作为最终报告
- 11 个专职 Worker：搜索、网页抓取、内容摘要、代码沙箱、数据处理、
  EDA、模型训练、报告生成、打包、文件 IO、ReAct 迭代式调研
- **结构化数据适配器**：东方财富（港股）、SEC EDGAR（美股）、A 股、CoinGecko（加密货币）、
  FRED（宏观指标）、Google News（新闻）——金融/加密/宏观任务自动预载权威数据
- 长期记忆（ChromaDB）：任务前注入相关经验（相关性阈值过滤）、任务后沉淀策略
- 对话上下文：同一会话连续追问，规划器携带前序要求与结果
- Critic 计划评审、失败自动重试 + 单步重规划、Worker 守护自愈、进化沙箱
- **验收器**（数字溯源/主体归属/来源标注诚实性）→ 带缺口任务标记 `SUCCESS_WITH_ISSUES`，
  反思按失败诊断（step_failure.json）精准补缺口
- **多用户鉴权 + 审计日志**：登录/角色（admin/viewer）、操作审计、初始管理员引导
- **报告分享**：一键生成公开只读链接（可选密码 + 自定义有效期 1–30 天）
- **任务完成通知**：Webhook / Server酱 / Email 三通道
- **报告 PDF 导出**：服务端生成（嵌入中文字体），或浏览器打印
- **多项目工作区**：任务按项目隔离组织，成果互不干扰
- **定时任务**：按间隔或每日时间自动执行目标
- **MCP 兼容**：自研 MCP Server（mcp_lite）+ 第三方 MCP Client 即插即用
- **代码沙箱**：默认要求容器隔离（docker）；隔离不可用时拒绝执行、不回退宿主；restricted / none 无操作系统级隔离，仅限显式选择
- **模型分级路由**：规划/执行/评测按用途选模型；相同目标规划缓存命中零成本
- **成本与可观测**：任务级 token/成本台账、SLO 看板、LLM 端点健康与自动切流
- Web 控制台：实时进度、任务树、Agent 拓扑、健康监控、历史会话、在线配置、LLM 用量

## 安装与运行（支持多种方式）

### 对话上下文与导入背景

- 提交任务后自动进入一个会话（conversation）；再次输入作为**追加要求**提交到同一会话，
  规划器携带前序要求与结果摘要一起规划，历史会话可随时切换继续。
- **导入上下文**：任务输入框下方可展开"导入上下文"区，粘贴需求背景、参考资料、
  URL 或约束条件，随任务一起进入规划，并随会话保留、供后续追问沿用。

### 方式 A：Windows 一键

```bat
start.bat   :: 一键启动（Redis + 全部服务 + 前端，自动打开浏览器）
stop.bat    :: 一键停止
```

首次运行 `start.bat` 会自动 `pip install -r requirements.txt`，并在检测到
`frontend/dist` 缺失时自动执行 `npm install && npm run build`（需已安装 Node.js 18+；
没有 Node 也能启动，Web 界面将使用内置回退页，功能受限）。

### 方式 B：Linux / macOS 一键

```bash
bash start.sh    # 需要 Python 3.10+、Redis（Docker 可选，见下）
bash stop.sh     # 一键停止
```

与 Windows 一致：首次运行自动装依赖、自动构建前端。

> **不需要 Docker**：项目唯一的外部依赖是 **Redis**（消息总线/任务队列），
> SQLite 与向量库都是本地内嵌。启动时会**自动依赖自检**——缺失的 pip 包自动补装，
> Redis 缺失时（Windows）自动下载便携版并启动、其它平台优先用系统 `redis-server`
> 或给出安装指引。也可手动执行：`python launcher.py deps`（只报告）/
> `python launcher.py deps --fix`（检测并补齐）。
> Docker 只是可选的容器化部署方式（方式 C），不是运行前提。

> **Python 版本**：支持 3.10–3.14。官方 `pygame` 在 3.14 暂无 wheel，
> 但 **pygame-ce 2.5.6+ 完整支持 3.10–3.14**（drop-in 替代，导入名仍是 `pygame`）。
> 想让 `code_execution` 生成的 pygame 游戏可运行，执行
> `pip install -r requirements-games.txt`（可选依赖）即可。
> 系统会自动探测环境：有 pygame 就用 pygame；没有则引导生成 turtle / 单文件 HTML 方案，
> 不影响其他功能。

### 方式 C：Docker Compose（推荐，无需本地 Python/Node）

```bash
cp .env.example .env        # 至少设置 LLM_API_KEY
docker compose up --build -d
# 访问 http://localhost:8080 · 停止: docker compose down
```

### 方式 D：手动安装

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
npm install --prefix frontend && npm run build --prefix frontend  # 仓库已带构建产物，此步可跳过
cp config.example.json config.json                   # 填入真实 API Key
# 准备 Redis（二选一）：
#   docker run -d --name zhiguan-redis -p 6379:6379 redis:7-alpine
#   或安装原生 Redis（Memurai / tporadowski-redis / apt install redis-server）
python launcher.py                                   # 启动（会预检 Redis，失败给出指引）
python launcher.py stop                              # 停止
python launcher.py status                            # 状态
```

> 访问地址：http://localhost:8080（仓库内已附构建好的前端产物，**无需 Node 即可使用**）。
> 仅在修改前端源码后才需要重新构建：`rm -rf frontend/dist && npm install --prefix frontend && npm run build --prefix frontend`。

#### 跨设备与小技巧

| 场景 | 做法 |
|---|---|
| 终端中文乱码（Windows cmd 默认 GBK） | 脚本会自动切 UTF-8 代码页；仍异常时用 `WM_PLAIN_TEXT=1` 输出纯英文 |
| 只有 `py` 启动器 / 只有 `python3` | `start.bat` 自动回退 `py -3`；`start.sh` 优先 `python3` 再回退 `python` |
| CI / 管道里运行（无交互） | `WM_NONINTERACTIVE=1`（start 不 pause）；`WM_ASSUME_YES=1`（stop 跳过 Redis 询问） |
| Windows 防火墙反复弹框要放行 Redis | 便携 Redis 只监听本机回环（`--bind 127.0.0.1 -::1`），不暴露局域网、不触发入站询问；需要局域网共享时 `WM_REDIS_BIND=0.0.0.0`（历史"阻止"规则的放行命令见部署指南 5.1） |
| 启动后想确认服务真的起来了 | 启动会自动校验并打印 `N/N 服务存活`；`WM_START_STRICT=1` 时未全部存活以非零码退出 |
| 只想看依赖是否齐备 | `python launcher.py deps`（只报告）/ `--fix`（自动补齐） |
| 停止 | `python launcher.py stop` 会一并停掉本项目启动的便携 Redis；系统级 Redis 需自行管理 |

## 配置

真实配置统一放在 `config.json`（已 `.gitignore`，不入库）：

```json
{
  "llm": { "api_key": "...", "base_url": "...", "model": "..." },
  "planner": { "model": "...", "base_url": "...", "api_key": "..." },
  "backup": { "api_key": "...", "base_url": "...", "model": "..." },
  "embedding": { "api_key": "...", "base_url": "...", "model": "BAAI/bge-large-zh-v1.5" },
  "redis": { "host": "localhost", "port": 6379 },
  "system": {
    "task_timeout": 300, "max_retry": 2, "replan_depth": 2,
    "critic": true, "critic_timeout": 30,
    "max_steps": 8, "max_parallel": 3, "max_iterations": 2,
    "stall_timeout": 300, "plan_confirm_timeout": 300, "scheduler": false
  }
}
```

模板见 `config.example.json`；Docker 方式用 `.env`（`LLM_API_KEY` 等）。

- **模型分级**：`llm` 为执行模型（Worker），`planner` 为规划器专用更稳模型；
- **双源 failover**：`backup` 为主端点失败时自动切换的备用 LLM（搜索同样内置
  DuckDuckGo → Bing → 兜底 三级回退）；
- **规划自检**：计划缺少报告/总结步骤时自动补一步 `report_generator`（报告兜底）；
- **任务模板**：控制台可选"数据分析流水线 / 行业调研报告 / 董事会汇报"模板，
  确定性步骤、跳过规划直接执行（`templates.json`）；
- **结果缓存**：提交相同目标可携带 `cache_ttl_min`（分钟），TTL 内命中成功结果直接返回；
- `scheduler=true` 开启每日 3:00 自动进化。

### 多用户鉴权与审计（V1.0）

Web 控制台（8080）默认要求登录，未登录只能访问公开只读分享页（`/share/<token>`）、
`/api/health` 与前端静态资源；其余 `/api/*`、`/task/*`、`/tasks` 一律需要会话。

- **用户存储**：`config.json` 的 `users` 段，密码用 `pbkdf2_hmac(sha256) + 随机盐`
  存储（格式 `pbkdf2_sha256$迭代次数$盐$哈希`），绝不明文；前端读取配置时也会剥离该段。
- **首次初始化（二选一）**：
  1. 启动前设置 `WEAVEMIND_ADMIN_PASSWORD=你的密码`（可选 `WEAVEMIND_ADMIN_USERNAME`，
     默认 `admin`），服务启动时自动创建 admin；
  2. 什么都不设，首次打开登录页会引导创建初始管理员（`POST /api/setup-admin`，仅一次）。
  手工配置示例：
  ```json
  "users": {
    "admin": { "password_hash": "pbkdf2_sha256$200000$<BASE64_SALT>$<BASE64_HASH>", "role": "admin" }
  }
  ```
  可用 `python scripts/hash_password.py admin 你的密码` 生成真实哈希。
- **会话**：登录返回 `secrets.token_urlsafe(32)` token，服务端内存保存
  `{user, role, expires}`，TTL 默认 24h（`SESSION_TTL_SECONDS` 可调）；前端用
  `Authorization: Bearer <token>`，后端同时兼容 `Cookie: session=<token>`。
- **角色矩阵**：`admin` 可提交/删除任务、生成/撤销分享、保存配置、查看审计；
  `viewer` 只读（历史、状态、报告、记忆等），写操作与配置/审计接口返回 403。
- **审计日志**：追加写入 `logs/audit.jsonl`（JSON Lines），记录登录成败、登出、
  任务提交/删除、分享生成/撤销、配置保存，含 `timestamp/user/ip/action/target/result`；
  管理员可用 `GET /api/audit?limit=200` 查询。

## 架构

| 组件 | 说明 |
| --- | --- |
| `orchestrator_v2.py` | 编排器：规划 → 并行 DAG 执行 → 自主迭代；失败重试/重规划；计划确认；验收状态诚实化 |
| `common.py` | Redis 消息/队列、SQLite/Redis 双注册表（线程安全） |
| `llm_client.py` | LLM 调用（同步/异步、JSON 容错、用量统计、模型分级路由、调用缓存、余额感知） |
| `memory_manager.py` | ChromaDB 长期记忆（注入 + 沉淀 + 可视化导出） |
| `worker_base.py` / `async_worker_base.py` | 同步/异步 Worker 基类（心跳、kill 监听） |
| `workers/` | 11 个专职 Worker（含 web_fetch、代码沙箱、ReAct） |
| `adapters/` | 结构化数据适配器：eastmoney（港股）/ sec_edgar（美股）/ A股 / coingecko / macro（FRED）/ news（RSS）+ resolver/router |
| `acceptance_checker.py` | 验收器：数字溯源、主体归属、来源标注诚实性（含媒体别名归组、补录建议） |
| `web_ui.py` | 后端 API：任务/会话/记忆/进化/指标/鉴权/审计/分享/通知/PDF/定时任务；生产模式伺服前端 |
| `worker_guardian.py` | Worker 守护：心跳监控、进程级复活、启动宽限期、隔离 |
| `evolution_sandbox.py` | 策略进化：变异 → 锦标赛 → 红线 → 部署请求 |
| `notifications.py` | 任务完成通知：Webhook / Server酱 / Email |
| `report_pdf.py` | 纯 Python PDF 生成（嵌入中文字体，跨平台） |
| `scheduled_jobs.py` | 定时任务调度（interval / 每日 cron） |
| `launcher.py` | 服务启动/停止/状态 + supervise 守护模式（崩溃自愈） |
| `frontend/` | React + Vite + Tailwind 控制台（任务、对话、拓扑、健康、记忆与进化、历史、设置、登录） |

## 数据流

```text
用户目标 -> 规划(LLM + 记忆 + Critic) -> [可选]计划确认/编辑
        -> 并行 DAG 执行(Redis 队列 -> Workers)
        -> 验收评审 -> 未通过则追加步骤迭代
        -> 最终交付报告 -> 历史落库 + 策略沉淀 + 前端实时展示
```

## 测试

```bash
python smoke_test.py             # 快速端到端冒烟（需服务已启动；V1.0 起自动登录）
python smoke_test.py --pipeline  # 完整数据流水线
# 冒烟账号默认 admin/admin，可用 WEAVEMIND_SMOKE_USER / WEAVEMIND_SMOKE_PASSWORD 覆盖
python test_common.py            # 基础库单测（fakeredis）
python test_orchestrator_v2.py   # 编排器回归（调度/迭代/能力校验，fakes 模式）
python test_auth_audit.py        # 多用户鉴权与审计日志回归
python verification_suite.py     # 边界条件验证套件
```

GitHub Actions CI 自动执行后端编译/单测与前端构建。

## 路线图

**V1.0 已完成 ✅**

- [x] 并行 DAG 执行、失败重试/重规划、Worker 守护（含启动宽限期与残留清理）
- [x] 对话上下文、历史会话切换、快捷查看/重跑
- [x] 自主迭代模式、计划可编辑
- [x] 记忆与进化可视化（含系统自述、锦标赛回放）
- [x] 工具插件化 + MCP 兼容（`mcp_lite.py` 自研 MCP server / `mcp_client.py` 第三方 MCP / `tool_dispatch.py` 路由+审计）
- [x] 多用户鉴权与审计日志（admin/viewer 角色、操作审计、初始管理员引导）
- [x] 报告一键分享链接（公开只读 + 可选密码 + 自定义有效期）
- [x] 代码执行容器级沙箱（默认要求容器隔离；隔离不可用即拒绝执行，不回退宿主）
- [x] 私有化部署文档（[docs/部署指南.md](docs/部署指南.md)）+ Docker Compose / Windows / Linux 一键
- [x] 验收器（数字溯源/主体归属/来源标注诚实性）→ SUCCESS_WITH_ISSUES 状态诚实化
- [x] 结构化数据适配器：港股/美股/A股/加密货币/宏观/新闻 六类数据源
- [x] 任务完成通知（Webhook / Server酱 / Email）、报告 PDF 导出、多项目工作区、定时任务
- [x] 模型分级路由（plan/exec/judge 分层）+ LLM 调用缓存 + 反思收敛（成本优化）
- [x] 编排器/WebUI/配置韧性：launcher 守护自愈、Redis AOF、system 配置热重载
- [x] 场景模板库基础（4 个手工模板 + auto-* 自动固化，手工模板优先路由）
- [x] 评测集随真实任务自动生长（验收 fail 自动沉淀为新评测案例 `evals/auto_grow.py`）
- [x] 策略灰度与回滚（进化胜者 rollout 灰度分流，灰度成功率低于阈值自动回滚）
- [x] 错误模式库（step_failure 按 error_type 跨任务聚合为修复模板，注入反思 prompt）
- [x] 成本预算机制（月度预算超限自动降级高价角色模型，`/api/health` budget 可观测）
- [x] 多语言报告（提交 `language` 字段）+ 分享页 HTML 主题模板（light/dark/paper）

## 贡献

欢迎任何形式的贡献：提 issue、改进 Worker、补测试、写文档。
开发前请先运行 `python test_common.py` 与 `npm run build` 确保不破坏现有功能。

## 已知限制

- 复杂目标的规划稳定性依赖所选模型；规划器（`planner` 段）建议配置强推理模型，
  当前默认映射为 `qwen3.7-max`（见 `config.json` 的 `model_roles`）。
- 日志默认 5MB × 3 轮转，位于 `logs/`；`priority_router.py`、`auto_scaler.py`
  保留但暂未接入主链路。
- 数字溯源已绑定**子句级主体**：数字紧邻的子句里写明了他司、且来源行主体不同时会被拒绝。
  但报告只在标题/文档级声明主体时仍不参与绑定——同一数字在多处出现、单元内没有局部归属时，
  仍可能被判为可溯源（对抗测试 `test_acceptance_adversarial.py::test_known_gap_number_subject_mismatch`
  记录该缺口，文档级主体识别尚未实现）。金额阈值 0.7 未因美股链路而下调。
- 美股链路（SEC EDGAR）已接入结构化路由并绑定主体/报告期/单位，但验证范围只到
  "合成夹具 + 真实路由与验收入口"，未跑真实网络抓取（见上"实测成绩"表后的说明）。
- 结构化数据链路只覆盖 A 股/港股/美股/加密/宏观；其它市场与品类仍走通用搜索，
  溯源率落在上表的"纯调研任务"档。
- 密钥与本地数据不入库（`config.json`、`.env`、`agents.db*`、`chroma_memory*`、日志等均已忽略）。

## License

[MIT](LICENSE)
