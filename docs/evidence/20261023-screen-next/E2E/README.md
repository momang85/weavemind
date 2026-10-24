# 测试账号端到端验收（E2E）

生成时间：2026-10-23 18:2x（本机 +08:00）
实例：隔离测试实例 `http://127.0.0.1:8083`（`tmp/r1next-isolated`，前端取自 `frontend/dist-screen-check`）
账号：`admin`（管理员）、`viewer`（只读）——两个都是本单隔离实例专用的测试账号，密码只写在 `tmp/r1next_isolated_instance.py`，不进报告。
生产 8080：**未重启、未部署、未改配置**。

---

## 一句话结论

架构师派单的四批（R1.1 身份/完整性/范围/期间 → R1.2 用户数据导入闭环 → R1.3 手工更新与冻结结果比较 → R2 最小历史实验与缺口方案）都已落地并有报告与证据；
本轮做了**受控重启**（隔离实例）后用 `admin` / `viewer` 两个测试账号跑完整闭环，HTTP 走查 **22/22 PASS**，浏览器里也真点了一遍（含 viewer 的 403 与作用域隔离）。
过程中发现并修掉一个真实噪音：浏览器刷新/切账号时客户端 abort 会让服务端打一整页 traceback——现在只记 debug，并补了 1 个单测；同时把「资料不足变化」从「结论翻转」里拆成两个独立计数（四模块合计 **184** 项全 OK）。

---

## 一、派单交付物核对

| 派单环节 | 交付 | 状态 |
|---|---|---|
| R1.1 身份 / 完整性 / 范围 / 期间阻断项（含架构师三条探针） | `R1.1-report.md` + `R1.1/` | 完成（探针已变会失败的测试） |
| R1.2 用户数据导入闭环（模板 → 上传 → 预览 → 筛选 → 证据 → 保存重开） | `R1.2-report.md` + `R1.2/` | 完成 |
| R1.3 手动更新与两份冻结结果比较（不可归因标注、字段白名单） | `R1.3-report.md` + `R1.3/` | 完成 |
| R2 最小历史实验 + 数据缺口设计 | `R2-minimal-history-experiment.md` | 完成（只交方案，未扩引擎） |
| §7 每批测试 / 反例 / 真实退出码 / 用户闭环 / 边界 / 部署状态 | 各报告 + `tests.log`、`http-walk.log`、`build.log`、`diff-HEAD.patch` | 完成 |
| 未授权项（提交推送、生产重启/部署、付费、删真实产物…） | 全程未触碰 | 遵守 |

---

## 二、受控重启记录（只动本单实例）

1. 按端口精确定位 8083 的监听进程 → `taskkill //F //PID <pid>`（本次两次：`40436` → 首次 E2E 前，`45164` → 修完断开噪音后）。
2. 重新 `python tmp/r1next_isolated_instance.py`（后台）拉起，数据目录仍是 `tmp/r1next-isolated`（结果/预设/数据集原样保留）。
3. `/api/health` → `status: ok`、`build.restart_required: false`（前后端同版，无需再重启）。
4. 重启后的服务日志里 **0 个 Traceback**（`grep -c Traceback tmp/r1next-isolated/boot-e2e2.log` → 0）。

---

## 三、双账号 HTTP 端到端（`E2E/test-accounts-walk.log`，22/22 PASS，退出码 0）

| 分组 | 检查 | 结果 |
|---|---|---|
| 基础 | ① health 200 且 `restart_required=false`；② admin 登录角色正确；③ viewer 登录角色正确；④ 未登录读接口 401 | PASS |
| 数据入口 | ⑤ 模板下载（BOM + 表头）；⑥ 上传用户数据（内容寻址，同内容复用同一 `dataset_id`）；⑦ 预览给快照与来源且标 `user_provided`（不落结果） | PASS |
| 主流程 | ⑧ 执行筛选冻结结果；⑨ 证据绑该 `run_id`；⑩ 预设记住数据来源；⑪ 预设可读回；⑫ 重开原版哈希不变；⑬ 两份结果对比给出归因定性；⑭ 同参重放同一 `run_id` 且 `reused` | PASS |
| 计数语义 | 对比面板新增「新变资料不足 / 资料不足已补齐」两个独立计数（不与「结论翻转」混用） | PASS（截图 01 可见） |
| 权限/作用域 | ⑮ viewer 看不到 admin 的结果；⑯ 读 admin 的 run → 404；⑰ 对比 admin 的结果 → 404；⑱ viewer 执行筛选 → 403 权限不足；⑲ 看不到 admin 预设；⑳ 看不到 admin 数据集；㉑ viewer 允许下载模板（只读动作） | PASS |
| 只读性 | ㉒ 对比/重开之后两份原版哈希都没变 | PASS |

