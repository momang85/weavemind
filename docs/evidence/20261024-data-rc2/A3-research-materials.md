# RC2-1024 · A3：研究任务材料投影接入「数据准备」

- 任务书：`docs/员工任务_数据可信与独立试用包RC2_20261024.md` §3 A3（第 53–58 行）、§A 验收（第 63 行）
- 立项依据：`docs/evidence/20261024-data-rc2/architect_review.json` → `additional_source_review[0]`
  ——「`screen_prepare.materials_projection` 只列举筛选数据集/缓存/清单/运行，
  **没有接通**既有公司研究任务的准入事实/底稿投影」；本任务即补这一段接通。
- 执行人：teammate `research-materials`；执行时间 2026-10-24。
- 结论：**已接通并可运行**。新增 `research_materials.py`（只读投影 + 显式物化），
  `web_ui.py` 加两条路由（GET 只读 / POST 物化，均进审计），`test_research_materials.py`
  10 例全绿（exit 0）；`test_screen_prepare.py` 9 例全绿（exit 0）。
  真实数据核对结果：**投影链路可用，事实层未验**（仓库工作区里没有任何
  `working_paper.json` / `financials.json`，见 §7）。

---

## 1. 做了什么

### 1.1 新模块 `research_materials.py`（889 行，根目录，与既有平铺布局一致）

对外只有两个入口（外加单任务投影）：

| 函数 | 作用 | 是否写盘 |
|---|---|---|
| `materials_from_research(scope, *, limit=200, task_ids=())` | 本账号已授权研究任务的材料清单：任务身份/状态、主体与期间、准入事实（事实 ID+字段+值+期间+来源定位）、底稿（路径/哈希/条目数）、来源准入结论（采用/待核验/剔除）、逐任务 `unavailable` 原因 | 否（只读） |
| `research_material(scope, task_id)` | 单任务投影（显式不可用原因：无权限/任务不存在/底稿缺失/无准入事实） | 否（只读） |
| `collect_from_research(scope, *, task_id, fact_ids, periods, name, max_rows)` | 用户**显式选定**事实 → 物化成**本作用域**新数据集；返回 `(data, errors)` | 是（只写本作用域数据集目录） |

### 1.2 `web_ui.py` 最小接线（4 处插入，未改动任何既有分支）

| 位置 | 内容 |
|---|---|
| `_GET_ROUTES`（现 11023 行） | `GET /api/screen/prepare/research-materials`（`?task_id=` 可选：单任务投影） |
| `_POST_ROUTES`（现 11096 行） | `POST /api/screen/prepare/research-collect` |
| `_SCREEN_WRITE_PATHS`（现 10215–10224 行） | 追加 `"/api/screen/prepare/research-collect"`（与 `prepare/plan`、`prepare/collect` 同组：自助研究角色可写**本作用域**，仍受角色白名单约束） |
| 新处理器（现 10753–10811 行） | `_get_screen_prepare_research_materials` / `_post_screen_prepare_research_collect` |

纪律照抄兄弟处理器：
- GET 用 `_screen_scope_or_401(self)`（未登录 401），读失败 500 并说明「读取失败不当空材料」；
- POST 用 `_screen_scope(admin)`，客户端 `url/urls/path/file/filepath` → 400 `client_url_rejected`；
- `screen_store.StoreWriteError` → **503** `{"code":"store_unavailable"}`；
- 其他错误 → **400**（`errors` 逐条）；
- 成功 → `audit_log(..., "screen.prepare.research_collect", target="research_task:<task_id>")`，
  `detail` 含 scope / task / dataset / reused / facts / rows（与 `screen.prepare.collect` 同风格）。

### 1.3 复用的既有实现（不另造权限、不另造事实层）

