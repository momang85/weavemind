# B0 复检与修复（BETA-1023 · 选股研究 Beta）

生成时间：2026-10-23　工作区：`C:\Users\ding0\Desktop\ai_agent\织光`　HEAD `ed8fd90`（未提交、未推送）
运行环境：隔离实例 8083（`tmp/r1next-isolated`，账号 admin/viewer 为本单测试账号），生产 8080 **未重启、未改配置**。

## 0. 结论先行

B0 的六项要求逐条落地，**4 个真实缺陷在复检中被抓出来并修掉**（下面 §3，每条都有修复前后的反例）：

1. 比较口径补齐：实现版本 / 字段口径版本 / **实际评估期**进入比较与归因；「期间模式相同但落到不同年度」不再被当成没变。
2. 数量语义拆开：股票池增删、结果进出、结论翻转（新通过/不再通过）、读数变化、资料变化（新变资料不足/已补齐）各算各的，明细行带原因。
3. 并发与中断：同身份并发执行只产出一份原版；同内容并发上传只得到一个数据集 ID；写入中断不落半个结果、临时目录清干净、可原样重试；错误文案不再误导「换个名字就行」。
4. 来源核验：来源性质（官方缓存=适配器整理的宽表 / 用户上传 / 合成演示）、原始文件指纹、人工复核状态三件事分开显示；接口**只显示状态，不代签**。
5. R2 文档事实性错误已勘误（§6），旧文保留原文作为历史记录。

验证结果：**197 项自动化测试全过（exit 0）**、双账号 22 项 HTTP 走查 22/22、B0 专项 HTTP 走查 25/25、浏览器实测两份对比（截图 01/02）。

## 1. 要求 → 实际落地

| B0 要求 | 实际做法 | 证据 |
|---|---|---|
| 比较对象要有「比较版本 / 实际评估期 / 数量语义」 | `_CONDITION_ORDER` 扩到 8 项：规则、股票池、期间口径、**实际评估期**、**计算实现版本**、**字段口径版本**、资料截止日、数据来源；归因与「不可直接归因」同步扩到版本/评估期 | `screen_store.py:1390`、`compare_runs:1557`；截图 `01-b0-compare-evalperiod.png` |
| 实际评估期（不是模式） | 每次结果按年统计行数 `2024年×1`；期间口径只描述策略本身（模式+要求年度），实际年度单独比 | `_eval_period_counts:1429`、`_period_text:1492` |
| 数量语义分开 | 三行计数：股票池（新增/移除）｜结果里（新增/取消/两份都在）｜筛选结论（新通过/不再通过/读数变化/期间不可比 / 资料变化）；明细 `reason_kind` + 中文解释 | 截图 `02-b0-compare-pool-and-verdict.png` |
| 并发 / 中断 | 6 线程同身份执行 → 1 份原版、5 次 `reused`；5 线程同内容上传 → 1 个 ID；拦截写盘异常 → `StoreWriteError`、无结果目录、无临时残留、重试成功 | `test_screen_store.py:1130`（`TestConcurrencyAndInterrupt`） |
| 来源核验 | `provenance{kind,kind_label,file_sha256,origin_ref,source_urls,original_verified:false,human_review:{status:"not_reviewed",by:null,at:null}}`；界面写「文件指纹」+「核验：人工复核未开始（接口只显示状态，不代签）」 | `screen_store.py:181`；`StockScreen.tsx` `sourceLine()` |
| 修 R2 能力/缺口描述 | 见 §6 勘误表：交易日历有独立源、缺行情≠停牌、Qlib 是未安装的委托适配器、公告日与来源定位其实已有 | `R2-minimal-history-experiment.md` 末尾勘误 |

## 2. 本次实际改动

