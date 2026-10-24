# T1 研究材料入口 + 来源契约 · 交付报告（TRIAL-READY-1024 / T1）

- 作者：teammate `research-materials`；日期：2026-10-24
- 模块：`research_materials.py`（`MODULE_VERSION = "t1-1"`，1190 行）
- 测试：`test_trial_t1.py`（新增 18 用例）+ `test_research_materials.py`（A3 回归 10 用例，随 G5 更新 2 处断言）
- 写集纪律：本轮只改 `research_materials.py` / `test_research_materials.py` / 新建 `test_trial_t1.py` / `tmp/t1-g6-repro.py` / `tmp/t1-t16-summary.py` / 本报告。**未动** `web_ui.py`、`screen_store.py`、`frontend/**`、`screen_prepare.py`、生产库、8080、真实账号；未 commit/推送。
- 审计纪律：所有测试/脚本 `AUDIT_FILE` 指向各自 mkdtemp 临时目录；本轮**零**新增 `logs/audit.jsonl` 写入（末行仍是 05:35:36Z 的 `screen.watchlist.note`，与本任务无关）。

## 0. 结论

| 项 | 状态 |
|---|---|
| G6 权限先于分页（P1） | ✅ 修复 + 修复前/后读数（0/200 → 1/1） |
| G5 来源契约 | ✅ 模块侧落地（枚举+四布尔+归一+不一致可见）；screen_store 侧给 lead 清单 |
| T1.3 接口缺口 | ✅ 条目补齐（origin / working_paper_version / fields / usable_fields / per-fact origin）；剩余缺口列给 lead |
| T1.4 不可用显式 | ✅ 7 种码分账（含损坏 ≠ 缺失 ≠ 不可算） |
| T1.5 底稿哈希绑定 | ✅ `expect_paper_sha256`，底稿变更拒绝按旧选择物化 |
| T1.6 真实导出链路 | ✅ 真实导出器跑通（仅 mock 传输层 `_get`），投影+物化值一致 |

复现命令（都在仓库根，`PYTHONIOENCODING=utf-8`，退出码为真实退出码）：

```
python test_trial_t1.py          → 18 tests OK，exit 0
python test_research_materials.py → 10 tests OK，exit 0
python tmp/t1-g6-repro.py        → G6 before/after JSON，exit 0
python tmp/t1-t16-summary.py     → T1.6 链路摘要 JSON，exit 0
```

## 1. G6 权限先于分页（P1，先修）

### 修法（research_materials.py `_scoped_task_rows`）

- **改前语义**（G6 根因）：`SELECT … ORDER BY created_at DESC LIMIT 200` 先取最新 200 条，**再**在内存里按 `task_history.user == 本人` 过滤 → 别人新增任务把本人旧任务挤到第 200 名之外，材料静默消失。
- **改后语义**：owner 等值限定进 SQL——`WHERE COALESCE(user,'') = ?`，然后 `ORDER BY COALESCE(created_at,'') DESC, task_id DESC LIMIT ? OFFSET ?`；`counts` 只回聚合数（`scanned / owned_total / other_owner / unattributed / scope_filter:"owner_equality(sql)" / limit / offset / has_more`），**不带他人身份明细**。空用户作用域直接返回空（无主任务不属于任何人）。

### 回归读数（`tmp/t1-g6-repro.py`，场景 = architect_review.json G6 原样：1 本人旧任务 2020-01-01 + 801 条他人任务 2026-01-01）

```json
"before": { "returned_owned_tasks": 0, "scanned": 200,
            "verdict": "本人旧任务被 800 条他人新任务挤没（G6 复现）" },
"after":  { "error": "", "returned_owned_tasks": 1, "ids": ["synthetic-owned"],
            "owned_total": 1, "other_owner": 801, "scanned": 1,
            "scope_filter": "owner_equality(sql)",
            "verdict": "旧任务在（owner 等值限定在 SQL 层，先限定再排序分页）" }
```

### 分页读数（5 本人 + 800 他人，limit=2）

```
offset 0 → [t1-owned-1, t1-owned-2]  has_more=true
offset 2 → [t1-owned-3, t1-owned-4]  has_more=true
offset 4 → [t1-owned-old]            has_more=false   （改前这一页会拿到他人任务）
materials_from_research(limit=2, offset=4) → ["t1-owned-old"]，owned_total=5
paging.filter = "先按提交人等值限定，再排序/分页（G6）"
```

