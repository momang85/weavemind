# RC2-A：数据契约与独立取数 — 实现与回归证据

日期：2026-10-24　执行：lead（DATA-RC2-1024 §3）　前置：20261024-beta-closeout（C0–C3）与架构师复核
回归基线：HEAD `ed8fd90`（脏树未提交，与任务边界一致）

## 1. 架构师缺陷 → 修复对照

| # | 架构师发现 | 修复位置 | 回归 |
|---|---|---|---|
| F1 | 甲2023/乙2024 被错判为「甲2024 已覆盖」 | `screen_prepare._official_rows()` 返回 `{(code,period):rows}` 逐主体逐期间投影，`prepare_plan` 按主体+期间生成格点；预览格含 `material/computable/fields` 三态（`covered`/`partial`/`material_only`/`missing`） | `test_data_rc2.StrictValidation.test_half_year…`、`test_screen_prepare.test_revenue_only_row_is_not_full_coverage`、`rc2_after_probe.F1` |
| F2 | 半年度请求被静默物化成年报 | `parse_periods_strict()`：只接受 `YYYY` 年度与 `YYYY-MM-DD` 期末日，半年/季度/非法日期/空串显式拒绝；预览与取数共用同一解析 | `test_data_rc2.StrictValidation`（5 例） |
| F3 | 响应主体与请求代码不符仍入库 | `eastmoney` 每行带 `source_subject{code,security_code,name}`；`_rows_from_adapter_financials` 校验 `source_subject.code == 请求代码`，不符按 `subject_mismatch` 记错并拒绝该主体，不落入请求代码名下 | `test_data_rc2.SourceIdentityAndPrecision`（2 例）+ probe F3 |
| F4 | 123456789 元 → 123000000（亿元舍入进入计算） | `eastmoney._yuan_exact()`：保留整数/高精度元金额（NaN/Inf 拒绝），行级 `amounts_yuan{}` + `normalization.version=amount_yuan/1`；`_rows_from_adapter_financials` 优先 `amounts_yuan`（`precision="source_exact"`），仅退化时用展示值并标记 `display_rounded_yuan`；存储层全精度，亿元只是展示契约（`_to_yi` 保留） | `test_data_rc2` 精度 3 例（含 7 位临界、展示舍入不改读数）+ probe F4 |
| F5 | 损坏数据集被当作成功复用 | `screen_store._persist_dataset`：写后回读实际 `rows.json`，比对 sha256 与行数、scope/schema；不符抛 `StoreWriteError`（HTTP 503 `store_unavailable`），不改写原文件 | `test_data_rc2`（损坏→拒绝，字节不变）+ probe F5（`bytes_unchanged_after_refusal=true`） |
| F6 | DNS 失败/未连接被计成真实请求 | `collect` 计量改为 `adapter_calls`（适配器调用数）+ `network_requests=None`（传输层无连接事件，未知）+ `network_ok/failed/denied/pre_connect_failures` 分列；`network_requests_note` 说明语义 | `test_data_rc2` 计量 2 例 + probe F6（`adapter_calls=1, network_requests=null`） |
| F7 | 账本不可读被覆盖 7→1 | `screen_store.record_cost`：`integrity in (corrupt,unreadable)` 或 `load_errors` 时不写、不改字节、返回 `reason=ledger_corrupt/ledger_unreadable`；`rebuild_cost` 先归档坏字节再从 0 重建 | `test_data_rc2.LedgerAndMetering`（3 例）+ probe F7（字节保留、累计 7 不丢） |

## 2. A1 主体×期间联合覆盖

- 预览 `POST /api/screen/prepare/plan` 与取数 `POST /api/screen/prepare/collect` 共用 `parse_codes_strict`/`parse_periods_strict`（单处实现，无第二条路径）。
- 期间策略 `uniform/pinned/latest_each` 显式传入并进入 `result_hash`；非连续年度只返回所选年度，不自动补中间年（回归 `test_non_contiguous_years_only_requested_returned`）。
- 每格状态不再是「有材料=全覆盖」：`covered`=五字段可全算；`partial`=部分字段可算（`fields` 列出缺口字段名）；`material_only`=有材料但无可算字段；`missing`=缺材料。`totals` 增加 `partial`、`material_only`。

## 3. A2 无损源→计算

