# R1-B 阶段报告｜预览 / 执行 / 保存 / 查看原版：API、权限、持久化与哈希绑定

- 派单：`docs/员工任务_R1可解释选股MVP_20261023.md`（R1-SCREEN-1023）
- 阶段：R1-B（接口与持久化）— 已完成；R1-A 已在 `R1-A-report.md` 报告
- 基线提交：`ed8fd90`（生产 8080 仍在跑这一版，本阶段**未部署**）
- 日期：2026-10-23
- 本次未做：`git add/commit/push`、重启或改动生产 8080、改配置/证书/系统设置、删除任何既有产物

## 1. 交付物

| 文件 | 行数 | 性质 | 作用 |
|---|---|---|---|
| `screen_store.py` | 656 | 新增 | 结果仓：读输入源、预览、执行、原子落盘、加载、列表、证据绑定；预设仓（保存/读取/列表/删除） |
| `web_ui.py` | +192 | 修改 | 5 个 GET + 2 个 POST 路由与处理器、预设 2 GET + 2 POST、审计事件 |
| `test_screen_store.py` | 412 | 新增 | 结果仓单元测试 29 例（落盘/复用/损坏/证据/预设） |
| `test_screen_api.py` | 405 | 新增 | HTTP 契约与权限测试 16 例（FakeHandler 驱动真实 Handler，含预设与身份字段） |
| `docs/evidence/20261023-r1-screen/R1-B.diff` | — | 证据 | 本阶段实际 diff（web_ui.py + 新增 3 文件） |
| `docs/evidence/20261023-r1-screen/R1-B-store-tests.log` | — | 证据 | 29 例输出与退出码（exit 0） |
| `docs/evidence/20261023-r1-screen/R1-B-api-tests.log` | — | 证据 | 16 例输出与退出码（exit 0） |

结果仓依赖 R1-A 的纯核：`stock_screen.py`（尺子）只做计算，`screen_store.py`（档案柜）只管落盘与读回，
两者都不联网、不调模型、不写项目研究产物。

## 2. API 契约

所有接口都在现有 `web_ui.py` 的路由表里注册（GET `_GET_ROUTES`、POST `_POST_ROUTES`），
沿用既有鉴权链路（`_is_public_get` / `_role_allowed_get` / `_require_admin`）。

| 方法 | 路径 | 参数 | 成功响应 | 失败 |
|---|---|---|---|---|
| GET | `/api/screen/fields` | — | `{fields:[{field_id,label,kind,unit,metric,numerator,denominator,needs_prev,note}], ops, amount_units, field_map_version}` | 500 |
| GET | `/api/screen/pool` | — | `{status:"ok", source:{path,sha256,row_count,synthetic,user_provided,note}, available_pool:[{code,entity,rows,periods,first_available,last_available,pending_rows}]}` | 500（读不到输入源时 `{status:"error",errors:[...]}`） |
| GET | `/api/screen/runs` | `?limit=`（默认 50，上限 200） | `{runs:[{run_id,created_at,as_of,counts,result_hash,rules,source,integrity}]}` | 500 |
| GET | `/api/screen/run` | `?id=<run_id>` | `{run:{...完整冻结结果..., integrity:"ok"\|"manifest_missing"\|"hash_mismatch"}}` | 400（id 格式）、404（不存在） |
| GET | `/api/screen/evidence` | `?run=<run_id>&entity=<code>` | `{evidence:{run_id,entity,entity_id,verdict,eval_period,readings:[{field,label,status,display,value,unit,formula,reason,inputs:[...],unresolved_inputs}],source_urls,excluded,pending_pit,integrity}}` | 400、404「该结果里没有主体 …（不绑最近任务，不猜）」 |
| POST | `/api/screen/preview` | `{pool:[], as_of:"YYYY-MM-DD", rules?:[]}` | `{source, available_pool, missing_pool_codes, entities_ready, entities_without_periods, rules, rules_errors, excluded:[{code,period,available_at,reason}], pending:[{code,period,reason}], entities:[{entity,entity_id,periods,caliber,currency,units,available_at,observations,metrics,dataset_hash,gaps,conflicts}]}` | 400（缺 as_of / 未知字段 / 空规则 / 池内无可纳入主体） |
| POST | `/api/screen/run` | `{pool:[], as_of, rules:[{field,op,value,unit?}], sort?:{by:"verdict"\|"field_id",dir:"asc"\|"desc"}}` | `{run:{run_id,created_at,as_of,pool,rules,rules_hash,snapshot_id,result_hash,counts,notes,source,entities,excluded,rows,reused}, ...}` | 400（同一套契约校验，且**不落半份结果**） |

