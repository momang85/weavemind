# N4 场景实机复验：干净环境近似、断网、端口冲突、错误密钥、代码隔离提示

日期：2026-09-26。交付物：运行包 `weavemind-2026.09.26.4-win-x64.zip`
（sha256 `41429db2d1a79aa6b19d5528dd433ab6e248ab185d07df8a752c422c3be7ea89`，291,400,142 字节，
23,983 个文件；构建报告 `secrets: 0`、`dev_paths: 0`、包内验证含 `project_root_on_path: true`）。

本文件是"用户点名要跑的六项场景"的执行记录：场景 1/2（干净环境近似整链）、场景 4（断网）、
场景 5（端口冲突与 Redis 5）、场景 7（错误密钥）、场景 8（代码隔离提示），以及为它们补的
新人指导。**所有"未验"都写在文末，不靠措辞掩盖。**

> 版本说明（照实）：场景 1/2、4、5、7 的实跑记录在 `2026.09.26.3` 上取得；随后发现并修掉了
> §2 的第 10 条（Windows 工具输出解码），重建为 `2026.09.26.4`——该修复只影响"非 UTF-8 控制台
> 下读子进程输出"，不改变上述任何被验证的行为。交付前在 `.4` 上复跑了场景 1/2 整链与页面
> 载荷（`demo_available: true`、`code_execution` 存在），并额外验证了"同机两实例"互不干扰
> （见 §5 末）。

## 1. 结论速览

| 场景 | 结果 | 关键证据 |
|---|---|---|
| 1+2 干净环境近似（无系统 Python/Node/Docker） | **通过**（同一台机器的净化环境，非另装机器） | `Source: packaged runtime`、Python 3.11.9、依赖 14/14、16/16 服务、工作台 HTTP 200 @ 8080、退出码 0 |
| 3 中文与空格路径 | 通过 | 解压到 `…\织光 新人实机 20260926\解压目录4\` 后全链可跑 |
| 4 断网（进程级死代理）冷启动 | **通过** | 清空实例状态后仍 `复用已有下载包` 起 Redis、依赖来自包内、工作台 HTTP 200 |
| 5 端口冲突 + Redis 5 | **通过** | 8080 被占→本实例 8081；6379 上是 Redis 5→本实例 6380；占用方进程存活、`stop` 不动它 |
| 7 错误密钥（真机端点） | **通过** | 向导与页面均判 `鉴权失败`；`/api/config` 不回显密钥；诊断脱敏无泄漏 |
| 8 含代码任务的隔离提示 | **通过**（提示三处可见；真机付费任务未跑，见 §6） | 启动报告、`/api/auth/bootstrap` 的 `code_execution`、计划阶段执行前提示（单测） |

## 2. 本批修掉的真实缺陷（都是场景演练暴露的，不是"顺手改"）

1. **运行包不使用包内解释器**（场景 1 直接失败）
   `start.bat`/`stop.bat` 只探测系统 `python`/`py -3`/`python3`，包内 `runtime\python.exe` 从未被用到。
   净化环境实测：先落到系统 `py -3`（Python 3.14）——在开发机上"看起来能用"，在干净机器上
   停在 `[1/6] Python` 报错。修复：包内运行时优先（同样用 `WMPYOK` 标记验过才采用），
   系统 Python 只作回退；并打印 `Source: packaged runtime (...)`。
2. **包根不在 `sys.path`**（修完第 1 条后立刻暴露）
   带 `._pth` 的解释器是隔离模式，不会把脚本目录加进 `sys.path`：
   `python launcher.py` → `ModuleNotFoundError: No module named 'db_paths'`。
   修复：`._pth` 增加 `..`（运行包根目录）；**包内验证改为同时导入项目模块**
   （`db_paths, cli_text, task_intent`）并打印 `project_root_on_path`——此前只验第三方包，
   所以"验证全绿、双击即挂"。
3. **回环健康检查被代理接走**（场景 4 暴露）
   设了 `HTTP_PROXY` 后 `urllib` 把 `http://127.0.0.1:8080/api/health` 也送去代理：
   死代理下报"工作台：未响应"而服务其实在跑（企业网常态）。修复：就绪探测用
   `build_opener(ProxyHandler({}))` 显式不走代理，并把 `localhost,127.0.0.1,::1` 追加进
   `NO_PROXY`（不覆盖用户已有排除项）。
4. **默认端口被占就报错退出**（场景 5）
   修复：Web 默认端口被占→本实例让位到空闲端口并落盘（`runtime_ports.json`），
   `launcher.py url`／子进程／页面同一端口；Redis 端口被占或版本 <6（如 Redis 5）→
   本实例另起便携 Redis 于空闲端口，**不停用、不接管**对方进程；显式配置的端口不自动改，只如实报冲突。
