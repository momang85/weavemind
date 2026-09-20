# C2 证据：从"来源准入"推进到"结论受支持"（2026-09-20）

批次入口：`docs/报告成稿复核与下一步指导_20260920.md`（C2 六条）。C1 读数见
`c1_export_and_review_honesty_20260920.md`；F3-A/F3-B 见另两份证据。

## C2-1 引用了未采用来源的句子：保留主张—证据关联

**问题（洋河原稿实机形态）**：正文用"经21财经报道交叉印证""2024 年 5%—10% 目标未达成"
开展判断，同一材料在来源清单里却是"未采用"；装配只把映射不到的编号**去掉**，读者仍会
把该句当成有来源支持的事实。`report_brief._remap_inline_refs` 的旧行为就是"去编号 +
记缺口"，没有任何句子级的说明。

**修法**（三层，逐句而不是末尾笼统免责）：

| 层 | 落点 | 读数 |
|---|---|---|
| 逐句记录 | `_unsupported_claims()` | 句子、原编号、原 URL、标题、**未采用原因**（取自审计里的 `rejected` 原因，如"晚于资料截止日""未取得正文或未核验，仅罗列"） |
| 正文标注 | `_mark_unsupported()` | 在该句句末标点**之前**插入 `〔待核查：本句引用的来源未采用（原因：…）（该来源未采用，结论不得当作已证事实）〕`；标记文字**不含数字**（正文新增读数会被验收当"不可溯源数字"拦下） |
| 主张记录 | `_claims(..., unsupported=...)` | 该句 `status=unsupported` + `reason` + `source{url,title,old_n}`；不因"数字绑得上底稿"就算支持 |

**两条边界都做了**：
- **不删句**：`test_excluded_source_leaves_claim_marked_not_silently_dropped` 断言被标记句
  之外的句子（"降幅为近三年最大""归母净利润 66.73 亿元"）仍在正文里；
- **不误标**：`test_adopted_source_claims_are_untouched` 断言来源在采用清单里时**不出现**
  任何 `〔待核查` 标记。

**句法坑（已修）**：编号常写在句号**之后**（"…尚未见效。[3]"）。按标点直接切句会让引用
与它支持的那句话脱钩 → 标记落不到该句、主张记录也认不出。现在 `_sentences()` 把紧跟句末
标点的纯引用记号归回上一句，`_sentence_key()` 对编号/标点/括号免疫。

## C2-2 主张记录可追溯的最小字段

`structure["claims"]` 每条：`text / type / status / reason / subject / periods /
citations / fact_ids / source / version_id`；`version_id` 由
`report_brief.stamp_structure_version()` 在**装配确定版本后**盖上（装配前为空串）。
`structure["version_id"]` 与之一致——结构对象与主张都绑定本版正文。

`type` 取值：`observation`（数字绑得上底稿）/ `inference` / `assumption` /
`third_party_view`（引用了第三方报道）/ `target_claim`（目标类判断）/ `unbound`。
`status` 取值：`bound` / `needs_check` / `unsupported`。

## C2-3 可准入 ≠ 每句可证明

**① 会计政策/准则套话、仅重复数字的段落不得冒充变化解释**（`narrative_evidence`）：
新增 `CAUSAL_MARKERS` / `is_causal()` / `is_policy_text()`，`classify()` 与
`_prose_classify()` 都加同一道闸——归为"变化解释"必须有因果语言且不是政策套话，否则
降级为附注（数字出处）。

真实文本上的验证（`evals/real/yanghe_ar2024_excerpt.json`，洋河 2024 年报摘录）：
摘录里的"1、收入确认""一、审计意见"（真实第 27 页）**不再**进入变化解释；结果是
`missing_labels = ["经营变化解释"]`——**如实缺失**，而不是拿政策声明填上。
对照 F3-A 那轮的真实年报读数："变化解释 第 37 页（财务报告 > 会计政策声明）"曾被当作
变化解释采纳，正是本批要修的行为。

