import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch


def race_probe(store, scope, kind):
    barrier = threading.Barrier(2, timeout=8)
    thread_state = threading.local()
    write_lock = threading.Lock()
    reader_name = "load_watchlist" if kind == "watchlist" else "load_cost"
    original_reader = getattr(store, reader_name)
    original_writer = store._write_one

    def synchronized_reader(target_scope):
        with write_lock:
            result = original_reader(target_scope)
        if not getattr(thread_state, "first_read_done", False):
            thread_state.first_read_done = True
            barrier.wait()
        return result

    def serialized_writer(*args, **kwargs):
        with write_lock:
            return original_writer(*args, **kwargs)

    def mutate(index):
        if kind == "watchlist":
            return store.watchlist_mutate(
                scope=scope, action="add", code=f"{index:06d}.SZ", name="synthetic")
        return store.record_cost(scope, runs_total=1)

    with patch.object(store, reader_name, side_effect=synchronized_reader), \
            patch.object(store, "_write_one", side_effect=serialized_writer), \
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(mutate, (1, 2)))
    data, errors = original_reader(scope)
    if errors:
        raise RuntimeError(errors)
    actual = len(data["items"]) if kind == "watchlist" else data["runs_total"]
    return {"probe": f"{kind}_concurrent_read_modify_write", "expected": 2,
            "actual": actual, "lost_update": actual != 2,
            "method": "both requests read before either writes; byte reads and writes serialized",
            "operation_errors": [result[1] for result in results] if kind == "watchlist" else []}


def main():
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix="weavemind_architect_1024_") as directory:
        isolated = Path(directory)
        for key, value in {
            "WEAVEMIND_TEST_MODE": "1",
            "WEAVEMIND_DATA_DIR": str(isolated / "data"),
            "WEAVEMIND_LOG_DIR": str(isolated / "logs"),
            "MEMORY_DIR": str(isolated / "memory"),
            "WEAVEMIND_SCREEN_DIR": str(isolated / "screen"),
            "WEAVEMIND_SCREEN_CACHE": str(isolated / "missing-sample.json"),
        }.items():
            os.environ[key] = value
        import screen_store as store

        watch_scope = {"user": "probe-watch", "project": "personal"}
        cost_scope = {"user": "probe-cost", "project": "personal"}
        findings = [race_probe(store, watch_scope, "watchlist"),
                    race_probe(store, cost_scope, "cost")]
        corrupt_scope = {"user": "probe-corrupt", "project": "personal"}
        store.record_cost(corrupt_scope, runs_total=7)
        ledger = Path(store._cost_path(corrupt_scope))
        broken_bytes = b'{"runs_total":7, invalid'
        ledger.write_bytes(broken_bytes)
        store.record_cost(corrupt_scope, runs_total=1)
        recovered, errors = store.load_cost(corrupt_scope)
        findings.append({"probe": "corrupt_ledger_overwrite",
                         "original_bytes_preserved": ledger.read_bytes() == broken_bytes,
                         "runs_total_after": recovered["runs_total"],
                         "warnings_after": recovered.get("warnings", []),
                         "errors_after": errors})
        report = {"date": "2026-10-24", "runtime": sys.version.split()[0],
                  "scope": "synthetic offline probes; temporary storage; not full acceptance",
                  "fingerprints": {
                      name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                      for name in ("screen_store.py", "web_ui.py", "stock_screen.py")},
                  "findings": findings}
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
