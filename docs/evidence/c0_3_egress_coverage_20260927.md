# C0-3 统一出口覆盖：每个内容入口执行 mode（09-27 架构行动）

日期：2026-09-27。批次：**C0-3**（指令 `docs/真实研究闭环与阶段D收口_20260927.md` §3.3、§4-C0.3）。
基线 `453f958`；C0-1 = `5107ac0`、C0-2 = `e5c5607`。

## 1. 反例 → 修法

| # | 反例（§3.3） | 修前 | 修后 |
|---|---|---|---|
| 1 | 必须代理但无代理，PDF 仍下载 1 次 | 只有 `net_policy.fetch_document` 检查 `proxy_required`；`annual_report_pdf` → `transport.get_via_urllib` 这条路上没检查 | `net_policy.require_egress_ok()` 作为**内容入口守卫**，在 `get_via_urllib` / `get_via_socket` / `dual_channel_get` 三个入口**发请求之前**调用：`proxy_required` 且不支持 → 抛 `NetworkPolicyError`（`test_proxy_required_refuses_before_any_request`：urlopen 调用数 0） |
| 2 | 配置代理后 `RemoteDisconnected`（**建连后断开**）仍触发 socket 直连 1 次 | 只有"连接阶段失败"判为代理失败，建连后断开按源站侧处理 → 降级直连 | `dual_channel_get` 先判断**代理是否在生效**（inherit 且环境配了代理）：在生效时**任何** urllib 失败都不降级直连，抛 `ProxyEgressError`（`test_proxy_in_effect_blocks_fallback_even_after_connect`：socket 调用 0 次）；代理不参与时第二通道照旧（`test_origin_failure_without_proxy_still_uses_the_second_channel`） |
| 3 | 为内容下载清掉整进程代理，连带改变其它服务出口 | 上一批在 launcher 启动时 `apply_direct_mode_env()` 清空本进程代理变量 | 删除该接口与 launcher 调用；改为 `net_policy.proxy_settings_report()`：**只报告**（mode / 是否配代理 / 脱敏 host / 是否支持代理出口），**不修改任何环境**（`test_egress_is_reported_but_process_env_is_never_mutated`，并断言旧接口已不存在） |
| 4 | requested / effective 出口不可查 | 无 | `proxy_settings_report()` 给出 requested mode 与"代理是否在生效"；`fetch_document` 结果继续带 `egress="direct_pinned"` |

保留不动（§4-C0.3 明确要求）：公网校验（`validate_public_url`）、跳转校验（不跟随重定向）、
DNS 绑定（已验 IP 连接）、TLS 验证（系统 CA + 正确 SNI）；不改用户当前设置。

## 2. 代码位置

- `net_policy.py`：新增 `require_egress_ok()`、`proxy_settings_report()`；移除
  `apply_direct_mode_env()`（连带删掉 `_PROXY_ENV_NAMES`）。
- `adapters/transport.py`：新增 `_require_egress_ok()` / `_proxy_in_effect()`；三个内容入口
  调用守卫；`dual_channel_get` 的降级条件改为"代理不参与时才允许第二通道"。
- `launcher.py`：删除启动时的代理环境清理（保留一段注释说明为什么不这么做）。

## 3. 定向验证

```
python -m unittest test_net_policy test_p0 test_startup_readiness test_delivery_chain \
                     test_search_quality_unified
# 911 tests OK（新增：proxy_required 入口拒绝、代理在生效不落直连、只报告不改环境、
#   require_egress_ok 三态；并把旧断言 test_origin_failure_still_uses_the_second_channel
#   改名为 test_origin_failure_without_proxy_still_uses_the_second_channel 且 proxy=False——
#   行为预期随策略变化，判据未放宽）
```

## 4. 未验 / 未做（照实）

- **未验**：真实代理在线/离线两种环境下的真机抓取（本批为替身；禁网替身覆盖
  "必须代理 → 拒绝"与"配置代理 → 不落直连"两条）。
- **未做**：`direct` 模式下 `get_via_urllib` 仍走 urllib（继承部署代理）——**未**逐请求改写通道。
  理由：指令禁止"为内容下载清理整进程代理"，而编辑器/安全扫描对逐请求改通道的新请求点有拦截
  记录；当前 `direct` 的语义是"不把内容下载切换到代理"，`proxy_required` 的拒绝语义已完整。
  若要 `direct` 完全绕开环境代理走已验 IP 通道，需另立一次改动并带定向验证。
- **未做**：C1（补材料入口接入编排）及之后批次。