三页并集 = 本人全部 5 条、无重叠；`other_owner=800`、`unattributed=3` 分开计；投影里不出现任何 `t1-other-*` id。无主任务对任何账号单查都显式 `no_permission`。

### 测试（test_trial_t1.py · G6OwnershipBeforePagination，4 用例）

`test_owned_old_task_not_hidden_by_800_newer_other_tasks`（**G6 可复现回归断言：造 800 条他人任务**）、`test_pagination_no_overlap_no_loss`、`test_unattributed_and_empty_scope_never_returned`、`test_projection_totals_are_honest`。

## 2. G5 来源契约（研究材料不再借 user_provided 冒充）

### 契约定义（模块侧已落地）

- 稳定枚举 `RESEARCH_ORIGIN="research_material"`；`ORIGIN_KINDS` 全集：`research_material / user_upload / official_cache_materialized / adapter_normalized / synthetic_demo / user_provided（旧标识） / unknown`。
- 四个**独立**布尔（回答四个不同问题）：`user_provided`（是否本人提供材料）/ `official`（是否服务端认定官方）/ `synthetic`（是否合成样例）/ `independent_verification`（是否经独立核验）。研究材料规范值：`True / False / False / False`——投影**不冒充独立核验**。
- 新写入路径统一走 `_research_origin(...)`：数据集 provenance、每条事实的 `facts[].origin`、任务条目的 `origin` 三层同一套形状，都带 `parent_task_id / parent_task_version / working_paper_ref / fact_ids`。
- 读取归一 `origin_identity(meta, provenance)`：旧数据集 `kind="user_provided"` 且带 `parent_task_id` → 归一为 `research_material`（`legacy=True, legacy_kind="user_provided"`）；**只读归一，绝不重写已冻结的数据集文件**（测试里对数据集目录做了字节级 before/after 断言）。
- 不一致可见：存储元信息与规范值冲突时 `consistent=False` + `mismatch=[{field:"meta.user_provided", stored:false, canonical:true}, {field:"meta.provenance_kind", …}]` + `store_fix_required` 指名 screen_store；`collect_from_research` 返回里带 `dataset_origin`（规范答案），note 里提示消费方读它。

### 回归证据（test_trial_t1.py · G5OriginContract，5 用例全绿）

- 物化落盘后 `payload.metadata.provenance.kind == "research_material"`、四布尔正确、`working_paper_ref.sha256 == 底稿文件哈希`；
- 旧 A3 形状数据集（`kind=user_provided` + parent_task_id）读取归一为 `research_material` 且文件未动；
- `screen_store` 现状留痕：`dataset_source_meta({"provenance_kind":"research_material"})` 的 `provenance.kind` 被降级为 `"unknown"`（`_PROVENANCE_KINDS` 不含它）——这条测试在 lead 修完 store 后会自动转成正向断言。

### 需要 lead 改的清单（screen_store / web_ui / frontend）

1. **`screen_store._PROVENANCE_KINDS`** 加 `"research_material": "本人研究材料（任务事实投影）"`（否则 `dataset_source_meta` 与 `_provenance` 都把它降级成 unknown）。
2. **`save_dataset_from_source`**（现第 636/650–655 行）：去掉写死的 `payload["metadata"]["user_provided"] = False`；改为按 provenance 落四个布尔——`user_provided=bool(prov.get("user_provided"))`、`official=bool(prov.get("official")) and not user_provided and not synthetic`、`synthetic=bool(prov.get("synthetic"))`、`independent_verification=bool(prov.get("independent_verification"))`；并把 `parent_task_id / parent_task_version / parent_task_owner / working_paper_ref / paper_sha256 / acquisition` 透传进 `extra_meta`（本模块 provenance 已全部带齐，store 只需透传）。
3. **读侧**：`list_datasets` 不再推导 `user_provided = not official`，改为读 meta 上的规范布尔；`dataset_source_meta` 同步（可调 `research_materials.origin_identity` 做同一个归一，别在 store 里再写一份归一逻辑）。
4. **兼容旧数据**：读旧数据集（kind=user_provided + parent_task_id）按 `origin_identity` 归一显示为 research_material，**不重写冻结内容**（模块侧 `origin_identity` 已实现并有测试，store 直接复用即可）。
5. **前端（StockScreen.tsx PrepMaterials.research）**：类型已兼容（`tasks[].entities` 实为对象数组 `{entity, entity_id, code, market, facts, admitted_facts}`，页面只读 `status/note/tasks.length`）；后续如要显示「哪些字段能算」，可用新增的 `fields / usable_fields / working_paper_version.short`，无需改后端形状。
6. web_ui 无需改动：`_GET/POST` 路由与 `_post_screen_prepare_research_collect(self, p, body, admin)` 形状不变（A3 已接线并 smoke 过；本轮未再跑 handler 级 smoke，避免再写审计）。

