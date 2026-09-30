# DeepSeek 执行状态

> ## 当前账（2026-10-01 · **主线改为分析卓越化 U1→U2→U3**）
>
> **当前目标**：按 [分析卓越化_经营驱动研究与标杆案例_20261001.md](分析卓越化_经营驱动研究与标杆案例_20261001.md)
> （唯一当前行动）交标杆案例：**U1** 洋河 2023/2024 业务驱动利润报告 → **U2** 同案例现金流补充资料调节桥＋反向情景
> → **U3** 复制三一。沿 Observation/Dataset/AnalysisPlan/ModelRun 与既有报告链扩展，**最多两个新算子**
> （`operating_drivers`、`cash_reconciliation`），不新增服务/worker/数据库；约 80% 投入分析能力与案例。
>
> **已收口（安全检查点，防护保留，不自动接续）**：T0-a 分享/会话凭据 `d325cc4`、T0-b 执行许可与取消
> `581dfe6`+`e6c4843`+`a3a4400`、T0-c 共享账本原子票据 `a4f516c`，三者远端 CI success、应用 16/16 + HTTP 200；
> **T0-d/T1/T2 不再自动推进**，旧反例留维护积压（需要时只修影响当前案例关键金额或触及安全边界的问题）。
>
> **U1 进展（本批：真实披露数已取全并试算闭合）**：证据
> [u1_yanghe_drivers.json](evidence/u1_yanghe_drivers.json)（脚本 `scripts/u1_yanghe_drivers.py`，可重复运行）。
> 归母净利 **−33.43 亿** = 毛利 **−38.01 亿**（收入规模 **−31.54**／毛利率 **−6.47**）＋ 毛利线以下净额 **+4.58 亿**；
> 两条桥闭合差都在 1e-6 元量级、ΔB 逐项闭合 0.00。ΔB 逐项：所得税 **+7.20**、税金及附加 **+4.43**、研发 **+1.80**
> 增利；公允价值变动 **−3.59**、管理 **−1.60**、财务（利息收入减少）**−1.44**、销售 **−1.29**、投资收益 **−1.09** 减利。
> 白酒：销量 **−16.30%**、隐含均价 **+3.93%**、毛利率 **−2.07pp** → 毛利下降以**规模**为主。
> 两条**互斥**切法（均为酒类口径，不可相加）：产品切 白酒 ΔGP −37.96 亿；地区切 省内 −12.87／省外 −25.21 亿
> （省外毛利率 **−3.35pp** vs 省内 **−0.43pp**）。
> **替代解释（会改变判断）**：实际税率 24.19%→**27.09%**（+2.90pp）——所得税的"增利"是利润下滑的被动结果，
> 按上年税率折算本应只有 22.11 亿，税率因素反而多吃掉约 **2.65 亿**；公允价值变动/投资收益属非经营因素；
> 隐含均价上升也可能是**产品结构**而非提价（不得直接称"提价效果"）。
>
> **U1 已实现（本批）**：算子 `financial_analysis/operators/operating_drivers.py` 注册为第六族
> `operating_drivers_v1`（对称分解＋毛利线以下逐项＋分段切法＋量价），两个固定铰链声明
> `component_ids` 并由独立 `components_gold` 逐项核对；未取到的明细进"未解释差额"、不摊派；
> 分段**按切法分开出**且**排除报表范围**（`合并` 是母项本身、`母公司` 是另一种范围）。
> **摄取贯通**：合并利润表 15 个行项目进 `annual_financial_tables.LABELS`；MD&A 的分产品/分地区/
> 销售模式两期收入、10% 以上表本期成本、实物销量走定向抽取 `adapters/operating_detail_tables.py`
> （表头形状不符即不取；上期成本按披露同比反推并标 `derived_from`；同一分段两套口径取**含成本**的表）。
> **正文已消费**：经营驱动卡给出**三段式**——三项最大贡献（亿元）／分段四切法与量价（含"不可跨切法
> 相加""均价含结构、不得称提价"）／**替代解释**（实际税率 24.19%→27.09%、税率因素多吃掉 2.65 亿、
> 非经营项）／**待核查**（红酒与其他缺成本 → 补 10% 以上表或分部附注）。
> **真材料全链**（[u1_yanghe_ingest_run.json](evidence/u1_yanghe_ingest_run.json)，正常入口，脚本不写数字）：
> 130 事实/28 指标（36 条分段与量价、12 条推算）→ 冻结 126 可用 → **validated、无失败检查** →
> **正文分析卡段落 6362 字符**（含上述三段式）。读数：归母净利 **−33.4254 亿** = 毛利 **−38.0095**
> （规模 −31.5354／毛利率 −6.4741）＋毛利线以下 **+4.5841**；分产品:白酒 **−37.958 亿**；
> 分地区 省内 **−12.872**／省外 **−25.214 亿**；量价 销量 **−53.823** ＋价格 **+11.684** = **−42.139 亿**。
> **仍未做**：独立成篇的 4–6 页正文与利润瀑布图（当前是报告链的「分析卡」段落）；
> `total_assets` 两处同 id 同值被判冲突（与本案例无关，留积压）。
> **下一步**：U2（现金调节桥＋反向情景阈值＋三图）→ U3（三一）；U1 成篇随后补。
> 提交 `ccd4a4a`，远端 **CI success**（run `36753718389`）。
>
> ## 历史账（按日期，细节保留）
>
> ## 历史账（2026-09-30 上午 · **阶段 Q 深化 L0→L1→L2→L3 逐批执行**）
>
> 依据 `docs/阶段Q深化_可信分析与问题驱动建模_20260930.md` 与复核证据
> `docs/evidence/20260930-Q-deepening-review.md`（基线 `f3a49dc`）；**保留 K0 已关反例、
> K1 叙事接入与 K2 两包各 7/7 复算**，不重派整批；本批**无付费试错、无新数据订阅**。
> 证据与"失败→通过"读数：`docs/evidence/20260930-L0-L3-closure.md`。
>
> **L0-a 可信输入/输出契约**（`3028dfd`，反例 F1–F5）：`contracts.full_identity_ok`
> 一次核主体/币种/报表范围/金额量纲，**算子入口与独立金样共用同一条判据**
> （"洋河/CNY 净利 + 茅台/USD 毛利"由 `validated` 变为 `not_applicable`，不再标错主体币种）；
> 列头按**完整日期**解析并按报表性质分 stock/flow（`2024-01-01` 期初列不再占 2024 年末格）；
> 单位与币种**分别取证**（下一行 `币种：美元` 胜过"元→CNY"推断）；
> 冲突判据统一为**完整身份**且沿 `derived_from` **血缘传播**（父收入冲突 → 派生毛利不可用）；
> 独立验证新增**恒定执行**的 `binding`（按 fact_id 反查数据集 + 对照声明身份）与
> `output_shape`（输出在声明内、kind↔单位相符、期间只能来自数据集、分项类型一致），
> 篡改分项/期间/主体/币种一律 `validation_failed`，合法载荷照常通过；
> `validation.RULES_VERSION` 随 run 记录（**不进 run_id**，以免破坏 K2 两包 `output_id`
> 与 7/7 离线复算），规则变更后旧 run 判 `rules_changed`（不冒充按新规则已验证）。
>
> **L0-b 所选运行真正进正文**（复核 U1/U2）：新增 `analysis/selection.json` 选择绑定
> （模型 → run_id + dataset_hash + 参数 + 规则版本 + 采纳身份），`report_brief` **按选择**
> 渲染分析卡；选择过期（资料/规则变）**只报"未采用"，绝不改取别的运行**；
> `POST /analysis/adopt` 必须给 `run_id`（缺 → 400），采纳前核"输入仍当前、已验证、
> 规则版本匹配"（不符 → 409 + 原因），返回**实际采纳版本的 `identity_id`**
> （不再读不存在的 `report_version_id`）；顺带修掉采纳路径调用编排器才有的
> `self._accept_fn_for`（HTTP handler 上没有这个方法）导致**采纳必然 500** 的真实缺陷；
> `GET /analysis` 给 `current_package`/`packages`/`selection`/`default_params`，
> 面板"导出当前包"无当前包时走既有 `POST /package` 生成后再下载匹配包（空/失败不打开目录）。
>
> **L0-c 情景数值与解释同一份参数**（复核 M1）：标签/公式从**同一组已解析参数**生成
> （`使用者情景（收入 +10.00%／毛利率 +2.00pp…）`，不再写死 `+5%/+1pp`），
> 百分比与百分点分开写、负增速不称"上行"、"毛利线以下隐含块"不冒称纯费用；
> 输出结构声明进契约（`OutputSpec.structure`）：并行情景 `bar_grouped`、敏感度
> `bar_sorted`，只有可闭合贡献桥才画 `waterfall`。
>
> **L0-d 所有入口共享截止**（复核 S1/S3）：urllib 字节通道的**总截止从进入取件起算**并
> 在响应头读完后查钟（本机慢头探针：`timeout=0.05` 旧行为 0.511s 后 `ok=True` →
> 现在 `ok=False`/`error_kind=read_timeout`/`body_bytes=0`，文本通道同样失败关闭）；
> K1 官方发现的 240s 改为 `_OfficialDiscoveryBudget` **共享台账**：发现/取件/准入同一截止、
> 每次取件夹进剩余预算、次数与字节入账、**预算耗尽后不再启动取件**（落进
> `official_discovery.json` 的 `budget`）。
>
> **L1 官方材料同时进财务事实与叙事**：`working_paper_export` 在没有 `financials.json` 时
> 不再直接 skipped——已准入官方原文经 `facts.facts_from_annual_tables`（复用现役抽取器）
> 过契约后照样产出底稿；**缓存真年报跨公司读数**：洋河 002304 / 三一 600031 / 京蓝 000711
> 三家**经营特点不同**的非金融公司走正常资料入口，各自 4/4 模型 validated
> （`docs/evidence/l1_official_facts_chain.json`）；**真实公网在线可达性仍未实测**。
>
> **L2 研究问题驱动的模型选择**（复核 M2）：见下。
>
> **L2 研究问题驱动的模型选择**（复核 M2）：新增 `financial_analysis/questions.py`
> （规则化问题类型 + 命中词 + 需要材料 + 允许的模型），`compile_plan` 先按**问题类型**
> 选模型：`只研究现金转换，不做情景预测` → 只采用"利润到现金的转化/现金质量"，
> 情景模型带理由"与所问问题无关"被拒，且**否定语境**（"不做情景预测"）不算命中；
> `只分析营收变动的量价因素` → **不采用任何无关模型**，如实列"缺销量/平均单价/分产品收入"
> 的缺口；未命中任何类型时退回输入齐备性选择并**明说"问题未规则化"**；
> 计划绑定问题类型与输出（`AnalysisPlan.question_types/needs/gaps/notes`、
> `PlanItem.outputs`）。
>
> **L3 利润—现金深分析**：新增注册模型 `profit_to_cash`（`profit_to_cash_v1`）——
> 闭合的利润桥金额分解（毛利端 + 毛利线以下，`residual=0`）+ 现金—利润缺口变化
> （= 尚未解释的差额，含营运资本/折旧摊销/减值与归属层差异）+ 现金转化变化（**两期净利
> 均为正**才给，百分点），一个数一个名字、零概率零预测；`validation` 补上算子声明的
> `cash_conversion_sign`（非正分母时**不得**出现该比率）。
>
> **本批定向验证**：`test_financial_analysis` 115、`test_annual_financial_tables` 38、
> `test_delivery_chain` 408、`test_offline_delivery` 39、`test_report_quality` 39、
> `test_narrative_evidence` 56、`test_frontend_guards` 62、`test_review_edit_api` 27、
> `test_transport_deadline` 22、`test_financial_chain` 36、`test_orchestrator_v2` 86、
> `test_p0` 434、`test_facts` 44、`test_working_paper` 61、`test_net_policy` 51 等全过；
> 前端 `tsc --noEmit` 0 错误、`npm run build` 通过、node 行为测试 60/60。
> **不再以全仓扫测代替产品完成**（本轮按批跑定向用例）。
>
> **K2 保留证据**：`ui-603f626cbe` 与 `ui-fa2cb73e59` 从 ZIP 字节复算**各 7/7 一致**
> （`dataset_hash` 仍 `f34114d9…`）；每包如实单列 **1 条 `label_changes`**——情景输出的
> 显示文本按 L0-c 改为从参数生成，数值/单位/期间身份/输入完全一致；复算把"数对不上"与
> "改了措辞"分开记，不混为一谈。
>
> **提交与 CI**：`3028dfd` → `671278c` → `d07449c` → `b1955d9`，已推 `origin/main`；
> **HEAD `b1955d9` 的 CI（GitHub Actions）success**（run `36665738509`）。
> 应用重启后 **16/16 服务、HTTP 200 @ 8080**；`GET /analysis` 不带会话 401；
> 新面板文案已在 `frontend/dist` 产物里（**带登录会话的真实点击仍未验**）。
>
> **仍未解决（不假装已解决）**：真人 F3 五项 ≥8/10（必须真人）；干净 Windows 一键启动
> 与完整交付验收（`clean-env-e2e` 是 ubuntu + 固定模型替身，不能替代）；带登录会话的
> 页面真实点击（实例已初始化、两个 admin 都有密码，未经同意不新建账号）；
> K1 付费 API 端到端实网整跑（预算未确认）；**Q4 统计预测未开放**（两期财报不能支撑趋势/回归）。
>
> ## 历史账（2026-09-30 下午 · 带登录会话的页面真实点击 + 两个实机缺陷修复 + Q4 预测闸门）
>
> 证据 [20260930-logged-in-clickthrough.md](evidence/20260930-logged-in-clickthrough.md)、
> [统计预测门槛与禁用清单_20260930.md](统计预测门槛与禁用清单_20260930.md)、
> [k2_audit_all.json](evidence/k2_audit_all.json)。**无付费调用**；口令只用于登录，未写入任何文件。
>
> - **页面入口补缺**：任务页此前只认「本标签页提交」或 `?conv=`——没有会话绑定的历史任务
>   （脚本产出、K2 复算包）在页面上**没有入口**，这道门因此一直验不了。现支持深链
>   `/?task=<id>`（优先于 `?conv=`），历史页加「打开研究工作台」按钮（前端已重建 dist）。
> - **真实点击链路全通**（真浏览器 + 真会话）：登录（错口令 401、无会话 401、`/api/health` 200）
>   → 深链打开任务 → 选运行（`profit_bridge→scenario_sensitivity`，面板列出三个参数的默认值与范围）
>   → 改假设（`revenue_growth 0.05→0.10`）→ 确定性复算（**前后对比**：情景归母净利 959.12→1,038.55 亿元，
>   敏感度 17.41→17.41）→ 采纳（`identity cc0d1c19…`，面板标「进正文」）
>   → 导出（新包 `deliverables_20260930_130634_a6a57c.zip`，5,623,363 B，`/files/...` 200 + PK 头）。
> - **实机缺陷一（已修）**：点「采纳这版」后**导不出来**——恒 409「导出期间发生修订…请重试」，
>   而重试永远不会好。根因：采纳按所选运行重渲染正文并落新版本，却**没把这一版正文投影成交付正文**
>   （候选采纳/人工修订都写了 `update_delivery_projection`，分析采纳漏了）。修法：采纳后同纪律投影
>   （失败任务上 `allow_terminal=True`，只写正文/验收、不写状态），响应新增 `delivery_projected`；
>   新用例**不带修复先失败**（`delivery_projected is None`），修复后断言导出快照接受该正文。
> - **实机缺陷二（已修）**：`launcher.py restart` 后编排器「拒绝启动：已有编排器实例…租约 30s 内」
>   → `15/16`、「研究能力未就绪」，**要重启两次**。修法：`claim_ownership_with_wait()` 有界等待让位
>   （上限 `OWNER_HB_TTL+6s`，活着的持有者仍拒绝启动）。真机复验：单次重启即 `16/16` + 研究能力就绪。
> - **Q4 统计预测仍不开放**：新增显式问题类型 `forecast_trend`（`models=()`）——预测/趋势/概率/回归/
>   目标价类问题现在**零模型采用**并写明「不开放」+ 门槛文档入口；此前会退回"按输入齐备性选模型"，
>   把利润桥/现金质量/营运资金当成对"预测明年收入"的回答。修前修后读数都记在门槛文档里。
> - **K2 如实记账**：两包复算仍 **7/7**（`mismatches=[]`、各 1 条仅措辞变化、`dataset_hash f34114d9…`）；
>   但点击链把 `ui-fa2cb73e59` 的采纳稿推进了一版，原包 `004010_092fdb` 变 `historical`（字节未变、
>   仍可复算），新当前包 `130634_a6a57c`；`ui-603f626cbe` 当前包未变。
> - **CI 只暴露的一个缺陷（已修）**：远端 CI（`b4802f7`，run `36669474210`）在
>   `test_url_command_keeps_a_single_line_on_stdout` 失败——本机有 16 个服务在跑所以过了，
>   CI 上 `pids.json` 不存在 → `print_status` 提前返回、没有 URL 行。用例已改为显式钉住
>   pids 状态与读取（不依赖运行环境）。**HEAD `b959c3a` 的 CI success**（run `36672477395`）。
> - **仍未解决**：真人 F3（必须真人）；干净 Windows 一键启动/完整交付验收；K1 付费 API 实网整跑
>   （**已批准但额度上限未给**）；PDF 全页视觉、当前 HEAD 远端 CI 重查。
>
> ## 前账（2026-09-30 · 官方入口在线实测 + 更正稿补缺 + 端口读数小修）
>
> 证据 [20260930-online-entry-and-port-reading.md](evidence/20260930-online-entry-and-port-reading.md)、
> [a2_official_entry_online.json](evidence/a2_official_entry_online.json)、
> [a2_corrected_report_online.json](evidence/a2_corrected_report_online.json)、
> [l1_official_facts_chain.json](evidence/l1_official_facts_chain.json)。只跑定向验证、无付费。
>
> - **官方入口公网在线实测**（`scripts/a2_official_entry_online.py`，无模型）：巨潮公告查询
>   `new/hisAnnouncement/query` HTTP 200，契约 `cninfo-hisannouncement-v1`；000711（2019/2020）
>   → `found` 5 条、600031（2023/2024）→ `found` 4 条，候选里含 **2025-09-05 的更正后年报**
>   （`version=corrected`，与原始版并存）；反例 999999 → `no_candidates/empty_result`。
> - **顺带修真缺陷**：内容级否定（代码不在索引/期间无公告）此前被记成 `status=unavailable`
>   ——页面会让人"稍后重试"而不是核对代码；现记 `no_candidates`，传输/限流/结构变化才是
>   `unavailable`（`test_cninfo_discovery` 33 全过，+2）。
> - **`000711-corrected` 更正稿正文补齐**（`scripts/a2_corrected_report_online.py`）：走产品通道
>   （`store(web_link)` → `admit` → `net_policy.fetch_document`）取回 HTTP 200、`egress=direct_pinned`、
>   1,513,031 B、`98dc313a…`、273 页、1263 小节、规则 `adm-2/f3a-1`，写入缓存布局；
>   L1 离线复跑 **4/4 材料、3 家公司**（000711 原文与更正稿同代码不重复计数）全 `validated`，
>   更正稿与原稿结论确有差异（`profit_change` −1,362,952,262.96 vs −1,318,104,774.55、
>   应收账款周转天数 469.20 vs 403.21）。
> - **`launcher.py status` 端口读数小修**：修前 `URL: …:8081` + "工作台未响应 @ 8081"（实例其实在
>   8080 上 200）；修后 `URL: …:8080` + `HTTP 200 @ 8080` + 如实说明记录不一致。根因两条并修：
>   ①URL 与探活各自解析一次端口（现同源，且以真正响应的端口为准，仅在"本实例持有 webui"时才看
>   备选，以免把别人的程序当成我们的工作台）；②**用例真的改写了 `.weavimind/runtime_ports.json`**
>   （现 `setUpModule` 隔离 + `TestRuntimeStateIsolation` 守卫断言真实文件字节不变）。
>   该文件已按当前实例恢复为 `{"web": 8080, "preferred": 8080}`。
> - **定向验证**：`test_startup_readiness` 76、`test_cninfo_discovery` 33、
>   `test_orchestrator_v2` 86、`test_sandbox_isolation`、`test_delivery_chain` 408 全过；
>   `scripts/check_secrets.py` 干净；`py_compile` 通过。
>
> ## 前账（2026-09-30 凌晨 · **K0→K1→K2→K3 全部执行完毕**）
>
> 依据 `docs/阶段Q增量验收与产品闭环推进_20260929夜.md`；**无付费整跑、无新数据订阅**。
> 证据 `docs/evidence/k0_trusted_input_20260929.md`、`docs/evidence/k1_k2_k3_closure_20260930.md`。
>
> **K0 可信输入与取件边界**（`26cd761` / `f2ac12f`）：口径不回退、算子独立校验报表范围、
> `financials` 退路绑研究契约；抽取器六类静默错数（空存货借科目、期初期末倒年、完整日期截年、
> 跨表借单位、美元标 CNY、冲突收入仍派生）全部先复现旧读数再修；缓存回放三家公司**逐位一致**；
> 响应头吃根截止（实测预算 0.2s：旧 0.483s / 新 0.242s）、字节通道总截止、有界解压、
> 上传默认 3 MiB→30 MiB 且变量名正式名生效。
>
> **K1 官方资料进正常研究任务**（`d2f002c`）：`_official_discovery_intake` 与预载同位置调用
> `disclosure_ingest.discover`（契约公司/期间、`until=as_of`）→ 取件 → 准入 → 并入
> `fetch_snapshot` → 重建叙事证据；结论落盘 `official_discovery.json`。离线正例：located 0→4
> 且指向 cninfo、`missing_labels` 四类全缺→空、现金问题 full、收入 partial、利润分解**如实仍 none**；
> 反例（无年报 / 取件失败）如实记原因码+next_steps，且**不产生证据**。
> **未做付费 API 端到端实机**（额度未确认），API 路径与用例同走 `run()`。
>
> **K2 同版可复算交付**（`6d06557`）：包内新增 `analysis/{dataset,plan,context}.json` + 清单
> `analysis_inputs`；`package_statuses/current_package` 按**包内身份**判 current/historical
> （时间戳不参与身份判定）；验收脚本拆成默认只读 + `--export`。实机：`ui-603f626cbe`
> 旧包 `81047fdf…` 标 historical（采纳 `f585edbf…`），冻结新包含 PDF/6 图/底稿/7 运行/3 输入
> → **7/7 离线复算一致**（dataset_hash 与实机一致）；`ui-fa2cb73e59` 同样 7/7；旧包原地保留。
>
> **K3 分析工作台**（`e7deafe` + 质量项）：`GET /analysis`（含**原始依据**：每个输出挂上它
> 消费的包内观察 fact_id/期间/值/单位/口径/来源）+ `POST /analysis/recompute`（新运行、
> 不自动采纳、参数越界 400、不适用如实说明）+ `POST /analysis/adopt`（只收已验证运行、走同一条
> 装配路径）；页面新增 `AnalysisWorkbenchPanel`（分析卡→原始依据→改假设→复算→前后对比→采纳→导出当前包）。
> 顺手修掉两处"改了不生效"：scenario 的声明参数名与 compute/gold 读取名不一致、`runner.run`
> 未把参数交给独立验证（一改假设就 gold 判失败）。
> **质量项（同资料新旧对照，`ui-603f626cbe`）**：一级标题 2→1、重复 `分析卡` 标题 2→0、
> 卡片性质与"下一项验证动作"按模型各说各的（此前两张卡同一句、且都是利润桥那句）、
> 来源说明把"正文引用编号"与"未采用候选"分开写（不再自相矛盾）、**散文内容丢失 0 行**
> （只改层级与措辞，不截断分析）。读数 `docs/evidence/k3_quality_compare_ui-603f626cbe.json`。
>
> **门槛**：全量核对（CI 同源、每文件一进程）**53/53**；前端 `tsc --noEmit`、`npm run build`、
> node 行为测试 60/60 全过。
>
> **仍未解决（不假装已解决）**：真人 F3 五项 ≥8/10（**必须真人**）；干净 Windows 一键启动/
> 完整交付（`clean-env-e2e` 是 ubuntu+固定模型替身烟测，不能替代）；`000711-corrected` 正文
> 缓存缺失（更正稿基线无法离线复验）；`launcher.py status` 探活端口 8081 与实际 8080 的小不一致。
>
> ## 前账（2026-09-29 夜 · K0 可信输入与取件边界）
>
> **K0-a 可信模型输入**（`26cd761`）：
> ① `AnalysisDataset.get/require` 口径**不回退**（显式"合并"缺项不再拿唯一那条母公司值），
> 缺输入提示带"另有其它口径"；`primary_caliber` 计入不可用观察（"不可用"≠"缺输入"）。
> ② 新增 `report_scope_ok`：利润桥/现金质量/情景/同年比率四处独立校验"跨指标范围相同且
> 有声明"——固定反例（合并净利 10/12 + 母公司毛利 30/40 + 母公司 CFO 20）由
> **validated 变为缺输入/拒绝**，同口径数据照常 validated（不全局关模型）；输出说明从
> 已验证输入生成，不再固定写"合并"。
> ③ `data_analyzer` 的 `financials` 退路复用与底稿**同一条契约解析**（主体/两期/口径/as_of）：
> 缺要素不跑模型并回报可行动缺口；错公司/错口径/晚披露的观察被排除并写缺口；
> `freeze` 不再把"请求年度无观察"退回"数据里所有年度"。
>
> **K0-b 抽取器只接受可证明的单元格**（`26cd761`，六类反例全部先复现旧读数）：
> 空存货不借下一科目（未知行标签=新行边界）；期初/期末**按列序**映射年份；完整日期
> `2020年1月1日` 不再截成年份占年末格；不跨报表标题借年份/单位；`单位：元 币种：美元`
> 判 USD（币种与量纲分列取证）；收入冲突时**不派生**毛利并记 `derivation_input_conflict`。
> 事实层新增 `period_kind`（存量/流量）与 `period_label`（完整列头）。
> **缓存回放**（`scripts/k0_replay_cached_tables.py`，离线）：京蓝 40/44、洋河 28/32、
> 三一 34/38 与修前基线 **text_hash/事实/观察/拒绝逐位一致**，模型仍 validated。
> **未复验**：`000711-corrected` 只存了 `meta.json`（正文未入库）→ 夜验收的"更正稿
> 38/42、`3f6ee7b8…`"本轮无法离线复验，**不假装通过**，待补正文缓存。
>
> **K0-c 取件边界**（`f2ac12f`）：响应头在 `begin()` 之前就吃根截止（实测预算 0.2s：
> 改前 0.483s 且报错阶段错成"响应体"，改后 0.242s 报"响应头未读完"）；字节通道
> `resp.read` → 有界读（read1 + 每次读前设剩余超时 + 读后查钟），保留"截断+over_limit"
> 契约，两条边界都给不了且给了上限时退回一次受限读、无上限则明确拒绝；gzip 改**解压
> 过程中**查上限；上传默认 3 MiB → **30 MiB**（与披露下载同量级）；变量名正式名
> `WEAVEMIND_UPLOAD_MAX_BYTES` 生效、旧拼写兼容不删。新增用例验**实际入口**
> `annual_report_pdf.fetch_bytes`。
>
> **全量核对**（CI 同源、每文件一进程）：**53/53 通过**（`test_common.py` 为本机 Windows
> 控制台编码 ENV，不计）。证据 `docs/evidence/k0_trusted_input_20260929.md`。
>
> **下一步（按门槛继续，不等"继续"指令）**：K1 把 `disclosure_ingest.discover` 接进
> **正常研究任务**（官方发现→取件→准入→影响问题覆盖/分析/候选稿；含一个有证据正例与
> 一个缺料草稿反例），然后 K2 同版可复算交付包、K3 页面改假设→复算→采纳→导出。
> 仍不做付费试错；`launcher.py status` 探活端口 8081 与实际 8080 的不一致待单独小修。
>
> ## 前账（2026-09-29 夜 · 分析链进交付已修完并整跑通过）
>
> **付费整跑（当时授权，两次）**：`scripts/research_acceptance_run.py --submit`
> （走 `POST /api/tasks` 鉴权之后的**同一个函数**），贵州茅台 2023/2024，合并口径。
>
> | 任务 | 终态 | 交付 | 分析运行 | 读数 |
> |---|---|---|---|---|
> | `ui-fa2cb73e59` | FAILED | **verified**（`hard_fail` 空、验收 pass、评审 PASS） | 3 模型 + 4 比率 validated | 主链全通；FAILED 来自**反思轮新增**的 `i2-r2` 抓取无候选 URL → 连锁标死 `i2-r3`/打包 |
> | `ui-603f626cbe` | **SUCCESS** | **verified**（`aligned` 真、`review_valid` 真） | `analysis_runs.json` 26,974 B：`profit_bridge`/`cash_quality`/`scenario_sensitivity` validated + 4 比率 | 6/6 必需事实带来源定位、同比 11 条、缺口 0、问题 0；导出 MD 27.3KB / PDF 5.57MB / CSV / JSON |
>
> **修完的四项**（每条先在旧代码上复现失败，再修）：
> ① **分析步拿到数据集**：派发 `data_analyzer` 前先落底稿（幂等）；worker 在底稿缺失时按
> 预载载荷 `financials.json` 冻结数据集，运行记录里记 `dataset_source`。旧码读数是实机原话
> `No fresh CSV found in workspace`。用**真实失败工作区**离线预演：3 模型 + 4 比率 validated。
> ② **可选步骤失败不再连锁**：阻塞传播与"步骤失败⇒任务失败"两处漏了 `optional` 判定
> （`deps_failed` 一直认）——`2b` 一失败就把解释/分析/报告逐步标死，旧码读数与实机逐字一致：
> `Blocked by failed dependency: ['2b']` → 交付 `draft` + `hard_fail="分析未完成…"`。
> ③ **财务任务的检索/抓取是补充证据**：结构化财务已预载时这两类步骤按 `optional` 语义处理
> （**反思轮新增的抓取步骤同样适用**，这正是第一次整跑 FAILED 的原因）。取不到=缺口，不阻塞。
> ④ **金融分析步失败不得降级为文字概括**：不重规划成 `content_summary`，失败如实保留。
>
> **交付物自己写明的保留意见（未隐藏）**：`research_state=research_draft`（"仅原始披露"，
> `located=0`）、`review_state.verdict=DEGRADED`（金融类**计划评审 30s 超时**）、正文开头逐字写着
> 「评审未完成，已降级…请经人工复核后方可使用」。三态分离照旧：机器验收通过 / 研究状态如实为
> "底稿" / 人工复核未代劳。
>
> 整跑原始读数与逐项证据：`docs/evidence/paid_run_analysis_chain_20260929.md`。
>
> **仍未解决（不假装已解决）**：**本机公开检索取不到可用来源**（三次旧实机 + 本轮两次，`web_search`
> 候选里都没有可用年报正文 URL → 抓取"无候选 URL"）；本轮把它变成**缺口**而非阻塞，但
> "研究状态=底稿、located=0"的根因是**来源可得性**，需要检索侧专项。真人 F3 五项 ≥8/10
> （**必须真人**）；金融类计划评审 30s 超时的"评审降级 ⇒ 交付能否 verified"是架构决策，本轮未改；
> 页面「改假设→复算→采纳→导出」（Q2/Q3 UI）；`report_version`/交付包携带 `derived_from`；
> AGENTS.md 优化积压 10 项（长期项，非本阶段任务）。
>
> ## 前账（2026-09-29 深夜 · 问题收口 + 分析链定位）
>
> **问题收口**：
> ① **取件时间预算按通道分**：正文 30s / 披露下载 120s（可环境变量覆盖），材料直链、
> `annual_report_pdf.fetch_bytes`、`web_fetch` worker 统一改用。实机证据：运行中
> `webfetchworker` 取三一 2024 年报返回 `读取超时（总截止 29.9824s）`（旧码会立刻
> `WinError 10038`）→ 修好的代码在跑 + 30s 确实太紧。
> ② **更正我自己写错的记录**：先前"外部探针无回执、队列未被消费"是错的——正确队列键是
> `task_queue:webfetchworker`（服务标签≠注册名≠队列键），那条被消费了，结果落在
> `task_result:<task_id>`；新增 `python launcher.py queues` 把三者对应关系打出来。
> ③ **港股 `00700.HK` 撤回原结论**：复测 200 / 243025 字节 / 96 行，适配器取回 12 年
> （最新 2025 收入 7517.66 亿元）；`URLError 10061` 已不成立。
> ④ **上交所公告查询明确不打通**：4 种参数全 `total=0`（`pageSize` 回显 10，请求写 25）
> → 不是被过滤筛空；不再盲试，沪市走巨潮。
> ⑤ **血缘进观察**：`Observation.derived_from/formula_version` 随观察进数据集**并计入
> 观察指纹**（改血缘=换观察）。
> ⑥ **亏损公司情景模型**由 `validation_failed` 改判 `not_applicable`（附理由）。
> ⑦ **主要会计数据 5 列 3 年**不做猜测性列映射，新增原因码
> `adjustment_variants_or_ratio_column`，同样指标从审计过的三张报表取。
>
> 定向全绿（当时）：`test_delivery_chain` 405、`test_orchestrator_v2` 81、`test_transport_deadline` 15、
> `test_startup_readiness` 69、`test_financial_analysis` 66、`test_annual_financial_tables` 28、
> `test_cninfo_discovery` 31、`test_net_policy` 50。
> 证据 `docs/evidence/a2_official_discovery_20260929.md` §8–§10。
>
> **当时仍未解决**：分析结论进正文/交付（根因②，已在本账（上）修完并整跑通过）；
> 真人 F3 五项 ≥8/10（**必须真人**）；亏损期情景专用判据（研究侧）；
> 页面"改假设→复算→采纳→导出"（Q2/Q3 UI）；`report_version`/交付包是否一并带
> `derived_from`（下一批）；AGENTS.md 的优化积压 10 项（长期项，非本阶段任务）。
>
> ## 历史（`5f8c8b2` 及以前，按日期保留）
>
> **A2（`87eb855`…`5f8c8b2`）**：官方发现链打通（更正 09-07 结论）、来源按端点注册、
> 抽取器认三种真实版面、口径显式化、三家公司真机链（三一 4/4 模型 validated、
> 洋河与冻结样本逐位一致、京蓝重述纪律）；同时修回 A1 自伤回归（取件通道恒失败）。
> 证据 `docs/evidence/a2_official_discovery_20260929.md`。CI 全绿（run `36559669470`）。
>
> ## 历史（`e033a6d` 及以前，按日期保留）
>
> **A0（`7c5fe0c`+）**：按 `docs/金融资料获取与事实化补链_20260929.md` §2 修掉抽取器的**事实污染**：
>
> ## 历史（`5f8c8b2` 及以前，按日期保留）
>
> **A2（`87eb855`…`5f8c8b2`）**：官方发现链打通（更正 09-07 结论）、来源按端点注册、
> 抽取器认三种真实版面、口径显式化、三家公司真机链（三一 4/4 模型 validated、
> 洋河与冻结样本逐位一致、京蓝重述纪律）；同时修回 A1 自伤回归（取件通道恒失败）。
> 证据 `docs/evidence/a2_official_discovery_20260929.md`。CI 全绿（run `36559669470`）。
>
> ## 历史（`e033a6d` 及以前，按日期保留）
>
> **A0（`7c5fe0c`+）**：按 `docs/金融资料获取与事实化补链_20260929.md` §2 修掉抽取器的**事实污染**：
> ① 不再跨口径补齐（母公司 18,600,000 只标母公司；合并 2020 应收账款因**拆行**拒绝，
> 理由 `split_number`，带 PDF 页码）；② 政策调整表里的 2020 存货不再当年末数
> （`table_unrecognized`）；③ 收入恢复（`营业收入扣除金额/扣除后金额` 不再被前缀误吞）；
> ④ 单位/币种/口径**必须有证据**（表头单位标注、表名），无证据即拒绝，不再默认"元/CNY/合并"；
> ⑤ 每列映射不再截断（附注列只在表头声明时排除，否则 `column_mismatch`）；
> ⑥ 接受事实带非空 `fact_id`，定位为 `PDF 第 N 页 · 表名 · 行「原标签」`；
> ⑦ 毛利只在同主体/同期间/同口径/同币种/同单位且输入有身份时派生（带 `derived_from`）。
> 证据：`docs/evidence/annual_financial_tables_20260929.md` 的**更正**一节（旧叙述保留）。
> A0 已交小提交；**未开新付费整跑**、未改模型/权限/代理/CI，未扩凭据。
> **A0 剩余**：合并口径的 2020 应收账款/存货仍是拒绝（按 A0 属正确行为，需更可靠的单元格解析）；
> `Observation` 尚未承载 `derived_from`（血缘只进了一半）。
>
> **A1（已重做交付）**：`net_policy` 改用 `http.client` 解析（透明解 chunked + gzip、
> **时钟同源**的总截止）、文本通道加总截止与默认上限（8 MiB）、容量策略单一来源
> （`transfer_limits.py`）+ `read_with_deadline(max_bytes=…)`。做法：**先补测试替身**
> （假 socket 的 `makefile`/`read1`）再改生产码。**字节通道保留既有「截断+over_limit」
> 契约 → 时间边界未闭合**（已登记）。
> **东财 77 字节根因已探明并修**：`SECURITY_CODE` 存**裸代码**，带 `.SZ/.SH` 被判参数错
> （`code 9201「参数错误为空」`、89 字节）；去后缀放进**适配器内部**，空结果抛出接口
> 自己的 `success/code/message`。真机复验：洋河 002304.SZ rows=2（与冻结样本逐位一致）、
> **三一重工 600031.SH rows=2**（第二家公司结构化事实打通）；港股 00700.HK 仍
> `URLError 10061`（原因未定）。
> 定向：`test_transport_deadline` 12、`test_net_policy` 45、`test_search_quality_unified` 80、
> `test_eastmoney_params` 9、`test_p0` 434 全 OK。
> **原 A1 条目（仅记录当时意图）**：（`docs/evidence/a1_transport_deadline_20260929.md`）：
> ① `transport` 两个通道的 `resp.read()` 无总截止 → 改走 `read_with_deadline`（+默认字节上限）；
> ② `net_policy.fetch_document` **手拆响应头且不认 chunked**（分块框架会混进 PDF）→ 换
> `http.client.HTTPResponse`（成熟解析 + 透明解块 + gzip），**保留**已验 IP 直连/3xx 不跟随/不带凭据；
> ③ **时钟不同源**（墙钟 vs 单调钟）让总截止算成几十亿秒——本批自己引入又当场抓出，已修；
> ④ 容量策略统一到 `transfer_limits.py`（上传 3 MiB / 下载 30 MiB / 正文 8 MiB / 二进制 64 MiB /
> PDF 页数 1200 可配），`explain()` 给具体超限说明。
> 定向：`test_transport_deadline` **13 OK**（慢体、chunked 逐字节、gzip、非 2xx、200+错误体、
> 超上限、3xx 不跟随、socketpair 慢分块、容量单一来源）；`test_deploy_manifest` 40 OK（CI 51 步）。
> **东财 77 字节原因仍为 unknown**（本批未发外网请求，不写"站点/出口限制"）。
> **A1 剩余**：诊断矩阵（入口×upstream×出口×原因码）与 health_registry 词表接线；
> 多地址/重试共用根截止；`net_policy` 逐 IP 预算与 `read_with_deadline` 两套实现是否合并；
> 页面上限提示。**A2 未开始**。
>
> **发布权限（独立事项）**：`gh` token 缺 `workflow` scope，推 `.github/workflows/ci.yml`
> 被 GitHub 拒绝（`4` 个待推提交触碰该文件）。修法：`gh auth refresh -h github.com -s workflow`。
> **不扩凭据、不删 CI 规避**；本地修复继续进行。
>
> ## 历史（`7c5fe0c` 及以前，按日期保留）
>
> **基线**：Q0 前 `5fe791e`；Q0 五个提交
> `046f3b2`（派生输出语义/正文匹配/位置对齐）→ `523d4c6`（旧 owner 失租闸门）→
> `a8359c2`（检索总截止 + 资料期间）→ `4bc8834`/`d4d3946`（证据账更正）；Q1 两个提交
> `3903945`（包：冻结数据集 + 利润桥接）→ `447b04f`（同版绑定 + data_analyzer 金融路径）；
> Q2 第一批（三族模型）。证据：
> `docs/evidence/q0_derived_output_semantics_20260929.md`、`q0_owner_lease_gate_20260929.md`、
> `q0_search_deadline_and_period_20260929.md`、`q0_ci_and_second_company_20260929.md`、
> `q1_frozen_dataset_and_profit_bridge_20260929.md`、
> `q1_delivery_binding_and_worker_20260929.md`、`q2_model_families_20260929.md`。
>
> ### CI 状态（**更正**：本仓库**有** CI，不是"没配"）
>
> | 项 | 事实 |
> |---|---|
> | 配置 | `.github/workflows/ci.yml` **已存在且被跟踪**；48 个 `run: python test_*.py` 步骤 == 48 个已跟踪 `test_*.py`（双向差集为空） |
> | 本地门禁 | `python test_deploy_manifest.py` **39 OK**（含"每个测试文件都要在 CI 里"那条守卫） |
> | 远端最近一次 | 最后**已推送**的提交是 `2519d9a`（origin/main）；该提交上 CI = **failure**，唯一失败步骤是 `backend / Deploy manifest & lock guards`，原因 `test_question_assessment.py` 不在 CI 内 |
> | 该失败的现状 | 修复提交是 `8a44f85`（H0），它在 `2519d9a` **之后**、**未推送**，所以远端没有它的记录 |
> | 本批及之前 60 个提交 | **没有任何远端 CI 记录**（本地 `main` 领先 `origin/main` **61 个提交**，未推送=未授权推送） |
>
> → 准确说法是"**CI 已配置、本地守卫通过、远端结果只到 `2519d9a` 且为 fail、本批无远端记录**"，
> 既不是"仓库没有 CI"，也不是"CI 通过"。
>
> ### 已验（本轮真跑过、可复算）
>
> - **跨进程真实演练**（`5fe791e`，隔离 Redis 6390 + 临时 DB + 真进程）：第二实例拒绝启动、
>   退出码 2、TTL 过期后接管 gen 递增、RECEIVED 收执被恢复；不再属"只剩环境项"。
> - **Q0-①**：输出元数据伪造 / 分母写成本期 / 正文跨指标跨期间借用全部改判拒绝；
>   顺带修掉 `extract_financial_numbers` 的**位置错位**（旧实现改写文本后记 pos，护栏读到**邻句**）；
>   冻结样本溯源 353/375=94.1% → **364/375=97.1%**，另两个样本 100%/100%，阈值未动。
> - **Q0-②**：同 owner 指纹失租后必须拦；本地有效期 = 服务端 PTTL − 保守余量（29s ≤ TTL 30s），
>   改前是 TTL+10=40s。
> - **Q0-③**：真实 `http.client.HTTPResponse` 的 chunked 慢分块头：0.1022s/0.3182s（预算 0.02s）
>   → **0.0203s/0.0217s**；`search_diag` 不再抬预算（低于下限零请求、不占额度）。
> - **Q0-④**：正式名「半年度报告」与倒装「年度报告2025公告」改判冲突；次年披露正例仍放行；
>   硬边界在结构化摄取（`disclosure_ingest` 按文档自身报告期判定）。
> - **Q1 第一片**（`3903945`）：`financial_analysis/` 十个文件——冻结数据集（冲突值不择一、
>   真实零与缺失分开、非年度期间不组年度对）、注册表（唯一入口 + 算子白名单）、
>   利润桥接（Δ归母净利 = Δ毛利 + Δ毛利线以下，闭合差精确 0）、独立验证（Decimal 手算金样
>   + 恒等式 + 禁百分点替代）、分析卡/图（共用 run_id）。真机冻结样本只读复算：
>   dataset_hash `56c2c9dd…`、run `validated`、**-33.43 / -38.01 / +4.58 亿元**。
> - 定向套件：`test_delivery_chain` 403、`test_p0` 434、`test_search_quality_unified` 80、
>   `test_startup_readiness` 68、`test_offline_delivery` 34、`test_orchestrator_v2` 81、
>   `test_financial_analysis` **57**、`test_deploy_manifest` 39（CI 48→49 步 == 49 个测试文件）、
>   `test_r0_boundaries` 47、`test_narrative_evidence` 68、`test_financial_chain` 36、
>   `test_report_quality` 39、`test_task_projection` 19 —— 逐文件 OK。
> - **Q1 同版绑定 + worker 金融路径**（`447b04f`）：运行记录落盘/身份块/正文↔运行核对
>   （`financial_analysis/store.py`）；正文 `## 分析卡`、清单 `analysis_runs`、ZIP 内
>   `analysis/analysis_runs.json` 三者指向同一次运行（端到端用例：清单 hash 与包内字节相符、
>   包内正文含 run 标记、对包内正文再核对 ok）；`data_analyzer` 有金融底稿时走
>   "冻结数据集→计划→注册模型→落盘运行"，不再猜最新 CSV/末列目标。
> - **Q2 第一批**：现金质量（−20.44 亿元 / 覆盖率 69.37%，非正利润不给覆盖率）、
>   营运资本（真实样本缺 4 个 slug → **拒绝并列出补料清单**；夹具验证 8.0 亿元 / 16.43 天、
>   写明期末口径）、条件情景（基准复现基期差额 0、单因素敏感度 2.89 亿元/pp、方向检查通过）。
>
> ### 未验（**不是**"只剩环境项"，逐条说清）
>
> 1. **第二家公司**：材料**已通过正常入口准入**（用户给的巨潮直链 → 京蓝科技 000711.SZ
>    《2020 年年度报告（更正后）》，273 页/221316 字符、报告期 2020、披露日 2025-09-05、
>    `as_of=2025-09-30` → `within`、三指标均 `present_in_scope`；材料记录 `2db15147bad934f9`，
>    隔离工作区，见 `docs/evidence/second_company_jinglan_20260929.md`）。
>    **但"事实化"仍缺**：东财结构化入口当前返回空载荷，从 PDF 抽"主要会计数据/财务报表"
>    的抽取器未做 → 四族模型尚未在第二家上跑过；**Q2 门槛只在"材料准入"这一层成立**。
>    另：该材料是**更正后**版本（不能当作 2021 年当时已知的数据）、且公司带 `*ST` 警示，
>    作"不同经营特点的对照"偏极端。
> 2. **干净环境同包完整链**（`clean_env_verified`）：需无 Python/Node/Docker 的机器；
>    本机跑 `scripts/e2e_clean_check.py` 只能证明本机环境。
> 3. **真人 F3 五项 ≥8/10**：必须真人评，代理不得代评。
> 4. **远端 CI 对本批的结论**：无（未推送）；`2519d9a` 的 fail 已在本地被 `8a44f85` 修掉但未远端复验。
> 5. `_promotion_subject` 散文前缀误判（本轮**量化**了它对冻结样本的 7 处影响，未修——
>    动它等于动跨公司同值护栏，需单独一批带语料回归）。
> 5b. **已修但需记住**：算子目录原叫 `models/`，被 `.gitignore` 的 `models/` 整目录静默忽略，
>    导致 Q1/Q2 三个提交缺文件（干净检出必 ImportError）。已改名 `operators/` 并加索引层守卫
>    （`test_deploy_manifest.test_python_files_under_copied_dirs_are_tracked`）。**不重写历史**，
>    前三个提交本身仍缺文件；HEAD 起自洽。
> 6. 真实外网检索路径的截止表现（本轮全部进程内替身）；DDGS 同步 SDK 卡住仍只能事前拒绝+事后记账；
>    `urlopen` 响应头阶段的总截止未改。
> 7. C3 仍"部分实现"、C4 仍未毕业（银行门禁/人审/发布均未变）。
> 8. **Q1 端到端同版绑定未做**：分析卡/图在包内共用 `run_id`，但
>    `report_brief`/`delivery_pipeline`/冻结包还**没有**消费
>    `financial_analysis.report_adapter.delivery_binding()`；`data_analyzer_worker`
>    也还没按显式数据集/分析计划调用本包（架构要求取消"猜最新 CSV/目标列"的金融路径）。
>
> ### 下一步
>
> ① 抽取器补 `accounts_receivable`/`inventory`/`accounts_payable`/`operating_cost`
>    （营运资本在真实样本上目前必然拒绝，缺口已登记）；
> ② 页面上的"改假设→复算→采纳→导出"（Q2 退出条件之一）；
> ③ 图从声明变成像素（CJK 字体 + 既有 PDF 渲染口径）；
> ④ 第二个真实公司（需官方直链或人工文件）；到手前 Q2 的"两个真实公司"门槛**不得**宣称满足；
> ⑤ 统计模型（Q4）仅设计。
>
> ---
>
> ## 以下为**历史原文**（按日期保留，不再代表当前状态）
>
> 其中这些说法已被上面更正：①"仍未验只剩环境项"（跨进程/租约/恢复已在 `5fe791e` 真跑）；
> ②"远端 CI/推送 CI 仍缺"（CI 早已存在且被跟踪；缺的是**远端运行记录**）；
> ③"C/D 未做"（C 批与 D 批均已提交，见证据文档）。

