# W2 · 变化简报（查看变化）验收底稿

- 日期：2026-10-24
- 执行人：d-acceptance（W2 前端写集）
- 实例：`127.0.0.1:8095`（临时实例，跑完即停）
- 后端：未改动，只调用 `/api/screen/brief` 与 `/api/screen/watchlist`
- 结论：**通过**（两场景 + 幂等 + 基准不被推进 + 409 版本冲突全部实测；未验项见末节）

---

## 1. 变更（`frontend/src/pages/StockScreen.tsx`）

- 清单卡片新增「查看变化（相对上次记录）」开关；卡片头显示 `清单版本 rev … · 跟踪基准 …`。
- 面板：两个选择框（上次记录 / 本次记录，取自本人 `list_runs`）+ `生成简报` / `把「本次记录」设为跟踪基准` / `清除基准`。
- 简报渲染：6 格概览（条件读数变化 / 数据·来源更新 / 范围变化 / 资料不足 / 缺记录 / 未观察到变化）、可比性提示、base/current 身份卡（各带「打开这一份结果」）、逐家卡片（状态迁移、变化类型 chip、警告、数值变化表含 `相对 ±%`、读数变化、`看公司（上次/本次记录）`）、变化类型图例、`brief_id` 与「模型调用 0 次」脚注、`status==="failed"` 与不可比警告。
- 生成走 `GET /api/screen/brief?base=&current=`，30 s 超时；409/400 只在面板内提示，不清空既有结果。

---

## 2. 场景与结果

测试数据（同一作用域 `admin/personal`，同一个 5 家 × 3 期合成池）：

| 记录 | run_id（前 12） | 数据来源 | 规则 | 通过/未通过/不足 |
| --- | --- | --- | --- | --- |
| run1 | `890f2f0885fa` | 官方缓存 `w1_synthetic_5`（合成演示） | A：营收增速≥0、毛利率≥20、资产负债率≤60 | 1 / 4 / 0 |
| run2 | `29d0ffab1313` | 用户数据集「合成演示 v2」（两家数值变化） | 同 A | 0 / 5 / 0 |
| run3 | `11da64b160be` | 同 v2 数据集 | B：A 但资产负债率阈值改为 ≤45 | 0 / 5 / 0 |

### 2.1 场景一：只换了数值/来源（run1 → run2）

- 概览：条件读数变化 **2**、数据/来源更新 **2**、范围变化 0、资料不足 0、缺记录 0、未观察到变化 **3**。
- 可比性：`来源数据集/文件变了`（同一套规则与报告期，来源从官方缓存换成用户数据集）。
- 卡片：
  - 美的 `000333.SZ`：通过 → 未通过（`数据/来源版本更新`）——总负债 21,247,643,199 → 38,484,251,713 元（+17,236,608,514，相对 +81.12%）；资产负债率 39.20% → 71.00%。
  - 三一 `600031.SH`：未通过 → 未通过（`数据/来源版本更新`）——毛利润 +448,668,686（相对 +5%）、营业收入 +5,916,510,149（相对 +12%）；毛利率 18.20% → 17.06%、营收同比 512.15% → 585.61%。
  - 洋河 / 格力 / 紫金：`未观察到变化`（只给两张「看公司」链接，不编造差异）。
- 截图：`screens/w2-02-brief-data-changed.png`；日志：`logs/w2-brief-r1-r2.json`。

### 2.2 场景二：改了一条规则（run1 → run3）

- 除上面的数值/来源标记外，**多出可比性警告**「条件不同：读数差异不能当成经营变化」，每张卡多出 `条件修改` chip。
- 概览与场景一相同数值（2/2/0/0/0/3），但语义被显式区分。
- 截图：`screens/w2-03-brief-rules-changed.png`；日志：`logs/w2-brief-r1-r3.json`。

### 2.3 其余判据

| 判据 | 做法 | 结果 | 证据 |
| --- | --- | --- | --- |
| 同一份记录不能和自己比 | `base == current` | 400「同一份记录不能和自己比较：请选择两次不同记录」，面板提示、不清空 | `logs/w2-brief-same-run.json` |
| 幂等 | 同清单 rev 下重复请求同一对记录 | `brief_id` 完全一致（`1775295c9fa1d6fe…` ×2） | `logs/w2-brief-id-idempotency.json` |
| 看简报不推进基准 | 连续看两次简报后查清单 | `baseline_run` 仍是 run1，`rev` 未变 | 同上 + `w2-brief-r1-r2.json` |
| 基准只能显式改 | 面板「清除基准」 | 页面变「还没有跟踪基准」；再点「把本次记录设为跟踪基准」，基准回到 run1，`baseline_set_at` 刷新 | `logs/w2-baseline-ui-clear-set.json` |
| 乐观并发 `if_rev` 过期 | 旧 `if_rev` 调 `set_baseline` | 409 `version_conflict` + `current.rev`，基准**未被覆盖** | `logs/w2-baseline-conflict.json` |
| 正常 `set_baseline` 回写 | 正确 `if_rev` | 200，`rev` 不变（再次说明 rev 只由条目派生） | `logs/w2-baseline-set.json` |

---

## 3. 命令与退出码

