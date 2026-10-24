# D 阶段 · 用户验收（真实浏览器走查 + 隔离 API 验证）

- 任务：`DATA-RC2-1024` / D 段（用户验收证据）
- 执行者：d-acceptance
- 日期：2026-10-24 12:50–13:16
- 结论口径：**「通过」= 我实际观察到该行为并留下证据；「失败」= 实际观察到与要求不符；「未验」= 没观察到，写明原因与复现步骤**。没有一条「通过」是靠推断得出的。
- 数据性质：全部为**合成演示数据 / 用户提供的演练数据**，不是真实上市公司数据，不得当作投资候选。
- 本报告不含任何口令；实例口令随机生成，只落在 `tmp/d-8087/d_env.sh` 与该进程的环境变量里。该文件与 `tokens.json` 已用 `icacls` 移除继承、只授权当前 Windows 用户（`<本机用户>:(F)`）；MSYS `stat` 仍显示 644（Windows 上 POSIX 位不可靠，以 ACL 为准）。

---

## 0. 结论摘要

| # | 验收项目 | 渠道 | 结果 | 证据 |
|---|---|---|---|---|
| 1 | 主流程：登录 → 选数据/材料 → 设条件 → 执行 → 结果+证据 → 保存/重开 → 更新比较 | 真实浏览器（8087） | **通过** | `screens/d-01…d-05`、`d-07`、`d-10` |
| 2a | 原生文件选择器上传（点「上传 CSV 数据」→ 系统选择文件） | 真实浏览器 | **未验**（环境受限） | 扩展报错原文见 §3.2 |
| 2b | 上传落盘后成为可用数据集（并出现在界面里） | 隔离 API + 浏览器只读复核 | **通过** | `d-11`、`logs/d-api-upload.log` |
| 3 | 「下载模板（CSV）」浏览器下载落盘 | 真实浏览器 | **未验**（点了有提示、磁盘无文件） | `logs/d-api-template.log`（接口 200） |
| 4 | 跨账号规则模板导出/导入（A→B 规则一致） | 隔离 API | **通过** | `logs/d-api-rules-template.log` |
| 5 | 分页：第 2 页是否真有数据 | 真实浏览器 | **通过** | `d-08`、`d-09` |
| 6a | 390/420px 窄屏：真实改变浏览器视口 | 真实浏览器 | **未验**（`window.resizeTo` 改不到 innerWidth） | §3.6 |
| 6b | 390px 视口下渲染与横向溢出 | 真实浏览器（390px iframe 视口） | **通过**（标注非设备模拟） | `d-14` |
| 6c | 键盘 Tab 焦点顺序走查 | 真实浏览器 | **未验**（无按键注入能力），附 DOM 顺序枚举 | §3.6 |
| 7 | 未保存内容：切换来源 / 刷新后是否保留 | 真实浏览器 | **通过**（行为已记录，见备注） | `d-13`、`logs/d-browser-notes.log` |
| 8a | 双标签冲突：旧版本写入 → 409 `version_conflict` | 隔离 API | **通过** | `logs/d-api-watchlist-conflict.log` |
| 8b | 双标签冲突：浏览器界面里能否看到 409 | 真实浏览器 + 源码核对 | **失败**（前端不发送 `if_version`，实际是后写覆盖） | §3.8 |
| 9a | 取消/超时：是否留下半截数据集 | 隔离 API（RST/短超时） | **通过**（无半截：要么完整 50/50，要么没有） | `logs/d-api-cancel-timeout.log` |
| 9b | 取数中途取消（适配器出站请求被中断） | 隔离 API | **未验**（出站被策略拒绝，无法造出慢请求） | 同上 |
| 10 | 来源响应乱序：旧响应不得覆盖新状态 | 隔离 API + 浏览器（注入延迟的测试夹具） | **通过** | `logs/d-api-out-of-order.log`、`d-12` |

**计数：通过 9 项 / 失败 1 项 / 未验 5 项（含 9b、6a、6c、2a、3）。**

---

## 1. 环境与实例（可复现）

