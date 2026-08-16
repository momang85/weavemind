# 织光 WeaveMind 标准化对照（对标《标准/》ACP 课程）

> 目的：把课程方法论固化为项目内可执行的标准，所有功能可追溯到对应章节与落地模块。

| 课程章节 | 标准要点 | 项目落地 | 验证方式 |
|---------|---------|---------|---------|
| C1 环境 | 依赖可复现、密钥不入库 | `requirements.lock`（make_lock.py 生成）、config.json/.env gitignore、`scripts/check_secrets.py` | CI secret scan |
| C2-2.3 提示词 | 六要素（目标/上下文/角色/受众/样例/输出格式） | `step_envelope.py` 步骤信封、`prompt_registry.py` 覆盖 | test_prompt_system |
| C2-2.4 评测 | Ragas 指标、专家 Ground Truth | `evals/`（案例集 + judge + 校准） | test_p0 / gate_ci |
| C2-2.5 RAG | 检索质量、数据溯源 | `chart_specs.py` 溯源校验、search 过滤 | test_delivery_chain |
| C3-3.2 规划反思 | Plan&Execute、自我/外部反馈 | planner 验收点、`validators/`、贯通测试、反思重做 | test_orchestrator_v2 |
| C3-3.4 记忆 | 短期/长期、主动记忆、治理 | `memory_manager.py`（Chroma + note/delete）、反思 memory_ops | test_p0 / API |
| C3-3.5 Skill | 触发条件/工作流/标准、渐进式披露、Skills-as-Code | `skills/` + `skill_registry.py`（applies 门控 + lessons 写回） | gate_ci / test_p0 |
| C3-3.6 评测驱动 | 端到端+白盒、LLM-as-Judge 校准 | 评测闸门（evals/drive.py）+ 黄金集校准 | 实跑回归 |
| C3-3.8 Harness | 标准→验证→记录→写回 | validators 注册表、反思教训写回 lessons | test_p0 |
| C4-4.3 生产 | 成本/性能/可观测 | `costs.py` 成本台账、metrics P95/失败率、滚动摘要、SLO 看板 | /api/metrics、/api/task/*/usage |
| C4-4.4 安全 | 注入防护、内容安全、权限 | `security.py`、`kb_access_control.py`、沙箱 `code_sandbox.py` | test_p0 / 实跑拦截 |

## 质量不变量（CI 强制）

`python -m evals.gate_ci`：评测集 schema 有效、skills ≥3 且带 description/applies、validators ≥4、judge 解析正常。
另有：`python scripts/check_secrets.py`（密钥零泄漏）、`python -m evals.run --dry-run`。

## 使用建议

- 新增能力时先写评测案例（`evals/cases/`），再实现，最后过 `gate_ci`。
- 风险操作步骤由规划器标 `mode: human_in_loop`，前端确认后执行。
- 反思产出自动写回 `skills/lessons.jsonl`（仅真实任务），注入后续同类步骤。
