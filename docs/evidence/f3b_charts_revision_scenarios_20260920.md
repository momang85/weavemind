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
  跨修订可直接比对。`test_scenarios.py` 把它纳入回归（含"图注不得含读数"的不变量）。

**三场景读数**：

| 场景 | 交付 | 证据 located | 缺口 | 图 | 关键读数 |
|---|---|---|---|---|---|
| normal_growth | **verified** | 4 | 0 | 5 | 覆盖"高于"；验收 pass；PDF 5 页无空白页 |
| loss_mixed_units | draft | 2 | 2 | 2 | 覆盖"不表示利润有现金支撑"；底稿记"币种/单位不一致：不可直接算同比"与"基期为负：不具可比含义"，故不生成同比 |
| missing_footnote_asof | draft | 0 | 4 | 4 | 超截止文章判 `after_as_of` 并写明原因；覆盖"低于" |

**视觉**：PDF 层用确定性判定（页数/空白页/文字量，三场景均 pass）；图表层此前用 chart_qa
残余（全 publish）+ 对渲染 PNG 的人工核对（基线 chart_3 的缺陷已确认消失、修复后的
对比图/比率分面图逐张核对：类别正确、数值与底稿一致、轴标题齐备、两期顺序与配色一致）。
`visual-judge` 子代理本次因账号连接不可用未能启动（回退为人工核对，读数见上）。

## 未验证 / 遗留

- **推送未完成**：提交 `80d65d1` 在本地，远端仍 `02aa6b9`。读数：`git ls-remote`（带 `gh` 凭据）
  通、`curl` 对 GitHub 的 GET/POST（含 569KB body）都通（404/401 快速返回），但 `git push`
  的 receive-pack 上传在 ~4 分钟后被超时终止——代理/直连、HTTP/1.1/2、`http.postBuffer`
  单次缓冲、`gh` 凭据注入四种组合都试过，均在同样位置挂住（今天早些时候同一环境推送成功过两次，
  判断是网络路径变化）。另外凭据缓存已失效（`GIT_TERMINAL_PROMPT=0` 时报
  "could not read Username"），用户终端里直接 `git push` 会弹 x-access-token 弹窗。
- 修订入口（B3）只做了接口联调与源码级检查，**未做一次真实人工修订的端到端实机**
  （需要一份新实机任务；按指令实机要预声明预算，留到下一轮）。
- 图表分面后的**实机**产出未跑（场景跑法是离线渲染；真机下一轮确认）。
- 场景②的"单位不可换算→不生成比率"由既有单测覆盖（`test_ratio_not_produced_when_units_not_convertible`）；
  场景里体现的是"跨期单位不一致 → 底稿判问题、不硬算同比"。
