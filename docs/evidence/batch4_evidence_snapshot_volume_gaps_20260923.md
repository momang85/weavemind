# 09-23 第四轮：统一证据判断 / 真导出快照 / 量价分析 / 证据连续性（实机复核四项）

**任务**：`ui-706c5ef4a5`（洋河股份 2023–2024，银行对公视角，资料截至 2025-04-30）
**本轮改动**：`6f2c004d1b52`（采纳身份 `9b9ad5bc6017…`，父 `1989fa2ec508`）；包 `deliverables_20260923_195700_d4afd2.zip`
**范围**：无新增 HTTP 抓取、无付费模型生成；材料为已保存的公告片段（离线重入）。

## 1. 统一证据判断（风险章节不再凭背景/读数写"解释已取得"）

**反例（复核点名）**：同一份正文里，研究问题写"利润原因待证"，风险章节却写"归母净利润：解释已取得"；
收入只拿到"行业进入存量竞争、公司调整经营策略"这一段，却被算成必答问题"已支持"。

**根因**：`_change_explanation` 用 `bool(matched)` 判"有没有解释"，而匹配器现在会返回
`explanation / background / reading` 三种；背景与读数同样为真 → 风险章节照写"解释已取得"。
逐问支持处则按 `match_kind` 判，两处判据不同源。

**修复**（`report_brief.py`）：
- `unproven` 三态与逐问支持**同一条判据**（`match_kind`）：只有 `explanation` 才写"解释已取得（…），但量价与贡献程度未核实"；
  `background` → "未取得该指标变化的解释；仅有行业/市场背景（定位），不构成量价/结构解释"；
  `reading` → "未取得该指标变化的解释；已取材料只含该指标读数（定位）"；无匹配 → "变化原因尚不能证明"。
- 逐问支持：`background` **不再**置 `has_evidence`（不计入必答分子），改为 `background_only` + "已取材料：…仅行业/市场背景"。
- 『变化解释』块给每条披露加**归属行**（该段解释了哪项指标；不解释任何一项时写明"不构成原因支持"），
  判据与逐问支持、风险条目共用。

**实机结果**：必答问题 1/3（收入按量价/结构数据支持；利润、现金仍为缺口），
研究状态横幅仍是"研究草稿／待补原始披露——必答问题缺可定位依据（2/3）"；
风险章节现为：利润"未取得该指标变化的解释；已取材料只含该指标读数"、现金"变化原因尚不能证明"、
收入"已取得量价/结构数据（…），但各因素贡献度未拆分；定性原因仍只有行业/市场背景"。

**定向测试**：`TestResearchQuestions.test_background_alone_does_not_support_the_revenue_question`
（背景不计入必答，0/3）、`test_risk_section_wording_follows_match_kind`（三态措辞）、
`TestRealMdnaPositivePath.test_real_management_explanation_enters_the_brief`（真实年报片段只作背景，
风险章节不得写"解释已取得"）。

## 2. 真导出快照（图表/底稿/证据/审计稿按字节冻结）

**反例**：版本检查通过（采纳身份没变）**不代表整包同版**——快照只记了图表 hash，打包时按名字重读磁盘：
导出期间重渲染的图、重写的底稿、重跑的证据文件照样进包，清单还给这些"新字节"记 hash。

**修复**（`delivery_pipeline.py` + `web_ui.py`）：
- `export_snapshot` 新增 `_freeze_payload`：正文/PDF/底稿（json+csv）/图表/审计模型稿/
  **引用证据**全部读成字节冻结；引用证据由**快照时刻读到的资料**生成一次（`citation_evidence_payload(material=…)`）。
- `repack_adopted(snapshot=…)` 只写快照字节，**不再 glob 磁盘**；清单由 `_manifest_from_frozen`
  按写入字节算 hash，并带 `frozen`（快照 hash）与 `drift`（快照后磁盘变过的成员，只报告）。
