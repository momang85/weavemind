"""
织光 (ZhiGuang) — ContentSummaryWorker

能力标签: [content_summary]
职责: 总结、提炼、生成报告——调用 LLM 处理文本内容。
"""

import os, sys, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

logger = logging.getLogger(__name__)


def _load_json_loose(text: str) -> dict | list | None:
    """宽松 JSON 解析：容忍 markdown 围栏、前后多余文字，
    从首个 '{' 起按花括号配平截取 JSON 对象。"""
    if not text:
        return None
    import json as _json
    import re as _re
    t = text.strip()
    m = _re.search(r"```(?:json)?\s*(.*?)```", t, _re.S)
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
                        return _json.loads(t[i:j + 1])
                    except Exception:
                        break
    try:
        return _json.loads(t, strict=False)
    except Exception:
        return None


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
            from prompt_registry import get_prompt

            system = get_prompt("content_summary", (
                "你是专业内容总结师。根据指令对内容进行总结、提炼或生成报告。"
                "输出高质量的 Markdown 格式。如果是生成最终报告，要包含："
                "总体摘要、关键发现、数据要点、建议。"
                "保持专业、简洁、可操作。"
                "\n\n数据要点：如果检索结果中存在与本任务主题直接相关的可靠数值"
                "（市场规模、份额、增速、营收等），请在正文中以 Markdown 表格呈现，"
                "列为：机构/来源 | 指标 | 数值 | 年份 | 口径说明 | 来源链接。"
                "只收录与任务主题直接相关的数值；不同机构/口径分开列行；"
                "数值必须来自检索资料真实出现的内容，严禁编造。"
            ))

            user = f"指令: {instruction}\n\n请根据指令生成总结内容。"

            try:
                from llm_client import call_llm_stream
                try:
                    summary = call_llm_stream(system, user)
                except Exception:
                    result = call_llm(system, user, expect_json=False)
                    summary = result.get("content", f"总结: {instruction}")
                # 第二次调用：从总结中提取结构化图表数据（严格 JSON，保证结构可靠）
                try:
                    ext_sys = (
                        "你是数据提取器。从给定的总结中提取与本任务主题直接相关的数值数据点，"
                        "输出严格JSON图表规格：{\"charts\":[{\"question\":\"调研问题\","
                        "\"conclusion\":\"一句话结论\",\"type\":\"bar|line|horizontal_bar|pie|scatter\","
                        "\"title\":\"指标+时间+地域+单位\",\"x_axis_title\":\"...\","
                        "\"y_axis_title\":\"...(单位)\",\"unit\":\"亿美元\","
                        "\"time_range\":\"2025年\",\"region\":\"全球\",\"source\":\"数据来源\","
                        "\"sample_size\":\"5\",\"annotation\":\"结论注释\",\"missing\":\"缺失说明\","
                        "\"outliers\":\"异常说明\",\"data\":[{\"label\":\"口径/来源\","
                        "\"value\":1500,\"year\":2025,\"caliber\":\"德勤预测\","
                        "\"source\":\"https://example.com\"}]}]}。"
                        "图表规范：① 每张图必须先给 question（要回答的调研问题）和 conclusion"
                        "（一句话结论）；无法得出明确结论就跳过该图，不要硬画。"
                        "② 图表类型按数据特征：时间序列→line；类别≤10→bar；类别>10→horizontal_bar；"
                        "占比且≤5类→pie；关系→scatter。"
                        "③ 标注必须完整：title 含指标+时间+地域+单位，x/y 轴标题带单位，"
                        "source/time_range/region/unit/sample_size/annotation 不能为空。"
                        "④ 禁止为单个数据点生成图表：至少需要 2 个可对比的数据点，"
                        "否则跳过该图。同一指标的不同年份/机构数据合并到一张图"
                        "（时间序列折线或对比柱状），不得拆成多张单点图。"
                        "⑤ 饼图必须 2~5 类且数值加和有意义；单类饼图禁止。"
                        "⑥ label 必须是简短类别名（机构/年份/地区/厂商，≤12字），"
                        "禁止把指标描述或整句当作 label"
                        "（如\"AI芯片占全球芯片市场11%…\"不允许）。"
                        "⑦ 数值必须与来源完全一致，保留小数"
                        "（1059.8 不能写成 1060）；饼图（pie）仅用于加和≈100% 的占比，"
                        "非占比数据用 bar/line。"
                        "规则：只提取与主题直接相关的数值（市场规模/份额/增速/营收等）；"
                        "口径必须区分不同来源与定义；数值必须来自总结中真实出现的内容，"
                        "严禁编造；不同来源的同一指标分成多行；"
                        "排除与核心主题无关的其它领域数值（如人形机器人/SoC/汽车/"
                        "手机/投资额/财报等，除非它们就是任务核心主题本身）；"
                        "没有可靠数值或无法得出结论就输出 {\"charts\":[]}。只输出JSON。"
                    )
                    import re as _re
                    clean_summary = _re.sub(
                        r"\[CHART_DATA\].*?(\n\n|$)", "", summary, flags=_re.S
                    )[:6000]
                    # 宽松解析：LLM 可能带 markdown 围栏或多余文字，expect_json=False
                    # 后手动截取首个平衡 JSON 对象，避免格式问题直接丢数据。
                    ext_raw = call_llm(ext_sys, f"总结：\n{clean_summary}", expect_json=False)
                    ext = _load_json_loose(
                        str((ext_raw or {}).get("content") or "")
                        if isinstance(ext_raw, dict) else str(ext_raw or "")
                    )
                    specs = (ext or {}).get("charts") or []
                    logger.info(
                        "Chart spec extraction: charts=%d (raw_len=%d)",
                        len(specs), len(str(ext_raw or "")),
                    )
                    # 兼容旧格式：扁平 data 行 → 打包为规格
                    if not specs:
                        rows = (ext or {}).get("data") or []
                        if rows:
                            from chart_specs import wrap_rows_to_specs
                            specs = wrap_rows_to_specs([r for r in rows if isinstance(r, dict)])
                    if specs:
                        # 同指标跨年份的单点图先合并为时间序列，避免 2025/2026
                        # 被拆成两张无意义单点图
                        from chart_specs import (
                            merge_year_series, validate_spec, verify_specs_against_text,
                        )
                        specs = merge_year_series(specs)
                        # 数据溯源：数值必须能在总结文本中找到，防 LLM 编造/转写错误
                        specs, dropped = verify_specs_against_text(specs, clean_summary)
                        if dropped:
                            logger.info("Chart data verification dropped %d rows", dropped)
                        # 标注完整性校验：数据点不足/类型非法不可修复（防 LLM 编造
                        # 数字），直接丢弃；仅对"标注缺失"类问题让 LLM 一次性补全。
                        final_specs: list[dict] = []
                        for s in specs:
                            issues = validate_spec(s)
                            if not issues:
                                final_specs.append(s)
                                continue
                            data_bad = any(
                                ("data" in x) or ("单点" in x) or ("type 非法" in x)
                                for x in issues
                            )
                            if data_bad:
                                logger.info(
                                    "Chart spec dropped (unfixable): %s",
                                    "; ".join(issues),
                                )
                                continue
                            try:
                                fix_prompt = (
                                    "以下图表规格缺少关键标注，请补齐后输出完整严格JSON"
                                    "（保持原数据与结论不变）：\n"
                                    + "; ".join(issues) + "\n规格：\n"
                                    + _json.dumps(s, ensure_ascii=False)[:4000]
                                )
                                fixed_raw = call_llm(
                                    "你是图表标注完善器。补齐缺失字段，只输出严格JSON"
                                    "{\"charts\":[...]}，不得改动数值。",
                                    fix_prompt, expect_json=False,
                                )
                                fixed = _load_json_loose(
                                    str((fixed_raw or {}).get("content") or "")
                                    if isinstance(fixed_raw, dict) else str(fixed_raw or "")
                                )
                                fixed_specs = (fixed or {}).get("charts") or []
                                if fixed_specs and not validate_spec(fixed_specs[0]):
                                    final_specs.append(fixed_specs[0])
                                else:
                                    final_specs.append(s)
                            except Exception:
                                final_specs.append(s)
                        specs = final_specs
                        summary = (
                            summary
                            + "\n\n[CHART_DATA]\n"
                            + _json.dumps({"charts": specs}, ensure_ascii=False)
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
