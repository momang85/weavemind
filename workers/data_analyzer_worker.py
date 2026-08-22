"""Data Analyzer Worker — EDA with 3 charts, no LLM needed."""
import asyncio, json, os, tempfile, time
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
    _needs_task = True

    @staticmethod
    def _load_frame(fpath: Path) -> pd.DataFrame:
        """读取数据文件：CSV 直读；JSON（structured_data.json）按
        rows/items/points 数组转 DataFrame，让预载的排行/宏观数据
        无需 CSV 也能做 EDA。"""
        if fpath.suffix.lower() != ".json":
            return pd.read_csv(fpath)
        with fpath.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            payload = data.get("data") or {}
            for key in ("rows", "items", "points"):
                rows = payload.get(key)
                if isinstance(rows, list) and rows:
                    return pd.DataFrame(rows)
            flat = {
                k: v for k, v in payload.items()
                if not isinstance(v, (list, dict)) and v is not None
            }
            if flat:
                return pd.DataFrame([flat])
            raise ValueError(f"{fpath.name} 中没有可消费的行数据")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return pd.DataFrame(data)
        raise ValueError(f"{fpath.name} 不是结构化数据（无法转为 DataFrame）")

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        try:
            chart_dir = CHART_DIR
            data_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "data"
            ws = Path(tempfile.gettempdir()) / "agent_workspace"
            if task and task.get("workspace"):
                ws = Path(str(task["workspace"]))
                chart_dir = ws / "charts"
                data_dir = ws / "data"
                chart_dir.mkdir(parents=True, exist_ok=True)
                data_dir.mkdir(parents=True, exist_ok=True)
            # Find data path from instruction or use latest CSV in workspace
            import re
            paths = re.findall(
                r'[A-Za-z]:[\\/][^\s,]+\.(?:csv|xlsx|json)|/tmp/[^\s,]+\.(?:csv|xlsx|json)',
                instruction,
            )
            if paths:
                fpath = Path(paths[0].replace("\\", "/"))
            else:
                # 预载结构化数据优先：ranking.csv / structured_data.json 是本次任务
                # 刚预载的真实数据，即使指令未显式给路径也可直接消费（断链修复）
                candidates: list[Path] = []
                ranking_csv = data_dir / "ranking.csv"
                structured_json = ws / "project" / "structured_data.json"
                if ranking_csv.exists():
                    candidates.append(ranking_csv)
                if structured_json.exists():
                    candidates.append(structured_json)
                if not candidates:
                    # 仅当指令明确涉及数据分析，且工作区存在 1 小时内的新 CSV 时才兜底，
                    # 避免把历史任务遗留的无关数据（如加州房价）拉进当前任务。
                    keywords = ("分析", "数据", "csv", "数据集", "eda", "统计", "建模", "训练", "房价", "预测", "回归")
                    instruction_l = instruction.lower()
                    if not any(k in instruction_l for k in keywords):
                        return json.dumps({"status": "failed", "error": "No data path provided in instruction"}, ensure_ascii=False)
                    now = time.time()
                    candidates = [
                        p for p in data_dir.glob("*.csv")
                        if now - p.stat().st_mtime < 3600
                    ]
                if not candidates:
                    return json.dumps({"status": "failed", "error": "No fresh CSV found in workspace"}, ensure_ascii=False)
                fpath = candidates[0]

            df = self._load_frame(fpath)
            shape = list(df.shape)
            cols = list(df.columns)
            missing = df.isnull().sum().to_dict()
            numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()
            target_col = df.columns[-1]  # assume last column is target
            if len(df) < 2 or len(numeric_cols) < 2:
                # 单行/无数值列（如单点行情快照）：无法做相关性/散点，
                # 如实返回成功但无图，避免 worker 内部异常
                return json.dumps({
                    "status": "success",
                    "shape": shape,
                    "columns": cols,
                    "missing": missing,
                    "target": target_col,
                    "charts": [],
                    "data_path": str(fpath),
                }, ensure_ascii=False)

            # Chart 1: Numeric columns distribution histogram
            fig1, axes = plt.subplots(1, min(3, len(numeric_cols)), figsize=(12, 4))
            if len(numeric_cols) == 1:
                axes = [axes]
            for ax, col in zip(axes, numeric_cols[:3]):
                df[col].hist(ax=ax, bins=30, alpha=0.7)
                ax.set_title(col)
            plt.tight_layout()
            chart1 = str(chart_dir / "histograms.png")
            fig1.savefig(chart1, dpi=100)
            plt.close(fig1)

            # Chart 2: Correlation heatmap
            fig2, ax2 = plt.subplots(figsize=(10, 8))
            corr = df[numeric_cols].corr()
            sns.heatmap(corr, annot=True, fmt=".2f", cmap="coolwarm", ax=ax2)
            plt.tight_layout()
            chart2 = str(chart_dir / "heatmap.png")
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
                chart3 = str(chart_dir / "scatter.png")
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
