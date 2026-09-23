# 第二批证据：导出缺陷与快照一致性（2026-09-23 日间）

指令：`docs/阶段D交付真实性复验与执行_20260923.md` 第二批（D/E/F）。基线 `279dce4`。
边界：无新增 HTTP、无付费模型；未改模型/权限/模板/全局 0/0/0；用户脏文件未动。

## D. 首页结论不再落到纸外（逐行换页）

- 根因确认：`para`/`quote` 只在**段首**检查一次空间，随后逐行画到底。包内 PDF
  `a5687c87…` 实测第 1 页有字符框 **bottom = −46.6pt**（页下边界之外），与复核一致。
- 修复：新增 `_draw_wrapped`（**每行**查剩余空间，长段落/长引用可拆页），para 与 quote
  都改走它；字号、内容都没动。
- 验证（同一份交付正文重新渲染）：内容流文字定位 `min_y=25.2`（页脚），页脚线以下
  文字 op 仅 11 个 = 11 页页脚；**0 个越界字符**。页数 9→11（长段拆页的代价，可接受）。
- 定向用例 `test_report_quality.TestPdfPagination`：长结论段 + 长引用 → 断言
  `min_y ≥ 20`、页脚线以下不超过页脚行数、内容必须分页。

## E. 重包绑定一次快照（不静默混版）+ 原子发布 + 规则指纹失效

- 新增 `delivery_pipeline.export_snapshot`：一次捕获**采纳身份 + 交付正文 + 资料/规则
  指纹 + 图表 hash**；`_post_task_package` 的 MD/PDF/底稿/清单全部由该快照生成
  （此前 MD、PDF 各自重读"当前版本"）。
- 发布前复核采纳身份：期间发生修订 → `RuntimeError("version changed")` → 接口返回
  **409 + retry**，不发布混版包。
- 包名 = 时间戳 + 随机后缀（`deliverables_YYYYmmdd_HHMMSS_<6hex>.zip`），写临时文件后
  `os.replace` **原子发布**；同秒两次重包生成两个包、互不覆盖、不留 `.tmp`。
- `rules_fingerprint` 入绑定并参与比较：`_research_binding` 保存、`state_is_current`
  比较、`staleness_reason` 报"验收规则内容变化"；读端（`_research_state_for`）另外核对
  **当前**资料指纹与规则身份（结构未重建时也能发现资料/规则已变）。
- 定向用例：`test_export_snapshot_rejects_interleaved_revision`（A→修订切B→拒绝发布）、
  `test_same_second_repacks_do_not_overwrite`、`test_rules_fingerprint_change_invalidates_state`。
- 实测（正常页面）：修订 `7ae1f710`→`1989fa2e` 后，旧绑定缺 `rules_fingerprint` →
  读端如实判"验收规则内容变化（指纹 空 → 2ddecc9a）：待重验"；重新装配后
  `stale=false`、绑定含 `rules_fingerprint=2ddecc9a`、`evidence_fingerprint=ba446104`。

## F. 包内最小引用证据 + api_chunk 坐标口径

- 新增 `evidence/citation_evidence.json`（schema `weavemind.citation_evidence/1`）：
  只收**已准入、带定位**的记录（本任务 8 条），每条给 `url / locator / chunk /
  doc_char_start-end / chunk_char_start-end / crosses_chunk / text / text_sha256 /
  admission`，并附 `chunk_offsets` 映射表。
- 坐标口径写明：`locator` 的字符区间是**合并文档偏移**（本任务 6 段公告文本拼接，
  每段 5000 字符），`chunk_char_*` 为**段内**偏移；跨片段区间标 `crosses_chunk=true`
  （避免"段内 3996-194"这种倒挂读法）；没有 PDF 页码映射就不写页码。
- `narrative_evidence.build` 的载荷新增 `chunk_offsets`（文档偏移 → 片段号）。
- 实测：chunk 2 记录段内偏移与文档偏移一致；chunk 3 记录（doc 7476-7855）正确换算为
  段内 2475-2854；跨段记录标 `crosses_chunk=true`。

## 正常页面一条链复验（采纳 → 重包）

| 读数 | 值 |
|---|---|
| 采纳 | `7ae1f710`（第一批候选）→ `1989fa2e`（可读性小修：把"单位：元 …"表格倾倒改成销售模式/地区收入构成摘要，保留数据与出处） |
| 研究状态 | `stale=false`；必答 **1/3**（收入=背景依据；利润、现金待补）；绑定含契约/资料/规则三指纹 |
| 交付 MD | 10,638 字符；含"初步背景依据""只含读数/背景""洋河股份:2024年年度报告""api_chunk 3" |
| 最终包 | `deliverables_20260923_134459_289ccf.zip`：14 个成员（MD/PDF/底稿/6 图/审计稿/**引用证据**/包内清单）；页面回报"包内字节自检 全部一致" |
| 清单 | `research_body_sha256=1989fa2e…`、`delivered_md_sha256=259e1e60…`、`pdf_sha256=23451b87…`、`rules_fingerprint=2ddecc9a`、`evidence_fingerprint=ba446104`；MD/PDF 字节与清单一致 |
| PDF | 11 页；`min_y=25.2`、页脚线以下仅页脚 → 无越界字符 |
| 旧包 | 5 个包全保留（`20260922_120247` / `20260923_011430` / `_014753` / `_134027_e79e09` / `_134459_289ccf`），时间与标识未改 |

## 测试

`test_delivery_chain` **330 通过**（新增 3 例）；`test_report_quality`（含新分页用例）、
`test_narrative_evidence`、`test_acceptance_adversarial` 共 101 通过。
顺带修一处**既有**不稳定用例：`test_build_is_deterministic` 逐字比对记录时把构建时刻
`fetched_at` 也算进去，跨秒即失败——改为比对内容字段（并加 chunk_offsets 比较）。

## 未验项（如实）

- 未做并发修订的**真实**交错（用受控内存探针/用例覆盖；未起并发付费任务）。
- 5 页主文维持（未为压页删内容）；第 3 页留白与图注分页在逐行换页后自然缓解，未逐页复核。
- 真实 Redis 多进程、真人评分仍未验；引用证据只覆盖已准入记录，未覆盖被排除来源。
