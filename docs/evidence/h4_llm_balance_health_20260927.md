# H4：端点欠费时健康页报"可用"（假绿）——诊断面修复与余额门禁现状（2026-09-27）

## 触发

C4 样本①（洋河 002304.SZ）按计划用页面同一入口提交，被拒：

```
{"error": "全部 LLM 端点余额不足，请充值后重试"}   HTTP 503
```

而**同一时刻** `/api/status` 的 `health.items[llm]` 写的是
`state=available / label=可用 / detail=primary=ok, backup=ok`，`llm_warning` 为空。
两个信号互相矛盾——这正是让"环境看起来正常"的原因。

## 实测证据（每步可复现）

1. **强制重探**（绕过 600s 冷却）`get_balance_status(use_cache=False)`：
   elapsed **0.2 秒**，主备都返回 `{"ok": false, "reason": "insufficient_balance"}`。
2. **原始异常**（直接调 `LLMClient._send_request`）：
   `LLMCallError: HTTP 402: {"code":"INSUFFICIENT_BALANCE","message":"余额不足",
   "data":{"retryAfterSeconds":12},"traceId":"trace_0256733b…"}`——主、备各一次，
   base_url 都是 `https://tokenrhythm.studio/v1`。
3. **归类函数黑盒**：`HTTP 402` → `insufficient_balance`（正确，**不是误判**）。
4. **402 是否可自愈**：92 秒内每 10 秒探一次，**10/10 全部失败**；
   `retryAfterSeconds` 呈 50→40→29→19→9 后重置为 50（每分钟滚动的倒计时窗口），
   连窗口边界那次也失败 → **不是限流，是余额/额度真的耗尽**。
5. **同一份响应的自相矛盾**（实测原样）：
   ```
   health.items[llm]  → state:"available"  label:"可用"  detail:"primary=ok, backup=ok"
   llm_health.balance → primary:{ok:false, reason:"insufficient_balance"}
                        backup :{ok:false, reason:"insufficient_balance"}
   llm_health.primary → healthy:true, fails:1, last_degradation_reason:"insufficient_balance"
   llm_warning        → ""
   diversity.reason   → "same_host"
   ```

## 根因

`health_registry.probe_llm()`（`health_registry.py:171`）只看
`_endpoint_health[*].healthy`，而 `healthy` 是**带失败阈值**的瞬时状态：一次 402 只写
`fails=1` + `last_degradation_reason`，`healthy` 仍为 `true`，于是"账户欠费"被报成"可用"。
同一个函数在状态读不出来时还回 `ok=True`（读不到也说可用），属同一类假绿。

## 修复（只动诊断面，不碰门禁）

- **新增 `llm_client.balance_terminal_reason()`**：主备**都**处于终态失败
  （`insufficient_balance` / `unauthorized`）时返回该原因，否则空串；
  `unreachable`（可能自愈）与未探测都**不算**终态。只读缓存结论、不额外打端点——
  与 `/api/status` 读的是同一份 `get_balance_status()` 缓存，两边因此不会再各说各话。
- **`health_registry.probe_llm()`**：合并终态结论——有它就不再报可用，`reason` 给可读说明，
  `detail` 追加 `balance=<reason>`；`healthy` 仍如实保留在 detail 里**（不隐藏瞬时状态，
  只是不让它盖过终态结论）**。
- **异常分支**：`ok=True` → `_unknown_entry(...)`（读不到 = unknown，不是可用）。

## 明确**没有**改（属门禁，需用户决定）

- `web_ui._post_task` 的余额预检**语义不变**（主备都终态失败仍然拒绝，仍回 503）；
- `_BALANCE_COOLDOWN = 600s` **不变**：判定终态失败后 10 分钟内不再重探。
  代价是**充值后最长要等 10 分钟**才恢复接单——这是既有的设计取舍（避免每 30 秒白打欠费端点），
  改动属门禁行为，交用户决定，本批不动。
- 未改 `templates.json`、模型、权限、代理。

## 反例与最小验证

- **反例（修复前）**：同一响应里 `llm` 项 `state=available` 而 `balance.ok=false`。
- **最小验证**：`python -m unittest test_p0.TestHealthRegistryAndAlertDedupe` → **13 项通过**，
  其中 4 项为本批新增：
  ①欠费时不得报可用（且 `detail` 带 `balance=`）；
  ②无终态结论时保持可用（不误报正常环境）；
  ③状态读不出来是 `unknown`；
  ④终态判定只在**两端都终态**时成立（一端可用、或不可达都不算）。

## 未验项（如实留白）

- **未在"余额正常"的真实账户上做正向验证**（本机无余额，无法构造）：
  `probe_llm` 在正常环境的行为只有 mock 覆盖，**真机正向未验**。
- **未做真实浏览器点击式 E2E**：本会话 `browser_evaluate` / `browser_opencli_run`
  一律被会话审批策略自动拒绝（`automation mode: standard`，`page evaluate: ask`），
  浏览器侧无法驱动；样本①走的是**页面同一入口的 HTTP 调用**
  （`POST /api/login` → `POST /task`，载荷与页面 `buildResearchGoal()` 的产物逐字段一致）。
- OpenCLI 扩展仍未在 Edge 加载（`opencli doctor` → `Extension: not connected`），故未使用桥。
- `diversity.reason = "same_host"`：主备**同主机**（都是 `tokenrhythm.studio`），
  "双端点余额预检"实为同一厂商查两次，**备用端点不提供冗余**。已记录，未改（超出本批范围）。

## 结论：C4 样本①被外部付费能力阻塞

- 提交被 503 拒绝，**任务未创建、未执行、未产生任何研究产物**（无 id、无工作区）。
- 探针成本：所有探测请求一律被 HTTP 402 拒绝、不消耗 token，但按
  `c4_sample_plan_20260927.md` §4 的口径，费用仍记 **未知**（不填 0）。
- 三份样本**不需要再改代码**，账户余额恢复后即可按原计划开跑。
