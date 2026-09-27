# H1 剩余截止落到实际请求（2026-09-27，Harness 接续）

批次：**H1**（指令 `docs/Harness接续执行指令_20260927.md` §2-H1）。承接
`docs/evidence/c0_2_root_search_budget_20260927.md` §4 明确留存的三个缺口。默认根任务
仍是 **6 次 provider 调用 / 60 秒**，未放宽。

## 1. 缺口 → 修法（逐条）

| # | C0-2 §4 留存的缺口 | 修前 | 修后 |
|---|---|---|---|
| 1 | Bing 固定 12/15 秒不服从剩余时间 | `adapters/text_search._fetch_bing_html` 写死 `timeout=12`；`worker_base._search_bing` 写死 `timeout=15` | 两者都取 `provider_timeout("bing", wait)` = `min(上限, 剩余时间)`，并**按块读取、块间查同一个截止线** |
| 2 | ddgs `max(3.0, …)` 最小值越界 | `text_search._search_ddg` 用 `max(3.0, timeout)`；`worker_base._invoke` 用 `max(3.0, min(wait, 8.0))` | 一律 `provider_timeout("ddgs", wait)`，**不加任何下限抬升** |
| 3 | 同步 SDK 无法取消 → "超时返回后丢弃"不算真正停止 | 只能事后记账（`overrun_calls`） | ① 请求自己的超时 ≤ 剩余时间（自带的可终止边界）；② 剩余低于可行下限时**一个请求都不发**；③ 响应体按块读，到点停止读取 |

**受控的可终止边界（三类提供方分别处理）**：

- **Bing（urllib，可切）**：`read_with_deadline(resp, deadline)` 在**块间**检查墙钟——
  socket timeout 只管单次操作，慢速分块响应可以每块都小于它、整体远超截止。到点抛
  `TimeoutError`，由执行器按超时记账。因为可以在块间切断，**不抬高它的可行下限**。
- **ddgs（同步 SDK，不可切）**：只能把 `timeout` 压到剩余时间并拒绝过短预算——
  `PROVIDER_MIN_WAIT["ddgs"] = 1.0`，剩余低于它就不发请求。
- **一律不新起线程**：没有"超时返回后继续出网"的后台线程。

**拒绝出发不消耗额度**：`run_search` 在 `budget.take()` **之前**判可行下限，因此被拒绝的
请求既不发出去、也不占用调用次数（`test_refuses_to_issue_below_provider_floor` 断言
`budget.used == 0`）。拒绝结果如实记 `refused_calls` 并归 `status="timeout"`
（不是 `ok/no_results`，也不是 `parse_error`）。

## 2. 代码位置

- `adapters/search_runner.py`：新增 `MIN_VIABLE_CALL_SECONDS` / `PROVIDER_TIMEOUT_CAPS` /
  `PROVIDER_MIN_WAIT` / `provider_timeout()` / `provider_min_wait()` / `read_with_deadline()`；
  `run_search` 加可行下限闸门；`SearchOutcome` 加 `refused_calls`（进 `as_dict`）；
  状态机加 `refused_calls → timeout` 分支。
- `adapters/text_search.py`：`_fetch_bing_html(query, timeout=None)`（按块读、无固定 12s）、
  `_search_bing(..., timeout=None)`、`_search_ddg` 去 3 秒下限、`web_text_search._call` 传 `wait`、
  specs 带 `min_wait`。
- `worker_base.py`：`_search_bing(query, timeout=None)`（按块读、无固定 15s）、
  `_invoke` 把 `wait` 传给两条通道、`_provider_specs` 带 `min_wait`。

## 3. 离线反例（新增 4 条，全在 CI 覆盖文件 `test_search_quality_unified.py`）

| 用例 | 断言的反例 |
|---|---|
| `test_provider_timeout_never_exceeds_remaining` | 固定 12/15 秒与 `max(3.0, …)` 都不得越过剩余时间；富余时仍用提供方上限（不放大） |
| `test_slow_body_read_stops_at_deadline` | **慢读取**：每块 0.05s、截止 0.12s → 只读 3 次就抛 `TimeoutError`，读数 < 6（读取被真正切断） |
| `test_refuses_to_issue_below_provider_floor` | 剩余 1.00s < ddgs 下限 5.00s → 0 次调用、`attempts=0`、`refused_calls=1`、`budget.used=0`、`status=timeout` |
| `test_no_new_request_after_deadline` | **截止后零新请求**：单次耗 0.12s > 0.1s 截止 → 只发 `["q1"]` 一次，后续查询不再发出 |

**实测**：

```
python -m unittest test_search_quality_unified
# 默认跑法 Ran 63 tests → 62 OK；唯一 error 是沙箱拒写 mkdtemp 目录（见 §6，已用脚手架跑通 63/63）
# 新增 4 条 + C0-2 既有的 overrun/remaining-time/short-deadline 等全部通过
python -m unittest test_p0 -k bing -k text_search -k ddg
# Ran 4 tests OK
python .tmp/run_tests.py test_search_quality_unified test_p0
# 63 OK / 412 OK（脚手架绕开 mkdtemp 只读限制，见 §6）
```