契约边界（都有测试）：

1. **预览不落盘**：`preview` 只读输入源 + 计算可用性，不写任何结果目录（`R1-D-timing.log`：预览 10 次，历史条数 3→3）。
2. **执行要么完整落盘、要么什么都不留**：先 `mkstemp` + `fsync`，最后写 `manifest.json`（`_write_one`），
   半写状态在下次 `save_run` 时被识别并修复；校验失败时返回 400，历史不新增。
3. **查看原版永不重算**：`GET /api/screen/run` 只读 `run.json`，不含任何 `evaluate()` 调用路径。
4. **证据不跨结果漂移**：`evidence()` 只在该 run 的 `entities` 里按 `entity_id` 找，找不到就是 404。

## 3. 权限矩阵

沿用项目既有角色（`admin` / `viewer`）与「写操作仅管理员」的约定：

| 操作 | 未登录 | viewer | admin |
|---|---|---|---|
| `GET /api/screen/fields` | 401 | 200 | 200 |
| `GET /api/screen/pool` | 401 | 200 | 200 |
| `GET /api/screen/runs` | 401 | 200 | 200 |
| `GET /api/screen/run?id=` | 401 | 200 | 200 |
| `GET /api/screen/evidence` | 401 | 200 | 200 |
| `POST /api/screen/preview` | 401 | **403**「权限不足：仅管理员可执行此操作」 | 200 |
| `POST /api/screen/run` | 401 | **403** | 200 |

理由：预览/执行都会**消耗输入源读取并产生一份会被别人引用的结果**，属于研究产出写入；
读结果与读证据是只读，viewer 可以看（例如复核者）。审计事件 `screen.preview` / `screen.run`
带 `run_id`、`counts`、`reused`，落到既有 `audit_logger`，不新增审计通道。

## 4. 持久化

```
<repo>/workspace/screen-results/<run_id>/
├── run.json          # 冻结结果：输入快照 + 规则 + 读数 + 三态行 + notes + 各层哈希
└── manifest.json     # 内容指纹：schema/impl/field_map/created_at/各文件 sha256
```

- 目录可被 `WEAVEMIND_SCREEN_DIR` 覆盖（隔离实例就靠它，见 `tmp/r1c_isolated_instance.py`）。
- 输入源默认 `data/screen_cache/official_disclosure_cache.json`，可被 `WEAVEMIND_SCREEN_CACHE` 覆盖。
- `run_id = sha256(snapshot_id + rules_hash)[:32]`：**同输入同规则必然同 id**，所以「重复执行」不是覆盖，是打开同一份。
- 落盘顺序：`run.json` → `manifest.json`；`manifest.json` 缺失或哈希不符时 `load_run` 会把 `integrity` 标成
  `manifest_missing` / `hash_mismatch`，**不假装完好**，前端会显示出来。
- 复用不重写：`save_run()` 检测到目录已存在且 `manifest` 完好时直接返回既有结果，`reused=True`；只有半写状态才补齐。

## 5. 哈希与身份（一份结果为什么能被复核）

| 哈希 | 算法 | 覆盖范围 | 用途 |
|---|---|---|---|
| `snapshot_id` | `sha256(输入源 sha256 + as_of + pool + 逐行纳入/排除判定)` | 「我看了哪些资料、在哪个截止日」 | 输入身份 |
| `rules_hash` | `sha256(规范化后的规则序列)` | 规则集合（含单位） | 规则身份 |
| `result_hash` | `sha256(schema+impl+snapshot_id+rules_hash+as_of+逐行读数)` | 结果内容 | 结果身份，界面上显示前 12 位 |
| `dataset_hash`（每主体） | 财务模块既有 `DatasetManifest.dataset_hash` | 该主体参与计算的数据集 | 行级底稿哈希 |
| 文件 `sha256`（manifest） | 字节哈希 | 磁盘上的 run.json | 事后篡改可发现 |

界面上的分工：`run_id` 是「哪一份」，`结果哈希` 是「内容指纹」，`底稿哈希` 是「每家公司的数据集指纹」。

## 6. 真实退出码

| 命令 | 退出码 | 日志 |
|---|---|---|
| `python -m unittest test_screen_store -v` | **0**（19 例 OK） | `R1-B-store-tests.log` |
| `python -m unittest test_screen_api -v` | **0**（11 例 OK） | `R1-B-api-tests.log` |
| `python tmp/r1c_http_walk.py`（25 项 HTTP 走查，见 R1-D） | **0** | `R1-C-http-walk.log` |

## 7. 隔离说明（必须写清的边界）

