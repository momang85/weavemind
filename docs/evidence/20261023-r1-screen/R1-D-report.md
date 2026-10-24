# R1-D 阶段报告｜隔离实例浏览器闭环、权限/稳定性/构建验证与部署前状态

- 派单：`docs/员工任务_R1可解释选股MVP_20261023.md`（R1-SCREEN-1023）
- 阶段：R1-D（验收与证据）— 已完成
- 基线：`ed8fd90`；日期 2026-10-23
- 结论一句话：**闭环在隔离实例上跑通、证据齐全、生产 8080 未部署且未重启**；
  上 8080 需要单独验收与回滚方案，本阶段未做。

## 1. 验证拓扑（为什么不是生产）

| 实例 | 端口 | 输入源 | 用途 |
|---|---|---|---|
| 生产 | 8080 | 真实缓存 | **只观察、不触碰**：未重启、未重建、未写入 |
| 隔离 A | 8081 | `data/screen_cache/official_disclosure_cache.json`（真实官方披露缓存，2 家公司） | 正/反/未知/时间边界/权限/复用/证据跳转 |
| 隔离 B | 8082 | `tmp/r1c-conflict/conflict_cache.json`（**合成**冲突夹具：同一(指标,期间,口径) 出现「元」和「亿元」） | 冲突路径与合成数据标识 |

隔离手段（`tmp/r1c_isolated_instance.py`，49 行）：`WEB_PORT`、`WEAVEMIND_DB`、`WEAVEMIND_LOG_DIR`、
`WEAVEMIND_SCREEN_DIR`、`WEAVEMIND_SCREEN_CACHE`、`SHARE_FILE`、`MEMORY_DIR` 全部指向 `tmp/r1c-*`，
并覆盖 `web_ui.CONFIG_PATH` 与 `web_ui.DIST_DIR = frontend/dist-screen-check`。
隔离实例自建 `admin` / `viewer` 两个账号，与生产用户库无关。

## 2. 测试矩阵（每条都有真实输出）

主证据：`R1-C-http-walk.log`（33 项，**退出码 0 / ALL CHECKS PASSED**）、`screenshots/`、`R1-D-timing.log`。

