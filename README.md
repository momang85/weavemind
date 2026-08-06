# 织光 (ZhiGuang / WeaveMind)

多智能体 AI 任务编排系统：输入一句目标，系统自动用 LLM 拆解计划，分派给一组专职
Worker 执行，最终生成报告，并通过 Web 控制台实时展示进度。支持"同一上下文连续追问"、
历史会话切换、快捷查看/重跑项目结果。

## 功能特性

- LLM 规划 → 多 Worker 并行/串行执行 → 最终交付文档（自动挑选最实质产出作为报告）
- 10 个专职 Worker：搜索、网页抓取、内容摘要、代码沙箱、数据处理、EDA、模型训练、报告生成、打包、文件 IO
- 长期记忆（ChromaDB）：任务前注入相关经验，任务后沉淀成功策略
- 对话上下文：同一会话连续追问，规划器携带前序要求与结果
- 计划评审（Critic）、失败自动重试 + 单步重规划、Worker 守护自愈、策略进化沙箱
- **自主迭代模式**：任务完成后自动验收评审，不达标自动追加步骤继续执行
- **计划可编辑**：生成计划后可上下移动/删除/添加步骤，确认后再执行
- Web 控制台：实时进度、任务树、Agent 拓扑、健康监控、历史对话、在线配置

## 演示：自主迭代模式

![自主迭代模式](docs/demo-iteration.svg)

任务不再"一次规划、一次执行"就结束，而是进入**执行 → 验收评审 → 追加步骤**的循环：

1. 规划器生成计划并执行（无依赖步骤并行）；
2. 完成后由 LLM 对照用户目标做验收评审；
3. 不达标时输出缺口（gaps）与下一步骤（next_steps），自动进入第 N+1 轮；
4. 通过评审或达到 `system.max_iterations`（默认 2 轮）后输出最终报告。

实测示例（目标要求"介绍必须包含表格 + Mermaid 架构图 + Python 代码块"）：

| 轮次 | 步骤 | 结果 |
|---|---|---|
| 第 0 轮 | 6 步（搜索/摘要/报告/代码） | 全成功，评审发现缺口 |
| 第 1 轮 | 2 步（报告 + 代码） | 全成功，仍缺要素 |
| 第 2 轮 | 3 步（报告 + 代码） | 全成功，评审通过 |

前端计划树会为第 1 轮以后的步骤显示 **「第 N 轮」** 徽标，进度实时可见。

### 计划可编辑（先确认计划）

提交任务时勾选 **「先确认计划」**：规划器生成计划后暂停，等待你编辑——
每个步骤可**上移 / 下移 / 删除**，底部可**添加新步骤**（选择能力 + 填写指令），
点「确认并执行」后按你的计划运行；「放弃」或超时（`plan_confirm_timeout`，默认 300 秒）
则取消任务。

## 安装与运行（支持多种方式）

### 方式 A：Windows 一键

```bat
start.bat   :: 一键启动（Redis + 全部服务 + 前端，自动打开浏览器 http://localhost:5173）
stop.bat    :: 一键停止
```

### 方式 B：Linux / macOS 一键

```bash
bash start.sh    # 一键启动（需要 Python 3.10+、Docker）
bash stop.sh     # 一键停止
```

### 方式 C：Docker Compose（推荐，无需本地 Python/Node）

```bash
# 1. 复制并配置密钥（docker compose 自动读取同目录 .env）
cp .env.example .env
# 编辑 .env，至少设置 LLM_API_KEY

# 2. 构建并启动（Redis + 应用，前端构建产物由后端直接伺服）
docker compose up --build -d

# 3. 访问 http://localhost:8080
# 停止: docker compose down
```

### 方式 D：手动安装

```bash
# 后端
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 前端（开发模式）
cd frontend && npm install && npm run dev            # Vite 开发服务器 :5173

# 或前端生产构建（构建后 launcher 不再启动 Vite，由 web_ui 直接伺服 :8080）
cd frontend && npm install && npm run build && cd ..

# 配置密钥
cp config.example.json config.json                   # 填入真实 API Key

# 启动 / 停止 / 状态
python launcher.py
python launcher.py stop
python launcher.py status
```

> 访问地址：开发模式 http://localhost:5173；生产/Docker 模式 http://localhost:8080。

## 配置

真实配置统一放在 `config.json`（已加入 `.gitignore`，不入库）：