---

## 四、浏览器 UI 走查（测试账号真点）

| 步骤 | 结果 | 截图 |
|---|---|---|
| 重启后 admin 打开「可解释选股」：17 份历史结果、3 个数据来源（官方缓存 / 我的财报导入 / 模板直传）、4 个预设 | 正常 | — |
| admin 选两份结果点「对比这两份」 | 归因横幅 + 条件卡 + 读数差异正常渲染 | `01-admin-compare-after-restart.png` |
| 退出 admin → 走登录页用 `viewer` 登录 | 顶栏显示「viewer 只读」 | — |
| viewer 看页面：数据来源只有官方缓存（看不到 admin 上传的两份）、预设只有「（不载入）」、历史结果「还没有已保存的结果」 | 作用域隔离在 UI 上可见 | — |
| viewer 勾两家公司点「执行筛选」 | 红条「操作未完成 / 权限不足：仅管理员可执行此操作」 | `02-viewer-403-execute.png` |
| 退出 viewer → 登录页用 `admin` 登录 | 顶栏「admin 管理员」，预设与 17 份历史回来 | `03-admin-restored.png` |

> 说明：本轮切换账号走的是应用自己的登录页（先 `POST /api/logout` 再登录），不是直接改 cookie；
> 若用 fetch 直接换 cookie，页头用户名是前端缓存的，会短暂显示旧名字——这是测试手法造成的，不是产品缺陷。

---

## 五、本轮发现并修的问题

1. **客户端提前断开 → 服务端一整页 traceback**（真实出现在重启前那次走查：浏览器标签页 abort，`ConnectionAbortedError [WinError 10053]` 打进服务端日志）。
   修法：`Handler._write_body` 统一兜住 `BrokenPipeError / ConnectionAbortedError / ConnectionResetError`，只记 debug；`_json` 与 `_html` 都改走它。
   回归：`test_screen_api.TestDisconnectTolerance`（1 项）驱动一个 `write()` 必抛断开的替身，确认 `_json` / `_html` 都不抛且状态码仍是 200。
2. **测试数据里出现同名预设**（同一个「E2E 预设 20261023」被重复保存出多条）：这是**已知未改**的语义——`保存为预设` 现在是「新建」，同名不会覆盖，下拉里会出现两条看不清区别的项。
   本批没有动这个语义（改覆盖会丢失用户的变体），已在下面列为下一步建议；顺手清掉了隔离实例里我自己造出的重复项，只留一条。

最终测试计数（`E2E/tests.log`）：`test_stock_screen` 59 + `test_screen_store` 64 + `test_screen_api` 34 + `test_auth_audit` 27 = **184 全 OK**。

---

## 六、边界与部署状态

- 生产 8080：进程仍在跑旧代码，`build.restart_required: true`；`frontend/dist` 未重建、未部署。上线仍需**一次受控重启 + dist 同步**的授权（本单未授权，未做）。
- 隔离实例 8083：跑的是当前代码（`restart_required: false`），数据都在 `tmp/r1next-isolated`，可随时再点或按端口精确停掉。
- 本轮全部动作：0 次模型调用、0 次联网抓取、0 元数据费用；未 `git add/commit/push`，未碰他人 dirty 路径。

---

## 七、前后端同版部署 / 回退清单（待授权，**未执行**）

> 本单未授权重启/部署生产，下面是执行清单，不是已发生的动作。

**上线前确认**
1. 待部署文件清单只有本单改动：`web_ui.py`、`stock_screen.py`、`screen_store.py`、`frontend/src/pages/StockScreen.tsx`（+ 测试）；不带他人 dirty 路径。
2. 后端环境：服务进程**不带** `WEAVEMIND_TEST_MODE`；`SCREEN_DIR` 与 `data/screen_cache` 指向真实数据；真实缓存 sha256 仍为 `4dc828ef…`（6 行、2 家公司，**不是市场样本**）。
3. 前端：`npx tsc -b && npx vite build`（不带 `--outDir` 即输出 `frontend/dist`），记录 `index.html` 引用的入口文件名与 sha256。
4. 只有重启后 `/api/health` 的 `build.restart_required` 变 `false`，才算「已加载代码 = 磁盘代码」。

**部署顺序（每步可回退）**
1. 备份现有前端产物：`cp -r frontend/dist frontend/dist.bak-<ts>`。
2. 构建并**一次性**替换 `frontend/dist`（不要只换一半，避免新旧 chunk 混用）。
3. 记录 8080 的 pid 与启动时间；用 `launcher.py` 走受控重启（不按进程名批量杀、不影响其他会话）。
4. 起服务后 `curl /api/health`：`status: ok`、`restart_required: false`，实例 id 已更新。
5. 生产只做**只读**冒烟：打开「可解释选股」→ 展开历史结果 → 对两份既有结果做一次对比（GET 只读）；不新建数据、不跑新筛选。
6. 让用户在真实账号下点一遍，再宣布可用。