| 类别 | 用例 | 期望 | 实测 |
|---|---|---|---|
| 正向 | 池含洋河+三一，截止 2025-04-30，毛利率≥70 且资产负债率≤60 | 通过 1 / 未通过 1 | 通过 1（洋河 73.16%、23.24%，2024年）/ 未通过 1（三一 26.43%，理由「毛利率 26.43% ≥ 70.0%：不满足」） |
| 反向 | 阈值抬到 99% | 通过 0，且是「筛选结果」不是「暂无数据」 | 通过 0 家 + 诚实空态文案（`05-zero-pass-and-insufficient.png`） |
| 未知 | 真实池里混入 `999999.SZ` | 计入资料不足并说明原因，不编造读数 | `insufficient:1`，理由「输入里没有该主体的观察：不编造读数」 |
| 未知（整池） | 池内**全部**代码都不在输入里 | 明确拒绝，不返回空结果冒充筛选 | HTTP 400「输入里没有这些主体的观察：999999.SZ；股票池内没有任何可纳入主体」 |
| 冲突 | 隔离 B：2024 年出现元/亿元两套观测 | 不择一、不平均；冲突期间不参与比较，且**说出来** | 洋河回退到 2023年（75.25%/25.42%），notes 给「002304.SZ 最新期间 2024年 存在冲突读数（11 条）：已回退到 2023年 评估，该期间不参与比较」；界面另有合成数据横幅（`08-conflict-synthetic.png`） |
| 时间边界（含） | 三一 2024 年报披露 2025-04-18，截止 2025-04-30 | 可用 | 期间含 `2024年` |
| 时间边界（不含） | 同上，截止 2025-04-17 | 不可用 | `periods=[]`、`last_available=None` |
| 时间边界（未来行） | 洋河 2025 年报可用时点 2026-04-28，截止 2025-04-30 | 排除并给理由 | excluded 1 行：「资料可用时点 2026-04-28 晚于截止日 2025-04-30：不纳入（不倒灌）」 |
| 契约 | 未知字段 `net_margin` | 400 拒绝，不替换相近字段 | 400「未知字段「net_margin」：可选 …」 |
| 契约 | 空规则 / 缺 `as_of` | 400 | 400 |
| 安全 | `run_id` 路径穿越 | 404 | 404「结果不存在或不可读」 |
| 证据绑定 | 请求不在该结果里的主体 `999999.SZ` | 404，不绑最近任务 | 404「该结果里没有主体 999999.SZ（不绑最近任务，不猜）」 |
| 权限 | 未登录 GET/POST | 401 | 401 |
| 权限 | viewer POST 预览/执行 | 403 | 403「权限不足：仅管理员可执行此操作」 |
| 权限 | viewer GET 原版/历史 | 200 | 200 |
| 复用 | 同参数重复执行 | 同 `run_id`，`reused=true`，不重算 | 同 `run_id`（`ffccec3d…`/`d8cc9a02…`/`540d2de8…`），结果哈希一致；界面给「…哈希一致：直接打开那份原版…未重算」 |
| 不变性 | 换阈值 | 新 `run_id`，旧结果原样保留 | 新 id `07ff3954…`，历史 5 份并存 |
| 完整性 | 读回原版 | `integrity=ok`，哈希与执行时一致 | ok；10 次 GET 哈希稳定 |
| 预设 | admin 保存「高毛利验证池」并读回 | 200，名称/池/规则一致 | 200，`preset_id=42f71ec8…`，name/pool/2 条规则全部一致 |
| 预设 | 带 `preset_id` 保存 | 同 id 覆盖，不新增 | 列表只剩「高毛利验证池(改)」，无重复 |
| 预设 | 预设里放未知字段 `net_margin` | 400，不替换 | 400「rules[0] 未知字段「net_margin」：可选 …」 |
| 预设 | `preset_id=../../etc/passwd` | 400，不落盘 | 400「预设 id 非法」 |
| 预设 | 删除预设 | 200，只删这一份 | 200；剩 1 份其他预设（`3c33aeae`），runs 6→6 不变 |
| 预设 | 再读已删除预设 | 404 | 404「预设不存在或不可读」 |
| 预设 | 未登录读 / viewer 保存 | 401 / 403 | 401「未登录或登录已过期」、403「权限不足：仅管理员可执行此操作」 |
| 过期提示 | 输入文件变了之后打开旧原版 | 明确警示，不冒充现状 | 警示条「这份原版与当前实现/输入不一致（只能查看，不冒充现状）」+「输入文件已变：这份用的是 4dc828ef0bea…，当前缓存是 ed5d48ddf31f…」（`11-stale-input-changed.png`） |
| 过期提示 | 旧实现版本（模拟 `stock_screen/0.9.0`）的原版 | 同时给出两条原因 | 「实现版本已变：这份由 stock_screen/0.9.0 生成，当前是 stock_screen/1.0.0」+「输入文件已变…」（`12-stale-old-version.png`）；原版读数与哈希不变 |

## 3. 耗时与复算分账（`R1-D-timing.log`，隔离 8081）

| 操作 | 次数 | 耗时 | 关键观察 |
|---|---|---|---|
| `POST /api/screen/preview` | 10 | min 14.8 / 中位 16.3 / max 19.7 ms | 失败 0 次；**历史条数 4→4，预览不落盘** |
| `POST /api/screen/run`（首次） | 1 | 48.0 ms | `reused=false`，`run_id=540d2de8…` |
| `POST /api/screen/run`（同参数） | 1 | 39.5 ms | `reused=true`，同 `run_id`、同 `result_hash` |
| `GET /api/screen/run?id=`（查看原版） | 10 | min 8.8 / 中位 11.2 / max 13.1 ms | 哈希与完整性稳定；无任何重算路径 |
| `GET /api/screen/evidence` | 5 | min 8.1 / 中位 8.5 / max 9.7 ms | 绑定 `run_id`+`entity` |

