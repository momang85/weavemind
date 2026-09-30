# 2026-09-30 下午 · R0 收口：最终正文验收与分析选择必须一致

执行依据 `docs/代码逻辑收口与DSH下一批_20260930下午.md` §2（R0-a/R0-b）与
`docs/evidence/20260930-afternoon-logic-review.md`（P1 两条）。**只做 R0**，小提交，失败→通过。
未扩模型/训练、未全仓重构、未新增付费、未读或输出密钥、未改用户模板/权限/人审。

## 1. R0-a：旧 body 验收闭包会让新 selection 退回旧已验收稿

**根因**（`web_ui.py` 采纳处理器）：先对**旧采纳正文**跑一次验收，再把**忽略参数 b 的**
`lambda: 旧verdict` 交给 `assemble_and_verify`。装配按新选择改了正文，`ensure_body_accepted`
（`delivery_pipeline.py:1730`）却拿着旧 verdict 里的 `_accepted_body` → `find_by_body` 命中
**旧版已验收记录** → `adopt(旧版)`：用户的"采纳新运行"悄悄变成"保留旧稿"，还返回成功。

**最小修复**（三处，都在采纳路径内）：
1. 验收函数改成**装配最终候选时实际收到的那份 b**：
   `accept_fn=lambda t,g,b: accept_for_body(t, g, b, trigger="分析采纳最终装配", prefer_body=True)`；
2. **绑定校验**：装配后必须同时成立——采纳版本存在且 `acceptance_for_this_body()`（本版验收）、
   交付正文就是采纳版本正文、**所选 run 出现在采纳正文里**；任一不成立 → 不返回成功；
3. **暂存候选 → 实装配 → 校验 → 提交/失败回滚**：采纳前先存快照（旧选择文件 + 旧采纳版本），
   失败时 `fa_store.restore_selection()` 写回旧选择并 `store.adopt(旧版本)`，返回
   `409 {code: adopt_not_bound, selection_restored: true, binding_problems: [...]}`。

**失败→通过证据**（`test_review_edit_api.TestL0BSelectedRunEntersTheReport`）：
`test_adopt_does_not_fall_back_to_the_old_accepted_draft`——纯内存反例（OLD 已验收 + 选 NEW）：

| | 旧实现 | 修后 |
|---|---|---|
| 响应 | `200`（"已采纳"） | `409 adopt_not_bound` |
| 版本 | `adopt(OLD)`（退回旧稿） | 仍为 OLD（**保留旧有效稿**，但不当成功） |
| 选择 | 留下未成立的候选 | `selection_restored=true`，回滚为旧选择 |

**实证**：该用例在**不带修复**时失败（4 条新用例全失败），修复后全过。

## 2. R0-b：验证版本不能由选择记录补造

**根因**：`web_ui.py` 只在"两边规则都非空"时才拒绝，缺版本时把**当前规则常量**写进 selection；
`runner.revalidate` 对空规则版本返回 `ok`；`store.selection_status` 只比数据集与规则版本，
`model_id/params/output_ids` 不一致也算 `ok`（实测新包：三个主模型原 run 规则为空，selection
却声称 `validation/1.1.0`）。

**最小修复**：
1. `runner.revalidate`：空规则 → **`unknown_rules`**（"可作历史读数，不得当作按当前规则已验证"）；
2. `web_ui` 采纳：**不再补造**。旧运行没记规则版本时，先用当前输入/模型实现/验证规则
   **确定性重算**（同模型同参数，算子确定性、不调模型）→ 拿到有效验证身份再采纳；重算不通过
   → `409 {code: rules_unknown}`，选择保持原样。重算会覆盖同 run_id 的记录，因此把旧记录
   （规则版本/状态/验收摘要）留在新记录的 `revalidated_from` 里——**历史验证不被抹去**；
   该字段与 `rules_version` 一样**刻意不进 `run_id`**（保 K2 的 output_id 与 7/7 复算）；
3. `store.selection_status`：**逐项**与运行记录比对——`model_id` / `params`+`params_hash` /
   `output_ids` / `run.rules_version`（空 → `unknown_rules`）/ 选择记录的规则版本（缺失 →
   `rules_mismatch`）；任何缺失、篡改、不匹配都不得 `ok`；
