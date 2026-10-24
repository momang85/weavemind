# W0 契约：统一公司页 + 自选变化简报（WATCH-BRIEF-1024 §3）

日期：2026-10-24。前置状态：TRIAL-READY-1024（T0–T3）**进行中**——G1–G6 修复由 `logic-audit`（T0 计算一致性）与 `research-materials`（T1 研究材料）执行，修复通过前 W1/W2 不接真实用户路径；本契约先行交付。

## 1. 入口梳理（现状 → 目标）

| 入口 | 现状接缝 | 目标 |
|---|---|---|
| 筛选结果点公司 | `StockScreen.tsx` 结果行（含 `rows[]` 的 entity/verdict/readings，评估期、来源） | 行内「看公司」→ 同一公司页（带本次 run_id + dataset 身份） |
| 我的清单点公司 | 清单面板 `load_watchlist()`（`items[]`：code/name/note/version），只有代码没有数据身份 | 「看公司」→ 公司页进入「选择记录」态：列出本人名下包含该公司的冻结结果让用户选，**只有代码不得自动打开不明来源记录** |
| 有权限的研究记录点公司 | `/api/screen/prepare/research-materials`（本人任务投影）+ `research-collect` | 任务行「看公司」→ 公司页（材料态，引用任务+底稿版本身份） |

- 三个入口进**同一组件/路由**：`/screen/company?code=<code>&run=<run_id>&dataset=<dataset_id>`（react-router 已在 `frontend/src/App.tsx`，加一条 Route；不新建路由体系）。
- 联合定位规则：`code + run_id/dataset_id` 才能直接打开具体记录；缺 run/dataset 时公司页处于「选择输入」态，列出**本作用域**候选（`list_runs` / `list_datasets` / research-materials），由用户点选；服务端按 scope 派生身份，客户端提交的 user/project 不是授权。

## 2. 公司页内容契约（`W1-company` 的接口与组件）

`GET /api/screen/company?code=&run=&dataset=`（服务端 scope 内只读；新增只读端点，沿用登录/角色校验）：

- `identity`：公司名/代码/市场、来源类别（official_cache / user_upload / research_material / synthetic_demo）、数据集与 run 身份（`dataset_id`、`run_id`、`schema/impl_version`、`created_at`）。
- `as_of`：报告期列表 + 资料截止日（来自选定数据集/运行，**不是「数据更新到今天」**）。
- `financials`：核心财务字段/趋势（复用 `StockScreen` 财务小表/趋势组件的数据形状；**不补缺失点、不把缺失画成 0**）。
- `conditions`：本次筛选条件、命中/未命中、缺口（复用 run 的 readings/reasons；未选 run 时为空并说明）。
- `evidence`：证据入口（复用 `screen_store.evidence(run, entity_id)` 的只读投影；来源不标「已独立核验」）。
- `watch`：加入/移出清单（复用现有 `POST /api/screen/watchlist`，含 `if_version`）。
- 状态机（含异常）：`ok` / `no_data`（资料为空→给「选择已有材料 / 本人研究材料 / 支持的数据源」三入口，不伪装「公司没有变化」）/ `multi_source`（多候选让用户选）/ `forbidden`（无权限）/ `corrupt`（数据集不可读）/ `stale_reference`（run/dataset 已删或哈希不符）/ `missing_fields`（缺字段明示）/ `synthetic`（样例标签贯穿）。
- 视觉：白/暖白底、金色强调、深墨文字、细线条（复用 `ui.tsx` 组件）；技术细节（哈希/审计）折叠可查。

## 3. 简报内容与状态契约（`W2-brief`）

生成动作：清单 → 「查看变化」→ 选两次本人冻结记录（base + current）→ 预览比较范围 → 「生成简报」。**本版零模型、零外网**；确定性投影，不重算不覆盖。