| 用途 | 复用对象 |
|---|---|
| 任务行 | `task_history`（列 `task_id/goal/status/created_at/updated_at/user/project/research_request_json`），DB 路径每次调 `db_paths.resolve_db_path()` 现取；查询用 `PRAGMA query_only=ON` |
| 任务可见性 | `user` 列等值（与 `web_ui._find_cached_task` 的 `COALESCE(user,'')=?` 同一语义） |
| 任务工作区定位 | `workspace.existing_task_project_dir`（只读，**绝不** `task_project_dir` mkdir） |
| 事实与准入四分 | `working_paper.json` 的 `rows` + `selection`（`working_paper.Selection` 的 selected/pending/unrelated/conflicts） |
| 无底稿退路 | `financials.json` → `facts.facts_from_financials` → `working_paper.select_facts`（有 `research_request_json` 契约才判准入，没有就只标「未判定」） |
| 金额单位 | `facts.to_yuan`（不可换算 = 不物化，绝不猜量级） |
| 来源准入留痕 | `project/materials/index.json`（`material_intake` 的 admitted/pending_intake/rejected/fetch_failed）、`project/source_pending.json`、`project/source_exclusions.json`、`project/search_adopted.json` |
| 数据集写入/内容定址/复用/损坏判定 | 全部交给 `screen_store.save_dataset_from_source`（含 RC2-F5 的 rows.json 复验）；本模块**没有**复制任何存储校验逻辑 |

---

## 2. 接口契约（示例为合成夹具，非真实数据）

### 2.1 `GET /api/screen/prepare/research-materials`

登录即可（不要求管理员；只回本账号名下任务）。可选 `?task_id=<id>`。

```json
{
  "status": "ok",
  "scope": {"user": "demo", "project": "personal"},
  "research": {
    "status": "ok",
    "version": "a3-1",
    "scope": {"user": "demo", "project": "personal"},
    "tasks": [
      {
        "task_id": "demo-task-1",
        "title": "研究示例公司（000000.SZ）2024 年报",
        "status": "SUCCESS_WITH_ISSUES",
        "created_at": "2026-10-23 00:11:27",
        "updated_at": "2026-10-23 00:35:39",
        "owner": "demo",
        "project": "default",
        "entities": [{"entity": "示例公司", "entity_id": "000000.SZ",
                       "code": "000000.SZ", "market": "cn",
                       "facts": 6, "admitted_facts": 2}],
        "periods": ["2023年", "2024年"],
        "row_periods": ["2023-12-31", "2024-12-31"],
        "facts": [
          {"fact_id": "fact-demo-rev-2024", "metric": "revenue",
           "metric_label": "营业收入", "period": "2024年", "period_end": "",
           "period_type": "年报", "value": 1000.0, "unit": "亿元",
           "currency": "CNY", "caliber": "合并", "entity": "示例公司",
           "entity_id": "000000.SZ", "market": "cn",
           "disclosed_at": "2025-04-29", "retrieved_at": "2025-04-30",
           "source_url": "http://example.invalid/annual-2024.pdf",
           "locator": {"kind": "annual_table", "table": "合并利润表",
                        "row": "营业收入"},
           "verify_state": "verified", "derived": false,
           "admission": "admitted", "admission_label": "采用",
           "admission_reason": "底稿认证选中：可用于物化"}
        ],
        "facts_total": 8, "facts_admitted": 2, "facts_pending": 1,
        "facts_excluded": 2, "facts_source": "working_paper.json",
        "facts_truncated": 0,
        "working_paper": {"present": true, "readable": true,
                           "path": "…/projects/default/demo-task-1/project/working_paper.json",
                           "sha256": "…", "rows": 8, "derived": 3,
                           "gaps": 0, "problems": [], "paper_ok": true},
        "admitted_sources": {
          "present": true,
          "adopted": [{"material_id": "m1", "url": "http://example.invalid/a.pdf",
                        "title": "示例公司2024年年度报告", "status": "admitted",
                        "provenance_label": "官方披露原文", "from": "material_intake"}],
          "pending": [], "excluded": [{"url": "http://forum.example.com/x",
                                        "title": "网友整理", "reason": "非官方来源…"}],
          "counts": {"adopted": 1, "pending": 0, "excluded": 1}
        },
        "usable": true,
        "unavailable": null
      }
    ],
    "unavailable": [
      {"task_id": "demo-task-2", "code": "working_paper_missing",
       "reason": "底稿缺失：任务工作区没有 working_paper.json，也没有 financials.json（结构化事实层为空时无物化依据）",
       "entities": [], "periods": []}
    ],
    "totals": {"tasks_total": 2, "usable": 1, "facts_admitted": 2,
               "facts_pending": 1, "facts_excluded": 2, "scanned_rows": 578,
               "excluded_other_owner": 61, "excluded_unattributed": 467},
    "note": "只列本账号名下（task_history.user == 本作用域用户）的研究任务；…"
  },
  "note": "只投影本账号名下研究任务的准入事实/底稿/来源准入；没有从报告正文反抽数字，也没有扫描其他账号目录"
}
```

