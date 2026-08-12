"""
织光 (ZhiGuang) — ContentSummaryWorker

能力标签: [content_summary]
职责: 总结、提炼、生成报告——调用 LLM 处理文本内容。
"""

import os, sys, logging, re, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

logger = logging.getLogger(__name__)


class ContentSummaryWorker(AsyncWorkerBase):
    """LLM 驱动的文本总结 Worker。"""
    _needs_task = True

    def __init__(self, **kwargs):
        super().__init__(
            agent_id=kwargs.pop("agent_id", "content_summarizer"),
            capabilities=["content_summary"],
            **kwargs,
        )

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        import asyncio
        loop = asyncio.get_running_loop()

        def _sync():
            from llm_client import call_llm

            system = (
                "你是专业内容总结师。根据指令对内容进行总结、提炼或生成报告。"
                "输出高质量的 Markdown 格式。如果是生成最终报告，要包含："
                "总体摘要、关键发现、数据要点、建议。"
                "保持专业、简洁、可操作。"
            )

            user = f"指令: {instruction}\n\n请根据指令生成总结内容。"

            try:
                result = call_llm(system, user, expect_json=False)
                text = result.get("content", f"总结: {instruction}")
            except Exception as exc:
                logger.warning("Content summary failed: %s", exc)
                raise

            from pathlib import Path
            # 确定性图表嵌入：与 report_generator 一致，保证图文搭配
            if task and task.get("workspace"):
                proj = Path(str(task["workspace"])) / "project"
                charts = []
                if proj.exists():
                    cutoff = time.time() - 120 * 60
                    for c in proj.rglob("*.png"):
                        if "screenshots" in c.parts:
                            continue
                        try:
                            if c.stat().st_mtime >= cutoff:
                                charts.append(c)
                        except OSError:
                            continue
                if charts:
                    text += "\n\n## 图表\n\n"
                    text += "".join(f"![{c.stem}]({c})\n\n" for c in charts[:6])
            # 来源附录：从指令中的 [数据来源] 块提取 URL
            src_urls: list[str] = []
            m = re.search(r"\[数据来源\](.*)", str(instruction), re.S)
            if m:
                for u in re.findall(r"https?://[^\s\)\]]+", m.group(1)):
                    if u not in src_urls:
                        src_urls.append(u)
            if src_urls:
                text += "\n\n## 数据来源\n\n"
                text += "\n".join(f"- [{u}]({u})" for u in src_urls[:15])
            return text

        return await loop.run_in_executor(None, _sync)


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-content-summary")
    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    db_path = os.environ.get("REGISTRY_DB", "agents.db")

    registry = AsyncRegistry(db_path)
    messaging = AsyncMessaging(redis_host, redis_port)

    worker = ContentSummaryWorker(
        agent_id="content_summarizer",
        registry=registry,
        messaging=messaging,
        max_concurrency=5,
    )

    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.shutdown()


def main():
    try:
        import asyncio
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
