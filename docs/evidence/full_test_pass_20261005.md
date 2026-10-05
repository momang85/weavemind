# 全面测试记录（2026-10-05，本机，用户批准）

> **范围**：本机**全量**跑 CI 覆盖的**全部** 55 个 `test_*.py`，外加 Y2a 三项真实数据验收。
> **本机跑批不代表 CI 已过**：CI 是 Linux/Python 3.11；本机是 Windows/Python 3.14.3。
> 两边**依赖集合一致**（本机缺 `reportlab`/`feedparser`，而这两个**也不在** `requirements-runtime.lock`），
> 所以差异主要在解释器版本与操作系统，不在依赖。
> 跑批脚本 `.tmp/run_full_suite.py`（一次性、未入库），逐文件独立子进程、结果逐条落盘
> `.tmp/full_suite_progress.jsonl`。

## 一、全量结果：**55 / 55 PASS**（0 FAIL、0 TIMEOUT）

- 解释器：`C:\Python314\python.exe`（3.14.3）——本机**唯一**依赖齐备的解释器；
  `pyenv 3.11.6` 连 `redis`/`pypdf` 都没有，用它跑全量只会得到**假失败**（见第四节）。
- 总用时 **2187.1 秒**（≈36.5 分钟）。
- **覆盖一致性核对**：磁盘上 55 个 `test_*.py` 与 `ci.yml` 里 55 条 `python test_*.py` 步骤
  **完全一一对应**（差集 0）—— 既没有"CI 不跑的孤儿测试"，也没有"CI 跑了但文件不在"的悬空步骤。

最慢 6 个（供后续分片参考，不是问题）：

| 文件 | 用时 |
|---|---|
| `test_offline_delivery`（M0-f） | 524.8s |
| `test_task_time_optimization` | 345.4s |
| `test_root_budget` | 313.5s |
| `test_orchestrator_v2` | 251.1s |
| `test_delivery_chain` | 414 用例 | 151.3s |
| `test_p0`（434 用例） | 122.1s |

**本轮改动相关**：`test_market_events` **54 项全过（1 skip 按设计）**；
`test_net_policy`、`test_delivery_chain`、`test_orchestrator_v2`、`test_offline_delivery`、
`test_financial_analysis` 全过 ⇒ 本轮新增包/脚本**没有**碰坏既有链路。

## 二、新增的性质测试与冻结金样（补关系断言覆盖不到的角落）

| 测试 | 锁住什么 |
|---|---|
| `test_row_order_does_not_change_reading` | 载荷行序打乱后 `reading_hash` 与逐点值**完全相同**（否则"同输入同输出"是假的） |
| `test_no_future_row_enters_feature_side` | 性质：锚点 ≤ `feature_cutoff`，且**所有非锚点结果点 > 截点**（特征泄漏的机器判据） |
| `test_frozen_numeric_golden` | 冻结数值：100→110 / 1000→1050 ⇒ `return=0.1`、`benchmark_return=0.05`、`excess_return=0.047619`（手算值） |
| `test_aggregate_arithmetic_is_frozen` | 聚合冻结：A=+4.7619%、B=0% ⇒ 均值＝中位数＝**0.0238095**（n=2）；两个单点值同时断言 |
| `test_repeated_runs_are_bit_identical` | 连跑 5 次只有 1 个哈希（无非确定性） |

> 写这两条金样时**我自己的算错了一次**：原以为两事件超额是对称的 ±4.7619%、均值 0，
> 实际第二个事件也是 +4.7619%（我只改了标的价格没改基准）。测试报错后按手算改正为
> A=+4.7619%／B=0%⇒均值 0.0238095 —— **是测试写错，不是算子错**，如实记下。

## 三、Y2a 三项真实数据验收（同一跑批，解释器 3.14.3）

| 验收 | 结果 |
|---|---|
| A 两家公司事件读数（数据集 `3d6b4acb…`） | `status=ok`，`reading_hash=98fbc4cdc4b6…` |
| B 事件日历离线（已准入材料 → 事件） | 1 条事件、`date_basis=source_url_format`、`reading_hash=8f1b02c95b64…` |
| C 京蓝更正/重述手算小样（数据集 `46d37bbb…`） | 披露 `2025-09-05`、`t0=2025-09-08`、`suspension_suspect=['2025-09-05']`、`no_lookahead=true`、**`gold_diffs=[]`** |

