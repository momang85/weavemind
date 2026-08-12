"""Report Generator Worker - 真正的报告撰写器：按指令用 LLM 生成 Markdown 文档并落盘。"""

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase

REPORT_DIR = Path(tempfile.gettempdir()) / "agent_workspace" / "reports"


class ReportGeneratorWorker(AsyncWorkerBase):
    _class_capabilities = ["report_generator"]
    _needs_task = True

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        charts_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "charts"
        data_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "data"
        report_dir = REPORT_DIR
        if task and task.get("workspace"):
            ws = Path(str(task["workspace"]))
            charts_dir = ws / "charts"
            data_dir = ws / "data"
            report_dir = ws / "reports"
            for d in (charts_dir, data_dir, report_dir):
                d.mkdir(parents=True, exist_ok=True)
        # 只考虑本次任务时间窗口内的产物，避免把历史任务遗留的无关数据（如房价）
        # 拉进当前报告。
        cutoff = time.time() - 120 * 60
        charts = [
            c for c in (charts_dir.glob("*.png") if charts_dir.exists() else [])
            if c.stat().st_mtime >= cutoff
        ]
        # 代码执行生成的图表（project/*.png）同样纳入，供报告嵌入
        if task and task.get("workspace"):
            proj_dir = Path(str(task["workspace"])) / "project"
            if proj_dir.exists():
                charts += [
                    c for c in proj_dir.rglob("*.png")
                    if "screenshots" not in c.parts
                    if c.stat().st_mtime >= cutoff
                ]
        # 去重（同名文件可能同时出现在 charts/ 与 project/）
        seen_charts: set[str] = set()
        charts = [c for c in charts if not (str(c) in seen_charts or seen_charts.add(str(c)))]
        data_csvs = [
            d for d in (data_dir.glob("*.csv") if data_dir.exists() else [])
            if d.stat().st_mtime >= cutoff
        ]

        data_info = ""
        for d in data_csvs[:3]:
            try:
                import pandas as pd
                df = pd.read_csv(d)
                data_info += f"- **{d.name}**: {df.shape[0]} rows, {df.shape[1]} cols\n"
            except Exception:
                data_info += f"- **{d.name}**: file exists ({d.stat().st_size} bytes)\n"

        try:
            artifacts = (
                f"可用图表：{', '.join(c.name for c in charts) or '无'}\n"
                f"可用数据：\n{data_info or '无'}"
            )
            system = (
                "你是专业报告撰写者。根据指令生成一份完整、具体、可直接交付的 Markdown 文档。"
                "要求：结构清晰（使用标题/表格/列表），内容详实而非占位符，"
                "严格围绕任务主题，语言流畅。直接输出 Markdown 正文，不要额外说明。"
                "重要：工作区列出的图表/数据文件若与本任务主题无关（例如游戏任务中出现房价数据集），"
                "一律不得使用，只能使用上一步结果中与任务主题直接相关的信息。"
                "若工作区存在与任务主题相关的图表文件（PNG），必须在报告中以"
                "![图表说明](图表的绝对路径) 形式嵌入，并标注数据来源。"
            )
            user = f"{instruction}\n\n工作区产物：\n{artifacts}"
            # 主端点连试 2 次即切备用，减少慢端点对报告环节的拖累
            report = await self._call_llm(system=system, prompt=user, max_attempts=2)
            report = report.strip()
            if len(report) < 100:
                raise RuntimeError("Generated report too short")
            if not report.startswith("#"):
                report = "# 报告\n\n" + report

            # 数据来源附录：从指令中的 [数据来源] 块提取 URL 并去重
            src_urls: list[str] = []
            m = re.search(r"\[数据来源\](.*)", str(instruction), re.S)
            if m:
                for u in re.findall(r"https?://[^\s\)\]]+", m.group(1)):
                    if u not in src_urls:
                        src_urls.append(u)
            if src_urls:
                report += (
                    "\n\n## 数据来源\n\n"
                    + "\n".join(f"- [{u}]({u})" for u in src_urls[:15])
                )
            # 确定性图表嵌入：不依赖 LLM 自觉，直接追加"## 图表"段落
            if charts:
                report += "\n\n## 图表\n\n"
                for c in charts[:6]:
                    report += f"![{c.stem}]({c})\n\n"

            rpath = report_dir / "report.md"
            rpath.write_text(report, encoding="utf-8")
            return json.dumps({
                "status": "success",
                "report_path": str(rpath),
                "charts": len(charts),
                "datasets": len(data_csvs),
                "chars": len(report),
            }, ensure_ascii=False)
        except Exception as exc:
            # 回退：LLM 不可用时输出结构化模板
            chart_md = "".join(f"![{c.stem}]({c})" + "\n\n" for c in charts)
            report = f"""# Data Science Pipeline Report

## Summary
- Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}
- Task: {instruction[:200]}
- Pipeline executed by WeaveMind AI Team

## Data Overview
{data_info}

## Exploratory Data Analysis
{chart_md}

## Model Training Results
The model training results are embedded above. See the feature importance chart for key predictors.

## Conclusion
This report was generated automatically by the WeaveMind multi-agent system.
All data, code, and visualizations are available in the project workspace.
"""
            try:
                rpath = report_dir / "report.md"
                rpath.write_text(report, encoding="utf-8")
                return json.dumps({
                    "status": "success",
                    "report_path": str(rpath),
                    "charts": len(charts),
                    "datasets": len(data_csvs),
                    "fallback": True,
                }, ensure_ascii=False)
            except Exception as e2:
                return json.dumps({"status": "failed", "error": str(e2)})


if __name__ == "__main__":
    import asyncio
    from logging_setup import setup_logging

    setup_logging("worker-report-generator")
    agent_id = sys.argv[1] if len(sys.argv) > 1 else "reportgeneratorworker"
    from async_worker_base import AsyncRegistry, AsyncMessaging

    reg = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    msg = AsyncMessaging(os.environ.get("REDIS_HOST", "localhost"), int(os.environ.get("REDIS_PORT", "6379")))

    async def run():
        worker = ReportGeneratorWorker(
            agent_id=agent_id,
            capabilities=ReportGeneratorWorker._class_capabilities,
            registry=reg,
            messaging=msg,
        )
        await worker.run()

    asyncio.run(run())