- `screen_store.py`
  - 来源性质 `_provenance()` + `load_source`/`save_dataset`/`dataset_source_meta`/`_run_summary` 全链路带上 `provenance`，`save_dataset` 存 `source_urls`（≤50）。
  - 比较：条件扩到 8 项；`_NON_ATTRIBUTABLE_KEYS` 6 项；版本键空值视为「不同」；`_period_text` 只描述策略（模式+要求年度），实际年度由 `eval_periods` 单列。
  - 数量：`_run_summary` 带 `counts`；`compare_runs` 的 `summary` 分 `pool_added/pool_removed/entered/exited/stayed/stayed_changed/stayed_unchanged/verdict_flipped/became_pass/left_pass/insufficient_new/insufficient_resolved/pass_from_insufficient/fail_from_insufficient/not_comparable`。
  - 写入：`_write_one` 的 IO 异常收敛成 `StoreWriteError`（不再冒裸 OSError，也不留半个结果）；数据集两条撞 ID 的文案改成「换名称不会得到新 ID」，不再误导。
- `web_ui.py`：`/api/screen/datasets` 的 `official_source` 加 `provenance`（之前界面拿不到来源性质）；`_write_body` 吞掉客户端断开（上一轮的修复在本轮复测中仍生效）。
- `frontend/src/pages/StockScreen.tsx`：来源行显示性质/文件指纹/复核状态；条件徽标把 6 项列为不可直接归因；计数拆三行；进入/退出明细显示原因；读数随评估期变化标「期间不同：读数不可直接比较」。
- 测试：`test_screen_store.py` +13（比较语义 5、来源性质 3、并发中断 5），`test_screen_api.py` +1（断开容忍）——本轮之前已在。共 **197**（59+77+34+27）。

## 3. 复检抓到的缺陷（反例 → 修复）

| # | 反例（修复前真实表现） | 根因 | 修复 |
|---|---|---|---|
| 1 | 同一份规则、同一只股票、只把「统一报告期」的数据从两年换成只有 2023 年：对比显示**全部条件相同**、读数直接相减 | `period_policy` 文本把「模式」和「实际年度」揉在一个字段里，且比较不看实际年度 | 实际评估期单列成条件；模式相同也照样标「实际评估期不同 → 不可直接归因」，读数标不可横比 |
| 2 | 磁盘满/写盘报错时，`execute` 抛出裸 `OSError`，调用方拿不到「可原样重试」的结论 | `save_run` 只包了 rename，没包写文件 | 收敛成 `StoreWriteError`，文案写明「未写入任何结果；临时目录已清理」；测试断言无结果目录、无 `.tmp-*` 残留、重试成功 |
| 3 | 数据集目录 meta 损坏时提示「请更换名称」——但 ID 是**内容指纹**，换名字还是同一个 ID，用户照做只会再撞一次 | 文案沿用「按名字寻址」的旧心智 | 「数据集按**内容**定 ID，换名称不会得到新 ID；请修复该目录或联系管理员移走」；加测试锁住这句话 |
| 4 | 界面拿不到官方缓存的来源性质（接口没返回 `provenance`），只能显示「official」字样 | `/api/screen/datasets` 手拼 `official_source` 时漏字段 | 补 `provenance`；界面显示「适配器整理的宽表｜文件指纹 …｜核验：人工复核未开始（接口只显示状态，不代签）」 |

另外确认了两件**没坏**的事：对比全程只读（两份 `result_hash`/`snapshot_id` 前后一致）；同内容不同名字的数据集仍是同一 ID 且返回 `reused=true`（这是内容寻址的正确行为，只是文案要说清楚）。

## 4. 未验 / 边界（不掩饰）

- 并发只测了**同进程多线程**。两个服务进程共享同一 `WEAVEMIND_SCREEN_DIR`（例如部署时挂同一卷）没有测，也没有跨进程锁；现部署形态是单进程，若将来多进程共用目录，需要先补文件锁。
- Windows 下 rename 被占用/杀软扫描的瞬时冲突只做了 6 次退避重试，**没有**在真实网络盘/NAS 上验证。
- 浏览器只走了「已有结果 → 对比」；上传与执行链路在 B1/B2 再走（B0 不承诺全流程）。
- 全程未调用模型、未联网抓数、未计费；来源核验只到「文件指纹 + 声明」，**没有任何一条被标成人工复核通过**（也不允许代理代签）。
- 生产 8080 未重启（`build.restart_required: true` 仍在），前端只构建到 `frontend/dist-screen-check`。

## 5. 复现与真实退出码

