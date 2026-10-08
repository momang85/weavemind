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

## 四、冻结假说卡（§17 的第一步，本批只产卡不建模型）

卡由**冻结产物**生成（`POST /api/research-study/hypothesis-card`），两张卡都在
[hypothesis_card.md](./hypothesis_card.md)；要点：

- **状态**：`draft_not_human_confirmed` —— **未经人工确认，因此不叫「事前假说」**；本入口**不代签**。
- **10 天压力是设定情景**（使用者假设），**不当历史信号**；不含综合信用分、不含 alpha 声明。
- 每家公司给：经营改善方向、利润/现金/周转解释（现金转化、应收/收入、存货/成本、负债/资产、
  毛利率差额）、原始来源（文件 + sha256 前缀）、可知时点（特征截止/结果评价截止/材料最晚可用时间）、
  下一同口径披露 3 项、反证条件 3 项。

## 五、未验证 / 未做（照实）

- **界面点击未执行**：应用可用（16/16 服务、HTTP 200、本批新页已构建进 dist），但工作台需要管理员登录；
  口令属用户凭据（README 明确没有固定默认管理员密码）⇒ 代理侧不读配置密钥、不改账号权限、
  不绕过登录，也**不把 HTTP/脚本调用说成页面点击**。恢复条件见 [ui_recovery.md](./ui_recovery.md)。
- 真实 Wind 导出/XLSX 直读、历史版本完整性、许可真实性：**未验证**（§16 同样列在"尚未证明"）。
- 事件/情绪的通用 NLP 准确率、实体识别、中文人工标注：**未做**；候选 ≠ 事实认证。
- 信用/反欺诈：只有**流程就绪**检查（模拟数据），**未训练、未校准**，无真实客户标签与授权合同。
- 回测账本仍是既有内部原型，**未验证策略有效性**；Qlib 未装、未委托。
- 5 分钟读者任务：本批入口面向研究者调试，**未做人工可用性评价**。
- 费用：本地确定性计算 **0 API 调用**；真实付费/等待/人工活跃时间**未测**。