不可用原因码（`code` → 界面文案）：
`task_not_found`（任务不存在）/ `no_permission`（无权限：管理员/调度器名下或无提交人身份的历史任务不自动开放）/
`working_paper_missing`（底稿缺失）/ `no_admitted_facts`（无准入事实）/ `nothing_selected`（POST 未显式选事实）。

### 2.2 `POST /api/screen/prepare/research-collect`

```json
{"task_id": "demo-task-1", "fact_ids": ["fact-demo-rev-2024"], "periods": ["2024"]}
```

- 只接受 `task_id` / `fact_ids` / `periods`（可选 `name`）；**不接受**客户端 URL 或磁盘路径 → 400 `client_url_rejected`；
- `fact_ids` 为空 → 400（「未显式选择：本入口不把任务里的全部事实自动带入」）；
- 任务不可用（无权限/不存在/底稿缺失/无准入事实）→ 400，错误文本带原因；
- 存储不可用或复用校验失败 → 503 `store_unavailable`。

```json
{
  "status": "ok",
  "scope": {"user": "demo", "project": "personal"},
  "source": "research_task:demo-task-1",
  "task": {"task_id": "demo-task-1", "title": "研究示例公司…", "status": "SUCCESS_WITH_ISSUES",
            "owner": "demo", "project": "default", "version": "2026-10-23 00:35:39",
            "facts_total": 8, "facts_admitted": 2},
  "dataset": {"dataset_id": "a0ac902f9f45494bf896567bd719b688", "name": "研究材料投影 demo-task-1",
               "row_count": 1, "codes": ["000000.SZ"], "periods": ["2024-12-31"],
               "official": false, "provenance_kind": "user_provided",
               "provenance_label": "本人研究材料（任务事实投影）"},
  "reused": false,
  "row_count": 1,
  "rows": [{"entity": "示例公司", "code": "000000.SZ", "period": "2024-12-31",
             "unit": "元", "currency": "CNY", "caliber": "合并", "period_type": "年报",
             "revenue": 100000000000.0, "source_url": "http://example.invalid/annual-2024.pdf",
             "disclosed_at": "2025-04-29", "available_at": "2025-04-29",
             "source_kind": "research_task_fact",
             "orig_fact_ids": ["fact-demo-rev-2024"],
             "fact_refs": [{"fact_id": "fact-demo-rev-2024", "metric": "revenue",
                             "period": "2024年", "unit": "亿元", "raw_value": 1000.0,
                             "value_yuan": 100000000000.0,
                             "period_basis": "年报惯例期末 12-31（事实未带 period_end，非来源声明日期）",
                             "source_url": "http://example.invalid/annual-2024.pdf",
                             "locator": {"table": "合并利润表", "row": "营业收入"}}]}],
  "materialized": [{"fact_id": "fact-demo-rev-2024", "metric": "revenue",
                     "metric_label": "营业收入", "period": "2024年",
                     "row_period": "2024-12-31", "value_yuan": 100000000000.0,
                     "unit": "亿元", "caliber": "合并", "disclosed_at": "2025-04-29"}],
  "skipped": [{"fact_id": "fact-demo-q3", "metric": "revenue", "period": "2024年三季度",
                "reason": "期间无法确定：period=2024年三季度、period_type=三季报（不猜期末日）"}],
  "requested_fact_ids": ["fact-demo-rev-2024"],
  "requested_periods": ["2024"],
  "origin": {"parent_task_id": "demo-task-1",
              "parent_task_version": "2026-10-23 00:35:39",
              "fact_ids": ["fact-demo-rev-2024"],
              "working_paper_ref": {"path": "…/working_paper.json", "sha256": "…",
                                     "rows": 8},
              "note": "回到原版：按 parent_task_id + parent_task_version 取该任务底稿，再按 fact_ids 逐条对照（working_paper_ref.sha256 证明文件未变）。"},
  "counters": {"network_attempts": 0, "network_requests": 0, "network_denied": 0,
                "adapter_calls": 0, "pre_connect_failures": 0,
                "note": "本入口只读本账号任务产物并写本作用域数据集：没有调用任何取数适配器，也没有发起传输层连接"},
  "note": "已把显式选定的准入事实物化成本作用域的新数据集（official=False、user_provided=True）。逐事实的未物化原因见 skipped；缺失的期间/字段没有补零，也没有从报告正文反抽。"
}
```

