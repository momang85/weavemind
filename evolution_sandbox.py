"""
织光 (ZhiGuang) — 策略演化沙箱 (EvolutionSandbox)

达尔文式策略优化系统：
1. 变异工厂：从基准策略生成 3 个变体
2. 竞技场：Docker 沙箱中并行测试，LLM 裁判排序
3. 安全门：连续 3 轮稳定第一 + 红线检测 + 人工审核
4. 自动部署：优胜策略发布到 registry

安全原则：
    - 任何自动化部署都需要人工审核确认
    - 触发红线的变体立即标记"有毒"永不录用
    - 资源限制通过 Docker 强制执行
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from common import MessagingClient, AgentRegistry
from llm_client import call_llm, LLMCallError

logger = logging.getLogger(__name__)

# 待人工审批的部署请求（Redis List）；批准后写入 active 策略键
PENDING_KEY = "evolution:pending"
ACTIVE_KEY_PREFIX = "strategy:active:"

# ---------------------------------------------------------------------------
# 策略数据结构
# ---------------------------------------------------------------------------


@dataclass
class StrategyConfig:
    """策略参数化配置。

    Attributes:
        strategy_id: 策略唯一标识。
        agent_type: 对应的智能体类型（如 "search_agent"）。
        temperature: LLM temperature (0.1-1.0)。
        max_sources: 最大数据源数量。
        summarization_prompt: 总结提示词模板。
        filter_rules: 过滤规则列表。
        generation: 演化代数。
        parent_id: 父策略 ID。
    """

    strategy_id: str
    agent_type: str
    temperature: float = 0.7
    max_sources: int = 5
    summarization_prompt: str = "用中文三句话总结"
    filter_rules: list[str] = field(default_factory=list)
    generation: int = 0
    parent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "agent_type": self.agent_type,
            "temperature": self.temperature,
            "max_sources": self.max_sources,
            "summarization_prompt": self.summarization_prompt,
            "filter_rules": self.filter_rules,
            "generation": self.generation,
            "parent_id": self.parent_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StrategyConfig":
        return cls(
            strategy_id=data.get("strategy_id", f"strategy-{_now_epoch()}"),
            agent_type=data.get("agent_type", "search_agent"),
            temperature=data.get("temperature", 0.7),
            max_sources=data.get("max_sources", 5),
            summarization_prompt=data.get("summarization_prompt", "用中文三句话总结"),
            filter_rules=data.get("filter_rules", []),
            generation=data.get("generation", 0),
            parent_id=data.get("parent_id"),
        )


# 默认基准策略
DEFAULT_STRATEGY = StrategyConfig(
    strategy_id="baseline-v1",
    agent_type="search_agent",
    temperature=0.7,
    max_sources=5,
    summarization_prompt="用中文三句话总结",
    filter_rules=["排除社交媒体", "优先官方站点"],
    generation=0,
)


# ============================================================================
# 演化沙箱
# ============================================================================


class EvolutionSandbox:
    """策略演化沙箱：变异 → 竞技 → 安全门 → 部署。

    Usage:
        sandbox = EvolutionSandbox(messaging, registry)
        result = sandbox.evolve("search_agent", baseline_strategy)
    """

    # 变异数量
    NUM_VARIANTS = 3
    # 每轮测试任务数
    TASKS_PER_ROUND = 5
    # 稳定所需的连续胜出轮数
    STABILITY_ROUNDS = 3
    # 红线：超过此倍数的 token 消耗视为有毒
    TOKEN_REDLINE_RATIO = 10.0
    # 有毒策略存储
    POISON_FILE = "poison_strategies.json"

    # 锦标赛裁判系统提示词
    JUDGE_SYSTEM = """你是精通A/B测试和增长黑客的技术策略师。
对策略变体的输出进行排序，不仅看结果好坏，还要分析策略的创新性和泛化潜力。

## 裁判维度
1. 信息完整性：是否包含了所需关键信息
2. 表达清晰度：语言是否简洁准确
3. 可操作性：输出是否可直接使用
4. 创新亮点：是否有出人意料的优秀处理方式

## 输出格式
{
  "ranking": ["variant_B", "variant_A", "variant_C"],
  "scores": {"variant_A": 8.5, "variant_B": 9.2, "variant_C": 6.0},
  "best": "variant_B",
  "best_innovation": "variant_B 的什么具体做法值得推广",
  "reason": "一句话原因"
}

