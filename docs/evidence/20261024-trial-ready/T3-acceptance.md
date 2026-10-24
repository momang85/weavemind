# T3 · 最终构建验收（TRIAL-READY-1024）

- 日期：2026-10-24；执行人：lead（集成）
- 结论：**T0/T1 修复在本构建上全部回归通过；T2 的未修项（可读名字、草稿保护）不冒充通过；未验项逐条列明。**
- 构建：与 `docs/evidence/20261024-watch-brief/W3-acceptance.md` §1 同一份最终构建（`weavemind-rc2-20261024T141228Z-cd6a388f.zip`，sha256 `657c0d36…b737`）。TRIAL-READY 与 WATCH-BRIEF 是同一棵树，不再另打第二份包。

## 分通道

| 通道 | 内容 | 结果 |
| --- | --- | --- |
| 接口 | `POST /api/screen/prepare/plan`（含 `as_of`/`dataset_id` 透传）、`POST /api/screen/execute`、`/api/screen/company`、`/api/screen/brief`、`/api/screen/watchlist` 基准确认 | 200/400/409 语义与 W3 §2 冒烟一致；G1–G4 反例在接口层重放由 `test_screen_api`（48）+ `test_trial_t0`（21）覆盖 |
| 组件 | `test_stock_screen` 61、`test_screen_prepare` 11、`test_screen_consistency` 13、`test_screen_store` 71、`test_trial_t0` 21、`test_trial_t1` 18、`test_research_materials` 10 | 全部 exit 0 |
| 合成 | 两份合成 run → 公司页 + 简报（W3 §2）；G1–G4 探针同输入重放（T0 报告） | 通过 |
| 真实浏览器 | 同一最终 dist 上的 W1/W2 走查（13 张截图，`20261024-watch-brief/screens/`） | 通过（未验项见下） |
| 真实数据 | `workspace/**` 无 `working_paper.json`/`financials.json`；真实账号取数未跑 | **未验** |

## 与门槛的对应

- G1–G6：见 `20261024-watch-brief/release-gates.md` §1（修复前后实测 + 判定）。
- T2：见 `T2-usability.md`（通过 4、部分 1、未验 1、未修 2）。
- T3 本人：本文件；未修/未验项不列入「通过」。

## 未验（不冒充）

Python 3.11 运行时；真实账号/真实数据路径；原生文件选择框、真键盘、真实窄视口；`stale_reference`/`forbidden`/简报 `no_baseline|one_record|failed` 的 UI 文案；审计文件隔离缺口（见 W3 §7 披露）。