# DeepSeek 执行状态（2026-09-28 更新 · **"全部遗留清零"已更正** · C3 部分 · C4 未过）

> **D 批（09-28 晚，基线 `41de3ba`）已做完**（证据 `docs/evidence/d_period_and_deadline_20260928.md`）：
> ② `read_with_deadline` 的**总截止**真正生效（审查形状：进程内 socket pair、每 5ms 一字节、
> 预算 0.02s → **0.1076s → 0.0220s**）：单次读改 `read1`、每次读前设 socket 剩余时间、读后查钟、
> 到点关闭连接、**不可保证有界的路径明确拒绝**（`UnboundedReadError`，非可重试，业务区别保留）；
> ① 期间口径照裁决落地：`period_years()`/`disclosure_years()` 分开，"2024 年度报告，2025 年 4 月披露"
> 允许而"2025 年度报告/2025 年年报"拒绝，`as_of` 年份不再扩展目标期间，重试批次与初始查询同语义。
> 定向：`test_delivery_chain` 400 OK、`test_p0` 429 OK、`test_search_quality_unified` 77 OK。
> **仍未验只剩环境项**（跨进程并发 / 真实租约转移 / 崩溃恢复演练、第二家公司真实原文、
> 干净环境同包完整链、远端 CI、真人 F3）——都需要本机不具备的环境或真人。

> **C 批（候选闭环）进展（09-28 下午，基线 `b85e63e`）**：C-1/C-2/C-3/C-4/C-6/C-7 已修并交
> 定向证据与**页面级**证据（`docs/evidence/afternoon_c_candidate_loop_20260928.md`）：
> FAILED 任务可按已并入材料生成候选（原失败审计保留）、`candidate_identity_id` 不再是绑定方法、
> 幂等键绑全集依据（材料集合+契约+采纳版 identity+有效规则）、超时改"结果未知 + 稳定 operation id +
> 可查询"、失败行动由任务自身失败事实决定（不再指向无关 Docker）、结论卡写明期间与单位。
> 页面上跑通：生成 → 预览/比较 → **两步显式采纳** → **对该身份重验** → **同版导出**。
> 本轮复查的四个新问题也已修完：**导出 409**（采纳的那一版就是交付正文 + 打包闸门按终态种类分类 +
> 交付投影同步：包 `deliverables_20260928_194051_e44448.zip`，清单身份/正文 hash 与采纳版一致、逐文件
> hash 复算无漂移）、**33.43亿元 不可溯源**（小节标题式前缀被当主体；修后三个真实任务
> 94.1% / 99.5% / 100%，跨公司护栏仍生效）、**378,003 被摘录截成半个数**、预览面板显示竞态。
> 完整闭环复跑后采纳版口径 **203/203 = 100% 可溯源，未支持 0**。
> **未验仅剩环境项**（跨进程并发/租约转移/崩溃恢复演练、第二家公司真实原文、干净环境、远端 CI、真人 F3）；
> **D 批整批未做**。

> **更正（09-28 下午架构复核，基线 HEAD `1a5fd9b`）**：本文件此前写的"全部遗留清零"
> 与实际未验范围不符。复核给出并全部复现了三条独立反例：**二次 DB 读取失败仍放行**、
> **同 QUEUED 重复取得启动权**、**派生行 `formula="1+1"`/`value=999`/输入不存在仍 100% 溯源**
> （跨公司差额同样可借用）。因此 **P1-a 的"52%→94%"不能作为验收通过证据**：它减少了假失败，
> 同时引入了**假通过**；阈值虽未改，但"支持"的定义被放宽了。
>
> A/B 两批已按新单修完并交定向证据（`docs/evidence/afternoon_a_b_repro_20260928.md`），
> **C/D 未做**（C 见上方进展块）。**C3 仍部分实现、C4 仍未通过**；第二家公司真实原文、
> 干净环境同包完整链、远端 CI、真人 F3 原门槛与银行受控试点门禁仍留待验。

**当前批次**：`docs/DSH真实样本复核与下一批执行_20260928.md`（基线 `65a6b62`），
先 P0（重复执行与恢复断点）→ P1（计算与溯源）→ P1（检索协议）→ P1（补材料闭环与 actionable）。
上一批 `docs/Harness接续执行指令_20260927.md`（基线 2519d9a）的 H0/H2 成果保留、H1 仍有缺口。
**C3 改称"部分实现、恢复与并发未验收"**，不再标"已收口"；C4 样本有真实材料与产物但**未毕业**。
本轮**不新增付费整跑、不降阈值、不改用户设置/门禁/模型/代理、不清理真库**；
`docs/evidence/c4_samples_run_20260927.md` 那份"当前余额耗尽"已随用户充值**解除**（保留为历史，
不再周期探测余额）。

## P1 本轮已交（9 项，各自带证据文档；全部离线复算，未跑付费整链）

| 项 | 提交 | 一句话 | 证据 |
|---|---|---|---|
| P0-f 页面层结论 | `3ec1366` | 旧包在 UI 层从不展示；身份判断落在下载链接口 | `p0f_delivery_identity_20260928.md` |
| **P1-a** 数字溯源四处**假失败** | `ed60e07` | 冻结样本溯源率 **52% → 94%**（阈值 0.7 未动）：定位元数据单列账；无单位大数边界；底稿派生行沿公式/输入 fact_id 准入（记"计算"不记"引用"）；量纲/小数位/正负号/两期差额 | `p1a_number_traceability_20260928.md` |
| **P1-b** 正确计算 ≠ 正确解释 | `126186c` | 派生读数标年份/单位/时间正序；"利润侵蚀主要发生在毛利线以下"与底稿 `+4.58 亿元` 符号相反 → 判 `unsupported` 并点明"不能用利润率差替代金额归因" | `p1b_correct_calc_vs_correct_reading_20260928.md` |
| **P1-d(根因)** `/actionable` 恒为 None | `375479e` | `_get_task_page` 读了一个**从未定义**的 `health` → 每次 NameError 被吞 → "该做什么"从来没显示过 | 提交信息（本轮无单独文档） |
| **P1-e** 取件拒收分因 | `a0780e3` | HTTP 状态 / Content-Type / `not_pdf` / 截断 / 损坏 / 真扫描件各自可辨；只有真扫描件才建议 OCR；字节通道二进制往返、SSRF 拦下零请求、代理失败不降级直连 | `p1e_pdf_reject_taxonomy_20260928.md` |
| **P1-c①** 契约重建吞查询 | `3a84b3c` | 反例逐字复现（4 条重试查询 → 0 条）后修复：契约内保留、契约外拒绝；重建幂等 | `p1c1_contract_rebuild_queries_20260928.md` |
| **P1-c③** 块间查时钟 ≠ 中断阻塞 | `69ddc04` | 单次 read 用光预算并返回 EOF 时不再当"读完"；剩余时间落到 socket；EOF 越界即超时 | `p1c3_read_deadline_20260928.md` |
| **P1-c②** 状态跨层丢失 | `764ce20` | `[]` 不再是一种状态：四条出口各记真值（`attempts` 是**真实发出的调用数**）；`search_status` 作 `result` 的**兄弟字段**上行，编排器算 `search_verdict{completed, zero_hits, attempted}`；`[无新查询]` 让 Worker 在**发请求之前**停（不回退契约查询）；`_dispatch` 的重建结果**原地写回**计划对象 | `p1c2_cross_layer_status_20260928.md` |
| **P1-d** 按新材料生成候选正文入口 | `fd46427` | `POST /api/task/<id>/candidate` → `handle_regenerate_candidate`：确定性装配（不调用模型）、**不采纳**、按（材料+契约+采纳版）幂等、只重做产出正文的步骤、旧工件与人工文字保留、`allow_paid` 明确 409 | `p1d_candidate_entry_20260928.md` |
| **页面层视觉核验**（遗留项） | `c407483` | 登录后开冻结样本逐面**截图判读**，看到并修掉 3 处**单测抓不到**的渲染层缺陷（字面星号 ×3 处、「结论速览」把吨位当结论） | `page_visual_check_20260928.md` |

