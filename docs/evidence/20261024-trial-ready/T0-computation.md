# TRIAL-READY-1024 · T0 计算一致性（后端修复）

- 日期：2026-10-24；执行者：pkg-hygiene（DeepSeek Harness，tierflow_pro）
- 运行时：本机 Python `3.14.3`（`C:/Python314/python.exe`；生产目标 Python 3.11 本机不可用，见「范围外」）
- 方法：离线合成数据（`600031.SH` 「合成复核公司（非真实财务数据）」）+ mock 传输 + `tempfile.mkdtemp(prefix="t0-")` 隔离目录；env 先设、模块后导入（与 `architect_probes.py` 同一做法）。不提交、不联网、不碰 8080。
- 修复前证据：`docs/evidence/20261024-trial-ready/architect_review.json`（findings G1–G4 `reproduced: true`）与 `architect_probes.py`（只读，未改动）。
- 修复后实测：`tmp/t0-after-readings.py` → `tmp/t0-after-readings.json`（与探针同一输入重放，当前代码输出）；新回归 `test_trial_t0.py`（21 用例全过）。

## 一、修复前（复核原判据，两套判据是根因）

| # | 复核发现 | 修复前实测（architect_review.json） |
|---|---|---|
| G1 | 同比按 manifest 相邻位/「更早期间里最大」取上一年：只有 2022（收入 100）与 2024（收入 150）、**没有 2023** 也给出同比 | `revenue_growth`：`status="ok"`、`value=50`、`display="50.00%"`、`verdict="pass"`、`reason="营业收入同比增速 50.00% > 40%：满足全部条件"`、`dataset_hash=a18230ee…bbd7b`（输入哈希随输入，相同输入的 dataset_hash 与修复后一致） |
| G2 | 预览覆盖只判「字段非空」：资产总计=0 的行预览说可算，执行却资料不足 | `preview_status="covered"`、`debt_field.status="ok"`；同输入执行 `counts={"pass":0,"fail":0,"insufficient":1,"total":1}` |
| G3 | 预览把本作用域所有数据集+官方缓存拼成一张表：2023 在数据集 A、2024 在数据集 B 时借 A 的 2023 算同比 | `preview_growth.status="ok"`、`previous_period="2023-12-31"`；执行（只选 B）`counts.insufficient=1` |
| G4 | 适配器在源响应缺 `CURRENCY` 时静默默认币种 | A 股路径存 `currency="CNY"`（`raw_currency_present=false`）；观察层看起来像「已声明币种」，实际是适配器猜的 |

## 二、修复（判据单点 + 输入绑定）

| # | 改动 | 文件 | 判据落点 |
|---|---|---|---|
| G1 | 同比只认**严格相邻的上一个自然年**：`2024年`→`2023年`、`2024-12-31`→`2023-12-31`；更早年度/非年度形态各自给原因（「缺上一年度 …」「…不是相邻年度，不做跨年度同比」），零/负基数、跨币种、跨口径给专门原因 | `stock_screen.py` | `adjacent_prior_period()` + `judge_growth()`（唯一判定入口） |
| G2 | 取值统一成 `ComputableValue`（元金额+状态+币种/口径），预览 `_field_status` 与执行 `_ratio_reading`/`_growth_reading` 都调 `judge_ratio`/`judge_growth`——两边可以取不同证据（宽表行/冻结观察），不允许有两套判据 | `stock_screen.py`、`screen_prepare.py` | `value_from_row()` / `value_from_observation()` + `judge_ratio()` / `judge_growth()` + `unusable_reason()`（四类原因各写各的：缺/无效/unknown/冲突） |
| G3 | 预览绑定**与执行同一份输入**：`dataset_id` 指定就只用该数据集（错误只报告、不 substitute），未指定绑定内置官方缓存（镜像 `screen_store._resolve_input`）；同比的上一年只从**同一绑定输入**内按 `adjacent_prior_period` 取 | `screen_prepare.py` | `_resolve_preview_input()`；`prepare_plan(...)` 返回 `input_binding{dataset_id,name,source,periods,official,as_of,pit_note,note}` |
| G4 | 源响应缺 `CURRENCY` → 币种**保持未知**（空串），并加 `currency_source="unknown"` + `currency_note`（「源响应未声明 CURRENCY：币种保持未知，不默认 HKD/CNY（不猜、不补默认）」）；声明了就照抄（`currency_source="source_CURRENCY"`）。金额不受影响：`amounts_yuan` 全精度元、`display_unit`「亿元」展示契约分开 | `adapters/eastmoney.py` | 港股 `fetch()`（eastmoney_datacenter）与 A 股 `fetch_ashare()` 两条路径同一规则 |

配套（同一取舍，执行与预览共用）：

