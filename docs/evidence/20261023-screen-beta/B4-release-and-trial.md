# B4 试用包与发布准备

任务书：`docs/员工任务_选股研究Beta_真实数据与普通用户闭环_20261023.md` §B4
本报告写**这一轮实际打出来的包、实际做过的首启检查、以及一份还没执行的部署/回滚清单**。
凡是没做的（真人试用、生产部署）都在第九节明确写成「未验 / 未执行」，不用测试结果冒充发布。

## 一、这一阶段交付什么

1. 一份给试用者的脚本：`docs/evidence/20261023-screen-beta/trial-script.md`（10 步，含预期、等待时间、失败处理）；
2. 一份干净首启检查（全新数据目录，11 项全过）；
3. 打包清单：改了哪些文件、每个文件指纹、依赖与数据目录要求、构建产物指纹；
4. 部署清单与回滚清单（**未执行**：本单没有生产启停权限）；
5. 内部走查记录（agent 自测，不是真人试用）。

## 二、本单改动的文件与指纹（sha256 前 12 位，2026-10-23 20:32 实测）

| 文件 | 行数 | sha256[:12] | 状态 |
|---|---|---|---|
| `stock_screen.py` | 1042 | `4fbc9ef22af6` | 新增（R1 选股内核，实现版本 `stock_screen/1.2.1`） |
| `screen_store.py` | 2033 | `7601efe1f2b9` | 新增（冻结存储 + 清单/模板/成本账本） |
| `web_ui.py` | 10924 | `ac6241ce5450` | 修改（8 条选股写路由 + B3 三条只读/写入路由） |
| `frontend/src/pages/StockScreen.tsx` | 2046 | `f1bb9889a17d` | 新增（三步流程 + 图表 + 清单/模板/成本卡） |
| `frontend/src/components/Login.tsx` | 109 | `42be8a9b1cad` | 修改（登录回填 role，不再写死 viewer） |
| `frontend/src/lib/settingsText.ts` | 66 | `9e08715b05d9` | 修改（`researcher → 研究员（自助研究）`） |
| `frontend/src/pages/Settings.tsx` | 845 | `bf6316a751aa` | 修改（角色下拉加入自助研究角色） |
| `frontend/src/auth.ts` | 69 | `d2eedd9c9811` | 修改（`role: string`） |
| `frontend/src/components/AppLayout.tsx` | 221 | `58376fc9f1cb` | 修改（角色标签） |
| `test_stock_screen.py` | 745 | `1f32bced34c0` | 新增 59 用例 |
| `test_screen_store.py` | 1370 | `7039701711ca` | 新增 77 用例 |
| `test_screen_api.py` | 1006 | `eefe98463349` | 新增 48 用例 |
| `test_screen_continuity.py` | 268 | `666d5fb6db32` | 新增 24 用例 |
| `test_auth_audit.py` | 810 | `df616e17f510` | 修改 27 用例（含本单回归） |
| `tmp/r1next_isolated_instance.py` | — | — | 隔离实例启动脚本（8083，不进包） |
| `tmp/b4_fresh_instance.py` | — | — | 干净首启脚本（8084，不进包） |

构建产物（前端，`frontend/dist-screen-check/`）：`index.html` sha256 前 12 位 `b416e5739440`（804 B，20:18 构建），入口资源 `StockScreen-BBP7Xn4z.js`。

**生产前端产物 `frontend/dist/` 未动**：`index.html` 仍是 2026-10-22 23:39 的 `a4683563d746`。按边界要求，本单不重建、不替换生产 dist。

## 三、依赖与运行环境

- **无新增第三方依赖**：`stock_screen.py` / `screen_store.py` 只用标准库（`json/hashlib/csv/decimal/os/pathlib/threading` 等）；`web_ui.py` 的改动不引入新 import；前端未加 npm 包（图表是手写 SVG，不引图表库）。
- Python：目标 3.11。本机只有 3.14.3 与 3.12：
  - 3.14.3 上 235 个用例全过（`logs/b3-tests.log`）；
  - 3.12 上 `test_stock_screen`（59）、`test_screen_store`（77）、`test_screen_continuity`（24）全过，`web_ui.py` 等 7 个文件 `py_compile` 通过（`logs/b4-py312-tests.log`，`PY312_COMPILE_EXIT=0`）；
  - 3.12 上 `test_screen_api` **导入失败**：`ModuleNotFoundError: No module named 'redis'`——是 3.12 解释器里没装 `redis` 这个运行依赖，不是代码不兼容。要在 3.11 上确认，需要一台按 `requirements` 装好依赖的机器，本单没有 → 记「未验」。