**② 仅重复数字的附注不进解释**（`report_brief._change_explanation`）：附注记录只有
`is_causal(snippet)` 为真时才作为解释候选；数字本身已由底稿给出。

**③ 目标/达成类判断另立门槛**（`_is_target_claim`）：句中含"目标/达成/完成率/经营计划"
时，除非引用里有**发行人年报**，否则 `type=target_claim`、`status=needs_check`、
原因写明"目标类判断需核对目标年度、目标发布时点与实际数"。这正是洋河原稿
"2024 年 5%—10% 目标未达成这一事实"的形态。

## C2-4 同一主张状态贯通各处

- 正文：逐句标记（C2-1）；
- 缺口/风险：`_risks()` 新增 `unsupported_claim` 条目（点名同一句 + 未采用原因 +
  要补的材料）；
- 面板：`research.claims` / `research.unsupported_claims`（状态、原因、主体、期间、
  来源编号），前端在"修改与重验"块把 `未采用来源`/`待核查` 逐条列出；
- 导出：正文即交付字节，标记随正文进 Markdown/PDF/交付包（同一 `version_id`）。

**"解释已取得"与"尚不能证明"不再并存**：`_change_explanation` 给每条未证明项打
`has_explanation`（有管理层/附注解释即真），风险文案随之分为
"解释已取得（见『变化解释』），但量价与贡献程度未核实" 与 "…的变化原因尚不能证明"。
正常场景第 3 页那种"一边给解释、一边说没有解释"的重复消除。

## C2-5 真实年报文本的正向离线验收

夹具 `evals/real/yanghe_ar2024_excerpt.json`：洋河股份 **2024 年年度报告真实文本的有界
摘录**（东财公告文本 API，`art_code=AN202504281664011244`，披露日 2025-04-29 ≤ 资料截止
2025-04-30），按**真实页码**（1/2/3/5/10/11/27/37/64 页）摘取业务背景/附注/风险/政策小节，
一字未改，7.3KB。

读数：`located=8`、`excluded=[]`、`missing_labels=["经营变化解释"]`；定位带真实页码
（"第 2 页""第 10 页""第 64 页"）；来源类型 `issuer_annual_report` +
`based_on="发行人年度报告原文，经第三方平台（data.eastmoney.com）获取"`。

**未覆盖**：摘录的页码范围内没有带因果语言的经营讨论正文（真实年报的 MD&A 正文不在这
几页），因此"真实经营讨论进入变化解释"这一正向分支**未验**；本轮没有为此再扩大抓取
（每次抓取都是真实网络请求）。合成场景仍只作分支行为验证。

## C2-6 面板与版本同版

- `_research_payload` 新增 `structure_version` 与 `structure_current`：结构对象的版本与
  当前选中版本一致才为 `True`，**证明不了就是 `False`**（fail closed）；前端在该值为
  `False` 时显示"面板绑定版本 X，与当前版本 Y 不一致——修订后需重新装配，勿把旧发现当
  新版"。
- 实机读数（`ui-f00775cfdf`）：`structure_version=""`（该结构是 C2 之前的装配产物）→
  `structure_current=False` → 页面如实提示。该报告正文里的"经21财经报道交叉印证"一句
  **仍未被标注**（它是旧装配的字节）；重新装配需走修订/重跑，本轮**没有**改动这份已交付
  的实机报告（避免覆盖研究员可能正在复核的交付物），只把提示做出来。
- 单元读数：`test_panel_reports_stale_structure`（无版本记录 → False）、
  `test_panel_claims_carry_support_status`（主张状态/来源进面板）。

## 未验证 / 遗留

- 真实经营讨论（有因果语言）进入"变化解释"的正向分支未验（见 C2-5）；
- 实机未重跑：本轮无新增模型调用，端点是否可用**未验证**（用户"密钥已重新配置"不等于
  端点已通，执行者未发起恢复探针）；
- `visual-judge` 子代理账号仍不可用（C1 已记），PDF 观感评审仍只有执行者自查；
- 主张记录目前覆盖**模型正文里的含数字句子**；纯文字结论（不含数字）不在其中——需要时
  再扩，不在本批。
