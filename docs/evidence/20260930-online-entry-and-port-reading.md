# 2026-09-30 · 官方入口公网在线实测 + 端口读数小修（L4 前置补缺）

本批三件事，全部**只跑定向验证**：官方入口真实公网可达（无模型、无付费）、更正稿正文缓存补齐
并离线复验、`launcher.py status` 端口读数自相矛盾的小修。**未改用户模板/模型/代理/权限/人审**，
未追加付费试错，未动真实库。

---

## A. `launcher.py status` 探活端口与实际端口不一致（小修）

### A1. 复现（真实实例 + 过期记录，非替身）

现场：16/16 服务存活、工作台实际在 `8080` 上响应 `HTTP 200`，而
`.weavimind/runtime_ports.json` 里留着上一次让位的记录 `{"web": 8081, "preferred": 8080}`。

| 读数 | 修前（`e3f8a1e`） | 修后 |
|---|---|---|
| `launcher.py status` URL 行 | `URL: http://localhost:8081` | `URL: http://localhost:8080` |
| `launcher.py status` 工作台行 | `[!!] 工作台：未响应（[WinError 10061] 目标计算机积极拒绝）@ 8081` | `[OK] 工作台：HTTP 200 @ 8080` |
| 端口记录说明 | 无（只说"未响应"） | `[--] 端口记录与运行实例不一致：记录的 8081 无响应，实际在 8080 响应（已按实际端口显示；`python launcher.py stop` 后重新启动即可刷新记录）` |
| `launcher.py url`（stdout） | `http://localhost:8081`（打不开） | `http://localhost:8080`（说明走 stderr，stdout 仍单行） |

修前的读数会让人以为"服务全在跑但工作台死了"，而页面其实是好的。

### A2. 根因（两条，都修）

1. **URL 与探活各自解析一次端口**：`print_status` 先 `web_url()`（自己解析一次），
   再 `readiness_report()`（又解析一次）。两次解析之间只要归属校验的结论不同，就会打出两个端口。
   → 现在一次读取解析，URL、探活、返回的 `port/url` 同源；记录端口无响应时依次探活候选端口，
   以**真正响应的端口**为准，并如实打印"记录与实例不一致"。
2. **测试用例真的改写了运行状态**：`start_services` 的假进程路径（`test_recorded_spawn_failures_round_trip`
   等）没有隔离让位记录，而本机 8080 上有真在跑的工作台 → 用例把真实的
   `.weavimind/runtime_ports.json` 写成 `{"web": 8081, "preferred": 8080}`（现场文件时间戳可查）。
   → 该测试文件 `setUpModule` 把 `RUNTIME_PORTS_FILE` 重定向到临时目录；并加一条守卫用例
   （`TestRuntimeStateIsolation`）断言跑完 `start_services` 后**真实文件的字节不变**，
   同时断言让位记录确实落在隔离文件里（证明那条路径真的会写）。

### A3. 为什么只在"本实例持有 webui"时才看备选端口

`answering_web_port()` 的备选端口检查以 `_webui_owned_here()` 为条件：本实例的 webui 不在跑时，
默认端口上响应的可能是**别人的**程序——把那个端口报成"我们的工作台"会把用户带到错误页面。
用例 `test_foreign_listener_is_never_reported_as_our_workbench` 钉住这条（无归属 → 不探备选、如实报未响应）。

**定向读数**：`test_startup_readiness.py` **76 用例全过**（原 73 + 3 条新读数 + 隔离守卫，退出码 0）。

---

## B. 官方入口**公网在线**实测（`scripts/a2_official_entry_online.py`，无模型/无付费）

此前"官方入口可达"只有离线替身读数。本脚本直接问官方端点（POST 表单查询，出域校验 + 已验 IP 直连）：

| 查询 | 状态 | 候选 | 端点读数 |
|---|---|---|---|
| 京蓝科技 000711（2019/2020） | `found` | 5 条 | `http://www.cninfo.com.cn/new/hisAnnouncement/query` HTTP 200，契约 `cninfo-hisannouncement-v1`，`total=25` |
| 三一重工 600031（2023/2024） | `found` | 4 条 | 同端点 200 |
| **反例** 不存在的公司 999999 | `no_candidates` | 0 条 | `reason_code=empty_result`，理由"999999 不在巨潮证券索引里（非 A 股/已退市/代码有误）" |

**更正稿可见**：不带 `until` 查询时，候选里同时出现
`京蓝科技股份有限公司2020年年度报告（更正后）`（2025-09-05，`version=corrected`）与原始版
`2020年年度报告`（2021-04-27，`version=original`）——"更正版与原始版并存、不覆盖"在真实端点上也成立。

### B1. 顺带修掉一个真缺陷：**内容级否定被记成"端点不可用"**

