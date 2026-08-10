"""织光 (ZhiGuang) - Packaging Worker

将本次任务的真实交付物打包为 ZIP：以共享 project 工作区（code_execution /
file_io 的落盘目录）为基础，只打包时间窗口内的新文件，避免把历史任务
的陈旧产物混入交付包。
"""

import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

PROJECT_DIR = Path(tempfile.gettempdir()) / "agent_workspace" / "project"
REPORT_DIR = Path(tempfile.gettempdir()) / "agent_workspace" / "reports"
STATIC_DIR = Path(os.environ.get("PACKAGE_OUTPUT_DIR", str(Path(tempfile.gettempdir()) / "agent_packages")))
FRESH_MINUTES = int(os.environ.get("PACKAGE_FRESH_MINUTES", "120"))


class PackagingWorker(AsyncWorkerBase):
    """打包交付 Worker：将项目工作区中的新产物打成 ZIP。"""

    _class_capabilities = ["package"]
    _needs_task = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        STATIC_DIR.mkdir(parents=True, exist_ok=True)

    async def execute(self, instruction: str, task: dict | None = None) -> str:
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._sync_package, instruction, task or {},
        )

    def _sync_package(self, instruction: str, task: dict | None = None) -> str:
        from llm_client import call_llm

        proj_path = None
        try:
            system = (
                "你是路径解析器。从指令中提取项目路径。"
                '输出JSON: {"project_path": "/path/to/project"}。'
                "如果指令未指定路径，输出空对象 {}。只输出JSON。"
            )
            result = call_llm(system, instruction, expect_json=True)
            p = str(result.get("project_path") or "").strip()
            if p and os.path.isdir(p):
                proj_path = Path(p)
        except Exception:
            pass
        if proj_path is None:
            proj_path = (
                Path(str(task["workspace"])) / "project"
                if task and task.get("workspace")
                else PROJECT_DIR
            )
        return self._package(proj_path, task or {})

    def _fresh_files(self, root: Path, task: dict) -> list[tuple[Path, str]]:
        """返回 (绝对路径, 相对路径) 且属于本次任务的文件：
        mtime 在任务开始之后（或窗口内），避免把历史任务/并行任务的产物混进交付包。"""
        cutoff = time.time() - FRESH_MINUTES * 60
        try:
            task_start = float(task.get("task_start_ts") or 0)
            lower_bound = task_start - 60  # 允许 60s 缓冲
        except (TypeError, ValueError):
            lower_bound = cutoff
        lower_bound = max(lower_bound, cutoff)
        files: list[tuple[Path, str]] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            try:
                mt = p.stat().st_mtime
                if mt < lower_bound:
                    continue
            except OSError:
                continue
            rel = p.relative_to(root).as_posix()
            # 排除编译缓存、临时校验文件与截图证据（截图在前端/报告中展示，不入交付包）
            if (
                rel.startswith("__pycache__/")
                or "/__pycache__/" in rel
                or "_check_" in rel
                or rel.startswith(".test_")
                or rel.startswith("screenshots/")
                or "/screenshots/" in rel
            ):
                continue
            files.append((p, rel))
        # 报告文件独立存放，若新鲜则一并纳入（放在 reports/ 前缀下）
        report_dir = REPORT_DIR
        if task and task.get("workspace"):
            report_dir = Path(str(task["workspace"])) / "reports"
        if report_dir.exists():
            for p in sorted(report_dir.glob("*.md")):
                try:
                    if p.stat().st_mtime >= lower_bound:
                        files.append((p, f"reports/{p.name}"))
                except OSError:
                    continue
        return files

    def _package(self, proj_path: Path, task: dict) -> str:
        if not proj_path.is_dir():
            raise RuntimeError(f"Project path not found: {proj_path}")
        files = self._fresh_files(proj_path, task)
        if not files:
            raise RuntimeError(
                f"No fresh files found in {proj_path} (window={FRESH_MINUTES}min); "
                "task did not produce persistent artifacts"
            )

        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = STATIC_DIR
        if task and task.get("workspace"):
            # 交付包放进任务自己的成果文件夹，方便整体移动
            out_dir = Path(str(task["workspace"]))
        zip_path = out_dir / f"deliverables_{ts}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for abs_path, arc_name in files:
                zf.write(abs_path, arc_name)

        names = [a for _, a in files]
        return (
            f"[PACKAGED] {zip_path.name} ({zip_path.stat().st_size / 1024:.1f} KB, {len(files)} files)\n"
            f"Download: file://{zip_path}\n"
            f"Files: {', '.join(names[:20])}{' ...' if len(names) > 20 else ''}"
        )


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-packaging")
    registry = AsyncRegistry(os.environ.get("REGISTRY_DB", "agents.db"))
    messaging = AsyncMessaging(
        os.environ.get("REDIS_HOST", "localhost"),
        int(os.environ.get("REDIS_PORT", "6379")),
    )
    worker = PackagingWorker(
        agent_id="packaging_worker",
        capabilities=PackagingWorker._class_capabilities,
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
