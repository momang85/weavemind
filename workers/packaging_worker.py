"""
织光 (ZhiGuang) — PackagingWorker

能力标签: [package]
职责: 将项目文件夹打包成 ZIP 文件供下载。
"""

import os, sys, shutil, logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

logger = logging.getLogger(__name__)

OUTPUT_DIR = os.environ.get("PACKAGE_OUTPUT_DIR", "/tmp/agent_workspace")
STATIC_DIR = os.environ.get("STATIC_DIR", "/tmp/agent_packages")


class PackagingWorker(AsyncWorkerBase):
    """打包交付 Worker。将项目文件夹打成 ZIP。"""

    def __init__(self, **kwargs):
        super().__init__(
            agent_id=kwargs.pop("agent_id", "packaging_worker"),
            capabilities=["package"],
            **kwargs,
        )
        os.makedirs(STATIC_DIR, exist_ok=True)

    async def execute(self, instruction: str) -> str:
        import asyncio
        loop = asyncio.get_running_loop()

        def _sync():
            from llm_client import call_llm

            # Parse project path from instruction
            system = (
                "你是路径解析器。从指令中提取项目路径。"
                '输出JSON: {"project_path": "/tmp/agent_workspace/project_name"}'
                "如果指令未指定路径，默认使用 /tmp/agent_workspace。只输出JSON。"
            )
            try:
                result = call_llm(system, instruction, expect_json=True)
                proj_path = result.get("project_path", OUTPUT_DIR)
            except Exception:
                proj_path = OUTPUT_DIR

            return self._package(proj_path)

        return await loop.run_in_executor(None, _sync)

    def _package(self, proj_path: str) -> str:
        if not os.path.isdir(proj_path):
            raise RuntimeError(f"Project path not found: {proj_path}")

        proj_name = os.path.basename(proj_path.rstrip("/\\"))
        zip_name = f"{proj_name}.zip"
        zip_path = os.path.join(STATIC_DIR, zip_name)

        try:
            shutil.make_archive(
                os.path.join(STATIC_DIR, proj_name),
                'zip',
                proj_path,
            )
            size_kb = os.path.getsize(zip_path) / 1024

            # Count files
            file_count = sum(1 for _ in self._walk_files(proj_path))

            return (
                f"[PACKAGED] {zip_name} ({size_kb:.1f} KB, {file_count} files)\n"
                f"Download: file://{zip_path}\n"
                f"Project: {proj_path}"
            )
        except Exception as exc:
            raise RuntimeError(f"Package failed: {exc}") from exc

    def _walk_files(self, path):
        for root, _, files in os.walk(path):
            for f in files:
                yield os.path.join(root, f)


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-packaging")
    redis_host = os.environ.get("REDIS_HOST", "localhost")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    db_path = os.environ.get("REGISTRY_DB", "agents.db")

    registry = AsyncRegistry(db_path)
    messaging = AsyncMessaging(redis_host, redis_port)

    worker = PackagingWorker(
        agent_id="packaging_worker",
        registry=registry,
        messaging=messaging,
        max_concurrency=3,
    )

    try:
        await worker.run()
    except KeyboardInterrupt:
        await worker.shutdown()


def main():
    try:
        import asyncio
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
