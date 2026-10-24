# B1 · 数据接入与普通用户闭环（BETA-1023）

> 订单：`docs/员工任务_选股研究Beta_真实数据与普通用户闭环_20261023.md` §B1
> 基线：HEAD `ed8fd90`（工作区脏，见文末「基线」）；隔离实例 `127.0.0.1:8083`，数据目录 `tmp/r1next-isolated`
> 所有结论以本机真实运行结果为准；**未验的写未验**，不当成通过。

## 0. 结论先行

1. **最小自助研究权限已落地并经真机走查**：新增角色白名单 `config.json → screen.research_roles`（默认空＝只有管理员能写），管理员可在「设置 → 用户」直接创建/改派被开通的角色；前端按 `/api/screen/capabilities` 显示「只读账号：可看、可比、不可执行」并禁用写控件，后端仍逐个 POST 路由硬校验（前端禁用不是唯一防线），被拒的写尝试写审计 `screen.write.denied`。走查 28/28 通过（EXIT=0）。
2. **真机发现并修掉两个「普通用户身份」缺陷**：①登录页把任何非 `viewer` 角色都归一成 `admin`，研究员在界面上显示为「管理员」（比实际权限更大，误导）；②建号接口硬编码只认 `admin`/`viewer`，白名单里的角色根本建不出来。两处都已修，并加了回归用例。
3. **产品内公司/池准备与 CSV 补充说明已可用**：目录（来自所选数据来源）支持按名称/代码搜索、全选/清空、已选计数；上传区新增「字段说明（中文）」折叠面板（17 列逐列解释 + 常见坑 + 「缺字段不等于 0」）。
4. **真实样本 20–50 家：未验（0/35 能取到）**。本机 DNS 把全部外网域名解析到 `198.18.0.0/15`（fake-IP），产品的 SSRF 门 `net_policy.validate_public_url` 因此拒绝出站，适配器被**在发起连接之前**拦下（不是超时、不是解析失败）。按订单「外部缺口要记录并继续」，这里记录原样证据（`logs/b1-sample-attempt.json`），不伪造读数；当前可用真实数据仍是缓存里的 **2 家 / 6 行**（洋河、三一，来自巨潮 PDF），并已用这 6 行在**真浏览器里完成上传→预览→执行**全链路（run `ab77fcd8…`）。
5. 选股筛选本身**零模型调用**：上传、预览、执行、对比都是确定性计算；本轮未产生任何付费调用。

## 1. 要求 → 落地对照

| 订单要求 | 落地 | 状态 |
|---|---|---|
| 最小自助研究权限，且**只在隔离实例**启用 | `screen.research_roles=["researcher"]` 只写进 `tmp/r1next-isolated/config.json`；生产 `config.json` 未改 | ✅ 已验（走查+真机） |
| 权限可配置、拒绝理由可执行 | `_screen_read_only_reason()` 说明「本部署已开通：researcher」并指出改哪里；`GET /api/screen/capabilities` 返回 `can_research/self_service_roles/read_only_reason` | ✅ 已验 |
| 前端不能只是「藏按钮」 | 后端 `do_POST` 对 5 条写路径统一 `_screen_can_research()` 校验 + 审计；只读账号上传/执行/存预设一律 403 `code=screen_read_only` | ✅ 已验（HTTP 走查 28 项） |
| 产品内完成公司/池准备 | 数据来源下拉（官方缓存 / 我上传的数据集）→ 目录勾选 + 手输代码；新增名称/代码搜索、全选/清空、已选 N/M | ✅ 已验（真机） |
| 复用既有适配器 / 来源准入 / 事实链 | 数据入口仍是 `screen_store.save_dataset`（内容定 ID、provenance、文件指纹）；官方缓存路径 `data/screen_cache/official_disclosure_cache.json` 由既有适配器产物喂入，不新造第二条通路 | ✅ 已验（既有契约不变） |
| CSV 作为补充：中文说明 + 示例 | 上传区「字段说明（中文）」17 列逐条；模板仍可下载；示例数据 `sample-6-rows-real-cache.csv`（真实缓存导出，非编造） | ✅ 已验 |
| 真实样本 20–50 家、多行业、非金融，如实报实际数量与缺口 | 35 家候选逐一调用 `adapters.eastmoney.fetch`：**0 成功 / 35 被 SSRF 拦**（fake-IP DNS）；实际可用真实数据 2 家 6 行 | ⚠️ **未验**（环境边界，见 §5） |

