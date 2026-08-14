"""
织光 (ZhiGuang) — 评论家智能体 (CriticAgent)

订阅中枢规划草案，从四个维度进行多角色批判性评审。
只有通过评审的计划才能进入执行阶段。

评审维度：完备性、效率、安全性、可执行性
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from typing import Any

from common import MessagingClient
from llm_client import call_llm, LLMCallError, LLMJSONParseError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 评审系统提示词
# ---------------------------------------------------------------------------

CRITIC_SYSTEM = """你是经验丰富的首席架构师兼UX专家，以眼光毒辣、要求严苛著称。
你不是为了否定而否定，而是为了让产品更卓越。

## 五维评审（每项 1-10 分）
1. **完备性 (completeness)**：步骤是否足以交付完整可用产品？是否遗漏样式、交互、错误处理？
2. **效率 (efficiency)**：技术选型和任务安排是否最优？有没有多余步骤？有没有更轻量的替代方案？
3. **安全性 (safety)**：是否存在 XSS 风险？数据处理是否安全？依赖来源是否可信？
4. **可执行性 (executability)**：指令是否具体可验证？能力是否能真正完成任务？
5. **创新性 (innovation)**：产品是否有让人眼前一亮的创意点？如果没有，大胆提出一个具体可实现的创新建议。

## 输出格式（严格 JSON）
{
  "scores": {"completeness": 8, "efficiency": 7, "safety": 9, "executability": 8, "innovation": 6},
  "verdict": "PASS" | "FAIL",
  "critical_issues": ["必须修复的问题1", "..."],
  "suggestions": ["优化建议1", "..."],
  "innovation_suggestion": "具体的创新建议，如：增加数据粒子背景动画、实时滚动AI头条跑马灯",
  "summary": "一句话评审总结"
}