> **P1-c③ 是补交**：它的实现与 4 条用例被分开在两处——用例随 `3a84b3c` 入库而实现留在工作区，
> 那一版 HEAD 上的 `test_search_quality_unified` **会红**。发现后立即补交（`69ddc04`），
> 并把这次"实现与用例分家"记在这里备查。

**P1-b 的行为口径变化（明说）**：未改任何阈值/门禁参数，但归因倒置的结论现在会进
`unsupported`（原来可能 `partially_supported`），因此**可能**让某份交付从"就绪"变"草稿"。

**P1-c② 的口径边界（明说）**：`_normalize_result` **刻意不改顶层 status**——
`refused_budget`/`providers_cooling`/`stopped_no_new_queries` 是**我们自己没发请求**，
按提供方故障处置会误触发熔断与重规划；`timeout` 这类也不在那里改判，只把真值说出来。

**回归**：本轮各批合计 `test_delivery_chain` / `test_offline_delivery` / `test_p0` /
`test_root_budget` / `test_startup_readiness` / `test_search_quality_unified` /
`test_orchestrator_v2` / `test_net_policy` / `test_narrative_evidence` /
`test_question_assessment` / `test_acceptance_adversarial` / `test_financial_chain` /
`test_fact_fidelity` / `test_report_quality` / `test_us_chain` / `test_facts` /
`test_review_edit_api` / `test_task_projection` / `test_writer_consolidation` 全绿
（单批最多 **1164 项**一次跑完 OK，1 skipped）。

## P1-d 已交：按新材料生成**候选正文**入口（证据 `docs/evidence/p1d_candidate_entry_20260928.md`）

那条"另行授权"的接缝**已接上**：补材料链此前刻意停在正文之前
（`handle_add_material()` 最后一句原话："正文需按新材料重生成时另行授权"），
现在 `POST /api/task/<id>/candidate` → `OrchestratorV2.handle_regenerate_candidate()`
把那句接上了。六条要求逐条落实 + 各带用例：

1. 展示**材料/契约版本、影响步骤、预算与授权**：响应带 material/contract 指纹、
   `affected_steps`/`stopped_steps`/`budget`/`generation{mode,paid,model_called}`/`acceptance`；
2. **同一次动作只生成一个候选**：幂等键 =（材料身份 + 契约指纹 + **当前采纳版本**），
   命中即复用并标 `created:false`（用例断言版本库**不多出**第二个）；
3. **只重做依赖新材料的步骤**：只有产出正文的能力进 `affected_steps`，检索/清洗进 `stopped_steps`；
4. **旧工件与人工文字保留**：只 `VersionStore.record()`，**不** adopt、不写交付投影、
   不覆盖 `reports/report.md`；候选由**当前采纳版的分析节**重新装配（人工那段仍在候选里）；
5. **新候选需显式采纳、旧批准不继承**：`adopted:false` / `requires_explicit_adoption` /
   `old_approval_inherited:false` / `parent_version_id`=当前采纳版；验收对**候选正文**跑；
6. **不新增付费生成**：默认确定性装配（`model_called:false`）；带 `allow_paid` →
   **409 `paid_not_authorized`**，且**不投递、不落候选**（不悄悄降级）。

"已并入"的判据（实测修正）：`read_index()` 的条目**不带 `refresh`**，
所以逐条回读该材料 meta：准入 **且** 已并入快照 **且** `refresh.ok`。

**页面按钮已加并真机复看**（`frontend/src/components/console/MaterialPanel.tsx`）：
「按新材料生成候选正文」区块 + 按钮 + 前置条件提示；`frontend/dist` 已重建，
`web_ui.DIST_DIR` 指向它，刷新即生效。

**仍未接线**：模型重生成（`allow_paid` 明确 409）；未新增采纳端点（沿用既有
`VersionStore.adopt()`，`/review/edit` 那条路径）。

## P1 尚未做完（逐条给出处与"为什么没半做"）

1. ~~**P1-c② 状态跨层丢失**~~ —— **已交（`764ce20`）**。未覆盖的部分见证据文档：
   `result` 的形状**没有**改成结构化对象（严格读法是"内层结构化、边界转数组"；本批是
   "数组照旧 + 兄弟字段"，因为 4 处消费者按数组解析）；未新增"实际停止/清理时间分账"字段；
   未做"重启后把检查点里的指令读回来与派发时逐字比对"的恢复演练。
2. ~~**P1-c① 的"持久保存规范计划"**~~ —— **已交（`764ce20`）**：`_dispatch` 的重建结果
   改为**原地写回调用方持有的那个 step 对象**（它正是 `_publish_full_state` 推给页面、
   `all_steps` 里被持久化的那一份）；用例走**真实** `_dispatch`，断言第二次不再重建。
   **仍未做**："重启后把检查点里的指令读回来与派发时逐字比对"的**恢复演练**（需要一次
   真实恢复，本批只做到"写回计划对象"这一步）。
3. **P1-c① 的待裁决口径**：`conflicting_periods` 允许 `as_of` 年份（既有语义），
   故"洋河股份 … 2025年年度报告 全文"按契约自己的定义算"契约内"。本轮与 `violations()`
   同一把尺，**没有**另立更严口径。若认为重试批次应只允许 `periods` 内年份，请裁决。
4. ~~**P1-d 的另一半"补材料后按新材料生成候选正文入口"**~~ —— **已交（`fd46427`）**，
   六条要求逐条落实并各带用例；页面按钮已加并**真机复看**。**仍未接线**：模型重生成
   （`allow_paid` 明确 409）、未新增采纳端点（沿用既有 `VersionStore.adopt()`）。
5. **`_promotion_subject` 误判（P1-a 残留 19 处的成因）未修**：实测
   `_subject_of("需进一步取得利润表分项明细方可解释")` → `"需进一步取得"`，
   `_MEDIA_TOKENS` 含单字"报"会把"洋河股份合并报表口径下的…"整条丢掉；因此丢掉 56 处命中。
   这是**跨公司同值护栏**的判据本身，改它要单独一批 + 语料级回归；现状偏保守（少数诚实
   命中被否），方向安全。已把证据写进 `acceptance_checker._promotion_subject` 注释。
6. ~~**页面层视觉核验（截图）**~~ —— **已做（`c407483`）**：登录 → 开冻结样本 → 逐面截图判读，
   **看到并修掉 3 处单测抓不到的渲染层缺陷**（字面星号 ×3 处、「结论速览」把吨位当结论）。
   仍留着的一类：**步骤指令区**里的 `**` 是原样展示发给 worker 的指令文本（逐字展示是"如实"，
   改它等于改提示词），**没有**一并改掉，不算渲染缺陷。
7. **第二家公司原文、`clean_env_verified`、推送 CI、真人 F3 五项 ≥8/10** —— 仍缺，
   与上一批结论一致。
8. **`working_paper_export` 表头单位**（`fd46427` 顺带修）：旧实现取"第一行有单位的"，
   行序一变就把全表单位写成"吨"。**已更正定性**（`88c6976`）：这是**夹具复现的潜在缺陷**，
   冻结样本 `ui-a06a005c9b` 的 `scope.unit` 实测为 `亿元`（财务行在前），当时没踩到。

## P0 收口：三处遗留全部闭合（证据见 P0-d/P0-e 两份文档的"补完"节）

1. **编排器侧查重改为带作用域**：`find_by_idempotency(..., scope=...)`（关键字参数，
   保位置兼容），`accept_task_request` 传 `receipt_scope(user, project, "task.submit")`，
   匹配规则与 `claim_receipt` **完全一致**（同作用域 + 作用域为空的历史行）。
2. **P0-c(2) 运行选项落库**：新增 `run_options_json` 列，`claim_receipt` 收
   `auto_run`/`template_steps`/`report_confirm` 并落库，`list_unstarted_queued` 带出，
   `resume_unstarted_queued` 按**原请求**恢复（不退默认值）——崩溃恢复不再把
   "先确认计划""模板步骤""报告确认"悄悄丢掉。
3. **时间线并发写不再丢更新**：`record_submit_event` 的"读-改-写"改为
   `BEGIN IMMEDIATE` 写锁事务（原实现两个写者会互相覆盖）。

**验证**：`test_task_persistence` **47 项 OK**（+2：恢复携带原运行选项；
两个写者并发追加**两个事件都保留**）、`test_writer_consolidation` **60 OK**、
`test_p0` **416 OK**、`test_delivery_chain` **373 OK**、`test_startup_readiness` **61 OK**。

**仍属未验（不是未修的代码问题）**：跨进程并发、真实 Redis 上的发布失败/双击演练、
真实崩溃演练——都需要第二个进程或停用户服务，按指令"真实 Redis 验证只用隔离前缀、
不杀用户服务；不可用则明确未验"，本轮只做单进程并发与替身覆盖。

## P0-e 已交：时间线区分「发布意图」与「确实发布」（证据 `docs/evidence/p0e_timeline_stages_20260928.md`）

- **缺陷**：`_publish_task` 在调用 `r.publish` **之前**就把 `published`（"已发布到
  orchestrator:main"）写进收执——发布抛错或进程在两步之间死掉时，**时间线在撒谎**，
  事后无法区分"没发出去"与"发出去了没被消费"。
- **修复**：事件词表新增 **`publish_intent`**；落收执只带 `received` + `publish_intent`
  （下发编排器的 `submit_events` 同）；`r.publish` 包 try/except——失败**不记 published**、
  抛错（收执保留 RECEIVED 交给 P0-c 的恢复）；成功后才由 web_ui 补记真实 `published`。
  对账口径变为四段可分：`publish_intent → published → consumed → started`。
- **验证**：`test_writer_consolidation` **60 项 OK**（+2：失败时**有意图、无 published**；
  成功时 `published` 必在 `publish_intent` **之后**）；既有用例的下发事件断言更新为
  `["received","publish_intent"]`。
- **回归**：`test_p0` **416 OK**、`test_delivery_chain` **373 OK**、`test_startup_readiness` **61 OK**。
- **未验项**：未在真实 Redis 上制造发布失败观察时间线与恢复联动（失败路径为单测覆盖）；
  时间线是"读-改-写"，web_ui 补记 `published` 与编排器写 `consumed` **理论上有丢更新窗口**
  （本批未改并发写协议，如实记录）；编排器侧 `find_by_idempotency` 仍是**全局**比对；
  未做跨进程并发验证；未跑付费整链。

## P0-d 已交：同作用域幂等键**原子**绑定唯一任务（证据 `docs/evidence/p0d_atomic_idempotency_20260928.md`）

- **缺陷**：旧路径"先 `find_by_idempotency` 查、再 `mark_received` 插"是两步无互斥 →
  两个并发请求**各落一行 RECEIVED**；消费 A 映射到 B 后库里留下 A，启动恢复又执行 A
  （同一份提交执行两次、重复付费）。且查重**不带作用域**：不同用户自造的键撞车时，
  一方的提交会被判成另一方的重复而丢弃。
- **修复**：新增 `task_state.claim_receipt()`——查重与落收执合成一个 `BEGIN IMMEDIATE`
  事务，返回 `created` / `duplicate`（同作用域同键**同内容**）/ `conflict`（同键**不同内容**，
  明确冲突）/ `error`（不落库不执行）；作用域 = **可信用户|工作区|操作**（`user` 取会话身份，
  不接受请求体自报）；内容指纹 = 目标+项目+研究契约（不含会话/上下文，避免把重试误判成冲突）；
  新增 `idem_scope`/`request_fingerprint` 两列；`mark_received` 改走同一裁决并保留旧布尔契约
  （**conflict → False**，不假装成功）；**历史重复行不删**（保留审计）。
- **验证**：`test_task_persistence` **45 项 OK**（+6，含**两线程并发同键** → 只有 1 方拿到
  `created`、库里只有 1 行；换用户/换工作区各自独立；冲突不落第二行）。
- **回归**：`test_p0` **416 OK**、`test_writer_consolidation` **57 OK**、
  `test_startup_readiness` **61 OK**。
- **P0-d(2) 已闭合**：`web_ui._publish_task` 删除"先查后插"的前置检查，改为**先 `claim_receipt`
  再决定是否发布**——`created` 才发布；`duplicate` 复用既有任务**且不发布**；
  `conflict`/`error` 抛错且不发布。兼容规则：`idem_scope` 为空的行（旧实现/`mark_queued` 落的）
  仍参与全局按键匹配，且因指纹为空**保守判 duplicate**（复用、绝不重复执行）。
  新增用例"冲突不得发布"，并把"落不了收执要报错且不发布"的打桩从旧 `mark_received`
  换成 `claim_receipt`（场景与断言不变）→ `test_writer_consolidation` **58 项 OK**。
- **仍余一项未统一**：编排器侧 `find_by_idempotency` 仍是**全局**比对（未带作用域）；
  当前流程下不造成重复执行（同一提交带同一 `task_id`，走 `promote_received` 裁决），
  但两处语义尚未完全一致。未做跨进程并发验证；未跑付费整链。

## P0-c 已交：恢复覆盖「已 QUEUED 但从未开始」的崩溃窗口（证据 `docs/evidence/p0c_queued_crash_window_20260928.md`）

- **缺陷**：`promote_received` 把 RECEIVED→QUEUED 之后、执行线程起来之前崩溃 → 该行是
  **QUEUED**，而旧恢复只扫 RECEIVED，**永久漏掉**（任务卡在"排队中"，既不执行也不失败）；
  且只有启动时扫一次，运行期崩溃覆盖不到。
- **修复**：新增 `task_state.list_unstarted_queued()`（判据是**时间线里没有 `started`**，
  不是猜）+ `orchestrator_v2.resume_unstarted_queued()`（执行权复用 `mark_running` 的原子
  `UPDATE … WHERE status IN (QUEUED,'PENDING')`，谁改成 RUNNING 谁执行）+
  `start_recovery_loop(60s, 每轮每类 20 条)` 有界周期恢复；启动时也扫一次。
  与 accept/收执恢复**共用同一条派发闸门**（未持有归属 → 不恢复、不改状态）。
- **不重复收费**：恢复走正常执行路径，证据/快照按既有幂等规则复用，不新建付费调用。
- **验证**：`test_task_persistence` **39 项 OK**（+3）；其中反例断言先证明该行
  **不在** `list_received`（旧恢复确实看不见它）。
- **回归**：`test_startup_readiness` **61 OK**、`test_writer_consolidation` **57 OK**、
  `test_p0` **416 OK**、`test_orchestrator_v2` **79 OK**。
- **未验项**：**保留原请求的 `auto_run`/`template_steps`/`report_confirm` 未做**——
  这三个字段当前**没有落库**，需加 `run_options_json` 列并从 `_publish_task` 一路写下来
  （P0-c 第 2 小批）；未做真实崩溃演练与多轮周期扫描长时间观察；
  **P0-d/P0-e 未实施，"同键并发只留一个可执行收执"仍未验证**。

## P0-b 已交：实例归属改为真实租约（证据 `docs/evidence/p0b_owner_lease_20260928.md`）

- **三个洞**：①心跳只在认领时写一次（30 秒后过期 → **A 还活着 B 就能接管**）；
  ②`cur == inst` 即放行 → **同名实例**的两个进程互相当成"自己的旧租约"；
  ③认领异常 fail-open "按单实例继续" → 证明不了唯一执行权仍派发。
- **修复**：每进程唯一令牌（`实例名:pid:随机`）；认领/续期/拒绝**三合一 Lua 原子**；
  租约（TTL）取代一次性心跳 + 后台续租线程；**续租必须带令牌比对**（被接管后不得续命）；
  新增派发闸门 `ownership_held()`——`accept_task_request` 回 `rejected:no_ownership`、
  `resume_received_tasks` 返回 0；认领异常仍不阻断启动但**闸门关闭、停止新派发**。
- **顺带挡下一个真实运维陷阱**：旧实现 `SET owner inst` **不带 TTL**，那个键永不失效 →
  新实现会把它当"别人的活租约"而**永久拒绝启动**；加 `_OWNER_LEGACY_TAKEOVER_LUA`
  **仅当 `PTTL == -1`** 时原子接管。
- **验证**：`TestOrchestratorOwnership` **9 项 OK**（含"同名实例必须被拒""被接管不得续期"
  "遗留无 TTL 键可迁移""闸门关闭时不接收也不恢复"）。闸门波及的 4 个 `setUp` 显式声明
  "本实例持有归属"（那些用例测收执/幂等/时间线，不测闸门）。
- **回归**：`test_startup_readiness` **61 OK**、`test_task_persistence` **36 OK**、
  `test_writer_consolidation` **57 OK**、`test_p0` **416 OK**。
- **未验项**：**本机应用整个处于停止状态**（无 python 进程、6379/8080 未监听）→
  遗留键迁移与跨租期两进程互斥**只有单测覆盖，真实 Redis 未验**（按指令"不可用则明确未验"）；
  未接优雅退出的 release 调用点（靠 TTL 过期接管兜底）；
  **P0-c/d/e 未实施，"同键并发只留一个可执行收执"仍未验证**。

## P0-a 已交：收执裁决区分「异常」与「缺行」（证据 `docs/evidence/p0a_receipt_verdict_20260928.md`）

- **缺陷**：`task_state.promote_received()` 遇**数据库异常**返回 `"absent"` → 调用方走旧路径
  `mark_queued`，于是一条**正在 RUNNING** 的任务遇一次连接异常仍回 accepted 且无 `_skip_run`，
  允许再次启动；收执仍在库却被当成"老路径请求"，绕过唯一执行权裁决点。
- **修复**：裁决分四种 `promoted` / `already` / `absent` / `error`（`absent` 语义保留，只把异常摘出来；
  推进未中而**读**也失败同样报 `error`——读不出来 ≠ 没有收执）；
  `accept_task_request` 新增 `error` 分支：**拒绝执行、不落回旧路径**，回执
  `rejected:receipt_error`，收执留在 RECEIVED 等下次恢复。
- **验证**：`test_task_persistence` **36 项 OK**（+5 新增；3 项既有断言从旧原因串更新为新契约，
  场景与强度不变，只是"更靠前被拒"）。
- **顺带修掉假 ERROR**：测试里 `subprocess.run(text=True)` 未钉编码 → 本机 GBK + 非 ASCII 路径
  下 `UnicodeDecodeError` 让断言退化成 `TypeError`；修 3 处（含 `test_delivery_chain.py` 1 处）。
  连带发现：`test_startup_readiness` 的编码守卫**只扫产品码**，测试文件同类缺陷无人拦（未修）。
- **回归**：`test_startup_readiness` **57 OK**、`test_delivery_chain` **373 OK**。
- **未验项**：未做真实 Redis 双进程验证；**P0-b（租约）/P0-c（QUEUED 崩溃窗口）/P0-d（同键并发
  原子绑定）/P0-e（时间线分段）尚未实施**，故"同键并发只留一个可执行收执"**仍未验证**。

## C4 真实样本已开跑：①洋河两次 + ②补材料恢复（证据 `docs/evidence/c4_samples_run_20260927.md`）

用户充值后端点恢复（`01:08` 主备均 ok），三条样本按预声明计划开跑。**结论：都还没通过**。

- **样本① 第一次（无材料，`ui-29e8ca73b5`）**：真实浏览器点击提交，62 秒 / 1 次调用 / $0.0029；
  `FAILED`——机器验收 pass 且绑定正文，但**研究硬门槛未通过**（`located=0`、必答问题未完成 2/3）。
  根子是我**没按计划给真实年报材料**。
- **样本② 第一、二步（机制验证通过）**：拿上面那个失败任务当"资料不足"，走**页面补材料入口**。
  镜像站 URL 取件只拿到 985 字节被正确拒收（判"扫描件"）；换**巨潮官方直链**后
  `admitted`：`located 0→13`、`official_disclosure`、`periods=[2023,2024]`、时效 `within`、
  三指标全 `present_in_scope`、身份指纹 `7d593395→f6326002`；**只重做证据/结构/底稿（底稿 46 行）**，
  正文重生成列为待办。重投同材料 → `duplicate=true` 未重复摄取；全台账 **0 个重复幂等键**；
  时间线 `received→published→received→consumed→started`。
- **样本① 第二次（带巨潮直链，`ui-a06a005c9b`）**：12 分 10 秒 / **15 次调用** / **$0.1145**；
  应用自己抓下完整年报（4,699,879 字节）、6 张图、`PACKAGE_MANIFEST.json`；
  正文用上发行人口径（毛利率 75.25%→73.16%、净利率 30.24%→23.11%）。
  但**机器验收 `fail`**：数字溯源率 **52%（210/406）** < 阈值 70%，财务金额溯源率 50%（68/137）。
  台账终态 `SUCCESS_WITH_ISSUES`。**不符合样本①通过判据**。
- **本批新发现（均未修，见证据 §4）**：①派生金额（Δ归母净利润/Δ毛利/毛利线以下净额）
  按"无引用"计入不可溯源，且 `9536/9573` 这类**页码被当财务数字**进分母；
  ②`web_search` 4 次"契约指纹不一致"重试同一条查询、每次都记 SUCCESS，最终仍判检索失败且 0 候选 URL；
  ③`/actionable` 把失败归因成"code_sandbox 不可用→看健康页"，真因是检索/材料与验收门槛、
  正确动作应是**补材料**（C3 面缺陷）；④**补材料闭环缺正文重生成入口**（只有"待授权"待办），
  故"覆盖提升"无法闭环；⑤取件对镜像站失效、对官方站正常，需区分"下载失败"与"PDF 无文本"；
  ⑥embedding 真不可用（`MODEL_CAPABILITY_NOT_SUPPORTED`）。
- **未做**：样本③（中国平安）未跑；真人 F3 评分未做；`clean_env_verified` 仍 false；
  第二家（600031）原文仍未取得。

## 🔴 当前阻塞：C4 三份样本跑不了——LLM 账户余额耗尽（证据 `docs/evidence/h4_llm_balance_health_20260927.md`）

- **现象**：用**真实浏览器点击**（Playwright，登录→研究表单→提交）跑样本①洋河 `002304.SZ`，
  实时动态报 `❌ 全部 LLM 端点余额不足，请充值后重试`（服务端 503）。
  任务**未创建、未执行、无任何产物**。UI 生成的目标文本与页面
  `buildResearchGoal()` 产物逐字节一致，契约字段齐备（`cn`/`合并`/`equity`/2023-2024/2025-04-30、无缺口）。
- **实测**：强制重探 0.2 秒返回；原始异常
  `HTTP 402 {"code":"INSUFFICIENT_BALANCE","message":"余额不足","data":{"retryAfterSeconds":12}}`；
  主备同主机 `tokenrhythm.studio`；92 秒内每 10 秒探一次 **10/10 失败**，
  `retryAfterSeconds` 是每分钟滚动窗口 → **不是限流，是余额/额度真的耗尽，无自愈窗口**。
- **下一步**：先给该账户充值，**不需要再改代码**（三份样本的契约与入口都已就绪）。
- **附带发现（未改）**：`diversity.reason="same_host"`——主备端点同主机，"双端点余额预检"
  实为同一厂商查两次，**备用端点不提供冗余**。

### 顺手修掉的假绿：欠费时健康页报"可用"

- **症状**：同一份 `/api/status` 里，`llm` 项写 `state=available / detail=primary=ok, backup=ok`，
  而 `llm_health.balance` 写主备均 `insufficient_balance`、`llm_warning` 为空——两个信号互相矛盾，
  这正是"环境看起来正常"的原因。
- **根因**：`health_registry.probe_llm()` 只看 `_endpoint_health[*].healthy`，而它是**带失败阈值**的
  瞬时状态（实测欠费时仍是 `healthy=true, fails=1`）；异常分支还回 `ok=True`（读不到也说可用）。
- **修复（只动诊断面）**：新增 `llm_client.balance_terminal_reason()`（两端**都**终态才返回原因，
  只读缓存结论）；`probe_llm()` 合并该结论（有它就不再报可用，`detail` 追加 `balance=`，
  `healthy` 仍如实保留），异常分支改为 `_unknown_entry`。
- **明确没改（属门禁，交用户决定）**：`_post_task` 余额预检语义不变；`_BALANCE_COOLDOWN = 600s` 不变
  ——**代价：充值后最长要等 10 分钟才恢复接单**。
- **验证**：`test_p0` 416 项（+4 新增）/ `test_actionable_state` 31 / `test_settings_requirements` 26 /
  `test_sandbox_isolation` 24 / `test_startup_readiness` 57，**逐文件全绿**。
- **未验项**：没有余额正常的账户可做正向验证，`probe_llm` 在正常环境的行为**仅 mock 覆盖**。

### 环境侧复验（用户操作后）

- 16 个服务**真重启**：`00:34:34` 停（含便携 Redis）→ `00:34:46` 全部拉起
  （此前 `start.bat` 会被启动器"本实例已在运行…复用而不重启"挡掉，**必须先 `stop.bat`**）；
- Playwright `chromium-1243` 在盘、`browser_evaluate` / `browser_recipe_run` 可用
  （会话审批策略已放行）→ **浏览器点击式路径可用**；
- OpenCLI 桥**仍未通**（`opencli doctor` → `Extension: not connected`），本路线不需要它。

## C4 进行中：便携包已重建，真实样本与真人评分未做（提交 `761291b`/`c581fa4`，证据 `docs/evidence/c4_run_package_20260927.md`）

- **便携包重建一次**（C4 第 1 条，C0–C3 稳定后）：`dist/weavemind-2026.09.27.2-win-x64.zip`，
  sha256 `f246064ae482eb58…`，289,472,753 字节 / 23,754 条目；源码提交 `761291b`，
  脏状态 diff 指纹 `0e578233…`，前端整树 `19b71d21…`（与仓库 `index.html` 引用一致），
  Windows 依赖锁 108 条 `659e4434…`；包内自检**密钥 0 / 开发路径 0**，并已用**包内解释器**
  验证 Python 3.11.9 / OpenSSL 3.0.13 / SQLite 3.45.1。
- **包内容逐项核对**：上一版缺的 S3（`adapters/disclosure_ingest.py`）与 C1/C2/C3、
  Redis 修复标记、H1 标记**都在包内**；`scripts/scoring_handoff.py` 不进包属预期。
- **同版核对 + 评分交接自动化**（C4 第 3 条）：新增 `scripts/scoring_handoff.py`，从工作区实读
  当前采纳版本 / 最新交付包 sha256 / 包内 `PACKAGE_MANIFEST.json` 的 `report_version_id`，
  当场判同版。对真实洋河样本 `ui-706c5ef4a5` 实测 **✅ 同版**（身份 `d962e1d3…` 一致，
  包 `a21fe35b31…`）；旧交接页里手写的旧包链接已加更正指针。
