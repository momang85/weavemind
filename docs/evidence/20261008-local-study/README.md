# 10-08 本地研究试验入口：执行证据

批次：架构师 10-08 新批次（[三域规划 §16](../../三域金融研究架构规划_20261005.md) ＋
[离线框架 README](../../../examples/research_study/README.md)）。基线 `ffa1d02`（经营韧性批次**保留不动**）。
本批唯一成果：**现有工作台一个「本地研究试验」入口**（文件预览 → 明确截止/公司 → `run_study` →
折叠查看经营/事件/风险/内部附件），＋ 一张**冻结假说卡**（§17 的第一步）。
全部离线：0 API 调用、不联网、不建任务、不新增服务/worker/数据库/SDK。

## 一、入口（复用既有上传/鉴权/任务结果接缝，不写第二套）

- 后端 8 个薄端点（`web_ui.py` 末尾一节）：`sample` / `studies`（GET，需登录）、
  `upload` / `preview` / `run` / `verify` / `replay` / `hypothesis-card`（POST，仅管理员）。
  契约与复用面见 [entry_api.md](./entry_api.md)。
- 前端新页 `/research-study`「本地研究试验」（`frontend/src/pages/ResearchStudy.tsx`），
  侧栏入口与演示模式拦截一并接上；dist 已重建（`ResearchStudy-COoVdPMw.js`，19,432 B）。
- 硬门槛（服务端）：未确认不给运行（400 `preview_required`）；确认主体必须与**文件里读到**的一致
  （409 `subjects_unconfirmed`）；spec 引用的文件必须真在本次输入里（400）；同一研究不覆盖（`exists`）。

## 二、两个模拟公司案例（同一入口、同一份冻结输入）

`examples/research_study/study.json`（`synthetic: true`），特征截止 2025-04-30、结果评价截止 2025-06-30：

| 项 | 900001.SH（模拟公司甲） | 900002.SH（模拟公司乙） |
|---|---|---|
| 情景归母净利（假设成立时） | 160.00 万元 | 250.00 万元 |
| 维持目标所需收入变化（单因素） | −2.78% | −9.09% |
| 所需毛利率与基期之差 | −0.83 pp | −3.18 pp |
| 天数冲击额外占用（回款/库存各 +10 天） | **55.89 万元** | **99.45 万元** |
| 经营现金/归母净利 | 62.50% | 96.00% |
| 期末应收/收入、存货/成本、总负债/资产 | 15.00%／28.57%／54.17% | 21.82%／23.08%／61.36% |
| 异常核查线索 | 应收增长>收入增长 +60.00pp；库存增长>成本增长 +51.43pp | 库存增长>成本增长 +15.71pp |
| 事件候选 | earnings_guidance · positive（第 1/5/20 交易日基准调整 +0.76%／+3.76%／+14.60%） | orders_demand · positive（−0.45%／−2.23%／−8.68%） |
| 标签就绪 | credit/fraud `synthetic_contract_only`（**尚未训练**） | 同 |

结论形状与读数**随公司变化**（不是同一组数），并且全部只是**模拟数据上的流程验证**。

## 三、实际证据（本目录，逐条可复算）

- [entry_probe.txt](./entry_probe.txt)：入口端到端实录（预览→反例→运行→verify→replay→假说卡）。
- [preview_payload.json](./preview_payload.json) / [run_payload.json](./run_payload.json)：
  前端实际会渲染的载荷（原字段、单位、缺口、逐行问题、主体、两个截止、内部附件清单）。
- [verify_replay.json](./verify_replay.json)：`verify ok=true`（核对 12 个冻结产物字节）／
  `replay ok=true`（`expected_hash == actual_hash`，用冻结输入重算，**不要求原始文件仍在**）。
- [summary.md](./summary.md)、[study_manifest.json](./study_manifest.json)：内部研究摘要与冻结身份
  （来源文件 + raw_sha256、实现 hash、params hash、产物 hash、`research_draft`/`unreviewed`/不可对外）。
- [hypothesis_card.md](./hypothesis_card.md) / [hypothesis_card_payload.json](./hypothesis_card_payload.json)：
  **待人工确认的假说卡**（经营改善方向、利润/现金/周转解释、原始来源、可知时点、下一同口径披露、
  反证条件；卡hash `8dfcab6b0d40928a…`）。
