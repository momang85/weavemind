# K0 可信输入与取件边界（2026-09-29 夜验收后第一批）

> 执行依据 `docs/阶段Q增量验收与产品闭环推进_20260929夜.md` §二；验收基线 `39ff27b`。
> 三个小提交：`26cd761`（K0-a 口径/契约 + K0-b 抽取器）、`f2ac12f`（K0-c 传输）。
> **本轮无付费整跑、无新外网请求**（缓存回放与本地 socketpair 反例）。

## 0. K0 退出对照

| 退出条件 | 状态 | 证据 |
|---|---|---|
| 口径不回退、跨指标不混算 | 通过 | §1.1（反例由 validated → 拒绝/缺输入） |
| 退路绑定研究契约（主体/两期/口径/as_of） | 通过 | §1.2（三反例 + 缺契约缺口） |
| 抽取器六类静默错数 | 通过 | §2（每条先复现旧读数） |
| 三家公司缓存正确数不退化 | 通过（三家逐位一致） | §2.7 |
| 慢头 / 字节通道 / 压缩上限覆盖实际入口 | 通过 | §3（含实测耗时） |
| 上传默认与披露下载同量级、变量名修好 | 通过 | §3.4–3.5 |
| 每小提交只跑涉及文件；全量核对 | 通过 | §4（CI 同源 53/53） |

## 1. K0-a：可信模型输入

### 1.1 口径不回退（`financial_analysis/contracts.py`）

- `AnalysisDataset.get/require`：显式或默认口径**都只认一个报表范围**；该范围没有该
  (指标, 期间) 就返回 `None`（缺输入提示里附"另有其它口径"线索）。**不再**回退到"唯一那条
  母公司值"。同一口径内多条**身份不同**的观察仍返回 `None`（不任选）。
- `primary_caliber` 计入不可用观察：口径已声明但元数据不可用时报"观察不可用"而非"缺输入"。
- 新增 `report_scope_ok(*obs)`：跨指标要求**范围相同且有声明**；利润桥/现金质量/情景/
  同年比率四处独立调用；金样（独立路径）同样拒绝，避免"compute 拒了、gold 却算出来"。
- 输出说明从已验证输入生成（`assumptions` 不再固定写"合并"）。

**固定反例读数**（合并净利两期 10/12、母公司毛利 30/40、母公司 CFO 20）：

| | 修前 | 修后 |
|---|---|---|
| `get(gross_profit, 2024年, caliber=合并)` | 返回母公司 40 | `None`（+ 提示"另有母公司口径"） |
| `compile_plan` adopted | profit_bridge / cash_quality **均在** | 两者都进 `rejected`（缺输入） |
| `run(profit_bridge)` | **validated** | `missing_input` |
| 同口径数据（`_two_period_rows`） | validated | validated（**未全局关模型**） |
| 合并 CFO ÷ 归母净利（同范围观察比率） | validated | validated（限制说明保留归属层差异） |

### 1.2 `financials` 退路绑定研究契约（`workers/data_analyzer_worker.py`）

退路与底稿走**同一条**契约解析（`working_paper_export.resolve_request`），并逐条落实：

- 契约要素（主体 / ≥2 个年度期间 / 报表口径 / as_of）缺一 → **不跑模型**，返回可行动缺口
  （`status=failed` + `gap`，不落任何 `analysis_runs.json`）；
- 观察级过滤：主体不符（`facts.check_subject`）、口径≠契约、披露日晚于 as_of 的观察**一律排除**，
  排除条数写进数据集缺口；一条都不剩 → 拒绝并说明契约主体/口径/as_of 与被排除原因；
- `freeze` 期间选择：**请求年度没有观察就是没有**（不再 `or _annual` 退回"数据里所有年度"），
  缺口写明"请求年度在数据里没有对应观察：2023（不自动改取别的年度）"。

反例（都在 `test_financial_analysis` 里，先复现旧行为再加断言）：无落库契约、载荷 2024/2025
而请求 2023/2024、错公司（洋河 vs 茅台）、只有母公司口径、披露日晚于 as_of。

## 2. K0-b：抽取器只接受可证明的单元格

| 反例 | 修前读数 | 修后 |
|---|---|---|
| `存货` 空行 + `在建工程 200.00 180.00` | 取成存货 200/180 | 不取（未知行标签=新行边界） |
| `期初余额 期末余额` + 90/100 | 90→2020年、100→2019年（倒年） | 90→2019年、100→2020年（按列序） |
| `2020年1月1日` 列（=上年末） | 截成"2020年"占年末格 | 跳过该列，只留 `2020年12月31日` 那列 |
| 母公司表缺年份/单位 | 跨标题借合并表年份 + "万元" | 拒绝（`no_periods`/`no_unit_evidence`） |
| `单位：元 币种：美元` | CNY | **USD**（币种单列取证，与单位分列） |
| 收入 100/110 冲突 + 成本 60 | 字典择末派生毛利 50 并留 ok | 冲突期**不派生**并记 `derivation_input_conflict`；已消歧的 2023 期照常派生 40 |