| 项 | 值 |
|---|---|
| 实例 | 自有隔离实例，**只用 127.0.0.1:8087**（收尾时已停，见 §5） |
| 启动方式 | `python tmp/d_instance_8087.py`（环境变量注入端口/数据目录；不写仓库默认配置） |
| 数据目录 | `tmp/d-8087/`（会话库、screen 作用域数据、config.json 均在此，未触碰其它实例目录） |
| 进程 | pid 2252，启动于 2026-10-24 12:50:53，Python 3.14.3（`/api/health` 自报） |
| 已加载源码指纹 | `loaded_sha=5000acc909d8`；`/api/health` 同时报 `disk_sha=37c5593b3fd4`、`changed=["web_ui.py"]`、`restart_required=true` → **本次走查跑的是 12:50 加载的代码，不是磁盘最新代码**（Lead 之后继续改了 A/B 源码）。所有证据对应 12:50 加载版本。 |
| 前端构建 | `frontend/dist`，`index.html` mtime 2026-10-24 13:07:22；`/api/health` 报 `frontend.state=built`、`loaded_index_sha=disk_index_sha=8c886724f29a`、`assets=33`、`changed=false`。即：13:07 的重新构建与进程已加载的前端**同一指纹**，构建名/字节数未变（`index-BCnMfQ5q.js` 214,981 B、`index-SyayeYPe.css` 47,734 B）。 |
| 账号 | `dadmin`（admin）、`dresearcher`（researcher，`can_research:true`，浏览器走查主账号）、`dviewer`（viewer，`can_research:false`）。三个口令均为随机生成，只存在于 `tmp/d-8087/d_env.sh`（ACL 仅当前用户）与进程环境变量，**未写入本报告、截图、日志**。 |
| 数据 | `tmp/c2_synthetic_50.json`：50 家 `900001.SZ…900050.SZ` × 2 期（2023-12-31 / 2024-12-31），`metadata.synthetic=true`、`official=false`、`source_url=about:synthetic-demo`。另有演练用「用户提供」CSV：`880001.SZ/880002.SZ` 两个**虚构主体**，`source_url=about:d-user-provided`。 |
| 浏览器 | 用户真实浏览器（Edge，扩展控制），任务会话名 `rc2-d`，窗口 CSS 视口 1888×987、dpr 1.35（截图 2549×1332 物理像素）。 |

### 渠道口径（务必区分）

- **真实浏览器走查**：`webbridge_*` 驱动用户真实浏览器，页面 = `http://127.0.0.1:8087/screen`，截图存 `docs/evidence/20261024-data-rc2/screens/`。
- **隔离 API 验证**：`urllib` 直接打 `http://127.0.0.1:8087/api/...`，脚本 `tmp/d-8087/d_api_probe.py`、`tmp/d-8087/d_cancel_rst.py`，输出存 `logs/d-api-*.log`。
- **两者不可互相顶替**：例：模板下载「接口 200」不等于「浏览器下载成功」；上传「API 能落盘」不等于「原生选择器可用」。下面每行都标了渠道。

---

## 2. 证据清单

截图（`docs/evidence/20261024-data-rc2/screens/`）：