## 3. T1.3 接口缺口清单（给 lead）

已补（`materials_from_research` / `research_material` 每条任务）：

- `origin`（任务级，`kind=research_material` + parent/fact_ids/working_paper_ref）；
- `working_paper_version`（`source / sha256 / short / rows / task_updated_at`）——「能回到哪份原版」；
- `fields`（按 `stock_screen.FIELD_SPECS` **同一份判据**逐字段给 `status: ok|partial|missing` + `need/missing/periods/unit/note`）与 `usable_fields`——「哪些当前字段能算」，筛选改字段这里不漂；
- `facts[].origin`（事实级来源身份，贯穿）；
- `totals` 增 `owned_total / has_more / scanned_rows`，`paging{limit,offset,owned_total,has_more,filter}`；
- `unavailable[].hint`（每种码都带用户能照着做的提示）。

仍未闭环、需要 lead / 后续任务接的：

1. **screen_store 写读两侧**（上面 §2 清单 1–4）——这是 G5 的最后一公里；
2. **前端**要展示 `fields/usable_fields`（T1.3 的"缺"主要剩这层呈现）；
3. **事实层主体身份**：`facts_from_financials` 的 `entity_id` 取 `financials.json` 的 `metadata.stock_code`（本地裸码 `600887`），带后缀的 `SECUCODE=600887.SH` 在 `source_subject.code` 里——真实链路实测行 `code="600887"`。若筛选自选股 join 用带后缀代码，这里会接不上（**需要 facts.py 或消费侧归一**，本轮未动）；
4. **GET /screen/prepare/research-materials 的分页参数**：`web_ui._get_screen_prepare_research_materials` 目前不透 `limit/offset`（默认全量本人任务，G6 修复后安全，但任务多时可接上 `paging`）。

## 4. T1.4 不可用原因显式（损坏 ≠ 没有材料 ≠ 不能算 ≠ 没权限）

`UNAVAILABLE_REASONS` 7 种码，每种带 `reason + hint`，测试逐一断言（test_trial_t1.py · T1ProjectionShape）：

| 码 | 语义 | 断言要点 |
|---|---|---|
| `workspace_missing` | 工作区目录不在 | 任务在库里、目录没了 ≠ 无底稿 |
| `working_paper_missing` | 工作区在、无底稿 | facts_admitted=0 时列表 status=unavailable（不是 ok） |
| `working_paper_corrupt` | 底稿在但读不了 | `working_paper.present=True + readable=False + warnings`（不伪装成 missing） |
| `financials_corrupt` | 结构化载荷在但读不了 | `_read_json` 以 sha 有无区分"不存在/损坏"（本轮修） |
| `no_admitted_facts` | 只有待核验/剔除 | 不生成空数据集 |
| `not_computable` | 准入了但算不出 | **事实还在**（facts_admitted≥1），只是白名单/期间/单位不满足 |
| `no_permission` | 非本人任务 | 管理员/系统/无主任务不自动开放，不泄露标题 |

另：底稿损坏但 `financials.json` 可用时**不阻断**——投影来自 financials，`warnings` 里如实写「底稿损坏；本次投影来自 financials.json」。

## 5. T1.5 选择绑定底稿哈希

`collect_from_research(..., expect_paper_sha256=...)`：

- 哈希一致 → 物化成功，`provenance.paper_sha256` 与 `working_paper_ref.sha256` 绑定文件哈希；
- 选择与提交之间底稿被更正 → 拒绝：`"底稿已变更：现在是 9fa879e11b14…，选择时是 …；请重新查看该任务的事实并再次确认（不按旧选择物化）"`，且不产生数据集；
- 底稿没了 → `"无法确认底稿版本…请重新查看材料后再选"`。

