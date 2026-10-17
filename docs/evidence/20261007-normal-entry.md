# 10-07 正常入口复验（判断随数据版）— 一次确定性导出

任务 `ui-10ea37599d`（洋河 2023–2024，真实材料、0 元 API）。入口＝**页面会话里的正常 HTTP 入口**：
`POST /api/task/<id>/analysis/adopt`（重新装配）→ 页面「按当前版本重新导出」→ `POST /api/task/<id>/package`。
下载入口 `GET /api/task/<id>/report.md` 与 `/pdf` 的字节也一并实测比对。

**关于「采纳这版」按钮**：该按钮本次为 `disabled`，title 写「这一条已经在正文里」——它守的是
*选择*没变（同一 run `f478645b6722…`），而本次要的是**用新代码重新装配**。因此按**同一端点**
（UI 调用的那一个，同一 run_id）触发装配；这不是绕开入口，而是 UI 因选择未变而隐藏的那一步。
预先做了 `POST /analysis/recompute`（确定性复算、0 模型调用）：`run_id` 相同（同输入同参数内容寻址），
差异全为 `delta 0.0` ⇒ 正文变化只来自判断层代码，不来自读数变化。

## 结果

| 项 | 值 |
|---|---|
| 采纳身份（`report_version_id`） | `829dda5b13a118f9529fb244f602bc3e212ca23bf2ad7784a8e1e31045be19e5` |
| 交付状态 | `verified`（装配后 `delivery_projected: true`、`acceptance_bound: true`） |
| 包 | `deliverables_20261007_011810_fd775e.zip`，5,889,521 字节 |
| 包成员 | **30**（29 内容成员 ＋ `PACKAGE_MANIFEST.json`），**逐成员 hash 与清单全符** |
| 交付正文 sha256 | `c0cedd77560c2db7a3887bea5bb36b86fff335b2d08567250a12f9a713a911fe`（＝清单＝下载入口字节） |
| 交付 PDF sha256 | `ee52ec1a4d16e3a2d3e9574ff7210a3f775b3dc07825cc4c8357ae47612c464c`（＝清单＝下载入口字节） |
| 页数／正文字符 | **10 页**／13,063 字符（PDF 文本 12,112 字符） |
| 首屏 | 第 1 页即 `## 关键判断与下一步`；`## 三项主判断`／`## 关键发现`／`## 分析`／`## 分析摘要` 均不在正文 |
| 移出内容随包 | `analysis/analysis_detail.md` 含收束块 **26,471 字节**（＋`analysis_cards.md`＋引用证据） |
| 机器验收 | `overall=pass`，无失败项；数字溯源率 **79.7%（145/182）**，计算命中 49、引用 96 |
| 研究状态 | `research_draft`（**人工复核未做**——三态分离，代理侧不代替用户标复核） |
| 量化成员 | 无 `quant/*`（本任务工作区没有量化读数；外发门不产结论） |

**正文级方向证据**（对最终交付正文，不只是对照表）：目标兑现「未兑现」＋计划/实际两条独立读数、
利润下降＋税费**缓冲** +11.64 亿元、现金**由利润变化主导**、发行人原因＝真实原句（含「存量竞争」）、
**全文无**「税费减少托底」；PDF 第 1 页同样含首屏与「未兑现」。

**与副本装配交叉核对**：副本（生产函数、同一材料）导出的包 `deliverables_20261007_010519_d80e85.zip`
里 `reports/report.md` 的 sha256 **也是 `c0cedd77…`** —— 副本装配与正常入口产物**同版**
（[20261007_external_gate_copy.json](20261007_external_gate_copy.json)：该副本带 4 个量化读数，
外发门 `allowed=false`，包内只剩 `quant/EXTERNAL_DELIVERY_RESTRICTION.md`，财报正文保留、自检 30/30 `ok`）。

## 留档文件

- [delivered_report.md](20261007-normal-entry/delivered_report.md)：包内交付正文（sha 见上）
- [PACKAGE_MANIFEST.json](20261007-normal-entry/PACKAGE_MANIFEST.json)：包内清单（逐成员 hash）
- [analysis_detail.md](20261007-normal-entry/analysis_detail.md)：同版附件（含收束块）
- [pdf_text.txt](20261007-normal-entry/pdf_text.txt)：交付 PDF 逐页文本
- [page_1.png](20261007-normal-entry/page_1.png)／[page_2.png](20261007-normal-entry/page_2.png)／[page_3.png](20261007-normal-entry/page_3.png)：首屏三页渲染
- [20261007_normal_entry_verify.json](20261007_normal_entry_verify.json)：机器复验（逐成员 hash、同版比对、页数、方向断言）

**边界**：本次为**同材料重新装配 ＋ 重新打包**（确定性、0 模型调用、无新研究、无付费任务），
旧包与旧身份原样保留；PDF/正文只经由 `web_ui` 的既有导出函数生成，未手工改写包内任何字节。