```bash
export PYTHONIOENCODING=utf-8 HOME='C:\Users\ding0'
C:/Python314/python.exe -m unittest test_stock_screen   # Ran 59  OK      EXIT=0
C:/Python314/python.exe -m unittest test_screen_store   # Ran 77  OK      EXIT=0
C:/Python314/python.exe -m unittest test_screen_api     # Ran 34  OK      EXIT=0
C:/Python314/python.exe -m unittest test_auth_audit     # Ran 27  OK      EXIT=0
npx tsc -b && npx vite build --outDir dist-screen-check # built in 4.14s EXIT=0
C:/Python314/python.exe tmp/e2e_test_accounts.py        # 22/22 PASS      EXIT=0
C:/Python314/python.exe tmp/b0_walk.py                  # 25/25 PASS      EXIT=0
```

日志：`logs/b0-tests.log`、`logs/b0-build.log`、`logs/b0-e2e-22.log`、`logs/b0-http-walk.log`。
B0 走查用的三份结果（隔离库）：`aca8b9ca…`（两年数据，评估期 2024 年）、`2af057f5…`（只有 2023，评估期 2023 年）、`1bb08028…`（池加三一，三一计入资料不足）。

界面核对（浏览器实测，非接口替代）：

- 截图 01：同一规则、同一只股票、同一期间模式，仅数据年份不同 → 归因「不可直接归因」、理由「实际评估期变了：2023年×1 → 2024年×1」、条件表「实际评估期 不同」、读数行「期间不同：读数不可直接比较」、计数「期间不可比 1」。
- 截图 02：同一数据、同一规则，股票池加一只 → 「股票池 不同」、「池新增 1｜结果里新增 1」、结论行「新通过 0」、明细「股票池这次新增了这家公司（不是条件变化导致的入选）」。

## 6. R2 文档勘误（原文保留，作为历史记录）

| R2 原文 | 事实 | 证据 |
|---|---|---|
| 「交易日历靠行情日期推导，无独立源」 | 有独立读侧适配器 `adapters/trading_calendar.py`（schema `weavemind.trading_calendar/0`，`load()/ok()/is_session()`，带 license、dataset_id，覆盖区间外返回 `unknown` 而不猜「休市」），并有抓取脚本 `scripts/fetch_trading_calendar.py`；但**本机没有日历数据集目录**，所以「能力有、数据无」 | `adapters/trading_calendar.py:2-20`、`:123-156`；`ls trading_calendar` 为空 |
| 「缺行情 = 停牌」（隐含） | 原型明确区分：有独立日历时，标的当日无行情按停牌处理并留拒绝记录；无日历时用基准日历当代理并标注「疑似」；两者都不等同于休市 | `quant_research/backtest.py:213-233` |
| 「Qlib 委托实现」写得像已有 | `quant_research/qlib_adapter.py` 只是**探测 + 未安装时返回原因/安装指引**；主环境不装 Qlib，`run_via_qlib` 的委托路径**从未运行过** | `quant_research/qlib_adapter.py:9-11`、`:26` |
| 「缓存里两家公司只有年报值，无公告日、无更正版本」 | 6 行里**有** `available_at`（可用时点）、`disclosed_at`、`source_url`（cninfo PDF 原文）、`material_ids`/`orig_fact_ids`（各 15 条事实）、`period_basis`/`caliber`；缺的是更正/重述链（`restatement` 全空）、全市场覆盖、PIT 成分与 ST/退市标记 | `data/screen_cache/official_disclosure_cache.json`（sha256 `4dc828ef0bea…`，6 行） |
| 「无历史日线库；adapters 能抓当日行情但未落历史序列」 | 成立，补充：`adapters/market_history.py` 已是「导入/读取 + license + adj_basis」的读侧能力，`Dockerfile` 不复制 `data/`、`data/screen_cache` **不进 git**，所以干净检出/镜像里连这 6 行都没有 | `adapters/market_history.py:14,29`；`git ls-files data/screen_cache` 为空 |

其余结论（无 PIT 池、无指数基准、无许可声明、不能用两家公司算 IC、不在主环境装 Qlib/RQSDK、不用今日池回填历史）**保持不变**。
