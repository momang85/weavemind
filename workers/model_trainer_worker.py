"""Model Trainer Worker — train+eval, no LLM needed."""
import asyncio, json, tempfile, tempfile, time
from pathlib import Path
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.linear_model import LinearRegression
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_squared_error, r2_score
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from async_worker_base import AsyncWorkerBase

class ModelTrainerWorker(AsyncWorkerBase):
    _class_capabilities = ["model_trainer"]
    _needs_task = True

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        try:
            import re
            paths = re.findall(
                r'[A-Za-z]:[\\/][^\s,]+\.(?:csv|xlsx|json)|/tmp/[^\s,]+\.(?:csv|xlsx|json)',
                instruction,
            )
            data_dir = Path(tempfile.gettempdir()) / "agent_workspace" / "data"
            if task and task.get("workspace"):
                data_dir = Path(str(task["workspace"])) / "data"
                data_dir.mkdir(parents=True, exist_ok=True)
            if paths:
                fpath = Path(paths[0])
            else:
                keywords = ("训练", "建模", "模型", "预测", "回归", "csv", "数据集", "数据")
                instruction_l = instruction.lower()
                if not any(k in instruction_l for k in keywords):
                    return json.dumps({"status": "failed", "error": "No data path provided in instruction"}, ensure_ascii=False)
                now = time.time()
                fresh = [
                    p for p in data_dir.glob("*.csv")
                    if now - p.stat().st_mtime < 3600
                ]
                if not fresh:
                    return json.dumps({"status": "failed", "error": "No fresh CSV found in workspace"}, ensure_ascii=False)
                fpath = max(fresh, key=lambda p: p.stat().st_mtime)

            df = pd.read_csv(fpath)
            # Assume last column is target
            target_col = df.columns[-1]
            X = df.drop(columns=[target_col]).select_dtypes(include=["number"])
            y = df[target_col]

            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

            results = {}
            # Linear Regression
            lr = LinearRegression()
            lr.fit(X_train, y_train)
            y_pred_lr = lr.predict(X_test)
            rmse_lr = mean_squared_error(y_test, y_pred_lr) ** 0.5
            r2_lr = r2_score(y_test, y_pred_lr)
            results["LinearRegression"] = {"RMSE": round(rmse_lr, 4), "R2": round(r2_lr, 4)}

            # Random Forest
            rf = RandomForestRegressor(n_estimators=50, random_state=42, n_jobs=-1)
            rf.fit(X_train, y_train)
            y_pred_rf = rf.predict(X_test)
            rmse_rf = mean_squared_error(y_test, y_pred_rf) ** 0.5
            r2_rf = r2_score(y_test, y_pred_rf)
            results["RandomForest"] = {"RMSE": round(rmse_rf, 4), "R2": round(r2_rf, 4)}

            # Feature importance
            importance = list(zip(X.columns.tolist(), rf.feature_importances_.tolist()))
            importance.sort(key=lambda x: x[1], reverse=True)

            return json.dumps({
                "status": "success",
                "data_path": str(fpath),
                "target": target_col,
                "features": X.columns.tolist(),
                "samples": len(df),
                "models": results,
                "feature_importance": importance[:5],
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"status": "failed", "error": str(e)})


if __name__ == "__main__":
    import asyncio, sys, os
    from logging_setup import setup_logging
    setup_logging("worker-model-trainer")
    agent_id = sys.argv[1] if len(sys.argv) > 1 else "modeltrainerworker"
    from async_worker_base import AsyncRegistry, AsyncMessaging
    reg = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    msg = AsyncMessaging(os.environ.get("REDIS_HOST", "localhost"), int(os.environ.get("REDIS_PORT", "6379")))
    async def run():
        worker = ModelTrainerWorker(agent_id=agent_id, capabilities=ModelTrainerWorker._class_capabilities, registry=reg, messaging=msg)
        await worker.run()
    asyncio.run(run())
