"""织光 (ZhiGuang) - File I/O Worker（受限工作区内的文件读写）。"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

logger = logging.getLogger(__name__)


class FileIoWorker(AsyncWorkerBase):
    """在受限工作区内执行文件读写操作。"""

    _class_capabilities = ["file_io"]
    WORKSPACE_DIR = os.path.join(os.path.expanduser("~"), ".weavemind_workspace")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        os.makedirs(self.WORKSPACE_DIR, exist_ok=True)

    def _safe_path(self, filename: str) -> Path:
        """将文件名限定在工作区内，防止路径穿越。"""
        base = Path(self.WORKSPACE_DIR).resolve()
        path = (base / filename).resolve()
        if not str(path).startswith(str(base)):
            raise ValueError(f"Path escapes workspace: {filename}")
        return path

    async def execute(self, instruction: str) -> str:
        try:
            lower = instruction.lower()
            if "read" in lower and "log" in lower:
                return await self._handle_read_logs(instruction)
            if "write" in lower:
                return await self._handle_write_file(instruction)
            if "read" in lower:
                return await self._handle_read_file(instruction)
            return await self._handle_unknown_instruction(instruction)
        except Exception as exc:
            logger.error("File operation error: %s", exc)
            raise RuntimeError(f"File operation error: {exc}") from exc

    async def _handle_read_logs(self, instruction: str) -> str:
        resp = await self._call_llm(
            instruction=(
                f"Extract the number of recent log files to read from this instruction: "
                f"'{instruction}'. Return only the number as integer."
            )
        )
        try:
            num_files = min(max(int(resp.strip()), 1), 10)
        except ValueError:
            num_files = 5

        log_dir = Path(self.WORKSPACE_DIR) / "task_logs"
        if not log_dir.exists():
            return "No task logs directory found."

        files = sorted(
            [f for f in log_dir.glob("*.log")],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:num_files]
        if not files:
            return "No log files found."

        parts = []
        for f in files:
            try:
                parts.append(f"=== {f.name} ===\n{f.read_text(encoding='utf-8', errors='replace')}")
            except Exception as exc:
                parts.append(f"Error reading {f.name}: {exc}")
        return "\n\n".join(parts)

    async def _handle_read_file(self, instruction: str) -> str:
        resp = await self._call_llm(
            instruction=(
                f"Extract the filename from this instruction: '{instruction}'. "
                "Return only the filename without path."
            )
        )
        filename = resp.strip().strip('"').strip("'")
        try:
            path = self._safe_path(filename)
        except ValueError as exc:
            return str(exc)
        if not path.exists() or not path.is_file():
            return f"File not found: {filename}"
        return path.read_text(encoding="utf-8", errors="replace")

    async def _handle_write_file(self, instruction: str) -> str:
        resp = await self._call_llm(
            instruction=(
                f"Extract filename and content from this instruction: '{instruction}'. "
                "Return as JSON with 'filename' and 'content' keys."
            )
        )
        try:
            data = json.loads(resp)
            filename = data["filename"]
            content = data["content"]
        except (json.JSONDecodeError, KeyError):
            return "Failed to parse filename and content from instruction."
        try:
            path = self._safe_path(filename)
        except ValueError as exc:
            return str(exc)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        return f"Successfully wrote to {filename}"

    async def _handle_unknown_instruction(self, instruction: str) -> str:
        return await self._call_llm(
            instruction=(
                f"Explain that file operations are limited to reading/writing within "
                f"{self.WORKSPACE_DIR} based on this instruction: '{instruction}'. Keep brief."
            )
        )


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-file-io")
    registry = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    messaging = AsyncMessaging(
        os.environ.get("REDIS_HOST", "localhost"),
        int(os.environ.get("REDIS_PORT", "6379")),
    )
    worker = FileIoWorker(
        agent_id="fileioworker",
        capabilities=FileIoWorker._class_capabilities,
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
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