量级说明：单次筛选 50ms 以内，是"自己的股票池 + 5 个财务字段"的规模；不据此推断全市场规模。
（本阶段数字比 R1-C 时更高，是因为隔离实例的输入换成了本机副本、且机器同时在跑 3 个实例与前端构建；两次都完整记录，不挑好看的。）

## 4. 类型 / 构建 / 测试 真实退出码

| 命令 | 退出码 | 日志 |
|---|---|---|
| `npx tsc -b`（frontend） | **0** | `R1-C-build.log` |
| `npx vite build --outDir dist-screen-check` | **0** | `R1-C-build.log`（`StockScreen-BRHbqWwt.js` 30.31 kB / gzip 10.12 kB） |
| `python -m unittest test_stock_screen test_screen_store test_screen_api` | **0**（94 例 OK） | `R1-D-tests.log` |
| `python -m unittest test_stock_screen`（单跑，49 例） | **0** | `R1-A-tests.log` |
| `python -m unittest test_screen_store`（29 例） | **0** | `R1-B-store-tests.log` |
| `python -m unittest test_screen_api`（16 例） | **0** | `R1-B-api-tests.log` |
| `python tmp/r1c_http_walk.py`（33 项） | **0** | `R1-C-http-walk.log` |
| `python tmp/r1c_timing.py`（耗时/复用/稳定性） | **0** | `R1-D-timing.log` |

回归抽查（既有测试，确认没碰坏别人的东西）：`test_auth_audit` 27/27、`test_transport_deadline` 22/22、
`test_lora_manager` 9/9、`test_data_source_settings` 45/45、`test_startup_readiness` 77/77，均退出码 0
（逐项输出见 `R1-D-regression.log`）。

## 5. 隔离瑕疵：一次已修掉的越界（必须披露）

隔离 A **第一次启动**时，`MemoryManager` 的启动维护跑在了真实记忆库 `chroma_memory/` 上
（`purge_expired` + `enforce_conversation_cap`，遍历到 464 条会话、269 条策略）。

- 影响面：该目录被 `.gitignore` 覆盖，`git status` 无受控文件变化；操作是"过期清理/上限裁剪"，
  未删除仍在有效期内的会话，也没有对外写入。
- 处理：给 `tmp/r1c_isolated_instance.py` 增加 `MEMORY_DIR` 覆盖，重启后日志为
  `Memory maintenance done: conversations: 0 docs, strategies: 0 docs` —— 隔离生效。
- 结论：**这是一次真实的隔离缺口，已修补并留证**；后续隔离实例必须显式设置 `MEMORY_DIR`。

## 6. 生产状态（`R1-D-state.log`）

- `GET http://127.0.0.1:8080/api/health` → `status: ok`，`instance: inst-cf0d8d8dd0`，`pid 27356`，
  `started_at 2026-10-23T08:11:04`，`uptime 14021s`（**未重启**）。
- `build.source`：`loaded_sha de24c0b55aba` / `disk_sha c64df6b33930`，`changed: ["web_ui.py"]`，
  `restart_required: true` —— 因为我在磁盘上改了 `web_ui.py`；**运行时仍是最初加载的版本**，
  所以 8080 的行为没有变。这一点会被项目自带身份模块如实报出来，不需要我"声明清白"。
- `build.frontend`：`loaded_index_sha == disk_index_sha`、`state: built`、`changed: false`
  → 生产前端产物**未被重建**（`frontend/dist/index.html` mtime 仍是 2026-10-22 23:39:35）。
- `workspace/screen-results` 在生产仓库**不存在**（所有落盘都在 `tmp/` 隔离目录）。
- `git rev-parse HEAD` = `ed8fd90`；受控改动只有 3 个修改文件 + 5 个新增文件 + 证据目录（见 §8）。

