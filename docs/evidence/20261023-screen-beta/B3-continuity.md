# B3 持续研究：研究清单、规则模板、成本分账、手工更新对比

任务书：`docs/员工任务_选股研究Beta_真实数据与普通用户闭环_20261023.md` §B3
本报告只写**实际做过的改动与实测结果**；未跑过的写「未验」，并说明为什么。

## 一、这一阶段定义的「完成」

B3 要求把一次性筛选变成可以**连续做研究**的东西，且成本说清楚：

1. 清单/备注：能把自己关心的公司留在产品里，下次能看见（只在本账号可见，不是持仓）；
2. 手工更新对比：资料更正后重跑，要能看出**谁进、谁出、谁读数变了**，并且说清差异是「数据更新」还是「条件效果」；
3. 规则模板导入导出：条件可以带走、可以给别人，但不能夹带私有内容（股票池、备注、结果、账号），导入**不能偷偷执行**；
4. 成本分账：请求/缓存/计算/模型四类分开记，没有报价的费用写未知，不用 0 冒充。

判定标准：后端有契约与测试、前端有可见入口、浏览器里真的点得到、点完能被服务端数据证实。

## 二、实际改了什么

| 文件 | 改动 | 为什么 |
|---|---|---|
| `screen_store.py`（新增 B3 段） | `WATCHLIST_SCHEMA`/`RULES_TEMPLATE_SCHEMA`/`COST_SCHEMA`；`watchlist_mutate`（add/remove/note，幂等、上限 200、备注 500）；`export_rules_template` / `import_rules_template`（只校验+预览，永不执行/落盘）；`load_cost` / `record_cost` | 清单、模板、账本都要有明确的 schema 与上限，否则前端与测试各写一套 |
| `web_ui.py` | 新增 5 条路由：`GET /api/screen/watchlist`、`POST /api/screen/watchlist`、`POST /api/screen/rules/export`、`POST /api/screen/rules/import`、`GET /api/screen/cost`；三处写入加入 `_SCREEN_WRITE_PATHS`（走 `_require_auth` + `_screen_can_research` + `screen.write.denied` 审计）；`_post_screen_run` 记录 `runs_total/compute_runs/cache_hits/compute_ms` 并在响应里回 `cost` | 写入必须与执行同一条权限链；执行响应要能当场解释这次是「算了」还是「复用」 |
| `frontend/src/pages/StockScreen.tsx` | 结果行「加入清单/已在清单」；「我的研究清单」卡片（备注 500 字、移出、加入股票池）；第 2 步「导出模板 / 导入模板」+ 预览 + 「应用这些条件（不执行）」；「成本与请求」卡片（8 项分账） | 用户要在同一个页面完成「筛→留→再筛」，不能靠记忆和手工记笔记 |
| `test_screen_continuity.py`（新，24 用例） | 清单往返/幂等/坏代码/备注长度/作用域隔离/上限/未知动作；模板往返/不含私有字段/拒绝坏规则/拒绝越界字段/拒绝私有键/拒绝未知键/20 条上限/期间策略校验/版本不一致给警告/导入不执行；账本初始/累计/未知键/隔离 | 这三块都是「写状态」的功能，必须有反例测试 |
| `test_screen_api.py`（41→48） | 清单跨研究员隔离、只读账号 403、坏代码与超长备注、模板往返且不产生 run、导入拒绝私有/未知键、viewer 禁止导入、run 之后账本有增量 | 存层对了不代表 HTTP 层权限也对 |

`stock_screen.py` 本阶段无改动（实现版本仍是 `stock_screen/1.2.1`，B2 已 bump）。

## 三、接口契约（实测）

```
GET  /api/screen/watchlist        -> {watchlist:{schema,items:[{code,name,note,added_at,updated_at}],limits:{max_items:200,max_note:500}}}
POST /api/screen/watchlist        body {action:"add"|"remove"|"note", code, name?, note?}
POST /api/screen/rules/export     body {rules:[...], period_policy:{mode,report_period}} -> {template:{schema,created_at,impl_version,field_map_version,period_policy,rules,notes}}
POST /api/screen/rules/import     body {template:{...}} -> {preview:{rules,period_policy,notes}, warnings:[...]}   # 不执行、不写盘
GET  /api/screen/cost             -> {cost:{prepare_requests,previews,compute_runs,compute_ms,cache_hits,runs_total,model_calls,network_requests,external_fee,...}}
```

拒绝行为（中文错误，非 500）：

- 清单代码不是 `6位数字.两位交易所` → 400；备注 > 500 字 → 400；超过 200 只 → 400；对不在清单里的代码写备注 → 400；
- 模板里出现 `pool/run_id/dataset_id/scope/user/token` 等私有键 → 400；未知键 → 400；schema 不匹配 → 400；未知字段 / 百分比带单位 / 超过 20 条规则 / 期间策略非法 → 400；
- 只读账号（viewer）访问上述写入 → 403「只读账号…」，且落 `screen.write.denied` 审计；
- 模板实现版本与当前不同 → 不拒绝，返回 `warnings`，前端在预览里显式提示「口径可能不同，执行后不要直接和旧结果比高低」。