**替身签名调整（不是放宽判据）**：`_search_bing` 现在接收剩余时间，三处注入替身原先只收一个
位置参数，改为等价的 `(q, timeout=None)`：
`test_p0.py:1457`（bing down）、`test_p0.py:4917`（bing 正向）、
`test_search_quality_unified.py` 的 `_bing`。断言与阈值未改。

## 4. 真实 Redis 双进程预占：**未验**（环境不具备，照实记录）

指令要求"真实 Redis 双进程预占用现有可用环境"。本轮实际执行与结果：

- **本机 Redis 可用**（`PING True`，127.0.0.1:6379），两个**独立进程**（`Start-Process`）
  对同一 `root_task_id` 并发调用生产函数 `_reserve_search_calls(6, total_calls=6)`：
  - 进程 B：`granted=6, left=60.0`；
  - 进程 A：`granted=0, why="台账预占失败：MISCONF Redis is configured to save RDB snapshots…"`；
  - 台账最终 `used=6`，两次授予合计 `6 ≤ 6`。
  - **可信的部分**：跨进程共享同一台账键成立；且**预占失败时本次零额度**、不回落默认额度
    （C0-2 的 fail-closed 语义在真实 Redis 上得到一次实证）。
  - **不可信的部分**：A 的失败来自 Redis 自身拒写，不是并发定序，因此**"健康 Redis 上
    两进程并发预占总量 ≤ 6"仍未验证**。
- **起一个干净的私有 Redis 以完成该验证：做不到。** 便携 `redis-server.exe`（msys2）在
  本会话沙箱内启动即失败：
  `fatal error - NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0S5-…): 0xC0000022`。
  这是沙箱对命名内核对象的限制，不是项目缺陷；按纪律**不扩大权限**、不重启用户服务。
- 结论：该项按"环境不具备 → 未验"记录，不以顺序假对象替代。清理：本次留下的键
  `search_ledger:h1dual210631` 带 TTL 7199s，会自行过期（当前 Redis 拒写，无法 DEL）。

## 5. 顺带发现的运行环境缺陷（非本批范围，需架构师/用户处理）

本机运行中的 Redis（6379）处于**持续性 MISCONF**：`stop-writes-on-bgsave-error=yes`、
`rdb_last_bgsave_status=err`、`lastsave=2026-09-27 17:07:12`，之后**所有写命令被拒**
（实测 `SET` 直接 `MISCONF`）。

根因（只读探测）：`CONFIG GET dir` = `/portable/Redis-8.10.1-Windows-x64-msys2` —— 这是
msys2 风格路径，不是合法 Windows 路径，bgsave 必然失败；再加上
`stop-writes-on-bgsave-error=yes`，Redis 转入拒写状态。

**影响**：写被拒会命中 `LPUSH task_queue:*`、结果回传、状态落库与检索台账（`HINCRBY`）——
即**当前实例无法接受新任务**；H1 的每一条"预占失败 → 本次零额度"都会生效（检索不发请求）。
本批**不重启服务、不改用户 Redis 配置**；仅报告，请按运维决定处理（修 `dir` 配置后重启，
或先 `CONFIG SET stop-writes-on-bgsave-error no` 应急——后者会掩盖持久化故障，需人工判断）。

## 6. 未验 / 未做（照实）

- **未验**：真实 Bing/ddgs 出网路径的截止表现（本批全部为离线替身；未发真实检索）。
  H1 明确"替换外部 provider、无需发真实金融搜索"。
- **未验**：健康 Redis 上的双进程并发预占（原因见 §4）。
- **未做**：`sina_ranking.py:150` 的 `timeout=15` 属排行链路，不在本批的有界检索范围。
- **未做**：ddgs 内部 12/15 秒等第三方默认值无法从外部收窄，只保证**我们传给它的 timeout**
  不越界；其内部重试行为不在本项目控制内（如实记录，不假称绝对）。
- **沙箱限制曾挡住 2 条既有用例，已查明根因并用本地脚手架跑通**（H2 批次一并处理）：
  本会话沙箱把 `tempfile.mkdtemp()` 建的目录视为只读——mkdtemp 用 `mode=0o700`，
  而 `os.mkdir` 用默认 mode 可写（实测：`os.mkdir+write` OK、`makedirs+write` OK、
  `mkdtemp+write` PermissionError）。因此
  `test_search_quality_unified.TestPolicyIsConfigurable.test_current_policy_reads_config_file_and_reloads`
  与 `test_p0.TestScheduledJobAlertRetry.test_failure_triggers_daily_bounded_resubmit`
  在默认跑法下报 `PermissionError [Errno 13]`，与代码改动无关。
  用一次性本地脚手架 `.tmp/run_tests.py`（**不进库**，`.tmp/` 已 gitignore；只在运行期把
  `mkdtemp` 换成默认 mode 的等价实现，未改项目文件、未扩权、未改测试语义）复跑：
  - `test_search_quality_unified` → **63 例 OK**（不再有那条 error）；
  - `test_p0` → **412 例 OK**；
  - `test_search_quality_unified.TestPolicyIsConfigurable` 10 例 OK、
    `test_p0.TestScheduledJobAlertRetry` 4 例 OK。
  CI（ubuntu）无此限制，按原样运行。原记录为"环境受限未跑"，现更正为**已跑通**。