## 7. 在线是否生效（明确回答）

**没有。** 生产 8080 现在既没有 R1-C 页面（旧 `frontend/dist` 里没有 `/screen` 路由），
也还在跑旧的 `web_ui.py`（无 `/api/screen/*`）。要用必须按顺序做两件事：
① 重建 `frontend/dist`（覆盖生产前端产物）；② 受控重启 `launcher.py`（会重载 `web_ui.py`）。

顺序约束：先重建前端再重启后端，避免"新后端 + 旧前端"的中间态；
但重启会中断 16 个服务进程与在跑任务，**必须走单独的部署验收 + 回滚方案**（本阶段未授权，未做）。

## 8. 本阶段改动清单（受控）

```
 M frontend/src/App.tsx
 M frontend/src/components/AppLayout.tsx
 M web_ui.py                       (+192 行，5 GET + 2 POST + 预设 2 GET + 2 POST)
?? stock_screen.py                 (872 行，R1-A 纯核 + R1-D 冲突回退说明)
?? screen_store.py                 (656 行，结果仓 + 预设仓)
?? test_stock_screen.py            (597 行 / 49 例)
?? test_screen_store.py            (412 行 / 29 例)
?? test_screen_api.py              (405 行 / 16 例)
?? frontend/src/pages/StockScreen.tsx (971 行，含预设与过期提示)
?? tmp/r1c_isolated_instance.py    (50 行，隔离实例，含 SCREEN_PRESETS 覆盖)
?? tmp/r1c_http_walk.py            (237 行，33 项 HTTP 走查)
?? tmp/r1c_timing.py               (101 行，耗时/复用分账)
?? data/screen_cache/official_disclosure_cache.json (10.3KB，真实官方披露缓存)
?? docs/evidence/20261023-r1-screen/  (报告/diff/日志/截图/演示产物)
```

未触碰的既有脏路径：`docs/evidence/20261012-c5-review/README.md`、`evals/cases/auto_grown.json` 等保持原样。

## 9. 证据索引

| 文件 | 内容 |
|---|---|
| `R1-A-report.md` / `R1-A.diff` / `R1-A-tests.log` | 字段清单与纯核（49 例） |
| `R1-B-report.md` / `R1-B.diff` / `R1-B-*-tests.log` | 接口、权限、持久化、哈希、预设（29+16 例） |
| `R1-C-report.md` / `R1-C.diff` / `R1-C-build.log` | 页面与构建 |
| `R1-C-http-walk.log` | 33 项真实 HTTP 走查（全 PASS，含预设 12 项） |
| `R1-D-tests.log` / `R1-D-timing.log` / `R1-D-state.log` | 合并测试、耗时与复算分账、生产状态 |
| `screenshots/00–13*.png` | 桌面/证据/原版/历史/空态/冲突/复用/窄屏/预设保存/预设载入/输入过期/版本过期 |
| `demo-run-d8cc9a02.json` + `.manifest.json` | **演示产物**：一份完整冻结结果（含规则、快照、哈希、逐行读数与输入 fact_id），可直接用 `python -c` 打开核对 |

演示产物自检：62,233 字节，`sha256 5499d893c6af715f…`，`counts {pass:1, fail:1, insufficient:0}`，
`rules [{gross_margin ≥ 70.0}]`，`entities [002304.SZ, 600031.SH]`。

## 10. 遗留风险与限制（不掩饰）

1. **真实数据只有 2 家公司**（洋河 2022–2025、三一 2023–2024）。所有"通过/未通过"结论只对这 2 家成立，
   任何界面文案都不得表述为全市场筛选。扩样本属于下一阶段（数据接入），不在 R1 范围。
2. 冲突用例依赖**合成夹具**（`tmp/r1c-conflict/`），已在数据元信息与界面横幅双重标注 `synthetic: true`，
   不进入真实候选、不进入交付与验收证据。
