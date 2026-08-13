"""
织光 (ZhiGuang) — ContentSummaryWorker

能力标签: [content_summary]
职责: 总结、提炼、生成报告——调用 LLM 处理文本内容。
"""

import os, sys, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

logger = logging.getLogger(__name__)


class ContentSummaryWorker(AsyncWorkerBase):
    """LLM 驱动的文本总结 Worker。"""

    def __init__(self, **kwargs):
        super().__init__(
            agent_id=kwargs.pop("agent_id", "content_summarizer"),
            capabilities=["content_summary"],
            **kwargs,
        )

    async def execute(self, instruction: str) -> str:
        import asyncio
        loop = asyncio.get_running_loop()

        def _sync():
            import json as _json
            from llm_client import call_llm

            system = (
                "你是专业内容总结师。根据指令对内容进行总结、提炼或生成报告。"
                "输出高质量的 Markdown 格式。如果是生成最终报告，要包含："
                "总体摘要、关键发现、数据要点、建议。"
                "保持专业、简洁、可操作。"
                "\n\n结构化图表数据：如果指令中提供的检索结果存在与本任务主题直接相关"
                "的可靠数值（市场规模、份额、增速、营收等），在总结末尾追加一个 "
                "[CHART_DATA] JSON 块（放在总结之后，只输出一次）：\n"
                "[CHART_DATA]\n"
                '{"data":[{"指标":"市场规模","年份":2025,"数值":1500,"单位":"亿美元",'
                '"口径":"德勤预测","来源":"https://example.com"}]}\n'
                "规则：只收录与任务主题直接相关的数值；口径必须区分（不同机构/定义分开列）；"
                "数值必须来自检索资料真实出现的内容，严禁编造；"
                "不同来源的同一指标分开成多行；没有可靠数值就不要输出 [CHART_DATA]。"
                "\n【硬性要求】如果检索结果中存在本主题的可靠数值（市场规模/份额/增速/营收等），"
                "你必须在总结正文之后、原样输出 [CHART_DATA] JSON 块（不得省略，不要放在代码块里）。"
            )

            user = f"指令: {instruction}\n\n请根据指令生成总结内容。"

            try:
                result = call_llm(system, user, expect_json=False)
                summary = result.get("content", f"总结: {instruction}")
                # 第二次调用：从总结中提取结构化图表数据（严格 JSON，保证结构可靠）
                try:
                    ext_sys = (
                        "你是数据提取器。从给定的总结中提取与本任务主题直接相关的数值数据点，"
                        "输出严格JSON：{\"data\":[{\"指标\":\"市场规模\",\"年份\":2025,"
                        "\"数值\":1500,\"单位\":\"亿美元\",\"口径\":\"德勤预测\","
                        "\"来源\":\"https://example.com\"}]}。"
                        "规则：只提取与主题直接相关的数值（市场规模/份额/增速/营收等）；"
                        "口径必须区分不同来源与定义；数值必须来自总结中真实出现的内容，"
                        "严禁编造；不同来源的同一指标分成多行；"
                        "没有可靠数值就输出 {\"data\":[]}。只输出JSON。"
                    )
                    ext = call_llm(ext_sys, f"总结：\n{summary[:6000]}", expect_json=True)
                    rows = (ext or {}).get("data") or []
                    if rows:
                        summary = (
                            summary
                            + "\n\n[CHART_DATA]\n"
                            + _json.dumps({"data": rows}, ensure_ascii=False)
                        )
                except Exception as exc:
                    logger.warning("Chart data extraction failed: %s", exc)
                return summary
            except Exception as exc:
                logger.warning("Content summary failed: %s", exc)
                raise

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
