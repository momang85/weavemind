# P0-f（进行中）：交付身份三版不同 —— 地面真相与已定位的缺口（2026-09-28）

依据：`docs/DSH真实样本复核与下一批执行_20260928.md` §3「交付身份先处理（P0，不必等模型重生成）」。
冻结样本 `ui-a06a005c9b`。基线 `84d9307`。

## 地面真相（只读实读产物，未改动任何文件）

工作区：`%LOCALAPPDATA%\Temp\agent_workspace\tasks\projects\default\ui-a06a005c9b`

| 项 | 实测 |
|---|---|
| 交付包 | `deliverables_20260928_012445.zip` **4,060,767 字节**，sha256 `6072d9390ec79de7…` |
| 磁盘正文 | `project/reports/report.md` **20,892 字节**，sha256 `35f37f00c0f3b7ee…` |
| 包内正文 | `reports/report.md` **6,179 字节**，sha256 `178a4a85c0128eb9…` → **与磁盘不是一份** |
| 包内清单身份 | `report_version_id` = **空**、`research_body_sha256` = **空**（无身份可对） |
| 清单 `delivered_md_sha256` | `178a4a85…`（= 包内那份 6179 字节正文） |
| 清单 `pdf_sha256` | `436928c6acaac793…` = **原始年报** `fetched/436928c6acaac793.pdf`（同 hash 也在文件清单里） |
| 清单 schema | `weavemind.package/2`；`sources_fingerprint 8fc88275…`、`rules_fingerprint 2ddecc9a` |

**架构师描述的三点全部坐实**：①采用稿/磁盘稿/ZIP 三版不同；②旧 manifest 身份为空；
③`pdf` 位指向原始年报（不是研究报告 PDF）。

## 沿正常下载链的检查：缺口已定位到具体一行

下载链的陈旧判定在 `web_ui._export_payload()`（`web_ui.py:2981-3012`），四路判据：

| 路 | 条件 | 对本样本是否生效 |
|---|---|---|
| A 清单比包新 | `generated_at - zip_mtime > 120` 且身份不同 | 取决于导出清单时间，**不确定** |
| B 包早于采纳版本 | `adopted_at - zip_mtime > 1.0` | **取决于时间戳**——时间对齐就失效 |
| C **身份比对** | `if _pkg_body and _cur_body and _pkg_body != _cur_body` | ❌ **不生效**：本包 `_pkg_body` 为空，条件短路 |
| D 包内无清单 | 由 A/B 间接覆盖 | 不适用（包内有清单） |

**结论（缺口）**：**"包内有清单、但身份为空"这一路没有任何判据**。这样的包只能靠
A/B 两路**时间戳**侥幸被抓；一旦时间对齐，它就会被当成当前交付对外提供——
而这正是"缺绑定/过时包不能冒充当前交付"要防的事。
`web_ui.py` 里已加注释记录该缺口（见 `_export_payload` 内 "P0-f 待补" 段）。

## 修法（已落地）：只在"包内**有**清单但身份为空"时判陈旧

上一版修法 `if _cur_body and not _pkg_body: stale = True` **打破两条既有"不误标"用例**。
读懂夹具后原因清楚了：`TestExportVersionNamespaces._ws()` 写的是**导出清单**
`export_manifest.json`（即 `_exp`），而**包内清单** `_pkg_manifest` 要从 zip 里读；
那两个夹具的 zip 是空壳 `b"PK\x05\x06"` → `_pkg_manifest = {}` → `_pkg_body` 为空。
于是我的规则把"**包内读不到清单**"也当成了"身份为空"，连带把它们的"不误标"断言打破。

**"包内没有清单"与"包内有清单但身份为空"是两件事**，判据必须分开：

```python
if _cur_body and _pkg_manifest and not _pkg_body:
    _package_stale = True
```

- 包内**读不到清单**（`_pkg_manifest` 为空）→ 不是"身份为空"，不据此判陈旧（保持既有行为）；
- 包内**有清单但身份字段为空** → 无法证明属于当前版本 → 存在采纳正文时判陈旧。

**验证**：`test_delivery_chain` **374 项 OK**（+1 新用例
`test_package_manifest_with_empty_identity_is_stale`：用真 zip 装入 schema 在、身份为空的
清单 → 判陈旧；尚无采纳正文时不误标）。两条既有"不误标"用例**仍绿**。

## 已落地（2）：原始披露 PDF 不再冒充研究报告 PDF

**根因**（`delivery_pipeline.py:525-528`，旧版清单构造器）：

```python
_pdfs = sorted(a for a in file_hashes if str(a).lower().endswith(".pdf"))
_pdf = _pdfs[0] if _pdfs else ""          # ← 取包内**任意** PDF，按字母序
```

`sorted()` 下 `fetched/436928c6acaac793.pdf`（原始年报）**排在** `reports/report.pdf`
前面 —— 所以"研究报告 PDF"位被原始披露占走。schema 2 的构造器（同文件 767 行）
**本来是对的**：`_pdf = "reports/report.pdf" if "reports/report.pdf" in hashes else ""`。
旧构造器与它口径不一致。

**修法**：旧构造器对齐 schema 2 的口径——只认 `reports/report.pdf`，没有就**如实留空**，
并写 `pdf_missing_reason` 说明"包内没有研究报告 PDF；原始披露 PDF 不占该位置"，
让读者能区分"没有报告 PDF"与"忘了放"。

**验证**：`test_delivery_chain` **374 OK**、`test_offline_delivery` **34 OK**、
`test_p0` **416 OK**。

## 仍然未做到的

- **页面是否真的拦截该旧包，尚未实测**：本批只读到了判定逻辑，还没沿
  `/api/task/<id>/deliverables` + 页面下载按钮跑一遍看实际表现（含视觉核验）。
- "根据选中正文冻结同版报告与证据附件"尚未做；旧包**未删除、未覆盖**（按指令保留为失败证据）。
- 原始披露 PDF 冒充研究报告 PDF 这件事，**还没有任何拦截**（本轮只确认了它存在）。
