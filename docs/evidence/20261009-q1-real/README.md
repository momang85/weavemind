# 证据包：B1 批次（Q1-v1 真实数据运行）2026-10-09

架构师决策 10-08「立即开B1，不扩C」的交付：基于 `evals/a2_official_chain_20260929`
已准入的官方年报表抽取，构建可比年度链并运行冻结的 Q1-v1 机制评估。

## 输入（真实，非合成）

| 材料 | 公司 | 披露日 | 文本 sha256（前 12 位） | 计入 |
|---|---|---|---|---|
| 2023 年年度报告 | 洋河 002304 | 2024-04-27 | 48e137d4d0b9 | 全链 + 截止前链 |
| 2024 年年度报告 | 洋河 002304 | 2025-04-29 | a71287d53c7f | 全链 |
| 2025 年年度报告 | 洋河 002304 | 2026-04-28 | b01f7d7cf8cf | 全链 |
| 2024 年年度报告 | 三一 600031 | 2025-04-18 | aeaf2e836e2a | 全链 |

- 来源标签：`official_disclosure_cache`（A2 批次已准入的官方年报表抽取，**不是 Wind**）。
- 卡片「来源」行打印**提取文本** sha256；`projection_ledger.json` 另含原始 PDF 的
  `raw_sha256` 与目录哈希 `material_id`（doc_path 段），三者不同，勿混用。
- synthetic=false；单位：洋河「元」、三一「千元」（表头标注，×1000 一次换算，无二次缩放）。
- 四份文档均 entity_state=verified；事实 100/102/106/100 条 + 4 个派生毛利；无跨文档冲突。
- 上年数取本期报告比较列（每公司每年仅一份报告），重述未与原披露交叉验证（见 hand_calc.md §7）。

## 交付物

| 文件 | 内容 |
|---|---|
| `hand_calc.md` | **人工复算底稿**：打印值抄录、手工 E/O/Y_op、组成表恒等式（残差 0）、与流水线逐项对照 |
| `mechanism_cards.md` | 两张机制卡（002304 / 600031），历史重建 + usage_state 块 |
| `mechanism_evaluation.md` | 6 案例 / 2 公司评估：Q1 规则 1 命中 / 1 误判 / 2 弃权；结论 **no_evidence** |
| `research.md` | 研究正文（口径、读数、判定、局限） |
| `financials_projection.json` | 显式投影为原 preview 可读行（9.8KB；这不是第二套导入器） |
| `projection_ledger.json` | 每行 material_id / url / sha256 / locator / unit_check / 跨文档一致 |
| `mechanisms.json` / `mechanism_evaluation.json` | 机器读数与评估（schema `weavemind.q1_readings/1`、`weavemind.q1_evaluation/1`） |
| `study_manifest_full.json` / `study_manifest_pit.json` | 全链与截止前（PIT）两个 study 的清单 |
| `verify_replay_full.json` / `verify_replay_pit.json` | 六项校验（verify/replay × 全链/PIT × 常规/Q1）全部通过 |
| `run_summary.json` | 一次运行的摘要（study id、verdict、校验布尔值） |

## 核心读数（与 hand_calc.md 一致）

- 洋河：O_2023 = +1.7206% → 实际 −10.4673%（误判）；O_2024 = −10.4673% → 实际 −19.2394%（命中）；
  O_2025 = −19.2394% → 待结果。组成表主驱动：2024 毛利 −38.01 亿；2025 毛利 −73.70 亿、税金 +14.08 亿。
- 三一：O_2024 = +1.5768% → 2025 年报不在缓存 → 待结果；组成表主驱动：毛利 +13.86 亿、研发 +4.84 亿。
- 评估：split_at=2025-04-30；exploration 桶（洋河 t=2022 正、t=2023 负、三一 t=2023 正 → 2正/1负）
  exploration-majority 平局 → 基准弃权；结论 no_evidence（可评样本少 + 窗口重叠共享 1 家公司）——**预期内**。

## PIT 排除证明（防未来信息泄漏）

PIT study 用 cutoff=2024-04-27 重跑：洋河仅保留 2022/2023（2024/2025 全部 30 行以
「信息在本次截止之后才可用」排除），三一 0 行可见；PIT 读数里 t=2023 的 Y_op
state=pending（"下一同口径年度尚未取得（待数据），不填零"）——**没有任何 2024/2025 回填**。

## 复现

```
C:\Python314\python.exe scripts\q1_real_study.py --evidence docs/evidence/20261009-q1-real
# 可选参数：--out tmp/research-studies --inputs tmp/q1-real-inputs --cutoff 2026-10-09 --split-at 2025-04-30
```

纯模块：`quant_research/mechanisms.py`（读数/组成/卡）+ `quant_research/evaluate_mechanisms.py`
（案例/判定/follow-up）；脚本是一次性显式投影 + 流水线（projection_version=q1_projection/1.0.0）。
重复运行会生成新的 study 目录（内容确定、id 稳定）；manifest `q1.module_hashes`
锁定两个纯模块的内容哈希，改动模块后哈希即失效。

## 研究状态与复核状态（严格分离）

- 研究状态：research_draft；机器验收：六项校验通过；**人工复核：未完成（unreviewed）**。
- Web UI 上的同版研究显示「待验证」徽标：登录不可用（无固定默认管理员密码），
  点击验证未完成——UI 观察不阻塞本批，不构成复核通过。
- 对外交付不允许；无 alpha 声明；已见结果一律历史重建。

## Seam ②（措辞修正，3 个架构师未跟踪文件，本批未提交）

1. `adapters/wind_import.py:551` — 来源标注回退 `"来源未标注（未指定数据来源）"`（不再写 "Wind"）。
2. `quant_research/study.py:123` — 同上回退。
3. `quant_research/study.py:249` — 同上回退；前瞻卡确认措辞不改（人工复核红线）。

## 明确不做

无行情/价格领域扩展；无信用训练标签或合成信用评分；无新服务/DB/SDK/DSL；
无付费重新获取；旧 93 项 / thousand-item / CI 用例不重跑。