**回退顺序**
1. 停服务 → `mv frontend/dist.bak-<ts> frontend/dist` → 起服务 → 再查 `/api/health`。
2. 后端回退：本次改动**未 commit**，工作区可用 `git apply -R docs/evidence/20261023-screen-next/R1.3/diff-HEAD.patch` 反向应用（只回退本单文件）；或用备份文件覆盖。
3. 回退后同样确认 `restart_required` 与预期一致（只回退前端、未重启后端时它仍为 `true`，属正常）。

**兼容性（实测结论）**
- 「旧前端 + 新后端」：可运行。新接口只是新增路由，旧 dist 不调用它们；本单 8080 旧 dist 与新后端并存期间页面正常。
- 「新前端 + 旧后端」：**不可**——新前端会请求 `/api/screen/datasets`、`/api/screen/compare` 等不存在的路由（404）。所以顺序必须是**先重启后端，再换 dist**。

### 干净检出 / 镜像数据检查（已实测）

派单要求「新数据目录和镜像/持久卷路径做干净检出检查」。做法：`git clone` 一份干净检出（不含任何未跟踪文件）→ 应用本单补丁 → 在克隆里跑四个模块。

结果（`clean-checkout-tests.log`）：**Ran 179 tests, OK (skipped=2)**。两处 skip 都是「本机没有真实披露缓存样本」的类级跳过（`TestRealCacheSample`、`test_real_cache_sample_two_companies`）——这正是干净检出/容器镜像的真实状态，跳过而不是失败。

| 检查项 | 结论 |
|---|---|
| `Dockerfile` 的 `COPY *.py` 是否带得上新模块 | 带得上。`stock_screen.py` / `screen_store.py` 都在根目录，`COPY *.py ./` 覆盖；`COPY frontend/dist` 来自镜像内 Vite 构建 |
| 镜像里有没有 `data/screen_cache` | **没有**（`data/` 未进镜像）。所以容器首次启动时官方缓存必然缺失——产品行为是**明确说无数据**，不是崩：接口回 `usable:false, errors:["文件不存在：…"]`，页面同时显示「官方缓存不可用：文件不存在：…（可以下载模板用 CSV 上传自己的数据）」+「这个数据来源里还没有可用主体」的引导（截图 `04-no-sample-data-explicit.png`，用 8084 的「无缓存实例」实测） |
| 用户上传的数据/结果落在哪 | 默认落在**仓库目录** `workspace/screen`（`screen_store.screen_base()`），而镜像只把 `WEAVEMIND_DATA_DIR=/data` 挂成卷 → 容器重建后用户数据会丢。**部署前应当设 `WEAVEMIND_SCREEN_DIR=/data/screen`**（并把 `WEAVEMIND_SCREEN_CACHE` 指到卷内或按需上传），本单未改部署配置 |
| 未跟踪本地文件是否影响「干净部署」 | 不影响功能，但真实缓存在本机是**未跟踪文件**：干净检出/镜像里没有它，不能拿它假装「开箱有真实数据」。真实缓存只有 2 家公司共 6 行，任何时候都不当市场样本 |
| 合成数据会不会被误当真实 | 不会：合成夹具带 `synthetic:true`，页面对应位置明写「当前缓存是合成演示数据，不进真实候选」；干净检出里连合成缓存都没有时按上面「无数据」处理 |

## 八、复现

```bash
# 1) 受控重启隔离实例（只按 8083 端口定位进程）
netstat -ano | grep ":8083.*LISTENING"        # 取最后一列 PID
taskkill //F //PID <pid>
C:/Python314/python.exe tmp/r1next_isolated_instance.py   # 后台常驻

# 2) 双账号端到端（admin + viewer，22 项）
C:/Python314/python.exe tmp/e2e_test_accounts.py

# 3) 四模块回归
C:/Python314/python.exe -m unittest test_stock_screen test_screen_store test_screen_api test_auth_audit
```

## 九、下一步建议（都不影响当前可用）

1. 预设同名时给「覆盖 / 另存为」二选一（或下拉里按名称去重并显示条数），避免同名项分不清。
2. 数据集删除入口 + 损坏结果「隔离」按钮（目前只有 API）。
3. 若要把本轮成果带到生产：一次受控重启 + 把 `frontend/dist-screen-check` 同步为生产 dist，并在页面上确认真实缓存仍只有那两家公司（不把它当市场样本）。