- 状态机：`no_baseline`（只有一次记录，提示「设为跟踪基准」，不能比较）/ `one_record` / `comparable` / `no_observed_change`（可比范围内未观察到变化，如实说）/ `insufficient`（资料不足单列）/ `not_comparable`（报告期/币种/口径/版本/范围不同，明确警示不直接相减）/ `failed`（生成失败不改原基准）。
- 简报最小内容：标题「本次记录相对上次记录」+ 两份记录时间/报告期/资料截止日/来源/版本；概览分列「条件状态变化 / 资料状态变化 / 范围·规则·版本变化」（可同时出现）；公司卡：旧值→新值（同单位可比才给差值；比例指标区分百分点差与相对增幅；原始精度计算、展示可格式化）、状态转换（满足/未满足/资料不足）、规则阈值与计算解释、两边证据入口。
- 变化原因按证据分类：`data_updated` / `source_version` / `period_switch` / `rules_changed` / `scope_changed` / `impl_version` / `pending_review`。没有修订标记不得写「财报更正」；没有因果证据不得写「经营恶化」；缺记录/不可读/未来披露不得算「无变化」。
- 基准规则：base 由用户**明确选择**（或「设为跟踪基准」显式操作，带 watchlist 版本条件 409 保护）；查看简报**不自动推进基准**；生成失败/资料不足/异常返回不改变原基准；旧冻结结果不改写。
- 幂等：同输入（base/current run_id + 清单版本 + 比较口径）重复生成 → 同一结果（结果哈希相同），不新造内容；抓取时间变化不制造经营变化。
- 身份绑定 schema：`schema/impl_version + scope + base_run_id + current_run_id + result_hash + watchlist_rev + 比较口径`；刷新能打开同一份输入投影；依赖的原记录缺失/损坏 → 明确「不可验证」，不出替代品。

## 4. current/baseline 的选择与更新规则

1. 不默取「全局最新记录」当基准；不把「页面最新」当「数据更新到今天」——报告期/资料截止日一律来自记录本身。
2. 设基准 = 写 watchlist 级 `baseline_run`（`if_rev` 条件写，冲突 409 不覆盖）；换基准 = 同样的显式动作。
3. 生成简报时 base 优先取用户所选；未设基准且有 ≥2 条记录时提示选择，不自动挑。
4. 更新数据是**另一个明确动作**（现有数据准备/筛选流程），简报页只放引导入口，不自动发起取数或研究任务。

## 5. 标注与集成人

- 复用位置：`screen_store.load_run/list_runs/compare_runs/evidence/load_watchlist`、筛选运行的 scope/哈希/完整性、`research_materials` 授权材料投影、`StockScreen.tsx` 财务小表/趋势、`ui.tsx` 视觉组件、现有 watchlist 写路径（含 409）。
- 拟新增：`GET /api/screen/company`（web_ui，Lead）、公司页 `CompanyPage.tsx`（Lead）、简报模块 `watch_brief.py`（Lead）、`POST /api/screen/brief`（web_ui，Lead）、watchlist `baseline_run` 字段（screen_store，Lead）、前端清单「查看变化」（StockScreen/CompanyPage，Lead）。
- **唯一集成人：Lead（lead）**——公共 `web_ui.py` / `screen_store.py` / `StockScreen.tsx` 的全部改动由 Lead 落地；`logic-audit`（T0：stock_screen/screen_prepare/adapters）与 `research-materials`（T1：research_materials）在自己的写集内工作，改公共文件先发 Lead。
- 公共模块改动前先搜引用（`grep`）+ 定向测试（`test_screen_api` / `test_screen_store` / `test_screen_consistency` / `test_trial_t0` / `test_trial_t1`）。

## 6. 验收样例（W1/W2 共用数据类别）

- 合成：`data/samples/synthetic_5_companies.json`（5 家虚构，2 年）+ 构造两份可比 run（改一个字段值 + 一条规则跨阈值）。
- 真实转录：4 家 × 2023/2024（三一/洋河/美的/格力，user_provided；三一口径差异保留警示）。
- 必测场景：TRIAL-READY §5.4 的 8 条 + W1 状态机（no_data/multi_source/forbidden/corrupt/stale_reference/missing_fields/synthetic）。

## 7. 门槛依赖（W1/W2 开工条件）

- T0（G1–G4）回归通过 + 原 F1–F7 不倒退（`logic-audit`）。
- T1（G5/G6）回归通过 + 来源契约一致（`research-materials`）。
- 未满足时可做隔离组件/合成样本，不接真实用户路径。