## 四、机器验收

`logs/b3-tests.log`（真实退出码，逐文件跑，未吞管道）：

| 文件 | 用例 | 退出码 |
|---|---|---|
| `test_stock_screen.py` | 59 | 0 |
| `test_screen_store.py` | 77 | 0 |
| `test_screen_api.py` | 48 | 0 |
| `test_screen_continuity.py` | 24 | 0 |
| `test_auth_audit.py` | 27 | 0 |
| **合计** | **235** | 全 0 |

前端：`npx tsc -b` 退出 0；`npx vite build --outDir dist-screen-check` 退出 0（`logs/b3-build.log`）。

## 五、浏览器实测（researcher 账号，8083 实例）

环境：`http://127.0.0.1:8083/screen`，账号 `researcher`（自助研究角色），数据为「用户数据集：b1-researcher-sample」。

1. **执行复用**：勾选 `002304.SZ` + 示例条件「高毛利低负债」→ 执行 → 结果页显示 `run_id 1d66f114901b…`，与 B2 同一份（同输入复用，`reused=true`，`compute_ms=0`）；
2. **加入清单**：结果行点「加入清单」→ 按钮变「已在清单」，清单卡片出现 `洋河股份 002304.SZ`，计数「1 只」，提示「已加入清单：002304.SZ（观察对象，不是持仓）」；
3. **写备注**：备注框输入「2025 毛利率下滑要盯渠道库存」→ 失焦保存 → 提示「备注已保存：002304.SZ」；服务端 `GET /api/screen/watchlist` 返回 `note` 一致，`updated_at` 从 20:22:23 变为 20:22:53（真的落盘，不是只在前端）；
4. **导出模板**：点「导出模板」→ 页面提示「规则模板已导出：只含条件、期间策略与版本，不含股票池/备注/结果/账号信息」；服务端返回的模板里没有 `pool/dataset_id/user`（HTTP 层用例 `test_rules_template_roundtrip_and_no_run` 同向验证）；
5. **导入模板**：通过页面自身的 `<input type=file>` 注入一份模板（条件改为 毛利率 ≥ 65、资产负债率 ≤ 55，`impl_version` 伪造成 `stock_screen/1.0.0`）→ 预览显示「2 条条件 · 期间策略 uniform」+ 两行条件 + 「模板来自实现版本 stock_screen/1.0.0，当前是 stock_screen/1.2.1：口径可能不同…」；
6. **应用但不执行**：点「应用这些条件（不执行）」→ 表单条件变为 65/55，提示「已把模板条件填进表单（仍未执行…）」，**结果区 run_id 仍是 `1d66f114…`**（没有偷偷跑一份新结果）；
7. **成本分账**：展开后显示「执行请求 1 次 / 确定性计算 0 次·0 ms / 复用已冻结结果 1 次 / 模型调用 0 次 / 数据准备请求 0 次 / 输入预览 2 次 / 出站抓取 0 次 / 外部费用 未知（无报价）」。

截图：`07-b3-watchlist-and-cost.png`（清单+备注+成本卡）。

## 六、手工更新对比实测

脚本：`tmp/b3_manual_update.py`，日志 `logs/b3-manual-update-walk.log`，**退出码 0**。数据：在真实缓存 6 行基础上做两份「更正后」CSV（不改原始文件）：

- `tmp/b3-updated-6-rows.csv`：洋河 2025 `gross_profit` 改为收入的 69.5%（更正后毛利下滑）；
- `tmp/b3-missing-2025.csv`：洋河 2025 `gross_profit` 留空（更正时该期未披露毛利）。

两次上传都得到真实 dataset id（`4e60d8e1…`、`218ada0c…`，第二次上传同内容 `reused=True`）。

| 对比 | 基准 | 对照 | 服务端结论 |
|---|---|---|---|
| A→B | `1d66f114…`（原件，池=洋河） | `d0d5460d…`（更正后，池=洋河） | `洋河 通过 → 未通过`，`field_changes: 毛利率 71.60% → 69.50%`；`verdict_flipped=1`、`left_pass=1`；归因 `input_changed`「条件未变，输入变了」 |
| A→E | `1d66f114…` | `583dc39c…`（缺 2025 毛利） | `洋河 通过 → 资料不足`，`gross_margin` 状态 `missing`（不是 0、也不是直接不通过）；`insufficient_new=1` |
| C→D | `83fc4c37…`（原件，池=两家） | `493f5722…`（更正后，池=两家） | 三一 `600031.SH` 在两份里都是「资料不足」——因为统一报告期取池内最新年度 2025 年，三一没有 2025 行。这条不是数据更正造成的，系统没有把它算成「变化」 |
| D→D2 | `493f5722…` | 原样重跑 | 同一 `run_id`，`reused=true`、`compute_ms=0`：**相同输入不制造假差异** |