```json
{
  "llm": { "api_key": "...", "base_url": "...", "model": "..." },
  "embedding": { "api_key": "...", "base_url": "...", "model": "BAAI/bge-large-zh-v1.5" },
  "redis": { "host": "localhost", "port": 6379 },
  "system": {
    "task_timeout": 90,
    "max_retry": 2,
    "replan_depth": 2,
    "critic": true,
    "critic_timeout": 30,
    "max_steps": 8,
    "max_parallel": 3,
    "max_iterations": 2,
    "scheduler": false
  }
}
```

模板见 `config.example.json`。旧 `.env` 仅作环境变量参考（Docker 方式使用）。
`system.scheduler=true` 开启每日 3:00 自动策略进化；`system.critic=true` 开启计划评审；
`system.max_steps` 限制单任务最大步骤数（默认 8），`system.max_parallel` 控制并行执行
并发度（默认 3，无依赖步骤并发执行）；`system.max_iterations` 开启**自主迭代模式**
（默认 2 轮）：任务完成后自动验收评审，不达标则追加步骤继续执行直到通过（设 0 关闭）；
可选 `planner` 段为规划器指定更稳的专用模型。

## 对话上下文

- 任务控制台提交任务后自动进入一个会话（conversation）；再次输入作为**追加要求**
  提交到同一会话，规划器携带前序要求与结果摘要一起规划。
- "项目结果"标签页可快速查看/重跑当前会话或最近的完成结果；"对话"标签页展示完整线程。
- 历史页按会话分组，可展开查看每条消息的报告，或点击"继续对话"回到控制台延续会话。

## 架构

| 组件 | 说明 |
| --- | --- |
| `orchestrator_v2.py` | 编排器：规划 -> 派发 -> 收集 -> 报告；失败自动重试 + 单步重规划；自动挑选交付内容作为最终报告 |
| `common.py` | Redis 消息/队列、SQLite/Redis 双注册表（线程安全） |
| `llm_client.py` | LLM 调用（同步/异步双通道、JSON 容错、重试退避） |
| `memory_manager.py` | ChromaDB 长期记忆：任务前注入经验（相关性阈值过滤）、任务后沉淀策略 |
| `worker_base.py` / `async_worker_base.py` | 同步/异步 Worker 基类（心跳、注册、kill 监听） |
| `workers/` | 搜索、数据处理、EDA、模型训练、报告、代码沙箱、打包、文件 IO 等专职 Worker |
| `web_ui.py` | 后端 API（8080）：任务/会话、配置、事件、指标；生产模式伺服前端构建产物 |
| `worker_guardian.py` | Worker 守护：心跳监控、进程级自动复活、隔离 |
| `metrics_collector.py` | 指标采集 |
| `evolution_sandbox.py` | 策略进化：变异 -> 锦标赛 -> 安全红线 -> 部署请求 |
| `frontend/` | React + Vite + Tailwind 控制台（任务、对话、拓扑、健康、历史、设置） |

## 数据流

```text
用户目标 -> orchestrator_v2 (LLM 拆解计划 + 记忆注入 + Critic 评审)
        -> Redis 任务队列 -> 各 Worker 执行 -> task_result 回传
        -> 依赖注入（URL/路径/前序结果自动串联）-> 最终交付报告
        -> 写入 task_history(SQLite) + 策略沉淀(ChromaDB) + 前端实时展示
```

## 测试

```bash
python smoke_test.py             # 快速端到端冒烟（需服务已启动）
python smoke_test.py --pipeline  # 完整数据流水线
python test_common.py            # 基础库单测（fakeredis）
python verification_suite.py     # 边界条件验证套件
```

## GitHub 发布说明

- 密钥与本地数据**不入库**：`config.json`、`.env`、`agents.db*`、`chroma_memory*`、日志、
  `node_modules`、`frontend/dist` 等均已加入 `.gitignore` / `.dockerignore`。
- 首次克隆后：`cp config.example.json config.json` 填入自己的 API Key（或复制 `.env.example`
  为 `.env` 后用 Docker 方式），无需任何额外依赖下载。
- Docker 构建时不携带任何密钥；镜像内不包含 `config.json`，全部通过环境变量注入。
- 如需分叉/自用，请先在自己的服务商后台轮换密钥（旧提交历史中曾包含明文 Key，
  本仓库已重建为单一初始提交）。

## 已知限制

- 模型对复杂领域目标的规划稳定性依赖所选模型；`deepseek-v4-flash` 偶发泛化，
  复杂任务建议使用更强的模型（如 `deepseek-v4-pro`）。
- 日志默认 5MB x 3 轮转，位于 `logs/`（已忽略）。
- `priority_router.py`、`auto_scaler.py` 保留但未接入主链路，可作为后续能力扩展参考。
