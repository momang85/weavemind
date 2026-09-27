# C4 第一步：便携运行包重建与产物记录（2026-09-27）

批次：**C4**（`docs/真实研究闭环与阶段D收口_20260927.md` §4-C4 第 1 条）。
本文件只记录**可离线完成**的部分（重建包 + 同版核对 + 评分交接），
真实样本与真人评分仍缺，见 §5/§6。

## 1. 为什么现在才重建

C4 明确"只在前述代码批次稳定后统一重建便携包，不在每次文档修订后重打包"。
C0–C3 与 Redis 修复已全部提交（`2519d9a..761291b`，18 个提交），故本次重建一次。
上一版包 `weavemind-2026.09.27.1` **不含 S3 披露摄取层**（09-27 记录在案），
本版把它与 C1/C2/C3 一并装入。

## 2. 构建记录（C4 要求逐项）

| 项 | 值 |
|---|---|
| 源码提交 | `761291b13f8d0d2691cae2d6a87f249f4e1a0f90`（branch `main`） |
| 工作区脏状态 | 已跟踪改动 **7** 个（含架构师/用户既有脏文件 `templates.json`、`evals/cases/auto_grown.json`、3 篇 docs、`.gitignore`）；未跟踪 **48** 项 |
| 已跟踪改动差异指纹 | `git diff` 的 sha256 = `0e578233a64c97ef8c167159f902cd1a…`（156,216 字节） |
| 前端产物 | `frontend/dist` 24 个文件，整树 sha256 `19b71d219b894925c10de3755da85579…`；`index.html` 引用 `index-DVOwW9rR.js` |
| 依赖锁（Windows） | `requirements-runtime-win.lock`（**108** 条）sha256 `659e443495d2d557c214edb543fdd4d5…`；另存 `requirements-runtime.lock`（113 条）与 `requirements.lock`（257 条） |
| 包 | `dist/weavemind-2026.09.27.2-win-x64.zip` |
| 包 sha256 | `f246064ae482eb5812d1b426eab8a16167a605ee5c092ceea33556c9f9a5ddcb` |
| 包大小 / 条目 | **289,472,753** 字节 / **23,754** 条目 |
| 构建命令 | `python scripts/build_run_package.py --version 2026.09.27.2 --out dist --offline-dir .weavemind/downloads` |
| 构建时耗 | 约 8 分钟（复用缓存：Python 3.11.9 embed 10.7MB + 便携 Redis 13.3MB；108 个 wheel 走阿里云镜像） |

构建报告（`dist/build_report.json`）关键项：

- `secrets: []`、`dev_paths: []` —— 包内**无密钥、无开发机绝对路径**；
- `wheels: {packages: 108, sdist_only: ["jieba"], sdist_install: {ok: true, packages: ["jieba==0.42.1"]}}`
  —— 两个阶段（先纯 wheel、再补 sdist 纯 Python 包）都成功；
- `redis: {sha256: 4e8f2f95…, matches_reference: true}` —— 便携 Redis 摘要与参照一致；
- `sources.count: 160` —— 应用源码 160 个文件；
- 包内自检：用**包内解释器**验证 Python `3.11.9` / OpenSSL `3.0.13` / SQLite `3.45.1`，
  且 `project_root_on_path: true`。

## 3. 包内容核对：本轮工作确实进包（逐项验证）

| 检查 | 结果 |
|---|---|
| S3 摄取层 `adapters/disclosure_ingest.py`（上一版**缺**） | ✅ 在包内 |
| C1 补材料 `material_intake.py` | ✅ |
| C2 问题契约 `question_assessment.py` | ✅（内容含 `QUESTION_RULES`） |
| C3 新人可行动状态 `actionable_state.py`（本轮新增） | ✅（内容含 `waiting_material`） |
| Redis 修复（显式 `--dir` / 不存快照） | ✅ `dep_check.py` 含 `--stop-writes-on-bgsave-error` |
| H1 搜索截止 | ✅ `adapters/search_runner.py` 含 `read_with_deadline` |
| 前端产物与仓库一致 | ✅ 包内与仓库 `index.html` 都引用 `index-DVOwW9rR.js`；引用文件均在包内 |
| `scripts/scoring_handoff.py`（本轮新增） | ⛔ **不在包内** —— 构建器只打包**应用运行时**源码（`scripts/` 属维护工具，不进用户包），这是预期行为 |

## 4. 同版核对与评分交接（C4 第 3 条）

