# S3 上半：按金融资料类型路由（A 股原始披露摄取窄闭环）

日期：2026-09-27。批次：搜索网络与金融资料专项 **S3 上半**（专项 §7）。
承接 S2（`s2_network_egress_20260927.md`、`s2_health_states_20260927.md`：出口显式化、健康四态）。

本批做三件离线可验的事：① 泄露日纪律（期末/公告日/获取日三分离）；② 权威域判定不再被
URL 参数伪造；③ 原始披露摄取的窄闭环（发现/人工接入 → 原文 → 主体期间截止校验 → 可定位证据）。

## 1. 改了什么

| 位置 | 之前 | 现在 |
|---|---|---|
| `facts.py` 披露日 | `disclosure_date or disclosed_at or report_date`：**期末顶替公告日** | 只认来源声明的披露/公告日；缺失留空（未知）。底稿 A′4 的"时点未核实"闸因此真正生效（此前被期末静默填平） |
| `facts.py` 日期字段 | 期末、披露日两个 | 加 `retrieved_at`（获取日，来源声明了才记）；**期末 / 公告日 / 获取日三者分开** |
| `search_quality._domain_hits` | `needle in url` 整串子串匹配 | 只看 hostname，按"完整域=自身或子域 / `ir.` 前缀标签 / 单标签=可注册域标签前缀"匹配；`AUTHORITY_JUNK` 里两个前缀式写法（`blog.csdn`/`cp.baidu`）改为标签式（`csdn`/`baidu`） |
| `finance_plugin` 来源说明 | "SEC 公共数据无使用限制" | 改为 SEC 公平访问政策（≤10 请求/秒、可识别 UA）与"按原始披露标注并保留 filed/accn"；FRED 补"聚合二次发布、官方口径以发布机构为准" |
| `adapters/disclosure_ingest.py`（新） | 无：发现/准入/证据绑定散在链路各处 | 摄取层：`discover()`（巨潮未连通即如实不可用 + 正当出路）、`pick_official_candidates()`（纯函数筛候选）、`ingest()`（主体/期间/截止三道校验 + 证据定位 + 正文 hash） |

**边界内没动**：不新增服务端请求点（取件仍走既有通道）；不绕验证码/访问控制；
`net_policy` 的两类授权边界没放宽；东财仍标注为聚合源（`eastmoney_ashare`），不称官方。

## 2. 逐条验收（专项 §7 与退出条件）

| 项 | 做法 | 读数 |
|---|---|---|
| 洋河素材作回归 | 冻结样本 `evals/real/yanghe_ar2024_pages_20260922.json`（东财公告文本 API 真实取件，71 片段） | `ingest` → **admitted**；披露日 2025-04-29（day 精度）≤ 截至 2025-04-30；证据坐标为 `api_chunk`；正文 hash 绑定（`TestDisclosureIngest.test_real_frozen_report_is_admitted_with_locatable_evidence`） |
| 错主体被拒 | 同一文档、请求主体换 `贵州茅台/600519.SH` | `subject_mismatch`（且**先于**"非官方源"判定——否则操作者会被引向错误方向） |
| 错期被拒 | 请求期间 2023、文档自身报告期 2024 | `period_mismatch`。**关键**：样本正文里满是"2023"对比年，只按"正文含该年"判会误放行，故按标题的报告期判（`test_wrong_period_is_rejected_even_though_text_mentions_it`） |
| 晚于截止被拒 | 截至日 2024-01-01、披露日 2025-04-29 | `after_cutoff` |
| 披露日未知/精度不足 | 无公告日字段、URL 只有年份（`/report/2024`，那是**报告期**） | `cutoff_unknown` 且 `may_use_as_background=True`（材料可作背景，不得当已核验时点证据）；不宣称截至日时允许准入但记 `precision="year"` |
| 第三方转载原文 | 东财公告页转载洋河年报全文（真实样本即此形态） | 准入（标题即报告 + 主体命中），`provenance` 如实标 `auto_search` / `manual_url` / `manual_file`；点评/摘要类文章 → `not_official_source` |
| 巨潮未连通保持不可用 | `discover()` | `unavailable` + 原因（探针恒空/需 token）+ 正当出路（官方直链 / 人工取得文件）；**不伪造公告列表** |
| 候选筛选 | 三条候选（官方域原文 / 点评文章 / 别家公司） | 只留第一条（`test_candidate_picker_keeps_official_and_drops_commentary`） |

## 3. 第二家公司的发现问题（**未闭合，照实记录**）

指令要求"另选已有授权的第二家非金融公司原始年报"证明同一条路径。执行者两次**有界**检索
（项目自己的检索通道，无模型、无付费）均**零候选**：

```
web_text_search("三一重工 2024年年度报告 公告")     → 0 条
web_text_search("三一重工 600031 2024年年度报告")   → 0 条
```

这正是专项 §3-3/§3-7 指出的**发现链路未闭合**：检索侧拿不到官方公告直链，巨潮接口又未打通，
于是"自动发现→原文"这一段目前只能靠**人工提供直链或人工取得的文件**（摄取层已实现并测过这条
路径）。按"同类失败不继续轮询"的纪律，本轮不再换词重试；第二家公司的**真机**取证因此待办，
两条正当出路：① 操作者给一个官方直链（`manual_url`）；② 巨潮发现接口打通或经管理员批准的
替代官方列表源接入（`discover()` 已预留分支）。

## 4. 命令与结果

```
python -m unittest test_search_quality_unified   # 53 OK（新增权威域边界 17 例 + 伪造参数 1 例）
python -m unittest test_facts                    # 46 OK（披露日不回落、获取日分记）
python -m unittest test_working_paper            # 61 OK（真快照夹具改为"期末+公告日"同形状）
python -m unittest test_delivery_chain           # 含新增 TestDisclosureIngest 9 例
python -m unittest test_p0 test_financial_chain  # 608 OK（合计）
npm run build                                    # 本批未动前端
```

被调整的既有断言（修复必须调整行为预期）：`test_facts.test_period_end_fallback_is_recorded`
→ `test_period_end_never_stands_in_for_disclosure_date`（期末不再当披露日）；
`test_working_paper` 的 `REAL_SNAPSHOT`/`BASELINE_PAYLOAD` 与三处覆写从"把公告日塞进
report_date"改为真实形状（`report_date`=期末、`disclosure_date`=公告日）；
`test_working_paper.test_missing_disclosure_date_is_unverified` 改为去掉 `disclosure_date`。

## 5. 未做 / 未验（照实）

- **未做**：第二家公司的真机取证（见第 3 节）；巨潮发现接口打通。
- **未做**：`disclosure_ingest` 尚未接进编排器/抓取 worker 的**在线**路径（本批只做摄取层与
  离线验证；接线要动 `web_fetch_worker`/编排器候选处理，按"一批一提交"留给下一批，避免一次
  改动过大）。
- **未做**：人工上传的**通道**（工作台没有上传端点）——本批只支持"给直链/给本地文件路径"，
  与既有事实一致（无外部文件接入通道）。
- **未验**：港股 HKEX 与美股 SEC 的同类路径（专项明确"仅保留扩展边界"）；SEC 的 filed/accn
  版本保留仍缺（待专项后续批次）。
- **未验**：`ingest()` 在真实**PDF** 上的表现（本批证据是公告文本 API 的接口片段；PDF 路径
  走 `annual_report_pdf` 已有实现，但没有本轮真实取件）。