| 文件 | 内容 |
|---|---|
| `d-01-login-page.png` | 登录页（用户名 `admin` 预填、「首次使用？创建初始管理员」） |
| `d-02-screen-source-and-prep.png` | /screen 首屏：数据来源=官方披露缓存（100 行）、下载模板/上传 CSV、合成演示横幅、50 家勾选列表（已选 0/50） |
| `d-03-prep-coverage-preview.png` | 数据准备（材料摘要 / 取数来源 / 期间 / 预览覆盖与缺口 / 先试一家 / 整批取数并生成数据集）+ 覆盖表（2023-12-31「部分可算 revenue_growth」、2024-12-31「五字段可全算」） |
| `d-04-run-result-counts.png` | 执行结果：通过 19 / 未通过 31 / 资料不足 0 / 合计 50，逐行读数+算式，底稿哈希，「未独立核验，不自动升级为官方证据」横幅 |
| `d-05-evidence-panel.png` | 证据面板：绑定本次冻结结果（run_id、结果哈希），输入科目「期间/数值/口径/可用时点/核对」、来源文档 `about:synthetic-demo` |
| `d-06-pagination-page2.png` | 12:54 那份结果的第 2 页（首轮点击后未通过行 20 → 31，末行 900047/900048） |
| `d-07-run-history.png` | 历史结果（原版只读）表 + 人工复筛对比选择器 |
| `d-08-pagination-page1-control.png` | 第 1 页末尾「显示更多（还剩 11 家）」+ 冻结页脚（run_id / 结果哈希 / 快照） |
| `d-09-pagination-page2-rows.png` | 点开后第 2 页行（900045/46/47/48…），无「显示更多」 |
| `d-10-update-compare.png` | 更新比较：2 份冻结结果，「归因：不可直接归因」、规则差异逐条、数据来源差异、不再通过 10 家、逐家进出 |
| `d-11-uploaded-dataset-in-ui.png` | API 上传的数据集出现在界面：来源下拉「我上传：D 演练：用户提供样例（2 家）（4 行）」、表头「用户提供（未独立核验）| 文件指纹 f4b1499a6aac…」、勾选列表 880001.SZ/880002.SZ |
| `d-12-stale-response-guard.png` | 旧响应迟到后界面仍为最后选择的官方缓存（50 家） |
| `d-13-unsaved-reset-after-reload.png` | 刷新后条件回到默认（资产负债率 60） |
| `d-14-narrow-390.png` | 390 CSS px 视口渲染：移动端底部导航、控件竖向堆叠、无横向溢出 |

日志（`docs/evidence/20261024-data-rc2/logs/`）：`d-instance-8087.log`（实例 stdout/stderr）、`d-first-boot.log`（建号+三次登录 200）、`d-api-template.log`、`d-api-upload.log`、`d-api-rules-template.log`、`d-api-watchlist-conflict.log`、`d-api-cancel-timeout.log`、`d-api-out-of-order.log`、`d-api-env-build.log`、`d-browser-notes.log`（浏览器侧观测的原始返回，逐条带时间）。

---

## 3. 逐项详情

### 3.1 主流程（真实浏览器）— 通过

路径与观察（全部在 `dresearcher` 下）：

1. **登录**：`/` 登录页 → 提交后进入工作台（`POST /api/login` 200）；浏览器地址栏与 Cookie 会话正常。
2. **选数据/材料**：数据来源下拉选中「官方披露缓存（服务端配置）（100 行）」，展开 **数据准备**（`<details>`）：
   - 材料摘要：`官方披露缓存：100 行 / 50 家；…我的数据集 0 份；研究清单 0 只`；
   - 期间 `2023,2024`；「预览覆盖与缺口」→ 表格给出每期可算/部分可算字段与缺口；
   - 「整批取数并生成数据集」→ `POST /api/screen/prepare/collect` 200，返回 dataset `3bcccc8f…`（100 行 / 50 家 / `reused:false` / 网络计数全 0）。
3. **设条件**：3 条规则（营收同比增速 ≥ 0、毛利率 ≥ 20、资产负债率 ≤ 60），顶部显示「数据来源：… | 文件指纹 … | 核验：人工复核未开始」。
4. **执行**：`POST /api/screen/run` 200 → `run_id 761ad4f103c27c07c5ae00590a3209bd`、`result_hash 3b275581cbe1…`、快照 `013ed0ed212e…`；计数 通过 19 / 未通过 31 / 资料不足 0 / 合计 50。
5. **结果 + 证据**：逐行读数带算式（例：营收增速 6.43%、毛利率 81.97%、资产负债率 57.96%），展开证据面板 → 绑定本次冻结结果（run_id + 结果哈希）、逐科目「期间/数值/口径/可用时点/核对」、来源文档 `about:synthetic-demo`；横幅明确「未独立核验，不自动升级为官方证据」。
6. **保存/重开**：「历史结果（原版只读）」列出 1 份（`761ad4f103c2…`、19/31/0、来源「合成演示」）；点「打开原版」→ 页面回到同一份结果，`run_id`/结果哈希/计数**完全一致**（19/31/0/50）。
7. **更新比较**：把资产负债率改成 ≤ 30 再执行 → 第二份冻结结果 `d32ba267c94df0403565da7e5ae3cece`（9/41/0/50）；历史列表变 2 份；选择基准=旧、对照=新 → 「对比这两份」给出：
   - 「归因：不可直接归因」，逐条列出差异原因（筛选规则变了、数据来源不同）；
   - 规则差异原文：`营业收入同比增速 >= 0%；毛利率 >= 20%；资产负债率 <= 60%` → `…资产负债率 <= 30%`；
   - 结论：新通过 0 / 不再通过 10 / 读数有变化 0，逐家进出（例：合成演示公司01 900001.SZ 通过 → 未通过）。

