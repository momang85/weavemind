# W3 · 最终验收（统一公司页 + 自选变化简报）

- 日期：2026-10-24
- 执行人：lead（集成与最终验收）；分通道执行：d-acceptance（前端/浏览器）、pkg-hygiene（T0 计算）、research-materials（T1 材料）
- 结论：**通过（就本批 W0–W2 范围与全部可机验项）**；未验项逐条列明（见 §6），不冒充通过。

---

## 1. 最终构建指纹

| 项 | 值 |
| --- | --- |
| 分发包 | `tmp/watch-brief-package/weavemind-rc2-20261024T141228Z-cd6a388f.zip` |
| zip sha256 | `657c0d36b3bb8f1d42694a78c2d90c2c54ec68c85d99bd9b37ab9d7e0e03b737` |
| 构建内容 | 365 文件 / 12,675,565 B；`--verify` exit 0（manifest 覆盖 364 项 + 清单自身，逐文件 sha256 一致，无禁入内容） |
| 前端 | `npm run build`（`tsc -b && vite build`）EXIT=0；`npx tsc --noEmit` EXIT=0；`index-DQNxb1hv.js 212.66 kB`、`CompanyPage-BLEjbS7N.js 20.8 kB`、`StockScreen-B97ViVRw.js 92.43 kB` |
| 关键源文件（sha256 前 16） | `web_ui.py 023959f9d97e384f`、`screen_store.py e0ac5d308440e63e`、`stock_screen.py c9e27ca02db7d80c`、`screen_prepare.py 03b8f53e624175e4`、`adapters/eastmoney.py c71a101dd99fb15a`、`watch_brief.py 1be7d4a4f292ebfe`、`research_materials.py 419128eb02e48fd7`、`CompanyPage.tsx a9b6077a0e69f50e`、`StockScreen.tsx 0639e15d1049db9a`、`App.tsx b353f2c5a8f8bad5`、`frontend/dist/index.html f5aa392d1b8f9fd8` |
| 版本语义 | `stock_screen/1.3.0`（T0 输出语义变化，进 `impl_version`；旧冻结结果留旧版本可读，比较页提示版本差异、不当经营变化） |

## 2. 集成冒烟（临时实例 8094，跑完即停）

- 实例：`tmp/wb_instance_8094.py`（复刻 d2_instance_8093），数据目录 `tmp/wb-8094/`，口令只在本机 `tmp/wb-8094/secrets.env`（ACL 限当前用户，不入日志/报告）。**已停：netstat 无 8094 LISTENING（0 条）。**
- 种子：`tmp/w3-seed.py` 在 `tmp/wb-8094/screens` 造两份本人冻结记录（合成，不联网）：r1 `dc5ed4c7c3a0…`、r2 `947644ab40d5…`（600031 2024 营业收入 +3000 万）。

| 检查 | 命令/断言 | 结果 |
| --- | --- | --- |
| 登录 | `POST /api/login`（cookie 会话） | 200 `{"status":"ok","role":"admin"}` |
| 公司页 run 视图 | `GET /api/screen/company?code=600031.SH&run=<r1>` | 200；`financials` 12 条、`conditions` 1 条、`status=ok`（`logs/w3-company-r1.json` 由脚本目录 `tmp/wb-8094/` 持有） |
| 公司页选择输入 | `GET /api/screen/company?code=600031.SH`（无 run/dataset） | 200 `status=select_input`，候选 2 条（只列本人作用域，不自动打开） |
| 变化简报 | `GET /api/screen/brief?base=<r1>&current=<r2>` | 200；`brief_id=b1b887d9c98abdcf…`、`data_changed=1`、`cards=2`、`title=本次记录相对上次记录` |
| 基准设置 | `POST /api/screen/watchlist {"action":"set_baseline","run_id":<r1>}` | 200；`baseline_run==r1` |
| 基准并发保护 | 同上但带过期 `if_rev`（基准设置前的旧 rev） | **409** `version_conflict`，`current` 带服务端最新 rev，未写入 |
| 记录列表 | `GET /api/screen/runs` | 200，可见 2 条（仅本人作用域） |
| 静态资源 | `GET /` 引用 `assets/index-DQNxb1hv.js`；`GET /assets/CompanyPage-BLEjbS7N.js` | 200 / 200 |
| 健康 | `GET /api/health` | 200 |

## 3. 测试矩阵（`C:/Python314/python.exe <模块>.py`，逐文件 exit 0）

