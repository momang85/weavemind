# N0 收尾 + N1 首批：一个实例、明确状态、可恢复失败（09-26）

**指令**：`docs/新人一键启动与首次研究体验_20260925.md` §2（N0）、§3（N1）。
**基线**：`bde71dd` + 无沙箱研究路径小批（`f884db3`，已推送、CI 绿）。

## 0. N0 收尾：无沙箱研究路径小批

`f884db3` 已提交并推送（CI success），内容见
[no_docker_research_path_20260925.md](no_docker_research_path_20260925.md)：
图表不再落到 `code_execution`、交付守门按目标判、隔离不可用不进修复轮、
依赖自检与启动校验打印沙箱状态。本轮在其上补两处纠偏（见 §1）。

## 1. N0 纠偏（本轮）

| # | 问题 | 处理 |
|---|---|---|
| 1 | `_goal_wants_code` 用宽关键词（"编写""代码"）判"要不要交付程序"，把「编写洋河研究报告」「研究股票代码 002304」判成要交付代码 | 判定移入既有意图模块 `task_intent.wants_code_deliverable`：按**交付物名词**（脚本/程序/网页/小游戏/爬虫/可运行/命令行/单文件…）或"写/实现 + 交付物名词"构式判；名词里不含裸"代码"，并显式剔除"股票代码/证券代码/基金代码/科目代码/代码为/代码："等标识符搭配。交付守门改为调用它 |
| 2 | 依赖自检与修复轮提示把 `CODE_EXECUTION_SANDBOX=restricted` 列为新人出路 | 两处都删掉该推荐，只留"恢复隔离（装 Docker 并构建沙箱镜像）"与"让任务不生成代码步骤"；并写明"不要用关闭隔离来解决（restricted/none 只能由操作者显式选择）"。`test_startup_readiness` 相应改为**断言不得出现** restricted 出路 |

反例/正例（用例锁定）：

| 目标 | 判定 |
|---|---|
| 编写洋河股份 2023 与 2024 年度研究报告 | 不要代码 |
| 研究股票代码 002304 的经营表现，输出分析报告 | 不要代码 |
| 研究基金代码 510300 的净值变化 | 不要代码 |
| 就代码质量问题写一段说明，附来源标注 | 不要代码 |
| 用 Python 编写一个可直接运行的命令行小程序 | 要代码 |
| 写一个单文件 HTML 小游戏 | 要代码 |
| 实现一个爬虫脚本并跑通 | 要代码 |

## 2. N1 首批：四项行为修复

| # | 缺陷 | 修复 |
|---|---|---|
| 1 | **重复启动停旧任务**：`start_services` 无条件 `stop_services`，第二次双击会杀掉进行中的研究任务 | 新增 `instance_state()`（PID 文件 + **进程归属校验** `_pid_owns_project`，避免 PID 复用误认）；已在本实例运行则**复用**：不停止、不重启、不重复 spawn，打印地址与三层状态后返回 `{"reused": True, ...}`。显式重启仍走原路径（`WM_FORCE_RESTART=1` 或 `launcher.py restart`） |
| 2 | **spawn 失败漏计**：spawn 失败只打日志、不进 PID 文件，`verify_services` 只统计启动成功的那些（少启动一个反而显示"15/15 存活"） | PID 文件新增 `failed: [name]`；`verify_services` 把未启动项计入 `total` 与 `down`（标注"未启动"），返回体新增 `never_started`；守护重启成功后从该清单移除 |
| 3 | **存活冒充研究就绪**：`/api/health` 或"N/N 进程存活"被当作可研究 | 新增 `readiness_report()` / `print_readiness()`：① 工作台＝**实际端口**上 `/api/health` 响应；② 研究能力＝Redis 可达且 ≥6 **且** 注册表里 5 项必需能力（web_search/web_fetch/content_summary/report_generator/package）心跳新鲜（≤180s）**且** 编排器进程存活；③ 代码隔离（可选能力，单独一行）。任一层不成立即打印"研究能力：未就绪——原因 + 下一步"，**不得宣称可研究**。`launcher.py status` 与启动流程都打印这三层 |
| 4 | **固定 8080**：`start.bat` 硬写 `http://localhost:8080`（提示与打开浏览器都用它） | 端口唯一来源 `web_port()`（`WEB_PORT` → `config.json` 的 `web.port` → 8080）与 `web_url()`；新增 `launcher.py url` 与 `launcher.py readiness` 子命令，`start.bat` 用 `for /f` 取实际地址再打开 |

