"""Report Generator Worker — Markdown report from templates, no LLM needed."""
import asyncio, json, tempfile, tempfile, time
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from async_worker_base import AsyncWorkerBase

REPORT_DIR = Path(tempfile.gettempdir()) / "agent_workspace" / "reports"
pass

class ReportGeneratorWorker(AsyncWorkerBase):
    _class_capabilities = ["report_generator"]

    async def execute(self, instruction: str) -> str:
        try:
            # Collect available artifacts
            charts_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "charts"
            data_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "data"
            charts = sorted(charts_dir.glob("*.png")) if charts_dir.exists() else []
            data_csvs = sorted(data_dir.glob("*.csv")) if data_dir.exists() else []

            chart_md = ""
            for c in charts:
                chart_md += f"![{c.stem}]({c})\n\n"

            data_info = ""
            for d in data_csvs[:3]:
                try:
                    import pandas as pd
                    df = pd.read_csv(d)
                    data_info += f"- **{d.name}**: {df.shape[0]} rows, {df.shape[1]} cols\n"
                except Exception:
                    data_info += f"- **{d.name}**: file exists ({d.stat().st_size} bytes)\n"

            # Build report from template
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

            rpath = REPORT_DIR / "report.md"
            rpath.write_text(report, encoding="utf-8")
            return json.dumps({
                "status": "success",
                "report_path": str(rpath),
                "charts": len(charts),
                "datasets": len(data_csvs),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "failed", "error": str(e)})


if __name__ == "__main__":
    import asyncio, sys, os
    from logging_setup import setup_logging
    setup_logging("worker-report-generator")
    agent_id = sys.argv[1] if len(sys.argv) > 1 else "reportgeneratorworker"
    from async_worker_base import AsyncRegistry, AsyncMessaging
    reg = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    msg = AsyncMessaging(os.environ.get("REDIS_HOST", "localhost"), int(os.environ.get("REDIS_PORT", "6379")))
    async def run():
        worker = ReportGeneratorWorker(agent_id=agent_id, capabilities=ReportGeneratorWorker._class_capabilities, registry=reg, messaging=msg)
        await worker.run()
    asyncio.run(run())