- 数据目录（必须显式给，否则默认落仓库目录）：
  - `WEAVEMIND_SCREEN_DIR`：筛选存储根（`scopes/<user>/<project>/{datasets,runs,presets}` + `watchlist/`、`cost/`）。生产建议 `/data/screen`；
  - `WEAVEMIND_SCREEN_CACHE`：内置真实披露缓存 JSON。**该文件不在 git 里**（`data/screen_cache/official_disclosure_cache.json`，sha256 `4dc828ef0bea…`，6 行，`synthetic:false`，含洋河 2022–2025 + 三一 2023–2024）。干净检出/镜像里没有它，首启只会有空股票池；
  - `agents.db`、`logs/`、`share_links.json`：与现有部署相同。
- 权限：`web_ui` 进程需要对 `WEAVEMIND_SCREEN_DIR` 有读写权限（写入是临时目录 + `os.rename`，需要同分区目录）；上传数据集要能落盘。

## 四、干净首启检查（8084，全新数据目录）

脚本 `tmp/b4_fresh_instance.py`：全新 `tmp/b4-fresh/`（无数据集、无清单、无历史、无预设），挂当前代码 + 当前 dist，挂真实缓存，账号 `researcher`。检查脚本 `tmp/b4_first_start.py`，日志 `logs/b4-first-start.log`，**退出码 0，11/11 PASS**：

| 检查 | 结果 |
|---|---|
| `/api/health` | 200 `status=ok` |
| researcher 登录 | 200，`role=researcher` |
| `/api/screen/capabilities` | `can_research=true`（自助研究角色生效） |
| `/api/screen/pool` | `available_pool` = 洋河 `002304.SZ`、三一 `600031.SH`（真实缓存被读到） |
| `/api/screen/watchlist` | 空清单 |
| `/api/screen/datasets` | 空数据集 |
| `/api/screen/cost` | `runs_total=0`、`compute_runs=0`、`external_fee=unknown`（明确为零，不是缺失） |
| 未上传数据集直接执行 | 200，`source=official_disclosure_cache`（用内置真实缓存，不是空结果也不是 500） |
| 上传 6 行真实 CSV | 200，`dataset_id=a18408ef…` |
| 同条件执行 | 200，`run_id=fa78fcac…`，`pass=1` |
| 账本随执行累加 | `0 → 2` |

首启还验证了「历史结果为空」时结果区给空状态、不给假数据。**注意**：这一步只证明「代码 + 当前 dist + 真实缓存」能起，不证明 3.11 环境能起（见第三节）。

## 五、本阶段暴露的一个环境事实（必须记录）

本单开工时记录的「生产 8080 存活」在收尾时不再成立：`http://127.0.0.1:8080/api/health` 现在是 **connection refused**，`netstat` 没有 8080 的 LISTENING，`logs/webui.log` 最后一行停在 **2026-10-23 16:17:45**，说明生产 webui 进程在那之后就不在了；当前在跑的 python 进程只有 `tmp/r1c_isolated_instance.py`（8081/8082，其 `/api/health` 也不响应）、本单的 8083/8084。

本单对生产 8080 **没有做任何启停、部署、构建替换**（任务书 §6 明确禁止，且本会话无该权限）。这条只作为事实记录：谁要部署，得先把 8080 起回来，或者按第六节流程走一遍。

## 六、部署清单（**未执行**，需授权后按序做）

前提：有 8080 启停权限；有真人窗口可回滚；已备份。