- 自检 `verify` 同时对照清单 hash 与**快照 hash**（`snapshot_mismatch` 单列）。
- 页面响应新增 `drift` / `frozen_members`；页面 handler 改为"先渲染 PDF → 再取快照（含 md/pdf 字节）→ 打包"。

**实机结果**（包 `deliverables_20260923_195700_d4afd2.zip`）：
- 17 个成员（16 冻结成员 + 清单），逐成员 hash 复算**全部一致**；
- 清单身份：`report_version_id=9b9ad5bc…`、`research_body_sha256=6f2c004d…`（采纳版）、
  `delivered_md_sha256=3d357590…`、`pdf_sha256=678a196e…`、`rules_fingerprint=2ddecc9a`、`evidence_fingerprint=a7d101cf`；
- `drift = {"evidence/citation_evidence.json": "changed_on_disk"}`——**如实**：工作区里那份是离线重入时写的，
  包内那份来自本次快照（内容不同即报，不静默）；
- 页面「下载Markdown」取到的字节 sha `3d357590344192cb` = 包内 `reports/report.md`；
  页面 PDF 端点（`/api/task/ui-706c5ef4a5/pdf`）字节 sha `678a196e5becfe1c` = 包内 `reports/report.pdf`；
  且包内 MD 与任务库送达正文（`task_history.report`）**逐字节相同** → 页面/包/库三处同版。

**定向测试**：`TestSameVersionDeliveryChain.test_snapshot_freezes_charts_working_paper_and_evidence_bytes`
（快照后改写图表/底稿/资料 → 包内仍是快照字节、引用证据取自快照资料、drift 如实报告）、
`test_snapshot_keeps_version_identity_and_rejects_interleaving`。

## 3. 研究价值（销量/渠道/地区数据形成分析；减重复数字与长段原文）

**修复**：
- `narrative_evidence.extract_volume_price`（确定性、离线）：从已准入的年报片段抽
  销售量/生产量/库存量（吨）与分产品/分地区/分销售模式收入构成（本期值、占比、上期值、同比），
  每条带 `locator`（api_chunk + 小节 + 字符区间）与 `text_sha256`；推算项带算式与边界。
  抽取按**列型**匹配（`值 占比 值 占比 同比`），避免从成本表里抽到成本数
  （实机反例：省内曾抽成营业成本 12,748,484,435.48）。
- `report_brief` 新增『量价与结构（发行人披露）』一节：实物量表 + 收入构成表 + 推算项（带算式）+ 直观关系 + 边界 + 出处；
  收入研究问题的支持改为**量价/结构数据**（`kind=structure`），边界写明"各因素贡献度未拆分"。
- 压长段原文：业务背景/管理层解释/风险片段统一 `_abridge` 到一句（标"……（节选，原文见出处定位）"）；
  **收入构成原始表不再当业务背景整段贴出**（与结构化块重复）。

**实机结果**（候选 → 交付）：字符 10,280 → 11,269（新增结构化分析；同时删掉原始表段与长段原文）；
新节读数：白酒销售量 -16.30%、生产量 -8.40%、库存量 +16.38%；白酒收入 -13.01%（占比 97.57%）、
省内 -11.20%、省外 -14.13%、批发经销 -13.10%、线上直销 -9.77%；
推算白酒吨价 202,592 元/吨（上期 194,936），同比 +3.93%——算式 `(28175707878.18 / 139076.05)`、
上期 `(32389581931.71 / 166154.73)`；结构差：省外比省内多降 2.93pp、线上直销比批发经销少降 3.33pp。
验收：**overall pass**；溯源率 92%（172/186，域=financial，阈值 70%）；金额溯源 100%（50/50）；
`analysis_completeness` 覆盖完整（含"结论/风险有实质内容"）。

**定向测试**：`TestVolumePriceExtraction`（3 例：构成表取值、推算与边界、无披露时 `ok=False`）、
`TestResearchQuestions.test_volume_price_supports_revenue_question_and_is_rendered`。