存量/流量与完整日期进事实层：新增 `period_kind`（stock/flow，来自列头 token）与
`period_label`（完整列头原文，如 `2020年12月31日`）。

### 2.7 缓存回放（`scripts/k0_replay_cached_tables.py`，离线）

| 公司 | text_hash | 事实 | 观察 | 拒绝 | 与 `report.json` 基线 |
|---|---|---|---|---|---|
| 京蓝 000711.SZ（原版） | `2e511aa8b75f…` | 40 | 44 | 32 | **逐位一致** |
| 洋河 002304.SZ | `a71287d53c7f…` | 28 | 32 | 21 | **逐位一致** |
| 三一 600031.SH | `aeaf2e836e2a…` | 34 | 38 | 29 | **逐位一致** |

模型仍 validated（京蓝 3 / 洋河 4 / 三一 4），conflicts 0，期间与基线相同。
**未复验项**：`evals/a2_official_chain_20260929/000711-corrected/` 只存了 `meta.json`
（正文 `doc.json` 未入库），所以夜验收里"更正稿 38 事实/42 观察、`3f6ee7b8…`"这条本轮
**无法离线复验**——不假装通过；需要重取原件或由审查侧提供该正文缓存后补跑。

## 3. K0-c：真正覆盖完整取件过程

1. **响应头吃根截止**（`net_policy._read_http_response`）：`begin()` 之前先把 `resp.fp`
   包上截止线。实测（本地 socketpair，状态行/头按 8 字节/60ms 滴入，预算 0.2s）：

   | | 耗时 | 报错阶段 |
   |---|---|---|
   | 改前 | **0.483s** | "慢响应体"（头没被管住，指向错误阶段） |
   | 改后 | **0.242s** | "响应头未读完" |

2. **字节通道总截止**（`adapters/transport.get_bytes_via_urllib`）：`resp.read()` →
   `_read_bounded_bytes`（`read1` + 每次读前设剩余 socket 超时 + 读后查钟）。
   旧路径读数：慢体整段读完后返回 `ok=True`（总截止未生效）；新路径
   `error_kind=read_timeout`。"截断 + `over_limit`"契约保留（`too_large` 用例仍绿）。
   两条边界都给不了且给了字节上限 → 退回**一次受限读**（urllib `HTTPError` 形态，985 字节
   403 页照读）；无上限 → 明确 `unbounded_read_refused`，不假装读到正文。
   新增用例直接验**实际入口** `annual_report_pdf.fetch_bytes` 走的是有界通道。
3. **有界解压**：`gzip.decompress` 之后才查上限 → `zlib.decompressobj(16+MAX_WBITS)`
   + `max_length`，解压过程中即查（64 KiB 上限拒绝 4 MiB 压缩炸弹）。
4. **上传上限**：默认 3 MiB → **30 MiB**（与披露下载同量级；4–5 MiB 年报复核可进）。
5. **变量名**：正式名 `WEAVEMIND_UPLOAD_MAX_BYTES` 生效；旧拼写
   `WEAVIMIND_UPLOAD_MAX_BYTES`（少一个 E）**兼容不删**——已按旧名配置的部署不受影响。

## 4. 全量核对（与 CI 同一清单）

`python scripts/local_full_sweep.py`：**53/53 通过**（每文件一进程，与
`.github/workflows/ci.yml` 同一调用方式）。`test_common.py` 在本机因 Windows 控制台
gbk 编码打印 ✓ 失败（ENV，不计；CI 的 Linux 上正常）。

本批涉及文件的读数：`test_financial_analysis` 80、`test_annual_financial_tables` 34、
`test_net_policy` 51、`test_transport_deadline` 20、`test_delivery_chain` 408、
`test_p0` 434、`test_orchestrator_v2` 83、`test_offline_delivery` 36。

## 5. 边界与未做

- 未改用户模型/权限/代理/凭据；未清理脏文件；未动真实库；未追加付费整跑。
- K1（官方 discover 接正常研究任务）、K2（同版可复算交付包）、K3（页面复算）未开始。
- 更正稿缓存正文缺失（§2.7）待补；`launcher.py status` 探活端口 8081 与实际 8080 的
  不一致仍未修（架构师已列为小修）。
