# 运行环境缺陷：便携 Redis 落盘目录错导致全部写命令被拒（2026-09-27）

类别：**运行环境缺陷 + 代码根因修复**（非批次任务；由 H1 的双进程预占复验顺带查实）。
影响面：**所有**用便携 Redis 跑本项目的人——16 个服务都在跑，但**一个任务都提交不了**。

## 1. 现象

- `/api/status` 一类读接口正常；提交任务时编排器收执超时/失败。
- 直接探测：`PING` 返回 `MISCONF Redis is configured to save RDB snapshots, but it's
  currently unable to persist to disk…`——**连 PING 都被拒**。
- `SET`/`DEL`/`HINCRBY` 全部被拒；`GET` 可用（读不受影响）。

## 2. 根因（两层）

1. **`dir` 无值可用**：`CONFIG GET dir` = `/portable/Redis-8.10.1-Windows-x64-msys2`。
   便携版是 **msys2** 构建，启动时没有给 `--dir`，于是它拿**进程 CWD** 当 `dir`
   （`dep_check` 用 `cwd=redis_exe.parent` 启动）；msys2 把该 CWD 报成上面这种 POSIX 形态，
   在 Windows 上 `bgsave` **永远失败**（`rdb_last_bgsave_status:err`）。
2. **失败后连写都被禁**：`stop-writes-on-bgsave-error=yes`（默认）在首次 bgsave 失败后
   进入 MISCONF，**拒掉所有可能修改数据集的命令**。于是"服务都在、任务全提交不了"。

**为什么不能就地修**：`dir` 在 Redis 8 是 **protected config**，运行期改不了——
实测 `CONFIG SET dir …` → `ERR CONFIG SET failed … can't set protected config`；
`BGSAVE` 也被 MISCONF 拒。**只能在启动参数里给对**。

另外：redis-py 6 连接时会发 `CLIENT SETINFO`（修改服务器状态），因此在 MISCONF 状态下
**用 redis-py 连 PING 都失败**；诊断必须走裸 RESP（本目录脚本即为裸 RESP）。

## 3. 代码修复（durable）

`dep_check.py`：

- `_redis_data_dir()`：给出显式落盘目录（数据根下 `redis/`）。**非 ASCII 路径退到
  `%LOCALAPPDATA%/WeaveMind/redis`**——msys2 程序把命令行参数按 POSIX 路径处理，
  中文目录（很常见）会在转换中出错；RDB 只是机器本地运行态，不为此赌。
- `_redis_start_argv(exe, port, data_dir)`：显式追加 `--dir <正斜杠绝对路径>`
  与 `--dbfilename dump.rdb`；不传 `data_dir` 时保持旧参数形态（便于单独诊断）。
- `verify_redis_persistence(port, expect_dir)`：**启动后自检** `CONFIG GET dir` 是否符合
  预期、并触发一次 `BGSAVE` 读 `rdb_last_bgsave_status`；不通过就**大声报出来**，
  并给出可执行建议（把 `WEAVEMIND_DATA_DIR` 指到纯 ASCII 目录后重启）。
  `ensure_redis` 的两条启动分支都会调用它，结果进 `persistence_ok`/`persistence_detail`，
  并在控制台打印警告——把"用户提交任务失败时才发现的静默故障"前移到启动时刻。

## 4. 本机运行实例的处置（照实）

**只做了应急解封，没有真修**（本会话做不到）：

- 已执行 `CONFIG SET stop-writes-on-bgsave-error no` → `PING` 恢复 `PONG`，
  `SET`/`GET`/`DEL` 正常，应用可再次接受任务。
- **持久化仍是坏的**：`rdb_last_bgsave_status:err`、`dir` 仍为 `/portable/…`
  （protected，改不了）。**重启 Redis 之前，Redis 里的数据不会落盘**；崩溃/重启会丢
  自上次成功保存（`rdb_last_save_time` 对应的 2026-09-27 17:07）之后的全部内容
  （队列、快照、账本；任务台账与产物在 SQLite/工作区，不受影响）。
- **为什么不在本会话重启**：新的 msys2 Redis 进程在本会话**起不来**——
  `fatal error - NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0S5-…): 0xC0000022`（拒绝访问），
  且与 `--dir` 指哪儿无关（中文/ASCII 路径都试过）。**先停再起**会让应用彻底没有 Redis，
  风险大于收益，因此没停。
- 用户侧恢复方式：重启一次 Redis 即可（`stop.bat` + `start.bat`）。**注意**：本节结论已被
  §7 修订——只修 `dir` 不够，重启后仍需"不存快照 + 不锁写"，见 §7。

