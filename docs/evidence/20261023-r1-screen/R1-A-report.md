# R1-A 阶段报告（可解释选股 MVP）— 纯筛选核心 + 输入快照

任务：R1-SCREEN-1023（`docs/员工任务_R1可解释选股MVP_20261023.md` §3-A）
日期：2026-10-23　执行：lead（Team Lead）
基线：HEAD `ed8fd90`（生产 8080 实例 `inst-cf0d8d8dd0` 运行中，未重启、未停机）。

## 1. 本阶段交付物（全部新增文件，未触碰既有文件）

| 文件 | 行数 | 说明 |
|---|---|---|
| `stock_screen.py` | 859 | 纯筛选核心：字段映射、输入快照、规则、读数、三态评估 |
| `test_stock_screen.py` | 576 | 48 个用例（正/反/未知/冲突/零分母/负利润/单位/身份/边界/PIT/确定性/真实缓存） |
| `data/screen_cache/official_disclosure_cache.json` | — | 真实官方缓存投影（10297 字节，sha256 `4dc828ef0bea63ac…`），逐字节来自 `tmp/q1-real-inputs/financials.json`，原文件未动 |

差分证据：`docs/evidence/20261023-r1-screen/R1-A.diff`（真实 `git diff --no-index`，69769 字节）。
测试日志：`R1-A-tests.log`（真实退出码 **0**，48 tests OK，0.028s）。

## 2. 字段清单与来源契约（R1-A 第 1 项）

### 2.1 可筛选字段 v1（`FIELD_SPECS`，5 个，全部有真实数据支持）

| field_id | 标签 | 类型 | 单位 | 口径/公式 | 身份声明 |
|---|---|---|---|---|---|
| `revenue_growth` | 营业收入同比增速 | pct | % | (本期收入−上期收入)/上期收入×100 | 上一期缺失或非正 → **不适用**（不编造增速） |
| `gross_margin` | 毛利率 | pct | % | gross_profit / revenue | 毛利 = 营业收入−营业成本（官方三表口径） |
| `operating_cashflow` | 经营活动现金流净额 | amount | 元 | 直读 | 负值是真实读数不是缺失 |
| `debt_ratio` | 资产负债率 | pct | % | total_liabilities / total_assets | 复用 `RATIO_MODELS` 既有口径 |
| `cashflow_coverage` | 经营现金流/归母净利润 | pct | % | operating_cashflow / **net_profit** | **归母**（`facts.CORE_METRICS` net_profit 语义），不用 `net_profit_consolidated` 顶替 |

单位换算走 `financial_analysis.contracts._money_scale`（元=1 / 万=1e4 / 亿=1e8），不另设第二套。

### 2.2 纳入快照的金额字段（15 个 wide 列 → 长表观察）

revenue / operating_cost / gross_profit / net_profit / net_profit_consolidated /
operating_cashflow / rd_expense / total_assets / total_liabilities / accounts_receivable /
inventory / accounts_payable / admin_expense / selling_expense / taxes_and_surcharges。
显示标签复用 `facts.metric_label`，期间角色复用 `contracts.period_role_of`
（未声明角色的指标不收，不猜）。fact_id 走既有 `facts.make_fact_id` 稳定 ID 约定。

### 2.3 数据来源（R1-A 第 2 项）

- **真实缓存**：`data/screen_cache/official_disclosure_cache.json` = 官方披露缓存投影
  （origin: `tmp/q1-real-inputs/financials.json`，q1_real_study.py 投影链产出）。
  仅 **2 家公司 × 4 期**（洋河股份 002304.SZ 2022–2025、三一重工 600031.SH 2023–2024），
  带 source_url（cninfo 原文 PDF）、disclosed_at/available_at、unit=元、caliber=合并、CNY。
  这是本机当前唯一的真实输入；**不假装是全市场样本**。
- **用户上传**：同一 wide 行契约（validate_payload 同一校验），标记 `user_provided`，
  永远显示「用户提供，未独立核验」，不自动升级为官方证据。
- **合成样例**：只存在于测试与演示数据，处处带「合成演示」标注（evaluate 注记 +
  快照 synthetic 标志），不进真实候选。

### 2.4 期间类型归一（发现的既有约定差异，显式映射）

