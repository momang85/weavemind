# RUNTIME-1022 第二批 P0-B：安全出口诊断（2026-10-22）

## 结论

新代码验证通过，**在线生效待受控重启**。核心变化：出口失败不再被压成"无数据/策略拒绝"
一句话，而是分成 `reserved_address` / `dns_error` / `policy_blocked` / `tls_error` /
`connect_error` / `timeout` / `real no_results`，并给出下一步——**没有放宽任何安全边界**，
也**没有改主机 DNS/代理**（人工调整说明见 `P0-B-manual-host-network.md`，停在审批边界）。

## 改动文件

| 文件 | 改动 |
|---|---|
| `net_diag.py`（新） | 只读出口诊断：`address_class()`（保留段单列：`benchmark_198_18` / `benchmark_2001_2` / `cgnat` / `private` / `loopback` / `link_local`…）、`address_advice()`（可操作建议，只建议不动手）、`_resolve_bounded()`（getaddrinfo 无超时参数 → 线程 + 超时，超时记 `dns_error` 并注明）、`classify_host()`、`egress_report()`（已登记公网端点 + 适配器既有内容主机，TTL 缓存，四态汇总）、`reset_cache_for_test()`。**不发 HTTP 请求**（无新建请求点），输出脱敏 |
| `net_policy.py` | `Decision` 新增 `category`（`ok`/`policy_blocked`/`dns_error`/`reserved_address`/`unparsable_url`）并在每个拒绝点赋值；`NetworkPolicyError`/`FetchError` 携带 `category`；连接阶段失败细分（`tls_error`/`timeout`/`connect_error`，新增 `_connect_error_category`）；`classify_network_error()` 透传自身类别；重定向 `policy_blocked`、超限 `response_too_large`、读取超时 `timeout` |
| `search_diag.py` | `ERROR_CLASSES` 增 `reserved_address`、`connect_error`（此前连接被拒归 `parse_error`，会把"连不上"误读成"解析坏了"）；`classify_error()` 相应分支 |
| `health_registry.py` | 新探针 `probe_network_egress()`（`network_egress` 条目）：四态、只解析不发请求、按 `egress_report` 的 state 折算；无目标 → `unknown`（不假装可用） |
| `web_ui.py` | 新只读接口 `GET /api/diagnostics/network`（管理员；`?refresh=1` 强制重解析），返回逐目标分类 + 当前出口方式（脱敏）；`/api/status` 的 `dependencies` 自动带上 `network_egress` |
| `test_net_diag.py`（新） | 15 个离线桩用例，见下 |
| `workspace.py`（顺带修正，P0-A 遗留） | 跨根恢复改为**只走显式登记**（`WEAVEMIND_WORKSPACE_LEGACY_ROOTS` / `roots.json`；`builtin` 哨兵才展开内建临时根扫描）。原因：ambient 扫描系统临时目录会让测试任务 id（如 `t-1`）撞上历史残留同名目录，`test_p0` 聚合用例真实跑出 `count=1`。绑定索引也改为**跟随当前工作区根**（`<根>/task_roots.json`），修掉跨测试串绑定 |
| `docs/evidence/20261022-runtime-repair/verify_history.py` | 显式登记三个历史根 + `builtin`（复核口径不变，结果与第一批一致） |

## 定向测试与真实退出码

| 命令 | 结果 | 退出码 |
|---|---|---|
| `python test_net_diag.py` | 15/15 OK | 0 |
| `python test_p0.py` | 438/438 OK（含聚合用例；这是本次真 bug 的回归） | 0 |
| `python test_net_policy.py` | 51/51 OK | 0 |
| `python test_workspace_addressing.py` | 16/16 OK | 0 |
| `python test_task_persistence.py` | 51/51 OK | 0 |
| `python test_data_source_settings.py` / `test_search_quality_unified.py`（80）/ `test_report_quality.py` / `test_working_paper.py` / `test_delivery_chain.py` | 全 OK | 0 |
| `python test_auth_audit.py` | OK（管理员门限改动后） | 0 |
| `python -m py_compile web_ui.py net_policy.py net_diag.py health_registry.py search_diag.py workspace.py` | 通过 | 0 |
| `python docs/evidence/20261022-runtime-repair/verify_history.py` | 5/5 定位一致，`mismatch=[]` | 0 |

## 离线桩覆盖（`test_net_diag.py`）

1. **策略仍拒绝保留地址**：`198.18.0.88`、`2001:2::53`（桩解析）+ 内容通道
   `fetch_document` 抛 `NetworkPolicyError(category="reserved_address")` 且**不建立连接**；
   另覆盖环回/私网/链路本地/CGNAT/元数据服务/URL 凭据/非法协议。
2. **允许的公网出口仍用已验 IP + 校验证书**：桩连接记录目标 = 解析出的 IP（不二次解析），
   `verify_mode=CERT_REQUIRED`、`check_hostname=True`、`SNI=主机名`；
   登记服务通道同样走已验 IP。
3. **凭据通道不混用**：内容派生通道剥掉 `Authorization`/`Cookie`/`X-Api-Key`（保留非凭据头），
   登记服务通道携带自己的鉴权头。
4. **分类与健康不假装**：词表单源（`net_diag.CATEGORIES = search_diag.ERROR_CLASSES ∪ APP_ONLY`）、
   地址类别表、`classify_host` 四情形、`egress_report` 的 ok/degraded/unavailable/unknown、
   报告不含密钥标记、健康探针四态映射、诊断主机清单与 adapters 源码一致（防漂移）。

## 真实观测（如实记录）

- 本机当前会话的 DNS 解析：`www.cninfo.com.cn`、`static.cninfo.com.cn`、`www.bing.com`、
  `datacenter-web.eastmoney.com`、`push2.eastmoney.com` **全部解析到公网地址**
  （`net_diag.egress_report()` → `state=ok, counts={'ok': 5}`）。
- 诊断快照（另一环境/另一时刻）解析到 `198.18.0.x`/`2001:2::x`。两者不矛盾：fake-IP
  是否生效取决于当时哪台环境/哪个网络栈在解析。**分类器对两种情形都给对类别**，
  测试用桩锁定保留段分支。
- 因此"线上现在是否仍被 fake-IP 挡住"**不能用本次证据断言**；受控重启后用
  `GET /api/diagnostics/network?refresh=1` 实测为准。

## 与任务书第二批对照

- 分类（解析失败/保留地址/策略拒绝/TLS/连接/真实零结果）✅ 落地为同一套类别词，
  并接到健康与设置诊断接口；不统称"无数据"，不误报密钥问题。
- 离线桩测试：策略仍拒保留地址、放行出口仍用已验 IP + 校验证书、两类凭据通道不混用 ✅。
- 主机真实 DNS/可信出口配置：给最小人工说明 + 复验步骤，**停在审批边界** ✅
  （`P0-B-manual-host-network.md`）。
- 未改系统 DNS/代理/TUN、未放行保留网段、未关 SSRF/TLS/沙箱、未改凭据 ✅。

## 剩余风险

1. ~~`/api/status` 拖慢风险~~ **已收口**：`egress_report` 有整体时间预算
   （`WM_EGRESS_DIAG_BUDGET`，默认 8s，超了不再解析并如实标注"未探测"）+ 单目标
   2s 超时 + 60s TTL 缓存；本机实测一次全量解析 5 目标 0.05s。
2. 诊断接口返回主机名与 IP 类别（管理员可见），不含凭据与完整配置。
3. 在线实例未载入新代码；重启前建议显式登记历史根（runbook 已给命令）。