## 5. 验证

| 项 | 结果 |
|---|---|
| `test_redis_acquisition`（新增 8 例） | **31 OK**（argv 带 `--dir`/正斜杠、无 `--dir` 时保持旧形态、数据根跟随、非 ASCII 退路、自检三个分支） |
| `test_deploy_manifest` / `test_startup_readiness` | 30 OK / **57 OK** |
| `test_p0` 相关 | `redis_start_argv` 与"start 不先杀别人的 Redis"用例 OK |
| 实机（只读） | `CONFIG SET stop-writes-on-bgsave-error no` 后 `PING/PONG`、`SET/GET/DEL` 正常；`GET /api/health` **HTTP 200**；`GET /api/status` 需登录（未伪造会话） |

## 7. 修订（同日，用户重启后复验）：**只修 `dir` 不够**，便携版改为按设计不存快照

用户按要求重启 Redis 后复验，结论推翻了 §3/§4 的"给对 `dir` 即可"：

**`dir` 的修复确实生效了**——`CONFIG GET dir` 已变为
`/cygdrive/c/Users/ding0/AppData/Local/WeaveMind/redis`（正是我加的 ASCII 退路），
但**`BGSAVE` 仍然失败**。`logs/redis.log` 给出两层真因：

```
-1914266367 [main] redis-server 1653 dofork: child -1 - forked process 28940 died
  unexpectedly, retry 0, exit code 0xC0000142, errno 11
1653:M # Can't save in background: fork: Resource temporarily unavailable
...
987:C # Failed opening the temp RDB file temp-987.rdb (in server root dir
  /cygdrive/c/Users/ding0/AppData/Local/WeaveMind/redis) for saving: Permission denied
986:M # Background saving error
```

即本机 **msys2 便携版的 RDB 保存不可靠**：后台保存的 fork 子进程要么
`0xC0000142`（DLL 初始化失败 / fork 仿真失败），要么在 `dir` 里建临时 RDB 文件被拒。
只要 `stop-writes-on-bgsave-error=yes`，这个失败就会把**所有写命令**锁死。
换句话说：**只要还在期待它写 RDB，这个问题就会以不同形式复发**。

**因此修订为**（提交 `e282090`）：

- 便携版启动参数加 `--save ""`（不存快照）、`--appendonly no`、
  `--stop-writes-on-bgsave-error no`（**保存失败永不锁写入**）；`--dir` 保留
  （将来若启用快照，目录也是对的）。
- **系统/外部 Redis 不加**这些开关——那条路径的持久化归用户自己管。
- 自检由"验 bgsave"改为 **`verify_redis_writable`**（`SET`/`GET`/`DEL` 探针 +
  如实报告持久化模式）：便携版按设计不存快照，再拿 bgsave 判健康只会误报；
  真正要守住的是"写命令当下可用、且不会被保存失败锁死"。
- 便携 Redis 的定位写进注释与启动提示：**只承担总线/队列/缓存，持久事实在 SQLite 与工作区**；
  需要持久化请用系统/外部 Redis。

**本机运行实例已就地稳定**：`CONFIG SET save ""`（周期性失败保存停止，日志里每 6 秒一次的
`Can't save in background` 不再出现）+ `stop-writes-on-bgsave-error no`；
`PING/SET/GET/DEL` 正常，`DBSIZE` 正常。重启后这些也会由修好的启动参数自动带上。

**代价（明确写清）**：便携 Redis 现在**不落盘**。应用重启/机器重启会丢 Redis 里的内容
（队列、任务快照、健康快照、搜索账本、通知去重标记）——这些本来就是可重建的运行态
（任务台账/产物在 SQLite 与工作区）；通知去重标记丢失最多导致同一条通知重复一次。
此前"以为有 RDB"的状态是**假的持久化**，比明确不持久更危险。

**顺带实测**：重启后的新 Redis 里唯一的键是 `orchestrator:owner`——H3a 的**实例归属认领
在真实环境里生效**（本机验证的第一个 C3 机制）。

## 8. 验证（修订后）

| 项 | 结果 |
|---|---|
| `test_redis_acquisition`（含新增"便携版必须关快照与锁写""写自检通过/被拒"） | **31 OK** |
| `test_deploy_manifest` / `test_startup_readiness` / `test_p0` | 30 / **57** / **412** OK |
| 实机（只读 + 一次 SET/GET/DEL 探针） | `PING→PONG`；`save=""`；`stop-writes=no`；写正常 |