5. **配置不完整时把新人丢在控制台**（场景 1 体验）
   此前 `awaiting_config` 直接返回且不启动服务，页面首启引导与内置演示根本没机会出现。
   修复：配置未完成也把工作台拉起来，引导在页面里继续；研究能力仍如实报"未就绪"。
6. **`isatty()` 在 Windows 不可靠**（自动化场景）
   重定向到 NUL 也报 True，导致非交互场景仍进入问答式引导并撞 EOF。修复：用
   `GetConsoleMode` 判真控制台。
7. **内置演示没随包**（场景 1 页面）
   `demo/` 不在构建清单里，无密钥的新人在页面看到"内置演示未随包提供"——而那是他唯一能看的内容。
   修复：`demo` 进 `REQUIRED_SOURCES`，并加断言。
8. **引导会清掉用户已有的高级配置**
   写盘基底用的是**模板**：一份不完整的 `config.json` 走完引导后，用户手工设过的
   `redis.port`/`system.*` 被静默清掉。修复：基底改为已有 `config.json`（可解析时），否则模板。
9. **含代码任务缺"执行前"提示**（场景 8）
   此前只有"修复轮被跳过"的事后日志。修复：计划含 `code_execution` 且隔离不可用时，
   **执行前**发一条提示（不降级、不静默跳过、不给"关闭隔离"这条出路），并让页面在
   `/api/auth/bootstrap` 里拿到 `code_execution` 状态，在首启引导里常驻说明。
10. **子进程输出按 UTF-8 解码，中文系统直接抛栈**（源码方式实测）
    `launcher.py deps --fix` 在中文 Windows 上打出 4 段 `UnicodeDecodeError`
    （`subprocess` 读线程解码 `tasklist`/`sc` 的 GBK 输出），且 `_is_alive` 的 PID 判断随之
    失效——对新人是"一启动就报错"的观感。修复：所有 `text=True` 的子进程调用补
    `errors="replace"`（`launcher.py` 3 处、`dep_check.py` 4 处），并加守卫测试
    （扫描源码：出现 `text=True` 就必须在附近有 `errors=`）。

## 3. 场景 1/2 实跑记录（净化环境）

命令形态（净化环境：`env -i` + 仅 `C:\Windows\system32;C:\Windows` 的 PATH，
无系统 Python/Node/Docker；`< /dev/null` 模拟非交互）：

```
cd <解压目录>\weavemind-2026.09.26.3-win-x64
cmd /c start.bat
```

输出要点（`final_acceptance_1.log`）：

```
  [1/6] Python
       Using: python
       Source: packaged runtime (...\runtime\python.exe)
  [OK] 运行包：package:2026.09.26.3　Python 3.11.9
  [OK] 依赖包 14/14 就绪
  [OK] 已启动便携版 Redis（pid=…，绑定 127.0.0.1 -::1，…\.weavemind\redis\portable\…）
  [OK] 前端产物已就绪（frontend/dist，免 Node）
  [OK] 依赖：已检查并补齐缺失项
  [!!] 配置：模型/密钥待填写：打开工作台按首启引导完成（演示可直接看）
  [OK] 启动校验：16/16 服务存活
  [OK] 工作台：HTTP 200 @ 8080
  [OK] 研究能力：就绪（Redis 8、编排器与 5 项必需 Worker 心跳新鲜）
  [--] 代码执行：容器隔离不可用（…）——涉及代码执行的步骤会被拒绝；检索/结构化数据/图表/报告/交付不受影响。
  URL: http://localhost:8080
  Ready.  http://localhost:8080        （退出码 0）
```

页面侧（同一实例，HTTP 直查）：

- `GET /api/auth/bootstrap` → `{"setup_required": true, "config_complete": false,
  "demo_available": true, "code_execution": {"isolation_ready": false,
  "isolation_required": true, "execution_available": false, "note": "要求容器隔离但当前不可用…"}}`
- `GET /api/demo/brief` → `available: true`，`title: 内置演示简报（合成数据）`，
  `note: 内置演示：非本次实时生成，不代表真实研究结果；不写入任何研究统计`
- `GET /` → 200（已构建前端直接可服务，未起 Vite/Node）

**未覆盖**：另装一台真正干净的 Windows（无开发环境、无本项目历史）。当前证据是
"同一台机器的净化环境 + 中文与空格路径"，因此仍标"干净机器未验"。

## 4. 场景 4 断网演练（进程级）

