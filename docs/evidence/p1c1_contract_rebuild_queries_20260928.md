# P1-c①：契约重建吞查询（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §4 第一条

> **契约重建吞查询。** 在已有契约块后附四条 `[重试检索查询]`，缺失/陈旧wire触发
> `apply_to_steps` 后变成零条。`_strip_contract_marks` 在契约标记处截断尾部，而派发只
> 修改局部step。修完整的计划→重试→重建→worker路径，保留契约内新查询、拒绝契约外查询，
> 持久保存规范计划；不是只改日志或取消指纹校验。无新查询/额度耗尽时明确停，不再自然
> 回退到原查询。

基线 `a0780e3`。**未跑付费整链、未联网、未调用模型**：全部离线复算。

## 一、反例逐字复现（本机离线，用真实 `ExecutionContract`）

```
apply_to_steps          → 指令：2 条 [检索查询] + 装配句 + [研究契约] 行 + 正文
追加 4 条 [重试检索查询] → 指令末尾多出 4 行重试查询 + 「【搜索重试 1】」提示
再次 apply_to_steps      → 重试查询 **4 → 0 条**；「【搜索重试 1】」也消失
repairs                  → ['s1: 剥离旧契约标记 3 处', 's1: 按契约重建检索查询']
```

**"重建成功"的日志下面，整批新查询被静默删掉**。而 `worker_base._query_variants()`
的优先级是：有 `[重试检索查询]` 行 → 只打这批；否则 → `contract.queries()`。
于是重建之后 Worker 拿到的是**契约原查询** —— 也就是"上一批已证明打不出结果的查询"
再打一遍，换了资料面的那一轮**等于原地重试**。

## 二、根因（两处，都在 `execution_contract.py`）

### 1. `_strip_contract_marks` 在契约标记处**截断尾部**

```python
body = "\n".join(keep)
if CONTRACT_MARK in body:
    body = body.split(CONTRACT_MARK)[0].rstrip()     # ← 标记之后全部丢弃
```

契约块是**单行**（`contract_line()`），但这里按"标记之后全是契约内容"处理。而编排器
（`orchestrator_v2._search_retry_instruction`）恰恰把重试批次**追加在指令末尾**——
正好落在被截断的那一段里。

### 2. 重建**不幂等**：装配句与契约正文每次重建都多积累一份

`_build_instruction` 无条件 `f"{head}\n{contract_line()}"` 前置，而剥离阶段既不认
自己写过的 `head`，也把契约行标记之后的正文当成"要保留的正文"送回 `keep`。
实测三次重建后：装配句 4 份、契约正文 3 份，指令越滚越长。

## 三、修法

| 位置 | 改动 |
|---|---|
| `_strip_contract_marks` | 契约行**整行丢掉**（内容全部由 `contract_line()` 重生成）；行内标记仍只切标记及其后同段；**其余行（含 `[重试检索查询]`、"【搜索重试 N】"）原样保留** |
| `_build_instruction` | 前置 `head` 之前先摘掉旧的同名装配句（只摘**不含查询行**的那部分，逐份移除）→ **幂等** |
| 新增 `query_in_contract(q)` | 契约内判据两条，都由契约字段算出：①不与契约期间冲突（沿用 `conflicting_periods`，与 `violations()` **同一把尺**，不另立更严的口径）；②提到本主体（简称/代码/全称之一） |
| 新增 `retry_queries_in(text)` | 剥离**之前**先把 `[重试检索查询]` 取出来 |
| `apply_to_steps` | 逐条过 `query_in_contract` → **契约内保留、契约外拒绝并写进修复记录**；旧的 `[重试检索查询]` 行**一律先摘掉**再按校验后的集合重建（否则被拒的查询会留在指令里跟着发出去）；修复记录分别写"保留契约内重试查询 N 条"/"拒绝契约外重试查询 N 条：<第一条与理由>" |

## 四、修后（同一反例，离线复算）

```
重试保留 = 4 条        （修前 0 条）
「【搜索重试 1】」在   （修前消失）
二次重建 == 一次重建；三次重建 == 二次重建   （修前每建一次多一份）
契约行 1 份；装配句 1 份
constraint：契约外查询（"比亚迪 2024年年度报告 全文"）
  → 拒绝，理由「未提到契约主体（洋河股份（002304.SZ））」，且**不在**重建后的指令里
repairs: ['s1: 剥离旧契约标记 2 处',
          's1: 保留契约内重试查询 1 条',
          's1: 拒绝契约外重试查询 1 条：比亚迪 2024年年度报告 全文（未提到契约主体（…））',
          's1: 按契约重建检索查询']
```

**未改**：指纹校验（`matches`）、派发前的 `violations` 闸门、`_dispatch` 中
"重建失败即拒发"的处置——一条都没动。修的是"重建的内容"，不是"重建的准入"。

## 五、测试

`test_search_quality_unified.TestStructuredRetryQueries` 新增 4 条：

| 用例 | 断言 |
|---|---|
| `test_contract_rebuild_keeps_in_contract_retry_queries` | 反例逐字复现：4 条重试查询重建后**仍是 4 条**；重试提示活过重建；契约查询仍只有 1 条；修复记录写明"保留 4 条" |
| `test_contract_rebuild_rejects_out_of_contract_retry_queries` | 契约外（换主体）被拒且记明理由，**且不在重建后的指令里** |
| `test_contract_rebuild_is_idempotent` | 二次=一次、三次=二次；契约行 1 份、装配句 1 份、重试 4 条 |
| `test_query_in_contract_judges_subject_and_period` | 主体/期间两条判据（含空查询） |

回归：`test_search_quality_unified` + `test_orchestrator_v2` **149 OK**；
其余见提交信息。

## 六、本批**未**做完的部分（如实列出，不含糊）

1. **持久保存规范计划**：`orchestrator_v2._dispatch` 里重建结果只落在**局部**
   （`step = _fixed[0]`，`orchestrator_v2.py:4573`），**没有写回计划/状态**。
   即"这次派发用的是重建后的指令"成立，"规范计划被持久化"**不成立**。
   本批**未改**——它要动计划持久化路径（`state` / checkpoint / `_publish_full_state`），
   需要单独一批并配"重启后读回的指令与派发时一致"的回归。
2. **无新查询时的"明确停"**：`_search_retry_instruction` 在重试计划用尽时已如实写
   "本轮没有可用的新查询"，但 **Worker 仍会回退到契约原查询**——
   `worker_base._execute_bounded` 第 1046 行 `variants = self._query_variants(instruction)
   or [instruction[:120]]`：`_query_variants` 返回空会退化成"拿整段指令当查询"，
   比回退原查询更差。要做对，必须先让"停"这件事**跨层可见**（结构化
   status/reason/retryable），否则只能记日志、外层照记 SUCCESS —— 那正是本批
   第 ② 条"状态跨层丢失"要一起解决的问题。**两条一起做**，本批不半做。
3. `conflicting_periods` 允许 `as_of` 年份（既有语义），因此"洋河股份 … 2025年年度报告
   全文"这类查询按**契约自己的定义**算"契约内"。本批保持与 `violations()` 同一把尺，
   没有另立更严口径——若架构师认为重试批次应只允许 `periods` 内的年份，
   这是一处**待裁决的口径**，不是遗漏。

## 七、口径声明

全部结论来自离线复算（`ExecutionContract` 真实实现 + 假指令），未联网、未消耗额度；
未改门禁/阈值/模型/权限/模板；未删除任何日志、备份或产物。
