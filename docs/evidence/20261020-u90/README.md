# U90 证据索引（2026-10-20）

任务书：`docs/用户体验90分_执行任务书_20261020.md` ｜ 基线 `19054dd`（本批未 commit/push）

| 文件 | 内容 |
|---|---|
| `U90_交审报告_20261020.md` | **交审报告**：修改文件、逐项测试、真实用户路径、20 项评分+硬门槛、等待/人工/模型调用/费用、剩余阻塞 |
| `resume_ui_91677c7767.md` / `.json` | U90-B/D：真实失败任务（10-18 被演练真杀 Redis 拖死）网页恢复全过程：点击前入口文案、21:08:15 接管时间线、409 防重、终态有缺口、费用 |
| `resume_entry_ui-91677c7767.png` / `resume_done_ui-91677c7767.png` | 恢复前入口（重新接管执行·原因行）／恢复后（按钮消失·缺口原文） |
| `recovery_drill.md` / `.json` | 断点复用隔离演练：真 `task_state` + 真 `_execute_steps(resume_completed=True)`，仅桩付费派发；跳过 2/5 步、删工作区后复用 0 |
| `adopt_chain_verify.json` / `analysis_chain_cc853cf86b.json` | 采纳链（候选→采纳→装配→投影）与复算链版本一致性核验 |
| `workbench_adopted_cc853cf86b.png` / `material_load_error_cc853cf86b.png` | 工作台采纳态／材料面板断线三态（加载失败+原因+重试、12s 超时） |
| `downloads/` | 三包下载物：MD/PDF/ZIP + `downloads_summary.json`；`downloads/README.md` 逐包版本/哈希/成员说明（含 S3 恢复包） |
| `downloads_s3_resumed.json` + `downloads/ui-91677c7767-resumed.{zip,report.md,pdf}` | S3 恢复包核验：ZIP 200/483,290B/17 成员、包内自洽；**API 如实判 `package_stale: true`**（包体 ≠ 当前正文）；页面 MD/PDF 头与可读性 |
| `usage_cost.json` | 每任务账本（calls / tokens / cost_usd） |
| `architect-dispatch-42b6d8.png` | 架构师派发回执（任务书到达） |
| `architect_review_20261020.md` / `architect-repair-dispatch-20261020-2223.png` | **架构师独立审查 + 补修派发**（裁决：第 15/18 项记 0，自评上限 85，非审定成绩） |
| `u90_supplement_report.md` | **R1→R4 补修交审报告**：逐项根因/修法/测试/演练/边界、加载身份、重评、剩余未验 |
| `r1_resume_validity.json` / `r1_reasons.json` / `r1_instruction_diff.json` | R1：断点身份与产物校验的判据、拒绝原因、指令差异（反例与正例） |
| `r2_repack_drill.json` / `r2_pdf_pages/page{1,2,3}.png` | R2：同版重打包演练 30/30（新包身份链、22/22 成员哈希、旧包字节未变）、PDF 逐页目检 |
| `r2_live_page_state.json` | R2：**重启后真机页面口径**（三一/洋河逐包 historical + 原因行、`current=""`、无旧包冒充当前） |
| `r3_resume_ui_live.json` / `r3_ui_retry_after_503.png` | R3：真实浏览器同页 503→可重试→重试成功、后端 CAS 回滚实测、超时/双击自动化 |
| `r4_source_statement.json` | R4：声明层 12/12（真机冻结结构改前/改后、材料直供+检索降级、零来源）+ 流程腿 12/12（新包内声明=实际来源、清单身份==采纳、成员哈希、旧包只读指纹） |
| `recovery_drill_pre_r1.md` / `.json` | R1 之前的隔离演练对照（架构师反例的原始证据） |

## 快速复核命令

```powershell
# 逐文件回归（示例；不要整目录跑）
python test_task26_checkpointer.py
# 前端
cd frontend; node --test tests/resume.test.mjs tests/*.test.mjs
# 恢复入口行为（隔离演练复跑，见 recovery_drill.md）
python .tmp\u90_resume_verify.py
```
