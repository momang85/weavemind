# DeepSeek 执行状态（2026-09-15 更新）

**本批目标**：先把我推送后出现的两条 CI 红灯修掉，再把同一个缺陷家族（redis-py 内建重试）收口。

## 一、CI 红灯定位与修复（提交 `fix(CI)`）

1. **`docker-image` 作业 "Build image" 失败**
   - 根因：`Dockerfile` 有 `COPY prompts/ ./prompts/`，但 `prompts/` 在版本库里**零文件**（只装 gitignore 的
     `overrides.json` / `*.bak`）。本地目录还在，所以怎么跑都看不出问题；CI 干净检出后该目录不存在，
     `COPY` 直接以 `not found` 失败。本地反复构建失败于 Docker Hub 不可达（`auth.docker.io` 超时），
     一直没能走到这一步。
   - 修复：删除该行。override 属运行时可写状态——容器里落 `/data/prompts`（`WEAVEMIND_DATA_DIR`），
     `prompt_registry` 写入时 `mkdir(parents=True, exist_ok=True)`，缺失即空覆盖，镜像不需要这份空目录。
   - 守卫：`test_deploy_manifest.py` 新增 `test_copy_sources_exist_in_clean_checkout`（每个 COPY 源必须在
     git 索引里）；并用历史那一行自证有齿（喂 `COPY prompts/` → 报红）。同时把 `prompts` 从
     `REQUIRED_RESOURCES` 移除——它此前把该 bug 固化成了"必须拷进镜像"。
2. **`backend` 作业 "Settings requirements tests" 失败**
   - 根因：`test_all_config_sections_covered_or_declared` 直接打开本机 `config.json`（已 gitignore）→
     CI 无此文件，抛 `FileNotFoundError`。
   - 修复：优先 `config.json`、回退 `config.example.json`，两者都没有才 fail。已按"无 config.json"的
     CI 条件本地模拟验证通过。

## 二、同家族缺陷收口（提交 `fix(Redis)`）

冷启动竞态修的是 `common.MessagingClient`，但**同一写法还有 8 处**：`adapters/quote_cache`、
`adapters/source_health`、`lora_client`、`metrics_collector`、`orchestrator_v2._new_redis_sync`、
`tool_dispatch`、`smoke_test`、`verification_suite` 构造 `redis.Redis(...)` 时未关内建重试（后两处连超时都没有）。

- 实测代价：`/api/config/requirements` → `health_registry._redis_get` 在 Redis 不可达时单次要 26~48 秒才失败，
  设置页/健康页表现为长时间卡死；本地跑 `test_settings_requirements` 的 `TestConfigEndpoints` /
  `TestHealthRegistryProbes` 直接挂住（现在分别 26s / 42s 跑完，EXIT=0）。
- 修复：8 处全部显式传 `retry`（复用 `common._NO_REDIS_RETRY` 或文件内同款常量）；`smoke_test` /
  `verification_suite` 补 2 秒连接与读超时。
- 守卫：`test_startup_readiness.py` 新增 `TestNoUnretriedRedisClients`——AST 扫全仓，任何未传 `retry=` 的
  同步客户端（`aioredis` 除外）报红，并带检测器自证。
- 口径更正：`MessagingClient` 连不通实测 **7.6 秒**抛出（redis-py 内建重试已关，耗时来自
  `MessagingClient._connect` 自己的 3 次退避——那是应用层策略，不是 redis-py 的重试风暴）。

## 三、执行过的测试（本地 Windows / Python 3.14，真实退出码）

| 测试文件 | 结果 |
| --- | --- |
| `test_deploy_manifest` | 10 项 EXIT=0（含新增检出等价性守卫） |
| `test_startup_readiness` | 7 项 EXIT=0（含新增全仓重试守卫） |
| `test_settings_requirements` | EXIT=0（修复前 `TestConfigEndpoints`/`TestHealthRegistryProbes` 挂死） |
| `test_metrics_scope` | EXIT=0 |
| `test_delivery_chain` | EXIT=0 |
| `test_lora_manager` / `test_isolation_scope` | EXIT=0 |

## 四、阻塞与待验证

- **`docker-image` 作业本轮无本地复现**：本机到 `auth.docker.io` 超时（Docker Hub 不可达），镜像构建
  走不到 COPY 那步；修复依据是"COPY 源与干净检出不等价"这一确凿事实，最终确认只能靠 CI 复跑。
- 推送：此前 `git push` 挂死在本机 Git Credential Manager，现代理已监听，推送后需以 CI 结果为准。
- 未修（已排序）：交付包内 `index.html` 编码缺陷已检出未拦截、图表分级未覆盖自动生成图、
  美股结构化财务链路未生效（金额溯源 0%）、便携 Redis 默认源为 GitHub（无代理会卡住）。

---

# 历史记录（2026-09-14）

**当时问题**：代码沙箱安全默认值——未配置 / 取值非法 / 隔离不可用都必须拒绝执行，不得回退宿主。

**该批修改**（提交 `d6505ef`）

- `code_sandbox.py`：默认（未设置）即按 docker 策略；删除 FALLBACK 开关；非法取值抛 `SandboxConfigError` 并拒绝；`sandbox_status()` 区分 `isolation_ready`（restricted/none 恒 false）与 `execution_available`；拒绝提示只给恢复隔离的出路，不诱导关闭隔离
- `workers/code_execution_worker.py`：`_run_smoke` / `execute` 遇设施错误原样上抛并保留根因，不进入模型重生成循环（新增 `code_sandbox.is_facility_error`）
- `health_registry.py` / `web_ui.py` / `launcher.py`：健康项以隔离为判据、同时报"能否执行"；启动日志说明隔离状态但不阻断启动
- `docker-compose.yml` / `docs/部署指南.md` / `README(.en).md`：删除"挂 socket 即真隔离"的未验证承诺；容器内代码执行不可用、工作台其余功能照常
- 测试：新增 `test_sandbox_isolation.py`（24 项）；`test_p0` 沙箱类按新语义更新；`test_delivery_chain` 执行流水线用例显式选开发模式（未删用例、未全局设值）

**当时执行过的测试**：`test_p0` 387 项、`test_sandbox_isolation` 24 项、`test_delivery_chain` 171 项（排除网络类）及其余 10 个文件均 EXIT=0；mock 故障分支一律拒绝、宿主进程创建次数 0。

**新环境实测补充（`docs/新手实测报告_20260914.md`）**

- 执行隔离**无直接证据**（原表述已更正）：`grade=publish` 是图表**质检等级**，`charts_pipeline` 内置渲染走宿主 `subprocess.run`，不能据此推断容器执行
- **P0 前端缺陷已修（批 F1）**：登录/创建管理员/退出白屏（`React #300/#310`）→ `App.tsx` 去掉早退后的 `useMemo` + `ErrorBoundary` 覆盖两条分支；同批演示模式不再发真实请求、首屏状态改"连接中"、指标页区分进行中/已完成
- **F1 补修（架构审查四项）**：① 指标改为后端同范围同快照聚合（`test_metrics_scope.py` 7 项）；② 演示模式改为 fetch 层统一拦截写/付费操作；③ `grade=publish` 不能证明容器隔离，报告已更正为待验证；④ 门禁命中与恢复证据见 `docs/门禁处理记录.md`
- **冷启动竞态（P0，已修）**：新实例首次启动 15/16、编排器静默死掉；faulthandler 抓到栈落在 `redis/retry.py call_with_retry`。修复=关内建重试 + 启动器拉服务前等一次成功 PING，细则见报告 §2.5