## 2. 权限矩阵（后端为准）

| 路由 | 方法 | 未登录 | viewer（只读） | researcher（白名单角色） | admin | 依据 |
|---|---|---|---|---|---|---|
| `/api/screen/capabilities` | GET | 401 | 200（`can_research:false`） | 200（`true`） | 200（`true`） | `_get_screen_capabilities` |
| `/api/screen/datasets`、`/runs`、`/run?id=`、`/compare` | GET | 401 | 200（仅本作用域） | 200（仅本作用域） | 200（管理员作用域） | `_role_allowed_get` |
| `/api/screen/template` | GET | 401 | 200 | 200 | 200 | 同上 |
| `/api/screen/preview`、`/run`、`/datasets`、`/preset`、`/preset/delete` | POST | 401 | **403** `screen_read_only` + 审计 | 200（写进自己的 scope） | 200 | `_SCREEN_WRITE_PATHS` + `_screen_can_research` |
| `/api/screen/run/quarantine` | POST | 401 | 403 | 403 | 200 | 管理面（`_require_admin`） |
| `/api/screen/legacy`、`/api/config`、`/api/users`、`/api/audit` | GET/POST | 401 | 403 | **403** | 200 | `_require_admin` |
| 结果可见性 | — | — | 只读本作用域 | 自己上传/执行的结果；读管理员结果 **404** | 只看管理员作用域（看不到研究员结果） | `_screen_scope(admin)` + 404 不泄露存在性 |

角色来源与开关：

- `_screen_research_roles()` 读 `config.json → screen.research_roles`（缺省 `[]`）；`admin` 恒可写，**不受白名单影响**。
- 用户管理 `POST /api/users`：允许 `admin`、`viewer` 与**白名单内角色**；其它字符串一律 400，错误里直说「自助研究角色需先在 config.json → screen.research_roles 开通」。
- 前端 `Login.tsx` 原样保存服务端返回的 `role`（修掉「非 viewer 视为 admin」）；`settingsText.roleLabel` 增加 `研究员（自助研究）`；设置页的用户下拉按 `capabilities.self_service_roles` 动态出现，不写死枚举。

## 3. 实际改动（本轮）

| 文件 | 改动 |
|---|---|
| `web_ui.py:10190` | `_SCREEN_WRITE_PATHS`（5 条写路径）、`_screen_research_roles()`、`_screen_can_research()`、`_screen_read_only_reason()` |
| `web_ui.py:4209` | `do_POST` 对筛选写路径先做角色校验；拒绝时 `_audit("screen.write.denied", result="fail")` 并返回 403 `{code:"screen_read_only"}` |
| `web_ui.py:10520` | `GET /api/screen/capabilities`：`{status, role, can_research, scope, self_service_roles, read_only_reason, note}` |
| `web_ui.py:8700` | 建号/改派角色：`admin|viewer|白名单角色`，其它给可执行错误 |
| `frontend/src/pages/StockScreen.tsx:219,840,884` | `CSV_FIELDS` 17 列中文说明面板；目录搜索 + 全选/清空 + 已选计数；`readOnly` 横幅与写控件禁用（含 `title` 说明） |
| `frontend/src/components/Login.tsx:54` | `role: String(d.role || 'viewer')`（原：非 viewer→admin） |
| `frontend/src/lib/settingsText.ts` | `roleLabel` 增加 `researcher → 研究员（自助研究）` |
| `frontend/src/pages/Settings.tsx:524,576,602` | 用户管理按 capabilities 动态列出可开通角色（新建与改派两处） |
| `test_screen_api.py` | +7 用例（能力分角色、研究员本作用域可写、研究员进不了管理面、只读写被拒且理由可执行、白名单缺省只读、建号角色白名单、未知角色拒绝） |

