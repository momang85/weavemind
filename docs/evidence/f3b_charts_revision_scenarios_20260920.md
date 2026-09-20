# F3-B 证据：图表分面/标签、结果页顺序、修订与同版导出、三类离线场景

2026-09-20。承接《三场景输出复核与F3执行指令_20260919》§F3-B（HEAD `02aa6b9`）。
四步全部落地：B1 图表 → B2 结果页 → B3 修订/导出 → B4 三场景与视觉判定。

## B1 图表按问题分面 + 标签修正

**修前（实机 `ui-f00775cfdf` 的 chart_3 基线，逐条确认）**：四个经济含义不同的比率
（归母净利率 30.24/23.11、经营现金流覆盖 61.2/69.37、资产负债率 25.42/23.24、
研发投入强度 0.88/0.37）画在同一 % 轴；`grouped_bar` 拿 `caliber`（年份）当系列，
而 `short_label` 对 >12 字标签**优先取口径**——"经营现金流对归母净利润的覆盖"（14 字）
因此变成类别 `2023年`/`2024年`：x 轴混入年份、覆盖倍数被拆成两个年组、分组与系列配对错位；
研发强度被压成不可见细条；`render_grouped_bar` 从不设轴标题。

**修后**：
- 规格③改为**按指标分面**：每个比率一条规格（两期对比、x=期间、y=`<比率>（%）`、unit=%），
  各带自己的 `question` 与**由数据算出的观察**（百分点表述 + 覆盖倍数的符号条件）。
  洋河样例：5 条规格（对比 1 + 同比 1 + 比率 3），渲染 `total=5 skipped=0`、**QA 残余为空（全部 publish）**。
- 标签与轴：类别优先取指标名（长则截断，不再退化成口径）；`render_grouped_bar` 补
  `set_xlabel/set_ylabel`。
- **分面图两期顺序**：`is_natural_order` 只认纯数字标签，"2023年"带"年"字 → 被按数值降序排成
  `2024年/2023年`（颜色与年份对应反转）。抽成 `chart_specs.is_period_labels`（唯一实现，
  脚本 import 复用），并只在标签多/长时才斜排刻度。
- manifest 与图注：渲染脚本把 `question/observation/caption/section_hint` 写进 manifest；
  简报图注 = `图 N：<question>；<caption>（数据同『财务对照』表与底稿）`。

**B1 顺带发现并修掉的真缺陷（由验收抓到）**：图注若直接放观察里的读数（如"下降 7.13 个百分点"、
"同比 38.85%"），这些数字会被验收计入报告数字且**不可溯源**——正常增长场景的溯源率从 ~100%
掉到 **69%（47/68）** → 验收 fail、交付被判草稿。改为：正文图注用**不含读数的 caption**
（数字留在图内与『同比与比率』块，那里带算式可溯源），界面面板仍显示完整 observation。

## B2 结果页：发现 → 缺口 → 证据 → 修改 → 导出

- 后端 `_get_task_page` 新增 `research` 块（只读 `report_structure.json` /
  `narrative_evidence.json` / `chart_manifest.json`）：`findings[]`、
  `gaps{required_data, evidence, citations, as_of, unproven}`、
  `evidence{located, missing_labels, excluded[], rejected_citations[], rules_version}`、
  `citations[]`（含 `admission`）、`locators[]`（结构化字段位置）、
  `evidence_locations[]`（管理层/附注解释的小节或页码定位）、`charts[]`
  （`file/grade/question/observation`）。
- 前端新增 `ResearchBriefPanel`，挂在正文之前，顺序即 **关键发现 → 缺口（三类分开）
  → 证据（准入/未采用原因/正文引用未准入/出处定位/字段位置）→ 图表（问题+观察，草稿级标注）
  → 修改与重验 → 导出版本绑定**。