- 定向测试 `test_research_study_entry.py`：**15 项通过**（含身份/时点反例）；
  连同架构师 §16 的四个文件 `test_wind_import/test_financial_events/test_risk_research/test_research_study`
  一次跑 **93 项全过**。
- CI 门禁：本批只补了**本批自己**的步骤（`test_research_study_entry.py`）。核过提交后
  **59 步 = 59 个已入库测试文件**、无悬空步骤、无遗漏（`build_and_ci.json`）。
  架构师 §16 的四个测试文件**尚未入库**（git 未跟踪）⇒ **本地**工作树跑 CI 覆盖守卫会 FAIL
  （磁盘上有、ci.yml 里没有），干净检出不受影响；框架入库时须同步补它们的 ci.yml 步骤。

### 反例（都在本批测试里，不靠"看起来对"）

| 反例 | 结果 |
|---|---|
| 未预览/未确认直接运行 | 400 `preview_required` |
| 请求里写别的公司名 | 409 `subjects_unconfirmed`，并回显**文件里**读到的主体 |
| 输入里换一家公司、请求仍写原主体 | 以**文件**为准（主体=600000.SH），409 拒绝 |
| spec 引用未上传的文件 | 400，并指出文件名 |
| `filename` 含路径/`..` | 400（防目录穿越） |
| 篡改产物一个字节 | `verify ok=false`，`failed` 指出该文件 |
| 实现变化后 replay | 拒绝：`实现已改变；需明确生成新的研究版本，不给旧版本背书` |
| 可用时间早于披露时间 | 该行 `analysis_eligible=false`、`eligible_records=0`，问题码 `availability_before_disclosure` |

## 四、冻结假说卡（§17.5 的五组字段；§17.2 的 Q1 方法**只冻结、不计算**）

卡由**冻结产物**生成（`POST /api/research-study/hypothesis-card`），
[hypothesis_card.md](./hypothesis_card.md) / [hypothesis_card_payload.json](./hypothesis_card_payload.json)；
`card_version=hypothesis_card/1.1.0`，`hypothesis_id` 与 `card_hash` 都由**判断内容**算出
（`generated_at` 不参与 hash ⇒ 同一判断重复生成同 hash，判断变了 hash 必变）。五组字段：

| 组（§17.5） | 卡里有什么 |
|---|---|
| ① 身份与时点 | `hypothesis_id`、方法/公式版本（`q1-v1`，`status=fixed_not_computed`）、口径（合并/CNY/t−1→t→t+1）、生成与冻结时间、特征截止与结果评价截止、来源文件+raw_sha256、`study_id`/`result_hash`、披露/可用/版本可用时点 |
| ② 研究判断 | 每家一句**可反证机制**、预期方向、观察指标（`Y_op` 符号）、**目标期**（下一同口径年度；不混 H1/累计/单季）、替代解释 3 条、反证条件 3 条、下一材料 3 项 |
| ③ 试验约定 | 公司池（**只从文件读**）、主终点（`sign(Y_op)`）/次终点（20 日基准调整）、窗口、两条对照基线、留出（未做）、候选/变体账（1 份）、是否人工确认（否）、PIT 说明 |
| ④ 后续结果 | 验证截止 + 成熟/待成熟/缺失/不可比**状态位**；本批为模拟流程样例 ⇒ `状态=待数据`，`实际读数=None`、机制判定 `未决`、价格反应 `待数据`（**不写实际读数、不假造成熟**） |
| ⑤ 使用状态 | `synthetic`、`provenance_kind=流程样例（模拟）`（真实材料事后生成则标**历史重建/回顾解释**，并提示检查生成时是否已看到后续结果）、内部研究、`research_draft`、机器检查（`verify_study ok=True`，核对 12 项）、人工复核 `unreviewed`、不可对外 |

要点：**10 天压力是设定情景**（使用者假设），**不当历史信号**；不含综合信用分、不含 alpha 声明；
`E` 明确标为"自定义经营余额（融资与所得税前）"，**不是法定营业利润/EBIT**，也不是对既有文献因子的复制。

## 五、对 §17.7 A 批的逐条对照