关键 HTTP 码：`/api/login` 200、`/api/screen/pool` 200、`/api/screen/fields` 200、`/api/screen/prepare/materials` 200、`/api/screen/prepare/plan` 200、`/api/screen/prepare/collect` 200、`/api/screen/run` 200、`/api/screen/runs` 200、`/api/screen/compare` 200、`/api/screen/datasets` 200。

**观察（不改代码，交 Lead 判定）**：`run` 的「数据来源」在两份结果里不同（第二份用的是 `c2_synthetic_50` 官方缓存，第一份用的是物化后的数据集 `3bcccc8f…`），因此比较面板同时报「筛选规则不同」「数据来源不同」。这是**如实呈现**（好事），但也说明：刷新页面后数据来源会回到默认缓存，用户若不注意会以不同来源重跑——是否要在「用当前数据重新筛选」时锁住原来源，属产品决策。

### 3.2 原生文件选择器 / 上传落盘 — 2a 未验（环境），2b 通过（API）

- 页面结构：`上传 CSV 数据` 是包着 `<input type="file" accept=".csv,text/csv" class="hidden">` 的 `<label>`，点击即触发**系统原生文件选择框**；同一区域还有 `accept="application/json,.json"` 的隐藏输入（规则模板导入）。
- 我**没有**去点开那个系统对话框（会在用户屏幕上弹出模态框且我无按键能力关闭，可能阻塞后续走查），改用扩展的文件注入通道尝试，拿到**扩展报错原文**：

  ```
  extension_error: upload needs Chrome's per-extension file access, which is off by default.
  Ask the user to open chrome://extensions, find Kimi, open Details, and turn on
  "Allow access to file URLs", then retry. The extension cannot turn this on for them.
  ```

  → 原生选择器这一条在本环境**无法完成**，记 **未验**；不绕过、也不要求用户去扩权。

- 上传能力本身（2b）用隔离 API 验证并回浏览器复核：`POST /api/screen/datasets`（CSV 3340 B）→ **200**，`dataset_id=f4b1499a6aac92ffa2ba761a94d34a9b`、4 行、`codes=[880001.SZ, 880002.SZ]`、`official=false`、`user_provided=true`、`note="用户上传数据集：只在本作用域可见；不是官方披露缓存，不能用于宣称官方数据"`；**相同内容再传一次返回 `reused:true`（内容定址去重）**。
- 浏览器官网复核（`d-11`）：刷新后来源下拉出现「我上传：D 演练：用户提供样例（2 家）（4 行）」，表头显示「用户提供（未独立核验）| 文件指纹 f4b1499a6aac… | 核验：人工复核未开始」，勾选列表出现 `D演练甲公司 880001.SZ`、`D演练乙公司 880002.SZ`（已选 0/2）。

**观察**：来源下拉把「由官方缓存物化出来的数据集」也显示为「**我上传**：官方缓存物化 …」，而它们的 `provenance` 是 `official_cache_materialized`、`user_provided=false`。同一份数据在表头写作「用户数据集」，在下拉里写作「我上传」，两处措辞与真实来源身份不一致，容易被读成「用户上传的」。建议统一措辞（不影响数值与核验状态）。

### 3.3 模板下载 — 未验（点了有提示，磁盘没文件）