- 实机读数（`ui-f00775cfdf`，浏览器）：payload `research` 就绪（findings 4、缺口五组、
  locators 9、charts 3）；页面截图确认顺序与内容——"关键发现（4 条，数字来自底稿）"里
  覆盖读数为**"当期经营现金流低于归母净利润"**（F3-A 的修复在 UI 上可见），
  缺口区列出"证据（未取得正文定位）：业务背景、经营变化解释、财务附注、风险因素"与
  "还不能证明什么"的具体材料清单。

## B3 修订入口 + 同版导出

- 前端 `ResearchBriefPanel` 提供轻量修订：整份正文替换或 find/replace → `POST
  /api/task/<id>/review/edit`（**复用既有接口**，不另造版本系统）→ 用响应里的
  `version_id/parent_version_id/acceptance/delivery` 显示"新版本 vs 父版本、验收结论、
  是否草稿"，并重取任务详情刷新页面。
- 导出白名单：交付包与导出清单落在工作区根目录，此前**不在** `/files/` 白名单 → 包下不来。
  现按文件名形状（`deliverables_*.zip`、`export_manifest.json`）对**登录用户**放行，
  路径穿越仍由 `_safe_workspace_path` 兜底。
- payload 新增 `export` 块：`manifest_version_id`、`current_version_id`、包文件名与生成时间；
  包早于当前版本时界面明确标注"包生成于 vX，当前 vY——包不含最新修订"，Markdown/PDF
  仍按当前版本导出（响应头 `X-Report-Version-Id`）。

## B4 三类固定离线场景 + 视觉判定

- `evals/scenarios/*.json` 冻结三份输入（契约 + financials + 抓取夹具 + 模型稿 + 评审输入 + 预期）：
  ①`normal_growth` 正常增长且证据齐全；②`loss_mixed_units` 亏损/经营净流出 + 跨期单位不一致；
  ③`missing_footnote_asof` 缺附注且资料截止不满足。
- `scripts/scenario_run.py` 跑**真实路径**（底稿 → 证据 → 图表规格+渲染子进程 → 交付装配 →
  导出 markdown/PDF/清单），产物写 `.weavemind/scenarios/<name>/`（运行期、gitignore），
  输出 `manifest.json`（每产物 sha256+字节、版本号、验收、缺口、图表分级、PDF 视觉判定），
  跨修订可直接比对。场景检查做成 `scenario_checks.py` 模块，由 CI 清单内的
  `test_offline_delivery.TestFrozenOfflineScenarios` 调用（仓库守卫要求每个 `test_*.py`
  都必须在 `ci.yml` 里，而工作流文件需要 `workflow` scope 才能推送——做成模块既进门禁
  又不新增工作流条目；也可 `python -m unittest scenario_checks` 单独跑），含
  "正文图注不得含读数"的不变量。

**三场景读数**：

| 场景 | 交付 | 证据 located | 缺口 | 图 | 关键读数 |
|---|---|---|---|---|---|
| normal_growth | **verified** | 4 | 0 | 5 | 覆盖"高于"；验收 pass；PDF 5 页无空白页 |
| loss_mixed_units | draft | 2 | 2 | 2 | 覆盖"不表示利润有现金支撑"；底稿记"币种/单位不一致：不可直接算同比"与"基期为负：不具可比含义"，故不生成同比 |
| missing_footnote_asof | draft | 0 | 4 | 4 | 超截止文章判 `after_as_of` 并写明原因；覆盖"低于" |

**视觉**：PDF 层此前只有"页数/空白页/文字量"，那只是**可解析性**，不是视觉通过（见 C1 批：
三场景分别缺 5/2/4 张图却全判 pass）。现在拆成两件事——`pdf_parse`（可解析性）与
`pdf_export`（导出完整性：引用图是否真嵌入、缺图占位、表头与数据列是否同列网格）。
图表层用 chart_qa 残余（全 publish）+ 对渲染 PNG 的人工核对（基线 chart_3 的缺陷已确认消失、
修复后的对比图/比率分面图逐张核对：类别正确、数值与底稿一致、轴标题齐备、两期顺序与配色一致）。
`visual-judge` 子代理本次因账号连接不可用未能启动（回退为人工核对，读数见上）。

