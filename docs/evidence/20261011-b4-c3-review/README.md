# C3 复核证据：研究实验台——现金反转条件＋一次披露更新（2026-10-11）

架构师裁定（`docs/总架构师执行指令.md` L11，受保护只读）：**唯一当前行动 C3「研究实验台」——现金反转条件＋一次披露更新**（约 80% 先产新功能，≤20% 必要接缝）。本目录只放本批定向复核的机器证据；**人工复核仍由人做，代理不代签**（`research_state=not_applicable`、`human_review=未复核`、`external_delivery.allowed=false`）。

## 怎么跑

```
python -X utf8 docs/evidence/20261011-b4-c3-review/c3_review.py
```

探针只读两份本批候选包、两个研究目录、两份 C2e 基线包与一份 C2c 基线包；**不重跑旧 28/13/57/258 项、全量套件、CI、付费链**，输出 `results.json`（**37 项检查：三一 13 ＋ 洋河 17 ＋ 纯函数护栏 7，全部通过**）与 `review_fingerprints.json`（关键文件/产物/包的 sha256、旧包字节不变性、未验证清单）。

## 本批产物

| 项 | 值 |
|---|---|
| 三一研究（沿用 C2e，同版追加） | `4dba42a6bdde92276c4dabe428e9e4e77c1a31d39f3959b8631e0f6061ae8548` |
| 三一实验台 JSON（研究详情可读） | `tmp/research-studies/4dba42a6bdde9227/c3_experiment_600031.SH.json`，sha256 `101dca96…` |
| 三一最终包 | `deliverables_20261011_183959_f38391.zip`，version `52828ee6…`，sha256 `d443e59c0201fea5d00630f67efc87af2c70778bb25941f8c5e2ee0087ece437`，5,210,401 B |
| 洋河研究（沿用 C2e，同版追加） | `0af20919e6245b888974ee17321f327f99580ab745687e80aca6dae6921e7f4f` |
| 洋河实验台 JSON | `tmp/research-studies/0af20919e6245b88/c3_experiment_002304.SZ.json`，sha256 `2c4451fc…` |
| 洋河最终包 | `deliverables_20261011_184706_13b232.zip`，version `e758867e…`，sha256 `c06a409b6f80f5fce8f3f9a9f3138cbe6f0610c7a22c197dbf51ef925a92f7a2`，5,263,914 B |
| 一页研究行动（四格＋机制队列＋披露更新） | `c3_action_page.md`（14,857 B）／`c3_action_page.pdf`（5,311,261 B） |
| 新功能（计算层） | `financial_analysis/operators/cash_reconciliation.reversion_ladder`（纯函数，见 `scripts/c3_experiment.py` 调用） |
| 装配层（薄接缝） | `research_report_adapter.py`：正文前置节、机制节、`company_view.c3_experiment`、随包附件 `analysis/c3_experiment_<code>.md` |

包内同一结果的三个读口：`reports/report.md`（前置节/机制节/披露更新）、`analysis/context.json` 的 `company_view.c3_experiment`（候选回撤项与 0/50/100%）、`analysis/c3_experiment_<code>.md`（同一 JSON 确定性渲染）。

## 四格精确值（其他项固定会计反事实；非预测/因果）

| 公司 | 视角 | C（亿） | 回撤项（会计位置） | λ=0 | λ=50% | λ=100% | λ*=C/a | 域内可达 |
|---|---|---|---|---|---|---|---|---|
| 三一重工 | 本期额 | 148.142780000 | 经营性应付项目的增加 | 148.142780000 | 120.178510000 | **92.214240000** | 264.8787% | 否（范围不可达） |
| 三一重工 | 同比贡献 | 91.060580000 | 经营性应付项目的增加 | 91.060580000 | 32.994655000 | **−25.071270000** | 78.4114% | 是 |
| 洋河股份 | 本期额 | −7.630403268 | 经营性应付项目的增加 | −7.630403268 | 15.161578211 | **37.953559689** | 16.7392% | 是（现金归零） |
| 洋河股份 | 同比贡献 | −53.917515641 | 合并净利润变化 | −53.917515641 | −31.540173348 | **−9.162831056** | 120.4735% | 否（范围不可达） |