- 浏览器点「下载模板（CSV）」：页面提示

  ```
  模板已下载（CSV，Excel 可直接打开）：请删掉两行示例后填入自己的数据，再上传
  ```

  但磁盘核对：`C:\Users\ding0\Downloads` 点击前后均为 **15 项**、目录 mtime 未变（10:18:35），无 `weavemind-screen-template.csv`，也无 `.crdownload` 残留 → **无法确认落盘**，记 **未验**（受扩展/浏览器下载限制，未做任何绕过）。
- 同一入口的接口侧（隔离 API）：`GET /api/screen/template` → **200**、`Content-Type: text/csv; charset=utf-8`、`Content-Disposition: attachment; filename="weavemind-screen-template.csv"`、789 B、3 行 25 列（前两行是显式标注「示例行，请删除」的示例）。
- 结论写法：**接口正常（200/attachment/内容正确）≠ 浏览器下载成功**；本项在真实浏览器里未验。

### 3.4 跨账号规则模板 — 通过（API 渠道）

- A 账号（`dresearcher`）`POST /api/screen/rules/export` → **200**，模板 `schema=weavemind.stock_screen.rules/1`、469 B（紧凑），顶层键只有 `schema/created_at/impl_version/field_map_version/period_policy/rules/notes/disclaimer`——**不含股票池、备注、结果、账号信息**。
- B 账号（`dadmin`）`POST /api/screen/rules/import` → **200**，`preview.rules` 与 A 的 3 条规则**逐条一致**（`revenue_growth >= 0`、`gross_margin >= 20`、`debt_ratio <= 45`，field/op/value 全同），`period_policy` 同、`rules_count=3`；返回明确写着「只做校验与预览：没有执行筛选、没有写入任何结果」。
- 反向验证：把 `watchlist`、`runs` 塞进模板再导入 → **400**「模板含私人或不可移植内容（watchlist）：拒绝导入」。
- 权限：`dviewer` 导出 → **403** `screen_read_only`（「当前角色「viewer」不在自助研究白名单（本部署已开通：researcher）内」）。
- 浏览器渠道未覆盖的部分：导出走 Blob 下载、导入走原生文件选择器，**两者都撞上 §3.2/§3.3 的同一环境限制**，所以浏览器侧未验（不是接口问题）。

### 3.5 分页 — 通过（第 2 页确实有数据）

- 50 家结果里「未通过」组共 31 家，首屏渲染 20 行，末尾出现 **「显示更多（还剩 11 家）」**（`d-08`）。
- 点击后：未通过行 **20 → 31**，最末两行为 `900047.SZ`、`900048.SZ`，按钮消失（`d-09`）；随后是「资料不足 0 家」与冻结页脚（`run_id 761ad4f103c2…` / 结果哈希 / 快照）。
- 与上一批次的差别：上一批只证明了「50 家在一页里」，本批是**真的把第 2 页点开并截了图**（页内分页控件、剩余数量、第 2 页行都在同一份证据里）。

### 3.6 窄屏 390/420px + 键盘焦点 — 6a 未验、6b 通过、6c 未验

- **6a 真实改视口：未验。** 在真实浏览器里 `window.resizeTo(390, 900)` 之后：`window.outerWidth/Height` 一度变为 390×900，但 `window.innerWidth/Height` 仍为 **1888×987**，`matchMedia('(max-width:480px)').matches === false`；随后 outer 又回到 1707×1019。**结论：本环境无法用脚本把真实视口改成 390/420 px**，如实记未验（未用设备模拟/远程调试端口之类手段绕过）。
- **6b 390 px 渲染：通过（标注：在真实浏览器内用 390 px 宽的 iframe 视口加载同源 `/screen`，不是设备模拟，无触摸与 dpr 变化）。** 实测 `contentWindow.innerWidth=387`（扣滚动条），`documentElement.scrollWidth==clientWidth`（**无横向溢出**），布局切换为移动端：出现底部导航「研究工作台/可解释选股/研究档案/研究试验/设置/更多」，操作按钮（下载模板、上传 CSV 数据）改为整行堆叠（`d-14`）；420 px 下同样无横向溢出。
- **6c 键盘 Tab 顺序：未验。** 本会话没有按键注入能力（无法真实按 Tab）。替代证据为**程序化按 DOM 顺序枚举可聚焦元素**（117 个，非真实按键顺序）：`跳到主要内容` → `织光 WeaveMind` → `研究工作台` → `可解释选股` → `研究档案` → `研究试验` → `系统管理` → `设置` → `运行选项` → …→ `服务在线` → `退出登录` → `数据来源 select` → `下载模板（CSV）` → 文件输入 → `字段说明` …；含 1 个 `tabindex="-1"`。
  **顺带发现（可访问性）**：顶栏两个图标按钮读作 `button:(无文本)`，上传用的 file input 也无可读名字（无 `aria-label`）——键盘能到，但读屏/键盘用户无法分辨。

