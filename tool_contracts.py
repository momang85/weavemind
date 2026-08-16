# -*- coding: utf-8 -*-
"""工具契约与注册表（对标标准 C3-3.1：工具定义/输出格式约束/引导-校验-重试）。

- 每个 worker 能力定义 输入描述 + 返回契约（最小字段校验）；
- 编排器在派发后按契约校验结果，不合格时把错误喂回指令重试（不再盲目重发）；
- TOOL_REGISTRY 供规划器/未来 MCP 动态发现工具。
"""

import json


TOOL_REGISTRY = [
    {
        "name": "web_search",
        "description": "联网检索，返回与主题直接相关的来源列表（含原始 URL）。用于需要最新外部事实（市场数据/新闻/价格/真实仓库）的目标。",
        "parameters": {"instruction": "检索要求与主题"},
        "returns": "JSON 数组：[{\"title\": \"标题\", \"url\": \"原始链接\", \"snippet\": \"摘要\"}]",
        "required": ["url", "title"],
    },
    {
        "name": "web_fetch",
        "description": "抓取指定 URL 的正文文本。用于读取上一步搜索到的具体页面。",
        "parameters": {"instruction": "含 [URL: ...]"},
        "returns": "JSON：{\"status\": \"success|failed\", \"url\": \"\", \"title\": \"\", \"text\": \"正文\"}",
        "required": ["status"],
    },
    {
        "name": "data_loader",
        "description": "按指令 URL 或内置数据集加载数据并落盘到任务 data 目录。",
        "parameters": {"instruction": "数据 URL 或数据集名"},
        "returns": "JSON：{\"status\": \"downloaded|loaded_sklearn|failed\", \"path\": \"\", \"rows\": 0, \"cols\": 0}",
        "required": ["status"],
    },
    {
        "name": "data_analyzer",
        "description": "EDA：输出数据形状/缺失/相关性热力图/分布图（图表落盘）。",
        "parameters": {"instruction": "含 [Data: 路径]"},
        "returns": "JSON：{\"status\": \"success|failed\", \"charts\": [\"路径\"]}",
        "required": ["status"],
    },
    {
        "name": "model_trainer",
        "description": "训练/测试划分并输出模型指标（RMSE/R²）与特征重要性。",
        "parameters": {"instruction": "含 [Data: 路径]"},
        "returns": "JSON：{\"status\": \"success|failed\", \"models\": {\"模型\": {\"RMSE\": 0, \"R2\": 0}}}",
        "required": ["status", "models"],
    },
    {
        "name": "content_summary",
        "description": "内容总结/提炼/生成报告：结构化 Markdown（总体摘要/关键发现/数据要点/建议）。",
        "parameters": {"instruction": "总结要求与前序结果"},
        "returns": "Markdown 文本；有可靠数值时附 [CHART_DATA] 规格 JSON",
        "required": [],
        "min_len": 10,
    },
    {
        "name": "report_generator",
        "description": "生成可交付的 Markdown 报告（概述/正文/关键数据一览/数据来源附录）。",
        "parameters": {"instruction": "报告要求与前序结果"},
        "returns": "Markdown 报告文本",
        "required": [],
        "min_len": 20,
    },
    {
        "name": "code_execution",
        "description": "生成并运行 Python 脚本或自包含单文件 HTML（游戏/工具/分析脚本）。",
        "parameters": {"instruction": "代码要求与验收标准"},
        "returns": "可运行代码文件（.py/.html）或 JSON {\"status\": \"success\", \"path\": \"\"}",
        "required": [],
    },
    {
        "name": "file_io",
        "description": "在任务工作区内读写文件（按指令提取文件名）。",
        "parameters": {"instruction": "读写要求与文件名"},
        "returns": "JSON：{\"status\": \"success|failed\", \"path\": \"\", \"content\": \"\"}",
        "required": ["status"],
    },
    {
        "name": "package",
        "description": "把本次任务产物打包为 ZIP 交付包。",
        "parameters": {"instruction": "打包要求"},
        "returns": "文本，含 Download: file://<zip 路径>",
        "required": [],
        "must_contain": "Download: file://",
    },
    {
        "name": "react_agent",
        "description": "运行时 ReAct Agent：根据中间结果反复调用工具（搜索/抓取/总结/代码）直到完成任务。用于多轮调研、需要迭代核对的任务。",
        "parameters": {"instruction": "任务目标与要求"},
        "returns": "最终答案文本",
        "required": [],
        "min_len": 1,
    },
]


def tool_catalog_text() -> str:
    """规划器可用的工具目录（紧凑，FC 风格工具定义）。"""
    lines = ["## 工具目录（能力定义）"]
    for t in TOOL_REGISTRY:
        lines.append(f"- {t['name']}: {t['description']} 返回: {t['returns']}")
    return "\n".join(lines)


def _parse(raw) -> object:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return str(raw or "")


def validate_result(capability: str, raw) -> tuple[bool, list[str]]:
    """按能力返回契约校验 worker 输出。返回 (是否通过, 问题列表)。"""
    tool = next((t for t in TOOL_REGISTRY if t["name"] == capability), None)
    if not tool:
        return True, []
    data = _parse(raw)
    issues: list[str] = []
    if isinstance(data, list):
        if not data:
            issues.append("返回空列表（检索无结果）")
        for it in data[:5]:
            if not isinstance(it, dict):
                issues.append("列表元素非对象")
                break
            for f in tool.get("required", []):
                if not str(it.get(f) or "").strip():
                    issues.append(f"列表元素缺少 {f}")
                    break
    elif isinstance(data, dict):
        for f in tool.get("required", []):
            if f == "models":
                if not isinstance(data.get(f), dict) or not data[f]:
                    issues.append("缺少 models 指标")
            elif not str(data.get(f) or "").strip():
                issues.append(f"缺少 {f}")
    else:
        text = str(data or "")
        if tool.get("min_len") and len(text) < tool["min_len"]:
            issues.append(f"输出过短（{len(text)} < {tool['min_len']} 字符）")
        if tool.get("must_contain") and tool["must_contain"] not in text:
            issues.append(f"输出缺少 {tool['must_contain']}")
    return (not issues, issues[:5])
