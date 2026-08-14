# -*- coding: utf-8 -*-
"""中枢→Worker 指令信封：为每个步骤补齐【角色 / 受众 / 输出要求 / 质量标准】。

用户目标与前序结果已由编排器注入；本模块负责补齐 LLM 工作者最容易缺失的
四要素（角色、受众、结构化输出、验收标准），并允许 prompt_registry 覆盖。
"""

from prompt_registry import load_overrides

# 按能力分类：LLM 类（受众由模型按目标推断）/ 机器类（输出给下游程序）
_ENVELOPES: dict[str, dict] = {
    "web_search": {
        "role": "信息检索专家，只返回与任务主题直接相关、来源可信的结果",
        "audience": "下游分析引擎（机器可读，但质量要经得起人工核验）",
        "output": "严格 JSON 数组 [{\"title\": \"标题\", \"url\": \"原始链接\", \"snippet\": \"关键摘要\"}]，只输出 JSON",
        "criteria": (
            "① 每条都必须带原始 URL；② 过滤垃圾站/空结果/与主题无关内容；"
            "③ 同一目标最多检索一轮，避免重复搜索；④ 来源需权威（官方、机构、主流媒体）"
        ),
    },
    "web_fetch": {
        "role": "网页抓取与正文提取工程师",
        "audience": "下游分析引擎（机器可读）",
        "output": "严格 JSON {\"status\": \"success|failed\", \"url\": \"\", \"title\": \"\", \"text\": \"正文\"}",
        "criteria": "① 保留原文关键信息与来源 URL；② 剔除导航/广告/脚本噪音；③ 失败时返回 failed 与原因",
    },
    "data_loader": {
        "role": "数据工程师",
        "audience": "下游数据分析引擎（机器可读）",
        "output": "严格 JSON {\"status\": \"downloaded|loaded_sklearn|failed\", \"path\": \"\", \"dataset\": \"\", \"rows\": 0, \"cols\": 0}",
        "criteria": "① 优先使用指令指定的 URL/数据集；② 不得把与主题无关的内置数据集拉进任务；③ 落盘到任务 data 目录",
    },
    "data_analyzer": {
        "role": "数据分析师",
        "audience": "下游建模引擎与最终报告（机器可读 + 图表可核验）",
        "output": "严格 JSON {\"status\": \"success|failed\", \"charts\": [\"路径\"], \"stats\": {...}}",
        "criteria": "① 图必须带标题与轴标签；② 数值必须来自真实数据文件；③ 图表落盘到任务 charts 目录",
    },
    "model_trainer": {
        "role": "机器学习工程师",
        "audience": "下游报告（机器可读指标）",
        "output": "严格 JSON {\"status\": \"success|failed\", \"models\": {\"模型\": {\"RMSE\": 0, \"R2\": 0}}, \"feature_importance\": []}",
        "criteria": "① 划分训练/测试集并报告指标；② 特征重要性按降序；③ 使用指令指定的数据文件",
    },
    "content_summary": {
        "role": "专业内容总结师",
        "audience": "由任务目标推断（如董事会、CTO、普通用户、工程师），按其理解水平组织术语与详略",
        "output": (
            "结构化 Markdown：总体摘要 / 关键发现 / 数据要点（表格：机构|指标|数值|年份|口径|来源）/ 建议；"
            "如存在可画图的可靠数值，按图表规范输出 [CHART_DATA] 规格 JSON"
        ),
        "criteria": (
            "① 只使用与主题直接相关的信息；② 数值必须来自检索资料且标注口径差异，严禁编造；"
            "③ 不同来源/口径不得混为一谈；④ 图表至少 2 个数据点、有结论才画"
        ),
    },
    "report_generator": {
        "role": "专业报告撰写者",
        "audience": "由任务目标推断（如董事会、CTO、普通用户、工程师），按其决策场景组织报告",
        "output": (
            "可直接交付的 Markdown 报告：标题 / 概述 / 正文章节（含表格）/ 关键数据一览 / 数据来源附录；"
            "图表由系统按章节自动嵌入，正文用文字提及图表即可"
        ),
        "criteria": (
            "① 覆盖用户目标的所有要求，正文详实而非占位符；② 关键数据一览表（指标|数值|口径/年份|来源）；"
            "③ 数据来源附录列原始 URL；④ 与任务无关的产物（如历史数据集）一律不用"
        ),
    },
    "code_execution": {
        "role": "资深全栈工程师",
        "audience": "最终用户（如需可玩/可交互，输出必须真正能运行）",
        "output": (
            "单个自包含可运行的 Python 文件，或自包含单文件 HTML（内联 CSS/JS）；"
            "只输出代码/文件内容，必要时注释说明依赖"
        ),
        "criteria": (
            "① 代码必须可直接运行，生成的文件落盘到任务 project 目录；"
            "② 游戏/交互类目标必须可玩、可验证（贯通测试通过）；"
            "③ 依赖仅用标准库或已安装包，额外依赖在注释说明；④ 不输出半成品"
        ),
    },
    "file_io": {
        "role": "文件操作助手",
        "audience": "用户（操作结果需明确可查）",
        "output": "严格 JSON {\"status\": \"success|failed\", \"path\": \"\", \"content\": \"\"}",
        "criteria": "① 按指令读写指定文件；② 路径不得越出任务工作区；③ 中文/英文指令均可",
    },
    "package": {
        "role": "交付打包员",
        "audience": "用户（下载即用）",
        "output": "返回 ZIP 交付包下载链接（Download: file://...）",
        "criteria": "① 只打包本次任务的产物；② 不混入历史任务/并行任务文件；③ 报告与图表纳入交付包",
    },
}


def build_envelope(capability: str, goal: str, hints: list[str] | None = None) -> str:
    """生成步骤信封文本；优先使用注册表覆盖（prompt_registry 自迭代的产物），
    并追加 RAG 检索到的历史提示词改进经验（进化系统反哺）。"""
    ov = load_overrides().get(f"step:{capability}")
    if ov and str(ov.get("prompt") or "").strip():
        base = "\n\n" + str(ov["prompt"]).strip()
    else:
        env = _ENVELOPES.get(capability or "")
        if not env:
            base = (
                "\n\n【任务目标】{goal}\n"
                "【要求】严格围绕任务目标执行，只输出与主题直接相关的结果；"
                "不确定的数据标注来源与口径。"
            ).format(goal=(goal or "")[:300])
        else:
            base = (
                "\n\n【角色】{role}。\n"
                "【受众】{audience}。\n"
                "【输出要求】{output}。\n"
                "【质量标准】{criteria}。"
            ).format(
                role=env["role"],
                audience=env["audience"],
                output=env["output"],
                criteria=env["criteria"],
            )
    # RAG 历史改进经验：只取与本能力相关的记录，追加为执行注意点
    related = [
        h for h in (hints or [])
        if f"step:{capability}" in h or f"（{capability}" in h
    ]
    if related:
        base += (
            "\n【历史改进经验（RAG）】以下来自此前同类任务的反思/自迭代，"
            "本次执行必须避免重蹈覆辙：\n"
            + "\n".join(f"- {h[:300]}" for h in related[:2])
        )
    return base
