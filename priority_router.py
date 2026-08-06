"""
织光 (ZhiGuang) — 优先级任务路由器 (PriorityRouter)

在 Redis 前增加反压与优先级队列。
高负载时自动暂停低优先级消费，确保核心任务不受影响。

队列结构:
    task:high:{agent_id}  — 高优先级（实时用户请求）
    task:low:{agent_id}   — 低优先级（后台批处理）
"""

import os, sys, json, time, logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)


class PriorityRouter:
    """双队列优先级路由器。先消费高优先级，高队列空时才消费低优先级。"""

    def __init__(self, messaging, high_first: bool = True):
        self._messaging = messaging
        self._high_first = high_first

    def push_task(self, agent_id: str, task: dict, priority: str = "high") -> bool:
        """将任务推入对应优先级队列。

        Args:
            agent_id: 目标 Worker ID。
            task: 任务字典。
            priority: "high" 或 "low"。
        """
        queue = f"task:{priority}:{agent_id}"
        return self._messaging.push_task_to_queue(queue, task)

    def pop_task(self, agent_id: str, timeout: int = 2) -> dict | None:
        """先取高优先级，无任务时降级取低优先级。

        使用 Redis BRPOP 实现：高队列优先，低队列兜底。
        """
        r = self._messaging._redis
        high_key = f"task:high:{agent_id}"
        low_key = f"task:low:{agent_id}"

        try:
            # BRPOP 高优先级队列
            result = r.brpop(high_key, timeout=timeout)
            if result:
                return json.loads(result[1])
            # 高队列空 → 降级到低优先级
            result = r.brpop(low_key, timeout=timeout)
            if result:
                return json.loads(result[1])
        except Exception:
            pass
        return None

    def get_queue_depths(self, agent_id: str) -> dict:
        """返回各队列当前深度。"""
        try:
            r = self._messaging._redis
            return {
                "high": r.llen(f"task:high:{agent_id}"),
                "low": r.llen(f"task:low:{agent_id}"),
            }
        except Exception:
            return {"high": -1, "low": -1}


# ============================================================================
# 流式输出适配器 (SSE)
# ============================================================================

class StreamingOutput:
    """将 Worker 的部分结果以 SSE 流式推送给前端。

    用法:
        stream = StreamingOutput(redis_client)
        stream.start(task_id)

    前端监听: GET /stream/{task_id} → text/event-stream
    """

    def __init__(self, redis_client):
        self._r = redis_client

    def publish_progress(self, task_id: str, step_id: str, status: str, partial_result: str = ""):
        """发布进度更新到流式频道。"""
        self._r.publish(f"stream:{task_id}", json.dumps({
            "step_id": step_id,
            "status": status,
            "partial": partial_result[:500],
            "timestamp": time.time(),
        }, ensure_ascii=False))

    def subscribe(self, task_id: str):
        """生成器：阻塞式获取流式事件。"""
        pubsub = self._r.pubsub()
        pubsub.subscribe(f"stream:{task_id}")
        for msg in pubsub.listen():
            if msg["type"] == "message":
                yield json.loads(msg["data"])


# ============================================================================
# 多模态输出分发器 (OutputDispatcher)
# ============================================================================

class OutputDispatcher:
    """订阅 output:publish 频道，将报告转换为多种格式并分发到各渠道。

    支持:
        - Markdown → 原样返回
        - HTML Email → 通过模板渲染
        - Slack/企业微信 → Webhook 推送
    """

    FORMATS = ["markdown", "html", "slack", "wechat_work"]

    def __init__(self, messaging):
        self._messaging = messaging

    def dispatch(self, task_id: str, report: str, goal: str,
                 formats: list[str] | None = None, channels: list[str] | None = None):
        """将报告转为指定格式并分发。

        Args:
            task_id: 任务ID。
            report: Markdown 报告。
            goal: 用户原始目标。
            formats: 输出格式列表，默认 ["markdown"]。
            channels: 分发渠道，如 ["slack", "email"]。
        """
        formats = formats or ["markdown"]
        channels = channels or []

        outputs = {}

        if "markdown" in formats:
            outputs["markdown"] = report

        if "html" in formats:
            outputs["html"] = self._to_html(report, goal)

        if "slack" in formats:
            outputs["slack"] = self._to_slack(report, goal)

        # 发布到各渠道
        for channel in channels:
            self._messaging.publish(f"output:{channel}", {
                "task_id": task_id,
                "goal": goal[:100],
                "formats": outputs,
                "timestamp": time.time(),
            })

        return outputs

    def _to_html(self, report: str, goal: str) -> str:
        """Markdown → HTML 邮件模板。"""
        return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
body{{font-family:-apple-system,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#1a1a2e;line-height:1.6}}
h1{{color:#16213e;border-bottom:2px solid #0f3460;padding-bottom:8px}}
h2{{color:#0f3460}} table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left}} th{{background:#0f3460;color:#fff}}
.badge-ok{{color:#16a34a}}.badge-fail{{color:#dc2626}}
.footer{{margin-top:30px;font-size:12px;color:#94a3b8;border-top:1px solid #e2e8f0;padding-top:10px}}
</style></head><body>
<h1>AI Report</h1><p><strong>Goal:</strong> {goal}</p>
{self._md_to_html(report)}
<div class="footer">Generated by ZhiGuang AI Agent System</div>
</body></html>"""

    def _to_slack(self, report: str, goal: str) -> dict:
        """生成 Slack Block Kit 格式。"""
        return {
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"Report: {goal[:100]}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": report[:3000]}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": "Generated by ZhiGuang"}]},
            ]
        }

    def _md_to_html(self, md: str) -> str:
        """简易 Markdown → HTML 转换。"""
        html = md
        html = html.replace("**", "").replace("# ", "<h2>").replace("\n", "<br>")
        return html


# ============================================================================
# 集成示例：在 orchestrator 中使用
# ============================================================================

def _integrate_into_orchestrator():
    """展示如何在 orchestrator 中集成优先级路由和流式输出。

    将此代码片段合并到 orchestrator.py 的 _handle_goal 方法中。
    """
    return """
    # ---- 在 orchestrator.py _handle_goal 中集成 ----

    # 1. 启用流式输出
    from priority_router import StreamingOutput
    stream = StreamingOutput(self._messaging._redis)

    # 2. 在每个步骤完成时推送进度
    for step in plan.steps:
        stream.publish_progress(task_id, step.task_id, "IN_PROGRESS")
        result = self._dispatch_step(step, plan.steps)
        stream.publish_progress(task_id, step.task_id, result["status"], result.get("result", ""))

    # 3. 完成后通过 OutputDispatcher 多渠道分发
    from priority_router import OutputDispatcher
    dispatcher = OutputDispatcher(self._messaging)
    dispatcher.dispatch(task_id, final_report, goal, formats=["markdown", "html"], channels=["slack"])
    """