数据集 provenance（落进 `rows.json` 的 `metadata.provenance`，可事后复核）：

```json
{"kind": "user_provided", "kind_label": "本人研究材料（任务事实投影）",
 "user_provided": true, "official": false, "synthetic": false,
 "label": "本人研究材料（任务 demo-task-1 事实投影）",
 "parent_task_id": "demo-task-1", "parent_task_version": "2026-10-23 00:35:39",
 "parent_task_owner": "demo", "parent_task_project": "default",
 "fact_ids": ["fact-demo-rev-2024", "fact-demo-rev-2023"],
 "working_paper_ref": {"path": "…/working_paper.json", "sha256": "…", "rows": 8},
 "facts_source": "working_paper.json",
 "source_urls": ["http://example.invalid/annual-2024.pdf"],
 "module_version": "a3-1",
 "note": "行来自本账号研究任务的准入事实（底稿 rows+selection），不是官方数据，也没有从报告正文反抽数字；人工复核状态一概不写。"}
```

（`screen_store.save_dataset_from_source` 会在 `metadata` 顶层固定 `user_provided: false`
且 `official` 取 `provenance["official"]`；因此该数据集在 `list_datasets` /`dataset_source_meta`
视角是 **`official=false`、`user_provided=true`**——服务端按来源判定，用户不可自封官方；
provenance 里的 `user_provided: true` 是同一事实的显式记录，见 §8 的口径注记。）

### 2.3 与前端 `StockScreen.tsx` 的字段对齐

`PrepMaterials.research` 类型为 `{status, note?, tasks?: {task_id?, title?, entities?: string[], periods?: string[]}[]}`；
本接口返回 `{status, note, tasks[], unavailable[], totals, scope, version}`：
`status` / `note` / `tasks[].task_id` / `tasks[].title` / `tasks[].periods` 名字一致，
`tasks[].entities` 是**对象数组**（`{entity, entity_id, code, market, facts, admitted_facts}`），
比前端 `string[]` 更细。前端当前只读 `status / note / tasks.length`（`StockScreen.tsx` 1294–1299 行），
**不需要改前端**；若以后要展示主体名，用 `entities.map(e => e.entity)`。

---

## 3. 「准入事实/底稿 ≠ 屏幕缓存」的判定说明（本任务的语义边界）

| 维度 | 屏幕数据集/官方缓存（`screen_store`） | 本模块投影的研究任务材料 |
|---|---|---|
| 身份 | 内容定址 `dataset_id`（sha256 前 32 位） | 任务事实 `fact_id` = sha256(主体+指标+期间+口径) 前 16 位（`facts.make_fact_id`，与值无关） |
| 谁允许用 | 上传者本人作用域；官方缓存由服务端配置 | 任务**提交人**本人（`task_history.user` 等值）；管理员/调度器/无提交人身份的任务**不自动开放** |
| 准入判据 | 官方缓存（`official=true`）或用户上传（`official=false`，未独立核验） | 底稿 `selection`：selected（采用）/pending（待核验）/unrelated（剔除：主体不符）/conflicts（剔除：同组合冲突未决）；**只有 selected 可物化** |
| 数字出处 | 上传行 / 官方缓存行 | 底稿 `rows`（事实层）或 `financials.json` 结构化载荷；**绝不**从 `reports/*.md` 正文反抽 |
| 物化后身份 | `official` 由来源决定；上传的永远是 `user_provided` | `official=false`、`provenance.kind=user_provided`，并带 `parent_task_id`/`parent_task_version`/`fact_ids`/`working_paper_ref`，可回到原版 |
| 时间语义 | 行内 `available_at`/`disclosed_at` 做 PIT | 同：取该行所有事实**最晚**的 `disclosed_at`（少披露的那条不能静默过 PIT 闸）；缺披露日 → 不物化并逐条说明 |
| 状态合规 | 机器验收 / 研究状态 / 人工复核三者不许混 | 本模块**不写**任何评审/复核状态，只投影与物化 |