对比页的归因文案是「条件未变，输入变了：规则、股票池、期间口径都相同，差异来自输入数据或资料截止日变化；这解释的是『数据更新带来的变化』，不是『筛选条件的效果』」——与 B0 的「可比性/归因」要求一致。

浏览器侧同一份对比（选两份结果 → 「对比这两份」）渲染一致：8 项条件逐条标「相同/不同」、汇总行「不再通过 1 / 读数有变化 1」、明细「洋河股份 002304.SZ 通过 → 未通过，毛利率：71.60% → 69.50%」。截图：`08-b3-manual-update-compare.png`。

## 七、成本分账口径

账本按账号作用域累加（`weavemind.stock_screen.cost/1`），字段含义：

- `runs_total`：执行请求次数（含复用）；`compute_runs`：真正做确定性计算的次数；`cache_hits`：命中已冻结结果的次数；`compute_ms`：确定性计算耗时；
- `prepare_requests`：数据准备/上传请求；`previews`：输入预览请求；`network_requests`：出站抓取（选股链路当前恒 0）；
- `model_calls`：模型调用（选股链路当前恒 0）；`external_fee`：外部数据费用，没有报价就写 `"unknown"`。

实测累计（本阶段结束时）：`runs_total=9`、`compute_runs=3`、`cache_hits=6`、`compute_ms=48`、`model_calls=0`、`network_requests=0`、`external_fee="unknown"`。这组数字的意义是：**9 次执行里只有 3 次真的算过**，其余 6 次直接复用冻结结果——这就是「低成本」在本产品里可核对的部分，而不是一句宣传。

页面上同时写明：「本地 API 额度为零不等于整体运营成本为零」——避免把「没花模型钱」说成「没有成本」。

## 八、反例与踩坑（都是真的遇到过）

1. **合成 `blur` 事件不会触发保存**：第一次用 `dispatchEvent(new Event('blur'))` 改备注，页面提示了但服务端仍是空 `note`。React 17+ 在根节点监听 `focusout`（会冒泡），不监听不冒泡的 `blur`。改成 `FocusEvent('focusout',{bubbles:true})` 后 `note` 才真正写入。→ 结论：浏览器自动化验证写状态时，必须回读服务端，不能只看页面提示。
2. **「导出模板」点了但文件没落到 Downloads**：页面提示成功、接口返回 200，`C:\Users\ding0\Downloads` 里没有 `screen-rules-template.json`。Chrome 在受控环境下的下载落盘未验（见第九节）；模板内容本身由 HTTP 用例与导入往返证明。
3. **导入模板不会执行**：应用后 `run_id` 未变（仍是 `1d66f114…`），说明「导入=填表单」而不是「导入=执行」。这是刻意的：执行要用户自己点，并且要显式看见会生成新结果。
4. **模板版本不一致不拒绝**：伪造 `stock_screen/1.0.0` 的模板会被接受，但带 `warnings`，前端在预览里直接展示。拒绝会让用户没法导入旧模板；不提示则会让人把不同口径的结果放一起比。
5. **长驻标签页会让按钮「假死」**：同一页面连续合成事件很久后，点「执行筛选」没有任何请求发出（服务端账本 `runs_total` 不增）。开新标签页重做即恢复正常。这是自动化会话的现象，未在产品里复现，记录在此以免误判为产品 bug。

## 九、未验与遗留

| 项 | 状态 | 说明 |
|---|---|---|
| Chrome 下载真实落盘（导出模板文件） | 未验 | 页面/接口成功，Downloads 目录无文件；需要真实用户手势或放开下载策略才能验 |
| 原生文件选择器导入模板 | 未验 | 本次是向页面自己的 `<input type=file>` 注入 `File`；系统文件对话框没法在自动化里点 |
| 清单 200 只上限的界面表现 | 未验 | 存层与 API 有用例（第 201 只 400），UI 未造 200 条数据 |
| 模板跨账号浏览器实测 | 未验 | API 用例已覆盖「另一账号导入不含私有信息」「viewer 禁止导入」；浏览器只测了同账号 |
| 20–50 家真实样本下的清单/对比/分页 | 未验 | 真实样本受环境 DNS 限制（见 B1），当前只有 2 家公司 |
| 定时/自动刷新资料 | 未做 | 任务书未要求；当前是手工更新（用户上传更正后的 CSV）后重跑 |

## 十、结论

B3 的四件事在**真实服务、真实账号、真实浏览器**里都打通了：清单与备注落盘可见、模板可带走且导入不执行、成本四类分账可核对、资料更正后重跑能分清「谁退出」与「谁只是资料不足」。

同时明确没做到的部分：真实用户手势下的下载与文件选择、20–50 家样本规模、跨账号浏览器验证。这些都没有用「测试通过」冒充「用户能用」。