```
cd frontend && npm run build   # tsc -b && vite build
→ ✓ built in 4.14s              EXIT=0
cd frontend && npx tsc --noEmit -p tsconfig.json
→ EXIT=0
```

`frontend/dist` 已重建；最终产物 `StockScreen-B97ViVRw.js 92.43 kB` / `CompanyPage-BLEjbS7N.js 20.8 kB` / `index-DQNxb1hv.js 212.66 kB`。

---

## 4. 截图（3 张，`screens/`）

| 文件 | 内容 |
| --- | --- |
| `w2-01-brief-panel.png` | 面板本身：只读投影说明、两组选择框、rev 与基准行、三个按钮 |
| `w2-02-brief-data-changed.png` | run1→run2：概览 6 格、`来源数据集/文件变了`、逐家卡片与数值变化表（含 `相对 ±%`） |
| `w2-03-brief-rules-changed.png` | run1→run3：多出「条件不同」警告与 `条件修改` chip |

## 5. 日志（`logs/`）

`w2-brief-r1-r2.json`、`w2-brief-r1-r3.json`、`w2-brief-same-run.json`、`w2-brief-id-idempotency.json`、`w2-baseline-conflict.json`、`w2-baseline-set.json`、`w2-baseline-ui-clear-set.json`、`w2-baseline-rev-finding.json`。

---

## 6. 自测中修掉的问题

**变化类型 chip 重复渲染**：`change_kinds` 里同一类型可出现多次（两处数值变化各带一次 `data_updated`），直接 map 会渲染两枚相同标签。改为去重后渲染（`Array.from(new Set(...))`），三一那行从「数据/来源版本更新 ×2」变为一枚；已重建并重新截图。

---

## 7. 未验项

1. **`no_baseline` / `one_record` 简报状态**：本页的简报始终要求用户显式选两份记录，后端对缺 `base`/`current` 直接 400，因此没有「无基准时自动出简报」的界面路径；这两个状态未在 UI 上验到。
2. **`failed`（`integrity=="unreadable"` / 不可用记录）**：需要一条损坏或不可用的冻结记录；本实例三条记录都可用，未构造（`corrupt` 只在数据集侧验过，见 W1）。
3. **不可比场景中的 `not_comparable_reasons` 全部文案**：只验到「来源数据集/文件变了」与「条件不同：读数差异不能当成经营变化」两条；报告期切换、实现版本变化未构造。
4. **窄视口下的面板**：390px iframe 只覆盖了公司页（`w1-10`），简报面板在窄视口下的折行未单独截取。
5. **真实键盘操作**：按钮点击是合成事件，不是真键盘/真鼠标事件（与 D 阶段同一限制）。

---

## 8. 发现（后端行为，未改，交 Lead 判断）

1. **`watchlist.rev` 不包含 `baseline_run`**（`logs/w2-baseline-rev-finding.json`）：`rev` 由条目派生，所以「设基准」不改变 `rev`。后果是两个标签页拿同一个旧 `if_rev` 各设一次基准时**都不会 409**，后者覆盖前者（实测 A 设 run2、B 用同一旧 rev 设 run3，两次均 200）。若基线并发也需要挡住，需要把 `baseline_run`/`baseline_set_at` 纳入 rev 或在 `set_baseline` 里额外比 `if_baseline`。
2. **`brief_id` 绑定清单 rev**（`logs/w2-brief-id-idempotency.json`）：同一对记录在清单增删条目后 `brief_id` 会变（即使卡片内容一致），因为 `identity.watchlist_rev` 进入简报身份。同一 rev 下完全幂等（已验证）；如果希望「同一对记录永远同一 id」，需要把 rev 从身份里摘掉或改成只取范围指纹。
3. **数据集分支 `metric_label` 直接回填字段键**：前端已按已知字段补展示同义词（W1 第 1 节）；若要长期一致，建议后端在数据集分支给中文标签。

---

## 9. Lead 集成追记（2026-10-24）

前端交回的三条后端观察处理如下：

1. **`watchlist.rev` 不含 `baseline_run`**（已修）：`screen_store._watchlist_rev(items, baseline_run)` 现在把跟踪基准一起哈希。效果：两个标签页拿同一个旧 `if_rev` 各设一次基准时，后写者 **409 `version_conflict`**，不再静默覆盖；`test_watch_brief::TestBaseline` 与集成冒烟（8094，过期 rev → 409）双向验证。副作用是设/清基准本身也会让 rev 前进 1 次，属预期（清单状态确实变了）。
2. **`brief_id` 绑定清单 rev**（按契约保留）：W0 契约要求简报身份含「清单版本/比较范围」，所以清单增删条目后同一对记录的 `brief_id` 会变、卡片内容不变；**同一 rev 下完全幂等**。这是「同一输入 → 同一身份」的正确行为，不是缺陷；如果将来要「跨清单版本复用同一份简报」，应另存快照而不是放宽身份。
3. **数据集分支 `metric_label`**（已修）：见 `W1-company.md` §10 第 1 条，后端补中文标签，前端兜底不再被触发。

另：简报「可比性」在 `comparability.flags` 上新增 `overview.rules_changed / impl_version_changed / source_changed / period_policy_changed` 四个布尔直读字段，前端可直接用（当前 UI 用 flags 判定，兼容）。