先证明屏蔽有效（对照组）：在同样的环境下用包内解释器请求 `https://pypi.org/simple/` →
`URLError [WinError 10061] 目标计算机积极拒绝`（`HTTP_PROXY/HTTPS_PROXY/ALL_PROXY=http://127.0.0.1:9`）。

然后把实例状态清空（删 `.weavemind` 下除 `downloads/` 以外的全部内容与 `logs/`，模拟"刚解压"），
再以同一死代理环境执行 `start.bat`（`final_scenario4_offline.log`）：

```
  [OK] 运行包：package:2026.09.26.2　Python 3.11.9
      正在获取便携版 Redis（约 14MB；…）…
      已获取：复用已有下载包（sha256 4e8f2f956ed92fea…）      ← 本地缓存，未联网
  [OK] 依赖：已检查并补齐缺失项
  [OK] 依赖包 14/14 就绪
  [OK] 启动校验：16/16 服务存活
  [OK] 工作台：HTTP 200 @ 8080
  [OK] 研究能力：就绪（Redis 8、…）
```

即：**包内资源足以冷启动**（Python、依赖、Redis、前端都在包里），断网不阻塞首次运行；
本轮的修复 3（回环不走代理）是工作台能报 200 的前提。

**未覆盖**：真机物理断网（拔网线/禁网卡）。当前是进程级代理屏蔽 + 对照组证明。

## 5. 场景 5 端口冲突与 Redis 5

占用方是两个**独立进程**（不是本项目）：`port_holder.py` 占 127.0.0.1:8080（只监听、回一句占用说明），
`redis5_stub.py` 占 127.0.0.1:6379（**协议桩**：`PING → +PONG`、`INFO server → redis_version:5.0.14`、
`HELLO → -ERR unknown command`）。随后正常执行 `start.bat`（`final_scenario5_ports.log`）：

```
[INFO] 端口 8080 已被其它程序占用，本实例改用 8081（不接管对方进程）
  [--] 端口 6379 上的 Redis 主版本为 5，低于本项目要求的 6：本实例改用 127.0.0.1:6380 的自带 Redis（不接管该进程）
  [OK] 启动校验：16/16 服务存活
  [OK] 工作台：HTTP 200 @ 8081
  [OK] 研究能力：就绪（Redis 8、编排器与 5 项必需 Worker 心跳新鲜）
  URL: http://localhost:8081
```

核对：

| 检查 | 结果 |
|---|---|
| 新进程执行 `launcher.py url` | `http://localhost:8081`（与页面、子进程一致） |
| `.weavimind/runtime_ports.json` | `{"web": 8081, "preferred": 8080, "redis": 6380}` |
| 监听表 | 8080=占用方(9812)、8081=本实例 webui、6379=占用方(11836)、6380=本实例便携 Redis |
| `stop` 之后 | 本实例 16 服务与 6380 停止；**9812 与 11836 仍存活**（未误杀、未接管） |

**未覆盖**：真实安装的 Redis 5。当前是协议桩（只实现探测/握手用到的命令）。

**同机两实例（.4 上额外验证）**：开发实例占着 8080 与 6379（Redis 8）时再启动运行包实例，
它把 Web 让到 8081、并**复用**了那个兼容的 Redis（`Redis 已运行（localhost:6379，版本 8）`）；
随后对运行包实例执行 `stop`：只停它自己的 16 个服务，开发实例仍 `health=200`、Redis 进程存活。
即：两个实例同机共存不互相误杀。（共用同一个 Redis 会共享消息总线，属已知取舍，已写进指南。）

## 6. 场景 7 错误密钥（真机端点，零费用）

端点取本项目正在用的 `https://tokenrhythm.studio/v1`（模型 `deepseek-flash`），密钥用
**明显无效的占位串**（`wrong-key-not-a-real-credential-0123456789`），请求必然被拒 → 无费用。

**向导路径**（`WM_WIZARD_ANSWERS` 驱动，`final_scenario7_wrongkey.log`）：

```
正在测试 https://tokenrhythm.studio/v1 …
  ! 鉴权失败（key 可能无效或已过期）
回车=重新填写 / 输入 n = 仍然保存[y]: n
已写入 config.json：base_url=https://tokenrhythm.studio/v1 model=deepseek-flash api_key=wron…6789
```

即：错误被**分类为鉴权失败**（不是"网络不可达"），给出可读原因，并允许"重新填写/仍然保存"，
写盘时密钥被掩码打印。

**页面路径**（`final_scenario7_page.log`，走 HTTP 接口）：

