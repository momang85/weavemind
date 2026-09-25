# 无 Docker 也能跑完研究类任务（09-25）

**背景**：新手实机（Windows、Python 3.13、**没有 Docker**）跑研究任务失败，形态有两种：

1. 研究任务（中国平安）计划里出现 `code_execution` 步骤 → 沙箱拒绝执行 → 代码交付守门判
   "无代码交付物" → 进修复轮（修复步同样是 `code_execution`）→ 再失败，**空转十几分钟**
   （实机：2 成功 / 6 失败 / 1 运行中）。
2. 另一次任务（非上市主体）搜索拿不到候选 URL → `web_fetch` 报
   `No URL found in instruction` → 下游步骤 `Blocked by failed dependency` 连锁失败。

本批次处理第 1 类（与 Docker 依赖直接相关）；第 2 类另行处理。

## 事实基线（先确认，再改）

- **只有 `code_execution` 需要 Docker**：`workers/code_execution_worker.py` 走
  `code_sandbox`（默认 `docker`：断网、只读系统盘、仅挂任务工作区；隔离不可用即拒绝执行，
  不退到宿主解释器）。
- **图表不需要 Docker**：`charts_pipeline` 与 `workers/data_analyzer_worker.py` 都在
  **进程内**渲染（`subprocess` + `sys.executable`，matplotlib Agg）。真实交付的 6 张图
  （洋河任务）就是在**没有 Docker 的开发机**上产出的。
- 因此"研究类任务需要容器"是**规划规则造成的**，不是能力本身的要求。

## 改动

| # | 位置 | 改了什么 |
|---|---|---|
| 1 | `orchestrator_v2.PLANNER_SYSTEM` | 规则 11：图表/可视化 → **必须用 `data_analyzer`**（进程内渲染），明确"不要用 code_execution 生成图表"；规则 8 增补：`code_execution` 只用于目标明确要求代码/脚本/程序/可运行文件，研究/财报/调研类目标不得含代码步骤 |
| 2 | 代码交付守门 | 判据由"计划里有没有 `code_execution` 步骤"改为**目标是否要求代码**（新增 `_goal_wants_code`）：研究类目标不再被误判"缺代码交付物" |
| 3 | 交付修复轮前置 | 新增 `_sandbox_blocker()`：默认/显式 docker 模式且隔离不可用 → **不进修复轮**，推一条可操作提示（三条出路），不再白跑两轮 |
| 4 | 状态可见 | `dep_check.check_code_sandbox()` 把沙箱状态写进依赖自检报告（中性标记 `[--]`，**不作为启动阻塞项**——研究类任务不需要代码执行）；`launcher.py` 启动校验后打印同一行；任务级提示见改动 3 |

安全默认值**未放宽**：默认仍要求容器隔离，隔离不可用时仍拒绝执行模型生成的代码；
本批次只是把"研究类任务不依赖代码执行"这条路径做实，并把状态提前到用户跑任务之前。

## 实机可见效果

依赖自检报告（开发机，确实没有 docker）：

```
  [--] 容器隔离不可用（docker 不可用（CLI 缺失或守护进程未响应））：涉及代码执行的步骤会被拒绝，
       其余能力（检索/结构化数据/图表/报告/交付）不受影响。
       出路：① 安装并启动 Docker 后构建沙箱镜像 （docker build -f Dockerfile.sandbox -t weavimind-code-sandbox:latest .）；
             ② 本机试用可显式设 CODE_EXECUTION_SANDBOX=restricted（无隔离）；
             ③ 让任务不生成代码步骤（研究类任务默认如此）。
结论：全部就绪，可以启动
```

## 用例

```
REDIS_PORT=6399 python -m unittest test_orchestrator_v2.TestDockerFreeResearchPath   # 5 例
REDIS_PORT=6399 python -m unittest test_startup_readiness.TestCodeSandboxIsVisibleBeforeTasks  # 3 例
REDIS_PORT=6399 python -m unittest test_orchestrator_v2 test_startup_readiness test_setup_wizard  # 118 OK
REDIS_PORT=6399 python -m unittest test_delivery_chain   # 343 OK
REDIS_PORT=6399 python -m unittest test_p0               # 405 OK（含 CI 评测闸门）
```

新增用例覆盖：规划提示不再把 `code_execution` 作为图表选项、`_goal_wants_code` 按目标判定
（研究/说明/检索类为假，写程序/脚本/HTML 类为真）、交付守门读目标、`_sandbox_blocker` 的三种
情形（隔离不可用 → 拦；隔离就绪 → 不拦；操作者显式非隔离 → 不拦）、修复轮跳过、依赖自检
与启动校验都打印该状态且不阻塞。

## 未验 / 边界（如实列出）

- **规划规则是提示词，不是硬约束**：真实 planner 仍可能产出 `code_execution` 步骤；本批次
  用交付守门（改动 2）与修复轮跳过（改动 3）兜底，但"计划里带代码步骤"本身不会被拒绝。
- `CODE_EXECUTION_SANDBOX=restricted` / `none` 无操作系统级隔离（仅剥离密钥类环境变量），
  仍只适合本机试用；本批次没有改变这条默认。
- **自动构建沙箱镜像**（`docker build`）未做：镜像缺失目前只提示构建命令。有 Docker 的机器
  仍需手动构建一次；这是下一步的"一键化"候选。
- 第 2 类失败（非上市主体 / 搜索无候选 URL → `No URL found in instruction` → 链式阻断）
  未在本批次处理。
- 无 Docker 的研究任务路径此前已在开发机反复跑通（真实任务与冻结样本），但**没有**专门做过
  "无 Docker 环境下从零到交付"的端到端记录；本批次只补了状态可见与规则收敛。
