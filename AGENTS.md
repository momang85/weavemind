# 织光（WeaveMind）— 项目指南

> 本文件由项目优化工程师维护，供 AI 编码代理（DeepSeek Harness / ZCode 等）在本工作区会话时自动加载。最后更新：2026-09-27。

## 定位

上市公司研究工作台（研究试用阶段）：把公开披露资料整理成**可复核**的研究交付——正文 + 可复算底稿 + 来源定位 + 证据包。首期聚焦 A 股非金融公司两期经营研究。明确不做自动交易、授信、目标价。三种状态严格分离：**机器验收 / 研究状态 / 人工复核**。

## 运行形态

- 后端 Python 3.11 多进程单体：stdlib `BaseHTTPRequestHandler` 手写 API（无 Web 框架）；前端 React/TS/Vite/Zustand。
- Redis = 队列与消息总线；`agents.db`（SQLite）= 任务/会话/审计；ChromaDB = 记忆检索。
- 启动：`start.bat` → `launcher.py up`（依赖自检/补装 + 便携 Redis + 拉起 16 个服务进程：11 worker、critic、orchestrator、webui、guardian、metrics）；端口 8080。
- 三个镜像：`Dockerfile`（主，端口 8080）、`Dockerfile.sandbox`（code_execution 沙箱，`--network none` 只读根）、`Dockerfile.worker`（EvolutionSandbox 竞技场 worker）。
- 容器内必须 `WEAVEMIND_SUPERVISE=1`，否则 PID 1 退出、全部子进程死亡。

## 模块地图

| 模块 | 职责 |
|---|---|
| `adapters/` | 数据源适配层：eastmoney/sina/cninfo/sec_edgar/coingecko/news/macro 抓取器 + `router.py` 能力注册 + `resolver.py` 公司名→代码 + `quote_cache.py` |
| `orchestrator_v2.py` | 编排核心：计划、预算、契约校验、派发与反思重做 |
| `web_ui.py` | HTTP API + 工作台页面 |
| `launcher.py` | 统一服务进程管理器 |
| `llm_client.py` | OpenAI 兼容调用与 token 分账 |
| `facts.py` / `narrative_evidence.py` | 事实与证据定位 |
| `working_paper.py` | 确定性计算底稿 |
| `question_assessment.py` | 逐问题证据覆盖评估 |
| `report_brief.py` → `acceptance_checker.py` → `report_version.py` / `delivery_pipeline.py` | 成稿 → 机器验收 → 版本重验/冻结快照/哈希交付包 |
| `admission.py` | 成功经验准入的唯一显式谓词 |
| `source_admission.py` | 检索来源准入（候选/待核验/采用/剔除四分）：主体、期间、文档用途、资料截止日四判据；宏观/行业材料只能作背景 |
| `research_intent.py` | 提交意图与契约判定：一般任务 / 公司研究 / 界外请求四分，缺契约给问题清单（不编造、不回填） |
| `search_evidence.py` | 检索尝试与采纳证据分账（P1-D）：`search_attempts.json` 试过什么、`search_adopted.json` 采纳了什么；阶段预算（initial/retry/repair）、更差结果不覆盖既有采纳证据、无新增材料停损 |
| `build_identity.py` | 实例身份与构建指纹（P1-E）：文件字节哈希区分磁盘版本 / 启动时已加载版本 / 前端构建，`/api/health`·`/api/status` 暴露 `build.restart_required`；不用 git HEAD 冒充已加载代码，输出不含路径与密钥 |
| `data_sources.py` | 用户自加数据源（设置页编辑）：HTTP JSON/MCP 两类，登记出站端点 `data_source:<id>`、注册工具 `finance_custom_<id>`、逐源探测；正文是否可入模型上下文由 `model_context` 决定 |
| `credentials.py` | 本机密钥仓 `credentials.json`（0600，不进 git）：数据源/MCP 密钥只在此处，接口只回「已配置」布尔 |
| `_trial_*.py`、`probe_batchA.py` | 一次性调试脚本（勿引用、勿"修复"） |

## 核心数据流

浏览器 → `web_ui` POST `/api/task/...`（`_publish_task`，web_ui.py:3561）→ Redis pubsub `orchestrator:main`（orchestrator_v2.py:8340）→ `OrchestratorV2.run` 生成计划并绑定执行契约 → `_dispatch`（orchestrator_v2.py:4525，契约指纹校验）→ `lpush task_queue:{agent_id}` → 各 worker（`BaseWorker` 子类，worker_base.py:172）→ 结果经 Redis 回传 → facts/证据落工作区 → `working_paper` 计算 → `report_brief` 成稿 → `acceptance_checker` 验收 → `delivery_pipeline` 冻结快照出 zip → 下载。