- **加载版本可查**（C4 明确要求）：新增 `code_version.py`（读 `.git` 得短 HEAD，脏加
  `+dirty`，无 `.git` 如实返回空），已被提交时间线使用；实测本仓库 `761291b+dirty`。
  同时记录：当前运行实例启动于 22:58:28（加载 ≈ `b034c4b`），**比本包旧**，
  要谈"同包验收"需重启一次。
- **未做（阶段 D 剩余出口条件）**：①三条真实样本（三问可回答 / 资料不足+补材料恢复 /
  金融主体边界）需**付费运行**，按指令"不新增付费研究生成"本会话未发起；
  ②真人 F3 五项评分 ≥8/10（每份、无严重问题）——空表或模型自评不能代替；
  ③第二家公司（600031）真实原文仍未取得；④`clean_env_verified: false`（N4 归口）。

## 运行环境缺陷修复：便携 Redis 落盘目录（已交 `35b83ac`/`e282090`/`45c3d1d`，证据 `docs/evidence/redis_dir_misconf_20260927.md`）

- **症状**：16 个服务都在跑，但**一个任务都提交不了**；`PING` 直接回 MISCONF。
- **根因**：便携 Redis（msys2 构建）启动时**没给 `--dir`**，于是拿进程 CWD 当 `dir`，
  msys2 把它报成 `/portable/Redis-…` 这种 POSIX 形态 → Windows 上 `bgsave` 永远失败 →
  `stop-writes-on-bgsave-error=yes` 进入 MISCONF → **所有写命令被拒**。
  `dir` 在 Redis 8 是 **protected config**（`CONFIG SET` 报 can't set protected config），
  运行期改不了，只能启动时给对。
- **修复**：`dep_check._redis_start_argv` 显式追加 `--dir <正斜杠绝对路径>` +
  `--dbfilename dump.rdb`；数据目录取数据根下 `redis/`，**非 ASCII 路径退到
  `%LOCALAPPDATA%/WeaveMind/redis`**（msys2 对中文命令行参数不可靠）；新增
  `verify_redis_persistence` **启动后自检**（dir 是否符合预期 + 真跑一次 BGSAVE 看
  `rdb_last_bgsave_status`），不通过就在启动时打印原因与可执行建议。
- **本机实例只做了应急解封**：`CONFIG SET stop-writes-on-bgsave-error no` → `PING/PONG`、
  读写恢复正常、`/api/health` HTTP 200；但 **RDB 仍落不了盘**（dir 改不了），
  **需在正常终端重启一次 Redis 才真正修好**。本会话起不了新的 msys2 Redis
  （`NtCreateDirectoryObject … 0xC0000022`，与 `--dir` 指哪儿无关），先停再起会让应用彻底无 Redis，
  故未停。
- **修订（同一日，用户重启后复验，提交 `e282090`）**：`dir` 的修复**确实生效**
  （重启后已是 ASCII 目录），但 `BGSAVE` 仍失败——`logs/redis.log` 显示本机
  **msys2 便携版 RDB 保存不可靠**（fork 子进程 `0xC0000142`／临时 RDB 文件 Permission denied）。
  于是改为：便携版 `--save "" --appendonly no --stop-writes-on-bgsave-error no`
  （**不存快照 + 保存失败永不锁写**），系统/外部 Redis 不加这些开关；
  自检从"验 bgsave"改为 **`verify_redis_writable`**（SET/GET/DEL 探针 + 报告持久化模式）。
  **代价明确**：便携 Redis 现在不落盘（丢的只是可重建的运行态；台账/产物在 SQLite 与工作区）——
  此前"以为有 RDB"是**假持久化**，比明确不持久更危险。
  本机实例已就地 `CONFIG SET save ""` + `stop-writes=no` 稳定下来。
- **诊断经验**：redis-py 6 连接时会发 `CLIENT SETINFO`（改服务器状态），MISCONF 下**连 PING 都失败**；
  诊断这类实例必须用**裸 RESP**（脚本在 `.tmp/`，不进库）。
- **顺带实测**：重启后的新 Redis 里唯一的键是 `orchestrator:owner`——H3a 的**实例归属认领
  在真实环境生效**。

## C3 收口：新人可行动状态统一（已交，提交 `ad683f1`，证据 `docs/evidence/c3_newcomer_states_20260927.md`）

- **六态映射**（`actionable_state.classify_task`）：未接收 / 待消费 / 执行中 / 待材料 /
  明确失败 / 已完成，每个状态配**唯一**推荐入口（去设置 / 健康页 / 补材料 / 重试 / 看详情 /
  等待）。失败时优先指向**能修的原因**（模型未配置 → 去设置）；**认不出的状态按未接收处理**
  （不许猜成"在跑"让用户白等）；已完成固定提醒"机器验收 ≠ 研究通过"。
- **唯一一份健康视图**（`unified_health`）：四态 + 实例 + 观测时间 + 汇总；
  **未知/过期/异实例/空视图一律不算绿**；旧 `source_health` 只在依赖快照未覆盖该源时补条目
  并标 `legacy`（同一件事不再显示两遍）。`/api/status` 增加 `health` 字段，
  `dependencies`/`source_health` 保留为兼容别名。
- **一个入口**：新增轻量端点 `GET /api/task/<id>/actionable`（口径与任务页同一份实现）；
  前端 `lib/actionable.ts` 把动作译成页面意图，TaskConsole 顶部一条提示 + 按钮
  （**只有已完成才是绿色**，待材料/待消费是黄色）。`frontend/dist` 干净重建。
- **CI**：纳入 `test_actionable_state.py`，**48 步 == 48 个测试文件**（双向差集仍为空）。
- **C3 四项验收**：①同幂等键只建一个任务、只执行一次 ✅（代码+单测）；
  ②接收后重启可查收执并恢复 ✅（收执/幂等键/时间线持久化 + `resume_received_tasks`
  原子推进，只执行一次）；③缺材料补入后继续且不重复已完成调用 🟡（C1 已实现"只重做依赖
  环节"，本批纳入可行动状态；**实机链路未复验**）；④两实例同 Redis 不重复处理 🟡
  （启动闸门 + 执行权单一裁决均有单测，**未做真实双进程复验**——本机 Redis MISCONF 拒写）。
- **未验/未做**：真实双进程 + 真实 Redis；真实 UI 端到端演练（同键重复提交只跑一次、
  杀编排器再启动自动恢复、等材料→补材料→继续）；健康页仍按原样展示 `dependencies`
  （未把收执时间线可视化）；**C4 全部未开始**。

## H3b 收执先落库 + 启动恢复 + 网页幂等键（已交，提交 `b2cf84c`/`c90f8c8`/`1443de6`，证据 `docs/evidence/h3b_receipt_recovery_20260927.md`）

- **先落收执再触发工作**：新增状态 `RECEIVED`（待消费）。网页在发布**之前**把收执写进
  任务库（`mark_received`，含 received/published 时刻与实例）；落库失败**不发布**并抛错。
  旧行为是"编排器收到消息才登记"——编排器没起来或落在崩溃窗口里时，请求只剩一条
  120 秒 TTL 的 Redis 键，用户看到超时却查不到任何东西。
- **执行权单一裁决**：`promote_received()` 用 `UPDATE … WHERE status='RECEIVED'` 决定
  谁执行（`promoted`/`already`/`absent`）。消息路径与"启动恢复"路径共用它，
  同一条请求因此**不会被执行两次**；已被推进过的消息只回执、不执行。
- **启动恢复**：`resume_received_tasks(older_than=10)` 在订阅之前把"收执已落库但从未
  消费"的任务捡回来执行（阈值 10 秒避开正在飞的那条消息）。`mark_queued` 遇到已有
  收执行改为**推进**（不再主键冲突），且**绝不把 RUNNING/终态改回 QUEUED**。
- **网页幂等键**：新增纯函数 `frontend/src/lib/submissionKey.ts`——同一提交意图的重复
  点击/失败重试复用同一个键（服务端只建一个任务），成功后或改题换新键（主动再跑一次
  仍是新任务）。`frontend/dist` 干净重建。
- **收口点提取**：把 main() 内联的 `_run_task` 提为模块级 `run_and_finalize`，
  消息路径与恢复路径共用（避免两份兜底落库实现）。
- **验证**：`test_task_persistence`+`test_writer_consolidation`+归属用例 **94 例 OK**；
  按项目约定与 CI 口径**逐文件**跑：`test_startup_readiness` **57 OK**、
  `test_delivery_chain` **373 OK**、`test_p0` **412 OK**；
  前端 `node --test` **51 例全过**、`tsc -b` 通过、`test_frontend_guards` **53 例 OK**。
- **被改断言 3 处（非放宽判据，逐条理由见证据 §3）**：①"webui 一行都不写"→
  "只写一条 `RECEIVED`，不得写 QUEUED"；②超时用例由"必须抛错且无行"→
  "如实返回 `ack_pending_consume` + 恰好一条 RECEIVED"；③源码文本断言由内联
  `_run_task` 改指模块级 `run_and_finalize`。
- **未做**：新人可行动状态统一（未接收/待消费/执行中/待材料/明确失败合成一张表）、
  "两实例同 Redis 不重复处理"的真实双进程复验、真实 UI 端到端演练（同键重复提交只跑
  一次 / 提交后杀编排器再启动自动恢复）；C4 全部未开始。

### 回归与事故记录（本轮自查，照实留痕）

- **单进程全量跑的 4 个失败＝基线一致，非本批回归**：为确认不是自己改坏的，另开
  `2519d9a`（本会话开始前）的 git worktree 用**完全相同**的单进程全量命令跑了一遍——
  本会话 1101 例得 2 failures + 2 errors，基线 1076 例得到**完全相同的 2 failures + 2 errors**
  （两个 metrics 指标聚合用例 FAIL、图表渲染与"新进程读同一库"两例 ERROR）。
  性质：**跨文件同进程的测试隔离问题**，不是产品缺陷，**不影响 CI**——CI 与 `AGENTS.md`
  都是"一个文件一个进程"，按该口径逐文件跑时这 4 项全部通过。已定位触发者：
  `test_startup_readiness` 与指标两例同进程即复现，单跑通过；**根因未定位**
  （`db_paths.resolve_db_path()` 实测不缓存，"路径被缓存"假设已排除）。是否立项修留给架构师。
