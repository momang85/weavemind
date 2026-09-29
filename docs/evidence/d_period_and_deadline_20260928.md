# D 批执行证据（期间口径 + 真实截止中断）· 2026-09-28 晚

基线：`41de3ba`（C 批收口）。约束同前：不新增付费整跑（本批全是离线/进程内验证，
**没有任何外呼**）、不改阈值/模型/权限/模板、不清理真库。

---

## D-② 真实截止中断（`read_with_deadline`）

### 修前复现（审查给的形状，进程内 socket pair）

脚本：`%TEMP%\wm_d2_repro.py`（对端每 5ms 发一字节、预算 0.02s；响应体走
`sock.makefile("rb")` 的 buffered read，与 `http.client` 同形状）。

```
[D-②] 预算 0.020s → TimeoutError(read deadline exceeded (slow response body))；实际耗时 0.1076s
       是否守住总截止（≤预算+0.05s）：False
```

**0.1076s**（与审查实测 0.1069s 同量级）：总截止没有被执行——`read()` 的缓冲循环会一直
读到读满或 EOF，所以控制权只在**对端结束**时才回来；`_bound_single_read` 设的
socket 超时只管**单次 recv**，"每 5ms 来一个字节"永远不触发它。返回值 `False`
（"这条路径不可终止"）也被调用方忽略。

### 修后

| 项 | 修法 |
|---|---|
| 真正的块内边界 | 单次读改用 **`read1`**（最多触发一次底层读，拿到多少返回多少）→ 循环里的时钟检查能在预算内叫停 |
| 单次读的硬边界 | 每次读之前把**剩余时间**设到响应自己的 socket（尽力而为） |
| 到点即停 | 每次读后再查时钟，超了就抛 `TimeoutError`；**EOF 越界**照样抛（不是"读完了"） |
| 到点**放掉连接** | 新 `_close_quietly(resp)`：超时/拒绝路径关闭响应体，不留还在出网的连接 |
| 不可保证有界的路径**明确拒绝** | 新 `UnboundedReadError`；`read_with_deadline(..., require_bounded=True)` 为默认。既无 `read1` 又设不上超时 → 直接拒绝（**不发出读取**，测试断言 `reads == 0`） |
| 业务区别保留 | 归类：`UnboundedReadError → "refused_unbounded"`（**不进** `RETRYABLE`，重试只会再发一次不可取消的请求）；`TimeoutError → "timeout"`；零命中仍是 `no_results` |

同一脚本（修后）：

```
[D-②] 预算 0.020s → TimeoutError(read deadline exceeded (slow response body))；实际耗时 0.0220s
       是否守住总截止（≤预算+0.05s）：True
```

**0.1076s → 0.0220s**（预算 0.02s）。

### 定向用例（`test_search_quality_unified.py`，共 77 条全 OK）

- 新增 `test_slow_drip_body_is_cut_at_the_deadline_not_at_peer_eof`：**socket pair + 每 5ms 一字节 + 预算 0.02s**，
  断言"耗时 ≤ 预算 + 0.05s"且"到点已关闭连接"（就是本轮反例的固化）；
- 新增 `test_unboundable_path_is_refused_by_default`：无 `read1`、无可设超时 → `UnboundedReadError`，且 **0 次读取**；
- 原有三条"块间/块内时钟"用例显式传 `require_bounded=False`（它们验的是时钟逻辑，不是边界策略），
  正向用例（预算充足正常读完）不受影响。

---

## D-① 期间口径（报告所属期间 vs 披露资料截止日）

### 裁决（照单执行）

- 2023 / 2024 = **报告所属期间**；`as_of`（2025-04-30 / 06-30）= **披露资料截止日**；
- "洋河股份 2024 年度报告，2025 年 4 月披露" → **允许**；
- "洋河股份 2025 年度报告" / "2025 年年报" / "2025 年半年度报告" → **拒绝**（那是披露年份，
  不是本契约的目标期间）；
- **不用 `as_of` 年份扩展目标期间**；重试批次 / 初始查询 / 摄取走同一语义。

### 改动

`execution_contract.py`：

- `period_years()`（只有 `periods`）与 `disclosure_years()`（只有 `as_of` 年份）**分开**；
  `allowed_years()` = 两者并集，但语义改为"**允许出现**"，不再是"允许当期间"；
- `conflicting_periods()`：年份落在 `periods` → 合法；年份是 `as_of` 年份时，
  只有**紧跟报告期词**（`_YEAR_REPORT_RE`，锚在年份后面 `match`）或**不在披露/日期语境**
  （`_disclosure_context`：后随"年X月"/`YYYY-MM-DD`，或附近有"披露/发布/公告/截至/资料截止"）
  才算冲突；
- `query_in_contract()` 沿用同一判据 → **重试批次只允许 `periods` 内的年份**，与初始查询同语义。

### 实测（同一契约：periods=[2023,2024]、as_of=2025-04-30、doc_type=年度报告）

```
'洋河股份 2024年年度报告 营业收入'          -> 冲突 []            查询保留 True
'洋河股份2024年度报告，2025年4月披露'       -> 冲突 []            查询保留 True
'洋河股份 2025年度报告 营业收入'            -> 冲突 ['2025']      查询保留 False（含契约外期间 2025）
'洋河股份2025年年报'                       -> 冲突 ['2025']      查询保留 False
'洋河股份 2025年4月29日 披露的年度报告'      -> 冲突 []            查询保留 True
'洋河股份 2023年年度报告 归母净利润'         -> 冲突 []            查询保留 True
'洋河股份 2019年年度报告'                  -> 冲突 ['2019']      查询保留 False
'洋河股份2024年三季报'                     -> 冲突 ['三季报','季报'] 查询保留 False
'洋河股份2025年半年度报告'                  -> 冲突 ['2025']      查询保留 False
'资料截至 2025-04-30 的合并报表'            -> 冲突 []            （无主体名 → 查询不保留）
```

定向用例：`test_delivery_chain.TestPeriodVersusDisclosureSemantics`（3 条）——
期间/披露集合分开、披露日允许而披露年份不得当报告期、契约期间与其它年份照旧。

---

## 本批未做 / 未验

- **D 之外**：C 批遗留的"环境项"仍未验（跨进程真实并发、真实 Redis 租约转移、真实崩溃恢复演练、
  第二家公司真实原文、干净环境同包完整链、远端 CI、真人 F3）——都需要本机不具备的环境/真人。
- **摄取侧**：`disclosure_ingest` 的准入判据本来就把 `periods` 与 `disclosed_at <= as_of`
  分开用，本轮未改它；上面的语义统一落在**契约层**（查询/重试/标注不适用），
  逐条产品或披露的准入仍按原判据（未发现把 `as_of` 年份当期间的路径）。
- 本批**没有**新增外呼：D-② 的验证全在进程内 socket pair，D-① 全是纯函数。