## 配置与扩展点

- `config.json`：llm / embedding / planner / backup / redis / system / users，`model_roles` 做角色→模型映射。
- 环境变量：`WEAVEMIND_DATA_DIR`、`REDIS_HOST`、`WEB_PORT`、`SKIP_DEP_CHECK` 等；`.env` 仅 Compose 用。日志落地：`WEAVEMIND_LOG_DIR`（显式）> `WEAVEMIND_DATA_DIR/logs` > 测试进程系统临时目录 > 仓库 `logs/`；`WEAVEMIND_TEST_MODE=1` 强制按测试进程处理（测试日志绝不混入生产 `logs/`）。
- 扩展点：adapters 的 `DATA_CAPABILITIES` 注册表、`data_sources.py`（用户自加数据源：HTTP JSON/MCP + 密钥仓，设置页可编辑）、`mcp_client.py`/`tool_dispatch.py`（MCP-lite）、`skills/`、prompts 运行时 overrides。

## 代理工作守则

1. **根目录平坦布局**：202 个根级 .py 顶层互相 import。改公共模块前先全局搜引用。
2. **巨文件**：orchestrator_v2.py 8445 行、web_ui.py 6522 行（212 方法）、acceptance_checker.py 3767 行、llm_client.py 2847 行。小步修改，别顺手重构；所谓拆分只是 Mixin 搬代码，不改耦合。
3. **测试平铺根目录**（99 个 test_*.py，test_p0.py 438 用例）：按文件运行，别全量跑。
4. **密钥**：`.env` 与 `config.json` 含真实 key；绝不提交、绝不粘贴到日志或回复。数据源/MCP 密钥走 `credentials.json`（0600，已被 .gitignore；环境变量 `WEAVEMIND_DATA_SOURCE_<ID>_KEY`/`WEAVEMIND_MCP_<NAME>_KEY` 优先），接口只回「已配置」不回明文。
5. **三套 worker 基类并存**（根 `worker_base.py`、`workers/`、`async_worker_base.py`）：改动前先确认现役实现。
6. 研究交付的语义红线：验收状态与人工复核分离，勿在代理侧"替用户"标记复核通过。

## 优化积压（优化工程师维护，按影响排序）

1. **密钥明文散落**：`.env` 真实 key；`config.json` 4 处明文 api_key；6 个 `config.json.bak*` 同含密钥且其中至少一个未被 .gitignore 覆盖。→ 迁移到环境变量/凭据库、补 .gitignore、轮换已暴露 key。
2. **巨文件耦合**（见上）→ 沿数据流边界渐进抽包，先立接缝测试再动手。
3. **根目录临时物堆积**：`_trial_*`×6、`probe_batchA.py`、`worker_base.py.log`（76MB）、7 个 config 备份、`distill_*.jsonl`、0 字节 `tasks.db`、10.9MB agents.db+WAL 全在 git 根，约 50 项未跟踪。→ 归档清理 + .gitignore。
4. **测试巨型混居** → 拆 `tests/` 目录按模块分文件，CI 分片。
5. **隐式依赖环**：`adapters/router.py` 顶层 import 根模块 `task_classifier`；charts_pipeline 靠方法体内延迟导入破环。→ 引入包结构与依赖方向规则。
6. **同步轮询式编排**：`_dispatch` 无 worker 时 `time.sleep(5)×6` 阻塞（orchestrator_v2.py:4590+）；`_NO_REDIS_RETRY` 补丁在 llm_client.py、common.py、web_ui.py、tool_dispatch.py 四处复制。→ 抽公共 Redis 客户端 + worker 就绪事件。
7. **冗余并行实现**：`chroma_memory/`、`chroma_memory_test/`、`chroma_memory_verify/` 三目录并存；`mcp_client.py` vs `mcp_lite.py`。→ 定一个删其余。
8. **运行时自改环境**：dep_check.py 59KB + launcher 内嵌 pip 安装 + 三套 requirements lock 并存，可复现性差。→ 固定锁文件，装依赖只发生在安装期。
9. **容器语义靠注释补救**：CMD 直接 `launcher.py`（启动即返回）靠 compose 注入 `WEAVEMIND_SUPERVISE=1` 常驻；config.json 未持久化卷。→ CMD 改前台常驻 + 补卷。
10. **worker 基类三套并存**（见守则 5）→ 明确现役实现后合并，其余删除。