输入缓存用 `period_type="年度"`，`financial_analysis.dataset._ANNUAL_TYPES` 认
`("", "10-K", "年报", "annual")`。`stock_screen._period_type_of` 做显式词汇归一
（年度/年报/annual/10-K → 年报），半年报/季报原样保留（会被年度身份判定拒绝，不冒充年报）。

## 3. 输入快照（R1-A 第 3 项）

`build_snapshot()`：池过滤（`facts.market_of_code`：cn 收、hk/us/裸短代码拒并说明）
→ PIT 过滤（**缺 available_at/disclosed_at → 待核验，不参与筛选**；可用时点 > as_of →
排除「不倒灌」；period_end > as_of → 排除「期间尚未披露」）→ 逐主体
`financial_analysis.dataset.freeze`（口径不回退、冲突不择一、血缘传播、期间身份全部复用）。
快照身份 `snapshot_id` = sha256(schema + impl + 字段映射版本 + 池 + as_of + 输入字节哈希 + 各数据集哈希)。
**已验证**：截断未来输入不改变过去快照哈希（test_future_row_does_not_change_past_snapshot_hash）。

## 4. 测试与手算对照（R1-A 第 4 项）

48 用例全绿（exit 0）。手算基准（合成）：收入 200→220（+10%）、毛利 50/200（25%）、
负债 80/资产 200（40%）、现金流 30 / 归母 25（120%），合并净利 27 不得顶替归母 25 ——
全部逐项断言通过；显示量化不影响比较（0.3600001 亿阈值 vs 0.36 亿读数 → 未通过）。

真实缓存冒烟（as_of=2025-04-30，规则五条）：

| 读数 | 三一重工 2024 | 洋河股份 2024 | 手算核对 |
|---|---|---|---|
| 营业收入同比增速 | 6.22% | −12.83% | ✓（777.7339/732.2173；288.763/331.2628） |
| 毛利率 | 26.43% | 73.16% | ✓（205.5643/777.7339；211.2508/288.763） |
| 资产负债率 | 52.02% | 23.24% | ✓ |
| 现金流/归母净利 | 247.92% | 69.36% | ✓（148.1428/59.7545；46.2871/66.7339） |
| 经营现金流净额 | 148.14 亿 | 46.29 亿 | ✓ |

- 入选 0 / 未通过 2 / 资料不足 0 —— **诚实零入选**（三一毛利率不达标、洋河增速为负）。
- PIT 边界：as_of=2025-04-17 时三一两年报（2025-04-18 披露）均不可用 → 资料不足 ✓；
  洋河 2025 年报（2026-04-28 披露）晚于截止日 → 排除 ✓。
- 确定性：同输入重跑 result_hash 一致 ✓；耗时 load 0.5ms / snapshot 2.2ms / evaluate 0.4ms
  （远低于 §4 的 5s 预算）。

## 5. 修复的自身缺陷（写测试时发现并当轮修掉）

1. `evaluate` 引用了不存在的 `snapshot.pending_rows` → 补为逐主体 `pending_pit` 聚合属性；
2. 排序 `reverse=True` 会把「缺读数」组翻到最前 → 改为两遍稳定排序（缺读数永远最后，平局按代码）；
3. `load_payload_file` 内联 `__import__` hack → 顶层 `import os`，显式 with 关文件；
4. `_amount_reading` 死分支、未用 `_sort_key_for`/未用 import → 清除；
5. 全部界外时错误只给笼统提示 → 逐条给出「代码 期间：原因」。

## 6. 未决 / 越界不做（如实记录）

- 真实输入只有 2 家 × 4 期：**不扩大宣称**；更多公司待用户提供或后续真实缓存接入。
- 净利润身份：`cashflow_coverage` 用归母是**字段定义**，不是运行时猜测；测试锁定。
- 快照/结果的持久化（R1-B）：`InputSnapshot.to_dict()` / `ScreenResult.to_dict()` 已可序列化，
  存储位置与「查看原版 vs 重新筛选」语义按 R1-B 实现（工作区根持久目录，不进 TEMP）。
- 本阶段未改 `web_ui.py`、未建路由、未动前端、未发网络请求、未调用任何模型/搜索/付费适配器。
