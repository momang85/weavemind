# S2 网络通道一致性：出口显式化、代理失败不落直连、同一套诊断类别

日期：2026-09-27。批次：搜索网络与金融资料专项 **S2 上半**（专项 §6）。承接 S1
（`s1_bounded_search_20260926.md`：检索收敛、候选五态、无候选不派发）。

本批只做"通道一致性"这一半；`health_registry` 的状态语义与实例身份（§6 第 4 条）留 S2 下半。

## 1. 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `net_policy.connection_mode()`（新） | 无：出口方式没有显式表达，只由环境变量隐式决定 | 三态显式：`inherit`（跟随部署代理，默认）/ `direct`（明确不走代理）/ `proxy_required`（要求经代理出口）。取值来自 `WM_CONTENT_FETCH_MODE` 或 `config.json` 的 `network.content_fetch.mode`；**非法值按 inherit 处理并记日志**，不把"读不到配置"当"用户要直连" |
| `net_policy.proxy_settings()`（新） | 无 | 脱敏报告环境代理（只回 host:port，不带凭据），供诊断与页面显示"当前出口" |
| `net_policy.is_proxy_failure()` / `classify_network_error()`（新） | 无 | 代理层失败判定 + 统一类别：**407、异常文本含 proxy、或"配了代理且失败在连接阶段"**（走 `URLError.reason`，因为 urllib 把底层连接错误装在那里）。"已建连后被重置"不算连接阶段失败，避免误判 |
| `net_policy.fetch_document()` | 用已验 IP 直连，但对"环境里配了代理"不置一词 | 结果里显式带 `egress="direct_pinned"` 并记日志说明未使用环境代理；`proxy_required` 模式下**拒绝**（`NetworkPolicyError`）而不是静默改道直连（本版本经代理出口明确不支持：代理侧自行解析域名，与"校验与连接同一个已验 IP"冲突） |
| `net_policy.apply_direct_mode_env()`（新） | 无 | `direct` 模式下由 launcher 在启动时清理本进程代理环境变量（只删代理变量、不改系统设置），子进程继承⇒对全部服务一致生效 |
| `adapters/transport.dual_channel_get` | urllib 失败（含代理失败）→ 降级 raw socket **直连** | 代理失败 → 抛 `ProxyEgressError`（类别 `proxy_error`），**直连次数 0**；源站侧失败仍走第二通道 |
| `adapters/transport.get_via_socket` | `socket.create_connection((host, port))`：域名二次解析 + 恒做 TLS | 走 `net_policy.connect_validated`：连**校验时解析出的那个 IP**（消除"校验后改指内网"的窗口），TLS 用系统 CA 与正确 SNI，http/https 都按 scheme 处理 |
| `adapters/transport.classify_error()`（新） + 错误对象带 `.category` | 各源错误只有自己的类型名 | 各源错误与 `net_policy` 同一套类别（`proxy_error/timeout/dns_error/policy_blocked/...`），适配器报错即可被下游按类别处置 |
| `annual_report_pdf.fetch_bytes` | 失败只打原始异常 | 记**类别**（代理/超时/DNS/策略各自可辨），处置不变（返回 None） |
| `adapters/news.py` | RSS 失败只打异常文本 | `_record_failure()` + `last_error()`：类别、通道、时间；RSS 与检索兜底两条路径都记 |
| `search_diag.classify_error` | 只看异常类型/文本 | 先判代理层失败（`proxy_error`），否则沿用原表——S0 诊断不再把"出口问题"读成"资料源不可用" |

**边界内不动的东西**：`net_policy` 的两类授权边界（登记端点 / 内容派生 URL）未放宽；
私网/元数据地址、URL 凭据、混合解析、重定向仍一律拒绝；TLS 校验未动；未新增服务端请求点
（新逻辑都挂在既有通道上）。

## 2. 最小验收逐条（专项 §6）

