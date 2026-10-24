# C0 存储一致性收口报告（BETA-CLOSE-1024）

日期：2026-10-24｜执行：lead（隔离实例 8085 / tmp/closeout-8085，生产 8080 未启动）

## 1. 结论

- **PASS**：`docs/evidence/20261024-beta-closeout/logs/c0-http-consistency.log`（HTTP 24 项全过）
- **PASS**：`logs/c0-store-tests.log`（store 级并发/原子性/单写者回归 13 项，`test_screen_consistency.py`）
- 既有 235 项测试（test_stock_screen 59 / test_screen_store 77 / test_screen_api 48 / test_screen_continuity 24 / test_auth_audit 27）在本单改动后全绿。

## 2. 实现要点（screen_store.py / web_ui.py）

| 需求 | 实现 | 证据 |
|---|---|---|
| 全程读-改-写事务保护 | `save_preset`/`delete_preset`/`save_dataset`/`watchlist_mutate`/`record_cost` 全部在 `_lock_for(path)` 进程内互斥下执行；跨进程由 `claim_writer()`（msvcrt `LK_NBLCK` + `.writer.json`）独占 | c0-store-tests 13/13；8086 第二写者被拒（holder 信息含 pid/时间/目录） |
| 冲突→409 | `VersionConflict(RuntimeError)` 带 `.current`；`_post_screen_watchlist` 映射 409 `version_conflict` | c0-http-consistency.log |
| 计量未记账可见 | `record_cost` 返回 `recorded` + `recording{ok,reason,detail}`；run/dataset 响应嵌 `metering` + 「注意：…成本计量未成功记录…」 | c0-http-consistency.log（corrupt 账本时 `recorded=false, reason=ledger_corrupt`） |
| 坏账本字节保留 | `load_cost` 判 `empty/ok/corrupt/unreadable`；corrupt 字节原样保留，`rebuild_cost` 归档为 `ledger.corrupt-<stamp>.json` | c0-http-consistency.log「执行筛选后坏账本字节仍未被覆盖」 |
| 并发回归 | 并发加清单 2→2、并发记账全额入账（1 秒分辨率 → 每条目单调 version） | test_screen_consistency.py |
| 计数语义 | 成功/失败/缓存复用/预览/计算分账；`network_attempts/network_denied/network_requests/network_ok/network_failed` 分列，**被策略拒绝的请求没有真正连接，与已抓取分开计数** | C1 日志（denied 1 → requests 0） |
| Windows `os.replace` 失败 | `_write_one` 重试 4×0.03s 后抛 `StoreWriteError`（「未写入」），HTTP 层 503 `store_unavailable`，**不诱导重复执行**（可原样重试，内容定址幂等） | c0-store-tests |
| 单写者 | `main()` 先 `claim_writer` 再 `_init_db`，失败打印「启动拒绝：…」并 `SystemExit(2)`；`/api/health` 暴露 `screen_writer{claimed_by_this_process,pid}` | 8086 启动拒绝日志（本单实测复现） |

## 3. 架构师三个反例的修复验证

1. 清单 2 次并发 add → 1 条：现在 2 条都留下（409/锁二选一不再丢更新）。
2. 两次 `record_cost(runs_total=1)` → 1：现在全额入账。
3. 损坏账本被覆写、警告丢失：现在 corrupt 字节保留 + 响应明说 + rebuild 走归档。

## 4. 边界

未触碰生产 8080/真实数据/agents.db；全部验证在 `tmp/closeout-8085`、`tmp/c2-8086` 隔离实例；未 commit/push。
