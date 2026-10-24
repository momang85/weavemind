# R2 前置决策：最小历史验证实验与数据缺口（一页纸）

生成时间：2026-10-23　工作区：`C:\Users\ding0\Desktop\ai_agent\织光`（§6 要求：只交方案与缺口，不扩引擎）

## 0. 结论先行

- **本机现在没有可点的历史选股验证数据**：真实缓存只有 `data/screen_cache/official_disclosure_cache.json` 里 6 行、两家公司（洋河 2022–2025、三一 2023–2024）的年度观察，没有历史股票池、没有历史日线、没有公告日与更正版本、没有指数基准、没有数据许可声明。
- **已经具备的是「账本与判据」而不是「数据」**：`quant_research/backtest.py` 已实现下一交易日开盘成交、T+1、现金不足/停牌无行/不足一手进 `rejections`、成本前后分列、印花税按生效日、账本审计（现金守恒/净值恒等）、留出区间固定进 spec、无效结果如实记 `invalid`；`event_returns` 提供输入绑定与指纹；`event_calendar.py` 能把披露事件归一成日历；`financial_analysis` 有 Observation 状态机与期间标签。
- **因此本批不启动任何历史收益结论**。第一批实验的规模被锁在「同池等权 vs 明确市场基准」的冻结规则对照上，且必须先补齐缺口表第 1–4 项。

## 1. 第一个实验（目标：可复算的对照，不是找 alpha）

| 项 | 冻结口径 |
|---|---|
| 规则 | 直接取 R1 已冻结的筛选规则（白名单 5 字段：营收增速 / 毛利率 / 经营现金流 / 资产负债率 / 现金流覆盖），**不重新拟合、不调阈值** |
| 池 | 必须是**当时可得**的 PIT 池；没有 PIT 池则不跑（禁止用今天股票池回填历史） |
| 权重 | 等权，月度再平衡 |
| 成交 | 信号 = 前一交易日收盘，成交 = 本交易日**开盘** |
| 成本 | 佣金 2.5bp（最低 5 元）+ 过户费 0.1bp + 滑点 5bp + 印花税按生效日（2023-08-28 起卖出 0.05%） |
| 基准 | 与池规模/行业匹配的单一指数历史收盘（如沪深 300 或中证 500，二者选一并在 spec 里写死） |
| 输出 | 成本前后年化、最大回撤、换手、暴露（行业/规模/期初现金）、拒单明细、无效原因 |
| 留出区间 | `holdout_start` 事先写进 spec；留出段只在最后跑一次，不参与任何调参 |
| 验收 | 账本审计通过；输入指纹 + `spec_hash` 可复算；无成交记 `invalid` 而不是 0 收益 |

第二阶段（**只有**第一阶段的补件到位且结果可复算才做）：分组收益 → RankIC → 前滚检验。机制复用同一套账本与拒单语义，不新写撮合引擎。

## 2. 数据缺口表（现状 = 本机实际可点的东西）

| # | 需要什么 | 本机现状 | 可用性 | 补件与验收标准 |
|---|---|---|---|---|
| 1 | 历史股票池（PIT 成分、上市/退市日、ST 与更名、停牌状态） | 无。股票池是 R1 用户勾选的当前代码（缓存内只有 2 家） | **unavailable** | 按月（或事件驱动）快照的成分表 + 上市退市日 + ST 标记 + `membership_source`；补件需带许可，验收：任取一个月末，能用当时快照重建池且与今日池不同 |
| 2 | 当时可知的财务版本（报告期 + **公告日** + 更正/重述链） | 部分。`financial_analysis` 有 Observation/State 与期间标签；缓存里两家公司只有年报值，**无公告日、无更正版本** | **部分可用** | 每条观察补 `announcement_date`、`source_doc_id`、`version/restated_from`；验收：信号日只允许使用 `announcement_date ≤ 信号日` 的版本，重述不得改写历史筛选 |
| 3 | 原始行情与公司行动（日线 OHLCV、复权因子或分红送转事件、停牌） | 无历史日线库。`adapters/` 能抓当日行情，但未落历史序列；回测原型要 `payload.data` 才跑 | **unavailable** | 日线 + 复权口径二选一（`adj_basis: none` + 显式分红，或 `qfq`，不可混用）+ 停牌标记 + 许可；验收：随机抽 3 个交易日与公开行情核对，复权后价格连续且分红只计一次 |
| 4 | 交易日历与披露日历 | 部分。`event_calendar.py` 可把披露事件归一；交易日历靠行情日期推导，无独立源 | **部分可用** | 独立交易日历（含临时休市）+ 财报预约披露日；验收：节假日/临时休市不生成交易日，信号与成交日不落到非交易日 |
| 5 | 证券级交易约束（涨跌停、T+1、一手、ST 限买、停牌） | 部分。T+1、一手、停牌无行、现金不足已在原型里，**缺涨跌停价与 ST 限买** | **部分可用** | 每日涨跌停价 + ST 标记；验收：开盘价封涨停时买单进 `rejections`，不静默按开盘价成交 |
| 6 | 基准（指数历史收盘与成分来源） | 无 | **unavailable** | 指数日线 + 指数名称与口径说明；验收：基准与组合同一交易日历、同一区间，缺失日显式标注 |
| 7 | 成本参数与生效日 | 可用。`DEFAULT_COSTS` 已有佣金/最低/过户费/滑点/印花税生效日表 | **可用** | 仅需在报告里写明用的是哪一版成本；验收：改动任一成本参数必须换 `spec_hash` 并重出账本 |
| 8 | 数据许可（内部研究 / 可展示 / 不可再分发） | 无。原型有 `license` 字段但当前为空字符串 | **unavailable** | 每份输入带许可声明与到期日；验收：页面只展示许可允许展示的字段，越界数据不出现在报告与截图里 |
| 9 | 留出区间与防事后挑选 | 机制可用。`holdout_start` 在 spec 里，规则无拟合参数 | **可用** | 无需补件；验收：留出段只在最终一次运行，运行记录里能看到区间与 `spec_hash` |

