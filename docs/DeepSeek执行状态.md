# DeepSeek 执行状态（2026-09-14）

**当前问题**：代码沙箱安全默认值——未配置 / 取值非法 / 隔离不可用都必须拒绝执行，不得回退宿主。

**本批修改**（提交 `d6505ef`）

- `code_sandbox.py`：默认（未设置）即按 docker 策略；删除 FALLBACK 开关；非法取值抛 `SandboxConfigError` 并拒绝；`sandbox_status()` 区分 `isolation_ready`（restricted/none 恒 false）与 `execution_available`；拒绝提示只给恢复隔离的出路，不诱导关闭隔离
- `workers/code_execution_worker.py`：`_run_smoke` / `execute` 遇设施错误原样上抛并保留根因，不进入模型重生成循环（新增 `code_sandbox.is_facility_error`）
- `health_registry.py` / `web_ui.py` / `launcher.py`：健康项以隔离为判据、同时报"能否执行"；启动日志说明隔离状态但不阻断启动
- `docker-compose.yml` / `docs/部署指南.md` / `README(.en).md`：删除"挂 socket 即真隔离"的未验证承诺；容器内代码执行不可用、工作台其余功能照常
- 测试：新增 `test_sandbox_isolation.py`（24 项）；`test_p0` 沙箱类按新语义更新；`test_delivery_chain` 执行流水线用例显式选开发模式（未删用例、未全局设值）

**执行过的测试**（本地 Windows / Python 3.14，均为真实退出码）

- `test_p0` 387 项 EXIT=0；`test_sandbox_isolation` 24 项 EXIT=0；`test_delivery_chain` 171 项（排除网络类）EXIT=0
- `test_common` / `test_orchestrator_v2` / `test_prompt_system` / `test_setup_wizard` / `test_acceptance_adversarial` / `test_financial_chain` / `test_task_state` / `test_task_persistence` / `test_deploy_manifest` / `test_writer_consolidation` 全部 EXIT=0
- mock 故障分支（缺 CLI / 守护进程不可达 / 镜像缺失 / 启动异常 / 异步 communicate 后容器层失败）：一律拒绝，宿主进程创建次数 0；容器内脚本报错原样返回、不重跑

**尚未验证**（与上条分开报告）

- **真实容器隔离**：本机 Docker 守护进程未运行 → 沙箱镜像构建与容器内实际执行未做，需在有 Docker 的环境单独验证
- `test_delivery_chain` 的 `TestSourceHealthRouting` / `TestSinaRankingAdapter` 未跑（本机访问不到新浪行情），由 CI 覆盖
- CI 未跑：本机 git 代理 `localhost:7897` 未监听，`git push` 失败，本批提交仅存在本地

**阻塞与下一步**

- 阻塞：无 Docker 守护进程（真实容器证据缺失）；git 代理未启动（无法推送 / 跑 CI）
- 下一步：待架构师复核本批；随后按指令顺序处理根任务预算贯通（P1-5）