### 3.7 未保存内容 — 通过（行为已记录）

- 改规则不改提交：把资产负债率改成 **55**（未执行）→ 切换数据来源到「我上传：…（4 行）」→ 再切回官方缓存，规则输入仍为 **`0 / 20 / 55`**（**切换来源后未保存内容保留**）。
- 直接**刷新页面**：规则回到默认 **`0 / 20 / 60`**，来源选择清空，期间口径回 `uniform`（`d-13`）。前端 `StockScreen.tsx` 里 **没有任何 localStorage/sessionStorage 草稿**（grep 0 命中），所以刷新即丢，且刷新前**没有任何「未保存」提示**。
- 结论：行为已确认（不是猜测）。是否要给「未保存条件」加提示或草稿，属产品决策，D 段不改代码。

### 3.8 双标签冲突 — 8a 通过（API），8b 失败（界面侧无 409）

- **8a（API 渠道，通过）**：同一账号两个会话，对同一条清单项写备注：
  - 会话 A 带 `if_version=1` → **200**，该项 version 变 2；
  - 会话 B 仍带 `if_version=1`（旧版本）→ **409**，`code="version_conflict"`，错误原文「880001.SZ 的清单内容已被另一个页面修改（版本 1 → 2）：本次没有写入，请刷新后重试」，并附 `current{code,note,updated_at,version:2,rev}`；
  - 冲突后实际内容仍是 A 写的「标签A先写」（version 2）→ **没有悄悄覆盖**。
- **8b（浏览器界面，失败）**：`frontend/src/pages/StockScreen.tsx` 的 `saveWatchNote` 请求体只有 `{action,code,note}`，**全前端 grep `if_version`/`if_updated_at` 为 0 命中**；等价请求（不带 `if_version`）实测 **200 并覆盖**（note 变成后写的内容，version 3）。也就是说：**服务端能力具备、浏览器两个标签同时改备注时界面仍然是「后写覆盖、无冲突提示」**，用户看不到 409。这是本批次唯一判定为「失败」的项（不是后端缺陷，是前端未接入乐观并发）。

### 3.9 取消 / 超时 — 9a 通过，9b 未验

- **9a「不产生半截数据集」（通过）**：
  - 客户端 50 ms 断开一次 50 家取数（官方缓存 × 2023）：服务端 20 ms 就完成，落库 dataset `13eda394…`，**50 行 / 50 家 / 期间 2023-12-31**（完整）。
  - 更强的一次：发出请求后 **10 ms 用 RST 断开**（`SO_LINGER(1,0)`，模拟关标签/切页），客户端完全不读响应 → 服务端仍完成，落库 `5f1d6513…`，**行数==50、公司数 50、期间只有 2024-12-31**；之后 `GET /api/screen/pool` 仍 200。→ 观察到的都是「要么完整、要么没有」，**没有半截数据集**（写路径是内容定址目录 + 原子重命名，`screen_store.save_dataset_from_source`）。
  - **但要如实说清语义**：客户端断开**不等于**服务端取消。服务端会继续跑完并落库，成本也照记（`prepare_requests` 从 3 增到 4）。用户视角「我取消了」与系统视角「已经生成了一份数据集」可能不一致——建议产品明确文案或加服务端取消。