只输出JSON。"""

    def __init__(
        self,
        messaging: MessagingClient,
        registry: AgentRegistry,
        docker_image: str = "zhiguang-worker:latest",
    ) -> None:
        """初始化演化沙箱。

        Args:
            messaging: 消息客户端。
            registry: 能力注册表。
            docker_image: Worker Docker 镜像名。
        """
        self._messaging = messaging
        self._registry = registry
        self._docker_image = docker_image

        # 加载有毒策略黑名单
        self._poison_list: set[str] = self._load_poison_list()

        # 运行状态
        self._running = False
        self._shutting_down = False

        logger.info(
            "EvolutionSandbox initialized (image=%s, %d poisoned strategies)",
            docker_image,
            len(self._poison_list),
        )

    # ==================================================================
    # 主入口：完整演化流程
    # ==================================================================

    def evolve(
        self,
        agent_type: str,
        base_strategy: StrategyConfig | None = None,
        test_tasks: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """执行完整的一轮演化：变异 → 竞技 → 安全门 → 部署。

        Args:
            agent_type: 智能体类型。
            base_strategy: 基准策略，默认使用 DEFAULT_STRATEGY。
            test_tasks: 测试任务列表，默认使用内置标准任务。

        Returns:
            演化结果字典：
            {
                "winner": StrategyConfig | None,
                "stable": bool,
                "deployed": bool,
                "rounds": [...],
                "summary": str,
            }
        """
        base = base_strategy or DEFAULT_STRATEGY
        test_tasks = test_tasks or self._default_test_tasks()

        logger.info(
            "=== Evolution Round for '%s' (base: %s) ===",
            agent_type,
            base.strategy_id,
        )

        # Phase 1: 变异
        variants = self.mutate(base)
        logger.info(
            "Generated %d variants from %s: %s",
            len(variants),
            base.strategy_id,
            [v.strategy_id for v in variants],
        )

        # Phase 2: 竞技场
        tournament_result = self.run_tournament(agent_type, variants, test_tasks)
        winner_id = tournament_result.get("winner")
        rankings = tournament_result.get("rankings", [])

        if winner_id is None:
            logger.warning("Tournament produced no winner")
            return _make_result(None, False, False, [], "未能产生优胜者")

        winner = next((v for v in variants if v.strategy_id == winner_id), None)
        logger.info("Tournament winner: %s", winner_id)

        # Phase 3: 检查稳定性（需要连续 3 轮第一）
        #  简化版：单轮胜出即返回，稳定性检查由外部循环负责
        stable = self._check_continuous_wins(winner_id, rankings)

        # Phase 4: 安全门 → 自动部署
        deployed = False
        if stable and winner is not None:
            deployed = self._safety_gate_and_deploy(winner)

        summary = (
            f"演化完成: 胜者={winner_id}, 稳定={stable}, 已部署={deployed}"
        )
        logger.info(summary)

        return _make_result(
            winner, stable, deployed, rankings, summary,
            scoreboard=tournament_result.get("scoreboard"),
            win_counts=tournament_result.get("win_counts"),
        )

    # ==================================================================
    # 1. 变异工厂
    # ==================================================================

    def mutate(self, base: StrategyConfig) -> list[StrategyConfig]:
        """从基准策略生成 3 个变体。

        Args:
            base: 基准策略配置。

        Returns:
            3 个变异策略的列表。
        """
        gen = base.generation + 1
        variants: list[StrategyConfig] = []

        # 变体 A: temperature 随机扰动
        variant_a = deepcopy(base)
        variant_a.strategy_id = f"{base.strategy_id}-gen{gen}-A"
        variant_a.parent_id = base.strategy_id
        variant_a.generation = gen
        variant_a.temperature = round(
            max(0.1, min(1.0, base.temperature + random.uniform(-0.3, 0.3))), 2
        )
        variants.append(variant_a)

        # 变体 B: filter_rules 增删改
        variant_b = deepcopy(base)
        variant_b.strategy_id = f"{base.strategy_id}-gen{gen}-B"
        variant_b.parent_id = base.strategy_id
        variant_b.generation = gen
        variant_b.filter_rules = self._mutate_filter_rules(base.filter_rules)
        variants.append(variant_b)

        # 变体 C: 修改 summarization_prompt（调用 LLM）
        variant_c = deepcopy(base)
        variant_c.strategy_id = f"{base.strategy_id}-gen{gen}-C"
        variant_c.parent_id = base.strategy_id
        variant_c.generation = gen
        variant_c.summarization_prompt = self._mutate_prompt(base.summarization_prompt)
        variants.append(variant_c)

        return variants

    def _mutate_filter_rules(self, rules: list[str]) -> list[str]:
        """随机变异过滤规则：增加/删除/修改。

        Args:
            rules: 原始规则列表。

        Returns:
            变异后的规则列表。
        """
        rules = list(rules)  # 拷贝
        action = random.choice(["add", "remove", "modify"])

        candidate_additions = [
            "优先官方站点",
            "排除社交媒体",
            "排除论坛",
            "优先学术来源",
            "要求时效性在1年内",
            "排除广告内容",
        ]

        if action == "add" and rules:
            candidates = [r for r in candidate_additions if r not in rules]
            if candidates:
                rules.append(random.choice(candidates))
        elif action == "remove" and len(rules) > 1:
            rules.pop(random.randint(0, len(rules) - 1))
        elif action == "modify" and rules:
            idx = random.randint(0, len(rules) - 1)
            replacements = [r for r in candidate_additions if r != rules[idx]]
            if replacements:
                rules[idx] = random.choice(replacements)

        return rules

    def _mutate_prompt(self, current_prompt: str) -> str:
        """调用 LLM 生成有意义的策略变体（非随机，而是有方向的设计选择）。

        Args:
            current_prompt: 当前提示词。

        Returns:
            变异后的提示词。
        """
        system = (
            "你是技术策略创新的专家。给定一个基础策略，生成一个有显著设计差异的变体。"
            "不是随机扰动，而是有明确的设计方向。例如："
            "- 极致性能：用更轻量的替代方案"
            "- 视觉冲击：增加创意UI元素"
            "- 交互革新：引入新的交互模式"
            "输出纯文本，给出新策略的简短描述，不超过200字。不要JSON。"
        )
        user = (
            f"原始提示词: {current_prompt}\n\n"
            "请生成一个风格迥异的等价版本。例如："
            "'用中文三句话总结' → '提炼关键词并以表格呈现'"
            "或 '保持原文风格进行摘要' → '提取核心观点并标注置信度'。"
        )

        try:
            result = call_llm(system, user, expect_json=False)
            new_prompt = result.get("content", current_prompt).strip()
            if len(new_prompt) > 200:
                new_prompt = new_prompt[:200]
            logger.info("Prompt mutated: '%s' → '%s'", current_prompt[:40], new_prompt[:40])
            return new_prompt
        except Exception as exc:
            logger.warning("Prompt mutation failed, keeping original: %s", exc)
            return current_prompt

    # ==================================================================
    # 2. 竞技场
    # ==================================================================

    def run_tournament(
        self,
        agent_type: str,
        variants: list[StrategyConfig],
        test_tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """并行测试所有变体，LLM 裁判排序。

        实际执行时通过 Docker 容器运行变体 Worker；
        当前为模拟模式，直接调用 LLM 模拟各变体输出。

        Args:
            agent_type: 智能体类型。
            variants: 策略变体列表。
            test_tasks: 标准化测试任务。

        Returns:
            {"winner": "variant_id", "rankings": [...]}
        """
        logger.info(
            "Tournament: %d variants × %d tasks for '%s'",
            len(variants),
            len(test_tasks),
            agent_type,
        )

        # 过滤有毒变体
        clean_variants = [
            v for v in variants if v.strategy_id not in self._poison_list
        ]
        if len(clean_variants) < len(variants):
            logger.warning(
                "Excluded %d poisoned variants", len(variants) - len(clean_variants)
            )

        if not clean_variants:
            return {"winner": None, "rankings": [], "error": "所有变体已被标记为有毒"}

        # 对每个任务，并行执行所有变体
        all_results: list[dict[str, Any]] = []

        for task_idx, task in enumerate(test_tasks[: self.TASKS_PER_ROUND]):
            logger.info("Tournament task %d/%d", task_idx + 1, min(len(test_tasks), self.TASKS_PER_ROUND))

            task_outputs: dict[str, str] = {}
            task_redlines: list[str] = []

            for variant in clean_variants:
                try:
                    output = self._execute_variant(variant, task)
                    task_outputs[variant.strategy_id] = output
                except RedLineViolation as exc:
                    logger.error(
                        "Variant %s triggered RED LINE: %s", variant.strategy_id, exc
                    )
                    task_redlines.append(variant.strategy_id)
                    self._mark_poison(variant.strategy_id)
                except Exception as exc:
                    logger.error(
                        "Variant %s execution failed: %s", variant.strategy_id, exc
                    )
                    task_outputs[variant.strategy_id] = f"[执行失败] {exc}"

            # 移除触发红线的变体
            for rid in task_redlines:
                clean_variants = [v for v in clean_variants if v.strategy_id != rid]

            # LLM 裁判排序
            if len(task_outputs) >= 2:
                ranking = self._judge_outputs(task, task_outputs)
                all_results.append(ranking)
            elif len(task_outputs) == 1:
                vid = list(task_outputs.keys())[0]
                all_results.append({
                    "task": task.get("instruction", "")[:50],
                    "winner": vid,
                    "ranking": [vid],
                })

        # 综合排名：统计每个变体的胜出次数
        win_counts: dict[str, int] = {}
        for r in all_results:
            winner = r.get("winner", "")
            if winner:
                win_counts[winner] = win_counts.get(winner, 0) + 1

        # 总分 = 排名积分（第1名3分，第2名2分，第3名1分）
        score_board: dict[str, float] = {v.strategy_id: 0.0 for v in clean_variants}
        for r in all_results:
            ranking = r.get("ranking", [])
            for rank, vid in enumerate(ranking):
                points = max(0, len(ranking) - rank)
                score_board[vid] = score_board.get(vid, 0) + points

        # 得分最高者胜出
        winner_id = max(score_board, key=score_board.get) if score_board else None

        logger.info(
            "Tournament complete. Scoreboard: %s. Winner: %s",
            score_board,
            winner_id,
        )

        return {
            "winner": winner_id,
            "scoreboard": score_board,
            "win_counts": win_counts,
            "rankings": all_results,
            "task_count": len(all_results),
        }

    def _execute_variant(
        self, variant: StrategyConfig, task: dict[str, Any]
    ) -> str:
        """在沙箱中执行某个变体策略。

        当前为模拟模式：用 LLM 模拟变体策略的输出。

        Args:
            variant: 策略配置。
            task: 测试任务 {"instruction": "..."}。

        Returns:
            策略执行输出字符串。

        Raises:
            RedLineViolation: 触发红线。
        """
        instruction = task.get("instruction", "搜索")

        # 构建模拟提示词：让 LLM 模拟不同策略下的输出风格
        system = (
            f"你正在模拟一个搜索智能体。你的策略参数如下：\n"
            f"- temperature: {variant.temperature}\n"
            f"- max_sources: {variant.max_sources}\n"
            f"- summarization_prompt: {variant.summarization_prompt}\n"
            f"- filter_rules: {variant.filter_rules}\n\n"
            f"请根据这些参数执行任务。如果过滤规则要求排除某些内容，请遵守。"
        )

        try:
            result = call_llm(system, instruction, expect_json=False)
            output = result.get("content", "")

            # 红线检测
            self._check_redlines(output, variant)

            return output
        except RedLineViolation:
            raise
        except LLMCallError as exc:
            raise RuntimeError(f"LLM call failed: {exc}") from exc

    def _check_redlines(self, output: str, variant: StrategyConfig) -> None:
        """对输出执行红线检测。

        Args:
            output: 策略输出字符串。
            variant: 策略配置（用于记录）。

        Raises:
            RedLineViolation: 触发红线。
        """
        # 红线1: 不可解析的乱码（中文字符占比 < 10% 且长度 > 100）
        if len(output) > 100:
            chinese_chars = sum(1 for c in output if '\u4e00' <= c <= '\u9fff')
            ratio = chinese_chars / len(output) if output else 0
            if ratio < 0.1:
                raise RedLineViolation(
                    variant.strategy_id,
                    f"乱码检测: 中文字符占比 {ratio:.2%} < 10%",
                    severity="HIGH",
                )

    # ==================================================================
    # 3. 裁判排序
    # ==================================================================

    def _judge_outputs(
        self,
        task: dict[str, Any],
        outputs: dict[str, str],
    ) -> dict[str, Any]:
        """调用 LLM 对多个变体的输出进行排序。

        Args:
            task: 测试任务。
            outputs: {variant_id: output_text} 映射。

        Returns:
            排序结果字典。
        """
        task_desc = task.get("instruction", "未知任务")

        # 构建比较文本
        comparison = f"## 任务\n{task_desc}\n\n## 各变体输出\n\n"
        for vid, output in outputs.items():
            comparison += f"### {vid}\n{output[:500]}\n\n"

        try:
            result = call_llm(self.JUDGE_SYSTEM, comparison, expect_json=True)
            result["task"] = task_desc[:50]
            # 过滤评审虚构的 variant id，只保留真实变体（防止胜者匹配失败）
            valid = set(outputs.keys())
            ranking = [r for r in result.get("ranking", []) if r in valid]
            if not ranking:
                ranking = list(valid)
            winner = result.get("winner")
            if winner not in valid:
                winner = ranking[0] if ranking else None
            scores = result.get("scores") or {}
            if isinstance(scores, dict):
                scores = {k: v for k, v in scores.items() if k in valid}
            result["ranking"] = ranking
            result["winner"] = winner
            result["scores"] = scores
            return result
        except Exception as exc:
            logger.warning("Judge LLM call failed: %s, using random ranking", exc)
            # 回退：随机排序
            ids = list(outputs.keys())
            random.shuffle(ids)
            return {
                "task": task_desc[:50],
                "winner": ids[0] if ids else None,
                "ranking": ids,
                "scores": {vid: 5.0 for vid in ids},
                "reason": "裁判不可用，随机排序",
            }

    # ==================================================================
    # 4. 安全门与自动部署
    # ==================================================================

    def _check_continuous_wins(
        self, winner_id: str, rankings: list[dict[str, Any]]
    ) -> bool:
        """检查优胜策略是否在最近几轮中连续胜出。

        简化实现：检查本轮是否在所有任务中获最高总分。
        完整版需要外部维护历史记录。

        Args:
            winner_id: 胜者 ID。
            rankings: 本轮排序结果。

        Returns:
            是否满足稳定性要求。
        """
        if not rankings or not winner_id:
            return False

        # 统计 winner 在各任务中的排名
        first_place_count = 0
        for r in rankings:
            ranking = r.get("ranking", [])
            if ranking and ranking[0] == winner_id:
                first_place_count += 1

        # 简化：超过半数任务排第一即视为稳定
        stable = first_place_count >= len(rankings) / 2
        logger.info(
            "Stability check: %s wins %d/%d tasks → %s",
            winner_id,
            first_place_count,
            len(rankings),
            "STABLE" if stable else "UNSTABLE",
        )
        return stable

    def _safety_gate_and_deploy(self, winner: StrategyConfig) -> bool:
        """安全检查 + 自动部署。

        部署前要求人工审核。发布策略配置到 Redis 频道。

        Args:
            winner: 优胜策略。

        Returns:
            是否成功部署。
        """
        # 检查有毒名单
        if winner.strategy_id in self._poison_list:
            logger.error("Winner %s is in poison list, refusing to deploy", winner.strategy_id)
            return False

        # 检查安全约束
        if winner.temperature > 0.95:
            logger.warning(
                "Winner temperature %.2f too high, capping to 0.9", winner.temperature
            )
            winner.temperature = 0.9

        try:
            payload = winner.to_dict()
            payload["timestamp"] = _now_iso()
            payload["status"] = "pending"
            # 持久化到待审批列表（web 控制台读取并展示审批按钮）
            self._messaging.redis.rpush(
                PENDING_KEY, json.dumps(payload, ensure_ascii=False)
            )
            # 兼容通知：仍发布到能力更新频道
            self._messaging.publish("registry.capability.update", {
                "type": "strategy_deploy_request",
                "strategy": payload,
                "timestamp": payload["timestamp"],
                "requires_approval": True,
            })
            logger.info(
                "Strategy '%s' deployment REQUEST published (awaiting approval). "
                "Strategy: temp=%.2f, prompt='%s', rules=%s",
                winner.strategy_id,
                winner.temperature,
                winner.summarization_prompt[:40],
                winner.filter_rules,
            )
            return True
        except Exception as exc:
            logger.error("Failed to publish deployment request: %s", exc)
            return False

    # ==================================================================
    # 有毒策略管理
    # ==================================================================

    def _mark_poison(self, strategy_id: str) -> None:
        """将策略标记为有毒，永久拉黑。

        Args:
            strategy_id: 策略 ID。
        """
        self._poison_list.add(strategy_id)
        self._save_poison_list()
        logger.warning("Strategy '%s' marked as POISON (permanent ban)", strategy_id)

    def _load_poison_list(self) -> set[str]:
        """加载有毒策略黑名单。"""
        try:
            if os.path.exists(self.POISON_FILE):
                with open(self.POISON_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return set(data.get("poisoned", []))
        except Exception as exc:
            logger.warning("Failed to load poison list: %s", exc)
        return set()

    def _save_poison_list(self) -> None:
        """保存有毒策略黑名单。"""
        try:
            with open(self.POISON_FILE, "w", encoding="utf-8") as f:
                json.dump({"poisoned": sorted(self._poison_list)}, f, indent=2)
        except Exception as exc:
            logger.error("Failed to save poison list: %s", exc)

    # ==================================================================
    # 测试任务
    # ==================================================================

    def _default_test_tasks(self) -> list[dict[str, Any]]:
        """内置标准化测试任务（从 successful_strategies 抽象而来）。"""
        return [
            {"instruction": "搜索2024年人工智能最新进展", "expected_capability": "web_search"},
            {"instruction": "查找深度学习在医疗领域的应用案例", "expected_capability": "web_search"},
            {"instruction": "搜索量子计算与AI结合的最新研究", "expected_capability": "web_search"},
            {"instruction": "查找自动驾驶技术2024年突破", "expected_capability": "web_search"},
            {"instruction": "搜索大语言模型安全对齐的最新方法", "expected_capability": "web_search"},
        ]


# ============================================================================
# 异常定义
# ============================================================================


class RedLineViolation(Exception):
    """红线违规异常 — 触发立即拉黑。"""

    def __init__(self, strategy_id: str, reason: str, severity: str = "HIGH") -> None:
        self.strategy_id = strategy_id
        self.severity = severity
        super().__init__(f"RED LINE: {strategy_id} - {reason}")


# ============================================================================
# 辅助函数
# ============================================================================


def _make_result(
    winner: StrategyConfig | None,
    stable: bool,
    deployed: bool,
    rankings: list,
    summary: str,
    scoreboard: dict | None = None,
    win_counts: dict | None = None,
) -> dict:
    return {
        "winner": winner.to_dict() if winner else None,
        "stable": stable,
        "deployed": deployed,
        "rankings": rankings,
        "scoreboard": scoreboard or {},
        "win_counts": win_counts or {},
        "summary": summary,
        "timestamp": _now_iso(),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_epoch() -> str:
    return str(int(time.time() * 1000))


# ============================================================================
# 主循环服务
# ============================================================================


def main() -> None:
    """启动 EvolutionSandbox 服务，监听演化请求。"""
    from logging_setup import setup_logging
    setup_logging("evolution")

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    db_path = os.environ.get("REGISTRY_DB", "agents.db")

    logger.info("Starting EvolutionSandbox (Redis: %s:%d)", redis_host, redis_port)

    messaging = MessagingClient(redis_host, redis_port)
    registry = AgentRegistry(db_path)
    sandbox = EvolutionSandbox(messaging, registry)

    def shutdown(signum=None, frame=None):
        logger.info("Shutting down...")
        sandbox._running = False

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, shutdown)
        except Exception:
            pass

    # 监听演化请求
    sandbox._running = True
    logger.info("EvolutionSandbox listening on 'orchestrator:evolution_request'")

    try:
        for raw_message in messaging.subscribe("orchestrator:evolution_request"):
            if not sandbox._running:
                break

            try:
                agent_type = raw_message.get("agent_type", "search_agent")
                base_strategy_data = raw_message.get("base_strategy")
                base = StrategyConfig.from_dict(base_strategy_data) if base_strategy_data else None
                test_tasks = raw_message.get("test_tasks")

                logger.info("Evolution request for '%s'", agent_type)
                result = sandbox.evolve(agent_type, base, test_tasks)

                messaging.publish("orchestrator:evolution_result", result)
                logger.info("Evolution result published: %s", result["summary"])

            except Exception as exc:
                logger.error("Evolution error: %s", exc, exc_info=True)

    except KeyboardInterrupt:
        pass
    finally:
        try:
            messaging.close()
        except Exception:
            pass
        try:
            registry.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