| 验收项 | 证据 |
|---|---|
| 代理失败后直连次数 0 | `TestTransportEgress.test_proxy_failure_never_falls_back_to_direct`：伪造 `URLError(ConnectionRefusedError)` + 设 `HTTPS_PROXY` → 抛 `ProxyEgressError`，socket 通道调用次数 **0** |
| 源站侧失败仍可用第二通道（未扩大改动） | `test_origin_failure_still_uses_the_second_channel`（`RemoteDisconnected` 不算代理失败） |
| 代理层失败判定不误伤 | `TestEgressPolicy.test_proxy_failure_detection`：407 与"配代理+拒连"为真；同种异常在**没配代理**时为假；`ValueError` 为假 |
| TLS 失败不改校验 | `test_tls_verification_is_never_disabled`：`net_policy`/`transport`/`news`/`annual_report_pdf` 里不得出现关闭校验的写法（禁用片段按拼接构造，避免断言自身命中） |
| 内容 URL 指向内网/跳转内网均拒绝 | 既有 `TestPublicUrlValidation` / `TestFetchDocumentTransport`：内网、元数据、映射地址、私网混合解析、302→`http://10.0.0.1/` 一律拒绝/不跟随（未放宽） |
| 正常公开资料正例不受影响 | `test_public_content_url_positive_case`（公网 IP 字面量，无需 DNS）+ 既有 `test_public_host_allowed`；真机正例仍由 S0 诊断的 Bing/东财通道覆盖 |
| 回环工作台保留本地访问 | `launcher._publish_loopback_no_proxy` + 健康探针的 `ProxyHandler({})` 未动；`test_startup_readiness` 全绿（本批未触碰该路径） |
| 明确不支持经代理的内容抓取时**拒绝**而非改道 | `test_proxy_required_mode_is_declared_unsupported`：`_connect_pinned` 未被调用即抛 `NetworkPolicyError`，理由写明"不支持" |
| 类别同表 | `test_news_failure_records_unified_category`（RSS → `proxy_error`）、`test_proxy_error_classifies_for_adapters`、`search_diag` 既有类别用例含 `proxy connect failed → proxy_error` |

## 3. 命令与结果

```
python -m unittest test_net_policy      # 33 OK（新增出口策略 8 例 + 传输出口纪律 3 例）
python -m unittest test_p0              # 含新闻类别 1 例（其余不变）
python -m unittest test_startup_readiness test_deploy_manifest   # 回环/启动面未受影响
```

## 4. 未做 / 未验（照实）

- **未做（S2 下半）**：`health_registry` 的 `unknown/degraded/available/unavailable`、
  `checked_at` 与过期语义、快照的实例身份（防运行包/源码实例互串），以及页面状态与
  `web_ui.source_health` 的同源收敛——按指令顺序放在本批之后。
- **未做**：`ddgs` 客户端自身的出口（库内自带 http 客户端）不受本批控制；只保证"我们传给它
  的后端名有效"（S1）与本批的通道纪律，库内请求量/代理行为仍记"未知"。
- **未做**：直接调 `get_via_urllib` 的适配器（东财、resolver、PDF 下载）在 `direct` 模式下
  仍走 urllib（其代理环境已由 launcher 启动时清理，故实际仍是直连）；若将来要逐请求判定，
  需把这些调用点改到双通道入口。
- **未验（已补，见第 5 节）**：本批代码为离线单测（替身控制流）；真机出口的包内复验在
  第 5 节补上（运行包 `2026.09.27.1`）。
- `proxy_required` 模式当前**只有拒绝语义**（无代理出口实现）；这是"做不到时明确不支持"
  的落地，不是可用功能。

## 5. 包内复验（运行包 `2026.09.27.1`，包内解释器）

包身份：`weavemind-2026.09.27.1-win-x64.zip`，sha256
`0443a425aca8a30a7ea2779304b1d3d4f8c4ac02376d7bbf05572c2cb42c8f74`（291,423,676 字节，
构建自检 secrets=0 / dev_paths=0；干净机器仍未验）。

| 复验项 | 做法（包内解释器，真 urllib） | 读数 |
|---|---|---|
| **代理失败后直连次数 0** | 设 `HTTP(S)_PROXY=http://127.0.0.1:9`（保留端口，立即拒连）后调 `dual_channel_get`；给 `get_via_socket` 加计数 | 抛 `ProxyEgressError`（`category=proxy_error`），**socket 调用 0 次**；日志明确写"代理层失败：不降级直连" |
| 正常公开资料正例不受影响 | 无代理环境抓 Bing 搜索页 | ok，99,408 字节 |
| 明确直连模式可用 | `WM_CONTENT_FETCH_MODE=direct` + 同样的死代理环境 | `apply_direct_mode_env` 清理 `HTTP_PROXY,HTTPS_PROXY`；`proxy_settings` 变为未配置；抓取仍 ok（98,659 字节）——**明确直连不受环境代理影响** |
| 健康状态语义 | `health_registry.snapshot()` | `search`/`market_source` = `unknown` 且 `ok=False`（无快照）；`llm/embedding/planner/mcp/lora` = `available`；`code_sandbox` = `unavailable`（docker 不在） |
| 前端四态随包 | 包内 `frontend/dist` 产物 | `Settings-*.js` 含"未检查""读数过期"字样（与源码同版） |
| S1 检索路径未被 S2 改动打断 | 包内 `search_diag.py` 有界探测（≤6 次调用） | Bing 10 条/0.48s、ddgs `no_results`/8.09s、东财 1 条/0.66s、公告查看页 0.38s，预算用 4/6 次——与 S1 复验同形 |

本批新增公开请求：3 次（正例 1、direct 模式 1、`search_diag` 4 次中的 4 次按该脚本自报预算口径
计入；其中 ddgs 的 HTTP 次数按惯例记"未知"）。无模型调用、无付费调用、未改用户设置。
