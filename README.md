# 织光 ZhiGuang · WeaveMind

> **一支看得见、会记忆、会自我进化的 AI 团队，跑在你自己的电脑上。**

[![CI](https://github.com/momang85/weavemind/actions/workflows/ci.yml/badge.svg)](https://github.com/momang85/weavemind/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](docker-compose.yml)

织光（WeaveMind）是一个**开箱即用的多智能体 AI 任务编排系统**：输入一句目标，
系统自动用 LLM 拆解计划、分派给一组专职 Worker 并行执行、产出交付文档——
而且它能**记住经验、对话式追问、计划可编辑、自主迭代、甚至自我进化**，全程可视化。

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

## 🚀 30 秒快速体验

```bash
# 1. 克隆
git clone https://github.com/momang85/weavemind.git && cd weavemind

# 2. 配密钥
cp config.example.json config.json   # 填入你的 LLM_API_KEY

# 3. 启动（任选一种，见下方"安装与运行"）
bash start.sh                        # 或 start.bat / docker compose up --build -d

# 4. 打开 http://localhost:5173（生产/Docker 模式为 8080），输入：
#    "调研 2026 年全球工业 AI 视觉市场并写一份董事会汇报，含架构图与 ROI 测算"
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
- 10 个专职 Worker：搜索、网页抓取、内容摘要、代码沙箱（密钥剥离）、数据处理、
  EDA、模型训练、报告生成、打包、文件 IO
- 长期记忆（ChromaDB）：任务前注入相关经验（相关性阈值过滤）、任务后沉淀策略
- 对话上下文：同一会话连续追问，规划器携带前序要求与结果
- Critic 计划评审、失败自动重试 + 单步重规划、Worker 守护自愈、进化沙箱
- Web 控制台：实时进度、任务树、Agent 拓扑、健康监控、历史会话、在线配置、LLM 用量

## 安装与运行（支持多种方式）

### 方式 A：Windows 一键

```bat
start.bat   :: 一键启动（Redis + 全部服务 + 前端，自动打开浏览器）
stop.bat    :: 一键停止
```

### 方式 B：Linux / macOS 一键

```bash
bash start.sh    # 需要 Python 3.10+、Docker（提供 Redis）
bash stop.sh     # 一键停止
```

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
cd frontend && npm install && npm run build && cd .. # 或 npm run dev 走开发模式
cp config.example.json config.json                   # 填入真实 API Key
python launcher.py                                   # 启动
python launcher.py stop                              # 停止
python launcher.py status                            # 状态
```

> 访问地址：开发模式 http://localhost:5173；生产/Docker 模式 http://localhost:8080。

## 配置

真实配置统一放在 `config.json`（已 `.gitignore`，不入库）：

```json
{
  "llm": { "api_key": "...", "base_url": "...", "model": "..." },
  "planner": { "model": "...", "base_url": "...", "api_key": "..." },
  "embedding": { "api_key": "...", "base_url": "...", "model": "BAAI/bge-large-zh-v1.5" },
  "redis": { "host": "localhost", "port": 6379 },
  "system": {
    "task_timeout": 90, "max_retry": 2, "replan_depth": 2,
    "critic": true, "critic_timeout": 30,
    "max_steps": 8, "max_parallel": 3, "max_iterations": 2,
    "plan_confirm_timeout": 300, "scheduler": false
  }
}
```

模板见 `config.example.json`；Docker 方式用 `.env`（`LLM_API_KEY` 等）。
`planner` 段可为规划器指定更稳的专用模型；`scheduler=true` 开启每日 3:00 自动进化。

## 架构

| 组件 | 说明 |
| --- | --- |
| `orchestrator_v2.py` | 编排器：规划 → 并行 DAG 执行 → 自主迭代；失败重试/重规划；计划确认 |
| `common.py` | Redis 消息/队列、SQLite/Redis 双注册表（线程安全） |
| `llm_client.py` | LLM 调用（同步/异步、JSON 容错、用量统计） |
| `memory_manager.py` | ChromaDB 长期记忆（注入 + 沉淀 + 可视化导出） |
| `worker_base.py` / `async_worker_base.py` | 同步/异步 Worker 基类（心跳、kill 监听） |
| `workers/` | 10 个专职 Worker（含 web_fetch、代码沙箱） |
| `web_ui.py` | 后端 API：任务/会话/记忆/进化/指标；生产模式伺服前端 |
| `worker_guardian.py` | Worker 守护：心跳监控、进程级复活、隔离 |
| `evolution_sandbox.py` | 策略进化：变异 → 锦标赛 → 红线 → 部署请求 |
| `frontend/` | React + Vite + Tailwind 控制台（任务、对话、拓扑、健康、记忆与进化、历史、设置） |

## 数据流

```text
用户目标 -> 规划(LLM + 记忆 + Critic) -> [可选]计划确认/编辑
        -> 并行 DAG 执行(Redis 队列 -> Workers)
        -> 验收评审 -> 未通过则追加步骤迭代
        -> 最终交付报告 -> 历史落库 + 策略沉淀 + 前端实时展示
```

## 测试

```bash
python smoke_test.py             # 快速端到端冒烟（需服务已启动）
python smoke_test.py --pipeline  # 完整数据流水线
python test_common.py            # 基础库单测（fakeredis）
python verification_suite.py     # 边界条件验证套件
```

GitHub Actions CI 自动执行后端编译/单测与前端构建。

## 路线图

- [x] 并行 DAG 执行、失败重试/重规划、Worker 守护
- [x] 对话上下文、历史会话切换、快捷查看/重跑
- [x] 自主迭代模式、计划可编辑
- [x] 记忆与进化可视化（含系统自述、锦标赛回放）
- [ ] 工具插件化 + MCP 兼容（让用户 3 行代码注册自定义工具）
- [ ] 场景模板库（数据分析 / 行业调研 / 董事会报告 / 周报…）
- [ ] 多用户鉴权与审计日志、私有化部署文档
- [ ] 代码执行容器级隔离、报告一键分享链接

## 贡献

欢迎任何形式的贡献：提 issue、改进 Worker、补测试、写文档。
开发前请先运行 `python test_common.py` 与 `npm run build` 确保不破坏现有功能。

## 已知限制

- 复杂目标的规划稳定性依赖所选模型；`deepseek-v4-flash` 偶发泛化，
  复杂任务建议在 `planner` 段配置更强的模型。
- 日志默认 5MB × 3 轮转，位于 `logs/`；`priority_router.py`、`auto_scaler.py`
  保留但暂未接入主链路。
- 密钥与本地数据不入库（`config.json`、`.env`、`agents.db*`、`chroma_memory*`、日志等均已忽略）。

## License

[MIT](LICENSE)