测试：`test_changed_paper_rejects_old_selection`（改写底稿内容后按旧哈希提交被拒、按新哈希可过）、`test_correct_sha_binds_paper_in_provenance`、`test_missing_paper_cannot_bind`。

## 6. T1.6 真实导出链路（真实代码，仅传输层 mock）

`tmp/t1-t16-summary.py` 的原始读数（exit 0）：

- `resolve_request` → `source="stored"`、company_id `600887.SH`、market `cn`、periods `[2023, 2024]`（契约来自落库 research_request_json）；
- `route_structured_for_request`（`eastmoney._get` mock，`cninfo.enabled` mock False）→ `source="eastmoney_ashare"`、`annual_count=2`、带 `contract`/`resolution` 键；`financials.json` 用与 `structured_pipeline` 相同的 `json.dumps(data, ensure_ascii=False, indent=1)` 落盘；
- `working_paper_export.write_working_paper`（真实导出器）→ `ok=true, rows=8, derived=8`，gaps 2 条（operating_cashflow 2023/2024 如实缺失，不推断），产出 `working_paper.json/.csv` 两件套，sha256 `9fa879e1…ee89`；
- 投影：`facts_source="working_paper.json"`、`facts_total=8、facts_admitted=8`、`paper_sha256_matches_file=true`、`usable_fields=["revenue_growth","gross_margin"]`（与 FIELD_SPECS 判据一致）；
- 物化（带哈希绑定）：行 `{period:"2024-12-31", code:"600887", unit:"元", revenue:115780000000.0}`（亿元×1e8 回到原始元金额）；`provenance_kind="research_material"`；底稿文件投影/物化前后字节不变。

同链路在 `test_trial_t1.py · T1RealExportChain.test_export_then_project_then_collect` 里作为常驻回归（mock 只在 `eastmoney._get` 传输层，适配器/契约/导出器/投影/物化全是真实代码）。

## 7. 测试隔离与命令

- 每个测试文件各自 `tempfile.mkdtemp`；`AGENTS_DB / WEAVEMIND_SCREEN_DIR / WEAVEMIND_WORKSPACE_ROOT / AUDIT_FILE / WEAVEMIND_LOG_DIR` 全部指向临时根；`_wipe()` 先断言路径在临时根内再清理（`test_trial_t1.py` 的 `_wipe` 还覆盖 `logs/` 与 `audit.jsonl`）；
- `A3` 测试的一处夹具更新：`T_NOPAPER` 现在显式建空工作区目录（"无底稿" 与 "工作区缺失" 是两个码，A3 断言 `working_paper_missing` 不变，`workspace_missing` 由新增任务覆盖）；
- 未起服务、未连网（唯一"网络"是被 mock 的 `_get`）、未碰 8080、未 commit。

## 8. 未验项 / 风险

1. **screen_store 侧修改未落地**（需 lead）：`dataset_source_meta` 仍会把 `research_material` 降级为 unknown、`user_provided` 仍写死 False——在 lead 修完之前，`dataset_origin.consistent=False` + `mismatch` 会如实出现在每次物化返回里（这是设计好的可见状态，不是回归）；
2. **生产真实任务的底稿路径仍未验**：A3 已查过生产 `workspace/` 下没有任何 `working_paper.json` / `financials.json` / `materials/index.json`，50 个 `momang` 任务全部 `working_paper_missing`；本轮 T1.6 用 mock 传输层证明的是**代码链路**，不是生产数据的存在性；
3. **`entity_id` 裸码问题**（见 §3.3）：真实链路行 `code="600887"`，与筛选自选股代码（通常带后缀）的 join 需要消费侧确认/归一；
4. **web_ui handler 级 smoke 本轮未重跑**（路由/签名 A3 已验过，本轮模块签名向后兼容：`materials_from_research` 新增 `offset` 关键字参数带默认值，`screen_prepare.py:301` 的 `rm.materials_from_research(norm)` 不受影响；避免再写生产审计日志）；
5. `tmp/t1-*.py` 是一次性证据脚本（写集内），lead 验完可删。