- **PIT 单判据** `stock_screen.row_pit_state(row, as_of)`：`build_snapshot` 的行过滤与预览都调它——`ok` 纳入；`pending`（缺 `available_at`/`disclosed_at`）待核验不参与筛选；`excluded`（晚于截止日/期末未到）不纳入。预览新增可选 `as_of` 参数：给了就把待核验/晚于截止日的行如实标出（`pit.state` + `pit.reason`，`prior_year_pit` 注明上一年度同样不借），不给就明说「未提供 as_of：本预览只判材料与字段可计算性，不判定 PIT」——不假装判过。
- **版本升级** `stock_screen.IMPL_VERSION`：`"stock_screen/1.2.1"` → **`"stock_screen/1.3.0"`**（G1/G2 改的是输出语义：同一输入从「给同比/可算」变为「不编造、报缺口」）。`run_identity()` 含 `impl_version` → 同输入换版本产生新身份，旧冻结结果留在旧版本下可读，不与今天的混为一谈。`SCHEMA_VERSION="weavemind.stock_screen/1"`、`FIELD_MAP_VERSION="1"` 不变。

## 三、修复后实测（同一输入重放，`tmp/t0-after-readings.json`）

**G1**（2022 收入 100、2024 收入 150、无 2023；规则 `revenue_growth > 40`，pinned 2024）：

- 执行：`counts={"pass":0,"fail":0,"insufficient":1,"total":1}`；读数 `revenue_growth`：`status="missing"`、`value=null`（不给 50.0）、`reason="缺上一年度 2023年 的营业收入观察：同比只与严格相邻的上一自然年比（2024年 → 2023年），不用更早年度顶替、不前值补齐"`、`verdict="insufficient"`、`dataset_hash=a18230ee44aced9b17af1a671a46eab6f8eaff6ad2963f4c1a8af59b28fabd7b`（与修复前同输入的 dataset_hash 一致——输入没变，变的是判读）。
- 正控：2023+2024 → `status="ok"`、`value=50.0`、`formula="(150.0元（2024年） − 100.0元（2023年）) / 100.0元（2023年） × 100"`、`pass=1`。
- 零/负基数：`status="not_applicable"`、reason 含「非正」；跨币种（2023 HKD vs 2024 CNY）：`status="conflicting"`、reason 含「币种不一致」。

**G2**（资产总计=0）：

- 预览（绑定官方缓存）：`preview_status="partial"`（4/5 字段可算），`debt_ratio`：`status="not_applicable"`、`reason="资产负债率 2024-12-31 的分母为 0：不可算，不编造（total_assets 是真实零）"`、`computable=false`——不再是 `covered/ok`。
- 执行：`debt_ratio` 读数 `status="not_applicable"`（与预览同一状态词）、`counts.insufficient=1`。
- 五字段逐条同判据：`test_preview_and_execution_agree_on_every_field_shape` 对 debt_ratio（分母 0）/gross_margin（收入 0）/cashflow_coverage（净利 0）/operating_cashflow（缺值）/revenue_growth（缺上一年）逐条断言预览状态==执行读数状态。

**G3**（2023 在 `synthetic-source-a`、2024 在 `synthetic-source-b`；绑定 B）：

- 预览：`input_binding.dataset_id=<B 的 id>`、`periods=["2024-12-31"]`；`revenue_growth`：`status="missing"`、`prior_period_required="2023-12-31"`、`reason="缺上一年度 2023-12-31 的营业收入观察：…（2024-12-31 → 2023-12-31），不用更早年度顶替、不前值补齐"`——不再 `ok + previous 2023-12-31`。
- 执行（同一 dataset_id）：`counts.insufficient=1`，与预览一致。
- 把两年放进同一份数据集 → 预览 `ok`、执行 `pass=1`（同一输入下两边同时转为可算）。
- 「我有什么材料」与「这次能算什么」分开：`materials_projection` 库存视图仍列全部数据集；`prepare_plan` 只认绑定输入。

**G4**（源响应无 `CURRENCY`；mock `_get`）：

- 存储行：`currency=""`（不再写 CNY）、`unit="元"`、`caliber="合并"`、`period_type="年报"`、`revenue=123456789`（**全精度元**，`precision="source_exact"`）；provenance `kind="adapter_collected"`、`original_verified=false`。
- 执行（该数据集，`debt_ratio > 0`）：读数 `status="unknown"`、`reason="分子total_liabilities（总负债）2024年 观察不可用（unknown）：关键元数据未知：currency"`、`counts.insufficient=1`——不按 CNY 硬算。预览同判据（`status="unknown"`、reason 含「币种」）。
- 正控：`CURRENCY="HKD"` → 照抄 `HKD`（`currency_source="source_CURRENCY"`）；港股 `fetch()` 缺 CURRENCY → `metadata.currency=""`、`currency_source="unknown"`、note 含「不默认」。

## 四、回归（全部 exit 0）

