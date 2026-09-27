# H2 问题保留与证据性质可见（2026-09-27，Harness 接续）

批次：**H2**（指令 `docs/Harness接续执行指令_20260927.md` §2-H2）。纠正
`docs/evidence/c2_question_contract_20260927.md` §5 记下的两个取舍。不降低旧题完成标准、
不伪造材料、不覆盖人工正文。

## 1. 取舍一：负基数问题不再消失

**原取舍（C2 §5）**：同比算不出（基期为负）时**不出这一问**——理由是"照样出问并给绝对额
会把 PDF 分页推到多一页近似空白页（冻结场景 `pdf_parseable` 反例）"。

**H2 判定**：已在契约中的问题不能因同比不可算、证据不足或分页效果而消失；保持问题身份与
分母，展示**绝对变化 + 可比性限制 + 未完成原因**；分页问题**修布局**，不删问题。

**改法（`report_brief._research_questions`）**：删掉 `continue`，改为保留本问并改换表述：

| 项 | 内容 |
|---|---|
| 观察 | `归母净利润 -10亿元（2023 年）→ 8亿元（2024 年），绝对变化 +18亿元；两期存在非正值，同比（百分比）不具可比含义，本问不给百分比方向` |
| 边界（前置） | `基期为负/为零时同比没有可比含义，本问**不给出百分比方向**，只按绝对额与由负转正/同负表述；` |
| 未完成原因 | 问题对象新增 `retained_reason`：`同比不可算（基期非正）**不构成删题理由**：本问保留在必答问题里并保留分母，未完成原因见依据与下一步`，并在正文以"说明："露出 |
| 结构字段 | 新增 `yoy_computable`（页面/清单可判是否可比） |

**分母**：`_question_assessments` 本来就遍历全部 `_RESEARCH_QUESTIONS`（三问），所以评估
分母一直是 3；消失的是**读者可见的那一问**。改后正文与评估都是三问，两处一致。

## 2. 取舍二：证据性质进读者实际读到的正文

**原取舍（C2 §5）**：证据性质只放结构对象（`question_assessments[*].rule.nature`），
"主文不加行"，理由同样是 PDF 分页。

**H2 判定**：性质必须出现在读者实际阅读的**页面/Markdown/PDF/ZIP**里，仅放结构字段不构成
完整交付。页面、PDF、ZIP 都与 Markdown 同源（`ReportViewer` 渲染 `/api/task/<id>/report.md`），
所以落在 Markdown 即四者齐备。

**改法（`render_brief_markdown`）**：把性质**加进既有那一行**（不新增正文行），两处：
- 『关键判断与下一步』：`- 依据：{类型}；覆盖：{覆盖}；{定位}；证据性质：{nature}`
- 『研究问题与下一步』：`- {材料}；证据性质：{nature}；边界：…`

**诚实性细节**：没有材料时写"证据性质：尚未取得可判定的证据"，**不**把"该类问题需要什么
性质的证据"冒充成"已有这种性质证据"（覆盖为 `none` 时不再照抄 `rule.nature`）。

**实测正文样例**（负基数冻结样本，逐字摘自渲染结果）：

```
- **归母净利润 2023 年 -10亿元 → 2024 年 8亿元**（完整读数见『关键发现』）
  - 材料：未取得对应披露，**观察成立、原因待证**；证据性质：尚未取得可判定的证据；
    边界：基期为负/为零时同比没有可比含义，本问**不给出百分比方向**，只按绝对额与
    由负转正/同负表述；…；说明：同比不可算（基期非正）**不构成删题理由**：…
```

（有材料的问则显示 `证据性质：计算自披露数值（非独立核实）` /
`证据性质：发行人口径的量化分解（未独立验证）`。）

## 3. 分页：**同一个冻结场景里验证"不删题也不出空白页"**

这是本次取舍能被反转的前提，因此单独验证：`test_offline_delivery`（冻结离线交付场景，
含 `pdf_parseable`＝无空白页判定、`images_embedded`、`table_columns_aligned`）：

```
python .tmp/run_tests.py test_offline_delivery
# Ran 34 tests OK（含 loss 场景：两处"基期为负"底稿问题 + 保留的负基数问题）
```

即：保留负基数问题**且**新增性质文字之后，PDF 仍未出现"<50 字符且无图"的空白页。
结论：原取舍的分页理由在本轮改动下不再成立（文案加在既有行内、未新增正文行）。

## 4. 补 C2 残留项：两次装配的结构级稳定性

C2 §5 记"两次装配稳定性只有评估级断言"。新增
`test_delivery_chain.TestResearchQuestions.test_two_consecutive_assemblies_are_stable`：
同一输入连续 `build_structure` 两次 → **正文逐字节一致**、问题（metric/observation/boundary）
一致、风险（kind/text）数量与类别一致、评估条数一致。

## 5. 验证（本会话实测）

| 套件 | 结果 |
|---|---|
| `test_delivery_chain.TestResearchQuestions` | **16 OK**（含四类适用性用例、负基数保留、性质可见、两次装配稳定） |
| `test_report_quality` + `test_question_assessment` + `test_narrative_evidence` | **140 OK** |
| `test_working_paper` + `test_facts` + `test_acceptance_adversarial` + `test_report_version` | **148 OK** |
| `test_review_edit_api`（编辑→新版本→交付/下载、旧批准不迁移） | **15 OK** |
| `test_offline_delivery`（冻结场景全链，含 PDF 可解析） | **34 OK** |

**被改动的断言（不是放宽判据）**：`test_negative_base_has_no_percentage_direction`
→ 更名 `test_negative_base_keeps_question_with_absolute_change`，把原"`net_profit` 不得出现
在问题里"反转为"必须出现，并加绝对变化/可比性限制/保留原因/分母=3 的断言；
`test_key_points_carry_evidence_nature` 从"只在结构里"扩展为"正文也必须能看到"。

## 6. 沙箱限制与本地验证脚手架（照实）

本会话沙箱把 `tempfile.mkdtemp()` 建的目录视为只读（mkdtemp 用 `mode=0o700`；
`os.mkdir` 用默认 mode 可写），仓库测试大量用 mkdtemp，直接跑会全部 `PermissionError`。
因此本地验证用一次性脚手架 `.tmp/run_tests.py`（**不进库**，`.tmp/` 已 gitignore）：
只在运行期把 `mkdtemp` 换成默认 mode 的等价实现，不改任何项目文件、不改测试语义。
CI（ubuntu）无此限制，按原样运行。

**未验**：
- 真实浏览器点击路径的页面渲染（本会话不登录、不伪造会话）；性质文字已确认在
  `report.md` 与结构对象里，页面/PDF/ZIP 与它同源。
- 第二次冻结场景全链的**真人**复核仍缺（属 C4 的退出条件）。
- 未跑全仓 47 个测试文件（按纪律只跑相关回归）；`test_delivery_chain` 全文件里有 8 例
  因环境失败（代码执行沙箱不可用、本机 Redis MISCONF 拒写、WinError 5 文件拒绝访问），
  与本批改动的代码路径无关，已在 H1 证据 §5/§6 记录同一环境缺陷。