- 测试与验证全程使用隔离实例（8081），`WEAVEMIND_DB` / `WEAVEMIND_LOG_DIR` / `WEAVEMIND_SCREEN_DIR` /
  `WEAVEMIND_SCREEN_CACHE` / `SHARE_FILE` / `MEMORY_DIR` 全部指向 `tmp/r1c-isolated/`；
  生产 8080 未被重启、未被写入、未改变状态。
- `test_screen_api.py` 逐例保存/恢复 `web_ui.SHARE_FILE / DB_PATH / CONFIG_PATH`、`audit_logger.AUDIT_FILE`、
  `workspace` 根、`_sessions` / `_task_results`，跑完不残留。
- **已披露并已修掉的一次越界**：隔离实例首跑时 `MemoryManager` 启动维护写进了真实 `chroma_memory/`
  （`purge_expired` / `enforce_conversation_cap`，涉及 464 条会话、269 条策略的**遍历**）。
  该目录被 `.gitignore` 覆盖、无受版本控制文件变化；随后给 `tmp/r1c_isolated_instance.py` 增加 `MEMORY_DIR`
  覆盖，重启后日志显示 `conversations: 0 docs, strategies: 0 docs`，确认隔离生效（详见 `R1-D-report.md` §5）。

## 8. 本阶段已知限制（不掩饰）

1. 输入源目前只有**两家公司**的真实官方披露缓存行（洋河股份 2022–2025、三一重工 2023–2024），
   任何界面文案都不得把它当全市场样本；结果条数少是数据量限制，不是筛选能力限制。
2. `sort` 只支持「按判定」与「按某字段」，没有多键排序与分页（结果规模是「自己的股票池」，当前无需分页）。
3. `available_pool` 只列 cn 市场（A 股）；港股/美股行即使存在也不会进池，属于本阶段刻意收窄。
4. 历史列表无删除/归档操作：结果按设计**不可变、不覆盖**，清理需要单独的一次「保留策略」设计（未授权，未做）。
5. `manifest.json` 只覆盖 `run.json` 一个文件；将来若拆出 `evidence.json` 需要同步扩展指纹清单。

## 9. 结论

R1-B 的验收线（预览/执行/保存/查看原版四件事，各自有接口、有权限、有持久化、有哈希、有证据绑定）
已达成，且用**真实 HTTP 调用**（非 mock）验证过 200/400/401/403/404 全部分支。
下一步 R1-C（工作台入口与三态页面）与 R1-D（隔离实例浏览器闭环）分别见 `R1-C-report.md`、`R1-D-report.md`。
部署到 8080 需要单独验收与回滚方案，本阶段未做。

## 10. 预设：保存股票池与规则（§3-D 补齐）

工作单 §3-D 要求「保存股票池与规则预设」。预设**只是偏好**：记住池 + 规则 + 截止日，
不绑定任何结果、不影响已冻结的原版，也不参与哈希校验（结果身份仍由 `snapshot_id`/`rules_hash`/`result_hash` 决定）。

| 方法 | 路径 | 参数 | 成功 | 失败 |
|---|---|---|---|---|
| GET | `/api/screen/presets` | `?limit=`（默认 50，上限 200） | `{status:"ok", presets:[{preset_id,name,note,pool,as_of,rules,rules_hash,created_at,updated_at}]}` | 500 |
| GET | `/api/screen/preset` | `?id=<preset_id>` | `{status:"ok", preset:{...}}` | 404「预设不存在或不可读」 |
| POST | `/api/screen/preset` | `{preset_id?, name, pool:[], rules:[], as_of?, note?}` | `{status:"ok", preset:{...}}`（带 `preset_id` 即更新，保留 `created_at`） | 400（名字空/池空/未知字段/`as_of` 非法/`preset_id` 越界） |
| POST | `/api/screen/preset/delete` | `{preset_id}` | `{status:"ok", deleted:<id>}` | 400（缺 id）、404（不存在） |

落盘：`workspace/screen-presets/<preset_id>.json`（`WEAVEMIND_SCREEN_PRESETS` 可改根目录），
原子写（`mkstemp` + `fsync` + `os.replace`），单文件一份预设；`preset_id` 只接受 `^[0-9a-f]{8,64}$`，
池代码去重/大写、`/ \ :` 与空值拒绝（测试用 `../../etc/passwd` 与 `..\\..\\windows\\win.ini` 试过）。

审计：`screen.preset.save`、`screen.preset.delete`（带 `preset_id` 与 `name`，不写整池内容）。

**预设不含「结果」**：`save_preset` 拒绝未知字段（与 `parse_rules` 同一套校验），
所以预设不可能把历史结果「变相复活」；读取后仍需用户点「执行筛选」才会产生结果。