## 4. 验证（真实命令与真实退出码）

| 验证 | 命令 | 结果 |
|---|---|---|
| HTTP 权限走查（28 项：角色/作用域/404/审计/只读可读） | `C:/Python314/python.exe tmp/b1_walk.py` | `28/28 PASS`，**EXIT=0**（`logs/b1-permission-walk.log`） |
| API 回归 | `python -m unittest test_screen_api` | `Ran 41 tests ... OK`，**EXIT=0** |
| 前端类型检查/构建 | `npx tsc -b` / `npx vite build --outDir dist-screen-check` | `TSC_EXIT=0`、`✓ built`，**EXIT=0**（`logs/b1-build.log`） |
| 真机：只读账号 | 浏览器登录 `viewer` | 横幅「只读账号：可看、可比、不可执行」+ 理由；上传/预览/执行/预设全禁用（`03-b1-viewer-readonly.png`） |
| 真机：研究员 | 浏览器登录 `researcher` | 无只读横幅；上传/预览/执行可用；页头角色显示「研究员（自助研究）」（`04-b1-researcher-upload-run.png`） |
| 真机：上传→预览→执行 | 6 行真实缓存 CSV → `b1-researcher-sample.csv` | 数据集「6 行｜用户提供（未独立核验）｜文件指纹 a18408ef…」；预览 44 条观察、2022–2025；执行 run `ab77fcd85e3c34cc04469cb96f9a17ad`，洋河 2025 毛利率 71.60%、资产负债率 19.97%，结论「通过」 |
| 跨作用域隔离 | 同上 | 管理员列表看不到研究员结果；管理员读该 run 得 404；研究员读管理员 run 得 404 |
| 只读写尝试审计 | `viewer` 上传 → 403 | `logs/audit.jsonl` 出现 `screen.write.denied`（走查第 24 项断言） |

浏览器上传的诚实说明：本机 Kimi 扩展默认关闭「允许访问文件网址」，系统选文件框无法由代理打开。本轮把同一份 6 行真实 CSV 通过页面内 `File`/`DataTransfer` 注入文件输入，**仍由页面自身代码读取 File → POST → 落库 → 执行**（不是绕过前端直接打接口）；原生选文件框这一环**未验**，需要用户在 `chrome://extensions → Kimi → 详细信息 → 允许访问文件网址` 打开后补验（B2 会再走一次）。

## 5. 真实样本尝试：35 家 → 0 家可取（未验，环境边界）

- 候选：35 家 A 股非金融、多行业（白酒/乳制品/调味品/家电/电池/光伏/汽车/医药/机械/建材/钢铁/有色/煤炭/石化/化工/通信/电子/免税/建筑/地产/航空/计算机），见 `tmp/b1_sample_attempt.py:UNIVERSE`。
- 逐家调用产品自带 `adapters.eastmoney.fetch(name, code, year_range=(2023,2025))`。
- 结果：`{"attempted": 35, "succeeded": 0}`，**EXIT=2**（脚本按「有失败即 2」返回，不吞错）。错误原文一致：
  `blocked URL by SSRF guard: https://datacenter-web.eastmoney.com/api/data/v1/get?...`
- 根因（可复核）：`socket.gethostbyname("datacenter-web.eastmoney.com") → 198.18.0.15`、`www.cninfo.com.cn → 198.18.0.12`；`net_policy.validate_public_url` 判定「解析到非公网地址」→ 拒绝。这是**本机网络策略**，不属于本订单可改范围（不改网络/凭据/安全策略），因此不绕。
- 现有真实数据（可直接用的）：`data/screen_cache/official_disclosure_cache.json`，`synthetic:false`，**6 行 / 2 家**：
  | 公司 | 代码 | 期间 | 来源（巨潮 PDF） | 可用时点 |
  |---|---|---|---|---|
  | 洋河股份 | 002304.SZ | 2022–2025 | `static.cninfo.com.cn/finalpage/2024-04-27/1219873234.PDF` 等 | 2024-04-27 / 2025-04-29 / 2026-04-28 |
  | 三一重工 | 600031.SH | 2023–2024 | 巨潮年报 PDF（缓存内 `source_url`） | 2025-04-18 |
