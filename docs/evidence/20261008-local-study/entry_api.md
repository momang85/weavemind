# 本地研究试验入口（§16 接入）——接口与页面

本批唯一成果：把架构师已写好的**离线框架**（`quant_research/study.py` + `scripts/run_research_study.py`
+ `examples/research_study/`）接进**现有工作台**一个入口：文件预览 → 明确截止/公司 → `run_study`
→ 折叠查看经营/事件/风险/内部附件。不新增服务/worker/数据库/SDK，不改金融引擎，不重写巨文件。

## 一、后端（`web_ui.py` 末尾一节，约 380 行，全部薄封装）

| 方法 | 路径 | 作用 | 鉴权 |
|---|---|---|---|
| GET | `/api/research-study/sample` | 内置两公司模拟样例的 spec 与文件清单（只读） | 需登录 |
| GET | `/api/research-study/studies` | 已保存的内部研究产物列表（研究ID/截止/模拟/状态/附件数） | 需登录 |
| POST | `/api/research-study/upload` | 保存上传文件到本次研究的输入目录（复用材料入口的 base64 契约与单件上限） | 仅管理员 |
| POST | `/api/research-study/preview` | **只读**预览：逐文件原字段/单位/缺口/逐行问题 + 主体 + 两个截止 | 仅管理员 |
| POST | `/api/research-study/run` | 确认后运行并保存内部产物（同研究不覆盖） | 仅管理员 |
| POST | `/api/research-study/verify` | 复用 `verify_study`：核对冻结产物字节 hash | 仅管理员 |
| POST | `/api/research-study/replay` | 复用 `replay_study`：用**冻结输入**重算（不要求原始文件仍在） | 仅管理员 |
| POST | `/api/research-study/hypothesis-card` | 由冻结产物生成**待人工确认**的假说卡（§17 的第一步） | 仅管理员 |

复用面（不新造）：

- **鉴权**：直接落在既有 `do_GET`/`do_POST` 门内（GET 需登录、POST 仅管理员），没有另写一套；
- **上传契约**：与 `POST /api/task/<id>/material` 同形（`{filename, data(base64)}`）与同一单件上限
  `material_intake.MAX_BYTES`，另加件数与总量上限；只管保存原件，不解析、不投递、不建任务；
- **任务/结果接缝**：study **不是任务**（不投 Redis、不调编排器/模型），产物目录与 CLI 共用
  `tmp/research-studies/`（可用 `WEAVEMIND_RESEARCH_STUDY_DIR` 覆盖），列表/核对/重放沿用既有实现。

硬门槛（服务端判定，不靠前端自觉）：

1. `run` 必须带 `confirmed: true` 与 `confirmed_subjects` → 否则 400 `preview_required`；
2. `confirmed_subjects` 必须与**文件里读到**的主体完全一致 → 否则 409 `subjects_unconfirmed`
   （请求里写别的公司名不会改材料身份）；
3. `spec` 里引用的文件必须**真在本次输入目录**里（纯文件名、无路径分隔符）→ 否则 400；
4. 已存在同一研究ID → **不覆盖**，返回 `status=exists` 与旧目录；
5. 产物一律带 `research_state=research_draft`、`human_review=unreviewed`、
   `external_delivery.allowed=false`；入口**不采纳、不进 MD/PDF/ZIP 交付包**。

## 二、前端（`frontend/src/pages/ResearchStudy.tsx`，新页 + 侧栏一项）

- 路由 `/research-study`，侧栏「本地研究试验」（`AppLayout`）；页面用现有 `ui.tsx` 原语（Card/Alert/EmptyState）。
- 四段式：① 选定输入（内置样例 / 上传，逐文件选用途）→ ② 明确两个截止 + 逐个勾选**文件里的公司** →
  ③ 运行并保存（未确认时按钮禁用且服务端也会拒）→ ④ 已保存产物列表。
- 运行后**折叠**展示：经营（各公司各模型运行与读数）、事件候选、历史检验、风险特征与标签准备、
  内部附件清单；另按钮：`verify` / `replay` / **冻结假说卡**（展示卡正文）。
- 顶部常驻告示：模拟数据 ⇒ 只验证流程；内部研究草稿、人工未复核；不自动采纳、不进交付包、不对外分发。
- 演示模式：新前缀 `/api/research-study` 已加入 `lib/demoGuard.ts` 拦截清单（否则页面会绕过"演示不写数据"的承诺）。

## 三、构建

`cd frontend && npm run build`（`tsc -b && vite build`），本批新增 chunk
`dist/assets/ResearchStudy-COoVdPMw.js`（19,432 B）与更新后的 `index-CWNpexyZ.js`；dist 与源码一并提交。
