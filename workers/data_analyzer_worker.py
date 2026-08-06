"""Data Analyzer Worker — EDA with 3 charts, no LLM needed."""
import asyncio, json, os, tempfile
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from async_worker_base import AsyncWorkerBase

CHART_DIR = Path(tempfile.gettempdir()) / "agent_workspace" / "charts"
pass

class DataAnalyzerWorker(AsyncWorkerBase):
    _class_capabilities = ["data_analyzer"]

    async def execute(self, instruction: str) -> str:
        try:
            # Find data path from instruction or use latest CSV in workspace
            import re
            paths = re.findall(r'/tmp/[^\s,]+\.(csv|xlsx|json)', instruction)
            data_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "data"
            if paths:
                fpath = Path(paths[0].replace("\\", "/"))
            else:
                csvs = sorted(data_dir.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not csvs:
                    return json.dumps({"status": "failed", "error": "No CSV found in workspace"})
                fpath = csvs[0]

            df = pd.read_csv(fpath)
            shape = list(df.shape)
            cols = list(df.columns)
            missing = df.isnull().sum().to_dict()
            numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
            target_col = df.columns[-1]  # assume last column is target

            # Chart 1: Numeric columns distribution histogram
            fig1, axes = plt.subplots(1, min(3, len(numeric_cols)), figsize=(12, 4))
            if len(numeric_cols) == 1:
                axes = [axes]
            for ax, col in zip(axes, numeric_cols[:3]):
                df[col].hist(ax=ax, bins=30, alpha=0.7)
                ax.set_title(col)
            plt.tight_layout()
            chart1 = str(CHART_DIR / "histograms.png")
            fig1.savefig(chart1, dpi=100)
            plt.close(fig1)

            # Chart 2: Correlation heatmap
            fig2, ax2 = plt.subplots(figsize=(10, 8))
            corr = df[numeric_cols].corr()
            sns.heatmap(corr, annot=True, fmt=".2f", cmap="coolwarm", ax=ax2)
            plt.tight_layout()
            chart2 = str(CHART_DIR / "heatmap.png")
            fig2.savefig(chart2, dpi=100)
            plt.close(fig2)

            # Chart 3: Target vs top feature scatter
            if len(numeric_cols) >= 2:
                top_feat = corr[target_col].drop(target_col).abs().idxmax()
                fig3, ax3 = plt.subplots(figsize=(8, 6))
                ax3.scatter(df[top_feat], df[target_col], alpha=0.5)
                ax3.set_xlabel(top_feat); ax3.set_ylabel(target_col)
                ax3.set_title(f"{target_col} vs {top_feat}")
                plt.tight_layout()
                chart3 = str(CHART_DIR / "scatter.png")
                fig3.savefig(chart3, dpi=100)
                plt.close(fig3)
            else:
                chart3 = ""

            return json.dumps({
                "status": "success",
                "shape": shape,
                "columns": cols,
                "missing": missing,
                "target": target_col,
                "charts": [chart1, chart2, chart3],
                "data_path": str(fpath),
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "failed", "error": str(e)})


if __name__ == "__main__":
    import asyncio, sys, os
    from logging_setup import setup_logging
    setup_logging("worker-data-analyzer")
    agent_id = sys.argv[1] if len(sys.argv) > 1 else "dataanalyzerworker"
    from async_worker_base import AsyncRegistry, AsyncMessaging
    reg = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    msg = AsyncMessaging(os.environ.get("REDIS_HOST", "localhost"), int(os.environ.get("REDIS_PORT", "6379")))
    async def run():
        worker = DataAnalyzerWorker(agent_id=agent_id, capabilities=DataAnalyzerWorker._class_capabilities, registry=reg, messaging=msg)
        await worker.run()
    asyncio.run(run())
