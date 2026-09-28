# P1-c③：块间查时钟 ≠ 中断阻塞（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §4 第三条

> **块间查时钟不等于中断阻塞。** `read_with_deadline` 在单次read中耗0.12秒、预算0.02秒、
> 返回EOF时仍正常返回。补连接/慢读/SDK整个调用的可终止边界，传剩余时间只是必要条件；
> 无法兑现的provider应在有界路径拒发或受控隔离终止。禁止仅在返回后丢结果宣称停止。
> 保留根任务6次/60秒及实际停止/清理时间分账。
>
> **退出证据：** 慢速连续字节及EOF越界/不响应取消测试。

基线 `a0780e3`。**未联网、未跑付费整链**：全部用假响应离线验证。

## 一、反例（逐字复现架构师的读数）

修前 `adapters/search_runner.read_with_deadline`：

```python
while True:
    if time.monotonic() >= deadline:          # ← 只在**块间**查
        raise TimeoutError(...)
    block = resp.read(chunk)
    if not block:
        break                                 # ← 一次 read 把预算用光并返回 EOF → 当"读完"
    buf.append(block)
return b"".join(buf)
```

预算 0.02 秒、单次 `read()` 耗 0.12 秒、返回 EOF →
循环直接 `break`，**空正文被当成功返回**。"到点即停止读取"变成
**"返回之后才说停"**，正是指令里禁止的形状（"禁止仅在返回后丢结果宣称停止"）。

## 二、修法

```python
def _bound_single_read(resp, seconds) -> bool:
    """把剩余时间落到响应自己的 socket 上（resp.fp.raw._sock → … → resp），
    让**单次** read() 也在预算内；设不了如实返回 False。"""

def read_with_deadline(resp, deadline, chunk=65536, *, clock=time.monotonic) -> bytes:
    while True:
        remain = deadline - clock()
        if remain <= 0: raise TimeoutError(...)
        _bound_single_read(resp, remain)      # ① 单次读自己的硬边界（传剩余时间）
        block = resp.read(chunk)
        over = clock() >= deadline
        if not block:
            if over:
                raise TimeoutError("... at EOF ...")   # ② EOF **越界**不是"读完"
            break
        buf.append(block)
        if over:
            raise TimeoutError(...)                    # ③ 已过截止还拿到数据 → 正文不完整
    return b"".join(buf)
```

三处要点分别对应指令的三句：

| 指令 | 落实 |
|---|---|
| "传剩余时间只是**必要条件**" | `_bound_single_read` 把剩余时间设到 socket 上，**并且**保留块间/块内时钟检查——两者都要，缺一不可 |
| "补连接/慢读/SDK 整个调用的可终止边界" | 单次读的 socket 超时 = 剩余时间；`_fetch_bing_html` 本就把 `min(提供方上限, 剩余)` 传给 `urlopen`（连接边界）；`provider_min_wait` 对**无法取消**的同步 SDK（ddgs）在低于可行下限时**拒发** |
| "无法兑现的 provider 应在有界路径拒发" | 同上（既有 H1 行为，本批未改，也未放松） |

**没有**新起线程、没有"超时后继续出网"的后台任务：停止就是真的停止。

## 三、测试（`test_search_quality_unified.TestBoundedSearchRunner`，全部离线）

| 用例 | 断言 |
|---|---|
| `test_single_read_overrunning_the_deadline_is_not_reported_as_success` | **反例逐字复现**：预算 0.02 秒 + 单次 read 0.12 秒 + EOF → 抛 `TimeoutError`（理由含 EOF），且**不再发起第二次读**；返回了数据的情况同样抛；预算充足时正常读完不受影响 |
| `test_remaining_time_is_applied_to_the_single_read` | 剩余时间确实设到 socket 上，且**不得为 0**（0 = 非阻塞空转），不超过预算 |
| `test_unboundable_response_is_still_stopped_at_the_deadline` | 响应对象**没有**可设超时的 socket 时，时钟检查仍是硬边界 |
| （既有）`test_slow_body_read_stops_at_deadline` | 慢速连续字节在块间被切断，读次数有界 |

## 四、本批**未**覆盖（如实列出）

1. **SDK 整个调用的可终止边界只做到"拒发"**：`ddgs` 是同步 SDK，无法中途取消。
   现有处置是"剩余时间低于可行下限就不发请求"（`provider_min_wait`，
   `test_refuses_to_issue_below_provider_floor` 覆盖）。**没有**做"发出去之后的受控隔离终止"
   （例如子进程 + kill）——那要动执行模型，本批不做也不假装做了。
2. **"根任务 6 次/60 秒及实际停止/清理时间分账"**：既有实现有任务级预算预占与退回
   （`_finish_search` → `_release_search_calls`），本批**未改**；"实际停止/清理时间"
   的分账未新增字段，如实记为未做。
3. 单次读的 socket 超时是**尽力而为**：`_bound_single_read` 走
   `resp.fp.raw._sock → resp.fp.raw → resp.fp → resp` 四条路径，都拿不到就返回 False
   （由时钟检查兜底，有测试）。

## 五、口径声明

全部验证用假响应完成，**未对任何真实站点发起请求**，未消耗搜索额度；
未改门禁/阈值/模型/权限/模板；未删除任何日志、备份或产物。