新增 `scripts/scoring_handoff.py`：从工作区**实读**①当前采纳版本 ②最新交付包（含 sha256）
③包内 `PACKAGE_MANIFEST.json` 的 `report_version_id`，当场做同版核对。

对真实样本洋河任务 `ui-706c5ef4a5` 实测（本会话生成 `docs/evidence/scoring_handoff_20260927.md`）：

- 当前采纳版本身份 `d962e1d3f4aecc24…`（正文 `b1ea5c59…`，验收 pass 且**绑定本版正文**）；
- 交付包 `deliverables_20260924_185945_3a152d.zip`，sha256 `a21fe35b3148027b…`，5,621,827 字节；
- 包内 `report_version_id` = `d962e1d3f4aecc24…` = 当前采纳版本 → **同版核对 ✅**；
- 包内 Markdown / PDF 各自 sha256 一并列出。

**旧链接已修**：`r4_scoring_handoff_20260924.md` 手写的包名是 `…20260924_092225_2a9922.zip`
（旧包），已在页首加"已被取代"指针并给出当前包名；历史记录不改写，只做更正。
交接单从此**不可能**指向旧包——不同版时它会打印"**不可用于评分**"。

## 5. 未完成：真实样本（需付费运行，本会话按纪律未做）

C4 要求"通过同一个正常入口保留三个样本"：①真实非金融公司可回答三问；
②资料不足 + 补材料恢复；③平安等金融/未知主体的适用性边界。当前状态：

| 样本 | 现状 |
|---|---|
| ① 三问可回答 | ❌ 尚无：现有真实样本（洋河 `ui-706c5ef4a5`）必答 0–1/3 完成、`research_draft` |
| ② 资料不足 + 补材料恢复 | 🟡 C1 已在 **真实编排器**上跑过补材料链路（准入 `user_file`、61 小节 8 条证据、重投幂等），但"资料不足 → 补 → 继续"的完整三问闭环未验 |
| ③ 金融/未知主体边界 | 🟡 有历史平安实机稿（`Temp/…/live_task_evidence/report.md`，sha256 `40054a6f…`）与 C2 的四个适用性用例；该稿**早于本轮修复**，不能当新效果证据 |

**指令明确"不新增付费研究生成"**，故本会话不发起这三条实机；它们与真人评分一起
构成阶段 D 的剩余出口条件。

## 6. 未完成：真实运行实例与新包的关系（C4 明确要求记录）

- 当前在跑的服务**全部启动于 22:58:28**（用户重启 Redis 时一并拉起），
  因此加载的是**那一刻的工作树**：最后一个早于该时刻的提交是 `b034c4b`（22:54:23）。
  即运行实例**包含 C3 与首版 Redis 修复**，**不包含** `e282090`（修正后的 Redis 启动参数）、
  `45c3d1d`、`761291b`。
- 本次包构建自 `761291b`（+脏改动） → **包比运行实例新**。要让"实际运行实例 =
  包内代码"成立，需在重启后再验收（部署指南口径）。
- 为此新增 `code_version.py`：从 `.git` 读**短 HEAD**（工作区脏则加 `+dirty`），
  取不到（包内没有 `.git`）如实返回空串。它已被提交时间线使用
  （`orchestrator_v2._instance_identity()` → `submit_timeline_json.code_version`），
  于是"这条请求被哪份代码处理"从此**可查**，不必再靠"我记得重启过"。
  实测本仓库当前值为 `761291b+dirty`（与 `git rev-parse --short HEAD` 一致）。

## 7. 验证

| 项 | 结果 |
|---|---|
| `test_deploy_manifest`（新增评分交接 4 例 + `code_version` 5 例） | **39 OK** |
| 包内自检（构建器内置） | 密钥 0 / 开发路径 0 / 包内解释器 verify 通过 |
| 包内容核对（§3 逐项） | 全部 ✅（`scripts/` 一项为预期不进包） |
| 前端一致性 | 包内与仓库 `index.html` 引用一致 |

## 8. 遗留（照实）

1. **`clean_env_verified: false`** —— 干净环境（无 Python/Node/Docker）验收属 N4，
   构建报告自己写明"不代表已通过"；本次没有在干净机器上跑包内验收。
2. 三个真实样本（§5）与**真人 F3 评分 ≥8/10**（每份，且无严重事实/语义/溯源错误）未做；
   空表或模型自评不能代替。
3. 第二家公司（三一重工 600031）真实原文仍未取得（受限检索候选 0，如实留待）。
4. 包比运行实例新（§6）：需重启一次服务后，才谈得上"同包验收"。