## 3. 最小数据协议（补件到位后按此接入，不新增依赖）

```
prices:    {schema, adj_basis: "none"|"qfq", data: {code: [{date, open, high, low, close, volume, amount}]},
            suspended: {code: [date...]}, limit: {code: [{date, up, down}]}}
dividends: {code: [{date, cash_per_share, shares_ratio}]}          # 仅 adj_basis = none
calendar:  {trading_dates: [date...], disclosure: [{code, period, announce_date}]}
universe:  {as_of: date, codes: [...], membership_source: str}      # 逐期快照，不是今天的池
financial: [{code, period, announcement_date, version, state, values{5 白名单字段}}]
benchmark: {code, name, series: [{date, close}], source: str}
license:   每份输入必带 {scope: "internal"|"displayable"|"no_redistribute", expires: date}
```

## 4. 防作弊与边界（本单明确不做）

- 不用今天的股票池回填历史，不用两家公司算 IC 或宣称「稳定 alpha」。
- 不在主环境安装 Qlib/RQSDK、不扩主环境依赖：`qlib_adapter.available()` 为假时给原因与安装指引，不编造委托实现。
- 不新写完整撮合引擎；现有 `quant_research/backtest.py` 作**回归参照**，未来引擎须在锁定独立环境、用同一输入跑出同一账本（对账脚本先于引擎）。
- 不做模拟组合（另单）、不上机器学习（更后）、不展示买卖点或自动调仓。

## 5. 何时才允许给用户看收益

三个门槛同时满足：① 缺口表第 1–4 项补齐且带许可；② 第一实验在留出区间复算一致（同 `spec_hash` 同账本）；③ 页面同时展示「用了哪份数据、哪段区间、哪些缺失」。
没满足之前，页面只说「本工具当前能做什么」，**不出现任何收益曲线或胜率**。

## 6. 与产品定位的对齐

「个人投资者 / 小团队 / 低年费」下，这一段的价值不是承诺跑赢，而是**知道自己缺什么、能复算什么**：缺口表本身就是给用户的透明交付；补件可以逐步来，但结论不能提前说。同理，我们不声称市场第一，也不承诺效果——「最合适」是要靠这类可复核的小实验一点点验证的目标。

---

## 7. 勘误（BETA-1023 · B0 复检，2026-10-23）

> 上文作为历史记录**原文保留**；以下为复检后的事实更正，验收以本节为准。完整核对见 `docs/evidence/20261023-screen-beta/B0-review.md` §6。

| 原文 | 更正 | 证据 |
|---|---|---|
| §2 第 4 行「交易日历靠行情日期推导，无独立源」 | 读侧**有独立交易日历** `adapters/trading_calendar.py`（`weavemind.trading_calendar/0`，覆盖区间外返回 `unknown`，带 license），抓取脚本 `scripts/fetch_trading_calendar.py` 也在；**但本机没有日历数据集**（`trading_calendar/` 目录不存在）。缺口是「数据未落地」，不是「没有能力」。 | `adapters/trading_calendar.py:2-20,123-156` |
| §2 第 3 行把「标的当日无行情」笼统当停牌 | `quant_research/backtest.py` 已区分三种口径：有独立日历 → 无行情按停牌并留拒绝记录；只有基准 → 用基准交易日当代理并标「疑似」；都没有 → 标的并集且声明停牌日会消失。不能把「缺行情」等同于「停牌」。 | `quant_research/backtest.py:213-233` |
| §4「Qlib 只作为适配目标放在 `qlib_adapter.py`」 | 表述正确，补充：该适配器只有 `available()` 探测与未安装时的原因/指引，委托运行路径 `run_via_qlib` **从未执行过**；主环境不装 Qlib/RQSDK 的约束不变。 | `quant_research/qlib_adapter.py:9-11,26` |
| §2 第 2 行「缓存里两家公司只有年报值，无公告日、无更正版本」 | 6 行里**有** `available_at`、`disclosed_at`、`source_url`（cninfo PDF）、`material_ids`/`orig_fact_ids`（各 15 条事实）。真正缺的是：更正/重述链（`restatement` 全空）、全市场覆盖、PIT 成分与 ST/退市标记。 | `data/screen_cache/official_disclosure_cache.json` |
| §2 第 3 行「adapters 能抓当日行情但未落历史序列」 | 成立；补充 `adapters/market_history.py` 已有导入/读取 + license + adj_basis 的读侧契约，缺口同样是「没有数据文件」；`data/screen_cache` 不在 git、`Dockerfile` 不复制 `data/`，干净检出/镜像里这 6 行也不存在。 | `adapters/market_history.py:14,29`；`git ls-files data/screen_cache` 为空 |

其余结论（无 PIT 池、无指数基准、无许可声明、不用两家公司算 IC、不用今日池回填历史、三个门槛未满足前不展示收益）**不变**。
