# N1 余项：统一启动控制器、单实例锁、断点恢复、脱敏诊断（09-26）

**指令**：`docs/新人一键启动与首次研究体验_20260925.md` §3（N1 其余条目）。
**前置**：N1 首批四项见 [n0_n1_single_start_and_readiness_20260926.md](n0_n1_single_start_and_readiness_20260926.md)
（提交 `6715543`）。

## 1. 一个启动控制器（`launcher.py up`）

`start.bat` / `start.sh` 收敛为**薄入口**：探测解释器 → `launcher.py up` → 用
`launcher.py url` 取实际地址打开浏览器。整条链（配置 / 依赖 / Redis / 服务 / 就绪）
只在控制器里，不再三处并行维护。

状态取值（对外只显示当前步骤与下一步；技术细节在日志里）：
`checking_runtime → preparing_deps → awaiting_config → starting_services →
waiting_ready → research_ready`／`limited_experience`（能看不能研究）／`failed`。

`start.bat` 同时**移除了 Docker Desktop 自动启动**（架构指令 §3：正常启动不主动启动其他软件）；
Redis 由依赖自检按 5.1 的顺序处理（系统已装 → 离线复用 → 多源下载 → 便携版）。

## 2. 统一有效配置（在依赖/Redis 检查之前）

`effective_config()`：`config.json` → 环境变量（`_apply_env`）→ 端口 / Redis 目标 /
数据库 / 配置完整性 / 前端产物 / 运行身份，**一次算清**再进入检查。
此前 `_apply_env` 只在 `start_services()` 里跑，而脚本层的 Redis 探测已按默认 6379 探过——
把 `redis.port` 改成 6390 的用户会被判"本机没有 Redis"。

端口唯一来源 `web_port()`：`WEB_PORT` → `config.json` 的 `web.port` → 8080；URL、就绪探测、
提示与打开浏览器共用它。

## 3. 单实例锁与断点恢复

- `acquire_instance_lock()` / `release_instance_lock()`：原子创建 `.weavimind/instance.lock`
  （pid + 状态）；第二次双击（或启动中再次双击）**复用同一实例并显示同一进度**；
  持有者已死或锁过期则接管（不会永久锁死）；只删除本进程持有的锁。
- `startup_state.json` 记 `identity`（包清单版本 → VERSION → git 短哈希）+ 每步结果：
  身份未变则复用已验证结论（**暖启动不重装、不重复联网**）；包版本变化即整体失效重检。
- 已有配置**不做**消耗额度的连通性探针（`--probe` 只保留给用户显式调用）。

## 4. 失败只给一个可执行动作 + 脱敏诊断

失败态（`awaiting_config` / `limited_experience` / `failed`）各自只打印一条下一步：
完成模型配置 / 处理未就绪项后再提交研究 / 看 logs 并生成诊断。

`launcher.py diagnostics [out.txt]`：版本、运行身份、三层就绪、配置状态、Redis 目标、
启动步骤、各日志尾部；**正则脱敏**（Authorization 整行、Bearer、api_key/token/secret、sk-*），
只读本仓库 `logs/`，**不自动上传**。用例断言诊断里不得出现假密钥。

## 5. 验证

```
REDIS_PORT=6399 python -m unittest test_startup_readiness   # 33 OK（新增 10 例控制器用例）
REDIS_PORT=6399 python -m unittest test_p0                  # 407 OK（含评测闸门）
REDIS_PORT=6399 python -m unittest test_setup_wizard        # 36 OK（薄入口守卫已改写）
REDIS_PORT=6399 python -m unittest test_sandbox_isolation   # 24 OK
REDIS_PORT=6399 python -m unittest test_orchestrator_v2 test_delivery_chain  # 全绿
```

新增/改写的用例（行为级）：有效配置先于探测形成（含非默认端口 8123/6390）；单实例锁
（第二次报占用、持有者已死可接管、只删自己的锁）；身份变化让已完成步骤失效；
脱敏与诊断（假密钥不得出现、标注未上传）；控制器四态（`awaiting_config` 不启动服务、
`limited_experience` 提示不要提交研究、`research_ready` 启动并给出 URL、锁被占用时复用）；
暖启动不重跑依赖检查。启动脚本守卫改为：薄入口、不含 `dep_check.py --fix` / `setup_wizard.py` /
`--probe`、不含 Docker 自动启动、失败给诊断入口。

实机（本机，无 Docker）：

```
  [OK] 运行包：git:6715543　Python 3.14.3
  [OK] 依赖：上轮已验证就绪（未重复联网安装）
  [OK] 配置：deepseek-flash @ https://tokenrhythm.studio/v1
  [OK] 工作台：HTTP 200 @ 8080
  [OK] 研究能力：就绪（Redis 8、编排器与 5 项必需 Worker 心跳新鲜）
  [--] 代码执行：容器隔离不可用（…）
  [OK] 可研究：工作台可访问、研究能力就绪
  URL: http://localhost:8080        （exit=0）
```

## 6. 顺带修掉的测试隔离缺陷

排查"测试端口上留下真实 Redis"时定位到：`test_startup_readiness` 的控制器用例没 mock
依赖步骤，于是真跑了 `dep_check.ensure_all` → 在 `REDIS_PORT=6399` 上拉起一个便携 Redis
并留在后台；它会污染后续用例（`test_redis_reachable_false_on_closed_port` 因此报红）。
已在该用例里 mock `_run_dependency_check`，并用临时探针逐个类/方法确认两个测试文件
跑完不再留下 Redis。同类风险的两处旧用例（`test_p0` 与 `test_sandbox_isolation` 里直接
调用真实 `start_services`）改为先固定 `instance_state`，使其继续覆盖"停旧起新"路径。

## 7. 未验 / 边界

- **干净环境未验**：以上都是开发机（已装 Python/依赖、无 Docker）实测；"解压即用、
  无系统 Python/Node"要等 N2 便携包与 N4 干净环境验收。
- 端口"连接超时 vs 拒绝"未区分语义；无 psutil 时归属校验的回退路径在中文控制台会因
  GBK 解码失败返回空（可能把"已在运行"误判成未运行）——仍记录为残余。
- 前端缺失时仍走后端回退页（控制器未把它标成"恢复页"），属 N3 范围。
- 网络/依赖失败目前给"重试本步骤 / 诊断导出"两类动作，尚未做"选择离线包"的交互。