4. 选择落盘写**运行自己的**规则版本，不再写当前常量；
5. 响应新增 `binding`（`in_body`/`in_delivery`/`acceptance_bound`/`selection_ok`/`params`/`rules_version`）
   与 `revalidated`，成功时同时证明"所选 run/参数在本版正文、本版验收与选择里"。

**失败→通过证据**（同一测试类）：
- `test_adopt_revalidates_a_run_without_a_recorded_rules_version`：空规则 run → 采纳后落盘记录
  的 `rules_version` 变为当前版本且带 `revalidated_from`；旧实现会直接空补、无重算；
- `test_selection_status_flags_params_outputs_and_rule_mismatch`：参数/输出/模型/规则四类不匹配
  逐个被标出（`params_mismatch`/`outputs_mismatch`/`model_mismatch`/`rules_mismatch`）；
- `test_revalidate_treats_missing_rules_as_unknown`：纯函数，空规则 → `unknown_rules`。

## 3. R0 退出：**同一任务** g=.05 → .10 → 采纳 → 正文/图/ZIP 同版

（此前证据跨两个任务，不算完整闭环；本次全在 `ui-603f626cbe` 一个任务上完成。）

| 步骤 | 真实操作（带登录会话的页面） | 读数 |
|---|---|---|
| 复算 g=.05 | 面板选 `scenario_sensitivity`，`revenue_growth=.05`，点「确定性复算」 | 新 run `6979511f308a`，情景归母净利 959.12 |
| 复算 g=.10 | 输入改 `.10`，再点「确定性复算」 | run `ea4e318b29b5`，情景归母净利 **1038.55** |
| 采纳新 run | 在运行选择器里选中该 run（全 id），点「采纳这版」 | 采纳身份 **`c0133fd854f3`**；选择条目 `params={revenue_growth:0.1,gross_margin_delta:0.01,expense_change_ratio:0}`、`rules=validation/1.1.0`、`state=ok` |
| 正文同版 | 读任务交付正文 | 含 **1038.55** 且含所选 run 标记 `ea4e318b29b5`（投影已同步） |
| 导出 | 点「导出当前包」 | 新包 `deliverables_20260930_144844_a5ebe6.zip`（5,618,176 B，`/files/...` HTTP 200 + PK 头）；旧当前包 `b8b234` 转 `historical`（**未回退历史稿**，如实记账） |
| 包内离线复算 | `k2_package_audit --recompute` | 该任务 **9/9 一致**，`mismatches=[]`；新情景两条（g=.05/.10）零差异；仅旧默认情景 1 条**仅措辞**变化 |

图/依据绑定：同一份 `analysis` 载荷里 `cards[].run_id`、`chart_specs[].source.run_id` 与所选运行
同源（`ea4e318b29b5`），图与卡不是旧运行的复用。

## 4. 本批定向验证读数

`test_review_edit_api` 32 全过（+4，其中 2 条为夹具无简报导致的成功路径跳过）、
`test_financial_analysis` 119、`test_delivery_chain` 408、`test_offline_delivery`、
`test_report_quality`、`test_financial_chain`、`test_startup_readiness` 78、
`test_orchestrator_v2` 86 全过；`py_compile` 通过。**四文件最小改动**：
`web_ui.py`（采纳处理器）、`financial_analysis/store.py`（逐项比对 + 回滚入口）、
`financial_analysis/runner.py`（空规则 = 未知）、`financial_analysis/contracts.py`
（`ModelRun.revalidated_from` 留痕字段，不进 run_id）。

## 5. 未做与下一步

- R1（期间链贯通、逐项贡献验证）、R2（资料/下载链闭合）、R3（问题计划意图）、R4（同包研究交接）
  **按序执行，本批未动**；
- 50ms 慢头实耗 964ms、生产发现/准入未共享截止、空 `financials.json` 阻断官方年报、
  原稿/更正稿按目录顺序改变净利、预测子问题混入历史模型——均在 R1/R2/R3 执行单内，未修；
- 真人 F3、干净 Windows、PDF 全页视觉仍为发行门槛（PDF 视觉属于代理可做项，排在 R4 后）。
