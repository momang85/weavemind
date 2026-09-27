# C0-2 根检索预算：首次请求前原子预占、真实停止（09-27 架构行动）

日期：2026-09-27。批次：**C0-2**（指令 `docs/真实研究闭环与阶段D收口_20260927.md` §3.3、§4-C0.2）。
基线 `453f958`；上一批 C0-1 = `5107ac0`。默认仍是 **6 次 provider 调用 / 60 秒，不放宽**。

## 1. 反例 → 修法（逐条）

| # | 反例（§3.3） | 修前 | 修后 |
|---|---|---|---|
| 1 | 首轮耗 59 秒只用 1 次，台账在**跑完**才写 → 下一派发仍得 5 次/60 秒 | `_record_search_ledger` 在 `_finish_search` 里记实际用量；起点 `HSETNX` 在收尾才落 | `_reserve_search_calls()` 在**首次请求之前** `HINCRBY used` 预占 + `HSETNX started` 落下根起点；剩余墙钟按根起点算（`test_deadline_counts_from_first_reservation`：把 started 回拨 59 秒后，下一次剩余 ≤2 秒） |
| 2 | 并发派发不预占，各自按"整份额度"发放 | 无预占 | 预占是原子 `HINCRBY`：先占者得额度，超额 `HINCRBY` 回滚；`test_concurrent_reservations_never_exceed_the_cap` 断言两次预占总量 ≤ 6 |
| 3 | bytes 键台账被误读成"未使用" | 只认一个字段名 | `_ledger_pick` 按字段后缀匹配（`str(k).endswith(field)`），str/bytes 都认 |
| 4 | 读取/预占失败仍拿新额度 | 读失败按默认额度继续（等于台账一坏就无限额度） | 读/写失败 → **本次发放 0**，日志写明原因（`test_ledger_failure_grants_no_new_quota`） |
| 5 | 0.1 秒预算、provider 跑 0.25 秒返回仍记 `ok` | 只看 `any_call_ok` | 调用返回后比对 `elapsed > wait + 0.25s` 或已过截止 → 记 `overrun_calls/overrun_seconds`，**结果不入库**，无产出时状态 `timeout`（`test_overrun_result_is_not_counted_as_success`） |
| 6 | `take()` 的 1.0 秒下限让短预算形同虚设 | `max(1.0, time_left())` | 改为 `max(0.05, time_left())`；provider 拿到的 wait 服从剩余时间（`test_provider_receives_the_remaining_time`） |
| 7 | 预占额度可能泄漏 | 无 | 收尾 `_release_search_calls(max_calls - used)` 按**实际发出数**退回（`test_unused_reservation_is_returned`：预占 6、用 1 → 后续仍能拿 5） |

记录口径（§4-C0.2 要求"发起截止、实际停止、可量化清理开销分别记录"）：
`SearchOutcome.as_dict()` 新增 `overrun_calls` / `overrun_seconds`；台账键同时保留 `used` 与
`started`（根起点），派发日志写明授予额度与剩余墙钟。**不宣称"绝对精确 60 秒"**——同步 SDK
无法中途取消，超时返回只能"不计入 + 记账"，这一点在 §2 未验里写明。

## 2. 代码位置

- `worker_base.py`：`_ledger_pick` / `_search_ledger_read` / `_reserve_search_calls` /
  `_release_search_calls` / `_search_ledger_limits`（替换原 `_search_ledger` +
  `_record_search_ledger` + `_search_allowance`）；`_execute_bounded` 改为"先预占再取件"，
  `_finish_search` 改为按实际用量退回。
- `adapters/search_runner.py`：`take()` 去掉 1 秒下限；调用后超预算判定与
  `overrun_calls/overrun_seconds`；超预算且无产出 → `status="timeout"`；模块 logger。

## 3. 定向验证

```
python -m unittest test_search_quality_unified test_p0 test_offline_delivery test_delivery_chain
# 855 tests OK（含新增：预占 6 例 + 并发 1 例 + 起点 1 例 + 退回 1 例 + 超时返回 2 例）
```

被调整的断言（不删断言、不放宽判据）：
- `TestTaskScopedSearchBudget` 从"读余额"改写为"预占"语义（首次全额 / 重试只拿余额 /
  并发不超限 / 起点起算 / 预占失败零额度 / 不跨任务串）；
- `test_p0.test_execute_returns_results_via_bing`：给最小假台账 + 身份上下文（C0-2 之后
  **没有任务身份的检索不发请求**，这是刻意的：不给"无身份就有无限额度"留口子）。

## 4. 未验 / 未做（照实）

- **未验**：多**进程**真实并发（两个 worker 同根任务同时预占）——本批用共享假台账做了同进程
  双 agent 验证；真实多进程需要跑起服务并制造并发检索，未做。
- **未验**：Bing 固定 12/15 秒与 DDGS `max(3.0, …)` 的超时仍未完全服从剩余时间——本批把
  **执行器**的剩余时间传播与超预算判定做实（超时返回不计入），但两个渠道内部的超时参数
  收窄（Bing 12s→剩余、ddgs 3s 下限）留到下一批，避免与 C0-3 的出口改动交叉。
- **未做**：C0-3 出口覆盖（`proxy_required` 在 HTML/PDF/披露各入口执行、建连后断开不落直连、
  requested/effective 出口可查）。
- 物理 HTTP 次数仍按"拿不到即未知"记（DDGS 隐藏其请求次数）。
