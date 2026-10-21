# U90-D 下载与同版核验（2026-10-20）

> 两段：**S1 洋河回放**（下节，历史样品回放）与 **S2/S3 三一 采纳后新包**（本节末），
> 外加 **S3 恢复包**（`ui-91677c7767`，详见 `../downloads_s3_resumed.json` 与下节末）。
> 与交审报告的场景对应：本文件 S1 = 任务书场景①（洋河，以历史样品回放取证，非新实时取数）；
> 本文件 S2/S3 = 任务书场景②③的证据（三一同路径 + 洋河恢复）。
> 全部字节原件在本目录；核验脚本 `.tmp/u90_download_verify*.py`、`.tmp/u90_s3_download.py`（只读探针，
> 代称不了按钮已验，按钮动作见 `../resume_ui_91677c7767.md` 与本文件「入口」两段）。

## S2/S3（三一重工 `ui-cc853cf86b`）：改参 → 候选 → 采纳 → 导出新包 后的下载身份

入口：真实浏览器（会话 `momang`）「补材料」标签页 → 分析工作台 `导出当前包` 按钮
（21:0x 生成 `deliverables_20261020_204948_375af7.zip`）；下载走页面同一路径。

| 文件 | 路径 | 结果 |
|---|---|---|
| MD | `GET /api/task/ui-cc853cf86b/report.md` | 200，56700 B，`X-Report-Version-Id: 23b61beeb37a719b…`（= 已采纳身份）、`X-Report-Research-Body-Sha256: 0699a73bb88f…`（= 采纳候选正文）|
| PDF | `GET /api/task/ui-cc853cf86b/pdf` | 200，5625071 B，`%PDF-` + pypdf 打开，页数见 `../analysis_chain_cc853cf86b.json`，正文含「三一重工／情景／参考来源／免责声明」|
| ZIP | `GET /files/ui-cc853cf86b/deliverables_20261020_204948_375af7.zip` | 200，5717706 B，SHA-256 `c8005aaa…`，23 成员 |

- 包内 `PACKAGE_MANIFEST.json`：`schema=weavemind.package/2`，`report_version_id=23b61beeb37a…`
  （= 版本库 `selected`），`research_body_sha256=0699a73bb88f…`（= 采纳候选正文），
  `delivered_md_sha256=c3c2aa93…`。**下载 MD 字节 == 包内 `reports/report.md` == 清单 `delivered_md_sha256`**（三项一致）。✔
- `analysis/selection.json` 包内带选择记录：run `a200c599638c`（`revenue_growth=0.1`，validation/1.1.0，本次浏览器复算+采纳）。✔
- 旧包 `deliverables_20261020_201859_478d5a.zip`（对应上一版 `d290ab012e54…`）仍可**按精确文件名**下载（200，无过期标记）；
  页面与 `/api/task/<id>/deliverables` 只指向当前包（`delivery_identity.package` = 新包）。**旧新未混包**（每个包内部身份自洽），
  但「按旧 URL 仍能取到旧包」作为残留风险记入交审报告。
- `drift`（如实）：`analysis/analysis_runs.json` changed_on_disk、`evidence/citation_evidence.json` missing_on_disk
  —— 包内字节取自装配时刻，交付正文身份不受影响。

---

## S1 洋河回放（`ui-10ea37599d`，历史样品回放，非本批实时取数）

入口：登录会话 `momang`（真实浏览器操作：任务页「导出当前包」按钮生成
`deliverables_20261020_180828_e1871e.zip`）；下载路径与页面一致
（`/files/<task>/...` 与 `/api/task/<id>/report.md|pdf`）。本目录文件即下载字节原件。

| 文件 | 路径 | 结果 |
|---|---|---|
| MD | `GET /api/task/ui-10ea37599d/report.md` | 200，19913 B，SHA-256 = 清单 `markdown.sha256`（675483ff…）一致 |
| PDF | `GET /api/task/ui-10ea37599d/pdf` | 200，5519744 B，8 页，`%PDF-` 头 + pypdf 打开成功，正文含「洋河股份／归母净利润／营业收入」，SHA-256 = 清单 `pdf.sha256`（80ea23f1…）一致 |
| ZIP | `GET /files/ui-10ea37599d/deliverables_20261020_180828_e1871e.zip` | 200，5742597 B，23 成员 |

## 同版身份
- 包内 `PACKAGE_MANIFEST.json`（weavemind.package/2）`report_version_id`
  = 工作区 `export_manifest.json.report_version_id` = `97b839ed…`。✔