1. 冻结现场：`git status --porcelain > /tmp/before.txt`；把本报告第二节的 14 个文件 + `frontend/dist/` 打成一个时间戳 tar；
2. 构建前端：`cd frontend && npm ci && npx tsc -b && npx vite build`（默认输出 `frontend/dist`，**不要**用 `dist-screen-check` 冒充生产产物）；
3. 确认后端依赖已装（`redis` 等），Python 3.11 下跑一遍：`python -m unittest test_stock_screen test_screen_store test_screen_api test_screen_continuity test_auth_audit`，五个退出码必须全 0；
4. 停 8080（用 `netstat` 找到监听 8080 的 pid，只杀这一个；不要按进程名批量杀）；
5. 起服务：`launcher.py up`（或现有部署方式），确认 16 个进程起来；
6. 验收（缺一不可）：
   - `GET /api/health` 200，且 `build.restart_required=false`（构建指纹与磁盘一致）；
   - `POST /api/login` 管理员登录 200；
   - 打开 `/screen`：三步结构与「成本与请求」卡可见；
   - 用旧 `run_id`（例如 8083 上的 `1d66f114901b…` 同输入）在**同一份缓存数据**上执行：应 `reused=true`，不新建；
   - 上传一份 CSV → 执行 → 冻结成功；清单加一条 → 刷新仍在；
7. 记录：`/api/health` 的构建指纹、`run_id`、失败步骤与时间点。

## 七、回滚清单（**未执行**）

触发条件（任一）：`/api/health` 起不来或 `restart_required=true` 无法消除；登录 500；`/screen` 白屏；执行筛选 500；清单/模板写入报 500。

1. 停当前 8080（同上，只杀监听 8080 的 pid）；
2. 恢复第六节第 1 步的 tar（14 个文件 + `frontend/dist/`）；
3. 若第 2 步只恢复了源码，重新 `npx vite build` 出与备份一致的 dist（或直接恢复备份的 dist 目录）；
4. 起服务，验收：`/api/health` 200、登录 200、`/screen` 可打开、旧结果可「查看原版」；
5. 回滚后**不要**删除新数据目录（`scopes/` 里的数据集/结果/清单是用户数据，删了就找不回）；新代码写下的 `run_id` 在旧代码下不可读属于预期，旧代码只认自己的存储布局——所以回滚前先导出一份结果清单；
6. 记录回滚时间、原因、恢复的文件指纹。

## 八、试用脚本与内部走查

- 试用脚本：`trial-script.md`（含「谁先开权限」「每步预期」「失败怎么办」「结束时请记下什么」）。
- 内部走查：agent 以 `researcher` 账号在 8083 上按脚本走完 10 步（20:19–20:28），结果写进 `trial-script.md` 末尾表格；关键节点有截图 `07-b3-watchlist-and-cost.png`、`08-b3-manual-update-compare.png`。
- **真人试用：未验**。本单没有第二位使用者，不能写「用户验证通过」，也不能把 agent 的点击当成普通用户的首次体验。真实试用至少要 2 个人（一个没看过产品的人 + 一个会看年报的人），并按脚本第 10 节收集卡点。

## 八之二、试用记录表（给真人试用时用，本次未填真实数据）

任务书要求单独记录「数据准备/配置时间、首次任务时间、重复使用时间、理解错误、失败恢复、退出原因与外部费用」。
下表是模板；本单没有真人受试者，**只有代理操作耗时**，不能当成用户通过率或体验评分。

| 参与者 | 数据准备/配置（分钟） | 首次筛选（分钟） | 重复使用（分钟） | 理解错误（哪句话/哪个按钮） | 失败恢复（卡点→怎么恢复） | 退出原因 | 外部费用 |
|---|---|---|---|---|---|---|---|
| P1 | | | | | | | |
| P2 | | | | | | | |

代理内部走查耗时（**不是真人耗时**，仅供估算量级；时间取自日志与操作时间戳）：

| 环节 | 代理耗时 | 说明 |
|---|---|---|
| 登录到页面可操作 | ≈5 秒 | 页面加载 + 能力探测 |
| 选范围 + 选条件（数据集、勾 1 家、点一个示例） | ≈40 秒 | 合成事件，无阅读时间 |
| 首次执行（同输入复用） | <1 秒 | 服务端 `reused=true`，`compute_ms=0` |
| 首次执行（真算，6 行数据） | 16–31 ms 计算 + 1–2 秒请求往返 | 服务端 `compute_ms` 实测 |
| 重复使用（重开页面 → 选两份结果 → 对比） | ≈60 秒 | 含页面重载 |
| 数据准备（上传 6 行 CSV） | <1 秒 | 本机 HTTP 上传；真实用户要加上找文件/对列名的时间 |

