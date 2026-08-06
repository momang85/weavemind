"""Data Loader Worker — downloads dataset, no LLM needed."""
import asyncio, json, tempfile, os, sys
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from async_worker_base import AsyncWorkerBase

WORKSPACE = Path(tempfile.gettempdir()) / "agent_workspace" / "data"
WORKSPACE.mkdir(parents=True, exist_ok=True)

class DataLoaderWorker(AsyncWorkerBase):
    _class_capabilities = ["data_loader"]

    async def execute(self, instruction: str) -> str:
        try:
            # Extract URL from instruction
            import re
            urls = re.findall(r'https?://[^\s,]+', instruction)
            
            if urls:
                url = urls[0]
                import requests
                fname = url.split("/")[-1].split("?")[0] or "dataset.csv"
                fpath = WORKSPACE / fname
                r = requests.get(url, stream=True, timeout=120)
                r.raise_for_status()
                with open(fpath, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                return json.dumps({"status": "downloaded", "path": str(fpath), "url": url, "size": fpath.stat().st_size})

            # Fallback: sklearn built-in dataset
            import pandas as pd
            for name in ["fetch_california_housing", "load_diabetes", "load_iris"]:
                try:
                    from sklearn import datasets as skds
                    fn = getattr(skds, name)
                    data = fn()
                    cols = [str(c) for c in data.feature_names] if hasattr(data.feature_names, '__iter__') else []
                    df = pd.DataFrame(data.data, columns=cols)
                    if hasattr(data, "target"):
                        df["target"] = data.target
                    fpath = WORKSPACE / f"{name}_data.csv"
                    df.to_csv(fpath, index=False)
                    return json.dumps({"status": "loaded_sklearn", "dataset": name, "path": str(fpath), "rows": len(df), "cols": len(df.columns)})
                except Exception as _e:
                    import traceback; traceback.print_exc()
                    continue

            return json.dumps({"status": "failed", "error": "No URL found and no sklearn fallback"})
        except Exception as e:
            import traceback; traceback.print_exc()
        return json.dumps({"status": "failed", "error": str(e)})


if __name__ == "__main__":
    import asyncio, sys, os
    from logging_setup import setup_logging
    setup_logging("worker-data-loader")
    agent_id = sys.argv[1] if len(sys.argv) > 1 else "dataloaderworker"
    from async_worker_base import AsyncRegistry, AsyncMessaging
    reg = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    msg = AsyncMessaging(os.environ.get("REDIS_HOST", "localhost"), int(os.environ.get("REDIS_PORT", "6379")))
    async def run():
        worker = DataLoaderWorker(agent_id=agent_id, capabilities=DataLoaderWorker._class_capabilities, registry=reg, messaging=msg)
        await worker.run()
    asyncio.run(run())
