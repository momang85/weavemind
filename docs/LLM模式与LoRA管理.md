# LLM 运行模式与 LoRA 管理（架构约束 v1）

> 本文档固化多 Worker LoRA 规模化的四条核心约束，任何新增 LoRA/Worker
> 必须遵守。违规修改会导致质量回退或架构混乱。

## 1. 运行模式（前端可切换）

| 模式 | 行为 | 适用场景 |
|---|---|---|
| `cloud` | 所有 Worker 走商业 API（DeepSeek-V4-flash 主 + SiliconFlow 备份） | 默认/稳定优先/质量敏感 |
| `hybrid` | 已蒸馏的 Worker（如 content_summary）优先走本地 QLoRA + LoRA，云端兜底 | 规模化降本/离线可用 |

- 切换入口：前端顶栏按钮（ModeToggle）；API：`GET/POST /api/llm-mode`
- 持久化：Redis `llm_mode`（worker 实时读）+ config.json `system.llm_mode`（重启生效）
- lora_client 每 2s 缓存模式，`cloud` 模式下探活直接 False（绝不走本地）

## 2. 四条硬约束

### ① 基座模型必须同一
所有 LoRA 必须在**同一个基座**上训练（当前 Qwen2.5-7B-Instruct）。
lora_serve 只加载一次 base，多个 adapter 共享——混用不同基座的 LoRA
会导致 adapter 加载失败或推理错乱。换基座 = 全部重新蒸馏训练。

### ② LoRA 目录规范 + 严禁混用
```
loras/                    ← 每个 Worker 一个目录
  summarizer/             ← content_summary（已上线）
  ranking/                ← 未来：finance_ranking
  code_gen/               ← 未来：code_execution
  relevance_judge/        ← 未来：检索相关性判定
```
- 每个目录 = 一个独立 adapter（adapter_model.safetensors + tokenizer + config）
- **部署目录只放推理必需文件**：删除 optimizer.pt/rng_state.pth 等训练残留（省 ~40MB）
- lora_servers.json 注册 `{worker名: {port, lora_path, system_prompt}}`，端口一一对应
- 不同 LoRA 绝不混用：请求按端口路由，生成前 set_adapter(worker)

### ③ Orchestrator / Critic 永不蒸馏
这两个模块需要最强的规划/推理/评审能力，7B 小模型无法达标；
且调用频率低，走商业 API 成本可忽略。**禁止**为 orchestrator/critic
创建或接入任何 LoRA（审查代码时检查：只有 content_summary 等执行型
Worker 允许 local_generate）。

### ④ 质量回归监控（必做）
切换小模型或用新 LoRA 前，必须跑同一批测试任务对比：
```bash
python eval_distill.py            # 本地 LoRA 评测（JSON 合规/来源纪律/图表合规/耗时）
# cloud vs hybrid 同批对比：用 distill_test_v2.jsonl 分别在两种模式下跑，
# 对比 成功率/来源标注率/图表合规率，任何指标下降 >10% 即回退。
```
验收器（acceptance_checker）是最终裁判：hybrid 模式任务若 SUCCESS_WITH_ISSUES
中的来源/溯源缺口显著高于 cloud 模式 → 该 Worker LoRA 质量不达标，回退 cloud。

## 3. 新增 Worker LoRA 流程（标准 4.1）

1. **蒸馏数据**：`distill_v2.py` 参数化 Worker 类型（teacher=智谱 glm-4-flash，
   强制 sources 字段 + schema 过滤 + 领域相关性校验），产出 `distill_{worker}.jsonl`
2. **训练**：`finetune_qlora.py --data ... --out loras/<worker>/`（4-bit + LoRA r=8）
3. **部署目录净化**：删除 optimizer/rng/scaler/scheduler/trainer_state/training_args
4. **注册**：lora_servers.json 加 `{worker: {port, lora_path}}`，重启 lora_serve
5. **质量门**：eval_distill.py 同批对比 + 真实任务验收对比，达标才 enabled=true
6. **灰度**：hybrid 模式下先小流量，验收器监控，无回退再放大

## 4. 当前状态（2026-09）

| Worker | LoRA | 数据 | 训练 | 端口 | 状态 |
|---|---|---|---|---|---|
| content_summary | loras/summarizer | 23 条 | 5ep loss 0.74 | 8765 | ✅ hybrid 可用 |
| finance_ranking | - | - | - | 8766(预留) | 待蒸馏 |
| code_execution | - | - | - | 8767(预留) | 待蒸馏 |
| relevance_judge | - | - | - | 8768(预留) | 待蒸馏 |
| orchestrator/critic | **永不 LoRA** | - | - | - | 商业 API |