| 模块 | 用例数 | exit | 覆盖 |
| --- | --- | --- | --- |
| `test_stock_screen` | 61 | 0 | 判据/身份/期间政策（含 T0 G1 相邻年度同比） |
| `test_screen_prepare` | 11 | 0 | 预览绑定单一输入（T0 G3）、共享可计算判据（G2） |
| `test_screen_consistency` | 13 | 0 | 预览与执行读数一致 |
| `test_screen_store` | 71 | 0 | 持久化/损坏防护/作用域/G5 来源布尔与身份 |
| `test_screen_api` | 48 | 0 | 路由与权限（含新 company/brief/基准路由） |
| `test_data_rc2` | 18 | 0 | 上一批 A–D 回归 |
| `test_research_materials` | 10 | 0 | 研究材料（T1）回归 |
| `test_trial_t0` | 21 | 0 | G1–G4 反例 + 正控全钉 |
| `test_trial_t1` | 18 | 0 | G5/G6 修复回归 |
| `test_watch_brief` | 8 | 0 | 简报契约（变化/无变化/规则变化/期间切换/幂等/不改旧文件/零联网）+ 基准 rev 冲突 |
| `test_screen_continuity` | — | 0 | 连续性回归 |

## 4. 分通道证据

| 通道 | 证据 |
| --- | --- |
| 接口 | §2 冒烟表 + `tmp/wb-8094/`（w3-*.json、cookie、seed）；进程级隔离（`WEAVEMIND_SCREEN_DIR`/`WEAVEMIND_LOG_DIR` 指向 `tmp/wb-8094/`），真实缓存零写入；审计文件未重定向（见 §7 披露） |
| 组件 | §3 测试矩阵（11 文件全部 exit 0；mkdtemp/`_wipe()` 隔离） |
| 合成 | 合成样例两份 run → brief（本报告 §2 + `W2-brief.md` §2 表）；三一 600031 口径差异保持标记不混算（上一批结论继续有效） |
| 真实浏览器 | `screens/` 13 张（w1-01…w1-10 + w2-01…w2-03，含 corrupt、390px iframe 标注「非设备模拟」）；webbridge 会话 `w1-company`；用户保持浏览器控制关闭权 |

## 5. 本批门槛（W 批增量）

- 公司页/简报**零模型、零外网**：`test_watch_brief` 用桩断言生成期间无 `urlopen`；`cost.model_calls=0`、`network_requests="未知（本操作不发起外部请求）"`（简报本体不发起请求，措辞诚实）。✅
- 基准**不隐式推进**：看简报/生成简报不改 `baseline_run`；失败/409/不可比都不推进；只有显式 `set_baseline` 会改，且受 rev 乐观并发保护（本次集成把 `baseline_run` 纳入 rev 哈希，堵住「两个标签同旧 rev 各设一次、后写覆盖」的口子）。✅
- **旧结果不重写**：`test_watch_brief::test_brief_id_is_idempotent_and_old_files_unchanged` 断言 run.json 字节前后一致。✅
- 公司页**完整寻址**：`code + run/dataset` 必填；只有 code → `select_input` 候选（本作用域），绝不按代码自动打开不明来源记录。✅
- 合成标签全程可见、来源措辞不写「均已独立核验」：前端 W1 走查截图（w1-08 用户来源）。✅

## 6. 未验（不冒充通过）

1. **Python 3.11 运行时**：本机无 3.11（`py -3.11`/`python3.11`/`C:\Python311` 均不可用）；本批代码仅 stdlib、无 3.12+ 语法，但运行时本身未验。
2. **真实账号/真实数据路径**：生产 8080 未动、无监听；未用真实密钥；仓库 `workspace/**` 仍无 `working_paper.json`/`financials.json`，真实数据事实层未验（TRIAL-READY T1 结论继续有效）。
3. **原生文件选择框 / 真键盘 Tab 顺序 / 真实窄视口**：浏览器扩展未开「允许访问文件 URL」、`resizeTo` 改不了真实 `innerWidth`、后台标签无真实焦点；只做了同源 iframe 390px 近似与合成事件。
4. **UI 造不出的状态**：`stale_reference`（run 指纹在 run 侧、不逐次重算文件字节）、`forbidden`（需另一作用域）、简报 `no_baseline`/`one_record`/`failed` 文案（本页简报必须显式选两份记录）。库级/API 层对应语义已有测试覆盖。
5. **真实浏览器 E2E 复放**：截图为一次性走查，非可重复脚本（保持浏览器控制归用户）。

## 7. 范围与边界

- 未 commit、未 push、未建分支；工作区保留全部未提交改动（HEAD `ed8fd90`）。
- 生产 8080 无监听；8087/8089–8097 临时端口全部停净；真实缓存 `data/screen_cache/official_disclosure_cache.json` 未动。
- 审计（诚实记录一次隔离缺口）：`logs/audit.jsonl` 新增 43 行（05:35 之后：13 时 37 行 = 8095 实例走查、14 时 6 行 = 8094 集成冒烟），内容只有 `admin` 角色的 login.success / screen.watchlist.* 动作记录，无密码、无用户数据、无生产数据。原因：两个临时实例脚本都重定向了 `WEAVEMIND_LOG_DIR` 但没有重定向审计文件（`AUDIT_FILE`）。已作为**遗留缺陷**记下（实例脚本模板需把 `AUDIT_FILE` 一并指向 tmp），审计日志为 append-only 台账，故只披露不删改。
- 浏览器标签组「W1 公司页与变化简报走查」保留，由用户决定关闭。
