# 第二家公司真实资料：京蓝科技（000711.SZ）2020 年年度报告（更正后）

- 来源：**用户提供**的巨潮查看器链接，解出官方直链
  `http://static.cninfo.com.cn/finalpage/2025-09-05/1224639904.PDF`
  （查看器参数里的 `docUrl` 原样给出 `finalpage/2025-09-05/1224639904.PDF`，
  只做"查看器 → 静态域"的机械改写，**不是**手拼出来的目标）。
- 隔离环境：工作区 `%TEMP%\wm_jl_ws`（`task_id=ui-jl2020`）、材料记录
  `2db15147bad934f9`；**没有**碰正在运行实例的任务库/工作区，全程未调模型、未新增付费服务。

## 1. 取件与解析（应用既有通道）

`annual_report_pdf.doc_from_url(...)`（内部走 `net_policy.fetch_document` 的内容通道，
策略校验/出口模式都在那一层）：

| 项 | 实测 |
|---|---|
| 标题 | 京蓝科技股份有限公司 2020 年年度报告（更正后） |
| 页数 / 正文字符 | **273 页 / 221,316 字符**（有文本层，非扫描件） |
| 正文 sha256 | `3f6ee7b8a79ee673aead60fd3fa6c7710c61bd3aed42b34a0170986325e00b72` |
| 拒收类别 | 无（`doc_from_url` 未触发任何 reject 回调） |

## 2. 准入（**正常入口**：`material_intake.store` → `admit`）

```
store: ok=true, material_id=2db15147bad934f9, duplicate=false（通道 web_link）
admit: ok=true, status=admitted, provenance_label=人工提供的官方直链
cutoff: disclosed_at=2025-09-05, precision=day, basis=source_url_format,
        as_of=2025-09-30, verdict=within
section_count=1263；read_scope: text_chars=221316, truncated=false
材料索引：1 条，status=admitted
```

同一份文档走纯函数入口 `disclosure_ingest.ingest(..., section_pick="questions")` 的**指标覆盖**：

| 指标 | 状态 | 承载小节数 |
|---|---|---|
| `revenue` | `present_in_scope` | 15 |
| `net_profit` | `present_in_scope` | 17 |
| `operating_cashflow` | `present_in_scope` | 43 |

`_doc_period(title,url)` = **2020**（用文档**自身**的报告期判定，不拿正文里的对比年充数）；
`source_type`/`document_provenance` 都是 `issuer_annual_report`。

## 3. 这份材料的两条**语义提醒**（必须随材料一起记，不能只用它的数字）

1. **它是"更正后"版本、披露日 2025-09-05**：报告期 2020 与披露日相差 4 年 8 个月。
   - `as_of` 必须 ≥ 2025-09-05 才成立"资料截止内"（本次取 2025-09-30；若把 `as_of` 设成
     2021-04-30，材料会被 `REJECT_AFTER_CUTOFF` 正确拒绝）；
   - 它**不能**当作"2021 年当时已知的 2020 年数据"：时点回测里"当时版本"是 2021 年披露的
     原版年报。这正是数据集契约里 `restatement` 字段要表达的东西（本材料应记
     `restatement="更正后 2025-09-05"`），也是"不能用后来重述后的值泄露未来信息"那一条的实例。
2. **`*ST京蓝` 带退市风险警示**：作为"第二家非金融公司"成立，但经营特点偏极端
   （大概率负利润/负权益），拿它当"不同经营特点的对照"不如非 ST 公司中性。
   若要更中性的第二家，给一条非 ST 公司的官方直链即可同法再做一份。

## 4. 仍未做：把这份材料变成**事实**再跑四族模型

四族模型（利润桥/现金质量/营运资本/情景）要的是两期 `revenue`/`net_profit`/`gross_profit`/
`operating_cashflow` 等**结构化事实**。两条路：

1. 东财结构化入口（`adapters/eastmoney.fetch_ashare`）——本环境对已知可用代码也返回
   77 字节空载荷（站点/出口限制，见 `q0_ci_and_second_company_20260929.md` §2 ④），**当前不可用**；
2. 从这份 PDF 抽"主要会计数据 / 合并财务报表"（表格 + 跨页 + 双期对齐 + 与正文交叉核对）
   ——属**抽取器工作**（现役抽取器只覆盖东财结构化行与经营维度表），未做；
   要做就单独一批（含"抽错宁可拒绝"的反例矩阵）。

在事实到位之前：**Q2 的"两个真实公司"只在"材料准入"这一层成立**，
"两家公司各自跑通分析链并对照"仍未成立，不得宣称退出。
