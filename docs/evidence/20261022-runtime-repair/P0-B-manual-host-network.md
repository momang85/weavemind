# 主机网络人工调整说明（P0-B，停在审批边界，2026-10-22）

> 本文件是**给操作者的最小调整说明**，应用侧没有也不会自动执行其中任何一步。
> 应用侧不做的事：改系统 DNS、改 hosts、动代理/TUN、放行 198.18.0.0/15 或 2001:2::/48、
> 关闭 SSRF/TLS 校验。这些都属于主机/网络层，必须由人决定并操作。

## 现象（诊断快照 20261022）

系统 DNS 把 `www.cninfo.com.cn`、`static.cninfo.com.cn`、`www.bing.com`、
`datacenter-web.eastmoney.com` 等解析到 `198.18.0.x` 与 `2001:2::x`，全部落在
**测试保留段**（RFC 2544 / RFC 5180），织光按 SSRF 防线拒绝该地址，检索/披露抓取
因此拿不到资料。这是 fake-IP 网络模式的典型特征——**最常见**的解释是本机有
代理/TUN 类工具接管了 DNS（如 Clash/Mihomo 系的 fake-ip 模式），但这只是特征推断，
不是对某个具体软件的指认。

注意：DSH 工具会话（本仓库开发环境）实测解析到的是公网地址——fake-IP 映射是否
存在取决于**当时哪台环境/哪个网络栈**在解析，不是恒定事实。以 `/api/diagnostics/network`
的实时分类为准。

## 应用侧现在能告诉你的（无需人工）

- `/api/status` → `network_egress` 条目：四态（available/degraded/unavailable/unknown），
  汇总"哪个目标、落在哪个保留段"；
- `GET /api/diagnostics/network`（管理员登录）：逐目标 DNS 分类 + 下一步建议 +
  当前出口方式（脱敏）；
- 研究过程中的网络失败现在带类别（`reserved_address` / `dns_error` / `tls_error` /
  `connect_error` / `timeout` / `policy_blocked`），不再统称"无数据"，也不会误报成密钥问题。

## 若确认要恢复真实解析（人工二选一）

**方案 A（推荐，改动最小）**：在代理/TUN 工具里把研究目标域名加入**直连规则**，
或关闭其 fake-ip 模式，让系统 DNS 返回真实公网地址。覆盖名单至少包括：
`www.cninfo.com.cn`、`static.cninfo.com.cn`、`www.bing.com`、
`datacenter-web.eastmoney.com`、`push2.eastmoney.com`（以及实际使用中的模型/API 域名）。

**方案 B**：保持代理工具不动，由部署方确认代理对织光所在进程提供真实出口。
注意：织光的内容抓取通道本版本**明确不支持经代理出口**（代理侧自行解析域名，与
"校验与连接共用同一个已验 IP"冲突，宁可拒绝不静默改道）；方案 B 实际指"让系统 DNS
恢复正常解析"，而不是"给应用配代理"。

**不要做**：放行 198.18.0.0/15 / 2001:2::/48（等于给 SSRF 防线开洞）、改 hosts 硬指、
关闭证书校验。这些都会把"连不上"变成"连到错误的地方"。

## 复验步骤（调整完成后）

1. `GET /api/diagnostics/network?refresh=1` → 期望全部目标 `ok`（解析到公网地址）；
2. `/api/status` → `network_egress` 变为 available；
3. `python search_diag.py --json > 出口复核.json`（在运行包内执行）→ 检索/披露通道
   不再出现 `reserved_address`；
4. 重跑一个新研究任务，确认资料进入证据链（这一步才会真正验证端到端）。

## 在线生效状态

以上探针与分类**随下次受控重启**在在线实例（8080，PID 24572）生效；当前在线实例
仍运行旧代码，其表现是旧的"无数据/策略拒绝"。重启属于受控操作，不在本工单内执行。