三条硬边界（测试逐条覆盖）：
1. **不猜**：期间只能由 `period_end`（来源声明）或「年报 + `YYYY年`」得到期末日；季报/中报/写法不认识 → 不物化并说明。单位只在 `facts.to_yuan` 可换算时换算（亿元→元 ×1e8，比率/吨/未知 → 拒绝）。
2. **不补**：缺字段就不成列（宽表白名单 `stock_screen.AMOUNT_METRICS` 之外一律不进）；缺披露日、缺主体代码 → 逐事实说明。
3. **不越权**：只按 `task_id` 走既有工作区定位，不扫整个 workspace、不读其他账号目录；投影阶段零写盘（`PRAGMA query_only` + `existing_task_project_dir`）。

---

## 4. 回归命令与退出码

工作目录：`C:\Users\ding0\Desktop\ai_agent\织光`（MSYS bash，`PYTHONIOENCODING=utf-8`）。

```
$ python test_research_materials.py            # A3 新增
EXIT=0
Ran 10 tests in 0.523s
OK

$ python test_screen_prepare.py                # C1 兄弟用例（Lead 维护，未改动）
EXIT=0
Ran 9 tests in 6.474s
OK
```

原始输出（`python test_research_materials.py`，逐例）：

```
1）A/B 互相看不到对方任务材料：投影列表 + 单任务投影两条路。 ... ok
2）不可用原因显式：无权限 / 任务不存在 / 底稿缺失 / 无准入事实。 ... ok
3）无准入事实的任务 → 拒绝物化（不生成空数据集）。 ... ok
4）显式 fact_ids → provenance 带 parent_task_id/fact_ids；official=False、 ... ok
5）未选事实不进数据集；待核验/剔除/冲突/派生/缺期间/缺披露日逐条说明。 ... ok
6a）同内容重复提交 → 同一 dataset_id、reused=True，不新增副本。 ... ok
6b）数据集被改坏 → 再收一次必须显式失败（StoreWriteError），不冒充复用成功。 ... ok
不带 fact_ids 拒绝：不把任务里的事实整批自动带入。 ... ok
没有底稿但有 financials.json + 契约 → 结构化事实投影（只读，不落盘）。 ... ok
投影内容：准入事实带来源定位；底稿带哈希；来源准入三态分账。 ... ok
----------------------------------------------------------------------
Ran 10 tests in 0.523s

OK
```

原始输出（`python test_screen_prepare.py` 尾部）：

```
test_revenue_only_row_is_not_full_coverage (__main__.MaterialsAndPlan.test_revenue_only_row_is_not_full_coverage)
RC2-A1：一行只有 revenue 不能宣称五字段全覆盖——状态 partial，缺项逐列。 ... ok
----------------------------------------------------------------------
Ran 9 tests in 6.474s

OK
```

### 4.1 覆盖矩阵（10 例 ↔ 验收点）

| 验收点（任务书） | 用例 |
|---|---|
| 复用任务权限，不把管理员任务开放给研究员；跨账号不可见 | `test_cross_account_isolation_both_directions`、`test_unavailable_reasons_explicit` |
| 无权限/无准入事实时**显式** `unavailable`（不静默当"没有材料"） | `test_unavailable_reasons_explicit` |
| 无准入事实 → 拒绝物化（不生成空数据集） | `test_task_without_admitted_facts_refused` |
| 显式选定 → 数据集带 `parent_task_id`/`fact_ids`、`official=False`、`user_provided=True`、「能回到哪份原版」 | `test_explicit_facts_provenance_and_official_flags` |
| 用户没显式选的 task/fact 不得自动带入 | `test_requires_explicit_fact_selection`、`test_unselected_facts_excluded_and_skips_explained` |
| 缺期间/缺字段逐项说明，不猜、不补零 | `test_unselected_facts_excluded_and_skips_explained` |
| 同内容幂等复用；损坏数据集不当成功复用 | `test_idempotent_reuse_same_content`、`test_corrupted_dataset_not_reported_as_reuse` |
| 只读投影内容完整（事实+定位+底稿哈希+来源三态） | `test_projection_lists_facts_sources_and_working_paper` |
| 没有底稿时退到结构化载荷（有契约才判准入） | `test_financials_fallback_without_paper` |