- 结论：**「20–50 家真实样本」未验**。要补齐只有三条路（都需要环境/人）：①在能正常解析公网的机器上跑同一脚本；②由用户按 §6 的 CSV 说明导入自己的数据；③管理员把官方缓存文件换成含更多公司的版本（形状不变）。三条都不改产品代码。

## 6. CSV 补充（普通用户自助路径）

产品内「字段说明（中文）」逐列解释（节选，全文见界面）：

| 列 | 必填 | 含义 | 常见坑 |
|---|---|---|---|
| `entity` | ✅ | 公司名（显示用） | 同名不算错，但 `code` 要能唯一对应主体 |
| `code` | ✅ | A 股代码带后缀 | `600519.SH` / `000858.SZ`；不认识的代码计「资料不足」，不编造 |
| `period` | ✅ | 报告期，日期 | 写「2024年」会被拒收（不猜哪一期） |
| `available_at` | 可选 | 最早能拿到的日期 | 留空＝待核验，不参与「资料截止日」比较 |
| `unit`/`caliber` | 可选 | 金额单位/报表口径 | 只认「元」与「合并/母公司」，不自动换算 |
| `revenue`…`operating_cashflow` | 可选 | 7 个金额字段（元） | 缺字段≠0：算不出就记「资料不足」并说明缺哪几项 |

示例：`sample-6-rows-real-cache.csv`（由真实缓存导出，含 `source_url`/`available_at`/`disclosed_at`，可直接上传复现 §4 的 run）。

## 7. 反例与已修缺陷

| # | 现象 | 影响 | 处理 |
|---|---|---|---|
| 1 | 登录页 `d.role === 'viewer' ? 'viewer' : 'admin'` | 研究员被显示成「管理员」，用户以为自己权限更大 | 改为原样保存 `role`；真机复验显示「研究员（自助研究）」 |
| 2 | `POST /api/users` 只认 `admin`/`viewer` | 白名单角色建不出来，只能手改 `config.json` | 允许白名单角色；未知角色 400 且提示去哪开通 |
| 3 | 系统选文件框打不开（扩展权限） | 原生上传环未验 | 记录；用页面内注入 File 完成产品侧全链路；B2 请用户开权限后补验 |
| 4 | 同一字段两条规则时，结论文案重复「毛利率 71.60%；毛利率 71.60%」 | 可读性差（不误导数值） | 记入 B2 待办：解释文案带阈值（如「毛利率 ≥70%」） |

## 8. 未验 / 边界

- **20–50 家真实样本**：0/35 可取，环境网络策略所致（§5），未验。
- 原生文件选择框上传：未验（扩展权限），产品侧 File 读取→POST 已验。
- 生产 8080 未动：本轮只在隔离实例 8083 验证；`frontend/dist` 未重建（仍是旧构建），生产无部署。
- 未改真实账号权限：三个测试账号只存在于 `tmp/r1next-isolated/config.json`（虚构密码）。
- 建号白名单语义只在隔离实例验证；生产 `config.json` 无 `screen.research_roles`，行为与 B0 一致（只有 admin 能写）。

## 9. 基线

- HEAD `ed8fd90`；本轮改动文件：`web_ui.py`、`frontend/src/pages/StockScreen.tsx`、`frontend/src/components/Login.tsx`、`frontend/src/lib/settingsText.ts`、`frontend/src/pages/Settings.tsx`、`frontend/src/auth.ts`（上一轮）、`frontend/src/components/AppLayout.tsx`（上一轮）、`test_screen_api.py`。
- 与本订单无关的既有脏文件未触碰（如 `docs/evidence/20261012-c5-review/README.md`、`evals/cases/auto_grown.json`、`frontend/src/App.tsx`）。
