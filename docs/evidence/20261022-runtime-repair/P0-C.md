# RUNTIME-1022 第三批（P0-C）：来源准入 + 研究契约修复 — 实际修复与验证记录

> 本批全部改动**只在工作区源码**，未 `git commit`/`push`，未重启在线实例。
> 结论先行：**新代码验证通过（含 6 项必回归），在线生效待受控重启**（在线实例 PID 24572
> 自 2026-10-22 17:08 运行旧代码，本批不重启）。

## 1. 修复范围

### 1a. 来源链（候选/待核验/采用/剔除四分）

| 文件 | 改动 |
|---|---|
| `source_admission.py`（新） | 准入唯一谓词：`admit(candidate, contract, *, body)` → `{url, stage, admission, purpose, usable_as, subject_state, document_period, published_at, doc_type, reasons, need_body_verify, verified_with_body}`；`admit_many` → `{admitted, pending, rejected, background, counts}`；判据四项：**主体 / 期间 / 文档用途 / 资料截止日**；四阶段 `candidate → pending → admitted/rejected`，逐条 `reasons` |
| `orchestrator_v2.py` | `_filter_source_items` 不再 fail-open：委托 `_partition_source_items`（市场类任务走原路；公司研究走 `admit_many`；待核验打 `待核验：…` 前缀剔除；剔除逐条理由；全不适用时每条补 `（目标主体：…）` 后缀并整批 reject，不再"退回关键词放行"）；`_step_context_after_admission` 把被剔除 URL 从步骤上下文剥离、给采用/待核验条目打 `_admission` 标记，全剔除时步骤 FAILED；`_pick_fetch_url` 统一准入闸门（rejected→跳过；宏观/行业背景→跳过；admitted→+4；pending→-1）；`_candidate_inadmissible` 只在合法全称 rescue 失败后才剔除（集团母公司仍拒） |
| 持久化 | `source_exclusions.json`（剔除）与 `source_pending.json`（待核验）分账本；报告步同时写两份，`_task_sources_lock` 串行、URL 去重；待核验材料**不进采用桶、保持可核验**，不足元数据时不自动采信也不超额重试 |

### 1b. 契约链（意图判定 + 表单/后端一致）

| 文件 | 改动 |
|---|---|
| `research_intent.py`（新） | 自由文本意图四分 `general / company_research / out_of_scope`；状态 `not_applicable / needs_contract / in_scope / out_of_scope`；`research_request_json` 缺失时**不再默认 `not_applicable`**，改为文本判据 + 问题清单；缺契约字段向用户要（不编造公司/代码/期间/口径）；界外（港股/美股/金融业/三期+）给边界说明 + 手工材料路径；合法一般任务保留 `not_applicable`；回填 `backfill:"not_attempted"`（历史无契约任务只读注解，不静默补） |
| `web_ui.py` | `_publish_task` 提交即判：`needs_contract` 时追加 `contract_missing` 事件；返回与 Redis 消息带 `research_intent`；任务页 payload 带 `research_intent`（只读、零 LLM、失败仅告警） |
| `delivery_pipeline.py` | 新研究状态 `in_scope_no_structure`（范围内研究、检查未执行，待任务产出后重评）；无结构分支不再一律 `not_applicable` |
| 前端 | `frontend/src/lib/researchGoal.ts`（`looksLikeCompanyResearch` / `intentNotice`，形态学判据，无公司名白名单）；`SubmitPanel.tsx` 自定义目标下的琥珀提示；`TaskConsole.tsx` 提交后日志提示 |

**红线自检**：无公司关键词特例、无站点黑名单——判据只有形态学正则（后缀/代码/期间）+ 通用披露词；主体判定复用 `facts._norm_name`，`issuer_form_only` 只用于把 mismatch 升级回 ok（识别全称），从不制造 mismatch。

## 2. 验证（真实退出码，无 tail 掩码）

| 验证 | 结果 | 退出码 |
|---|---|---|
| `python test_source_admission.py`（新 19 用例） | OK | 0 |
| `python test_research_intent.py`（新 14 用例） | OK | 0 |
| `python test_source_exclusion_registry.py`（扩至 22 用例） | OK | 0 |
| `python test_orchestrator_v2.py`（106 用例） | OK | 0 |
| `python test_delivery_chain.py`（425 用例 + 重放 11/11） | 424 OK + 1 联网用例环境失败（见 §4） | 1 |
| `python test_workspace_addressing.py`（扩至 17 用例） | OK | 0 |
| `python -m py_compile web_ui.py orchestrator_v2.py source_admission.py research_intent.py delivery_pipeline.py workspace.py` | 无输出 | 0 |
| 前端 `node --experimental-strip-types --test frontend/tests/researchGoal.test.mjs`（20 用例） | OK | 0 |
| 前端 `npx tsc -b` / `npm run build`（验证后 dist 已还原到 HEAD，见 §5） | `built in 3.68s`，`TaskConsole-*.js` 含提示文案、共享 chunk 含 notice | 0 |
| CI YAML `yaml.safe_load` 解析 + 新 4 步就位 | OK | 0 |

### 六项必回归（全部在 `test_source_admission.py` / `test_source_exclusion_registry.py` / 手工对照）

