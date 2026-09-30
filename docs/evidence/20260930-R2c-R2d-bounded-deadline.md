# 2026-09-30 下午 · R2 后半：下载链真闭合（有界终止 + 发现/准入共享总截止）

执行依据 `docs/代码逻辑收口与DSH下一批_20260930下午.md` §4 R2（R2-c/R2-d）。与 R2-a/R2-b
（[资料链](20260930-R2a-R2b-material-version.md)）合起来即 R2 全批。小提交、失败→通过；
未扩模型/训练、未全仓重构、未新增付费、未读或输出密钥。

## 1. R2-c：50ms 慢响应头必须**真的有界终止**（不是"读完再查钟"）

**根因**（`adapters/transport.py`）：头阶段用 `urllib.request.urlopen(timeout=budget)`，而
`timeout` 只是**每次 socket 操作**的超时。对端每 8ms 发 4 字节时每次 recv 都"很快返回"，
于是逐字节慢头把一次 50ms 预算的取件拖到 **0.964s**，只在"读完头再查钟"处被记为
`read_timeout`——事后判定，不是有界终止；`test_transport_deadline.py` 当时只敢断言 `<3s`。

**最小修复**：新增 `_open_bounded(url, budget=, deadline=, headers=)`——头阶段改用
`http.client` 自己取，并给**总截止**配一个看门狗线程：
到点 `sock.shutdown(SHUT_RDWR)` + `close()`（**先 shutdown 再 close**：Windows 上只 close
不会打断另一个线程里阻塞的 recv，实测仍拖到 0.96s），阻塞中的 `getresponse()` 立刻返回，
按 `read_timeout` 如实上报（`body_bytes=0`）。响应对象被包一层 close（顺带关连接），
`with resp:` 与 `_close_quietly(resp)` 两条既有路径都受惠。两条通道（字节/文本）共用它。

**失败→通过**（`test_transport_deadline.py`，断言从"<3s"改成真实上界）：

| 通道 | 预算 | 修前 | 修后 |
|---|---|---|---|
| 字节通道（PDF/下载路径） | **0.05s** | 0.964s（事后判定） | **0.069s**，`read_timeout`，`body_bytes=0` |
| 文本通道 | 0.05s（该通道预算下限 0.5s） | 0.968s | **0.511s**，`TimeoutError`（保留 `deadline` 措辞契约） |

用例断言收紧为 `字节 <0.5s` / `文本 <1.2s`；**不带修复时两条都失败**。

## 2. R2-d：发现 → 取件 → 准入必须共享**同一份剩余总截止**

**根因**（两处，都在生产路径上）：
1. `orchestrator_v2._official_discovery_intake` 里 `self._discovery_fetch` 生产就是 `None`，
   而 `wrap_fetch(None)` 也返回 `None` → `discover` 用**内部默认取件**，那些请求完全不受这份
   台账约束（预算只统计到注入了替身的测试里）；
2. `mi.admit(...)` 没接剩余预算 → `material_intake._fetch_link` 另起一份"下载通道"预算；
   且 `wrap_fetch` 里 `max(0.5, min(...))` 会把"只剩 0.05s" **抬高到 0.5s** 继续取件。

**最小修复**：
- 生产路径上把 `adapters.cninfo._default_fetch` 包进 `_budget.wrap_fetch(...)`（次数/字节/
  截止与准入同一份台账）；
- 新增 `MIN_REQUEST_SECONDS = 0.5`：剩余预算 **< 0.5s 就拒绝取件**（记
  `deadline_below_min`），不再抬高；`wrap_fetch` 的 timeout 夹取改为
  `min(请求值, 剩余)`，不再有 0.5s 下限放大；
- `material_intake.admit(..., timeout=)` 新增参数并透传给 `_fetch_link`（`timeout=None` 才回落到
  下载通道默认值）；`timeout < MIN_REQUEST_SECONDS` 时**不发请求**，如实记
  `fetch_failed` + "剩余预算不足"；编排器把 `timeout=_budget.remaining()` 传进去。

**失败→通过**（`test_orchestrator_v2.TestOfficialDiscoveryBudget` +3，**不带修复 2 失败 1 报错**）：
预算 0.2s 时取件钩子拒绝且 `refused=['deadline_below_min']`、**未调用底层 fetch**；
生产路径（不注入替身）拿到的 fetch 是 callable 且调用它真的走到默认取件、超时被夹进总预算；
`mi.admit(timeout=0.1)` 不发请求、状态 `fetch_failed`、原因写明"剩余预算不足"。

## 3. 顺带发现并修掉一个**静默真连网**的测试缝

`test_delivery_chain.TestTencentQuotesRankingAndCache` 此前 patch `urllib.request.urlopen`
来喂 canned 行情；头阶段改成 `http.client` 后那个桩**不再被经过**，用例在真机上悄悄取回了
**真实行情**（断言 `1258.62 != 1500.0` 暴露）。已把替身挂到新缝 `transport._open_bounded`，
并逐条确认其余 patch `urlopen` 的用例（`test_prompt_system` / `test_settings_requirements` /
`test_offline_delivery` / `test_net_policy`）不经过被改的路径、全部仍通过。

## 4. 定向验证读数

`test_transport_deadline` 22、`test_net_policy` 51（改桩位置，断言不变）、
`test_orchestrator_v2` 89（+3）、`test_admission`、`test_cninfo_discovery` 33、
`test_delivery_chain` 408、`test_offline_delivery`、`test_financial_analysis` 131、
`test_prompt_system`、`test_settings_requirements` 全过；K2 两包离线复算不变（9/9、7/7）。

## 5. 下一步

R2 ✅（a/b/c/d 四条）→ **R3**：问题计划按意图区分历史分析 / 条件情景 / **预测子问题**
（纯预测请求明确 unavailable，不因出现指标词启动历史模型；混合任务只回答已支持子问题并说明
门槛；计划、执行、卡片与问题覆盖必须一致，notes 从实际计划生成），并纠正 Q4 文档里
"≥5/8 期、≥30 公司年度、3 折"被写成通用开关的表述（改为逐模型定义适用性）。随后 R4。