3. 窄屏验证用同源 iframe 模拟 390px（浏览器工具不支持改窗口尺寸）：CSS 断点真实生效，但不是真机；
   Tab 序结论来自"原生元素 + 无负 tabindex + 焦点环规则"，未逐键走查。
4. 生产 `web_ui.py` 磁盘版已含 R1-B 接口，`restart_required: true`：**任何人重启 8080 都会让 R1-B 立即生效
   （而前端仍是旧构建）**。这是当前最需要盯住的部署风险，建议在正式验收前不要重启。
5. 历史结果无保留策略（不可变、不覆盖），长期会累积；清理策略需单独设计。
6. 三个隔离实例仍在后台运行，供复核点击（复核完应关闭，账号在 `tmp/r1c_isolated_instance.py` 内，**不得**用于生产）：
   8081 用真实缓存的**副本** `tmp/r1c-stale/cache.json`（其中三一 2024 营收被 +1，只为演示「输入已变」提示，
   副本与真实 `data/screen_cache/official_disclosure_cache.json` 无关）；8082 用合成冲突夹具。
   真实缓存文件本身未被修改（sha256 仍为 `4dc828ef0bea…`）。
7. 「过期提示」里的旧实现版本样例是把一份结果目录复制后改 `impl_version` 并重算清单哈希得到的**模拟**样本
   （`ac1165f3…`，隔离目录内），用来验证提示分支；不代表真的存在旧版本结果，也不进交付证据。

8. **多用户隔离与既有产品一致，但没有加分仓**：预设与结果都落在实例级持久根（`workspace/screen-presets`、
   `workspace/screen-results`），同一实例内 admin 与 viewer 共用同一份研究台，viewer 只读、写操作仍需 admin。
   没有按用户分仓，也没有暴露成公共 JSON（全部走会话鉴权 + 角色校验）。若未来要多用户/多项目隔离，
   需要把 `user`/`project` 维度加进存储路径与查询条件，并补越权测试（本单未做，属下一阶段范围）。

## 11. 部署与回滚提案（**待批准，本阶段未执行**）

1. 部署窗口：先 `git diff` 复核本报告 §8 清单 → 重建 `frontend/dist` → 受控重启 `launcher.py`。
2. 冒烟：`/api/health`、`/screen` 页面加载、一次预览 + 一次执行（用 2 家真实公司）、查看原版、证据跳转。
3. 回滚：`frontend/dist` 用部署前备份还原；`web_ui.py` 回到 `ed8fd90` 版本；重启一次。
   结果目录 `workspace/screen-results/` 是纯新增产物，回滚时保留不删（避免删掉别人的研究记录）。
4. 验收口径：三种状态仍严格分离；"机器验收"不等于"人工复核"，本次不替用户签署任何复核状态。

## 12. 工作区清洁度（不隐瞒自己动了什么）

- `git status`：受控修改 14 项（含本单 3 个前端/后端文件与证据目录），未跟踪条目 22,135 项 ——
  **其中 17,935 项在 `tmp/`，绝大多数是本次开工前就已存在的历史目录**（`tmp/weavemind_pitch_20261021/` 8,628 项、
  `tmp/weavemind_visual_20261021/` 8,599 项等）；本单自己产生的是 `tmp/r1c*` 共 **32 项、1.9 MB**（隔离实例的 DB/日志/结果/预设）。
- 未使用 `git add` 提交语义：只对 9 个新文件做了 `git add -N`（intent-to-add），使 `git diff` 能输出完整新增内容作为证据；
  **没有 commit / push / 建分支 / 全量暂存**。
- 生产仓库内不存在 `workspace/screen-results` 与 `workspace/screen-presets`（全部落在 `tmp/` 隔离目录）；
  `frontend/dist` 未被重建（`index.html` mtime 仍是 2026-10-22 23:39:35）。
- 真实输入文件 `data/screen_cache/official_disclosure_cache.json` 未被修改（`sha256 4dc828ef0bea…` 与 R1-C 时一致）；
  「输入已变」演示用的是副本 `tmp/r1c-stale/cache.json`。