实机反例：代码 999999 返回 `cninfo[orgId 映射]: 999999 不在巨潮证券索引里`，此前 `discover()`
把任何异常都记成 `status="unavailable"` → 页面/编排器会显示"端点不可用，稍后重试"，
而用户真正该做的是核对代码。修法：内容级原因码（`empty_result` / `irrelevant_result`）
记成 `no_candidates`，传输/限流/结构变化才记 `unavailable`；理由文本保持上游原话（不加"失败"前缀）。
用例 `test_cninfo_discovery.py` **33 用例全过**（+2：不存在代码→`no_candidates`、空结果→`no_candidates`；
传输失败仍是 `unavailable` 的既有用例未动）。

---

## C. `000711-corrected` 更正稿正文缓存补齐（在线取件 + 离线复验）

### C1. 在线取件（`scripts/a2_corrected_report_online.py`，走产品自身通道）

路径：`material_intake.store(web_link)` → `material_intake.admit` →
`net_policy.fetch_document`（严格出域校验、已验 IP 直连、不跟随重定向）。

| 字段 | 读数 |
|---|---|
| URL | `http://static.cninfo.com.cn/finalpage/2025-09-05/1224639904.PDF` |
| HTTP | 200（`egress=direct_pinned`） |
| 字节 / sha256 | 1,513,031 B / `98dc313ab7daf361418611cd56ee67d2b9744f15c9fd831e5e4f0819a8d21fc9` |
| 解析 | `pypdf`，273 页，221,044 字符，未截断 |
| 披露时间 | 2025-09-05（`day`，依据 `source_url_format`） |
| 准入 | `admitted`，小节 1263，证据条目 8，规则版本 `adm-2/f3a-1` |
| 缓存落盘 | `evals/a2_official_chain_20260929/000711-corrected/project/materials/2db15147bad934f9/{doc.json,meta.json,raw.bin}` |

未准入时**不覆盖缓存**（宁可保持缺失，也不留下来源不明的正文）——脚本里显式这么写。

### C2. 离线复验（`scripts/l1_official_facts_chain.py` 复跑）

四份材料（三家非金融公司，其中 000711 的原文与更正稿同代码**不重复计数**）：

| 材料 | 期间 | 观察（可用） | 模型 | 结论 |
|---|---|---|---|---|
| 002304 洋河股份 | 2023/2024 | 36/36 | profit_to_cash、cash_quality、profit_bridge、working_capital | 4/4 `validated` |
| 600031 三一重工 | 2023/2024 | 36/36 | 同上 | 4/4 `validated` |
| 000711 京蓝科技（原文） | 2019/2020 | 34/34 | 同上 | 4/4 `validated` |
| **000711 京蓝科技（更正后）** | 2019/2020 | 34/34 | 同上 | 4/4 `validated` |

**更正稿与原稿的差异被如实取出**（这正是"正文缺失就无法离线复验"的原因）：

| 输出 | 原稿（2021-04-27） | 更正稿（2025-09-05） |
|---|---|---|
| 净利润变化 `profit_change` | −1,318,104,774.55 | **−1,362,952,262.96** |
| 现金—利润缺口变化 | 1,466,915,446.53 | **1,511,762,934.94** |
| 毛利变化 | −421,835,117.39 | **−488,198,765.74** |
| 营运资金占用变化 | −4,104,090,908.73 | **−4,007,544,257.08** |
| 应收账款周转天数 | 403.21 | **469.20** |

两份都 `request_source=official_material`（**没有** `financials.json`，结构化 API 在脚本里不存在）。

---

## D. 本批定向验证读数

| 套件 | 结果 |
|---|---|
| `test_startup_readiness.py` | 76 全过（+3 读数 + 隔离守卫） |
| `test_cninfo_discovery.py` | 33 全过（+2） |
| `test_orchestrator_v2.py` / `test_sandbox_isolation.py` / `test_delivery_chain.py` | 全过（`discover()` 状态语义变更的回归面） |
| `scripts/check_secrets.py` | `OK: 未发现密钥泄漏` |
| `python -m py_compile`（改动文件） | 通过 |

证据文件：[a2_official_entry_online.json](a2_official_entry_online.json)、
[a2_corrected_report_online.json](a2_corrected_report_online.json)、
[l1_official_facts_chain.json](l1_official_facts_chain.json)。

---

## E. 仍未解决（不假装已解决）

- **带登录会话的页面真实点击**：本机实例已初始化（`config.json` 有两个 admin 账号且都设了密码），
  未经用户同意不得新建/重置账号，因此**仍未验**（登录页与未带会话的 401 已验）。
- **K1 付费 API 端到端实网整跑**：额度未确认，代理不批准付费试错，**仍未跑**。
- **真人 F3 五项 ≥8/10**：必须真人，**仍未验**。
- **干净 Windows 一键启动与完整交付验收**：`clean-env-e2e` 是 ubuntu + 固定模型替身，**不能替代**。
- **Q4 统计预测**：两期财报不能支撑趋势/回归，**未开放**（需架构师显式开启）。
