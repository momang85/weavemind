# 页面按钮路径复核（09-24 晚间 · 登录后实机）

**目的**：补上 R2/R3/R4 均记录为"未验"的那一项——**页面 UI 整链**
（「修改正文并重验」→「按当前版本重新导出」→ 下载），走页面自身的按钮与接口，
不用离线替代脚本。
**任务**：`ui-706c5ef4a5`（洋河股份 002304.SZ，2023–2024 合并口径，银行对公视角）。
**服务**：`launcher.py stop` → `deps --fix` → `start`（16/16 存活）；登录身份
`momang · 管理员`（会话存 SQLite `sessions`，重启后仍有效）。
**范围**：无新增 HTTP 抓取、无付费模型调用；正文修订只改措辞，未改任何数字。

## 1. 改前状态（页面读到的事实）

| 项 | 页面显示 |
|---|---|
| 机器验收 | pass（本版 `6228c7467bf1`，绑定本版正文） |
| 计划评审（Critic） | PASS（属版本 `60781cdc76e7`，当前版本无评审结论） |
| 人工复核 | 待复核（无研究员批准记录） |
| 交付包 | `deliverables_20260924_092225_2a9922.zip`（生成于 2026-09-24 09:22），无陈旧提示 |
| 研究状态 | 研究草稿／待补原始披露：必答问题未完成（3/3），其中**部分覆盖 1 项**（收入[分解覆盖充分]） |

## 2. 「修改正文并重验」：新版本 + 重验 + 交付绑定

页面操作：点「修改正文并重验」→ 在「替换整份研究正文」框内粘贴修订稿 → 提交。
修订内容（4 处措辞，**无数字改动**）：三处「想确认 确认…」重复，与一处半角引号
`'暂时性'` 改为「暂时性」。正文 13,856 → 13,847 字符。

页面返回：`新版本 b1ea5c59c7b0（父 6228c7467bf1）；验收 pass，交付已绑定该版本`，
复核栏同步为「机器验收：pass（本版 b1ea5c59c7b0，绑定本版正文）」，并存两行琥珀提示
（包生成于 `6228c7467bf1`、当前版本 `b1ea5c59c7b0`——包不含最新修订，请重新导出）。

离线核对（工作区文件，只读）：

- `report_versions.json`：选中版 `b1ea5c59c7b0b3e5…`，`parent_id = 6228c7467bf1a7ee…`，
  `adopted=True`、`draft_only=False`、`acceptance.overall=pass`、身份 `d962e1d3f4aecc24…`；
  其 `body` 与页面提交的修订稿（LF）**逐字符相同**；
- `deliveries[]` 追加登记：`delivered_sha256=745eb16715922f8a…`、
  `accepted_body_sha256=b1ea5c59c7b0b3e5…`、`report_version_id=d962e1d3f4aecc24…`；
- 旧批准（`review_state.json` 的 `60781cdc76e7`）**未迁移**到新版本——页面据此显示
  「计划评审：属版本 60781cdc76e7，当前版本无评审结论」。

## 3. 「按当前版本重新导出」：新包与旧包并存

页面操作：点「按当前版本重新导出」→ 页面回显
`已生成 deliverables_20260924_185945_3a152d.zip（16 个成员，包内字节自检 全部一致）`，
陈旧提示两行消失（导出块只剩「交付包 …（生成于 2026-09-24 18:59）」）。

离线核对（包内 `PACKAGE_MANIFEST.json` 对照磁盘/任务库）：

| 项 | 值 |
|---|---|
| 成员 | 17（16 冻结成员 + `PACKAGE_MANIFEST.json`；页面说的 16 = 冻结成员） |
| 逐成员 hash | `frozen` 16 项**全部一致**（无 mismatch） |
| 采纳正文 | `research_body_sha256=b1ea5c59c7b0b3e5…`（= 页面修订版） |
| 报告版本 | `report_version_id=d962e1d3f4aecc24…`（= 该版身份） |
| 绑定 | `binding_verified=True`、`logic_fingerprint=2e9f2c85a7179536` |
| 交付 MD | `delivered_md_sha256=745eb16715922f8a…` = 包内 `reports/report.md` = 任务库送达正文 = 工作区 `reports/report.md`（四处逐字节相同） |
| PDF | `pdf_sha256=34f398715a758b74…`，5,580,480 字节，**14 页、0 字符越界**，标题/关键句无缺失 |
| 图表/底稿/审计稿 | 与磁盘工作副本**逐字节一致**（`charts/*`、`working_paper.{json,csv}`、`audit/*.md` 5 份） |
| `drift` | 仅 `evidence/citation_evidence.json: changed_on_disk`（工作区那份是离线重入时写的，包内那份取自本次快照） |
| 旧包 | 磁盘上 9 个 `deliverables_*.zip` 全部保留（含本次之前的 `2a9922`） |

页面下载与包内字节同源（页面内 `fetch`，登录会话）：

- `GET /api/task/ui-706c5ef4a5/report.md` → 200，`sha256=745eb167…`，响应头
  `x-report-version-id=d962e1d3…`、`x-report-research-body-sha256=b1ea5c59…`、`x-report-draft=0`；
