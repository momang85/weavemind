# 第二批证据：正常修订后研究状态与下载包同版（2026-09-23）

指令：`docs/阶段D正常交付闭环执行指令_20260923.md` 第二批。服务以本批代码重启（HEAD 提交见文末）。
边界：无模型、无付费实机；未改模型/权限/全局 0/0/0/模板。

## 1. 任务真实契约同源（写端不再只回读旧绑定）

- `web_ui._post_task_review_edit`：契约优先从**任务记录**解析（`_contract_wire_for`），
  解析不出才退回旧 `binding.contract`，并在日志里注明来源。
- `delivery_pipeline.assemble_and_verify`：契约入参三种形状都认（`{"wire": …}`、
  `{"contract": …}`、**扁平 wire**）——此前传扁平 wire 会静默丢契约。
- 实测（页面修订后）：`research_state.binding.contract_fingerprint = a1b9a2f5…`，
  `binding.contract` 带主体/期间/口径/as_of/必需指标/视角原文（洋河股份 002304.SZ /
  2023–2024 / 合并 / 2025-04-30 / bank_corporate / revenue+net_profit+operating_cashflow）。

## 2. 投影与采纳正文同源 + 失效原因区分

- `report_brief.build_structure` 新增：`source_body_sha256`（投影按哪版正文建的）、
  `evidence.fingerprint`（**资料指纹**：只由证据记录的身份字段算出，不掺正文）、
  `rules_version` / `rules_fingerprint`。
- `assemble_and_verify`：`ensure_body_accepted` 之后按**最终采纳正文**处理投影——
  来源就是采纳正文 → 重盖版本号；来源是别的正文 → 按采纳正文**重建投影**。
- `delivery_pipeline.staleness_reason`：失效原因逐项说清（正文不同版 / 结构按旧正文重建 /
  旧格式结构 / 资料指纹变化 / 规则版本变化 / 契约缺失或变化）。
- 页面与清单读端统一走 `_research_state_for`（`/task` 的 `research.research_state` 与
  导出清单本体），不再直接回读文件里的旧 stale/reason。
- 实测（旧结构，未重建前）：页面显示
  「结构投影没有记录来源正文（旧格式）…；绑定里缺契约指纹；执行契约变化：待重验」——
  不再是笼统的"结构≠正文"。

## 3. 资料准入链：已保存公告文本进入任务（只支持收入）

- `narrative_evidence`：新增 `chunk_offsets` 支持（无页码映射时定位写
  `api_chunk K（字符 a-b）`）；`classify` 增加"概述/分析类叶子 + 具体分析父级 + 因果语言"
  规则（此前"四、主营业务分析 > 1、概述"判 `kind=None`，发行人收入解释进不了证据集）；
  段落回退同样支持 api_chunk。
- 新脚本 `scripts/admit_saved_material_20260923.py`（离线、无模型）：把
  `evals/real/yanghe_ar2024_pages_20260922.json` 的 6 段合并为一个文档写入任务
  `project/fetch_snapshot.json`（按 URL 幂等、带 `chunk_offsets`），再走
  `narrative_evidence.build`（产品同一条链）。
- 实测（任务 ui-706c5ef4a5）：记录 8 条、已准入/已定位 **8 条**（此前 0），
  类别 business_background 4 / change_explanation 1 / footnote 2 / risk 1，
  `missing_kinds=[]`；定位样例 `api_chunk 3 · 小节：…四、主营业务分析 > 1、概述（字符 9607-10820）`。
  - **收入解释 1 条**（发行人原文，api_chunk 3）；**利润原因 0 条、现金原因 0 条** → 保持缺口。
  - 读数落 `docs/evidence/_batch2_material_admission_20260923.json`。

## 4. 正常"重新导出"：新 ZIP 与版本身份区分

- `delivery_pipeline.package_manifest`（schema `weavemind.package/2`）：显式区分
  `report_version_id` / `research_body_sha256`（**采纳身份**，陈旧判定用它）/
  `delivered_md_sha256` / `pdf_sha256` / 逐文件 `files` hash；不再写会被误读的 `body_sha256`。
  `workers/packaging_worker._package_manifest` 改为委托同一实现（老路径同 schema）。
- `delivery_pipeline.repack_adopted`：按当前采纳版本重打包（无模型、确定性），
  包内 MD 与页面下载同源；打包后**复算包内每个成员 hash** 并回报 `verify`。
- 新接口 `POST /api/task/<id>/package`（登录会话；终态任务 409）；前端"导出与复核"块
  新增「按当前版本重新导出」按钮（`frontend/dist` 已重建并随提交）。
- 读端：`_export_payload` 按 `research_body_sha256`（兼容旧包 `body_sha256`）判陈旧，
  并新增 `package_report_version_id` / `package_delivered_md_sha256` / `package_pdf_sha256`。

## 5. 正常页面一条链（修订→重验→状态→MD/PDF→新 ZIP）

任务 `ui-706c5ef4a5`，全程页面点击（表单 → `POST /api/task/<id>/review/edit` → 重新导出按钮）：

| 步骤 | 读数 |
|---|---|
| 修订内容 | 清掉正文里 4 处**孤立 `**` 标记**（原稿残留，逐行核对后已无奇数个 `**` 的行） |
| 新版本 | `37e0657a42d8`（父 `5d0116b67c2f`）；机器验收 pass（绑定本版）；交付已绑定 |
| 研究状态 | `stale=false`；`located=8`；必答 **2/3**（收入、利润有依据；现金缺）；32 条肯定结论未证实 → 仍 `research_draft`（草稿状态与版本失效是两件事） |
| 绑定 | `structure_version_id == 37e0657a42d8`（与采纳正文同源）；契约/资料/规则指纹齐全 |
| 导出 | MD `ad749945688cb066`（36,917B）、PDF `30257407a04a8f8a`（5,582,427B）；响应头 `x-report-research-body-sha256=37e0657a…` |
| 重新导出 | `deliverables_20260923_011430.zip`（11 个成员：MD/PDF/底稿 json+csv/6 图/包内清单）；页面回报"包内字节自检 全部一致"；`package_stale=false` |
| 同源核对 | 包内 `reports/report.md` == 页面 MD 导出字节；包内 PDF == 页面 PDF 导出字节；包内 `research_body_sha256=37e0657a…` == 当前采纳 |
| 旧包 | `deliverables_20260922_120247.zip` 保留（时间与标识未改），陈旧提示在重新导出后消失 |

## 6. 定向测试

| 文件 | 结果 |
|---|---|
| `test_delivery_chain`（含新增 repack 1 例、失效原因 3 断言、schema2 断言） | **325 通过** |
| `test_narrative_evidence`（含 api_chunk 2 例、概述分类） | 45 通过 |
| `test_root_budget` | 76 通过 |
| `test_financial_chain` / `test_report_quality` / `test_offline_delivery` / `test_r0_boundaries` | 34 / 32+ / 全部通过 |

## 7. 未验项（如实）

- 真实 Redis 多进程仍未验（本批用内存假后端/单进程）；真人研究员评分未做。
- 资料准入是**离线重入**（把已抓取文本写回快照再走正常链），没有新增 HTTP；
  抓取→准入在真实任务里的正向全链仍未跑（需付费实机，按指令前置未过不新增）。
- 主文去重（第三批）未做：当前 15 页报告仍有重复主文；本轮只清了孤立 `**`。
- 修订只走了一次（去星号），未做"资料/契约/规则变化的负例"页面实测；
  失效原因区分在 `test_delivery_chain` 里用生产函数断言（资料/规则/契约三路 + 重建恢复）。
