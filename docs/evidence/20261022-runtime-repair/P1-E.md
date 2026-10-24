# RUNTIME-1022 第五批（P1-E）：在线版本与后续验收准备 — 实际修复与验证记录

> 本批全部改动**只在工作区源码与证据目录**，未 `git commit`/`push`，未重启在线
> 实例，未触发付费研究，未修改生产配置 / 凭据 / 系统 DNS / 代理。
> 结论先行：**构建指纹与日志隔离已验证（离线）；受控重启计划已备好未执行**；
> 在线实例 PID 24572 仍在跑 P1-E 之前的代码（实测 `/api/health` 无 `build` 字段）。

## 1. 诊断问题 → 本批修复

| 诊断问题 | 修复 |
|---|---|
| 分不清"磁盘源码 / 进程已加载代码 / 前端构建"——曾想用 `git rev-parse HEAD` 充当版本 | 新增 `build_identity.py`：**文件字节哈希**（sha256 前 12 位）覆盖 13 个关键模块；进程**导入即快照**（= 启动时刻版本），此后磁盘再变 → `source.changed` 点名文件、`source.restart_required=true`；前端 `frontend/dist` 同口径（`index.html` + assets 清单哈希，无 dist 如实报 `dev`）。**不用 git HEAD 冒充已加载代码** |
| `/api/health` 无实例身份，探活方不知道"在线在跑哪份代码" | `/api/health`（公开）与 `/api/status`（登录）新增 `build` 视图：`instance`（复用 `health_registry.instance_id()`：工作区根+端口指纹）、`pid`、`started_at`、`uptime_sec`、python 版本、`source{loaded_sha, disk_sha, changed[], restart_required}`、`frontend{...}`、`workspace_root_id`/`data_root_id`（短哈希，**不含路径**）、`restart_required`、如实说明 note |
| 重启无计划——历史上有过"重启把跑着的任务扔了 / 回退找不到基线"的坑 | `P1-E-restart-plan.md`：前置检查（无 RUNNING/QUEUED 任务、队列空、历史可读）、备份（git 状态存档 + agents.db 在线 backup 只读拷贝）、回退路径（git stash + launcher up / db.bak 恢复）、服务与数据根清单（16 服务、8080、便携 Redis、agents.db 位置、工作区根）、执行步骤、重启后四步验收（实例身份→历史只读复验→一轮范围内研究预算 6 次/60s→日志隔离抽查）、已知待办如实保留。**只准备，不执行** |
| 测试日志与生产日志混在同一 `logs/`（模拟 Redis / 模拟任务名 t-wd 的条目混进真实故障结论） | `logging_setup.py` 落地优先级：`WEAVEMIND_LOG_DIR`（显式）> `WEAVEMIND_DATA_DIR/logs`（数据根）> **测试进程系统临时目录**（`tempdir/weavemind-test-logs/`，绝不写生产 `logs/`）> 仓库 `logs/`（生产默认，行为不变）。测试进程判定保守双条件：入口 `test_*.py` + 已导入 unittest，或显式 `WEAVEMIND_TEST_MODE=1`；生产入口永不误判 |

## 2. 修复明细（文件级）

| 文件 | 改动 |
|---|---|
| `build_identity.py`（新，约 210 行） | `SOURCE_FILES`（13 个关键模块）、`file_sha12` / `source_fingerprint` / `loaded_snapshot` / `changed_files`、`frontend_fingerprint`、`_root_id`（路径→8 位短哈希）、`identity()`（完整视图）、`reset_loaded_for_test` |
| `web_ui.py` | `_get_health` 增加 `payload["build"]`（失败只告警不坏探活）；`_system_status()` 增加 `"build": _build_identity_view()`（新辅助函数，失败降级空对象） |
| `logging_setup.py` | `LOG_DIR_ENV`/`is_test_run()`/`log_dir()`；`setup_logging` 改用 `log_dir()`（生产默认行为不变，仍是仓库 `logs/`） |
| `.github/workflows/ci.yml` | 新增 2 步：`python test_build_identity.py`、`python test_log_isolation.py`（累计 6 个本会话新测试文件全部进 CI 门禁） |
| `AGENTS.md` | 模块地图补 `build_identity.py` 行；环境变量补日志落地优先级；测试数量更新（99 个 test_*.py） |
| `test_build_identity.py`（新，10 用例） | 无路径/密钥泄漏；指纹稳定与字节敏感；临时基缺失；改名检测；`restart_required` 点名文件；快照防御拷贝；前端 built/dev 两态；文件名清单无路径 |
| `test_log_isolation.py`（新，10 用例） | 优先级四级；测试判定三态（test 入口 / 生产入口不误判 / TEST_MODE）；`setup_logging` 落临时目录且生产 `logs/` 无同名文件；显式目录 UTF-8 |

## 3. 验证（真实退出码，无 tail 掩码）

| 验证 | 结果 | 退出码 |
|---|---|---|
| `python test_build_identity.py`（新 10 用例） | OK | 0 |
| `python test_log_isolation.py`（新 10 用例） | OK | 0 |
| `python test_deploy_manifest.py`（41 用例，含"测试文件必须进 CI"门禁） | OK（新增 2 步登记后转绿） | 0 |
| `python test_task21_supervise_default.py`（24 用例，mock setup_logging 路径） | OK | 0 |
| `python -m py_compile build_identity.py logging_setup.py web_ui.py` | 无输出 | 0 |
| 离线接口验证：`web_ui._get_health(fake, "/api/health")` → `build.pid == getpid()`、`source.file_count==13`、json 无 `C:`/反斜杠/`api_key` | OK | 0 |
| 离线接口验证：`web_ui._system_status()` → `build` 存在 | OK | 0 |
| 在线探针（只读 GET，无成本）：`http://127.0.0.1:8080/api/health` → `status=ok`，**无 `build` 字段**；PID 24572、启动 2026-10-22T17:08:16 | 见 `P1-E-live-probe.json` | 0 |

## 4. 在线生效情况（严格区分"新代码验证通过"与"在线已在跑新代码"）

- 在线实例（8080/PID 24572）实测 `/api/health` 返回 `code_sandbox/llm/redis/service/status/time`
  **六个键，没有 `build`** —— 仍是 P1-E 之前的代码。
- 本地磁盘源码指纹 `fc6c445467d7`（13 文件）含全部本会话修复。
- 重启后判据：`/api/health` 的 `build.source.restart_required == false` 且
  `build.started_at` 更新 → 才算"在线已在跑新代码"。

## 5. 边界自检

- 未重启服务、未触发付费研究、未 `git commit`/`push`、未改生产配置/凭据/DNS/代理；
- 在线探针为只读 GET（`/api/health` 公开接口），无登录态、无成本；
- 备份命令写进计划但**未执行**（执行属重启计划 §2，待授权）；
- 未删除或移动旧产物；测试全部临时目录隔离；日志隔离测试验证"不写生产 logs/"。

## 6. 本批后仍待办

- **受控重启**：按 `P1-E-restart-plan.md` 执行（待用户授权）；
- 重启后：账号 momang 端到端全面测试（历史任务找回 + 一轮范围内研究预算 +
  审计四件套检查 + 降级披露抽查）；
- 环境项：本机 DNS fake-IP（198.18.x）未解——在线检索会**如实降级**而非编造来源；
  `config.json` 的 `backup.model` 404 与 embedding 端点待用户决定。