- 源契约行级元数据：`source_subject`、`amounts_yuan`、`normalization.version`；`caliber`/`currency` 缺失**不补默认**（`_rows_from_adapter_financials` 现按「缺失→空串」，由观察层按既有规则记 unknown），对应订单 A2「不默认补元/CNY/合并」。
- 重述可用时间回归 `test_restatement_available_at_respected`：同期间两条观察（原披露 2025-04-20、重述 2026-08-01），`as_of=2025-12-31` 只读原值（重述不倒灌）；`as_of=2026-12-31` 才纳入重述值。
- 来源身份：请求代码≠响应主体时该主体整组拒绝（`subject_mismatch`），不产生张冠李戴的行。
- 研究材料投影（A3）：`research_materials.materials_from_research` 接入 `prepare_plan.materials_projection` 与 `GET /api/screen/prepare/research-materials`、`POST /api/screen/prepare/research-collect`，实现见 `A3-research-materials.md`（teammate research-materials 交付）。

## 4. 回归与探针

| 命令 | 结果 | 日志 |
|---|---|---|
| `python test_data_rc2.py`（18 用例：严格校验 5、来源身份与精度 4、账本与计量 4、并发保护 1、真实网络探针 1、溯源与未知 2、类内 1） | OK (18) | `logs/a-test_data_rc2.log` |
| `python test_screen_prepare.py` | OK (9) | `logs/a-test_prepare.log` |
| `python test_screen_consistency.py` | OK (13) | `logs/a-test_consistency.log` |
| `python test_screen_store.py` / `test_stock_screen.py` / `test_screen_api.py` / `test_screen_continuity.py` | 各 OK | `logs/b-*.log` |
| `python docs/evidence/20261024-data-rc2/rc2_after_probe.py`（F1–F7 逐项复跑） | `all_fixed=true`，exit 0 | `logs-rc2_after_probe.txt` |

说明：
- `test_data_rc2` 的真实网络用例在无网环境自动跳过（本机当前无公网），真实 3–5 家公司端到端验证仍标**未验**，见 `release-gates.md`。
- 计量语义变更（`network_requests=None` 表示未知）已同步前端展示（`StockScreen.tsx`：适配器调用 N 次、真实 HTTP 请求「未知」，成本卡片同口径），`frontend/dist` 已重建（`npm run build` exit 0）。
- 探针脚本 `architect_probes.py` 未改动；修复后复跑脚本为 `rc2_after_probe.py`（逐项断言 fixed）。

## 5. 真实样本端到端（同日补充，先 4 家）

数据来源：东方财富 data center `RPT_F10_FINANCE_MAINFINADATA`（与 `adapters/eastmoney.py` 同报表同字段），执行者用只读 web_fetch 取原始 JSON 后转录为 CSV 上传；`source_url` 记录到具体 API 请求（含 SECURITY_CODE 过滤），可复取核对。脚本 `tmp/rc2_real_samples.py`，完整输出 `logs-a-real-samples.txt`。

- 上传：4 家真实公司（三一重工 600031.SH、洋河股份 002304.SZ、美的集团 000333.SZ、格力电器 000651.SZ）× 2023/2024 年报，8 行，`available_at=NOTICE_DATE`（2024-04-29→2025-04-28 各自照实），provenance=`user_provided`、official=false、不冒充官方数据；
- 运行（revenue_growth>0，latest_each）：counts `{pass:2, fail:2, insufficient:0, total:4}`（600031 +5.90%、000333 +9.48% 通过；002304 −12.83%、000651 −7.31% 不通过），result_hash `c2a9e521…`；
- 与仓库**真实官方缓存**（data/screen_cache/official_disclosure_cache.json）同公司同期间交叉核对：洋河 2023/2024 与 API 完全一致；**三一 2023/2024 不一致**（缓存 73,222,172,500 / 77,773,391,000 vs API 营业总收入 74,018,936,000 / 78,383,379,000）——两个来源口径与更正时间不同，这正是「来源身份进 result_hash、不得混源」的原因；后续人工复核须按缓存行内 source_url 回原文核验；
- 本机环境注意：系统 DNS 把公网域名解析到保留段 198.18.0.20（代理/VPN fake-IP），应用内 SSRF 守卫正确拒绝（`blocked URL by SSRF guard`），所以**应用内适配器直连东方财富在本机不可用**（这是安全行为的正确表现，不是缺陷）；真实样本经 web_fetch 只读转录走「用户提供」路径完成核对。适配器直连的真实请求数采样仍标未验（需可直连公网的环境）。