```
$ C:/Python314/python.exe -m unittest test_stock_screen      → Ran 61 tests … OK    (exit=0)
$ C:/Python314/python.exe -m unittest test_screen_prepare    → Ran 11 tests … OK    (exit=0)
$ C:/Python314/python.exe -m unittest test_screen_consistency → Ran 13 tests … OK   (exit=0)
$ C:/Python314/python.exe -m unittest test_data_rc2          → Ran 18 tests … OK    (exit=0)
$ C:/Python314/python.exe -m unittest test_screen_api        → Ran 48 tests … OK    (exit=0)
$ C:/Python314/python.exe -m unittest test_trial_t0          → Ran 21 tests … OK    (exit=0)  ← 本次新增
```

`test_trial_t0.py` 覆盖：G1 五形态（非相邻/相邻正控/单期/零负基数/跨币种）+ 预览同判据；G2（分母 0 双视图、五字段逐条、缺币种 unknown、判据单点直调、无效值）；G3（不跨数据集拼行、库存≠计算视图、as_of PIT 两边一致、未给 as_of 明说）；G4（A 股无 CURRENCY 存未知、阻断计算、HKD 照抄、港股路径同规则）；版本（1.3.0 + run_identity 含 impl_version 且换版本换身份）。隔离全部在 `mkdtemp(prefix="t0-")` 下（env：`WEAVEMIND_TEST_MODE/DATA_DIR/LOG_DIR/SCREEN_DIR/SCREEN_CACHE/WORKSPACE_ROOT`、`MEMORY_DIR`、`AGENTS_DB`），不写仓库数据目录。

改动文件指纹（sha256 前 16 位）：`stock_screen.py c9e27ca02db7d80c`、`screen_prepare.py 03b8f53e624175e4`、`adapters/eastmoney.py c71a101dd99fb15a`、`test_trial_t0.py 3e3e59a606531ef7`。

## 五、范围外 / 遗留（只标记，本任务未改）

1. **前端「默认 CNY」措辞**：`frontend/src/pages/StockScreen.tsx:227` 的字段说明写着币种「默认 CNY」，与后端 G4 修复后的「缺币种=未知（不默认）」语义相反——`frontend/**` 在本任务禁改清单里，只在此标记；后端存的是空串（未知），前端该文案应改为「未知（来源未声明时不猜）」。
2. **web API 未暴露 `as_of`**：`prepare_plan` 已支持 `as_of` 参数，但 `web_ui.py:10927` 只传 `codes/periods`（`web_ui.py` 本任务禁改）——前端暂时传不了资料截止日，PIT 判定仅库级可用；接线后预览即可与执行在 as_of 维度也对齐。
3. **上游观察注记用英文字段名**：币种未知时冻结观察的 note 是「关键元数据未知：currency」（`financial_analysis/` 一侧的写法，非 T0 写入集）——语义正确，建议后续统一成中文名（币种）。
4. **生产 Python 3.11 未验**：本机无 3.11（沿用 RC2 C5「未验」结论）；本批改动只用 stdlib（`decimal`/`re`/`json`/`dataclasses`/`unittest`），无 3.12+ 语法，3.11 兼容风险低但未实测。
5. **`test_data_rc2.test_plan_same_validation_as_collect` 的耦合**：该用例断言 `len(plan["errors"]) == 2`；预览现在会在输入不可解析时合并绑定错误——若某环境下官方缓存不可读，会多出一条绑定错误使其变 3。当前默认环境下缓存可读、用例通过；属既有用例对错误条数的脆断言，建议该轨道改为按内容断言。

## 追记（Lead 集成，2026-10-24 14:0x）

T0 执行者标记的两条范围外项已由 Lead 在集成时修掉：

1. **前端币种文案**：`frontend/src/pages/StockScreen.tsx` 字段说明由 `默认 CNY` 改为「缺省即未知，不补默认」，与后端 G4 语义一致（已随最终 `frontend/dist` 重建）。
2. **预览 `as_of` 未透传**：`web_ui.py::_post_screen_prepare_plan` 现在把 `dataset_id` 与 `as_of` 传给 `screen_prepare.prepare_plan`，因此接口层也能用 G3/G2 的绑定输入与 PIT 判据（库级能力不再只存在于模块里）。

集成后定向回归（Lead 实跑，`C:/Python314/python.exe <模块>`，全部 exit 0）：
`test_stock_screen`、`test_screen_prepare`、`test_screen_consistency`、`test_screen_store`、`test_screen_api`、`test_data_rc2`、`test_research_materials`、`test_trial_t0`、`test_trial_t1`、`test_watch_brief`、`test_screen_continuity`。

版本：`stock_screen/1.3.0` 已进冻结结果的 `impl_version`；旧结果仍按原版本可读，比较页提示版本差异（不当作经营变化）。