## 3. 验证

```
REDIS_PORT=6399 python -m unittest test_startup_readiness     # 20 OK（新增 10 例）
REDIS_PORT=6399 python -m unittest test_orchestrator_v2 test_setup_wizard test_deploy_manifest  # 144 OK
REDIS_PORT=6399 python -m unittest test_p0                    # 407 OK（含 CI 评测闸门）
REDIS_PORT=6399 python -m unittest test_delivery_chain        # 343 OK
```

新增用例（行为级，非源码字符串）：

- **复用**：第二次 `start_services` 不调用 `stop_services`、不 spawn，返回 `reused=True`；
  `WM_FORCE_RESTART=1` 时仍停旧起新；PID 被系统回收给别的进程（归属校验失败）不算本实例。
- **spawn 失败记账**：`failed: [bad-svc]` 计入 `total`/`down` 且打印"未启动"；
  `start_services` 把失败写进 PID 文件后可读回。
- **就绪判定**：三层齐备 → ready；工作台 200 但编排器不在 → **未就绪**且输出不含"可研究"；
  缺 `report_generator` 能力 → 未就绪；心跳过期（>180s）→ 未就绪。
- **非默认端口**：`WEB_PORT=8123` 时 `web_url()`、就绪探测 URL 与输出都是 8123。

实机（本机，无 Docker）：

```
  [OK] 启动校验：16/16 服务存活
  [OK] 工作台：HTTP 200 @ 8080
  [OK] 研究能力：就绪（Redis 8、编排器与 5 项必需 Worker 心跳新鲜）
  [--] 代码执行：容器隔离不可用（docker 不可用（CLI 缺失或守护进程未响应））——涉及代码执行的步骤会被拒绝；检索/结构化数据/图表/报告/交付不受影响。
```

第二次 `python launcher.py start`（实例已在运行）：

```
  [OK] 已在运行（16 个服务）：http://localhost:8080——已复用本实例，不重启、不打断进行中的任务
```

前后 PID 逐项相同（`critic/webui/orchestrator/...` 16 项一致），`failed: []`。

## 4. 已知限制 / 未验（如实列出）

- **归属校验依赖进程扫描**：`_pid_owns_project` 用 `_scan_residual_processes()`（优先 psutil，
  无 psutil 时回退 tasklist/wmic）。本机实测回退路径在中文控制台下会因 GBK 字节解码失败而
  返回空列表（本次实测看到 `UnicodeDecodeError` 噪音），届时"已在运行"会被判成未运行并
  重新拉起一套进程。**未修**，作为 N1 残余记录。
- 端口探测在服务刚退出时可能出现"连接超时"而非"拒绝"，本机实测两者都出现过；就绪判定
  只按"能否拿到 2xx"处理，未区分这两种失败语义。
- `web.port` 目前只在 `config.json` 存在该段时生效；`config.example.json` 未列该段（端口仍以
  `WEB_PORT` 为主）。
- N1 其余条目（统一配置形成时机、安装状态失效与断点恢复、失败恢复动作与脱敏诊断导出、
  暖启动不重复探测收费模型）尚未实现；N2 便携包、N3 页面引导、N4 验收矩阵未开始。
- 本轮未新增付费模型调用、未改模型/权限/模板/配置、未自动提交任何样例任务。