1. **传奇影业错拿总局宏观公报** → `test_macro_bulletin_is_rejected_for_company_research`（宏观用途直接拒，不进公司证据）+ `test_fetch_selection_never_picks_macro_bulletin`（候选列表混入公报时取用 cninfo 年报）。
2. **伊利错拿 SKF 6008 轴承页** → `test_bearing_product_page_is_rejected_after_body_check`（正文核验后仍 subject_mismatch → rejected；此前因 URL 含 `6008` 被误救，现 `_norm_subject_hit` 只比归一化公司名）。
3. **主体+期间正确的官方披露正例** → `test_full_legal_name_is_admitted`（cninfo 全称年报 admitted，`usable_as=evidence`）。
4. **待核验材料** → `test_macro_bulletin_without_body_is_pending_not_admitted` / `test_pending_material_does_not_enter_bucket_but_stays_verifiable`（无正文 → pending，有界核验，不自动采信）。
5. **全部候选不适用** → `test_all_rejected_bucket_is_empty_not_fail_open`（空桶，不再放行）+ `test_all_rejected_marks_step_failed`（步骤 FAILED，理由见剔除账本）。
6. **下游上下文无被剔除来源** → `test_rejected_urls_never_reach_step_context`（被剔除 URL 从步骤上下文剥离）+ `test_source_exclusion_registry.py`（剔除/待核验分账本并入交付包）。

### 范围内/界外判例（`test_research_intent.py`）

- "研究伊利股份 600887 …"（缺结构）→ `needs_contract`（字段清单提问，`source=text`）；
- 带全结构 → `in_scope`；港股/美股/金融业/三期 → `out_of_scope` + 边界说明；
- 未标注市场的外国公司 → `needs_contract`（问而不猜）；
- "总结这段文字" → `general` + `not_applicable`；历史无契约任务 → `contract_missing` 只读注解 + 修复建议（`backfill:"not_attempted"`）。

### 手工对照（伊利目标，四候选混入，`admit_many` 实跑输出）

```
admitted  company_financial  static.cninfo.com.cn/finalpage/2025-04-29/8.PDF  主体与期间均适用
pending   macro_background   stats.gov.cn/sj/zxfb/202501/t1.html   宏观/统计材料：需抓正文确认是否涉及本主体（有界验证；即便提及也只能作背景）
pending   unknown            skf.com/cn/products/6008              主体未核验：需抓一次正文后按同一判据复核（有界验证）
pending   company_financial  static.cninfo.com.cn/finalpage/x/9.PDF 发布日未知：主体已核验但时点/期间不足，保留为待核验
counts: {'admitted': 1, 'pending': 3, 'rejected': 0, 'background': 0, 'total': 4}
```

带正文复核后：公报 → `rejected`（`usable_as=background`，宏观只能作背景）；SKF 轴承页 → `rejected`（`非本公司披露`）；与六项必回归一致。取用步（`_pick_fetch_url`）只挑 cninfo 年报；宏观/行业用途永不被取用。

## 3. 顺手修复的同批回归

- **P0-A 复查修复（`workspace.py`）**：`workspace_roots()` 不再把模块目录旁 `workspace/` 作为隐式回扫根——根被显式覆盖（env/数据根/`configure_workspace_root`）后，同名残留目录（`workspace/t1` 等）曾劫持定位，`test_prune_superseded` 因此失败。现历史回查只走显式登记（`WEAVEMIND_WORKSPACE_LEGACY_ROOTS` / roots.json）；新增回归 `test_overridden_root_never_ambient_scans_module_dir`。此后 `test_orchestrator_v2.py` 106/106 OK。
- **CI 补步**：`.github/workflows/ci.yml` 增 4 步（`test_source_admission.py`、`test_research_intent.py`、`test_net_diag.py`、`test_workspace_addressing.py`）；`AGENTS.md` 模块地图补 `source_admission.py` / `research_intent.py`，测试计数 93→96（438 用例）。

## 4. 环境性失败（非代码回归，如实记录）

- `test_delivery_chain.py` 联网用例 `test_fetch_quotes_parses_gbk_and_correct_indices`：本机 DNS 再次进入 fake-IP 段（`qt.gtimg.cn` → `198.18.0.30` / `2001:2::1e`，全部 5 个内容主机一致），SSRF 守卫按设计拒绝非公网地址。与 20261022 诊断快照同因；本机 DNS 前次（本会话早些时候）曾解析到公网 IP（state=ok），说明该条件在漂移。证据：`egress-current-20261023.json`。未改任何系统 DNS/代理配置。
- 该用例与 P0-C 改动无耦合（行情解析），同会话早前全量跑曾 425/425 OK。

## 5. 未做 / 边界遵守

- 未重启在线实例：**线上仍运行 10-22 17:08 的旧代码**；本批行为（准入闸门、意图提示、剔除账本）在线生效待受控重启。
- 前端 `frontend/dist` 为**入库构建产物**（.gitignore 明确"dist 入库"）：本批先重建验证（`tsc -b` + `vite build`，提示文案进 bundle）后已**恢复到 HEAD 提交的 bundle**（新哈希文件删除、tracked 文件还原，无仓库脏路径）。在线 GUI 继续提供 10-22 的既有 bundle，前端改动（意图提示）与后端一样**在线生效待受控重启/重建**；对新代码的前端行为以 `researchGoal.test.mjs` 20 用例 + build 证据为准。
- 未动：生产配置/凭据/系统 DNS/代理；旧证据目录；`_trial_*` 等他人路径；未签署任何人工复核。