- 22/22 非清单成员 SHA-256 与包内清单逐一相等（清单含自身 hash 在逻辑上不可能，
  PACKAGE_MANIFEST.json 为身份元数据成员）。✔ 无 member_mismatch。
- 下载 MD 字节 = 包内 `reports/report.md`（675483ff…）= 清单 `delivered_md_sha256`。✔
- 下载 PDF ≠ 包内 PDF 属**设计内**：页面 PDF = 完整正文（正文+附录，8 页）；
  包内 PDF = 主文（附录前，7 页，任务22口径）。逐页文本（剥离页脚后）包内主文
  是页面 PDF 的**前缀**，页码不同解释字节差异；`inner_pdf_sha256 = 包内清单 pdf_sha256`。✔
- 采纳链闭环：`analysis_inputs.selection.entries[0]` = 本次浏览器复算+采纳的
  run `a25a4fff…`（params revenue_growth=0.10, gross_margin_delta=0.01,
  expense_change_ratio=0），`adopted_identity = 97b839ed…`，`in_package: true`，
  note「包内存有选中记录，报告由所选版本装配」。✔ 旧新未混版。

## 漂移（drift，如实记录，不粉饰）
快照后磁盘上 `analysis/*.json|md` 与 `evidence/citation_evidence.json` 报
missing_on_disk／changed_on_disk：包内字节来自快照（正确语义，包内 12/12 validated
runs 均可复算），工作区成员路径解析此前漏掉二级目录（`delivery_pipeline._disk_drift`
已在本批修复并过 12/12 回归）；`analysis_runs.json` changed 属实（本次复算新增 run）。

## 修复（本批）
`delivery_pipeline.py::_disk_drift`：候选路径先查 `ws/<arc>`（analysis/plan.json 等
二级目录成员），再退回 project/ 平铺——修复前这些成员被误报 missing_on_disk。
验证：`test_delivery_chain.TestSameVersionDeliveryChain` 12/12 OK。

## 附带核验（脚本 `\.tmp\u90_download_verify.py`，记录于 downloads_summary.json）
- 明细 JSON：`docs/evidence/20261020-u90/downloads/downloads_summary.json`
- pypdf 文本探针与前后缀关系：见上表与正文说明；PDF 已按页打开读取（非仅 MIME/200）。

---

## S3 恢复包（`ui-91677c7767`，2026-10-20 21:21:30 恢复运行的出包；脚本 `.tmp/u90_s3_download.py`）

| 项 | 结果 |
|---|---|
| ZIP | `GET /files/ui-91677c7767/deliverables_20261020_212027.zip` → 200，483290 B，SHA-256 `cedec8fb…`，17 成员 |
| 清单 | `schema=weavemind.package/2`，`report_version_id=3cb1f60d…`，`research_body_sha256=9a861fd39fa4…`，`delivered_md_sha256=f11adbce…`；包内 `reports/report.md` 字节 == `delivered_md_sha256` ✔（包内自洽）；无 analysis selection 侧车（该任务非分析驱动，工作台如实标"没有可复算输入"） |
| MD | `GET /api/task/<id>/report.md` → 200，16241 B，`X-Report-Version-Id: 6d5e4655…`、`X-Report-Research-Body-Sha256: 619159be1625…`、草稿 0；**与包内 `delivered_md_sha256` 不相等**——原因见下行 |
| PDF | `GET /api/task/<id>/pdf` → 200，5472086 B，7 页 pypdf 可读，含「洋河／免责声明」 |

**staleness（本批发现的真实状态）**：`/api/task/<id>/deliverables` 对该包如实给出
`package_stale: true` + note「该包不是当前采纳正文生成的版本…请重新导出后再对外交付」——
包体 `9a861fd39fa4` 是恢复运行出包时的正文，当前正文已被结构投影重建为 `619159be1625`。
即：**页面正文与该包正文不同版，平台有标记（API 层），但旧包文件本身仍可按精确 URL 200 下载
（响应无 staleness 头）**。该包内部自洽（manifest/正文一致），不构成包内新旧混用，
但"下载到的包落后于页面正文"作为真实样本记入交审报告剩余阻塞②。
该免责声明为中性文案（「数据来源于公开渠道…」），不含与事实相悖的"未引用外部数据源"条款；
相悖条款只出现在三一 `ui-cc853cf86b` 的 MD（v1/v2 均有）——见交审报告第 19 项与剩余阻塞③b。