## B3 实机验证（接口修订 / 重验验证，不需要模型调用）

在任务 `ui-f00775cfdf` 上走了两次**修订接口**（与结果页"修改与重验"同一实现），
由执行者发起、**不是研究员操作**：

| 项 | 读数 |
|---|---|
| 第一次请求 | `POST /api/task/ui-f00775cfdf/review/edit`，`{"find": "## 分析", "replace": "## 分析（人工复核：口径与缺口已核对）"}` |
| 第一次响应 | 200；新版本 `cc6f2190…` ← 父版本 `65ec49d1…`；`acceptance=pass`；`delivery.status=verified`、`draft=false`；`needs_reverify=false`；`wrapper_source=stored`（交付说明逐字节复用） |
| 旧结论不迁移 | 响应注记："修订版是新版本：旧验收、旧评审与旧批准都不迁移，须重新验证" |
| 同版导出 | `/api/task/…/report.md` → 200、11,739 字节、响应头 `X-Report-Version-Id=634f5de4a33c` 与页面当前版本一致 |
| 交付包下载 | `/files/ui-f00775cfdf/deliverables_20260920_104729.zip` → **200（449,041 字节）**——白名单修复前该路径不可下载 |
| 耗时 | 约 1 分钟（含重验与导出），无需重跑任务 |
| 第二次请求（纠正） | 同接口 `{"find": "## 分析（人工复核：口径与缺口已核对）", "replace": "## 分析"}` |
| 第二次响应 | 200；正文回到 `65ec49d1…`（撤销第一次的测试文案）；`acceptance=pass`、`version_bound=true`；交付 `verified`；历史版本保留 10 条 |

**这两次是"接口能用"的证据，不是人工复核已完成的证据**：修订由执行者用测试文案触发，
`cc6f2190` 里那句"（人工复核：口径与缺口已核对）"因此是**虚假的复核声明**，已用同一条
正常修订路径撤回；页面与交付物现在显示的是交付说明自带的
"本交付物未取得绑定计划版本的评审 PASS，须经人工复核后方可使用"。
机器验收（`acceptance=pass`、版本绑定）与人工复核（**尚无研究员批准记录**）在结果页
分开呈现，见 §C1-4。

## 未验证 / 遗留

- **推送已完成**：`dc84d0f`（F3-B 主体）、`b81565e`（文档）与随后的场景模块重构均已上
  `origin/main`。两点经验记在这里：①`git push` 会先调 GCM（`git credential-manager get`）
  等弹窗，而本机 GCM 无缓存凭据 → 表现为"推送挂住"；本次用
  `-c credential.helper=`（清空 helper 链）+ `gh auth token` 注入走通。②gh 令牌**没有
  `workflow` scope**，不能推 `.github/workflows/`；因此场景检查改为 `scenario_checks.py`
  模块（由 CI 清单内的 `test_offline_delivery.py` 调用），**不再需要改工作流文件**——
  这也是 CI 首轮失败的根因（守卫 `test_every_test_file_runs_in_ci` 抓到
  `test_scenarios.py` 不在门禁内）。
- **新实机被端点上限阻塞**：`POST https://ark.cn-beijing.volces.com/api/v3/chat/completions`
  → `429 SetLimitExceeded`（账号 2132184326 的 glm-5-3-flash 达到推理上限、模型服务已暂停，
  需在"模型激活"页调整或关闭"安全体验模式"）。按约定不切模型、不绕过；端点恢复后按
  洋河对公同契约跑一份（≤25 分钟 / ≤40 次调用），用于人工评分与分面图的真机确认。
- 图表分面后的**实机**产出因此未跑（场景跑法是离线渲染）。
- 人工评分表已备好：`docs/研究员评分表_F3_20260920.md`（五项 0–2 + 客观读数 + 打开方式，
  明确不由模型自评）。
- 场景②的"单位不可换算→不生成比率"由既有单测覆盖（`test_ratio_not_produced_when_units_not_convertible`）；
  场景里体现的是"跨期单位不一致 → 底稿判问题、不硬算同比"。
