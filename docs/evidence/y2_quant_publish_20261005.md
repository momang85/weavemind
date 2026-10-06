# Y2 接入证据：量化读数以**附件**进交付包 ＋ 许可门（2026-10-05）

> **边界**：0 元 API、未联网、未调模型、未改主文、未改权限/预算；
> 价格许可为**免费源·仅内部试验** ⇒ 许可门判 **不可对外**，原始数据不得随包外发。

## 一、接入方式：附件，不是改写主文

规划 §11.2 要求"先接正常选版/同版附件，旧主文只有限保留必要结论与边界"。本轮就按这条做：
**只加附件**，报告链正文与实际成稿逻辑**一行未改**。

| 环节 | 实现 | 可检查字段 |
|---|---|---|
| 落盘 | `quant_research.publish.store_reading()` → 工作区 `quant/event_returns.json`、`quant/backtest.json` | 未登记的读数名**拒写**（不随手堆文件） |
| 随包 | `delivery_pipeline._freeze_payload` 里按 **`analysis/*` 同一套契约**收 `quant/*`（**有才进包、不制造空文件**） | 没有量化读数的工作区，包成员**一字不变**（测试锁住） |
| 清单 | `quant/quant_manifest.json`：每条读数的 operator/status/`input_kind`/指纹/哈希/`delivery_eligible` | 离线可核对"这份包里的数字是哪次读数" |
| 底稿 | `quant/quant_detail.md`：**数字留底稿**（正文只放结论），含输入绑定、哈希、许可门结论 | 坏文件跳过并记 `unreadable`，不让一个坏文件毁掉整包导出 |

## 二、许可门（能不能对外）

`external_delivery_gate()` **按每条读数自带的 `delivery_eligible` 判**，不重新解释许可、
也不因为"我们内部看过了"放行：

- **任一读数不可对外 ⇒ 整体不可对外**（不是"多数通过就行"），`blocked_by` 列出是哪几条；
- **没有读数也不默认放行**（`allowed=False` + 原因）；
- 实机结论：`allowed=false`，原因逐条写明"免费源·仅内部试验 ⇒ 仅内部试验，不得进对外下载包；
  对外交付须机构授权或只交付衍生结论"。

## 三、真实数据端到端（`scripts/x1_quant_publish_run.py`）

数据集 `3d6b4acb…`（002304＋600031＋000300，前复权，免费源仅内部试验）：

| 读数 | 状态 | 输入指纹 | 读数哈希 |
|---|---|---|---|
| `event_returns_v1`（3 个已核对事件） | ok | `3dfabdcac371…` | `98fbc4cdc4b6…` |
| `backtest_v1`（冻结规则） | ok、**audit_ok=true** | `3dfabdcac371…` | `15cf961e801f…` |

- 交**交付链同一个函数** `_freeze_payload` 取到的成员：
  `quant/backtest.json`、`quant/event_returns.json`、`quant/quant_detail.md`、`quant/quant_manifest.json`
  —— 证明读数**真的进了快照**，不是只躺在磁盘上。
- 回测数字不变：总收益 −38.36%、成交 66 笔、拒绝 24 条、`audit_ok=true`。
  （`reading_hash` 由 `eda147c5…` 变为 `15cf961e…`，因为哈希口径纳入了 `input_kind` —— 见下。）

## 四、本轮被测试逼出的一个真缺口（已修）

`quant_detail.md` 渲染出 `dataset \`None…\``：**回测读数当时没有 `input_kind` 绑定**。
规划 §11.3 明确"Y2 数值接入前再做一次 `input_kind` 绑定/重放"——绑定是接入的前置条件，
缺了它就无法回答"这份回测跑在哪份数据上"。已修：`backtest.run()` 现在与 `event_returns`
**共用同一份绑定实现**（`binding()`/`fingerprint()` 只有一处判据），并加两条测试：
绑定字段齐备、**换数据集必须换读数哈希**（否则"绑定"是摆设）。

## 五、回归风险：实测与归因（不猜）

`delivery_pipeline` 是巨文件（2100+ 行）、被 414 个交付链用例覆盖，所以改完立刻跑相邻套件：

- `test_delivery_chain`：**1 个 ERROR**（`RuntimeError: blocked URL by SSRF guard: https://qt.gtimg.cn/...`）
  —— 网络环境所致（TUN 代理解析成非公网地址）；**同一测试在半小时前的 56/56 全量里是 PASS**。
- `test_offline_delivery`：**1 个 FAIL**（`TestAsyncCallDiagnostics`：`client.calls == 0 != 2`）。
- **归因（实测而非推断）**：把 `delivery_pipeline.py` 单独 `git stash` 回退后重跑该测试，
  **结果完全相同（0 != 2）** ⇒ 与我的改动无关，是网络状态相关的间歇失败。
- 直接覆盖我改动的两个用例 `TestX0PackageAndSidecars`（含
  `test_freeze_payload_includes_referenced_analysis_markdown`）**2/2 PASS**。

**结论**：本轮改动**没有**引入回归；两条环境失败已如实登记，且 CI（Linux、无 TUN 代理）是权威判据。

## 六、CI 抓到一个本地测不出的**真实部署缺陷**（已修）

`delivery_pipeline` 现在会 import `quant_research`，而 **Dockerfile 没把这个目录拷进镜像**：
`test_deploy_manifest.test_runtime_imports_are_in_image` 直接判死 ——
`AssertionError: ['quant_research'] != [] : 运行入口会导入这些本地条目，但 Dockerfile 没拷进镜像`。

- **后果（如果不修）**：容器里 `quant_research` 不存在 ⇒ 那条 `try/except` 会走 `except`，
  于是**线上永远导不出量化附件**，而本地全绿。这正是"本地测试通过 ≠ 能部署"的实例。
- **修法**：`Dockerfile` 增加 `COPY quant_research/ ./quant_research/`（`Dockerfile.sandbox` /
  `Dockerfile.worker` 不拷 `financial_analysis`，同样不跑交付链 ⇒ 不需要）。
- 修后 `test_deploy_manifest` **40 项全过**。
- **教训登记**：往交付链加新包时，必须同时过"运行入口导入 vs 镜像 COPY"这道守卫；
  本轮的本地全量（56/56）**不可能**发现它，因为本机本来就跑在源码树里。

## 七、测试

- 新增 `test_quant_publish.py` **14 项全过**；已挂进 CI（磁盘与 ci.yml 仍一一对应）。
- `test_backtest.py` 26 → **28 项全过**（新增两条绑定测试）。
- `test_market_events.py` 54 项全过。

## 八、边界与待办

- **不宣称**：读数可对外（许可门说不行）、正文已含量化结论（**正文一行未改**，本轮只做附件）、
  因果或 alpha。
- **待办**：正文侧的"有限接入"（按 §11.2 只在主文保留必要结论与边界）需要单独设计并跑
  M0-f 冻结期望，**本轮刻意不碰**——上一轮主文收束就是在这里翻过车（必要边界消失）；
  独立交易日历；涨跌停/排队/冲击成本；Qlib 独立环境实跑。
