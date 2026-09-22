"""织光 (ZhiGuang) - Packaging Worker

将本次任务的真实交付物打包为 ZIP：以共享 project 工作区（code_execution /
file_io 的落盘目录）为基础，只打包时间窗口内的新文件，避免把历史任务
的陈旧产物混入交付包。
"""

import json
import logging
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db_paths
from async_worker_base import AsyncWorkerBase, AsyncRegistry, AsyncMessaging

logger = logging.getLogger(__name__)
PROJECT_DIR = Path(tempfile.gettempdir()) / "agent_workspace" / "project"
REPORT_DIR = Path(tempfile.gettempdir()) / "agent_workspace" / "reports"
STATIC_DIR = Path(os.environ.get("PACKAGE_OUTPUT_DIR", str(Path(tempfile.gettempdir()) / "agent_packages")))
FRESH_MINUTES = int(os.environ.get("PACKAGE_FRESH_MINUTES", "120"))

# 预载/中间产物：属于"给流程用的缓存"，不是用户要的交付物，默认不入包
# （structured_data.json / clean_chart_data.json 由结构化预载写入，
#  ranking.csv 是行情预载落盘；此前按 mtime 全量 rglob 会把它们塞进交付包）
PRELOAD_NAMES = {
    "structured_data.json", "clean_chart_data.json", "search_results.json",
    "ranking.csv",
}
# 目标明确要数据文件时，才把 data/*.csv 视为交付物
_DATA_DELIVERABLE_HINTS = (
    "csv", "excel", "xlsx", "数据文件", "数据表", "原始数据", "导出", "下载数据",
    "数据集", "附件数据",
)


def _goal_wants_data(goal: str) -> bool:
    g = str(goal or "").lower()
    return any(k in g for k in _DATA_DELIVERABLE_HINTS)


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
        # 简单任务快速路径：每任务独立工作区已是权威路径，跳过 LLM 路径解析
        if not (task and task.get("workspace") and task.get("simple")):
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
            if task and task.get("workspace"):
                proj_path = Path(str(task["workspace"])) / "project"
            else:
                # 无任务工作区时不再回落到共享目录：那里可能躺着别的任务的产物，
                # 打出来的"交付包"会混入他人文件。宁可明确失败。
                raise RuntimeError(
                    "缺少任务工作区，拒绝打包共享目录（避免混入其它任务的产物）"
                )
        return self._package(proj_path, task or {})

    def _fresh_files(self, root: Path, task: dict) -> list[tuple[Path, str]]:
        """返回 (绝对路径, 相对路径) 且属于本次任务交付物的文件。

        白名单口径：本任务的代码/资源（project/**）+ reports/*.md + charts/*.png；
        `data/*.csv` 与预载 JSON 属于流程缓存，默认排除（目标明确要数据文件时保留），
        避免把"别的步骤/预载吃的行情数据"当成用户交付物。"""
        excluded: list[str] = []
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
            # 预载/中间产物默认不入包（详见 PRELOAD_NAMES 注释）
            if Path(rel).name in PRELOAD_NAMES and not _goal_wants_data(task.get("goal", "")):
                excluded.append(rel)
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
        # 任务工作区的图表纳入交付包；data/*.csv 仅当目标明确要数据文件时纳入
        if task and task.get("workspace"):
            ws_root = Path(str(task["workspace"]))
            subs = [("charts", "*.png")]
            if _goal_wants_data(task.get("goal", "")):
                subs.append(("data", "*.csv"))
            else:
                for p in sorted((ws_root / "data").glob("*.csv")) if (ws_root / "data").is_dir() else []:
                    excluded.append(f"data/{p.name}")
            for sub, glob_pat in subs:
                sub_dir = ws_root / sub
                if not sub_dir.is_dir():
                    continue
                for p in sorted(sub_dir.glob(glob_pat)):
                    try:
                        if p.stat().st_mtime >= lower_bound:
                            files.append((p, f"{sub}/{p.name}"))
                    except OSError:
                        continue
        if excluded:
            logger.info("交付包排除 %d 个预载/中间产物: %s",
                        len(excluded), ", ".join(sorted(set(excluded))[:8]))
        return files

    @staticmethod
    def _package_manifest(task: dict, files: list) -> dict:
        """包内清单：正文/材料/图表/规则的身份 + 打包时间（缺什么就空，不编）。"""
        import hashlib
        ws = Path(str((task or {}).get("workspace") or "")) if task else None
        out: dict = {"packaged_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "schema": "weavemind.package/1"}
        def _sha(p: Path) -> str:
            try:
                return hashlib.sha256(p.read_bytes()).hexdigest()
            except Exception:
                return ""
        # 正文：包里的 reports/*.md（打包时刻的字节）
        body_hashes = {}
        for abs_path, arc in files:
            if str(arc).startswith("reports/") and str(arc).endswith(".md"):
                body_hashes[str(arc)] = _sha(Path(abs_path))
        out["bodies"] = body_hashes
        out["body_sha256"] = next(iter(body_hashes.values()), "")
        # 图表：包内 charts/*.png
        out["charts"] = {str(arc): _sha(Path(abs_path))
                         for abs_path, arc in files if str(arc).startswith("charts/")}
        # 材料与规则指纹：与版本身份同源（复用既有实现，不另算一套）
        try:
            import delivery_pipeline as dp
            tid = str((task or {}).get("task_id") or "")
            if ws is not None and tid:
                body_text = ""
                for abs_path, arc in files:
                    if str(arc).startswith("reports/") and str(arc).endswith(".md"):
                        try:
                            body_text = Path(abs_path).read_text(encoding="utf-8")
                        except Exception:
                            body_text = ""
                        break
                out["sources_fingerprint"] = dp.sources_fingerprint(tid, body_text)
                rv, rf = dp.rules_identity(tid)
                out["rules_version"], out["rules_fingerprint"] = rv, rf
        except Exception as exc:                 # noqa: BLE001 - 指纹算不出不阻断打包
            logger.warning("包内清单指纹计算失败：%s", str(exc)[:120])
        return out

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
        # 批次4：包内携带**真实 manifest**（关联正文/材料/图表/规则 hash）——下载与陈旧
        # 判定按**包内标识**对照当前采纳版，而不是靠文件时间戳（时间戳只作辅助）
        manifest = self._package_manifest(task, files)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for abs_path, arc_name in files:
                zf.write(abs_path, arc_name)
            zf.writestr("PACKAGE_MANIFEST.json",
                        json.dumps(manifest, ensure_ascii=False, indent=1))

        names = [a for _, a in files]
        return (
            f"[PACKAGED] {zip_path.name} ({zip_path.stat().st_size / 1024:.1f} KB, {len(files)} files)\n"
            f"Download: file://{zip_path}\n"
            f"Files: {', '.join(names[:20])}{' ...' if len(names) > 20 else ''}"
        )


async def amain():
    from logging_setup import setup_logging
    setup_logging("worker-packaging")
    registry = AsyncRegistry(db_paths.resolve_db_path())
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
