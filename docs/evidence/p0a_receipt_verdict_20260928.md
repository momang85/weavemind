# P0-a：收执裁决必须区分「异常」与「缺行」（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §2 第 2 条
「错误不是缺行」。基线 `65a6b62`。

## 缺陷（可离线复现）

`task_state.promote_received()` 在**数据库异常**时返回 `"absent"`：

```python
except Exception as exc:
    logger.warning("任务 %s 收执推进失败：%s", ...)
    return "absent"          # ← 异常被伪装成"没有收执"
```

调用方 `orchestrator_v2.accept_task_request()` 对非 `promoted/already` 的裁决走
**旧路径** `mark_queued()`。后果（与指令给的内存反例一致）：

- 一条**正在 RUNNING** 的任务遇到一次连接异常 → 仍返回 accepted 且无 `_skip_run`
  → 允许再次启动（重复执行、重复付费）；
- 收执明明还在库（RECEIVED），却因为一次读/写异常被当成"老路径请求"，
  绕过唯一执行权裁决点。

## 修复

**`task_state.promote_received()` 返回四种裁决**（异常独立成项）：

| 裁决 | 含义 | 调用方义务 |
|---|---|---|
| `promoted` | 本次赢得执行权（RECEIVED→QUEUED） | 执行 |
| `already` | 行存在但已不是 RECEIVED（含 RUNNING/终态） | **不执行**（置 `_skip_run`） |
| `absent` | **确实没有这一行**（老路径/直接调用编排器） | 走旧路径登记 |
| `error` | **数据库异常**（推进失败，或推进未中而**读**也失败） | **拒绝本次执行**、保留恢复状态 |

- `absent` **保留**（既有语义与测试 `test_promote_absent_when_no_receipt` 依赖它），
  只把"异常"从它里面摘出来；
- 读失败同样报 `error`：**读不出来 ≠ 没有收执**，不得据此放行重复执行。

**`orchestrator_v2.accept_task_request()` 增加 `error` 分支**：

```python
elif verdict == "error":
    ok, reason = False, "收执推进失败：任务库异常（已拒绝执行，保留恢复状态）"
    _set_task_ack(orch, task_id, "rejected:receipt_error")
    return ok, reason          # 绝不落回 mark_queued 旧路径
```

收执**留在 RECEIVED**，等下一次启动恢复再试（不丢请求、不重复执行）。

## 定向验证

新增 5 项：

1. `promote_received` 遇 DB 异常 → `"error"`，且显式断言 **≠ `"absent"`**；
2. 推进未中 + `read_task` 抛异常 → 同样 `"error"`；
3. RUNNING 任务的同一请求再次到达 → `"already"`（且状态仍是 RUNNING，不被改回 QUEUED）；
4. `accept_task_request` 遇 `"error"` → `ok=False`、**不置 `_skip_run`**、reason 含"收执推进失败"、
   收执仍是 RECEIVED（恢复状态保留）；
5. 既有 3 项断言从旧原因串 `"登记失败"` 更新为 `"收执推进失败"`——
   场景（库不可写必须 rejected、不得假成功、不得留假时间线）与断言强度**不变**，
   改的只是"在哪里被拒"：现在是**更靠前**的收执裁决点。

结果：`python -m unittest test_task_persistence` → **36 项 OK**。

## 顺带修掉的假 ERROR（同类缺陷，非 P0 文件）

`test_task_persistence.TestDbPathResolution.test_state_writer_reads_the_same_file_in_fresh_process`
一直报 ERROR。真因不是沙箱，而是**测试自身缺陷**：

```
UnicodeDecodeError: 'gbk' codec can't decode byte 0xb6 in position 38
```

`subprocess.run(..., text=True)` 未钉编码 → 本机 GBK 默认编码 + 非 ASCII 路径（`织光`）
下解码在读线程崩掉 → `proc.stderr` 为 `None` → 断言退化成 `TypeError`。

修 3 处调用（补 `encoding="utf-8", errors="replace"`）：
`test_task_persistence.py` ×2、`test_delivery_chain.py` ×1（后者不在本 P0 文件内，
一并说明，因为它属同一缺陷类）。

**连带发现（未修）**：`test_startup_readiness.test_windows_tool_output_is_decoded_with_replace`
这道守卫**只扫 `launcher.py` 与 `dep_check.py`**（产品码），测试文件里的同类缺陷无人拦
——本轮 3 处即由此漏出。

## 未验项

- **未做真实 Redis 双进程验证**（本批不杀用户服务、不改用户 Redis 配置）；按指令，
  两进程跨租期的验证在 P0-b（租约）与 P0-c（恢复）完成后一起做，用 provider 计数替身。
- **P0-b（真实租约）、P0-c（QUEUED 崩溃窗口恢复）、P0-d（同键并发原子绑定）、
  P0-e（时间线分段）尚未实施**，因此"同键并发只留一个可执行收执"**仍未验证**。
- 本轮未跑任何付费整链，只跑单元测试。
