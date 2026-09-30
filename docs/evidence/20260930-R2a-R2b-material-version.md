# 2026-09-30 下午 · R2 前半：资料链真闭合（版本裁决 + 空载荷不阻断官方原文）

执行依据 `docs/代码逻辑收口与DSH下一批_20260930下午.md` §4 R2。本批只做**资料链两条**（R2-a/R2-b）；
R2-c（50ms 慢头有界终止）与 R2-d（生产发现/准入共享总截止）留下一批——见 §5。

## 1. R2-a：原稿/更正稿必须按**披露版本**裁决，目录顺序不得决定金融结论

**根因**（`working_paper_export._official_material_facts`）：逐份材料抽事实，抽到第一份合格的就
`break`。同一次研究里若同时存在原稿与更正稿，**哪份先出现在索引/目录里就决定读数**。复核已用
真实京蓝缓存复现：同一 `as_of`，仅调换索引顺序，2020 净利从 **−2,354,850,607.11** 变成
**−2,399,698,095.52** 元。

**最小修复**：去掉 `break`，改成两段式——

1. **逐份抽取**（不提前停）：每份已准入材料各自抽事实，记录 `material_id/title/披露日/是否更正稿`；
   晚于 `as_of` 的仍然一条不取（严格历史时点只用当时披露版本）。
2. **版本裁决**：按 `(披露日, 是否更正稿, material_id)` **确定性排序**后逐版本合并，同一
   `(指标, 期间, 口径)` 由**披露更晚的版本**决定；两版数值不同 → 记
   `版本差异（指标 期间）：<材料 A>（披露 …）=a vs <材料 B>（披露 …，更正/修订稿）=b；按披露更晚的版本取值（目录顺序不参与裁决）`；
   数值一致且新的是更正稿 → 记"更正/修订稿与原稿一致"。
3. **互相补足**：不再"一份就够"——单份材料缺某一期/某字段时，继续用其它已准入材料补齐，
   并在 notes 写明"多份已准入材料共同供数（缺期/缺字段互相补足）：<ids>"。

**失败→通过**（`TestL1OfficialMaterialFeedsFacts.test_report_version_arbitration_ignores_index_order`）：
同一材料集（原稿 2025-04-03 / 更正稿 2025-04-20，2024 净利 6,673,000,000 与 6,873,000,000），
索引顺序 `[原稿, 更正稿]` 与 `[更正稿, 原稿]` 两种排法：修前两种顺序给出**不同**读数，
修后**完全相同**且都取更正稿的 6,873,000,000，notes 含"版本差异"与"更正"。

## 2. R2-b：空/错误的 `financials.json` 不得阻断官方年报退路

**根因**（`working_paper_export.build_result`）：只看 `fin_path.exists()`。文件存在但内容为
`{"error":"upstream unavailable","data":[]}` 时直接走结构化分支 → `rows=0`、`paper_ok=false`，
**官方原文那条路一次都没被调用**（复核实测 helper 调用 0 次）。

**最小修复**：新增 `_financials_payload_usable(payload)`——**按内容**判：不是对象 / 带 `error` /
没有 `financials` 行 / 解析后无可用事实 → 都算不可用。不可用时**落到官方原文路径**，并把
"结构化财务载荷未采用：<原因>" 写进 `official_notes`（读者能分清"没有"与"坏了"）；
两条路都不行才 `skipped`，且 `reason` 同时给出结构化载荷不可用的原因。

**失败→通过**（`..._empty_financials_does_not_block_the_official_route`）：已准入官方年报 +
错误载荷 → 修前 `ok=False/skipped`，修后 `ok=True`、`request_source=official_material`、
`rows>0` 且 notes 说明未采用原因。

## 3. 定向验证读数

新增 2 条用例（不带修复时**都失败**）。回归：`test_financial_analysis` **131**、
`test_working_paper` 61、`test_delivery_chain` 408、`test_offline_delivery`、`test_report_quality`、
`test_financial_chain`、`test_facts`、`test_fact_fidelity` 全过；`py_compile` 通过。
K2 两包离线复算不变（9/9、7/7）。

## 4. 本批未做（不假装完成）

- **R2-c 50ms 慢头的**有界终止**：现状是"读完响应头再查钟"→ 事后判定（实耗 ≈964ms/预算 50ms，
  测试容许 3s）。真正有界需要把字节通道从 `urllib.request.urlopen` 换成 **`http.client` + 逐读
  `socket.settimeout(剩余预算)`**（连接建立后立即收紧超时，头阶段也受同一个总截止约束），
  并同步改写真替身夹具（现有慢头夹具是 patch `urlopen` 的）——属下一批的独立改动。
- **R2-d 生产发现/准入共享总截止**：`orchestrator_v2` 官方发现的 `_discovery_fetch=None` 让包装
  返回 None（内部真实取件未受控），`mi.admit` 另起下载预算；需要把"发现 → 取件 → 准入"接到
  **同一个剩余总截止**，且 0/不足最小请求预算不得抬高到 0.5s 继续。
- R3（问题计划意图）、R4（同包交接与 PDF 视觉）按序在后。