- **9b「取数中途取消」（未验）**：`adapter:eastmoney` 在这台机器上被出站策略直接拒绝（`status:"blocked"`、`companies[0].status="denied_by_policy"`、`counters: adapter_calls=1, network_denied=1, network_requests=0`，见 §3.9 日志），**造不出一个能持续数秒的出站请求**，因此「在适配器取数中途取消/超时」无法验证；也没有为了造慢请求去连外网。

### 3.10 来源响应乱序 — 通过

- 服务端一致性（隔离 API，两请求并发，总耗时 17 ms）：
  - `GET /api/screen/pool` → 来源 `c2_synthetic_50`（100 行、`synthetic=true`）、50 家公司（900001.SZ…）；
  - `GET /api/screen/pool?dataset=f4b1499a…` → 来源「用户数据集：D 演练：用户提供样例（2 家）」（sha256 `f4b1499a…`）、2 家公司（880001.SZ/880002.SZ）。
  - 两次响应**各带自己的来源与数据**，没有互相串台。
- 期间口径同样不串：并发两次 `POST /api/screen/prepare/plan`（2023 与 2024），各自返回自己的期间：`2023-12-31 → 状态 partial`、`2024-12-31 → 状态 covered`（都标明 `via="我的数据集"` 与 `dataset_id`）。
- 客户端序保护（浏览器，**注入延迟的测试夹具**）：在页面里给 `fetch` 加一层「只延迟 `/api/screen/pool?dataset=` 1500 ms」的包装，然后先选「数据集来源」（慢）、30 ms 后立刻切到「官方缓存」（快）。网络面板确认发生两次 pool 请求（`pool` 与 `pool?dataset=f4b1…`，均 200），**旧的数据集响应在 1.5 s 后才回来**；最终界面仍是最后选择的官方缓存（`c2_synthetic_50`、50 家、勾选列表 50 个 9xxxxx 代码），`880001.SZ` 只出现在研究清单里、没有污染股票池（`d-12`）。夹具已在使用后移除（`window.fetch` 复原）。
- 口径声明：这条的「乱序」是**我用注入延迟制造**的，不是自然网络抖动；结论是「旧响应迟到时界面保持最后选择」，与源码中 `poolSeq` 序保护一致。

---

## 4. 未验项汇总（原因 + 复现步骤）

| 项 | 原因 | 复现步骤（在能扩权/有按键注入的环境里） |
|---|---|---|
| 2a 原生文件选择器上传 | Chrome 扩展未开「允许访问文件网址」，扩展拒绝注入文件（报错原文见 §3.2）；未绕过、未要求用户扩权 | 打开 `chrome://extensions` → Kimi → 详情 → 打开「允许访问文件 URL」→ 回 `/screen` 点「上传 CSV 数据」→ 选 `tmp/d-8087/d_upload_user_provided.csv` → 期望出现「（4 行）」数据集并进入勾选列表 |
| 3 模板下载落盘 | 点「下载模板（CSV）」页面提示已下载，但 `Downloads` 目录与新文件均无变化 | 同上扩权后点一次，检查 `C:\Users\ding0\Downloads\weavemind-screen-template.csv` 是否存在且为 3 行 25 列 |
| 6a 真实改视口 390/420 | `window.resizeTo` 只改到 `outerWidth`，`innerWidth` 仍 1888，`matchMedia(max-width:480px)` 为 false | 用 DevTools 设备模拟或真实窄窗口把视口设成 390/420 → 复核底部导航、无横向溢出、按钮堆叠（我的 iframe 证据 `d-14` 可作为预期对照） |
| 6c 键盘 Tab 走查 | 本会话无按键注入能力 | 在 390 px 与桌面两种视口下按 Tab 走一遍：起点应是「跳到主要内容」，顺序覆盖侧栏 → 顶栏（**注意两个图标按钮与文件输入无可读名字**）→ 数据来源 → 下载模板/上传 → 字段说明 → … → 执行筛选 → 结果行（`aria-expanded` 可展开证据）→ 历史结果 → 对比 |
| 9b 适配器取数中途取消 | 出站被策略拒绝（`network_denied=1`），无法造出持续数秒的请求 | 在允许出站的环境里，对 `adapter:eastmoney` 发起 ≥3 家取数，1 s 时关标签/断网 → 复核：无半截数据集、成本记账与实际请求一致 |

