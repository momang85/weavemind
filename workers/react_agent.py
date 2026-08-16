# -*- coding: utf-8 -*-
"""ReAct Agent（运行时 tool_calls，对标标准 3.1 ReAct 模式）。

循环：LLM 决策（最终答案 或 工具调用）→ 派发工具 → 观察结果 → 再决策，
直到给出最终答案或达到最大轮数。工具经 tool_dispatch 派发给现有 worker。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging


REACT_SYSTEM = """你是 ReAct Agent（运行时工具调用）。【受众】任务执行系统（机器可读）。
【任务】根据任务目标，循环决策：
- 需要外部信息/能力时，选择工具并给出参数；
- 信息足够时，直接给出最终答案。
【可用工具】见消息中的工具目录。
【输出要求】每轮只输出严格 JSON 之一：
  {"final": "最终答案"}
  或 {"tool": "工具名", "arguments": {"instruction": "给该工具的具体指令"}}
【规则】
1. 每轮最多调用 1 个工具；观察结果后再决策（ReAct 思考-行动-观察）。
2. 工具结果不满足需求时，可换查询/换工具重试，但不要无限循环。
3. 无法获得所需数据时，如实说明"未能获取 XX"并给出最终答案，禁止编造。
只输出JSON。"""


class ReactAgent(AsyncWorkerBase):
    _class_capabilities = ["react_agent"]
    _needs_task = True

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        from tool_contracts import tool_catalog_text
        from tool_dispatch import dispatch_tool

        max_rounds = int(os.environ.get("REACT_MAX_ROUNDS", "5") or 5)
        task_id = str((task or {}).get("task_id") or "")
        workspace = str((task or {}).get("workspace") or "")
        history = [
            {"role": "user", "content":
                f"任务目标：{instruction}\n\n可用工具：\n{tool_catalog_text()}"}
        ]
        for rnd in range(1, max_rounds + 1):
            try:
                resp = await self._call_llm(
                    system=REACT_SYSTEM,
                    prompt="对话历史：\n" + json.dumps(history, ensure_ascii=False)[:8000],
                    max_tokens=1200,
                )
            except Exception as exc:
                return f"ReAct 决策调用失败：{exc}"
            decision = _loads_loose(resp)
            if not isinstance(decision, dict):
                history.append({"role": "assistant", "content": f"（非法决策输出）{str(resp)[:200]}"})
                continue
            if decision.get("final"):
                return str(decision["final"])
            tool = str(decision.get("tool") or "")
            args = decision.get("arguments") or {}
            if not tool or not args.get("instruction"):
                history.append({"role": "assistant", "content": "（决策缺少 tool/arguments.instruction）"})
                continue
            history.append({"role": "assistant", "content": f"调用工具 {tool}"})
            result = dispatch_tool(
                tool, str(args["instruction"]),
                task_id=task_id, timeout=int(args.get("timeout") or 300),
                workspace=workspace,
            )
            obs = str(result.get("result") or result)[:3000]
            status = result.get("status")
            history.append({"role": "user", "content": f"工具 {tool} 结果（{status}）：\n{obs}"})
        return "ReAct 达到最大轮数仍未收敛；请重试或细化目标。"


def _loads_loose(text) -> dict | None:
    import re
    t = str(text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    i = t.find("{")
    if i >= 0:
        depth = 0
        for j in range(i, len(t)):
            if t[j] == "{":
                depth += 1
            elif t[j] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(t[i:j + 1])
                    except Exception:
                        break
    try:
        return json.loads(t, strict=False)
    except Exception:
        return None


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-react-agent")
    registry = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    messaging = AsyncMessaging(
        os.environ.get("REDIS_HOST", "localhost"),
        int(os.environ.get("REDIS_PORT", "6379")),
    )
    worker = ReactAgent(
        agent_id="reactagentworker",
        capabilities=ReactAgent._class_capabilities,
        registry=registry,
        messaging=messaging,
        max_concurrency=2,
    )
    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.shutdown()


if __name__ == "__main__":
    import asyncio
    asyncio.run(amain())