真人试用时至少要补：找一个没看过产品的人，只给脚本不给口头提示，记录他卡在哪一句；再找一个会看年报的人，检查「理由能否回到原文核对」。

## 八之三、量化后续：下一单的最小实验定义（不安装 Qlib/RQSDK，不扩撮合引擎）

本单不做收益结论，也不把「有回测原型」当成「能出收益」。下一单若要碰量化，先满足下列逐项前置，缺一项就只写「受限实验」：

**必须逐项具备（缺一即明确写限制，不得省略）**

1. 历史股票池（PIT 成分/上市退市状态），不能用今日池回填历史；
2. 当时可知的财务版本：以 `available_at/disclosed_at` 为准，不用「报告期」冒充「当时能知道」；
3. 行情与公司行动（复权、分红送转、停牌、退市）；
4. 独立交易日历（现成 `adapters.trading_calendar`；不要用「有行情就有交易日」代替）；
5. 证券交易约束（涨跌停、T+1、最小交易单位、流动性/停牌不可成交）；
6. 基准与成本（指数基准、佣金/印花税/滑点，逐项写明来源与被验证程度）；
7. 数据许可（公开可访问 ≠ 允许商业再分发；引用官方说明，不编造报价）。

**冻结与顺序**

- 规则与留出区间**预先冻结**（写进实验定义文件，先冻结再跑）；
- 先验证两件事：①同输入可复现同一 `run_id`/结果哈希；②账本（请求/计算/缓存/模型/外部费用）与实际一致；
- 只有①②都过，才允许谈因子、组合权重与模拟跟踪；
- 结论只按「已具备项」下，缺项写进「本实验不能回答的问题」，不给出不受限的收益结论。

**本单已有的、可直接复用的部分**：确定性筛选内核与冻结存储（`run_id`/`result_hash`/`snapshot_id`）、字段白名单与期间口径、来源与核验状态分离、成本账本、两份结果的只读对比与归因（数据变 / 条件变 / 实现变分开）。

## 九、未验与遗留（逐条给理由）

| 项 | 状态 | 原因 / 影响 |
|---|---|---|
| Python 3.11 下的全量测试与启动 | 未验 | 本机无 3.11；3.12 能过 160 个用例 + 7 文件编译，3.12 的 API 用例因缺 `redis` 包导入失败（环境依赖，不是代码） |
| 真实用户试用（2 人以上） | 未验 | 本单没有真人参与者；脚本已就绪 |
| 生产 8080 部署与回滚演练 | 未执行 | 无启停/部署权限（任务书 §6）；清单已写，等授权 |
| 生产 8080 当前状态 | 已知异常 | 开机时记录存活，收尾时 connection refused，webui 日志停在 16:17:45；本单未做任何启停 |
| 内置真实缓存随包分发 | 未做 | 该 JSON 不在 git（6 行真实数据，含来源 URL）；干净环境需单独提供，否则首启股票池为空 |
| Chrome 下载/文件选择器 | 未验 | 自动化下下载未落盘、系统文件对话框点不到；B3 报告已记 |
| 20–50 家真实样本规模 | 未验 | 环境 fake-IP DNS 导致抓取被 SSRF 守卫拒绝（B1 报告），当前只有 2 家真实公司 |
| `>20` 家公司分页、清单 200 上限 | 未验 | 存层/API 有上限用例，UI 未造大数据 |

## 十、结论

试用包在「代码 + 构建产物 + 数据目录 + 权限 + 首启检查」五件事上是自洽的：全新目录 11 项检查全过，235 个用例全过（3.14），3.12 上除运行依赖缺失外也过。

但发布这一环**没有做**：生产 8080 目前是停的，本单没权限也没动它；部署/回滚清单是纸面流程，需要授权后由有权限的人执行并留下验收记录。真人试用同样未发生——这一条不能用「内部走查通过」替代。
