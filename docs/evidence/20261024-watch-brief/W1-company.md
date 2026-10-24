# W1 · 公司研究页（CompanyPage）验收底稿

- 日期：2026-10-24
- 执行人：d-acceptance（W1 前端写集）
- 实例：`127.0.0.1:8095`（临时实例，跑完即停；数据在 `tmp/w1-8095/`，不占用 8080/8091/8093/8097）
- 后端：未改动（`web_ui.py` / `screen_store.py` / `watch_brief.py` 均为 Lead 所有，本次只读调用）
- 结论：**通过**（入口 3/3 实测；页面状态 8 类实测，其中 `stale_reference` 未能构造、`forbidden` 未触发，见「未验项」）

---

## 1. 变更文件（只在前端写集内）

| 文件 | 变更 | 要点 |
| --- | --- | --- |
| `frontend/src/pages/CompanyPage.tsx` | 新增（约 690 行） | `?code=` + `?run=` / `?dataset=` 三态；数据身份、关键财务（缺失显示 `—（缺失）`，不补 0）、本次筛选条件读数、判定/缺口/冲突、证据（`/api/screen/evidence`）、哈希与审计折叠；`synthetic` / `user_provided` / `official` 徽标常显；人工复核一律「未开始」，页面不提供复核入口 |
| `frontend/src/App.tsx` | +2 行 | `lazy(() => import('./pages/CompanyPage'))` + `<Route path="/screen/company">` |
| `frontend/src/pages/StockScreen.tsx` | 增入口链接 | 结果行 `看公司`（带 `run`）、清单条目 `看公司`（只带 `code` → 走「选择输入」） |

`CompanyPage.tsx` 内的展示同义词 `METRIC_LABEL` 只影响表头文字：数据集分支后端把 `metric_label` 直接回填成字段键（`revenue`/`gross_profit`…），run 分支才是中文标签；前端补一份映射让两处一致，**不改任何取值/口径**。

---

## 2. 三个入口（逐个实测）

| # | 入口 | URL | 页面表现 | 截图 |
| --- | --- | --- | --- | --- |
| 1 | 结果行「看公司」 | `/screen/company?code=000333.SZ&run=890f2f08…` | 直接进入该冻结记录下的公司页；徽标「合成演示数据 · 非真实披露」；`run_id`/`result_hash`/`snapshot_id`/`as_of`/`created_at`/`report_period`/`impl_version`/`field_map_version`/`source_label`/`periods` 全显示 | `screens/w1-01-screen-results-look-company.png`、`w1-02-company-run-view-top.png`、`w1-03-company-run-view-sections.png` |
| 2 | 清单条目「看公司」 | `/screen/company?code=600031.SH` | 不按代码自动打开来源不明的记录：落在「选择输入」，列 3 条本人冻结记录 + 1 份数据集，提示「只有公司代码不够」 | `w1-04-screen-watchlist-look-company.png`、`w1-05-company-select-input.png` |
| 3 | 数据集只读视图 | `?code=600031.SH&dataset=1c3a9372…` | 「数据身份（数据集）」只给已有字段；`本次筛选条件读数` 显示「没有条件读数：数据集视图不执行筛选」，**没有判定/缺口/冲突** | `w1-06-company-dataset-view.png` |

同一条页面在用户上传数据集上的来源标识：`用户提供 · 非官方`（`user_provided=true` / `official=false` / `synthetic=false`，`human_review=not_reviewed`）——见 `w1-08-company-run-view-user-source.png`。

---

## 3. 页面状态覆盖

| 状态 | 构造方式 | 结果 | 证据 |
| --- | --- | --- | --- |
| 正常 run 视图 | run1 + `000333.SZ` | 200；18 条观察、3 条条件读数、判定「通过」 | `logs/w1-company-run.json`、`w1-02`/`w1-03` |
| `select_input` | 只给 `code` | 200；3 个候选 + 1 个数据集 | `logs/w1-company-select.json`、`w1-05` |
| 数据集只读 | `dataset=` | 200；18 条观察、`conditions=[]`、note「不执行筛选」 | `logs/w1-company-dataset.json`、`w1-06` |
| `not_in_run` | `run1` + `300750.SZ` | 404；「该记录没有覆盖这家公司（不是「公司没有数据」，而是本次范围未合）」+ 换入口按钮 | `logs/w1-company-notinrun.json`、`w1-07` |
| `corrupt` | 把数据集 `meta.json` 改成不可解析后请求 | 404 `status:"corrupt"`；页面「数据集不可读 / 数据集元信息不可读：该数据集已损坏，不会用于筛选」；复原后 200 | `logs/w1-company-corrupt.json`、`w1-09-company-corrupt.png` |
| `stale_reference` | 改数据集 `rows.json` 的一个数值 | **未触发**：接口仍 200（run 侧记录的是数据集指纹，不逐次重算文件字节）→ 该分支未在 UI 验到，见未验项 | `logs/w1-company-stale-reference.json` |
| `forbidden` | 未构造（需另一用户作用域访问他人 run） | 未验 | — |
| 清单纳入/移出 | 页内按钮 | 「已移出清单：000333.SZ（清单是观察对象，不是持仓）」→「已加入清单：000333.SZ（观察对象，不是持仓）」；按钮态在「加入研究清单 / 在清单中 · 移出」之间正确切换 | `w1-02`（移出前）、操作过程见本节说明 |
| 窄视口（近似） | 390×760 同源 iframe，页面标题已标注「非设备模拟」 | 徽标换行、按钮折行、数据身份字段纵向堆叠、长哈希 `break-all` 不溢出；底部导航切成图标条 | `w1-10-narrow-390-iframe.png` |