- **测试曾误写真库（已修复）**：H3b 第一版测试里有若干 `_publish_task` 用例只重定向了
  `web_ui.DB_PATH`、没重定向 `task_state.DB_PATH`，新加的"先落收执"于是写进了
  **真实的 `agents.db`**（7 条 `RECEIVED`，13:59:43–14:00:01，两条带测试幂等键
  `k-ev`/`k-race`）。危害：污染用户任务历史，并让上述指标用例在**全量组合**下失败。
  处置：按**精确 task_id** 删除这 7 行（未用状态/时间通配），真库 `526 → 519` 行、
  `RECEIVED` 归零；并给全部 8 处 `_publish_task` 调用点补上 `task_state.DB_PATH` 重定向。
  备份/日志/**数据库文件本身**未删除、未移动。

## H3a 持久化收执 + 幂等提交 + 实例归属（已交，提交 `afb2f34`，证据 `docs/evidence/h3a_receipt_idempotency_20260927.md`）

- **幂等键**：web_ui 发布前查一次（命中就复用、**连发布都不做**），编排器侧再查一次
  （竞争时先落库者赢）——命中则**不登记、不执行**，收执 `accepted:dedup:<已有 id>`，
  并把 `data["_effective_task_id"]` 指回原任务，消息循环据此跳过第二次执行。
  任务库新增 `idempotency_key`；**没有键时行为与之前完全一致**（刻意保留的兼容边界）。
- **持久化收执 + 提交时间线**：任务行新增 `submit_timeline_json` / `accepted_by`，
  逐段记 **received / published / persisted / consumed / started**（含时刻、实例、
  代码版本）。收执不再只活在 120 秒 TTL 的 Redis 键里：重启后仍可查（幂等键、收执行、
  时间线三样都在）。收执超时先查任务库，已收执则返回 `ack_pending_consume`，
  不再谎报"任务未被创建"（那句会把用户引向重复提交）。
- **实例归属**：启动时认领 `orchestrator:owner` + 心跳（30 秒）；已有**活**实例时
  **明确拒绝启动**（返回非零），心跳过期才允许接管——两个编排器订阅同一条
  `orchestrator:main` 会把同一条请求执行两次，属"默默重复消费"，本批改为显式拒绝。
  登记失败也不再启动执行（避免"界面失败但实际在跑"的反向幽灵任务）。
- **验证**：新增 11 例（幂等 6 + 归属 5）全过；`test_task_persistence` +
  `test_writer_consolidation` **92 OK**；含 startup/cancel/review/budget 在内 **307 例**中
  除 2 条**沙箱管道受限**的既有用例（`subprocess` + PIPE、`tasklist` 探活 pid）外全过，
  与本批改动无关。
- **文档更正（指令明确要求）**：把 clean-env-e2e 偶发收执超时的结论由
  "判定为 runner 侧抖动、不是本次改动引入的回归"更正为 **"间歇性收执失败，根因未确定"**，
  并写明用提交时间线定位、不放宽 60 秒判据、不靠反复重跑到绿。
- **未做（C3 未整批退出）**：①前端还没生成幂等键（HTTP 已支持字段与 `Idempotency-Key` 头，
  所以真实双击目前还不走去重）；②重启后"把从未 consumed 的任务重新投递"未做；
  ③新人可行动状态（未接收/待消费/执行中/待材料/明确失败）未与 health 四态合成一张表；
  ④"两实例同 Redis 不重复处理"只有替身单测，**未**用真实双进程 + 真实 Redis 复验
  （本机 Redis MISCONF 拒写、沙箱内起不了私有 Redis）。

## H2 问题保留与证据性质可见（已交，提交 `5bd150e`，证据 `docs/evidence/h2_question_retention_20260927.md`）

- **反转 C2 §5 的取舍一（负基数问题不消失）**：删掉 `report_brief._research_questions` 里
  "同比算不出就不出这一问"的 `continue`。改后保留问题身份与分母，观察改为
  **绝对变化**（`-10亿元（2023）→ 8亿元（2024），绝对变化 +18亿元`）+ **可比性限制**
  （两期存在非正值、不给百分比方向）+ **未完成原因**（新增 `retained_reason`，
  正文以"说明："露出；结构字段加 `yoy_computable`）。评估分母本来就是三问，现在
  读者可见的问题数与评估分母重新一致。
- **反转取舍二（证据性质必须可读）**：性质从"只在结构对象"改为**进正文**——
  页面/PDF/ZIP 与 Markdown 同源（`ReportViewer` 渲染 `/api/task/<id>/report.md`），
  所以落在 Markdown 即四者齐备。做法是**加进既有那一行**（不新增正文行），两处：
  『关键判断与下一步』的"依据"行与『研究问题与下一步』的材料行。
  诚实性细节：覆盖为 `none` 时写"尚未取得可判定的证据"，不把"该问需要什么性质的证据"
  冒充成"已有这种性质证据"。
- **分页（原取舍的理由）已单独验证**：`test_offline_delivery` 冻结场景 **34 例 OK**，
  含 `pdf_parseable`＝无空白页判定——保留负基数问题**且**加性质文字后 PDF 仍无
  "<50 字符且无图"的空白页。原"删题迁就分页"的理由在本轮改法下不再成立。
- **补 C2 残留项**：新增结构级稳定性断言（同一输入两次装配 → 正文逐字节一致、
  问题/风险的数量与类别一致）。
- **验证**：`TestResearchQuestions` 16 OK（含四类适用性用例）／报告+质量+叙事 140 OK／
  底稿+事实+对抗验收+版本 148 OK／`test_review_edit_api` 15 OK（编辑→新版本→下载同版、
  旧批准不迁移）／冻结场景 34 OK。
- **未验**：真实浏览器点选渲染（本会话不登录、不伪造会话）；真人复核仍属 C4。
  本地验证用一次性脚手架 `.tmp/run_tests.py`（不进库）绕开沙箱对 `mkdtemp`（mode 0o700）
  的只读限制，未改任何项目文件；CI（ubuntu）无此限制。

## H1 剩余截止落到实际请求（已交，提交 `5c2e158`，证据 `docs/evidence/h1_deadline_propagation_20260927.md`）

- **关闭 C0-2 §4 三个留存缺口**：Bing 固定 `timeout=12`（轻量链）/`timeout=15`（worker 链）
  与 ddgs `max(3.0, …)` 下限全部改为 **`provider_timeout()` = min(提供方上限, 剩余时间)**，
  不再做任何下限抬升；默认仍是 **6 次/60 秒**，未放宽。
- **受控的可终止边界**：Bing 走新 `read_with_deadline()`——按块读取、**块间**查同一墙钟截止
  （socket timeout 只管单次操作，慢速分块响应能远超它），到点抛 `TimeoutError` 停止读取；
  ddgs 是同步 SDK、不可取消，因此给它抬高**可行下限** `PROVIDER_MIN_WAIT["ddgs"]=1.0`。
  执行器在 `budget.take()` **之前**判下限：不够就**一个请求都不发**、也不消耗额度
  （`refused_calls` 记账并归 `timeout`）。一律不新起"超时后继续出网"的后台线程。
- **离线反例 4 条**（均在 CI 覆盖文件 `test_search_quality_unified.py`）：
  超时不越界 / 慢读取在块间被切断 / 低于可行下限零请求零消耗 / 截止后零新请求。
- **验证**：`test_search_quality_unified` 63 例 → 62 OK（唯一 error 是沙箱拒写临时目录，
  与 H1 无关）；`test_p0 -k bing -k text_search -k ddg` 4 例 OK。替身签名等价调整 3 处。
- **未验**：真实出网路径的截止表现（本批全部离线替身，未发真实检索）；
  **健康 Redis 上的双进程并发预占**——本机 Redis 处于 MISCONF 拒写，私有 Redis 在沙箱内
  无法启动（msys2 `NtCreateDirectoryObject 0xC0000022`），按"环境不具备 → 未验"记录，
  不以顺序假对象替代。现有 Redis 上双进程实测到的是"预占失败 → 本次零额度"的 fail-closed 行为。
- **顺带发现（非本批范围，需运维决定）**：运行中的 Redis `dir` 被配成 msys2 风格路径
  `/portable/Redis-8.10.1-Windows-x64-msys2` → bgsave 恒失败 → `stop-writes-on-bgsave-error=yes`
  使**所有写命令被拒**（实测 `SET` 报 MISCONF）。影响 `LPUSH task_queue:*`、结果回传、
  状态落库与检索台账，即**当前实例无法接受新任务**。本批不重启服务、不改其配置，仅报告。

## H0 提交防护与 C2 测试门禁（已交，提交 `8a44f85`，证据 `docs/evidence/h0_commit_guard_20260927.md`）

- **配置备份忽略补口**：`.gitignore` 增加 `config.json.bak.*`。旧两条模式（`config.json.bak`、
  `config.json.*.bak`）都匹配不到 `config.json.bak.<tag>-<timestamp>` 形态，故 09-20 那个
  含密钥备份一直以 `??` 挂在未跟踪区。修后本机 **7 个** `config.json*.bak` 逐条 `git check-ignore`
  **全部忽略**；`git log --all -- 'config.json.bak*'` **无任何记录**（从未入库）→ 按指令
  **不轮换密钥**、不删除/移动任何备份、未读/未打印任何密钥值。
- **C2 测试进 CI**：`ci.yml` 纳入 `test_question_assessment.py` 步骤。核对口径：
  **已提交 CI 47 步 == 跟踪的 47 个 `test_*.py`**，双向差集为空（无指向不存在文件、无漏跑）。
  该步骤原为工作区一处**既有未提交改动**（来源未核实，不记为架构师改动），本批按 H0.2 精确接续。
- **暂存纪律**：先确认暂存区为空；只暂存 `.gitignore` 本批 hunk + `ci.yml` 该步骤 hunk。
  `.gitignore` 的 `dump.rdb`（**09-19 既有改动**）仍未暂存；`templates.json`、
  `evals/cases/auto_grown.json`、4 处 docs 脏改、`dist/`、`_trial_*`×6、`probe_batchA.py`、
  `trial_tasks.json`、`worker_base.py.log`、`agents.db` 一律未动。未用 `git add .`。
- **文档更正（H0.3）**：`c2_question_contract` §6 把该 `ci.yml` 改动写成"架构师未提交"→ 改为
  来源未核实的既有改动，并把 18 例更正为 20 例（本批实测 `Ran 20 tests OK`）；
  `n4_scenario_drills` §9 已过时的"指向不存在的 `test_question_assessment.py`"一并更正；
  `c1_material_entry` §2 "别人的未提交工作"改为"既有未提交工作，来源未核实"。
- **未验**：远端 CI 仍是 46 步（本批未推送）；未重跑全仓（用 `git check-ignore` 逐条与 CI
  文件集双向差集作证据）。清理日志/备份/数据库/临时物**不属本批**。

## C1 补材料入口接入编排（已交，证据 `docs/evidence/c1_material_entry_20260927.md`）

- **正常入口**：`POST /api/task/<id>/material`（+`GET …/materials`）与任务页「补材料」标签；
  鉴权沿用既有 admin 门并**新增任务归属校验**，运行中的任务拒收（不与抓取回灌抢写快照）。
  网页上传只接受请求体字节、存储路径由服务生成；本地调试读文件走 `local_file` 通道，两者在
  材料记录里分开记。
- **摄取服务** `material_intake.py`：内容寻址原件 + 幂等键（直链按 URL、文件按内容 hash）、
  类型/体积/页数体检（拒压缩包、拒二进制）、直链取件走既有内容通道、调同一准入服务
  `disclosure_ingest.ingest`、并入 `project/fetch_snapshot.json`（**不另建证据库**）。
- **身份可核对**：文档带 `material_id` / 原件 sha256 / 正文 sha256 / 解析版本 / 准入规则版本 /
  主体与期间依据 / 披露日的**依据** / 逐指标状态 / 已解析与未解析字符区间。
- **按问题取证据**：按指标关键词挑承载小节（不再只截前五节）；三种"没有"分开
  （未取得 / 已取得但未定位 / 在列明的材料范围内未披露）。
- **规则变更重验**：材料记录准入规则版本，版本对不上就重跑判据（用例断言重跑发生）。
- **只重做依赖环节**：补材料后重做证据→结构→底稿；交付正文逐字节不变，模型重生成只挂待办；
  身份没变时**不产生假待办**并写明原因。
- **局部恢复**：编排器启动扫 `material_pending_tasks`，补做"已保存未摄取"的材料（原件已在盘的
  不重复发请求）。
- **实机**（16/16 服务、新代码）：洋河任务 `ui-706c5ef4a5` 经真实编排器贯穿——准入 `user_file`、
  披露日依据 `source_field:published_at`、61 小节 8 条证据、快照 `e0e0ce71…` 与磁盘复算一致、
  定位仍是 `api_chunk`；同一材料重投 → `stale=false`/无待办。
- **未验**：已登录浏览器里的点选实测（本机 sessions 表为空、不伪造会话）；第二家公司
  三一重工 600031 真实原文（本轮一次受限检索候选 0 → 如实留待取得）。
- 顺带修一处**在库**缺陷（`test_delivery_chain.py` 借用测试实例从不跑清理 → 工作区根与任务库
  路径泄漏到后续文件）。另记两条非在库项：`test_prompt_system.py` 带 `assume-unchanged` 且含
  未提交测试类（本批不接管其提交）；`test_offline_delivery` 会因生产 `_apply_cfg_to_env()` +
  真机 config 把真实凭据留在测试进程环境（值不记录，建议加进程级还原夹具）。

## C0-3 统一出口覆盖（已交，证据 `docs/evidence/c0_3_egress_coverage_20260927.md`）

- **每个内容入口执行 mode**：新增 `net_policy.require_egress_ok()`，在 `get_via_urllib` /
  `get_via_socket` / `dual_channel_get` **发请求之前**调用——`proxy_required` 且不支持经代理出口
  时直接拒绝（反例"必须代理但没代理仍下载 1 次"关闭，urlopen 调用数 0）。
- **建连后断开不落直连**：`dual_channel_get` 判断"代理是否在生效"，在生效时**任何** urllib 失败
  （含 `RemoteDisconnected`）都抛 `ProxyEgressError`；代理不参与时第二通道照旧。
- **不为内容下载清理整进程代理**：删除 launcher 的 `apply_direct_mode_env()`，改为
  `proxy_settings_report()` **只报告**（requested mode / 代理是否生效 / 脱敏 host / 是否支持代理出口）。
- 保留公网校验、跳转校验、DNS 绑定与 TLS 验证不动；验证 `test_net_policy`+`test_p0`
  +`test_startup_readiness`+`test_delivery_chain`+`test_search_quality_unified` 共 911 例 OK。
- 未验：真实代理在线/离线真机抓取（本批为替身）；`direct` 模式下 `get_via_urllib` 仍走 urllib
  （逐请求改通道未做，理由与出路写在证据文档）。

## C0-2 根检索预算（已交，证据 `docs/evidence/c0_2_root_search_budget_20260927.md`）

- **首次请求前**原子预占次数并按根起点落时刻（`HINCRBY used` + `HSETNX started`）；剩余墙钟从
  首次预占起算，不再"首轮跑完才记"；预占失败/台账不可读 → **本次零额度**（旧行为是"读不到按默认
  额度走"，等于台账一坏就无限额度）；未用预占按实际发出数退回；bytes/str 键都认。
- **真实停止**：`take()` 去掉 1 秒下限（短预算如实传给 provider）；调用超预算返回 → 结果**不入库**、
  记 `overrun_calls/overrun_seconds`、无产出时状态 `timeout`（不再假 ok）。
- 默认 **6 次/60 秒不放宽**；验证 `test_search_quality_unified`+`test_p0`+`test_offline_delivery`
  +`test_delivery_chain` 共 855 例 OK。
- 未验：多**进程**真实并发（本批为同进程双 agent 共享假台账）；Bing 12/15 秒与 DDGS 3 秒下限的
  内部超时收窄留下一批；C0-3 出口覆盖未做。

## C0-1 统一资料准入（已交，证据 `docs/evidence/c0_1_admission_unification_20260927.md`）

- 关掉 §3.2 全部反例：`2025-00-99` 不再准入、`2025/04/29` 正常识别（真实日历 + 精度 + 依据）；
  URL 只认具名公告标识与日期形状路径段（`?asof=` 不再当证据），裸年份不再当发布年；
  正文主体判定取代标题判定（标题洋河/正文茅台 → `subject_mismatch`）；错误页（`Access denied`）
  拒收；权威域改"完整域 + 受控子域"（`cninfo-fake.org`/`eastmoney-fake.org`/`ir.evil.org` 不再命中）；
  准入带 `source_class`（官方披露/第三方镜像/用户文件），不因标题或用户直链升为官方。
- 验证：`test_search_quality_unified`+`test_narrative_evidence`+`test_delivery_chain`+`test_facts`
  +`test_working_paper`+`test_p0` 共 990 例 OK；被调整的断言与 fixture 逐条写在证据文档。
- 未做：C0-2 根检索预算、C0-3 出口覆盖；新旧两条链路主体判定窗口的彻底同源（留 C1 接线）。
## S3 上半：A 股原始披露摄取窄闭环（2026-09-27，证据 `docs/evidence/s3_disclosure_ingest_20260927.md`）

- **披露日纪律（专项 §3-8 必修）**：`facts.py` 不再用报告期末顶替公告日（此前
  `disclosure_date or disclosed_at or report_date`），缺失即留空；新增 `retrieved_at`（获取日）。
  底稿 A′4 的"时点未核实"闸因此真正生效——期末一般 ≤ 截至日，兜底会让闸静默放行。
- **权威域判定（专项 §3-10）**：`_domain_hits` 从整串子串匹配改为**只看 hostname** 的
  域/子域/标签边界匹配（`?origin=cninfo.com.cn`、`eastmoney.evil.org` 不再算权威）。
- **摄取层 `adapters/disclosure_ingest.py`（新）**：`discover()`（巨潮未连通即如实不可用 +
  正当出路）/ `pick_official_candidates()`（纯函数筛候选）/ `ingest()`（主体、期间、截止
  三道校验 + 证据定位 + 正文 hash；`provenance` 区分自动检索/人工直链/人工取得文件）。
- **验收**：洋河冻结样本（公告文本 API 真实取件）→ admitted + api_chunk 定位 + hash 绑定；
  错主体/错期/晚于截止/披露日未知或精度不足分别拒绝（后两者可作背景但不得当已核验时点证据）；
  点评类文章拒为 `not_official_source`；巨潮发现如实 `unavailable`。测试：新增
  `test_delivery_chain.TestDisclosureIngest` 9 例 + 权威域 18 例 + facts 2 例，共 1059 例全绿。
- **未闭合（照实）**：指令要的"第二家非金融公司真机取证"未完成——两次有界检索（项目自己的
  检索通道）均**零候选**，即发现链路本身没闭合；按"同类失败不再轮询"停手。两条正当出路：
  操作者给官方直链，或打通巨潮/经批准的替代官方列表源（`discover()` 已留分支）。
- **未做**：摄取层尚未接进编排器/抓取 worker 的在线路径；无上传端点（只支持直链/本地文件）；
  港股 HKEX 与美股 SEC 的同类路径仍属扩展边界，SEC 的 filed/accn 版本保留仍缺。

**当前批次**：搜索网络与金融资料获取专项（指令 `docs/搜索网络与金融资料获取专项_20260926.md`）。

## S2 上半：网络通道一致性（2026-09-27，证据 `docs/evidence/s2_network_egress_20260927.md`）

- **出口方式显式化**：`net_policy.connection_mode()` 三态 `inherit / direct / proxy_required`
  （`WM_CONTENT_FETCH_MODE` 或 `config.json network.content_fetch.mode`，非法值按 inherit 并记日志）；
  `proxy_settings()` 脱敏报告环境代理；`direct` 模式由 launcher 启动时清理本进程代理变量
  （只删代理变量、不改系统设置，子进程继承）。
- **代理失败不落直连**（专项 §6 核心）：`dual_channel_get` 遇到代理层失败改抛
  `ProxyEgressError`（类别 `proxy_error`），**socket 直连次数 0**；"配了代理且失败在连接
  阶段"经 `URLError.reason` 判定，"已建连后被重置"不算，避免误伤第二通道。
- **socket 通道改走已验 IP**：`net_policy.connect_validated` 连校验时解析出的那个 IP
  （消除"校验后改指内网"窗口），TLS 系统 CA + 正确 SNI，http/https 按 scheme 处理。
- **内容抓取出口如实标注**：`fetch_document` 结果带 `egress="direct_pinned"`；`proxy_required`
  模式下**拒绝**（本版本经代理出口明确不支持），不静默改道。
- **同一套诊断类别**：`net_policy.classify_network_error`；各源错误对象带 `.category`；
  PDF 下载与新闻 RSS 记类别（`news.last_error()`）；`search_diag.classify_error` 先判代理层。
- **验收**：代理失败直连 0（替身断言）、源站失败仍走第二通道、407/拒连判定不误伤、
  TLS 校验未关（源码守卫）、内网/跳转内网仍拒、公网正例仍通、回环工作台未受影响、
  `proxy_required` 拒绝而非改道。测试：`test_net_policy` 33 例（新增 11）、
  `test_p0`+`test_startup_readiness`+`test_deploy_manifest`+`test_delivery_chain` 834 例全绿。
- **未验**：全离线单测；代理在线/离线两种真机出口未验；包内复验待下次重建运行包。

## S2 下半：健康状态语义与快照实例身份（2026-09-27，证据 `docs/evidence/s2_health_states_20260927.md`）

- **状态四态**：`health_registry` 每条依赖新增 `state ∈ available/degraded/unavailable/unknown`
  （`ok` 降为兼容视图 = available|degraded），并带 `instance` / `checked_at` / `stale`。
- **假绿修掉**：`probe_search` / `probe_market_source` 此前"无快照 → ok=True"；现在无快照、
  快照过期（默认 300 秒，`WM_HEALTH_SNAPSHOT_TTL` 可调）、快照来自**别的实例**一律 `unknown`
  且非绿。部分源坏 = `degraded`（仍可用），全坏 = `unavailable`。
- **实例身份**：快照自带 `_instance`/`_checked_at`（两个发布点：worker 搜索健康、行情源健康）；
  `instance_id()` 取 `WM_INSTANCE_ID` 或"工作区根 + 端口"指纹，防运行包/源码实例互串。
- **页面**：`/api/config/requirements` 增加 `health_state`/`health_stale`/`health_checked_at`
  与 `unknown_health`；设置页徽章四态（未检查 / 读数过期 / 降级 / 异常），`frontend/dist` 同版重建。
- **验收**：缺快照/过期/异实例均非绿；一个可选源坏 = 降级而非全不可用；`unhealthy()` 收
  `unavailable+unknown`、`degraded` 单列（新增 `degraded_items()`/`unknown_items()`）。
  测试：`test_p0` 新增 4 例（含字段一致性），连同 `test_frontend_guards`/`test_settings_requirements`/
  `test_startup_readiness` 共 543 例全绿。
- **运行实例**：系统重启后已按 `deps --fix` → `start` 起回 16/16（便携 Redis v8、单实例），
  服务启动晚于 S2b 提交、服务端主包与本批产物同名 ⇒ 后端前端均为新代码；实测搜索/行情健康
  快照尚无（首次检索/用行情后才发布）⇒ 页面这两项应显示"未检查"而非绿色——正是本批语义。
  真机页面四态渲染需登录（sessions 表为空、admin 密码不在执行者手上，未伪造会话）。
- **未做/未验**：`web_ui.source_health` 旧字段与统一 `dependencies` 的同源收敛、S4 首屏
  "资料获取能力"视图未做；未在运行页面用真实快照看过四态（需重启服务）。
- **包内复验（2026-09-27，运行包 `2026.09.27.1`，zip sha256 `0443a425…8f74`）**：
  死代理（`127.0.0.1:9`）下 `dual_channel_get` 抛 `ProxyEgressError` 且 **socket 直连 0 次**；
  无代理时 Bing 抓取正例 ok（99.4KB）；`WM_CONTENT_FETCH_MODE=direct` 清理环境代理后仍可抓取
  （98.7KB）；包内健康四态与前端徽章文案同版就位；`search_diag` 有界探测（Bing 10 条/0.48s、
  ddgs `no_results`/8.09s、东财 1 条、公告查看页 0.38s，预算 4/6）证明 S1 检索路径未被 S2 打断。

## 闸门观察（照实记录，不改判据）

- **clean-env-e2e 偶发"任务收执超时"**：`4de058d`（纯文档提交）首次跑时该 job 在
  "阶段 task：60 秒内没收到任务收执（orchestrator 未消费队列）"失败；同一提交直接重跑即通过，
  且此前两个含代码的提交（S2a `0bb8de7`、S2b `4a3a990`）该 job 均通过。
  **结论更正（H3，按指令）**：原记录写成"判定为 runner 侧抖动，**不是本次改动引入的回归**"——
  这个结论超出了证据：同提交重跑变绿只能说明**失败是间歇性的**，既不能证明是 runner 的问题，
  也不能排除回归。现按事实改为：**"间歇性收执失败，根因未确定"**。
  定位手段不再是"反复重跑到绿"，而是 H3 落地的**持久化收执与提交时间线**：
  任务行上的 `submit_timeline_json` 会逐段记下 received / published / persisted / consumed /
  started（含时刻、实例、代码版本），可直接看出请求卡在"没收到""收到没落库""落库没消费"
  还是"消费没开始"。判据（60 秒收执窗口）不放宽。

## 已交批次：S0 + S1（含包内复验补口，2026-09-26/27）

- **S0（`search_diag.py`，新）**：离线事实 + 一次有界公开探测（≤6 次调用、≤60 秒、无重试、无模型），
  不新建请求点（全走项目既有通道）。两环境事实表：Bing 两侧可用（10 条/0.5s）；东财结构化两侧可用；
  **ddgs 单后端调用在包内 32 秒后 parse_error（ConnectError→google）、源码 2.9 秒返回 5 条**；
  cninfo 披露通道未启用；"已知公开披露文件"取回的是公告查看页 HTML 而非 PDF。
  依赖差异：`ddgs 9.14.4→9.16.0`、`bs4 源码有/包内缺`。证据 `docs/evidence/s0_worker_env_diag_20260926.md`。
- **S1 三入口收敛成一个执行器**（`adapters/search_runner.py`，新）：一个预算（`WM_SEARCH_MAX_CALLS=6`
  + `WM_SEARCH_DEADLINE_SECONDS=60` 共用一条截止线）、显式单后端（从不 auto 全扫）、
  (查询,后端) 去重、结果协议 `status/items/attempts/elapsed/reason/retryable/provider/backend`
  + 旧数组兼容层。worker 与轻量路径都改走它。
- **候选与步骤状态**：`url_health` 改五态（`reachable/not_found/inaccessible/unknown/policy_blocked`，
  请求走 `net_policy.fetch_document`）；编排器只剔 404/410、其余保留候选并记原因；
  过滤后为空不再保持 SUCCESS；**无候选 URL 不派发抓取**（抓取调用数 0）；交付侧只有 404/410 写"链接失效"。
- **S1 补口（包内实测驱动）**：① ddgs 后端可用性改问 `ddgs.engines.ENGINES` 注册表
  （无效后端名会让 ddgs 静默回落 auto 全引擎重扫），三处调用点统一，拿不到就不发请求；
  ② ddgs 的 `No results found.` 归 `no_results`（完成但零命中），不再记 parse_error 熔断；
  ③ **任务级检索预算**：Redis 台账按根任务累计次数与首次检索时刻，重试/重做派发只拿余额；
  ④ **补查重试真的换查询**：契约 `retry_queries()`（主体/期间/文档类型/截止不变，只换资料面）
  + 执行器跨轮 `exclude_keys` 去重 + worker `[重试检索查询]` 只打新批 + 编排器重试指令携带新查询。
- **包内复验读数**（运行包 `2026.09.26.13`，zip sha256 `22eabe9a…9423`）：注册表
  `brave/duckduckgo/google/grokipedia/mojeek/startpage/wikipedia/yahoo`（无 yandex），生产选中
  `brave`；`search_diag`：Bing 10 条/0.48s、ddgs `no_results`/8.05s、东财 1 条/0.28s、
  公告查看页 0.16s（仍非 PDF 直链），预算用 4/6 次。
- **未验（S0/S1 范围）**：S3（A 股披露发现闭环）未动；`_replan_step` 的"换实现"分支未改；
  任务级台账按根任务共享 6 次/60 秒（多检索步骤先到先得，放宽改两个环境变量），
  并行派发可能低估一轮；**未跑真机端到端**；干净机器与物理断网照旧未验。

## 归档批次（中国平安复跑、N4 场景复验、N0–N4、R1–R4 与更早）

### 中国平安式无 Docker 任务新人视角复跑（2026-09-26 晚）

用户授权的一次真实付费研究任务。结论：**成因已消失**（6 步计划不含 `code_execution`，任务 12 分钟
跑到终态，外部检索失败后自动重规划改走结构化财务数据），终态 FAILED 且报告自带免责声明。
复跑又修掉三个真问题：① 两个实例共用一台 Redis → 任务被另一个实例抢去执行（`orchestrator:main`
是 pub/sub 广播；已改为非本实例的 Redis 不复用，自起空闲端口；显式配置同一台时仍会串台，未修）；
② 模板写死的角色/规划/备用端点让首次任务评审直接降级（已把模板三项留空 + 占位符守卫）；
③ 配置缺失回退硬编码 stub 导致新手实例丢产品默认值（已改为回退随包模板 + 占位符不算已配置）。
证据：`docs/evidence/pingan_style_live_run_20260926.md`。

### N4 场景实机复验（2026-09-26 白天）

**批次**（归档）：用户点名的六项场景复验。交付物 `dist/weavemind-2026.09.26.4-win-x64.zip`
（sha256 `41429db2d1a79aa6b19d5528dd433ab6e248ab185d07df8a752c422c3be7ea89`）。
六场景结果：1/2 净化环境整链通过；4 断网冷启动通过；5 端口冲突与 Redis 5 通过（让位且不接管）；
7 错误密钥通过（2 次被拒请求零费用）；8 代码隔离提示三处可见。**本批修掉 10 个真实缺陷**
（运行包不用包内解释器、包根不在 `sys.path`、回环健康检查被代理接走、默认端口被占即报错退出、
配置未完成把新人丢在控制台、`isatty()` 对 NUL 也报 True、内置演示未随包、引导清掉用户已有高级配置、
含代码任务缺"执行前"提示、`text=True` 子进程输出按 UTF-8 解码抛栈）。
证据：`docs/evidence/n4_scenario_drills_20260926.md`。

### N0–N4（2026-09-25/26）：新人一键启动与首次研究体验

**批次**（归档）：新人使用专项 N（指令 `docs/新人一键启动与首次研究体验_20260925.md`）——
N0 纠偏 `6715543`、N1 统一启动 `e81dcf3`、N2 运行包 `e858f0f`、N3 首启引导 `c1ade62`、
N4 验收矩阵 `e761c1c`、CI 修复 `936d4e0`。九条场景逐条给出状态与证据；干净环境相关行如实标未验。

- 当时实测通过（本机）：③ 中文与空格路径；⑥ 连续双击复用实例；⑨ 修订/导出/重启保留账户与配置。
- 当时部分通过：② 包内自带 3.11.9 独立运行时；⑤ 自定义端口单一来源与就绪探测（行为用例）；
  ⑦ 演示与真实分开、错误在原页面纠正；⑧ 公司研究在无 Docker 机器上跑通。
- 当时未验：① 干净 Windows 整链；④ 真机断网演练。→ **本批已复验 1/2/4/5/7/8，见顶部。**
- 证据：`docs/evidence/n4_newcomer_acceptance_matrix_20260926.md`；专项总览
  `n0_n1_single_start_and_readiness_20260926.md`、`n1_startup_controller_20260926.md`、
  `n2_run_package_20260926.md`、`n3_first_run_guide_20260926.md`。

### R1（2026-09-23 晚间）：统一研究判断与证据连续性

**批次**（归档）：阶段 D 后半程 · **R1**（指令
`docs/阶段D系统收敛与研究效能提升_20260923.md`）。
新增 `question_assessment.py`：给定指标判定材料类别（分解覆盖/管理层归因[发行人说法]/
观察/背景/明确否认/未披露/不确定/未来），因果必须是短语或构式且**只算在它所在子句**，
否定/未披露/不确定/未来优先——复核三反例（"原因尚未披露"、"并非由…导致"、"逗号后的
真因果"）与同段异指标、错期间、单字不升级共 11 例。
问题区/风险区/头部状态**读同一份** `structure["question_assessments"]`：
只有 `decomposition` 算"回答完成"，管理层归因只算部分覆盖；实机候选把收入记为
`分解覆盖（部分）`、利润记为`仅读数`，头部状态由旧 "1/3 已支持" 改为
"未完成（3/3，其中部分覆盖 1 项）"。
材料按文档与连续片段处理：逐文档 `chunk_offsets_by_doc`/`chunk_gaps_by_doc`，
缺口是**硬边界**（跨缺口小节的标题不继承、缺口后为"续段"并标 `after_gap`），
跨缺口不再合成单条证据（实机跨缺口记录 0 条）；每条记录带 `segments` 可逐段回溯。
从现有缓存产出新候选 `aac5d6415bffa24a`（11,209 字符，只读验收 overall pass、溯源 92%），
旧版 `6f2c004d1b52` 保留未覆盖。
证据：`docs/evidence/r1_unified_assessment_20260923.md`。
未验：真实 Redis/并发导出（R2）、多文档实机、真人评分（R4）、页面采纳（R2 做）。
本轮无新增 HTTP、无付费模型、未改模型权限/模板/全局 0/0/0。

**R2（同日晚间）**：导出做成可靠的版本操作——受保护读取（交付正文须证明属于当前采纳版本：
版本库交付登记 或 生产渲染核对，两条都不成立即 fail closed + 409），发布前在**临时包**上
校验通过才原子上线（失败不产生新包、旧包保持），绑定新增 `logic_fingerprint`（判定逻辑
源码指纹）参与失效判定，长列表与代码块改为逐行换页（离线探针的 1010 字符越界已修）。
证据：`docs/evidence/r2_export_snapshot_integrity_20260923.md`。
R2 退出（实机正常页面）：修订采纳 `aac5d6415bff`（父 `6f2c004d1b52`，验收 pass）→ 重新导出 `deliverables_20260923_230558_3d22ce.zip`（17 成员逐哈希一致、`binding_verified=True`、跨缺口记录标 after_gap、包内 PDF 12 页 0 越界、MD 与任务库送达正文逐字节相同）。

**R3（09-24 凌晨）**：用已有资料形成有用的研究简报——经营维度事实上底稿
（`facts.facts_from_operating`；46 行含 28 行经营维度，带产品/渠道/地区维度与**原表行列定位**；
按**组**取行后四组各自与表内「营业收入合计」闭合——修正了此前"分地区未闭合"的误判；
缺行不当零）；首屏新增『关键判断与下一步』（观察/意义/依据/边界/下一步）与附录
『逐问题资料计划』（想确认/现有证据/缺什么/补什么/影响）；变化解释不再重复读数；
`candidate_quality` 消费逐问题评估与底稿（有据覆盖→矛盾→信息保留→未支持→重复→观察，
长度不参与）；材料缓存带 `snapshot_sha256`，旧缓存按当前资料重建。
采纳 `6228c7467bf1a7ee`（父 `aac5d641`，验收 pass、装配 verified），包
`deliverables_20260924_092225_2a9922.zip`（17 成员逐哈希一致、binding_verified、
PDF 14 页 0 越界）。新一轮此前未利用、现在有依据的观察：**销量下降而发行人库存增加**
（销售 -16.30%/生产 -8.40%/库存 +16.38%）、省外降幅大于省内、线上降幅小于批发经销；
明确不能推出渠道压货/终端需求/提价。
证据：`docs/evidence/r3_operating_facts_and_selection_20260924.md`。
未验：页面 UI 本轮未重复驱动（会话丢失，走的是按钮背后的同一套函数）；主文 14 页未达
2–4 页软目标；真实 Redis 并发与真人评分属 R4。

**R4（09-24 白天）**：固定样本回放与指标记录——冻结三份 R4 场景
（`evals/scenarios/r4_*.json`：第二家非金融公司伊利 600887.SH 资料较充分、缺 2024 现金流
且无正文的资料不足样本、招商银行 600036.SH 作为**研究对象**的适用性边界）+ 一个用户修订
样本（洋河任务，页面同路径采纳）；`scripts/r4_replay_fixed_samples.py` 逐任务核对并写
`docs/evidence/r4_fixed_replay_20260924.json`：**4/4 pass**（充分样本 verified、必需事实不缺、
矛盾 0、请求 0；不足样本 draft 但缺口逐条列出；银行样本不生成企业口径比率；修订样本
旧批准不迁移、同版导出、修订后 0 请求）。跨章节矛盾检测器改为保守判定（水平/变化分开、
期间进键、长短语优先归属、实物量与表格行跳过、百分点零头容差；9 场景 7 个为 0，
`margin_mix`/`all_decline` 剩余计数如实记录、只作候选比较用计数）。
真人评分交接：`docs/evidence/r4_scoring_handoff_20260924.md`（可打开的产物 + 6 步操作 +
已知边界；**不代填、无批准**）。证据：`docs/evidence/r4_fixed_samples_20260924.md`。
未验：真实 Redis 多进程/取消重启一致性（留待模拟供应商定向验证）、页面 UI 本轮未重复驱动、
`r4_bank_subject` 收入问覆盖为"无相关材料"（记为改进点）、主文 14 页未达 2–4 页软目标。

**页面按钮路径复核（09-24 晚间 · 登录后实机）**：补做 R2/R3/R4 一直挂着的那一项。
任务 `ui-706c5ef4a5`，全程走页面自身按钮：「修改正文并重验」（改 4 处措辞、不动数字）→
`新版本 b1ea5c59c7b0（父 6228c7467bf1）；验收 pass，交付已绑定该版本`；
「按当前版本重新导出」→ `deliverables_20260924_185945_3a152d.zip`（16 冻结成员逐哈希一致、
`binding_verified=True`、`research_body_sha256=b1ea5c59…`、包内 MD 与任务库送达正文逐字节相同、
PDF 14 页 0 越界、旧包 9 个全部保留）。**复核中查出并修掉两个页面级缺陷**：
① `/files` 白名单不认带十六进制后缀的包名 → 页面「下载交付包 zip」对**重新导出的包**恒 404
（修复后 200，字节与磁盘包一致）；② 下载 Markdown/PDF 会重写导出清单并盖新 `generated_at`，
旧的时间初筛据此把当前包误判陈旧（页面冒出"包可能不含最新修订"），现改为时间初筛 + 身份确认
（清单与包内清单的正文/报告版本一致即视为渲染）。两者各带用例（`test_financial_chain` 36 OK、
`test_delivery_chain` 343 OK）。证据：`docs/evidence/page_button_recheck_20260924.md`。
未验：真人评分（页面照实显示"人工复核：待复核"）、真实 Redis 多进程并发、主文 14 页未达软目标。

**新手安装受阻的指引补口（09-25）**：按实机截图（Python 3.13 机器上 `start.bat` 停在
`[4/6] Dependencies`：依赖 0/14、镜像报 `No matching distribution found for aiosqlite`、
默认源超时，便携 Redis 4 个源 326 秒全失败）修三处会让新手白跑的坑：
① 依赖失败报告只给一行错误——现在直接附 4 步排查（确认通道 → 换 `WM_PIP_INDEX_URL` 镜像
→ 分小批装、chromadb 单独装 → 离线 wheels），部署指南新增
「5.3 依赖装不上时的排查与离线安装」+ FAQ 9.6 + 环境变量表补齐 `WM_PIP_INDEX_URL`/
`WM_NO_AUTO_DOWNLOAD`/`WM_REDIS_*`/`SKIP_REDIS_CHECK`；
② `SKIP_REDIS_CHECK=1` 此前只有 launcher 预检认，照着指引设了仍被 `start.bat` 的闸门挡住
——依赖自检（`--fix` 与只报告两种模式）现在也认，且打印代价（消息总线不可用、worker 与队列
不会工作）；
③ 三处指引把 tporadowski/redis（Redis 5.x）列为可选方案，与"需 ≥6（RESP3/HELLO）"
自相矛盾，照做会"启动即崩、日志报 unknown command HELLO"——改为 Memurai / redis-windows
并写明不能用；便携包体积说明由"约 5MB"更正为实际约 14MB。
用例：`test_redis_acquisition` 新增 5 例（跳过开关 4 例 + 不再推荐 Redis 5 构建）、
`test_p0.TestPipMirrorPolicy` 新增 2 例（失败报告带可执行步骤、指引镜像与默认源不漂移）。

**无 Docker 也能跑完研究类任务（09-25）**：新手实机（Python 3.13、无 Docker）跑研究任务时，
计划里的图表步骤被规划成 `code_execution` → 默认要求容器隔离、隔离不可用即拒绝执行 →
代码交付守门判"无代码交付物" → 修复轮（修复步同样是 `code_execution`）→ 空转十几分钟。
先确认事实：**只有 `code_execution` 需要 Docker**，图表由 `charts_pipeline` /
`data_analyzer` 进程内渲染（开发机同样没有 docker，真实交付的 6 张图就是这么出的）。
改动：① 规划规则——图表必须用 `data_analyzer`、明确不许用 `code_execution` 画图，
`code_execution` 只用于目标明确要求代码的任务；② 代码交付守门改按**目标**判
（新增 `_goal_wants_code`），研究类目标不再被误判缺代码；③ 隔离不可用时**不进交付修复轮**，
推一条可操作提示（三条出路）；④ 沙箱状态前移到依赖自检报告与启动校验（中性标记、不阻塞启动）。
安全默认值未放宽（默认仍要求容器隔离、隔离不可用仍拒绝执行）。
证据：`docs/evidence/no_docker_research_path_20260925.md`。用例：`test_orchestrator_v2`
新增 5 例、`test_startup_readiness` 新增 3 例；`test_orchestrator_v2`+`test_startup_readiness`
+`test_setup_wizard` 118 OK、`test_delivery_chain` 343 OK、`test_p0` 405 OK。
未验：规划规则是提示词而非硬约束（仍可能产出代码步骤，由守门与修复轮跳过兜底）；
沙箱镜像自动构建未做（有 Docker 的机器仍需手动 build 一次）；"搜索无候选 URL → `web_fetch`
判失败 → 下游链式阻断"（实机非上市主体那一次）未处理。

**上一轮（保留）**：阶段 D · **09-23 实机复核四项**。
项1：风险章节与逐问支持**同一条判据**（`match_kind`）——只有 explanation 才写"解释已取得"，
行业/市场背景与读数如实写"未取得该指标变化的解释"，且背景**不计入**必答分子；
『变化解释』每条披露带"归属"行。实机：必答 1/3（收入按量价/结构支持，利润/现金为缺口）。
项2：导出快照**冻结逐成员字节**（正文/PDF/底稿/图表/审计稿/引用证据），打包不再重读磁盘，
清单按写入字节算 hash 并带 `frozen`/`drift`，自检同时对照快照 hash；页面下载 MD/PDF 与包内**同字节**
（`3d357590…` / `678a196e…`），包内 MD 与任务库送达正文逐字节相同。
项3：新增『量价与结构（发行人披露）』——白酒销售量 -16.30%、分产品/分地区/分销售模式构成、
推算吨价 202,592 元/吨（+3.93%，带算式），收入问题按**量价/结构**支持；长段原文压成节选、
原始构成表不再重复贴出。验收 pass、溯源率 92%。
项4：片段跳号 = 缺口——合并文本在跳号处插标记，跨缺口记录带 `crosses_gap`/`missing_chunks`，
定位写明"非连续原文"；包内引用证据 8 条按坐标取回一致，`text_sha256` 可复算。
顺带修复：`_mark_unsupported` 丢换行导致"## 结论"与正文粘连（结论小节认不出）——改为按字符区间替换。
正常页面链：修订采纳 `6f2c004d1b52`（身份 `9b9ad5bc`，父 `1989fa2ec508`）；页面重新导出 →
`deliverables_20260923_195700_d4afd2.zip` 17 成员、自检全一致；包内 PDF 11 页**0 字符越界**、
标题与关键句（边界/引用/免责声明）逐条可找到。
证据：`docs/evidence/batch4_evidence_snapshot_volume_gaps_20260923.md`。
未验项：真实 Redis 多进程、导出期间真实并发修订、人工研究员评分、5 页主文软目标。
本轮无新增 HTTP 抓取、无付费模型生成。

**当前批次（上一轮，保留）**：阶段 D · **09-23 日间交付真实性复验**（指令 `docs/阶段D交付真实性复验与执行_20260923.md`）。
第一批：逐问题支持关系（背景/读数/原因分开，利润不再被同段收入解释误算支持 → 必答 1/3）、
比率方向按 `(1+gN)/(1+gD)`（同增/同降/反向/等速/跨零各定向例）、新候选改走**生产装配入口**
（当前采纳正文 + 8 条已准入资料；参考来源含洋河股份:2024年年度报告）。
第二批：PDF 逐行换页（首版第 1 页有 141 字符落在页外 → 修后 0 越界）、重包绑定**一次快照**
（期间修订 → 409 不混版；唯一包名 + 原子发布）、`rules_fingerprint` 入绑定并参与失效判定、
包内补**最小引用证据**（8 条 + 片段坐标映射）。
正常页面链：采纳 `7ae1f710` → `1989fa2e`，研究状态 `stale=false`、必答 1/3；
最终包 `deliverables_20260923_134459_289ccf.zip` 14 成员、自检全部一致。
证据：`docs/evidence/batch1_truthfulness_20260923.md`、`batch2_export_integrity_20260923.md`。

**推送状态（更正）**：`279dce4` 已于本日推送成功，`HEAD == origin/main`，CI run 35804433184 成功；
此前"未推送"的记录已过时，本轮不重复推送同一提交。

**当前批次（上一轮，保留）**：阶段 D · **09-23 正常交付闭环三小批**（指令 `docs/阶段D正常交付闭环执行指令_20260923.md`）。
第一批 `f7ba751`：财务语义三反例关闭（费用率空标签/无值两期/分母更快）、删 fetch_snapshot 准入捷径、
api_chunk 诚实定位（接口片段不冒认 PDF 页码）、账本锁恢复采纳身份。
第二批 `d0ae8ef`：契约同源写端、结构投影带来源正文/资料指纹/规则版本且失效原因逐项区分、
已保存公告文本经正常准入链进任务（8 条定位、收入解释只支持收入）、按当前版本重新导出新 ZIP
（清单 schema 2 区分采纳身份与文件 hash；页面新增按钮）。
第三批：主文收敛候选（15,701→8,334 字符，验收 pass）、负号/标点/标题去重/PDF 抬头渲染修复、
模型稿按内容 hash 版本化留档并入包；正常页面链复验：采纳 `1a3477c5`、`stale=false`、
新包 12 成员自检一致。
证据：`docs/evidence/batch1_verification_gaps_20260923.md`、`batch2_state_and_package_20260923.md`、
`batch3_readable_brief_20260923.md`。
**未追加付费实机、未换模型、未改全局 0/0/0、未改 templates/权限；推送按指令未重试（本地提交在案）。**

| 层 | 状态 |
|---|---|
| 第一批 | 三反例改前全误通过→改后按"值+算式+输入"与方向一致性裁决；未准入抓取文本不再支持归因；定位写 `api_chunk K（字符 a-b）`；锁 False→True 采纳同伴身份、旧 writers 保留 |
| 第二批 | 绑定完整（契约/资料/规则指纹非空）→ 修订后 `stale=false`；资料准入链 8 条定位、`missing_kinds=[]`、利润/现金仍缺口；重导出包 schema 2、页面"包内字节自检 全部一致"、旧包保留 |
| 第三批 | 主文 8,454 字符（原 36,917）、PDF 9 页（原 15 页）；任务指令/bank_corporate/chart_id 退出主文；"见 6.3 表"类失效引用清除；标题印两遍、负号断行、句号甩行首修复 |
| 未验项（如实） | ①主文 5 页未达 2–4 页目标（已去重、底稿与次要图入附录）②真实 Redis 多进程、公开原文获取正向全链、真人评分仍未验 ③visual-judge 子代理账号不可用，未做第二人视觉复核 ④并发修订混版未实测 ⑤推送未重试（三次失败后按指令停） |
| 历史批次 | 见下（09-22 晚间与 09-22 午后） |

## 历史（按日期，保留当时结论）

### 2026-09-22（晚间）：收口复验 + 浏览器正常入口测试

### 2026-09-22（午后）：阶段 D · A→B→C→D 四小批已实现并离线验证（指令
`docs/阶段D实机复核与下一批指令_20260922.md`，审查基线 `7ec2003`）。
A 批（验收两处豁免收窄 + 一请求一票 + 415 usage 归属 + 共享写失败 fail closed + 口径标注）、
B 批（执行契约穿过 Critic 修订到实际派发 + 已存真实材料整链闭合）、
C 批（归因/算术分开核验 + 图表 chart_id 绑定 + 就绪按契约必答问题逐项裁决 + 状态绑定采纳正文）、
D 批（候选修订保留历史 + 同版导出清单/PDF + 逐页目视 + 旧包陈旧）。
**未追加付费实机、未换模型、未改全局 0/0/0、未改 templates/权限。**

| 层 | 状态 |
|---|---|
| A-1 验收豁免收窄 | `_extract_source_claims` 豁免收窄到**声明片段本身**（"数据来源：X，需核查…"仍检查 X）；同值提升必须**同一事实身份**（`_promotion_subject` + `_subjects_conflict`）。实测：来源为空 + 尾加"需核查"→ 仍 fail（检查 1 条）；来源只有宁德时代营收100亿元、正文另称比亚迪/腾讯各100亿元 → **fail，溯源 33%（1/3）**；真"需核查……才能判断"句仍 pass |
| A-2 一请求一票 | `llm_client`：只有**真的流式→非流式回退**才另开票（`self._fell_back`）。实测：普通非流式 1 次发送 / 1 票 / 用量 5 一次 / 1 条记录；cap=1 首次放行、第二次发送前拒；415 回退 2 发送 2 票、成功 usage 只记一次、415 那次 `unknown_calls=1`、形状 2 条 |
| A-3 共享写失败 fail closed | `_save` 返回是否可信落盘；拿不到锁**不写**（不做无锁覆盖）；`persist_uncertain` 是账本级事实（落盘 + 断点继承）；有界多进程在"共享不可用 / 已标记不确定 / 预留未成功返回"三种情形**拒绝新付费请求**，并撤销该次预留（本地+共享计数回滚）。不配上限的任务照常（保留可用报告与只读工作台） |
| A-4 口径标注 | `BudgetLimits` 文档 + 快照新增 `count_basis`（`max_calls`=所有预留票据；`provider_requests`=llm+backup；`steps_dispatched`=step）与 `token_basis`（上界/实际/未知独立）。**未放宽任何上限** |
| B 执行契约 | 新增 `execution_contract.py`：主体/代码/期间/as_of/口径/文档类型/必需指标 → 指纹、按年短查询、期间冲突判定、幂等重建、不变量校验、历史提示"不适用"标注。修订稿先按契约重建**再复评**（PASS 绑在重建后的这一版）；派发前复核指纹，不一致则重建、重建失败/仍冲突则**拒绝派发**；SearchAgent 查询来源优先级 = 派发契约字段 > 指令 `[检索查询]` 行 > 整段指令，并剔除含契约外期间的变体 |
| B 原始字节复用 | `web_fetch_worker` 把 PDF 原始字节落任务工作区工件（`project/fetched/<sha16>.pdf` + sha256）；`_try_pdf_evidence` 解析**消费同一份字节**（校验 hash、路径必须落在任务工作区内），不再按 URL 重抓；工件缺失/校验不符按缺口处理 |
| C-1 归因与算术 | 新增 `check_attribution_support`（缺证归因不得因后接边界句豁免；明确假设/待查不扣分）与 `check_ratio_arithmetic`（绝对额减幅不能替代比率复算；给出两期比率或底稿派生关系则通过）。实机两处反例（§3.3 固定费用归因、§5.2 负债率算术）均被判 fail |
| C-2 图表绑定 | 规格带稳定 `chart_id` + 绑定块（指标/单位/期间/口径），渲染脚本写入清单，简报图注显示 `图 N［chart_id］…（绑定：…）`；新增 `check_chart_references`（图文错配只认正向证据）；同比全为负时措辞改为"降幅最小/降幅最大"；顺带修 chart_id 下划线被 Markdown 吞掉的渲染缺陷 |
| C-3/4/5 就绪与绑定 | `research_state` 重写：必答问题按契约（默认收入/利润/现金）**逐项**裁决；银行材料只作可选问题独列（不再出现 1/4 凑数）；肯定结论处于未证实状态即不就绪（语义角色判定，不豁免 boundary 字符串）；状态带 `binding`（采纳身份/正文 sha/结构版本/契约指纹与原文），结构不属于当前正文 → **待重验**；`export_manifest.json` 文件本体新增 `research_state` |
| D 同版交付 | 实机工作区离线候选修订（保留历史版本）：两处金融反例 + §2.4 同类绝对额解释 → 修订后**验收 pass / 交付 verified**（修订前 fail/draft）；图表按生产通道重算重渲染（6 张带 chart_id）；走 `/report.pdf` 同一路径实际导出，清单 `files.pdf.sha256` 与字节一致（`ea9321d2…`），清单与状态**同一 report_version_id**；15 页逐页目视（bundled `pypdfium2`）——图 1–3 清晰、图注与图一致、表列对齐；记录三处未修版面小缺陷 |
| 验证（本批） | `test_root_budget` 72（新增一请求一票 3 + 失败拒发 4 + 口径断言）；`test_delivery_chain` **315**（新增契约 5 + 已存材料链 3 + 归因/图表 4 + 就绪与绑定 3 + 同版链 2）；`test_acceptance_adversarial` 23（新增冻结反例 3）；`test_offline_delivery` 34；`test_r0_boundaries` 47；`test_report_quality` 32；`test_financial_chain` 34 全绿 |
| 未验项（如实） | ①真实 Redis 多进程未验（本批用内存假后端 + 无 Redis 端口）②公开原文获取未实测（离线批）③PDF 逐页目视由执行者完成——本机 visual-judge 子代理不可用，未做第二人复核 ④候选修订是离线产出，服务装载的仍是修订前代码 ⑤版面三处小缺陷已记录未修 ⑥研究状态仍 `research_draft`（必答三问 0/3 有定位依据）——本次确实没有取得原始年报，是如实结论 |

### 2026-09-18：C 批（固定研究路径）与 CI 修复

**C 已完成**
- 研究任务走**代码内固定步骤**（契约 → 搜索/抓取 → 解释已选事实 → 出报告）：命中条件是
  表单落库契约 + `facts.research_shaped`（有主体、≥2 期间、已声明口径）；先于关键词匹配与
  那次无预算的 LLM 路由调用。闸门一条不绕（取消/评审/计划确认/根预算/准入/研究硬门槛）。
- 结构化预载前移到规划之前；失败分支也落可复核底稿；报告步骤注入"已选事实"块。
- `route_structured_for_request`：契约有市场 + 稳定代码就直接抓（跳过 resolve 公司名）；
  适配器只收**本地代码**（`600519.SH` → `600519`，实机预探针发现的缺陷）。
- 脱敏调用形状 `llm_calls`：阶段/次数/耗时/输入长度/输出上限/HTTP 或错误类别/结束原因，
  收尾落 `llm_calls.jsonl`；规划失败与重试的进度消息不再写异常原文。

**已验证范围（三类证据分开）**
- **离线矩阵**：`TestResearchFixedPathOffline` 11 项（规划超时/坏 JSON 仍进受控路径、
  素材缺失绝不判已验证、正文失败仍留可重算底稿、三种时点取消、根预算耗尽、诊断脱敏）。
- **全量回归**：CI backend 的全部 **45 个测试文件本地逐个跑通**（与 CI 同一命令，最终提交
  `c451491` 上重跑确认）。
- **CI**：`c451491` 四个 job 全部 success（backend / frontend / docker-image / clean-env-e2e）。
- **实机（一轮，未达终态）**：固定路径与契约抓取在真实服务上生效（执行步骤正是固定四步，
  规划器未被咨询；`financials.json.contract` 与东财两年数据齐全；11 次调用形状脱敏可读）。
  未达成"合格报告"：① 抓到的 18 条事实口径全 unknown（**没有适配器声明报表口径**，而 A′
  要求口径只认来源声明的）→ 6 项必需事实缺口 → draft；② LLM 端点持续 504/读超时，反思
  不可用 → 强制重做循环 → 超过预声明的 25 分钟墙钟上限，按纪律请求停止（11.6s 生效）；
  ③ 模型正文缺来源清单/免责声明且有 8 处占位 → 验收 fail。四面同版、一次修订、导出四件套
  **因未达终态未验证**。

**阻塞与下一步**
- **口径证据链已修（`docs/evidence/caliber_evidence_chain_20260918.md`）**：查清可达来源里
  **不存在**机器可读的口径字段（东财主指标 165 字段无、F10 忽略 type 参数、cninfo 未打通），
  因此改为让**适配器按来源自身的结构**声明口径并要求附依据：A 股行含 `PARENTNETPROFIT`、
  港股含 `HOLDER_PROFIT`（只在合并报表存在的科目）→ 声明合并；SEC 按 10-K 申报规则声明。
  缺该结构就不声明（fail closed），`_caliber_ok` 门槛一字未改。事实新增
  `caliber_source`/`caliber_evidence`，底稿行、读侧 detail 与报告的"已选事实"块都带口径与依据。
  顺带闭合 A′ 遗留：东财 `NOTICE_DATE` → 逐行披露日期（截至日证据）。
  离线端到端已证：生产形状的研究任务跑到 `status=verified`（6/6 必需事实 + 3 条同比），
  不声明口径的对偶仍判 draft。
- 仍待验证：修复后的**实机**验收（不重复跑同一条实机请求）；港股无公告日期（截至日证据偏弱）、
  美股口径依据是申报规则（不可由数据自证）、母公司口径在这些来源上取不到（会如实报缺口）。
- 环境：LLM 端点 `tokenrhythm.studio` 不稳定（504/读超时）；嵌入能力不可用（HTTP 400
  MODEL_CAPABILITY_NOT_SUPPORTED，已按降级处理）。
- 未做：前端修订入口（界面留到 D）、港股/美股契约抓取实机、D（试用与扩展决策）。

## 历史（按日期，保留当时结论）

### 2026-09-17：B 批（PDF 排版）与 CI 收尾

**B 批完成，并从 CI 日志里挖出两个 Linux 渲染真缺陷**

已完成（都有断言）：页脚「第 N 页 / 共 M 页」、长表格**跨页重画表头**、缺图**可见占位**、
封面标题与正文同名标题**去重**、三处画线几何（此前多一个操作数把线画成斜线）、
表头文字基线落在色块之内。

从 CI 日志挖出的两个真缺陷（都属于"只有真渲染才看得见"）：

1. **cmap 只保留一个子表** → Linux 字体（DroidSansFallbackFull.ttf）的 ASCII 与 CJK
   分处不同子表，取到的那份不含 ASCII → `glyph_id("1") == 0` → PDF 里「第 1 页」
   印成「第  页」、英文文件名整段消失（抽取文本是一串 `\x00`）。
   已改为合并全部 format 0/4/6/12 子表（按 (3,10) > (3,1) > (0,*) > 其它 覆盖）。
2. **字体缺字形时直接留白**。合并子表后 CI 仍然缺 ASCII（该字体的 Latin 不在我们
   解析得到的子表里），于是改成**让排版对缺字形免疫**：缺字形且落在单字节范围的字符
   改用 PDF 内置 Helvetica（/F2，base-14，无需嵌入）绘制，宽度口径同步；
   `_text_run` 切段 + 只在真用到时登记 /F2。
   测试用"把字体对 ASCII 的映射强制成 0"复现该环境，断言数字仍可见 —— 断言的是
   **"文档能渲染"**，而不是"字体恰好覆盖全"（后者取决于部署环境装了什么字体）。

- 顺带：排版断言改为**空白不敏感**——按字形切段绘制后，抽取工具会在两段接缝处多插
  空格（CI 抽出 `第 1 页 /  共 3 页`），空格数量是实现细节而非被测内容。
- **未验证**：逐页视觉验收（需要栅格化渲染器；本机 pypdfium2 无 3.14 wheel、
  无 poppler/mutool/ghostscript/ImageMagick）。当前只有几何/结构/文本断言。
- **未验证**：`b280cea`/`e7697a7` 的 CI 结果（本机代理当时不可达；`ec4f942` 之后
  的每次提交都只做本地全量复跑）。

**CI 从"从未观察过绿灯"到全绿：这轮修掉的七个真问题**

| # | 问题 | 性质 |
|---|---|---|
| 1 | `ci.yml` 步骤名里未加引号的冒号（`(R1: bound PASS…)`）→ 整份 YAML 解析失败 | **门禁自身坏了**：GitHub 仍创建 run，但**零作业**、结论 failure，本地全绿看不出来 |
| 2 | `launcher.py start` 拉起子进程后自己返回 → 容器 PID 1 退出、服务被收走 | 部署真缺陷：`docker compose up` 起来即退 |
| 3 | redis 无就绪探针 + `depends_on` 只保证启动顺序 → app 反复重启 | 部署真缺陷 |
| 4 | `web_ui` 默认 `BIND_HOST=127.0.0.1` → 容器只监听自己，`ports: 8080:8080` 空转 | 部署真缺陷（宿主机 readiness 探活 200s 全失败） |
| 5 | `test_windows_prefers_system_binary` 在 Linux 上必然失败（两端分支不同） | 用例机依赖（已按平台分叉 + 补 POSIX 用例） |
| 6 | `live_probe_embedding_classifies_quota` / `test_checkpointer` / `test_task_time_optimization` 依赖本机 config.json（CI 无配置、本机改用了真实 key 的余额） | 用例机依赖：CI 报"未配置"、本机报"余额不足"——都不是被测行为 |
| 7 | e2e 脚本 `_status` 查 `task_history` 时表未建好即抛异常 → 门禁一次轮询失败就整轮崩 | 门禁脆弱：同一份代码上一次绿、这一次红 |

- 另外补了守卫：`ci.yml` 必须能被 YAML 解析、每步恰有 `run`/`uses` 之一、名里不得有未加引号的 `: `；
  compose 的"容器必须常驻 + redis 就绪探针 + `BIND_HOST=0.0.0.0`"不变量。
- `test_task_time_optimization::test_convergence_continues_when_best_report_improves` 的状态断言
  在 R1/R2 **之前就是红的**（用 `d0e5a72` 的 worktree 复核过）：该用例全程打桩、没有版本绑定的
  验收，按"以最终验收判定"就该记 `SUCCESS_WITH_ISSUES`；已按可达且正确的期望改写并注明原因。
- **仍如实记两件事**：① `backend` 是 fail-fast，当前全绿说明所有 44 步都过了，但一旦早步骤失败，
  后面会显示 skipped（本轮就是这样才发现不了后面 37 步的问题）；② CI 日志/annotation 需要登录
  才能读（API 403），本轮起 `gh` 已登录（`momang85`，scope 含 `repo`）但**推送/读日志要记得给代理**
  （`HTTPS_PROXY=http://127.0.0.1:7897`，gh 不读 git 的 `http.proxy`）。

**B 批（PDF 排版）：线几何已修，其余项与视觉门禁未做**

- **三处画线全是同一个真缺陷**：`report_pdf.py` 的画线指令写成 `x y W 0 m x2 y l`，
  而 `m`/`l` 各自只吃两个操作数——PDF 取**最后两个**，于是线被画成从 `(W, 0)` 到
  `(x2, y)` 的斜线：位置、长度、方向全错，却仍然"生成成功"（所以只看"没报错"发现不了）。
  已修：分隔线（正文 `hr`）、表格行线、标题下划线三处，全部改成 `x y m x2 y l`。
- **加了几何守卫**（`test_p0.TestReportPdfLineGeometry`，3 项）：解 PDF 内容流按算子取
  紧前操作数，断言 `m`/`l` 恰好两个操作数、线必须水平、跨度只能是正文宽或标题下划线的
  80pt、端点落在可打印区内。**已验证这个守卫能抓住原缺陷**（把旧写法改回去 → 测试报
  `4 != 2`，输出里能看到 `50.00 668.25 495.28 0 m ...` 那行）。
- **B 批其余项未做**（如实记账）：长表格跨页表头重复、图片缩放/失败占位、页脚页码、
  封面简洁任务名去重。
- **视觉门禁在本机不可用**：逐页出 PNG 需要渲染器，而本机 `pypdfium2`（Python 3.14 无
  wheel）、`fitz`/`pdf2image`（无 poppler）、`mutool`、`ghostscript`、ImageMagick 全部缺失
  （Windows 自带的 `convert.exe` 是文件系统工具，不是 ImageMagick）。装上可用渲染器之前，
  这一批只能做几何/结构断言，**不能宣称"逐页视觉验收通过"**。

**检索质量统一：已落地（两条路径收敛为一个实现 + 策略可配置）**

- **唯一实现**：`adapters/search_quality` 成为检索质量的唯一事实来源；
  `worker_base.SearchAgent` 的 `_clean_search_text` / `_extract_keywords` / `_query_variants` /
  `_filter_results` / `_is_garbage_result` 全部委派过去。此前两条路径各有一套近似的词表、
  计分与变体逻辑（一条有长连读要求与权威域加权、另一条没有），同一份结果在一处被过滤、
  在另一处被保留；已部署策略的 blocks/boosts 现在作为参数进入同一实现。
- **策略可配置**：新增 `SearchQualityPolicy`（`system.search_quality` 段 + 环境变量
  `WM_SEARCH_MIN_SCORE` / `WM_SEARCH_REQUIRE_HIT_RUN` / `WM_SEARCH_MAX_VARIANTS`）：
  min_score、长连读要求、变体上限、权威域四档权重、停用词、垃圾域名/路径/标题、
  机构定向白名单、以及 **ddgs 引擎清单**（原先写死在 worker_base）。默认值 = 迁移前行为，
  `config.example.json` 已附完整示例段。
- **迁移中发现并修掉两类假阴性**（都会让合法结果被整批误杀）：
  1. 长连读要求对**纯英文结果**也生效：查询里混着中文指令碎片（"搜索GitHub上完整的…
     开源项目"）时，合法的英文结果全军覆没。现在该规则只作用于含中文的结果；
     英文结果由英文词边界计分与 min_score 判定。
  2. 调用方**显式放宽** min_score（"严格过滤为空 → 放宽再试"的兜底路径）时不再套硬规则，
     否则"放宽"等于无效。
- **顺带修（实机复现的真缺陷）**：预算账本的跨进程计数原先每次读取都做一次 Redis 往返，
  Redis 不可用时每次付连接超时——看门狗的一次告警被拖到断言之后才发出（告警形同无效）。
  现在：**观测读取不为探测付代价**（只有写路径建立后端连接）、后端失败**按后实例永久降级**
  并有进程级健康缓存、账本客户端用 0.3s 连接 / 0.5s 读（消息总线的 5s 长连接超时另算）。
- **顺带修**：共享计数的键空间原先只按 `root_task_id`，同一 id 重新提交（重跑/测试复用）
  会继承上一轮留在 Redis 的花费，第一次预留即被拒。现在按**账本身份**
  （`ledger_id`，随账本文件持久化）隔离：同一次运行（含断点恢复）共用计数，
  重新开始的一次运行不继承上一轮。
- 回归：新增 `test_search_quality_unified.py`（17 项：两条路径同源、默认值不变、
  配置/环境变量生效、英文结果不被误杀、显式放宽生效）已接入 CI；
  `test_root_budget` 36→37、`test_offline_delivery` 8（并修掉 `TestOfflineFaultInjection`
  的**用例间端点健康缓存泄漏**——单跑通过、整类跑失败的假失败）；
  `test_p0` 391（含新增的 3 项 PDF 几何守卫）、`test_orchestrator_v2` 68、
  `test_delivery_chain` 184、`test_prompt_system` 39 全绿。
- **环境事实（如实记录）**：本批回归期间本机 Redis 曾停掉，套件耗时从 34s 涨到 209s 并出现
  8 处超时型假失败；用项目自己的 `dep_check.ensure_redis` 起回便携 Redis（pid 4604，
  绑定 127.0.0.1）后恢复（34.8s / 68 项全绿）。这些失败**与本批改动无关**，
  但说明这套测试对机器负载敏感，不能只看一次红灯就下结论。

**R2 请求级预算与取消：已落地**

- **票据状态互斥迁移**（去掉假平衡）：一张票据只能从 `open` 走到 `settled` **或**
  `unsettled` 之一，且只走一次。此前 `_call_llm_cancellable` 取消时写的是**凭空造的**
  `"{phase}-inflight"` 票据（账面上"取消已入账"），而真正预留的那张仍挂在 open 上，
  随后又被调用方的 `finally` 无条件 `settle(ok=True)`——同一张调用一次 unsettled、
  一次 settled，真实状态再也读不出来。现在：取消只动**真票据**（`mark_unsettled(ticket)`
  或 `mark_stage_unsettled(stage)` 只迁移当前 open 的），虚构票据一律拒绝并计入
  `rejected_transitions`；票据的生命周期收进 `_budget_scope` 上下文管理器（正常返回
  → settle ok；`TaskCancelled(ticket_settled=True)` → 不再结算；其它异常 → settle
  ok=False）。
- **token 也按上界预留**：修掉反例——`max_tokens=100` 时两次 `reserve(tokens=80)` 都会获准
  （此前只在结算时才扣）。现在 `remaining()["tokens"]` 用"已承诺量 = open 上界 + 已结算实际
  + 待对账上界"，第二次预留直接被拒；退回（发送前失败）会把上界还回去。
- **跨进程原子预留**：计数与票据号走 Redis `INCRBY`（先加后校验、超了回退），文件只是快照。
  修掉反例——两个实例各自 `reserve` 都获准、各自返回同一票据号（`step-1`）。现在票据号带
  序号 + 实例标记，`remaining()`/`snapshot()` 读共享计数，`snapshot()` 另给本地口径
  （`calls.local_reserved`）并标 `cross_process`。Redis 不可用时降级为单进程账本（如实说明）。
- **取消不可复位**：取消请求同时写提示键（可清）与**不可复位令牌**（无 TTL，`NX` 只写一次），
  并且 `_cancel_requested` 还认**已落库的 CANCELLED 终态**——Redis 被清/重启也不会
  "取消被抹掉"。`_clear_cancel` 只清提示键，令牌与终态保留。
- **取消后零新请求**：`llm_client` 新增取消守卫（编排器在 `run()` 注册 `_cancel_requested`），
  在**每次尝试前**与**切备用前**（含"健康路由直接走备用"那条分支）复查，命中即抛
  `LLMCancelledError` → `_call_llm_cancellable` 转成 `TaskCancelled`（取消不是失败，
  不走重试/降级）。此前取消只在派发边界可见，客户端仍会把重试与主备各跑一遍。
- **新线程在创建边界传 context**：`_call_llm_cancellable` 用 `contextvars.copy_context()`
  把调用方上下文带进工作线程并在拷贝里 `set_task_context`（`threading.Thread` 默认不继承
  contextvars；之前线程内台账/守卫都拿不到 task_id）。
- **根 deadline 收口等待**：`_wait_step_result` 的等待上限取 `min(步骤超时, 根任务剩余秒数)`，
  避免"根预算早已用尽还在按 300s 空等"。
- **两个时限分开记录**：`_cancel_latency` 分别给出取消 UI 响应时延（`WM_CANCEL_UI_DEADLINE_SECONDS`，
  默认 30s，超出如实标记）与供应商在飞结算窗口（`WM_INFLIGHT_SETTLE_SECONDS`，默认 900s，
  进 `unsettled` 票据的 `reconcile_by`）；拿不到请求时刻就报"未知"，不编数字。
- **0 值语义修正**：`system.budget` 全 0 = **真正不限**，不再回落 `system.task_timeout`
  变成 600 秒硬截止（配置写 0 与账本记的上限从此一致）。
- **顺带修（实机复现的真缺陷）**：`_execute_steps` 的 pipeline 串行判定与"占名额"不在同一次
  持锁里——两个 worker 都可能读到 `in_flight==0` 各取一步，pipeline 模式实测并发变成 2。
  现在串行判定与 `in_flight += 1` 在同一次持锁内完成。
- 回归：`test_root_budget` 19→36（token 上界、跨进程双实例、票据互斥、根 deadline、两个时限）、
  `test_cancel_semantics` 36→46（不可复位令牌、客户端重试/切备复查、线程上下文），
  `test_p0` 388、`test_orchestrator_v2` 68、`test_delivery_chain` 184、`test_offline_delivery` 8 全绿。
- **未验证**：跨进程原子预留只在替身 Redis（`INCRBY` 语义）上验证，未做真实多进程并发压测；
  真实供应商的计费窗口未观测（`WM_INFLIGHT_SETTLE_SECONDS` 是策略值，不是实测值）。

**R1 评审与待复核隔离：已落地（4 项 + 审批 API 并发保护）**

- **交付状态按根任务**：`_delivery_draft_reason` / `_delivery_status` / `_delivery_hard_fail`
  三个实例标量 → `_delivery(task_id)`（按根任务存放）。此前同一实例并发跑两个任务时，
  A 收尾写入的草稿理由会把 B 已判定的 verified 翻成 false，B 的交付与准入随之误判。
- **异版 PASS 不复用（评审绑定计划版本）**：`_review_bind_pass` 除指纹外再记
  `plan_version`；新增 `review_scope_ok()`（PASS + 策略版本 + 身份模式 + **绑定版本 == 当前版本**），
  交付守卫与准入判定都改读它，不再读裸 `verdict == "PASS"`。
  - 为什么用版本号而不是只比指纹：Critic 评审发生在规划返回时，之后编排器还要做机械加工
    （依赖连线、包步骤补齐、目标/Skills 注入、结构化裁剪），执行的那份 `steps` 与评审时
    看到的对象**本来就不同**——按对象相等判定会让每一次真实运行都被判成"没有绑定的 PASS"。
    因此机械加工**不**改版本（同一版计划的物化），**实质改写**才 +1：反思追加/替换步骤、
    计划确认阶段用户改了计划。
  - `_plan_fingerprint` 同时改成规范投影（只取 step_id/capability/instruction/depends_on/round，
    缺字段与空字段等价、依赖排序无关），执行期回填的 `iteration/status/result` 不再改变指纹。
  - 后果如实说明：反思改写过计划的运行，旧 PASS 不再覆盖它 → 个人模式仍可作经验
    （admitted）但**不计入已验证成功**（verified=false，模板固化因此不触发）；
    银行口径下连经验池都不进。这是"异版 PASS 不复用"的应有语义，不是回归。
- **恢复不复用旧 PASS**：`_restore_review_state` 增加计划版本比较（策略版本/身份模式已有），
  版本不符 → 按"未完成评审"处置；**旧 checkpoint 没有 `plan_version` 字段 → 视为无法证明，
  一律不复用**（拦下而不是放行）。checkpoint 新增 `plan_version`（与 `review.plan_version`
  分开记：两者不同即表示落盘时计划已被改写）。
- **待复核内容的隔离闭环**：
  - `add_prompt_refinement(..., status=...)`：默认 **`pending_review`**（默认拒绝），
    调用方必须显式声明"本次运行已验证成功"才写 `active`；状态同时进 metadata 与文档正文。
  - `query_prompt_refinements`：向量路径与**字面兜底路径**都只返回 `status == "active"` 的条目；
    **缺 status 的历史记录一律按未生效处理**（它们写入时正是"反思产出直接生效"的年代，
    状态无从证明）。此前注册表按 M0-d 拦住了 pending_review，但 RAG 那条没有拦——
    同一次运行写下的改进仍然会被下一个任务检索并注入。
  - `count_pending_refinements()` + `memory_health.refinements_held`：隔离**可见**，
    不是静默丢弃；`set_prompt_refinement_status(ids, "active")` 是放行入口（只改状态，
    **不动**任何样本的核实标记——人工 approve ≠ 数据已核实）。
  - 反思写入方（`_record_reflection_refinement`）与自迭代写入方（`prompt_refinery`）都改成
    按准入结论传状态：注册表与 RAG 两条路径用**同一个**状态。
- **审批 API 并发保护**（`/api/evolution/approve`）：认领改为原子
  （`lrem(count=1)` 的返回值当凭据，拿不到 → 409），修掉"两个并发审批都读到同一条待审策略、
  都删成功、都去部署（同一策略被批准两次、后一次覆盖前一次的 rollout）"；另加版本保护——
  已部署的另一版策略不再被静默覆盖（需显式 `force=true`，冲突时返回 409 且**不**把待审策略
  认领掉，否则它会凭空消失）。**未做**：CSRF（后端同时接受 `Cookie: session=`，
  跨站表单可借会话 cookie 发起写操作）与 A5 管理界面——按计划留到 D 批。
- 回归：新增 `test_review_isolation.py`（10 项，含"生成→待审→下一任务检索/注入为零→放行后可用"
  链路）已接入 CI；`test_review_protocol` 24→28、`test_admission` 29→31（新增异版 PASS
  的本地/银行两条判定）、`test_memory_degradation` 30 项。

**MKT-P0-4 README 区间披露 + 定位改写：已落地（仅仓库内）**

- 删除单点最优值：`README.md` 原第 37 行"A 股活跃度日报数字可溯源率 **85%**"已替换为
  **8%–85% 区间表**，四类任务分列（A 股结构化链路 85% / 美股链路 8%、金额 0% /
  财务任务结构化未命中 46%、通用调研 0%），每行带独立实测来源链接，并明写
  **"能否拿到报告期结构化财务数据是 85% 与 8% 的分水岭，而不是模型能力差异"**。
- 定位改写：首屏加"**主打场景：可复核的上市公司研究工作台**……每个数字都要能回溯到底稿并重算，
  算不出来的必须显式标成'模型知识'"，并声明"披露完整区间而不是只报最好那一次"。
- 美股链路状态如实标注：根因已修 + 英文/代码主体识别 + 单位口径 + 子句级主体绑定，
  但**验证范围只到合成夹具（金额溯源 0.833）**，真实 `data.sec.gov` 抓取未跑通，
  故 8% 仍作为可引用实测值保留。
- `README.md`「已知限制」原写"不绑定数值-主体归属"（已过时）→ 改为**子句级已绑定 +
  文档级仍缺**（指向 `test_acceptance_adversarial.py::test_known_gap_number_subject_mismatch`），
  并补美股链路验证范围、结构化链路覆盖面两条。`README.en.md` 补同口径三条，两份不冲突。
- 首屏 `docs/hero-demo.gif` 引用**已存在**（README.md:17），无需新增素材。
- **未做（按你已定的边界）**：Show HN / 掘金 / 公众号发布、定价与询价——由你本人执行，
  我不登录外部平台。

**MKT-P0-1 干净环境端到端门禁：已落地并本机演练通过**

- `scripts/e2e_clean_check.py`（临时目录 clone → 依赖自检 → 起 stub → 起服务 →
  提交任务 → 等终态 → 断言产出报告；独立端口、结果写 `docs/evidence/e2e_clean_last.json`）
  ＋ `scripts/stub_llm.py`（OpenAI 兼容替身，不烧额度；凭据运行时随机占位；
  请求限回环白名单）。
- 本机演练 EXIT=0：commit `c1a7559`、deps 退出码 0、16/16 服务存活、任务 `ui-842ff25fe6`
  以 **SUCCESS** 结束、任务库 report 1648 字符；本机实例未受影响。
- 已进 CI：`clean-env-e2e` 作业（Redis service 容器 + 仓库内 stub + 独立端口 + 证据 artifact）。
- **未验证**：CI 作业尚未观察到绿灯；克隆源是本机 `.git`（证明"提交内容足够"），
  不等于远端 clone 的同一性；该演练不证明报告质量（stub 返回固定文本）。

**MKT-P0-3 美股链路：已接进 target（数字-主体硬绑定仍待做）**

- **真断点已修**：US 的允许清单/注入/`fetch_sec` 其实都在，断在**公司名提取只认中文**
  （`_looks_like_company` 要求 2-6 个汉字）——英文/代码目标（"分析 WBD 的财报"、"AAPL 营收"）
  提取不到主体 → `route_structured` 返回 None → 工作区从来没有 `financials.json`。
  现在英文名/美股代码都能认（`(?<![A-Za-z])` 边界，中文紧邻也成立），并补了
  `中文名（代码）` 锚点（"微软（MSFT）"）与请求语剥离（"请给微软"不再留下"给微软"）。
  同时修掉三类把噪声当公司名的提取（动词前缀 "Analyze Apple"、期间残片 "一期"、
  泛称 "美股科技"），中文既有行为**不变**（`test_financial_chain` 31 项全绿）。
- **单位诚实性**：`_collect_sources` 原来对所有市场把金额写成"亿元"；现在优先用源声明的
  `metadata.unit`（美股为"亿美元"），不再靠短单位兜底偶然命中。
- **固定夹具 + 判据**：新增 `test_us_chain.py`（9 项 EXIT=0）——合成 10-K 夹具（**非现实公司
  事实**，明确标注）走真实 `route_structured` → SEC 分支 → `financials.json` → 注入 →
  验收，实测 **金额溯源率 0.833（5/6）**、阈值仍 0.7、overall=pass；夹具里没有的数字不被算作可溯源。
- **数字-主体绑定：子句级已绑定，文档级仍是缺口（本批如实划分）**
  - 已绑定：数字**紧邻的子句**里写明了他司主体、且命中来源行主体不同 → 该数字不进可溯源
    （新用例 `test_clause_subject_conflict_is_rejected` 断言）；比较报告/多实体报告**不误伤**
    （收窄过程中修掉三类假阴性：拿任务目标当兜底主体、`_norm` 压掉换行导致行内主体取成"第一家公司"、
    "口径/材料"这类泛称被当公司；并给归属检查内部调用加了 `subject_check=False`，两套判断不再互相污染）。
  - 仍缺：报告只在**标题/文档级**声明主体、数字所在单元没有局部归属时主体不参与绑定
    （架构给的那条 1741 亿用例仍判可溯源，已在测试里**记录为已知缺口**并标明阈值未下调）。
    要补它需要在"文档级主体 + 多实体识别"上做设计，避免重新引入上面的假阴性。

**MKT-P0-2 便携 Redis 国内可用性：已落地（多源 + 预算 + 离线复用 + 中文指引）**

- `dep_check.py`：**系统已装 Redis 优先**（Windows 先探 PATH / 已注册服务 / 常见安装目录）→
  **离线复用**（`.weavemind/downloads/redis-windows.zip` 合法即用，不联网）→ **多源下载**
  （`WM_REDIS_ZIP_URL` → `WM_REDIS_MIRRORS` 前缀 → 内置 ghproxy/gh-proxy/ghfast → 官方 release），
  默认总预算 60s、每源 20s，到点即停；可选 `WM_REDIS_ZIP_SHA256` 强校验摘要；镜像主机已进下载白名单，
  但仍走 https + 重定向逐跳复校 + 体积上限。失败指引改为**可执行步骤**（系统安装/Docker/镜像/
  手动放包/固定摘要/换 host）。
- **实测（真实网络）**：用户镜像不可用时自动落到 `ghproxy.net`，**24.6s** 取到 zip
  （13,933,009 字节，sha256 `4e8f2f956ed92fea…`），且与更早一次从官方源获取的同一文件
  **逐字节一致**（两条独立路径互证）；预算纪律 ≤30s 达标。
- 回归：新增 `test_redis_acquisition` **14 项 EXIT=0**（源顺序/前缀语义/摘要拒绝后继续试/
  非 zip 拒绝/预算到点停/离线复用不联网/系统 Redis 优先/指引可执行/默认预算 60s），已接入 CI。
- 顺带修：`test_startup_readiness` 的全仓守卫抓到我这轮三个驱动脚本没关 Redis 内建重试
  （Redis 不可达会挂几十秒），已统一传 `_NO_REDIS_RETRY`。
- **未验证**：镜像可达性只在**本机网络**验证过一次；"无代理 60 秒就绪"这条判据在你的真实网络里
  需要再跑一次确认（清掉 `.weavemind/redis/portable` 后 `python launcher.py deps --fix` 即可复现）。

**R0b/R0c 进度（逐项给证据）**

- ✅ **R0.2 版本身份真实接线**：验收输出**全量** `report_sha256`（短 hash 单列 `report_sha256_short`，
  只作显示）；`bind_acceptance` 改为**按完整身份精确绑定**（短 hash / 来源指纹不符一律不绑，只更新
  目标条目）；`adopted()` 去掉"同正文兄弟条目借验收"的回退；`adopt()` 改为**身份优先**定位（修掉
  "同正文换来源时采纳写错条目"）；终态判定新增 `_acceptance_summary_for_status`——读**选中版本自身**
  验收并带 `version_bound`/`needs_reverify`，未绑定由 `derive_status` 统一按有缺口处理。
- ✅ **R0.3 统一"已验证交付"谓词**：`report_version.verified_delivery()` 成为唯一实现（身份/正文/交付
  一致 + 验收 **pass** + 属该版 + 硬约束 + **必需**评审；个人模式评审降级不算草稿，银行口径缺 PASS
  仍是草稿）；编排器收尾守卫、导出清单 `status/draft/draft_reason`、`/pdf` 与 `/report.md` 响应头
  共用它。清单写入失败**可见**（`manifest_write_error` + 日志）；导出路由不再把"导出失败"混成 404。
- ✅ **前端导出边界**：401/403 分别提示"需登录/无权限"且**不自动下载**；服务端不可用退本地缓存稿时
  文件名与提示写明"**本地未验证副本**"；打印页页脚始终标注版本或用"版本未知（本地未验证副本）"。
- 🐞 **顺带修掉一个真实缺陷**：`web_ui.py` 没有模块级 `logger`——此前四处导出/清单日志都会抛
  `NameError` 被吞（"清单写不进去也没有任何可见记录"），现已定义并实测（失败日志正常打印）。

**R0 进度（首批，逐项给证据）**

- ✅ **R0.4 计算反例**（`acceptance_checker.py`）：常量豁免改为**按操作数位置**判定
  （`1000/1-1` 的分母 1 不再被末尾 `-1` 连带豁免）；新增逐操作数语义联合判定
  （`_combo_semantics`：每个必需输入都要绑定指标/期间/币种，未绑定 → unknown 而非 ok）；
  材料解析新增币种，并修掉两处被它照出来的真实缺陷——指标取"±20 字窗口首个词"（会把
  `2025 年收入 1200 万元` 标成"毛利率"）、期间取"子句首个年份"（会把 2024 的数标成 2025），
  现在都改为**取数字之前最近**的词/年；顺带补上 `万美元/万港元` 的单位解析。
  回归：`test_r0_boundaries`（R0.4 部分 7 项）+ `test_report_quality` 26 + `test_acceptance_adversarial` 19 全绿。
- ✅ **R0.1 目标达成与诚实披露分开**：新增 `derive_requirements`（目标里要求数据吗？要来源吗？
  点名了哪些指标/期间？）与 `check_requirement_coverage`（四态 `executed / honest_disclosure /
  goal_met / evidence`），并把"目标未达成"做成**独立门槛**：缺必需数值或来源 → 不得 `overall=pass`；
  缺失但已显式披露 → `partial`；未披露 → `fail`；正文为空 → `unknown`；定性要求（"定性即可、
  不需要数字"）不被迫造数字。缺口现在直接给可操作文案。
  最小复现（架构给的）**已转绿**：要求数据与官方来源、正文全"未披露"、来源为空 → 由 `pass` 变为非 pass 并给出缺口。
- ⏳ **R0.2 版本身份真实接线**、**R0.3 已验证交付谓词**：未开始，见下节"实机缺口 ④/⑤"。

**四分类（按《修订》§5 要求）**

- **已实现**：版本/证据记录（`report_version.py`）、导出清单、根预算（`root_budget.py`）、
  评审协议（`review-policy/v1` + 根任务状态）、准入谓词（`admission.py`）、取消四分类与
  可取消的模型等待、阶段观测、按端点额度冷却；前端 ReportViewer/Console 的中文与门户化改动**在源码里**。
- **独立定向通过**（他人可在禁止真实连接下复现）：`test_offline_delivery.TestOfflineFullDelivery`
  3 项、增量验收 4 反例、`test_review_protocol` + `test_admission` 53 项；本机另跑
  `test_report_version` 17 / `test_root_budget` 19 / `test_cancel_semantics` 36 / `test_offline_delivery` 8
  均 EXIT=0（**未跑全套**，不以单测总数替代关卡）。
- **实机缺口**：① `ui-2554df5f9f` 是 SUCCESS_WITH_ISSUES，但两个目标收入数值都未取得、
  来源 URL=0、验收多处占位却 `overall=pass` → **不代表研究目标达成**；② 该任务账本
  `plan=2/step=5`、`tokens_settled=0` → worker 内部请求与费用**未对齐**；③ 该样例当时
  未生成 `export_manifest.json`；④ 配置 0（不限）实际回落 600s（任务①因此提前收尾）；
  ⑤ 取消时票据 `1/1/1` 是"真票据已结算 + 虚构 inflight 未结算"的**假平衡**；
  ⑥ 检索通道对茅台类查询返回无关结果（本轮两个任务失败的直接原因）。
- **未验证**：① **浏览器/UI 未验收**（`dist` 已重建为 `index-yUwXXuD1.js` 并被 8080 提供，
  但未在浏览器逐项验收）；② pending_review 是否经 RAG 泄漏**未被证明**——注册表侧已隔离，
  RAG 侧今天 0 条记录是因为嵌入欠费写入失败，不是隔离生效；③ 主/备同 host，"主备容灾"未做跨供应商验证。

**P0 联调结论（本轮，已记录改动）**

- LLM 检测：主/备探测 ok（1.9s）；三段模型真实最小调用均返回（1.2/2.1/2.2s）；JSON 规划路径可用。
  生效模型：plan/reflect/review → `qwen3.8-max`，exec/judge → `deepseek-flash`；
  `planner.model=qwen3.7-max` 对规划调用不生效（**只记录，不改模型权限**）。
- `system.budget` 已改 `{0,0,0}`（按你的选择）；`frontend/dist` 已重建（新指纹）。
- 三个任务：① 含来源研究 FAILED（验收 fail，数字溯源 0/48）；② 定性研究 FAILED（检索无关结果，0/6 步）；
  ③ 运行中取消 CANCELLED（**停止延迟 1.0s**，账本假平衡见上）。
- 任务①②的"失败"**不是**目标达成证据；每份交付物都带"预算耗尽/评审降级/未验收草稿"注记，
  状态如实降级，未进成功沉淀。

**M0-f 三块（分开做）**

- ① **离线完整交付**（`test_offline_delivery.py`，CI 已挂）：固定官方文档夹具 +
  真实编排链路（模型回包与 worker 结果在请求边界替身）跑完整交付，核对
  页面正文 hash == 记录的交付 hash、Markdown/PDF 各自 hash 且绑定同一 `report_version_id`、
  服务端导出路由送达字节 == 清单登记 hash、验收规则指纹与版本记录一致。
  同文件覆盖"离线不得联网"（`socket.connect` 直接失败）。
- ② **离线故障注入**（同文件）：取消（终态 CANCELLED、在飞转待对账）、评审超时（个人记降级并
  写进交付物 / 银行拒绝且不留已验证交付）、协议错误回包（不得判成功）、迟到结果
  （不覆盖已选中版本、留痕）。
- ③ **实机少量真实任务**（已重启服务加载新代码）：4 次公开研究任务 + 3 次取消验收，
  结果与暴露缺陷见 `docs/实机运行记录_M0f_20260916.md`。

**实机暴露并已修的 6 个缺陷**（每个都带离线回归）：取消无法打断模型调用/健康探测的等待；
预算耗尽只拒绝单次派发导致循环空转；配置里的预算上限不生效；研究类任务从不产生验收；
快速路径/单步任务没有选中版本；未验收草稿只在日志里说（现在写进交付物并如实降级状态）。

**验证**（真实退出码）

- 离线（全部 EXIT=0）：`test_offline_delivery` 8 项、`test_root_budget` 19 项、
  `test_cancel_semantics` 36 项、`test_report_version` 17 项、`test_admission` 29 项、
  `test_review_protocol` 24 项、`test_task_state` 8 项；`test_orchestrator_v2` 68 项、
  `test_delivery_chain` 184 项、`test_p0`、`test_deploy_manifest` 10 项均通过。
- 实机：`ui-2554df5f9f` 验收 pass + 选中版本带自身验收 + 交付 `ok=True` + 5/5 步、
  报告 12.3KB；取消 13s 进 CANCELLED 且被放弃的调用记为 `unsettled`；
  预算账本 `limits` 来自配置且 `reserved==settled`。

**未验证 / 已知缺口**

- **UI 鉴权与浏览器渲染未验证**：本机无有效会话、我没有凭据，也没有新建旁路凭据；
  任务提交走 webui 同一条内部通道，页面只核对数据一致性。
- 模型端点本轮持续退化（504/读超时/推理预算耗尽）：happy path 仅第 4 次跑通，
  不据此宣称稳定；应用内定时任务与实跑并发争用同一端点，耗时有干扰。
- 取消延迟 13s（>预期 3s）：取消信号还未传进 `llm_client` 的每次重试间隙。
- Embedding 欠费（402）：待补录队列在用，策略沉淀本轮因准入拒绝而未发生。
- 实跑为有界写入了 `system.budget={900s,60 次,0}`；是否保留由你决定（默认 0/0/0＝不限）。

**M0-a…e 摘要（已完成）**

- **a 版本与证据绑定**：单一权威版本记录（正文全量 SHA256 + 来源/规则指纹 + 该版自身验收）、
  唯一采纳点、恢复校验归属与 hash、收尾交付一致性与最终交付 hash、导出清单逐格式各自 hash。
- **b 评审协议**：非法身份模式不降级、裁决白名单（仅 PASS/FAIL）、修订稿需复评取得绑定 PASS、
  评审状态按根任务隔离、critic 关闭/直出/模板/恢复共用策略、降级写进交付物与 `review_state.json`。
- **c 取消/超时/协议**：等待四分类（result/cancel/timeout/protocol）、协议错误不冒充超时、
  取消覆盖验收前与三处人工确认、取消为持久终态、在飞转待对账。
- **d 经验与模板准入**：`admission.admit_success` 显式谓词（默认拒绝）三处共用；
  未验证策略标 `needs_review`、自迭代产出写 `pending_review` 不生效；统计只认 `verified`、同任务只计一次。
- **e 根任务预算与阶段观测**：一次任务一份账（原子落盘、恢复不重置）、先预留后发送、失败也结算、
  取消转待对账；规划与派发在发送前卡预算；阶段观测带等待对象/进展/剩余预算；
  额度冷却按端点生效（健康主端点不被欠费备用冻住）。

# 历史批次（V1 反例批次，2026-09-16 上半场）

**当时问题**：按《架构复核与纠偏_20260916》第 0 节修 V1 的三个实际回归（不放宽阈值）。

**本批修改**（工作区，**未提交**）

V1 三个反例（第 0 节）：

- **0.2 分来源通道**（`acceptance_checker`）：`clean_chart_data` 的结构化 JSON 与 `user_material` 文本不再拼成一段；
  派生判定改用"结构化文本 / 用户材料文本"各自通道，公式核验改用**结构化记录 + 用户材料里带单位的数字**
  组成的记录列表。修前：仅加入 `user_material` 文本就把原本 pass/1.0 的同比计算判成 fail/0.0。
- **0.3 完整公式 + 单位 + token 边界**（`_formula_derived_in_report` + `_unit_profile` + `_collect_source_records`）：
  表达式必须是紧邻数字的**完整括号算式**（删掉"截取局部二元式"的兜底）、操作数必须**按值**命中来源记录
  （不再用子串，`12` 不能命中 `1200`）、操作数之间量纲一致且与结果量纲/缩放相容（挡住 万元 与 亿元 混用）；
  换算常量（1/2/100）放行，其余缺输入即拒绝。指标/期间语义**尚未绑定**，未实现部分保持"未核实"。

**验证**（真实退出码，全部离线）

- 复核给的三条反例现在**全部拒绝**：`120%（1200/1000-1）` computed=0（值不符）、`2200亿元（1200+1000）` computed=0
  （量纲错一万倍）、`利润2亿元（12-10）` coverage=0.0 computed=0（缺输入+子串命中已被边界挡住）；
  正向对照 `20%（1200/1000-1）` 仍 **coverage 1.0 / computed 1**（没有靠收紧阈值把真话否掉）。
- 结构化通道不再被文本破坏：同一份报告在"仅 clean"与"clean+user_material"两种输入下判定一致（都 pass/1.0）。
- `test_report_quality` **22 项 EXIT=0**（新增 5 项反例/对照）；`test_acceptance_adversarial` EXIT=0、
  `test_deploy_manifest` EXIT=0。

**尚未修（按复核顺序，下一步）**

- **0.1 版本绑定**：`orchestrator_v2` 仍把同一份最新 acceptance 传给旧稿与候选稿（3746-3748、4048-4050），
  4209 用旧 best_report、4232 读最新验收 → 交付内容与元数据可能错配。要做的是把正文/来源/验收/指纹
  绑成**不可混用的版本记录**，修订比较与导出引用同一版本（不能只改 `compare_versions` 排序）。
- **第 1 节**：未知/缺失/非法评审裁决在银行口径下仍被放行（只有 ERROR/DEGRADED 被拦）；
  `_review_is_required` 吞掉 `default_mode()` 的配置错误 → 非法 mode 默认按个人模式；
  取消导致的等待返回被上层当作超时；`system.critic` 关闭/模板/恢复入口未覆盖。
- **缺验收/有缺口不得计成功经验或 verified 模板**（记忆与模板沉淀需按验收结果设闸）。
- B/C/D/E/F 连续实跑按复核要求**暂缓**；先离线定向回归、再一次有界端到端。

**事实更正**：B `ui-4cf6e482e9` 已于 **01:05:36 以 SUCCESS_WITH_ISSUES 结束（1697 秒，acceptance_json 为空）**，
当前 RUNNING=0；我此前"仍在卡死"的判断已过时，不再据此重启或断言死锁。它只作为"有缺口/失败样例"，
其财务数字不得当作事实引用。

**保护项**：门禁原样（命中留证、走复核）；模型权限未动；`templates.json` 指纹未变；配置中只写入了你提供的新 Key
（`llm`/`planner`/`backup` 三处，`config.json` 已在版本库忽略，未写入任何源码/示例/测试）。

# 历史批次（V2-1/V2-2/V1.1/V1.2/L01 网络通道）

**当前问题**：V2-2 评审退化（超时/异常不得当通过）；V2-1 取消语义与端到端验证待续。

**本批修改**（工作区，**未提交**）

V2-2 评审退化（本轮）：

- `_review_plan`：超时/异常/`ERROR`/`DEGRADED` 四种"没评上"的情形统一走 `_review_unavailable`——
  **个人模式**标记 `_review_degraded`（如实标注"未完成评审（降级）"，计划按草案继续，交付物注明需人工复核）；
  **银行口径**（`WEAVEMIND_IDENTITY_MODE=bank`）抛 `ReviewRequiredError` **拒绝继续**，不再把超时当通过。
- `verdict=ERROR`（Critic 身份/协议拒绝）与 `DEGRADED` 不再掉进"修订"分支（那里会多花一次 LLM 并掩盖"没评上"）。
- `critic_agent._fallback_review`：不再返回 `PASS`，改为 `DEGRADED` + 原因（"评审系统不可用，未完成评审——不得视为通过"）。

**执行过的测试与结果**（真实退出码）

- 新增 `test_review_degradation` **10 项 EXIT=0**：个人模式超时/异常标降级且不修订、银行模式超时/异常拒绝、
  ERROR 与 DEGRADED 不进修订分支、PASS/FAIL 行为不变（FAIL 仍触发一次修订）、回退评审非 PASS
- `test_deploy_manifest` EXIT=0、`test_prompt_system` 72s EXIT=0
- ⚠️ **`test_orchestrator_v2` 首次复跑超时：EXIT=124（900s 上限，此前 ~217s）**——属**未查明**的回归信号。
  已做一次防御性修正（`_plan` 里改为 `getattr(self, "_review_degraded", "")`，避免测试用 `__new__`
  构造的实例触发 AttributeError 在重试循环里拖死），并已重跑该套件，结果见下条；**在套件通过前不宣称 V2-2 已验证**。

**尚未验证**：8080 未重启，取消语义与评审退化的端到端行为（真实停止、真实 Critic 超时）需一次实机操作；
`_review_plan` 之外（反思 `3809`、评测闸门 `3712`、收尾阶段 `4175/4198/4241`）仍无取消前置检查。

**本批修改**（工作区，**未提交**）

V2-1 取消语义（本轮）：

- **独立终态**：`task_state.CANCELLED` 加入终态词表（`TERMINAL` 四值）；`_finish_cancelled` 改写 `CANCELLED`
  （此前硬编码 `FAILED`），SSE `task_complete`、落库、通知一致；`web_ui` 取消接口的终态集合补 `CANCELLED`。
  前端 `statusMeta` 早已映射「已取消」，此前是**死代码**。
- **派发取消闸门**：`_dispatch` 在入队（`lpush`）**之前**统一判定取消，覆盖所有派发路径（首次派发、重做链、
  任务级修复、ReAct 降级重派、重试、重规划后派发）——取消后不再发起新步骤与新付费调用。
- **等待提前返回**：`_wait_for_result` 增加 `cancel_task_id`，按 1 秒切片检查取消并立即返回——这正是实测
  "停止后仍等 ~92 秒"的那条路径（此前只在派发前挡新步骤）。

**执行过的测试与结果**（真实退出码）

- 新增 `test_cancel_semantics` **11 项 EXIT=0**：取消是独立终态、`_finish_cancelled` 写 CANCELLED 且清标志、
  SSE 事件携带 CANCELLED、**闸门位于入队之前**、`_cancel_requested` 读标志、等待循环**取消后 <5s 返回**
  （对照：无取消时正常拿结果、不传新参数行为不变）、取消接口终态集合含 CANCELLED、指标可计数、前端映射存在
- 受影响用例按新语义更新并注明原因：`test_writer_consolidation.test_finish_cancelled_finalizes_and_clears`
  由断言 `FAILED` 改为 `CANCELLED`（EXIT=0）
- 相关回归：`test_deploy_manifest` EXIT=0、`test_task_state` EXIT=0；新测试已接进 CI

**V2-1 尚未完成**（下一轮）：收尾阶段（验收 `4533`、Memory 固化 `4175`、模板沉淀 `4198`、后台提示词自迭代
线程 `4241`）与反思/评测闸门（`3809`/`3712`）仍无取消前置检查；无超时 `t.join()`（`4819-4820`）未显式加界
（当前靠等待提前返回来收敛）；人工确认等待（`4323/4380`，上限 600s）取消后不会立即结束。
**端到端未验证**：8080 服务按要求未重启，取消语义需一次真实"运行中停止"确认卡片显示"已取消"与收尾耗时——单测不能替代。

## 历史批次（V1 / L01 / 门禁）

**本批修改**（工作区，**未提交**）

V1.1 修订稿选择（不再以长短代替质量）：

- 新增 `report_quality.py`：`compare_versions()` 按**硬约束→验收→实质改进**排序——
  验收等级（pass>未知>fail）→ 验收缺口数 → 占位/未完成痕迹 → 是否重复用户需求块；
  全部相同则**保持当前版本**，并在原因里写明"长度差异不作为改进依据"。
- `orchestrator_v2.py` 两处（初稿轮次 3649、反思重做后 3947）都改为调用它，替换
  `len(cand) > len(best_report)`；日志改为输出**可解释的比较原因**，不再只说字符数。

V1.2 来源语义（用户材料是来源，但真实性未核实）：

- `acceptance_checker.py`：`run_acceptance` 把**用户材料**（任务目标/指令）注为独立来源通道；
  数字命中它记为 `source=user_material` + `user_provided=True` + `verified=False`（**不**算模型知识、
  **不**算不可溯源），并新增 `user_input_count` 计数；派生判定改用"清洗数据 + 用户材料"，
  再对**报告内写明的公式**做核验（`_eval_arith_expression` 用 AST 白名单求值，
  **不使用动态执行入口**——报告文本是不可信输入），要求公式的所有操作数都能在来源中找到。
- 阈值与判定强度**未放宽**：外部无证据数字仍失败、无输入的计算仍不可溯源。

**执行过的测试与结果**（真实退出码）

- 新增 `test_report_quality`（离线固定样例，不用付费 LLM）**11 项 EXIT=0**：更短纠错稿被接受、
  更长错误稿被拒绝、仅变长不算改进、验收等级压过长度、用户材料可溯源且标"未核实"、
  混合来源都可溯源、写明公式的派生值可溯源、未写公式的仍不可溯源、缺输入的计算仍被拒、
  外部无证据数字仍失败
- `test_deploy_manifest` / `test_acceptance_adversarial` / `test_p0`：本轮相关回归（见下条追加）

**门禁**：本轮写时扫描拦下一次**真问题**——我最初用动态求值算报告里的算式，被"代码注入"规则拦下，
判定正确，已改为 AST 白名单求值。另：先前那次 `execute` 命名与注释字面量误报仍记录在
`docs/门禁命中证据与复核请求.md` 第三节。未停用钩子、未换终端绕过。

V1.2b 免责声明按来源生成（本轮新增）：

- `report_quality.py` 新增 `disclaimer_clause(has_external, has_user_material)` 与 `disclaimer_instruction()`：
  三种措辞（只用用户材料 / 只用外部检索 / 两者都有）+ 选择规则，并明确"没有外部检索禁止写
  『数据来源于公开渠道』""用户材料必须写明真实性未经独立核实、不得改称模型知识、不得伪造来源"。
- `orchestrator_v2._REPORT_FORMAT_REQUIREMENTS` 里那条**写死的**"数据来源于公开渠道"已替换为
  `disclaimer_instruction()` 注入（A/C 这类以用户材料为来源的任务不再被强行宣称公开渠道）。

**执行过的测试与结果**（真实退出码）

- `test_report_quality` **17 项 EXIT=0**（11 → 17）：新增免责声明 6 项——只用用户材料不得出现"公开渠道"、
  只有外部来源保留公开渠道措辞、混合来源分别说明、无来源时说明是模型知识、注入要求含三种措辞与禁止条款、
  编排器不得再写死固定免责声明；以及修订稿选择 4 项与来源语义 7 项
- `test_deploy_manifest` EXIT=0、`test_acceptance_adversarial` EXIT=0、`test_p0` EXIT=0

**尚未做**（V1 余项，位置已定位，留给下一批）：

- **跨任务经验相关性/约束优先级过滤**：教训经 `orchestrator_v2._inject_memory_context(goal)`
  → `memory_manager.inject_context(goal)` 进入规划提示词（`:641` 以 "Relevant past experience" 拼入）。
  实测 B 被注入 A 的"不联网、虚构数据"约束，与 B 的官方检索目标冲突。做法：按当前目标过滤
  "约束类教训"（联网/虚构/离线/不执行代码 等），不相关的不注入或降级为"历史做法、未必适用"，
  且不得覆盖本次任务的显式约束；需要独立测试（相关/不相关/显式约束优先）。
- 正文长度策略显式化；页面/Markdown/PDF 同版本的端到端断言。
- `llm_client`/`lora_client`/`mcp_client`/embedding/webhook 仍未接入登记端点（L01 未完成项）。

**下一步**：V2 预算/评审退化/取消 → V3 PDF 排版 → V4 中文进度；银行认证未接入，**不宣称银行可用**。

**本批修改**（工作区，**未提交**）

L01 协议入口收口（依 17:38 复核补充，不只针对例子打补丁）：

1. **模式校验统一**：`resolve_mode(mode)` 让**显式传参与环境变量走同一条规则**——`admit_dispatch(..., mode="bnak")`
   与环境变量写成 `bnak` 得到同样拒绝（`IdentityConfigError`），不再因为"显式传参"跳过校验。
2. **只有缺 `context` 键才算旧消息**：显式 `context: null` 属协议非法，直接拒绝；旧消息走独立的本地兼容策略。
3. **v1 版本号仅接受 JSON 整数**：布尔（`True`/`False` 是 int 子类）、小数 `1.9`、字符串 `"1"`、`null`、数组、对象
   一律拒绝，不做 `int()` 转换；缺失版本键也拒绝；支持版本忽略新增可选字段。
4. **兼容缺口可观测**：`admit_dispatch` 返回 `(ctx, gaps, refuse_reason)`，三个接收点（两个 Worker + Critic）
   都记 warning 日志，Worker 还把 `context_gaps` 随结果回传——不再像以前那样在内部丢掉。

L01 此前四项（上一批，已接受）：Critic 先校验后评审（银行缺身份零调用 + 明确 ERROR）、清理进 `finally`、
心跳子线程 `copy_context`、Worker/Critic 共用 `admit_dispatch` 边界。

**执行过的测试与结果**（真实退出码）

- `test_task_context` **43 项 EXIT=0**（30 → 43）：新增边界矩阵 9 项（显式模式校验、`context: null`、
  版本类型矩阵、缺版本键、银行拒绝、兼容缺口返回）+ 真实 Worker 接收边界 4 项
  （调用基类真实 `_handle`：银行拒绝 → **零执行** + FAILED 回传 + 不残留身份；坏版本 → 零执行；
  `context: null` → 零执行；本地旧消息照常执行且回传 `context_gaps`）
- 相关回归：`test_deploy_manifest` EXIT=0、`test_p0` 128s **EXIT=0**、`test_orchestrator_v2` 202s **EXIT=0**

**门禁**：写时扫描第二次 `execute` 误报（测试替身里的协议方法名）已在
`docs/门禁命中证据与复核请求.md` 第三节如实记录（含取舍说明）；未改产品代码、未停用钩子、未换终端绕过。

**本批修改**（工作区，**未提交**）

网络通道（依《身份与网络边界》决策，新增 `net_policy.py` + `test_net_policy.py`）：

- 两类接口互不重叠：`request_service(endpoint_id, operation)` 读 `config.json` 的 `network.endpoints`
  登记表（用途/scheme/host/port/允许操作/`loopback` 标记），**调用方自报 `trusted`/`allow_private` 一律拒绝**；
  `fetch_document(url)` 走内容派生通道——仅 http/https 公网、拒绝环回/私网/链路本地/共享(`100.64.0.0/10`)/
  保留/组播/未指定/**IPv4+IPv6 映射地址**、拒绝 URL 用户凭据、**解析失败即拒绝**。
- 校验与连接同源：抓取用**已验 IP** 连接、TLS 保留 SNI 与系统 CA 校验、**不跟随重定向**、不带调用方凭据、
  有超时与响应体上限；审计只记脱敏目标与策略判定（`audit_logger`）。
- `adapters/transport._validate_public_url` 收敛为委托共享策略，修掉旧实现"DNS 解析失败也放行"、
  缺 `100.64/8`、缺映射地址判定三处（现有抓取点因此立即变为严格）。
- 真实缺陷：`workers/data_loader_worker.py` 的下载改走 `fetch_document`（内容派生 SSRF），
  文件名改为 `Path(name).name` + 落盘前 `resolve()` 边界校验（Windows 反斜杠穿越），失败如实回报。
- 配置面：`config.example.json` 新增 `network.endpoints` 示例（含登记的本机模型），`settings_schema` 同步。

L01 协议入口（上一批，已接受）：模式校验统一（显式 mode 与环境同规则）、只有缺 `context` 键才算旧消息、
v1 只接受非布尔整数、兼容缺口可由调用方记录与回传。

**执行过的测试与结果**（真实退出码）

- `test_net_policy` **22 项 EXIT=0**：决策的最小验收集合全覆盖——登记本地模型允许、同一地址经内容抓取拒绝、
  未登记私网拒绝、公网允许、DNS 失败/公网+私网混合/映射地址/`100.64.0.1`/元数据主机/用户凭据/协议 全部拒绝、
  重定向不跟随、跨源不携带凭据、**连接使用已验 IP**（防重绑定）、体量上限、未登记/私网目标不触达传输层、
  旧校验器委托后行为一致；另含 data_loader 接线 2 项（内网地址拒绝且不落盘、穿越文件名被夹在工作区内）
- `test_task_context` **43 项 EXIT=0**；相关回归：`test_settings_requirements` 26 项 EXIT=0（新配置段已进设置清单）、
  `test_deploy_manifest` EXIT=0、`test_p0` 126s **EXIT=0**、`test_delivery_chain` 132s **EXIT=0**

**门禁**：写时扫描第三次误报（注释里出现"关闭证书校验"的字面量被当作真的关闭校验）已记入
`docs/门禁命中证据与复核请求.md` 第三节；实现用的是系统 CA 校验。未改产品语义、未停用钩子、未换终端绕过。

**下一步**：把 `llm_client`/`lora_client`/`mcp_client`/embedding/webhook 的地址解析接到登记端点
（当前它们仍直接用配置里的 base_url，未走 `request_service`）；随后 L02 全链预算、事实链。
银行认证与可信身份仍未接入，**不宣称银行可用**。

**尚未验证**（按决策显式保留）：编排器仍未从可信认证/会话链填 `tenant/workspace/actor` → **银行能力未就绪**，
不从提示词/前端自报/默认公共租户补值；`deadline` 与 `step_deadline` 的同步规则未定；预算强制执行属 L02。

**下一步**：按决策实现网络通道（`request_service` 登记端点 / `fetch_document` 内容抓取；修
`_validate_public_url` 的 DNS 失败放行与 `100.64.0.0/10`、校验与连接同源解析），再做 L02 与事实链。
门禁保持原样，不换终端绕过、不为减命中改写代码。

# 历史记录（2026-09-15 之前）

**该批修改**（提交 `d6505ef` / `c600943` / `de2a825` / `8cbee18` / `ee99dd0` / `ebfad59` / `e72378a` / `eea6f0f` / `ab740b5`）

## 一、CI 红灯定位与修复（提交 `fix(CI)`）

1. **`docker-image` 作业 "Build image" 失败**
   - 根因：`Dockerfile` 有 `COPY prompts/ ./prompts/`，但 `prompts/` 在版本库里**零文件**（只装 gitignore 的
     `overrides.json` / `*.bak`）。本地目录还在，所以怎么跑都看不出问题；CI 干净检出后该目录不存在，
     `COPY` 直接以 `not found` 失败。本地反复构建失败于 Docker Hub 不可达（`auth.docker.io` 超时），
     一直没能走到这一步。
   - 修复：删除该行。override 属运行时可写状态——容器里落 `/data/prompts`（`WEAVEMIND_DATA_DIR`），
     `prompt_registry` 写入时 `mkdir(parents=True, exist_ok=True)`，缺失即空覆盖，镜像不需要这份空目录。
   - 守卫：`test_deploy_manifest.py` 新增 `test_copy_sources_exist_in_clean_checkout`（每个 COPY 源必须在
     git 索引里）；并用历史那一行自证有齿（喂 `COPY prompts/` → 报红）。同时把 `prompts` 从
     `REQUIRED_RESOURCES` 移除——它此前把该 bug 固化成了"必须拷进镜像"。
2. **`backend` 作业 "Settings requirements tests" 失败**
   - 根因：`test_all_config_sections_covered_or_declared` 直接打开本机 `config.json`（已 gitignore）→
     CI 无此文件，抛 `FileNotFoundError`。
   - 修复：优先 `config.json`、回退 `config.example.json`，两者都没有才 fail。已按"无 config.json"的
     CI 条件本地模拟验证通过。

## 二、同家族缺陷收口（提交 `fix(Redis)`）

冷启动竞态修的是 `common.MessagingClient`，但**同一写法还有 8 处**：`adapters/quote_cache`、
`adapters/source_health`、`lora_client`、`metrics_collector`、`orchestrator_v2._new_redis_sync`、
`tool_dispatch`、`smoke_test`、`verification_suite` 构造 `redis.Redis(...)` 时未关内建重试（后两处连超时都没有）。

- 实测代价：`/api/config/requirements` → `health_registry._redis_get` 在 Redis 不可达时单次要 26~48 秒才失败，
  设置页/健康页表现为长时间卡死；本地跑 `test_settings_requirements` 的 `TestConfigEndpoints` /
  `TestHealthRegistryProbes` 直接挂住（现在分别 26s / 42s 跑完，EXIT=0）。
- 修复：8 处全部显式传 `retry`（复用 `common._NO_REDIS_RETRY` 或文件内同款常量）；`smoke_test` /
  `verification_suite` 补 2 秒连接与读超时。
- 守卫：`test_startup_readiness.py` 新增 `TestNoUnretriedRedisClients`——AST 扫全仓，任何未传 `retry=` 的
  同步客户端（`aioredis` 除外）报红，并带检测器自证。
- 口径更正：`MessagingClient` 连不通实测 **7.6 秒**抛出（redis-py 内建重试已关，耗时来自
  `MessagingClient._connect` 自己的 3 次退避——那是应用层策略，不是 redis-py 的重试风暴）。

## 三、执行过的测试（本地 Windows / Python 3.14，真实退出码）

| 测试文件 | 结果 |
| --- | --- |
| `test_deploy_manifest` | 10 项 EXIT=0（含新增检出等价性守卫） |
| `test_startup_readiness` | 7 项 EXIT=0（含新增全仓重试守卫） |
| `test_settings_requirements` | EXIT=0（修复前 `TestConfigEndpoints`/`TestHealthRegistryProbes` 挂死） |
| `test_metrics_scope` | EXIT=0 |
| `test_delivery_chain` | EXIT=0 |
| `test_lora_manager` / `test_isolation_scope` | EXIT=0 |

## 四、F3a 报告页完整升级（已落地并实测）

六项全部接线（`frontend/src/components/ReportViewer.tsx`，不动后端契约）：

| 项 | 内容 |
| --- | --- |
| 结论卡（置顶） | 正文首表前 4 行，列与截断口径与分享页 `_share_page_structured` 一致；无表时降级显示验收器统计并标注"非报告结论" |
| 数字可信度卡 | 四档计数 + 数字溯源率 + **金额溯源率单列** + 不可溯源清单（最多 10 条，与后端截断一致）+ 口径/阈值/域 + 达阈值判定 |
| 图表 | 图注（title/alt）、点击放大（Esc 与遮罩关闭）、单图下载、"草稿级"标签（"补充图表（未达发布标准）"章节下） |
| 证据指纹 | 规则版本 / 规则指纹 / 报告 SHA256 / 评估时间，附"为何留指纹"说明 |
| 验收时间线 | `/api/task/<id>/acceptance/timeline`：每轮 trigger / overall / 缺口数 / 时间 / 报告指纹前 8 位 |
| 导出区收敛 | 分"报告文件""复核与分享"两组；交付包 zip 标注未提供的原因（`/files` 只放行 reports\|charts\|data） |

**浏览器实测**（新实例真跑一个财务任务 `ui-02ef5f30f1`，SUCCESS）：

- 可信度卡 **与 `acceptance_report.json` 逐项一致**：共 27 个 / 引用 25 / 计算 0 / 模型知识 2 / 不可溯源 0、
  数字溯源率 93%（25/27）、金额溯源率 100%（25/25）、阈值 70%、域=财务、判定"达阈值"
- 指纹卡与后端一致：`2026.09.12` / `2ddecc9a` / `b40ff4b1e89ee23e` / `2026-09-15T03:11:55+00:00`；
  时间线 1 条（报告步骤 / pass / 缺口 0 / 同一指纹）
- 图表：3 张渲染，`src` 为后端改写后的 `/files/ui-02ef5f30f1/charts/*.png`；放大浮层弹出、**真实按键 Esc 关闭**；
  单图下载取数 `200 image/png 26122 bytes`
- 实例 `index.html` 与本地产物哈希一致（确认实测的就是本批构建）

**顺带修掉一个既有缺陷（非本批引入）**：报告容器带 `animate-fade-in`（`will-change: transform`）会为
`fixed` 后代新建包含块 → 分享对话框浮层被拉到报告全高（实测 **6388px、top=-5693**，用户根本看不到，
无法设密码/生成分享链接）。新增的图表浮层同样会中招。两处改为 `createPortal(document.body)`；
修后浮层高度=视口高度、两个按钮均落在视口内（实测）。

**未触发的条件渲染**（本样本无对应数据，待有样本再验）：未溯源清单（该任务 `unverifiable_count=0`）、
"草稿级"标签（该报告无"补充图表（未达发布标准）"章节）。

## 五、F3b 交互与移动端（第一批已落地，实测通过）

已落地：

- **窄屏底栏**：10 项平铺改为 4 个高频页 + 「更多」抽屉；抽屉内含其余 6 个页面、演示开关、退出登录。
  此前 <640px 时演示开关与退出登录全部不可达（两者都带 `hidden sm:flex` / `hidden md:flex`）。
- **StepInspector**：字段中文化（步骤详情/能力/执行体/指令/执行结果/决策轨迹）；结果改为白名单字段 + 中文名，
  对象/数组只进折叠区；**本机路径脱敏**（保留末两段，仍可定位文件）；原始 JSON 默认折叠；小屏全屏（`w-full sm:w-96`）。
- **重跑二次确认**：说明"重新规划并消耗额度"，给「确认重跑 / 改为修改目标 / 取消」三个出口
  （"复用原计划"需后端支持，对话框内如实说明；「改为修改目标」把目标填回提交框并滚回提交区）。
- **演示模式置灰扫尾**：Agents 直发（输入框 + 发送）与终止、Settings 通知保存、定时任务启停/删除/新增、
  用户角色/改密/删除/新增——全部 `disabled` + 原因 title，不再"点了才被 403 拦"。
- **实时通道降级提示**：SSE 断开回退轮询时顶部提示"实时通道已降级为轮询（每 2 秒拉一次）"，并说明任务不受影响。
- **响应式**：报告页统计条、Agents 统计、Health 五列、Evals 四列改为断点网格（窄屏 2 列）。

实测（375×812 与 1280×720，实例真机）：

- 底栏 5 项、`scrollWidth - innerWidth = 0`（无横向溢出）；「更多」抽屉在视口内（挂 body、面板高 303px），
  含演示开关与退出登录；**手机端开启演示成功**（横幅出现、底栏标签变「更多 · 演示」），关闭后恢复
- 测试手段说明：`onToggleDemo` 用**同步** `window.confirm` 确认，自动化点击会被原生弹窗阻塞（实测 32s 超时），
  验证时先在页面里桩掉 `confirm`——只影响测试会话，未改产品代码

**第二批（时长提示，已落地）**：

- 提交区显示"近 N 次任务平均 X 分钟"（取 `/api/status` 里已完成任务的 created/completed 差值，
  样本为 0 时明说"预计时长未知（暂无已完成任务样本）"，不给假预期）；运行中显示"已运行 X 分钟"（每 30s 走一次）
- 实测：实例上该文案为"近 1 次任务平均 23 分钟"，与该任务实际耗时（03:08 → 03:31）一致

**仍未完成（留作 F3b 第三批）**：

- 三态分离：约 29 处静默 `catch {}` 尚未逐页改为"错误态 + 重试"（优先 History / TaskConsole / Memory /
  ReportViewer / useTaskLive）。逐页都要补错误态 UI，批量改容易引入回归，单独一批做

## 六、F4 守卫固化（已落地）

`test_frontend_guards.py` 由 13 项扩到 **27 项**（该文件已在 CI 内，由 `test_deploy_manifest` 的
"每个 test 文件都进 CI"断言兜底），新增守卫：

- **浮层必须走 `createPortal`**：并断言"`fixed inset-0` 数量 ≤ `createPortal(` 数量"——正是 F3a 实测到的
  6388px 浮层那个坑。已用合成反例自证有齿（未挂 portal / 未引入 createPortal 两种写法都报红）
- **报告页接线**：`covered_ratio`、`amount_rate`、`untraceable`、`unverifiable_count`、四项指纹字段、
  `/acceptance/timeline`、结论卡口径（键 24 / 值 32 截断）、图表三件套（草稿级 / Esc / 下载图片）
- **窄屏入口**：底栏 4+更多 收敛标记、抽屉含「演示模式」「退出登录」
- **步骤详情**：`redactPaths` 不仅定义还要在实际渲染路径上用（≥4 处）、原始 JSON 默认不展开、
  中文标签齐全、不得残留英文标签
- **重跑必须确认**：且不得退回 `onClick={() => onSubmit(m.goal)}` 这种直接提交

未纳入（依赖 F3b 第二批）："错误态 ≠ 空态"的断言。

## 七、阻塞与待验证

- **`docker-image` 作业本轮无本地复现**：本机到 `auth.docker.io` 超时（Docker Hub 不可达），镜像构建
  走不到 COPY 那步；修复依据是"COPY 源与干净检出不等价"这一确凿事实，最终确认只能靠 CI 复跑。
- **推送需要一次性凭据（已定位，不是代理问题）**：代理 `http://localhost:7897` 正常——`curl` 走代理访问
  `github.com` 首页与 `git-receive-pack` 端点均在 0.7~1.4 秒内返回（未带凭据的 401）；`git ls-remote` 正常。
  阻塞点在凭据：`git credential fill`（配 `GCM_INTERACTIVE=never`）返回"无缓存凭据"，而仓库内
  `credential.helper=manager`（GCM）在无 TTY 环境下转为等待交互式登录 → push 挂住直到超时。
  本机也没有 `gh` CLI、没有 `~/.git-credentials`、环境变量里没有令牌。
  补齐一次 GitHub 凭据（或由本机人工执行一次 push）即可；本地现有 4 个提交待推：
  `8cbee18`（冷启动竞态）、`ee99dd0`（CI 两条红灯）、`ebfad59`（Redis 重试收口）、`6e3799e`（门禁记录）。
- **CI 尚未复跑**：上述 4 个提交未推送，`docker-image` 作业的修复只能等推送后有 CI 结果才能确认。
- 未修（已排序）：交付包内 `index.html` 编码缺陷已检出未拦截、图表分级未覆盖自动生成图、
  美股结构化财务链路未生效（金额溯源 0%）、便携 Redis 默认源为 GitHub（无代理会卡住）。

---

# 历史记录（2026-09-14）

**当时问题**：代码沙箱安全默认值——未配置 / 取值非法 / 隔离不可用都必须拒绝执行，不得回退宿主。

**该批修改**（提交 `d6505ef`）

- `code_sandbox.py`：默认（未设置）即按 docker 策略；删除 FALLBACK 开关；非法取值抛 `SandboxConfigError` 并拒绝；`sandbox_status()` 区分 `isolation_ready`（restricted/none 恒 false）与 `execution_available`；拒绝提示只给恢复隔离的出路，不诱导关闭隔离
- `workers/code_execution_worker.py`：`_run_smoke` / `execute` 遇设施错误原样上抛并保留根因，不进入模型重生成循环（新增 `code_sandbox.is_facility_error`）
- `health_registry.py` / `web_ui.py` / `launcher.py`：健康项以隔离为判据、同时报"能否执行"；启动日志说明隔离状态但不阻断启动
- `docker-compose.yml` / `docs/部署指南.md` / `README(.en).md`：删除"挂 socket 即真隔离"的未验证承诺；容器内代码执行不可用、工作台其余功能照常
- 测试：新增 `test_sandbox_isolation.py`（24 项）；`test_p0` 沙箱类按新语义更新；`test_delivery_chain` 执行流水线用例显式选开发模式（未删用例、未全局设值）

**当时执行过的测试**：`test_p0` 387 项、`test_sandbox_isolation` 24 项、`test_delivery_chain` 171 项（排除网络类）及其余 10 个文件均 EXIT=0；mock 故障分支一律拒绝、宿主进程创建次数 0。

**新环境实测补充（`docs/新手实测报告_20260914.md`）**

- 执行隔离**无直接证据**（原表述已更正）：`grade=publish` 是图表**质检等级**，`charts_pipeline` 内置渲染走宿主 `subprocess.run`，不能据此推断容器执行
- **P0 前端缺陷已修（批 F1）**：登录/创建管理员/退出白屏（`React #300/#310`）→ `App.tsx` 去掉早退后的 `useMemo` + `ErrorBoundary` 覆盖两条分支；同批演示模式不再发真实请求、首屏状态改"连接中"、指标页区分进行中/已完成
- **F1 补修（架构审查四项）**：① 指标改为后端同范围同快照聚合（`test_metrics_scope.py` 7 项）；② 演示模式改为 fetch 层统一拦截写/付费操作；③ `grade=publish` 不能证明容器隔离，报告已更正为待验证；④ 门禁命中与恢复证据见 `docs/门禁处理记录.md`
- **冷启动竞态（P0，已修）**：新实例首次启动 15/16、编排器静默死掉；faulthandler 抓到栈落在 `redis/retry.py call_with_retry`。修复=关内建重试 + 启动器拉服务前等一次成功 PING，细则见报告 §2.5
