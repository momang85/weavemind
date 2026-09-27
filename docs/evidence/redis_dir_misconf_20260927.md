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
- 用户侧恢复方式：**在正常（非本会话）终端重启一次 Redis**（或 `stop.bat` + `start.bat`）。
  修好的启动参数会带上显式 `--dir`，并在启动后自检落盘；自检不通过会打印原因与建议。

## 5. 验证

| 项 | 结果 |
|---|---|
| `test_redis_acquisition`（新增 8 例） | **31 OK**（argv 带 `--dir`/正斜杠、无 `--dir` 时保持旧形态、数据根跟随、非 ASCII 退路、自检三个分支） |
| `test_deploy_manifest` / `test_startup_readiness` | 30 OK / **57 OK** |
| `test_p0` 相关 | `redis_start_argv` 与"start 不先杀别人的 Redis"用例 OK |
| 实机（只读） | `CONFIG SET stop-writes-on-bgsave-error no` 后 `PING/PONG`、`SET/GET/DEL` 正常；`GET /api/health` **HTTP 200**；`GET /api/status` 需登录（未伪造会话） |

## 6. 遗留

- 本机 Redis 需要**在正常终端重启一次**才算真正修好（本会话起不了 msys2 Redis）。
- `.tmp/` 下的诊断脚本（裸 RESP 客户端、目录实验、解封脚本、真库探针）**不进库**，
  需要复查时可重跑；其中"用裸 RESP 而不是 redis-py 连 MISCONF 实例"这条经验建议保留。
- Docker/compose 路径用的是容器内 Redis，不受本缺陷影响（不受 Windows msys2 限制）。