证据面板：`/api/screen/evidence?run=29d0ffab…&entity=000333.SZ` 返回 3 条读数、首条含 2 个 inputs —— `logs/w1-evidence-r2-000333.json`。

---

## 4. 命令与退出码

```
cd frontend && npm run build      # 脚本 = tsc -b && vite build
→ ✓ built in 4.14s                 EXIT=0
   dist/assets/CompanyPage-BLEjbS7N.js   20.8 kB
   dist/assets/StockScreen-B97ViVRw.js   92.43 kB
   dist/assets/index-DQNxb1hv.js        212.66 kB

cd frontend && npx tsc --noEmit -p tsconfig.json
→ EXIT=0（strict / noUnusedLocals / noUnusedParameters 全开）
```

`frontend/dist` 已由本次构建重建到最新（构建三次，均 EXIT=0；最终产物哈希如上）。打包以 Lead 侧为准。

---

## 5. 截图（10 张，`docs/evidence/20261024-watch-brief/screens/`）

| 文件 | 内容 |
| --- | --- |
| `w1-01-screen-results-look-company.png` | 选股页结果行：每个候选行右侧「看公司」（入口 1） |
| `w1-02-company-run-view-top.png` | 公司页顶部：公司名 + 判定 + 合成演示徽标 + 数据身份 + 关键财务（入口 1 落地） |
| `w1-03-company-run-view-sections.png` | 同一页的条件读数 / 判定·缺口·冲突 / 证据与哈希折叠区 |
| `w1-04-screen-watchlist-look-company.png` | 清单卡片：条目「看公司」（入口 2）+ `清单版本 rev` / `跟踪基准` 行 |
| `w1-05-company-select-input.png` | 「选择输入」：3 条冻结记录 + 1 份数据集，不自动打开 |
| `w1-06-company-dataset-view.png` | 数据集只读视图：无判定、条件读数处明示「不执行筛选」 |
| `w1-07-company-not-in-run.png` | `not_in_run` 状态 |
| `w1-08-company-run-view-user-source.png` | 用户上传数据集的 run 视图：`用户提供 · 非官方` 徽标 + `dataset_id` |
| `w1-09-company-corrupt.png` | `corrupt` 状态 |
| `w1-10-narrow-390-iframe.png` | 390px iframe 近似窄视口（标注「非设备模拟」） |

## 6. 日志（`logs/`）

`w1-company-run.json`、`w1-company-select.json`、`w1-company-dataset.json`、`w1-company-notinrun.json`、`w1-company-corrupt.json`、`w1-company-stale-reference.json`、`w1-evidence-r2-000333.json`。

---

## 7. 自测中发现并已修掉的问题

1. **简报卡片变化类型标签重复**（`StockScreen.tsx`）：后端 `change_kinds` 允许重复（两处数值变化各带一次 `data_updated`），前端直接 map 会渲染两枚相同 chip。改为 `Array.from(new Set(...))` 后，三一那行由「数据/来源版本更新 ×2」变为一枚。
2. **数据集视图表头是字段键**（`CompanyPage.tsx`）：见第 1 节 `METRIC_LABEL`；修前表头是 `revenue/gross_profit/…`，修后与 run 视图一致（营业收入/毛利润/…）。

两处修完都重建并重新截图（当前交付的截图均为最终构建产物）。

---

## 8. 未验项（明确做不到，不做推测）

1. **`stale_reference` 分支**：改数据集文件字节后接口仍 200（指纹记在 run 侧，不逐次重算），没有合法办法在 UI 上造出该状态。
2. **`forbidden` 分支**：需要另一用户作用域的 run；本实例只有一个作用域 `admin/personal`，未构造。
3. **原生文件选择框上传**：浏览器扩展未开「允许访问文件网址」权限，`upload` 返回 `extension_error`，与 D 阶段同一限制。
4. **真实键盘 Tab/焦点顺序**：没有真键盘注入通道，未验。
5. **真实窄视口**：脚本不能改 `innerWidth`（`resizeTo` 只改外框），只有同源 iframe 近似，截图已标注「非设备模拟」；iframe 内的关键财务表是否横向滚动未单独截取。

---

## 9. 观察（不改后端，交 Lead 判断）

1. **数据集视图 `period` 是原始标签**：run 视图显示「2022 年（flow）」，数据集视图显示数据集自带的 `2022-12-31`。这是忠实呈现数据集原标签，未做推断，故未改；若要求两处一致，需要后端在数据集分支给出 `period_kind`。
2. **从公司页返回选股页会丢「当前打开的原版」**：`/screen` 的「打开原版」是组件态不是 URL，浏览器返回后结果卡回到「还没有筛选结果」。公司页本身有「回到这份结果」入口，因此不阻塞验收；是否把打开态同步进 URL 由 Lead 定。

---

## 10. Lead 集成追记（2026-10-24）

本轮 Lead 侧后端改动（前端无需再改）：

1. **数据集视图中文标签**：`GET /api/screen/company` 的 dataset 分支不再把 `metric_label` 回填成字段键，改为营业收入/营业成本/毛利/归母净利润等中文标签（前端兜底保留，二者一致）。
2. **预览 `as_of` 透传**：`POST /api/screen/prepare/plan` 现在接受并透传 `dataset_id` 与 `as_of`，接口层与库级能力对齐（G3 绑定输入 + PIT 判据）。
3. **币种文案**：`StockScreen.tsx` 字段说明由「默认 CNY」改为「缺省即未知，不补默认」，与后端 G4 一致（随最终 dist 重建）。

§9 两条观察的处理：数据集 `period` 保留原始标签（忠实呈现，不推断）；`/screen` 打开态进 URL 属交互增强，不在本批 W0–W2 范围，未做，已在 manual-acceptance 说明从公司页可一键回到该结果。
