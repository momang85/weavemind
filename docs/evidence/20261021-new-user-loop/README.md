# 新用户真实闭环（2026-10-21）— 证据索引

工单标签：新用户闭环（全新工单）。目标：一个真实新用户视角的端到端闭环（真实浏览器 + 真实服务 + 全新任务），并对两个历史缺口给出实机正例或如实记「未验 + 原因」。
本目录证据由 **全新账号 + 全新任务** 产生；旧批次证据（`20261020-u90/`、`20261018-phase3-e2e/`）只作背景对照，不作为本批结论。

## 新任务与账号
- 登录账号：`momang`（既有）→ 本次新建 `u90new21` 并由页内角色入口提为管理员（密码只在会话内存，未落任何文件/日志）。
- 新任务：**`ui-d59152ebb9`**（洋河股份 002304.SZ · A股 · 2023–2024 · 合并报表 · 投研视角 · 资料截至 2025-04-30；参考资料留空），2026-10-21 06:41:57 UTC 经真实页面提交。
- 终态：`SUCCESS_WITH_ISSUES`（06:56:20 UTC；检索缺口按资料缺口处理 + step_degraded）。

## 本批结论（速览）
| 项 | 结论 | 证据 |
|---|---|---|
| A 新用户开局（真实浏览器） | **达成**（新建账号 + 页内提权 + 首次提交全程页面操作） | 截图 01–09 |
| B 全链（新任务，八步） | **达成**（材料准入→确定性复算→生成候选→预览比较→采纳→导出当前包→页面下载 MD/PDF/ZIP；采纳第一次被绑定守卫拒绝并回滚、第二次成立） | 截图 10–17 + `per-item/*` |
| R2 同版核验 | **通过**（三处身份一致 + 25 个成员哈希全对 + 当前包非历史包） | `per-item/r2_same_version_check.txt`、`final_page_state.json` |
| R4 声明核验 | **通过**（免责声明含「本次外部检索降级运行…报告来源以实际采用的来源清单为准」；声明与实际来源并排） | `per-item/r4_declaration_vs_sources.json` |
| R1 断点复用正例 | **未验 + 原因**（失败注入被会话权限挡住；预算只剩 1 次不足以「制造失败+续跑」；不许用桩替代） | report.md §R1 |
| R3 失败重试与挂起（恢复路径） | **未验 + 原因**（无 FAILED 任务、恢复入口不可达）；同页失败→重试成功的**采纳流实机观察**已记录（非恢复路径，不冒充 R3） | report.md §R3、`per-item/adopt_fail_then_retry.txt` |
| 旧包/历史任务保护 | **通过**（17 个旧 zip、34 个旧 PDF 哈希前后一致；历史任务行未触碰） | `per-item/fingerprint_diff.json` |
| 费用 | 38 次 LLM 调用（failed 9）；价格表缺失 → 费用「未测」；授权真实运行 1/2 次（续跑 0） | `final_page_state.json` |

## 快速复核命令（本机）
```powershell
# 1) 同版核验：包内 manifest 身份 + selection 身份 + 25 成员哈希全对（只读解包，不动原包）
python -X utf8 -c "import zipfile,json,hashlib; z=zipfile.ZipFile(r'docs/evidence/20261021-new-user-loop/downloads/ui-d59152ebb9.zip'); m=json.loads(z.read('PACKAGE_MANIFEST.json')); s=json.loads(z.read('analysis/selection.json')); print('manifest.report_version_id =', m['report_version_id']); print('selection.adopted_identity =', s['adopted_identity']); print('members_all_match =', all(hashlib.sha256(z.read(k)).hexdigest()==v for k,v in m['frozen'].items()))"
#    期望：manifest.report_version_id == selection.adopted_identity == 9751ef20b489546be76da463f86fdad0e151a6fb04ffeb2d89a40d6deea2bb13，members_all_match = True
# 2) 交付三件套哈希（页面服务字节副本）
Get-FileHash docs/evidence/20261021-new-user-loop/downloads/* -Algorithm SHA256
#    md  = 034112ed306c9ea160f734877ef341c7f49ed15853a0a4d67609d9f233c059bc（== manifest.delivered_md_sha256）
#    pdf = 35c66f809833974447c046b00468ee1c35b7e7ff2e9683838207f1f88912212f（== manifest.pdf_sha256）
#    zip = 62d0c6fb15f571d70c0a012c280fe2c1c45873eb3558bf0aa3dcbaa87d7f2d53（== 页面服务字节）
# 3) 旧包/历史任务前后指纹
python -X utf8 -c "import json;d=json.load(open('docs/evidence/20261021-new-user-loop/per-item/fingerprint_diff.json',encoding='utf-8'));print('changed:', d['zips_changed'], d['pdfs_changed'])"   # 期望均为空
# 4) 任务状态
curl.exe -s -H "Cookie: <登录会话cookie>" http://localhost:8080/task/ui-d59152ebb9
```

## 目录
- `report.md` — 闭环路径、逐步截图清单、同版核验表、声明对照表、R1/R3 记录、指纹比对、费用、剩余项。
- `screenshots/01–17*.png` — 逐步页面截图（见 report.md 清单）。
- `downloads/` — 交付三件套字节副本（md/pdf/zip）+ 哈希（README 上表）。
- `per-item/` — 逐项 JSON/逐字记录（API 快照、同版核验、声明对照、采纳失败→重试、PDF 页文本、指纹 diff、工作台前后状态）。
- `fingerprint_before.json` / `fingerprint_after.json` — 前后指纹快照（17/18 zip、34/35 pdf、565/566 任务行）。
- `lead_verification_20261021.md` — **Lead 侧独立复核（只读复算）**：51 个旧工件哈希重算、包身份/成员哈希、R4 声明命中、llm_calls 行数；另含两处事实更正（二栈 30 个孤儿进程已由 Lead 清理、`downloads/*.pdf` 为冻结成员而非按钮字节）与看板 failed 列账实差异说明。