## 规则
1. 任一维度 < 5 → verdict = FAIL，必须列出所有 critical_issues。
2. PASS 时也必须提供 innovation_suggestion。
3. 只输出 JSON。"""


# ============================================================================
# 评论家智能体
# ============================================================================


class CriticAgent:
    """独立评论家：审查计划草案，输出多维度评审结果。

    Usage:
        critic = CriticAgent(MessagingClient("localhost", 6379))
        critic.run()
    """

    def __init__(self, messaging: MessagingClient) -> None:
        self._messaging = messaging
        self._running = False
        self._shutting_down = False

        # 统计
        self._total_reviews = 0
        self._pass_count = 0
        self._fail_count = 0

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    def run(self) -> None:
        """启动评论家，监听 orchestrator:plan_draft 频道。"""
        self._setup_signal_handlers()
        self._running = True

        logger.info("CriticAgent started, listening on 'orchestrator:plan_draft'")

        for raw_message in self._messaging.subscribe("orchestrator:plan_draft"):
            if not self._running:
                break

            try:
                plan_id = raw_message.get("plan_id", "")
                goal = raw_message.get("goal", "")
                steps = raw_message.get("steps", [])

                if not plan_id or not steps:
                    logger.warning("Received incomplete plan draft: plan_id=%s", plan_id)
                    continue

                logger.info(
                    "Received plan draft '%s': goal='%s', %d steps",
                    plan_id,
                    goal[:60],
                    len(steps),
                )

                # 执行评审
                review = self.review_plan(plan_id, goal, steps)

                # 发布评审结果
                self._publish_review(review)
                logger.info(
                    "Published review for '%s': verdict=%s, scores=%s",
                    plan_id,
                    review.get("verdict"),
                    review.get("scores", {}),
                )

            except Exception as exc:
                logger.error("Error in critic loop: %s", exc, exc_info=True)
                # 发布失败结果，避免 orchestrator 永久等待
                if raw_message and raw_message.get("plan_id"):
                    self._publish_review({
                        "plan_id": raw_message["plan_id"],
                        "verdict": "ERROR",
                        "error": str(exc),
                        "scores": {},
                        "suggestions": [f"评审系统异常: {exc}"],
                        "summary": "评审过程出错",
                    })

        logger.info(
            "CriticAgent stopped. Reviews: %d total, %d PASS, %d FAIL",
            self._total_reviews,
            self._pass_count,
            self._fail_count,
        )

    def shutdown(self, signum: int | None = None, frame: Any = None) -> None:
        """优雅退出。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._running = False
        signal_name = signal.Signals(signum).name if signum else "manual"
        logger.info("CriticAgent shutting down (signal: %s)...", signal_name)

    # ------------------------------------------------------------------
    # 评审引擎
    # ------------------------------------------------------------------

    def review_plan(
        self,
        plan_id: str,
        goal: str,
        steps: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """对计划草案进行多维度评审。

        Args:
            plan_id: 计划 ID。
            goal: 原始目标。
            steps: 步骤列表。

        Returns:
            评审结果字典，包含 scores, verdict, suggestions, summary。
        """
        # 构建评审请求
        plan_text = json.dumps(
            {
                "plan_id": plan_id,
                "goal": goal,
                "steps": [
                    {
                        "step_id": s.get("step_id", i + 1),
                        "capability": s.get("capability", "unknown"),
                        "instruction": s.get("instruction", ""),
                        "parent_id": s.get("parent_id"),
                    }
                    for i, s in enumerate(steps)
                ],
            },
            ensure_ascii=False,
            indent=2,
        )

        user_prompt = f"请审查以下任务计划：\n\n{plan_text}"

        try:
            from prompt_registry import get_prompt
            result = call_llm(
                get_prompt("critic", CRITIC_SYSTEM), user_prompt, expect_json=True
            )
        except LLMJSONParseError as exc:
            logger.error("Critic LLM JSON parse error: %s", exc)
            return self._fallback_review(plan_id, f"评审解析失败: {exc}")
        except LLMCallError as exc:
            logger.error("Critic LLM call error: %s", exc)
            return self._fallback_review(plan_id, f"评审调用失败: {exc}")

        # 提取评审结果
        scores = result.get("scores", {})
        verdict = result.get("verdict", "FAIL").upper()
        suggestions = result.get("suggestions", [])
        summary = result.get("summary", "")

        # 强制检查：任一维度低于5分 → FAIL
        for dim, score in scores.items():
            if isinstance(score, (int, float)) and score < 5:
                if verdict == "PASS":
                    logger.warning(
                        "Critic gave PASS but %s=%s < 5, forcing FAIL", dim, score
                    )
                    verdict = "FAIL"
                    suggestions.append(f"[自动修正] {dim} 评分过低({score}/10)，必须修改")

        review = {
            "plan_id": plan_id,
            "verdict": verdict,
            "scores": scores,
            "suggestions": suggestions,
            "summary": summary,
            "reviewer": "critic_agent",
            "timestamp": _now_iso(),
        }

        # 统计
        self._total_reviews += 1
        if verdict == "PASS":
            self._pass_count += 1
        else:
            self._fail_count += 1

        return review

    def _publish_review(self, review: dict[str, Any]) -> None:
        """发布评审结果：Pub/Sub 频道 + Redis 列表（供编排器 BRPOP 可靠等待）。"""
        plan_id = review.get("plan_id", "")
        try:
            self._messaging.publish("orchestrator:plan_review", review)
            if plan_id:
                import json
                self._messaging._redis.rpush(
                    f"plan_review:{plan_id}", json.dumps(review, ensure_ascii=False)
                )
        except Exception as exc:
            logger.warning("Failed to publish review for '%s': %s", plan_id, exc)

    # ------------------------------------------------------------------
    # 回退评审
    # ------------------------------------------------------------------

    def _fallback_review(self, plan_id: str, error_msg: str) -> dict[str, Any]:
        """当 LLM 调用失败时，生成保守的回退评审结果。

        默认行为：直接 PASS（不阻塞），但记录错误。
        """
        logger.warning("Using fallback review for '%s': %s", plan_id, error_msg)
        return {
            "plan_id": plan_id,
            "verdict": "PASS",
            "scores": {
                "completeness": 5,
                "efficiency": 5,
                "safety": 5,
                "executability": 5,
            },
            "suggestions": [f"评审系统降级: {error_msg}"],
            "summary": "评审系统暂时不可用，默认通过",
            "reviewer": "critic_agent_fallback",
            "timestamp": _now_iso(),
        }

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _setup_signal_handlers(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.shutdown)
            except Exception:
                pass


# ============================================================================
# 辅助函数
# ============================================================================


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# 启动入口
# ============================================================================


def main() -> None:
    from logging_setup import setup_logging
    setup_logging("critic")

    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))

    logger.info("Starting CriticAgent (Redis: %s:%d)", redis_host, redis_port)

    messaging = MessagingClient(redis_host, redis_port)
    critic = CriticAgent(messaging)

    try:
        critic.run()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt, shutting down...")
        critic.shutdown()
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        critic.shutdown()
        sys.exit(1)
    finally:
        try:
            messaging.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