## 4. 证据连续性（片段跳号 = 缺口，不得当连续原文）

**反例**：缓存片段号为 2、3、4、5、10、11（缺 6–9），合并时片段的尾巴直接接下一段开头，
"连续原文"其实是拼接；摘录、字符区间与文本 hash 会被读成一段连续披露。

**修复**（`narrative_evidence.py`）：
- `chunk_gaps(chunk_offsets)` 列出跳号缺口；`merge_chunks(...)` 在跳号处插入
  `【资料缺口：此处缺接口片段 6–9（原文未取得），以下内容与上一段**不连续**】`，偏移表与实际文本严格一致；
- 记录级：跨缺口的记录带 `crosses_gap` + `missing_chunks`，`locator` 追加"（跨缺失片段 6–9，为分段摘录、非连续原文）"；
- 载荷带 `chunk_gaps`；包内引用证据带 `chunk_gaps` 与逐条 `crosses_gap` / `missing_chunks`。

**实机结果**：离线重入材料后，合并文本在字符 20,049 处出现缺口标记（原片段 5 的研发投入表 → 片段 10）；
`chunk_gaps = [{after_chunk:5, missing:[6,7,8,9], doc_offset:20049}]`；
1 条 `footnote` 记录跨缺口并被标记（定位含"跨缺失片段 6–9…非连续原文"）；
包内引用证据 8 条**逐条**按 `doc_char_start/end` 从冻结材料取回一致（8/8），`text_sha256` 全部可复算，
跨缺口那条的取回片段里含缺口标记。

**定向测试**：`TestChunkGapContinuity`（4 例：标记与偏移一致、连续片段不加标记、跨缺口记录被标记、
载荷与引用证据暴露缺口）。

## 顺带修复（本轮实测暴露）

`_mark_unsupported` 之前把文本按句切开再用 `"".join` 拼回，**换行丢失**：`## 结论` 与下一段粘成
`## 结论2024 年…`，`analysis_completeness` 从"结论有实质内容"变成"结论缺"（本轮候选实测）。
改为**按字符区间逐句替换**（`_sentence_spans`），只改命中的那句、其余字节不动；重复标记不叠加。
测试：`TestResearchQuestions.test_mark_unsupported_keeps_line_structure`。

## 包内 PDF 复核（离线，逐字符）

- 11 页；逐字符量底边/顶边：**无字符越界**（各页 `min_bottom=24.3`（页脚线及以上）、`max_top ≤ 788.8 < 841.9`）；
- 交付 MD 的**每个标题**与关键句（结论尾句"方可进入偿债能力评估环节"、边界、比率适用条件、
  参考来源条目、免责声明）在 PDF 文本中**全部可找到**（破折号按排版规范化等价处理）；
- 图注与图同页：p4 图1、p5 图2、p6 图3、p10 覆盖图、p11 两张比率图（逐页渲染确认有图形内容）；
- 图表页（p5）下半页仍有留白（`max_top=462.4`）——版面观感项，未在本轮改动。

## 未验项（如实列出，不宣称）

- **真实 Redis 多进程**下的快照/发布并发未验（本轮为单进程实机 + 离线受控反例）；
- **导出期间发生真实并发修订**未在实机复现（受控反例见 `test_snapshot_keeps_version_identity_and_rejects_interleaving`、
  `test_export_snapshot_rejects_interleaved_revision`）；
- **人工研究员评分**未做（机器验收不等于研究就绪；本任务研究状态仍是草稿：利润/现金缺可定位解释）；
- 主文页数：本轮 11 页（含 5 张图表页与附录），未压到 5 页软目标；以"有据、看得全、导出同版"优先；
- `drift` 中的 `changed_on_disk` 只说明工作区文件与包内不同源，**不代表**包内内容有问题（包内为快照字节）。
