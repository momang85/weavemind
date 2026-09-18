# 口径证据链修复（2026-09-18）

承接批次 C 实机发现的阻塞项（`docs/evidence/research_path_C_20260918.md` §4.2/§5）：
**没有任何数据源声明报表口径**，而 A′ 规则要求"口径只认来源声明的"，两者相加使研究交付
永远到不了 verified（实机 `ui-96f5c363cc`：18 条事实全 unknown → 6 项必需事实缺口 → draft）。

## 1 先查清"来源到底声明了什么"（数据源侧结论）

| 来源 | 结论 |
|---|---|
| 东财 A 股主要指标 `RPT_F10_FINANCE_MAINFINADATA` | 165 个字段，**无任何口径/合并标记字段**（`PARENTNETPROFIT` 是"归母净利"这个指标，不是口径声明） |
| 东财 F10 三大报表（利润表等） | 返回 `REPORT_TYPE` 只有期间（`四季度`）；`type`/`reportType` 参数被忽略，拿不到母公司单独报表 |
| 东财港股 `RPT_HKF10_FN_MAININDICATOR` | 同样无口径字段 |
| 巨潮 cninfo | 仍是未打通的骨架（探针恒空/405、mcode 门禁），不可用 |
| SEC EDGAR 10-K | 事实集只有合并报表，但没有"口径"这一维度的字段 |

即：**机器可读的口径声明在可达的来源里不存在**。所以修法不能是"读一个字段"。

## 2 修法：适配器按**来源自身的结构**声明口径，且必须带依据

声明不是猜，而是把来源可观测的结构事实写出来，并**只在结构成立时**声明：

- A 股：行内含 `PARENTNETPROFIT`（归属于母公司股东的净利润）→ 声明**合并**。
  依据：该科目只在合并报表中存在（母公司单独报表没有"归属母公司股东"这一层）。
- 港股：行内含 `HOLDER_PROFIT`（股东应占溢利/归母净利）→ 同样声明**合并**。
- 美股：SEC 10-K 的 us-gaap XBRL 事实集为合并报表口径（母公司单独报表不在 10-K 事实集）。
  这一条依据是**申报规则**，不是载荷结构，故证据原文写清，便于人工复核。
- 来源形状一旦变化（缺上述字段）→ **不声明**，`facts` 记 unknown，底稿如实列缺口。

落地：

| 位置 | 改动 |
|---|---|
| `adapters/eastmoney.py` `_declare_consolidated` | 结构证据判定；`fetch_ashare`/`fetch` 命中才写 `metadata.caliber` + `caliber_evidence` |
| `adapters/sec_edgar.py` | `metadata.caliber="合并"` + 申报规则依据 |
| `facts.py` `_caliber_of` | 口径只认来源声明（行级 > 实体级），**口径与证据成对**：说不出依据就按 unknown；非法值（把"三季报口径"这类期间描述塞进口径字段）不采信 |
| `facts.py` `Fact` | 新增 `caliber_source`（row/metadata）与 `caliber_evidence`：口径"是谁说的、依据是什么"随事实一起走 |

**门槛没有放松**：`_caliber_ok` 一字未改——未知口径仍不参与达标与同比，请求口径与来源不符
仍判不符（例：表单选"母公司"，而东财主要指标只有合并口径 → 事实进 `pending`、底稿列口径缺口，
这是正确行为，不是 bug）。

## 3 顺带闭合的两件事（A′ 遗留）

- **披露日期取证**：东财 A 股行的 `NOTICE_DATE` 是公告日期（与报告期末、抓取时间三者不同），
  现在逐行写成 `disclosure_date`，`facts` 的 `disclosed_at` 优先取它——"截至日"这才有来源证据。
  实测 600519：2023 年报公告日 2025-04-03、2024 年报公告日 2024-04-03（均早于截至日 2025-04-30）。
- **报告期末对齐**：`period_end` 此前只从来源的 `start/end` 取，适配器行给的是 `report_date`
  （它就是期末），于是"期末空着而 disclosed_at 回落成期末"看起来自相矛盾；现按 `report_date` 回填。

## 4 证据链在交付物里可见（不只是内部状态）

- 底稿行（`working_paper.json`）与读侧 detail（`build_result` 的 `rows_detail`/`derived_detail`）
  都带 `caliber`/`caliber_source`/`caliber_evidence`；
- 报告步骤注入的 [已选事实] 块会写明"报表口径：合并（依据…）。报告中须注明口径及该依据"；
  未声明时写"**未声明**——不得声称已知口径，须如实写明这一缺口"。

## 5 验证

**真实数据预演**（不依赖实机任务，直接跑抓取 + 底稿）：

```
metadata.caliber = 合并
metadata.caliber_evidence = 来源行含 PARENTNETPROFIT（归属于母公司股东的净利润），该科目只存在于合并报表 → 合并报表口径
row disclosure_date = ['2025-04-03', '2024-04-03']
fact.caliber = 合并 | caliber_source = metadata
paper.ok = True | 必需事实 6/6 | 同比 3 条（单位 %）：营收 15.66 / 归母净利 15.38 / 经营现金流 38.85
港股 00700：metadata.caliber = 合并（HOLDER_PROFIT 依据）
```

**测试**：

- `test_facts.TestCaliberEvidence`（7 项）：metadata/行级声明的级别与依据、无依据仍标注、
  无声明 fail closed、期间描述不采信、`disclosure_date` → `disclosed_at`、期末回落。
- `test_financial_chain.TestCaliberDeclaration`（3 项）：A 股有 `PARENTNETPROFIT` 才声明并带
  逐行公告日；**缺该字段时不写 caliber**；SEC 声明的依据原文可查。
- `test_working_paper.TestCaliberEvidenceChain`（4 项）：声明后底稿达标 6/6 + 3 条同比（%）、
  对偶（不声明）仍不达标且不产生同比、行与读侧 detail 都带口径依据。
- `test_offline_delivery.TestResearchFixedPathOffline`（13 项，新增 2 项）：**生产形状**
  （行内无口径、适配器声明）的离线研究任务跑到 `status=verified` 交付且硬门槛为空；
  对偶（不声明）仍是 draft + 硬门槛写明缺口。

## 6 边界与未验证

- **港股没有公告日期**（`NOTICE_DATE` 为 None）：`disclosed_at` 回落报告期末，是"截至日"
  证据链上仍偏弱的一环，未在本批解决。
- **美股的口径依据是申报规则**（非载荷可观测结构）：可信但不可由数据自证，证据原文已写明。
- **母公司口径在这些来源上取不到**：用户选"母公司"会得到口径缺口而非静默的合并数据（预期行为）。
- **未重跑实机**：本修复的实机验收留到下一轮（同一实机请求不重复跑）。
- Mimosa 预推送仍无完整扫描结论（`scanner_enobufs`），不据此宣称项目安全。