### 4.2 处理器级烟测（不落测试文件，用于确认 HTTP 层接线；数据集在临时目录）

用合成夹具 + 伪造会话调用处理器函数（不起服务、不碰 8080）：

```
GET  /api/screen/prepare/research-materials?task_id=demo-task-1  -> 200 ok，usable=True，facts_admitted=5
GET  /api/screen/prepare/research-materials                      -> 200 ok，tasks_total=3
POST /api/screen/prepare/research-collect {task_id, fact_ids×2, periods:["2024"]}
     -> 200 ok，dataset=a0ac902f…，rows=1，official=False，kind=user_provided，reused=False
POST 同请求（第二次）                                            -> 200 ok，reused=True，同一 dataset_id
POST 带 url 字段                                                 -> 400 client_url_rejected
POST task=无准入事实任务                                          -> 400（错误文本以「无准入事实」开头）
POST 数据集 rows.json 被改坏后同请求                              -> 503 store_unavailable
```

路由表校验（`import web_ui` 反射）：`_GET_ROUTES` 70 条含 `research-materials`，
`_POST_ROUTES` 49 条含 `research-collect`，`_SCREEN_WRITE_PATHS` 含
`/api/screen/prepare/research-collect`；`import web_ui` 成功、`ast.parse` 通过。

---

## 5. 改动清单（只碰允许的文件）

| 文件 | 动作 | 说明 |
|---|---|---|
| `research_materials.py` | **新增** 889 行 | 投影 + 物化，无副作用写盘（除 `collect_from_research` 写本作用域数据集） |
| `test_research_materials.py` | **新增** 453 行 | 10 例；`mkdtemp` 隔离（`WEAVEMIND_SCREEN_DIR`/`AGENTS_DB`/`WEAVEMIND_WORKSPACE_ROOT` 全指向本脚本临时目录），`_wipe()` 先断言路径在临时根内再删 |
| `web_ui.py` | 改 **4 处**（2 处理器 + 2 路由表 + 1 白名单，合计 ~60 行） | 未修改既有分支；未动 `do_GET`/`do_POST` 逻辑 |
| `screen_prepare.py` / `screen_store.py` / `stock_screen.py` / `adapters/` / `frontend/` | **未改** | `screen_prepare.materials_projection` 里的 `research` 段由 Lead 接通（`import research_materials` + `materials_from_research(norm)`），本模块只提供被调用的函数 |

---

## 6. 与 Lead 的接口约定（已完成核对）

- `screen_prepare.materials_projection`（磁盘现版第 277–286 行）调用
  `research_materials.materials_from_research(norm)`，异常时回落
  `{"status": "unavailable", "tasks": [], "note": ...}` —— 与本模块返回结构一致
  （`status` 只有 `ok`/`unavailable`，`tasks` 恒为数组，另带 `unavailable`/`totals`/`note`）。
- `test_screen_prepare.py::MaterialsAndPlan` 全绿即证明该接通链路（投影里带 `research` 字段）无回归。

---

## 7. 真实数据核对（只读）

在**真实** `agents.db` + 仓库 `workspace/` 上跑 `materials_from_research({"user":"momang","project":"personal"})`：

```
status: unavailable
totals: {"tasks_total": 50, "usable": 0, "facts_admitted": 0, "facts_pending": 0,
         "facts_excluded": 0, "scanned_rows": 578, "excluded_other_owner": 61,
         "excluded_unattributed": 467}
- ui-7581c1a827 SUCCESS_WITH_ISSUES facts: 0 paper: False 底稿缺失：任务工作区没有 working_paper.json，也没有 financials.json
- ui-2bcfcbc06d SUCCESS_WITH_ISSUES facts: 0 paper: False 同上
- …（momang 名下 50 个任务逐一同因）
note: …；另有 467 条任务没有提交人身份（历史/系统任务），未纳入投影
```

结论与边界：
- **真实**验证到的：任务库读取、提交人作用域过滤（`excluded_other_owner=61`、`excluded_unattributed=467`）、
  工作区只读定位、逐任务不可用原因、总额统计；全程未创建目录、未写库
  （核对前后 `workspace/task_roots.json` mtime 仍为 2026-10-23 16:00、`agents.db` 仍为 10-23 19:46，
  工作区目录一小时内有 0 个被改动）。
