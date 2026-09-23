# R1 证据（09-23 晚间批次）：统一研究判断与证据连续性

**指令**：`docs/阶段D系统收敛与研究效能提升_20260923.md` §4（R1）
**基线**：`417b83e`（本轮开始时 HEAD）；本轮 R1 提交见状态文档
**范围**：无新增 HTTP、无付费模型调用；材料为已保存的公告片段（离线重入）。

## 1. 一次评估、多处读取（根因一）

新增 `question_assessment.py`：给定某指标的候选披露，判定它是**哪一种材料**，并保留
判定窗口（span）、目标指标、判定理由、极性/时态/确定性：

| 类别 | 含义 | 是否算"回答完成" |
|---|---|---|
| `decomposition` | 分解覆盖充分（定量闭合该问） | **只有它算** |
| `management_cause` | 管理层明确归因（**发行人说法，非独立证实**） | 否（部分覆盖） |
| `observation` | 观察可验证（仅读数） | 否 |
| `background` | 背景相关（行业/市场语境） | 否 |
| `negation` / `not_disclosed` | 明确否认原因 / 明确未披露原因 | 否 |
| `tentative` / `hypothesis` | 不确定 / 未来或假设 → 待复核 | 否 |

四个消费方读**同一份** `structure["question_assessments"]`：
`_research_questions`（问题区）、`_change_explanation`+`_risks`（风险区）、
`delivery_pipeline.research_state`（头部状态）。缺条目时按旧 `match_kind` 折算一次
（兼容既有落盘结构），不再各自重判。

### 语义矩阵（正反例同时修，`test_question_assessment.py` 11 例）

| 输入 | 修复前 | 修复后 |
|---|---|---|
| 净利润下降**原因尚未披露**。 | explanation ✗ | `not_disclosed` |
| 净利润下降**并非由**原材料涨价导致。 | explanation ✗ | `negation` |
| 2024年净利润下降，**主要由于**原材料涨价。 | reading ✗ | `management_cause` |
| 若原材料价格继续上涨，净利润**将**下降。 | reading | `hypothesis`（待复核） |
| 净利润下降**可能**与费用投放增加有关。 | reading | `tentative`（待复核） |
| 2023年净利润因减值下降。（评估 2024） | explanation ✗ | `none`（期间不符） |
| 营业收入因销量下降而下降，**净利润66.73亿元，同比下降33.37%**。 | 利润被收入因果误算 | 收入=`management_cause`；利润=`observation` |
| 报告期内白酒行业进入存量竞争，2024 年实现营业收入…下降 12.83%。 | 背景算"已支持" | `background`（不计回答完成） |

判定规则：因果必须是**因果短语/构式**（主要由于/所致/导致/因X而Y/受X影响/系X所致…），
单字"因/受/系"不升级；否定/未披露/不确定/未来优先于因果；因果只算在**它所在的那个
子句**头上（带子句组成的合并窗口）；期间明确不符即不认。

### 展示与状态的一致性（实机候选）

- 问题区：`材料：**分解覆盖（部分）**（来源 [2]；api_chunk 3 …）`／`材料：仅读数/观察…`／
  `材料：未取得对应披露，**观察成立、原因待证**`；
- 风险区：`营业收入：已取得量价/结构数据（…），分解覆盖：部分`、`归母净利润：未取得该指标
  变化的解释；已取材料只含该指标读数（…）`、`经营活动现金流净额的变化原因尚不能证明`；
- 头部状态：`必答问题未完成（3/3）：…（其中部分覆盖 1 项：收入变化的量价与结构依据[分解覆盖充分]）`
  ——**不再沿用旧 "1/3 已支持"**；`mandatory_supported=0`、`mandatory_partial=1`。

## 2. 证据连续性（根因二）

- **逐文档映射**：载荷新增 `chunk_offsets_by_doc` / `chunk_gaps_by_doc`；包内引用证据
  按**每条记录自己的 URL** 换算段内偏移（`_map_for`），不再共用"最长的那张表"。
- **缺口是硬边界**：`split_sections` 在缺口标记处结束当前节并清空标题栈，缺口后的内容
  作为"续段"单独成节（path 标`（续：缺片段 6–9）`），其后所有节都带 `after_gap` 标记
  （标题不继承，定位写明"上接缺口"）。
- **跨缺口不再合成单条证据**：实机 8 条记录中，跨缺口记录数 **0**；此前那条
  `footnote`（研发投入表 → 品质方面）现被断开，各自落在连续片段内。
- **分段可回溯**：每条记录带 `segments`（`{chunk, doc_start, doc_end, chunk_local_start}`），
  离线可按段回到原文；`text_sha256` 可复算。

实机核对（`ui-706c5ef4a5`，离线重入后）：
`chunk_gaps=[{after_chunk:5, missing:[6,7,8,9], doc_offset:20049}]`；
风险记录 `chunk=10 after_gap=True missing=[6,7,8,9]`，定位
`api_chunk 10 · 小节：（三）可能面对的风险（字符 20474-20707）（上接缺口 6–9，为分段摘录、非连续原文）`；
`by_doc` 表按 URL 各存一份。

## 3. 从现有缓存产生的新候选（保留旧版）

- 生产入口（`build_structure` → `render_brief_markdown` → `rewrite_report_links`）重新生成：
  候选 `aac5d6415bffa24a`，11,209 字符；当前采纳版 `6f2c004d1b52` **未被覆盖**。
- 只读验收（`run_acceptance`，未写验收产物）：**overall pass**，全部硬检查通过；
  溯源率 92%（172/186，域 financial，阈值 70%）、金额溯源 100%（50/50）、
  `analysis_completeness` 覆盖完整（本轮顺带修掉一个真实假阳性：缺口说明里的"未披露"
  被占位符探测器命中，改写为"价无发行人披露口径"，语义不变）。
- 候选与旧版差异（问题区/风险区/状态同源）：`营业收入` 由旧的"初步背景依据/支持"
  改为`分解覆盖（部分）`；`归母净利润` 由"解释已取得"改为"仅读数、未取得解释"；
  头部状态由"缺可定位依据（2/3）"改为"未完成（3/3，其中部分覆盖 1 项）"。

## 4. 测试与命令

```
REDIS_PORT=6399 python -m unittest test_question_assessment     # 11 OK（语义矩阵）
REDIS_PORT=6399 python -m unittest test_narrative_evidence      # 52 OK（含缺口硬边界/逐文档映射）
REDIS_PORT=6399 python -m unittest test_delivery_chain          # 335 OK（含覆盖语义/措辞一致）
REDIS_PORT=6399 python -m unittest test_report_quality test_facts test_working_paper \
  test_review_edit_api test_report_version                       # 170 OK
```

## 5. 未验项（如实列出）

- 真实 Redis 多进程与真实并发导出（R2 范围）；
- 空模板/多文档**真实**材料的端到端（本轮逐文档映射用受控夹具与单文档实机验证）；
- 真人研究员评分（R4 范围）；旧结构兼容折算只覆盖 `has_evidence=True` 的历史结构；
- 候选尚未采纳：正常页面"查看差异 → 采纳/拒绝"在 R2 批次做。
