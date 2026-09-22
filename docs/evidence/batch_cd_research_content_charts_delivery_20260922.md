# 批次 C/D：研究内容、图表、就绪标准与同版交付（09-22 复核 P1 定向修复）

2026-09-22。指令 `docs/阶段D实机复核与下一批指令_20260922.md` §3-C/§3-D（审查基线 `7ec2003`）。
C 批全部离线；D 批在**实机 ui-706c5ef4a5 的工作区**上做了一次离线候选修订 + 实际导出
（不调用模型、不联网、不新增付费请求）。

## C-1 归因与算术分开核验

新增两条确定性检查（`acceptance_checker`，financial/research 域计入缺口）：

| 检查 | 反例（实机原句） | 结果 |
|---|---|---|
| `attribution_support` | §3.3"净利率降幅大于毛利率降幅，**主要来自**收入规模下降对固定性费用摊薄的削弱"（本次无费用明细） | **fail**：缺证归因（后接"进一步拆分原因未体现"**不豁免**） |
| 同上（正例） | 写成"**可能**来自…（假设，待费用明细核实）" | pass（明确假设不扣分） |
| `ratio_arithmetic` | §5.2"资产端减少额（24.47 亿元）大于负债端减少额（20.90 亿元），是资产负债率下降的直接算术原因" | **fail**：比率算术误释 |
| 同上（正例） | 给出两期比率读数（25.42%→23.24%）或底稿里有该比率的派生关系 | pass |

冻结用例：`test_delivery_chain.TestAttributionAndChartBinding`（4 例）。

## C-2 图表绑定稳定 chart_id

- `chart_specs.financial_research_specs` 每张图带 `chart_id`（`core_scale` / `yoy_growth` /
  `ratio_<metric>`）与绑定块（指标、单位、期间、口径、主体）；渲染脚本把两者写进
  `chart_manifest.json`；简报图注显示 `图 N［chart_id］…（绑定：…）`——编号只是显示结果。
- 新增 `check_chart_references`：正文"图N"附近的指标词与第 N 张图绑定不符 → 图文错配
  （只认**正向证据**：窗口里出现别的图的指标词才判失败；没写指标名记"无法判定"，
  不误伤装配图注）。冻结用例覆盖错位、缺清单、无法判定三种情形。
- **方向措辞收口**：同比全为负时不再写"增幅最大"（实机 chart_2 的 −12.83%）——
  改为"降幅最小／降幅最大"。实测：图 2 页脚已变为
  "营业收入同比 降幅最小（-12.83%），归母净利润同比 降幅最大（-33.38%）"。
- 附带修掉一个渲染缺陷：`ratio_net_margin` 里的下划线被 Markdown 当斜体吞掉，
  PDF 里渲染成 `rationetmargin` → 显示时转义、`report_pdf._inline_plain` 还原。

## C-3/C-4 研究就绪按契约必答问题逐项裁决

`delivery_pipeline.research_state` 重写：

| 规则 | 行为 |
|---|---|
| 必答问题 | 契约 `required_metrics` 与默认三问（收入/利润/现金）取交集；逐项要有**带定位**依据才计 |
| 银行材料 | 只作**可选问题**独列，不进分子分母（不再出现"1/4 有依据"） |
| 肯定结论 | 语义角色非 boundary/inference/assumption 且 support_status ∈ unsupported/partially_supported/needs_check → 不就绪 |
| 缺失必答项 | 逐项写明（如"必答问题缺可定位依据（1/3）：经营活动现金流净额…"） |

## C-5 状态绑定最终采纳正文（页面/正文/PDF/清单同一绑定）

- 状态对象带 `binding`：`report_version_id`（采纳身份）+ `body_sha256` + `structure_version_id`
  + 契约指纹与契约原文；结构不属于当前正文 → 直接返回"待重验"（`stale=True`）。
- `web_ui._export_payload` / `_research_state_for` 读时再对一次绑定；
- **`export_manifest.json` 文件本体**新增 `research_state`（本次实机清单此前没有）；
- 编排器与人工修订路径都把契约传进 `assemble_and_verify`。

## D-1/D-3 候选修订 + 同版导出 + PDF 逐页目视

在实机工作区（`ui-706c5ef4a5`）离线执行（脚本：`scripts/candidate_revision_20260922.py`、
`scripts/rerender_candidate_charts_20260922.py`、`scripts/export_candidate_pdf_20260922.py`）：

1. **候选修订**（保留历史版本）：§3.3 改为"原因无法判定 + 待核查假设"；§5.2 改为两期
   比率复算；§2.4/附录的"绝对额减幅"解释同样改为比率复算。修订后**验收 pass**、交付
   `verified`（修订前 `fail`/draft）。
2. **图表重渲染**：规格按生产通道（`working_paper_export.chart_rows`）重算 → 6 张图带
   chart_id/绑定，措辞按方向修正；PNG 与清单同步进 `charts/`。
3. **实际导出 PDF**：走 `/report.pdf` 同一条路径（`_task_pdf_bytes` +
   `_write_export_manifest`）。清单 `files.pdf.sha256` 与导出字节一致（实测
   `ea9321d2b1fe171a…`），清单 `status=verified`、`research_state.binding.report_version_id`
   与清单 `report_version_id` **同一身份**。
4. **逐页目视**（bundled Python + `pypdfium2`，15 页 1191×1684 PNG）：
   - 图 1–3 图面清晰、坐标与图例可读，图注与图内容一致（图 2 措辞已收口）；
   - 财务对照表列对齐、无截断；
   - 发现并已修：chart_id 下划线被吞；发现未修（记录在案）：`图 N` 引用行末的
     "期间 2023、2024" 会在行尾断开（"2024" 被拆成两行）、第 2 页末"图表"标题
     与首图分页（孤立标题）、第 1 页"资产负债率 23 .24%"数字断开。
5. **旧包仍陈旧**：本次 ZIP（12:02 打的）不含修订后正文与图表；离线链用例
   `TestSameVersionDeliveryChain` 断言"旧 ZIP 标陈旧 / 缺包内清单 → 包内版本未知"。

## 未验项（如实）

- **PDF 逐页目视由我（执行者）完成**：本机 visual-judge 子代理不可用
  （账号连接不可用），因此改用 bundled Python 渲染 + 人工逐页判读，未做第二人复核。
- 候选修订是**离线产出**：未通过 HTTP 修订接口、未重启服务、未追加实机；
  服务当前装载的仍是修订前代码（本批改动尚未部署）。
- 版面三处小缺陷（引用行尾数字断开、孤立标题、数字断开）**已记录未修**：
  属版面取舍，按指令留到 A–C 通过后统一处理。
- 研究状态仍为 `research_draft`（必答三问 0/3 有定位依据）：本次没有取得原始年报，
  这是**如实结论**，不是阈值问题。