- 与 `docs/evidence/20261011-c2e-accept/innovation_reference.json` 的精确输入逐位一致（探针按 Decimal 精确比较，非四舍五入）。
- run 身份：三一主 run `5d4b99ccb64c21fa…`（冻结 runs）；洋河续页 `370dba92a40e36a3…`（`realization.cash_run_ref`）——不取列表首个 cash/default 年份。
- 护栏（探针 7 项）：未知视角／λ 越域／指定项缺项／全零项（无同向项）／无诊断块 → 一律 `applicable=False` ＋原因，**不造数**；同一 run 两次调用逐字相同（冻结重读）；λ* 与 C/a 精确一致（容差 1e-20）。

## 机制队列（5 条实卡 ＋ 1 条 pending）

| 公司 | 机制卡 | 状态 | 窗口／下一资料 |
|---|---|---|---|
| 洋河股份 | 利润收缩中的税费缓冲仍存在 | 加强 | 2025H1；年报 `ee4429ee…` 只进年度后续 |
| 洋河股份 | 销量压力延续（数量收缩未转正） | 加强 | H1 销量同比 −32.35% |
| 洋河股份 | 企业成品库存继续累积（未被消化） | 削弱 | H1 库存量同比 −33.96%（企业口径） |
| 洋河股份 | 现金来源的方向延续（利润主导） | 加强 | 本期同口径仍利润项主导 |
| 洋河股份 | 单位收入（推算吨价）不作为定价证据 | 削弱 | 推算吨价 −4.50%（同口径齐备才入卡） |
| 三一重工 | 营运资本主导的现金改善可持续性 | pending | 2025 年报现金流量表补充资料未取得，不凑成功 |

缺数据/口径边界不入卡；不为凑「成果/胜率」新增成功或证伪；此前队列的缺失项仍按缺失登记。

## 一次真实披露更新（历史重建登记）

- 旧截止 `2025-04-30`（洋河 2024 年报 `f42e747c73850b59`，2025-04-29）→ 新截止 `2025-08-19`（2025 半年报 `2ce860dddd94e5a6`）；`historical_rebuild=True`，`based_on` 为同研究已登记版本（非事前声明）。
- 复用 `financial_analysis.continuation.snapshot/compare`：H1 内同比（2025H1 对 2024H1，**不年度相减、不乘二**）——销量 **−32.35%** 支持「收缩延续」、企业成品库存 **−33.96%** 削弱「累积延续」。
- 2026-04-28 年报 `ee4429ee…` 只进**年度后续**对照，不入本窗口。

## 旧包保留（字节不变）

| 基线包 | sha256 | 状态 |
|---|---|---|
| C2e 三一 `…_171519_344cb4.zip` | `d6d6b7de…` | 不变 |
| C2e 洋河 `…_171523_8c5000.zip` | `6d8c3914…` | 不变 |
| C2c `…_142232_080384.zip` | `f79861e7…` | 不变 |

## 如实未达 / 需人工

- **GUI 实机复核未做**：weavemind 服务栈当前未运行（8080 被非本项目进程占用、无 python/redis），无可用登录会话；未猜密码、未起新服务、未杀无关进程。
- **PDF 像素级目视**需人工或可读图模型；本批只核文本层（四格/机制卡/更新段在场与数值）。
- **研究详情前端交互渲染本轮未做**：同一份 JSON 已落在研究目录（现有文件清单 API 可读）并随包（`company_view.c3_experiment` ＋ 附件），前端渲染按「不重构巨文件/不加服务」留待后续。
- **三一 `183959_f38391` 包内附件为 CRLF**（其交付早于附件 LF 落盘修复），与研究目录同一 JSON 的现渲染**逐行一致**（探针按行规范化比较通过，字节差仅换行符）；洋河最终包已为 LF。
- **经济因果 / 供应商条款**：本轮明示**不能判断**（回撤项只是会计位置的反事实），不因无法证明条款而标证伪。

## 本批必要文件（改动全部未提交，HEAD `c85371e`）

`financial_analysis/operators/cash_reconciliation.py`（`reversion_ladder`）、`scripts/c3_experiment.py`（装配＋一页行动页）、`research_report_adapter.py`（前置节/机制节/详情视图/附件入包）、`tmp/research-studies/<sid>/c3_experiment_<code>.json`、`docs/evidence/20261011-b4-c3-review/`（本目录）；一次性脚本 `tmp/_c3_deliver.py`、`tmp/_c3_probe1.py`、`tmp/_c3_fingerprints.py`、`tmp/_c3_final_check.py`。受保护只读文件（`docs/总架构师执行指令.md`、`docs/三域金融研究架构规划_20261005.md`、`acceptance_checker.py`、`delivery_pipeline.py` 等）本批未改。