| §17.7 A 要求 | 本批做法 |
|---|---|
| 现有界面一条本地研究入口 | `/research-study`「本地研究试验」页 + 8 个端点（见 [entry_api.md](./entry_api.md)） |
| 复用已有上传/鉴权/参数/任务结果接缝 | 上传同形 base64 契约与单件上限；GET 需登录 / POST 仅管理员；参数（两个截止）显式入 spec；结果列表/`verify`/`replay` 复用既有实现 |
| 调用 `wind_import.preview_file` → `study.run_study/write_study` | 预览与运行都走 `study.run_study`（`preview_file` 在它内部；**同一条数值路径**），无第二套解析；预览不写盘 |
| 公司和两个截止须显式 | 预览回显**文件里读到**的主体，运行需 `confirmed_subjects` 一致（否则 409）；`feature_cutoff`/`evaluation_as_of` 显式可改 |
| 展示两公司模拟经营/事件/风险读数及内部附件，缺域/模拟/未复核状态清楚 | 四块折叠展示 + 13 个内部附件清单；缺域（本样例无缺）走既有缺口；`synthetic`/`research_draft`/`unreviewed` 三处状态常驻 |
| 只预览明确文件，不扫描配置 | spec 里的文件名必须**纯文件名且真在本次输入目录**（否则 400）；不读 `config.json`/不改用户权限 |
| 保存选定输入与参数，改参数生成新版，旧版保留 | `inputs.json`/`study_manifest.json` 冻结 spec+来源+实现+产物 hash；同研究 `exists` 不覆盖，改参数/输入得新研究ID |
| 复用 verify/replay | 两端点薄封装 `verify_study`/`replay_study`；`verify ok（12 项）`、`replay 同实质结果` |
| UI 登录未恢复时离线装配 + 列具体恢复条件，不绕鉴权/不称已点击 | [ui_recovery.md](./ui_recovery.md)、[app_reload.json](./app_reload.json)；如实标"界面点击未执行" |
| 研究产物本地内部使用，不自动采纳/对外打包 | `external_delivery.allowed=false`、`adopted=false`、不进 MD/PDF/ZIP 交付包 |
| 附**一张**冻结假说卡 | 本目录的卡（两家模拟公司）；**Q1 读数与核验留到 B 批**（§17.7 B 上限两个纯模块） |

## 六、未验证 / 未做（照实）

- **界面点击未执行**：应用可用（16/16 服务、HTTP 200、本批新页已构建进 dist，且已在提交后
  `stop`+`up` 重新加载），但工作台需要管理员登录；口令属用户凭据（README 明确没有固定默认管理员密码）
  ⇒ 代理侧不读配置密钥、不改账号权限、不绕过登录，也**不把 HTTP/脚本调用说成页面点击**。
  恢复条件见 [ui_recovery.md](./ui_recovery.md)。
- **Q1 未实现**（按 §17.7：B 批才做）：`E/O/Y_op` 只作为**冻结的方法定义**写进卡，
  **不计算**任何读数、不比较基线、不扩方法搜索；两个纯模块（`mechanisms.py`/`evaluate_mechanisms.py`）
  尚未创建。
- **真实洋河/三一的两张研究卡**（§17.6 第 1 条提到的"真实洋河/三一用当前冻结披露写两张研究卡"）
  本批**未做**：它们的三表要变成 study 的输入文件才走同一入口，那属于导入器职责
  （§16 明确"勿再写第二套导入"）；请确认是由 Wind 到达后的预览路径接入，还是用既有缓存另建转换。
- 真实 Wind 导出/XLSX 直读、历史版本完整性、许可真实性：**未验证**（§16 同样列在"尚未证明"）。
- 事件/情绪的通用 NLP 准确率、实体识别、中文人工标注：**未做**；候选 ≠ 事实认证。
- 信用/反欺诈：只有**流程就绪**检查（模拟数据），**未训练、未校准**，无真实客户标签与授权合同。
- 回测账本仍是既有内部原型，**未验证策略有效性**；Qlib 未装、未委托。
- 5 分钟读者任务：本批入口面向研究者调试，**未做人工可用性评价**。
- 费用：本地确定性计算 **0 API 调用**；真实付费/等待/人工活跃时间**未测**。
