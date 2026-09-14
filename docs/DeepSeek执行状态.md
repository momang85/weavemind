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

**新环境实测补充（第一手，`docs/新手实测报告_20260914.md`）**

- 执行隔离**无直接证据**（原表述已更正）：`grade=publish` 是图表**质检等级**，`charts_pipeline` 内置渲染走宿主 `subprocess.run`，不能据此推断容器执行；需按任务取运行模式/容器执行记录后才能确认，当前记为待验证
- **P0 前端缺陷已修（批 F1，本地提交）**：登录/创建管理员/退出白屏（原 `React #300/#310`）→ `App.tsx` 去掉早退后的 `useMemo` + `ErrorBoundary` 覆盖两条分支；浏览器复测退出与登录两次转换均正常、无未捕获错误。同批：演示模式不再发真实请求（快答停用 + 开关二次确认 + 顶部常驻提示）、首屏状态改"连接中"、指标页区分进行中/已完成、交付摘要不再自称"状态 SUCCESS"
- **F1 补修（架构审查四项，本地提交）**：① 指标改为后端**同范围同快照**聚合（新增 `tasks` 分类计数 + 终态分母；数据不可用/无终态显示"未知"），前端不再用 /tasks 列表推断——新增 `test_metrics_scope.py` 7 项；② 演示模式改为 fetch 层统一拦截写/付费操作（记忆删除、演化触发、快答、通知配置 → 403 不离开浏览器），文案收窄为"只替换控制台展示数据、只读仍走真实服务"；③ `grade=publish` 不能证明容器隔离，已在报告中更正为待验证；④ 门禁命中与恢复证据见 `docs/门禁处理记录.md`
- 未修（已排序）：交付包内 `index.html` 编码缺陷已检出未拦截、图表分级未覆盖自动生成图、美股结构化财务链路未生效（金额溯源 0%）、便携 Redis 默认源为 GitHub（无代理会卡住）

**阻塞与下一步**

- 阻塞：无 Docker 守护进程（真实容器证据缺失）；git 代理未启动（无法推送 / 跑 CI）
- 下一步：待架构师复核本批；随后按指令顺序处理根任务预算贯通（P1-5）