**跨解释器确定性（额外收获）**：A 的 `reading_hash=98fbc4cdc4b6…` 在 **Python 3.11.6 与 3.14.3 上完全相同**，
C 的 `gold_diffs` 两边都是空 ⇒ 读数不依赖解释器版本（`Decimal`＋定点量化＋`sort_keys` 规范化 JSON 生效）。

## 四、为什么换解释器：一次差点被当成"真失败"的假警报

最初用 `pyenv 3.11.6` 跑时，`test_net_policy.py` 报 3 个 ERROR：
两个是 `ModuleNotFoundError: No module named 'redis'`（[test_net_policy.py:821](../../test_net_policy.py) 经
`workers.data_loader_worker` → `async_worker_base` 顶层 import `redis.asyncio`），
一个是 `NetworkPolicyError: 解析到非公网地址：198.18.0.35`（TUN 代理 fake-IP）。
换上依赖齐备的 3.14.3 后 `test_net_policy` **PASS**，`redis` 那类错误消失 ⇒ **确认为环境所致，非代码缺陷**。
（`198.18.x` 的处理见第五节。）

## 五、环境侧的已知限制（不改代码，如实登记）

1. **TUN 代理**：本机网络策略把域名解析到 `198.18.x`（fake-IP）时，`net_policy` 会按"非公网地址"拒绝。
   表现为**间歇性**：巨潮公告查询（`scripts/x1_event_calendar_run.py` 的联网分支）本轮 12 次全部失败。
   ⇒ 这**不是**"这家公司没有公告"，脚本把它记成 `errors` 并把 0 行标成"该类别码未取到行"。
2. **巨潮类别码未实测验证**：`CATEGORY_CANDIDATES` 里的码是**待实测**常量，0 行只代表"这个码没拿到行"。
3. **交易日历仍未落盘**：停牌只能以基准序列为代理报**疑似**（`suspension_suspect`），不能与休市区分。
4. **本机缺 `reportlab`/`feedparser`**：与 CI 一致（两者都不在 `requirements-runtime.lock`），
   因此不构成"本机少跑"。

## 六、第二轮：Y2b 落地后的端到端全量（**56 / 56 PASS**）

用户要求"最后端到端测试"。Y2b（组合回测 `quant_research/backtest.py` ＋ Qlib 适配层
`quant_research/qlib_adapter.py` ＋ `test_backtest.py`）落地后，把测试面从 55 扩到 **56**，
**再跑一次全量**：

- **56 / 56 PASS**（0 FAIL／0 TIMEOUT），总用时 **2262.4 秒**（≈37.7 分钟），解释器 3.14.3。
- 新增 `test_backtest` **PASS**（26 项：账本守恒、T+1、成本前后、印花税生效日、分红入账、
  不可能成交、留出区间、冻结哈希、审计、Qlib 边界）。
- 最慢 5 个：`test_offline_delivery`(564.1s)、`test_task_time_optimization`(336.5s)、
  `test_root_budget`(314.1s)、`test_orchestrator_v2`(254.0s)、`test_delivery_chain`(164.3s)。
- **覆盖一致性**：磁盘 56 个 `test_*.py` 与 `ci.yml` 新增 `test_backtest.py` 步骤后
  **仍为 56 对 56、差集 0**（新测试已挂进 CI，不会变成"跑了但没人管"的孤儿）。

端到端另含 Y2b 真实数据跑批：`scripts/x1_backtest_run.py` 在 `3d6b4acb…`（002304＋600031＋000300）
上产出 `status=ok`、**`audit_ok=true`**、66 笔成交、24 条拒绝、成本前后净值线与回撤/换手/敞口，
以及 Qlib CSV 导出（1816＋908 行、跳过 0）。详见
[Y2b 证据](y2b_backtest_20261005.md)。

## 七、结论与边界

- **全量 55/55 PASS**，加上 Y2a 三项真实数据验收通过、跨解释器哈希一致 ⇒ 本轮的算子/日历/脚本**可交付**。
- **仍不得据此宣称**：CI 已过（看 CI）、人工复核通过（未做）、alpha/因果（未做识别策略）、
  交易日历已解决（待补）、采购已完成（等你决定）。