| 步骤 | 结果 |
|---|---|
| `POST /api/setup-admin`（首个管理员，无默认密码，随机 14 位） | 200 `{"user":"drilladmin","role":"admin"}` |
| `POST /api/login` | 200 + 会话 cookie |
| `POST /api/config/test`（用户点击触发） | 200 `{"ok": false, "reason": "unauthorized", "latency_ms": 858}`，detail 只含 base/model |
| 未登录时 `POST /api/config/test` | 401（公开面收敛） |
| `GET /api/config` | 200，`api_key: ""`（不回显） |

**诊断脱敏**：`runtime\python.exe launcher.py diagnostics diag.txt` → 11,285 字节，
不含该密钥、不含 `Authorization`/`Bearer `/`api_key=`，文件头写明"本机生成，未上传"。

**费用**：本轮共 2 次被拒请求（向导 1 次 + 页面 1 次），均无计费；**未跑任何研究任务**。

## 7. 场景 8 代码隔离提示（三处可见）

1. **启动报告**（`final_acceptance_1.log`）：`[--] 代码执行：容器隔离不可用（docker 不可用…）——
   涉及代码执行的步骤会被拒绝；检索/结构化数据/图表/报告/交付不受影响。` 并列出两条真实出路，
   明确"不要用关闭隔离来解决"。
2. **页面（提交前）**：`/api/auth/bootstrap` 增加 `code_execution`（布尔 + 既有说明文案，
   无路径/凭据），首启引导常驻一行 `data-code-execution-notice`："本机没有可用的容器隔离——
   含生成并运行代码的步骤会被拒绝执行，也不会退到本机运行；公司研究、图表、报告与交付下载不受影响。
   请不要用关闭隔离来解决。"
3. **任务执行前**（编排器）：计划含 `code_execution` 且隔离不可用时，在执行步骤之前发提示
   （同一任务只发一次，不降级、不静默跳过）。单测：
   `test_orchestrator_v2.TestDockerFreeResearchPath::test_sandbox_notice_is_emitted_before_code_steps_run`
   等 4 例。

**未覆盖**：真机跑一个含 `code_execution` 的付费任务去看这条提示。按指令"本小批不为证明一键
新增付费研究"，未跑；离线门禁与页面可见性已验。

## 8. 新人指导（本批一并交付）

- 包内 `运行说明.txt`（构建器生成，UTF-8 BOM）：双击哪个文件、下一步做什么、出问题看哪里。
- `docs/新人上手指南.md`：该走哪条路、首次启动会看到什么（逐行对照）、
  出错→下一步对照表、**验证到什么程度（含未验项）**。
- `docs/部署指南.md`：方式 A 拆成"运行包 / 源码"两节，清掉"自动启动 Docker Desktop""固定映射 6379"
  等过时口径，补端口让位行为。
- `README.md` / `README.en.md`：快速开始表加运行包一行，并如实标注验证范围。

## 9. 测试与回归

- 新增/调整的行为测试：`test_startup_readiness`（端口让位、Redis 让位/不停他人、回环不走代理、
  `NO_PROXY` 追加、控制台判定、控制器不再把配置未完成者丢在控制台）共 12 例；
  `test_deploy_manifest`（`._pth` 含包根 + 包内验证导入项目模块、运行说明写入、`demo/` 必进包）3 例；
  `test_orchestrator_v2`（执行前沙箱提示）4 例；`test_frontend_guards`（页面隔离提示）1 例；
  `test_auth_audit`（bootstrap 的 `code_execution` 形状与不泄漏）扩展 1 例；
  `test_setup_wizard`（引导保留已有高级配置、基底回退）2 例。
- 本机 CI 平价跑法：ci.yml 列出的 46 个测试文件逐个跑，全部 OK
  （本地 ci.yml 有一行未提交改动指向不存在的 `test_question_assessment.py`，不属本批，未提交）。

## 10. 未验与已知边界（照实）

| 项 | 状态 | 说明 |
|---|---|---|
| 另装一台真正干净的 Windows | **未验** | 需要一台没有开发环境的机器；当前是净化环境近似 |
| 真机物理断网 | **未验** | 当前是进程级代理屏蔽（含对照组证明屏蔽有效） |
| 真实 Redis 5 安装 | **未验** | 当前是协议桩 |
| 含 `code_execution` 的付费任务真机提示 | **未验** | 需一次真实模型调用；离线门禁与页面可见性已验 |
| 浏览器里渲染首启引导（含新提示行） | **未验** | 需要登录态；本轮以 HTTP 接口核对字段与文案 |
| 真人评分《研究员评分表_F3_20260920》 | **未填** | 仍待人工 |
| Docker 路径中文字体 | 已知缺口 | Windows 用系统字体；Docker 仍缺字体（`WEAVEMIND_PDF_FONT` 可指定） |
