# RC2-B：完整性、计量与并发 — 实现与回归证据

日期：2026-10-24　执行：lead（DATA-RC2-1024 §4）　回归基线：HEAD `ed8fd90`

## 1. 数据集完整性（F5）

`screen_store._persist_dataset` 写入后**回读**实际 `rows.json`：
- `sha256(rows.json 字节) == meta.source_sha256` 且行数一致，且 `scope`/`schema` 字段相符，才算写入成功；
- 不一致 → 抛 `StoreWriteError`（「同 ID 数据集实际内容与元信息不一致（rows.json 哈希不符，可能损坏）：损坏数据不当作成功复用，也不重写原文件」），HTTP 层映射 503 `store_unavailable`，原文件字节保持不变；
- 回归：`test_data_rc2` 损坏复用用例（重复 `collect` 返回错误、字节不变、`load_dataset` 继续拒绝）+ `rc2_after_probe` F5（`bytes_unchanged_after_refusal=true`, `load_rejects=true`）。

## 2. 账本不可读/损坏（F7）

`screen_store.record_cost` 读取失败路径：
- `integrity ∈ {corrupt, unreadable}` 或存在 `load_errors` → **不写入**、不覆盖原字节，返回 `recorded=false`，`recording.reason ∈ {ledger_corrupt, ledger_unreadable}`；
- `rebuild_cost(confirm=True)`：先把坏字节归档（`archived` 路径字节与坏文件一致），再从零账本重建（`runs_total=0`，不冒充历史完整统计）；
- 回归：`test_data_rc2.LedgerAndMetering`（不可读→`ledger_unreadable` 且字节不变且累计 7 不丢；损坏→`ledger_corrupt` 且重建先归档）；`rc2_after_probe` F7（`bytes_preserved=true, runs_total=7`）。

## 3. 计量失败可见

- `screen_prepare.collect` 返回 `metering{recorded, recording}`，两条路径（adapter/官方缓存物化）都带；web 层 `_post_screen_prepare_collect` 透传给前端；
- 业务成功 + 计量失败 → 前端显示警告条（`StockScreen.tsx`：「业务结果已返回，但成本计量未记录（reason）…」）；
- 回归：`test_metering_failure_visible_in_collect_response`（坏账本下 collect 仍返回结果，`metering.recorded=false, reason=ledger_corrupt`）。
- `network_requests` 语义（F6）：适配器来源下 `null`=未知（传输层不暴露连接事件），`adapter_calls` 为适配器调用数；前端成本卡片与取数结果均显示「未知」，不再显示 0。既有 `network_ok/failed/denied/attempts` 分列保留。

## 4. 真实取数次数 vs 适配器调用数（F6）

- `collect` 每公司每期间调用适配器一次 → `adapter_calls` 计数；被策略拒绝的公司不发起调用，计入 `denied`；连接前失败（DNS 等）计入 `pre_connect_failures`，不计为已连接；
- `network_requests` 在适配器来源下固定 `None` + `network_requests_note`（照实写「未知」的原因），probe F6 断言 `adapter_calls=1, network_requests=null`。

## 5. 并发保护（保留）

- 8 线程并发加清单仍受版本守卫（`test_data_rc2.ConcurrencyRetained.test_eight_way_watchlist_adds_stay_serialized`：一个成功、其余 409 `version_conflict`）；
- `test_screen_continuity.py`（OK）覆盖运行历史/结果连续性；`test_screen_store.py`（OK）覆盖同内容数据集幂等复用（同 `dataset_id`）。

## 6. 回归清单（本轮全部 exit 0）

| 测试 | 结果 | 日志 |
|---|---|---|
| `test_data_rc2.py` | OK (18) | `logs/a-test_data_rc2.log` |
| `test_screen_store.py` | OK | `logs/b-test_screen_store.log` |
| `test_stock_screen.py` | OK | `logs/b-test_stock_screen.log` |
| `test_screen_api.py` | OK (48) | `logs/b-test_screen_api.log` |
| `test_screen_continuity.py` | OK | `logs/b-test_screen_continuity.log` |
| `test_screen_prepare.py` | OK (9) | `logs/a-test_prepare.log` |
| `rc2_after_probe.py` | `all_fixed=true` | `logs-rc2_after_probe.txt` |

未验：真实网络下的真实请求数采样（需公网与真实站点许可，本轮无网），见 `release-gates.md`。