---

## 5. 实例收尾

- 12:50:53 起在 127.0.0.1:8087 起的自有实例已**停止**（后台作业已取消，进程已退出）。
- 端口复核：`127.0.0.1:8087` 无 `LISTENING`（`netstat` 只剩历史 `TIME_WAIT` 条目）；新建连接失败、`GET /api/health` 不再响应。
- 未触碰 8080/8085/8086/8089/8090/8091；未改任何产品源码、未提交、未 push；写入范围仅 `tmp/d-*` 与 `docs/evidence/20261024-data-rc2/`（`D-user-acceptance.md`、`screens/d-*.png`、`logs/d-*.log`）。

---

## 6. 给 Lead 的判定建议（D 段不改代码）

1. **必须处理**：§3.8 前端备注保存不发送 `if_version` → 双标签场景用户看不到 409（后端已有能力，接入即可）。
2. **需要产品决策**：客户端断开 ≠ 服务端取消（§3.9a：请求仍会跑完并落库、仍计成本）。
3. **措辞问题**：来源下拉把缓存物化数据集显示成「我上传」，与表头「用户数据集」和 `provenance` 不一致（§3.2）。
4. **可访问性**：顶栏两个图标按钮与文件输入缺少可读名字（§3.6c）。
5. **构建一致性**：本次走查跑在 `loaded_sha=5000acc909d8`（`/api/health` 报 `restart_required=true`，磁盘 `web_ui.py` 已变）；前端 13:07 重建与已加载指纹一致（`8c886724f29a`）。若 A/B 的最终改动要算进「用户验收通过」，需要在最终代码上**重启实例后重跑 §3.1/§3.5/§3.8 三项**。

---

## 复核追记（Lead 修复后，2026-10-24 13:37）

本报告判定的唯一「失败」项——**浏览器双标签冲突看不到 409**——已定位、修复并在真实浏览器复核通过：

- 根因：`frontend/src/pages/StockScreen.tsx` 的 `saveWatchNote` 请求体不带 `if_version`（后端 `/api/screen/watchlist` 的乐观并发校验本身是好的，API 渠道当时就通过）。
- 修复：保存备注时带「上次看到的该条 `version`」；返回 `code=version_conflict` 时显示提示并 `loadWatch()` 重读，不把对方内容覆盖掉。
- 复核（独立实例 8093，`frontend/dist` 为修复后构建；证据 `logs/d-recheck.log`、`screens/d-recheck-01/02`）：
  - 标签 A 保存 `if_version:2` → 200、version→3、note=「A页改1」；
  - 标签 B（旧版本）保存 `if_version:2` → **409**，`code=version_conflict`，`current.note=「A页改1」`；界面原文：「这条被另一个页面改过（版本冲突，本次没有写入）：已重新读取，请确认后再改一次。」；
  - 复核后服务端 GET 仍为「A页改1」、version 3 → **未覆盖**；
  - 同轮确认「预览覆盖与缺口」状态文字可见：`五字段可全算 1 格、部分可算 1 格、有材料但算不出 0 格、缺材料 0 格`（`screens/d-recheck-03`）。
- 因此本项改判：**通过**（浏览器渠道）。其余结论不变：通过 9→10 / 失败 1→0 / 未验 5。
- 另修正一处界面口径：数据集下拉此前把「官方缓存物化」显示成「我上传」，现按 `official || provenance_kind==='official_cache_materialized'` 标注为「官方缓存物化」，`research_material` 单独标注；后端 `provenance` 未改动。
- 复核备注：后台标签不派发真实 focus 事件，备注保存用合成 `focusout` 触达组件自身 `onBlur`（与真实失焦同一代码路径）；中途实例被外部杀掉一次，用同一数据目录重启后继续，B 标签旧状态未刷新，与「先开两标签、后写者用旧版本」同一路径；「Failed to fetch」出现于实例已死期间，未计入 409 证据。