- `GET /api/task/ui-706c5ef4a5/pdf` → 200，`sha256=34f39871…`（= 包内 PDF）；
- `GET /files/ui-706c5ef4a5/deliverables_20260924_185945_3a152d.zip` → 200，
  5,621,827 字节，`sha256=a21fe35b3148027b…`（= 磁盘包字节）。

## 4. 本次页面复核发现并修复的两个缺陷

### 4.1 「下载交付包 zip」对重新导出的包恒 404

`web_ui._FILES_AUTHED_ROOT_RE` 只认 `deliverables_[0-9_]+\.zip`，而
`delivery_pipeline.repack_adopted`（页面「按当前版本重新导出」）为避免同秒撞名生成的
名字是 `deliverables_<8位日期>_<6位时刻>_<6位十六进制>.zip`——后缀含字母，白名单不匹配。
实机对照（同一会话内 `fetch`）：

| 路径 | 修复前 | 修复后 |
|---|---|---|
| `deliverables_20260923_011430.zip`（无后缀） | 200 | 200 |
| `deliverables_20260923_134027_e79e09.zip` | **404** | 200 |
| `deliverables_20260924_092225_2a9922.zip` | **404** | 200 |
| `deliverables_20260924_185945_3a152d.zip` | **404** | 200（5,621,827 字节，sha 与磁盘一致） |
| `deliverables.zip`（形状不符） | 404 | 404（仍拒） |

修复：白名单按**两个写入方**的实际命名放行
（`packaging_worker`：`deliverables_<8位日期>_<6位时刻>.zip`；`repack_adopted`：再加
`_<6位十六进制>`），匿名（分享链接）仍拿不到包。用例：
`test_financial_chain.TestFilesVisibilityPolicy` 新增两条（两种形状可下 + 形状仍受约束 +
白名单与写入方命名对齐）。提交 `06f7190`。

### 4.2 下载报告会让页面误报"包不含最新修订"

`_write_export_manifest` 在**每次下载 Markdown/PDF** 时也会重写 `export_manifest.json`
并盖新的 `generated_at`；`_export_payload` 的陈旧初筛只看"清单比包新两分钟以上"，
于是 18:59 重新导出的包被 19:09 那次下载写的清单判成陈旧：

- 现象：面板出现「包文件生成于 2026-09-24 18:59，早于最近一次装配——重装配未重建包，
  包可能不含最新修订，请重新导出」，而同一页的正文版号对比
  （`package_body_version_id == current_body_version_id == b1ea5c59…`）说明包就是当前版；
- 数据：清单 `generated_at=1790248148`（19:09）、包 mtime 18:59，差 563 秒 > 120 秒阈值；
  19:09 正是页面内两次下载（`report.md`、`pdf`）的时刻。

修复：时间顺序只当初筛，再用**身份**确认——清单 `body_sha256`/`report_version_id` 与包内
`PACKAGE_MANIFEST` 的 `research_body_sha256`/`report_version_id` 一致即视为"只是渲染"，
不判陈旧；身份不一致、或旧包没有身份可对（包内无清单）仍判陈旧（保留原有的
"重装配未重建包"与旧包时间戳兜底）。用例：`test_delivery_chain` 新增两条
（下载触发重写清单不误标 + 旧包仍按时间判）。提交 `18055cf`。

修复后同一页面复核：`package_stale=false`，导出块无琥珀提示，包下载 200。

## 5. 测试与回归

```
REDIS_PORT=6399 python -m unittest test_financial_chain   # 36 OK（新增 2 例）
REDIS_PORT=6399 python -m unittest test_delivery_chain    # 343 OK（新增 2 例）
REDIS_PORT=6399 python -m unittest test_auth_audit test_frontend_guards   # 61 OK
```

服务在修复后重启两次（`stop → deps --fix → start`，各 16/16 存活）；浏览器会话在重启后
仍为 `momang · 管理员`（会话落 SQLite，不因重启失效）。

## 6. 未验项（如实列出）

- **真人研究员评分**仍未进行（`docs/evidence/r4_scoring_handoff_20260924.md` 的 6 步交接、
  评分表待填）；页面上的「人工复核：待复核」正是这一状态，机器验收 pass 不代表已复核。
- 旧批准不迁移是**设计**（评审属旧版本，页面照实标注），不是缺陷；若要新版本带评审，
  需重新过 Critic 与人工批准。
- 面板「修改与重验」区的『未采用来源』会把**边界/推断句**一并列出（"本条只说明不能据此
  得出什么，不是事实主张"）——当前是逐句标注的副作用，条数与文案偏啰嗦，未改。
- 旧包（包内无 `PACKAGE_MANIFEST`）的陈旧判定仍依赖时间顺序，与历史行为一致。
- 主文 14 页未达 2–4 页软目标；真实 Redis 多进程并发、真实并发修订仍未实机验证。
- 本文件只记录这一轮页面复核与两处修复；R1–R4 的其余未验项见各自证据文档。