- **未验**（真实数据侧）：准入事实投影与物化链路。原因：仓库 `workspace/**` 里
  **没有任何** `working_paper.json` / `financials.json`（`glob` 命中 0），历史任务当年没有产出底稿；
  `%LOCALAPPDATA%\Temp\agent_workspace` 等历史根也没有。因此事实层 100% 由合成夹具验证
  （`working_paper.json` 手写 rows+selection、`financials.json` 走 `facts_from_financials`+`select_facts`）。
  真实底稿一旦产出（`working_paper_export` 落盘），同一段代码无需改动即可生效——但**本轮没有真实底稿可跑**，
  这里明确记「未验」，不冒充已验证。

---

## 8. 未验项与已知限制

1. **真实底稿/事实物化未验**（见 §7）：没有真实 `working_paper.json`/`financials.json` 可跑；
   端到端「真实任务 → 底稿 → 数据准备 → 筛选」未做实测。
2. **浏览器端未验**：`StockScreen.tsx` 目前只读 `status/note/tasks.length`，没有"勾选事实并物化"的交互；
   `POST /api/screen/prepare/research-collect` 只做了处理器级烟测（§4.2），未在真实 8080 实例上点击验证。
   前端改造不在本任务范围（`frontend/` 未改）。
3. **`periods` 语义**：只支持年度粒度（`2024` / `2024年` / `2024-12-31` → 按年过滤）。
   季报/中报事实不进数据集（当期数据准备只认可比口径的年度行），这是与筛选侧一致的**有意限制**，
   不是遗漏；错误写法会 400 而不是静默丢弃。
4. **底稿缺失即不可物化**：任务若当年没落 `working_paper.json`（真实数据全是这种情况），
   只能走 `financials.json` 退路；两者都没有 → `working_paper_missing`。不会去 `reports/*.md`
   反抽数字来"凑"出材料。
5. **`metadata.user_provided` 口径注记**：`screen_store.save_dataset_from_source` 在 `metadata`
   顶层固定写 `user_provided: false`（该函数是给"服务端核验来源"用的），而 `list_datasets`/
   `dataset_source_meta` 按 `official=false` 派生 `user_provided=true`。本数据集因此是
   **`official=false` + 展示 `user_provided=true`**；provenance 里另有显式 `user_provided: true`。
   这是既有存储契约，本任务不改 `screen_store.py`；若后续要统一字段，属该模块的独立改动。
6. **物化不等于复核**：本入口只把"用户已选定的事实"变成数据集，不写任何 `review_state`/验收结论；
   人工复核仍是用户自己的动作。
7. **副作用披露**（诚实记录）：处理器烟测（§4.2）调用真实 `audit_log`，向仓库 `logs/audit.jsonl`
   追加了 **2 行**审计记录（`action=screen.prepare.research_collect`、`user=a3a`、`ip=127.0.0.1`，
   时间 2026-10-24T04:51:31Z）；它指向的数据集在临时目录、已随测试清理而失效。
   未改写/删除这两行（审计轨迹不可回改）。后续复跑应把 `AUDIT_FILE` 指到临时目录
   （本次回归命令已加 `AUDIT_FILE=$TMPDIR/...`，不再污染仓库审计）。

---

## 9. 复核建议（给 Lead）

1. 跑一遍 `python test_research_materials.py`（应 exit 0，10 例）与 `python test_screen_prepare.py`（应 exit 0，9 例）。
2. `git diff web_ui.py` 只看 4 处：`_SCREEN_WRITE_PATHS` 末尾追加、两个处理器、两处路由表各一行；
   确认没有触碰 `do_POST` 的分支顺序（`_SCREEN_WRITE_PATHS` 命中后仍走 `_screen_can_research` 角色白名单）。
3. 抽查 `research_materials.py` 的三条红线实现点：
   `_row_period`（不猜期末）、`_period_selected`/`to_yuan` 门槛、
   `_row_fact_ids`/`provenance`（回到原版）；以及 `collect_from_research` 里
   `StoreWriteError` **没有**被吞掉。
